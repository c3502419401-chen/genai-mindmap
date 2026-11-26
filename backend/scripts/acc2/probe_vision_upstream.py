"""P4 的上游对证：直接打 `api.deepseek.com`，确认 `deepseek-chat` 收不收 `image_url`。

为什么还要这一层：本机上传一张全新图片 2.2 秒就返回了准确结果，而后端配置的
模型名是 `deepseek-chat`（`run-backend.bat`）。二者要么说明"DeepSeek 的 chat
端点已经吃图片"，要么说明"还有一条我不知道的链路"。验收报告里不能写"大概是
模型支持了"，所以直接对上游发一次同样的请求，把 HTTP 状态与回包里的 `model`
字段记下来。

**只发一次，不带重试**：这一发是为了判定能力边界，不是为了拿结果。

用法：
    python scripts/acc2/probe_vision_upstream.py
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1]
IMAGE = HERE / "fresh_sketch.png"


def from_bat() -> dict[str, str]:
    """从 `run-backend.bat` 读三个变量（后端进程用的就是它们）。"""
    text = (BACKEND.parent / "run-backend.bat").read_text(encoding="utf-8", errors="replace")
    out = {}
    for name in ("MINDMAP_LLM_BASE_URL", "MINDMAP_LLM_API_KEY", "MINDMAP_LLM_MODEL"):
        match = re.search(rf"^set {name}=(.+)$", text, re.M)
        if match:
            out[name] = match.group(1).strip()
    return out


def main() -> int:
    config = {**from_bat(), **{k: v for k, v in os.environ.items() if k.startswith("MINDMAP_LLM_")}}
    base = config.get("MINDMAP_LLM_BASE_URL", "")
    key = config.get("MINDMAP_LLM_API_KEY", "")
    model = config.get("MINDMAP_LLM_MODEL", "")
    print(f"target: {base} model={model} key=…{key[-6:] if key else '(none)'}")
    if not (base and key and model):
        print("!! 缺配置，跳过")
        return 1

    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "这张图最上面一行字是什么？只回答那一行。"},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," +
                           base64.b64encode(IMAGE.read_bytes()).decode()}},
            ],
        }],
        "max_tokens": 100,
        "temperature": 0,
    }
    request = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            body = json.loads(response.read().decode("utf-8"))
        answer = body.get("choices", [{}])[0].get("message", {}).get("content")
        result = {"status": 200, "served_model": body.get("model"),
                  "answer": answer, "usage": body.get("usage")}
    except urllib.error.HTTPError as exc:
        result = {"status": exc.code,
                  "body": exc.read().decode("utf-8", "replace")[:600]}
    except urllib.error.URLError as exc:
        result = {"status": "network", "body": str(exc)[:300]}

    print(json.dumps(result, ensure_ascii=False)[:800])
    (HERE / "p4_upstream.json").write_text(
        json.dumps({"target": {"base": base, "model": model}, **result},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
