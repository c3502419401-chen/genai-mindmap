"""记忆条目：一张表、一套向量存法、一条带时间衰减的检索。

**这张表和 `user_prefs` 的分工**（一混就会重演"我明明写了它却没用"）：

  - `user_prefs`：**用户显式写的**。A/B 类是参数（图型、拆解方式、联网开关），
    必须精确，正确位置是请求层；C 类 `note.*` 是自由文本，条数是个位数，全塞进
    prompt 就够——它的 docstring 里"不做检索"那段论证在 8 条这个规模上是对的。
  - `memory_items`：**系统从历史会话里推断的**。写入源是总结管线，条数会从个位数
    长到几百上千。到了那个规模，全塞进 prompt 不可能，检索才成立。

所以这里**不接管** `note.*`，也不读它。手写的是显式意图、自动提炼的是推断，
两者在提示词里各占一段（显式的在前），这是刻意的：用户写下"标题别超过 8 个字"
之后，它必须永远生效，不能被一次"检索到大概相关的三条"挤掉。

**为什么复用 `text-embedding-v3` 与 `vector_cache` 的存取**：

  - 向量维度（1024 float32）与打包方式与 `chunk_vectors` 逐字节相同——同一份
    `pack` / `unpack`，不复制第二份。两处各写一份的结果已经出现过一次：换了
    字节序、一处的旧向量全读不出来，而报错只有一个沉默的 `None`。
  - 查询文本的向量走 `vector_cache`（按文本哈希缓存）。同一个 `goal` 反复生成
    不会重复付费；而这条缓存本来就存在，不需要为记忆另建一张。
  - 条目自己的向量**不进** `chunk_vectors`：那张表是纯缓存（丢了只会变慢），
    而记忆的向量是这条记忆的一部分（丢了这条记忆就再也检索不到）。混进去的后果
    是某天 `prune` 清缓存时把记忆的语义路一起清掉。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, get_args
from uuid import uuid4

from app.knowledge import vector_cache
from app.knowledge.embedding import (
    EmbeddingError,
    EmbeddingNotConfigured,
    configured as embedding_configured,
    embed_texts,
    model_name as embedding_model,
)
from app.mindmaps import repository

logger = logging.getLogger(__name__)

ITEM_TABLE = "memory_items"
SESSION_TABLE = "memory_sessions"
#: 观测（`observations.py`）与命题（`propositions.py`）的三张表。**DDL 留在这里**：
#: 这个模块是记忆 schema 的唯一所有者，四张表的建表语句读一处就够。另外两个模块
#: 只从常量拿表名，所以 `store` 不反向 import 它们——既没有环，也不需要把 DDL 拆成
#: 第三个"只放 schema"的文件。
OBSERVATION_TABLE = "memory_observations"
PROPOSITION_TABLE = "memory_propositions"
EDGE_TABLE = "memory_proposition_observations"

# ---------------------------------------------------------------- 值域与上限

#: 一条记忆是什么。三档的区别只有一个：它约束的是**表达方式**、**事实**，还是
#: **做事习惯**。分档不是为了给检索用（检索只看向量），是为了让人看列表时能扫，
#: 以及给将来的"按档设阈值"留一个不需要改表结构的入口。
#:
#:   - `preference` 表达方式上的取舍：标题去掉人名、节点不超过 8 个字
#:   - `fact`       关于这个人的稳定事实：他做电类课程、习惯中文术语
#:   - `pattern`    做事习惯：先要鱼骨图再补细节、喜欢先给结论
MemoryKind = Literal["preference", "fact", "pattern"]
KINDS: tuple[str, ...] = get_args(MemoryKind)

#: 命题（proposition）的档位。**刻意不放进 `KINDS`**，三条理由：
#:
#:   1. `KINDS` 是"关于这个人的判断属于哪一档"，是给人看的分类；命题是**有出处的
#:      判断**，它与三档不是平行关系——一条命题内部分类成 preference/fact/pattern
#:      都成立。把它塞进 `KINDS` 会让"这条是偏好还是命题"变成一个必须二选一的问题。
#:   2. `POST /memory` 用 `KINDS` 做校验（`router.add_memory`）：命题**不能**手工写，
#:      它必须带 `observation_ids`。放进 `KINDS` 就等于开了一个"手工写命题但没有任何
#:      观测支撑"的口子，那正是这套设计要禁止的形态。
#:   3. `summarize._parse_items` 对不认识的档位兜底成 `preference`。命题若在
#:      `KINDS` 里，会话总结的模型偶尔会在它自己的输出里写 `kind="proposition"`，
#:      于是产出一条没有边的"命题"——没有出处，检索时与真命题混在一起。
PROPOSITION_KIND = "proposition"

#: `add()` 接受的档位：三档 + 命题。分开定义是因为**内部写入方**（命题管线）需要
#: 写命题，而**外部写入方**（HTTP 手工添加）不该能写——校验用哪个由调用方决定。
WRITABLE_KINDS: tuple[str, ...] = (*KINDS, PROPOSITION_KIND)

#: 这条记忆是怎么来的。`summary` = 会话结束时的模型提炼；`user_explicit` = 用户
#: 手工加的。与 `user_prefs.source` 同一套措辞（`user_explicit`），但**不共用
#: 那张表**：这里是"可检索的推断/声明"，那里是"永远全塞的显式参数"。
MemorySource = Literal["summary", "user_explicit"]
SOURCES: tuple[str, ...] = get_args(MemorySource)

SOURCE_SUMMARY = "summary"
SOURCE_USER_EXPLICIT = "user_explicit"

#: 一条记忆的字数上限。与总结 prompt 里那条硬约束一致（"一条一句，不超过 100 字"），
#: 入库时再兜一次：prompt 是约束，不是保证。
#:
#: 超了**截断**而不是拒绝。一句被截断的偏好仍然多半是有用的（"标题去掉人名与并列
#: 项，节点不超过 8 个字"截到 100 字还是这句话），而拒绝的代价是这条事实永久丢失
#: ——同一个会话只会被总结一次，丢了不会再有第二次机会。
MAX_ITEM_CHARS = 100

#: 一次检索最多读多少行。全表扫在"几百上千条"这个规模上是毫秒级（1000 行 × 4 KB
#: = 4 MB 顺序读），但病态增长（一天几十次会话、跑一年）时会变成几十 MB。
#:
#: 超过这一行数时按 `created_at DESC` 截断——**这是有偏的**，极旧的记忆这次不参与
#: 检索。偏差写在这里而不是让它静默发生：一条 warning 会说明"这次只看了最近的
#: N 条"。真要让它无偏，该做的是向量索引（sqlite-vec / 外置），不是把这个数调大。
MAX_SCAN = 5000

#: 检索返回几条。取 6 而不是 5 或 8：
#:
#:   - 5 太少：同一条偏好的不同侧面常常占两条（"标题去掉人名"与"标题别带并列项"
#:     会被分开总结），5 条里挤掉一条就少一个侧面；
#:   - 8 太多：一条最多 100 字，8 条 = 800 字 ≈ 550 token，比 `note.*` 整段
#:     （500 字）还长，而第 7、8 名的余弦通常已经掉到"沾边"那一档；
#:   - 6 = 6 × 100 字上限 = 600 字 ≈ 400 token，与 C 类备注同量级。
TOP_K = 6

#: 相关性门槛，作用在**余弦**上，不作用在 `score`（`cosine * decay`）上。
#:
#: 这个位置是整套设计里最容易写错的一处：如果门槛判 `score`，那么衰减就变成了
#: 一台隐式的删除机——任何固定的分数阈值，被一个随时间单调下降到 0 的因子乘过
#: 之后，旧记忆**必然**在某个时刻低于它。那正是"衰减只影响排序，不删除"这条要求
#: 要禁止的事。判余弦则两者互不干扰：
#:
#:   - 余弦 < `MIN_COSINE` → 与这次主题无关，任何年龄都不该注入；
#:   - 余弦 ≥ `MIN_COSINE` → 留在候选里，**排序**才看衰减。
#:
#: 0.35 这个数：`text-embedding-v3` 上，两段同为中文、同属"思维导图"语境但说的是
#: 两件事的句子，余弦常在 0.2–0.35；而同一句偏好的两种说法（"标题去掉人名" 与
#: "别在标题里写人名"）实测在 0.45 以上。0.35 卡在这两个区间之间，代价是偶尔漏掉
#: 一个很弱的改写（少注入一条），收益是不让"都跟导图有关"变成"都算相关"。
#:
#: 这不是一条精确测量出来的最优值：本机没有 embedding 凭证，取的是经验区间。
#: 所以它写成常量、单点可调——真跑起来之后该用日志里的 `cosine` 分布去校准它，
#: 而不是凭"感觉紧了"随手改。
MIN_COSINE = 0.35

#: 时间衰减的半衰期（天）。**30 天的依据**：
#:
#:   1. 与"偏好"这个对象的稳定性对齐。偏好比事实稳（Ebbinghaus 那条遗忘曲线描述
#:      的是**无意义音节与细节**，月尺度上几乎归零），但也不是终身的——用户换了
#:      工作、换了课程，标题风格与图型习惯会跟着变。一个月是"我上次那样做是什么
#:      时候"这个人能自己回忆起来的时间尺度。
#:   2. 与仓库里已有的窗口对齐。`runs` 台账的"最近几次"、`prefs.SUGGESTION_WINDOW`
#:      的 3 次、`edits` 的当周统计，全都在"几次 / 几天"这一档；半衰期 30 天意味着
#:      30 天后旧记忆的排序权重减半，而 60 天后 1/4——它退到后面，但**仍在候选里**
#:      （见 `MIN_COSINE` 的注释：门槛判余弦，不判 score）。
#:   3. 数值上界可解释：0.5 ** (365/30) ≈ 0.0002。一年前的记忆在排序里几乎不可能
#:      压过新记忆，除非它的余弦高出一大截——那正是"旧偏好只在语义格外贴合时才
#:      重新出现"的定量表述。
#:   4. 不取 7 天：一次休假回来，所有偏好的权重减半，而人没变。衰减太快会把
#:      "稳定事实"读成"过期事实"，而这条管线没有别的信号能把它救回来。
#:   5. 不取 90 天：三个月前被改掉的偏好会与新偏好分数接近，两条自相矛盾的事实
#:      同时进 top_k，模型只能二选一（或者更糟：两句都听）。
HALF_LIFE_DAYS = 30.0

#: 检索 query 里最多拼几份材料标题。50 份材料的文件名全拼进去，向量会退化成
#: "一堆文件名的平均"——离主题更远，比只用 `goal` 更差。
MAX_QUERY_SOURCES = 3

#: 时间格式与 `mindmaps` / `runs` / `user_prefs` / `edit_events` / `library_*` 完全
#: 一致。五处格式不同就会在"谁更晚"上错一天——这是仓库里写过的教训（`runs.py:_NOW`）。
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

_CREATE_ITEMS = f"""CREATE TABLE IF NOT EXISTS {ITEM_TABLE} (
    id            TEXT PRIMARY KEY,
    text          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    source        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    formed_at     TEXT,
    last_used_at  TEXT,
    source_run_id TEXT,
    session_id    TEXT,
    text_hash     TEXT NOT NULL,
    model         TEXT,
    dim           INTEGER,
    vector        BLOB
)"""

_CREATE_SESSIONS = f"""CREATE TABLE IF NOT EXISTS {SESSION_TABLE} (
    session_id    TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    item_count    INTEGER NOT NULL DEFAULT 0,
    detail        TEXT,
    summarized_at TEXT NOT NULL
)"""

#: 观测：动作级事实，一条一个 `at`。**建表理由与粒度依据在 `observations.py` 的
#: 模块 docstring 里**，这里只放语句。
#:
#: `source_key` 的 UNIQUE 索引不是优化，是正确性：抽取是幂等的（每次 run 开始都
#: 抽一次），没有它整张表会随运行次数翻倍，而每条观测看起来都合法。
_CREATE_OBSERVATIONS = f"""CREATE TABLE IF NOT EXISTS {OBSERVATION_TABLE} (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    source_key  TEXT NOT NULL,
    map_id      TEXT,
    run_id      TEXT,
    at          TEXT NOT NULL,
    summary     TEXT NOT NULL,
    payload     TEXT NOT NULL,
    refs        TEXT NOT NULL,
    stat        INTEGER NOT NULL DEFAULT 1,
    consumed_at TEXT,
    captured_at TEXT NOT NULL
)"""

#: 命题：**1:1 关联到 `memory_items` 的一条**。正文与向量留在 `memory_items`（复用
#: 它的 UNIQUE `text_hash` 去重与 `vector` 列），这张表只放命题特有的四件事：依据、
#: 置信度、持久性、修订状态。
#:
#: 为什么不把 confidence/decay 加成 `memory_items` 的列：那会让"这一列对
#: preference/fact/pattern 意味着什么"变成一个没有答案的问题（它们没有置信度），
#: 于是每个读它的地方都要先判 kind。分开一张表，`JOIN` 一下就知道"这是命题"。
_CREATE_PROPOSITIONS = f"""CREATE TABLE IF NOT EXISTS {PROPOSITION_TABLE} (
    item_id       TEXT PRIMARY KEY,
    reasoning     TEXT NOT NULL DEFAULT '',
    confidence    INTEGER NOT NULL,
    decay         INTEGER NOT NULL,
    status        TEXT NOT NULL,
    superseded_by TEXT,
    revision      TEXT NOT NULL,
    revisions     INTEGER NOT NULL DEFAULT 0,
    source_run_id TEXT,
    session_id    TEXT,
    created_at    TEXT NOT NULL,
    revised_at    TEXT NOT NULL
)"""

#: 命题 ↔ 观测的二部图**边**。这就是论文结构里 `observation_ids` 的落地形态。
#:
#: 为什么是边表而不是 `memory_items` 上的一个 JSON 列：修订（consolidation）要
#: **合并两条命题的支撑集**，那是一次集合运算；JSON 列上做它要先把所有行读进内存、
#: 拆串、并集、写回——而且没有任何约束挡住重复 id。边表上主键就是那个约束，
#: "这两条边合并"是 `INSERT OR IGNORE` 一句话。
#:
#: 不加外键：与 `edit_events` 同一条理由（加外键只会在删除时多一个级联分支）。代价
#: 是删掉一条 `memory_items` 会留下孤儿边——读的地方一律从 `memory_items` JOIN
#: 进来，所以孤儿边不会被读到，但它会让 `COUNT(*)` 偏大，这一点写在
#: `propositions.edge_count()` 的 docstring 里。
_CREATE_EDGES = f"""CREATE TABLE IF NOT EXISTS {EDGE_TABLE} (
    item_id        TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    added_at       TEXT NOT NULL,
    PRIMARY KEY (item_id, observation_id)
)"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建五张表。由 `repository.connect()` 在同一个连接上调用，幂等。

    **`text_hash` 上是一条 UNIQUE 索引**，不是普通索引：去重是这个设计的硬要求
    （同一条偏好在两次会话里被总结出来，不该变成两条），而"先查再插"在并发下会
    漏——总结管线与手工添加可能同时写。UNIQUE 让数据库来裁决，`INSERT OR IGNORE`
    读到 `rowcount=0` 就知道已经存在。

    之所以不是 `(text_hash, kind)` 联合唯一：同一条文本只该存在一次，与它的档位
    无关。允许两档并存等于允许"同一句话既是偏好又是事实"，检索时两条一起进
    top_k，白占两个名额。

    后三张表（观测 / 命题 / 边）与 `memory_items` **在同一时刻、同一个连接上**建，
    理由与 `runs` / `edit_events` 那几处完全相同：读它们的路径都在热路径上（每次
    run 开始抽一次观测），多一个"发现表不存在 → 建表"的分支没有好处。
    """
    conn.execute(_CREATE_ITEMS)
    conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{ITEM_TABLE}_hash "
                 f"ON {ITEM_TABLE}(text_hash)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{ITEM_TABLE}_created "
                 f"ON {ITEM_TABLE}(created_at)")
    conn.execute(_CREATE_SESSIONS)

    conn.execute(_CREATE_OBSERVATIONS)
    conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{OBSERVATION_TABLE}_key "
                 f"ON {OBSERVATION_TABLE}(source_key)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{OBSERVATION_TABLE}_at "
                 f"ON {OBSERVATION_TABLE}(at)")
    # 时间路（"最近 N 条"）与"还没被归纳用过"两条查询各要一个索引。合成一个
    # `(consumed_at, at)`：前者是 `WHERE consumed_at IS NULL ORDER BY at`，
    # 后者是 `ORDER BY at DESC`，这个索引对两条都能用上，省一个。
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{OBSERVATION_TABLE}_open "
                 f"ON {OBSERVATION_TABLE}(consumed_at, at)")

    conn.execute(_CREATE_PROPOSITIONS)
    # 语义路只查 `status='active'` 的命题，`(status, item_id)` 让它走索引而不是
    # 全表扫——命题会长到几百上千条，而这条查询在每次检索里都要跑一遍。
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{PROPOSITION_TABLE}_status "
                 f"ON {PROPOSITION_TABLE}(status, item_id)")

    conn.execute(_CREATE_EDGES)
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{EDGE_TABLE}_observation "
                 f"ON {EDGE_TABLE}(observation_id)")


# ---------------------------------------------------------------- 时间

def utcnow() -> datetime:
    """当前的 UTC 时刻。

    名字带 `utc` 而不是叫 `now`：`age_days` / `recall` 都有一个叫 `now` 的关键字
    参数（那是"以哪个时刻为基准"，是用例钉住分数的地方）。模块级函数同名的话，
    函数体里那句 `now or now()` 会先解析到参数，于是"没传 now"变成
    `None()`——一个只在缺省路径上炸的 `TypeError`。
    """
    return datetime.now(timezone.utc)


def parse_time(stamp: str | None) -> datetime | None:
    """把库里的 ISO 时间读成 aware datetime；读不出来返回 None。

    三种形状都要认，因为库里的时间来自三个地方：SQLite 的
    `strftime('%Y-%m-%dT%H:%M:%fZ')`（带毫秒）、模型给的大概日期（`YYYY-MM-DD`）、
    以及可能有也可能没有毫秒的历史行。统一按 UTC 读：写入端全是 UTC，这里如果
    按本地时区解释，衰减会整体偏几个小时——不致命，但会让"30 天"变成"29.8 天"，
    而这种偏差只会在对比两份部署时暴露。
    """
    if not stamp:
        return None
    text = stamp.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def age_days(stamp: str | None, *, now: datetime | None = None) -> float | None:
    """这条记忆"有多旧"，单位天。读不出来返回 None。

    负值按 0 处理（未来的时间戳）：时钟跳变、模型给出一个未来日期、手工改库都会
    造出这种行。让年龄为负的条目拿到 > 1 的衰减权重，等于给一条坏数据额外加成。
    """
    parsed = parse_time(stamp)
    if parsed is None:
        return None
    delta = (now or utcnow()) - parsed
    return max(0.0, delta.total_seconds() / 86400.0)


def decay(days: float | None) -> float:
    """`0.5 ** (age_days / HALF_LIFE_DAYS)`；年龄未知时按"没有证据"给 1.0。

    未知年龄给 1.0 而不是 0：`formed_at` 与 `created_at` 都读不出来的行是坏数据，
    但因为坏数据把它排到最后，等于让"坏"变成了"旧"。它的余弦仍然要过
    `MIN_COSINE` 才能进来，所以给 1.0 的风险只是它可能排在前面——而那一条在
    列表里能被看见、能被删掉，比静默消失好。

    纯函数、没有隐含的"现在"：调用方传 `age_days` 进来，用例才能钉住一个具体的
    分数而不依赖跑测试的时刻。
    """
    if days is None:
        return 1.0
    if days <= 0:
        return 1.0
    return 0.5 ** (days / HALF_LIFE_DAYS)


# ---------------------------------------------------------------- 向量

def _vector_for(text: str) -> tuple[list[float] | None, str | None]:
    """条目文本 → 单位向量；未配置或失败时返回 (None, None)。

    失败只记 warning、不抛：一条记忆没有向量，后果是它检索不到（`recall` 会跳过
    没有向量的行），而不是整次总结失败。反过来做的话，一次 embedding 抖动会让
    "这次会话总结出 4 条"变成"一条都没写进去"。
    """
    if not embedding_configured():
        logger.warning("记忆条目没有向量：向量服务未配置，这条只能靠人工查看")
        return None, None
    try:
        vectors = embed_texts([text])
    except EmbeddingNotConfigured:
        logger.warning("记忆条目没有向量：向量服务未配置，这条只能靠人工查看")
        return None, None
    except EmbeddingError as exc:
        logger.warning("记忆条目没有向量：%s", f"{type(exc).__name__}：{exc}"[:160])
        return None, None
    vector = vectors[0] if vectors else None
    return (vector, embedding_model()) if vector else (None, None)


def query_vector(query: str) -> list[float] | None:
    """检索语句 → 单位向量，**走 `vector_cache`**。

    与 `knowledge.service._vector_scores` 同一个取舍：同一个 `goal` 很可能被反复
    生成（用户改一个词再跑一次），而每次生成只检索一次是这次改动里最硬的一条
    性能约束（见 `agent.service._loop` 的那段注释）。缓存让"同一句话"的第二次
    检索不再付一次 0.3–1 秒。

    缓存键是 `vector_cache.text_hash(query)`，与块向量同一张表、同一个模型名。
    这是有意的复用：那条缓存本来就是"任意文本 → 向量"。
    """
    if not query.strip():
        return None
    if not embedding_configured():
        return None
    model = embedding_model()
    key = vector_cache.text_hash(query)
    cached = vector_cache.get_vectors([key], model).get(key)
    if cached:
        return cached
    try:
        vectors = embed_texts([query])
    except EmbeddingNotConfigured:
        return None
    except EmbeddingError as exc:
        # 记忆检索失败不该让一次生成变慢或变错：返回 None，调用方注入空串。
        logger.warning("记忆检索跳过：查询向量失败（%s）",
                       f"{type(exc).__name__}：{exc}"[:160])
        return None
    vector = vectors[0] if vectors else None
    if vector:
        vector_cache.put_vectors([(key, vector)], model)
    return vector


# ---------------------------------------------------------------- 写

@dataclass(frozen=True)
class Item:
    """`memory_items` 里的一行。`vector_dim` 为 None 表示这条没有向量。"""

    id: str
    text: str
    kind: str
    source: str
    created_at: str
    formed_at: str | None = None
    last_used_at: str | None = None
    source_run_id: str | None = None
    session_id: str | None = None
    vector_dim: int | None = None
    model: str | None = None

    @property
    def has_vector(self) -> bool:
        return self.vector_dim is not None


@dataclass(frozen=True)
class Added:
    """`add()` 的结果：写进去的那条记忆，以及这次是新建还是命中去重。"""

    item: Item
    created: bool


def _row(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        text=row["text"],
        kind=row["kind"],
        source=row["source"],
        created_at=row["created_at"],
        formed_at=row["formed_at"],
        last_used_at=row["last_used_at"],
        source_run_id=row["source_run_id"],
        session_id=row["session_id"],
        vector_dim=row["dim"],
        model=row["model"],
    )


def _connect() -> sqlite3.Connection:
    # 表在 `repository.connect()` 里与 `mindmaps` / `runs` 同一时刻建好，所以这里
    # 没有任何"发现表不存在"的分支——与 `prefs.service._connect` 同一条理由。
    return repository.connect()


def _check_text(text: str) -> str:
    """规范化并夹住长度。空文本抛 `ValueError`（那是程序错误，不是数据问题）。"""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        raise ValueError("记忆条目不能是空文本")
    if len(cleaned) > MAX_ITEM_CHARS:
        logger.warning("记忆条目超过 %d 字上限，已截断（原文 %d 字）",
                       MAX_ITEM_CHARS, len(cleaned))
        cleaned = cleaned[:MAX_ITEM_CHARS]
    return cleaned


def add(
    text: str,
    kind: str = "preference",
    *,
    source: str = SOURCE_SUMMARY,
    source_run_id: str | None = None,
    session_id: str | None = None,
    formed_at: str | None = None,
    embed: bool = True,
) -> Added:
    """写一条记忆；文本已存在时返回已有那条（`created=False`），不重复写。

    **`created_at` 是总结时刻，不是"这条事发生的时刻"**。两者分开存：
    `created_at` 是审计事实（"这条是什么时候被写进来的"），`formed_at` 是模型对
    "这条大约是什么时候形成的"的推断，可空。衰减基准取 `COALESCE(formed_at,
    created_at)`——见 `recall()`。

    为什么不把 `formed_at` 直接写进 `created_at`：那会让"这条记忆是哪次会话总结
    出来的"变成一个需要反推的问题，而 `session_id` / `source_run_id` 正是为回答
    它存在的。审计事实被模型推断覆盖，是一种不可逆的信息丢失。
    """
    cleaned = _check_text(text)
    if kind not in WRITABLE_KINDS:
        raise ValueError(f"未知的记忆档位：{kind!r}")
    if source not in SOURCES:
        raise ValueError(f"未知的记忆来源：{source!r}")
    digest = vector_cache.text_hash(cleaned)

    with closing(_connect()) as conn, conn:
        existing = conn.execute(
            f"SELECT * FROM {ITEM_TABLE} WHERE text_hash = ?", (digest,)).fetchone()
        if existing is not None and existing["vector"] is not None:
            # 去重命中且向量齐全：一次 embedding 都不该再付。这是常态——
            # 同一条偏好会在好几次会话里被反复总结出来。
            return Added(item=_row(existing), created=False)

        vector, model = _vector_for(cleaned) if embed else (None, None)
        if existing is not None:
            # 命中但原来没有向量（那次 embedding 失败了）：这次补上——否则这条记忆
            # 会永久检索不到，而它在列表里看起来完全正常。
            if vector is not None:
                conn.execute(
                    f"UPDATE {ITEM_TABLE} SET vector = ?, dim = ?, model = ? WHERE id = ?",
                    (vector_cache.pack(vector), len(vector), model, existing["id"]),
                )
                existing = conn.execute(
                    f"SELECT * FROM {ITEM_TABLE} WHERE id = ?", (existing["id"],)).fetchone()
            return Added(item=_row(existing), created=False)

        item_id = uuid4().hex
        conn.execute(
            f"INSERT INTO {ITEM_TABLE} (id, text, kind, source, created_at, formed_at, "
            f"source_run_id, session_id, text_hash, model, dim, vector) "
            f"VALUES (?, ?, ?, ?, {_NOW}, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, cleaned, kind, source, formed_at, source_run_id, session_id,
             digest, model, None if vector is None else len(vector),
             None if vector is None else vector_cache.pack(vector)),
        )
        row = conn.execute(f"SELECT * FROM {ITEM_TABLE} WHERE id = ?", (item_id,)).fetchone()
    return Added(item=_row(row), created=True)


def delete(item_id: str) -> bool:
    """硬删一条。返回是否真的删掉了——404 与"删了但没删掉"必须分得开。"""
    with closing(_connect()) as conn, conn:
        result = conn.execute(f"DELETE FROM {ITEM_TABLE} WHERE id = ?", (item_id,))
    return result.rowcount == 1


def count() -> int:
    with closing(_connect()) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {ITEM_TABLE}").fetchone()[0])


def items(limit: int = 200) -> list[Item]:
    """最近的若干条，新的在前。供 HTTP 列表与总结时的"已知记忆"用。"""
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT * FROM {ITEM_TABLE} ORDER BY created_at DESC, id LIMIT ?",
            (max(1, limit),)).fetchall()
    return [_row(row) for row in rows]


# ---------------------------------------------------------------- 读：检索

@dataclass(frozen=True)
class Hit:
    """一次检索的命中，带上算分过程的每一步。

    四项分数（`cosine` / `decay` / `score` / `age_days`）全部保留，不只是 `score`：
    "衰减到底有没有在起作用"只能靠对比这三个数回答，而一个只返回 `score` 的接口
    让人无法区分"因为语义不符"与"因为太旧"——两种失败要改的常量不是同一个。
    """

    item: Item
    cosine: float
    decay: float
    score: float
    age_days: float | None

    @property
    def text(self) -> str:
        return self.item.text


def _candidates(limit: int = MAX_SCAN) -> list[sqlite3.Row]:
    """读出参与检索的行（含向量列）。"""
    with closing(_connect()) as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM {ITEM_TABLE}").fetchone()[0])
        if total > limit:
            logger.warning("记忆表共 %d 行，超过本次检索上限 %d，只取最近的 %d 行"
                           "（更旧的这次不参与：要让它无偏，该上向量索引而不是调大这个数）",
                           total, limit, limit)
        return conn.execute(
            f"SELECT * FROM {ITEM_TABLE} ORDER BY created_at DESC, id LIMIT ?",
            (limit,)).fetchall()


def recall(
    query: str,
    *,
    top_k: int = TOP_K,
    min_cosine: float = MIN_COSINE,
    now: datetime | None = None,
    touch: bool = False,
) -> list[Hit]:
    """按这次的主题检索相关记忆，带时间衰减排序。

    ```
    score = cosine(query_vector, memory.vector) * decay(age)
    decay(age) = 0.5 ** (age_days / HALF_LIFE_DAYS)
    ```

    三条写死的性质：

      1. **衰减只影响排序，不删除。** 门槛 `min_cosine` 判的是**余弦**，不是
         `score`。见 `MIN_COSINE` 的注释：判 score 的话，任何固定阈值加上一个
         单调趋 0 的因子，等价于"旧记忆迟早被删掉"。
      2. **低于门槛的不注入**，且最多 `top_k` 条。不然"检索"就是换个说法的全塞。
      3. **衰减基准是 `COALESCE(formed_at, created_at)`。** 模型能给出"这条大约
         是什么时候形成的"时用那个：一条三年前就有的习惯不该因为今天是第一次被
         总结出来，就在排序上冒充新记忆。给不出时退回总结时刻（`created_at` 本身
         始终是总结时刻，没有被改写——见 `add()`）。

    `touch=True` 时把命中的 `last_used_at` 刷成现在。它**不参与排序**：参与的话
    就变成"用过的更可能被再用"，新记忆永远进不来。它现在的唯一用途是观测——
    "这条记忆真的被检索到过吗"是这套东西能不能被证明有用的唯一凭据。

    向量已由 `embed_texts` 做过 L2 归一化，所以余弦就是点积，不必再开方。
    """
    if not query.strip():
        return []
    vector = query_vector(query)
    if vector is None:
        return []
    rows = _candidates()
    if not rows:
        return []
    model = embedding_model()
    moment = now or utcnow()
    hits: list[Hit] = []
    for row in rows:
        if row["vector"] is None or not row["dim"]:
            continue
        # 换了 embedding 模型/维度之后，旧向量与新查询向量不在同一个空间里。
        # 直接点积会得到一个"看起来合理、实际是垃圾"的数——必须跳过，绝不截断对齐。
        if row["model"] and row["model"] != model:
            continue
        item_vector = vector_cache.unpack(row["vector"], row["dim"])
        if not item_vector or len(item_vector) != len(vector):
            continue
        cosine = sum(left * right for left, right in zip(item_vector, vector))
        if cosine < min_cosine:
            continue
        base = row["formed_at"] or row["created_at"]
        days = age_days(base, now=moment)
        weight = decay(days)
        hits.append(Hit(
            item=_row(row),
            cosine=cosine,
            decay=weight,
            score=cosine * weight,
            age_days=days,
        ))

    # 两次稳定排序；先排次要键（新的在前），再排主要键。`(-score, created_at)` 想
    # 一条 key 写出来要在元组里混正负号，读的人得先反推排序方向。
    hits.sort(key=lambda hit: hit.item.created_at, reverse=True)
    hits.sort(key=lambda hit: hit.score, reverse=True)
    kept = hits[:max(1, top_k)]
    if touch and kept:
        _touch([hit.item.id for hit in kept], moment)
    return kept


def _touch(ids: list[str], moment: datetime) -> None:
    """把这几条的 `last_used_at` 刷成 `moment`。

    失败只记 warning：这是一条观测值，写不进去不该让一次生成失败。
    """
    stamp = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    marks = ",".join("?" * len(ids))
    try:
        with closing(_connect()) as conn, conn:
            conn.execute(
                f"UPDATE {ITEM_TABLE} SET last_used_at = ? WHERE id IN ({marks})",
                (stamp, *ids))
    except sqlite3.Error as exc:
        logger.warning("记忆的 last_used_at 没有写进去：%s", type(exc).__name__)


# ---------------------------------------------------------------- 注入

#: 记忆那一段的表头。**与 `prefs.NOTE_HEADER` 是两段**，顺序也是刻意的：
#:
#:   - `NOTE_HEADER`（"关于这个人的长期偏好"）写的是用户**显式声明**的东西，
#:     它必须永远生效；
#:   - 这一段写的是系统**推断**出来的东西，可能过时、可能是错的。
#:
#: 所以显式的在前、推断的在后，且推断这一段自己声明让位。两段合并成一段的后果
#: 是用户无法判断"哪几句话是我说的"，而那正是这次改动要避免的那种混乱。
MEMORY_HEADER = (
    "关于这个人的记忆（系统从过往会话里自动总结的推断，可能已经过时；"
    "与上面明确的偏好或本次指令冲突时，以后者为准）："
)


def query_text(request: object) -> str:
    """这次检索用的 query：`goal` + 材料标题，最多 3 个标题。

    `goal` 一定有（契约里 `min_length=1`），所以"都没有时退化成纯主题"这条在实现上
    就是**没有材料时只用 goal**——纯主题就是 goal 本身，不必另造一个字段。

    材料标题只取前几个：50 份材料的文件名拼进去，向量会退化成"一堆文件名的平均"，
    离主题更远。顺序取 `request.sources` 的原序（调用方按重要性排过），不排序。
    """
    goal = str(getattr(request, "goal", "") or "").strip()
    parts = [goal] if goal else []
    sources = getattr(request, "sources", None) or []
    titles = 0
    for source in sources:
        if titles >= MAX_QUERY_SOURCES:
            break
        name = str(getattr(source, "filename", "") or "").strip()
        if name:
            parts.append(name)
            titles += 1
    return "\n".join(parts)


def prompt_for(request: object) -> str:
    """一次生成要在 system 里注入的记忆那一段；没有可注入的返回**空串**。

    空串而不是"（暂无相关记忆）"：一个空的小标题会让模型以为"这个人的记忆是空的"
    是一种需要它处理的输入，而真实语义是"这次没有相关的东西，别按记忆想事情"。
    调用方按空串整段跳过（`agent.service._system_text` 里那个 `if part` 过滤）。

    检索**只在这里发生一次**（由 `agent.service._loop` 在进入轮次之前在后台线程
    调一次）。放进 `_system_text` 会让每一轮都检索一遍：骨架 1 次 + 结构 1 次 +
    detail 2 次 + 工具循环 N 轮，每次 embedding 0.3–1 秒，延迟翻几倍。
    """
    query = query_text(request)
    if not query:
        return ""
    started = time.perf_counter()
    hits = recall(query, touch=True)
    if not hits:
        logger.debug("记忆检索：query %d 字，无命中（%.0fms）",
                     len(query), (time.perf_counter() - started) * 1000)
        return ""
    # 只记条数与最高分。记忆正文是"关于这个人的话"，不进日志。
    logger.info("记忆检索：命中 %d 条，最高 score=%.3f（%.0fms）",
                len(hits), hits[0].score, (time.perf_counter() - started) * 1000)
    return MEMORY_HEADER + "\n" + "\n".join(f"- {hit.text}" for hit in hits)


__all__ = [
    "EDGE_TABLE",
    "HALF_LIFE_DAYS",
    "ITEM_TABLE",
    "KINDS",
    "MAX_ITEM_CHARS",
    "MAX_SCAN",
    "MEMORY_HEADER",
    "MIN_COSINE",
    "OBSERVATION_TABLE",
    "PROPOSITION_KIND",
    "PROPOSITION_TABLE",
    "SESSION_TABLE",
    "SOURCES",
    "SOURCE_SUMMARY",
    "SOURCE_USER_EXPLICIT",
    "TOP_K",
    "WRITABLE_KINDS",
    "Added",
    "Hit",
    "Item",
    "add",
    "age_days",
    "count",
    "decay",
    "delete",
    "ensure_schema",
    "items",
    "parse_time",
    "prompt_for",
    "query_text",
    "query_vector",
    "recall",
    "utcnow",
]
