"""P5 的另一半：`.ppt`（97–2003 二进制）这条路在本机是什么结局。

`.pptx` 走 Docling，本机已实测可用（见 `p5_ppt.json`）。`.ppt` 要先经系统级
LibreOffice 转 PDF 再复用视觉提取——本机没装 LibreOffice，容器默认也不装
（`WITH_OFFICE=0`）。这个脚本把"没装的时候用户看到什么"记下来：是 422 还是 500，
错误文案有没有告诉用户该怎么办。

用法：
    python scripts/acc2/probe_ppt_legacy.py
"""
from __future__ import annotations

import http.cookiejar
import json
import shutil
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8000"
PPTX = HERE / "acceptance_sample.pptx"
PPT = HERE / "legacy_sample.ppt"


def session() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f"{BASE}/api/auth/me", timeout=15) as response:
        response.read()
    return opener


def upload(opener, path: Path, content_type: str) -> dict:
    boundary = uuid.uuid4().hex
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        path.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    request = urllib.request.Request(
        f"{BASE}/api/ingestion/files", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    started = time.monotonic()
    try:
        with opener.open(request, timeout=900) as response:
            payload = json.loads(response.read().decode("utf-8"))
        source = payload.get("source") or {}
        return {"status": 200, "seconds": round(time.monotonic() - started, 1),
                "chars": len(source.get("text") or ""),
                "text_head": (source.get("text") or "")[:200],
                "truncated": source.get("truncated")}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("detail")
        except json.JSONDecodeError:
            detail = raw
        return {"status": exc.code, "seconds": round(time.monotonic() - started, 1),
                "detail": detail}


def main() -> int:
    # 同一个包体的两份：文件名决定走哪条路（`.pptx` 走 Docling，`.ppt` 走 LibreOffice）
    shutil.copyfile(PPTX, PPT)
    opener = session()
    report = {
        "pptx": upload(opener, PPTX, "application/vnd.openxmlformats-officedocument."
                                      "presentationml.presentation"),
        "ppt": upload(opener, PPT, "application/vnd.ms-powerpoint"),
    }
    for key, value in report.items():
        print(f"== {key} -> {value['status']} {value['seconds']}s "
              f"{json.dumps(value, ensure_ascii=False)[:400]}")
    path = HERE / "p5_ppt_formats.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
