"""四个细粒度工具（`add_node` / `update_node` / `remove_node` / `move_node`）。

这些工具的卖点只有三个词：**原子、可指认、限额同源**。所以每一条用例都在守其中
一条，而不是"调通了就行"：

  - **原子**：失败时 `ctx.candidate` 一个字都不变、`ctx.batches` 不多一条、
    `ctx.revision` 不动。半批生效比失败更糟——模型会拿着一个错的树继续改。
  - **可指认**：错误码与 `path` 要指到具体字段（`parent_not_found` /
    `index_out_of_range` / `cycle_detected`），模型下一轮才改得对。
  - **限额同源**：整图 1000 节点 / 8 层。`add_node` 与 `move_node` 必须检查，
    `update_node` 不该受它影响（改字段不改结构）。

`remove_node` 的子树语义单独一组：级联删除是**选过**的方案，理由写在
`tools._remove_node` 的 docstring 里，这里把它钉死。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import build_registry
from app.contracts.mindmap import MAX_TREE_DEPTH, MindMapContent, Node

TREE = {
    "title": "示例",
    "root": {
        "id": "root", "label": "根", "children": [
            {"id": "a", "label": "甲", "children": [
                {"id": "a1", "label": "甲一", "children": [
                    {"id": "a1x", "label": "甲一之下", "children": []},
                ]},
                {"id": "a2", "label": "甲二", "children": []},
            ]},
            {"id": "b", "label": "乙", "children": []},
            {"id": "c", "label": "丙", "children": []},
        ],
    },
}


def _content() -> MindMapContent:
    return MindMapContent.model_validate(TREE)


def _ctx(
    intent: str = "edit",
    candidate: MindMapContent | None = None,
    selected_ids: list[str] | None = None,
) -> RunContext:
    request = AgentRunRequest(
        goal="改这张图", intent=intent, selected_ids=selected_ids or [],
        budget=RunBudget(max_model_calls=5, max_output_tokens=32768),
    )
    return RunContext(request=request, candidate=candidate)


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(call_id=f"c-{name}", name=name, arguments=arguments)


def _run(name: str, ctx: RunContext, **arguments):
    registry = build_registry("edit", has_library=False)
    return asyncio.run(registry.execute(_call(name, **arguments), ctx))


def _flat(root: Node) -> list[tuple[str, str]]:
    """前序遍历的 `(id, label)`，方便整体比对"树有没有变"。"""
    out: list[tuple[str, str]] = []

    def walk(node: Node) -> None:
        out.append((node.id, node.label))
        for child in node.children:
            walk(child)

    walk(root)
    return out


def _find(root: Node, node_id: str) -> Node:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.id == node_id:
            return node
        stack.extend(node.children)
    raise AssertionError(f"找不到节点 {node_id}")


def _assert_untouched(ctx: RunContext, before: MindMapContent) -> None:
    """失败之后候选树必须逐字节不变，也没有多出批次/版本。"""
    assert ctx.candidate is not None
    assert ctx.candidate is before, "失败时不该换掉 ctx.candidate 这个对象"
    assert ctx.candidate.model_dump() == before.model_dump(), "失败时候选树被改动了"
    assert ctx.batches == [], "失败时不该留下任何 op 批次"
    assert ctx.revision == 0, "失败时版本号不该前进"


# ---------------------------------------------------------------- 注册表


def test_edit_intent_registers_all_eight_write_tools():
    """`edit` 意图下八个写工具都给：四个节点 + 两个边 + 补说明 + 批量兜底。

    边工具与节点工具**并列**而不是塞进 `mindmap_edit` 的 op 表：一条边改一次，
    开销只是多一次调用；而把两种东西混进同一张 op 表会让批量工具的提示词再长
    一截，模型在批量里连边的情况本来就少。

    `mindmap_fill_details` 也算"写"：它改的是 `detail` 字段。不并进批量工具的
    理由与边工具同源——批量是"精确改、按 id 逐个说"，而补说明要改哪些节点由代码
    按长度数出来，让模型在 op 表里逐个报一遍既漏又长（见 `FillDetailsArgs`）。
    """
    names = build_registry("edit", has_library=False).names()
    assert set(names) == {
        "add_node", "update_node", "remove_node", "move_node",
        "add_edge", "remove_edge", "mindmap_fill_details", "mindmap_edit",
    }


def test_create_and_expand_registries_are_unchanged():
    """不回归：`create` 只有生成，`expand` 只有展开 + 整图体检。

    细粒度工具不能漏到这两个意图里；`audit_tree` 是例外，因为它是 `readonly`
    且不产节点——它数的是树上已有的东西（见 `tests/test_agent_audit.py`）。
    """
    assert build_registry("create_deep", has_library=False).names() == ["mindmap_generate_deep"]
    assert build_registry("expand", has_library=False).names() == [
        "audit_tree", "node_expand"]


def test_fine_grained_tools_are_not_reachable_from_create_or_expand():
    """看不到的工具模型不会去试——细粒度工具只属于 `edit`。"""
    for intent in ("create", "expand"):
        registry = build_registry(intent, has_library=False)
        result = asyncio.run(registry.execute(_call("add_node", parent_id="root", label="甲"), _ctx()))
        assert result.ok is False
        assert result.data["code"] == "unknown_tool"


# ---------------------------------------------------------------- add_node


def test_add_node_creates_one_child_with_visuals():
    ctx = _ctx(candidate=_content())
    result = _run(
        "add_node", ctx, parent_id="a", label="新要点", detail="两句说明",
        color="teal", icon="📌",
    )
    assert result.ok, result.content
    added = _find(ctx.candidate.root, result.data["node_id"])
    parent = _find(ctx.candidate.root, "a")
    assert [child.id for child in parent.children][-1] == added.id
    assert (added.label, added.detail, added.color, added.icon) == ("新要点", "两句说明", "teal", "📌")
    # 引用一律空：编辑路径不开放引用
    assert added.source_refs == []
    # 一次调用只落一个节点
    assert len(ctx.batches) == 1
    assert len(ctx.batches[0].ops) == 1


def test_add_node_respects_index_and_defaults_to_append():
    ctx = _ctx(candidate=_content())
    _run("add_node", ctx, parent_id="root", label="插到最前", index=0)
    assert [child.label for child in _find(ctx.candidate.root, "root").children][0] == "插到最前"

    ctx = _ctx(candidate=_content())
    _run("add_node", ctx, parent_id="root", label="加到末尾")
    assert [child.label for child in _find(ctx.candidate.root, "root").children][-1] == "加到末尾"


def test_add_node_failures_leave_the_tree_untouched():
    """三种失败都必须是"一个 op 都不落"。"""
    for kwargs, code in (
        ({"parent_id": "nope", "label": "x"}, "parent_not_found"),
        ({"parent_id": "c", "label": "x", "index": 5}, "index_out_of_range"),
    ):
        base = _content()
        ctx = _ctx(candidate=base)
        result = _run("add_node", ctx, **kwargs)
        assert result.ok is False, kwargs
        assert result.data["code"] == code
        _assert_untouched(ctx, base)


def test_add_node_rejects_when_tree_is_full():
    """整图 1000 节点：到顶之后加不进去，而且候选树不变。"""
    base = _content()
    ctx = _ctx(candidate=base)
    # 用真实的节点数当上限：把 `_node_limit()` 打桩成"这张图现在刚好装满"。
    # 写死一个 8 只会让用例在 TREE 长出一个节点之后悄悄失效（多出来的那个节点
    # 不再撞上限，于是"树满了"这条根本没被测到）。
    used = len(_flat(base.root))
    with patch("app.agent.tools._node_limit", return_value=used):
        result = _run("add_node", ctx, parent_id="root", label="放不下")
    assert result.ok is False
    assert result.data["code"] == "too_many_nodes"
    assert result.data["node_limit"] == used
    _assert_untouched(ctx, base)
    # 上限正好等于当前节点数、而限额是"不能超过"，所以这一条必须失败；
    # 上界给 used + 1 时同一个调用应当成功（边界两侧都钉一下）。
    with patch("app.agent.tools._node_limit", return_value=used + 1):
        ok = _run("add_node", _ctx(candidate=_content()), parent_id="root", label="刚好放得下")
    assert ok.ok, ok.content


def test_add_node_rejects_past_the_depth_limit():
    """8 层是契约硬边界：第 8 层的节点下面加不出子节点。"""
    root: dict = {"id": "d0", "label": "0", "children": []}
    cursor = root
    for depth in range(1, MAX_TREE_DEPTH):
        child = {"id": f"d{depth}", "label": str(depth), "children": []}
        cursor["children"] = [child]
        cursor = child
    base = MindMapContent.model_validate({"title": "深树", "root": root})
    ctx = _ctx(candidate=base)
    result = _run("add_node", ctx, parent_id=f"d{MAX_TREE_DEPTH - 1}", label="第 9 层")
    assert result.ok is False
    assert result.data["code"] == "too_deep"
    _assert_untouched(ctx, base)


# ---------------------------------------------------------------- 装饰字段的校验


def test_add_node_normalizes_color_case():
    """`"Blue "` 归一成 `blue`：大小写与空白不该让一次改动失败。"""
    ctx = _ctx(candidate=_content())
    result = _run("add_node", ctx, parent_id="root", label="新", color="Blue ")
    assert result.ok, result.content
    added = _find(ctx.candidate.root, result.data["node_id"])
    assert added.color == "blue"


def test_add_node_rejects_values_that_are_not_visuals_at_all():
    """写歪的 `color` / `icon` 是**参数错误**，不是"默默不改"。

    这一条与生成侧（`normalize_color` 把不认识的键丢掉）方向相反，理由在上下文：
    骨架那一次调用已经花掉，为一个装饰字段废掉整棵树是白花钱；而工具调用可以
    重来，`ok=true` 但树没变才是最难查的反馈（模型以为改好了，用户看到"没反应"）。
    """
    for kwargs in ({"color": "#2563eb"}, {"icon": "放大镜"}, {"color": "深蓝"}):
        base = _content()
        ctx = _ctx(candidate=base)
        result = _run("add_node", ctx, parent_id="root", label="新", **kwargs)
        assert result.ok is False, kwargs
        assert result.data["code"] == "invalid_tool_args", kwargs
        _assert_untouched(ctx, base)


def test_update_node_rejects_an_invalid_color_instead_of_clearing_it():
    """非法颜色不能当成"清空"：那会静默丢掉一个原本正确的值。"""
    base = _content()
    base.root.children[0].color = "red"
    ctx = _ctx(candidate=base)
    result = _run("update_node", ctx, node_id="a", color="#2563eb")
    assert result.ok is False
    assert result.data["code"] == "invalid_tool_args"
    _assert_untouched(ctx, base)


# ---------------------------------------------------------------- update_node


def test_update_node_changes_fields_and_clears_visuals():
    base = _content()
    base.root.children[0].color = "red"
    base.root.children[0].icon = "⚠️"
    base.root.children[0].note = "用户的备注"
    ctx = _ctx(candidate=base)

    result = _run("update_node", ctx, node_id="a", label="甲（改）", color="", icon="")
    assert result.ok, result.content
    node = _find(ctx.candidate.root, "a")
    assert node.label == "甲（改）"
    assert (node.color, node.icon) == (None, None)
    # 用户的备注与引用都不在这条路上
    assert node.note == "用户的备注"
    assert _flat(ctx.candidate.root)[1] == ("a", "甲（改）")


def test_update_node_failure_leaves_the_tree_untouched():
    base = _content()
    ctx = _ctx(candidate=base)
    result = _run("update_node", ctx, node_id="nope", label="x")
    assert result.ok is False
    assert result.data["code"] == "node_not_found"
    _assert_untouched(ctx, base)


def test_update_node_needs_at_least_one_field():
    """一个字段都不给是参数不合法，不是"成功但没改"。"""
    base = _content()
    ctx = _ctx(candidate=base)
    result = _run("update_node", ctx, node_id="a")
    assert result.ok is False
    # 参数模型允许全空（它要容忍"只改一处"的各种组合），所以这一条由工具自己判
    assert result.data["code"] in ("invalid_ops", "invalid_tool_args")
    _assert_untouched(ctx, base)


def test_update_node_cannot_change_source_refs():
    """引用仍是关闭的：`source_refs` 连参数都不存在，硬塞进来被判参数不合法。

    这条不是洁癖——生成路径上每一条引用都过逐字校验，编辑路径再开一个入口，
    等于多一条能凭空造引用的路（`EditChange` 的 docstring 钉着同一件事）。
    """
    base = _content()
    ctx = _ctx(candidate=base)
    result = _run("update_node", ctx, node_id="a", source_refs=[{"source_id": "s1"}])
    assert result.ok is False
    assert result.data["code"] == "invalid_tool_args"
    _assert_untouched(ctx, base)


def test_update_node_is_not_blocked_by_the_node_limit():
    """限额只管结构：树已经到 1000 个节点时，改一个字段仍然要能成功。"""
    base = _content()
    ctx = _ctx(candidate=base)
    with patch("app.agent.tools._node_limit", return_value=len(_flat(base.root))):
        result = _run("update_node", ctx, node_id="a", label="到顶也能改")
    assert result.ok, result.content
    assert _find(ctx.candidate.root, "a").label == "到顶也能改"


# ---------------------------------------------------------------- remove_node


def test_remove_node_cascades_the_whole_subtree():
    """级联删除：删 `a` 就连 `a1`（及 `a1x`）、`a2` 一起消失。

    这是**选过**的方案，不是默认行为（三个选项与理由见 `tools._remove_node`）。
    """
    base = _content()
    ctx = _ctx(candidate=base)
    result = _run("remove_node", ctx, node_id="a")
    assert result.ok, result.content
    ids = [node_id for node_id, _ in _flat(ctx.candidate.root)]
    assert ids == ["root", "b", "c"]
    # 返回值要能让人知道"少了几个"——否则用户看到的是"删了一个，少了一片"
    assert result.data["removed_count"] == 4
    assert result.data["removed_nodes"][0] == "甲"
    assert "甲一之下" in result.data["removed_nodes"]
    assert result.data["parent_id"] == "root"


def test_remove_node_refuses_the_root_and_unknown_ids():
    base = _content()
    ctx = _ctx(candidate=base)
    root_result = _run("remove_node", ctx, node_id="root")
    assert root_result.ok is False
    assert root_result.data["code"] == "cannot_remove_root"
    _assert_untouched(ctx, base)

    unknown = _run("remove_node", ctx, node_id="nope")
    assert unknown.ok is False
    assert unknown.data["code"] == "node_not_found"
    _assert_untouched(ctx, base)


def test_remove_node_leaf_only_touches_its_parent():
    base = _content()
    ctx = _ctx(candidate=base)
    result = _run("remove_node", ctx, node_id="b")
    assert result.ok, result.content
    assert result.data["removed_count"] == 1
    assert [node_id for node_id, _ in _flat(ctx.candidate.root)] == [
        "root", "a", "a1", "a1x", "a2", "c"]


# ---------------------------------------------------------------- move_node


def test_move_node_moves_the_subtree():
    base = _content()
    ctx = _ctx(candidate=base)
    result = _run("move_node", ctx, node_id="a1", new_parent_id="b")
    assert result.ok, result.content
    assert [child.id for child in _find(ctx.candidate.root, "b").children] == ["a1"]
    # 子树跟着一起走（`a1x` 还在 `a1` 下面），而不是被提升到新父节点
    assert [child.id for child in _find(ctx.candidate.root, "a1").children] == ["a1x"]
    assert [child.id for child in _find(ctx.candidate.root, "a").children] == ["a2"]


def test_move_node_rejects_cycles_and_the_root():
    base = _content()
    ctx = _ctx(candidate=base)
    cycle = _run("move_node", ctx, node_id="a", new_parent_id="a1x")
    assert cycle.ok is False
    assert cycle.data["code"] == "cycle_detected"
    _assert_untouched(ctx, base)

    self_move = _run("move_node", ctx, node_id="a", new_parent_id="a")
    assert self_move.ok is False
    assert self_move.data["code"] == "cycle_detected"
    _assert_untouched(ctx, base)

    root_move = _run("move_node", ctx, node_id="root", new_parent_id="b")
    assert root_move.ok is False
    assert root_move.data["code"] == "cannot_move_root"
    _assert_untouched(ctx, base)


def test_move_node_rejects_moves_that_break_the_depth_limit():
    """移动不改节点数，改的是深度——所以这条限额必须在这里判。

    执行器只查环不查深度（它拿不到"新父节点在第几层"），不判的话这棵树会先被
    工具"成功"移动，再在整树校验时炸成 `tree_invalid`。
    """
    # root -> p1 -> p2 -> … -> p7（7 层），外加一支高度 3 的子树挂在 root 下
    root: dict = {"id": "p1", "label": "1", "children": [
        {"id": "tall", "label": "高支", "children": [
            {"id": "tall1", "label": "高支一", "children": [
                {"id": "tall2", "label": "高支二", "children": []},
            ]},
        ]},
    ]}
    cursor = root
    for depth in range(2, 8):
        child = {"id": f"p{depth}", "label": str(depth), "children": []}
        cursor["children"].append(child)
        cursor = child
    base = MindMapContent.model_validate({"title": "深树", "root": root})
    ctx = _ctx(candidate=base)
    # 把 3 层高的 `tall` 挂到第 7 层的 `p7` 下 → 7 + 3 - 1 = 9 层，超契约
    result = _run("move_node", ctx, node_id="tall", new_parent_id="p7")
    assert result.ok is False
    assert result.data["code"] == "too_deep"
    assert result.data["depth_limit"] == MAX_TREE_DEPTH
    _assert_untouched(ctx, base)


def test_move_node_failure_on_missing_parent_is_atomic():
    base = _content()
    ctx = _ctx(candidate=base)
    result = _run("move_node", ctx, node_id="a1", new_parent_id="nope")
    assert result.ok is False
    assert result.data["code"] == "parent_not_found"
    _assert_untouched(ctx, base)


# ---------------------------------------------------------------- 批量兜底


def test_batch_tool_can_change_six_nodes_in_one_call():
    """批量兜底的价值就在这里：六处改动 = **一次**工具调用。

    细粒度工具每个节点各花一次。`RunBudget.max_tool_calls` 提到 50 之后，额度
    不再是"改六个就吃掉 6/50"那种稀缺——省下的变成了**模型往返**：一次工具调用
    之后模型要再看一眼结果，六处改动分散着做就是六轮往返（每轮 5–20 秒），
    批量是一轮。所以批量不是"旧的遗留"，在多改动的场景里它仍是更省的那条路。
    """
    ctx = _ctx(candidate=_content())
    result = _run("mindmap_edit", ctx, ops=[
        {"op": "update", "id": "a", "changes": {"color": "red", "icon": "⚠️"}},
        {"op": "update", "id": "b", "changes": {"color": "green"}},
        {"op": "update", "id": "c", "changes": {"label": "丙（改）", "color": "teal"}},
        {"op": "update", "id": "a1", "changes": {"detail": "新的说明"}},
        {"op": "update", "id": "a2", "changes": {"color": "gray"}},
        {"op": "move", "id": "a2", "new_parent_id": "b", "index": 0},
    ])
    assert result.ok, result.content
    # 一次调用、一个批次、六条 op
    assert len(ctx.batches) == 1
    assert len(ctx.batches[0].ops) == 6
    assert ctx.revision == 1
    root = ctx.candidate.root
    assert _find(root, "a").color == "red"
    assert _find(root, "c").label == "丙（改）"
    assert [child.id for child in _find(root, "b").children] == ["a2"]


def test_batch_tool_rejects_an_invalid_color_atomically():
    """批量里有一条颜色写歪 → 整批不生效（与细粒度是同一个原子承诺）。"""
    base = _content()
    ctx = _ctx(candidate=base)
    result = _run("mindmap_edit", ctx, ops=[
        {"op": "update", "id": "a", "changes": {"color": "blue"}},
        {"op": "update", "id": "b", "changes": {"color": "#2563eb"}},
    ])
    assert result.ok is False
    assert result.data["code"] == "invalid_ops"
    _assert_untouched(ctx, base)


# ---------------------------------------------------------------- 选区与事件流


def test_selection_is_a_hint_for_fine_grained_tools_too():
    """选中 `a` 却改 `b`：照样改（选区是提示，不是墙），但结果里点名 `b`。

    与批量（`mindmap_edit`）是同一套口径（`tools._scope_note`）。这里单独钉一次，
    因为细粒度工具是模型改一两处时的**首选**路径：硬拦在它们身上，等于把
    「整张图都加上时间维度」这种请求整条堵死。
    """
    base = _content()
    ctx = _ctx(candidate=base, selected_ids=["a"])
    result = _run("update_node", ctx, node_id="b", label="乙改")
    assert result.ok, result.content
    assert result.data["outside_selection"] == ["b"]
    assert "不在用户选中的节点范围内" in result.content
    assert _find(ctx.candidate.root, "b").label == "乙改"


def test_out_of_selection_is_empty_when_nothing_selected():
    """没有选区时 `outside_selection` 是空列表，而不是"什么都没算"。"""
    ctx = _ctx(candidate=_content())
    result = _run("update_node", ctx, node_id="b", label="乙改")
    assert result.ok
    assert result.data["outside_selection"] == []
    assert "不在用户选中的节点范围内" not in result.content


def test_success_carries_the_batch_for_the_ops_frame():
    """`data.batches` 是前端 op 帧的唯一来源：少了它，候选树停在原地。

    `service._run_tool` 按 `result.data["batches"]` 发 `ops` 帧，而前端靠这些帧
    增量渲染候选。工具回一句"已修改"但候选没动，用户看到的就是"点了没反应"。
    """
    ctx = _ctx(candidate=_content())
    result = _run("add_node", ctx, parent_id="root", label="新")
    assert result.ok
    assert len(result.data["batches"]) == 1
    batch = result.data["batches"][0]
    assert batch.base_revision == 0
    assert len(batch.ops) == 1
    assert result.data["batch"] is batch
    assert ctx.revision == 1
