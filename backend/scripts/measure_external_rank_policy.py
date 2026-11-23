"""§一 那张表之外的三块：规则网格、打分原型、limit 扫描与多样性。

单独一个模块而不是继续堆进 `measure_external_rank.py`：那边是「量特征」，这里是
「量策略」——两件事的输入相同（同一份候选池 + 同一张人工判定表），产出完全不同。
分开之后，改策略不必碰抓取的代码，改特征也不必碰排序的代码。

**这里的每个函数都是原型，不是生产实现**：生产落地在 `knowledge/external.py`
（`_score_hit` / `_select_hits`），两者的数必须一致——`scripts` 里改一次权重而
生产里忘了改，是这个仓库最熟悉的一类故障。所以原型与生产共用同一套判据函数
（从 `external` 直接 import，不在这里重写），只有"怎么用这些判据"在这一层被反复试。
"""
from __future__ import annotations

from typing import Any, Sequence

import measure_external_rank as base

external = base.external
POOL = base.POOL


def _rows() -> list[dict[str, Any]]:
    return base._rows()


def _count(terms: Sequence[str], text: str) -> int:
    """词块在一段文字里出现的**次数**（不是"有没有"）。"""
    return sum((text or "").count(term) for term in terms)


def _evidence_terms(query: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """判据要数的两组词面：中文词块（区分大小写）+ 英文词（小写，比对时把正文小写）。

    英文词必须一起数：纯英文 query（`Cursor`、`consensus algorithm`）的
    `_phrase_terms` 是空的，只数中文词块会让它们**永远拿不到正文证据**——实测
    「Cursor」那题里维基的 `Anysphere`（正文开头就写着它为 Cursor 提供开发服务）
    打分为 0，一条真依据被判成噪声。
    """
    return tuple(external._phrase_terms(query)), tuple(sorted(external._word_terms(query)))


def _count_evidence(query: str, text: str) -> int:
    phrases, words = _evidence_terms(query)
    value = (text or "")
    return _count(phrases, value) + _count(words, value.lower())


def verdict_of(topic: str, title: str) -> str | None:
    return base.verdict_of(topic, title)


# ---------------------------------------------------------------- 规则网格
#
# 任务是"按 §一 量出来的判据实现"，所以这一步要回答的不是"哪个特征 AUC 最高"，
# 而是"**组合起来的判据**在池子上留下谁、扔掉谁"。每个格子给四个数：
#   TP = 判据放行且人工判为相关；FP = 放行但判为不相关
#   FN = 判据挡掉但其实是相关（误杀）；edge = 放行里"边缘"的条数
# 精度 = TP/(TP+FP)，召回 = TP/(TP+FN)。**误杀比漏放贵**：漏放只是少一条依据，
# 误杀是把一条真依据永久换掉，而这张图之后没有任何机会再看见它。


def rules_grid() -> int:
    base.load_judged()
    rows = _rows()
    entries: list[tuple[str, str, dict[str, Any], str]] = []
    for row in rows:
        for feat, spec in zip(row["features"], row["pool"]):
            entries.append((row["topic"], verdict_of(row["topic"], spec["title"]) or "unjudged",
                            feat, spec["title"]))

    def rule_gate(feat: dict[str, Any]) -> bool:
        return bool(feat["gate"])

    def rule_gate_cnt1(feat: dict[str, Any]) -> bool:
        return bool(feat["gate"]) and feat["body_count"] >= 1

    def rule_gate_cnt2(feat: dict[str, Any]) -> bool:
        return bool(feat["gate"]) and feat["body_count"] >= 2

    def rule_title_phrase(feat: dict[str, Any]) -> bool:
        return bool(feat["gate"]) and feat["title_phrase"]

    def rule_title_cov(feat: dict[str, Any]) -> bool:
        return bool(feat["gate"]) and feat["title_cov"] >= 0.5

    def rule_cnt_and_titlecov(feat: dict[str, Any]) -> bool:
        return bool(feat["gate"]) and feat["body_count"] >= 2 and feat["title_cov"] >= 0.5

    def rule_rerank(feat: dict[str, Any]) -> bool:
        return bool(feat["gate"]) and (feat["rerank"] or 0.0) >= 1.9

    def rule_union(feat: dict[str, Any]) -> bool:
        """词面门 + 「至少有一处强证据」：标题命中 / 正文 ≥2 次 / 源自己给了 ≥1.9 分。"""
        if not feat["gate"]:
            return False
        return (feat["title_phrase"] or feat["body_count"] >= 2
                or (feat["rerank"] or 0.0) >= 1.9)

    def rule_any(feat: dict[str, Any]) -> bool:
        return True

    def rule_title_phrase_only(feat: dict[str, Any]) -> bool:
        """**不要词面门**，只要求标题命中——用来量"门到底有没有用"。"""
        return bool(feat["title_phrase"])

    rules = {
        "无判据（全放行）": rule_any,
        "a) 词面门（现状）": rule_gate,
        "a)+词频≥1": rule_gate_cnt1,
        "a)+词频≥2": rule_gate_cnt2,
        "a)+标题含词块": rule_title_phrase,
        "a)+标题重合≥0.5": rule_title_cov,
        "a)+词频≥2 且 标题≥0.5": rule_cnt_and_titlecov,
        "a)+rerank≥1.9": rule_rerank,
        "a)+（标题 或 词频≥2 或 rerank≥1.9）": rule_union,
        "只有标题含词块（不要门）": rule_title_phrase_only,
    }
    print("| 判据 | 放行 | TP | FP | 误杀 FN | 边缘 | 精度 | 召回 |")
    print("|---|---|---|---|---|---|---|---|")
    for name, rule in rules.items():
        tp = fp = fn = edge = 0
        for _topic, verdict, feat, _title in entries:
            passed = rule(feat)
            if verdict == "revelevant" or verdict == "relevant":
                tp += 1 if passed else 0
                fn += 0 if passed else 1
            elif verdict == "noise":
                fp += 1 if passed else 0
            elif verdict == "edge":
                edge += 1 if passed else 0
        total_pass = tp + fp + edge
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        print(f"| {name} | {total_pass} | {tp} | {fp} | {fn} | {edge} "
              f"| {precision:.1%} | {recall:.1%} |")
    return 0


# ---------------------------------------------------------------- 打分原型
#
# 打分只做一件事：**把"有词面依据"这件事拆成几个可加的档**。三个分量都是 [0,1]，
# 权重来自 §一 的 AUC 次序（标题 > 正文前 300 字 > 全文词频 > 长度），不是拍出来的：
#
#   title  标题命中：AUC 最高的一档（`title_cov` 0.696、`title_phrase` 的二值 AUC 见
#          `rules_grid`），而且它是**源站自己对这条内容做的事先概括**——比正文里
#          偶然出现的一串字更接近"这条讲什么"；
#   lead   正文前 300 字里的词块次数（封顶 3 次）：与引用能取证的深度同一口径；
#   freq   全文词频（封顶 12 次）：长文里反复出现，比"出现一次"强；封顶是因为
#          导航/页脚的重复会把一个跑题页面刷成高频。
#
# `rerank` 只做**同一档内的微调**（乘在最后，系数 0.25）：DeepSeek 自己的重排分是
# 唯一来自源站的语义判断，应该用；但它是"这条页面关于 query 的程度"，不是"这条
# 资料能不能当依据"——实测它把「课程 - 国家高等教育智慧教育平台」排在自己那批的
# 第一名（1.812），所以它不能决定档位，只能决定同档内谁先。
TITLE_WEIGHT = 0.5
LEAD_WEIGHT = 0.3
FREQ_WEIGHT = 0.2
RERANK_FLOOR = 1.5
RERANK_CEIL = 2.0
RERANK_INFLUENCE = 0.25
# 五源没有 rerank 分。**给中性值 0.5 而不是 0**：给 0 等于"没有分数就是最低分"，
# 实测的后果是 deepseek 的每一条都压过维基词条——「机器学习」那题 limit=3 变成
# 三条 deepseek 商品页（`--sweep` 的变体对照里 `neutral=0.0` 那一行：相关 0/3）。
RERANK_NEUTRAL = 0.5
# 标题分档：逐字相同 > 整串命中 > 词块命中 > 只有 token 重合。分档而不做二值，
# 是因为「机器学习」（维基词条）与「机器学习理论与实践」（图书商品页）在二值口径下
# 都是"标题含这个词块"——而它们一条是词条、一条是商品页。
TITLE_EXACT = 1.0
TITLE_FULL = 0.9
TITLE_PHRASE = 0.8
TITLE_TOKENS = 0.5


def _rerank_norm(hit: external.ExternalHit) -> float:
    score = hit.rerank_score
    if score is None:
        return RERANK_NEUTRAL
    span = RERANK_CEIL - RERANK_FLOOR
    return max(0.0, min(1.0, (score - RERANK_FLOOR) / span))


def _title_score(query: str, hit: external.ExternalHit) -> float:
    text = (query or "").strip()
    title = hit.title or ""
    if text and title.strip() == text:
        return TITLE_EXACT
    if text and text in title:
        return TITLE_FULL
    if any(phrase in title for phrase in external._phrase_terms(text)):
        return TITLE_PHRASE
    words = external._word_terms(text)
    if words and external._word_terms(title) & words:
        return TITLE_PHRASE
    tokens = {t for t in external._token_list(title) if t.strip()}
    query_tokens = [t for t in external._token_list(text) if t.strip()]
    if not query_tokens:
        return 0.0
    return TITLE_TOKENS * (sum(1 for t in query_tokens if t in tokens) / len(query_tokens))


def score_hit(query: str, hit: external.ExternalHit) -> float:
    title = _title_score(query, hit)
    lead_count = _count_evidence(query, external._hit_blob(hit))
    freq_count = _count_evidence(query, hit.text or "")
    lead = min(lead_count, 3) / 3.0
    freq = min(freq_count, 12) / 12.0
    evidence = TITLE_WEIGHT * title + LEAD_WEIGHT * lead + FREQ_WEIGHT * freq
    return evidence * (1.0 - RERANK_INFLUENCE + RERANK_INFLUENCE * _rerank_norm(hit))


# ---------------------------------------------------------------- 去重
#
# 判据：`_token_list` 的**集合**（中文 bigram + 英文整词）在「标题 + 正文前 200 字」
# 上的 Jaccard ≥ `DEDUPE_THRESHOLD` 就算同一条。不引新依赖（复用检索那套分词），
# 也不比 cross_map 的余弦差——候选池里真正的重复是"同一篇文章从两个源回来"，
# 标题几乎逐字相同，不需要语义相似度。
#
# 阈值 0.60 是量出来的（`diversity_report` 会打印池内的相似度分布）：同文异址的
# 几对（碳中和 29/30、Cursor 39/40、白皮书三份）Jaccard 在 0.75 以上，而"同主题
# 不同文"的最高一对在 0.5 以下。中间留一格，避免把"讲同一件事的两篇不同文章"
# 也并掉——那两篇各有各的细节，正是"多样性"要保的东西。
DEDUPE_THRESHOLD = 0.60


def _shingles(hit: external.ExternalHit) -> set[str]:
    tokens = external._token_list(hit.title) + external._token_list((hit.text or "")[:200])
    return {token for token in tokens if token.strip()}


def similarity(a: external.ExternalHit, b: external.ExternalHit) -> float:
    left, right = _shingles(a), _shingles(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def dedupe(
    ranked: Sequence[external.ExternalHit], *, threshold: float = DEDUPE_THRESHOLD,
) -> tuple[list[external.ExternalHit], int]:
    """按**给定顺序**留第一条、并掉后面相似的。返回 `(留下的, 并掉几条)`。"""
    kept: list[external.ExternalHit] = []
    dropped = 0
    for hit in ranked:
        if any(similarity(hit, other) >= threshold for other in kept):
            dropped += 1
            continue
        kept.append(hit)
    return kept, dropped


# ---------------------------------------------------------------- 选条（排序 + 去重 + 多样性）
#
# "多样性"这一层**不按源**（那是这次要拆掉的每源保底），按**内容**：
# MMR 的朴素版——每选一条，就把"与已选内容最相似"的程度折算成扣分。它管的是
# "别拿三条讲同一件事的材料"，而"三条来自同一个源"只要内容不同就允许。
# 系数 0.25 的含义：相似度 1.0 的一条要损失 1/4 分，不足以把一条高分相关命中
# 挤到一条低分噪声后面，但足够在同分附近把重复的排到后面。
MMR_LAMBDA = 0.25
# 与生产同源的分数地板：零证据的候选不当依据（`external.SCORE_FLOOR`）。
SCORE_FLOOR = external.SCORE_FLOOR


def select(
    query: str, hits: Sequence[external.ExternalHit], limit: int, *,
    threshold: float = DEDUPE_THRESHOLD, mmr: float = MMR_LAMBDA,
) -> list[external.ExternalHit]:
    scored = [hit for hit in hits if score_hit(query, hit) > SCORE_FLOOR]
    scored.sort(key=lambda hit: -score_hit(query, hit))
    unique, _dropped = dedupe(scored, threshold=threshold)
    picked: list[external.ExternalHit] = []
    pool = list(unique)
    while pool and len(picked) < limit:
        best_index = 0
        best_value = None
        for index, hit in enumerate(pool):
            penalty = max((similarity(hit, other) for other in picked), default=0.0)
            value = score_hit(query, hit) - mmr * penalty
            if best_value is None or value > best_value:
                best_value, best_index = value, index
        picked.append(pool.pop(best_index))
    return picked


# ---------------------------------------------------------------- 扫描


def _hits(row: dict[str, Any]) -> list[external.ExternalHit]:
    return [base._hit(spec) for spec in row["pool"]]


def _stats(topic: str, hits: Sequence[external.ExternalHit]) -> dict[str, Any]:
    verdicts = [verdict_of(topic, hit.title) for hit in hits]
    known = [v for v in verdicts if v in ("relevant", "edge", "noise")]
    return {
        "count": len(hits),
        "relevant": sum(1 for v in known if v == "relevant"),
        "edge": sum(1 for v in known if v == "edge"),
        "noise": sum(1 for v in known if v == "noise"),
        "unjudged": sum(1 for v in verdicts if v is None),
        # `chars` 是**池内**的字数（每条正文在池子里只留 `POOL_TEXT_CHARS=600` 字），
        # `raw` 是抓取时的真实长度。要报"材料总字数"只能报 `raw`——600 那个数会把
        # 一条 7883 字的维基词条报成 600 字，等于把"材料很厚"这件事抹掉。
        "chars": sum(len(hit.text or "") for hit in hits),
        "raw": sum(min(hit.raw_chars or len(hit.text or ""), external.TEXT_MAX_CHARS)
                   for hit in hits),
        "sources": sorted({hit.source_name for hit in hits}),
        "titles": [hit.title for hit in hits],
    }


def _baseline(query: str, hits: Sequence[external.ExternalHit], limit: int) -> list:
    """改动之前的取样：过词面门 → 每源轮流取（`_interleave`）+ DeepSeek 独占 1 席。

    **冻结在脚本里**而不是 import 生产代码：改动落地之后生产里已经没有
    `_blend_deepseek` 了，而"现状"这一列必须能在改动之后照样重算——否则这张对照表
    会随生产代码一起漂，变成"只能看一次"的数据。
    """
    relevant = external._relevant_hits(query, hits)
    language = external._query_language(query)
    search = [hit for hit in relevant if hit.source_name == external.DEEPSEEK_SOURCE]
    if not search or limit < 2:
        return external._interleave(relevant, limit, prefer_language=language)
    rest = [hit for hit in relevant if hit.source_name != external.DEEPSEEK_SOURCE]
    picked = external._interleave(rest, limit - 1, prefer_language=language)
    return picked + external._interleave(search, 1)


def variant_grid() -> int:
    """打分口径的变体对照：这就是"为什么是这套权重"的依据。

    三个开关各两档，都在**同一份池子**上重放：
      - `graded`：标题分档（逐字 / 整串 / 词块 / token）vs 二值（含词块即满分）；
      - `neutral`：五源没有 rerank 分时给 0.0 还是 0.5；
      - `mmr`：内容多样性惩罚 0 还是 0.25。
    读数看四件事：相关条数、噪声条数、用到的源数、"单源占满"的主题数（0 个源占满
    全部名额 = 结果不单一）。`机器学习` 那一列单独看——它是这次要修的那个坏样本。
    """
    base.load_judged()
    rows = _rows()
    print("| 标题分档 | 无分中性值 | MMR | limit | 条数 | 相关 | 噪声 | 源数 "
          "| 单源占满主题 | 机器学习相关 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for graded in (False, True):
        for neutral in (0.0, 0.5):
            for mmr in (0.0, 0.25):
                for limit in (3, 8):
                    total = {"count": 0, "relevant": 0, "noise": 0}
                    sources: set[str] = set()
                    solo = 0
                    machine = ""
                    for row in rows:
                        topic = row["topic"]
                        chosen = _select_variant(topic, _hits(row), limit,
                                                 graded=graded, neutral=neutral, mmr=mmr)
                        stats = _stats(topic, chosen)
                        for key in total:
                            total[key] += stats[key]
                        names = [hit.source_name for hit in chosen]
                        sources.update(names)
                        if names and len(set(names)) == 1:
                            solo += 1
                        if topic == "机器学习":
                            machine = f"{stats['relevant']}/{stats['count']}"
                    print(f"| {'分档' if graded else '二值'} "
                          f"| {neutral} | {mmr} | {limit} | {total['count']} "
                          f"| {total['relevant']} | {total['noise']} | {len(sources)} "
                          f"| {solo} | {machine} |")
    return 0


def _select_variant(
    query: str, hits: Sequence[external.ExternalHit], limit: int, *,
    graded: bool, neutral: float, mmr: float,
) -> list:
    """`variant_grid` 用的打分/选条（参数化版本）。选定的那档与 `score_hit` 等价。"""

    def title_of(hit: external.ExternalHit) -> float:
        if graded:
            return _title_score(query, hit)
        phrases = external._phrase_terms(query)
        words = external._word_terms(query)
        tokens = {t for t in external._token_list(hit.title) if t.strip()}
        query_tokens = [t for t in external._token_list(query) if t.strip()]
        cov = (sum(1 for t in query_tokens if t in tokens) / len(query_tokens)
               if query_tokens else 0.0)
        word = (len(words & external._word_terms(hit.title)) / len(words)) if words else 0.0
        return max(1.0 if any(p in hit.title for p in phrases) else 0.0, cov, word)

    def value_of(hit: external.ExternalHit) -> float:
        lead = min(_count_evidence(query, external._hit_blob(hit)), 3) / 3.0
        freq = min(_count_evidence(query, hit.text or ""), 12) / 12.0
        evidence = TITLE_WEIGHT * title_of(hit) + LEAD_WEIGHT * lead + FREQ_WEIGHT * freq
        score = hit.rerank_score
        if score is None:
            norm = neutral
        else:
            norm = max(0.0, min(1.0, (score - RERANK_FLOOR) / (RERANK_CEIL - RERANK_FLOOR)))
        return evidence * (1.0 - RERANK_INFLUENCE + RERANK_INFLUENCE * norm)

    scored = sorted(hits, key=lambda hit: -value_of(hit))
    unique, _dropped = dedupe(scored)
    pool = list(unique)
    picked: list[external.ExternalHit] = []
    while pool and len(picked) < limit:
        best_index, best_value = 0, None
        for index, hit in enumerate(pool):
            penalty = max((similarity(hit, other) for other in picked), default=0.0)
            value = value_of(hit) - mmr * penalty
            if best_value is None or value > best_value:
                best_value, best_index = value, index
        picked.append(pool.pop(best_index))
    return picked


def sweep() -> int:
    """limit 3 / 6 / 8 / 12 上的五指标：条数、相关条数、材料总字数、源数、噪声条数。"""
    base.load_judged()
    rows = _rows()
    limits = (3, 6, 8, 12)
    for name, pick in (("现状（每源保底 + deepseek 1 席）", _baseline),
                       ("方案（打分 → 排序 → 去重 → MMR）", None)):
        print(f"\n### {name}")
        print("| limit | 条数 | 相关 | 边缘 | 噪声 | 材料总字数（真实） | 用到的源 | 逐主题相关 |")
        print("|---|---|---|---|---|---|---|---|")
        for limit in limits:
            total = {"count": 0, "relevant": 0, "edge": 0, "noise": 0,
                     "raw": 0, "sources": set()}
            per_topic: list[str] = []
            for row in rows:
                topic = row["topic"]
                hits = _hits(row)
                chosen = (pick(topic, hits, limit) if pick is not None
                          else select(topic, hits, limit))
                stats = _stats(topic, chosen)
                for key in ("count", "relevant", "edge", "noise", "raw"):
                    total[key] += stats[key]
                total["sources"].update(stats["sources"])
                per_topic.append(f"{topic[:6]} {stats['relevant']}/{stats['count']}")
            print(f"| {limit} | {total['count']} | {total['relevant']} "
                  f"| {total['edge']} | {total['noise']} | {total['raw']} "
                  f"| {len(total['sources'])} | {'；'.join(per_topic)} |")
    return 0


def diversity_report() -> int:
    """§五：按相关度排会不会让结果变单一 —— 源分布、内容重复度、相似度分布。

    三张表：
      1. 每个 limit 下"结果里用了几个源"、每个源占几条、以及**同一源连续占满**的
         主题数（"每次都只拿维基那 3 条"这个担心，用这个数回答）；
      2. 池内的相似度分布：同文异址的对有多像、同主题不同文的对有多像 —— 去重阈值
         就该落在这两组之间；
      3. 去重/MMR 各自并掉了什么（逐条，可复核）。
    """
    base.load_judged()
    rows = _rows()
    print("## 1. 源分布（方案 vs 现状）")
    print("| limit | 方案：源数/单源占满的主题 | 现状：源数/单源占满的主题 |")
    print("|---|---|---|")
    for limit in (3, 6, 8, 12):
        cells = []
        for pick in (None, _baseline):
            sources: set[str] = set()
            solo = 0
            for row in rows:
                hits = _hits(row)
                chosen = pick(row["topic"], hits, limit) if pick else select(
                    row["topic"], hits, limit)
                names = [hit.source_name for hit in chosen]
                sources.update(names)
                if names and len(set(names)) == 1:
                    solo += 1
            cells.append(f"{len(sources)}/{solo}")
        print(f"| {limit} | {cells[0]} | {cells[1]} |")

    print("\n## 2. 池内相似度分布（去重阈值该落在哪）")
    pairs: list[tuple[float, str, str, str]] = []
    for row in rows:
        hits = _hits(row)
        for i in range(len(hits)):
            for j in range(i + 1, len(hits)):
                value = similarity(hits[i], hits[j])
                if value >= 0.25:
                    pairs.append((value, row["topic"], hits[i].title, hits[j].title))
    pairs.sort(reverse=True)
    print(f"相似度 ≥0.25 的候选对：{len(pairs)} 对（池内共 "
          f"{sum(len(_hits(r)) for r in rows)} 条）")
    for value, topic, left, right in pairs[:12]:
        print(f"  {value:.2f}  {topic} :: {left[:32]} ‖ {right[:32]}")
    if pairs:
        print(f"\n最高一对 {pairs[0][0]:.2f}；两两相似度中位 "
              f"{pairs[len(pairs) // 2][0]:.2f}")

    print("\n## 3. 去重并掉了谁（limit=12，方案口径）")
    for row in rows:
        topic = row["topic"]
        hits = _hits(row)
        scored = sorted(hits, key=lambda hit: -score_hit(topic, hit))
        kept, dropped = dedupe(scored)
        kept_ids = {id(hit) for hit in kept}
        gone = [hit for hit in scored if id(hit) not in kept_ids]
        if not gone:
            continue
        print(f"  {topic}（并掉 {dropped} 条）：")
        for hit in gone:
            twin = next((other for other in kept
                         if similarity(hit, other) >= DEDUPE_THRESHOLD), None)
            print(f"    - [{hit.source_name}] {hit.title[:44]} ≡ "
                  f"{twin.title[:44] if twin else '?'}")
    return 0
