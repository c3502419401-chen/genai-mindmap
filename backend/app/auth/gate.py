"""门禁：业务接口一律要求"有身份"，匿名请求在这一层就被挡住。

**为什么是中间件而不是给 32 条业务路径各加一个依赖**：那 32 条路径里任何一条漏加，
就是一条匿名可访问的口子，而"漏了一条"在代码评审里极难看出来——它们长得都一样。
放在一处之后，"谁被放行"是一张显式的白名单，加接口时不需要记得做任何事。

**为什么"有身份"就够，不要求登录**：免费试用三次的设计要求新访客能直接用。所以
身份有三种，放行的标准是"不是匿名"——带游客 cookie 的访客也算有身份。游客 cookie
由 `GET /api/auth/me` 下发，前端启动时先调它一次。

**它必须挂在 `IdentityMiddleware` 里面**（注册顺序见 `main.py`）：那一层把"当前是
谁"放进 ContextVar，这一层只负责读。反过来注册的话 `current_identity()` 恒为匿名，
所有人都会拿到 401——而那种故障的表现是"整站 401"，看起来像部署坏了。
"""
from __future__ import annotations

import logging
from uuid import uuid4

from fastapi.responses import JSONResponse

from app.auth import identity as identity_module

logger = logging.getLogger(__name__)


def _request_id(scope: dict) -> str:
    """沿用 `X-Request-ID`，没有就新生成一个。

    与 `core/errors.py::_request_id` 同一套口径（那里要 `Request` 对象，而中间件
    手里只有 scope，所以这一小段重写一次）。不沿用同一个 id 的话，这次 401 在
    前端日志与后端日志里对不上号——而"匿名请求被挡"正是最需要对齐现场的一类。
    """
    for name, value in scope.get("headers") or ():
        if name == b"x-request-id":
            cleaned = "".join(ch for ch in value.decode("latin-1") if "!" <= ch <= "~")
            if cleaned:
                return cleaned[:128]
    return uuid4().hex[:8]

#: 完全不需要身份的路径。`/api/health` 是部署的健康检查（nginx 与 compose 的
#: healthcheck 都打它，而且它们不带 cookie）；`/api/auth/*` 是"取得身份"这件事本身，
#: 要求它先有身份是循环依赖。
PUBLIC_PATHS = frozenset({"/api/health"})
PUBLIC_PREFIXES = ("/api/auth/",)

#: 这些方法不改变任何状态，也不需要身份。`OPTIONS` 是 CORS 预检——浏览器在拿到
#: 401 之前就会先问一句"我能不能发这个请求"，那一句里没有 cookie，也没有必要有。
SAFE_METHODS = frozenset({"OPTIONS"})


def _needs_identity(scope: dict) -> bool:
    """这条请求要不要过门禁。只看路径与手法，不看身份。"""
    if scope.get("method") in SAFE_METHODS:
        return False
    path = str(scope.get("path") or "")
    # 只有 API 需要门禁。静态资源（前端 dist）由 nginx 直接发，本来也到不了这里；
    # 万一到了（本机 vite 反代），让它过去——挡静态资源没有任何安全收益，只会
    # 让"页面白屏"这种更难查的故障出现。
    if not path.startswith("/api/"):
        return False
    if path in PUBLIC_PATHS:
        return False
    return not any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


class GateMiddleware:
    """纯 ASGI 中间件：匿名请求直接回 401，不进路由。

    写成纯 ASGI 而不是 `BaseHTTPMiddleware`：后者会把响应体包一层流，对 SSE 有
    两处已知坑（见 `identity.py` 里同一个理由）。这里只做"要么放行要么回一句话"，
    没有需要改写的响应体。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not _needs_identity(scope):
            await self.app(scope, receive, send)
            return
        ident = identity_module.current_identity()
        if ident.kind == "anonymous":
            # 手写响应体而不是 raise：异常处理器只挂在路由上，中间件里抛出去会
            # 变成 500。这里必须与 `core/errors.py` 的输出同形（`code`/`message`/
            # `detail`/`request_id`），否则前端 `toApiError` 读不到 `code`，界面
            # 只会显示一句没有下一步的"请求失败"。
            logger.info("匿名请求被门禁挡住：%s %s", scope.get("method"), scope.get("path"))
            request_id = _request_id(scope)
            await JSONResponse(
                status_code=401,
                content={
                    "code": "auth_required",
                    "message": "这个操作需要一个身份。刷新页面即可（会自动分配一个游客身份，"
                               "每天有 3 次免费生成），或者注册账号。",
                    "detail": {"path": scope.get("path")},
                    "request_id": request_id,
                },
                headers={"X-Request-ID": request_id},
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


__all__ = ["GateMiddleware", "PUBLIC_PATHS", "PUBLIC_PREFIXES"]
