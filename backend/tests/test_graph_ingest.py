"""外部检索的材料增量入图：`_intake` 那一步的闸、幂等、成本账与接线。

## 为什么要有这个文件

图谱那两处出口（两段式 / 第三档骨架）吃的是同一个前提："这批材料在图里"。而材料
进图原来只有 `scripts/ingest_graph_corpus.py` 那条离线路径——生成链路自己从不建图。
于是"全新主题 + 联网搜索"这条真实流程永远过不了 `GRAPH_OUTLINE_MIN_ENTITIES`：图里
没有这批材料的边，`scoped_entity_count` 恒为 0，第三档每次返回空。

这个文件按风险从高到低盯五件事：

  1. **默认关**。它给每次生成多付十几秒与一万 token，没有实测依据不该默认生效；
  2. **不花钱的路一步都不多发**：开关关着 / 没有材料 / 没配密码 / 熔断中，四种情况下
     连一条查询都不发（`_ingest_external_graph` 的四道闸，与 `_graph_outline` 同构）；
  3. **幂等靠第三把锁**：`scoped_entity_count > 0` 就跳过这份。前两把锁（`content_hash`
     与边的 `UNIQUE`）只保证"重复不写"，保证不了"重复不抽"——而抽是每块一次 LLM 调用；
  4. **失败降级**：单份材料抛错不废整次入图，也不让生成失败；
  5. **接线**：只有"没有材料 → 外部检索补位"这条路会触发它（用户自己上传的材料不归
     这一步管），账目进 `data.diagnostics`，开关关着时一条多出来的键都没有。

## 这个文件里最要紧的那一条

`test_paper_abstract_concepts_reach_the_skeleton` 是这一轮的**主指标**：上一轮的证据是
"维基 1 份 + 论文 2 份"里 9 条一级分支全部来自维基，两份论文摘要（占 24%）一条都没
贡献。那条证据的成因不是"摘要进不了图"，而是"摘要从来没进过图"——它的
`heading_outline` 恒空，而第一档只认章节。这个用例把那条链从头钉到尾：两份无标题摘要
→ 图里只有它们的实体 → 骨架那一次的 user 里有**只来自第二份摘要**的概念名 → 模型据此
给出的分支能在摘要原文里逐字找到。

模型替身与 `test_graph_outline.py` / `test_graph_two_pass.py` 同一套写法
（`patch("app.agent.tools.chat_async")`）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    DETAIL_ONLY_PROMPT,
    GRAPH_INGEST_CONCURRENCY,
    GRAPH_INGEST_ENV,
    GRAPH_INGEST_MAX_CHUNKS,
    GRAPH_OUTLINE_ENV,
    GRAPH_OUTLINE_HEADER,
    GRAPH_OUTLINE_MIN_ENTITIES,
    GRAPH_PLAN_RULES,
    PLAN_PROMPT,
    _ExternalFetch,
    _Intake,
    _ingest_external_graph,
    _intake,
    build_registry,
    graph_ingest_enabled,
)
from app.contracts.source import Source, SourceKind
from app.knowledge import graph_ingest, graph_store

TOPIC = "城市雨污分流改造"

# 两份**没有标题**的检索材料：openalex / crossref 命中的形状（纯摘要）。
# 都要超过 `SHORT_MATERIAL_CHARS`（300）——低于它 `_intake` 会把材料当"用户附的一段
# 文字"塞进 `hint_block`，`sources` 变成空的，这条链根本不会跑。
ABSTRACT_ONE = (
    "城市雨污分流改造是改善水环境、推进生态城市建设的关键举措，但其施工受老旧城区"
    "管网现状、施工空间、新旧管网衔接及城市正常运行等因素制约，存在诸多难点。"
    "本文阐述了施工准备阶段的现场调查要点，包括既有管线的探测与标识、地下水位与"
    "土质条件的复核，以及施工期交通导改方案的编制。老旧城区的合流制管道在改造前"
    "需要先做混接点排查，逐个排口取样确认污水来源，再决定是就地截流还是彻底分流。"
    "施工期间的临时排水不能中断，通常采用分段施工加临时泵站的方式维持污水输送，"
    "避免污水溢流进入雨水系统。管道修复按破损程度分类处理，局部破损用内衬法，"
    "整段老化则整段更换，新旧管道的衔接处必须做闭水试验，试验不合格不得回填。"
)

ABSTRACT_TWO = (
    "城市雨污分流改造可改善城市水体状态，强化区域排水承载能力，管网排布设计环节"
    "存在多项实施阻碍，本文说明该项设计工作的实际作用，分别对应水体治理、排水系统"
    "扩容、内涝防治三个方面。管网布局要先把汇水分区划清楚，再按高水高排、低水低排"
    "的原则布置干管，避免长距离输送带来的埋深增加。老旧城区的道路断面狭窄，雨污"
    "两套管道难以同槽敷设，需要在既有合流管的位置做原位换管或增设雨水快排通道。"
    "设计阶段还要复核下游受纳水体的排放标准与调蓄池容积，否则改造完成后上游"
    "排水能力提升、下游反而出现新的积水点。管网排布与竖向标高必须联合设计，"
    "单一专业出图会在交叉口处产生无法施工的标高冲突。"
)

ENTITIES = [
    graph_store.GraphEntity(name="雨污分流改造", type="概念", degree=9),
    graph_store.GraphEntity(name="老旧城区管网", type="概念", degree=7),
    graph_store.GraphEntity(name="混接点排查", type="概念", degree=6),
    graph_store.GraphEntity(name="闭水试验", type="概念", degree=5),
    graph_store.GraphEntity(name="汇水分区", type="概念", degree=5),
    graph_store.GraphEntity(name="下游受纳水体", type="概念", degree=4),
    graph_store.GraphEntity(name="竖向标高", type="概念", degree=3),
    graph_store.GraphEntity(name="调蓄池容积", type="概念", degree=2),
]
LINKS = [
    graph_store.GraphLink(src="雨污分流改造", dst="老旧城区管网", relation="受限于",
                          source_id="src-crossref", start=0, end=60, weight=2),
    graph_store.GraphLink(src="汇水分区", dst="竖向标高", relation="联合设计",
                          source_id="src-crossref", start=60, end=120, weight=1),
]


def _material(text: str, *, source_id: str, filename: str) -> Source:
    return Source(id=source_id, kind=SourceKind.paper, status="parsed",
                  filename=filename, text=text)


def _abstracts() -> list[Source]:
    return [
        _material(ABSTRACT_ONE, source_id="src-openalex",
                  filename="openalex: 城市雨污分流改造工程施工难点与技术对策"),
        _material(ABSTRACT_TWO, source_id="src-crossref",
                  filename="crossref: 城市雨污分流改造工程管网布局设计难点"),
    ]


def _fake_graph(monkeypatch, *, counts: dict[str, int] | None = None,
                default: int = 0, entities=None, links=None,
                configured: bool = True, available: bool = True) -> dict:
    """把图存储换成"这批材料各有多少个带边实体"的替身，返回它收到的调用账。

    `counts` 按 `source_id` 给数：并集查询（一次传全部 id）取"任一为真"，逐份查询各取
    自己的。这样才测得出"先查并集、全没入就只查一次"那条捷径。
    """
    calls: dict = {"count": 0, "scopes": [], "subgraph": 0, "kwargs": []}
    monkeypatch.setattr(graph_store, "configured", lambda: configured)
    monkeypatch.setattr(graph_store, "available", lambda: available)

    def fake_count(scope=None):
        calls["count"] += 1
        calls["scopes"].append(list(scope or []))
        if not scope:
            return default
        if len(scope) > 1:
            return max((counts or {}).get(item, default) for item in scope)
        return (counts or {}).get(scope[0], default)

    def fake_subgraph(**kwargs):
        calls["subgraph"] += 1
        calls["kwargs"].append(kwargs)
        return list(entities if entities is not None else []), list(links or [])

    monkeypatch.setattr(graph_store, "scoped_entity_count", fake_count)
    monkeypatch.setattr(graph_store, "subgraph", fake_subgraph)
    return calls


def _fake_ingest(monkeypatch, *, fail_on: str = "") -> list[tuple[str, dict]]:
    """把 `graph_ingest.ingest_graph` 换成记账替身，返回每次调用的 (source_id, 参数)。"""
    seen: list[tuple[str, dict]] = []

    async def fake(source: Source, *, max_chunks=20, source_id=None) -> dict:
        seen.append((source.id, {"max_chunks": max_chunks, "source_id": source_id}))
        if source.id == fail_on:
            raise OSError("磁盘满了")
        return {"chunks": 2, "entities": 5, "edges": 4, "failed": 0, "aborted": False}

    monkeypatch.setattr(graph_ingest, "ingest_graph", fake)
    return seen


async def _value(payload):
    return payload


def _request(sources: list[Source]) -> AgentRunRequest:
    return AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default", sources=sources,
        budget=RunBudget(max_model_calls=20, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=1),
    )


def _ctx(sources: list[Source]) -> RunContext:
    return RunContext(request=_request(sources),
                      sources_by_id={source.id: source for source in sources})


# ---------------------------------------------------------------- 1. 开关


def test_graph_ingest_is_off_by_default(monkeypatch):
    """默认**关**：它给每次生成多付十几秒与一万 token，翻默认值要先有实测 A/B。

    与 `MINDMAP_GRAPH_OUTLINE`（默认开）口径相反，理由见 `GRAPH_INGEST_ENV` 上面那
    一段：那一档补的是缺口、不额外花钱；这一步要为每次生成真付一次抽取。
    """
    monkeypatch.delenv(GRAPH_INGEST_ENV, raising=False)
    assert graph_ingest_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " Y ", "T"])
def test_switch_reads_truthy_values(monkeypatch, value):
    monkeypatch.setenv(GRAPH_INGEST_ENV, value)
    assert graph_ingest_enabled() is True


@pytest.mark.parametrize("value", ["0", "off", "false", "no", ""])
def test_switch_reads_falsy_values(monkeypatch, value):
    monkeypatch.setenv(GRAPH_INGEST_ENV, value)
    assert graph_ingest_enabled() is False


# ---------------------------------------------------------------- 2. 四道闸：一步都不多发


def test_switch_off_costs_nothing(monkeypatch):
    """开关关着时**一次查询都不发**——这是默认路径，代价必须精确等于零。"""
    calls = _fake_graph(monkeypatch, default=99)
    seen = _fake_ingest(monkeypatch)
    monkeypatch.delenv(GRAPH_INGEST_ENV, raising=False)
    assert asyncio.run(_ingest_external_graph(_abstracts())) is None
    assert calls["count"] == 0 and not seen


def test_no_material_costs_nothing(monkeypatch):
    calls = _fake_graph(monkeypatch, default=99)
    monkeypatch.setenv(GRAPH_INGEST_ENV, "1")
    assert asyncio.run(_ingest_external_graph([])) is None
    assert calls["count"] == 0


def test_unconfigured_graph_is_never_probed(monkeypatch):
    """没配密码 = 这台机器不用图：不为它付 N 份材料的连接超时。"""
    calls = _fake_graph(monkeypatch, default=99, configured=False)
    seen = _fake_ingest(monkeypatch)
    monkeypatch.setenv(GRAPH_INGEST_ENV, "1")

    def explode():
        raise AssertionError("没配图时不该建驱动")

    monkeypatch.setattr(graph_store, "_driver", explode)
    assert asyncio.run(_ingest_external_graph(_abstracts())) is None
    assert calls["count"] == 0 and not seen


def test_open_breaker_blocks_the_ingest(monkeypatch):
    """熔断冷却中同样不连：库不在时让用户为一次注定失败的建图干等，是最差的交换。"""
    calls = _fake_graph(monkeypatch, default=99, available=False)
    seen = _fake_ingest(monkeypatch)
    monkeypatch.setenv(GRAPH_INGEST_ENV, "1")
    assert asyncio.run(_ingest_external_graph(_abstracts())) is None
    assert calls["count"] == 0 and not seen


# ---------------------------------------------------------------- 3. 幂等：第三把锁


def test_already_ingested_material_is_skipped_without_extraction(monkeypatch):
    """**这份已经在图里** → 跳过，一次抽取调用都不发。

    前两把锁（`content_hash` / 边的 `UNIQUE`）保证"重复不写"，但保证不了"重复不抽"：
    抽取是每块一次 LLM 调用，而第二把锁要等**抽完**才生效。同一主题连跑两次时外部检索
    大概率命中缓存（材料一模一样），少了这一把就是原价再付一遍抽取的钱。
    """
    calls = _fake_graph(monkeypatch, counts={"src-openalex": 4, "src-crossref": 6})
    seen = _fake_ingest(monkeypatch)
    monkeypatch.setenv(GRAPH_INGEST_ENV, "1")

    stats = asyncio.run(_ingest_external_graph(_abstracts()))
    assert stats is not None
    assert stats["skipped"] == 2 and stats["chunks"] == 0
    assert not seen
    # 并集 1 次 + 逐份 2 次：并集 > 0 时必须逐份定位到底哪一份缺。
    assert calls["count"] == 3


def test_mixed_batch_only_extracts_the_missing_ones(monkeypatch):
    _fake_graph(monkeypatch, counts={"src-openalex": 0, "src-crossref": 5})
    seen = _fake_ingest(monkeypatch)
    monkeypatch.setenv(GRAPH_INGEST_ENV, "1")

    stats = asyncio.run(_ingest_external_graph(_abstracts()))
    assert stats is not None
    assert stats["skipped"] == 1
    assert [item[0] for item in seen] == ["src-openalex"]


def test_fresh_topic_costs_exactly_one_count_query(monkeypatch):
    """全新主题（= 这一档真正要服务的场景）：一次只读查询就判定"全都要入"。"""
    calls = _fake_graph(monkeypatch, default=0)
    _fake_ingest(monkeypatch)
    monkeypatch.setenv(GRAPH_INGEST_ENV, "1")
    asyncio.run(_ingest_external_graph(_abstracts()))
    assert calls["count"] == 1
    assert calls["scopes"] == [["src-openalex", "src-crossref"]]


# ---------------------------------------------------------------- 4. 真入：参数与汇总


def test_ingest_uses_the_cost_gate_and_the_persistent_id(monkeypatch):
    """每份都带**持久 id**（不再写一次库）与成本闸（`GRAPH_INGEST_MAX_CHUNKS`）。"""
    _fake_graph(monkeypatch, default=0)
    seen = _fake_ingest(monkeypatch)
    monkeypatch.setenv(GRAPH_INGEST_ENV, "1")

    stats = asyncio.run(_ingest_external_graph(_abstracts()))
    assert stats is not None
    assert [item[0] for item in seen] == ["src-openalex", "src-crossref"]
    for source_id, kwargs in seen:
        assert kwargs["source_id"] == source_id
        assert kwargs["max_chunks"] == GRAPH_INGEST_MAX_CHUNKS
    assert stats["materials"] == 2 and stats["skipped"] == 0
    assert stats["chunks"] == 4 and stats["entities"] == 10 and stats["edges"] == 8
    assert stats["failed"] == 0 and stats["aborted"] is False


def test_one_failure_does_not_cancel_the_rest(monkeypatch):
    """单份材料写库炸掉：其余照常算进统计，整次入图不抛、生成照常继续。"""
    _fake_graph(monkeypatch, default=0)
    _fake_ingest(monkeypatch, fail_on="src-openalex")
    monkeypatch.setenv(GRAPH_INGEST_ENV, "1")

    stats = asyncio.run(_ingest_external_graph(_abstracts()))
    assert stats is not None
    # 只算成功那份的账：失败的那份一块都没进，报 4 块就是谎报。
    assert stats["chunks"] == 2 and stats["entities"] == 5 and stats["edges"] == 4


def test_concurrency_is_capped():
    """并发上限是个常数：改它要连带改交换（撞限流 vs 多等一轮），不该是随手调的数。"""
    assert 1 <= GRAPH_INGEST_CONCURRENCY <= 6


# ---------------------------------------------------------------- 5. 接线：`_intake` 那一步


def test_intake_ingests_the_external_material(monkeypatch):
    """没有材料 → 外部检索补位 → **入图**，账目挂到 `_Intake.ingested` 上。"""
    fetched = _ExternalFetch(_abstracts(), 2, 0.1)
    monkeypatch.setattr("app.agent.tools._external_sources",
                        lambda topic, ctx: _value(fetched))
    monkeypatch.setattr("app.agent.tools.external_knowledge_enabled", lambda: True)
    _fake_graph(monkeypatch, default=0)
    seen = _fake_ingest(monkeypatch)
    monkeypatch.setenv(GRAPH_INGEST_ENV, "1")

    intake = asyncio.run(_intake(TOPIC, _ctx([])))
    assert isinstance(intake, _Intake)
    assert [source.id for source in intake.sources] == ["src-openalex", "src-crossref"]
    assert intake.ingested is not None and intake.ingested["chunks"] == 4
    assert [item[0] for item in seen] == ["src-openalex", "src-crossref"]


def test_intake_does_not_touch_the_graph_with_uploaded_material(monkeypatch):
    """用户自己上传的材料**不归这一步管**：不走外部检索，也不入图。

    这条是范围边界：这一轮只补"检索回来的那批"。给上传材料入图是另一件事（那时用户
    本来就在等解析，成本结构完全不同），混进来会让"这一步花了多少"无从计算。
    """
    calls = _fake_graph(monkeypatch, default=99)
    seen = _fake_ingest(monkeypatch)
    monkeypatch.setenv(GRAPH_INGEST_ENV, "1")
    monkeypatch.setattr("app.agent.tools.external_knowledge_enabled", lambda: True)

    intake = asyncio.run(_intake(TOPIC, _ctx(_abstracts())))
    assert intake.external is None
    assert intake.ingested is None
    assert calls["count"] == 0 and not seen


# ---------------------------------------------------------------- 6. 接线：进 prompt 与诊断


def _walk(node: dict):
    yield node
    for child in node.get("children") or []:
        if isinstance(child, dict):
            yield from _walk(child)


def _tree(labels: list[str]) -> dict:
    return {"title": TOPIC, "root": {
        "id": "root", "label": TOPIC, "detail": "", "children": [
            {"id": f"b{index}", "label": label, "children": [
                {"id": f"b{index}-1", "label": f"{label}·要点", "detail": "",
                 "children": []},
            ]}
            for index, label in enumerate(labels)
        ]}}


def _branches_from(user: str) -> list[str]:
    """从图谱块里读实体名，模拟"模型按提示词挑原词"这个行为。

    假模型不读全文，它只读 prompt——而这正是要钉的那条链：**概念只要进了骨架那一次的
    user，模型就有可能把它写成一级分支**。这里把它写成"一定"，于是断言能覆盖"prompt
    里没有这个概念"这个失败模式。拿不到图谱块时退回一组与材料无关的说法（那正是"自行
    归纳"给出的东西：模板词）。
    """
    names: list[str] = []
    for line in user.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        name = stripped[2:].split("（")[0].strip()
        if name:
            names.append(name)
    return names[:3] or ["背景与意义", "主要措施", "问题与展望"]


def _run_generate(ctx: RunContext, *, branches: int = 3):
    registry = build_registry("create", has_sources=bool(ctx.request.sources),
                              has_library=False)
    seen: list[tuple[str, str]] = []
    state: dict = {"tree": None}

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": _branches_from(user)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            tree = state["tree"] or _tree(["占位"])
            return {node["id"]: "这句说明写到八十个字以上，讲清它是什么、怎么做、"
                                "有什么条件与数字，这样第三段的产出才符合长度目标。"
                    for node in _walk(tree["root"]) if node.get("id")}
        state["tree"] = _tree(_branches_from(seen[0][1]))
        return state["tree"]

    with patch("app.agent.tools.chat_async", new=chat):
        result = asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate",
                     arguments={"topic": TOPIC, "branches": branches}), ctx))
    # 树在 `ctx.candidate` 上（生成工具的出口），不在 `data` 里——`data` 是账目。
    return result, seen, ctx.candidate


def _codes(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


def test_paper_abstract_concepts_reach_the_skeleton(monkeypatch):
    """**这一轮的主指标**：论文摘要那部分的概念进不进得了骨架。

    形状与上一轮的证据一致——两份材料都是纯摘要（`heading_outline` 恒空），一份是
    openalex、一份是 crossref。区别只在于**它们在图里**：

      - 第一档（章节）在这一批上永远拿不到东西；
      - 第三档（图谱）只要图里有这批材料的边，就能把这些概念送进骨架那一次调用。

    断言分三层，缺一层这条链就没被钉住：
      1. 图够用 → 走的是图谱那一套提示词（不是"自行归纳"）；
      2. **只来自第二份摘要**的概念（`汇水分区`、`下游受纳水体`）出现在骨架的 user 里
         ——第一份摘要里一个都没有，所以它们不可能是"从别的材料串进来的"；
      3. 最终一级分支的这些名字能在摘要原文里**逐字找到**（这就是"骨架忠实度"）。
    """
    monkeypatch.setenv(GRAPH_OUTLINE_ENV, "1")
    monkeypatch.setenv(GRAPH_INGEST_ENV, "1")
    # 图里已经有这批材料（`_ingest_external_graph` 那一步的产物），所以 count 给够。
    _fake_graph(monkeypatch, default=len(ENTITIES), entities=ENTITIES, links=LINKS)
    sources = _abstracts()
    assert graph_store.scoped_entity_count(
        [source.id for source in sources]) >= GRAPH_OUTLINE_MIN_ENTITIES

    result, seen, content = _run_generate(_ctx(sources))
    assert result.ok, result.content

    system, user = seen[0]
    assert GRAPH_PLAN_RULES in system                       # 1
    assert GRAPH_OUTLINE_HEADER in user
    assert "汇水分区" in user and "下游受纳水体" in user       # 2
    assert "汇水分区" not in ABSTRACT_ONE
    assert "汇水分区" in ABSTRACT_TWO
    labels = [child.label for child in content.root.children]
    # 3. 每条分支都能在**这批摘要的原文**里逐字找到（= 骨架忠实度那个口径）。
    # 两条摘要都算：`_graph_outline` 取的是这批材料的并集，不按材料分桶。
    assert labels and all(label in ABSTRACT_ONE or label in ABSTRACT_TWO
                          for label in labels)
    assert "GRAPH_OUTLINE_USED" in _codes(result)


def test_without_the_graph_the_branches_are_template_words(monkeypatch):
    """对照组：同一批材料、图里没有它们 → 一级分支是模板词，一条都不在原文里。

    这一条与上一条**必须成对存在**。只有上一条时，"分支在原文里"可能只是主题够具体
    的运气；两条一起看，才说明那是图谱带来的。
    """
    monkeypatch.setenv(GRAPH_OUTLINE_ENV, "1")
    _fake_graph(monkeypatch, default=0, entities=ENTITIES, links=LINKS)
    result, seen, content = _run_generate(_ctx(_abstracts()))

    assert result.ok, result.content
    system, user = seen[0]
    assert GRAPH_PLAN_RULES not in system and GRAPH_OUTLINE_HEADER not in user
    labels = [child.label for child in content.root.children]
    assert labels and not any(label in ABSTRACT_ONE or label in ABSTRACT_TWO
                             for label in labels)
    assert "GRAPH_OUTLINE_USED" not in _codes(result)


def test_diagnostic_reports_the_cost(monkeypatch):
    """入图那一笔账要进 `data.diagnostics`：用户等的那十几秒除此之外没有任何痕迹
    （前端只认 `external_search` 那一对 stage 帧）。"""
    monkeypatch.setenv(GRAPH_INGEST_ENV, "1")
    monkeypatch.setenv(GRAPH_OUTLINE_ENV, "0")
    _fake_graph(monkeypatch, default=0)
    _fake_ingest(monkeypatch)
    fetched = _ExternalFetch(_abstracts(), 2, 0.1)
    monkeypatch.setattr("app.agent.tools._external_sources",
                        lambda topic, ctx: _value(fetched))
    monkeypatch.setattr("app.agent.tools.external_knowledge_enabled", lambda: True)

    result, _, _ = _run_generate(_ctx([]))
    assert result.ok, result.content
    rows = [item for item in result.data["diagnostics"]
            if item["code"] == "GRAPH_INGEST_DONE"]
    assert len(rows) == 1
    assert "2 份检索材料抽进了图谱" in rows[0]["message"]
    assert "4 块" in rows[0]["message"]


def test_switch_off_leaves_no_trace(monkeypatch):
    """开关关着时**一条多出来的诊断都没有**——与加这个能力之前逐字节相同。"""
    monkeypatch.delenv(GRAPH_INGEST_ENV, raising=False)
    monkeypatch.setenv(GRAPH_OUTLINE_ENV, "0")
    _fake_graph(monkeypatch, default=0)
    fetched = _ExternalFetch(_abstracts(), 2, 0.1)
    monkeypatch.setattr("app.agent.tools._external_sources",
                        lambda topic, ctx: _value(fetched))
    monkeypatch.setattr("app.agent.tools.external_knowledge_enabled", lambda: True)

    result, _, _ = _run_generate(_ctx([]))
    assert result.ok, result.content
    assert "GRAPH_INGEST_DONE" not in _codes(result)
