"""混合材料的骨架：§二 的账（分几组、各出几条）与 §三 的 A/B（覆盖率 / 条数 / 忠实度 / 耗时）。

## 为什么需要这个脚本

缺陷的形状是"**一部分材料被静默丢掉**"，它不会让生成失败，只会让图看起来正常：

    只有 markdown（4 份 / 66,536 字）        30 条章节，覆盖 0.91
    只有 paper（20 份 / 6,658 字）            0 条（没有标题）
    markdown + paper 混（24 份 / 73,194 字）  30 条章节，覆盖 0.83 ← paper 那 20 份没有位置

"覆盖 0.83"这个数看起来还不错，因为**分母是字数**：那 20 份摘要只占 9%。所以修法不能
只看整批覆盖率，要看**有多少份材料被至少一条一级分支指到**——那就是本脚本的主指标。

## 怎么读输出

    python scripts/measure_mixed_skeleton.py --probe     # 不花钱：只量分组与图谱挑选
    python scripts/measure_mixed_skeleton.py             # A/B 各 1 次（真模型）
    python scripts/measure_mixed_skeleton.py --repeat 2 --branches 6

`--probe` 给的是 §二 那三行数字（章节 N 条 / 图谱 N 条 / 合并后总数）与抽检的概念在
原文里的出处；不带参数时跑 A（`MINDMAP_MIXED_SKELETON=0`）/ B（`=1`）两臂，各给
① 材料覆盖率 ② 分支总数 ③ 骨架忠实度 ④ 耗时。
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
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(BACKEND / "scripts") not in sys.path:
    sys.path.insert(0, str(BACKEND / "scripts"))

from measure_graph_outline import (  # noqa: E402
    _fill_env_from_registry,
    _squash,
    faithfulness,
    tree_stats,
)

TOPIC = "这个多智能体思维导图系统的架构与取舍"
"""与 `docs/GRAPH-TWO-PASS.md` 第 5 节那批 4 份 markdown 的 A/B 同一个主题：
库里的材料是用户自己的请求历史，没有"更自然"的题；换题会让两轮的忠实度不可比。"""

BRANCHES = 6
"""界面上默认的条数。它同时是"分支总数不该爆"的参照：请求 6 条、回来 6–8 条都算正常。"""


def _fixtures(rows: list, loaded: list) -> dict[str, list]:
    """§二 / §三 用的五组材料，按 kind 拼（与缺陷报告里那五行的口径一致）。

    "web + paper"那一行**只取两份长维基**：库里三份短维基（89 / 149 / 157 字）自己
    也抽不出目录，加不加都不改变结论，而报告里那一行 22 份 / 16,968 字正是这么算的
    （20 + 2；16,968 = 17,363 − 157 − 149 − 89）。
    """
    by_kind: dict[str, list] = {}
    for row, source in zip(rows, loaded):
        by_kind.setdefault(row["kind"], []).append(source)
    markdown = by_kind.get("markdown", [])
    paper = by_kind.get("paper", [])
    web = by_kind.get("web", [])
    long_web = [source for source in web if len(source.text or "") >= 1000]
    return {
        "只有 markdown": markdown,
        "只有 paper": paper,
        "markdown + paper 混": markdown + paper,
        "markdown 少量 + paper 大量": markdown[2:3] + paper,
        "web + paper 混（两份长维基）": long_web + paper,
    }


def probe(rows: list, loaded: list) -> dict:
    """§二：五组材料的现状、分组结果、图谱补充条数与合并后的总数。"""
    from app.agent import tools as tools_module
    from app.knowledge.service import heading_outline, outline_coverage

    fixtures = _fixtures(rows, loaded)
    report: dict = {"fixtures": [], "split": {}, "supplement": {}, "samples": []}
    print("=== 现状（A）：整批一个结论 ===", flush=True)
    for name, sources in fixtures.items():
        chars = sum(len(source.text or "") for source in sources)
        entries = heading_outline(sources)
        share = outline_coverage(sources, entries) if entries else 0.0
        usable = tools_module._outline_entries(sources)
        note = "" if usable else "（过不了 60% 的闸 → 骨架里一条都没有）"
        print(f"{name:<30} {len(sources):>3} 份 / {chars:>7,} 字 → {len(entries):>3} 条"
              f"  覆盖 {share:.2f}{note}", flush=True)
        report["fixtures"].append({
            "name": name, "sources": len(sources), "chars": chars,
            "entries": len(entries), "coverage": round(share, 4),
            "usable": len(usable),
        })

    print("\n=== 分组（每份单独判：目录非空且覆盖 ≥ 60%）===", flush=True)
    for name, sources in fixtures.items():
        split = tools_module._outline_split(sources)
        merged = tools_module._mixed_outline(sources)
        print(f"{name:<30} 有章节 {len(split.titled):>2} 份 / 无章节 {len(split.plain):>2} 份"
              f" | 章节 {len(split.chapters):>2} 条", flush=True)
        report["split"][name] = {
            "titled": len(split.titled), "plain": len(split.plain),
            "chapters": len(split.chapters),
            "mixed": merged is not None,
        }
        if merged is None:
            continue
        supplement = merged.supplement
        total = len(merged.chapters) + supplement.items
        print(f"{'':<30} 图谱补充 {supplement.items} 条 / 覆盖 "
              f"{supplement.covered}/{supplement.plain} 份 → 合并后 {total} 条"
              f"（{len(merged.chapters)} 章节 + {supplement.items} 图谱；"
              f"分组阈值 {tools_module.SKELETON_GROUP_THRESHOLD}，"
              f"{'已分组' if total > tools_module.SKELETON_GROUP_THRESHOLD else '仍扁平'}）",
              flush=True)
        report["supplement"][name] = {
            "items": supplement.items, "covered": supplement.covered,
            "plain": supplement.plain, "entities": supplement.entities,
            "merged": total,
        }
        if name == "markdown + paper 混":
            report["block"] = supplement.block
            report["samples"] = _sample_concepts(supplement.block, split.plain)
    return report


def _sample_concepts(block: str, plain: list, limit: int = 5) -> list[dict]:
    """§二 的抽检：前 `limit` 条概念在**哪一份、哪一句**里逐字出现。

    判据与产品同源（`_squash` 去空白标点后的子串），不是"看起来像"：一条概念若
    只在抽取层的中间产物里，这里就找不到它，也就说明它撑不起分支。
    """
    names = re.findall(r"^\d+\. (.+?)(?:（[^）]*）)?$", block, flags=re.MULTILINE)
    rows: list[dict] = []
    for name in names[:limit]:
        needle = _squash(name)
        for source in plain:
            body = _squash(source.text or "")
            index = body.find(needle)
            if index < 0:
                continue
            rows.append({
                "concept": name,
                "filename": source.filename or source.id,
                "quote": body[max(0, index - 24):index + len(needle) + 24],
                "files_with_it": sum(
                    1 for item in plain if needle in _squash(item.text or "")),
            })
            break
        else:
            rows.append({"concept": name, "filename": "", "quote": "", "files_with_it": 0})
    print("\n=== 抽检：图谱概念在原文里的出处（去空白标点后逐字匹配）===", flush=True)
    for row in rows:
        mark = "✅" if row["filename"] else "❌ 原文里找不到"
        print(f"  {mark} {row['concept']}｜{row['filename'][:34]}"
              f"｜{row['files_with_it']} 份｜…{row['quote']}…", flush=True)
    return rows


def material_coverage(content, sources: list) -> dict:
    """① 材料覆盖率：有多少份材料被**至少一条一级分支**指到。两个口径一起看。

    **主口径是引用**（`source_refs`）：一条一级分支的子树里出现某份材料的引用，就是
    "这一支指到了这份材料"——它同时说明这一支的内容真的从那份材料里取过东西。实测
    （`data/ab-mixed*.json`）发现**逐字标签口径在这里没用**：模型写的一级分支名几乎
    从不逐字等于材料里的词（A 组 0/24、B 组 0/24），那一列只能当"标签是不是原文原词"
    的旁证，不能当"材料有没有位置"的判据。

    **旁证是标签**（`label_*`）：分支名去掉空白标点后是否是某份原文的子串。它对"分支
    名用了材料里的原词"敏感，对"内容确实取自那份材料"不敏感——两个都要报，差值本身
    就是信息（B 组的分支名更贴材料时，这一列会涨）。
    """
    by_id = {source.id: (source.filename or source.id) for source in sources}
    squashed = [(source.id, _squash(source.text or "")) for source in sources]

    def _cited(node) -> set[str]:
        found: set[str] = set()
        stack = [node]
        while stack:
            current = stack.pop()
            for ref in (current.source_refs or []):
                if ref.source_id in by_id:
                    found.add(ref.source_id)
            stack.extend(current.children or [])
        return found

    branched: list[dict] = []
    covered: dict[str, list[str]] = {}
    for node in content.root.children:
        cited = _cited(node)
        branched.append({
            "label": node.label,
            "sources_cited": len(cited),
            "files": [by_id[source_id] for source_id in sorted(cited)],
        })
        for source_id in cited:
            covered.setdefault(source_id, []).append(node.label or "")

    label_hits: dict[str, list[str]] = {}
    for node in content.root.children:
        needle = _squash(node.label or "")
        if not needle:
            continue
        for source_id, body in squashed:
            if needle in body:
                label_hits.setdefault(source_id, []).append(node.label or "")

    return {
        "materials": len(sources),
        "covered": len(covered),
        "ratio": round(len(covered) / len(sources), 4) if sources else 0.0,
        "label_covered": len(label_hits),
        "label_ratio": round(len(label_hits) / len(sources), 4) if sources else 0.0,
        "branches": branched,
        "uncovered": [name for source_id, name in by_id.items() if source_id not in covered],
    }


async def _run_once(*, topic: str, branches: int, sources: list, mixed: bool) -> dict:
    """跑一次生成。开关走环境变量——与真实请求同一条路（`run-backend.bat` 也这么给）。"""
    from app.agent import tools as tools_module
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall

    os.environ[tools_module.MIXED_SKELETON_ENV] = "1" if mixed else "0"
    request = AgentRunRequest(
        goal=topic, intent="create", thinking_model="default", sources=sources,
        budget=RunBudget(max_model_calls=40, max_tool_calls=8, max_seconds=900.0,
                         max_output_tokens=65536, max_repairs=1),
    )
    ctx = RunContext(request=request,
                     sources_by_id={source.id: source for source in sources})
    registry = tools_module.build_registry("create", has_sources=True, has_library=False)
    call = ToolCall(call_id="measure", name="mindmap_generate",
                    arguments={"topic": topic, "branches": branches})
    seen: list[tuple[str, str]] = []
    durations: list[float] = []
    original = tools_module.chat_async

    async def spy(system: str, user: str, **kwargs):
        started = time.perf_counter()
        answer = await original(system, user, **kwargs)
        durations.append(time.perf_counter() - started)
        seen.append((system, user))
        return answer

    started = time.perf_counter()
    with patch.object(tools_module, "chat_async", spy):
        result = await registry.execute(call, ctx)
    elapsed = time.perf_counter() - started
    if not result.ok:
        return {"ok": False, "content": result.content, "seconds": round(elapsed, 2)}
    return {
        "ok": True, "seconds": round(elapsed, 2), "data": result.data,
        "content": ctx.candidate, "plan_user": seen[0][1] if seen else "",
        "plan_system": seen[0][0] if seen else "", "calls": len(seen),
        # ④ 骨架那一次自己的耗时：新增的图谱查询与逐份解析都花在它之前/之内，
        # 混在总耗时里看不出"这一次改慢了多少"。
        "plan_seconds": round(durations[0], 2) if durations else None,
    }


def _row(arm: str, sample: int, outcome: dict, sources: list) -> dict:
    content = outcome["content"]
    stats = tree_stats(content)
    cover = material_coverage(content, sources)
    faithful = faithfulness(content, sources)
    codes = [item["code"] for item in ((outcome["data"] or {}).get("diagnostics") or [])]
    labels = [node.label for node in content.root.children]
    return {
        "arm": arm, "sample": sample, "seconds": outcome["seconds"],
        "calls": outcome["calls"], "branches": stats["branches"],
        "nodes": stats["nodes"], "labels": labels,
        "material_covered": cover["covered"], "material_ratio": cover["ratio"],
        "label_covered": cover["label_covered"],
        "label_ratio": cover["label_ratio"],
        "branch_sources": cover["branches"],
        "uncovered": cover["uncovered"],
        "faithfulness": faithful["ratio"],
        "faithfulness_segment": faithful["segment_ratio"],
        "diagnostics": codes,
        "plan_has_chapters": "这份材料自带的章节" in outcome["plan_user"],
        "plan_has_graph": "这几份材料没有章节标题" in outcome["plan_user"],
        "plan_seconds": outcome.get("plan_seconds"),
    }


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true", help="只量 §二，不跑模型")
    parser.add_argument("--repeat", type=int, default=1, help="每臂跑几次")
    parser.add_argument("--branches", type=int, default=BRANCHES)
    parser.add_argument("--topic", default=TOPIC)
    parser.add_argument("--fixture", default="markdown + paper 混",
                        help="A/B 用哪一组材料（默认 24 份那份）")
    parser.add_argument("--save", default="", help="报告写到这个路径（JSON）")
    args = parser.parse_args(argv)

    _fill_env_from_registry()
    os.environ.setdefault("MINDMAP_DB_PATH", str(BACKEND / "data" / "mindmaps.db"))
    os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "0"    # 材料已经定死，不许再补
    os.environ["MINDMAP_RERANK"] = "0"                # 少一次网络往返；只影响选题
    os.environ["MINDMAP_SECTION_SLICE"] = "0"         # 切章只在单份材料上生效，这里是 24 份
    os.environ.setdefault("MINDMAP_NEO4J_URI", "bolt://127.0.0.1:7687")
    os.environ.setdefault("MINDMAP_NEO4J_USER", "neo4j")

    from app.knowledge import graph_store, library

    rows = library.list_sources(limit=200)
    loaded = [library.load(row["id"]) for row in rows]
    pairs = [(row, source) for row, source in zip(rows, loaded) if source is not None]
    rows = [row for row, _source in pairs]
    loaded = [source for _row, source in pairs]

    report = probe(rows, loaded)
    print(f"\n图：configured={graph_store.configured()} available={graph_store.available()}"
          f" 全图 {graph_store.stats() if graph_store.configured() else '（没配）'}", flush=True)

    if args.probe:
        if args.save:
            Path(args.save).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"已写入 {args.save}", flush=True)
        return 0

    sources = _fixtures(rows, loaded)[args.fixture]
    print(f"\nA/B 材料：{args.fixture}（{len(sources)} 份 / "
          f"{sum(len(source.text or '') for source in sources):,} 字）", flush=True)
    report["ab"] = {
        "fixture": args.fixture, "topic": args.topic, "branches": args.branches,
        "materials": [source.filename for source in sources],
        "runs": [],
    }
    for arm, mixed in (("A 现状", False), ("B 混骨架", True)):
        for index in range(1, args.repeat + 1):
            print(f"\n=== {arm} 第 {index} 次 ===", flush=True)
            outcome = await _run_once(
                topic=args.topic, branches=args.branches, sources=sources, mixed=mixed)
            if not outcome["ok"]:
                print(f"失败：{outcome['content']}", flush=True)
                report["ab"]["runs"].append(
                    {"arm": arm, "sample": index, **outcome})
                continue
            row = _row(arm, index, outcome, sources)
            print(f"  ② 分支 {row['branches']} 条 / 节点 {row['nodes']} / "
                  f"总耗时 {row['seconds']}s（骨架 {row['plan_seconds']}s）/ "
                  f"调用 {row['calls']} 次", flush=True)
            print(f"  ① 材料覆盖（引用口径）{row['material_covered']}/{len(sources)}"
                  f"（{row['material_ratio']:.0%}）"
                  f"｜标签口径 {row['label_covered']}/{len(sources)}"
                  f"（{row['label_ratio']:.0%}）", flush=True)
            for branch in row["branch_sources"]:
                print(f"      · {branch['label'][:20]:<22} 引用 {branch['sources_cited']:>2} 份",
                      flush=True)
            if row["uncovered"]:
                print(f"      · 没指到：{', '.join(name[:16] for name in row['uncovered'][:6])}"
                      + ("…" if len(row["uncovered"]) > 6 else ""), flush=True)
            print(f"  ③ 骨架忠实度 {row['faithfulness']:.1%}"
                  f"（分段 {row['faithfulness_segment']:.1%}）", flush=True)
            print(f"  分支：{' / '.join(row['labels'])}", flush=True)
            print(f"  诊断：{[code for code in row['diagnostics'] if code.startswith('GRAPH')]}",
                  flush=True)
            report["ab"]["runs"].append(row)
    if args.save:
        Path(args.save).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {args.save}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
