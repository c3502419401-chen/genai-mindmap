"""量「用户那段话有没有被当成主题」：五条真实长输入，各跑一次真生成。

用法：
    python scripts/measure_input_extract.py                # 当前开关状态跑五条
    python scripts/measure_input_extract.py --arm off      # 强制关（基线）
    python scripts/measure_input_extract.py --arm on       # 强制开（MINDMAP_INPUT_EXTRACT=1）

为什么走 `service.run` 而不是直接调工具：要量的第一列是 **`args.topic` 到底是什么**
——它是外层 agent 模型从用户原话里填出来的参数，可能已经被压缩过。直接调工具只能
把原话硬塞进 `topic`，那量到的是"最坏情况"，不是现状。这里用 spy 把真实参数记下来。

输出是一张表（也落一份 JSON，给 A/B 对照脚本读）：
    goal → args.topic → 根节点 label → 一级分支 → 节点数 / 耗时

口径（与 run-backend.bat 的差别只有一条）：**外部检索关**（`MINDMAP_EXTERNAL_KNOWLEDGE=0`）。
开着的话，一段长文本会先被拿去搜网，素材进来之后根节点由材料决定，那条现象就被
遮住了——这一轮要看的是"纯主题路径上那句话怎么进树"，所以两臂都关。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
os.chdir(BACKEND)
sys.path.insert(0, BACKEND)

# 与 run-backend.bat 同一套
os.environ.setdefault("MINDMAP_LLM_MODE", "model")
os.environ.setdefault("MINDMAP_LLM_BASE_URL", "https://api.deepseek.com")
os.environ.setdefault("MINDMAP_LLM_API_KEY", "sk-7961cf33c9c94110b749e800adfb067d")
os.environ.setdefault("MINDMAP_LLM_MODEL", "deepseek-chat")
os.environ.setdefault("MINDMAP_LLM_MODEL_DEEP", "deepseek-reasoner")
os.environ.setdefault("MINDMAP_RERANK", "0")
os.environ.setdefault("MINDMAP_PROFILE_RERANK", "0")
os.environ.setdefault("MINDMAP_SECTION_SLICE", "1")
os.environ.setdefault("MINDMAP_GRAPH_INGEST", "0")
os.environ.setdefault("MINDMAP_GRAPH_OUTLINE", "0")
os.environ.setdefault("MINDMAP_DETAIL_RETRIEVAL", "1")
os.environ.setdefault("MINDMAP_NODE_SCALE", "1")
os.environ.setdefault("MINDMAP_DETAIL_LENGTH", "free")
os.environ.setdefault("MINDMAP_VERIFY_AFTER_GENERATE", "0")
os.environ.setdefault("MINDMAP_EXTERNAL_KNOWLEDGE", "0")

# 副本库：读真库的画像/记忆表，写入落在临时文件上
REAL_DB = os.path.join(BACKEND, "data", "mindmaps.db")
COPY_DB = os.path.join(tempfile.gettempdir(), "input-extract-measure.db")
if os.path.exists(REAL_DB):
    shutil.copyfile(REAL_DB, COPY_DB)
os.environ["MINDMAP_DB_PATH"] = COPY_DB

from app.agent import service  # noqa: E402
from app.agent.contracts import AgentRunRequest, RunBudget  # noqa: E402
from app.agent.provider import default_provider  # noqa: E402
from app.agent.tools import build_registry  # noqa: E402

CASES: list[tuple[str, str]] = [
    ("①带背景的诉求", "我想了解怎么在制造业团队里推行AI工具，公司300人，担心员工抵触"),
    ("②多问题混合", "帮我梳理一下RAG的召回问题，另外我们的向量库选型也没定，还有成本怎么控"),
    ("③带约束的任务", "下周要给老板讲清楚为什么我们的检索效果差，要有数据支撑，别太技术"),
    ("④几乎不成句", "AI学习 怎么学 路线"),
    ("⑤纯主题（对照）", "机器学习的主要方向"),
]


async def one(label: str, goal: str, *, budget_calls: int = 40) -> dict:
    """跑一条：返回这一条的全部读数。"""
    registry = build_registry("create", has_sources=False, has_library=False)
    calls: list[dict] = []
    reports: list[dict] = []
    execute = registry.execute

    async def spy(call, ctx):
        calls.append({"name": call.name, "arguments": dict(call.arguments)})
        result = await execute(call, ctx)
        data = result.data or {}
        reports.append({
            "node_count": data.get("node_count"),
            "calls": data.get("calls"),
            "input": data.get("input"),
            "diagnostics": [item.get("code") for item in data.get("diagnostics") or []],
        })
        return result

    registry.execute = spy  # type: ignore[method-assign]

    request = AgentRunRequest(
        goal=goal,
        intent="create",
        thinking_model="default",
        budget=RunBudget(max_model_calls=budget_calls, max_tool_calls=8,
                         max_seconds=600.0, max_output_tokens=8192, max_repairs=1),
    )
    holder: dict = {}

    def on_close(content) -> None:
        holder["content"] = content

    started = time.perf_counter()
    errors: list[str] = []
    stages: list[tuple[float, str, str]] = []
    usage: dict = {}
    try:
        async for event in service.run(
            request, provider=default_provider(), tools=registry, on_close=on_close,
        ):
            name = type(event).__name__
            if name == "StageEvent":
                stages.append((round(time.perf_counter() - started, 2),
                               getattr(event, "stage", ""), getattr(event, "detail", "")))
            elif name == "DoneEvent":
                usage = dict(getattr(event, "usage", None) or {})
            elif name in ("ErrorEvent", "FailedEvent"):
                errors.append(str(getattr(event, "code", "")) + ": "
                              + str(getattr(event, "message", ""))[:300])
    except Exception as exc:  # noqa: BLE001 — 脚本要留下失败原因，而不是整批断掉
        errors.append(f"{type(exc).__name__}: {exc}"[:300])
    total = time.perf_counter() - started

    content = holder.get("content")
    root_label, branches, nodes = None, [], 0
    if content is not None:
        nodes = sum(1 for _ in _walk(content))
        root = getattr(content, "root", None)
        if root is not None:
            root_label = root.label
            branches = [child.label for child in root.children]
        else:
            # 鱼骨 / 流程图：没有 root，一级就是最外面那一层
            for attr in ("bones", "stages"):
                items = getattr(content, attr, None) or []
                if items:
                    branches = [getattr(item, "label", str(item)) for item in items]
                    break

    generate_args = [call["arguments"] for call in calls if call["name"] == "mindmap_generate"]
    topic = (generate_args[0].get("topic") if generate_args else None)
    report = reports[0] if reports else {}
    return {
        "case": label,
        "goal": goal,
        "goal_chars": len(goal),
        "topic": topic,
        "topic_chars": len(topic) if isinstance(topic, str) else None,
        "root_label": root_label,
        "root_chars": len(root_label) if isinstance(root_label, str) else None,
        "branches": branches,
        "nodes": nodes,
        "seconds": round(total, 1),
        "stages": stages,
        "calls": calls,
        "input": report.get("input"),
        "step_seconds": report.get("calls"),
        "usage": usage,
        "diagnostics": report.get("diagnostics"),
        "errors": errors,
    }


def _walk(content):
    from app.agent import shapes

    yield from shapes.iter_nodes(content)


def _print_table(rows: list[dict], arm: str) -> None:
    print(f"\n{'=' * 100}\n臂：{arm}\n{'=' * 100}")
    header = f"{'case':<14}{'原文':>5}{'args.topic':>10}{'根节点':>8}{'一级分支':>6}{'节点':>5}{'秒':>7}"
    print(header)
    print("-" * 100)
    for row in rows:
        print(f"{row['case']:<14}{row['goal_chars']:>5}{row['topic_chars'] or 0:>10}"
              f"{row['root_chars'] or 0:>8}{len(row['branches']):>6}{row['nodes']:>5}"
              f"{row['seconds']:>7.1f}")
    print()
    for row in rows:
        print(f"--- {row['case']}（原文 {row['goal_chars']} 字）")
        print(f"    原文      : {row['goal']}")
        print(f"    args.topic: {row['topic']}")
        print(f"    根节点    : {row['root_label']}")
        print(f"    一级分支  : {' / '.join(row['branches'])}")
        shape = row.get("input")
        if shape:
            print(f"    提取      : source={shape.get('source')} skip={shape.get('skip') or '（无）'}"
                  f" {shape.get('seconds')}s → topic={shape.get('topic')!r}")
            print(f"                 intent={shape.get('intent')!r} "
                  f"questions={shape.get('questions')} constraints={shape.get('constraints')}")
        if row.get("usage"):
            print(f"    usage     : {row['usage']}")
        if row["errors"]:
            print(f"    错误      : {row['errors']}")
        others = [call["name"] for call in row["calls"] if call["name"] != "mindmap_generate"]
        if others:
            print(f"    其它工具  : {others}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=["on", "off", "default"], default="default")
    parser.add_argument("--only", default="", help="只跑 case 标签含这个子串的那几条")
    parser.add_argument("--out", default="", help="JSON 输出路径")
    args = parser.parse_args()

    if args.arm == "on":
        os.environ["MINDMAP_INPUT_EXTRACT"] = "1"
    elif args.arm == "off":
        os.environ.pop("MINDMAP_INPUT_EXTRACT", None)

    cases = [item for item in CASES if args.only in item[0]] if args.only else CASES
    rows = []
    for label, goal in cases:
        print(f"[{label}] 跑：{goal[:40]}…", flush=True)
        row = await one(label, goal)
        rows.append(row)
        print(f"    → topic={row['topic']!r} root={row['root_label']!r} "
              f"分支={len(row['branches'])} 节点={row['nodes']} {row['seconds']}s", flush=True)

    arm = os.environ.get("MINDMAP_INPUT_EXTRACT", "（未设）")
    _print_table(rows, f"MINDMAP_INPUT_EXTRACT={arm}")

    out = args.out or os.path.join(
        HERE, f"_input_extract_{args.arm}.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2)
    print(f"\nJSON → {out}")


if __name__ == "__main__":
    asyncio.run(main())
