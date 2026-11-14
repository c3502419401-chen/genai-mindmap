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

from app.contracts.mindmap import MindMapContent, Node
from app.contracts.ops import (
    AddOp,
    MindMapOp,
    MoveOp,
    OpBatch,
    OpError,
    RemoveOp,
    UpdateOp,
)

# 草案里临时 id 的前缀。模型没法预知树里已有那些 id，自造名字只有两种结局：
# 撞上既有节点，或者在被要求"引用刚才那个新节点"时写了个它自己都不确定的名字。
TEMP_ID_PREFIX = "$"

# 会承载"节点 id"的三个字段。add 的 id、update/remove/move 的 id、
# move 的 new_parent_id 都可能是一个临时名。
_ID_FIELDS = ("id", "parent_id", "new_parent_id")

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
        # 浅拷贝即可：只改顶层这三个标量字段，不碰嵌套结构，也不改调用方给的草案。
        item = dict(draft)
        for field in _ID_FIELDS:
            value = item.get(field)
            if not isinstance(value, str) or not value.startswith(TEMP_ID_PREFIX):
                continue
            if value not in mapping:
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


def _apply_remove(content: MindMapContent, op: RemoveOp, base: str) -> None:
    """删节点连整棵子树。根节点不可删——删了就没有导图了。"""
    if op.id == content.root.id:
        raise OpError("cannot_remove_root", f"{base}.id", "根节点不能删除")
    parent = find_parent(content.root, op.id)
    if parent is None:
        raise OpError("node_not_found", f"{base}.id", f"节点 {op.id} 不存在")
    # 摘掉这一个引用，它下面的整棵子树随之从树上消失，不需要逐个清理。
    parent.children = [child for child in parent.children if child.id != op.id]


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
