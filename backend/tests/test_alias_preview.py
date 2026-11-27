"""实体消解产品入口：只读预览、出处、选择、冲突与取消。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
import types
from contextlib import contextmanager
from pathlib import Path

import pytest
from conftest import guest_client
from test_graph_resolve import FakeAliasStore, FakeResult

from app.contracts.source import Source, SourceKind
from app.knowledge import graph_resolve, graph_store, library, router

_REAL_ALIAS_SNAPSHOT = graph_store.alias_snapshot
_REAL_ADD_ALIASES = graph_store.add_aliases


@pytest.fixture
def graph(monkeypatch):
    with router._PREVIEW_LOCK:
        router._PREVIEWS.clear()
    monkeypatch.setattr(graph_store, "configured", lambda: True)
    monkeypatch.setattr(graph_resolve, "embedding_configured", lambda: True)
    documents = {
        "国家市场监督管理总局": ("监管材料.txt", "国家市场监督管理总局负责市场监管。"),
        "市场监管总局": ("简称材料.txt", "市场监管总局发布监管通知。"),
        "预算审批流程": ("流程材料.txt", "预算审批流程需要财务复核。"),
        "审批流程": ("简称流程.txt", "审批流程包含财务复核。"),
    }
    evidence = {}
    for name, (filename, text) in documents.items():
        source_id = library.ingest(Source(kind=SourceKind.text, filename=filename,
                                          text=text, status="parsed"))
        evidence[name] = [(source_id, 0, len(text))]
    state: list[tuple[str, str]] = []
    writes = []

    monkeypatch.setattr(graph_store, "alias_snapshot", lambda: sorted(state))
    monkeypatch.setattr(graph_store, "entity_degrees", lambda **kw: [
        ("国家市场监督管理总局", 3), ("市场监管总局", 1),
        ("预算审批流程", 3), ("审批流程", 1),
    ])
    monkeypatch.setattr(graph_store, "entity_evidence", lambda names: evidence)
    monkeypatch.setattr(graph_resolve, "embed_texts", lambda names: [
        [1.0, 0.0] if "市场" in name else [0.0, 1.0] for name in names
    ])

    def no_cache(*args, **kwargs):
        raise AssertionError("预览不能读写会初始化 SQLite 的向量缓存")

    monkeypatch.setattr(graph_resolve.vector_cache, "get_vectors", no_cache)
    monkeypatch.setattr(graph_resolve.vector_cache, "put_vectors", no_cache)

    def add_aliases(candidates, *, expected_aliases):
        if sorted(state) != expected_aliases:
            raise graph_store.AliasSnapshotConflict("stale")
        for candidate in candidates:
            for alias in candidate.aliases:
                state.append((alias, candidate.canonical))
                writes.append((alias, candidate.canonical))
        return len(candidates)

    monkeypatch.setattr(graph_store, "add_aliases", add_aliases)
    yield state, writes, evidence
    with router._PREVIEW_LOCK:
        router._PREVIEWS.clear()


def _wait(api, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = api.get(f"/api/knowledge/aliases/candidates/{job_id}")
        body = response.json()
        if body["status"] != "running":
            return body
        time.sleep(0.01)
    pytest.fail("预览线程未完成")


def test_preview_shows_real_quotes_and_applies_only_selected(graph):
    state, writes, _ = graph
    with guest_client() as api:
        response = api.post("/api/knowledge/aliases/candidates")
        assert response.status_code == 202
        preview = _wait(api, response.json()["job_id"])
        assert preview["status"] == "done", preview
        assert len(preview["items"]) == 2
        candidate = next(item for item in preview["items"] if item["alias"] == "市场监管总局")
        assert candidate["canonical"] == "国家市场监督管理总局"
        assert candidate["reason"] == "substring" and candidate["similarity"] == 0.6
        assert candidate["alias_evidence"][0]["filename"] == "简称材料.txt"
        assert candidate["alias_evidence"][0]["quote"] == "市场监管总局发布监管通知。"
        assert candidate["alias_evidence"][0]["kind"] == "graph_edge"
        assert candidate["canonical_evidence"][0]["quote"] == "国家市场监督管理总局负责市场监管。"
        assert state == writes == []
        applied = api.post("/api/knowledge/aliases/apply", json={
            "job_id": preview["job_id"], "candidate_ids": [candidate["id"]],
        })
        assert applied.status_code == 200, applied.text
        assert applied.json() == {"relationships_created": 1, "created": [{
            "alias": "市场监管总局", "canonical": "国家市场监督管理总局",
        }], "skipped": 0}
        assert writes == [("市场监管总局", "国家市场监督管理总局")]
        assert api.post("/api/knowledge/aliases/apply", json={
            "job_id": preview["job_id"], "candidate_ids": [candidate["id"]],
        }).status_code == 409


def test_missing_evidence_and_forged_or_duplicate_choices_are_rejected(graph):
    _, writes, evidence = graph
    library.forget(evidence["市场监管总局"][0][0])
    evidence["市场监管总局"] = [("missing-source", 0, 10)]
    with guest_client() as api:
        job_id = api.post("/api/knowledge/aliases/candidates").json()["job_id"]
        preview = _wait(api, job_id)
        missing = next(item for item in preview["items"] if item["alias"] == "市场监管总局")
        assert missing["alias_evidence"] == [] and not missing["selectable"]
        for ids in ([missing["id"]], ["forged"], ["0", "0"]):
            response = api.post("/api/knowledge/aliases/apply", json={
                "job_id": job_id, "candidate_ids": ids,
            })
            assert response.status_code == 422
            assert response.json()["code"] == "invalid_candidates"
        extra = api.post("/api/knowledge/aliases/apply", json={
            "job_id": job_id, "candidate_ids": ["0"], "canonical": "伪造",
        })
        assert extra.status_code == 422 and extra.json()["code"] == "validation_error"
    assert writes == []


def test_isolated_entity_can_use_a_clearly_labeled_literal_match(graph):
    _, _, evidence = graph
    evidence["市场监管总局"] = []
    with guest_client() as api:
        job_id = api.post("/api/knowledge/aliases/candidates").json()["job_id"]
        preview = _wait(api, job_id)
        candidate = next(item for item in preview["items"] if item["alias"] == "市场监管总局")
        assert candidate["selectable"]
        assert candidate["alias_evidence"][0]["kind"] == "text_match"
        assert candidate["alias_evidence"][0]["quote"] == "市场监管总局发布监管通知。"
        assert candidate["alias_evidence"][0]["filename"] == "简称材料.txt"


def test_selected_isolated_entity_reaches_full_name_edges_and_can_be_undone(graph, monkeypatch):
    _, _, evidence = graph
    evidence["市场监管总局"] = []
    full, short = "国家市场监督管理总局", "市场监管总局"
    path = graph_store.GraphPath(nodes=[full, "价格法"], relations=["执行"],
                                 evidence=[evidence[full][0]], directions=[graph_store.FORWARD])

    class Graph(FakeAliasStore):
        def run(self, query, params=None, **kwargs):
            if query.startswith("MATCH p = "):
                return FakeResult([{}] if full in params["names"] else [])
            return super().run(query, params, **kwargs)

    store = Graph(nodes={full, short, "预算审批流程", "审批流程"})

    @contextmanager
    def transaction(*args, **kwargs):
        yield store

    monkeypatch.setattr(graph_store, "_transaction", transaction)
    monkeypatch.setattr(graph_store, "alias_snapshot", _REAL_ALIAS_SNAPSHOT)
    monkeypatch.setattr(graph_store, "add_aliases", _REAL_ADD_ALIASES)
    monkeypatch.setattr(graph_store, "_parse_path", lambda row: path)
    assert graph_store.neighbors(short) == []
    with guest_client() as api:
        job_id = api.post("/api/knowledge/aliases/candidates").json()["job_id"]
        preview = _wait(api, job_id)
        candidate = next(item for item in preview["items"] if item["alias"] == short)
        response = api.post("/api/knowledge/aliases/apply", json={
            "job_id": job_id, "candidate_ids": [candidate["id"]],
        })
        assert response.status_code == 200, response.text
        assert (short, full) in store.aliases
        assert [item.nodes for item in graph_store.neighbors(short)] == [[full, "价格法"]]
        assert api.delete(f"/api/knowledge/aliases/{short}", params={"canonical": full}).status_code == 200
        assert graph_store.neighbors(short) == []
        assert [item.nodes for item in graph_store.neighbors(full)] == [[full, "价格法"]]


def test_changed_aliases_reject_stale_preview(graph):
    state, writes, _ = graph
    with guest_client() as api:
        job_id = api.post("/api/knowledge/aliases/candidates").json()["job_id"]
        preview = _wait(api, job_id)
        state.append(("另一别名", "另一主名"))
        response = api.post("/api/knowledge/aliases/apply", json={
            "job_id": job_id, "candidate_ids": [preview["items"][0]["id"]],
        })
        assert response.status_code == 409 and response.json()["code"] == "preview_conflict"
    assert writes == []


def test_deleted_source_invalidates_the_reviewed_evidence(graph):
    _, writes, evidence = graph
    with guest_client() as api:
        job_id = api.post("/api/knowledge/aliases/candidates").json()["job_id"]
        preview = _wait(api, job_id)
        candidate = next(item for item in preview["items"] if item["alias"] == "市场监管总局")
        library.forget(evidence["市场监管总局"][0][0])
        response = api.post("/api/knowledge/aliases/apply", json={
            "job_id": job_id, "candidate_ids": [candidate["id"]],
        })
        assert response.status_code == 409 and response.json()["code"] == "preview_conflict"
    assert writes == []


def test_existing_pair_is_not_offered_for_a_misleading_undo(graph):
    state, writes, _ = graph
    state.append(("市场监管总局", "国家市场监督管理总局"))
    with guest_client() as api:
        job_id = api.post("/api/knowledge/aliases/candidates").json()["job_id"]
        preview = _wait(api, job_id)
        assert preview["status"] == "done"
        assert all(item["alias"] != "市场监管总局" for item in preview["items"])
    assert writes == []


def test_partial_embedding_failure_reports_degradation_and_keeps_structural_candidates(graph, monkeypatch):
    monkeypatch.setattr(graph_resolve, "embed_texts", lambda names: [None] * len(names))
    with guest_client() as api:
        job_id = api.post("/api/knowledge/aliases/candidates").json()["job_id"]
        preview = _wait(api, job_id)
        assert preview["status"] == "done"
        assert "只有 0 / 4 个实体有向量" in preview["warnings"][0]
        assert any(item["alias"] == "市场监管总局" for item in preview["items"])


def test_embedding_outage_stops_after_one_failed_batch(monkeypatch):
    monkeypatch.setattr(graph_resolve, "_preview_cached_vectors", lambda names: {})
    requests = []

    def unavailable(names):
        requests.append(names)
        raise graph_resolve.EmbeddingError("offline")

    monkeypatch.setattr(graph_resolve, "embed_texts", unavailable)
    names = [f"实体{index}" for index in range(25)]
    assert graph_resolve._name_vectors(names, read_only=True, allow_partial=True) == {}
    assert requests == [names[:graph_resolve.EMBEDDING_BATCH]]


def test_preview_reuses_cached_vectors_without_writing_new_ones(monkeypatch):
    cached_name, missing_name = "已有向量", "需要向量"
    cached_hash = graph_resolve.vector_cache.text_hash(cached_name)
    missing_hash = graph_resolve.vector_cache.text_hash(missing_name)
    graph_resolve.vector_cache.put_vectors([(cached_hash, [1.0, 0.0])], "test-model")
    monkeypatch.setattr(graph_resolve, "embedding_model", lambda: "test-model")
    requests = []

    def embed(names):
        requests.extend(names)
        return [[0.0, 1.0]]

    monkeypatch.setattr(graph_resolve, "embed_texts", embed)
    result = graph_resolve._name_vectors([cached_name, missing_name], read_only=True)
    assert result == {cached_name: [1.0, 0.0], missing_name: [0.0, 1.0]}
    assert requests == [missing_name]
    assert set(graph_resolve.vector_cache.get_vectors([cached_hash, missing_hash], "test-model")) == {
        cached_hash,
    }


def test_cancel_blocks_apply_and_other_work_until_embedding_returns(graph, monkeypatch):
    _, writes, _ = graph
    started, release = threading.Event(), threading.Event()
    original_embed = graph_resolve.embed_texts

    def blocking_embed(names):
        started.set()
        assert release.wait(5)
        return original_embed(names)

    monkeypatch.setattr(graph_resolve, "embed_texts", blocking_embed)
    try:
        with guest_client() as api:
            job_id = api.post("/api/knowledge/aliases/candidates").json()["job_id"]
            assert started.wait(3)
            assert api.delete(f"/api/knowledge/aliases/candidates/{job_id}").json()["status"] == "cancelled"
            assert api.post("/api/knowledge/aliases/candidates").status_code == 409
            assert api.post("/api/knowledge/aliases/apply", json={
                "job_id": job_id, "candidate_ids": ["0"],
            }).status_code == 409
            release.set()
            deadline = time.monotonic() + 3
            while router._PREVIEWS[job_id]["running"] and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not router._PREVIEWS[job_id]["running"]
            assert api.get(f"/api/knowledge/aliases/candidates/{job_id}").json()["items"] == []
    finally:
        release.set()
    assert writes == []


def test_missing_preview_and_unconfigured_services_are_not_empty_results(graph, monkeypatch):
    with guest_client() as api:
        missing = api.get("/api/knowledge/aliases/candidates/missing")
        assert missing.status_code == 404 and missing.json()["code"] == "not_found"
        monkeypatch.setattr(graph_resolve, "embedding_configured", lambda: False)
        unavailable = api.post("/api/knowledge/aliases/candidates")
        assert unavailable.status_code == 503 and unavailable.json()["code"] == "embedding_unavailable"


def test_graph_write_checks_preview_snapshot_before_any_mutation(monkeypatch):
    queries = []

    class Transaction:
        def run(self, query, params=None):
            queries.append(query)
            if query == graph_store.CYPHER_LOAD_ALIASES:
                return [{"alias": "已有简称", "canonical": "已有全称"}]
            pytest.fail("别名发生变化后不能继续执行图写入")

    @contextmanager
    def transaction(*args, **kwargs):
        yield Transaction()

    monkeypatch.setattr(graph_store, "_transaction", transaction)
    candidate = graph_resolve.MergeCandidate(canonical="预算审批流程", aliases=["审批流程"],
                                              similarity=0.67, reason="substring")
    with pytest.raises(ValueError, match="别名表已变化"):
        graph_store.add_aliases([candidate], expected_aliases=[])
    assert queries == [graph_store.CYPHER_LOAD_ALIASES]


def test_strict_graph_read_propagates_connection_failure(monkeypatch):
    @contextmanager
    def transaction(*args, **kwargs):
        raise graph_store.GraphStoreError("unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(graph_store, "_transaction", transaction)
    assert graph_store.entity_degrees() == []
    with pytest.raises(graph_store.GraphStoreError):
        graph_store.entity_degrees(strict=True)


def test_preview_reserves_bounded_seats_for_recent_names(monkeypatch):
    queries = []

    class Transaction:
        def run(self, query, params=None):
            queries.append(query)
            if query == graph_store.CYPHER_ENTITY_DEGREES:
                return [{"name": f"高频{index}", "degree": 20 - index} for index in range(10)]
            if query == graph_store.CYPHER_RECENT_ENTITIES:
                assert params["limit"] == 2
                return [{"name": "最近的孤立实体", "degree": 0},
                        {"name": "最近的全称实体", "degree": 1}]
            pytest.fail("不应运行其他查询")

    @contextmanager
    def transaction(*args, **kwargs):
        yield Transaction()

    monkeypatch.setattr(graph_store, "_transaction", transaction)
    ordinary = graph_store.entity_degrees(limit=10)
    assert ordinary == [(f"高频{index}", 20 - index) for index in range(10)]
    assert queries == [graph_store.CYPHER_ENTITY_DEGREES]
    queries.clear()
    preview = graph_store.entity_degrees(limit=10, strict=True)
    assert preview == ordinary[:8] + [("最近的孤立实体", 0), ("最近的全称实体", 1)]
    assert queries == [graph_store.CYPHER_ENTITY_DEGREES, graph_store.CYPHER_RECENT_ENTITIES]


def test_preview_uses_strong_direct_edges_instead_of_flattening_a_group():
    existing_short = "市场监管总局"
    existing_full = "国家市场监督管理总局"
    new_full = "国家市场监督管理总局青岚联调分局"
    new_short = "市场监管总局青岚联调分局"
    edges = [
        (existing_short, existing_full, 0.6, "substring"),
        (existing_full, new_full, 0.75, "substring"),
        (new_full, new_short, 0.75, "substring"),
        (existing_short, new_short, 0.5, "substring"),
    ]
    degrees = {existing_short: 20, existing_full: 12, new_full: 2, new_short: 0}
    candidates = graph_resolve._preview_pairs(edges, degrees)
    selected = next(item for item in candidates if item.aliases == [new_short])
    assert selected.canonical == new_full and selected.similarity == 0.75
    assert len({item.aliases[0] for item in candidates}) == len(candidates)
    direct = {(left, right) for left, right, _, _ in edges}
    assert all((item.aliases[0], item.canonical) in direct or
               (item.canonical, item.aliases[0]) in direct for item in candidates)


def test_default_resolver_matches_previous_commit_byte_for_byte(monkeypatch):
    """新预览关闭时仍走原来的分组、缓存与返回值口径。"""
    root = Path(__file__).resolve().parents[2]
    current_degrees = graph_store.entity_degrees
    source = subprocess.run(
        ["git", "show", "a385134:backend/app/knowledge/graph_resolve.py"],
        cwd=root, check=True, capture_output=True, encoding="utf-8",
    ).stdout
    old = types.ModuleType("previous_graph_resolve_for_preview_test")
    monkeypatch.setitem(sys.modules, old.__name__, old)
    exec(compile(source, "previous_graph_resolve.py", "exec"), old.__dict__)
    monkeypatch.setattr(old, "embedding_configured", lambda: True)
    monkeypatch.setattr(graph_resolve, "embedding_configured", lambda: True)
    vectors = {"甲": [1.0, 0.0], "乙": [1.0, 0.0], "预算审批流程": [0.0, 1.0]}
    writes = []
    monkeypatch.setattr(graph_resolve.vector_cache, "get_vectors", lambda hashes, model: {})
    monkeypatch.setattr(graph_resolve.vector_cache, "put_vectors",
                        lambda items, model: writes.append((items, model)))
    for module in (old, graph_resolve):
        monkeypatch.setattr(module, "embedding_model", lambda: "baseline-model")
        monkeypatch.setattr(module, "embed_texts", lambda names: [vectors.get(name) for name in names])
    for rows in ([], [("甲", 2), ("乙", 1)],
                 [("甲", 3), ("乙", 2), ("预算审批流程", 2), ("审批流程", 1)]):
        monkeypatch.setattr(graph_store, "entity_degrees", lambda *, limit: rows)
        writes.clear()
        previous = asyncio.run(old.resolve_entities())
        previous_writes = list(writes)
        writes.clear()
        current = asyncio.run(graph_resolve.resolve_entities())
        assert json.dumps(current, ensure_ascii=False).encode() == json.dumps(
            previous, ensure_ascii=False).encode()
        assert writes == previous_writes

    graph_source = subprocess.run(
        ["git", "show", "a385134:backend/app/knowledge/graph_store.py"],
        cwd=root, check=True, capture_output=True, encoding="utf-8",
    ).stdout
    old_store = types.ModuleType("previous_graph_store_for_preview_test")
    monkeypatch.setitem(sys.modules, old_store.__name__, old_store)
    exec(compile(graph_source, "previous_graph_store.py", "exec"), old_store.__dict__)
    monkeypatch.setattr(graph_store, "entity_degrees", current_degrees)

    class Transaction:
        def run(self, query, params=None):
            assert params == {"limit": 5}
            return [{"name": "甲", "degree": 2}, {"name": "乙", "degree": 0}]

    @contextmanager
    def transaction(*args, **kwargs):
        yield Transaction()

    monkeypatch.setattr(graph_store, "_transaction", transaction)
    monkeypatch.setattr(old_store, "_transaction", transaction)
    previous = old_store.entity_degrees(limit=5)
    current = graph_store.entity_degrees(limit=5)
    assert json.dumps(current, ensure_ascii=False).encode() == json.dumps(
        previous, ensure_ascii=False).encode()
