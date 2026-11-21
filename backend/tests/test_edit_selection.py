"""「选中节点 + 一句话」这条路的三层用例。

改这张图从"整图 + 一句话"变成"选中节点 + 修改意愿"，三个地方各有一层要害：

  1. **system prompt**：用户圈住的节点必须带着 id / 标题 / 层级路径进提示词。
     只把 id 写进去没有用——模型得知道"这三个节点分别在讲什么"，不然它仍然要
     从整棵树里猜哪三个是用户圈住的；
  2. **执行层**：选区是上下文提示，不是作用域。选中 1 个节点说「整张图都加上
     时间维度」时，模型改别处必须被接受（这一条用 scripted provider 走完整循环，
     断言没有 error 帧、候选真的变了）；
  3. **不回归**：`expand` 的选中逻辑与 `create` 一概不变，`edit` 的七个工具都在。

为什么"扩权"要单独钉一条：把选区写成硬约束是**看起来更安全、实际更糟**的那一侧。
用户说「整张图都加上时间维度」时手上常常还留着一个选区，硬拦会让模型回一句
"我只能改你选中的节点"，用户得先取消选区再问一遍——而真正要改的是全图。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import HTTPException

from app.agent.contracts import (
    AgentRunRequest,
    Completion,
    DoneEvent,
    ErrorEvent,
    RunBudget,
    ToolCall,
    ToolEndEvent,
)
from app.agent.router import _validate
from app.agent.service import (
    SELECTION_HEADER,
    SELECTION_RULE,
    _system_text,
    run,
    selection_text,
)
from app.agent.tools import build_registry
from app.contracts.mindmap import MindMapContent

TREE = {
    "title": "示例",
    "root": {
        "id": "root", "label": "根", "children": [
            {"id": "a", "label": "甲", "children": [
                {"id": "a1", "label": "甲一", "children": []},
            ]},
            {"id": "b", "label": "乙", "children": [
                {"id": "b1", "label": "乙一", "children": []},
            ]},
            {"id": "c", "label": "丙", "children": []},
        ],
    },
}


def _content() -> MindMapContent:
    return MindMapContent.model_validate(TREE)


def _request(intent: str = "edit", selected: list[str] | None = None,
             goal: str = "把这几个合并成一个") -> AgentRunRequest:
    return AgentRunRequest(
        goal=goal, intent=intent, selected_ids=selected or [],
        snapshot=None if intent == "create" else _content(),
        budget=RunBudget(max_model_calls=4),
    )


# ---------------------------------------------------------------- 1. prompt


def test_prompt_lists_id_label_and_path_for_each_selected_node():
    """选中三个节点：每条都要带 id、标题与从根到它的路径。"""
    text = selection_text(_request(selected=["a1", "b1", "c"]), _content())

    assert SELECTION_HEADER in text
    # id 与标题：模型要能把这句话对上大纲里的节点
    assert "[a1] 甲一" in text
    assert "[b1] 乙一" in text
    assert "[c] 丙" in text
    # 路径：同一个标题在不同分支下含义不同，少了路径它就不知道"甲一"在哪一支
    assert "路径：根 > 甲 > 甲一" in text
    assert "路径：根 > 乙 > 乙一" in text
    # 分寸那句话必须原样在
    assert SELECTION_RULE in text
    # **不是**硬约束：出现了就得是"不是范围限制"这句，而不是"只能改这些"
    assert "不是允许改动的范围限制" in text
    assert "只能改当前选中节点" not in text


def test_prompt_has_no_selection_section_when_nothing_is_selected():
    """没选中时**整段不出现**——不留一个空的「用户选中了：」。"""
    request = _request(selected=[])
    assert selection_text(request, _content()) == ""

    system = _system_text(request, _content())
    assert SELECTION_HEADER not in system
    # 连"选中"这个词都不该出现：出现了模型就会去找一个不存在的选区
    assert "选中" not in system
    # 大纲还在（这是另一件事，不能顺手一起删掉）
    assert "[a1] 甲一" in system


def test_unknown_ids_are_skipped_without_an_empty_header():
    """老前端带来的旧 id：查不到就跳过；全查不到时整段不出现。"""
    content = _content()
    mixed = selection_text(_request(selected=["ghost", "a"]), content)
    assert "[a] 甲" in mixed
    assert "ghost" not in mixed

    assert selection_text(_request(selected=["ghost"]), content) == ""


def test_expand_does_not_get_the_selection_section():
    """`expand` 的选中走它自己的那条路（`_expand` 把标题链写进工具调用）。"""
    assert selection_text(_request("expand", selected=["a"]), _content()) == ""
    # 但通用的大纲仍然在：那一段不是给 expand 专用的
    assert "[a] 甲" in _system_text(_request("expand", selected=["a"]), _content())


def test_create_has_no_selection_section():
    """`create` 时画布是空的，选区无从谈起。"""
    assert selection_text(_request("create", selected=["a"]), None) == ""


# ---------------------------------------------------------------- 2. 扩权


class _ScriptedProvider:
    """按脚本依次返回预设结果；脚本项可以是 `Completion` 或要抛的异常。"""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        # 每次调用的 system 都存下来：提示词有没有被刷新、有没有丢选区，靠它看
        self.systems: list[str] = []

    async def complete_tools(self, messages, tools, *, budget):
        self.systems.append(messages[0]["content"])
        if not self._script:
            raise AssertionError("脚本已用完，循环比预期多调了一次模型")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _completion(calls: list[ToolCall] | None = None, finish: str = "stop",
                content: str = "好的") -> Completion:
    calls = calls or []
    message: dict[str, Any] = {"role": "assistant", "content": content}
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


def _collect(request: AgentRunRequest, provider) -> list:
    async def drain():
        return [event async for event in run(request, provider=provider,
                                            tools=build_registry("edit"))]

    return asyncio.run(drain())


def test_wide_scope_edit_is_accepted_when_only_one_node_is_selected():
    """选中 1 个节点，但用户说的是「整张图都加上时间维度」：改别处必须被接受。

    这是本次改动最要紧的一条边界：`selected_ids` 是上下文提示，不是作用域。
    模型给了一次 `update_node` 指向未选中的 `c`，断言：
      - 工具成功（没有 `out_of_selection` 这种拒绝），候选真的变了；
      - 整条流里没有 error 帧，最后是 done；
      - 结果里带着"越界"提醒（模型据此在正文里交代原因），但那是提示不是失败。
    """
    call = ToolCall(call_id="c-1", name="update_node",
                    arguments={"node_id": "c", "detail": "加上时间维度：季度目标"})
    provider = _ScriptedProvider([_completion([call], finish="tool_calls"), _completion()])
    events = _collect(_request(selected=["a"], goal="整张图都加上时间维度"), provider)

    assert not [event for event in events if isinstance(event, ErrorEvent)]

    ends = [event for event in events if isinstance(event, ToolEndEvent)]
    assert [event.ok for event in ends] == [True]
    assert ends[0].result_summary["outside_selection"] == ["c"]

    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.content is not None
    node = next(child for child in done.content.root.children if child.id == "c")
    assert node.detail == "加上时间维度：季度目标"

    # 第二轮（模型收尾那一轮）的 system 里选区仍在：刷新大纲不能把这一段刷掉
    assert all(SELECTION_HEADER in system for system in provider.systems)


def test_in_scope_edit_carries_no_out_of_scope_hint():
    """选中的就是它：结果里 `outside_selection` 为空，正文不多一句提醒。"""
    call = ToolCall(call_id="c-1", name="update_node",
                    arguments={"node_id": "a", "label": "甲（改）"})
    provider = _ScriptedProvider([_completion([call], finish="tool_calls"), _completion()])
    events = _collect(_request(selected=["a"]), provider)

    ends = [event for event in events if isinstance(event, ToolEndEvent)]
    assert [event.ok for event in ends] == [True]
    assert ends[0].result_summary["outside_selection"] == []


# ---------------------------------------------------------------- 3. 不回归


def test_edit_intent_still_offers_all_eight_tools():
    """选中信息不该改变工具集：批量 + 六个细粒度 + 补说明，一个都不能少。"""
    names = build_registry("edit", has_library=False).names()
    assert len(names) == 8
    assert set(names) == {
        "add_node", "update_node", "remove_node", "move_node",
        "add_edge", "remove_edge", "mindmap_fill_details", "mindmap_edit",
    }


def test_create_registry_is_untouched():
    assert build_registry("create_deep", has_library=False).names() == ["mindmap_generate_deep"]


def test_expand_registry_is_untouched():
    """`expand` 的两个工具：展开 + 整图体检（`audit_tree`，只读、不产节点）。

    体检工具在这条路上是必需的：模型逐个 `node_expand` 展开时，"还差哪几支"
    没有任何代码在替它数，而它看到的大纲只列前 200 个节点。
    """
    assert build_registry("expand", has_library=False).names() == [
        "audit_tree", "node_expand"]


def test_router_accepts_many_selected_ids_for_edit():
    """三个选中 id 是这条路要支持的常态；开流前的校验只约束 expand。"""
    _validate(_request(selected=["a", "a1", "b"]))  # 不抛就算过


def test_router_still_requires_exactly_one_id_for_expand():
    with pytest.raises(HTTPException) as failure:
        _validate(_request("expand", selected=["a", "b"]))
    assert failure.value.status_code == 422
