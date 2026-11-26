"""删除导图：`DELETE /api/mindmaps/{id}`。

写用户手册时实测到这个端点返回 **405**，而库里有 24 张图是"试几个主题"留下的
（其中一张就是写手册时生成的）。没有删除入口的列表等于让用户攒垃圾数据，而他
连自己攒了都不知道——列表里没有任何一处提示"这些删得掉"。

这些用例钉四件事，每一件都是"不看就会踩到"的那种：

1. **账本跟着走**：`edit_events` 里 `map_id` 的那些行必须一起消失。判据写在
   `repository.delete_map` 的注释里，其中最硬的一条是 `edits.health()` 的
   `COUNT(DISTINCT map_id)`——留着孤儿行，"昨日编辑过的图数"会把一张已经删掉的
   图算进分母，而那个数本来是用来判断"这张表还在工作吗"的。
2. **非 owner 是 403 不是 404**：读是共享的，他看得见这张图，所以"不存在"是谎话。
3. **被拒的删除不留半截副作用**：账本一行都不许少。
4. **204 不带 body**：删掉的东西没有可回报的形状。
"""
from __future__ import annotations

from contextlib import closing

from fastapi.testclient import TestClient

from app.main import app
from app.mindmaps import edits, repository
from conftest import guest_client

PASSWORD = "password123"


def _register(username: str) -> TestClient:
    client = TestClient(app)
    response = client.post("/api/auth/register",
                           json={"username": username, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return client


def _tree(title: str = "要删的图") -> dict:
    return {"title": title,
            "root": {"id": "r", "label": title,
                     "children": [{"id": "c", "label": "子", "children": []}]}}


def _event() -> dict:
    return {"actor": "user", "kind": "remove", "node_id": "c", "at": "2026-11-26T10:00:00Z"}


def _create(client: TestClient, title: str = "要删的图", *, with_event: bool = True) -> dict:
    """建一张图。`with_event=True` 时顺带写一条编辑事件（那才是"账本里有东西"的图）。"""
    body: dict = {"content": _tree(title)}
    if with_event:
        body["events"] = [_event()]
    response = client.post("/api/mindmaps", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _count_events(map_id: str) -> int:
    with closing(repository.connect()) as conn, conn:
        row = conn.execute("SELECT COUNT(*) FROM edit_events WHERE map_id = ?",
                           (map_id,)).fetchone()
    return int(row[0])


def _exists(map_id: str) -> bool:
    return repository.get_map(map_id) is not None


# ---------------------------------------------------------------- 删得掉


def test_a_user_can_delete_a_map_they_created():
    """主路径：建、删、没了。204 且不带 body。"""
    client = guest_client()
    created = _create(client)

    response = client.delete(f"/api/mindmaps/{created['id']}")

    assert response.status_code == 204
    assert response.content == b"", "204 不该带 body"
    assert not _exists(created["id"])
    assert client.get(f"/api/mindmaps/{created['id']}").status_code == 404


def test_deleting_a_map_takes_its_edit_ledger_with_it():
    """账本一起删。留着它只会让 `health()` 的分母把删掉的图算进去。"""
    client = guest_client()
    created = _create(client)
    assert _count_events(created["id"]) == 1, "先证明账本里真的有东西"

    assert client.delete(f"/api/mindmaps/{created['id']}").status_code == 204

    assert _count_events(created["id"]) == 0


def test_other_maps_and_their_ledgers_are_untouched():
    """删一张不动另一张——包括另一张的账本。

    这一条防的是"DELETE 写成了不带 WHERE"那种最贵的错法：它会让整个导图库消失，
    而所有"删完之后还有东西"的断言都会在第一条上就通过。
    """
    client = guest_client()
    doomed = _create(client, "要删的")
    kept = _create(client, "留着的")

    assert client.delete(f"/api/mindmaps/{doomed['id']}").status_code == 204

    assert _exists(kept["id"])
    assert _count_events(kept["id"]) == 1


def test_deleting_the_same_map_twice_is_404():
    """第二次是 404 而不是 204：重复删除不该被当成成功（界面会据此判断要不要从列表里拿掉）。"""
    client = guest_client()
    created = _create(client)
    assert client.delete(f"/api/mindmaps/{created['id']}").status_code == 204
    assert client.delete(f"/api/mindmaps/{created['id']}").status_code == 404


# ---------------------------------------------------------------- 权限


def test_only_the_creator_may_delete_a_map():
    """写限主：B 删 A 的图是 **403** `map_forbidden`，不是 404。

    404 会让 B 以为这张图已经被别人删了（或自己记错了 id）；而它明明还在
    `GET /api/mindmaps` 里、点开还能看。读是共享的，所以这条谎话不成立。
    """
    alice, bob = _register("del_alice"), _register("del_bob")
    created = _create(alice)

    response = bob.delete(f"/api/mindmaps/{created['id']}")

    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "map_forbidden"
    # 文案要给下一步：删不掉的人仍然可以导出或另存。
    assert "创建者" in body["message"]
    assert "导出" in body["message"] or "另存为" in body["message"]
    # A 自己删还是好的（证明 403 挡的是"别人"，不是"谁都不行"）。
    assert alice.delete(f"/api/mindmaps/{created['id']}").status_code == 204


def test_a_forbidden_delete_does_not_touch_the_ledger():
    """被拒的删除不留半截副作用：账本一行都不许少。

    这是上面那条的**反面判据**，比"图还在"更难成立——删除的实现里账本与图是两条
    `DELETE`，判据一旦写在两条之间，B 的一次尝试就能把 A 的账本清空，而图还在。
    """
    alice, bob = _register("led_a"), _register("led_b")
    created = _create(alice)

    assert bob.delete(f"/api/mindmaps/{created['id']}").status_code == 403

    assert _exists(created["id"])
    assert _count_events(created["id"]) == 1


def test_an_unowned_legacy_row_is_deletable_by_the_deployer_only():
    """`owner=''` 的历史行：与保存同一条判据，只有部署方能删。

    删除比保存更需要这一条：改坏了还能再改，删掉了（连同账本）谁都救不回来。
    """
    legacy_id = "legacy-del"
    with closing(repository.connect()) as conn, conn:
        conn.execute("INSERT INTO mindmaps (id, version, content, updated_at) "
                     "VALUES (?, 1, ?, '2026-01-01T00:00:00Z')",
                     (legacy_id, '{"title":"旧","root":{"id":"r","label":"旧","children":[]}}'))
    deployer = _register("del_first")
    late = _register("del_late")

    assert late.delete(f"/api/mindmaps/{legacy_id}").status_code == 403
    assert deployer.delete(f"/api/mindmaps/{legacy_id}").status_code == 204


# ---------------------------------------------------------------- 与账本统计的关系


def test_deleting_a_map_leaves_the_health_probe_working():
    """删完之后 health 探测器照常工作，且不再把那张图算进去。

    `health()` 是死表探测器（"昨日编辑过的图数 / 昨日保存的图数"）。账本一起删的
    可观测后果就在这里：账本里不能留下任何指向已删图的行。
    """
    client = guest_client()
    created = _create(client)
    edits.health()  # 删之前也不许炸

    assert client.delete(f"/api/mindmaps/{created['id']}").status_code == 204

    assert _count_events(created["id"]) == 0
    edits.health()
