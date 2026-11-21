"""`runs` 表与 run 身份的测试。

这一层验的是此前不存在的东西：**同一次运行的身份**与**运行结束（含被取消）时的
草稿**。SSE 帧格式一个字节都没改，所以这里不重复验帧（那份在 test_agent_stream）。

取消那条用例直接驱动 `router._frames`：uvicorn 在客户端断开时做的事就是对自己
手里的响应生成器调一次 `aclose()`，这条路与真机一致，而且能精确停在"候选已经
产出、用户刚好关页面"的那一刻——换成 TestClient 打请求，取消时机就不可控了。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import pytest
import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

import app.agent.router as agent_router
from app.agent import runs
from app.agent.contracts import (
    AgentRunRequest,
    Completion,
    DoneEvent,
    StageEvent,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from app.agent.router import _request_summary, router
from app.agent.service import run as service_run
from app.agent.stream import DONE_FRAME, encode_event
from app.agent.tools import ToolRegistry
from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source
from app.core.errors import register_error_handlers

app = FastAPI()
register_error_handlers(app)
app.include_router(router, prefix="/api")
client = TestClient(app)

URL = "/api/agent/runs/stream"

# 三条一级分支：`mindmap_generate` 的默认校验要求 ≥3，少一条整棵树会被判 invalid。
TREE = {
    "title": "示例",
    "root": {"id": "root", "label": "主题", "children": [
        {"id": "a", "label": "甲", "children": []},
        {"id": "b", "label": "乙", "children": []},
        {"id": "c", "label": "丙", "children": []},
    ]},
}
CANDIDATE = MindMapContent.model_validate(TREE)


def _frames(text: str) -> list[dict]:
    out: list[dict] = []
    for block in text.split("\n\n"):
        line = block.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            continue
        out.append(json.loads(payload))
    return out


async def _scripted(*events):
    for event in events:
        yield event


def _completion(calls: list[ToolCall] | None = None, finish: str = "stop") -> Completion:
    calls = calls or []
    message: dict[str, Any] = {"role": "assistant", "content": "好的"}
    if calls:
        message["tool_calls"] = [
            {"id": call.call_id, "type": "function",
             "function": {"name": call.name,
                          "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
            for call in calls
        ]
    return Completion(message=message, tool_calls=calls, finish_reason=finish,
                      usage={"total_tokens": 1})


class ScriptedProvider:
    """按脚本返回；脚本项可以是 Completion，可以是异常，也可以是"永不返回"。"""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls = 0

    async def complete_tools(self, messages, tools, *, budget):
        self.calls += 1
        if not self._script:
            raise AssertionError("脚本已用完，循环比预期多调了一次模型")
        item = self._script.pop(0)
        if item is _HANG:
            await asyncio.Event().wait()
        if isinstance(item, BaseException):
            raise item
        return item


_HANG = object()


class _EchoArgs(BaseModel):
    text: str = Field(min_length=1)


def _registry(handler) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolSpec(name="echo", description="测试工具", args_model=_EchoArgs, fn=handler))
    return registry


# ---------------------------------------------------------------- 仓储层


def test_begin_creates_a_running_row():
    claim = runs.begin("r1", {"goal": "做一张导图", "intent": "create"})

    assert claim.claimed is True
    assert claim.record.status == runs.STATUS_RUNNING
    row = runs.get("r1")
    assert row is not None
    assert row.request == {"goal": "做一张导图", "intent": "create"}
    assert row.candidate is None
    # 时间戳只在服务端生成，客户端不能指定：它是"谁更晚"的唯一依据。
    assert row.created_at == row.updated_at
    assert row.created_at.endswith("Z")


def test_finish_writes_candidate_and_status():
    runs.begin("r1", {})
    assert runs.finish("r1", runs.STATUS_DONE, candidate=CANDIDATE, mode="model") is True

    row = runs.get("r1")
    assert row.status == runs.STATUS_DONE
    assert row.candidate is not None
    assert row.candidate.root.label == "主题"
    assert row.mode == "model"


def test_repeated_begin_is_idempotent_and_keeps_the_draft():
    """同一 run_id 重复 begin：不报错，也不覆盖已经产出的候选。

    这条是"两个标签页同时投递"的底线：第二个请求可以拿不到运行权，但它不能把
    第一个已经跑出来的草稿抹掉——那等于用户为了捡结果而点一下，结果就没了。
    """
    runs.begin("r1", {"goal": "第一次"})
    runs.finish("r1", runs.STATUS_DONE, candidate=CANDIDATE)

    again = runs.begin("r1", {"goal": "第二次"})

    assert again.claimed is False
    row = runs.get("r1")
    assert row.status == runs.STATUS_DONE
    assert row.candidate is not None and row.candidate.root.label == "主题"
    # 身份行记的是"这一次运行是什么"，不是"最后一次请求说了什么"。
    assert row.request == {"goal": "第一次"}


def test_done_is_terminal_so_a_late_finish_cannot_overwrite_it():
    """`done` 之后冻结：一条迟到的取消不许把已经交付的候选改写成 cancelled。"""
    runs.begin("r1", {})
    runs.finish("r1", runs.STATUS_DONE, candidate=CANDIDATE)

    assert runs.finish("r1", runs.STATUS_CANCELLED) is False
    row = runs.get("r1")
    assert row.status == runs.STATUS_DONE
    assert row.candidate is not None


def test_finish_without_candidate_never_clears_the_previous_draft():
    """收尾没拿到候选（被取消的那次树还没生成完）不该把上一份草稿写成 NULL。"""
    runs.begin("r1", {})
    runs.finish("r1", runs.STATUS_CANCELLED, candidate=CANDIDATE)

    runs.finish("r1", runs.STATUS_CANCELLED)

    assert runs.get("r1").candidate is not None


def test_cancelled_and_failed_runs_can_be_reopened_but_done_cannot():
    for terminal in (runs.STATUS_CANCELLED, runs.STATUS_FAILED):
        runs.begin("r-" + terminal, {})
        runs.finish("r-" + terminal, terminal)

        reopened = runs.begin("r-" + terminal, {})

        assert reopened.claimed is True, terminal
        assert runs.get("r-" + terminal).status == runs.STATUS_RUNNING

    runs.begin("r-done", {})
    runs.finish("r-done", runs.STATUS_DONE)
    assert runs.begin("r-done", {}).claimed is False


def test_finish_rejects_non_terminal_status():
    runs.begin("r1", {})
    with unittest.TestCase().assertRaises(ValueError):
        runs.finish("r1", runs.STATUS_RUNNING)


def test_reopening_a_run_clears_the_previous_partial_flag():
    """重开一轮就把上一轮那个"没长完"擦掉：它说的是上一轮的产出。

    不清的后果不是"多一个陈旧字段"这么轻：`finish` 用 `COALESCE` 写 partial，值一旦
    留下就再也不会被 NULL 覆盖——这一轮如果完整跑完（正常收尾根本不传 partial），
    记录里仍然是 `partial = 1`，前端据此把一次完整生成标成"部分分支未完成"，
    而且下一次载入草稿也照着这个错标记渲染。
    """
    runs.begin("retry-1", {"goal": "再来一次"})
    runs.finish("retry-1", runs.STATUS_CANCELLED, candidate=CANDIDATE,
                partial=True, partial_reason="工具卡住了")

    assert runs.get("retry-1").partial is True

    runs.begin("retry-1", {"goal": "再来一次"})
    record = runs.get("retry-1")
    assert record.status == runs.STATUS_RUNNING
    assert record.partial is None, "重开之后上一轮的标记必须消失"
    assert record.partial_reason is None

    runs.finish("retry-1", runs.STATUS_DONE, candidate=CANDIDATE)
    assert runs.get("retry-1").partial is None, "完整跑完的那一轮不该带着旧标记"


def test_reopening_a_run_swaps_in_the_new_request_summary():
    """重开要换掉 `request`：台账上"这一行在做什么"只有这一列说得清。

    「补齐说明」就是拿检查点那行的 id 发一次 `intent="edit"`（见
    `stores/mindmap.ts` 的 `fillMissingDetails`）。如果重开不改 `request`，
    `router._summary()` 返回的 `intent` 仍是上一轮的 `create`，前端
    `commitGenerated` 据此走"重置身份"分支——用户在改一张已保存的图，载入结果后
    再点保存就变成新建一条。
    """
    runs.begin("resume-1", {"goal": "原来的主题", "intent": "create"})
    runs.finish("resume-1", runs.STATUS_FAILED,
                candidate=CANDIDATE, failure={"code": "agent_failed", "message": "断了"})

    claim = runs.begin("resume-1", {"goal": "给这张导图补说明", "intent": "edit"})

    assert claim.claimed is True
    assert claim.record.request == {"goal": "给这张导图补说明", "intent": "edit"}
    assert claim.record.candidate is not None, "续写的起点不能被抹掉"


def test_reopening_with_an_empty_summary_keeps_the_old_request():
    """内部调用方传空摘要时保留旧值：那表示"这次没带摘要"，不是"摘要为空"。"""
    runs.begin("keep-req", {"goal": "原来的主题", "intent": "create"})
    runs.finish("keep-req", runs.STATUS_FAILED, failure={"code": "agent_failed", "message": "断了"})

    claim = runs.begin("keep-req", {})

    assert claim.claimed is True
    assert claim.record.request == {"goal": "原来的主题", "intent": "create"}


def test_pending_only_returns_done_runs_that_were_never_applied():
    """`pending()` 的字面含义：**done** 且没应用过。"""
    runs.begin("done-unapplied", {})
    runs.finish("done-unapplied", runs.STATUS_DONE, candidate=CANDIDATE)

    runs.begin("done-applied", {})
    runs.finish("done-applied", runs.STATUS_DONE, candidate=CANDIDATE)
    runs.mark_applied("done-applied")

    runs.begin("done-no-candidate", {})
    runs.finish("done-no-candidate", runs.STATUS_DONE)

    runs.begin("cancelled-with-draft", {})
    runs.finish("cancelled-with-draft", runs.STATUS_CANCELLED, candidate=CANDIDATE)

    runs.begin("still-running", {})

    assert [row.run_id for row in runs.pending()] == ["done-unapplied"]
    # 前端展示时把"被取消但有草稿"一起算：那 40 秒正是这么捡回来的。
    assert {row.run_id for row in runs.pending(
        statuses=(runs.STATUS_DONE, runs.STATUS_CANCELLED))} == {
            "done-unapplied", "cancelled-with-draft"}


def test_saved_runs_stop_showing_up():
    """存下来之后就不再提醒。

    候选层删掉之后没有"丢弃"这个动作了，`applied_at` 是唯一的"处理过了"标记：
    它现在说的是"这份结果被保存进库了"，而不是"用户点了应用"。
    """
    runs.begin("r1", {})
    runs.finish("r1", runs.STATUS_DONE, candidate=CANDIDATE)

    assert runs.mark_applied("r1") is True

    assert runs.pending() == []
    # 记过账的行仍然查得到，只是不再进 pending。
    row = runs.get("r1")
    assert row.applied_at is not None


def test_mark_applied_is_idempotent_and_keeps_the_first_stamp():
    runs.begin("r1", {})
    runs.finish("r1", runs.STATUS_DONE, candidate=CANDIDATE)

    assert runs.mark_applied("r1") is True
    first = runs.get("r1").applied_at
    assert runs.mark_applied("r1") is True

    assert runs.get("r1").applied_at == first


def test_eight_simultaneous_begin_calls_start_exactly_one_run():
    """并发认领靠 SQLite 的写锁，不靠进程内字典——后者在多 worker 下每个进程一份。"""
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: runs.begin("race", {}), range(8)))

    assert sum(claim.claimed for claim in claims) == 1
    assert all(claim.record.run_id == "race" for claim in claims)


def test_request_summary_never_carries_source_text():
    """`runs` 是身份台账，不是材料仓库：正文一个字都不进来。"""
    secret = "绝密原文" * 500
    body = AgentRunRequest(goal="整理材料", sources=[Source(kind="text", text=secret)])

    summary = _request_summary(body)

    assert summary["source_ids"] == [body.sources[0].id]
    assert secret not in json.dumps(summary, ensure_ascii=False)


# ---------------------------------------------------------------- 路由层


def test_old_client_without_run_id_gets_byte_identical_frames():
    """不带 run_id 的老客户端：响应体与加这张表之前**逐字节**相同。

    服务端会自己补一个 run_id 落库，但那是副作用，不许出现在帧里——帧格式一变，
    旧前端就会开始丢字段。
    """
    events = [
        StageEvent(seq=1, stage="准备"),
        DoneEvent(seq=2, content=CANDIDATE, mode="model"),
    ]
    with patch("app.agent.router.run", return_value=_scripted(*events)):
        response = client.post(URL, json={"goal": "做一张导图", "intent": "create"})

    assert response.status_code == 200
    assert response.text == "".join(encode_event(event) for event in events) + DONE_FRAME
    # 副作用确实发生了：这一行有结论，并且 run_id 是服务端生成的合法形状。
    rows = runs.pending(statuses=(runs.STATUS_DONE,), unresolved_only=False)
    assert len(rows) == 1
    assert len(rows[0].run_id) == 32 and rows[0].run_id.isalnum()


def test_records_are_written_for_each_terminal_status():
    """正常收尾 / 上游失败 / 中途被取消，三种结局都落一行。"""
    with patch("app.agent.router.run", return_value=_scripted(
            DoneEvent(seq=1, content=CANDIDATE, mode="model"))):
        client.post(URL, json={"goal": "g", "intent": "create", "run_id": "ok"})

    async def failing():
        from app.generation.service import GenerationError
        raise GenerationError("模型服务不可用")
        yield  # pragma: no cover

    with patch("app.agent.router.run", return_value=failing()):
        client.post(URL, json={"goal": "g", "intent": "create", "run_id": "bad"})

    ok, bad = runs.get("ok"), runs.get("bad")
    assert ok.status == runs.STATUS_DONE and ok.candidate is not None
    assert bad.status == runs.STATUS_FAILED
    assert bad.failure == {"code": "generation_failed", "message": "模型服务不可用"}
    assert bad.candidate is None


def test_same_run_id_while_running_is_409():
    runs.begin("dup", {"goal": "g"})
    started: list[int] = []

    def should_not_start(_request, **_kwargs):
        started.append(1)
        return _scripted()

    with patch("app.agent.router.run", new=should_not_start):
        response = client.post(URL, json={"goal": "g", "intent": "create", "run_id": "dup"})

    assert response.status_code == 409
    assert response.json()["code"] == "run_in_progress"
    assert response.json()["message"]
    assert started == []


def test_same_run_id_after_done_replays_the_candidate_without_calling_the_model():
    """同一个 run_id 第二次请求：把已有候选原样重放，**一次模型调用都不发**。"""
    runs.begin("done-1", {"goal": "g", "intent": "create"})
    runs.finish("done-1", runs.STATUS_DONE, candidate=CANDIDATE, mode="model")

    chats: list[str] = []

    async def fake_chat(system, user, **kwargs):
        chats.append(user)
        return TREE

    def should_not_start(_request, **_kwargs):
        raise AssertionError("已经有结论的 run 不该重新跑")

    with patch("app.agent.tools.chat_async", new=fake_chat), \
            patch("app.agent.router.run", new=should_not_start):
        response = client.post(URL, json={"goal": "g", "intent": "create", "run_id": "done-1"})

    frames = _frames(response.text)
    assert [frame["type"] for frame in frames] == ["snapshot", "done"]
    assert frames[-1]["content"]["title"] == "示例"
    # 来历也照原样回填：占位树被重放成 model 就是界面替它撒谎。
    assert frames[-1]["mode"] == "model"
    assert response.text.rstrip().endswith("data: [DONE]")
    assert chats == []


def test_replay_keeps_demo_mode():
    runs.begin("demo-1", {"goal": "g"})
    runs.finish("demo-1", runs.STATUS_DONE, candidate=CANDIDATE, mode="demo")

    def should_not_start(*_args, **_kwargs):
        raise AssertionError("不该重新跑")

    with patch("app.agent.router.run", new=should_not_start):
        response = client.post(URL, json={"goal": "g", "intent": "create", "run_id": "demo-1"})

    assert _frames(response.text)[-1]["mode"] == "demo"


def test_replay_keeps_the_partial_flag():
    """重放出来的帧要带原来那个"没长完"的标记与原因。

    同一份产出，重连后若变成"完整生成"，用户会以为上一轮真的一次跑完了——
    而他断开之前看到的明明是"部分完成"。标记必须落库，正是为了这一刻。
    """
    runs.begin("partial-1", {"goal": "g"})
    runs.finish("partial-1", runs.STATUS_DONE, candidate=CANDIDATE, mode="model",
                partial=True, partial_reason="模型调用次数已用完（已执行 6 次工具调用）")

    def should_not_start(*_args, **_kwargs):
        raise AssertionError("不该重新跑")

    with patch("app.agent.router.run", new=should_not_start):
        response = client.post(URL, json={"goal": "g", "intent": "create",
                                          "run_id": "partial-1"})

    done = _frames(response.text)[-1]
    assert done["type"] == "done"
    assert done["partial"] is True
    assert done["partial_reason"] == "模型调用次数已用完（已执行 6 次工具调用）"


def test_completed_run_replays_without_a_partial_flag():
    """完整跑完的那次重放时不该凭空多出 `partial`。

    正常收尾根本不传 `partial`（`DoneEvent.partial` 是 None），`COALESCE` 让它留
    在 NULL 上；重放时读到 None，`exclude_none` 就把它抹掉——帧与加字段之前一样。
    """
    runs.begin("full-1", {"goal": "g"})
    runs.finish("full-1", runs.STATUS_DONE, candidate=CANDIDATE, mode="model")

    def should_not_start(*_args, **_kwargs):
        raise AssertionError("不该重新跑")

    with patch("app.agent.router.run", new=should_not_start):
        response = client.post(URL, json={"goal": "g", "intent": "create",
                                          "run_id": "full-1"})

    done = _frames(response.text)[-1]
    assert "partial" not in done
    assert "partial_reason" not in done


def test_cancelled_run_is_reopened_instead_of_being_replayed():
    """取消过的 run 允许用同一个 id 重跑：那是用户主动重试，不是重复投递。"""
    runs.begin("retry-1", {"goal": "g"})
    runs.finish("retry-1", runs.STATUS_CANCELLED, candidate=CANDIDATE)

    with patch("app.agent.router.run", return_value=_scripted(
            DoneEvent(seq=1, content=CANDIDATE, mode="model"))) as replayed:
        response = client.post(URL, json={"goal": "g", "intent": "create", "run_id": "retry-1"})

    assert replayed.called
    assert [frame["type"] for frame in _frames(response.text)] == ["done"]
    assert runs.get("retry-1").status == runs.STATUS_DONE


def test_run_id_is_validated_and_extra_fields_still_forbidden():
    """新字段进契约，`extra="forbid"` 那条护栏不能跟着松掉。"""
    assert client.post(URL, json={
        "goal": "g", "intent": "create", "run_id": "a/b"}).status_code == 422
    assert client.post(URL, json={
        "goal": "g", "intent": "create", "runid": "abc"}).status_code == 422


# ---------------------------------------------------------------- 取消路径


def _routed_with(provider, registry):
    """把 `_frames` 调用的 `run` 换成"真循环 + 假 provider"。"""

    def routed(request, *, on_close=None, on_checkpoint=None, first_seq=None):
        return service_run(request, provider=provider, tools=registry,
                           on_close=on_close, on_checkpoint=on_checkpoint)

    return routed


def _open_run(run_id: str, request: AgentRunRequest) -> None:
    """真机上 `stream_run` 开流之前都会先 `begin()`，这里直接驱动 `_frames` 要补上。"""
    runs.begin(run_id, {"goal": request.goal, "intent": request.intent})


def _cancel_after_first_snapshot(run_id: str, request: AgentRunRequest) -> list[str]:
    """驱动 `_frames`，收到 snapshot 帧就关掉生成器（= 客户端断开 / 用户取消）。"""

    async def scenario() -> list[str]:
        chunks: list[str] = []
        agen = agent_router._frames(request, run_id)
        async for chunk in agen:
            chunks.append(chunk)
            if '"type": "snapshot"' in chunk:
                break
        # uvicorn 在客户端断开时做的就是这个
        await agen.aclose()
        return chunks

    _open_run(run_id, request)
    return asyncio.run(scenario())


def test_cancel_mid_run_still_writes_the_draft():
    """**这就是"关掉页面那 40 秒别白花"**：树已经生成完，用户关了页面。"""
    provider = ScriptedProvider([
        _completion([ToolCall(call_id="c1", name="echo", arguments={"text": "生成"})],
                    finish="tool_calls"),
        _HANG,
    ])

    def generate(_args, ctx):
        ctx.publish_tree(CANDIDATE.model_copy(deep=True))
        return ToolResult(ok=True, content="已生成")

    async def fake_chat(system, user, **kwargs):
        return TREE

    request = AgentRunRequest(goal="做一张导图", intent="create")
    with patch("app.agent.router.run", new=_routed_with(provider, _registry(generate))):
        _cancel_after_first_snapshot("cancel-1", request)

    row = runs.get("cancel-1")
    assert row.status == runs.STATUS_CANCELLED
    assert row.candidate is not None
    assert row.candidate.root.label == "主题"
    assert row.failure is None


def test_cancelled_edit_run_keeps_the_tree_that_only_the_context_knows():
    """`edit` 的改动只走 op 批次，**没有任何一帧带着整棵树**。

    所以草稿只能来自 `ctx.candidate`：客户端手里那堆增量拼不回整树。这条用例
    专门盯这一点——把 `on_close` 拿掉，它必须红。
    """
    snapshot = MindMapContent.model_validate(TREE)
    provider = ScriptedProvider([
        _completion([ToolCall(call_id="c1", name="echo", arguments={"text": "改"})],
                    finish="tool_calls"),
    ])

    def edit(_args, ctx):
        assert ctx.candidate is not None
        grown = ctx.candidate.model_copy(deep=True)
        grown.root.children.append(type(grown.root)(id="new", label="新加的一支", children=[]))
        ctx.publish_tree(grown)
        return ToolResult(ok=True, content="已改")

    request = AgentRunRequest(goal="改一下", intent="edit", snapshot=snapshot)

    async def scenario() -> None:
        agen = agent_router._frames(request, "edit-cancel")
        async for chunk in agen:
            if '"type": "tool_end"' in chunk:
                break
        await agen.aclose()

    _open_run("edit-cancel", request)
    with patch("app.agent.router.run", new=_routed_with(provider, _registry(edit))):
        asyncio.run(scenario())

    row = runs.get("edit-cancel")
    assert row.status == runs.STATUS_CANCELLED
    assert row.candidate is not None
    assert [child.id for child in row.candidate.root.children] == ["a", "b", "c", "new"]


def test_cancelled_error_also_lands_as_cancelled():
    """任务被 `cancel()`（不是 aclose）时同样要落库——两条路都是"用户走了"。"""
    provider = ScriptedProvider([_HANG])
    request = AgentRunRequest(goal="做一张导图", intent="create")

    async def scenario() -> None:
        agen = agent_router._frames(request, "task-cancel")
        task = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await agen.aclose()

    _open_run("task-cancel", request)
    with patch("app.agent.router.run", new=_routed_with(provider, _registry(
            lambda _args, ctx: ToolResult(ok=True, content="ok")))):
        asyncio.run(scenario())

    row = runs.get("task-cancel")
    assert row.status == runs.STATUS_CANCELLED


# ---------------------------------------------------------------- 读路径


def test_list_endpoint_hides_the_candidate_body_and_the_applied_ones():
    runs.begin("done-1", {"goal": "上次那张图", "intent": "create"})
    runs.finish("done-1", runs.STATUS_DONE, candidate=CANDIDATE)
    runs.begin("done-2", {"goal": "已经应用过的", "intent": "create"})
    runs.finish("done-2", runs.STATUS_DONE, candidate=CANDIDATE)
    runs.mark_applied("done-2")

    response = client.get("/api/agent/runs", params={"status": "done", "unapplied": "1"})

    assert response.status_code == 200
    body = response.json()
    assert [item["run_id"] for item in body] == ["done-1"]
    assert body[0]["goal"] == "上次那张图"
    # 列表不带候选正文：那是一整棵树，20 条一起下发只为显示一句提示。
    assert "candidate" not in body[0]


def test_list_endpoint_includes_cancelled_drafts_by_default():
    """默认把"被取消但有草稿"也算上——否则那 40 秒只是从内存白花变成入库白花。"""
    runs.begin("done-1", {"goal": "完成了", "intent": "create"})
    runs.finish("done-1", runs.STATUS_DONE, candidate=CANDIDATE)
    runs.begin("cancelled-1", {"goal": "被中断了", "intent": "create"})
    runs.finish("cancelled-1", runs.STATUS_CANCELLED, candidate=CANDIDATE)

    body = client.get("/api/agent/runs").json()

    assert {item["status"] for item in body} == {"done", "cancelled"}


def test_detail_endpoint_returns_the_tree_and_the_failure_reason():
    runs.begin("done-1", {"goal": "上次那张图", "intent": "expand"})
    runs.finish("done-1", runs.STATUS_DONE, candidate=CANDIDATE, mode="model")

    body = client.get("/api/agent/runs/done-1").json()

    assert body["candidate"]["root"]["label"] == "主题"
    assert body["intent"] == "expand"
    assert body["mode"] == "model"
    assert client.get("/api/agent/runs/nope").status_code == 404


def test_detail_endpoint_carries_the_partial_flag():
    """"半棵树"这个事实必须随台账一起下发，不能只在实时流里出现过一次。

    `GET /agent/runs/{id}` 是前端点「载入」时唯一的信息来源。少了这个标记，一份
    半棵树与一次完整生成在界面上完全同形：没有告警、状态行照旧写"记得保存"，
    用户会以为上次真的跑完了。列表接口同理——它决定"要不要提示还有草稿可捡"。
    """
    runs.begin("half-1", {"goal": "跑到一半", "intent": "create"})
    runs.finish("half-1", runs.STATUS_DONE, candidate=CANDIDATE, mode="model",
                partial=True, partial_reason="模型调用次数已用完")

    detail = client.get("/api/agent/runs/half-1").json()
    assert detail["partial"] is True
    assert detail["partial_reason"] == "模型调用次数已用完"

    listed = client.get("/api/agent/runs").json()
    assert [item["run_id"] for item in listed] == ["half-1"]
    assert listed[0]["partial"] is True


def test_detail_endpoint_leaves_partial_empty_for_a_finished_run():
    """完整跑完的记录里 `partial` 是 null，不是 false：两者含义不同。

    null = 这条记录没记过（老库、或那一轮没走交付标记），false 会被读成"确定是完整的"。
    前端判断一律写 `partial === true`，所以这里只钉住"不该凭空变成 false"。
    """
    runs.begin("whole-1", {"goal": "跑完了", "intent": "create"})
    runs.finish("whole-1", runs.STATUS_DONE, candidate=CANDIDATE, mode="model")

    detail = client.get("/api/agent/runs/whole-1").json()
    assert detail["partial"] is None
    assert detail["partial_reason"] is None


def test_applied_endpoint_is_idempotent_and_404_for_unknown():
    runs.begin("r1", {})
    runs.finish("r1", runs.STATUS_DONE, candidate=CANDIDATE)

    assert client.post("/api/agent/runs/r1/applied").status_code == 204
    assert client.post("/api/agent/runs/r1/applied").status_code == 204
    assert runs.get("r1").applied_at is not None
    assert client.get("/api/agent/runs").json() == []

    assert client.post("/api/agent/runs/nope/applied").status_code == 404


def test_dismissed_endpoint_is_gone():
    """没有"丢弃候选"这个动作了，那条写入口必须一起消失。

    判在 OpenAPI 上而不是判 `POST /runs/x/dismissed` 的状态码：那条路现在会 404
    （路径不存在），与"没有这次运行"撞成同一个码，断言它区分不出"端点删了"与
    "端点还在但行不在"——而这两件事的后果完全不同。
    """
    document = client.get("/openapi.json").json()
    assert "/api/agent/runs/{run_id}/applied" in document["paths"]
    assert "/api/agent/runs/{run_id}/dismissed" not in document["paths"]
    # 列表/详情里也不该再出现这个列：前端镜像着它，留着就会有人去读。
    schemas = document["components"]["schemas"]
    assert "dismissed_at" not in schemas["RunSummary"]["properties"]


# ---------------------------------------------------------------- 检查点


def test_checkpoint_saves_the_half_tree_while_running():
    """跑到一半落的那棵树，收尾之前就能从台账里读回来。"""
    runs.begin("cp-1", {"goal": "g", "intent": "create"})

    assert runs.checkpoint("cp-1", CANDIDATE) is True

    record = runs.get("cp-1")
    assert record.status == runs.STATUS_RUNNING
    assert record.candidate is not None
    assert record.candidate.root.children[0].label == "甲"
    assert record.checkpointed_at is not None


def test_checkpoint_does_not_touch_a_finished_run():
    """收尾之后迟到的检查点不许把成品盖成半棵树。

    这是 `WHERE status = 'running'` 唯一的用途：写说明那 26 秒里进程可能刚好在被
    取消之后，工具里的检查点调用会晚一步到达。
    """
    runs.begin("cp-2", {"goal": "g"})
    full = MindMapContent.model_validate({**TREE,
                                          "root": {**TREE["root"], "children": [
                                              *TREE["root"]["children"],
                                              {"id": "d", "label": "丁", "children": []}]}})
    runs.finish("cp-2", runs.STATUS_DONE, candidate=full, mode="model")

    assert runs.checkpoint("cp-2", CANDIDATE) is False
    record = runs.get("cp-2")
    assert record.status == runs.STATUS_DONE
    assert record.checkpointed_at is None
    assert len(record.candidate.root.children) == 4


def test_finish_clears_the_checkpoint_mark():
    """交出成品那一刻，"这只是半棵树"的标记要清掉。

    不清的话，界面会一直按"说明还没写"提示一份完整的生成结果。
    """
    runs.begin("cp-3", {"goal": "g"})
    runs.checkpoint("cp-3", CANDIDATE)
    assert runs.get("cp-3").checkpointed_at is not None

    runs.finish("cp-3", runs.STATUS_DONE, candidate=CANDIDATE, mode="model")

    record = runs.get("cp-3")
    assert record.checkpointed_at is None
    assert record.status == runs.STATUS_DONE


def test_finish_without_a_candidate_keeps_the_checkpoint():
    """收尾没交出候选时，检查点那份要留着（`COALESCE`）。

    取消路径就是这样：树还在生成，客户端断了，`finish(candidate=None)` 只写库状态，
    这份检查点正是用户下次进来能捡到的东西。
    """
    runs.begin("cp-4", {"goal": "g"})
    runs.checkpoint("cp-4", CANDIDATE)

    runs.finish("cp-4", runs.STATUS_CANCELLED)

    record = runs.get("cp-4")
    assert record.candidate is not None
    assert record.checkpointed_at is not None


def test_sweep_turns_a_stale_running_row_into_a_recoverable_failure():
    """崩溃留下的 `running` 化石要么永远 409、要么永远没人捡，必须清掉。

    清成 `failed` 之后 `begin()` 才认领得动它，`pending()` 也才列得出来——清理的
    意义不是"让表干净"，是让那棵树重新变成用户能看见的东西。
    """
    runs.begin("stale-1", {"goal": "g"})
    runs.checkpoint("stale-1", CANDIDATE)
    # 把 `updated_at` 推到很久以前，模拟"进程在那之后再没动过"。
    conn = runs._connect()
    try:
        conn.execute("UPDATE runs SET updated_at = '2020-01-01T00:00:00.000Z' "
                     "WHERE run_id = 'stale-1'")
        conn.commit()
    finally:
        conn.close()

    swept = runs.sweep_abandoned()

    assert "stale-1" in swept
    record = runs.get("stale-1")
    assert record.status == runs.STATUS_FAILED
    assert record.failure["code"] == "run_abandoned"
    # 检查点本身不动：清理的是状态，不是那棵树。
    assert record.candidate is not None
    assert record.checkpointed_at is not None


def test_sweep_leaves_a_fresh_running_row_alone():
    """刚开的那一轮不许被当成化石清掉。"""
    runs.begin("fresh-1", {"goal": "g"})

    assert runs.sweep_abandoned() == []
    assert runs.get("fresh-1").status == runs.STATUS_RUNNING


def test_abandoned_run_shows_up_in_the_list_with_its_node_count():
    """被清掉的那一行要能在列表里看到，并且带上"已经存了多少节点"。"""
    runs.begin("stale-2", {"goal": "崩溃前那次", "intent": "create"})
    runs.checkpoint("stale-2", CANDIDATE)
    conn = runs._connect()
    try:
        conn.execute("UPDATE runs SET updated_at = '2020-01-01T00:00:00.000Z' "
                     "WHERE run_id = 'stale-2'")
        conn.commit()
    finally:
        conn.close()
    runs.sweep_abandoned()

    rows = client.get("/api/agent/runs").json()

    row = next(item for item in rows if item["run_id"] == "stale-2")
    assert row["status"] == "failed"
    assert row["checkpointed_at"] is not None
    assert row["node_count"] == 4
    # 它不是半棵树：结构是全的，缺的只是说明。
    assert row["partial"] is None


def test_beginning_a_fresh_run_cleans_up_abandoned_ones():
    """开新 run 时顺手清理，不需要后台任务也不需要启动钩子。"""
    runs.begin("stale-3", {"goal": "g"})
    conn = runs._connect()
    try:
        conn.execute("UPDATE runs SET updated_at = '2020-01-01T00:00:00.000Z' "
                     "WHERE run_id = 'stale-3'")
        conn.commit()
    finally:
        conn.close()

    runs.begin("brand-new", {"goal": "g"})

    assert runs.get("stale-3").status == runs.STATUS_FAILED
    assert runs.get("brand-new").status == runs.STATUS_RUNNING


def test_reopening_a_run_clears_the_checkpoint_mark():
    """用户重试同一轮时，上一轮留下的检查点标记不能再挂着。

    不清的话，新一轮跑到收尾之前界面会按"说明是空的"提示，而那时的树是新一轮的。
    """
    runs.begin("cp-5", {"goal": "g"})
    runs.checkpoint("cp-5", CANDIDATE)
    runs.finish("cp-5", runs.STATUS_CANCELLED)

    claim = runs.begin("cp-5", {"goal": "g"})

    assert claim.claimed is True
    assert claim.record.checkpointed_at is None
    assert claim.record.status == runs.STATUS_RUNNING


def _age(run_id: str) -> None:
    """把一行推成"很久没动过"的化石（真按时间等是等不起的）。"""
    conn = runs._connect()
    try:
        conn.execute("UPDATE runs SET updated_at = '2020-01-01T00:00:00.000Z' "
                     f"WHERE run_id = '{run_id}'")
        conn.commit()
    finally:
        conn.close()


def test_sweep_leaves_an_active_run_alone():
    """本进程还在跑的那一轮，`updated_at` 再旧也不许判成化石。

    一轮生成长时间不出帧是正常的：单次模型调用受 `max_seconds` 约束（上限 3600
    秒），比 `RUN_ABANDONED_SECONDS`(1800) 还长。只按时间判就会把这种轮次误杀，
    而误杀之后它后面的 `checkpoint()` 带 `WHERE status = 'running'`，从此全部
    静默失效——用户下次进来看到的是一棵停在某一刻、再也不会更新的树。
    """
    runs.begin("live-1", {"goal": "g"})
    runs.active("live-1")
    try:
        _age("live-1")

        assert runs.sweep_abandoned() == []
        assert runs.get("live-1").status == runs.STATUS_RUNNING
        # 误杀之后这一句会返回 False：检查点只能写 running 的行。
        assert runs.checkpoint("live-1", CANDIDATE) is True
    finally:
        runs.inactive("live-1")

    # 注销之后它就只是一行普通的化石，该被扫到。
    # （先重新推老：上面那个成功的 `checkpoint()` 刚把 `updated_at` 刷新过。）
    _age("live-1")
    assert "live-1" in runs.sweep_abandoned()
    assert runs.get("live-1").status == runs.STATUS_FAILED


def test_sweep_keeps_cleaning_other_rows_while_one_is_active():
    """排除活跃的只是那几行，不是整次清理——僵尸行仍要被收掉。"""
    runs.begin("live-2", {"goal": "g"})
    runs.begin("dead-1", {"goal": "g"})
    runs.active("live-2")
    try:
        _age("live-2")
        _age("dead-1")

        assert runs.sweep_abandoned() == ["dead-1"]
    finally:
        runs.inactive("live-2")


def test_the_stream_registers_the_run_and_unregisters_it_on_the_way_out():
    """`_frames` 自己登记、自己注销：漏了注销，那一行就永远不会被清理。"""
    request = AgentRunRequest(goal="g", intent="create")
    seen_active: list[bool] = []

    async def fake_run(body, *, on_close=None, on_checkpoint=None, first_seq=1):
        # 跑到一半时应当已经登记过了——清扫正是可能在别处此刻发生的。
        seen_active.append("stream-1" in runs._active_ids())
        yield DoneEvent(seq=1, content=CANDIDATE, mode="model")

    async def scenario() -> None:
        agen = agent_router._frames(request, "stream-1")
        async for _chunk in agen:
            pass

    _open_run("stream-1", request)
    with patch("app.agent.router.run", new=fake_run):
        asyncio.run(scenario())

    assert seen_active == [True], "生成期间这一轮必须在活跃登记里"
    assert "stream-1" not in runs._active_ids(), "收尾之后必须注销，否则永远不会被清理"
    assert runs.get("stream-1").status == runs.STATUS_DONE


def test_a_stream_that_never_starts_leaves_no_registration():
    """开流前就断开时生成器一次都不跑，不能留下一条永远没人注销的登记。"""
    request = AgentRunRequest(goal="g", intent="create")
    _open_run("stream-2", request)

    async def scenario() -> None:
        agen = agent_router._frames(request, "stream-2")
        # 一次都不迭代就直接关掉（uvicorn 收到断开时就是这样）。
        await agen.aclose()

    with patch("app.agent.router.run", new=lambda *a, **k: None):
        asyncio.run(scenario())

    assert "stream-2" not in runs._active_ids()


def test_checkpoint_is_written_off_the_event_loop():
    """检查点那次写库必须落在工作线程上。

    工具是 async 的、跑在事件循环上；同步写 SQLite 等于拿整个循环去等一把写锁
    （`BEGIN IMMEDIATE` 撞上别的写入时最长等满忙等上限），同一进程里别的 SSE 流
    会跟着一帧都发不出来。这条用例钉的是"没在主线程写"，不是"写成功了"。
    """
    request = AgentRunRequest(goal="g", intent="create")
    threads: list[str] = []
    real_checkpoint = runs.checkpoint

    def spy(run_id: str, content: Any) -> bool:
        threads.append(threading.current_thread().name)
        return real_checkpoint(run_id, content)

    async def fake_run(body, *, on_close=None, on_checkpoint=None, first_seq=1):
        assert on_checkpoint is not None, "路由层必须把检查点通道接上"
        await on_checkpoint(CANDIDATE)
        yield DoneEvent(seq=1, content=CANDIDATE, mode="model")

    async def scenario() -> None:
        agen = agent_router._frames(request, "cp-thread-1")
        async for _chunk in agen:
            pass

    _open_run("cp-thread-1", request)
    loop_thread = threading.current_thread().name
    with patch("app.agent.router.run", new=fake_run), \
            patch("app.agent.runs.checkpoint", new=spy):
        asyncio.run(scenario())

    assert len(threads) == 1
    assert threads[0] != loop_thread, "检查点写库落在了事件循环线程上"
    assert runs.get("cp-thread-1").status == runs.STATUS_DONE


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------------- 迁移的并发安全（CI 红过一次）
#
# `_ensure_columns` 是 check-then-act：`PRAGMA` 查到列不存在，然后 `ALTER`。
# 两个连接同时进来时两边都查到"不存在"，一个成功、另一个抛
# `duplicate column name`。本地 Windows 上时序恰好躲过去了，CI 的 Linux 上
# `test_eight_simultaneous_begin_calls_start_exactly_one_run` 必红。
#
# SQLite 没有 `ADD COLUMN IF NOT EXISTS`，只能 catch。这两条钉住的是：
# "同一次迁移跑两遍不抛"，以及"别的 ALTER 错误仍然要抛"。


def test_ensure_columns_survives_a_racing_second_adder(tmp_path) -> None:
    """模拟竞态：PRAGMA 说没有，实际别人已经加好了。

    直接连调两次是不够的 —— 那时 PRAGMA 会看到列、走 `continue`，catch 分支
    根本走不到。所以用一个代理把 `PRAGMA table_info` 的答案骗成"什么都没有"，
    强制它真的去 ALTER 一个已存在的列 —— 这正是 CI 上八个并发 `begin()` 撞出来
    的那条路。（`sqlite3.Connection.execute` 是 C 方法，`patch.object` 换不掉它，
    所以用代理而不是 mock。）
    """
    from app.mindmaps.repository import _ensure_columns

    class _BlindPragma:
        """除了 `PRAGMA table_info` 之外，一切都转给真连接。"""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kwargs):
            if sql.startswith("PRAGMA table_info"):
                return iter(())          # 骗它："这一列都没有"
            return self._real.execute(sql, *args, **kwargs)

    conn = sqlite3.connect(tmp_path / "probe.db")
    conn.execute("CREATE TABLE probe (id TEXT PRIMARY KEY)")
    conn.execute("ALTER TABLE probe ADD COLUMN served_model TEXT")   # 别人先加好了

    _ensure_columns(_BlindPragma(conn), "probe", {"served_model": "TEXT"})   # 不该抛

    columns = {row[1] for row in conn.execute("PRAGMA table_info(probe)")}
    assert "served_model" in columns
    conn.close()


def test_ensure_columns_reraises_errors_that_are_not_duplicates(tmp_path) -> None:
    """只吞 `duplicate column name`。

    表名拼错要继续抛 —— 否则迁移出问题时会被静默跳过，而缺列的后果是
    "事件全部静默丢失"（`_ensure_columns` 的 docstring 里写着）。
    """
    from app.mindmaps.repository import _ensure_columns

    conn = sqlite3.connect(tmp_path / "probe2.db")
    conn.execute("CREATE TABLE probe (id TEXT PRIMARY KEY)")
    with pytest.raises(sqlite3.OperationalError):
        _ensure_columns(conn, "no_such_table", {"x": "TEXT"})
    conn.close()
