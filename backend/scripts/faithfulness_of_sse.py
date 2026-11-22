"""量一次 SSE 结果的**骨架忠实度**：一级分支有多少能在本次材料原文里逐字找到。

口径与 `scripts/measure_graph_outline.py::faithfulness` 同源（去空白标点后逐字子串
＋分段＋4gram 三个口径），但它读的是**真实请求存下来的 SSE 帧**而不是进程内
`MindMapContent`——线上那条路径上，这是唯一能事后复核的数字。

    python scripts/faithfulness_of_sse.py .visioncheck/sse-carbon.jsonl

第三个口径（`少/hit`）不宽容、不可糊弄：模板词（「政策目标与原则」）在两份摘要里
一个字都搜不到，而图谱骨架给出的概念名（「碳中和」）必然搜得到。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

KEEP = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
SPLIT = re.compile(r"与其|以及|与|和|及|、|，|,|/|＋|\+")
NGRAM = 4


def main(argv: list[str]) -> int:
    path = Path(argv[0] if argv else ".visioncheck/sse-carbon.jsonl")
    db = Path(argv[1]) if len(argv) > 1 else Path("backend/data/mindmaps.db")
    frames = [json.loads(line) for line in
              path.read_text(encoding="utf-8").splitlines() if line.strip()]
    done = next((frame for frame in frames if frame.get("type") == "done"), None)
    if done is None:
        print("这一份 SSE 里没有 done 帧（生成失败过？）")
        return 2
    labels = [child["label"] for child in done["content"]["root"]["children"]]

    # 材料文本从 `source_usage` 里那几份回查：SSE 结果自带 source_id，不用猜是哪批。
    ids = [item["source_id"] for item in done["content"].get("source_usage") or []]
    texts: list[tuple[str, str]] = []
    conn = sqlite3.connect(str(db))
    marks = ",".join("?" for _ in ids)
    for source_id, filename, body in conn.execute(
            f"SELECT id, filename, text FROM library_sources WHERE id IN ({marks})",
            ids or [""]):
        texts.append((filename or source_id, KEEP.sub("", body or "")))

    hits = 0
    for label in labels:
        squashed = KEEP.sub("", label)
        where = next((name for name, body in texts if squashed and squashed in body), None)
        segments = [KEEP.sub("", piece) for piece in SPLIT.split(label)]
        segments = [piece for piece in segments if len(piece) >= 2]
        segment_hit = bool(segments) and all(
            any(piece in body for _, body in texts) for piece in segments)
        if len(squashed) >= NGRAM:
            windows = [squashed[i:i + NGRAM] for i in range(len(squashed) - NGRAM + 1)]
            ngram = sum(1 for window in windows
                        if any(window in body for _, body in texts)) / len(windows)
        else:
            ngram = 1.0 if squashed and any(squashed in b for _, b in texts) else 0.0
        if where:
            hits += 1
        mark = "逐字命中" if where else ("分段命中" if segment_hit else "未命中")
        print(f"  [{mark}] {label}（4gram {ngram:.2f}）")
    print(f"忠实度 {hits}/{len(labels)} = "
          f"{hits / len(labels):.1%}" if labels else "没有一级分支")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
