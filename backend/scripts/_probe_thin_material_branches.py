"""薄材料上骨架只给 2 条：补丁现在到底怎么处置（**离线复跑**）。

为什么要说清"离线"：真模型复跑要 `MINDMAP_LLM_API_KEY`，本机 `.env` 里只有
`DASHSCOPE_API_KEY`（那一个服务语音转写）。所以这里做两件不依赖模型的事：

  1. **目录侧**是真数据、真代码：拿 `scripts/acc_samples.json` 里那 20 条验收材料
     （含 6 条立项材料）跑一遍 `_outline_entries` / `outline_coverage`，看现在
     骨架那一次**看得见几块**、覆盖率过不过 `OUTLINE_MIN_COVERAGE`（0.6）、
     以及会不会补 `OUTLINE_FEW_BLOCKS_NOTE`。这一侧一个随机量都没有。
  2. **骨架侧**是同一套代码路径 + 桩响应：复现 `docs/OFF-TOPIC-AND-FEW-BRANCHES.md`
     里那三种结局，确认"判死"只在纯主题那一档还留着。

三者对应的用例在 `backend/tests/test_few_branches.py`（4 节）。这个脚本的价值不是
替代用例，是给"补丁真的修好了"一句可复算的话——用例红了会有人看见，但"现在对真实
材料是几块"这个数，只有跑一遍才知道。

    python scripts/_probe_thin_material_branches.py
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall  # noqa: E402
from app.agent.tools import (  # noqa: E402
    DETAIL_ONLY_PROMPT,
    MIN_BRANCHES,
    OUTLINE_FEW_BLOCKS_NOTE,
    OUTLINE_MIN_COVERAGE,
    PLAN_PROMPT,
    _outline_entries,
    build_registry,
    outline_coverage,
)
from app.contracts.source import Source, SourceKind  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
SAMPLES = BACKEND / "scripts" / "acc_samples.json"

TOPIC = "薄材料上的骨架"


def _load_samples() -> list[dict]:
    return json.load(io.open(SAMPLES, encoding="utf-8"))["samples"]


def _source(text: str, *, source_id: str = "s-probe") -> Source:
    return Source(id=source_id, kind=SourceKind.document, status="parsed",
                  filename="样本.txt", text=text)


def probe_outline() -> None:
    """20 条真实样本：骨架那一次看得见几块、覆盖率多少、要不要补那句话。"""
    print("== 1. 目录侧：骨架看得见几块（真材料，无模型）==")
    print(f"{'id':5} {'块数':>4} {'覆盖率':>7} {'过门槛':>6} {'补那句话':>8}  材料")
    few = 0
    for sample in _load_samples():
        source = _source(sample["text"], source_id=sample["id"])
        entries = _outline_entries([source])
        coverage = outline_coverage([source], entries)
        passes = coverage >= OUTLINE_MIN_COVERAGE
        note = bool(entries) and len(entries) < MIN_BRANCHES
        few += 1 if note else 0
        kind = (sample.get("goal") or "").replace("根据这份", "").replace("生成思维导图。", "")
        print(f"{sample['id']:5} {len(entries):>4} {coverage:>7.2f} "
              f"{'是' if passes else '否':>6} {'是' if note else '否':>8}  {kind}")
    print(f"合计 {len(_load_samples())} 条，目录少于 {MIN_BRANCHES} 块（会补"
          f" `OUTLINE_FEW_BLOCKS_NOTE`）的有 {few} 条。\n")


def _request(sources: list[Source]) -> AgentRunRequest:
    return AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default", sources=sources,
        budget=RunBudget(max_model_calls=20, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=1),
    )


def _ctx(sources: list[Source]) -> RunContext:
    return RunContext(request=_request(sources),
                      sources_by_id={source.id: source for source in sources})


def _tree(branches: list[str]) -> dict:
    return {"title": TOPIC, "root": {
        "id": "root", "label": TOPIC, "detail": "中心主题", "children": [
            {"id": f"b{index}", "label": label, "children": [
                {"id": f"b{index}-1", "label": f"{label}·一", "detail": "一句说明。"},
                {"id": f"b{index}-2", "label": f"{label}·二", "detail": "另一句说明。"},
            ]}
            for index, label in enumerate(branches)
        ]}}


def _generate(sources: list[Source], plan_answers: list[list[str]]):
    """按 `plan_answers` 依次回答骨架那一次；结构那一次永远照最后一次的条数写。"""
    seen: list[tuple[str, str]] = []
    answers = list(plan_answers)

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None):
        seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            labels = answers.pop(0) if len(answers) > 1 else answers[0]
            return {"branches": list(labels)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {}
        labels = plan_answers[-1] if len(plan_answers) == 1 else plan_answers[1]
        return _tree(list(labels))

    ctx = _ctx(sources)
    registry = build_registry("create", has_sources=bool(sources), has_library=False)
    with _patched_chat(chat):
        result = asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate", arguments={"topic": TOPIC}),
            ctx))
    plans = [item for item in seen if item[0].startswith(PLAN_PROMPT)]
    return result, ctx, plans


def _patched_chat(chat):
    from unittest.mock import patch
    return patch("app.agent.tools.chat_async", new=chat)


def probe_outcomes() -> None:
    """三种结局各跑一次，打印 ok / code / 一级分支 / 诊断。

    **薄材料必须先过 `SHORT_MATERIAL_CHARS`（300 字）**：更短的会被当成"主题补充说明"
    走另一条路，那时它根本不算"有材料"，测的就不是这里要测的东西。所以下面这段是
    "两块标题 + 够长的正文"，不是随手写两行。
    """
    print("== 2. 骨架侧：三种结局（同一套代码路径，桩响应）==")
    thin = _source(
        "薄材料\n"
        "一、背景\n"
        "这件事之所以要做，是因为现有的流程在每个环节都要人工复核一遍，而复核的人"
        "手里没有统一的判据：同一个问题换一个人看，结论可能不一样。上一季度统计下来，"
        "平均每个单子要经过四次流转才能定下来，其中两次是纯粹的等待，一次是因为材料"
        "不全被退回，剩下一次才是真正的内容评审。流转一多，责任就散了，出了问题谁也"
        "说不清是哪一环该拦下来。所以这一轮先把流程本身摆清楚，再谈工具。\n"
        "二、结论\n"
        "先把复核判据写成一份可执行的清单，把它挂在最靠前的那一环上；后面几环只做"
        "形式检查，不再重复判内容。清单本身按「能不能被一条一条勾掉」来写，不写原则性"
        "的话。清单之外的情况走例外通道，例外通道要有记录，记录要能被下一个人读懂。"
        "如果这一轮只能做一件事，就把清单写出来并试跑两周，用试跑期间的退回率判断它"
        "够不够用；不够用再补条目，而不是先做大而全的系统。\n"
    )
    cases = [
        ("① 纯主题 + 2 条", [], [["甲", "乙"]]),
        ("② 薄材料 + 先 2 条、重问 6 条", [thin],
         [["甲", "乙"], ["甲", "乙", "丙", "丁", "戊", "己"]]),
        ("③ 薄材料 + 两次都 2 条", [thin], [["甲", "乙"]]),
    ]
    for name, sources, answers in cases:
        result, ctx, plans = _generate(sources, answers)
        branches = [child.label for child in ctx.candidate.root.children] \
            if ctx.candidate is not None else []
        codes = [item["code"] for item in result.data.get("diagnostics", [])]
        below = "有" if "BRANCHES_BELOW_MIN" in codes else "无"
        code = "" if result.ok else result.data.get("code", "")
        print(f"{name:28} ok={str(result.ok):5} code={code or '-':18} "
              f"骨架调用={len(plans)} 一级分支={len(branches)} "
              f"BRANCHES_BELOW_MIN={below}")


def _clean_env() -> None:
    """把这台机器上的开关全部压回"什么都没开"。

    不这么做的话，`MINDMAP_EXTERNAL_KNOWLEDGE` 一开，骨架那一路会**真的发外网请求**
    （openalex / 维基），"纯主题"那一档就不再是纯主题——第一次跑这个脚本就是这么
    得出 `① 纯主题 + 2 条 → ok=True` 的假结论的。用例那边由
    `tests/conftest.py::clean_env` 统一负责，这里没有那层，只能自己来。
    """
    for name in (
        "MINDMAP_EXTERNAL_KNOWLEDGE", "MINDMAP_LIBRARY_IN_GENERATE",
        "MINDMAP_LIBRARY_SEARCH", "MINDMAP_GRAPH_OUTLINE", "MINDMAP_GRAPH_SKELETON",
        "MINDMAP_GRAPH_INGEST", "MINDMAP_MIXED_SKELETON", "MINDMAP_SECTION_SLICE",
        "MINDMAP_QUERY_EXPANSION", "MINDMAP_INPUT_EXTRACT", "MINDMAP_AUTO_REPAIR",
    ):
        os.environ[name] = "0"


def main() -> None:
    _clean_env()
    print(f"OUTLINE_MIN_COVERAGE={OUTLINE_MIN_COVERAGE}  MIN_BRANCHES={MIN_BRANCHES}  "
          f"一句话={OUTLINE_FEW_BLOCKS_NOTE[:12]}…\n")
    probe_outline()
    probe_outcomes()


if __name__ == "__main__":
    main()
