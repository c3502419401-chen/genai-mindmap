"""最小数据隔离：导图**读共享、写限主**，运行记录**只本人可见**。

口径来自产品决定（不是实现顺手）：
- 导图是团队产物，登录之后能看别人的图才谈得上协作；改别人的图则必须是创建者。
- `runs` 是过程记录（还没保存的树、模型的思考、失败原因），属于发起者本人。
  它的可见性口径与导图**刻意不同**，两处放在一起看才不会以为哪个写错了。

这两条合起来的效果：`GET /api/mindmaps` 对所有人一样，`PUT` 只对创建者成功。
"""
from __future__ import annotations

from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.agent import runs as runs_module
from app.main import app
from app.mindmaps import repository

PASSWORD = "password123"


def _register(username: str) -> TestClient:
    client = TestClient(app)
    response = client.post("/api/auth/register",
                           json={"username": username, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return client


def _guest() -> TestClient:
    client = TestClient(app)
    assert client.get("/api/auth/me").status_code == 200
    return client


def _tree(title: str = "共享的图") -> dict:
    return {"title": title,
            "root": {"id": "r", "label": title,
                     "children": [{"id": "c", "label": "子", "children": []}]}}


def _create(client: TestClient, title: str = "共享的图") -> dict:
    response = client.post("/api/mindmaps", json={"content": _tree(title)})
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------- 读共享


def test_anyone_with_an_identity_can_read_someone_elses_map():
    """读共享：B 能打开 A 的图。团队协作的前提。"""
    alice, bob = _register("iso_alice"), _register("iso_bob")
    created = _create(alice)
    response = bob.get(f"/api/mindmaps/{created['id']}")
    assert response.status_code == 200
    assert response.json()["content"]["title"] == "共享的图"


def test_the_list_is_the_same_for_everyone():
    """列表不做过滤：过滤了，"能看别人的图"就只对拿到链接的人成立。"""
    alice, bob = _register("list_alice"), _register("list_bob")
    _create(alice, "甲")
    _create(bob, "乙")
    ids_alice = {item["id"] for item in alice.get("/api/mindmaps").json()}
    ids_bob = {item["id"] for item in bob.get("/api/mindmaps").json()}
    assert ids_alice == ids_bob
    assert len(ids_alice) == 2


# ---------------------------------------------------------------- 写限主


def test_only_the_creator_may_update_a_map():
    """写限主：B 的 `PUT` 是 403 `map_forbidden`，A 的照常成功。"""
    alice, bob = _register("edit_alice"), _register("edit_bob")
    created = _create(alice)
    response = bob.put(f"/api/mindmaps/{created['id']}",
                       json={"version": 1, "content": _tree("B 想改")})
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "map_forbidden"
    # 文案要给出**下一步**：读是共享的，所以"另存为"是真实可行的出路。
    assert "创建者" in body["message"] and "另存为" in body["message"]
    # A 自己改还是好的。
    assert alice.put(f"/api/mindmaps/{created['id']}",
                     json={"version": 1, "content": _tree("A 改的")}).status_code == 200


def test_a_forbidden_update_does_not_leak_the_version():
    """403 不能顺带告诉对方"真实版本号是多少"——那是创建者的信息。

    `update_map` 的判据顺序刻意是"先存在、再归属、最后才比版本"：反过来的话
    猜测版本号的请求会拿到 409 而不是 403，等于把版本位泄露给任何人。
    """
    alice, bob = _register("ver_alice"), _register("ver_bob")
    created = _create(alice)
    # A 先改一次，版本变成 2；B 拿一个过期的版本号去写。
    alice.put(f"/api/mindmaps/{created['id']}", json={"version": 1, "content": _tree("v2")})
    stale = bob.put(f"/api/mindmaps/{created['id']}",
                    json={"version": 1, "content": _tree("B")})
    assert stale.status_code == 403


def test_a_guest_can_create_and_owns_what_it_creates():
    """游客能创建，归到他那台浏览器；换个游客就改不了。"""
    first, second = _guest(), _guest()
    created = _create(first, "游客的图")
    assert first.put(f"/api/mindmaps/{created['id']}",
                     json={"version": 1, "content": _tree("改过")}).status_code == 200
    assert second.put(f"/api/mindmaps/{created['id']}",
                      json={"version": 1, "content": _tree("别人")}).status_code == 403


def test_an_unowned_legacy_row_is_writable_by_the_deployer_only():
    """`owner=''` 的历史行只有部署方能接管。

    迁移不编 owner（那等于假装知道是谁创建的），空串语义是"无主"。放开给所有人
    会让任何注册的人都能改历史数据；完全锁死则老用户自己再也动不了它——所以
    给"第一个注册的账号"这一个出口。
    """
    legacy_id = "legacy-iso"
    with closing(repository.connect()) as conn, conn:
        conn.execute("INSERT INTO mindmaps (id, version, content, updated_at) "
                     "VALUES (?, 1, ?, '2026-01-01T00:00:00Z')",
                     (legacy_id, '{"title":"旧","root":{"id":"r","label":"旧","children":[]}}'))
    nobody = _register("iso_first")  # 这台机器上第一个注册的账号 = 部署方
    assert nobody.put(f"/api/mindmaps/{legacy_id}",
                      json={"version": 1, "content": _tree("接管")}).status_code == 200
    late = _register("iso_late")
    assert late.put(f"/api/mindmaps/{legacy_id}",
                    json={"version": 2, "content": _tree("也想接管")}).status_code == 403


# ---------------------------------------------------------------- runs 只本人可见


def _seed_run(run_id: str, owner: str, *, candidate: str | None = None) -> None:
    """直接写一行 `runs`：接口层怎么产生它（模型调用）不在这个文件的范围里。

    `pending()` 的默认筛选里有 `candidate IS NOT NULL`（"没产出结果的运行不值得
    捡回来"），所以想要它出现就必须给一个候选。这里给一颗最小的合法树。
    """
    import json

    request = {"goal": "隔离用例", "intent": "create"}
    if candidate is None:
        candidate = json.dumps(_tree("候选"), ensure_ascii=False)
    with closing(runs_module._connect()) as conn, conn:
        conn.execute(
            "INSERT INTO runs (run_id, status, request, candidate, created_at, updated_at, owner) "
            "VALUES (?, 'done', ?, ?, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?)",
            (run_id, json.dumps(request, ensure_ascii=False), candidate, owner))


def test_a_run_is_only_visible_to_its_owner():
    """`GET /runs/{id}` 对别人是 404 而不是 403。

    `run_id` 是随机串，"这条记录存在但不属于你"本身就是不该给的信息——
    403 等于确认了这个 id 有效。
    """
    owner = _register("run_owner")
    stranger = _register("run_stranger")
    _seed_run("run-presence-check", "user:999999")
    assert stranger.get("/api/agent/runs/run-presence-check").status_code == 404
    # 同一条路由，主人自己拿得到（用主人真实的 owner 串另起一行）。
    from app.auth import identity as identity_module  # noqa: PLC0415

    with closing(runs_module._connect()) as conn, conn:
        owner_key = conn.execute("SELECT owner FROM runs WHERE run_id = ?",
                                 ("run-presence-check",)).fetchone()[0]
    mine = identity_module.current_identity()
    assert mine.kind == "anonymous", "测试里不该有残留身份"
    # 直接在 owner 语境下建一行并读回来：`get_for` 的 owner 参数是这条用例的主角。
    _seed_run("run-mine", "user:1")
    assert runs_module.get_for("run-mine", "user:1") is not None
    assert runs_module.get_for("run-mine", "user:2") is None


def test_pending_filters_by_owner():
    """「上次的结果」只捞自己的：否则刷新页面会看到别人没保存的树。"""
    _seed_run("run-a", "user:a")
    _seed_run("run-b", "user:b")
    mine = {record.run_id for record in runs_module.pending(owner="user:a")}
    assert mine == {"run-a"}


def test_marking_applied_cannot_be_done_by_someone_else():
    """知道别人 `run_id` 的人不能替他标掉 `applied_at`。

    那一标之后，那条记录再也不会出现在他的「上次的结果」里——一条安静的数据丢失。
    """
    _seed_run("run-apply", "user:owner")
    assert runs_module.mark_applied("run-apply", owner="user:other") is False
    assert runs_module.mark_applied("run-apply", owner="user:owner") is True


# ---------------------------------------------------------------- 流接口的归属

def test_streaming_someone_elses_finished_run_is_404():
    """`POST /runs/stream` 带别人的 `run_id` 必须是 404，不能把树重放出来。

    这条路是审查时实测到的（P1-1）：`GET /runs/{id}` 已经按 owner 过滤了，但
    stream 走的是 `runs.begin()` → `_replay()`，中间没有任何归属检查。B 拿 A 的
    done 行打一次流，流里就是 A 的整棵树。`run_id` 由前端生成（时间戳 + 随机 6 位），
    猜中的成本比看上去低，而代价是别人的未保存草稿。
    """
    alice, bob = _register("stream_alice"), _register("stream_bob")
    _seed_run("run-stream-done", "user:1")

    response = bob.post("/api/agent/runs/stream",
                        json={"goal": "偷看一下", "intent": "create",
                              "run_id": "run-stream-done"})
    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"
    # 流里一个字节都不该有 A 的内容。404 走的是错误响应，不是流。
    assert "候选" not in response.text


def test_streaming_an_abandoned_run_of_someone_else_does_not_steal_it():
    """用别人 `failed` 的 `run_id` 打流 → 404，**而且那行不能改归自己**。

    这是审查里更隐蔽的那一半：光拒绝请求还不够。原来的重开 SQL 是
    `UPDATE ... SET owner = ?`，所以 B 打一次流之后那行就变成 B 的了，A 之后连
    自己的 `GET /runs/{id}` 都是 404——一次"请求被允许"变成了数据归属的静默转移。
    """
    alice, bob = _register("steal_alice"), _register("steal_bob")
    with closing(runs_module._connect()) as conn, conn:
        conn.execute(
            "INSERT INTO runs (run_id, status, request, created_at, updated_at, owner) "
            "VALUES ('run-steal', 'failed', '{}', "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "'user:1')")

    response = bob.post("/api/agent/runs/stream",
                        json={"goal": "接着跑", "intent": "create",
                              "run_id": "run-steal"})
    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"

    with closing(runs_module._connect()) as conn:
        owner = conn.execute(
            "SELECT owner FROM runs WHERE run_id = 'run-steal'").fetchone()[0]
        status = conn.execute(
            "SELECT status FROM runs WHERE run_id = 'run-steal'").fetchone()[0]
    assert owner == "user:1", "别人的行不能被重开成自己的"
    assert status == "failed", "被拒绝的请求也不该把状态改成 running"
