"""A/B：第二段给「整份材料」还是给「每支自己那一章的原文」。

    python scripts/measure_section_slice.py                        # 两臂各跑一次
    python scripts/measure_section_slice.py --repeat 2             # 采样
    python scripts/measure_section_slice.py --material FILE --save out.json

两个 arm 是同一份代码、同一个骨架形态，**唯一**的差别是第二段（结构）怎么发：

  - `full`（`MINDMAP_SECTION_SLICE` 不设）：结构与说明各拿**整份正文**，一次写完
    （这就是现状——`FULL_TEXT_MATERIAL_CHARS = 50_000`，14k 字的材料整份进 prompt）；
  - `slice`（`MINDMAP_SECTION_SLICE=1`）：骨架定好之后**每章一次调用**，每次只给它
    自己那一章的原文，组装成整棵树；第三段（只写 detail）与上面**逐字节相同**。

测量口径（六个数，前两个是核心）：

  1. **每章覆盖率**：这一章的字有多少被**活下来的引用**覆盖（按章内区间并集算）。
     与"全材料覆盖率"不同——那个数被长章拉平，看不出"某一章整章没被写"；
  2. **跨支重复**：`_cross_branch_duplicates` 的**节点数**，并把重复的 label 与它
     出现在哪几支打出来（分片那版是 6 处，判据同源）；
  3. `detail` 平均 / 最短：第三段没改，但要确认它没被前面的 N 次调用影响；
  4. 节点数与**有没有撞 `INITIAL_MAX_NODES` 被裁**（`cut_nodes` / `partial`）；
  5. 耗时：三段各自 + 总墙钟；
  6. token：按 `estimate_input_tokens` 估（输入 = Σ system+user，输出 = Σ 响应原文）。

**骨架形态在两臂里是一样的**：`SKELETON_GROUP_THRESHOLD` 抬到不可能达到（两臂都
不请求两级）。这不是为了美化现状，而是为了让"差异只可能来自第二段"——两级骨架是
第三个变量，混进来之后"覆盖率为什么变"就说不清了（`measure_two_tier.py` 用同一种
手法造 arm）。这一条必须写进报告。

凭据与开关沿用 `measure_two_tier.py` 的约定：进程环境里没有就从用户级
（`HKCU\\Environment`）补；外部检索与库检索关掉（它们要外网，会污染对比）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

CREDENTIALS = (
    "MINDMAP_LLM_MODE", "MINDMAP_LLM_BASE_URL", "MINDMAP_LLM_API_KEY",
    "MINDMAP_LLM_MODEL", "MINDMAP_EMBEDDING_BASE_URL", "MINDMAP_EMBEDDING_API_KEY",
    "MINDMAP_EMBEDDING_PROTOCOL", "MINDMAP_EMBEDDING_MODEL", "MINDMAP_EMBEDDING_DIM",
)

DEFAULT_MATERIAL = Path(__file__).resolve().parents[2] / "docs" / "RAG-DECISIONS.md"
DEFAULT_TOPIC = "检索增强生成的技术决策"
ARMS = ("full", "slice")


def _user_env(name: str) -> str:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(value)


def _fill_credentials() -> None:
    for name in CREDENTIALS:
        if not os.environ.get(name):
            value = _user_env(name)
            if value:
                os.environ[name] = value
    os.environ.setdefault("MINDMAP_LLM_MODE", "model")
    os.environ.setdefault("MINDMAP_LLM_BASE_URL", "https://api.deepseek.com/v1")
    os.environ.setdefault("MINDMAP_LLM_MODEL", "deepseek-chat")


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _chapter_coverage(root, text: str, chapters) -> list[dict]:
    """每一章：被引用覆盖的字数 / 这一章的字数（按章内区间并集算）。

    另外两个数列在这里而不是另算一遍：**引用条数**落在本章的有几条、**节点数**有
    几个至少引用了一次本章。三个数一起看才说得清"这一章被写了多少"——只有一个
    覆盖率时，"5%"到底是"引了一句"还是"引了五句短话"分不出来。
    """
    spans: list[tuple[int, int]] = []
    per_node: list[list[tuple[int, int]]] = []
    for node in _walk(root):
        mine: list[tuple[int, int]] = []
        for ref in node.source_refs:
            if ref.start is None or ref.end is None or ref.end <= ref.start:
                continue
            spans.append((ref.start, ref.end))
            mine.append((ref.start, ref.end))
        if mine:
            per_node.append(mine)
    rows = []
    for chapter in chapters:
        clipped = [(max(low, chapter.start), min(high, chapter.end))
                   for low, high in spans
                   if low < chapter.end and high > chapter.start]
        clipped.sort()
        covered, cursor = 0, chapter.start
        for low, high in clipped:
            low = max(low, cursor)
            if high > low:
                covered += high - low
                cursor = high
        nodes = sum(1 for mine in per_node
                    if any(low < chapter.end and high > chapter.start
                           for low, high in mine))
        rows.append({
            "chapter": chapter.index,
            "title": chapter.title,
            "chars": chapter.chars,
            "covered": covered,
            "share": round(covered / chapter.chars, 3) if chapter.chars else 0.0,
            "refs": len(clipped),
            "nodes": nodes,
        })
    return rows


def _branch_of(root, node_id: str) -> str:
    """节点挂在哪条一级分支下（一级分支自己返回它自己）。"""
    stack = [(child, child.label) for child in root.children]
    while stack:
        node, label = stack.pop()
        if node.id == node_id:
            return label
        stack.extend((child, label) for child in node.children)
    return "根"


def _duplicate_rows(root, duplicates) -> list[dict]:
    nodes = {node.id: node for node in _walk(root)}
    rows = []
    for label, places in duplicates:
        holders = []
        for node in nodes.values():
            if node.label.replace(" ", "") == label.replace(" ", ""):
                holders.append(_branch_of(root, node.id))
        rows.append({"label": label, "branches": sorted(set(holders)), "places": places})
    return rows


def _classify(system: str) -> str:
    from app.agent.tools import (
        DETAIL_ONLY_PROMPT, PLAN_PROMPT, SECTION_SLICE_PROMPT,
    )

    if system.startswith(PLAN_PROMPT):
        return "skeleton"
    if system.startswith(SECTION_SLICE_PROMPT):
        return "slice"
    if system.startswith(DETAIL_ONLY_PROMPT):
        return "detail"
    return "structure"


async def _run_arm(*, arm: str, text: str, topic: str, branches: int):
    from app.agent import tools as tools_module
    from app.agent.contracts import (
        AgentRunRequest, RunBudget, RunContext, ToolCall,
    )
    from app.agent.tools import build_registry
    from app.contracts.source import Source, SourceKind
    from app.generation.service import estimate_input_tokens

    calls = {"skeleton": 0, "structure": 0, "slice": 0, "detail": 0}
    tokens = {"in": 0, "out": 0}
    real_chat = tools_module.chat_async

    async def counting_chat(system: str, user: str, **kwargs):
        kind = _classify(system)
        calls[kind] += 1
        tokens["in"] += estimate_input_tokens(system) + estimate_input_tokens(user)
        result = await real_chat(system, user, **kwargs)
        tokens["out"] += estimate_input_tokens(
            json.dumps(result, ensure_ascii=False))
        return result

    source = Source(id="s-rag", kind=SourceKind.markdown, status="parsed",
                    filename="RAG-DECISIONS.md", text=text)
    request = AgentRunRequest(
        goal=topic, intent="create", thinking_model="default", sources=[source],
        budget=RunBudget(max_model_calls=60, max_tool_calls=8, max_seconds=900.0,
                         max_output_tokens=65536, max_repairs=1),
    )
    ctx = RunContext(request=request, sources_by_id={source.id: source})
    registry = build_registry("create", has_sources=True, has_library=False)
    call = ToolCall(call_id="measure", name="mindmap_generate",
                    arguments={"topic": topic, "branches": branches})

    if arm == "slice":
        os.environ["MINDMAP_SECTION_SLICE"] = "1"
    else:
        os.environ.pop("MINDMAP_SECTION_SLICE", None)
    threshold = tools_module.SKELETON_GROUP_THRESHOLD
    tools_module.SKELETON_GROUP_THRESHOLD = 10**6      # 两臂都不分组
    tools_module.chat_async = counting_chat
    started = time.perf_counter()
    try:
        result = await registry.execute(call, ctx)
        elapsed = time.perf_counter() - started
    finally:
        tools_module.chat_async = real_chat
        tools_module.SKELETON_GROUP_THRESHOLD = threshold

    if not result.ok:
        return {"ok": False, "arm": arm, "content": result.content}, None

    from app.agent.tools import _chapter_slices, _cross_branch_duplicates

    content = ctx.candidate
    root = content.root
    chapters = _chapter_slices([source])
    nodes = [node for node in _walk(root) if node is not root]
    details = [len((node.detail or "").strip()) for node in nodes]
    duplicates = _cross_branch_duplicates(root)
    coverage = _chapter_coverage(root, text, chapters)
    data = result.data
    return {
        "ok": True,
        "arm": arm,
        "chapters": len(chapters),
        "coverage": coverage,
        "coverage_avg": round(sum(row["share"] for row in coverage)
                              / max(1, len(coverage)), 3),
        "coverage_full": sum(row["covered"] for row in coverage),
        "coverage_total_chars": sum(row["chars"] for row in coverage),
        "chapters_hit": sum(1 for row in coverage if row["share"] > 0),
        "tree": content.model_dump(),
        "nodes": data["node_count"],
        "cut_nodes": data.get("cut_nodes"),
        "partial": data.get("partial"),
        "width": len(root.children),
        "planned_branches": data.get("planned_branches"),
        "detail_avg": round(sum(details) / max(1, len(details)), 1),
        "detail_min": min(details, default=0),
        "detail_filled": (data.get("detail") or {}).get("filled"),
        "detail_requested": (data.get("detail") or {}).get("requested"),
        "duplicates": len(duplicates),
        "duplicate_rows": _duplicate_rows(root, duplicates),
        "refs": (data.get("source_refs") or {}).get("kept"),
        "refs_declared": (data.get("source_refs") or {}).get("declared"),
        "seconds": round(elapsed, 1),
        "stage_seconds": data.get("calls"),
        "calls": dict(calls),
        "calls_total": sum(calls.values()),
        "tokens": dict(tokens),
        "section_slice": data.get("section_slice"),
        "diagnostics": sorted({item["code"] for item in data["diagnostics"]}),
    }, content


def _table(rounds: list[dict[str, dict]]) -> str:
    arms = list(ARMS)
    head = "| 指标 | " + " | ".join(
        f"{arm}#{index}" for index in range(1, len(rounds) + 1) for arm in arms) + " |"
    sep = "|---|" + "---|" * (len(rounds) * len(arms))

    def cell(key: str, fmt=str, pick=None) -> str:
        values = []
        for report in rounds:
            for arm in arms:
                item = report.get(arm) or {}
                value = item.get(key) if pick is None else pick(item)
                values.append(fmt(value) if item.get("ok") else "失败")
        return "| " + " | ".join(values) + " |"

    rows = [
        ("每章覆盖率（平均）", cell("coverage_avg", lambda v: f"{v:.1%}")),
        ("**有引用的章数 / 总章数**", cell(
            "chapters_hit",
            pick=lambda item: f"{item['chapters_hit']}/{item['chapters']}")),
        ("覆盖率合计（字 / 全文）", cell("coverage_full")),
        ("总节点数", cell("nodes")),
        ("被裁节点数", cell("cut_nodes")),
        ("partial", cell("partial")),
        ("一级分支数", cell("width")),
        ("跨支重复（节点）", cell("duplicates")),
        ("detail 平均", cell("detail_avg")),
        ("detail 最短", cell("detail_min")),
        ("写回说明/需要", cell(
            "detail_filled",
            lambda v: v if v is not None else "—",
            pick=lambda item: f"{item['detail_filled']}/{item['detail_requested']}")),
        ("引用条数（保留）", cell("refs")),
        ("模型调用", cell("calls_total", pick=lambda item: "+".join(
            str(item["calls"][key]) for key in ("skeleton", "structure", "slice", "detail")))),
        ("输入 token（估）", cell("tokens", pick=lambda item: f"{item['tokens']['in']:,}")),
        ("输出 token（估）", cell("tokens", pick=lambda item: f"{item['tokens']['out']:,}")),
        ("总耗时(s)", cell("seconds")),
    ]
    return head + "\n" + sep + "\n" + "\n".join(
        f"| {label} |{values}" for label, values in rows)


def _coverage_table(rounds: list[dict[str, dict]]) -> str:
    arms = list(ARMS)
    head = "| 章 | 字数 | " + " | ".join(
        f"{arm}#{index}" for index in range(1, len(rounds) + 1) for arm in arms) + " |"
    sep = "|---" * (2 + len(rounds) * len(arms)) + "|"
    lines = []
    first = rounds[0].get(arms[0]) or {}
    for index, row in enumerate(first.get("coverage") or []):
        cells = []
        for report in rounds:
            for arm in arms:
                item = report.get(arm) or {}
                rows = item.get("coverage") or []
                share = rows[index]["share"] if index < len(rows) else 0.0
                refs = rows[index]["refs"] if index < len(rows) else 0
                cells.append(f"{share:.0%}（{refs} 条）")
        lines.append(f"| {row['chapter']}. {row['title'][:22]} | {row['chars']} | "
                     + " | ".join(cells) + " |")
    return head + "\n" + sep + "\n" + "\n".join(lines)


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--material", default=str(DEFAULT_MATERIAL))
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--branches", type=int, default=12)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--save", default="")
    args = parser.parse_args(argv)

    _fill_credentials()
    os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "0"
    os.environ["MINDMAP_LIBRARY_SEARCH"] = "0"
    os.environ["MINDMAP_LIBRARY_REMEMBER"] = "0"
    os.environ.setdefault("MINDMAP_DB_PATH", "data/measure_section_slice.db")

    text = Path(args.material).read_text(encoding="utf-8")
    rounds: list[dict[str, dict]] = []
    for round_index in range(1, max(1, args.repeat) + 1):
        reports: dict[str, dict] = {}
        for arm in ARMS:
            report, _content = await _run_arm(
                arm=arm, text=text, topic=args.topic, branches=args.branches)
            reports[arm] = report
            if report["ok"]:
                calls = "+".join(str(report["calls"][key])
                                 for key in ("skeleton", "structure", "slice", "detail"))
                print(f"[{round_index}] {arm}: 覆盖率 "
                      f"{report['coverage_avg']:.1%}｜{report['nodes']} 节点"
                      f"（被裁 {report['cut_nodes']}）｜重复 {report['duplicates']}"
                      f"｜detail {report['detail_avg']}｜{report['seconds']}s"
                      f"｜调用 {calls}", file=sys.stderr)
            else:
                print(f"[{round_index}] {arm}: 失败 {report['content']}", file=sys.stderr)
        rounds.append(reports)

    print(_table(rounds))
    print()
    print(_coverage_table(rounds))
    print()
    for index, report in enumerate(rounds, start=1):
        for arm in ARMS:
            item = report.get(arm) or {}
            if not item.get("ok"):
                continue
            print(f"\n### {arm}#{index} 跨支重复明细（{item['duplicates']} 处）")
            if not item["duplicate_rows"]:
                print("（无）")
            for row in item["duplicate_rows"]:
                print(f"- 「{row['label']}」出现在 {'、'.join(row['branches'])}")
            print(f"\n### {arm}#{index} 诊断：{', '.join(item['diagnostics'])}")
            if item.get("section_slice"):
                print(f"\n### {arm}#{index} 按章切账：{json.dumps(item['section_slice'], ensure_ascii=False)}")
    if args.save:
        Path(args.save).write_text(
            json.dumps(rounds, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n报告已写到 {args.save}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
