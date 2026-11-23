"""外部检索「取数策略」的实测脚本：候选池 → 特征区分力 → 排序策略 → limit 扫描。

## 为什么要有这个脚本

任务要改三件事：去掉"每源保底"、改成按相关度排 + 去重、把 `DEFAULT_LIMIT` 从 3 提高。
三件事都依赖同一个前提——**得先证明有一个判据能把"相关"和"不相关"分开**。分不开时
提高 limit 只是多拿垃圾；按一个不可靠的分数排序只是把"每源保底"换成一个更坏的规则。

所以这个脚本只做一件事的两种时态：**先量，再改**。

    python scripts/measure_external_rank.py --collect     # 联网一次：写候选池
    python scripts/measure_external_rank.py --sheet       # 离线：出人工判定用的一览表
    python scripts/measure_external_rank.py --features    # 离线：六个特征各自的区分力
    python scripts/measure_external_rank.py --sweep       # 离线：limit 3/6/8/12 五指标
    python scripts/measure_external_rank.py --diversity   # 离线：源分布与内容重复度
    python scripts/measure_external_rank.py --skeleton    # 联网（LLM）：骨架支数 / 判废率
    python scripts/measure_external_rank.py --live        # 联网（源 + LLM）：真主题实测

## 候选池的口径（决定了下面每个数能说明什么）

`--collect` 拦的是 `external._search_one`——那是**过门之前、截断之前**的全部候选，
六个源、每源上限 `CAP`。也就是说池子回答的是"这次检索源站到底给了什么"，而不是
"生产最后留下了什么"。改判据、改排序、改 limit 全部在这同一份池子上重放，差异只
可能来自策略本身（与 `measure_external_relevance.py` 同一条理由）。

一处必须说清的偏差：**生产的每源上限等于当次的 `limit`**，而池子是用 `CAP` 抓的。
所以"从池子里取前 3 条"与"生产 limit=3 时真实跑一遍"不是同一件事——后者源站只会
返回 3 条候选。`--live` 那一支补的就是这个：它真的按 `limit` 去搜。

## 人工判定

`measure_external_rank.judged.json`：`{主题: [[标题子串, 判定, 依据], ...]}`，判定取
`relevant` / `edge` / `noise`，按顺序第一条命中生效。**依据必须写**——"这条为什么算
相关"是这张表唯一不能自动化、也唯一会被后人复核的部分。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Sequence

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

# 每一条读数都建立在"这次真打了一轮网"上，所以缓存必须关（同 `measure_external_relevance`）。
os.environ.setdefault("MINDMAP_EXTERNAL_CACHE", "0")

from app.knowledge import external  # noqa: E402

HERE = Path(__file__).resolve().parent
POOL = HERE / "measure_external_rank.pool.jsonl"
JUDGED_PATH = HERE / "measure_external_rank.judged.json"
SKELETON_OUT = HERE / "measure_external_rank.skeleton.jsonl"
LIVE_OUT = HERE / "measure_external_rank.live.jsonl"

# 每源候选上限。**必须 ≥ 最大被扫描的 limit**：池子比 limit 窄时，"limit=12 只能拿到
# 8 条"会被误读成"源里只有 8 条"。
CAP = 12
# 抓池子用的预算：12 条维基正文 + 12 条 DeepSeek 补抓，13 次那个生产预算不够用。
COLLECT_REQUEST_LIMIT = 80
COLLECT_DEADLINE_SECONDS = 300.0

# 主题。前三个是任务点名的（都有已知坏样本），后四个是历史台账里反复出现的坏样本
# （同形异义 / 跨语言 / 品牌名），用来让"区分力"这张表不只被三个主题撑起来。
TOPICS: list[tuple[str, str]] = [
    ("机器学习", "任务点名：deepseek 抓回教育平台课程页"),
    ("碳中和的主要方面", "任务点名：五源混着越南媒体与政府网站"),
    ("城市雨污分流改造", "任务点名：政府网站混着"),
    ("最终一致性", "历史坏样本：晶体学论文里写着「最终一致性因子」"),
    ("rust 学习路线", "历史坏样本：陳淑樺 / 教学模式论文"),
    ("Cursor", "历史坏样本：光标论文"),
    ("幻觉", "历史坏样本：戏曲与决定论论文"),
]

# 判定用的文本长度。与池子落盘一致：判据只看前 `RELEVANCE_CHARS`（300）字，人工判定
# 也只需要看清开头讲什么。留全文会让这个可入库的复现工件变成几十万字的他人正文副本。
POOL_TEXT_CHARS = 600

CJK = re.compile(r"[\u4e00-\u9fff]")


# ---------------------------------------------------------------- 判定表


_RULES: dict[str, list[tuple[str, str, str]]] = {}
_VERDICTS: dict[tuple[str, str], str] = {}


def load_judged() -> None:
    if not JUDGED_PATH.exists():
        print(f"!! 判定表缺席：{JUDGED_PATH}（所有命中都会记为未判定）")
        return
    payload = json.loads(JUDGED_PATH.read_text(encoding="utf-8"))
    for topic, rules in payload.items():
        if topic.startswith("_"):
            continue
        _RULES[topic] = [(str(needle), str(verdict), str(reason))
                         for needle, verdict, reason in rules]


def verdict_of(topic: str, title: str) -> str | None:
    """查人工判定：先精确，再按子串顺序匹配；都没有返回 None（未判定）。"""
    if (topic, title) in _VERDICTS:
        return _VERDICTS[(topic, title)]
    for needle, verdict, _reason in _RULES.get(topic, []):
        if needle in title:
            _VERDICTS[(topic, title)] = verdict
            return verdict
    return None


def reason_of(topic: str, title: str) -> str:
    for needle, _verdict, reason in _RULES.get(topic, []):
        if needle in title:
            return reason
    return ""


# ---------------------------------------------------------------- 特征


def _phrase_hits(query: str, blob: str) -> list[str]:
    return sorted({p for p in external._phrase_terms(query) if p in blob})


def _word_hits(query: str, blob: str) -> list[str]:
    return sorted(external._word_terms(query) & external._word_terms(blob))


def _count_terms(terms: Sequence[str], text: str) -> int:
    return sum(text.count(term) for term in terms)


def gate_branch(query: str, hit: external.ExternalHit) -> str:
    """`_relevant_hits` 的判定过程，逐个分支还原出来。

    这条函数存在的唯一理由是要回答一个具体问题：**一条明显跑题的命中，是走哪一支
    进来的**。三支：`phrase`（正文/标题里有 query 的中文词块）、`word`（有英文词）、
    `failopen`（两边书写系统没有交集，判据弃权）、`no-terms`（query 切不出词面）。
    """
    phrases = external._phrase_terms(query)
    words = external._word_terms(query)
    if not phrases and not words:
        return "no-terms"
    blob = external._hit_blob(hit)
    if _phrase_hits(query, blob):
        return "phrase"
    if _word_hits(query, blob):
        return "word"
    if bool(external._CJK_RUN_RE.search(blob)) != bool(external._CJK_RUN_RE.search(query)):
        return "failopen"
    return "dropped"


def features(query: str, hit: external.ExternalHit) -> dict[str, Any]:
    """一条命中的全部被测特征。**名字与 §一 那张表一一对应。"""
    phrases = sorted(external._phrase_terms(query))
    query_tokens = [t for t in external._token_list(query) if t.strip()]
    title_tokens = {t for t in external._token_list(hit.title) if t.strip()}
    blob = external._hit_blob(hit)
    body = hit.text or ""
    # 词频数的是**正文全文**里的出现次数（不是前 300 字）：这一列要回答的正是
    # "只出现在开头一次"与"通篇反复出现"有没有区别。
    body_count = _count_terms(phrases, body)
    return {
        "source": hit.source_name,
        "title": hit.title,
        "chars": len(body),
        "raw_chars": hit.raw_chars or len(body),
        # a) 词面门
        "gate": gate_branch(query, hit) != "dropped",
        "gate_branch": gate_branch(query, hit),
        # b) 标题与 query 的词面重合度（bigram + 英文词，按 query 侧覆盖率算）
        "title_cov": (sum(1 for t in query_tokens if t in title_tokens) / len(query_tokens)
                      if query_tokens else 0.0),
        "title_phrase": any(p in hit.title for p in phrases),
        # c) 正文里 query 词块出现的**次数**（全文 / 前 300 字）
        "body_count": body_count,
        "blob_count": _count_terms(phrases, blob),
        # d) 正文长度已在上面的 `chars`；再留一个"是否短到一句话"的标记
        "short": len(body) < 200,
        # f) deepseek 自带的重排分（五源没有）
        "rerank": hit.rerank_score,
        "authority": hit.authority,
        "site": hit.site,
    }


# ---------------------------------------------------------------- 抓池子


class _Capture:
    """拦 `_search_one`，留下**过门之前、截断之前**的全部候选。"""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._original = external._search_one
        # 每源候选宽度：生产的 `SOURCE_CANDIDATES=5` 是给"最终只要 3 条"配的，
        # 这里要扫到 limit=12，所以把源侧的 ask 一起放大（`_openalex` / `_crossref`
        # 读的是这个模块级常量）。
        self._candidates = external.SOURCE_CANDIDATES

    def __enter__(self) -> "_Capture":
        capture = self

        def wrapped(query: str, limit: int, **kwargs):
            hits = capture._original(query, limit, **kwargs)
            capture.rows.append({"query": query, "limit": limit,
                                 "hits": list(hits)})
            return hits

        external._search_one = wrapped
        external.SOURCE_CANDIDATES = CAP
        return self

    def __exit__(self, *exc: Any) -> None:
        external._search_one = self._original
        external.SOURCE_CANDIDATES = self._candidates


def _spec(hit: external.ExternalHit) -> dict[str, Any]:
    return {
        "source_name": hit.source_name,
        "title": hit.title,
        "text": (hit.text or "")[:POOL_TEXT_CHARS],
        "url": hit.url,
        "language": hit.language,
        "raw_chars": hit.raw_chars,
        "site": hit.site,
        "authority": hit.authority,
        "rerank_score": hit.rerank_score,
        "direction": hit.direction,
    }


def collect() -> int:
    rows: list[dict[str, Any]] = []
    for topic, kind in TOPICS:
        budget = external._Budget(COLLECT_REQUEST_LIMIT, COLLECT_DEADLINE_SECONDS)
        with _Capture() as capture:
            started = time.perf_counter()
            external._search_one(topic, CAP, budget=budget)
            elapsed = time.perf_counter() - started
        hits = [hit for pool in capture.rows for hit in pool["hits"]]
        row = {
            "topic": topic,
            "kind": kind,
            "seconds": round(elapsed, 2),
            "requests": budget.used,
            "failures": budget.failures,
            "count": len(hits),
            "by_source": {name: sum(1 for h in hits if h.source_name == name)
                          for name in external.DEFAULT_SOURCES},
            "features": [features(topic, hit) for hit in hits],
            "pool": [_spec(hit) for hit in hits],
        }
        rows.append(row)
        print(f"{topic:16s} 候选 {len(hits):2d} 条  {elapsed:5.2f}s  请求 {budget.used} 次  "
              f"{row['by_source']}", flush=True)
    POOL.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")
    print(f"\n写盘：{POOL}（{len(rows)} 个主题，"
          f"{sum(r['count'] for r in rows)} 条候选）")
    return 0


def _rows() -> list[dict[str, Any]]:
    if not POOL.exists():
        raise SystemExit(f"候选池缺席：{POOL}（先跑 --collect）")
    return [json.loads(line) for line in
            POOL.read_text(encoding="utf-8").splitlines() if line]


# ---------------------------------------------------------------- 判定一览表


def sheet() -> int:
    """给人工判定用的一览表：每条一行，带全部特征与首句。"""
    for row in _rows():
        print(f"\n=== {row['topic']}（{row['count']} 条候选）===")
        for index, (spec, feat) in enumerate(zip(row["pool"], row["features"]), start=1):
            head = " ".join(spec["text"].split())[:110]
            print(f"{index:2d} [{spec['source_name']:9s}] gate={feat['gate_branch']:8s} "
                  f"tcov={feat['title_cov']:.2f} cnt={feat['body_count']:2d} "
                  f"chars={feat['chars']:5d} rs={feat['rerank'] or 0:.3f} | "
                  f"{spec['title'][:56]}")
            print(f"    {head}")
    return 0


# ---------------------------------------------------------------- 区分力


def _auc(pos: list[float], neg: list[float]) -> float:
    """秩和口径的 AUC：随机取一条相关、一条不相关，前者分数更高的概率。

    用 AUC 而不是"均值差"：这些特征全是长尾（正文长度、词频），均值被一两条极值拖着
    走；AUC 只看次序，与尺度无关。0.5 = 不比掷硬币好。
    """
    if not pos or not neg:
        return float("nan")
    wins = 0.0
    for a in pos:
        for b in neg:
            wins += 1.0 if a > b else 0.5 if a == b else 0.0
    return wins / (len(pos) * len(neg))


def _describe(values: list[float]) -> str:
    if not values:
        return "—"
    ordered = sorted(values)
    mid = ordered[len(ordered) // 2]
    return f"中位 {mid:g} / 区间 [{ordered[0]:g}, {ordered[-1]:g}]"


def features_report() -> int:
    """§一 那张表：六个特征各自把"相关"与"不相关"分开的能力。"""
    load_judged()
    rows = _rows()
    numeric = {
        "b) 标题词面重合度 title_cov": lambda f: f["title_cov"],
        "c) 正文词块出现次数 body_count": lambda f: float(f["body_count"]),
        "c') 前300字出现次数 blob_count": lambda f: float(f["blob_count"]),
        "d) 正文长度 chars": lambda f: float(f["chars"]),
        "f) deepseek rerank_score": lambda f: float(f["rerank"] or 0.0),
    }
    totals = {"relevant": 0, "edge": 0, "noise": 0, "unjudged": 0}
    per_feature: dict[str, dict[str, list[float]]] = {
        name: {"relevant": [], "noise": []} for name in numeric}

    print("## 逐主题判定分布")
    print("| 主题 | 候选 | 相关 | 边缘 | 不相关 | 未判定 |")
    print("|---|---|---|---|---|---|")
    for row in rows:
        bucket = {"relevant": 0, "edge": 0, "noise": 0, "unjudged": 0}
        for feat in row["features"]:
            verdict = verdict_of(row["topic"], feat["title"])
            bucket["unjudged" if verdict is None else verdict] += 1
            if verdict in ("relevant", "noise"):
                for name, pick in numeric.items():
                    per_feature[name][verdict].append(pick(feat))
        for key, value in bucket.items():
            totals[key] += value
        print(f"| {row['topic']} | {row['count']} | {bucket['relevant']} "
              f"| {bucket['edge']} | {bucket['noise']} | {bucket['unjudged']} |")
    print(f"| **合计** | **{sum(totals.values())}** | **{totals['relevant']}** "
          f"| **{totals['edge']}** | **{totals['noise']}** | **{totals['unjudged']}** |")

    print("\n## 数值特征的区分力（相关 vs 不相关，边缘与未判定不参与）")
    print("| 特征 | 相关 | 不相关 | AUC | 相关分布 | 不相关分布 |")
    print("|---|---|---|---|---|---|")
    for name, buckets in per_feature.items():
        pos, neg = buckets["relevant"], buckets["noise"]
        print(f"| {name} | {len(pos)} | {len(neg)} | {_auc(pos, neg):.3f} "
              f"| {_describe(pos)} | {_describe(neg)} |")

    print("\n## a) 词面门 / e) 源：通过率与噪声率")
    print("| 分组 | 相关 | 边缘 | 不相关 | 未判定 | 噪声率（已判定里） |")
    print("|---|---|---|---|---|---|")
    for label, pick in (("门通过", lambda f: f["gate"]),
                        ("门挡掉", lambda f: not f["gate"])):
        buckets = {"relevant": 0, "edge": 0, "noise": 0, "unjudged": 0}
        for row in rows:
            for feat in row["features"]:
                if not pick(feat):
                    continue
                verdict = verdict_of(row["topic"], feat["title"])
                buckets["unjudged" if verdict is None else verdict] += 1
        judged = buckets["relevant"] + buckets["edge"] + buckets["noise"]
        rate = buckets["noise"] / judged if judged else 0.0
        print(f"| {label} | {buckets['relevant']} | {buckets['edge']} "
              f"| {buckets['noise']} | {buckets['unjudged']} | {rate:.1%} |")
    for name in external.DEFAULT_SOURCES:
        buckets = {"relevant": 0, "edge": 0, "noise": 0, "unjudged": 0}
        for row in rows:
            for feat in row["features"]:
                if feat["source"] != name:
                    continue
                verdict = verdict_of(row["topic"], feat["title"])
                buckets["unjudged" if verdict is None else verdict] += 1
        judged = buckets["relevant"] + buckets["edge"] + buckets["noise"]
        if not judged:
            continue
        rate = buckets["noise"] / judged if judged else 0.0
        print(f"| 源={name} | {buckets['relevant']} | {buckets['edge']} "
              f"| {buckets['noise']} | {buckets['unjudged']} | {rate:.1%} |")

    print("\n## 门的三个分支各放进来什么")
    print("| 分支 | 相关 | 边缘 | 不相关 | 未判定 |")
    print("|---|---|---|---|---|")
    for branch in ("phrase", "word", "failopen", "no-terms", "dropped"):
        buckets = {"relevant": 0, "edge": 0, "noise": 0, "unjudged": 0}
        for row in rows:
            for feat in row["features"]:
                if feat["gate_branch"] != branch:
                    continue
                verdict = verdict_of(row["topic"], feat["title"])
                buckets["unjudged" if verdict is None else verdict] += 1
        if sum(buckets.values()):
            print(f"| {branch} | {buckets['relevant']} | {buckets['edge']} "
                  f"| {buckets['noise']} | {buckets['unjudged']} |")

    print("\n## 候选级明细（判定 / 特征 / 首句）")
    for row in rows:
        for feat, spec in zip(row["features"], row["pool"]):
            verdict = verdict_of(row["topic"], feat["title"])
            print(f"{row['topic']} | {spec['source_name']} | {verdict or '未判定'} | "
                  f"gate={feat['gate_branch']} tcov={feat['title_cov']:.2f} "
                  f"cnt={feat['body_count']} chars={feat['chars']} "
                  f"rs={feat['rerank']} | {spec['title'][:50]} "
                  f"| {reason_of(row['topic'], feat['title'])[:60]}")
    return 0


def evidence() -> int:
    """贴原文：一条候选的头 `RELEVANCE_CHARS` 字到底写了什么。

    这个出口存在的理由是一个具体问题：**「课程 - 国家高等教育智慧教育平台」为什么
    过了 `_relevant_hits`**。要回答它，只能把门看到的那 300 字原样贴出来——判据
    只看这一段，所以"这条到底为什么被判相关"的答案必须在这一段里能指出来。
    """
    load_judged()
    for row in _rows():
        print(f"\n=== {row['topic']} ===")
        for spec in row["pool"]:
            blob = f"{spec['title']}\n{spec['text'][:external.RELEVANCE_CHARS]}"
            phrases = sorted(external._phrase_terms(row["topic"]))
            words = sorted(external._word_terms(row["topic"]))
            hit_phrases = [p for p in phrases if p in blob]
            hit_words = [w for w in words if w in external._word_terms(blob)]
            print(f"\n[{spec['source_name']}] {spec['title'][:70]}")
            print(f"  判定={verdict_of(row['topic'], spec['title'])} "
                  f"门={gate_branch(row['topic'], _hit(spec))} "
                  f"词块命中={hit_phrases} 英文词命中={hit_words} "
                  f"rerank={spec.get('rerank_score')}")
            print(f"  门前 300 字原文：{blob[:external.RELEVANCE_CHARS]!r}")
    return 0


def _hit(spec: dict[str, Any]) -> external.ExternalHit:
    """池子里的一行 → `ExternalHit`（离线重放用）。"""
    return external.ExternalHit(
        title=spec["title"], text=spec["text"], url=spec["url"],
        source_name=spec["source_name"], fetched_at="2026-01-01T00:00:00Z",
        language=spec["language"], raw_chars=spec.get("raw_chars", len(spec["text"])),
        site=spec.get("site") or "", authority=spec.get("authority"),
        rerank_score=spec.get("rerank_score"), direction=spec.get("direction") or "",
    )


# ---------------------------------------------------------------- 骨架：支数与判废率
#
# 判定率那一列是这次改动的**关键指标**：材料少到骨架给不出 3 条一级分支时，整个
# 生成被 `MIN_BRANCHES` 判废（`tools.py` 的 `too_few_branches`）。今天实测"2 份
# 材料 → 15/18 次判废"，所以 limit 提到多少必须看这一列会不会降，而不是看"多拿了
# 几条材料"。
#
# 这里跑的是**骨架那一次调用本身**：与 `_generate` 拼给模型的 system/user 逐字节
# 相同（`PLAN_PROMPT` + `_plan_ask` + `_material_inventory`），只省掉了前面的入库
# 与后面的节点生长。所以它量的是"材料清单长什么样 → 模型给几条分支"，正是这次
# 要回答的问题。
#
# 一处必须说清的偏差：生产上 `MINDMAP_SECTION_SLICE=1` 时，带章节标题的材料会走
# "按章切"（一级分支 = 章数），这条实测走的是骨架那次调用。两者不是同一个数，
# 但"材料越多、审批的支数越多"这个方向一致。


async def _skeleton_once(topic: str, hits: Sequence[external.ExternalHit]) -> dict[str, Any]:
    from app.agent import tools
    from app.generation.service import chat_async

    sources = [tools._external_source(hit) for hit in hits]
    outline = tools._skeleton_outline(sources)
    materials = tools._material_inventory(sources, has_structure=bool(outline))
    ask = tools._plan_ask(topic, outline=outline, materials=materials)
    started = time.perf_counter()
    raw = await chat_async(tools.PLAN_PROMPT, ask, max_tokens=1600)
    plan = tools._cap_plan(tools._skeleton_plan(raw))
    return {
        "branches": len(plan.flat),
        "titles": [branch.label for branch in plan.flat],
        "seconds": round(time.perf_counter() - started, 2),
        "outline_lines": len([line for line in outline.splitlines() if line.strip()]),
        "materials": len(sources),
        "chars": sum(len(source.text or "") for source in sources),
    }


def _pick_from_pool(topic: str, limit: int) -> list[external.ExternalHit]:
    """按**方案**从池子里选 `limit` 条（打分 → 排序 → 去重 → MMR）。"""
    from measure_external_rank_policy import select

    for row in _rows():
        if row["topic"] == topic:
            return select(topic, [_hit(spec) for spec in row["pool"]], limit)
    raise SystemExit(f"池子里没有主题 {topic}")


def skeleton_report(*, repeat: int = 1, topics: Sequence[str] | None = None) -> int:
    """每个 limit 下：骨架给几条、判废几次。**离线选题 + 联网一次 LLM**。"""
    from app.agent import tools

    names = list(topics) if topics else [row["topic"] for row in _rows()]
    records: list[dict[str, Any]] = []
    for limit in (3, 6, 8, 12):
        for topic in names:
            hits = _pick_from_pool(topic, limit)
            for attempt in range(max(repeat, 1)):
                try:
                    result = asyncio.run(_skeleton_once(topic, hits))
                    error = None
                except Exception as exc:  # noqa: BLE001 — 实测脚本，异常本身就是数据
                    result = {"branches": 0, "titles": [], "seconds": 0.0,
                              "outline_lines": 0, "materials": len(hits),
                              "chars": sum(len(hit.text or "") for hit in hits)}
                    error = f"{type(exc).__name__}: {exc}"
                row = {"limit": limit, "topic": topic, "attempt": attempt,
                       "too_few": result["branches"] < tools.MIN_BRANCHES,
                       "error": error, **result}
                records.append(row)
                print(f"limit={limit:2d} {topic:12s} 支数={result['branches']:2d} "
                      f"材料={result['materials']:2d} 字={result['chars']:5d} "
                      f"目录={result['outline_lines']:2d} 行 {result['seconds']:5.2f}s "
                      f"{'判废' if row['too_few'] else ''} {error or ''}", flush=True)
    SKELETON_OUT.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8")

    print("\n| limit | 次数 | 平均支数 | 支数区间 | too_few_branches 判废率 | 材料平均字数 |")
    print("|---|---|---|---|---|---|")
    for limit in (3, 6, 8, 12):
        bucket = [r for r in records if r["limit"] == limit]
        if not bucket:
            continue
        branches = [r["branches"] for r in bucket]
        killed = sum(1 for r in bucket if r["too_few"])
        chars = sum(r["chars"] for r in bucket) / len(bucket)
        print(f"| {limit} | {len(bucket)} | {sum(branches) / len(bucket):.2f} "
              f"| [{min(branches)}, {max(branches)}] | {killed}/{len(bucket)} "
              f"| {chars:.0f} |")
    print(f"\n写盘：{SKELETON_OUT}")
    return 0


def live_report(
    *, repeat: int = 1, topics: Sequence[str] | None = None, limits: Sequence[int] = (3, 8),
) -> int:
    """真主题、真检索：`search_external(limit=N)` → 骨架，各跑一遍。

    与 `--skeleton` 的差别只有一处，但那一处正是外面最容易被质疑的：这里**真的按
    `limit` 去搜**（源站因此返回不同的候选宽度），而不是从一份固定池子里切前 N 条。
    """
    from app.agent import tools

    load_judged()
    names = list(topics) if topics else [t for t, _ in TOPICS[:3]]
    external.deepseek_search_enabled()
    records: list[dict[str, Any]] = []
    for limit in limits:
        for topic in names:
            started = time.perf_counter()
            try:
                hits = external.search_external(topic, directions=[topic], limit=limit)
                error = None
            except Exception as exc:  # noqa: BLE001
                hits, error = [], f"{type(exc).__name__}: {exc}"
            elapsed = time.perf_counter() - started
            stats = _live_stats(topic, list(hits))
            branches: list[int] = []
            titles: list[str] = []
            for _attempt in range(max(repeat, 1)):
                try:
                    result = asyncio.run(_skeleton_once(topic, list(hits)))
                    branches.append(result["branches"])
                    titles = result["titles"]
                except Exception as exc:  # noqa: BLE001
                    branches.append(0)
                    error = error or f"{type(exc).__name__}: {exc}"
            row = {"limit": limit, "topic": topic, "seconds": round(elapsed, 2),
                   "branches": branches, "branch_titles": titles, "error": error, **stats}
            records.append(row)
            print(f"limit={limit:2d} {topic:12s} 命中 {stats['count']:2d} "
                  f"相关 {stats['relevant']:2d} 字 {stats['chars']:5d} "
                  f"{elapsed:5.2f}s 支数 {branches} "
                  f"{'判废' if any(b < tools.MIN_BRANCHES for b in branches) else ''}")
            for hit in hits:
                print(f"    [{hit.source_name:9s}] {hit.title[:56]} "
                      f"({len(hit.text)}字, rs={hit.rerank_score})")
    LIVE_OUT.write_text(
        "\n".join(json.dumps({k: v for k, v in r.items() if k != "titles"},
                             ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    print(f"\n写盘：{LIVE_OUT}")
    return 0


def _live_stats(topic: str, hits: Sequence[external.ExternalHit]) -> dict[str, Any]:
    verdicts = [verdict_of(topic, hit.title) for hit in hits]
    known = [v for v in verdicts if v in ("relevant", "edge", "noise")]
    return {
        "count": len(hits),
        "relevant": sum(1 for v in known if v == "relevant"),
        "edge": sum(1 for v in known if v == "edge"),
        "noise": sum(1 for v in known if v == "noise"),
        "unjudged": sum(1 for v in verdicts if v is None),
        "chars": sum(len(hit.text or "") for hit in hits),
        "sources": [hit.source_name for hit in hits],
        "titles": [hit.title for hit in hits],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect", action="store_true", help="联网抓一次候选池")
    parser.add_argument("--sheet", action="store_true", help="出人工判定一览表")
    parser.add_argument("--features", action="store_true", help="六个特征的区分力")
    parser.add_argument("--evidence", action="store_true", help="贴出每条候选的门前 300 字")
    parser.add_argument("--rules", action="store_true", help="判据网格（TP/FP/误杀）")
    parser.add_argument("--sweep", action="store_true", help="limit 扫描")
    parser.add_argument("--diversity", action="store_true", help="源分布与内容重复度")
    parser.add_argument("--skeleton", action="store_true", help="骨架支数与判废率")
    parser.add_argument("--live", action="store_true", help="真主题端到端（源 + 骨架）")
    parser.add_argument("--repeat", type=int, default=1, help="骨架每个格子重复几次")
    parser.add_argument("--limits", type=str, default="", help="--live 的 limit 清单，如 3,8")
    parser.add_argument("--topics", type=str, default="", help="只跑这几个主题（逗号分隔）")
    args = parser.parse_args()
    if args.collect:
        return collect()
    if args.sheet:
        return sheet()
    if args.features:
        return features_report()
    if args.evidence:
        return evidence()
    if args.skeleton:
        return skeleton_report(repeat=args.repeat, topics=_topic_filter(args.topics))
    if args.live:
        return live_report(repeat=args.repeat, topics=_topic_filter(args.topics),
                           limits=_limit_filter(args.limits))
    from measure_external_rank_policy import diversity_report, rules_grid, sweep
    if args.rules:
        return rules_grid()
    if args.sweep:
        return sweep()
    if args.diversity:
        return diversity_report()
    parser.print_help()
    return 2


def _topic_filter(raw: str) -> list[str] | None:
    names = [item.strip() for item in raw.split(",") if item.strip()]
    return names or None


def _limit_filter(raw: str) -> list[int]:
    values = [int(item) for item in raw.replace(" ", "").split(",") if item.strip()]
    return values or [external.DEFAULT_LIMIT, 8]


if __name__ == "__main__":
    sys.exit(main())
