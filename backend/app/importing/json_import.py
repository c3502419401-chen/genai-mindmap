"""我们自己的 JSON → 三种形状之一。**这是唯一无损的一条路**，也是"导出再导入"的保底。

支持三种外壳，都是真实会遇到的：

    {"title": …, "root": …}                      # 裸的 MindMapContent
    {"content": {…}}                             # 保存请求体 `SaveRequest`
    {"id": …, "version": 3, "content": {…}}      # 库里的 `MapRecord`

以及**三种形状**（本仓库导出 JSON 时写的就是这三种之一）：

    {"root": …}                    → 树        （MindMapContent）
    {"problem": …, "bones": […]}   → 鱼骨图    （FishboneContent）
    {"stages": […]}, {"edges": […]} → 流程图    （FlowContent）

## 判据：按顶层键，不按 `diagram_type`

`diagram_type` 在这一层的职责是**产物自述**（导出文件头、界面文案），不是判别键
——契约层判别 `DiagramContent` 走的是同一个判据（`extra="forbid"` + 各套的必填
字段，见 `contracts/mindmap.py` 末尾那段注释）。这里照抄那条判据，好处是"导出成功
的 JSON 一定能导入回来"这件事有同一个答案：契约认它是什么形状，导入就认什么形状。

三条边界：

  - **`stages` 优先**。一份同时有 `root` 与 `stages` 的畸形文件按流程读（契约联合
    的顺序是树在前，但 `FlowContent` 不许有 `root`，所以那种文件其实两套都过不了
    ——这里选流程只是为了让错误信息指向 `stages`）。
  - **有 `problem` 就是鱼骨**，哪怕 `bones` 是空数组。空骨的鱼骨图是合法的
    （见 `FishboneContent.bones` 的注释），而"只有主骨的草稿"正是它的常见形态。
  - **其余一律树**。包括 `diagram_type == "fishbone"` 但形状是树的旧记录：那批图
    是阶段 2 之前生成的，"它长得像树就按树导"（与契约的判别、前端 `kindOf` 的
    判别完全一致）。

## 字段逐个挑

**不把整份 dict 交给 pydantic**。原因不是洁癖：`MindMapContent` 是
`extra="forbid"`，而外部 JSON 里多一个 `"highlight"` 就会让整份导入 422，错误
信息指向一条用户改不掉的字段名。逐字段挑的代价是"我们多了一个字段要在这里补"
——这件事由 `test_import_json_roundtrip` 与三种形状各一份的往返用例钉着（导出
加了字段而导入不认，往返用例立刻红）。

取舍记在这里：

  - `source_refs` / `source_usage` **只计数、不保留**（理由见
    `normalize` 模块 docstring 的第 ⑤ 条）。
  - `color` 是调色板键就留；是别的值（hex、中文色名）时**不塞进 `color`**——
    `color` 的语义是"渲染意图"，猜错会让整张图染错色。能认出 hex 就存进
    `style.color`（`NodeStyle.color` 的注释写明它就是为导入准备的，界面不渲染，
    但用户点一次保存不会丢）。
  - `layout` / `theme` / `diagram_type` / `role` / `provenance` 原样交给
    `normalize` 校验：那里才知道 `manual` 与坐标的联动约束、哪些 `role` 是契约
    认识的。**这一层只回答"文件里写了什么"**——把校验放两份的结局是两边对同一个
    值给出不同答案。
"""
from __future__ import annotations

import json

from app.contracts.mindmap import NODE_COLORS, NODE_EXPANDABLE, NODE_ORIGINS
from app.importing.colors import normalize_hex
from app.importing.errors import ParseFailure, UnsupportedFormat
from app.importing.model import (
    SHAPE_FISHBONE,
    SHAPE_FLOWCHART,
    SHAPE_TREE,
    Budget,
    RawBone,
    RawEdge,
    RawMap,
    RawNode,
    RawStage,
)
from app.importing.text import decode_text


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _raw_node(data: dict) -> RawNode:
    """一个节点 dict → RawNode。未知键全部丢掉。"""
    node = RawNode(label=_text(data.get("label")) or "")
    old_id = data.get("id")
    if isinstance(old_id, str) and old_id.strip():
        node.old_id = old_id.strip()
    node.note = _text(data.get("note"))
    node.detail = _text(data.get("detail"))

    color = data.get("color")
    if isinstance(color, str):
        key = color.strip().lower()
        if key in NODE_COLORS:
            node.color = key
    style = data.get("style")
    if isinstance(style, dict):
        copied = {k: style.get(k) for k in ("color", "icon", "bold", "italic", "shape")}
        # 调色板键之外的颜色：认得 hex 就塞进 `style.color` 保留原值。
        if node.color is None:
            hex_value = normalize_hex(color)
            if hex_value:
                copied["color"] = hex_value
        node.style = copied

    icon = data.get("icon")
    if isinstance(icon, str) and icon.strip():
        node.icon = icon.strip()
    node.collapsed = bool(data.get("collapsed"))
    expandable = data.get("expandable")
    if isinstance(expandable, str) and expandable in NODE_EXPANDABLE:
        node.expandable = expandable
    # 来历**原样收**：这是我们自己导出的 JSON，文件里写的值就是那个节点在**导出
    # 那张图里**的来历，搬回来应当逐节点不变（`test_node_origin.py` 的往返用例钉着）。
    # 不认识的值不在这里猜：`normalize._node` 按 `NODE_ORIGINS` 判，落在集合外就是
    # `unknown`——"文件自称的出生"和"库里现在这张图"是两件事，只有前者能照抄。
    origin = data.get("origin")
    if isinstance(origin, str) and origin in NODE_ORIGINS:
        node.origin = origin
    # `role` 原样收（合法值由 `normalize` 判）：它是流程图判定节点唯一的载体，
    # 这一层顺手把它过滤掉的话，往返之后「是 / 否」两个出口就找不到自己的起点了。
    role = data.get("role")
    if isinstance(role, str) and role.strip():
        node.role = role.strip()
    pos = data.get("pos")
    if isinstance(pos, dict):
        node.pos = pos
    return node


def _parse_node(data: dict, budget: Budget) -> tuple[RawNode, int, bool]:
    """节点 dict → RawNode 树。返回 `(根, 引用条数, 是否撞硬上限)`。"""
    root = _raw_node(data)
    refs = len(data.get("source_refs") or []) if isinstance(data.get("source_refs"), list) else 0
    over_cap = False
    stack: list[tuple[dict, RawNode]] = [(data, root)]
    while stack:
        source, node = stack.pop()
        children = source.get("children")
        if not isinstance(children, list):
            continue
        for child in children:
            if not isinstance(child, dict):
                continue
            if not budget.take():
                over_cap = True
                break
            child_node = _raw_node(child)
            if isinstance(child.get("source_refs"), list):
                refs += len(child["source_refs"])
            node.children.append(child_node)
            stack.append((child, child_node))
        if over_cap:
            break
    return root, refs, over_cap


def _raw_edges(body: dict) -> list[RawEdge]:
    edges: list[RawEdge] = []
    items = body.get("edges")
    if not isinstance(items, list):
        return edges
    for item in items:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        target = item.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        label = item.get("label")
        kind = item.get("kind")
        edges.append(RawEdge(
            source=source, target=target,
            label=label if isinstance(label, str) else None,
            kind=kind if isinstance(kind, str) else None,
        ))
    return edges


def _shape_of(body: dict) -> str:
    """按**顶层键**判形状。判据与契约 `DiagramContent` 的判别同源，见模块 docstring。"""
    if isinstance(body.get("stages"), list):
        return SHAPE_FLOWCHART
    if isinstance(body.get("bones"), list) or "problem" in body:
        return SHAPE_FISHBONE
    return SHAPE_TREE


def _raw_containers(
    items: object, budget: Budget, *, kind: str,
) -> tuple[list[RawBone] | list[RawStage], int, bool]:
    """`bones` / `stages` 数组 → 中间表示。两种容器的差别只有三个字段名。

    `kind` 是 `"bone"` 或 `"stage"`。写成一个循环而不是两份：两者的**边界处理
    完全一样**（根节点也要占一个 budget、子树按同一个 `_parse_node` 读、引用计数
    累加），唯一的差别是"类别"与"阶段 id"——分两份的结局一定是某天给其中一个补了
    `budget` 而另一个忘了。
    """
    bones: list[RawBone] = []
    stages: list[RawStage] = []
    refs = 0
    over_cap = False
    if not isinstance(items, list):
        return bones if kind == "bone" else stages, refs, over_cap
    for item in items:
        if not isinstance(item, dict):
            continue
        root_data = item.get("root")
        if not isinstance(root_data, dict):
            # 容器根缺失：整根骨/整个阶段都没有内容。丢掉而不是造一个空节点——
            # 契约 `Node.label` 非空，造出来的"（无标题）骨"会在画布上多一根
            # 用户没写过的骨头。
            continue
        if not budget.take():
            over_cap = True
            break
        root, root_refs, node_over = _parse_node(root_data, budget)
        refs += root_refs
        if node_over:
            over_cap = True
        if kind == "bone":
            bones.append(RawBone(
                root=root,
                category=_text(item.get("category")),
                color=_text(item.get("color")),
                icon=_text(item.get("icon")),
            ))
        else:
            stage_id = _text(item.get("id"))
            stages.append(RawStage(
                root=root, old_id=stage_id, edges=_raw_edges(item)))
        if over_cap:
            break
    return (bones if kind == "bone" else stages), refs, over_cap


def _unwrap(document: object) -> dict:
    """剥掉 `{"content": …}` / `MapRecord` 的外壳。"""
    if isinstance(document, list):
        raise UnsupportedFormat(
            "JSON 顶层是数组：这不是一份导图。导入需要 MindMapContent（含 root 的对象）")
    if not isinstance(document, dict):
        raise UnsupportedFormat("JSON 顶层不是对象，读不出导图结构")
    inner = document.get("content")
    if isinstance(inner, dict):
        return inner
    return document


def parse(raw: bytes) -> RawMap:
    text = decode_text(raw)
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseFailure(
            f"JSON 第 {exc.lineno} 行第 {exc.colno} 列解析失败：{exc.msg}") from exc

    body = _unwrap(document)
    shape = _shape_of(body)
    budget = Budget()
    counters: dict[str, int] = {}
    over_cap = False

    if shape == SHAPE_FLOWCHART:
        stages, refs, over_cap = _raw_containers(body.get("stages"), budget, kind="stage")
        if not stages:
            # 契约 `FlowContent.stages` 是 `min_length=1`：一份没有任何阶段的"流程图"
            # 不是图。报 unsupported 而不是让它在归一化末尾 422——那时用户拿到的
            # 是"导入结果没能通过契约校验"，指不回是哪一部分的问题。
            raise UnsupportedFormat("这份 JSON 的 stages 是空的：流程图至少需要一个阶段")
        usage = body.get("source_usage")
        if isinstance(usage, list) and usage:
            counters["source_usage"] = len(usage)
        if refs:
            counters["source_refs"] = refs
        return RawMap(
            root=None,
            title=_text(body.get("title")),
            theme=body.get("theme") if isinstance(body.get("theme"), dict) else None,
            shape=SHAPE_FLOWCHART,
            stages=stages,
            relations=_dicts(body.get("relations")),
            edges=_raw_edges(body),
            counters=counters,
            over_hard_cap=over_cap,
        )

    if shape == SHAPE_FISHBONE:
        bones, refs, over_cap = _raw_containers(body.get("bones"), budget, kind="bone")
        usage = body.get("source_usage")
        if isinstance(usage, list) and usage:
            counters["source_usage"] = len(usage)
        if refs:
            counters["source_refs"] = refs
        return RawMap(
            root=None,
            title=_text(body.get("title")),
            theme=body.get("theme") if isinstance(body.get("theme"), dict) else None,
            shape=SHAPE_FISHBONE,
            problem=_text(body.get("problem")),
            bones=bones,
            relations=_dicts(body.get("relations")),
            counters=counters,
            over_hard_cap=over_cap,
        )

    root_data = body.get("root")
    if not isinstance(root_data, dict):
        raise UnsupportedFormat(
            "这份 JSON 里没有 root 节点：它不是我们的导图格式"
            "（需要 {title, root: {id, label, children}}）")

    budget.take()
    root, refs, over_cap = _parse_node(root_data, budget)

    layout = body.get("layout")
    diagram_type = body.get("diagram_type")
    theme = body.get("theme")
    if refs:
        counters["source_refs"] = refs
    usage = body.get("source_usage")
    if isinstance(usage, list) and usage:
        counters["source_usage"] = len(usage)
    return RawMap(
        root=root,
        title=_text(body.get("title")),
        layout=layout if isinstance(layout, str) else None,
        diagram_type=diagram_type if isinstance(diagram_type, str) else None,
        theme=theme if isinstance(theme, dict) else None,
        shape=SHAPE_TREE,
        relations=_dicts(body.get("relations")),
        edges=_raw_edges(body),
        counters=counters,
        over_hard_cap=over_cap,
    )


def _dicts(value: object) -> list[dict]:
    """`relations` 那一层只收 dict：非 dict 的项在归一化层没有可用的字段。"""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
