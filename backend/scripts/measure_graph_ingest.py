"""量"检索材料入图"这一步的真实成本，并验它把骨架改好了多少。

## 它回答的四个问题

`_intake` 里那一步（`tools._ingest_external_graph`）在生成前把外部检索回来的材料抽成
实体与边，好让第三档骨架（`tools._graph_outline`）④ 在"材料没有章节标题"时接管。
这个脚本要给出四个数，缺一个这改动都不该合：

  1. **成本**：3 条真实检索材料切成几块、串行/并行各多少秒、折多少 token。
     单块 2.5–3.4 秒、约 1.7k token 是 `scripts/ingest_graph_corpus.py` 那批
     （22 份 / 179 块 / 610 秒 / 306k token）的口径；这里要验短材料是不是同一个量级。
  2. **A/B**：入图关（`MINDMAP_GRAPH_OUTLINE=0`，= 现在的行为）vs 入图开。
     四个口径沿用 `measure_graph_outline.py`（字数/要点/实体覆盖 + **骨架忠实度**），
     **口径函数直接从那个脚本 import**——两处各写一遍迟早分叉。
  3. **★ 论文摘要那部分贡献了什么**：上一轮的证据是"维基 1 份 + 论文 2 份"里 9 条
     一级分支全部来自维基，两份论文摘要（2393 字、占 24%）一条都没贡献。这一项单独
     数：一级分支的 label 有多少能在**没有标题的那批材料**（摘要）里逐字找到，
     其中多少**只在摘要里出现过**（= 只能来自摘要，维基里没有）。
  4. **两组的原始一级分支**：自评要贴原文，指标只是判据。

## 用法

    python scripts/measure_graph_ingest.py --probe      # 只量检索形状，不花钱不写库
    python scripts/measure_graph_ingest.py              # 量成本 + A/B 各 1 次
    python scripts/measure_graph_ingest.py --repeat 2 --only b
    python scripts/measure_graph_ingest.py --skip-cost  # 成本已量过，直接入图 + 跑 B 组

`--save` 把那两组的一级分支、四个口径、每份材料的逐条命中、以及入图的逐份成本
写进 JSON。

## 成本这一步为什么要串行与并行各跑一次

产品里的实现是 `asyncio.gather` + 信号量（`GRAPH_INGEST_CONCURRENCY`）。并发省的是
**墙钟**，不省 token，而"省了多少"正是"这个默认值该不该开"的判据。两次跑之间要
**把那批 source_id 的边删掉**（`_clear_source_edges`）：否则第三把锁
（`scoped_entity_count > 0` 就跳过）会让第二次变成 0 秒——那不是测量，是复述。
只删边不删实体：判据数的是带边实体，孤立节点不影响任何一次判定。
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
for path in (BACKEND, BACKEND / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# 口径函数与凭据装载都从上一个 A/B 脚本借：覆盖率的三个口径、骨架忠实度的三个口径
# （逐字 / 分段 / 4gram）都已经被那一轮钉过，重写一遍就是第二套口径。
from measure_graph_outline import (  # noqa: E402
    _fill_env_from_registry, _squash, coverage, faithfulness, tree_stats,
)

# 主题：库里没有（`library.list_sources()` 那 22 份里没有它），中文维基有条目
# （2427 字 / 7 个 markdown 标题），而 openalex / crossref 命中是纯摘要（无标题）
# ——正是"标题只在维基那几份里"那个形状。**这是这个主题唯一的用途**：不再换别的，
# 换了就没有可比性。
TOPIC = "海绵城市"


def _clear_source_edges(source_ids: list[str]) -> int:
    """删掉这些 `source_id` 上的全部边，返回删了几条。

    **只给这个脚本用**：产品里"重入一遍图"靠的是幂等（重复不写），而这里要量的是
    "第一次入图"的成本，所以必须先把上一次的产物拿掉。只删边不删实体——判据
    （`scoped_entity_count`）数的是带边实体，删完边它的口径立刻回到 0。
    """
    from app.knowledge import graph_store

    total = 0
    driver = graph_store._driver()
    database = graph_store._config()[3]
    for source_id in source_ids:
        record = driver.execute_query(
            # 无向匹配（`-[r]-`）比 `-[r]->` 稳：抽取层写边时方向由材料决定，这里
            # 不关心方向，只按 `source_id` 清。括号不能省——`-[r:REL] WHERE` 是语法
            # 错误（`-[]-` 那个 `-()` 是模式的一部分）。
            f"MATCH ()-[r:{graph_store.EDGE_TYPE}]-()"
            " WHERE r.source_id = $source_id"
            " WITH r LIMIT 10000 DELETE r RETURN count(*) AS c",
            {"source_id": source_id}, database_=database,
        ).records
        total += int(record[0]["c"]) if record else 0
    return total


class Meter:
    """包一层 `graph_extract.chat_async`，量出真实的输入/输出字符数。

    做法与 `scripts/ingest_graph_corpus.py` 的 `Meter` 完全一样（那里解释了为什么
    可以这样量）：`extract_graph` 内部调模块级 `chat_async`，在脚本进程里换掉它就
    能计量，进程退出后改动消失。折算 1.5 字 / token 是 DeepSeek 官方口径，也是
    `docs/RAG-DECISIONS.md` 第 8 节用的那个数。
    """

    CHARS_PER_TOKEN = 1.5

    def __init__(self) -> None:
        self.calls = 0
        self.in_chars = 0
        self.out_chars = 0
        self.failed = 0

    def install(self) -> None:
        from app.knowledge import graph_extract

        original = graph_extract.chat_async

        async def counted(system: str, user: str, **kwargs):
            self.calls += 1
            self.in_chars += len(system) + len(user)
            try:
                result = await original(system, user, **kwargs)
            except Exception:
                self.failed += 1
                raise
            self.out_chars += len(result) if isinstance(result, str) else len(str(result))
            return result

        graph_extract.chat_async = counted  # type: ignore[assignment]

    def as_dict(self) -> dict:
        rate = self.CHARS_PER_TOKEN
        return {
            "calls": self.calls, "failed": self.failed,
            "in_chars": self.in_chars, "out_chars": self.out_chars,
            "in_tokens_est": round(self.in_chars / rate),
            "out_tokens_est": round(self.out_chars / rate),
            "total_tokens_est": round((self.in_chars + self.out_chars) / rate),
        }


def _fetch_materials(topic: str) -> tuple[list, list[dict]]:
    """外部检索 + 入库，返回（可检索的来源，逐条的形状）。

    与产品走同一个入口（`external.search_external` + `library.ingest`），因为"抓回来
    的材料长什么样"是这个脚本的全部前提：条数、字数、有没有 markdown 标题。
    **关缓存**（`MINDMAP_EXTERNAL_CACHE=0`）：命中缓存会让这一步变成上一批的快照，
    而这一次要验的正是"新搜回来的东西"。
    """
    from app.agent import tools as tools_module
    from app.knowledge import external as external_knowledge
    from app.knowledge import library

    hits = external_knowledge.search_external(topic, limit=tools_module.EXTERNAL_HIT_LIMIT)
    sources, rows = [], []
    for hit in hits:
        source = tools_module._external_source(hit)
        try:
            source_id = library.ingest(source)
        except Exception as exc:  # noqa: BLE001 — 单条入库失败不废整次
            print(f"  入库失败 {hit.source_name}：{type(exc).__name__}", flush=True)
            continue
        source = source.model_copy(update={"id": source_id})
        sources.append(source)
        rows.append({
            "filename": source.filename,
            "source_name": hit.source_name,
            "source_id": source_id,
            "chars": len(source.text or ""),
        })
    return sources, rows


def _split_by_title(sources: list) -> tuple[list, list]:
    """按"有没有章节标题"把材料分成两份（`_outline_entries` 用的是同一个函数）。

    这样分是因为 ★ 那个指标问的是"论文摘要那部分有没有贡献分支"：摘要类材料在骨架
    这一步**一条标题都提供不了**（`heading_outline` 给空），所以它们想进骨架只有
    图谱这一条路。
    """
    from app.knowledge.service import heading_outline

    titled, plain = [], []
    for source in sources:
        (titled if heading_outline([source]) else plain).append(source)
    return titled, plain


def summary_contribution(content, plain: list, titled: list) -> dict:
    """★ 指标：一级分支里有多少条只能来自**没有标题的那批材料**（摘要）。

    三个数，从宽到严：

      - `in_plain`：label 能在摘要原文里逐字找到（它可能同时也出现在维基里）；
      - `only_plain`：**只在摘要里**能找到——维基里找不到。这一条才是"摘要贡献了
        分支"的硬证据：维基那份是有标题的，它的概念本来就能通过第一章进来；
      - `rows`：逐条的判据（自评要贴原文）。
    """
    plains = [(_squash(source.text or ""), source.filename or source.id)
              for source in plain]
    titles = [(_squash(source.text or ""), source.filename or source.id)
              for source in titled]

    def _find(label: str, pool) -> str | None:
        return next((name for body, name in pool if label and label in body), None)

    rows = []
    for branch in content.root.children:
        label = _squash(branch.label or "")
        rows.append({
            "label": branch.label or "",
            "in_plain": _find(label, plains),
            "in_titled": _find(label, titles),
        })
    only = [row for row in rows if row["in_plain"] and not row["in_titled"]]
    return {
        "branches": len(rows),
        "in_plain": sum(1 for row in rows if row["in_plain"]),
        "only_plain": len(only),
        "rows": rows,
    }


async def _ingest_serial(sources: list) -> tuple[dict, list[dict]]:
    """逐份串行入图（成本上界）。"""
    from app.agent import tools as tools_module
    from app.knowledge import graph_ingest

    rows, started = [], time.perf_counter()
    for source in sources:
        each = time.perf_counter()
        stats = await graph_ingest.ingest_graph(
            source, max_chunks=tools_module.GRAPH_INGEST_MAX_CHUNKS,
            source_id=source.id)
        rows.append({
            "filename": source.filename,
            "chars": len(source.text or ""),
            "chunks": stats["chunks"], "entities": stats["entities"],
            "edges": stats["edges"], "failed": stats["failed"],
            "seconds": round(time.perf_counter() - each, 2),
        })
    return {"seconds": round(time.perf_counter() - started, 2)}, rows


async def _run_once(*, topic: str, branches: int, sources: list, graph: bool) -> dict:
    """跑一次生成。开关通过环境变量给——与真实请求走的是同一条路。"""
    from unittest.mock import patch

    from app.agent import tools as tools_module
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall

    os.environ[tools_module.GRAPH_OUTLINE_ENV] = "1" if graph else "0"
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
        "ok": True, "seconds": round(elapsed, 2), "data": result.data,
        "content": ctx.candidate, "plan_prompt": seen[0][1] if seen else "",
        "calls": len(seen),
    }


def _report_run(content, sources, plain, titled, entity_names, outcome) -> dict:
    cover = coverage(content, sources, entity_names)
    faithful = faithfulness(content, sources)
    contribution = summary_contribution(content, plain, titled)
    data = outcome["data"] or {}
    codes = [item["code"] for item in (data.get("diagnostics") or [])]
    return {
        "ok": True, "seconds": outcome["seconds"], "calls": outcome["calls"],
        **tree_stats(content), **cover,
        "faithfulness": faithful["ratio"],
        "faithfulness_segment": faithful["segment_ratio"],
        "faithfulness_ngram": faithful["ngram_ratio"],
        "faithful_rows": faithful["rows"],
        "graph_outline_used": "GRAPH_OUTLINE_USED" in codes,
        "graph_ingest_done": "GRAPH_INGEST_DONE" in codes,
        "diagnostics": codes[:20],
        "summary_contribution": contribution,
        "branches_raw": [child.label for child in content.root.children],
    }


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", default=TOPIC)
    parser.add_argument("--branches", type=int, default=6)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--only", choices=("a", "b"), default="")
    parser.add_argument("--probe", action="store_true", help="只量检索形状，不花钱")
    parser.add_argument(
        "--e2e", action="store_true",
        help="端到端：**不带材料**跑一次生成，看 `_intake` 里那段是不是真的接上了")
    parser.add_argument("--skip-cost", action="store_true", help="跳过入图（图里已有边）")
    parser.add_argument(
        "--with-serial", action="store_true",
        help="多跑一遍串行入图当上界（默认只量产品的并发实现；串行的数已量过 20.9s/7 块）")
    parser.add_argument(
        "--only-plain", action="store_true",
        help="A/B 只跑**没有标题的那批**（摘要）：这才是第三档真正接管的形状")
    parser.add_argument("--save", default="")
    args = parser.parse_args(argv)

    _fill_env_from_registry()
    os.environ.setdefault("MINDMAP_DB_PATH", str(BACKEND / "data" / "mindmaps.db"))
    os.environ["MINDMAP_EXTERNAL_CACHE"] = "0"      # 必须真搜，见 `_fetch_materials`
    os.environ["MINDMAP_EXTERNAL_PROXY"] = os.getenv(
        "MINDMAP_EXTERNAL_PROXY", "http://127.0.0.1:7897")
    os.environ["MINDMAP_RERANK"] = "0"              # 只影响选题，不影响骨架与入图
    os.environ.setdefault("MINDMAP_NEO4J_URI", "bolt://127.0.0.1:7687")
    os.environ.setdefault("MINDMAP_NEO4J_USER", "neo4j")
    os.environ["MINDMAP_GRAPH_EXTRACTION"] = "1"    # 抽取本身默认关，这个脚本要真抽

    from app.agent import tools as tools_module
    from app.knowledge import graph_store
    from app.knowledge.service import _chunk_spans, heading_outline, outline_coverage

    # 产品里这一步默认关（`GRAPH_INGEST_ENV` 不设就是关）。这里显式打开——而且必须
    # 在 import 之后按常量名设：写死字符串会让改常量的人漏掉这里。
    os.environ[tools_module.GRAPH_INGEST_ENV] = "1"

    if not graph_store.configured():
        print("没有配置 MINDMAP_NEO4J_PASSWORD，入图这条链跑不起来。", flush=True)
        return 2
    print(f"图存储在线：{graph_store.stats()}", flush=True)

    print(f"\n=== 外部检索：{args.topic!r} ===", flush=True)
    started = time.perf_counter()
    sources, rows = _fetch_materials(args.topic)
    fetch_seconds = round(time.perf_counter() - started, 2)
    total_chars = sum(row["chars"] for row in rows)
    chunks = sum(len(_chunk_spans(source.text or "")) for source in sources)
    print(f"{len(sources)} 条 / {total_chars} 字 / 待抽 {chunks} 块，"
          f"检索+入库 {fetch_seconds}s", flush=True)
    for source, row in zip(sources, rows):
        entries = heading_outline([source])
        share = outline_coverage([source], entries) if entries else 0.0
        print(f"  {row['chars']:5d} 字 / 标题 {len(entries):2d} / 覆盖 {share:4.2f} / "
              f"{len(_chunk_spans(source.text or '')):2d} 块  {row['filename'][:52]}",
              flush=True)

    plain = [source for source, row in zip(sources, rows)
             if not heading_outline([source])]
    titled = [source for source in sources if source not in plain]
    p_chars = sum(len(source.text or "") for source in plain)
    all_entries = tools_module._outline_entries(sources)
    share = outline_coverage(sources, all_entries) if all_entries else 0.0
    print(f"\n有标题 {len(titled)} 份 / {total_chars - p_chars} 字；"
          f"无标题（摘要）{len(plain)} 份 / {p_chars} 字 "
          f"（{p_chars / total_chars:.0%}）", flush=True)
    print(f"全批目录：{len(all_entries)} 条 / 覆盖率 {share:.3f}"
          f"（闸 {tools_module.OUTLINE_MIN_COVERAGE}）→ "
          f"{'章节骨架接管，第三档不触发' if all_entries else '目录不可用，第三档接管'}",
          flush=True)

    report: dict = {
        "topic": args.topic, "fetch_seconds": fetch_seconds,
        "materials": rows, "material_chars": total_chars, "chunks": chunks,
        "titled": [source.filename for source in titled],
        "plain": [source.filename for source in plain],
        "plain_chars": p_chars,
        "outline_entries": len(all_entries),
        "outline_coverage": round(share, 4),
        "runs": [],
    }
    if args.probe:
        print("\n--probe：只量到这里，没有花钱、没有写图。", flush=True)
        if args.save:
            Path(args.save).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    if args.e2e:
        # 这一段验的是**产品接线**，不是效果：材料为空 → `_intake` 真的去外部检索 →
        # 真的入图 → 骨架真的能读到这批材料的边。A/B 那两组是手工入的图，说明不了
        # `_intake` 里那两行接对了。
        os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "1"
        print(f"\n=== 端到端：无材料 → 外部检索 → 入图 → 骨架（{args.topic!r}）===",
              flush=True)
        outcome = await _run_once(topic=args.topic, branches=args.branches,
                                  sources=[], graph=True)
        if not outcome["ok"]:
            print(f"失败：{outcome['content']}", flush=True)
            return 4
        data = outcome["data"] or {}
        for item in (data.get("diagnostics") or []):
            print(f"  [{item['code']}] {item['message']}", flush=True)
        print(f"  一级分支：{[child.label for child in outcome['content'].root.children]}",
              flush=True)
        print(f"  {outcome['seconds']}s / {outcome['calls']} 次调用", flush=True)
        report["e2e"] = {
            "seconds": outcome["seconds"], "calls": outcome["calls"],
            "diagnostics": [item["code"] for item in (data.get("diagnostics") or [])],
            "branches": [child.label for child in outcome["content"].root.children],
        }
        if args.save:
            Path(args.save).write_text(
                json.dumps(report, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
        return 0

    groups = {"a": "A 现在（不图）", "b": "B 入图（图谱定骨架）"}
    wanted = [args.only] if args.only else ["a", "b"]

    # A/B 用哪一批材料。`--only-plain` 是这一档唯一的真实作用域：**目录不可用**时
    # 才轮到图谱定骨架。全量那组（维基 1 份撑过覆盖率闸）走的是章节骨架，入图对它
    # 一个字符都改不了——那一组的价值是证明"改了也是白改"，不是证明"改好了"。
    if args.only_plain:
        used, used_titled = list(plain), []
        print(f"\nA/B 只用无标题那批：{len(used)} 份 / "
              f"{sum(len(source.text or '') for source in used)} 字（目录不可用 → "
              "第三档接管）", flush=True)
    else:
        used, used_titled = list(sources), list(titled)

    meter = Meter()
    meter.install()
    if not args.skip_cost:
        if args.with_serial:
            print("\n=== 成本 1/2：串行入图（上界）===", flush=True)
            serial, serial_rows = await _ingest_serial(sources)
            for item in serial_rows:
                print(f"  {item['chunks']:2d} 块 / 实体 {item['entities']:3d} / "
                      f"边 {item['edges']:3d} / {item['seconds']:5.2f}s  "
                      f"{item['filename'][:48]}", flush=True)
            removed = _clear_source_edges([source.id for source in sources])
            print(f"  串行合计 {serial['seconds']}s；已删掉这批材料的 {removed} 条边，"
                  "准备量并发", flush=True)
            report["cost_serial"] = {**serial, "per_material": serial_rows,
                                     "extraction": meter.as_dict()}
            meter = Meter()
            meter.install()

        print("\n=== 入图：并发（产品的实现）===", flush=True)
        before = meter.as_dict()
        parallel = await tools_module._ingest_external_graph(sources)
        after = meter.as_dict()
        delta = {key: after[key] - before[key] for key in after}
        if parallel is None:
            print("  `_ingest_external_graph` 返回 None：开关或图的四道闸没过", flush=True)
            return 3
        print(f"  {parallel}", flush=True)
        report["cost_parallel"] = {**parallel, "extraction": delta}
        print(f"  并发 {parallel['seconds']}s（{meter.calls} 次调用 / 输入约 "
              f"{delta['in_tokens_est']} token）", flush=True)
    else:
        print("\n=== 入图（不计量）===", flush=True)
        parallel = await tools_module._ingest_external_graph(sources)
        print(f"  {parallel}", flush=True)

    scope = [source.id for source in used]
    count = graph_store.scoped_entity_count(scope)
    entities, links = graph_store.subgraph(entities=200, links=400, scope=scope)
    entity_names = [item.name for item in entities]
    print(f"\n图（scope = A/B 那 {len(scope)} 份）：带边实体 {count}（阈值 "
          f"{tools_module.GRAPH_OUTLINE_MIN_ENTITIES}）/ 取回 {len(entities)} 实体 / "
          f"{len(links)} 关系", flush=True)
    report["graph_entities_in_scope"] = count
    report["graph_entities_fetched"] = len(entities)
    report["graph_links_in_scope"] = len(links)

    # **A 组跑在入图之后**，靠开关（`MINDMAP_GRAPH_OUTLINE=0`）而不是靠"图里没货"来
    # 定义"现在的行为"：`_graph_outline` 的第一道闸就是那个开关，关着时它一次查询都
    # 不发，图里有什么对它零影响。放在这里是为了让两组共用同一份 `entity_names`
    # ——否则"实体覆盖"在 A 组那格只能是 0（没有实体名可比），那不是指标，是缺测。
    if "a" in wanted:
        for index in range(1, args.repeat + 1):
            print(f"\n=== {groups['a']} 第 {index} 次 ===", flush=True)
            outcome = await _run_once(topic=args.topic, branches=args.branches,
                                     sources=used, graph=False)
            if not outcome["ok"]:
                print(f"失败：{outcome['content']}", flush=True)
                continue
            report["runs"].append({"group": "a", **_report_run(
                outcome["content"], used, plain, used_titled, entity_names, outcome)})
            row = report["runs"][-1]
            print(f"  分支 {row['branches']}｜忠实度 {row['faithfulness']:.0%}"
                  f"｜摘要贡献 {row['summary_contribution']['only_plain']}"
                  f"｜{row['seconds']}s｜{row['branches_raw']}", flush=True)

    if "b" in wanted:
        for index in range(1, args.repeat + 1):
            print(f"\n=== {groups['b']} 第 {index} 次 ===", flush=True)
            outcome = await _run_once(topic=args.topic, branches=args.branches,
                                     sources=used, graph=True)
            if not outcome["ok"]:
                print(f"失败：{outcome['content']}", flush=True)
                continue
            report["runs"].append({"group": "b", **_report_run(
                outcome["content"], used, plain, used_titled, entity_names, outcome)})
            row = report["runs"][-1]
            print(f"  分支 {row['branches']}｜忠实度 {row['faithfulness']:.0%}"
                  f"｜摘要贡献 {row['summary_contribution']['only_plain']}"
                  f"｜图谱档 {'是' if row['graph_outline_used'] else '否'}"
                  f"｜{row['seconds']}s｜{row['branches_raw']}", flush=True)
            for item in row["faithful_rows"]:
                mark = "逐字" if item["hit"] else ("分段" if item["segments_all_hit"]
                                                   else "未命中")
                print(f"    [{mark}] {item['label']}", flush=True)

    print("\n=== 汇总 ===", flush=True)
    for key in wanted:
        rows_ok = [row for row in report["runs"]
                   if row["group"] == key and row.get("ok")]
        if not rows_ok:
            print(f"{groups[key]}：没有成功的样本", flush=True)
            continue

        def avg(field: str) -> float:
            values = [row[field] for row in rows_ok if isinstance(row.get(field), (int, float))]
            return round(sum(values) / len(values), 4) if values else 0.0

        print(f"{groups[key]}：字数覆盖 {avg('chars_coverage'):.1%} / 要点覆盖 "
              f"{avg('point_coverage'):.1%} / 实体覆盖 {avg('entity_coverage'):.1%} / "
              f"**骨架忠实度 {avg('faithfulness'):.1%}"
              f"（分段 {avg('faithfulness_segment'):.1%} / "
              f"4gram {avg('faithfulness_ngram'):.1%}）** / 摘要独占分支 "
              f"{sum(row['summary_contribution']['only_plain'] for row in rows_ok)} / "
              f"节点 {avg('nodes')}（{avg('branches')} 支） / {avg('seconds')}s",
              flush=True)
    if args.save:
        Path(args.save).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(f"\n报告已写入 {args.save}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
