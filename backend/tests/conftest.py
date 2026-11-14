"""测试会话的全局隔离：宿主机上配了什么，都不能改变任何一条断言。

为什么必须有这一层：每个测试文件各写一份 `delenv`，结局一定是漏。`test_knowledge.py`
只清了 `DASHSCOPE_API_KEY`，没清 `MINDMAP_EMBEDDING_API_KEY`——于是同一份代码在
配了 embedding key 的机器上会真的去调接口，`test_score_is_bm25` 从"BM25 原始分"
变成"RRF 分"而失败，文件耗时从 0.4s 涨到 3.5s。防护只该有一处，就写在这里。

三条默认值：

  - **没有凭证**：embedding 与 LLM 的 key/base/model 一律清空，`configured()`
    恒为假，`search_sources` 稳定走纯 BM25。
  - **开关显式关**：`MINDMAP_HYBRID_SEARCH=0`、`MINDMAP_QUERY_EXPANSION=0`、
    `MINDMAP_PARENT_RETRIEVAL=0`、`MINDMAP_GRAPH_EXTRACTION=0`、`MINDMAP_RERANK=0`。
    需要它们的用例自己 `monkeypatch.setenv(..., "1")`，
    那会覆盖这里的值；反过来，忘记开的用例只会"少测"一条路，不会偷偷发出真实请求。

    `MINDMAP_RERANK` 在**线上默认是 1**（评估跑出 R@1 +14.2pp 之后翻的，见
    `app/knowledge/rerank.py`），这里仍然置 0，理由与其余开关不同：它是唯一一个
    "开着就要多一次网络往返"的开关，而绝大多数用例关心的不是重排。置 0 之后
    "重排这条路"只在 `tests/test_rerank.py` 里被覆盖——那是刻意的，别在这里改成
    1，否则每个配了 `MINDMAP_EMBEDDING_API_KEY` 替身的用例都会多出一次被 mock
    的 rerank 调用，断言会以"多了一条请求"的形式莫名失败。
  - **数据库落 tmp**：`MINDMAP_DB_PATH` 指向本次测试的 `tmp_path`，仓库里的
    `backend/data/mindmaps.db` 不会被任何测试碰到。
  - **`MINDMAP_NEO4J_*` 一律清空**：宿主机上可能正好跑着一个 Neo4j（开发机上很
    常见），不清空的话图存储的用例会去连它、写它、然后断在别人的数据上。清空之后
    "没配置图存储"是每个用例的默认前提，需要真 Neo4j 的用例自己 `setenv` 进去
    （见 `tests/test_graph_neo4j.py` 的 `graph` fixture）。

图存储的驱动是模块级单例，`monkeypatch` 只撤环境变量、撤不掉已经建好的连接池，
所以再加一条 `reset_graph_driver`：每个用例前后都断开，谁知道上一个用例把单例
指到哪个实例上去了。

另有一道网络闸：真走到 `httpx` 传输层的请求直接抛断言。所有需要 HTTP 的用例
都在更外层替换了 `httpx.Client` / `httpx.AsyncClient`（或用 `MockTransport`），
根本走不到传输层；这道闸证明的是"这些断言不依赖网络"，而不是"实现不发请求"。
"""
from __future__ import annotations

import httpx
import pytest

# 凭证与外部服务地址。`DASHSCOPE_API_KEY` 单独列：embedding 与 ASR 都会回退到
# 它，只清 `MINDMAP_EMBEDDING_API_KEY` 是上一版漏掉的那一半。
CREDENTIALS = (
    "DASHSCOPE_API_KEY",
    "MINDMAP_EMBEDDING_API_KEY",
    "MINDMAP_EMBEDDING_BASE_URL",
    "MINDMAP_EMBEDDING_PROTOCOL",
    "MINDMAP_EMBEDDING_MODEL",
    "MINDMAP_EMBEDDING_DIM",
    "MINDMAP_LLM_MODE",
    "MINDMAP_LLM_BASE_URL",
    "MINDMAP_LLM_API_KEY",
    "MINDMAP_LLM_MODEL",
)

# 其余会改变行为的环境变量：开关、本地模型目录、外部程序路径。
BEHAVIOUR = (
    "MINDMAP_AGENT_ENABLED",
    "MINDMAP_NLI_ENABLED",
    "MINDMAP_NLI_MODEL_DIR",
    "MINDMAP_NLI_MIN_FREE_MB",
    "MINDMAP_SOFFICE_CMD",
    # 材料进 prompt 的形态（`full` / `rag`）。清空 = 用默认值（`rag`）。
    # 不清的话，宿主机上留着 `MINDMAP_SOURCE_MODE=full` 会让 `test_source_mode.py`
    # 里"正文不进 prompt"那几条在别的机器上悄悄变成假绿/假红——它是这个文件
    # 存在的理由（"防护只该有一处"）的又一个例子。
    "MINDMAP_SOURCE_MODE",
    # 知识库检索（`tools.library_search_enabled`）。**清空而不是置 0**：它默认开，
    # 而"默认开 + 临时库是空的 = 既有用例行为不变"正是接线时要验证的那条前提。
    # 置 0 会把那条前提整个跳过：所有用例都不会经过"查了一次空库"的那条路，
    # 于是"库空时逐字节不变"永远没被人跑过。需要关掉的用例自己 `setenv(..., "0")`。
    "MINDMAP_LIBRARY_SEARCH",
    # 重排模型名（`knowledge/rerank.py`）。清空 = 用默认值。不清的话宿主机上换个
    # 模型名会让 `tests/test_rerank.py` 里断言请求体的几条换一个字符串。
    "MINDMAP_RERANK_MODEL",
    # 输出 token 上限（`generation/service.max_tokens_cap`）。清空 = 用默认值 65536。
    # 不清的话，宿主机上为 qwen 配过 32768 的机器会把"发出去的 max_tokens"整批改小，
    # `tests/test_budget.py` 与 `test_agent_tools.py` 里断言数值的那几条会随机器漂。
    "MINDMAP_LLM_MAX_TOKENS",
)

SWITCHES = ("MINDMAP_HYBRID_SEARCH", "MINDMAP_QUERY_EXPANSION", "MINDMAP_PARENT_RETRIEVAL",
            "MINDMAP_GRAPH_EXTRACTION", "MINDMAP_RERANK")

# 图存储的连接参数。清空而不是设默认值：URI/USER/DATABASE 有代码默认，PASSWORD
# 没有——"没配"必须是可见的未配置状态，否则宿主机上跑着的 Neo4j 会被单测连上。
NEO4J = (
    "MINDMAP_NEO4J_URI",
    "MINDMAP_NEO4J_USER",
    "MINDMAP_NEO4J_PASSWORD",
    "MINDMAP_NEO4J_DATABASE",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """每个测试的起点：没凭证、开关全关、数据库在 tmp、图存储未配置。"""
    for name in (*CREDENTIALS, *BEHAVIOUR, *NEO4J):
        monkeypatch.delenv(name, raising=False)
    for name in SWITCHES:
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("MINDMAP_DB_PATH", str(tmp_path / "mindmaps.db"))
    yield


@pytest.fixture(autouse=True)
def reset_graph_driver():
    """图存储的驱动单例在每个用例前后都关掉。

    不关的后果不是"慢"，是"错"：上一个用例把单例指到某个实例上，下一个用例改了
    `MINDMAP_NEO4J_*` 却拿到旧连接池，于是它在写上一个用例的库——这种错在测试里
    表现为"偶尔失败"，最难查。
    """
    from app.knowledge import graph_store

    graph_store.close_driver()
    yield
    graph_store.close_driver()


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch):
    """真发到传输层的请求一律失败，并指出目标 URL。"""
    def blocked(self, request):
        raise AssertionError(
            f"测试不该发真实网络请求：{request.method} {request.url}")

    async def blocked_async(self, request):
        raise AssertionError(
            f"测试不该发真实网络请求：{request.method} {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)
