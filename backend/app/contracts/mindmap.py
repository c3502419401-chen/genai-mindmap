"""导图契约（v2，见 `docs/CONTRACT_V2.md`）。

v1 只有 `id`/`label`/`children`/`source_refs`。v2 补齐了画布与引用需要的字段：
正文 `note`、布局 `layout`、主题 `theme`、跨节点关系 `relations`，以及节点级的
样式与手动坐标。**新字段全部有默认值**，所以库里那些没有 `schema_version` 的
v1 记录不需要迁移脚本就能读进来（`migrate_content()` 负责这一步，见文件末尾），
保存时自然写成 v2。
"""
from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.contracts.edits import EditEventIn, MAX_EVENTS_PER_SAVE
from app.contracts.source import SourceRef, SourceUsage

# 整树节点上限。1000 是**承载能力**：前端渲染、保存、导出都还扛得住，所以它是
# 契约的硬边界而不是产品口径。原先的 300 是照「一句话主题」场景拍的，一份
# 12106 字的材料实测就能长出 526 个节点——上限比材料要求还低时，被砍掉的是
# 用户的内容而不是模型的发挥。
MAX_TREE_NODES = 1000

# 层级上限。8 层是可读性约束（再深，横向滚动换来的信息量已经抵不过"看不见全貌"），
# 不是承载能力，所以它保持不变。
MAX_TREE_DEPTH = 8

# 单个节点的子节点上限。它是**硬边界**（`Node.children` 的 `max_length`），
# 与另外两个限额不同：超过不是"该告警"，而是契约层直接 422。
#
# 50 这个数是横向排布的承载能力：一级分支 50 个已经要滚动三屏，再多就不是"这个
# 主题有 50 个方面"，而是"这里没被整理过"。导入侧要用它做截断（超了砍掉并告警），
# 所以它必须有常量名——写死在 `Field(max_length=50)` 里时，导入层只能重复一个 50。
MAX_NODE_CHILDREN = 50

# 关系条数上限。200 条大约是一张 1000 节点导图里"跨分支解释性关系"的合理密度，
# 再多的画布也读不清——那时用户要的其实是子节点，不是连线。
MAX_RELATIONS = 200

# 附加边（`MindMapContent.edges`）的条数上限。200 与 `MAX_RELATIONS` 同口径：
# 一张 1000 节点的图里"跨父子节点的附加关系"超过 200 条时，画布已经读不清了——
# 那时用户要的其实是节点，不是连线。
#
# 与 `relations` 的差别只有一处，但那处很重要：**这是 warn 口径，不是硬边界**。
# relations 超过 200 由 `Field(max_length=...)` 直接拒；edges 超过 200 只报
# `EDGE_LIMIT`（见 `app/mindmaps/edges.py`），图照常能存、能画。理由是 flow
# 图里边的数量天然随分支数增长，为了几条多出来的连线把整张图判失败，代价方向
# 完全错了。硬边界在下面的 `MAX_EDGES_LIMIT`，它只挡真正会让传输/渲染出问题
# 的量级。
MAX_EDGES = 200

# `edges` 的承载硬边界 = 10 × 产品口径。它挡的是"一份 JSON 里塞十万条边"这种
# 滥用，不是"这张图画得有点密"。两个数分开，等于把"该告警"和"不能接受"分开。
MAX_EDGES_LIMIT = MAX_EDGES * 10

# 边上的文字上限。边上只该放一个词或一小句（"是" / "否" / "通过" / "驳回"），
# 40 字够写「驳回：材料不全，退回补正」。再长的内容属于节点详情（`detail`）。
MAX_EDGE_LABEL_CHARS = 40

# 最长环的节点数。流程图里的环是**回退/返工**，一屏能看懂的回路不超过十来个
# 节点；再长的环在画布上会绕成一团，用户看不出从哪里回到哪里。超了报
# `EDGE_CYCLE_TOO_LONG`（warn），不硬拒——把已经画出来的图整张判失败，代价
# 远大于"这个环有点长"这件事本身。
MAX_EDGE_CYCLE_NODES = 12

# 边的语义类型。三个值对应三种画法（前端的自定义边组件按它换样式）：
#   - `flow`：主干上的顺序跳转（默认，画实线箭头）
#   - `branch`：判断节点的条件出口（"是"/"否"两种去向，换色）
#   - `loop`：回退（虚线 + 箭头，一眼看出"流程往回走"）
#
# 为什么是 `Literal` 而不是自由字符串：颜色/线型要按它选，而自由字符串的意思
# 是"每一个没见过的值都渲染成默认样式"——那三个值就白定义了。模型写错时契约层
# 会拒，模型能照着错误信息改对；渲染层则永远只处理这三支。
EDGE_KINDS = ("flow", "branch", "loop")
EdgeKind = Literal["flow", "branch", "loop"]

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

# 一个节点还能不能再往下展开。**这是给界面画按钮用的状态，不是给模型看的信号**
# （模型那一侧的对应物是展开响应里的布尔 `expandable`，两回事）。
#
# 存进树里而不是每次现算，是因为它有三件事现算不出来：
#
#   1. **模型说过"到底了"**。只有写那次响应的模型知道"这个要点已经是最小可执行
#      单元"，那个判断随调用一起消失；不落库，下次打开这张图就只能猜。
#   2. **被额度截断过**。截断发生在生成过程中，事后从形状上看与"模型说没有更多
#      内容"完全一样（都是个叶子），而两者对用户的意思相反：一个是"到头了"，
#      一个是"没展开完，还能再点一次"。
#   3. **层数到顶**。第 8 层是契约硬边界（`MAX_TREE_DEPTH`），加不出子节点。
#
# 三态而不是布尔：`unknown` 必须与 `no` 分开。被额度截断的节点若被记成 `no`，
# 界面上按钮灰掉、说明"已经到底了"——用户被一句假话挡住了唯一能继续的入口，
# 而这张图看起来是完整的。反过来把真的到头的节点记成 `yes`，用户点了只有"没
# 有更多内容"可选，白花一次调用。
NODE_EXPANDABLE = ("yes", "no", "unknown")

# 节点颜色的**调色板**。`Node.color` 只能取这八个键之一，不接受任意 hex。
#
# 为什么不做 `#RRGGBB`：模型自由发挥的 hex 会得到一堆脏色（同一张图里出现
# `#3f8ac9`、`#4a90d9`、`#2f7fbf` 三个几乎一样的蓝），而且换主题（暗色/亮色）
# 时无法自动适配——hex 是**结论**，不是语义。键是语义（`red` 就是"风险/告警"），
# 具体色值留给渲染层：CSS 变量一处定义，暗色主题只覆盖 `:root`，导图数据不动。
#
# 八个键是按"能表达一张图里的分类"选的，不是凑数：主色（blue）、正向/结论
# （green）、注意/待定（amber）、风险（red）、并列主题（purple）、过程/辅助
# （teal）、人/组织（pink）、背景/其他（gray）。再多就会开始重复语义，
# 而两个键意思一样时模型只会随机挑一个。
NODE_COLORS = ("blue", "green", "amber", "red", "purple", "teal", "pink", "gray")

# 调色板键 → 前端 CSS 变量名的前缀。**只有前缀在契约里**，具体色值在
# `frontend/src/styles.css` 的 `:root` 里定义。
#
# 放在这里是为了让"后端加了一个颜色、前端却没定义"这件事能被用例抓住
# （`tests/test_node_visuals.py` 读 styles.css 逐个比对）：静默漏一个键的表现是
# 那个颜色画出来是默认灰，而没有任何地方会报错。
NODE_COLOR_CSS_PREFIX = "--mind-color-"

# 节点的图标上限（码点）。emoji 常见 1–4 个码点（`🎯` 是 1，带肤色/ZWJ 的
# `👨‍👩‍👧` 可以到 8），16 给组合 emoji 留够余量，同时挡住"把一句话塞进图标"。
MAX_NODE_ICON_CHARS = 16

NodeColor = Literal["blue", "green", "amber", "red", "purple", "teal", "pink", "gray"]

# 图型。三个值同时是 `AgentRunRequest.diagram_type`（请求：用户要画什么）与
# `MindMapContent.diagram_type`（产物：画成了什么）的取值集合——一处定义，两处
# 引用。分成两份字面量写的后果是可预期的：某天加第四种图型，请求层收下了、
# 契约层 422，而报错会指向产物而不是那张没被加进去的表。
#
# **它不是 `layout`**：`layout` 描述排布（横向 / 纵向 / 时间线 / 手动坐标），
# 图型描述"这张图按什么读"——流程图要看条件分支与回退，鱼骨图的一级分支固定是
# 人 / 机 / 料 / 法 / 环 / 测。
DIAGRAM_TYPES = ("tree", "flowchart", "fishbone")
DiagramKind = Literal["tree", "flowchart", "fishbone"]

# 图型的中文名。导出文件头（`export.service.to_puml`）要用它：写成 `fishbone`
# 读不出意思，写成"因果图"又与界面上那个下拉的名字对不上。**只放中文名，不放
# 说明**——说明属于界面（`SourceInputs.vue` 里那四个 hint）。
DIAGRAM_LABELS = {"tree": "思维导图", "flowchart": "流程图", "fishbone": "鱼骨图"}

# 鱼骨图一级分支（"骨"）的典型六类：5M1E 的人 / 机 / 料 / 法 / 环 / 测。
#
# **它是一份推荐词表，不是契约白名单**。契约层拿它做的事只有一件：把"类别"这个
# 字段的长度与去重判掉（见 `FishboneContent.validate_fishbone`）。为什么不硬绑：
# 5M1E 之外还有 8P、4M1E、服务业的"人 / 流程 / 系统 / 外部"，而模型写"人员"而不是
# "人"时，硬绑的表现是整张图 422——用户宁可要一张"类别名字不太标准但内容对"的
# 鱼骨图。词表留给生成侧做提示词与诊断（阶段 2），那里报 warn 是恰当的粒度。
FISHBONE_CATEGORIES = ("人", "机", "料", "法", "环", "测")

# 骨的根数上限。六类就是六根，`Field(max_length=...)` 是硬边界而不是 warn：
# 骨是**顶层数组**，多出来的第七根在画布上没有位置（主骨上下各三根是 5M1E 的
# 固定摆法），静默画到框外比 422 更难排查。少于此数完全合法——很多问题的成因
# 只有三类，凑到六根等于让模型编。
MAX_FISHBONE_BONES = len(FISHBONE_CATEGORIES)

# 骨的类别上限（码点）。类别是一个词（"人"、"流程"、"外部环境"），40 字够写
# "供应商来料与库存周转"，再长的内容属于 `problem` 或节点详情。
MAX_FISHBONE_CATEGORY_CHARS = 40

# 主骨上那句话的上限。与 `MAX_NODE_LABEL_CHARS` 同口径：它最终会被画成一行的字，
# 200 是"一句话主题"的既有容量。写成 `"为什么 X 会 Y"` 的句式通常 30–60 字。
MAX_FISHBONE_PROBLEM_CHARS = 200

# 流程图的阶段数上限。阶段是读者一眼扫过的大段（"提交 / 审查 / 付款"），20 段在
# 1080p 画布上已经要横向滚两屏。与 `bones` 同为**顶层数组**，所以它是
# `Field(max_length=...)` 的硬边界而不是 warn：多出来的阶段在主轴上没有位置，
# 静默挤到画布外比 422 更难排查。
MAX_FLOW_STAGES = 20


def normalize_color(value: object) -> str | None:
    """模型给的 `color` → 调色板里的键，或 `None`。

    三件事：非字符串/空串当没给；大小写与空白归一（模型常写 `"Blue "`）；
    **不在调色板里的一律丢掉，不报错**。

    为什么是"丢掉"而不是"报错"：这条函数只服务生成侧（骨架里的 color/icon）。
    一个 `color: "深蓝"` 或 `color: "#1e40af"` 说明模型没照提示词写，但它产出的
    结构本身是好的——为了一个装饰字段把一次已经花掉的骨架调用整批判失败，
    代价方向完全错了。契约层（`Node.color` 的 Literal）仍然是硬拒绝，
    所以脚本与前端构造的数据不会绕过白名单。
    """
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    return key if key in NODE_COLORS else None


def normalize_icon(value: object) -> str | None:
    """模型给的 `icon` → 可落库的 emoji，或 `None`。

    三件事：折掉空白、按 `MAX_NODE_ICON_CHARS` 截断、**把"写成文字的图标"丢掉**。

    第三条不是洁癖。实测模型会写 `"icon": "放大镜"` 或者 `"用眼睛表示观察"`
    ——它照着自己的话写了一个**词**。这种值落到树上会变成节点标题前面挂着四个
    汉字，看起来像标题的一部分，而用户没有任何办法知道"那个字本来应该是个图标"。
    所以按 Unicode 类别判：只要含字母或数字（`L*` / `N*`，中日韩汉字也在 `Lo`
    里）就不是图标，丢掉。emoji 是 `So`（Symbol, other），可以过。

    不追求"严格的 emoji 白名单"：那要跟着 Unicode 版本更新一张属性表，为一个
    装饰字段不值得。长度 + 字母/数字这两条挡住的是真实出现的两种坏值。
    """
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    for char in text:
        if unicodedata.category(char)[0] in ("L", "N"):
            return None
    return text[:MAX_NODE_ICON_CHARS]


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
    """节点样式（v2 预留字段，界面对它只做透传保存）。

    `style.color` 与 `Node.color` **不是同一个东西**，两者的关系必须写清楚，
    否则下一个人会以为它们是"新旧两版"而随手改坏一处：

      - `NodeStyle.color` 是 `#RRGGBB`，来源是导入/将来的取色器，**界面不渲染**。
        它是"用户画的那个色"，逐像素还原用。
      - `Node.color` 是**调色板键**（`blue`/`green`/…），界面按它渲染，能跟着
        主题（暗色/亮色）自动适配。

    两个字段同时存在时 **`Node.color` 优先**：语义色是渲染意图，hex 是历史值。
    `Node.color` 为 `None` 时才回落到…也不回落——`style.color` 仍然不渲染，
    它只是被保留下来，免得用户点一次保存就把导入的颜色丢掉。
    """

    model_config = ConfigDict(extra="forbid")
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    icon: str | None = Field(default=None, max_length=40)
    bold: bool = False
    italic: bool = False
    shape: Literal["rounded", "rect", "pill"] = "rounded"


class RelationType(str, Enum):
    """跨节点关系的**旧四值**。

    这是这张图从 v1 起就有的取值集合，库里的老图、导入的老 JSON、前端历史类型
    全都按它写。它**不再是生成侧要写的字段名**（那件事现在是 `Relation.kind`），
    留着是因为"读得回旧数据"与"写得出新数据"是两件必须同时成立的事：删掉它等于
    让所有存量导图在下一次保存时 422，而用户什么都没改。
    """

    cause = "cause"        # A 导致 B
    ref = "ref"            # A 引用/参见 B
    conflict = "conflict"  # A 与 B 冲突
    depends = "depends"    # A 依赖 B


class RelationKind(str, Enum):
    """跨节点关系的语义种类——**规范名**，生成侧只写这一个字段。

    为什么在 `RelationType` 之外另立一套，而不是把它扩成十个值：旧四值是按
    "这句话怎么说"命名的（`cause` / `ref`），而图谱两段式要表达的是"两个概念在
    结构上是什么关系"（`part_of` / `refines` 在旧集合里根本无处安放）。两套混在
    一个枚举里，读代码的人永远分不清哪个值是给旧数据用的、哪个是新生成用的。

    **六个值**对应六种真的会出现在材料里的关系，每一种都有画法上的区别（见
    `frontend/src/canvas/MindRelation.vue` 的虚线样式与标签）：

      - `depends_on`：A 依赖 B（B 是 A 的前提）。有方向，箭头指向 B；
      - `causes`：A 导致 B（因果）。有方向，箭头指向 B；
      - `conflicts`：A 与 B 冲突/互斥。无方向语义，画成不带箭头的虚线；
      - `part_of`：A 是 B 的组成部分（A ⊂ B）。有方向，箭头指向 B；
      - `refines`：A 细化/具体化 B（A 是 B 的一个下位说法）。有方向，箭头指向 B；
      - `references`：A 引用/参见 B（材料里明说"见 X"）。有方向，箭头指向 B。

    为什么是 `Literal` 而不是自由字符串：颜色/线型要按它选，而自由字符串的意思
    是"每一个没见过的值都渲染成默认样式"——那六个值就白定义了。与 `EdgeKind`
    同一条理由。
    """

    depends_on = "depends_on"
    causes = "causes"
    conflicts = "conflicts"
    part_of = "part_of"
    refines = "refines"
    references = "references"


# 六个值的字符串集合。给"拿到的是任意 JSON，先判合法再构造"的那类调用方用
# （`agent.tools._relations_from`）：直接 `Relation(kind=...)` 撞上非法值时抛的是
# `ValidationError`，那会把"模型写错一个词"升级成整张图 500。判一次再构造，错的那条
# 丢掉、其余照收。派生自枚举而不是另写一份字面量——两处迟早分叉。
RELATION_KIND_VALUES = frozenset(item.value for item in RelationKind)


# 旧值 → 新值的唯一映射。放在模块级而不是 validator 里，是因为**三处**要读它：
# `Relation` 的同步器、导入层（老 JSON 只有 `type`）、以及测试里"每一种旧值都能
# 读得回来"的那条断言。写在 validator 内部就成了第二份口径。
LEGACY_TO_KIND: dict[RelationType, RelationKind] = {
    RelationType.cause: RelationKind.causes,
    RelationType.ref: RelationKind.references,
    RelationType.conflict: RelationKind.conflicts,
    RelationType.depends: RelationKind.depends_on,
}

# 关系理由的字数上限。300 与 `evidence` 同口径：`why` 是一句话（"B 是 A 的输入，
# 原文第 3 段把两者并列写出"），不是一段论证——超过 300 字说明模型在写解读而不是
# 在说这条边为什么成立。
MAX_RELATION_WHY_CHARS = 300


class Relation(BaseModel):
    """跨节点的关系线。来源与目标是节点 id，必须都在树里。

    **两个语义字段并存**，这是本轮改动的核心，得一句话说清谁管什么：

      - `kind` 是**规范名**，六个值见 `RelationKind`。生成路径只写它。
      - `type` 是**旧名**，四个值见 `RelationType`。它继续存在是为了读得回存量
        数据（库里的图、导入的老 JSON、前端历史类型），**不许**在新代码里当输入。

    两者由 `_sync_kind` 单向同步：只给 `type` 时按 `LEGACY_TO_KIND` 推出 `kind`；
    只给 `kind` 时 `type` **保持 None**，不做反向映射。

    为什么反向不映射：六个新值里有 `part_of` / `refines` 两个在旧四值里**没有对应**
    ——多对一的映射写出来只能靠猜（`part_of` 该叫 `ref` 还是 `depends`？），而猜错的
    后果是"画布按 kind 渲染、导出按 type 渲染"两条路给出不同的标签。所以规矩定成
    "`type` 是输入的回声，`kind` 是唯一的规范值"：渲染、去重、导出全部读 `kind`，
    `type` 只保证老数据读得回来、老前端读得到它认识的那四个值。
    """

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=100)
    # 规范名。`None` 只在"这是老数据、它只给了 type"时出现，由同步器补齐。
    kind: RelationKind | None = None
    # 旧名。**只反映输入给了什么**，不由 `kind` 反推（理由见类 docstring）。
    type: RelationType | None = None
    label: str | None = Field(default=None, max_length=100)
    # 这条边成立的**理由**（一句话）。与 `evidence` 分工不同：
    #
    #   - `why`：为什么把这两个节点连起来——读图的人不看材料也能判断这条线对不对；
    #   - `evidence`：材料里的原话（逐字），用来回查。
    #
    # 契约层允许 `None`（老数据没有它），**生成侧不许**：`agent.tools._relations_from`
    # 把没有 `why` 的边直接丢掉并记诊断。没有理由的边在画布上就是一根说不出来由的
    # 虚线，它带来的信息是负的。
    why: str | None = Field(default=None, max_length=MAX_RELATION_WHY_CHARS)
    evidence: str | None = Field(default=None, max_length=300)
    provenance: Literal["user", "ai", "import"] = "user"

    @field_validator("evidence", "why")
    @classmethod
    def blank_evidence_as_none(cls, value: str | None) -> str | None:
        return value if value and value.strip() else None

    @model_validator(mode="after")
    def _sync_kind(self) -> Relation:
        """把老数据的 `type` 升成 `kind`。**只有这一个方向**。

        三种输入都要能读：

          - 新数据只给 `kind`（生成路径）：`type` 留 None，没有旧消费者需要它；
          - 老数据只给 `type`（库里的图、导入的老 JSON）：`kind` 按 `LEGACY_TO_KIND`
            补上，画布因此能按新标签渲染老图；
          - 两个都给：`kind` 赢，`type` 原样留着。**不报错**——报错会让"模型多写了
            一个字段"变成整棵树 422，而这件事的正确处理是"以规范名为准"。
        """
        if self.kind is None:
            if self.type is None:
                raise ValueError("跨节点关系必须给出 kind（六个值之一）")
            self.kind = LEGACY_TO_KIND[self.type]
        return self


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


class Edge(BaseModel):
    """跨父子节点的附加关系：流程图里的条件跳转、汇合、回退。

    **为什么是「树 + 附加边」而不是扁平的 `nodes + edges`**：树这一层被每一处
    依赖——`_generate_deep` 的骨架与按支展开、`INITIAL_MAX_NODES` / `MAX_DEPTH`
    的限额、`node_expand`、`expandable` 三态、`child_counts` 与 `path` 索引链、
    前端的 `flatten()` 与 tidy 布局、三种导出。全推倒的代价远超收益，而表达能力
    并不缺：**主干顺序 = 父链**，**条件分支 = 子节点**，**汇合与回退 = 这条边**。

    三件事刻意不做，每一条都有具体后果：

      - **不要求同父**。连到另一条 subtree 上正是"汇合"的定义；加一条"必须
        同父"等于把这个字段能表达的东西全部禁掉，流程图又退化回树。
      - **不要求无环**。A→B→A 是流程图的核心需求（驳回、返工、重试）。上限只有
        "最长环 ≤ `MAX_EDGE_CYCLE_NODES`"，而且是 warn。
      - **不参与布局**。节点坐标仍由树算出（`frontend/src/canvas/layout/tidy.ts`），
        边只画连线。边参与布局的话，用户每加一条线整个画布都会重排，刚记住的
        位置就没了——这是"树 + 边"能成立的前提之一。
    """

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=100)
    # 边上的文字：「是」/「否」/「通过」/「驳回」。`None` 是"这条边没有文字"，
    # 不是空串——空串会让前端多一个"有标签但画出来什么都没有"的分支。
    label: str | None = Field(default=None, max_length=MAX_EDGE_LABEL_CHARS)
    # 语义类型，决定画法（见 `EDGE_KINDS`）。`None` 等于 `flow`：多数边就是普通
    # 的顺序箭头，逼调用方为每一条都声明 kind 只会得到一堆 `flow`。
    kind: EdgeKind | None = None

    @field_validator("label")
    @classmethod
    def blank_label_as_none(cls, value: str | None) -> str | None:
        """纯空白的标签等于没有标签（与 `Node.note` / `Node.detail` 同一条规矩）。"""
        return value if value and value.strip() else None


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=MAX_NODE_LABEL_CHARS)
    children: list[Node] = Field(default_factory=list, max_length=MAX_NODE_CHILDREN)
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
    # 这一支还能不能再展开。三态的判定表见模块顶部 `NODE_EXPANDABLE` 的注释：
    #
    #   - `yes`：还能展开——有子节点但没长完，或者本身就是叶子；
    #   - `no`：已经到底——模型明确说没有更细的内容，或已经到第 8 层；
    #   - `unknown`：不确定——**被额度截断的不算 `no`**，那是"没展开完"，不是
    #     "没得展开"；旧图没有这个字段时也是它。
    #
    # 缺省取 `unknown` 而不是 `yes`：库里那些 v1/v2 记录从来没被问过这件事，
    # 把没问过的记成"还能展开"会让界面替旧图许下一个它兑现不了的承诺。
    expandable: Literal["yes", "no", "unknown"] = "unknown"
    collapsed: bool = False
    # 视觉元素：语义色 + 图标。**两个都可选、缺省 None**，所以库里所有旧图
    # 不需要迁移脚本就能读进来（与 `note` / `detail` / `expandable` 同一条规矩：
    # 新字段一律有默认值）。
    #
    # `color` 只能是 `NODE_COLORS` 里的键。用 `Literal` 而不是 pattern：非法值
    # 会在契约层直接被拒，而不是被画成一个脏色。生成侧另有一层归一化
    # （`normalize_color`），模型写了个不认识的键会被丢掉而不是让整棵树失败。
    #
    # 为什么加在 `Node` 上而不是继续用 `style.color`：语义色是**渲染意图**，
    # 见 `NodeStyle` 的 docstring。
    color: NodeColor | None = None
    # 图标。目前是 emoji（零依赖、跨平台、`<span>` 直接渲染），不是图标库的 id：
    # 前端没有图标库（`package.json` 里没有 `lucide-vue-next` 之类），为一个
    # 纯装饰字段新增一个依赖不划算。将来真上了图标库，这个字段可以保持
    # "字符串"不变——那时它承载的就是库里的 id。
    icon: str | None = Field(default=None, max_length=MAX_NODE_ICON_CHARS)
    style: NodeStyle | None = None
    # 这个节点在**图里扮演什么**：普通步骤 / 判定 / 起点 / 终点。
    #
    # 为什么需要它：`Edge.kind` 已经有 `flow` / `branch` / `loop`——**边**有语义
    # 类型，节点侧却没有对偶。流程图里判定节点必须画菱形，否则「是/否」两个出口
    # 是从哪儿分出来的读不出来，而 `branch` 那条边已经默认它的起点是个判定。
    #
    # 三条不能走的替代路，每条都有一个具体后果：
    #
    #   - **不能复用 `NodeStyle.shape`**。那是导入侧的"用户画的那个形状"，逐像素
    #     还原用；`Node.color` 与 `NodeStyle.color` 的分工已经把这条线划过一次了
    #     ——语义不能塞进样式，否则换主题时形状语义会跟着漂。
    #   - **不能靠出边数让前端猜**。用户编辑到一半（只连了一条 `branch` 出边）
    #     会闪变，而"这个节点长什么样"是用户记住这张图的一部分。
    #   - **不能放进 `metadata`**（那是给来源的），也不能塞 `style`。
    #
    # 默认 `None` 是旧图零迁移的老规矩：库里那些从没被问过"你是什么角色"的节点
    # 一律当作普通步骤，行为与加这个字段之前逐字节相同。
    #
    # 六个取值 = 标准流程图的六个图框。加值域本身**不需要迁移**：`Literal` 只扩大
    # 可写集合，读旧数据那条路一个字节都没变（旧值全部仍在集合里，`None` 仍是
    # 普通步骤）。
    #
    #   - `step`     ：矩形，处理 / 步骤（`None` 与它同义）
    #   - `decision` ：菱形，判定（有两条以上出口）
    #   - `start`    ：胶囊，起点
    #   - `end`      ：胶囊，终点
    #   - `data`     ：平行四边形，**输入 / 输出**（读到了一个值、交出了一份材料）
    #   - `database` ：圆柱，**存储**（数据落在某处 / 从某处取出）
    #
    # 为什么只补这两个、不把文档 / 六边形 / 双竖线 / 梯形一起补：判定"画哪种框"
    # 的这件事必须由模型说，而它只在**图上真出现**时才说得准。输入输出与存储
    # 几乎每张流程图上都有（"从哪读、写到哪"），其余几种的出现频率低一个数量级，
    # 多说一句只会把 `role` 这一段变长、把 `decision` 的注意力摊薄——那一段的
    # 实测代价见 `tools._FLOW_BODY` 的注释（写在 system 里时一个 decision 都没标）。
    role: Literal["step", "decision", "start", "end", "data", "database"] | None = None
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=20)
    pos: Position | None = None

    @field_validator("label")
    @classmethod
    def fold_label(cls, value: str) -> str:
        folded = _fold(value)
        if not folded:
            raise ValueError("节点标题不能为空")
        return folded

    @field_validator("icon")
    @classmethod
    def blank_icon_as_none(cls, value: str | None) -> str | None:
        """纯空白的图标等于没有图标。

        与 `note` / `detail` 同一条理由：留一个空串会让前端多一个"有图标但画出来
        什么都没有"的分支（`<span>` 里塞一个空格），而 `None` 的语义是明确的。
        """
        return value if value and value.strip() else None

    @field_validator("note", "detail")
    @classmethod
    def blank_text_as_none(cls, value: str | None) -> str | None:
        """纯空白的正文等于没写。

        留一个空串会让前端多一个"有正文但显示为空"的分支（详情区显示一块空白，
        回退逻辑永远不触发），而 `null` 的语义是明确的"没有这段内容"。
        """
        return value if value and value.strip() else None


def _unique_source_ids(source_usage: Sequence[SourceUsage]) -> set[str]:
    """`source_usage` 的来源 ID 集合，顺带拒掉重复。

    重复的来源摘要不是"多写了一遍"：同一份材料在图上被引用时，`source_refs` 只能
    指向一个 `source_id`，两份摘要并存意味着"这条引用到底对应哪一份"没有答案。
    所以它在三种图型里都是硬拒。
    """
    ids = {source.source_id for source in source_usage}
    if len(ids) != len(source_usage):
        raise ValueError("来源摘要中的来源 ID 不能重复")
    return ids


def _scan_tree(
    root: Node, source_ids: set[str], seen: set[str] | None = None,
) -> tuple[set[str], bool]:
    """遍历一棵子树，返回 `(节点 id 集合, 是否有节点带坐标)`。

    四件事一次做完，因为它们必须在**同一次**遍历里、按同一个顺序判：id 唯一、
    节点总数 ≤ `MAX_TREE_NODES`、深度 ≤ `MAX_TREE_DEPTH`、`source_refs` 都指回
    `source_usage`。分成四趟会让错误顺序变，而错误顺序是用户看到的第一句话。

    `seen` 传入时复用同一份 id 空间——鱼骨图有六根骨、流程图有若干阶段，它们是
    并列的子树而不是一棵树，但**节点 id 必须全局唯一**：前端按 id 建索引，两根骨
    各有一个 `n1` 时点一下会跳到另一根骨上。节点总数也按全部子树合计算，因为
    画布承受的是整张图。

    深度仍按**每棵子树**从 1 数：一根骨挂在主骨上，但画布上它是从自己的根展开的
    一列，层数限制针对的正是那个"列"。
    """
    ids = seen if seen is not None else set()
    has_position = False
    stack: list[tuple[Node, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if node.id in ids:
            raise ValueError("节点 ID 必须唯一")
        ids.add(node.id)
        if node.pos is not None:
            has_position = True
        if len(ids) > MAX_TREE_NODES or depth > MAX_TREE_DEPTH:
            raise ValueError(
                f"导图最多 {MAX_TREE_NODES} 个节点，深度不超过 {MAX_TREE_DEPTH} 层")
        for ref in node.source_refs:
            if ref.source_id not in source_ids:
                raise ValueError(f"节点 {node.id} 引用的来源 {ref.source_id} 不在 source_usage 中")
        stack.extend((child, depth + 1) for child in node.children)
    return ids, has_position


def _validate_relations(relations: Sequence[Relation], node_ids: set[str]) -> None:
    """跨节点关系线的四条约束。三种图型共用：`relations` 落在 `ContentBase` 上。"""
    relation_ids: set[str] = set()
    # 去重键读 `kind`（规范名）而不是 `type`：`type` 只在老数据上有值，新生成的关系
    # 全是 None——按它去重时，同一对节点上的两条 `part_of` 会双双通过，画布上
    # 出现两条完全重叠的虚线。
    relation_keys: set[tuple[str, str, RelationKind | None]] = set()
    for relation in relations:
        if relation.id in relation_ids:
            raise ValueError("关系 ID 必须唯一")
        relation_ids.add(relation.id)
        if relation.source not in node_ids:
            raise ValueError(f"关系 {relation.id} 的起点节点 {relation.source} 不在导图中")
        if relation.target not in node_ids:
            raise ValueError(f"关系 {relation.id} 的终点节点 {relation.target} 不在导图中")
        if relation.source == relation.target:
            raise ValueError(f"关系 {relation.id} 的起点与终点不能是同一个节点")
        key = (relation.source, relation.target, relation.kind)
        if key in relation_keys:
            raise ValueError("同一对节点之间的同类关系不能重复添加")
        relation_keys.add(key)


def _validate_edges(
    edges: Sequence[Edge], node_ids: set[str], *, what: str = "节点",
    where: str = "导图中",
) -> None:
    """附加边的三条硬约束：条数上限、id 唯一、端点存在且不是自环。

    与 `relations` 的差别在**硬拒的边界**：条数用的是 `MAX_EDGES_LIMIT`（承载能力）
    而不是 `MAX_EDGES`（产品口径）——后者只报 warn，理由见 `MAX_EDGES` 的注释。

    `what` / `where` 只影响错误文案：树里端点是**节点**（"不在导图中"），流程的
    `FlowContent.edges` 端点是**阶段**，而 `FlowStage.edges` 端点是**该阶段内的
    节点**（"不在阶段 s_1 里"）。判据一模一样，所以只有一份实现；但把"阶段 s1 不在
    导图中"说成"节点 s1"，排查时会去树里找一个根本不在树里的 id。
    """
    if len(edges) > MAX_EDGES_LIMIT:
        raise ValueError(f"附加边最多 {MAX_EDGES_LIMIT} 条")
    edge_ids: set[str] = set()
    for edge in edges:
        if edge.id in edge_ids:
            raise ValueError(f"边 ID 必须唯一，重复的是 {edge.id}")
        edge_ids.add(edge.id)
        # dangling 在这里**硬拒**，与 EDGE_LIMIT / EDGE_CYCLE_TOO_LONG 那两个
        # 只报 warn 的码不同。理由不是严格，是渲染：前端按 id 找节点算端点，
        # 找不到就是一条 `source: undefined` 的边，Vue Flow 会抛错而不是跳过，
        # 整张画布白屏。所以"指向不存在的节点"永远不能进到落库/渲染的图里；
        # 生成路径会在构造之前先把这种边滤掉并报 `EDGE_DANGLING`（见
        # `app/mindmaps/edges.py::clean_edges`），两条路都不让脏边活下来。
        if edge.source not in node_ids:
            raise ValueError(f"边 {edge.id} 的起点{what} {edge.source} 不在{where}")
        if edge.target not in node_ids:
            raise ValueError(f"边 {edge.id} 的终点{what} {edge.target} 不在{where}")
        # 自环画不出来——它只会渲染成一个点，用户看不出那里有一条线，
        # 却会因为"边数 +1"而困惑。而"节点到自己"这件事在图上没有语义。
        if edge.source == edge.target:
            raise ValueError(f"边 {edge.id} 的起点与终点不能是同一个{what}（自环无法绘制）")


class ContentBase(BaseModel):
    """三种图型（思维导图 / 鱼骨图 / 流程图）共用的顶层字段。

    抽出来的只有"三套都会有、且语义一模一样"的五个：版本、标题、主题、跨节点
    关系、来源摘要。**`extra="forbid"` 也在这里**——它是三种图型能被同一个联合类型
    分派的前提（见文件末尾 `DiagramContent` 的注释），漏在任何一个子类上都会让
    判别退化成"谁先声明谁赢"。

    不抽的字段都是"某一种图型才有的顶层结构"：树有 `root` / `layout`，鱼骨有
    `problem` / `bones`，流程有 `stages` / `edges`。它们的差别不是命名，是**这张图
    按什么读**，硬塞进基类只会得到三个恒为 `None` 的字段。
    """

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[2] = 2
    title: str = Field(min_length=1, max_length=200)
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


class MindMapContent(ContentBase):
    # 思维导图的顶层结构：一棵 `Node` 树 + 排布 + 附加边。**tree 图型走它。**
    #
    # 说明写成注释而不是 docstring：docstring 会作为 `description` 进 openapi 与
    # `schema.d.ts`（`Edge` / `Relation` 就是这样），而下面这段是**迁移史**，不是
    # 给调用方读的契约语义——把它广播到前端的生成类型里只是噪音。
    #
    # `diagram_type` 在这里是**宽**的（`DiagramKind`，默认 `tree`），有意不收窄成
    # `Literal["tree"]`：库里与前端手里有一批 `diagram_type == "fishbone"` 但形状仍
    # 是一棵树的记录（生成链路还不产出鱼骨形状，阶段 2 才做），收窄它们会在下一次
    # 保存时 422——用户什么都没改，图却存不回去。判别由**形状**做，不由这个字段做，
    # 理由见 `DiagramContent`。

    root: Node
    layout: LayoutKind = LayoutKind.tree
    # 跨父子节点的附加边。**默认空列表**，旧图缺这个键时 Pydantic 直接补一个空
    # 列表——这就是"旧图零迁移"的全部实现，不需要改库、不需要停机、不需要
    # `schema_version` 升级（树 + 边的**树**那一半形状没变，读完写回时多出来的
    # `edges: []` 是无害的默认值）。
    edges: list[Edge] = Field(default_factory=list)
    # 这张图**是什么图型**。加它的理由是产物无法自证：一张鱼骨图与一张思维导图
    # 在树形状上可以长得一模一样（一级分支六条、下面挂要点），界面上唯一的区别
    # 只存在于生成它那一次调用用了哪份提示词——那个信息随调用一起消失了。
    #
    # 三处读者：
    #   1. 前端据此决定要不要摆「附加关系」区块（那个区块只对流程图有意义）；
    #   2. 导出（PUML 文件头注明图型），读的人不必从形状去猜；
    #   3. 排查"我要的鱼骨图怎么成了导图"时，那张图自己说得出来。
    #
    # 默认 `tree`：库里所有旧记录都没有这个键，Pydantic 的字段默认值把缺的补成
    # `tree`——**零迁移**，与 `edges` 同一条规矩（不改库、不升 `schema_version`）。
    # 而"旧图本来就是思维导图"这句是真的：它们全部由 `tree` 那条提示词生成。
    diagram_type: DiagramKind = "tree"
    # 一级分支**这一支讲什么**：`{一级分支节点 id: 8–12 字概括}`。
    #
    # 为什么它是**契约字段**、而不是"生成那一次顺手塞进 `ToolResult.data`"：`data`
    # 是一次运行的读数（`verify` / `input` / `material` 那几块都走它），它**不落库、
    # 也不进 `done` 帧的 `content`**。走那条路的话，用户刷新一次页面、或者从库里
    # 重新打开这张图，概括就没了——而"这一支有多少节点、讲的是什么"是**这张图的
    # 属性**，不是这一次运行的属性。让它每次重开都再调一次模型（实测 1.87 秒 / 5 组）
    # 是拿钱买一个本来就在手里的答案。
    #
    # 为什么不塞进 `Node` 的既有文本槽：那三个槽**都已经有主**。
    #   - `label` 是标题（概括与它并列写在一起，就再也说不清哪个是标题——这也正是
    #     它该挂在节点**旁边**而不是写进节点里的原因）；
    #   - `detail` 归生成侧的说明（逐节点写"这个节点讲什么"，一级分支自己那段也有）；
    #   - `note` 归用户（他在界面上敲的批注，重新生成一次就会被整段盖掉）。
    # `style` / `role` / `icon` / `pos` 各有语义，同样不能借用。
    #
    # 为什么是 `id → 文案` 而不是 `list[{id, label, summary}]`：键就是节点 id，重复
    # 在结构上不存在；多一层对象只会让渲染层每次重画都多解一层，而它要的只是
    # "按 id 查一句"。
    #
    # **不做键校验**（这一条是刻意的）：键指向一个已经不存在的节点（用户删了那一支、
    # 导入时改了 id、模型给了个对不上号的 id）时这里不报错。读者是渲染层，它按 id
    # 查不到就不画；而"删节点要顺手清理另一张表"要在编辑、撤销、`ops`、导入四条路上
    # 各写一遍，漏一条就是用户什么都没干却保存 422——与 `Node.collapsed` 那条判断
    # 同一个方向。
    branch_digests: Annotated[
        dict[str, str], Field(default_factory=dict, exclude_if=lambda value: not value),
    ]

    @field_validator("edges")
    @classmethod
    def dedupe_edges(cls, value: list[Edge]) -> list[Edge]:
        """去重的判据见模块级 `_dedupe_edges`；流程图那边共用同一份。"""
        return _dedupe_edges(value)

    @model_validator(mode="after")
    def validate_tree(self) -> MindMapContent:
        # 第二层：保存、读取和所有接收导图的接口共用同一关联约束。
        source_ids = _unique_source_ids(self.source_usage)
        node_ids, has_position = _scan_tree(self.root, source_ids)

        # 坐标只在手动布局下有意义：自动布局的图带着历史坐标，换布局时会被
        # 当成"用户想把这个节点放这里"，画出来是一团乱麻。
        if self.layout is LayoutKind.manual:
            if not has_position:
                raise ValueError("手动布局至少需要一个节点带坐标")
        elif has_position:
            raise ValueError("只有手动布局才允许保存节点坐标")

        _validate_relations(self.relations, node_ids)
        _validate_edges(self.edges, node_ids)
        return self


def _dedupe_edges(value: list[Edge]) -> list[Edge]:
    """按 `(source, target, label)` 去重，**保留第一条**。

    为什么去重键里有 `label`：判断节点的两个出口指向同一个目标是完全正常的
    流程（"是"和"否"都回到主流程），把它们当成重复边删掉，图上就少了一条
    分支说明。反过来，两个端点与文字都一样的边没有任何表达差异，留着只会让
    画布上叠一条看不出来的重线。

    为什么是"去重"而不是"报错"：重复边是**幂等**输入——模型重试一次、用户
    点两次"连线"都会产生它，而它不改变图的意思。为一条信息量为零的边让整张
    图 422，代价方向错了。真正需要硬拒的是 dangling 与自环（见 `_validate_edges`）。

    两种图型（树的 `edges` 与流程的阶段间跳转）共用这一份：它们的去重语义没有
    任何差别，分开写只会在某一天让两边对"什么算重复"给出不同答案。
    """
    unique: list[Edge] = []
    seen: set[tuple[str, str, str | None]] = set()
    for edge in value:
        key = (edge.source, edge.target, edge.label)
        if key in seen:
            continue
        seen.add(key)
        unique.append(edge)
    return unique


class FishboneBone(BaseModel):
    """鱼骨图上的一根骨：一个类别 + 一棵子树。

    **`root: Node` 而不是 `causes: list[Node]`**：`Node` 已经把 `children` /
    `detail` / `color` / `icon` / `source_refs` / `expandable` 全带上了，套一层
    `Node` 等于免费获得编辑、引用、展开这些能力——它们的实现全都只认 `Node`。
    `causes` 就是 `root.children`，多一层数组只会让"这条原因下面还能不能再展开"
    这件事换个地方回答。

    骨本身额外带 `color` / `icon`，而不是复用 `root.color`：主骨上那一行是**类别**
    （人 / 机 / 料），它属于图的骨架而不是内容节点，配色要能独立于第一个原因。
    """

    model_config = ConfigDict(extra="forbid")
    category: str = Field(min_length=1, max_length=MAX_FISHBONE_CATEGORY_CHARS)
    root: Node
    color: NodeColor | None = None
    icon: str | None = Field(default=None, max_length=MAX_NODE_ICON_CHARS)

    @field_validator("category")
    @classmethod
    def fold_category(cls, value: str) -> str:
        folded = _fold(value)
        if not folded:
            raise ValueError("骨的类别不能为空")
        return folded

    @field_validator("icon")
    @classmethod
    def blank_icon_as_none(cls, value: str | None) -> str | None:
        """纯空白的图标等于没有图标（与 `Node.icon` 同一条归一）。"""
        return value if value and value.strip() else None


class FishboneContent(ContentBase):
    """鱼骨图：主骨上的一句话（`problem`）+ 至多六根骨，每根骨挂一棵子树。

    与 `MindMapContent` 的三个差别都不是命名问题，是**这张图按什么读**：

      - 没有 `layout`。鱼的排布是几何定死的（主骨横贯、骨按类别固定上下交替），
        让用户选"横向还是纵向"只会得到一张画歪的鱼。前端按 `diagram_type` 分派
        （`canvas/layout/index.ts`），不读 `layout`。
      - `problem` 独立于 `title`。`title` 是文件名（"报销慢的原因分析"），
        `problem` 是主骨上那一句（"为什么报销要 11 天"）——后半句才是这张图的论点，
        而它会被画在鱼头里，长度与字号都受位置限制。
      - 原因挂在 `bone.root.children` 上，而不是挂在主骨上。鱼骨图的一级结构是
        **类别**，不是"原因 1 / 原因 2"。
    """

    diagram_type: Literal["fishbone"] = "fishbone"
    problem: str = Field(min_length=1, max_length=MAX_FISHBONE_PROBLEM_CHARS)
    # 根数硬上限、类别不重复。**空的 `bones` 合法**：一张只有主骨的草稿是"还没想
    # 清楚"，那是诊断层该说的事（阶段 2 报 warn），不该让用户存不下当前进度。
    bones: list[FishboneBone] = Field(default_factory=list, max_length=MAX_FISHBONE_BONES)

    @field_validator("problem")
    @classmethod
    def fold_problem(cls, value: str) -> str:
        folded = _fold(value)
        if not folded:
            raise ValueError("鱼骨图的主骨上必须有一句话")
        return folded

    @model_validator(mode="after")
    def validate_fishbone(self) -> FishboneContent:
        source_ids = _unique_source_ids(self.source_usage)
        # 六根骨是并列的子树，但节点 id 必须全局唯一（前端按 id 建索引）。所以
        # 一份 `seen` 穿到底，节点总数也按六棵子树合计算。
        node_ids: set[str] = set()
        categories: set[str] = set()
        for bone in self.bones:
            if bone.category in categories:
                raise ValueError(f"骨的类别不能重复：{bone.category}")
            categories.add(bone.category)
            _scan_tree(bone.root, source_ids, node_ids)
        _validate_relations(self.relations, node_ids)
        return self


class FlowStage(BaseModel):
    """流程里的一个阶段：一个 id + 一棵步骤树 + **阶段内部**的附加边。

    `root: Node` 而不是 `steps: list[Node]`：与 `FishboneBone.root` 同一条理由——
    `Node` 带的东西（`detail` / `source_refs` / `expandable` / 颜色图标）一样都不少，
    而第一层"步骤"与第 N 层"步骤"在数据里本来就该是同一个东西。`steps` 就是
    `root.children`。

    ## 两个数组、两个命名空间（这一节是这个结构最容易串的地方）

    模块里有**两处** `edges`，端点属于**两个互不相通的命名空间**：

      - `FlowContent.edges` —— **阶段之间**的跳转（提交 → 审查、付款 → 提交驳回）。
        端点必须是**阶段 id**（`stages[i].id`），拿一个真实存在的步骤 id 当端点会被
        硬拒（`test_edge_endpoints_are_stages_not_nodes` 钉着）。它画出来是画布上的
        那根**主轴**。
      - `FlowStage.edges`（就是本字段）—— **这个阶段内部**的边：判定节点的「是 / 否」
        两条出边、以及步骤之间的顺序跳转。端点必须是**该阶段自己的节点 id**，
        拿另一个阶段的节点、或者拿阶段 id，都会被硬拒。它画出来是阶段内部的一条
        **细线**。

    为什么必须分成两个数组（而不是给端点加 `stage:` / `node:` 前缀，把边都塞进
    `FlowContent.edges`）：命名空间靠**位置**隔离时，校验是"每条边的两个端点都在
    这个集合里"——一次集合查表，且它天然排除了"阶段内的边连到另一个阶段"这种错；
    前缀方案要把每个端点都解析一遍，还会给 id 引入一个转义问题（节点 id 允许任意
    字符，写 `node:x` 的节点 id 和"前缀表示法"就分不开了）。代价是消费者要遍历
    `stages[i].edges`——而它本来就要遍历 `stages[i].root`。

    为什么要有这个字段（阶段 1 契约的真实空洞）：`role="decision"` 的节点在阶段
    **内部**，而原来的 `FlowContent.edges` 端点只有阶段 → 模型标出来的判定节点，
    它的两条「是 / 否」出边**无处可写**。实测一份流程图 5 个判定节点、`branch_edges`
    恒为 0：不是模型没标，是契约没有位置。

    `id` 与节点的 id 是**两个命名空间**：阶段 id 只被 `FlowContent.edges` 的端点
    引用（那是阶段之间的跳转），节点 id 只被 `relations` 与这两个阶段内数组引用。
    不合并是因为合并之后"这条边连的是阶段还是步骤"要靠形状猜，而两者在画布上画
    出来的东西完全不同（一条主轴 vs 一条细线）。
    """

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    root: Node
    # 阶段内的边。**默认空列表**，旧记录缺这个键时 Pydantic 直接补一个空列表——
    # 与 `MindMapContent.edges` / `FlowContent.edges` 同一条"旧图零迁移"规矩：
    # 不改库、不升 `schema_version`，旧流程图读进来就是"阶段内没有附加边"的图，
    # 而它在加这个字段之前画出来正好也是那样。
    edges: list[Edge] = Field(default_factory=list)

    @field_validator("edges")
    @classmethod
    def dedupe_edges(cls, value: list[Edge]) -> list[Edge]:
        """去重的判据见模块级 `_dedupe_edges`；另外两处 `edges` 共用同一份。"""
        return _dedupe_edges(value)


class FlowContent(ContentBase):
    """流程图：按顺序排的阶段 + 阶段之间的跳转（含回退）。

    与 `MindMapContent` 的三个差别：

      - 没有单一的 `root`。流程的一级结构是**阶段**（提交 / 审查 / 付款），每一步
        还各自有内部的步骤树；把它们塞进一棵树的第 1、2 层，会让"这是阶段"与
        "这是阶段里的第一步"在数据里长得一样。
      - **这里这个 `edges` 只装阶段之间的跳转**，端点命名空间是**阶段 id**。这与
        `MindMapContent.edges` 刚好相反，也是同一个 `Edge` 类型在两处的唯一区别
        （见 `_validate_edges` 的 `what` / `where` 参数）。阶段**内部**的边（判定
        节点的两条出边、步骤之间的顺序跳转）在 `FlowStage.edges` 里，端点命名空间
        是那个阶段自己的节点 id——两处是**两个数组、两个命名空间**，见
        `FlowStage` 的 docstring。
      - 允许环，而且环是**核心需求**：驳回 / 返工 / 重试就是 `付款 → 提交`。
        `MAX_EDGE_CYCLE_NODES` 那条 warn 在流程里同样适用（见 `mindmaps/edges.py`）。
    """

    diagram_type: Literal["flowchart"] = "flowchart"
    # 顺序就是 `stages` 的顺序：流程图的"先后"是数组下标，不需要另一个 `order` 字段
    # ——两个来源说同一件事时，它们迟早会互相矛盾。
    #
    # `min_length=1` 不只是产品口径，也是**判别的必要条件**：`stages` 若可缺省，
    # 任何一份只有 `title` 的畸形请求体都会静默落成一个"空流程图"而不是 422。
    stages: list[FlowStage] = Field(min_length=1, max_length=MAX_FLOW_STAGES)
    edges: list[Edge] = Field(default_factory=list)

    @field_validator("edges")
    @classmethod
    def dedupe_edges(cls, value: list[Edge]) -> list[Edge]:
        """去重的判据见模块级 `_dedupe_edges`；树那边共用同一份。"""
        return _dedupe_edges(value)

    @model_validator(mode="after")
    def validate_flow(self) -> FlowContent:
        source_ids = _unique_source_ids(self.source_usage)
        stage_ids: set[str] = set()
        # 阶段内部的节点 id 仍然必须全局唯一（前端按 id 建索引，两个阶段各有一个
        # `step1` 时点一下会跳到另一个阶段里），节点总数也按全部阶段合计算。
        node_ids: set[str] = set()
        # 阶段内的边逐阶段校验（端点必须是**这个阶段自己的**节点），所以每轮要把
        # 这一阶段新收进来的 id 单独拿出来。`_scan_tree` 复用同一个 `seen`，差值
        # 就是它这一次真正加的那些——比再遍历一遍子树便宜，也不会与那趟遍历
        # 用两套口径算"哪些节点属于这个阶段"。
        stage_edges: list[tuple[str, list[Edge], set[str]]] = []
        for stage in self.stages:
            if stage.id in stage_ids:
                raise ValueError(f"阶段 ID 必须唯一，重复的是 {stage.id}")
            stage_ids.add(stage.id)
            before = set(node_ids)
            _scan_tree(stage.root, source_ids, node_ids)
            stage_edges.append((stage.id, stage.edges, node_ids - before))
        # 边 id 在**两处数组之间**也必须唯一。`_validate_edges` 各自按自己的数组判
        # 唯一性，两批合起来重名它看不见；而前端与树那边一样按 id 建索引，重名的
        # 两条边里有一条会永远画不出来（或者连到另一条的位置上）。
        edge_ids: set[str] = set()
        for edge in (*self.edges, *(item for _, edges, _ in stage_edges for item in edges)):
            if edge.id in edge_ids:
                raise ValueError(f"边 ID 必须唯一，重复的是 {edge.id}")
            edge_ids.add(edge.id)
        # 边端点查的是**阶段** id（见类注释），所以把它自己的命名空间传进去。
        _validate_edges(self.edges, stage_ids, what="阶段")
        for stage_id, edges, own_ids in stage_edges:
            # `where` 带上阶段名：一份流程图里有二十个"节点 x 不在…"，不说是哪个
            # 阶段时用户无法判断是自己连错了还是模型写错了。
            _validate_edges(edges, own_ids, where=f"阶段 {stage_id} 里")
        _validate_relations(self.relations, node_ids)
        return self


# 三种图型的顶层结构，用在"保存 / 读取"这一层（`SaveRequest.content` 与
# `MapRecord.content`）。
#
# **判别靠形状，不靠 `diagram_type`。** 这是这一层唯一的判断题，理由有四条：
#
#   1. **`diagram_type` 现在装不下这个职责。** 库里与前端手里有一批
#      `diagram_type == "fishbone"` 但形状仍是一棵树的记录——生成链路在阶段 2 之前
#      就是这么产出的（`agent/tools.py` 拿 `MindMapContent` 改一下 `diagram_type`
#      就算鱼骨图）。把 `TreeContent.diagram_type` 收窄成 `Literal["tree"]`，这批
#      图下一次保存就 422：用户什么都没改，图却存不回去。
#   2. **反过来也不行。** 让 `MindMapContent.diagram_type` 保持宽（`DiagramKind`），
#      判别键就不是单值，tagged union 不成立。
#   3. **形状判别是免费的。** `extra="forbid"` 在 `ContentBase` 上，加上每套各有
#      必填字段，三套天然互斥：树必有 `root`（且不许有 `problem` / `stages`），鱼骨
#      必有 `problem` + `bones`（且不许有 `root`），流程必有 `stages`。这不是巧合，
#      而是把 `extra="forbid"` 放进基类的原因。
#   4. **`diagram_type` 已有的职责够重了。** 它是**产物自述**：前端据此决定摆不摆
#      「附加关系」区块、导出的 PUML 文件头写哪一行、排查"我要的鱼骨图怎么成了导图"
#      时那张图自己说得出来。让一个"自述"同时承担"判别键"，等于往库里一个已经有三
#      类值的旧字段上再加一条它兑现不了的责任。
#
# `left_to_right` 而不是 smart：旧图全是树，排在第一位一次验证就命中；smart 模式
# 会把三套都试一遍，而树是可能上千节点的大对象。失败的两个成员是廉价的（缺必填
# 字段立刻失败），但顺序带来的**确定性**（同一份 JSON 永远解析成同一个成员）比
# smart 的"挑匹配字段最多的"更值得依赖。
#
# `TreeContent` 是 `MindMapContent` 的语义别名，不是子类：
#   - 子类会在 openapi 里多出一份与 `MindMapContent` 完全相同的 schema
#     （`TreeContent-Input` / `-Output`），而 `contract-check.ts` 与 `schema.d.ts`
#     要为这份重复买单，收益是零；
#   - `MindMapContent` 必须保持可构造（生成链路、导入、导出、上百条用例都在用它），
#     别名保证"同一份定义"；子类会让 `isinstance` 与 `model_json_schema` 分成两个；
#   - 语义上"思维导图的顶层契约"就叫 `MindMapContent`，这个名字已经在
#     `docs/CONTRACT_V2.md`、前端 `types.ts`、导入导出里用了上百处，不该为对称性
#     再造一个同形的名字。
#
# **旧图零迁移**的全部实现仍然是"字段默认值"：旧记录缺 `diagram_type` 由
# `MindMapContent` 补 `tree`，缺 `edges` 补空列表，而形状本来就是树——所以它落在
# 联合的第一个成员上，一个字节都不用改，也不需要给 `migrate_content()` 加任何分支。
TreeContent = MindMapContent

DiagramContent = Annotated[
    Union[MindMapContent, FishboneContent, FlowContent],
    Field(union_mode="left_to_right"),
]


# v1 记录里缺的字段。只补顶层：Node 的新字段由 Pydantic 默认值补齐，不需要
# 递归改写——那会让"迁移"变成"重写整棵树"，一旦某个字段语义变过就再难回头。
#
# `edges` **不在**这张表里：它有字段默认值（`default_factory=list`），旧图缺这个键
# 时 Pydantic 直接补空列表，不需要迁移脚本改写库里那份 JSON。这就是"旧图零迁移"。
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
    """保存一份导图，**顺带提交这一次保存里攒下的改动日志**。

    事件为什么不走独立端点（`POST /mindmaps/{id}/edits`）：第一次保存之前根本
    没有 `map_id`。要么发不出去，要么必须先 create 再补发，于是多出一种半截状态
    ——内容存了、事件没存——而统计从此只看见部分改动，且没有任何地方能察觉。
    放进同一个请求体，两者天然同成败：409 冲突时一起失败、重试就都补上。

    `events` 缺省是空列表而不是必填：一次没有改动的保存（改完再改回来、或只是
    重新点了一下保存）与上一版请求体逐字节相同。

    `content` 是**三种图型的联合**（`DiagramContent`），不是 `MindMapContent`：
    鱼骨图与流程图的顶层结构不同（`problem` + `bones` / `stages` + 阶段间跳转），
    用树那一套 `extra="forbid"` 装不下。判别靠形状，所以旧前端（只发树形状）的
    请求与加这个联合之前逐字节相同。
    """

    content: DiagramContent
    events: list[EditEventIn] = Field(default_factory=list, max_length=MAX_EVENTS_PER_SAVE)
    #: 前端这一次**没带上**多少条（超出 `MAX_EVENTS_PER_SAVE` 的部分留在前端队列里
    #: 等下次保存）。只用来记一条服务端 warning，不落表：往行为事件表里插一条假事件，
    #: 会让每一个统计口径的 `total` 都变脏。
    truncated: int = Field(default=0, ge=0)


class UpdateRequest(SaveRequest):
    version: int = Field(ge=1)


class MapRecord(BaseModel):
    """一条落库的导图。`content` 与 `SaveRequest.content` 是同一个联合类型——
    写进来的是什么形状，读出去就是什么形状（JSON 里 `diagram_type` 与形状都在）。"""

    id: str
    version: int
    content: DiagramContent
    updated_at: str
