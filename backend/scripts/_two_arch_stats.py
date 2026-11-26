"""盲评数字的统计口径：按树的 bootstrap、按组的符号检验、以及"规模效应"自检。

为什么要单独一份：`每百节点` 把规模归一了，但两臂的节点分布不同（整树 53–214，
逐支 53–80）。如果"重复率"本身随规模变化，那归一化只解决了量纲，没解决混杂。
这一份就是查那个的。
"""
from __future__ import annotations

import json
import random
import statistics
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"


def load():
    rows = json.loads((DATA / "two_arch.json").read_text(encoding="utf-8"))
    key = json.loads((DATA / "two-arch-blind" / "_key.json").read_text(encoding="utf-8"))
    judged: dict[tuple[str, str, int], int] = {}
    for path in sorted((DATA / "two-arch-judge").glob("*.json")):
        for item in json.loads(path.read_text(encoding="utf-8")):
            anon = item["图"]
            meta = key[anon]
            judged[(meta["group"], meta["arm"], meta["run"])] = item["多余重复处数"]
    out = []
    for row in rows:
        if not row.get("ok"):
            continue
        mark = (row["group"], row["arm"], row["run"])
        if mark not in judged:
            continue
        out.append({**row, "人工重复": judged[mark]})
    return out


def rate(rows) -> tuple[int, int, float]:
    dups = sum(row["人工重复"] for row in rows)
    nodes = sum(row["node_count"] for row in rows)
    return dups, nodes, dups / max(1, nodes) * 100


def bootstrap(rows, rounds: int = 20000) -> tuple[float, float, float]:
    """按树重抽样（同一臂内），给出每百节点重复率差的 95% 区间。

    重抽样单位是**树**而不是节点：同一张图里的重复彼此相关，按节点重抽样会把
    区间算窄一个量级（这也是 Poisson 检验在这个数据上不该用的原因）。
    """
    rng = random.Random(20261126)
    arms = {arm: [row for row in rows if row["arm"] == arm] for arm in ("A", "B")}
    diffs = []
    for _ in range(rounds):
        _, _, left = rate([rng.choice(arms["A"]) for _ in arms["A"]])
        _, _, right = rate([rng.choice(arms["B"]) for _ in arms["B"]])
        diffs.append(left - right)
    diffs.sort()
    point = rate(arms["A"])[2] - rate(arms["B"])[2]
    # 双侧 95%
    lo = diffs[int(0.025 * rounds)]
    hi = diffs[int(0.975 * rounds)]
    share = sum(1 for delta in diffs if delta >= 0) / rounds
    return point, lo, hi, share


def main() -> int:
    rows = load()
    print(f"有盲评的图：{len(rows)}\n")
    for arm in ("A", "B"):
        block = [row for row in rows if row["arm"] == arm]
        dups, nodes, per100 = rate(block)
        print(f"{arm} 臂：{len(block)} 张，{dups} 处多余重复 / {nodes} 节点 "
              f"= {per100:.2f} 每百")
    print()
    point, lo, hi, share = bootstrap(rows)
    print(f"每百节点重复率差（A−B）= {point:+.2f}，按树 bootstrap 95% 区间 "
          f"[{lo:+.2f}, {hi:+.2f}]；B 不优于 A 的重抽样占比 {share:.1%}")

    print("\n按组配对（每百节点）：")
    better = 0
    for group in sorted({row["group"] for row in rows}):
        block = [row for row in rows if row["group"] == group]
        left = rate([row for row in block if row["arm"] == "A"])
        right = rate([row for row in block if row["arm"] == "B"])
        better += 1 if left[2] < right[2] else 0
        print(f"  {group:<3} 整树 {left[2]:>6.2f}   逐支 {right[2]:>6.2f}   "
              f"差 {left[2] - right[2]:>+7.2f}")
    print(f"整树更低（重复更少）的组：{better}/6")

    print("\n规模效应自检（同一臂内：节点数 与 每百重复率 的关系）")
    for arm in ("A", "B"):
        block = [row for row in rows if row["arm"] == arm]
        sizes = [row["node_count"] for row in block]
        rates = [row["人工重复"] / max(1, row["node_count"]) * 100 for row in block]
        if len(set(rates)) < 2:
            print(f"  {arm} 臂 每百重复率全同，无法算相关")
            continue
        mean_size, mean_rate = statistics.mean(sizes), statistics.mean(rates)
        cov = sum((s - mean_size) * (r - mean_rate) for s, r in zip(sizes, rates))
        var_s = sum((s - mean_size) ** 2 for s in sizes) ** 0.5
        var_r = sum((r - mean_rate) ** 2 for r in rates) ** 0.5
        print(f"  {arm} 臂 节点数 {min(sizes)}–{max(sizes)}，"
              f"每百重复率与节点数的 Pearson r = {cov / max(1e-9, var_s * var_r):+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
