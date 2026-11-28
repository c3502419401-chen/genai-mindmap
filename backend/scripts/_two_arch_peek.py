"""看一眼某一格产物的扁平标签（自己核对用，不进报告）。用法：python scripts/_two_arch_peek.py M-A-1 [M-B-1 ...]

无参数时以前是"什么都不打印、exit 0"——那与"看过了、没有异常"在终端上长得一模一样，
是仓库里反复在治的静默失败。现在无参数打印用法并 exit 2。
"""
from __future__ import annotations

import json
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


def main(argv: list[str]) -> int:
    if not argv:
        print("用法：python scripts/_two_arch_peek.py M-A-1 [M-B-1 ...]", file=sys.stderr)
        return 2
    rows = _read("two_arch.json")
    for name in argv:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
