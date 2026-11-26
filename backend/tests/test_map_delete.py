"""删除导图：`DELETE /api/mindmaps/{id}`。

这个文件的第一条用例在**实现之前**是 `xfail` 的（写用户手册时实测到 405，见
`docs/USER-GUIDE.md` 第 9 节第 1 条）。它钉住的不是"函数在不在"，而是**用户能不
能删掉自己攒下的那张图**——`repository` 里没有删除、路由里没有 DELETE，用户没有
任何一条路径能回收那张试手气生成的图。

剩下的用例钉四件事，每一件都是"取消注释就会踩到"的那种：

1. **账本跟着走**：`edit_events` 里 `map_id` 的那些行必须一起消失（判据写在
   `repository.delete_map` 的注释里，其中最硬的一条是 `edits.health()` 的
   `COUNT(DISTINCT map_id)` 会把删掉的图算进"昨日编辑过的图数"）。
2. **非 owner 是 403 不是 404**：读是共享的，他看得见这张图，所以"不存在"是谎话。
3. **204 不带 body**：删掉的东西没有可回报的形状。
4. **删不掉别人的图上，谁的账本都不动**：这是第 1 条的反面——一次被拒的删除不能
   留下半截副作用。
"""
from __future__ import annotations

from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.mindmaps import edits, repository

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


def _create(client: TestClient, title: str = "要删的图") -> dict:
    response = client.post("/api/mindmaps", json={"content": _tree(title)})
    assert response.status_code == 201, response.text
    return response.json()


def _event(map_id: str) -> dict:
    return {"actor": "user", "kind": "remove", "node_id": "c", "at": "2026-11-26T10:00:00Z"}


def _count_events(map_id: str) -> int:
    with closing(repository.connect()) as conn, conn:
        row = conn.execute("SELECT COUNT(*) FROM edit_events WHERE map_id = ?",
                           (map_id,)).fetchone()
    return int(row[0])


@pytest.mark.xfail(strict=True, reason="现状：DELETE /api/mindmaps/{id} 没有实现（实测 405）")
def test_a_user_can_delete_a_map_they_created(guest_client: TestClient):
    """缺陷一的现状探针：修完之后 `xfail` 必须摘掉（strict 会以 XPASS 报错提醒）。"""
    created = _create(guest_client)
    response = guest_client.delete(f"/api/mindmaps/{created['id']}")
    assert response.status_code == 204
