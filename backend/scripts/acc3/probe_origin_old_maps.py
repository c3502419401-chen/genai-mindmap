"""一次性：拿库里真实的图当"老数据"样本，验证 `origin` 的读回与不崩。

只读：不写库、不删、不改。跑法（在 backend 下）：
    python scripts/acc3/probe_origin_old_maps.py
"""
from __future__ import annotations

from collections import Counter

from app.agent import shapes
from app.mindmaps import repository


def main() -> None:
    records = repository.list_maps()
    print(f"库里的图：{len(records)} 张")
    origins: Counter[str] = Counter()
    shapes_seen: Counter[str] = Counter()
    for record in records:
        content = record.content
        shapes_seen[type(content).__name__] += 1
        for node, _depth in shapes.walk_depths(content):
            origins[node.origin] += 1
    print(f"形状分布：{dict(shapes_seen)}")
    print(f"节点来历分布（按节点）：{dict(origins)}")
    total = sum(origins.values())
    print(f"节点总数：{total}")


if __name__ == "__main__":
    main()
