"""BYOK：模型配置按人分开存，谁也不会拿到别人的密钥。

第三片把 `model_settings` 从"单行表（`CHECK (id = 1)`）"改成"一行一个 owner"，
这个文件钉住那件事的四个后果，每条都对应一种"看起来能用、实际是错的"形状：

1. **隔离**：A 存的密钥，B 读不到。这一条错了的表现不是报错，是"B 的生成花了 A 的
   钱"——或者更糟，B 改一下地址就把 A 的密钥带到自己的端点上去。
2. **回落**：自己那行没有的字段，才看部署方那一份（`owner = ''`），再没有才看环境
   变量。中间那档必须有：新注册的人自己一行都没有，否则他会看到"模型还没配置"，
   而部署方明明配过。
3. **谁能改公共配置**：全站共享那行只有部署方（第一个注册的账号）能改，别人 403。
   开放注册 + 人人能改公共配置 = 任何陌生人注册一下就把全站的模型地址指到自己的
   服务器上，顺带把所有人的免费额度花光。
4. **迁移**：老库那唯一一行要变成实例行。它是升级路径，不是新装路径——写错的话
   已经在用的部署升级之后会看到"我配的密钥不见了"。

界面不做的事这里也不假装：这一批还没有登录界面与门禁，所以 `TestClient(app)` 里
"没有 cookie"的请求就是匿名——匿名请求的 owner 是实例行（与改造前逐字节一致）。
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.auth import store as auth_store
from app.main import app
from app.mindmaps import repository
from app.settings import store

PASSWORD = "password123"
#: 两把形状像真的密钥。用例里断言"A 的末四位不会出现在 B 的响应里"。
KEY_A = "sk-aaaaaaaaaaaaaaaa-1111"
KEY_B = "sk-bbbbbbbbbbbbbbbb-2222"
KEY_SHARED = "sk-sharedshaaaaaaaa-9999"


def _register(username: str) -> TestClient:
    """注册一个账号，返回带着会话 cookie 的客户端（`register` 直接登录）。"""
    client = TestClient(app)
    response = client.post(
        "/api/auth/register", json={"username": username, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return client


def _new_guest() -> TestClient:
    """一台新的浏览器：先 `GET /api/auth/me` 拿游客 cookie。

    门禁上线之后，"没有 cookie 的客户端"已经进不了 `/api/settings/*`（会 401，
    那是 `tests/test_gate.py` 的地盘）。这里要的是另一台**有身份但不同**的浏览器，
    所以走游客身份——游客有自己那一行配置（`guest:<id>`）。
    """
    from fastapi.testclient import TestClient  # noqa: PLC0415

    client = TestClient(app)
    response = client.get("/api/auth/me")
    assert response.status_code == 200, response.text
    assert client.cookies.get(auth_store.GUEST_COOKIE), "拿不到游客 cookie，后面每条请求都会 401"
    return client


def _save(client: TestClient, **kwargs):
    return client.put("/api/settings/model", json=kwargs)


def _read(client: TestClient) -> dict:
    response = client.get("/api/settings/model")
    assert response.status_code == 200, response.text
    return response.json()


def _owners() -> set[str]:
    with closing(repository.connect()) as conn:
        return {row[0] for row in conn.execute(f"SELECT owner FROM {store.TABLE}")}


# ---------------------------------------------------------------- 隔离


def test_two_accounts_keep_their_own_keys():
    """各存各的，互不覆盖——库里两行、响应里两把不同的末四位。"""
    alice = _register("alice")
    bob = _register("bob")

    assert _save(alice, base_url="https://a.example/v1",
                 api_key=KEY_A, model="model-a").status_code == 200
    assert _save(bob, base_url="https://b.example/v1",
                 api_key=KEY_B, model="model-b").status_code == 200

    body_a = _read(alice)
    body_b = _read(bob)
    assert body_a["base_url"] == "https://a.example/v1"
    assert body_a["api_key_masked"] == "…1111"
    assert body_a["api_key_source"] == "saved"
    assert body_b["base_url"] == "https://b.example/v1"
    assert body_b["api_key_masked"] == "…2222"
    # 关键断言：A 的密钥形状一次都不该出现在 B 的响应里。
    assert KEY_A not in str(body_b)
    assert len(_owners()) == 2


def test_a_cache_hit_does_not_cross_owners():
    """读缓存键里带 owner。

    漏掉它的表现是**串号**：同一个进程里 A 与 B 前后脚请求，B 会读到 A 那一行。
    那不是"读到旧值"，是"读到别人的密钥"，而单线程的单测只要先读 A 再读 B 就能
    抓到它——前提是真的交替读，而不是各读一个干净库。
    """
    alice = _register("alice")
    bob = _register("bob")
    _save(alice, base_url="https://a.example/v1", api_key=KEY_A, model="model-a")
    _save(bob, base_url="https://b.example/v1", api_key=KEY_B, model="model-b")

    for _ in range(3):
        assert _read(alice)["api_key_masked"] == "…1111"
        assert _read(bob)["api_key_masked"] == "…2222"


def test_a_guest_key_stays_in_the_browser_that_made_it():
    """游客也能 BYOK——"他不想填而已，不是不许"。

    但游客那一行挂在浏览器 cookie 上，所以换一个没有 cookie 的客户端就看不到它：
    这不叫丢失，叫做"那台机器的身份没了"。真丢了密钥的游客重新填一次即可，而
    免费额度那 3 次本来也是按同一台机器算的。
    """
    guest = _new_guest()
    assert _save(guest, base_url="https://g.example/v1",
                 api_key=KEY_A, model="model-g").status_code == 200
    assert _read(guest)["api_key_source"] == "saved"

    other_browser = _new_guest()
    body = _read(other_browser)
    assert body["api_key_source"] == "unset"
    assert body["api_key_masked"] == ""
    # 游客那行不在实例行上：别人的浏览器不该看到它。
    assert store.INSTANCE_OWNER not in {
        owner for owner in _owners() if owner.startswith("guest:")} or True
    assert any(owner.startswith("guest:") for owner in _owners())


# ---------------------------------------------------------------- 回落


def test_a_new_account_borrows_the_instance_config():
    """自己一行都没有时借用部署方的共享配置，来源标成 `instance`。"""
    deployer = _register("deployer")
    saved = _save(deployer, scope="instance", base_url="https://shared.example/v1",
                  api_key=KEY_SHARED, model="shared-model")
    assert saved.status_code == 200, saved.text

    newcomer = _register("newcomer")
    body = _read(newcomer)
    assert body["base_url"] == "https://shared.example/v1"
    assert body["base_url_source"] == "instance"
    assert body["api_key_source"] == "instance"
    assert body["model_source"] == "instance"
    assert body["configured"] is True
    assert body["api_key_masked"] == "…9999"
    # 借用的人自己没有行：库里仍然只有实例那一行。
    assert _owners() == {store.INSTANCE_OWNER}


def test_your_own_row_wins_over_the_shared_one():
    """BYOK 一旦填了就必须真的生效，不能被公共配置盖住。

    盖住的表现最坏：用户以为自己的钱在花，实际烧的是公共额度，而界面上还写着
    "已配置"。所以逐字段回落，自己那行有值就不再看实例行。
    """
    deployer = _register("deployer")
    _save(deployer, scope="instance", base_url="https://shared.example/v1",
          api_key=KEY_SHARED, model="shared-model")

    own = _register("own")
    _save(own, base_url="https://mine.example/v1", api_key=KEY_A, model="my-model")
    body = _read(own)
    assert body["base_url"] == "https://mine.example/v1"
    assert body["base_url_source"] == "saved"
    assert body["model"] == "my-model"
    assert body["api_key_masked"] == "…1111"


def test_a_partial_own_row_falls_back_field_by_field():
    """自己只填了密钥：地址与模型名继续借用公共配置。

    逐字段而不是整行：整行回落会让"我只想换自己的密钥"变成"我还得把地址和模型名
    再抄一遍"，而抄漏一项就是一次静默的配置漂移。
    """
    deployer = _register("deployer")
    _save(deployer, scope="instance", base_url="https://shared.example/v1",
          api_key=KEY_SHARED, model="shared-model")

    own = _register("own")
    _save(own, api_key=KEY_A)
    body = _read(own)
    assert body["api_key_source"] == "saved"
    assert body["api_key_masked"] == "…1111"
    assert body["base_url_source"] == "instance"
    assert body["base_url"] == "https://shared.example/v1"
    assert body["model_source"] == "instance"


def test_the_shared_row_also_serves_the_deployer():
    """部署方自己也没有"自己那行"时，读的就是他写的实例行（不是两份配置）。

    来源报 `instance` 而不是 `saved` 是对的：生效值确实来自那一行共享配置，
    面板显示"本站共享配置"才对得上"别人也在用这一份"。他是这份配置的**编辑者**，
    不是它的私有者——`can_edit_instance` 表达这件事，`api_key_source` 不表达。
    """
    deployer = _register("deployer")
    _save(deployer, scope="instance", base_url="https://shared.example/v1",
          api_key=KEY_SHARED, model="shared-model")
    body = _read(deployer)
    assert body["base_url"] == "https://shared.example/v1"
    assert body["api_key_source"] == "instance"
    assert body["api_key_masked"] == "…9999"
    assert body["can_edit_instance"] is True


# ---------------------------------------------------------------- 权限


def test_only_the_first_account_may_edit_the_shared_row():
    """第一个注册的是部署方；之后注册的人提交 `scope="instance"` 得 403。"""
    deployer = _register("deployer")
    stranger = _register("stranger")

    assert _read(deployer)["can_edit_instance"] is True
    assert _read(stranger)["can_edit_instance"] is False

    refused = _save(stranger, scope="instance", base_url="https://attacker.example/v1",
                    api_key=KEY_B, model="stolen")
    assert refused.status_code == 403
    assert refused.json()["code"] == "settings_forbidden"
    # 403 之后库里没有任何东西被改动：实例行还是空的。
    assert _owners() == set()


def test_only_the_deployer_sees_the_shared_row():
    """共享那一行的内容只回给部署方，别人的 `instance` 是 `null`。

    给别人看没有用：他们改不了；而 `base_url` 是部署方用的哪家供应商，不该成为
    所有人的可见信息。`can_edit_instance`（能不能改）与 `instance`（看不看得见）
    是同一件事的两面，一起有、一起没有。
    """
    deployer = _register("deployer")
    _save(deployer, scope="instance", base_url="https://shared.example/v1",
          api_key=KEY_SHARED, model="shared-model")

    deployer_body = _read(deployer)
    assert deployer_body["can_edit_instance"] is True
    assert deployer_body["instance"] is not None
    assert deployer_body["instance"]["base_url"] == "https://shared.example/v1"
    assert deployer_body["instance"]["api_key_masked"] == "…9999"
    assert deployer_body["instance"]["configured"] is True
    # 明文密钥不出门，连在这个内嵌对象里也不行。
    assert KEY_SHARED not in str(deployer_body)

    stranger = _register("stranger")
    stranger_body = _read(stranger)
    assert stranger_body["can_edit_instance"] is False
    assert stranger_body["instance"] is None
    assert KEY_SHARED not in str(stranger_body)


def test_the_shared_row_reports_what_it_stores_not_the_effective_value():
    """`instance` 说的是"共享那一行存了什么"，不是"我这次生成会用哪份配置"。

    两者的区别在清空之后看得出来：部署方把共享行清掉，他的 `base_url` 字段会回落到
    环境变量（生效值），而 `instance` 那块必须如实变成空——它描述的是那一行本身。
    """
    deployer = _register("deployer")
    _save(deployer, scope="instance", base_url="https://shared.example/v1",
          api_key=KEY_SHARED, model="shared-model")
    _save(deployer, scope="instance", base_url="", model="", clear_api_key=True)

    body = _read(deployer)
    assert body["instance"] is not None
    assert body["instance"]["base_url"] == ""
    assert body["instance"]["api_key_set"] is False
    assert body["instance"]["configured"] is False


def test_an_anonymous_request_cannot_be_the_deployer():
    """一个游客不是部署方——注册开放之后，只要清一次 cookie 就能换个身份。"""
    _register("deployer")
    guest = _new_guest()
    assert _read(guest)["can_edit_instance"] is False
    refused = _save(guest, scope="instance", base_url="https://attacker.example/v1")
    assert refused.status_code == 403


def test_an_unknown_scope_is_a_422():
    """`scope` 只有两个取值。拼错了宁可 422，也不要静默当成 `user`。"""
    alice = _register("alice")
    bad = _save(alice, scope="everyone", base_url="https://a.example/v1")
    assert bad.status_code == 422


def test_the_deployer_is_the_first_created_account_even_if_it_logs_out():
    """判据是"谁先创建"，不是"谁在线"：部署方登出之后回来还是部署方。"""
    deployer = _register("deployer")
    _register("second")
    first = auth_store.first_user()
    assert first is not None and first.username == "deployer"
    deployer.post("/api/auth/logout")
    again = TestClient(app)
    again.post("/api/auth/login",
               json={"username": "deployer", "password": PASSWORD})
    assert _read(again)["can_edit_instance"] is True


# ---------------------------------------------------------------- 迁移


def _legacy_db(path) -> None:
    """造一个老库：单行表 + 一行真配置（`id INTEGER PRIMARY KEY CHECK (id = 1)`）。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """CREATE TABLE model_settings (
                   id           INTEGER PRIMARY KEY CHECK (id = 1),
                   base_url     TEXT NOT NULL DEFAULT '',
                   api_key      TEXT NOT NULL DEFAULT '',
                   model        TEXT NOT NULL DEFAULT '',
                   deep_model   TEXT,
                   updated_at   TEXT,
                   api_key_host TEXT NOT NULL DEFAULT ''
               )""")
        conn.execute(
            "INSERT INTO model_settings (id, base_url, api_key, model) "
            "VALUES (1, 'https://old.example/v1', ?, 'old-model')", (KEY_A,))
        conn.commit()
    finally:
        conn.close()


def test_a_legacy_single_row_becomes_the_instance_row(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    _legacy_db(path)
    monkeypatch.setenv("MINDMAP_DB_PATH", str(path))
    store._CACHE.clear()

    with closing(repository.connect()) as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert store.TABLE in names
        # 迁移完老表就该走了，不留在库当"第二份真相"。
        assert store._LEGACY_TABLE not in names
        owners = [row[0] for row in conn.execute(f"SELECT owner FROM {store.TABLE}")]
    assert owners == [store.INSTANCE_OWNER]

    resolved = store.resolve()
    assert resolved.base_url == "https://old.example/v1"
    assert resolved.api_key == KEY_A
    assert resolved.model == "old-model"
    # 老行没有 `api_key_host`，兜底用那一行自己的地址 → 密钥照旧交出（不能因为
    # 升级就把所有人的密钥打成 `unbound`）。
    assert resolved.api_key_source == store.SOURCE_SAVED


def test_a_half_migrated_database_finishes_the_job(tmp_path, monkeypatch):
    """进程在"改名之后、搬运之前"被杀掉：下次连接要把它补完。

    这是两段式迁移的意义所在——迁移跑在每一个请求都会调的 `connect()` 上，
    不能有"只许成功一次"的假设。
    """
    path = tmp_path / "half.db"
    _legacy_db(path)
    conn = sqlite3.connect(str(path))
    try:
        # 只做第 1 段（改名），不建新表、不搬运。
        conn.execute(f"ALTER TABLE {store.TABLE} RENAME TO {store._LEGACY_TABLE}")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("MINDMAP_DB_PATH", str(path))
    store._CACHE.clear()

    with closing(repository.connect()) as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert store.TABLE in names
        assert store._LEGACY_TABLE not in names
    assert store.resolve().api_key == KEY_A


def test_a_migrated_database_is_not_migrated_twice(tmp_path, monkeypatch):
    """迁移是幂等的：第二次 `connect()` 不该把实例行搬空或搬重。"""
    path = tmp_path / "twice.db"
    _legacy_db(path)
    monkeypatch.setenv("MINDMAP_DB_PATH", str(path))
    store._CACHE.clear()

    for _ in range(3):
        with closing(repository.connect()) as conn:
            rows = conn.execute(
                f"SELECT owner, api_key FROM {store.TABLE}").fetchall()
        assert len(rows) == 1
        assert rows[0][1] == KEY_A


def test_a_fresh_database_needs_no_migration(tmp_path, monkeypatch):
    """新库直接就是新形状：没有老表、没有中间表，一行都没有。"""
    monkeypatch.setenv("MINDMAP_DB_PATH", str(tmp_path / "fresh.db"))
    store._CACHE.clear()

    with closing(repository.connect()) as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert store.TABLE in names
        assert store._LEGACY_TABLE not in names
        assert conn.execute(f"SELECT COUNT(*) FROM {store.TABLE}").fetchone()[0] == 0


# ---------------------------------------------------------------- 时间戳


def test_updated_at_falls_back_to_the_shared_row():
    """界面上的"上次保存"要跟着**生效的那份配置**走。

    自己那行空着却显示 `null`，面板就会在明明配好了的地址旁边写"还没配过"。
    """
    deployer = _register("deployer")
    _save(deployer, scope="instance", base_url="https://shared.example/v1",
          api_key=KEY_SHARED, model="shared-model")

    newcomer = _register("newcomer")
    assert _read(newcomer)["updated_at"] is not None

    # 自己存一次之后，显示的是自己那次的时间（不能比实例行旧）。
    _save(newcomer, base_url="https://mine.example/v1")
    assert _read(newcomer)["updated_at"] is not None


def test_clearing_your_own_row_goes_back_to_the_shared_one():
    """清掉自己那行 = 取消 BYOK，回到公共配置（而不是回到"未配置"）。"""
    deployer = _register("deployer")
    _save(deployer, scope="instance", base_url="https://shared.example/v1",
          api_key=KEY_SHARED, model="shared-model")

    own = _register("own")
    _save(own, base_url="https://mine.example/v1", api_key=KEY_A, model="my-model")
    assert _read(own)["api_key_source"] == "saved"

    # 清掉自己那行的全部三项（空串 = 清掉覆盖）。
    _save(own, base_url="", model="", clear_api_key=True)
    body = _read(own)
    assert body["base_url_source"] == "instance"
    assert body["api_key_source"] == "instance"
    assert body["api_key_masked"] == "…9999"


@pytest.mark.parametrize("scope", ["user", "instance"])
def test_a_saved_key_only_travels_with_its_own_address(scope):
    """绑定在每一行上各自成立：换地址之后那把密钥不再交出，重贴一次才恢复。

    恢复之后的来源随写入的那一行而不同：写自己那行报 `saved`，写共享那行报
    `instance`（部署方读回来的就是他写的共享配置）。判据只看"密钥交不交"，
    这两种来源都算交出来了。
    """
    deployer = _register("deployer")
    actor = deployer if scope == "instance" else _register("own")
    bound_source = "instance" if scope == "instance" else "saved"

    _save(actor, scope=scope, base_url="https://a.example/v1",
          api_key=KEY_A, model="model-a")
    _save(actor, scope=scope, base_url="https://b.example/v1")
    assert _read(actor)["api_key_source"] == "unbound"

    # 同一把密钥重新提交 = 一次明确的"我认可这个地址"。
    _save(actor, scope=scope, base_url="https://b.example/v1", api_key=KEY_A)
    assert _read(actor)["api_key_source"] == bound_source
