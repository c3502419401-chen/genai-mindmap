"""`edit_events.parent_id` 这一列：**"用户新加的节点挂在哪"从猜变成可判**。

这一组用例存在的理由是一句读数：改动日志跑了几周，`edit_events` 里攒了 565 条——
`update` 559 / `style` 4 / `remove` 2，而 **`add` 零条**。不是用户从没加过节点，
是"加节点"这件事从来没被记成一行可判的事实。于是任何"这个新节点该挂哪 / 它讲的
东西和哪条论证线同源"的判断，都只能靠猜。

所以这里守四件事：

  1. **来源**：`parent_id` 从保存那一刻的**树**算（`edits.parents_of`），不从请求体
     读一个前端算好的值——树是权威，事件是描述。这条同时让它不必动契约
     （`EditEventIn` 是 `extra="forbid"`，加字段要重导 `openapi.json`）。
  2. **口径**：三种图型各是什么。鱼骨的六根骨 / 流程的各阶段**自己是根**——它们
     在画布上挂在主骨/阶段容器上，但那是 `FishboneBone` / `FlowStage` 的关系，
     不是 `Node` 的父子关系。与前端 `edit-log.ts::indexForest` 逐条对齐。
  3. **端到端**：真发一次 `PUT /mindmaps/{id}`、带一条真实 `add` 事件，从库里读回来
     的 `parent_id` 就是那个父节点的 id。这是"补上之后能不能读"的唯一诚实判据。
  4. **老库**：列是在表已经存在之后加的，`CREATE TABLE IF NOT EXISTS` 不会补列——
     补不上就 `INSERT ... no such column`，被 `record` 自己吞掉，于是**所有事件
     静默丢失**，`total` 恒为 0，看着和"没人改过"一模一样。

刻意不测的：`parent_id` 进不进 `GET /edits` 的响应。**它不进**——`EditEventRow` 是
响应模型，给它加字段就要重导 `openapi.json` 与 `schema.d.ts`。这一列目前只有一条库内
读路径（`edits.parent_ids`），而那一条**零产品调用方**：`link.py` 并不读它（口径见
那个函数的 docstring，那半句"服务 link.py 的候选"已经改正）。
"""
from __future__ import annotations

import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.mindmaps import edits
from app.mindmaps import repository
from app.mindmaps.router import router as mindmap_router

api = FastAPI()
api.include_router(mindmap_router, prefix="/api")
client = TestClient(api)

#: 一棵最小的树：`root → a → b`。与 `test_edit_events.py::CONTENT` 同一份形状，
#: 好让"同一棵树在前端 emit 出的 parentId 与后端推导的一致"这件事能被对照着读。
CONTENT = {
    "title": "示例",
    "root": {
        "id": "root",
        "label": "主题",
        "children": [
            {"id": "a", "label": "子节点", "children": [
                {"id": "b", "label": "孙节点", "children": []},
            ]},
        ],
    },
}

#: 同一棵树，只是在 `a` 下面新加了一个 `c`——「用户手工加了一个节点」的最短复现。
CONTENT_PLUS_C = {
    "title": "示例",
    "root": {
        "id": "root",
        "label": "主题",
        "children": [
            {"id": "a", "label": "子节点", "children": [
                {"id": "b", "label": "孙节点", "children": []},
                {"id": "c", "label": "新加的节点", "children": []},
            ]},
        ],
    },
}

FISHBONE = {
    "title": "鱼骨",
    "diagram_type": "fishbone",
    "problem": "为什么报销要 11 天",
    "bones": [
        {"category": "人", "root": {"id": "p", "label": "人", "children": [
            {"id": "p1", "label": "审批人不在", "children": []},
        ]}},
        {"category": "机", "root": {"id": "m", "label": "机", "children": []}},
    ],
}

FLOW = {
    "title": "流程",
    "diagram_type": "flowchart",
    "stages": [
        {"id": "s1", "root": {"id": "st1", "label": "提交", "children": [
            {"id": "st1a", "label": "填单", "children": []},
        ]}},
    ],
}


def _create(content: dict = CONTENT) -> str:
    response = client.post("/api/mindmaps", json={"content": content})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _add_event(node_id: str, label: str, depth: int = 3) -> dict:
    return {
        "actor": "user",
        "kind": "add",
        "node_id": node_id,
        "after": label,
        "depth": depth,
        "at": "2026-11-24T09:00:00.000Z",
    }


def _raw_parent(map_id: str, node_id: str) -> str | None:
    """直接从库里读一列。**不经过 `GET /edits`**——那一层按响应模型过滤，读不到它。"""
    with repository.connect() as conn:
        row = conn.execute(
            "SELECT parent_id FROM edit_events WHERE map_id = ? AND node_id = ? "
            "ORDER BY id DESC LIMIT 1", (map_id, node_id),
        ).fetchone()
    assert row is not None, f"没有 node_id={node_id} 的事件"
    return row[0]


# --------------------------------------------------------------- 来源：从树推导


def test_parents_of_a_tree():
    from app.contracts.mindmap import MindMapContent

    parents = edits.parents_of(MindMapContent.model_validate(CONTENT))

    assert parents == {"root": None, "a": "root", "b": "a"}


def test_fishbone_bones_are_their_own_roots():
    """六根骨**自己是根**：它在画布上挂在主骨上，但那是骨的关系，不是节点的父子。"""
    from app.contracts.mindmap import FishboneContent

    parents = edits.parents_of(FishboneContent.model_validate(FISHBONE))

    assert parents == {"p": None, "p1": "p", "m": None}


def test_flow_stages_are_their_own_roots():
    from app.contracts.mindmap import FlowContent

    parents = edits.parents_of(FlowContent.model_validate(FLOW))

    assert parents == {"st1": None, "st1a": "st1"}


# --------------------------------------------------------------- 端到端：写一条真 add


def test_add_event_records_where_the_new_node_was_attached():
    """真实保存一次，从库里读回来的 `parent_id` 就是 `c` 挂的那个父节点 `a`。"""
    map_id = _create()
    response = client.put(
        f"/api/mindmaps/{map_id}",
        json={
            "content": {**CONTENT_PLUS_C, "title": "示例（改过）"},
            "version": 1,
            "events": [_add_event("c", "新加的节点")],
        },
    )
    assert response.status_code == 200, response.text

    assert _raw_parent(map_id, "c") == "a"
    # 同一份事实的稳定读路径：`(kind, node_id, parent_id)`。
    assert edits.parent_ids(map_id) == [("add", "c", "a")]


def test_a_new_first_level_branch_records_the_center_topic():
    """新加的一级分支 → 父是中心主题（`root` 那个节点 id），不是一个 `null`。

    这条与下面那条 `remove` 成对读：`NULL` 只表示"这个节点不在树上了"，不表示
    "它是根"以外的东西——树只有一个根，加不出第二个。
    """
    map_id = _create()
    content = {
        "title": "示例",
        "root": {
            "id": "root",
            "label": "主题",
            "children": [
                {"id": "a", "label": "子节点", "children": []},
                {"id": "d", "label": "另一个一级分支", "children": []},
            ],
        },
    }
    response = client.put(
        f"/api/mindmaps/{map_id}",
        json={"content": content, "version": 1, "events": [_add_event("d", "另一个一级分支", 2)]},
    )
    assert response.status_code == 200, response.text

    assert _raw_parent(map_id, "d") == "root"


def test_remove_has_no_parent_because_the_node_is_gone():
    """删掉的节点不在树上了 → `NULL`。那不是"删除前的父"，要那个得另记一列。"""
    map_id = _create()
    content = {
        "title": "示例",
        "root": {"id": "root", "label": "主题", "children": [
            {"id": "a", "label": "子节点", "children": []},
        ]},
    }
    response = client.put(
        f"/api/mindmaps/{map_id}",
        json={"content": content, "version": 1,
              "events": [{"actor": "user", "kind": "remove", "node_id": "b",
                          "before": "孙节点", "depth": 3, "subtree_size": 1,
                          "at": "2026-11-24T09:00:00.000Z"}]},
    )
    assert response.status_code == 200, response.text

    assert _raw_parent(map_id, "b") is None
    # 读路径把它滤掉：`parent_ids` 只给"放置动作"的样本，NULL 的没有信息量。
    assert edits.parent_ids(map_id) == []


def test_update_also_carries_the_parent_it_belongs_to():
    """同一条事实（这个节点在谁下面），只是动作不同：改文字时它也有父。"""
    map_id = _create()
    response = client.put(
        f"/api/mindmaps/{map_id}",
        json={"content": CONTENT, "version": 1,
              "events": [{"actor": "user", "kind": "update", "field": "label",
                          "node_id": "b", "before": "孙节点", "after": "孙",
                          "depth": 3, "at": "2026-11-24T09:00:00.000Z"}]},
    )
    assert response.status_code == 200, response.text

    assert _raw_parent(map_id, "b") == "a"
    # 缺省 `kinds` 只取放置动作，update 不进来——它回答的是另一个问题。
    assert edits.parent_ids(map_id) == []
    assert edits.parent_ids(map_id, kinds=("update",)) == [("update", "b", "a")]


# --------------------------------------------------------------- 老库：列是后加的


def test_existing_table_gets_the_parent_column(tmp_path, monkeypatch):
    """旧表（没有 `parent_id`）连一次之后就有了，且老行是 NULL、数据一条不少。

    这条与 `test_edit_events.py::test_existing_table_gets_the_new_columns` 是同一
    件事的另一半：那边守的是仓储层的三列，这边守的是**后来在 `edits` 层补的那一列**
    （仓储层正在被别人改，这一列不能等）。
    """
    legacy = tmp_path / "legacy.db"
    with sqlite3.connect(legacy) as conn:
        conn.execute("""CREATE TABLE edit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, map_id TEXT NOT NULL, run_id TEXT,
            actor TEXT NOT NULL, kind TEXT NOT NULL, node_id TEXT, field TEXT,
            before TEXT, after TEXT, depth INTEGER, at TEXT NOT NULL,
            recorded_at TEXT NOT NULL)""")
        conn.execute("INSERT INTO edit_events (map_id, actor, kind, at, recorded_at) "
                     "VALUES ('old', 'user', 'add', 'x', 'x')")
    monkeypatch.setenv("MINDMAP_DB_PATH", str(legacy))

    with repository.connect() as conn:
        assert "parent_id" not in {row[1] for row in conn.execute("PRAGMA table_info(edit_events)")}
        edits.ensure_schema(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(edit_events)")}
        kept = conn.execute("SELECT COUNT(*) FROM edit_events").fetchone()[0]
        # 幂等：第二次不抛、不重建。
        edits.ensure_schema(conn)
        again = {row[1] for row in conn.execute("PRAGMA table_info(edit_events)")}

    assert "parent_id" in columns
    assert columns == again
    # 不做迁移：老行还是 NULL，而 NULL 在这里有确切含义（"当时没记"）。
    assert kept == 1
    with repository.connect() as conn:
        assert conn.execute("SELECT parent_id FROM edit_events").fetchone()[0] is None


def test_parent_ids_are_scoped_to_one_map():
    """样本按图隔离：查这张图不会捞到另一张图的放置动作。

    它是"这个用户的历史"能不能用的前提——第一版缺的正是这个（库里 `add` 零条），
    但列修好之后，这个过滤器必须一开始就是对的，否则第一批样本就会互相污染。
    """
    first = _create()
    second = _create()
    for map_id in (first, second):
        response = client.put(
            f"/api/mindmaps/{map_id}",
            json={"content": CONTENT_PLUS_C, "version": 1,
                  "events": [_add_event("c", "新加的节点")]},
        )
        assert response.status_code == 200, response.text

    assert edits.parent_ids(first) == [("add", "c", "a")]
    assert edits.parent_ids(second) == [("add", "c", "a")]

    with repository.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM edit_events WHERE kind = 'add'"
                            ).fetchone()[0] == 2


# --------------------------------------------------------------- 契约没变


def test_response_shape_is_unchanged():
    """`GET /edits` 的字段集与加这一列之前**逐字相同**。

    这一列故意不进响应模型：`EditEventRow` 是接口响应，给它加字段就要重导
    `openapi.json` 与 `schema.d.ts`（另一条线正在改那两个文件）。把它钉成用例，
    免得下次有人"顺手"加进去而没重导快照。
    """
    map_id = _create()
    client.put(
        f"/api/mindmaps/{map_id}",
        json={"content": CONTENT_PLUS_C, "version": 1,
              "events": [_add_event("c", "新加的节点")]},
    )
    rows = client.get(f"/api/mindmaps/{map_id}/edits").json()
    assert set(rows[-1]) == {
        "id", "map_id", "run_id", "actor", "kind", "node_id", "field", "before",
        "after", "depth", "subtree_size", "change_count", "node_count", "at",
        "recorded_at",
    }
