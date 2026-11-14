"""真实 Neo4j 上的**读**路径：stats / 游走 / 图检索 / 一次端到端入库。

与 `tests/test_graph_neo4j.py` 的分工：那个文件**拥有**实例（每个用例前后
`DETACH DELETE`），它测的是写语义（约束、幂等、事务回滚）。这个文件只**读**一张
已经建好的图——清空它等于把被测对象删掉，所以它绝不 wipe。

三道 skip，任何一道不成立就跳过而不是失败（本机没有 Neo4j 是这个项目的常态，CI 上
更是）：

1. 没配 `MINDMAP_TEST_NEO4J_PASSWORD`（与 `test_graph_neo4j.py` 同一个变量名，
   跑法一致）；
2. 连不上；
3. 图里还没有实体——建图要真调模型，刚 clone 的机器上就是空的。

跨文档那两条再多一道：图上的边存的是**建图时那个库**的持久 `source_id`，没有那个库
（`MINDMAP_LIVE_LIBRARY_DB`）就解不出正文，`search_graph` 会把每条证据都丢掉——那时
要 skip 而不是断言"图没用"。

端到端入库默认 skip：它真调一次模型（花钱、看网络）。要看它跑：
`MINDMAP_LIVE_LLM=1 pytest tests/test_graph_neo4j_live.py -k end_to_end`。
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import httpx
import pytest

from app.contracts.source import Source, SourceKind
from app.knowledge import graph_ingest, graph_search, graph_store, library

pytestmark = pytest.mark.integration

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
DOCS = Path(__file__).resolve().parents[2] / "docs"

URI_ENV = "MINDMAP_TEST_NEO4J_URI"
USER_ENV = "MINDMAP_TEST_NEO4J_USER"
PASSWORD_ENV = "MINDMAP_TEST_NEO4J_PASSWORD"
DATABASE_ENV = "MINDMAP_TEST_NEO4J_DATABASE"
LIBRARY_ENV = "MINDMAP_LIVE_LIBRARY_DB"
LLM_ENV = "MINDMAP_LIVE_LLM"

# 端到端那条要真调模型，而 `conftest.clean_env` 把 `MINDMAP_LLM_*` 全清空了（这是
# 全局隔离的核心，不能为一条用例松掉）。所以凭证从一组**测试专用**的名字读进来，
# 用例里再映射成实现读的那些变量：CI 上不设就 skip，本机跑一次只花一次调用。
LIVE_LLM_ENVS = {
    "MINDMAP_LLM_MODE": "MINDMAP_LIVE_LLM_MODE",
    "MINDMAP_LLM_BASE_URL": "MINDMAP_LIVE_LLM_BASE_URL",
    "MINDMAP_LLM_API_KEY": "MINDMAP_LIVE_LLM_API_KEY",
    "MINDMAP_LLM_MODEL": "MINDMAP_LIVE_LLM_MODEL",
}

DEFAULT_URI = "bolt://127.0.0.1:7687"

# `conftest.no_real_http` 会把传输层全部打桩成"禁止真发请求"。端到端那条要真调模型，
# 所以在模块导入时（早于任何 fixture）抓住原始实现，用例里再把它装回去。
_REAL_HANDLE = httpx.HTTPTransport.handle_request
_REAL_HANDLE_ASYNC = httpx.AsyncHTTPTransport.handle_async_request


def _settings() -> dict[str, str]:
    password = os.getenv(PASSWORD_ENV, "").strip()
    if not password:
        pytest.skip(f"未设置 {PASSWORD_ENV}：跳过需要 Neo4j 的用例")
    return {
        "uri": os.getenv(URI_ENV, "").strip() or DEFAULT_URI,
        "user": os.getenv(USER_ENV, "").strip() or "neo4j",
        "password": password,
        "database": os.getenv(DATABASE_ENV, "").strip() or "neo4j",
    }


@pytest.fixture()
def live_graph(monkeypatch):
    """把 `MINDMAP_NEO4J_*` 指到实测实例；**不清库**（理由见模块 docstring）。"""
    settings = _settings()
    graph_store.close_driver()
    monkeypatch.setenv("MINDMAP_NEO4J_URI", settings["uri"])
    monkeypatch.setenv("MINDMAP_NEO4J_USER", settings["user"])
    monkeypatch.setenv("MINDMAP_NEO4J_PASSWORD", settings["password"])
    monkeypatch.setenv("MINDMAP_NEO4J_DATABASE", settings["database"])
    counts = graph_store.stats()
    if counts["entities"] <= 0:
        graph_store.close_driver()
        pytest.skip("图里还没有实体：先跑一次材料入库（见 docs/RAG-DECISIONS.md 第 8 节）")
    yield graph_store
    graph_store.close_driver()


# ---------------------------------------------------------------- 1. 连接与 stats


def test_stats_has_every_key(live_graph):
    """`stats()` 的五个键必须齐：少一个，调用方在"图是空的"上就会走错分支。"""
    counts = live_graph.stats()
    assert set(counts) == {"entities", "edges", "isolated_entities", "aliases",
                           "merged_entities"}
    assert counts["entities"] >= 1
    assert counts["edges"] >= 0
    # 孤立实体是有实体没边的那部分，不可能比实体总数多。
    assert 0 <= counts["isolated_entities"] <= counts["entities"]


def test_degrees_and_walk_agree(live_graph):
    """度数 > 0 的实体，一跳游走必须给得出路径（无向、1 跳）。"""
    degrees = live_graph.entity_degrees(limit=20)
    assert degrees, "图里没有实体度数：entity_degrees 读不到东西"
    name, degree = degrees[0]
    paths = live_graph.neighbors(name, hops=1, limit=10)
    assert isinstance(paths, list)
    if degree > 0:
        assert paths, f"度数 {degree} 的实体在 1 跳上游走不到任何路径"
        assert all(path.hops == 1 for path in paths)


# ---------------------------------------------------------------- 2. 跨文档图检索


def _load_eval_module():
    spec = importlib.util.spec_from_file_location(
        "evaluate_retrieval_live", SCRIPTS / "evaluate_retrieval.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_crossdoc_queries_can_hit_two_materials(live_graph, monkeypatch):
    """跨文档套里**至少一条**问法的图命中同时来自两份材料。

    这条钉的是"图路确实能产出跨文档证据"这个能力本身，不是"图检索有收益"——收益
    要看 recall / coverage 的对照（本仓库实测为零，见 `graph_search` 的默认值说明）。
    断言写成"至少一条"而不是逐条：命中取决于图里抽出了哪些实体名，而那是模型的
    产物，逐条断言会把测试钉死在一张具体的图上。
    """
    db = os.getenv(LIBRARY_ENV, "").strip()
    if not db:
        pytest.skip(f"未提供建图时用的库（{LIBRARY_ENV}）：边上的 source_id 解不出正文")
    monkeypatch.setenv("MINDMAP_DB_PATH", db)
    monkeypatch.setenv("MINDMAP_GRAPH_SEARCH", "1")

    module = _load_eval_module()
    materials = module.load_materials(list(module.DEFAULT_MATERIALS), [])
    sources = [material.source for material in materials]
    # `_load_eval_module()` 已把 `scripts/` 插进 sys.path（它自己也要 import 那两套 case）。
    crossdoc = importlib.import_module("eval_cases_crossdoc")

    covered = []
    for case in crossdoc.CASES:
        hits = graph_search.search_graph(sources, case.query, graph_search.graph_pool(10))
        covered.append(len({hit.source_id for hit in hits}))
    assert max(covered) >= 2, f"14 条问法里没有一条的图命中跨到两份材料：{covered}"


# ---------------------------------------------------------------- 3. 端到端入库


def test_ingest_graph_end_to_end(live_graph, monkeypatch):
    """真材料 → 一次真实抽取 → 实体/边落进 Neo4j。

    默认 skip：它真调一次模型。要看它跑就设 `MINDMAP_LIVE_LLM=1`——CI 上没有凭证，
    也不该为一个"能跑通"的证据付钱。
    """
    if os.getenv(LLM_ENV, "").strip() not in {"1", "true", "yes"}:
        pytest.skip(f"真调模型的用例默认不跑：设 {LLM_ENV}=1")
    for target, source in LIVE_LLM_ENVS.items():
        value = os.getenv(source, "").strip()
        if not value:
            pytest.skip(f"未设置 {source}：端到端用例需要一组真实的模型凭证")
        monkeypatch.setenv(target, value)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _REAL_HANDLE)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request",
                        _REAL_HANDLE_ASYNC)
    monkeypatch.setenv("MINDMAP_GRAPH_EXTRACTION", "1")

    text = (DOCS / "ARCHITECTURE.md").read_text(encoding="utf-8")[:1200]
    source = Source(kind=SourceKind.markdown, filename="ARCHITECTURE.md", text=text)
    before = live_graph.stats()

    source_id = library.ingest(source)
    result = asyncio.run(
        graph_ingest.ingest_graph(source, source_id=source_id, max_chunks=1))

    assert result["chunks"] == 1
    assert not result["aborted"]
    assert result["failed"] == 0, "抽取或写库失败：这一次没有可评估的产物"
    assert result["entities"] + result["edges"] > 0, "抽出来的实体与边都是 0"
    after = live_graph.stats()
    assert after["entities"] + after["edges"] > before["entities"] + before["edges"]
