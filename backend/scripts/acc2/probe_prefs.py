"""P6 的第四子项（偏好学习与风格推荐）真跑：写一条偏好、读建议。

核对报告说它是"显式写入 + 近 3 次一致的规则"，不是模型学习。这里把这句话变成
读数：写入前 / 写入后各读一次 `/api/prefs/suggestions`，看建议是怎么来的。

用法：
    python scripts/acc2/probe_prefs.py
"""
from __future__ import annotations

import http.cookiejar
import json
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8000"


def session() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f"{BASE}/api/auth/me", timeout=15) as response:
        response.read()
    return opener


def call(opener, path: str, method: str = "GET", body: dict | None = None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with opener.open(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def main() -> int:
    opener = session()
    report: dict = {}
    for step, (method, path, body) in {
        "prefs_before": ("GET", "/api/prefs", None),
        "suggestions_before": ("GET", "/api/prefs/suggestions", None),
    }.items():
        status, payload = call(opener, path, method, body)
        report[step] = {"status": status, "payload": payload}
        print(f"== {step} -> {status} {json.dumps(payload, ensure_ascii=False)[:300]}")

    # 写入是 `PUT /api/prefs/{key}`（不是 POST —— POST 会 405）。
    for attempt, value in enumerate(("flat", "flat", "flat"), start=1):
        status, payload = call(opener, "/api/prefs/layout.depth", "PUT", {"value": value})
        print(f"== write {attempt} -> {status} {json.dumps(payload, ensure_ascii=False)[:200]}")
        report[f"write_{attempt}"] = {"status": status, "payload": payload}

    status, payload = call(opener, "/api/prefs/suggestions", "GET")
    report["suggestions_after"] = {"status": status, "payload": payload}
    print(f"== suggestions_after -> {status} {json.dumps(payload, ensure_ascii=False)[:600]}")

    path = HERE / "p6_prefs.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
