"""量两段式（图谱 → 节点）对三段式（章节骨架 → 节点）：覆盖率 × 关系质量 × detail。

## 为什么要这个脚本

用户的原话："覆盖率 30 是不是太低了，引入知识图谱嵌入然后根据知识图谱生成节点，
不用定骨架了怎么样，改成两段式"。这句话里有两个断言、一个动作：

  - 断言一：现在的覆盖率低（30%）；
  - 断言二：图谱能替代章节骨架来定节点；
  - 动作：改成两段式，然后**测覆盖率和逻辑关系有没有显示出来**。

前两个断言只能靠同一批材料上跑两组数来验。所以这个脚本跑 A/B **各 N 次**（生成有
随机性，单次采样说明不了问题），量三个覆盖率口径 + 关系质量 + detail 代价。

## 三个覆盖率口径（缺一个数字就没意义）

1. **材料字数覆盖**：材料的字有多少被至少一个节点的 `source_refs` 覆盖。口径是
   `(source_id, start, end)` 区间的**并集**——把每条引用的长度加起来会重复计算相邻
   引用重叠的那 100 字（`CHUNK_OVERLAP_CHARS`）。
2. **要点覆盖**：材料按句号/换行切成句子，有多少**句**被某个节点的 label 或 detail
   提到。判据是**字符级 8-gram 命中**：句子去空白后取所有 8 字滑窗，任一窗口出现在
   节点文本里就算这一句被覆盖。用 n-gram 而不是"整句包含"，是因为没有模型会把一整句
   原文照抄进 label；反过来只用关键词表会把"提及"放宽到没有意义。
3. **实体覆盖**：这批材料的图里有多少实体落到了某个节点上（名字出现在节点文本里）。

三个口径一起看才有信息：字数覆盖低而要点覆盖高，说明节点用了材料的内容但没标出处；
反过来则是标了出处但没消化。

## 关系质量

条数、`kind` 分布、以及每条 `why` 的抽样。**不看 why 的边等于没测**——图谱生成最
容易的失败是"连了一堆说得通但没依据的线"，而那件事只有读 why 才看得出来。

## 用法

    python scripts/measure_graph_two_pass.py                 # A/B 各 2 次
    python scripts/measure_graph_two_pass.py --repeat 3
    python scripts/measure_graph_two_pass.py --only b        # 只跑两段式
    python scripts/measure_graph_two_pass.py --save out.json
"""
# requires-optional: winreg
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

DOCS = ["AGENT_PLAN.md", "PLAN.md", "ARCHITECTURE.md", "RAG-DECISIONS.md"]
TOPIC = "这个多智能体思维导图系统的架构与取舍"

# 与 `docs/RAG-DECISIONS.md` 第 8 节、`FEATURES_AND_OWNERS.md` F14 用的是同一批材料：
# A 组当年量出 30.5%（按支检索）/ 5.1%（主题级一次检索），两段式要跟它比。
SENTENCE_SPLIT = re.compile(r"[。！？\n]+")
GRAM = 8
MIN_SENTENCE_CHARS = 12


def _fill_env_from_registry() -> None:
    """凭据：DSH/CI 的 shell 是进程级作用域，未必继承用户级环境变量。

    与 `scripts/measure_single_call.py` 同一个做法（读 `HKCU\\Environment`），
    不在这里另发明一套读法。
    """
    try:
        import winreg
    except ImportError:  # 仅 Windows 有：Linux/CI 上没有 HKCU\Environment 这一层可读
        return

    names = (
        "MINDMAP_LLM_MODE", "MINDMAP_LLM_BASE_URL", "MINDMAP_LLM_API_KEY",
        "MINDMAP_LLM_MODEL", "MINDMAP_LLM_MODEL_DEEP",
        "MINDMAP_EMBEDDING_BASE_URL", "MINDMAP_EMBEDDING_API_KEY",
        "MINDMAP_EMBEDDING_PROTOCOL", "MINDMAP_EMBEDDING_MODEL",
        "MINDMAP_EMBEDDING_DIM", "MINDMAP_RERANK", "MINDMAP_RERANK_MODEL",
        "MINDMAP_EXTERNAL_KNOWLEDGE", "MINDMAP_GRAPH_EXTRACTION",
        "MINDMAP_NEO4J_URI", "MINDMAP_NEO4J_USER", "MINDMAP_NEO4J_PASSWORD",
    )
    for name in names:
        if os.environ.get(name):
            continue
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, name)
        except OSError:
            continue
        if value:
            os.environ[name] = str(value)


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _node_text(content) -> str:
    """整棵树的文本：label + detail。覆盖率口径 B/C 都在这上面判。"""
    parts: list[str] = []
    for node in _walk(content.root):
        parts.append(node.label or "")
        parts.append(node.detail or "")
    return "\n".join(parts)


def _grams(text: str, size: int = GRAM) -> set[str]:
    chars = re.sub(r"\s+", "", text)
    if len(chars) < size:
        return {chars} if chars else set()
    return {chars[index:index + size] for index in range(len(chars) - size + 1)}


def _merge_spans(spans: list[tuple[int, int]]) -> int:
    """区间并集的长度。重叠不重复计数（相邻引用天然重叠 `CHUNK_OVERLAP_CHARS`）。"""
    if not spans:
        return 0
    ordered = sorted(spans)
    total = 0
    start, end = ordered[0]
    for next_start, next_end in ordered[1:]:
        if next_start <= end:
            end = max(end, next_end)
            continue
        total += end - start
        start, end = next_start, next_end
    return total + (end - start)


def coverage(content, sources: list, entity_names: list[str]) -> dict:
    """三个口径一次算完。分母都来自**本次材料**，不是全库。"""
    by_source = {source.id: source for source in sources}
    # ---- 口径 A：引用区间并集 / 材料总字数
    spans: dict[str, list[tuple[int, int]]] = {}
    for node in _walk(content.root):
        for ref in node.source_refs or []:
            source = by_source.get(ref.source_id)
            if source is None:
                continue
            text = source.text or ""
            start, end = ref.start, ref.end
            if start is None or end is None:
                # 没有偏移的引用只能按 quote 在原文里找位置：`verify_source_refs`
                # 留着这类引用时它已经逐字校验过，所以找得到。
                quote = (ref.quote or "").strip()
                if not quote:
                    continue
                index = text.find(quote)
                if index < 0:
                    continue
                start, end = index, index + len(quote)
            if end > start:
                spans.setdefault(ref.source_id, []).append((int(start), int(end)))
    material_chars = sum(len((source.text or "")) for source in sources)
    covered_chars = sum(_merge_spans(items) for items in spans.values())

    # ---- 口径 B：句子覆盖
    text_all = _node_text(content)
    grams = _grams(text_all)
    sentences: list[str] = []
    for source in sources:
        for raw in SENTENCE_SPLIT.split(source.text or ""):
            sentence = raw.strip()
            if len(re.sub(r"\s+", "", sentence)) >= MIN_SENTENCE_CHARS:
                sentences.append(sentence)
    hit = sum(
        1 for sentence in sentences
        if _grams(sentence) & grams
    )

    # ---- 口径 C：实体覆盖
    lowered = text_all.lower()
    entity_hit = sum(1 for name in entity_names if name and name.lower() in lowered)

    return {
        # A
        "refs_covered_chars": covered_chars,
        "material_chars": material_chars,
        "chars_coverage": round(covered_chars / material_chars, 4) if material_chars else 0.0,
        "refs_total": sum(len(items) for items in spans.values()),
        # B
        "sentences": len(sentences),
        "sentences_hit": hit,
        "point_coverage": round(hit / len(sentences), 4) if sentences else 0.0,
        # C
        "entities": len(entity_names),
        "entities_hit": entity_hit,
        "entity_coverage": round(entity_hit / len(entity_names), 4) if entity_names else 0.0,
    }


def tree_stats(content) -> dict:
    nodes = [node for node in _walk(content.root) if node is not content.root]
    details = [len((node.detail or "").strip()) for node in nodes]
    filled = [value for value in details if value > 0]
    branches = content.root.children
    return {
        "nodes": len(nodes) + 1,
        "branches": len(branches),
        "depth": _depth(content.root),
        "detail_avg": round(sum(filled) / len(filled), 1) if filled else 0.0,
        "detail_min": min(filled) if filled else 0,
        "detail_max": max(filled) if filled else 0,
        "detail_empty": sum(1 for value in details if value == 0),
        "detail_under_80": sum(1 for value in details if value < 80),
    }


def _depth(node, level: int = 1) -> int:
    if not node.children:
        return level
    return max(_depth(child, level + 1) for child in node.children)


async def _run_once(*, topic: str, branches: int, sources: list, graph: bool) -> dict:
    """跑一次生成。开关通过环境变量给——与真实请求走的是同一条路。"""
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
    from app.agent.tools import build_registry

    if graph:
        os.environ["MINDMAP_GRAPH_SKELETON"] = "1"
    else:
        os.environ.pop("MINDMAP_GRAPH_SKELETON", None)

    request = AgentRunRequest(
        goal=topic, intent="create", thinking_model="default", sources=sources,
        budget=RunBudget(max_model_calls=40, max_tool_calls=8, max_seconds=900.0,
                         max_output_tokens=65536, max_repairs=1),
    )
    ctx = RunContext(request=request,
                     sources_by_id={source.id: source for source in sources})
    registry = build_registry("create", has_sources=True, has_library=False)
    call = ToolCall(call_id="measure", name="mindmap_generate",
                    arguments={"topic": topic, "branches": branches})
    started = time.perf_counter()
    result = await registry.execute(call, ctx)
    elapsed = time.perf_counter() - started
    if not result.ok:
        return {"ok": False, "content": result.content,
                "seconds": round(elapsed, 2), "graph": graph}
    return {
        "ok": True,
        "seconds": round(elapsed, 2),
        "graph": graph,
        "data": result.data,
        "content": ctx.candidate,
    }


def _relation_report(content) -> dict:
    relations = list(content.relations or [])
    kinds: dict[str, int] = {}
    for relation in relations:
        key = relation.kind.value if relation.kind is not None else "?"
        kinds[key] = kinds.get(key, 0) + 1
    return {
        "count": len(relations),
        "kinds": kinds,
        "with_why": sum(1 for relation in relations if (relation.why or "").strip()),
        "samples": [
            {
                "source": relation.source,
                "target": relation.target,
                "kind": relation.kind.value if relation.kind is not None else None,
                "why": relation.why,
            }
            for relation in relations[:8]
        ],
    }


def _label_of(content, node_id: str) -> str:
    for node in _walk(content.root):
        if node.id == node_id:
            return node.label
    return node_id


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=2, help="每组跑几次")
    parser.add_argument("--branches", type=int, default=6)
    parser.add_argument("--topic", default=TOPIC)
    parser.add_argument("--only", choices=("a", "b"), default="", help="只跑一组")
    parser.add_argument("--save", default="", help="报告写到这个路径（JSON）")
    args = parser.parse_args(argv)

    _fill_env_from_registry()
    os.environ.setdefault("MINDMAP_DB_PATH", str(BACKEND / "data" / "mindmaps.db"))
    os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "0"
    os.environ.setdefault("MINDMAP_NEO4J_URI", "bolt://127.0.0.1:7687")
    os.environ.setdefault("MINDMAP_NEO4J_USER", "neo4j")

    from app.knowledge import graph_store, library
    from app.knowledge import graph_search

    # 材料：必须是**库里已有的**那四份（建图时入的库），否则边上的 source_id 与
    # 请求里的临时 id 对不上，`subgraph` 会回退全图，A/B 就比的不是同一张图。
    entries = {item["filename"]: item["id"] for item in library.list_sources(limit=200)}
    missing = [name for name in DOCS if name not in entries]
    if missing:
        print(f"库里缺少这些材料（先跑 scripts/ingest_graph_corpus.py）：{missing}")
        return 2
    sources = [library.load(entries[name]) for name in DOCS]
    sources = [source for source in sources if source is not None]
    if len(sources) < len(DOCS):
        print("材料加载不完整。")
        return 2

    scope = [source.id for source in sources]
    entities, links = graph_store.subgraph(
        entities=200, links=400, scope=scope)
    entity_names = [item.name for item in entities]
    print(f"图：本批材料的实体 {len(entities)} 个 / 关系 {len(links)} 条"
          f"（全图 {graph_store.stats()}）", flush=True)
    print(f"主题里点到的实体：{graph_search.entities_in_query(args.topic)}", flush=True)

    groups = {"a": "三段式（章节骨架）", "b": "两段式（图谱）"}
    wanted = [args.only] if args.only else ["a", "b"]
    report: dict = {"topic": args.topic, "materials": DOCS,
                    "material_chars": sum(len(source.text or "") for source in sources),
                    "graph_entities_in_scope": len(entities),
                    "graph_links_in_scope": len(links),
                    "runs": []}

    for key in wanted:
        for index in range(1, args.repeat + 1):
            print(f"\n=== {groups[key]} 第 {index} 次 ===", flush=True)
            outcome = await _run_once(
                topic=args.topic, branches=args.branches,
                sources=sources, graph=(key == "b"))
            if not outcome["ok"]:
                print(f"失败：{outcome['content']}", flush=True)
                report["runs"].append({"group": key, "sample": index, **outcome})
                continue
            content = outcome["content"]
            stats = tree_stats(content)
            cover = coverage(content, sources, entity_names)
            relations = _relation_report(content)
            data = outcome["data"] or {}
            row = {
                "group": key, "sample": index, "ok": True,
                "seconds": outcome["seconds"],
                # 两段式独有的账（`data["graph"]`），三段式没有这个键。
                "graph_block": data.get("graph"),
                "calls": data.get("calls"),
                "retrieval": data.get("retrieval"),
                **stats, **cover,
                "relations": relations,
                "relation_labels": [
                    {
                        "source": _label_of(content, item["source"]),
                        "target": _label_of(content, item["target"]),
                        "kind": item["kind"], "why": item["why"],
                    }
                    for item in relations["samples"]
                ],
                "diagnostics": [
                    item["code"] for item in (data.get("diagnostics") or [])
                ][:20],
            }
            report["runs"].append(row)
            print(
                f"节点 {stats['nodes']}（{stats['branches']} 支 / {stats['depth']} 层）"
                f"｜字数覆盖 {cover['chars_coverage']:.1%}"
                f"｜要点覆盖 {cover['point_coverage']:.1%}"
                f"｜实体覆盖 {cover['entity_coverage']:.1%}"
                f"｜关系 {relations['count']} 条（{relations['kinds']}）"
                f"｜detail 平均 {stats['detail_avg']} 空 {stats['detail_empty']}"
                f"｜{outcome['seconds']}s",
                flush=True,
            )
            if args.save:
                tree_path = Path(args.save).with_suffix(f".{key}{index}.json")
                tree_path.write_text(
                    json.dumps(content.model_dump(), ensure_ascii=False, indent=1),
                    encoding="utf-8")

    # ---- 汇总表
    print("\n=== 汇总（每组取均值）===")
    for key in wanted:
        rows = [row for row in report["runs"] if row["group"] == key and row.get("ok")]
        if not rows:
            print(f"{groups[key]}: 没有成功的样本")
            continue

        def avg(field: str) -> float:
            values = [
                row[field] for row in rows if isinstance(row.get(field), (int, float))]
            return round(sum(values) / len(values), 4) if values else 0.0

        relations_avg = round(
            sum(row["relations"]["count"] for row in rows) / len(rows), 1)
        print(
            f"{groups[key]}："
            f"字数覆盖 {avg('chars_coverage'):.1%} / "
            f"要点覆盖 {avg('point_coverage'):.1%} / "
            f"实体覆盖 {avg('entity_coverage'):.1%} / "
            f"节点 {avg('nodes')}（{avg('branches')} 支） / "
            f"detail 平均 {avg('detail_avg')} / "
            f"关系 {relations_avg} 条 / "
            f"{avg('seconds')}s"
        )
    report["summary"] = {
        key: [
            {field: row[field] for field in (
                "chars_coverage", "point_coverage", "entity_coverage",
                "nodes", "branches", "detail_avg", "detail_empty", "seconds")}
            for row in report["runs"] if row["group"] == key and row.get("ok")
        ]
        for key in wanted
    }
    if args.save:
        path = Path(args.save)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
        print(f"\n报告已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
