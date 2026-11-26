"""看一眼某一格产物的扁平标签（自己核对用，不进报告）。用法：python scripts/_two_arch_peek.py M-A-1 [M-B-1 ...]"""
import json
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
rows = json.loads((DATA / "two_arch.json").read_text(encoding="utf-8"))
for name in sys.argv[1:]:
    row = next((item for item in rows
                if f"{item['group']}-{item['arm']}-{item['run']}" == name), None)
    if row is None:
        print(f"{name}: 没有这一行")
        continue
    print(f"=== {name}  主题 {row['topic']}  节点 {row['node_count']}  "
          f"机械重复 {row['dup_places']}")
    for item in row["labels"]:
        print(f"[{item['branch']}] {item['label']}")
    print()
