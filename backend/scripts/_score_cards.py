"""把 20 棵已生成的树压成"评分卡"，供人按 4 维度打分。

为什么需要它：`docs/PLAN.md:128` 的 4 维度（覆盖 / 层次 / 无捏造 / 可读）里，
只有"覆盖"能用 `must` 关键词自动算。其余三维要人看树，而整棵树的大纲有上千行
（17 条 × 60–90 行），逐条通读会把上下文吃光，也容易看到后面忘了前面。
评分卡把每条压成十几行：一级分支、每支扇出、重复与超长 label、抽检的 detail 原文。

用法：
    python scripts/_score_cards.py > data/acc_score_cards.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DATA = BACKEND / "data"
SAMPLES = Path(__file__).with_name("acc_samples.json")


def walk(node, depth=1):
    yield node, depth
    for child in node.get("children") or []:
        yield from walk(child, depth + 1)


def card(row: dict, content: dict, must: list[str]) -> str:
    root = content["root"]
    pairs = list(walk(root))
    labels = [(n.get("label") or "").strip() for n, _ in pairs]
    details = [(n.get("detail") or "").strip() for n, _ in pairs]
    blob = "\n".join(labels + details)
    missing = [word for word in must if word not in blob]
    top = root.get("children") or []
    lines = [
        f"## {row['id']} 【{row['category']}】{row['title']}",
        f"中心：{root.get('label')}　节点 {len(pairs)}　深度 "
        f"{max(d for _, d in pairs)}　引用覆盖 {row['fingerprint']['ref_coverage']}　"
        f"覆盖 {row['fingerprint']['must_rate']}　缺 {missing or '无'}",
        f"一级分支（{len(top)}）：" + " ／ ".join((n.get('label') or '').strip() for n in top),
        "每支直属子节点数：" + str([len(n.get('children') or []) for n in top]),
    ]
    dup: dict[str, int] = {}
    for label in labels:
        if label:
            dup[label] = dup.get(label, 0) + 1
    repeated = {k: v for k, v in dup.items() if v > 1}
    if repeated:
        lines.append("重复 label：" + "；".join(f"{k}×{v}" for k, v in list(repeated.items())[:8]))
    long_labels = [label for label in labels if len(label) > 22]
    if long_labels:
        lines.append("超长 label：" + "；".join(long_labels[:6]))
    # 抽检：一级分支各取 1 条 detail，再加 2 条二级的
    picks: list[tuple[str, str, int]] = []
    for node, depth in pairs:
        label = (node.get("label") or "").strip()
        detail = (node.get("detail") or "").strip()
        if depth == 1 and detail:
            picks.append((label, detail, depth))
    for node, depth in pairs:
        if depth == 2 and node.get("detail"):
            picks.append(((node.get("label") or "").strip(),
                          (node.get("detail") or "").strip(), depth))
    lines.append("抽检 detail：")
    for label, detail, depth in picks[:6]:
        lines.append(f"  - [L{depth}] {label}｜{detail[:120]}")
    empty_detail = sum(1 for detail in details if not detail)
    lines.append(f"无 detail 的节点：{empty_detail}/{len(details)}")
    return "\n".join(lines)


def main() -> int:
    samples = {s["id"]: s for s in json.loads(SAMPLES.read_text(encoding="utf-8"))["samples"]}
    rows = [json.loads(line) for line in (DATA / "acc_structure.jsonl")
            .read_text(encoding="utf-8").splitlines() if line]
    latest = {}
    for row in rows:
        latest[row["id"]] = row
    for sid in sorted(latest):
        row = latest[sid]
        path = DATA / "acc_trees" / f"{sid}.json"
        if not row.get("ok") or not path.exists():
            print(f"## {sid} 【{row['category']}】{row['title']}\n未产出树："
                  f"{json.dumps(row.get('error'), ensure_ascii=False)}\n")
            continue
        content = json.loads(path.read_text(encoding="utf-8"))
        print(card(row, content, samples[sid]["must"]))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
