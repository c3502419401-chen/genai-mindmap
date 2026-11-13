"""实体消解的用例：聚类逻辑、阈值、别名落库、以及游走跨别名。

分两层，约定与 `tests/test_graph_neo4j.py` 相同：

  - 不碰库的那一半跑在默认 `pytest` 里：并查集、主名选择、阈值、向量缺失、
    未配置时零请求、别名链防环、落库前的去重/成环/缺节点拒绝（用假事务记录语句）；
  - 带 `integration` 标记的那一半要真 Neo4j（`MINDMAP_TEST_NEO4J_PASSWORD`），
    连不上就 skip，默认跑法不会红。

**为什么"写两次只有一条边"主要在集成层**：那是 `MERGE` 的语义，在假事务里自己实现
一遍等于自证。非集成层测的是"我们发出去的行是什么样的"（同一对只发一行、成环的
不发、缺节点的不发），以及返回值只数真正新建的那些。

`conftest.py` 已经清了 embedding 凭证、封了传输层：需要向量的用例显式 `setenv`
一个假 key/base，再把 `graph_resolve.embed_texts` 换掉——所以这里的"零 HTTP"是
"一次调用都没发"，不是"发出去被拦了"。
"""
from __future__ import annotations

import asyncio
import math
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.knowledge import graph_resolve, graph_store, vector_cache
from app.knowledge.embedding import EmbeddingError
from app.knowledge.graph_resolve import (
    MergeCandidate,
    find_merge_candidates,
    resolve_entities,
)
from app.knowledge.graph_store import (
    GraphPath,
    add_aliases,
    add_edge,
    clear_aliases,
    close_driver,
    neighbors,
    resolve,
    stats,
    upsert_entity,
)

URI_ENV = "MINDMAP_TEST_NEO4J_URI"
USER_ENV = "MINDMAP_TEST_NEO4J_USER"
PASSWORD_ENV = "MINDMAP_TEST_NEO4J_PASSWORD"
DATABASE_ENV = "MINDMAP_TEST_NEO4J_DATABASE"
ALLOW_WIPE_ENV = "MINDMAP_TEST_NEO4J_ALLOW_WIPE"

DEFAULT_URI = "bolt://127.0.0.1:7687"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------- 向量与候选的夹具
#
# 4 维向量，各自落在不同的轴上：轴 0 上的 `_AXIS0` 与别的轴上的向量**正交**，
# 所以每一对的余弦都被单独捏在手里，不会被"另外两对恰好也像"污染。

_AXIS0 = [1.0, 0.0, 0.0, 0.0]


def pair_vector(cosine: float, axis: int) -> list[float]:
    """与 `_AXIS0` 余弦恰好是 `cosine` 的单位向量（落在第 `axis` 维上）。"""
    values = [0.0, 0.0, 0.0, 0.0]
    values[0] = cosine
    values[axis] = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return values


def enable_embedding(monkeypatch):
    """让 `embedding.configured()` 为真。真请求不会发：`embed_texts` 已被换掉。"""
    monkeypatch.setenv("MINDMAP_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("MINDMAP_EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")


def install_degrees(monkeypatch, rows):
    """把候选源换成给定的一串 `(名字, 边数)`；不碰 Neo4j。"""
    monkeypatch.setattr(graph_store, "entity_degrees", lambda *, limit=500: list(rows))


def fake_embed(monkeypatch, vectors, *, cached=None):
    """换掉 `embed_texts` 与向量缓存，返回"真正被送去算向量的文本"列表。"""
    sent: list[str] = []

    def embed(texts):
        sent.extend(texts)
        return [vectors.get(text) for text in texts]

    monkeypatch.setattr(graph_resolve, "embed_texts", embed)
    monkeypatch.setattr(graph_resolve.vector_cache, "get_vectors",
                        lambda hashes, model: dict(cached or {}))
    monkeypatch.setattr(graph_resolve.vector_cache, "put_vectors",
                        lambda items, model: 0)
    return sent

# ------------------------------------------------- 1. 两段式判定（纯函数，可穷举）
#
# 判定是这一层的核心，所以它被写成一个不看库、不看网络的纯函数：每一条规则都能在
# 这里穷举，而不用先造一张图。


def test_structural_pair_is_enough_even_when_cosine_is_zero():
    """结构关系单独成立：余弦 0.1 也合并，没有向量也合并。

    这是两段式的全部意义。第一版只有余弦一条闸，而「预算审批流程|审批流程」是
    0.9099——低于任何能挡住「市场监管总局|市场监管部门」（0.9241）的阈值。结构关系
    是构词关系，不是"像"，它不需要分数背书。
    """
    merged, reason, strength = graph_resolve._should_merge("审批流程", "预算审批流程", 0.1)
    assert (merged, reason) == (True, "substring")
    assert strength == pytest.approx(4 / 6)
    # 拿不到向量（embed 部分失败）也照样合：结构证据不依赖向量。
    assert graph_resolve._should_merge("审批流程", "预算审批流程", None)[:2] == (
        True, "substring")


def test_embedding_only_backs_up_near_identical_pairs():
    """余弦兜底只收 ≥0.95 的；0.9241 那一档挡在外面。"""
    assert graph_resolve._should_merge("甲", "乙", 0.96)[:2] == (True, "embedding")
    assert graph_resolve._should_merge("甲", "乙", 0.95)[:2] == (True, "embedding")
    # 实测里最贵的那一对：泛指「市场监管部门」vs 专名「市场监管总局」（bge 0.9241）。
    assert graph_resolve._should_merge("市场监管总局", "市场监管部门", 0.9241)[0] is False
    assert graph_resolve._should_merge("甲", "乙", None) == (False, "", 0.0)


def test_abbreviation_survives_a_middle_deletion():
    """顺序子序列而不是连续子串：简称可以在中间整段删字。

    「市场监管总局」不是「国家市场监督管理总局」的子串（少了「管理」两个字），
    按连续子串实现，这个旗舰案例会被漏掉——它是这一层最典型的那个别名。
    """
    assert "市场监管总局" not in "国家市场监督管理总局"
    assert graph_resolve._abbreviation_ratio(
        "国家市场监督管理总局", "市场监管总局") == pytest.approx(6 / 10)
    assert graph_resolve._abbreviation_ratio(
        "数字化转型办公室", "数字化转型办") == pytest.approx(6 / 8)
    # 乱序不算：子序列要求顺序一致。
    assert graph_resolve._abbreviation_ratio("总局市场", "国家市场监督管理总局") == 0.0
    assert graph_resolve._abbreviation_ratio("甲", "乙") == 0.0
    assert graph_resolve._abbreviation_ratio("甲", "甲") == 0.0


def test_upper_concept_is_not_an_abbreviation():
    """上位概念 ⊂ 专名不是简称：短名丢了类型词 → 不合并。

    没有这道闸，结构规则会造出一族新的错合，而且因为没有分数兜着，比余弦那次更硬：
    「数据安全」合进「数据安全法」之后，图上会出现"某条法律适用于数据安全"的假路径。
    """
    for short, long in (
        ("数据安全", "数据安全法"),
        ("市场监管", "市场监管总局"),
        ("采购", "采购管理办法"),
        ("董事", "董事会"),
        ("数据安全", "数据出境安全评估"),
        ("董事会", "董事会议事规则"),
    ):
        assert graph_resolve._abbreviation_ratio(short, long) == 0.0, (short, long)
    # 反过来：短名自己保留了类型词，就是简称。
    assert graph_resolve._abbreviation_ratio("审批流程", "预算审批流程") > 0
    assert graph_resolve._abbreviation_ratio("数字化转型办", "数字化转型办公室") > 0
    assert graph_resolve._abbreviation_ratio("审批流程", "预算审批流程") > 0


def test_generic_term_never_becomes_an_alias():
    """泛称短名直接拒：`预算` 不是 `年度预算` 的另一个名字。

    这一族是结构规则必错的地方——长名那边没有类型词可丢（「年度预算」以「预算」结尾，
    而「预算」不是类型词），只能靠一张封闭的泛称表踩刹车。失效模式是漏合。
    """
    for generic in ("预算", "审计", "风险", "合规", "数据", "董事"):
        assert generic in graph_resolve.GENERIC_TERMS
    for short, long in (("预算", "年度预算"), ("审计", "内部审计"),
                        ("风险", "风险控制"), ("合规", "合规审查")):
        assert graph_resolve._abbreviation_ratio(short, long) == 0.0, (short, long)


def test_subsequence_coincidence_is_rejected():
    """子序列巧合：两个不同的专名共享高频字，但短名没有一整块连续的字。

    `数据安全法` 是 `数据出境安全评估办法` 的子序列（数|据|安|全|法 都在），连续段
    却只有「数据」/「安全」两块、占不到短名的一半——真简称总有一大块字是连着的
    （市场监管总局的「市场监督」、董秘的「董」、个保法的「保护法」）。
    """
    assert graph_resolve._abbreviation_ratio("数据安全法", "数据出境安全评估办法") == 0.0
    assert graph_resolve._longest_common_run("数据安全法", "数据出境安全评估办法") == 2
    # 对照：董秘靠一个连续段就过闸（2 * 1 >= 2）。
    assert graph_resolve._abbreviation_ratio("董秘", "董事会秘书") > 0


def test_length_ratio_floor_keeps_the_institution_full_name_out():
    """0.4 的下限挡住「财政部 / 中华人民共和国财政部」（3/10 = 0.3）。

    这是**有意的漏合**：机构全称的限定词太长，短名只占三成。收它要把下限降到 0.3，
    代价是 3 字短名 × 8–10 字长名这一整档全部进来。漏合是缺失，错合是伪造。
    """
    assert graph_resolve.MIN_LENGTH_RATIO == pytest.approx(0.4)
    assert graph_resolve._abbreviation_ratio("财政部", "中华人民共和国财政部") == 0.0
    assert graph_resolve._abbreviation_ratio("发改委", "国家发展和改革委员会") == 0.0


# ------------------------------------------------------- 2. 并查集与主名（纯函数）


def test_union_find_merges_transitively():
    """A~B、B~C 但 A 不~C：仍然合成一组。

    这是并查集存在的全部理由：按"从第一个名字往下连"的写法，先遇到 A 就只合了
    {A,B}，先遇到 B 就合 {A,B,C}——同一个库跑两次给出两个答案。
    """
    edges = [("甲", "乙", 0.95, "embedding"), ("乙", "丙", 0.96, "embedding")]
    groups = graph_resolve._group_pairs(edges)
    assert [set(members) for members, _strength, _reason in groups] == [{"甲", "乙", "丙"}]
    # 组内最弱一环：不是 0.96，也不是"A 与 C 的相似度"（它们之间没有边）。
    assert groups[0][1] == pytest.approx(0.95)


def test_union_find_keeps_two_groups_apart():
    """两对互不相连的对 → 两个组；兜底组排在结构组前面（更需要人看）。"""
    edges = [
        ("甲", "乙", 0.97, "embedding"),
        ("审批流程", "预算审批流程", 0.67, "substring"),
    ]
    groups = graph_resolve._group_pairs(edges)
    assert [set(members) for members, _s, _r in groups] == [
        {"甲", "乙"}, {"审批流程", "预算审批流程"}]
    assert [reason for _members, _s, reason in groups] == ["embedding", "substring"]
    # 没有边就不成组：单打独斗的名字不该让"候选组数"虚高。
    assert graph_resolve._group_pairs([]) == []


def test_group_reason_reports_the_weakest_link():
    """一组里既有结构边又有兜底边 → 整组报 embedding（最弱的那一环决定成色）。"""
    groups = graph_resolve._group_pairs([
        ("甲", "甲全称", 0.6, "substring"),
        ("甲全称", "甲简写", 0.97, "embedding"),
    ])
    assert len(groups) == 1
    members, strength, reason = groups[0]
    assert set(members) == {"甲", "甲全称", "甲简写"}
    assert (strength, reason) == (pytest.approx(0.97), "embedding")


def test_canonical_prefers_more_edges_then_longer_name():
    """主名先看边数（信息量），并列看名字长度（正式程度），再并列看字典序。"""
    assert graph_resolve._pick_canonical(["甲", "甲甲"], {"甲": 5, "甲甲": 2}) == "甲"
    assert graph_resolve._pick_canonical(
        ["市场监管总局", "国家市场监督管理总局"],
        {"市场监管总局": 3, "国家市场监督管理总局": 3},
    ) == "国家市场监督管理总局"
    # 两档都并列：只求确定性，不承载语义。
    assert graph_resolve._pick_canonical(["乙", "丙"], {"乙": 1, "丙": 1}) == "丙"


# ------------------------------------------------------- 3. 阈值与向量（编排）


def test_cosine_fallback_respects_its_threshold(monkeypatch):
    """0.96 那对靠兜底合、0.90 与 0.50 那对不合（默认兜底阈值 0.95）。"""
    enable_embedding(monkeypatch)
    install_degrees(monkeypatch, [("甲", 3), ("乙", 2), ("丙", 1), ("丁", 0)])
    fake_embed(monkeypatch, {
        "甲": _AXIS0,
        "乙": pair_vector(0.96, 1),
        "丙": pair_vector(0.90, 2),
        "丁": pair_vector(0.50, 3),
    })

    candidates = run(find_merge_candidates())

    assert [(item.canonical, item.aliases) for item in candidates] == [("甲", ["乙"])]
    assert candidates[0].similarity == pytest.approx(0.96)
    assert candidates[0].reason == "embedding"
    # 阈值是兜底那一路的参数：调到 0.89，0.90 那对就该进来。
    relaxed = run(find_merge_candidates(threshold=0.89))
    assert sorted(name for item in relaxed for name in item.aliases) == ["丙", "乙"]


def test_threshold_does_not_touch_structural_pairs(monkeypatch):
    """把兜底阈值调到 1.0，结构对照样合并——`threshold` 只管数值那一路。"""
    enable_embedding(monkeypatch)
    install_degrees(monkeypatch, [("预算审批流程", 3), ("审批流程", 2)])
    fake_embed(monkeypatch, {"审批流程": _AXIS0, "预算审批流程": pair_vector(0.10, 1)})

    candidates = run(find_merge_candidates(threshold=1.0))

    assert [(item.canonical, item.aliases) for item in candidates] == [
        ("预算审批流程", ["审批流程"])]
    assert candidates[0].reason == "substring"


def test_missing_vector_only_disables_the_cosine_path(monkeypatch):
    """embed 失败（None）只挡余弦那一路，结构那一路照常。

    `丙` 与 `甲` 的余弦是 0.96，本该一起合；它没有向量，所以只能缺席（补零向量会把
    "没结论"伪装成"比过了，不相似"）。但「丁简写」与「丁」是子序列关系，没有向量也
    照样合并。
    """
    enable_embedding(monkeypatch)
    install_degrees(monkeypatch,
                    [("甲", 3), ("乙", 2), ("丙", 2), ("丁简写", 3), ("丁简", 0)])
    fake_embed(monkeypatch, {
        "甲": _AXIS0,
        "乙": pair_vector(0.96, 1),
        "丙": None,
        "丁简写": None,
        "丁简": None,
    })

    candidates = run(find_merge_candidates())

    assert [(item.canonical, item.aliases, item.reason) for item in candidates] == [
        ("甲", ["乙"], "embedding"),
        ("丁简写", ["丁简"], "substring"),
    ]


def test_unconfigured_embedding_returns_empty_without_any_call(monkeypatch):
    """没配 embedding → `[]`，且一次向量请求、一次图查询都不发。

    `conftest.py` 已经把凭证清空，所以这里的默认状态就是"未配置"。两条 `boom`
    证明的是"提前返回"而不是"发出去被拦了"。
    """
    def boom(*args, **kwargs):
        raise AssertionError("未配置时不该发向量请求，也不该去查图")

    monkeypatch.setattr(graph_resolve, "embed_texts", boom)
    monkeypatch.setattr(graph_store, "entity_degrees", boom)

    assert run(find_merge_candidates()) == []


def test_credentials_vanishing_mid_flight_is_still_unconfigured(monkeypatch):
    """预检通过、真正调用时才发现没凭证（并发下会发生）：仍按"未配置"返回空。"""
    enable_embedding(monkeypatch)
    install_degrees(monkeypatch, [("甲", 3), ("乙", 2)])

    def boom(texts):
        raise graph_resolve.EmbeddingNotConfigured("凭证被清掉了")

    monkeypatch.setattr(graph_resolve, "embed_texts", boom)
    monkeypatch.setattr(graph_resolve.vector_cache, "get_vectors",
                        lambda hashes, model: {})

    assert run(find_merge_candidates()) == []


def test_embedding_outage_is_not_disguised_as_no_candidates(monkeypatch):
    """配了但调不通 → 抛错，不返回 `[]`。

    返回空会被下游读成"没有可合并的名字"，而实际上一次都没比过——那正是这一轮要
    消掉的那种哑失败（`neighbors` 的 `[]` 与"确实无关"同形）。
    """
    enable_embedding(monkeypatch)
    install_degrees(monkeypatch, [("甲", 3), ("乙", 2)])

    def boom(texts):
        raise EmbeddingError("全部 1 批向量请求失败")

    monkeypatch.setattr(graph_resolve, "embed_texts", boom)
    monkeypatch.setattr(graph_resolve.vector_cache, "get_vectors",
                        lambda hashes, model: {})

    with pytest.raises(EmbeddingError):
        run(find_merge_candidates())


def test_vectors_are_reused_from_the_cache(monkeypatch):
    """命中缓存的名字不再送去算一次：缓存键就是 `vector_cache.text_hash`。"""
    enable_embedding(monkeypatch)
    install_degrees(monkeypatch, [("甲", 3), ("乙", 2)])
    sent = fake_embed(
        monkeypatch,
        {"乙": pair_vector(0.96, 1)},
        cached={vector_cache.text_hash("甲"): _AXIS0},
    )

    assert [item.aliases for item in run(find_merge_candidates())] == [["乙"]]
    assert sent == ["乙"]          # 甲 走了缓存


def test_candidates_are_logged_as_counts_not_names(monkeypatch, caplog):
    """日志只有形状：条数、依据分布、耗时，没有一个实体名（名字就是材料内容）。"""
    enable_embedding(monkeypatch)
    install_degrees(monkeypatch, [("甲", 3), ("乙", 2)])
    fake_embed(monkeypatch, {"甲": _AXIS0, "乙": pair_vector(0.96, 1)})

    with caplog.at_level("INFO"):
        run(find_merge_candidates())

    assert "候选组 1" in caplog.text and "兜底边 1" in caplog.text
    assert "甲" not in caplog.text


# --------------------------------------------------------- 3. 别名落库（假事务）


class FakeResult:
    def __init__(self, records=(), created: int = 0, deleted: int = 0) -> None:
        self._records = list(records)
        self._summary = SimpleNamespace(
            counters=SimpleNamespace(relationships_created=created,
                                     relationships_deleted=deleted))

    def single(self):
        return self._records[0] if self._records else None

    def consume(self):
        return self._summary

    def __iter__(self):
        return iter(self._records)


class FakeAliasStore:
    """假事务：按语句内容回答，写别名时按 `(别名, 主名)` 去重并计数。

    这个去重是照着 `MERGE` 的语义实现的——所以本层能证明的只是"我们发出去的每一
    行都是想要的、返回值只数新建的"，**真幂等在集成层**。两个都写是有意的：这一
    层能在没有 Neo4j 的机器上跑，而且语句与传参对不上时会立刻红。
    """

    def __init__(self, nodes=(), aliases=()) -> None:
        self.nodes = set(nodes)
        self.aliases = {(alias, canonical) for alias, canonical in aliases}
        self.calls: list[tuple[str, dict]] = []

    def run(self, query: str, params=None, **_kwargs):
        params = dict(params or {})
        self.calls.append((query, params))
        if "RETURN a.name AS alias" in query:
            return FakeResult([{"alias": a, "canonical": c}
                               for a, c in sorted(self.aliases)])
        if "ORDER BY name, created_at" in query:
            wanted = set(params.get("names", []))
            return FakeResult([
                {"name": name, "id": index, "type": "",
                 "created_at": "2026-01-01T00:00:00.000Z"}
                for index, name in enumerate(sorted(self.nodes)) if name in wanted
            ])
        if "MERGE (a)-[r:ALIAS_OF]->(b)" in query:
            created = 0
            for row in params["rows"]:
                pair = (row["alias"], row["canonical"])
                if pair not in self.aliases:
                    self.aliases.add(pair)
                    created += 1
            return FakeResult(created=created)
        if "DELETE r" in query:
            deleted = len(self.aliases)
            self.aliases.clear()
            return FakeResult(deleted=deleted)
        return FakeResult()

    def alias_rows(self) -> list[dict]:
        """两次调用里所有被送进 `MERGE` 的行。"""
        return [row for query, params in self.calls
                if "MERGE (a)-[r:ALIAS_OF]->(b)" in query
                for row in params["rows"]]


@contextmanager
def _pass_through(transaction):
    yield transaction


def install_alias_store(monkeypatch, store):
    monkeypatch.setattr(graph_store, "_transaction",
                        lambda read_only=False: _pass_through(store))
    return store


def test_add_aliases_is_idempotent(monkeypatch):
    """同一对写两次：第二次新建 0 条，库里仍只有一条边。"""
    store = install_alias_store(monkeypatch, FakeAliasStore(nodes={"甲", "甲全称"}))
    candidates = [MergeCandidate(canonical="甲全称", aliases=["甲"], similarity=0.95)]

    assert add_aliases(candidates) == 1
    assert add_aliases(candidates) == 0
    assert store.aliases == {("甲", "甲全称")}
    # 两次都发了同一行，是 `MERGE` 让它落在一条边上——本层能钉住的是"发出去的行"。
    assert [(row["alias"], row["canonical"]) for row in store.alias_rows()] == [
        ("甲", "甲全称"), ("甲", "甲全称")]


def test_add_aliases_dedupes_within_one_call(monkeypatch):
    """同一对在一个批次里出现两次也只会发一行（并列候选不该变成两条边）。"""
    store = install_alias_store(monkeypatch, FakeAliasStore(nodes={"甲", "甲全称"}))
    candidates = [
        MergeCandidate(canonical="甲全称", aliases=["甲"], similarity=0.95),
        MergeCandidate(canonical="甲全称", aliases=["甲"], similarity=0.94),
    ]

    assert add_aliases(candidates) == 1
    assert len(store.alias_rows()) == 1


def test_alias_edge_keeps_the_decision_basis(monkeypatch):
    """边上的 `source` 记的是"凭什么合的"：候选带 reason 就照它落，手工构造才回退。"""
    store = install_alias_store(monkeypatch, FakeAliasStore(nodes={"甲", "甲全称"}))
    add_aliases([MergeCandidate("甲全称", ["甲"], 0.6, "substring")])
    assert store.alias_rows()[0]["source"] == "substring"

    fallback = install_alias_store(monkeypatch, FakeAliasStore(nodes={"乙", "乙全称"}))
    add_aliases([MergeCandidate("乙全称", ["乙"], 0.97)])
    # 没有 reason（手工构造 / 老调用方）→ 回退到 `source` 参数的默认值。
    assert fallback.alias_rows()[0]["source"] == "embedding"


def test_add_aliases_refuses_a_cycle(monkeypatch):
    """A→B 已在库里时，B→A 被拒；同一批里的反向对也一样（先写的留下）。"""
    store = install_alias_store(
        monkeypatch, FakeAliasStore(nodes={"甲", "乙"}, aliases={("甲", "乙")}))
    assert add_aliases([MergeCandidate(canonical="甲", aliases=["乙"], similarity=0.95)]) == 0
    assert store.aliases == {("甲", "乙")}

    pair_store = install_alias_store(monkeypatch, FakeAliasStore(nodes={"丙", "丁"}))
    assert add_aliases([
        MergeCandidate(canonical="丙", aliases=["丁"], similarity=0.95),
        MergeCandidate(canonical="丁", aliases=["丙"], similarity=0.95),
    ]) == 1
    assert pair_store.aliases == {("丁", "丙")}


def test_add_aliases_skips_pairs_with_missing_nodes(monkeypatch):
    """端点不在库里就不写：`MATCH` 落空时 Cypher 会安静地少写一条，那种缺失没有信号。"""
    store = install_alias_store(monkeypatch, FakeAliasStore(nodes={"甲"}))
    assert add_aliases([MergeCandidate(canonical="甲全称", aliases=["甲"],
                                       similarity=0.95)]) == 0
    assert store.aliases == set()
    assert store.alias_rows() == []


def test_add_aliases_skips_self_and_blank(monkeypatch):
    """同名对与空名字一个字节都不写。"""
    store = install_alias_store(monkeypatch, FakeAliasStore(nodes={"甲", "  "}))
    assert add_aliases([
        MergeCandidate(canonical="甲", aliases=["甲", "   "], similarity=0.99),
    ]) == 0
    assert store.alias_rows() == []


def test_clear_aliases_returns_deleted_count(monkeypatch):
    store = install_alias_store(
        monkeypatch, FakeAliasStore(nodes={"甲", "乙"}, aliases={("甲", "乙")}))
    assert clear_aliases() == 1
    assert store.aliases == set()


# ------------------------------------------------------------ 4. resolve 与防环


def test_resolve_walks_the_chain_and_returns_identity(monkeypatch):
    install_alias_store(monkeypatch, FakeAliasStore(
        nodes={"甲", "乙", "丙"}, aliases={("甲", "乙"), ("乙", "丙")}))
    assert resolve("甲") == "丙"        # 多跳：别名 → 别名 → 主名
    assert resolve("乙") == "丙"
    assert resolve("丙") == "丙"        # 没有别名：返回自己
    assert resolve("不存在的名字") == "不存在的名字"


def test_resolve_survives_a_cycle(monkeypatch):
    """手工造 A→B→A：不无限循环，且返回值确定（环上没有主名，按原样返回）。"""
    install_alias_store(monkeypatch, FakeAliasStore(
        nodes={"甲", "乙"}, aliases={("甲", "乙"), ("乙", "甲")}))
    assert resolve("甲") == "甲"
    assert resolve("乙") == "乙"


def test_resolve_gives_up_after_max_hops(monkeypatch, caplog):
    """超过 `MAX_ALIAS_HOPS` 跳就原样返回并记 warning——日志里不能带名字。"""
    names = [f"实体{index:02d}" for index in range(graph_store.MAX_ALIAS_HOPS + 3)]
    aliases = {(names[index], names[index + 1]) for index in range(len(names) - 1)}
    install_alias_store(monkeypatch, FakeAliasStore(nodes=set(names), aliases=aliases))

    with caplog.at_level("WARNING"):
        assert resolve(names[0]) == names[0]
    assert "别名链超过" in caplog.text
    assert names[0] not in caplog.text          # 名字就是材料内容，不进日志


def test_resolve_without_a_graph_returns_the_name():
    """没配图存储时返回自己（归一化后），不是空串：调用方不需要第二个分支。"""
    assert resolve("  甲  ") == "甲"
    assert resolve("") == ""


def test_path_names_are_rewritten_to_the_canonical():
    """路径里的名字换成主名；换完出现重复节点就整条丢掉（那是合并后的自环）。"""
    alias_map = {"甲别名": "甲"}
    path = GraphPath(nodes=["甲别名", "乙"], relations=["依赖"],
                     evidence=[("s1", 0, 10)])
    rewritten = graph_store._canonicalize_path(path, alias_map)
    assert rewritten is not None
    assert rewritten.nodes == ["甲", "乙"]
    assert rewritten.relations == ["依赖"] and rewritten.directions == ["forward"]

    loop = GraphPath(nodes=["甲别名", "甲"], relations=["依赖"],
                     evidence=[("s1", 0, 10)])
    assert graph_store._canonicalize_path(loop, alias_map) is None


# ------------------------------------------------------ 5. resolve_entities 编排


def test_resolve_entities_defaults_to_dry_run(monkeypatch):
    """默认只算不写：返回值里 `aliases` 恒为 0，候选可读。"""
    enable_embedding(monkeypatch)
    install_degrees(monkeypatch, [("甲", 3), ("乙", 2)])
    fake_embed(monkeypatch, {"甲": _AXIS0, "乙": pair_vector(0.95, 1)})
    written: list[list] = []
    monkeypatch.setattr(graph_store, "add_aliases",
                        lambda candidates, **kwargs: written.append(candidates) or 0)

    result = run(resolve_entities())

    assert result["dry_run"] is True
    assert result["groups"] == 1 and result["aliases"] == 0
    assert result["candidates"] == [
        {"canonical": "甲", "aliases": ["乙"], "similarity": 0.95,
         "reason": "embedding"}]
    assert written == []          # 一次都没调写接口


def test_resolve_entities_reports_written_and_skipped(monkeypatch):
    """写模式的三个数：组数 / 写入 / 跳过（跳过 = 总对数 - 写入）。"""
    enable_embedding(monkeypatch)
    install_degrees(monkeypatch, [("甲", 3), ("乙", 2)])
    fake_embed(monkeypatch, {"甲": _AXIS0, "乙": pair_vector(0.95, 1)})
    # 写接口返回 0：模拟"这一对已经存在"或"成环被拒"。
    monkeypatch.setattr(graph_store, "add_aliases", lambda candidates, **kwargs: 0)

    result = run(resolve_entities(dry_run=False))

    assert (result["dry_run"], result["groups"]) == (False, 1)
    assert (result["aliases"], result["skipped"]) == (0, 1)


# ============================================================ 6. 真 Neo4j（集成）
#
# 这一份 fixture 与 `tests/test_graph_neo4j.py` 里的那份是同一套逻辑，刻意各写一
# 份：`tests/` 不是包，跨模块复用 fixture 只有两条路——把 Neo4j 搬进 conftest（每
# 个测试文件都要付这个代价），或把测试模块注册成插件（它的用例会被收集两遍）。四
# 十行重复比那两条路都便宜。


def _host_of(uri: str) -> str:
    return uri.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0].strip("[]").lower()


@pytest.fixture(scope="session")
def neo4j_settings():
    password = os.getenv(PASSWORD_ENV, "").strip()
    if not password:
        pytest.skip(f"未设置 {PASSWORD_ENV}：跳过需要 Neo4j 的用例")
    settings = {
        "uri": os.getenv(URI_ENV, "").strip() or DEFAULT_URI,
        "user": os.getenv(USER_ENV, "").strip() or "neo4j",
        "password": password,
        "database": os.getenv(DATABASE_ENV, "").strip() or "neo4j",
    }
    if (_host_of(settings["uri"]) not in LOOPBACK_HOSTS
            and os.getenv(ALLOW_WIPE_ENV, "").strip() not in {"1", "true", "yes"}):
        pytest.skip(f"拒绝清空非本机的 Neo4j（{settings['uri']}）："
                    f"确认是专用实例后设 {ALLOW_WIPE_ENV}=1")
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(settings["uri"],
                                  auth=(settings["user"], settings["password"]),
                                  connection_timeout=3.0)
    try:
        driver.verify_connectivity()
    except Exception as exc:                       # noqa: BLE001 —— 连不上的原因不重要
        pytest.skip(f"本机没有可用的 Neo4j（{type(exc).__name__}）")
    finally:
        driver.close()
    return settings


def _wipe(settings: dict) -> None:
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(settings["uri"],
                                  auth=(settings["user"], settings["password"]),
                                  connection_timeout=5.0)
    try:
        driver.execute_query("MATCH (n:Entity) DETACH DELETE n",
                             database_=settings["database"])
    finally:
        driver.close()


@pytest.fixture
def graph(neo4j_settings, monkeypatch):
    monkeypatch.setenv("MINDMAP_NEO4J_URI", neo4j_settings["uri"])
    monkeypatch.setenv("MINDMAP_NEO4J_USER", neo4j_settings["user"])
    monkeypatch.setenv("MINDMAP_NEO4J_PASSWORD", neo4j_settings["password"])
    monkeypatch.setenv("MINDMAP_NEO4J_DATABASE", neo4j_settings["database"])
    close_driver()
    _wipe(neo4j_settings)
    try:
        yield graph_store
    finally:
        close_driver()
        _wipe(neo4j_settings)


@pytest.mark.integration
def test_aliases_are_idempotent_in_the_graph(graph):
    """真库上写两次：只有一条边，`stats()` 也只数一条。"""
    upsert_entity("甲全称", "组织")
    upsert_entity("甲", "组织")
    candidates = [MergeCandidate(canonical="甲全称", aliases=["甲"], similarity=0.95)]

    assert add_aliases(candidates) == 1
    assert add_aliases(candidates) == 0
    assert stats()["aliases"] == 1
    assert stats()["merged_entities"] == 1
    assert resolve("甲") == "甲全称"
    assert resolve("甲全称") == "甲全称"


@pytest.mark.integration
def test_neighbors_reaches_edges_hanging_on_an_alias(graph):
    """边建在别名上，从主名出发也要走得到 —— 这就是本轮要消掉的那个哑失败。"""
    upsert_entity("甲", "组织")
    add_edge("甲别名", "乙", "依赖", source_id="doc-a", chunk_start=0, chunk_end=10)

    # 合并之前：从主名问，什么都没有——与"这两个实体确实无关"逐字节相同。
    assert neighbors("甲", hops=2) == []

    assert add_aliases([MergeCandidate(canonical="甲", aliases=["甲别名"],
                                       similarity=0.95)]) == 1

    for name in ("甲", "甲别名"):          # 主名与别名两个入口都通
        paths = neighbors(name, hops=2)
        assert [path.nodes for path in paths] == [["甲", "乙"]]
        assert paths[0].relations == ["依赖"]
        assert paths[0].evidence == [("doc-a", 0, 10)]
        assert paths[0].directions == ["forward"]


@pytest.mark.integration
def test_alias_does_not_count_as_an_edge_in_stats(graph):
    """别名边不是 `REL`：`edges` 不涨、孤立实体照旧算——两个指标的口径不能混。"""
    for name in ("甲全称", "甲", "乙全称", "乙"):
        upsert_entity(name, "组织")

    assert add_aliases([
        MergeCandidate(canonical="甲全称", aliases=["甲"], similarity=0.95),
        MergeCandidate(canonical="乙全称", aliases=["乙"], similarity=0.97),
    ]) == 2

    assert stats() == {"entities": 4, "edges": 0, "isolated_entities": 4,
                       "aliases": 2, "merged_entities": 2}
    assert clear_aliases() == 2
    assert stats()["aliases"] == 0 and stats()["merged_entities"] == 0


@pytest.mark.integration
def test_dry_run_writes_nothing_and_the_real_run_closes_the_island(graph, monkeypatch):
    """dry-run 一条边都不写；`dry_run=False` 之后简称才真正通到全称的证据上。"""
    add_edge("国家市场监督管理总局", "价格法", "执行", source_id="doc-a",
             chunk_start=0, chunk_end=10)
    add_edge("国家市场监督管理总局", "反垄断法", "执行", source_id="doc-b",
             chunk_start=20, chunk_end=30)
    upsert_entity("市场监管总局", "组织")

    assert neighbors("市场监管总局", hops=1) == []

    enable_embedding(monkeypatch)
    # 刻意给一对**正交**的向量（余弦 0.10，远低于 0.95 的兜底线）：这一组能不能合，
    # 完全取决于结构判定。它们在真实模型上确实不是"几乎相同"，但它们确实是同一个
    # 机构的两个名字——这正是两段式要表达的东西。
    fake_embed(monkeypatch, {
        "国家市场监督管理总局": _AXIS0,
        "市场监管总局": pair_vector(0.10, 1),
    })

    dry = run(resolve_entities())
    assert dry["dry_run"] is True and dry["aliases"] == 0
    assert dry["candidates"] == [{"canonical": "国家市场监督管理总局",
                                  "aliases": ["市场监管总局"],
                                  "similarity": 0.6,
                                  "reason": "substring"}]
    assert stats()["aliases"] == 0                     # 一条都没写

    written = run(resolve_entities(dry_run=False))
    assert written["aliases"] == 1 and written["skipped"] == 0
    assert stats()["aliases"] == 1
    assert stats()["merged_entities"] == 1
    assert resolve("市场监管总局") == "国家市场监督管理总局"

    reached = {tuple(path.nodes) for path in neighbors("市场监管总局", hops=1)}
    assert reached == {("国家市场监督管理总局", "价格法"),
                       ("国家市场监督管理总局", "反垄断法")}

    # 退回去：清掉别名，图回到合并之前的样子（原始边从没被搬动过）。
    assert clear_aliases() == 1
    assert neighbors("市场监管总局", hops=1) == []
