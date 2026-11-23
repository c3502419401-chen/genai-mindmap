"""实测：**现在**的布局在真图上到底哪里不清晰。

## 为什么先量

"实时调整节点关系与布局"这件事的第一个风险是**修一个没人遇过的问题**。所以这个
脚本不提出任何方案，只做两件事：

  1. 从库里把**真图**取出来（`runs.candidate` 是每次生成落地的那棵树，
     `mindmaps.content` 是用户保存过的图），按内容指纹去重；
  2. 把每张图压成"几何输入"（`root` + `edges` + `diagram_type`）导出成一份样本
     JSON，交给**前端**用真实的 `layoutDiagram` 算五个指标。

## 为什么几何必须在前端算

三套几何（tidy / fishbone / flow）只有一份实现，在 `frontend/src/canvas/layout/`。
在 Python 里重写一遍等于造第二份口径：重写的那份会先跑通、然后慢慢与真几何分叉，
而它量出来的结论会被当成"线上就是这样"。所以这里的边界是硬的：

    本脚本 = 取数与内容形状统计（Python）
    几何与指标 = 真实现（TypeScript，见 `frontend/tests/layout-clarity.spec.ts`）

## 用法（在 `backend/` 下）

    python scripts/measure_layout_clarity.py
    python scripts/measure_layout_clarity.py --db data/mindmaps.db --out scripts/_layout_clarity_sample.json

导出的样本是**临时产物**（`_` 前缀，不进提交）：它只有取数价值，重跑一次就能重建。
报告里的表与结论会手抄进 `docs/`，那是要留档的部分。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DEFAULT_DB = "data/mindmaps.db"
DEFAULT_OUT = "scripts/_layout_clarity_sample.json"

# 几何只读这几个字段。`detail` / `note` / `style` / `pos` 一个都不进样本：
# 它们不参与布局（`NODE_SIZE` 是固定的），带进来只会把样本放大十倍。
KEEP_NODE = ("id", "label", "children", "role", "source_refs")


def _slim_refs(refs) -> list[dict]:
    """引用只留"出自哪份材料、哪一段"。几何不用它，但 §二 的合并实测要用它核
    `source_refs` 是不是真的取了并集。"""
    out = []
    for ref in refs or []:
        if isinstance(ref, dict):
            out.append({key: ref.get(key) for key in ("source_id", "page", "start", "end")
                        if ref.get(key) is not None})
    return out


def _slim_node(node: dict) -> dict:
    slim = {"id": node.get("id"), "label": node.get("label")}
    if node.get("role"):
        slim["role"] = node["role"]
    refs = _slim_refs(node.get("source_refs"))
    if refs:
        slim["source_refs"] = refs
    children = node.get("children") or []
    slim["children"] = [_slim_node(child) for child in children if isinstance(child, dict)]
    return slim


def _count(node: dict) -> int:
    return 1 + sum(_count(child) for child in node.get("children") or [])


def _depth(node: dict) -> int:
    children = node.get("children") or []
    return 1 + max((_depth(child) for child in children), default=0)


def _shape(title: str, payload: dict) -> dict:
    """一份产物 → 几何输入。三种图型的 `root` 都在顶层（流程图的合成根 / 鱼骨的中心
    主题），所以这里不需要按图型分叉——分派在 `layoutDiagram` 里，前端只有那一处。"""
    content = payload
    root = content.get("root")
    if not isinstance(root, dict):
        raise ValueError("没有 root")
    diagram_type = content.get("diagram_type") or "tree"
    return {
        "title": content.get("title") or title,
        "diagram_type": diagram_type,
        "root": _slim_node(root),
        # 流程图的分层要看附加边（dagre 把"依赖"也算进层级），其余图型忽略它。
        "edges": [
            {"id": edge.get("id"), "source": edge.get("source"), "target": edge.get("target")}
            for edge in (content.get("edges") or []) if isinstance(edge, dict)
        ],
        "nodes": _count(root),
        "depth": _depth(root),
        "branches": [_count(child) for child in root.get("children") or []],
        "relations": len(content.get("relations") or []),
    }


def _fingerprint(shape: dict) -> str:
    """按**内容**去重，不按 run id：同一个主题跑两次往往只差几个字，而这里是"图有
    多少张"的统计，不是"生成了多少次"。用 slim 之后的形状算指纹，于是同一张图的
    重复 run 会撞在同一个键上。"""
    blob = json.dumps({key: shape[key] for key in ("title", "diagram_type", "root")},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _collect(db: Path) -> tuple[list[dict], dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    samples: list[dict] = []
    seen: dict[str, str] = {}
    counts = Counter()
    skipped = Counter()

    def add(source: str, title: str, raw: str) -> None:
        try:
            shape = _shape(title, json.loads(raw))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            skipped[type(error).__name__] += 1
            return
        fingerprint = _fingerprint(shape)
        if fingerprint in seen:
            counts["duplicate"] += 1
            return
        seen[fingerprint] = source
        counts[shape["diagram_type"]] += 1
        samples.append({"key": fingerprint, "source": source, **shape})

    for row in conn.execute("select id, content from mindmaps"):
        add(f"map:{row['id']}", "", row["content"] or "")
    for row in conn.execute(
            "select run_id, request, candidate from runs "
            "where candidate is not null and length(candidate) > 2"):
        title = ""
        try:
            title = (json.loads(row["request"] or "{}") or {}).get("topic") or ""
        except (TypeError, ValueError):
            title = ""
        add(f"run:{row['run_id']}", title, row["candidate"])

    conn.close()
    return samples, {"counts": dict(counts), "skipped": dict(skipped)}


def _report(samples: list[dict]) -> None:
    nodes = sorted(sample["nodes"] for sample in samples)
    if not nodes:
        print("库里没有可用的图。")
        return
    def percentile(share: float) -> int:
        index = min(len(nodes) - 1, int(round(share * (len(nodes) - 1))))
        return nodes[index]

    print(f"图 {len(samples)} 张（按内容指纹去重）")
    print(f"节点数  最小 {nodes[0]} / 中位 {percentile(0.5)} / 90 分位 {percentile(0.9)} / 最大 {nodes[-1]}")
    print(f"节点数 ≥ 40 的：{sum(1 for n in nodes if n >= 40)} 张；"
          f"≥ 80 的：{sum(1 for n in nodes if n >= 80)} 张")
    widest = sorted(samples, key=lambda item: -max(item["branches"], default=0))[:3]
    for sample in widest:
        print(f"  最宽的一级分支：{max(sample['branches'], default=0)} 个节点 —— {sample['title'][:24]}")
    depths = Counter(sample["depth"] for sample in samples)
    print("深度分布：" + "、".join(f"{depth} 层×{count}" for depth, count in sorted(depths.items())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    db = Path(args.db)
    if not db.is_absolute():
        db = BACKEND / db
    if not db.exists():
        print(f"没有这个库：{db}")
        return 2

    samples, stats = _collect(db)
    print(f"库：{db}")
    print(f"按图型：{stats['counts']}")
    if stats["skipped"]:
        print(f"跳过（读不出来的产物）：{stats['skipped']}")
    _report(samples)

    out = Path(args.out)
    if not out.is_absolute():
        out = BACKEND / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "db": str(db.relative_to(BACKEND)).replace("\\", "/"),
        "samples": samples,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n样本已写入 {out.relative_to(BACKEND)}（{len(samples)} 张，临时产物，不进提交）")
    print("几何指标：cd frontend ; npx vitest run tests/layout-clarity.spec.ts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
