"""导图操作契约（agent 与原生的编辑共用同一套）。

位置：契约层。执行器在 `app/agent/ops.py`，前端同构实现在
`frontend/src/mindmap/ops.ts`，两端的类型定义必须逐字段对应。

字段命名一律 snake_case：这些结构会原样跨端传输，转换只会在中间某处
漏掉一个字段，而那种 bug 表现为"保存后引用没了"这种极难排查的现象。
项目里既有的 `source_refs` / `source_usage` 就是这个口径。

为什么不复用整树 PUT：整树提交无法表达"只改了哪一处"，服务端与前端都
只能整体替换，一次生成就会抹掉用户的手工编辑。op 是唯一能同时满足
"模型可精确改"与"用户改动不被覆盖"的粒度。
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts.source import SourceRef
from app.contracts.mindmap import MAX_NODE_DETAIL_CHARS, MAX_NODE_LABEL_CHARS, MAX_TREE_NODES


class OpError(Exception):
    """单个 op 执行失败。

    `path` 用 `ops.<下标>.<字段>` 的形式，因为这段文字会作为工具结果回给模型，
    它需要知道是哪一条、哪个字段错了才能自己改对；只回一句"操作失败"，
    模型只能整批重写。
    """

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class _OpBase(BaseModel):
    # extra="forbid" 不是洁癖：accept 一个多余字段，就意味着模型可以往契约里
    # 塞我们没实现的语义，而执行器会静默忽略它。宁可报错。
    model_config = ConfigDict(extra="forbid")


class AddOp(_OpBase):
    """在当前树里挂一个新节点。

    `id` 由服务端在规范化阶段分配（见 `agent/ops.py::new_node_id`），
    模型草案里用的是 `$tmp1` 这种临时名，避免模型自造 id 与既有节点撞车。
    `index` 是该父节点当前子列表里的插入位置，允许等于 length（追加到末尾）。
    """

    op: Literal["add"]
    id: str = Field(min_length=1, max_length=100)
    parent_id: str = Field(min_length=1, max_length=100)
    index: int = Field(ge=0)
    label: str = Field(min_length=1, max_length=MAX_NODE_LABEL_CHARS)
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=20)
    # 生成产出的说明。`add` 必须能带它，否则分片生成的长文本只能塞回 label——
    # 那正是"节点是完整句子、画布挤成一团"的成因。缺省 `None` 与"空详情"同义
    # （契约层会把空串归一成 None，见 `mindmap.Node.blank_text_as_none`）。
    detail: str | None = Field(default=None, max_length=MAX_NODE_DETAIL_CHARS)


class UpdateChanges(BaseModel):
    """只允许改这三个字段。

    `label` / `detail` / `source_refs` 都是 `None` 表示"这一项不动"，与"清空"
    区分开：`source_refs=[]` 是显式清空，`source_refs=None` 是保持原样。这个区别
    丢不得，否则模型漏写一个字段就等于把引用全删了。

    `detail` 走同一个约定：`None` = 不动，空串 = 清空（执行器归一成 `None`）。
    用户在界面上手写的 `note` 不在这三个字段里——生成侧永远碰不到它。
    """

    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, min_length=1, max_length=MAX_NODE_LABEL_CHARS)
    detail: str | None = Field(default=None, max_length=MAX_NODE_DETAIL_CHARS)
    source_refs: list[SourceRef] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def at_least_one(self) -> UpdateChanges:
        if self.label is None and self.detail is None and self.source_refs is None:
            raise ValueError("update 至少要改一个字段")
        return self


class UpdateOp(_OpBase):
    op: Literal["update"]
    id: str = Field(min_length=1, max_length=100)
    changes: UpdateChanges


class RemoveOp(_OpBase):
    """删节点及其整棵子树。根节点不可删——删了就没有导图了。"""

    op: Literal["remove"]
    id: str = Field(min_length=1, max_length=100)


class MoveOp(_OpBase):
    """把节点（连同子树）挂到另一个父节点下。

    这是四个 op 里唯一能改变拓扑的，也是唯一能造出环的：把 A 移到它自己的
    子孙下面，树就不再是一棵树。环检测在执行器里，不在这里——契约层拿不到
    整棵树，做不了这个判断。
    """

    op: Literal["move"]
    id: str = Field(min_length=1, max_length=100)
    new_parent_id: str = Field(min_length=1, max_length=100)
    index: int = Field(ge=0)


MindMapOp = Annotated[
    Union[AddOp, UpdateOp, RemoveOp, MoveOp],
    Field(discriminator="op"),
]


class OpBatch(BaseModel):
    """一批原子操作。

    整批要么全成、要么全不成：执行器在副本上按序执行，任何一条抛 OpError
    就丢弃副本。这不是"尽力而为"的批量，因为半批生效会让树的 shape 和
    模型以为的不一致，而模型下一轮会基于那个错误前提继续改。

    `base_revision` 是这批 op 所依据的树版本。执行前比对，不一致就整批拒绝，
    防止"模型看着旧树改、用户已经改过"被静默合并。
    """

    model_config = ConfigDict(extra="forbid")
    batch_id: str = Field(min_length=1, max_length=100)
    base_revision: int = Field(ge=0)
    # 上限取整树节点数：深层生成是**一次性提交整棵树**，一个节点对应一个 add，
    # 所以一批的条数天然可以到节点上限。这里曾经固定 100，深一点的图直接抛
    # ValidationError——而那道限制既不表达任何真实边界，也没人知道它存在，
    # 只在放开生成深度之后才暴露出来。
    #
    # 不下放到 100 以下的理由：批次是原子的，拆成多批提交就不再是"整棵树一起
    # 校验"，中途失败会留下半棵树。
    ops: list[MindMapOp] = Field(min_length=1, max_length=MAX_TREE_NODES)
