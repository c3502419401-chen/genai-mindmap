"""只发**骨架那一次**调用，数它给几条一级分支。

## 为什么要有它

`probe_plan_inventory.py` 跑的是整条生成链（骨架 → 结构 → 说明，4–6 次模型调用），
一次 30–40 秒。而"清单把份数喂成维度数"这个缺陷**只发生在骨架那一次**——后面几次
与它无关。所以复现时把后几步全砍掉，一次 5–8 秒，五次采样比原来便宜一个数量级。

## 它造的是**逐字相同**的那次输入

    system = PLAN_PROMPT
    user   = _plan_ask(topic, branches, outline="", materials=_material_inventory(...))

与 `_generate` 里那一次一字不差（整树那条，`outline_entries` 为空、没有 hint_block、
图型是 tree）。改这条路径的任何一处措辞，这里都会跟着变——这正是要的：探针不复制
prompt，它调用产 prompt 的那个函数。

用法：

    python scripts/probe_skeleton_only.py --repeat 5
    python scripts/probe_skeleton_only.py --repeat 5 --corpus     # 那 15 份，验不退化
"""
# requires-optional: winreg
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

TOPIC_THIN = "碳中和与碳达峰"
TOPIC_CORPUS = "机器学习在各领域的应用与研究"


def _fill_env_from_registry() -> None:
    try:
        import winreg
    except ImportError:  # 仅 Windows 有：Linux/CI 上没有 HKCU\Environment 这一层可读
        return

    for name in ("MINDMAP_LLM_MODE", "MINDMAP_LLM_BASE_URL", "MINDMAP_LLM_API_KEY",
                 "MINDMAP_LLM_MODEL", "MINDMAP_LLM_MODEL_DEEP"):
        if os.environ.get(name):
            continue
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, name)
        except OSError:
            continue
        if value:
            os.environ[name] = str(value)


def _materials(corpus: bool) -> list:
    if corpus:
        from scripts.measure_graph_outline import _materials as graph_materials
        return graph_materials()[0]
    from app.knowledge import library

    picked = []
    for row in library.list_sources(limit=200):
        name = row["filename"] or ""
        if name.startswith("openalex:") and "碳中和" in name:
            source = library.load(row["id"])
            if source is not None and (source.text or "").strip():
                picked.append(source)
    return picked


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--branches", type=int, default=6)
    parser.add_argument("--corpus", action="store_true")
    parser.add_argument("--inventory", choices=("on", "off"), default="on")
    args = parser.parse_args(argv)

    _fill_env_from_registry()
    os.environ.setdefault("MINDMAP_DB_PATH", str(BACKEND / "data" / "mindmaps.db"))
    from app.agent import tools

    os.environ[tools.PLAN_INVENTORY_ENV] = "1" if args.inventory == "on" else "0"
    sources = _materials(args.corpus)
    topic = TOPIC_CORPUS if args.corpus else TOPIC_THIN
    inventory = tools._material_inventory(sources, has_structure=False)
    ask = tools._plan_ask(topic, args.branches, outline="", materials=inventory)
    print(f"材料 {len(sources)} 份 / "
          f"{sum(len(source.text or '') for source in sources)} 字｜"
          f"清单 {'开' if args.inventory == 'on' else '关'}｜请求 {args.branches} 支",
          flush=True)
    print("--- 清单 ---", flush=True)
    print(inventory, flush=True)

    counts: list[int] = []
    for index in range(1, args.repeat + 1):
        raw = await tools.chat_async(tools.PLAN_PROMPT, ask)
        plan = tools._skeleton_plan(raw)
        counts.append(len(plan.flat))
        print(f"  第 {index} 次：{len(plan.flat)} 支 → "
              f"{[branch.label for branch in plan.flat]}", flush=True)

    judged = [count for count in counts if count < tools.MIN_BRANCHES]
    print(f"\n分支数 {counts}｜判废（< {tools.MIN_BRANCHES}）{len(judged)}/{len(counts)}",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
