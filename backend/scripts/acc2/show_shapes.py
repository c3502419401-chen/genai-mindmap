"""把 `p6_shapes.json` 里的坐标打成人能读的表（P6 判读用）。"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    data = json.loads((HERE / "p6_shapes.json").read_text(encoding="utf-8"))
    for kind in ("tree", "fishbone", "flow"):
        block = data.get(kind) or {}
        print("==", kind, "nodes=", block.get("nodeCount"), "edges=", block.get("edgeCount"))
        for node in block.get("nodes") or []:
            print("   {:10s} x={:>9} y={:>8}  {}".format(
                node["id"], round(node["x"], 1), round(node["y"], 1), node["label"][:20]))
        print("   edge types:",
              sorted({edge["type"] for edge in block.get("edges") or []}))
        if block.get("error"):
            print("   ERROR:", block["error"])
        print()
    print("page_errors:", data.get("page_errors"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
