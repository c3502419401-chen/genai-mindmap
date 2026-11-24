"""账号与会话：口令哈希、两张表、三种身份、五个端点。

这一层验的是"身份层是活的，而且它现在**不拦任何请求**"。分开四件事：

  1. 口令：哈希串的形状、盐、校验、以及库里那一行被改坏时表现为"这个人登不上"
     而不是这个接口 500；
  2. 账号：注册的三种失败（名字不合法 / 密码太短 / 重名）、不区分大小写的重名、
     登录失败不区分"没这个人"与"密码错"——后者是账号枚举信道；
  3. 会话：开一条能认回人、登出后认不回、过期后认不回、改密码把全部踢掉；
  4. 身份层：没有会话 cookie 的请求不碰库、游客 cookie 只由 `/me` 发、坏会话
     cookie 按未登录处理（不是 500）、**任何一个业务接口都还没被拦**。

第 4 条最后那句是这一批的验收判据之一：门禁是**后面**按接口逐个打开的。如果
现在就有一条路径开始 401，说明有人提前把闸合上了——那会让合并当天所有人都进不去。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.auth import identity, store
from app.main import app

client = TestClient(app)

PASSWORD = "password123"
SECRET_TAIL = "1234"


def _fresh() -> TestClient:
    """干净的浏览器：不带任何 cookie。"""
    return TestClient(app)


def _no_store() -> None:
    raise AssertionError("这一条路径不该碰数据库")


# ---------------------------------------------------------------- 口令


def test_hash_and_verify_round_trip():
    hashed = store.hash_password(PASSWORD)
    assert hashed.startswith("scrypt$")
    assert hashed.count("$") == 5
    assert store.verify_password(PASSWORD, hashed) is True
    assert store.verify_password("password124", hashed) is False


def test_the_same_password_hashes_differently_every_time():
    """盐是随机的：两个账号用同一句口令，库里看不出来。"""
    first = store.hash_password(PASSWORD)
    second = store.hash_password(PASSWORD)
    assert first != second
    assert store.verify_password(PASSWORD, first)
    assert store.verify_password(PASSWORD, second)


def test_the_hash_never_contains_the_password():
    hashed = store.hash_password("correct horse battery staple")
    assert "correct horse" not in hashed
    assert "battery" not in hashed


def test_a_malformed_stored_hash_is_a_failed_login_not_a_crash():
    """库里一行形状不对的哈希，只该让**这一个账号**登不上。"""
    for broken in ["", "not-a-hash", "scrypt$16384$8$1$zzzz$qqqq",
                   "scrypt$16384$8$1$" + "00" * 16]:
        assert store.verify_password(PASSWORD, broken) is False


def test_an_absurd_cost_parameter_is_refused_rather_than_computed():
    """参数来自库：一行被改成 n=2^30 的记录会让进程申请几百 GB。

    那是拒绝服务，不是校验失败——所以它有上下限，超了就返回 False 不计算。
    """
    salt = "00" * store.SALT_BYTES
    digest = "00" * store.SCRYPT_DKLEN
    assert store.verify_password(PASSWORD, f"scrypt$1073741824$8$1${salt}${digest}") is False
    assert store.verify_password(PASSWORD, f"scrypt$16384$1024$1${salt}${digest}") is False


# ---------------------------------------------------------------- 账号


def test_registering_and_finding_the_user():
    user = store.create_user("alice", PASSWORD)
    assert store.find_user("alice") == user
    assert store.get_user(user.id) == user
    # `User` 不带哈希：这个对象会一路流到路由层与请求上下文。
    assert not hasattr(user, "password_hash")


def test_usernames_are_unique_ignoring_case():
    """`Alice` 与 `alice` 看起来一样，所以只能是一个账号。"""
    store.create_user("Alice", PASSWORD)
    with pytest.raises(store.AuthError) as caught:
        store.create_user("alice", PASSWORD)
    assert caught.value.code == "auth_username_taken"
    assert caught.value.status == 409
    # 显示保留用户打的大小写。
    assert store.find_user("ALICE").username == "Alice"


@pytest.mark.parametrize("bad", ["ab", "a" * 33, "中文名", "has space", "has-dash"])
def test_bad_usernames_are_refused_with_the_rule_in_the_detail(bad):
    with pytest.raises(store.AuthError) as caught:
        store.create_user(bad, PASSWORD)
    assert caught.value.code == "auth_username_invalid"
    assert caught.value.detail["pattern"] == store.USERNAME_PATTERN.pattern


def test_short_passwords_are_refused():
    with pytest.raises(store.AuthError) as caught:
        store.create_user("bob", "short")
    assert caught.value.code == "auth_password_too_short"


def test_passwords_keep_their_spaces():
    """不 strip：口令里的空格是口令的一部分。"""
    raw = "  spaced123  "
    store.create_user("carol", raw)
    assert store.authenticate("carol", raw) is not None
    assert store.authenticate("carol", raw.strip()) is None


def test_authenticate_returns_none_for_either_kind_of_mistake():
    store.create_user("dave", PASSWORD)
    assert store.authenticate("dave", "wrong-password") is None
    assert store.authenticate("nobody", PASSWORD) is None
    assert store.authenticate("dave", "") is None
    assert store.authenticate("dave", PASSWORD) is not None


def test_a_missing_account_still_pays_for_one_hash(monkeypatch):
    """账不存在与密码错必须在**耗时**上也一样。

    不这么做的话，"没这个人"会快 36 毫秒，计时差就是一条精确的账号枚举信道。
    这里不测时钟（会 flaky），测的是"那条路确实算了一次哈希"。
    """
    calls: list[str] = []
    real = store.verify_password

    def counting(password: str, stored: str) -> bool:
        calls.append(stored)
        return real(password, stored)

    monkeypatch.setattr(store, "verify_password", counting)
    assert store.authenticate("never-registered", PASSWORD) is None
    assert len(calls) == 1
    assert calls[0] == store._DUMMY_HASH


# ---------------------------------------------------------------- 会话


def test_a_session_resolves_back_to_its_user():
    user = store.create_user("erin", PASSWORD)
    token = store.open_session(user.id)
    assert store.session_user(token) == user
    assert store.count_sessions(user.id) == 1


def test_logout_revokes_only_that_one_session():
    """两台设备各登一次，踢掉一台不影响另一台。"""
    user = store.create_user("frank", PASSWORD)
    phone = store.open_session(user.id)
    laptop = store.open_session(user.id)
    assert store.count_sessions(user.id) == 2

    assert store.revoke_session(phone) is True
    assert store.session_user(phone) is None
    assert store.session_user(laptop) == user
    # 重复登出不报错，只是没有行可改。
    assert store.revoke_session(phone) is False


def test_an_expired_session_stops_working():
    """过期判定在 SQL 里比定宽 UTC 文本，不靠定时任务（单机没有可靠调度器）。

    直接改库里的 `expires_at` 来造"一年前就该过期"的那一行：等 30 天不是选项，
    而"过期逻辑对不对"恰恰是这条用例唯一要回答的事。
    """
    from contextlib import closing

    from app.mindmaps import repository

    user = store.create_user("grace", PASSWORD)
    token = store.open_session(user.id)
    assert store.session_user(token) is not None

    with closing(repository.connect()) as conn, conn:
        conn.execute("UPDATE sessions SET expires_at = "
                     "strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 seconds')")
    assert store.session_user(token) is None


def test_unknown_and_empty_tokens_are_simply_nobody():
    assert store.session_user("") is None
    assert store.session_user("not-a-real-token") is None


def test_changing_the_password_kicks_every_device():
    """改密码最常见的动机是"我怀疑有人进过我的号"，那时还留着别的会话等于没改。"""
    user = store.create_user("heidi", PASSWORD)
    store.open_session(user.id)
    store.open_session(user.id)
    assert store.count_sessions(user.id) == 2

    assert store.change_password(user.id, "brand-new-password") is True
    assert store.count_sessions(user.id) == 0
    assert store.authenticate("heidi", "brand-new-password") is not None
    assert store.authenticate("heidi", PASSWORD) is None


def test_changing_the_password_of_a_vanished_user_is_false():
    assert store.change_password("no-such-id", "brand-new-password") is False


def test_the_schema_comes_up_with_the_repository():
    """`repository.connect()` 建表：中间件在链最前面读它，不能等第一条请求建。"""
    from contextlib import closing

    from app.mindmaps import repository

    with closing(repository.connect()) as conn:
        names = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {store.USER_TABLE, store.SESSION_TABLE} <= names
    # 幂等：再来一次不报错。
    with closing(repository.connect()) as conn:
        store.ensure_schema(conn)


# ---------------------------------------------------------------- 身份层


def _set_cookie(response) -> dict[str, str]:
    """把一条响应的 Set-Cookie 拆成 {名字: 值}（只取第一条同名）。"""
    found: dict[str, str] = {}
    for raw in response.headers.get_list("set-cookie"):
        name, _, rest = raw.partition("=")
        found[name.strip()] = rest.split(";")[0]
    return found


def test_me_says_who_you_are_and_hands_out_a_guest_cookie():
    browser = _fresh()
    first = browser.get("/api/auth/me")
    assert first.status_code == 200
    body = first.json()
    assert body["user"] is None
    assert body["kind"] == "guest"
    assert store.GUEST_COOKIE in _set_cookie(first)

    # 第二次带着 visitor cookie 回来，拿到的是同一个编号（不是又发一个新的）。
    second = browser.get("/api/auth/me")
    assert second.json()["guest_id"] == body["guest_id"]
    assert store.GUEST_COOKIE not in _set_cookie(second)


def test_a_malformed_guest_cookie_is_ignored_not_echoed():
    """游客 id 会被写进额度计数表；接受任意字符串等于接受任意长度的键。"""
    browser = _fresh()
    browser.cookies.set(store.GUEST_COOKIE, "../../etc/passwd")
    body = browser.get("/api/auth/me").json()
    assert body["kind"] == "guest"
    assert body["guest_id"] != "../../etc/passwd"


def test_requests_without_a_session_cookie_do_not_touch_the_database(monkeypatch):
    """健康检查与静态资源占绝大多数请求，它们不该各付一次会话查询。"""
    monkeypatch.setattr(store, "session_user", lambda token: _no_store())
    fresh = _fresh()
    assert fresh.get("/api/health").status_code == 200
    assert fresh.get("/api/auth/me").status_code == 200


def test_a_stale_session_cookie_means_logged_out_not_a_500():
    browser = _fresh()
    browser.cookies.set(store.SESSION_COOKIE, "a-token-that-was-never-issued")
    response = browser.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json()["user"] is None


def test_the_cookie_attributes_are_the_ones_we_promised():
    browser = _fresh()
    raw = browser.get("/api/auth/me").headers["set-cookie"]
    assert "HttpOnly" in raw
    assert "SameSite=Strict" in raw
    assert "Path=/" in raw
    # 本机是 http，不该带 Secure（否则部分浏览器会直接不收）。
    assert "Secure" not in raw


def test_secure_is_added_behind_https():
    """反向代理后面才算 https：`X-Forwarded-Proto` 与 scope 本身都认。"""
    plain = identity.cookie_header("x", "v", scope={"scheme": "http", "headers": []},
                                   max_age=1)
    assert "Secure" not in plain
    by_scope = identity.cookie_header("x", "v", scope={"scheme": "https", "headers": []},
                                      max_age=1)
    assert "Secure" in by_scope
    by_header = identity.cookie_header("x", "v", scope={
        "scheme": "http",
        "headers": [(b"x-forwarded-proto", b"https")],
    }, max_age=1)
    assert "Secure" in by_header


# ---------------------------------------------------------------- HTTP 面


def test_register_logs_you_in_and_never_returns_the_password():
    browser = _fresh()
    response = browser.post("/api/auth/register",
                            json={"username": "ivan", "password": PASSWORD})
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"id", "username", "created_at"}
    assert PASSWORD not in response.text
    assert store.SESSION_COOKIE in _set_cookie(response)

    who = browser.get("/api/auth/me").json()
    assert who["kind"] == "user"
    assert who["user"]["username"] == "ivan"


def test_a_taken_username_is_409_with_a_sentence():
    _fresh().post("/api/auth/register", json={"username": "judy", "password": PASSWORD})
    response = _fresh().post("/api/auth/register",
                             json={"username": "judy", "password": PASSWORD})
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "auth_username_taken"
    assert "换一个" in body["message"]


def test_an_empty_credential_is_422_before_any_database_work():
    response = _fresh().post("/api/auth/register", json={"username": "", "password": ""})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_an_unknown_field_is_refused_rather_than_ignored():
    """`extra="forbid"` 是仓库的默认风格，账号请求体也不例外。"""
    response = _fresh().post("/api/auth/login",
                             json={"username": "kit", "password": PASSWORD, "admin": True})
    assert response.status_code == 422


def test_login_and_logout_round_trip():
    _fresh().post("/api/auth/register", json={"username": "leo", "password": PASSWORD})

    browser = _fresh()
    assert browser.post("/api/auth/login",
                        json={"username": "leo", "password": PASSWORD}).status_code == 200
    assert browser.get("/api/auth/me").json()["kind"] == "user"

    assert browser.post("/api/auth/logout").status_code == 204
    assert browser.get("/api/auth/me").json()["kind"] == "guest"
    # 幂等：再点一次也是 204。
    assert browser.post("/api/auth/logout").status_code == 204


def test_a_wrong_password_says_only_one_thing():
    _fresh().post("/api/auth/register", json={"username": "mia", "password": PASSWORD})
    wrong = _fresh().post("/api/auth/login",
                          json={"username": "mia", "password": "wrong-password"})
    missing = _fresh().post("/api/auth/login",
                            json={"username": "no-such-person", "password": PASSWORD})
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["code"] == missing.json()["code"] == "auth_bad_credentials"
    assert wrong.json()["message"] == missing.json()["message"]


def test_identity_does_not_leak_from_one_request_to_the_next():
    """ContextVar 忘了 reset 的话，上一个请求的身份会漏到下一个请求。"""
    _fresh().post("/api/auth/register", json={"username": "nina", "password": PASSWORD})
    logged_in = _fresh()
    logged_in.post("/api/auth/login", json={"username": "nina", "password": PASSWORD})
    assert logged_in.get("/api/auth/me").json()["kind"] == "user"

    # 同一进程里的另一台浏览器，什么都没带。
    assert _fresh().get("/api/auth/me").json()["user"] is None


def test_changing_the_password_requires_both_old_password_and_a_session():
    _fresh().post("/api/auth/register", json={"username": "oscar", "password": PASSWORD})

    anonymous = _fresh()
    assert anonymous.post("/api/auth/password", json={
        "current_password": PASSWORD, "new_password": "another-password",
    }).status_code == 401

    browser = _fresh()
    browser.post("/api/auth/login", json={"username": "oscar", "password": PASSWORD})
    assert browser.post("/api/auth/password", json={
        "current_password": "not-the-old-one", "new_password": "another-password",
    }).status_code == 401

    assert browser.post("/api/auth/password", json={
        "current_password": PASSWORD, "new_password": "another-password",
    }).status_code == 204
    # 改完自己也被踢了：所有设备都要重新登录。
    assert browser.get("/api/auth/me").json()["kind"] == "guest"
    assert _fresh().post("/api/auth/login", json={
        "username": "oscar", "password": "another-password",
    }).status_code == 200


# ---------------------------------------------------------------- 现在还没上门禁


def test_the_gate_is_now_closed_for_anonymous_callers():
    """**这一刻到了**：门禁已经合闸，匿名请求一律 401。

    这条用例原来是 `test_business_endpoints_are_still_open_in_this_batch`，钉的是
    "这一批只做身份层、不拦任何请求"。它的存在意义就是"别提前合闸"——现在合闸了，
    于是它按当初写下的约定红了，改法也正是当初写明的那一种：**把断言改成"该 401 的
    401、该放行的放行"**，而不是删掉。删掉它等于把"合闸这件事没人验证过"重新变成
    默认状态。

    这里只验四件事，更细的白名单判定在 `tests/test_gate.py`。
    """
    # 放行的：健康检查（部署脚本与容器 healthcheck 依赖）与身份接口本身。
    # **这两条各用一个新 client**：`/api/auth/me` 的响应里带 `Set-Cookie`，在同一个
    # client 上调用它就会给这个 client 装上游客身份，后面再断言"匿名被拦"必然落空
    # （第一次踩到就是这么来的：自己把自己变成了有身份的人）。
    assert _fresh().get("/api/health").status_code == 200
    assert _fresh().get("/api/auth/me").status_code == 200
    # 拦住的：业务路径，用一个从没调过 `/api/auth/me` 的 client。
    anonymous = _fresh()
    for path in ("/api/mindmaps", "/api/prefs", "/api/settings/model"):
        response = anonymous.get(path)
        assert response.status_code == 401, f"{path} 应当要身份，实得 {response.status_code}"
        assert response.json()["code"] == "auth_required"


def test_the_identity_middleware_is_installed():
    """中间件真的挂上了（不是只写了一个类）。"""
    middleware_types = [m.cls.__name__ for m in app.user_middleware]
    assert "IdentityMiddleware" in middleware_types


def test_contextvar_default_is_nobody():
    """在没有请求的普通函数里调用 `current_user()` 不该炸。"""
    assert asyncio.run(_nobody())


async def _nobody() -> bool:
    return identity.current_user() is None
