"""删节点之后"该一起消失的东西"与工具如实上报的数字。

这两件事曾经各算一次同一个概念（"删节点连带删"），两处都与后端的实现不同源：

  1. **清哪些东西**：契约对 `edges` 与 `relations` 的 dangling 一样是硬拒
     （`_validate_edges` / `_validate_relations`），而清理只写了 `edges` 那一半。
     删掉一个被 `relations` 引用的节点，整批 op 会在最后那次整图校验里被拒，工具
     报成 `tree_invalid`——模型只收到一句"改动之后整张图不合法"，指不回"我刚删了
     那个节点"。鱼骨与流程更隐蔽：子树被抽成临时 `MindMapContent` 跑 op 时，
     `relations` 根本不在那个临时对象里。
  2. **上报删了几条边**：`remove_node` 只数 `MindMapContent.edges`。可流程图的边分
     两层，阶段内边（`FlowStage.edges`）的端点是节点 id，删步骤时它们真的被清掉了
     ——工具却报 `removed_edge_count: 0`，模型据此以为"一条关系都没删"。

这一组把三处口径钉在**后端实现**上（它是真相）：清的范围 = 上报的范围 = 真被删掉
的那一批。三种形状各一组，因为 `relations` 落在契约的 `ContentBase` 上——三种都有。
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext
from app.agent.ops import apply_ops
from app.agent.tools import RemoveNodeArgs, _remove_node
from app.contracts.mindmap import (
    DiagramContent,
    FishboneContent,
    FlowContent,
    MindMapContent,
)
from app.contracts.ops import OpBatch


def _node(node_id: str, label: str, children: list | None = None) -> dict[str, Any]:
    return {"id": node_id, "label": label, "children": children or []}


def _parse(payload: dict[str, Any]) -> DiagramContent:
    """与后端的判别同一条口径：**看形状**，不看 `diagram_type`。"""
    if "stages" in payload:
        return FlowContent.model_validate(payload)
    if "bones" in payload:
        return FishboneContent.model_validate(payload)
    return MindMapContent.model_validate(payload)


def _ctx(payload: dict[str, Any]) -> RunContext:
    request = AgentRunRequest(
        goal="删一个节点", intent="edit",
        budget=RunBudget(max_model_calls=4, max_output_tokens=32768),
    )
    return RunContext(request=request, candidate=_parse(payload))


def _tree_payload() -> dict[str, Any]:
    return {
        "title": "带关系的树",
        "root": _node("root", "根", [_node("a", "甲", [_node("a1", "甲一")]), _node("b", "乙")]),
        "relations": [
            {"id": "r1", "source": "a", "target": "b", "type": "depends"},
            # 端点在**子孙**上：级联删除后它同样悬空
            {"id": "r2", "source": "a1", "target": "root", "type": "ref"},
            # 不命中，必须留下
            {"id": "r3", "source": "root", "target": "b", "type": "cause"},
        ],
    }


def _flow_payload() -> dict[str, Any]:
    return {
        "title": "报销流程",
        "diagram_type": "flowchart",
        "stages": [
            {
                "id": "s_1",
                "root": _node("a", "审不审", [_node("a1", "填表"), _node("a2", "补材料")]),
                "edges": [
                    {"id": "e_i1", "source": "a", "target": "a2", "label": "否", "kind": "branch"},
                    {"id": "e_i2", "source": "a1", "target": "a2", "kind": "flow"},
                ],
            },
            {"id": "s_2", "root": _node("b", "付款", [_node("b1", "打款")])},
        ],
        "edges": [{"id": "e1", "source": "s_1", "target": "s_2", "kind": "flow"}],
        "relations": [
            {"id": "r1", "source": "a2", "target": "b1", "type": "ref"},
            {"id": "r2", "source": "a1", "target": "b1", "type": "cause"},
        ],
    }


def _fishbone_payload() -> dict[str, Any]:
    return {
        "title": "为什么报销要 11 天",
        "diagram_type": "fishbone",
        "problem": "为什么报销要 11 天",
        "bones": [
            {"category": "人", "root": _node("h", "人", [_node("h1", "只有一个人审")])},
            {"category": "法", "root": _node("f", "法", [_node("f1", "流程太长")])},
        ],
        "relations": [
            {"id": "r1", "source": "h1", "target": "f1", "type": "cause"},
            {"id": "r2", "source": "h", "target": "f", "type": "ref"},
        ],
    }


def _relation_ids(content: DiagramContent) -> list[str]:
    return [relation.id for relation in content.relations]


class TestTreeRemoveKeepsRelationsValid:
    """树：删节点连带删 `relations`。此前这里会以 `tree_invalid` 整批失败。"""

    def test_remove_node_clears_dangling_relations(self):
        ctx = _ctx(_tree_payload())

        result = asyncio.run(_remove_node(RemoveNodeArgs(node_id="a"), ctx))

        assert result.ok, result.content
        assert result.data.get("code") is None
        assert _relation_ids(ctx.candidate) == ["r3"]
        # 契约层重新校验一遍：这正是"保存不会 422"的后端等价物
        MindMapContent.model_validate(ctx.candidate.model_dump())

    def test_ops_executor_clears_relations_too(self):
        """执行器那一处：即使不经过工具（op 批次直接落库），关系也要一起清。"""
        content = MindMapContent.model_validate(_tree_payload())
        batch = OpBatch(batch_id="b1", base_revision=1, ops=[{"op": "remove", "id": "a"}])

        updated = apply_ops(content, batch)

        assert _relation_ids(updated) == ["r3"]
        assert [edge.id for edge in updated.edges] == []

    def test_untouched_relations_survive(self):
        ctx = _ctx(_tree_payload())

        result = asyncio.run(_remove_node(RemoveNodeArgs(node_id="b"), ctx))

        assert result.ok, result.content
        # 删 b：r1 与 r3 都以 b 为端点，r2 与 b 无关
        assert _relation_ids(ctx.candidate) == ["r2"]


class TestFlowRemoveKeepsRelationsValid:
    """流程：阶段内边与 `relations` 都要清。`relations` 属于整图，不在临时子树里。"""

    def test_remove_step_clears_inner_edges_and_relations(self):
        ctx = _ctx(_flow_payload())

        result = asyncio.run(_remove_node(RemoveNodeArgs(node_id="a2"), ctx))

        assert result.ok, result.content
        content = ctx.candidate
        assert isinstance(content, FlowContent)
        assert content.stages[0].edges == []
        assert content.edges[0].id == "e1"  # 阶段之间的边不动
        assert _relation_ids(content) == ["r2"]
        FlowContent.model_validate(content.model_dump())

    def test_removed_edge_count_equals_what_really_disappeared(self):
        """P2-⑥：上报的条数 = 内容里**真少掉的**条数（两张边表一起数）。

        只数 `content.edges` 时这里会得到 0，而阶段内的两条线已经没了——模型据此
        以为"一条关系都没删"，下一轮要么重复添加、要么漏掉补边。
        """
        ctx = _ctx(_flow_payload())
        content = ctx.candidate
        assert isinstance(content, FlowContent)
        before = [edge.id for stage in content.stages for edge in stage.edges]
        before += [edge.id for edge in content.edges]

        result = asyncio.run(_remove_node(RemoveNodeArgs(node_id="a2"), ctx))

        assert result.ok, result.content
        content = ctx.candidate
        after = [edge.id for stage in content.stages for edge in stage.edges]
        after += [edge.id for edge in content.edges]
        # e_i1（a → a2）与 e_i2（a1 → a2）两条都以 a2 为端点
        assert sorted(result.data["removed_edges"]) == ["e_i1", "e_i2"]
        assert result.data["removed_edge_count"] == len(before) - len(after) == 2
        assert "附加关系" in result.content

    def test_removed_relations_are_reported_too(self):
        """顺手清掉的关系也要说：`relations` 界面不渲染，不说模型就查不到。"""
        ctx = _ctx(_flow_payload())

        result = asyncio.run(_remove_node(RemoveNodeArgs(node_id="a2"), ctx))

        assert result.ok, result.content
        assert result.data["removed_relations"] == ["r1"]
        assert result.data["removed_relation_count"] == 1
        assert "跨节点关系" in result.content

    def test_removing_a_step_without_inner_edges_reports_zero(self):
        ctx = _ctx({
            "title": "报销流程",
            "diagram_type": "flowchart",
            "stages": [
                {"id": "s_1", "root": _node("a", "审", [_node("a1", "填表"), _node("a2", "补材料")])},
                {"id": "s_2", "root": _node("b", "付款")},
            ],
            "edges": [{"id": "e1", "source": "s_1", "target": "s_2"}],
        })

        result = asyncio.run(_remove_node(RemoveNodeArgs(node_id="a2"), ctx))

        assert result.ok, result.content
        assert result.data["removed_edge_count"] == 0
        assert "附加关系" not in result.content


class TestFishboneRemoveKeepsRelationsValid:
    """鱼骨没有边表，但它**同样有 `relations`**——清不到的表现是 `tree_invalid`。"""

    def test_remove_bone_child_clears_relations(self):
        ctx = _ctx(_fishbone_payload())

        result = asyncio.run(_remove_node(RemoveNodeArgs(node_id="h1"), ctx))

        assert result.ok, result.content
        content = ctx.candidate
        assert isinstance(content, FishboneContent)
        assert _relation_ids(content) == ["r2"]
        FishboneContent.model_validate(content.model_dump())
