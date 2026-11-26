"""复核自检清单：把被点到的节点连同父/子与 detail 打出来，人判真假用。

只读库；不调模型。

    python scripts/_audit_selfcheck.py 3d71b0ab 电力负荷预测应用
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "mindmaps.db"


def _load(prefix: str) -> dict:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    for row in conn.execute(
            "select run_id, candidate from runs where candidate is not null "
            "and candidate != '' order by created_at desc"):
        if row["run_id"].startswith(prefix):
            return json.loads(row["candidate"])
    raise SystemExit("没找到")


def _walk(node, path=()):
    yield node, path
    for child in node.get("children") or []:
        yield from _walk(child, (*path, node.get("label", "")))


def main() -> int:
    prefix = sys.argv[1]
    needle = sys.argv[2] if len(sys.argv) > 2 else ""
    body = _load(prefix)
    print("标题：", body.get("title"))
    for node, path in _walk(body["root"]):
        hay = " / ".join(path) + " / " + (node.get("label") or "")
        if needle and needle not in hay:
            continue
        depth = len(path)
        detail = (node.get("detail") or "").replace("\n", " ")
        print(f"{'  ' * depth}{node.get('label')}　[{node.get('id')[:16]}]"
              f"　detail={detail[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

