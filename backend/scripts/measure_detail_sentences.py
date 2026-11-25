"""说明那一轮的**句子数下限**：三臂 × 两批 × 每批 2 次。

## 它回答的问题

`DETAIL_ONLY_PROMPT` 里有一条「每一段至少 3 句完整的话」。它的来历写在 `tools.py`
第三段那段注释里：把「结构 + 每说明」压在同一次输出时 detail 五采样只有
45.3 / 28.0 / 28.5 / 26.2 / 27.4 字，并在 system 里加"不少于 60 字"反而更短
（28.5 → 26.2）。**救"越写越短"的第一道是 80 字下限，句子数是第二道加固。**

这条加固也可能是噪声：句数下限会把"没那么多可说"的节点逼成三句结构合格、信息重复
的话。这一次量它：放开之后长度会不会塌、会不会碎片化、会不会更贵。

## 三臂

    A  现状          80–150 字、至少 3 句完整的话        （开关关，逐字节不变）
    B  去句数        80–150 字，不设句子数               （`MINDMAP_DETAIL_SENTENCES=1`）
    C  去句数+去下限  不设句数、不设字数下限，按材料分量   （实验用，不产品化）

C 保留 150 作**建议上限**。依据不是"整齐"：上限管的是可读性与输出量（第三段的墙钟
= 输出字数 / 吞吐），去掉它等于同时放开 κ 与耗时两件事；而下限管的是"写多少才够"，
那才是这次要交给材料的部分。

## 为什么三臂跑**同一份清单、同一份证据**

要量的是提示词的措辞，不是"树会怎么长"。所以分两段：

1. **采集**（每批一次，走完整生成链路）：拿到这一次真实的结构树 + 第三段真实的输入
   （`sections` / `full_text` / `material_chars` / `skip_ids`），顺手把 `_fill_details`
   换成"只记录不执行"。树里 detail 全空 = 第三段要写全部节点，与真实路径一致
   （结构那一轮的 system 明确要求"这一轮不要写 detail"）。
2. **重放**（三臂 × 2 次）：同一棵树、同一份 `sections`、同一个区间，只换 system。
   `_fill_details` 本身不动——patch 的只有 `_detail_prompt` 与 `_detail_self_check`
   （C 臂），A/B 两臂走的都是产品路径（B 就是那个开关）。

如果不钉清单，臂间的差里会混进"树的形状不同"：κ 的分母是材料字数（固定），分子是
Σdetail——节点数一变，均长与 κ 就同时漂，句子数分布也不再可比。

## 指标

    ①  均长 / 中位 / 最短 / 最长          ← 放开之后会不会塌到 40 字以下
    ②  κ = Σdetail / 材料字数            ← >2 就是膨胀
    ③  ρ = 8-gram 句覆盖率（`measure_graph_outline.coverage`）← 有没有因为变短而漏材料
    ④  句子数分布（1 / 2 / 3 / 4+ 句，按 。！？ 切）
    ⑤  平均句长、**碎句收尾**（末句 < 10 字）、**一句 90 字**（1 句且 ≥80 字）
    ⑥  耗时（第三段墙钟）与 system 长度（prompt 侧成本）

## 固定住的开关

    MINDMAP_EXTERNAL_KNOWLEDGE=0   材料已定死，不许再补
    MINDMAP_RERANK=0               少一次网络往返（只影响检索质量，三臂一致）
    MINDMAP_SECTION_SLICE=0        12 章 → 12 次调用，变量太多
    MINDMAP_DETAIL_SCALE / NODE_SCALE 关   区间恒为 80–150，三臂同一个数

## 用法

    python scripts/measure_detail_sentences.py --capture
    python scripts/measure_detail_sentences.py --replay --repeat 2
    python scripts/measure_detail_sentences.py --report data/detail-sentences-ab.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
for extra in (BACKEND, Path(__file__).resolve().parent):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from measure_graph_outline import _fill_env_from_registry  # noqa: E402
from measure_graph_outline import coverage as coverage_of  # noqa: E402
from measure_graph_two_pass import DOCS as THICK_DOCS  # noqa: E402

THIN_KEYWORD = "碳中和"
THIN_TOPIC = "碳中和"
THICK_TOPIC = "这个多智能体思维导图系统的架构与取舍"

#: 句切分：与任务口径一致（。！？）。不切分号与逗号——那是同一句里的停顿，
#: 按它切会把"一句 90 字"误判成"三句 30 字"，正好把要量的东西抹掉。
SENTENCE_SPLIT = re.compile(r"[。！？]+")

#: "碎句"的阈值：一句不到 10 字（去空白后）基本只是转折或小结，不承载信息。
SHORT_SENTENCE_CHARS = 10
#: "一句话硬撑到下限"的形态：1 句且长度 ≥ 80。
LONG_SINGLE_SENTENCE_CHARS = 80

ARMS = ("A", "B", "C", "D")
ARM_LABELS = {
    "A": "现状：80–150 字 + 至少 3 句",
    "B": "去句数：80–150 字，不设句子数",
    "C": "去句数 + 去下限：按材料分量，150 只作建议上限",
    "D": "去句数 + 限句长：80–150 字，单句不超过 45 字",
}

#: D 臂的单句上限。取 45 不是拍的：80 字下限 ÷ 2 句 = 40，留 5 字余量；而中文
#: 一句 45 字已接近"读第二遍才顺"的边界（现状 A 臂的实测均句长是 33.9）。
SENTENCE_CAP = 45

SNAPSHOT = BACKEND / "data" / "detail-sentences-capture.json"
REPORT = BACKEND / "data" / "detail-sentences-ab.json"


# ---------------------------------------------------------------- 材料与环境


def _env_from_bat() -> None:
    """`run-backend.bat` 里 `set NAME=VALUE` 的项补进环境（只补未设置的）。

    为什么不硬编码：那个 bat 是这台机器上唯一有 embedding key 的地方（用户级
    `HKCU\\Environment` 里只有 LLM 那几个）。今天 embedding 挂过一次，降级与完整的
    区别全在这几个变量上，抄一份进脚本就等于让脚本自己造一个未来会漂的真相。
    """
    bat = BACKEND.parent / "run-backend.bat"
    if not bat.exists():
        return
    for line in bat.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line.lower().startswith("set ") or "=" not in line:
            continue
        name, _, value = line[4:].partition("=")
        name, value = name.strip(), value.strip()
        # 跳过 `>>` 重定向那种行（`set > file`）
        if not name or not value or name.startswith(">"):
            continue
        os.environ.setdefault(name, value)


def _probe_embedding() -> str:
    """这一次检索是完整还是降级：真发一次 embedding 请求。

    今天这一条挂过一次（40 批 chunk 向量全失败 → 检索降级纯 BM25），而降级会让 ρ 的
    分母变成"只靠字面命中"，与完整检索的读数不是同一件事。所以每一份报告都要带上
    这一次的答案，不能靠"昨天是好的"。
    """
    try:
        from app.knowledge.embedding import embed_texts

        vectors = embed_texts(["碳中和"])
    except Exception as exc:  # noqa: BLE001 - 探测本身不许中断实验
        return f"error: {type(exc).__name__}: {exc}"
    if not vectors or vectors[0] is None:
        return "degraded: 没有向量（纯 BM25）"
    return f"ok: {len(vectors[0])} 维"


def _batches() -> dict[str, dict]:
    from app.knowledge import library

    rows = library.list_sources(limit=300)
    thin_rows = [row for row in rows if THIN_KEYWORD in (row.get("filename") or "")]
    thin = [library.load(row["id"]) for row in thin_rows]
    by_name = {row["filename"]: row["id"] for row in rows}
    missing = [name for name in THICK_DOCS if name not in by_name]
    if missing:
        raise SystemExit(f"库里缺少厚批材料：{missing}")
    thick = [library.load(by_name[name]) for name in THICK_DOCS]
    return {
        "thin": {"topic": THIN_TOPIC, "sources": thin,
                 "why": f"库内 filename 含「{THIN_KEYWORD}」的 {len(thin)} 份"},
        "thick": {"topic": THICK_TOPIC, "sources": thick,
                  "why": f"docs 四份长文档（{', '.join(THICK_DOCS)}）"},
    }


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _entity_names(sources) -> list[str]:
    try:
        from app.knowledge import graph_store

        if not graph_store.configured():
            return []
        entities, _ = graph_store.subgraph(
            entities=200, links=400, scope=[source.id for source in sources])
        return [item.name for item in entities]
    except Exception:  # noqa: BLE001 - 图库没起不该让实验停
        return []


# ---------------------------------------------------------------- 1. 采集


async def _capture_batch(name: str, batch: dict) -> dict:
    """跑一次完整生成，把第三段的**输入**整份截下来（不真跑第三段）。"""
    from app.agent import tools as tools_module
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall

    os.environ["MINDMAP_SECTION_SLICE"] = "0"
    os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "0"
    os.environ["MINDMAP_RERANK"] = "0"
    # 收尾那几步（核查 / 跨图标引 / 入图）都在第三段之后，与本次实验无关：
    # 它们只会给采集那一次加几分钟，一个字节都不进第三段的输入。
    for late in ("MINDMAP_VERIFY_AFTER_GENERATE", "MINDMAP_OPTIMIZATION_CROSS_MAP",
                 "MINDMAP_GRAPH_INGEST", "MINDMAP_EXTERNAL_DEEPSEEK_SEARCH"):
        os.environ[late] = "0"
    for switch in (tools_module.budget.NODE_SCALE_ENV,
                   tools_module.budget.DETAIL_SCALE_ENV,
                   tools_module.DETAIL_SENTENCES_ENV):
        os.environ.pop(switch, None)

    captured: dict = {}

    async def spy(content, **kwargs):
        # 只记输入，然后把树原样交回去：这一次不花第三段的两次调用
        captured["tree"] = content.model_dump()
        captured["fill"] = {
            "sections": list(kwargs.get("sections") or ()),
            "full_text": bool(kwargs.get("full_text")),
            "instruction": kwargs.get("instruction"),
            "material_chars": int(kwargs.get("material_chars") or 0),
            "skip_ids": list(kwargs.get("skip_ids") or ()),
            "external_rules": kwargs.get("external_rules") or "",
            "chapters": len(kwargs.get("chapters") or ()),
        }
        return content, {"requested": 0, "filled": 0, "missing": 0,
                         "unknown_keys": 0, "seconds": 0.0,
                         "skipped": "capture"}

    request = AgentRunRequest(
        goal=batch["topic"], intent="create", thinking_model="default",
        sources=batch["sources"],
        budget=RunBudget(max_model_calls=60, max_tool_calls=8, max_seconds=1200.0,
                         max_output_tokens=65536, max_repairs=1))
    ctx = RunContext(request=request,
                     sources_by_id={source.id: source for source in batch["sources"]})
    registry = tools_module.build_registry("create", has_sources=True, has_library=False)
    call = ToolCall(call_id="capture", name="mindmap_generate",
                    arguments={"topic": batch["topic"], "branches": 6})

    started = time.perf_counter()
    with patch.object(tools_module, "_fill_details", new=spy):
        result = await registry.execute(call, ctx)
    elapsed = round(time.perf_counter() - started, 2)
    if not result.ok:
        raise SystemExit(f"[{name}] 采集失败：{result.content}")

    fill = captured.get("fill") or {}
    if fill.get("chapters"):
        # 采集阶段故意关掉按章切（`MINDMAP_SECTION_SLICE=0`）：章对象不可序列化，
        # 而这条路在本次实验里不是变量。真出现说明上面那条 env 没生效。
        raise SystemExit(f"[{name}] 采集到了 chapters，重放无法复现：{fill['chapters']}")
    return {
        "batch": name, "topic": batch["topic"], "why": batch["why"],
        "capture_seconds": elapsed,
        "material_chars": sum(len(source.text or "") for source in batch["sources"]),
        "sources": [{"id": source.id, "filename": source.filename,
                     "chars": len(source.text or "")} for source in batch["sources"]],
        "tree": captured["tree"],
        "fill": fill,
        "sections_chars": sum(len(item) for item in fill.get("sections") or ()),
        "diagnostics": sorted({item["code"] for item in
                               (result.data or {}).get("diagnostics") or []}),
        "data_detail": (result.data or {}).get("detail"),
    }


async def capture_all(names: list[str] | None = None) -> int:
    _fill_env_from_registry()
    _env_from_bat()
    os.environ.setdefault("MINDMAP_DB_PATH", str(BACKEND / "data" / "mindmaps.db"))

    probe = _probe_embedding()
    print(f"embedding 探测：{probe}", flush=True)
    batches = _batches()
    picked = {name: batches[name] for name in (names or list(batches))}
    # 合并已有快照：`--batches thin` 之后再跑 `--batches thick` 不该把薄批冲掉。
    out: dict = {"embedding": probe, "captures": {}}
    if SNAPSHOT.exists():
        out = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        out["embedding"] = probe
        out.setdefault("captures", {})
    for name, batch in picked.items():
        print(f"\n=== 采集 {name} === {batch['why']}"
              f"｜{sum(len(s.text or '') for s in batch['sources']):,} 字", flush=True)
        capture = await _capture_batch(name, batch)
        out["captures"][name] = capture
        fill = capture["fill"]
        from app.contracts.mindmap import MindMapContent

        targets = sum(1 for _ in _walk(MindMapContent.model_validate(capture["tree"]).root))
        print(f"树 {targets} 个节点｜证据 {len(fill['sections'])} 节 / "
              f"{capture['sections_chars']:,} 字｜full_text={fill['full_text']}"
              f"｜material_chars={fill['material_chars']:,}"
              f"｜采集耗时 {capture['capture_seconds']}s", flush=True)
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n快照已写入 {SNAPSHOT}", flush=True)
    return 0


# ---------------------------------------------------------------- 2. 重放


def _arm_prompt(arm: str, min_chars: int, max_chars: int) -> str:
    from app.agent import tools as tools_module

    if arm == "A":
        return tools_module._detail_only_prompt(min_chars, max_chars)
    if arm == "B":
        return tools_module._detail_only_prompt_open(min_chars, max_chars)
    if arm == "D":
        return (
            tools_module._DETAIL_ONLY_SHAPE_RULES
            + f"\n每一段说明写 {min_chars}–{max_chars} 字，少于 {min_chars} 字就是没写完。"
            "分几句按意思走，几句都行，但**单句不要超过 "
            f"{SENTENCE_CAP} 字**——一句话写到七八十字，"
            "读者要回头读第二遍，拆成两句就好。"
            "如果这个节点的标题是一串并列的东西（几个算法、几种变体、几类方法、几个阶段），"
            "要逐个说清它们各自做什么、差别在哪，不要只把名字列一遍就收。"
            f"超过 {max_chars} 字就挑最关键的两三点，长文留给节点下面的子节点。\n"
        )
    return (
        tools_module._DETAIL_ONLY_SHAPE_RULES
        + "\n每一段的长度按这个节点在材料里的分量决定：材料里讲得多的写足，"
        "只被提了一句的就写一两句，不设字数下限。"
        "分几句按意思走，几句都行，但不要把一句话硬拉长。"
        "如果这个节点的标题是一串并列的东西（几个算法、几种变体、几类方法、几个阶段），"
        "要逐个说清它们各自做什么、差别在哪，不要只把名字列一遍就收。"
        f"建议不超过 {max_chars} 字——超了就挑最关键的两三点，"
        "长文留给节点下面的子节点。\n"
    )


def _arm_check(arm: str):
    """C/D 臂的自查句：A/B 走产品实现（`_detail_self_check`）。"""
    if arm not in ("C", "D"):
        return None

    def check(count: int, span) -> str:
        if arm == "C":
            return (f"\n\n写完后自查一遍：上面 {count} 个 id 是不是都写了？"
                    f"每一段是不是都不超过 {span.max_chars} 字？"
                    "没写全的补上，然后再返回。")
        return (f"\n\n写完后自查一遍：上面 {count} 个 id 是不是都写了？"
                f"每一段是不是都在 {span.min_chars}–{span.max_chars} 字、"
                f"没有哪一句超过 {SENTENCE_CAP} 字？不够的补写，然后再返回。")

    return check


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in SENTENCE_SPLIT.split(text or "") if part.strip()]


def _detail_rows(content, only: set[str] | None = None) -> list[dict]:
    """每段说明的形态。口径与 `measure_scale._text_budget` 一致：不含中心主题。

    `only` 是**这一次真的写回来的** id 集合。不按它过滤会把结构轮留下的东西一起统计
    进去：折叠节点（「其他 N 项」）的 `detail` 是那张清单本身（`含：A；B；C`，34 字、
    1 句），它进 `skip_ids` 根本不归第三段写。实测厚批 19 段里有 6 段是这一类——不
    剔除的话，"1 句占 37%"这个读数有一半是别人的产物。
    """
    rows: list[dict] = []
    for node in _walk(content.root):
        if node is content.root:
            continue
        if only is not None and node.id not in only:
            continue
        text = (node.detail or "").strip()
        if not text:
            continue
        parts = _sentences(text)
        lengths = [len(re.sub(r"\s+", "", part)) for part in parts] or [0]
        rows.append({
            "id": node.id, "label": node.label, "chars": len(text),
            "sentences": len(parts), "sentence_chars": lengths,
            "tail_chars": lengths[-1],
            "text": text,
        })
    return rows


def _metrics(rows: list[dict], material_chars: int, cover: dict) -> dict:
    lengths = [row["chars"] for row in rows]
    per_sentence = [statistics.mean(row["sentence_chars"]) for row in rows]
    hist = {"1": 0, "2": 0, "3": 0, "4+": 0}
    for row in rows:
        key = "4+" if row["sentences"] >= 4 else str(row["sentences"])
        hist[key] += 1
    short_tail = [row for row in rows if row["tail_chars"] < SHORT_SENTENCE_CHARS]
    single_long = [row for row in rows
                   if row["sentences"] == 1 and row["chars"] >= LONG_SINGLE_SENTENCE_CHARS]
    three_tail = [row for row in rows
                  if row["sentences"] >= 3 and row["tail_chars"] < SHORT_SENTENCE_CHARS]
    total = sum(lengths)
    return {
        "segments": len(rows),
        "detail_total": total,
        "detail_avg": round(statistics.mean(lengths), 1) if lengths else 0.0,
        "detail_median": statistics.median(lengths) if lengths else 0,
        "detail_min": min(lengths) if lengths else 0,
        "detail_max": max(lengths) if lengths else 0,
        "kappa": round(material_chars / total, 2) if total else None,
        "rho": cover.get("point_coverage"),
        "rho_sentences": cover.get("sentences"),
        "sent_hist": hist,
        "sent_avg_chars": round(statistics.mean(per_sentence), 1) if per_sentence else 0.0,
        "tail_short": len(short_tail),
        "tail_short_share": round(len(short_tail) / len(rows), 3) if rows else 0.0,
        "single_long": len(single_long),
        "single_long_share": round(len(single_long) / len(rows), 3) if rows else 0.0,
        "three_short_tail": len(three_tail),
        "three_short_tail_share": (round(len(three_tail) / len(rows), 3) if rows else 0.0),
        "under_40": sum(1 for value in lengths if value < 40),
        "under_80": sum(1 for value in lengths if value < 80),
        "over_150": sum(1 for value in lengths if value > 150),
    }


async def _replay_once(capture: dict, arm: str) -> dict:
    from app.agent import tools as tools_module
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext
    from app.contracts.mindmap import MindMapContent

    fill = capture["fill"]
    content = MindMapContent.model_validate(capture["tree"])
    request = AgentRunRequest(
        goal=capture["topic"], intent="create", thinking_model="default", sources=[],
        budget=RunBudget(max_model_calls=30, max_tool_calls=4, max_seconds=900.0,
                         max_output_tokens=65536, max_repairs=0))
    ctx = RunContext(request=request, sources_by_id={})
    diagnostics: list = []

    open_switch = arm in ("B", "C")
    if open_switch:
        os.environ[tools_module.DETAIL_SENTENCES_ENV] = "1"
    else:
        os.environ.pop(tools_module.DETAIL_SENTENCES_ENV, None)

    patches = [patch.object(
        tools_module, "_detail_prompt",
        new=lambda lo, hi, _arm=arm: _arm_prompt(_arm, lo, hi))]
    check = _arm_check(arm)
    if check is not None:
        patches.append(patch.object(tools_module, "_detail_self_check", new=check))
    for item in patches:
        item.start()

    started = time.perf_counter()
    before = {node.id: (node.detail or "") for node in _walk(content.root)}
    try:
        filled, report = await tools_module._fill_details(
            content, ctx=ctx, topic=capture["topic"],
            sections=fill["sections"], external_rules=fill["external_rules"],
            diagnostics=diagnostics, skip_ids=fill["skip_ids"],
            full_text=fill["full_text"], material_chars=fill["material_chars"])
    finally:
        for item in patches:
            item.stop()

    seconds = round(time.perf_counter() - started, 2)
    after = {node.id: (node.detail or "") for node in _walk(filled.root)}
    written = {node_id for node_id, text in after.items()
               if text and text != before.get(node_id, "")}
    from app.agent.tools import _default_detail_span

    span = _default_detail_span()
    system = _arm_prompt(arm, span.min_chars, span.max_chars)
    return {
        "filled": report.get("filled"),
        "missing": report.get("missing"),
        "unknown": report.get("unknown_keys"),
        "skipped": report.get("skipped"),
        "seconds": seconds,
        "report_seconds": report.get("seconds"),
        "system": system,
        "system_chars": len(system),
        "sentences_open": bool(report.get("sentences_open")),
        "diagnostics": [item.code for item in diagnostics],
        "content": filled,
        "written": sorted(written),
    }


async def replay_all(repeat: int, batches: list[str], arms: list[str]) -> int:
    _fill_env_from_registry()
    _env_from_bat()
    os.environ.setdefault("MINDMAP_DB_PATH", str(BACKEND / "data" / "mindmaps.db"))
    if not SNAPSHOT.exists():
        raise SystemExit(f"没有快照：先跑 --capture（{SNAPSHOT}）")
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    all_batches = _batches()

    runs: list[dict] = []
    print(f"embedding（采集时探测）：{snapshot.get('embedding')}", flush=True)
    for index in range(1, repeat + 1):
        for name in batches:
            capture = snapshot["captures"][name]
            sources = all_batches[name]["sources"]
            entities = _entity_names(sources)
            for arm in arms:
                print(f"\n=== 第 {index} 轮 · {name} · {arm} 臂（{ARM_LABELS[arm]}）===",
                      flush=True)
                outcome = await _replay_once(capture, arm)
                rows = _detail_rows(outcome["content"], set(outcome["written"]))
                cover = coverage_of(outcome["content"], sources, entities)
                metrics = _metrics(rows, capture["material_chars"], cover)
                row = {
                    "batch": name, "arm": arm, "sample": index,
                    "material_chars": capture["material_chars"],
                    "seconds": outcome["seconds"],
                    "report_seconds": outcome["report_seconds"],
                    "system_chars": outcome["system_chars"],
                    "filled": outcome["filled"], "missing": outcome["missing"],
                    "written": len(outcome["written"]),
                    "sentences_open": outcome["sentences_open"],
                    "diagnostics": outcome["diagnostics"],
                    **metrics,
                }
                runs.append(row)
                print(f"段 {metrics['segments']}（filled {row['filled']}"
                      f"／written {row['written']}）"
                      f"｜合计 {metrics['detail_total']:,} 字"
                      f"｜均 {metrics['detail_avg']}／中位 {metrics['detail_median']}"
                      f"／最短 {metrics['detail_min']}／最长 {metrics['detail_max']}"
                      f"｜κ {metrics['kappa']}｜ρ {metrics['rho']}"
                      f"｜句数 {metrics['sent_hist']}"
                      f"｜碎句收尾 {metrics['tail_short']}"
                      f"（{metrics['tail_short_share']:.0%}）"
                      f"｜一句≥80 {metrics['single_long']}",
                      flush=True)
                print(f"    耗时 {row['seconds']}s（report {row['report_seconds']}s）"
                      f"｜system {row['system_chars']} 字", flush=True)
                runs[-1]["sentences"] = [
                    {key: row[key] for key in
                     ("id", "label", "chars", "sentences", "sentence_chars", "tail_chars")}
                    for row in rows]

    # 合并已有报告：先跑 `--arms A,B,C` 再跑 `--arms D` 不该把前三个臂冲掉。
    merged: dict[tuple, dict] = {}
    if REPORT.exists():
        for row in json.loads(REPORT.read_text(encoding="utf-8")).get("runs") or []:
            merged[(row["batch"], row["arm"], row["sample"])] = row
    for row in runs:
        merged[(row["batch"], row["arm"], row["sample"])] = row
    runs = sorted(merged.values(), key=lambda item: (item["batch"], item["arm"],
                                                     item["sample"]))
    seen_batches = sorted({row["batch"] for row in runs})
    seen_arms = [arm for arm in ARMS if any(row["arm"] == arm for row in runs)]
    report = {
        "embedding": snapshot.get("embedding"),
        "repeat": repeat,
        "arms": {arm: ARM_LABELS[arm] for arm in seen_arms},
        "captures": {name: {key: capture[key] for key in
                            ("topic", "why", "material_chars", "capture_seconds",
                             "sections_chars", "diagnostics", "data_detail")}
                     for name, capture in snapshot["captures"].items()},
        "systems": {arm: _arm_prompt(arm, 80, 150) for arm in seen_arms},
        "checks": {arm: _check_text(arm) for arm in seen_arms},
        "runs": runs,
        "summary": _summary(runs, seen_batches, seen_arms),
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                      encoding="utf-8")
    _print_table(report)
    print(f"\n报告已写入 {REPORT}", flush=True)
    return 0


def _check_text(arm: str) -> str:
    """这一臂的自查句原文（进报告，方便读 diff 的人看到"最后一行"也换了）。"""
    from app.agent import tools as tools_module
    from app.agent.tools import _default_detail_span

    span = _default_detail_span()
    custom = _arm_check(arm)
    if custom is not None:
        return custom(99, span)
    if arm == "B":
        os.environ[tools_module.DETAIL_SENTENCES_ENV] = "1"
    else:
        os.environ.pop(tools_module.DETAIL_SENTENCES_ENV, None)
    try:
        return tools_module._detail_self_check(99, span)
    finally:
        os.environ.pop(tools_module.DETAIL_SENTENCES_ENV, None)


def _mean(values) -> float | None:
    picked = [value for value in values if isinstance(value, (int, float))]
    return round(sum(picked) / len(picked), 2) if picked else None


def _summary(runs: list[dict], batches: list[str], arms: list[str]) -> dict:
    out: dict = {}
    for name in batches:
        for arm in arms:
            rows = [row for row in runs
                    if row["batch"] == name and row["arm"] == arm]
            if not rows:
                continue
            out[f"{name}:{arm}"] = {
                "runs": len(rows),
                "segments": _mean([row["segments"] for row in rows]),
                "detail_total": _mean([row["detail_total"] for row in rows]),
                "detail_avg": _mean([row["detail_avg"] for row in rows]),
                "detail_median": _mean([row["detail_median"] for row in rows]),
                "detail_min": min(row["detail_min"] for row in rows),
                "detail_max": max(row["detail_max"] for row in rows),
                "kappa": _mean([row["kappa"] for row in rows]),
                "rho": _mean([row["rho"] for row in rows]),
                "sent_avg_chars": _mean([row["sent_avg_chars"] for row in rows]),
                "tail_short": _mean([row["tail_short"] for row in rows]),
                "tail_short_share": _mean([row["tail_short_share"] for row in rows]),
                "single_long": _mean([row["single_long"] for row in rows]),
                "single_long_share": _mean([row["single_long_share"] for row in rows]),
                "three_short_tail": _mean([row["three_short_tail"] for row in rows]),
                "under_40": _mean([row["under_40"] for row in rows]),
                "under_80": _mean([row["under_80"] for row in rows]),
                "over_150": _mean([row["over_150"] for row in rows]),
                "seconds": _mean([row["seconds"] for row in rows]),
                "system_chars": rows[0]["system_chars"],
                "sent_hist": {key: _mean([row["sent_hist"][key] for row in rows])
                              for key in ("1", "2", "3", "4+")},
            }
    return out


def _pct(value, digits: int = 1) -> str:
    return "-" if not isinstance(value, (int, float)) else f"{value:.{digits}%}"


def _print_table(report: dict) -> None:
    print("\n## 六类指标（每格均值；最短/最长取该格极值）\n")
    print("| 批 | 臂 | 段 | Σdetail | 均长 | 中位 | 最短 | 最长 | κ | ρ "
          "| 均句长 | 耗时 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for key, item in report["summary"].items():
        batch, arm = key.split(":")
        print(f"| {batch} | {arm} | {item['segments']} | {item['detail_total']:,.0f} "
              f"| {item['detail_avg']} | {item['detail_median']} | {item['detail_min']} "
              f"| {item['detail_max']} | {item['kappa']} | {_pct(item['rho'])} "
              f"| {item['sent_avg_chars']} | {item['seconds']}s |")

    print("\n## 碎片化（句子数分布 / 碎句收尾 / 一句硬撑）\n")
    print("| 批 | 臂 | 1 句 | 2 句 | 3 句 | 4+ 句 | 碎句收尾(<10字) | 一句≥80字 "
          "| 3句且末句<10字 | <40 字段 | <80 字段 | >150 字段 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for key, item in report["summary"].items():
        batch, arm = key.split(":")
        hist = item["sent_hist"]
        print(f"| {batch} | {arm} | {hist['1']} | {hist['2']} | {hist['3']} "
              f"| {hist['4+']} | {item['tail_short']:.1f}"
              f"（{_pct(item['tail_short_share'])}）"
              f"| {item['single_long']:.1f}（{_pct(item['single_long_share'])}）"
              f"| {item['three_short_tail']:.1f} | {item['under_40']:.1f} "
              f"| {item['under_80']:.1f} | {item['over_150']:.1f} |")


# ---------------------------------------------------------------- 3. 只出表


def print_report(path: Path) -> int:
    report = json.loads(path.read_text(encoding="utf-8"))
    _print_table(report)
    for arm, system in (report.get("systems") or {}).items():
        print(f"\n### {arm} 臂 system（{len(system)} 字符）\n")
        print(system)
    return 0


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", action="store_true", help="每批跑一次完整生成，存快照")
    parser.add_argument("--replay", action="store_true", help="三臂重放（需要快照）")
    parser.add_argument("--repeat", type=int, default=2, help="每格轮数")
    parser.add_argument("--batches", default="thin,thick")
    parser.add_argument("--arms", default="A,B,C")
    parser.add_argument("--report", default="", help="只读 JSON 出表，不跑模型")
    args = parser.parse_args(argv)

    if args.report:
        path = Path(args.report)
        return print_report(path if path.is_absolute() else BACKEND / path)
    if not args.capture and not args.replay:
        parser.print_help()
        return 1
    if args.capture:
        await capture_all([name.strip() for name in args.batches.split(",")
                           if name.strip()])
    if args.replay:
        batches = [name.strip() for name in args.batches.split(",") if name.strip()]
        arms = [name.strip().upper() for name in args.arms.split(",") if name.strip()]
        for name in arms:
            if name not in ARMS:
                raise SystemExit(f"未知的臂：{name}（可选 {', '.join(ARMS)}）")
        await replay_all(args.repeat, batches, arms)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
