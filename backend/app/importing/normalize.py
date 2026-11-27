"""把"文件里的内容"变成"能落进我们画布的图"：五件边界处理只在这里做。

产物是**三种图型之一**（`DiagramContent`）：树 / 鱼骨 / 流程。形状由 `RawMap.shape`
决定，那一层只有一个事实来源——`json_import` 按顶层键判，其余五个解析器从不设它
（默认 `tree`）。三种形状共用下面的五件处理与同一份 `_Builder`；差别只在"容器"是
什么：树有一个（中心主题）、鱼骨有六根骨、流程有若干阶段。

五个坑，以及每个坑的具体做法：

  ① **id 重新生成**。外部文件里的 id 可能是 `1`、`node-1`、UUID，或者我们自己的
     `n_xxx`。全部换新（`agent.ops.new_node_id`，与生成侧同一个生成器），同时建
     `旧 id → 新 id` 的映射，按它重建 `edges` / `relations` 的端点。**映射只登记
     被保留的节点**：端点落在被裁掉的分支上时那条边就是 dangling，契约层会硬拒
     （`Edge.source` 找不到 → 前端 `source: undefined` → Vue Flow 抛错白屏），
     所以它必须在进契约之前消失。流程的阶段 id 同样重新生成（`s_` 前缀），并且
     走**另一张映射表**——阶段与节点是两个命名空间，共用一张表会让一条阶段间跳转
     连到一个"碰巧同名"的步骤上。

  ② **限额**。1000 节点 / 8 层 / 50 子节点。三个都是**截断 + warn**，不是整份
     拒绝：用户导一张 XMind 进来，因为多了几个节点就什么都不给，比少几个节点糟
     得多。

     节点数用 **BFS（层序）** 保留，理由不是"实现简单"：
       - DFS 保留的是"第一个分支的最深处"——一张 1500 节点的图会剩下一根细长的
         辫子，另外五个一级分支整条消失，用户看到的图缺了五个主题，而它们才是
         他最先要看的东西；
       - BFS 保证每一层、每一个一级/二级分支都留下，被裁掉的是各支最深处的细节
         ——那是"没读到这里"，不是"这里没有了"；
       - 与深度上限同向：先撞节点上限时 BFS 的层数最小，图仍然读得清；DFS 则可能
         1000 个节点全花在 8 层里的一条路径上。
     代价是 BFS 会保留大量叶子而丢掉"某个分支的完整链路"，这个取舍写在 warning
     里（"按层序保留，深处与靠后的分支被裁掉"）。

     **多容器时是"多源 BFS"**：六根骨 / 若干阶段的根一起入队，层内按容器顺序轮转
     ——所以每根骨都留下内容，被裁掉的是各支最深处。逐容器截满会让第一根骨吃掉
     全部额度、另外五根整根消失，那样导进来的已经不是鱼骨图。理由与取舍写在
     `_Builder` 的 docstring 与 `docs/IMPORT.md` 的"截断顺序"一节。

     50 子上限是契约 `Node.children` 的硬边界（`max_length=50`），超过会 422，
     所以必须在构造时裁。

  ③ **`label` 长度**。生成侧限 16，**导入不套用**：用户手写的长标题被截到 16
     字是不可接受的。改用契约上限 200，超了才截并报一条聚合告警。空白标题兜成
     「（无标题）」——契约 `label` 非空，一条空标题会让整份文件 422。骨的
     `category` 同理（空 → 「（未分类）」、重复 → 加序号），它的 40 字上限也是
     契约硬边界。

  ④ **视觉字段的逆向映射**。`color` 只认调色板键或 `colors` 里那张精确色表；
     映射不到就丢掉（`None` 的语义是"没有颜色"，会跟随上级——那比猜一个灰色
     诚实）。`icon` 走 `normalize_icon`，它会把"写成文字的图标"丢掉。`role` 只收
     契约那四个值——它是流程图判定节点画菱形的唯一依据。

  ⑤ **`source_refs` 一律清空**，并在结果里明说。外部文件的引用不在我们的坐标
     体系里：`SourceRef.start/end` 指向的是**别人那份原文**的字符偏移，而
     `source_usage` 里的 `source_id` 在这边根本不存在。带进来的直接后果是
     `MindMapContent.validate_tree` 抛"节点 X 引用的来源不在 source_usage 中"
     ——整份导入 422，而错误信息指向一个用户改不掉的地方。`source_usage` 与它
     同源同理，一起清空。
"""
from __future__ import annotations

import unicodedata
from collections import deque
from dataclasses import dataclass, field
from uuid import uuid4

from app.agent.ops import new_edge_id, new_node_id
from app.contracts.mindmap import (
    DIAGRAM_TYPES,
    EDGE_KINDS,
    MAX_EDGE_LABEL_CHARS,
    MAX_EDGES,
    MAX_FISHBONE_BONES,
    MAX_FISHBONE_CATEGORY_CHARS,
    MAX_FISHBONE_PROBLEM_CHARS,
    MAX_FLOW_STAGES,
    MAX_NODE_CHILDREN,
    MAX_NODE_DETAIL_CHARS,
    MAX_NODE_LABEL_CHARS,
    MAX_TREE_DEPTH,
    MAX_TREE_NODES,
    NODE_EXPANDABLE,
    NODE_ORIGINS,
    DiagramContent,
    Edge,
    FishboneBone,
    FishboneContent,
    FlowContent,
    FlowStage,
    LayoutKind,
    MindMapContent,
    Node,
    NodeStyle,
    Position,
    Relation,
    RelationType,
    Theme,
    clip_label,
    normalize_color,
    normalize_icon,
)
from app.importing.colors import normalize_hex
from app.importing.errors import ParseFailure
from app.importing.model import (
    RAW_NODE_HARD_CAP,
    SHAPE_FISHBONE,
    SHAPE_FLOWCHART,
    ImportWarning,
    RawEdge,
    RawMap,
    RawNode,
    RawStage,
)

# ---- 告警码 ----
#
# 与 `mindmaps/edges.py` 的三个码同一个风格：码是稳定的（前端可以按码做分组或
# 折叠），文案是给人读的。**不加 `level` 字段**：导入没有 info 档——用户上传
# 一个文件，凡是我们动过的地方都值得他知道。
TRUNCATED_NODES = "truncated_nodes"
TRUNCATED_DEPTH = "truncated_depth"
TRUNCATED_CHILDREN = "truncated_children"
TRUNCATED_BONES = "truncated_bones"
TRUNCATED_STAGES = "truncated_stages"
CATEGORY_TRUNCATED = "category_truncated"
CATEGORY_MISSING = "category_missing"
CATEGORY_DUPLICATE = "category_duplicate"
LABEL_TRUNCATED = "label_truncated"
LABEL_MISSING = "label_missing"
TITLE_TRUNCATED = "title_truncated"
TEXT_TRUNCATED = "text_truncated"
PROBLEM_TRUNCATED = "problem_truncated"
SOURCE_REFS_CLEARED = "source_refs_cleared"
EDGES_DROPPED = "edges_dropped"
RELATIONS_DROPPED = "relations_dropped"
FIELDS_DROPPED = "fields_dropped"
DUPLICATE_IDS = "duplicate_ids"
HARD_CAP = "hard_cap"

# 契约字段的取值集合，用来判断"文件给的值能不能原样收下"。
_LAYOUTS = {item.value for item in LayoutKind}
_THEMES_PRESETS = {"default", "slate", "forest", "sunset", "mono"}
_FONTS = {"system", "serif", "mono"}
_DIRECTIONS = {"right", "left", "both"}
_SHAPES = {"rounded", "rect", "pill"}
# `Node.role` 的四个取值。契约里那个字段是 `Literal` 而没有配套常量（它只有四个
# 值、只在一处校验），要一份字面量集合时只能在这里再写一遍——两边不一致的表现是
# "某个角色在归一化层被丢掉，画出来永远是方块"，所以它必须与 `Node.role` 同源。
_ROLES = {"step", "decision", "start", "end"}
_RELATION_TYPES = {item.value for item in RelationType}
_PROVENANCES = {"user", "ai", "import"}
#: 骨类别缺失时的兜底。与 `LABEL_MISSING` 的「（无标题）」同一个风格：它必须
#: **看得出来是占位**，否则用户会以为那根骨本来就叫这个名字。
_CATEGORY_PLACEHOLDER = "（未分类）"

# 位置字段的上下界，与 `contracts.mindmap.Position` 的 Field 一致。
_POS_LIMIT = 1e6


def one_line(text: object) -> str:
    """把任意值折成单行字符串，并**去掉控制字符**。

    非字符串按空处理（外部文件里 label 可能是数字）。

    去控制字符不是洁癖：markdown / JSON 里可以塞进 NUL、BEL、ESC。它们进了
    `label` 之后有三个真实后果——落库的 JSON 里是多字节转义（排查时看不见）、
    前端把它渲染成一个看不见的方块、终端里 `\\x1b[31m` 会让导出文件带颜色。
    折成空格而不是删掉：`abc\\x00def` 是"两个词"，不是"一个词 abcdef"。
    """
    if isinstance(text, str):
        cleaned = "".join(
            " " if unicodedata.category(ch) == "Cc" else ch for ch in text)
        return " ".join(cleaned.split())
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return str(text)
    return ""


def _clip_text(text: object, limit: int) -> tuple[str | None, bool]:
    """折行 + 按码点截断。返回 `(值, 是否截过)`；空文本折成 None。"""
    folded = one_line(text)
    if not folded:
        return None, False
    if len(folded) <= limit:
        return folded, False
    return clip_label(folded, limit), True


@dataclass
class _Survey:
    """整棵 raw 树的体检数字。先数一遍，才能报出准确的"文件里有多少"。"""

    total: int = 0
    max_depth: int = 0
    # `(label, 子节点总数)`，只登记超过 50 的节点。
    over_children: list[tuple[str, int]] = field(default_factory=list)
    has_pos: bool = False


def survey(root: RawNode) -> _Survey:
    """数节点、量深度、找出子节点超限的节点。

    用显式栈而不是递归：外部文件的深度不受我们控制（一份缩进 500 层的 markdown
    完全可能），递归会在 `RecursionError` 上崩掉——那是一个 500 而不是 400，
    用户拿不到任何可操作的信息。
    """
    stats = _Survey()
    stack: list[tuple[RawNode, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        stats.total += 1
        stats.max_depth = max(stats.max_depth, depth)
        # 必须是**有效**坐标：`{"x": "左边"}` 这种值会让"有坐标"成立，于是一张
        # 声明 manual 的图被原样收下，然后 `validate_tree` 抛"手动布局至少需要
        # 一个节点带坐标"——错误指向一个用户改不掉的地方。
        if _pos(node.pos) is not None:
            stats.has_pos = True
        if len(node.children) > MAX_NODE_CHILDREN:
            stats.over_children.append((one_line(node.label), len(node.children)))
        for child in node.children:
            stack.append((child, depth + 1))
    return stats


def survey_forest(roots: list[RawNode]) -> _Survey:
    """多棵并列子树合并成一份体检（鱼骨的六根骨、流程的若干阶段）。

    三项的合并方式不同，每一项都有理由：

      - `total` **相加**：节点上限是"整张图"的承载能力（契约 `_scan_tree` 用同一个
        `seen` 合计算），六根骨各 900 个节点与一棵 5400 节点的树一样存不下。
      - `max_depth` 取**最大值**：契约的深度是"按每棵子树从 1 数"（一根骨挂在主骨
        上，但画布上它是一列自己的）。保持这条口径，`MAX_TREE_DEPTH` 的告警说的
        才与树那边是同一件事。
      - `has_pos` / `over_children` 取并集：它们描述的是"文件里有没有这种东西"。
    """
    merged = _Survey()
    for root in roots:
        stats = survey(root)
        merged.total += stats.total
        merged.max_depth = max(merged.max_depth, stats.max_depth)
        merged.over_children.extend(stats.over_children)
        merged.has_pos = merged.has_pos or stats.has_pos
    return merged


@dataclass
class _Tally:
    """构建过程中的计数。全部汇成告警，不静默丢东西。"""

    clipped_labels: int = 0
    missing_labels: int = 0
    clipped_texts: int = 0
    duplicate_ids: int = 0
    kept: int = 0
    positions: int = 0
    # 鱼骨/流程专属的四类。树永远为 0（它只有一个容器，不存在"骨超过六根"或
    # "类别缺失"这种事），所以它们不影响树的任何一条告警。
    clipped_categories: int = 0
    missing_categories: int = 0
    duplicate_categories: int = 0
    dropped_containers: int = 0


def _style(value: object) -> NodeStyle | None:
    """只保留 `NodeStyle` 认识的四个字段，其余丢掉。

    `NodeStyle` 是 `extra="forbid"`：外部文件里的 `{"fontSize": 14}` 会让整个
    构造 422，而它只是个装饰字段。所以这里逐字段挑，挑不出东西就返回 None。
    """
    if not isinstance(value, dict):
        return None
    data: dict[str, object] = {}
    color = normalize_hex(value.get("color"))
    if color:
        data["color"] = color
    icon = value.get("icon")
    if isinstance(icon, str) and 0 < len(icon) <= 40:
        data["icon"] = icon
    for flag in ("bold", "italic"):
        if isinstance(value.get(flag), bool):
            data[flag] = value[flag]
    shape = value.get("shape")
    if isinstance(shape, str) and shape in _SHAPES:
        data["shape"] = shape
    return NodeStyle(**data) if data else None


def _pos(value: object) -> Position | None:
    """坐标：两个数字、在 ±1e6 内才算数。"""
    if not isinstance(value, dict):
        return None
    try:
        x = float(value["x"])
        y = float(value["y"])
    except (KeyError, TypeError, ValueError):
        return None
    if abs(x) > _POS_LIMIT or abs(y) > _POS_LIMIT:
        return None
    return Position(x=x, y=y)


def _node(raw: RawNode, taken: set[str], tally: _Tally, keep_pos: bool) -> Node:
    """一个 raw 节点 → 契约 `Node`（子节点留空，由 BFS 填）。"""
    label, clipped = _clip_text(raw.label, MAX_NODE_LABEL_CHARS)
    if not label:
        # 契约 `label` 非空：一条空标题会让整份文件 422，而用户没法定位到它。
        label = "（无标题）"
        tally.missing_labels += 1
    elif clipped:
        tally.clipped_labels += 1

    note, note_clipped = _clip_text(raw.note, MAX_NODE_DETAIL_CHARS)
    detail, detail_clipped = _clip_text(raw.detail, MAX_NODE_DETAIL_CHARS)
    if note_clipped or detail_clipped:
        tally.clipped_texts += 1

    pos = _pos(raw.pos) if keep_pos else None
    if pos is not None:
        tally.positions += 1

    kwargs: dict[str, object] = {
        "id": new_node_id(taken),
        "label": label,
        "note": note,
        "detail": detail,
        "collapsed": bool(raw.collapsed),
        "color": normalize_color(raw.color),
        "icon": normalize_icon(raw.icon),
        "style": _style(raw.style),
        "pos": pos,
    }
    if raw.expandable in NODE_EXPANDABLE:
        kwargs["expandable"] = raw.expandable
    # `role` 是流程图判定节点唯一的载体（见 `RawNode.role`）。取值不在契约的四个
    # 里就丢掉：文件自造的角色画出来只是普通方块，而留着它会让 `Node(**kwargs)`
    # 在契约层 422——为一个装饰性字段把整份导入判死，代价方向错了。
    if raw.role in _ROLES:
        kwargs["role"] = raw.role
    # 来历：文件说了、且在值域内就照抄，否则 `unknown`。**这是六种导入格式唯一的
    # 一个 `RawNode → Node` 转换点**，所以判定只写这一处——六个解析器各写一遍的
    # 结局一定是 markdown 那条路忘了搬、而往返用例只覆盖 JSON 那条路。
    #
    # 缺省 `unknown` 而不是 `manual`：第三方文件里没有这个概念，"不知道"是真话；
    # 猜成 `manual` 会让界面说"这个节点不来自生成链路"——那句话在导进来的图上
    # 也许对，但它是我们编的，不是文件说的。
    if raw.origin in NODE_ORIGINS:
        kwargs["origin"] = raw.origin
    return Node(**kwargs)


def _title(raw: RawMap) -> tuple[str, bool]:
    """树形标题：文件给的 → 根节点文字 → 「导入的导图」；超 200 截断。"""
    root_label = raw.root.label if raw.root is not None else ""
    text = one_line(raw.title) or one_line(root_label) or "导入的导图"
    if len(text) <= 200:
        return text, False
    return clip_label(text, 200), True


def _theme(raw_theme: object) -> Theme:
    """只保留 `Theme` 认识的字段。认不出的值丢掉（不猜默认值之外的语义）。"""
    if not isinstance(raw_theme, dict):
        return Theme()
    data: dict[str, object] = {}
    preset = raw_theme.get("preset")
    if isinstance(preset, str) and preset in _THEMES_PRESETS:
        data["preset"] = preset
    accent = normalize_hex(raw_theme.get("accent"))
    if accent:
        data["accent"] = accent
    font = raw_theme.get("font")
    if isinstance(font, str) and font in _FONTS:
        data["font"] = font
    direction = raw_theme.get("direction")
    if isinstance(direction, str) and direction in _DIRECTIONS:
        data["direction"] = direction
    return Theme(**data)


def _resolve(
    endpoint: str, id_map: dict[str, str], label_map: dict[str, str],
    stage_map: dict[str, str] | None = None,
) -> str | None:
    """边的端点：先按旧 id 查，再按文字查（流程的阶段间边还有一个阶段命名空间）。

    两条路都要：JSON 的边是 id，markdown / opml / puml 的边是**节点文字**（导出
    时把 id 换成了人看得懂的名字）。`label_map` 的键是折叠过的原始文字，所以这里
    也折叠一次——用户把 markdown 里的多行标题合并回一行之后，边仍然连得上。

    `stage_map` 只有流程的阶段间边会传非空（那批边的端点必须是阶段 id，见契约
    `FlowStage` 的 docstring）。它在 `id_map` **之后**查，看起来像"节点 id 优先"，
    但在那条路上调用方传的是**空的** `id_map` 与只装阶段标题的 `label_map`——两个
    命名空间因此不会互相串。串一次的后果不是报错，而是一条连到某个"碰巧同名"的
    节点上的线：画出来完全正常，指向却是错的。
    """
    found = id_map.get(endpoint)
    if found is not None:
        return found
    if stage_map is not None:
        found = stage_map.get(endpoint)
        if found is not None:
            return found
    return label_map.get(one_line(endpoint))


@dataclass
class _EdgeResult:
    edges: list[Edge] = field(default_factory=list)
    dropped: int = 0
    reasons: list[str] = field(default_factory=list)


def _rebuild_edges(
    raw_edges: list[RawEdge], id_map: dict[str, str], label_map: dict[str, str],
    *, stage_map: dict[str, str] | None = None,
) -> _EdgeResult:
    """按新 id 重建附加边，丢 dangling / 自环 / 重复 / 超限。

    `stage_map` 只服务流程的阶段间边（见 `_resolve`）；树与阶段内边不传，行为与
    加这个参数之前逐字节相同。
    """
    result = _EdgeResult()
    taken: set[str] = set()
    seen: set[tuple[str, str, str | None]] = set()
    for raw_edge in raw_edges:
        source = _resolve(raw_edge.source, id_map, label_map, stage_map)
        target = _resolve(raw_edge.target, id_map, label_map, stage_map)
        if source is None or target is None:
            result.dropped += 1
            result.reasons.append("端点找不到")
            continue
        if source == target:
            result.dropped += 1
            result.reasons.append("自环")
            continue
        label, _ = _clip_text(raw_edge.label, MAX_EDGE_LABEL_CHARS)
        key = (source, target, label)
        if key in seen:
            result.dropped += 1
            result.reasons.append("重复")
            continue
        if len(result.edges) >= MAX_EDGES:
            result.dropped += 1
            result.reasons.append("超过 200 条")
            continue
        seen.add(key)
        kind = raw_edge.kind if raw_edge.kind in EDGE_KINDS else None
        result.edges.append(Edge(
            id=new_edge_id(taken), source=source, target=target, label=label, kind=kind))
    return result


def _new_relation_id(taken: set[str]) -> str:
    """关系 id。`r_` 前缀与节点（`n_`）、边（`e_`）分开，排查时一眼分得清。"""
    while True:
        candidate = f"r_{uuid4().hex}"
        if candidate not in taken:
            return candidate


def _rebuild_relations(
    raw_relations: list[dict], id_map: dict[str, str],
) -> tuple[list[Relation], int]:
    """重建跨节点关系。端点要重映射，`type` 不在枚举里的丢掉。

    `provenance` **文件说了就照收，没说才记 `import`**。这句话改过一次，理由值得
    留着：原来一律写 `import`（"导入带来的关系不是用户画的"），对 markdown / opml /
    puml 完全成立——那些格式根本没有这个字段。但对我们自己的 JSON 就错了：导出时
    写进去的 `user` / `ai` 是**这张图的一部分**（"这条关系是用户连的还是模型给的"），
    一刀盖成 `import` 等于往返之后丢了它，而"导出再导入必须一样"正是这条路存在的
    理由。缺省仍是 `import`，所以外部格式的行为一个字节没变。
    """
    kept: list[Relation] = []
    taken: set[str] = set()
    dropped = 0
    for item in raw_relations:
        if not isinstance(item, dict):
            dropped += 1
            continue
        source = _resolve(str(item.get("source") or ""), id_map, {})
        target = _resolve(str(item.get("target") or ""), id_map, {})
        kind = item.get("type")
        if source is None or target is None or source == target or kind not in _RELATION_TYPES:
            dropped += 1
            continue
        label, _ = _clip_text(item.get("label"), 100)
        evidence, _ = _clip_text(item.get("evidence"), 300)
        provenance = item.get("provenance")
        kept.append(Relation(
            id=_new_relation_id(taken), source=source, target=target, type=kind,
            label=label, evidence=evidence,
            # 外部文件带来的关系不是用户在这张图上画的，也不是模型给的：
            # `provenance` 的第三个取值就是为导入准备的。文件自己写了合法值时
            # 以文件为准（理由见 docstring）。
            provenance=provenance if provenance in _PROVENANCES else "import",
        ))
    return kept, dropped


def _layout_and_pos(raw: RawMap, stats: _Survey) -> tuple[LayoutKind, bool]:
    """布局与"要不要保留坐标"。

    契约对两者的约束是双向的：`manual` 必须至少有一个坐标，非 `manual` 不许有
    坐标（`validate_tree` 两条都会硬拒）。所以：

      - 文件说 manual 但一个有效坐标都没有 → 降级 `tree`；
      - 文件没说 manual 却带着坐标 → 丢掉坐标（保留布局）。
    """
    layout = LayoutKind(raw.layout) if raw.layout in _LAYOUTS else LayoutKind.tree
    if layout is LayoutKind.manual:
        return (LayoutKind.manual, True) if stats.has_pos else (LayoutKind.tree, False)
    return layout, False

class _Builder:
    """id 重生成、映射登记、限额截断的**唯一实现**。三种形状共用。

    ## 多源 BFS = 容器轮转（这是整份文件里唯一一处"策略"而不是"校验"）

    树只有一棵，BFS 的含义没有歧义。鱼骨有六根骨、流程有二十个阶段，`MAX_TREE_NODES`
    那一千个额度怎么分有三条路：

      - **逐个容器截满再下一个**：第一根骨吃掉 1000，另外五根整根消失。
      - **每层各容器平均分**：要额外写一套配额分配，而"平均"在容器大小悬殊时反而
        更糟（一根 3 个原因的骨与一根 800 个原因的骨各分 500，后者被砍掉的正是最
        该保留的部分）。
      - **多源 BFS**：把六个容器的根同时放进队列，层序出队。第 1 层是六根骨，
        第 2 层是每根骨的第一个原因（按容器顺序），第 3 层是每根骨的第二个原
        因……**层内按容器轮转，整体仍是层序**。

    选第三条，理由是它与既有 BFS 的立意是同一条：BFS 保证"每一个一级分支都留下，
    被裁掉的是各支最深处"。在鱼骨上"一级分支"就是那六根骨——**鱼骨图的读法本身
    就是六类成因并列对照**，砍掉五类之后那已经不是一张鱼骨图，而是一棵长得像鱼骨
    的树。流程同理：阶段是主轴，少一个阶段整条流程就断了。

    实现上没有额外的轮转代码：多源初始化之后，队列的出队顺序本来就是"层序 + 层内
    按入队顺序"，而入队顺序就是容器顺序。策略是队列的性质，不是一段 if。
    """

    def __init__(self, tally: _Tally, keep_pos: bool) -> None:
        self.tally = tally
        self.keep_pos = keep_pos
        self.taken: set[str] = set()
        #: 旧 id → 新 id。只登记**被保留**的节点：端点落在被裁掉的分支上时那条边
        #: 就是 dangling，契约层会硬拒，所以它必须在进契约之前消失。
        self.id_map: dict[str, str] = {}
        #: 旧文字 → 新 id。文本格式（markdown / opml / puml）的边端点只有文字。
        self.label_map: dict[str, str] = {}

    def new(self, raw: RawNode) -> Node:
        """建一个节点（不带子节点），并计入 `tally.kept`。"""
        node = _node(raw, self.taken, self.tally, self.keep_pos)
        self.tally.kept += 1
        return node

    def link(self, raw: RawNode, node: Node, *, is_root: bool = False) -> None:
        """登记新 id 与文字。`is_root` 那一支不做重复检查——容器根是最先登记的，
        它自己的旧 id 不可能已经出现过（与树路径原来的 `setdefault` 一致）。"""
        if raw.old_id:
            if is_root:
                self.id_map.setdefault(raw.old_id, node.id)
            elif raw.old_id in self.id_map:
                self.tally.duplicate_ids += 1
            else:
                self.id_map[raw.old_id] = node.id
        for key in {one_line(raw.label), node.label}:
            if key:
                self.label_map.setdefault(key, node.id)

    def container(self, raw: RawNode) -> Node:
        """建一个容器根（树的中心主题、一根骨、一个阶段）。"""
        node = self.new(raw)
        self.link(raw, node, is_root=True)
        return node

    def grow(
        self, seeds: list[tuple[RawNode, Node, int]], budget: int = MAX_TREE_NODES,
    ) -> list[set[str]]:
        """从这些容器根出发按层序展开，返回**每个容器最终拥有的节点 id 集合**。

        返回 id 集合是为了流程的阶段内边：那些边的端点必须落在**本阶段**里，而
        `id_map` / `label_map` 是全局的（一份文件里所有阶段共用）。不按容器过滤的话，
        一条"阶段 1 的步骤 → 阶段 2 的步骤"的边会被连上，然后契约层硬拒整份图
        （`边 X 的终点节点 Y 不在阶段 s_1 里`）——一条边连错换来的是整张图导不进来。
        """
        queue: deque[tuple[RawNode, Node, int, int]] = deque(
            (raw, node, depth, index)
            for index, (raw, node, depth) in enumerate(seeds))
        owned: list[set[str]] = [{node.id} for _raw, node, _depth in seeds]
        while queue and self.tally.kept < budget:
            raw_node, node, depth, owner = queue.popleft()
            for raw_child in raw_node.children[:MAX_NODE_CHILDREN]:
                if self.tally.kept >= budget:
                    break
                if depth + 1 > MAX_TREE_DEPTH:
                    continue
                child = self.new(raw_child)
                node.children.append(child)
                self.link(raw_child, child)
                owned[owner].add(child.id)
                queue.append((raw_child, child, depth + 1, owner))
        return owned


def _new_stage_id(taken: set[str]) -> str:
    """阶段 id。`s_` 前缀与节点（`n_`）、边（`e_`）、关系（`r_`）分开。

    契约要求阶段 id 与节点 id 是**两个命名空间**（`FlowStage` 的 docstring）。
    前缀不是校验上的必需品，但它让"这个 id 属于哪一层"在日志与导出的 JSON 里一眼
    看得出来——排查"这条线连的是阶段还是步骤"时，那一眼就是全部成本。
    """
    while True:
        candidate = f"s_{uuid4().hex}"
        if candidate not in taken:
            return candidate


def _clip_title(text: str) -> tuple[str, bool]:
    """标题的契约上限（200）与截断标记。"""
    if len(text) <= 200:
        return text, False
    return clip_label(text, 200), True


def _container_title(raw: RawMap, fallback: str | None) -> tuple[str, bool]:
    """鱼骨/流程的标题：文件给的 → 容器给的兜底 → 「导入的导图」。

    没有 `root.label` 这一档（那两种形状顶层没有一棵树），所以兜底由调用方给：
    鱼骨用 `problem`，流程用第一个阶段的标题。
    """
    return _clip_title(one_line(raw.title) or one_line(fallback) or "导入的导图")


def _base_warnings(raw: RawMap) -> list[ImportWarning]:
    """解析器留下的痕迹 + 解析期硬上限。三种形状共用，且**排在结果最前面**：
    它们说的是"这个文件本身不完整"，比任何归一化告警都更靠前。"""
    warnings: list[ImportWarning] = [
        ImportWarning(code=code, message=message) for code, message in raw.hints
    ]
    if raw.over_hard_cap:
        warnings.append(ImportWarning(
            code=HARD_CAP,
            message=(f"文件超过 {RAW_NODE_HARD_CAP} 个节点，只读取了前 {RAW_NODE_HARD_CAP} 个；"
                     "剩下的部分没有被解析，导入结果可能缺少靠后的分支"),
        ))
    return warnings


def _limit_warnings(stats: _Survey, tally: _Tally, *, kept_note: str) -> list[ImportWarning]:
    """限额告警。判据是**文件自身的属性**，不是"我们丢了多少"——所以数字取自
    `stats`（文件里有多少），只有"保留了几个"取自 `tally`。

    `kept_note` 是三种形状唯一不同的那句话（树："深处与靠后的分支被裁掉"；鱼骨与
    流程要说清"每一根骨/每一个阶段都还在，少的是各自深处的内容"）。不拼成一段参数化
    的文案，是因为那句话是用户读完这条告警之后唯一会记住的部分。
    """
    warnings: list[ImportWarning] = []
    if stats.total > MAX_TREE_NODES:
        warnings.append(ImportWarning(
            code=TRUNCATED_NODES,
            message=(f"文件共有 {stats.total} 个节点，超过上限 {MAX_TREE_NODES}；"
                     f"已按层序保留前 {tally.kept} 个，{kept_note}"),
        ))
    if stats.max_depth > MAX_TREE_DEPTH:
        warnings.append(ImportWarning(
            code=TRUNCATED_DEPTH,
            message=(f"文件最深 {stats.max_depth} 层，超过上限 {MAX_TREE_DEPTH} 层；"
                     f"第 {MAX_TREE_DEPTH + 1} 层及以下的节点已丢弃（共 {stats.total} 个节点里"
                     "有一部分在这个范围内）"),
        ))
    if stats.over_children:
        names = "、".join(f"「{label or '（无标题）'}」（{count} 个）"
                         for label, count in stats.over_children[:3])
        more = (f" 等 {len(stats.over_children)} 个节点"
                if len(stats.over_children) > 3 else "")
        warnings.append(ImportWarning(
            code=TRUNCATED_CHILDREN,
            message=(f"有子节点超过上限 {MAX_NODE_CHILDREN} 个的节点：{names}{more}；"
                     f"每个节点只保留前 {MAX_NODE_CHILDREN} 个子节点"),
        ))
    if tally.clipped_labels:
        warnings.append(ImportWarning(
            code=LABEL_TRUNCATED,
            message=(f"{tally.clipped_labels} 个节点的标题超过 {MAX_NODE_LABEL_CHARS} 字，"
                     "已截到上限"),
        ))
    if tally.missing_labels:
        warnings.append(ImportWarning(
            code=LABEL_MISSING,
            message=(f"{tally.missing_labels} 个节点在文件里没有文字，已填为「（无标题）」。"
                     "节点标题不能为空，空标题会让整份导图存不回去"),
        ))
    if tally.clipped_texts:
        warnings.append(ImportWarning(
            code=TEXT_TRUNCATED,
            message=(f"{tally.clipped_texts} 个节点的正文超过 {MAX_NODE_DETAIL_CHARS} 字，"
                     "已截到上限"),
        ))
    if tally.duplicate_ids:
        warnings.append(ImportWarning(
            code=DUPLICATE_IDS,
            message=(f"文件里有 {tally.duplicate_ids} 个重复的节点 id；"
                     "指向同一个 id 的附加关系只连到第一个节点上"),
        ))
    return warnings


def _refs_warning(raw: RawMap) -> ImportWarning | None:
    """⑤：引用与来源摘要一律清空。三种形状都要说这一句。"""
    refs_cleared = raw.counters.get("source_refs", 0)
    usage_cleared = raw.counters.get("source_usage", 0)
    if not (refs_cleared or usage_cleared):
        return None
    parts = []
    if refs_cleared:
        parts.append(f"{refs_cleared} 处节点引用")
    if usage_cleared:
        parts.append(f"{usage_cleared} 条来源摘要")
    return ImportWarning(
        code=SOURCE_REFS_CLEARED,
        message=(f"导入的引用已清空（{'、'.join(parts)}）：外部文件的字符偏移与"
                 "来源 ID 指向的是别处的原文，带进来会让引用核验整份失败"),
    )


def _edge_warnings(
    edge_result: _EdgeResult, relations: list[Relation], relations_dropped: int, *,
    what: str = "附加关系",
) -> list[ImportWarning]:
    """边与关系被动过手的痕迹。`what` 让流程的"阶段之间的跳转"与树的"附加关系"
    各自说清是哪一层——两份文案里都出现"边"字，不说清就分不出是哪一种。"""
    warnings: list[ImportWarning] = []
    if edge_result.dropped:
        reasons = "、".join(sorted(set(edge_result.reasons)))
        warnings.append(ImportWarning(
            code=EDGES_DROPPED,
            message=(f"{edge_result.dropped} 条{what}没有导入（{reasons}）；"
                     f"其余 {len(edge_result.edges)} 条已保留"),
        ))
    if relations_dropped:
        warnings.append(ImportWarning(
            code=RELATIONS_DROPPED,
            message=(f"{relations_dropped} 条跨节点关系没有导入（端点找不到或类型不认识）；"
                     f"其余 {len(relations)} 条已保留"),
        ))
    return warnings


def _normalize_tree(raw: RawMap, warnings: list[ImportWarning]) -> MindMapContent:
    """树：**这条路的输出与支持三种形状之前逐字节相同**。

    它没有鱼骨/流程那种"坐标随便留"的自由：契约 `validate_tree` 对"布局与坐标"有
    双向约束（manual 必须至少一个坐标、非 manual 不许有坐标），所以布局要跟着坐标
    一起判。
    """
    if raw.root is None:
        # 不可达：五个外部解析器都必填 `root`，`json_import` 判出 tree 之前也已经
        # 确认过 `root` 是个 dict。留着这一句是为了让类型收窄有依据，而不是让某个
        # `None` 一路走到 `survey()` 里变成一个 TypeError。
        raise ParseFailure("树形内容缺少 root 节点")

    stats = survey(raw.root)
    layout, keep_pos = _layout_and_pos(raw, stats)

    title, title_clipped = _title(raw)
    if title_clipped:
        warnings.append(ImportWarning(
            code=TITLE_TRUNCATED, message="导图标题超过 200 字，已截断"))

    # ---- ① ② ③：BFS 构造 + 重新生成 id ----
    tally = _Tally()
    builder = _Builder(tally, keep_pos)
    root = builder.container(raw.root)
    builder.grow([(raw.root, root, 1)])

    warnings.extend(_limit_warnings(
        stats, tally, kept_note="深处与靠后的分支被裁掉"))

    # ---- ④：顺手记下"布局被动过" ----
    if raw.layout in _LAYOUTS and LayoutKind(raw.layout) is LayoutKind.manual \
            and not keep_pos:
        warnings.append(ImportWarning(
            code=FIELDS_DROPPED,
            message="文件声明的是手动布局，但没有任何有效坐标；已按自动布局导入",
        ))
    elif raw.layout not in (None, *_LAYOUTS):
        warnings.append(ImportWarning(
            code=FIELDS_DROPPED,
            message=f"文件里的布局值「{raw.layout}」不认识，已按思维导图布局导入",
        ))
    elif raw.layout in _LAYOUTS and LayoutKind(raw.layout) is not LayoutKind.manual \
            and stats.has_pos:
        warnings.append(ImportWarning(
            code=FIELDS_DROPPED,
            message="文件里带着节点坐标，但布局不是手动布局；坐标已丢弃"
                    "（自动布局下它们会被当成用户意图，画出来是一团乱麻）",
        ))
    elif stats.has_pos:
        # 文件没写 layout，但每个节点都带坐标。契约不允许"自动布局 + 坐标"，
        # 所以坐标丢掉；这里说出来，免得用户以为我们读漏了。
        warnings.append(ImportWarning(
            code=FIELDS_DROPPED,
            message="文件里带着节点坐标，但没有声明手动布局；坐标已丢弃",
        ))

    refs_warning = _refs_warning(raw)
    if refs_warning is not None:
        warnings.append(refs_warning)

    edge_result = _rebuild_edges(raw.edges, builder.id_map, builder.label_map)
    relations, relations_dropped = _rebuild_relations(raw.relations, builder.id_map)
    warnings.extend(_edge_warnings(edge_result, relations, relations_dropped))

    diagram_type = raw.diagram_type if raw.diagram_type in DIAGRAM_TYPES else "tree"
    # 最后一道保险：文件里**有**有效坐标，但带坐标的那个节点正好被限额裁掉了
    # （1000 节点截断时完全可能）。契约要求 manual 至少一个坐标，否则整份 422，
    # 所以这里退回自动布局，而不是把一张"差一个坐标"的图判死。
    if layout is LayoutKind.manual and tally.positions == 0:
        layout = LayoutKind.tree
        warnings.append(ImportWarning(
            code=FIELDS_DROPPED,
            message="原来的手动布局坐标都在被裁掉的节点上，已按自动布局导入",
        ))
    return MindMapContent(
        title=title,
        root=root,
        layout=layout,
        theme=_theme(raw.theme),
        relations=relations,
        edges=edge_result.edges,
        diagram_type=diagram_type,
    )


def _bone_warnings(tally: _Tally, total: int) -> list[ImportWarning]:
    """骨本身的四种"被动过手"：超六根、类别被截断、类别缺失、类别重复。"""
    warnings: list[ImportWarning] = []
    if tally.dropped_containers:
        warnings.append(ImportWarning(
            code=TRUNCATED_BONES,
            message=(f"这份鱼骨图有 {total} 根骨，超过上限 {MAX_FISHBONE_BONES} 根；"
                     f"只导入了前 {MAX_FISHBONE_BONES} 根，后面的骨与它们的内容没有导入"),
        ))
    if tally.clipped_categories:
        warnings.append(ImportWarning(
            code=CATEGORY_TRUNCATED,
            message=(f"{tally.clipped_categories} 根骨的类别超过 "
                     f"{MAX_FISHBONE_CATEGORY_CHARS} 字，已截到上限"),
        ))
    if tally.missing_categories:
        warnings.append(ImportWarning(
            code=CATEGORY_MISSING,
            message=(f"{tally.missing_categories} 根骨在文件里没有类别，"
                     f"已填为「{_CATEGORY_PLACEHOLDER}」"),
        ))
    if tally.duplicate_categories:
        warnings.append(ImportWarning(
            code=CATEGORY_DUPLICATE,
            message=(f"{tally.duplicate_categories} 根骨的类别与前面的重复，已加上序号"
                     "（骨的类别必须唯一，否则两根骨在画布上抢同一个位置）"),
        ))
    return warnings


def _normalize_fishbone(raw: RawMap, warnings: list[ImportWarning]) -> FishboneContent:
    """鱼骨图：主骨上一句话 + 至多六根骨。

    四处与树不同的判断：

      - **`problem` 单独兜底**。契约要求它非空（`min_length=1`），而"文件里没写
        主骨上那句"是可能的（手改过的 JSON）。兜底取 `title`：拿文件名当主骨上的
        话是次优，但比让整份图 422 强，而且它至少是这张图的主题。
      - **类别不能重复**（契约硬拒）。重复的类别加序号后缀而不是丢骨：丢骨是丢内容
        （那一整棵原因子树），改名只动一个 40 字以内的词。
      - **节点坐标照留**（`keep_pos=True`）。`FishboneContent` 对 `pos` 没有任何
        约束（鱼的排布是几何定死的），所以留着不违反契约，而丢掉它就是往返之后
        少一个字段。
      - **空骨合法**。`bones: []` 是"只有主骨的草稿"，契约允许，这里也不补。
    """
    stats = survey_forest([bone.root for bone in raw.bones])
    first_bone_label = raw.bones[0].root.label if raw.bones else None
    title, title_clipped = _container_title(
        raw, one_line(raw.problem) or one_line(first_bone_label))
    if title_clipped:
        warnings.append(ImportWarning(
            code=TITLE_TRUNCATED, message="导图标题超过 200 字，已截断"))

    problem, problem_clipped = _clip_text(raw.problem, MAX_FISHBONE_PROBLEM_CHARS)
    if not problem:
        problem = title
        warnings.append(ImportWarning(
            code=FIELDS_DROPPED,
            message="鱼骨图的主骨上没有那句话（problem），已用导图标题代替",
        ))
    elif problem_clipped:
        warnings.append(ImportWarning(
            code=PROBLEM_TRUNCATED,
            message=f"主骨上的那句话超过 {MAX_FISHBONE_PROBLEM_CHARS} 字，已截到上限",
        ))

    tally = _Tally()
    builder = _Builder(tally, keep_pos=True)
    bones: list[FishboneBone] = []
    seeds: list[tuple[RawNode, Node, int]] = []
    used: set[str] = set()
    for item in raw.bones[:MAX_FISHBONE_BONES]:
        root = builder.container(item.root)
        category, clipped = _clip_text(item.category, MAX_FISHBONE_CATEGORY_CHARS)
        if not category:
            # 契约 `FishboneBone.category` 非空。空类别比"少了几个原因"更糟：一根
            # 没有名字的骨在画布上就是一条横线，用户看不出它本该是什么。
            category = _CATEGORY_PLACEHOLDER
            tally.missing_categories += 1
        elif clipped:
            tally.clipped_categories += 1
        if category in used:
            tally.duplicate_categories += 1
            base, serial = category, 2
            while category in used:
                category = clip_label(
                    f"{base}（{serial}）", MAX_FISHBONE_CATEGORY_CHARS)
                serial += 1
        used.add(category)
        bones.append(FishboneBone(
            category=category,
            root=root,
            color=normalize_color(item.color),
            icon=normalize_icon(item.icon),
        ))
        seeds.append((item.root, root, 1))
    tally.dropped_containers = max(0, len(raw.bones) - len(bones))
    builder.grow(seeds)

    warnings.extend(_limit_warnings(
        stats, tally,
        kept_note="每根骨都保留了内容，被裁掉的是各骨最深处与靠后的原因"))
    warnings.extend(_bone_warnings(tally, len(raw.bones)))
    refs_warning = _refs_warning(raw)
    if refs_warning is not None:
        warnings.append(refs_warning)

    relations, relations_dropped = _rebuild_relations(raw.relations, builder.id_map)
    if relations_dropped:
        warnings.append(ImportWarning(
            code=RELATIONS_DROPPED,
            message=(f"{relations_dropped} 条跨节点关系没有导入（端点找不到或类型不认识）；"
                     f"其余 {len(relations)} 条已保留"),
        ))
    return FishboneContent(
        title=title,
        problem=problem,
        bones=bones,
        theme=_theme(raw.theme),
        relations=relations,
    )


def _normalize_flow(raw: RawMap, warnings: list[ImportWarning]) -> FlowContent:
    """流程图：按顺序的阶段 + 阶段之间的跳转 + 各阶段内部的边。

    **两处 `edges` 分开重建**，这是这一支最容易写错的地方（契约 `FlowStage` 的
    docstring 把两套都说了一遍）：

      - `raw.edges`（阶段之间）的端点只在**阶段**命名空间里解析：`id_map` 传空，
        `stage_map` 传阶段 id 映射，`label_map` 只装阶段标题。三者里任何一个放错
        位置，一条边就会连到一个"碰巧同名"的步骤上——画出来完全正常，指向却是错的。
      - `stage.edges`（阶段内部）的端点必须在**这个阶段自己**的节点里。所以每个
        阶段拿到的是按 `owned` 过滤过的局部映射（见 `_Builder.grow`）。

    阶段 id 也重新生成：它虽是 id，但和节点 id 同属"外部文件给的东西"，而 `s_`
    前缀让两个命名空间在 JSON 里分得开。
    """
    if not raw.stages:
        raise ParseFailure("流程图至少需要一个阶段")

    stats = survey_forest([stage.root for stage in raw.stages])
    title, title_clipped = _container_title(raw, raw.stages[0].root.label)
    if title_clipped:
        warnings.append(ImportWarning(
            code=TITLE_TRUNCATED, message="导图标题超过 200 字，已截断"))

    tally = _Tally()
    builder = _Builder(tally, keep_pos=True)
    stage_id_map: dict[str, str] = {}
    stage_label_map: dict[str, str] = {}
    taken_stage_ids: set[str] = set()
    built: list[tuple[RawStage, str, Node]] = []
    seeds: list[tuple[RawNode, Node, int]] = []
    for item in raw.stages[:MAX_FLOW_STAGES]:
        root = builder.container(item.root)
        new_id = _new_stage_id(taken_stage_ids)
        taken_stage_ids.add(new_id)
        if item.old_id:
            stage_id_map.setdefault(item.old_id, new_id)
        for key in {one_line(item.root.label), root.label}:
            if key:
                stage_label_map.setdefault(key, new_id)
        built.append((item, new_id, root))
        seeds.append((item.root, root, 1))
    tally.dropped_containers = max(0, len(raw.stages) - len(built))
    owned = builder.grow(seeds)

    stages: list[FlowStage] = []
    inner_dropped = 0
    for (item, new_id, root), own in zip(built, owned):
        local_ids = {old: new for old, new in builder.id_map.items() if new in own}
        local_labels = {key: new for key, new in builder.label_map.items() if new in own}
        inner = _rebuild_edges(item.edges, local_ids, local_labels)
        inner_dropped += inner.dropped
        stages.append(FlowStage(id=new_id, root=root, edges=inner.edges))

    warnings.extend(_limit_warnings(
        stats, tally,
        kept_note="每个阶段都保留了内容，被裁掉的是各阶段最深处与靠后的步骤"))
    if tally.dropped_containers:
        warnings.append(ImportWarning(
            code=TRUNCATED_STAGES,
            message=(f"这份流程图有 {len(raw.stages)} 个阶段，超过上限 {MAX_FLOW_STAGES} 个；"
                     f"只导入了前 {MAX_FLOW_STAGES} 个，后面的阶段与它们的内容没有导入"),
        ))
    if inner_dropped:
        warnings.append(ImportWarning(
            code=EDGES_DROPPED,
            message=(f"{inner_dropped} 条阶段内部的附加边没有导入（端点不在本阶段里、"
                     "是自环或重复）；阶段之间的边不受影响"),
        ))
    refs_warning = _refs_warning(raw)
    if refs_warning is not None:
        warnings.append(refs_warning)

    outer = _rebuild_edges(raw.edges, {}, stage_label_map, stage_map=stage_id_map)
    relations, relations_dropped = _rebuild_relations(raw.relations, builder.id_map)
    warnings.extend(_edge_warnings(
        outer, relations, relations_dropped, what="阶段之间的跳转"))
    return FlowContent(
        title=title,
        stages=stages,
        edges=outer.edges,
        theme=_theme(raw.theme),
        relations=relations,
    )


def normalize(
    raw: RawMap, *, source_format: str,
) -> tuple[DiagramContent, list[ImportWarning]]:
    """`RawMap` → 三种图型之一 + 告警列表。

    这是唯一的入口：`service.parse_import` 只调它，六个解析器都碰不到契约。

    **形状由 `raw.shape` 决定，不由 `diagram_type` 决定。** 五个外部解析器从不设
    `shape`，它保持默认 `tree`——"外部格式没有图型概念，硬推断是编造"这句话就落实
    在那个默认值上（见 `docs/IMPORT.md` 的格式矩阵与"判据"一节）。

    `source_format` 目前不参与任何判断。保留这个参数是因为它是调用点唯一知道、而
    结果里必须出现的一件事（`ImportResult.source_format`），也是将来"按格式决定
    宽容度"唯一会读的东西。
    """
    warnings = _base_warnings(raw)
    if raw.shape == SHAPE_FISHBONE:
        return _normalize_fishbone(raw, warnings), warnings
    if raw.shape == SHAPE_FLOWCHART:
        return _normalize_flow(raw, warnings), warnings
    return _normalize_tree(raw, warnings), warnings
