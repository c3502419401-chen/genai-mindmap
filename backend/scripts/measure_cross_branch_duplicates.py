"""量 D 判据那 17 处"跨支重复 label"到底是什么，以及每一处该不该改。

背景（台账第 9 条）：`scripts/_structure_audit.py` 的 D 判据是

    dup = 全树非空 label 的个数 - 去重后的个数

**全局同名计数、不分层级、不看父**。于是三类东西都会进这个数：

    a 真重复      同一件事在两支下各写一遍（该合并/该用关系表达）
    b 交叉引用    同一批条目在两种视角下都出现（"课程内容"与"学时分配"都列章名）
                  —— 语义上不是重复，是同一个对象出现在两个上下文里
    c 误报        名字相同但指的是不同东西（分属不同的父）

这个脚本把每一处（每个"多出来的实例"）摊开成一行，带上父链、节点 id、detail 首句、
以及三个结构标记：

    same_parent   两处挂在同一个父下（`_cross_branch_duplicates` 不管这种，`check()`
                  的 `DUPLICATE_LABEL` 管）
    same_chain    两处在同一条祖先链上（父子同名）
    cross         父不同、也不在同一条链上 —— 这才是 `_cross_branch_duplicates` 的视野

**D 判据比 `_cross_branch_duplicates` 宽**：前者连 same_parent / same_chain 都算进去。
这一步先把"宽出来多少"量清楚，再谈 a/b/c。

======================================================================
三档判定（`--triage`，报告 §一的机器版）
======================================================================
a/b/c 不是靠"同名"定的，也不是靠 detail 像不像定的 —— 靠三条确定性规则，
在**旧树 17 处上逐条对得上人判**（`docs/CROSS-BRANCH-DUPLICATES.md` §一）：

    grey  不该算重复
          ① 两处在同一条祖先链上（父子同名）：那是"父节点既当条目又当分组"，
             `check()` 的层级规则管，不是跨支重复；
          ② 两处的父是**同一模板的不同实例**（"候选人 A" / "候选人 C"、"第一部分" /
             "第二部分"）：同名不同指。判据用父 label 的**单字 Jaccard ≥0.4**——
             2-gram 在这类上只有 0.2（"候选人A后端方向硕士" vs "候选人C算法方向博士"
             的 2-gram 交集只有「候选/选人/方向」），单字才看得出是同一个模板
             （实测 0.43）。0.4 这个阈值同时在反向筛掉别的组合：「实验穿插安排」vs
             「实验考核」只有 0.25、「共评审三个需求」vs「讨论要点」0.00。
    red   真重复：detail 的 2-gram 重叠 ≥0.5，两处讲的是同一件事（删一处不丢信息）。
    amber 交叉引用：其余 —— 跨支同名，但 detail 各讲各自视角下的事（范围清单 vs
          讨论记录、设计要求 vs 里程碑交付）。读者两支都要看得到，不该删。

`red` 才该计入 D 的红；`amber` 是提示（产品上该用 relations 指认）；`grey` 不该出现在
这个判据里。

用法：
    python scripts/measure_cross_branch_duplicates.py
    python scripts/measure_cross_branch_duplicates.py --triage      # 只打三档汇总
    python scripts/measure_cross_branch_duplicates.py --json data/cross_branch_duplicates.json
    python scripts/measure_cross_branch_duplicates.py --only S02
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if TYPE_CHECKING:  # 只在类型检查时导入：`app.agent.tools` 是重依赖链，见 `analyse`
    from app.contracts.mindmap import MindMapContent

DATA = BACKEND / "data"
TREES = DATA / "acc_trees"


def _walk(node, chain: tuple[str, ...] = ()):
    """(node, 父链) —— 父链不含自己。"""
    yield node, chain
    for child in getattr(node, "children", []) or []:
        yield from _walk(child, (*chain, node.label))


def _is_prefix(short: tuple[str, ...], long: tuple[str, ...]) -> bool:
    return len(short) < len(long) and long[: len(short)] == short


def classify(chain_a: tuple[str, ...], chain_b: tuple[str, ...]) -> str:
    if chain_a == chain_b:
        return "same_parent"
    if _is_prefix(chain_a, chain_b) or _is_prefix(chain_b, chain_a):
        return "same_chain"
    return "cross"


def _char_jaccard(a: str, b: str) -> float:
    """单字 Jaccard：判"两个父是不是同一个模板的不同实例"。

    2-gram 在这类上太低（见文件头），单字才看得出「候选人 A（后端方向，硕士）」与
    「候选人 C（算法方向，博士）」是同一张表格的两行。
    """
    sa, sb = set(_STRIP.sub("", a)), set(_STRIP.sub("", b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def triage(group: dict) -> tuple[str, str]:
    """`(red|amber|grey, 理由)` —— 规则见文件头「三档判定」。"""
    chains = [tuple(place["chain"]) for place in group["places"]]
    if all(chains[i] == chains[0] for i in range(len(chains))):
        return "grey", "同一父下的同级同名（`check()` 的 DUPLICATE_LABEL 管）"
    parents = [chain[-1] if chain else "根" for chain in chains]
    grand = [chain[:-1] if chain else () for chain in chains]
    same_grand = all(item == grand[0] for item in grand)
    if same_grand and len(parents) >= 2:
        pairs = [(parents[i], parents[j]) for i in range(len(parents))
                 for j in range(i + 1, len(parents))]
        if pairs and all(_char_jaccard(a, b) >= 0.4 for a, b in pairs):
            return "grey", "两处的父是同一模板的不同实例（同名不同指）"
    if _same_chain_pair(chains):
        return "grey", "两处在同一条祖先链上（父节点既当条目又当分组）"
    if group["overlap"] >= 0.5:
        return "red", "detail 讲同一件事（删一处不丢信息）"
    return "amber", "跨支同名，但两处各讲各自视角下的事（交叉引用）"


def _same_chain_pair(chains: list[tuple[str, ...]]) -> bool:
    for i in range(len(chains)):
        for j in range(i + 1, len(chains)):
            if _is_prefix(chains[i], chains[j]) or _is_prefix(chains[j], chains[i]):
                return True
    return False


_STRIP = re.compile(r"[\s，。；：、（）()「」“”\"'!？?.,;:—\-]+")


def overlap(a: str, b: str) -> float:
    """两段 detail 的 2-gram Jaccard：粗口径的"是不是同一段内容"。

    不用分词器：这一步只用来把 a（真重复：detail 讲同一件事）与 b（交叉引用：
    detail 各讲各自视角下的事）分开，2-gram 对中文短句已经够。数字与标点先抹平，
    否则「4 学时」在两处的写法差异会压低重叠。
    """
    ga = {a[i:i + 2] for i in range(len(a) - 1)} if len(a) > 1 else {a}
    gb = {b[i:i + 2] for i in range(len(b) - 1)} if len(b) > 1 else {b}
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def analyse(content: MindMapContent) -> dict:
    # 延迟导入：`app.agent.tools` 是一条重依赖链，而三档判定（`triage`）要能被用例
    # 单独导入——那个文件不该为了判一组同名去拖起整个生成链路。
    from app.agent.tools import _cross_branch_duplicates

    root = content.root
    places: dict[str, list[dict]] = {}
    for node, chain in _walk(root):
        label = (node.label or "").strip()
        key = re.sub(r"\s+", "", label)
        if not key:
            continue
        places.setdefault(key, []).append({
            "id": node.id,
            "label": label,
            "chain": list(chain),
            "depth": len(chain) + 1,
            "branch": chain[0] if chain else "",
            "detail": _STRIP.sub("", (node.detail or "").strip()),
            "raw_detail": (node.detail or "").strip(),
        })
    groups = []
    dup = 0
    for key, items in places.items():
        if len(items) < 2:
            continue
        dup += len(items) - 1
        kinds = []
        overlaps = []
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                kinds.append(classify(tuple(items[i]["chain"]), tuple(items[j]["chain"])))
                overlaps.append(overlap(items[i]["detail"], items[j]["detail"]))
        groups.append({
            "key": key,
            "count": len(items),
            "extra": len(items) - 1,
            "kinds": sorted(set(kinds)),
            "overlap": max(overlaps) if overlaps else 0.0,
            # 「哪两支」：两处的**父节点**名字（跨支时也就是两个不同的父）。
            "parents": [items[0]["chain"][-1] if items[0]["chain"] else "根"]
                       + [p["chain"][-1] if p["chain"] else "根" for p in items[1:]],
            "places": items,
        })
        band, why = triage(groups[-1])
        groups[-1]["band"] = band
        groups[-1]["because"] = why
    groups.sort(key=lambda g: (-g["extra"], g["key"]))
    cross = _cross_branch_duplicates(root)
    return {
        "dup": dup,
        "groups": groups,
        "cross_branch": [{"label": label, "parents": parents} for label, parents in cross],
        "cross_branch_nodes": sum(len(parents) for _, parents in cross),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", default="")
    parser.add_argument("--only", default="")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--triage", action="store_true",
                        help="只打三档（red/amber/grey）汇总与按三档重算的 A–F 合规率")
    parser.add_argument("--trees", default=str(TREES),
                        help="树目录（缺省 data/acc_trees；重跑产物用 "
                             "data/acc_trees-rerun-*）")
    args = parser.parse_args(argv)
    trees = Path(args.trees)
    ids = sorted(p.stem for p in trees.glob("S*.json"))
    if args.only:
        wanted = {s.strip().upper() for s in args.only.split(",")}
        ids = [sid for sid in ids if sid in wanted]
    report = []
    total_dup = total_groups = 0
    by_kind = {"same_parent": 0, "same_chain": 0, "cross": 0}
    for sid in ids:
        raw = json.loads((trees / f"{sid}.json").read_text(encoding="utf-8"))
        from app.contracts.mindmap import MindMapContent
        content = MindMapContent.model_validate(raw)
        result = analyse(content)
        result["id"] = sid
        report.append(result)
        total_dup += result["dup"]
        total_groups += len(result["groups"])
        for group in result["groups"]:
            if len(group["kinds"]) == 1:
                by_kind[group["kinds"][0]] += group["extra"]
            else:
                # 一组里既有跨支又有同链：按最"重"的那类记（cross > same_parent > same_chain）。
                for kind in ("cross", "same_parent", "same_chain"):
                    if kind in group["kinds"]:
                        by_kind[kind] += group["extra"]
                        break
        if (result["dup"] or args.verbose) and not args.triage:
            print(f"\n=== {sid}  D={result['dup']}  "
                  f"跨支判据节点数={result['cross_branch_nodes']} ===")
            for group in result["groups"]:
                print(f"  「{group['key']}」×{group['count']}"
                      f"（多出 {group['extra']}）　{'/'.join(group['kinds'])}"
                      f"　父=「{'」vs「'.join(group['parents'])}」"
                      f"　detail 重叠 {group['overlap']:.2f}　→ {group['band']}")
                for place in group["places"]:
                    print(f"     - [{place['id']}] 父链={' / '.join(place['chain']) or '根'}"
                          f"　detail={len(place['raw_detail'])} 字"
                          f"　「{place['raw_detail'][:40]}」")
    print(f"\n合计：D 判据 {total_dup} 处（{total_groups} 组）")
    print(f"  结构分类（按「多出的实例」记）：跨支 {by_kind['cross']}、"
          f"同父 {by_kind['same_parent']}、同链 {by_kind['same_chain']}")
    bands = {"≥0.5（同一段内容）": 0, "0.25–0.5（部分重叠）": 0, "<0.25（各讲各的）": 0}
    for item in report:
        for group in item["groups"]:
            value = group["overlap"]
            key = ("≥0.5（同一段内容）" if value >= 0.5
                   else "0.25–0.5（部分重叠）" if value >= 0.25 else "<0.25（各讲各的）")
            bands[key] += group["extra"]
    print("  detail 重叠分档：" + "；".join(f"{k} {v}" for k, v in bands.items()))
    print(f"  `_cross_branch_duplicates` 节点数合计 "
          f"{sum(item['cross_branch_nodes'] for item in report)}"
          f"（= 组数 × 2 时说明两套判据在旧树上完全重合）")

    # ---- 三档：这才是"该改几处"的口径 ----
    tally = {"red": 0, "amber": 0, "grey": 0}
    for item in report:
        for group in item["groups"]:
            tally[group["band"]] += group["extra"]
    print("\n三档（按「多出的实例」记）："
          + "；".join(f"{band} {count}" for band, count in tally.items()))
    red_by_tree = {item["id"]: sum(g["extra"] for g in item["groups"] if g["band"] == "red")
                   for item in report}
    redded = [sid for sid, value in red_by_tree.items() if value]
    print(f"  有 red 的树：{('、'.join(f'{sid}({red_by_tree[sid]})' for sid in redded)) or '无'}")
    # A–F 的 D 换成"只数 red"，阈值仍 ≤1（`_structure_audit.audit` 的那一条）
    red_pass = sum(1 for value in red_by_tree.values() if value <= 1)
    print(f"  按三档重算：D 只数 red（阈值 ≤1）时，{red_pass}/{len(report)} 棵树这一条过"
          f"（原口径 {sum(1 for item in report if item['dup'] <= 1)}/{len(report)} 过）")
    for item in report:
        for group in item["groups"]:
            if group["band"] == "grey":
                print(f"    grey {item['id']}「{group['key']}」：{group['because']}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print(f"明细写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
