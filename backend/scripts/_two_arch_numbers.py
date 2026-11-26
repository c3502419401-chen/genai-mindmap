"""报告里那些派生数字的单一出处：跑它，别手算。"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
rows = json.loads((DATA / "two_arch.json").read_text(encoding="utf-8"))


def arm(name: str) -> list[dict]:
    return [row for row in rows if row["arm"] == name and row.get("ok")]


for name in ("A", "B"):
    block = arm(name)
    nodes = sum(row["node_count"] for row in block)
    tokens = sum(row["prompt_tokens"] + row["completion_tokens"] for row in block)
    print(f"{name} 臂  n={len(block)}  节点 {nodes}  调用 "
          f"{sum(row['model_calls'] for row in block)}  token {tokens}"
          f"（输入 {sum(row['prompt_tokens'] for row in block)} / "
          f"输出 {sum(row['completion_tokens'] for row in block)}）")
    print(f"    token/图 {tokens / len(block):.0f}  token/节点 {tokens / nodes:.0f}"
          f"  墙钟 {sum(row['seconds'] for row in block):.0f}s"
          f"（均 {statistics.mean(row['seconds'] for row in block):.1f}s）")
    print(f"    detail 中位 {statistics.median(row['detail_chars_mean'] for row in block):.1f} 字"
          f"  可引用覆盖 均 "
          f"{statistics.mean(row['citable_coverage'] for row in block):.1%}"
          f"  部分交付 {sum(1 for row in block if row.get('partial'))}")
    steps = sum((row.get("auto_repair") or {}).get("steps") or 0 for row in block)
    checks = sum(len((row.get("self_check") or {}).get("issues") or []) for row in block)
    print(f"    修复步数 合计 {steps}，自检发现 合计 {checks} 条"
          f"（{'修复与自检只在整树那条路上有' if name == 'A' else '逐支那条路不接这两个开关'}）")
    search = [row.get("search") or {} for row in block]
    print(f"    检索 命中/未命中：{sum(item.get('hit', 0) for item in search)}"
          f"/{sum(item.get('miss', 0) for item in search)}")
    mech = sum(row["dup_places"] for row in block)
    print(f"    机械重复 {mech} 处 / {nodes} 节点 = {mech / nodes * 100:.2f} 每百")
    print()

total_mech = sum(row["dup_places"] for row in rows if row.get("ok"))
print(f"两臂机械重复合计 {total_mech} 处")
