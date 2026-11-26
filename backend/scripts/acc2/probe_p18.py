"""P18 前半（向量库）验收：确认检索真的走 SQLite + numpy，而不是 Milvus/Pinecone。

要拿到的三类读数：
  1. **存储**：`mindmaps.db` 里到底有没有向量表、多少行、什么模型、多少字节；
  2. **检索**：真发一次 `/api/knowledge/search`，记录耗时与命中条数；
  3. **算法**：是不是 numpy 一次性矩阵乘（静态定位那行 + 运行期把矩阵形状读出来）。

判定口径来自 `docs/OPTIMIZATION-CROSS-MAP.md` §2.1：numpy 暴力扫 top-10 是
9.5–21.6 ms（1000×1024），换 Milvus Lite 是 179 ms + 119 MB。所以只要这里量到
的量级与 numpy 一侧一致，"没装 Milvus"就是被数据支持的实现选择，而不是缺件。

用法：
    python scripts/acc2/probe_p18.py
"""
from __future__ import annotations

import http.cookiejar
import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1]
DB = BACKEND / "data" / "mindmaps.db"
BASE = "http://127.0.0.1:8000"


def tables() -> dict:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    names = [row[0] for row in conn.execute(
        "select name from sqlite_master where type='table' order by name")]
    counts = {}
    for name in names:
        try:
            counts[name] = conn.execute(f'select count(*) from "{name}"').fetchone()[0]
        except sqlite3.Error as exc:  # pragma: no cover - 只用于体检
            counts[name] = f"ERR {exc}"
    detail = {}
    if "chunk_vectors" in names:
        columns = [row[1] for row in conn.execute('pragma table_info("chunk_vectors")')]
        model_rows = conn.execute(
            "select model, count(*), sum(length(vector)) from chunk_vectors "
            "group by model").fetchall() if "model" in columns else []
        detail["chunk_vectors"] = {
            "columns": columns,
            "by_model": [
                {"model": row[0], "rows": row[1], "vector_bytes": row[2]}
                for row in model_rows
            ],
        }
    conn.close()
    return {"tables": counts, "detail": detail}


def session() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f"{BASE}/api/auth/me", timeout=15) as response:
        response.read()
    return opener


def search(opener, query: str, limit: int = 10) -> dict:
    # 字段名是 `top_k`（`SearchRequest`），不是 `limit`：写错会被 422 拒掉，
    # 而错误信息只说 "extra_forbidden"，很容易误读成"接口没实现"。
    payload = json.dumps({"query": query, "top_k": limit}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}/api/knowledge/search", data=payload, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    started = time.monotonic()
    try:
        with opener.open(request, timeout=120) as response:
            body = json.loads(response.read().decode("utf-8"))
        elapsed = round((time.monotonic() - started) * 1000, 1)
        hits = body.get("hits") or body.get("results") or []
        return {
            "status": 200,
            "ms": elapsed,
            "keys": sorted(body),
            "hit_count": len(hits),
            "first_hits": [
                {k: hit.get(k) for k in ("source_id", "score", "title", "text")
                 if k in hit}
                for hit in hits[:3]
            ],
        }
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "raw": exc.read().decode("utf-8", "replace")[:400]}
    except urllib.error.URLError as exc:
        return {"status": "network", "raw": str(exc)[:200]}


def main() -> int:
    report: dict = {"db": tables()}
    print("== tables:", json.dumps(report["db"]["tables"], ensure_ascii=False))
    print("== vector detail:", json.dumps(report["db"]["detail"], ensure_ascii=False)[:600])

    opener = session()
    report["search"] = {}
    for query in ("缓存命中率对查询延迟的影响", "订单查询接口超时"):
        result = search(opener, query)
        report["search"][query] = result
        print(f"== search[{query}] -> {result.get('status')} "
              f"{result.get('ms')}ms hits={result.get('hit_count')}")

    # 算法层：numpy 矩阵乘那一行到底在哪、长什么样
    cross = (BACKEND / "app" / "optimization" / "cross_map.py").read_text(encoding="utf-8")
    lines = [
        {"line": index + 1, "text": text.strip()}
        for index, text in enumerate(cross.splitlines())
        if "@" in text and ("base" in text or "queries" in text)
    ]
    vector_cache = (BACKEND / "app" / "knowledge" / "vector_cache.py").read_text(
        encoding="utf-8")
    report["algorithm"] = {
        "cross_map_matrix_lines": lines[:6],
        "sqlite_in_vector_cache": "sqlite3" in vector_cache,
        "numpy_in_vector_cache": "numpy" in vector_cache,
        "float32": "float32" in vector_cache,
    }
    print("== algorithm:", json.dumps(report["algorithm"], ensure_ascii=False)[:600])

    path = HERE / "p18_vector.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
