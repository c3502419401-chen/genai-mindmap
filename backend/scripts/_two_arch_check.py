"""看 `two_arch.json` 里那些不进主表的字段：修复步数、自检条数、部分交付、检索缓存命中。

这是 `docs/TWO-ARCH-COMPARISON.md` 第 5 节列的复算入口之一，所以**数据不在时必须红**，
不是静默跳过：跳过等于"这次没算"，而调用它的人会以为算过了。
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"

#: 缺数据时的统一提示。`data/` 在 `.gitignore` 里（`data/` 那一行），干净 checkout
#: 上没有这些测量产物。**读它只能发生在 `main()` 里**：模块顶层读一次，
#: `check_script_hygiene.py` 的 load 档就会在 CI 上红（它在子进程里 `runpy` 加载模块、
#: 不执行 `main`），而本地因为磁盘上有 `data/` 会绿——那正是这道闸要防的
#: "本地绿、干净 checkout 红"。这个仓库为此连红过五次。
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
