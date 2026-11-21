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
    # DeepSeek 的 tools API（`knowledge/external.py` 的 Search / Fetch）。key 与 base
    # 都要清：它们**会回落**到上面那对 `MINDMAP_LLM_*`，只清专用的两个等于没清——
    # 宿主机上配了 LLM key 时，Search 的用例会真的打出去。
    "MINDMAP_DEEPSEEK_TOOLS_API_KEY",
    "MINDMAP_DEEPSEEK_TOOLS_BASE_URL",
)

# 其余会改变行为的环境变量：开关、本地模型目录、外部程序路径。
BEHAVIOUR = (
    "MINDMAP_AGENT_ENABLED",
    "MINDMAP_NLI_ENABLED",
    "MINDMAP_NLI_MODEL_DIR",
    "MINDMAP_NLI_MIN_FREE_MB",
    "MINDMAP_SOFFICE_CMD",
    # 知识库检索（`tools.library_search_enabled`）。**清空而不是置 0**：它默认开，
    # 而"默认开 + 临时库是空的 = 既有用例行为不变"正是接线时要验证的那条前提。
    # 置 0 会把那条前提整个跳过：所有用例都不会经过"查了一次空库"的那条路，
    # 于是"库空时逐字节不变"永远没被人跑过。需要关掉的用例自己 `setenv(..., "0")`。
    "MINDMAP_LIBRARY_SEARCH",
    # 外部检索的代理地址（`knowledge/external.py`）。清空 = 用默认的 Clash Verge 地址；
    # 不清的话，宿主机上为别的用途配的代理会让外部源的请求目标随机器漂。
    # 单测一律替换 `external._client`，这条只是防止"以为在测 A、其实连了 B"。
    "MINDMAP_EXTERNAL_PROXY",
    # 重排模型名（`knowledge/rerank.py`）。清空 = 用默认值。不清的话宿主机上换个
    # 模型名会让 `tests/test_rerank.py` 里断言请求体的几条换一个字符串。
    "MINDMAP_RERANK_MODEL",
    # 输出 token 上限（`generation/service.max_tokens_cap`）。清空 = 用默认值 65536。
    # 不清的话，宿主机上为 qwen 配过 32768 的机器会把"发出去的 max_tokens"整批改小，
    # `tests/test_budget.py` 与 `test_agent_tools.py` 里断言数值的那几条会随机器漂。
    "MINDMAP_LLM_MAX_TOKENS",
    # 材料要不要在生成时入库（`knowledge/remember.py`）。**清空而不是置 0**：默认开，
    # 而"默认开 + 临时库是空的"就是这些用例要验的起点。不清会继承宿主机变量：
    # 谁在本地设过 `MINDMAP_LIBRARY_REMEMBER=0`，"入库后换 id"那批用例就会集体变红
    # ——一个只在某些机器上出现的绿/红。
    "MINDMAP_LIBRARY_REMEMBER",
    # 风格画像要不要拼进 user 末尾（`prefs/style.PROMPT_SWITCH_ENV`）。**清空而不是
    # 置 0**：它默认**开**（代价只有几十个汉字），而"默认开 + 临时库的 style_profile
    # 是空表 = 拼出来是空串 = 既有断言逐字节不变"正是接线时要验的那条前提。
    # 置 0 会让所有生成用例都不经过"读了画像表但没读到"那条路。
    "MINDMAP_STYLE_PROMPT",
)

SWITCHES = ("MINDMAP_HYBRID_SEARCH", "MINDMAP_QUERY_EXPANSION", "MINDMAP_PARENT_RETRIEVAL",
            "MINDMAP_GRAPH_EXTRACTION", "MINDMAP_RERANK", "MINDMAP_GRAPH_SEARCH",
            # 外部检索的多方向拆分（`external.DIRECTIONS_ENV`）。**置 0**：线上默认也是
            # 关（实测三条全相关 13/20 → 6/20，见 `external.DIRECTIONS_ENV` 那段），
            # 而这里置 0 让每条既有生成用例都不经过"多打两轮源 + 一次模型往返"，
            # 需要它的用例自己 `setenv(..., "1")`。
            "MINDMAP_EXTERNAL_DIRECTIONS",
            # 外部知识检索（`tools.external_knowledge_enabled`）**在这里置 0**，不在
            # BEHAVIOUR 里清空。它现在默认**开**（`dceae7e` 之后翻的，实测见 `tools.py`
            # 那段注释），清空等于让每一条生成用例都真的去搜一次外网：
            # `no_real_http` 会把请求变成断言失败，而 `_external_sources` 会把它当
            # "外部源全挂"吞掉，于是每条用例都多出一句"没有搜到外部资料"和多一个
            # `external` 键——几百条断言一起变红。需要它的用例自己 `setenv(..., "1")`；
            # "默认开"这件事由 `test_external_knowledge.py` 的一条专门用例
            # （`delenv` + 直接问 `external_knowledge_enabled()`）钉着。
            "MINDMAP_EXTERNAL_KNOWLEDGE",
            # DeepSeek 的第六个源与"按链接抓取"。两者**默认关**（成本 + query 出境，
            # 理由见 `external.DEEPSEEK_SEARCH_ENV`），这里置 0 让每条既有生成用例都
            # 停在"六源里的五个"——也就是 `3c40ba6` 的形状。要它们的用例自己
            # `setenv(..., "1")`，并同时把 `_client` 换成假响应。
            "MINDMAP_EXTERNAL_DEEPSEEK_SEARCH",
            "MINDMAP_EXTERNAL_DEEPSEEK_FETCH",
            # 画像加权（`prefs/profile.SWITCH_ENV`）。**置 0**：它默认关（打开之后
            # 每条命中要多一次 embedding，见 `profile.SWITCH_ENV`），而这里的用例
            # 全部走 `no_real_http`——清空会让"默认开"那一侧的用例真的去 embed。
            # 需要它的用例自己 `setenv(..., "1")` 并替换 `embed_texts`。
            "MINDMAP_PROFILE_RERANK",
            # 第三档骨架（`tools.GRAPH_OUTLINE_ENV`）。它线上默认**开**——补的是
            # "材料抽不出章节标题"那个缺口。这里置 0 而不是清空，理由与
            # `MINDMAP_EXTERNAL_KNOWLEDGE` 完全相同：清空 = 默认开 = 每条"无标题
            # 材料"的生成用例都会去问一次图存储，而宿主机上很可能正跑着一个 Neo4j
            # （`NEO4J` 那组只清连接参数，挡不住用例自己 `setenv` 进去之后的那批）。
            # 需要它的用例自己 `setenv(..., "1")`；"默认开"由
            # `tests/test_graph_outline.py` 的一条专门用例（`delenv` + 直接问
            # `graph_outline_enabled()`）钉着。
            "MINDMAP_GRAPH_OUTLINE")

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
def full_text_material_off(monkeypatch):
    """把"装得下的材料直接塞进 prompt"这条路**置关**（`FULL_TEXT_MATERIAL_CHARS=0`）。

    与 `SWITCHES` 里那几项同一个理由：它线上默认是**开**（5 万字，依据见
    `tools.FULL_TEXT_MATERIAL_CHARS` 的注释），而绝大多数既有用例钉的是"带材料 →
    按支检索"那条路——它们的模型替身（`test_one_path._tree_chat`）就是按 user 里的
    "第 N 支「标题」的检索结果"分组造引用的，全塞一开、那些分组不在 user 里了，
    引用自然造不出来，断言会以"引用被剔掉了"的形式失败，而真正的原因是这次请求
    走了另一条路。

    需要它的用例自己 `monkeypatch.setattr(tools, "FULL_TEXT_MATERIAL_CHARS", ...)`
    （见 `tests/test_full_text_material.py`）；"默认值是 50_000"这件事由那个文件里
    的一条专门用例钉着——放这里等于把产品口径藏进测试配置。
    """
    from app.agent import tools

    monkeypatch.setattr(tools, "FULL_TEXT_MATERIAL_CHARS", 0)
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
