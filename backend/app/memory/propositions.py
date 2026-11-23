"""命题（proposition）：跨事件的判断，并且**每个判断都带着它的出处**。

Coco 论文（arXiv:2611.13457）的第二级压缩。第一级是观测（`observations.py`：一次
具体动作，不做解释），第二级是命题：**周期性归纳出的、跨事件的判断**。

## 结构

论文给的形状，这里一一对应落地：

| 论文 | 这里 |
| --- | --- |
| `proposition` | `memory_items.text`（复用它的 UNIQUE `text_hash` 去重与 `vector` 列） |
| `reasoning` | `memory_propositions.reasoning` |
| `confidence` 1–10 | `memory_propositions.confidence` |
| `decay` 1–10 | `memory_propositions.decay`（1 = 短时上下文，10 = 持久偏好） |
| `observation_ids` | `memory_proposition_observations` 表的两列（**二部图的边**） |

**`decay` 不是时间衰减**。`store.HALF_LIFE_DAYS` 那一套管的是"这条记忆有多旧"，
按天连续下降；这里的 `decay` 是模型对这个判断**适用期**的分档，是命题的属性，
不随时间变。两者都会影响排序，但只有前者会随时间动——混起来写会让"这条为什么
排后面"无法回答。本模块的 `persistence()` 是后者到排序权重的唯一映射。

## 命题不是自由摘要：`observation_ids` 怎么保证真能回指

三段链，每一段都是主键：

```
命题 item_id
  → memory_proposition_observations.observation_id
  → memory_observations.refs            （{"run_ids": [...]} 或 {"event_ids": [...]}）
  → runs.run_id / edit_events.id        （原始事件行）
```

实现上有三道闸，缺一道这条链就会退化成"我总结了一段话，出处是'用户习惯'"：

  1. **模型只能选编号，不能编 id**。输入里的观测带 `[o1] [o2]` 前缀，编号与真实
     `observation_id` 的映射留在本地；模型输出 `"observation_ids": ["o1","o2"]`。
     输出里出现本次候选集之外的编号，**丢弃那一个编号**（不是丢掉整条命题——一条
     命题里写错一个编号，另外几个仍是真的）。
  2. **一条新命题至少两条观测**。只有一条观测支撑的"判断"是复述，不是归纳。
     这条只在**新建**时强制：挂到已有命题上的新观测可以只有一条（那条命题本来
     就有别的出处）。
  3. **`observations.resolve_refs()` 能当场核对**。给它一批观测 id，它回
     `{"run_ids": [...], "event_ids": [...]}`，可以直接拿去 `runs` / `edit_events`
     里查行。`tests/test_memory_proposition.py` 里那条回指用例就是这么验的。

## 修订：IDENTICAL / SIMILAR / UNRELATED

新命题进来时要先判它和已有的命题是什么关系。判定是**两级**，不是一次模型调用：

| 判定 | 判据 | 动作 | 代价 |
| --- | --- | --- | --- |
| IDENTICAL | `text_hash` 命中已有命题 | 只把新观测加进边表 | 零模型调用 |
| SIMILAR | 与最近邻命题的余弦 ≥ `SIMILAR_COSINE` | consolidation（一次模型调用） | 一次调用 |
| UNRELATED | 其余 | 新建一条 | 零模型调用 |

**为什么阈值取 0.60，而且宁可判高**：`store.MIN_COSINE` 的注释里有这份库上的实测
区间——同语境不同事 0.2–0.35，同一句话的两种说法 ≥0.45。命题比偏好短、比偏好凝练，
共享虚词的比例更高，余弦会虚高。而两侧的错判代价不对称：

  - 判成 UNRELATED（其实该合并）→ 库里多一条近义命题，占一个 top_k 名额。**看得见、
    可以删**。
  - 判成 SIMILAR（其实不该合并）→ 一次模型调用把两个不同的判断合成一条，旧的两条
    被 supersede。信息**不可恢复**（虽然旧行还在，但已经不在检索里）。

不可恢复贵得多，所以阈值往上取。0.60 是"明显是同一件事"的下界，不是"可能相关"的
下界——后者是 `store.MIN_COSINE` 管的事，两者不是同一个判据。

## consolidation：重写 / 合并 / 拆分，并且**同时重写支撑集**

模型返回的是"替换后的一组命题"，形状只有一种（`{"propositions": [...]}`），
`replaces` 列出它替换掉了本次候选里的哪几条。rewrite（一条替换一条）、merge
（一条替换多条）、split（多条替换一条）都是这个形状的特例——**只有一条代码路径**。

支撑集的合并**不由模型决定**：

```
新命题的观测集 = 模型给的编号 ∪ 它 replaces 的那几条命题的全部旧边 ∪ 新命题自己的观测
```

这条是代码强制的（`_apply_consolidation` 里的 `inherited_any`），所以"重写时观测集
怎么并"有一个确定的答案，可以被用例钉住。**摘要改写不能丢证据**：被替换的旧命题的边
一条都不能少，否则新命题的置信度就建立在更少的证据上，而它看起来与原来一样权威。

一个已知的粗粒度：**split 时两条新命题都继承全部旧边**（模型看不到旧观测的编号，
无法做更细的分配）。这是刻意的取舍——支撑集偏大可以审阅、可以删边；丢证据不可恢复。
要更细的分法，得把旧命题的观测摘要也喂给模型，那是比现在多一倍输入的改动。

## 修订是"新增 + supersede"，不是原地 UPDATE

旧命题 `status` 改成 `superseded`、`superseded_by` 指向新命题；新命题是一行新的
`memory_items` + 一行新的 `memory_propositions`。理由三条：

  1. `memory_items.text_hash` 是 UNIQUE 索引。原地改正文要么撞唯一约束，要么要
     先删后插——而"先删后插"在并发下会丢行。
  2. 用户要能**看到**系统改了什么（论文 Appendix A 的第一条 limitation 就是
     "用户想看到、改正、忘掉系统学到的东西"）。原地 UPDATE 之后旧判断消失，
     那条 limitation 就永远解不掉。
  3. 可回滚：读的地方一律过滤 `status='active'`，把 `superseded_by` 置空就能恢复
     那条旧命题——一次 UPDATE，不需要备份。

## 只做"记"，不做"学"

Coco 的三段是"观察 → 命题 → 从隐式反馈学"。本模块只做前两段。第三段（Coco-PE /
Coco-LoRA）不做的理由不是"以后再说"，而是**现在不成立**：库里 `memory_items` 0 条、
`edit_events` 里 `add` / `move` 事件 0 条，个位数的真实反馈都没有，学出来的东西
无法与"没学"区分。写在这里是为了让下一个人知道这是缺口，不是遗漏。
"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace as dc_replace
from typing import Any, Sequence

from app.generation.service import GenerationError, _config, chat_async
from app.knowledge import vector_cache
from app.memory import observations, store
from app.mindmaps import repository

logger = logging.getLogger(__name__)

TABLE = store.PROPOSITION_TABLE
EDGE_TABLE = store.EDGE_TABLE
KIND = store.PROPOSITION_KIND

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUSES: tuple[str, ...] = (STATUS_ACTIVE, STATUS_SUPERSEDED)

REVISION_CREATED = "created"
REVISION_IDENTICAL = "identical"
REVISION_CONSOLIDATED = "consolidated"

ACTION_IDENTICAL = "identical"
ACTION_SIMILAR = "similar"
ACTION_UNRELATED = "unrelated"
ACTIONS: tuple[str, ...] = (ACTION_IDENTICAL, ACTION_SIMILAR, ACTION_UNRELATED)

#: 与最近邻命题的余弦达到这个值，就认为"可能在说同一件事"，交给 consolidation。
#: 取 0.60 的完整论证见模块 docstring：错判代价不对称，宁可比"多留一条"更保守。
SIMILAR_COSINE = 0.60

#: consolidation 的候选条数。5 条 ≈ 一次调用里能读清的量；再多模型会在里面绕。
CANDIDATE_K = 5

#: 一次归纳至少要有几条待归纳观测。1 条观测无法"跨事件"，归纳出来的只会是复述。
MIN_INPUT_OBSERVATIONS = 2

#: 一次归纳最多喂几条观测。40 条 × 一行摘要 ≈ 2400 字，与 `summarize.MAX_RUNS`
#: 同量级。超过就分批（`induce()` 多调几次），而不是把窗口无限拉长。
MAX_INPUT_OBSERVATIONS = 40

#: 一条**新建**命题至少要几条观测支撑。见模块 docstring 的第二道闸。
MIN_SUPPORT_FOR_NEW = 2

#: 一次归纳最多接受几条命题。模型偶尔会把同一件事写成五条，退化的信号是条数。
MAX_PROPOSITIONS_OUT = 8

#: 归纳的输入里放几条已有命题的正文（避免重复）。20 × 100 字 ≈ 1400 字。
KNOWN_LIMIT = 20

#: 输出预算。8 条命题 × (正文 100 字 + 依据 100 字) ≈ 1200 token，留到 1600。
INDUCE_MAX_TOKENS = 1600

MAX_REASONING_CHARS = 300

_CONFIDENCE_RANGE = (1, 10)
_DECAY_RANGE = (1, 10)

#: 时间格式与 `mindmaps` / `runs` / `memory_items` / `edit_events` 完全一致。
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

#: 模型输出里的引用编号。只认 `o1` / `p2` 这种形如"字母+数字"的短编号。
_REF = re.compile(r"([op])\s*(\d+)", re.IGNORECASE)


# ---------------------------------------------------------------- 提示词

#: 归纳用的 system。**每一句都在挡一种具体的污染**，改之前先读它挡什么。
PROPOSITION_SYSTEM = """\
你在读一个人使用思维导图工具时的**动作记录**（他提了哪些要求、在图上保存了哪些修改）。
你的任务是把这些记录归纳成**关于这个人的判断**——不是复述他做过什么。

## 一条判断值不值得记：两个测试

**测试一**：把这条判断交给另一个完全不认识他的人，下次他提要求时用得上吗？
  - 用不上（「他生成过一张机器学习的图」）→ 那是记录，不是判断。**不要写。**
  - 用得上（「他要结构，但细节要自己补」）→ **写下来。**

**测试二**：换一个主题，这条判断还成立吗？
  - 不成立（「他要鱼骨图」只在某一个主题上出现一次）→ 那是这次的特征，不是这个人的。
  - 成立 → **写下来。**

## 硬约束

1. 一条一句话，不超过 100 字。不要编号、不要分点。
2. 每条判断必须由**至少两条**记录支撑。只有一条记录支持的，是复述，不是判断。
3. `observation_ids` 只能填下面给出的记录编号（o1、o2…），**不能写别的，不能编造**。
   程序会用这些编号去核对原始事件——编号是假的，这条判断整条作废。
4. `confidence` 1-10：证据有多硬。两条一致记录 ≈ 4-5；五条以上、跨不同主题 ≈ 7-8。
5. `decay` 1-10：这个判断有多持久。1 = 只适用于这一段上下文（这一次的要求方式），
   10 = 长期偏好（换主题、隔几天仍然成立）。
6. 不要把记录里的主题改写成「他关注 X」。那种判断对每次生成都"相关"，而它一个字的
   信息量都没有——它占掉的是别的判断的位置。
7. **产出 0 条是完全合法的结果。** 这些记录看不出这个人怎么做事，就返回空数组。

## 输出

只输出一个 JSON 对象，不要任何别的文字、不要代码围栏：

{"propositions": [{"proposition": "…", "reasoning": "…", "confidence": 7, "decay": 8,
                   "observation_ids": ["o1", "o3"]}]}
"""

#: consolidation 用的 system。输出形状只有一种（一组替换后的命题）。
REVISE_SYSTEM = """\
你在维护一个人的"判断库"。现在有一条新归纳出来的判断，库里已有若干看起来可能相关的判断。
你的任务是给出**替换后的一组判断**：合并、重写、拆分，或者都不做。

## 规则

1. `replaces` 只能填上面给出的已有判断编号（p1、p2…）。**留空 = 新增一条**，不替换任何已有判断。
2. `observation_ids` 只能填**新判断的**支撑记录编号（o1、o2…）。已有判断的支撑记录
   由程序自动继承，你不要写，也看不到它们的编号。
3. 新判断与某条已有判断说的是同一件事 → 输出**一条**合并后的判断，`replaces` 列出那几条。
4. 某条已有判断其实混了两件事 → 拆成**两条**，两条的 `replaces` 都写那一条。
5. 其实不是一回事 → 输出一条新增判断，`replaces` 留空。
6. 每条不超过 100 字；`confidence` / `decay` 1-10（含义与归纳时相同）。
7. 只在确实需要时合并。两条判断各自成立、只是主题相近，就保留两条。

## 输出

只输出一个 JSON 对象：

{"propositions": [{"proposition": "…", "reasoning": "…", "confidence": 7, "decay": 9,
                   "observation_ids": ["o1", "o2"], "replaces": ["p1"]}]}
"""


# ---------------------------------------------------------------- 形状

@dataclass(frozen=True)
class Proposal:
    """一条命题（库里的一行 + 它的边）。"""

    item_id: str
    text: str
    reasoning: str
    confidence: int
    decay: int
    status: str
    revision: str
    revisions: int
    created_at: str
    revised_at: str
    superseded_by: str | None = None
    observation_ids: tuple[str, ...] = ()

    @property
    def support(self) -> int:
        return len(self.observation_ids)


@dataclass(frozen=True)
class Draft:
    """模型归纳出的一条候选命题。`observation_ids` 已换成本地真实 id。"""

    proposition: str
    reasoning: str
    confidence: int
    decay: int
    observation_ids: tuple[str, ...]


@dataclass(frozen=True)
class Replacement:
    """consolidation 的输出：一条替换后的命题，`replaces` 已是真实 item_id。"""

    proposition: str
    reasoning: str
    confidence: int
    decay: int
    observation_ids: tuple[str, ...]
    replaces: tuple[str, ...]


@dataclass(frozen=True)
class Revision:
    """一条候选命题的修订结论。`degraded` 说明"本该合并但退回了新建"。"""

    action: str
    item_ids: tuple[str, ...] = ()
    superseded: tuple[str, ...] = ()
    attached: int = 0
    degraded: bool = False
    skipped: str | None = None


@dataclass(frozen=True)
class Induction:
    """一次归纳的结果。`status` 与 `memory_sessions.status` 同一套措辞。"""

    status: str
    observations: int = 0
    drafts: int = 0
    created: int = 0
    identical: int = 0
    consolidated: int = 0
    dropped: int = 0
    detail: str | None = None


@dataclass(frozen=True)
class PropHit:
    """语义路的一次命中，带算分过程（与 `store.Hit` 同一种可解释性）。"""

    proposal: Proposal
    cosine: float
    weight: float
    score: float

    @property
    def text(self) -> str:
        return self.proposal.text


def persistence(decay: int) -> float:
    """`decay` 1–10 → 排序权重 0.5–1.0（线性，端点明确）。

    只影响**排序**，不删除任何条目——与 `store.decay` 那条"衰减只影响排序"同一条
    原则。`decay=1` 的命题（只适用于这段上下文）权重减半，仍然在候选里；它在
    `search` 里的位置由余弦和这个权重一起决定，而不是被门槛挡掉。
    """
    low, high = _DECAY_RANGE
    value = max(low, min(high, int(decay)))
    return 0.5 + 0.5 * (value - low) / float(high - low)


def _connect() -> sqlite3.Connection:
    return repository.connect()


#: 一次读命题要的列。**列名写全，不用 `p.*`**：两张表都有 `created_at`，而
#: `sqlite3.Row` 按名字取同名列时返回的是**第一个**——`p.*` 在前就会拿到命题行的
#: 写入时刻，而不是记忆条目的。两者通常只差几毫秒，所以这个 bug 不会报错，只会让
#: "这条命题是什么时候写的"在某些地方悄悄换成另一个时间。
_FIELDS = (
    "p.item_id, p.reasoning, p.confidence, p.decay, p.status, p.superseded_by, "
    "p.revision, p.revisions, p.revised_at, i.text, i.created_at"
)


def _clamp(value: object, bounds: tuple[int, int], default: int) -> int:
    """把模型给的数字夹进值域。**不因为一个数字错就丢一条命题**。

    判断的正文对不对，比它自称几成把握更重要——数字夹一下还能用，整条丢掉就没了。
    """
    low, high = bounds
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.debug("命题的分数不是数字（%r），按 %d 处理", value, default)
        return default
    return max(low, min(high, number))


def _clean(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit]


def _row(row: sqlite3.Row) -> Proposal:
    return Proposal(
        item_id=row["item_id"],
        text=row["text"],
        reasoning=row["reasoning"] or "",
        confidence=row["confidence"],
        decay=row["decay"],
        status=row["status"],
        revision=row["revision"],
        revisions=row["revisions"],
        created_at=row["created_at"],
        revised_at=row["revised_at"],
        superseded_by=row["superseded_by"],
        observation_ids=(),
    )


# ---------------------------------------------------------------- 边（二部图）

def _add_edges(conn: sqlite3.Connection, item_id: str, ids: Sequence[str]) -> int:
    """把一批观测挂到这条命题上。返回新增边数。

    `PRIMARY KEY (item_id, observation_id)` 让这件事天然幂等：同一条观测被挂两次
    只算一条边，不会把支撑数灌成 2。用 `DO NOTHING` 而不是 `INSERT OR IGNORE`：
    后者连"observation_id 为空"这种写坏的行也一起吞掉。
    """
    created = 0
    for observation_id in ids:
        if not observation_id:
            continue
        result = conn.execute(
            f"INSERT INTO {EDGE_TABLE} (item_id, observation_id, added_at) "
            f"VALUES (?, ?, {_NOW}) ON CONFLICT(item_id, observation_id) DO NOTHING",
            (item_id, observation_id),
        )
        created += 1 if result.rowcount == 1 else 0
    return created


def observation_ids(item_id: str) -> tuple[str, ...]:
    """一条命题的出处。按挂上的时间排序，稳定。"""
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT observation_id FROM {EDGE_TABLE} WHERE item_id = ? "
            f"ORDER BY added_at, observation_id",
            (item_id,),
        ).fetchall()
    return tuple(row[0] for row in rows)


def edge_count() -> int:
    """边总数。**含孤儿边**：删掉的 `memory_items` 不会带走它的边（没有外键，理由
    见 `store._CREATE_EDGES`）。要清就调 `prune_orphans()`。"""
    with closing(_connect()) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {EDGE_TABLE}").fetchone()[0])


def _edges_in(conn: sqlite3.Connection, item_ids: Sequence[str]) -> set[str]:
    """一次查出若干条命题的全部边（consolidation 继承用）。"""
    if not item_ids:
        return set()
    marks = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"SELECT observation_id FROM {EDGE_TABLE} WHERE item_id IN ({marks})", list(item_ids),
    ).fetchall()
    return {row[0] for row in rows}


def prune_orphans() -> int:
    """删掉两类孤儿：没有 `memory_items` 的边、没有 `memory_propositions` 行的命题 item。

    后者是"`store.add` 成功、写命题行失败"那种半截状态留下的。它**不会**出现在检索里
    （语义路从 `memory_propositions` JOIN 进来），但它在记忆列表里看起来像一条命题，
    而它没有任何依据——所以要有地方能清掉它。
    """
    with closing(_connect()) as conn, conn:
        edges = conn.execute(
            f"DELETE FROM {EDGE_TABLE} WHERE item_id NOT IN "
            f"(SELECT id FROM {store.ITEM_TABLE})"
        ).rowcount
        items = conn.execute(
            f"DELETE FROM {store.ITEM_TABLE} WHERE kind = ? AND id NOT IN "
            f"(SELECT item_id FROM {TABLE})",
            (KIND,),
        ).rowcount
    return edges + items


# ---------------------------------------------------------------- 读

def prop(item_id: str) -> Proposal | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            f"SELECT {_FIELDS} FROM {TABLE} AS p "
            f"JOIN {store.ITEM_TABLE} AS i ON i.id = p.item_id WHERE p.item_id = ?",
            (item_id,),
        ).fetchone()
    if row is None:
        return None
    return dc_replace(_row(row), observation_ids=observation_ids(item_id))


def proposals(limit: int = 200, *, status: str = STATUS_ACTIVE) -> list[Proposal]:
    """按创建时间倒序。`status` 传 None 时连被 supersede 的一起返回（审计用）。"""
    sql = (f"SELECT {_FIELDS} FROM {TABLE} AS p "
           f"JOIN {store.ITEM_TABLE} AS i ON i.id = p.item_id")
    params: list[Any] = []
    if status:
        sql += " WHERE p.status = ?"
        params.append(status)
    sql += " ORDER BY i.created_at DESC, p.item_id LIMIT ?"
    params.append(max(1, limit))
    with closing(_connect()) as conn:
        rows = conn.execute(sql, params).fetchall()
    found = [_row(row) for row in rows]
    if not found:
        return []
    edges: dict[str, tuple[str, ...]] = {}
    with closing(_connect()) as conn:
        marks = ",".join("?" * len(found))
        for row in conn.execute(
            f"SELECT item_id, observation_id FROM {EDGE_TABLE} "
            f"WHERE item_id IN ({marks}) ORDER BY added_at, observation_id",
            [item.item_id for item in found],
        ):
            edges[row["item_id"]] = (*edges.get(row["item_id"], ()), row["observation_id"])
    return [dc_replace(item, observation_ids=edges.get(item.item_id, ())) for item in found]


def count(*, status: str | None = STATUS_ACTIVE) -> int:
    with closing(_connect()) as conn:
        if status:
            return int(conn.execute(
                f"SELECT COUNT(*) FROM {TABLE} WHERE status = ?", (status,)).fetchone()[0])
        return int(conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])


def _candidates(limit: int = store.MAX_SCAN) -> list[sqlite3.Row]:
    """参与语义路的命题行（含向量列）。"""
    with closing(_connect()) as conn:
        return conn.execute(
            f"SELECT {_FIELDS}, i.vector, i.dim, i.model "
            f"FROM {TABLE} AS p JOIN {store.ITEM_TABLE} AS i ON i.id = p.item_id "
            f"WHERE p.status = ? ORDER BY i.created_at DESC, p.item_id LIMIT ?",
            (STATUS_ACTIVE, max(1, limit)),
        ).fetchall()


def _cosine(row: sqlite3.Row, vector: list[float], model: str) -> float | None:
    """这一行与查询向量的余弦；跨模型/跨维度时返回 None。

    与 `store.recall` 同一条硬约束：换了 embedding 模型之后旧向量不在同一个空间里，
    直接点积会得到一个"看起来合理、实际是垃圾"的数——跳过，绝不截断对齐。
    """
    if row["vector"] is None or not row["dim"]:
        return None
    if row["model"] and row["model"] != model:
        return None
    item_vector = vector_cache.unpack(row["vector"], row["dim"])
    if not item_vector or len(item_vector) != len(vector):
        return None
    return sum(left * right for left, right in zip(item_vector, vector))


def embedding_model_name() -> str:
    """当前 embedding 模型名。抽出来是为了让用例能钉住"跨模型不比余弦"。"""
    from app.knowledge.embedding import model_name

    return model_name()


def search(
    query: str, *, top_k: int = store.TOP_K, min_cosine: float = store.MIN_COSINE,
) -> list[PropHit]:
    """按主题检索命题，**只查命题**（`kind='proposition'` 且在命题表里 active 的）。

    `score = cosine * persistence(decay)`。不用时间衰减：命题是归纳出来的判断，
    它的价值不该因为"三天前归纳的"而下降——那是观测那一层的事。这是刻意的分野：
    观测按时间路取（最近），命题按语义路取（相关 + 适用期）。

    门槛判的是**余弦**（与 `store.recall` 同一条理由：判 score 的话，任何固定阈值
    乘上一个会随 decay 变小的因子，等于让 decay=1 的命题在被创建的那一刻就注定
    不出现）。
    """
    if not query.strip():
        return []
    vector = store.query_vector(query)
    if vector is None:
        return []
    model = embedding_model_name()
    hits: list[PropHit] = []
    for row in _candidates():
        cosine = _cosine(row, vector, model)
        if cosine is None or cosine < min_cosine:
            continue
        weight = persistence(row["decay"])
        hits.append(PropHit(proposal=_row(row), cosine=cosine, weight=weight,
                            score=cosine * weight))
    hits.sort(key=lambda hit: hit.proposal.created_at, reverse=True)
    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits[:max(1, top_k)]


def _neighbours(vector: list[float], k: int) -> list[tuple[sqlite3.Row, float]]:
    """余弦最近的 k 条候选命题。"""
    model = embedding_model_name()
    scored: list[tuple[sqlite3.Row, float]] = []
    for row in _candidates():
        cosine = _cosine(row, vector, model)
        if cosine is not None:
            scored.append((row, cosine))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:max(1, k)]


# ---------------------------------------------------------------- 模型输入

def user_text(items: Sequence[observations.Observation], known: Sequence[str]) -> str:
    """归纳的 user 消息。**只放观测的一行摘要**，不放 payload 明细。

    明细（`payload.changes` 里那 20 条改动）会让模型去复述具体改了哪个字，而那正是
    "命题不是自由摘要"要禁止的形态。摘要里已经有"他改了 550 处 label"这个量，
    足够判断倾向；具体改了什么由 `observation_ids` 回指去查，不在提示词里。
    """
    stamp = store.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"现在是 {stamp}。",
        "",
        f"## 记录（{len(items)} 条，按时间顺序；编号是程序给的，输出时原样引用）",
    ]
    for index, item in enumerate(items, start=1):
        lines.append(f"[o{index}] {item.at[:16]} | {item.summary}")
    if known:
        lines += ["", "## 已有的判断（不要重复，也不要换一种说法再说一遍）"]
        lines.extend(f"- {text}" for text in known)
    return "\n".join(lines)


def revise_text(
    draft: Draft, candidates: Sequence[tuple[sqlite3.Row, float]],
    allowed_new: dict[str, str],
) -> str:
    """consolidation 的 user 消息。

    **候选命题的观测摘要不给**（只给条数）：给编号会让模型以为能给旧命题分配观测，
    而它做不到（那些编号不在它看到的 allowed 里）。条数是它判断"这条判断有多硬"
    需要的全部信息。

    新判断那一段只复述**它自己引用的编号**，不列全部候选：`allowed_new` 在一次归纳里
    最多有 40 个编号，全列出来会让模型以为"这条判断用到了全部 40 条记录"。
    """
    lines = ["## 已有的判断（可能与新的这条说的是同一件事）"]
    for index, (row, cosine) in enumerate(candidates, start=1):
        support = len(observation_ids(row["item_id"]))
        lines.append(
            f"[p{index}] {row['text']}"
            f"（confidence {row['confidence']}, decay {row['decay']}, "
            f"已有 {support} 条记录支撑，余弦 {cosine:.2f}）"
        )
        if row["reasoning"]:
            lines.append(f"     依据：{row['reasoning']}")
    lines += [
        "",
        "## 新的判断",
        f"{draft.proposition}（confidence {draft.confidence}, decay {draft.decay}）",
    ]
    if draft.reasoning:
        lines.append(f"依据：{draft.reasoning}")
    inverse = {value: key for key, value in allowed_new.items()}
    cited = [inverse[item] for item in draft.observation_ids if item in inverse]
    lines.append("它引用的记录：" + ("、".join(cited) if cited else "（无）"))
    return "\n".join(lines)


def _known_texts(limit: int = KNOWN_LIMIT) -> list[str]:
    return [item.text for item in proposals(limit)]


# ---------------------------------------------------------------- 解析

def _refs_from(raw: object, allowed: dict[str, str], prefix: str) -> list[str]:
    """把模型给的编号换成本地真实 id。**越界的编号逐个丢弃**，不整条丢。

    只认 `o1` / `p2` 这种形状（以及裸整数，模型偶尔会写 `[1, 3]`）。已经写成真实
    id 的也接受——但真实 id 是 32 位 hex，模型编不出来，所以这条主要是给手工构造的
    测试输入用的。
    """
    if not isinstance(raw, list):
        return []
    found: list[str] = []
    for entry in raw:
        key: str | None = None
        if isinstance(entry, bool):
            continue
        if isinstance(entry, int):
            key = f"{prefix}{entry}"
        elif isinstance(entry, str):
            text = entry.strip()
            match = _REF.fullmatch(text)
            if match:
                key = f"{match.group(1).lower()}{match.group(2)}"
            elif text in allowed:
                key = text
        if key is None:
            continue
        if key not in allowed:
            logger.debug("命题引用了候选集之外的编号 %r，丢弃这一个", key)
            continue
        resolved = allowed[key]
        if resolved not in found:
            found.append(resolved)
    return found


def parse_drafts(payload: object, allowed: dict[str, str]) -> tuple[list[Draft], int]:
    """模型输出 → (候选命题, 丢弃条数)。形状不对时返回空，不抛。

    `allowed` 是 `{"o1": observation_id, ...}`，与 `user_text` 里的编号一一对应。
    """
    raw: object = payload
    if isinstance(raw, dict):
        raw = raw.get("propositions")
        if raw is None:
            raw = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(raw, list):
        return [], 0
    drafts: list[Draft] = []
    dropped = 0
    for entry in raw:
        if not isinstance(entry, dict):
            dropped += 1
            continue
        text = _clean(entry.get("proposition") or entry.get("text"), store.MAX_ITEM_CHARS)
        if not text:
            dropped += 1
            continue
        ids = _refs_from(entry.get("observation_ids"), allowed, "o")
        drafts.append(Draft(
            proposition=text,
            reasoning=_clean(entry.get("reasoning"), MAX_REASONING_CHARS),
            confidence=_clamp(entry.get("confidence"), _CONFIDENCE_RANGE, 5),
            decay=_clamp(entry.get("decay"), _DECAY_RANGE, 5),
            observation_ids=tuple(ids),
        ))
        if len(drafts) >= MAX_PROPOSITIONS_OUT:
            break
    return drafts, dropped


def parse_replacements(
    payload: object, allowed_new: dict[str, str], allowed_old: dict[str, str],
) -> list[Replacement]:
    """consolidation 的输出 → 替换后的命题列表。形状不对时返回空（调用方降级）。"""
    raw: object = payload
    if isinstance(raw, dict):
        raw = raw.get("propositions")
        if raw is None:
            raw = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(raw, list):
        return []
    found: list[Replacement] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        text = _clean(entry.get("proposition") or entry.get("text"), store.MAX_ITEM_CHARS)
        if not text:
            continue
        found.append(Replacement(
            proposition=text,
            reasoning=_clean(entry.get("reasoning"), MAX_REASONING_CHARS),
            confidence=_clamp(entry.get("confidence"), _CONFIDENCE_RANGE, 5),
            decay=_clamp(entry.get("decay"), _DECAY_RANGE, 5),
            observation_ids=tuple(_refs_from(entry.get("observation_ids"), allowed_new, "o")),
            replaces=tuple(_refs_from(entry.get("replaces"), allowed_old, "p")),
        ))
        if len(found) >= MAX_PROPOSITIONS_OUT:
            break
    return found


# ---------------------------------------------------------------- 写

def _item_by_hash(digest: str) -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        return conn.execute(
            f"SELECT * FROM {store.ITEM_TABLE} WHERE text_hash = ?", (digest,),
        ).fetchone()


def _prop_upsert(
    conn: sqlite3.Connection, item_id: str, draft: Draft | Replacement, *,
    revision: str, run_id: str | None, session_id: str | None,
) -> None:
    """写/更新命题行。`revisions` 统计"这条被修订过几次"。

    upsert 而不是纯 INSERT：IDENTICAL 路径上这条命题已经存在，只需要刷新
    `revised_at` 与计数；而 upsert 让"重放一次归纳"是幂等的。
    """
    conn.execute(
        f"INSERT INTO {TABLE} (item_id, reasoning, confidence, decay, status, "
        f"superseded_by, revision, revisions, source_run_id, session_id, "
        f"created_at, revised_at) "
        f"VALUES (?, ?, ?, ?, ?, NULL, ?, 0, ?, ?, {_NOW}, {_NOW}) "
        f"ON CONFLICT(item_id) DO UPDATE SET "
        f"reasoning = CASE WHEN excluded.reasoning <> '' THEN excluded.reasoning "
        f"                   ELSE {TABLE}.reasoning END, "
        f"confidence = excluded.confidence, decay = excluded.decay, "
        f"revision = excluded.revision, revisions = {TABLE}.revisions + 1, "
        f"revised_at = {_NOW}",
        (item_id, draft.reasoning, draft.confidence, draft.decay, STATUS_ACTIVE,
         revision, run_id, session_id),
    )


def _promote(item_id: str) -> bool:
    """把一条"总结阶段产出的、还没有出处的判断"升级成命题。

    只在 `source='summary'` 时调用：那种行是系统推断的，现在有了观测支撑，档位从
    "推断出的偏好"变成"有出处的命题"——正文与向量都不动，只是多了一层证据。
    用户显式写下的（`user_explicit`）**不升级**：那条比他自己的推断更权威，让它
    出现在命题列表里会让人分不清"我说的"与"系统归纳的"。
    """
    with closing(_connect()) as conn, conn:
        result = conn.execute(
            f"UPDATE {store.ITEM_TABLE} SET kind = ? WHERE id = ? AND kind <> ?",
            (KIND, item_id, KIND),
        )
    return result.rowcount == 1


def _attach(draft: Draft, item_id: str, *, revision: str, run_id: str | None,
            session_id: str | None) -> Revision:
    """IDENTICAL：只把新观测加进它的边，正文与向量一个字都不动。

    这是论文里"IDENTICAL → 只把新观测加进它的 observation_ids"的直译。不重写正文
    的理由：正文相同的两条判断不该因为第二次被归纳出来而换一次措辞——那会让"这条
    判断是什么时候定下来的"变得没有答案。
    """
    with closing(_connect()) as conn, conn:
        row = conn.execute(f"SELECT 1 FROM {TABLE} WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            _prop_upsert(conn, item_id, draft, revision=revision,
                         run_id=run_id, session_id=session_id)
        else:
            conn.execute(
                f"UPDATE {TABLE} SET revisions = revisions + 1, revised_at = {_NOW} "
                f"WHERE item_id = ?", (item_id,))
        attached = _add_edges(conn, item_id, draft.observation_ids)
    logger.info("命题命中 IDENTICAL：%s（新增 %d 条观测）", item_id[:8], attached)
    return Revision(action=ACTION_IDENTICAL, item_ids=(item_id,), attached=attached)


def _create(draft: Draft, *, run_id: str | None, session_id: str | None) -> Revision:
    """UNRELATED：新建一条。**新建要求至少两条观测**（一条支撑的是复述）。"""
    if len(draft.observation_ids) < MIN_SUPPORT_FOR_NEW:
        logger.debug("丢弃一条只有 %d 条观测支撑的新命题：%s",
                     len(draft.observation_ids), draft.proposition[:40])
        return Revision(action=ACTION_UNRELATED, skipped="support")
    added = store.add(draft.proposition, KIND, source=store.SOURCE_SUMMARY,
                      source_run_id=run_id, session_id=session_id)
    try:
        with closing(_connect()) as conn, conn:
            _prop_upsert(conn, added.item.id, draft, revision=REVISION_CREATED,
                         run_id=run_id, session_id=session_id)
            _add_edges(conn, added.item.id, draft.observation_ids)
    except sqlite3.Error:
        # `store.add` 已经提交了一条 `kind='proposition'` 的 item，而命题行没写进去：
        # 那会变成一条"没有依据的命题"。把刚建的那条撤掉（命中去重时 `created=False`，
        # 那种情况不走到这里），免得它污染检索与列表。
        logger.warning("命题行没写进去，撤回刚建的记忆条目", exc_info=True)
        if added.created:
            store.delete(added.item.id)
        raise
    return Revision(action=ACTION_UNRELATED, item_ids=(added.item.id,))


def _apply_consolidation(
    replacements: Sequence[Replacement], *, candidates: Sequence[sqlite3.Row],
    draft: Draft, run_id: str | None, session_id: str | None,
) -> Revision:
    """把一组替换后的命题落库：新行 + supersede 旧行 + 继承旧边。

    落库顺序刻意如此：**先建完所有新命题，再统一标记旧命题**。反过来的话，中途失败
    会留下"旧的已经下线、新的一条都没有"的空洞，那比"多一条重复"糟得多。
    """
    candidate_ids = {row["item_id"] for row in candidates}
    replaced: list[str] = []
    for replacement in replacements:
        replaced.extend(item for item in replacement.replaces if item in candidate_ids)
    replaced = list(dict.fromkeys(replaced))

    created: list[str] = []
    with closing(_connect()) as conn:
        inherited = _edges_in(conn, replaced)
    for replacement in replacements:
        added = store.add(replacement.proposition, KIND, source=store.SOURCE_SUMMARY,
                          source_run_id=run_id, session_id=session_id)
        merged = list(dict.fromkeys([
            *replacement.observation_ids,
            *draft.observation_ids,
            *sorted(inherited),
        ]))
        with closing(_connect()) as conn, conn:
            _prop_upsert(conn, added.item.id, replacement, revision=REVISION_CONSOLIDATED,
                         run_id=run_id, session_id=session_id)
            _add_edges(conn, added.item.id, merged)
        created.append(added.item.id)

    if replaced and created:
        # 全部指向**最后一条**新命题：split 时旧命题被拆成两条，需要一个确定的
        # "它变成了什么"。取最后一条是任意的，但固定（两条新的都在 `created` 里，
        # 审计时能全部找到）。不做成一对多是因为 `superseded_by` 是单值，而把它
        # 改成边表会为这一条审计信息多建一张表。
        with closing(_connect()) as conn, conn:
            marks = ",".join("?" * len(replaced))
            conn.execute(
                f"UPDATE {TABLE} SET status = ?, superseded_by = ?, revised_at = {_NOW} "
                f"WHERE item_id IN ({marks}) AND status = ?",
                (STATUS_SUPERSEDED, created[-1], *replaced, STATUS_ACTIVE),
            )
    logger.info("命题 consolidation：替换 %d 条 → 新建 %d 条", len(replaced), len(created))
    if not created:
        return Revision(action=ACTION_SIMILAR, degraded=True, skipped="no_replacement")
    return Revision(action=ACTION_SIMILAR, item_ids=tuple(created),
                    superseded=tuple(replaced), attached=len(inherited))


async def _consolidate_async(draft: Draft, candidates: Sequence[tuple[sqlite3.Row, float]],
                             allowed_new: dict[str, str]) -> object:
    return await chat_async(
        REVISE_SYSTEM,
        revise_text(draft, candidates, allowed_new),
        max_tokens=INDUCE_MAX_TOKENS,
    )


def _degrade(draft: Draft, candidates: Sequence[tuple[sqlite3.Row, float]], *,
             run_id: str | None, session_id: str | None) -> Revision:
    """SIMILAR 但 consolidation 没成功时：把观测挂到**最近的那个候选**上，正文不动。

    为什么不退回 `_create`（第一版是那么写的，用例把它打回来了）：新建要求至少两条
    观测，而 SIMILAR 场景下新观测常常只有一条——其余的出处已经在旧命题上了。退回
    新建会被 `MIN_SUPPORT_FOR_NEW` 丢掉，于是这条观测在 `mark_consumed` 之后再也
    看不到。那正是"不丢证据"要禁止的结果，而它伪装成了一次保守的降级。

    挂到最近邻的依据是判定本身：余弦已经 ≥ `SIMILAR_COSINE`，我们认为它们在说同一
    件事。模型只是没给出合并后的措辞，那不构成"这条观测与它无关"的理由。
    `degraded=True` 让调用方知道这次没有走完整的 consolidation。
    """
    best = candidates[0][0]["item_id"]
    attached = _attach(draft, best, revision=REVISION_CONSOLIDATED,
                       run_id=run_id, session_id=session_id)
    return Revision(action=ACTION_SIMILAR, item_ids=attached.item_ids,
                    attached=attached.attached, degraded=True)


def _consolidate(draft: Draft, candidates: Sequence[tuple[sqlite3.Row, float]],
                 allowed_new: dict[str, str], *, run_id: str | None,
                 session_id: str | None) -> Revision:
    """SIMILAR：一次模型调用决定重写/合并/拆分。**任何一步失败都不丢证据**。

    降级方向见 `_degrade`：挂到最近邻，而不是凭空新建一条。
    """
    allowed_old = {f"p{index}": row["item_id"]
                   for index, (row, _) in enumerate(candidates, start=1)}
    try:
        payload = asyncio.run(_consolidate_async(draft, candidates, allowed_new))
    except GenerationError as exc:
        logger.warning("命题 consolidation 的模型调用失败，退回挂到最近邻：%s",
                       str(exc)[:120])
        return _degrade(draft, candidates, run_id=run_id, session_id=session_id)
    except Exception:  # noqa: BLE001 — 一次修订失败不该让整批归纳失败
        logger.warning("命题 consolidation 异常，退回挂到最近邻", exc_info=True)
        return _degrade(draft, candidates, run_id=run_id, session_id=session_id)
    replacements = parse_replacements(payload, allowed_new, allowed_old)
    if not replacements:
        logger.warning("命题 consolidation 的输出不可用，退回挂到最近邻")
        return _degrade(draft, candidates, run_id=run_id, session_id=session_id)
    try:
        return _apply_consolidation(
            replacements, candidates=[row for row, _ in candidates], draft=draft,
            run_id=run_id, session_id=session_id,
        )
    except sqlite3.Error:
        logger.warning("命题 consolidation 落库失败，退回挂到最近邻", exc_info=True)
        return _degrade(draft, candidates, run_id=run_id, session_id=session_id)


def revise(draft: Draft, *, allowed: dict[str, str], run_id: str | None = None,
           session_id: str | None = None) -> Revision:
    """一条候选命题的三态修订。返回它做了什么（与三态一一对应）。

    `allowed` 是本次归纳的 `{"o1": observation_id}`，透传给 consolidation——模型在
    那一轮里只能引用本次的编号，那条约束就是靠它兑现的。
    """
    digest = vector_cache.text_hash(draft.proposition)
    hit = _item_by_hash(digest)
    if hit is not None:
        if hit["kind"] != KIND:
            if hit["source"] != store.SOURCE_SUMMARY:
                # 与一条用户显式写下的偏好撞文本。那条更权威（它不需要出处也是真的），
                # 这里不建命题、也不改它——把观测挂上去会让人以为它是系统归纳的。
                logger.info("命题与用户显式写下的偏好重复，跳过：%s", draft.proposition[:40])
                return Revision(action=ACTION_IDENTICAL, skipped="explicit")
            _promote(hit["id"])
        return _attach(draft, hit["id"], revision=REVISION_IDENTICAL,
                       run_id=run_id, session_id=session_id)

    vector = store.query_vector(draft.proposition)
    candidates = _neighbours(vector, CANDIDATE_K) if vector is not None else []
    if candidates and candidates[0][1] >= SIMILAR_COSINE:
        return _consolidate(draft, candidates, dict(allowed), run_id=run_id,
                            session_id=session_id)
    return _create(draft, run_id=run_id, session_id=session_id)


# ---------------------------------------------------------------- 归纳入口

def _llm_ready() -> bool:
    """模型没配置就别开始，也**不消耗**观测。

    与 `summarize._llm_ready` 同一条判定（共用 `_config()` 抛错这一个出口）。区别在
    消耗：那边不写 `memory_sessions` 行，这边不 `mark_consumed`——未配置是预期状态
    （本机开发、CI、演示），不是"这批观测没用"。
    """
    try:
        _config()
    except GenerationError:
        return False
    return True


async def _induce_async(items: Sequence[observations.Observation],
                        known: Sequence[str]) -> object:
    return await chat_async(
        PROPOSITION_SYSTEM, user_text(items, known), max_tokens=INDUCE_MAX_TOKENS,
    )


def induce(*, limit: int = MAX_INPUT_OBSERVATIONS, run_id: str | None = None,
           session_id: str | None = None) -> Induction:
    """把一批还没被归纳过的观测压成命题。同步、可在线程里跑、**失败不抛**。

    为什么同步：与 `summarize.run_session` 同一个理由——它要在线程里跑，线程里没有
    运行中的事件循环，自己 `asyncio.run` 一个就够了；顺带的好处是用例与脚本可以
    直接调，不必起 loop。

    **"这次归纳出 0 条"与"失败"分开**：0 条是合法结论（观测看了，看不出判断），
    它照样把观测标记成已消费——否则同一批最旧的观测会在每次归纳里占住窗口，把新
    观测永远挡在外面。失败（模型挂、输出坏）**不**标记，下一次重试，代价是一次
    调用；不重试的代价是这段证据永远进不了命题，那更贵。

    `limit` 是一次喂给模型的观测条数上限。库里积压超过它时调用方循环调几次——每次
    都会消费掉一批，所以循环一定会结束。
    """
    if not _llm_ready():
        logger.debug("命题归纳跳过：模型未配置")
        return Induction(status="skipped", detail="模型未配置")
    items = observations.unconsumed(limit)
    if len(items) < MIN_INPUT_OBSERVATIONS:
        return Induction(
            status="empty", observations=len(items),
            detail=f"待归纳的观测只有 {len(items)} 条"
                   f"（要 {MIN_INPUT_OBSERVATIONS} 条才够跨事件）",
        )

    allowed = {f"o{index}": item.id for index, item in enumerate(items, start=1)}
    try:
        payload = asyncio.run(_induce_async(items, _known_texts()))
    except GenerationError as exc:
        detail = f"模型调用失败：{exc}"
        logger.warning("命题归纳失败：%s", detail[:160])
        return Induction(status="failed", observations=len(items), detail=detail)
    except Exception as exc:  # noqa: BLE001 — 后台线程里抛出去没有谁接得住
        detail = f"归纳过程异常：{type(exc).__name__}"
        logger.warning("命题归纳异常", exc_info=True)
        return Induction(status="failed", observations=len(items), detail=detail)

    drafts, dropped = parse_drafts(payload, allowed)
    created = identical = consolidated = skipped = 0
    for draft in drafts:
        try:
            revision = revise(draft, allowed=allowed, run_id=run_id, session_id=session_id)
        except Exception:  # noqa: BLE001 — 一条坏命题不该让整批丢
            logger.warning("一条命题没写进去", exc_info=True)
            skipped += 1
            continue
        if revision.skipped:
            skipped += 1
            continue
        if revision.action == ACTION_UNRELATED:
            created += len(revision.item_ids)
        elif revision.action == ACTION_SIMILAR:
            consolidated += len(revision.item_ids)
        else:
            identical += 1

    consumed = observations.mark_consumed(item.id for item in items)
    logger.info(
        "命题归纳：%d 条观测 → %d 条候选（丢弃 %d）；新建 %d、合并 %d、命中 %d；"
        "已消费 %d 条观测",
        len(items), len(drafts), dropped, created, consolidated, identical, consumed,
    )
    return Induction(
        status="done" if drafts else "empty",
        observations=len(items), drafts=len(drafts), created=created,
        identical=identical, consolidated=consolidated, dropped=dropped + skipped,
        detail=None if drafts else "这批观测没归纳出判断（合法结论）",
    )


__all__ = [
    "ACTION_IDENTICAL",
    "ACTION_SIMILAR",
    "ACTION_UNRELATED",
    "ACTIONS",
    "CANDIDATE_K",
    "EDGE_TABLE",
    "INDUCE_MAX_TOKENS",
    "KIND",
    "KNOWN_LIMIT",
    "MAX_INPUT_OBSERVATIONS",
    "MAX_PROPOSITIONS_OUT",
    "MIN_INPUT_OBSERVATIONS",
    "MIN_SUPPORT_FOR_NEW",
    "PROPOSITION_SYSTEM",
    "REVISE_SYSTEM",
    "REVISION_CONSOLIDATED",
    "REVISION_CREATED",
    "REVISION_IDENTICAL",
    "SIMILAR_COSINE",
    "STATUSES",
    "STATUS_ACTIVE",
    "STATUS_SUPERSEDED",
    "TABLE",
    "Draft",
    "Induction",
    "PropHit",
    "Proposal",
    "Replacement",
    "Revision",
    "count",
    "edge_count",
    "embedding_model_name",
    "induce",
    "observation_ids",
    "parse_drafts",
    "parse_replacements",
    "persistence",
    "prop",
    "proposals",
    "prune_orphans",
    "revise",
    "revise_text",
    "search",
    "user_text",
]
