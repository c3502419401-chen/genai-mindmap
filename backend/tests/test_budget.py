"""单次调用的产出预算：`节点数 × 满额字段 + JSON 开销 + 推理余量 ≤ min(三个上限)`。

这个文件守的是一条**算术**，不是某个数字。它要回答的问题是：

    「这一次调用要求模型写 N 个节点」，这件事在发出去之前，能不能证明装得下？

装不下的表现不是报错，是 `finish_reason=length` 之后拿到半棵树——从提示词那边看
模型完全照做了，从预算这边看要求本身就不该发出去。所以断言分三层：

  1. 公式本身（三个上限取最小、反解节点数、超过就拆批）；
  2. 公式与工具分配算的接线（`_generate_deep` 的 allowance 被容量夹住）；
  3. 配置面（`MINDMAP_LLM_MAX_TOKENS` 可配、服务端 400 自报的上限会被学到、
     预算默认值由"真实需要量"算出来）。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.contracts import (
    MAX_MODEL_CALLS_LIMIT,
    RunBudget,
    _default_max_model_calls,
    _initial_call_plan,
)
from app.agent.tools import (
    DEEP_FANOUT,
    DEEP_MIN_BREADTH,
    INITIAL_MAX_DEPTH,
    INITIAL_MAX_NODES,
    _nested_capacity,
    _node_limit,
    _nodes_per_call,
)
from app.contracts.mindmap import GENERATED_LABEL_MAX_CHARS, MAX_NODE_DETAIL_CHARS
from app.generation import budget
from app.generation.service import (
    MAX_OUTPUT_TOKENS,
    learn_max_tokens,
    max_tokens_cap,
    parse_max_tokens_limit,
    reset_learned_max_tokens,
)


# ---------------------------------------------------------------- 1. 公式


def test_three_limits_take_the_minimum():
    """三个上限取最小：预算、模型上限、剩余上下文。

    少任何一条都会在真实环境里翻车：只看预算时换个模型就 400；只看模型上限时
    上下文被输入吃掉一半之后输出空间其实已经缩水；只看上下文时 qwen 的 32768
    会被当成 65536 发出去。
    """
    assert budget.available_output_tokens(65536) == 65536
    assert budget.available_output_tokens(65536, model_cap=32768) == 32768
    assert budget.available_output_tokens(
        65536, model_cap=32768, context_left=4096) == 4096
    # None 是"这次不设限"，不是"上限为 0"
    assert budget.available_output_tokens(8192, model_cap=None, context_left=None) == 8192


def test_full_label_plus_detail_plus_refs_is_counted():
    """每个节点的成本 = 结构开销 + 16 字标签 + 详情 + 2 条引用（各带 300 字引文）。"""
    only_label = budget.per_node_tokens(detail_chars=0, refs=0)
    with_detail = budget.per_node_tokens(detail_chars=200, refs=0)
    with_refs = budget.per_node_tokens(detail_chars=200, refs=2)
    assert only_label < with_detail < with_refs
    # 每条引用固定 230 token，两条就是 460
    assert with_refs - with_detail == 2 * (budget.REF_STRUCTURE_TOKENS + budget.REF_QUOTE_TOKENS)
    # 16 字标签本身只占 11 token 左右，不能把公式的大头算到标签上
    assert budget.per_node_tokens(detail_chars=0, refs=0) < 100


def test_batch_cost_includes_json_overhead_and_reasoning_margin():
    """整批成本 = 信封 + 节点成本 + 推理余量，余量只加在内容上。"""
    node = budget.per_node_tokens(detail_chars=200, refs=0)
    one = budget.batch_output_tokens(1, detail_chars=200, refs=0)
    # 一个节点的整批成本严格大于它自己的内容成本：信封与余量都在里面
    assert one > node
    assert one >= node + budget.ENVELOPE_TOKENS
    # 线性：N 个节点的成本就是 N 倍内容成本（余量按比例，不按节点数另加）
    ten = budget.batch_output_tokens(10, detail_chars=200, refs=0)
    assert ten > budget.batch_output_tokens(1, detail_chars=200, refs=0)
    # 逐批 ceil 会带来几个 token 的取整差，允许 ±1%
    assert ten - budget.ENVELOPE_TOKENS == pytest.approx(
        10 * (one - budget.ENVELOPE_TOKENS), rel=0.01)
    # 空批次也不该是 0：顶层的大括号与 children 键仍在
    assert budget.batch_output_tokens(0) == budget.ENVELOPE_TOKENS


def test_nodes_per_call_is_the_inverse_of_the_cost():
    """反解出来的节点数，回代成本必须装得进同一个预算。"""
    for tokens in (2048, 8192, 32768, 65536):
        capacity = budget.nodes_per_call(tokens)
        assert capacity >= 0
        if capacity:
            assert budget.batch_output_tokens(capacity) <= tokens
        # 多要一个就装不下——这才是"上界"的定义
        assert budget.batch_output_tokens(capacity + 1) > tokens


def test_tiny_budget_yields_zero_nodes():
    """装不下任何一个满额节点时返回 0：调用方据此停手，而不是硬发一次。"""
    assert budget.nodes_per_call(200) == 0
    assert budget.plan_batches(10, 0) == []


def test_full_scale_guard_splits_into_batches():
    """**满额守卫**：100 个"契约满额"节点装不进一次调用，必须被拆批。

    这是这条公式存在的全部理由。按契约上限（2000 字详情）算，单节点约 1.8k
    token，一次 65536 的调用装得下 26 个左右；要求 100 个必然截断。正确行为是
    拆成 4 批，每批都装得下——而不是发出一份注定被切断的请求。
    """
    tokens = MAX_OUTPUT_TOKENS
    per_call = budget.nodes_per_call(tokens, detail_chars=MAX_NODE_DETAIL_CHARS)
    assert 0 < per_call < 100, "满额节点不该一次装得下 100 个"

    batches = budget.plan_batches(100, per_call)
    assert sum(batches) == 100
    assert all(size <= per_call for size in batches)
    assert len(batches) >= 2, "装不下就必须拆批，不能硬发"
    for size in batches:
        assert budget.batch_output_tokens(
            size, detail_chars=MAX_NODE_DETAIL_CHARS) <= tokens


def test_prompt_scale_always_fits_one_branch_expansion():
    """提示词口径下，一次展开（含"模型多给一层"）一定装得进单次预算。

    `_generate_deep` 一次最多要 `DEEP_FANOUT` 个要点 + 每点最多 `DEEP_FANOUT` 个
    多给的下层 = 5 + 25 = 30 个节点。三者取小之后的容量必须不小于它，否则"分片
    生成"这个名字就名不副实——每一批都会被截断。
    """
    worst = DEEP_FANOUT * (1 + _nested_capacity(DEEP_FANOUT, 1))
    assert worst == 30
    assert budget.nodes_per_call(MAX_OUTPUT_TOKENS) >= worst
    assert budget.nodes_per_call(32768) >= worst


# ---------------------------------------------------------------- 2. 初次规模与调用数


def test_initial_tree_is_four_levels_and_eighty_nodes():
    """初次满额树：`1 + 6 + 18 + 54 = 79` 个节点，4 层含根。"""
    sizes = budget.level_sizes(6, 3, INITIAL_MAX_DEPTH + 1)
    assert sizes == [1, 6, 18, 54]
    assert sum(sizes) == 79
    assert sum(sizes) <= INITIAL_MAX_NODES
    # 第 5 层会是多少：241，装不进 80
    assert sum(budget.level_sizes(6, 3, INITIAL_MAX_DEPTH + 2)) == 241


def test_real_call_count_is_not_the_node_count():
    """**真实调用数 25，不是节点数 79**——本次最容易算错的一处。

    骨架产出第 1 层，之后每次调用展开一个父节点：展开 6 个分支产出第 2 层、
    展开 18 个二级产出第 3 层。**最后那一层（54 个）不需要再被展开**，它已经被
    上一层写出来了。把 54 也算进去会得出 79，进而误判"80 装不下 4 层"。
    """
    assert budget.calls_for_levels([1, 6, 18, 54]) == 1 + 6 + 18 == 25
    assert budget.calls_for_levels([1, 3, 6]) == 1 + 3 == 4
    # 只有根：骨架那一次就够了
    assert budget.calls_for_levels([1]) == 1
    # 两层：骨架 + 展开一层
    assert budget.calls_for_levels([1, 4]) == 1


def test_default_budget_covers_the_initial_plan():
    """默认调用额度必须覆盖"4 层满额 + 补点 + 外层轮次"，而不是拍一个数。"""
    plan = _initial_call_plan()
    # 25 次内容调用 + 6 次补点 + 2 轮对话
    assert plan == 25 + 6 + 2 == 33
    assert RunBudget().max_model_calls == _default_max_model_calls()
    # 默认值是这 33 次的 3 倍（重做一张 + 十几次扩展），向上取整到 100
    assert RunBudget().max_model_calls == 100
    assert RunBudget().max_model_calls >= plan * 2
    # 旧默认值 80 对**一棵 4 层树**其实够用（33 次）；它被烧光是"没有层数上限"
    # 那几年的事（7 层 / 240 节点要一百多次）。这条断言守的是"默认值别再降回 80"。
    assert RunBudget().max_model_calls > 80


def test_max_model_calls_can_be_raised_above_120():
    """120 不再是天花板：真实需要量变大时必须能调到更高，但仍有上界。"""
    assert RunBudget(max_model_calls=200).max_model_calls == 200
    assert RunBudget(max_model_calls=MAX_MODEL_CALLS_LIMIT).max_model_calls == MAX_MODEL_CALLS_LIMIT
    with pytest.raises(ValidationError):
        RunBudget(max_model_calls=MAX_MODEL_CALLS_LIMIT + 1)


# ---------------------------------------------------------------- 3. 输出上限可配


def test_cap_follows_the_environment(monkeypatch):
    """`MINDMAP_LLM_MAX_TOKENS` 覆盖默认值：qwen 系只认 32768，不该改代码。"""
    reset_learned_max_tokens()
    assert max_tokens_cap() == MAX_OUTPUT_TOKENS
    monkeypatch.setenv("MINDMAP_LLM_MAX_TOKENS", "32768")
    assert max_tokens_cap() == 32768
    # 非法值按默认值继续，不抛错：这是一个可选调优项
    monkeypatch.setenv("MINDMAP_LLM_MAX_TOKENS", "lots")
    assert max_tokens_cap() == MAX_OUTPUT_TOKENS
    # 配得比默认值大是允许的（本地部署的模型可能就是更大）
    monkeypatch.setenv("MINDMAP_LLM_MAX_TOKENS", "131072")
    assert max_tokens_cap() == 131072


def test_learned_server_ceiling_wins(monkeypatch):
    """服务端自报的上限比配置值更可信：它是这次真的会 400 的那个数。"""
    reset_learned_max_tokens()
    monkeypatch.setenv("MINDMAP_LLM_MAX_TOKENS", "65536")
    learn_max_tokens(8192)
    try:
        assert max_tokens_cap() == 8192
        # 只往下学：再学到更大的值不该把它抬回去
        learn_max_tokens(65536)
        assert max_tokens_cap() == 8192
    finally:
        reset_learned_max_tokens()
    assert max_tokens_cap() == 65536


def test_server_error_text_is_parsed():
    """两种见过的 400 文案都要认；认不出来就返回 None（不猜）。"""
    assert parse_max_tokens_limit(
        '{"error":{"message":"Range of max_tokens should be [1, 32768]"}}') == 32768
    assert parse_max_tokens_limit(
        "max_tokens is too large: 65536. This model supports at most 16384") == 16384
    assert parse_max_tokens_limit("invalid api key") is None
    assert parse_max_tokens_limit("") is None


def test_server_rejecting_max_tokens_is_downgraded_once(monkeypatch):
    """服务端 400 里自报上限时，自动降级**重试一次**，并把学到值记住。

    不降级的后果是"换一个模型就整条生成路径报错"，而错文案指向的是"请检查服务
    配置"——配置本身是对的，只是上限不同。只重试一次：重试的请求体里已经带着
    学到的上限，再撞 400 说明问题不在这个数（密钥、模型名、余额都会给 400）。
    """
    import asyncio
    import json as jsonlib

    from app.generation.service import chat_async

    reset_learned_max_tokens()
    monkeypatch.setenv("MINDMAP_LLM_MAX_TOKENS", "65536")

    sent: list[dict] = []

    class _Response:
        def __init__(self, status: int, payload: bytes) -> None:
            self.status_code = status
            self._payload = payload
            self.request = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aread(self):
            return self._payload

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                import httpx

                request = httpx.Request("POST", "https://example.com")
                # 真身上 `exc.response` 就是那个带正文的流式响应；这里照着造，
                # 否则降级逻辑读不到 `Range of max_tokens should be [1, N]`。
                raise httpx.HTTPStatusError(
                    f"HTTP {self.status_code}", request=request,
                    response=httpx.Response(self.status_code, request=request,
                                            content=self._payload),
                )

        async def aiter_bytes(self):
            yield self._payload

    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, **kwargs):
            sent.append(jsonlib.loads(kwargs["content"]))
            self.calls += 1
            if self.calls == 1:
                body = jsonlib.dumps({"error": {
                    "message": "Range of max_tokens should be [1, 32768]"}}).encode()
                return _Response(400, body)
            body = jsonlib.dumps({"choices": [
                {"message": {"content": "{\"title\":\"t\"}"}, "finish_reason": "stop"}]}).encode()
            return _Response(200, body)

    client = _Client()
    for name, value in {"MINDMAP_LLM_MODE": "model",
                        "MINDMAP_LLM_BASE_URL": "https://example.com/v1",
                        "MINDMAP_LLM_MODEL": "test",
                        "MINDMAP_LLM_API_KEY": "k"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("app.generation.service.httpx.AsyncClient",
                        lambda *a, **k: client)
    try:
        result = asyncio.run(chat_async("系统", "用户", max_tokens=65536))
        assert result == {"title": "t"}
        # 两次请求：第一次用配置值，第二次用服务端自报的 32768
        assert [item["max_tokens"] for item in sent] == [65536, 32768]
        # 学到值在进程内生效：后续请求直接用它
        assert max_tokens_cap() == 32768
    finally:
        reset_learned_max_tokens()


# ---------------------------------------------------------------- 4. 接线：工具真的用了容量


def test_deep_tool_capacity_is_wired_into_the_allowance():
    """`_nodes_per_call` 的结果必须被 `_generate_deep` 当作第三个上限。

    这里不重跑整条生成链（那在 `test_generation_limits.py`），只验两件可从外部
    观察的事实：容量随预算单调、且 2048 这种极小预算下不会装下"5 个要点 + 一层"。
    """
    from app.agent.contracts import AgentRunRequest, RunContext

    def capacity(max_output_tokens: int) -> int:
        request = AgentRunRequest(
            goal="做一张导图", intent="create",
            budget=RunBudget(max_model_calls=10, max_output_tokens=max_output_tokens),
        )
        return _nodes_per_call(RunContext(request=request))

    assert capacity(2048) <= 2
    assert capacity(8192) < capacity(32768)
    assert capacity(32768) >= DEEP_MIN_BREADTH
    # 容量永远不超过"按满额口径算出来"的那个数——它是同一条公式的另一个入口
    assert capacity(65536) <= budget.nodes_per_call(65536)


def test_label_budget_follows_the_contract_constant():
    """16 字那个数只在契约里定义一次，预算公式跟着它走。"""
    assert GENERATED_LABEL_MAX_CHARS == 16
    narrow = budget.per_node_tokens(label_chars=16, detail_chars=0, refs=0)
    wide = budget.per_node_tokens(label_chars=200, detail_chars=0, refs=0)
    assert wide > narrow, "旧图那种 200 字标题的节点确实更贵，公式要能表达这一点"


def test_node_limit_is_capped_for_the_first_generation():
    """契约是承载能力（1000），初次承诺是 80——两者不能混成一个数。"""
    assert _node_limit() > INITIAL_MAX_NODES
    assert INITIAL_MAX_NODES == 80
    assert INITIAL_MAX_DEPTH == 3
