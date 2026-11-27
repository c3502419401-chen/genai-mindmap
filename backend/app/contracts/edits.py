"""编辑日志的契约：一条改动长什么样，统计结果长什么样。

放在 `contracts/` 而不是 `mindmaps/edits.py`，是为了避开一个环：`SaveRequest`
（`contracts/mindmap.py`）要把事件放进请求体，而 `mindmaps/repository.py` 又要
import `contracts.mindmap`。事件模型留在仓储层的话，`contracts → mindmaps.edits →
mindmaps.repository → contracts` 就成环了，只能靠函数内 import 打补丁——那是症状，
不是解法。

几处刻意的取舍（数字怎么读全靠它们，条数故意不写死——每加一种 `kind` 都会变）：

  - **`before` / `after` 是摘要，不是内容。** `label` 存全文（≤200 字，统计要算
    长度），`detail` / `note` 只存 `"1800字"`。一份 2000 字的正文进日志表，它就
    变成了第二个 `mindmaps.content`；而"从 1800 字改成 700 字"本身就是情报。
  - **`field` 只在 `update` / `style` 上出现。** 这条规则让统计能自校验：
    `sum(by_field) == by_kind["update"] + by_kind["style"]`。新增与删除改的其实是
    父节点的 `children`，但把它们也标成 `children` 会让那条恒等式失效，而
    `by_kind` 已经把"加了多少、删了多少"说清楚了。
  - **`depth` 是节点层数（根=1）**，`remove` 事件填删除前所在层。`removed_depth`
    只对 `subtree_size = 1` 的零散删除分组："删掉的集中在第 3 层"就是"初次生成
    挖得太深"的证据；成规模的删除另走 `removed_subtree`，因为一整支被删时根所在
    的层并不代表"这一层被嫌弃"。
  - **`subtree_size` 是"这条 remove 一共删掉几个节点"（含自己）。** 一条事件代表
    一整棵被删的子树，不是每个后代各一条——否则 depth 2…5 各留几条，看起来"每层
    都在被删"，真实动作却只有一个。加列之前的老数据是 `NULL`（当时没记），统计把
    它们单列成 `removed_subtree["size_unknown"]`，**不**当成 1。
  - **`kind="generate"` 是"一次生成落到了画布上"，不是改动。** 每次生成完成记一条，
    `node_count` 是这次落进画布的那棵树有几个节点——**在生成完成那一刻定死**，不是
    保存时数：用户改完之后树上的节点数已经变了，那数出来的是"改后的规模"。
    它就是统计的分母：`SUM(node_count) WHERE kind='generate'`。
    `change_count` 曾经承担过"这次接受带来几处改动"，现在不用了——"这次生成有没有
    被改"只要看同一个 `run_id` 下有没有改动事件就够了，不需要前端算好塞进来。
  - **`kind="repair"` 是"规则批量整理"，不是"谁改的"。** 一键修复（`POST
    /optimization/repair`）把整棵树替换掉，diff 出来的是 add / remove / move /
    update 那些动作——**但这些事件的 `kind` 一律记成 `repair`**，`actor` 仍是
    `user`（扳机是用户按的，见 `docs/OPTIMIZATION-REPAIR.md` §7）。
    于是 `by_kind` 少掉"这次修复删了几个、搬了几个"的分桶，换来的是一个干净的
    事实：**用户回看"我改过什么"时，不会看到自己没做过的改动。**
    动作粒度没有丢——`field` / `before` / `after` / `depth` / `subtree_size` 都还在
    事件行上，只是不能再按 `kind` 分组读。`repair` 是**一次模型调用都没有**的动作，
    所以它不能进 `agent` 桶（那会让"模型改的"这个分母失去意义），也不该混进
    `user` 那份"人手动改了什么"的账。
  - **`kind="accept"` 保留在字面量里，但已不再写入。** 它是"应用候选"那个动作的
    遗迹：候选层删掉之后没有"应用"这一步，新数据一律是 `generate`。留着它是因为
    库里已有几千行历史（`by_kind` 要能读到它们），而老前端还会发——拒收会让一次
    真实保存失败，代价方向错了。
  - **时间有两个。** `at` 是前端说的事件时刻（可能漂移：用户改系统时间、页面开了
    很久），`recorded_at` 是入库时刻（永远可信）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: 单次保存最多接受多少条事件。**与前端 `EDIT_EVENT_LIMIT` 同一个数**：前端先分批，
#: 用户就不用为"一次删掉整棵树"收一条 422；这里仍然校验，因为契约是最终裁判。
#: 超出部分**不是丢弃**：前端把它们留在队列里等下一次保存。
MAX_EVENTS_PER_SAVE = 500

#: 单条摘要的长度上限。`label` / `title` ≤200，`"第2位"` / `"A → B（是）"` 更短，
#: 400 是给"标题全文 + 少量装饰"留的余量。超了直接拒：那是形状错了，不是内容长。
MAX_SUMMARY_CHARS = 400

#: 与前端 `mindmap/edit-log.ts` 的三个联合类型逐字对应。写成 `Literal` 而不是 `str`：
#: 它们会进 OpenAPI，前端镜像那边是同一份字面量联合，两端对齐靠契约。
#:
#: `generate` 与其余几种并列：它不是"改了什么"，而是"本来可以改却没改"的那个分母。
#: `repair` 也不是"哪一处被改了"，而是一次**批量整理**——它替换掉 diff 出来那批
#: 事件的 `kind`（动作粒度仍在事件行上，见文件头）。
#: `accept` 是 `generate` 的前身（候选层的"应用到画布"），只读不写，见文件头。
EditActor = Literal["user", "agent"]
EditKind = Literal["add", "remove", "update", "move", "edge", "style", "accept", "generate",
                   "repair"]
EditField = Literal["label", "title", "detail", "note", "color", "icon"]


class EditEventIn(BaseModel):
    """一条改动（`POST/PUT /mindmaps` 请求体里的一项）。

    `extra="forbid"`：字段名漂移（比如前端写成 `nodeId`）必须当场 422。静默丢掉
    一条事件比拒收整次保存更糟——统计会少一块，而没人知道少的是哪一块。
    """

    model_config = ConfigDict(extra="forbid")

    actor: EditActor
    kind: EditKind
    #: 节点 id。`edge` 事件填边的**源**节点，`title` 事件没有节点。
    node_id: str | None = Field(default=None, max_length=100)
    field: EditField | None = None
    before: str | None = Field(default=None, max_length=MAX_SUMMARY_CHARS)
    after: str | None = Field(default=None, max_length=MAX_SUMMARY_CHARS)
    #: 层数，根是 1。上限给到 64 而不是 `MAX_TREE_DEPTH`：日志是历史记录，一张按
    #: 旧契约存的深树被删掉时，拒收会让整次保存失败，而它已经是事实。
    depth: int | None = Field(default=None, ge=1, le=64)
    #: 这条 `remove` 一共删掉几个节点（含自己）。`1` = 零散删除，`12` = 整支一起删。
    #:
    #: 下限是 1（"删掉 0 个节点"没有意义）；**不设上限**，理由与 `depth` 的 64 相同：
    #: 一张按旧契约存的、节点数超过现上限的深树被删掉时，拒收会让整次保存失败，而
    #: 那已经是事实。少一个上界比丢一次真实保存可接受得多。
    subtree_size: int | None = Field(default=None, ge=1)
    #: 这次 `accept` 带来的改动条数，`0` = 一字未改。只有 `kind="accept"` 有值。
    #:
    #: **新数据不再写它**（候选层已删，见文件头）。保留列与字段是为了历史行可读：
    #: 老行上它有值，新行上是 `NULL`，两者在统计里都不参与运算。
    #:
    #: 不进 `after`：`after` 的语义是"截断后的文本摘要"，把计数塞进去会让
    #: `CAST(after AS INTEGER)` 对每一条文字摘要静默返回 0——一个看起来合理、实际
    #: 全错的聚合，比报错糟得多。下限 0，同样不设上限（它是个实测计数，不是用户输入）。
    change_count: int | None = Field(default=None, ge=0)
    #: 这次生成落进画布的那棵树有几个节点。只有 `kind="generate"` 有值。
    #:
    #: **在生成完成那一刻定死**：那一刻用户还没开始改，`node_count` 说的就是"模型
    #: 交给你多少东西"。保存时才数会数到改后的树——一次"删掉一半再保存"会把分母
    #: 算成改后的规模，比例永久偏高。
    #:
    #: 不设下限：树至少有一个根，但把 `0` 判成非法只会在某个边界分支上让一次真实
    #: 保存失败，而"这次生成是空的"本身也是一条值得记的事实。
    node_count: int | None = Field(default=None, ge=0)
    #: 这次改动动的是哪个 run 的产物。只有"应用候选"那一步对得上，手工编辑留空。
    run_id: str | None = Field(default=None, max_length=100)
    #: 事件发生的时刻（前端 `new Date().toISOString()`）。**可为空**：拿不到合法
    #: ISO 时退回入库时刻，而不是拒收——少一个时间戳比少一条改动可接受得多。
    at: str | None = None

    @field_validator("at")
    @classmethod
    def _valid_iso_or_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return text


class EditEventRow(EditEventIn):
    """落库后的一行：载荷 + 自增 id + 服务端写入时刻。"""

    id: int
    map_id: str
    at: str
    recorded_at: str


class LabelShrunk(BaseModel):
    """标题被改短的统计，**只统计 `actor="user"` 且 `kind != "repair"`**。

    过滤 actor 的理由：`agent` 的改动是"模型自己又改了一遍"（生成直接落画布时那批
    改动记在 `agent` 名下），它不反映用户的偏好。混进来时 `count` 会比用户的真实
    条数多（实测 B 图 3 条 user 的缩短被算成 4 条，多出来的那条是 agent 的 13→10
    字），而"用户的缩短偏好"正是这个数字要回答的东西。

    过滤 `kind` 的理由：一键修复的 `actor` 也是 `user`（扳机是用户按的），而
    `SINGLE_CHILD` 那条修法**是改文字**——"外层 / 内层"两层合成一个标题，必然比
    外层原来那个短。不过滤它，"规则把两层标题拼成一个"就会被读成"用户嫌标题太长"，
    而那条规则本身已经把标题改短过一次：拿它去论证"生成侧该写短一点"是循环论证。

    两条过滤各挡一样东西：`actor` 挡模型，`kind` 挡规则。

    `avg_before` / `avg_after` 是**字数**（码点），只有 `count` 为 0 时才是 `None`。
    两个平均值的差（18.3 → 9.1）就是"缩短了多少"，比只看 count 有信息量得多：
    从 18 缩到 9 是"太长了"，从 18 缩到 17 只是"改了个字"。
    """

    count: int = 0
    avg_before: float | None = None
    avg_after: float | None = None


class EditSummary(BaseModel):
    """`GET /mindmaps/{id}/edits/summary` 的响应。

    两条恒等式让数字可以先自校验再读结论：

      - `sum(by_actor) == total`；
      - `sum(by_field) == by_kind["update"] + by_kind["style"]`。

    对不上就说明有写入路径绕过了契约，而不是"统计有点怪"。

    第二条**不会**因为加了 `kind="repair"` 而失效：`repair` 事件里改文字的确实带
    `field`，但 `by_field` 只统计 `kind in ('update','style')` 的行（与 `label_shrunk`
    排除 `repair` 同一条理由：那不是人写的）。所以 `sum(by_kind) == total` 与
    `sum(by_field) == update + style` 两条都照旧是等式，`by_kind` 只是多了一个桶。

    改动量与"本来可以不改"的分母分列四组，全部是原始量（比例让读的人自己除）：
    分母 `generated_nodes`、分子 `changed_events`、以及"这次生成一处都没被改"的
    `untouched_runs`。三者必须能对上：`untouched_runs <= generated_runs`，而
    `changed_events == 0` 时 `untouched_runs == generated_runs`。
    """

    total: int
    #: 最近一条事件的**入库**时刻（`recorded_at`，不是前端给的 `at`）；空表是 `None`。
    #:
    #: 它是这张表的死表探测器：`total=0` 与"这张图没人改过"长得一样，而
    #: `last_event_at=null` 说的是另一件事——**从来没有任何事件进过这张表**。
    last_event_at: str | None = None
    by_actor: dict[str, int]
    by_kind: dict[str, int]
    by_field: dict[str, int]
    label_shrunk: LabelShrunk
    #: 删除的节点按层分布（根=1）。**只含零散删除**（`subtree_size = 1`）。键在 JSON
    #: 里是字符串，前端声明为 `Record<string, number>`。
    removed_depth: dict[int, int]
    #: 删除规模直方图：`{"size_1": 5, "size_12": 1, "size_unknown": 2}`。
    #:
    #: 键带 `size_` 前缀而不是裸数字：它和 `size_unknown` 是同一个轴上的两组投影，
    #: 混排在同一层字典里，裸键 `"unknown"` 与 `"1"` 会读成两个不相干的东西。
    #: `size_unknown` 是加列之前的老数据（`subtree_size` 为 NULL），它们**不进**
    #: `removed_depth`——否则"每层都在被删"那次污染还在。
    removed_subtree: dict[str, int]
    #: 生成次数：`kind='generate'` 的行数。
    generated_runs: int = 0
    #: **分母**：`SUM(node_count) WHERE kind='generate'`。即"模型一共交给用户多少个
    #: 节点"。用户删掉一半再保存，这数不变——它说的是模型给了什么。
    generated_nodes: int = 0
    #: **分子**：`COUNT(*) WHERE kind IN ('update','remove','add','move','style','repair')`。
    #: 两点都由"分子"这个身份决定，不是随手列的：
    #:
    #:  - `edge` 不进：分母数的是节点，把"加了一条关系"算进节点改动会让比例失去意义；
    #:  - `repair` **进**：一键修复确实改了节点（删、搬、改文字），不算进来的话，
    #:    一次"模型给了 30 个节点、规则删掉 12 个"的生成会被读成"一字未改"。它与
    #:    `untouched_runs` 的集合必须逐字相同，否则会出现"既不算改动、也不算一字未改"
    #:    的黑洞，见 `app/mindmaps/edits.py::summary` 里那段注释。
    changed_events: int = 0
    #: 那个 `run_id` 下**一条改动事件都没有**的 generate 行数——"生成完一字未改"。
    #:
    #: 这是整张表最贵的一个数：没有它，10 张图里 9 张没被改、1 张改了 10 处，统计
    #: 只会读到"标题被普遍改短"，然后去压生成侧的上限，而那 9 张本来合适的图在表里
    #: 根本没出现过，偏差方向永远朝"生成质量更差"。
    untouched_runs: int = 0


class EditHealth(BaseModel):
    """跨图的日志链路健康检查（`GET /mindmaps/edits/health`）。

    判据只有一句：**"保存了但没有任何事件"在正常情况下不该发生**。每次生成完成都记
    一条 `generate`（连一处没改也记），所以"昨日有保存、昨日零事件"只能是链路断了
    ——前端在发、后端在丢，或者事件根本没进请求体。实测有过 40 分钟这个状态（8000
    跑旧代码），而当时没有任何告警：单图的 `total=0` 看着和"没人改过"一模一样。

    窗口是 **UTC 昨日整天**（`[window_start, window_end)`）：库里所有时间戳都是
    `...Z`，按本地日切会让边界那几个小时归错天，而这里要判的只是"有没有发生过"，
    不值得为它引入一处时区配置。
    """

    window_start: str
    window_end: str
    #: 昨日有编辑事件的图数（`COUNT(DISTINCT map_id)`）。
    edited_maps: int
    #: 昨日保存过的图数。`mindmaps` 只留最后一次保存时刻，所以这是**下界**：
    #: 一张昨天存过、今天又存过的图不会出现在昨天的窗里。下界只会漏报，不会误报。
    saved_maps: int
    #: `saved_maps > 0 and edited_maps == 0`：链路可能是断的。
    chain_suspect: bool
    #: 一句话结论，界面可直接显示。
    detail: str


__all__ = [
    "EditActor",
    "EditEventIn",
    "EditEventRow",
    "EditField",
    "EditHealth",
    "EditKind",
    "EditSummary",
    "LabelShrunk",
    "MAX_EVENTS_PER_SAVE",
    "MAX_SUMMARY_CHARS",
]
