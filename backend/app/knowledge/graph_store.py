"""图存储：实体与边落在 SQLite，游走靠递归 CTE。

**为什么不用 Neo4j**：部署目标是 2C2G 单机（`docs/DEPLOY.md` 7.9、`docs/PLAN.md`
的部署口径），Neo4j 的 JVM 堆起步就是 1G+，机器上装不下第二个服务。这一层的
规模是"几千实体、上万条边"，正好在 SQLite 的舒适区：带索引的查找是微秒级，
两跳游走是毫秒级。代价是放弃 Cypher——所有遍历都写成 `WITH RECURSIVE`，
见 `neighbors`。这个替代在多层游走上的真实上限写在 `MAX_HOPS` 的注释里。

**为什么和导图共库**（`mindmaps.repository.connect()`）：省一个文件、一套备份、
一条 `MINDMAP_DB_PATH`。表名带 `graph_` 前缀，与 `mindmaps` / `chunk_vectors`
互不相干，`CREATE TABLE IF NOT EXISTS` 不会碰它们。

**游走是无向的**（`neighbors`）。抽取模型没有稳定依据判断关系方向——"甲依赖乙"
与"乙被甲依赖"在自然语言里都成立，而方向判反之后**图不会断，只是路径反向**：
从错误的那一端出发就走进死胡同，`neighbors` 返回 `[]`，与"这两个实体确实无关"
完全无法区分。哑失败在图上比在文本上更贵：文本还能 grep，图上你连"该不该有边"
都不知道。所以边按无向走，但**方向必须留在路径里**——`GraphPath.directions`
逐跳记录这条边原本是 `nodes[i] → nodes[i+1]` 还是反过来，否则读路径的人看不出
谁依赖谁。

**写与读的降级不对称**：读接口（`neighbors` / `stats`）在库不可用时返回空值——
"查不到"与"库坏了"在检索层是同一种结果，为一次查询崩掉整条链路没有收益。
写接口（`upsert_entity` / `add_edge` / `add_edges_bulk`）**照常抛 `sqlite3.Error`**
——把"没写进去"说成"写好了"会让材料入库静默缺边，而这种缺失没有任何信号能发现：
图不会报错，它只是少了一条路。

**写入有两条路**：单条 `add_edge`（一次连接一个事务，适合零星补写）与
`add_edges_bulk`（一个连接一个事务，适合入库）。两者差的是 fsync 的次数，不是
逻辑：实测 12000 行逐条写 136 秒、单事务 34 毫秒，差 4000 倍。`graph_ingest`
走批量那条路。

## 归一化规则

`normalize_name` 只做三件事：

  1. **全角转半角**：NFKC 归一化（`ＡＢＣ`→`ABC`、`１`→`1`、全角空格→半角）。
     顺带把 `㈱` 这类兼容字符折平——它们在中文材料里少见，折平只赚不赔。
  2. **去首尾空白、折叠内部连续空白**：`"  张三 "` 与 `"张三"` 必须是同一个实体，
     否则抽取层每次给的轻微差异都会造出一个新节点。
  3. **英文小写化**：`Transformer` 与 `transformer` 是同一个东西。

**中文不拆词**：实体名原样保留。项目里没有分词依赖（`service._bigram_terms` 的
二元组是给 BM25 用的相似度特征，不是词），在这里塞一个分词器会让"张三丰"与
"张三"变成两个实体的概率上升到不可控，而抽取层已经按"名词性"筛过一遍。存储层
再做一次切分，等于在两处各有一套粒度，谁都不服谁。

**同一 `name` 不同 `type` 是两个实体**（`UNIQUE(name, type)`）。这是刻意的：
材料里的"苹果"是公司、"苹果"是水果，合并它们会造出一条"苹果→iPhone"的假边。
代价是同一实体在不同块里被判成不同类型时会出现两行；抽取层用类型白名单
（`graph_extract.ENTITY_TYPES`）把这种分裂压到"有类型 vs 无类型"这一种。

**NULL 那个坑**：SQLite 的 `UNIQUE` 认为两个 NULL 互不相等，所以 `(name, NULL)`
可以重复插入任意多次，`upsert_entity` 的幂等会当场失效。落库时把 `None` 折成
空串（`_fold_type`）；`type` 不出现在任何返回结构里，所以不需要反向还原。

## 没有做的：实体消解（这是能力边界，不是待办事项）

上面三条归一化只折全角、空白、大小写，**不做别名归并**，也没有同义词表。于是
材料里的「国家市场监督管理总局」与「市场监管总局」在库里是**两个实体**，各带
各的边；简称、缩写、旧称、中英对照全部落在这一刀之外。

后果不是"少连了几条边"，而是**孤岛与"确实无关"无法区分**：`neighbors` 在两份
材料之间找不到路径时返回 `[]`，与两份材料真的没关系时返回的 `[]` 逐字节相同。
下一个读这段代码的人会以为图是完整的——它不是。

由此可以给出这套东西当前的真实能力：**跨文档关联只在专有名词级成立**（机构
全称、人名、标准号、文号这类写法稳定的对象），**概念级关联不成立**（「数字化
转型」对「数字化改革」）。而跨文档长程关联的价值恰好集中在概念层——所以这层的
形态是对的（路径与证据严格对齐、幂等、毫秒级），召回是缺的。

补齐它要的不是调参数，而是一层实体消解（别名表、简称抽取、规则或模型归并），
以及随之而来的"合并错了比没合并更贵"的判定标准。在那之前，把图上的"没路径"
当成"不确定"，不要当成"无关"。
"""
from __future__ import annotations

import logging
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import dataclass, field

from app.mindmaps import repository

logger = logging.getLogger(__name__)

ENTITY_TABLE = "graph_entities"
EDGE_TABLE = "graph_edges"

# 路径序列化成一条字符串穿过递归 CTE：实体名与关系短语是中文自由文本，引号、
# 竖线、顿号都可能出现，只有控制字符能保证 split 无歧义。`normalize_name` 会
# 剔掉控制字符，所以这两个分隔符一定不会来自数据本身。
_SEP = "\x1f"        # 条目之间
_FIELD_SEP = "\x1e"  # 一条证据的字段之间

# 路径里每一跳的方向。留成两个字符串而不是布尔，是因为它会进 `GraphPath` 的
# 返回结构、会被日志与将来的融合层读到：`"reverse"` 比 `False` 更难被误读。
FORWARD = "forward"   # 这一跳顺着边的原方向：nodes[i] 是 relation 的 src
REVERSE = "reverse"   # 这一跳逆着边的原方向：nodes[i] 是 relation 的 dst

# 游走深度的硬上限。递归 CTE 枚举的是**简单路径**（节点不重复），路径条数随
# 深度组合增长：一个 10 度节点的三层完全图能炸出上千条路径，而 2C2G 上一条
# 这样的查询会把 CPU 占到百毫秒级。无向游走把这个增长又放大一倍（平均度翻倍），
# 所以这个上限比有向时更必要。5 跳是"够用且不会失控"的界——真需要跨 5 跳以上
# 才能连上的两件事，靠这条边找回来的可信度也已经很低了。
MAX_HOPS = 5

# 单次游走的路径条数上限。检索阶段一次只拿几十条证据，多的用不上。
MAX_LIMIT = 200

# SQLite 单条语句的变量上限是 999（老版本），一次 IN 查询按 400 分批留足余量。
_QUERY_BATCH = 400

# 一次连接里要确保存在的东西。索引与表同批建：`CREATE ... IF NOT EXISTS` 重复
# 执行是纯 schema 查找，比"先探测再建"少一次往返。
_SCHEMA = (
    f"""CREATE TABLE IF NOT EXISTS {ENTITY_TABLE} (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        type       TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(name, type)
    )""",
    f"""CREATE TABLE IF NOT EXISTS {EDGE_TABLE} (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        src_id      INTEGER NOT NULL REFERENCES {ENTITY_TABLE}(id),
        dst_id      INTEGER NOT NULL REFERENCES {ENTITY_TABLE}(id),
        relation    TEXT NOT NULL,
        source_id   TEXT NOT NULL,
        chunk_start INTEGER NOT NULL,
        chunk_end   INTEGER NOT NULL,
        created_at  TEXT NOT NULL,
        UNIQUE(src_id, dst_id, relation, source_id, chunk_start)
    )""",
    # 三条索引各服务一类查询：`src_id` 与 `dst_id` 是无向游走的两条腿（`neighbors`
    # 里的 `OR` 会被 SQLite 拆成 MULTI-INDEX OR，两条腿各走一次索引），
    # `source_id` 是"这份材料贡献了哪些边"。少了 `dst_id` 这一条，无向游走会退化成
    # 整表扫描——实测同样的 3000 实体 / 9000 边，带索引 0.2ms，不带是 3ms 量级。
    f"CREATE INDEX IF NOT EXISTS idx_{EDGE_TABLE}_src ON {EDGE_TABLE}(src_id)",
    f"CREATE INDEX IF NOT EXISTS idx_{EDGE_TABLE}_dst ON {EDGE_TABLE}(dst_id)",
    f"CREATE INDEX IF NOT EXISTS idx_{EDGE_TABLE}_source ON {EDGE_TABLE}(source_id)",
)

# 递归 CTE：从 `name` 对应的所有实体出发，**无向**走 `hops` 跳（见模块 docstring
# 的「游走是无向的」）。四件事各自有理由：
#
#   - 起点用 `= name` 而不是 `LIKE`：实体名是归一化后的精确键，模糊匹配会把
#     "张三"与"张三丰"混起来，那是两个东西。
#   - 无向靠 `edge.src_id = cur OR edge.dst_id = cur`：SQLite 会把这一条拆成
#     `MULTI-INDEX OR`，两条腿分别走 `idx_graph_edges_src` 与 `_dst`。试过用
#     "邻接视图（UNION ALL 两个方向）+ join 视图"的写法，更漂亮，但计划里两张表
#     都变成 `SCAN graph_edges`（视图的谓词不下推），9000 条边慢 15 倍，弃用。
#   - `next_id`（下一跳的节点 id）写成同一个 `CASE` 表达式三次：SQLite 不能在
#     SELECT 里定义让 WHERE 复用的别名，而 CTE 套一层子查询就会把上面的索引计划
#     破坏掉。同一表达式出现三处是这里唯一能接受的重复——它由测试钉住。
#   - 环靠 `visited` 里记 id 挡：无向之后一条边就能来回，没有这一条 A→B 会把
#     递归跑到语句被取消。有了它，枚举的是简单路径，天然终止。
#   - 递归部分**没有 ORDER BY / LIMIT**：SQLite 不允许（递归 SELECT 有限制），
#     而且也不需要——SQLite 实现递归 CTE 用的是 FIFO 队列，短路径本来就先出，
#     外层 `LIMIT` 能在物化完之前就停下。加一个 ORDER BY 反而会把整个路径集合
#     先算出来再排序，正好抵消掉这个短路。
_WALK = f"""
WITH RECURSIVE walk(cur, depth, names, rels, dirs, evid, visited) AS (
    SELECT e.id, 0, e.name || :sep, '', '', '', :sep || e.id || :sep
      FROM {ENTITY_TABLE} e
     WHERE e.name = :name
    UNION ALL
    SELECT CASE WHEN edge.src_id = walk.cur THEN edge.dst_id ELSE edge.src_id END,
           walk.depth + 1,
           walk.names || target.name || :sep,
           walk.rels || edge.relation || :sep,
           walk.dirs || (CASE WHEN edge.src_id = walk.cur THEN '{FORWARD}'
                              ELSE '{REVERSE}' END) || :sep,
           walk.evid || edge.source_id || :fsep || edge.chunk_start
                     || :fsep || edge.chunk_end || :sep,
           walk.visited || (CASE WHEN edge.src_id = walk.cur THEN edge.dst_id
                                 ELSE edge.src_id END) || :sep
      FROM walk
      JOIN {EDGE_TABLE} edge ON edge.src_id = walk.cur OR edge.dst_id = walk.cur
      JOIN {ENTITY_TABLE} target ON target.id =
           (CASE WHEN edge.src_id = walk.cur THEN edge.dst_id ELSE edge.src_id END)
     WHERE walk.depth < :depth
       AND instr(walk.visited, :sep || (CASE WHEN edge.src_id = walk.cur THEN edge.dst_id
                                             ELSE edge.src_id END) || :sep) = 0
)
SELECT depth, names, rels, dirs, evid FROM walk WHERE depth > 0 LIMIT :limit
"""


@dataclass(frozen=True)
class GraphPath:
    """一条游走路径。四个列表**按下标对齐**：

    - `nodes[i]` 与 `nodes[i + 1]` 由 `relations[i]` 连接，证据是 `evidence[i]`；
    - `directions[i]` 说明这一跳在**原始边**里是顺着还是逆着（`FORWARD`/`REVERSE`）；
    - `len(relations) == len(evidence) == len(directions) == len(nodes) - 1`；
    - `nodes[0]` 是起点（归一化后的名字），`hops` 为 0 的路径不存在——只返回
      至少走过一条边的路径，否则"邻居"里会冒出一条"自己到自己的 0 跳证据"。

    **为什么必须留方向**：游走是无向的（见模块 docstring），但边本身有向。
    只给 `nodes` 与 `relations`，"甲 → 依赖 → 乙"与"甲 ← 依赖 ← 乙"读起来
    一模一样——而这两句话的意思相反。`directions[i] == FORWARD` 表示
    `nodes[i]` 是 `relations[i]` 的 src；`REVERSE` 表示它是 dst。任何要展示
    "谁依赖谁"的下游都得读这一位，只读 `relations` 会把它读反。

    `evidence` 是 `(source_id, chunk_start, chunk_end)`：下一轮把它并进
    `EvidenceHit` 时，这三样正好能拼回原文位置，不需要再查一次库。
    """

    nodes: list[str] = field(default_factory=list)
    relations: list[str] = field(default_factory=list)
    evidence: list[tuple[str, int, int]] = field(default_factory=list)
    directions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 手工构造路径时 `directions` 很容易被漏写；关系短语本身没有方向，漏写
        # 就等于声明"全按正向读"。内部 `_parse_path` 总是给全，所以这里只兜
        # "空列表"这一种情况，长度不符留给调用方看见（静默补齐会掩盖存储层 bug）。
        if not self.directions and self.relations:
            object.__setattr__(self, "directions", [FORWARD] * len(self.relations))

    @property
    def hops(self) -> int:
        return len(self.relations)


def normalize_name(name: str | None) -> str:
    """实体名/关系短语的归一化键（规则见模块 docstring 第「归一化规则」节）。

    控制字符一并剔除：它们既会破坏路径序列化的分隔符，也会在日志里当控制序列用。
    非字符串输入先转成字符串——抽取层的模型输出什么都可能给，与其在上游逐字段
    断言类型，不如让"`1` 归一化成 '1' 再因为太短被丢掉"这一条规则统一收尾。
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", str(name))
    text = "".join(char for char in text if char.isprintable())
    return " ".join(text.split()).lower()


def _fold_type(kind: str | None) -> str:
    """类型的落库形态：`None` → 空串。理由见模块 docstring 的「NULL 那个坑」。"""
    return normalize_name(kind)


def _pick_existing(candidates: list[tuple[int, str]], kind: str) -> int | None:
    """从同名实体的若干行里挑一行复用；挑不出来就 `None`（该新建）。

    规则只有两条：**类型明确就精确匹配；类型不明就取最早那行**（`candidates`
    按 id 升序）。第二条不是"随便挑一个"——调用方说不出类型时（只写一条边的
    下游、批量写边），复用任意一行都比新建一行强，新建会把一个实体劈成"有类型
    的"和"空类型的"两半，而边只挂在后一半上（实测 3000 实体被记成 6000）。

    单条写入（`_upsert`）与批量写入（`add_edges_bulk`）都走这一个函数：两处各写
    一份判断，迟早出现"先建实体再写边"与"批量写"给出两套实体归属。
    """
    if not candidates:
        return None
    if not kind:
        return candidates[0][0]
    for entity_id, existing in candidates:
        if existing == kind:
            return entity_id
    return None


def _open() -> sqlite3.Connection:
    """开连接并确保表与索引存在。失败就抛——写接口不该假装写成功。"""
    conn = repository.connect()
    try:
        for statement in _SCHEMA:
            conn.execute(statement)
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def _upsert(conn: sqlite3.Connection, entity_name: str, entity_type: str) -> int:
    """在**已有连接**内取回实体 id，没有就插入。

    不走 `INSERT OR REPLACE`：替换会先把行删掉再插，`AUTOINCREMENT` 换一个新 id，
    已经指向旧 id 的边全部变成孤儿（外键在 SQLite 默认不强制，所以不会报错，
    只会静默指错）。`OR IGNORE` + `SELECT` 两步换来的是 id 稳定。

    **类型为空时先复用同名实体（任意类型）**。这一条不是优化，是正确性：`add_edge`
    的 `src_type` / `dst_type` 是可选的，调用方（尤其是只关心"谁连着谁"的下游）
    不会为了写一条边先去查类型。若这里直接拿空类型去 `INSERT OR IGNORE`，同一个
    "甲公司"会在库里变成两行——一行 `type='组织'`（抽取时建的）、一行 `type=''`
    （写边时建的），边全挂在后面那行，前者成了孤立实体。实测 3000 个实体按 3 条边
    连完，`stats()` 报出 6000 个实体、3000 个孤立——图看着建成了，一半节点是死的。
    判定规则与批量路径共用 `_pick_existing`。
    """
    rows = conn.execute(
        f"SELECT id, type FROM {ENTITY_TABLE} WHERE name = ? ORDER BY id",
        (entity_name,),
    ).fetchall()
    picked = _pick_existing([(int(row["id"]), row["type"] or "") for row in rows],
                            entity_type)
    if picked is not None:
        return picked
    conn.execute(
        f"INSERT OR IGNORE INTO {ENTITY_TABLE} (name, type, created_at) "
        f"VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (entity_name, entity_type),
    )
    row = conn.execute(
        f"SELECT id FROM {ENTITY_TABLE} WHERE name = ? AND type = ?",
        (entity_name, entity_type),
    ).fetchone()
    # 走到这里还拿不到 id 只能是并发下别的写入方把行删了，属于不可恢复的错误。
    if row is None:
        raise sqlite3.IntegrityError("实体写入后读不到 id")
    return int(row["id"])


def upsert_entity(name: str, type: str | None = None) -> int:
    """写入（或取回）一个实体，返回它的 id。同名同类型幂等。

    `type` 为空**不等于**"新建一个无类型实体"：库里已有同名实体时直接复用它
    （理由见 `_upsert`）。反过来，`type` 明确给出时按 `(name, type)` 精确匹配，
    所以 `("甲", "人物")` 与 `("甲", "组织")` 仍是两行——这是 `UNIQUE(name, type)`
    的契约，也是"苹果公司 vs 苹果"能被区分开的原因。

    `name` 归一化后为空时抛 `ValueError`：空名字落库后所有空名字的实体都会被
    `UNIQUE` 撞成一行，那是比"这次没抽到实体"严重得多的静默错误。
    """
    entity_name = normalize_name(name)
    if not entity_name:
        raise ValueError("实体名为空")
    entity_type = _fold_type(type)
    conn = _open()
    with closing(conn), conn:
        return _upsert(conn, entity_name, entity_type)


def add_edge(
    src: str,
    dst: str,
    relation: str,
    *,
    source_id: str,
    chunk_start: int,
    chunk_end: int,
    src_type: str | None = None,
    dst_type: str | None = None,
) -> bool:
    """写一条边；**没有落库就返回 `False`**（已在库里，或这条边本身没意义）。

    两种 `False` 都不是错误：同一份材料重跑入库、相邻块因重叠再抽到同一条边，
    都是正常情况，重复写不该让整次入库算失败。真正的错误（库不可写）照常抛
    `sqlite3.Error`，让调用方计入 `failed`。

    自环（`src == dst`）直接不写：它在游走里永远走不到（`visited` 一开始就含
    起点 id），落库只会让"边数"虚高，而边数是判断图建得怎么样的唯一总量指标。

    `src` / `dst` 是**实体名**而不是 id：调用方（抽取层）手上只有名字，逼它先
    查 id 只会把一次事务拆成三次。两个实体的 upsert 与这条边的插入在同一个连接
    同一个事务里完成，不会出现"边写进去了、实体没写进去"的中间态。

    `chunk_start` / `chunk_end` 原样落库——它们是下一轮回填证据高亮的唯一依据，
    在这里做任何"修正"（比如把 end 拉到句末）都会让高亮标错字。
    """
    src_name, dst_name = normalize_name(src), normalize_name(dst)
    phrase = normalize_name(relation)
    if not (src_name and dst_name and phrase) or src_name == dst_name:
        # 不是错误，是"这条边没有内容"：模型给了空字段或把一个东西连向自己，
        # 丢掉比落一条走不通的边好。
        return False
    if int(chunk_end) < int(chunk_start):
        raise ValueError("证据区间倒置：chunk_end < chunk_start")
    conn = _open()
    with closing(conn), conn:
        src_id = _upsert(conn, src_name, _fold_type(src_type))
        dst_id = _upsert(conn, dst_name, _fold_type(dst_type))
        cursor = conn.execute(
            f"INSERT OR IGNORE INTO {EDGE_TABLE} "
            f"(src_id, dst_id, relation, source_id, chunk_start, chunk_end, created_at) "
            f"VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (src_id, dst_id, phrase, str(source_id), int(chunk_start), int(chunk_end)),
        )
        return cursor.rowcount > 0


@dataclass(frozen=True)
class EdgeInput:
    """一条待写入的边。参数与 `add_edge` 一一对应，只是打包成一个值。

    为什么要有这个类型，而不是让 `add_edges_bulk` 收 `list[tuple]`：六个位置参数
    排在一起，`chunk_start` 与 `chunk_end` 调换位置不会有任何报错，结果是高亮标错
    字。有字段名，至少错得看得见。
    """

    src: str
    dst: str
    relation: str
    source_id: str
    chunk_start: int
    chunk_end: int
    src_type: str | None = None
    dst_type: str | None = None


def _prepare_edge(item: EdgeInput) -> tuple[str, str, str, str, int, int, str, str] | None:
    """归一化 + 校验一条边；无意义（空名 / 自环 / 空关系）返回 `None`。

    与 `add_edge` 走同一套判断。两处不一致的结局是"单条写得进去、批量写不进去"
    或者反过来——这是排查成本最高的一类不一致。
    """
    src_name, dst_name = normalize_name(item.src), normalize_name(item.dst)
    phrase = normalize_name(item.relation)
    if not (src_name and dst_name and phrase) or src_name == dst_name:
        return None
    start, end = int(item.chunk_start), int(item.chunk_end)
    if end < start:
        raise ValueError("证据区间倒置：chunk_end < chunk_start")
    return (src_name, dst_name, phrase, str(item.source_id), start, end,
            _fold_type(item.src_type), _fold_type(item.dst_type))


def _load_existing(conn: sqlite3.Connection, names: list[str]) -> dict[str, list[tuple[int, str]]]:
    """一次查出这批名字已有的实体行，按名字分组、组内按 id 升序。

    分批（`_QUERY_BATCH`）是因为 SQLite 单条语句的变量上限在 999，而一次入库的
    实体数没有上限。`ORDER BY id` 不是排序洁癖：`_pick_existing` 在类型不明时取
    第一行，这里必须保证"第一行"是 id 最小的那一行，否则同一次入库的两次调用
    可能挑到不同的行。
    """
    found: dict[str, list[tuple[int, str]]] = {}
    for start in range(0, len(names), _QUERY_BATCH):
        window = names[start:start + _QUERY_BATCH]
        marks = ",".join("?" * len(window))
        rows = conn.execute(
            f"SELECT id, name, type FROM {ENTITY_TABLE} "
            f"WHERE name IN ({marks}) ORDER BY id",
            tuple(window),
        ).fetchall()
        for row in rows:
            found.setdefault(row["name"], []).append((int(row["id"]), row["type"] or ""))
    return found


def add_edges_bulk(edges: list[EdgeInput]) -> int:
    """一个连接、一个事务写完一批边，返回**真正落库**的条数。

    存在的理由只有一个，但足够：`add_edge` 每条边一次连接一次事务，而 SQLite
    每次提交都要 fsync。实测 12000 行逐条写 136 秒、单事务 34 毫秒——差 4000 倍。
    单份材料 20 块时差的是 3 秒（能忍），整库重建时差的是分钟级（不能忍），而
    "重建整库"是这条路径迟早要干的事。

    **失败不留半张图**：整批先在事务外校验（空名 / 自环 / 区间倒置），任何一条
    倒置直接抛 `ValueError` 且一条都不写；进了事务之后任何异常都由 `with conn`
    回滚，实体与边一起退回去。批量写入的原子性以"这一批"为界，不以"整份材料"
    为界——调用方（`graph_ingest`）按块调用，所以失败粒度仍是块。

    返回值与 `add_edge` 同口径：被 `UNIQUE` 忽略的重复边不计入，但它们**不是
    错误**（重跑入库、相邻块重叠都会产生重复）。
    """
    if not edges:
        return 0
    prepared: list[tuple[str, str, str, str, int, int, str, str]] = []
    for item in edges:
        row = _prepare_edge(item)
        if row is not None:
            prepared.append(row)
    if not prepared:
        return 0

    # 先把这批边涉及的所有 (名字, 类型) 收齐，实体才能一次写完——不然"先建实体
    # 再建边"就又是两次往返。
    wanted: dict[str, set[str]] = {}
    for src_name, dst_name, _phrase, _sid, _s, _e, src_kind, dst_kind in prepared:
        wanted.setdefault(src_name, set()).add(src_kind)
        wanted.setdefault(dst_name, set()).add(dst_kind)

    conn = _open()
    with closing(conn), conn:
        names = list(wanted)
        existing = _load_existing(conn, names)
        missing = [
            (name, kind)
            for name, kinds in wanted.items()
            for kind in kinds
            if _pick_existing(existing.get(name, []), kind) is None
        ]
        if missing:
            conn.executemany(
                f"INSERT OR IGNORE INTO {ENTITY_TABLE} (name, type, created_at) "
                f"VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                missing,
            )
            # 重新读一次才拿得到新行的 id：`executemany` 不返回逐行 lastrowid。
            existing = _load_existing(conn, names)
        ids: dict[tuple[str, str], int] = {}
        for name, kinds in wanted.items():
            for kind in kinds:
                picked = _pick_existing(existing.get(name, []), kind)
                if picked is None:
                    raise sqlite3.IntegrityError("实体写入后读不到 id")
                ids[(name, kind)] = picked
        cursor = conn.executemany(
            f"INSERT OR IGNORE INTO {EDGE_TABLE} "
            f"(src_id, dst_id, relation, source_id, chunk_start, chunk_end, created_at) "
            f"VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            [
                (ids[(src_name, src_kind)], ids[(dst_name, dst_kind)], phrase,
                 sid, start, end)
                for src_name, dst_name, phrase, sid, start, end, src_kind, dst_kind
                in prepared
            ],
        )
        return cursor.rowcount


def _parse_path(row: sqlite3.Row) -> GraphPath | None:
    """拆一条路径；形状不对就整条丢掉。

    宁可少一条路径，也不返回一条 `relations` / `directions` / `evidence` 错位的
    路径：错位的后果是证据指向错误的关系、方向标到错误的跳，而且这种错误在界面
    上一眼看不出来。
    """
    nodes = row["names"].split(_SEP)[:-1]  # 末尾一定多一个分隔符
    relations = row["rels"].split(_SEP)[:-1] if row["rels"] else []
    directions = row["dirs"].split(_SEP)[:-1] if row["dirs"] else []
    evidence: list[tuple[str, int, int]] = []
    for item in row["evid"].split(_SEP):
        if not item:
            continue
        parts = item.split(_FIELD_SEP)
        if len(parts) != 3:
            logger.warning("图谱证据格式异常，跳过该路径（长度 %d）", len(parts))
            return None
        try:
            evidence.append((parts[0], int(parts[1]), int(parts[2])))
        except ValueError:
            logger.warning("图谱证据偏移不是整数，跳过该路径")
            return None
    if not nodes or len(nodes) - 1 != len(relations) or len(relations) != len(evidence):
        return None
    if len(directions) != len(relations):
        return None
    if any(direction not in (FORWARD, REVERSE) for direction in directions):
        # 只可能来自 SQL 写错，但方向读错比路径少一条危险得多。
        logger.warning("图谱路径方向取值异常，跳过该路径")
        return None
    return GraphPath(nodes=nodes, relations=relations, evidence=evidence,
                     directions=directions)


def neighbors(name: str, *, hops: int = 2, limit: int = 50) -> list[GraphPath]:
    """从 `name` 出发**无向**游走 `hops` 跳，返回最多 `limit` 条路径（短路径优先）。

    为什么无向见模块 docstring；方向没有丢，`GraphPath.directions` 逐跳记着这条
    边原本是顺着还是逆着走的，所以"从 B 出发走到 A"仍然能读出"原本是 A→B"。

    `hops` 与 `limit` 都**收在硬上限内**（`MAX_HOPS` / `MAX_LIMIT`）：这不是洁癖，
    是 2C2G 上的可用性——一个手滑传进来的 `hops=12` 会让递归 CTE 在稠密子图上
    跑出指数级的中间行。截断只发生在超限时，正常取值不受影响。

    环检测在无向之后比有向时更吃紧：一条边就能来回走，`visited` 必须挡住
    "甲→乙→甲"。它挡的是**节点重复**，所以返回的每条路径都是简单路径。

    同名不同类型的实体（"苹果"公司 / 水果）会被各自当作起点，若两行挂着内容
    完全相同的边，会拿到两条一模一样的路径——三项（节点、关系、方向）与证据全等
    的重复在这一层合并掉；只差方向的两条**不合并**，它们是"甲依赖乙"与"乙依赖甲"
    两个不同的断言。

    库不可用时返回 `[]`——查询侧不需要为"图没建好"准备第二个分支。
    """
    entity = normalize_name(name)
    if not entity:
        return []
    depth = min(max(int(hops), 0), MAX_HOPS)
    if depth == 0:
        # 0 跳的语义是"没有走任何边"，那就没有路径。返回起点本身会让调用方
        # 拿到一条零证据的"命中"，比返回空更糟。
        return []
    cap = min(max(int(limit), 1), MAX_LIMIT)
    try:
        conn = _open()
    except (sqlite3.Error, OSError) as exc:
        logger.warning("图谱游走不可用：%s", type(exc).__name__)
        return []
    with closing(conn):
        rows = conn.execute(
            _WALK,
            {"name": entity, "depth": depth, "limit": cap, "sep": _SEP, "fsep": _FIELD_SEP},
        ).fetchall()
    paths: list[GraphPath] = []
    seen: set[tuple] = set()
    for row in rows:
        path = _parse_path(row)
        if path is None:
            continue
        key = (tuple(path.nodes), tuple(path.relations),
               tuple(path.directions), tuple(path.evidence))
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def stats() -> dict:
    """实体数、边数、孤立实体数。

    孤立实体是**有实体没边**的那些：它单独看没意义，但数量和总数一起看能直接
    回答"这次抽取到底连上了没有"。全是孤立实体，说明关系抽取一路空转，图建了
    等于没建——这个信号比"实体数"本身有用。

    读不到库时返回三个 0，与"空图"同形：调用方看到的都是"还没有图"。
    """
    zero = {"entities": 0, "edges": 0, "isolated_entities": 0}
    try:
        conn = _open()
    except (sqlite3.Error, OSError) as exc:
        logger.warning("图谱统计不可用：%s", type(exc).__name__)
        return zero
    with closing(conn):
        entities = conn.execute(f"SELECT COUNT(*) FROM {ENTITY_TABLE}").fetchone()[0]
        edges = conn.execute(f"SELECT COUNT(*) FROM {EDGE_TABLE}").fetchone()[0]
        isolated = conn.execute(
            f"SELECT COUNT(*) FROM {ENTITY_TABLE} e WHERE NOT EXISTS ("
            f"SELECT 1 FROM {EDGE_TABLE} x WHERE x.src_id = e.id OR x.dst_id = e.id)"
        ).fetchone()[0]
    return {"entities": int(entities), "edges": int(edges),
            "isolated_entities": int(isolated)}
