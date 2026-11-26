"""盲评的 anon 名与 (组, 臂, 次) 的对应关系，逐行打出来核对，别靠记。"""
import json
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
key = json.loads((DATA / "two-arch-blind" / "_key.json").read_text(encoding="utf-8"))
judged = {}
for path in sorted((DATA / "two-arch-judge").glob("*.json")):
    for item in json.loads(path.read_text(encoding="utf-8")):
        judged[item["图"]] = item["多余重复处数"]

rows = json.loads((DATA / "two_arch.json").read_text(encoding="utf-8"))
by_mark = {(r["group"], r["arm"], r["run"]): r for r in rows if r.get("ok")}

print(f"{'组':<3}{'臂':<3}{'次':<3}{'节点':>5}{'盲评名':>7}{'人工重复':>9}{'每百':>7}")
totals = {"A": [0, 0, 0], "B": [0, 0, 0]}
for anon in sorted(key):
    meta = key[anon]
    row = by_mark[(meta["group"], meta["arm"], meta["run"])]
    count = judged.get(anon, 0)
    per100 = count / max(1, row["node_count"]) * 100
    print(f"{meta['group']:<3}{meta['arm']:<3}{meta['run']:<3}{row['node_count']:>5}"
          f"{anon:>7}{count:>9}{per100:>7.2f}")
    bucket = totals[meta["arm"]]
    bucket[0] += count
    bucket[1] += row["node_count"]
    bucket[2] += 1 if count else 0
for arm, (dups, nodes, trees) in totals.items():
    print(f"{arm}: {dups} 处 / {nodes} 节点 = {dups / nodes * 100:.2f} 每百，"
          f"有重复 {trees}/{len(key) // 2} 张")
