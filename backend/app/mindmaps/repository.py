"""本地 SQLite 仓储；生产环境 PostgreSQL 仓储尚待实现。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4
from app.contracts.mindmap import MapRecord, MindMapContent, migrate_content


def db_path() -> Path:
    configured = os.getenv("MINDMAP_DB_PATH", "").strip()
    return Path(configured) if configured else Path(__file__).resolve().parents[2] / "data" / "mindmaps.db"


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS mindmaps (
        id TEXT PRIMARY KEY, version INTEGER NOT NULL,
        content TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    # 知识库的 `library_sources` / `library_chunks` 与上面这张表建在同一时刻、
    # 同一个连接上：三条 DDL 全部幂等，所以不需要迁移脚本，也不会有"忘了跑脚本"
    # 的窗口（与 `_record` 读时补齐 schema_version 是同一条理由）。
    #
    # import 放在函数内是**为了避免环**，不是风格：`library` 顶层要
    # `from app.mindmaps import repository` 拿 `connect` / `db_path`，在模块顶层
    # 反向 import 回去就成环。函数内的 import 在第一次连接之后由 sys.modules 缓存
    # 命中，没有反复开销。
    from app.knowledge.library import ensure_schema

    ensure_schema(conn)
    return conn


def _record(row: sqlite3.Row) -> MapRecord:
    # 库里可能还躺着没有 schema_version 的 v1 记录；补默认值这一步放在读取处，
    # 而不是写一个一次性迁移脚本——数据量小、只有一个写入方，读时补齐足够，
    # 而且永远不会有"忘了跑脚本"的窗口。
    return MapRecord(id=row["id"], version=row["version"],
                     content=migrate_content(json.loads(row["content"])),
                     updated_at=row["updated_at"])


def list_maps() -> list[MapRecord]:
    with closing(connect()) as conn, conn:
        rows = conn.execute("SELECT * FROM mindmaps ORDER BY updated_at DESC, id").fetchall()
    return [_record(row) for row in rows]


def get_map(map_id: str) -> MapRecord | None:
    with closing(connect()) as conn, conn:
        row = conn.execute("SELECT * FROM mindmaps WHERE id = ?", (map_id,)).fetchone()
    return _record(row) if row else None


def create_map(content: MindMapContent) -> MapRecord:
    map_id = str(uuid4())
    with closing(connect()) as conn, conn:
        conn.execute("INSERT INTO mindmaps VALUES (?, 1, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                     (map_id, content.model_dump_json()))
    return get_map(map_id)  # type: ignore[return-value]


def update_map(map_id: str, version: int, content: MindMapContent) -> str:
    with closing(connect()) as conn, conn:
        result = conn.execute("""UPDATE mindmaps SET content = ?, version = version + 1,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE id = ? AND version = ?""", (content.model_dump_json(), map_id, version))
        if result.rowcount:
            return "updated"
        exists = conn.execute("SELECT 1 FROM mindmaps WHERE id = ?", (map_id,)).fetchone()
        return "conflict" if exists else "missing"
