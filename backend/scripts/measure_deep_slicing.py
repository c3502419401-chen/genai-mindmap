"""分片 + 按支检索的三组对照实测（本机可复现，无 LLM 密钥）。

三组：

  A  `SOURCE_MODE=full` + `mindmap_generate`   整树，全文在 prompt 里
  B  `SOURCE_MODE=rag`  + `mindmap_generate`   整树，一次检索
  C  `SOURCE_MODE=rag`  + `mindmap_generate_deep`  分片，每支各检索一次

**测的是检索覆盖度，不是生成质量**。本机没有 `MINDMAP_LLM_*` 凭证，所以"模型"
是脚本化的（`ScriptedModel`）：它只按固定规则使用**它在 prompt 里真正看到的文字**
——看得到哪几段就写哪几个节点，命中里给什么引文就抄什么引文。于是覆盖度这个
数字反映的是"材料有没有被送进模型"，正是这次改动能影响的部分；生成质量完全
不在这个脚本的射程内。

判定口径（三组同一套，才可比）：

  - **覆盖率** = 最终树的 `source_refs` 命中的材料段落数 / 段落总数。命中的定义
    是"引用区间的**起点**落在这一段里"（父块/叶子块有重叠，用任何重叠都算会让
    覆盖率虚高）；另附"任意重叠"作上界。
  - **引用存活率** = 最终树上留存的引用条数 / 模型实际写出的引用条数，并逐条跑
    `optimization.source_refs._reference_problem`（逐字校验）。
  - **检索与耗时** = `search_parent` 调用次数、`embed_texts` 次数与文本条数、
    按 25 条一批折算的 HTTP 批数、整组墙钟时间。
  - **工具调用次数** = `tool_end` 事件数（模型视角的工具调用）。

用法（在 `backend/` 下）：

    python scripts/measure_deep_slicing.py                     # 无向量：纯 BM25
    python scripts/measure_deep_slicing.py --fake-embedding    # 注入假向量，量批量与耗时
    python scripts/measure_deep_slicing.py --skeleton generic  # 骨架标题改成通用猜测

`--skeleton` 有两个取值，对应两种"模型怎么定一级分支"：

  - `headings`（默认）：用材料自带的小节标题。真实模型在 `rag` 形态下看不到正文，
    这一条对它有利——报告里必须说明。
  - `generic`：用一组与材料用词无关的通用标题（"背景/现状/方法/数据"…），
    模拟"骨架猜偏了"的情况。两者之差就是"分支标题当 query 够不够用"的下限证据。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# 环境必须在 import 应用之前定下来：`source_mode()` 等函数每次都读 env，但
# `repository.db_path()` 这类在 import 期就可能缓存，早设比晚设安全。
_TMP = tempfile.mkdtemp(prefix="measure-slicing-")
os.environ["MINDMAP_DB_PATH"] = str(Path(_TMP) / "measure.db")
os.environ.pop("MINDMAP_HYBRID_SEARCH", None)
os.environ.setdefault("MINDMAP_PARENT_RETRIEVAL", "1")

from app.agent.contracts import (  # noqa: E402
    AgentRunRequest,
    Completion,
    RunBudget,
    ToolCall,
)
from app.agent.service import run  # noqa: E402
from app.agent.tools import (  # noqa: E402
    DEEP_MAX_BRANCHES,
    PLAN_PROMPT,
)
from app.contracts.source import Source, SourceKind  # noqa: E402
from app.optimization.source_refs import _reference_problem  # noqa: E402

# 每组的一级分支上限：与 `GenerateDeepArgs.branches` 的默认量级一致，且不超过
# 工具自己的上限（`DEEP_MAX_BRANCHES`）。
MAX_BRANCHES = min(8, DEEP_MAX_BRANCHES)
# 每支要点数：取 3 与 `DEEP_BRANCH_TOP_K` 对齐，否则一批 3 条命中会被裁到 2 条，
# 覆盖率会凭空少一截（那不是检索的问题，是参数的问题）。
POINTS_PER_BRANCH = 3

GENERIC_TITLES = ("背景", "现状", "问题", "方法", "数据", "结论", "风险", "展望")


# ---------------------------------------------------------------- 材料与段落


def load_material(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def segment(text: str, minimum: int = 80) -> list[tuple[int, int]]:
    """按空行切段，丢掉过短的段落（标题行、列表项残片）。

    段落是覆盖率的**分母**，所以切法必须固定并且三组一致。80 字的下限是为了
    让分母只包含有内容的段：一行 `## 5 下一步` 被算作"一段"会让覆盖率凭空下降，
    而它本来也不该是一个独立的"该被覆盖的内容块"。
    """
    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"[^\n](?:.*?)(?=\n\s*\n|\Z)", text, flags=re.S):
        start, end = match.start(), match.end()
        while start < end and text[start] in "\n \t":
            start += 1
        while end > start and text[end - 1] in "\n \t":
            end -= 1
        if end - start >= minimum:
            spans.append((start, end))
    return spans


def _paragraph_at(spans: list[tuple[int, int]], offset: int) -> int | None:
    for index, (start, end) in enumerate(spans):
        if start <= offset < end:
            return index
    return None


def _paragraphs_touched(spans: list[tuple[int, int]], start: int, end: int) -> list[int]:
    return [index for index, (left, right) in enumerate(spans)
            if end > left and start < right]


def _best_paragraph(spans: list[tuple[int, int]], start: int, end: int) -> int | None:
    """一条引用归给哪一段：与它重叠最多的那一段。

    为什么不是"起点所在的那一段"：切块是按句首吸附的，而标题行、表格行常常被
    段落切分丢掉（它们不足 80 字，不是"内容段"），于是大量命中的起点落在段与段
    之间的空隙里——按起点归属会把真实命中记成 0。按最大重叠归属对三组一视同仁，
    它只回答"这条证据主要来自哪一段"。
    """
    best, best_overlap = None, 0
    for index, (left, right) in enumerate(spans):
        overlap = min(end, right) - max(start, left)
        if overlap > best_overlap:
            best, best_overlap = index, overlap
    return best


def _ref_span(text: str, ref) -> tuple[int, int] | None:
    """一条引用的字符区间：有偏移用偏移，只有引文就在原文里定位它。

    两种形态都要支持才可比：`full` 形态下模型给的是"抄一段原文"（没有偏移），
    `rag` 形态下抄的是 `evidence_*`（带偏移）。只认偏移会让 A 组覆盖率直接归零
    ——那个 0 说明的是度量口径，不是覆盖。
    """
    if ref.start is not None and ref.end is not None:
        return ref.start, ref.end
    quote = (ref.quote or "").strip()
    if not quote:
        return None
    offset = text.find(quote)
    return (offset, offset + len(quote)) if offset >= 0 else None


def _clean(label: str, limit: int = 18) -> str:
    """节点标题：去掉 markdown 记号，压掉空白，截断。"""
    text = re.sub(r"^[#>\-\*\s]+", "", label).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit] or "（空）"


def _sources_from_prompt(user: str) -> list[str]:
    """`full` 形态的 prompt 里那一段 sources 数组 → 每份的正文。

    解不出来就退回"整条消息当一份正文"：这条路径只在数据形状意外时需要，
    它的保守后果是覆盖率偏高（子串匹配更容易命中），不会把失败伪装成成功。
    """
    marker = "sources 数组（JSON）：\n"
    if marker not in user:
        return [user]
    try:
        items = json.loads(user.split(marker, 1)[1])
    except (ValueError, IndexError):
        return [user]
    if not isinstance(items, list):
        return [user]
    return [str(item.get("text") or "") for item in items if isinstance(item, dict)]


# ---------------------------------------------------------------- 脚本化的"模型"


@dataclass
class ScriptedModel:
    """只看得到 prompt 里真正出现的文字，并按固定规则把它变成节点。

    这不是在模拟"聪明的模型"，而是在模拟**一个忠实但只会抄的模型**：看得到就写，
    看不到就写不出来。生成质量因此被固定住，三组之间的差别只可能来自"材料有没有
    送到它眼前"。

    - 整树生成（A/B）：A 从 prompt 里的 sources JSON 认出可见段落；B 从上一轮
      `rag_query` 的命中里认出可见段落（真实模型也是这么用上下文的）。
      每段一个节点，节点上抄一段逐字引文。
    - 分片生成（C）：骨架按 `titles` 返回；每支按**这一支自己的 evidence 数组**
      写节点，一条命中一个节点，引文抄 `evidence_*`。
    """

    spans: list[tuple[int, int]]
    text: str
    titles: tuple[str, ...]
    source_id: str
    hits: list = field(default_factory=list)
    refs_written: int = 0
    calls: int = 0

    # -------- 三种请求的分派
    async def __call__(self, system: str, user: str, *, max_tokens=None, on_delta=None):
        self.calls += 1
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(self.titles)}
        if "本条一级分支：" in user:
            return self._expand(user)
        if "从一级分支到这里的路径：" in user:
            return {"children": []}      # 第 3 层不会走到：子项都声明 expandable=false
        return self._whole_tree(user)

    # -------- 整树
    def _whole_tree(self, user: str) -> dict:
        refs = [self._ref_from_text(index) for index in self._visible_paragraphs(user)]
        refs = [ref for ref in refs if ref]
        # 平铺成 4 条一级分支：`mindmap_generate` 要求一级分支不少于 3 条。
        buckets = 4
        children = []
        for bucket in range(buckets):
            chunk = refs[bucket::buckets]
            children.append({
                "id": f"b{bucket}",
                "label": f"要点组 {bucket + 1}",
                "children": [
                    {"id": f"b{bucket}-{position}", "label": _clean(ref["quote"]),
                     "children": [], "source_refs": [ref]}
                    for position, ref in enumerate(chunk)
                ],
            })
        self.refs_written += len(refs)
        return {"title": "材料整理", "root": {"id": "root", "label": "材料整理",
                                             "children": children}}

    def _visible_paragraphs(self, user: str) -> list[int]:
        """这一轮生成看得见哪几段。

        `full` 形态：来源正文整份在 prompt 里的 sources 数组里——把那个数组解出来
        逐段比对（不是在整条消息上做子串匹配：JSON 里换行是 `\\n`，逐字比会漏掉
        所有带换行的段落，那是度量口径的漏，不是覆盖的漏）。
        `rag` 形态：只有上一轮检索的命中，按命中区间的起点归属段落。
        """
        if os.getenv("MINDMAP_SOURCE_MODE", "rag") == "full":
            visible: list[int] = []
            texts = _sources_from_prompt(user)
            for index, (start, _) in enumerate(self.spans):
                probe = self.text[start:start + 40]
                if probe and any(probe in body for body in texts):
                    visible.append(index)
            return visible
        seen: list[int] = []
        for hit in self.hits:
            index = _paragraph_at(self.spans, hit.start)
            if index is not None:
                seen.append(index)
        return seen

    def _ref_from_text(self, index: int) -> dict | None:
        start, end = self.spans[index]
        quote = self.text[start:end][:200]
        return {"source_id": self.source_id, "quote": quote} if quote else None

    # -------- 分片
    def _expand(self, user: str) -> dict:
        if "evidence 数组）：\n" not in user:
            return {"children": []}      # 无证据：按提示词要求如实留空
        entries = json.loads(user.split("evidence 数组）：\n", 1)[1])
        children = []
        for position, entry in enumerate(entries[:POINTS_PER_BRANCH]):
            ref = self._ref_from_hit(entry)
            if ref is None:
                continue
            children.append({"label": _clean(str(entry.get("quote") or "命中")),
                             "expandable": False, "source_refs": [ref]})
            self.refs_written += 1
        return {"children": children}

    @staticmethod
    def _ref_from_hit(entry: dict) -> dict | None:
        source_id = entry.get("source_id")
        if not isinstance(source_id, str):
            return None
        quote = entry.get("evidence_quote")
        if isinstance(quote, str) and quote:
            return {"source_id": source_id,
                    "start": entry.get("evidence_start"),
                    "end": entry.get("evidence_end"),
                    "quote": quote}
        text = entry.get("quote")
        if isinstance(text, str) and text:
            return {"source_id": source_id, "quote": text[:300]}
        return None


# ---------------------------------------------------------------- 检索与向量的观测


@dataclass
class Probes:
    searches: list[tuple[str, int]] = field(default_factory=list)
    embed_calls: list[int] = field(default_factory=list)
    embed_batches: list[int] = field(default_factory=list)
    search_hits: list[tuple[str, list]] = field(default_factory=list)


def make_probes() -> Probes:
    return Probes()


class ScriptedProvider:
    """按脚本返回 Completion；脚本用完就断言，避免循环偷偷多跑一轮。"""

    def __init__(self, script: list[Completion], *, probes: Probes) -> None:
        self._script = list(script)
        self.probes = probes

    async def complete_tools(self, messages, tools, *, budget):
        if not self._script:
            raise AssertionError("脚本已用完：循环比预期多跑了一轮")
        return self._script.pop(0)


def _completion(calls: list[ToolCall]) -> Completion:
    message: dict = {"role": "assistant", "content": ""}
    if calls:
        message["tool_calls"] = [
            {"id": call.call_id, "type": "function",
             "function": {"name": call.name,
                          "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
            for call in calls
        ]
    return Completion(message=message, tool_calls=calls,
                      finish_reason="tool_calls" if calls else "stop",
                      usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})


# ---------------------------------------------------------------- 单组运行


@dataclass
class GroupResult:
    name: str
    tool: str
    mode: str
    ok: bool
    coverage: float
    coverage_any: float
    refs_written: int
    refs_kept: int
    refs_verbatim: int
    tool_calls: int
    model_calls: int
    searches: int
    embed_calls: int
    embed_texts: int
    embed_batches: int
    elapsed: float
    nodes: int
    branches: int
    notes: list[str] = field(default_factory=list)
    output: str = ""


def run_group(
    name: str, tool: str, mode: str, *, text: str, spans: list[tuple[int, int]],
    titles: tuple[str, ...], topic: str, topic_query: str, source_id: str,
    fake_embedding: bool, embed_latency: float, query_mode: str = "topic-title",
) -> tuple[GroupResult, Probes]:
    os.environ["MINDMAP_SOURCE_MODE"] = mode
    source = Source(id=source_id, kind=SourceKind.document, status="parsed",
                    filename="RAG-DECISIONS.md", text=text)
    request = AgentRunRequest(
        goal=topic, intent="create", sources=[source],
        budget=RunBudget(max_model_calls=80, max_tool_calls=12,
                         max_output_tokens=65536),
    )
    probes = make_probes()
    model = ScriptedModel(spans=spans, text=text, titles=titles, source_id=source_id)

    calls: list[ToolCall] = []
    if tool == "mindmap_generate":
        if mode == "rag":
            calls.append(ToolCall(call_id="c-rag", name="rag_query",
                                  arguments={"question": topic_query}))
        calls.append(ToolCall(call_id="c-gen", name="mindmap_generate",
                              arguments={"topic": topic}))
    else:
        calls.append(ToolCall(call_id="c-deep", name="mindmap_generate_deep",
                              arguments={"topic": topic, "branches": MAX_BRANCHES,
                                         "points_per_branch": POINTS_PER_BRANCH,
                                         "max_depth": 2}))
    script = [_completion([call]) for call in calls] + [_completion([])]

    from app.knowledge.service import search_parent as real_search_parent

    def measuring_search(sources, query, top_k=5, *, filters=None, library=False):
        # `library` 是 `_rag` 后加的一个 kwarg：本脚本只量"请求内材料"的覆盖，
        # 保持默认关（不碰持久化索引），但签名必须收下它，否则 `_rag` 调用即 TypeError。
        hits = real_search_parent(sources, query, top_k, filters=filters,
                                  library=library)
        probes.searches.append((query, top_k))
        probes.search_hits.append((query, hits))
        # B 组：整树生成要"记得"上一轮检索看到了什么
        model.hits = list(hits)
        return hits

    def fake_embed(texts):
        probes.embed_calls.append(len(texts))
        probes.embed_batches.append(math.ceil(len(texts) / 25) or 1)
        if embed_latency:
            time.sleep(embed_latency * max(1, math.ceil(len(texts) / 25)))
        # 确定性伪向量：真实语义不需要，只需要长度一致、且不为零向量
        return [[math.sin((len(t) % 97) + i) for i in range(64)] for t in texts]

    patches = [
        patch("app.agent.tools.chat_async", new=model),
        patch("app.agent.tools.search_parent", new=measuring_search),
    ]
    # 记下工具回给模型的 content：报告里要贴"三种形态的真实输出"，而事件流里的
    # `result_summary` 只有 `data`（`content` 是给模型看的，不进事件）。
    from app.agent.tools import ToolRegistry

    original_execute = ToolRegistry.execute
    outputs: list[str] = []

    async def recording_execute(self, call, ctx):
        result = await original_execute(self, call, ctx)
        outputs.append(f"{call.name}: {result.content}")
        return result

    patches.append(patch.object(ToolRegistry, "execute", new=recording_execute))
    if query_mode != "title":
        # 消融实验：只换"分支 query 怎么拼"这一处。`title` 是随代码发布的那一版，
        # 另外两个是量给它看的两端——`topic-title` 是上一版（已证伪），
        # `topic` 是所有支发同一句（等于"整树检索、问了 N 次"的对照组）。
        patches.append(patch(
            "app.agent.tools._branch_query",
            new=(lambda title: f"{topic} {title}") if query_mode == "topic-title"
            else (lambda title: topic)))
    if fake_embedding:
        patches += [
            patch("app.knowledge.service.embedding_configured", new=lambda: True),
            patch("app.knowledge.service.embed_texts", new=fake_embed),
        ]

    started = time.perf_counter()
    events = []
    for context in patches:
        context.start()
    try:
        async def drain():
            return [event async for event in
                    run(request, provider=ScriptedProvider(script, probes=probes))]

        events = asyncio.run(drain())
    finally:
        for context in reversed(patches):
            context.stop()
    elapsed = time.perf_counter() - started

    done = [event for event in events if event.type == "done"]
    content = done[-1].content if done else None
    ok = content is not None and events[-1].type == "done"

    refs: list = []
    if content is not None:
        stack = [content.root]
        while stack:
            node = stack.pop()
            refs.extend(node.source_refs)
            stack.extend(node.children)

    starts_hit: set[int] = set()
    any_hit: set[int] = set()
    uncovered = 0
    for ref in refs:
        span = _ref_span(text, ref)
        if span is None:
            uncovered += 1
            continue
        index = _best_paragraph(spans, span[0], span[1])
        if index is None:
            uncovered += 1          # 落在标题/表格这类"非内容段"里
        else:
            starts_hit.add(index)
        any_hit.update(_paragraphs_touched(spans, span[0], span[1]))

    verbatim = 0
    for ref in refs:
        source_obj = source if ref.source_id == source.id else None
        if _reference_problem(ref, source_obj) is None:
            verbatim += 1

    nodes = 0
    if content is not None:
        stack = [content.root]
        while stack:
            node = stack.pop()
            nodes += 1
            stack.extend(node.children)
    branches = len(content.root.children) if content is not None else 0

    notes: list[str] = []
    retrieval = None
    for event in events:
        if event.type == "tool_end" and event.name == "mindmap_generate_deep":
            retrieval = event.result_summary.get("retrieval")
    if retrieval:
        notes.append(
            f"mode={retrieval['mode']} 有证据分支={retrieval['branches_with_hits']}"
            f"/{branches} 失败={len(retrieval['failed_branches'])}"
            f" 空={len(retrieval['empty_branches'])}")
    for event in events:
        if event.type == "tool_end":
            stats = event.result_summary.get("source_refs")
            if stats and stats.get("dropped"):
                notes.append(f"逐字校验剔除：{stats['dropped_by_code']}")
    if not fake_embedding:
        notes.append("未配置向量：纯 BM25")

    return GroupResult(
        name=name, tool=tool, mode=mode, ok=ok,
        coverage=len(starts_hit) / len(spans) if spans else 0.0,
        coverage_any=len(any_hit) / len(spans) if spans else 0.0,
        refs_written=model.refs_written, refs_kept=len(refs), refs_verbatim=verbatim,
        tool_calls=sum(1 for event in events if event.type == "tool_end"),
        model_calls=model.calls,
        searches=len(probes.searches),
        embed_calls=len(probes.embed_calls),
        embed_texts=sum(probes.embed_calls),
        embed_batches=sum(probes.embed_batches),
        elapsed=elapsed, nodes=nodes, branches=branches, notes=notes,
        output="\n".join(outputs),
    ), probes


# ---------------------------------------------------------------- 骨架标题


def heading_titles(text: str) -> tuple[str, ...]:
    """材料自带的小节标题（`##` / `###`），截到 20 字，最多 `MAX_BRANCHES` 条。

    这是对 C 组**有利**的假设：真实模型在 `rag` 形态下看不到正文，只能凭主题猜
    分块。用材料自己的标题等于给它一份"正确答案"的骨架——所以 C 的覆盖率是
    上界，而 `--skeleton generic` 给出偏坏的那一端。
    """
    titles: list[str] = []
    for match in re.finditer(r"^#{2,3}\s+(.+)$", text, flags=re.M):
        title = _clean(match.group(1), 20)
        if title and title not in titles:
            titles.append(title)
    return tuple(titles[:MAX_BRANCHES]) or GENERIC_TITLES[:MAX_BRANCHES]


# ---------------------------------------------------------------- 报告


def report(results: list[GroupResult], *, spans: int, chars: int, titles) -> None:
    header = (f"材料 {chars:,} 字，切 {spans} 段；骨架 "
              f"{len(titles)} 支：{'、'.join(titles)}")
    print(header)
    print()
    columns = ("组", "工具", "形态", "覆盖率", "上界", "引用 写出/留存/逐字",
               "工具调用", "模型调用", "检索", "embed 次数(批)/文本", "节点", "耗时")
    rows = []
    for item in results:
        rows.append((
            item.name, item.tool, item.mode,
            f"{item.coverage:.1%}", f"{item.coverage_any:.1%}",
            f"{item.refs_written}/{item.refs_kept}/{item.refs_verbatim}",
            item.tool_calls, item.model_calls, item.searches,
            f"{item.embed_calls}({item.embed_batches})/{item.embed_texts}",
            item.nodes, f"{item.elapsed * 1000:.0f}ms",
        ))
    widths = [max(len(str(row[index])) for row in [columns, *rows])
              for index in range(len(columns))]
    for row in [columns, *rows]:
        print("  ".join(str(cell).ljust(widths[index])
                        for index, cell in enumerate(row)))
    print()
    for item in results:
        if item.notes:
            print(f"{item.name}: {'；'.join(item.notes)}")


def branch_overlap(probes: Probes, spans, limit: int = 3) -> None:
    """C 组自查：每支 top-k 命中落在哪些段落，支与支之间重不重合。

    "召回泛"的可测形态就是这里：如果多支的 top-1 是同一个父块、或者 top-k 的段落
    集合大量重合，说明 query 的区分度不够——那是下一轮要不要动骨架输出契约
    （让骨架顺带吐检索关键词）的证据。这里同时给出三个量，避免只挑一个好看的：

      - 每支 top-1 落在哪个父块（起点）：几支共用同一个起点就是几支"问了同一件事"；
      - 每支 top-k 覆盖的段落集合与两两 Jaccard 均值；
      - 并集覆盖了材料里多少不同段落。
    """
    print()
    print("分片检索的支间重合（top-%d）：" % limit)
    per_branch: list[tuple[str, set[int], int]] = []
    for query, hits in probes.search_hits:
        paragraphs = {
            index for index in (
                _best_paragraph(spans, hit.start, hit.end) for hit in hits[:limit])
            if index is not None}
        top = hits[0].start if hits else -1
        per_branch.append((query, paragraphs, top))
        print(f"  {query[:34]:36s} top1@{top:<6d} 段落 {sorted(paragraphs)}")
    if not per_branch:
        return

    tops = [item[2] for item in per_branch]
    shared_top = max((tops.count(value) for value in set(tops)), default=0)
    union = set().union(*(item[1] for item in per_branch))
    jaccards: list[float] = []
    for left in range(len(per_branch)):
        for right in range(left + 1, len(per_branch)):
            a, b = per_branch[left][1], per_branch[right][1]
            if a or b:
                jaccards.append(len(a & b) / len(a | b))
    mean_jaccard = sum(jaccards) / len(jaccards) if jaccards else 0.0
    print(f"  最多支行共用同一个 top-1 父块：{shared_top}/{len(per_branch)}；"
          f"两两 Jaccard 均值：{mean_jaccard:.2f}")
    print(f"  并集覆盖的不同段落：{len(union)}/{len(spans)}"
          f"（{len(union) / len(spans):.1%}）")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--material", default=str(REPO / "docs" / "RAG-DECISIONS.md"))
    parser.add_argument("--skeleton", choices=("headings", "generic"), default="headings")
    parser.add_argument("--query-mode", choices=("title", "topic-title", "topic"),
                        default="title",
                        help="分支检索的 query 怎么拼。默认 title 就是随代码发布的那一版；"
                             "topic-title 是上一版（实测更差），topic 是同一句发 N 次的对照组")
    parser.add_argument("--branches", type=int, default=MAX_BRANCHES,
                        help="一级分支条数（成本闸的上界是 20，用它可以量最坏情况）")
    parser.add_argument("--only", default="ABC",
                        help="只跑某几组（A/B/C 的任意组合）。量 C 的冷缓存成本时用 C，"
                             "否则同进程里 B 已经把那批块向量写进缓存，C 的数字会偏小")
    parser.add_argument("--show-output", action="store_true",
                        help="打印每组工具回给模型的那段 content（真实输出）")
    parser.add_argument("--fake-embedding", action="store_true",
                        help="注入确定性假向量（量批量与次数用）")
    parser.add_argument("--embed-latency", type=float, default=0.0,
                        help="每批向量请求注入的模拟延迟（秒）")
    args = parser.parse_args()

    path = Path(args.material)
    text = load_material(path)
    spans = segment(text)
    titles = (heading_titles(text) if args.skeleton == "headings"
              else GENERIC_TITLES[:MAX_BRANCHES])
    if len(titles) < args.branches:
        # 标题不够时用通用词补到 N 条：量"20 支的最坏成本"时需要的只是条数。
        padded = list(titles) + [f"补充方面{n}" for n in
                                 range(1, args.branches - len(titles) + 1)]
        titles = tuple(padded)
    titles = titles[:args.branches]
    topic = "RAG 与召回：一轮评估记录与取舍"
    # B 组那一次检索的问句：用主题本身。这是最自然、也最有利于 B 的选择——
    # 换成"这份材料讲了什么"这类泛问句，B 的覆盖只会更低（见 docs/RAG-DECISIONS.md 2.3）。
    topic_query = topic

    common = dict(text=text, spans=spans, titles=titles, topic=topic,
                  topic_query=topic_query, source_id="m1",
                  fake_embedding=args.fake_embedding,
                  embed_latency=args.embed_latency, query_mode=args.query_mode)
    groups = {
        "A": ("mindmap_generate", "full"),
        "B": ("mindmap_generate", "rag"),
        "C": ("mindmap_generate_deep", "rag"),
    }
    pairs = [
        run_group(name, *groups[name], **common)
        for name in ("A", "B", "C") if name in args.only.upper()
    ]
    results = [item for item, _ in pairs]
    report(results, spans=len(spans), chars=len(text), titles=titles)
    if args.show_output:
        print()
        for item in results:
            print(f"[{item.name}] {item.output}")
            print()
    deep = [probes for item, probes in pairs if item.tool == "mindmap_generate_deep"]
    if deep:
        branch_overlap(deep[-1], spans, limit=POINTS_PER_BRANCH)
    print(f"\nquery 拼法：{args.query_mode}；骨架来源：{args.skeleton}；"
          f"分支数：{len(titles)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
