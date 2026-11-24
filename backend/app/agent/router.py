"""AI 代理路由。

`/tasks` 是早期留下的阶段规划桩，保留给旧调用方；`/runs/stream` 是真正的多轮
工具循环，以 SSE 推送进度与结果。

开流前后的错误必须走不同通道：请求体不合法、开关关闭这类问题在第一个字节发出
之前就要返回普通 HTTP 错误（422 / 503 / 409），前端能按状态码区分；一旦开始推送，
HTTP 状态码已经发出去了，后续只能以流内的 `error` 帧表达。

`/runs` 是这次新增的**读**入口（列表 / 单条 / 标记已保存）。没有它们，
`runs` 表就是一张"写了没人读"的死表——落库的结果永远没人捡起来。

注意这里说的"结果"不是"待确认的候选"：生成完成时前端已经把树装进画布了（画布标成
未保存），`runs.candidate` 只是那份结果的快照，用于关掉页面之后捡回来、以及将来做
生成历史。所以没有"应用/丢弃"这一对动作，只有"保存成功"这一个时间戳。
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, AsyncIterator, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from app.agent import runs, shapes
from app.agent.contracts import (
    AgentEvent,
    AgentRunRequest,
    DoneEvent,
    ErrorEvent,
    SnapshotEvent,
    SourcesEvent,
)
from app.agent.diagram_intent import apply as apply_diagram_intent
from app.agent.service import AgentPlan, AgentTaskRequest, plan, run
from app.agent.stream import DONE_FRAME, encode_event
from app.auth import identity as identity_module
from app.auth import owner as owner_module
from app.core.errors import AppError
from app.generation import quota
from app.generation.service import GenerationError, ModelNotConfigured
from app.knowledge.remember import remember_sources
from app.memory import summarize as memory_summarize
from app.settings import store as settings_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["AI 代理"])

# SSE 的三条响应头对三条开流路径都成立，收在一处：漏掉 `X-Accel-Buffering`
# 的那条路会在反向代理后面攒够一批才发，看起来像"卡了几十秒才出结果"。
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # 反向代理默认攒够一批才转发，那样 SSE 会退化成一次性响应
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

# 没带 run_id 的老客户端由服务端补一个。用 uuid4 的十六进制而不是 `run-` 前缀：
# 前者天然满足 `AgentRunRequest.run_id` 的字符集，不必为"服务端自己发的值"再放宽
# 一次校验。
_GENERATED_RUN_ID_BYTES = 16


@router.post("/tasks", response_model=AgentPlan)
def create_task(body: AgentTaskRequest):
    """目标驱动生成（F02）。当前只返回阶段骨架，不产生导图。"""
    return plan(body.goal)


def _agent_enabled() -> bool:
    """总开关。

    默认开启。旧生成通道已经删掉了，这条开关关掉就等于关掉 AI 能力：开流前
    直接返回 503（`agent_disabled`），前端拿不到任何替代入口。
    """
    return os.getenv("MINDMAP_AGENT_ENABLED", "1").strip() != "0"


def _validate(body: AgentRunRequest) -> None:
    """开流之前把自相矛盾的请求挡掉。

    这些约束放这里而不是 `AgentRunRequest` 的模型校验里：它们描述的是"这个意图
    和这份输入搭不搭"，属于调用方语义，不是字段本身不合法。
    """
    if body.intent == "create" and body.snapshot is not None:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_request", "message": "新建导图时不应带 snapshot"},
        )
    if body.intent != "create" and body.snapshot is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_request", "message": f"{body.intent} 必须带上当前导图快照"},
        )
    if body.intent == "expand" and len(body.selected_ids) != 1:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_request", "message": "展开节点时必须且只能选中一个节点"},
        )


def _request_summary(body: AgentRunRequest) -> dict[str, Any]:
    """落库的请求摘要。

    **只存来源的 id，不存正文**：`runs` 是运行身份的台账，不是材料仓库；正文一旦
    进这张表，删库之前它就一直在，而它本来只该活在这一次请求里。`goal` / `intent`
    存下来是因为读的那条路要用——提示条上写"上次的「XXX」生成完成了"，以及
    "查看"时按原来的意图决定这份候选算新作品还是改现有这张图。
    """
    return {
        "goal": body.goal,
        "intent": body.intent,
        "thinking_model": body.thinking_model,
        # 图型也落进摘要：这是"用户到底要的是流程图还是导图"唯一留下的持久痕迹。
        # 它不参与重放（重放的是候选树本身），但事后排查"我要的流程图怎么成了
        # 导图"时，没有这一行就只能靠猜。`None` 与 `"tree"` 在这里也必须分得开：
        # 前者是"用户让我自己判断"，后者是"用户明确要思维导图"。
        #
        # 这里记的是**解析之后**的值（`apply_diagram_intent` 在调用之前跑过），
        # 所以"用户打字说了鱼骨图"这件事在台账里是 `fishbone`，不是 `None`——
        # 否则最该被查的那一次恰恰查不出来。
        "diagram_type": body.diagram_type,
        # 这次真的生效了哪些偏好。它是偏好面板上"上次生效：…"的唯一数据源，
        # 也是"改一个值 → 生成一次 → 产物确实变了"这条验收链在库里的落点。
        #
        # 记在这里而不是另起一张表：它描述的是**这一次请求**，与 `goal` / `intent`
        # 同一个粒度。单独一张表只会多一条永远要 JOIN 才能回答"这次为什么不一样"。
        "prefs_applied": list(body.prefs_applied),
        "selected_ids": list(body.selected_ids),
        "source_ids": [source.id for source in body.sources],
    }


async def _replay(record: runs.RunRecord) -> AsyncIterator[str]:
    """把一个已经有结论的 run 原样"重放"成一条流。

    帧格式与真跑一模一样（`snapshot → done → [DONE]`）：同一个 run_id 再投一次时，
    客户端拿到的仍是那棵树与那个 `mode`，前端走的还是"落画布"那条路。

    这条路存在的意义是**不再调模型**：同一个 run_id 已经跑完过一次，再花一遍钱
    只为得到同一棵树，是这张表要消灭的第一件事。`mode` 也照原样回填——占位树
    被重放成 `mode=model` 就是界面替它撒谎。

    `model` / `reasoning` 同样回填（`runs.finish()` 落的那两列）。重放的价值是
    "同一个 run_id 拿到的东西与第一次一样"，缺了它们，用户重连之后看到的那条
    结果既不说用的哪个模型、也没有思考过程——"重放"就变成了"重放了一部分"。
    老行上这两列是 NULL，`DoneEvent` 里整条字段不存在，与"没说"同义。

    `done` 但没有结果的 run 同样重放（内容是空）：它在服务端已经是一次有结论的
    运行，重跑一次并不比"如实说没产出"更可能成功。用户真想重试时，前端每次点击
    都会生成新的 run_id，走的是另外一条路。

    `partial` / `partial_reason` 也照落库的值回填：重放若是"完整生成"，用户会以为
    上一轮真的一次跑完了——而他重连之前看到的明明是"部分完成"。
    """
    mode = record.mode if record.mode in ("demo", "model") else None
    seq = 0
    if record.candidate is not None:
        seq += 1
        yield encode_event(SnapshotEvent(seq=seq, content=record.candidate))
        seq += 1
        yield encode_event(DoneEvent(
            seq=seq, content=record.candidate, mode=mode,
            model=record.model, served_model=record.served_model,
            reasoning=record.reasoning or "",
            partial=record.partial, partial_reason=record.partial_reason))
    else:
        yield encode_event(DoneEvent(
            seq=1, content=None, mode=mode,
            model=record.model, served_model=record.served_model,
            reasoning=record.reasoning or "",
            partial=record.partial, partial_reason=record.partial_reason))
    yield DONE_FRAME


async def _frames(
    body: AgentRunRequest, run_id: str, *, charge_ip: str | None = None
) -> AsyncIterator[str]:
    """把循环产出的事件翻成帧，并保证流一定以 `[DONE]` 收尾。

    开流之后抛异常会让客户端看到一条断掉的连接，前端无法区分"后端崩了"和
    "网络断了"；所以这里把异常收成一条 error 帧，让前端拿到明确的失败原因。

    终态在这里判、也在这里落库（`finally`，三条出路都写得到）：

      - 发出过 `done` 帧 → `done`
      - 发出过 `error` 帧 → `failed`
      - 什么都没发就断了 → `cancelled`

    判据故意是"**帧**发没发出去"，而不是"抛了哪种异常"：客户端断开时 Starlette
    是关生成器，抛的是 `GeneratorExit`（`BaseException`），`except Exception`
    接不到；用帧作判据，取消、断开、超时被掐这三条路自动归进同一格。

    `charge_ip` 非空表示这一轮**占过公共免费额度的一格**，收尾时没有任何产出就
    退回去。判据是"最后有没有候选"而不是"有没有 error 帧"：半棵树交付
    （`DoneEvent(partial=True)`）是有产出的、那一次真的调用过模型，该照扣。
    """
    seq = 0
    # 登记"本进程正在跑这一轮"。`sweep_abandoned()` 靠它分清"活着但静默"与"进程
    # 已经没了"：一轮生成长时间不出帧是正常的（单次模型调用最长能到 `max_seconds`
    # 上限 3600 秒），只按 `updated_at` 判会把这种轮次误杀，而误杀之后它后面的
    # `checkpoint()` 会因为 `WHERE status = 'running'` 全部静默失效。
    #
    # 登记点在本生成器的第一行、而不是 `stream_run()` 里：生成器要到第一次迭代才
    # 执行，客户端在开流前断开时它一次都不跑——登记在这里就不会留下一条永远没人
    # 注销的活跃记录。注销在 `finally` 里，紧挨着 `finish()`。
    runs.active(run_id)
    # 只在正常收尾或已处理过的错误之后补 [DONE]。放在 finally 里会在 aclose()
    # 触发 GeneratorExit 时也去 yield，Python 直接抛
    # "async generator ignored GeneratorExit"，底层生成器的清理也被跳过。
    finished = False
    resolved = runs.STATUS_CANCELLED
    failure: dict[str, Any] | None = None
    mode: str | None = None
    # `done` 帧上那两个"这次走了哪条路"的字段。终态落库时要一起写：不写的话，
    # 同一个 run_id 第二次投递（`_replay`）拿到的帧就少了 model/reasoning，
    # 而第一次是有的——同一个 run 两种结果，用户会以为重放丢了东西。
    model: str | None = None
    # 服务端回报的模型名（与请求名分开记）。理由同上：重放要还原「请求 X → 服务 Y」
    # 的整句，少一半等于同一个 run 两条路径给两种说法。
    served_model: str | None = None
    reasoning: str | None = None
    # "这棵树没长完"（预算耗尽、工具超时、连续失败、空转收尾）与原因。同上，
    # 落库是为了让重放与第一次同形：不存它，重连后拿到的帧会说"完整生成"。
    partial: bool | None = None
    partial_reason: str | None = None
    # 候选的两个来源，按可信度排：`ctx` 是权威（含 edit/expand 那些只走 op 批次的
    # 改动），`observed` 是兜底（只在 `run()` 的收尾回调没能执行时才有用，比如
    # 任务被硬取消导致内层生成器没能跑完 finally）。多这一层不重复：一个来源是
    # "循环知道什么"，一个是"帧里已经发出去什么"，后者永远不会比前者更新。
    captured: dict[str, Any] = {"ctx": None, "observed": None}

    def on_close(candidate: Any) -> None:
        # 同步、只做赋值：取消路径上第一个 await 会立刻再抛一次 CancelledError。
        captured["ctx"] = candidate

    async def on_checkpoint(content: Any) -> None:
        # 中途那次落库。**异步**：工具是 async 的、跑在事件循环上，同步写 SQLite
        # 会把整个循环拿去等写锁——同一个进程里别的 SSE 流会跟着一帧都发不出来。
        # 交给线程池，阻塞关在那个 worker 里，这一轮生成照常推进。
        #
        # 写失败**不抛**：检查点是"跑不完时少丢一点"的补丁，它自己出错不该让一轮
        # 本来能成功的生成失败。真正常态收尾时 `finish()` 会再写一次并清掉这个标记。
        try:
            await run_in_threadpool(runs.checkpoint, run_id, content)
        except Exception:
            logger.warning("run %s 的检查点没有落库", run_id, exc_info=True)

    try:
        # 材料入库排在事件流之前：入库会把临时 id 换成库里的持久 id，换完的
        # id 必须同时送到前端（`sources` 帧）和生成上下文（重排后的 request）。
        #
        # **没有材料时整段跳过**：`remember_sources([])` 是空操作，而没有入库就
        # 没有"第一帧之前的静默"，那行注释帧也就没有送出去的理由。无条件发会让
        # "不带材料的请求，帧与加这段之前逐字节相同"这条不变量失效——那正是老
        # 客户端最依赖的一条。
        if body.sources:
            yield ": 正在把材料收进知识库\n\n"
            remembered = await run_in_threadpool(remember_sources, body.sources)
            if remembered.mapping:
                seq += 1
                yield encode_event(SourcesEvent(seq=seq, mapping=remembered.mapping))
                body = body.model_copy(update={"sources": remembered.sources})
        # `aclosing` 不是为了好看：客户端断开时这里被关掉，不显式关内层生成器，
        # 它的收尾（取消工具任务、交回候选）要等垃圾回收才执行——那段时间工具里
        # 的模型请求还在跑、还在计费。
        # `first_seq=seq+1`：`sources` 帧已占掉 seq，循环里的编号接着往下发。
        async with contextlib.aclosing(
            run(body, on_close=on_close, on_checkpoint=on_checkpoint, first_seq=seq + 1)
        ) as events:
            async for event in events:
                seq = max(seq, getattr(event, "seq", 0))
                if isinstance(event, DoneEvent):
                    resolved = runs.STATUS_DONE
                    mode = event.mode
                    model = event.model
                    served_model = event.served_model
                    # 空串按"没说"落库：`runs.finish()` 用 COALESCE，写成空串会把
                    # 上一轮已经记下的思考过程盖成空。
                    reasoning = event.reasoning or None
                    partial = event.partial
                    partial_reason = event.partial_reason
                    captured["observed"] = event.content
                elif isinstance(event, ErrorEvent):
                    resolved = runs.STATUS_FAILED
                    failure = {"code": event.code, "message": event.message}
                elif isinstance(event, SnapshotEvent):
                    captured["observed"] = event.content
                yield encode_event(event)
        finished = True
    except ModelNotConfigured as exc:
        # 必须排在 `except GenerationError` 之前（它是子类）：这条错误的下一步
        # 不是"重试"，界面上要显示成"去配模型"。
        resolved = runs.STATUS_FAILED
        failure = {"code": "model_not_configured", "message": str(exc)}
        yield encode_event(ErrorEvent(
            seq=seq + 1, code="model_not_configured", message=str(exc)))
        finished = True
    except GenerationError as exc:
        resolved = runs.STATUS_FAILED
        failure = {"code": "generation_failed", "message": str(exc)}
        yield encode_event(ErrorEvent(seq=seq + 1, code="generation_failed", message=str(exc)))
        finished = True
    except Exception as exc:  # noqa: BLE001 — 兜底：任何未预期异常都要变成可读的一帧
        # 只给用户一句能看懂的话。异常类型与原文（可能带绝对路径、库结构、上游
        # 地址）只进日志：出站帧是要给浏览器看的，不是排障现场。
        logger.exception("生成过程中出现未预期异常（%s），已收成 agent_failed 帧",
                         type(exc).__name__)
        resolved = runs.STATUS_FAILED
        failure = {"code": "agent_failed", "message": "生成过程中出现未预期错误，请稍后重试"}
        yield encode_event(ErrorEvent(
            seq=seq + 1,
            code="agent_failed",
            message="生成过程中出错了，请稍后重试。",
        ))
        finished = True
    finally:
        candidate = captured["ctx"] if captured["ctx"] is not None else captured["observed"]
        # 落库失败不该把一条已经跑完的流变成断开的连接：这是收尾，不是结果。
        # 拦下它并留下日志，用户至少还能从 done 帧拿到候选。
        try:
            runs.finish(run_id, resolved, candidate=candidate, failure=failure, mode=mode,
                        model=model, served_model=served_model, reasoning=reasoning,
                        partial=partial, partial_reason=partial_reason)
        except Exception:  # noqa: BLE001 — 见上
            logger.exception("run %s 的终态没有落库", run_id)
        # 没有任何产出 → 把开流前占的那一格还回去（见函数 docstring）。
        # 放在 `finish` 之后：先还额度再落终态的话，中间那一瞬间别的请求
        # 可能已经看到"额度回来了"，而这次运行的结论还没写下去。
        if charge_ip is not None and candidate is None:
            quota.refund(charge_ip)
        # 注销活跃登记：必须放在 `finish` 之后——先注销再落终态，中间那一瞬间
        # 别的请求的 `sweep_abandoned()` 就有机会把这一行判成化石。
        runs.inactive(run_id)
    if finished:
        yield DONE_FRAME


@router.post("/runs/stream")
async def stream_run(body: AgentRunRequest, request: Request) -> StreamingResponse:
    if not _agent_enabled():
        raise HTTPException(
            status_code=503,
            detail={"code": "agent_disabled", "message": "AI 代理功能当前已关闭"},
        )
    _validate(body)
    # 图型：请求没带就**从 `goal` 的文字里认**，并把那个词从 `goal` 里剥掉
    # （见 `app/agent/diagram_intent.py`）。位置在这里有三个理由：
    #
    #   1. 这是"请求已经合法、还没有任何一个字节发出去"的唯一时刻——认错时
    #      一切都还来得及；
    #   2. 必须早于 `_request_summary`：运行台账里那一行是事后排查"我要的鱼骨图
    #      怎么成了思维导图"的唯一痕迹，记 `None` 等于什么都没记；
    #   3. 工具层不行：`diagram_type` 在那里的默认值就是模型会走的默认值，推断
    #      放在它前面才轮得到"用户说的是什么"。
    apply_diagram_intent(body)

    # 公共免费额度：**开流之前**占一格。闸必须在门口，理由写在
    # `generation/quota.py::consume` 的 docstring 里（先发后记的话两个并发请求会
    # 同时看到"还剩 1 次"然后都放行）。
    #
    # 只在真会用公共密钥时占：用户填了自己的密钥（`saved`）花的是他自己的钱，
    # 演示模式一次模型都不调，这两种情况都不该被限量。`llm_mode()` 是只读的
    # 环境变量，与面板无关。
    charged = False
    ip = identity_module.client_ip(request.scope)
    if (
        quota.enabled()
        and settings_store.llm_mode() != "demo"
        and settings_store.resolve().api_key_source == settings_store.SOURCE_PUBLIC
    ):
        if not quota.consume(ip):
            # 429 而不是流里的 error 帧：这时候一个字节都还没发，客户端能按状态码
            # 分辨"今天用完了"与"生成失败了"，不必从一条流里去猜。`Retry-After`
            # 指向本地零点——界面上写的就是"明天恢复"。
            raise AppError(
                code="free_quota_exhausted",
                message="今天的免费生成次数用完了。明天会恢复；也可以到右上角的"
                        "「模型设置」里填自己的密钥，填了就不再受这个限制。",
                status=429,
                headers={"Retry-After": str(quota.seconds_until_tomorrow())},
            )
        charged = True

    run_id = body.run_id or uuid4().hex[:_GENERATED_RUN_ID_BYTES * 2]
    # `owner` 显式传进去，认领之后还要用它比对：`begin()` 的三种"没认领成功"
    # 分支里有两种会把**库里那行**交回来（409 与重放），而那一行可能是别人的。
    # 不比对的话，知道别人 `run_id` 的人能直接重放他的树（审查实测：B 打一次流
    # 就拿到了 A 的图）。重开那条已经在 SQL 里用 `AND owner = ?` 挡住了。
    who = owner_module.current_owner()
    claim = runs.begin(run_id, _request_summary(body), owner=who)
    if not claim.claimed:
        record = claim.record
        if record.owner != who:
            # 404 而不是 403：`run_id` 是客户端自己起的字符串，可以随便猜。403 等于
            # 确认"这个 id 真实存在"，于是 `runs` 成了一个可枚举的存在性探针。
            # 对外那句话与"这个 id 根本没有过"完全一样。
            if charged:
                quota.refund(ip)
            raise AppError(
                code="run_not_found",
                message="这次生成已经不在等待里了。换一个新的运行重新生成一次即可。",
                status=404,
            )
        if record.status == runs.STATUS_RUNNING:
            # 同一个 run 并发跑两次会互相打架：两边都往同一个候选上写，谁也说不清
            # 最终那棵树是谁的。409 是开流之前的错误，前端能按状态码分辨，不必
            # 从一条流里去猜。
            #
            # 用 `AppError` 而不是 `HTTPException(detail={...})`：后者的处理链只认
            # `detail` 是字符串时的那几种 code，传字典出去顶层 `code` 会退化成
            # `http_409`，前端拿到的就是一句没有信息量的兜底文案。
            if charged:
                # 这一次不会执行，刚占的那一格要还回去。不还的话"同一个 run_id
                # 连点两次"会白扣一次额度，而用户什么都没拿到。
                quota.refund(ip)
            raise AppError(
                code="run_in_progress",
                message="这次生成（同一个 run_id）已经在跑了，重复提交不会再开一次。"
                        "等它结束，或到页面上读取它的结果。",
                status=409,
            )
        # `done`：把已有候选重放出来，一次模型调用都不发。
        if charged:
            # 重放不调模型，占的那一格同样要还。这是"额度按真实消耗记"的落点：
            # 用户刷新页面捡回上一次结果，不该被当成新的一次生成。
            quota.refund(ip)
        return StreamingResponse(_replay(record), media_type="text/event-stream",
                                 headers=_SSE_HEADERS)

    _maybe_summarize(run_id)
    return StreamingResponse(
        _frames(body, run_id, charge_ip=ip if charged else None),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


def _maybe_summarize(run_id: str) -> None:
    """看看有没有"刚结束、还没总结"的上一个会话，有就起一个后台线程去总结。

    **位置**：认领完这次运行之后、返回流之前。不能更早（认领之前 `runs` 表里还
    没有当前这一行，`due()` 会把"现在"当成上一次运行的结束时刻，间隔永远不够），
    也不能更晚（生成一开始就没人关心上一次会话了）。

    **为什么能在这里做**：这就是"会话结束由后端自己判定、不需要前端配合"的落点。
    后端唯一能自己判定的时刻是"下一次运行开始"，而这里正是那个时刻。

    **失败只记 warning**：总结是附属动作，它抛出去会让一次本来正常的生成 500。
    读库超时、模型配置坏掉、库被锁——这些都不该影响用户这一次要的图。

    **`owner` 在这里读、一路传下去**：`maybe_summarize` 起的线程读不到请求的
    `ContextVar`，所以"总结出来算谁的"必须在这一刻定死。传空串（比如后台任务里
    调这个函数）的后果是记忆写成无主——那正是 PR#27 审查里 P0-2 的形状。
    """
    try:
        memory_summarize.maybe_summarize(
            exclude_run_id=run_id, owner=owner_module.current_owner())
    except Exception as exc:  # noqa: BLE001 — 见 docstring
        logger.warning("上一个会话的记忆总结没有启动（%s），这次生成不受影响",
                       type(exc).__name__)


# ---------------------------------------------------------------- 读：run 的台账

class RunSummary(BaseModel):
    """列表里的一行：**不带结果正文**。

    结果是一整棵树。20 条一起下发，等于每次进页面先传几十万字节，只为显示一句
    "上次有一次生成还没保存"。真要那份内容时点「查看」，走 `GET /runs/{run_id}`。
    """

    model_config = ConfigDict(extra="forbid")
    run_id: str
    status: str
    #: 原样回填给前端：`create` 的结果落画布时不能继承当前图的身份，`edit`/`expand` 要。
    intent: str
    goal: str
    created_at: str
    updated_at: str
    #: 这份结果后来被保存了。空值 = 生成过、还没存下来，下次进页面会提醒一次。
    applied_at: str | None = None
    #: 半棵树（有产出但没跑完）。**None 是"这条记录没记过"**，不是"完整"——
    #: 老行上这列是 NULL，判"是否半棵"必须写 `is True`。
    #:
    #: 列表里也要带：前端靠它决定"捡回上次结果"时该不该提示"有分支没生成完整"。
    #: 只在 `done` 帧上带过、读接口丢掉的那一版，用户点「载入」拿到的半棵树与一次
    #: 完整生成长得一模一样——而这恰恰是本 PR 想消灭的那种"坏了和本来就没有长得一样"。
    partial: bool | None = None
    #: 这份结果不是收尾交的，而是跑到一半截下来的检查点（`runs.checkpoint`）。
    #:
    #: 有值 = "说明还没写、`detail` 是空的，结构是完整的"。它不进 `partial`：
    #: `partial` 说的是"树没长完"，这里说的是"树长全了、说明没来得及写"，
    #: 两件事的下一步不一样（前者补内容，后者只是缺描述）。
    checkpointed_at: str | None = None
    #: 这份结果有多少个节点。前端拿它说"已保存 N 个节点"，不值得为一句提示
    #: 再点开详情拉整棵树。
    node_count: int | None = None


class RunDetail(RunSummary):
    """单条：带结果正文与失败原因。"""

    model_config = ConfigDict(extra="forbid")
    candidate: Any | None = None
    failure: dict[str, Any] | None = None
    mode: str | None = None
    #: 半棵树的那一句原因（与 `done` 帧的 `partial_reason` 同一句）。
    partial_reason: str | None = None


def _summary(record: runs.RunRecord) -> RunSummary:
    request = record.request
    return RunSummary(
        run_id=record.run_id,
        status=record.status,
        intent=str(request.get("intent") or "create"),
        goal=str(request.get("goal") or ""),
        created_at=record.created_at,
        updated_at=record.updated_at,
        applied_at=record.applied_at,
        partial=record.partial,
        checkpointed_at=record.checkpointed_at,
        # 只在检查点上数一遍：成品那份前端已经会按 `partial` 说话，不需要这个数。
        # 数一遍是遍历一棵树，几十微秒；列表最多 20 条，划算。
        node_count=(
            shapes.count_nodes(record.candidate)
            if record.checkpointed_at and record.candidate is not None
            else None
        ),
    )


@router.get("/runs", response_model=list[RunSummary])
def list_runs(
    status_filter: Literal["done", "cancelled", "failed", "running"] | None = Query(
        default=None, alias="status",
        description="只看某一种状态。缺省 = 有结果且还没被保存过的（done + cancelled + failed）。",
    ),
    unapplied: bool = Query(
        default=True,
        description="只列有结果、且后来没被保存过的；置 false 则把这段台账原样列出来。",
    ),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[RunSummary]:
    """运行台账。默认回答的是"上次跑完、我还没存下来的是哪一次"。

    `status` 缺省时把 `done` / `cancelled` / `failed` 一起算：

      - **`cancelled` 但已经产出结果的那次同样要能被捡起来**——否则"关掉页面那
        40 秒"照样白花，只是从内存白花变成写进数据库白花。
      - **`failed` 里有 `run_abandoned`**：进程崩了，`sweep_abandoned()` 把那行判死
        并留下了崩溃前那个检查点。不列它的后果是"检查点存得再好也没人读得到"——
        用户下次进来看到的是"什么都没有"，而那棵树其实就在库里。

    有没有东西可捡由 `candidate IS NOT NULL` 判（`pending()` 里的条件），不由状态判：
    一个 `failed` 行可能什么都留，一个 `cancelled` 行可能留了整棵树。

    这条查询就是这张表结果列的**唯一读者**。没有它，`candidate` 写进去之后再没人看过。
    """
    statuses: tuple[str, ...] = (
        (status_filter,) if status_filter
        else (runs.STATUS_DONE, runs.STATUS_CANCELLED, runs.STATUS_FAILED)
    )
    records = runs.pending(limit=limit, statuses=statuses, unresolved_only=unapplied)
    return [_summary(record) for record in records]


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str) -> RunDetail:
    # `get_for` 而不是 `get`：`run_id` 是客户端自己生成并传上来的，猜中一个就等于
    # 读到别人还没保存的那棵树。别人的 id 一律 404——"这个 id 存在但不属于你"
    # 本身就是一条不该给的信息。
    record = runs.get_for(run_id)
    if record is None:
        raise HTTPException(status_code=404,
                            detail={"code": "run_not_found", "message": "没有这次运行的记录"})
    return RunDetail(
        **_summary(record).model_dump(),
        candidate=None if record.candidate is None else record.candidate.model_dump(mode="json"),
        failure=record.failure,
        mode=record.mode,
        partial_reason=record.partial_reason,
    )


@router.post("/runs/{run_id}/applied", status_code=status.HTTP_204_NO_CONTENT)
def mark_run_applied(run_id: str) -> Response:
    """这次生成的结果**被保存进库了**。前端在保存成功后调用一次，幂等。

    "生成过但没保存"与"生成过、也存下来了"只能靠这个时间戳分开：那份内容可能被
    PUT 到一张已有导图上（推不出新记录），也可能用户存成了另一张。用 `mindmaps`
    表反推，得到的全是猜。

    按 owner 过滤：否则知道别人 `run_id` 的人能替他标掉这一位，那条记录再也不会
    出现在他的「上次的结果」里——一条安静的数据丢失。
    """
    if not runs.mark_applied(run_id):
        raise HTTPException(status_code=404,
                            detail={"code": "run_not_found", "message": "没有这次运行的记录"})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router", "AgentEvent"]
