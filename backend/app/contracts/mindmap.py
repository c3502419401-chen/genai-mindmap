"""导图契约（v2，见 `docs/CONTRACT_V2.md`）。

v1 只有 `id`/`label`/`children`/`source_refs`。v2 补齐了画布与引用需要的字段：
正文 `note`、布局 `layout`、主题 `theme`、跨节点关系 `relations`，以及节点级的
样式与手动坐标。**新字段全部有默认值**，所以库里那些没有 `schema_version` 的
v1 记录不需要迁移脚本就能读进来（`migrate_content()` 负责这一步，见文件末尾），
保存时自然写成 v2。
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.contracts.source import SourceRef, SourceUsage

# 整树节点上限。1000 是**承载能力**：前端渲染、保存、导出都还扛得住，所以它是
# 契约的硬边界而不是产品口径。原先的 300 是照「一句话主题」场景拍的，一份
# 12106 字的材料实测就能长出 526 个节点——上限比材料要求还低时，被砍掉的是
# 用户的内容而不是模型的发挥。
MAX_TREE_NODES = 1000

# 层级上限。8 层是可读性约束（再深，横向滚动换来的信息量已经抵不过"看不见全貌"），
# 不是承载能力，所以它保持不变。
MAX_TREE_DEPTH = 8

# 关系条数上限。200 条大约是一张 1000 节点导图里"跨分支解释性关系"的合理密度，
# 再多的画布也读不清——那时用户要的其实是子节点，不是连线。
MAX_RELATIONS = 200

# 节点标题（`label`）的**契约上限**= 200。这是给旧图留的读取口径：库里那些
# 一句话当标题的记录（实测 20–30 字，最长的一批贴着 200）必须照常打得开。
MAX_NODE_LABEL_CHARS = 200

# AI 生成时的标题上限 = 16 码点。这是**生成侧**的约束（`generation.service` 与
# `agent.tools` 的归一化都读它），不是契约校验：把 `Field(max_length=200)` 直接
# 改成 16，等于让所有旧图在下一次保存时 422——用户什么都没改，图却存不回去。
#
# 16 这个数的依据是画布：`180px` 宽的圆角框里放 14px 字，一行约 12 个字、两行
# 24 个字，16 字是"缩到能看见全貌"之后仍读得出的长度。超过它的内容属于详情
# （`detail`），不属于标题。
GENERATED_LABEL_MAX_CHARS = 16

# 节点详情上限。与 `note` 同为 2000：同一块文字在界面上占同一块地方，两个字段
# 的容量不该一个够写、一个不够写。`detail` 与 `note` 的**语义**不同（生成产出
# vs 用户备注），所以只能分开存，不能合并（见 `Node.detail` 的注释）。
MAX_NODE_DETAIL_CHARS = 2000


def clip_label(text: str, limit: int = GENERATED_LABEL_MAX_CHARS) -> str:
    """按**码点**截断标题，返回不超过 `limit` 个码点的字符串。

    按码点而不是 UTF-16 下标：`Array.from` / Python 的 `str` 都以码点为单位，
    而 JS 的 `length` 会把一个 emoji 数成 2——用码点才能让两端对"16 字"给出同
    一个答案（前端 `edits.ts::charLength` 也是这个口径）。

    省略号**不**追加：它是 1 个码点，加上去会让"16 字"变成 17 字，于是再截一次、
    再判一次，最后谁也不知道上限到底是几。要显示省略号的地方（画布）自己加。
    """
    if limit <= 0:
        return ""
    chars = list(text or "")
    return "".join(chars[:limit])


def _fold(text: str) -> str:
    """把换行与连续空白折成单空格。

    导出三种格式里换行都会破坏结构（缩进、XML 属性），而节点标题里带换行的来源
    很具体：模型偶尔把一句话拆成两行返回。折在契约层比在三个导出函数里各折一次
    可靠，也让"同一个节点在画布和导出里文字一致"这件事有单一实现。
    """
    return " ".join(text.split())


class Position(BaseModel):
    """节点坐标。只有 `layout == "manual"` 时有意义——自动布局的坐标是算出来的，
    存进数据只会在下次换布局时被当成用户意图。"""
    model_config = ConfigDict(extra="forbid")
    x: float = Field(ge=-1e6, le=1e6)
    y: float = Field(ge=-1e6, le=1e6)


class NodeStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    icon: str | None = Field(default=None, max_length=40)
    bold: bool = False
    italic: bool = False
    shape: Literal["rounded", "rect", "pill"] = "rounded"


class RelationType(str, Enum):
    cause = "cause"        # A 导致 B
    ref = "ref"            # A 引用/参见 B
    conflict = "conflict"  # A 与 B 冲突
    depends = "depends"    # A 依赖 B


class Relation(BaseModel):
    """跨节点的关系线。来源与目标是节点 id，必须都在树里。"""
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=100)
    type: RelationType
    label: str | None = Field(default=None, max_length=100)
    evidence: str | None = Field(default=None, max_length=300)
    provenance: Literal["user", "ai", "import"] = "user"

    @field_validator("evidence")
    @classmethod
    def blank_evidence_as_none(cls, value: str | None) -> str | None:
        return value if value and value.strip() else None


class Theme(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preset: Literal["default", "slate", "forest", "sunset", "mono"] = "default"
    accent: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    font: Literal["system", "serif", "mono"] = "system"
    direction: Literal["right", "left", "both"] = "right"


class LayoutKind(str, Enum):
    tree = "tree"          # 经典思维导图（左右/单向）
    vertical = "vertical"  # 根在上、子在下（画布上的"向下"）
    logic = "logic"        # 逻辑结构图（向右缩进的清单，与 vertical 不同）
    timeline = "timeline"
    fishbone = "fishbone"  # L3，不进 v1.0.0 的 UI
    manual = "manual"      # 用户拖拽后进入，pos 生效


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=MAX_NODE_LABEL_CHARS)
    children: list[Node] = Field(default_factory=list, max_length=50)
    # 正文。`label` 是短标题（画布上只放得下 200 字以内的两行），长的内容放这里。
    # 渲染取值收敛为 `note or label`，两处都空是不可能的（label 非空）。
    #
    # **`note` 是用户自己写的备注**：`agent/ops.py` 的 add/update 之外，只有
    # `stores/mindmap.ts::commitNote`（界面上的编辑框）会动它。所以任何"模型生成"
    # 的内容都**不许**写进 `note`——重新生成一次就会把用户敲的字整段盖掉，而那种
    # 丢失在界面上看不出来（它长得就像"模型又写了一遍"）。
    note: str | None = Field(default=None, max_length=MAX_NODE_DETAIL_CHARS)
    # 生成出来的说明。与 `note` 同容量、不同语义：`detail` 归生成（整树、分片
    # 展开、单节点展开都会填），`note` 归用户。界面上两者分区展示。
    #
    # 上限 2000 与 `note` 一致；超出的部分不静默截断——`Field` 会直接拒收，
    # 因为一条 3000 字的 detail 说明提示词本身没被遵守，该在生成侧归一化
    # （`tools._slot_from` 已经截了），不该让契约替它兜。
    detail: str | None = Field(default=None, max_length=MAX_NODE_DETAIL_CHARS)
    collapsed: bool = False
    style: NodeStyle | None = None
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=20)
    pos: Position | None = None

    @field_validator("label")
    @classmethod
    def fold_label(cls, value: str) -> str:
        folded = _fold(value)
        if not folded:
            raise ValueError("节点标题不能为空")
        return folded

    @field_validator("note", "detail")
    @classmethod
    def blank_text_as_none(cls, value: str | None) -> str | None:
        """纯空白的正文等于没写。

        留一个空串会让前端多一个"有正文但显示为空"的分支（详情区显示一块空白，
        回退逻辑永远不触发），而 `null` 的语义是明确的"没有这段内容"。
        """
        return value if value and value.strip() else None


class MindMapContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[2] = 2
    title: str = Field(min_length=1, max_length=200)
    root: Node
    layout: LayoutKind = LayoutKind.tree
    theme: Theme = Field(default_factory=Theme)
    relations: list[Relation] = Field(default_factory=list, max_length=MAX_RELATIONS)
    source_usage: list[SourceUsage] = Field(default_factory=list, max_length=50)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("导图标题不能为空")
        return text

    @model_validator(mode="after")
    def validate_tree(self) -> MindMapContent:
        # 第二层：保存、读取和所有接收导图的接口共用同一关联约束。
        source_ids = {source.source_id for source in self.source_usage}
        if len(source_ids) != len(self.source_usage):
            raise ValueError("来源摘要中的来源 ID 不能重复")
        seen: set[str] = set()
        has_position = False
        stack = [(self.root, 1)]
        while stack:
            node, depth = stack.pop()
            if node.id in seen:
                raise ValueError("节点 ID 必须唯一")
            seen.add(node.id)
            if node.pos is not None:
                has_position = True
            if len(seen) > MAX_TREE_NODES or depth > MAX_TREE_DEPTH:
                raise ValueError(
                    f"导图最多 {MAX_TREE_NODES} 个节点，深度不超过 {MAX_TREE_DEPTH} 层")
            for ref in node.source_refs:
                if ref.source_id not in source_ids:
                    raise ValueError(f"节点 {node.id} 引用的来源 {ref.source_id} 不在 source_usage 中")
            stack.extend((child, depth + 1) for child in node.children)

        # 坐标只在手动布局下有意义：自动布局的图带着历史坐标，换布局时会被
        # 当成"用户想把这个节点放这里"，画出来是一团乱麻。
        if self.layout is LayoutKind.manual:
            if not has_position:
                raise ValueError("手动布局至少需要一个节点带坐标")
        elif has_position:
            raise ValueError("只有手动布局才允许保存节点坐标")

        relation_ids: set[str] = set()
        relation_keys: set[tuple[str, str, RelationType]] = set()
        for relation in self.relations:
            if relation.id in relation_ids:
                raise ValueError("关系 ID 必须唯一")
            relation_ids.add(relation.id)
            if relation.source not in seen:
                raise ValueError(f"关系 {relation.id} 的起点节点 {relation.source} 不在导图中")
            if relation.target not in seen:
                raise ValueError(f"关系 {relation.id} 的终点节点 {relation.target} 不在导图中")
            if relation.source == relation.target:
                raise ValueError(f"关系 {relation.id} 的起点与终点不能是同一个节点")
            key = (relation.source, relation.target, relation.type)
            if key in relation_keys:
                raise ValueError("同一对节点之间的同类关系不能重复添加")
            relation_keys.add(key)
        return self


# v1 记录里缺的字段。只补顶层：Node 的新字段由 Pydantic 默认值补齐，不需要
# 递归改写——那会让"迁移"变成"重写整棵树"，一旦某个字段语义变过就再难回头。
V1_TO_V2_DEFAULTS: dict[str, object] = {
    "schema_version": 2,
    "layout": LayoutKind.tree.value,
    "theme": {},
    "relations": [],
}


def migrate_content(data: dict) -> dict:
    """把库里读出来的 v1 记录补成 v2 形状；已经是 v2 就原样返回。

    幂等，且**只补缺、不覆盖**：调用方（仓储层）拿到的 dict 可能已经是 v2，
    也可能带着未来版本才有的字段，这里不做裁剪。
    """
    if not isinstance(data, dict):
        raise TypeError("导图内容必须是对象")
    if data.get("schema_version") == 2:
        return data
    return {**{k: v for k, v in V1_TO_V2_DEFAULTS.items() if k not in data}, **data}


class SaveRequest(BaseModel):
    content: MindMapContent


class UpdateRequest(SaveRequest):
    version: int = Field(ge=1)


class MapRecord(BaseModel):
    id: str
    version: int
    content: MindMapContent
    updated_at: str
