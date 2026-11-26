"""实测：生成收尾接上 `repair()` 之后，**真实产物**上到底修了几处。

与 `measure_repair.py` 的分工：那个脚本量的是"用户点一键修复会发生什么"（读
`mindmaps`，用户保存过的图，含手工改过的）；这个量的是"生成收尾顺手修会发生
什么"（读 `runs.candidate`，**模型这一次写下的原树**）。两个分母不能混：前者里
有探针树与手工编辑，后者才是这条接线的作用对象。

量的东西（零模型调用）：

  1. 三条可修规则在真实产物上的命中分布（`DUPLICATE_LABEL` / `SINGLE_CHILD` /
     `HEADING_ONLY_BRANCH`）——`docs/SUBAGENT-VS-SELFCHECK.md` 报的是 37/79 张
     图有 `SINGLE_CHILD`、共 116 次；这里按**当前库**重算一遍（分母会漂）。
  2. 修了几步、跳过几处、节点少了多少。
  3. 幂等：对修完的树再跑一次 `repair()` 必须是 0 步。
  4. **修复动不了跨支重复**（那张表里的另一条指标）：`_cross_branch_duplicates`
     的判据是"不同父、逐字相同"，而 `DUPLICATE_LABEL` 只比同级——两条规则的分母
     不重叠。修前修后各量一次，用来回答"确定性修复能不能替代自检回路"。

用法（在 `backend/` 下）：

    python scripts/measure_generate_repair.py
    python scripts/measure_generate_repair.py --limit 20 --save scripts/_gen_repair.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DEFAULT_DB = BACKEND / "data" / "mindmaps.db"

#: 这份报告只认这几条（`optimization/service.check()` 的七种码里与收尾有关的那几条）。
WATCHED = ("DUPLICATE_LABEL", "SINGLE_CHILD", "HEADING_ONLY_BRANCH",
           "NEAR_DUPLICATE_LABEL", "SHALLOW_BRANCH", "MISSING_DETAIL")


def _load(db: Path, limit: int) -> list[tuple[str, dict]]:
    """`(run_id, content)`，按内容指纹去重（同一次生成的重复落库只算一张图）。"""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    seen: set[str] = set()
    out: list[tuple[str, dict]] = []
    for row in conn.execute(
            "select run_id, candidate from runs where candidate is not null "
            "and candidate != '' order by created_at"):
        try:
            payload = json.loads(row["candidate"])
        except ValueError:
            continue
        if not isinstance(payload.get("root"), dict):
            continue
        if payload.get("diagram_type", "tree") != "tree":
            continue
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append((row["run_id"], payload))
        if limit and len(out) >= limit:
            break
    return out


def _codes(diagnostics) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in diagnostics:
        if item.code in WATCHED:
            counts[item.code] = counts.get(item.code, 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--save", type=Path, default=None)
    args = parser.parse_args()

    from app.agent.tools import _cross_branch_duplicates
    from app.contracts.mindmap import MindMapContent
    from app.optimization.repair import AUTO_REPAIRABLE_CODES, repair
    from app.optimization.service import check

    rows = _load(args.db, args.limit)
    print(f"库：{args.db}\n可读的树：{len(rows)} 张（按内容去重、只算 tree）\n")

    hit_trees = {code: 0 for code in WATCHED}
    hit_total = {code: 0 for code in WATCHED}
    steps_total = 0
    skipped_total = 0
    changed_trees = 0
    nodes_before = 0
    nodes_after = 0
    dup_before = 0
    dup_after = 0
    dup_trees_before = 0
    dup_trees_after = 0
    not_idempotent: list[str] = []
    per_tree: list[dict] = []

    for run_id, payload in rows:
        try:
            content = MindMapContent.model_validate(payload)
        except Exception:  # noqa: BLE001 — 老产物可能与现契约不符
            continue
        before = _codes(check(content))
        for code, count in before.items():
            hit_trees[code] += 1
            hit_total[code] += count
        dup_before_here = _cross_branch_duplicates(content.root)
        dup_before += sum(len(places) for _, places in dup_before_here)
        dup_trees_before += 1 if dup_before_here else 0

        outcome = repair(content, codes=AUTO_REPAIRABLE_CODES)
        again = repair(outcome.content, codes=AUTO_REPAIRABLE_CODES)
        if again.steps:
            not_idempotent.append(run_id)

        dup_after_here = _cross_branch_duplicates(outcome.content.root)
        dup_after += sum(len(places) for _, places in dup_after_here)
        dup_trees_after += 1 if dup_after_here else 0

        steps_total += outcome.steps
        skipped_total += len(outcome.skipped)
        if outcome.steps:
            changed_trees += 1
        nodes_before += sum(1 for _ in _walk(content))
        nodes_after += sum(1 for _ in _walk(outcome.content))
        if outcome.steps or outcome.skipped:
            per_tree.append({
                "run_id": run_id,
                "nodes": sum(1 for _ in _walk(content)),
                "steps": outcome.steps,
                "codes": {item.code: sum(1 for other in outcome.applied
                                        if other.code == item.code)
                          for item in outcome.applied},
                "skipped": len(outcome.skipped),
            })

    print("三条可修规则的命中（修复前，逐张累加）：")
    for code in ("DUPLICATE_LABEL", "SINGLE_CHILD", "HEADING_ONLY_BRANCH"):
        print(f"  {code:<20} {hit_total[code]:>4} 次，涉及 {hit_trees[code]}/{len(rows)} 张图")
    print("只报不修的几条（对照）：")
    for code in ("NEAR_DUPLICATE_LABEL", "SHALLOW_BRANCH", "MISSING_DETAIL"):
        print(f"  {code:<20} {hit_total[code]:>4} 次，涉及 {hit_trees[code]}/{len(rows)} 张图")

    print(f"\n修复：{steps_total} 步，涉及 {changed_trees}/{len(rows)} 张图；"
          f"跳过 {skipped_total} 处")
    print(f"节点：{nodes_before} → {nodes_after}（−{nodes_before - nodes_after}）")
    print(f"幂等：{'全部成立' if not not_idempotent else '反例 ' + '、'.join(not_idempotent)}")

    print("\n跨支重复（`_cross_branch_duplicates`，**修复动不了它**）：")
    print(f"  修前：{dup_trees_before}/{len(rows)} 张图有，共 {dup_before} 处")
    print(f"  修后：{dup_trees_after}/{len(rows)} 张图有，共 {dup_after} 处")

    if per_tree:
        per_tree.sort(key=lambda row: -row["steps"])
        print("\n改动最大的 5 张：")
        for row in per_tree[:5]:
            detail = "、".join(f"{code} {count}" for code, count in row["codes"].items())
            print(f"  {row['nodes']:>4} 节点  步 {row['steps']:>3}"
                  f"（{detail or '无'}）  跳过 {row['skipped']}  {row['run_id'][:8]}")

    if args.save:
        args.save.write_text(json.dumps({
            "trees": len(rows),
            "hit_total": hit_total, "hit_trees": hit_trees,
            "steps": steps_total, "skipped": skipped_total,
            "trees_changed": changed_trees,
            "nodes": {"before": nodes_before, "after": nodes_after},
            "cross_branch_duplicates": {
                "before": {"trees": dup_trees_before, "total": dup_before},
                "after": {"trees": dup_trees_after, "total": dup_after},
            },
            "per_tree": per_tree,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n写进 {args.save}")
    return 0


def _walk(content):
    stack = [content.root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.children)


if __name__ == "__main__":
    raise SystemExit(main())
