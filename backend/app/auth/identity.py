"""每个请求"当前是谁"，以及会话 cookie 的读写。

**为什么用 `ContextVar` 而不是把身份塞进 `request.state`**：需要身份的地方不只有
路由函数，还有 `settings.store.resolve()` 这类**被深层函数调用**的地方（它在每次
模型调用的开头被调用，而那时手上只有"这次请求"，没有 FastAPI 的 `Request`）。
`ContextVar` 让"当前用户"在整个调用栈里可读，且不需要给五个调用点改签名——这是
后面 BYOK 那一批能不动 `_config()` 的前提。`asyncio.to_thread` 会复制当前上下文，
所以 `ingestion/vision_pdf.py` 里那些线程调用也能读到同一个身份。

**为什么是纯 ASGI 中间件，不是 `BaseHTTPMiddleware`**：后者把下游包进一个独立
任务，历史上对 SSE（本项目的主链路）有两处坑——一是它会等下游任务结束，二是
`http.disconnect` 的传递不完全。这里只需要"进入时解析一次、退出时复位"，十几行
纯 ASGI 就够，且对流式响应零影响。

**这一层不拒绝任何请求**。它只回答"是谁"，`kind` 可能是 `user` / `guest` /
`anonymous`。门禁在后面的批次里按接口逐个打开——一次全打开会让合并当天所有人
（包括自己和队友）都进不去，而"进不去"没有旁路可验证。
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from uuid import uuid4

from app.auth import store

logger = logging.getLogger(__name__)

#: 当前请求的身份。缺省是"既没登录也没游客 cookie"。
#:
#: 用 `ContextVar` 而不是模块级全局：同一进程里并发的两个请求必须看到各自的
#: 身份，否则会在多用户场景下把 A 的密钥用在 B 的请求上——那是最坏的一种 bug，
#: 而且它只在并发下出现。
_current: ContextVar[store.Identity] = ContextVar("auth_identity", default=store.Identity())


def current_identity() -> store.Identity:
    """当前请求的身份。在任何地方（含被深层调用的库函数）都可调用。"""
    return _current.get()


def current_user() -> store.User | None:
    """当前登录的人；没登录返回 None。"""
    return _current.get().user


# ---------------------------------------------------------------- cookie

#: `Secure` 属性怎么判：只有真的走 https 才加。
#:
#: 不能无条件加：本机开发是 `http://127.0.0.1:5173`，浏览器对 `Secure` cookie 的
#: 宽容度各版本不一致（Chrome 对 localhost 放行，但对 `127.0.0.1` 的旧版本不放），
#: 无条件加会让"本机跑起来登录不了"变成一个只在部分浏览器上出现的故障。
#:
#: 反向代理后面由 `X-Forwarded-Proto` 判断：`deploy/nginx.conf` 会设这个头，
#: 而 uvicorn 是否信任它取决于启动参数，所以两个来源都看。
_FORWARDED_PROTO = "x-forwarded-proto"


def _is_https(scope: dict) -> bool:
    if scope.get("scheme") == "https":
        return True
    for name, value in scope.get("headers") or ():
        if name.decode("latin-1").lower() == _FORWARDED_PROTO:
            return value.decode("latin-1").split(",")[0].strip().lower() == "https"
    return False


def _cookies(scope: dict) -> dict[str, str]:
    """从 ASGI scope 里解析 cookie。

    自己解而不是用 `starlette.requests.Request(scope).cookies`：中间件里没有
    `Request` 对象，构造一个只为了拿 cookie 反而更绕；而 cookie 的解析规则很简单
    （`;` 分隔、第一个 `=` 切分），不需要一整个库。
    """
    found: dict[str, str] = {}
    for name, value in scope.get("headers") or ():
        if name.decode("latin-1").lower() != "cookie":
            continue
        for chunk in value.decode("latin-1").split(";"):
            key, sep, raw = chunk.strip().partition("=")
            if not sep or not key or key in found:
                continue
            found[key] = raw
    return found


def cookie_header(name: str, value: str, *, scope: dict, max_age: int | None) -> str:
    """拼一条 `Set-Cookie`。

    `HttpOnly` + `SameSite=Strict` 是安全要求（用户拍板）：前者让 JS 拿不到会话
    token（XSS 就偷不走），后者让跨站 POST 不携带它（CSRF 的第一道防线）。代价是
    "从别的站点点链接过来会显示未登录"——这个站没有那种入口，代价为零。
    """
    parts = [f"{name}={value}", "Path=/", "HttpOnly", "SameSite=Strict"]
    if max_age is not None:
        parts.append(f"Max-Age={int(max_age)}")
    if _is_https(scope):
        parts.append("Secure")
    return "; ".join(parts)


def cleared_cookie(name: str, *, scope: dict) -> str:
    """让浏览器立刻丢掉这条 cookie 的 `Set-Cookie`。"""
    return cookie_header(name, "", scope=scope, max_age=0)


def new_guest_id() -> str:
    """一个新的游客 id。

    `uuid4().hex` 而不是 `token_urlsafe`：固定 32 位十六进制让 cookie 与库里的列
    都定长。
    """
    return uuid4().hex


def guest_cookie_value(guest_id: str) -> str:
    """放进 `mm_guest` 的值：`<id>.<签名>`。

    签名在 `store.sign_guest` 里（密钥从库路径派生，见那边的注释）。**签发与读取
    必须成对出现**：只改一边会让所有游客 cookie 突然不认，表现为"每次刷新都换一个
    游客身份"，而用户自己的导图与设置看起来就像丢了。
    """
    return store.sign_guest(guest_id)


def read_cookie(scope: dict, name: str) -> str:
    """读一条 cookie 的值；没有就是空串。"""
    return (_cookies(scope).get(name) or "").strip()


_REAL_IP = "x-real-ip"
_FORWARDED_FOR = "x-forwarded-for"


def _header(scope: dict, wanted: str) -> str:
    for name, value in scope.get("headers") or ():
        if name.decode("latin-1").lower() == wanted:
            return value.decode("latin-1").strip()
    return ""


def client_ip(scope: dict) -> str:
    """发起这次请求的地址，用于登录限速。

    **顺序是刻意的**：`X-Real-IP` 优先于 `X-Forwarded-For`。两份 nginx 配置都写
    `X-Real-IP $remote_addr`（`deploy/nginx.conf` 与 `docker/nginx.conf`），那是
    直连那一跳的真实地址；而 `X-Forwarded-For` 是**追加**的列表，最左那个是客户端
    自己声称的——把最左当真相等于让攻击者填什么就是什么。这里取 `X-Forwarded-For`
    只是为了不依赖我们自己的反代配置（比如有人直接把 uvicorn 暴露给一个只设了
    这个头的网关），所以取**最后一个**而不是第一个：最后一个是我们信任的那一跳
    写进来的。

    三个来源都没有时退回 `scope["client"][0]`；连它也没有就给空串，限速那边会
    把空串归成一个桶——那不是"不限"，是"所有身份不明的人共用一个桶"，比悄悄
    放行安全。

    注意这一层不校验地址真伪：能伪造 `X-Real-IP` 的前提是能直接连到后端端口
    （绕过 nginx），那种情况下前端所有防护都已经失效，限速表里多一个假桶不是
    新的损失。真要收紧，方向是在 nginx 上覆盖掉客户端传来的这两个头。
    """
    real = _header(scope, _REAL_IP)
    if real:
        return real
    forwarded = _header(scope, _FORWARDED_FOR)
    if forwarded:
        parts = [piece.strip() for piece in forwarded.split(",") if piece.strip()]
        if parts:
            return parts[-1]
    client = scope.get("client") or ()
    return str(client[0]) if client else ""


def _read_guest(cookies: dict[str, str]) -> str | None:
    """从 cookie 读出**已验证签名**的游客 id（`store.read_guest`）。

    没有签名时，任何格式合法的 32 位十六进制串都是一个身份：审查时实测过，把
    `mm_guest` 改成别人的值就能读到那个人未保存的草稿、待处理列表，甚至用他的
    run_id 拿到他那一轮的树。签名让"拿到别人的 cookie"与"拿到别人的密码"重新
    成为同一件事。
    """
    return store.read_guest(cookies.get(store.GUEST_COOKIE) or "")


def resolve_identity(scope: dict) -> store.Identity:
    """从 cookie 解析身份。

    只有**带着会话 cookie** 时才查库；没有 cookie 的请求（大多数 GET、健康检查、
    静态资源）零数据库开销——这一点很重要，健康检查在 CD 里每个版本都会打。
    """
    cookies = _cookies(scope)
    guest_id = _read_guest(cookies)
    token = (cookies.get(store.SESSION_COOKIE) or "").strip()
    if token:
        user = store.session_user(token)
        if user is not None:
            return store.Identity(user=user, session_hash=store.session_hash(token))
        # cookie 有但不认：可能是被撤销了、过期了、或者库换过了。**不清它**——
        # 清 cookie 需要写响应头，而中间件这一层还没有响应；由 `/auth/me` 顺手清。
        logger.info("会话 cookie 存在但无效，本次按未登录处理")
    return store.Identity(guest_id=guest_id)


class IdentityMiddleware:
    """把 `resolve_identity()` 的结果放进 `ContextVar`，包住整个下游。

    `finally` 里 `reset()` 是必须的：不 reset 的话，ASGI server 复用任务时上一个
    请求的身份会漏到下一个请求——在 `anyio` 的任务模型下这不一定发生，但**依赖
    "它一定不发生"是没必要的赌**。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        identity = resolve_identity(scope)
        token = _current.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(token)


__all__ = [
    "IdentityMiddleware",
    "cleared_cookie",
    "client_ip",
    "cookie_header",
    "current_identity",
    "current_user",
    "guest_cookie_value",
    "new_guest_id",
    "resolve_identity",
]
