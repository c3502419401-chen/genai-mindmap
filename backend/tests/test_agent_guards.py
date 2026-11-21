"""`max_tool_calls` 提到 50 之后的配套防线。

把额度从 8 提到 50 是一次**放开闸门**：8 曾经靠"次数用完就停"自然掐断了
"查一下→再查一下"的死循环，50 拦不住它了。所以这一份用例钉的是三件事：

  1. 预算真的变了，而且没有连带撞上别的上限（50 次工具 < 100 次模型调用）；
  2. 两条**行为**防线——连续重复调用、连续无进展——各自在自己的阈值上生效，
     而且不在边角上误伤（`A, B, A` 必须放行、展开节点必须算"有进展"）；
  3. 用户真的能用到 50：30 次真实 `add_node` 一路跑到底，不撞任何上限。

全部用脚本化 provider 驱动，不连网络、不耗额度。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.agent.contracts import (
    MAX_TOOL_CALLS_LIMIT,
    AgentRunRequest,
    Completion,
    RunBudget,
    RunContext,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from app.agent.service import (
    NO_PROGRESS_ABORT,
    NO_PROGRESS_WARN,
    REPEAT_CALL_WINDOW,
    _budget_message,
    _duplicate_anchor,
    _loop,
    _tree_signature,
)
from app.agent.tools import RagArgs, ToolRegistry, _count_nodes, build_registry
from app.contracts.mindmap import MindMapContent, Node

# 5 个节点、三层。与 `test_agent_tools.py` 的样本同形：一级分支 3 条。
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
NODE_COUNT = 5


def _tree() -> MindMapContent:
    return MindMapContent.model_validate(TREE)


def _request(intent: str = "edit", budget: RunBudget | None = None,
             snapshot: MindMapContent | None = None) -> AgentRunRequest:
    return AgentRunRequest(goal="改这张导图", intent=intent,
                           budget=budget or RunBudget(), snapshot=snapshot)


def _ctx(request: AgentRunRequest, candidate: MindMapContent | None = None,
         *, produced: bool = False) -> RunContext:
    """造一个 run 上下文。

    `produced=True` 表示「这一轮已经真的长出过树」——收尾判据看的是这个标记，
    不是 `candidate` 有没有值：`edit`/`expand` 的候选位一开始就是用户带来的原树，
    拿它当判据会把"一棵都没长"的失败说成"交付了半棵树"。
    """
    ctx = RunContext(request=request, candidate=candidate)
    if produced:
        ctx.publish_tree(candidate if candidate is not None else _tree())
    return ctx


def _completion(calls: list[ToolCall] | None = None, finish: str = "stop") -> Completion:
    calls = calls or []
    message: dict[str, Any] = {"role": "assistant", "content": "好的"}
    if calls:
        message["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in calls
        ]
    return Completion(message=message, tool_calls=calls, finish_reason=finish,
                      usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


class ScriptedProvider:
    """按脚本依次返回预设结果；用完了再多调一次就报错。"""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls = 0

    async def complete_tools(self, messages, tools, *, budget):
        self.calls += 1
        if not self._script:
            raise AssertionError("脚本已用完，循环比预期多调了一次模型")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class QuestionArgs(RagArgs):
    """借用 `RagArgs` 的形状（`question` 字段），让测试读起来就是那句话。"""


def _rag_registry(*, readonly: bool = True) -> ToolRegistry:
    """一个只记账的假 `rag_query`。

    判据（工具名 + 规范化参数）与工具实现无关，用假工具是为了不把检索栈
    （BM25、向量缓存、知识库开关）拖进"重复判定"这条用例。
    """
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="rag_query", description="测试用的检索", args_model=QuestionArgs,
        fn=lambda args, ctx: ToolResult(
            ok=True, content=f"命中：{args.question}", data={"question": args.question}),
        readonly=readonly,
    ))
    return registry


def _echo_registry() -> ToolRegistry:
    """**不改树**的工具：参数每次不同、永远返回 ok=True。

    专门用来隔离防线二——防线一被"参数不同"绕开了，剩下的变量只有"树变没变"。
    """
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="echo", description="不改树的工具", args_model=QuestionArgs,
        fn=lambda args, ctx: ToolResult(ok=True, content=f"echo:{args.question}"),
    ))
    return registry


def _drain_loop(request: AgentRunRequest, ctx: RunContext, provider,
                registry: ToolRegistry) -> list:
    """直接驱动 `_loop`：这几个用例要看 `ctx` 上的状态（诊断、revision）。"""

    async def drain():
        return [event async for event in
                _loop(request, ctx, provider=provider, tools=registry)]

    return asyncio.run(drain())


def _stages(events: list) -> list[str]:
    return [event.stage for event in events if event.type == "stage"]


def _ends(events: list) -> list:
    return [event for event in events if event.type == "tool_end"]


def _rounds(calls: list[ToolCall], *, tail: bool = True) -> list[Completion]:
    """一次工具调用一轮的脚本，可带最后的收尾轮。"""
    script = [_completion([call], finish="tool_calls") for call in calls]
    if tail:
        script.append(_completion())
    return script


# ---------------------------------------------------------------- 1. 预算


def test_default_tool_budget_is_fifty_with_a_higher_ceiling():
    """默认 50、上界 120：两个数一起钉住，改一个不动另一个要立刻红。"""
    assert RunBudget().max_tool_calls == 50
    assert MAX_TOOL_CALLS_LIMIT == 120
    assert RunBudget(max_tool_calls=120).max_tool_calls == 120
    with pytest.raises(ValidationError):
        RunBudget(max_tool_calls=121)


def test_old_request_without_the_field_still_works():
    """老客户端不带 `budget`、或带着旧值 8：都要照旧收。

    这是"提额度"不能顺手改坏的那条路——前端与脚本里还有一堆
    `{"budget": {"max_tool_calls": 8}}`，把它们变成 422 等于一次静默的接口破坏。
    """
    without = AgentRunRequest.model_validate({"goal": "做个导图"})
    assert without.budget.max_tool_calls == 50

    legacy = AgentRunRequest.model_validate(
        {"goal": "做个导图", "budget": {"max_tool_calls": 8}})
    assert legacy.budget.max_tool_calls == 8


def test_tool_budget_stays_under_the_model_budget():
    """工具额度必须小于模型额度——一次工具调用要花一次模型往返。

    50 次工具 ≈ 50 次 provider 请求（一次 completion 里并发发多个 tool_call 时才
    会少几次），默认 `max_model_calls` 是 100，所以先撞到的永远是工具上限，
    而不是"模型调用次数已用完"。反过来（工具额度 ≥ 模型额度）时，用户为工具设的
    那个上限永远不会生效，两道闸里有一道是假的。
    """
    budget = RunBudget()
    assert budget.max_tool_calls < budget.max_model_calls


# ---------------------------------------------------------------- 2. 重复调用


def test_duplicate_anchor_only_looks_at_the_unbroken_tail():
    """判据是"连续"：中间插过一次别的调用就放行。

    `A, B, A` 是模型正常的"先查、再改、回来复核"，把它判成重复等于让模型用旧
    结果编。窗口 `REPEAT_CALL_WINDOW` 只限制往回扫多远。
    """
    request = _request()
    ctx = _ctx(request)

    ctx.record_call("c0", "A")
    assert _duplicate_anchor(ctx, "A") == "c0"

    ctx.record_call("c1", "B")
    assert _duplicate_anchor(ctx, "A") is None   # 中间插入了别的调用 → 放行
    assert _duplicate_anchor(ctx, "B") == "c1"

    # 连续三次同一指纹：锚点必须是最早那次，否则模型会以为"上一次重复被拒了，
    # 那我再试一次"。
    tail = _ctx(request)
    tail.record_call("x0", "A")
    assert _duplicate_anchor(tail, "A") == "x0"
    assert REPEAT_CALL_WINDOW >= 2


def test_same_tool_same_arguments_twice_is_rejected():
    """同一个 `rag_query(question="X")` 连续两次 → 第二次 `duplicate_call`。

    只读工具的缓存也在这里让位：缓存命中会返回 `ok=True` 的旧结果，模型读到的
    就是"又查成功了"，它根本没有机会知道自己在重复。
    """
    call = ToolCall(call_id="q1", name="rag_query", arguments={"question": "材料里讲了什么"})
    again = ToolCall(call_id="q2", name="rag_query", arguments={"question": "材料里讲了什么"})
    request = _request()
    events = _drain_loop(request, _ctx(request), ScriptedProvider(_rounds([call, again])),
                         _rag_registry())

    ends = _ends(events)
    assert ends[0].ok is True
    assert ends[1].ok is False
    assert ends[1].result_summary["code"] == "duplicate_call"
    assert ends[1].result_summary["duplicate_of"] == "q1"
    assert events[-1].type == "done"


def test_asking_again_after_a_different_call_is_allowed():
    """`A, B, A`：第三次真的执行（用户可能在这中间改了材料）。"""
    first = ToolCall(call_id="q1", name="rag_query", arguments={"question": "同一个问题"})
    other = ToolCall(call_id="q2", name="rag_query", arguments={"question": "另一个问题"})
    third = ToolCall(call_id="q3", name="rag_query", arguments={"question": "同一个问题"})
    assert first.arguments == third.arguments != other.arguments
    request = _request()
    events = _drain_loop(
        request, _ctx(request),
        ScriptedProvider([_completion([first, other, third], finish="tool_calls"),
                          _completion()]),
        _rag_registry(),
    )

    assert [end.ok for end in _ends(events)] == [True, True, True]


def test_duplicate_call_does_not_count_as_a_failed_repair():
    """`duplicate_call` 不计入 `max_repairs`。

    它的语义是"模型在原地打转"（由防线一拒绝、防线二计数），不是"参数写错了、
    改不过来"。计入的直接后果：默认 `max_repairs=1` 下，模型重复两次就掐死整个
    run——一句可自我纠正的提示变成了终止条件。
    """
    calls = [ToolCall(call_id=f"q{n}", name="rag_query", arguments={"question": "同一个问题"})
             for n in range(3)]
    request = _request(budget=RunBudget(max_repairs=0))
    events = _drain_loop(request, _ctx(request), ScriptedProvider(_rounds(calls)),
                         _rag_registry())

    assert [end.result_summary.get("code") for end in _ends(events)] == [
        None, "duplicate_call", "duplicate_call",
    ]
    # 两次重复也没有把 run 判死
    assert events[-1].type == "done"


# ---------------------------------------------------------------- 3. 无进展


def _varying_calls(count: int) -> list[ToolCall]:
    """每次参数都不同的调用：绕开防线一，只留"树变没变"这一个变量。"""
    return [ToolCall(call_id=f"c{index}", name="echo", arguments={"question": f"第{index}次"})
            for index in range(count)]


def test_no_progress_warns_then_stops():
    """连续不改树 → 第 8 次记 `NO_PROGRESS`，第 16 次中止。"""
    request = _request()
    ctx = _ctx(request, candidate=_tree(), produced=True)
    events = _drain_loop(
        request, ctx, ScriptedProvider(_rounds(_varying_calls(NO_PROGRESS_ABORT))),
        _echo_registry(),
    )

    assert "no_progress" in _stages(events)
    # 这一轮真的产出过树，所以中止帧是 Done（成果保留），不是 Error
    assert events[-1].type == "done"
    assert len(_ends(events)) == NO_PROGRESS_ABORT
    assert [item.code for item in ctx.loop_diagnostics] == ["NO_PROGRESS"]
    assert "连续" in ctx.loop_diagnostics[0].message
    # 每一条 tool_start 都有配对的 tool_end：中止不能留下一行"执行中"
    starts = [event.call_id for event in events if event.type == "tool_start"]
    assert starts == [end.call_id for end in _ends(events)]


def test_no_progress_without_a_candidate_ends_with_an_error():
    """一棵树都没长出来时，无进展中止要报错而不是假装完成。"""
    request = _request()
    ctx = _ctx(request)
    events = _drain_loop(
        request, ctx, ScriptedProvider(_rounds(_varying_calls(NO_PROGRESS_ABORT))),
        _echo_registry(),
    )

    assert events[-1].type == "error"
    assert events[-1].code == "no_progress"
    assert events[-1].limit == "no_progress"


def test_warning_lands_at_the_warn_threshold_not_before():
    """警告的时点：第 8 次之前一次都不许出现。"""
    request = _request()
    ctx = _ctx(request, candidate=_tree())
    events = _drain_loop(
        request, ctx, ScriptedProvider(_rounds(_varying_calls(NO_PROGRESS_WARN - 1))),
        _echo_registry(),
    )

    assert "no_progress" not in _stages(events)
    assert ctx.loop_diagnostics == []
    assert events[-1].type == "done"


def test_tree_signature_sees_label_changes_not_just_node_counts():
    """指纹要认得出"节点数没变但 label 变了"。

    这正是"反复 `update_node` 成同一个值"的反面：判据只看节点数的话，一次真正
    的改写与一次空转就长得一模一样，防线二要么全放过要么全误伤。
    """
    request = _request()
    baseline = _tree_signature(_ctx(request, candidate=_tree()))

    changed = _tree()
    changed.root.children[0].label = "甲（改过）"
    assert _tree_signature(_ctx(request, candidate=changed)) != baseline
    assert _tree_signature(_ctx(request, candidate=_tree())) == baseline
    assert _tree_signature(_ctx(request)) == ""


# ---------------------------------------------------------------- 4. 真的能用满


def test_thirty_real_edit_calls_run_to_completion():
    """30 次真实 `add_node`（每次加一个节点）→ 不撞任何上限。

    这是"提到 50 之后用户真的能用"的正面证据：30 次工具调用 + 31 次模型往返，
    双双留在默认预算（50 / 100）之内，防线一与防线二都不该被触发。
    """
    snapshot = _tree()
    request = _request(intent="edit", snapshot=snapshot)
    ctx = _ctx(request, candidate=snapshot)
    calls = [
        ToolCall(call_id=f"add-{index}", name="add_node",
                 arguments={"parent_id": "root", "label": f"要点 {index}"})
        for index in range(30)
    ]
    provider = ScriptedProvider(_rounds(calls))
    events = _drain_loop(request, ctx, provider, build_registry("edit", has_library=False))

    assert events[-1].type == "done"
    assert "no_progress" not in _stages(events)
    assert all(end.ok for end in _ends(events))
    assert ctx.revision == 30
    assert _count_nodes(ctx.candidate.root) == NODE_COUNT + 30
    # 31 次模型往返：每个工具各一轮，外加收尾那一轮
    assert provider.calls == 31


def test_expanding_a_branch_counts_as_progress():
    """`node_expand` 不碰 `ctx.revision`，防线二不能因此把它当成空转。

    判据用整树指纹而不是 revision，就是为这条路径：展开一整层子节点时
    revision 稳在 0，用它当判据的话"连续展开 8 次"会被误判成无进展。
    """
    request = _request()
    ctx = _ctx(request, candidate=_tree())
    after = _tree()
    after.root.children[1].children.append(Node(id="b2", label="乙二"))
    ctx.candidate = after

    assert ctx.revision == 0
    assert _tree_signature(ctx) != _tree_signature(_ctx(request, candidate=_tree()))


# ---------------------------------------------------------------- 5. 墙钟


def test_seconds_timeout_message_carries_the_progress():
    """时间上限那一句必须带"走到哪了"。

    600 秒是只在长循环上才撞到的上限，撞上它的那一刻恰恰最需要解释：用户等了
    十分钟，只看到"这次运行超过了时间上限"，他无从判断"我的图改完没有"。
    """
    request = _request(snapshot=_tree())
    ctx = _ctx(request, candidate=_tree())
    message = _budget_message(ctx, "max_seconds", 37)

    assert "已执行 37 次工具调用" in message
    assert f"树上是 {NODE_COUNT} 个节点" in message
    assert "这之前的改动都已进树里" in message


def test_count_budgets_keep_their_short_message():
    """次数上限那两条不加进度：停在某一轮开头，内容与上一帧 tool_end 一致。"""
    ctx = _ctx(_request(), candidate=_tree())
    assert _budget_message(ctx, "max_tool_calls", 50) == "工具调用次数已用完"
    assert _budget_message(ctx, "max_model_calls", 100) == "模型调用次数已用完"


def test_timed_out_tool_reports_the_same_sentence_as_the_loop(caplog):
    """工具超时与循环判停必须说同一句话，且都带进度。

    两处各写一份文案的话，同一件事在日志、`tool_end` 与收尾帧里会长得不一样，
    而排查的人只能靠它们互相对上。

    这里带着候选超时，所以收尾帧是 `done`（标 `partial`）而不是 `error`——树的
    交出口径统一之后，"跑到一半停了"不再等于"什么都没拿到"。那句解释从
    `error.message` 移到了 `done.partial_reason`，仍然是**逐字相同的一句**。
    """
    async def slow(args, ctx):
        # 先真的长出一棵树再卡住：收尾判据是"这一轮产出过"，光有用户带来的
        # 快照不算交付，那样超时会被判成 error 而不是 done。
        ctx.publish_tree(_tree())
        await asyncio.sleep(2)
        return ToolResult(ok=True, content="太慢了")

    request = _request(budget=RunBudget(max_seconds=0.3), snapshot=_tree())
    ctx = _ctx(request, candidate=_tree())
    registry = ToolRegistry()
    registry.register(ToolSpec(name="echo", description="慢工具", args_model=QuestionArgs,
                               fn=slow))
    call = ToolCall(call_id="slow-1", name="echo", arguments={"question": "x"})
    events = _drain_loop(request, ctx, ScriptedProvider(_rounds([call], tail=False)),
                         registry)

    end = _ends(events)[0]
    assert end.result_summary["code"] == "tool_timeout"
    assert "已执行 1 次工具调用" in end.result_summary["message"]
    last = events[-1]
    assert last.type == "done", "树已经在手上了，超时不该把它报成失败"
    assert last.content is not None
    assert last.partial is True
    assert last.partial_reason == end.result_summary["message"]
    assert caplog.text.count("tool_timeout") >= 1


def test_seconds_ceiling_is_unchanged():
    """`max_seconds` 这次**没有**放宽：600 秒仍是默认的物理边界。

    调大它会让用户等更久，而"50 次工具调用能不能跑完"的瓶颈是模型往返延迟，
    不是这个数：`edit` 意图下一次细粒度工具本身是内存操作（毫秒级），一次往返
    5–20 秒时 50 次大约 250–1000 秒——撞线是**可能**的。撞线时的解释比放宽数字
    更有用，所以改成在诊断里说清"用掉多少、做到哪、之后没执行"。
    """
    assert RunBudget().max_seconds == 600.0
    with pytest.raises(ValidationError):
        RunBudget(max_seconds=3601)


# ---------------------------------------------------------------- 6. 不回归


def test_create_intent_still_gets_no_rag_query():
    """`create` 仍然拿不到 `rag_query`，但理由换了：是**重复**，不是预算。

    分片生成在工具内部自己检索（骨架之后每支一次、限额补查再各一次），而
    `mindmap_generate_deep` 的参数里没有"把我刚查到的东西用上"这个入口——
    模型查完只能自己在文字里复述，而每一支检索用的是**那支的标题**。
    查了→扔掉，白烧一次模型往返。预算从 8 到 50 不改变这件事。
    """
    assert build_registry("create_deep", has_sources=True, has_library=True).names() == [
        "mindmap_generate_deep",
    ]
    assert "rag_query" in build_registry("edit", has_sources=True,
                                         has_library=False).names()
    assert "rag_query" in build_registry("expand", has_sources=True,
                                         has_library=False).names()


def test_edit_intent_keeps_all_eight_tools():
    """`edit` 的八个写工具一个都没少——提额度不是为了删工具。"""
    names = build_registry("edit", has_library=False).names()
    assert set(names) == {
        "add_node", "update_node", "remove_node", "move_node",
        "add_edge", "remove_edge", "mindmap_fill_details", "mindmap_edit",
    }
    assert len(names) == 8


def test_expand_intent_keeps_its_tools():
    """`expand` 只有展开 + 整图体检（无来源时）。

    `audit_tree` 进这一组不是"多给一个工具"：那条路上模型是逐个 `node_expand`
    展开的，`mindmap_generate_deep` 内部那套验收（`_audit_branches` / `partial`）
    覆盖不到它，而大纲只列前 200 个节点——"还差哪几支"只能由代码数。
    """
    assert build_registry("expand", has_library=False).names() == [
        "audit_tree", "node_expand"]
