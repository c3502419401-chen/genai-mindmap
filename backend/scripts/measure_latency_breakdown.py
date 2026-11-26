"""量一次生成的**分段**耗时：同一主题、可切开关，输出"哪一段花了几秒"。

为什么需要它：`data.calls` / `data.retrieval` / `data.external` / `data.detail`
里本来就记着各段秒数（`_generate` 的账），但那个 `data` 只活在一次 SSE 的
`tool_end` 帧里——`runs` 表不存它，日志也没有一行汇总。于是"为什么一次生成要
两分钟"这个问题从来没有可复算的答案。

这个脚本直接把工具跑在进程内，打印一张表；开关走命令行，所以同一份代码能在
不同组合之间对比（组合与 `run-backend.bat` 对齐，缺省就是生产那套）。

用法：
    python scripts/measure_latency_breakdown.py                    # 生产组合
    python scripts/measure_latency_breakdown.py --no-external      # 关外部检索
    python scripts/measure_latency_breakdown.py --no-graph --no-cross-map

**它不经过 HTTP、不占游客配额**（直接调工具），但**会真的花模型额度**：一次
约 1–3 分钟、十几万 token 输入。跑之前先想清楚要对比哪两组。
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
    "MINDMAP_LLM_MODEL", "MINDMAP_LLM_MODEL_DEEP",
    "MINDMAP_EMBEDDING_BASE_URL", "MINDMAP_EMBEDDING_API_KEY",
    "MINDMAP_EMBEDDING_PROTOCOL", "MINDMAP_EMBEDDING_MODEL", "MINDMAP_EMBEDDING_DIM",
)

#: `run-backend.bat` 里那套生产组合。脚本缺省复刻它，不比不知道"关掉省多少"。
PRODUCTION = {
    "MINDMAP_EXTERNAL_KNOWLEDGE": "1",
    "MINDMAP_EXTERNAL_DEEPSEEK_SEARCH": "1",
    "MINDMAP_SECTION_SLICE": "1",
    "MINDMAP_GRAPH_INGEST": "1",
    "MINDMAP_GRAPH_EXTRACTION": "1",
    "MINDMAP_DETAIL_RETRIEVAL": "1",
    "MINDMAP_NODE_SCALE": "1",
    "MINDMAP_DETAIL_SCALE": "1",
    "MINDMAP_DETAIL_LENGTH": "free",
    "MINDMAP_OPTIMIZATION_CROSS_MAP": "1",
    "MINDMAP_VERIFY_AFTER_GENERATE": "0",
    "MINDMAP_RERANK": "1",
    "MINDMAP_PROFILE_RERANK": "1",
    "MINDMAP_LLM_MODEL": "deepseek-chat",
    "MINDMAP_LLM_MODEL_DEEP": "deepseek-reasoner",
}

#: 图谱那两档（入图 / 图谱骨架）要 Neo4j 凭据。少了它们，`GRAPH_INGEST=1` 与
#: `=0` 在这一进程里完全一样——那样量出来的"关掉省几秒"是假的。
NEO4J = ("MINDMAP_NEO4J_URI", "MINDMAP_NEO4J_USER", "MINDMAP_NEO4J_PASSWORD")


def _user_env(name: str) -> str:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(value)


def _fill_credentials() -> None:
    """凭据优先取用户环境变量，其次取 `run-backend.bat` 里那份。

    后端在跑（`run-backend.bat` 起的那个进程）时，本机没有第二个地方存着这些
    值——用户级环境变量里通常只有一部分。启动脚本里的 `set X=...` 是最接近
    "生产那一套"的副本，读它比让调用方手抄一遍更不容易错。
    """
    missing = [name for name in (*CREDENTIALS, *NEO4J) if not os.environ.get(name)]
    if not missing:
        return
    for name in list(missing):
        value = _user_env(name)
        if value:
            os.environ[name] = value
            missing.remove(name)
    if not missing:
        return
    bat = BACKEND.parent / "run-backend.bat"
    if not bat.exists():
        return
    for line in bat.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text.lower().startswith("set "):
            continue
        key, _, raw = text[4:].partition("=")
        key = key.strip()
        if key in missing and raw.strip():
            os.environ[key] = raw.strip()


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", default="碳中和的主要方面")
    parser.add_argument("--branches", type=int, default=5)
    parser.add_argument("--no-external", action="store_true", help="关外部检索（联网那一段）")
    parser.add_argument("--no-deepseek-search", action="store_true",
                        help="只关 DeepSeek 那第六个外部源")
    parser.add_argument("--no-graph", action="store_true", help="关外部材料入图")
    parser.add_argument("--no-section", action="store_true", help="关按章切")
    parser.add_argument("--no-detail-retrieval", action="store_true", help="关说明阶段的逐节点检索")
    parser.add_argument("--no-scale", action="store_true", help="关节点数随材料放大")
    parser.add_argument("--no-cross-map", action="store_true", help="关跨图参考建议")
    parser.add_argument("--label", default="", help="这一组的名字，进表头")
    parser.add_argument("--out", default="", help="把这一组的结果同时写进这个 json 文件")
    args = parser.parse_args(argv)

    for key, value in PRODUCTION.items():
        os.environ.setdefault(key, value)
    _fill_credentials()
    if not os.environ.get("MINDMAP_LLM_API_KEY"):
        print("没有模型凭据：先跑一次 run-backend.bat，或设 MINDMAP_LLM_API_KEY", file=sys.stderr)
        return 2
    os.environ.setdefault("MINDMAP_DB_PATH", str(BACKEND / "data" / "mindmaps.db"))

    if args.no_external:
        os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "0"
    if args.no_deepseek_search:
        os.environ["MINDMAP_EXTERNAL_DEEPSEEK_SEARCH"] = "0"
    if args.no_graph:
        os.environ["MINDMAP_GRAPH_INGEST"] = "0"
    if args.no_section:
        os.environ["MINDMAP_SECTION_SLICE"] = "0"
    if args.no_detail_retrieval:
        os.environ["MINDMAP_DETAIL_RETRIEVAL"] = "0"
    if args.no_scale:
        os.environ["MINDMAP_NODE_SCALE"] = "0"
    if args.no_cross_map:
        os.environ["MINDMAP_OPTIMIZATION_CROSS_MAP"] = "0"

    # 所有开关都设好之后才能 import：`tools` 的常量是模块级读 env 的。
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
    from app.agent.tools import build_registry, library_available

    topic = args.topic
    request = AgentRunRequest(goal=topic, intent="create", thinking_model="default",
                              budget=RunBudget(max_model_calls=40, max_tool_calls=8,
                                               max_seconds=600.0, max_output_tokens=65536,
                                               max_repairs=1))
    ctx = RunContext(request=request)
    registry = build_registry("create", has_sources=False, has_library=library_available())
    call = ToolCall(call_id="measure", name="mindmap_generate",
                    arguments={"topic": topic, "branches": args.branches})

    started = time.perf_counter()
    result = await registry.execute(call, ctx)
    elapsed = time.perf_counter() - started
    if not result.ok or ctx.candidate is None:
        print(json.dumps({"ok": False, "reason": result.content,
                          "seconds": round(elapsed, 2)}, ensure_ascii=False))
        return 1

    data = result.data or {}
    nodes = list(_walk(ctx.candidate.root))
    details = [len((node.detail or "").strip()) for node in nodes[1:]]
    row = {
        "label": args.label or _label_of(args),
        "seconds_total": round(elapsed, 1),
        "calls": data.get("calls"),
        "retrieval": (data.get("retrieval") or {}).get("seconds"),
        "retrieval_hits": (data.get("retrieval") or {}).get("hits"),
        "external": (data.get("external") or {}).get("seconds"),
        "external_sources": (data.get("external") or {}).get("sources"),
        "external_cached": (data.get("external") or {}).get("cached"),
        "graph_ingest": data.get("graph_ingest"),
        "input": (data.get("input") or {}).get("seconds"),
        "detail": {
            "seconds": (data.get("detail") or {}).get("seconds"),
            "halves": (data.get("detail") or {}).get("halves"),
            "filled": (data.get("detail") or {}).get("filled"),
            "short_nodes": (data.get("detail") or {}).get("short_nodes"),
        },
        "nodes": len(nodes),
        "depth": _depth(ctx.candidate.root),
        "detail_avg": round(sum(details) / len(details), 1) if details else 0,
        "detail_min": min(details) if details else 0,
        "diagnostics": [item.get("code") for item in data.get("diagnostics") or []],
        # 入图那一步的秒数只写在诊断的 message 里（`_graph_ingest_diagnostic`），
        # `data` 没有独立字段——要回答"那 17.5 秒现在还在吗"，就得把这句话留住。
        "diagnostic_messages": [item.get("message") for item in data.get("diagnostics") or []
                                if item.get("code", "").startswith("GRAPH")],
        "model_calls": ctx.model_calls_used,
        "switches": {key: os.environ.get(key) for key in PRODUCTION},
    }
    print(json.dumps(row, ensure_ascii=False, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps(row, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
    return 0


def _depth(root) -> int:
    best = 1

    def walk(node, depth: int) -> None:
        nonlocal best
        best = max(best, depth)
        for child in node.children:
            walk(child, depth + 1)

    walk(root, 1)
    return best


def _label_of(args: argparse.Namespace) -> str:
    tags = [name for name in ("no_external", "no_deepseek_search", "no_graph", "no_section",
                              "no_detail_retrieval", "no_scale", "no_cross_map")
            if getattr(args, name)]
    return "+".join(tags) if tags else "production"


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
