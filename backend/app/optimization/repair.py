"""自动修复：**只做"修法唯一"的那几条规则**，其余继续只给建议。

======================================================================
为什么只有三条
======================================================================
`optimization/service.py::check()` 有七种结构码，能不能自动修取决于**"修法是否唯一"**：

    DUPLICATE_LABEL      合并两个同级同名节点（保留子树并集）  唯一  → 自动修
    SINGLE_CHILD         把单子链压成一层                       唯一  → 自动修
    HEADING_ONLY_BRANCH  把"只是个标题"的一级分支并进它的唯一子节点  唯一  → 自动修
    NEAR_DUPLICATE_LABEL 「是不是同一件事」要人判               不唯一 → 只给建议
    SHALLOW_BRANCH       补什么内容要模型                      不唯一 → 只给建议
    MISSING_DETAIL       写什么说明要模型                      不唯一 → 只给建议
    DEPTH_LIMIT          没有可执行的动作（见 service.NOT_SUGGESTED）  不给

"唯一"在这里是可检验的意思，不是形容词：**结果与"分段跑还是一趟跑"无关**。合并永远
并进该父下**第一个**出现的那个；压缩永远把外层并进内层；并且**合并段永远在压链段
之前**（反过来的顺序会让压链先改掉 label，两个同名兄弟就再也不是同名了）。
`tests/test_optimization_repair.py::test_order_of_repairs_does_not_change_the_result`
把这条顺序钉住。

======================================================================
三条硬约束
======================================================================
1. **幂等**：`repair(repair(x))` 的 `applied` 为空，且两棵树逐字节相同。靠两件事：
   每轮重新 `check()`（旧诊断会过期），以及**预算按节点数算**——每次修复严格让节点
   少一个，所以"最多修 n 步"既是终止证明也是幂等的保证。
   （真图上量到过反例：561 节点的图有 200+ 处同级重名，固定 200 步的预算会让第二趟
   继续修，于是同一个输入两次调用给出两棵不同的树。见 `scripts/measure_repair.py`。）

2. **引用并集**：合并/压缩时两边的 `source_refs` 取并集（保序、按五元组去重），
   **不是覆盖**。并集超过契约上限（20 条）时**放弃这条修复**并记进 `skipped`——
   截断是丢证据，而丢证据比不修更糟。`detail` / `note` 同理（拼接，超 2000 字放弃）。

3. **可撤销**：每条修复带一条 `RepairUndo`，`revert()` 按逆序应用回去，
   `revert(repair(x))` 与 `x` 逐字段相同（用例 + 真图脚本都核过）。**不依赖前端的
   撤销栈**：撤销栈只在一个会话里有效，而这个函数是后端的纯函数，谁调用它都得能还原。

   undo 存的是**字段快照**，不是整棵子树（`NodeFields`）。一开始存了整棵子树，真图上
   一量就爆了：561 节点那张图的 200 步合并攒出 **3.6 MB** 的 undo，端点响应塞不下。
   现在每一步只记"被替换的那个节点的字段 + 它原来有几个孩子"，子树靠"摘尾巴"还原
   （合并后 kept 的孩子是 `原孩子 + 被并进来的孩子`，尾巴就是后者，一个计数就够）。

   为什么不用现有的 op 机制（`agent/ops.py` 的 `OpBatch`）：op 是**增量编辑**的通道
   （模型改图），它没有"把两个节点的子树并起来"这种复合操作，也不记录被删节点的原
   字段——而撤销要的恰恰是那些字段。造一个 `merge` op 等于给那条通道加一种只能撤销、
   不能重放的操作，两边都不合适。

======================================================================
跨节点关系与附加边：**重定向，不删除**
======================================================================
合并让一个节点 id 消失（被并掉的那个）。指向它的 `relations` / `edges` 若照原样留着，
画布上会出现一条指向不存在节点的线（`MindRelation` 拿不到端点位置）。三种处理里只有
一种不丢信息：

    删掉那条线        丢用户的表达
    留着不管          画出一条悬空线
    把端点改到保留者    线的语义不变（"A 与「重复的那个」有关" → "A 与「留下的那个」有关"）

所以这里做重定向，并把每一次改写记进 `RepairUndo.redirected`，撤销时改回来。

======================================================================
不碰的东西
======================================================================
- **层级不动**：不自动增删层级，只压缩"一层只有一个孩子"这种没有信息量的层。
- **模型不调**：本文件一次网络调用都没有（与 `optimization/nli.py` 的"关闭即零开销"
  同一条规矩）。
- **check 的判据不复制**：候选来自 `check()` 自己的诊断，所以"修完那条码消失"是
  结构上成立的，不是两份判定碰巧一致。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Iterator, Literal, Sequence

from pydantic import BaseModel, ConfigDict

from app.contracts.diagnostic import Diagnostic
from app.contracts.mindmap import (
    MAX_NODE_CHILDREN,
    MAX_NODE_DETAIL_CHARS,
    MAX_NODE_LABEL_CHARS,
    MindMapContent,
    Node,
)
from app.optimization.service import check

# ---------------------------------------------------------------- 可控修的码

#: 能自动修的三个码。与 `service._SUGGESTION_OF_CODE` 是**不同的表**：那张表管
#: "诊断转成什么建议"，这张管"哪条能自动落地"。两条规则同一个码、两种处置的局面
#: （NEAR_DUPLICATE 是 dedupe 建议但不能自动修）必须能被读出来，所以不复用。
AUTO_REPAIRABLE_CODES = ("DUPLICATE_LABEL", "SINGLE_CHILD", "HEADING_ONLY_BRANCH")

#: 不自动修的码 → 理由。与 `service.NOT_SUGGESTED` 同一条规矩：新增一个码时必须
#: 在这里（或上面）做一个决定，否则用例会红。
MANUAL_ONLY: dict[str, str] = {
    "NEAR_DUPLICATE_LABEL": "「是不是同一件事」要人判：字面像不等于语义同（实测 40 对里 0 对真近义）",
    "SHALLOW_BRANCH": "补什么内容要模型：这一支缺的是信息，不是排版",
    "MISSING_DETAIL": "写什么说明要模型",
    "DEPTH_LIMIT": "没有可执行的动作：契约禁止第 9 层（见 service.NOT_SUGGESTED）",
}

#: 契约里 `Node.source_refs` 的 `max_length`。写在这里是因为契约把它写成字面量，
#: 没有导出常量；改契约时这里要跟着改（`test_source_ref_limit_matches_contract`
#: 用反射读回契约，分叉会红）。
MAX_SOURCE_REFS = 20

#: 压缩时两层标题的连接符。为什么是 ` / ` 而不是 `：` 或换行：`/` 在中文标题里
#: 已经读作"层级"（「机器学习 / 监督学习」），而且它不改变任何一层的字面，用户
#: 撤销前后看到的是同一批名词。
LABEL_JOINER = " / "


class RepairKind(str, Enum):
    """修复动作的种类。给界面显示用，与诊断码分开（一次合并可能对应两个码）。"""

    merge_duplicate = "merge_duplicate"      # DUPLICATE_LABEL
    collapse_chain = "collapse_chain"        # SINGLE_CHILD / HEADING_ONLY_BRANCH


# ---------------------------------------------------------------- 撤销数据


class NodeFields(BaseModel):
    """一个节点的**全部字段快照，不含 `children`**。

    撤销要还原的是"这个槽位上原来是什么"，而子树的形状可以靠"槽位 + 摘尾巴"推回来
    （见模块 docstring 第 3 条）。存整棵子树在真图上会爆（561 节点那张图 3.6 MB），
    存字段是几百字节。

    用 `dict` 而不是逐字段声明，是为了**不抄一遍 `Node`**：抄一遍的话，`Node` 以后加
    字段（`role`、`pos` 都是后加的），这里就静默丢一个——而丢的那个字段恰恰是撤销
    之后用户会发现"形状变了"的地方。`model_dump(exclude={'children'})` 永远是全的。
    """

    model_config = ConfigDict(extra="forbid")
    data: dict[str, Any]

    @classmethod
    def of(cls, node: Node) -> "NodeFields":
        return cls(data=node.model_dump(mode="python", exclude={"children"}))

    def build(self, children: list[Node]) -> Node:
        """按快照重建节点，`children` 由调用方给（那是被搬动过的那部分）。"""
        return Node.model_validate({**self.data, "children": children})


class RepairInsert(BaseModel):
    """修复时被并掉、撤销时要插回原位的那个节点。"""

    model_config = ConfigDict(extra="forbid")
    index: int
    fields: NodeFields
    #: 这个节点原来有几个孩子。它们现在是 kept 的**尾部**，撤销时按这个数摘回来。
    children: int


class RepairRedirect(BaseModel):
    """关系线端点的改写记录（`before` ← `after` 的反向）。"""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["relation", "edge"]
    id: str
    field: Literal["source", "target"]
    before: str
    after: str


class RepairUndo(BaseModel):
    """一条修复的反操作。粒度是"**被改动的那一个槽位**"。

    `parent_id = None` 表示改动落在**根槽位**上（根自己只有一个孩子时，把唯一的孩子
    并进根：根不动、孩子消失）。

    两种修复用同一套字段，靠 `inner` 区分：
      - 合并（`inner=None`）：`replaced` 是保留者的原字段、`replaced_children` 是它
        原来的孩子数，`inserted` 是被并掉的那个；
      - 压链（`inner` 有值）：`replaced` 是外层的原字段，`inner` 是内层的原字段，
        `replaced_children` 恒为 1；撤销时把两者重新拼成两层。
    """

    model_config = ConfigDict(extra="forbid")
    parent_id: str | None
    at: int
    replaced: NodeFields
    replaced_children: int = 0
    inserted: list[RepairInsert] = []
    inner: NodeFields | None = None
    redirected: list[RepairRedirect] = []


class RepairItem(BaseModel):
    """一次真正落地的修复。`message` 是给用户看的一句话（说清改了什么）。"""

    model_config = ConfigDict(extra="forbid")
    code: str
    kind: RepairKind
    node_id: str
    message: str
    undo: RepairUndo


class RepairSkip(BaseModel):
    """看到了、但不能修的那一条。**必须报出来**：静默跳过会让"点了一键修复却没变"
    变成一个查不出来的状态。"""

    model_config = ConfigDict(extra="forbid")
    code: str
    node_id: str | None
    reason: str


class RepairResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: MindMapContent
    applied: list[RepairItem] = []
    skipped: list[RepairSkip] = []
    #: 修完之后重跑的 `check()`。调用方据此说"还剩几条建议"。
    remaining: list[Diagnostic] = []
    #: 实际做了几步（= `len(applied)`；单独留一个字段是为了让日志读起来直接）。
    steps: int = 0


# ---------------------------------------------------------------- 树的小工具


def _iter_nodes(root: Node) -> Iterator[tuple[Node, Node | None, int]]:
    """`(节点, 父节点, 在父 children 里的下标)`。根的下标恒为 0、父恒为 None。"""
    stack: list[tuple[Node, Node | None, int]] = [(root, None, 0)]
    while stack:
        node, parent, index = stack.pop()
        yield node, parent, index
        for position, child in enumerate(node.children):
            stack.append((child, node, position))


def _index(content: MindMapContent) -> tuple[dict[str, Node], dict[str, str | None]]:
    nodes: dict[str, Node] = {}
    parents: dict[str, str | None] = {}
    for node, parent, _ in _iter_nodes(content.root):
        nodes[node.id] = node
        parents[node.id] = parent.id if parent is not None else None
    return nodes, parents


def _count_nodes(root: Node) -> int:
    return sum(1 for _ in _iter_nodes(root))


def _ref_key(ref) -> tuple:
    return (ref.source_id, ref.page, ref.start, ref.end, ref.quote)


def _union_refs(left, right) -> list | None:
    """引用并集，保序去重。超过契约上限返回 `None`（调用方据此放弃这条修复）。"""
    merged: list = []
    seen: set[tuple] = set()
    for ref in list(left) + list(right):
        key = _ref_key(ref)
        if key in seen:
            continue
        seen.add(key)
        merged.append(ref)
    if len(merged) > MAX_SOURCE_REFS:
        return None
    return merged


def _join_text(outer: str | None, inner: str | None) -> str | None:
    """两段正文合并。相同取一、一空取另一、都非空且不同则拼接。

    为什么拼接而不是"取长的那个"：`detail` 是模型写的说明、`note` 是用户写的备注，
    两者都承载信息。取长的那个在"短的那段说了另一件事"时是静默丢内容，而用户看不见
    自己丢了什么。超过契约上限时返回 `None`，调用方放弃修复。
    """
    left = (outer or "").strip()
    right = (inner or "").strip()
    if not left:
        return right or None
    if not right or left == right:
        return left
    joined = f"{left}\n\n{right}"
    if len(joined) > MAX_NODE_DETAIL_CHARS:
        return None
    return joined


def _joined_label(outer: str, inner: str) -> str | None:
    """压缩两层标题。超长返回 `None`。

    包含关系时不重复拼：`outer in inner` 时只留 `inner`（「监督学习」/「有监督学习」
    这类父子标题在实际图里很常见，拼起来是「监督学习 / 有监督学习」——读者会以为
    这是两个不同的层级，而它们本来就是一层）。
    """
    left, right = outer.strip(), inner.strip()
    if not left:
        return right or None
    if not right:
        return left
    if left in right:
        return right
    if right in left:
        return left
    joined = f"{left}{LABEL_JOINER}{right}"
    return joined if len(joined) <= MAX_NODE_LABEL_CHARS else None


_EXPANDABLE_RANK = {"no": 0, "unknown": 1, "yes": 2}


def _wider_expandable(left: str, right: str) -> str:
    """两个节点的"还能不能展开"合并成一个：**取更可能展开的那个**。

    合并之后子树是两者之和，只要有一边还能展开，合并结果就还能展开。反过来取
    `no` 会让界面对一支其实没长完的内容关掉"展开"按钮。
    """
    return left if _EXPANDABLE_RANK.get(left, 1) >= _EXPANDABLE_RANK.get(right, 1) else right


def _absorb(outer: Node, inner: Node) -> Node | None:
    """把 `outer` 并进 `inner`（`inner` 的 id、子节点、形状保留），返回新节点。

    用于"消掉单子链"：外层只是个标题、内层是内容。`inner` 当骨架是因为**引用与
    关系线大多指向有内容的那个**——保留它的 id，`relations` / `edges` / 用户记住的
    选中都不失效。

    超出上限（label / refs / detail / 子节点数）返回 `None`。
    """
    label = _joined_label(outer.label, inner.label)
    if label is None:
        return None
    refs = _union_refs(outer.source_refs, inner.source_refs)
    if refs is None:
        return None
    detail = _join_text(outer.detail, inner.detail)
    note = _join_text(outer.note, inner.note)
    if (outer.detail or inner.detail) and detail is None:
        return None
    if (outer.note or inner.note) and note is None:
        return None
    if len(inner.children) > MAX_NODE_CHILDREN:
        return None
    return Node(
        id=inner.id,
        label=label,
        children=[child.model_copy(deep=True) for child in inner.children],
        note=note,
        detail=detail,
        expandable=_wider_expandable(outer.expandable, inner.expandable),
        # 合并之后这一层一定有内容要看了，折叠状态归 "展开"：留着 `collapsed=True`
        # 会让用户点完修复看到的东西比修之前更少。
        collapsed=False,
        color=inner.color or outer.color,
        icon=inner.icon or outer.icon,
        style=inner.style if inner.style is not None else outer.style,
        role=inner.role or outer.role,
        source_refs=refs,
        # `pos` 是**手动坐标**（layoutMode=manual 时才生效）。两层合成一层之后旧坐标
        # 没有意义，取内层的那个：它是用户最后动过的位置的概率更高。
        pos=inner.pos if inner.pos is not None else outer.pos,
    )


def _merge_siblings(keep: Node, drop: Node) -> Node | None:
    """把 `drop` 并进同级的 `keep`（`keep` 的 id 与位置保留），返回新节点。

    **孩子的顺序是撤销的凭据**：新孩子 = `keep` 原孩子 + `drop` 原孩子。撤销时按
    `keep` 原来的孩子数把尾巴摘下来还给 `drop`，所以这个顺序不能变。
    """
    children = [child.model_copy(deep=True) for child in keep.children] + [
        child.model_copy(deep=True) for child in drop.children
    ]
    if len(children) > MAX_NODE_CHILDREN:
        return None
    refs = _union_refs(keep.source_refs, drop.source_refs)
    if refs is None:
        return None
    detail = _join_text(keep.detail, drop.detail)
    note = _join_text(keep.note, drop.note)
    if (keep.detail or drop.detail) and detail is None:
        return None
    if (keep.note or drop.note) and note is None:
        return None
    return Node(
        id=keep.id,
        # 两个标签逐字相同（`DUPLICATE_LABEL` 的判据），所以取谁都一样。
        label=keep.label,
        children=children,
        note=note,
        detail=detail,
        expandable=_wider_expandable(keep.expandable, drop.expandable),
        collapsed=False,
        color=keep.color or drop.color,
        icon=keep.icon or drop.icon,
        style=keep.style if keep.style is not None else drop.style,
        role=keep.role or drop.role,
        source_refs=refs,
        pos=keep.pos if keep.pos is not None else drop.pos,
    )


def _redirect(content: MindMapContent, gone: str, keep: str) -> list[RepairRedirect]:
    """把指向 `gone` 的关系线/附加边端点改到 `keep`，返回改了什么。

    刻意**不删除**一条线：`relations` 表达的是两个概念之间的关系，合并掉的那个节点
    与保留下来的那个是"同一件事的两种写法"（`DUPLICATE_LABEL` 的判据是逐字相同），
    所以那条关系对保留者同样成立。
    """
    records: list[RepairRedirect] = []
    for relation in content.relations:
        for field in ("source", "target"):
            if getattr(relation, field) == gone:
                records.append(RepairRedirect(kind="relation", id=relation.id, field=field,
                                              before=gone, after=keep))
                setattr(relation, field, keep)
    for edge in content.edges:
        for field in ("source", "target"):
            if getattr(edge, field) == gone:
                records.append(RepairRedirect(kind="edge", id=edge.id, field=field,
                                              before=gone, after=keep))
                setattr(edge, field, keep)
    return records


def _unredirect(content: MindMapContent, records: Sequence[RepairRedirect]) -> None:
    relations = {relation.id: relation for relation in content.relations}
    edges = {edge.id: edge for edge in content.edges}
    for record in reversed(list(records)):
        holder = relations.get(record.id) if record.kind == "relation" else edges.get(record.id)
        if holder is None:
            continue
        setattr(holder, record.field, record.before)


# ---------------------------------------------------------------- 三条修复


def _merge_duplicate(content: MindMapContent, keep_id: str, drop_id: str
                     ) -> tuple[RepairItem, None] | tuple[None, RepairSkip]:
    nodes, parents = _index(content)
    keep, drop = nodes.get(keep_id), nodes.get(drop_id)
    if keep is None or drop is None:
        return None, RepairSkip(code="DUPLICATE_LABEL", node_id=drop_id,
                                reason="两个节点里有一个已经不在了")
    parent_id = parents.get(keep_id)
    if parents.get(drop_id) != parent_id:
        return None, RepairSkip(code="DUPLICATE_LABEL", node_id=drop_id,
                                reason="两个节点不在同一个父节点下（诊断过期）")
    if parent_id is None:
        return None, RepairSkip(code="DUPLICATE_LABEL", node_id=drop_id,
                                reason="根节点没有同级节点")
    siblings = nodes[parent_id].children
    keep_index = next(index for index, node in enumerate(siblings) if node.id == keep_id)
    drop_index = next(index for index, node in enumerate(siblings) if node.id == drop_id)
    if drop_index <= keep_index:
        return None, RepairSkip(code="DUPLICATE_LABEL", node_id=drop_id,
                                reason="保留者不是先出现的那个：合并目标不唯一")
    merged = _merge_siblings(keep, drop)
    if merged is None:
        return None, RepairSkip(code="DUPLICATE_LABEL", node_id=drop_id,
                                reason=(f"合并后超出契约上限（子节点 ≤{MAX_NODE_CHILDREN}、"
                                        f"引用 ≤{MAX_SOURCE_REFS}、正文 ≤{MAX_NODE_DETAIL_CHARS} 字）"))

    undo = RepairUndo(
        parent_id=parent_id,
        at=keep_index,
        replaced=NodeFields.of(keep),
        replaced_children=len(keep.children),
        inserted=[RepairInsert(index=drop_index, fields=NodeFields.of(drop),
                               children=len(drop.children))],
    )
    siblings[keep_index] = merged
    del siblings[drop_index]
    undo.redirected = _redirect(content, drop_id, keep_id)
    return RepairItem(
        code="DUPLICATE_LABEL",
        kind=RepairKind.merge_duplicate,
        node_id=drop_id,
        message=(f"把「{drop.label.strip()}」并进了同级同名节点「{keep.label.strip()}」："
                 f"子树取并集（{len(keep.children)} + {len(drop.children)} 个），"
                 f"引用取并集（{len(merged.source_refs)} 条）"),
        undo=undo,
    ), None


def _collapse_chain(content: MindMapContent, parent_id: str, code: str
                    ) -> tuple[RepairItem, None] | tuple[None, RepairSkip]:
    """把"一层只有一个孩子"的那一层去掉：外层并进内层。"""
    nodes, parents = _index(content)
    outer = nodes.get(parent_id)
    if outer is None:
        return None, RepairSkip(code=code, node_id=parent_id, reason="这个节点已经不在了")
    if len(outer.children) != 1:
        return None, RepairSkip(code=code, node_id=parent_id,
                                reason=f"现在它有 {len(outer.children)} 个子节点（诊断过期）")
    inner = outer.children[0]
    grandparent_id = parents.get(parent_id)

    merged = _absorb(outer, inner)
    if merged is None:
        return None, RepairSkip(code=code, node_id=parent_id,
                                reason=(f"合并后超出契约上限（标题 ≤{MAX_NODE_LABEL_CHARS} 字、"
                                        f"引用 ≤{MAX_SOURCE_REFS}、正文 ≤{MAX_NODE_DETAIL_CHARS} 字）"))

    if grandparent_id is None:
        # 根自己只有一个孩子：根不能消失，所以把孩子的子树并进根，根的内容留下。
        undo = RepairUndo(parent_id=None, at=0,
                          replaced=NodeFields.of(outer),
                          replaced_children=1,
                          inner=NodeFields.of(inner))
        content.root = merged.model_copy(update={"id": outer.id}, deep=True)
        # 根自己的 id 保住了，消失的是**孩子**的 id。
        undo.redirected = _redirect(content, inner.id, outer.id)
        message = (f"根「{outer.label.strip()}」下只有一条支「{inner.label.strip()}」，"
                   f"已把两层合成一层（标题为「{merged.label.strip()}」）")
    else:
        siblings = nodes[grandparent_id].children
        index = next(position for position, node in enumerate(siblings) if node.id == parent_id)
        undo = RepairUndo(parent_id=grandparent_id, at=index,
                          replaced=NodeFields.of(outer),
                          replaced_children=1,
                          inner=NodeFields.of(inner))
        siblings[index] = merged
        undo.redirected = _redirect(content, outer.id, inner.id)
        message = (f"「{outer.label.strip()}」下只有一条支「{inner.label.strip()}」，"
                   f"已把两层合成一层（标题为「{merged.label.strip()}」）")

    return RepairItem(
        code=code,
        kind=RepairKind.collapse_chain,
        node_id=outer.id,
        message=message,
        undo=undo,
    ), None


def _apply(content: MindMapContent, diagnostic: Diagnostic
           ) -> tuple[RepairItem, None] | tuple[None, RepairSkip]:
    if diagnostic.code == "DUPLICATE_LABEL":
        keep_id = diagnostic.evidence or ""
        drop_id = diagnostic.node_id or ""
        if not keep_id or not drop_id:
            return None, RepairSkip(code=diagnostic.code, node_id=diagnostic.node_id,
                                    reason="诊断没有给出「保留哪一个」")
        return _merge_duplicate(content, keep_id, drop_id)
    if diagnostic.code in ("SINGLE_CHILD", "HEADING_ONLY_BRANCH"):
        if not diagnostic.node_id:
            return None, RepairSkip(code=diagnostic.code, node_id=None, reason="诊断没有指明节点")
        return _collapse_chain(content, diagnostic.node_id, diagnostic.code)
    return None, RepairSkip(code=diagnostic.code, node_id=diagnostic.node_id,
                            reason=MANUAL_ONLY.get(diagnostic.code, "这条规则没有自动修法"))


def repair(
    content: MindMapContent, *, codes: Sequence[str] = AUTO_REPAIRABLE_CODES,
    max_steps: int | None = None,
) -> RepairResult:
    """把能自动修的那几条修到不动点，返回修好的树 + 做了什么 + 反操作。

    `codes` 决定"这一趟只修哪几类"（界面可以给三个独立的按钮）。**它不影响幂等**：
    只修 `DUPLICATE_LABEL` 一次之后，`codes` 不变再跑一次仍然什么都不做。

    `max_steps` 缺省 = 树的节点数。这不是随手取的：每次修复**严格让节点少一个**
    （合并删掉一个、压链删掉一层），所以"最多 n 步"既是终止证明，也保证第一趟就能
    修到不动点——固定成 200 那类常数会让大图的第二趟继续修，于是同一个输入两次调用
    给出两棵不同的树（真图上量到过，见模块 docstring 第 1 条）。

    ## 两段式，不是"按诊断顺序"

    先做**全部合并**、再做**全部压链**，两段交替到不动点。顺序是刻意的：

      - 合并的判据是 `label` 逐字相同；
      - 压链**会改 label**（`外层 / 内层`）。

    所以"先压链"会把两个同名的兄弟压成 `甲 / 子` 与 `乙 / 子`，它们就不再同名了——
    合并这一步被前面的步骤无意中取消。反过来（先合并、后压链）不会：合并只减少同名
    节点，压链只改已经独一无二的标题。

    两段交替而不是各跑一次：压链可能造出新的同级同名（两个不同的 `甲 / 子` 被压到
    同一层），那要回到合并段处理。
    """
    allowed = tuple(code for code in codes if code in AUTO_REPAIRABLE_CODES)
    merges = tuple(code for code in allowed if code == "DUPLICATE_LABEL")
    collapses = tuple(code for code in allowed if code != "DUPLICATE_LABEL")

    budget = _count_nodes(content.root) if max_steps is None else max_steps
    work = content.model_copy(deep=True)
    applied: list[RepairItem] = []
    skipped: list[RepairSkip] = []
    blocked: set[tuple[str, str | None, str]] = set()

    def first_of(group: tuple[str, ...]) -> Diagnostic | None:
        for diagnostic in check(work):
            if diagnostic.code not in group:
                continue
            if (diagnostic.code, diagnostic.node_id, diagnostic.evidence) in blocked:
                continue
            return diagnostic
        return None

    while len(applied) < budget:
        progressed = False
        for group in (merges, collapses):
            while len(applied) < budget:
                target = first_of(group)
                if target is None:
                    break
                item, skip = _apply(work, target)
                if item is not None:
                    applied.append(item)
                    progressed = True
                    continue
                assert skip is not None
                skipped.append(skip)
                # 修不了的那一条要被记住：不记就会在下一轮再取到同一条，死循环。
                blocked.add((target.code, target.node_id, target.evidence))
        if not progressed:
            break

    return RepairResult(
        content=work,
        applied=applied,
        skipped=skipped,
        remaining=check(work),
        steps=len(applied),
    )


def _locate(content: MindMapContent, parent_id: str | None, at: int) -> list[Node]:
    """撤销时的"槽位"：根那一层返回 `[root]` 的假列表，其余返回父的 children。"""
    if parent_id is None:
        return [content.root]
    nodes, _ = _index(content)
    parent = nodes.get(parent_id)
    if parent is None:
        raise ValueError(f"撤销失败：父节点 {parent_id} 已经不在树里")
    return parent.children


def revert(content: MindMapContent, items: Sequence[RepairItem]) -> MindMapContent:
    """撤销一串修复（逆序），返回还原后的树。

    `revert(repair(x).content, repair(x).applied)` 与 `x` 逐字段相同——这是"可撤销"
    的可检验形式，也是**唯一**该被信任的形式：前端撤销栈只在一个会话里有效，而这个
    函数要在任何调用方手里成立。

    历史对不上时**报错**：静默返回一棵怪树会让"撤销过一次、图却更乱了"变成一个只在
    用户那里发生、复现不出来的问题。
    """
    work = content.model_copy(deep=True)
    for item in reversed(list(items)):
        undo = item.undo
        _unredirect(work, undo.redirected)
        siblings = _locate(work, undo.parent_id, undo.at)
        if undo.at >= len(siblings):
            raise ValueError(f"撤销失败：槽位 {undo.parent_id or 'root'}[{undo.at}] 不存在")

        if undo.inner is not None:
            # 压链：把当前那个合成节点拆回两层（内层留住现在的孩子）。
            merged = siblings[undo.at]
            inner = undo.inner.build(children=merged.children)
            siblings[undo.at] = undo.replaced.build(children=[inner])
            if undo.parent_id is None:
                work.root = siblings[0]
            continue

        # 合并：按"原来有几个孩子"把尾巴摘下来还给被并掉的那个。
        kept = siblings[undo.at]
        tail = kept.children[undo.replaced_children:]
        siblings[undo.at] = undo.replaced.build(children=kept.children[:undo.replaced_children])
        for insert in sorted(undo.inserted, key=lambda record: record.index):
            children = tail[:insert.children]
            tail = tail[insert.children:]
            siblings.insert(insert.index, insert.fields.build(children=children))
        if undo.parent_id is None:
            work.root = siblings[0]
    return work


def repair_summary(result: RepairResult) -> str:
    """一句人话，给状态行/日志用。空修复时返回空串（调用方据此不加这一段）。"""
    if not result.applied:
        return ""
    merges = sum(1 for item in result.applied if item.kind == RepairKind.merge_duplicate)
    collapses = len(result.applied) - merges
    parts = []
    if merges:
        parts.append(f"合并了 {merges} 处同级重名")
    if collapses:
        parts.append(f"压掉了 {collapses} 处单子链")
    text = "，".join(parts)
    if result.skipped:
        text += f"；{len(result.skipped)} 处没修（超出契约上限或诊断过期）"
    return text


__all__ = [
    "AUTO_REPAIRABLE_CODES",
    "MANUAL_ONLY",
    "MAX_SOURCE_REFS",
    "NodeFields",
    "RepairItem",
    "RepairKind",
    "RepairResult",
    "RepairSkip",
    "RepairUndo",
    "repair",
    "repair_summary",
    "revert",
]
