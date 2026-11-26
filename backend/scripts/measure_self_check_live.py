"""自检回路的**接线后**实测：跑产品代码（`tools._run_self_check`），不是跑脚本版。

`measure_selfcheck.py` 量的是"这条回路值不值得做"（它自己写了一份提示词与清单
生成器，量完还要人工复核）。这个脚本量的是**接进 `_generate` 的那一份**在真实
历史产物上说了什么：

  - 报了几条、分几类、丢掉几条（kind 不认识 / id 对不上 / 超上限）；
  - 其中**几条是逐字判据看不见的**——判据取 `optimization.check()` 的七条与
    `_cross_branch_duplicates` 的逐字比，凡是被它们已经报过的节点，这一条算
    "重复发现"；剩下的就是这条回路独有的产出；
  - 花了多少秒、多少输入 token；
  - 如果用户**全照清单处理**，跨支重复会剩几处（代码侧模拟，不改产品行为）。

用法（在 `backend/` 下，会花 1 次审查调用/张图）：

    python scripts/measure_self_check_live.py                 # 默认两张历史产物
    python scripts/measure_self_check_live.py --run 410ba3d0  # 指定一张
    python scripts/measure_self_check_live.py --dry           # 不调模型，只打印清单

凭据与 `measure_selfcheck.py` 同一条路：从 `run-backend.bat` 的 `set NAME=value`
行取，不落盘、不打印。**不写库**：库以只读打开。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DEFAULT_DB = BACKEND / "data" / "mindmaps.db"
RUN_BAT = BACKEND.parent / "run-backend.bat"

#: 报告 4.2 用的那两张（一张分片产物、一张一次写完产物），默认就跑它们。
DEFAULT_RUNS = ("410ba3d0", "3d71b0ab")

#: 这条回路"独有的"几类：`check()` 的七条规则与 `_cross_branch_duplicates` 都不报
#: 这些（前者只比同级、后者只比逐字）。
UNIQUE_KINDS = ("semantic_duplicate", "off_topic", "level_odd")


def _bat_credentials() -> dict[str, str]:
    found: dict[str, str] = {}
    if not RUN_BAT.exists():
        return found
    for line in RUN_BAT.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = re.match(r"\s*set\s+([A-Z0-9_]+)=(.*)$", line, re.I)
        if match:
            found[match.group(1).upper()] = match.group(2).strip()
    return found


def _fill_credentials() -> None:
    bat = _bat_credentials()
    for name in ("MINDMAP_LLM_BASE_URL", "MINDMAP_LLM_API_KEY", "MINDMAP_LLM_MODEL",
                 "MINDMAP_LLM_MODEL_DEEP"):
        if not os.environ.get(name) and bat.get(name):
            os.environ[name] = bat[name]
    os.environ.setdefault("MINDMAP_LLM_MODE", "model")
    os.environ.setdefault("MINDMAP_LLM_BASE_URL", "https://api.deepseek.com")
    os.environ.setdefault("MINDMAP_LLM_MODEL", "deepseek-chat")


def _load(db: Path, prefix: str) -> tuple[str, dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    for row in conn.execute(
            "select run_id, candidate from runs where candidate is not null "
            "and candidate != '' order by created_at desc"):
        if row["run_id"].startswith(prefix):
            return row["run_id"], json.loads(row["candidate"])
    raise SystemExit(f"库里没有 run_id 以 {prefix} 开头的产物")


def _literal_coverage(content) -> set[str]:
    """逐字判据已经指过的节点 id（`check()` 的七条 + 跨支重复）。"""
    from app.agent.tools import _cross_branch_duplicates
    from app.optimization.service import check

    covered: set[str] = set()
    for item in check(content):
        if item.node_id:
            covered.add(item.node_id)
        for token in (item.evidence or "").split():
            if token:
                covered.add(token)
    for _label, places in _cross_branch_duplicates(content.root):
        covered.update(places)
    return covered


def _report(content, outcome) -> dict:
    covered = _literal_coverage(content)
    issues = outcome.issues
    unique = [item for item in issues
              if item["kind"] in UNIQUE_KINDS and item["node_id"] not in covered]
    return {
        "issues": issues,
        "counts": outcome.counts,
        "dropped": outcome.dropped,
        "outline": {"nodes": outcome.nodes, "lines": outcome.lines,
                    "chars": outcome.chars},
        "seconds": outcome.seconds,
        "prompt_tokens": outcome.prompt_tokens,
        "completion_tokens": outcome.completion_tokens,
        "skipped": outcome.skipped,
        "failure": outcome.failure,
        "unique": [{"node_id": item["node_id"], "kind": item["kind"],
                    "path": item["path"], "problem": item["problem"]}
                   for item in unique],
        "literal_covered": len(covered),
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--run", default=None, help="run_id 前缀；缺省跑两张")
    parser.add_argument("--dry", action="store_true", help="不调模型")
    parser.add_argument("--save", type=Path, default=None)
    args = parser.parse_args()

    from app.agent import tools
    from app.agent.contracts import AgentRunRequest, RunContext
    from app.contracts.mindmap import MindMapContent

    prefixes = (args.run,) if args.run else DEFAULT_RUNS
    _fill_credentials() if not args.dry else None

    out: list[dict] = []
    for prefix in prefixes:
        run_id, candidate = _load(args.db, prefix)
        content = MindMapContent.model_validate(candidate)
        ctx = RunContext(
            request=AgentRunRequest(goal=content.title, intent="create"),
            sources_by_id={})
        if args.dry:
            outline, paths = tools._self_check_outline(content.root)
            print(f"== {run_id}：{len(paths)} 节点 / {len(outline)} 字，"
                  f"{len(content.root.children)} 条一级分支")
            print(outline[:1200], "\n")
            continue
        outcome = await tools._run_self_check(content, ctx, topic=content.title)
        row = {"run_id": run_id, **_report(content, outcome)}
        out.append(row)
        print(f"\n== {run_id}（{content.title}）：{outcome.nodes} 节点 / "
              f"{len(content.root.children)} 条分支")
        print(f"  清单 {outcome.lines} 行 / {outcome.chars} 字，"
              f"输入约 {outcome.prompt_tokens:,} token")
        print(f"  调用 {outcome.seconds}s，报出 {len(outcome.issues)} 条"
              f"（{outcome.counts or '无'}），丢掉 {outcome.dropped or '无'}")
        print(f"  逐字判据已经指过的节点：{row['literal_covered']} 个")
        print(f"  **逐字判据看不见的：{len(row['unique'])} 条**")
        for item in row["unique"]:
            print(f"    [{item['kind']}] {item['path']}：{item['problem']}")
        rest = [item for item in outcome.issues if item not in outcome.issues[
            :0]]  # 保持顺序，下面整体打印
        print("  —— 全部清单 ——")
        for item in rest:
            print(f"    [{item['kind']}] {item['path']}｜{item['problem']}"
                  f"｜改：{item['fix']}")

    if args.save and out:
        args.save.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(f"\n写进 {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
