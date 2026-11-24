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


# --------------------------------------------------------------------- 表迁移
#
# 下面这些用例都跑**真库**（`clean_env` 已经把 `MINDMAP_DB_PATH` 指到 tmp），
# 因为要钉的恰恰是"迁移有没有落盘"。用假连接测不出来：写死列名的 `INSERT ...
# SELECT` 在假连接上永远成功，而真库上它是 `no such column`；"没提交"这件事
# 更是只有跨连接再读一次才看得见。

_LEGACY_SETTINGS_NO_HOST = """CREATE TABLE model_settings (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    base_url   TEXT NOT NULL DEFAULT '',
    api_key    TEXT NOT NULL DEFAULT '',
    model      TEXT NOT NULL DEFAULT '',
    deep_model TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);"""

_LEGACY_SETTINGS_WITH_HOST = """CREATE TABLE model_settings (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    base_url     TEXT NOT NULL DEFAULT '',
    api_key      TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    deep_model   TEXT NOT NULL DEFAULT '',
    updated_at   TEXT,
    api_key_host TEXT NOT NULL DEFAULT ''
);"""

_LEGACY_PREFS = """CREATE TABLE user_prefs (
    pref_key      TEXT PRIMARY KEY,
    pref_value    TEXT NOT NULL,
    scope         TEXT NOT NULL DEFAULT 'global',
    source        TEXT NOT NULL,
    source_run_id TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);"""


def _seed(sql: str | None) -> None:
    """在当前 `MINDMAP_DB_PATH` 上建一张老表。`None` = 什么都不建。"""
    if sql is None:
        return
    from app.mindmaps.repository import db_path

    conn = sqlite3.connect(db_path())
    conn.executescript(sql)
    conn.commit()
    conn.close()


def _rows(table: str, columns: str) -> list[tuple]:
    """**新开一条裸连接**读，绕开一切缓存——只看得见真的落盘了的东西。"""
    from app.mindmaps.repository import db_path

    conn = sqlite3.connect(db_path())
    try:
        return [tuple(row) for row in conn.execute(f"SELECT {columns} FROM {table}")]
    finally:
        conn.close()


def _tables() -> set[str]:
    from app.mindmaps.repository import db_path

    conn = sqlite3.connect(db_path())
    try:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


class SettingsMigrationTest(unittest.TestCase):
    def _migrate(self) -> None:
        from app.settings import store

        store._CACHE.clear()
        self.addCleanup(store._CACHE.clear)
        conn = __import__("app.mindmaps.repository", fromlist=["connect"]).connect()
        conn.close()

    def test_a_main_era_row_survives_the_migration(self):
        """main 的老表**没有 `api_key_host`**。

        写死列名的搬运行会在这里抛 `no such column: api_key_host`，而它在
        `connect()` 的中段——不是"降级成未配置"，是**整个服务起不来**。
        """
        _seed(_LEGACY_SETTINGS_NO_HOST + """
            INSERT INTO model_settings
            VALUES (1, 'https://real.example/v1', 'sk-REAL', 'm1', '', '2026-01-01');
        """)

        self._migrate()  # 不抛就已经过了一半

        self.assertEqual(
            _rows("model_settings", "owner, base_url, api_key, model, api_key_host"),
            [("", "https://real.example/v1", "sk-REAL", "m1", "")])
        self.assertNotIn("model_settings_legacy_v1", _tables())

    def test_a_row_with_a_bound_host_keeps_that_host(self):
        """PR#27 中间态的老表：绑定过主机的那一行，迁移后绑定不能丢。

        丢了不会报错，只会让 `resolve()` 认为"没绑定过"、于是绑定校验整段失效——
        正是 P0-1 要挡的那条路。
        """
        _seed(_LEGACY_SETTINGS_WITH_HOST + """
            INSERT INTO model_settings
            VALUES (1, 'https://real.example/v1', 'sk-REAL', 'm1', '', '2026-01-01', 'real.example');
        """)

        self._migrate()

        self.assertEqual(
            _rows("model_settings", "owner, api_key, api_key_host"),
            [("", "sk-REAL", "real.example")])

    def test_the_migration_is_committed_not_just_written_in_a_transaction(self):
        """跨越连接还能读到，才叫落盘。

        `connect()` 的调用方是只读路径，不会替迁移提交。少了 `commit()` 的表现是：
        同一个进程里第一次读**看得见**（那条连接里事务还没回滚），下一次连接就是
        空的——而老表已经被 DROP，数据再也拿不回来。开发库上真踩到过。
        """
        _seed(_LEGACY_SETTINGS_WITH_HOST + """
            INSERT INTO model_settings
            VALUES (1, 'https://x.example/v1', 'sk-KEEP', 'm1', '', '2026-01-01', 'x.example');
        """)

        self._migrate()

        # 这里每一条都是新连接：看的就是磁盘上那份。
        self.assertEqual(_rows("model_settings", "api_key"), [("sk-KEEP",)])
        self._migrate()  # 再跑一次（每个请求都会调它）
        self.assertEqual(_rows("model_settings", "api_key"), [("sk-KEEP",)])

    def test_a_half_migrated_database_is_finished_by_the_next_connect(self):
        """崩溃窗口：老表已改名、新表已建、行没搬。

        判据必须认得出这个状态，否则老表永远躺在那里没人搬，而用户看到的是
        "我配的密钥没了"。
        """
        _seed(_LEGACY_SETTINGS_NO_HOST.replace("model_settings", "model_settings_legacy_v1")
              + """
            INSERT INTO model_settings_legacy_v1
            VALUES (1, 'https://real.example/v1', 'sk-REAL', 'm1', '', '2026-01-01');
        """)
        from app.settings.store import _CREATE

        conn = sqlite3.connect(__import__("app.mindmaps.repository",
                                          fromlist=["db_path"]).db_path())
        conn.execute(_CREATE)
        conn.commit()
        conn.close()

        self._migrate()

        self.assertEqual(_rows("model_settings", "api_key"), [("sk-REAL",)])
        self.assertNotIn("model_settings_legacy_v1", _tables())

    def test_a_null_column_does_not_silently_drop_the_row(self):
        """老表允许 NULL 的列，搬到新表时必须补默认值，不能让整行消失。

        `deep_model` 在**更早的版本**里是 `_ensure_columns` 用 `ALTER TABLE ADD
        COLUMN deep_model TEXT` 补上去的——`ADD COLUMN` 不带 `NOT NULL`，所以那批
        库里这一列允许 NULL；而新表把它声明成 `NOT NULL`。夹具必须复刻这个形状，
        直接照抄 main 现在的 `_CREATE`（那里已经是 `NOT NULL`）就造不出这个状态。

        `INSERT OR IGNORE` 会把违反 `NOT NULL` 的**整行**丢掉——不是报错，是
        `rowcount = 0`。实测症状是"升级完之后密钥与地址都没了"，而迁移日志一切正常。
        """
        _seed("""CREATE TABLE model_settings (
            id         INTEGER PRIMARY KEY CHECK (id = 1),
            base_url   TEXT NOT NULL DEFAULT '',
            api_key    TEXT NOT NULL DEFAULT '',
            model      TEXT NOT NULL DEFAULT '',
            updated_at TEXT
        );
        ALTER TABLE model_settings ADD COLUMN deep_model TEXT;
        INSERT INTO model_settings
        VALUES (1, 'https://real.example/v1', 'sk-NULLCOL', 'm1', '2026-01-01', NULL);
        """)

        self._migrate()

        self.assertEqual(
            _rows("model_settings", "owner, api_key, deep_model"),
            [("", "sk-NULLCOL", "")])


class PrefsMigrationTest(unittest.TestCase):
    def _connect(self):
        from app.mindmaps import repository

        return repository.connect()

    def test_an_old_single_key_row_lands_under_the_empty_owner(self):
        """老行搬成 `owner = ''`（实例行），再由 `prefs.service` 只让部署方继承。"""
        _seed(_LEGACY_PREFS + """
            INSERT INTO user_prefs
            VALUES ('note.1', '别让外人看到', 'global', 'user_explicit', NULL,
                    '2026-01-01', '2026-01-01');
        """)

        self._connect().close()

        self.assertEqual(
            _rows("user_prefs", "owner, pref_key, pref_value, source"),
            [("", "note.1", "别让外人看到", "user_explicit")])
        self.assertNotIn("user_prefs_legacy_v1", _tables())

    def test_the_new_primary_key_allows_two_owners_on_the_same_key(self):
        """复合主键是这次迁移**唯一的目的**：同一个键两个人各一条。"""
        _seed(_LEGACY_PREFS)
        self._connect().close()

        conn = self._connect()
        conn.execute("INSERT INTO user_prefs (owner, pref_key, pref_value, source, "
                     "created_at, updated_at) VALUES ('user:1', 'icon.style', 'soft', "
                     "'user_explicit', '2026-01-01', '2026-01-01')")
        conn.execute("INSERT INTO user_prefs (owner, pref_key, pref_value, source, "
                     "created_at, updated_at) VALUES ('user:2', 'icon.style', 'flat', "
                     "'user_explicit', '2026-01-01', '2026-01-01')")
        conn.commit()
        conn.close()

        self.assertEqual(
            _rows("user_prefs", "owner, pref_value"),
            [("user:1", "soft"), ("user:2", "flat")])

    def test_a_half_migrated_prefs_table_is_finished(self):
        """与 settings 同一个崩溃窗口，prefs 这侧也要能续上。"""
        _seed(_LEGACY_PREFS.replace("user_prefs", "user_prefs_legacy_v1") + """
            INSERT INTO user_prefs_legacy_v1
            VALUES ('note.1', '老备注', 'global', 'user_explicit', NULL,
                    '2026-01-01', '2026-01-01');
        """)
        from app.mindmaps.repository import _PREF_CREATE

        conn = sqlite3.connect(__import__("app.mindmaps.repository",
                                          fromlist=["db_path"]).db_path())
        conn.execute(_PREF_CREATE)
        conn.commit()
        conn.close()

        self._connect().close()

        self.assertEqual(_rows("user_prefs", "owner, pref_value"), [("", "老备注")])
        self.assertNotIn("user_prefs_legacy_v1", _tables())

    def test_the_recovery_does_not_overwrite_fresher_rows(self):
        """续搬时新表可能已经有更新的行（崩溃那一刻之后用户改过）。

        老表里那份是更旧的，不能盖回去——`INSERT OR IGNORE` 才是对的。
        """
        _seed(_LEGACY_PREFS.replace("user_prefs", "user_prefs_legacy_v1") + """
            INSERT INTO user_prefs_legacy_v1
            VALUES ('note.1', '老备注', 'global', 'user_explicit', NULL,
                    '2026-01-01', '2026-01-01');
        """)
        from app.mindmaps.repository import _PREF_CREATE

        conn = sqlite3.connect(__import__("app.mindmaps.repository",
                                          fromlist=["db_path"]).db_path())
        conn.execute(_PREF_CREATE)
        conn.execute("INSERT INTO user_prefs (owner, pref_key, pref_value, source, "
                     "created_at, updated_at) VALUES ('', 'note.1', '新备注', "
                     "'user_explicit', '2026-02-02', '2026-02-02')")
        conn.commit()
        conn.close()

        self._connect().close()

        self.assertEqual(_rows("user_prefs", "pref_value"), [("新备注",)])


if __name__ == "__main__":
    unittest.main()
