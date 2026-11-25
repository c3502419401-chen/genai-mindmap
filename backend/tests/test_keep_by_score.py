"""按分数取数（阈值 + 上下界）：`service.keep_by_score` 与三个入口的接线。

**"要不要改"的证据在 `tests/test_retrieval_distribution.py`**（分数分布形态、`top_k=3`
漏掉什么），那个文件不碰这里的实现。这个文件只钉"改成什么形状、边界在哪"：

  - 上下界：都很低时 `min_k` 托底、都很高时 `max_k` 封顶、材料不够时不多凑；
  - **基准取第 `min_k` 名，不取第 1 名**——第 1 名是离群高点，拿它乘 0.85 会退化到
    比改之前还少的条数（`test_anchor_is_the_third_name_not_the_outlier_first`）；
  - 标尺整批统一：重排 > 余弦 > BM25，RRF 那一列一次都不用；
  - 没有可比分的条目（图证据）跟着"条数"走，不被阈值判死；
  - **`top_k` 是下界**：`at_least=top_k` 少了这一半，`top_k=5` 会取到 3 条
    （第一次跑对照表时出现的 0.60x 就是它）。

三个入口（`search_parent` / `search_multi` / `search_exact_blocks`）各有一条接线用例。
单路那两个断言"结果是没开 keep 时的**前缀**"；多路只断言"原有命中一条不少"——
保底席位按 `want` 算，条数变了顺序本来就会变。
"""
from __future__ import annotations

import asyncio

import pytest

from app.knowledge import service
from app.knowledge.service import (
    EvidenceHit,
    KeepPolicy,
    keep_by_score,
    search_exact_blocks,
    search_multi,
    search_parent,
)
from test_retrieval_distribution import source as _source

POLICY = KeepPolicy(min_k=3, max_k=6, rel=0.85)


def hit(index: int, **kwargs) -> EvidenceHit:
    """一条命中：`start/end` 按下标拉开，`(source_id, start, end)` 因此互不相同。"""
    data = {"source_id": "s1", "start": index * 100, "end": index * 100 + 90,
            "quote": "正文", "score": 1.0}
    data.update(kwargs)
    return EvidenceHit(**data)


# ---------------------------------------------------------------- 二、阈值与上下界


def test_no_policy_returns_the_same_object():
    """"没启用"必须是**同一个列表对象**：既有 20 个测试文件里的桩都不传 policy。"""
    hits = [hit(index) for index in range(5)]
    assert keep_by_score(hits, None) is hits


def test_min_k_holds_when_every_score_is_low():
    """都很低时纯相对系数会取到一条 → `min_k` 托底。"""
    hits = [hit(0, score=0.5), hit(1, score=0.1), hit(2, score=0.05),
            hit(3, score=0.01), hit(4, score=0.009)]
    for item in hits:
        item.score_bm25 = item.score
    assert len(keep_by_score(hits, POLICY)) == 3


def test_max_k_caps_when_every_score_is_high():
    """都很高时（余弦长尾）能取到几十条 → `max_k` 封顶。"""
    hits = [hit(index, score=1.0 - index * 0.001) for index in range(30)]
    for item in hits:
        item.score_vector = item.score
    assert len(keep_by_score(hits, POLICY)) == POLICY.max_k


def test_anchor_is_the_third_name_not_the_outlier_first():
    """基准取第 `min_k` 名：第 1 名是离群高点时，用它当基准会退化。

    形状取自实测（`agent_plan/先修三件小事` 那一组：BM25 58.7 / 37.6 / 5.1 / 3.7 …）。
    用第 1 名 × 0.85 = 49.9 过门的只有它自己 → 只剩 `min_k` 条（等于没改）；用第 3
    名 × 0.85 则整段长尾都"差不多"。**注意第 3 名之后仍然要有断崖时它照样只取 3 条**
    ——那是阈值该做的事，不是缺陷（见下一条用例）。
    """
    scores = [58.7, 37.6, 5.1, 5.0, 4.9, 4.8, 4.7, 4.6]
    hits = [hit(index, score=value) for index, value in enumerate(scores)]
    for item in hits:
        item.score_bm25 = item.score
    assert len(keep_by_score(hits, POLICY)) == POLICY.max_k

    outlier = KeepPolicy(min_k=3, max_k=6, rel=0.85)
    assert outlier.budget(3) == 6


def test_a_real_cliff_still_stops_at_min_k():
    """断崖型材料（第 3 名之后掉 27%）不硬凑：阈值该停就停，只保 `min_k` 条。

    这一条是"如实说"那一半：不是所有材料都该多取。实测里 `agent_plan/先修三件小事`
    就是这种形状，那时 `top_k=3` 本来就是对的。
    """
    scores = [58.7, 37.6, 5.1, 3.7, 3.4, 3.2, 3.1, 3.0]
    hits = [hit(index, score=value) for index, value in enumerate(scores)]
    for item in hits:
        item.score_bm25 = item.score
    assert len(keep_by_score(hits, POLICY)) == POLICY.min_k


def test_never_more_than_the_hits_it_was_given():
    """材料本身不够（薄批只有一两个父块）时不能凭空凑到 `min_k`。"""
    hits = [hit(0, score=1.0), hit(1, score=0.9)]
    for item in hits:
        item.score_bm25 = item.score
    assert len(keep_by_score(hits, POLICY)) == 2


def test_scale_prefers_rerank_over_vector_over_bm25():
    """整批统一标尺，顺序是重排 > 余弦 > BM25。

    构造：一条余弦很高但重排很低。用余弦会被留下，用重排会被判成"不达标"——
    两者条数不同，断言就能分辨标尺到底选了哪个。
    """
    hits = [hit(0, score=0.03, score_vector=0.9, score_bm25=9.0, rerank_score=0.9),
            hit(1, score=0.03, score_vector=0.88, score_bm25=8.0, rerank_score=0.8),
            hit(2, score=0.03, score_vector=0.86, score_bm25=7.0, rerank_score=0.8),
            hit(3, score=0.03, score_vector=0.85, score_bm25=6.0, rerank_score=0.1),
            hit(4, score=0.03, score_vector=0.84, score_bm25=5.0, rerank_score=0.1)]
    assert len(keep_by_score(hits, POLICY)) == 3, "重排分该把那两条 0.1 挡在门外"

    for item in hits:
        item.rerank_score = None
    assert len(keep_by_score(hits, POLICY)) == len(hits), "没有重排分时该改看余弦"


def test_graph_hits_ride_along_instead_of_being_filtered_out():
    """图证据没有可比分数：它们跟着"条数"被带出去，不被逐条筛掉。

    `_with_graph` 在 `keep_by_score` 之前跑，图命中进了同一个列表；如果这里改成
    "逐条筛 `>= floor`"，那些 `score_*` 全是 None 的条目会被整批判死，而图路是
    `MINDMAP_GRAPH_SEARCH` 打开时唯一的补证据通道。
    """
    hits = [hit(index, score=1.0 - index * 0.01) for index in range(4)]
    for item in hits:
        item.score_bm25 = item.score
    graph = hit(9, score=0.0, origin="graph")
    hits.append(graph)
    kept = keep_by_score(hits, POLICY)
    assert kept[-1] is graph


# ---------------------------------------------------------------- 三、三个入口


def test_search_parent_takes_more_than_top_k_and_keeps_the_order():
    """`search_parent(..., keep=)` 的条数 ≥ `top_k`，且是没开 keep 时的**前缀**。"""
    source = _source()
    baseline = search_parent([source], "预算审批流程", 3)
    widened = search_parent([source], "预算审批流程", 3, keep=POLICY)
    assert len(widened) > len(baseline)
    assert len(widened) <= POLICY.max_k
    keys = [(item.source_id, item.start, item.end) for item in widened]
    assert keys[:len(baseline)] == [(item.source_id, item.start, item.end)
                                    for item in baseline]


def test_search_parent_keep_is_inert_when_the_material_is_small():
    """薄批（材料比 `max_k` 还小）时条数就等于材料能给出的全部，不多不少。"""
    source = _source("预算审批流程由部门负责人与财务经理共同确认，其余环节由系统留痕。")
    hits = search_parent([source], "预算审批流程", 3, keep=POLICY)
    assert 1 <= len(hits) <= POLICY.max_k


def test_search_exact_blocks_accepts_keep():
    source = _source()
    baseline = search_exact_blocks([source], "预算审批流程", 2)
    widened = search_exact_blocks([source], "预算审批流程", 2, keep=POLICY)
    assert len(widened) > len(baseline)
    assert [item.start for item in widened][:len(baseline)] == \
        [item.start for item in baseline]


def test_search_multi_accepts_keep():
    """多路入口：条数变多、且**原有命中一条不少**。

    这里刻意不断言"前缀相同"：多路的保底席位是 `ceil(want/2)` 条，`want` 从 3 变成
    6 时席位从 2 变成 3，交付顺序本来就会变（那是 `search_multi` 自己的口径，见它的
    docstring）。单路那两个入口（`search_parent` / `search_exact_blocks`）才钉前缀。
    """
    source = _source()
    queries = ["预算审批流程", "谁能批这笔钱"]
    baseline = asyncio.run(search_multi([source], queries, 3))
    widened = asyncio.run(search_multi([source], queries, 3, keep=POLICY))
    assert len(widened) > len(baseline)
    assert len(widened) <= POLICY.max_k
    kept = {(item.source_id, item.start, item.end) for item in widened}
    assert {(item.source_id, item.start, item.end) for item in baseline} <= kept


def test_top_k_is_a_lower_bound_not_a_cap():
    """`KeepPolicy.budget` 把 `top_k` 解释成下界：下游要 `max(max_k, top_k)` 条。"""
    assert POLICY.budget(3) == POLICY.max_k
    assert POLICY.budget(8) == 8
    assert POLICY.budget(1) == POLICY.max_k


@pytest.mark.parametrize("policy", [KeepPolicy(min_k=1, max_k=1, rel=1.0)])
def test_a_policy_of_one_reproduces_the_old_behaviour(policy):
    """`min_k=max_k=1` 时与"取第一条"逐字段相同——这是把老口径留在手边的退路。"""
    source = _source()
    baseline = search_parent([source], "预算审批流程", 1)
    kept = search_parent([source], "预算审批流程", 1, keep=policy)
    assert [item.model_dump() for item in kept] == \
        [item.model_dump() for item in baseline]


def test_default_policy_matches_the_measured_numbers():
    """三个入口的 policy 与文档里那张表一致（改了数就得改文档与量测脚本）。"""
    from app.agent import tools

    assert service.DEFAULT_KEEP == KeepPolicy(min_k=3, max_k=6, rel=0.85)
    assert tools.DEEP_BRANCH_KEEP.max_k == 6
    assert tools.DETAIL_RETRIEVAL_KEEP.max_k == 5
    assert tools.RAG_QUERY_KEEP.max_k == 8
    assert tools.DEEP_BRANCH_TOP_K == tools.DEEP_BRANCH_KEEP.min_k
    assert tools.DETAIL_RETRIEVAL_TOP_K == tools.DETAIL_RETRIEVAL_KEEP.min_k


def test_the_switch_defaults_to_off(monkeypatch):
    """**总开关默认关**——这条钉的是"量完之后翻回来"这个决定本身。

    为什么它值得一条用例：默认值是这一轮唯一被推翻的东西（代码与策略都留着）。
    下一次有人想翻回 `1`，会先撞到这面墙，然后去读
    `tools.KEEP_BY_SCORE_ENV` 上面那三条依据（+5.9pp 是低端样本造成的假象、
    样本间波动 20–30pp、on 臂 33% 失败率 vs off 0%）。

    同时钉住三件事：
      - 缺省（`delenv`）→ 关；
      - `1/on/true/yes` → 开；
      - `0/off/空串/"随便写的"` → 关（正列举真值，不是"不在假值集合里"）。
    """
    from app.agent import tools

    monkeypatch.delenv(tools.KEEP_BY_SCORE_ENV, raising=False)
    assert tools.keep_by_score_enabled() is False
    assert tools.branch_keep() is None
    assert tools.detail_keep() is None
    assert tools.rag_keep() is None

    for value in ("1", "on", "true", "yes", " ON "):
        monkeypatch.setenv(tools.KEEP_BY_SCORE_ENV, value)
        assert tools.keep_by_score_enabled() is True, value
        assert tools.branch_keep() == tools.DEEP_BRANCH_KEEP

    for value in ("0", "off", "false", "no", "", "随便写的"):
        monkeypatch.setenv(tools.KEEP_BY_SCORE_ENV, value)
        assert tools.keep_by_score_enabled() is False, value
        assert tools.branch_keep() is None
