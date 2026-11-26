"""改图**该查时到底查没查**：5 组"该翻材料"的说法，各跑一次真实的 edit。

背景（`9c2ebcf`）：`edit` / `expand` 的检索指令从"没有说法"改成"**默认不要检索**，
只有用户明确说「按材料补充」「核对原文」时才查"。那句指令写在提示词层
（`service._RAG_INTENT_RULES`），代价是那位 agent 自己记下的：

> 模型可能在该查时也不查，表现为改图后引用为空。

所以这条要**量**，不是推。「会不会发生」没有意义——实测数字才是判据：

    0/5   → 兜底是给不存在的问题加代码，不做，把这条记进文档；
    ≥2/5  → 按建议加（提示词层：goal 里出现「材料 / 原文 / 引用 / 出处」时，
            把检索指令提到最前面）。

## 怎么量

真发 `/api/agent/runs/stream`（`intent=edit`，`snapshot` 是真图，`sources` 是一份
真材料），看三件事：

  1. 事件流里有没有 `tool_start name=rag_query`（有没有查）；
  2. 产物候选树上 `source_refs` 的总条数（引用是不是空的）——这是"没查"在产出上的
     表现，也是用户会看到的东西；
  3. 耗时（`rag_query` 是 3–7 秒一次，没查的那几次会明显更快）。

对照组是 3 句**不该查**的说法（"把标题改短一点"这类）：它们没查是**对的**，
用来证明判据不是"凡是 edit 都算没查"。

用法（在 `backend/` 下，需要后端在跑）：

    python scripts/measure_edit_retrieval.py            # 5 组该查 + 3 组不该查
    python scripts/measure_edit_retrieval.py --ask 0 2  # 只跑第 0、2 组
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import sqlite3
import time
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DB = BACKEND / "data" / "mindmaps.db"
BASE = "http://127.0.0.1:8000"
URL = f"{BASE}/api/agent/runs/stream"

#: 该翻材料的说法（判据是"这句话说出口时用户期望它去核原文"）。
#: 节点名取自真图 `c1d30b17`（「注意力机制的核心思想」）的一级分支，材料是那篇论文。
ASK = (
    "把「自注意力与跨序列对齐」这一支按材料补充一下",
    "「相似度打分与权重分配」下面这几条的引用要和原文对上",
    "把「多头并行捕捉多类关系」这段改成材料里的说法",
    "按原文核对一下「查询、键与值的三元结构」这几条说得对不对",
    "把材料里的原文补进「加权求和生成上下文向量」这几条",
)

#: 对照组：这几句**不该**查（改了措辞/颜色，与材料无关）。它们没查是对的。
CONTROL = (
    "把「注意力机制的核心思想」这个标题改短一点",
    "把「多头并行捕捉多类关系」改名成「多头并行」",
    "把标题改短一点",
)

#: 要改的图与要翻的材料：图是「注意力机制的核心思想」，材料是那篇论文的正文。
MAP_ID = "c1d30b17-7e80-4c88-81eb-fc57a8fe5089"
MATERIAL_FILENAME = "attention-37973.md"


def _load_map() -> dict:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute("select content from mindmaps where id = ?",
                       (MAP_ID,)).fetchone()
    if row is None:
        raise SystemExit(f"库里没有 {MAP_ID}")
    return json.loads(row["content"])


def _load_material() -> dict:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "select id, filename, text from library_sources where filename = ? "
        "order by length(text) desc", (MATERIAL_FILENAME,)).fetchone()
    if row is None:
        raise SystemExit(f"库里没有材料 {MATERIAL_FILENAME}")
    return {"id": row["id"], "kind": "document", "status": "parsed",
            "filename": row["filename"], "text": row["text"]}


def _count_refs(node: dict) -> int:
    return len(node.get("source_refs") or []) + sum(
        _count_refs(child) for child in node.get("children") or [])


def _count_nodes(node: dict) -> int:
    return 1 + sum(_count_nodes(child) for child in node.get("children") or [])


def _opener() -> urllib.request.OpenerDirector:
    """带 cookie 的 opener。**先 `GET /api/auth/me` 领一张游客 cookie**。

    没有它请求直接 401：`/api/agent/runs/stream` 认身份（`owner`），而游客身份
    是浏览器拿到的第一张 cookie（`app/auth/router.py::who_am_i` 是全站唯一发它的
    地方）。手动拼一个 32 位十六进制串是不行的——值带签名。
    """
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    with opener.open(f"{BASE}/api/auth/me", timeout=20) as response:
        response.read()
    return opener


def _run(opener, goal: str, snapshot: dict, material: dict, tag: str) -> dict:
    # `run_id` 带时间戳：同一个 id 重投会被 `runs.begin()` 判成"已经跑过"、
    # 直接回放上次的候选——一次模型调用都不发，量出来的"没查"是假的。
    run_id = f"measure-edit-{tag}-{int(time.time())}"
    body = json.dumps({
        "goal": goal, "intent": "edit", "diagram_type": "tree",
        "run_id": run_id,
        "snapshot": snapshot, "sources": [material],
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        URL, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Accept": "text/event-stream"})
    started = time.perf_counter()
    queries: list[str] = []
    tools: list[str] = []
    candidate = None
    error = None
    with opener.open(request, timeout=600) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                frame = json.loads(payload)
            except ValueError:
                continue
            kind = frame.get("type") or next(iter(frame), "")
            if kind == "tool_start":
                tools.append(frame.get("name") or "")
                if frame.get("name") == "rag_query":
                    queries.append(json.dumps(frame.get("args_summary") or {},
                                              ensure_ascii=False)[:80])
            elif kind == "done":
                candidate = frame.get("content")
            elif kind in ("error", "agent_error"):
                error = frame.get("message") or frame
    seconds = time.perf_counter() - started
    return {"goal": goal, "queries": len(queries), "query_args": queries,
            "tools": tools, "seconds": round(seconds, 1),
            "refs": _count_refs(candidate["root"]) if candidate else None,
            "nodes": _count_nodes(candidate["root"]) if candidate else None,
            "error": error}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ask", type=int, nargs="*", default=None)
    parser.add_argument("--save", type=Path, default=None)
    args = parser.parse_args()

    snapshot = _load_map()
    material = _load_material()
    print(f"图：{snapshot.get('title')}（{_count_nodes(snapshot['root'])} 节点，"
          f"{_count_refs(snapshot['root'])} 条引用）")
    print(f"材料：{material['filename']}（{len(material['text'])} 字）")

    rows: list[dict] = []
    opener = _opener()
    picks = args.ask if args.ask is not None else list(range(len(ASK)))
    for index in picks:
        row = _run(opener, ASK[index], snapshot, material, f"ask{index}")
        row["group"] = "ask"
        rows.append(row)
        print(f"\n[该查 {index}] {ASK[index]}")
        print(f"  查了 {row['queries']} 次；引用 {row['refs']} 条；"
              f"{row['seconds']}s；工具 {'、'.join(row['tools']) or '无'}")
        if row["error"]:
            print(f"  错误：{row['error']}")
        for item in row["query_args"]:
            print(f"    query: {item}")

    for index, goal in enumerate(CONTROL):
        row = _run(opener, goal, snapshot, material, f"ctl{index}")
        row["group"] = "control"
        rows.append(row)
        print(f"\n[对照 {index}] {goal}")
        print(f"  查了 {row['queries']} 次；引用 {row['refs']} 条；"
              f"{row['seconds']}s；工具 {'、'.join(row['tools']) or '无'}")

    missed = [row for row in rows
              if row["group"] == "ask" and row["queries"] == 0]
    empty = [row for row in rows if row["group"] == "ask" and not row["refs"]]
    print("\n== 结论素材 ==")
    print(f"  该查的 {len(picks)} 组里 **{len(missed)} 组没查**；"
          f"{len(empty)} 组改完之后引用为空")
    for row in missed:
        print(f"    没查：{row['goal']}（引用 {row['refs']} 条）")
    print(f"  对照组 {len(CONTROL)} 句里 "
          f"{sum(1 for row in rows if row['group'] == 'control' and row['queries'])} 句查了"
          "（查了也不算错，只是白等几秒）")

    if args.save:
        args.save.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(f"\n写进 {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
