"""看 `two_arch.json` 里那些不进主表的字段：修复步数、自检条数、部分交付、检索缓存命中。"""
import json
import statistics
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
rows = json.loads((DATA / "two_arch.json").read_text(encoding="utf-8"))

print(f"{'组':<3}{'臂':<3}{'次':<3}{'节点':>5}{'一级':>5}{'深':>4}{'修复步':>6}"
      f"{'自检条':>6}{'部分':>5}{'检索 中/未':>11}{'诊断码'}")
for row in rows:
    if not row.get("ok"):
        print(f"{row['group']:<3}{row['arm']:<3}{row['run']:<3}失败 {str(row.get('error'))[:70]}")
        continue
    repair_steps = (row.get("auto_repair") or {}).get("steps")
    issues = (row.get("self_check") or {}).get("issues") or []
    search = row.get("search") or {}
    codes = [code for code in row.get("diagnostic_codes") or []
             if code not in ("SELF_CHECK_ISSUES",)]
    print(f"{row['group']:<3}{row['arm']:<3}{row['run']:<3}{row['node_count']:>5}"
          f"{row['branches']:>5}{row['max_depth']:>4}"
          f"{'-' if repair_steps is None else repair_steps:>6}"
          f"{len(issues) if row.get('self_check') else '-':>6}"
          f"{str(row.get('partial'))[:5]:>5}"
          f"{f'{search.get('hit', 0)}/{search.get('miss', 0)}':>11}"
          f" {','.join(codes)[:70]}")

print()
for arm in ("A", "B"):
    ok = [row for row in rows if row["arm"] == arm and row.get("ok")]
    if not ok:
        continue
    nodes = [row["node_count"] for row in ok]
    print(f"{arm} 臂 {len(ok)} 张：节点 中位 {statistics.median(nodes):.0f} "
          f"范围 {min(nodes)}–{max(nodes)}；"
          f"调用 合计 {sum(r['model_calls'] for r in ok)} 均 "
          f"{statistics.mean([r['model_calls'] for r in ok]):.1f}；"
          f"token 合计 {sum(r['prompt_tokens'] + r['completion_tokens'] for r in ok)}；"
          f"墙钟 合计 {sum(r['seconds'] for r in ok):.0f}s 均 "
          f"{statistics.mean([r['seconds'] for r in ok]):.0f}s；"
          f"部分交付 {sum(1 for r in ok if r.get('partial'))} 张；"
          f"机械重复 {sum(r['dup_places'] for r in ok)} 处 / {sum(nodes)} 节点 = "
          f"{sum(r['dup_places'] for r in ok) / sum(nodes) * 100:.2f} 每百")
