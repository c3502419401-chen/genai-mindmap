"""出 3.3 那张逐张明细（markdown），按 (组, 臂, 次) 排序，人工判重复来自盲评。

这张表以前是手抄的，抄错了四格。现在只从两份文件算：`two_arch.json`（读数）与
`data/two-arch-judge/*.json` + `_key.json`（盲评）。

这是 `docs/TWO-ARCH-COMPARISON.md` 第 3.3 节那张表的生成器，也是第 5 节列的复算入口，
所以**数据不在时必须红**：静默跳过会让那张表"复算过了"而其实一格没算。
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


def _read_judged() -> dict[str, int]:
    """`two-arch-judge/*.json` 里人工判的重复处数，按盲评图名索引。

    目录空时不能安静返回 `{}`：那份表的"人工判重复"整列会变成 0，读者会以为
    "盲评结论是没有重复"。缺数据与"判出 0 处"必须能分开。
    """
    folder = DATA / "two-arch-judge"
    files = sorted(folder.glob("*.json"))
    if not files:
        print("错误：" + _MISSING.format(name="two-arch-judge/*.json"), file=sys.stderr)
        raise SystemExit(2)
    judged: dict[str, int] = {}
    for path in files:
        for item in json.loads(path.read_text(encoding="utf-8")):
            judged[item["图"]] = item["多余重复处数"]
    return judged


def main() -> int:
    key = _read("two-arch-blind/_key.json")
    judged = _read_judged()

    by_mark = {}
    for anon, meta in key.items():
        by_mark[(meta["group"], meta["arm"], meta["run"])] = judged.get(anon, 0)

    rows = [r for r in _read("two_arch.json") if r.get("ok")]
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
