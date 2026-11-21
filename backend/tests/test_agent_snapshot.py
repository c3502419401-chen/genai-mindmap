"""`AgentRunRequest.snapshot` 是**三种图型**的联合：鱼骨与流程的 expand / edit 必须可达。

这一组用例的来历是一个真实的死路：`snapshot` 曾经收窄成 `MindMapContent`，而同一轮
的 `snapshot` / `done` 事件早就放宽到了 `DiagramContent`。后果是鱼骨图与流程图发
`expand` / `edit` **必然 422**——后端为它们写的 `_expand` / `_apply_ops_any`（按骨 /
阶段分片执行、有空子树、有诊断）在用例里是绿的，对前端却不可达。

放宽之后要逐条确认的是**语义**，不是类型：哪几项真的成立、哪几项不成立，不成立的那
一项必须挡在 422 之前并给人话。所以这四组分别钉：

  1. 三种形状都能被请求层收下（树那一档逐字节不变）；
  2. 认不出形状的 payload 给一句人话，而不是一棵 `anyOf` 错误树；
  3. `_expand` / `expandable` 在鱼骨与流程上成立——展开的是"骨下的原因 / 阶段里的
     步骤"，三态按各子树的层深算；
  4. `edit` 里**成立**的部分（同骨/同阶段的一批 op）与**不成立**的部分
     （`add_edge` / `remove_edge`）分开：后者明确拒绝，因为借道执行器时那条边会
     静默丢在临时树里。
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.agent import shapes
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext
from app.agent.router import _validate
from app.agent.service import _system_text
from app.agent.tools import (
    AddEdgeArgs,
    EditArgs,
    ExpandArgs,
    _add_edge,
    _edit,
    _expand,
)
from app.contracts.mindmap import (
    FishboneContent,
    FlowContent,
    MindMapContent,
)

GOAL = "把这张图补完"


def _node(node_id: str, label: str, children: list | None = None) -> dict[str, Any]:
    return {"id": node_id, "label": label, "children": children or []}


def _fishbone_payload() -> dict[str, Any]:
    return {
        "title": "报销慢",
        "problem": "为什么报销要 11 天",
        "bones": [
            {"category": "人", "root": _node("h1", "人", [_node("h1-1", "审批人休假")])},
            {"category": "法", "root": _node("l1", "法", [_node("l1-1", "流程太长")])},
        ],
    }


def _flow_payload() -> dict[str, Any]:
    return {
        "title": "注册流程",
        "stages": [
            {"id": "s_1", "root": _node("a", "提交", [_node("a1", "填表")])},
            {"id": "s_2", "root": _node("b", "审核", [_node("b1", "核验")])},
        ],
        "edges": [{"id": "e1", "source": "s_1", "target": "s_2", "kind": "flow"}],
    }


def _ctx(snapshot: Any, *, intent: str = "edit", selected: list[str] | None = None,
         ) -> RunContext:
    request = AgentRunRequest(
        goal=GOAL, intent=intent, snapshot=snapshot,
        selected_ids=selected or [],
        budget=RunBudget(max_model_calls=4, max_output_tokens=32768),
    )
    # `service.run` 就是这么初始化的：候选图 = 请求带来的那张图。
    return RunContext(request=request, candidate=request.snapshot)


class TestRequestAcceptsThreeShapes:
    def test_tree_snapshot_is_still_a_tree(self):
        """树这一档**零改动**：旧的树 payload 仍然落在 `MindMapContent` 上。"""
        request = AgentRunRequest(goal=GOAL, intent="edit", snapshot={
            "title": "t", "root": _node("r", "t", [_node("c", "分支")])})

        assert isinstance(request.snapshot, MindMapContent)

    def test_fishbone_snapshot_is_accepted(self):
        request = AgentRunRequest(goal=GOAL, intent="edit",
                                  snapshot=_fishbone_payload())

        assert isinstance(request.snapshot, FishboneContent)
        assert request.snapshot.problem == "为什么报销要 11 天"

    def test_flow_snapshot_is_accepted(self):
        request = AgentRunRequest(goal=GOAL, intent="edit", snapshot=_flow_payload())

        assert isinstance(request.snapshot, FlowContent)
        assert [stage.id for stage in request.snapshot.stages] == ["s_1", "s_2"]

    def test_unknown_shape_gets_one_readable_sentence(self):
        """认不出形状：一句人话，而不是 pydantic 的三层 `anyOf` 错误树。"""
        with pytest.raises(ValidationError) as caught:
            AgentRunRequest(goal=GOAL, intent="edit", snapshot={"diagram_type": "tree"})

        message = str(caught.value)
        assert "不是一份导图" in message
        # 联合类型的默认报错会长出一串 `anyOf` / `field required`：用户从里面读不出
        # "我该改什么"，而这条预检的全部价值就是不让他看到那些。
        assert "anyOf" not in message

    def test_intent_gates_still_hold(self):
        """请求层的两条意图约束与图型无关：新建不带图、改图必须带图。"""
        with pytest.raises(HTTPException):
            _validate(AgentRunRequest(goal=GOAL, intent="create",
                                      snapshot=_flow_payload()))
        with pytest.raises(HTTPException):
            _validate(AgentRunRequest(goal=GOAL, intent="edit"))
        # 鱼骨 / 流程带 snapshot 时不再被这一层拦——这正是放宽的意义。
        _validate(AgentRunRequest(goal=GOAL, intent="edit",
                                  snapshot=_fishbone_payload()))


class TestSystemTextWorksOnEveryShape:
    def test_outline_lists_bones_and_stages(self):
        """system 里那份"当前导图"对三种结构都列得出来（模型据此引用节点 id）。"""
        bone_text = _system_text(
            AgentRunRequest(goal=GOAL, intent="expand",
                            snapshot=_fishbone_payload()),
            AgentRunRequest(goal=GOAL, intent="expand",
                            snapshot=_fishbone_payload()).snapshot)
        flow_text = _system_text(
            AgentRunRequest(goal=GOAL, intent="expand", snapshot=_flow_payload()),
            AgentRunRequest(goal=GOAL, intent="expand",
                            snapshot=_flow_payload()).snapshot)

        assert "[h1-1] 审批人休假" in bone_text
        assert "[b1] 核验" in flow_text


class TestExpandOnNonTreeShapes:
    def _expand_with(self, snapshot: Any, node_id: str) -> tuple[Any, RunContext]:
        ctx = _ctx(snapshot, intent="expand", selected=[node_id])

        async def fake_chat(system: str, user: str, *, max_tokens: int | None = None,
                            on_delta: Any = None) -> dict:
            return {"children": [{"label": "补出来的一步"}]}

        with patch("app.agent.tools.chat_async", new=fake_chat):
            result = asyncio.run(_expand(ExpandArgs(node_id=node_id, count=2), ctx))
        return result, ctx

    def test_a_reason_can_be_added_under_a_bone(self):
        """鱼骨：展开一根骨 = 给这一类补**原因**，产物仍是 `FishboneContent`。"""
        result, ctx = self._expand_with(_fishbone_payload(), "h1")

        assert result.ok, result.content
        assert isinstance(ctx.candidate, FishboneContent)
        assert [child.label for child in shapes.find(ctx.candidate, "h1").children] == [
            "审批人休假", "补出来的一步"]

    def test_a_step_can_be_added_inside_a_stage(self):
        """流程：展开一个阶段 = 给它补**步骤**，阶段与阶段间的边都不受影响。"""
        result, ctx = self._expand_with(_flow_payload(), "b")

        assert result.ok, result.content
        assert isinstance(ctx.candidate, FlowContent)
        assert [child.label for child in shapes.find(ctx.candidate, "b").children] == [
            "核验", "补出来的一步"]
        # 阶段间那条边一个字节都没动。
        assert [(edge.source, edge.target) for edge in ctx.candidate.edges] == [
            ("s_1", "s_2")]

    def test_expandable_is_stamped_per_subtree(self):
        """`expandable` 三态在非树上同样成立：层数按**各子树**从 1 数。

        一根骨/一个阶段在画布上是从自己的根展开的一列，所以"第 8 层"这条覆盖规则
        必须按子树算——按整图算的话，鱼骨的六根骨会莫名多出五层预算。
        """
        _, ctx = self._expand_with(_fishbone_payload(), "h1")

        # 被展开的骨根还能继续展开；新加的叶子默认 `yes`（模型没说到底）。
        assert shapes.find(ctx.candidate, "h1").expandable == "yes"
        assert shapes.find(ctx.candidate, "补出来的一步") is None  # 它换了真实 id
        added = [node for node in shapes.walk_nodes(shapes.find(ctx.candidate, "h1"))
                 if node.label == "补出来的一步"]
        assert added and added[0].expandable == "yes"

    def test_declared_dead_end_becomes_no(self):
        """模型说"到底了"时落成 `no`——与树上同一条判据（`_stamp_expandable` 共用）。"""
        ctx = _ctx(_flow_payload(), intent="expand", selected=["b"])

        async def fake_chat(system: str, user: str, *, max_tokens: int | None = None,
                            on_delta: Any = None) -> dict:
            return {"children": [{"label": "最后一个动作", "expandable": False}]}

        with patch("app.agent.tools.chat_async", new=fake_chat):
            result = asyncio.run(_expand(ExpandArgs(node_id="b", count=2), ctx))

        assert result.ok, result.content
        added = [node for node in shapes.walk_nodes(shapes.find(ctx.candidate, "b"))
                 if node.label == "最后一个动作"]
        assert added[0].expandable == "no"


class TestEditOnNonTreeShapes:
    def test_a_batch_inside_one_stage_is_applied(self):
        """一批 op 落在**同一个阶段**里：成立（借道临时子树，写回原位）。"""
        ctx = _ctx(_flow_payload())
        args = EditArgs(ops=[
            {"op": "update", "id": "b1", "changes": {"label": "逐项核验"}},
            {"op": "add", "id": "$new1", "parent_id": "b", "label": "出结论"},
        ])

        result = asyncio.run(_edit(args, ctx))

        assert result.ok, result.content
        assert isinstance(ctx.candidate, FlowContent)
        assert shapes.find(ctx.candidate, "b1").label == "逐项核验"
        assert [child.label for child in shapes.find(ctx.candidate, "b").children] == [
            "逐项核验", "出结论"]

    def test_a_batch_across_two_stages_is_rejected_with_a_sentence(self):
        """跨阶段的一批 op → `cross_container` 人话（不是猜一个位置，也不是 422）。"""
        ctx = _ctx(_flow_payload())
        args = EditArgs(ops=[
            {"op": "update", "id": "a1", "changes": {"label": "甲"}},
            {"op": "update", "id": "b1", "changes": {"label": "乙"}},
        ])

        result = asyncio.run(_edit(args, ctx))

        assert not result.ok
        assert result.data["code"] == "cross_container"
        assert "每一个阶段是一棵独立的子树" in result.content

    def test_add_edge_lands_in_the_right_layer_of_a_flow(self):
        """`add_edge` 在流程图上**按层级落笔**，不再拒绝。

        它曾经明确拒绝（`edges_not_supported`），因为那时实现会**静默丢**：借道
        `apply_ops` 时边落在那棵临时 `MindMapContent` 的 `edges` 里，而写回只换子树
        根——工具回一句"已连接"，候选图里一条边都没有。让用户以为线画上了，比拒绝
        他严重得多。

        现在判层级这一层真的存在了（`ops.resolve_flow_edge_placement` 与生成侧的
        `shapes._split_edges` 同一套规则），所以这条路径上要守的东西变了：
        **同阶段的两个步骤落在 stage.edges、端点保持节点 id；跨阶段落在 content.edges、
        端点换成阶段 id**。落错数组的表现是"画出来多了一条主轴"，比丢掉更隐蔽。
        """
        ctx = _ctx(_flow_payload())
        # a1 与 b1 分属两个阶段 → 阶段之间的连线，端点换成 s_1 → s_2。
        result = asyncio.run(_add_edge(AddEdgeArgs(source="a1", target="b1", label="否"), ctx))

        assert result.ok, result.content
        assert isinstance(ctx.candidate, FlowContent)
        assert [(edge.source, edge.target) for edge in ctx.candidate.edges] == [
            ("s_1", "s_2"), ("s_1", "s_2")]
        assert result.data["edge_layer"] == "stage"
        assert "阶段之间" in result.content

    def test_add_edge_inside_one_stage_lands_in_that_stage(self):
        """同一阶段里的两个步骤 → 那个阶段的 `edges`，端点**保持节点 id**。"""
        ctx = _ctx(_flow_payload())
        result = asyncio.run(_add_edge(AddEdgeArgs(source="a1", target="a", label="退回"), ctx))

        assert result.ok, result.content
        assert isinstance(ctx.candidate, FlowContent)
        stage = ctx.candidate.stages[0]
        assert [(edge.source, edge.target, edge.label) for edge in stage.edges] == [
            ("a1", "a", "退回")]
        # 阶段之间那一层一个字节都没动（新增的只有原来那条）
        assert [(edge.source, edge.target) for edge in ctx.candidate.edges] == [("s_1", "s_2")]
        assert result.data["edge_layer"] == "inner"
        assert "阶段内边" in result.content

    def test_the_executor_itself_refuses_edge_ops_on_a_fishbone(self):
        """执行器自己也不接这一批：它不该在"写不回去"的路径上返回成功。

        工具层（`_add_edge`）已经有预检，这一条守的是**执行器**的下限：`_apply_ops_any`
        借道临时 `MindMapContent` 执行，边会落在那个临时对象的 `edges` 里，而写回
        只换子树根。哪天多一个入口直接构造 `OpBatch`，这条是唯一会报错的地方。
        """
        from app.agent.tools import _apply_ops_any
        from app.contracts.ops import OpBatch, OpError

        content = _ctx(_fishbone_payload()).candidate
        batch = OpBatch(batch_id="b1", base_revision=1, ops=[
            {"op": "add_edge", "id": "$e1", "source": "h1-1", "target": "l1-1"},
        ])

        with pytest.raises(OpError) as caught:
            _apply_ops_any(content, batch)

        assert caught.value.code == "edges_not_supported"
        assert caught.value.path == "ops.0"
