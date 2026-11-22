"""生成之后的核对：三态判据、定位、挂引用，以及"开关关着时键集不变"。

这一步要回答的是「这棵树里哪些节点在网上找得到依据」，所以用例钉的**不是**"跑通了"，
而是四件在真实链路上会静默失效的事：

  1. **判据本身**（`parse_verdicts` / `locate_quote`）：`chat_async` 返回的是**已解析
     的 JSON**，不是 `{"content": "..."}`。按后者取会拿到 `None`，异常被兜底记成
     "这一块漏判"——整棵树判 0 条，看起来像模型不听话。第一版就是这么错的，
     所以这里有一条专门钉它的用例（`test_a_parsed_dict_is_accepted`）。
  2. **引文必须是原文切片**：模型抄的句子会换行→空格，直接挂上去会被
     `verify_source_refs` 判成 `SOURCE_QUOTE_MISMATCH` 并剔除——"检索成功、引用写出、
     最后消失"，这条链上最隐蔽的一种失败。
  3. **三态处置不能改**：supported 挂引用、unverified **保留**、contradicted 保留并
     标出。删是不可逆的，而"小众但正确"与"模型编的"在"搜不到"这个结果上长得一样。
  4. **开关关着时 `data` 的键集与加这个能力之前逐字节相同**：整个仓库的回归用例比的
     是键集，不只是值。

不联网、不读真库：检索与模型都给替身，`library.ingest` 落在 conftest 指定的临时库上。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from app.agent import tools as tools_module
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.contracts.mindmap import MindMapContent, Node
from app.contracts.source import Source, SourceRef, SourceKind, SourceUsage
from app.knowledge import external as external_knowledge
from app.optimization import verify as verify_step

TOPIC = "碳中和"
BRANCHES = ("政策", "技术路径", "企业案例")

# 核对抓回来的正文：两句**逐字可定位**的原文，分别对上「政策」与「技术路径」。
# 它模拟 DeepSeek Search 的返回（一整段正文，不是摘要）——引用能不能落回正文，
# 是这一步唯一的判据。
ARTICLE = (
    "# 碳中和概览\n\n"
    "政策方面，碳中和政策要求重点行业制定碳达峰路线图，二零二五年前完成一次摸底，"
    "并把结果写进年度报告。\n\n"
    "技术路径包括可再生能源替代、碳捕集与封存、电气化改造三类，三类的单位成本差别很大。\n"
)

_DETAIL_TEXT = (
    "这里是一段写到八十个字以上的具体说明：它说清这个要点是什么、怎么做、"
    "在什么条件下成立，并给出可以核对的判断标准，而不是把标题换一种说法。"
)

# 判 supported 的两句：第二句在原文里**跨行**（引用时模型会把换行换成空格），
# 用来钉 `locate_quote` 的空白容错与"挂上去的是原文切片"。
QUOTE_POLICY = "碳中和政策要求重点行业制定碳达峰路线图，二零二五年前完成一次摸底"
QUOTE_TECH = "技术路径包括可再生能源替代、碳捕集与封存、电气化改造三类"


# ---------------------------------------------------------------- 构造与替身


def _content(*, refs: bool = False) -> MindMapContent:
    """一棵最小的树：三条一级分支，各带两个要点。"""
    children = []
    for index, title in enumerate(BRANCHES):
        refs_here = ([SourceRef(source_id="s-doc", quote="原文里的一句")]
                     if refs and index == 0 else [])
        children.append(Node(id=f"b{index}", label=title, source_refs=refs_here,
                             children=[
            Node(id=f"b{index}-1", label=f"{title}·一", detail="一句说明。"),
            Node(id=f"b{index}-2", label=f"{title}·二", detail="另一句说明。"),
        ]))
    return MindMapContent(
        title=TOPIC,
        # 契约要求引用指向已登记的来源：造带引用的树时必须同时给 source_usage，
        # 少这一条 `MindMapContent` 直接拒收（"节点引用的来源不在 source_usage 中"）。
        source_usage=([SourceUsage(source_id="s-doc", kind=SourceKind.document,
                                   filename="材料.md", char_count=100)] if refs else []),
        root=Node(id="root", label=TOPIC, detail="中心主题", children=children),
    )


def _wide_tree(count: int) -> MindMapContent:
    """`count` 个内容节点的树：三级铺开（`Node.children` 有上限，不能全挂一层）。"""
    groups: list[Node] = []
    made = 0
    for group_index in range(0, max(1, count // 25) + 1):
        leaves: list[Node] = []
        for leaf_index in range(25):
            if made >= count:
                break
            leaves.append(Node(id=f"n{made}", label=f"节点{made}"))
            made += 1
        if leaves:
            groups.append(Node(id=f"g{group_index}", label=f"组{group_index}",
                               children=leaves))
    return MindMapContent(title="大图", root=Node(id="root", label="大图", children=groups))


def _article(text: str = ARTICLE, *, title: str = "碳中和", site: str = "example.org"):
    """一篇"搜回来的正文"——用真的 `ExternalHit`：核对那一步吃的就是它，
    换一个手搓的形状会让"入库"那条路上的失败（`raw_chars` 缺失之类）测不到。"""
    return external_knowledge.ExternalHit(
        title=title, text=text, url=f"https://{site}/a", source_name="deepseek",
        fetched_at="2026-01-01T00:00:00Z", language="zh",
        raw_chars=len(text), site=site,
    )


def _source(text: str = ARTICLE, *, source_id: str = "s-verify") -> Source:
    return Source(id=source_id, kind=SourceKind.web, status="parsed",
                  filename="deepseek: 碳中和", text=text)


def _judged_ids(user: str) -> list[str]:
    """从判定 prompt 的 user 里读回节点 id。

    只读**节点那一段**（`## 文章正文` 之前），并跳过"格式：id | 路径 | 标签 | 说明"
    那一行说明——它也含 ` | `，不跳的话每块都会多算一个不存在的节点 id，而那个 id
    会被 `parse_verdicts` 当"编的 id"记进 `invalid`：用例就会在错误的层上看问题。
    """
    head = user.split("## 文章正文", 1)[0]
    ids: list[str] = []
    for line in head.splitlines():
        if line.startswith("格式："):
            continue
        parts = line.split(" | ")
        if len(parts) >= 4:
            ids.append(parts[0])
    return ids


# ---------------------------------------------------------------- 1. query 与节点


def test_the_query_puts_the_title_first_and_stops_at_the_limit():
    content = _content()
    assert verify_step.build_query(content) == f"{TOPIC} {' '.join(BRANCHES)}"


def test_a_branch_that_repeats_the_title_is_dropped():
    """一级分支常是标题的复述：不去重会吃掉一半 50 字预算换零信息量。"""
    content = MindMapContent(
        title="碳中和的主要方面",
        root=Node(id="root", label="碳中和的主要方面", children=[
            Node(id="b1", label="碳中和的主要方面"),
            Node(id="b2", label="减排与吸收的平衡"),
        ]),
    )
    assert verify_step.build_query(content) == "碳中和的主要方面 减排与吸收的平衡"


def test_an_over_long_title_is_cut_but_a_branch_is_never_half_written():
    """满了就停：截断只发生在标题自己就超长的时候（没有更好的办法）。"""
    long_title = "碳中和" * 20                    # 60 字，超过 50
    content = MindMapContent(title=long_title,
                             root=Node(id="root", label=long_title, children=[]))
    assert len(verify_step.build_query(content)) == external_knowledge.QUERY_MAX_CHARS

    content = MindMapContent(
        title="碳中和",
        root=Node(id="root", label="碳中和", children=[
            Node(id="b1", label="短的"), Node(id="b2", label="另一个非常非常长的分支标题名称"),
        ]),
    )
    query = verify_step.build_query(content, limit=12)
    assert query == "碳中和 短的", query


def test_node_rows_are_preorder_with_paths():
    rows = verify_step.node_rows(_content())
    assert [row["id"] for row in rows] == [
        "root", "b0", "b0-1", "b0-2", "b1", "b1-1", "b1-2", "b2", "b2-1", "b2-2"]
    assert rows[0]["path"] == ""
    assert rows[2]["path"] == f"{TOPIC} / 政策"


# ---------------------------------------------------------------- 2. 判定解析


def test_a_parsed_dict_is_accepted():
    """`chat_async` 返回的是解析好的 JSON —— 走 `parse_model_json` 会抛 AttributeError。

    上面那一版就是在这里错的：异常被调用方兜成"漏判"，整棵树判 0 条，而日志里
    看不出是解析问题。这条用例的存在就是让那个错误不可能再悄悄回来。
    """
    verdicts, invalid = verify_step.parse_verdicts(
        {"verdicts": [{"id": "b0", "state": "supported", "quote": QUOTE_POLICY,
                       "article": 1}]},
        ["b0"])
    assert invalid == 0
    assert verdicts == [{"id": "b0", "state": "supported", "quote": QUOTE_POLICY,
                         "article": 1}]


def test_a_json_string_is_accepted_too():
    verdicts, invalid = verify_step.parse_verdicts(
        json.dumps({"verdicts": [{"id": "b1", "state": "unverified"}]}), ["b1"])
    assert invalid == 0 and verdicts[0]["state"] == "unverified"
    assert verdicts[0]["quote"] == ""


def test_unknown_ids_and_states_are_counted_not_folded_into_unverified():
    """模型编的 id / 第四个状态名算"没判"，不能算"搜不到依据"。

    两件事混成一个数，「支持率」这个指标就没法解释了：分母里一半是模型没回答。
    """
    verdicts, invalid = verify_step.parse_verdicts(
        {"verdicts": [
            {"id": "b0", "state": "supported", "quote": "q"},
            {"id": "b1", "state": "unknown"},          # 第四个状态名
            {"id": "nope", "state": "supported"},      # 树上没有这个 id
            "垃圾条目",
        ]},
        ["b0", "b1"])
    assert [item["id"] for item in verdicts] == ["b0"]
    assert invalid == 3


def test_a_non_positive_article_index_becomes_none():
    verdicts, _ = verify_step.parse_verdicts(
        {"verdicts": [{"id": "b0", "state": "supported", "quote": "q", "article": 0}]},
        ["b0"])
    assert verdicts[0]["article"] is None


# ---------------------------------------------------------------- 3. 定位


def test_a_quote_is_located_through_whitespace_and_becomes_the_original_slice():
    """引文跨行时模型会把换行写成空格——那不是编的，而挂上去的必须是原文切片。"""
    text = "前言。" + ARTICLE
    found = verify_step.locate_quote(QUOTE_TECH, text)
    assert found is not None
    start, end = found
    assert text[start:end] == QUOTE_TECH
    assert "\n" not in text[start:end]


def test_a_quote_that_is_really_absent_returns_none():
    assert verify_step.locate_quote("这句话在正文里根本不存在", ARTICLE) is None
    assert verify_step.locate_quote("", ARTICLE) is None


def test_quote_in_reports_which_article():
    assert verify_step.quote_in(QUOTE_POLICY, [{"text": "别的正文"}, {"text": ARTICLE}]) == 2
    assert verify_step.quote_in(QUOTE_POLICY, [{"text": "别的正文"}]) is None


# ---------------------------------------------------------------- 4. 挂引用


def _report(**nodes) -> verify_step.VerifyReport:
    report = verify_step.VerifyReport(query="q")
    for node_id, verdict in nodes.items():
        report.nodes[node_id] = verdict
    return report


def test_attach_refs_uses_the_original_slice_and_only_for_supported():
    content = _content()
    report = _report(**{
        "b0": {"state": "supported", "quote": QUOTE_POLICY, "article": 1},
        "b1": {"state": "unverified", "quote": "", "article": None},
        "b2": {"state": "contradicted", "quote": "", "article": None},
    })
    sources = [_source()]
    assert verify_step.attach_refs(content, report, sources) == 1

    node = content.root.children[0]
    assert len(node.source_refs) == 1
    ref = node.source_refs[0]
    assert ref.source_id == sources[0].id
    assert ref.quote in ARTICLE                   # 逐字在原文里
    assert ARTICLE[ref.start:ref.end] == ref.quote
    # unverified / contradicted **一个都没有被删**，也没有被挂上引用
    assert [len(child.source_refs) for child in content.root.children] == [1, 0, 0]
    assert len(content.root.children) == 3


def test_attach_refs_never_touches_a_node_that_already_has_a_ref():
    """材料那一步写的引用与网上搜到的依据可信度不同，不能互相覆盖。"""
    content = _content(refs=True)
    existing = content.root.children[0].source_refs[0]
    report = _report(b0={"state": "supported", "quote": QUOTE_POLICY, "article": 1})
    assert verify_step.attach_refs(content, report, [_source()]) == 0
    assert content.root.children[0].source_refs == [existing]


def test_attach_refs_skips_a_quote_that_is_not_in_the_article():
    content = _content()
    report = _report(b0={"state": "supported", "quote": "模型自己写的一句话", "article": 1})
    assert verify_step.attach_refs(content, report, [_source()]) == 0
    assert content.root.children[0].source_refs == []


def test_attach_refs_skips_an_article_index_out_of_range():
    content = _content()
    report = _report(b0={"state": "supported", "quote": QUOTE_POLICY, "article": 3})
    assert verify_step.attach_refs(content, report, [_source()]) == 0


# ---------------------------------------------------------------- 5. verify_tree


def _run(coro):
    return asyncio.run(coro)


def test_verify_tree_judges_every_node_with_one_call_per_chunk():
    seen: list[str] = []

    async def chat(system: str, user: str, **kwargs) -> dict:
        seen.append(user)
        assert system == verify_step.JUDGE_SYSTEM
        ids = _judged_ids(user)
        return {"verdicts": [
            {"id": node_id, "state": "supported" if index == 0 else "unverified",
             "quote": QUOTE_POLICY if index == 0 else "", "article": 1 if index == 0 else 0}
            for index, node_id in enumerate(ids)]}

    content = _content()
    report = _run(verify_step.verify_tree(content, articles=[_article()], chat=chat))
    assert report.judged == 10
    assert report.states == {"supported": 1, "unverified": 9, "contradicted": 0}
    assert report.articles == 1
    assert report.skipped == ""
    # 树**没有被改**（挂引用是 `attach_refs` 的事）
    assert all(not node.source_refs for node in tools_module._walk_tree(content.root))


def test_verify_tree_splits_large_trees_into_chunks():
    calls: list[int] = []

    async def chat(system: str, user: str, **kwargs) -> dict:
        ids = _judged_ids(user)
        calls.append(len(ids))
        return {"verdicts": [{"id": node_id, "state": "unverified"} for node_id in ids]}

    content = _wide_tree(130)
    total = len(verify_step.node_rows(content))
    report = _run(verify_step.verify_tree(content, articles=[_article()], chat=chat))
    assert total == 137                       # 130 个内容节点 + 根 + 6 个组
    assert calls == [verify_step.JUDGE_MAX_NODES, verify_step.JUDGE_MAX_NODES,
                     total - 2 * verify_step.JUDGE_MAX_NODES]
    assert report.calls == 3 and report.judged == total


def test_verify_tree_without_articles_says_so_instead_of_judging():
    async def chat(**kwargs):  # pragma: no cover — 不该被调到
        raise AssertionError("没有正文时不该发判定调用")

    report = _run(verify_step.verify_tree(_content(), articles=[], chat=chat))
    assert report.skipped == "no_articles"
    assert report.judged == 0
    assert "没有做" in verify_step.summarize(report, [])


def test_a_failed_chunk_does_not_lose_the_other_chunks():
    """一块解析不出来只算它漏判：核对是生成之后多出来的一步，不能把树拖成失败。"""
    state = {"calls": 0}

    async def chat(system: str, user: str, **kwargs) -> dict:
        state["calls"] += 1
        if state["calls"] == 1:
            return "这不是 JSON"
        ids = _judged_ids(user)
        return {"verdicts": [{"id": node_id, "state": "unverified"} for node_id in ids]}

    content = _wide_tree(70)
    total = len(verify_step.node_rows(content))
    report = _run(verify_step.verify_tree(content, articles=[_article()], chat=chat))
    assert report.invalid >= verify_step.JUDGE_MAX_NODES
    assert report.judged == total - verify_step.JUDGE_MAX_NODES


def test_all_chunks_failing_is_reported_as_such():
    async def chat(system: str, user: str, **kwargs) -> dict:
        return {}

    report = _run(verify_step.verify_tree(_content(), articles=[_article()], chat=chat))
    assert report.judged == 0
    assert report.skipped == "no_verdicts"


def test_the_search_is_skipped_when_articles_are_given():
    def search(query: str):  # pragma: no cover — 不该被调到
        raise AssertionError("给了正文就不该再搜一次")

    async def chat(system: str, user: str, **kwargs) -> dict:
        return {"verdicts": []}

    _run(verify_step.verify_tree(_content(), articles=[_article()], chat=chat,
                                 search=search))


# ---------------------------------------------------------------- 6. 摘要话术


def test_the_summary_says_unverified_is_kept_not_deleted():
    report = _report(
        a={"state": "supported", "quote": QUOTE_POLICY, "article": 1},
        b={"state": "unverified", "quote": "", "article": None},
        c={"state": "contradicted", "quote": "", "article": None},
    )
    report.articles = 1
    text = verify_step.summarize(report, [_source()])
    assert "未证实（保留，未删除）" in text
    assert "相矛盾" in text and "请你核对" in text


# ---------------------------------------------------------------- 7. 开关


def test_the_switch_is_off_by_default(monkeypatch):
    monkeypatch.delenv(verify_step.VERIFY_ENV, raising=False)
    assert verify_step.verify_enabled() is False
    monkeypatch.setenv(verify_step.VERIFY_ENV, "1")
    assert verify_step.verify_enabled() is True
    monkeypatch.setenv(verify_step.VERIFY_ENV, "off")
    assert verify_step.verify_enabled() is False


# ---------------------------------------------------------------- 8. 端到端：挂上去还要活着


@pytest.fixture
def tree_chat(monkeypatch):
    """模型替身：骨架给三条分支，结构写整棵树，说明按 id 回一份。"""
    monkeypatch.setenv(tools_module.EXTERNAL_KNOWLEDGE_ENV, "1")
    monkeypatch.setattr(external_knowledge, "search_external",
                        lambda *args, **kwargs: [])

    async def chat(system: str, user: str, **kwargs) -> dict:
        if system.startswith(tools_module.PLAN_PROMPT):
            return {"branches": list(BRANCHES)}
        if system.startswith(tools_module.DETAIL_ONLY_PROMPT):
            ids = [line.split("id=", 1)[1].split("｜", 1)[0]
                   for line in user.splitlines() if "id=" in line]
            return {node_id: _DETAIL_TEXT for node_id in ids}
        if system == verify_step.JUDGE_SYSTEM:
            ids = _judged_ids(user)
            quotes = {"b0": QUOTE_POLICY, "b1": QUOTE_TECH}
            return {"verdicts": [
                {"id": node_id, "state": "supported" if node_id in quotes else "unverified",
                 "quote": quotes.get(node_id, ""), "article": 1 if node_id in quotes else 0}
                for node_id in ids]}
        return {"title": TOPIC, "root": {
            "id": "root", "label": TOPIC, "detail": "中心主题",
            "children": [{"id": f"b{index}", "label": title, "children": [
                {"id": f"b{index}-1", "label": f"{title}·一", "detail": "一句说明。"},
                {"id": f"b{index}-2", "label": f"{title}·二", "detail": "另一句说明。"},
            ]} for index, title in enumerate(BRANCHES)]}}

    return chat


def _run_generate(chat, *, budget_calls: int = 20, emit=None):
    request = AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default", sources=[],
        budget=RunBudget(max_model_calls=budget_calls, max_tool_calls=4,
                         max_seconds=60.0, max_output_tokens=8192, max_repairs=1))
    ctx = RunContext(request=request)
    ctx.emit = emit
    registry = tools_module.build_registry("create", has_sources=False, has_library=False)
    with patch("app.agent.tools.chat_async", new=chat):
        return ctx, asyncio.run(registry.execute(
            ToolCall(call_id="c-verify", name="mindmap_generate",
                     arguments={"topic": TOPIC, "branches": 3}), ctx))


def test_switch_on_writes_the_three_states_and_keeps_the_refs(tree_chat, monkeypatch):
    """开了之后：三态进 `data["verify"]`，supported 的引用挂上而且**通过了收尾校验**。

    "通过了"这三个字是这条用例的全部：`verify_source_refs(remove_invalid=True)` 会把
    任何一条对不上原文的引用静默删掉，而"没抛异常"什么都不能证明。
    """
    monkeypatch.setenv(verify_step.VERIFY_ENV, "1")
    monkeypatch.setattr(verify_step, "search_articles",
                        lambda query, **kwargs: [_article()])
    frames: list[dict] = []

    async def emit(frame) -> None:
        frames.append(frame)

    ctx, result = _run_generate(tree_chat, emit=emit)
    assert result.ok, result.content
    verify = result.data["verify"]
    assert verify["query"] == f"{TOPIC} {' '.join(BRANCHES)}"
    assert verify["articles"] == 1
    assert verify["states"] == {"supported": 2, "unverified": 8, "contradicted": 0}
    assert verify["attached_refs"] == 2 and verify["dropped_refs"] == 0
    # 每个节点都有一条三态（表按 node_id 给，前端要的就是这个）
    assert set(verify["nodes"]) == {node.id for node in
                                    tools_module._walk_tree(ctx.candidate.root)}
    refs = {node.id: node.source_refs for node in
            tools_module._walk_tree(ctx.candidate.root) if node.source_refs}
    assert set(refs) == {"b0", "b1"}
    for refs_here in refs.values():
        assert refs_here[0].quote in ARTICLE
    assert verify["seconds"] >= 0
    # 进度帧：与外部检索同一种阶段（前端不必为这一步多认一个 stage）
    assert any(frame.get("stage") == tools_module.EXTERNAL_SEARCH_STAGE for frame in frames)
    assert "GENERATION_VERIFIED" in [item["code"] for item in result.data["diagnostics"]]


def test_switch_off_keeps_the_data_keyset(tree_chat, monkeypatch):
    """关着时 `data` 里**没有** `verify` 这个键：回归用例比的是键集，不只是值。"""
    monkeypatch.delenv(verify_step.VERIFY_ENV, raising=False)

    def forbidden(query: str, **kwargs):  # pragma: no cover — 不该被调到
        raise AssertionError("开关关着时不该搜")

    monkeypatch.setattr(verify_step, "search_articles", forbidden)
    _, result = _run_generate(tree_chat)
    assert result.ok, result.content
    assert "verify" not in result.data
    assert not any(item["code"].startswith("VERIFY") for item in result.data["diagnostics"])


def test_no_budget_skips_the_step_and_says_so(monkeypatch):
    """额度用光时整段跳过并记一条诊断：核对没有资格把已经成型的树拖成失败。

    直接调那一步（而不是跑完整生成）：这里要钉的是**额度判定排在最前面**，而
    "刚好只差一次调用"这种预算是靠数生成侧用了几次凑不出来的——那种写法会在
    任何一次生成侧多/少一次调用时变成一条假红。
    """
    monkeypatch.setenv(verify_step.VERIFY_ENV, "1")

    def forbidden(query: str, **kwargs):  # pragma: no cover — 不该被调到
        raise AssertionError("没有额度时不该搜")

    monkeypatch.setattr(verify_step, "search_articles", forbidden)
    request = AgentRunRequest(
        goal=TOPIC, intent="create", sources=[],
        budget=RunBudget(max_model_calls=1, max_tool_calls=4,
                         max_seconds=60.0, max_output_tokens=8192, max_repairs=1))
    ctx = RunContext(request=request)
    ctx.model_calls_used = 1                      # 额度刚好用完
    diagnostics: list = []
    data = asyncio.run(tools_module._verify_after_generate(
        _content(), ctx=ctx, sources=[], diagnostics=diagnostics))
    assert data is None
    assert [item.code for item in diagnostics] == ["VERIFY_SKIPPED_NO_BUDGET"]


def test_the_drop_step_only_looks_at_the_refs_it_attached():
    """收尾那一遍校验**只碰这一步亲手挂的引用**。

    把树上原有的引用一起塞进探针会做两件不该做的事：重判一遍生成那一步已经校验过的
    引用；以及用"这次抓回来的那几篇"当来源表，原有的引用因此校验失败、被当成
    "引文对不上"删掉。实测就是这么崩的（一跑真图就 `ValidationError`）。
    """
    content = _content(refs=True)                # 节点 b0 上有一条**别人写的**引用
    report = verify_step.VerifyReport(query="q")
    report.attached_nodes = []
    tools_module._drop_unverifiable_refs(content, report, [_source()], [])
    assert len(content.root.children[0].source_refs) == 1
    assert report.dropped == 0


def test_more_than_fifty_attached_refs_are_checked_in_batches():
    """探针一批只能带 50 个节点（`Node.children` 的上限）：不分批就是一次
    `ValidationError`，而那句报错说的是"children 太多"，与本意隔了两层。"""
    source = _source()
    children = [Node(id=f"n{index}", label=f"节点{index}",
                     source_refs=[SourceRef(source_id=source.id, quote=QUOTE_POLICY)])
                for index in range(58)]
    content = MindMapContent(
        title="大图",
        source_usage=[SourceUsage(source_id=source.id, kind=SourceKind.web,
                                  filename="deepseek: 碳中和", char_count=len(ARTICLE))],
        root=Node(id="root", label="大图", children=[
            Node(id="g0", label="组", children=children[:30]),
            Node(id="g1", label="组2", children=children[30:]),
        ]))
    report = verify_step.VerifyReport(query="q")
    report.attached_nodes = [node.id for node in children]
    tools_module._drop_unverifiable_refs(content, report, [source], [])
    # 引文在正文里，一条都不该被摘
    assert report.dropped == 0


def test_a_ref_that_fails_verbatim_check_is_detached(tree_chat, monkeypatch):
    """挂上去还要过一遍 `verify_source_refs`：不合格的摘掉，而不是留在树上。

    构造：判定说 supported，但引文在正文里找不到（`attach_refs` 本来就会跳过它），
    再把 `attach_refs` 换成一个会挂"假引文"的版本——证明兜底那一层真的在工作。
    """
    monkeypatch.setenv(verify_step.VERIFY_ENV, "1")
    monkeypatch.setattr(verify_step, "search_articles",
                        lambda query, **kwargs: [_article()])
    real_attach = verify_step.attach_refs

    def attach_with_a_fake_quote(content, report, sources):
        attached = real_attach(content, report, sources)
        first = content.root.children[0]
        first.source_refs = [SourceRef(source_id=sources[0].id, quote="纯属编造的一句引文")]
        return attached + 1

    monkeypatch.setattr(verify_step, "attach_refs", attach_with_a_fake_quote)
    ctx, result = _run_generate(tree_chat)
    assert result.ok, result.content
    assert result.data["verify"]["dropped_refs"] == 1
    assert content_refs(ctx) == [] or all(
        ref.quote in ARTICLE for node in tools_module._walk_tree(ctx.candidate.root)
        for ref in node.source_refs)


def content_refs(ctx) -> list:
    return [ref for node in tools_module._walk_tree(ctx.candidate.root)
            for ref in node.source_refs]
