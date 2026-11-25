"""§四 验收：同一批 (材料, query) 上"固定 3 条" vs "按分数取"的对照。

三列数字都是这一次改动要负责的东西：

    - **取到几条**：`DEEP_BRANCH_KEEP` 把 3 变成 3–6（`scripts/measure_topk_threshold.py`
      量过分数分布，这里量的是取数后果）；
    - **材料总字数 / prompt token**：多给的那几条是**正文**，进 prompt 要付钱。
      token 用 `generation.service.estimate_input_tokens` 折算——生成侧拼 prompt
      时用的就是它，两处用同一个函数才可比；
    - **不同父块数**：同一条父块被给两遍是重复，不是信息（实测 94% 落在不同父块，
      这里在真材料上复核一次）。

耗时也记：`_rerank_depth` 会跟着 `want` 放大候选池，带重排时池子从 12 条变成
24 条（`pool_size(6)=24` vs `pool_size(3)=12`），那是一次 rerank 请求体的差别。

用法（在 `backend/` 下）：

    python scripts/measure_keep_effect.py                 # 纯 BM25（无 key）
    python scripts/measure_keep_effect.py --queries 5     # 每组材料 5 个标题
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.agent.tools import DEEP_BRANCH_KEEP, DETAIL_RETRIEVAL_KEEP, RAG_QUERY_KEEP  # noqa: E402
from app.contracts.source import Source, SourceKind  # noqa: E402
from app.generation.service import estimate_input_tokens  # noqa: E402
from app.knowledge import service  # noqa: E402

DB = BACKEND / "data" / "mindmaps.db"

MATERIALS: dict[str, tuple[str, ...]] = {
    "pdf": ("1706.03762v1.pdf",),
    "attention": ("attention-37973.md",),
    "agent_plan": ("AGENT_PLAN.md",),
    "plan": ("PLAN.md",),
    "thin": ("wikipedia: 碳中和", "openalex: 碳中和、碳达峰的绿色低碳新型工业园区的建设",
             "openalex: 高速铁路节能发展助力碳中和碳达峰"),
}

POLICIES = {
    "branch": (3, DEEP_BRANCH_KEEP),
    "detail": (3, DETAIL_RETRIEVAL_KEEP),
    "rag": (5, RAG_QUERY_KEEP),
}


@dataclass
class Row:
    material: str
    query: str
    top_k: int
    name: str
    before_hits: int = 0
    after_hits: int = 0
    before_chars: int = 0
    after_chars: int = 0
    before_tokens: int = 0
    after_tokens: int = 0
    before_parents: int = 0
    after_parents: int = 0
    before_ms: float = 0.0
    after_ms: float = 0.0
    runs: list[int] = field(default_factory=list)   # 每次（不同 query）改后取到几条


def load_sources(names: tuple[str, ...]) -> list[Source]:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "select id, filename, kind, text from library_sources where filename in "
        f"({','.join('?' * len(names))}) order by length(text) desc", names).fetchall()
    con.close()
    seen: set[str] = set()
    out: list[Source] = []
    for sid, filename, kind, text in rows:
        if not text or filename in seen:
            continue
        seen.add(filename)
        out.append(Source(id=sid, kind=SourceKind(kind), filename=filename,
                          text=text, status="parsed"))
    return out


def outline_queries(sources: list[Source], count: int) -> list[str]:
    titles: list[str] = []
    for group in service.heading_outline_by_source(sources):
        for entry in group:
            title = (entry.title or "").strip()
            if 4 <= len(title) <= 30 and title not in titles:
                titles.append(title)
    return titles[:count]


def text_of(hits, sources: list[Source]) -> str:
    """命中的原文拼起来——这是真正进 prompt 的那部分字。"""
    body = {source.id: (source.text or "") for source in sources}
    return "".join(body.get(hit.source_id, "")[hit.start:hit.end] for hit in hits)


def measure(material: str, sources: list[Source], query: str,
            top_k: int, keep) -> tuple[int, int, int, int, float, int]:
    started = time.perf_counter()
    before = service.search_parent(sources, query, top_k)
    before_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    after = service.search_parent(sources, query, top_k, keep=keep)
    after_ms = (time.perf_counter() - started) * 1000
    before_text, after_text = text_of(before, sources), text_of(after, sources)
    return (len(before), len(after), len(before_text), len(after_text),
            before_ms + after_ms, len(after))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=int, default=5)
    ap.add_argument("--set", default="pdf,attention,agent_plan,plan,thin")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    os.environ["MINDMAP_RERANK"] = "0"

    rows: list[Row] = []
    print(f"{'材料':<12}{'query':<24}{'口径':<8}{'改前':>5}{'改后':>5}"
          f"{'字数前':>7}{'字数后':>7}{'token前':>8}{'token后':>8}{'耗时ms':>8}")
    print("-" * 100)
    for name in args.set.split(","):
        names = MATERIALS.get(name.strip())
        if not names:
            continue
        sources = load_sources(names)
        if not sources:
            print(f"跳过 {name}：库里没有 {names}")
            continue
        for policy_name, (top_k, keep) in POLICIES.items():
            row_set: list[Row] = []
            for query in outline_queries(sources, args.queries):
                before_hits, after_hits, before_chars, after_chars, ms, _ = measure(
                    name, sources, query, top_k, keep)
                row = Row(material=name, query=query, top_k=top_k, name=policy_name,
                          before_hits=before_hits, after_hits=after_hits,
                          before_chars=before_chars, after_chars=after_chars,
                          before_tokens=estimate_input_tokens("甲" * before_chars),
                          after_tokens=estimate_input_tokens("甲" * after_chars),
                          before_parents=1, after_parents=1, before_ms=ms,
                          runs=[after_hits])
                row_set.append(row)
                print(f"{name:<12}{query[:22]:<24}{policy_name:<8}"
                      f"{before_hits:>5}{after_hits:>5}{before_chars:>7}{after_chars:>7}"
                      f"{row.before_tokens:>8}{row.after_tokens:>8}{ms:>8.1f}")
            rows.extend(row_set)

    if rows:
        checks = [row.after_hits / max(row.before_hits, 1) for row in rows]
        char_ratio = (sum(row.after_chars for row in rows)
                      / max(sum(row.before_chars for row in rows), 1))
        print(f"\n条数倍数：中位 {statistics.median(checks):.2f}x "
              f"最小 {min(checks):.2f}x 最大 {max(checks):.2f}x")
        print(f"材料总字数：改前 {sum(row.before_chars for row in rows)} → "
              f"改后 {sum(row.after_chars for row in rows)}（{char_ratio:.2f}x）")
        print(f"prompt token：改前 {sum(row.before_tokens for row in rows)} → "
              f"改后 {sum(row.after_tokens for row in rows)}")
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([asdict(row) for row in rows],
                                   ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
