"""量 20 棵验收树里含 ` / ` 的 label：多少、长什么样、拆开两边各是什么。

为什么要有：`_joined_label` 的第二档（拼起来 ≤ 24 字就拼）是"可读"这一维的唯一
系统性成因，但"该拼"与"不该拼"的判据不能凭 3 个坏例拍。这里把全部 ` / ` 节点
摆出来，让每一条都能被看见。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.optimization.repair import LABEL_JOINER, READABLE_LABEL_CHARS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "data" / "acc_trees"


def walk(node: dict, path: str = ""):
    yield node, path
    for child in node.get("children") or []:
        yield from walk(child, f"{path}/{node.get('id')}")


def main() -> None:
    files = sorted(ROOT.glob("S*.json"))
    total = 0
    rows: list[tuple[str, str, str, str, int]] = []
    for file in files:
        tree = json.loads(file.read_text(encoding="utf-8"))
        for node, _path in walk(tree["root"]):
            total += 1
            label = (node.get("label") or "").strip()
            if LABEL_JOINER not in label:
                continue
            left, _, right = label.partition(LABEL_JOINER)
            rows.append((file.stem, node.get("id", ""), left, right, len(label)))

    print(f"节点总数 {total}；含 ` / ` 的 {len(rows)} 个；可读线 {READABLE_LABEL_CHARS}")
    print(f"其中长度 > 可读线的 {sum(1 for r in rows if r[4] > READABLE_LABEL_CHARS)} 个")
    print()
    for file, nid, left, right, size in rows:
        print(f"{file} {nid:<10} [{size:>2}] {left}  ||  {right}")


if __name__ == "__main__":
    main()
