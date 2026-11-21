"""从 A/B 的 JSON 里算几个支撑结论的数（不调模型）。

    python scripts/analyze_section_slice.py data/section-slice-ab.json
"""
from __future__ import annotations

import difflib
import json
import re
import statistics
import sys
from pathlib import Path


def _walk(node):
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


def _quote_lengths(tree: dict) -> list[int]:
    lengths = []
    for node in _walk(tree["root"]):
        for ref in node.get("source_refs") or []:
            quote = ref.get("quote")
            if isinstance(quote, str) and quote:
                lengths.append(len(quote))
    return lengths


def _correlation(rows: list[dict]) -> float:
    """章字数与覆盖率的皮尔逊相关：负得越狠，"平分预算"越是瓶颈。"""
    xs = [row["chars"] for row in rows]
    ys = [row["share"] for row in rows]
    if len(xs) < 2:
        return 0.0
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = (sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** 0.5
    return num / den if den else 0.0


def _normalize(label: str) -> str:
    return re.sub(r"[\s，。、：；（）()「」\"'·—…!?！？]+", "", label or "")


def _near_duplicates(tree: dict, threshold: float = 0.8) -> list[tuple[str, str, float]]:
    """**近似**重复的 label 对（不在同一条祖先链上、相似度 ≥ threshold）。

    `_cross_branch_duplicates` 只做逐字判据（只折空白），对"改写了一个字"完全瞎——
    那是刻意的（它只在诊断里报，宁可漏不误报）。这里用更宽的判据看一眼：如果这条路
    真的把重复消掉了，那么连"高度相似"也不该在两支之间出现。
    """
    flat: list[tuple[str, tuple[str, ...]]] = []

    def walk(node, chain: tuple[str, ...]):
        for child in node.get("children") or []:
            flat.append((child.get("label") or "", chain))
            walk(child, (*chain, child.get("label") or ""))

    walk(tree["root"], ())
    pairs = []
    for i in range(len(flat)):
        for j in range(i + 1, len(flat)):
            left, right = flat[i], flat[j]
            if left[1] == right[1]:
                continue                      # 同一父节点下是另一类问题（`check()` 管）
            a, b = _normalize(left[0]), _normalize(right[0])
            if not a or not b:
                continue
            ratio = difflib.SequenceMatcher(None, a, b).ratio()
            if ratio >= threshold:
                pairs.append((left[0], right[0], round(ratio, 2)))
    return pairs


def main(path: str) -> None:
    rounds = json.loads(Path(path).read_text(encoding="utf-8"))
    for index, report in enumerate(rounds, start=1):
        for arm in ("full", "slice"):
            item = report.get(arm) or {}
            if not item.get("ok"):
                continue
            lengths = _quote_lengths(item["tree"])
            coverage = item["coverage"]
            nodes = item["nodes"]
            refs = len(lengths)
            covered = sum(row["covered"] for row in coverage)
            total = sum(row["chars"] for row in coverage)
            per_node = refs / max(1, nodes - 1)
            print(f"{arm}#{index}: 节点 {nodes}｜引用 {refs}"
                  f"（每节点 {per_node:.2f} 条）"
                  f"｜引文均长 {statistics.fmean(lengths):.1f} 字"
                  f"（最短 {min(lengths, default=0)} / 最长 {max(lengths, default=0)}）")
            print(f"         覆盖 {covered}/{total} = {covered / total:.1%}"
                  f"｜章字数与覆盖率的相关系数 {_correlation(coverage):+.2f}")
            # 天花板算术：覆盖率的上限 ≈ 节点数 × 每条引文长度 / 全文
            for label, quote_len in (("当前引文均长", statistics.fmean(lengths)),
                                     ("引文写满 300 字", 300)):
                ceiling = (nodes - 1) * per_node * quote_len / total
                print(f"         {label} → 天花板 {ceiling:.0%}")
            near = _near_duplicates(item["tree"])
            print(f"         近似重复（相似度 ≥0.8、跨父）：{len(near)} 对")
            for left, right, ratio in near[:6]:
                print(f"           {ratio}  「{left}」↔「{right}」")
            print()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/section-slice-ab.json")
