"""看一眼记忆/观测/命题这条链在**真实库**上的形状（只读，不写一个字节）。

为什么要这个脚本：`memory_items` 为 0 这件事本身说明不了问题——它可能是"总结管线没
触发"，也可能是"触发了但每次都合法地产出 0 条"，还可能是"根本没数据可总结"。这三种
要采取的动作完全不同，而它们在 HTTP 面上长得一模一样（列表都是空的）。

用法：

    cd backend
    python scripts/memory_corpus_report.py                 # 默认读 data/mindmaps.db
    python scripts/memory_corpus_report.py --db other.db

输出分五段：runs / edit_events / memory_* / 观测可达性 / 命题可达性。最后一段是
**能不能产出第一条命题**这个问题的直接回答，不是估计。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "mindmaps.db"


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        print(f"  （查询失败：{type(exc).__name__}: {exc}）")
        return []


def _table(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> None:
    rows = _rows(conn, sql, params)
    if not rows:
        print("  （无行）")
        return
    keys = rows[0].keys()
    print("  " + " | ".join(keys))
    for row in rows:
        print("  " + " | ".join(str(row[key]) for key in keys))


def report(db: Path) -> int:
    if not db.exists():
        print(f"库不存在：{db}")
        return 1
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    print(f"库：{db}（{db.stat().st_size / 1024:.0f} KB）\n")

    print("## runs")
    _table(conn, "SELECT COUNT(*) AS n, MIN(created_at) AS first, MAX(updated_at) AS last "
                 "FROM runs")
    _table(conn, "SELECT json_extract(request, '$.intent') AS intent, status, COUNT(*) AS n "
                 "FROM runs GROUP BY intent, status ORDER BY n DESC")
    _table(conn, "SELECT SUM(applied_at IS NOT NULL) AS applied, "
                 "SUM(partial = 1) AS partial, COUNT(*) AS total FROM runs")

    print("\n## edit_events")
    _table(conn, "SELECT COUNT(*) AS n, MIN(recorded_at) AS first, MAX(recorded_at) AS last "
                 "FROM edit_events")
    _table(conn, "SELECT kind, actor, COUNT(*) AS n, COUNT(DISTINCT map_id) AS maps "
                 "FROM edit_events GROUP BY kind, actor ORDER BY n DESC")
    _table(conn, "SELECT SUM(run_id IS NULL) AS no_run, SUM(run_id IS NOT NULL) AS with_run, "
                 "COUNT(*) AS total FROM edit_events")
    _table(conn, "SELECT map_id, COUNT(*) AS n, MIN(recorded_at) AS first, "
                 "MAX(recorded_at) AS last FROM edit_events "
                 "GROUP BY map_id ORDER BY n DESC LIMIT 10")

    print("\n## memory_*")
    for name in ("memory_items", "memory_sessions", "memory_observations",
                 "memory_propositions", "memory_proposition_observations"):
        found = _rows(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                      (name,))
        if not found:
            print(f"  {name}: 表不存在")
            continue
        count = _rows(conn, f"SELECT COUNT(*) AS n FROM {name}")
        print(f"  {name}: {'?' if not count else count[0]['n']} 行")
    _table(conn, "SELECT status, COUNT(*) AS n, SUM(item_count) AS items "
                 "FROM memory_sessions GROUP BY status")

    print("\n## 观测可达性（按 run 切分：一次 run + 它之后的编辑）")
    _table(conn, """
        SELECT r.run_id IS NOT NULL AS has_run, COUNT(*) AS n
        FROM edit_events GROUP BY has_run""")

    print("\n## 命题可达性（可归纳的最小单元：同一 map 上一条观测够不够）")
    _table(conn, """
        SELECT COUNT(*) AS maps_with_events,
               SUM(n_events >= 3) AS maps_with_3plus_events
        FROM (SELECT map_id, COUNT(*) AS n_events FROM edit_events GROUP BY map_id)""")
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args(argv)
    return report(args.db)


if __name__ == "__main__":
    sys.exit(main())
