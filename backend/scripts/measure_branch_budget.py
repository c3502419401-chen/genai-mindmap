"""临时测量脚本（不入库）：节点预算进提示词，会不会把一级分支数一起压掉。

三格，只动一个数：

    A   NODE_SCALE 关                    node_limit = 80
    B1  NODE_SCALE 开（材料 424 字算出 10）  node_limit = 10
    B2  NODE_SCALE 开 + NODE_CAP=80       node_limit = 80（人为钉回 80）

B1 与 B2 的差别**只在那一个数**，所以它是"预算进提示词"这一条因果链最干净的对照：
A 与 B1 之间还隔着整个开关，B1 与 B2 之间只隔着配额。

材料固定为今天实测的那两份 openalex 摘要（210 + 214 = 424 字）。
一次跑完三格，交替顺序，把模型/网络的漂移同时摊到三格上。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
for extra in (BACKEND, Path(__file__).resolve().parent):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from measure_graph_outline import (  # noqa: E402
    _fill_env_from_registry,
    coverage as coverage_of,
    tree_stats,
)
from measure_scale import _depth_hist, _text_budget, _run_once  # noqa: E402

# 今天实测的那两份：`filename like '%碳中和%'` 的 openalex 摘要。
SOURCE_IDS = (
    "3484e8db-2add-4166-8076-a956bc015a8e",  # 高速铁路节能发展助力碳中和碳达峰 210 字
    "58c95354-dc28-482c-8ee9-158a5b25a968",  # 碳中和、碳达峰的绿色低碳新型工业园区的建设 214 字
)
TOPIC = "碳中和"

ARMS = {
    # 现状：开关关着，预算恒为 80
    "A": {"scale": False, "cap": None, "pool": False},
    # 旧口径：伸缩开着，但预算是"总量池 + 前序裁剪"（`MINDMAP_NODE_POOL=1`）
    "B": {"scale": True, "cap": None, "pool": True},
    # 修法：伸缩开着，预算按支摊（默认）
    "C": {"scale": True, "cap": None, "pool": False},
}


def _row(key: str, index: int, outcome: dict, sources: list, material_chars: int) -> dict:
    if not outcome["ok"]:
        return {"grid": key, "sample": index, "ok": False, "why": outcome["content"]}
    content, data = outcome["content"], outcome["data"]
    stats = tree_stats(content)
    depth = _depth_hist(content)
    text = _text_budget(content)
    cover = coverage_of(content, sources, [])
    branches = data.get("branches") or []
    return {
        "grid": key,
        "sample": index,
        "ok": True,
        "node_limit": data.get("node_limit"),
        "planned_branches": data.get("planned_branches"),
        # 落树后的一级分支数（`data["branches"]` 与画布上看到的是同一个东西）
        "branches": len(branches),
        "branch_titles": [item.get("label") if isinstance(item, dict) else item
                          for item in branches],
        "nodes": stats["nodes"],
        # 模型**自己写的**节点数（裁剪之前）：`nodes + cut_nodes`。这一个数是区分
        # "模型少写"与"被裁掉"的唯一证据——裁剪前有 39 个节点、裁完 10 个、只剩 1 支，
        # 那是 `_clip_tree` 干的，不是模型选的。
        "before_clip": stats["nodes"] + (data.get("cut_nodes") or 0),
        "nodes_per_branch": round(stats["nodes"] / len(branches), 1) if branches else None,
        "depth": depth["depth"],
        "depth_hist": depth["depth_hist"],
        "cut_nodes": data.get("cut_nodes"),
        "partial": data.get("partial"),
        "detail_total": text["detail_total"],
        "detail_avg": text["detail_avg"],
        "detail_min": text["detail_min"],
        "detail_empty": text["detail_empty"],
        "compression": round(material_chars / text["detail_total"], 2)
        if text["detail_total"] else None,
        "point_coverage": round(cover["point_coverage"], 3),
        "chars_coverage": round(cover["chars_coverage"], 3),
        "retrieval_mode": (data.get("retrieval") or {}).get("mode"),
        "seconds": outcome["seconds"],
        "model_calls": outcome.get("model_calls"),
        "codes": [item["code"] for item in (data.get("diagnostics") or [])],
    }


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--grids", default="A,B1,B2")
    parser.add_argument("--branches", type=int, default=6)
    parser.add_argument("--topic", default="",
                        help="留空 = 薄批用固定主题、中批/大批用批次自带的主题")
    parser.add_argument("--batch", default="small", choices=("small", "medium", "large"))
    parser.add_argument("--arm", default="b", choices=("a", "b"))
    parser.add_argument("--external", action="store_true",
                        help="打开 MINDMAP_EXTERNAL_KNOWLEDGE（外部检索补材料）")
    parser.add_argument("--save", default="")
    args = parser.parse_args(argv)

    _fill_env_from_registry()
    os.environ.setdefault("MINDMAP_DB_PATH", str(BACKEND / "data" / "mindmaps.db"))
    os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "1" if args.external else "0"
    os.environ["MINDMAP_RERANK"] = "0"
    os.environ["MINDMAP_SECTION_SLICE"] = "0"
    # 父任务要求：显式关掉明细检索，减少变量（这一段与分支数无关，但它会多一次
    # 调用、改第三段的输入，进而改 κ）。
    os.environ["MINDMAP_DETAIL_RETRIEVAL"] = "0"

    from app.knowledge import library

    sources = [library.load(sid) for sid in SOURCE_IDS]
    assert all(s is not None for s in sources), "库里缺这两份材料"
    topic = args.topic or TOPIC
    if args.batch != "small":
        # 中批/大批直接复用 `measure_scale._batches()`：那三批的选材逻辑与历史报告
        # 绑在一起，这里另挑一批就等于换了个实验。
        from measure_scale import _batches

        picked = _batches()[args.batch]
        sources = picked["sources"]
        topic = args.topic if args.topic else picked["topic"]
    material_chars = sum(len(s.text or "") for s in sources)
    print(f"材料 {len(sources)} 份 / {material_chars} 字："
          f"{[ (len(s.text or ''), s.filename) for s in sources ]}", flush=True)
    print(f"主题 {topic}｜请求 {args.branches} 条一级分支｜arm {args.arm}｜"
          f"外部检索 {'开' if args.external else '关'}｜格 {args.grids}｜各 {args.repeat} 次",
          flush=True)

    grids = [item for item in args.grids.split(",") if item]
    rows: list[dict] = []
    for index in range(1, args.repeat + 1):
        for key in grids:
            arm = ARMS[key]
            # `_run_once` 不碰 NODE_POOL（它只设 NODE_SCALE / NODE_CAP / DETAIL_SCALE），
            # 所以这一格由这里显式管：关着的新口径要能证明它没被宿主机变量污染。
            os.environ["MINDMAP_NODE_POOL"] = "1" if arm["pool"] else "0"
            print(f"\n=== {key} 第 {index} 次（scale={arm['scale']} "
                  f"pool={arm['pool']} cap={arm['cap']}）===", flush=True)
            outcome = await _run_once(
                topic=topic, branches=args.branches, sources=sources,
                scale=arm["scale"], arm=args.arm, cap=arm["cap"], detail_scale=False)
            row = _row(key, index, outcome, sources, material_chars)
            rows.append(row)
            if not row["ok"]:
                print(f"失败：{row['why']}", flush=True)
                continue
            print(
                f"上限 {row['node_limit']}｜骨架 {row['planned_branches']} 支 → "
                f"落树 {row['branches']} 支｜节点 {row['nodes']}｜"
                f"裁掉 {row['cut_nodes']}｜层 {row['depth']} {row['depth_hist']}｜"
                f"说明 {row['detail_total']:,} 字（均 {row['detail_avg']}）｜"
                f"κ {row['compression']}｜覆盖 {row['point_coverage']:.1%}｜"
                f"{row['seconds']}s", flush=True)
            print(f"    一级：{row['branch_titles']}", flush=True)

    if args.save:
        Path(args.save).write_text(
            json.dumps({"topic": topic, "batch": args.batch, "arm": args.arm,
                        "material_chars": material_chars,
                        "branches": args.branches, "rows": rows},
                       ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\n已写入 {args.save}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
