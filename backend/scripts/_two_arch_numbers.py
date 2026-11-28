"""报告里那些派生数字的单一出处：跑它，别手算。

这是 `docs/TWO-ARCH-COMPARISON.md` 第 5 节列的复算入口之一，所以**数据不在时必须红**。
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"

#: 缺数据时的统一提示。`data/` 在 `.gitignore` 里，干净 checkout 上没有这些测量产物。
#: **读它只能发生在 `main()` 里**：模块顶层读一次，`check_script_hygiene.py` 的 load 档
#: 就会在 CI 上红（子进程 `runpy` 只加载模块、不执行 `main`），而本地会绿。
_MISSING = (
    "需要 backend/data/{name}，但当前树里没有它。\n"
    "  `data/` 在 .gitignore 里，干净 checkout 上不存在——这是测量产物，不是源码。\n"
    "  复现步骤见 docs/TWO-ARCH-COMPARISON.md 第 5 节「复现」。"
)


def _read(name: str):
    """读 `backend/data/` 下的一份产物；不在就报错退出（退出码 2，不是 1）。"""
    path = DATA / name
    if not path.is_file():
        print("错误：" + _MISSING.format(name=name), file=sys.stderr)
        raise SystemExit(2)
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    rows = _read("two_arch.json")

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
