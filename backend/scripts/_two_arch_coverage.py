"""可引用覆盖率只在"有材料"的图上才有意义：无材料那四张恒为 0，混进均值会把两臂
同时压低、也会把差距缩小。这一份把两个口径都打出来。

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
    rows = [row for row in _read("two_arch.json") if row.get("ok")]

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
