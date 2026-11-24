"""风格画像的**第二层**（`app/prefs/profile.py`）：聚类、权重、以及排序里那一项。

这一层的用例全部离线：聚类喂人工构造的向量（三个正交方向），加权把
`profile._cosines` 换掉。真正的向量服务只在 `scripts/measure_profile_ab.py` 里跑。

三条硬约束各自有用例钉着：

  1. **只在排序用，不在召回用**：返回的列表与输入等长、同一批对象；
  2. **α 有上限，且只在过了主题门之后加分**；
  3. **带时间衰减**：旧样本对质心的影响按 `0.5 ** (age / 30)` 缩小。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.prefs import profile


@dataclass
class _Hit:
    """`EvidenceHit` 的鸭子类型替身：`boost` 只读这三个字段。"""

    score: float
    quote: str = "一段原文"
    source_id: str = "s1"
    start: int = 0

    def __hash__(self) -> int:
        return id(self)


def _unit(*values: float) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


# ---------------------------------------------------------------- 聚类


def test_cluster_recovers_three_orthogonal_groups():
    """三组正交方向上的向量应该被分回三组——这是聚类在"可分"数据上的基准。"""
    matrix = []
    for axis in range(3):
        base = [0.0, 0.0, 0.0]
        base[axis] = 1.0
        for noise in (0.0, 0.05, -0.05):
            vector = list(base)
            vector[(axis + 1) % 3] = noise
            matrix.append(_unit(*vector))
    groups = profile.cluster(matrix, 3)
    assert len(groups) == 3
    assert sorted(len(members) for _, members in groups) == [3, 3, 3]
    # 同一组的成员必须来自同一根轴
    for _center, members in groups:
        assert len({index // 3 for index in members}) == 1


def test_cluster_is_deterministic():
    """同样的输入必须给出同样的簇。**这不是洁癖**：`profile_vectors` 是落库的，
    每跑一次换一批方向，A/B 的差异就全变成聚类噪声。"""
    matrix = [_unit(1, 0.2 * index % 0.7, index % 2) for index in range(10)]
    first = profile.cluster(matrix, 3)
    second = profile.cluster(matrix, 3)
    assert [[members for _, members in first]] == [[members for _, members in second]]


def test_cluster_drops_empty_groups():
    """全同的向量切 5 份时只该剩 1 个簇——空簇被丢掉，不硬凑。"""
    matrix = [[1.0, 0.0]] * 5
    groups = profile.cluster(matrix, 5)
    assert len(groups) == 1
    assert len(groups[0][1]) == 5


def test_cluster_handles_no_vectors():
    assert profile.cluster([], 3) == []


def test_pick_k_stays_within_three_and_five():
    """3–5 个方向是产品口径，不是算法自由度。"""
    assert profile._pick_k(5) == 3
    assert profile._pick_k(18) == 3
    assert profile._pick_k(24) == 4
    assert profile._pick_k(60) == 5
    assert profile._pick_k(400) == 5


def test_silhouette_is_high_for_separated_groups():
    matrix = [_unit(1, 0), _unit(0.98, 0.02), _unit(0, 1), _unit(0.02, 0.98)]
    groups = profile.cluster(matrix, 2)
    assert profile.silhouette(matrix, groups) > 0.8


def test_silhouette_scores_singletons_as_zero():
    """单样本簇对轮廓的贡献按 0 计（与 sklearn 同口径）：一个只含自己的簇，
    "与同类有多近"没有定义，不能算成"完美的一簇"。"""
    matrix = [_unit(1, 0), _unit(1, 0.01), _unit(0, 1), _unit(0.01, 1)]
    groups = [(vector, [index]) for index, vector in enumerate(matrix)]
    assert profile.silhouette(matrix, groups) == 0.0


# ---------------------------------------------------------------- 取样


def test_node_count_reads_three_shapes():
    tree = {"root": {"id": "r", "label": "r", "children": [
        {"id": "a", "label": "a", "children": []},
        {"id": "b", "label": "b", "children": [{"id": "c", "label": "c", "children": []}]},
    ]}}
    assert profile._node_count(tree) == 4
    fish = {"bones": [{"label": "骨", "children": [{"label": "子", "children": []}]},
                      {"label": "骨二", "children": []}]}
    assert profile._node_count(fish) == 3
    flow = {"stages": [{"label": "阶段", "nodes": [
        {"id": "n1", "label": "n1", "children": []}]}]}
    assert profile._node_count(flow) == 2
    assert profile._node_count({}) == 0


def test_mindmap_text_uses_titles_not_details():
    """只用标题与分支标签：detail 是模型写的说明，人人相似，算进方向里会把所有图
    拉向同一个中心。"""
    payload = {"title": "机器学习", "root": {"id": "r", "label": "机器学习", "children": [
        {"id": "a", "label": "监督学习", "detail": "很长很长的说明" * 20, "children": []},
    ]}}
    text = profile._mindmap_text(payload)
    assert "机器学习" in text and "监督学习" in text
    assert "很长很长的说明" not in text


def test_samples_skip_boilerplate_mindmaps():
    """节点数低于 `MIN_MINDMAP_NODES` 的图不进样本（验收图的标题是脚手架）。"""
    import json
    from contextlib import closing

    from app.mindmaps import repository

    small = {"schema_version": 2, "title": "树基线（改动前后逐像素对比用）",
             "root": {"id": "r", "label": "基线", "children": [
                 {"id": "a", "label": "甲", "children": []}]}}
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO mindmaps (id, version, content, updated_at) VALUES"
            " ('m1', 1, ?, '2026-11-19T00:00:00.000Z')",
            (json.dumps(small, ensure_ascii=False),))
    assert profile._samples("") == []


def test_samples_are_deduplicated_by_text():
    from contextlib import closing

    from app.mindmaps import repository

    rows = (("a.txt", "同一份材料"), ("a.txt", "同一份材料"), ("b.txt", "另一份"))
    with closing(repository.connect()) as conn, conn:
        for index, (name, body) in enumerate(rows):
            conn.execute(
                "INSERT INTO library_sources (id, content_hash, filename, kind, text,"
                " metadata, created_at, last_used_at) VALUES (?, ?, ?, 'text', ?, '{}',"
                " '2026-11-19T00:00:00.000Z', '2026-11-19T00:00:00.000Z')",
                (f"s{index}", f"h{index}", name, body))
    texts = [sample.text for sample in profile._samples("")]
    assert texts == ["a.txt\n同一份材料", "b.txt\n另一份"]


# ---------------------------------------------------------------- 建方向


def test_build_directions_is_empty_without_embedding_service():
    """没配向量服务时返回空列表——降级不是故障，[整次生成] 一行都不该变。"""
    samples = [profile.Sample(f"文本{index}", "material", "2026-11-19T00:00:00.000Z")
               for index in range(8)]
    assert profile.build_directions(samples) == []


def test_build_directions_needs_min_samples():
    samples = [profile.Sample(f"文本{index}", "material", None) for index in range(4)]
    assert profile.build_directions(samples) == []


def test_build_directions_uses_cache_and_decays_weights(monkeypatch):
    """走缓存取向量、按 `decay(age)` 加权：三条同向、一条很久以前的异向。

    权重进两处——质心的加权平均、以及簇的 `weight`（"这个方向现在有多重"）。
    """
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    old = (now - timedelta(days=180)).isoformat()
    fresh = now.isoformat()
    samples = [
        profile.Sample("甲", "material", fresh, "甲"),
        profile.Sample("乙", "material", fresh, "乙"),
        profile.Sample("丙", "material", fresh, "丙"),
        profile.Sample("丁", "material", old, "丁"),
        profile.Sample("戊", "material", fresh, "戊"),
    ]
    vectors = {
        "甲": [1.0, 0.0], "乙": [1.0, 0.01], "丙": [0.99, 0.0],
        "丁": [0.0, 1.0], "戊": [0.98, 0.02],
    }
    monkeypatch.setattr(profile.embedding, "configured", lambda: True)
    monkeypatch.setattr(profile.embedding, "model_name", lambda: "fake")
    monkeypatch.setattr(profile.embedding, "dimension", lambda: 2)
    monkeypatch.setattr(profile.vector_cache, "get_vectors",
                        lambda keys, model: {profile.vector_cache.text_hash(text): vector
                                             for text, vector in vectors.items()})
    monkeypatch.setattr(profile.vector_cache, "put_vectors", lambda items, model: len(items))
    monkeypatch.setattr(profile, "utcnow", lambda: now)

    directions = profile.build_directions(samples)
    assert len(directions) >= 2
    heavy = directions[0]
    assert heavy.samples >= 3          # 甲乙丙戊里至少三条落在同一个方向
    assert heavy.weight > 0.7          # 三条新样本主导了这个方向
    # 丁是半年前的：decay(180/30) = 0.5**6 ≈ 0.016，权重只占 0.4%
    assert 0.0 < directions[-1].weight < 0.05


# ---------------------------------------------------------------- 加权（排序）


def _stored(weight: float = 1.0, label: str = "方向") -> profile.Stored:
    return profile.Stored(label=label, vector=[1.0, 0.0], weight=weight,
                          samples=3, updated_at="2026-11-19T00:00:00.000Z")


def test_boost_keeps_same_objects_and_length(monkeypatch):
    """约束 1 的可执行版本：**不增、不减、不换对象**，只改顺序。"""
    hits = [_Hit(score=0.08), _Hit(score=0.075), _Hit(score=0.02)]
    monkeypatch.setattr(profile, "_cosines",
                        lambda h, v: {0: (0.1, 0), 1: (1.0, 0), 2: (0.0, 0)})
    result = profile.boost(hits, directions=[_stored()])
    assert len(result.hits) == len(hits)
    assert {id(hit) for hit in result.hits} == {id(hit) for hit in hits}
    assert [hit.score for hit in result.hits] == [0.075, 0.08, 0.02]


def test_boost_reports_which_hits_were_lifted(monkeypatch):
    """`lifted` 是过滤气泡检验的唯一输入：只有它能回答"加权上来的那几条后来怎样了"。"""
    hits = [_Hit(score=0.08), _Hit(score=0.075), _Hit(score=0.02)]
    monkeypatch.setattr(profile, "_cosines",
                        lambda h, v: {0: (0.1, 0), 1: (1.0, 0), 2: (0.0, 0)})
    result = profile.boost(hits, directions=[_stored()])
    assert result.lifted == [hits[1]]


def test_boost_does_not_lift_hits_below_the_topic_gate(monkeypatch):
    """约束 2：主题分低于本批最强命中一半的那一条，画像贴合度 1.0 也不加分。

    这是"α 有上限"之外的第二道闸：它保证个性化**改不了"与主题无关"这个判断**。
    """
    hits = [_Hit(score=0.10), _Hit(score=0.01)]
    monkeypatch.setattr(profile, "_cosines",
                        lambda h, v: {0: (0.0, 0), 1: (1.0, 0)})
    result = profile.boost(hits, directions=[_stored()])
    assert result.hits[0] is hits[0]
    assert result.lifted == []


def test_boost_clamps_alpha_to_upper_bound(monkeypatch):
    """α 传 0.9 与传 `MAX_ALPHA` 的结果必须一样。"""
    hits = [_Hit(score=0.08), _Hit(score=0.075)]
    monkeypatch.setattr(profile, "_cosines",
                        lambda h, v: {0: (0.0, 0), 1: (1.0, 0)})
    large = profile.boost(hits, directions=[_stored()], alpha=0.9)
    capped = profile.boost(hits, directions=[_stored()], alpha=profile.MAX_ALPHA)
    assert large.hits == capped.hits


def test_boost_scales_alpha_by_direction_weight(monkeypatch):
    """只占 10% 样本的方向，α 也只有最强方向的 10%——弱方向不该有同等话语权。"""
    hits = [_Hit(score=0.08), _Hit(score=0.075)]
    monkeypatch.setattr(profile, "_cosines", lambda h, v: {0: (0.0, 0), 1: (1.0, 1)})
    result = profile.boost(hits, directions=[_stored(1.0), _stored(0.1, "弱方向")])
    # 0.075 归一化成 0，加上 cos(1.0) × α(0.15 × 0.1) = 0.015 < 1.0
    assert result.hits[0] is hits[0]
    assert result.lifted == []


def test_boost_is_a_no_op_without_directions(monkeypatch):
    hits = [_Hit(score=0.08), _Hit(score=0.02)]
    monkeypatch.setattr(profile, "_cosines", lambda h, v: {})
    result = profile.boost(hits, directions=[])
    assert result.hits == hits and result.lifted == []


def test_boost_is_a_no_op_when_cosines_is_empty(monkeypatch):
    """向量服务不可用时 `_cosines` 返回空字典：按原序交付，不抛错。"""
    hits = [_Hit(score=0.08), _Hit(score=0.02)]
    monkeypatch.setattr(profile, "_cosines", lambda h, v: {})
    result = profile.boost(hits, directions=[_stored()])
    assert result.hits == hits and result.lifted == []


def test_boost_does_not_touch_score_field(monkeypatch):
    """`score` 是检索侧的融合分，**不改**：改了"这条为什么是 0.07"就再也说不清。"""
    hits = [_Hit(score=0.08), _Hit(score=0.075)]
    monkeypatch.setattr(profile, "_cosines", lambda h, v: {0: (0.2, 0), 1: (0.9, 0)})
    profile.boost(hits, directions=[_stored()])
    assert [hit.score for hit in hits] == [0.08, 0.075]


# ---------------------------------------------------------------- 读回


def test_stored_skips_vectors_with_wrong_dimension(monkeypatch):
    """换了 embedding 维度之后，旧向量与新查询向量不在同一个空间里，必须整条跳过。"""
    from contextlib import closing

    from app.mindmaps import repository

    monkeypatch.setattr(profile.embedding, "dimension", lambda: 3)
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO profile_vectors (id, vector, label, weight, samples,"
            " created_at, updated_at, map_id) VALUES ('d0', ?, '方向', 1.0, 2,"
            " '2026-11-19T00:00:00.000Z', '2026-11-19T00:00:00.000Z', 'run:x')",
            (profile.vector_cache.pack([1.0, 0.0]),))
    assert profile.stored(map_id=None) == []


def test_stale_is_true_when_empty():
    assert profile.stale() is True


def test_stale_is_false_right_after_rebuild(monkeypatch):
    from contextlib import closing

    from app.mindmaps import repository

    monkeypatch.setattr(profile.embedding, "dimension", lambda: 2)
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO profile_vectors (id, vector, label, weight, samples,"
            " created_at, updated_at, map_id) VALUES ('d0', ?, '方向', 1.0, 2,"
            " strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
            " '')",
            (profile.vector_cache.pack([1.0, 0.0]),))
    assert profile.stale() is False


def test_profile_vectors_schema_is_the_agreed_one():
    """`map_id` 是后来加的列，顺序排在最后——老行的该列是 NULL（"当时还没有隔离"）。"""
    from contextlib import closing

    from app.mindmaps import repository

    with closing(repository.connect()) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(profile_vectors)")]
    assert columns == ["id", "vector", "label", "weight", "samples",
                       "created_at", "updated_at", "map_id"]


def test_switch_is_off_by_default(monkeypatch):
    monkeypatch.delenv(profile.SWITCH_ENV, raising=False)
    assert profile.enabled() is False
    monkeypatch.setenv(profile.SWITCH_ENV, "1")
    assert profile.enabled() is True
