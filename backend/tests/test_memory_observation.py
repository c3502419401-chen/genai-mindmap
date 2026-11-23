"""观测：什么算"一次动作"，以及它凭什么能回指到原始事件。

这一层的错误代价是**命题建立在假的支撑上**：如果一次批量保存被切成 550 条观测，
一条命题的支撑数就会是 550（置信度的错觉）；如果两次不同的保存被合并成一条，
"他改完又改回去"这个最有信息量的动作就消失了。所以这里的用例几乎都在钉边界：
**同一批 = 一条，不同批 = 两条**。

不在这里验的：归纳（`test_memory_proposition.py`）、双路合并
（`test_memory_hybrid.py`）。
"""
from __future__ import annotations

from contextlib import closing
from datetime import timedelta

from app.memory import observations, store
from app.mindmaps import repository


# ---------------------------------------------------------------- 夹具

def stamp(minutes_ago: float = 0.0) -> str:
    moment = store.utcnow() - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def put_run(run_id: str, goal: str = "机器学习", *, intent: str = "create",
            status: str = "done", minutes_ago: float = 0.0,
            applied: bool = False, diagram: str | None = None) -> str:
    """直插一行 `runs`。用 SQL 而不是 `runs.start/finish`：那套是给 SSE 生成器用的
    状态机，这里要的只是"表里有一行历史"。
    """
    import json

    request = json.dumps(
        {"goal": goal, "intent": intent, "diagram_type": diagram, "prefs_applied": []},
        ensure_ascii=False,
    )
    moment = stamp(minutes_ago)
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO runs (run_id, status, request, created_at, updated_at, applied_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, status, request, moment, moment, moment if applied else None),
        )
    return run_id


def put_events(map_id: str, at: str, edits: list[dict], *, recorded_at: str | None = None) -> None:
    """直插一批 `edit_events`。`at` 显式给：它是**动作发生的时刻**，也是切分键。"""
    rows = [
        (map_id, None, edit.get("actor", "user"), edit.get("kind", "update"),
         edit.get("node_id", "n-1"), edit.get("field", "label"),
         edit.get("before", ""), edit.get("after", ""), edit.get("depth", 1),
         None, None, None, at, recorded_at or at)
        for edit in edits
    ]
    with closing(repository.connect()) as conn, conn:
        conn.executemany(
            "INSERT INTO edit_events (map_id, run_id, actor, kind, node_id, field, before, "
            "after, depth, subtree_size, change_count, node_count, at, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


# ---------------------------------------------------------------- 表

def test_three_tables_are_created_with_the_others():
    """观测 / 命题 / 边 三张表与 `memory_items` 同一时刻建好。

    与 `test_memory_store.py` 里那条同一个理由：读它们的路径在热路径上（每次 run
    开始抽一次观测），多一个"发现表不存在 → 建表"的分支没有好处。
    """
    with closing(repository.connect()) as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {store.OBSERVATION_TABLE, store.PROPOSITION_TABLE, store.EDGE_TABLE} <= names


def test_schema_is_idempotent():
    repository.connect().close()
    repository.connect().close()
    assert observations.count() == 0


# ---------------------------------------------------------------- run 观测

def test_a_run_is_one_observation():
    """一次 run = 一条观测，摘要只陈述事实（意图、目标、终态、有没有被保存）。"""
    put_run("run-1", "机器学习，用鱼骨图", diagram="fishbone", applied=True)
    got = observations.capture()
    assert got.runs == 1 and got.edits == 0

    item = observations.recent(1)[0]
    assert item.kind == observations.OBSERVATION_RUN
    assert "create" in item.summary
    assert "机器学习，用鱼骨图" in item.summary
    assert "done" in item.summary
    assert "已保存" in item.summary
    assert item.refs == {"run_ids": ["run-1"]}
    assert item.payload["diagram_type"] == "fishbone"


def test_an_unsaved_run_says_so():
    """生成完没保存的那一次与保存了的，是两种事实——观测必须分开说。

    它是"他这次不满意"最便宜的证据：UI 上什么都没发生，而库里这一行说明结果没有被
    留下。
    """
    put_run("run-1", status="done", applied=False)
    observations.capture()
    assert "未保存" in observations.recent(1)[0].summary


def test_a_failed_run_is_still_an_observation():
    """失败也是一次动作。丢掉它等于抹掉"他要过这个、没拿到"。"""
    put_run("run-1", "碳中和", status="failed")
    observations.capture()
    item = observations.recent(1)[0]
    assert "failed" in item.summary
    assert "已保存" not in item.summary


def test_a_broken_request_does_not_break_capture():
    """`request` 读不出来时观测照样建，只是没有 goal。

    这条不是假想：`runs.request` 是 TEXT，历史行里出现过坏 JSON（手工改库、老格式）。
    让一条坏行把整次抽取打断，等于新观测一条都进不来。
    """
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO runs (run_id, status, request, created_at, updated_at) "
            "VALUES ('run-bad', 'done', '{不是 JSON', ?, ?)",
            (stamp(), stamp()),
        )
    assert observations.capture().runs == 1
    item = observations.recent(1)[0]
    assert "无目标文本" in item.summary
    assert item.payload["goal"] == ""


def test_empty_goal_says_no_goal_text():
    put_run("run-1", "")
    observations.capture()
    assert "无目标文本" in observations.recent(1)[0].summary


# ---------------------------------------------------------------- edit 观测（粒度）

def test_one_save_is_one_observation_even_with_many_edits():
    """**同一 `at` 的 550 条改动是一条观测**，不是 550 条。

    这是整层最要紧的一条。按行算的话，一条命题的支撑数会被灌到 550——"支撑数"是
    置信度的来源，被灌水之后它就不再有意义，而库里看起来完全正常。
    """
    at = stamp(5)
    put_events("map-1", at, [
        {"before": f"标题{i}", "after": f"短{i}"} for i in range(50)
    ])
    got = observations.capture()
    assert got.edits == 1
    item = observations.recent(1)[0]
    assert item.kind == observations.OBSERVATION_EDIT
    assert item.stat == 50
    assert len(item.event_ids) == 50
    assert "50 处" in item.summary


def test_two_saves_are_two_observations():
    """不同 `at` 的两次保存是两条观测——"改完又改回来"不能被合并掉。"""
    put_events("map-1", stamp(10), [{"before": "A", "after": "B"}])
    put_events("map-1", stamp(5), [{"before": "B", "after": "A"}])
    assert observations.capture().edits == 2
    items = observations.recent(2)
    assert len(items) == 2
    assert items[0].at > items[1].at  # 新的在前


def test_the_same_moment_on_two_maps_is_two_observations():
    """同一毫秒、不同图 = 两条观测。身份是 `(map_id, at)`，不是 `at`。

    实测库里就有这种行（两张图的日志被同一批改动写过）：它们在这两张图上都发生了，
    所以是两条事实；合并会让人以为"他只改了一张图"。
    """
    at = stamp(3)
    put_events("map-1", at, [{"before": "A", "after": "B"}])
    put_events("map-2", at, [{"before": "A", "after": "B"}])
    assert observations.capture().edits == 2


def test_recorded_at_does_not_split_a_batch():
    """入库时刻不同不拆批。实测：同一批的 `recorded_at` 会差几毫秒（12:23:34.567 /
    .569 / .571），用切分键会把它读成三次动作。"""
    at = stamp(4)
    put_events("map-1", at, [{"before": "A", "after": "B"}], recorded_at=stamp(4))
    put_events("map-1", at, [{"before": "C", "after": "D"}], recorded_at=stamp(3.9))
    assert observations.capture().edits == 1


def test_identical_edits_fold_but_keep_the_count():
    """同一类改动折叠成一种、带次数。折叠是给模型看的（不重复），次数是信息。"""
    at = stamp(2)
    put_events("map-1", at, [{"before": "长标题", "after": "短"}] * 7)
    observations.capture()
    item = observations.recent(1)[0]
    assert item.payload["events"] == 7
    assert len(item.payload["changes"]) == 1
    assert item.payload["changes"][0]["n"] == 7


def test_changes_are_capped_and_the_omission_is_recorded():
    """折叠后仍超过上限时留前 N 种，并记 `omitted`。

    "悄悄少了几种"与"这里本来就只有这么多种"在 payload 里必须分得开——前者会让
    模型低估这次改动的规模。
    """
    at = stamp(1)
    edits = [{"before": f"标题{i}", "after": f"短{i}"}
             for i in range(observations.MAX_CHANGES + 5)]
    put_events("map-1", at, edits)
    observations.capture()
    item = observations.recent(1)[0]
    assert len(item.payload["changes"]) == observations.MAX_CHANGES
    assert item.payload["omitted"] == 5


def test_agent_edits_are_marked_as_model_not_user():
    """`actor` 要传进摘要：模型改的和用户改的，是两种完全不同的事实。"""
    put_events("map-1", stamp(1), [{"actor": "agent", "before": "A", "after": "B"}])
    observations.capture()
    assert "模型" in observations.recent(1)[0].summary


# ---------------------------------------------------------------- 幂等与增量

def test_capture_is_idempotent():
    """抽两次只增一次。`source_key` 的 UNIQUE 索引就是这件事的护栏。

    没有它，"每次 run 开始都抽一次"会让整张表随运行次数翻倍，而每条观测看起来都
    合法——那是这类设计最难发现的一种坏。
    """
    put_run("run-1")
    put_events("map-1", stamp(1), [{"before": "A", "after": "B"}])
    assert observations.capture() == observations.Captured(runs=1, edits=1)
    assert observations.capture() == observations.Captured(runs=0, edits=0)
    assert observations.count() == 2


def test_new_events_are_picked_up_incrementally():
    put_run("run-1")
    observations.capture()
    put_run("run-2")
    assert observations.capture().runs == 1
    assert observations.count() == 2


# ---------------------------------------------------------------- 回指

def test_refs_point_at_real_rows():
    """**回指链的实证**：命题 → 观测 → 原始行，每一段都由主键接上。

    这条用例是"命题不是自由摘要"的机器可查版本：给出观测 id，拿到的
    `event_ids` 必须能在 `edit_events` 里查到行，`run_ids` 必须能在 `runs` 里查到行。
    """
    at = stamp(1)
    put_events("map-1", at, [{"before": "A", "after": "B"}, {"before": "C", "after": "D"}])
    put_run("run-1")
    observations.capture()

    refs = observations.resolve_refs([item.id for item in observations.recent(2)])
    assert refs["missing"] == []
    assert refs["run_ids"] == ["run-1"]
    assert len(refs["event_ids"]) == 2

    with closing(repository.connect()) as conn:
        found = conn.execute(
            f"SELECT COUNT(*) FROM edit_events WHERE id IN "
            f"({','.join('?' * len(refs['event_ids']))})", refs["event_ids"],
        ).fetchone()[0]
    assert found == 2


def test_resolve_refs_reports_missing_ids():
    """回指不到的 id 单独列出来。静默丢弃会让"回指断了"看起来像"没引用"。"""
    refs = observations.resolve_refs(["不存在"])
    assert refs["missing"] == ["不存在"]
    assert refs["event_ids"] == []


def test_event_ids_are_capped_and_flagged():
    """一次超过 `MAX_REFS` 的批量操作会被截断，并且**标出来**。

    不标的后果是"这条观测只改了 2000 处"变成一个假事实，而它是永久的（观测不再重抽）。
    """
    at = stamp(1)
    put_events("map-1", at, [
        {"before": f"L{i}", "after": f"S{i}"} for i in range(observations.MAX_REFS + 3)
    ])
    observations.capture()
    item = observations.recent(1)[0]
    assert len(item.event_ids) == observations.MAX_REFS
    assert item.refs["truncated"] is True
    assert item.stat == observations.MAX_REFS + 3


# ---------------------------------------------------------------- 消费与读取

def test_unconsumed_is_oldest_first():
    """待归纳的观测按时间**正序**：先要骨架再补细节，顺序是判断的一部分。"""
    put_run("run-2", minutes_ago=1)
    put_run("run-1", minutes_ago=10)
    observations.capture()
    assert [item.run_id for item in observations.unconsumed(10)] == ["run-1", "run-2"]


def test_mark_consumed_is_one_way_and_idempotent():
    put_run("run-1")
    observations.capture()
    item = observations.recent(1)[0]
    assert observations.mark_consumed([item.id]) == 1
    assert observations.mark_consumed([item.id]) == 0
    assert observations.unconsumed(10) == []
    assert observations.count() == 1


def test_recent_is_newest_first():
    put_run("run-1", minutes_ago=5)
    put_run("run-2", minutes_ago=1)
    observations.capture()
    assert [item.run_id for item in observations.recent(2)] == ["run-2", "run-1"]


def test_capture_on_an_empty_database_is_free():
    assert observations.capture() == observations.Captured()
    assert observations.count() == 0


def test_nothing_is_written_to_the_real_database():
    """隔离自检：`conftest` 把库放系统临时目录，这里顺手把它钉住。

    这条不是重复保险——它是"这套东西不会在跑测试时污染开发库"的唯一证据，而那条库
    里躺着 93 次真实运行。不写 `"tmp" in path` 是因为 Windows 的临时目录叫
    `AppData/Local/Temp`，那个断言会以"看起来有道理"的方式失败。
    """
    import pathlib
    import tempfile

    put_run("run-1")
    observations.capture()
    assert observations.count() == 1
    root = pathlib.Path(tempfile.gettempdir()).resolve()
    assert root in repository.db_path().resolve().parents
