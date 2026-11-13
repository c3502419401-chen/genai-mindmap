"""知识库：把「来源」从请求内的临时对象变成可跨请求复用的持久对象。

位置：`ingestion.parse_bytes` 产出 `Source` 之后、`agent` 请求组装之前；检索侧由
`knowledge.service` 的 `library=True` 分支读取。

## 为什么要有这一层

`Source.id` 是契约里的 `uuid4` 默认值——每次请求现生成。于是三件事同时不成立：

  - 材料一关浏览器就没了，用户下次还得重传；
  - `graph_store` 的边存的是入库时那个 `source_id`，跨请求对不上，`neighbors`
    返回的 `evidence` 指向一个**已经不存在**的来源；
  - `vector_cache` 只能按**块文本**复用（`text_hash`），不能按来源组织。

这一层只解决第一件与第三件的一半：**正文与来源身份**。它不碰切块算法、不碰打分、
不碰向量缓存（见下面「与向量缓存的关系」）。

## 唯一键是 `content_hash`，不是文件名

指纹只算 `text`（`sha256(text.encode("utf-8"))`）。同一份正文换一个文件名再上传，
不该在库里出现第二条记录——用户改的是展示名，不是内容。反过来，同名不同内容
（`报告.docx` 改了三版）必须各存一条，否则后一版会覆盖前一版而检索侧毫无信号。

这也是 `UNIQUE(content_hash)` 落在表上、而不是靠应用层先查后写的理由：并发的两次
上传同一份材料时，"先查后写"中间有窗口，唯一约束没有。

## 复用已有记录时只更新 `last_used_at`

`filename` / `kind` / `metadata` / `text` 都不覆盖。理由不是"省一次 UPDATE"：

  - 后到的描述性信息**不一定更新**。用户第一次标了「财务 / 预算」两个标签，第二次
    上传时前端没带上标签（很常见——标签是另一个界面的状态），覆盖就等于把用户的
    标注清空；
  - `text` 与 `content_hash` 必须同步。只更新其中一个会让表内自相矛盾，而
    `content_hash` 是唯一键，改它等于换一条记录。

要改元数据请走显式的更新接口（本轮没有；需求出现之前不猜它的语义）。

## 切块表只做台账，检索仍现切

`library_chunks` 的区间**直接取 `service._chunk_tree`**，不在这里另写切块——两份
切点实现迟早会分叉，而那种分叉除了回归用例没有任何信号。

它的用途是**回查与校验**：拿到一个持久 `source_id` 之后，能查出"这份来源被切成
了哪几段、父子关系如何"，而不必重新切一遍原文。检索路径**不**读这张表：
`search_sources` 与 `search_parent` 是两套粒度（单级块 / 子块匹配父块返回），
各自都要现切；从表里读回来还要反序列化，快不了多少，却多一个"表与算法不一致"
的失败模式。这条取舍写在服务层 `_library_sources` 的注释里。

## `load` 不还原页数、截断标记、逐句时间戳

那几项描述的是**某一次解析的过程**（第几页失败、是否被 `MAX_PAGES` 截断、每句话
在第几毫秒），不是**正文本身**。库里存的是正文与它的描述性元数据
（`SourceMetadata`：作者 / 时间 / 分类 / 标签），这些才是跨请求检索要用的。

所以 `load` 的返回值与入库前**在 DDL 覆盖的字段上**逐字段相等：
`id` / `kind` / `filename` / `text` / `metadata` / `content_hash`；`status` 归一化成
`"parsed"`（正文在库里，它就是已解析），`span` / `parsed_pages` / `truncated` /
`failed_pages` / `sentences` / `duration_seconds` 取 `Source` 的默认值。这一条是
刻意的边界，不是漏项：把解析过程也存下来，等于为"同正文、不同解析路径"再造一层
唯一性，而唯一键已经定成正文指纹了。

## 与向量缓存的关系：`prune` 不删 `chunk_vectors`

两张表的生命周期不同。`library_sources` 是**数据**（删了就没了），`chunk_vectors`
是**缓存**（删了只会变慢）。同一段文本可能被别的来源、甚至被一次临时请求用到，
按 `source_id` 去删它既删不准（缓存按 `text_hash` 索引，没有 `source_id` 列），
删了还会重算。所以 `prune` 只管 `library_*` 两张表，向量表交给它自己的
`MAX_ROWS` 上限。

## 体积

正文整份存（检索要用），`library_chunks` 每块一行（只存区间与层级，不存文本）。
实测账见 `docs/LIBRARY-MEASUREMENTS.md`；粗略量级是 1 MB 正文约 1.0 MB 库文件，
200 条上限的来源表在几十 MB 级，2C2G 上不构成约束。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from uuid import uuid4

from app.contracts.source import Source, SourceKind, SourceMetadata
from app.mindmaps import repository

logger = logging.getLogger(__name__)

SOURCES_TABLE = "library_sources"
CHUNKS_TABLE = "library_chunks"

# 建表语句与 `mindmaps.repository.connect()` 里那条 `CREATE TABLE IF NOT EXISTS`
# 是同一套写法、同一时刻执行（见 `ensure_schema`）。没有迁移脚本：这三条全部幂等，
# 而且库文件是单机的、只有一个写入方，读时补齐比"记得跑脚本"可靠。
CREATE_SOURCES = f"""CREATE TABLE IF NOT EXISTS {SOURCES_TABLE} (
    id            TEXT PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    filename      TEXT,
    kind          TEXT NOT NULL,
    text          TEXT NOT NULL,
    metadata      TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT NOT NULL,
    UNIQUE(content_hash)
)"""

CREATE_CHUNKS = f"""CREATE TABLE IF NOT EXISTS {CHUNKS_TABLE} (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id  TEXT NOT NULL REFERENCES {SOURCES_TABLE}(id) ON DELETE CASCADE,
    start      INTEGER NOT NULL,
    end        INTEGER NOT NULL,
    level      INTEGER NOT NULL,
    parent     INTEGER,
    UNIQUE(source_id, start, end, level)
)"""

CREATE_CHUNKS_INDEX = (
    f"CREATE INDEX IF NOT EXISTS idx_{CHUNKS_TABLE}_source ON {CHUNKS_TABLE}(source_id)")

# 时间格式与 `mindmaps` 表完全一致（SQLite 的 %f 是毫秒，不是微秒）。
# 不为好看：`last_used_at` 参与淘汰比较，两处格式不同就会在边界上错一天。
# 所以 `prune` 的 cutoff 也在 SQL 里生成，不在 Python 里拼——拼出来的 Python
# `%f` 是 6 位微秒，与库里的 3 位毫秒比字符串时 `...00.123Z` vs `...00.123456Z`
# 的第四位是 `Z` 对 `4`，字典序立刻反过来。
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建两张表与索引。幂等，供 `repository.connect()` 在每次连接时调用。"""
    conn.execute(CREATE_SOURCES)
    conn.execute(CREATE_CHUNKS)
    conn.execute(CREATE_CHUNKS_INDEX)


def _connect() -> sqlite3.Connection:
    """连接 + 打开外键。

    `PRAGMA foreign_keys` 是**连接级**开关，SQLite 默认关着，`ON DELETE CASCADE`
    只有在它打开时才生效。不打开的表现是 `forget` 返回真、切块静默留着——脏数据
    不会报错，只会在下一次 `prune` 之后让表越来越大。
    """
    conn = repository.connect()
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def content_hash(text: str) -> str:
    """正文指纹：`sha256(text.encode("utf-8"))` 的十六进制全长。

    与 `vector_cache.text_hash`（前 32 位）不同：那个是缓存键、碰撞只影响命中率；
    这个是**唯一键**，碰撞会把两份不同正文合成一条，那是数据丢失。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StoredChunk:
    """`library_chunks` 里的一行。

    `parent` 是**本来源内父块的序号**（0 起），不是 `_chunk_tree` 里的列表下标——
    列表下标混着父块与子块，跨来源没有意义；父块序号在来源内唯一、且顺序稳定。
    父块自己的 `parent` 是 `None`。
    """

    start: int
    end: int
    level: int
    parent: int | None


def _chunk_rows(source_id: str, text: str) -> list[tuple[str, int, int, int, int | None]]:
    """按 `_chunk_tree` 的结果摊成待插入的行。

    延迟 import：`service` 在 `library=True` 时 import 本模块，模块顶层 import 回去
    就是环。切块算法本身**只有一份**，这里只是消费它的输出。
    """
    from app.knowledge.service import _chunk_tree

    tree = _chunk_tree(text)
    ordinal_of: dict[int, int] = {}
    for index, node in enumerate(tree):
        if node.level == 0:
            ordinal_of[index] = len(ordinal_of)
    return [
        (source_id, node.start, node.end, node.level,
         None if node.parent is None else ordinal_of[node.parent])
        for node in tree
    ]


def _metadata_json(metadata: SourceMetadata) -> str:
    """元数据落库形态。

    `ensure_ascii=False` 让中文标签在库里可读（排查时要能直接看）；
    `sort_keys=True` 让同一份元数据的序列化稳定——将来若要比对"元数据变没变"，
    键序抖动会把它判成每次都变了。
    """
    return json.dumps(metadata.model_dump(), ensure_ascii=False, sort_keys=True)


def ingest(source: Source) -> str:
    """把一份来源写进库，返回**持久 id**；同 `content_hash` 复用已有记录。

    `source.content_hash` 只是"解析时的指纹"，进库前一律重算：正文可能在解析之后
    被改过（调用方编辑、按上下文截断），拿旧指纹去写会造出一条"指纹与正文对不上"
    的记录，而唯一键正是这个指纹——两份不同的正文会因此撞成一条。两者不一致时
    记一条 warning，落库的永远是与 `text` 一致的那个。

    空正文抛 `ValueError`：空材料没有可检索的内容，落一行只会让 `list_sources`
    与 `prune` 的计数变脏。调用方（`graph_ingest`）在判定正文为空之后才调这里。
    """
    text = source.text or ""
    if not text.strip():
        raise ValueError("正文为空：空材料不该进知识库")

    digest = content_hash(text)
    declared = source.content_hash
    if declared and declared != digest:
        logger.warning(
            "正文指纹与解析时不一致（声明 %s… 实际 %s…）：以正文为准",
            declared[:8], digest[:8])

    with closing(_connect()) as conn, conn:
        row = conn.execute(
            f"SELECT id FROM {SOURCES_TABLE} WHERE content_hash = ?", (digest,)
        ).fetchone()
        if row is not None:
            source_id = str(row["id"])
            conn.execute(
                f"UPDATE {SOURCES_TABLE} SET last_used_at = {_NOW} WHERE id = ?",
                (source_id,))
            logger.debug("知识库复用 来源=%s 正文=%d 字", source_id[:8], len(text))
            return source_id

        source_id = str(uuid4())
        try:
            conn.execute(
                f"INSERT INTO {SOURCES_TABLE} "
                "(id, content_hash, filename, kind, text, metadata, "
                f"created_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?, {_NOW}, {_NOW})",
                (source_id, digest, source.filename, source.kind.value, text,
                 _metadata_json(source.metadata)),
            )
        except sqlite3.IntegrityError:
            # 并发的两次上传同一份材料：唯一约束替"先查后写"的窗口兜底。
            # 先回滚再回查——语句级失败之后事务处于"已中止"状态，不回滚就继续
            # 发语句，SQLite 会报 "cannot start a transaction within a transaction"
            # 之类与真正原因无关的错。
            conn.rollback()
            existing = conn.execute(
                f"SELECT id FROM {SOURCES_TABLE} WHERE content_hash = ?", (digest,)
            ).fetchone()
            if existing is None:      # pragma: no cover - 约束只可能因为 hash 冲突
                raise
            source_id = str(existing["id"])
            conn.execute(
                f"UPDATE {SOURCES_TABLE} SET last_used_at = {_NOW} WHERE id = ?",
                (source_id,))
            return source_id

        rows = _chunk_rows(source_id, text)
        conn.executemany(
            f"INSERT OR IGNORE INTO {CHUNKS_TABLE} "
            "(source_id, start, end, level, parent) VALUES (?, ?, ?, ?, ?)", rows)
    logger.info("知识库入库 来源=%s 正文=%d 字 切块=%d 行",
                source_id[:8], len(text), len(rows))
    return source_id


def _to_source(row: sqlite3.Row) -> Source:
    """一行 → `Source`。`metadata` 过 `SourceMetadata` 的 validator 再归一化一次。

    那道归一化不是冗余：库里存的可能是上一版代码写进去的时间串（带偏移、带毫秒、
    或者纯日期），构造时折成 UTC 秒级才对得上当前的过滤语义。`extra="forbid"` 也
    顺带保证库里不会躺着我们不认识的键。
    """
    return Source(
        id=str(row["id"]),
        kind=SourceKind(str(row["kind"])),
        filename=row["filename"],
        text=str(row["text"]),
        status="parsed",
        metadata=SourceMetadata(**json.loads(row["metadata"])),
        content_hash=str(row["content_hash"]),
    )


def load(source_id: str) -> Source | None:
    """按持久 id 还原来源；不存在返回 `None`。

    **无副作用**：不改 `last_used_at`。这个接口的语义是"回查一条已知来源"
    （图边带来的 `source_id`、界面上点一个引用），被回查不等于被检索命中——把
    "点开看了一眼"也算使用，会让淘汰策略失去区分度。检索路径用它自己的
    `recent()` + `touch()`，见服务层。
    """
    with closing(_connect()) as conn, conn:
        row = conn.execute(
            f"SELECT * FROM {SOURCES_TABLE} WHERE id = ?", (source_id,)).fetchone()
    return _to_source(row) if row is not None else None


def chunks(source_id: str) -> list[StoredChunk]:
    """按原文顺序返回这份来源的切块台账（父块在前、子块紧跟其后）。

    顺序直接由 `(start, level)` 排定：同一个 `start` 上父块一定先于它的首个子块
    （父块起点就是子块起点时，`level=0` 排在前面），其余按原文位置。
    """
    with closing(_connect()) as conn, conn:
        rows = conn.execute(
            f"SELECT start, end, level, parent FROM {CHUNKS_TABLE} "
            "WHERE source_id = ? ORDER BY start, level", (source_id,)).fetchall()
    return [StoredChunk(start=row["start"], end=row["end"], level=row["level"],
                        parent=row["parent"]) for row in rows]


def list_sources(*, limit: int = 50) -> list[dict]:
    """库里的来源摘要，最近使用的在前。**不含正文**——列表页不需要它，而一份
    三万字的正文乘上 50 条就是内存里的 1.5 MB。
    """
    with closing(_connect()) as conn, conn:
        rows = conn.execute(
            f"SELECT s.id, s.content_hash, s.filename, s.kind, s.created_at, "
            f"s.last_used_at, length(s.text) AS chars, "
            f"(SELECT COUNT(*) FROM {CHUNKS_TABLE} c WHERE c.source_id = s.id) "
            "AS chunks "
            f"FROM {SOURCES_TABLE} s ORDER BY s.last_used_at DESC, s.id LIMIT ?",
            (max(int(limit), 1),)).fetchall()
    return [dict(row) for row in rows]


def recent(*, limit: int = 100) -> list[Source]:
    """最近使用过的 `limit` 份来源，**含正文**，最近使用的在前。

    检索侧拿它做"库里的候选来源"：先按 `last_used_at` 截断（这是硬上限，避免把
    整个库都切块），再由服务层按查询做一次字面粗筛、取更小的子集。
    """
    with closing(_connect()) as conn, conn:
        rows = conn.execute(
            f"SELECT * FROM {SOURCES_TABLE} "
            "ORDER BY last_used_at DESC, id LIMIT ?", (max(int(limit), 1),)).fetchall()
    return [_to_source(row) for row in rows]


def touch(source_ids: list[str]) -> int:
    """把这些来源的 `last_used_at` 刷成现在，返回真正更新的条数。

    检索命中库里的来源时调用：`last_used_at` 记录的是"最后一次**用到**"，不是
    "入库时间"。两者混为一谈时，`prune` 会优先淘汰那些一直在被检索、只是入库很早
    的材料——那恰好是最该留的那批。
    """
    unique = [str(source_id) for source_id in dict.fromkeys(source_ids) if source_id]
    if not unique:
        return 0
    marks = ",".join("?" * len(unique))
    with closing(_connect()) as conn, conn:
        cursor = conn.execute(
            f"UPDATE {SOURCES_TABLE} SET last_used_at = {_NOW} "
            f"WHERE id IN ({marks})", unique)
        return cursor.rowcount


def forget(source_id: str) -> bool:
    """删掉一份来源（切块跟着级联删），返回是否真删到了。

    切块走 `ON DELETE CASCADE`，不在 Python 里补一条 DELETE：两处各删一次时，
    任何一处漏改都会留下孤儿行，而孤儿行只会在下一次 `prune` 之后表现为"表越来越
    大、查出来的块数对不上"。
    """
    with closing(_connect()) as conn, conn:
        cursor = conn.execute(
            f"DELETE FROM {SOURCES_TABLE} WHERE id = ?", (source_id,))
        return cursor.rowcount > 0


def prune(*, keep_days: int = 90, keep_count: int = 200) -> int:
    """按使用时间与条数淘汰来源，返回删除条数。**不自动调用**。

    两条规则独立生效、可以同时删同一批（第二次 DELETE 命中 0 行）：

      1. `last_used_at` 早于 `keep_days` 天前 → 删；
      2. 除最近的 `keep_count` 条之外 → 删。

    `keep_days=0` 是"全删"的显式写法（cutoff 就是现在），不是"不按天数筛"；
    `keep_count=0` 同理。两个默认值（90 天 / 200 条）对着 2C2G：一份典型材料
    30 KB，200 条约 6 MB 正文，加上切块台账十几 MB 量级。

    删来源级联删切块，**不删 `chunk_vectors`**——理由见模块 docstring 的
    「与向量缓存的关系」。
    """
    days = f"-{max(int(keep_days), 0)} days"
    keep = max(int(keep_count), 0)
    removed = 0
    with closing(_connect()) as conn, conn:
        removed += conn.execute(
            f"DELETE FROM {SOURCES_TABLE} "
            f"WHERE last_used_at < strftime('%Y-%m-%dT%H:%M:%fZ','now',?)",
            (days,)).rowcount
        removed += conn.execute(
            f"DELETE FROM {SOURCES_TABLE} WHERE id NOT IN ("
            f"SELECT id FROM {SOURCES_TABLE} "
            "ORDER BY last_used_at DESC, id LIMIT ?)", (keep,)).rowcount
    if removed:
        logger.info("知识库淘汰 %d 份来源（keep_days=%d keep_count=%d）",
                    removed, keep_days, keep_count)
    return removed
