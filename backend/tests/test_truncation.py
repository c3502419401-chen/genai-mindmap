"""截断回收：`finish_reason=length` 不再等于"整棵树报废"。

改这条行为之前的两种做法都试过，各有一个致命面：

  - 一律抛错：模型已经写好的几十个节点跟着丢掉，用户点了"生成"什么也拿不到；
  - 交给 `_balance_brackets` 补齐：半截 JSON 被补成"合法"，缺了一块的树被当成
    完整结果交出去——比失败难查得多，因为界面上看不出少了什么。

第三条路是**只收闭合了的对象**：写了一半的 label 根本不会闭合，自然进不来；
完整的子树才进得来，并且工具会在 `diagnostics` 里明说这次被截断了。
"""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import build_registry
from app.generation.service import (
    GenerationError,
    SalvagedOutput,
    chat_async,
    parse_model_json,
    salvage_model_json,
)


# 一棵"会被切成两半"的树：第三个分支写到一半就没有了。
FULL_TREE = (
    '{"title":"测试","root":{"id":"r","label":"根","children":['
    '{"id":"a","label":"甲","detail":"甲的解释","children":[]},'
    '{"id":"b","label":"乙","detail":"乙的解释","children":[]},'
    '{"id":"c","label":"丙","children":[]}]}}'
)
CUT_AT_THIRD = FULL_TREE[: FULL_TREE.index('{"id":"c"') + 18]


# ---------------------------------------------------------------- 纯解析


def test_only_closed_nodes_are_recovered():
    """被切断的第三个节点（label 都没写完）不进来，前两个完整节点进来。"""
    salvaged = salvage_model_json(CUT_AT_THIRD)
    assert isinstance(salvaged, SalvagedOutput)
    assert salvaged.partial is True
    labels = [node["label"] for node in salvaged["root"]["children"]]
    assert labels == ["甲", "乙"]
    assert salvaged.recovered == 2
    # 根自己的 id/label 也保住了：没有根就没有树
    assert salvaged["root"]["label"] == "根"
    assert salvaged["title"] == "测试"


def test_partial_label_is_never_recovered():
    """切在 label 中间：那个对象没有闭合，回收不出来也不该硬凑。"""
    text = '{"title":"t","root":{"id":"r","label":"根","children":[{"id":"a","label":"写了一半的'
    assert salvage_model_json(text) is None


def test_children_shape_is_recovered_too():
    """分片展开的形状（顶层直接是 `children`）走同一条回收路径。"""
    text = ('{"children":[{"label":"一","detail":"说明一"},'
            '{"label":"二","detail":"写了一半')
    salvaged = salvage_model_json(text)
    assert salvaged is not None
    assert [node["label"] for node in salvaged["children"]] == ["一"]


def test_unrecognised_shape_returns_none():
    """认不出形状就不猜：猜出来的结果会被下游当成一次成功的解析。"""
    assert salvage_model_json('{"points":["一","二"') is None
    assert salvage_model_json("完全不是 JSON") is None


def test_complete_json_still_goes_through_the_normal_parser():
    """没被截断时行为逐字不变：正常解析拿到三个节点，回收器根本不参与。"""
    parsed = parse_model_json(FULL_TREE)
    assert [node["label"] for node in parsed["root"]["children"]] == ["甲", "乙", "丙"]
    # 回收器只认"最外层已闭合的**节点**"：完整 JSON 的最外层是整份响应
    # （`{title, root}`），它自己不是节点，所以回收返回 None——调用方也只在
    # `finish_reason=length` 时才叫它。这条断言守的是"两条路不会互相串门"。
    assert salvage_model_json(FULL_TREE) is None


# ---------------------------------------------------------------- HTTP 层


class _FakeResponse:
    def __init__(self, body: bytes, status_code: int = 200, finish_reason: str | None = None):
        self.body = body
        self.status_code = status_code
        self.finish_reason = finish_reason
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        yield self.body


class _FakeClient:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def stream(self, method, url, **kwargs):
        return self.response


def _body(content: str, finish_reason: str | None) -> bytes:
    return json.dumps({
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
    }).encode()


def _env() -> dict[str, str]:
    return {
        "MINDMAP_LLM_MODE": "model",
        "MINDMAP_LLM_BASE_URL": "https://example.com/v1",
        "MINDMAP_LLM_MODEL": "test",
        "MINDMAP_LLM_API_KEY": "not-a-real-key",
    }


def _chat(content: str, finish_reason: str | None):
    with patch.dict(os.environ, _env()), patch(
            "app.generation.service.httpx.AsyncClient",
            return_value=_FakeClient(_FakeResponse(_body(content, finish_reason)))):
        return asyncio.run(chat_async("系统", "用户"))


def test_truncated_response_is_salvaged_not_failed():
    """`finish_reason=length` + 可回收的半棵树 → 返回 partial 结果，不抛错。"""
    result = _chat(CUT_AT_THIRD, "length")
    assert isinstance(result, SalvagedOutput)
    assert [node["label"] for node in result["root"]["children"]] == ["甲", "乙"]


def test_unrecoverable_truncation_still_raises():
    """一个完整节点都回收不出来时才失败——那时确实没有可交付的东西。"""
    with pytest.raises(GenerationError) as raised:
        _chat('{"title":"t","root":{"id":"r","label":"写了一半', "length")
    assert "截断" in str(raised.value)


def test_stop_reason_parses_normally():
    """`stop` 走原路：不回收、不标 partial。"""
    result = _chat(FULL_TREE, "stop")
    assert not isinstance(result, SalvagedOutput)
    assert len(result["root"]["children"]) == 3


# ---------------------------------------------------------------- 工具层


def _ctx() -> RunContext:
    request = AgentRunRequest(
        goal="做一张导图", intent="create",
        budget=RunBudget(max_model_calls=10, max_output_tokens=32768),
    )
    return RunContext(request=request)


def test_deep_tool_marks_partial_and_warns_on_truncation():
    """分片展开被截断时：已有的完整节点落树，`partial` 与诊断都要有。"""
    plan = {"branches": ["甲", "乙", "丙"]}

    async def fake(system, user, *, max_tokens=None):
        if "只定一级骨架" in system:
            return plan
        # 一次展开只写出了第一个节点就被切断
        return SalvagedOutput(
            {"children": [{"label": f"{user.split('本条一级分支：')[1].split(chr(10))[0]}一"}]},
            recovered=1,
        )

    ctx = _ctx()
    with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
        result = asyncio.run(build_registry("create").execute(
            ToolCall(call_id="c", name="mindmap_generate_deep",
                     arguments={"topic": "主题", "branches": 3, "points_per_branch": 2}),
            ctx,
        ))

    assert result.ok, result.content
    codes = [item["code"] for item in result.data["diagnostics"]]
    assert "OUTPUT_TRUNCATED_RECOVERED" in codes
    # 回收到的节点确实进了树
    assert len(ctx.candidate.root.children) == 3
    assert all(len(branch.children) == 1 for branch in ctx.candidate.root.children)
