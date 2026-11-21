"""流程图的两层边：`add_edge` / `remove_edge` 先判层级，再落笔。

这一组用例守的是"**一条 op 落到哪个数组**"，因为两张数组出错的表现都很隐蔽：

  - 阶段内的判定出边落进 `FlowContent.edges`：契约会硬拒（端点必须是阶段 id），
    但更要紧的是**画布上它会变成一条主轴**——看起来像"提交 → 付款"那种跳转，
    而它其实是某一步上的「否」；
  - 阶段之间的跳转落进 `FlowStage.edges`：契约同样拒（端点必须是该阶段的节点），
    而报错只说某个"节点"不在阶段里，看不出是层级判错了。

判据与生成侧 `shapes._split_edges` 必须同源：同一对端点在"生成"和"编辑"两条路上
落进不同数组，会让同一张图在两次操作之后长出两种形状。
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agent import shapes
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext
from app.agent.ops import (
    INNER_LAYER,
    STAGE_LAYER,
    apply_flow_edge_ops,
    resolve_flow_edge_placement,
)
from app.agent.tools import (
    AddEdgeArgs,
    RemoveEdgeArgs,
    _add_edge,
    _apply_ops_any,
    _remove_edge,
)
from app.contracts.mindmap import FishboneContent, FlowContent
from app.contracts.ops import OpBatch, OpError


def _node(node_id: str, label: str, children: list | None = None) -> dict[str, Any]:
    return {"id": node_id, "label": label, "children": children or []}


def _flow_payload() -> dict[str, Any]:
    """两个阶段；第一阶段里有一个判定节点（两个出口）与一条步骤顺序跳转的位置。"""
    return {
        "title": "报销流程",
        "diagram_type": "flowchart",
        "stages": [
            {
                "id": "s_1",
                "root": _node("a", "审不审", [_node("a1", "通过"), _node("a2", "退回")]),
            },
            {"id": "s_2", "root": _node("b", "付款", [_node("b1", "打款")])},
        ],
        "edges": [{"id": "e1", "source": "s_1", "target": "s_2", "kind": "flow"}],
    }


def _flow() -> FlowContent:
    return FlowContent.model_validate(_flow_payload())


def _ctx(content: Any) -> RunContext:
    request = AgentRunRequest(
        goal="把连线补上", intent="edit", snapshot=content,
        budget=RunBudget(max_model_calls=4, max_output_tokens=32768),
    )
    return RunContext(request=request, candidate=request.snapshot)


class TestPlacement:
    """`resolve_flow_edge_placement`：一条边属于哪一层。"""

    def test_two_stage_ids_are_a_stage_transition(self):
        placement = resolve_flow_edge_placement(_flow(), "s_1", "s_2")
        assert placement.layer == STAGE_LAYER
        assert (placement.source, placement.target) == ("s_1", "s_2")
        assert placement.demoted is False

    def test_two_nodes_in_one_stage_are_inner(self):
        placement = resolve_flow_edge_placement(_flow(), "a", "a2")
        assert placement.layer == INNER_LAYER
        assert placement.stage_id == "s_1"
        # 端点保持**节点 id**：阶段内那一层的命名空间就是这个
        assert (placement.source, placement.target) == ("a", "a2")

    def test_depth_nodes_across_stages_are_demoted_to_stage_ids(self):
        """两个深层步骤跨阶段：降级成阶段级连线，**并标记 demoted**。

        与 `shapes._split_edges` 的 `demoted` 同一个取舍：`FlowContent.edges` 的端点
        只能是阶段 id，精确端点保不住，方向还在。标记它是为了让摘要说清"你的精确
        端点没有被保留"，而不是让模型以为写进去了。
        """
        placement = resolve_flow_edge_placement(_flow(), "a1", "b1")
        assert placement.layer == STAGE_LAYER
        assert (placement.source, placement.target) == ("s_1", "s_2")
        assert placement.demoted is True

    def test_mixing_a_stage_id_with_a_node_id_is_refused(self):
        with pytest.raises(OpError) as caught:
            resolve_flow_edge_placement(_flow(), "s_1", "a1")
        assert caught.value.code == "mixed_edge_namespace"
        assert "命名空间" in caught.value.message

    def test_unknown_endpoint_is_node_not_found(self):
        with pytest.raises(OpError) as caught:
            resolve_flow_edge_placement(_flow(), "a1", "ghost")
        assert caught.value.code == "node_not_found"
        assert caught.value.path.endswith(".target")


class TestExecutor:
    """`apply_flow_edge_ops`：整批原子，错误路径指得回原来那一条 op。"""

    def test_inner_add_writes_stage_edges_only(self):
        content = _flow()
        batch = OpBatch(batch_id="b1", base_revision=0, ops=[
            {"op": "add_edge", "id": "$e1", "source": "a", "target": "a2", "label": "否"},
        ])
        updated = apply_flow_edge_ops(
            content, [("ops.0", batch.ops[0])])

        assert [(edge.source, edge.target) for edge in updated.edges] == [("s_1", "s_2")]
        assert [(edge.source, edge.target, edge.label)
                for edge in updated.stages[0].edges] == [("a", "a2", "否")]

    def test_stage_add_keeps_node_ids_out_of_the_main_axis(self):
        content = _flow()
        op = OpBatch(batch_id="b1", base_revision=0, ops=[
            {"op": "add_edge", "id": "$e1", "source": "a1", "target": "b1", "label": "通过"},
        ]).ops[0]
        updated = apply_flow_edge_ops(content, [("ops.0", op)])

        assert [(edge.source, edge.target) for edge in updated.edges] == [
            ("s_1", "s_2"), ("s_1", "s_2")]
        assert updated.stages[0].edges == []

    def test_remove_searches_both_arrays(self):
        content = _flow()
        add = OpBatch(batch_id="b1", base_revision=0, ops=[
            {"op": "add_edge", "id": "e_inner", "source": "a", "target": "a2"},
        ]).ops[0]
        content = apply_flow_edge_ops(content, [("ops.0", add)])

        removed_inner = apply_flow_edge_ops(
            content, [("ops.0", OpBatch(batch_id="b2", base_revision=1, ops=[
                {"op": "remove_edge", "id": "e_inner"}]).ops[0])])
        assert removed_inner.stages[0].edges == []
        assert len(removed_inner.edges) == 1

        removed_outer = apply_flow_edge_ops(
            content, [("ops.0", OpBatch(batch_id="b3", base_revision=1, ops=[
                {"op": "remove_edge", "id": "e1"}]).ops[0])])
        assert removed_outer.edges == []
        # 删主轴那条边不动阶段内那一层
        assert len(removed_outer.stages[0].edges) == 1

    def test_missing_edge_id_is_reported(self):
        with pytest.raises(OpError) as caught:
            apply_flow_edge_ops(_flow(), [("ops.0", OpBatch(
                batch_id="b1", base_revision=0,
                ops=[{"op": "remove_edge", "id": "e_nope"}]).ops[0])])
        assert caught.value.code == "edge_not_found"

    def test_edge_ids_must_be_unique_across_the_two_arrays(self):
        """跨数组重名是契约的硬约束：前端按 id 建索引，重名的有一条永远画不出来。"""
        content = _flow()
        op = OpBatch(batch_id="b1", base_revision=0, ops=[
            {"op": "add_edge", "id": "e1", "source": "a", "target": "a2"},
        ]).ops[0]
        with pytest.raises(OpError) as caught:
            apply_flow_edge_ops(content, [("ops.0", op)])
        assert caught.value.code == "duplicate_edge"

    def test_self_loop_is_refused(self):
        op = OpBatch(batch_id="b1", base_revision=0, ops=[
            {"op": "add_edge", "id": "$e1", "source": "a1", "target": "a1"},
        ]).ops[0]
        with pytest.raises(OpError) as caught:
            apply_flow_edge_ops(_flow(), [("ops.0", op)])
        assert caught.value.code == "self_loop"


class TestApplyOpsAny:
    """`_apply_ops_any`：节点 op 与边 op 同批时**先节点后边**，鱼骨仍然拒绝。"""

    def test_node_then_edge_in_one_batch(self):
        content = _flow()
        batch = OpBatch(batch_id="b1", base_revision=0, ops=[
            {"op": "add", "id": "$new1", "parent_id": "a", "index": 0, "label": "补材料"},
            {"op": "add_edge", "id": "$e1", "source": "a", "target": "a1",
             "label": "是"},
        ])
        updated = _apply_ops_any(content, batch)

        assert isinstance(updated, FlowContent)
        added = [node.label for node in shapes.find(updated, "a").children]
        assert "补材料" in added
        assert [(edge.source, edge.target) for edge in updated.stages[0].edges] == [
            ("a", "a1")]

    def test_edge_op_path_points_back_to_the_original_index(self):
        """拆批之后 `OpError.path` 仍然指得回模型发的那一条（重排下标比不报错更糟）。"""
        batch = OpBatch(batch_id="b1", base_revision=0, ops=[
            {"op": "update", "id": "a1", "changes": {"label": "过"}},
            {"op": "add_edge", "id": "$e1", "source": "a1", "target": "a1"},
        ])
        with pytest.raises(OpError) as caught:
            _apply_ops_any(_flow(), batch)
        assert caught.value.path.startswith("ops.1")

    def test_removing_a_node_drops_its_stage_edges(self):
        """连带删边：删掉一个步骤，端点是它的阶段内边跟着消失（否则整批被契约拒）。"""
        inner = FlowContent.model_validate({
            **_flow_payload(),
            "stages": [
                {"id": "s_1",
                 "root": _node("a", "审不审", [_node("a1", "通过"), _node("a2", "退回")]),
                 "edges": [{"id": "e_i1", "source": "a1", "target": "a"}]},
            ] + _flow_payload()["stages"][1:],
        })
        batch = OpBatch(batch_id="b1", base_revision=0, ops=[
            {"op": "remove", "id": "a1"},
        ])
        updated = _apply_ops_any(inner, batch)

        assert isinstance(updated, FlowContent)
        assert shapes.find(updated, "a1") is None
        assert updated.stages[0].edges == []

    def test_fishbone_still_refuses_edge_ops(self):
        fish = FishboneContent.model_validate({
            "title": "慢", "problem": "为什么慢",
            "bones": [{"category": "人", "root": _node("h1", "人", [_node("h1-1", "休假")])}],
        })
        batch = OpBatch(batch_id="b1", base_revision=0, ops=[
            {"op": "add_edge", "id": "$e1", "source": "h1-1", "target": "h1"},
        ])
        with pytest.raises(OpError) as caught:
            _apply_ops_any(fish, batch)
        assert caught.value.code == "edges_not_supported"
        assert caught.value.path == "ops.0"


class TestEdgeTools:
    """两条边工具走完整条落库路（规范化 → 批次 → 执行 → 候选图）。"""

    def test_remove_edge_on_a_stage_inner_edge(self):
        content = _flow_payload()
        content["stages"][0]["edges"] = [
            {"id": "e_i1", "source": "a", "target": "a2", "label": "否"}]
        ctx = _ctx(content)

        result = asyncio.run(_remove_edge(RemoveEdgeArgs(edge_id="e_i1"), ctx))

        assert result.ok, result.content
        assert isinstance(ctx.candidate, FlowContent)
        assert ctx.candidate.stages[0].edges == []
        assert result.data["edge_layer"] == INNER_LAYER
        assert "阶段内部" in result.content

    def test_add_edge_reports_the_layer_it_landed_in(self):
        ctx = _ctx(_flow_payload())
        result = asyncio.run(_add_edge(AddEdgeArgs(source="a", target="a2", label="退回"), ctx))

        assert result.ok, result.content
        assert result.data["edge_layer"] == INNER_LAYER
        assert "阶段内边" in result.content

    def test_add_edge_across_stages_says_it_demoted(self):
        ctx = _ctx(_flow_payload())
        result = asyncio.run(_add_edge(AddEdgeArgs(source="a1", target="b1", label="跳过"), ctx))

        assert result.ok, result.content
        assert result.data["edge_layer"] == STAGE_LAYER
        # 降级这件事必须说出来，不能让模型以为精确端点被保留了
        assert "端点只能是阶段 id" in result.content

    def test_duplicate_inner_edge_is_refused_with_layer_context(self):
        content = _flow_payload()
        content["stages"][0]["edges"] = [
            {"id": "e_i1", "source": "a", "target": "a2", "label": "否"}]
        ctx = _ctx(content)

        result = asyncio.run(_add_edge(AddEdgeArgs(source="a", target="a2", label="否"), ctx))

        assert not result.ok
        assert result.data["code"] == "duplicate_edge"
        assert "这个阶段内部" in result.content
