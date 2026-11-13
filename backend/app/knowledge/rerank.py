"""召回后的重排（reranker）：在**已融合、已去重**的候选池里重新排序。

流程位置（`service.search_parent` / `service.search_multi`）：

    打分融合 → 折父块/多路累加 → 去重 → 【重排】 → 截断 top_k
                                        ↑ 本模块
    扩池发生在上游：开关开着**且凭证齐备**时，上游把候选池放宽到
    `pool_size(top_k)` 条，本模块从里面挑 `top_k`。

为什么必须先扩池：`top_k=3` 时上游只给 3 条，"重排"能做的只有换顺序；而 R@1
想涨，靠的正是"把原本排第 5 的正确片段抬到第 1"——池子里没有第 5 条，
重排就无从谈起。所以 `RERANK_POOL = max(top_k * 4, 10)`。

三条降级约定（都返回**原顺序的前 `top_k` 条**，不抛错、不返回空）：
  - 未配置：`MINDMAP_EMBEDDING_API_KEY` 与 `DASHSCOPE_API_KEY` 都为空；
  - 请求失败：超时（8 秒，**不重试**）/ 5xx / 4xx / 连接错误；
  - 解析失败：响应不是合法 JSON、缺 `results`、条目缺 `index` 或
    `relevance_score`、索引越界，或返回条数与输入**不等**。最后这条是刻意的：
    少给的候选没有分数，按 `-inf` 塞到最后等于用一个没发生过的判断去决定名次，
    整体退回原顺序更诚实。

## 使用前提：收益随 query 与文档的字面重合度上升而**下降**，甚至变负

这是这个模块最重要的一条事实，比任何参数都重要。同一个开关、同一份材料，两套
测试集的读数方向相反：

| 测试集 | query 与目标区间的词面重合 | `R@1` | `MRR@3` |
|---|---|---:|---:|
| manual 套（28 条手写真实问法） | 均值 **31.4%** | 67.9% → **82.1%**（+14.2pp） | 78.0% → 86.9% |
| title 套（章节标题原文） | **100%** | 100.0% → **96.6%**（−3.4pp） | 100.0% → 98.3% |

机制说得通，不是噪声：**字面几乎完全重合时，BM25 已经是最优排序**——"标题就在
该段第一行"这种极端情形下，没有任何语义信号比字符串本身更准。此时 reranker 按
语义重排，会把"字面最像"的那条挤下去，净效果是掉点。反过来，manual 套那 31.4%
的重合度正是字面匹配不够、语义能力有活可干的地方，于是 +14.2pp。

所以这个默认值（`MINDMAP_RERANK=1`）的前提是**检索服务于自然语言问法**——本仓库
的 `rag_query` 就是这种形态（用户口语提问，`docs/RETRIEVAL-EVAL.md` 第 7 节量的
偏差也是同一件事）。如果将来有调用方拿 `search_parent` / `search_multi` 去做
"标题定位""关键词跳到某章"这类字面检索，应当在那里显式关掉重排：那里开着它
是负收益，而且是**静默的**负收益（结果照样返回，只是顺序变差）。

这也是"不能只看 title 套"的第二个证据：第一个是标题当 query 把 `R@1` 顶到 100%、
至少高估 20.7pp（第 7 节的阶梯实验），第二个是**它连改善方向都会给反**。

另一条实测，用来挡住一个容易走偏的归因：**粗颗粒本身没有让分数塌缩**。本地一个
117M 的 cross-encoder（`mmarco-mMiniLMv2-L12-H384`）在 34–40 条父块上的极差中位
**0.4551**、top1−top2 间隔中位 **0.2607**，比 `qwen3-rerank` 的 0.26 跨度还宽——
450–700 字的块没有把分布压扁。但它那套打分的净收益是 `R@1` **0.0pp**、`MRR@3`
**−1.2pp**。所以"用哪个模型"的量级远大于"父块多粗"，别把模型选型失败写成颗粒度
问题。真正要留意的是另一类：27 次调用里 **8 次**整批候选被判"都不相关"
（`max` ≤ 0.02、标准差 < 0.05），此时相邻差值只有 0.001 量级，重排等于按噪声
排序——它不会凭空把无关项排到前面，但会消耗掉原本靠 BM25 名次偶然保住的那一条。

## 分数落在哪个字段：新增 `rerank_score`，**不覆盖** `score`

  1. `score` 的口径是"同一次检索内的融合分"（RRF / BM25 / 多路累加），而
     `search_multi` 与 `_merge_batches` 都明写着**分数不单调**（保底段在前）。
     把 rerank 的 [0,1] 概率灌进同一个字段，会让"score 越大越靠前"这个从未
     成立的假设看起来成立了，于是下游任何一次 `sorted(hits, key=score)` 都会
     把重排**静默撤销**。
  2. 保留 `score` 之后，`rerank_score is not None` 就是"这条被打过分"的唯一
     标记。误用的表现是"重排没生效"（可观测、可复现），而不是"名次莫名其妙"。

`EvidenceHit` 的区间字段（`start`/`end`/`quote`/`leaf_start`/`leaf_end`）
**一个都不动**：重排只改顺序与标注，不改证据。

## 端点与请求体形状

原生 DashScope（本模块只实现这一种）：:

    POST {root}/api/v1/services/rerank/text-rerank/text-rerank
    Authorization: Bearer <key>
    {"model": "qwen3-rerank",
     "input": {"query": "...", "documents": ["...", "..."]},
     "parameters": {"return_documents": false, "top_n": <len(documents)>}}

    → {"output": {"results": [{"index": 0, "relevance_score": 0.798}, ...]},
       "usage": {...}, "request_id": "..."}

**实测状态分两段，别混着读**：

  - 配好 MaaS 凭证的机器上，这份代码（含下面 `_root` 的推导）打
    `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/rerank/
    text-rerank/text-rerank` **返回 200**，响应就是上面那个形状（`output.results`
    里每项 `index` + `relevance_score`，按分数降序）。这一条是端到端的。
  - 本机**没有**凭证（`MINDMAP_EMBEDDING_API_KEY` / `DASHSCOPE_API_KEY` 都是空的），
    只能验到鉴权层：同一路径返回 **401 `{"code":"InvalidApiKey"}`**。这条 401 证明
    路径存在、请求体被接受（路径错是 404，字段错是 400），但成功响应本身不是在本机
    看到的。响应形状取自百炼官方文档「通用文本排序模型API使用详情」的成功响应示例，
    字段名与示例逐字一致。

`compatible-mode` 那条路走不通，不需要试第二遍：
  - 官方文档明写 `gte-rerank-v2` **不支持** OpenAI 兼容接口，调用会返回
    `model_not_supported`；兼容接口只有 `qwen3-rerank` / `qwen3.7-text-rerank`，
    且路径是 `{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-api/v1/reranks`。
  - 本机实测 `https://dashscope.aliyuncs.com/compatible-mode/v1/reranks` → **404**；
    `https://dashscope.aliyuncs.com/compatible-api/v1/reranks` → 401（存在，但只
    服务 qwen3 系列）。

所以这里**不做兼容分支**：原生接口对两个模型都可用，而兼容接口对 `gte-rerank-v2`
必然 400。写两条路只会在两个地方维护同一件事。

## base 从哪来

沿用 `MINDMAP_EMBEDDING_BASE_URL`（同一个 MaaS 工作空间）。它给的是 OpenAI
兼容 base，形如 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`
或 `.../api/v1`；rerank 的路由挂在**站点根**下，所以先剥掉尾部 `/v1` 再拼。

与 `embedding.py`「代码里不留默认域名」的做法不同：这里的公共域名
`https://dashscope.aliyuncs.com` 不带任何工作空间 ID，不是谁的账号地址，
留作兜底不会泄露什么；而 embedding 没有这个兜底，是因为 MaaS 的 embeddings
路由只存在于工作空间域下。

两个候选按顺序试，**第一次请求就确定用哪个**：
  - `MINDMAP_EMBEDDING_BASE_URL` 推导出来的地址先试；
  - "不成"只认 **404 / 路径类错误码**（`model_not_found`、`not_found`、
    `URL not found` 一类）。这类错误说明"这个网关没有这条路由"，换域名有意义。
    超时与 5xx 不换（瞬时故障，换域名只是把一次失败变成两次）；401/403 不换
    （同一个 key，换域名必然同样 401）。
  - 换过一次的端点记进进程级 `_UNUSABLE`，之后的请求不再打它——否则每次检索
    都要为一个已知不存在的路由多付一次往返。

## 日志

只记条数、耗时、降级原因类别。**query 与片段内容一律不进日志**：它们就是材料
原文，与 `embedding.py` 里同一条纪律。
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

import httpx

from app.knowledge.embedding import BASE_URL_ENV, KEY_ENVS

if TYPE_CHECKING:                      # 只为类型标注；运行时不导入，避免环形依赖
    from app.knowledge.service import EvidenceHit

logger = logging.getLogger(__name__)

# `MINDMAP_RERANK` 默认 **1**（开）。
#
# 这个默认值是被**评估**翻过来的，不是拍的：manual 套（28 条手写真实问法，
# 纯 BM25 + 父块）`R@1` 从 67.9% 涨到 82.1%（+14.2pp）、`MRR@3` +8.9pp、
# `COV@10` +7.8pp，`R@3` 持平。同一批读数在 `docs/RETRIEVAL-EVAL.md` 的
# manual 套那节。
#
# **但这个 1 有前提**：检索面向自然语言问法。同样的开关在 title 套（query 就是
# 章节标题，字面重合 100%）上 `R@1` 掉 3.4pp——BM25 在字面几乎完全重合时已经是
# 最优排序，重排反而把"字面最像"的挤下去。详见模块 docstring 的
# 「使用前提：收益随字面重合度上升而下降」。字面检索场景请在调用方关掉它。
#
# 代价是每次检索多一次 HTTP 调用（8 秒超时、不重试、失败即降级）。实测
# `qwen3-rerank` 打 600 字候选：12 条中位 587ms、23 条中位 883ms——在 5 秒检索
# 预算里占 12%–18%。
#
# 翻默认值之前确认过两件事，缺一不可：
#   - 关着时与接入前**逐字段一致**（`tests/test_rerank.py` 拿 `c38684c` 的旧模块钉着，
#     连"不构造 HTTP 客户端"都在断言里）；
#   - 没凭证时不产生任何额外动作：`configured()` 只读环境变量，`_rerank_depth()`
#     因此在缺 key 时返回 `top_k`，既不扩池也不发请求——扩池会改变 RRF 名次映射，
#     白扩一次就等于让同一批命中的 `score` 悄悄变一个数。
RERANK_ENV = "MINDMAP_RERANK"
MODEL_ENV = "MINDMAP_RERANK_MODEL"

# 默认模型。`qwen3-rerank`，**选它的唯一理由是区分度**：同一批候选（相关 /
# 部分相关 / 无关）在两个模型上的实测分数是
#
#     qwen3-rerank    0.798 / 0.379 / 0.157     ← 三档拉得开
#     gte-rerank-v2   0.357 / 0.141 / 0.006     ← 全挤在低分区，极差 0.35 但间距小
#
# 120 条候选里"该排第一的那条"与"排第二的那条"常常只差一点语义，`gte-rerank-v2`
# 在这种贴近的间距上给出的差值更小（例子里 0.141→0.006 看着像拉得开，但那是
# "完全无关" vs "部分相关"；真候选中相邻名次的差值普遍 < 0.02）。分数挤在一起
# 时，重排的稳定性全靠排序的稳定排序兜底，收益会明显缩水。
#
# `gte-rerank-v2` 仍可选（`MINDMAP_RERANK_MODEL=gte-rerank-v2`）：它支持 50 余
# 语种、单条 4000 token，在长文档场景更稳；只是在本项目的父块（450–700 字）上
# 区分度不如 qwen3。`gte-rerank`（不带 v2）官方已公告 2026-05-30 下线，且实测
# 该账号 403 无权限，不作为默认。
DEFAULT_MODEL = "qwen3-rerank"

# 候选池：`max(top_k * 4, 10)`。取 4 倍是因为评估里 `top_k` 常取 3–10，而
# "该被抬到第一的那条"实测常落在原第 4–8 名；下限 10 是给 `top_k=1/3` 这种
# 小截断留的余量——池子小于 10 条时重排的可用信号太少。
POOL_FACTOR = 4
MIN_POOL = 10

# 8 秒、不重试。这是**整次重排**的预算：2C2G 上检索全部预算约 5 秒，重排只是
# 其中一段，超过 8 秒的等待已经比"用原顺序"更伤。不重试的理由与 embedding
# 相反的那一半一致：rerank 在关键路径上，重试会把延迟乘以 2–3 倍，而它的
# 失败语义本来就是"降级为原顺序"——降级是免费的，重试不是。
TIMEOUT = 8.0

RERANK_PATH = "/api/v1/services/rerank/text-rerank/text-rerank"
# 公共域名兜底。见模块 docstring「base 从哪来」。
PUBLIC_ROOT = "https://dashscope.aliyuncs.com"

# OpenAI 兼容 base 的尾部，剥离顺序**从长到短**（短的都以 `/v1` 结尾）。
_BASE_SUFFIXES = ("/compatible-mode/v1", "/compatible-api/v1", "/api/v1", "/v1")

# 判定"这个端点没有这条路由"的错误码片段（小写）。见模块 docstring。
_ROUTE_MISSING_MARKERS = ("not_found", "not found", "not supported", "no such")

# 进程级：实测过"没有这条路由"的端点。跨请求复用，避免每次检索都白打一次。
_UNUSABLE: set[str] = set()


class RerankError(Exception):
    """重排调用失败。调用方据此退回原顺序，不向用户报错。"""


class RerankNotConfigured(RerankError):
    """缺 key。

    与 `EmbeddingNotConfigured` 同一种性质：**预期状态**而不是故障。本机开发、
    CI、没配凭证的部署都会走到这里，所以不记 warning，静默降级。
    """


# ---------------------------------------------------------------- 配置


def enabled() -> bool:
    """`MINDMAP_RERANK` 是否打开。默认开；`0/off/false/no/空串` 一律关。

    正列举真值而不是"不在假值集合里"：默认开时两种写法等价，而正列举让"改默认值"
    与"判定逻辑"彻底解耦——将来若因某个模型不可用再翻回关，只改上面那个字符串。
    与 `service._parent_retrieval_enabled` 同一种写法和同一个理由。
    """
    raw = os.getenv(RERANK_ENV, "1").strip().lower()
    return raw in {"1", "on", "true", "yes"}


def model_name() -> str:
    return os.getenv(MODEL_ENV, "").strip() or DEFAULT_MODEL


def api_key() -> str:
    """key 的读取顺序与 embedding 完全一致（同一个百炼账号）。"""
    for name in KEY_ENVS:
        key = os.getenv(name, "").strip()
        if key:
            return key
    raise RerankNotConfigured(
        "重排未配置：缺少 MINDMAP_EMBEDDING_API_KEY（或回退的 DASHSCOPE_API_KEY）；"
        "缺凭证时重排自动降级为原顺序，不影响召回")


def configured() -> bool:
    """能否真发一次重排请求。

    调用方先用它预检：**没配凭证时连候选池都不扩**。这一条是"开关关着与开着
    逐字节一致"的另一半保障——扩池本身会改变 RRF 的名次映射，白扩池等于在用
    一个没人用的候选集算分数。
    """
    return any(os.getenv(name, "").strip() for name in KEY_ENVS)


def pool_size(top_k: int) -> int:
    """候选池大小：`max(top_k * 4, 10)`。`top_k` 非正时返回 0。"""
    if top_k <= 0:
        return 0
    return max(top_k * POOL_FACTOR, MIN_POOL)


def _root(base: str) -> str:
    """剥掉 OpenAI 兼容 base 的尾部，得到站点根。剥不掉就原样返回。"""
    trimmed = base.rstrip("/")
    for suffix in _BASE_SUFFIXES:
        if trimmed.endswith(suffix):
            return trimmed[: -len(suffix)]
    return trimmed


def endpoints() -> list[str]:
    """候选端点，按尝试顺序去重。

    `MINDMAP_EMBEDDING_BASE_URL` 有值时它在前，公共域名在后；没值时只有公共域名。
    已判定"没有这条路"的端点直接剔除（见 `_UNUSABLE`）。
    """
    candidates: list[str] = []
    base = os.getenv(BASE_URL_ENV, "").strip().rstrip("/")
    if base:
        derived = _root(base)
        # base 本身已经是站点根（没有 /v1 尾巴）时 `_root` 返回原值，仍然成立
        candidates.append(f"{derived}{RERANK_PATH}")
    candidates.append(f"{PUBLIC_ROOT}{RERANK_PATH}")
    ordered = list(dict.fromkeys(candidates))
    usable = [url for url in ordered if url not in _UNUSABLE]
    # 全被判定死掉了也要给一个（最后一个），否则调用方拿到空列表无法区分
    # "没得试"与"不该试"——那两种情况的降级日志完全不同。
    return usable or ordered[-1:]


# ---------------------------------------------------------------- 请求


def _payload(query: str, documents: list[str], model: str) -> dict:
    """原生请求体。字段名与官方文档示例逐字一致。

    `top_n` 传 `len(documents)`：要的是**全部候选的分数**——重排要自己取前
    `top_k`，并且评估要看分数分布；让服务端先砍一批，等于把这个决策交出去。
    `return_documents=false`：原文我们手上就有（`hit.quote`），传回来只是多付
    一次上行流量。
    """
    return {
        "model": model,
        "input": {"query": query, "documents": documents},
        "parameters": {"return_documents": False, "top_n": len(documents)},
    }


def _detail(response: httpx.Response) -> tuple[str, str]:
    """错误详情：`(code, message)`，都截断。

    两种错误体都认：原生是 `{"code": ..., "message": ...}`，兼容接口是
    `{"error": {"message": ...}}`。不记响应正文——错误响应有时会回显输入片段。
    """
    try:
        body = response.json()
    except ValueError:
        return "", ""
    if not isinstance(body, dict):
        return "", ""
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or error.get("type") or "").strip(), \
            str(error.get("message") or "").strip()[:80]
    return str(body.get("code") or "").strip(), str(body.get("message") or "").strip()[:80]


def _post(url: str, payload: dict, key: str) -> dict:
    """发一次，**不重试**。非 200 一律抛 `RerankError`，把分类留给调用方。"""
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=httpx.Timeout(TIMEOUT)) as client:
            response = client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise RerankError(f"超时 {TIMEOUT:.0f}s（{type(exc).__name__}）") from exc
    except httpx.HTTPError as exc:
        raise RerankError(f"请求失败：{type(exc).__name__}") from exc

    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError as exc:
            raise RerankError("响应不是合法 JSON") from exc
        if not isinstance(body, dict):
            raise RerankError("响应不是 JSON 对象")
        return body

    code, message = _detail(response)
    lowered = f"{code} {message}".lower()
    missing = (response.status_code == 404
               or any(marker in lowered for marker in _ROUTE_MISSING_MARKERS))
    error = RerankError(f"HTTP {response.status_code}：{code} {message}".strip())
    # 挂一个属性而不是单开子类：调用方只需要"能不能换域名"这一个布尔。
    error.route_missing = missing        # type: ignore[attr-defined]
    raise error


def _scores(body: dict, size: int) -> list[float]:
    """把响应对回输入顺序，返回与输入**等长**的分数列表。

    缺 `output` 也认扁平 `results`（兼容接口的形状），虽然当前只走原生路——
    解析这一层的宽容度不花钱，而端点将来若换成 qwen3-rerank，这里不用动。

    严格到底：条数不等、索引越界、字段缺失都判失败。理由是这种半截数据会
    静默产出"一半候选有分数、一半没有"的排序，比整批降级难查得多。
    """
    container = body.get("output")
    results = container.get("results") if isinstance(container, dict) else None
    if not isinstance(results, list):
        results = body.get("results")
    if not isinstance(results, list) or not results:
        raise RerankError("响应缺少 results")
    if len(results) != size:
        raise RerankError(f"结果条数不符：{len(results)} != {size}")

    parsed: list[float | None] = [None] * size
    for item in results:
        if not isinstance(item, dict):
            raise RerankError("结果条目不是对象")
        try:
            index = int(item["index"])
            value = float(item["relevance_score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RerankError(f"结果条目字段非法：{exc}") from exc
        if not 0 <= index < size or parsed[index] is not None:
            raise RerankError(f"结果索引越界或重复：{index}")
        parsed[index] = value
    if any(value is None for value in parsed):
        raise RerankError("结果缺少部分候选的分数")
    return [value for value in parsed if value is not None]


def score_documents(query: str, documents: list[str]) -> list[float]:
    """给一批文本打重排分，返回与输入等长的列表（**不排序**）。

    单独暴露出来是给评估脚本用的：它要的是分数分布，不是排好序的 hits。
    失败时抛 `RerankError`（含 `RerankNotConfigured`），由调用方决定降级。
    """
    if not documents:
        return []
    key = api_key()
    payload = _payload(query, documents, model_name())

    last: RerankError | None = None
    for url in endpoints():
        try:
            body = _post(url, payload, key)
        except RerankError as exc:
            last = exc
            if getattr(exc, "route_missing", False) and len(endpoints()) > 1:
                # 这个网关没有 rerank 路由：记下来，换下一个候选再试**一次**。
                _UNUSABLE.add(url)
                continue
            raise
        return _scores(body, len(documents))
    raise last or RerankError("没有可用端点")


# ---------------------------------------------------------------- 对外入口


def rerank(query: str, hits: list[EvidenceHit], top_k: int) -> list[EvidenceHit]:
    """按重排分数重排 `hits`，返回前 `top_k` 条。

    三条不调用的短路（都是有意的，不是优化）：
      - `top_k <= 0` 或 `hits` 为空：没什么可挑；
      - `len(hits) <= top_k`：池子不比要的条数多，重排只能换顺序不能换人选，
        而"换顺序"在候选不足时唯一的效果是把一条确定无关的排到前面；
      - 未配置：`api_key()` 会抛 `RerankNotConfigured`，同样走"原顺序前 top_k"。

    返回的每条都带 `rerank_score`（0–1），`score` 与所有区间字段原样保留。
    排序用 Python 的稳定排序，同分保持上游顺序（上游顺序本身带 RRF 名次信息）。
    """
    if top_k <= 0 or not hits:
        return []
    if len(hits) <= top_k:
        return hits[:top_k]

    started = time.perf_counter()
    try:
        scores = score_documents(query, [hit.quote for hit in hits])
    except RerankNotConfigured:
        # 预期状态：静默降级，一个字都不记（与 embedding 同一条纪律）。
        return hits[:top_k]
    except RerankError as exc:
        logger.warning("重排降级为原顺序：候选 %d / %s", len(hits), exc)
        return hits[:top_k]

    scored = sorted(zip(hits, scores), key=lambda pair: -pair[1])
    picked = [hit.model_copy(update={"rerank_score": round(value, 6)})
              for hit, value in scored[:top_k]]
    # 只记条数与耗时。query 与片段是材料原文，一律不进日志。
    logger.info("重排：候选 %d → %d 条 / 耗时 %.0fms",
                len(hits), len(picked), (time.perf_counter() - started) * 1000)
    return picked
