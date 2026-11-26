"""自检回路实测：**在已经写好的树上 +2 次模型调用，能买到多少质量**。

要回答的是可行性报告第三节那个问题：用户提的"每支一个子代理 + 由一级节点的模型
检查 + 有错就修"，其中**真正可能带来质量的是"检查 + 修"这一步**，而"每支一个
子代理"只是他想到的实现方式。所以这里把两件事拆开量：

  基线       = 库里已经生成好的一张图（零调用，它本来就是产物）
  自检       = 1 次模型调用：只给"分支 + 缩进节点 + id"，让它报结构性问题
  修         = 代码落地它报的动作（合并 / 删除），**不再花调用**
  对照       = `optimization.repair()`：零模型调用的确定性修复，能修哪几条

量的东西：

  1. 模型报的问题里**有几条是真的**（代码可复核的那三类逐条判 strict / near / 无），
     以及**漏了几条**（库里那张图实测有的跨支重复，它报出来没有）；
  2. 修前 / 修后的 `_cross_branch_duplicates`（与生成收尾同一条判据）与节点数；
  3. 总调用数、总耗时、输入输出 token（按 `estimate_input_tokens`）。

用法（在 `backend/` 下）：

    python scripts/measure_selfcheck.py                  # 自动挑重复最多的一张
    python scripts/measure_selfcheck.py --run 3d71b0ab    # 指定 run_id 前缀
    python scripts/measure_selfcheck.py --dry             # 不调模型，只打印清单

**不写库、不碰产品代码**：库以只读打开，逐字校验与修复都在内存里做完。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DEFAULT_DB = BACKEND / "data" / "mindmaps.db"
RUN_BAT = BACKEND.parent / "run-backend.bat"

#: 与 `_cross_branch_duplicates` 同一条判据的归一化：只折空白。
def _norm(label: str) -> str:
    return re.sub(r"\s+", "", label or "")


REVIEW_SYSTEM = """你是思维导图的审稿人。别人已经写好了一张导图，你要找出**结构性问题**。

只报下面五类，每条都必须给出**节点 id**（清单里每行开头的 id）：

1. `cross_branch_duplicate` —— 同一个说法挂在**两条不同的一级分支**下。字面相同，
   或者明显是同一个概念（例：「无监督学习」与「无监督学习：无标注找结构」）。
2. `sibling_duplicate` —— **同一个父节点下**两个子节点在讲同一件事。
3. `chain` —— 一个节点只有一个孩子，而且父子是同一件事的两层说法（白占一层）。
4. `label_detail_mismatch` —— 节点说明讲的内容与它自己的标题不是一回事。
5. `orphan` —— 节点内容与它所在的一级分支无关。

判据要严：**宁少不滥**。报错一条的代价比漏掉一条大——用户会照着你的清单删东西。
拿不准就不报。

只返回一个 JSON 对象，不要别的话：

{"issues":[{"kind":"cross_branch_duplicate","nodes":["id1","id2"],"why":"一句话"}]}

没有问题时返回 {"issues":[]}。"""


# ---------------------------------------------------------------- 凭据


def _bat_credentials() -> dict[str, str]:
    """从 `run-backend.bat` 的 `set NAME=value` 行取凭据（不落盘、不打印）。"""
    found: dict[str, str] = {}
    if not RUN_BAT.exists():
        return found
    for line in RUN_BAT.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = re.match(r"\s*set\s+([A-Z0-9_]+)=(.*)$", line, re.I)
        if match:
            found[match.group(1).upper()] = match.group(2).strip()
    return found


def _fill_credentials() -> None:
    bat = _bat_credentials()
    for name in ("MINDMAP_LLM_BASE_URL", "MINDMAP_LLM_API_KEY", "MINDMAP_LLM_MODEL"):
        if not os.environ.get(name) and bat.get(name):
            os.environ[name] = bat[name]
    os.environ.setdefault("MINDMAP_LLM_MODE", "model")
    os.environ.setdefault("MINDMAP_LLM_BASE_URL", "https://api.deepseek.com")
    os.environ.setdefault("MINDMAP_LLM_MODEL", "deepseek-chat")


# ---------------------------------------------------------------- 取树


def _load_run(db: Path, prefix: str | None) -> tuple[str, dict, dict]:
    import sqlite3

    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = list(c.execute(
        "select run_id, created_at, candidate from runs "
        "where candidate is not null and candidate != '' order by created_at desc"))

    from app.agent.tools import _cross_branch_duplicates
    from app.contracts.mindmap import MindMapContent

    best: tuple[int, str, dict, dict] | None = None
    for row in rows:
        candidate = json.loads(row["candidate"])
        if candidate.get("diagram_type", "tree") != "tree":
            continue
        if prefix and not row["run_id"].startswith(prefix):
            continue
        try:
            content = MindMapContent.model_validate(candidate)
        except Exception:  # noqa: BLE001 — 老产物可能与现契约不符
            continue
        duplicates = _cross_branch_duplicates(content.root)
        weight = sum(len(places) for _, places in duplicates)
        if best is None or weight > best[0]:
            best = (weight, row["run_id"], row["created_at"], candidate)
        if prefix:
            break
    if best is None:
        raise SystemExit("库里找不到可用的树")
    return best[1], best[3], best[2]


# ---------------------------------------------------------------- 清单


def _outline(root) -> tuple[str, dict[str, str]]:
    """树 → 缩进清单 + `id → 位置路径`。**只给 label，不给 detail**。

    不给 detail 是有意的：detail 让输入翻三倍，而"跨支重复 / 同父重复 / 空链"
    这三类判定只看 label 与结构。少给的那部分正是这次要量的代价之一。
    """
    lines: list[str] = []
    paths: dict[str, str] = {}

    def walk(node, depth: int, chain: tuple[str, ...]) -> None:
        for child in node.children:
            if depth == 0:
                lines.append(f"【一级分支】{child.label}")
                paths[child.id] = child.label
                walk(child, depth + 1, (child.label,))
                continue
            pad = "  " * depth
            lines.append(f"{pad}- {child.label}  (id={child.id})")
            paths[child.id] = " / ".join((*chain, child.label))
            walk(child, depth + 1, (*chain, child.label))

    walk(root, 0, ())
    return "\n".join(lines), paths


# ---------------------------------------------------------------- 复核


def _near(left: str, right: str, *, threshold: float = 0.45) -> bool:
    """字符二元组 Jaccard，外加"去掉编号后一方包含另一方"。

    阈值 0.45 是拿这一批真实 label 校准的：「线性模型与核方法」vs「线性与核方法家族」
    只有 0.40——它们是同一个概念，只是模型换了个后缀。0.6 那条线会把这一类全判成
    "不相近"，于是复核结果比模型本身的判断还差。
    """
    def grams(text: str) -> set[str]:
        text = _norm(text)
        return {text[i:i + 2] for i in range(len(text) - 1)} or {text}

    cleaned = [_norm(re.sub(r"^[\d.、\s]+", "", text)) for text in (left, right)]
    if cleaned[0] and cleaned[1] and (cleaned[0] in cleaned[1]
                                      or cleaned[1] in cleaned[0]):
        return True
    left_grams, right_grams = grams(left), grams(right)
    if not left_grams or not right_grams:
        return False
    return len(left_grams & right_grams) / len(left_grams | right_grams) >= threshold


def _audit(content, issues: list[dict]) -> list[dict]:
    """逐条复核模型报的问题：代码能判的判，不能判的标 `unverified`。"""
    nodes = {}
    children: dict[str, list[str]] = {}
    parents: dict[str, str | None] = {}

    def walk(node, parent: str | None) -> None:
        for child in node.children:
            nodes[child.id] = child
            parents[child.id] = parent
            children.setdefault(parent or "root", []).append(child.id)
            walk(child, child.id)

    walk(content.root, None)

    rows: list[dict] = []
    for issue in issues:
        kind = str(issue.get("kind") or "")
        ids = [item for item in (issue.get("nodes") or []) if item in nodes]
        missing = [item for item in (issue.get("nodes") or []) if item not in nodes]
        verdict = "unverified"
        note = ""
        if missing:
            verdict, note = "bad_id", f"清单里没有 {'、'.join(missing[:3])}"
        elif kind == "cross_branch_duplicate" and len(ids) >= 2:
            first, second = nodes[ids[0]], nodes[ids[1]]
            same_parent = parents.get(ids[0]) == parents.get(ids[1])
            if same_parent:
                verdict, note = "none", "报的是跨支，但这两个在同一个父下"
            elif _norm(first.label) == _norm(second.label):
                verdict = "strict"
            elif _near(first.label, second.label):
                verdict = "near"
            else:
                # 字面判据看不出来 ≠ 报错了。「监督学习：依赖标注信号」与「从标注数据
                # 学映射」只共享一个前缀，而它们在语义上确实是一件事——逐字判据对
                # 这一类是瞎的（`_cross_branch_duplicates` 的 docstring 也承认这条）。
                # 所以这里记 `unjudged`，不记 `none`：只有代码能证明错的那几条才算错。
                verdict, note = "unjudged", "字面判据判不了，需人看"
        elif kind == "sibling_duplicate" and len(ids) >= 2:
            first, second = nodes[ids[0]], nodes[ids[1]]
            if parents.get(ids[0]) != parents.get(ids[1]):
                # 它说的是"两个兄弟**子树**里各写了一遍"——kind 名用错了，但那比
                # 同父重复更隐蔽：`_cross_branch_duplicates` 只比 label 全等，
                # 「偏差-方差分解」与「偏差-方差分解量化泛化误差」它抓不到。
                verdict = ("near" if _near(first.label, second.label)
                           else "cross_subtree")
                note = "不在同一父下（跨子树）"
            elif _norm(first.label) == _norm(second.label):
                verdict = "strict"
            elif _near(first.label, second.label):
                verdict = "near"
            else:
                verdict, note = "unjudged", "字面判据判不了，需人看"
        elif kind == "chain" and ids:
            only = ids[0]
            child_ids = children.get(only, [])
            if len(child_ids) == 1 and _near(nodes[only].label,
                                             nodes[child_ids[0]].label):
                verdict = "strict"
            elif _near(nodes[only].label, nodes[ids[1]].label):
                # 父有多个孩子、但被点的那一条与父同义：模型说的是"父子同义"，
                # 那一条确实白占一层，只是它把"只有一个孩子"说错了。
                verdict, note = "near", f"父有 {len(child_ids)} 个孩子，但被点的这条同义"
            else:
                verdict, note = "none", f"父有 {len(child_ids)} 个孩子，被点的也不同义"
        elif kind in ("label_detail_mismatch", "orphan") and ids:
            # 这两类要读懂内容，代码判不了。留 `unverified`，在报告里单列。
            verdict = "unverified"
        rows.append({"kind": kind, "nodes": ids, "verdict": verdict,
                     "note": note, "why": str(issue.get("why") or "")[:80]})
    return rows


# ---------------------------------------------------------------- 修


def _apply_merges(content, rows: list[dict]):
    """把 `strict` / `near` 的重复合并落地：保留第一个，另一个的孩子并过去。

    合并语义与 `optimization.repair._merge_siblings` 同一条：保留者不动，被并掉
    的那个节点从树上消失、它的孩子接到保留者后面。这里不复制那份实现（它还要管
    引用并集、撤销、关系重定向），因为本次要量的只是"跨支重复还剩几处"。
    """
    from app.contracts.mindmap import Node

    pairs = [(row["nodes"][0], row["nodes"][1]) for row in rows
             if row["verdict"] in ("strict", "near")
             and row["kind"] in ("cross_branch_duplicate", "sibling_duplicate")
             and len(row["nodes"]) >= 2]
    if not pairs:
        return content, 0

    original: dict[str, Node] = {}

    def index(node: Node) -> None:
        for child in node.children:
            original[child.id] = child
            index(child)

    index(content.root)

    into: dict[str, list[str]] = {}
    dropped: set[str] = set()
    for keep, drop in pairs:
        if keep in dropped or drop in dropped or keep == drop:
            continue
        into.setdefault(keep, []).append(drop)
        dropped.add(drop)

    def rebuild(node: Node) -> Node:
        kids: list[Node] = []
        for child in node.children:
            if child.id in dropped:
                continue
            rebuilt = rebuild(child)
            extra: list[Node] = []
            for drop_id in into.get(child.id, []):
                source = original.get(drop_id)
                if source is not None:
                    extra.extend(rebuild(source).children)
            kids.append(rebuilt.model_copy(
                update={"children": [*rebuilt.children, *extra]}))
        return node.model_copy(update={"children": kids})

    return content.model_copy(update={"root": rebuild(content.root)}), len(dropped)


# ---------------------------------------------------------------- 主流程


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--run", default=None, help="run_id 前缀")
    parser.add_argument("--dry", action="store_true", help="不调模型")
    parser.add_argument("--save", type=Path, default=None)
    args = parser.parse_args()

    from app.agent.tools import _cross_branch_duplicates
    from app.contracts.mindmap import MindMapContent
    from app.optimization.repair import repair
    from app.optimization.service import check
    from app.generation.service import estimate_input_tokens

    run_id, candidate, created = _load_run(args.db, args.run)
    content = MindMapContent.model_validate(candidate)
    outline, paths = _outline(content.root)
    nodes = _walk(content.root)
    duplicates = _cross_branch_duplicates(content.root)

    print(f"基线：run {run_id}（{created}）")
    print(f"  节点 {len(nodes)}、一级分支 {len(content.root.children)}、"
          f"清单 {len(outline.splitlines())} 行 / {len(outline)} 字")
    print(f"  跨支重复 {sum(len(places) for _, places in duplicates)} 处："
          + "；".join(f"「{label}」在 {'、'.join(places)}"
                      for label, places in duplicates[:5]))

    before_codes = _count_codes(check(content))
    print(f"  收尾规则命中：{before_codes}")

    # ---- 零调用那一档：确定性修复 ----
    outcome = repair(content)
    repaired = outcome.content
    result = outcome
    after_repair = _cross_branch_duplicates(repaired.root)
    print(f"\n[零调用] repair()：落地 {len(result.applied)} 条，跳过 {len(result.skipped)} 条")
    print(f"  节点 {len(_walk(repaired.root))}（−{len(nodes) - len(_walk(repaired.root))}）")
    print(f"  跨支重复 {sum(len(places) for _, places in after_repair)} 处")
    print(f"  收尾规则命中：{_count_codes(check(repaired))}")

    if args.dry:
        print("\n--dry：不调模型。下面是这次要喂给审查调用的清单：\n")
        print(outline[:2000])
        return 0

    # ---- 一次调用那一档：模型审查 ----
    _fill_credentials()
    from app.generation.service import chat_async

    user = (f"主题：{content.title}\n\n这张图有 {len(content.root.children)} 条一级分支、"
            f"共 {len(nodes)} 个节点：\n\n{outline}")
    started = time.perf_counter()
    raw = await chat_async(REVIEW_SYSTEM, user, max_tokens=4000)
    seconds = time.perf_counter() - started
    issues = (raw or {}).get("issues") if isinstance(raw, dict) else None
    if not isinstance(issues, list):
        issues = []
    print(f"\n[一次调用] 审查：{seconds:.1f} 秒，报出 {len(issues)} 条"
          f"（输入约 {estimate_input_tokens(REVIEW_SYSTEM) + estimate_input_tokens(user):,} token）")

    rows = _audit(content, issues)
    verdicts = {}
    for row in rows:
        verdicts.setdefault(row["verdict"], []).append(row)
    for verdict in ("strict", "near", "cross_subtree", "unjudged", "unverified",
                    "none", "bad_id"):
        group = verdicts.get(verdict) or []
        if not group:
            continue
        print(f"  复核={verdict}：{len(group)} 条")
        for row in group[:12]:
            print(f"    {row['kind']} {row['nodes']} {row['note']} ｜ {row['why']}")

    # 召回：库里那张图真有的跨支重复（逐字判据），模型的清单里点到没有
    reported_ids = {item for row in rows for item in row["nodes"]}
    hit = 0
    for label, _ in duplicates:
        same = {node.id for node in nodes if _norm(node.label) == _norm(label)}
        if len(same & reported_ids) >= 2:
            hit += 1
    print(f"  召回：基线实测 {len(duplicates)} 处逐字跨支重复，它点到 {hit} 处"
          f"（{hit / len(duplicates):.0%}）" if duplicates
          else "  召回：基线本来没有逐字跨支重复")

    # ---- 修：把它报的、复核站得住的那几条落地 ----
    fixed, merged = _apply_merges(content, rows)
    after_codes = _count_codes(check(fixed))
    after_dup = _cross_branch_duplicates(fixed.root) if merged else duplicates
    print(f"\n[修] 合并 {merged} 个节点（不再花调用）")
    print(f"  节点 {len(_walk(fixed.root))}（−{len(nodes) - len(_walk(fixed.root))}）")
    print(f"  跨支重复 {sum(len(places) for _, places in after_dup)} 处"
          f"（修前 {sum(len(places) for _, places in duplicates)} 处）")
    print(f"  收尾规则命中：{after_codes}")

    report = {
        "run_id": run_id, "created_at": created,
        "before": {"nodes": len(nodes), "duplicates": len(duplicates),
                   "codes": before_codes},
        "repair": {"applied": len(result.applied), "skipped": len(result.skipped),
                   "nodes": len(_walk(repaired.root)),
                   "duplicates": len(after_repair)},
        "review": {"seconds": round(seconds, 1), "issues": len(issues),
                   "verdicts": {key: len(value) for key, value in verdicts.items()},
                   "recall": f"{hit}/{len(duplicates)}"},
        "after_fix": {"nodes": len(_walk(fixed.root)), "merged": merged,
                      "duplicates": sum(len(p) for _, p in after_dup),
                      "codes": after_codes},
        "rows": rows,
    }
    if args.save:
        args.save.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(f"\n写进 {args.save}")
    return 0


def _walk(node):
    out = [node]
    for child in node.children:
        out.extend(_walk(child))
    return out


def _count_codes(diagnostics) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in diagnostics:
        if item.code in ("SINGLE_CHILD", "DUPLICATE_LABEL", "NEAR_DUPLICATE_LABEL",
                         "SHALLOW_BRANCH", "MISSING_DETAIL"):
            counts[item.code] = counts.get(item.code, 0) + 1
    return counts


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
