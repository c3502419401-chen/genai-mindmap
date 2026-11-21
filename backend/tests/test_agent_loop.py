"""多轮工具循环的行为测试。

全部用脚本化的假 provider 驱动，不连网络也不消耗模型额度。这样做的理由：
循环里真正容易错的不是"怎么调模型"，而是结果配对、幂等、去重、预算结算、
终止判断——这些跟谁产生工具调用无关，用假实现能把每条分支都精确构造出来。
"""
from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel, Field, ValidationError

from app.agent.contracts import (
    AgentRunRequest,
    Completion,
    RunBudget,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from app.agent.provider import _parse_completion
from app.agent.service import MAX_LOG_MESSAGE_CHARS, _dedupe_key, outline_text, run
from app.agent.tools import PLAN_PROMPT, ToolRegistry, build_registry
from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source
from app.generation.service import (
    CONTEXT_TOKEN_LIMIT,
    GenerationError,
    ModelNotConfigured,
    estimate_input_tokens,
)

# 生成工具返回的树：5 个节点、三层。用于验证工具内调用与 snapshot 帧。
# 一级分支必须是 3 条：`mindmap_generate` 走 `validate_generated_content` 的默认
# 路径，`MIN_BRANCHES = 3` 会把 2 分支的树判成 invalid_map。
TREE = {
    "title": "示例",
    "root": {"id": "root", "label": "根", "children": [
        {"id": "a", "label": "甲", "children": []},
        {"id": "b", "label": "乙", "children": [
            {"id": "b1", "label": "乙一", "children": []},
        ]},
        {"id": "c", "label": "丙", "children": []},
    ]},
}


def _node_ids(root) -> set[str]:
    ids = {root.id}
    for child in root.children:
        ids |= _node_ids(child)
    return ids


def _completion(calls: list[ToolCall] | None = None, finish: str = "stop",
                content: str = "好的") -> Completion:
    calls = calls or []
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
            }
            for call in calls
        ]
    return Completion(message=message, tool_calls=calls, finish_reason=finish,
                      usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


class ScriptedProvider:
    """按脚本依次返回预设结果；脚本项可以是 Completion 或要抛的异常。"""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls = 0
        # 记下每次看到的 messages，用来断言"提醒"确实发给了模型
        self.seen: list[list[dict[str, Any]]] = []

    async def complete_tools(self, messages, tools, *, budget):
        self.calls += 1
        self.seen.append([dict(message) for message in messages])
        if not self._script:
            raise AssertionError("脚本已用完，循环比预期多调了一次模型")
        item = self._script.pop(0)
        # CancelledError 继承自 BaseException 而不是 Exception，用 Exception 判断
        # 会让"脚本里放一个取消异常"被当作正常返回值 return 出去。
        if isinstance(item, BaseException):
            raise item
        return item


class EchoArgs(BaseModel):
    text: str = Field(min_length=1)


class CountArgs(BaseModel):
    times: int = Field(ge=1)


def _collect(request: AgentRunRequest, provider, registry: ToolRegistry | None = None) -> list:
    """把 async 生成器收成列表。项目没装 pytest-asyncio，用 asyncio.run 更省事。"""

    async def drain():
        return [event async for event in run(request, provider=provider, tools=registry)]

    return asyncio.run(drain())


def _registry(handler, args_model=EchoArgs, name: str = "echo",
              readonly: bool = False) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolSpec(name=name, description="测试工具", args_model=args_model,
                               fn=handler, readonly=readonly))
    return registry


def _request(intent: str = "create", budget: RunBudget | None = None) -> AgentRunRequest:
    return AgentRunRequest(goal="给机器学习做一张导图", intent=intent,
                           budget=budget or RunBudget())


def _failure_logs(caplog) -> list[dict]:
    return [json.loads(record.args[0]) for record in caplog.records
            if record.name == "app.agent.service"]


def test_tool_failure_logs_reason_and_usage_without_material_or_tree(caplog):
    private_text = "不应出现在日志里的原文" * 100
    private_tree = {"root": {"label": "不应出现在日志里的树"}}

    def broken(args, ctx):
        ctx.reserve_model_call()
        return ToolResult(ok=False, content=private_text, data={
            "code": "invalid_map", "message": "引用与原文不匹配",
            "tree": private_tree, "diagnostics": [{"text": private_text}],
        })

    call = ToolCall(call_id="failure-1", name="echo", arguments={"text": private_text})
    request = AgentRunRequest(goal="整理", sources=[Source(kind="text", text=private_text)])
    events = _collect(request, ScriptedProvider([
        _completion([call], finish="tool_calls"), _completion(),
    ]), _registry(broken))
    end = next(event for event in events if event.type == "tool_end")
    assert end.result_summary["code"] == "invalid_map"
    assert end.result_summary["message"] == "引用与原文不匹配"
    # 这一轮最后是一句 stop 且没有候选，循环会额外记一条 no_tool_call 的诊断
    # （见 service.py 里那段注释）；这里只关心工具失败那一条。
    tool_failures = [e for e in _failure_logs(caplog) if e["code"] != "no_tool_call"]
    assert tool_failures == [{
        "call_id": "failure-1", "tool": "echo", "code": "invalid_map",
        "message": "引用与原文不匹配", "model_calls_used": 2, "tool_calls_used": 1,
    }]
    assert private_text not in caplog.text
    assert private_tree["root"]["label"] not in caplog.text


def test_failure_without_code_or_message_reaches_events_and_logs(caplog):
    call = ToolCall(call_id="missing-summary", name="echo", arguments={"text": "x"})
    events = _collect(_request(), ScriptedProvider([
        _completion([call], finish="tool_calls"), _completion(),
    ]), _registry(lambda args, ctx: ToolResult(
        ok=False, content="这次没有可用分支", data={"diagnostics": [], "failed_branches": ["甲"]},
    )))
    end = next(event for event in events if event.type == "tool_end")
    assert end.result_summary["code"] == "tool_failed"
    assert end.result_summary["message"] == "这次没有可用分支"
    assert end.result_summary["failed_branches"] == ["甲"]
    assert _failure_logs(caplog)[0]["message"] == end.result_summary["message"]


def test_long_failure_message_is_only_a_bounded_log_summary(caplog):
    reason = "模型校验失败\n" + "过长的模型输出" * MAX_LOG_MESSAGE_CHARS
    events = _collect(_request(), ScriptedProvider([GenerationError(reason)]))
    assert events[-1].message == reason
    log = _failure_logs(caplog)[0]
    assert log["code"] == "generation_failed"
    assert len(log["message"]) <= MAX_LOG_MESSAGE_CHARS
    assert reason not in caplog.text


def test_generation_error_is_logged_exactly_once(caplog):
    """一次失败只记一条：收尾处统一记，`except` 分支不许再记一遍。

    两处各记一次不会让排查变容易，只会让人以为"失败了两次"：同一句话在日志里
    出现两遍，还各自带着当时的 `model_calls_used`，看得人以为是两个不同的 run。
    """
    events = _collect(_request(), ScriptedProvider([GenerationError("模型服务返回 HTTP 502")]))
    assert events[-1].code == "generation_failed"

    logs = [entry for entry in _failure_logs(caplog) if entry["code"] == "generation_failed"]
    assert len(logs) == 1, f"同一次失败记了 {len(logs)} 条日志"
    assert logs[0]["message"] == "模型服务返回 HTTP 502"


@pytest.mark.parametrize("failure,code", [
    (GenerationError("模型服务返回 HTTP 429"), "generation_failed"),
    (RuntimeError("适配器意外失败"), "agent_failed"),
    (asyncio.TimeoutError(), "budget_exhausted"),
])
def test_provider_failure_is_logged_with_usage(caplog, failure, code):
    provider = ScriptedProvider([failure])
    if type(failure) is RuntimeError:
        with pytest.raises(RuntimeError, match="适配器意外失败"):
            _collect(_request(), provider)
    else:
        events = _collect(_request(), provider)
        assert events[-1].code == code
    log = _failure_logs(caplog)[0]
    assert log["code"] == code
    assert log["message"]
    assert log["call_id"] is None and log["tool"] is None
    assert log["model_calls_used"] == 1 and log["tool_calls_used"] == 0


@pytest.mark.parametrize("failure,code", [
    (GenerationError("工具上游失败"), "generation_failed"),
    (RuntimeError("工具意外失败"), "agent_failed"),
])
def test_tool_exception_is_logged_before_propagating(caplog, failure, code):
    def broken(args, ctx):
        ctx.reserve_model_call()
        raise failure

    call = ToolCall(call_id="raised-1", name="echo", arguments={"text": "x"})
    provider = ScriptedProvider([_completion([call], finish="tool_calls")])
    with pytest.raises(type(failure), match=str(failure)):
        _collect(_request(), provider, _registry(broken))
    log = _failure_logs(caplog)[0]
    assert log["code"] == code and str(failure) in log["message"]
    assert log["call_id"] == call.call_id and log["tool"] == call.name
    assert log["model_calls_used"] == 2 and log["tool_calls_used"] == 1


@pytest.mark.parametrize("limit", ["max_model_calls", "max_tool_calls"])
def test_budget_stop_is_logged_with_last_call_and_usage(caplog, limit):
    call = ToolCall(call_id="budget-1", name="echo", arguments={"text": "x"})
    provider = ScriptedProvider([_completion([call], finish="tool_calls")])
    events = _collect(_request(budget=RunBudget(**{limit: 1})), provider, _registry(_echo))
    log = _failure_logs(caplog)[0]
    assert log["code"] == events[-1].code == "budget_exhausted"
    assert log["message"] == events[-1].message
    assert log["call_id"] == call.call_id and log["tool"] == call.name
    assert log["model_calls_used"] == 1 and log["tool_calls_used"] == 1


def test_tool_timeout_logs_the_started_call_and_pairs_the_events(caplog):
    async def slow(args, ctx):
        ctx.reserve_model_call()
        await asyncio.Event().wait()

    call = ToolCall(call_id="timeout-1", name="echo", arguments={"text": "x"})
    events = _collect(
        _request(budget=RunBudget(max_seconds=0.1)),
        ScriptedProvider([_completion([call], finish="tool_calls")]), _registry(slow),
    )
    end = next(event for event in events if event.type == "tool_end")
    log = _failure_logs(caplog)[0]
    assert log["code"] == end.result_summary["code"] == "tool_timeout"
    assert log["message"] == end.result_summary["message"]
    assert log["call_id"] == call.call_id and log["tool"] == call.name
    assert log["model_calls_used"] == 2 and log["tool_calls_used"] == 1
    assert events[-1].limit == "max_seconds"


def test_excess_tool_calls_in_one_completion_are_logged_before_execution(caplog):
    first = ToolCall(call_id="allowed", name="echo", arguments={"text": "x"})
    second = ToolCall(call_id="blocked", name="echo", arguments={"text": "y"})
    events = _collect(
        _request(budget=RunBudget(max_tool_calls=1)),
        ScriptedProvider([_completion([first, second], finish="tool_calls")]), _registry(_echo),
    )
    log = _failure_logs(caplog)[0]
    assert log["code"] == "budget_exhausted"
    assert log["call_id"] == second.call_id and log["tool"] == second.name
    assert log["model_calls_used"] == 1 and log["tool_calls_used"] == 1
    assert [event.call_id for event in events if event.type == "tool_start"] == [first.call_id]


def test_repair_budget_logs_both_the_tool_failure_and_the_stop_reason(caplog):
    call = ToolCall(call_id="repair-1", name="echo", arguments={"text": "x"})
    events = _collect(
        _request(budget=RunBudget(max_repairs=0)),
        ScriptedProvider([_completion([call], finish="tool_calls")]),
        _registry(lambda args, ctx: ToolResult(ok=False, content="参数不合法")),
    )
    logs = _failure_logs(caplog)
    assert [log["code"] for log in logs] == ["tool_failed", "too_many_failures"]
    assert logs[-1]["message"] == events[-1].message
    assert all(log["call_id"] == call.call_id and log["tool"] == call.name for log in logs)
    assert all(log["model_calls_used"] == 1 and log["tool_calls_used"] == 1 for log in logs)


class BasicFlowTest(unittest.TestCase):
    def test_stop_without_tools_ends_with_done(self):
        """模型直接给文字收尾：只发 done，不发工具事件。

        整轮零工具调用会先被提醒一次，所以脚本给两段文字：提醒之后仍然只写字，
        循环就认了——这时的行为才是这条用例要固定的"只发 done"。
        """
        events = _collect(
            _request(), ScriptedProvider([_completion(), _completion()]), build_registry("create")
        )
        types = [event.type for event in events]
        self.assertEqual(types, ["stage", "done"])
        self.assertEqual(events[-1].content, None)  # create 且没有快照

    def test_seq_is_continuous_from_one(self):
        """seq 必须从 1 连续递增：前端靠它判断丢帧和乱序。"""
        events = _collect(
            _request(), ScriptedProvider([_completion(), _completion()]), build_registry("create")
        )
        self.assertEqual([event.seq for event in events], [1, 2])

    def test_text_only_failure_carries_the_model_words(self):
        """没产出导图时，done 帧要带上模型的原话与思考。

        线上出现过"模型只写字、不调工具"：那时接口层只有一帧 done，`journalctl`
        里也未必捞得到告警，页面上唯一能说清原因的就是这两个字段。
        """
        said = Completion(
            message={
                "role": "assistant",
                "content": "导图大概是：机器学习分监督和无监督……",
                "reasoning_content": "看到 mindmap_generate 了，但直接说更简单。",
            },
            finish_reason="stop",
        )
        events = _collect(_request(), ScriptedProvider([said, said]), build_registry("create"))

        self.assertEqual(events[-1].type, "done")
        self.assertIsNone(events[-1].content)
        self.assertIn("机器学习分监督和无监督", events[-1].text)
        self.assertIn("直接说更简单", events[-1].reasoning)

    def test_successful_result_does_not_carry_model_text(self):
        """有产出时不带原话：那是正常结果，不需要把整篇输出塞进事件流。"""
        snapshot = MindMapContent(**TREE)
        events = _collect(
            AgentRunRequest(goal="扩一下", intent="create", snapshot=snapshot),
            ScriptedProvider([_completion(), _completion()]),
            build_registry("create"),
        )
        self.assertEqual(events[-1].type, "done")
        self.assertIsNotNone(events[-1].content)
        self.assertEqual(events[-1].text, "")
        self.assertEqual(events[-1].reasoning, "")

    def test_tool_result_is_filled_back_by_call_id(self):
        """工具结果要按 tool_call_id 回填，且下一轮能看到它。"""
        seen_messages: list[list[dict]] = []

        class Recorder(ScriptedProvider):
            async def complete_tools(self, messages, tools, *, budget):
                seen_messages.append([dict(item) for item in messages])
                return await super().complete_tools(messages, tools, budget=budget)

        call = ToolCall(call_id="call_1", name="echo", arguments={"text": "原文"})
        provider = Recorder([_completion([call], finish="tool_calls"), _completion()])
        events = _collect(_request(), provider, _registry(_echo))

        self.assertIn("tool_start", [event.type for event in events])
        self.assertIn("tool_end", [event.type for event in events])
        # 第二轮的消息里必须有一条 role=tool 且 id 对得上
        second = seen_messages[1]
        tool_msgs = [item for item in second if item.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["tool_call_id"], "call_1")
        self.assertIn("echo:原文", tool_msgs[0]["content"])

    def test_finish_without_stop_and_without_tools_is_error(self):
        """既没有工具调用、finish_reason 又不是 stop：这是异常路径，不能当成功。"""
        events = _collect(_request(), ScriptedProvider([_completion(finish="content_filter")]),
                          build_registry("create"))
        self.assertEqual(events[-1].type, "error")
        self.assertEqual(events[-1].code, "unexpected_finish")


def _echo(args: EchoArgs, ctx) -> ToolResult:
    return ToolResult(ok=True, content=f"echo:{args.text}", data={"echoed": args.text})


class ToolDispatchTest(unittest.TestCase):
    def test_unknown_tool_executes_nothing_and_loop_continues(self):
        """模型编了个不存在的工具名：回一条可读错误，循环继续而不是崩掉。"""
        executed: list[str] = []

        def handler(args, ctx):
            executed.append("called")
            return ToolResult(ok=True, content="ok")

        call = ToolCall(call_id="c1", name="does_not_exist", arguments={})
        provider = ScriptedProvider([_completion([call], finish="tool_calls"), _completion()])
        events = _collect(_request(), provider, _registry(handler))

        self.assertEqual(executed, [])
        end = next(event for event in events if event.type == "tool_end")
        self.assertFalse(end.ok)
        self.assertEqual(end.result_summary["code"], "unknown_tool")
        self.assertEqual(events[-1].type, "done")

    def test_bad_arguments_executes_nothing(self):
        """参数缺必填字段：零执行，回 invalid_tool_args，循环继续。"""
        executed: list[str] = []

        def handler(args, ctx):
            executed.append("called")
            return ToolResult(ok=True, content="ok")

        call = ToolCall(call_id="c1", name="echo", arguments={})  # text 必填
        provider = ScriptedProvider([_completion([call], finish="tool_calls"), _completion()])
        events = _collect(_request(), provider, _registry(handler))

        self.assertEqual(executed, [])
        end = next(event for event in events if event.type == "tool_end")
        self.assertFalse(end.ok)
        self.assertEqual(end.result_summary["code"], "invalid_tool_args")
        self.assertEqual(events[-1].type, "done")

    def test_parse_error_is_reported_not_raised(self):
        """模型把参数 JSON 写坏了：报参数错误，不执行，也不让整轮崩。"""
        executed: list[str] = []

        def handler(args, ctx):
            executed.append("called")
            return ToolResult(ok=True, content="ok")

        call = ToolCall(call_id="c1", name="echo", arguments={},
                        raw_arguments='{"text": "半截', parse_error="函数参数不是合法 JSON")
        provider = ScriptedProvider([_completion([call], finish="tool_calls"), _completion()])
        events = _collect(_request(), provider, _registry(handler))

        self.assertEqual(executed, [])
        end = next(event for event in events if event.type == "tool_end")
        self.assertEqual(end.result_summary["code"], "invalid_tool_args")


class IdempotencyTest(unittest.TestCase):
    def test_same_call_id_runs_once(self):
        """同一个 call_id 出现两次，工具只该执行一次。"""
        executed: list[str] = []

        def handler(args, ctx):
            executed.append(args.text)
            return ToolResult(ok=True, content="ok")

        call = ToolCall(call_id="dup", name="echo", arguments={"text": "一次"})
        provider = ScriptedProvider([
            _completion([call, call], finish="tool_calls"),
            _completion(),
        ])
        _collect(_request(), provider, _registry(handler))
        self.assertEqual(executed, ["一次"])

    def test_readonly_tool_dedupes_same_arguments(self):
        """只读工具按参数去重：同样的查询必然得到同样的结果，重复执行没有意义。

        实测模型会并行发起两个检索请求，去重能省掉一次真实的网络往返。
        """
        executed: list[str] = []

        def handler(args, ctx):
            executed.append(args.text)
            return ToolResult(ok=True, content="ok")

        first = ToolCall(call_id="c1", name="echo", arguments={"text": "同样"})
        second = ToolCall(call_id="c2", name="echo", arguments={"text": "同样"})
        provider = ScriptedProvider([_completion([first, second], finish="tool_calls"), _completion()])
        _collect(_request(), provider, _registry(handler, readonly=True))
        self.assertEqual(executed, ["同样"])

    def test_write_tool_is_never_answered_from_a_cache(self):
        """写操作绝不能按参数去取"上一次的结果"。

        模型完全可能先改成 X、再改成 Y、再改回 X，第三次参数与第一次相同，
        但那时树的状态已经不是第一次的样子。走上缓存会直接返回旧结果，
        模型以为改成功了，实际停在 Y——比报错危险得多。

        `max_tool_calls` 提到 50 之后多了一道防线：**紧挨着**的同一个写调用会被
        判 `duplicate_call`（连续两次给同一个节点写同一个值本来就是空操作）。
        关键区别是它返回 `ok=False` 的"没有执行"，而不是一份被伪装成成功的结果
        ——上面那个危险正是被这一点挡掉的。隔一次别的调用之后（X→Y→X 的真实
        形状）照常执行，见下一条。
        """
        executed: list[str] = []

        def handler(args, ctx):
            executed.append(args.text)
            return ToolResult(ok=True, content="ok")

        calls = [ToolCall(call_id=f"c{index}", name="echo", arguments={"text": "同样"})
                 for index in range(2)]
        provider = ScriptedProvider([_completion(calls, finish="tool_calls"), _completion()])
        events = _collect(_request(), provider, _registry(handler, readonly=False))
        self.assertEqual(executed, ["同样"])
        ends = [event for event in events if event.type == "tool_end"]
        self.assertEqual(ends[1].result_summary["code"], "duplicate_call")

    def test_write_tool_runs_again_after_a_different_call(self):
        """X → Y → X：第三次必须真的执行。

        这是"写工具不能按参数缓存"的原始场景（用户可能说了"还是改回去"），也是
        防线一窗口的边界：它判的是**连续**重复，中间插过一次别的调用就放行。
        """
        executed: list[str] = []

        def handler(args, ctx):
            executed.append(args.text)
            return ToolResult(ok=True, content="ok")

        calls = [
            ToolCall(call_id="c0", name="echo", arguments={"text": "甲"}),
            ToolCall(call_id="c1", name="echo", arguments={"text": "乙"}),
            ToolCall(call_id="c2", name="echo", arguments={"text": "甲"}),
        ]
        provider = ScriptedProvider([_completion(calls, finish="tool_calls"), _completion()])
        events = _collect(_request(), provider, _registry(handler, readonly=False))
        self.assertEqual(executed, ["甲", "乙", "甲"])
        self.assertTrue(all(event.ok for event in events if event.type == "tool_end"))

    def test_same_call_id_with_different_arguments_is_conflict(self):
        """同一个 call_id 换参数是协议违规，必须报错而不是返回旧结果。"""
        executed: list[str] = []

        def handler(args, ctx):
            executed.append(args.text)
            return ToolResult(ok=True, content="ok")

        first = ToolCall(call_id="same", name="echo", arguments={"text": "甲"})
        second = ToolCall(call_id="same", name="echo", arguments={"text": "乙"})
        provider = ScriptedProvider([_completion([first, second], finish="tool_calls"), _completion()])
        events = _collect(_request(), provider, _registry(handler))

        self.assertEqual(executed, ["甲"])
        ends = [event for event in events if event.type == "tool_end"]
        self.assertTrue(ends[0].ok)
        self.assertFalse(ends[1].ok)
        self.assertEqual(ends[1].result_summary["code"], "call_id_conflict")

    def test_dedupe_key_ignores_key_order(self):
        """参数键序不同但内容相同，指纹必须一致。"""
        one = ToolCall(call_id="a", name="echo", arguments={"x": 1, "y": 2})
        two = ToolCall(call_id="b", name="echo", arguments={"y": 2, "x": 1})
        self.assertEqual(_dedupe_key(one), _dedupe_key(two))


class BudgetTest(unittest.TestCase):
    def test_model_call_budget_exhausted_is_error_not_done(self):
        """模型一直要调工具：到上限就报错，不能静默截断后假装成功。"""
        def handler(args, ctx):
            return ToolResult(ok=True, content="ok")

        calls = [ToolCall(call_id=f"c{index}", name="echo", arguments={"text": str(index)})
                 for index in range(10)]
        script = [_completion([call], finish="tool_calls") for call in calls]
        events = _collect(
            _request(budget=RunBudget(max_model_calls=2, max_tool_calls=50)),
            ScriptedProvider(script), _registry(handler),
        )
        self.assertEqual(events[-1].type, "error")
        self.assertEqual(events[-1].code, "budget_exhausted")
        self.assertEqual(events[-1].limit, "max_model_calls")

    def test_tool_call_budget_exhausted_is_error(self):
        """一次响应里塞超过上限的工具调用：超出的那些不执行。"""
        executed: list[str] = []

        def handler(args, ctx):
            executed.append(args.text)
            return ToolResult(ok=True, content="ok")

        calls = [ToolCall(call_id=f"c{index}", name="echo", arguments={"text": str(index)})
                 for index in range(4)]
        events = _collect(
            _request(budget=RunBudget(max_model_calls=5, max_tool_calls=2)),
            ScriptedProvider([_completion(calls, finish="tool_calls")]), _registry(handler),
        )
        self.assertEqual(len(executed), 2)
        self.assertEqual(events[-1].code, "budget_exhausted")
        self.assertEqual(events[-1].limit, "max_tool_calls")

    def test_budget_ran_out_after_candidate_is_done_not_error(self):
        """候选已经建好时预算用完：给 done，不把成果报成失败。

        收尾那轮只是想问模型「还要不要继续」，问不了不影响已经建好的树。
        真机上六支三层用掉 27 次而预算给 25 时，用户看到的是候选和一条
        「模型调用次数已用完」同时出现，不知道该不该用那份候选。
        """
        async def fake_chat(system, user, *, max_tokens=None, on_delta=None):
            if system.startswith(PLAN_PROMPT):
                return {"branches": ["分支一", "分支二", "分支三"]}
            return {"children": [{"label": "要点一", "expandable": False}]}

        call = ToolCall(call_id="gen-1", name="mindmap_generate_deep",
                        arguments={"topic": "机器学习", "branches": 3,
                                   "points_per_branch": 2})
        provider = ScriptedProvider([_completion([call], finish="tool_calls")])
        with patch("app.agent.tools.chat_async", new=fake_chat):
            # 2 次刚好够「外层发工具 + 工具内那一次骨架」，展开就没有额度了
            events = _collect(
                _request(budget=RunBudget(max_model_calls=2, max_tool_calls=50)),
                provider, build_registry("create_deep"))

        self.assertEqual(events[-1].type, "done")
        self.assertIsNotNone(events[-1].content)
        # 根节点的 label 就是主题（分片路的根在骨架之前就定好）
        self.assertEqual(events[-1].content.root.label, "机器学习")
        # 走这条收尾的产物来自模型，不是占位树：前端要据此标来源。
        self.assertEqual(events[-1].mode, "model")

    def test_repair_budget_limits_failed_tool_calls(self):
        """max_repairs 数的是工具失败次数，与模型调用次数无关。"""
        def broken(args, ctx):
            return ToolResult(ok=False, content="参数不对", data={"code": "nope"})

        calls = [ToolCall(call_id=f"c{index}", name="echo", arguments={"text": str(index)})
                 for index in range(4)]
        script = [_completion([call], finish="tool_calls") for call in calls]
        events = _collect(
            _request(budget=RunBudget(max_model_calls=10, max_tool_calls=50, max_repairs=1)),
            ScriptedProvider(script), _registry(broken),
        )
        failures = [event for event in events if event.type == "tool_end" and not event.ok]
        # 容忍 1 次，第 2 次就停。两次失败都必须有配对的 tool_end，
        # 否则前端会一直停在"执行中"。
        self.assertEqual(len(failures), 2)
        self.assertEqual([event.type for event in events].count("tool_start"), 2)
        self.assertEqual(events[-1].code, "too_many_failures")
        self.assertEqual(events[-1].limit, "max_repairs")

    def test_timed_out_tool_emits_paired_tool_end(self):
        """时间预算耗尽时，每个 tool_start 都要有配对的 tool_end。

        少了那一帧，事件序列是 `stage → tool_start → error`，前端那行工具永远
        停在「执行中」——`max_repairs` 判停那条路径修过同一个问题，超时这里漏了。
        """
        async def slow(args, ctx):
            await asyncio.sleep(2)
            return ToolResult(ok=True, content="太慢了")

        call = ToolCall(call_id="slow-1", name="echo", arguments={"text": "x"})
        provider = ScriptedProvider([_completion([call], finish="tool_calls")])
        events = _collect(
            _request(budget=RunBudget(max_model_calls=5, max_tool_calls=5,
                                      max_seconds=0.4)),
            provider, _registry(slow),
        )

        types = [event.type for event in events]
        self.assertEqual(types, ["stage", "tool_start", "tool_end", "error"])

        starts = [event.call_id for event in events if event.type == "tool_start"]
        ends = [event.call_id for event in events if event.type == "tool_end"]
        self.assertEqual(starts, ends)
        self.assertTrue(starts)  # 没有工具被调用的话这条测试什么都没证

        end = next(event for event in events if event.type == "tool_end")
        self.assertFalse(end.ok)
        self.assertEqual(end.result_summary["code"], "tool_timeout")
        self.assertEqual(events[-1].limit, "max_seconds")
        # seq 仍然连续：补帧不能打断前端判断丢帧的依据
        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))

    def test_budget_one_allows_exactly_one_real_model_call(self):
        """预算 1：外层调用 + 工具内部调用合计只能 1 次。

        这是实测缺陷的回归：判停只看外层轮数时，预算 1 的 run 仍然发出了
        4 次真实请求（外层 1 次，生成工具内部又调了 3 次）。
        """
        chats: list[str] = []

        async def fake_chat(system, user, *, max_tokens=None):
            chats.append(user)
            if system.startswith(PLAN_PROMPT):
                return {"branches": ["分支一", "分支二", "分支三"]}
            return {"children": [{"label": "要点一", "expandable": False}]}

        call = ToolCall(call_id="c1", name="mindmap_generate_deep",
                        arguments={"topic": "机器学习", "branches": 3})
        provider = ScriptedProvider([_completion([call], finish="tool_calls"), _completion()])
        with patch("app.agent.tools.chat_async", new=fake_chat):
            events = _collect(
                _request(budget=RunBudget(max_model_calls=1)),
                provider, build_registry("create_deep"),
            )

        self.assertEqual(provider.calls + len(chats), 1)
        # 唯一那次额度被外层用掉，工具必须直接失败而不是先花掉再上报
        self.assertEqual(chats, [])
        self.assertEqual(events[-1].type, "error")
        self.assertEqual(events[-1].limit, "max_model_calls")

    def test_budget_two_lets_the_tool_spend_the_second_call(self):
        """对照：预算 2 时工具能拿到第二次额度并真的出骨架。

        没有这一条，预算 1 的"工具内部零次调用"可能只是因为工具永远不调模型。
        """
        chats: list[str] = []

        async def fake_chat(system, user, *, max_tokens=None, on_delta=None):
            chats.append(user)
            if system.startswith(PLAN_PROMPT):
                return {"branches": ["分支一", "分支二", "分支三"]}
            return {"children": [{"label": "要点一", "expandable": False}]}

        call = ToolCall(call_id="c1", name="mindmap_generate_deep",
                        arguments={"topic": "机器学习", "branches": 3})
        provider = ScriptedProvider([_completion([call], finish="tool_calls"), _completion()])
        with patch("app.agent.tools.chat_async", new=fake_chat):
            events = _collect(
                _request(budget=RunBudget(max_model_calls=2)),
                provider, build_registry("create_deep"),
            )

        # 第二次额度给了骨架那一次调用：展开一次都没发（额度已经用完）
        self.assertEqual(len(chats), 1)
        self.assertEqual(provider.calls + len(chats), 2)
        snapshot = next(event for event in events if event.type == "snapshot")
        self.assertEqual([branch.label for branch in snapshot.content.root.children],
                         ["分支一", "分支二", "分支三"])
        self.assertTrue(all(not branch.children for branch in snapshot.content.root.children))
        # 候选已经建好，收尾那轮问不了 → done（不是 error）：与上一条同一语义。
        self.assertEqual(events[-1].type, "done")


class DeepRunUnderDefaultBudgetTest(unittest.TestCase):
    """六支分片在**默认预算**下要能走完整循环。

    这条是端到端的回归：分片工具自己算得再对，只要默认 `max_model_calls` 不够，
    收尾那轮就会把一次已经建好的树变成 `budget_exhausted`——用户拿不到 done，
    前端也就拿不到最终内容。
    """

    def test_six_branch_deep_run_finishes_with_done(self):
        # `on_delta` 是循环挂上上报通道后工具会传的回调。这条用例只关心预算与
        # 收尾，所以收到就丢：不消费它意味着这条调用没有产生任何预览帧，
        # 正好也证明"不上报不影响主流程"。
        async def fake_chat(system, user, *, max_tokens=None, on_delta=None):
            if system.startswith(PLAN_PROMPT):
                return {"branches": [f"分支{index}" for index in range(1, 7)]}
            return {"points": ["要点一", "要点二", "要点三"]}

        call = ToolCall(
            call_id="deep-1", name="mindmap_generate_deep",
            arguments={"topic": "机器学习", "branches": 6, "points_per_branch": 3},
        )
        provider = ScriptedProvider([
            _completion([call], finish="tool_calls"),
            _completion(),
        ])
        with patch("app.agent.tools.chat_async", new=fake_chat):
            events = _collect(_request(), provider, build_registry("create_deep"))

        self.assertEqual(events[-1].type, "done")
        self.assertEqual(len(events[-1].content.root.children), 6)
        for branch in events[-1].content.root.children:
            self.assertEqual(len(branch.children), 3)
        # 2 轮外层调用（第一轮发工具、第二轮收尾），工具内 7 次都记在同一个账本里
        self.assertEqual(provider.calls, 2)
        end = next(event for event in events if event.type == "tool_end")
        self.assertTrue(end.ok, end.result_summary)
        self.assertFalse(end.result_summary["partial"])

    def test_recursive_deep_run_fits_default_budget(self):
        """递归展开（模型自报可展开性）在默认预算下也要走完。

        上一条用的是 `points` 旧格式，语义就是叶子、根本不会递归，所以它证明
        不了「模型自己说还能再拆」这条路要花多少次调用。真机上六支三层用掉
        27 次，预算给 25 时收尾那轮必然报 budget_exhausted。
        """
        async def fake_chat(system, user, *, max_tokens=None, on_delta=None):
            if system.startswith(PLAN_PROMPT):
                return {"branches": [f"分支{index}" for index in range(1, 7)]}
            # 两个点里只有一个说自己还能拆，另一支到底
            return {"children": [
                {"label": "还能拆", "expandable": True},
                {"label": "到底了", "expandable": False},
            ]}

        call = ToolCall(
            call_id="deep-1", name="mindmap_generate_deep",
            arguments={"topic": "机器学习", "branches": 6,
                       "points_per_branch": 2, "max_depth": 3},
        )
        provider = ScriptedProvider([
            _completion([call], finish="tool_calls"),
            _completion(),
        ])
        with patch("app.agent.tools.chat_async", new=fake_chat):
            events = _collect(_request(), provider, build_registry("create_deep"))

        self.assertEqual(events[-1].type, "done")
        root = events[-1].content.root
        self.assertEqual(len(root.children), 6)
        for branch in root.children:
            self.assertEqual(len(branch.children), 2)
        # 递归确实发生了：至少有一支的「还能拆」自己又长出了子节点
        self.assertTrue(any(child.children for branch in root.children
                            for child in branch.children))
        # 默认预算没被撞破，收尾轮照常发出 done
        self.assertEqual(provider.calls, 2)


class OutlineRefreshTest(unittest.TestCase):
    def test_second_turn_sees_nodes_created_in_first_turn(self):
        """第二轮必须看到上一轮产出的节点 id。

        不刷新大纲的话，模型拿到的大纲永远是请求开始时的快照：它刚生成的节点
        没有 id 可引用，于是只能整张重来——那正是这次改造要消灭的行为。
        """
        seen: list[str] = []

        class Recorder(ScriptedProvider):
            async def complete_tools(self, messages, tools, *, budget):
                seen.append(messages[0]["content"])
                return await super().complete_tools(messages, tools, budget=budget)

        def generate(args, ctx):
            ctx.publish_tree(MindMapContent.model_validate({
                "title": "新图",
                "root": {"id": "root", "label": "根", "children": [
                    {"id": "fresh-node", "label": "刚生成的", "children": []},
                ]},
            }))
            return ToolResult(ok=True, content="已生成")

        call = ToolCall(call_id="c1", name="echo", arguments={"text": "生成"})
        provider = Recorder([_completion([call], finish="tool_calls"), _completion()])
        _collect(_request(), provider, _registry(generate))

        self.assertEqual(len(seen), 2)
        self.assertNotIn("fresh-node", seen[0])
        self.assertIn("[fresh-node] 刚生成的", seen[1])

    def test_tool_internal_model_calls_count_against_budget(self):
        """工具内部直接调模型也要计入总预算。

        生成和展开工具各自会再调一次模型。不计数的话 max_model_calls 只约束了
        外层轮数，实际花费可以翻好几倍。
        """
        def costly(args, ctx):
            ctx.extra_model_calls += 1
            return ToolResult(ok=True, content="ok")

        calls = [ToolCall(call_id=f"c{index}", name="echo", arguments={"text": str(index)})
                 for index in range(3)]
        script = [_completion([call], finish="tool_calls") for call in calls]
        events = _collect(
            _request(budget=RunBudget(max_model_calls=2, max_tool_calls=50)),
            ScriptedProvider(script), _registry(costly),
        )
        self.assertEqual(events[-1].type, "error")
        self.assertEqual(events[-1].limit, "max_model_calls")


class SnapshotEmissionTest(unittest.TestCase):
    """snapshot 帧定义了就必须真的发出来。

    只定义不发射时，前端只能在 done 时一次性拿到内容，"边生成边看"无从谈起。
    """

    def test_generate_success_emits_snapshot_with_valid_tree(self):
        async def fake_chat(system, user, *, max_tokens=None, on_delta=None):
            if system.startswith(PLAN_PROMPT):
                return {"branches": ["甲", "乙", "丙"]}
            return {"children": [{"label": "要点一", "expandable": False},
                                 {"label": "要点二", "expandable": False}]}

        call = ToolCall(call_id="c1", name="mindmap_generate_deep",
                        arguments={"topic": "机器学习", "branches": 3,
                                   "points_per_branch": 2, "max_depth": 2})
        provider = ScriptedProvider([_completion([call], finish="tool_calls"), _completion()])
        with patch("app.agent.tools.chat_async", new=fake_chat):
            events = _collect(_request(), provider, build_registry("create_deep"))

        types = [event.type for event in events]
        # 顺序即契约：tool_end 之后、done 之前；分片生成不产生 op 批次，
        # 所以这里看不到 ops 帧。中间那一段 `node_grow` 是生长预览（骨架落定先报
        # 根与一级分支，展开时每写出一个子项再报一条），帧数取决于模型给了几个
        # 节点，所以只钉形状不钉条数。
        self.assertEqual(types[:2], ["stage", "tool_start"])
        self.assertEqual(types[-3:], ["tool_end", "snapshot", "done"])
        self.assertEqual(set(types[2:-3]), {"node_grow"})
        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))

        labels = [event.label for event in events if event.type == "node_grow"]
        self.assertEqual(labels[0], "机器学习")          # 根最先报，path=None
        self.assertLessEqual({"甲", "乙", "丙"}, set(labels))

        snapshot = next(event for event in events if event.type == "snapshot")
        # 整树校验：能重新走一遍契约（id 唯一、层数、children 上限）才算完整
        replay = MindMapContent.model_validate(snapshot.content.model_dump())
        self.assertEqual([branch.label for branch in replay.root.children],
                         ["甲", "乙", "丙"])
        self.assertTrue(all(len(branch.children) == 2 for branch in replay.root.children))
        # snapshot 与 done 必须是同一棵树：两个来源不一致会让前端闪一下
        self.assertEqual(events[-1].content.model_dump(), snapshot.content.model_dump())


    def test_snapshot_is_not_emitted_when_candidate_already_exists(self):
        """expand / edit 一开始就有候选树：不重复发 snapshot。"""
        tree = MindMapContent.model_validate(TREE)

        def handler(args, ctx):
            ctx.publish_tree(tree.model_copy(deep=True))
            return ToolResult(ok=True, content="ok")

        call = ToolCall(call_id="c1", name="echo", arguments={"text": "改"})
        provider = ScriptedProvider([_completion([call], finish="tool_calls"), _completion()])
        request = AgentRunRequest(goal="展开节点", intent="edit", snapshot=tree)
        events = _collect(request, provider, _registry(handler))
        self.assertNotIn("snapshot", [event.type for event in events])


class ProviderParsingTest(unittest.TestCase):
    def test_length_finish_reason_refuses_to_parse_tools(self):
        """输出被硬切时，半截参数不能拿去执行。"""
        payload = {
            "choices": [{
                "finish_reason": "length",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "echo", "arguments": '{"text": "半'}}],
                },
            }],
            "usage": {},
        }
        with self.assertRaises(GenerationError):
            _parse_completion(bytearray(json.dumps(payload).encode()))

    def test_broken_arguments_become_parse_error_not_exception(self):
        """参数 JSON 坏掉：标记 parse_error 交给执行层，不在解析层抛。"""
        payload = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "echo", "arguments": '{"text": '}}],
                },
            }],
            "usage": {},
        }
        completion = _parse_completion(bytearray(json.dumps(payload).encode()))
        self.assertEqual(len(completion.tool_calls), 1)
        self.assertIsNotNone(completion.tool_calls[0].parse_error)
        self.assertEqual(completion.tool_calls[0].arguments, {})

    def test_valid_tool_call_is_decoded(self):
        payload = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "echo", "arguments": '{"text": "中文"}'}}],
                },
            }],
            "usage": {"total_tokens": 3},
        }
        completion = _parse_completion(bytearray(json.dumps(payload).encode()))
        self.assertEqual(completion.finish_reason, "tool_calls")
        self.assertEqual(completion.tool_calls[0].arguments["text"], "中文")
        self.assertEqual(completion.usage["total_tokens"], 3)

    def test_served_model_is_read_from_the_response_top_level(self):
        """响应体顶层的 `model` 要留下来：它是"服务端实际跑了哪个"的唯一证据。

        实测同一把 key、同一个地址：请求写 `deepseek-chat`，响应回 `deepseek-flash`
        （两个名字是同一模型的两个别名，区别只在思不思考）。丢掉这一位，界面上就
        只能显示我们发出去的那个名字，用户永远看不出请求被路由到了别处。
        """
        payload = {
            "model": "deepseek-flash",
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "好"}}],
            "usage": {},
        }
        completion = _parse_completion(bytearray(json.dumps(payload).encode()))
        self.assertEqual(completion.served_model, "deepseek-flash")

    def test_served_model_is_none_when_the_response_omits_it(self):
        """没有 `model` 就是"没说"：None，不是空串、也不是请求名。"""
        payload = {
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "好"}}],
            "usage": {},
        }
        completion = _parse_completion(bytearray(json.dumps(payload).encode()))
        self.assertIsNone(completion.served_model)


class CancelTest(unittest.TestCase):
    def test_cancelled_error_propagates(self):
        """取消必须穿透循环，不能被吞成一次正常结束。"""
        async def scenario():
            provider = ScriptedProvider([asyncio.CancelledError()])
            async for _ in run(_request(), provider=provider, tools=build_registry("create_deep")):
                pass

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(scenario())


class OutlineTest(unittest.TestCase):
    def test_outline_carries_node_ids(self):
        """大纲必须带 id：否则模型只能靠文字猜改哪一个，而文字会被它自己改写。"""
        content = MindMapContent.model_validate({
            "title": "示例",
            "root": {"id": "root", "label": "根", "children": [
                {"id": "n1", "label": "分支", "children": [
                    {"id": "n2", "label": "叶", "children": []},
                ]},
            ]},
        })
        text = outline_text(content)
        self.assertIn("[root] 根", text)
        self.assertIn("[n1] 分支", text)
        self.assertIn("[n2] 叶", text)
        self.assertIn("  ", text)  # 层级靠缩进表达

    def test_outline_of_none(self):
        self.assertIn("没有导图", outline_text(None))


class RegistryTest(unittest.TestCase):
    def test_fresh_registry_starts_empty(self):
        """注册表的初始状态是空的；业务工具由 build_registry() 填进去。

        这里断言的是 ToolRegistry 本身，不是 build_registry()——后者会随
        工具集演进，拿它当"空表"用迟早失效。
        """
        registry = ToolRegistry()
        self.assertEqual(registry.names(), [])
        self.assertEqual(registry.specs(), [])

    def test_duplicate_registration_is_rejected(self):
        registry = _registry(_echo)
        with self.assertRaises(ValueError):
            registry.register(ToolSpec(name="echo", description="重复", args_model=EchoArgs, fn=_echo))

    def test_schema_comes_from_args_model(self):
        """工具 schema 由参数模型生成，文档与校验同源，不会各写一份漂掉。"""
        schema = _registry(_echo).specs()[0]
        self.assertEqual(schema["function"]["name"], "echo")
        self.assertIn("text", schema["function"]["parameters"]["properties"])
        self.assertIn("required", schema["function"]["parameters"])

    def test_tool_returning_wrong_type_raises(self):
        """工具返回值写错要立刻暴露，不能让它以 None 传下去。"""
        def broken(args, ctx):
            return "不是 ToolResult"

        registry = _registry(broken)
        call = ToolCall(call_id="c1", name="echo", arguments={"text": "x"})

        async def scenario():
            return await registry.execute(call, ctx=None)

        with self.assertRaises(TypeError):
            asyncio.run(scenario())


class BudgetDefaultsTest(unittest.TestCase):
    """默认预算按实测数据定，不按「一句话主题」的场景拍。

    这两个默认值是整条 agent 路径的物理边界：时间上限决定了模型能不能把
    65536 token 的输出写完，输出上限决定了树能不能长到契约允许的规模。
    """

    def test_defaults_match_the_measured_limits(self):
        budget = RunBudget()
        self.assertEqual(budget.max_seconds, 600.0)
        self.assertEqual(budget.max_output_tokens, 65536)

    def test_seconds_ceiling_admits_an_hour(self):
        RunBudget(max_seconds=3600)          # 一小时是允许的物理上界
        with self.assertRaises(ValidationError):
            RunBudget(max_seconds=3601)


class SourcesContextTest(unittest.TestCase):
    """来源正文**没有**体积上限：正文不进 prompt，只进材料清单。

    这条边界原来是"`full` 形态下来源正文合计要装得进模型上下文"（超了在开流前
    422）。`full` 删除之后那个校验一并删掉了（`docs/ONE-PATH.md`）：正文段从不
    进 prompt，量它等于量一个不存在的对象。一份 25 万字的材料现在只贡献清单里
    一行（几十 token），所以下面这条"超大材料放行"就是当下的正确行为。

    份数上限（`MAX_SOURCES=50`）仍然在，见 `sources_not_too_many`。
    """

    def test_long_material_within_the_context_is_accepted(self):
        # 30000 汉字 ≈ 20000 token：字数上限删掉之后必须放行
        text = "字" * 30000
        request = AgentRunRequest(
            goal="整理材料",
            sources=[Source(kind="text", status="parsed", text=text)],
        )
        self.assertEqual(len(request.sources), 1)
        # 这份样本必须真的装得下，否则"放行"断言会因为限额变小而失去意义
        self.assertLess(estimate_input_tokens(text), CONTEXT_TOKEN_LIMIT)

    def test_material_over_the_context_is_accepted_now_that_the_body_never_enters(self):
        """超过上下文窗口的材料**放行**：正文整份进 prompt 的那条路已经不存在了。

        这条原来断言的是"必须抛 ValueError，被 FastAPI 收成 422"。删掉那个预检
        之后留着它只会挡住一份本来能处理的材料——它的正文不进 prompt，模型看到的
        是检索命中与章节标题行。样本按 `CONTEXT_TOKEN_LIMIT` 现算：限额是可调参数，
        写死字数的样本在调阈值之后就不再超限。
        """
        text = "字" * int(CONTEXT_TOKEN_LIMIT * 1.6)
        self.assertGreater(estimate_input_tokens(text), CONTEXT_TOKEN_LIMIT)

        request = AgentRunRequest(
            goal="整理材料",
            sources=[Source(kind="text", status="parsed", text=text)],
        )

        self.assertEqual(len(request.sources), 1)
        self.assertEqual(request.sources[0].text, text)

    def test_too_many_sources_still_get_rejected(self):
        """份数上限仍然拦，且文案说清"卡住的是清单"而不是"正文太大"。"""
        with self.assertRaises(ValidationError) as raised:
            AgentRunRequest(
                goal="整理材料",
                sources=[Source(kind="text", status="parsed", text="短文")
                         for _ in range(60)],
            )
        message = str(raised.exception)
        self.assertIn("超过单次请求上限", message)
        self.assertIn("清单", message)

# ---------------------------------------------------------------------------
# 模型只用文字回答、没调工具：先提醒一次，再不行才认
# ---------------------------------------------------------------------------


def _candidate_handler(args, ctx):
    ctx.publish_tree(MindMapContent.model_validate(TREE))
    return ToolResult(ok=True, content="ok", data={"content": TREE})


def test_nudges_once_when_model_answers_in_text(caplog):
    """线上实测的形态：模型把导图说明成一段文字，一条工具调用都没有。

    这时不该直接给用户一个空结果，而是明确要求它走工具再试一轮。
    """
    provider = ScriptedProvider([
        _completion(content="Rust 的学习路线大致是：基础语法、所有权、并发……", finish="stop"),
        _completion([ToolCall(call_id="gen-1", name="echo", arguments={"text": "生成"})],
                    finish="tool_calls"),
        _completion(),
    ])
    events = _collect(_request(), provider, _registry(_candidate_handler))
    done = next(event for event in events if event.type == "done")
    assert done.content is not None, "提醒之后模型改走了工具，结果不该是空的"
    assert [_e.name for _e in events if _e.type == "tool_start"] == ["echo"]
    assert provider.calls == 3
    # 第二次请求里必须能看到那句提醒，否则模型没有理由改变做法
    nudge = provider.seen[1][-1]
    assert nudge["role"] == "user" and "必须调用" in nudge["content"]
    assert any("no_tool_call" in json.loads(r.args[0])["code"] for r in caplog.records
               if r.name == "app.agent.service")


def test_gives_up_after_one_nudge(caplog):
    """提醒过还是只写字，就认：返回空结果，别在演示里反复空转额度。"""
    provider = ScriptedProvider([
        _completion(content="好的", finish="stop"),
        _completion(content="还是文字", finish="stop"),
    ])
    events = _collect(_request(), provider, _registry(_candidate_handler))
    done = next(event for event in events if event.type == "done")
    assert done.content is None
    assert provider.calls == 2, "只该提醒一次，不该无限重试"
    logged = [json.loads(r.args[0]) for r in caplog.records if r.name == "app.agent.service"]
    assert [entry["code"] for entry in logged] == ["no_tool_call", "no_tool_call"]


class PartialDeliveryTest(unittest.TestCase):
    """跑到一半停下时，已经长出来的树要交出去，标上"没长完"。

    从前六条终态路径各写一份收尾，其中两条交树、四条无条件发 error。同样的
    "停"在界面上于是有两种截然不同的样子：一次拿到了半棵树、一次什么都没拿到，
    而后者往往还是跑了三五分钟的那次。这些用例把四条改过的路径逐条钉住。
    """

    def _half_tree(self):
        return MindMapContent.model_validate(TREE)

    def _done(self, events):
        return next((event for event in events if event.type == "done"), None)

    def test_tool_timeout_hands_over_the_half_tree(self):
        """工具卡死超时：之前已经落进候选的树要交，而不是只甩一条错误。"""

        async def slow(args, ctx):
            ctx.publish_tree(self._half_tree())  # 先产出，再卡住
            ctx.reserve_model_call()
            await asyncio.Event().wait()

        call = ToolCall(call_id="slow-1", name="echo", arguments={"text": "x"})
        events = _collect(
            _request(budget=RunBudget(max_seconds=0.15)),
            ScriptedProvider([_completion([call], finish="tool_calls")]),
            _registry(slow),
        )
        done = self._done(events)
        assert done is not None, "已经有树了就不该只发 error"
        assert done.content is not None
        assert done.partial is True
        # 原因要能直接显示在结果旁边：它同时是 error 帧会用的那句话
        assert "时间上限" in (done.partial_reason or "")

    def test_too_many_failures_hands_over_the_half_tree(self):
        """连续失败到顶：树已经在那儿了，报错等于把它一起扔掉。"""

        def broken(args, ctx):
            ctx.publish_tree(self._half_tree())
            return ToolResult(ok=False, content="参数不对", data={"code": "nope"})

        calls = [ToolCall(call_id=f"c{index}", name="echo", arguments={"text": str(index)})
                 for index in range(2)]
        events = _collect(
            # max_repairs=1：判据是 `>`，所以第二次失败才停
            _request(budget=RunBudget(max_repairs=1)),
            ScriptedProvider([_completion(calls, finish="tool_calls")]),
            _registry(broken),
        )
        done = self._done(events)
        assert done is not None, "有树时 too_many_failures 该交树"
        assert done.partial is True
        assert "失败" in (done.partial_reason or "")

    def test_unexpected_finish_hands_over_the_half_tree(self):
        """`length` 截断这类意外收尾：前面长出来的树一样是能用的。"""

        def handler(args, ctx):
            ctx.publish_tree(self._half_tree())
            return ToolResult(ok=True, content="ok")

        call = ToolCall(call_id="c1", name="echo", arguments={"text": "x"})
        events = _collect(
            _request(),
            ScriptedProvider([
                _completion([call], finish="tool_calls"),
                _completion(finish="length"),
            ]),
            _registry(handler),
        )
        done = self._done(events)
        assert done is not None, "length 收尾不该把已有的树报成失败"
        assert done.partial is True
        assert "length" in (done.partial_reason or "")

    def test_tool_call_budget_hands_over_the_half_tree(self):
        """工具次数到顶：这一轮的其余工具不做了，但树要交出去。"""

        def handler(args, ctx):
            ctx.publish_tree(self._half_tree())
            return ToolResult(ok=True, content="ok")

        calls = [ToolCall(call_id=f"c{index}", name="echo", arguments={"text": str(index)})
                 for index in range(2)]
        events = _collect(
            _request(budget=RunBudget(max_tool_calls=1)),
            ScriptedProvider([_completion(calls, finish="tool_calls")]),
            _registry(handler),
        )
        done = self._done(events)
        assert done is not None
        assert done.partial is True

    def test_generation_error_hands_over_the_half_tree(self):
        """模型服务抛错时，已经长出来的那部分同样不该被丢掉。"""

        def handler(args, ctx):
            ctx.publish_tree(self._half_tree())
            return ToolResult(ok=True, content="ok")

        call = ToolCall(call_id="c1", name="echo", arguments={"text": "x"})
        events = _collect(
            _request(),
            ScriptedProvider([
                _completion([call], finish="tool_calls"),
                GenerationError("模型服务返回 HTTP 502"),
            ]),
            _registry(handler),
        )
        done = self._done(events)
        assert done is not None, "有树时模型层异常也不该把树扔掉"
        assert done.partial is True
        assert "502" in (done.partial_reason or "")

    def test_empty_tree_still_reports_the_error(self):
        """真的一棵树都没有时不能假装成功：那时用户确实什么都没拿到。"""

        async def slow(args, ctx):
            ctx.reserve_model_call()
            await asyncio.Event().wait()

        call = ToolCall(call_id="slow-1", name="echo", arguments={"text": "x"})
        events = _collect(
            _request(budget=RunBudget(max_seconds=0.15)),
            ScriptedProvider([_completion([call], finish="tool_calls")]),
            _registry(slow),
        )
        self.assertEqual(events[-1].type, "error")
        self.assertEqual(events[-1].limit, "max_seconds")
        self.assertFalse(any(event.type == "done" for event in events))

    def test_normal_finish_carries_no_partial_flag(self):
        """正常收尾的帧与加这个字段之前逐字节相同（`exclude_none` 会抹掉 None）。

        老客户端与 `_replay` 都依赖这一条：多一个 `"partial": null` 就是帧格式变了。
        """
        events = _collect(_request(), ScriptedProvider([_completion(), _completion()]),
                          build_registry("create"))
        done = self._done(events)
        assert done is not None
        self.assertIsNone(done.partial)
        self.assertIsNone(done.partial_reason)
        self.assertNotIn("partial", done.model_dump(mode="json", exclude_none=True))

    def test_model_not_configured_has_its_own_code(self):
        """没配模型密钥要有独立错误码：它的下一步是"去配"，不是"再试一次"。"""
        events = _collect(
            _request(),
            ScriptedProvider([ModelNotConfigured("请在后端环境变量中配置模型地址、密钥和模型名称")]),
            build_registry("create"),
        )
        error = events[-1]
        self.assertEqual(error.type, "error")
        self.assertEqual(error.code, "model_not_configured")
        self.assertIn("环境变量", error.message)


class EditKeepsItsOwnSnapshotTest(unittest.TestCase):
    """`edit`/`expand` 带着用户原树进来时，"有没有产出"不能拿候选位判。

    这是半棵树交付最容易踩反的一处：`edit` 的候选位从第一帧起就是用户那张导图
    （`router._validate` 要求非 create 必须带 snapshot），拿 `candidate is not None`
    当判据，任何一次失败都会被当成"交付了半棵树"——界面收到 `done`、把原封不动的
    快照重新写回画布、记一条 0 个节点变化的生成事件，而用户以为模型改过了。
    `model_not_configured` 尤其明显：本该弹"去配模型"的卡片，结果什么都没说。

    判据因此是"这一轮真的 publish 过树"，这组用例把两个方向都钉住：
    一棵都没长出来时必须是 error，长出来了才允许 done。
    """

    def _snapshot(self):
        return MindMapContent.model_validate(TREE)

    def _last(self, events):
        return events[-1]

    def _edit_request(self, **budget):
        return AgentRunRequest(
            goal="改一下这张图", intent="edit", snapshot=self._snapshot(),
            budget=RunBudget(**budget),
        )

    def test_edit_that_grew_nothing_reports_the_failure(self):
        """模型没配好 + 用户原树在手：必须报错，不能把原树当成果交回去。"""

        def noop(args, ctx):
            return ToolResult(ok=True, content="什么都没改")

        call = ToolCall(call_id="c1", name="echo", arguments={"text": "x"})
        events = _collect(
            self._edit_request(),
            ScriptedProvider([
                _completion([call], finish="tool_calls"),
                ModelNotConfigured("请在后端环境变量中配置模型地址、密钥和模型名称"),
            ]),
            _registry(noop),
        )
        last = self._last(events)
        self.assertEqual(last.type, "error", "一棵都没长出来，不能发 done")
        self.assertEqual(last.code, "model_not_configured")
        self.assertFalse(any(event.type == "done" for event in events))

    def test_edit_with_a_truncated_finish_is_a_correctable_failure(self):
        """`length` 截断且无产出：同样是 error，而不是"半棵树"。

        这条走的是 `unexpected_finish`，与上一条的 `ModelNotConfigured` 不是同一
        个分支，两条都要覆盖：判据在 `_deliver_or_fail` 里，而调用它有 8 条路。
        """

        def noop(args, ctx):
            return ToolResult(ok=True, content="什么都没改")

        call = ToolCall(call_id="c1", name="echo", arguments={"text": "x"})
        events = _collect(
            self._edit_request(),
            ScriptedProvider([
                _completion([call], finish="tool_calls"),
                _completion(finish="length"),
            ]),
            _registry(noop),
        )
        last = self._last(events)
        self.assertEqual(last.type, "error")
        self.assertEqual(last.code, "unexpected_finish")
        self.assertFalse(any(event.type == "done" for event in events))

    def test_edit_that_grew_a_tree_still_hands_it_over(self):
        """真的长出过树之后，`edit` 的半棵树照旧要交——修判据不能顺手把交付关掉。"""

        def grow(args, ctx):
            ctx.publish_tree(self._snapshot())
            return ToolResult(ok=True, content="改了")

        call = ToolCall(call_id="c1", name="echo", arguments={"text": "x"})
        events = _collect(
            self._edit_request(),
            ScriptedProvider([
                _completion([call], finish="tool_calls"),
                _completion(finish="length"),
            ]),
            _registry(grow),
        )
        done = next((event for event in events if event.type == "done"), None)
        self.assertIsNotNone(done)
        self.assertTrue(done.partial)
        self.assertIn("length", done.partial_reason or "")

    def test_tool_raising_generation_error_keeps_the_half_tree(self):
        """工具内部抛模型层异常：已有树要走统一收尾，不能整条上抛成 error。

        工具里那次模型调用失败，从前是 `raise outcome.error` 直接穿出循环，路由兜成
        `error` 帧——树在 `ctx` 上，但一个字节都没交出去。这正是"跑了三分钟什么都
        没拿到"的另一条路径。
        """

        def grow_then_explode(args, ctx):
            ctx.publish_tree(self._snapshot())
            raise GenerationError("模型服务返回 HTTP 502")

        call = ToolCall(call_id="c1", name="echo", arguments={"text": "x"})
        events = _collect(
            self._edit_request(),
            ScriptedProvider([_completion([call], finish="tool_calls")]),
            _registry(grow_then_explode),
        )
        last = self._last(events)
        self.assertEqual(last.type, "done", "树已经产出过，工具内异常不该把树扔掉")
        self.assertTrue(last.partial)
        self.assertIn("502", last.partial_reason or "")

    def test_tool_raising_generation_error_without_a_tree_reports_it(self):
        """同样在工具里抛，但一棵树都没长：继续上抛，由路由收成 `error` 帧。

        这一条故意留在上抛状态：没东西可交时，落库与终态帧的判断在路由那一层
        （它要把 run 标成 failed）。在循环里就地吞掉会让"失败"与"没产出"分不清。
        """

        def explode(args, ctx):
            raise GenerationError("模型服务返回 HTTP 502")

        call = ToolCall(call_id="c1", name="echo", arguments={"text": "x"})
        with pytest.raises(GenerationError):
            _collect(
                self._edit_request(),
                ScriptedProvider([_completion([call], finish="tool_calls")]),
                _registry(explode),
            )


class HeartbeatTest(unittest.TestCase):
    """长静默期间要有心跳帧。
    线上实测 3 路并发时单轮静默到过 25 秒，那段时间事件流里一个字节都没有；
    中间链路只要有 30–60 秒空闲超时就掐断连接，用户看到"连接中断"，而模型还在
    跑、还在计费。心跳的两个作用就是：不让连接空转，以及把"已等多久"送到页面。

    用例把 `HEARTBEAT_INTERVAL_SECONDS` 压到毫秒级，靠真实等待驱动，不打桩时间——
    这里的时序就是被测对象，把时间换成假时钟等于把要验的东西一起换掉了。
    """

    def test_heartbeats_during_a_slow_model_call(self):
        class SlowProvider:
            """第一次调用睡一会儿（模拟首字延迟），之后正常收尾。"""

            def __init__(self):
                self.calls = 0

            async def complete_tools(self, messages, tools, *, budget):
                self.calls += 1
                if self.calls == 1:
                    await asyncio.sleep(0.12)
                    return _completion([ToolCall(call_id="gen-1", name="echo",
                                                 arguments={"text": "生成"})],
                                       finish="tool_calls")
                return _completion()

        with patch("app.agent.service.HEARTBEAT_INTERVAL_SECONDS", 0.01):
            events = _collect(_request(), SlowProvider(), _registry(_candidate_handler))

        beats = [event for event in events if event.type == "heartbeat"]
        self.assertGreaterEqual(len(beats), 2, "静默 120 毫秒、心跳 10 毫秒，该发好几帧")
        # 心跳只带"静默了多久"，不该夹带业务信息
        self.assertTrue(all(beat.waited_ms >= 0 for beat in beats))
        self.assertGreaterEqual(beats[-1].waited_ms, beats[0].waited_ms)
        # seq 仍从 1 连续递增：心跳不能破坏前端"缺号即丢帧"的判断
        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(events[0].type, "stage")
        self.assertEqual(events[-1].type, "done")

    def test_heartbeats_during_a_slow_tool(self):
        async def slow(args, ctx):
            ctx.reserve_model_call()
            await asyncio.sleep(0.12)
            return ToolResult(ok=True, content="ok")

        call = ToolCall(call_id="slow-1", name="echo", arguments={"text": "x"})
        with patch("app.agent.service.HEARTBEAT_INTERVAL_SECONDS", 0.01):
            events = _collect(
                _request(),
                ScriptedProvider([_completion([call], finish="tool_calls"), _completion()]),
                _registry(slow),
            )

        types = [event.type for event in events]
        beats = [event for event in events if event.type == "heartbeat"]
        self.assertGreaterEqual(len(beats), 2)
        # 心跳必须夹在 tool_start 与 tool_end 之间：工具的静默期就是这里
        self.assertLess(types.index("tool_start"), types.index("heartbeat"))
        self.assertLess(types.index("heartbeat"), types.index("tool_end"))

    def test_no_heartbeat_when_calls_are_fast(self):
        """快调用不该冒出心跳：那只会把事件流刷成噪音。

        给两段文字脚本：零工具调用会先被提醒一次（`MAX_NUDGES`），
        这条用例要验的是"没有心跳"，不是提醒行为。
        """
        events = _collect(
            _request(), ScriptedProvider([_completion(), _completion()]), build_registry("create")
        )
        self.assertEqual([event.type for event in events], ["stage", "done"])

    def test_heartbeat_stops_when_budget_runs_out(self):
        """心跳不能把超时拖成无限静默：到了截止时间仍要正常收尾。"""

        async def sleep_forever(args, ctx):
            ctx.reserve_model_call()
            await asyncio.Event().wait()

        call = ToolCall(call_id="timeout-1", name="echo", arguments={"text": "x"})
        with patch("app.agent.service.HEARTBEAT_INTERVAL_SECONDS", 0.01):
            events = _collect(
                _request(budget=RunBudget(max_seconds=0.1)),
                ScriptedProvider([_completion([call], finish="tool_calls")]),
                _registry(sleep_forever),
            )
        self.assertEqual(events[-1].type, "error")
        self.assertEqual(events[-1].limit, "max_seconds")


if __name__ == "__main__":
    unittest.main()