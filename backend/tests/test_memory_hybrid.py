"""双路检索：时间路 + 语义路，按名次 RRF 合并。

这一层最容易被"感觉对"骗过去：两路的分数口径不同（时间没有分，语义是余弦），
任何把它们塞进一个线性组合的写法都会在数据变多之后悄悄改变行为。所以用例钉的是
**合并规则本身**（名次、同分、截断），而不是某一次的具体排序结果。

不在这里验的：观测怎么切（`test_memory_observation.py`）、命题怎么修订
（`test_memory_proposition.py`）。
"""
from __future__ import annotations

import json
import math
from contextlib import closing

from app.knowledge import service as knowledge
from app.memory import observations, propositions, recall, store
from app.mindmaps import repository

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


# ---------------------------------------------------------------- 夹具

def fake_embedding(monkeypatch, vectors: dict[str, list[float]] | None = None):
    """让向量服务"配置好"，并把 `embed_texts` 换成查表的假实现。"""
    sent: list[str] = []
    table = dict(vectors or {})
    monkeypatch.setenv("MINDMAP_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("MINDMAP_EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")

    def embed(texts: list[str]) -> list[list[float] | None]:
        sent.extend(texts)
        return [normalize(table.get(text)) for text in texts]

    monkeypatch.setattr(store, "embed_texts", embed)
    monkeypatch.setattr(store.vector_cache, "get_vectors", lambda hashes, model: {})
    monkeypatch.setattr(store.vector_cache, "put_vectors", lambda items, model: 0)
    return sent


def normalize(vector: list[float] | None) -> list[float] | None:
    if not vector:
        return None
    norm = math.sqrt(sum(value * value for value in vector))
    return vector if norm <= 0 else [value / norm for value in vector]


def put_obs(obs_id: str, summary: str, *, at: str = "2026-11-20T00:00:00.000Z",
            kind: str = observations.OBSERVATION_RUN) -> str:
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            f"INSERT INTO {store.OBSERVATION_TABLE} (id, kind, source_key, at, summary, "
            f"payload, refs, stat, captured_at) "
            f"VALUES (?, ?, ?, ?, ?, '{{}}', '{{}}', 1, {NOW})",
            (obs_id, kind, f"test:{obs_id}", at, summary),
        )
    return obs_id


def put_proposal(item_id: str, text: str, *, confidence: int = 7, decay: int = 9) -> str:
    """造一条命题（item + 命题行）。向量由 `fake_embedding` 的表提供。"""
    from app.knowledge import vector_cache

    vector = normalize(fake_vector(text))
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            f"INSERT INTO {store.ITEM_TABLE} (id, text, kind, source, created_at, "
            f"text_hash, model, dim, vector) "
            f"VALUES (?, ?, ?, 'summary', {NOW}, ?, 'text-embedding-v3', ?, ?)",
            (item_id, text, propositions.KIND, vector_cache.text_hash(text),
             None if vector is None else len(vector),
             None if vector is None else vector_cache.pack(vector)),
        )
        conn.execute(
            f"INSERT INTO {store.PROPOSITION_TABLE} (item_id, reasoning, confidence, decay, "
            f"status, revision, revisions, created_at, revised_at) "
            f"VALUES (?, '', ?, ?, ?, 'created', 0, {NOW}, {NOW})",
            (item_id, confidence, decay, propositions.STATUS_ACTIVE),
        )
    return item_id


_VECTORS: dict[str, list[float]] = {}


def fake_vector(text: str) -> list[float] | None:
    return _VECTORS.get(text)


def seed_vectors(monkeypatch, vectors: dict[str, list[float]]) -> None:
    _VECTORS.clear()
    _VECTORS.update(vectors)
    fake_embedding(monkeypatch, vectors)


def prop_hit(item_id: str, *, decay: int = 9) -> propositions.PropHit:
    """构造一次语义路命中（不起库）。`score` 只影响输入顺序，不影响 RRF。"""
    proposal = propositions.Proposal(
        item_id=item_id, text=f"判断 {item_id}", reasoning="", confidence=7, decay=decay,
        status=propositions.STATUS_ACTIVE, revision=propositions.REVISION_CREATED,
        revisions=0, created_at="2026-11-20T00:00:00.000Z",
        revised_at="2026-11-20T00:00:00.000Z",
    )
    return propositions.PropHit(proposal=proposal, cosine=0.9, weight=1.0, score=0.9)


def obs(item_id: str, *, at: str = "2026-11-20T00:00:00.000Z") -> observations.Observation:
    return observations.Observation(
        id=item_id, kind=observations.OBSERVATION_RUN, source_key=f"k:{item_id}", at=at,
        summary=f"动作 {item_id}", payload={}, refs={}, stat=1,
    )


# ---------------------------------------------------------------- 常量

def test_rrf_k_is_the_same_constant_as_the_knowledge_layer():
    """两处 `RRF_K` 必须相等。

    不共用 import 是因为 `knowledge.service` 会拉起 BM25 / rerank / 图检索一整条链，
    而这里只要一个整数；代价是"两个常量可能漂移"，这条用例就是那个代价的对价。
    它们不是一个可以各自调的数——改了它就是换了一套排序偏好，两处必须一起改。
    """
    assert recall.RRF_K == knowledge.RRF_K == 60


def test_hybrid_top_matches_the_memory_top_k():
    """融合后返回 6 条，与 `store.TOP_K` 同值：注入那一段的预算是一起算的。"""
    assert recall.HYBRID_TOP == store.TOP_K
    assert recall.ROUTE_K > recall.HYBRID_TOP


# ---------------------------------------------------------------- fuse（纯函数）

def test_fuse_only_uses_ranks():
    """RRF 只吃名次：`1/(60+1)` 与 `1/(60+2)`。

    如果哪天有人把余弦乘进来，这两个数会变——这条断言就是那个改动的红灯。
    """
    hits = recall.fuse([obs("a"), obs("b")], [], top=5)
    assert [hit.id for hit in hits] == ["a", "b"]
    assert hits[0].score == round(1 / 61, 6)
    assert hits[1].score == round(1 / 62, 6)


def test_fuse_prefers_a_proposition_when_the_scores_tie():
    """同分时命题优先：一条命题是跨事件的归纳，一次观测是一次动作。

    只在分数**完全相等**时生效（第 1 名对第 1 名就是这种情形），所以它是个细节，
    不是一个隐含的排序规则。
    """
    hits = recall.fuse([obs("o1")], [prop_hit("p1")], top=5)
    assert [hit.id for hit in hits] == ["p1", "o1"]


def test_swapping_ranks_gives_the_same_score():
    """名次相同就分数相同，与"哪一路"无关——这是 RRF 的性质，也是"两路完全等价"
    的表达。用它钉住"没有哪一路被暗中加权"。"""
    a = recall.fuse([obs("x"), obs("y")], [prop_hit("m"), prop_hit("n")], top=5)
    scores = {hit.id: hit.score for hit in a}
    assert scores["x"] == scores["m"]      # 两路各自的第 1 名
    assert scores["y"] == scores["n"]      # 两路各自的第 2 名
    assert scores["x"] > scores["y"]


def test_fuse_records_which_routes_mentioned_it():
    """`routes` 留痕：回答"这条为什么进来"只需要看它被哪一路、第几名提到。"""
    hits = recall.fuse([obs("o1")], [prop_hit("p1")], top=5)
    by_id = {hit.id: hit for hit in hits}
    assert by_id["o1"].routes == ((recall.ROUTE_TIME, 1),)
    assert by_id["p1"].routes == ((recall.ROUTE_SEMANTIC, 1),)


def test_fuse_truncates_to_top():
    hits = recall.fuse([obs(f"o{i}") for i in range(5)],
                       [prop_hit(f"p{i}") for i in range(5)], top=3)
    assert len(hits) == 3


def test_fuse_keeps_the_real_kind_of_the_observation():
    """`kind` 是真实档位（run / edit / proposition），不是"哪一路"。

    用它回答"这条是事实还是判断"。曾经把时间路一律标成 `run`，于是 edit 观测在
    融合结果里看起来像一次运行——一个只在读返回值的人才会踩到的坑。
    """
    edit = observations.Observation(
        id="e1", kind=observations.OBSERVATION_EDIT, source_key="k", at="2026-11-20",
        summary="一次保存", payload={}, refs={}, stat=3)
    hits = recall.fuse([edit], [], top=5)
    assert hits[0].kind == observations.OBSERVATION_EDIT
    assert hits[0].is_proposition is False


# ---------------------------------------------------------------- hybrid（带库）

def test_hybrid_works_with_only_the_time_route(monkeypatch):
    """没配 embedding 时语义路是空的，时间路照常给结果。

    这不是"降级模式"，是双路的正常形态之一：没有向量服务时，"最近发生了什么"仍然是
    可用上下文。
    """
    monkeypatch.delenv("MINDMAP_EMBEDDING_API_KEY", raising=False)
    put_obs("o1", "最近的一次生成", at="2026-11-20T10:00:00.000Z")
    put_obs("o2", "更早的一次生成", at="2026-11-20T09:00:00.000Z")

    bundle = recall.hybrid("机器学习")

    assert bundle.time_available is True
    assert bundle.semantic_available is False
    assert [hit.id for hit in bundle.hits] == ["o1", "o2"]
    assert all(hit.is_proposition is False for hit in bundle.hits)


def test_hybrid_works_with_only_the_semantic_route(monkeypatch):
    """库里没有观测、只有命题时，语义路自己就能给结果。"""
    seed_vectors(monkeypatch, {"结构": [1.0, 0.0], "他要结构": [1.0, 0.0],
                              "他喜欢鱼骨图": [1.0, 0.0]})
    put_proposal("p1", "他要结构")
    put_proposal("p2", "他喜欢鱼骨图")

    bundle = recall.hybrid("结构")

    assert bundle.time_available is False
    assert bundle.semantic_available is True
    assert {hit.id for hit in bundle.hits} == {"p1", "p2"}
    assert all(hit.is_proposition for hit in bundle.hits)


def test_hybrid_merges_both_routes(monkeypatch):
    """两路都有时，融合结果里两边的条目都在，且命题排在前面（同分优先）。"""
    seed_vectors(monkeypatch, {"结构": [1.0, 0.0], "他要结构": [1.0, 0.0]})
    put_proposal("p1", "他要结构")
    put_obs("o1", "最近的一次生成")

    bundle = recall.hybrid("结构")

    assert bundle.time_available and bundle.semantic_available
    assert [hit.kind for hit in bundle.hits] == [propositions.KIND,
                                                 observations.OBSERVATION_RUN]


def test_hybrid_returns_nothing_on_an_empty_database(monkeypatch):
    seed_vectors(monkeypatch, {"结构": [1.0, 0.0]})
    bundle = recall.hybrid("结构")
    assert bundle.hits == []
    assert bundle.time_available is False
    assert bundle.semantic_available is False


def test_empty_query_still_uses_the_time_route(monkeypatch):
    """空 query 时时间路照常工作——"最近发生了什么"不依赖主题。"""
    seed_vectors(monkeypatch, {"结构": [1.0, 0.0]})
    put_obs("o1", "最近的一次生成")
    bundle = recall.hybrid("   ")
    assert [hit.id for hit in bundle.hits] == ["o1"]
    assert bundle.semantic_available is False


def test_hybrid_truncates_to_hybrid_top(monkeypatch):
    """融合池比最终条数大，但返回的仍然只有 6 条。

    池子更大的意义是"第 7、8 名还有机会被合并进来"，而不是把它们都注入进去。
    """
    monkeypatch.delenv("MINDMAP_EMBEDDING_API_KEY", raising=False)
    for index in range(8):
        put_obs(f"o{index}", f"动作 {index}", at=f"2026-11-20T0{index}:00:00.000Z")
    bundle = recall.hybrid("机器学习")
    assert len(bundle.hits) == recall.HYBRID_TOP


def test_semantic_route_orders_by_persistence(monkeypatch):
    """同样的余弦下，`decay=10`（持久偏好）排在 `decay=1`（短时上下文）前面。

    这条钉的是 `persistence()` 真的接到了排序上——否则 `decay` 就只是库里的一个
    装饰字段，而论文里它是"这条判断能用多久"的唯一表达。
    """
    seed_vectors(monkeypatch, {"判断": [1.0, 0.0], "判断甲": [1.0, 0.0],
                              "判断乙": [1.0, 0.0]})
    put_proposal("durable", "判断甲", decay=10)
    put_proposal("fleeting", "判断乙", decay=1)

    hits = propositions.search("判断")

    assert [hit.proposal.item_id for hit in hits] == ["durable", "fleeting"]
    assert hits[0].weight == 1.0
    assert hits[1].weight == 0.5


def test_semantic_route_skips_vectors_from_another_model(monkeypatch):
    """换了 embedding 模型之后旧向量不参与——跨空间点积会得到"看起来合理的垃圾"。

    与 `store.recall` 同一条约束；这条用例保证它在新模块里没有被忘掉。
    """
    from app.knowledge import vector_cache

    seed_vectors(monkeypatch, {"结构": [1.0, 0.0], "他要结构": [1.0, 0.0]})
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            f"INSERT INTO {store.ITEM_TABLE} (id, text, kind, source, created_at, "
            f"text_hash, model, dim, vector) VALUES ('p1', '他要结构', ?, 'summary', "
            f"{NOW}, ?, 'old-model', 2, ?)",
            (propositions.KIND, vector_cache.text_hash("他要结构"),
             vector_cache.pack(normalize([1.0, 0.0]))),
        )
        conn.execute(
            f"INSERT INTO {store.PROPOSITION_TABLE} (item_id, reasoning, confidence, decay, "
            f"status, revision, revisions, created_at, revised_at) "
            f"VALUES ('p1', '', 7, 9, 'active', 'created', 0, {NOW}, {NOW})")
    assert propositions.search("他要结构") == []


def test_superseded_propositions_do_not_take_slots(monkeypatch):
    """被替换的命题不在检索里——席位留给有效的那些。"""
    seed_vectors(monkeypatch, {"结构": [1.0, 0.0], "他要结构": [1.0, 0.0]})
    put_proposal("p1", "他要结构")
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            f"UPDATE {store.PROPOSITION_TABLE} SET status = 'superseded', "
            f"superseded_by = 'p2' WHERE item_id = 'p1'")
    assert propositions.search("他要结构") == []


def test_route_k_bounds_each_route(monkeypatch):
    seed_vectors(monkeypatch, {**{f"判断{i}": [1.0, 0.0] for i in range(6)},
                              "判断": [1.0, 0.0]})
    for index in range(6):
        put_proposal(f"p{index}", f"判断{index}")
    assert len(propositions.search("判断", top_k=2)) == 2


# ---------------------------------------------------------------- 注入文本

def test_prompt_for_separates_judgements_from_facts(monkeypatch):
    """两段的可信度不同，必须分开写：命题是推断（可能错），观测是事实。

    合成一段会让模型分不清"他这么做过"与"我推断他会这么做"。
    """
    seed_vectors(monkeypatch, {"结构": [1.0, 0.0], "他要结构": [1.0, 0.0]})
    put_proposal("p1", "他要结构")
    put_obs("o1", "最近的一次生成")

    text = recall.prompt_for(recall.hybrid("结构"))

    assert "判断" in text and "动作" in text
    assert text.index("判断") < text.index("动作")
    assert "他要结构" in text


def test_prompt_for_is_empty_without_hits(monkeypatch):
    """没有命中时返回空串，不返回"（暂无记忆）"。

    与 `store.prompt_for` 同一条理由：空标题会让模型以为"这个人的记忆是空的"是一种
    需要它处理的输入。
    """
    seed_vectors(monkeypatch, {"结构": [1.0, 0.0]})
    assert recall.prompt_for(recall.hybrid("结构")) == ""


def test_hybrid_bundle_keeps_the_raw_routes(monkeypatch):
    """两路的原始命中都留在 bundle 里：回答"为什么没有它"要能查。

    只返回融合结果的话，"语义路空是因为没配向量还是因为都不相关"这个问题无法回答，
    而两种情况要改的东西完全不同。
    """
    seed_vectors(monkeypatch, {"结构": [1.0, 0.0], "他要结构": [1.0, 0.0]})
    put_proposal("p1", "他要结构")
    put_obs("o1", "最近的一次生成")
    bundle = recall.hybrid("结构")
    assert [hit.proposal.item_id for hit in bundle.semantic_route] == ["p1"]
    assert [item.id for item in bundle.time_route] == ["o1"]
    assert json.loads(json.dumps(bundle.query)) == "结构"
