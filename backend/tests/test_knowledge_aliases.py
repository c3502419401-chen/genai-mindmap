"""实体别名的产品入口：列、删一条、清空。

别名是模型推断出来的合并结果（"这两个名字指同一个东西"），猜错之后 `neighbors` 会给出
几条**有证据、有位置、还通得过校验**的假路径。`graph_store.clear_aliases` 早就写过，
但产品里没有任何入口能到达它——这一组用例钉住的是那条路真的通了。

三件事分开测，因为它们的失败方式完全不同：

  1. 图存储没开（没配 `MINDMAP_NEO4J_PASSWORD`）：读接口回空列表 + `configured=false`，
     写接口回 503。**不能混成 404**：一个是"这一层没启用"，一个是"这条别名不在"，
     前者要去配环境，后者要去核对合并结果。
  2. 删除成功：只删指定的那一对，返回真删掉的条数。
  3. 清空：`deleted` 是清空前的条数。

全部离线：Neo4j 那两层（真语句、真 MERGE 语义）由 `tests/test_graph_resolve.py` 的
假事务覆盖；这里测的是**路由层的翻译**，所以把 `graph_store` 的三个函数换掉。
"""
from __future__ import annotations

from conftest import guest_client

from app.knowledge import graph_store


def test_alias_listing_says_the_graph_layer_is_off():
    """没配图存储：200 + 空列表 + `configured=false`。

    返回 5xx 会让整个知识库面板打不开，而"知识库还能看"与"图存储开没开"是两件事。
    """
    api = guest_client()
    with api:
        response = api.get("/api/knowledge/aliases")

    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "total": 0, "configured": False}


def test_alias_listing_reports_edges_and_their_basis(monkeypatch):
    """配置了就把条目连同落库依据一起给出来。"""
    monkeypatch.setattr(graph_store, "configured", lambda: True)
    monkeypatch.setattr(graph_store, "list_aliases", lambda *, limit=500: [
        graph_store.AliasEdge(alias="甲", canonical="甲全称", source="substring",
                              similarity=0.9, created_at="2026-01-01T00:00:00.000Z"),
    ])

    api = guest_client()
    with api:
        response = api.get("/api/knowledge/aliases")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1 and body["configured"] is True
    assert body["items"][0]["alias"] == "甲"
    # 依据必须在响应里：界面靠它说"这条是凭构词关系合的 / 凭向量兜底合的"。
    assert body["items"][0]["source"] == "substring"
    assert body["items"][0]["similarity"] == 0.9


def test_delete_alias_without_graph_is_503_not_404(monkeypatch):
    """没配置图存储时删除是 503。回 404 会让用户以为"这条别名已经不在了"。"""
    api = guest_client()
    with api:
        response = api.delete("/api/knowledge/aliases/甲")

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "graph_unavailable"


def test_delete_alias_that_does_not_exist_is_404(monkeypatch):
    monkeypatch.setattr(graph_store, "configured", lambda: True)
    monkeypatch.setattr(graph_store, "delete_alias", lambda alias, canonical=None: 0)

    api = guest_client()
    with api:
        response = api.delete("/api/knowledge/aliases/不存在的别名")

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"


def test_delete_alias_passes_the_canonical_through(monkeypatch):
    """`canonical` 要原样传到图存储层：它是"只删这一对"与"删这个别名的全部指向"的分界。"""
    seen: list[tuple[str, str | None]] = []
    monkeypatch.setattr(graph_store, "configured", lambda: True)
    monkeypatch.setattr(graph_store, "delete_alias",
                        lambda alias, canonical=None: seen.append((alias, canonical)) or 1)

    api = guest_client()
    with api:
        response = api.delete("/api/knowledge/aliases/甲?canonical=甲全称")

    assert response.status_code == 200, response.text
    assert response.json() == {"alias": "甲", "deleted": 1}
    assert seen == [("甲", "甲全称")]


def test_clear_aliases_returns_the_deleted_count(monkeypatch):
    """清空是"这一批合并全错了"的退路；回执里的数就是清空前的条数。"""
    monkeypatch.setattr(graph_store, "clear_aliases", lambda: 7)

    api = guest_client()
    with api:
        response = api.delete("/api/knowledge/aliases")

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": 7}
