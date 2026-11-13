"""分支查询扩展：一次调用扩全部支、降级逐字节一致、标题永远第一位。

这一轮做的事只有一件：**把分片检索的每一支从"一条标题"变成"标题 + 至多 2 条
同义问法"**，多路并行后交给 `search_multi` 做 RRF 融合。所以本文件盯四件事：

  1. **一次调用处理全部分支**（8 支各调一次 = 8 次往返，5 秒预算下不可能）；
  2. **降级彻底**：开关关 / 没配置 / 超时 / 非法 JSON / 没材料 → `{}`，而调用方
     拿到 `{}` 之后的检索串与这个功能上线之前**逐字节相同**；
  3. **标题永远第一位**：`search_multi` 的保底席位只来自第一位那一路；
  4. **成本闸**：单支不扩展，多支才值得那一次往返。

还钉住两条"不能靠提示词保证"的事：模型改写过的键（不在标题清单里）整条丢弃，
以及等于标题的问法整条丢弃——后者会让实际路数从 3 掉到 2，调用方却以为发了 3 路。

融合那一条（两个问法都命中的块分数更高）用两份来源构造：不同来源的块天然分开，
比"指望切块把两个主题切到两个块里"稳得多。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from unittest.mock import patch

import pytest

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    BRANCH_QUERY_LIMIT,
    DEEP_BRANCH_TOP_K,
    PLAN_PROMPT,
    _branch_queries,
    _expand_branches,
    _retrieve_branches,
    _search_branch,
    build_registry,
)
from app.contracts.source import Source, SourceKind
from app.knowledge import query_plan
from app.knowledge.query_plan import BRANCH_SYSTEM_PROMPT, plan_branches

TITLES = ("预算审批", "绩效考核", "预算执行分析")

BUDGET = (
    "预算审批由财务经理核准后执行，超过五十万元的支出报总经理审批。"
    "每季度末做一次预算执行分析，偏差超过百分之十的需要书面说明原因并交财务部备案。"
) * 2
PERFORMANCE = (
    "绩效考核分季度与年度两次，由直属上级打分并交人力资源部复核。"
    "连续两个季度不合格的进入改进期，改进期内不得晋升也不参与年度评优。"
) * 2


def run(coro):
    return asyncio.run(coro)


def _sources() -> list[Source]:
    return [
        Source(id="doc-budget", kind=SourceKind.document, status="parsed", text=BUDGET),
        Source(id="doc-performance", kind=SourceKind.document, status="parsed",
               text=PERFORMANCE),
    ]


def _expand(**mapping: list[str]) -> dict[str, list[str]]:
    """`_expand(id=...)` 这种写法在中文键上不好用，统一走这个包装。"""
    return dict(mapping)


# ---------------------------------------------------------------- 1. 一次调用，全部支


@pytest.fixture
def expansion_on(monkeypatch):
    """开关打开。模型出口由各用例自己替换。"""
    monkeypatch.setenv("MINDMAP_QUERY_EXPANSION", "1")
    yield


def _fake_chat(monkeypatch, result):
    """替换 `query_plan.chat_async`，返回调用记录。

    `result` 可以是返回值、异常或协程函数。只记入参、不参与解析——本文件要断言的
    正是"解不出来时怎么办"，让 `chat_async` 的真身去解析反而把被测行为绕开了。
    """
    calls: list[dict] = []

    async def fake(system: str, user: str, *, max_tokens=None, on_delta=None):
        calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return await result()
        return result

    monkeypatch.setattr(query_plan, "chat_async", fake)
    return calls


def test_one_call_expands_every_branch(expansion_on, monkeypatch):
    """8 支标题 → **一次** `chat_async`，返回 8 个键、每键 2-3 条问法。

    调用次数是这条用例的核心：按支各调一次在 2C2G + 5 秒预算下不可能，而"看起来
    也对"的实现（循环 `plan_query`）恰好会在这里变成 8 次。
    """
    titles = [f"分支{i}" for i in range(1, 9)]
    payload = {title: [f"{title}怎么算", f"{title}是什么"] for title in titles}
    calls = _fake_chat(monkeypatch, payload)

    with patch.object(query_plan, "_material_snippets", return_value=["材料开头"]):
        expanded = run(plan_branches(titles, materials=["材料标题\n材料开头"]))

    assert len(calls) == 1
    assert set(expanded) == set(titles)
    assert all(2 <= len(item) <= 3 for item in expanded.values())
    # 提示词里要有全部标题（编号清单），否则模型无从逐条给问法
    for index, title in enumerate(titles, start=1):
        assert f"{index}. {title}" in calls[0]["user"]


def test_titles_equal_to_the_branch_are_dropped(expansion_on, monkeypatch):
    """问法等于标题的那一条整条丢掉，改写过的键整条丢掉。

    两种都是"白占一路"：前者的去重会让实际路数从 3 掉到 2（调用方却以为发了 3 路），
    后者对应的标题对不上任何一支——拿它去检索等于凭空加了一个新分支，而提示词要求
    的"不得增加方面"在代码里就落在这两处。
    """
    _fake_chat(monkeypatch, {
        "预算审批": ["预算审批", "预算审批要谁批"],
        "预算管理": ["预算怎么管"],          # 模型自己改写过的键：丢弃
        "绩效考核": [],                     # 空数组：整条不出现
    })

    expanded = run(plan_branches(["预算审批", "绩效考核"],
                                 materials=["材料标题\n材料开头"]))

    assert expanded == {"预算审批": ["预算审批要谁批"]}


def test_prompt_forbids_intent_inflation():
    """提示词里必须有那条"不得增加方面"的硬约束，且带 ❌/✅ 对照（回归）。

    这是 2.3 那次实测的直接产物：把「它多少钱」扩成「收费模式」「优惠政策」会让
    向量相似度被几个方面摊薄。措辞换成"尽量不要扩散"就挡不住了——所以钉的是
    那几句原话，不是"提示词非空"。
    """
    assert "不得增加原分支没有的方面" in BRANCH_SYSTEM_PROMPT
    assert "❌" in BRANCH_SYSTEM_PROMPT and "✅" in BRANCH_SYSTEM_PROMPT
    # 同一分支的不同问法要正反例都在，光有禁令没有对照等于没写
    assert "同一件事" in BRANCH_SYSTEM_PROMPT
    # 逐字照抄键：改写键会让那条分支直接失去扩展
    assert "逐字照抄" in BRANCH_SYSTEM_PROMPT


def test_log_carries_counts_only(expansion_on, monkeypatch, caplog):
    """日志只记条数与耗时：标题与材料正文一个都不许出现。

    材料可能是合同、病历、内部制度，而分支标题本身就是材料内容的摘要——日志会被
    收走、会被翻，这一条不比"不记 query 原文"轻。
    """
    _fake_chat(monkeypatch, {"机密项目预算审批": ["它要多少钱"]})
    caplog.set_level(logging.INFO, logger=query_plan.__name__)

    expanded = run(plan_branches(
        ["机密项目预算审批"], materials=["机密材料标题\n正文第一句是公司机密"]))

    assert expanded == {"机密项目预算审批": ["它要多少钱"]}
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "分支扩展" in logged and "outcome=ok" in logged
    assert "机密" not in logged
    assert "正文第一句" not in logged


# ---------------------------------------------------------------- 2. 降级：与没有这个功能一致


def test_off_returns_empty_without_any_request(monkeypatch):
    """开关关着 → `{}`，且一次模型调用都没有（连构造都不该发生）。"""
    monkeypatch.setenv("MINDMAP_QUERY_EXPANSION", "0")

    def explode(*args, **kwargs):
        raise AssertionError("开关关着时不该走到模型出口")

    monkeypatch.setattr(query_plan, "chat_async", explode)
    assert run(plan_branches(["预算审批"], materials=["材料"])) == {}


def test_missing_llm_config_returns_empty(expansion_on):
    """开着开关但没 `MINDMAP_LLM_*`：`chat_async` 的预检直接失败 → `{}`。

    这条**不替换** `chat_async`：要测的就是它读配置失败后本模块怎么反应。`{}` 是
    常态而不是异常——开开关但没配模型不该让每个分支刷一行警告。
    """
    assert run(plan_branches(["预算审批"], materials=["材料"])) == {}


def test_timeout_returns_empty_without_retry(expansion_on, monkeypatch):
    """超时按降级处理且**不重试**：调用次数是 1，而且真的是被掐断的。"""
    async def slow():
        await asyncio.sleep(3)

    calls = _fake_chat(monkeypatch, slow)
    started = time.perf_counter()
    with patch.object(query_plan, "BRANCH_TIMEOUT_SECONDS", 0.05):
        expanded = run(plan_branches(["预算审批"], materials=["材料"]))
    assert expanded == {}
    assert len(calls) == 1
    assert time.perf_counter() - started < 1.5


def test_invalid_json_returns_empty(expansion_on, monkeypatch):
    calls = _fake_chat(monkeypatch, "我建议先看材料里有没有相关章节。")
    assert run(plan_branches(["预算审批"], materials=["材料"])) == {}
    assert len(calls) == 1


def test_no_materials_returns_empty_without_request(expansion_on, monkeypatch):
    """没有材料就一次请求都不发：不看材料的改正是 2.3 那次覆盖率 0.0 的起点。"""
    def explode(*args, **kwargs):
        raise AssertionError("没有材料时不该扩展")

    monkeypatch.setattr(query_plan, "chat_async", explode)
    assert run(plan_branches(["预算审批"], materials=None)) == {}
    assert run(plan_branches(["预算审批"], materials=["", "  \n "])) == {}


def test_retrieve_branches_degraded_is_byte_identical(monkeypatch):
    """扩展降级时每支**只发标题**、走 `search_parent`，`search_multi` 零调用。

    "逐字节一致"在这里的具体含义：查询串就是 `_branch_query(title)` 的返回值
    （标题本身），顺序与骨架一致，路径就是单路父块检索——不是"看起来差不多"。
    """
    seen: list[tuple[str, int]] = []

    def spy_parent(sources, query, top_k=DEEP_BRANCH_TOP_K, *, filters=None):
        seen.append((query, top_k))
        return []

    def spy_multi(*args, **kwargs):
        raise AssertionError("降级时不该走多路检索")

    monkeypatch.setattr("app.agent.tools.search_parent", spy_parent)
    monkeypatch.setattr("app.agent.tools.search_multi", spy_multi)

    hits = run(_retrieve_branches(_sources(), list(TITLES), expansions={}))

    assert seen == [(title, DEEP_BRANCH_TOP_K) for title in TITLES]
    assert hits == [[], [], []]


# ---------------------------------------------------------------- 3. 多路：标题第一位


def test_branch_queries_keeps_the_title_first():
    """`_branch_queries`：标题第一位、与标题相同的问法丢掉、截断到 3 条。"""
    assert _branch_queries("预算审批") == ["预算审批"]
    assert _branch_queries("预算审批", ["预算审批要谁批", "谁批预算"]) == [
        "预算审批", "预算审批要谁批", "谁批预算"]
    # 与标题逐字相同的那条去掉，不是排在后面
    assert _branch_queries("预算审批", ["预算审批", "谁批预算"]) == [
        "预算审批", "谁批预算"]
    # 第 3 条之后的问法不进检索（`plan_branches` 允许模型给 3 条，调用方只用 2 条）
    assert len(_branch_queries("预算审批", ["a1", "a2", "a3", "a4"])) == BRANCH_QUERY_LIMIT


def test_each_branch_sends_one_plus_intents(monkeypatch):
    """有扩展的支发 `1 + len(intents)` 条 query，标题在第一位。"""
    sent: list[list[str]] = []

    async def spy_multi(sources, queries, top_k=DEEP_BRANCH_TOP_K, *, filters=None,
                        hyde=None):
        sent.append(list(queries))
        return []

    def spy_parent(sources, query, top_k=DEEP_BRANCH_TOP_K, *, filters=None):
        sent.append([query])
        return []

    monkeypatch.setattr("app.agent.tools.search_multi", spy_multi)
    monkeypatch.setattr("app.agent.tools.search_parent", spy_parent)

    expansions = {
        "预算审批": ["预算审批要谁批", "谁批预算"],
        "绩效考核": ["绩效怎么打分"],              # 1 条 → 2 路
        # 第三支没有键 → 缺省空，退化成单路
    }
    run(_retrieve_branches(_sources(), list(TITLES), expansions=expansions))

    assert sent == [
        ["预算审批", "预算审批要谁批", "谁批预算"],
        ["绩效考核", "绩效怎么打分"],
        ["预算执行分析"],
    ]


def test_partial_expansion_only_degrades_that_branch(monkeypatch):
    """某支扩展为空、其他支有 → 只有那支走单路，其余照常多路。

    这是最常见的真实形状：模型对两三支给了问法、其余给空数组。整批降级（"有一支
    空就全不用"）会把已经拿到的扩展一起扔掉。
    """
    parent: list[str] = []
    multi: list[list[str]] = []

    def spy_parent(sources, query, top_k=DEEP_BRANCH_TOP_K, *, filters=None):
        parent.append(query)
        return []

    async def spy_multi(sources, queries, top_k=DEEP_BRANCH_TOP_K, *, filters=None,
                        hyde=None):
        multi.append(list(queries))
        return []

    monkeypatch.setattr("app.agent.tools.search_parent", spy_parent)
    monkeypatch.setattr("app.agent.tools.search_multi", spy_multi)

    run(_retrieve_branches(_sources(), list(TITLES),
                           expansions={"绩效考核": ["绩效怎么打分"]}))

    assert parent == ["预算审批", "预算执行分析"]     # 第一支与第三支仍是单路
    assert multi == [["绩效考核", "绩效怎么打分"]]


def test_single_branch_never_calls_the_expander(monkeypatch):
    """成本闸：分支数为 1 时不调 `plan_branches`——多一次往返换不到任何东西。"""
    calls: list[list[str]] = []

    async def spy_plan(titles, *, materials=None):
        calls.append(list(titles))
        return {titles[0]: ["问法"]}

    monkeypatch.setenv("MINDMAP_QUERY_EXPANSION", "1")
    monkeypatch.setattr("app.agent.tools.plan_branches", spy_plan)

    assert run(_expand_branches(_sources(), ["预算审批"])) == {}
    assert calls == []

    # 两支才值得那一次共享往返
    assert run(_expand_branches(_sources(), ["预算审批", "绩效考核"])) == {
        "预算审批": ["问法"]}
    assert calls == [["预算审批", "绩效考核"]]


def test_expander_failure_never_breaks_retrieval(monkeypatch):
    """扩展抛错只等于"没有扩展"：`_expand_branches` 收成 `{}`，不往上抛。"""
    async def boom(titles, *, materials=None):
        raise RuntimeError("模型挂了")

    monkeypatch.setenv("MINDMAP_QUERY_EXPANSION", "1")
    monkeypatch.setattr("app.agent.tools.plan_branches", boom)

    assert run(_expand_branches(_sources(), ["预算审批", "绩效考核"])) == {}


# ---------------------------------------------------------------- 4. 融合：多路命中分数更高


def test_block_hit_by_two_queries_outranks_one_query_block():
    """被标题与问法**同时**命中的块，分数高于只被问法命中的块。

    这是多路唯一的价值来源（`search_multi` 的约束 5）：名次累加。两份来源让两块
    天然分开，断言才有意义——同一份材料里两个主题很容易落进同一个块。
    """
    hits = run(_search_branch(
        _sources(), "预算审批",
        ["绩效考核 打分复核", "预算审批 财务经理核准"]))

    scores = {hit.source_id: hit.score for hit in hits}
    assert "doc-budget" in scores and "doc-performance" in scores
    assert scores["doc-budget"] > scores["doc-performance"]


# ---------------------------------------------------------------- 5. 接进 `_generate_deep`


def _deep_ctx() -> RunContext:
    sources = _sources()
    request = AgentRunRequest(
        goal="整理管理办法", intent="create", sources=sources,
        budget=RunBudget(max_model_calls=20, max_tool_calls=8,
                         max_output_tokens=32768),
    )
    return RunContext(request=request, candidate=None,
                      sources_by_id={source.id: source for source in sources})


def _run_deep(monkeypatch, chat, expansion=None):
    """跑一次 `mindmap_generate_deep`；`expansion` 是给 `plan_branches` 的脚本输出。"""
    async def tool_chat(system, user, *, max_tokens=None, on_delta=None):
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(TITLES)}
        return {"children": [{"label": "要点", "expandable": False}]}

    monkeypatch.setenv("MINDMAP_SOURCE_MODE", "rag")
    monkeypatch.setenv("MINDMAP_QUERY_EXPANSION", "1" if expansion is not None else "0")
    monkeypatch.setattr("app.agent.tools.chat_async", tool_chat)
    if expansion is not None:
        _fake_chat(monkeypatch, expansion)

    registry = build_registry("create", has_sources=True)
    return run(registry.execute(
        ToolCall(call_id="c-deep", name="mindmap_generate_deep",
                 arguments={"topic": "管理办法", "branches": len(TITLES),
                            "points_per_branch": 2}),
        _deep_ctx(),
    ))


def test_deep_wires_expansion_into_retrieval(monkeypatch):
    """端到端：`retrieval.queries` 按支展开，`expanded_branches` 报出扩了几支。"""
    result = _run_deep(monkeypatch, None, expansion={
        "预算审批": ["预算审批要谁批", "谁批预算"],
        "绩效考核": ["绩效怎么打分"],
    })

    assert result.ok, result.content
    retrieval = result.data["retrieval"]
    assert retrieval["expanded_branches"] == 2
    assert retrieval["queries"] == [
        "预算审批", "预算审批要谁批", "谁批预算",
        "绩效考核", "绩效怎么打分",
        "预算执行分析",
    ]


def test_deep_without_expansion_keeps_one_query_per_branch(monkeypatch):
    """开关关着时 `retrieval.queries` 与改造前逐条相同（每支恰好一条标题）。"""
    result = _run_deep(monkeypatch, None, expansion=None)

    assert result.ok, result.content
    retrieval = result.data["retrieval"]
    assert retrieval["expanded_branches"] == 0
    assert retrieval["queries"] == list(TITLES)
    assert json.dumps(retrieval["queries"], ensure_ascii=False) == json.dumps(
        list(TITLES), ensure_ascii=False)
