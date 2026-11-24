"""记忆表本身与写入路径。

检索与时间衰减在 `test_memory_recall.py`：这里只回答"表建没建、写进去的是什么、
重复写会怎样"。分开是因为这两件事的错误代价完全不同——写坏了是**永久丢一条事实**，
排错了只是**这一条没被选中**。
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

    返回被送去算向量的文本列表——**"一次生成检索了几次"就是数它的长度**，
    这是这次改动最硬的一条性能约束的唯一机器可查的证据。

    缓存两侧也一起换掉：不换的话 `vector_cache` 会在同一个 sqlite 库上真的读写，
    而"命中缓存"与"真的调了一次 embedding"在计数上就没有区别了。
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
    """直接写一行，**绕开 `add()`**：要精确控制 `created_at`（衰减测试的输入）。

    `add()` 永远写"现在"，而"一条 90 天前的记忆"只能用这种方式造出来。
    """
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


# ---------------------------------------------------------------- 表

def test_tables_are_created_with_the_other_tables():
    """两张表在 `repository.connect()` 里就建好，与 `mindmaps` / `runs` 同一时刻。

    读记忆的两条路径（每次生成前的一次检索、会话结束时的总结）都在热路径上，
    多一条"发现表不存在 → 建表"的分支，等于在最需要稳定的地方多一个可能抛错的
    分支——`runs` / `user_prefs` / `edit_events` 的注释里都写过同一条理由。
    """
    with closing(repository.connect()) as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {store.ITEM_TABLE, store.SESSION_TABLE} <= names
    assert store.count(owner="") == 0


def test_schema_is_idempotent():
    """连两次不炸、不重复建。表是每次 connect 都会执行的 DDL。"""
    repository.connect().close()
    repository.connect().close()
    assert store.count(owner="") == 0


def test_text_hash_is_unique(monkeypatch):
    """`text_hash` 上是 UNIQUE 索引，不只是普通索引。

    "先查再插"在并发下会漏（总结管线与手工添加可能同时写）。让数据库裁决，才不会
    出现同一条偏好两条记录——那会白占两个 top_k 名额。
    """
    fake_embedding(monkeypatch)
    put("同一条文本", [1.0, 0.0])
    import sqlite3

    with closing(repository.connect()) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {store.ITEM_TABLE} "
                "(id, text, kind, source, created_at, text_hash) "
                "VALUES ('x', '同一条文本', 'preference', 'summary', ?, ?)",
                (stamp(), vector_cache.text_hash("同一条文本")),
            )


# ---------------------------------------------------------------- 写

def test_add_dedupes_by_text_hash(monkeypatch):
    """同一条文本第二次写返回已有那条，`created=False`，且不再算一次向量。

    "不再算向量"是这条用例真正钉的东西：同一条偏好会在好几次会话里被反复总结
    出来，每次都重新 embed 一次就是在给同一句话反复付钱。
    """
    sent = fake_embedding(monkeypatch, {"标题去掉人名": [1.0, 0.0]})
    first = store.add("标题去掉人名", "preference")
    assert first.created is True
    assert first.item.vector_dim == 2
    assert len(sent) == 1

    second = store.add("标题去掉人名", "fact")
    assert second.created is False
    assert second.item.id == first.item.id
    # 档位没有覆盖掉原来的：同一条文本只该存在一次，后写的档位不生效。
    assert second.item.kind == "preference"
    assert len(sent) == 1


def test_add_fills_a_missing_vector_on_dedupe(monkeypatch):
    """去重命中、但那条没有向量（上次 embedding 失败）时补上。

    不补的后果是它永久检索不到，而它在列表里看起来完全正常——正是最难发现的一种坏。
    """
    put("没有向量的那条", None)
    fake_embedding(monkeypatch, {"没有向量的那条": [1.0, 0.0]})
    added = store.add("没有向量的那条")
    assert added.created is False
    assert added.item.has_vector


def test_add_truncates_instead_of_rejecting(monkeypatch):
    """超长**截断**而不是拒绝：拒绝等于这条事实永久丢失（同一个会话只总结一次）。"""
    fake_embedding(monkeypatch)
    added = store.add("龙" * 150)
    assert len(added.item.text) == store.MAX_ITEM_CHARS


def test_add_rejects_unknown_kind_and_source(monkeypatch):
    fake_embedding(monkeypatch)
    with pytest.raises(ValueError):
        store.add("一句话", "opinion")
    with pytest.raises(ValueError):
        store.add("一句话", source="guess")
    with pytest.raises(ValueError):
        store.add("   ")


def test_add_without_embedding_config_stores_a_row(monkeypatch):
    """向量服务没配置时**条目照样落库**，只是没有向量。

    失败的粒度必须是"这条检索不到"，而不是"整次总结一条都没写进去"。
    """
    monkeypatch.delenv("MINDMAP_EMBEDDING_API_KEY", raising=False)
    added = store.add("标题去掉人名")
    assert added.created is True
    assert added.item.has_vector is False


def test_add_records_provenance(monkeypatch):
    """来源、会话、运行三个身份都要写上：事后能回答"这条是哪次总结出来的"。"""
    fake_embedding(monkeypatch)
    added = store.add("偏好鱼骨图", "pattern", source=store.SOURCE_USER_EXPLICIT,
                      source_run_id="run-1", session_id="sess-1",
                      formed_at="2026-11-01T00:00:00.000Z")
    assert added.item.source == store.SOURCE_USER_EXPLICIT
    assert added.item.source_run_id == "run-1"
    assert added.item.session_id == "sess-1"
    assert added.item.formed_at == "2026-11-01T00:00:00.000Z"
    assert added.item.created_at


def test_delete_reports_whether_it_happened(monkeypatch):
    """硬删。返回 False 让 HTTP 层能给出 404，而不是幂等 204。"""
    fake_embedding(monkeypatch)
    item = store.add("要删掉的").item
    assert store.delete(item.id, owner="") is True
    assert store.delete(item.id, owner="") is False
    assert store.count(owner="") == 0


def test_items_are_newest_first(monkeypatch):
    fake_embedding(monkeypatch)
    store.add("第一条")
    store.add("第二条")
    assert [item.text for item in store.items(owner="")] == ["第二条", "第一条"]


# ---------------------------------------------------------------- 时间解析

@pytest.mark.parametrize("text", [
    "2026-11-19T05:26:30.707Z",
    "2026-11-19T05:26:30Z",
    "2026-11-19",
])
def test_parse_time_accepts_the_three_shapes(text):
    """库里的时间是三种形状：SQLite 的毫秒、无毫秒、模型给的纯日期。"""
    parsed = store.parse_time(text)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_parse_time_garbage_is_none():
    assert store.parse_time("") is None
    assert store.parse_time(None) is None
    assert store.parse_time("昨天") is None


def test_utcnow_is_timezone_aware():
    """时间一律带时区：naive 与 aware 相减会抛 `TypeError`，而那会发生在衰减计算里。"""
    assert store.utcnow().tzinfo is not None
