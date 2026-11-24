"""一条请求之外的额外约束：游客身份必须**不可伪造**。

游客 id 是身份（它决定"这台浏览器"能看到哪些草稿、待处理、自己的设置）。没有签名
的时候它只是一段格式合法的 32 位十六进制串——审查时实测过：把 `mm_guest` 改成别人
的值，就能读到那个人的 run detail、待处理列表，甚至发出他那次运行。

这条约束放在 `tests/test_guest_signature.py` 而不是 `test_isolation.py`：那一份讲
的是"业务数据按 owner 分开"，这一份讲的是"owner 本身是从哪来的、为什么不能自己写"。
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.auth import identity, store
from app.main import app

_GUEST_COOKIE_SHAPE = re.compile(r"^[0-9a-f]{32}\.[0-9a-f]{16}$")


def _guest() -> TestClient:
    client = TestClient(app)
    assert client.get("/api/auth/me").status_code == 200
    return client


def test_the_guest_cookie_is_signed():
    """签发的值必须是 `<32 位 hex>.<16 位 hex>`。"""
    client = _guest()
    value = client.cookies.get(store.GUEST_COOKIE)
    assert value is not None
    assert _GUEST_COOKIE_SHAPE.match(value), value


def test_a_forged_guest_cookie_is_not_an_identity():
    """自己拼一个"形状对、签名假"的 cookie → 服务端认不出来，等于没有 cookie。

    **形状对很重要**：攻击者看得见 cookie 的格式（他就是想把别人的 id 塞进去），
    所以他会写上 `<id>.<随便 16 位 hex>`。只测"没有点号的裸 id 被拒"是不够的——
    那种形状连最原始的正则都能挡住，测不出签名有没有生效。

    这里刻意用一条**新的** `TestClient` 直接带上伪造值，而不是在已有 client 上
    `set()`：`TestClient` 的 cookie jar 按 (name, domain, path) 存，对 `testserver.local`
    已有的那条同名 cookie 再 `set()` 会变成两条而不是覆盖，测出来的是 jar 的行为，
    不是服务端的行为。
    """
    client = _guest()
    alice_id = client.get("/api/auth/me").json()["guest_id"]
    # 拿 A 的 id 配一个假签名：形状完全合法。
    forged = f"{alice_id}.{'0' * 16}"
    attacker = TestClient(app)
    attacker.cookies.set(store.GUEST_COOKIE, forged, domain="testserver.local")
    response = attacker.get("/api/auth/me")
    assert response.status_code == 200
    # 认不出 → 补发一个新的，而不是把他当成 A。
    returned = response.json()["guest_id"]
    assert returned != alice_id
    new_cookie = attacker.cookies.get(store.GUEST_COOKIE, domain="testserver.local")
    assert new_cookie is not None
    assert new_cookie != forged
    assert _GUEST_COOKIE_SHAPE.match(new_cookie), new_cookie


def test_everyone_cannot_just_claim_someone_elses_id():
    """B 把 A 的**真 cookie 值**原样搬过来 → 那还是 A 的身份（这才是需要防的）。

    这一条标出边界：签名防的是"猜/编 id"，不防"cookie 被真的偷走"。后者是
    cookie 本身的保密性问题（HttpOnly + SameSite + 只走 https），与签名正交。
    用例写成"搬真值仍然认"是为了让这条边界可见——否则很容易以为签名解决了
    盗用。
    """
    alice = _guest()
    alice_id = alice.get("/api/auth/me").json()["guest_id"]
    stolen = alice.cookies.get(store.GUEST_COOKIE, domain="testserver.local")
    assert stolen is not None
    thief = TestClient(app)
    thief.cookies.set(store.GUEST_COOKIE, stolen, domain="testserver.local")
    assert thief.get("/api/auth/me").json()["guest_id"] == alice_id


def test_sign_and_read_round_trip():
    """`sign_guest` / `read_guest` 成对：签过的认得出，改一个字符就认不出。"""
    guest_id = identity.new_guest_id()
    signed = store.sign_guest(guest_id)
    assert store.read_guest(signed) == guest_id
    # 改签名
    body, _, signature = signed.partition(".")
    flipped = "0" if signature[0] != "0" else "1"
    assert store.read_guest(f"{body}.{flipped}{signature[1:]}") is None
    # 改 id（签名对不上）
    other = "f" * 32
    assert store.read_guest(f"{other}.{signature}") is None
    # 形状不对
    assert store.read_guest(guest_id) is None
    assert store.read_guest("") is None
    assert store.read_guest("no-dot-at-all") is None


def test_a_guest_can_still_use_its_own_identity():
    """签名不能把正常游客挡在门外：拿到 cookie 之后写的东西还归自己。

    这条是"修了伪造"与"把游客整个弄坏"的分界线——只测 fail 的用例会让
    "所有游客都是匿名"这种实现也全绿。
    """
    client = _guest()
    assert client.post("/api/memory", json={"text": "我喜欢清单体"}).status_code == 201
    listed = client.get("/api/memory")
    assert listed.status_code == 200
    assert any("清单体" in item["text"] for item in listed.json())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
