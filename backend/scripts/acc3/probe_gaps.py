"""三处"接线没做"缺口的**运行级**读数（甲/乙/丙之外的第四组）。

三条断言都在真后端上跑（`run-backend.bat`，端口 8000），因为它们要验的正是
"产品路径通不通"——单元用例测不到这个：

  1. `POST /api/knowledge/search`：库里有 4913 条向量，路由必须给真命中；
  2. `POST /api/optimization/suggest`：产出里必须出现 `kind="link"`，并读几条；
  3. `POST /api/agent/run`（SSE）：`sources` 帧必须带最终清单，且与 `done` 帧里
     `content.source_usage` 的 id 逐个一致——那正是"纯 API 客户端拿到的 id"。

跑法（后端起着就行）：

    cd backend
    python scripts/acc3/probe_gaps.py            # 1 + 2
    python scripts/acc3/probe_gaps.py --sse      # 再加 3（会真花一次生成的钱）

只读：不写库、不改任何导图。第 3 条会发起一次真实生成（几十秒、几分钱）。
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import time
import urllib.request
from urllib.error import HTTPError

BASE = "http://127.0.0.1:8000"


class Client:
    """带游客身份的极简客户端：门禁要身份，`/api/auth/me` 是发 cookie 的唯一入口。"""

    def __init__(self, base: str = BASE):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.call("GET", "/api/auth/me")

    def call(self, method: str, path: str, body: dict | None = None,
             timeout: float = 180.0):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")

    def stream(self, path: str, body: dict, timeout: float = 900.0):
        """SSE：逐帧产出 `(type, payload)`。

        帧形状是 `data: {JSON}`，**没有 `event:` 行**（见 `agent/stream.py`：
        事件类型在 payload 的 `type` 上），`data: [DONE]` 收尾。
        """
        data = json.dumps(body, ensure_ascii=False).encode()
        request = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
        with self.opener.open(request, timeout=timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    return
                try:
                    frame = json.loads(payload)
                except ValueError:
                    continue
                yield frame.get("type", ""), frame


def probe_search(client: Client) -> dict:
    started = time.perf_counter()
    status, body = client.call("POST", "/api/knowledge/search",
                               {"query": "缓存命中率对查询延迟的影响", "top_k": 5})
    elapsed = (time.perf_counter() - started) * 1000
    if status != 200 or not isinstance(body, dict):
        return {"status": status, "error": body}
    hits = body.get("hits") or []
    return {
        "status": status,
        "ms": round(elapsed, 1),
        "implemented": body.get("implemented"),
        "hits": len(hits),
        "top": [{k: (hit.get(k) if k != "snippet" else (hit.get(k) or "")[:60])
                 for k in ("source_id", "filename", "kind", "start", "end",
                           "score", "method", "snippet")}
                for hit in hits[:2]],
    }


def _pick_map(client: Client) -> tuple[str, dict] | None:
    """库里最新的一张**树**（鱼骨/流程进不了 /suggest，路由会 422）。"""
    status, rows = client.call("GET", "/api/mindmaps")
    if status != 200 or not isinstance(rows, list):
        return None
    for row in rows:
        map_id = row.get("id")
        status, record = client.call("GET", f"/api/mindmaps/{map_id}")
        if status != 200 or not isinstance(record, dict):
            continue
        content = record.get("content")
        if isinstance(content, dict) and "root" in content:
            return map_id, content
    return None


def probe_suggest(client: Client) -> dict:
    picked = _pick_map(client)
    if picked is None:
        return {"error": "库里没有树型导图，先跑一次生成"}
    map_id, content = picked
    started = time.perf_counter()
    status, body = client.call("POST", "/api/optimization/suggest",
                               {"content": content, "map_id": map_id}, timeout=300.0)
    elapsed = (time.perf_counter() - started) * 1000
    if status != 200 or not isinstance(body, dict):
        return {"status": status, "error": body}
    kinds: dict[str, int] = {}
    for item in body.get("suggestions") or []:
        kinds[item.get("kind", "")] = kinds.get(item.get("kind", ""), 0) + 1
    links = [item for item in body.get("suggestions") or [] if item.get("kind") == "link"]
    return {
        "status": status,
        "ms": round(elapsed, 1),
        "map_id": map_id,
        "nodes": _count_nodes(content),
        "kinds": kinds,
        "link": body.get("link"),
        "link_message": links[0]["message"] if links else "",
    }


def _count_nodes(content: dict) -> int:
    root = content.get("root")
    if not isinstance(root, dict):
        return 0
    total, stack = 0, [root]
    while stack:
        node = stack.pop()
        total += 1
        stack.extend(node.get("children") or [])
    return total


def probe_sse(client: Client) -> dict:
    """真跑一次生成，比对 `sources` 帧与 `done` 帧里的 id。"""
    body = {
        "goal": "把这两条制度要点整理成思维导图",
        "sources": [{
            "kind": "text", "status": "parsed", "filename": "预算制度.md",
            "text": "# 预算审批制度\n\n预算审批分三级：部门、财务、总经理。\n\n"
                    "单笔超过 50 万要上会；超过 200 万要董事长签。\n",
        }],
    }
    started = time.perf_counter()
    frames: list[str] = []
    source_ids: list[str] = []
    usage_ids: list[str] = []
    content_nodes = 0
    for event, payload in client.stream("/api/agent/runs/stream", body):
        frames.append(event)
        if event == "sources":
            source_ids = [item["id"] for item in payload.get("sources") or []]
        elif event == "done":
            content = payload.get("content") or {}
            usage_ids = [item.get("source_id") for item in content.get("source_usage") or []]
            content_nodes = _count_nodes(content)
    # 断言的方向是"帧里给的 id ⊆ 最终 content 用的 id"：客户端上传的那批材料，
    # 帧里报的 id 就是导图里用的那个。反过来不成立也不该成立——生成途中**外部检索**
    # 入库的材料（`_external_sources` 自己 `library.ingest`）不在开流前那一帧里，
    # 它们本来就是持久 id，身份从 `content.source_usage` 读（那里带 filename/url）。
    return {
        "seconds": round(time.perf_counter() - started, 2),
        "frames": len(frames),
        "sources_frame_ids": [item[:8] for item in source_ids],
        "content_usage_ids": [item[:8] for item in usage_ids],
        "uploaded_ids_are_final": bool(source_ids) and set(source_ids) <= set(usage_ids),
        "external_in_usage": len(set(usage_ids) - set(source_ids)),
        "nodes": content_nodes,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sse", action="store_true", help="再加一次真生成（会花钱）")
    args = parser.parse_args(argv)

    client = Client()
    print("== 1. /api/knowledge/search：桩还是真检索 ==")
    print(json.dumps(probe_search(client), ensure_ascii=False, indent=2))
    print("\n== 2. /api/optimization/suggest：有没有 kind=link ==")
    print(json.dumps(probe_suggest(client), ensure_ascii=False, indent=2))
    if args.sse:
        print("\n== 3. SSE sources 帧 vs done 帧里的 source_usage ==")
        print(json.dumps(probe_sse(client), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
