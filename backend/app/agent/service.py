"""AI 代理（F02 / F18）。

位置：用户给一个目标（"设计一款智能家居产品"），先拆成阶段，再逐阶段生成分支。

状态：**骨架**。plan() 返回固定的三段结构，不含模型调用；
真正实现要做的是：目标 → 阶段划分 → 每阶段调用 run() → 合并成一棵树。

设计约束：
  - 每一步都要记录来源与依据，不只是结果
  - 代理产出的是候选，用户确认后才写进导图（与 F15/F16 同一条原则）
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.contracts import (
    AgentEvent,
    AgentRunRequest,
    DoneEvent,
    ErrorEvent,
    NodeGrowEvent,
    OpsEvent,
    RunBudget,
    RunContext,
    SnapshotEvent,
    StageEvent,
    ToolCall,
    ToolProvider,
    ToolResult,
    ToolStartEvent,
    ToolEndEvent,
)
from app.agent.provider import default_provider
from app.agent.tools import ToolRegistry, build_registry
from app.contracts.mindmap import MindMapContent
from app.generation.service import (
    SOURCE_MODE_RAG,
    GenerationError,
    _demo_content,
    source_mode,
)

DEFAULT_STAGES = ["目标澄清", "要点拆解", "分支展开", "风险与验证"]
logger = logging.getLogger(__name__)

# 校验异常可能带上模型输出，日志只需足够定位的摘要，不能随材料或树的大小增长。
MAX_LOG_MESSAGE_CHARS = 500


class AgentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str
    stages: list[str] = Field(default_factory=list)
    implemented: bool = False


class AgentTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=500)


def plan(goal: str) -> AgentPlan:
    """把目标拆成阶段。

    TODO(F02)：用模型做阶段划分，并为每个阶段生成"需要哪些输入"，
    再逐阶段调用本文件的 run()，最后把各阶段的候选合并成一棵树。
    现在返回固定阶段，只用来固定接口形状与前端交互顺序。
    """
    return AgentPlan(goal=goal, stages=list(DEFAULT_STAGES), implemented=False)


# ---------------------------------------------------------------- 多轮工具循环

_SYSTEM_BASE = (
    "你在操作一张思维导图，可以通过工具读写它。"
    "只能调用下面列出的工具；需要改图时用工具，不要只在文字里描述。"
)

# 按意图分叉指令，不是一套 system 打天下。实测教训：system 里只要写了
# "需要外部信息必须先查"，模型就会对"给考研数学复习计划生成导图"这种
# 完全不需要检索的请求也先调一次检索（10 次里 10 次都这样）。
_INTENT_RULES = {
    "create": "这次是新建导图。直接生成，不要先去检索资料。",
    "expand": "这次是展开某个已选中的节点。只补充子节点，不要重写整张图。",
    "edit": "这次是精确修改已有导图。必须按节点 id 改，禁止整张重新生成。",
}

# `rag` 形态下的 create 指令，与上面那条**语义相反**，而且必须是相反的：
# 正文不在上下文里，`don't retrieve` 等于让模型凭标题编。上面那条实测结论
# （"别让它先查"）成立于 `full` 形态——那时正文就在 prompt 里，先查纯属浪费；
# 形态一换，前提没了，结论就得跟着翻。
#
# 只覆盖 `create`：`expand` / `edit` 的指令里没有"不要检索"这句话，也就没有
# 矛盾要消除，多写一句反而会诱导它们在本不需要材料时也去查。
_RAG_INTENT_RULES = {
    "create": "这次是新建导图。材料正文不在上下文里，先调用 rag_query 检索，"
              "再按检索到的片段生成；检索不到的部分如实留空，不要编造。",
}


def outline_text(content: MindMapContent | None, limit: int = 200) -> str:
    """把树压成带 id 的大纲，让模型能引用具体节点。

    没有 id 的大纲模型就只能靠文字猜"改哪一个"，而文字是会被它自己改写的。
    """
    if content is None:
        return "（当前没有导图）"
    lines: list[str] = []

    def walk(node, depth: int) -> None:
        if len(lines) >= limit:
            return
        lines.append("  " * depth + f"[{node.id}] {node.label}")
        for child in node.children:
            walk(child, depth + 1)

    walk(content.root, 0)
    if len(lines) >= limit:
        lines.append(f"……（只列前 {limit} 个节点）")
    return "\n".join(lines)


def _intent_rule(request: AgentRunRequest) -> str:
    """这次意图该配哪条指令。`rag` 形态只覆盖有来源的 `create`（理由见上）。

    没有来源时不改：`rag` 与 `full` 在"这次根本没带材料"时行为完全一样，
    这时换成检索优先的指令，只会让模型去查一个空列表。
    """
    if request.sources and source_mode() == SOURCE_MODE_RAG:
        override = _RAG_INTENT_RULES.get(request.intent)
        if override is not None:
            return override
    return _INTENT_RULES.get(request.intent, "")


def _system_text(request: AgentRunRequest, content: MindMapContent | None) -> str:
    return "\n".join([
        _SYSTEM_BASE,
        _intent_rule(request),
        "当前导图：",
        outline_text(content),
    ]).strip()


def _initial_messages(request: AgentRunRequest) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": _system_text(request, request.snapshot)},
        {"role": "user", "content": request.goal},
    ]


def _refresh_outline(messages: list[dict[str, Any]], ctx: RunContext) -> None:
    """每轮把第一条 system 消息换成最新的树。

    不刷新的话，模型看到的大纲始终是请求开始时的快照：它刚生成的那些节点
    根本没有 id 可引用，于是下一轮只能整张重来——那正是这次改造要消灭的行为。
    原地替换而不是追加，是因为追加会让消息历史随轮数线性增长，几轮之后
    光大纲就把上下文吃满了。
    """
    messages[0] = {"role": "system", "content": _system_text(ctx.request, ctx.candidate)}


def _dedupe_key(call: ToolCall) -> str:
    """同工具同参数的指纹。

    规范化用 sort_keys：模型两次给的参数键序可能不同，但那显然是同一次调用。
    """
    return call.name + "\0" + json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str)


def _args_summary(call: ToolCall, width: int = 80) -> dict[str, Any]:
    """事件流里只放参数摘要：完整参数可能带整份来源正文。"""
    summary: dict[str, Any] = {}
    for key, value in list(call.arguments.items())[:6]:
        text = value if isinstance(value, (int, float, bool)) else str(value)
        if isinstance(text, str) and len(text) > width:
            text = text[:width] + "…"
        summary[key] = text
    return summary


def _merge_usage(total: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
    merged = dict(total)
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if isinstance(usage.get(key), int):
            merged[key] = int(merged.get(key, 0)) + int(usage[key])
    return merged


def _remaining_seconds(budget: RunBudget, started: float) -> float:
    return budget.max_seconds - (time.monotonic() - started)


def _budget_limit(budget: RunBudget, tool_calls: int, started: float) -> str | None:
    """时间与工具次数两种预算的判停。

    **模型调用额度不在这里判**：它由 `RunContext.reserve_model_call()` 统一裁决。
    外层轮次与工具内部的 `chat_async` 必须共用同一个账本，而这里只看得见外层，
    在这里判就成了第二份真相——预算 1 实测发出 4 次请求，正是这么来的。
    """
    if tool_calls >= budget.max_tool_calls:
        return "max_tool_calls"
    if _remaining_seconds(budget, started) <= 0:
        return "max_seconds"
    return None


_BUDGET_HINT = {
    "max_model_calls": "模型调用次数已用完",
    "max_tool_calls": "工具调用次数已用完",
    "max_seconds": "这次运行超过了时间上限",
}


def _log_failure(
    ctx: RunContext,
    tool_calls: int,
    *,
    code: str,
    message: str,
    call: ToolCall | None = None,
    level: int = logging.WARNING,
) -> None:
    # 不记录参数、result.data 或异常堆栈：它们可能夹带整份正文或整棵树。
    # JSON 转义换行，防止多行错误把一次失败拆成难以关联的多条日志。
    logger.log(level, "agent 运行异常 %s", json.dumps({
        "call_id": call.call_id if call else None,
        "tool": call.name if call else None,
        "code": code,
        "message": message[:MAX_LOG_MESSAGE_CHARS],
        "model_calls_used": ctx.model_calls_used,
        "tool_calls_used": tool_calls,
    }, ensure_ascii=False))


def _result_summary(result: ToolResult) -> dict[str, Any]:
    if result.ok:
        return result.data
    # 注册表的旧错误只把原因写在 content 中；补给事件流后界面与日志才能说同一件事。
    summary = dict(result.data)
    for key, fallback in (
        ("code", "tool_failed"),
        ("message", result.content or "工具未返回具体失败原因。"),
    ):
        if not isinstance(summary.get(key), str) or not summary[key].strip():
            summary[key] = fallback
    return summary


@dataclass
class _ToolOutcome:
    """一次工具执行的去向。

    放进一个容器里是因为 `_run_tool` 是 async generator：它要一边 yield 事件帧，
    一边把结果交回调用方，而生成器没法 `return` 一个值。
    """

    result: ToolResult | None = None
    error: BaseException | None = None
    timed_out: bool = False


def _budget_stop(
    ctx: RunContext,
    seq: "itertools.count[int]",
    usage_total: dict[str, Any],
    limit: str,
    tool_calls: int,
    call: ToolCall | None = None,
) -> DoneEvent | ErrorEvent:
    """预算用尽时的收尾帧：已经有候选就当作完成，没有才算失败。

    深层生成会在额度不足时提前停止展开并记一条诊断，所以走到这里时候选通常
    已经是一棵能用的树。这时再报「模型调用次数已用完」，用户看到的是候选和
    一条错误同时出现，不知道该不该用那份候选——预算的作用是限制继续花钱，
    不是把已经产出的结果报成失败。

    真的一份候选都没有（第一轮就没额度、工具失败前没产出）时仍然报错，
    那种情况下用户确实什么都没拿到。
    """
    _log_failure(
        ctx, tool_calls, call=call, code="budget_exhausted", message=_BUDGET_HINT[limit],
    )
    if ctx.candidate is not None:
        return DoneEvent(seq=next(seq), content=ctx.candidate, usage=usage_total,
                         mode="model")
    return ErrorEvent(
        seq=next(seq), code="budget_exhausted",
        message=_BUDGET_HINT[limit], limit=limit,
    )


def _node_grow(payload: dict[str, Any], seq: "itertools.count[int]") -> NodeGrowEvent | None:
    """工具上报的字典 → 带 seq 的帧；认不出来就返回 None 丢掉。

    预览是附加信息，一帧坏掉不能打断正在跑的工具，所以这里不抛异常。取号放在
    字段校验之后：校验失败的帧不占号，前端"seq 连续"这条判断才不会被一串缺号
    误导成丢帧。
    """
    if payload.get("type") != "node_grow":
        return None
    path = payload.get("path")
    label = payload.get("label")
    # path 可以是 None（根帧），但给了就必须是索引数组
    if (path is not None and not isinstance(path, list)) or not isinstance(label, str):
        return None
    flag = payload.get("expandable")
    try:
        return NodeGrowEvent(
            seq=next(seq), path=path, label=label,
            expandable=flag if isinstance(flag, bool) else None,
        )
    except ValidationError:
        return None


async def _run_tool(
    call: ToolCall,
    ctx: RunContext,
    registry: ToolRegistry,
    *,
    seq: "itertools.count[int]",
    timeout: float,
    outcome: _ToolOutcome,
) -> AsyncIterator[AgentEvent]:
    """执行一次工具，同时把工具内部的增量转成事件推出去。

    `run()` 是 async generator：它 `await` 工具执行的这段时间里**一帧都发不出去**。
    深层生成要发十几次模型调用，全憋在里面就是十几秒空屏，所以工具内部的上报
    只能走队列——工具往队列里放，这里一边等工具一边把队列里的东西转成帧。

    结果与异常也塞进同一条队列，好处是只有一个等待点：工具抛错时已经收到的预览
    帧不会丢（它们排在异常之前），超时也只需要取消这一个任务。超时语义与原先
    `wait_for` 包住整次执行一致：预算按墙钟算，产出预览帧的时间也算在里面。
    """
    updates: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def emit(payload: dict[str, Any]) -> None:
        updates.put_nowait(("emit", payload))

    async def worker() -> None:
        try:
            result = await _execute_once(call, ctx, registry)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 原样交回主循环上抛，别在这里改类型
            updates.put_nowait(("error", exc))
            return
        updates.put_nowait(("result", result))

    ctx.emit = emit
    task = asyncio.create_task(worker())
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(
                    updates.get(), timeout=max(0.0, deadline - time.monotonic()),
                )
            except (asyncio.TimeoutError, TimeoutError):
                outcome.timed_out = True
                return
            if kind == "emit":
                event = _node_grow(payload, seq)
                if event is not None:
                    yield event
                continue
            if kind == "error":
                outcome.error = payload
            else:
                outcome.result = payload
            return
    finally:
        # 通道只在这段执行期间有效：留着它，下一次工具的增量就会推进一个已经
        # 没人读的队列，控制器看见的是一串永远不出现的帧。
        ctx.emit = None
        task.cancel()
        # 已结束的任务再 cancel 是空操作；这里等它收尾，避免留下
        # "Task was destroyed but it is pending" 和一个还在跑的模型请求。
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _execute_once(call: ToolCall, ctx: RunContext, registry: ToolRegistry) -> ToolResult:
    """带两级幂等的执行。

    call_id 幂等是协议要求的：模型偶尔会把同一个 tool_call 重复发一遍。
    参数级去重是防"同一轮里两个不同 id 但参数完全一样"的调用——实测模型
    并行发起两个 node_expand(n1) 是会发生的事，执行两次就会加两遍子节点。
    """
    key = _dedupe_key(call)

    cached = ctx.executed.get(call.call_id)
    if cached is not None:
        cached_key, cached_result = cached
        if cached_key != key:
            # 同一个 call_id 换了参数是协议违规。返回旧结果会让模型以为新参数
            # 生效了，而实际上没有任何改动发生。
            return ToolResult(
                ok=False,
                content=f"call_id {call.call_id} 已经用于另一次参数不同的调用，请换一个新的 call_id 重试。",
                data={"code": "call_id_conflict"},
            )
        return cached_result

    spec = registry.get(call.name)
    readonly = spec is not None and spec.readonly
    if readonly:
        # 只读工具按参数去重是安全的：同样的查询必然得到同样的结果。
        # 写操作不能这样——即使参数一字不差，第二次执行时树已经变了，
        # 返回上次的结果等于谎报成功（实测：改成 X、Y、再改回 X，第三次被
        # 缓存吃掉，模型以为改成功了，实际停在 Y）。
        hit = ctx.dedupe.get(key)
        if hit is not None:
            ctx.executed[call.call_id] = (key, hit)
            return hit

    result = await registry.execute(call, ctx)
    ctx.executed[call.call_id] = (key, result)
    if readonly:
        ctx.dedupe[key] = result
    return result


async def run(
    request: AgentRunRequest,
    *,
    provider: ToolProvider | None = None,
    tools: ToolRegistry | None = None,
) -> AsyncIterator[AgentEvent]:
    """跑一次多轮工具循环，边跑边发事件。

    终止只有四种：模型以 stop 收尾（done）、预算耗尽（error）、上游失败（error）、
    工具累计失败超过 `max_repairs`（error）。
    不存在"静默截断后假装成功"——那会让前端拿到一棵看起来正常、实际缺了一半
    的树，比直接报错难排查得多。

    预算记账只有一个账本（`RunContext.model_calls_used`）：外层 provider 调用与
    工具内部的 `chat_async` 调用共用它，且都在**请求发出之前**占位。分两处判、
    两处记，就会出现"判的时候以为还有额度"的超支。
    """
    # 演示模式：机器上没有模型密钥时（评审演示、新人上手）走 agent 也要能出一张图，
    # 否则这里是 `chat_async` 的第一句 `GenerationError`，一张图都生成不出来。
    #
    # 判断放在这个函数的入口，而不是 `_generate` / `_generate_deep`：那两个是工具层
    # 各自的入口，写两遍就是同一个判断两处真相，而且外层循环会先花掉一次真正的
    # 模型调用才轮到工具。
    #
    # 只认显式的 `demo`（`.env.example` 就是这么发的）。缺省值**不**取 demo：旧路径
    # 那套「没配置就走 demo」的默认照搬到 agent 上，等于把默认路径静默降级成占位图，
    # 而 agent 正是要成为默认的那条路。
    if os.getenv("MINDMAP_LLM_MODE", "").strip().lower() == "demo":
        # 占位树复用旧路径的 `_demo_content`：同一份占位结构只该有一个定义，
        # 两处各写一份，改文案时必然只改一处。
        demo = _demo_content(request.goal)
        demo_seq = itertools.count(1)
        yield StageEvent(seq=next(demo_seq), stage="demo", detail="演示模式，不调用模型")
        yield SnapshotEvent(seq=next(demo_seq), content=demo)
        yield DoneEvent(seq=next(demo_seq), content=demo, usage={}, mode="demo")
        return

    active_provider = provider or default_provider()
    # 意图决定模型看得到哪些工具：expand 下根本没有 mindmap_edit，
    # 它也就不会去试"删掉另一个分支"。
    registry = tools if tools is not None else build_registry(
        request.intent, has_sources=bool(request.sources),
    )
    budget = request.budget
    ctx = RunContext(
        request=request,
        candidate=request.snapshot,
        sources_by_id={source.id: source for source in request.sources},
    )
    messages = _initial_messages(request)
    seq = itertools.count(1)
    started = time.monotonic()
    tool_calls = 0
    failures = 0
    usage_total: dict[str, Any] = {}
    last_call: ToolCall | None = None

    yield StageEvent(
        seq=next(seq),
        stage="准备",
        detail=f"{request.intent} 意图，{len(registry.names())} 个工具可用",
    )

    while True:
        limit = _budget_limit(budget, tool_calls, started)
        if limit is not None:
            yield _budget_stop(ctx, seq, usage_total, limit, tool_calls, last_call)
            return

        # 先占额度、再发请求。反过来做（调用成功后才记账）时，一旦工具内部
        # 也要调模型，额度会在工具执行期间被用掉而循环还不知道。
        if not ctx.reserve_model_call():
            yield _budget_stop(ctx, seq, usage_total, "max_model_calls", tool_calls, last_call)
            return

        # 每轮刷新大纲：模型必须看到上一轮产出节点的真实 id，否则它无法精确改
        _refresh_outline(messages, ctx)
        try:
            # 用剩余时间做超时，而不是让单次调用跑到 provider 自己的 60 秒上限：
            # 否则总时限形同虚设，一轮慢调用就能把预算耗尽而循环还在等。
            completion = await asyncio.wait_for(
                active_provider.complete_tools(messages, registry.specs(), budget=budget),
                timeout=max(0.1, _remaining_seconds(budget, started)),
            )
        except asyncio.CancelledError:
            # 必须原样抛出：吞掉它会让"用户点了取消"变成一次正常结束
            raise
        except (asyncio.TimeoutError, TimeoutError):
            yield _budget_stop(ctx, seq, usage_total, "max_seconds", tool_calls)
            return
        except GenerationError as exc:
            _log_failure(ctx, tool_calls, code="generation_failed", message=str(exc))
            yield ErrorEvent(seq=next(seq), code="generation_failed", message=str(exc))
            return
        except Exception as exc:  # noqa: BLE001 — 留下摘要后仍交给路由处理，不改变异常语义
            _log_failure(
                ctx, tool_calls, code="agent_failed",
                message=f"{type(exc).__name__}: {exc}", level=logging.ERROR,
            )
            raise

        messages.append(completion.message)
        usage_total = _merge_usage(usage_total, completion.usage)

        if not completion.tool_calls:
            if completion.finish_reason == "stop":
                yield DoneEvent(seq=next(seq), content=ctx.candidate, usage=usage_total,
                                mode="model")
            else:
                message = f"模型以 {completion.finish_reason or '未知原因'} 结束，且没有调用任何工具"
                _log_failure(ctx, tool_calls, code="unexpected_finish", message=message)
                yield ErrorEvent(
                    seq=next(seq), code="unexpected_finish",
                    message=message,
                )
            return

        for call in completion.tool_calls:
            if tool_calls >= budget.max_tool_calls:
                _log_failure(
                    ctx, tool_calls, call=call, code="budget_exhausted",
                    message=_BUDGET_HINT["max_tool_calls"],
                )
                yield ErrorEvent(
                    seq=next(seq), code="budget_exhausted",
                    message=_BUDGET_HINT["max_tool_calls"], limit="max_tool_calls",
                )
                return

            yield ToolStartEvent(
                seq=next(seq), call_id=call.call_id, name=call.name,
                args_summary=_args_summary(call),
            )
            # 已发起但超时或抛错的工具同样消耗一次调用，日志不能把它们记成零次。
            tool_calls += 1
            last_call = call
            ledger_before = ctx.model_calls_used
            extra_before = ctx.extra_model_calls
            had_candidate = ctx.candidate is not None
            begun = time.monotonic()
            outcome = _ToolOutcome()
            # 工具执行与它的增量上报并发进行：`_run_tool` 每收到一帧就 yield 出去，
            # 期间工具仍在跑（`node_grow` 就是靠这个在十几秒的生成里陆续上屏）。
            #
            # 用 `aclosing` 而不是裸 `async for`：客户端中途断开时 `run()` 会被关掉，
            # 不显式关这个子生成器，它的收尾（复位通道、取消工具任务）就要等垃圾
            # 回收才执行——那段时间工具里的模型请求还在跑、还在计费。
            async with contextlib.aclosing(_run_tool(
                call, ctx, registry, seq=seq,
                timeout=max(0.1, _remaining_seconds(budget, started)),
                outcome=outcome,
            )) as tool_events:
                async for event in tool_events:
                    yield event
            if outcome.timed_out:
                _log_failure(
                    ctx, tool_calls, call=call, code="tool_timeout",
                    message=_BUDGET_HINT["max_seconds"],
                )
                # 超时也是一次「工具结束了」。少了这一帧，事件序列就是
                # `stage → tool_start → error`，前端那行工具永远停在「执行中」——
                # `max_repairs` 判停那条路径修过同一个问题，超时这里当时漏了。
                yield ToolEndEvent(
                    seq=next(seq), call_id=call.call_id, name=call.name, ok=False,
                    result_summary={
                        "code": "tool_timeout",
                        "message": _BUDGET_HINT["max_seconds"],
                    },
                    latency_ms=int((time.monotonic() - begun) * 1000),
                )
                yield ErrorEvent(
                    seq=next(seq), code="budget_exhausted",
                    message=_BUDGET_HINT["max_seconds"], limit="max_seconds",
                )
                return
            if outcome.error is not None:
                # 工具内部抛异常时原样上抛，交给路由那个兜底把它变成一帧可读错误；
                # 在这里转成 ToolResult 等于替工具谎报"执行完了"。
                _log_failure(
                    ctx, tool_calls, call=call,
                    code="generation_failed" if isinstance(outcome.error, GenerationError) else "agent_failed",
                    message=f"{type(outcome.error).__name__}: {outcome.error}",
                    level=logging.ERROR,
                )
                raise outcome.error
            if outcome.result is None:  # pragma: no cover — 三种出路必命中一种
                _log_failure(
                    ctx, tool_calls, call=call, code="agent_failed",
                    message="工具执行既没有结果也没有异常", level=logging.ERROR,
                )
                raise RuntimeError("工具执行既没有结果也没有异常")
            result = outcome.result
            # 对账：工具有没有自己调了模型却没先占额度（新写的工具最容易漏这一步，
            # 漏一次就多一次免费调用）。只在账本这一轮没动过时按 extra 增量补记，
            # 避免与 reserve 的正常记账重复计数——宁可多记，不可漏记。
            if (ctx.extra_model_calls > extra_before
                    and ctx.model_calls_used == ledger_before):
                ctx.model_calls_used += ctx.extra_model_calls - extra_before

            summary = _result_summary(result)
            if not result.ok:
                _log_failure(
                    ctx, tool_calls, call=call, code=summary["code"], message=summary["message"],
                )

            # ToolEnd 必须在这个函数返回之前发出去，且要在失败判停**之前**：
            # 否则那一次 tool_start 永远等不到配对帧，前端会一直显示"执行中"。
            yield ToolEndEvent(
                seq=next(seq), call_id=call.call_id, name=call.name, ok=result.ok,
                result_summary=summary,
                latency_ms=int((time.monotonic() - begun) * 1000),
            )
            if not result.ok:
                # 模型给错参数是常态，它看到错误后下一轮通常能改对；但一直错下去
                # 就是在烧调用，超过容忍次数直接收尾。计的是累计失败数（不是连续），
                # 成功的工具调用不会把它清零。
                failures += 1
                if failures > budget.max_repairs:
                    message = (
                        f"工具执行已累计失败 {failures} 次（上限 {budget.max_repairs} 次），"
                        "继续重试没有意义，已停止。"
                    )
                    _log_failure(
                        ctx, tool_calls, call=call, code="too_many_failures", message=message,
                    )
                    yield ErrorEvent(
                        seq=next(seq), code="too_many_failures",
                        message=message,
                        limit="max_repairs",
                    )
                    return

            # 候选树从"没有"变成"有"（只可能是 mindmap_generate 成功）时补一帧
            # snapshot：前端立刻就能渲染候选内容，不必等到 done。
            # 位置选在 ToolEnd 之后、OpsEvent 之前：前端收到 tool_end 会先把工具
            # 结果落进状态，紧随其后的整树快照与它一致；若放到 OpsEvent 之后，
            # 同一批改动会先在增量里应用一次、再被整树覆盖一次。
            # content 一定是完整校验过的树：ctx.candidate 只由
            # validate_generated_content 与 apply_ops 的整树兜底写入，
            # 半成品进不来。
            if not had_candidate and ctx.candidate is not None:
                yield SnapshotEvent(seq=next(seq), content=ctx.candidate)

            # 工具若产生了 op 批次，单独发帧：前端据此增量应用，不必等整棵树。
            # 用 data 而不是解析 content，省掉一次 JSON 往返，也避免把回给模型的
            # 自然语言当成协议数据。
            for batch in result.data.get("batches") or []:
                yield OpsEvent(seq=next(seq), call_id=call.call_id, batch=batch)

            messages.append({
                "role": "tool",
                "tool_call_id": call.call_id,
                "content": result.content,
            })

