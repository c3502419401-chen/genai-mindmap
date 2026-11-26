"""出 3.3 那张逐张明细（markdown），按 (组, 臂, 次) 排序，人工判重复来自盲评。

这张表以前是手抄的，抄错了四格。现在只从两份文件算：`two_arch.json`（读数）与
`data/two-arch-judge/*.json` + `_key.json`（盲评）。
"""
import json
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
key = json.loads((DATA / "two-arch-blind" / "_key.json").read_text(encoding="utf-8"))
judged = {}
for path in sorted((DATA / "two-arch-judge").glob("*.json")):
    for item in json.loads(path.read_text(encoding="utf-8")):
        judged[(item["图"])] = item["多余重复处数"]

by_mark = {}
for anon, meta in key.items():
    by_mark[(meta["group"], meta["arm"], meta["run"])] = judged.get(anon, 0)

rows = [r for r in json.loads((DATA / "two_arch.json").read_text(encoding="utf-8"))
        if r.get("ok")]
rows.sort(key=lambda r: (r["group"], r["arm"], r["run"]))

print("| 组 | 臂 | 次 | 节点 | 一级 | 深 | 调用 | 墙钟 | 机械重复 | 每百 | 可引用覆盖 | 人工判重复 |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|")
for row in rows:
    mark = (row["group"], row["arm"], row["run"])
    cover = f"{row['citable_coverage']:.0%}" if row["sources"] else "—"
    flag = "（`partial`）" if row.get("partial") else ""
    print(f"| {row['group']} | {row['arm']} | {row['run']} | {row['node_count']} | "
          f"{row['branches']} | {row['max_depth']} | {row['model_calls']} | "
          f"{row['seconds']}s | {row['dup_places']} | {row['dup_per_100']:.2f} | "
          f"{cover} | {by_mark[mark]}{flag} |")
