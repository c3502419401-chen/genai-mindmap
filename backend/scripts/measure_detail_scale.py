"""每段说明写多长：**固定 80–150** vs **按材料伸缩**。膨胀的最后一半。

## 它回答的问题

上一轮把**节点数**接上了材料，说明长度仍是常量，于是同一批材料上还是配额驱动：

    48 节点 × 130 字（现状实测均长）= 6,240 字 · 材料 5,616 字 → κ 0.90（还是膨胀）

这一轮只动**每段说明写多长**（`MINDMAP_DETAIL_SCALE`），配额与清单全钉住：

    A  配额 48 + 清单开 + 固定 80–150      现状（`data/scale-quad-medium.json` 格 4）
    B  配额 48 + 清单开 + 字数伸缩          κ 应向上、ρ 不许掉超过 5pt
    C  配额伸缩 + 字数伸缩（无 cap）        两个常量都动：小批的对照组

C 只对小批跑。理由是中批在 `MINDMAP_NODE_SCALE` 下给 32 个节点、每节点支撑 175 字，
区间被可读性上限夹回 80–150——**与 A 逐字节相同**，跑了也量不到东西；小批则相反：
结构下限 10 个节点 × 75 字地板 = 750 字，对 492 字材料是 κ 0.66，而"配额 48"那一格
是 3,600 字、κ 0.14。**同一份材料、同一句话，两个后果差 4.8 倍**——C 就是把这件事
摆到台面上的那一格。

## 为什么 A/B 在小批上也必须跑

小批（492 字 / 1 份）是膨胀最厉害的一格：配额 48 时每节点只有 10 字材料支撑，而说明
仍要写 80–150 字。它是"下限是质量线还是配额"这个问题的**唯一判据**：中批上下限都够用，
量不出冲突。

## 指标

    κ  压缩比      材料字数 / Σdetail（越大越省；目标：中批 ≥1.0、小批 ≥0.5）
    ρ  要点覆盖    8-gram 句覆盖（`measure_graph_outline.coverage`）——不许掉超过 5pt
    α  锚定率      `source_refs` 区间并集 / 材料字数
    ④  说明均长 / 最短 / 空
    ⑤  节点数 / 一级分支数
    ⑥  耗时
    ⑦  这一次第三段实际要求的区间（`data.detail.chars`）——A 臂为空（= 80–150）

## 固定住的开关（与上一轮四格同条件）

    MINDMAP_SECTION_SLICE=0      12 章 → 12 次调用，变量太多
    MINDMAP_GRAPH_OUTLINE=0      第三档骨架是混淆变量
    MINDMAP_EXTERNAL_KNOWLEDGE=0 材料已定死，不许再补
    MINDMAP_RERANK=0             少一次网络往返
    MINDMAP_NODE_SCALE           仅 C 臂打开；A/B 由 `MINDMAP_NODE_CAP=48` 显式给定

## 用法

    python scripts/measure_detail_scale.py --repeat 3
    python scripts/measure_detail_scale.py --repeat 3 --arms A,B,C --batches small
    python scripts/measure_detail_scale.py --report data/detail-scale-ab.json   # 只出表
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from measure_graph_outline import (  # noqa: E402
    _fill_env_from_registry,
    coverage,
    tree_stats,
)
from measure_scale import (  # noqa: E402
    _batches,
    _materials_probe,
    _run_once,
    _text_budget,
)

#: 三臂。`cap=None` 表示这一臂不设人为配额（交给 `MINDMAP_NODE_SCALE`）。
ARMS: dict[str, dict] = {
    "A": {"detail_scale": False, "cap": 48, "node_scale": False,
          "label": "配额 48 + 固定 80–150"},
    "B": {"detail_scale": True, "cap": 48, "node_scale": False,
          "label": "配额 48 + 字数伸缩"},
    "C": {"detail_scale": True, "cap": None, "node_scale": True,
          "label": "配额伸缩 + 字数伸缩"},
}

#: C 臂只在哪几批上跑（理由见模块 docstring：中批上它与 A 逐字节相同）。
NODE_SCALE_BATCHES = ("small",)


def _cell(arm: str, batch: str) -> bool:
    return arm != "C" or batch in NODE_SCALE_BATCHES


def _mean(values: list) -> float | None:
    values = [value for value in values if isinstance(value, (int, float))]
    return round(sum(values) / len(values), 4) if values else None


def _summary(runs: list[dict], cells: list[tuple[str, str]]) -> dict:
    """每格一行均值。均值之外一律给原值——12 次运行不足以支撑区间估计。"""
    out = {}
    for batch, arm in cells:
        rows = [row for row in runs
                if row.get("ok") and row.get("batch") == batch and row.get("arm") == arm]
        out[f"{batch}:{arm}"] = {
            "runs": len(rows),
            "nodes": _mean([row["nodes"] for row in rows]),
            "branches": _mean([row["branches"] for row in rows]),
            "detail_total": _mean([row["detail_total"] for row in rows]),
            "detail_avg": _mean([row["detail_avg"] for row in rows]),
            "detail_min": _mean([row["detail_min"] for row in rows]),
            "compression": _mean([row["compression"] for row in rows]),
            "point_coverage": _mean([row["point_coverage"] for row in rows]),
            "chars_coverage": _mean([row["chars_coverage"] for row in rows]),
            "seconds": _mean([row["seconds"] for row in rows]),
            "cut": sum(1 for row in rows if row.get("cut")),
            "node_limit": sorted({row.get("node_limit") for row in rows}),
            "detail_span": [row.get("detail_span") for row in rows],
        }
    return out


def _pct(value, digits: int = 1) -> str:
    return "-" if not isinstance(value, (int, float)) else f"{value:.{digits}%}"


def _print_report(report: dict) -> None:
    rows = [row for row in report["runs"] if row.get("ok")]
    print("\n## 明细（每格每轮）\n")
    print("| 批 | 臂 | 轮 | 节点 | 支 | 说明总字 | 均长 | 最短 | κ 压缩比 | ρ 覆盖 "
          "| α 锚定 | 区间 | 秒 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        span = row.get("detail_span") or {}
        span_text = f"{span.get('min')}–{span.get('max')}" if span else "80–150"
        print(f"| {row['batch']} | {row['arm']} | {row['sample']} | {row['nodes']} "
              f"| {row['branches']} | {row['detail_total']:,} | {row['detail_avg']} "
              f"| {row['detail_min']} | {row['compression']} "
              f"| {_pct(row['point_coverage'])} | {_pct(row['chars_coverage'])} "
              f"| {span_text} | {row['seconds']}s |")

    print("\n## 每格均值\n")
    print("| 批 | 臂 | 组 | 节点 | 支 | 说明总字 | 均长 | 最短 | κ | ρ | α | 秒 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for key, item in report["summary"].items():
        batch, arm = key.split(":")
        print(f"| {batch} | {arm} | {ARMS[arm]['label']} | {item['nodes']} "
              f"| {item['branches']} | {item['detail_total']:,} | {item['detail_avg']} "
              f"| {item['detail_min']} | {item['compression']} "
              f"| {_pct(item['point_coverage'])} | {_pct(item['chars_coverage'])} "
              f"| {item['seconds']}s |")

    print("\n## A → B（同一配额、同一清单，只差每段写多长）\n")
    for batch in report["batches"]:
        base = report["summary"].get(f"{batch}:A")
        scaled = report["summary"].get(f"{batch}:B")
        if not base or not scaled:
            continue
        dk = (scaled["compression"] or 0) - (base["compression"] or 0)
        dr = ((scaled["point_coverage"] or 0) - (base["point_coverage"] or 0)) * 100
        print(f"**{batch}**：κ {base['compression']} → {scaled['compression']}"
              f"（{'+' if dk >= 0 else ''}{dk:.2f}）"
              f"｜ρ {_pct(base['point_coverage'])} → {_pct(scaled['point_coverage'])}"
              f"（{'+' if dr >= 0 else ''}{dr:.1f}pt）"
              f"｜α {_pct(base['chars_coverage'])} → {_pct(scaled['chars_coverage'])}"
              f"｜均长 {base['detail_avg']} → {scaled['detail_avg']}"
              f"｜说明总字 {base['detail_total']:,} → {scaled['detail_total']:,}")
    small_c = report["summary"].get("small:C")
    if small_c:
        base = report["summary"].get("small:A")
        print(f"**small 的 C 臂**（两个常量都动）：κ {small_c['compression']}"
              f"（A 是 {base['compression'] if base else '-'}）"
              f"｜节点 {small_c['nodes']}｜说明总字 {small_c['detail_total']:,}"
              f"｜ρ {_pct(small_c['point_coverage'])}"
              f"｜区间 {small_c['detail_span']}")


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3, help="轮数（每轮跑完所有臂）")
    parser.add_argument("--branches", type=int, default=6,
                        help="与上一轮四格同值（6），所有臂一致")
    parser.add_argument("--batches", default="medium,small")
    parser.add_argument("--arms", default="A,B,C")
    parser.add_argument("--save", default="data/detail-scale-ab.json")
    parser.add_argument("--report", default="", help="只读 JSON 出表，不跑模型")
    parser.add_argument("--no-trees", action="store_true", help="不保存每棵树")
    args = parser.parse_args(argv)

    if args.report:
        path = Path(args.report)
        if not path.is_absolute():
            path = BACKEND / path
        _print_report(json.loads(path.read_text(encoding="utf-8")))
        return 0

    batches = [name.strip() for name in args.batches.split(",") if name.strip()]
    arms = [name.strip().upper() for name in args.arms.split(",") if name.strip()]
    for name in arms:
        if name not in ARMS:
            raise SystemExit(f"未知的臂：{name}（可选 {', '.join(ARMS)}）")
    for name in batches:
        if name not in ("small", "medium", "large"):
            raise SystemExit(f"未知的批次：{name}")

    _fill_env_from_registry()
    os.environ.setdefault("MINDMAP_DB_PATH", str(BACKEND / "data" / "mindmaps.db"))
    os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "0"
    os.environ["MINDMAP_RERANK"] = "0"
    os.environ.pop("MINDMAP_SECTION_SLICE", None)
    os.environ.setdefault("MINDMAP_NEO4J_URI", "bolt://127.0.0.1:7687")
    os.environ.setdefault("MINDMAP_NEO4J_USER", "neo4j")

    all_batches = _batches()
    cells = [(batch, arm) for batch in batches for arm in arms if _cell(arm, batch)]
    probes = {}
    for batch in batches:
        probe = _materials_probe(all_batches[batch]["sources"])
        probes[batch] = probe
        print(f"=== {batch} === {probe['sources']} 份 / {probe['chars']:,} 字"
              f"｜章节 {probe['outline_entries']} 条"
              f"｜主题：{all_batches[batch]['topic']}", flush=True)
    print(f"臂：{arms}｜批次：{batches}｜每格 {args.repeat} 轮"
          f"｜branches={args.branches}｜格：{[f'{b}:{a}' for b, a in cells]}", flush=True)

    from app.knowledge import graph_store

    entities_by_batch: dict[str, list[str]] = {}
    for batch in batches:
        names: list[str] = []
        if graph_store.configured():
            entities, _ = graph_store.subgraph(
                entities=200, links=400,
                scope=[source.id for source in all_batches[batch]["sources"]])
            names = [item.name for item in entities]
        entities_by_batch[batch] = names

    report: dict = {
        "branches": args.branches, "repeat": args.repeat,
        "arms": {name: ARMS[name] for name in arms},
        "batches": {batch: {k: probes[batch][k] for k in
                            ("sources", "chars", "outline_entries", "outline_usable",
                             "filenames")} for batch in batches},
        "runs": [],
    }
    save_path = Path(args.save) if Path(args.save).is_absolute() else BACKEND / args.save

    # 交替跑而不是每格连跑：这组指标有 ±5 的采样噪声，分组跑会把模型/网络随时间的
    # 漂移算进格差（上一轮四格同一条理由）。顺序是"每轮把每一格各跑一次"。
    for index in range(1, args.repeat + 1):
        for batch, arm in cells:
            facts = ARMS[arm]
            print(f"\n=== 第 {index} 轮 · {batch} · {arm} 臂（{facts['label']}）===",
                  flush=True)
            outcome = await _run_once(
                topic=all_batches[batch]["topic"], branches=args.branches,
                sources=all_batches[batch]["sources"], scale=facts["node_scale"],
                arm="b", cap=facts["cap"], detail_scale=facts["detail_scale"])
            if not outcome["ok"]:
                print(f"失败：{outcome['content']}", flush=True)
                report["runs"].append({"batch": batch, "arm": arm, "sample": index,
                                       "ok": False, "content": str(outcome["content"])})
                continue
            content = outcome["content"]
            data = outcome["data"]
            text = _text_budget(content)
            cover = coverage(content, all_batches[batch]["sources"],
                             entities_by_batch[batch])
            stats = tree_stats(content)
            codes = [item["code"] for item in (data.get("diagnostics") or [])]
            chars = probes[batch]["chars"]
            row = {
                "batch": batch, "arm": arm, "sample": index, "ok": True,
                "material_chars": chars,
                "node_cap": facts["cap"], "node_scale": facts["node_scale"],
                "detail_scale": facts["detail_scale"],
                "node_limit": data.get("node_limit"),
                "nodes": stats["nodes"], "branches": stats["branches"],
                "branch_titles": [child.label for child in content.root.children],
                "seconds": outcome["seconds"],
                **text, **cover,
                "compression": (round(chars / text["detail_total"], 2)
                                if text["detail_total"] else None),
                "detail_span": outcome.get("detail_span"),
                "scaled_applied": "DETAIL_SCALE_APPLIED" in codes,
                "node_scale_applied": "NODE_SCALE_APPLIED" in codes,
                "capped": "NODE_CAP_APPLIED" in codes,
                "cut": "TREE_CLIPPED_TO_INITIAL" in codes,
                "partial": bool(data.get("partial")),
                "diagnostics": codes[:12],
            }
            report["runs"].append(row)
            span = row["detail_span"] or {}
            span_text = f"{span.get('min')}–{span.get('max')}" if span else "80–150"
            print(f"节点 {row['nodes']}（{row['branches']} 支）｜上限 {row['node_limit']}"
                  f"｜区间 {span_text}"
                  f"｜说明 {row['detail_total']:,} 字（均 {row['detail_avg']}，"
                  f"最短 {row['detail_min']}，空 {row['detail_empty']}）"
                  f"｜**κ {row['compression']}**"
                  f"｜ρ {row['point_coverage']:.1%}｜α {row['chars_coverage']:.1%}"
                  f"｜{row['seconds']}s", flush=True)
            print(f"    分支 {row['branch_titles']}", flush=True)
            if not args.no_trees:
                tree_path = save_path.with_suffix(f".{batch}-{arm}.{index}.json")
                tree_path.write_text(
                    json.dumps(content.model_dump(), ensure_ascii=False, indent=1),
                    encoding="utf-8")

    report["summary"] = _summary(report["runs"], cells)
    save_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    _print_report(report)
    print(f"\n报告已写入 {save_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
