"""`repository._ensure_columns` 的两件事：补上缺的列，以及容忍并发补列。

并发那条不是理论：`test_agent_runs.py::test_eight_simultaneous_begin_calls_start_exactly_one_run`
在 CI 上红过两次（`duplicate column name: model`、`partial`）。八个线程同时第一次
打开一个新库，都读到缺列、各发一条 `ALTER TABLE ADD COLUMN`，后到的就抛错。

这里用一个最小的假连接把两种情形钉死：撞名当成功，别的 SQLite 错误照旧往外抛——
后者是这条兜底唯一危险的走向，把 `database is locked` 一起吞掉就变成静默丢列。
"""
from __future__ import annotations

import sqlite3
import unittest

from app.mindmaps.repository import _ensure_columns


class _StubConnection:
    """只实现 `_ensure_columns` 用到的那两件事：PRAGMA 查列、ALTER 补列。"""

    def __init__(self, existing: set[str], error: Exception | None = None) -> None:
        self.existing = set(existing)
        self.error = error
        self.alter_attempts: list[str] = []

    def execute(self, sql: str):
        if sql.startswith("PRAGMA table_info("):
            return [(0, name, "TEXT", 0, None, 0) for name in sorted(self.existing)]
        self.alter_attempts.append(sql)
        if self.error is not None:
            raise self.error
        self.existing.add(sql.split("ADD COLUMN ")[1].split(" ")[0])
        return None


class EnsureColumnsTest(unittest.TestCase):
    def test_missing_column_is_added(self):
        conn = _StubConnection({"run_id", "status"})

        _ensure_columns(conn, "runs", {"model": "TEXT"})  # type: ignore[arg-type]

        self.assertEqual(len(conn.alter_attempts), 1)
        self.assertIn("ADD COLUMN model TEXT", conn.alter_attempts[0])

    def test_existing_column_is_not_touched(self):
        conn = _StubConnection({"run_id", "model"})

        _ensure_columns(conn, "runs", {"model": "TEXT"})  # type: ignore[arg-type]

        self.assertEqual(conn.alter_attempts, [])

    def test_duplicate_column_error_means_someone_else_won(self):
        """"已经有人补上了"是想要的结果，不该让第一个并发请求失败一次。"""
        conn = _StubConnection({"run_id"}, sqlite3.OperationalError("duplicate column name: model"))

        _ensure_columns(conn, "runs", {"model": "TEXT"})  # type: ignore[arg-type]  # 不抛

        self.assertEqual(len(conn.alter_attempts), 1)

    def test_other_sqlite_errors_still_raise(self):
        """吞掉全部 OperationalError 会把"库锁住了"变成"列没补上还装作补上了"。"""
        conn = _StubConnection({"run_id"}, sqlite3.OperationalError("database is locked"))

        with self.assertRaises(sqlite3.OperationalError) as info:
            _ensure_columns(conn, "runs", {"model": "TEXT"})  # type: ignore[arg-type]

        self.assertIn("database is locked", str(info.exception))


if __name__ == "__main__":
    unittest.main()
