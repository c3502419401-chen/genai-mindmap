"""分片生成 + 按支检索：骨架出来后，每条一级分支各检索一次再展开。

这一轮改的是**检索覆盖度**，所以本文件盯的不是"树长什么样"，而是四件事：

  1. 每支只看得到自己的 hits（兄弟分支的证据一个字都不进 prompt）；
  2. N 支的检索是**并行**的（串行实现也能把次数跑对，只有时间窗口能证伪）；
  3. 检索**不占**模型的 `max_tool_calls`（模型看到的仍然是一次工具调用）；
  4. 降级彻底：某支抛错只影响那一支；全都拿不到证据时退回整树一次检索，
     不交出空树；没有 embedding 配置时纯 BM25 照常工作。

**为什么用两份材料而不是一份材料的两个段落**：父块返回模式下一条命中是
450–700 字的父块，同一份材料里相隔两三句的两个话题必然落在同一个父块里——
"第 1 支的 prompt 里不该有第 2 段的文字"在一份材料上是**结构上做不到**的，
写成用例只会测到父块切在哪。拆成两份材料之后这条性质是构造保证的，断言才有
意义。（这条限制本身值得记下来：同一份材料里兄弟块的内容会随父块一起串门。）

形态前提：分片检索只在 `rag` 下发生（`full` 下正文已在 prompt 里，检索是白花），
所以本文件默认跑在 `rag`，另有两支用例钉住 `full` 下"不做检索、注册表仍撤掉 deep"。

`tests/conftest.py` 默认把 `MINDMAP_PARENT_RETRIEVAL` 关掉（那是为了让检索类用例
稳定走单级 BM25）。需要两级命中的用例自己打开它——这也正好覆盖了两种开关状态：
关着时 `search_parent` 内部退回 `search_sources`，分片照常工作。
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import patch

import pytest

from app.agent.contracts import (
    AgentRunRequest,
    Completion,
    RunBudget,
    RunContext,
    ToolCall,
)
from app.agent.service import run
from app.agent.tools import (
    BRANCH_EVIDENCE_RULES,
    BRANCH_EXPAND_PROMPT,
    BRANCH_NO_EVIDENCE_RULES,
    DEEP_BRANCH_TOP_K,
    DEEP_FALLBACK_TOP_K,
    PLAN_PROMPT,
    build_registry,
)
from app.contracts.source import Source, SourceKind
from app.generation.service import SOURCE_MODE_ENV
from app.knowledge.service import EvidenceHit

# 两份互不重叠的材料：一份讲预算审批，一份讲绩效考核。分支标题照这两块起，
# 于是"第 1 支只看到第 1 份材料"可以被逐字证伪——只要第 2 份的独有标记出现在
# 第 1 支的 prompt 里，这条用例就红。
BUDGET_TEXT = (
    "第三章 预算审批。预算由财务经理核准后执行，超过五十万元报总经理审批。"
    "每季度末做一次预算执行分析，偏差超过百分之十的需要书面说明原因。"
) * 3
PERFORMANCE_TEXT = (
    "第五章 绩效考核。考核分季度与年度两次，由直属上级打分并交人力资源部复核。"
    "连续两个季度不合格的进入改进期，改进期内不得晋升。"
) * 3
BUDGET_MARKER = "超过五十万元报总经理审批"
PERFORMANCE_MARKER = "连续两个季度不合格的进入改进期"

BRANCHES = ("预算审批", "绩效考核", "预算执行分析")
# 兜底检索的 query：主题 + 全部一级分支标题。用户填的"主题"经常是指令而不是
# 材料里的词（这个例子里"管理办法"只出现在文件名里），只拿主题去查 BM25 一个
# token 都命中不了——兜底就成了"兜了个空"。
WHOLE_QUERY = " ".join(["管理办法", *BRANCHES])
BUDGET_ID = "doc-budget"
PERFORMANCE_ID = "doc-performance"


def _sources() -> list[Source]:
    return [
        Source(id=BUDGET_ID, kind=SourceKind.document, status="parsed",
               filename="预算管理办法.docx", text=BUDGET_TEXT),
        Source(id=PERFORMANCE_ID, kind=SourceKind.document, status="parsed",
               filename="绩效考核办法.docx", text=PERFORMANCE_TEXT),
    ]


def _ctx(*, max_model_calls: int = 20, max_tool_calls: int = 8) -> RunContext:
    sources = _sources()
    request = AgentRunRequest(
        goal="整理管理办法", intent="create", sources=sources,
        budget=RunBudget(max_model_calls=max_model_calls,
                         max_tool_calls=max_tool_calls, max_output_tokens=32768),
    )
    return RunContext(request=request, candidate=None,
                      sources_by_id={source.id: source for source in sources})


def _plan(*titles: str) -> dict:
    return {"branches": list(titles)}


def _children(*labels: str) -> dict:
    return {"children": [{"label": label, "expandable": False} for label in labels]}


def _two(title: str) -> dict:
    """默认的展开响应：每支正好 `points_per_branch=2` 个要点（不触发补调）。"""
    return _children(f"{title}·一", f"{title}·二")


def _hit(offset: int = 0, length: int = 180, source_id: str = BUDGET_ID) -> EvidenceHit:
    """一条合成命中：区间落在材料里，`quote` 是原文切片（逐字校验要能过）。"""
    text = BUDGET_TEXT if source_id == BUDGET_ID else PERFORMANCE_TEXT
    start = min(offset, len(text) - 1)
    end = min(start + length, len(text))
    return EvidenceHit(source_id=source_id, start=start, end=end,
                       quote=text[start:end], score=0.5, score_bm25=1.0)


def _branch_of(user: str) -> str:
    """从展开消息里取出这次展开的是哪一支（与 `_generate_deep` 的消息格式同源）。"""
    return user.split("本条一级分支：", 1)[1].split("\n", 1)[0]


def _evidence_of(user: str) -> list[dict]:
    """取出拼在 user 末尾的 evidence 数组；没有就是空。"""
    if "evidence 数组）：\n" not in user:
        return []
    return json.loads(user.split("evidence 数组）：\n", 1)[1])


async def _plain_chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
    """默认模型替身：骨架三条，每支两个要点。**不看证据**，只保证流程跑完。"""
    if system.startswith(PLAN_PROMPT):
        return _plan(*BRANCHES)
    return _two(_branch_of(user))


def _run_deep(ctx: RunContext, chat, *, registry_with_sources: bool = True, **arguments):
    """跑一次 `mindmap_generate_deep`，`chat` 是模型替身。

    `registry_with_sources=False` 只用于 `full` 形态：那里注册表本来就撤掉了这个
    工具，而这里要验的是"绕过注册表直接调，也不会检索"。
    """
    args = {"topic": "管理办法", "branches": 3, "points_per_branch": 2}
    args.update(arguments)
    registry = build_registry("create", has_sources=registry_with_sources)
    with patch("app.agent.tools.chat_async", new=chat):
        return asyncio.run(registry.execute(
            ToolCall(call_id="c-deep", name="mindmap_generate_deep", arguments=args),
            ctx,
        ))


def _branch(root, label: str):
    return next(branch for branch in root.children if branch.label == label)


def _codes(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


@pytest.fixture(autouse=True)
def _rag_mode(clean_env, monkeypatch):
    """本文件默认 `rag` 形态：分片检索只在这条路上发生。"""
    monkeypatch.setenv(SOURCE_MODE_ENV, "rag")
    yield


# ---------------------------------------------------------------- 1. 每支只看自己的 hits


def test_each_branch_sees_only_its_own_hits(monkeypatch):
    """第 1 支的 prompt 里不能出现第 2 份材料的独有文字。

    这是"按支检索"与"整树检索一次、把 top_k 调大"之间唯一能证伪的差别：
    两种做法都能让 prompt 里出现**一些**证据，只有"兄弟分支的证据不串门"
    才能证明分片检索真的按支发生了。
    """
    monkeypatch.setenv("MINDMAP_PARENT_RETRIEVAL", "1")
    seen: list[tuple[str, str]] = []

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        if system.startswith(PLAN_PROMPT):
            return _plan(*BRANCHES)
        seen.append((system, user))
        return _two(_branch_of(user))

    result = _run_deep(_ctx(), chat)

    assert result.ok, result.content
    assert len(seen) == len(BRANCHES), [user[:20] for _, user in seen]
    budget_prompt = next(user for _, user in seen if "本条一级分支：预算审批" in user)
    performance_prompt = next(user for _, user in seen if "本条一级分支：绩效考核" in user)
    assert BUDGET_MARKER in budget_prompt
    assert PERFORMANCE_MARKER not in budget_prompt
    assert PERFORMANCE_ID not in budget_prompt
    # 反向也要钉：只断言一边时，"两段都塞进每一支"的实现照样能通过上面那条
    assert PERFORMANCE_MARKER in performance_prompt
    assert BUDGET_MARKER not in performance_prompt
    assert BUDGET_ID not in performance_prompt
    # 有证据的分支必须带上引用纪律，否则模型写不出 source_refs；基础格式仍在原提示词里
    system = next(system for system, user in seen if "本条一级分支：预算审批" in user)
    assert BRANCH_EVIDENCE_RULES in system
    assert system.startswith(BRANCH_EXPAND_PROMPT)


def test_node_poached_from_another_branch_is_dropped(monkeypatch):
    """节点想引兄弟分支的证据 → 引用被丢掉，节点留下。

    收口发生在 `_refs_from`（按本支命中判归属），不是收尾的逐字校验：那段文字
    **确实**在原文里，逐字校验会放它过去——"只许引本支证据"就退化成了口号。
    """
    monkeypatch.setenv("MINDMAP_PARENT_RETRIEVAL", "1")
    poached = PERFORMANCE_TEXT[:120]

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        if system.startswith(PLAN_PROMPT):
            return _plan(*BRANCHES)
        title = _branch_of(user)
        if title == "预算审批":
            return {"children": [
                # 这条引用逐字存在于原文，但属于绩效考核那一支的证据
                {"label": "越界引用", "expandable": False,
                 "source_refs": [{"source_id": PERFORMANCE_ID, "quote": poached}]},
                {"label": "正常", "expandable": False},
            ]}
        return _two(title)

    ctx = _ctx()
    result = _run_deep(ctx, chat)

    assert result.ok, result.content
    budget = _branch(ctx.candidate.root, "预算审批")
    assert [node.label for node in budget.children] == ["越界引用", "正常"]
    assert all(not node.source_refs for node in budget.children)
    assert result.data["source_refs"]["kept"] == 0


def test_citable_quote_is_copied_into_source_refs(monkeypatch):
    """照 evidence 抄一条引用 → 它真的挂到树上，并通过逐字校验。

    没有这一条，"每支只看自己的 hits"可以靠"根本不给引用"通过——那样拒得掉错，
    也拒掉了对。这里同时钉住 `evidence_*` 那一组是**可抄的**：坐标 + 引文一起抄
    才既过逐字校验、又落在命中的子块里。
    """
    monkeypatch.setenv("MINDMAP_PARENT_RETRIEVAL", "1")

    def ref_from(user: str) -> dict:
        entry = _evidence_of(user)[0]
        if "evidence_quote" in entry:
            return {"source_id": entry["source_id"],
                    "start": entry["evidence_start"], "end": entry["evidence_end"],
                    "quote": entry["evidence_quote"]}
        return {"source_id": entry["source_id"], "quote": entry["quote"][:200]}

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        if system.startswith(PLAN_PROMPT):
            return _plan(*BRANCHES)
        title = _branch_of(user)
        ref = ref_from(user)
        # 两条要点引用同一条证据：去重只作用于证据集，不该把节点合并掉
        return {"children": [
            {"label": f"{title}·一", "expandable": False, "source_refs": [ref]},
            {"label": f"{title}·二", "expandable": False, "source_refs": [dict(ref)]},
        ]}

    ctx = _ctx()
    result = _run_deep(ctx, chat)

    assert result.ok, result.content
    assert result.data["source_refs"]["kept"] == 2 * len(BRANCHES)
    assert result.data["source_refs"]["dropped"] == 0
    for branch in ctx.candidate.root.children:
        for node in branch.children:
            assert node.source_refs, node.label


def test_quote_whitespace_is_never_normalised(monkeypatch):
    """引文首尾的空白不许被洗掉：`quote == text[start:end]` 是逐字约束。

    叶子块按句首吸附，很多块以换行开头。上一版实现对 quote 做了 `strip()`，实测
    24 条合法引用里 13 条被判 `SOURCE_QUOTE_MISMATCH` 剔除——界面上看起来像"这一支
    没有证据"，而那和检索、和模型都没关系，是清洗把逐字关系弄坏了。
    """
    from app.agent.tools import _branch_evidence, _ref_from
    from app.contracts.mindmap import MindMapContent, Node
    from app.contracts.source import SourceUsage
    from app.optimization.source_refs import verify_source_refs

    text = "\n\n  开头是空白的一段原文，用来验证引文不会在入口被改写。" * 2
    source = Source(id="ws", kind=SourceKind.document, status="parsed", text=text)
    hit = EvidenceHit(source_id="ws", start=0, end=40, quote=text[0:40],
                      score=1.0, leaf_start=0, leaf_end=40)
    evidence = _branch_evidence([hit], [source])
    quote = evidence.payload[0]["evidence_quote"]
    assert quote.startswith("\n")            # 前提：这条命中确实以空白开头

    ref = _ref_from({"source_id": "ws", "start": 0, "end": 40, "quote": quote}, evidence)
    assert ref == {"source_id": "ws", "start": 0, "end": 40, "quote": quote}

    content = MindMapContent(
        title="t", root=Node(id="r", label="t", children=[
            Node(id="n", label="x", source_refs=[ref])]),
        source_usage=[SourceUsage(source_id="ws", kind=source.kind, filename=None,
                                  char_count=len(text.strip()))],
    )
    reports = verify_source_refs(content, [source], remove_invalid=True,
                                 evidence=evidence.spans)
    codes = [item.code for item in reports if item.code not in
             ("UNSOURCED_NODE", "SOURCE_PAGE_UNVERIFIED")]
    assert codes == [], codes
    assert content.root.children[0].source_refs[0].quote == quote


# ---------------------------------------------------------------- 2. 检索并行


def test_branch_query_is_the_title_alone():
    """分支 query **就是标题本身**，主题一个字都不进去。

    这个拼法是三组消融选出来的（数字见 `_search_branch` 的 docstring）：拼上主题
    之后 8 支的 top-1 全撞在文档标题块上（Jaccard 0.32，覆盖率 19.2%），只发标题
    降到 3/8（Jaccard 0.13，覆盖率 28.8%）。数字会随材料变，行为不该悄悄变回去——
    所以这里钉住"主题不进 query"这条行为，而不是钉住那三个百分数。
    """
    seen: list[str] = []

    def spy(sources, query, top_k=DEEP_BRANCH_TOP_K, *, filters=None):
        seen.append(query)
        return [_hit()]

    with patch("app.agent.tools.search_parent", new=spy):
        result = _run_deep(_ctx(), _plain_chat)

    assert result.ok, result.content
    assert seen == list(BRANCHES)            # 只有标题，顺序与骨架一致
    assert all("管理办法" not in query for query in seen)


def test_branch_searches_overlap():
    """N 支的检索窗口必须有交集：串行实现等不到第二个分支进来。

    用真实时间的窗口而不是"调用次数"来判：次数对不等于并行，而串行的代价在
    20 支 × 每次一次 embedding 往返时是 20 倍延迟，那正是这条要守的东西。
    假检索返回一条真命中，是为了避开"全都空 → 整树兜底"那条路（它会多一次调用，
    属于另一条用例的事）。
    """
    windows: list[tuple[float, float]] = []

    def fake_search(sources, query, top_k=DEEP_BRANCH_TOP_K, *, filters=None):
        started = time.perf_counter()
        time.sleep(0.15)          # 占住线程：串行时第二个分支只能排在后面
        windows.append((started, time.perf_counter()))
        return [_hit()]

    with patch("app.agent.tools.search_parent", new=fake_search):
        result = _run_deep(_ctx(), _plain_chat)

    assert result.ok, result.content
    assert len(windows) == len(BRANCHES)
    overlapping = [
        (left, right) for left in windows for right in windows
        if left is not right and right[0] < left[1] < right[1]
    ]
    assert overlapping, f"检索窗口没有交集（串行执行）：{windows}"


# ---------------------------------------------------------------- 3. 不占 max_tool_calls


def test_branch_retrieval_does_not_consume_tool_calls():
    """跑一次真正的 agent 循环：`tool_calls` 计数必须是 1，不是 1+N。

    检索发生在工具内部，是服务端自己的并发检索——模型看到的仍然只是一次
    `mindmap_generate_deep` 调用。这条如果退化（比如把检索做成 N 个独立工具），
    `max_tool_calls` 的默认值 8 会在 20 支的图上直接把整次运行掐断。
    """
    deep_call = ToolCall(call_id="deep-1", name="mindmap_generate_deep",
                         arguments={"topic": "管理办法", "branches": 3,
                                    "points_per_branch": 2})

    class Provider:
        def __init__(self) -> None:
            self.calls = 0

        async def complete_tools(self, messages, tools, *, budget):
            self.calls += 1
            if self.calls == 1:
                return Completion(
                    message={"role": "assistant", "content": "好的", "tool_calls": [{
                        "id": deep_call.call_id, "type": "function",
                        "function": {"name": deep_call.name,
                                     "arguments": json.dumps(deep_call.arguments,
                                                             ensure_ascii=False)},
                    }]},
                    tool_calls=[deep_call], finish_reason="tool_calls",
                    usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                )
            return Completion(message={"role": "assistant", "content": "完成"},
                              tool_calls=[], finish_reason="stop",
                              usage={"prompt_tokens": 1, "completion_tokens": 1,
                                     "total_tokens": 2})

    async def drain():
        return [event async for event in run(_ctx().request, provider=Provider())]

    with patch("app.agent.tools.chat_async", new=_plain_chat):
        events = asyncio.run(drain())

    tool_ends = [event for event in events if event.type == "tool_end"]
    assert len(tool_ends) == 1, [event.type for event in events]
    assert tool_ends[0].name == "mindmap_generate_deep"
    assert tool_ends[0].ok, tool_ends[0].result_summary
    # 检索确实跑过（不是"因为没跑所以没占额度"）
    assert tool_ends[0].result_summary["retrieval"]["mode"] == "branch"
    assert len(tool_ends[0].result_summary["retrieval"]["queries"]) == len(BRANCHES)
    assert events[-1].type == "done"


# ---------------------------------------------------------------- 4. 某支检索失败


def test_one_failed_branch_keeps_the_others_and_the_tree(monkeypatch):
    """某支检索抛错：那一支无证据展开、`source_refs` 为空，其余支正常，整体 ok。

    这是"降级要彻底"的第一层。合并成"检索一失败就整次失败"是最容易被写出来的
    实现，而它的代价是：20 支的图因为第 7 支的一次超时全部报废。
    """
    monkeypatch.setenv("MINDMAP_PARENT_RETRIEVAL", "1")
    from app.knowledge.service import search_parent as real_search

    prompts: dict[str, str] = {}

    def flaky_search(sources, query, top_k=DEEP_BRANCH_TOP_K, *, filters=None):
        if "绩效考核" in query:
            raise RuntimeError("模拟向量批失败")
        return real_search(sources, query, top_k, filters=filters)

    def ref_from(user: str) -> dict:
        entry = _evidence_of(user)[0]
        if "evidence_quote" in entry:
            return {"source_id": entry["source_id"],
                    "start": entry["evidence_start"], "end": entry["evidence_end"],
                    "quote": entry["evidence_quote"]}
        return {"source_id": entry["source_id"], "quote": entry["quote"][:200]}

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        if system.startswith(PLAN_PROMPT):
            return _plan(*BRANCHES)
        title = _branch_of(user)
        prompts[title] = system
        evidence = _evidence_of(user)
        if not evidence:
            return _children(f"{title}·无依据一", f"{title}·无依据二")
        ref = ref_from(user)
        return {"children": [
            {"label": f"{title}·有依据一", "expandable": False, "source_refs": [ref]},
            {"label": f"{title}·有依据二", "expandable": False, "source_refs": [ref]},
        ]}

    ctx = _ctx()
    with patch("app.agent.tools.search_parent", new=flaky_search):
        result = _run_deep(ctx, chat)

    assert result.ok, result.content
    assert result.data["retrieval"]["failed_branches"] == ["绩效考核"]
    assert "BRANCH_RETRIEVAL_FAILED" in _codes(result)
    # 失败那一支走"无依据"提示词，节点在、引用为空；其他支照常带回校验过的引用
    assert BRANCH_NO_EVIDENCE_RULES in prompts["绩效考核"]
    assert BRANCH_EVIDENCE_RULES in prompts["预算审批"]
    performance = _branch(ctx.candidate.root, "绩效考核")
    assert len(performance.children) == 2, "失败的一支仍要展开（不编内容，但保留结构）"
    assert all(not node.source_refs for node in performance.children)
    budget = _branch(ctx.candidate.root, "预算审批")
    assert all(node.source_refs for node in budget.children)
    assert result.data["source_refs"]["kept"] >= 2


# ---------------------------------------------------------------- 5. 检索全失败


def test_all_branch_searches_failing_falls_back_to_whole_tree(monkeypatch):
    """所有支都检索失败 → 退回整树一次检索，不产出空树。

    "检索失败"不等于"材料里没有"。不兜底的话，每支都会带着"没有证据就留空"的
    提示词展开，用户拿到的是一棵只剩骨架的树——而那是**服务端**造成的，不是
    材料造成的。
    """
    monkeypatch.setenv("MINDMAP_PARENT_RETRIEVAL", "1")
    from app.knowledge.service import search_parent as real_search

    queries: list[tuple[str, int]] = []

    def failing_search(sources, query, top_k=DEEP_BRANCH_TOP_K, *, filters=None):
        queries.append((query, top_k))
        if query == WHOLE_QUERY:       # 兜底：整树一次检索用"主题 + 全部标题"
            return real_search(sources, query, top_k, filters=filters)
        raise RuntimeError("模拟检索服务不可用")

    ctx = _ctx()
    with patch("app.agent.tools.search_parent", new=failing_search):
        result = _run_deep(ctx, _plain_chat)

    assert result.ok, result.content
    assert result.data["retrieval"]["mode"] == "whole"
    # 3 支各失败一次（top_k=3，query 就是分支标题）+ 1 次兜底（top_k=5）。
    #
    # **不比顺序**：`_retrieve_branches` 是"第一支串行 + 其余 `asyncio.gather`"，
    # 而 `_search_branch` 把同步的 `search_parent` 扔进 `to_thread`——第 2、3 支谁
    # 先进线程池由调度决定。原来这里写的是逐位相等，实测同一天两次全量跑一次绿
    # 一次红，两次的 `queries` 只差第 2、3 项换了位置。首位与末位仍然确定（第一支
    # 串行跑完才 gather，兜底在"全失败"之后），所以钉住这两端、其余比多重集。
    assert queries[0] == (BRANCHES[0], DEEP_BRANCH_TOP_K)
    assert queries[-1] == (WHOLE_QUERY, DEEP_FALLBACK_TOP_K)
    assert sorted(queries[:-1]) == sorted(
        (title, DEEP_BRANCH_TOP_K) for title in BRANCHES)
    assert "BRANCH_RETRIEVAL_FALLBACK" in _codes(result)
    # 不产出空树：每支都有内容，而且用的是兜底那批证据
    for branch in ctx.candidate.root.children:
        assert branch.children, branch.label
    assert result.data["retrieval"]["branches_with_hits"] == len(BRANCHES)


def test_whole_tree_fallback_also_failing_still_builds_a_tree():
    """兜底也失败 → 各支按"无依据"展开，仍然交出树（不是 error，也不是空树）。"""

    def dead_search(*args, **kwargs):
        raise RuntimeError("检索服务不可用")

    ctx = _ctx()
    with patch("app.agent.tools.search_parent", new=dead_search):
        result = _run_deep(ctx, _plain_chat)

    assert result.ok, result.content
    assert result.data["retrieval"]["mode"] == "whole"
    assert result.data["retrieval"]["branches_with_hits"] == 0
    assert all(branch.children for branch in ctx.candidate.root.children)


# ---------------------------------------------------------------- 6. 无 embedding 配置


def test_without_embedding_config_each_branch_gets_bm25_results(monkeypatch):
    """没有 embedding 配置时分片照常，每支只有 BM25 命中，一次向量请求都不发。

    本机与 CI 都是这个状态（`tests/conftest.py` 清空所有凭证），所以这条不是
    "边缘情况"，是**默认路径**：`_vector_scores` 在未配置时直接返回 `unconfigured`，
    检索降级为纯 BM25，父块折叠照常。
    """
    monkeypatch.setenv("MINDMAP_PARENT_RETRIEVAL", "1")
    calls: list[int] = []
    per_branch: dict[str, int] = {}

    def spy_embed(texts):
        calls.append(len(texts))
        raise AssertionError("未配置凭证时不该发向量请求")

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        if system.startswith(PLAN_PROMPT):
            return _plan(*BRANCHES)
        title = _branch_of(user)
        per_branch[title] = len(_evidence_of(user))
        return _two(title)

    with patch("app.knowledge.service.embed_texts", new=spy_embed):
        result = _run_deep(_ctx(), chat)

    assert result.ok, result.content
    assert calls == []
    assert result.data["retrieval"]["mode"] == "branch"
    # 预算审批这一支的 BM25 名次靠前：命中被送进了它的 prompt，且不超过 top_k
    assert per_branch["预算审批"] >= 1
    assert all(count <= DEEP_BRANCH_TOP_K for count in per_branch.values())


# ---------------------------------------------------------------- 7. hits 累积进 evidence


def test_branch_hits_accumulate_into_run_context_evidence(monkeypatch):
    """N 支的 hits 都进了 `RunContext.evidence`，四元组去重生效。

    证据集是收尾校验的唯一输入，也是"引用落在命中的子块里"的唯一依据。它少一条，
    对应节点的引用就会被判越界并剔除；它多一条重复项，200 条上限会被提前挤满。
    """
    monkeypatch.setenv("MINDMAP_PARENT_RETRIEVAL", "1")
    ctx = _ctx()
    with patch("app.agent.tools.search_parent",
               new=lambda sources, query, top_k=DEEP_BRANCH_TOP_K, *, filters=None:
               [_hit()]):          # 三支拿到**完全相同**的一批命中：去重才有意义
        result = _run_deep(ctx, _plain_chat)

    assert result.ok, result.content
    assert ctx.evidence, "分支命中没有进 RunContext.evidence"
    keys = [(span.source_id, span.start, span.end, span.leaf_start, span.leaf_end)
            for span in ctx.evidence]
    assert len(keys) == len(set(keys)), "证据集里出现了重复区间（去重没生效）"
    # 三支各一条相同命中 → 去重后只留一条
    assert result.data["retrieval"]["hits"] == len(BRANCHES)
    assert len(ctx.evidence) == 1


def test_evidence_spans_from_every_branch_are_kept(monkeypatch):
    """每支的命中都要进证据集：只把第一支记进去，后面两支的引用会被判越界。"""
    monkeypatch.setenv("MINDMAP_PARENT_RETRIEVAL", "1")
    ctx = _ctx()
    hits = {"预算审批": _hit(0), "绩效考核": _hit(0, source_id=PERFORMANCE_ID),
            "预算执行分析": _hit(60)}

    def fake_search(sources, query, top_k=DEEP_BRANCH_TOP_K, *, filters=None):
        return [hits[title] for title in hits if title in query] or []

    with patch("app.agent.tools.search_parent", new=fake_search):
        result = _run_deep(ctx, _plain_chat)

    assert result.ok, result.content
    spans = {(span.source_id, span.start, span.end) for span in ctx.evidence}
    assert len(spans) == len(BRANCHES), spans
    assert result.data["retrieval"]["branches_with_hits"] == len(BRANCHES)


# ---------------------------------------------------------------- 8/9. 形态与注册表


def test_full_mode_never_retrieves_per_branch(monkeypatch):
    """`full` 形态：一次分支检索都不发，提示词里也没有引用纪律。

    正文整份在 prompt 里，分片检索是白花——而且 `full` 下模型引用没被这次检索
    命中的段落是合法的，给它加"只许引命中的那一段"反而是错的。
    """
    monkeypatch.setenv(SOURCE_MODE_ENV, "full")
    searches: list[str] = []

    def spy_search(sources, query, top_k=5, *, filters=None):
        searches.append(query)
        return []

    result = _run_deep(_ctx(), _plain_chat, registry_with_sources=False)

    assert result.ok, result.content
    assert searches == []
    assert result.data["retrieval"]["mode"] == "off"
    assert "按支检索" not in result.content


def test_full_mode_still_discards_deep(monkeypatch):
    """`full` + 有来源：注册表里仍然没有 `mindmap_generate_deep`（行为不变）。

    这是回归钉子：放开 discard 只该在 `rag` 下发生。`full` 下它按主题自由展开
    的树与材料脱钩却会 ok=True 返回，那条路不能重新长出来。
    """
    monkeypatch.setenv(SOURCE_MODE_ENV, "full")
    names = build_registry("create", has_sources=True).names()
    assert "mindmap_generate_deep" not in names
    assert "mindmap_generate" in names
    assert "rag_query" not in names

    # 没有来源时两个生成工具都在场（与形态无关）
    assert "mindmap_generate_deep" in build_registry("create").names()


def test_rag_mode_registry_offers_deep_alongside_generate(monkeypatch):
    """`rag` + 有来源：两个生成工具都在场，外加 `rag_query`。"""
    monkeypatch.setenv(SOURCE_MODE_ENV, "rag")
    names = build_registry("create", has_sources=True).names()
    assert names == ["mindmap_generate", "mindmap_generate_deep", "rag_query"]
