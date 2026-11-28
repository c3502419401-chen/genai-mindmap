"""盲评的 anon 名与 (组, 臂, 次) 的对应关系，逐行打出来核对，别靠记。

它没被 `docs/` 当复算入口点名（`TWO-ARCH-COMPARISON.md` 第 5 节点的是
`_two_arch_rows.py` / `_two_arch_key` 的产物关系），但它的每一行都是**数据派生**的：
`two_arch.json` 的节点数与 `two-arch-judge/` 的人工判重复。所以这里不采用"没数据就
跳过"——跳过之后它会打印空表并 exit 0，读的人分不清"没有重复"与"没跑"。
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

    目录不存在与目录空是两件事，但对这份脚本是同一个后果：`judged` 为空会让下面
    每一行的"人工重复"变成 0、汇总变成"0 处 / N 节点"，也就是**把没测说成 0 重复**。
    所以两者都当缺数据报错，而不是让 `glob` 安静地返回空。
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
    rows = _read("two_arch.json")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
