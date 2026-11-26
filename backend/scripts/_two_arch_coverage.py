"""可引用覆盖率只在"有材料"的图上才有意义：无材料那四张恒为 0，混进均值会把两臂
同时压低、也会把差距缩小。这一份把两个口径都打出来。"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
rows = [row for row in json.loads((DATA / "two_arch.json").read_text(encoding="utf-8"))
        if row.get("ok")]

for label, block in (("全部 12 张", rows),
                     ("有材料 8 张", [row for row in rows if row["sources"]]),
                     ("无材料 4 张", [row for row in rows if not row["sources"]])):
    print(label)
    for arm in ("A", "B"):
        side = [row for row in block if row["arm"] == arm]
        if not side:
            continue
        print(f"  {arm}：可引用覆盖 均 "
              f"{statistics.mean(row['citable_coverage'] for row in side):.1%}"
              f"（最小 {min(row['citable_coverage'] for row in side):.0%}）"
              f"  材料份数覆盖 均 {statistics.mean(row['source_coverage'] for row in side):.1%}")
