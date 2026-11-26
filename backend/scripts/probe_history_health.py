"""离线体检：库里每一张已生成导图的真实缺陷分布。**零模型调用**。

回答"自检回路有没有东西可修"这个前提问题：如果历史产物里跨支重复 / 同级重复 /
层级过深本来就很少，那"让模型回头检查一遍"买到的东西就有限，代价再便宜也不值。

口径与运行时完全同源：`_cross_branch_duplicates`（生成收尾那条诊断）与
`optimization.service.check()`（收尾那 5 组结构规则）。不是另写一套判据。

    python scripts/probe_history_health.py            # 主库
    python scripts/probe_history_health.py --db X.db  # 指定库
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DEFAULT_DB = BACKEND / "data" / "mindmaps.db"


def _load(text: str | None) -> dict:
    try:
        value = json.loads(text or "{}")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _rows(db: Path) -> list[sqlite3.Row]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return list(c.execute(
        "select run_id, created_at, status, mode, request, candidate "
        "from runs where candidate is not null and candidate != '' "
        "order by created_at"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    # 两代架构的分界：`170aaaa`（2026-11-16）删掉整树生成、只留分片；
    # 2026-11-18 把一次生成加回来（`docs/ONE-CALL.md`）。于是 runs 表自带一组
    # 天然对照：分界之前跑的是"每支一个子代理 + 确定性验收 + 只补不合格支"，
    # 之后跑的是"一次写完"。
    parser.add_argument("--split", default="2026-11-18",
                        help="两代架构的分界日期（含当日起算新一代）")
    args = parser.parse_args()

    from app.contracts.mindmap import MindMapContent
    from app.agent.tools import _cross_branch_duplicates
    from app.optimization.service import check

    rows = _rows(args.db)
    print(f"{args.db.name}：{len(rows)} 条有产物的运行")

    kept: list[dict] = []
    skip = Counter()
    for row in rows:
        candidate = _load(row["candidate"])
        if candidate.get("diagram_type", "tree") != "tree":
            skip[f"非树图({candidate.get('diagram_type')})"] += 1
            continue
        if row["mode"] != "model":
            skip[f"mode={row['mode']}"] += 1
            continue
        try:
            content = MindMapContent.model_validate(candidate)
        except Exception as exc:  # noqa: BLE001 — 老产物可能与现契约不符
            skip[f"契约校验失败({type(exc).__name__})"] += 1
            continue
        duplicates = _cross_branch_duplicates(content.root)
        diagnostics = check(content)
        codes = Counter(item.code for item in diagnostics)
        nodes = list(_walk(content.root))
        details = [len((node.detail or "").strip()) for node in nodes[1:]]
        refs = sum(len(node.source_refs) for node in nodes)
        kept.append({
            "created_at": row["created_at"][:19],
            "run": row["run_id"][:8],
            "with_material": bool(candidate.get("source_usage")),
            # 「有材料」有两个来路，差别很大：随请求上传的（`request.source_ids`）
            # 与系统自己检索补进来的。用户提方案时说的"用户现在常见的是带材料"，
            # 指的是前者；而实测里前者的次数远少于后者。
            "material_source": (
                "随请求" if _load(row["request"]).get("source_ids") else "外部检索补"),
            "nodes": len(nodes),
            "width": len(content.root.children),
            "depth": _depth(content.root),
            "detail_avg": round(sum(details) / max(1, len(details)), 1),
            "detail_empty": sum(1 for value in details if value == 0),
            "refs": refs,
            "duplicates": len(duplicates),
            "duplicate_nodes": sum(len(places) for _, places in duplicates),
            "codes": dict(codes),
            "diagnostics": len(diagnostics),
        })

    print("跳过：", dict(skip) if skip else "无")
    if not kept:
        return 1

    print(f"\n== 可量 {len(kept)} 张树图（含材料 {sum(1 for k in kept if k['with_material'])} 张） ==")
    by_source = Counter(item["material_source"] for item in kept if item["with_material"])
    print("  带材料那批的来路：", dict(by_source))

    print("\n== 按天（有材料 / 无材料） ==")
    per_day: Counter[tuple[str, str]] = Counter()
    for item in kept:
        per_day[(item["created_at"][:10], "有材料" if item["with_material"] else "无材料")] += 1
    for day in sorted({key[0] for key in per_day}):
        print(f"  {day}  有材料 {per_day[(day, '有材料')]:>2}  无材料 {per_day[(day, '无材料')]:>2}")

    print("\n== 跨支重复（与生成收尾同一条判据） ==")
    dirty = [item for item in kept if item["duplicates"]]
    print(f"有跨支重复的图：{len(dirty)}/{len(kept)}"
          f"（{len(dirty) / len(kept):.0%}）")
    print(f"重复节点总数：{sum(item['duplicate_nodes'] for item in kept)}")
    for item in sorted(dirty, key=lambda row: -row["duplicate_nodes"])[:10]:
        print(f"  {item['created_at']} {item['run']} 节点 {item['nodes']}"
              f" 分支 {item['width']} 重复 {item['duplicate_nodes']} 处"
              f"（材料={item['with_material']}）")

    print("\n== 收尾 5 组结构规则的命中分布 ==")
    totals: Counter[str] = Counter()
    for item in kept:
        for code, count in item["codes"].items():
            totals[code] += count
    for code, count in totals.most_common():
        hit = sum(1 for item in kept if item["codes"].get(code))
        print(f"  {code:<26} 命中 {count:>4} 次，涉及 {hit:>2}/{len(kept)} 张图")

    print("\n== 规模与说明 ==")
    depths = Counter(item["depth"] for item in kept)
    print("  层级深度分布：", dict(sorted(depths.items())))
    print(f"  节点数 中位 {_median([item['nodes'] for item in kept])}，"
          f"一级分支 中位 {_median([item['width'] for item in kept])}")
    print(f"  detail 平均 中位 {_median([item['detail_avg'] for item in kept])} 字；"
          f"detail 全空的图 {sum(1 for item in kept if item['detail_empty'] == item['nodes'] - 1)}")
    print(f"  引用 中位 {_median([item['refs'] for item in kept])} 条；"
          f"零引用 {sum(1 for item in kept if item['refs'] == 0)}/{len(kept)} 张")

    material = [item for item in kept if item["with_material"]]
    if material:
        print("\n== 只算带材料的那些（引用本该落地） ==")
        print(f"  {len(material)} 张：引用中位 {_median([item['refs'] for item in material])} 条，"
              f"零引用 {sum(1 for item in material if item['refs'] == 0)} 张；"
              f"跨支重复 {sum(1 for item in material if item['duplicates'])} 张；"
              f"detail 平均中位 {_median([item['detail_avg'] for item in material])} 字")

    # ---- 两代架构的分组对照 ------------------------------------------------
    print(f"\n== 两代架构对照（分界 {args.split}） ==")
    old = [item for item in kept if item["created_at"][:10] < args.split]
    new = [item for item in kept if item["created_at"][:10] >= args.split]
    print(f"{'':<22}{'分片（每支一个子代理）':>22}{'一次写完':>14}")
    for label, key in (("图数", None), ("跨支重复图数", "duplicates"),
                       ("重复节点总数", "duplicate_nodes")):
        if key is None:
            print(f"{label:<22}{len(old):>22}{len(new):>14}")
            continue
        if key == "duplicates":
            left = sum(1 for item in old if item["duplicates"])
            right = sum(1 for item in new if item["duplicates"])
            print(f"{label:<22}{left:>22}{right:>14}"
                  f"   （占比 {_share(left, len(old))} vs {_share(right, len(new))}）")
            continue
        print(f"{label:<22}{sum(item[key] for item in old):>22}"
              f"{sum(item[key] for item in new):>14}")
    print(f"{'节点数中位':<22}{_median([i['nodes'] for i in old]):>22}"
          f"{_median([i['nodes'] for i in new]):>14}")
    print(f"{'一级分支中位':<22}{_median([i['width'] for i in old]):>22}"
          f"{_median([i['width'] for i in new]):>14}")
    print(f"{'detail 中位':<22}{_median([i['detail_avg'] for i in old]):>22}"
          f"{_median([i['detail_avg'] for i in new]):>14}")
    print(f"{'引用中位':<22}{_median([i['refs'] for i in old]):>22}"
          f"{_median([i['refs'] for i in new]):>14}")
    print(f"{'SINGLE_CHILD 次数':<22}"
          f"{sum(i['codes'].get('SINGLE_CHILD', 0) for i in old):>22}"
          f"{sum(i['codes'].get('SINGLE_CHILD', 0) for i in new):>14}")
    return 0


def _share(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "—"


def _walk(node, depth: int = 0):
    yield node
    for child in node.children:
        yield from _walk(child, depth + 1)


def _depth(node, level: int = 1) -> int:
    if not node.children:
        return level
    return max(_depth(child, level + 1) for child in node.children)


def _median(values: list[float]) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


if __name__ == "__main__":
    raise SystemExit(main())
