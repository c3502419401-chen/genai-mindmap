"""四格：**节点配额 × 材料清单**。同一批材料上只动这两件事，其余全钉住。

## 它回答的问题

两条杠杆各有实测，而且**都是买一样丢一样**：

    只动配额（80 → 32）  κ 0.51 → 1.16–1.39 ✓   ρ 23–34% → 16–19%（−7~15pt）✗
    只开清单（80 上）    κ 0.583 → 0.537 ✗      ρ +3.2pt ✓、α 16.9% → 24.2% ✓

上一轮做的人自己标了「两条一起上才有可能同时守住 κ 与 ρ —— 但这是推论，本轮没做
联合实验」。这个脚本补的就是那一格：

    格 1  配额 32 + 清单关   格 2  配额 32 + 清单开
    格 3  配额 48 + 清单关   格 4  配额 48 + 清单开

**要验的假设**：配额把节点压到 32 时 ρ 掉 7–15pt，是因为分支落在模板词上（金融风控 /
智能制造），那些分支本来就指不到材料；清单开着时分支落在真实篇目上（电力负荷预测 /
地质与地球科学），同样 32 个节点，每支都有具体篇目可写，ρ 也许不用掉那么多。

**反过来的判决**：若格 2 的 ρ 与格 1 没差别，则损失来自"节点太少写不下"，与分支落在
哪无关——那时只能二选一。

## 为什么四格在同一进程里交替

这组指标有 ±5 的采样噪声（上一轮 A 臂重跑给出 37 与 33 条说明）。分组跑会把模型/
网络随时间的漂移算进格差，交替跑把它摊到四格上。所以 `--repeat 3` 是**轮**数：
第 1 轮跑完四格，再第 2 轮——不是"每格连跑 3 次"。

## 指标

    κ  压缩比        Σdetail / 材料字数（越大越省）
    ρ  要点覆盖      8-gram 句覆盖（`measure_graph_outline.coverage`）
    α  锚定率        `source_refs` 区间并集 / 材料字数
    ④  分支数 / 节点数 以及有没有撞上限被裁（`TREE_CLIPPED_TO_INITIAL`）
    ⑤  耗时
    ⑦  分支与材料的贴合  一级分支 label 能否在材料原文里逐字找到
                        （`faithfulness` 的逐字 / 分段 / 4-gram 三个口径）
                        —— 格 1 与格 2 的差别就在"分支落在哪"，这一列把因果接上

## 固定住的开关（否则量到的是别的东西）

    MINDMAP_SECTION_SLICE=0      12 章 → 12 次调用，变量太多（用户明确要求关）
    MINDMAP_GRAPH_OUTLINE=0      第三档骨架会按批差异化触发，是混淆变量
    MINDMAP_EXTERNAL_KNOWLEDGE=0 材料已定死，不许再补
    MINDMAP_RERANK=0             少一次网络往返，只影响选题
    MINDMAP_NODE_SCALE           不设（关）：配额由 `MINDMAP_NODE_CAP` 显式给

## 用法

    python scripts/measure_quad.py --repeat 3            # 12 次运行（四格 × 3 轮）
    python scripts/measure_quad.py --cells 32:off,32:on  # 只跑两格
    python scripts/measure_quad.py --report data/scale-quad-medium.json   # 只出表
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
    faithfulness,
    tree_stats,
)
from measure_scale import (  # noqa: E402
    _batches,
    _depth_hist,
    _materials_probe,
    _run_once,
    _text_budget,
)

#: 四格的顺序 = 表格的阅读顺序。48 是中间值：32 上 ρ 掉太多、80 上 κ 不够，
#: 48 可能是甜点——所以它在表里紧跟 32，而不是按"配额 × 清单"的笛卡尔序排。
CELLS = ((32, "off"), (32, "on"), (48, "off"), (48, "on"))
CELL_KEYS = {f"{cap}:{inv}": (cap, inv) for cap, inv in CELLS}


def _cell_of(name: str) -> tuple[int, bool]:
    if name not in CELL_KEYS:
        raise SystemExit(f"未知的格：{name}（可选 {', '.join(CELL_KEYS)}）")
    cap, inventory = CELL_KEYS[name]
    return cap, inventory == "on"


def _mean(values: list) -> float | None:
    values = [value for value in values if isinstance(value, (int, float))]
    return round(sum(values) / len(values), 4) if values else None


def _summary(runs: list[dict], cells: list[str]) -> dict:
    """每格一行均值。**均值之外一律给原值**——12 次运行不足以支撑区间估计。"""
    out = {}
    for cell in cells:
        rows = [row for row in runs if row.get("cell") == cell and row.get("ok")]
        out[cell] = {
            "runs": len(rows),
            "nodes": _mean([row["nodes"] for row in rows]),
            "branches": _mean([row["branches"] for row in rows]),
            "compression": _mean([row["compression"] for row in rows]),
            "point_coverage": _mean([row["point_coverage"] for row in rows]),
            "chars_coverage": _mean([row["chars_coverage"] for row in rows]),
            "faithful_hit": _mean([row["faithfulness"]["ratio"] for row in rows]),
            "faithful_segment": _mean([row["faithfulness"]["segment_ratio"] for row in rows]),
            "faithful_ngram": _mean([row["faithfulness"]["ngram_ratio"] for row in rows]),
            "seconds": _mean([row["seconds"] for row in rows]),
            "cut": sum(1 for row in rows if row.get("cut")),
            "node_limit": sorted({row.get("node_limit") for row in rows}),
            "inventory_chars": sorted({row.get("inventory_chars") for row in rows}),
        }
    return out


def _pct(value, digits: int = 1) -> str:
    return "-" if not isinstance(value, (int, float)) else f"{value:.{digits}%}"


def _print_report(report: dict) -> None:
    """12 行明细 + 四格均值 + 分支贴合明细。"""
    cells = report["cells"]
    rows = [row for row in report["runs"] if row.get("ok")]
    print("\n## 明细（每格每轮）\n")
    print("| 格 | 轮 | 配额 | 清单 | 节点 | 支 | κ 压缩比 | ρ 覆盖 | α 锚定 "
          "| 贴合(逐字) | 裁 | 秒 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        print(f"| {row['cell']} | {row['sample']} | {row['node_limit']} "
              f"| {'开' if row['inventory'] else '关'} | {row['nodes']} | {row['branches']} "
              f"| {row['compression']} | {_pct(row['point_coverage'])} "
              f"| {_pct(row['chars_coverage'])} "
              f"| {row['faithfulness']['hit']}/{row['faithfulness']['branches']}"
              f"（{_pct(row['faithfulness']['ratio'])}） "
              f"| {'是' if row['cut'] else '否'} | {row['seconds']}s |")

    print("\n## 四格均值\n")
    print("| 格 | 配额 | 清单 | 节点 | 支 | κ 压缩比 | ρ 覆盖 | α 锚定 "
          "| 贴合逐字 | 贴合分段 | 贴合4gram | 撞上限被裁 | 秒 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for cell in cells:
        item = report["summary"][cell]
        cap, inventory = _cell_of(cell)
        print(f"| {cell} | {cap} | {'开' if inventory else '关'} | {item['nodes']} "
              f"| {item['branches']} | {item['compression']} "
              f"| {_pct(item['point_coverage'])} | {_pct(item['chars_coverage'])} "
              f"| {_pct(item['faithful_hit'])} | {_pct(item['faithful_segment'])} "
              f"| {_pct(item['faithful_ngram'])} "
              f"| {item['cut']}/{item['runs']} | {item['seconds']}s |")

    print("\n## 格 1 vs 格 2（同一配额，只差分支落在哪）\n")
    one, two = report["summary"].get("32:off"), report["summary"].get("32:on")
    if one and two:
        delta = None
        if isinstance(one["point_coverage"], float) and isinstance(two["point_coverage"], float):
            delta = (two["point_coverage"] - one["point_coverage"]) * 100
        print(f"ρ  {_pct(one['point_coverage'])} → {_pct(two['point_coverage'])}"
              f"（{'+' if (delta or 0) >= 0 else ''}{delta:.1f}pt）" if delta is not None
              else "ρ  数据不足")
        print(f"κ  {one['compression']} → {two['compression']}"
              f"｜α  {_pct(one['chars_coverage'])} → {_pct(two['chars_coverage'])}")
        print(f"贴合（逐字）{_pct(one['faithful_hit'])} → {_pct(two['faithful_hit'])}")

    print("\n## 一级分支与材料的贴合（每格举例）\n")
    for cell in cells:
        picks = [row for row in rows if row["cell"] == cell]
        if not picks:
            continue
        print(f"**{cell}**")
        for row in picks:
            marks = "、".join(
                f"{item['label']}{'✓' if item['hit'] else '✗'}"
                for item in row["faithfulness"]["rows"])
            print(f"  - 第 {row['sample']} 轮：{marks}")
        print()


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3, help="轮数（每轮跑完四格）")
    parser.add_argument("--branches", type=int, default=6,
                        help="与上一轮清单 A/B 同值（6），四格一致")
    parser.add_argument("--cells", default=",".join(f"{cap}:{inv}" for cap, inv in CELLS))
    parser.add_argument("--save", default="data/scale-quad-medium.json")
    parser.add_argument("--report", default="", help="只读 JSON 出表，不跑模型")
    parser.add_argument("--no-trees", action="store_true", help="不保存每棵树")
    args = parser.parse_args(argv)

    if args.report:
        report = json.loads(Path(args.report).read_text(encoding="utf-8")
                            if Path(args.report).is_absolute()
                            else (BACKEND / args.report).read_text(encoding="utf-8"))
        _print_report(report)
        return 0

    cells = [name.strip() for name in args.cells.split(",") if name.strip()]
    for name in cells:
        _cell_of(name)

    _fill_env_from_registry()
    os.environ.setdefault("MINDMAP_DB_PATH", str(BACKEND / "data" / "mindmaps.db"))
    os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "0"
    os.environ["MINDMAP_RERANK"] = "0"
    os.environ.pop("MINDMAP_SECTION_SLICE", None)
    os.environ.setdefault("MINDMAP_NEO4J_URI", "bolt://127.0.0.1:7687")
    os.environ.setdefault("MINDMAP_NEO4J_USER", "neo4j")

    batch = _batches()["medium"]
    probe = _materials_probe(batch["sources"])
    print(f"=== medium === {probe['sources']} 份 / {probe['chars']:,} 字"
          f"｜章节 {probe['outline_entries']} 条"
          f"｜主题：{batch['topic']}", flush=True)
    print(f"四格：{cells}｜每格 {args.repeat} 轮｜branches={args.branches}", flush=True)

    from app.knowledge import graph_store

    entity_names: list[str] = []
    if graph_store.configured():
        entities, _ = graph_store.subgraph(
            entities=200, links=400, scope=[source.id for source in batch["sources"]])
        entity_names = [item.name for item in entities]

    report: dict = {
        "batch": "medium", "branches": args.branches, "repeat": args.repeat,
        "cells": cells, "material": {k: probe[k] for k in
                                     ("sources", "chars", "outline_entries",
                                      "outline_usable", "filenames")},
        "topic": batch["topic"], "runs": [],
    }
    save_path = BACKEND / args.save if not Path(args.save).is_absolute() else Path(args.save)

    for index in range(1, args.repeat + 1):
        for cell in cells:
            cap, inventory = _cell_of(cell)
            print(f"\n=== 第 {index} 轮 · 格 {cell}（配额 {cap} / 清单 "
                  f"{'开' if inventory else '关'}）===", flush=True)
            outcome = await _run_once(
                topic=batch["topic"], branches=args.branches, sources=batch["sources"],
                scale=False, arm="b" if inventory else "a", cap=cap)
            if not outcome["ok"]:
                print(f"失败：{outcome['content']}", flush=True)
                report["runs"].append({"cell": cell, "sample": index, "ok": False,
                                       "content": str(outcome["content"])})
                continue
            content = outcome["content"]
            data = outcome["data"]
            depth = _depth_hist(content)
            text = _text_budget(content)
            cover = coverage(content, batch["sources"], entity_names)
            stats = tree_stats(content)
            faithful = faithfulness(content, batch["sources"])
            codes = [item["code"] for item in (data.get("diagnostics") or [])]
            row = {
                "cell": cell, "sample": index, "ok": True,
                "cap": cap, "inventory": inventory,
                "inventory_chars": outcome.get("inventory_chars"),
                "node_limit": data.get("node_limit"),
                "nodes": stats["nodes"], "branches": stats["branches"],
                "branch_titles": [child.label for child in content.root.children],
                "seconds": outcome["seconds"],
                **depth, **text, **cover,
                "compression": (round(probe["chars"] / text["detail_total"], 2)
                                if text["detail_total"] else None),
                "faithfulness": faithful,
                "capped": "NODE_CAP_APPLIED" in codes,
                "cut": "TREE_CLIPPED_TO_INITIAL" in codes,
                "partial": bool(data.get("partial")),
                "diagnostics": codes[:12],
            }
            report["runs"].append(row)
            print(f"节点 {row['nodes']}（{row['branches']} 支）｜上限 {row['node_limit']}"
                  f"｜说明 {row['detail_total']:,} 字｜**κ {row['compression']}**"
                  f"｜ρ {row['point_coverage']:.1%}｜α {row['chars_coverage']:.1%}"
                  f"｜贴合 {faithful['hit']}/{faithful['branches']}"
                  f"｜裁 {'是' if row['cut'] else '否'}｜{row['seconds']}s", flush=True)
            print(f"    清单 {row['inventory_chars']} 字｜分支 {row['branch_titles']}",
                  flush=True)
            if not args.no_trees:
                tree_path = save_path.with_suffix(f".{cap}{'on' if inventory else 'off'}"
                                                  f".{index}.json")
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
