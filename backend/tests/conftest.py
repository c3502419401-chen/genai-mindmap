"""测试会话的全局隔离：宿主机上配了什么，都不能改变任何一条断言。

为什么必须有这一层：每个测试文件各写一份 `delenv`，结局一定是漏。`test_knowledge.py`
只清了 `DASHSCOPE_API_KEY`，没清 `MINDMAP_EMBEDDING_API_KEY`——于是同一份代码在
配了 embedding key 的机器上会真的去调接口，`test_score_is_bm25` 从"BM25 原始分"
变成"RRF 分"而失败，文件耗时从 0.4s 涨到 3.5s。防护只该有一处，就写在这里。

三条默认值：

  - **没有凭证**：embedding 与 LLM 的 key/base/model 一律清空，`configured()`
    恒为假，`search_sources` 稳定走纯 BM25。
  - **开关显式关**：`MINDMAP_HYBRID_SEARCH=0`、`MINDMAP_QUERY_EXPANSION=0`、
    `MINDMAP_PARENT_RETRIEVAL=0`、`MINDMAP_GRAPH_EXTRACTION=0`。需要它们的用例自己 `monkeypatch.setenv(..., "1")`，
    那会覆盖这里的值；反过来，忘记开的用例只会"少测"一条路，不会偷偷发出真实请求。
  - **数据库落 tmp**：`MINDMAP_DB_PATH` 指向本次测试的 `tmp_path`，仓库里的
    `backend/data/mindmaps.db` 不会被任何测试碰到。

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
)

SWITCHES = ("MINDMAP_HYBRID_SEARCH", "MINDMAP_QUERY_EXPANSION", "MINDMAP_PARENT_RETRIEVAL",
            "MINDMAP_GRAPH_EXTRACTION")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """每个测试的起点：没凭证、开关全关、数据库在 tmp。"""
    for name in (*CREDENTIALS, *BEHAVIOUR):
        monkeypatch.delenv(name, raising=False)
    for name in SWITCHES:
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("MINDMAP_DB_PATH", str(tmp_path / "mindmaps.db"))
    yield


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
