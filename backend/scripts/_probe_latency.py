"""临时探针：真跑一次 agent 请求，记录 SSE 帧时间线与后端日志新增行。

用途是把"改节点会不会触发检索"与"每一段花多久"从推断变成读数。
跑完就删（不进提交）。

用法：
    python scripts/_probe_latency.py edit
    python scripts/_probe_latency.py expand
    python scripts/_probe_latency.py create "主题"
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import pathlib
import sys
import time
import urllib.request

#: 端口可换：验证"改完之后那条路真的不再检索"时，要在另一个进程里跑**新代码**，
#: 而又不能把用户正在用的 8000 那个后端重启掉。
PORT = os.environ.get("MINDMAP_PROBE_PORT", "8000")
URL = f"http://127.0.0.1:{PORT}/api/agent/runs/stream"
ME = f"http://127.0.0.1:{PORT}/api/auth/me"
LOG = pathlib.Path(r"D:\genai-mindmap\.visioncheck\backend.log")


def snapshot() -> dict:
    from app.contracts.mindmap import MindMapContent, Node

    root = Node(id="n0", label="碳中和路径", children=[
        Node(id="n1", label="能源结构调整", children=[
            Node(id="n1a", label="风电与光伏装机"),
            Node(id="n1b", label="煤电灵活性改造"),
        ]),
        Node(id="n2", label="工业过程减排", children=[
            Node(id="n2a", label="水泥熟料替代"),
        ]),
        Node(id="n3", label="碳汇与碳捕集"),
    ])
    return MindMapContent(title="碳中和路径", root=root).model_dump(mode="json")


def body(kind: str, goal: str) -> dict:
    if kind == "create":
        return {"goal": goal, "intent": "create", "diagram_type": "tree"}
    if kind == "edit":
        return {"goal": goal or "把「工业过程减排」这个标题改短一点", "intent": "edit",
                "snapshot": snapshot(), "selected_ids": ["n2"]}
    return {"goal": goal or "展开节点「能源结构调整」，补充 3 个子节点。",
            "intent": "expand", "snapshot": snapshot(), "selected_ids": ["n1"]}


def main(argv: list[str]) -> int:
    kind = argv[0] if argv else "edit"
    goal = argv[1] if len(argv) > 1 else ""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    # 门禁要 cookie：先取一份游客身份（GET /api/auth/me 是全站唯一发它的地方）。
    with opener.open(ME, timeout=10) as response:
        print(f"== 身份 {response.read()[:120].decode('utf-8', 'replace')}")
    offset = LOG.stat().st_size if LOG.exists() else 0
    payload = json.dumps(body(kind, goal), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        URL, data=payload, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Accept": "text/event-stream"})
    start = time.monotonic()
    marks: list[tuple[float, str]] = []
    content = None
    with opener.open(request, timeout=900) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            text = line[5:].strip()
            if not text or text == "[DONE]":
                continue
            try:
                frame = json.loads(text)
            except ValueError:
                continue
            el = time.monotonic() - start
            name = frame.get("type") or frame.get("event") or "?"
            detail = (frame.get("name") or frame.get("stage") or frame.get("message")
                      or frame.get("tool") or "")
            if name == "tool_end":
                detail = f"{detail} ok={frame.get('ok')} {frame.get('latency_ms')}ms"
            if name == "done":
                content = frame.get("content")
            marks.append((round(el, 2), f"{name} {detail}"[:110]))
    total = time.monotonic() - start
    print(f"== {kind} 总耗时 {total:.1f}s，帧 {len(marks)}")
    for el, name in marks:
        print(f"  {el:7.2f}s  {name}")
    nodes = 0
    if isinstance(content, dict):
        def walk(node):
            return 1 + sum(walk(c) for c in node.get("children") or [])
        nodes = walk(content["root"]) if content.get("root") else 0
    print(f"== 结果节点数 {nodes}")
    if LOG.exists():
        with LOG.open("rb") as handle:
            handle.seek(offset)
            tail = handle.read().decode("utf-8", "replace")
        print("== 后端日志新增 ==")
        for line in tail.splitlines():
            if any(key in line for key in ("材料入口", "外部检索", "检索", "耗时", "入图",
                                           "库", "rerank", "重排")):
                print("  " + line[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
