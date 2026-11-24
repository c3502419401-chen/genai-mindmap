"""命题：三态修订、consolidation 的观测集合并、以及"出处是真的"。

这一层每个错误都是**不可恢复**的：误合并把两个判断压成一条（旧判断下线、检索里
再也看不到），编造出处让一条判断看起来有证据。所以这里的用例围绕两件事写：
**三态各自的判据**，以及**边表在修订之后还是不是事实**。

不在这里验的：观测怎么切（`test_memory_observation.py`）、双路怎么合并
（`test_memory_hybrid.py`）、以及超参的取值理由（那些在模块 docstring 里）。
"""
from __future__ import annotations

import json
import math
from contextlib import closing

import pytest

from app.memory import observations, propositions, store, summarize
from app.mindmaps import repository

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


# ---------------------------------------------------------------- 夹具

def fake_embedding(monkeypatch, vectors: dict[str, list[float]] | None = None):
    """让向量服务"配置好"，并把 `embed_texts` 换成一个查表的假实现。

    与 `test_memory_store.py` 里那份同一种做法（那份是记忆层的通用夹具）。这里额外
    返回 `sent`：**"这次修订有没有重算向量"只能靠数它**。
    """
    sent: list[str] = []
    table = dict(vectors or {})
    monkeypatch.setenv("MINDMAP_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("MINDMAP_EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")

    def embed(texts: list[str]) -> list[list[float] | None]:
        sent.extend(texts)
        return [_normalize(table.get(text)) for text in texts]

    monkeypatch.setattr(store, "embed_texts", embed)
    monkeypatch.setattr(store.vector_cache, "get_vectors", lambda hashes, model: {})
    monkeypatch.setattr(store.vector_cache, "put_vectors", lambda items, model: 0)
    return sent


def _normalize(vector: list[float] | None) -> list[float] | None:
    if not vector:
        return None
    norm = math.sqrt(sum(value * value for value in vector))
    return vector if norm <= 0 else [value / norm for value in vector]


def fake_model(monkeypatch, payload: object, *, raises: Exception | None = None):
    """替换模型出口。`_config` 也一起替换：那是"模型已配置"的唯一判定。"""
    sent: list[tuple[str, str]] = []

    async def chat(system: str, user: str, *, max_tokens=None, **kwargs):  # noqa: ANN001
        sent.append((system, user))
        if raises is not None:
            raise raises
        return payload

    monkeypatch.setattr(propositions, "chat_async", chat)
    monkeypatch.setattr(propositions, "_config", lambda: ("http://x", "k", "m"))
    return sent


def put_obs(obs_id: str, *, kind: str = "run",
            summary: str = "[create]「机器学习」→ done", alive: bool = True) -> str:
    """直插一条观测。`alive=False` 时 `consumed_at` 已经是现在（模拟已归纳过）。"""
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            f"INSERT INTO {store.OBSERVATION_TABLE} "
            "(id, kind, source_key, at, summary, payload, refs, stat, consumed_at, "
            f"captured_at) VALUES (?, ?, ?, ?, ?, '{{}}', ?, 1, "
            f"{'NULL' if alive else NOW}, {NOW})",
            (obs_id, kind, f"test:{obs_id}", "2026-11-20T00:00:00.000Z", summary,
             json.dumps({"event_ids": [1]}, ensure_ascii=False)),
        )
    return obs_id


def seed_proposal(text: str, *, obs_ids: list[str], confidence: int = 7, decay: int = 8) -> str:
    """造一条已经存在的命题（item + 命题行 + 边），返回 item_id。"""
    added = store.add(text, propositions.KIND, source=store.SOURCE_SUMMARY)
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            f"INSERT INTO {store.PROPOSITION_TABLE} (item_id, reasoning, confidence, decay, "
            f"status, revision, revisions, created_at, revised_at) "
            f"VALUES (?, '旧依据', ?, ?, ?, 'created', 0, {NOW}, {NOW})",
            (added.item.id, confidence, decay, propositions.STATUS_ACTIVE),
        )
        for obs_id in obs_ids:
            conn.execute(
                f"INSERT INTO {store.EDGE_TABLE} (item_id, observation_id, added_at) "
                f"VALUES (?, ?, {NOW})", (added.item.id, obs_id),
            )
    return added.item.id


def draft(text: str, *obs_ids: str, confidence: int = 7, decay: int = 9) -> propositions.Draft:
    return propositions.Draft(proposition=text, reasoning="新依据", confidence=confidence,
                              decay=decay, observation_ids=tuple(obs_ids))


# ---------------------------------------------------------------- 结构与端点

def test_persistence_is_linear_and_bounded():
    """decay 1–10 → 0.5–1.0。端点必须是这两个数：它们是"短时上下文"与"持久偏好"
    这两种语义的定量表述，中间值只是线性插值。"""
    assert propositions.persistence(1) == 0.5
    assert propositions.persistence(10) == 1.0
    assert propositions.persistence(5) == pytest.approx(0.7222, abs=1e-3)
    # 越界按端点处理，不越出 0.5–1.0：一个坏数字不该让某条命题获得额外加成。
    assert propositions.persistence(0) == 0.5
    assert propositions.persistence(99) == 1.0


def test_proposition_kind_is_not_a_hand_writable_kind():
    """命题档位**不在** `KINDS` 里，所以 `POST /memory` 写不出命题。

    手工写一条命题却没有 `observation_ids`，正是这套设计要禁止的形态（"出处是
    用户习惯"）。
    """
    assert propositions.KIND not in store.KINDS
    assert propositions.KIND in store.WRITABLE_KINDS


def test_store_add_accepts_a_proposition(monkeypatch):
    fake_embedding(monkeypatch)
    added = store.add("他倾向先要骨架", propositions.KIND)
    assert added.created is True
    assert added.item.kind == propositions.KIND


# ---------------------------------------------------------------- IDENTICAL

def test_identical_only_adds_observations(monkeypatch):
    """正文一字不差 → 只把新观测挂上去，**不重写正文、不重算向量**。

    这条是论文"IDENTICAL → 只把新观测加进它的 observation_ids"的直译。重算向量
    的代价是钱；重写正文的代价更大——"这条判断是什么时候定下来的"会变得没有答案。
    """
    sent = fake_embedding(monkeypatch, {"他要结构": [1.0, 0.0]})
    put_obs("obs-a")
    put_obs("obs-b")
    item_id = seed_proposal("他要结构", obs_ids=["obs-a"])
    before = len(sent)

    revision = propositions.revise(draft("他要结构", "obs-b"), allowed={})

    assert revision.action == propositions.ACTION_IDENTICAL
    assert revision.attached == 1
    assert set(propositions.observation_ids(item_id)) == {"obs-a", "obs-b"}
    # 没有新的 embedding 调用：只在库里加了一条边。
    assert len(sent) == before
    assert propositions.prop(item_id).text == "他要结构"


def test_identical_bumps_the_revision_counter(monkeypatch):
    """被反复归纳出来的同一条判断，`revisions` 要涨。它回答"这条判断被确认过几次"。"""
    fake_embedding(monkeypatch, {"他要结构": [1.0, 0.0]})
    put_obs("obs-a")
    put_obs("obs-b")
    item_id = seed_proposal("他要结构", obs_ids=["obs-a"])
    assert propositions.prop(item_id).revisions == 0
    propositions.revise(draft("他要结构", "obs-b"), allowed={})
    assert propositions.prop(item_id).revisions == 1


def test_identical_edge_is_idempotent(monkeypatch):
    """同一条观测挂两次只有一条边。否则"支撑数"可以靠重放灌水。"""
    fake_embedding(monkeypatch, {"他要结构": [1.0, 0.0]})
    put_obs("obs-a")
    item_id = seed_proposal("他要结构", obs_ids=["obs-a"])
    revision = propositions.revise(draft("他要结构", "obs-a"), allowed={})
    assert revision.attached == 0
    assert len(propositions.observation_ids(item_id)) == 1


def test_identical_skips_a_text_the_user_wrote_explicitly(monkeypatch):
    """与用户**显式写下**的偏好撞文本时，不建命题也不改那条。

    显式声明不需要出处也是真的；把它升级成命题会让人分不清"我说的"与"系统归纳的"。
    """
    fake_embedding(monkeypatch, {"标题去掉人名": [1.0, 0.0]})
    put_obs("obs-a")
    put_obs("obs-b")
    store.add("标题去掉人名", "preference", source=store.SOURCE_USER_EXPLICIT)

    revision = propositions.revise(draft("标题去掉人名", "obs-a", "obs-b"), allowed={})

    assert revision.skipped == "explicit"
    assert propositions.count() == 0
    assert store.items(owner="")[0].kind == "preference"


def test_identical_promotes_a_summary_item(monkeypatch):
    """总结阶段产出的同文本条目**可以**升级成命题：现在它有出处了。

    正文与向量都不动，只是档位变了——这条判断的证据等级提高了一层。
    """
    fake_embedding(monkeypatch, {"他习惯中文术语": [1.0, 0.0]})
    put_obs("obs-a")
    put_obs("obs-b")
    added = store.add("他习惯中文术语", "fact", source=store.SOURCE_SUMMARY)
    propositions.revise(draft("他习惯中文术语", "obs-a", "obs-b"), allowed={})
    assert store.items(owner="")[0].kind == propositions.KIND
    assert propositions.prop(added.item.id) is not None


# ---------------------------------------------------------------- UNRELATED

def test_unrelated_creates_a_new_proposition(monkeypatch):
    fake_embedding(monkeypatch, {"他要结构": [1.0, 0.0], "他喜欢英文缩写": [0.0, 1.0]})
    put_obs("obs-a")
    put_obs("obs-b")
    seed_proposal("他要结构", obs_ids=["obs-a"])

    revision = propositions.revise(draft("他喜欢英文缩写", "obs-a", "obs-b"), allowed={})

    assert revision.action == propositions.ACTION_UNRELATED
    assert len(revision.item_ids) == 1
    created = propositions.prop(revision.item_ids[0])
    assert created.text == "他喜欢英文缩写"
    assert created.confidence == 7 and created.decay == 9
    assert set(created.observation_ids) == {"obs-a", "obs-b"}


def test_a_new_proposition_needs_two_observations(monkeypatch):
    """只有一条观测支撑的"判断"是复述，不是归纳——丢弃。

    注意这条**只对新建生效**：挂到已有命题上的一条观测是合法的（那条命题本来就有
    别的出处）。
    """
    fake_embedding(monkeypatch, {"他喜欢英文缩写": [0.0, 1.0]})
    put_obs("obs-a")
    revision = propositions.revise(draft("他喜欢英文缩写", "obs-a"), allowed={})
    assert revision.skipped == "support"
    assert propositions.count() == 0


def test_a_new_proposition_survives_without_embeddings(monkeypatch):
    """没配向量服务时命题照样落库（只是检索不到）。

    与 `store.add` 同一条取舍：失败的粒度是"这一条检索不到"，不是"整批一条都没写"。
    """
    monkeypatch.delenv("MINDMAP_EMBEDDING_API_KEY", raising=False)
    put_obs("obs-a")
    put_obs("obs-b")
    revision = propositions.revise(draft("他喜欢英文缩写", "obs-a", "obs-b"), allowed={})
    assert revision.action == propositions.ACTION_UNRELATED
    assert propositions.prop(revision.item_ids[0]).support == 2


# ---------------------------------------------------------------- SIMILAR / consolidation

def test_similar_triggers_consolidation_and_inherits_the_support_set(monkeypatch):
    """**这条是整层的核心**：SIMILAR 走 consolidation，新命题继承旧命题的全部观测。

    `replaces` 由模型给，但**观测集不由模型给**——代码把旧边并进来。否则一次改写
    就会让判断的置信度建立在更少的证据上，而它看起来与原来一样权威。
    """
    fake_embedding(monkeypatch, {
        "他倾向先要骨架": [1.0, 0.0],
        "他通常先出结构再补细节": [0.9, 0.435],
        "他先要结构，细节后补（合并）": [0.95, 0.31],
    })
    put_obs("obs-old-1")
    put_obs("obs-old-2")
    put_obs("obs-new")
    old_id = seed_proposal("他倾向先要骨架", obs_ids=["obs-old-1", "obs-old-2"])
    fake_model(monkeypatch, {"propositions": [{
        "proposition": "他先要结构，细节后补（合并）",
        "reasoning": "两次都是先要结构",
        "confidence": 8, "decay": 9,
        "observation_ids": ["o1"], "replaces": ["p1"],
    }]})

    revision = propositions.revise(
        draft("他通常先出结构再补细节", "obs-new"), allowed={"o1": "obs-new"})

    assert revision.action == propositions.ACTION_SIMILAR
    assert revision.superseded == (old_id,)
    new_id = revision.item_ids[0]
    new = propositions.prop(new_id)
    assert new.text == "他先要结构，细节后补（合并）"
    # 旧的 2 条 + 新的 1 条，一条都不少。
    assert set(new.observation_ids) == {"obs-old-1", "obs-old-2", "obs-new"}
    assert new.revision == propositions.REVISION_CONSOLIDATED


def test_superseded_propositions_stay_in_the_database_but_leave_the_list(monkeypatch):
    """被替换的命题**还在**（status=superseded + superseded_by），只是不进检索。

    论文 Appendix A 的第一条 limitation 是"用户想看到、改正、忘掉系统学到的东西"。
    原地 UPDATE 之后旧判断消失，那条 limitation 就永远解不掉。
    """
    fake_embedding(monkeypatch, {
        "他倾向先要骨架": [1.0, 0.0],
        "他通常先出结构再补细节": [0.9, 0.435],
        "合并后的判断": [0.95, 0.31],
    })
    put_obs("obs-old")
    put_obs("obs-new")
    old_id = seed_proposal("他倾向先要骨架", obs_ids=["obs-old"])
    fake_model(monkeypatch, {"propositions": [{
        "proposition": "合并后的判断", "reasoning": "", "confidence": 8, "decay": 9,
        "observation_ids": ["o1"], "replaces": ["p1"],
    }]})
    revision = propositions.revise(
        draft("他通常先出结构再补细节", "obs-new"), allowed={"o1": "obs-new"})

    assert old_id not in [item.item_id for item in propositions.proposals()]
    everything = propositions.proposals(status=None)
    superseded = [item for item in everything if item.item_id == old_id][0]
    assert superseded.status == propositions.STATUS_SUPERSEDED
    assert superseded.superseded_by == revision.item_ids[0]
    assert superseded.text == "他倾向先要骨架"


def test_consolidation_degrades_to_attaching_instead_of_losing_evidence(monkeypatch):
    """模型挂掉时把观测挂到最近邻，**不是**退回新建。

    第一版实现是退回新建，被这条用例打回来了：SIMILAR 场景下新观测常常只有一条
    （其余出处已经在旧命题上），而新建要求两条——退回新建会把它丢掉，于是这条观测
    在 `mark_consumed` 之后再也看不到。降级必须保证据。
    """
    fake_embedding(monkeypatch, {
        "他倾向先要骨架": [1.0, 0.0],
        "他通常先出结构再补细节": [0.9, 0.435],
    })
    put_obs("obs-old")
    put_obs("obs-new")
    old_id = seed_proposal("他倾向先要骨架", obs_ids=["obs-old"])
    fake_model(monkeypatch, {}, raises=RuntimeError("上游 500"))

    revision = propositions.revise(
        draft("他通常先出结构再补细节", "obs-new"), allowed={"o1": "obs-new"})

    assert revision.action == propositions.ACTION_SIMILAR
    assert revision.degraded is True
    # 证据挂上去了，正文一个字都没改。
    assert set(propositions.observation_ids(old_id)) == {"obs-old", "obs-new"}
    assert propositions.prop(old_id).text == "他倾向先要骨架"
    assert propositions.count() == 1


def test_consolidation_degrades_when_the_output_is_malformed(monkeypatch):
    fake_embedding(monkeypatch, {
        "他倾向先要骨架": [1.0, 0.0],
        "他通常先出结构再补细节": [0.9, 0.435],
    })
    put_obs("obs-old")
    put_obs("obs-new")
    old_id = seed_proposal("他倾向先要骨架", obs_ids=["obs-old"])
    fake_model(monkeypatch, "这不是 JSON")

    revision = propositions.revise(
        draft("他通常先出结构再补细节", "obs-new"), allowed={"o1": "obs-new"})

    assert revision.action == propositions.ACTION_SIMILAR
    assert revision.degraded is True
    assert "obs-new" in propositions.observation_ids(old_id)


def test_consolidation_cannot_replace_outside_the_candidate_set(monkeypatch):
    """`replaces` 里出现候选集之外的编号时，那一个被丢掉。

    模型偶尔会编一个 p9。丢掉那个编号最坏的结果是少 supersede 一条（多一条近义
    命题，可见可删）；接受它则会让一条**没有参与的**命题被下线。
    """
    fake_embedding(monkeypatch, {
        "他倾向先要骨架": [1.0, 0.0],
        "他通常先出结构再补细节": [0.9, 0.435],
        "重写后的判断": [0.95, 0.31],
    })
    put_obs("obs-old")
    put_obs("obs-new")
    old_id = seed_proposal("他倾向先要骨架", obs_ids=["obs-old"])
    fake_model(monkeypatch, {"propositions": [{
        "proposition": "重写后的判断", "reasoning": "", "confidence": 7, "decay": 8,
        "observation_ids": ["o1"], "replaces": ["p9"],
    }]})

    revision = propositions.revise(
        draft("他通常先出结构再补细节", "obs-new"), allowed={"o1": "obs-new"})

    assert revision.superseded == ()
    # 旧命题仍然有效：它没有被任何合法的 replaces 点名。
    assert old_id in [item.item_id for item in propositions.proposals()]


def test_consolidation_split_keeps_every_old_edge(monkeypatch):
    """拆成两条时，**两条都继承旧边**（已知的粗粒度，见模块 docstring）。

    取舍方向：支撑集偏大可以审阅、可以删边；丢证据不可恢复。
    """
    fake_embedding(monkeypatch, {
        "他先要结构再补细节而且讲究措辞": [1.0, 0.0],
        "他通常先出结构": [0.9, 0.435],
        "他对措辞很挑": [0.9, 0.435],
        "他先要结构": [0.95, 0.31],
    })
    put_obs("obs-old")
    put_obs("obs-new")
    seed_proposal("他先要结构再补细节而且讲究措辞", obs_ids=["obs-old"])
    fake_model(monkeypatch, {"propositions": [
        {"proposition": "他先要结构", "reasoning": "", "confidence": 7, "decay": 8,
         "observation_ids": ["o1"], "replaces": ["p1"]},
        {"proposition": "他对措辞很挑", "reasoning": "", "confidence": 6, "decay": 7,
         "observation_ids": [], "replaces": ["p1"]},
    ]})

    revision = propositions.revise(
        draft("他通常先出结构", "obs-new"), allowed={"o1": "obs-new"})

    assert len(revision.item_ids) == 2
    for item_id in revision.item_ids:
        assert "obs-old" in propositions.observation_ids(item_id)


# ---------------------------------------------------------------- 解析

def test_out_of_range_reference_is_dropped_not_the_whole_draft(monkeypatch):
    """越界编号逐个丢，不整条丢：一条命题里写错一个编号，另外几个仍是真的。"""
    drafts, dropped = propositions.parse_drafts(
        {"propositions": [{"proposition": "他要结构", "confidence": 7, "decay": 8,
                           "observation_ids": ["o1", "o99", "o2"]}]},
        {"o1": "obs-a", "o2": "obs-b"},
    )
    assert dropped == 0
    assert drafts[0].observation_ids == ("obs-a", "obs-b")


def test_numbers_are_clamped_not_rejected():
    """`confidence` / `decay` 越界夹住；不是数字时按中间值。

    正文对不对比它自称几成把握更重要——为一个数字丢掉一条判断，代价方向错了。
    """
    drafts, _ = propositions.parse_drafts(
        {"propositions": [
            {"proposition": "A", "confidence": 99, "decay": -3, "observation_ids": ["o1"]},
            {"proposition": "B", "confidence": "很高", "decay": None,
             "observation_ids": ["o1"]},
        ]},
        {"o1": "obs-a"},
    )
    assert (drafts[0].confidence, drafts[0].decay) == (10, 1)
    assert (drafts[1].confidence, drafts[1].decay) == (5, 5)


def test_bare_integers_are_accepted_as_references():
    """模型偶尔会写 `[1, 3]` 而不是 `["o1", "o3"]`。两种都认。"""
    drafts, _ = propositions.parse_drafts(
        {"propositions": [{"proposition": "A", "observation_ids": [2, 1]}]},
        {"o1": "obs-a", "o2": "obs-b"},
    )
    assert drafts[0].observation_ids == ("obs-b", "obs-a")


def test_malformed_payload_is_empty_not_an_exception():
    assert propositions.parse_drafts({"nope": []}, {}) == ([], 0)
    assert propositions.parse_drafts(None, {}) == ([], 0)


# ---------------------------------------------------------------- 归纳入口

def test_induce_writes_propositions_and_consumes_observations(monkeypatch):
    """端到端：两条观测 → 一条命题 → 观测被标记已消费。"""
    fake_embedding(monkeypatch, {"他反复用鱼骨图处理问题分解": [1.0, 0.0]})
    put_obs("obs-1", summary="[create]「碳中和」图型 fishbone")
    put_obs("obs-2", summary="[create]「机器学习」图型 fishbone")
    fake_model(monkeypatch, {"propositions": [{
        "proposition": "他反复用鱼骨图处理问题分解",
        "reasoning": "两次都是 fishbone",
        "confidence": 6, "decay": 9,
        "observation_ids": ["o1", "o2"],
    }]})

    outcome = propositions.induce()

    assert outcome.status == "done"
    assert outcome.created == 1
    assert [item.item_id for item in propositions.proposals()]
    assert propositions.proposals()[0].observation_ids == ("obs-1", "obs-2")
    assert observations.unconsumed(10) == []


def test_induce_marks_observations_consumed_even_with_zero_propositions(monkeypatch):
    """0 条命题也是结论——观测照样标成已消费。

    不标的后果很具体：同一批最旧的观测会永远占住归纳窗口的前 40 条，新观测永远
    进不来，而表面上一切正常。
    """
    fake_embedding(monkeypatch)
    put_obs("obs-1")
    put_obs("obs-2")
    fake_model(monkeypatch, {"propositions": []})

    outcome = propositions.induce()

    assert outcome.status == "empty"
    assert observations.unconsumed(10) == []


def test_induce_does_not_call_the_model_without_enough_observations(monkeypatch):
    """不到两条观测时不发请求：一条观测跨不了事件，归纳出来只会是复述。"""
    fake_embedding(monkeypatch)
    put_obs("obs-1")
    sent = fake_model(monkeypatch, {"propositions": []})

    outcome = propositions.induce()

    assert outcome.status == "empty"
    assert sent == []
    assert len(observations.unconsumed(10)) == 1


def test_induce_skips_without_consuming_when_the_model_is_not_configured(monkeypatch):
    """模型未配置时**不消耗**观测：那是预期状态，不是"这批观测没用"。"""
    fake_embedding(monkeypatch)
    put_obs("obs-1")
    put_obs("obs-2")

    def boom():
        from app.generation.service import GenerationError
        raise GenerationError("没配 key")

    monkeypatch.setattr(propositions, "_config", boom)
    outcome = propositions.induce()

    assert outcome.status == "skipped"
    assert len(observations.unconsumed(10)) == 2


def test_induce_leaves_observations_for_retry_when_the_model_fails(monkeypatch):
    """调用失败时观测留着下次再试。代价是一次调用，不重试的代价是证据永久丢失。"""
    fake_embedding(monkeypatch)
    put_obs("obs-1")
    put_obs("obs-2")
    fake_model(monkeypatch, {}, raises=RuntimeError("超时"))

    outcome = propositions.induce()

    assert outcome.status == "failed"
    assert len(observations.unconsumed(10)) == 2


# ---------------------------------------------------------------- 回指（核心约束）

def test_every_proposition_traces_back_to_real_events(monkeypatch):
    """**这套设计的硬约束**：给定一条命题，能列出它引用的每一个原始事件行。

    链是 `命题 item_id → 边 → 观测 id → 观测的 refs → runs.run_id / edit_events.id`。
    这条用例走完整条链，最后在两张原始表里数行——数不到就是"出处是编的"。
    """
    fake_embedding(monkeypatch, {"他会把长标题压短": [1.0, 0.0]})
    # 造两条真的原始事件：一次 run、一次保存批次。
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO runs (run_id, status, request, created_at, updated_at) "
            "VALUES ('run-x', 'done', '{\"goal\": \"机器学习\", \"intent\": \"create\"}', "
            f"{NOW}, {NOW})")
    at = "2026-11-20T12:00:00.000Z"
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO edit_events (map_id, actor, kind, node_id, field, before, after, "
            "at, recorded_at) VALUES ('map-x', 'user', 'update', 'n-1', 'label', "
            f"'一个很长的标题', '短标题', '{at}', '{at}')")
    observations.capture()
    assert observations.count() == 2

    ids = [item.id for item in observations.unconsumed(10)]
    fake_model(monkeypatch, {"propositions": [{
        "proposition": "他会把长标题压短", "reasoning": "两次记录",
        "confidence": 6, "decay": 9,
        "observation_ids": ["o1", "o2"],
    }]})
    propositions.induce(run_id="run-x")

    created = propositions.proposals()[0]
    assert set(created.observation_ids) == set(ids)
    refs = observations.resolve_refs(created.observation_ids)
    assert refs["missing"] == []
    assert refs["run_ids"] == ["run-x"]
    with closing(repository.connect()) as conn:
        events = conn.execute(
            f"SELECT COUNT(*) FROM edit_events WHERE id IN "
            f"({','.join('?' * len(refs['event_ids']))})", refs["event_ids"]).fetchone()[0]
        runs = conn.execute(
            f"SELECT COUNT(*) FROM runs WHERE run_id IN "
            f"({','.join('?' * len(refs['run_ids']))})", refs["run_ids"]).fetchone()[0]
    assert events == 1
    assert runs == 1


def test_prune_orphans_removes_a_proposition_without_its_row(monkeypatch):
    """`store.add` 成功、命题行没写进去时留下的半截条目要能清掉。

    它不在检索里（语义路从命题表 JOIN），但在记忆列表里看起来像一条命题，而它没有
    任何依据。
    """
    fake_embedding(monkeypatch)
    added = store.add("没有依据的命题", propositions.KIND)
    assert propositions.prop(added.item.id) is None
    assert propositions.prune_orphans() >= 1
    assert propositions.prop(added.item.id) is None
    assert all(item.id != added.item.id for item in store.items(owner=""))


# ---------------------------------------------------------------- 事件片的接线

def test_induce_is_off_only_when_explicitly_disabled(monkeypatch):
    """开关只认显式的 0/false/no/off。拼错一个值不该静默关掉一整条功能。"""
    monkeypatch.delenv(summarize.INDUCE_ENV, raising=False)
    assert summarize.induce_enabled() is True
    monkeypatch.setenv(summarize.INDUCE_ENV, "0")
    assert summarize.induce_enabled() is False
    monkeypatch.setenv(summarize.INDUCE_ENV, "of")
    assert summarize.induce_enabled() is True


def test_induce_propositions_returns_empty_when_disabled(monkeypatch):
    monkeypatch.setenv(summarize.INDUCE_ENV, "0")
    fake_embedding(monkeypatch)
    put_obs("obs-1")
    put_obs("obs-2")
    assert summarize.induce_propositions() == []
    assert len(observations.unconsumed(10)) == 2


def test_induce_propositions_captures_before_inducing(monkeypatch):
    """它的第一步是抽观测：`runs` / `edit_events` 里有东西就该先进观测表。

    这条钉的是"事件片"的接线本身——没有它，观测层永远不会被真实数据填充，而所有
    上层（命题、双路检索）在空表上都能"通过"。
    """
    fake_embedding(monkeypatch, {"他要结构": [1.0, 0.0]})
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO runs (run_id, status, request, created_at, updated_at) "
            "VALUES ('run-1', 'done', '{\"goal\": \"机器学习\", \"intent\": \"create\"}', "
            f"{NOW}, {NOW})")
        conn.execute(
            "INSERT INTO runs (run_id, status, request, created_at, updated_at) "
            "VALUES ('run-2', 'done', '{\"goal\": \"机器学习\", \"intent\": \"edit\"}', "
            f"{NOW}, {NOW})")
    fake_model(monkeypatch, {"propositions": []})

    summarize.induce_propositions()

    assert observations.count() == 2
    assert observations.unconsumed(10) == []
