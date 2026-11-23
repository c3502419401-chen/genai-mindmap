"""藏节点再猜：**"跨图历史 + 材料"到底能不能支撑"该挂哪"这个判断。**

这是 `optimization/link.py` 的第一版有没有用的唯一诚实判据。库里 `add` 事件零条，
所以拿不到"这个用户怎么挂节点"的历史——第一版只能靠跨图历史同类节点与材料原文。
那些东西够不够，不是靠讨论，是靠把真图上的一个节点**摘掉**再猜回去，数猜对几个。

## 四个配置（同一批样本）

                    开放集（整棵树里选）      三选一（真父 + 2 干扰）
    先描述再决定     decide                   decide/closed
    只输出决定       decision_only            decision_only/closed

两个轴各自回答一件事：

  · **开放/封闭**：开放集的随机基线是 `1/候选数`（通常 10–24 个，所以 5–10% 就已经
    显著高于随机）；封闭集是 `1/3`。**"高于随机"在小样本上很容易是运气**，所以两个
    都跑：开放集高而封闭集不高，说明它只是"知道了大概在哪一支"；两个都高才说明
    "它真的能挑出那个父节点"。
  · **先描述 / 只决定**：论文 §6.1 Table 2 那条（Qwen-3.5-9B 上 0.4832 → 0.3045）。
    本地如果没有差别，就该如实说这一版的"借洞察"没有落地效果。

## 样本怎么挑（这几条决定了数字能不能信）

  · 只取 `runs.candidate` 里 **tree** 型的图（鱼的六根骨/流程的阶段是并列子树，
    "父"这个词在那里有两种含义，混进来会让准确率变得没法解释）；
  · 只藏 **深度 ≥ 3** 的节点：一级分支的父永远是中心主题，藏它等于送分，会把
    准确率抬到没有意义的高度；
  · 每张图最多藏 1 个，且节点要带 `detail`（没有说明时模型只有 4–8 个字的标题可看，
    那不是"信息不够"，是样本本身没有判断材料）；
  · 历史库用**另外那些候选图**（排除当前这张；同名图也排除，与 `cross_map` 同一条
    规矩：同一主题的多个版本互相参照等于自我参照）。

## 跑法

    cd backend && python scripts/measure_link_placement.py --samples 12 --limit 1

要真模型：环境变量必须与 `run-backend.bat` 一致（`MINDMAP_LLM_MODE=model` 与那几个
key）。不给就走离线骨架（`--dry-run`），它只验证"藏节点/候选池/评分"这套器械对不对，
**不产生任何准确率结论**。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.contracts.mindmap import MindMapContent  # noqa: E402
from app.optimization import link  # noqa: E402

#: 随机种子写死：干扰项与样本选择都从它派生，两跑必须同一批题。
SEED = 20261124

#: 藏起来的节点至少要有这么多字的说明，否则"该挂哪"没有判断材料（见文件头）。
MIN_DETAIL_CHARS = 20

#: 每张图最多几个节点。太大的图（80+ 节点）候选池太宽，开放集基线低到 1%，比较失真。
MAX_NODES = 48


@dataclass
class Sample:
    map_id: str
    title: str
    content: MindMapContent       # 摘掉 target 之后的树
    target: Any                   # 被藏起来的节点
    true_parent: str
    true_parent_label: str
    depth: int
    budget: int                   # 开放集的候选数（随机基线 = 1/budget）


def _config() -> None:
    """与 `run-backend.bat` 同一套环境变量（真模型那条路）。"""
    import os

    defaults = {
        "MINDMAP_LLM_MODE": "model",
        "MINDMAP_LLM_BASE_URL": "https://api.deepseek.com",
        "MINDMAP_LLM_API_KEY": "sk-7961cf33c9c94110b749e800adfb067d",
        "MINDMAP_LLM_MODEL": "deepseek-chat",
        "MINDMAP_EMBEDDING_PROTOCOL": "openai",
        "MINDMAP_EMBEDDING_BASE_URL": (
            "https://ws-y7zvccm7g093iscn.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"),
        "MINDMAP_EMBEDDING_API_KEY": (
            "sk-ws-H.YPDPXIH.i6xQ.MEYCIQC_IlGF2kv7V-DDTl2I54Qkv3n37srUT9H4YWQD6rCGoAIhAKhO"
            "JIp5ukcLqzfQFrb5AxbSUMIpXh17MqPv0dDK5psU"),
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)


# ---------------------------------------------------------------- 样本


def _strip(node: dict, target_id: str) -> dict:
    """把 `target_id` 那棵子树整棵摘掉。纯 dict 操作，不碰 Pydantic 的不变式。"""
    if node.get("id") == target_id:
        raise ValueError("不该到这里：目标节点应当由父节点过滤掉")
    children = [child for child in node.get("children") or [] if child.get("id") != target_id]
    return {**node, "children": [_strip(child, target_id) for child in children]}


def _walk(node: dict, depth: int = 1, parent: str | None = None):
    yield node, depth, parent
    for child in node.get("children") or []:
        yield from _walk(child, depth + 1, node.get("id"))


def _pick_target(doc: dict, skip: set[str] | None = None) -> dict | None:
    """挑一个"值得猜"的节点：深度 ≥ 3、带说明、标签不重复。

    三条都是为了让这个数字可解释：一级分支的父恒为中心主题（送分）；没有说明的
    节点只剩一个短标题（样本本身没料）；标签重复时"猜对 id"与"猜对意思"会分家。

    `skip`：同一张图里已经藏过的节点 id。一张图可以出两道题（不同支），但不能出
    同一道——那是同一道题记两次分。
    """
    skip = skip or set()
    labels = Counter(node.get("label", "").strip() for node, _, _ in _walk(doc["root"]))
    pool = [
        node for node, depth, _ in _walk(doc["root"])
        if depth >= 3
        and node.get("id") not in skip
        and len((node.get("detail") or "").strip()) >= MIN_DETAIL_CHARS
        and labels[node.get("label", "").strip()] == 1
    ]
    # 按"深层优先"再按标签长度降序：浅的更容易（父是更大的一级分支），
    # 先取深的，样本里难度分布偏难一侧更保守。
    pool.sort(key=lambda node: (-len((node.get("label") or '')), node.get("id") or ''))
    return pool[0] if pool else None


def build_samples(*, samples: int, per_map: int) -> tuple[list[Sample], list[Any]]:
    """从 `runs.candidate` 造样本，同时返回"历史库"（这些候选图当历史用）。"""
    from app.mindmaps import repository

    history: list[Any] = []
    picked: list[tuple[str, str, dict]] = []
    with repository.connect() as conn:
        rows = conn.execute(
            "SELECT run_id, candidate FROM runs WHERE candidate IS NOT NULL "
            "ORDER BY created_at DESC").fetchall()
    for row in rows:
        try:
            doc = json.loads(row["candidate"])
        except (TypeError, ValueError):
            continue
        if "root" not in doc or not (doc.get("root") or {}).get("id"):
            continue
        try:
            content = MindMapContent.model_validate(doc)
        except Exception:  # noqa: BLE001 — 老候选可能不满足现行契约，跳过
            continue
        if sum(1 for _ in _walk(doc["root"])) > MAX_NODES:
            continue

        class _Record:
            def __init__(self, rid: str, body: MindMapContent) -> None:
                self.id = rid
                self.content = body

        history.append(_Record(f"run:{row['run_id']}", content))
        picked.append((f"run:{row['run_id']}", doc.get("title") or content.title, doc))

    rng = random.Random(SEED)
    rng.shuffle(picked)

    # **一道题只考一次。** 库里同一主题被生成过很多次（"城市垃圾分类政策"这一版就
    # 有 7 张候选），不去重的话"猜对几个"会被同一道题反复记分，准确率的方差全是假的。
    # 两张表：图标题去重（同一主题的多个版本互相当参照也是 `cross_map` 明确排除的
    # 噪声源），以及被藏节点标签去重。
    seen_titles: set[str] = set()
    seen_targets: set[str] = set()

    out: list[Sample] = []
    for map_id, title, doc in picked:
        if len(out) >= samples:
            break
        if title.strip() in seen_titles:
            continue
        used: set[str] = set()
        used_parents: set[str] = set()
        taken = 0
        # 一张图可以出 2 道题（不同支）：8 张图 × 2 比 8 张图 × 1 的置信区间窄得多，
        # 而"同一张图的两次判断相关"这件事在样本量这么小时不是主要误差来源。
        while taken < per_map and len(out) < samples:
            target = _pick_target(doc, used)
            if target is None:
                break
            used.add(target["id"])
            label = (target.get("label") or '').strip()
            if label in seen_targets:
                continue
            parent_id = None
            parent_label = ''
            depth = 0
            for node, node_depth, parent in _walk(doc["root"]):
                if node.get("id") == target["id"]:
                    depth, parent_id = node_depth, parent
                    break
            if parent_id is None or parent_id in used_parents:
                # 同一支里连藏两个节点会让"父"这个答案在第一题后就被削掉一块，
                # 两题之间不再是独立的。
                continue
            for node, _, _ in _walk(doc["root"]):
                if node.get("id") == parent_id:
                    parent_label = (node.get("label") or '').strip()
                    break
            stripped = {"title": doc.get("title") or title,
                        "root": _strip(doc["root"], target["id"])}
            try:
                tree = MindMapContent.model_validate(stripped)
            except Exception:  # noqa: BLE001
                continue
            stack = [MindMapContent.model_validate(doc).root]
            real = None
            while stack:
                current = stack.pop()
                if current.id == target["id"]:
                    real = current
                    break
                stack.extend(current.children)
            if real is None:
                continue
            budget = len(link.parent_choices(tree, real.id, target=real))
            if budget < 4:
                continue
            seen_targets.add(label)
            used_parents.add(parent_id)
            out.append(Sample(map_id=map_id, title=title, content=tree, target=real,
                              true_parent=parent_id, true_parent_label=parent_label,
                              depth=depth, budget=budget))
            taken += 1
        if taken:
            seen_titles.add(title.strip())
    return out, history


def closed_choices(sample: Sample, rng: random.Random) -> list[link._ParentChoice]:
    """真父 + 2 个干扰项。干扰项从其余合法父里随机取——**不能挑"看起来不像的"**，
    那等于替模型排除错误答案，准确率会虚高。"""
    pool = link.parent_choices(sample.content, sample.target.id, target=sample.target)
    truth = next((choice for choice in pool if choice.node_id == sample.true_parent), None)
    others = [choice for choice in pool if choice.node_id != sample.true_parent]
    rng.shuffle(others)
    picked = ([truth] if truth else []) + others[: 2]
    rng.shuffle(picked)
    return picked


# ---------------------------------------------------------------- 一次评测


async def judge(sample: Sample, *, mode: str, decision_only: bool,
                history_hits: list[dict[str, Any]],
                materials: list[dict[str, Any]], chat,
                bare: bool = False) -> dict[str, Any]:
    rng = random.Random(f"{SEED}:{sample.map_id}:{sample.target.id}:{mode}")
    if mode == "open":
        choices = link.parent_choices(sample.content, sample.target.id, target=sample.target)
    else:
        choices = closed_choices(sample, rng)
    # **候选顺序必须打乱。** `parent_choices` 按"与目标的字面重合"降序排，而真父
    # 恰恰是字面最像的那个（同一支的内容共享主题词）——不打乱的话，"模型每次都选
    # 第一个"就能拿 100%，而这个 100% 量的是排序函数，不是判断力。
    # 打乱之后 `truth_rank` 才有意义，见 `order0` 那条对照基线。
    choices = list(choices)
    rng.shuffle(choices)
    truth_rank = next((index for index, choice in enumerate(choices)
                       if choice.node_id == sample.true_parent), None)

    report = await link.suggest(
        sample.content, sample.target.id, target=sample.target, choices=choices,
        # `history` / `material` 是**已经算好的**匹配项，四个配置共用同一份：
        # 每一项都要一次 embedding 或一次检索，四次重算既慢又会让"四个配置看到的
        # 依据不完全相同"这件事变成不可解释的方差来源。
        history=[] if bare else history_hits,
        material=[] if bare else materials,
        chat=chat, on=True,
        decision_only=decision_only,
    )
    got = []
    for suggestion in report.suggestions:
        body = json.loads(suggestion.evidence)
        got = [item["parent_id"] for item in body["candidates"]]
        what = body.get("what", '')
        line = body.get("line", '')
        break
    else:
        what = line = ''
    return {
        "map_id": sample.map_id,
        "title": sample.title,
        "target": sample.target.label.strip(),
        "target_id": sample.target.id,
        "true_parent": sample.true_parent,
        "true_parent_label": sample.true_parent_label,
        "depth": sample.depth,
        "choices": len(choices),
        "truth_rank": truth_rank,
        # 真父**不在候选池里**的样本必须排除，不能算"猜错"：候选池截断到
        # `MAX_PARENT_CHOICES` 时，字面重合最低的那个可能根本没被递出去——
        # 那是器械的问题，把它记成模型的错会让准确率莫名偏低。
        "answerable": truth_rank is not None,
        # 「答对了」与「答对了且答案恰好排在第一位」要分开记：前者是判断力，
        # 后者可能只是排序。打乱之后两者都在，才读得出模型到底做了什么。
        "order0": truth_rank == 0,
        "picked": got[0] if got else None,
        "top": got,
        "top1": bool(got) and got[0] == sample.true_parent,
        "top3": sample.true_parent in got,
        "what": what,
        "line": line,
        "skipped": report.skipped,
        "dropped_evidence": report.dropped_evidence,
    }


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    _config()
    samples, history = build_samples(samples=args.samples, per_map=args.per_map)
    print(f"样本 {len(samples)} 个（历史库 {len(history)} 张候选图）")
    if not samples:
        print("没有可用样本：库里没有带 detail 的 tree 候选图")
        return {}

    if args.dry_run:
        for sample in samples:
            print(f"  · {sample.title}｜藏「{sample.target.label.strip()}」"
                  f"（第 {sample.depth} 层）→ 真父「{sample.true_parent_label}」"
                  f"｜开放集 {sample.budget} 个候选")
        return {"dry_run": True, "samples": len(samples)}

    from app.generation.service import chat_async

    # 每个样本算一次依据，四个配置共用。这两步各自要一次检索 / 一次 embedding，
    # 重算四遍既慢，也会让"四个配置看到的依据略有不同"变成说不清的方差。
    prepared: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    for index, sample in enumerate(samples, 1):
        key = f"{sample.map_id}:{sample.target.id}"
        hits = link.history_placements(sample.content, sample.target, records=history)
        mats = ([] if args.no_material
                else link.material_hits(f"{sample.title} {sample.target.label.strip()}".strip()))
        prepared[key] = (hits, mats)
        print(f"  依据 {index}/{len(samples)} 「{sample.target.label.strip()}」："
              f"历史 {len(hits)} 条、材料 {len(mats)} 段")

    results: dict[str, list[dict[str, Any]]] = {}
    for mode, decision_only, bare in (
        ("open", False, False), ("closed", False, False),
        # **决定性对照**：去掉跨图历史与材料，只剩这棵树本身。
        # 如果它也 8/8，那"跨图历史 + 材料能不能支撑判断"这个问题就没被证明——
        # 这次实验量到的是"树内上下文 + 模型常识"，而那是两件完全不同的投入。
        # 少了这一行，全文的结论都会朝"这套依据有效"偏。
        ("open", False, True), ("closed", False, True),
        ("open", True, False), ("closed", True, False),
    ):
        key = f"{mode}/{'decision_only' if decision_only else 'decide'}"
        key += "/bare" if bare else ''
        rows = []
        for index, sample in enumerate(samples, 1):
            hits, mats = prepared[f"{sample.map_id}:{sample.target.id}"]
            row = await judge(sample, mode=mode, decision_only=decision_only,
                              history_hits=hits, materials=mats, chat=chat_async,
                              bare=bare)
            rows.append(row)
            mark = "✓" if row["top1"] else ("·" if row["top3"] else "✗")
            print(f"  [{key}] {index}/{len(samples)} {mark} "
                  f"「{row['target']}」猜→{row['picked']} 真→{row['true_parent']}")
        results[key] = rows

    print("\n=== 汇总 ===")
    summary: dict[str, Any] = {}
    for key, rows in results.items():
        rows = [row for row in rows if row["answerable"]]
        n = len(rows)
        top1 = sum(row["top1"] for row in rows)
        top3 = sum(row["top3"] for row in rows)
        # 随机基线：开放集是 1/候选数，封闭集是 1/3。用每个样本自己的候选数算平均，
        # 而不是拿"最大/最小候选数"拍一个——样本候选池大小不一，拍一个数就是错。
        chance = (sum(1 / row["choices"] for row in rows) / n if n else 0)
        # 「答案本来就在第一位」的比例。它是"打乱之前的排序函数有多准"的读数，
        # 也是判断"这个准确率是不是白捡的"的唯一对照：
        #   order0 高 ≈ 答对率高  → 模型可能只是跟着排序走；
        #   order0 低而 top1 高   → 它真的在判。
        order0 = sum(row["order0"] for row in rows)
        summary[key] = {"n": n, "top1": top1, "top3": top3,
                        "top1_rate": round(top1 / n, 3) if n else 0,
                        "top3_rate": round(top3 / n, 3) if n else 0,
                        "chance": round(chance, 3),
                        "order0": order0,
                        "order0_rate": round(order0 / n, 3) if n else 0}
        print(f"{key:26s} n={n:3d} top1={top1}/{n} ({summary[key]['top1_rate']:.0%}) "
              f"top3={top3}/{n}  随机={chance:.1%}  "
              f"答案在第一位={order0}/{n}")

    out = {"samples": len(samples), "history": len(history), "summary": summary,
           "rows": results}
    path = Path(__file__).resolve().parents[1] / "data" / "measure_link_placement.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细写入 {path}")
    return out


def analyze(path: Path) -> dict[str, Any]:
    """读已落盘的明细，补两个**改判之前必须先看**的读数。不重跑模型。

    一、**猜错的那几个，错在哪一层**。`true_parent` 是唯一的运行时答案，但语义上
        经常不唯一：真父与模型猜的那个父，可能同属一个论点群（"抵消只处理剩余排放"
        与"不等于完全无排放"），或者干脆是父子关系（猜到了下一层）。把这类错判与
        "完全跑偏"混成一个 88%，会低估这一步。

    二、**要多少条真实 `add` 事件才可能把这件事做得比现在更好**。这是任务问的那个
        数，它是个统计功效问题，不是拍脑袋：要在 α=0.05 / power=0.8 下检出
        "+7 个百分点"，需要多少对样本。
    """
    from statistics import NormalDist

    data = json.loads(path.read_text(encoding="utf-8"))
    tree_rows = {}
    rough = NormalDist()

    def relation(doc: dict, node_id: str, other_id: str) -> str:
        parent: dict[str, str | None] = {}
        for node, _, up in _walk(doc["root"]):
            parent[node.get("id")] = up
        if node_id == other_id:
            return "exact"
        if parent.get(node_id) == other_id:
            return "one_up"        # 猜的是真父的父（往上走一层）
        if parent.get(other_id) == node_id:
            return "one_down"      # 猜的是真父的子（往下走一层）
        if parent.get(node_id) and parent.get(node_id) == parent.get(other_id):
            return "sibling"       # 同一个父下面的兄弟
        return "elsewhere"

    results: dict[str, Any] = {"rows": {}, "power": {}}
    for key, rows in data["rows"].items():
        counted = Counter()
        rows = [row for row in rows if row.get("answerable", True)]
        for row in rows:
            if row["top1"]:
                counted["exact"] += 1
                continue
            doc = tree_rows.get(row["map_id"])
            if doc is None:
                with __import__("sqlite3").connect(
                        f"file:{Path(__file__).resolve().parents[1] / 'data' / 'mindmaps.db'}"
                        "?mode=ro", uri=True) as conn:
                    found = conn.execute("SELECT candidate FROM runs WHERE run_id = ?",
                                         (row["map_id"].split(":", 1)[-1],)).fetchone()
                doc = json.loads(found[0]) if found and found[0] else {}
                tree_rows[row["map_id"]] = doc
            try:
                counted[relation(doc, row["picked"], row["true_parent"])] += 1
            except Exception:  # noqa: BLE001 — 图读不回来只少一条分类，不影响主表
                counted["unclassified"] += 1
        n = len(rows)
        results["rows"][key] = {
            "n": n,
            "exact": counted["exact"],
            "near": counted["one_up"] + counted["one_down"] + counted["sibling"],
            "elsewhere": counted["elsewhere"] + counted["unclassified"],
            "detail": dict(counted),
        }
        print(f"{key:26s} n={n:3d} 完全正确={counted['exact']:2d} "
              f"层级相邻/同支={counted['one_up'] + counted['one_down'] + counted['sibling']:2d} "
              f"完全跑偏={counted['elsewhere'] + counted['unclassified']:2d}")

    # 功效：n = (z_{α/2} + z_β)² × [p1(1-p1) + p2(1-p2)] / (p2-p1)²
    base = next(iter(data["summary"].values()))["top1_rate"] if data.get("summary") else 0.88
    for target in (base + 0.05, base + 0.07, base + 0.10):
        if target >= 1:
            continue
        z_a = rough.inv_cdf(1 - 0.05 / 2)
        z_b = rough.inv_cdf(0.8)
        need = (z_a + z_b) ** 2 * (base * (1 - base) + target * (1 - target)) / (target - base) ** 2
        results["power"][f"{target:.2f}"] = int(-(-need // 1))
        print(f"要检出 {base:.0%} → {target:.0%}（α=.05, power=.8）需要 "
              f"{int(-(-need // 1))} 个样本")
    return results


async def demo(count: int) -> None:
    """跑几条**完整产出**（含 what / line / candidates / why / evidence），供人工读。

    与评测那条路唯一的差别是这里不评分、不汇总：它回答的是"这条建议长什么样、
    证据能回指到哪儿"，而那是 `docs/LINK-SUGGESTIONS.md` 里要贴给人看的东西。
    """
    _config()
    samples, history = build_samples(samples=count, per_map=2)
    from app.generation.service import chat_async

    for sample in samples[:count]:
        hits = link.history_placements(sample.content, sample.target, records=history)
        mats = link.material_hits(f"{sample.title} {sample.target.label.strip()}".strip())
        report = await link.suggest(
            sample.content, sample.target.id, target=sample.target,
            history=hits, material=mats, chat=chat_async, on=True)
        print(f"\n===== {sample.title}｜藏起「{sample.target.label.strip()}」"
              f"（真父「{sample.true_parent_label}」）")
        for suggestion in report.suggestions:
            print(f"kind={suggestion.kind.value}  node_id={suggestion.node_id}")
            print(f"message: {suggestion.message}")
            body = json.loads(suggestion.evidence)
            print(json.dumps(body, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--per-map", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true",
                        help="只验器械（藏节点/候选池/评分），不调模型")
    parser.add_argument("--no-material", action="store_true")
    parser.add_argument("--analyze", action="store_true",
                        help="只读已落盘的明细，补错判分类与功效计算，不调模型")
    parser.add_argument("--demo", type=int, default=0,
                        help="跑 N 条完整产出并原样打印（含 evidence 回指）")
    args = parser.parse_args()
    if args.analyze:
        analyze(Path(__file__).resolve().parents[1] / "data" / "measure_link_placement.json")
        return
    if args.demo:
        asyncio.run(demo(args.demo))
        return
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
