"""A/B：生成时主动查库（`MINDMAP_LIBRARY_IN_GENERATE`）开与关，同一主题的产出差别。

**两条臂之间只有一个变量**：`_intake` 查不查库。为了做到这一点，三样东西都按住：

  - 外部检索关掉（`MINDMAP_EXTERNAL_KNOWLEDGE=0`）：它默认开、一次 4–6 秒联网，会把
    "库补了什么"整个盖住。关掉之后两条臂的 `sources` 都是空的，唯一差别就是库。
  - 不给 `rag_query`（`has_library=False`）：它是模型**自己**想起来调的库入口，留着
    它等于让两条臂都可能用上库，A/B 就不成立了。
  - 生成后核对（`MINDMAP_VERIFY_AFTER_GENERATE`）关掉：它多一次检索 + 一次模型调用，
    与库无关，只会让耗时对比变噪声。

## 报什么

| 列 | 为什么是它 |
|---|---|
| 一级分支 / 节点总数 | 库片段会把树的规模推向"有多少证据写多少"，这是主要的形态代价 |
| 有引用的节点 / 覆盖率 | 库参与买的就是这个数；买不到就白花钱 |
| 引用条数 / 被引来源份数 | 区分"一棵引用密集但只靠一份来源的树"与"多来源的树" |
| 墙钟 | 库检索 + rerank 是固定成本（实测 0.9–1.4 秒），要能看见 |
| 诊断码 | `LIBRARY_SUPPLEMENT` 说明这次真用了库；`..._SKIPPED` 说明为什么没用 |

## 用法（在 `backend/` 下，需要 `MINDMAP_LLM_*` 与 embedding / rerank 凭据）

    python scripts/measure_library_in_generate_ab.py
    python scripts/measure_library_in_generate_ab.py --topics "城市雨污分流改造"

每次运行**真花模型额度**：一个主题两次生成 = 约 6 次模型调用（骨架 + 结构 + 说明×2）。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall  # noqa: E402
from app.agent.tools import (  # noqa: E402
    LIBRARY_IN_GENERATE_ENV,
    build_registry,
)

TOPICS = [
    # 库里有 2 份精确相关的摘要（openalex 雨污分流 ×2，各 200 余字）
    "城市雨污分流改造",
    # 库里有 2 份科普网页（同一篇的两个抓取），内容讲机器学习是什么
    "机器学习的主要方向",
    # 负样本：库里没有任何一份与它相关（红烧肉的命中全是开发文档）
    "如何做红烧肉",
]


def _request(topic: str) -> AgentRunRequest:
    return AgentRunRequest(
        goal=topic, intent="create", thinking_model="default", sources=[],
        budget=RunBudget(max_model_calls=20, max_tool_calls=8,
                         max_seconds=180.0, max_output_tokens=8192, max_repairs=1),
    )


def _ctx(topic: str) -> RunContext:
    return RunContext(request=_request(topic), sources_by_id={})


def _walk(node: Any) -> list[Any]:
    stack, out = [node], []
    while stack:
        current = stack.pop()
        out.append(current)
        stack.extend(current.children)
    return out


def _stats(ctx: RunContext, seconds: float, result: Any) -> dict[str, Any]:
    content = ctx.candidate
    if content is None:
        return {"ok": False, "seconds": seconds,
                "diagnostics": _codes(result), "error": result.content[:200]}
    nodes = _walk(content.root)
    cited = [node for node in nodes if node.source_refs]
    refs = [ref for node in nodes for ref in node.source_refs]
    return {
        "ok": True,
        "seconds": seconds,
        "branches": len(content.root.children),
        "nodes": len(nodes),
        "cited_nodes": len(cited),
        "coverage": (len(cited) / len(nodes)) if nodes else 0.0,
        "refs": len(refs),
        "sources": len({ref.source_id for ref in refs}),
        "usage": [(item.kind, item.char_count) for item in content.source_usage],
        "diagnostics": _codes(result),
        "detail_chars": (
            sum(len(node.detail or "") for node in nodes) / max(len(nodes), 1)),
    }


def _codes(result: Any) -> list[str]:
    data = getattr(result, "data", None) or {}
    return [item.get("code") for item in data.get("diagnostics", []) or []]


async def _run(topic: str) -> dict[str, Any]:
    ctx = _ctx(topic)
    registry = build_registry("create", has_sources=False, has_library=False)
    started = time.perf_counter()
    result = await registry.execute(
        ToolCall(call_id="c-gen", name="mindmap_generate",
                 arguments={"topic": topic}), ctx)
    return _stats(ctx, time.perf_counter() - started, result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topics", default="")
    args = parser.parse_args()
    topics = ([item.strip() for item in args.topics.split(",") if item.strip()]
              if args.topics else TOPICS)

    os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "0"
    os.environ["MINDMAP_VERIFY_AFTER_GENERATE"] = "0"
    os.environ["MINDMAP_RERANK"] = "1"

    print(f"库：{os.getenv('MINDMAP_DB_PATH') or '(默认)'}")
    print(f"模型：{os.getenv('MINDMAP_LLM_MODEL')} / 重排：{os.getenv('MINDMAP_RERANK')}")
    header = (f"\n{'主题':16}{'库':4}{'秒':>7}{'分支':>5}{'节点':>5}"
              f"{'有引用':>6}{'覆盖率':>8}{'引用':>5}{'来源':>5}{'说明字数':>8}")
    print(header)
    print("-" * len(header))
    for topic in topics:
        for enabled in (False, True):
            os.environ[LIBRARY_IN_GENERATE_ENV] = "1" if enabled else "0"
            stats = asyncio.run(_run(topic))
            if not stats.get("ok"):
                print(f"{topic[:14]:16}{'开' if enabled else '关':4}"
                      f"  失败：{stats.get('error')}")
                continue
            print(f"{topic[:14]:16}{'开' if enabled else '关':4}"
                  f"{stats['seconds']:7.1f}{stats['branches']:5}{stats['nodes']:5}"
                  f"{stats['cited_nodes']:6}{stats['coverage']:8.1%}"
                  f"{stats['refs']:5}{stats['sources']:5}"
                  f"{stats['detail_chars']:8.0f}")
            codes = [code for code in stats["diagnostics"]
                     if code and code.startswith("LIBRARY")]
            if codes:
                print(f"{'':20}诊断：{'、'.join(codes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
