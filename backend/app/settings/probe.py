"""「测试连接」那一次最小请求：发一条最短的 chat，只看它通不通。

**为什么值得一个接口**：三层配置（地址、密钥、模型名）里任何一层错了，症状在
生成侧都是同一种——"模型服务出错了"，而那要等到一次几十秒的生成跑完、烧掉一次
调用才看得到，且看不出错在哪一层。这里用 `max_tokens=1` 发一次请求，把三层各自
的判据分开：连不上 = 地址；401/403 = 密钥；404 = 地址路径或模型名；其余按状态码
带一句原话。

**它不写库、不改任何状态**：面板上的流程是"填 → 测 → 存"，测试用面板里**当前
输入框**的值（`router` 那层把空字段填成已生效值），所以"我还没存但想先验一下"
是走得通的。

**密钥不进日志、不进错误文案**：上游的 401 正文常常带一段 key（OpenAI 系列会写
`Incorrect API key provided: sk-abc***`）。`_redact()` 把出现的明文 key 换成打码值，
再把正文截到 200 字——既留下能查的信息，又不把密钥写进响应体（界面会显示它，
用户一截图就出去了）。
"""
from __future__ import annotations

import ipaddress
import json
import os
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

#: 测试请求的超时。比生成侧（600 秒）短得多：这里的目的是"通不通"，等 15 秒还
#: 没有回包，用户已经可以判断是地址或网络的问题了。改这个数记得同步
#: `_message_for_timeout()` 里那句话（它把秒数念给用户）。
TIMEOUT_SECONDS = 15.0

#: `max_tokens=1`：只要能拿到一个 200 就证明三层都对。多花一个 token 没有任何
#: 额外信息，而推理模型的 thinking 段也会被这个上限压住。
PROBE_MAX_TOKENS = 1

#: 错误正文最多回多少字。够看清"model not found"这类原话，又不至于把一页 HTML
#: 错误页塞进界面。
_SNIPPET_CHARS = 200


@dataclass(frozen=True)
class ProbeResult:
    """一次测试连接的结论。`message` 是直接给用户看的中文。"""

    ok: bool
    message: str
    model: str
    status: int | None = None
    latency_ms: int = 0


#: 允许把模型地址指向内网 / 本机。**默认关**。
#:
#: 默认关的理由不是"内网不安全"，是这条路的代价不对称：放行一个内网地址，能让
#: 任何碰到这个接口的人拿**已存的密钥**当探针去扫内网（`http://redis:6379/`、
#: `http://169.254.169.254/…`），而密钥是明文塞在 `Authorization` 头里的。反过来，
#: 拒绝内网只是让"用本机 Ollama 当模型"这一类部署需要显式开一下开关。
#:
#: 需要它的部署（Ollama / LM Studio / 同机自建端点）在环境里设
#: `MINDMAP_SETTINGS_ALLOW_PRIVATE=1`。注意它**同时放开本机与内网**：没有更细的
#: 粒度是有意的——真要连本机模型的人，通常也要连宿主机的 `host.docker.internal`
#: 或局域网里的另一台机器，两档分不开就没必要假装分得开。
ALLOW_PRIVATE_ENV = "MINDMAP_SETTINGS_ALLOW_PRIVATE"


def _allow_private() -> bool:
    """这个开关**每次现读**环境变量，不是模块级常量。

    为了让用例能逐条改它（`monkeypatch.setenv` 对导入期常量无效），也为了让"改了
    开关要重启"这件事不变成一个隐蔽前提：进程环境本来就在启动时定死。
    """
    return os.getenv(ALLOW_PRIVATE_ENV, "").strip() == "1"


#: 云厂商的实例元数据服务地址。**任何情况下都不许填**，开关也打不开。
#:
#: 与下面那组"内网地址"分开的理由：内网模型是一个真实需求（同机 Ollama、局域网
#: 里的推理机），所以内网友好开关；而元数据服务上没有任何模型，它只提供临时凭证。
#: 允许它唯一的用途就是让服务端带着已存的密钥去访问一个能反过来签出云账号的接口，
#: 用户没有任何理由需要这个能力——开关放开的应该是"我要连自己的模型"，不是
#: "我要连任何东西"。
_METADATA_HOSTS = frozenset({
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "169.254.169.254",       # AWS / GCP / Azure / 阿里云共用的链路本地地址
    "100.100.100.200",       # 阿里云专有的元数据地址
    "fd00:ec2::254",         # AWS IPv6 元数据
})


def _refuse(base_url: str) -> str | None:
    """发请求之前先挡一道。返回拒绝理由，`None` = 放行。

    **三条规则，按判断顺序**：

      1. **云元数据地址永远拒**，`ALLOW_PRIVATE_ENV` 也打不开（见 `_METADATA_HOSTS`）；
      2. **内网 / 本机地址**：开关关着就拒，开关开着放行（Ollama、局域网推理机）；
      3. **公网地址必须 https**：明文发出去，密钥在链路上是原文。

    这不是"防住内网"的完整方案（DNS rebinding、302 到内网、以及公网域名解析到
    私网地址都挡不住）。它挡的是**成本最低的那一步**：在页面上填一个内网地址，
    然后让服务端带着已存的密钥去请求它。把这一步的成本从"零"抬到"要先能改配置"，
    剩下的交给鉴权（`docker/nginx.conf` 那一层与 `auth/`）与密钥绑定
    （`store.resolve()` 里那条 host 判定）。

    也顺手挡了非 http(s) 协议：`file:///etc/passwd` 拼上 `/chat/completions` 之后
    行为完全取决于 httpx 的版本，不该由它决定。
    """
    try:
        parts = urlsplit((base_url or "").strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        # 端口不是数字之类。当成"没有主机名"处理：再往下构请求也只会得到一句
        # 更难读的 httpx 异常。
        return "地址解析不出来，检查一下端口和协议头。"
    if parts.scheme not in ("http", "https"):
        return "地址必须以 http:// 或 https:// 开头。"
    if not host:
        return "地址里没有主机名。"

    # --- 规则 1：元数据地址，开关也不放行 ---------------------------------
    if host in _METADATA_HOSTS:
        return (f"不允许把模型地址指向云平台的元数据服务（{host}）。"
                f"那里没有模型，只有临时凭证。")

    private = host in {"localhost", "::1"} or host.endswith(".local")
    if not private:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            # 是域名，不是 IP 字面量。解析结果不在这里看（见 docstring）。
            ip = None
        if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local
                               or ip.is_reserved or ip.is_unspecified or ip.is_multicast):
            # `0.0.0.0` / `::` 在多数系统上连的是本机，`is_private` 并不覆盖它们；
            # `is_unspecified` / `is_multicast` 是这条的补洞，不是多余的谨慎。
            private = True

    # --- 规则 2：内网 / 本机，开关决定 -------------------------------------
    if private:
        if _allow_private():
            return None
        where = "本机" if host in {"localhost", "::1"} or host.endswith(".local") else "内网 / 保留地址"
        return (f"不允许把模型地址指向{where}（{host}）。"
                f"确实要连本机或内网模型的话，在后端环境变量里设 {ALLOW_PRIVATE_ENV}=1。")

    # --- 规则 3：公网必须 https -------------------------------------------
    if parts.scheme != "https":
        return ("公网地址必须用 https://。http:// 会把密钥明文发出去，"
                "中间任何一跳都能读到它。")
    return None


def _redact(text: str, key: str) -> str:
    """把正文里出现的明文密钥换成打码值，并截断。

    替换的是**整串**而不是"看起来像 key 的部分"：只有我们自己知道这次用的是哪
    把密钥，靠模式匹配（`sk-…`）会漏掉自建端点的口令。空 key 时不做替换（替换空
    串会在每个字符之间插入打码值）。
    """
    snippet = (text or "").strip()
    if key and snippet:
        snippet = snippet.replace(key, "…")
    if len(snippet) > _SNIPPET_CHARS:
        snippet = snippet[:_SNIPPET_CHARS] + "…"
    return snippet


def _body_snippet(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return (getattr(response, "text", "") or "").strip()
    # OpenAI 兼容的错误体是 `{"error": {"message": "..."}}`；有的端点直接给
    # `{"message": "..."}`。两种都取，取不到就把整段 JSON 字面量截断显示。
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(payload.get("message"), str):
            return payload["message"]
        return json.dumps(payload, ensure_ascii=False)
    return json.dumps(payload, ensure_ascii=False)


def _message_for_status(status: int, snippet: str) -> str:
    """状态码 → 一句能指导下一步的话。原话附在后面，不吞掉。"""
    tail = f"（服务端原话：{snippet}）" if snippet else ""
    if status in (401, 403):
        return f"密钥被拒绝（HTTP {status}），请检查密钥是否正确、是否有该模型的权限。{tail}"
    if status == 404:
        return (f"地址或模型名不对（HTTP 404）。检查地址是否包含 /v1，"
                f"以及模型名是否是这个端点认识的。{tail}")
    if status == 429:
        return f"被限流了（HTTP 429），地址与密钥是通的，稍后再试。{tail}"
    if status >= 500:
        return f"模型服务自己出错了（HTTP {status}），地址与密钥大概率是通的。{tail}"
    return f"模型服务返回 HTTP {status}。{tail}"


def _client() -> httpx.AsyncClient:
    """造一个客户端。**抽成函数是为了让用例能替换它**。

    与 `knowledge/external.py` 的 `_client()` 同一手法：单测一律替换这个函数，
    不去 patch `httpx` 模块本身——那是全局副作用，会影响同一次用例里别的请求。
    """
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=True)


async def probe(base_url: str, api_key: str, model: str) -> ProbeResult:
    """发一次最小 chat，返回结论。**不抛异常**：失败也是一种结论。

    401 / 连接失败 / 超时都要变成一句中文，而不是冒到统一错误出口变成 500——
    那会让"我填错了一个字符"看起来像"服务器坏了"。

    地址被 `_refuse()` 挡下时也走这条"结论"通道（`ok=False`、零延迟），与 401、
    超时的形状一样。调用方若想把它区分成 400，那是对"地址本身不合法"的判断，得
    自己在调用之前问 `_refuse()`（`router.py` 就是这么做的）。
    """
    refusal = _refuse(base_url)
    if refusal is not None:
        # 一条字节都不发。**密钥绝不出门**——这个函数唯一会带密钥的动作就是下面
        # 那次 `client.post`，所以挡在它前面就够了。
        return ProbeResult(ok=False, model=model, message=refusal)
    started = time.monotonic()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": PROBE_MAX_TOKENS,
        "temperature": 0,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with _client() as client:
            response = await client.post(base_url + "/chat/completions", json=body,
                                         headers=headers)
    except httpx.TimeoutException:
        return ProbeResult(
            ok=False, model=model,
            message=f"连接超时（{TIMEOUT_SECONDS:.0f} 秒没有响应），请检查地址是否可达。",
            latency_ms=int((time.monotonic() - started) * 1000))
    except (httpx.RequestError, httpx.InvalidURL, OSError) as exc:
        return ProbeResult(
            ok=False, model=model,
            message=f"连不上这个地址：{type(exc).__name__}。请检查地址与网络。",
            latency_ms=int((time.monotonic() - started) * 1000))

    latency = int((time.monotonic() - started) * 1000)
    if response.status_code != 200:
        snippet = _redact(_body_snippet(response), api_key)
        return ProbeResult(ok=False, model=model, status=response.status_code,
                           message=_message_for_status(response.status_code, snippet),
                           latency_ms=latency)

    # 200 就够了吗？不够——有的兼容端点在模型名不认识时会回 200 + 空 choices。
    # 那种响应在生成侧会变成"模型返回内容里没有可解析的 JSON"，用户完全没有线索。
    try:
        payload = response.json()
        choices = payload.get("choices") or []
    except (ValueError, json.JSONDecodeError, AttributeError):
        return ProbeResult(ok=False, model=model, status=200,
                           message="服务返回 200，但正文不是 JSON——这个地址返回的不是 "
                                   "OpenAI 兼容格式（少了 /v1 是常见原因）。",
                           latency_ms=latency)
    if not choices:
        return ProbeResult(ok=False, model=model, status=200,
                           message="服务返回 200，但没有 choices——这个地址返回的不是完整的 "
                                   "OpenAI 兼容格式，也可能是模型名不被这个端点认识。",
                           latency_ms=latency)

    served = payload.get("model") if isinstance(payload, dict) else None
    detail = f"服务端回报的模型是 {served}" if isinstance(served, str) and served else ""
    message = f"连接正常（{latency} 毫秒）。"
    if detail:
        message += f"{detail}。"
    return ProbeResult(ok=True, model=model, status=200, message=message,
                       latency_ms=latency)


__all__ = ["ALLOW_PRIVATE_ENV", "PROBE_MAX_TOKENS", "ProbeResult", "TIMEOUT_SECONDS",
           "probe"]
