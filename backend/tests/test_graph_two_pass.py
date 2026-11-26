"""图谱两段式：开关、第一段的输入输出、关系的四道过滤、以及降级。

这条路与三段式的差别只有一处但那一处是本质的：**一级分支不再来自章节标题，来自图**。
所以这个文件盯四件事：

  1. **开关默认关**，且只在 `tree` 上开——流程图的一级是阶段、鱼骨是六个固定类别，
     它们的"一级分支"根本不由概念决定；
  2. **图空时退回三段式并记诊断**，不是静默变成"只有主题"的树；
  3. **关系的四道过滤**（端点 / kind+why / 同父 / 上限）各自单独可测——它们各自对应
     一种真的会出现的模型输出形态；
  4. **模型把 relations 写进 root 里面**这条实测出来的形态不能让整张图作废。

模型替身与 `test_single_call_generate.py` 同一套写法（`patch("app.agent.tools.chat_async")`），
因为那条路与这条路的差别只在 prompts，不在 provider 的形状。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.agent import shapes
from app.agent import tools as agent_tools
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    DETAIL_ONLY_PROMPT,
    FLOW_PLAN_PROMPT,
    GRAPH_MIN_CONFIDENCE,
    GRAPH_RELATION_RULES,
    GRAPH_SKELETON_ENV,
    GRAPH_TREE_RULES,
    PLAN_PROMPT,
    _graph_plan_ask,
    _relations_from,
    build_registry,
    graph_skeleton_enabled,
)
from app.contracts.mindmap import Relation, RelationKind, RelationType
from app.contracts.source import Source
from app.knowledge import graph_store

TOPIC = "多智能体思维导图系统"
BRANCHES = ("数据结构", "生成链路", "检索与图谱")

GRAPH_ENTITIES = [
    graph_store.GraphEntity(name="mindmapcontent", type="概念", degree=16),
    graph_store.GraphEntity(name="sqlite", type="概念", degree=14),
    graph_store.GraphEntity(name="agent 循环", type="概念", degree=12),
    graph_store.GraphEntity(name="bm25", type="概念", degree=9),
]
GRAPH_LINKS = [
    graph_store.GraphLink(
        src="mindmapcontent", dst="sqlite", relation="存储于",
        source_id="s-1", start=0, end=100, weight=3),
]


def _request(sources: list[Source] | None = None) -> AgentRunRequest:
    return AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default",
        sources=sources or [],
        budget=RunBudget(max_model_calls=20, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=1),
    )


def _ctx(sources: list[Source] | None = None) -> RunContext:
    payload = sources or []
    return RunContext(request=_request(payload),
                      sources_by_id={source.id: source for source in payload})


def _tree(relations: list[dict] | None = None, *, nested: bool = False) -> dict:
    """一棵三支的树。`nested=True` 时把 relations 写进 root（实测的模型习惯）。"""
    children = [
        {"id": f"b{index}", "label": label, "children": [
            {"id": f"b{index}-1", "label": f"{label}·要点", "detail": "", "children": []},
        ]}
        for index, label in enumerate(BRANCHES)
    ]
    root = {"id": "root", "label": TOPIC, "detail": "", "children": children}
    if nested and relations:
        root["relations"] = relations
        return {"title": TOPIC, "root": root}
    payload: dict = {"title": TOPIC, "root": root}
    if relations is not None:
        payload["relations"] = relations
    return payload


def _valid_relations() -> list[dict]:
    return [
        {"source": "b0-1", "target": "b1-1", "kind": "depends_on",
         "why": "生成链路按数据结构落树", "confidence": 0.9},
        {"source": "b1-1", "target": "b2-1", "kind": "causes",
         "why": "检索质量决定生成内容", "confidence": 0.7},
    ]


def _walk(node: dict):
    yield node
    for child in node.get("children") or []:
        if isinstance(child, dict):
            yield from _walk(child)


def _detail_answer(tree: dict) -> dict:
    return {
        node["id"]: "这句说明写到八十个字以上，讲清它是什么、怎么做、有什么条件与数字，"
                    "这样第二段的产出才符合长度目标。"
        for node in _walk(tree["root"]) if node.get("id")
    }


def _chat(tree: dict, *, seen: list[tuple[str, str]] | None = None):
    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if seen is not None:
            seen.append((system, user))
        if system.startswith(DETAIL_ONLY_PROMPT):
            return _detail_answer(tree)
        return tree
    return chat


def _run(ctx: RunContext, chat, *, arguments: dict | None = None,
         subgraph_value=None):
    args = {"topic": TOPIC, "branches": 3}
    args |= arguments or {}
    registry = build_registry("create", has_sources=bool(ctx.request.sources),
                              has_library=False)
    value = subgraph_value if subgraph_value is not None else (
        list(GRAPH_ENTITIES), list(GRAPH_LINKS))
    with patch("app.agent.tools.chat_async", new=chat), \
            patch("app.agent.tools.graph_store.subgraph", return_value=value):
        return asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate", arguments=args), ctx))


def _content_with(raw: dict):
    """把一棵模型输出落成 `MindMapContent`（跳过生成链路，只要一棵树）。"""
    from app.generation.service import validate_generated_content
    return validate_generated_content(raw, sources=None)


# ---------------------------------------------------------------- 1. 开关


def test_graph_skeleton_is_off_by_default(monkeypatch):
    """默认关：这条路依赖 Neo4j 活着且图已建好，而"图没建"在新机器上是常态。"""
    monkeypatch.delenv(GRAPH_SKELETON_ENV, raising=False)
    assert graph_skeleton_enabled() is False


@pytest.mark.parametrize("value", ["1", "on", "true", "yes", " TRUE "])
def test_graph_skeleton_reads_truthy_values(monkeypatch, value):
    monkeypatch.setenv(GRAPH_SKELETON_ENV, value)
    assert graph_skeleton_enabled() is True


# ---------------------------------------------------------------- 2. 第一段接线


def test_graph_mode_puts_the_graph_into_the_structure_prompt(monkeypatch):
    """一级分支从图来：图谱块必须出现在结构那一次的 user 里，且不再有骨架调用。"""
    monkeypatch.setenv(GRAPH_SKELETON_ENV, "1")
    seen: list[tuple[str, str]] = []
    result = _run(_ctx(), _chat(_tree(), seen=seen))

    assert result.ok, result.content
    # 只有两次调用：结构 + 说明。三段式是三次（骨架 + 结构 + 说明）。
    assert len(seen) == 2
    system, user = seen[0]
    assert GRAPH_TREE_RULES in system
    assert "mindmapcontent" in user          # 实体名进了 user
    assert "存储于" in user                   # 关系短语也进了
    assert result.data["graph"]["entities"] == len(GRAPH_ENTITIES)
    assert result.data["graph"]["links"] == 1


def test_graph_mode_skips_flowchart(monkeypatch):
    """流程图的"一级分支"是阶段（先后来自材料），不由概念决定。"""
    monkeypatch.setenv(GRAPH_SKELETON_ENV, "1")
    ctx = _ctx()
    registry = build_registry("create", has_sources=False, has_library=False)

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {}
        # 流程图走的是 `FLOW_PLAN_PROMPT`，不是 `PLAN_PROMPT`——那条路的骨架要求
        # "一级分支是阶段 + 一个 edges 数组"，两句话都在那一份里。
        if system.startswith(FLOW_PLAN_PROMPT):
            return {"branches": list(BRANCHES)}
        return _tree()

    with patch("app.agent.tools.chat_async", new=chat), \
            patch("app.agent.tools.graph_store.subgraph") as subgraph:
        subgraph.return_value = (list(GRAPH_ENTITIES), list(GRAPH_LINKS))
        result = asyncio.run(registry.execute(
            ToolCall(call_id="c-flow", name="mindmap_generate",
                     arguments={"topic": TOPIC, "branches": 3,
                                "diagram_type": "flowchart"}), ctx))

    assert result.ok, result.content
    subgraph.assert_not_called()
    assert "graph" not in result.data


def test_graph_mode_falls_back_when_the_graph_is_empty(monkeypatch):
    """图空 → 退回三段式，并且**说出来**。静默退回会让覆盖率数字莫名其妙地掉回去。

    降级之后走的是**原样的三段式**，所以模型替身必须能答骨架那一次（`branches`），
    否则这次运行会死在 `too_few_branches` 上——那测的就不是降级了。
    """
    monkeypatch.setenv(GRAPH_SKELETON_ENV, "1")

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if system.startswith(DETAIL_ONLY_PROMPT):
            return _detail_answer(_tree())
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(BRANCHES)}
        return _tree()

    result = _run(_ctx(), chat, subgraph_value=([], []))

    assert result.ok, result.content
    codes = [item["code"] for item in result.data["diagnostics"]]
    assert "GRAPH_SKELETON_UNAVAILABLE" in codes
    assert "graph" not in result.data


# ---------------------------------------------------------------- 3. 关系落地


def test_graph_mode_lands_relations(monkeypatch):
    monkeypatch.setenv(GRAPH_SKELETON_ENV, "1")
    result = _run(_ctx(), _chat(_tree(_valid_relations())))

    assert result.ok, result.content
    assert result.data["graph"]["relations"] == 2
    assert result.data["graph"]["relation_kinds"] == ["causes", "depends_on"]


def test_relations_nested_in_root_do_not_kill_the_tree(monkeypatch):
    """实测形态：模型把 relations 塞进 root 里。`Node` 是 `extra="forbid"`，
    不摘出来整次生成就判废——提示词已经补了位置说明，这条测的是兜底。"""
    monkeypatch.setenv(GRAPH_SKELETON_ENV, "1")
    result = _run(_ctx(), _chat(_tree(_valid_relations(), nested=True)))

    assert result.ok, result.content
    assert result.data["graph"]["relations"] == 2


def test_same_parent_relation_is_dropped():
    """同一父节点下的关系由树表达，再画一条线就是 N² 条对角线。"""
    content, _ = _content_with(_tree())
    kept, drops = _relations_from(
        [{"source": "b0", "target": "b1", "kind": "depends_on", "why": "同一层"}],
        content)
    assert kept == []
    assert drops["same_parent"] == 1


def test_relation_without_why_is_dropped():
    content, _ = _content_with(_tree())
    kept, drops = _relations_from(
        [{"source": "b0-1", "target": "b1-1", "kind": "depends_on"}], content)
    assert kept == []
    assert drops["no_reason"] == 1


def test_relation_with_unknown_kind_is_dropped_not_raised():
    """认不出的 kind 与"没写 kind"同一条路：放过去会让 `Relation(...)` 抛
    `ValidationError`，而那会把"模型写错一个词"升级成整张图 500。"""
    content, _ = _content_with(_tree())
    kept, drops = _relations_from(
        [{"source": "b0-1", "target": "b1-1", "kind": "related_to", "why": "像"}],
        content)
    assert kept == []
    assert drops["no_reason"] == 1


def test_relations_resolve_by_label_too():
    """端点先认 id，再认 label：模型两种写法都有。"""
    content, _ = _content_with(_tree())
    kept, drops = _relations_from(
        [{"source": f"{BRANCHES[0]}·要点", "target": f"{BRANCHES[1]}·要点",
          "kind": "part_of", "why": "上半句是下半句的一部分"}], content)
    assert len(kept) == 1
    assert kept[0].source == "b0-1"
    assert kept[0].target == "b1-1"
    assert drops["dangling"] == 0


def test_relations_sorted_by_confidence_and_capped():
    """上限按置信度取：不设下限时，一堆 0.35 的边会把 0.99 的那条挤掉。

    端点对只有 3 对（三支各一个要点），所以用**六种 kind** 把条数堆上去——
    同一对端点的同一个 kind 会被 `duplicate` 滤掉，那测的就不是上限了。
    """
    content, _ = _content_with(_tree())
    targets = [("b0-1", "b1-1"), ("b1-1", "b2-1"), ("b0-1", "b2-1")]
    kinds = [kind.value for kind in RelationKind]
    payload = [
        {"source": source, "target": target, "kind": kind, "why": "低",
         "confidence": 0.35}
        for source, target in targets for kind in kinds[:-1]
    ]
    # 第 6 种 kind 只出现在这一条上：它是最高的那条，必须活下来。
    payload.append({"source": "b0-1", "target": "b1-1", "kind": kinds[-1],
                    "why": "最高", "confidence": 0.99})
    kept, drops = _relations_from(payload, content, limit=5)

    assert len(kept) == 5
    assert any(relation.why == "最高" for relation in kept)
    assert drops["limited"] >= 1


def test_relations_below_the_floor_are_dropped():
    content, _ = _content_with(_tree())
    kept, drops = _relations_from(
        [{"source": "b0-1", "target": "b1-1", "kind": "depends_on", "why": "猜的",
          "confidence": GRAPH_MIN_CONFIDENCE - 0.1}], content)
    assert kept == []
    assert drops["low_confidence"] == 1


def test_relations_are_renumbered_and_marked_ai():
    """id 由服务端分配：模型写的 id 与节点 id 在同一个命名空间里混着，容易撞。"""
    content, _ = _content_with(_tree())
    kept, _ = _relations_from(_valid_relations(), content)
    assert [relation.id for relation in kept] == ["rel_1", "rel_2"]
    assert all(relation.provenance == "ai" for relation in kept)


# ---------------------------------------------------------------- 4. 契约


def test_relation_kind_is_filled_from_the_legacy_type():
    relation = Relation(id="r1", source="a", target="b", type=RelationType.depends)
    assert relation.kind is RelationKind.depends_on
    # `type` 只是输入的回声：不做反向映射（`part_of` 在旧四值里没有对应）。
    assert relation.type is RelationType.depends


def test_relation_keeps_type_empty_when_only_kind_is_given():
    relation = Relation(id="r1", source="a", target="b", kind="part_of")
    assert relation.kind is RelationKind.part_of
    assert relation.type is None


def test_relation_requires_kind_or_type():
    with pytest.raises(Exception):
        Relation(id="r1", source="a", target="b")


def test_blank_why_is_none():
    relation = Relation(id="r1", source="a", target="b", kind="causes", why="   ")
    assert relation.why is None


def test_six_kinds_are_declared():
    assert {kind.value for kind in RelationKind} == {
        "depends_on", "causes", "conflicts", "part_of", "refines", "references"}


# ---------------------------------------------------------------- 5. 第一段的 prompt


def test_graph_ask_puts_the_topic_first_and_the_evidence_last():
    """顺序即权重：先图（选材依据），再证据（事实依据），最后才是要求。"""
    text = _graph_plan_ask(TOPIC, 6, graph="【图谱】…", evidence="第 1 个概念的检索结果")
    assert text.index(TOPIC) < text.index("【图谱】") < text.index("第 1 个概念的检索结果")
    assert "detail 一律给空字符串" in text


def test_graph_rules_forbid_copying_the_entity_list():
    """图谱是**选材依据**，不是要照抄的清单——照抄的产物是一张"图的照片"。"""
    assert "不是要照抄的清单" in GRAPH_TREE_RULES
    assert "只能连不同一级分支" in GRAPH_RELATION_RULES
    assert "why" in GRAPH_RELATION_RULES


# ------------------------------------------- 6. `planned_branches` 不假设产物是树


def test_planned_branches_does_not_assume_the_folded_content_is_a_tree(monkeypatch):
    """`planned_branches` 数的是**画布最外那一层**，不是"树的一级分支"。

    产品路径上这条不变量确实恒成立——`plan is None` 只出现在两段式成功那一条
    （`if graph_mode:` 的 if 分支），`graph_mode` 只在 `diagram_type == "tree"` 时为
    真，`diagram_type` 是一次快照，`_shape_output` 收的是同一个值——所以折叠产物
    一定是 `MindMapContent`。

    这条用例故意把那个保证拿掉（把出口换成鱼骨），因为**恒成立靠的是三百行之外的
    四步推理**，而任何一步改了都不会有人知道：症状是 `AttributeError:
    'FishboneContent' object has no attribute 'root'`，整次生成 500，与"这一支数错
    了"根本不是一回事。把跨文件的隐含约束换成一次显式取值，代价是这里多走一遍。

    改前 `len(content.root.children)` 在这条用例上抛 AttributeError；改后与
    `data["branches"]` 同一个口径，报鱼的骨数。
    """
    monkeypatch.setenv(GRAPH_SKELETON_ENV, "1")
    tree, _ = _content_with(_tree())
    fishbone, _ = shapes.to_fishbone(tree, problem=TOPIC)
    # 出口换成鱼骨：两段式（`plan is None`）的产物不再是树。
    monkeypatch.setattr(
        agent_tools, "_shape_output", lambda _content, **_kwargs: fishbone)

    result = _run(_ctx(), _chat(_tree()))

    assert result.ok, result.content
    assert result.data["planned_branches"] == len(fishbone.bones)
    # 而且与 `branches` 的行数一致——同一个问题的两个出口不该有两个答案。
    assert result.data["planned_branches"] == len(result.data["branches"])
    # 鱼骨没有中心主题：`root_id` 为 None 是既有约定，顺手钉住（它与上面那处读
    # `content.root` 是同一类假设，只是那一处已经用 isinstance 判过了）。
    assert result.data["root_id"] is None
