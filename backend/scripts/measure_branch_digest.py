"""实测：给一级分支写概括（`MINDMAP_BRANCH_DIGEST`）——**真模型、一次批量调用**。

## 为什么要有这个脚本

`docs/FOLD-RETIRED.md` §5 记过一条批量概括调用的实测（5 组 1.87 秒），但那是在
**折叠**那条路上量的，而且量的是"产出的字符串看起来可用"。这一轮它变成了画布上
的一枚角标，要回答的问题因此具体得多：

  1. 六条概括**是不是各说各的**（不是把分支标题换一种说法抄一遍）；
  2. 一次调用要多久、打多少 token；
  3. 失败 / 少写几支时，角标那一侧会是什么样子（降级由用例钉，这里只量真实产出）。

## 用法（在 `backend/` 下）

    python scripts/measure_branch_digest.py
    python scripts/measure_branch_digest.py --db data/mindmaps.db --map-id <id>
    python scripts/measure_branch_digest.py --out scripts/_branch_digest_sample.json

**需要模型配置**（与 `run-backend.bat` 同一套环境变量：`MINDMAP_LLM_BASE_URL` /
`MINDMAP_LLM_API_KEY` / `MINDMAP_LLM_MODEL`）。没配就只说一句，不发请求——这个脚本
不内置任何密钥。

## 边界

只调**一次**（`_fill_branch_digests` 的全部内容），不生成整棵树：要验的是"这一句话
值不值一次调用"，不是"树长得好不好"。整树的成本口径在 `docs/ONE-CALL.md`。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DEFAULT_DB = "data/mindmaps.db"
DEFAULT_OUT = "scripts/_branch_digest_sample.json"


def _count(node: dict) -> int:
    return 1 + sum(_count(child) for child in node.get("children") or [])


def load_tree(db: str, map_id: str | None) -> tuple[str, dict]:
    """从库里取一棵真树（默认取**节点数最多**的那一棵）。

    用真树而不是现造的等宽树：一级分支的条数与下属节点的疏密决定概括能不能写出
    区别来，等宽树量出来的六条会是一组同义词。
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("select id, content from mindmaps").fetchall()
    best: tuple[int, str, dict] | None = None
    for row_id, raw in rows:
        if map_id and row_id != map_id:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not data.get("root"):
            continue
        size = _count(data["root"])
        if best is None or size > best[0]:
            best = (size, row_id, data)
    if best is None:
        raise SystemExit(f"库里没有可用的树：{db}")
    return best[1], best[2]


async def measure(content, topic: str) -> dict:
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext
    from app.agent.tools import _fill_branch_digests, _BranchDigestReport

    ctx = RunContext(request=AgentRunRequest(
        goal=topic, intent="create", thinking_model="default",
        budget=RunBudget(max_model_calls=4, max_tool_calls=2, max_seconds=120.0,
                         max_output_tokens=8192),
    ))
    started = time.perf_counter()
    filled, report = await _fill_branch_digests(content, ctx, topic=topic)
    wall = round(time.perf_counter() - started, 3)
    assert isinstance(report, _BranchDigestReport)
    return {
        "topic": topic,
        "wall_seconds": wall,
        "report": report.account(),
        "digests": filled.branch_digests,
        "branches": [
            {
                "id": node.id,
                "label": node.label,
                "subtree_nodes": _count(node.model_dump()),
                "children": [child.label for child in node.children[:3]],
                "digest": filled.branch_digests.get(node.id),
            }
            for node in content.root.children
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--map-id", default=None)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    if not os.getenv("MINDMAP_LLM_API_KEY"):
        print("没有模型配置（MINDMAP_LLM_API_KEY 为空）：这个脚本要真调一次模型，"
              "请在 run-backend.bat 那套环境变量下运行。")
        return 2

    from app.contracts.mindmap import MindMapContent, migrate_content

    db = str(Path(args.db) if Path(args.db).is_absolute() else BACKEND / args.db)
    map_id, data = load_tree(db, args.map_id)
    content = MindMapContent.model_validate(migrate_content(data))
    print(f"图：{content.title}（{map_id}，{_count(data['root'])} 个节点，"
          f"{len(content.root.children)} 条一级分支）\n")

    payload = asyncio.run(measure(content, content.title))
    report = payload["report"]
    for index, branch in enumerate(payload["branches"], start=1):
        print(f"{index}. [{branch['subtree_nodes']:>3} 个节点] {branch['label']}")
        print(f"   下位：{'、'.join(branch['children'])}")
        print(f"   概括：{branch['digest'] or '（没写出来）'}")
    print()
    print(f"一次调用：{payload['wall_seconds']} 秒 · 输入约 {report['prompt_tokens']} token"
          f" · 输出约 {report['completion_tokens']} token · "
          f"写回 {report['written']}/{report['branches']} 支"
          + (f" · skipped={report['skipped']}" if report["skipped"] else ""))
    if report["missing"]:
        print(f"缺的：{report['missing']}")
    if report["unknown"]:
        print(f"对不上号的键：{report['unknown']}")

    out = Path(args.out) if Path(args.out).is_absolute() else BACKEND / args.out
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n样本：{out.relative_to(BACKEND.parent)}（临时产物，不进提交）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
