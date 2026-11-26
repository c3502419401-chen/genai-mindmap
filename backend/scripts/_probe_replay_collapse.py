"""把 20 棵验收树里那 74 个"压链拼出来的"节点**逆向还原成压链前的一层两节点**，
再用当前 `repair()` 重跑一遍，量出改动之后画布上还剩几个 ` / `。

为什么必须还原而不是直接重跑：`acc_trees` 是压链**之后**的树，`repair()` 幂等，
直接重跑什么都不做。还原的准确性由两个已知条件保证：

  · 含 ` / ` 的节点必然是走第二档的产物（走一档／第三档的都不含它）；
  · 第二档产物的 label 逐字等于 `外层 + " / " + 内层`，所以两段原文可无损拆回。
  · 只处理 `id` 跳代（`seg > depth-1`）的节点——那是压链吃掉了外层的硬证据；
    模型自己写的并列（`S12/s2-3`）不还原。

还原时外层的 detail / refs 不复制：它们早就被并进了内层，复制一遍会让
`_join_text` 拼出重复正文，那是实验假象不是规则行为。

跑法：`python scripts/_probe_replay_collapse.py [--old]`
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.contracts.mindmap import MindMapContent  # noqa: E402
from app.optimization.repair import (  # noqa: E402
    DEFERRED_LABEL_PREFIX,
    LABEL_JOINER,
    repair,
)

ROOT = Path(__file__).resolve().parents[1] / "data" / "acc_trees"


def walk(node: dict, depth: int = 1):
    yield node, depth
    for child in node.get("children") or []:
        yield from walk(child, depth + 1)


def seg_count(node_id: str) -> int:
    return len([p for p in re.split(r"-", node_id or "") if p])


def unfurl(node: dict, depth: int = 1) -> dict:
    """递归把每一条"压链产物"拆回两层。"""
    children = [unfurl(child, depth + 1) for child in node.get("children") or []]
    label = (node.get("label") or "").strip()
    collapsed = (
        LABEL_JOINER in label
        and depth > 1
        and seg_count(node.get("id", "")) > depth - 1
    )
    if not collapsed:
        return {**node, "children": children}

    outer_label, _, inner_label = label.partition(LABEL_JOINER)
    inner = {**node, "label": inner_label, "children": children}
    outer = {
        "id": f"{node.get('id', 'x')}~outer",
        "label": outer_label,
        "children": [inner],
        "expandable": node.get("expandable", "unknown"),
        "collapsed": False,
    }
    return outer


def count(node: dict) -> tuple[int, int, int, int]:
    """(节点数, 含 ` / `, detail 以「原上级：」开头, 叶子里含 ` / `)"""
    total = slash = deferred = leaf_slash = 0
    for item, _depth in walk(node):
        total += 1
        label = (item.get("label") or "").strip()
        if LABEL_JOINER in label:
            slash += 1
            if not item.get("children"):
                leaf_slash += 1
        if (item.get("detail") or "").startswith(DEFERRED_LABEL_PREFIX):
            deferred += 1
    return total, slash, deferred, leaf_slash


def main() -> None:
    detail = "--detail" in sys.argv[1:]
    before_total = before_slash = before_deferred = 0
    rebuilt_total = 0
    after_total = after_slash = after_deferred = 0
    per_tree = []
    idempotent = True
    for file in sorted(ROOT.glob("S*.json")):
        tree = json.loads(file.read_text(encoding="utf-8"))
        b = count(tree["root"])
        rebuilt = {**tree, "root": unfurl(tree["root"])}
        content = MindMapContent.model_validate(rebuilt)
        result = repair(content)
        a = count(result.content.root.model_dump())
        if repair(result.content).applied:
            idempotent = False

        before_total += b[0]
        before_slash += b[1]
        before_deferred += b[2]
        rebuilt_total += count(rebuilt["root"])[0]
        after_total += a[0]
        after_slash += a[1]
        after_deferred += a[2]
        per_tree.append((file.stem, b, a, a[0] - b[0]))

    print(f"磁盘上的树（压链后）：节点 {before_total}，"
          f"含 ` / ` {before_slash}，原上级 {before_deferred}")
    print(f"逆向还原成压链前   ：节点 {rebuilt_total}"
          f"（+{rebuilt_total - before_total} = 每条压链补回一个外层）")
    print(f"跑完当前 repair    ：节点 {after_total}，"
          f"含 ` / ` {after_slash}，原上级 {after_deferred}")
    print(f"节点数是否回到原样：{'是' if after_total == before_total else '否——出现了额外的同级合并'}")
    print(f"二次 repair 是否幂等：{'是' if idempotent else '否'}")
    print()
    print(f"{'树':<5} {'原节点':>6} {'原/':>4} {'修后/':>5} {'原上级':>5}")
    for name, b, a, delta in per_tree:
        print(f"{name:<5} {b[0]:>6} {b[1]:>4} {a[1]:>5} {a[2]:>5}")

    if not detail:
        return
    print()
    print("== 逐条：改后是拼还是降级 ==")
    for file in sorted(ROOT.glob("S*.json")):
        tree = json.loads(file.read_text(encoding="utf-8"))
        rebuilt = {**tree, "root": unfurl(tree["root"])}
        content = MindMapContent.model_validate(rebuilt)
        result = repair(content)
        kept: dict[str, str] = {}

        def index(node: dict) -> None:
            kept[node.get("id", "")] = node.get("label", "")
            for child in node.get("children") or []:
                index(child)

        index(result.content.root.model_dump())
        for item, _depth in walk(tree["root"]):
            label = (item.get("label") or "").strip()
            if LABEL_JOINER not in label:
                continue
            if seg_count(item.get("id", "")) <= _depth - 1:
                continue
            if _depth <= 1:
                continue
            now = kept.get(item.get("id", ""), "?")
            fate = "拼" if LABEL_JOINER in now else "降级"
            print(f"{file.stem} {item.get('id', ''):<12} {fate}  {label}  →  {now}")


if __name__ == "__main__":
    main()
