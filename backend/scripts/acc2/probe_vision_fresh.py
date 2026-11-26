"""P4 的决定性实验：造一张**从没上传过**的图，看视觉链路到底能不能识别。

为什么必须新造：库里已经有过 `i4_outline.png`（别的运行留下的），复用同一份
文件会命中历史解析结果，1.9 秒返回 200 —— 那是缓存的功劳，不是模型的能力。
判定"图像输入真能用"必须排除缓存，所以这里画一张内容全新的图（不同的主题、
不同的层级），并且把耗时记下来（真实视觉调用实测 5–7 秒/页，缓存是毫秒级）。

用法：
    python scripts/acc2/probe_vision_fresh.py
"""
from __future__ import annotations

import http.cookiejar
import json
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8000"
OUT = HERE / "fresh_sketch.png"

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]

#: 一张手绘风格的层级草图（内容全新，含一条表格状的三列结构）。
LINES = [
    (60, 50, "城市内涝治理方案"),
    (110, 120, "一、源头减排"),
    (160, 190, "海绵型小区改造"),
    (200, 250, "透水铺装面积占比 35%"),
    (110, 320, "二、管网提升"),
    (160, 390, "主干管扩容至 DN1200"),
    (200, 450, "雨污分流率从 62% 提到 90%"),
    (110, 520, "三、调蓄与预警"),
    (160, 590, "地下调蓄池 2.4 万立方米"),
    (200, 650, "内涝预警提前 45 分钟"),
]


def make_png() -> Path:
    image = Image.new("RGB", (900, 740), "white")
    draw = ImageDraw.Draw(image)
    font = None
    for candidate in FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(candidate, 22)
            break
        except OSError:
            continue
    for x, y, text in LINES:
        if font is None:
            draw.text((x, y), text, fill="black")
        else:
            draw.text((x, y), text, fill="black", font=font)
    # 连几条线，让它真的像草图而不是一段文本
    for y_from, y_to in ((110, 190), (110, 250), (320, 390), (320, 450), (520, 590), (520, 650)):
        draw.line((95, y_from, 165, y_to), fill="gray", width=2)
    image.save(OUT)
    print(f"made: {OUT} ({OUT.stat().st_size} bytes)")
    return OUT


def session() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f"{BASE}/api/auth/me", timeout=15) as response:
        response.read()
    return opener


def upload(opener, path: Path) -> dict:
    boundary = uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
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
        return {"status": 200, "seconds": elapsed, "kind": source.get("kind"),
                "text": source.get("text"), "chars": len(source.get("text") or ""),
                "source_id": source.get("id")}
    except urllib.error.HTTPError as exc:
        elapsed = round(time.monotonic() - started, 1)
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("detail")
        except json.JSONDecodeError:
            detail = raw
        return {"status": exc.code, "seconds": elapsed, "detail": detail}


def main() -> int:
    path = make_png()
    opener = session()
    result = upload(opener, path)
    print(json.dumps({k: result[k] for k in result if k != "text"}, ensure_ascii=False))
    if result.get("text"):
        print("--- 识别结果 ---")
        print(result["text"])
    out = HERE / "p4_vision_fresh.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
