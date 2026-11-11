"""块向量的 SQLite 缓存：混合检索里省掉重复的 embedding 调用。

**为什么按文本哈希缓存，而不是按 `source_id`**：`Source.id` 是
`uuid4()` 默认值，同一份材料每次请求都会拿到新 id——按它缓存的命中率恒为 0，
表只会无限膨胀。真正稳定的是块文本本身：同一份材料重传、同一次会话里
`rag_query` 被多次调用、相邻块因 100 码点重叠而共享大段原文，都能命中。

**为什么和导图共用同一个库**（`mindmaps.repository.db_path()`）：省掉一个文件、
一套备份、一套"测试要记得改路径"的环境变量。风险是事务隔离——`INSERT OR IGNORE`
是单条自动提交的短写，不与 `update_map` 的乐观锁共用事务，SQLite 的库级写锁
在毫秒级争用下由 `timeout=10` 吸收。真出现写冲突时最坏结果是这轮缓存没写进去
（下次重新 embed），不会破坏导图记录。

**上限保护**：行数到 `MAX_ROWS` 就不再写入，只读已有。向量表是纯缓存，丢掉
它只会变慢，不会丢数据，所以宁可停写也不要让它吃掉磁盘。
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import struct
from contextlib import closing

from app.mindmaps import repository

logger = logging.getLogger(__name__)

VECTOR_TABLE = "chunk_vectors"

# 缓存行数上限。1024 维 float32 = 4 KB/行，20 万行约 800 MB——已经是个不该
# 被静默跨过的体积，所以到线就停写。
MAX_ROWS = 200_000

# SQLite 单条语句的变量上限是 999（老版本），一次 IN 查询按 400 分批留足余量。
_QUERY_BATCH = 400

_CREATE = f"""CREATE TABLE IF NOT EXISTS {VECTOR_TABLE} (
    text_hash TEXT NOT NULL,
    model     TEXT NOT NULL,
    dim       INTEGER NOT NULL,
    vector    BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (text_hash, model)
)"""


def text_hash(text: str) -> str:
    """块文本的稳定键：sha256 前 32 位十六进制。

    取前 32 位（128 bit）而不是全长是为了让键短一半；在这个规模下碰撞概率
    可以忽略，而键短了索引和 IN 查询都更省。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _pack(vector: list[float]) -> bytes:
    """float32 小端打包。小端是显式指定的，不随机器字节序变。"""
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes, dim: int) -> list[float] | None:
    """解包；长度对不上（维度改过、写坏了）就当没有，绝不返回半截向量。"""
    if dim <= 0 or len(blob) != dim * 4:
        return None
    return list(struct.unpack(f"<{dim}f", blob))


def _open() -> sqlite3.Connection | None:
    """开连接并确保表存在。任何失败都返回 None：缓存不可用不是错误。"""
    try:
        conn = repository.connect()
        conn.execute(_CREATE)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("向量缓存不可用：%s", type(exc).__name__)
        return None
    return conn


def get_vectors(hashes: list[str], model: str) -> dict[str, list[float]]:
    """按文本哈希批量取向量；没命中的不在返回值里。"""
    if not hashes:
        return {}
    unique = list(dict.fromkeys(hashes))
    conn = _open()
    if conn is None:
        return {}
    found: dict[str, list[float]] = {}
    with closing(conn), conn:
        for start in range(0, len(unique), _QUERY_BATCH):
            window = unique[start:start + _QUERY_BATCH]
            marks = ",".join("?" * len(window))
            rows = conn.execute(
                f"SELECT text_hash, dim, vector FROM {VECTOR_TABLE} "
                f"WHERE model = ? AND text_hash IN ({marks})",
                (model, *window),
            ).fetchall()
            for row in rows:
                vector = _unpack(row["vector"], row["dim"])
                if vector:
                    found[row["text_hash"]] = vector
    return found


def put_vectors(items: list[tuple[str, list[float]]], model: str) -> int:
    """写入向量；同 (文本, 模型) 已有记录时忽略。返回真正落库的条数。

    `INSERT OR IGNORE` 而不是 REPLACE：同一段文本同一模型算出来的向量本来就
    应该一样，重复写只会白加一次磁盘写和一次 WAL 抖动。
    """
    if not items:
        return 0
    conn = _open()
    if conn is None:
        return 0
    written = 0
    with closing(conn), conn:
        # 上限检查放在写入前，一次检索只走一次 COUNT。
        total = conn.execute(f"SELECT COUNT(*) FROM {VECTOR_TABLE}").fetchone()[0]
        if total >= MAX_ROWS:
            logger.info("向量缓存已达上限 %d 行，本次只读不写（%d 条未落库）",
                        MAX_ROWS, len(items))
            return 0
        for hash_value, vector in items:
            if not vector:
                continue
            cursor = conn.execute(
                f"INSERT OR IGNORE INTO {VECTOR_TABLE} "
                f"(text_hash, model, dim, vector, created_at) "
                f"VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (hash_value, model, len(vector), _pack(vector)),
            )
            written += cursor.rowcount
    return written
