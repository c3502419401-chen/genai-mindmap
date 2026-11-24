"""账号的 HTTP 面：注册、登录、登出、问"我是谁"。

**为什么没有"找回密码"**：仓库里没有 SMTP，发不出任何邮件。假装有这个流程
（"我们会给你发一封邮件"）比没有它更糟——用户会去找一封永远不会到的信。
真要做，入口是部署方直接改库，写进 `docs/`。

**为什么 `GET /me` 是全站唯一会发游客 cookie 的地方**：游客 cookie 是"这台浏览器
的编号"，只该在"界面刚刚启动、要问当前是谁"那一刻发一次。放在别的接口上（每次
生成、每次保存）会让"我到底发过几个游客 id"变成一个无法回答的问题，而免费额度
正要按这个 id 计数。

**`/me` 永远返回 200**，未登录时 `user: null`。不用 401：这个接口回答的是
"现在是什么状态"，而 401 是"你被拒绝了"。前端启动时调用它，用 401 当"未登录"
会让错误处理与真正的鉴权失败混在一起——那正是这一批**还没有**任何 401 的原因。
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth import identity, store, throttle

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["账号"])


class UserView(BaseModel):
    """对外的账号形状。**不含 `password_hash`，也不含任何 id 之外的东西**。"""

    model_config = ConfigDict(extra="forbid")
    id: str
    username: str
    created_at: str


class WhoAmI(BaseModel):
    """`GET /me` 的响应：当前是谁。

    `kind` 是三种状态的名字，不是调试信息：界面上"未登录 / 游客 / 已登录"要显示
    不同的东西，而由后端算比前端自己拼（`user ? 'user' : guest_id ? 'guest' : ...`）
    更不容易在一处漏掉。
    """

    model_config = ConfigDict(extra="forbid")
    user: UserView | None = None
    guest_id: str | None = None
    kind: Literal["user", "guest", "anonymous"] = "anonymous"


class Credentials(BaseModel):
    """注册与登录共用的请求体。

    字段给 `min_length` 是为了让"空密码"在 Pydantic 这一层就被 422 拦住，而不是
    落到 `store` 里再抛业务错——两种错误在界面上都是红字，但前者不需要一次
    数据库往返。真正的长度规则仍在 `store` 里（8–200），这里只是挡明显空的。
    """

    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


def _view(user: store.User) -> UserView:
    return UserView(id=user.id, username=user.username, created_at=user.created_at)


def _set_session(response: Response, request: Request, user: store.User) -> None:
    """开一次会话并把 token 放进 cookie。

    旧会话**不撤销**：同一个人在两台设备上分别登录是正常用法，多设备各自一条。
    要踢掉别的设备，入口是改密码（它会撤销全部会话）。
    """
    token = store.open_session(user.id)
    response.headers.append(
        "set-cookie",
        identity.cookie_header(store.SESSION_COOKIE, token,
                               scope=request.scope,
                               max_age=store.SESSION_TTL_SECONDS))


@router.get("/me", response_model=WhoAmI)
def who_am_i(request: Request, response: Response) -> WhoAmI:
    """当前是谁。顺便补发游客 cookie（全站唯一发它的地方）。"""
    current = identity.current_identity()
    if current.user is not None:
        return WhoAmI(user=_view(current.user), guest_id=current.guest_id, kind="user")
    guest_id = current.guest_id
    if guest_id is None:
        # 没有合法游客 id：发一个新的。`Max-Age` 给一年——比会话长得多，因为它的
        # 语义是"这台浏览器"，不是"这次登录"。
        #
        # 值是 `<id>.<签名>`（`identity.guest_cookie_value`）：没有签名时任何格式
        # 合法的 32 位十六进制串都会被当成身份，改一下 cookie 就能读别人的草稿。
        guest_id = identity.new_guest_id()
        response.headers.append(
            "set-cookie",
            identity.cookie_header(store.GUEST_COOKIE,
                                   identity.guest_cookie_value(guest_id),
                                   scope=request.scope, max_age=365 * 24 * 3600))
    return WhoAmI(guest_id=guest_id, kind="guest")


@router.post("/register", response_model=UserView, status_code=status.HTTP_201_CREATED)
def register(body: Credentials, request: Request, response: Response) -> UserView:
    """注册并**直接登录**（返回 201 + 会话 cookie）。

    注册完不自动登录的话，用户要在同一个页面上把刚敲的密码再敲一遍——那不是安全，
    那只是让第一次使用的体验变差。注册成功意味着这条口令刚刚被验证过（是我们
    亲手 hash 的），没有额外的信任问题。
    """
    user = store.create_user(body.username, body.password)
    _set_session(response, request, user)
    logger.info("新账号注册：username=%s", user.username)
    return _view(user)


@router.post("/login", response_model=UserView)
def login(body: Credentials, request: Request, response: Response) -> UserView:
    """登录。账号或密码不对统一 401 `auth_bad_credentials`（理由见 `store.authenticate`）。

    **限速在哈希之前**：先问 `throttle.retry_after()`，被限就直接 429，连那次
    36 毫秒的 scrypt 都不花。顺序反过来的话，限速本身成了新的放大器——攻击者
    用被拒的请求把服务器算力耗光。
    """
    ip = identity.client_ip(request.scope)
    wait = throttle.retry_after(ip=ip, username=body.username)
    if wait:
        raise store.AuthError(
            "auth_rate_limited",
            f"失败次数太多了，请 {wait} 秒后再试。",
            status=429,
            headers={"Retry-After": str(wait)},
        )
    user = store.authenticate(body.username, body.password)
    if user is None:
        throttle.record_failure(ip=ip, username=body.username)
        raise store.AuthError("auth_bad_credentials", "账号或密码不对。", status=401)
    throttle.clear_account(body.username)
    _set_session(response, request, user)
    return _view(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request) -> Response:
    """登出：撤销这次会话并清 cookie。

    幂等：没登录、或者 cookie 已经无效，也返回 204。界面上的"退出登录"点两次
    不该第二次报错——它不是"删除资源"，是"让这台浏览器变成未登录"。

    响应对象在这里**新建**而不是复用注入的那个：注入的 `Response` 是给
    "在返回模型的同时补 header"用的，而这条路由的返回体就是它自己。
    """
    token = identity.read_cookie(request.scope, store.SESSION_COOKIE)
    if token:
        store.revoke_session(token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.headers.append(
        "set-cookie", identity.cleared_cookie(store.SESSION_COOKIE, scope=request.scope))
    return response


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(body: ChangePasswordRequest, request: Request) -> Response:
    """改密码。**需要已经登录**，并且要再输一次旧密码。

    两道都是必要的：只凭会话就能改密码，意味着一次 XSS（拿到会话）就能永久接管
    账号；只凭旧密码不看会话，等于把"知道你密码的人"和"已经登录的人"混为一谈。

    改完撤销全部会话（含当前这条），然后清 cookie：用户要在所有设备上重新登录。
    这不是"顺手多做一步"——改密码最常见的动机就是"我怀疑有人进过我的号"。
    """
    current = identity.current_identity()
    if current.user is None:
        raise store.AuthError("auth_required", "请先登录。", status=401)
    if store.authenticate(current.user.username, body.current_password) is None:
        raise store.AuthError("auth_bad_credentials", "当前密码不对。", status=401)
    if not store.change_password(current.user.id, body.new_password):
        # 会话指向的用户不在了（库被换过 / 手工删过行）。按未登录处理。
        raise store.AuthError("auth_required", "请重新登录。", status=401)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.headers.append(
        "set-cookie", identity.cleared_cookie(store.SESSION_COOKIE, scope=request.scope))
    return response


__all__ = ["router"]
