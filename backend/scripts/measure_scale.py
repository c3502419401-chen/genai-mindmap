"""图该多大：节点数随不随材料量伸缩？三批材料 × 各 2 次，量现状。

## 它回答的问题

`INITIAL_MAX_NODES = 80` 是一个**与材料无关的常数**。若它真的把模型变成"填配额"，
那么材料从 500 字到 66,000 字，产出的说明总量会几乎不变——压缩比随之从 5:1 掉到 0.5:1。

这个脚本不做 A/B：**做法在三批上完全一致**，唯一变的是材料量。

    小 ~500 字   1 份短摘要
    中 ~5,600 字 16 份短摘要（`measure_graph_outline` 那批）
    大 ~66,000 字 4 份长文档（`measure_graph_two_pass` 那批）

## 第一瓶颈那一轮加的 A/B：`--arm`

`--arm a` 是**骨架只看主题**（加材料清单之前的行为），`--arm b` 是骨架看到清单
（`_material_inventory`），`--arm both` 在**同一个进程里交替**跑两臂——本组指标有
±5 的采样噪声（上一轮 A 臂重跑是 37 与 33 条），分组跑会把模型/网络的漂移算进差里，
交替跑把它同时摊到两臂上。

A 臂不是"另一份代码"：它只是把 `tools._material_inventory` 临时换成一个恒返回空串
的函数，`_plan_ask` 收到 `materials=""` 后一个字都不加——与加清单之前**逐字节相同**
（那条不变量有 `tests/test_plan_inventory.py` 钉着）。其余链路完全一致。

## 固定住的开关（否则量到的是别的东西）

    MINDMAP_SECTION_SLICE=0      12 章 → 12 次调用，变量太多（用户明确要求关）
    MINDMAP_GRAPH_OUTLINE=0      第三档骨架会按批差异化触发，是混淆变量
    MINDMAP_GRAPH_SKELETON=0     两段式，同上
    MINDMAP_EXTERNAL_KNOWLEDGE=0 材料已定死，不许再补
    MINDMAP_RERANK=0             少一次网络往返，只影响选题
    MINDMAP_NODE_SCALE           不设（= 关）：这一轮验的是"骨架给对分支数"能不能在
                                 **不改节点配额**的前提下改善结果

## 指标

  材料：字数 / 份数 / `heading_outline` 条数 / 目录覆盖率
  图：  节点数 / 一级分支 / 深度分布 / 说明总字数 / 说明均长
  压缩比 = 材料字数 / 说明总字数（用户表的口径：`ab-two-pass.a1` = 66,532/11,924 = 5.6）
  另外记：要点覆盖率（材料句子被节点文本覆盖的比例）与**锚定率**（`source_refs`
          区间并集 / 材料字数）——前者是"压缩比"缺的那一半，后者是唯一能区分
          "这段是材料的"与"这段是模型自己的"的指标（见 `docs/GRAPH-SCALE.md` 第 6 节）。

## 用法

    python scripts/measure_scale.py --probe               # 只量材料，不跑模型
    python scripts/measure_scale.py                       # 三批 × 各 2 次
    python scripts/measure_scale.py --only small --repeat 1
    python scripts/measure_scale.py --only medium --repeat 3 --arm both \\
        --save data/scale-stock-medium.json                # 清单 A/B（交替）
    python scripts/measure_scale.py --save data/scale.json
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 口径与函数直接复用 A/B 那两个脚本，不在这里另发明一套：
# `coverage` 的要点覆盖与实体覆盖、`tree_stats` 的说明均长、`_walk` 的遍历。
from measure_graph_outline import (  # noqa: E402
    _fill_env_from_registry,
    _materials as _outline_materials,
    _walk,
    coverage as coverage_of,
    tree_stats,
)
from measure_graph_two_pass import DOCS as LARGE_DOCS  # noqa: E402

# 大批的主题与两段式那批一致（可复现历史数字），中批与第三档骨架那批一致。
LARGE_TOPIC = "这个多智能体思维导图系统的架构与取舍"
MEDIUM_TOPIC = "机器学习在各领域的应用与研究"
SMALL_TARGET_CHARS = 500


def _batches() -> dict[str, dict]:
    """三批材料。**选择逻辑写在代码里**，报告里带上理由，避免下次换人重挑。"""
    from app.knowledge import library

    rows = [row for row in library.list_sources(limit=200)]
    by_name = {row["filename"]: row["id"] for row in rows}

    # 小：单份、有正文、最接近 500 字。挑"最接近"而不是"最短"：89 字那份是
    # wikipedia 碳中和的摘头，1 份 89 字摘要不足以谈"图该多大"；492 字那份是
    # 一篇完整摘要，正好是用户说的"~500 字 1 份"。
    small_row = min(
        (row for row in rows if (row.get("kind") in ("paper", "web"))),
        key=lambda row: abs(len((library.load(row["id"]).text or "")) - SMALL_TARGET_CHARS),
    )
    small_source = library.load(small_row["id"])

    # 中：与 `data/ab-graph-outline.json` 逐字同一批（16 份 / 5,616 字）。
    #
    # **不直接用 `_outline_materials()`**：库在这之后又多入了 4 份无目录的短摘要
    # （海绵城市 / 雨污分流那一簇），它现在给 20 份 / 6,472 字。用户要的是"就是
    # ab-graph-outline 那批"，所以按历史报告的 `materials` 清单精确挑；那 16 份的
    # 正文与当时逐字节相同（合计仍是 5,616 字，已核）。
    medium_report = BACKEND / "data" / "ab-graph-outline.json"
    medium_sources, medium_why = [], "16 份短摘要（ab-graph-outline 那份清单）"
    if medium_report.exists():
        hist = json.loads(medium_report.read_text(encoding="utf-8"))
        # 按（文件名，字数）双键挑：库里有**同名两份** `wikipedia: 碳中和`（89 / 157 字），
        # 只按文件名会挑到后来入库的那一份，合计从 5,616 变成 5,684。
        pool: dict[tuple[str, int], list] = {}
        for row in library.list_sources(limit=200):
            source = library.load(row["id"])
            if source is not None:
                pool.setdefault((source.filename or "", len(source.text or "")),
                                []).append(source)
        for item in hist.get("entity_counts") or []:
            key = (item["filename"], item["chars"])
            bucket = pool.get(key) or []
            if bucket:
                medium_sources.append(bucket.pop(0))
    if len(medium_sources) < 16:
        medium_sources, _ = _outline_materials()
        medium_why = "16 份短摘要（历史清单缺失，退回当前无目录材料）"

    # 大：与 `measure_graph_two_pass` 逐字同一批（4 份 / 66,5xx 字）。
    missing = [name for name in LARGE_DOCS if name not in by_name]
    if missing:
        raise SystemExit(f"库里缺少大批材料：{missing}")
    large_sources = [library.load(by_name[name]) for name in LARGE_DOCS]

    return {
        "small": {"topic": (small_source.filename or "").replace("openalex: ", "")
                           .replace("crossref: ", "").replace("wikipedia: ", ""),
                  "sources": [small_source],
                  "why": f"单份最接近 {SMALL_TARGET_CHARS} 字的带正文摘要"},
        "medium": {"topic": MEDIUM_TOPIC, "sources": medium_sources, "why": medium_why},
        "large": {"topic": LARGE_TOPIC, "sources": large_sources,
                  "why": "4 份长文档（与 measure_graph_two_pass 同一批）"},
    }


def _materials_probe(sources: list) -> dict:
    """材料侧的数字，一次都不调模型。"""
    from app.agent.tools import OUTLINE_MIN_COVERAGE
    from app.knowledge.service import heading_outline, outline_coverage

    entries = heading_outline(sources)
    covered = outline_coverage(sources, entries) if entries else 0.0
    return {
        "sources": len(sources),
        "chars": sum(len(source.text or "") for source in sources),
        "outline_entries": len(entries),
        "outline_coverage": round(covered, 4),
        "outline_usable": bool(entries) and covered >= OUTLINE_MIN_COVERAGE,
        "filenames": [source.filename for source in sources],
        "per_source": sorted(
            ((len(source.text or ""), source.filename or "") for source in sources)),
        "per_source_chars": sorted(len(source.text or "") for source in sources),
    }


def _depth_hist(content) -> dict:
    """每层节点数（root 是第 1 层）。层深 = 键的最大值。"""
    hist: dict[int, int] = {}

    def walk(node, level: int) -> None:
        hist[level] = hist.get(level, 0) + 1
        for child in node.children:
            walk(child, level + 1)

    walk(content.root, 1)
    return {"depth_hist": {str(level): hist[level] for level in sorted(hist)},
            "depth": max(hist) if hist else 0}


def _text_budget(content) -> dict:
    """说明与标题的字数账。压缩比的分母就是 `detail_total`。"""
    nodes = [node for node in _walk(content.root) if node is not content.root]
    details = [len((node.detail or "").strip()) for node in nodes]
    labels = [len((node.label or "").strip()) for node in nodes]
    filled = [value for value in details if value]
    return {
        "detail_total": sum(details),
        "detail_avg": round(sum(filled) / len(filled), 1) if filled else 0.0,
        # 最短的那一段是"配额有没有被填满"之外的另一半：均长 100 而最短 21 说明模型
        # 对个别节点通融了，而那种形态恰是第三段当初要消灭的（见 `DETAIL_FILL_FLOOR`）。
        "detail_min": min(filled) if filled else 0,
        "detail_empty": len(details) - len(filled),
        "label_total": sum(labels),
    }


@contextlib.contextmanager
def _arm(arm: str):
    """A 臂 = 把材料清单摘掉（骨架只看主题）。

    现在是**环境变量**而不是 monkeypatch：`MINDMAP_PLAN_INVENTORY=0` 会让
    `_material_inventory` 自己早退成空串，结果与"加清单之前"逐字节相同（有回归
    用例钉着）。换实现的原因很实际——四格实验要在同一个进程里反复横跳两个开关，
    monkeypatch 得记住原函数并在每次进出后恢复，而环境变量是进程状态的显式入口，
    退出时删掉就回到了默认（开）。
    """
    from app.agent import tools as tools_module

    original = os.environ.get(tools_module.PLAN_INVENTORY_ENV)
    if arm == "a":
        os.environ[tools_module.PLAN_INVENTORY_ENV] = "0"
    try:
        yield
    finally:
        if original is None:
            os.environ.pop(tools_module.PLAN_INVENTORY_ENV, None)
        else:
            os.environ[tools_module.PLAN_INVENTORY_ENV] = original


async def _run_once(
    *, topic: str, branches: int, sources: list, scale: bool, arm: str = "b",
    cap: int | None = None, detail_scale: bool | None = None,
) -> dict:
    """跑一次整树生成。开关全部显式设置——四格之间只许配额与清单两件事变。

    `scale=True` 打开 `MINDMAP_NODE_SCALE`（节点数按材料伸缩）；默认关 = 现状。
    `cap=N` 打开 `MINDMAP_NODE_CAP`（人为配额，优先于伸缩）；不设 = 现状。
    `arm` 见 `_arm`：`a` = 骨架只看主题，`b` = 骨架看到材料清单。
    `detail_scale` 三态：`True` 打开 `MINDMAP_DETAIL_SCALE`（每段说明按材料伸缩）、
    `False` 显式关、`None`（默认）不碰这个变量——`None` 与 `False` 在"效果"上一样，
    在**证据**上不一样：`None` 说明这条线根本没被这段代码碰过（历史脚本的默认路径）。
    """
    from app.agent import tools as tools_module
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall

    os.environ["MINDMAP_SECTION_SLICE"] = "0"
    os.environ[tools_module.GRAPH_OUTLINE_ENV] = "0"
    os.environ.pop("MINDMAP_GRAPH_SKELETON", None)
    for name, value in ((tools_module.budget.NODE_SCALE_ENV, "1" if scale else None),
                        (tools_module.budget.NODE_CAP_ENV,
                         str(cap) if cap is not None else None),
                        (tools_module.budget.DETAIL_SCALE_ENV,
                         None if detail_scale is None else
                         ("1" if detail_scale else "0"))):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

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

    started = time.perf_counter()
    with _arm(arm):
        # 开关的**自证**：同一批材料在两个臂上给出的清单长度不同，这是"确实切了"
        # 的唯一进程内证据（不看 env，看它的产物）。
        inventory_chars = len(tools_module._material_inventory(
            sources, has_structure=False))
        result = await registry.execute(call, ctx)
    elapsed = time.perf_counter() - started
    if not result.ok:
        return {"ok": False, "content": result.content, "seconds": round(elapsed, 2)}
    return {
        "ok": True,
        "seconds": round(elapsed, 2),
        "data": result.data or {},
        "content": ctx.candidate,
        "model_calls": getattr(ctx, "model_calls_used", None),
        "inventory_chars": inventory_chars,
        "node_cap": tools_module.budget.node_cap(),
        # 这一次第三段要求的区间（`_fill_details` 出的账；没开伸缩时是 None）：
        # 表里那一列比 κ 更能说明"差在哪一步"——区间对了而 κ 没动，是模型没听话。
        "detail_span": (result.data or {}).get("detail", {}).get("chars"),
    }


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--branches", type=int, default=6,
                        help="与两个历史 A/B 脚本同值，三批一致")
    parser.add_argument("--only", default="", choices=("", "small", "medium", "large"))
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--scale", action="store_true",
                        help="打开 MINDMAP_NODE_SCALE（节点数按材料伸缩）；不设 = 现状")
    parser.add_argument("--cap", type=int, default=None,
                        help="MINDMAP_NODE_CAP 的值（人为配额，优先于伸缩）；不设 = 现状")
    parser.add_argument("--arm", default="b", choices=("a", "b", "both"),
                        help="a=骨架只看主题（清单关）；b=骨架看到材料清单（清单开，"
                             "默认）；both=同一进程里交替跑两臂")
    parser.add_argument("--save", default="")
    args = parser.parse_args(argv)

    _fill_env_from_registry()
    os.environ.setdefault("MINDMAP_DB_PATH", str(BACKEND / "data" / "mindmaps.db"))
    os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "0"
    os.environ["MINDMAP_RERANK"] = "0"
    os.environ.pop("MINDMAP_SECTION_SLICE", None)
    os.environ.setdefault("MINDMAP_NEO4J_URI", "bolt://127.0.0.1:7687")
    os.environ.setdefault("MINDMAP_NEO4J_USER", "neo4j")

    batches = _batches()
    wanted = [args.only] if args.only else ["small", "medium", "large"]

    report: dict = {"branches": args.branches, "scale": args.scale,
                    "arm": args.arm, "batches": {}, "runs": []}
    for key in wanted:
        batch = batches[key]
        probe = _materials_probe(batch["sources"])
        report["batches"][key] = {**probe, "topic": batch["topic"], "why": batch["why"]}
        print(f"\n=== {key} === {probe['sources']} 份 / {probe['chars']:,} 字"
              f"｜章节 {probe['outline_entries']} 条（覆盖 {probe['outline_coverage']:.0%}"
              f"｜可用 {'是' if probe['outline_usable'] else '否'}）"
              f"\n    主题：{batch['topic']}", flush=True)
        for chars, name in probe["per_source"]:
            print(f"      {chars:6,d} 字  {name[:60]}", flush=True)

    if args.probe:
        if args.save:
            Path(args.save).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n已写入 {args.save}", flush=True)
        return 0

    from app.knowledge import graph_store  # noqa: F401  （配置探活）

    for key in wanted:
        batch = batches[key]
        probe = report["batches"][key]
        # 实体覆盖要一并算（`coverage` 的第三个口径）；大批的 scope 是全库图的一小块。
        entity_names: list[str] = []
        if graph_store.configured():
            entities, _ = graph_store.subgraph(
                entities=200, links=400, scope=[source.id for source in batch["sources"]])
            entity_names = [item.name for item in entities]

        # 交替跑：同一轮里先 A 后 B，再下一轮——漂移同时摊到两臂（见模块 docstring）。
        arms = ["a", "b"] if args.arm == "both" else [args.arm]
        for index in range(1, args.repeat + 1):
            for arm in arms:
                print(f"\n=== {key} 第 {index} 次 · {arm.upper()} 臂 ===", flush=True)
                outcome = await _run_once(topic=batch["topic"], branches=args.branches,
                                          sources=batch["sources"], scale=args.scale,
                                          arm=arm, cap=args.cap)
                if not outcome["ok"]:
                    print(f"失败：{outcome['content']}", flush=True)
                    report["runs"].append(
                        {"batch": key, "sample": index, "arm": arm, **outcome})
                    continue
                content = outcome["content"]
                data = outcome["data"]
                depth = _depth_hist(content)
                text = _text_budget(content)
                cover = coverage_of(content, batch["sources"], entity_names)
                stats = tree_stats(content)
                codes = [item["code"] for item in (data.get("diagnostics") or [])]
                chars = probe["chars"]
                row = {
                    "batch": key, "sample": index, "arm": arm, "ok": True,
                    "material_chars": chars,
                    "sources": probe["sources"],
                    "outline_entries": probe["outline_entries"],
                    "outline_usable": probe["outline_usable"],
                    "seconds": outcome["seconds"],
                    "nodes": stats["nodes"],
                    "branches": stats["branches"],
                    "branch_titles": [child.label for child in content.root.children],
                    **depth, **text, **cover,
                    "compression": round(chars / text["detail_total"], 2) if text["detail_total"] else None,
                    "chars_per_node": round(chars / stats["nodes"], 1) if stats["nodes"] else None,
                    "node_limit": data.get("node_limit"),
                    "cap": outcome.get("node_cap"),
                    "inventory_chars": outcome.get("inventory_chars"),
                    "scaled": "NODE_SCALE_APPLIED" in codes,
                    "capped": "NODE_CAP_APPLIED" in codes,
                    "cut": "TREE_CLIPPED_TO_INITIAL" in codes,
                    "partial": bool(data.get("partial")),
                    "diagnostics": codes[:12],
                }
                report["runs"].append(row)
                print(
                    f"节点 {row['nodes']}（{row['branches']} 支）｜层 {row['depth']} "
                    f"{row['depth_hist']}｜说明 {row['detail_total']:,} 字"
                    f"（均 {row['detail_avg']}，空 {row['detail_empty']}）"
                    f"｜上限 {row['node_limit']}"
                    f"｜**压缩比 {row['compression']}:1**"
                    f"｜要点覆盖 {row['point_coverage']:.1%}"
                    f"｜锚定 {row['chars_coverage']:.1%}"
                    f"｜{row['seconds']}s", flush=True)
                print(f"    一级分支：{row['branch_titles']}", flush=True)
                if args.save:
                    tree_path = Path(args.save).with_suffix(f".{key}{index}{arm}.json")
                    tree_path.write_text(
                        json.dumps(content.model_dump(), ensure_ascii=False, indent=1),
                        encoding="utf-8")

    if args.save:
        Path(args.save).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\n报告已写入 {args.save}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
