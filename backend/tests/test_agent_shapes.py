"""三种结构的生成契约：产物形状、出口转换，以及读侧能不能走进 `bones` / `stages`。

这一组用例钉的是**阶段 2 的四件事**，每件都有自己的失败形态：

  1. **鱼骨与流程的产物真的是自己的形状**（`FishboneContent` / `FlowContent`），
     而不是一棵标着 `diagram_type` 的树——这正是阶段 1 结束时留下的那个洞
     （`content.model_copy(update={"diagram_type": ...})`）。
  2. **`problem` 从模型那里来**：骨架提示词要它，产物里放它，没给时兜底并且
     明说那不是模型写的。
  3. **流程的阶段顺序与阶段间跳转落在 `edges` 上**，端点是阶段 id 而不是节点 id；
     判定节点由模型标 `role="decision"`，入口/出口在模型没标时兜底。
  4. **树那条路一个字节都没动**：同一份 mock 输出仍然落在 `MindMapContent` 上。

另一组（`TestStructureAdapter` / `TestNonTreeEditing`）钉的是读侧：遍历、定位、
按 id 就地改，以及"跨骨/跨阶段的批被拒"这条明确边界。
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from app.agent import shapes
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext
from app.agent.tools import (
    DETAIL_ONLY_PROMPT,
    FISHBONE_PLAN_PROMPT,
    FLOW_PLAN_PROMPT,
    PLAN_PROMPT,
    AddNodeArgs,
    AuditTreeArgs,
    EditArgs,
    ExpandArgs,
    GenerateArgs,
    _add_node,
    _audit_tree_tool,
    _edit,
    _expand,
    _generate,
)
from app.contracts.mindmap import (
    FishboneBone,
    FishboneContent,
    FlowContent,
    FlowStage,
    MindMapContent,
    Node,
)

TOPIC = "机器学习的主要方向"

# 骨架 → 产物的三段 mock 里，骨架那一次的三种响应。
FISHBONE_PLAN = {
    "problem": "为什么团队的机器学习项目总在上线前卡住",
    "branches": [
        {"label": "人", "color": "blue"},
        {"label": "机", "color": "teal"},
        {"label": "料", "color": "amber"},
        {"label": "法", "color": "purple"},
        {"label": "环", "color": "green"},
        {"label": "测", "color": "red"},
    ],
}
FLOW_PLAN = {
    "branches": [
        {"label": "提交申请", "color": "blue"},
        {"label": "形式审查", "color": "amber"},
        {"label": "实质审查", "color": "teal"},
    ],
    # 1 是"形式审查"，2 是"实质审查"：相邻顺序已经由数组表达，这里画的是判断
    # 出口与一条回退——转换之后它们必须落在**阶段 id** 上。
    "edges": [
        {"source": 1, "target": 2, "label": "是", "kind": "branch"},
        {"source": 1, "target": 0, "label": "驳回", "kind": "loop"},
    ],
}
TREE_PLAN = {"branches": ["甲", "乙", "丙"]}


def _ctx(diagram_type: str | None = None) -> RunContext:
    request = AgentRunRequest(
        goal=TOPIC, intent="create", diagram_type=diagram_type,
        budget=RunBudget(max_model_calls=8, max_output_tokens=32768),
    )
    return RunContext(request=request)


def _branches_in(user: str) -> list[str]:
    """从结构那一轮的 user 消息里取出骨架清单（`1. 人` 这样的行）。"""
    marker = "不要增删改名）：\n"
    if marker not in user:
        return []
    titles: list[str] = []
    for line in user.split(marker, 1)[1].splitlines():
        if not line.strip():
            break
        head, _, tail = line.partition(". ")
        if head.isdigit() and tail:
            titles.append(tail)
    return titles


def _tree_from(titles: list[str], *, roles: dict[str, str] | None = None) -> dict[str, Any]:
    """结构那一轮：按骨架给定的标题造一棵两层的树。

    `roles` 用来给指定标题的节点打 `role`（流程图的判定节点就是这么来的——模型
    在结构响应里直接写字段，`Node.role` 在契约里，生成侧不需要任何解析）。
    """
    roles = roles or {}

    def node(node_id: str, label: str, children: list | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": node_id, "label": label,
                                   "children": children or []}
        if label in roles:
            payload["role"] = roles[label]
        return payload

    return {"title": TOPIC, "root": {
        "id": "r", "label": TOPIC, "children": [
            node(f"b{index}", title, [
                node(f"b{index}-1", f"{title}·一"),
                node(f"b{index}-2", f"{title}·二"),
            ])
            for index, title in enumerate(titles)
        ]}}


def _tree_for(user: str, *, roles: dict[str, str] | None = None) -> dict[str, Any]:
    """按结构那一次 user 消息里列出的骨架清单造树（`_generate` 的默认替身）。"""
    return _tree_from(_branches_in(user) or ["甲"], roles=roles)


def _collect_into(seen: list[Any]) -> Any:
    """检查点通道是 async 的（落库交给线程池），测试里用这个收集器接住。"""

    async def collect(content: Any) -> None:
        seen.append(content)

    return collect


def _generate_with(plan: dict, *, diagram_type: str,
                   structure: dict | None = None,
                   checkpoint: Any = None) -> tuple[Any, RunContext]:
    """跑一次完整的 `_generate`（骨架 / 结构 / 说明三段都 mock），返回工具结果。"""
    systems: list[str] = []

    async def fake_chat(system: str, user: str, *, max_tokens: int | None = None,
                        on_delta: Any = None) -> dict:
        systems.append(system)
        if system.startswith(DETAIL_ONLY_PROMPT):
            # 说明那一段不影响这一组用例的判据（形状 / problem / role），给空表：
            # 缺的说明会落成一条 warn，恰好顺带证明"缺说明不影响结构交付"。
            return {}
        if system.startswith(FISHBONE_PLAN_PROMPT) or system.startswith(FLOW_PLAN_PROMPT) \
                or system.startswith(PLAN_PROMPT):
            return plan
        return structure if structure is not None else _tree_for(user)

    ctx = _ctx(diagram_type)
    ctx.checkpoint = checkpoint
    with patch("app.agent.tools.chat_async", new=fake_chat):
        result = asyncio.run(_generate(GenerateArgs(topic=TOPIC, branches=6), ctx))
    assert result.ok, result.content
    return result, ctx


# ---------------------------------------------------------------- 鱼骨


class TestFishboneGeneration:
    def test_product_is_fishbone_content_with_the_model_problem(self):
        """产物是 `FishboneContent`：六根骨 + 主骨上那句话，没有 `root`。"""
        result, ctx = _generate_with(FISHBONE_PLAN, diagram_type="fishbone")

        content = ctx.candidate
        assert isinstance(content, FishboneContent)
        assert content.problem == "为什么团队的机器学习项目总在上线前卡住"
        assert [bone.category for bone in content.bones] == ["人", "机", "料", "法", "环", "测"]
        # 每根骨都挂着自己的子树（骨架给六类、结构那一轮每类两个原因）。
        assert all(len(bone.root.children) == 2 for bone in content.bones)
        # 骨的视觉元素来自骨架（`_apply_skeleton` 套在一级分支上，转换时搬过来）。
        assert content.bones[0].color == "blue"
        assert result.data["diagram"]["type"] == "fishbone"
        # 鱼骨图没有附加边这个概念，`data` 里报 0 而不是缺键。
        assert result.data["diagram"]["edges"] == 0

    def test_missing_problem_falls_back_to_the_title_and_says_so(self):
        """模型没给 `problem` 时用主题占位，并且**说出来**那不是它写的。"""
        plan = {"branches": FISHBONE_PLAN["branches"]}
        result, ctx = _generate_with(plan, diagram_type="fishbone")

        assert isinstance(ctx.candidate, FishboneContent)
        assert ctx.candidate.problem == TOPIC
        codes = [item["code"] for item in result.data["diagnostics"]]
        assert "FISHBONE_PROBLEM_MISSING" in codes

    def test_problem_echoing_the_title_is_reported(self):
        """复读标题 → 收下（用户可能真的这么命题），但记一条诊断。"""
        plan = {**FISHBONE_PLAN, "problem": TOPIC}
        result, ctx = _generate_with(plan, diagram_type="fishbone")

        assert ctx.candidate.problem == TOPIC
        codes = [item["code"] for item in result.data["diagnostics"]]
        assert "FISHBONE_PROBLEM_ECHOES_TITLE" in codes

    def test_incomplete_categories_are_reported_not_rejected(self):
        """少给一类不判失败：画布上少一根骨，但图仍然可用。"""
        plan = {"problem": FISHBONE_PLAN["problem"],
                "branches": FISHBONE_PLAN["branches"][:4]}
        result, ctx = _generate_with(plan, diagram_type="fishbone")

        assert isinstance(ctx.candidate, FishboneContent)
        assert len(ctx.candidate.bones) == 4
        codes = [item["code"] for item in result.data["diagnostics"]]
        assert "FISHBONE_CATEGORIES_INCOMPLETE" in codes


# ---------------------------------------------------------------- 流程


class TestFlowGeneration:
    def test_product_is_flow_content_keeping_stage_order(self):
        """产物是 `FlowContent`：阶段按骨架顺序，每阶段一棵步骤树。"""
        result, ctx = _generate_with(FLOW_PLAN, diagram_type="flowchart")

        content = ctx.candidate
        assert isinstance(content, FlowContent)
        assert [stage.root.label for stage in content.stages] == [
            "提交申请", "形式审查", "实质审查"]
        assert all(len(stage.root.children) == 2 for stage in content.stages)
        assert result.data["diagram"]["type"] == "flowchart"

    def test_edges_point_at_stage_ids_not_node_ids(self):
        """阶段间跳转落在 `edges` 上，端点是**阶段 id**（`s_` 前缀）。"""
        _, ctx = _generate_with(FLOW_PLAN, diagram_type="flowchart")

        content = ctx.candidate
        assert len(content.edges) == 2
        stage_ids = {stage.id for stage in content.stages}
        for edge in content.edges:
            assert edge.source in stage_ids
            assert edge.target in stage_ids
        # 顺序也要对：形式审查 → 实质审查（"是"）与 形式审查 → 提交申请（"驳回"）。
        assert [edge.kind for edge in content.edges] == ["branch", "loop"]
        assert content.edges[1].label == "驳回"

    def test_decision_role_from_the_model_is_kept(self):
        """模型在结构响应里写的 `role="decision"` 原样落进产物。"""
        structure = _tree_from(
            ["提交申请", "形式审查", "实质审查"],
            roles={"形式审查·一": "decision"},
        )
        _, ctx = _generate_with(FLOW_PLAN, diagram_type="flowchart",
                                structure=structure)

        roles = [node.role for node in shapes.iter_nodes(ctx.candidate)]
        assert "decision" in roles

    def test_start_and_end_are_defaulted_when_the_model_says_nothing(self):
        """一个都没标时补入口与出口：第一个阶段的第一步、最后一个阶段的收尾叶子。"""
        result, ctx = _generate_with(FLOW_PLAN, diagram_type="flowchart")

        content = ctx.candidate
        assert content.stages[0].root.role == "start"
        last_leaves = [node for node in shapes.walk_nodes(content.stages[-1].root)
                       if not node.children]
        assert last_leaves[-1].role == "end"
        codes = [item["code"] for item in result.data["diagnostics"]]
        assert "FLOW_ROLE_START_DEFAULTED" in codes
        assert "FLOW_ROLE_END_DEFAULTED" in codes

    def test_model_supplied_roles_are_not_overwritten(self):
        """模型标了 start / end 时不再补：那是它对流程的理解，不是结构事实。"""
        structure = _tree_from(
            ["提交申请", "形式审查", "实质审查"],
            roles={"提交申请·一": "start", "实质审查·二": "end"},
        )
        result, ctx = _generate_with(FLOW_PLAN, diagram_type="flowchart",
                                     structure=structure)

        codes = [item["code"] for item in result.data["diagnostics"]]
        assert "FLOW_ROLE_START_DEFAULTED" not in codes
        assert "FLOW_ROLE_END_DEFAULTED" not in codes

    def test_end_does_not_eat_a_data_or_database_shape(self):
        """收尾那一步标了 database 时，终点往前挪一步——`role` 是单值字段。

        这条钉的是一个具体取舍：末端常常是「归档到系统」「写入台账」，而它标的是
        `database`（圆柱）。把同一个节点改成 `end` 会让那只圆柱消失，读者再也看不出
        这一步在落库；而终点是**结构事实**（流程在哪结束），它必须存在。两全的办法
        是终点落前一步，并且把这次挪动说出来。
        """
        structure = _tree_from(
            ["提交申请", "形式审查", "实质审查"],
            roles={"实质审查·二": "database"},
        )
        result, ctx = _generate_with(FLOW_PLAN, diagram_type="flowchart",
                                     structure=structure)

        content = ctx.candidate
        leaves = [node for node in shapes.walk_nodes(content.stages[-1].root)
                  if not node.children]
        # 收尾那一步保住了它的 database（圆柱没被 end 顶掉）。
        assert leaves[-1].role == "database"
        # 终点落在它前面那个普通步骤上。
        assert leaves[-2].role == "end"
        codes = [item["code"] for item in result.data["diagnostics"]]
        assert "FLOW_ROLE_END_MOVED_UP" in codes
        assert "FLOW_ROLE_END_DEFAULTED" in codes

    def test_end_takes_precedence_when_every_step_touches_data(self):
        """整个阶段都是数据步骤时，终点仍然要给出来——覆盖并**记一条说明**。

        静默覆盖是最难查的一种：用户只会看到"我标的圆柱少了一个"。所以这一支也
        必须留下诊断，哪怕结论是"这里只能二选一"。
        """
        structure = _tree_from(
            ["提交申请", "形式审查", "实质审查"],
            roles={"实质审查·一": "data", "实质审查·二": "database"},
        )
        result, ctx = _generate_with(FLOW_PLAN, diagram_type="flowchart",
                                     structure=structure)

        leaves = [node for node in shapes.walk_nodes(ctx.candidate.stages[-1].root)
                  if not node.children]
        assert leaves[-1].role == "end"
        assert leaves[0].role == "data"
        codes = [item["code"] for item in result.data["diagnostics"]]
        assert "FLOW_ROLE_END_OVERRIDES_DATA" in codes


# ---------------------------------------------------------------- 阶段内的判定出边
#
# 这一组补的是**阶段 1 契约的真实空洞**：`FlowContent.edges` 的端点是阶段 id，而
# `role="decision"` 的节点在阶段内部——模型标了判定节点，它的「是 / 否」两条出边
# 却无处可写（阶段 2 实测：5 个判定节点、`branch` 边 0 条）。
#
# 现在 `FlowStage.edges` 给了它们位置，这一组钉的是**生成侧真的把模型写的东西填
# 进去了**：解析、端点、标签、以及"写歪的出口不判失败但要说出来"。


def _flow_structure(branch: Any = None, *, role: str | None = "decision") -> dict[str, Any]:
    """结构那一轮的响应：三个阶段，第二个阶段里有一个带 `branch` 的判定节点。

    判定节点的两条出口都指向**同一个阶段内**的其它步骤（这正是 `FlowStage.edges`
    要装的东西；跨阶段的那种在 `shapes._split_edges` 的用例里）。
    """
    decision: dict[str, Any] = {"id": "b1-1", "label": "材料齐全吗", "children": []}
    if role is not None:
        decision["role"] = role
    if branch is not None:
        decision["branch"] = branch
    return {"title": TOPIC, "root": {"id": "r", "label": TOPIC, "children": [
        {"id": "b0", "label": "提交申请", "children": [
            {"id": "b0-1", "label": "填表"}]},
        {"id": "b1", "label": "形式审查", "children": [
            decision,
            {"id": "b1-2", "label": "转实质审查"},
            {"id": "b1-3", "label": "通知补正"},
        ]},
        {"id": "b2", "label": "实质审查", "children": [
            {"id": "b2-1", "label": "专家评审"}]},
    ]}}


class TestFlowBranchEdges:
    def test_two_labelled_out_edges_land_in_the_stage(self):
        """两个出口写的是**标题**时，解析成真实节点 id 并落进那个阶段的 `edges`。"""
        structure = _flow_structure({"yes": "转实质审查", "no": "通知补正"})
        _, ctx = _generate_with(FLOW_PLAN, diagram_type="flowchart",
                                structure=structure)

        content = ctx.candidate
        assert isinstance(content, FlowContent)
        stage = content.stages[1]
        assert [(edge.source, edge.target, edge.label) for edge in stage.edges] == [
            ("b1-1", "b1-2", "是"), ("b1-1", "b1-3", "否")]
        assert all(edge.kind == "branch" for edge in stage.edges)
        # 端点确实是真的节点（不是模型抄过来的标题）。
        known = {node.id for node in shapes.iter_nodes(content)}
        assert {edge.source for edge in stage.edges} | {
            edge.target for edge in stage.edges} <= known

    def test_targets_written_as_node_ids_are_accepted(self):
        """出口写 id 也认（先认 id 再认标题）——模型两种写法都有。"""
        structure = _flow_structure({"yes": "b1-2", "no": "b1-3"})
        _, ctx = _generate_with(FLOW_PLAN, diagram_type="flowchart",
                                structure=structure)

        assert [(edge.target, edge.label)
                for edge in ctx.candidate.stages[1].edges] == [
            ("b1-2", "是"), ("b1-3", "否")]

    def test_a_branch_field_never_invalidates_the_tree(self):
        """`branch` 会在校验之前被摘掉：`Node` 是 `extra="forbid"`，留着会 422。

        这条是回归点——`_harvest_flow_branches` 少摘一次，用户看到的就是
        "模型返回的导图不合法"，而模型其实完全照提示词写了。
        """
        structure = _flow_structure({"yes": "转实质审查", "no": "通知补正"},
                                    role=None)  # 连 role 都没标，只有 branch
        result, ctx = _generate_with(FLOW_PLAN, diagram_type="flowchart",
                                     structure=structure)

        assert result.ok, result.content
        assert len(ctx.candidate.stages[1].edges) == 2

    def test_end_word_means_the_flow_stops_here(self):
        """「结束」不是节点：那一支不出边，也不该被算成解析失败。"""
        structure = _flow_structure({"yes": "转实质审查", "no": "结束"})
        result, ctx = _generate_with(FLOW_PLAN, diagram_type="flowchart",
                                     structure=structure)

        edges = ctx.candidate.stages[1].edges
        assert [(edge.target, edge.label) for edge in edges] == [("b1-2", "是")]
        codes = [item["code"] for item in result.data["diagnostics"]]
        assert "FLOW_BRANCH_UNRESOLVED" not in codes

    def test_an_unresolvable_target_is_dropped_and_reported(self):
        """目标写了一个不存在的标题：丢那一条边、记一条诊断，**不判失败**。"""
        structure = _flow_structure({"yes": "转实质审查", "no": "根本不存在的步骤"})
        result, ctx = _generate_with(FLOW_PLAN, diagram_type="flowchart",
                                     structure=structure)

        assert result.ok, result.content
        assert len(ctx.candidate.stages[1].edges) == 1
        codes = [item["code"] for item in result.data["diagnostics"]]
        assert "FLOW_BRANCH_UNRESOLVED" in codes

    def test_no_branch_field_leaves_the_stage_edges_empty(self):
        """模型没写 branch 时阶段内没有边：这是阶段 2 实测到的那个状态，仍能成图。"""
        _, ctx = _generate_with(FLOW_PLAN, diagram_type="flowchart",
                                structure=_flow_structure(None))

        assert [stage.edges for stage in ctx.candidate.stages] == [[], [], []]

    def test_cross_stage_target_is_demoted_to_a_stage_edge(self):
        """指向另一个阶段的判定出口：精确端点保不住 → 降级成阶段级的一条线。"""
        structure = _flow_structure({"yes": "专家评审", "no": "通知补正"})
        result, ctx = _generate_with(FLOW_PLAN, diagram_type="flowchart",
                                     structure=structure)

        content = ctx.candidate
        # 阶段内只剩「否」那条（「是」指向另一个阶段）。
        assert [(edge.target, edge.label) for edge in content.stages[1].edges] == [
            ("b1-3", "否")]
        stage_ids = {stage.id for stage in content.stages}
        assert all(edge.source in stage_ids and edge.target in stage_ids
                   for edge in content.edges)
        # 降级过的跳转与骨架那条「是」指向同一对阶段：契约层去重只留一条。
        branch_edges = [edge for edge in content.edges if edge.source == "s_b1"
                        and edge.target == "s_b2"]
        assert len(branch_edges) == 1


# ---------------------------------------------------------------- 树（一个字不改）


class TestTreeGenerationStaysATree:
    def test_product_is_still_mindmap_content(self):
        result, ctx = _generate_with(TREE_PLAN, diagram_type=None)

        content = ctx.candidate
        assert isinstance(content, MindMapContent)
        assert content.diagram_type == "tree"
        assert [child.label for child in content.root.children] == ["甲", "乙", "丙"]
        # 树这条路不写 `data["diagram"]`（与加图型之前逐字节相同）。
        assert "diagram" not in result.data


# ---------------------------------------------------------------- 适配层


def _fishbone() -> FishboneContent:
    return FishboneContent(
        title="报销慢",
        problem="为什么报销要 11 天",
        bones=[
            FishboneBone(category="人", root=Node(id="h1", label="人", children=[
                Node(id="h1-1", label="审批人休假")])),
            FishboneBone(category="法", root=Node(id="l1", label="法", children=[
                Node(id="l1-1", label="流程太长")])),
        ],
    )


def _flow() -> FlowContent:
    return FlowContent(
        title="注册",
        stages=[
            FlowStage(id="s_1", root=Node(id="a", label="提交", children=[
                Node(id="a1", label="填表")])),
            FlowStage(id="s_2", root=Node(id="b", label="审核", children=[
                Node(id="b1", label="核验")])),
        ],
        edges=[],
    )


class TestStructureAdapter:
    def test_iteration_walks_into_bones_and_stages(self):
        assert {node.id for node in shapes.iter_nodes(_fishbone())} == {
            "h1", "h1-1", "l1", "l1-1"}
        assert {node.id for node in shapes.iter_nodes(_flow())} == {
            "a", "a1", "b", "b1"}
        assert shapes.count_nodes(_fishbone()) == 4
        assert shapes.count_nodes(_flow()) == 4

    def test_find_and_path_start_from_the_owning_subtree(self):
        bone = _fishbone()
        assert shapes.find(bone, "l1-1").label == "流程太长"
        # 路径链首是**它所在那根骨的根**，不是别的骨：鱼骨图里一个原因的祖先链
        # 里不该出现另一根骨的内容。
        assert [node.id for node in shapes.path_to(bone, "l1-1")] == ["l1", "l1-1"]
        assert shapes.depth_of(bone, "h1-1") == 2
        # 骨/阶段的根没有"父节点"：那层挂载由 `FishboneBone` / `FlowStage` 表达。
        assert shapes.find_parent(bone, "h1") is None
        assert shapes.find_parent(_flow(), "a1").id == "a"

    def test_top_branches_are_bones_and_stages(self):
        assert [node.id for node in shapes.top_branches(_fishbone())] == ["h1", "l1"]
        assert [node.id for node in shapes.top_branches(_flow())] == ["a", "b"]
        tree = MindMapContent.model_validate(
            {"title": "t", "root": {"id": "r", "label": "t", "children": [
                {"id": "c", "label": "c"}]}})
        assert [node.id for node in shapes.top_branches(tree)] == ["c"]

    def test_replace_node_keeps_the_shape(self):
        bone = shapes.replace_node(
            _fishbone(), "h1-1", lambda node: node.model_copy(update={"label": "改过"}))
        assert isinstance(bone, FishboneContent)
        assert shapes.find(bone, "h1-1").label == "改过"

        flow = shapes.replace_node(
            _flow(), "b1", lambda node: node.model_copy(update={"label": "改过"}))
        assert isinstance(flow, FlowContent)
        assert shapes.find(flow, "b1").label == "改过"
        # 阶段 id 与节点 id 两个命名空间都不受影响。
        assert [stage.id for stage in flow.stages] == ["s_1", "s_2"]

    def test_to_fishbone_moves_extra_branches_into_the_last_bone(self):
        """超过六类的一级分支：并入最后一根骨，不丢内容。"""
        children = [Node(id=f"b{i}", label=f"类{i}", children=[
            Node(id=f"b{i}-1", label="原因")]) for i in range(7)]
        tree = MindMapContent(title="t", root=Node(id="r", label="t", children=children))

        content, extras = shapes.to_fishbone(tree, problem="为什么")

        assert extras == 1
        assert len(content.bones) == 6
        # 多出来的那一支**连同它的子树**挂到最后一根骨下：内容一个都不丢，层级上
        # 也还能看出它原本是一整块（`类6` 自己是那一支的根）。
        assert [node.id for node in content.bones[-1].root.children] == ["b5-1", "b6"]
        assert [node.id for node in shapes.walk_nodes(content.bones[-1].root)] == [
            "b5", "b5-1", "b6", "b6-1"]

    def test_to_flow_drops_relations_that_point_at_the_theme(self):
        """指向中心主题的关系不进产物：那里没有节点，留着就是 dangling。"""
        from app.contracts.mindmap import Relation

        tree = MindMapContent(
            title="t",
            root=Node(id="r", label="t", children=[
                Node(id="p", label="一"), Node(id="q", label="二")]),
            relations=[
                Relation(id="x1", source="p", target="q", type="depends"),
                Relation(id="x2", source="r", target="p", type="ref"),
            ],
        )

        content = shapes.revalidate(shapes.to_flow(tree)[0])
        assert [(item.source, item.target) for item in content.relations] == [("p", "q")]

    def test_to_flow_maps_edges_and_drops_theme_edges(self):
        """端点重映射到阶段 id；指向中心主题的边丢掉（流程图上没有那个位置）。"""
        from app.contracts.mindmap import Edge

        tree = MindMapContent(
            title="t",
            root=Node(id="r", label="t", children=[
                Node(id="p", label="一"), Node(id="q", label="二")]),
            edges=[
                Edge(id="e1", source="p", target="q", kind="branch", label="是"),
                Edge(id="e2", source="r", target="p"),
            ],
        )

        content, loss = shapes.to_flow(tree)

        assert loss.dropped == 1
        assert loss.demoted == 0
        assert len(content.edges) == 1
        assert (content.edges[0].source, content.edges[0].target) == ("s_p", "s_q")
        assert {stage.id for stage in content.stages} == {"s_p", "s_q"}

    def test_to_flow_puts_same_stage_edges_into_the_stage(self):
        """阶段**内部**的边落进 `FlowStage.edges`；跨阶段的深层跳转降级并计数。

        这是 `FlowStage.edges` 存在的理由：判定节点的两条出边都在一个阶段里，
        阶段之间没有它们的位置。
        """
        from app.contracts.mindmap import Edge

        tree = MindMapContent(
            title="t",
            root=Node(id="r", label="t", children=[
                Node(id="p", label="提交", children=[
                    Node(id="p1", label="填表", children=[Node(id="p1a", label="校验")])]),
                Node(id="q", label="审查", children=[Node(id="q1", label="核验")]),
            ]),
            edges=[
                # 阶段内的判定出边：两端都在阶段 p 里。
                Edge(id="b1", source="p1", target="p1a", label="是", kind="branch"),
                # 阶段之间的深层跳转：p1a → q1，精确端点保不住，降级成 s_p → s_q。
                Edge(id="b2", source="p1a", target="q1", label="否", kind="branch"),
            ],
        )

        content, loss = shapes.to_flow(tree)

        assert loss.inner == 1
        assert loss.demoted == 1
        assert loss.dropped == 0
        first = content.stages[0]
        assert [(edge.source, edge.target, edge.label) for edge in first.edges] == [
            ("p1", "p1a", "是")]
        # 阶段内边的端点仍是**节点** id，阶段间边的端点才是阶段 id。
        assert [(edge.source, edge.target) for edge in content.edges] == [("s_p", "s_q")]
        assert content.stages[1].edges == []


# ---------------------------------------------------------------- 非树的编辑


def _fishbone_ctx() -> RunContext:
    ctx = _ctx()
    ctx.candidate = _fishbone()
    return ctx


class TestNonTreeEditing:
    def test_add_node_inside_a_bone_keeps_the_shape(self):
        """`add_node` 能在骨下面加节点，产物仍然是 `FishboneContent`。"""
        ctx = _fishbone_ctx()

        result = asyncio.run(_add_node(AddNodeArgs(parent_id="h1-1", label="新原因"), ctx))

        assert result.ok, result.content
        assert isinstance(ctx.candidate, FishboneContent)
        assert shapes.find(ctx.candidate, "h1-1").children[0].label == "新原因"
        # 另一根骨一个字节都没动。
        assert [node.id for node in shapes.walk_nodes(ctx.candidate.bones[1].root)] == [
            "l1", "l1-1"]

    def test_cross_container_batch_is_rejected_explicitly(self):
        """一批 op 跨两根骨 → `cross_container`，而不是猜一个位置。"""
        ctx = _fishbone_ctx()
        args = _edit_args([
            {"op": "update", "id": "h1-1", "changes": {"label": "甲"}},
            {"op": "update", "id": "l1-1", "changes": {"label": "乙"}},
        ])

        result = asyncio.run(_edit(args, ctx))

        assert not result.ok
        assert result.data["code"] == "cross_container"
        # 失败之后候选树一个字都不动。
        assert shapes.find(ctx.candidate, "h1-1").label == "审批人休假"

    def test_audit_counts_bones_as_branches(self):
        """`audit_tree` 把六根骨当成"一级分支"来数（它就是最外层内容）。"""
        ctx = _fishbone_ctx()
        result = _audit_tree_tool(AuditTreeArgs(), ctx)

        assert result.ok, result.content
        assert result.data["branches"] == 2
        assert result.data["nodes"] == 4

    def test_expand_locates_a_node_inside_a_bone(self):
        """`node_expand` 能定位到骨下面的节点，并把新节点挂在它下面。"""
        ctx = _fishbone_ctx()
        ctx.request = AgentRunRequest(
            goal="展开", intent="expand", selected_ids=["h1-1"],
            budget=RunBudget(max_model_calls=4, max_output_tokens=32768),
        )

        async def fake_chat(system: str, user: str, *, max_tokens: int | None = None,
                            on_delta: Any = None) -> dict:
            return {"children": [{"label": "排班表缺失", "expandable": False}]}

        with patch("app.agent.tools.chat_async", new=fake_chat):
            result = asyncio.run(_expand(ExpandArgs(node_id="h1-1", count=2), ctx))

        assert result.ok, result.content
        assert isinstance(ctx.candidate, FishboneContent)
        assert [child.label for child in shapes.find(ctx.candidate, "h1-1").children] == [
            "排班表缺失"]


def _edit_args(ops: list[dict]) -> EditArgs:
    return EditArgs.model_validate({"ops": ops})


# ---------------------------------------------------------------- 检查点


class TestCheckpoint:
    """检查点要真的在"结构就绪、说明之前"那一刻被叫到。

    它是本轮唯一一个**不在收尾**发生的落库动作，所以验的不是"函数能调"，而是
    "在真跑一遍 `_generate` 的链路上，它被调到的时刻上那棵树是什么样"。
    """

    def test_checkpoint_sees_a_valid_tree_before_the_details_are_written(self):
        """叫到检查点时，手里那棵是契约合法的完整树，只是 `detail` 还空着。"""
        seen: list[Any] = []
        _generate_with(TREE_PLAN, diagram_type=None, checkpoint=_collect_into(seen))

        assert len(seen) == 1, "一轮生成只该打一个点"
        checkpointed = seen[0]
        # 那一刻它已经是契约类型（能过 `model_validate`），不是半截 JSON。
        reround = MindMapContent.model_validate(checkpointed.model_dump(mode="json"))
        assert shapes.count_nodes(reround) > 1
        # 说明还没写：这正是"落在这里而不是收尾"的意义。
        assert all(not node.detail for node in shapes.iter_nodes(reround))

    def test_checkpoint_payload_is_the_structure_the_run_finally_delivers(self):
        """检查点那份与最终交付的那份，**结构**必须一致（说明是之后才补的）。"""
        seen: list[Any] = []
        _result, ctx = _generate_with(TREE_PLAN, diagram_type=None, checkpoint=_collect_into(seen))

        checkpointed, delivered = seen[0], ctx.candidate
        assert [node.id for node in shapes.iter_nodes(checkpointed)] == [
            node.id for node in shapes.iter_nodes(delivered)]
        assert [node.label for node in shapes.iter_nodes(checkpointed)] == [
            node.label for node in shapes.iter_nodes(delivered)]

    def test_no_checkpoint_channel_means_no_call(self):
        """没有通道时不发生任何额外动作（单测与直接调工具那条路）。"""
        result, ctx = _generate_with(TREE_PLAN, diagram_type=None)

        assert ctx.checkpoint is None
        assert result.ok

    def test_fishbone_checkpoint_is_a_fishbone_not_a_tree(self):
        """要鱼骨图的那一轮，检查点存下来的也必须是鱼骨图。

        收尾的折叠（`_shape_output`）原本发生在检查点之后，于是崩溃后捡回来的
        是一棵 `MindMapContent`——用户当初明明说了要鱼骨图，捡回来的却是一张
        思维导图，看起来像"这一轮连图型都没认对"。
        """
        seen: list[Any] = []
        _result, ctx = _generate_with(FISHBONE_PLAN, diagram_type="fishbone",
                                      checkpoint=_collect_into(seen))

        assert len(seen) == 1
        checkpointed = seen[0]
        assert isinstance(checkpointed, FishboneContent)
        assert isinstance(ctx.candidate, FishboneContent)
        # 六根骨与最终交付的那份逐根同序：折叠是纯函数，折出来的形状不该与最终不同。
        assert [bone.category for bone in checkpointed.bones] == [
            bone.category for bone in ctx.candidate.bones]

    def test_flow_checkpoint_is_a_flow_not_a_tree(self):
        """要流程图的那一轮同理：检查点里得是阶段，不是一棵挂着 `diagram_type` 的树。"""
        seen: list[Any] = []
        _result, ctx = _generate_with(FLOW_PLAN, diagram_type="flowchart",
                                      checkpoint=_collect_into(seen))

        assert len(seen) == 1
        checkpointed = seen[0]
        assert isinstance(checkpointed, FlowContent)
        assert isinstance(ctx.candidate, FlowContent)
        assert [stage.root.label for stage in checkpointed.stages] == [
            stage.root.label for stage in ctx.candidate.stages]

    def test_checkpoint_stamps_cut_nodes_as_unknown(self):
        """被裁掉子节点的那些，快照里必须是 `unknown` 而不是缺省的 `yes`。

        正式打标（`_stamp_expandable`）在检查点后面，所以快照里那些节点还是 `yes`。
        用户点开它以为还能长，点下去却报 `too_deep`——被裁的恰好是唯一能再长的地方。

        上限压到 5 是为了真的触发裁剪：`GenerateArgs.branches=6` 决定骨架清单的
        条数，光给 plan 更多分支不会让树变大。
        """
        seen: list[Any] = []
        with patch("app.agent.tools.INITIAL_MAX_NODES", 5):
            _result, ctx = _generate_with(TREE_PLAN, diagram_type=None,
                                          checkpoint=_collect_into(seen))

        assert len(seen) == 1
        checkpointed = seen[0]
        assert isinstance(checkpointed, MindMapContent)
        stamped = {node.id: node.expandable for node in shapes.iter_nodes(checkpointed)}
        assert "unknown" in stamped.values(), "被裁的节点在快照里必须标成没长完"
        # 交付的那份与快照的标记口径一致：两边都说"没长完"，用户不会因为载入的是
        # 检查点而看到一个不同的可展开状态。
        delivered = {node.id: node.expandable for node in shapes.iter_nodes(ctx.candidate)}
        assert {key for key, value in delivered.items() if value == "unknown"} == {
            key for key, value in stamped.items() if value == "unknown"}

    def test_checkpoint_does_not_disturb_the_tree_the_rest_of_the_run_uses(self):
        """折叠只作用于副本：检查点之后 `_fill_details` / `check` 仍按一棵树工作。"""
        seen: list[Any] = []
        _result, ctx = _generate_with(FISHBONE_PLAN, diagram_type="fishbone",
                                      checkpoint=_collect_into(seen))

        # 交付的那份是鱼骨图，说明它与结构都来自同一棵树——若检查点那次折叠改坏了
        # 原树，这里会缺骨或串形。
        delivered = ctx.candidate
        assert isinstance(delivered, FishboneContent)
        assert len(delivered.bones) == len(FISHBONE_PLAN["branches"])


# ---------------------------------------------------------------- 编辑侧的检查点


class TestEditCheckpoint:
    """edit / expand 也要打检查点。

    创建那条路的检查点保的是"结构长好了、说明还没写"；编辑这条路保的是**已经改完
    的那一批 op**——同样是一大段模型调用之前能救回来的东西。少了它，一次二十分钟的
    编辑在写完说明前崩掉，用户重新打开看到的还是编辑前的树。
    """

    def test_edit_batch_lands_a_checkpoint(self):
        """一批 op 落树之后立刻打点，内容就是改完的那棵树。"""
        ctx = _fishbone_ctx()
        seen: list[Any] = []
        ctx.checkpoint = _collect_into(seen)

        args = _edit_args([{"op": "update", "id": "h1-1", "changes": {"label": "审批人已排班"}}])
        result = asyncio.run(_edit(args, ctx))

        assert result.ok, result.content
        assert len(seen) == 1, "一批 op 只该打一个点"
        checkpointed = seen[0]
        # 图型必须是用户原来那张：edit 路不经过 `_shape_output`，交出去的本来就是
        # 最终形状（`_apply_ops_any` 保持容器类型）。
        assert isinstance(checkpointed, FishboneContent)
        assert shapes.find(checkpointed, "h1-1").label == "审批人已排班"

    def test_single_node_tool_lands_a_checkpoint_too(self):
        """细粒度的 `add_node` 与 `mindmap_edit` 共用 `_commit_drafts`，也要打点。"""
        ctx = _fishbone_ctx()
        seen: list[Any] = []
        ctx.checkpoint = _collect_into(seen)

        result = asyncio.run(_add_node(AddNodeArgs(parent_id="l1-1", label="只是打点"), ctx))

        assert result.ok, result.content
        assert len(seen) == 1
        assert isinstance(seen[0], FishboneContent)
        assert [child.label for child in shapes.find(seen[0], "l1-1").children] == ["只是打点"]

    def test_failed_batch_does_not_land_a_checkpoint(self):
        """op 被拒时树一个字都没动，那就没有什么可存的——不打点。"""
        ctx = _fishbone_ctx()
        seen: list[Any] = []
        ctx.checkpoint = _collect_into(seen)

        args = _edit_args([{"op": "update", "id": "没有这个节点", "changes": {"label": "甲"}}])
        result = asyncio.run(_edit(args, ctx))

        assert not result.ok
        assert seen == []

    def test_expand_lands_a_checkpoint(self):
        """`node_expand` 在批次落树之后打点：新长的这一支不该等到写说明才安全。"""
        ctx = _fishbone_ctx()
        ctx.request = AgentRunRequest(
            goal="展开", intent="expand", selected_ids=["h1-1"],
            budget=RunBudget(max_model_calls=4, max_output_tokens=32768),
        )
        seen: list[Any] = []
        ctx.checkpoint = _collect_into(seen)

        async def fake_chat(system: str, user: str, *, max_tokens: int | None = None,
                            on_delta: Any = None) -> dict:
            return {"children": [{"label": "排班表缺失", "expandable": False}]}

        with patch("app.agent.tools.chat_async", new=fake_chat):
            result = asyncio.run(_expand(ExpandArgs(node_id="h1-1", count=2), ctx))

        assert result.ok, result.content
        assert len(seen) == 1
        assert isinstance(seen[0], FishboneContent)
        assert [child.label for child in shapes.find(seen[0], "h1-1").children] == [
            "排班表缺失"]

    def test_edit_without_a_checkpoint_channel_is_fine(self):
        """没有通道时（单测、脚本、直接调工具）不能因为缺回调而失败。"""
        ctx = _fishbone_ctx()
        assert ctx.checkpoint is None

        args = _edit_args([{"op": "update", "id": "h1-1", "changes": {"label": "甲"}}])
        result = asyncio.run(_edit(args, ctx))

        assert result.ok, result.content
