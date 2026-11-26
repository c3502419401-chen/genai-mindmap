"""乙组真跑：把真实图片 / 真实 WAV 传进 `/api/ingestion/files`，记录后端的第一反应。

凭证在不在**不由本机环境变量回答，由后端的行为回答**：脚本进程里没设
`MINDMAP_LLM_API_KEY` 不代表后端没设（后端是另一个进程）。所以这里只做一件事
——把文件真发过去，看它 200 还是 422、错误原文是什么。

判定口径：
  · 200 + 有正文  → 这条输入真能用（凭证与模型都到位）；
  · 422 + "缺凭证" 类文案 → 凭证缺失，功能未验（但要记下缺的是哪个变量）；
  · 422 + 其它（模型不支持该输入、格式越界）→ 能验，且验出了真结论。

用法：
    python scripts/acc2/probe_media.py            # 图片 + 语音各两份
    python scripts/acc2/probe_media.py --kind image
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1]
MEDIA = BACKEND / "scripts" / "acc_media"
BASE = "http://127.0.0.1:8000"


def session() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f"{BASE}/api/auth/me", timeout=15) as response:
        response.read()
    return opener


def upload(opener, path: Path) -> dict:
    boundary = uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
        f"Content-Type: {mime}\r\n\r\n".encode(),
        path.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    request = urllib.request.Request(
        f"{BASE}/api/ingestion/files", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    started = time.monotonic()
    try:
        with opener.open(request, timeout=600) as response:
            payload = json.loads(response.read().decode("utf-8"))
        elapsed = round(time.monotonic() - started, 1)
        source = payload.get("source") or {}
        text = source.get("text") or ""
        return {
            "file": path.name,
            "bytes": path.stat().st_size,
            "status": 200,
            "seconds": elapsed,
            "kind": source.get("kind"),
            "status_field": source.get("status"),
            "text_chars": len(text),
            "text_head": text[:400],
            "sentences": len(source.get("sentences") or []),
        }
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        elapsed = round(time.monotonic() - started, 1)
        try:
            detail = json.loads(raw).get("detail")
        except json.JSONDecodeError:
            detail = raw
        return {"file": path.name, "bytes": path.stat().st_size, "status": exc.code,
                "seconds": elapsed, "detail": detail}
    except urllib.error.URLError as exc:
        return {"file": path.name, "status": "network", "detail": str(exc)[:200]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["image", "audio", "all"], default="all")
    args = ap.parse_args()

    picks: list[Path] = []
    if args.kind in ("image", "all"):
        picks += [MEDIA / "image" / "i1_sketch_map.png",
                  MEDIA / "image" / "i4_outline.png"]
    if args.kind in ("audio", "all"):
        picks += [MEDIA / "audio" / "a1_real_line00.wav",
                  MEDIA / "audio" / "a3_tts_16k_mono.wav"]

    opener = session()
    report = []
    for path in picks:
        if not path.exists():
            print(f"!! missing {path}")
            continue
        result = upload(opener, path)
        report.append(result)
        print(f"== {path.name} -> {result['status']} {result.get('seconds')}s "
              f"{json.dumps({k: result[k] for k in result if k not in ('text_head', 'detail')}, ensure_ascii=False)}")
        if result.get("detail"):
            print(f"   detail: {str(result['detail'])[:300]}")
        if result.get("text_head"):
            print(f"   text: {result['text_head'][:150]!r}")

    out = HERE / f"media_{args.kind}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
