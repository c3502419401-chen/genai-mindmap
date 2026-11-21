"""A/B：第二段（结构）与第三段（说明）各自给什么证据。

    python scripts/measure_section_slice.py                        # 两组各跑一次
    python scripts/measure_section_slice.py --repeat 2             # 采样
    python scripts/measure_section_slice.py --arms slice,detail    # 只跑第三段这组 A/B
    python scripts/measure_section_slice.py --material FILE --save out.json

三组 arm 是同一份代码、同一个骨架形态，差别只有**开关**：

  - `full`（两个开关都不设）：结构与说明各拿**整份正文**，一次写完
    （这就是改动之前的现状——`FULL_TEXT_MATERIAL_CHARS = 50_000`，14k 字的材料整份进 prompt）；
  - `slice`（`MINDMAP_SECTION_SLICE=1`）：骨架定好之后**每章一次调用**，每次只给它
    自己那一章的原文，组装成整棵树。第三段拿的还是整份正文；
  - `detail`（`MINDMAP_SECTION_SLICE=1` + `MINDMAP_DETAIL_RETRIEVAL=1`）：结构同上，
    **第三段的证据换成"每个节点在它自己那一章内检索到的块"**。`slice` 与 `detail`
    一起跑，就把第三段的变量单独隔出来了（第二段两组完全一样）。

测量口径（前两个是核心）：

  1. **每章覆盖率**：这一章的字有多少被**活下来的引用**覆盖（按章内区间并集算）。
     与"全材料覆盖率"不同——那个数被长章拉平，看不出"某一章整章没被写"；
  2. **引用落点**：引用条数、**不同的**区间数、落在多少个不同段落 / 多少章上
     ——"条数涨了"不等于"覆盖了更多地方"，两个数必须一起看；
  3. 跨支重复：`_cross_branch_duplicates` 的**节点数**，并打出重复的 label 与它
     出现在哪几支（分片那版是 6 处，判据同源）；
  4. `detail` 平均 / 最短 / **章内贴合度**（说明的词组有多少落在它自己那一章里）：
     第三段的证据一变，这三个数最先动；
  5. 节点数与**有没有撞 `INITIAL_MAX_NODES` 被裁**（`cut_nodes` / `partial`）；
  6. 耗时：三段各自 + 总墙钟；token 按 `estimate_input_tokens` 估。

**骨架形态在各组里是一样的**：`SKELETON_GROUP_THRESHOLD` 抬到不可能达到（谁都不请求
两级）。这不是为了美化现状，而是为了让"差异只可能来自被切换的那一段"——两级骨架是
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
import re
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

# 每个 arm 只要设这几个环境变量，别的都一样。
ARM_ENV: dict[str, dict[str, str]] = {
    "full": {},
    "slice": {"MINDMAP_SECTION_SLICE": "1"},
    "detail": {"MINDMAP_SECTION_SLICE": "1", "MINDMAP_DETAIL_RETRIEVAL": "1"},
}
DEFAULT_ARMS = ("full", "slice")


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


def _paragraphs(text: str) -> list[tuple[int, int]]:
    """原文的段落区间（空行分隔）。"引用落在多少段上"的分母就是它的长度。"""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in re.finditer(r"\n\s*\n", text):
        spans.append((cursor, match.start()))
        cursor = match.end()
    spans.append((cursor, len(text)))
    return [(low, high) for low, high in spans if text[low:high].strip()]


def _ref_places(root, text: str) -> dict:
    """引用落在**多少个不同的地方**：条数、不同区间数、不同段落数。

    为什么单看条数会骗人：38 条挤在 3 段里与 38 条散在 30 段里是同一件事的两种说法，
    而覆盖率只认**并集**——前者的"条数翻倍"可能一分覆盖率都不加。三个数一起看才
    说得清"这条路买到的是更多依据，还是更啰嗦的引用"。
    """
    refs = [(ref.start, ref.end) for node in _walk(root) for ref in node.source_refs
            if ref.start is not None and ref.end is not None and ref.end > ref.start]
    spans = sorted(set(refs))
    paragraphs = _paragraphs(text)
    return {
        "refs": sum(len(node.source_refs) for node in _walk(root)),
        "spans": len(spans),
        "paragraphs": sum(1 for low, high in paragraphs
                          if any(start < high and end > low for start, end in spans)),
        "total_paragraphs": len(paragraphs),
    }


def _detail_affinity(root, text: str, chapters, *, gram: int = 2) -> float | None:
    """说明有多"贴着自己那一章"：节点说明的字符二元组里落在本章的比例（均值）。

    这是第三段**能**动的质量指标。引用条数由第二段决定（`_slice_refs` 在章内定位
    模型抄的 quote），第三段一个字都不写引用——所以要判"按节点章内检索"有没有用，
    只能看说明本身是否更贴它自己那一章：同一个节点，证据给的是整份材料时它会写到
    别的章的话题上，给的是本章的块时它就只能写这一章。
    """
    if not chapters:
        return None
    all_grams = {text[index:index + gram] for index in range(len(text) - gram + 1)}
    by_index = {f"s{chapter.index}": chapter for chapter in chapters}
    scores: list[float] = []
    for child in root.children:
        chapter = by_index.get(child.id)
        if chapter is None:
            continue
        own = text[chapter.start:chapter.end]
        own_grams = {own[index:index + gram] for index in range(len(own) - gram + 1)}
        rest = all_grams - own_grams
        stack = [child]
        while stack:
            node = stack.pop()
            stack.extend(node.children)
            detail = (node.detail or "").strip()
            if len(detail) < 20:
                continue
            grams = {detail[index:index + gram]
                     for index in range(len(detail) - gram + 1)}
            inside, outside = len(grams & own_grams), len(grams & rest)
            if inside + outside:
                scores.append(inside / (inside + outside))
    return round(sum(scores) / len(scores), 3) if scores else None


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

    for name in ("MINDMAP_SECTION_SLICE", "MINDMAP_DETAIL_RETRIEVAL"):
        os.environ.pop(name, None)
    os.environ.update(ARM_ENV[arm])
    threshold = tools_module.SKELETON_GROUP_THRESHOLD
    tools_module.SKELETON_GROUP_THRESHOLD = 10**6      # 各组都不分组
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
        "detail_affinity": _detail_affinity(root, text, chapters),
        "detail_evidence": (data.get("detail") or {}).get("evidence"),
        "duplicates": len(duplicates),
        "duplicate_rows": _duplicate_rows(root, duplicates),
        "refs": (data.get("source_refs") or {}).get("kept"),
        "refs_declared": (data.get("source_refs") or {}).get("declared"),
        "refs_places": _ref_places(root, text),
        "seconds": round(elapsed, 1),
        "stage_seconds": data.get("calls"),
        "calls": dict(calls),
        "calls_total": sum(calls.values()),
        "tokens": dict(tokens),
        "section_slice": data.get("section_slice"),
        "diagnostics": sorted({item["code"] for item in data["diagnostics"]}),
    }, content


def _table(rounds: list[dict[str, dict]], arms: list[str]) -> str:
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
        ("**detail 章内贴合度**", cell("detail_affinity",
                                     lambda v: "—" if v is None else f"{v:.1%}")),
        ("写回说明/需要", cell(
            "detail_filled",
            lambda v: v if v is not None else "—",
            pick=lambda item: f"{item['detail_filled']}/{item['detail_requested']}")),
        ("引用条数（保留）", cell("refs")),
        ("★ 引用落点：不同区间 / 段 / 章", cell(
            "refs_places",
            pick=lambda item: (f"{item['refs_places']['spans']}"
                               f" / {item['refs_places']['paragraphs']}"
                               f"（共 {item['refs_places']['total_paragraphs']} 段）"
                               f" / {item['chapters_hit']}"))),
        ("第三段证据（块 / 字 / 命中节点）", cell(
            "detail_evidence",
            pick=lambda item: ("—" if not item.get("detail_evidence") else
                               f"{item['detail_evidence']['blocks']}"
                               f" / {item['detail_evidence']['chars']:,}"
                               f" / {item['detail_evidence']['nodes_with_evidence']}"))),
        ("模型调用", cell("calls_total", pick=lambda item: "+".join(
            str(item["calls"][key]) for key in ("skeleton", "structure", "slice", "detail")))),
        ("输入 token（估）", cell("tokens", pick=lambda item: f"{item['tokens']['in']:,}")),
        ("输出 token（估）", cell("tokens", pick=lambda item: f"{item['tokens']['out']:,}")),
        ("总耗时(s)", cell("seconds")),
    ]
    return head + "\n" + sep + "\n" + "\n".join(
        f"| {label} |{values}" for label, values in rows)


def _coverage_table(rounds: list[dict[str, dict]], arms: list[str]) -> str:
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
    parser.add_argument("--arms", default=",".join(DEFAULT_ARMS),
                        help="逗号分隔，可选 " + " / ".join(ARM_ENV))
    parser.add_argument("--save", default="")
    args = parser.parse_args(argv)

    arms = [item.strip() for item in args.arms.split(",") if item.strip()]
    unknown = [item for item in arms if item not in ARM_ENV]
    if unknown or not arms:
        print(f"未知的 arm：{unknown or args.arms}（可选 {list(ARM_ENV)}）",
              file=sys.stderr)
        return 2

    _fill_credentials()
    os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "0"
    os.environ["MINDMAP_LIBRARY_SEARCH"] = "0"
    os.environ["MINDMAP_LIBRARY_REMEMBER"] = "0"
    os.environ.setdefault("MINDMAP_DB_PATH", "data/measure_section_slice.db")

    text = Path(args.material).read_text(encoding="utf-8")
    rounds: list[dict[str, dict]] = []
    for round_index in range(1, max(1, args.repeat) + 1):
        reports: dict[str, dict] = {}
        for arm in arms:
            report, _content = await _run_arm(
                arm=arm, text=text, topic=args.topic, branches=args.branches)
            reports[arm] = report
            if report["ok"]:
                calls = "+".join(str(report["calls"][key])
                                 for key in ("skeleton", "structure", "slice", "detail"))
                print(f"[{round_index}] {arm}: 覆盖率 "
                      f"{report['coverage_avg']:.1%}｜{report['nodes']} 节点"
                      f"（被裁 {report['cut_nodes']}）｜重复 {report['duplicates']}"
                      f"｜引用 {report['refs_places']['refs']}"
                      f" / 不同区间 {report['refs_places']['spans']}"
                      f"｜detail {report['detail_avg']}（贴合 "
                      f"{report['detail_affinity']}）｜{report['seconds']}s"
                      f"｜调用 {calls}", file=sys.stderr)
            else:
                print(f"[{round_index}] {arm}: 失败 {report['content']}", file=sys.stderr)
        rounds.append(reports)

    print(_table(rounds, arms))
    print()
    print(_coverage_table(rounds, arms))
    print()
    for index, report in enumerate(rounds, start=1):
        for arm in arms:
            item = report.get(arm) or {}
            if not item.get("ok"):
                continue
            print(f"\n### {arm}#{index} 跨支重复明细（{item['duplicates']} 处）")
            if not item["duplicate_rows"]:
                print("（无）")
            for row in item["duplicate_rows"]:
                print(f"- 「{row['label']}」出现在 {'、'.join(row['branches'])}")
            print(f"\n### {arm}#{index} 引用落点："
                  f"{json.dumps(item['refs_places'], ensure_ascii=False)}")
            if item.get("detail_evidence"):
                print(f"\n### {arm}#{index} 第三段证据："
                      f"{json.dumps(item['detail_evidence'], ensure_ascii=False)}")
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

