"""门禁：业务接口一律要求"有身份"，匿名请求在中间件层就被挡住。

这个文件只钉**门本身**——谁被放行、谁被拦住、拦住时回什么形状。业务接口在
"有身份时对不对"是其它文件的事（它们用 `conftest.guest_client()` 自动补身份）。

一条贯穿全文件的实现细节：**`GET /api/auth/me` 会给调用者装上游客 cookie**。
所以"匿名"必须用一个从没调过它的 client；同一个 client 上先调 `/me` 再断言
"匿名被拦"会永远失败——第一次写这批用例就踩在这上面，表现是"门禁没生效"。
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.gate import PUBLIC_PATHS, PUBLIC_PREFIXES
from app.main import app


def anonymous() -> TestClient:
    """一台没有任何 cookie 的浏览器。**不要**在这个 client 上调 `/api/auth/me`。"""
    return TestClient(app)


def guest() -> TestClient:
    """一台已经拿到游客身份的浏览器。"""
    client = TestClient(app)
    response = client.get("/api/auth/me")
    assert response.status_code == 200, response.text
    assert client.cookies.get("mm_guest"), "`/api/auth/me` 没发游客 cookie"
    return client


def _tree() -> dict:
    return {"title": "门禁用例", "root": {"id": "r", "label": "根", "children": []}}


# ---------------------------------------------------------------- 拦住的


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/mindmaps"),
    ("POST", "/api/mindmaps"),
    ("GET", "/api/prefs"),
    ("GET", "/api/settings/model"),
    ("PUT", "/api/settings/model"),
    ("GET", "/api/memory"),
    ("GET", "/api/knowledge/library"),
    ("POST", "/api/agent/runs/stream"),
    ("GET", "/api/agent/runs"),
    ("GET", "/api/mindmaps/whatever/edits"),
])
def test_business_paths_refuse_anonymous_callers(method: str, path: str):
    """业务路径一律 401，且 `code` 是 `auth_required`。

    断言 `code` 而不只是状态码：前端 `toApiError` 靠 `code` 决定显示哪句话，
    没有它界面只会说"请求失败"，用户不知道要去刷新一下。
    """
    response = anonymous().request(method, path, json={})
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "auth_required"
    # 文案里必须给出**下一步**（刷新页面拿游客身份，或注册账号）。
    assert "刷新" in body["message"] and "注册" in body["message"]
    assert body["detail"]["path"] == path


def test_the_401_carries_a_request_id():
    """401 也要带 `X-Request-ID`：中间件手写响应，最容易漏的就是它。"""
    response = anonymous().get("/api/mindmaps")
    assert response.headers.get("x-request-id")
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_an_incoming_request_id_is_reused():
    """前端带了 `X-Request-ID` 就必须沿用它，否则前后端日志对不上这次 401。"""
    response = anonymous().get("/api/mindmaps", headers={"X-Request-ID": "trace-42"})
    assert response.json()["request_id"] == "trace-42"


def test_a_forged_cookie_is_not_an_identity():
    """格式不对的游客 cookie 不认——它不能被当成"有身份"。"""
    client = anonymous()
    client.cookies.set("mm_guest", "../../etc/passwd")
    assert client.get("/api/mindmaps").status_code == 401


def test_an_expired_or_unknown_session_is_anonymous():
    """库不认识的会话 token 等于没有身份。"""
    client = anonymous()
    client.cookies.set("mm_session", uuid.uuid4().hex)
    assert client.get("/api/mindmaps").status_code == 401


# ---------------------------------------------------------------- 放行的


def test_health_is_open():
    """`/api/health` 必须免身份：部署脚本与容器 healthcheck 都打它，且不带 cookie。"""
    response = anonymous().get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/password"),
])
def test_identity_endpoints_are_open(method: str, path: str):
    """取得身份这件事本身不能要求先有身份（否则是循环依赖）。

    这里只断言"不是 401"——参数校验失败是 422、凭据不对是 400/401 但 `code`
    不同，那些由 `tests/test_auth.py` 钉。**注意 `/me` 之外的四条也不该 401**，
    否则新访客连注册都点不动。
    """
    response = anonymous().request(method, path, json={})
    assert response.status_code != 401, f"{method} {path} 被门禁挡住了"


def test_a_guest_cookie_is_enough():
    """游客也算有身份：免费试用三次要求新访客能直接用，不该先逼他注册。"""
    assert guest().get("/api/mindmaps").status_code == 200


def test_a_session_cookie_is_enough():
    """注册之后用会话 cookie，同样放行。"""
    client = anonymous()
    response = client.post("/api/auth/register",
                           json={"username": "gate_user", "password": "password123"})
    assert response.status_code == 201, response.text
    assert client.get("/api/mindmaps").status_code == 200


def test_options_is_not_gated():
    """CORS 预检不带 cookie，挡它等于让浏览器连真正的请求都发不出去。"""
    response = anonymous().options("/api/mindmaps")
    assert response.status_code not in (401,)


def test_non_api_paths_are_not_gated():
    """前端静态资源不该经过门禁。

    档它没有任何安全收益（`dist` 是公开文件），却会造出"页面白屏"这种比 401
    更难查的故障。这一条用 `/` 请求：它由后端的 SPA 兜底返回，或由 nginx 直接发。
    """
    response = anonymous().get("/")
    assert response.status_code != 401


# ---------------------------------------------------------------- 接线


def test_the_gate_wraps_the_identity_layer():
    """门禁必须在身份层的**里面**，顺序反了整站 401。

    `Starlette.add_middleware` 是 `insert(0)`，**后注册的在外层**。所以要的外层
    到内层是 `CORS → 身份 → 门禁`，`user_middleware` 列表恰好就是外到内的顺序。

    这条用例存在的理由：顺序反了之后 `current_identity()` 恒为匿名，现象是
    "带 cookie 也 401"，看起来像 cookie 没生效或部署坏了，而真因只是两行
    `add_middleware` 的先后——第一版就是这么写反的。
    """
    names = [m.cls.__name__ for m in app.user_middleware]
    assert "GateMiddleware" in names and "IdentityMiddleware" in names
    assert names.index("IdentityMiddleware") < names.index("GateMiddleware"), (
        f"门禁跑到身份层外面了：{names}")


def test_the_public_whitelist_is_small():
    """白名单只有"健康检查 + 取得身份"，加一条都要在这里显式改一次。

    没有这条断言的话，白名单会在某次小改动里悄悄长出一条业务路径——而那种
    漏洞在评审里看不出来（它就是一行字符串）。
    """
    assert PUBLIC_PATHS == {"/api/health"}
    assert PUBLIC_PREFIXES == ("/api/auth/",)


def test_the_gate_does_not_swallow_the_body():
    """被挡住时不能先读走请求体：那会让 401 之后的重试变成"空 body"。"""
    client = anonymous()
    response = client.post(
        "/api/mindmaps",
        content=json.dumps({"content": _tree()}).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401
    # 同一个 client 拿到游客身份后，**同一份 body 还能正常提交**。
    assert client.get("/api/auth/me").status_code == 200
    retried = client.post("/api/mindmaps", json={"content": _tree()})
    assert retried.status_code == 201, retried.text
