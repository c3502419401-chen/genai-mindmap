"""实测：三条自动修复在**真图**上的行为（做了多少、跳过多少、能不能撤销）。

## 为什么必须用真图

用例（`tests/test_optimization_repair.py`）钉的是**性质**：幂等、引用并集、可撤销。
它们证明不了两件事：

  1. 真图上要修多少步——一张 56 节点的图若有 30 条 `SINGLE_CHILD`，那"一键修复"
     就不是"顺手清一下"，而是一次大改。用户该在按钮上看见这个数字。
  2. 撤销数据有多大。`RepairUndo.replaced` 存的是**整棵被替换的子树**，最坏情况
     （根的压缩）是整张图。这直接决定端点响应能不能直接塞进一次 HTTP 响应。

所以这个脚本跑真图，把这两件事量出来。

## 用法（在 `backend/` 下）

    python scripts/measure_repair.py
    python scripts/measure_repair.py --db data/mindmaps.db --limit 0

`--limit 0` 表示不限制张数。默认全部。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DEFAULT_DB = "data/mindmaps.db"


def _load(db: Path) -> list[tuple[str, str, dict]]:
    """`(来源, 标题, content)`。按内容指纹去重——同一棵树跑两次生成是两个 run，
    但修复的行为完全相同，重复统计只会让"平均步数"偏向那些重复生成过的主题。"""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    import hashlib

    seen: set[str] = set()
    out: list[tuple[str, str, dict]] = []

    def add(source: str, title: str, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(payload.get("root"), dict):
            return
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        if fingerprint in seen:
            return
        seen.add(fingerprint)
        out.append((source, payload.get("title") or title or "", payload))

    for row in conn.execute("select id, content from mindmaps"):
        add(f"map:{row['id']}", "", row["content"] or "")
    for row in conn.execute(
            "select run_id, request, candidate from runs "
            "where candidate is not null and length(candidate) > 2"):
        title = ""
        try:
            title = (json.loads(row["request"] or "{}") or {}).get("topic") or ""
        except (TypeError, ValueError):
            title = ""
        add(f"run:{row['run_id']}", title, row["candidate"])
    conn.close()
    return out


def _codes(diagnostics) -> Counter:
    return Counter(item.code for item in diagnostics)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=0, help="0 = 全部")
    parser.add_argument("--json", default="", help="把逐张结果写到这个文件")
    args = parser.parse_args(argv)

    from app.contracts.mindmap import MindMapContent
    from app.optimization.repair import repair, repair_summary, revert
    from app.optimization.service import check

    db = Path(args.db)
    if not db.is_absolute():
        db = BACKEND / db
    if not db.exists():
        print(f"没有这个库：{db}")
        return 2

    records = _load(db)
    if args.limit:
        records = records[: args.limit]

    rows: list[dict] = []
    totals = Counter()
    failures: list[str] = []
    for source, title, payload in records:
        try:
            content = MindMapContent.model_validate(payload)
        except Exception as error:  # noqa: BLE001 —— 库里有历史数据，读不动就跳过并计数
            totals["unreadable"] += 1
            failures.append(f"{source} 读不出来：{type(error).__name__}")
            continue

        before = _codes(check(content))
        result = repair(content)
        after = _codes(result.remaining)

        # 三条性质，在**每一张真图**上核一遍（用例里是合成图）。
        again = repair(result.content)
        idempotent = again.steps == 0 and again.content.model_dump_json() == \
            result.content.model_dump_json()
        restored = revert(result.content, result.applied)
        reversible = restored.model_dump_json() == content.model_dump_json()
        undo_bytes = len(json.dumps(
            [item.undo.model_dump(mode="json") for item in result.applied],
            ensure_ascii=False).encode("utf-8"))

        kinds = Counter(item.code for item in result.applied)
        rows.append({
            "source": source,
            "title": title[:24],
            "nodes": _count(content.root),
            "before": dict(before),
            "steps": result.steps,
            "merges": kinds.get("DUPLICATE_LABEL", 0),
            "collapses": kinds.get("SINGLE_CHILD", 0) + kinds.get("HEADING_ONLY_BRANCH", 0),
            "skipped": len(result.skipped),
            "after": dict(after),
            "idempotent": idempotent,
            "reversible": reversible,
            "undo_kb": round(undo_bytes / 1024, 1),
            "summary": repair_summary(result),
        })
        totals["maps"] += 1
        totals["steps"] += result.steps
        totals["skipped"] += len(result.skipped)
        totals["with_fix"] += 1 if result.steps else 0
        totals["not_idempotent"] += 0 if idempotent else 1
        totals["not_reversible"] += 0 if reversible else 1
        totals["duplicate_before"] += before.get("DUPLICATE_LABEL", 0)
        totals["single_child_before"] += before.get("SINGLE_CHILD", 0)
        totals["heading_before"] += before.get("HEADING_ONLY_BRANCH", 0)

    print(f"库：{db}")
    print(f"可读的图：{totals['maps']} 张（读不动 {totals['unreadable']} 条）\n")
    if not rows:
        print("没有可分析的图。")
        return 0

    print("三条规则的存量（修复前，逐张累加）：")
    print(f"  DUPLICATE_LABEL     {totals['duplicate_before']}")
    print(f"  SINGLE_CHILD        {totals['single_child_before']}")
    print(f"  HEADING_ONLY_BRANCH {totals['heading_before']}")
    print()
    print(f"有可修项的图：{totals['with_fix']} / {totals['maps']} 张")
    print(f"总步数：{totals['steps']}（跳过 {totals['skipped']}）")
    steps = sorted(row["steps"] for row in rows)
    print(f"每张图的步数：中位 {steps[len(steps) // 2]} / 最大 {steps[-1]}")
    undo = sorted(row["undo_kb"] for row in rows)
    print(f"撤销数据：中位 {undo[len(undo) // 2]} KB / 最大 {undo[-1]} KB")
    idempotent_line = ("幂等：全部成立" if not totals["not_idempotent"]
                       else f"幂等：{totals['not_idempotent']} 张不成立")
    reversible_line = ("可撤销：全部成立" if not totals["not_reversible"]
                       else f"可撤销：{totals['not_reversible']} 张不成立")
    print(idempotent_line)
    print(reversible_line)
    print()

    worst = sorted(rows, key=lambda row: -row["steps"])[:5]
    print("改动最大的 5 张：")
    for row in worst:
        print(f"  {row['nodes']:>4} 节点  步 {row['steps']:>3}"
              f"（并 {row['merges']} / 压 {row['collapses']}）"
              f"  撤销 {row['undo_kb']:>6} KB  {row['title']}")
        print(f"        {row['summary']}")

    if failures:
        print("\n读不出来的记录：")
        for line in failures[:10]:
            print(f"  {line}")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        print(f"\n逐张结果已写入 {args.json}")
    return 0


def _count(node) -> int:
    return 1 + sum(_count(child) for child in node.children)


if __name__ == "__main__":
    raise SystemExit(main())
