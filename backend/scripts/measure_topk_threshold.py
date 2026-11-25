"""§一 量测：`top_k` 截断到底漏了什么——分数分布的形态、第 4–10 条的真实差距。

问题只有一句：**现在按"排序后取前 N 条"取数，被砍掉的那 7 条是不是只差一点点？**
如果差很多（断崖在 3 附近），固定 `top_k=3` 就是对的；如果前 20 条挤在一个区间，
那"分数差不多就都拼给模型"才成立。

三条口径都必须记，否则数字互相不可比（`service.EvidenceHit` 的注释里写死了这件事）：

  - **父块口径**：`_search_parent_core` 的回给调用方的形状（每条不同父块、
    `start/end` 是父块、`score` 来自子块匹配）；
  - **子块口径**：折叠之前的那批命中，用来看"第 4–10 条里有多少条只是同一个
    父块的另一截"（父块口径下这一问恒为 0，因为它已经去过重了）；
  - **分数的量纲**：hybrid 时 `score` 是 RRF 分（名次函数，没有"差不多"可言），
    纯 BM25 时 `score` 是 BM25 原始分。两者分别跑一遍。

用法（在 `backend/` 下）：

    python scripts/measure_topk_threshold.py                    # hybrid（有 key 时）
    python scripts/measure_topk_threshold.py --bm25-only        # 强制纯 BM25
    python scripts/measure_topk_threshold.py --depth 60 --json data/measure_topk.json

不吃生成、不吃 LLM：只调 `service` 的取数层。因此本脚本可以在 30 秒内跑完。
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

from app.contracts.source import Source, SourceKind  # noqa: E402
from app.knowledge import service  # noqa: E402

DB = BACKEND / "data" / "mindmaps.db"

#: 材料集：库里真实存在的正文（不是构造的短样本）。
#:
#: 四份"厚材料"对应 `docs/RAG-DECISIONS.md` 2.1 那张表的角色：一份英文 PDF、
#: 三份中文长文。薄批是碳中和那 4 份外部检索产物（合计 670 字，每份 89–214 字）——
#: 它代表"材料本身比 top_k 还小"的那一端，阈值在那种形状上不能把结果砍成 0 条。
MATERIALS: dict[str, tuple[str, ...]] = {
    "pdf": ("1706.03762v1.pdf",),
    "attention": ("attention-37973.md",),
    "agent_plan": ("AGENT_PLAN.md",),
    "plan": ("PLAN.md",),
    "thin": ("wikipedia: 碳中和", "openalex: 碳中和、碳达峰的绿色低碳新型工业园区的建设",
             "openalex: 高速铁路节能发展助力碳中和碳达峰"),
}


@dataclass
class Run:
    """一次 (材料, query) 的全部观测。"""

    material: str
    query: str
    method: str
    chars: int                       # 材料总字数
    parents_total: int               # 折父块后的可用父块总数（全量，未截断）
    parent_rows: list[list] = field(default_factory=list)  # [rank, score, bm25, vec, 块字数]
    leaf_parent_keys: list[str] = field(default_factory=list)  # 前 N 条子块各自落在哪个父块
    new_parents_4_10: int = 0        # 第 4–10 条（子块口径）里落进**新**父块的条数
    dupe_leaves_4_10: int = 0        # 第 4–10 条里与 top3 同父块（只是另一截）
    new_parent_keys_4_10: int = 0    # 子块口径第 4–10 条覆盖到的、top3 之外的新父块个数
    seconds: float = 0.0


def load_sources(names: tuple[str, ...]) -> list[Source]:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "select id, filename, kind, text from library_sources where filename in "
        f"({','.join('?' * len(names))}) order by length(text) desc",
        names).fetchall()
    con.close()
    seen: set[str] = set()
    sources: list[Source] = []
    for sid, filename, kind, text in rows:
        if not text or filename in seen:
            continue
        seen.add(filename)
        sources.append(Source(id=sid, kind=SourceKind(kind), filename=filename,
                              text=text, status="parsed"))
    return sources


def outline_queries(sources: list[Source], count: int) -> list[str]:
    """从正文取真实的小节标题当 query——它们是**分支标题**在生产里的同一形态。"""
    titles: list[str] = []
    for group in service.heading_outline_by_source(sources):
        for entry in group:
            title = (entry.title or "").strip()
            if 4 <= len(title) <= 30 and title not in titles:
                titles.append(title)
    return titles[:count]


def measure(material: str, sources: list[Source], query: str, depth: int) -> Run:
    started = time.perf_counter()
    corpus = service._collect_leaves(sources)
    # 与 `_search_parent_core` 同构，只是把返回条数放大到 depth：那一层的候选池
    # 是 `want * CANDIDATE_FACTOR`，这里照抄，否则"全量"其实是被池子卡过的全量。
    pool = max(depth * service.CANDIDATE_FACTOR, depth)
    ranked_leaves = service._fuse(corpus.items, query, pool)
    folded = service._fold_to_parents(ranked_leaves, corpus)
    elapsed = time.perf_counter() - started

    run = Run(material=material, query=query,
              method=ranked_leaves[0].method if ranked_leaves else "none",
              chars=sum(len(source.text or "") for source in sources),
              parents_total=len(folded), seconds=round(elapsed, 3))
    for rank, hit in enumerate(folded[:depth], start=1):
        run.parent_rows.append([rank, hit.score, hit.score_bm25, hit.score_vector,
                               len(hit.quote)])

    leaf_to_parent: dict[tuple[int, int], str] = {}
    for index, (_sid, start, end, _quote) in enumerate(corpus.items):
        parent = corpus.parents[index]
        leaf_to_parent[(start, end)] = f"{parent[0]}-{parent[1]}"

    for hit in ranked_leaves[:depth]:
        run.leaf_parent_keys.append(leaf_to_parent[(hit.start, hit.end)])
    top3 = set(run.leaf_parent_keys[:3])
    tail = run.leaf_parent_keys[3:10]
    run.dupe_leaves_4_10 = sum(1 for key in tail if key in top3)
    run.new_parents_4_10 = len(tail) - run.dupe_leaves_4_10
    run.new_parent_keys_4_10 = len({key for key in tail if key not in top3})
    return run


def curve(scores: list[float]) -> dict[str, float]:
    """分布形态：断崖还是平台。

    三个判据都是**比值**（不是差值），因为 RRF 与 BM25 的量纲差几十倍，差值不可比：
      - `r3` / `r10` / `r20`：第 3/10/20 条相对第 1 条的保留比例；
      - `drop_3to4`：第 4 条相对第 3 条跌掉的比例，`drop_2to3` 同理；
      - `cliff_at`：单步跌幅最大的位置（1 起）。"断崖在 3 附近"就是 `cliff_at <= 4`。
    """
    if len(scores) < 2 or scores[0] <= 0:
        return {}
    def at(index: int) -> float | None:
        return scores[index] if len(scores) > index else None
    drops = [(scores[i] - scores[i + 1]) / scores[i]
             for i in range(min(len(scores) - 1, 30)) if scores[i] > 0]
    cliff = (drops.index(max(drops)) + 1) if drops else 0
    return {
        "r3": round(at(2) / scores[0], 4) if at(2) is not None else None,
        "r10": round(at(9) / scores[0], 4) if at(9) is not None else None,
        "r20": round(at(19) / scores[0], 4) if at(19) is not None else None,
        "drop_2to3": round((scores[1] - scores[2]) / scores[1], 4) if at(2) else None,
        "drop_3to4": round((scores[2] - scores[3]) / scores[2], 4) if at(3) else None,
        "cliff_at": cliff,
        "max_drop": round(max(drops), 4) if drops else None,
    }


def summarize(runs: list[Run], depth: int) -> None:
    def num(value: float | None) -> str:
        return "  -  " if value is None else f"{value:>7.3f}"

    print(f"\n{'材料':<12}{'query':<26}{'method':<8}{'父块':>4}"
          f"{'r3':>7}{'r10':>7}{'r20':>7}{'3→4跌':>8}{'断崖位':>7}{'新父块':>7}")
    print("-" * 100)
    gaps: list[float] = []
    for run in runs:
        scores = [row[1] for row in run.parent_rows]
        stats = curve(scores)
        if stats and stats.get("drop_3to4") is not None:
            gaps.append(stats["drop_3to4"])
        print(f"{run.material:<12}{run.query[:24]:<26}{run.method:<8}"
              f"{run.parents_total:>4}"
              f"{num(stats.get('r3'))}{num(stats.get('r10'))}{num(stats.get('r20'))}"
              f"{num(stats.get('drop_3to4'))}"
              f"{stats.get('cliff_at', 0):>7}"
              f"{run.new_parent_keys_4_10:>7}")
    if gaps:
        print(f"\n第 3→4 条的跌幅：中位 {statistics.median(gaps):.3f} "
              f"最小 {min(gaps):.3f} 最大 {max(gaps):.3f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=int, default=60)
    ap.add_argument("--queries", type=int, default=5)
    ap.add_argument("--set", default="pdf,attention,agent_plan,plan,thin")
    ap.add_argument("--bm25-only", action="store_true")
    ap.add_argument("--rerank", action="store_true")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    if args.bm25_only:
        os.environ[service.HYBRID_ENV] = "0"
    # 量的是**取数层**的原始分布，所以默认把重排关掉：`MINDMAP_RERANK=1` 时
    # `_rerank_depth` 会把候选池放宽到 `pool_size(top_k)` 并只留 top_k 条，
    # 那一层会把"第 4 条差多少"整个换掉（它给的是一份重排后的前 N 名）。
    # 要看重排分的分布，另跑 `--rerank`。
    os.environ["MINDMAP_RERANK"] = "1" if args.rerank else "0"

    runs: list[Run] = []
    env = {"hybrid": service._hybrid_enabled(),
           "rerank": os.getenv("MINDMAP_RERANK", "0"),
           "depth": args.depth}
    print("环境：", json.dumps(env, ensure_ascii=False))
    for name in args.set.split(","):
        names = MATERIALS.get(name.strip())
        if not names:
            print(f"跳过未知材料集 {name}")
            continue
        sources = load_sources(names)
        if not sources:
            print(f"跳过 {name}：库里没有 {names}")
            continue
        queries = outline_queries(sources, args.queries)
        if not queries:
            print(f"跳过 {name}：正文里没有可用的小节标题")
            continue
        for query in queries:
            run = measure(name, sources, query, args.depth)
            runs.append(run)
            print(f"  {name:<12}{query[:30]:<32}{run.method:<8}"
                  f"父块 {run.parents_total:>3}  命中 {len(run.parent_rows):>3}  "
                  f"{run.seconds:.2f}s")

    summarize(runs, args.depth)
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"env": env,
                                    "runs": [asdict(run) for run in runs]},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
