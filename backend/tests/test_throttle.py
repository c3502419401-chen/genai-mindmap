"""登录限速：20 次失败 + 5 秒防抖，两个桶取严。

这一层验四件事：

  1. **形状**：窗口内前 20 次随便试（都是 401），第 21 次开始每次都要等 5 秒
     （429 + `Retry-After`），等够 5 秒放行一次；
  2. **取严**：只换账号或只换 IP 都绕不过去（撞库与换出口两种真实手法）；
  3. **清零的方向**：成功清账号桶、**不清 IP 桶**——持有一个有效账号的人不能
     用它给自己重置；
  4. **降级方向**：限速表读写失败时放开，不是把人关在门外。

时间在测试里不睡：`throttle` 的判据全由参数里的 `now` 与库里的时间戳决定，
所以用例直接改库里的 `last_at`／`first_at` 来"让时间过去"，比 `sleep(5)`
快也稳。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.auth import identity, store, throttle
from app.main import app

PASSWORD = "password123"


def _client() -> TestClient:
    return TestClient(app)


def _register(username: str) -> TestClient:
    client = _client()
    response = client.post(
        "/api/auth/register", json={"username": username, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return client


def _login(client: TestClient, username: str, password: str = PASSWORD,
           *, ip: str | None = None):
    """带 `X-Real-IP` 发一次登录。

    显式给 IP 是因为 `TestClient` 的 `scope["client"]` 是 `("testclient", 50000)`
    这个常量：不覆盖的话"换 IP"这件事在测试里做不出来，而它正是要验的东西之一。
    """
    headers = {"X-Real-IP": ip} if ip else {}
    return client.post("/api/auth/login",
                       json={"username": username, "password": password},
                       headers=headers)


def _db() -> sqlite3.Connection:
    from app.mindmaps import repository

    return repository.connect()


def _rows() -> list[tuple]:
    conn = _db()
    try:
        return list(conn.execute(
            f"SELECT bucket, failures, first_at, last_at FROM {throttle.TABLE} "
            "ORDER BY bucket"))
    finally:
        conn.close()


def _backdate(bucket: str, seconds: float, *, first: bool = False) -> None:
    """把这个桶的时间戳往前挪，模拟"已经过去 seconds 秒"。"""
    column = "first_at" if first else "last_at"
    conn = _db()
    try:
        conn.execute(
            f"UPDATE {throttle.TABLE} SET {column} = {column} - ? WHERE bucket = ?",
            (seconds, bucket))
        conn.commit()
    finally:
        conn.close()


def _elapse(ip: str, username: str, seconds: float, *, whole_window: bool = False) -> None:
    """让时间过去 seconds 秒——**两个桶一起挪**。

    失败是同时记进 IP 桶与账号桶的，只挪一个不够：判据取两个桶的较大值，另一个
    桶还停在"刚刚失败"上，于是照样 429。这个坑在写用例时踩过一次，所以做成
    一个函数，让"时间过去了"这件事只有一种写法。
    """
    for bucket in (throttle.bucket_ip(ip), throttle.bucket_account(username)):
        if whole_window:
            _backdate(bucket, seconds, first=True)
        _backdate(bucket, seconds)


def _fail(client: TestClient, username: str, count: int, *, ip: str = "10.0.0.1") -> None:
    for _ in range(count):
        response = _login(client, username, "wrong-password", ip=ip)
        assert response.status_code == 401, response.text


# ---------------------------------------------------------------- 形状


def test_the_first_twenty_failures_are_plain_401s():
    client = _register("shape_a")
    _fail(client, "shape_a", throttle.MAX_FAILURES)
    assert len(_rows()) >= 1


def test_the_twenty_first_attempt_is_a_429_with_retry_after():
    """第 21 次在**哈希之前**就被拒：它连"密码不对"都不该回答。"""
    client = _register("shape_b")
    _fail(client, "shape_b", throttle.MAX_FAILURES)
    response = _login(client, "shape_b", "wrong-password", ip="10.0.0.1")
    assert response.status_code == 429
    body = response.json()
    assert body["code"] == "auth_rate_limited"
    assert "秒" in body["message"]
    assert response.headers["Retry-After"] == "5"


def test_a_429_happens_even_with_the_right_password():
    """限速挡的是"继续尝试"，不是"密码对不对"——所以对的密码也只能等。"""
    client = _register("shape_c")
    _fail(client, "shape_c", throttle.MAX_FAILURES)
    response = _login(client, "shape_c", PASSWORD, ip="10.0.0.1")
    assert response.status_code == 429


def test_waiting_out_the_cooldown_lets_one_attempt_through():
    client = _register("shape_d")
    _fail(client, "shape_d", throttle.MAX_FAILURES)
    assert _login(client, "shape_d", "wrong-password", ip="10.0.0.1").status_code == 429
    # 让 5 秒过去（first_at 不大动：窗口仍在，只是冷却够了）。
    _elapse("10.0.0.1", "shape_d", throttle.COOLDOWN_SECONDS + 0.1)
    assert _login(client, "shape_d", "wrong-password", ip="10.0.0.1").status_code == 401
    # 那一次又记了一次失败，于是立刻又要等。
    assert _login(client, "shape_d", "wrong-password", ip="10.0.0.1").status_code == 429


def test_the_window_sliding_out_forgives_the_count():
    """安静 15 分钟之后重新拿到 20 次——"昨晚忘了密码"不该欠到第二天。"""
    client = _register("shape_e")
    _fail(client, "shape_e", throttle.MAX_FAILURES)
    assert _login(client, "shape_e", "wrong-password", ip="10.0.0.1").status_code == 429
    _elapse("10.0.0.1", "shape_e", throttle.WINDOW_SECONDS + 1, whole_window=True)
    assert _login(client, "shape_e", "wrong-password", ip="10.0.0.1").status_code == 401
    assert _login(client, "shape_e", "wrong-password", ip="10.0.0.1").status_code == 401


# ---------------------------------------------------------------- 两个桶取严


def test_changing_the_account_does_not_dodge_an_ip_limit():
    """撞库是"一个口令试很多账号"，所以 IP 桶必须自己数一份。"""
    client = _register("bucket_ip_a")
    _register("bucket_ip_b")
    _fail(client, "bucket_ip_a", throttle.MAX_FAILURES, ip="10.1.1.1")
    # 换一个**从没失败过的账号**、同一个 IP：仍被限。
    response = _login(client, "bucket_ip_b", "wrong-password", ip="10.1.1.1")
    assert response.status_code == 429


def test_changing_the_ip_does_not_dodge_an_account_limit():
    """同一个人换出口（移动网络）也绕不过账号桶。"""
    client = _register("bucket_acct")
    _fail(client, "bucket_acct", throttle.MAX_FAILURES, ip="10.2.2.2")
    response = _login(client, "bucket_acct", "wrong-password", ip="10.3.3.3")
    assert response.status_code == 429


def test_the_account_bucket_is_case_insensitive():
    """与 `users.username` 的 `COLLATE NOCASE` 对齐：改大小写不算换账号。"""
    assert throttle.bucket_account("Alice") == throttle.bucket_account("alice")


def test_an_empty_address_still_gets_a_bucket():
    """取不到 IP 时归成一个桶，不是"不限"。"""
    assert throttle.bucket_ip("") == throttle.bucket_ip("   ")
    assert throttle.bucket_ip("") != throttle.bucket_ip("1.2.3.4")


# ---------------------------------------------------------------- 清零的方向


def test_a_successful_login_clears_the_account_bucket():
    client = _register("clear_a")
    _fail(client, "clear_a", 5, ip="10.4.4.4")
    assert any(row[0] == throttle.bucket_account("clear_a") for row in _rows())
    response = _login(client, "clear_a", PASSWORD, ip="10.4.4.4")
    assert response.status_code == 200
    assert not any(row[0] == throttle.bucket_account("clear_a") for row in _rows())


def test_a_good_login_clears_the_account_bucket_but_not_the_ip_bucket():
    """否则持有一个有效账号的人能用它给自己重置，再继续撞别人。

    这条与"成功即清零"的字面口径差一格，是**刻意**的；见 `throttle` 模块头。

    注意断言的是"IP 桶那一行还在、计数没归零"，而不是"下一次立刻 429"：
    冷却已经过完一次（否则这次登录根本挤不进来），所以紧接着那一次会被放行，
    放行时又记一次失败，**再下一次**才回到 429。这也正是"每 5 秒一次"的形状。
    """
    client = _register("clear_b")
    _fail(client, "clear_b", throttle.MAX_FAILURES, ip="10.5.5.5")
    _elapse("10.5.5.5", "clear_b", throttle.COOLDOWN_SECONDS + 0.1)
    assert _login(client, "clear_b", PASSWORD, ip="10.5.5.5").status_code == 200
    assert not any(row[0] == throttle.bucket_account("clear_b") for row in _rows())
    ip_row = [row for row in _rows() if row[0] == throttle.bucket_ip("10.5.5.5")]
    assert ip_row and ip_row[0][1] >= throttle.MAX_FAILURES
    # 同一个 IP 换个从没失败过的账号继续撞：那一次被放行（冷却已到），但紧接着
    # 又回到 429——IP 桶没有因为前一次成功而归零。
    _register("clear_c")
    assert _login(client, "clear_c", "wrong-password", ip="10.5.5.5").status_code == 401
    assert _login(client, "clear_c", "wrong-password", ip="10.5.5.5").status_code == 429


def test_clear_all_empties_the_table():
    client = _register("clear_d")
    _fail(client, "clear_d", 2, ip="10.6.6.6")
    assert _rows()
    assert throttle.clear_all() >= 1
    assert _rows() == []


# ---------------------------------------------------------------- 降级与边界


def test_a_broken_throttle_table_lets_people_in(monkeypatch):
    """限速表坏了 → 放行。关掉保护比把所有人关在门外好；见模块头。"""
    def broken(**kwargs):
        raise sqlite3.OperationalError("no such table")

    monkeypatch.setattr(throttle, "_connect", broken)
    assert throttle.retry_after(ip="10.7.7.7", username="whoever") == 0
    # 记失败也不许把登录流程弄崩。
    assert throttle.record_failure(ip="10.7.7.7", username="whoever") is None
    assert throttle.clear_account("whoever") is None


def test_writing_a_failure_does_not_raise_when_the_table_is_gone(monkeypatch):
    client = _register("degrade_a")

    def broken(**kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(throttle, "_connect", broken)
    # 表坏了：仍要正常回答"密码不对"，而不是 500。
    assert _login(client, "degrade_a", "wrong-password", ip="10.8.8.8").status_code == 401


def test_the_table_is_created_by_the_repository():
    conn = _db()
    try:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()
    assert throttle.TABLE in names


# ---------------------------------------------------------------- 取 IP


def test_the_ip_comes_from_x_real_ip_first():
    scope = {"headers": [(b"x-real-ip", b"1.2.3.4"),
                         (b"x-forwarded-for", b"9.9.9.9, 8.8.8.8")]}
    assert identity.client_ip(scope) == "1.2.3.4"


def test_the_forwarded_list_takes_the_last_hop_not_the_first():
    """最左那个是客户端自己声称的，填什么就是什么——不能当真相。"""
    scope = {"headers": [(b"x-forwarded-for", b"9.9.9.9, 10.0.0.1")]}
    assert identity.client_ip(scope) == "10.0.0.1"


def test_the_socket_peer_is_the_fallback():
    assert identity.client_ip({"client": ("7.7.7.7", 5000)}) == "7.7.7.7"
    assert identity.client_ip({}) == ""


def test_a_throttled_login_does_not_touch_the_hash(monkeypatch):
    """限速判据必须在 scrypt 之前：否则被拒的请求照样烧算力。"""
    client = _register("order_a")
    _fail(client, "order_a", throttle.MAX_FAILURES, ip="10.9.9.9")

    calls: list[str] = []
    original = store.authenticate

    def counted(username, password):
        calls.append(username)
        return original(username, password)

    monkeypatch.setattr(store, "authenticate", counted)
    assert _login(client, "order_a", PASSWORD, ip="10.9.9.9").status_code == 429
    assert calls == []
