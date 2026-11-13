"""分支查询扩展：三组对照 + 一个粒度基准（本机可复现，无 LLM 密钥）。

这一轮把分片检索的每一支从"一条标题"变成"标题 + 2 条同义问法"，多路并行后由
`search_multi` 做 RRF 融合。要回答的问题只有一个：

    **同义问法带来的是新召回，还是"换个说法问同一件事"？**

四组的**唯一差别**是"每支发哪几条 query"（骨架、材料、切块、脚本化模型完全相同）：

  | 组 | 每支的 query |
  |---|---|
  | A   | 只有标题（今天这一版，`search_parent`，子块匹配 → 父块返回） |
  | B0  | 只有标题，但走 `search_multi`（**粒度基准**：1 路时它是单级块检索） |
  | B   | 标题 + 2 个同义问法（本轮要做的事） |
  | C   | 标题 + 2 个**材料里的真实小节名**（"扩展得很准"的上界） |

B0 不是凑数：`search_multi` 多路走的是**单级块**（`start/end` 就是匹配单元），
而 A 组是子块匹配 + 父块返回。A→B 的差里混着"粒度换了"这一项，不把它单独量
出来，就会把粒度效应记成扩展效应。所以分解是：

    A → B0   粒度效应
    B0 → B   扩展效应（本轮真正要量的东西）
    B0 → C   扩展的上界

"模型"仍然是脚本化的（`ScriptedModel`）：它按 `titles` 回骨架、按本支的 evidence
写节点。扩展那一步也由脚本给出——B 组问法是**人工按提示词要求写的**（同一分支的
不同问法、尽量贴材料用词、不引入新方面），C 组问法是材料里的小节名。因此：

  - 这些数字量的是**覆盖度**（材料被送进模型的范围），不是文笔；
  - B 组偏**有利**（人手写的问法比真实模型稳），真实模型的扩展只会更差；
  - C 组是上界（问法本身就是材料的结构标签）。

用法（在 `backend/` 下）：

    python scripts/measure_branch_expansion.py                 # 四组，纯 BM25
    python scripts/measure_branch_expansion.py --only ABC      # 只跑某几组
    python scripts/measure_branch_expansion.py --show-output   # 打印工具回给模型的 content
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
SCRIPTS = Path(__file__).resolve().parent
for path in (BACKEND, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# 复用上一轮的口径与运行器零件：段落切法、覆盖率定义、支间重合、脚本化模型。
# 复制一份的话，四组数字之间以及与本仓库既有报告之间就不可比了——而这几张表
# 唯一的用处就是互相比较。
import measure_deep_slicing as base  # noqa: E402

from app.agent.contracts import (  # noqa: E402
    AgentRunRequest,
    RunBudget,
    ToolCall,
)
from app.agent.service import run  # noqa: E402
from app.agent.tools import DEEP_BRANCH_TOP_K, ToolRegistry  # noqa: E402
from app.contracts.source import Source, SourceKind  # noqa: E402
from app.knowledge.service import (  # noqa: E402
    search_multi as real_search_multi,
    search_parent as real_search_parent,
)
from app.optimization.source_refs import _reference_problem  # noqa: E402

MATERIAL = REPO / "docs" / "RAG-DECISIONS.md"
TOPIC = "RAG 与召回：一轮评估记录与取舍"
BRANCHES = base.MAX_BRANCHES          # 8，与上一轮同一口径
SOURCE_ID = "m1"
POINTS = base.POINTS_PER_BRANCH       # 3 = DEEP_BRANCH_TOP_K

# ---------------------------------------------------------------- 两种扩展问法
#
# B 组：同义问法。写的时候逐条对照提示词的规则 2（同一分支的不同问法、不得增加
# 原分支没有的方面），并尽量用材料自己的词（"切块""混合检索""对照"）。
# 键是 `heading_titles` 的输出，也就是骨架真正拿到的标题——含两处被截到 20 字的
# （"1.1 递归切块（`knowledge/"），因为生产里骨架看到的就长这样。
SYNONYMS: dict[str, tuple[str, ...]] = {
    "一句话结论": ("结论是什么", "这篇文章的核心结论"),
    "1 已经落地的两件事": ("已经落地的有哪两件事", "现在实现的是哪两件事"),
    "1.1 递归切块（`knowledge/": ("递归切块是怎么切的", "切块参数与递归切块"),
    "1.2 混合检索（`knowledge/": ("混合检索是怎么做的", "BM25 与向量怎么融合"),
    "2 实测数据": ("实测数据有哪些", "跑了哪些实验数字是多少"),
    "2.1 切块：四份真实材料的对照": ("四份真实材料的切块对照", "切块在四份材料上的结果"),
    "2.2 混合检索：两个查询的对照": ("两个查询的混合检索对照", "混合检索在两个查询上的结果"),
    "2.3 多轮判断：发散（负结果，重要）": ("多轮判断为什么会发散", "多轮判断的负结果"),
}

# C 组：材料里真实存在的小节名（完整、不截断）。每支取"下一节"与"下下节"——
# 论证型文档里相邻小节本来就相关，所以这是"扩展得很准"的合理近似，而不是
# 随便挑两个别处的标题。顺序取、不回绕：末尾两支会给到文档尾部的小节。
SECTION_HEADINGS: tuple[str, ...] = (
    "一句话结论",
    "1 已经落地的两件事",
    "1.1 递归切块（`knowledge/service.py`）",
    "1.2 混合检索（`knowledge/embedding.py`、`vector_cache.py`、`service.py`）",
    "2 实测数据",
    "2.1 切块：四份真实材料的对照",
    "2.2 混合检索：两个查询的对照",
    "2.3 多轮判断：发散（负结果，重要）",
    "3 评估后决定不做的",
    "一条容易误用的证据",
    "4 三种检索形态，别混着谈",
    "5 下一步：结构引导的召回",
    "6 依据来源",
    "7 未验证与已知限制",
)


def _neighbour_headings(titles: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    """每支标题 → 后面两个小节名。用完整小节名而不是截断版的标题。"""
    plans: dict[str, tuple[str, ...]] = {}
    for index, title in enumerate(titles):
        headings = SECTION_HEADINGS[index + 1:index + 3]
        plans[title] = headings
    return plans


# ---------------------------------------------------------------- 脚本化扩展器


def _titles_from_prompt(user: str) -> list[str]:
    """从 `plan_branches` 的用户消息里取出标题清单（与 `_branches_message` 同源）。"""
    section = user.split("一级分支标题", 1)[-1]
    return [match.group(1).strip()
            for match in re.finditer(r"^\s*\d+\.\s+(.+)$", section, flags=re.M)]


def _scripted_expander(mapping: dict[str, tuple[str, ...]]):
    """按标题查表，回一段**合法 JSON 文本**（与真实模型的出口形态一致）。

    返回字符串而不是 dict：`plan_branches` 的 `_payload` 会走 `parse_model_json`，
    让那条解析路径真跑起来，测的才是生产链路。
    """
    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None):
        titles = _titles_from_prompt(user)
        payload = {title: list(mapping.get(title, ())) for title in titles}
        return json.dumps(payload, ensure_ascii=False)

    return chat


# ---------------------------------------------------------------- 单组运行


@dataclass
class GroupResult:
    name: str
    label: str
    ok: bool
    coverage: float
    coverage_any: float
    refs_written: int
    refs_kept: int
    refs_verbatim: int
    nodes: int
    branches: int
    calls: int
    queries: int
    elapsed: float
    per_branch: list[tuple[tuple[str, ...], list]] = field(default_factory=list)
    output: str = ""
    notes: list[str] = field(default_factory=list)


def run_group(
    name: str, label: str, *, text: str, spans: list[tuple[int, int]],
    titles: tuple[str, ...], expander, source_id: str, single_route: bool = False,
) -> GroupResult:
    """跑一次 `mindmap_generate_deep`。

    `expander=None` 表示开关关着（A 组：走 `search_parent`）。
    `single_route=True` 表示**只有标题但走 `search_multi`**（B0 组：粒度基准）——
    生产代码里"有扩展"才走多路，所以这一组只能在脚本层替换 `_search_branch`，
    它不改任何生产行为，只回答"把检索路径换成单级块会怎样"。
    """
    source = Source(id=source_id, kind=SourceKind.document, status="parsed",
                    filename=MATERIAL.name, text=text)
    request = AgentRunRequest(
        goal=TOPIC, intent="create", sources=[source],
        budget=RunBudget(max_model_calls=80, max_tool_calls=12,
                         max_output_tokens=65536),
    )
    probes = base.make_probes()
    per_branch: list[tuple[tuple[str, ...], list]] = []
    model = base.ScriptedModel(spans=spans, text=text, titles=titles,
                               source_id=source_id)

    def measuring_parent(sources, query, top_k=5, *, filters=None):
        hits = real_search_parent(sources, query, top_k, filters=filters)
        probes.searches.append((query, top_k))
        per_branch.append(((query,), list(hits)))
        return hits

    async def measuring_multi(sources, queries, top_k=5, *, filters=None, hyde=None):
        hits = await real_search_multi(sources, queries, top_k, filters=filters,
                                       hyde=hyde)
        probes.searches.append((" | ".join(queries), top_k))
        per_branch.append((tuple(queries), list(hits)))
        return hits

    async def one_route_branch(sources, title, intents=(), top_k=DEEP_BRANCH_TOP_K):
        hits = await real_search_multi(sources, [title], top_k)
        probes.searches.append((title, top_k))
        per_branch.append(((title,), list(hits)))
        return hits

    outputs: list[str] = []
    original_execute = ToolRegistry.execute

    async def recording_execute(self, call, ctx):
        result = await original_execute(self, call, ctx)
        outputs.append(f"{call.name}: {result.content}")
        return result

    script = [base._completion([ToolCall(
        call_id="c-deep", name="mindmap_generate_deep",
        arguments={"topic": TOPIC, "branches": len(titles),
                   "points_per_branch": POINTS, "max_depth": 2})]),
        base._completion([])]

    patches = [
        patch.dict(os.environ, {
            "MINDMAP_SOURCE_MODE": "rag",
            "MINDMAP_PARENT_RETRIEVAL": "1",
            "MINDMAP_QUERY_EXPANSION": "0" if expander is None else "1",
            # 没有凭证时 `chat_async` 会先检查配置；脚本化出口替换的是模块级
            # 名字，配置在校验之前就该被绕开，所以这里只需要"看起来配了"。
            "MINDMAP_LLM_BASE_URL": "http://example.invalid/v1",
            "MINDMAP_LLM_API_KEY": "scripted",
            "MINDMAP_LLM_MODEL": "scripted",
            "MINDMAP_LLM_MODE": "model",
        }),
        patch("app.agent.tools.chat_async", new=model),
        patch("app.agent.tools.search_parent", new=measuring_parent),
        patch("app.agent.tools.search_multi", new=measuring_multi),
        patch.object(ToolRegistry, "execute", new=recording_execute),
    ]
    if expander is not None:
        patches.append(patch("app.knowledge.query_plan.chat_async", new=expander))
    if single_route:
        patches.append(patch("app.agent.tools._search_branch", new=one_route_branch))

    started = time.perf_counter()
    events = []
    for context in patches:
        context.start()
    try:
        async def drain():
            return [event async for event in
                    run(request, provider=base.ScriptedProvider(script, probes=probes))]

        events = asyncio.run(drain())
    finally:
        for context in reversed(patches):
            context.stop()
    elapsed = time.perf_counter() - started

    done = [event for event in events if event.type == "done"]
    content = done[-1].content if done else None
    ok = content is not None and events[-1].type == "done"

    refs: list = []
    nodes = 0
    branches = 0
    if content is not None:
        stack = [content.root]
        while stack:
            node = stack.pop()
            nodes += 1
            refs.extend(node.source_refs)
            stack.extend(node.children)
        branches = len(content.root.children)

    starts_hit: set[int] = set()
    any_hit: set[int] = set()
    for ref in refs:
        span = base._ref_span(text, ref)
        if span is None:
            continue
        index = base._best_paragraph(spans, span[0], span[1])
        if index is not None:
            starts_hit.add(index)
        any_hit.update(base._paragraphs_touched(spans, span[0], span[1]))

    verbatim = sum(1 for ref in refs
                   if _reference_problem(ref, source if ref.source_id == source.id
                                         else None) is None)

    notes: list[str] = []
    for event in events:
        if event.type == "tool_end" and event.name == "mindmap_generate_deep":
            retrieval = event.result_summary.get("retrieval") or {}
            notes.append(f"扩展支={retrieval.get('expanded_branches')}"
                         f" 有证据支={retrieval.get('branches_with_hits')}/{branches}")

    return GroupResult(
        name=name, label=label, ok=ok,
        coverage=len(starts_hit) / len(spans) if spans else 0.0,
        coverage_any=len(any_hit) / len(spans) if spans else 0.0,
        refs_written=model.refs_written, refs_kept=len(refs), refs_verbatim=verbatim,
        nodes=nodes, branches=branches,
        calls=len(per_branch), queries=sum(len(queries) for queries, _ in per_branch),
        elapsed=elapsed, per_branch=per_branch, output="\n".join(outputs), notes=notes,
    )


# ---------------------------------------------------------------- 支间重合


def overlap_stats(per_branch: list[tuple[tuple[str, ...], list]],
                  spans: list[tuple[int, int]], limit: int = POINTS) -> dict[str, float]:
    """五个数字里的三个：共用 top-1、两两 Jaccard、并集段落数。

    与 `measure_deep_slicing.branch_overlap` 同一口径（`_best_paragraph` 按最大重叠
    归属），只是返回数字而不是打印——四组要用同一份代码算才可比。
    """
    sets: list[set[int]] = []
    tops: list[int] = []
    for _, hits in per_branch:
        sets.append({index for index in (
            base._best_paragraph(spans, hit.start, hit.end) for hit in hits[:limit])
            if index is not None})
        tops.append(hits[0].start if hits else -1)
    shared = max(Counter(tops).values(), default=0)
    union = set().union(*sets) if sets else set()
    jaccards: list[float] = []
    for left in range(len(sets)):
        for right in range(left + 1, len(sets)):
            if sets[left] or sets[right]:
                jaccards.append(len(sets[left] & sets[right])
                                / len(sets[left] | sets[right]))
    return {
        "shared_top": shared,
        "jaccard": sum(jaccards) / len(jaccards) if jaccards else 0.0,
        "union": len(union),
        "union_rate": len(union) / len(spans) if spans else 0.0,
    }


# ---------------------------------------------------------------- 报告


def report(results: list[GroupResult], spans: list[tuple[int, int]], chars: int,
           titles: tuple[str, ...]) -> None:
    print(f"材料 {MATERIAL.name}：{chars:,} 字，切 {len(spans)} 段；骨架 {len(titles)} 支"
          f"（材料小节标题，与上一轮同口径截到 20 字）；纯 BM25，每支 top-{POINTS}")
    print()
    header = ("组", "每支 query", "覆盖率", "上界", "共用top1", "Jaccard",
              "并集段落", "检索次数(调用/查询)", "树上引用 写出/留存/逐字")
    rows = []
    for item in results:
        stats = overlap_stats(item.per_branch, spans)
        per_query = (f"{item.queries / item.calls:.1f}" if item.calls else "-")
        rows.append((
            item.name,
            per_query,
            f"{item.coverage:.1%}" if item.calls and item.nodes else "—",
            f"{item.coverage_any:.1%}" if item.calls and item.nodes else "—",
            f"{stats['shared_top']}/{len(item.per_branch)}",
            f"{stats['jaccard']:.2f}",
            f"{stats['union']}/{len(spans)}",
            f"{item.calls}/{item.queries}",
            (f"{item.refs_written}/{item.refs_kept}/{item.refs_verbatim}"
             if item.nodes else "—"),
        ))
    widths = [max(len(str(row[index])) for row in [header, *rows])
              for index in range(len(header))]
    for row in [header, *rows]:
        print("  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)))
    print()
    for item in results:
        if item.notes:
            print(f"{item.name:3s} {item.label}：{'；'.join(item.notes)}")

    by_name = {item.name: item for item in results}
    print()
    print("分解（每支 top-3 命中的段落并集）：")
    for left, right, label in (("A", "B0", "A→B0 粒度效应"),
                               ("B0", "B", "B0→B 扩展效应"),
                               ("B0", "C", "B0→C 扩展上界")):
        if left in by_name and right in by_name:
            before = overlap_stats(by_name[left].per_branch, spans)
            after = overlap_stats(by_name[right].per_branch, spans)
            print(f"  {label}：并集 {before['union']}→{after['union']}"
                  f"（{after['union'] - before['union']:+d} 段），"
                  f"Jaccard {before['jaccard']:.2f}→{after['jaccard']:.2f}，"
                  f"共用 top-1 {before['shared_top']}→{after['shared_top']}")

    # 段落是"归属"口径（一条命中按最大重叠归给一段），大块跨两段时只看得出其中一段。
    # 块级差集是更硬的一条：**扩展到底有没有把一块全新的原文捞回来**。
    print()
    print("块级对照（每支 top-3 的 (source,start,end) 并集；块边界三组同源）：")
    blocks = {item.name: {(hit.source_id, hit.start, hit.end)
                          for _, hits in item.per_branch for hit in hits[:POINTS]}
              for item in results}
    anchor = blocks.get("A", set())
    reference = blocks.get("B0", anchor)
    for item in results:
        own = blocks[item.name]
        print(f"  {item.name:3s} {len(own):2d} 块；相对 A 新增 {len(own - anchor):d}"
              f"；相对 B0 新增 {len(own - reference):d}")
    if "B" in blocks and "B0" in blocks:
        print(f"  → B 相对 B0 新增的块：{sorted(blocks['B'] - blocks['B0'])}")
        print(f"  → B 相对 B0 丢掉的块：{sorted(blocks['B0'] - blocks['B'])}")

    print()
    print("各组每支实际发出的 query（前三支）：")
    for item in results:
        print(f"  [{item.name}] {item.label}")
        for queries, hits in item.per_branch[:3]:
            print(f"      {' | '.join(queries)}  →  {len(hits)} 条命中")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="A B0 B C", help="只跑某几组（空格分隔）")
    parser.add_argument("--show-output", action="store_true",
                        help="打印工具回给模型的那段 content（真实输出）")
    args = parser.parse_args()
    wanted = set(args.only.replace(",", " ").split())

    text = base.load_material(MATERIAL)
    spans = base.segment(text)
    titles = base.heading_titles(text)[:BRANCHES]        # 与上一轮同口径（截 20 字）
    synonyms = {title: SYNONYMS.get(title, ()) for title in titles}
    headings = _neighbour_headings(titles)

    results: list[GroupResult] = []
    plans = {
        "A": ("只有标题（今天）", None, False),
        "B0": ("只有标题，走 search_multi（粒度基准）", None, True),
        "B": ("标题 + 2 个同义问法（人工按提示词规则写）",
              _scripted_expander(synonyms), False),
        "C": ("标题 + 2 个材料真实小节名（上界）",
              _scripted_expander(headings), False),
    }
    for name in ("A", "B0", "B", "C"):
        if name not in wanted:
            continue
        label, expander, single_route = plans[name]
        results.append(run_group(name, label, text=text, spans=spans, titles=titles,
                                 expander=expander, source_id=SOURCE_ID,
                                 single_route=single_route))
    # 表格按 A / B0 / B / C 排，与文档里的行序一致
    order = {name: index for index, name in enumerate(("A", "B0", "B", "C"))}
    results.sort(key=lambda item: order.get(item.name, 99))

    report(results, spans, len(text), titles)
    if args.show_output:
        print()
        for item in results:
            if item.output:
                print(f"[{item.name}] {item.output}")
                print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
