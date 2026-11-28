"""量第三档骨架（目录不可用 → 用图谱定骨架）对"模型自行归纳"。

## 它回答的问题

骨架的第一档是**材料自带的章节**（`_skeleton_outline`）。抽不出标题时那条闸过不去，
而骨架那一次调用的输入里**只有主题**（正文要到第三步"结构"才进 prompt）——模型此时
没有任何结构信息，"自行归纳"给的是模板词。

这一档把图谱里的概念清单放进**骨架那一次**的 prompt，问的是同一个问题："这份材料讲
了哪几件事"。这个脚本要验的是：

  **B 组的一级分支是不是材料里真有的概念**（而不是换了一批模板词）。

所以主指标是「骨架忠实度」，其余三个覆盖率口径与 `measure_graph_two_pass.py` 完全
相同、用来防止"忠实度上去了但内容没覆盖"这种假胜利。

## 四个口径

1. **字数覆盖**：节点 `source_refs` 的 `(source_id, start, end)` 区间**并集** / 材料
   总字数。取并集是因为相邻引用天然重叠 100 字（`CHUNK_OVERLAP_CHARS`）。
2. **要点覆盖**：材料按句号 / 换行切句（去空白后 ≥ 12 字），句子去空白后取所有 8 字
   滑窗，任一窗口出现在节点文本（label + detail）里就算这一句被覆盖。
3. **实体覆盖**：这批材料的图里有多少实体的名字出现在节点文本里。
4. **骨架忠实度（主指标）**：一级分支的 label 有多少能在**本次材料的原文**里找到。
   判据是"去掉空白与标点之后逐字子串"——模板词（「政策目标与原则」）在材料里搜不到，
   真实概念（「交叉验证」）搜得到。**这一条是这次改动的全部意义所在**：如果 B 组只是
   把模板词换成另一批同样搜不到的模板词，这一档就不该存在。

## 用法

    python scripts/measure_graph_outline.py                # A/B 各 2 次
    python scripts/measure_graph_outline.py --repeat 3
    python scripts/measure_graph_outline.py --probe        # 只量那 15 份的实体数
    python scripts/measure_graph_outline.py --only b --save data/ab-graph-outline.json

`--save` 会把每组的树、四个口径、一级分支的逐条命中情况、以及 B 组用到的图谱块
一起写进 JSON——**自评那一节要用的原文全在里面**。
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
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# 这一批主题的家常说法。这批材料是"用户随手抓回来的搜索结果"：机器学习相关 6 份 +
# 财务报销 2 份 + 电商 1 份 + 网页技术 2 份 + 中世纪史 1 份 + 哲学 1 份 + 碳中和 1 份。
# 主题取占比最大的那簇；B 组会不会把无关概念也拉进来，正是自评要看的东西。
TOPIC = "机器学习在各领域的应用与研究"

SENTENCE_SPLIT = re.compile(r"[。！？\n]+")
GRAM = 8
MIN_SENTENCE_CHARS = 12
# 去空白与标点：只留下"能被打字机打出来的字符"。中文标点在 `\W` 里，但 `_` 与全角
# 空格不在同一边，所以正列举比 `[^\w]` 可靠。
_KEEP = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
# 复合分支名的切分点：模型把两个相连的概念写进一条分支时用的连接词。分段口径要用它，
# 逐字口径不用（那一个要求整条 label 逐字出现）。
_SPLIT_LABEL = re.compile(r"与其|以及|与|和|及|、|，|,|/|＋|\+")
# ngram 口径的窗口宽度。4 字：短到"电力负荷预测应用"里的"电力负荷"能命中，长到
# "应用 / 研究 / 分析"这类两字通用词不会单独凑出一个命中。
NGRAM = 4


def _fill_env_from_registry() -> None:
    """凭据：DSH 的 shell 是进程级作用域，未必继承用户级环境变量。

    与 `measure_graph_two_pass.py` / `measure_single_call.py` 同一个做法（读
    `HKCU\\Environment`），不在这里另发明一套读法。
    """
    try:
        import winreg
    except ImportError:  # 仅 Windows 有：Linux/CI 上没有 HKCU\Environment 这一层可读
        return

    names = (
        "MINDMAP_LLM_MODE", "MINDMAP_LLM_BASE_URL", "MINDMAP_LLM_API_KEY",
        "MINDMAP_LLM_MODEL", "MINDMAP_LLM_MODEL_DEEP",
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


def _squash(text: str) -> str:
    """去掉空白与标点，只留下字符本身。判"逐字子串"用。"""
    return _KEEP.sub("", text or "")


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


def _node_text(content) -> str:
    parts: list[str] = []
    for node in _walk(content.root):
        parts.append(node.label or "")
        parts.append(node.detail or "")
    return "\n".join(parts)


def faithfulness(content, sources: list) -> dict:
    """口径 ④：一级分支的 label 是否出现在**本次材料的原文**里（去空白标点后）。

    逐份判、不拼成一大串：拼接会让"上一份的结尾 + 下一份的开头"成为原文里不存在的
    子串，那种假命中恰好落在边界上，肉眼查不出来。

    **同时给两个辅助口径**，因为逐字判据会惩罚**好**的写法：

      - `segments`：label 按 `与 / 和 / 及 / 、` 切段，每段都能找到才算命中。模型常把
        两个相连的概念合成一条分支（「电力负荷预测与供需管理」），逐字判它"未命中"，
        可它恰恰是"从材料里长出来的"最强证据——模板词（「挑战与伦理治理」）切开之后
        也没有任何一段能在材料里找到。
      - `ngram`：label 的 4 字窗口有多少比例落在材料原文里。连续量，对"实体名 + 应用
        后缀"（「电力负荷预测应用」）这种写法也稳定——那个写法会让分段口径整段落空
        （切不出连接词），逐字更落空，但它的每一个 4 字窗口都在材料里。

    三个口径一起看才分得清"换了模板词"与"改写了真概念"。**主指标仍是逐字那一个**
    （它不宽容、不可糊弄）；另外两个只用来解释差值。
    """
    squashed = [(source.filename or source.id, _squash(source.text or ""))
                for source in sources]

    def _find(text: str) -> str | None:
        return next((name for name, body in squashed if text and text in body), None)

    def _ngram_ratio(label: str) -> float:
        pieces = [squashed_label for _, squashed_label in squashed]
        if len(label) < NGRAM:
            return 1.0 if label and any(label in body for body in pieces) else 0.0
        windows = [label[index:index + NGRAM] for index in range(len(label) - NGRAM + 1)]
        hits = sum(1 for window in windows
                   if any(window in body for body in pieces))
        return hits / len(windows)

    rows = []
    for branch in content.root.children:
        label = _squash(branch.label or "")
        pieces = [_squash(piece) for piece in _SPLIT_LABEL.split(branch.label or "")]
        pieces = [piece for piece in pieces if len(piece) >= 2]
        piece_hits = [{"piece": piece, "hit": _find(piece) is not None}
                      for piece in pieces]
        rows.append({
            "label": branch.label or "",
            "squashed": label,
            "hit": _find(label) is not None,
            "material": _find(label),
            "segments": piece_hits,
            "segments_all_hit": bool(piece_hits) and all(
                item["hit"] for item in piece_hits),
            "ngram": round(_ngram_ratio(label), 3),
        })
    total = len(rows)
    hit_count = sum(1 for row in rows if row["hit"])
    segment_hit = sum(1 for row in rows if row["segments_all_hit"])
    ngram = round(sum(row["ngram"] for row in rows) / total, 4) if total else 0.0
    return {
        "branches": total,
        "hit": hit_count,
        "ratio": round(hit_count / total, 4) if total else 0.0,
        "segment_hit": segment_hit,
        "segment_ratio": round(segment_hit / total, 4) if total else 0.0,
        "ngram_ratio": ngram,
        "rows": rows,
    }


def coverage(content, sources: list, entity_names: list[str]) -> dict:
    """口径 ①②③。分母都来自**本次材料**，不是全库。"""
    by_source = {source.id: source for source in sources}
    spans: dict[str, list[tuple[int, int]]] = {}
    for node in _walk(content.root):
        for ref in node.source_refs or []:
            source = by_source.get(ref.source_id)
            if source is None:
                continue
            text = source.text or ""
            start, end = ref.start, ref.end
            if start is None or end is None:
                quote = (ref.quote or "").strip()
                if not quote:
                    continue
                index = text.find(quote)
                if index < 0:
                    continue
                start, end = index, index + len(quote)
            if end > start:
                spans.setdefault(ref.source_id, []).append((int(start), int(end)))
    material_chars = sum(len(source.text or "") for source in sources)
    covered_chars = sum(_merge_spans(items) for items in spans.values())

    text_all = _node_text(content)
    grams = _grams(text_all)
    sentences: list[str] = []
    for source in sources:
        for raw in SENTENCE_SPLIT.split(source.text or ""):
            sentence = raw.strip()
            if len(re.sub(r"\s+", "", sentence)) >= MIN_SENTENCE_CHARS:
                sentences.append(sentence)
    hit = sum(1 for sentence in sentences if _grams(sentence) & grams)

    lowered = text_all.lower()
    entity_hit = sum(1 for name in entity_names if name and name.lower() in lowered)

    return {
        "refs_covered_chars": covered_chars,
        "material_chars": material_chars,
        "chars_coverage": round(covered_chars / material_chars, 4) if material_chars else 0.0,
        "sentences": len(sentences),
        "sentences_hit": hit,
        "point_coverage": round(hit / len(sentences), 4) if sentences else 0.0,
        "entities": len(entity_names),
        "entities_hit": entity_hit,
        "entity_coverage": round(entity_hit / len(entity_names), 4) if entity_names else 0.0,
    }


def tree_stats(content) -> dict:
    nodes = [node for node in _walk(content.root) if node is not content.root]
    details = [len((node.detail or "").strip()) for node in nodes]
    filled = [value for value in details if value > 0]
    return {
        "nodes": len(nodes) + 1,
        "branches": len(content.root.children),
        "detail_avg": round(sum(filled) / len(filled), 1) if filled else 0.0,
        "detail_empty": sum(1 for value in details if value == 0),
    }


def _materials() -> tuple[list, list[dict]]:
    """那 15 份材料：库里 kind 为 paper / web、有正文、且**抽不出可用章节目录**。

    判据必须与产品一致（`tools._outline_entries` + `OUTLINE_MIN_COVERAGE`），否则
    这一档在真请求里根本不触发，量的就是另一批材料。4 字的那份 `openalex: 机器学习`
    会被 `(text or "").strip()` 判空而跳过——它没有正文，不是材料。
    """
    from app.agent.tools import OUTLINE_MIN_COVERAGE
    from app.knowledge import library
    from app.knowledge.service import heading_outline, outline_coverage

    picked, skipped = [], []
    for row in library.list_sources(limit=200):
        if row["kind"] not in ("paper", "web"):
            continue
        source = library.load(row["id"])
        if source is None or not (source.text or "").strip():
            skipped.append({"filename": row["filename"], "why": "没有正文"})
            continue
        entries = heading_outline([source])
        share = outline_coverage([source], entries) if entries else 0.0
        if entries and share >= OUTLINE_MIN_COVERAGE:
            skipped.append({"filename": row["filename"],
                            "why": f"有可用目录（{len(entries)} 条 / 覆盖 {share:.0%}）"})
            continue
        picked.append(source)
    return picked, skipped


def probe_entities(sources: list) -> list[dict]:
    """每份材料在图上各自的**带边实体数**——阈值 `GRAPH_OUTLINE_MIN_ENTITIES` 的依据。

    口径与产品判据同一个函数（`graph_store.scoped_entity_count`）：孤立实体不算（它们
    撑不起分支），scope 之外的不算（图是跨材料共享的）。
    """
    from app.knowledge import graph_store

    rows = []
    for source in sources:
        rows.append({
            "filename": source.filename or source.id,
            "source_id": source.id,
            "chars": len(source.text or ""),
            "entities": graph_store.scoped_entity_count([source.id]),
        })
    return sorted(rows, key=lambda row: row["entities"])


async def _run_once(*, topic: str, branches: int, sources: list, graph: bool) -> dict:
    """跑一次生成。开关通过环境变量给——与真实请求走的是同一条路。"""
    from app.agent import tools as tools_module
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall

    if graph:
        os.environ[tools_module.GRAPH_OUTLINE_ENV] = "1"
    else:
        os.environ[tools_module.GRAPH_OUTLINE_ENV] = "0"

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

    # 拦一层 `chat_async`：要留的是**骨架那一次的 prompt**（自评那一节要贴原文），
    # 而它不进 `result.data`。
    seen: list[tuple[str, str]] = []
    original = tools_module.chat_async

    async def spy(system: str, user: str, **kwargs):
        seen.append((system, user))
        return await original(system, user, **kwargs)

    started = time.perf_counter()
    with patch.object(tools_module, "chat_async", spy):
        result = await registry.execute(call, ctx)
    elapsed = time.perf_counter() - started
    if not result.ok:
        return {"ok": False, "content": result.content, "seconds": round(elapsed, 2)}
    return {
        "ok": True,
        "seconds": round(elapsed, 2),
        "data": result.data,
        "content": ctx.candidate,
        "plan_prompt": seen[0][1] if seen else "",
        "plan_system": seen[0][0] if seen else "",
        "calls": len(seen),
    }


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=2, help="每组跑几次")
    parser.add_argument("--branches", type=int, default=6)
    parser.add_argument("--topic", default=TOPIC)
    parser.add_argument("--only", choices=("a", "b"), default="", help="只跑一组")
    parser.add_argument("--probe", action="store_true", help="只量材料与实体数，不跑模型")
    parser.add_argument("--save", default="", help="报告写到这个路径（JSON）")
    args = parser.parse_args(argv)

    _fill_env_from_registry()
    os.environ.setdefault("MINDMAP_DB_PATH", str(BACKEND / "data" / "mindmaps.db"))
    os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "0"     # 材料已经定死，不许再补
    os.environ["MINDMAP_RERANK"] = "0"                 # 少一次网络往返；只影响选题，不影响骨架
    os.environ.setdefault("MINDMAP_NEO4J_URI", "bolt://127.0.0.1:7687")
    os.environ.setdefault("MINDMAP_NEO4J_USER", "neo4j")

    from app.knowledge import graph_store, library  # noqa: F401  （配置探活）

    sources, skipped = _materials()
    print(f"材料 {len(sources)} 份 / {sum(len(s.text or '') for s in sources)} 字"
          f"（跳过 {len(skipped)} 份）", flush=True)
    for row in skipped:
        print(f"  跳过 {row['filename'][:48]}：{row['why']}", flush=True)
    if not graph_store.configured():
        print("没有配置 MINDMAP_NEO4J_PASSWORD，图这一档跑不起来。", flush=True)
        return 2

    rows = probe_entities(sources)
    total = sum(row["entities"] for row in rows)
    print("\n每份材料的带边实体数（阈值依据）：", flush=True)
    for row in rows:
        print(f"  {row['chars']:5d} 字  {row['entities']:3d} 实体  {row['filename'][:52]}",
              flush=True)
    numbers = sorted(row["entities"] for row in rows)
    print(f"  合计 {total} / 中位数 {numbers[len(numbers) // 2]} / "
          f"最小 {numbers[0]} / 最大 {numbers[-1]}", flush=True)

    if args.probe:
        if args.save:
            Path(args.save).write_text(json.dumps(
                {"materials": rows, "skipped": skipped}, ensure_ascii=False, indent=2),
                encoding="utf-8")
            print(f"\n已写入 {args.save}", flush=True)
        return 0

    scope = [source.id for source in sources]
    entities, links = graph_store.subgraph(entities=200, links=400, scope=scope)
    entity_names = [item.name for item in entities]
    print(f"\n图：本批材料实体 {len(entities)} / 关系 {len(links)}"
          f"（全图 {graph_store.stats()}）", flush=True)

    groups = {"a": "A 现状（模型自行归纳）", "b": "B 图谱定骨架"}
    wanted = [args.only] if args.only else ["a", "b"]
    report: dict = {
        "topic": args.topic,
        "materials": [source.filename for source in sources],
        "material_chars": sum(len(source.text or "") for source in sources),
        "entity_counts": rows,
        "graph_entities_in_scope": len(entities),
        "graph_links_in_scope": len(links),
        "runs": [],
    }

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
            faithful = faithfulness(content, sources)
            data = outcome["data"] or {}
            codes = [item["code"] for item in (data.get("diagnostics") or [])]
            row = {
                "group": key, "sample": index, "ok": True,
                "seconds": outcome["seconds"], "calls": outcome["calls"],
                **stats, **cover,
                "faithfulness": faithful["ratio"],
                "faithfulness_segment": faithful["segment_ratio"],
                "faithfulness_ngram": faithful["ngram_ratio"],
                "faithful_rows": faithful["rows"],
                # B 组独有的账：这一次到底走没走图谱那一档。A 组应该恒为 False。
                "graph_outline_used": "GRAPH_OUTLINE_USED" in codes,
                "diagnostics": codes[:20],
                "plan_prompt": outcome["plan_prompt"],
            }
            report["runs"].append(row)
            print(
                f"节点 {stats['nodes']}（{stats['branches']} 支）"
                f"｜字数覆盖 {cover['chars_coverage']:.1%}"
                f"｜要点覆盖 {cover['point_coverage']:.1%}"
                f"｜实体覆盖 {cover['entity_coverage']:.1%}"
                f"｜**骨架忠实度 {faithful['ratio']:.1%}**"
                f"（{faithful['hit']}/{faithful['branches']}）"
                f"｜分段 {faithful['segment_ratio']:.1%}"
                f"｜4gram {faithful['ngram_ratio']:.1%}"
                f"｜图谱档 {'是' if row['graph_outline_used'] else '否'}"
                f"｜{outcome['seconds']}s",
                flush=True,
            )
            for item in faithful["rows"]:
                mark = "逐字命中" if item["hit"] else (
                    "分段命中" if item["segments_all_hit"] else "未命中")
                where = f"  ← {item['material'][:36]}" if item["hit"] else ""
                print(f"    [{mark}] {item['label']}{where}", flush=True)
            if args.save:
                tree_path = Path(args.save).with_suffix(f".{key}{index}.json")
                tree_path.write_text(
                    json.dumps(content.model_dump(), ensure_ascii=False, indent=1),
                    encoding="utf-8")

    print("\n=== 汇总（每组取均值）===", flush=True)
    summary: dict = {}
    for key in wanted:
        rows_ok = [row for row in report["runs"]
                   if row["group"] == key and row.get("ok")]
        if not rows_ok:
            print(f"{groups[key]}: 没有成功的样本", flush=True)
            continue

        def avg(field: str) -> float:
            values = [row[field] for row in rows_ok
                      if isinstance(row.get(field), (int, float))]
            return round(sum(values) / len(values), 4) if values else 0.0

        summary[key] = [{field: row.get(field) for field in (
            "chars_coverage", "point_coverage", "entity_coverage", "faithfulness",
            "faithfulness_segment", "faithfulness_ngram",
            "nodes", "branches", "seconds")}
            for row in rows_ok]
        print(
            f"{groups[key]}：字数覆盖 {avg('chars_coverage'):.1%} / "
            f"要点覆盖 {avg('point_coverage'):.1%} / "
            f"实体覆盖 {avg('entity_coverage'):.1%} / "
            f"**骨架忠实度 {avg('faithfulness'):.1%}"
            f"（分段 {avg('faithfulness_segment'):.1%} / "
            f"4gram {avg('faithfulness_ngram'):.1%}）** / "
            f"节点 {avg('nodes')}（{avg('branches')} 支） / {avg('seconds')}s",
            flush=True,
        )
    report["summary"] = summary
    if args.save:
        path = Path(args.save)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
        print(f"\n报告已写入 {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
