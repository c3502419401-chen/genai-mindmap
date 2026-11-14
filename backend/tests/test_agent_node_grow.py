"""节点生长事件（`node_grow`）：增量提取、工具上报、SSE 帧与流式退回。

四块各自独立，失败原因也互不相同，所以分开测：
  - `stream_items` 只认「已经闭合的子项」：半截对象推出去，前端会看到文字被改写。
  - 工具只在开了上报通道时才走流式；通道关掉时行为必须和没有这个功能一样。
  - `chat_async` 的流式只是优化，任何一步不成立都要退回一次性请求。
  - 帧形状：根帧的 `path` 是 `null`，挂在根上的分支才是 `[]`。两者撞在一起时
    只能靠发送顺序区分，顺序一变前端就把第一个分支画成中心主题，而且不报错。

全部用假的 `chat_async` / 假 httpx 客户端驱动，不连网络也不消耗额度。
"""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import BaseModel

from app.agent.contracts import (
    AgentRunRequest,
    Completion,
    NodeGrowEvent,
    RunBudget,
    RunContext,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from app.agent.service import run
from app.agent.stream import encode_event
from app.agent.stream_items import closed_items
from app.agent.tools import (
    GENERATE_EVIDENCE_RULES,
    PLAN_PROMPT,
    ToolRegistry,
    build_registry,
)
from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source
from app.generation.service import (
    DETAIL_NO_SOURCE_RULES,
    DETAIL_PROMPT,
    RAG_SOURCE_PROMPT,
    RAG_THINKING_GUARD,
    GenerationError,
    chat_async,
    thinking_block,
)

TOPIC = "考研数学复习计划"


class _EchoArgs(BaseModel):
    """给"会超时的工具"用的最小参数模型。"""

    text: str = ""


# ---------------------------------------------------------------- 公共夹具


def _run(coro):
    return asyncio.run(coro)


def _ctx(emit=None, max_model_calls: int = 20,
         max_output_tokens: int = 32768) -> RunContext:
    request = AgentRunRequest(
        goal=TOPIC, intent="create",
        budget=RunBudget(max_model_calls=max_model_calls,
                         max_output_tokens=max_output_tokens),
    )
    return RunContext(request=request, emit=emit)


def _call(**arguments) -> ToolCall:
    return ToolCall(call_id="deep-1", name="mindmap_generate_deep", arguments=arguments)


def _collector() -> tuple[list[dict], Any]:
    """收上报帧的通道。返回 (帧列表, 通道函数)。"""
    frames: list[dict] = []

    async def emit(payload: dict) -> None:
        frames.append(dict(payload))

    return frames, emit


def _plan(*titles: str) -> dict:
    return {"branches": list(titles)}


def _branch_title(user: str) -> str:
    return user.split("本条一级分支：", 1)[1].split("\n", 1)[0]


def _streaming_chat(plan: dict, expand, chunk: int = 7):
    """假 `chat_async`：骨架返回 plan，其它按 `expand(user)` 返回，并逐段回调。

    按 `chunk` 个字符切段是为了让增量解析真的经历「对象写到一半」——一次给整个
    响应就测不到半截对象被推出去这件事。
    """

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None):
        body = plan if system.startswith(PLAN_PROMPT) else expand(user)
        if on_delta is not None:
            text = json.dumps(body, ensure_ascii=False)
            for start in range(0, len(text), chunk):
                await on_delta(text[start:start + chunk])
        return body

    return chat


def _paths(frames: list[dict]) -> list[Any]:
    return [frame["path"] for frame in frames]


def _keys(frames: list[dict]) -> list[tuple]:
    return [(None if frame["path"] is None else tuple(frame["path"]), frame["label"])
            for frame in frames]


# ---------------------------------------------------------------- 增量提取


class ClosedItemTest(unittest.TestCase):
    """哪些子项算「写完了」。"""

    def test_half_written_item_is_not_returned(self):
        """半截对象不推：推出去的那条文字随后还会变，前端只能重画。"""
        opened = '{"children":[{"label":"按章节通读教材","expandable":false},{"label":"做真题'
        items = closed_items(opened)
        self.assertEqual([item.label for item in items], ["按章节通读教材"])

        completed = opened + '","expandable":true}]}'
        self.assertEqual([item.label for item in closed_items(completed)],
                         ["按章节通读教材", "做真题"])

    def test_escaped_quote_and_chinese_survive(self):
        items = closed_items('{"children":[{"label":"说\\"先复习\\"再做题"}]}')
        self.assertEqual([item.label for item in items], ['说"先复习"再做题'])

    def test_nested_children_are_kept_in_order(self):
        """模型多给一层时，里层的顺序与外层一致，且父在前。"""
        items = closed_items(
            '{"children":[{"label":"甲","children":[{"label":"甲一"},{"label":"甲二"}]},'
            '{"label":"乙"}]}'
        )
        self.assertEqual([item.label for item in items], ["甲", "乙"])
        self.assertEqual([child.label for child in items[0].children], ["甲一", "甲二"])

    def test_expandable_only_counts_real_booleans(self):
        """字符串 "false"、0、没写，都算「没写」——它们与 false 不是一回事。"""
        items = closed_items(
            '{"children":[{"label":"真","expandable":false},'
            '{"label":"字符串","expandable":"false"},'
            '{"label":"数字","expandable":0},'
            '{"label":"没写"}]}'
        )
        self.assertEqual([item.expandable for item in items], [False, None, None, None])

    def test_items_without_usable_label_are_dropped(self):
        items = closed_items(
            '{"children":["纯字符串",{"label":""},{"label":"   "},{"label":7},'
            '{"label":"有效"}]}'
        )
        self.assertEqual([item.label for item in items], ["有效"])

    def test_label_named_children_does_not_confuse_the_scanner(self):
        """label 正好是 `children` 时不能把它当成新一层数组的起点。"""
        items = closed_items('{"children":[{"label":"children"},{"label":"后一个"}]}')
        self.assertEqual([item.label for item in items], ["children", "后一个"])

    def test_fenced_response_is_tolerated(self):
        items = closed_items('```json\n{"children":[{"label":"甲"}]}\n```')
        self.assertEqual([item.label for item in items], ["甲"])

    def test_empty_buffer_yields_nothing(self):
        self.assertEqual(closed_items(""), [])
        self.assertEqual(closed_items('{"children":['), [])


# ---------------------------------------------------------------- 工具上报


class DeepGrowReportTest(unittest.TestCase):
    """`mindmap_generate_deep` 在开了通道之后往外推什么、按什么顺序。"""

    def _run_deep(self, expand, *, max_depth: int = 2, chunk: int = 7):
        frames, emit = _collector()
        ctx = _ctx(emit=emit)
        chat = _streaming_chat(_plan("范围", "原理", "实现"), expand, chunk)
        call = _call(topic=TOPIC, branches=3, points_per_branch=2, max_depth=max_depth)
        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=chat)):
            result = _run(build_registry("create").execute(call, ctx))
        self.assertTrue(result.ok, result.content)
        return frames

    def test_root_frame_is_first_and_uses_null_path(self):
        """根帧的 path 必须是 null：用 [] 就与"挂在根上的分支"撞在一起了。"""
        frames = self._run_deep(lambda user: {"children": [{"label": "要点"}]})
        self.assertEqual(frames[0]["type"], "node_grow")
        self.assertIsNone(frames[0]["path"])
        self.assertEqual(frames[0]["label"], TOPIC)
        # 根没有模型给的 expandable，只能是 None（不是 False）
        self.assertIsNone(frames[0]["expandable"])

    def test_branches_hang_under_the_root(self):
        frames = self._run_deep(lambda user: {"children": [{"label": "要点"}]})
        branches = frames[1:4]
        self.assertEqual([frame["label"] for frame in branches],
                         ["范围", "原理", "实现"])
        self.assertEqual(_paths(branches), [[], [], []])

    def test_points_carry_their_branch_index(self):
        """`path` 指向父节点：同一条分支下的要点共用 `[分支下标]`。

        位置信息不进 `path`——协议里 `path` 是父节点的索引链，子项之间的先后
        顺序由 `children` 数组的顺序决定，重复靠 `(path, label)` 去重。
        """
        frames = self._run_deep(
            lambda user: {"children": [{"label": f"{_branch_title(user)}要点一"},
                                       {"label": f"{_branch_title(user)}要点二"}]}
        )
        keys = set(_keys(frames))
        for index, title in enumerate(["范围", "原理", "实现"]):
            self.assertIn(((index,), f"{title}要点一"), keys)
            self.assertIn(((index,), f"{title}要点二"), keys)

    def test_deeper_level_extends_the_chain(self):
        """第三层的 path 是父节点的完整索引链，不是只有一层。"""
        def expand(user: str) -> dict:
            if "本节点：" in user:
                return {"children": [{"label": "更细的一条"}]}
            return {"children": [{"label": f"{_branch_title(user)}要点一",
                                  "expandable": True},
                                 {"label": f"{_branch_title(user)}要点二",
                                  "expandable": False}]}

        frames = self._run_deep(expand, max_depth=3)
        deep = [frame for frame in frames if frame["path"] and len(frame["path"]) == 2]
        # 每条分支的第一个要点都可展开，所以第三层挂在 [0,0] / [1,0] / [2,0]
        self.assertEqual(sorted(frame["path"] for frame in deep),
                         [[0, 0], [1, 0], [2, 0]])
        self.assertEqual([frame["label"] for frame in deep], ["更细的一条"] * 3)

    def test_order_is_parent_before_child(self):
        """父节点的帧一定早于它的孩子：前端据此直接挂，不用先缓存。"""
        def expand(user: str) -> dict:
            if "本节点：" in user:
                return {"children": [{"label": "更细的一条"}]}
            return {"children": [{"label": f"{_branch_title(user)}要点",
                                  "expandable": True}]}

        frames = self._run_deep(expand, max_depth=3)
        depths = [-1 if frame["path"] is None else len(frame["path"]) for frame in frames]
        self.assertEqual(depths, sorted(depths))
        self.assertEqual(depths[0], -1)   # 根在最前面
        self.assertEqual(depths[-1], 2)   # 最深那层的帧确实存在，否则上面那句是空断言

    def test_half_written_items_are_not_reported(self):
        """对象还没闭合时一帧都不该出：出了就代表文字随后会变。"""
        frames, emit = _collector()
        ctx = _ctx(emit=emit)
        deltas: list[tuple[int, int]] = []

        async def chat(system: str, user: str, *, max_tokens=None, on_delta=None):
            if system.startswith(PLAN_PROMPT):
                return _plan("范围", "原理", "实现")
            if on_delta is None:
                raise AssertionError("开了通道就应该走流式")
            before = len(frames)
            await on_delta('{"children":[{"label":"半截')
            middle = len(frames)
            await on_delta('的要点","expandable":false}]}')
            deltas.append((middle - before, len(frames) - middle))
            return {"children": [{"label": "半截的要点", "expandable": False}]}

        call = _call(topic=TOPIC, branches=3, points_per_branch=2, max_depth=2)
        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=chat)):
            result = _run(build_registry("create").execute(call, ctx))

        self.assertTrue(result.ok, result.content)
        # 三次首轮展开各自：半截时不新增，闭合后正好新增一条
        self.assertEqual(deltas[:3], [(0, 1), (0, 1), (0, 1)])
        # 补点那一轮回来的还是同一句文字、挂在同一个父节点下：去重之后一条都不该再发
        self.assertEqual(len(deltas), 6)
        self.assertEqual(deltas[3:], [(0, 0)] * 3)

    def test_duplicate_labels_are_reported_once(self):
        """同一个父节点下重复的文字只报一次——最终树里本来就会去重。"""
        frames = self._run_deep(
            lambda user: {"children": [{"label": "重复的要点"}, {"label": "重复的要点"}]}
        )
        duplicated = [frame for frame in frames if frame["label"] == "重复的要点"]
        # 三条分支各回两条一模一样的：每条分支只该剩一帧
        self.assertEqual(len(duplicated), 3)
        self.assertEqual(sorted(frame["path"] for frame in duplicated),
                         [[0], [1], [2]])
        self.assertEqual(len(_keys(frames)), len(set(_keys(frames))))

    def test_expandable_flag_reaches_the_frame(self):
        frames = self._run_deep(
            lambda user: {"children": [{"label": "能再拆", "expandable": True},
                                       {"label": "拆到底了", "expandable": False}]}
        )
        flags = {frame["label"]: frame["expandable"] for frame in frames}
        self.assertIs(flags["能再拆"], True)
        self.assertIs(flags["拆到底了"], False)

    def test_nodes_cut_by_the_slot_quota_are_never_reported(self):
        """被 `_trim_slots` 裁掉的候选**不许**上报——它们从来没进过树。

        `points_per_branch=2` 时这一路只接纳前 2 个节点，模型多给的第 3 个会在
        合并前被裁掉。旧行为是照单全收地报，界面上表现为"长出来又消失"：用户看着
        一个节点出现，几秒后整棵树里没有它，而任何日志都不会提这件事。
        """
        frames = self._run_deep(
            lambda user: {"children": [{"label": "留下的一"}, {"label": "留下的二"},
                                       {"label": "被裁的三"}]}
        )
        labels = {frame["label"] for frame in frames}
        self.assertIn("留下的一", labels)
        self.assertIn("留下的二", labels)
        self.assertNotIn("被裁的三", labels)

    def test_no_channel_means_no_streaming_request(self):
        """通道为 None：一次流式都不发，工具行为与加这个功能之前一样。"""
        calls: list[dict] = []

        async def chat(system: str, user: str, **kwargs):
            calls.append(kwargs)
            if system.startswith(PLAN_PROMPT):
                return _plan("范围", "原理", "实现")
            return {"children": [{"label": "要点一"}, {"label": "要点二"}]}

        ctx = _ctx(emit=None)
        call = _call(topic=TOPIC, branches=3, points_per_branch=2, max_depth=2)
        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=chat)):
            result = _run(build_registry("create").execute(call, ctx))

        self.assertTrue(result.ok, result.content)
        self.assertTrue(calls)
        for kwargs in calls:
            self.assertNotIn("on_delta", kwargs)

    def test_channel_switches_every_expansion_to_streaming(self):
        """对照：开了通道之后每一次展开调用都带回调（骨架不需要）。"""
        seen: list[bool] = []

        async def chat(system: str, user: str, *, max_tokens=None, on_delta=None):
            if system.startswith(PLAN_PROMPT):
                seen.append(on_delta is not None)
                return _plan("范围", "原理", "实现")
            seen.append(on_delta is not None)
            return {"children": [{"label": "要点一"}, {"label": "要点二"}]}

        _, emit = _collector()
        ctx = _ctx(emit=emit)
        call = _call(topic=TOPIC, branches=3, points_per_branch=2, max_depth=2)
        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=chat)):
            result = _run(build_registry("create").execute(call, ctx))

        self.assertTrue(result.ok, result.content)
        self.assertEqual(seen[0], False)          # 骨架：只关心结构，不必流式
        self.assertTrue(all(seen[1:]))


# ---------------------------------------------------------------- 帧形状


class NodeGrowFrameTest(unittest.TestCase):
    def test_root_frame_serializes_null_path(self):
        """根帧的 path 序列化成 `null`，不能变成 `[]` 也不能整键丢掉。"""
        frame = encode_event(NodeGrowEvent(seq=1, path=None, label=TOPIC,
                                           expandable=None))
        payload = json.loads(frame[6:].strip())
        self.assertIn('"path": null', frame)
        self.assertIsNone(payload["path"])
        # expandable 的 null 同样必须出现：缺键与"明确没写"分不开
        self.assertIsNone(payload["expandable"])

    def test_branch_frame_keeps_empty_path(self):
        frame = encode_event(NodeGrowEvent(seq=2, path=[], label="范围"))
        payload = json.loads(frame[6:].strip())
        self.assertEqual(payload["path"], [])
        self.assertEqual(payload["type"], "node_grow")

    def test_deep_path_is_preserved(self):
        frame = encode_event(NodeGrowEvent(seq=3, path=[0, 2], label="教材",
                                           expandable=False))
        self.assertEqual(json.loads(frame[6:].strip())["path"], [0, 2])


# ---------------------------------------------------------------- 循环里的帧


def _tool_completion(call: ToolCall) -> Completion:
    return Completion(
        message={
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name,
                             "arguments": json.dumps(call.arguments, ensure_ascii=False)},
            }],
        },
        tool_calls=[call],
        finish_reason="tool_calls",
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )


def _stop_completion() -> Completion:
    return Completion(message={"role": "assistant", "content": "好了"},
                      finish_reason="stop")


class _ScriptedProvider:
    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls = 0

    async def complete_tools(self, messages, tools, *, budget):
        self.calls += 1
        if not self._script:
            raise AssertionError("脚本已用完，循环比预期多调了一次模型")
        return self._script.pop(0)


class RunFramesTest(unittest.TestCase):
    """端到端：工具在执行期间产出帧，循环一边等一边发。"""

    def _events(self) -> list:
        def expand(user: str) -> dict:
            return {"children": [{"label": f"{_branch_title(user)}要点"}]}

        chat = _streaming_chat(_plan("范围", "原理", "实现"), expand)
        call = _call(topic=TOPIC, branches=3, points_per_branch=2, max_depth=2)
        provider = _ScriptedProvider([
            _tool_completion(call),
            _stop_completion(),
        ])
        request = AgentRunRequest(goal=TOPIC, intent="create",
                                  budget=RunBudget(max_model_calls=20))

        async def drain():
            return [event async for event in
                    run(request, provider=provider, tools=build_registry("create"))]

        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=chat)):
            return _run(drain())

    def test_grow_frames_share_the_event_counter(self):
        """seq 与其它帧共用一个计数器：前端靠它判断丢帧，另起一套就废了。"""
        events = self._events()
        self.assertEqual([event.seq for event in events],
                         list(range(1, len(events) + 1)))
        kinds = [event.type for event in events]
        self.assertIn("node_grow", kinds)
        # 帧序：工具开跑之后才有生长帧，工具结束帧在它们之后
        self.assertLess(kinds.index("tool_start"), kinds.index("node_grow"))
        self.assertLess(kinds.index("node_grow"), kinds.index("tool_end"))

    def test_grow_frames_are_exactly_the_tree_preview(self):
        events = self._events()
        grow = [event for event in events if event.type == "node_grow"]
        self.assertIsNone(grow[0].path)
        self.assertEqual(grow[0].label, TOPIC)
        self.assertEqual([event.path for event in grow[1:4]], [[], [], []])
        self.assertEqual([event.label for event in grow[1:4]], ["范围", "原理", "实现"])
        self.assertEqual({tuple(event.path) for event in grow[4:]},
                         {(0,), (1,), (2,)})

    def test_done_still_carries_the_final_tree(self):
        """预览是预览：最终内容仍然只在 done（及 snapshot）里给，且是整树。"""
        events = self._events()
        self.assertEqual(events[-1].type, "done")
        self.assertEqual(len(events[-1].content.root.children), 3)


class TimeoutWithUpdatesTest(unittest.TestCase):
    """工具在上报过帧之后超时：预览照常发出，`tool_end` 仍然必须补上。"""

    def test_timeout_still_pairs_tool_end(self):
        async def slow(args, ctx):
            await ctx.emit({"type": "node_grow", "path": None, "label": TOPIC})
            await asyncio.sleep(5)
            return ToolResult(ok=True, content="太慢了")  # pragma: no cover - 永远超时

        registry = ToolRegistry()
        registry.register(ToolSpec(name="slow", description="慢工具",
                                   args_model=_EchoArgs, fn=slow))
        call = ToolCall(call_id="slow-1", name="slow", arguments={})
        provider = _ScriptedProvider([_tool_completion(call)])
        request = AgentRunRequest(
            goal=TOPIC, intent="create",
            budget=RunBudget(max_model_calls=5, max_tool_calls=5, max_seconds=0.3),
        )

        async def drain():
            return [event async for event in
                    run(request, provider=provider, tools=registry)]

        events = _run(drain())
        self.assertEqual([event.type for event in events],
                         ["stage", "tool_start", "node_grow", "tool_end", "error"])
        # seq 连续：补帧不能打断前端判断丢帧的依据
        self.assertEqual([event.seq for event in events], list(range(1, 6)))
        self.assertEqual(events[-2].result_summary["code"], "tool_timeout")
        self.assertEqual(events[-1].limit, "max_seconds")


# ---------------------------------------------------------------- 流式与退回


BASE_ENV = {
    "MINDMAP_LLM_BASE_URL": "https://llm.example.test/v1",
    "MINDMAP_LLM_API_KEY": "test-key",
    "MINDMAP_LLM_MODEL": "test-model",
}


def _mock_client(handler):
    """把 `chat_async` 里新建的 httpx 客户端换成按 handler 应答的假客户端。"""
    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def build(*args, **kwargs):
        kwargs.pop("transport", None)
        return real(transport=transport, **kwargs)

    return patch("app.generation.service.httpx.AsyncClient", new=build)


def _sse(chunks: list[str], finish_reason: str | None = None,
         done: bool = True) -> bytes:
    parts = []
    for index, text in enumerate(chunks):
        choice: dict[str, Any] = {"delta": {"content": text}}
        # 末帧带 finish_reason，与真实服务端的形状一致
        if finish_reason is not None and index == len(chunks) - 1:
            choice["finish_reason"] = finish_reason
        parts.append("data: " + json.dumps({"choices": [choice]}, ensure_ascii=False) + "\n\n")
    if done:
        parts.append("data: [DONE]\n\n")
    return "".join(parts).encode()


class GenerateGrowTest(unittest.TestCase):
    """带 id 的整树响应也必须在候选树完成前上报，而且不能影响最终生成。"""

    def _ctx(self, emit=None, *, with_sources=True):
        sources = [Source(id="s1", kind="document", status="parsed", text="论文原文。")]
        if not with_sources:
            sources = []
        request = AgentRunRequest(
            goal="整理论文", intent="create", sources=sources,
            budget=RunBudget(max_output_tokens=1234),
        )
        return RunContext(
            request=request, emit=emit,
            sources_by_id={source.id: source for source in sources},
        )

    def _tree(self):
        return {"title": "论文", "root": {"id": "root", "label": "论文", "children": [
            {"id": "a", "label": "架构", "children": [
                {"id": "a1", "label": "注意力", "children": []},
            ]},
            {"id": "b", "label": "训练", "children": [
                {"id": "b1", "label": "优化器", "children": [
                    {"id": "b11", "label": "参数", "children": []},
                ]},
            ]},
            {"id": "c", "label": "效果", "children": []},
        ]}}

    def _execute(self, ctx):
        registry = build_registry("create", has_sources=bool(ctx.request.sources))
        if ctx.request.sources:
            # 本文件跑在默认形态（`rag`）下。`mindmap_generate_deep` 现在**在**注册表
            # 里（分片 + 按支检索那一轮放开了 discard），本文件固定走的是整树生成
            # 那条路，所以这里守住的是"整树生成仍然可用"，不再是"deep 被撤掉"。
            self.assertIn("mindmap_generate", registry)
        call = ToolCall(call_id="generate-1", name="mindmap_generate",
                        arguments={"topic": "整理论文"})
        return _run(registry.execute(call, ctx))

    def test_sourced_generate_streams_paths_before_candidate_is_ready(self):
        frames, emit = _collector()
        ctx = self._ctx(emit)
        tree = self._tree()
        tree["root"]["source_refs"] = [{"source_id": "s1", "quote": "论文原文。"}]

        async def chat(system, user, *, max_tokens, on_delta):
            # 默认形态是 `rag`：输入里没有整份正文，正文只能经检索命中进来
            # （生成前那一次主题检索，见 `tests/test_generate_evidence.py`）。
            # 这条断言同时守两件事——预览通道不依赖正文，以及拼装只有一处
            # （工具里是同一处拼装，但流式与非流式是两个入口）。
            self.assertTrue(system.startswith(RAG_SOURCE_PROMPT))
            self.assertIn("本次附带 1 份材料", user)
            manifest, _, evidence = user.partition("本次已对该主题做过一次检索")
            self.assertNotIn("论文原文。", manifest)
            self.assertIn("论文原文。", evidence)
            self.assertEqual(max_tokens, 1234)
            text = json.dumps(tree, ensure_ascii=False)
            for start in range(0, len(text), 7):
                await on_delta(text[start:start + 7])
            self.assertIsNone(ctx.candidate)
            self.assertEqual(_keys(frames), [
                (None, "论文"), ((), "架构"), ((0,), "注意力"),
                ((), "训练"), ((1,), "优化器"), ((1, 0), "参数"), ((), "效果"),
            ])
            self.assertTrue(all(frame["expandable"] is None for frame in frames))
            return tree

        with patch("app.agent.tools.chat_async", new=chat):
            result = self._execute(ctx)
        self.assertTrue(result.ok, result.content)
        self.assertEqual(result.data["node_count"], 7)
        # 引用校验用的仍是完整原文：提示词里没有正文，不代表核验也拿不到正文。
        self.assertEqual(ctx.candidate.root.source_refs[0].quote, "论文原文。")
        self.assertEqual(ctx.candidate.source_usage[0].source_id, "s1")
        self.assertEqual((ctx.revision, ctx.extra_model_calls, ctx.model_calls_used), (1, 1, 1))

    def test_topic_only_generate_also_receives_delta_callback(self):
        frames, emit = _collector()
        ctx = self._ctx(emit, with_sources=False)
        tree = self._tree()

        async def chat(system, user, *, max_tokens, on_delta):
            self.assertEqual(system, DETAIL_PROMPT + thinking_block("default")
                             + DETAIL_NO_SOURCE_RULES)
            self.assertEqual(user, "整理论文")
            await on_delta(json.dumps(tree, ensure_ascii=False))
            return tree

        with patch("app.agent.tools.chat_async", new=chat):
            result = self._execute(ctx)
        self.assertTrue(result.ok, result.content)
        self.assertEqual(len(frames), 7)

    def test_children_wait_for_late_parent_and_flush_in_arrival_order(self):
        frames, emit = _collector()
        ctx = self._ctx(emit)
        batches = [
            [("a", {"id": "a1", "label": "甲一", "children": []})],
            [("a", {"id": "a2", "label": "甲二", "children": []})],
            [("root", {"id": "a", "label": "甲", "children": []}),
             ("root", {"id": "b", "label": "乙", "children": []})],
            [(None, {"id": "root", "label": "论文", "children": []}),
             ("root", {"id": "c", "label": "丙", "children": []})],
        ]

        async def chat(system, user, *, max_tokens, on_delta):
            # 注入提取顺序异常：正常解析器会先给父节点，不能靠正常顺序覆盖缓存。
            for chunk in ("{}", " ", " "):
                await on_delta(chunk)
                self.assertEqual(frames, [])
            await on_delta(" ")
            return self._tree()

        with (patch("app.agent.tools.chat_async", new=chat),
              patch("app.agent.tools.collect_new_nodes", side_effect=batches)):
            result = self._execute(ctx)
        self.assertTrue(result.ok, result.content)
        self.assertEqual(_keys(frames), [
            (None, "论文"), ((), "甲"), ((0,), "甲一"), ((0,), "甲二"),
            ((), "乙"), ((), "丙"),
        ])

    def test_partial_label_and_id_are_not_reported(self):
        frames, emit = _collector()
        chunks = [
            '{"title":"论文","root":{"id":"root","label":',
            '"中心主题',
            '","children":[{"id":"a","label":"架构',
            '","children":[{"label":"注意力","id":"a',
            '1","children":[]}]},{"id":"b","label":"训练","children":[]},'
            '{"id":"c","label":"效果","children":[]}]}}',
        ]

        async def chat(system, user, *, max_tokens, on_delta):
            expected = [[], [], ["中心主题"], ["中心主题", "架构"],
                        ["中心主题", "架构", "注意力", "训练", "效果"]]
            for chunk, labels in zip(chunks, expected):
                await on_delta(chunk)
                self.assertEqual([frame["label"] for frame in frames], labels)
            return json.loads("".join(chunks))

        with patch("app.agent.tools.chat_async", new=chat):
            result = self._execute(self._ctx(emit))
        self.assertTrue(result.ok, result.content)

    def test_deduplicates_parent_path_and_normalized_label(self):
        frames, emit = _collector()
        tree = self._tree()
        tree["root"]["children"][0]["label"] = " 重复 "
        tree["root"]["children"][1]["label"] = "重复"
        tree["root"]["children"][2]["children"] = [
            {"id": "c1", "label": "重复", "children": []},
        ]

        async def chat(system, user, *, max_tokens, on_delta):
            for char in json.dumps(tree, ensure_ascii=False):
                await on_delta(char)
            # 多次解析同一缓冲区也不能重发，重复标题仍占到达顺序中的位置。
            await on_delta("")
            return tree

        with patch("app.agent.tools.chat_async", new=chat):
            result = self._execute(self._ctx(emit))
        self.assertTrue(result.ok, result.content)
        keys = _keys(frames)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(keys.count(((), "重复")), 1)
        self.assertIn(((2,), "重复"), keys)

    def test_no_emitter_preserves_both_one_shot_call_signatures(self):
        for with_sources in (True, False):
            with self.subTest(with_sources=with_sources):
                ctx = self._ctx(with_sources=with_sources)
                calls = []

                # 故意不接 on_delta：传 None 也会让旧调用约定失效。
                async def chat(system, user, *, max_tokens):
                    calls.append((system, user, max_tokens))
                    return self._tree()

                with patch("app.agent.tools.chat_async", new=chat):
                    result = self._execute(ctx)
                self.assertTrue(result.ok, result.content)
                self.assertEqual(len(calls), 1)
                system, user, max_tokens = calls[0]
                expected_system = (DETAIL_PROMPT + thinking_block("default")
                                   + DETAIL_NO_SOURCE_RULES)
                if with_sources:
                    # 默认形态是 `rag`：提示词与输入都换成"清单 + 命中"的那一套。
                    # 之前这里钉的是 `full`（sources 数组 + 整份正文），形态一换，
                    # 期望值就得跟着换——放宽成"以某个前缀开头"是没用的，那正好
                    # 会放过"两种形态拼错了"这一整类问题。
                    #
                    # 末尾多出的一句来自 `mindmap_generate` 生成前那次检索：主题
                    # "整理论文"与正文"论文原文。"共享词项，必有命中，所以提示词
                    # 要告诉模型"这次不用再检索"（`GENERATE_EVIDENCE_RULES`）。
                    expected_system = (RAG_SOURCE_PROMPT + thinking_block("default")
                                       + RAG_THINKING_GUARD + GENERATE_EVIDENCE_RULES)
                    self.assertIn("本次附带 1 份材料", user)
                    manifest, _, evidence = user.partition("本次已对该主题做过一次检索")
                    # 正文只能经检索命中进来，清单里一个字节都没有
                    self.assertNotIn("论文原文。", manifest)
                    self.assertIn("论文原文。", evidence)
                    self.assertNotIn("sources 数组", user)
                else:
                    self.assertEqual(user, "整理论文")
                self.assertEqual(system, expected_system)
                self.assertEqual(max_tokens, 1234)
                self.assertEqual(ctx.candidate.root, MindMapContent.model_validate(self._tree()).root)

    def test_stream_failures_keep_sourced_generation_successful(self):
        text = json.dumps(self._tree(), ensure_ascii=False)
        payload = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
        for failure in ("ignored", "empty", "interrupted", "invalid_json"):
            with self.subTest(failure=failure):
                bodies = []
                frames, emit = _collector()
                ctx = self._ctx(emit)

                def handler(request):
                    body = json.loads(request.content)
                    bodies.append(body)
                    if not body.get("stream") or failure == "ignored":
                        return httpx.Response(200, json=payload)
                    if failure == "empty":
                        return httpx.Response(200, content=_sse([]))
                    if failure == "interrupted":
                        return httpx.Response(200, content=_sse([text[:90]], done=False))
                    return httpx.Response(200, content=_sse(["不是 JSON"]))

                with patch.dict(os.environ, BASE_ENV), _mock_client(handler):
                    result = self._execute(ctx)
                self.assertTrue(result.ok, result.content)
                self.assertEqual(ctx.candidate.root.id, "root")
                self.assertEqual(len(bodies), 2)
                self.assertTrue(bodies[0]["stream"])
                self.assertNotIn("stream", bodies[1])
                self.assertEqual({key: value for key, value in bodies[0].items()
                                  if key != "stream"}, bodies[1])

    def test_emitter_failure_does_not_fail_sourced_generation(self):
        emit = AsyncMock(side_effect=RuntimeError("预览通道失效"))
        ctx = self._ctx(emit)
        text = json.dumps(self._tree(), ensure_ascii=False)

        def handler(request):
            return httpx.Response(200, content=_sse([text[:90], text[90:]]))

        with patch.dict(os.environ, BASE_ENV), _mock_client(handler):
            result = self._execute(ctx)
        self.assertTrue(result.ok, result.content)
        self.assertEqual(result.data["node_count"], 7)
        emit.assert_awaited_once()


class ChatAsyncStreamingTest(unittest.TestCase):
    """`chat_async` 传了 `on_delta` 时的行为与退回。"""

    def _call(self, handler, on_delta, *, body_sink: list[dict] | None = None):
        def wrapped(request: httpx.Request) -> httpx.Response:
            if body_sink is not None:
                body_sink.append(json.loads(request.content))
            return handler(request)

        with patch.dict(os.environ, BASE_ENV), _mock_client(wrapped):
            return _run(chat_async("系统", "用户", max_tokens=1234, on_delta=on_delta))

    def test_deltas_are_callbacked_and_result_is_parsed(self):
        seen: list[str] = []
        bodies: list[dict] = []

        async def on_delta(text: str) -> None:
            seen.append(text)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_sse(['{"children":[', '{"label":"甲"}]}']))

        result = self._call(handler, on_delta, body_sink=bodies)
        self.assertEqual(json.loads("".join(seen))["children"][0]["label"], "甲")
        self.assertEqual(result, {"children": [{"label": "甲"}]})
        self.assertEqual(len(bodies), 1)
        self.assertTrue(bodies[0]["stream"])
        # 流式请求必须带同一份 max_tokens：两条出口不能分叉
        self.assertEqual(bodies[0]["max_tokens"], 1234)

    def test_server_ignoring_stream_falls_back_to_one_shot(self):
        """服务端不认 stream、直接回整包 JSON：退回一次性请求，结果照常。"""
        seen: list[str] = []
        bodies: list[dict] = []

        async def on_delta(text: str) -> None:
            seen.append(text)

        payload = {"choices": [{"message": {"content": '{"children":[{"label":"甲"}]}'},
                                "finish_reason": "stop"}]}
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=payload)

        result = self._call(handler, on_delta, body_sink=bodies)
        self.assertEqual(result, {"children": [{"label": "甲"}]})
        self.assertEqual(seen, [])                       # 一段 delta 都没拿到
        self.assertEqual(len(requests), 2)               # 流式一次 + 退回一次
        self.assertTrue(bodies[0]["stream"])
        self.assertNotIn("stream", bodies[1])

    def test_stream_http_error_falls_back(self):
        """流式请求直接报错（例如服务端不认这个参数）：退回之后照样出结果。"""
        requests: list[httpx.Request] = []
        ok = {"choices": [{"message": {"content": '{"children":[{"label":"乙"}]}'},
                           "finish_reason": "stop"}]}

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json=ok)

        async def on_delta(text: str) -> None:  # pragma: no cover - 不会走到
            raise AssertionError("失败的流不该产出 delta")

        result = self._call(handler, on_delta)
        self.assertEqual(result, {"children": [{"label": "乙"}]})
        self.assertEqual(len(requests), 2)

    def test_callback_failure_does_not_fail_generation(self):
        """预览回调自己坏了：停掉后续回调，正文照收、结果照出。"""
        calls = {"n": 0}

        async def on_delta(text: str) -> None:
            calls["n"] += 1
            raise RuntimeError("回调炸了")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_sse(['{"children":[', '{"label":"甲"}]}']))

        result = self._call(handler, on_delta)
        self.assertEqual(result, {"children": [{"label": "甲"}]})
        self.assertEqual(calls["n"], 1)  # 第一段之后就不再回调

    def test_truncated_stream_is_salvaged_without_a_second_request(self):
        """finish_reason=length：回收已闭合的节点，并且**仍不重发请求**。

        旧行为是一律抛错（"硬边界"）。现在唯一的区别是那句"能回收就不失败"：
        被切的正文里已经有一个完整节点（`{"label":"甲"}` 闭合了），把它交出去
        比让整次生成报废更有用。而"不重发请求"这条没有变——重发的还是同一份
        被切的输出，多花一次钱、结果一样。
        """
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=_sse(['{"children":[{"label":"甲"}'],
                                                     finish_reason="length"))

        async def on_delta(text: str) -> None:
            return None

        result = self._call(handler, on_delta)
        self.assertEqual(list(result["children"]), [{"label": "甲"}])
        self.assertTrue(getattr(result, "partial", False))
        self.assertEqual(getattr(result, "recovered", 0), 1)
        self.assertEqual(len(requests), 1)

    def test_unrecoverable_truncated_stream_is_rejected(self):
        """切在 label 中间：一个完整节点都回收不出来，才判失败（同样不重发）。"""
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=_sse(['{"children":[{"label":"写了一半'],
                                                     finish_reason="length"))

        async def on_delta(text: str) -> None:
            return None

        with self.assertRaises(GenerationError) as caught:
            self._call(handler, on_delta)
        self.assertIn("截断", str(caught.exception))
        self.assertEqual(len(requests), 1)

    def test_unterminated_stream_falls_back(self):
        """流被切断（没有 [DONE]、没有 finish_reason）：半截正文不能当结果。

        这一条是给"补括号"这道工序上的保险：缺尾巴的 JSON 会被补齐成合法结构，
        不拦住就等于把一棵缺了半边的树当成功交出去。
        """
        requests: list[httpx.Request] = []
        ok = {"choices": [{"message": {"content": '{"children":[{"label":"丙"}]}'},
                           "finish_reason": "stop"}]}

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                # 括号能补齐，但没有任何结束标记
                return httpx.Response(200, content=_sse(['{"children":[{"label":"甲"}'],
                                                         done=False))
            return httpx.Response(200, json=ok)

        async def on_delta(text: str) -> None:
            return None

        result = self._call(handler, on_delta)
        self.assertEqual(result, {"children": [{"label": "丙"}]})
        self.assertEqual(len(requests), 2)

    def test_unparseable_stream_falls_back(self):
        """流里拿到的正文拼不成 JSON：退回一次性请求，而不是让生成失败。"""
        requests: list[httpx.Request] = []
        ok = {"choices": [{"message": {"content": '{"children":[{"label":"丙"}]}'},
                           "finish_reason": "stop"}]}

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, content=_sse(["不是 JSON"]))
            return httpx.Response(200, json=ok)

        async def on_delta(text: str) -> None:
            return None

        result = self._call(handler, on_delta)
        self.assertEqual(result, {"children": [{"label": "丙"}]})
        self.assertEqual(len(requests), 2)


if __name__ == "__main__":
    unittest.main()
