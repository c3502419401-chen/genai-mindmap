"""P4 端到端：把"图片识别出的 Markdown"当成材料跑一次生成，看能不能出导图。

为什么分两步：`/api/ingestion/files` 只回答"图能不能被读成文本"（`p4_vision_fresh.json`
已验），而计划书要求的是"上传一张草图 → 得到导图"。这一步把识别结果送进真实的
SSE 生成，才能判"整条路通不通"。

用法：
    python scripts/acc2/probe_p4_generate.py
"""
from __future__ import annotations

import http.cookiejar
import json
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8000"


def main() -> int:
    vision = json.loads((HERE / "p4_vision_fresh.json").read_text(encoding="utf-8"))
    text = vision["text"]
    print(f"识别文本 {len(text)} 字，前 80：{text[:80]!r}")

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f"{BASE}/api/auth/me", timeout=15) as response:
        response.read()

    payload = json.dumps({"text": text, "kind": "text"}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}/api/ingestion/text", data=payload, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    with opener.open(request, timeout=60) as response:
        source = json.loads(response.read().decode("utf-8"))["source"]
    out = HERE / "p4_source.json"
    out.write_text(json.dumps({"source": source}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"saved: {out} (id={source['id'][:8]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
