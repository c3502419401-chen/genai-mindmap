"""编辑日志：一张表、两条读路径，以及这一版真正要回答的那个问题。

用例分成五组，各验一件不同的事：

  1. **落库**：保存时带上去的事件真的进了 `edit_events`，`GET /edits` 读得回来；
  2. **统计**：`by_actor` / `by_kind` / `by_field` 的数字，以及两条恒等式
     （`sum(by_actor) == total`、`sum(by_field) == update + style`）；
  3. **三个关键指标**：`label_shrunk`（只算 user）、`removed_depth`（只算零散删除）、
     `removed_subtree`（删除规模直方图）——它们直接对应"生成侧该改什么"，数字错一位，
     结论就指向错误的地方；
  4. **两处"分母"**：`generate` 事件（连"一字未改"也记，`node_count` 是分母）与
     `last_event_at`（空表是 null，不是空串）——没有它们，统计只剩"改过的那些"；
     外加"分母换人"之后的两条新口径：`changed_events` 与 `untouched_runs`；
  5. **边界**：超量 422、字段名漂移 422、非法时间戳退化、409 时不落事件、
     以及"事件写失败不能让保存失败"。

最后一条单独说：它是刻意的取舍，不是容错偷懒。保存的语义是"用户的内容没丢"，
为了统计丢掉一次真实保存，代价方向完全错了。同一个方向还有一条：
`test_existing_table_gets_the_new_columns`——旧表补不上列时，`record()` 的异常被
自己吞掉，事件会**全部静默丢失**，那正是这张表最擅长的那种坏法。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.contracts.edits import MAX_EVENTS_PER_SAVE, EditEventIn
from app.core.errors import register_error_handlers
from app.mindmaps import edits
from app.mindmaps import repository
from app.mindmaps.router import router as mindmap_router

api = FastAPI()
register_error_handlers(api)
api.include_router(mindmap_router, prefix="/api")
client = TestClient(api)

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


def _create(events: list[dict] | None = None, truncated: int = 0) -> str:
    body: dict = {"content": CONTENT}
    if events is not None:
        body["events"] = events
    if truncated:
        body["truncated"] = truncated
    response = client.post("/api/mindmaps", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _event(**overrides) -> dict:
    """一条"用户把长标题改短"的事件。各用例只覆盖自己关心的字段。"""
    base = {
        "actor": "user",
        "kind": "update",
        "field": "label",
        "node_id": "a",
        "before": "长" * 18,
        "after": "短" * 9,
        "depth": 2,
        "at": "2026-11-17T09:31:02.123Z",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- 落库


def test_events_are_stored_and_read_back():
    map_id = _create([
        _event(),
        _event(kind="style", field="color", before="无", after="red"),
    ])

    rows = client.get(f"/api/mindmaps/{map_id}/edits").json()

    assert len(rows) == 2
    assert {row["kind"] for row in rows} == {"update", "style"}
    assert all(row["map_id"] == map_id for row in rows)
    # `at` 是前端给的时刻，`recorded_at` 是入库时刻——两者分开才诊断得出时钟漂移。
    assert rows[0]["at"] == "2026-11-17T09:31:02.123Z"
    assert rows[0]["recorded_at"].endswith("Z")
    assert rows[1]["id"] == 2


def test_saving_without_events_writes_nothing():
    map_id = _create()
    assert client.get(f"/api/mindmaps/{map_id}/edits").json() == []
    assert client.get(f"/api/mindmaps/{map_id}/edits/summary").json()["total"] == 0


def test_update_appends_events_to_the_same_map():
    map_id = _create([_event()])
    response = client.put(
        f"/api/mindmaps/{map_id}",
        json={"content": {**CONTENT, "title": "改过的标题"}, "version": 1,
              "events": [_event(field="title", node_id=None, before="示例", after="改过的标题")]},
    )
    assert response.status_code == 200, response.text

    rows = client.get(f"/api/mindmaps/{map_id}/edits").json()
    assert [row["field"] for row in rows] == ["label", "title"]


def test_recent_returns_newest_first_and_respects_limit():
    map_id = _create([
        _event(at="2026-11-17T09:00:00.000Z", before="一", after="二"),
        _event(at="2026-11-17T10:00:00.000Z", before="三", after="四"),
        _event(at="2026-11-17T11:00:00.000Z", before="五", after="六"),
    ])
    rows = client.get(f"/api/mindmaps/{map_id}/edits?limit=2").json()
    assert [row["at"][11:13] for row in rows] == ["11", "10"]


# ---------------------------------------------------------------- 统计


def test_summary_counts_by_actor_kind_and_field():
    map_id = _create([
        _event(),
        _event(node_id="b"),
        _event(node_id="root"),
        _event(kind="style", field="color", before="无", after="blue"),
        _event(kind="add", field=None, node_id="c", before=None, after="新节点", depth=2),
        _event(actor="agent", kind="remove", field=None, node_id="b", before="孙节点",
               after=None, depth=3),
    ])

    summary = client.get(f"/api/mindmaps/{map_id}/edits/summary").json()

    assert summary["total"] == 6
    assert summary["by_actor"] == {"user": 5, "agent": 1}
    assert summary["by_kind"] == {"update": 3, "style": 1, "add": 1, "remove": 1}
    # `field` 只在 update / style 上有值：add / remove 不进 by_field。
    assert summary["by_field"] == {"label": 3, "color": 1}
    # 两条恒等式：对不上就说明有写入路径绕过了契约。
    assert sum(summary["by_actor"].values()) == summary["total"]
    assert sum(summary["by_field"].values()) == (
        summary["by_kind"].get("update", 0) + summary["by_kind"].get("style", 0))


def test_label_shrunk_counts_only_shorter_labels():
    map_id = _create([
        _event(before="长" * 18, after="短" * 9),
        _event(node_id="b", before="长" * 20, after="短" * 10),
        # 变长的不算"缩短"：用户把标题写细了，是另一件事。
        _event(node_id="root", before="短" * 5, after="长" * 9),
        # 导图标题的改动混进来会稀释"节点标题被改短"这个信号。
        _event(field="title", node_id=None, before="长" * 30, after="短" * 3),
    ])

    shrunk = client.get(f"/api/mindmaps/{map_id}/edits/summary").json()["label_shrunk"]

    assert shrunk["count"] == 2
    assert shrunk["avg_before"] == 19.0
    assert shrunk["avg_after"] == 9.5


def test_label_shrunk_averages_are_null_not_zero_when_empty():
    map_id = _create([_event(before="短标题", after="更长的标题啊")])
    shrunk = client.get(f"/api/mindmaps/{map_id}/edits/summary").json()["label_shrunk"]
    # 0 会被读成"平均改成 0 字"，那是一次完全不同的改动。
    assert shrunk == {"count": 0, "avg_before": None, "avg_after": None}


def test_removed_depth_groups_by_level():
    """`removed_depth` 只统计**零散删除**（`subtree_size = 1`）。

    一条 remove 现在代表"一整棵被删的子树"，`depth` 是它的根所在的层——根在第 2 层
    不等于"第 2 层的内容被嫌弃"。所以整支被删（size 12）与当时没记（NULL）都不进
    这个分布，各走 `removed_subtree` 与 `size_unknown`。
    """
    map_id = _create([
        _event(kind="remove", field=None, node_id="x", before="甲", after=None,
               depth=2, subtree_size=1),
        _event(kind="remove", field=None, node_id="y", before="乙", after=None,
               depth=2, subtree_size=1),
        _event(kind="remove", field=None, node_id="z", before="丙", after=None,
               depth=3, subtree_size=1),
        # 整支一起删：一条事件、12 个节点。根落在第 2 层，但它不是"零散删除"。
        _event(kind="remove", field=None, node_id="big", before="大块", after=None,
               depth=2, subtree_size=12),
        # 加列之前的老数据：不知道是零散还是整支，只能不进这个分布。
        _event(kind="remove", field=None, node_id="old", before="旧的", after=None, depth=4),
        # 别的 kind 的 depth 不算进"删在第几层"。
        _event(kind="update", depth=4),
    ])

    summary = client.get(f"/api/mindmaps/{map_id}/edits/summary").json()

    assert summary["removed_depth"] == {"2": 2, "3": 1}


def test_removed_subtree_groups_by_size_and_keeps_unknown_separate():
    """删除规模直方图：零散删除、整支删除、当时没记的老数据三组互不混淆。

    键序固定（规模升序、`unknown` 最后）：抖动的统计没法对比"这周和上周"。
    """
    map_id = _create([
        _event(kind="remove", field=None, node_id="a", before="甲", after=None,
               depth=4, subtree_size=12),
        _event(kind="remove", field=None, node_id="b", before="乙", after=None,
               depth=2, subtree_size=1),
        _event(kind="remove", field=None, node_id="c", before="丙", after=None,
               depth=2, subtree_size=1),
        _event(kind="remove", field=None, node_id="d", before="丁", after=None, depth=3),
    ])

    summary = client.get(f"/api/mindmaps/{map_id}/edits/summary").json()

    assert summary["removed_subtree"] == {
        "size_1": 2, "size_12": 1, "size_unknown": 1,
    }
    assert list(summary["removed_subtree"]) == ["size_1", "size_12", "size_unknown"]
    # 老数据只进 size_unknown：否则"每层都在被删"那次污染还在。
    assert summary["removed_depth"] == {"2": 2}


def test_subtree_size_is_stored_and_zero_is_rejected():
    map_id = _create([_event(kind="remove", field=None, node_id="a", before="甲",
                             after=None, depth=2, subtree_size=12)])
    row = client.get(f"/api/mindmaps/{map_id}/edits").json()[0]
    assert row["subtree_size"] == 12
    # 非 remove 的事件没有这一位。
    assert client.get("/api/mindmaps").status_code == 200
    assert row["change_count"] is None

    response = client.post(
        "/api/mindmaps",
        json={"content": CONTENT,
              "events": [_event(kind="remove", field=None, node_id="a", after=None,
                                depth=2, subtree_size=0)]},
    )
    # "删掉 0 个节点"没有意义：那是一条形状错误的事件，不是一条可用的数据。
    assert response.status_code == 422


def test_accept_event_records_zero_changes():
    """`accept` + `change_count=0` 就是"一字未改"。

    这是**候选层还在时**的分母。候选层删掉之后新数据一律写 `generate`，这条留着
    守两件事：历史行读得回来；老前端还在发 `accept` 时不 422（那条路会让一次真实
    保存失败——为了契约整洁丢掉用户的内容，代价方向错了）。
    """
    map_id = _create([_event(
        actor="user", kind="accept", field=None, node_id=None,
        before=None, after=None, depth=None, change_count=0,
    )])

    row = client.get(f"/api/mindmaps/{map_id}/edits").json()[0]
    assert row["kind"] == "accept"
    assert row["change_count"] == 0
    assert row["node_id"] is None and row["field"] is None and row["depth"] is None


def test_accept_keeps_both_identities_intact():
    """`accept` 没有 field、没有 depth，不该扰动任何一条自校验恒等式。"""
    map_id = _create([
        _event(),
        _event(kind="style", field="color", before="无", after="red"),
        _event(actor="user", kind="accept", field=None, node_id=None, before=None,
               after=None, depth=None, change_count=2),
    ])

    summary = client.get(f"/api/mindmaps/{map_id}/edits/summary").json()

    assert summary["by_kind"]["accept"] == 1
    assert summary["total"] == 3
    assert sum(summary["by_actor"].values()) == summary["total"]
    assert sum(summary["by_field"].values()) == (
        summary["by_kind"].get("update", 0) + summary["by_kind"].get("style", 0))
    # accept 不进 removed_depth / label_shrunk：它既不是删除也不是改标题。
    assert summary["removed_depth"] == {}
    assert summary["label_shrunk"]["count"] == 1


# ---------------------------------------------------------------- generate：新的分母


def _generate(run_id: str, node_count: int, **overrides) -> dict:
    """一次"生成落进画布"的事件：没有节点、没有字段，只有规模与 run。"""
    base = {
        "actor": "agent", "kind": "generate", "node_id": None, "field": None,
        "before": None, "after": None, "depth": None,
        "node_count": node_count, "run_id": run_id,
        "at": "2026-11-17T09:31:02.123Z",
    }
    base.update(overrides)
    return base


def test_generate_event_stores_node_count_and_run_id():
    map_id = _create([_generate("run-1", 42)])

    row = client.get(f"/api/mindmaps/{map_id}/edits").json()[0]

    assert row["kind"] == "generate"
    assert row["node_count"] == 42
    assert row["run_id"] == "run-1"
    # 与 accept 一样，它不是"改动"，所以这三格必须是空的——否则
    # `removed_depth` / `label_shrunk` 会被一条没有节点的行污染。
    assert row["node_id"] is None and row["field"] is None and row["depth"] is None


def test_generate_is_the_denominator():
    """分母 = `SUM(node_count) WHERE kind='generate'`，分子是节点改动条数。"""
    map_id = _create([
        _generate("run-1", 10),
        _generate("run-2", 30),
        # run-1 上确实改了两处。
        _event(actor="agent", kind="update", field="label", node_id="a",
               before="旧标题", after="新标题", run_id="run-1"),
        _event(actor="agent", kind="remove", field=None, node_id="b", before="孙节点",
               after=None, depth=3, subtree_size=1, run_id="run-1"),
        # 一条边不算节点改动：分母数的是节点，混进来比例就失去意义。
        _event(actor="agent", kind="edge", field=None, node_id="a", before=None,
               after="a → b", run_id="run-2"),
    ])

    summary = client.get(f"/api/mindmaps/{map_id}/edits/summary").json()

    assert summary["generated_runs"] == 2
    assert summary["generated_nodes"] == 40
    assert summary["changed_events"] == 2
    # run-2 只有一条 edge，没有任何节点改动 → 它是一次"一字未改"的生成。
    assert summary["untouched_runs"] == 1


def test_untouched_runs_is_every_generation_when_nothing_was_changed():
    map_id = _create([_generate("run-1", 8), _generate("run-2", 12)])

    summary = client.get(f"/api/mindmaps/{map_id}/edits/summary").json()

    assert summary["generated_nodes"] == 20
    assert summary["changed_events"] == 0
    assert summary["untouched_runs"] == 2


def test_generate_without_run_id_is_not_counted_as_untouched():
    """没有 `run_id` 的老行不是"一字未改"，是"当时没记"。

    `NULL = NULL` 在 SQL 里不成立，所以"这个 run 下有没有改动"那条 `NOT EXISTS`
    会把无 run_id 的行判成"没有改动"。不显式排掉，一次迁移就能凭空造出几十条
    "生成完一字没改"的正样本——而那正是这张表最贵的那个数。
    """
    map_id = _create([_generate(None, 5)])

    summary = client.get(f"/api/mindmaps/{map_id}/edits/summary").json()

    assert summary["generated_runs"] == 1
    assert summary["generated_nodes"] == 5
    assert summary["untouched_runs"] == 0


def test_generate_keeps_both_identities_intact():
    """`generate` 不扰动两条恒等式与三个关键指标。"""
    map_id = _create([
        _generate("run-1", 20),
        _event(),
        _event(kind="style", field="color", before="无", after="red"),
    ])

    summary = client.get(f"/api/mindmaps/{map_id}/edits/summary").json()

    assert summary["total"] == 3
    assert sum(summary["by_actor"].values()) == summary["total"]
    assert sum(summary["by_field"].values()) == (
        summary["by_kind"].get("update", 0) + summary["by_kind"].get("style", 0))
    # generate 不是删除、也没碰标题。
    assert summary["removed_depth"] == {}
    assert summary["label_shrunk"]["count"] == 1


def test_label_shrunk_counts_only_user_edits():
    """`agent` 的改动是"模型自己又改了一遍"，不该算进用户的缩短偏好。

    B 图的实测：3 条 user 的缩短被读成 4 条，多出来的那条正是 agent 的 13→10 字。
    """
    map_id = _create([
        _event(before="长" * 18, after="短" * 9),
        _event(actor="agent", node_id="b", before="长" * 13, after="短" * 10),
    ])

    shrunk = client.get(f"/api/mindmaps/{map_id}/edits/summary").json()["label_shrunk"]

    assert shrunk["count"] == 1
    assert shrunk["avg_before"] == 18.0
    assert shrunk["avg_after"] == 9.0


def test_label_shrunk_ignores_repair_edits():
    """一键修复把两层标题拼成一个，也是一条"变短的 label 改动"，但它不该算。

    修复的扳机是用户按的，所以 `actor` 是 `user`——只有 `kind='repair'` 能把
    "规则改短"与"人改短"分开。不分开，"规则把两层标题拼成一个"会被读成"用户嫌
    标题太长"，而这个数字唯一的用途就是给生成侧的标题长度定上限：拿规则自己造成
    的缩短去论证它，是循环论证。
    """
    map_id = _create([
        # 人改短一条：18 → 9。
        _event(before="长" * 18, after="短" * 9),
        # 修复把"外层 18 字"与内层拼成一个 12 字标题。同长度，只有条数能区分。
        _event(kind="repair", node_id="b", before="长" * 18, after="短" * 12),
    ])

    shrunk = client.get(f"/api/mindmaps/{map_id}/edits/summary").json()["label_shrunk"]

    # count 是 1 而不是 2：多出来的那条是规则干的。
    assert shrunk["count"] == 1
    assert shrunk["avg_before"] == 18.0
    assert shrunk["avg_after"] == 9.0


def test_by_field_ignores_repair_and_keeps_the_identity():
    """`repair` 改文字时也带 `field`，但它不进 `by_field`：那不是人改的。

    这条同时钉住那条恒等式：`repair` 一旦被算进 `by_field`，
    `sum(by_field) == by_kind.update + by_kind.style` 立刻不成立，而它是
    "有没有写入路径绕过契约"的唯一自校验。
    """
    map_id = _create([
        _event(),
        # 修复改了一个标题（带 field）……
        _event(kind="repair", node_id="b", before="长" * 10, after="短" * 4),
        # ……又删掉一个节点（不带 field，带规模）。
        _event(kind="repair", field=None, node_id="c", before="多余的", after=None,
               depth=3, subtree_size=1),
    ])

    summary = client.get(f"/api/mindmaps/{map_id}/edits/summary").json()

    assert summary["by_kind"] == {"update": 1, "repair": 2}
    assert summary["by_field"] == {"label": 1}
    assert sum(summary["by_field"].values()) == (
        summary["by_kind"].get("update", 0) + summary["by_kind"].get("style", 0))
    # 修复删掉的节点不进"用户嫌哪一层多余"：那个数问的是用户的手。
    assert summary["removed_depth"] == {}
    assert summary["removed_subtree"] == {}


def test_repair_counts_as_a_change_not_as_untouched():
    """规则修复改过节点 → 那次生成不算"一字未改"。

    `changed_events` 与 `untouched_runs` 用的是同一个 kind 集合。把 `repair` 漏在
    两边之外，"模型给了 30 个节点、规则删掉 12 个"会被读成"生成完一字未改"——而
    `untouched_runs` 是整张表最贵的那个数，误报的方向恰好是"生成质量很好"。
    """
    map_id = _create([
        _generate("run-1", 30),
        _event(kind="repair", field=None, node_id="c", before="一整支", after=None,
               depth=2, subtree_size=12, run_id="run-1"),
    ])

    summary = client.get(f"/api/mindmaps/{map_id}/edits/summary").json()

    assert summary["changed_events"] == 1
    assert summary["untouched_runs"] == 0
    assert sum(summary["by_actor"].values()) == summary["total"]


def test_last_event_at_is_the_recording_time_not_the_faked_one():
    """死表探测器的读数取**入库**时刻：`at` 由前端时钟给，可以漂到任何地方。"""
    map_id = _create([_event(at="2020-01-01T00:00:00.000Z")])

    summary = client.get(f"/api/mindmaps/{map_id}/edits/summary").json()
    row = client.get(f"/api/mindmaps/{map_id}/edits").json()[0]

    assert summary["last_event_at"] == row["recorded_at"]
    assert summary["last_event_at"] != "2020-01-01T00:00:00.000Z"
    assert summary["last_event_at"].endswith("Z")


def test_last_event_at_is_null_for_a_map_with_no_events():
    """`total=0` 与"这张图没人改过"长得一样，`last_event_at=null` 说的是另一件事。"""
    map_id = _create()

    summary = client.get(f"/api/mindmaps/{map_id}/edits/summary").json()

    assert summary["total"] == 0
    assert summary["last_event_at"] is None


# ---------------------------------------------------------------- 链路健康（死表探测器）


def _move_to_yesterday(map_id: str, *, events: bool = True) -> None:
    """把这张图（以及它的事件）挪到 UTC 昨日的中午。

    健康检查按**入库时刻**判日，所以这里改的是 `recorded_at` 而不是 `at`——改 `at`
    （前端时钟）测不出任何东西，那正是这个探测器要绕开的那一位。
    """
    stamp = None
    with repository.connect() as conn, conn:
        stamp = conn.execute("SELECT date('now','-1 day') || 'T12:00:00.000Z'").fetchone()[0]
        conn.execute("UPDATE mindmaps SET updated_at = ? WHERE id = ?", (stamp, map_id))
        if events:
            conn.execute("UPDATE edit_events SET recorded_at = ? WHERE map_id = ?",
                         (stamp, map_id))
    assert stamp is not None


def test_health_alarms_when_yesterday_had_saves_but_no_events():
    """**判据**：保存了却一条事件都没有，在正常情况下不该发生。

    每次应用候选都记一条 `accept`（连一字未改也记），所以"昨日保存 > 0 且昨日编辑
    = 0"只能是链路断了。这一条锁的正是那 40 分钟静默故障。
    """
    map_id = _create()  # 保存，但不带任何事件
    _move_to_yesterday(map_id, events=False)

    report = client.get("/api/mindmaps/edits/health").json()

    assert report["saved_maps"] == 1
    assert report["edited_maps"] == 0
    assert report["chain_suspect"] is True
    assert "日志链路可能断了" in report["detail"]


def test_health_is_clean_when_events_came_with_the_saves():
    map_id = _create([_event(actor="user", kind="accept", field=None, node_id=None,
                             before=None, after=None, depth=None, change_count=0)])
    _move_to_yesterday(map_id)

    report = client.get("/api/mindmaps/edits/health").json()

    assert report["saved_maps"] == 1
    assert report["edited_maps"] == 1
    assert report["chain_suspect"] is False
    assert "链路正常" in report["detail"]


def test_health_does_not_alarm_when_nothing_was_saved_yesterday():
    """没有保存时零事件是正常的：判据只对"有保存"的日子成立，否则每天都在报警。"""
    report = client.get("/api/mindmaps/edits/health").json()

    assert report["saved_maps"] == 0
    assert report["edited_maps"] == 0
    assert report["chain_suspect"] is False
    assert report["window_start"].endswith("T00:00:00.000Z")
    assert report["window_start"] < report["window_end"]


def test_health_route_is_not_shadowed_by_the_map_id_routes():
    """`/edits/health` 与 `/{map_id}/edits` 都是两个路径段，靠第二段区分。

    串了的话这里会拿到 404（"导图不存在"）而不是一份健康报告——而一个只会 404 的
    探测器等于没有。
    """
    response = client.get("/api/mindmaps/edits/health")
    assert response.status_code == 200, response.text
    assert {"edited_maps", "saved_maps", "chain_suspect"} <= set(response.json())


def test_summary_is_scoped_to_one_map():
    first = _create([_event()])
    second = _create([_event(), _event(node_id="b")])
    assert client.get(f"/api/mindmaps/{first}/edits/summary").json()["total"] == 1
    assert client.get(f"/api/mindmaps/{second}/edits/summary").json()["total"] == 2


# ---------------------------------------------------------------- 边界


def test_summary_404_for_unknown_map():
    assert client.get("/api/mindmaps/nope/edits/summary").status_code == 404
    assert client.get("/api/mindmaps/nope/edits").status_code == 404


def test_too_many_events_is_rejected():
    response = client.post(
        "/api/mindmaps",
        json={"content": CONTENT, "events": [_event()] * (MAX_EVENTS_PER_SAVE + 1)},
    )
    assert response.status_code == 422


def test_unknown_field_name_is_rejected():
    # `extra="forbid"`：字段名漂移必须当场 422，不能静默丢掉一条事件。
    bad = _event()
    bad["nodeId"] = bad.pop("node_id")
    response = client.post("/api/mindmaps", json={"content": CONTENT, "events": [bad]})
    assert response.status_code == 422


def test_invalid_timestamp_falls_back_to_server_clock():
    map_id = _create([_event(at="昨天下午")])
    row = client.get(f"/api/mindmaps/{map_id}/edits").json()[0]
    # 少一个准确时间戳比少一条改动可接受：`at` 退回入库时刻，而不是整批拒收。
    assert row["at"].endswith("Z")
    assert row["at"] == row["recorded_at"]


def test_conflict_does_not_write_events():
    map_id = _create([_event()])
    response = client.put(
        f"/api/mindmaps/{map_id}",
        json={"content": CONTENT, "version": 99, "events": [_event(node_id="b")]},
    )
    assert response.status_code == 409
    # 内容没落库，事件也就无处归属：一起拒，重试一次两者都补上。
    assert client.get(f"/api/mindmaps/{map_id}/edits/summary").json()["total"] == 1


def test_record_failure_does_not_fail_the_save(monkeypatch, caplog):
    """统计写失败 → 保存照常成功 + 一条 warning。

    这条用例锁的是取舍，不是容错：为了统计丢掉一次真实保存，方向是错的。
    """

    class BrokenRepository:
        @staticmethod
        def connect():
            raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(edits, "repository", BrokenRepository)
    with caplog.at_level("WARNING"):
        written = edits.record("map-1", [EditEventIn(**_event())])

    assert written == 0
    assert "编辑事件写入失败" in caplog.text


def test_truncated_count_is_accepted_and_not_stored():
    map_id = _create([_event()], truncated=7)
    summary = client.get(f"/api/mindmaps/{map_id}/edits/summary").json()
    # 截断只进服务端日志：插一条假事件会让每一个统计口径的 `total` 都变脏。
    assert summary["total"] == 1


def test_repository_creates_the_table_on_connect():
    """表在 `repository.connect()` 里建好，与 `mindmaps` / `runs` 同一时刻。

    不这样做，读统计那条路就要多一个"发现表不存在 → 建表"的分支——而它会在
    第一次有人打开面板时表现为一次 500。
    """
    with repository.connect() as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(edit_events)")]
    assert {"map_id", "run_id", "actor", "kind", "node_id", "field",
            "before", "after", "depth", "subtree_size", "change_count",
            "at", "recorded_at"} <= set(columns)


def test_existing_table_gets_the_new_columns(tmp_path, monkeypatch):
    """老库里的表**已经存在**，`CREATE TABLE IF NOT EXISTS` 不会给它补列。

    少这一步的后果不是报错，是 `edits.record()` 的 INSERT 抛 "no such column"、被它
    自己的 `except` 吞掉 → 所有事件静默丢失。这条用例先用旧 DDL 建表，再连一次。
    """
    legacy = tmp_path / "legacy.db"
    with sqlite3.connect(legacy) as conn:
        conn.execute("""CREATE TABLE edit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, map_id TEXT NOT NULL, run_id TEXT,
            actor TEXT NOT NULL, kind TEXT NOT NULL, node_id TEXT, field TEXT,
            before TEXT, after TEXT, depth INTEGER, at TEXT NOT NULL,
            recorded_at TEXT NOT NULL)""")
        conn.execute("INSERT INTO edit_events (map_id, actor, kind, at, recorded_at) "
                     "VALUES ('old', 'user', 'update', 'x', 'x')")
    monkeypatch.setenv("MINDMAP_DB_PATH", str(legacy))

    with repository.connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(edit_events)")}
        kept = conn.execute("SELECT COUNT(*) FROM edit_events").fetchone()[0]

    assert {"subtree_size", "change_count"} <= columns
    # 不做迁移：老行还是 NULL，而 NULL 在这里有确切含义（"当时没记"）。
    assert kept == 1
    with repository.connect() as conn:
        row = conn.execute("SELECT subtree_size, change_count FROM edit_events").fetchone()
    assert row[0] is None and row[1] is None


@pytest.mark.parametrize(
    "kind", ["add", "remove", "update", "move", "edge", "style", "accept", "repair"])
def test_all_kinds_round_trip(kind: str):
    map_id = _create([_event(kind=kind)])
    summary = client.get(f"/api/mindmaps/{map_id}/edits/summary").json()
    assert summary["by_kind"] == {kind: 1}
