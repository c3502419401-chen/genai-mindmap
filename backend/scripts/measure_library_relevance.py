"""知识库在**真主题**上的检索相关性实测（跑真库，不需要任何密钥也能跑）。

回答一个问题：**库里那 51 份材料，碰上用户随手输入的主题，检索召回的东西相关吗。**
这一条决定了「生成时由代码主动查库」值不值得做——库命中如果不相关，那个功能只是
把噪声从「外部检索」搬到「库」。

## 口径

  1. **库是什么**：`library_sources` 现状（份数 / 字数 / kind 分布 / 切块数）。没有
     一份是用户真的上传的——4 份是 `docs/` 下的开发文档，47 份是外部检索抓回来的
     web / paper 正文。这一点是后面所有数字的解释边界。
  2. **粗筛**：`_library_sources` 只按字面覆盖率取前 `LIBRARY_PICK=5` 份参与切块。
     真实漏召回的**大部分**发生在这里，而不是在打分阶段——所以这张表先报"哪 5 份
     进了候选池、覆盖率多少"，再报最终 hits。
  3. **检索**：`search_parent([], query, top_k=8, library=True)`，与生成链里
     `_rag` 的调用形状完全一致。同时报 `method`（`lexical` / `hybrid`）：embedding
     挂掉时整条向量路会降级成纯 BM25，那是**另一条曲线**，必须分开记。
  4. **耗时**：每个主题跑 1 次冷启动 + 2 次取中位。库检索是本地 + 可能的 embedding
     往返，它是否"0.1 秒级"直接决定能不能放进 `_intake`。

## 用法（在 `backend/` 下）

    python scripts/measure_library_relevance.py
    python scripts/measure_library_relevance.py --top-k 8 --rounds 3

**它会 `touch()` 命中的库来源**（`_library_batch` 就是这样），于是 `last_used_at`
被刷新、`recent()` 顺序变化。库只有 51 份、`LIBRARY_SCAN_LIMIT=100`，全部都在窗口
内，所以只影响"覆盖率相同的来源之间谁先"这一档，不改最终名次。
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.knowledge import library  # noqa: E402
from app.knowledge.service import (  # noqa: E402
    LIBRARY_PICK, _literal_coverage, _token_list, search_parent,
)

#: 5 个主题，按"与库里内容的重叠程度"从高到低排：
#:
#:   - 前两个贴着 `docs/` 那 4 份开发文档的用词（检索材料本来就该命中）；
#:   - 中间两个是**普通用户的真主题**（机器学习 / 碳中和），库里没有任何一份是
#:     为它们准备的——它们测的是「库里没东西时会不会硬塞一堆噪声」；
#:   - 最后一个是具体的工程题目（城市雨污分流改造），比前两个更窄。
TOPICS = [
    "思维导图生成的检索怎么分块",
    "生成思维导图时外部检索的准确性",
    "机器学习的主要方向",
    "碳中和的主要方面",
    "城市雨污分流改造",
]

#: **负样本组**：库里确实没有这些主题的材料（或只有一两个词形巧合）。它们测的是
#: 「这次该不该用库」这个判断本身——一个只会在库里找到东西的功能，在库里有东西
#: 的时候表现好是平凡的；它会不会在**没东西的时候硬塞**才是能不能接线的前提。
NEGATIVE_TOPICS = [
    "如何做红烧肉",
    "量子计算的基本原理",
    "小型咖啡机怎么选",
    "注意力机制原理",
    "海绵城市建设",
]


def _library_overview() -> None:
    rows = library.list_sources(limit=200)
    chars = sum(row["chars"] for row in rows)
    chunks = sum(row["chunks"] for row in rows)
    by_kind: dict[str, list[int]] = {}
    for row in rows:
        by_kind.setdefault(row["kind"], []).append(row["chars"])
    print("=" * 78)
    print(f"库现状：{len(rows)} 份 / {chars} 字 / {chunks} 块")
    for kind, values in sorted(by_kind.items(), key=lambda item: -len(item[1])):
        print(f"  {kind:10} {len(values):>3} 份 / {sum(values):>7} 字"
              f"（单份中位 {int(statistics.median(values))} 字）")
    print("-" * 78)
    for row in sorted(rows, key=lambda item: -item["chars"]):
        print(f"  {(row['filename'] or row['id'])[:50]:50}"
              f"{str(row['kind']).replace('SourceKind.', ''):9}"
              f"{row['chars']:>7} 字 {row['chunks']:>4} 块")
    print("=" * 78)


def _embedding_probe() -> None:
    """向量路现在到底活着吗：跑一次 `_vector_scores` 并打印它给的原因。

    生产上 40 批 chunk 向量请求曾经全失败，那条路会静默降级成纯 BM25——同一份
    材料、同一个 query，两档的命中完全不同，所以每次测量都必须先确认是哪一档。
    """
    from app.knowledge.service import _vector_scores

    similarity, reason = _vector_scores(["机器学习的主要方向", "城市雨污分流改造"], "机器学习的主要方向")
    print(f"向量路探针：{len(similarity)} 个向量 / 原因={reason or '(可用)'}")
    print("=" * 78)


def _probe(topic: str, top_k: int, rounds: int) -> None:
    terms = list(dict.fromkeys(_token_list(topic)))
    # 粗筛复现：`_library_sources` 的排序键就是字面覆盖率，这里按同一顺序重算，
    # 打印"哪几份进了候选池"。不吃库的 `touch`，不产生副作用。
    pool = library.recent(limit=100)
    ranked = sorted(pool, key=lambda item: -_literal_coverage(terms, item.text or ""))
    picked = ranked[:LIBRARY_PICK]

    print(f"\n主题：{topic}")
    print(f"  查询词：{'/'.join(terms[:12])}{'…' if len(terms) > 12 else ''}")
    print(f"  粗筛（LIBRARY_PICK={LIBRARY_PICK}）：")
    for item in picked:
        cov = _literal_coverage(terms, item.text or "")
        print(f"    · 覆盖 {cov:>4.2f}  {(item.filename or item.id)[:44]:44}"
              f"{item.kind:9}{len(item.text or ''):>7} 字")
    missed = [item for item in ranked[LIBRARY_PICK:] if _literal_coverage(terms, item.text or "") > 0]
    if missed:
        print(f"  粗筛外仍有 {len(missed)} 份覆盖率 > 0（被 LIBRARY_PICK 挤掉）：")
        for item in missed[:5]:
            print(f"    · 覆盖 {_literal_coverage(terms, item.text or ''):>4.2f}"
                  f"  {(item.filename or item.id)[:44]:44}{item.kind}")

    timings: list[float] = []
    hits = []
    for index in range(rounds):
        started = time.perf_counter()
        hits = search_parent([], topic, top_k, library=True)
        elapsed = (time.perf_counter() - started) * 1000
        # 第一轮含冷启动（读库 + 首次 embedding），中位数取后面几轮；只有一轮时
        # 只能记它自己，否则中位数的输入集是空的。
        if index or rounds == 1:
            timings.append(elapsed)
    print(f"  命中 {len(hits)} 条（top_k={top_k}，中位 {statistics.median(timings):.0f} ms）")
    for hit in hits:
        loaded = library.load(hit.source_id)
        name = (loaded.filename if loaded else hit.source_id)[:38]
        kind = loaded.kind if loaded else "?"
        head = " ".join((hit.quote or "").split())[:80]
        # `score` 是 RRF 名次分（1/(60+rank)），**没有绝对相关性含义**：第 1 名
        # 恒为 0.0164、第 8 名恒为 0.0147，不管内容多相关。判断相关性只能看
        # `score_bm25`（>0 = 词面命中）与 `score_vector`（余弦）。
        print(f"    · {name:38} {str(kind).replace('SourceKind.', ''):8} "
              f"rrf={hit.score:.4f} bm25={(hit.score_bm25 or 0):6.2f} "
              f"cos={(hit.score_vector or 0):.3f} "
              f"rerank={'-' if hit.rerank_score is None else f'{hit.rerank_score:.3f}':>5} "
              f"{hit.method:7} [{hit.start:>6},{hit.end:>6})")
        print(f"      {head}")


def _gate_view(topic: str, top_k: int, gate: float) -> None:
    """按 `score_bm25 >= gate` 过一道门之后还剩什么。

    `_intake` 里那道门必须**只用一次检索的产物**就能算出来，否则它要么多花一次
    调用，要么依赖人工。`score_bm25` 正好符合：它由查询与块共同决定（不是名次），
    0 表示一个字都没命中，> gate 表示词面确实沾上。这一节给出的就是"过门之后
    还剩几条 / 几份来源 / 多少字"——生成侧真正要回答的是最后那个字数。
    """
    hits = [hit for hit in search_parent([], topic, top_k, library=True)
            if (hit.score_bm25 or 0) >= gate]
    sources = {hit.source_id for hit in hits}
    chars = sum(hit.end - hit.start for hit in hits)
    print(f"  过门 bm25≥{gate}：{len(hits)} 条 / {len(sources)} 份 / {chars} 字")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--gate", type=float, default=5.0)
    parser.add_argument("--topics", default="",
                        help="只跑逗号分隔的指定主题；缺省跑主表 + 负样本组")
    args = parser.parse_args()

    print(f"库文件：{os.getenv('MINDMAP_DB_PATH') or '(默认 backend/data/mindmaps.db)'}")
    print(f"embedding：{'已配' if os.getenv('MINDMAP_EMBEDDING_API_KEY') else '未配'}"
          f"  rerank：MINDMAP_RERANK={os.getenv('MINDMAP_RERANK', '(默认)')}")
    _library_overview()
    _embedding_probe()
    topics = ([item.strip() for item in args.topics.split(",") if item.strip()]
              if args.topics else [*TOPICS, *NEGATIVE_TOPICS])
    for topic in topics:
        _probe(topic, args.top_k, args.rounds)
        _gate_view(topic, args.top_k, args.gate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
