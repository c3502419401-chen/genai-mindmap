"""按主题检索 + 时间衰减。

这一层只有一条核心不变量：**衰减只影响排序，从不删除**。它的实现方式是把门槛
（`MIN_COSINE`）判在**余弦**上、把衰减只用于排序——两个数乘在一起看起来是一回事，
判错了才发现"旧记忆迟早被时间删掉"。

所以大部分用例都在证明同一件事的两面：越旧越靠后（排序生效），以及最旧的那条
仍然在结果里（没有被删）。
"""
from __future__ import annotations

import math
from contextlib import closing
from datetime import timedelta

import pytest

from app.knowledge import vector_cache
from app.memory import store
from app.mindmaps import repository


# ---------------------------------------------------------------- 夹具

def fake_embedding(monkeypatch, vectors: dict[str, list[float]] | None = None):
    """让向量服务"配置好"，并把 `embed_texts` 换成一个查表的假实现。

    返回被送去算向量的文本列表——**"一次生成检索了几次"就是数它的长度**。
    缓存两侧一起换掉：不换的话"命中缓存"与"真的调了一次 embedding"在计数上
    没有区别，而这次改动最硬的一条约束正是"一次生成只检索一次"。
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


def unit(cosine: float, axis: int = 1) -> list[float]:
    """与"基准向量"（第 0 维）余弦恰好是 `cosine` 的单位向量。

    用余弦直接造向量，而不是造两段真文本再去算——这样"衰减有没有改变排序"这件事
    与 embedding 模型无关，用例钉住的是我们自己的算分公式。
    """
    values = [0.0, 0.0, 0.0, 0.0]
    values[0] = cosine
    values[axis] = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return values


BASE = [1.0, 0.0, 0.0, 0.0]


def stamp(days_ago: float = 0.0) -> str:
    moment = store.utcnow() - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def put(
    text: str,
    vector: list[float] | None,
    *,
    created_days_ago: float = 0.0,
    formed_at: str | None = None,
    kind: str = "preference",
    source: str = "summary",
) -> str:
    """直接写一行，**绕开 `add()`**：要精确控制 `created_at`（衰减测试的输入）。"""
    item_id = f"id-{abs(hash((text, created_days_ago))) % 10**10}"
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            f"INSERT INTO {store.ITEM_TABLE} "
            "(id, text, kind, source, created_at, formed_at, text_hash, model, dim, vector) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, text, kind, source, stamp(created_days_ago), formed_at,
             vector_cache.text_hash(text), "text-embedding-v3",
             None if vector is None else len(vector),
             None if vector is None else vector_cache.pack(vector)),
        )
    return item_id


# ---------------------------------------------------------------- 衰减公式

def test_decay_halves_every_half_life():
    """`0.5 ** (age / 30)` 的三个点：现在 1.0、30 天 0.5、90 天 0.125。"""
    assert store.decay(0) == 1.0
    assert store.decay(store.HALF_LIFE_DAYS) == pytest.approx(0.5)
    assert store.decay(store.HALF_LIFE_DAYS * 3) == pytest.approx(0.125)
    assert store.decay(365) == pytest.approx(0.5 ** (365 / 30), rel=1e-9)


def test_half_life_is_thirty_days():
    """这个数是设计决定：改它要重新读 `HALF_LIFE_DAYS` 那段五条依据。"""
    assert store.HALF_LIFE_DAYS == 30.0


def test_decay_treats_unknown_and_future_as_now():
    """读不出来的时间给 1.0（"没有证据"），未来的时间也钳到 1.0。

    给 0 的后果是坏数据被当成"最旧"静默沉底；给 >1 的后果是坏数据被额外加成。
    """
    assert store.decay(None) == 1.0
    assert store.decay(-5) == 1.0


def test_age_days_is_never_negative():
    """未来时间戳的年龄钳到 0，不返回负数。"""
    future = (store.utcnow() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    assert store.age_days(future) == 0.0


def test_age_days_uses_the_given_now():
    """`now` 是参数不是全局：用例才能钉住一个具体的天数而不依赖跑测试的时刻。"""
    moment = store.utcnow()
    ten_days_ago = (moment - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    assert store.age_days(ten_days_ago, now=moment) == pytest.approx(10, abs=0.01)


# ---------------------------------------------------------------- 门槛

def test_recall_without_embedding_returns_nothing(monkeypatch):
    """向量服务不可用 → 空列表，不抛。记忆检索失败不该让一次生成变错。"""
    monkeypatch.delenv("MINDMAP_EMBEDDING_API_KEY", raising=False)
    put("标题去掉人名", BASE)
    assert store.recall("标题", owner="") == []


def test_recall_on_empty_table_returns_nothing(monkeypatch):
    fake_embedding(monkeypatch, {"标题": BASE})
    assert store.recall("标题", owner="") == []


def test_recall_skips_rows_under_the_cosine_floor(monkeypatch):
    """余弦低于 `MIN_COSINE` 的不进候选——**与年龄无关**。

    这是"门槛判余弦"的直接证据：一条刚写进来的记忆余弦只有 0.30，它被挡在外面。
    如果门槛判 score，那么"新"本身就会给它加成（decay=1.0），过线与否就变成了
    "有多新"的问题，而不是"有多相关"。
    """
    fake_embedding(monkeypatch, {"查询": BASE})
    put("刚写但不太相关", unit(store.MIN_COSINE - 0.05))
    put("刚写且相关", unit(0.9))
    hits = store.recall("查询", owner="")
    assert [hit.text for hit in hits] == ["刚写且相关"]


def test_recall_respects_top_k(monkeypatch):
    fake_embedding(monkeypatch, {"查询": BASE})
    for index in range(10):
        put(f"条目 {index}", unit(0.9))
    assert len(store.recall("查询", top_k=3, owner="")) == 3
    assert len(store.recall("查询", owner="")) == min(store.TOP_K, 10)


def test_top_k_is_six():
    """6 = 6 × 100 字上限 ≈ 400 token，与 `note.*` 的 500 字预算同量级。"""
    assert store.TOP_K == 6


# ---------------------------------------------------------------- 衰减只排序

def test_decay_only_reorders_and_never_drops(monkeypatch):
    """**核心不变量**：一条 300 天前的记忆仍然能被检索到，只是排在后面。

    300 天 → decay = 0.5**10 ≈ 0.00098。它的 score 只剩千分之一，但它在 `hits` 里。
    判 score 的话 0.00098 会低于任何合理的阈值，那条记忆就被时间静默删掉了。
    """
    fake_embedding(monkeypatch, {"查询": BASE})
    put("很久以前就有的偏好", unit(0.8), created_days_ago=300)
    hits = store.recall("查询", owner="")
    assert len(hits) == 1
    assert hits[0].text == "很久以前就有的偏好"
    assert hits[0].decay < 0.01
    assert hits[0].score < 0.001


def test_old_but_more_relevant_can_still_outrank_new(monkeypatch):
    """衰减不是"新旧一票否决"：旧记忆语义明显更贴合时仍然能排在前面。

    这条决定了 `HALF_LIFE_DAYS = 30` 不是"30 天后旧记忆一律不可用"——它只让同样
    的相关性在时间上打折。
    """
    fake_embedding(monkeypatch, {"查询": BASE})
    put("很贴合的旧偏好", unit(0.95), created_days_ago=10)   # 0.95 × 0.784 ≈ 0.745
    put("不太相关的新偏好", unit(0.45), created_days_ago=0)  # 0.45 × 1.0   = 0.450
    hits = store.recall("查询", owner="")
    assert [hit.text for hit in hits] == ["很贴合的旧偏好", "不太相关的新偏好"]
    assert hits[0].score > hits[1].score


def test_new_and_old_mix_into_one_score_table(monkeypatch):
    """新旧混排：5 条新的 + 15 条旧的，同一个查询，看衰减是不是真的在排序。

    15 条旧的用同一个余弦（0.70），年龄从 7 天铺到 350 天。它们的 score 必须
    **单调下降**——衰减没生效的话这 15 条会并列在一起，而并列就说明"排序只看余弦"。
    同时 15 条一条都不许少：旧的只是靠后，没有被删。
    """
    fake_embedding(monkeypatch, {"机器学习 复习提纲": BASE})
    for index, cosine in enumerate([0.60, 0.66, 0.72, 0.78, 0.84]):
        put(f"新的偏好 {index}", unit(cosine), created_days_ago=0)
    ages = [7, 14, 30, 45, 60, 90, 120, 150, 180, 210, 240, 270, 300, 330, 350]
    for index, age in enumerate(ages):
        put(f"旧的偏好 {index}", unit(0.70), created_days_ago=age)

    hits = store.recall("机器学习 复习提纲", top_k=20, owner="")
    assert len(hits) == 20, "20 条全在候选里"
    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True), "score 必须单调不增"

    old = [hit for hit in hits if hit.text.startswith("旧的")]
    assert len(old) == 15, "旧的 15 条一条都不许被丢掉"
    assert [hit.decay for hit in old] == sorted(
        (hit.decay for hit in old), reverse=True), "同余弦的旧记忆必须按年龄递减"
    # 一条余弦**更高**（0.70）但 7 天前的记忆，已经被一条余弦**更低**（0.60）的新
    # 记忆反超（0.5955 < 0.6000）——这是"衰减真的进了排序"的直接证据：名次不再只由
    # 相似度决定。只差 0.0045，也正好说明 7 天 = 半衰期的 23%，权重掉 15%。
    newest_low = next(hit for hit in hits if hit.text == "新的偏好 0")
    assert old[0].score < newest_low.score
    # 30 天那条的 decay 恰好 0.5：半衰期在表里是看得见的。
    assert old[2].decay == pytest.approx(0.5, abs=0.01)
    # 350 天那条：decay ≈ 0.00031，score ≈ 0.00022，仍在结果里。
    assert old[-1].score > 0
    assert old[-1].score < 0.001


def test_default_top_k_keeps_the_newer_ones(monkeypatch):
    """默认 `top_k` 下，20 条里只有最靠前的 6 条进提示词——"检索"不是换个说法的全塞。"""
    fake_embedding(monkeypatch, {"查询": BASE})
    for index in range(5):
        put(f"新的 {index}", unit(0.84))
    for index, age in enumerate([7, 30, 90, 180, 350]):
        put(f"旧的 {index}", unit(0.70), created_days_ago=age)
    hits = store.recall("查询", owner="")
    assert len(hits) == store.TOP_K
    # 5 条新的分数相同、写入时刻也相同，它们之间的先后是确定的但没有含义，
    # 所以按集合比。第 6 个名额给的是 7 天那条旧记忆（0.70 × 0.851 ≈ 0.596），
    # 而不是 30 天那条（0.35）——这就是衰减在挑人。
    assert {hit.text for hit in hits[:5]} == {f"新的 {index}" for index in range(5)}
    assert hits[5].text == "旧的 0"


def test_recall_uses_formed_at_when_present(monkeypatch):
    """`formed_at` 优先于 `created_at` 作为衰减基准。

    一条"三年前就有的习惯、今天才第一次被总结出来"不该在排序上冒充新记忆。
    `created_at` 本身不变（它是审计事实），只是衰减读另一个字段。
    """
    fake_embedding(monkeypatch, {"查询": BASE})
    put("今天刚总结出来的旧习惯", unit(0.8), created_days_ago=0, formed_at=stamp(365))
    hit = store.recall("查询", owner="")[0]
    assert hit.age_days == pytest.approx(365, abs=1.0)
    assert hit.decay < 0.001


def test_recall_stable_orders_ties_by_recency(monkeypatch):
    """同分同年（同一批写进来的）按 `created_at` 新的在前：同样的输入永远同样的输出。"""
    fake_embedding(monkeypatch, {"查询": BASE})
    put("先写的", unit(0.9), created_days_ago=1)
    put("后写的", unit(0.9), created_days_ago=0)
    assert [hit.text for hit in store.recall("查询", owner="")] == ["后写的", "先写的"]


def test_recall_skips_rows_from_another_embedding_model(monkeypatch):
    """换了 embedding 模型之后，旧向量与新查询向量不在同一个空间里：跳过。

    直接点积会得到一个"看起来合理、实际是垃圾"的数——比跳过危险得多。
    """
    fake_embedding(monkeypatch, {"查询": BASE})
    put("旧模型算的向量", BASE)
    with closing(repository.connect()) as conn, conn:
        conn.execute(f"UPDATE {store.ITEM_TABLE} SET model = 'text-embedding-v2'")
    assert store.recall("查询", owner="") == []


def test_recall_skips_rows_without_a_vector(monkeypatch):
    fake_embedding(monkeypatch, {"查询": BASE})
    put("没有向量的那条", None)
    assert store.recall("查询", owner="") == []


def test_recall_touch_marks_last_used(monkeypatch):
    """`touch=True` 刷 `last_used_at`；它不参与排序，只是观测值。"""
    fake_embedding(monkeypatch, {"查询": BASE})
    item_id = put("被检索到的", unit(0.9))
    store.recall("查询", touch=True, owner="")
    with closing(repository.connect()) as conn:
        row = conn.execute(
            f"SELECT last_used_at FROM {store.ITEM_TABLE} WHERE id = ?", (item_id,)
        ).fetchone()
    assert row[0] is not None


def test_recall_does_not_touch_by_default(monkeypatch):
    """默认不写库：`recall` 在只读语境里（HTTP 的 `/search`）必须保持只读。"""
    fake_embedding(monkeypatch, {"查询": BASE})
    item_id = put("不该被标记", unit(0.9))
    store.recall("查询", owner="")
    with closing(repository.connect()) as conn:
        row = conn.execute(
            f"SELECT last_used_at FROM {store.ITEM_TABLE} WHERE id = ?", (item_id,)
        ).fetchone()
    assert row[0] is None


def test_recall_query_vector_is_cached(monkeypatch):
    """查询向量走 `vector_cache`：同一个 `goal` 反复生成不该重复付费。"""
    sent: list[str] = []

    def embed(texts: list[str]) -> list[list[float] | None]:
        sent.extend(texts)
        return [_normalize(BASE) for _ in texts]

    monkeypatch.setenv("MINDMAP_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("MINDMAP_EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(store, "embed_texts", embed)
    monkeypatch.setattr(store.vector_cache, "get_vectors", lambda hashes, model: {})
    written: list[str] = []
    monkeypatch.setattr(store.vector_cache, "put_vectors",
                        lambda items, model: written.extend(key for key, _ in items) or 0)

    store.query_vector("同一个目标")
    assert sent == ["同一个目标"]
    assert written == [vector_cache.text_hash("同一个目标")]


# ---------------------------------------------------------------- query 与注入

class _Source:
    def __init__(self, filename: str | None) -> None:
        self.filename = filename


class _Request:
    def __init__(self, goal: str, sources: list[_Source] | None = None) -> None:
        self.goal = goal
        self.sources = sources or []


def test_query_text_is_goal_plus_material_titles():
    request = _Request("机器学习 复习提纲", [_Source("第3章.pdf"), _Source(None)])
    assert store.query_text(request) == "机器学习 复习提纲\n第3章.pdf"


def test_query_text_caps_titles():
    """50 份材料的文件名全拼进去，向量会退化成"一堆文件名的平均"。"""
    sources = [_Source(f"材料{index}.pdf") for index in range(10)]
    assert store.query_text(_Request("主题", sources)).count("\n") == store.MAX_QUERY_SOURCES


def test_query_text_without_materials_falls_back_to_the_topic():
    """"都没有时退化成纯主题"——`goal` 一定有，所以退化的结果就是 goal 本身。"""
    assert store.query_text(_Request("纯主题")) == "纯主题"


def test_prompt_for_is_empty_without_hits(monkeypatch):
    """没有命中 → **空串**，调用方据此整段跳过（不注入"（暂无记忆）"）。"""
    fake_embedding(monkeypatch, {"纯主题": BASE})
    assert store.prompt_for(_Request("纯主题"), owner="") == ""


def test_prompt_for_renders_the_header_and_lines(monkeypatch):
    fake_embedding(monkeypatch, {"纯主题": BASE})
    put("标题倾向去掉人名", unit(0.9))
    text = store.prompt_for(_Request("纯主题"), owner="")
    assert text.startswith(store.MEMORY_HEADER)
    assert "- 标题倾向去掉人名" in text


def test_prompt_for_marks_the_hits_as_used(monkeypatch):
    """注入路径 `touch=True`：这条数据是"这套东西有没有被用上"的唯一凭据。"""
    fake_embedding(monkeypatch, {"纯主题": BASE})
    item_id = put("被注入的那条", unit(0.9))
    store.prompt_for(_Request("纯主题"), owner="")
    with closing(repository.connect()) as conn:
        row = conn.execute(
            f"SELECT last_used_at FROM {store.ITEM_TABLE} WHERE id = ?", (item_id,)
        ).fetchone()
    assert row[0] is not None


def test_prompt_for_and_notes_header_are_two_different_paragraphs():
    """记忆那一段与 `note.*` 那一段必须是两个表头、两句不同的话。

    合并成一段的后果是用户无法判断"哪几句话是我说的"——显式意图与系统推断混在
    一起，正是这次改动要避免的那种混乱。
    """
    from app.prefs.service import NOTE_HEADER

    assert store.MEMORY_HEADER != NOTE_HEADER
    assert "自动" in store.MEMORY_HEADER
    assert "自动" not in NOTE_HEADER
