"""三种图型各自的顶层结构：鱼骨 / 流程 / 与树的联合。

与 `test_contract_v2.py`（树那一套 v1→v2 的默认值与校验）、`test_edges.py`（附加边）
分工不同：这里只钉"鱼骨与流程的**形状**"，以及它们与树怎么被同一个入口分开读。

写这些用例的动机是先前的坑：鱼骨图与流程图在阶段 2 之前是"树换皮"——一张
`diagram_type == "fishbone"` 的图，形状仍是一棵树，于是"这一根骨下有几个原因"
只能靠数一级子节点的个数猜。形状进契约之后，猜的地方变成了读字段。
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.contracts.mindmap import (
    FISHBONE_CATEGORIES,
    MAX_FISHBONE_BONES,
    MAX_FLOW_STAGES,
    FishboneContent,
    FlowContent,
    MindMapContent,
    SaveRequest,
    TreeContent,
)
from app.mindmaps import repository


def node(node_id: str, label: str | None = None, children=None, **extra):
    return {"id": node_id, "label": label or node_id, "children": children or [], **extra}


def bone(category: str, root=None, **extra):
    return {"category": category, "root": root or node(category), **extra}


def fishbone(**extra):
    return {
        "title": "报销慢的原因",
        "problem": "为什么报销要 11 天",
        "bones": [bone("人"), bone("机"), bone("料")],
        **extra,
    }


class TestFishboneShape:
    def test_six_categories_fit(self):
        """5M1E 的六类各一根骨：这是提示词要求的标准形状，必须能一次装下。"""
        parsed = FishboneContent.model_validate(fishbone(
            bones=[bone(category) for category in FISHBONE_CATEGORIES]))
        assert [item.category for item in parsed.bones] == list(FISHBONE_CATEGORIES)
        assert parsed.diagram_type == "fishbone"

    def test_fewer_than_six_is_legal(self):
        """少于此数完全合法：很多问题的成因只有三类，凑到六根等于让模型编。"""
        parsed = FishboneContent.model_validate(fishbone())
        assert len(parsed.bones) == 3

    def test_causes_are_root_children(self):
        """`causes` 不是独立字段，它就是 `root.children`——这一条是设计前提。"""
        parsed = FishboneContent.model_validate(fishbone(bones=[
            bone("人", node("人", "人", [node("c1", "审批人休假"),
                                          node("c2", "岗位缺人")])),
        ]))
        assert [child.label for child in parsed.bones[0].root.children] == ["审批人休假", "岗位缺人"]

    def test_bone_carries_its_own_visuals(self):
        """骨的颜色/图标与 `root.color` 分开：类别是骨架，不是第一个原因。"""
        parsed = FishboneContent.model_validate(fishbone(bones=[
            bone("机", node("机", "机", color="blue"), color="amber", icon="⚙"),
        ]))
        assert parsed.bones[0].color == "amber"
        assert parsed.bones[0].root.color == "blue"

    def test_blank_icon_and_color_are_optional(self):
        parsed = FishboneContent.model_validate(fishbone(bones=[
            bone("料", icon="   "),
        ]))
        assert parsed.bones[0].icon is None
        assert parsed.bones[0].color is None


class TestFishboneValidation:
    def test_duplicate_category_is_rejected(self):
        """两根骨挂同一个类别，图上会出现两条同名的横骨，用户分不清哪条是哪条。"""
        with pytest.raises(ValidationError) as caught:
            FishboneContent.model_validate(fishbone(bones=[bone("人"), bone("人")]))
        assert "骨的类别不能重复" in str(caught.value)

    def test_category_is_folded(self):
        parsed = FishboneContent.model_validate(fishbone(bones=[bone("人 为\n因素")]))
        assert parsed.bones[0].category == "人 为 因素"

    def test_blank_category_is_rejected(self):
        with pytest.raises(ValidationError):
            FishboneContent.model_validate(fishbone(bones=[bone("   ")]))

    def test_node_ids_are_unique_across_bones(self):
        """六根骨是并列的子树，但 id 必须全局唯一：前端按 id 建索引。"""
        with pytest.raises(ValidationError) as caught:
            FishboneContent.model_validate(fishbone(bones=[
                bone("人", node("x", "人")), bone("机", node("x", "机")),
            ]))
        assert "节点 ID 必须唯一" in str(caught.value)

    def test_bone_count_is_bounded(self):
        many = [bone(f"类{i}") for i in range(MAX_FISHBONE_BONES + 1)]
        with pytest.raises(ValidationError):
            FishboneContent.model_validate(fishbone(bones=many))

    def test_node_budget_is_shared_across_bones(self):
        """节点上限按六棵子树**合计**算：画布承受的是整张图，不是一根骨。

        每根骨 1 + 50 + 150 = 201 个节点，单根远低于上限；六根合计 1206，超。
        这正是"按骨各自检查"会放过去的那一格。
        """
        def wide(index: int):
            children = [node(f"m{index}_{i}", children=[node(f"g{index}_{i}_{j}")
                                                        for j in range(3)])
                        for i in range(50)]
            return node(f"b{index}", children=children)

        bones = [bone(category, wide(index))
                 for index, category in enumerate(FISHBONE_CATEGORIES)]
        with pytest.raises(ValidationError) as caught:
            FishboneContent.model_validate(fishbone(bones=bones))
        assert "最多 1000 个节点" in str(caught.value)

    def test_source_refs_must_point_into_source_usage(self):
        with pytest.raises(ValidationError) as caught:
            FishboneContent.model_validate(fishbone(bones=[
                bone("人", node("人", "人", source_refs=[{"source_id": "ghost"}])),
            ]))
        assert "不在 source_usage 中" in str(caught.value)

    def test_relations_may_cross_bones(self):
        """跨骨的因果关系是鱼骨图的核心表达：某根骨的原因导致另一根骨的现象。"""
        parsed = FishboneContent.model_validate(fishbone(
            bones=[bone("人", node("人", "人", [node("a")])),
                   bone("机", node("机", "机", [node("b")]))],
            relations=[{"id": "r1", "source": "a", "target": "b", "type": "cause"}],
        ))
        assert parsed.relations[0].type.value == "cause"

    def test_relation_to_missing_node_is_rejected(self):
        with pytest.raises(ValidationError) as caught:
            FishboneContent.model_validate(fishbone(
                relations=[{"id": "r1", "source": "人", "target": "nope", "type": "ref"}]))
        assert "不在导图中" in str(caught.value)

    def test_empty_problem_is_rejected(self):
        with pytest.raises(ValidationError):
            FishboneContent.model_validate(fishbone(problem="   "))

    def test_problem_is_folded(self):
        parsed = FishboneContent.model_validate(fishbone(problem="为什么\n报销要 11 天"))
        assert parsed.problem == "为什么 报销要 11 天"

    def test_tree_fields_are_not_accepted(self):
        """`extra="forbid"` 让"鱼骨"与"树"的形状互斥——联合类型靠这一点判别。"""
        with pytest.raises(ValidationError):
            FishboneContent.model_validate(fishbone(root=node("r")))


def stage(stage_id: str, root=None, **extra):
    return {"id": stage_id, "root": root or node(stage_id, stage_id), **extra}


def flow(**extra):
    return {
        "title": "报销流程",
        "stages": [
            stage("提交", node("提交", "提交", [node("填单"), node("上传发票")])),
            stage("审查", node("审查", "审查", [node("形式审查"), node("实质审查")])),
            stage("付款", node("付款", "付款", [node("打款")])),
        ],
        **extra,
    }


class TestFlowShape:
    def test_stage_order_is_the_array_order(self):
        """"先后"是数组下标，不需要另一个 `order` 字段：两个来源说同一件事时，
        它们迟早会互相矛盾。"""
        parsed = FlowContent.model_validate(flow())
        assert [item.id for item in parsed.stages] == ["提交", "审查", "付款"]
        assert parsed.diagram_type == "flowchart"

    def test_steps_are_root_children(self):
        parsed = FlowContent.model_validate(flow())
        assert [child.label for child in parsed.stages[0].root.children] == ["填单", "上传发票"]

    def test_edges_connect_stages(self):
        parsed = FlowContent.model_validate(flow(edges=[
            {"id": "e1", "source": "提交", "target": "审查", "kind": "flow"},
            {"id": "e2", "source": "审查", "target": "付款", "label": "通过"},
        ]))
        assert [(edge.source, edge.target) for edge in parsed.edges] == [
            ("提交", "审查"), ("审查", "付款")]

    def test_rollback_cycle_is_legal(self):
        """驳回 / 返工 / 重试就是"付款 → 提交"。环在这里是核心需求，不是坏数据。"""
        parsed = FlowContent.model_validate(flow(edges=[
            {"id": "e1", "source": "提交", "target": "审查"},
            {"id": "e2", "source": "审查", "target": "付款"},
            {"id": "e3", "source": "付款", "target": "提交", "label": "驳回", "kind": "loop"},
        ]))
        assert len(parsed.edges) == 3

    def test_duplicate_edges_are_deduped(self):
        parsed = FlowContent.model_validate(flow(edges=[
            {"id": "e1", "source": "提交", "target": "审查", "label": "是"},
            {"id": "e2", "source": "提交", "target": "审查", "label": "是"},
        ]))
        assert [edge.id for edge in parsed.edges] == ["e1"]


class TestFlowStageEdges:
    """阶段**内部**的附加边：判定节点的「是 / 否」两条出边写在这里。

    这一组用例存在的理由是一次实测：契约里 `FlowContent.edges` 的端点是**阶段 id**，
    而 `role="decision"` 的节点在阶段**内部**——模型标了 5 个判定节点，产出里
    `branch_edges` 恒为 0，因为那两条出边在两个命名空间里都没有位置。

    所以这一组钉的不是"一个字段存在"，是**两个命名空间的隔离边界**：
    `FlowStage.edges` 的端点只能在这个阶段里，`FlowContent.edges` 的端点只能是阶段。
    """

    def test_decision_has_two_labelled_out_edges(self):
        """一个判定节点的「是 / 否」两条出边，落在它自己的阶段里。"""
        parsed = FlowContent.model_validate({
            "title": "注册流程",
            "stages": [
                {
                    "id": "s_1",
                    "root": node("a", "提交", [
                        node("check", "信息完整吗", role="decision"),
                        node("pass", "进入审查"),
                        node("fix", "补正资料"),
                    ]),
                    "edges": [
                        {"id": "b1", "source": "check", "target": "pass",
                         "label": "是", "kind": "branch"},
                        {"id": "b2", "source": "check", "target": "fix",
                         "label": "否", "kind": "branch"},
                    ],
                },
            ],
        })
        assert [(edge.source, edge.target, edge.label)
                for edge in parsed.stages[0].edges] == [
            ("check", "pass", "是"), ("check", "fix", "否")]
        assert all(edge.kind == "branch" for edge in parsed.stages[0].edges)

    def test_stage_edges_default_to_empty(self):
        """缺这个键的旧流程图读进来是"阶段内没有附加边"——零迁移。"""
        parsed = FlowContent.model_validate(flow())
        assert [stage.edges for stage in parsed.stages] == [[], [], []]

    def test_endpoint_must_belong_to_that_stage(self):
        """拿另一个阶段的节点 id 当端点要拒：这是两个 `edges` 最容易串的地方。"""
        with pytest.raises(ValidationError) as caught:
            FlowContent.model_validate({
                "title": "流",
                "stages": [
                    {"id": "s_1", "root": node("a", "提交", [node("a1")]),
                     "edges": [{"id": "b1", "source": "a1", "target": "b1run"}]},
                    {"id": "s_2", "root": node("b", "审查", [node("b1run")])},
                ],
            })
        assert "不在阶段 s_1 里" in str(caught.value)

    def test_endpoint_may_not_be_a_stage_id(self):
        """阶段 id 不算阶段内的节点 id：两个命名空间互不通用，否则画布会找错对象。"""
        with pytest.raises(ValidationError) as caught:
            FlowContent.model_validate({
                "title": "流",
                "stages": [
                    {"id": "s_1", "root": node("a", "提交"),
                     "edges": [{"id": "b1", "source": "a", "target": "s_2"}]},
                    {"id": "s_2", "root": node("b", "审查")},
                ],
            })
        assert "不在阶段 s_1 里" in str(caught.value)

    def test_self_loop_inside_a_stage_is_rejected(self):
        with pytest.raises(ValidationError) as caught:
            FlowContent.model_validate({
                "title": "流",
                "stages": [{"id": "s_1", "root": node("a", "提交"),
                            "edges": [{"id": "b1", "source": "a", "target": "a"}]}],
            })
        assert "自环" in str(caught.value)

    def test_duplicate_edges_inside_a_stage_are_deduped(self):
        """去重口径与另外两处 `edges` 完全一致：`(source, target, label)`。"""
        parsed = FlowContent.model_validate({
            "title": "流",
            "stages": [{
                "id": "s_1", "root": node("a", "提交", [node("b")]),
                "edges": [
                    {"id": "b1", "source": "a", "target": "b", "label": "是"},
                    {"id": "b2", "source": "a", "target": "b", "label": "是"},
                    # 同一对端点、不同 label：这两条是不同的分支说明，都要留。
                    {"id": "b3", "source": "a", "target": "b", "label": "否"},
                ],
            }],
        })
        assert [edge.id for edge in parsed.stages[0].edges] == ["b1", "b3"]

    def test_edge_ids_are_unique_across_both_arrays(self):
        """阶段间与阶段内的边共用一份 id 空间：前端按 id 建索引，重名会丢一条。"""
        with pytest.raises(ValidationError) as caught:
            FlowContent.model_validate({
                "title": "流",
                "stages": [
                    {"id": "s_1", "root": node("a", "提交", [node("a1")]),
                     "edges": [{"id": "e1", "source": "a1", "target": "a"}]},
                    {"id": "s_2", "root": node("b", "审查")},
                ],
                "edges": [{"id": "e1", "source": "s_1", "target": "s_2"}],
            })
        assert "边 ID 必须唯一" in str(caught.value)

    def test_stage_edges_do_not_leak_into_the_union(self):
        """带阶段内边的流程图仍然是**流程**，不会被判别成树（`extra="forbid"` 守着）。"""
        parsed = SaveRequest.model_validate({
            "content": {
                "title": "流",
                "stages": [{
                    "id": "s_1", "root": node("a", "提交", [node("b")]),
                    "edges": [{"id": "b1", "source": "a", "target": "b", "kind": "flow"}],
                }],
            },
        }).content
        assert isinstance(parsed, FlowContent)
        assert parsed.stages[0].edges[0].kind == "flow"


class TestFlowValidation:
    def test_stage_ids_must_be_unique(self):
        with pytest.raises(ValidationError) as caught:
            FlowContent.model_validate({"title": "流", "stages": [
                stage("s", node("a")), stage("s", node("b"))]})
        assert "阶段 ID 必须唯一" in str(caught.value)

    def test_edge_endpoints_are_stages_not_nodes(self):
        """端点命名空间是**阶段**：拿一个真实存在的步骤 id 当端点也要拒。

        这一条是流程与树最容易串的地方——两边的 `Edge` 是同一个类，但"连的是阶段
        还是步骤"完全不同。放过它，前端会在阶段列表里找不到端点，画出一条悬空主轴。
        """
        with pytest.raises(ValidationError) as caught:
            FlowContent.model_validate(flow(edges=[
                {"id": "e1", "source": "填单", "target": "审查"}]))
        assert "起点阶段 填单 不在导图中" in str(caught.value)

    def test_edge_to_missing_stage_is_rejected(self):
        with pytest.raises(ValidationError) as caught:
            FlowContent.model_validate(flow(edges=[
                {"id": "e1", "source": "提交", "target": "不存在的阶段"}]))
        assert "终点阶段" in str(caught.value)

    def test_self_loop_between_a_stage_and_itself_is_rejected(self):
        with pytest.raises(ValidationError) as caught:
            FlowContent.model_validate(flow(edges=[
                {"id": "e1", "source": "提交", "target": "提交"}]))
        assert "同一个阶段" in str(caught.value)

    def test_node_ids_are_unique_across_stages(self):
        with pytest.raises(ValidationError) as caught:
            FlowContent.model_validate({"title": "流", "stages": [
                stage("a", node("x")), stage("b", node("x"))]})
        assert "节点 ID 必须唯一" in str(caught.value)

    def test_stage_count_is_bounded(self):
        many = [stage(f"s{i}", node(f"n{i}")) for i in range(MAX_FLOW_STAGES + 1)]
        with pytest.raises(ValidationError):
            FlowContent.model_validate({"title": "流", "stages": many})

    def test_node_budget_is_shared_across_stages(self):
        """节点上限按全部阶段**合计**算（与鱼骨同一口径）。"""
        def wide(index: int):
            children = [node(f"m{index}_{i}", children=[node(f"g{index}_{i}_{j}")
                                                        for j in range(3)])
                        for i in range(50)]
            return node(f"s{index}", children=children)

        # 每阶段 201 个节点，六个阶段 1206 > 1000；单阶段远低于上限。
        stages = [stage(f"s{index}", wide(index)) for index in range(6)]
        with pytest.raises(ValidationError) as caught:
            FlowContent.model_validate({"title": "流", "stages": stages})
        assert "最多 1000 个节点" in str(caught.value)

    def test_relations_still_point_at_nodes(self):
        """`relations` 端点是**节点**（跨阶段的解释性关系），与 `edges` 相反。"""
        parsed = FlowContent.model_validate(flow(relations=[
            {"id": "r1", "source": "填单", "target": "打款", "type": "cause"}]))
        assert parsed.relations[0].source == "填单"

    def test_relation_endpoint_must_be_a_node(self):
        """与上一条相反：`relations` 只认**节点** id，阶段 id 不算。

        用一份阶段 id 与节点 id 不同的流程来钉：拿 `s1`（只作为阶段存在）当关系
        起点要拒，否则"跨阶段解释关系"会连到画不出来的东西上。
        """
        with pytest.raises(ValidationError) as caught:
            FlowContent.model_validate({
                "title": "流",
                "stages": [{"id": "s1", "root": node("n1", "提交")},
                           {"id": "s2", "root": node("n2", "付款")}],
                "relations": [{"id": "r1", "source": "s1", "target": "n2", "type": "ref"}],
            })
        assert "不在导图中" in str(caught.value)

    def test_tree_fields_are_not_accepted(self):
        with pytest.raises(ValidationError):
            FlowContent.model_validate(flow(root=node("r")))


def content_of(payload: dict):
    """按**保存入口**（`SaveRequest.content`）解析，拿到判别后的成员。

    用它而不是 `TypeAdapter(DiagramContent)`：真实入口就是这里，而联合的分派规则
    只在被请求体校验时才真正生效（pydantic 会把 `Field(union_mode=...)` 收进模型
    自己的 core schema）。
    """
    return SaveRequest.model_validate({"content": payload}).content


class TestDiagramUnion:
    def test_old_record_without_diagram_type_is_a_tree(self):
        """旧图（连 `diagram_type` 都没有）落在联合的第一个成员上。"""
        parsed = content_of({"title": "旧图", "root": node("r")})
        assert isinstance(parsed, MindMapContent)
        assert parsed.diagram_type == "tree"

    def test_old_fishbone_labelled_tree_still_parses_as_tree(self):
        """**最要紧的一条**：`diagram_type == "fishbone"` 但形状是一棵树。

        阶段 2 之前生成链路就是这么产出的（`agent/tools.py` 拿 `MindMapContent` 改一下
        `diagram_type` 就算鱼骨图）。这批图必须继续读成树、继续存得回去——判别要是
        认 `diagram_type` 而不是形状，它们会在下一次保存时 422，而用户什么都没改。
        """
        parsed = content_of({"title": "旧鱼骨", "diagram_type": "fishbone",
                             "root": node("r")})
        assert isinstance(parsed, MindMapContent)
        assert parsed.diagram_type == "fishbone"   # 自述保留，不被改写

    def test_fishbone_shape_parses_as_fishbone(self):
        parsed = content_of({"title": "缺陷", "problem": "为什么漏检",
                             "bones": [bone("人")]})
        assert isinstance(parsed, FishboneContent)
        assert parsed.diagram_type == "fishbone"

    def test_flow_shape_parses_as_flow(self):
        parsed = content_of({"title": "流程", "stages": [stage("s1", node("n1"))]})
        assert isinstance(parsed, FlowContent)
        assert parsed.diagram_type == "flowchart"

    def test_a_shape_is_never_ambiguous(self):
        """三套的必填字段与 `extra="forbid"` 让形状互斥，不需要退回"挑匹配最多"。"""
        tree_only = content_of({"title": "t", "root": node("r")})
        fish_only = content_of({"title": "t", "problem": "p", "bones": [bone("人")]})
        flow_only = content_of({"title": "t", "stages": [stage("s1", node("n1"))]})
        assert [type(item) for item in (tree_only, fish_only, flow_only)] == [
            MindMapContent, FishboneContent, FlowContent]

    def test_a_payload_that_fits_none_is_rejected(self):
        with pytest.raises(ValidationError):
            content_of({"title": "空的"})

    def test_tree_extra_keys_are_still_forbidden(self):
        """`extra="forbid"` 没有因为联合而放宽：树上带 `problem` 仍然 422。"""
        with pytest.raises(ValidationError):
            content_of({"title": "t", "root": node("r"), "problem": "p"})

    def test_tree_content_is_the_same_class(self):
        """`TreeContent` 是别名，不是子类：同一份定义，减少一份重复的 schema。"""
        assert TreeContent is MindMapContent


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDMAP_DB_PATH", str(tmp_path / "repo.db"))
    yield


class TestRepositoryRoundTrip:
    """三套形状都能经 `SaveRequest` → `repository` 存进去、原样读出来。"""

    def test_all_three_shapes_survive_a_round_trip(self, temp_db):
        payloads = [
            {"title": "树", "root": node("r")},
            {"title": "鱼骨", "problem": "为什么漏检", "bones": [bone("人")]},
            {"title": "流程", "stages": [stage("s1", node("n1"))],
             "edges": []},
        ]
        for payload in payloads:
            body = SaveRequest.model_validate({"content": payload})
            record = repository.create_map(body.content)
            again = repository.get_map(record.id)
            assert again is not None
            assert type(again.content) is type(body.content)
            assert again.content.model_dump() == body.content.model_dump()

    def test_a_legacy_row_reads_as_a_tree(self, temp_db):
        """库里一条 v2 早期记录（没有 `diagram_type` / `edges`）读出来是树。"""
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(repository.db_path())) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS mindmaps (id TEXT PRIMARY KEY,"
                         " version INTEGER NOT NULL, content TEXT NOT NULL,"
                         " updated_at TEXT NOT NULL)")
            conn.execute("INSERT INTO mindmaps (id, version, content, updated_at)"
                         " VALUES ('legacy', 1, ?,"
                         " '2026-11-01T00:00:00.000Z')",
                         (json.dumps({"schema_version": 2, "title": "旧图",
                                      "root": node("r")}, ensure_ascii=False),))
        record = repository.get_map("legacy")
        assert record is not None
        assert isinstance(record.content, MindMapContent)
        assert record.content.diagram_type == "tree"
        assert record.content.edges == []
