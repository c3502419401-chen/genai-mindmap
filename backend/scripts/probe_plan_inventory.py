"""复现并量化「材料清单把份数喂成维度数」那个 bug。

## 它要回答的两个数

    arm=on  （MINDMAP_PLAN_INVENTORY 开 = 线上默认）  骨架给几条一级分支？
    arm=off （清单关，`c5465fd9` 之前的行为）          同样的材料给几条？

判废线是 `MIN_BRANCHES = 3`：骨架回来 2 条时 `_generate` 直接返回
「模型只给了 2 条一级分支」——**用户拿到的不是一张窄图，是一次失败**。

## 材料为什么是那两份

`filename like '%碳中和%'`（210 + 214 字）是目前库里最薄的批：无章节、两篇独立
摘要。清单那句"上面这些材料是一堆彼此独立的篇目"是给 16 份写的，这里是它最容易
把"份数"读成"维度数"的地方。

## 用法

    python scripts/probe_plan_inventory.py                 # 每臂 5 次
    python scripts/probe_plan_inventory.py --repeat 8
    python scripts/probe_plan_inventory.py --only on --save data/plan-inventory-on.json

`--save` 写 JSON（每臂每次的分支数 / 判废原因 / 骨架 raw 前 600 字），自评要用的
原文全在里面。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# 主题取两份摘要的公共说法。不用更窄的词（"高铁"）——那会替模型选定一条分支，
# 而这次要量的是"它自己会切几条"。
TOPIC = "碳中和与碳达峰"


def _fill_env_from_registry() -> None:
    """凭据从 HKCU\\Environment 补（DSH 的 shell 不继承用户级环境变量）。"""
    import winreg

    names = ("MINDMAP_LLM_MODE", "MINDMAP_LLM_BASE_URL", "MINDMAP_LLM_API_KEY",
             "MINDMAP_LLM_MODEL", "MINDMAP_LLM_MODEL_DEEP",
             "MINDMAP_NEO4J_URI", "MINDMAP_NEO4J_USER", "MINDMAP_NEO4J_PASSWORD")
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


def _materials(corpus: bool = False) -> list:
    """默认取库里那两份 openalex 碳中和摘要（210 + 214 字，无章节）。

    只取 `openalex:` 前缀：库里还有两份 `wikipedia: 碳中和`（156 + 89 字），
    它们是同一条维基的不同抓取，混进来就把"2 份"变成"4 份"——而这次要复现的
    正是「2 份」这个形状（`docs/GRAPH-SCALE.md` 的 16 份 A/B 从没覆盖过它）。

    `corpus=True` 换成 `measure_graph_outline._materials()` 那 15 份：那个形状是清单
    当初的收益来源（α 16.9% → 24.2%），改表头之后必须确认它没退化。
    """
    if corpus:
        from scripts.measure_graph_outline import _materials as graph_materials
        return graph_materials()[0]
    from app.knowledge import library

    picked = []
    for row in library.list_sources(limit=200):
        name = row["filename"] or ""
        if not name.startswith("openalex:"):
            continue
        if "碳中和" not in name:
            continue
        source = library.load(row["id"])
        if source is not None and (source.text or "").strip():
            picked.append(source)
    return picked


async def _run_once(topic: str, branches: int, sources: list) -> dict:
    """跑一次真实生成，只认骨架那一次的产物。

    为什么不信 `result.data["diagnostics"]` 里的分支数：判废时 `_fail` 根本不给
    `content`，而"几条分支"要的是**骨架回来的原始条数**（判废前那个数才是根因）。
    所以拦 `chat_async` 取第一次调用的原始响应。
    """
    from app.agent import tools as tools_module
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall

    request = AgentRunRequest(
        goal=topic, intent="create", thinking_model="default", sources=sources,
        budget=RunBudget(max_model_calls=40, max_tool_calls=8, max_seconds=900.0,
                         max_output_tokens=65536, max_repairs=1),
    )
    ctx = RunContext(request=request,
                     sources_by_id={source.id: source for source in sources})
    registry = tools_module.build_registry("create", has_sources=True, has_library=False)
    call = ToolCall(call_id="probe", name="mindmap_generate",
                    arguments={"topic": topic, "branches": branches})

    seen: list[tuple[str, str, str]] = []
    original = tools_module.chat_async

    async def spy(system: str, user: str, **kwargs):
        # 记**响应**不只是请求：分支数在响应里，而判废时 `result.data` 什么都不给。
        answer = await original(system, user, **kwargs)
        seen.append((system, user, answer))
        return answer

    started = time.perf_counter()
    with patch.object(tools_module, "chat_async", spy):
        result = await registry.execute(call, ctx)
    elapsed = round(time.perf_counter() - started, 2)
    raw = seen[0][2] if seen else ""
    # 骨架响应的条数用产品自己的解析器数（`_skeleton_plan` 认得扁平 / 两级两种形状），
    # 不自己写正则：两处口径分叉时量出来的数不是产品看到的数。
    plan = tools_module._skeleton_plan(_extract_json(raw)) if raw else None
    codes = [item.get("code") for item in ((result.data or {}).get("diagnostics") or [])]
    return {
        "ok": bool(result.ok),
        "failure": "" if result.ok else str(result.content)[:160],
        "seconds": elapsed,
        "calls": len(seen),
        "branches": len(plan.flat) if plan else 0,
        "requested": branches,
        "diagnostics": codes,
        "plan_prompt": seen[0][1] if seen else "",
        "raw_head": str(raw)[:600],
    }


def _extract_json(text):
    """骨架响应 → dict。`chat_async` 走结构化输出时回来就是 dict；纯文本时摘 JSON。"""
    import json as _json
    import re

    if isinstance(text, dict):
        return text
    body = (text or "").strip()
    match = re.search(r"\{.*\}", body, re.S)
    if match:
        body = match.group(0)
    try:
        return _json.loads(body)
    except ValueError:
        return {}


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--branches", type=int, default=6)
    parser.add_argument("--topic", default=TOPIC)
    parser.add_argument("--only", choices=("on", "off"), default="")
    parser.add_argument("--corpus", action="store_true",
                        help="换成 15 份那批（清单原来的收益场景），确认不退化")
    parser.add_argument("--save", default="")
    args = parser.parse_args(argv)

    _fill_env_from_registry()
    os.environ.setdefault("MINDMAP_DB_PATH", str(BACKEND / "data" / "mindmaps.db"))
    os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "0"   # 材料定死，不许再补
    os.environ["MINDMAP_RERANK"] = "0"
    # 图谱那一档这次要**关掉**：第三档会替掉"模型自行归纳"，而这一次量的正是后者。
    os.environ["MINDMAP_GRAPH_OUTLINE"] = "0"
    os.environ["MINDMAP_GRAPH_INGEST"] = "0"

    from app.agent import tools as tools_module

    sources = _materials(corpus=args.corpus)
    total = sum(len(source.text or "") for source in sources)
    print(f"材料 {len(sources)} 份 / {total} 字", flush=True)
    for source in sources:
        print(f"  {source.filename[:56]}（{len(source.text or '')} 字）", flush=True)
    if len(sources) < 2:
        print("材料不足两份，这个探针要的正是「2 份」这个形状。", flush=True)
        return 2

    arms = {"on": "清单开（线上默认）", "off": "清单关（c5465fd9 之前）"}
    wanted = [args.only] if args.only else ["on", "off"]
    report: dict = {"topic": args.topic, "requested": args.branches,
                    "materials": [s.filename for s in sources],
                    "material_chars": total, "runs": []}
    for key in wanted:
        os.environ[tools_module.PLAN_INVENTORY_ENV] = "1" if key == "on" else "0"
        print(f"\n=== {arms[key]} ===", flush=True)
        for index in range(1, args.repeat + 1):
            row = await _run_once(args.topic, args.branches, sources)
            row.update({"arm": key, "sample": index,
                        "inventory": tools_module._material_inventory(
                            sources, has_structure=False)})
            report["runs"].append(row)
            mark = f"{row['branches']} 支" if row["ok"] else f"判废：{row['failure']}"
            print(f"  第 {index} 次  {mark}  ({row['seconds']}s)", flush=True)
    if args.save:
        Path(args.save).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {args.save}", flush=True)

    print("\n汇总", flush=True)
    for key in wanted:
        rows = [row for row in report["runs"] if row["arm"] == key]
        ok = [row for row in rows if row["ok"]]
        counts = [row["branches"] for row in ok]
        print(f"  {arms[key]}：成功 {len(ok)}/{len(rows)}"
              f"｜分支 {counts}", flush=True)
    print("\n清单原文（开臂）", flush=True)
    print(report["runs"][0]["inventory"], flush=True) if report["runs"] else None
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
