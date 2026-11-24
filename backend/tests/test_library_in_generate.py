"""生成时**由代码主动查库**（`MINDMAP_LIBRARY_IN_GENERATE`）的接线与边界。

这个开关与 `MINDMAP_LIBRARY_SEARCH` 不是一回事：那个管 `rag_query` 能不能查库
（模型想起来调的时候），这个管 `_intake` 会不会在生成之前自己查一次。默认**关**。

七组用例，回答七个问题：

  1. 关着时 `_intake` 与加这个能力之前**逐字节相同**（连一次库查询都不发生）；
  2. "材料够就不查"那道闸（`LIBRARY_SUPPLEMENT_CHARS=3000`）；
  3. 补上了：命中变成 `sources`（整份原文、持久 id）与 `hits`（父块）两样东西；
  4. **持久 id 真的回查得到**：`library.load()` 能取回原文，模型的引用能过
     `verify_source_refs` 那一道逐字校验（这是整条链最容易静默失败的地方）；
  5. 重排不可用时 **fail-closed**（`rerank_score` 全 None → 不用库）；
  6. 过门不足 `LIBRARY_MIN_HITS` 条 → 不用库（一条孤证撑不起一节证据）；
  7. 消费侧：`_generate` 把库那一节拼进 user、把引用规则拼进 system；没有库证据时
     仍是老的 `DETAIL_NO_SOURCE_RULES`（对照）。

`conftest.py` 的 autouse fixture 把 `MINDMAP_RERANK` 置 0，所以本文件里凡是需要
"重排给过分"的用例都自己构造 `rerank_score`——这样测的是门本身，不是重排能不能跑。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.agent import tools as agent_tools
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    LIBRARY_EVIDENCE_HEADER,
    LIBRARY_EVIDENCE_RULES,
    LIBRARY_IN_GENERATE_ENV,
    LIBRARY_MIN_HITS,
    LIBRARY_RELEVANCE_GATE,
    LIBRARY_SKIP_ENOUGH_MATERIAL,
    LIBRARY_SKIP_FEW_HITS,
    LIBRARY_SKIP_NO_LIBRARY,
    LIBRARY_SKIP_NO_RERANK,
    LIBRARY_SUPPLEMENT_CHARS,
    _Intake,
    _LibrarySupplement,
    _intake,
    build_registry,
)
from app.contracts.mindmap import MindMapContent, Node, SourceRef, SourceUsage
from app.contracts.source import Source, SourceKind
from app.knowledge import library
from app.knowledge.service import EvidenceHit, search_parent
from app.optimization.source_refs import verify_source_refs

TOPIC = "预算审批流程"
BRANCHES = ("预算审批", "报销时限", "数据备份")

# 一份 1500 字左右的**单主题**材料：六个环节都谈"预算审批"，于是切出来的每个父块
# 都带这个词、都能被同一条 query 命中（`search_parent` 每个父块最多给一条，这是
# "至少两条过门"的前提）。真实材料就是这个形状：同一件事跨好几段写。
#
# 三个长度约束同时成立，缺一条这条用例测的就变成另一条路：
#   - 总长 > `SHORT_MATERIAL_CHARS=300`（否则整份降级成 `hint_block`）；
#   - 总长 < `LIBRARY_SUPPLEMENT_CHARS=3000`（否则根本不查库）；
#   - 总长 > 两个 `CHUNK_MIN_CHARS`（否则只切出一个父块，凑不出 `LIBRARY_MIN_HITS`）。
MATERIAL = (
    "# 预算管理制度\n\n"
    "## 一 申报\n\n"
    "预算审批的第一步是部门申报，申报表要写清用途、金额与时点，"
    "部门负责人签字后方可进入财务复核。申报窗口每月前五个工作日开放，"
    "逾期原则上不受理，确有紧急事项的走专项通道并附书面说明。"
    "财务在收到申报的三个工作日内完成形式审核，材料不齐的一次性退回并列出缺项。\n\n"
    "## 二 复核\n\n"
    "预算审批的复核环节由财务与业务双方共同完成，重点是金额口径与历史执行率。"
    "上一期执行率低于百分之六十的项目，复核时要说明差异原因，"
    "连续两期偏低的项目由财务约谈经办人。复核意见必须落纸，"
    "口头同意不算数，后续审计只认留痕记录。\n\n"
    "## 三 批复\n\n"
    "预算审批的批复权限按金额分档：五十万元以内由财务负责人核准，"
    "五十万元至两百万元报总经理，两百万元以上报董事会。"
    "批复结果以书面形式下发，附核准金额与生效时点，"
    "未获批复的申报不得先行支出，已支出的按违规处理并追回。\n\n"
    "## 四 中期调整\n\n"
    "预算审批通过后原则上不再调整，确需调整的按同一套流程重走一遍，"
    "并说明调整幅度与影响面。中期调整每季度集中受理一次，"
    "调整累计幅度超过原批复金额百分之二十的，要重新提交董事会审议。\n\n"
    "## 五 归档\n\n"
    "预算审批的全过程材料由财务归档，包括申报表、复核意见、批复文件与执行台账。"
    "归档以年度为周期，纸质与电子两套并存，电子件按项目编号命名，"
    "保存期满五年后统一销毁并留存销毁清单。\n\n"
    "## 六 审计与追责\n\n"
    "预算审批的执行情况每年接受一次内审抽查，抽查比例不低于项目总数的百分之十。"
    "发现虚报用途、化整为零规避批复权限的，收回已批额度并追究经办人与审核人责任。"
    "审计结论纳入部门年度考核，连续两年被点名的部门下一年度申报额度按比例核减。\n"
)


def _request(sources: list[Source] | None = None, *, intent: str = "create",
             max_model_calls: int = 20) -> AgentRunRequest:
    return AgentRunRequest(
        goal=TOPIC, intent=intent, thinking_model="default", sources=sources or [],
        budget=RunBudget(max_model_calls=max_model_calls, max_tool_calls=8,
                         max_seconds=60.0, max_output_tokens=8192, max_repairs=1),
    )


def _ctx(sources: list[Source] | None = None, *, intent: str = "create") -> RunContext:
    payload = sources or []
    return RunContext(
        request=_request(payload, intent=intent),
        sources_by_id={source.id: source for source in payload},
    )


def _stored(text: str = MATERIAL, **kwargs) -> Source:
    return Source(kind=SourceKind.text, status="parsed", text=text, **kwargs)


def _library_hits(topic: str = TOPIC, top_k: int = 8) -> list[EvidenceHit]:
    """真库检索一次（父块、持久 id、真实偏移）。分数由调用方决定要不要注入。"""
    return search_parent([], topic, top_k, library=True)


def _scored(hits: list[EvidenceHit], score: float = 0.9) -> list[EvidenceHit]:
    """把重排分注入命中。`conftest` 把 `MINDMAP_RERANK` 置 0，真跑不会给出这个分。"""
    return [hit.model_copy(update={"rerank_score": score}) for hit in hits]


def _enable(monkeypatch) -> None:
    monkeypatch.setenv(LIBRARY_IN_GENERATE_ENV, "1")
    # 外部检索在 conftest 里已置 0（`SWITCHES`），这里再显式关一次：这条链上"没有
    # 材料"会先走外部检索，而本文件要测的是**库**那一步，不该被一次外网往返打断。
    monkeypatch.setenv("MINDMAP_EXTERNAL_KNOWLEDGE", "0")


async def _run_intake(ctx: RunContext, topic: str = TOPIC) -> _Intake:
    return await _intake(topic, ctx)


# ---------------------------------------------------------------- 1 默认关且零查询


def test_switch_defaults_to_off():
    """默认关。"所有新功能默认关"是这个仓库的一条约定，不是随手写的缺省值。"""
    assert agent_tools.library_in_generate_enabled() is False


def test_switch_off_never_touches_the_library(monkeypatch):
    """关着时：`library` 是 None、一次库检索都不发、其余字段与改动前完全一致。

    断言三样东西：`search_parent` 没被调用（零查询，不是"查了丢掉"）、
    `library` 是 None（不是"查过但没命中"的空对象）、`sources`/`full_text`
    仍是老口径。
    """
    monkeypatch.delenv(LIBRARY_IN_GENERATE_ENV, raising=False)
    material = Source(id="s-budget", kind=SourceKind.text, status="parsed",
                      filename="制度.md", text=MATERIAL)
    ctx = _ctx([material])

    with patch("app.agent.tools.search_parent", side_effect=AssertionError("不该查库")):
        intake = asyncio.run(_run_intake(ctx))

    assert intake.library is None
    assert [source.id for source in intake.sources] == ["s-budget"]
    assert intake.full_text_chars == len(MATERIAL.strip())


# ---------------------------------------------------------------- 2 材料够就不查


def test_enough_material_skips_the_library(monkeypatch):
    """材料合计 ≥ `LIBRARY_SUPPLEMENT_CHARS` 时**不查库**，并说出原因。

    阈值本身也断言一次：它是这个功能唯一的"补足判据"，改它必须是一次有意识的改动
    （依据写在 `LIBRARY_SUPPLEMENT_CHARS` 的注释里）。
    """
    _enable(monkeypatch)
    assert LIBRARY_SUPPLEMENT_CHARS == 3000
    long_material = Source(id="s-long", kind=SourceKind.text, status="parsed",
                           filename="长材料.md",
                           text=MATERIAL * 5)          # ≈ 3500 字，超阈值
    ctx = _ctx([long_material])

    with patch("app.agent.tools.search_parent", side_effect=AssertionError("不该查库")):
        intake = asyncio.run(_run_intake(ctx))

    assert intake.library is not None
    assert intake.library.skip == LIBRARY_SKIP_ENOUGH_MATERIAL
    assert intake.library.hits == []


def test_short_material_still_gets_library_supplement(monkeypatch):
    """300–3000 字之间**会**查库：那正是"用户给了一点、不够写一棵树"的区间。

    这是方案 b（"材料不足才补"）而不是方案 a（"有材料就完全不查"）的全部区别。
    """
    _enable(monkeypatch)
    thin = Source(id="s-thin", kind=SourceKind.text, status="parsed",
                  filename="薄材料.md", text="预算审批由部门负责人签核，财务执行付款。")
    library.ingest(_stored(filename="制度.md"))
    ctx = _ctx([thin])
    hits = _scored(_library_hits())

    with patch("app.agent.tools.search_parent", return_value=hits):
        intake = asyncio.run(_run_intake(ctx))

    assert intake.library is not None
    assert intake.library.skip == "", intake.library.skip
    assert intake.library.hits


# ---------------------------------------------------------------- 3 补上来的形状


def test_supplement_keeps_parent_blocks_and_full_sources_apart(monkeypatch):
    """命中是**父块**（进 prompt），`sources` 是**整份原文**（给校验器）——两样都要。

    少 `hits` 就没有片段可给模型看；少 `sources` 则收尾时引用查不到原文，会被判
    `SOURCE_TEXT_UNAVAILABLE` 静默剔掉（`_verify_sources` 的 docstring 记着这个坑）。
    """
    _enable(monkeypatch)
    source_id = library.ingest(_stored(filename="制度.md"))
    ctx = _ctx([])
    hits = _scored(_library_hits())

    with patch("app.agent.tools.search_parent", return_value=hits):
        intake = asyncio.run(_run_intake(ctx))

    supplement = intake.library
    assert supplement is not None and supplement.skip == ""
    # 库来源**不进** sources：它不属于"按支检索的语料"，理由见 `_Intake` 的 docstring。
    assert intake.sources == []
    assert intake.full_text is False and intake.full_text_chars == 0
    assert [source.id for source in supplement.sources] == [source_id]
    assert supplement.sources[0].text == MATERIAL          # 整份，不是块
    assert all(hit.end - hit.start < len(MATERIAL) for hit in supplement.hits)
    assert supplement.chars == sum(hit.end - hit.start for hit in supplement.hits)
    # 命中区间与来源都进了 ctx：收尾校验要按它们判"这句在不在原文里"
    assert source_id in ctx.library_sources
    assert ctx.evidence


def test_persistent_ids_survive_reference_verification(monkeypatch):
    """**持久 id 能回查**：模型照库那一节抄的引用不被逐字校验剔掉。

    这是整条链接上去之后最容易静默失败的环节——检索成功、引用写对、收尾时被删，
    界面上只表现为"这一支没有证据"。用例直接走 `verify_source_refs`（收尾用的
    就是它），断言引用还在、且没有"来源原文不可用"那条诊断。
    """
    _enable(monkeypatch)
    library.ingest(_stored(filename="制度.md"))
    ctx = _ctx([])
    hits = _scored(_library_hits())

    with patch("app.agent.tools.search_parent", return_value=hits):
        intake = asyncio.run(_run_intake(ctx))

    hit = intake.library.hits[0]
    source = library.load(hit.source_id)
    assert source is not None                     # 持久 id 回查得到
    content = MindMapContent(
        title=TOPIC,
        # 契约第二层要求每条引用的 `source_id` 都登记在 `source_usage` 里；真实链路上
        # 这一步由 `validate_generated_content` 用 `_verify_sources(ctx)` 重建
        # （`_generate` 的收尾），这里手工给出同一份集合。
        source_usage=[SourceUsage(source_id=hit.source_id, kind=source.kind,
                                  filename=source.filename,
                                  char_count=len(source.text))],
        root=Node(id="root", label=TOPIC, children=[
            Node(id="n1", label="预算审批",
                 source_refs=[SourceRef(source_id=hit.source_id, start=hit.start,
                                        end=hit.end, quote=hit.quote[:300])]),
        ]),
    )

    diagnostics = verify_source_refs(
        content, agent_tools._verify_sources(ctx), remove_invalid=True)

    assert content.root.children[0].source_refs, "引用被剔掉了：持久 id 没有回查成功"
    codes = [item.code for item in diagnostics]
    assert "SOURCE_TEXT_UNAVAILABLE" not in codes
    assert "SOURCE_QUOTE_MISMATCH" not in codes


# ---------------------------------------------------------------- 4 两道门


def test_missing_rerank_scores_fail_closed(monkeypatch):
    """`rerank_score` 全是 None（重排没生效）时**不用库**。

    这是这个功能的降级判据，不是注释：没有 rerank 的库命中和"随便捞几句"没有区别
    ——实测「城市雨污分流改造」8 条命中里 6 条来自开发文档，而 BM25 在库上方向是反的
    （真相关的「海绵城市建设」1.31–1.89、全无关的「量子计算」6.11–9.94）。
    """
    _enable(monkeypatch)
    library.ingest(_stored(filename="制度.md"))
    ctx = _ctx([])
    # `conftest` 把 `MINDMAP_RERANK` 置 0，真检索给出的正是"没有重排分"的形状。
    raw_hits = _library_hits()
    assert raw_hits and all(hit.rerank_score is None for hit in raw_hits)

    with patch("app.agent.tools.search_parent", return_value=raw_hits):
        intake = asyncio.run(_run_intake(ctx))

    assert intake.library is not None
    assert intake.library.skip == LIBRARY_SKIP_NO_RERANK
    assert intake.library.sources == []
    assert ctx.library_sources == {}


def test_gate_drops_hits_below_the_threshold(monkeypatch):
    """门是**分数**判据：`LIBRARY_RELEVANCE_GATE - 0.01` 全丢。"""
    _enable(monkeypatch)
    library.ingest(_stored(filename="制度.md"))
    ctx = _ctx([])
    raw = _library_hits()
    assert len(raw) >= LIBRARY_MIN_HITS, "这份材料要切出至少两个父块"
    below = [hit.model_copy(update={"rerank_score": LIBRARY_RELEVANCE_GATE - 0.01})
             for hit in raw]

    with patch("app.agent.tools.search_parent", return_value=below):
        intake = asyncio.run(_run_intake(ctx))

    assert intake.library is not None
    assert intake.library.skip == LIBRARY_SKIP_FEW_HITS
    assert intake.library.hits == []


def test_too_few_hits_is_not_enough(monkeypatch):
    """只有一条过门时也不用库：一条孤证撑不起一节证据，而它仍会占掉"每节点必引用"。"""
    _enable(monkeypatch)
    assert LIBRARY_MIN_HITS == 2
    library.ingest(_stored(filename="制度.md"))
    ctx = _ctx([])
    raw = _library_hits()
    only_one = [raw[0].model_copy(update={"rerank_score": 0.9}),
                *[hit.model_copy(update={"rerank_score": 0.01}) for hit in raw[1:]]]

    with patch("app.agent.tools.search_parent", return_value=only_one):
        intake = asyncio.run(_run_intake(ctx))

    assert intake.library is not None
    assert intake.library.skip == LIBRARY_SKIP_FEW_HITS
    assert intake.library.sources == []


def test_empty_library_is_not_a_failure(monkeypatch):
    """库是空的时候不查（`library_available()` 那道闸），也不报错。"""
    _enable(monkeypatch)
    ctx = _ctx([])

    with patch("app.agent.tools.search_parent", side_effect=AssertionError("空库不该查")):
        intake = asyncio.run(_run_intake(ctx))

    assert intake.library is not None
    assert intake.library.skip == LIBRARY_SKIP_NO_LIBRARY


def test_retrieval_failure_degrades_without_raising(monkeypatch):
    """库检索抛错：整次生成照常，`skip="error"` 把这件事说出来。"""
    _enable(monkeypatch)
    library.ingest(_stored(filename="制度.md"))
    ctx = _ctx([])

    with patch("app.agent.tools.search_parent", side_effect=RuntimeError("库文件被锁")):
        intake = asyncio.run(_run_intake(ctx))

    assert intake.library is not None
    assert intake.library.skip == "error"
    assert intake.sources == []


# ---------------------------------------------------------------- 5 消费侧：prompt


def _tree_chat(seen: list[tuple[str, str]], branches: tuple[str, ...]):
    """模型替身：骨架给分支，结构写整棵树，说明那一次按 id 回一句话。"""
    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        seen.append((system, user))
        if agent_tools.PLAN_PROMPT in system:
            return {"branches": list(branches)}
        if agent_tools.DETAIL_ONLY_PROMPT in system:
            ids = [line.split("id=", 1)[1].split("｜", 1)[0]
                   for line in user.splitlines() if "id=" in line]
            return {node_id: "一句说明。" for node_id in ids}
        children = [
            {"id": f"b{index}", "label": title, "children": [
                {"id": f"b{index}-1", "label": f"{title}·一", "detail": "一句说明。"},
            ]}
            for index, title in enumerate(branches)
        ]
        return {"title": TOPIC, "root": {
            "id": "root", "label": TOPIC, "detail": "中心主题", "children": children}}

    return chat


def _generate(ctx: RunContext, chat):
    registry = build_registry("create", has_sources=False, has_library=False)
    with patch("app.agent.tools.chat_async", new=chat):
        return asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate",
                     arguments={"topic": TOPIC, "branches": 3}), ctx))


def _supplemented_ctx() -> tuple[RunContext, _Intake]:
    """构造一份"库补上了"的 `_Intake`（消费侧用例只关心它，不重跑检索）。"""
    ctx = _ctx([])
    source_id = library.ingest(_stored(filename="制度.md"))
    source = library.load(source_id)
    ctx.record_library_sources([source])
    supplement = _LibrarySupplement(hits=_scored(_library_hits()), sources=[source],
                                    skip="", seconds=0.4)
    intake = _Intake(sources=[], short_material=[], material_chars=0, hint_block="",
                     external=None, library=supplement)
    return ctx, intake


def _structure_calls(seen: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """结构那一次（不是骨架、也不是只写说明那一次）。"""
    return [(system, user) for system, user in seen
            if agent_tools.PLAN_PROMPT not in system
            and agent_tools.DETAIL_ONLY_PROMPT not in system]


def test_generate_carries_the_library_section_and_rules(monkeypatch):
    """库补上了：user 末尾多一节库原文，system 换成库那套引用规则。

    两处都要断言——只拼 user 不换 system 时，模型会把库原文当成"用户上传的材料"，
    而 system 里那半句"没有可标出处材料"会让它一条引用都不写。
    """
    monkeypatch.setenv(LIBRARY_IN_GENERATE_ENV, "1")
    ctx, intake = _supplemented_ctx()
    seen: list[tuple[str, str]] = []

    with patch("app.agent.tools._intake", return_value=intake):
        result = _generate(ctx, _tree_chat(seen, BRANCHES))

    assert result.ok, result.content
    calls = _structure_calls(seen)
    assert calls
    user = calls[-1][1]
    header = LIBRARY_EVIDENCE_HEADER.format(
        count=len(intake.library.hits), chars=intake.library.chars)
    assert header in user
    assert 'source_id: "' in user
    assert LIBRARY_EVIDENCE_RULES.strip() in calls[-1][0]
    assert agent_tools.DETAIL_NO_SOURCE_RULES not in calls[-1][0]


def test_generate_without_library_keeps_the_no_source_rules(monkeypatch):
    """没有库证据时仍是老样子：system 里是 `DETAIL_NO_SOURCE_RULES`，user 里没有库节。

    这是"开关关着逐字节相同"在消费侧的对照臂。
    """
    monkeypatch.delenv(LIBRARY_IN_GENERATE_ENV, raising=False)
    ctx = _ctx([])
    seen: list[tuple[str, str]] = []
    intake = _Intake(sources=[], short_material=[], material_chars=0, hint_block="",
                     external=None, library=None)

    with patch("app.agent.tools._intake", return_value=intake):
        result = _generate(ctx, _tree_chat(seen, BRANCHES))

    assert result.ok, result.content
    for _system, user in seen:
        assert "知识库检索到的相关原文" not in user
    assert agent_tools.DETAIL_NO_SOURCE_RULES in _structure_calls(seen)[-1][0]


def test_skeleton_call_sees_the_library_too(monkeypatch):
    """骨架那一次也拿到库片段：一级分支决定整棵树的覆盖，只给结构那一步看会错位。"""
    monkeypatch.setenv(LIBRARY_IN_GENERATE_ENV, "1")
    ctx, intake = _supplemented_ctx()
    seen: list[tuple[str, str]] = []

    with patch("app.agent.tools._intake", return_value=intake):
        result = _generate(ctx, _tree_chat(seen, BRANCHES))

    assert result.ok, result.content
    skeleton = [(system, user) for system, user in seen
                if agent_tools.PLAN_PROMPT in system]
    assert skeleton, "没有发出骨架调用"
    system, user = skeleton[0]
    assert "知识库检索到的相关原文" in user
    # 骨架这一支**不该**说"没有可标出处的材料"——user 末尾正摆着库原文
    assert agent_tools.DETAIL_NO_SOURCE_RULES not in system


def test_diagnostic_reports_what_the_library_added(monkeypatch):
    """补上了要在诊断里报账：几段 / 几份 / 多少字 / 多久。"""
    monkeypatch.setenv(LIBRARY_IN_GENERATE_ENV, "1")
    ctx, intake = _supplemented_ctx()
    seen: list[tuple[str, str]] = []

    with patch("app.agent.tools._intake", return_value=intake):
        result = _generate(ctx, _tree_chat(seen, BRANCHES))

    assert result.ok, result.content
    codes = [item["code"] for item in result.data["diagnostics"]]
    assert "LIBRARY_SUPPLEMENT" in codes
    message = next(item["message"] for item in result.data["diagnostics"]
                   if item["code"] == "LIBRARY_SUPPLEMENT")
    assert f"{len(intake.library.hits)} 段" in message
    assert "引用可以照抄" in message


def test_skipped_diagnostic_explains_the_no_rerank_case(monkeypatch):
    """没补上也要有一句话，且"重排没生效"必须说清是重排的错。"""
    monkeypatch.setenv(LIBRARY_IN_GENERATE_ENV, "1")
    ctx = _ctx([])
    intake = _Intake(sources=[], short_material=[], material_chars=0, hint_block="",
                     external=None,
                     library=_LibrarySupplement([], [], LIBRARY_SKIP_NO_RERANK, 0.2))
    seen: list[tuple[str, str]] = []

    with patch("app.agent.tools._intake", return_value=intake):
        result = _generate(ctx, _tree_chat(seen, BRANCHES))

    assert result.ok, result.content
    entry = next(item for item in result.data["diagnostics"]
                 if item["code"] == "LIBRARY_SUPPLEMENT_SKIPPED")
    assert "重排" in entry["message"]
    # 没补上时必须回落到老规则（否则模型拿不到任何引用指令）
    assert agent_tools.DETAIL_NO_SOURCE_RULES in _structure_calls(seen)[-1][0]


def test_source_usage_only_counts_sources_actually_used(monkeypatch):
    """收尾的 `source_usage` 只认真的被引用到的来源——库那份也在其中。

    这条钉住"库来源能进 `source_usage`"：不进的话界面上"本次材料"永远是 0 份，
    而图上明明挂着库里的引用。
    """
    monkeypatch.setenv(LIBRARY_IN_GENERATE_ENV, "1")
    ctx, intake = _supplemented_ctx()
    hit = intake.library.hits[0]

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if agent_tools.PLAN_PROMPT in system:
            return {"branches": list(BRANCHES)}
        if agent_tools.DETAIL_ONLY_PROMPT in system:
            ids = [line.split("id=", 1)[1].split("｜", 1)[0]
                   for line in user.splitlines() if "id=" in line]
            return {node_id: "一句说明。" for node_id in ids}
        return {"title": TOPIC, "root": {
            "id": "root", "label": TOPIC, "detail": "中心主题", "children": [{
                "id": "b0", "label": BRANCHES[0], "children": [{
                    "id": "b0-1", "label": "审批权责", "detail": "一句说明。",
                    "source_refs": [{"source_id": hit.source_id, "start": hit.start,
                                     "end": hit.end, "quote": hit.quote[:300]}],
                }],
            }, {
                "id": "b1", "label": BRANCHES[1], "children": [],
            }, {
                "id": "b2", "label": BRANCHES[2], "children": [],
            }]}}

    with patch("app.agent.tools._intake", return_value=intake):
        result = _generate(ctx, chat)

    assert result.ok, result.content
    usage = ctx.candidate.source_usage
    assert [entry.source_id for entry in usage] == [hit.source_id]
    assert usage[0].char_count >= 1
