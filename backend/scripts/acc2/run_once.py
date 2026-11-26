"""验收用真跑器：发一次 agent 运行，把 SSE 帧时间线与最终整树落盘。

为什么走 HTTP 而不是进程内直调：验收要证明的是"用户点下去之后会发生什么"，
那段行为是 SSE（`node_grow` 一帧一帧长出来的），进程内直调量不出来，也绕过了
门禁与序列化这两层最容易出问题的地方。

用法：
    python scripts/acc2/run_once.py --label p5_ppt --goal "..." --source-file p5_pptx_source.json
    python scripts/acc2/run_once.py --label p14_fragment --goal "..." --intent create
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8000"


def session() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f"{BASE}/api/auth/me", timeout=15) as response:
        who = json.loads(response.read().decode("utf-8"))
    print(f"identity: kind={who.get('kind')} guest={str(who.get('guest_id'))[:8]}…")
    return opener


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--goal", required=True)
    ap.add_argument("--intent", default="create")
    ap.add_argument("--diagram-type", default=None)
    ap.add_argument("--source-file", default=None, help="ingestion/files 落盘的 source json")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    body: dict = {"goal": args.goal, "intent": args.intent}
    if args.diagram_type:
        body["diagram_type"] = args.diagram_type
    if args.source_file:
        raw = json.loads((HERE / args.source_file).read_text(encoding="utf-8"))
        body["sources"] = [raw["source"] if "source" in raw else raw]

    opener = session()
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}/api/agent/runs/stream", data=payload, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Accept": "text/event-stream"})

    frames: list[dict] = []
    started = time.monotonic()
    content = None
    seq_last = 0
    with opener.open(request, timeout=args.timeout) as response:
        status = response.status
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            text = line[5:].strip()
            if not text or text == "[DONE]":
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                continue
            elapsed = round(time.monotonic() - started, 2)
            kind = event.get("type") or event.get("event") or "?"
            seq_last = event.get("seq", seq_last)
            frames.append({"t": elapsed, "type": kind,
                           "keys": sorted(event)[:8]})
            if kind == "snapshot" and event.get("content"):
                content = event["content"]
            if kind == "error":
                print(f"!! error frame @{elapsed}s: {json.dumps(event, ensure_ascii=False)[:400]}")

    total = round(time.monotonic() - started, 2)
    out = {
        "label": args.label,
        "http_status": status,
        "goal": args.goal,
        "intent": args.intent,
        "diagram_type": args.diagram_type,
        "sources": len(body.get("sources") or []),
        "seconds": total,
        "frames": len(frames),
        "seq_last": seq_last,
        "frame_kinds": _count_kinds(frames),
        "first_frames": frames[:6],
        "content": content,
        "content_stats": _stats(content),
    }
    path = HERE / f"{args.label}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: out[k] for k in
                      ("http_status", "seconds", "frames", "frame_kinds", "content_stats")},
                     ensure_ascii=False, indent=1))
    print(f"saved: {path}")
    return 0


def _count_kinds(frames: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for frame in frames:
        counts[frame["type"]] = counts.get(frame["type"], 0) + 1
    return counts


def _stats(content: dict | None) -> dict:
    """三种图型各算各的：鱼骨没有 `root`，流程也没有（它们分别是 bones / stages）。"""
    if not content:
        return {"present": False}
    kind = content.get("diagram_type") or "tree"
    if content.get("bones") is not None:
        nodes, depth = 1, 1
        for bone in content["bones"]:
            sub_nodes, sub_depth = _walk(bone.get("root") or {}, 2)
            nodes += sub_nodes
            depth = max(depth, sub_depth)
        return {"present": True, "title": content.get("title"),
                "nodes": nodes, "depth": depth, "type": "fishbone",
                "bones": len(content["bones"]),
                "problem": content.get("problem")}
    if content.get("stages") is not None:
        nodes, depth = 1, 1
        for stage in content["stages"]:
            sub_nodes, sub_depth = _walk(stage.get("root") or {}, 2)
            nodes += sub_nodes
            depth = max(depth, sub_depth)
        return {"present": True, "title": content.get("title"),
                "nodes": nodes, "depth": depth, "type": "flowchart",
                "stages": len(content["stages"])}
    root = content.get("root") or {}
    nodes, depth = _walk(root)
    return {"present": True, "title": content.get("title"),
            "nodes": nodes, "depth": depth, "type": kind}


def _walk(node: dict, level: int = 1) -> tuple[int, int]:
    total, deepest = 1, level
    for child in node.get("children") or []:
        sub_total, sub_depth = _walk(child, level + 1)
        total += sub_total
        deepest = max(deepest, sub_depth)
    return total, deepest


if __name__ == "__main__":
    raise SystemExit(main())
