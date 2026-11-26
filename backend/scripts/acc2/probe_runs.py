"""检查最近若干次真实运行的记录：检索/材料命中到底有没有发生。

为什么需要它：`/api/knowledge/search` 对外是 `{"hits": [], "implemented": false}`
的桩，所以"向量检索真的在跑吗"这个问题不能拿那个接口回答。可用的替代证据是
`runs` 表与 `library_chunks` 的增长——生成链路内部那一次 `search_sources` 的
命中条数会落在这里。

用法：
    python scripts/acc2/probe_runs.py
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1]
DB = BACKEND / "data" / "mindmaps.db"


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    columns = [row[1] for row in conn.execute('pragma table_info("runs")')]
    print("runs columns:", columns)
    rows = conn.execute(
        'select * from "runs" order by rowid desc limit 5').fetchall()
    out = []
    for row in rows:
        item = {}
        for key in row.keys():
            value = row[key]
            if isinstance(value, (bytes, memoryview)):
                item[key] = f"<{len(bytes(value))} bytes>"
            elif isinstance(value, str) and len(value) > 300:
                item[key] = value[:300] + "…"
            else:
                item[key] = value
        out.append(item)
        print(json.dumps(item, ensure_ascii=False)[:900])
    path = HERE / "runs_tail.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved: {path}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
