"""导图 op 执行器（F18：让模型精确改图，而不是整棵重写）。

位置：agent 层。契约在 `app/contracts/ops.py`，前端同构实现是
`frontend/src/mindmap/ops.ts`，两边的执行语义必须逐条对应。

三条不可让步的约束：

  1. **整批原子**。所有 op 在深拷贝上按序执行，任一条抛 `OpError` 就丢弃副本。
     半批生效比整批失败更糟：树的 shape 会和模型以为的不一致，而模型下一轮
     会基于那个错误前提继续改，错一次会滚成一串。
  2. **顺序敏感**。后面的 op 可以引用前面 `add` 出来的 id——这正是模型把
     "先建父节点、再挂子节点"写成两条 op 的前提。
  3. **失败要可指认**。`OpError.path` 用 `ops.<下标>.<字段>`，这段文字会作为
     工具结果原样回给模型，它得知道是哪一条、哪个字段错了才能自己改对。
"""
from __future__ import annotations

from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from app.contracts.mindmap import (
    MAX_EDGES,
    Edge,
    FlowContent,
    FlowStage,
    MindMapContent,
    Node,
)
from app.contracts.ops import (
    AddEdgeOp,
    AddOp,
    MindMapOp,
    MoveOp,
    OpBatch,
    OpError,
    RemoveEdgeOp,
    RemoveOp,
    UpdateOp,
)
from collections.abc import Sequence
from typing import NamedTuple

# 草案里临时 id 的前缀。模型没法预知树里已有那些 id，自造名字只有两种结局：
# 撞上既有节点，或者在被要求"引用刚才那个新节点"时写了个它自己都不确定的名字。
TEMP_ID_PREFIX = "$"

# 会承载"节点 id"的四个字段。add 的 id、update/remove/move 的 id、
# move 的 new_parent_id，以及 add_edge 的两端。它们都可能是一个临时名。
#
# `id` 对 `add_edge` 是**边 id**（见 `_apply_add_edge`），但临时名的解析规则
# 完全一样：同一个 `$tmp1` 无论出现几次都指向同一个真实 id。
_ID_FIELDS = ("id", "parent_id", "new_parent_id", "source", "target")

# 承载"边 id"的 op。规范化时它们的 `id` 走 `new_edge_id` 而不是 `new_node_id`：
# 前缀不同不是为了好看，而是为了让日志与数据里一眼能分清"这是一条线的 id"，
# 排查"删错了线还是删错了节点"时省一次翻代码。
_EDGE_ID_OPS = ("add_edge",)

_OP_ADAPTER: TypeAdapter[MindMapOp] = TypeAdapter(MindMapOp)


def new_node_id(existing: set[str]) -> str:
    """生成一个不在 `existing` 里的节点 id。

    用完整 32 位 uuid4 hex。前端 `edits.ts` 里截成 8 位是够用的——前端只管
    显示与本次会话的局部编辑；服务端要落库、要参与之后所有 op 的引用解析，
    8 位 hex 在 1000 节点规模下碰撞概率虽低，但一旦撞上就是"引用了另一个节点"
    这种完全静默的错误，排查成本远高于多出的 26 个字符。
    """
    while True:
        candidate = f"n_{uuid4().hex}"
        if candidate not in existing:
            return candidate


def new_edge_id(existing: set[str]) -> str:
    """生成一个不在 `existing` 里的**边** id（`e_` 前缀，理由见 `_EDGE_ID_OPS`）。"""
    while True:
        candidate = f"e_{uuid4().hex}"
        if candidate not in existing:
            return candidate


def find_node(root: Node, node_id: str) -> Node | None:
    """按 id 找节点，找不到返回 None。

    语义对齐前端 `edits.ts::findNode`：**含根节点自身**。
    用显式栈而不是递归：深度上限只有 8，递归也安全，但 op 执行器要处理的
    是"模型给的、尚未校验的树"，不该依赖那个上限成立。
    """
    stack = [root]
    while stack:
        node = stack.pop()
        if node.id == node_id:
            return node
        stack.extend(node.children)
    return None


def find_parent(root: Node, node_id: str) -> Node | None:
    """按 id 找父节点；根节点没有父节点，返回 None。

    语义对齐前端 `edits.ts::findParent`。树不合法（id 重复）时返回其中任意一个
    父节点——执行器不负责修这种树，最后的整树校验会把它打回去。
    """
    if root.id == node_id:
        return None
    stack = [root]
    while stack:
        node = stack.pop()
        for child in node.children:
            if child.id == node_id:
                return node
        stack.extend(node.children)
    return None


def is_descendant(root: Node, ancestor_id: str, candidate_id: str) -> bool:
    """`candidate_id` 是不是 `ancestor_id` 的子孙（**含** `ancestor_id` 自己）。

    含自己是刻意的：move 的环检测既要拦"移到自己下面"，也要拦"移到自己的
    子孙下面"，后者在把 new_parent_id 与 ancestor 交换时会退化成前者，
    一个含自身的判据能同时覆盖，少一个分支就少一处能漏的地方。

    `ancestor_id` 不在树里时返回 False——那不是环，是"父节点不存在"，
    由调用方先报 `parent_not_found`。
    """
    ancestor = find_node(root, ancestor_id)
    if ancestor is None:
        return False
    stack = [ancestor]
    while stack:
        node = stack.pop()
        if node.id == candidate_id:
            return True
        stack.extend(node.children)
    return False


def apply_ops(content: MindMapContent, batch: OpBatch) -> MindMapContent:
    """按序执行整批 op，返回新的导图；任何一条失败整批作废。

    传入的 `content` 不会被修改，成功和失败都一样：调用方可能还拿着一份
    "执行前"的树另作他用（对比、回滚、写审计日志），执行器在它上面留下
    半截修改是绝对不能接受的。
    """
    working = content.model_copy(deep=True)
    for index, op in enumerate(batch.ops):
        base = f"ops.{index}"
        if isinstance(op, AddOp):
            _apply_add(working, op, base)
        elif isinstance(op, UpdateOp):
            _apply_update(working, op, base)
        elif isinstance(op, RemoveOp):
            _apply_remove(working, op, base)
        elif isinstance(op, MoveOp):
            _apply_move(working, op, base)
        elif isinstance(op, AddEdgeOp):
            _apply_add_edge(working, op, base)
        elif isinstance(op, RemoveEdgeOp):
            _apply_remove_edge(working, op, base)
        else:
            # 判别联合已经把类型定死在这里，走到这一支说明契约新增了 op
            # 而执行器忘了实现。宁可明确失败，也不要静默跳过一条 op。
            raise OpError("unknown_op", base, f"执行器不认识这种操作：{op!r}")

    return _validate_tree(working)


def normalize_ops(drafts: list[dict], existing: set[str]) -> tuple[list[MindMapOp], dict[str, str]]:
    """把草案里的 `$tmp1` 类临时 id 换成本次分配的真实 id。

    模型拿不到服务端分配的 id，也无法可靠地保证"我新起的名字"不撞上树里
    已有的节点。所以约定：草案里新节点用 `$` 开头的临时名，由这里统一分配，
    并**全局改写**引用它的 `id` / `parent_id` / `new_parent_id`——同一个
    `$tmp1` 无论出现几次都指向同一个真实 id，这就是"先建父、再挂子"能成立的原因。

    非 `$` 开头的 id 原样保留：那是模型指向既有节点的引用，改名就等于让它
    改错节点。

    返回 `(规范化后的 op 列表, {临时 id: 真实 id})`。映射要交给调用方，
    因为它还得回填到模型的可见状态里，否则模型下一轮会继续用旧名字。

    `existing` 不被修改：调用方的 id 集合是它自己那棵树的真相，
    执行器无权就地改它。
    """
    taken = set(existing)
    mapping: dict[str, str] = {}
    rewritten: list[dict] = []

    for draft in drafts:
        # 浅拷贝即可：只改顶层这几个标量字段，不碰嵌套结构，也不改调用方给的草案。
        item = dict(draft)
        # 边 op 的 `id` 是**边 id**：走 `e_` 前缀。这一步必须在字段循环之前
        # 决定，因为 `id` 在循环里被处理。
        edge_op = item.get("op") in _EDGE_ID_OPS
        for field in _ID_FIELDS:
            value = item.get(field)
            if not isinstance(value, str) or not value.startswith(TEMP_ID_PREFIX):
                continue
            if value not in mapping:
                if edge_op and field == "id":
                    real_id = new_edge_id(taken)
                else:
                    real_id = new_node_id(taken)
                taken.add(real_id)
                mapping[value] = real_id
            item[field] = mapping[value]
        rewritten.append(item)

    # 契约层的校验在这里收口：字段缺失、label 过长、额外字段一律由 pydantic 报错，
    # 执行器不重复实现一遍，也不把这些错误包装成 OpError——它们不是"某条 op 执行
    # 失败"，而是"这份草案根本不是合法的 op 列表"。
    return [_OP_ADAPTER.validate_python(item) for item in rewritten], mapping


def _validate_tree(content: MindMapContent) -> MindMapContent:
    """整树兜底校验：1000 节点 / 8 层 / 单节点 50 子 / id 唯一 / 引用合法。

    走 `model_dump()` 再 `model_validate()`，而不是直接 `model_validate(content)`：
    pydantic v2 默认 `revalidate_instances="never"`，传一个已构造好的实例会
    **原样返回**，`validate_tree` 一条都不会重跑——那样这层兜底等于不存在，
    而它恰恰是"整批 op 合起来把树搞坏了"的唯一防线。
    """
    try:
        validated = MindMapContent.model_validate(content.model_dump())
    except ValidationError as exc:
        details = "；".join(error["msg"] for error in exc.errors()[:3])
        raise OpError("tree_invalid", "ops", details or "执行这批 op 之后导图结构不合法") from exc

    # 契约层的 label 只约束 min_length=1，纯空白字符串（"   "）能过；前端的
    # assertTreeValid 会 trim 后拒绝。两端必须同一口径，否则同一条 op 在前端
    # 被拦下、在后端放行，模型收到的反馈取决于调用的是哪一端。
    stack = [validated.root]
    while stack:
        node = stack.pop()
        if not node.label.strip():
            raise OpError("tree_invalid", "ops", f"节点 {node.id} 的标题不能为空")
        stack.extend(node.children)
    return validated


def _apply_add(content: MindMapContent, op: AddOp, base: str) -> None:
    """挂一个新节点。`index == len(children)` 表示追加到末尾。"""
    parent = find_node(content.root, op.parent_id)
    if parent is None:
        raise OpError("parent_not_found", f"{base}.parent_id", f"父节点 {op.parent_id} 不存在")
    if find_node(content.root, op.id) is not None:
        raise OpError("duplicate_id", f"{base}.id", f"节点 id {op.id} 在树里已存在")
    if op.index > len(parent.children):
        raise OpError(
            "index_out_of_range", f"{base}.index",
            f"父节点 {op.parent_id} 现有 {len(parent.children)} 个子节点，index 最大为 "
            f"{len(parent.children)}，收到 {op.index}",
        )
    parent.children.insert(op.index, Node(
        id=op.id,
        label=op.label,
        children=[],
        # 复制一份而不是直接引用 op 里的 list：op 对象可能被调用方复用，
        # 树和 op 共享同一个可变列表迟早会出"改了 op 顺手改了树"的事。
        source_refs=list(op.source_refs),
        # `detail` 是生成产出的说明，与用户备注 `note` 分开存：这里只带 detail，
        # 永远不碰 note——重新生成一次就把用户敲的字盖掉是最不该发生的那种丢失。
        detail=op.detail,
        # `color` / `icon` 是可选的视觉元素，缺省 None。空串在这里归一成 None：
        # `add` 没有"清空"这个语义（新节点本来就没有颜色），把它传成 `""` 会在
        # 契约层被判非调色板键。
        color=op.color or None,
        icon=op.icon,
        # 走 op 落树的节点一律 `manual`：**op 是编辑轮的语言**（`add_node` /
        # `mindmap_edit` / 界面上的每一次结构改动），而"生成产出"从不经过 op
        # 的语义——生成链路在自己的出口统一刷 `generated`（`tools._stamp_origin`），
        # 包括那条确实借 `apply_ops` 落树的分片路（它刷在 `apply_ops` 之后）。
        #
        # 不从 op 里读：`origin` 是自证，能被 op 写就等于能被伪造溯源（契约里
        # `AddOp` 刻意没有这个字段，`UpdateOp` 更没有）。
        origin="manual",
    ))


def _apply_update(content: MindMapContent, op: UpdateOp, base: str) -> None:
    """改节点字段。None = 这一项不动，[] = 显式清空。"""
    node = find_node(content.root, op.id)
    if node is None:
        raise OpError("node_not_found", f"{base}.id", f"节点 {op.id} 不存在")
    if op.changes.label is not None:
        node.label = op.changes.label
    # 用 `is not None` 而不是真值判断：写成 `if op.changes.source_refs:` 的话，
    # 空列表会被当成"没给"，模型就永远清不掉一条错误引用——它只会反复发同一条
    # update，然后每次都"成功"但什么都没变。
    if op.changes.source_refs is not None:
        node.source_refs = list(op.changes.source_refs)
    # `detail` 同一个约定：`None` 不动，空串归一成 `None`（"清空"）。
    # 这里**不碰 note**：那是用户的备注，生成侧无权重写（见 `Node` 的注释）。
    if op.changes.detail is not None:
        detail = op.changes.detail.strip()
        node.detail = detail or None
    # 视觉元素同一条约定：`None` 不动；空串是**显式清除**。
    # `color` 在契约层已经归一成调色板键或空串（`ops._palette_color`），这里只把
    # 空串收成 `None`——不这么做，模型摘掉一个颜色之后树里会留一个 `""`，
    # 前端读到它就会去查一个不存在的 CSS 变量（画出来是透明/继承色）。
    if op.changes.color is not None:
        node.color = op.changes.color or None
    if op.changes.icon is not None:
        node.icon = op.changes.icon or None


def _apply_remove(content: MindMapContent, op: RemoveOp, base: str) -> None:
    """删节点连整棵子树。根节点不可删——删了就没有导图了。

    **连带删掉与这棵子树相关的附加边与跨节点关系**（源或目标命中被删的任意一个
    节点）。不删的话，库里会留下指向已消失节点的边/关系：契约层下一次校验会报
    `边 X 的起点节点 Y 不在导图中`，于是"删一个节点"变成"整张图存不回去"，
    而报错指的位置（某条边）与用户做的事（删节点）之间隔了很远。

    `relations` 与 `edges` 是**两套字段、同一条判据**：契约 `_validate_relations` 与
    `_validate_edges` 对 dangling 一样是硬拒。只清 `edges` 时，删一个被 `relations`
    引用的节点会让整批 op 在最后那次整树校验里被拒，工具层报成 `tree_invalid`
    ——模型只收到"改动之后整张图不合法"，指不回"我刚才删的那个节点"。

    命中范围是**整棵子树**而不是只有 `op.id` 一个节点：级联删除会把子孙一起
    摘掉，只清理被点名那一个，子孙身上的边同样会悬空。
    """
    if op.id == content.root.id:
        raise OpError("cannot_remove_root", f"{base}.id", "根节点不能删除")
    parent = find_parent(content.root, op.id)
    if parent is None:
        raise OpError("node_not_found", f"{base}.id", f"节点 {op.id} 不存在")
    doomed = _subtree_ids(find_node(content.root, op.id))
    # 摘掉这一个引用，它下面的整棵子树随之从树上消失，不需要逐个清理。
    parent.children = [child for child in parent.children if child.id != op.id]
    content.edges = [edge for edge in content.edges
                     if edge.source not in doomed and edge.target not in doomed]
    content.relations = [relation for relation in content.relations
                         if relation.source not in doomed and relation.target not in doomed]


def _subtree_ids(node: Node | None) -> set[str]:
    """节点及其全部子孙的 id（`node` 为 None 时返回空集）。"""
    if node is None:
        return set()
    ids: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        ids.add(current.id)
        stack.extend(current.children)
    return ids


def _apply_move(content: MindMapContent, op: MoveOp, base: str) -> None:
    """换父节点（连同子树）。**先移除再插入**的语义，索引上界因此按移除后算。

    判定顺序与 `frontend/src/mindmap/ops.ts` 逐条对齐：先查存在性、再判环。
    顺序不能反——`move` 一个不存在的节点到自己身上，该报的错是
    `node_not_found`（"你指的节点就没有"），而不是 `cycle_detected`。两端若
    在这里分叉，同一条草案在前端编辑器里和后端执行器里会得到两种反馈。
    """
    node = find_node(content.root, op.id)
    if node is None:
        raise OpError("node_not_found", f"{base}.id", f"节点 {op.id} 不存在")
    target = find_node(content.root, op.new_parent_id)
    if target is None:
        raise OpError("parent_not_found", f"{base}.new_parent_id",
                      f"新的父节点 {op.new_parent_id} 不存在")
    # is_descendant 含自己，所以"挂到自己"和"挂到自己的子孙"由同一句拦住。
    # 根节点也由它兜住：根的任何一个候选父节点要么是它自己、要么是它的子孙，
    # 因此"移动根节点"落到这里报 cycle_detected——与前端给的是同一个码。
    if is_descendant(content.root, op.id, op.new_parent_id):
        raise OpError("cycle_detected", f"{base}.new_parent_id",
                      f"节点 {op.id} 不能挂到 {op.new_parent_id} 下：移动会形成环")

    parent = find_parent(content.root, op.id)
    if parent is None:  # 根节点已经被上面的环检测挡住了，走到这里说明树本身不合法
        raise OpError("node_not_found", f"{base}.id", f"节点 {op.id} 在树里找不到父节点")
    parent.children = [child for child in parent.children if child.id != op.id]

    # 先移除再插入：同父内移动时 `target.children` 此刻已经少了一个元素，
    # 所以 index 的合法上界天然就是"减 1 之后"的长度，不必为同父/跨父
    # 写两套边界。副作用是"同父内 index=末位后一位"会被判越界——这正是
    # 前端 ops.ts 用同一套语义得到的结果，两端不能在这里分叉。
    if op.index > len(target.children):
        raise OpError(
            "index_out_of_range", f"{base}.index",
            f"新父节点 {op.new_parent_id} 移除后现有 {len(target.children)} 个子节点，"
            f"index 最大为 {len(target.children)}，收到 {op.index}",
        )
    target.children.insert(op.index, node)


def _apply_add_edge(content: MindMapContent, op: AddEdgeOp, base: str) -> None:
    """挂一条附加边。四个判据，全部在写之前。

    为什么这里要自己判一遍（契约层的 `validate_tree` 也会判）：执行器在**批次
    中途**跑，一条非法边不该等到整批跑完才由整树校验打回——那时错误只剩一句
    "边 X 的起点节点 Y 不在导图中"，`path` 指不到 `ops.3.target`，模型下一轮
    还得猜是哪条。这里判一次，错误码与字段路径都是准的。

    与契约层的一处**不同**（且是刻意的）：重复边在这里报 `duplicate_edge`
    而不是静默去重。契约层的去重服务于"库里的历史数据/别人构造的 payload"，
    执行器服务于"模型刚发来的这一批"——同一个批次里连发两条一模一样的边，
    模型需要知道其中一条没生效，否则它会以为"我加了两条"。
    """
    source = find_node(content.root, op.source)
    if source is None:
        raise OpError("node_not_found", f"{base}.source", f"边的起点节点 {op.source} 不存在")
    target = find_node(content.root, op.target)
    if target is None:
        raise OpError("node_not_found", f"{base}.target", f"边的终点节点 {op.target} 不存在")
    if op.source == op.target:
        raise OpError(
            "self_loop", f"{base}.target",
            f"{op.source} 不能连到自己：自环在画布上只会渲染成一个点",
        )
    if any(edge.id == op.id for edge in content.edges):
        raise OpError("duplicate_edge", f"{base}.id", f"边 id {op.id} 已经存在")
    if any(edge.source == op.source and edge.target == op.target and edge.label == op.label
           for edge in content.edges):
        same = f"（标签「{op.label}」）" if op.label else ""
        raise OpError(
            "duplicate_edge", f"{base}.target",
            f"{op.source} → {op.target}{same} 这条边已经存在",
        )
    if len(content.edges) >= MAX_EDGES:
        raise OpError(
            "too_many_edges", base,
            f"这张图已经有 {len(content.edges)} 条附加边，达到上限 {MAX_EDGES} 条，"
            "加不了新边",
        )
    content.edges.append(Edge(
        id=op.id,
        source=op.source,
        target=op.target,
        label=op.label,
        kind=op.kind,
    ))


def _apply_remove_edge(content: MindMapContent, op: RemoveEdgeOp, base: str) -> None:
    """删一条附加边。**不动节点**：边不是结构，删它之后树一个字节都不变。"""
    if not any(edge.id == op.id for edge in content.edges):
        raise OpError("edge_not_found", f"{base}.id", f"边 {op.id} 不存在")
    content.edges = [edge for edge in content.edges if edge.id != op.id]


# ---------------------------------------------------------------- 流程图：两层边
#
# `FlowContent` 上有**两处** `edges`、端点属于**两个互不相通的命名空间**
# （契约 `FlowStage` 的 docstring 是这条边的权威定义）：
#
#   - `FlowContent.edges`：**阶段之间**的跳转，端点必须是阶段 id（`s_…`）；
#   - `FlowStage.edges`：**阶段内部**的边（判定节点的「是 / 否」、步骤之间的顺序
#     跳转），端点必须是**该阶段自己的节点 id**。
#
# 所以一条 `add_edge` / `remove_edge` 在流程图上要**先判它属于哪一层**再落笔。
# 此前这里明确拒绝（`edges_not_supported`）：那是个"实现会静默丢数据"的判断——
# 借道树的执行器时边落在一棵临时 `MindMapContent` 里，而写回只换子树根，工具回一句
# "已连接"而 `ctx.candidate` 里一条边都没有。现在判定这一层真的存在了，拒绝可以撤。
#
# 判据与 `shapes._split_edges`（树 → 流程的迁移分流）**必须同源**，否则同一对端点在
# 生成路径与编辑路径上会落进不同的数组；所以这里用同一套"端点归属"规则：
#
#   1. 两个端点都是阶段 id            → 阶段之间（原生跳转，不降级）
#   2. 两个端点属于**同一个阶段**      → 那个阶段的内部
#   3. 两个端点属于**不同阶段**        → 阶段之间，端点换成各自的阶段 id，记 `demoted`
#      （精确端点保不住，但方向还在——与 `_split_edges` 的 `demoted` 同一个取舍）
#   4. 一个是阶段 id、另一个是节点 id  → 拒（两套命名空间混用，无法判断意图）
#   5. 端点根本不存在                  → 拒（`node_not_found` / `edge_not_found`）

STAGE_LAYER = "stage"
INNER_LAYER = "inner"


class FlowEdgePlacement(NamedTuple):
    """一条边在 `FlowContent` 里该落到哪一层、落笔时的端点是什么。

    `layer` / `source` / `target` 是**要写进数组的那三个值**；`stage_id` 只有
    `inner` 时才有意义（那个阶段的 id）；`demoted` 说明这条边本来指的是两个**深层
    步骤**，被降级成了阶段之间的连线——调用方据此在摘要里说清楚，别让模型以为
    精确端点被保留了。
    """

    layer: str
    source: str
    target: str
    stage_id: str | None
    demoted: bool


def _stage_owner_of(content: FlowContent) -> dict[str, str]:
    """节点 id → 它所属阶段的 id。阶段之间的端点用不上它，阶段内边全靠它。"""
    owner: dict[str, str] = {}
    stack: list[Node] = []
    for stage in content.stages:
        stack.append(stage.root)
        while stack:
            node = stack.pop()
            owner[node.id] = stage.id
            stack.extend(node.children)
    return owner


def resolve_flow_edge_placement(
    content: FlowContent, source: str, target: str, *, base: str = "edge",
) -> FlowEdgePlacement:
    """判一条 `(source, target)` 属于哪一层，返回落笔时该用的端点。

    纯函数、不改内容：工具那层的预检（要报"起点不存在"这种精确到参数的错）与
    执行器（要报 `ops.<下标>.target`）共用它，两处各写一份判据只会有一天互相矛盾。
    """
    stage_ids = {stage.id for stage in content.stages}
    owner = _stage_owner_of(content)

    source_is_stage = source in stage_ids
    target_is_stage = target in stage_ids
    source_stage = owner.get(source)
    target_stage = owner.get(target)

    if source_is_stage and target_is_stage:
        return FlowEdgePlacement(STAGE_LAYER, source, target, None, False)

    if source_is_stage != target_is_stage:
        stage_side = source if source_is_stage else target
        node_side = target if source_is_stage else source
        raise OpError(
            "mixed_edge_namespace", f"{base}.target",
            f"一条边的两个端点必须同属一个命名空间：阶段之间用阶段 id（如 {stage_side}），"
            f"阶段内部用该阶段的节点 id（如 {node_side}）。收到 {source} → {target}，"
            "两套混用无法判断这条线画在哪一层；要连阶段就用两个阶段 id，"
            "要连判定出口就用同一阶段里的两个节点 id。",
        )

    if source_stage is None:
        raise OpError("node_not_found", f"{base}.source", f"边的起点 {source} 不在任何阶段里")
    if target_stage is None:
        raise OpError("node_not_found", f"{base}.target", f"边的终点 {target} 不在任何阶段里")

    if source_stage == target_stage:
        return FlowEdgePlacement(INNER_LAYER, source, target, source_stage, False)

    return FlowEdgePlacement(STAGE_LAYER, source_stage, target_stage, None, True)


def _flow_stage(content: FlowContent, stage_id: str) -> FlowStage:
    """按 id 取阶段。找不到是**内部错误**（`resolve_flow_edge_placement` 已经保证它存在）。"""
    for stage in content.stages:
        if stage.id == stage_id:
            return stage
    raise OpError("node_not_found", "edge", f"阶段 {stage_id} 不在这份流程图里")


def _all_flow_edge_ids(content: FlowContent) -> set[str]:
    """两处数组的全部边 id。**跨数组唯一**是契约的硬约束（`validate_flow` 查它）：
    前端按 id 建索引，重名的两条边里有一条会永远画不出来。"""
    ids = {edge.id for edge in content.edges}
    for stage in content.stages:
        ids.update(edge.id for edge in stage.edges)
    return ids


def _apply_add_flow_edge(content: FlowContent, op: AddEdgeOp, base: str) -> None:
    """把一条边挂到它该在的那一层。判据全部在写之前，与树上那版逐条对应。"""
    placement = resolve_flow_edge_placement(content, op.source, op.target, base=base)
    if op.source == op.target or placement.source == placement.target:
        # 两个不同阶段的深层步骤被降级之后可能撞到同一个阶段（同一个阶段内部，
        # 那条边落进 inner 层，端点就是原来那两个，不会相等）。走到这里说明源与目标
        # 归一之后重合了——画出来是一个点。
        raise OpError(
            "self_loop", f"{base}.target",
            f"{placement.source} 不能连到自己：自环在画布上只会渲染成一个点",
        )
    if op.id in _all_flow_edge_ids(content):
        raise OpError("duplicate_edge", f"{base}.id", f"边 id {op.id} 已经存在")

    sibling: list[Edge]
    where: str
    stage: FlowStage | None = None
    if placement.layer == INNER_LAYER:
        stage = _flow_stage(content, placement.stage_id or "")
        sibling = stage.edges
        where = f"阶段 {stage.id} 内部"
        allowed = {node.id for node in _walk(stage.root)}
        for endpoint in (placement.source, placement.target):
            if endpoint not in allowed:
                raise OpError(
                    "node_not_found", f"{base}.source",
                    f"边的端点 {endpoint} 不在{where}：阶段内边的两个端点必须属于同一个阶段",
                )
    else:
        sibling = content.edges
        where = "阶段之间"

    if any(edge.source == placement.source and edge.target == placement.target
           and edge.label == op.label for edge in sibling):
        same = f"（标签「{op.label}」）" if op.label else ""
        raise OpError(
            "duplicate_edge", f"{base}.target",
            f"{placement.source} → {placement.target}{same} 这条边已经存在（{where}）",
        )
    # 上限**两层各算**（不是合计）：判据与契约一致——`_validate_edges` 是按数组各自
    # 调用的，`edge_diagnostics` 也是按各自的清单报 `EDGE_LIMIT`。合计会让一条吵闹的
    # 阶段先把阶段之间的额度吃光，而两者在画布上的规模本来就不成比例。
    if len(sibling) >= MAX_EDGES:
        raise OpError(
            "too_many_edges", base,
            f"{where}已经有 {len(sibling)} 条附加边，达到上限 {MAX_EDGES} 条，加不了新边",
        )

    edge = Edge(id=op.id, source=placement.source, target=placement.target,
                label=op.label, kind=op.kind)
    if stage is not None:
        stage.edges.append(edge)
    else:
        content.edges.append(edge)


def _apply_remove_flow_edge(content: FlowContent, op: RemoveEdgeOp, base: str) -> None:
    """删一条流程图的边。**按 id 查两个数组**，删线不动结构。

    只查 `content.edges` 的话，阶段内的边列得出来却删不掉——而"删不掉"的表现是
    工具回一句"边不存在"，模型下一轮继续试同一条。
    """
    for stage in content.stages:
        if any(edge.id == op.id for edge in stage.edges):
            stage.edges = [edge for edge in stage.edges if edge.id != op.id]
            return
    if any(edge.id == op.id for edge in content.edges):
        content.edges = [edge for edge in content.edges if edge.id != op.id]
        return
    raise OpError("edge_not_found", f"{base}.id", f"边 {op.id} 不存在")


def _walk(root: Node):
    """前序遍历一棵子树。这里只要"这个阶段里有哪些节点 id"。"""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.children)


def validate_flow(content: FlowContent) -> FlowContent:
    """整图兜底校验（与 `_validate_tree` 同一个理由、同一套走法）。

    `model_copy` / 直接 append 都不经 pydantic 校验，而这层是"整批 op 合起来把图
    搞坏了"的唯一防线：dangling、跨数组的边 id 重名、阶段内边连到别的阶段，全部
    由契约的 `validate_flow` 在这里收口，错误码统一是 `tree_invalid`。
    """
    try:
        return FlowContent.model_validate(content.model_dump())
    except ValidationError as exc:
        details = "；".join(error["msg"] for error in exc.errors()[:3])
        raise OpError("tree_invalid", "ops", details or "执行这批 op 之后流程图不合法") from exc


def apply_flow_edge_ops(
    content: FlowContent, items: Sequence[tuple[str, MindMapOp]],
) -> FlowContent:
    """把一批**只含边 op** 的批次落到流程图的正确那一层上，整批原子。

    `items` 是 `(字段路径, op)`：路径由调用方从**原始批次的下标**算出来
    （`ops.3`），所以在拆分批次之后 `OpError.path` 仍然指得回模型发的那一条——
    重排下标会让报错指向另一条 op，那比不报错更糟。
    """
    working = content.model_copy(deep=True)
    for base, op in items:
        if isinstance(op, AddEdgeOp):
            _apply_add_flow_edge(working, op, base)
        elif isinstance(op, RemoveEdgeOp):
            _apply_remove_flow_edge(working, op, base)
        else:
            raise OpError("unknown_op", base, f"边批次里出现了非边 op：{op.op!r}")
    return validate_flow(working)
