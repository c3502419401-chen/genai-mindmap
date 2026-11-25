"""生成策略这一轮的实测：三批 × 每批 2 次，六个指标。

跑的是**真模型**（DeepSeek，凭据取自用户级环境变量），走的是生产那条路
（`mindmap_generate` 整树一次生成），与界面上点一次"生成"的代码完全一致。

三批：

    薄批  `filename like '%碳中和%'`（4 份 / 673 字）
    厚批  docs 那四份（66,536 字）
    纯主题  不给材料（走 B+C：`DETAIL_NO_SOURCE_RULES` + `MINDMAP_VERIFY_AFTER_GENERATE`）

六个指标（每个都从**产出本身**算，不看提示词）：

    ① 引用覆盖率   有至少一条 source_refs 的节点 / 全部节点      ★ 核心
    ② 分支数 / 扇出  一级分支数、扇出直方图、有没有分叉
    ③ 节点数
    ④ 折叠 / 裁剪  `folded_rollups` / `folded_nodes` / `cut_nodes` + 聚合节点样本
    ⑤ 耗时
    ⑥ token（输入/输出，取自 `data["tokens"]`，没有就不报）

用法：

    python scripts/measure_generation_strategy.py --batch thin --repeat 2
    python scripts/measure_generation_strategy.py --batch thick --repeat 2
    python scripts/measure_generation_strategy.py --batch topic --repeat 2
    python scripts/measure_generation_strategy.py --batch all --repeat 2

一条纪律：**不碰用户的库**。`MINDMAP_DB_PATH` 指到 `data/generation_strategy.db`，
开跑前删、跑完留着（报告要能复现；要清掉就自己删那个文件）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DB = BACKEND / "data" / "generation_strategy.db"

#: 本机代理。实测（用户那一轮）走它维基 / openalex / crossref 都是 200，直连超时。
PROXY = "http://127.0.0.1:7897"

#: 材料的快照，**在切库之前**读好（见 `_snapshot_library` 里那段）。
_LIBRARY: list = []

#: 与 `run-backend.bat` 同一批键（那里设的、用户级环境变量里也有一份）。
CREDENTIALS = (
    "MINDMAP_LLM_MODE", "MINDMAP_LLM_BASE_URL", "MINDMAP_LLM_API_KEY",
    "MINDMAP_LLM_MODEL", "MINDMAP_EMBEDDING_BASE_URL", "MINDMAP_EMBEDDING_API_KEY",
    "MINDMAP_EMBEDDING_PROTOCOL", "MINDMAP_EMBEDDING_MODEL", "MINDMAP_EMBEDDING_DIM",
)


def _user_env(name: str) -> str:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(value)


def _prepare() -> None:
    for name in CREDENTIALS:
        if not os.environ.get(name):
            value = _user_env(name)
            if value:
                os.environ[name] = value
    # 与运行中的后端同一套开关（用户给的那张表）。逐个显式设，不读 .env——
    # 实测要能复现，而"这次到底开没开"必须在这份文件里读得到。
    for name in (
        "MINDMAP_SECTION_SLICE", "MINDMAP_GRAPH_INGEST", "MINDMAP_GRAPH_OUTLINE",
        "MINDMAP_DETAIL_RETRIEVAL", "MINDMAP_NODE_SCALE", "MINDMAP_DETAIL_SCALE",
        "MINDMAP_MIXED_SKELETON", "MINDMAP_VERIFY_AFTER_GENERATE",
        "MINDMAP_OPTIMIZATION_CROSS_MAP", "MINDMAP_EXTERNAL_DEEPSEEK_SEARCH",
    ):
        os.environ[name] = "1"
    # 本机代理（用户实测：走它 维基 200 / openalex 200 / crossref 200，直连超时）。
    # 纯主题那一批的核对（`MINDMAP_VERIFY_AFTER_GENERATE`）与外部检索都要外网，
    # 直连会静默降级成"搜到 0 篇"，那样 B+C 里的 C 看起来就像没生效。
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ[name] = PROXY


def _snapshot_library() -> None:
    """把**用户真实库**里的材料读进内存，然后才把库切到本脚本的临时文件。

    顺序不能反：`MINDMAP_DB_PATH` 一改，`library.recent()` 读到的就是那个空的临时库
    （实测第一次跑就是这么得到 0 份材料的）。材料一旦读进内存，后面全链路用的都是
    `ctx.sources` 里的 `Source` 对象，检索也直接打这些正文——不再回头读库。
    """
    global _LIBRARY
    from app.knowledge import library as knowledge_library

    _LIBRARY = knowledge_library.recent(limit=400)
    os.environ["MINDMAP_DB_PATH"] = str(DB)


def _load_batch(batch: str) -> list:
    """从快照里挑这一批的材料。"""
    everything = _LIBRARY
    if batch == "thin":
        picked = [item for item in everything if "碳中和" in (item.filename or "")]
        return picked[:4]
    if batch == "thick":
        # docs 那四份，合计 66,536 字（26265 + 20017 + 14120 + 6134）——超过
        # `FULL_TEXT_MATERIAL_CHARS=50_000`，所以这一批走**按支检索**那条引用规约
        # （`BRANCHED_EVIDENCE_RULES`），而不是全塞。这一点必须在报告里说清：
        # 两批走的不是同一段提示词。
        wanted = {"AGENT_PLAN.md", "PLAN.md", "RAG-DECISIONS.md", "ARCHITECTURE.md"}
        return [item for item in everything if (item.filename or "") in wanted]
    return []


def _fanout(root) -> Counter:
    """每个节点的子节点个数 → 计数。扇出 0 = 叶子。"""
    out: Counter = Counter()
    stack = [root]
    while stack:
        node = stack.pop()
        out[len(node.children)] += 1
        stack.extend(node.children)
    return out


def _count_refs(root) -> dict[str, int]:
    """引用覆盖率，**两个分母**。

    `node_count` 那个分母（全部节点）会把结构层稀释掉：骨架/章节那一层**结构上不写
    说明、因此不可能有引用**（早前实测过），它们进分母等于给一条与检索无关的常量打折。
    所以另给一个**可引用**分母：

        可引用节点 = `detail` 非空 的节点

    引用是给"说明"这一段挂的出处（`source_refs` 挂在节点上、`detail` 是它支撑的那段
    文字），没有说明的节点没有可引用的内容。这与"每个节点都必须有引用"那条规约要分清：
    规约要求的是"写了说明就要说出处"，它没说"结构层也要写出处"。
    """
    total = with_refs = citable = citable_with_refs = 0
    stack = [root]
    while stack:
        node = stack.pop()
        total += 1
        has_refs = bool(node.source_refs)
        with_refs += 1 if has_refs else 0
        if (node.detail or "").strip():
            citable += 1
            citable_with_refs += 1 if has_refs else 0
        stack.extend(node.children)
    return {
        "node_count": total,
        "with_refs": with_refs,
        "citable": citable,
        "citable_with_refs": citable_with_refs,
        "ref_coverage": round(with_refs / max(1, total), 3),
        "citable_coverage": round(citable_with_refs / max(1, citable), 3),
    }


def _by_depth(root) -> dict[int, dict]:
    """逐层的 (节点数, 有引用数, 可引用数)。

    这一列是"每个节点必须有引用"那条规则**会不会惩罚深度**的唯一证据：深层节点离原文
    更远，逐字引更难。如果第 3 层以下的有引用率断崖，那这条规则实际上在惩罚深度。
    「可引用」与 `_count_refs` 同口径（`detail` 非空）——两个口径必须同源，否则
    "总数涨了但覆盖率跌了"会无法归因。
    """
    out: dict[int, dict] = {}
    stack: list[tuple[object, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        bucket = out.setdefault(depth, {"nodes": 0, "with_refs": 0, "citable": 0,
                                        "citable_with_refs": 0})
        bucket["nodes"] += 1
        bucket["with_refs"] += 1 if node.source_refs else 0
        if (node.detail or "").strip():
            bucket["citable"] += 1
            bucket["citable_with_refs"] += 1 if node.source_refs else 0
        stack.extend((child, depth + 1) for child in node.children)
    for bucket in out.values():
        bucket["coverage"] = round(bucket["with_refs"] / max(1, bucket["nodes"]), 3)
        bucket["citable_coverage"] = round(
            bucket["citable_with_refs"] / max(1, bucket["citable"]), 3)
    return dict(sorted(out.items()))


def _rollup_samples(root) -> list[dict]:
    """产出里的聚合节点：label 是「其他 N 项」的那些。"""
    found = []
    stack = [root]
    while stack:
        node = stack.pop()
        if (node.label or "").startswith("其他 ") and (node.label or "").endswith(" 项"):
            found.append({
                "label": node.label,
                "detail": (node.detail or "")[:120],
                "refs": len(node.source_refs),
                "expandable": node.expandable,
            })
        stack.extend(node.children)
    return found


def _run_once(batch: str, index: int) -> dict:
    from app.agent import tools as agent_tools
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall

    sources = _load_batch(batch)
    topic = {
        "thin": "碳中和的主要方面",
        "thick": "生成流水线的设计与实测",
        "topic": "机器学习的主要方向",
    }[batch]
    request = AgentRunRequest(
        goal=topic, intent="create", thinking_model="default", sources=sources,
        budget=RunBudget(max_model_calls=60, max_tool_calls=8, max_seconds=900.0,
                         max_output_tokens=32768, max_repairs=1),
    )
    ctx = RunContext(request=request,
                     sources_by_id={source.id: source for source in sources})
    registry = agent_tools.build_registry(
        "create", has_sources=bool(sources), has_library=bool(sources))
    started = time.perf_counter()
    result = asyncio.run(registry.execute(
        ToolCall(call_id=f"m{index}", name="mindmap_generate",
                 arguments={"topic": topic, "diagram_type": "tree"}), ctx))
    seconds = round(time.perf_counter() - started, 1)

    row: dict = {
        "batch": batch, "run": index, "ok": bool(result.ok),
        "seconds": seconds,
        "material_chars": sum(len(item.text or "") for item in sources),
        "sources": len(sources),
    }
    if not result.ok:
        row["error"] = result.content[:300]
        return row
    tree = ctx.candidate
    if tree is None:
        row["ok"] = False
        row["error"] = "工具返回 ok 但没有产出树"
        return row
    root = tree.root
    stats = _count_refs(root)
    data = result.data or {}
    fanout = _fanout(root)
    row.update({
        "node_count": stats["node_count"],
        "branches": len(root.children),
        "with_refs": stats["with_refs"],
        "ref_coverage": stats["ref_coverage"],
        "citable": stats["citable"],
        "citable_with_refs": stats["citable_with_refs"],
        "citable_coverage": stats["citable_coverage"],
        "refs_total": data.get("source_refs", {}).get("kept"),
        "fanout": dict(sorted(fanout.items())),
        "by_depth": _by_depth(root),
        "forked_parents": sum(count for size, count in fanout.items() if size >= 2),
        "max_depth": _max_depth(root),
        "folded_rollups": data.get("folded_rollups", 0),
        "folded_nodes": data.get("folded_nodes", 0),
        "cut_nodes": data.get("cut_nodes", 0),
        "node_limit": data.get("node_limit"),
        "partial": data.get("partial"),
        "rollups_in_tree": _rollup_samples(root),
        "verify": data.get("verify"),
        "diagnostic_codes": [item["code"] for item in data.get("diagnostics", [])],
        "summary": (result.content or "")[:400].replace("\n", " "),
    })
    return row


def _max_depth(root, depth: int = 1) -> int:
    if not root.children:
        return depth
    return max(_max_depth(child, depth + 1) for child in root.children)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", default="all",
                        choices=("thin", "thick", "topic", "all"))
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--topic", default="", help="覆盖纯主题那一批的主题")
    parser.add_argument(
        "--keep", default="on", choices=("on", "off"),
        help=("按分数取数（`tools.KEEP_BY_SCORE_ENV`）的 A/B 臂。`off` = 回到"
              "\"每支取排序后前 3 条\"那个旧口径。两臂唯一的差别就是这一个环境变量，"
              "所以两行报告可以直接相减（见 `docs/RETRIEVAL-KEEP.md` 第 4 节）"))
    parser.add_argument(
        "--no-external", action="store_true",
        help=("关掉外部检索（`MINDMAP_EXTERNAL_DEEPSEEK_SEARCH`）。纯主题那一批**必须**"
              "带这个开关才测得到 B：外部检索会先把'没有材料'变成'8 份 / 22,744 字'，"
              "于是 `sources` 非空、`DETAIL_NO_SOURCE_RULES` 那句根本不进提示词"))
    args = parser.parse_args()

    _prepare()
    from app.agent.tools import KEEP_BY_SCORE_ENV

    os.environ[KEEP_BY_SCORE_ENV] = "1" if args.keep == "on" else "0"
    if args.no_external:
        os.environ["MINDMAP_EXTERNAL_DEEPSEEK_SEARCH"] = ""
    if not os.environ.get("MINDMAP_LLM_API_KEY"):
        print("缺 MINDMAP_LLM_API_KEY（用户级环境变量里也没有）", file=sys.stderr)
        return 2
    DB.unlink(missing_ok=True)
    _snapshot_library()
    print(f"材料快照 {len(_LIBRARY)} 份", flush=True)

    batches = ("thin", "thick", "topic") if args.batch == "all" else (args.batch,)
    rows: list[dict] = []
    for batch in batches:
        for index in range(1, args.repeat + 1):
            print(f"… {batch} #{index}", flush=True)
            try:
                row = _run_once(batch, index)
            except Exception as exc:                    # noqa: BLE001 — 一批失败不该中断报告
                row = {"batch": batch, "run": index, "ok": False,
                       "error": f"{type(exc).__name__}: {exc}"}
            rows.append(row)
            print(json.dumps({key: row.get(key) for key in (
                "ok", "seconds", "node_count", "branches", "ref_coverage",
                "citable", "citable_coverage", "folded_rollups", "cut_nodes")},
                ensure_ascii=False), flush=True)

    out = BACKEND / "data" / f"generation-strategy-keep-{args.keep}.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细：{out}")

    print("\n批次    次  节点  分支  有引用/总   覆盖率  可引用  可引用覆盖率  裁剪  耗时")
    for row in rows:
        if not row.get("ok"):
            print(f"{row['batch']:<6} {row['run']:>2}  FAILED  {row.get('error', '')[:60]}")
            continue
        print(f"{row['batch']:<6} {row['run']:>2} {row['node_count']:>5} "
              f"{row['branches']:>5} {row['with_refs']:>4}/{row['node_count']:<4} "
              f"{row['ref_coverage']:>6.1%} {row['citable']:>6} "
              f"{row['citable_coverage']:>13.1%} {row['cut_nodes']:>5} "
              f"{row['seconds']:>5}s")

    _report_aggregate(rows, args.keep)
    return 0


def _report_aggregate(rows: list[dict], keep: str) -> None:
    """多轮汇总：**均值与极差**，不是单次。

    判据要提前定死（`docs/RETRIEVAL-KEEP.md` §4.1）：`repeat=1` 时两臂的差里混着"这次
    树长得大不大"，`repeat=3` 之后看两件事——

      1. 可引用覆盖率的均值差还在不在（厚批那 +5.9pp）；
      2. 两臂各自的极差有多大。**极差大于均值差**时，这个差别不该归因于改动。
    """
    ok = [row for row in rows if row.get("ok")]
    if not ok:
        print("\n没有一次成功，无法汇总")
        return
    print(f"\n汇总（keep={keep}，每批 {len(ok)} 轮有效）")
    print(f"{'批次':<8}{'可引用覆盖率 均值':>18}{'极差':>10}{'全部节点覆盖率 均值':>20}"
          f"{'极差':>10}{'节点数 均值':>12}{'极差':>8}")
    batches = sorted({row["batch"] for row in ok})
    for batch in batches:
        group = [row for row in ok if row["batch"] == batch]
        cite = [row.get("citable_coverage", 0.0) for row in group]
        cover = [row.get("ref_coverage", 0.0) for row in group]
        nodes = [row.get("node_count", 0) for row in group]
        print(f"{batch:<8}{statistics.mean(cite):>17.1%}"
              f"{max(cite) - min(cite):>10.1%}"
              f"{statistics.mean(cover):>19.1%}{max(cover) - min(cover):>10.1%}"
              f"{statistics.mean(nodes):>12.0f}{max(nodes) - min(nodes):>8}")


if __name__ == "__main__":
    raise SystemExit(main())
