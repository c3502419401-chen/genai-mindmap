"""把 `measure_scale.py` 的几份报告汇成一张表（`docs/GRAPH-SCALE.md` 的数字来源）。

只读 JSON，不跑模型。用法：

    python scripts/summarize_scale.py            # 全部报告
    python scripts/summarize_scale.py --md       # 输出 markdown 表
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

# 报告顺序 = 结论的顺序：先现状（开关关），再开关打开的三个点位，最后两组对照。
REPORTS = [
    ("baseline", "data/scale-smoke.json"),
    ("baseline", "data/scale-baseline.json"),
    ("scaled", "data/scale-scaled-small.json"),
    ("scaled", "data/scale-scaled-small10.json"),
    ("scaled", "data/scale-scaled-small13.json"),
    ("scaled", "data/scale-scaled-medium.json"),
    ("branches16", "data/scale-medium-b16.json"),
    ("branches16+scaled", "data/scale-medium-b16-scaled.json"),
]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--md", action="store_true", help="输出 markdown 表")
    args = parser.parse_args(argv)

    if args.md:
        print("| 组 | 批 | 材料字数 | 份 | 章 | 节点上限 | 节点 | 一级分支 | 层 | 说明总字数 "
              "| 说明均长 | 压缩比 | 要点覆盖 | 秒 |")
        print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for label, name in REPORTS:
        path = BACKEND / name
        if not path.exists():
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        for row in report.get("runs") or []:
            if not row.get("ok"):
                continue
            hist = "-".join(str(value) for value in row["depth_hist"].values())
            # 最早那份报告（`scale-smoke.json`）是在加 `node_limit` 字段之前跑的。
            limit = row.get("node_limit")
            limit_text = f"{limit:2d}" if isinstance(limit, int) else "  -"
            if args.md:
                print(f"| {label} | {row['batch']} | {row['material_chars']:,} | "
                      f"{row['sources']} | {row['outline_entries']} | {limit_text} | "
                      f"{row['nodes']} | {row['branches']} | {row['depth']} | "
                      f"{row['detail_total']:,} | {row['detail_avg']} | "
                      f"{row['compression']}:1 | {row['point_coverage']:.1%} | "
                      f"{row['seconds']} |")
            else:
                print(f"{label:18s} {row['batch']:6s}#{row['sample']} "
                      f"字数 {row['material_chars']:6,d} 份 {row['sources']:2d} "
                      f"章 {row['outline_entries']:2d} 上限 {limit_text} "
                      f"节点 {row['nodes']:2d} 支 {row['branches']} 层 {row['depth']} "
                      f"[{hist}] 说明 {row['detail_total']:5,d} 均 {row['detail_avg']:5.1f} "
                      f"压缩比 {row['compression']:5.2f} 覆盖 {row['point_coverage']:.1%} "
                      f"裁剪 {'是' if row['cut'] else '否'} {row['seconds']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
