"""发一次真实的 SSE 生成请求，把每一帧原样存下来。

用它而不是 curl：这台机器的 `curl.exe` 在 PowerShell 里传中文 body 会走 GBK，
主题会变成问号，而"碳中和"这个主题正是要测的那一个。Python 侧显式 UTF-8 编码，
命令行参数只用来指定主题。

用法：

    python scripts/_sse_probe.py "碳中和的主要方面" D:\\genai-mindmap\\.visioncheck\\sse.json

输出：把 `data: {...}` 的每一帧按顺序写进目标文件（JSON Lines），并在 stderr 打一行
摘要（帧数、有没有 error、最后一张图的节点数）。
"""
from __future__ import annotations

import json
import sys
import urllib.request

URL = "http://127.0.0.1:8000/api/agent/runs/stream"


def main(argv: list[str]) -> int:
    goal = argv[0] if argv else "碳中和的主要方面"
    out = argv[1] if len(argv) > 1 else "sse.jsonl"
    body = json.dumps({"goal": goal, "intent": "create", "diagram_type": "tree"},
                      ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        URL, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Accept": "text/event-stream"})
    frames: list[dict] = []
    with urllib.request.urlopen(request, timeout=900) as response, \
            open(out, "w", encoding="utf-8") as handle:
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
            frames.append(frame)
            handle.write(json.dumps(frame, ensure_ascii=False) + "\n")
    kinds = [frame.get("stage") or frame.get("type") or list(frame)[0] for frame in frames]
    errors = [frame for frame in frames if "error" in str(frame)[:120]]
    print(f"帧数 {len(frames)}｜{out}", file=sys.stderr)
    print(f"帧型 {kinds[:12]}", file=sys.stderr)
    if errors:
        print(f"错误帧 {errors[:1]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
