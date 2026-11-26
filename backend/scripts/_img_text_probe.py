"""临时探针：一次图片上传到底解析出了什么文本。

为什么必须看文本：`source_nonempty` 只能说明"有字"，不能说明"识别对了"。
图片这条路如果有兜底（例如没配视觉模型时退化成别的来源），
按"非空即成功"会把兜底算成能力，这正是本指标最容易被高估的地方。
"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import time
import urllib.request
from pathlib import Path

URL = "http://127.0.0.1:8000/api/ingestion/files"


def main(argv: list[str]) -> int:
    path = Path(argv[0] if argv else "scripts/acc_media/image/i1_sketch_map.png")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.open("http://127.0.0.1:8000/api/auth/me", timeout=15).read()
    boundary = "----probe"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        URL, data=head + path.read_bytes() + tail, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    started = time.perf_counter()
    body = json.loads(opener.open(request, timeout=400).read().decode())
    source = body["source"]
    print(f"seconds={round(time.perf_counter() - started, 1)} kind={source['kind']} "
          f"chars={len(source['text'])}")
    print("keys:", list(source))
    print("--- text ---")
    print(source["text"])
    print("--- meta ---")
    print(json.dumps({k: source[k] for k in source
                      if k not in ("text", "sentences")}, ensure_ascii=False)[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
