"""取数策略：打分 → 排序 → 内容去重 → 按内容多样性挑前 limit 条。

这一套取代了"每源轮流取 + 给 DeepSeek 留 1 席"。用例分四组，每组对应一个曾经
出过真实故障的点：

  1. **打分**：标题分档（逐字 / 整串 / 词块 / token）、词频封顶、rerank 只做同档
     微调、五源缺 rerank 时拿中性值；
  2. **地板**：零词面证据的候选不当依据（全站跑题时返回空），但**跨语言**那一档
     只降到底部、不被抹掉；
  3. **去重**：同一条资料从两个源回来只留一条（留分数高的），"同主题不同文"不合并；
  4. **多样性**：MMR 把"与已选内容高度相似"的排到后面——**不按源**，按内容。

数字口径与 `docs/EXTERNAL-SELECTION.md` 一致（7 主题 / 256 条候选 / 人工判定）。
"""
from __future__ import annotations

import pytest

from app.knowledge import external


def _hit(
    title: str, text: str, *, source_name: str = "wikipedia", url: str = "",
    language: str = "zh", rerank_score: float | None = None,
) -> external.ExternalHit:
    return external.ExternalHit(
        title=title, text=text, url=url or f"https://example.invalid/{abs(hash(title))}",
        source_name=source_name, fetched_at="2026-01-01T00:00:00Z", language=language,
        raw_chars=len(text), rerank_score=rerank_score,
    )


# ---------------------------------------------------------------- 1 打分


def test_title_score_is_graded_not_binary():
    """标题分四档：逐字 1.0 > 整串 0.9 > 词块 0.8 > 只有 token 重合（折价）。

    二值口径下「机器学习」（维基词条）与「机器学习理论与实践」（图书商品页）同分，
    而一条是这条 query 的答案、另一条是商品页。分档就是为了把它们分开。
    """
    query = "机器学习"
    body = "机器学习是人工智能的一个分支。" * 3
    exact = _hit("机器学习", body)
    full = _hit("机器学习入门与实践", body)
    tokens = _hit("深度学习与统计学习", body)

    assert external._title_score(query, exact) == external.TITLE_SCORE_EXACT
    assert external._title_score(query, full) == external.TITLE_SCORE_FULL
    assert external._title_score(query, tokens) < external.TITLE_SCORE_PHRASE
    assert (external.score_hit(query, exact) > external.score_hit(query, full)
            > external.score_hit(query, tokens))


def test_occurrence_counts_are_capped():
    """词频有上限：把主题词堆在页头一万次也换不来更高的分。"""
    query = "机器学习"
    once = _hit("机器学习", "机器学习是人工智能的一个分支。")
    many = _hit("机器学习", "机器学习 " * 500)

    assert external.score_hit(query, many) <= (
        external.TITLE_WEIGHT + external.LEAD_WEIGHT + external.FREQ_WEIGHT)
    # `once` 的词块数：标题里 1 次 + 正文首句 1 次 = 前 300 字里 2 次；全文 1 次。
    # 再乘上"缺 rerank 分"的那层系数（中性值 0.5）。
    neutral = 1.0 - external.RERANK_INFLUENCE + external.RERANK_INFLUENCE * external.RERANK_NEUTRAL
    assert external.score_hit(query, once) == pytest.approx(neutral * (
        external.TITLE_WEIGHT
        + external.LEAD_WEIGHT * 2 / external.LEAD_OCCURRENCE_CAP
        + external.FREQ_WEIGHT * 1 / external.BODY_OCCURRENCE_CAP))


def test_missing_rerank_is_neutral_not_zero():
    """五源没有 rerank 分 → 中性值。给 0 等于"没分数就是最低分"，实测量到的后果是
    deepseek 的每一条都压过维基词条。"""
    query = "碳中和"
    body = "碳中和是指用碳汇抵消自身排放。"
    plain = _hit("碳中和", body)
    scored_low = _hit("碳中和", body, source_name="deepseek", rerank_score=1.5)

    assert external._rerank_norm(plain) == external.RERANK_NEUTRAL
    assert external._rerank_norm(scored_low) == 0.0
    # 同一个标题、同一段正文：有 rerank 且为最低值时反而略低于没有分数的五源
    assert external.score_hit(query, plain) > external.score_hit(query, scored_low)


def test_rerank_only_nudges_within_a_tier():
    """rerank 拉不开档位：标题不命中的一条，再高的分也追不上标题命中的一条。"""
    query = "机器学习"
    titled = _hit("机器学习", "机器学习是人工智能的分支。")
    untitled = _hit(
        "课程 - 国家高等教育智慧教育平台",
        "机器学习与人工智能 " + "选课平台 | 开课高校 | 开课教师 " * 3,
        source_name="deepseek", rerank_score=2.0)

    assert external.score_hit(query, titled) > external.score_hit(query, untitled)


def test_english_query_gets_body_evidence_from_english_words():
    """纯英文 query 靠英文词拿正文证据。

    只数中文词块时，`Cursor` 那题里维基的 `Anysphere`（正文开头就写着它为 Cursor
    提供开发服务）打分为 0——一条真依据被判成噪声。
    """
    assert external._occurrences("Cursor", "Anysphere offers Cursor as a service.") == 1
    assert external.score_hit(
        "Cursor", _hit("Anysphere", "它为开发者提供 Cursor 这个编辑器。")) > 0


# ---------------------------------------------------------------- 2 地板与跨语言


def test_zero_evidence_is_dropped():
    """零词面证据的候选不进材料：全站跑题时返回空列表，而不是塞一条进去。"""
    query = "Rust 所有权"
    noise = _hit("陳淑樺", "台灣殿堂級女歌手。")
    assert external.score_hit(query, noise) == 0.0
    assert external._select_hits(query, [noise], 8) == []


def test_cross_lingual_fail_open_ranks_last_but_survives():
    """跨语言命中：判据没有说话能力，所以给一个**比任何词面证据都小**的分。

    它必须能进（名额有剩时），也必须排在所有有词面证据的候选之后。
    """
    query = "注意力机制"
    chinese = _hit("注意力机制", "注意力机制是 Transformer 的核心。")
    english = _hit("Attention Is All You Need", "We propose a new architecture.",
                   source_name="arxiv", language="en")

    assert external._cross_lingual(query, english) is True
    assert external.score_hit(query, english) == external.CROSS_LINGUAL_SCORE
    assert external.score_hit(query, chinese) > external.score_hit(query, english)
    picked = external._select_hits(query, [english, chinese], 2)
    assert [hit.title for hit in picked] == ["注意力机制", "Attention Is All You Need"]
    # 名额只有一个时，跨语言那条让位
    assert [hit.title for hit in external._select_hits(query, [english, chinese], 1)] \
        == ["注意力机制"]


# ---------------------------------------------------------------- 3 去重


def test_same_article_from_two_sources_kept_once():
    """同一条资料从两个源回来（标题逐字相同、正文相同）只留一条，且留分数高的。"""
    query = "雨污分流改造"
    body = "雨污分流改造通过分离雨水与污水提高排水能力。" * 2
    openalex = _hit("城市排水系统雨污分流改造技术研究", body, source_name="openalex")
    crossref = _hit("城市排水系统雨污分流改造技术研究", body, source_name="crossref")

    picked = external._select_hits(query, [openalex, crossref], 8)

    assert len(picked) == 1
    assert picked[0].source_name == "openalex"  # 同分时保留先到的那条（稳定排序）


def test_distinct_articles_are_not_merged():
    """同主题不同文的几篇不合并——它们各有各的细节，正是多样性要保的东西。"""
    query = "雨污分流改造"
    first = _hit("城市雨污分流改造工程施工难点与技术对策",
                 "老旧城区管网现状、施工空间、新旧管网衔接是三大难点。", source_name="openalex")
    second = _hit("雨污分流改造对城市黑臭水体治理的成本-效益分析",
                  "通过建设独立排水系统减少污水直排，成本包括勘察与设备购置。",
                  source_name="openalex")

    assert external._similarity(first, second) < external.DEDUPE_THRESHOLD
    assert len(external._select_hits(query, [first, second], 8)) == 2


def test_dedupe_reports_how_many_it_merged():
    """`_dedupe` 返回合并条数：那个数进 debug 日志，是"去重到底干了什么"的唯一证据。"""
    body = "碳中和是指用碳汇抵消自身排放。" * 2
    item = _hit("碳中和", body)
    kept, dropped = external._dedupe([item, item, item])

    assert len(kept) == 1 and dropped == 2


# ---------------------------------------------------------------- 4 多样性（不按源）


def test_diversity_is_content_based_not_source_based():
    """三条同源但内容不同 → 全留；三条不同源但内容雷同 → 只留一条。

    这是"多样性"的正确定义：它管的是"别拿三条讲同一件事的材料"，不是"每个源都得
    分一条"。按源配额是这次拆掉的东西（实测加"每源封顶"之后 limit=8 的相关条数
    42→36、噪声 10→14）。
    """
    query = "碳中和"
    same_source = [
        _hit("碳中和的能源转型路径",
             "能源侧要做的三件事：提高非化石能源比例、电气化、建设新型电力系统。"),
        _hit("碳中和的工业减排路径",
             "工业侧重点是流程再造、氢冶金与碳捕集，钢铁与水泥是排放大户。"),
        _hit("碳中和的林业碳汇路径",
             "林业侧依靠造林再造林与森林经营提高碳储量，同时要防火灾与病虫害。"),
    ]
    assert external._select_hits(query, same_source, 3) == same_source

    body = "碳中和是指用碳汇抵消自身排放，包含能源、工业、林业三条路径。" * 2
    same_content = [
        _hit("碳中和", body, source_name=name, url=f"https://x/{name}")
        for name in ("wikipedia", "baike", "openalex")
    ]
    assert len(external._select_hits(query, same_content, 3)) == 1


def test_selection_respects_limit_and_stays_deterministic():
    """`limit` 是硬上界；同一批候选重放两次结果逐条相同（无随机、无时间依赖）。"""
    query = "碳中和"
    items = [
        _hit(f"碳中和方面{index}", text)
        for index, text in enumerate([
            "能源转型：非化石能源比例、电气化、新型电力系统与储能调峰。",
            "工业减排：流程再造、氢冶金、碳捕集封存与循环经济。",
            "交通运输：电动化、氢燃料、多式联运与绿色港口。",
            "建筑领域：超低能耗、电气化供暖与建材低碳化。",
            "农业与土地利用：稻田甲烷、化肥减量、保护性耕作。",
            "碳汇与生态：造林再造林、湿地修复、海洋蓝碳。",
            "碳市场与价格：配额分配、CCER、碳税与绿色金融。",
            "标准与核算：MRV 体系、产品碳足迹、第三方核查。",
            "技术路线图：CCUS、氢能、新型储能与智能电网。",
            "区域试点：低碳城市、零碳园区、近零碳工厂。",
            "国际合作：巴黎协定、气候投融资与技术转移。",
            "治理与政策：双碳领导小组、地方考核、法律修订。",
        ])
    ]
    first = external._select_hits(query, items, 5)
    second = external._select_hits(query, items, 5)

    assert len(first) == 5
    assert [hit.title for hit in first] == [hit.title for hit in second]


# ---------------------------------------------------------------- 5 回归：坏样本


def test_regression_the_course_page_no_longer_outranks_the_entry(monkeypatch):
    """回归：`机器学习` 那次五源全挂，deepseek 抓回教育平台课程页并进了材料。

    那条命中的形状（实测原文）：标题「课程 - 国家高等教育智慧教育平台」，正文前
    300 字是「机器学习与人工智能 ／ 机器学习与人工智能 ／ 5万+ 人选课 ／ 开课平台 | …」
    ——「机器学习」在门的窗口里出现 5 次，全在课程名与页头导航里，`_relevant_hits`
    的 `phrase` 分支因此放行。它当年还靠"DeepSeek 独占 1 席"直接拿到名额。

    现在两条都撤了：分数由"标题分档 + 词频 + rerank 微调"决定，课程页的标题与
    query 零重合，只能拿到"词频那一档"，排在维基/百科词条之后。
    """
    query = "机器学习"
    wiki = _hit("机器学习", "机器学习（英語：machine learning）是人工智能的一个分支。" * 3)
    baike = _hit("机器学习", "机器学习是一门多领域交叉学科。", source_name="baike")
    course = _hit(
        "课程 - 国家高等教育智慧教育平台",
        "机器学习与人工智能\n\n机器学习与人工智能\n\n5万+ 人选课\n\n开课平台 | 爱课程\n"
        "开课高校 | 浙江大学\n专业 | 工学计算机类\n机器学习概念和常用方法分类\n",
        source_name="deepseek", rerank_score=1.812)
    tutorial = _hit("机器学习基础一文速通", "机器学习基础：KNN、SVM、决策树与 PCA。",
                    source_name="deepseek", rerank_score=1.626)

    picked = external._select_hits(query, [course, wiki, baike, tutorial], 3)

    assert [hit.title for hit in picked] == ["机器学习", "机器学习", "机器学习基础一文速通"]
    assert course not in picked, "课程页又进了前 3 条"
    # 分数差不是一点：课程页只拿到"正文词频"那一档，连 rerank 满分也追不上
    assert external.score_hit(query, course) < external.score_hit(query, wiki) / 2
