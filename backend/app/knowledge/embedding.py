"""文本向量（`text-embedding-v3`）：混合检索的语义一路。

流程位置：
    search_sources() ──embed_texts()──> list[list[float] | None] ──> 余弦/RRF

**两套协议**，因为同一个模型挂在不同网关下：

  - `openai`（默认）：阿里云 MaaS 工作空间的 OpenAI 兼容端点。
    `POST {MINDMAP_EMBEDDING_BASE_URL}/embeddings`，
    请求 `{"model":..., "input": ["文本", ...]}`，响应 `data[i].embedding`。
    实测响应：`{"data":[{"embedding":[...1024 floats...],"index":0}], "usage":{...}}`
  - `dashscope`：DashScope 原生端点
    `/api/v1/services/embeddings/text-embedding/text-embedding`，
    请求 `{"model":..., "input": {"texts": [...]}, "parameters": {"dimension":..., "output_type":"dense"}}`，
    响应 `output.embeddings[i].embedding`。留这条分支是为了万一要换回公共域名。

**base 由环境变量给全，代码里不留默认域名**：MaaS 端点带工作空间 ID，硬编码
一个就等于把某个人账号的地址写进仓库。没给 base 且协议是 openai 时视为
**未配置**，检索直接降级为纯 BM25——这比连一个猜出来的域名更诚实。

**为什么单独成文件、也不复用 `AsrError`**：两者都调阿里云、都要处理缺凭证，
但失败语义正好相反。语音转写失败是**用户请求失败**——没有文本就没有 Source，
必须 422 让用户知道。向量失败只是**检索质量降级**——BM25 照样能给出结果。
异常层级分开，调用方才能只对后者做降级，而不会顺手把前者的错误吞成一次
静默的"没找到"。
"""
from __future__ import annotations

import logging
import math
import os
import time

import httpx

logger = logging.getLogger(__name__)

DASHSCOPE_ENDPOINT = ("https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
                      "text-embedding/text-embedding")
DEFAULT_MODEL = "text-embedding-v3"
DEFAULT_DIM = 1024
DEFAULT_PROTOCOL = "openai"

# key 的读取顺序：先专用变量，再回退到语音转写共用的那个（同一个百炼账号）。
KEY_ENVS = ("MINDMAP_EMBEDDING_API_KEY", "DASHSCOPE_API_KEY")
BASE_URL_ENV = "MINDMAP_EMBEDDING_BASE_URL"
PROTOCOL_ENV = "MINDMAP_EMBEDDING_PROTOCOL"

# 单次请求最多 10 条。**这个数字是实测出来的，不是按文档取的**：MaaS 网关在 12 条
# 就开始回 400 `batch size is invalid`，8 与 10 通过。DashScope 文档写的是 25，
# 但同一个模型换到工作空间端点后上限不同——按文档取会整批失败，而整批失败在这个
# 模块里等于"向量路全废、静默退化成纯 BM25"，日志里只有一行 warning。
MAX_BATCH = 10
# 单条文本的安全长度（码点）。v3 的上下文是 8192 token，中文按 1 token ≈ 1.2–1.5
# 字估算，6000 码点仍留有余量。切块上限 700 码点，正常路径碰不到这里。
MAX_CHARS = 6000

# 连接 10s / 读 60s。embedding 是同步短请求（25 条文本通常 < 1s），读超时给到
# 60s 只是为了不让偶发排队把一次导图生成的工具调用拖到用户以为卡死。
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 60.0

# 只对 5xx 与超时重试。4xx 是参数或凭证问题，重试只会重复烧额度、把失败延迟
# 三倍；而 5xx/超时是瞬时故障，重试两次的收益明显。
MAX_RETRIES = 2
RETRY_BACKOFF = 0.5


class EmbeddingError(Exception):
    """向量服务调用失败。调用方据此把检索降级为纯 BM25，不向用户报错。"""


class EmbeddingNotConfigured(EmbeddingError):
    """缺 key 或（openai 协议下）缺 base_url。

    这是**预期状态**而不是故障：本机开发、CI、没配凭证的部署都会走到这里。
    调用方应当静默降级，不要记 warning——把"还没配"当异常刷日志，真正的故障
    反而被淹掉。
    """


def protocol() -> str:
    """`openai` | `dashscope`。无法识别的值退回默认，别让一个拼错的环境变量
    把检索悄悄关掉。"""
    value = os.getenv(PROTOCOL_ENV, "").strip().lower()
    return value if value in {"openai", "dashscope"} else DEFAULT_PROTOCOL


def base_url() -> str:
    return os.getenv(BASE_URL_ENV, "").strip().rstrip("/")


def model_name() -> str:
    return os.getenv("MINDMAP_EMBEDDING_MODEL", "").strip() or DEFAULT_MODEL


def dimension() -> int:
    raw = os.getenv("MINDMAP_EMBEDDING_DIM", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_DIM
    return value if value > 0 else DEFAULT_DIM


def api_key() -> str:
    for name in KEY_ENVS:
        key = os.getenv(name, "").strip()
        if key:
            return key
    raise EmbeddingNotConfigured(
        "向量检索未配置：缺少 MINDMAP_EMBEDDING_API_KEY（或回退的 "
        "DASHSCOPE_API_KEY）；缺凭证时检索自动降级为纯 BM25，不影响功能")


def configured() -> bool:
    """能否走向量路。调用方先用它预检，省掉一次必然失败的往返。"""
    if not any(os.getenv(name, "").strip() for name in KEY_ENVS):
        return False
    # dashscope 协议有固定的公共端点，不需要 base；openai 协议没有 base 就无处可发
    return protocol() == "dashscope" or bool(base_url())


def endpoint() -> str:
    """完整 URL。openai 协议只做 `base + /embeddings`，不猜路径。"""
    if protocol() == "dashscope":
        return DASHSCOPE_ENDPOINT
    return f"{base_url()}/embeddings"


def _clip(text: str) -> tuple[str, bool]:
    """超长截断。返回 (文本, 是否截断过)。"""
    if len(text) <= MAX_CHARS:
        return text, False
    return text[:MAX_CHARS], True


def _payload(texts: list[str], model: str, dim: int) -> dict:
    if protocol() == "dashscope":
        return {
            "model": model,
            "input": {"texts": texts},
            "parameters": {"dimension": dim, "output_type": "dense"},
        }
    # OpenAI 兼容协议：input 是字符串数组。**不发 dimensions**——MaaS 网关对
    # 该字段的支持未验证，为一个没确认的参数换来 400 不划算。维度退回由模型
    # 默认值（1024）决定，`dimension()` 只用于缓存校验与日志。
    return {"model": model, "input": texts}


def _detail(response: httpx.Response) -> str:
    """错误详情：只取机器可读的 code 与截断后的 message。

    两种错误体都要认：OpenAI 兼容是 `{"error": {"message": ..., "type": ...}}`，
    DashScope 原生是 `{"code": ..., "message": ...}`。
    不记响应正文——错误响应有时会把输入片段回显在 message 里，而材料可能敏感。
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or error.get("type") or "").strip()
        message = str(error.get("message") or "").strip()[:80]
    else:
        code = str(body.get("code") or "").strip()
        message = str(body.get("message") or "").strip()[:80]
    return " ".join(part for part in (code, message) if part)


def _post(url: str, payload: dict, headers: dict[str, str]) -> dict:
    """发一批。只对 5xx 与超时重试，最多 `MAX_RETRIES` 次，指数退避。"""
    last = "未知错误"
    for attempt in range(MAX_RETRIES + 1):
        try:
            with httpx.Client(
                timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
            ) as client:
                response = client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            last = f"超时 {type(exc).__name__}"
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"请求失败：{type(exc).__name__}") from exc
        else:
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise EmbeddingError("响应不是合法 JSON") from exc
            detail = _detail(response)
            if response.status_code < 500:
                raise EmbeddingError(f"HTTP {response.status_code}：{detail}")
            last = f"HTTP {response.status_code} {detail}".strip()
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF * (2 ** attempt))
    raise EmbeddingError(last)


def _parse(body: dict, size: int) -> list[list[float] | None]:
    """把响应对回输入顺序，缺项留 None。

    索引字段两家不一样：OpenAI 兼容是 `data[i].index`，DashScope 原生是
    `output.embeddings[i].text_index`。服务端没给索引时退回枚举顺序——漏一条
    比整批失败好，调用方按块处理 None，代价只是那几块走不了向量路。
    """
    if protocol() == "dashscope":
        items = (body.get("output") or {}).get("embeddings") or []
        index_key = "text_index"
    else:
        items = body.get("data") or []
        index_key = "index"
    parsed: list[list[float] | None] = [None] * size
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get(index_key, position))
        except (TypeError, ValueError):
            continue
        vector = item.get("embedding")
        if 0 <= index < size and isinstance(vector, list) and vector:
            parsed[index] = [float(value) for value in vector]
    return parsed


def _normalize(vector: list[float]) -> list[float]:
    """L2 归一化。归一到单位长度后，余弦相似度就是点积，省掉每次检索的开方。"""
    norm = math.sqrt(sum(value * value for value in vector))
    return vector if norm <= 0 else [value / norm for value in vector]


def embed_texts(texts: list[str]) -> list[list[float] | None]:
    """批量取向量，返回与输入**等长**的列表，失败的条目为 None。

    失败分三档，调用方看到的行为不同：
      - 未配置（缺 key，或 openai 协议缺 base）：抛 `EmbeddingNotConfigured`
      - 全部批次都失败：抛 `EmbeddingError`（调用方记一条 warning 后降级）
      - 部分批次或部分条目失败：正常返回，失败处为 None。这时成功的块仍然
        参与语义路，只是候选少几条——比整条路作废对结果更有利。

    返回的向量已做 L2 归一化。
    """
    if not texts:
        return []
    key = api_key()
    model = model_name()
    dim = dimension()
    url = endpoint()
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    vectors: list[list[float] | None] = []
    batches = 0
    failures = 0
    clipped = 0
    for start in range(0, len(texts), MAX_BATCH):
        window = texts[start:start + MAX_BATCH]
        batches += 1
        prepared: list[str] = []
        for text in window:
            body_text, was_clipped = _clip(text)
            clipped += 1 if was_clipped else 0
            prepared.append(body_text)
        try:
            body = _post(url, _payload(prepared, model, dim), headers)
        except EmbeddingError as exc:
            failures += 1
            # 只记批号/条数/错误类型。块文本是材料原文，不进日志。
            logger.warning("向量批失败：第 %d 批 / %d 条 / %s",
                           batches, len(window), exc)
            vectors.extend([None] * len(window))
            continue
        vectors.extend(_parse(body, len(window)))

    if clipped:
        logger.warning("向量输入有 %d 条超长，已截断到 %d 码点", clipped, MAX_CHARS)
    if failures == batches:
        raise EmbeddingError(f"全部 {batches} 批向量请求失败")
    return [_normalize(vector) if vector else None for vector in vectors]
