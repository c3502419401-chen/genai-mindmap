"""结构合规的自动判据：不依赖人评的那一半「结构合理性」。

为什么单列：`docs/PLAN.md:128` 把指标叫「结构合理性」，但它只规定了 4 个维度与
「双人盲评」，没说什么算合格。人评那 4 维里，「覆盖」「无捏造」是内容维度，
「层次」「可读」才是结构维度。结构维度能用确定性判据算出来，内容维度只能人评。

所以这里把「结构」这一半做成可复算的判据（A–F），人评只管内容那两维。这样
「结构合规率」是一个谁都能重跑出来的数，而不是一句"我们觉得结构还行"。

判据与取值（全部先写死，改动要同时改报告）：
    A 一级分支数 3–8          少了像列表，多了读不动（可读性参考值 80 在下面 E 之外）
    B 树的深度 3–6            2 层没有信息量，7 层以上是目录不是图
    C 一级分支没有空的        空支在界面上就是一个点，用户点开什么都没有
    D 完全重复的 label ≤1     重复在一张图上就是信息丢失
    E 无 detail 的节点 ≤5%    说明是导图的可读性的一半
    F label 都在契约上限内    超长会被截断成半句（实测出现过「最短路径 Dijkstra 与」）

用法：
    python scripts/_structure_audit.py
    python scripts/_structure_audit.py --json data/acc_structure_audit.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DATA = BACKEND / "data"
LABEL_LIMIT = 24


def walk(node, depth=1):
    yield node, depth
    for child in node.get("children") or []:
        yield from walk(child, depth + 1)


def audit(content: dict) -> dict:
    root = content.get("root") or {}
    pairs = list(walk(root))
    labels = [(n.get("label") or "").strip() for n, _ in pairs]
    details = [(n.get("detail") or "").strip() for n, _ in pairs]
    top = root.get("children") or []
    depth = max((d for _, d in pairs), default=0)
    dup = len([label for label in labels if label]) - len({label for label in labels if label})
    empty_top = sum(1 for node in top if not (node.get("children") or []))
    no_detail = sum(1 for detail in details if not detail)
    long_labels = [label for label in labels if len(label) > LABEL_LIMIT]
    checks = {
        "A 一级分支 3-8": 3 <= len(top) <= 8,
        "B 深度 3-6": 3 <= depth <= 6,
        "C 无空一级分支": empty_top == 0,
        "D 重复 label ≤1": dup <= 1,
        "E 无 detail ≤5%": (no_detail / len(details)) <= 0.05 if details else False,
        "F label ≤%d 字" % LABEL_LIMIT: not long_labels,
    }
    return {
        "nodes": len(pairs), "depth": depth, "top": len(top),
        "dup": dup, "empty_top": empty_top, "no_detail": no_detail,
        "long_labels": long_labels[:5],
        "checks": checks,
        "pass": all(checks.values()),
        "failed": [key for key, value in checks.items() if not value],
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)
    rows = [json.loads(line) for line in (DATA / "acc_structure.jsonl")
            .read_text(encoding="utf-8").splitlines() if line]
    latest = {}
    for row in rows:
        latest[row["id"]] = row
    report = []
    for sid in sorted(latest):
        row = latest[sid]
        path = DATA / "acc_trees" / f"{sid}.json"
        if not row.get("ok") or not path.exists():
            report.append({"id": sid, "category": row["category"], "pass": False,
                           "failed": ["未产出树"], "must_rate": None,
                           "ref_coverage": None})
            continue
        result = audit(json.loads(path.read_text(encoding="utf-8")))
        result.update({"id": sid, "category": row["category"],
                       "must_rate": row["fingerprint"]["must_rate"],
                       "ref_coverage": row["fingerprint"]["ref_coverage"]})
        report.append(result)
    for item in report:
        flag = "通过" if item["pass"] else "不通过"
        marks = "" if item["pass"] else "　失败项：" + "；".join(item["failed"])
        extra = ""
        if item.get("nodes"):
            extra = (f"　节点 {item['nodes']} 深 {item['depth']} 一级 {item['top']} "
                     f"重复 {item['dup']} 无说明 {item['no_detail']}"
                     f" 覆盖 {item['must_rate']} 引用 {item['ref_coverage']}")
        print(f"{item['id']} 【{item['category']}】{flag}{extra}{marks}")
    passed = sum(1 for item in report if item["pass"])
    print(f"\n结构合规 {passed}/{len(report)} = {round(passed / len(report), 3)}")
    ok_must = [item["must_rate"] for item in report if item.get("must_rate") is not None]
    if ok_must:
        print(f"内容覆盖（must 命中率）均值 {round(statistics.mean(ok_must), 3)}"
              f"　中位 {round(statistics.median(ok_must), 3)}"
              f"　≥0.7 的比例 {round(sum(1 for v in ok_must if v >= 0.7) / len(ok_must), 3)}")
    by_cat: dict[str, list[float]] = {}
    for item in report:
        if item.get("must_rate") is not None:
            by_cat.setdefault(item["category"], []).append(item["must_rate"])
    for category, values in sorted(by_cat.items()):
        print(f"  {category}: n={len(values)} 覆盖均值 {round(statistics.mean(values), 3)}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print(f"明细写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
