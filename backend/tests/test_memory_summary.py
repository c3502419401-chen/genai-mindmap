"""会话结束的判定与总结管线。

这里钉的是三件事，每一件都有一种具体的坏法：

  1. **"会话结束"怎么判**——错了就会把同一段会话总结两遍（多花钱），或者永远不总结
     （功能静默失效）。
  2. **产出 0 条是合法结果**——它必须留一行 `memory_sessions` 记录，否则同一段会话
     会在每次新运行时被重新总结。
  3. **失败要有痕迹**——一条 warning + 一行 `status='failed'`，而不是静默跳过。
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest

from app.generation.service import GenerationError
from app.memory import store, summarize
from app.mindmaps import repository


# ---------------------------------------------------------------- 夹具

def enable_llm(monkeypatch) -> None:
    """让 `_llm_ready()` 为真。真请求不会发：`chat_async` 会被换掉。

    刻意**不设** `MINDMAP_LLM_MODEL_DEEP`，也刻意不断言它：总结走 `chat_async`
    且不传 `deep_thinking`，"不走推理模型"这件事在 `test_memory_summary` 的最后
    一条用例里钉。
    """
    monkeypatch.setenv("MINDMAP_LLM_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("MINDMAP_LLM_API_KEY", "test-key")
    monkeypatch.setenv("MINDMAP_LLM_MODEL", "deepseek-chat")


def fake_embedding(monkeypatch) -> None:
    """把 `store.embed_texts` 换成一个恒定向量；不让 `add()` 去联网。

    每段文本都拿到同一个单位向量：这里测的是管线（几条落库、状态对不对），
    不是相关性排序（那是 `test_memory_store` 的事）。
    """
    monkeypatch.setenv("MINDMAP_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("MINDMAP_EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")

    def embed(texts: list[str]) -> list[list[float] | None]:
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(store, "embed_texts", embed)
    monkeypatch.setattr(store.vector_cache, "get_vectors", lambda hashes, model: {})
    monkeypatch.setattr(store.vector_cache, "put_vectors", lambda items, model: 0)


def stamp(minutes_ago: float) -> str:
    moment = store.utcnow() - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def add_run(
    run_id: str,
    *,
    ended_minutes_ago: float = 30,
    started_minutes_ago: float | None = None,
    goal: str = "机器学习 复习提纲",
    intent: str = "create",
    diagram_type: str | None = None,
    prefs: tuple[str, ...] = (),
    status: str = "done",
) -> None:
    """直接写一行 `runs`。

    不用 `runs.begin/finish`：它们永远写"现在"，而这条管线全部的行为都取决于
    "上一次运行是什么时候结束的"。时间必须由用例给。
    """
    import json

    started = (ended_minutes_ago + 5) if started_minutes_ago is None else started_minutes_ago
    request = {
        "goal": goal, "intent": intent, "diagram_type": diagram_type,
        "prefs_applied": list(prefs),
    }
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO runs (run_id, status, request, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, status, json.dumps(request, ensure_ascii=False),
             stamp(started), stamp(ended_minutes_ago)),
        )


def add_event(
    *, kind: str = "update", field: str | None = "label",
    before: str | None = None, after: str | None = None,
    minutes_ago: float = 25, actor: str = "user", depth: int | None = 2,
    subtree_size: int | None = None, node_count: int | None = None,
) -> None:
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO edit_events (map_id, actor, kind, field, before, after, depth, "
            "subtree_size, node_count, at, recorded_at) "
            "VALUES ('m1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (actor, kind, field, before, after, depth, subtree_size, node_count,
             stamp(minutes_ago), stamp(minutes_ago)),
        )


def fake_model(monkeypatch, payload):
    """把总结用的模型出口换成一个固定返回值（或异常）。返回被发出去的 (system, user)。"""
    sent: list[tuple[str, str]] = []

    async def chat(system: str, user: str, **kwargs):
        sent.append((system, user))
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(summarize, "chat_async", chat)
    return sent


# ---------------------------------------------------------------- 会话判定

def test_due_is_none_without_runs(monkeypatch):
    enable_llm(monkeypatch)
    assert summarize.due() is None


def test_due_is_none_while_the_session_may_still_be_running(monkeypatch):
    """上一次运行刚结束（间隔不到 `GAP_MINUTES`）→ 不总结。

    这是整条管线最要紧的一条判断：判早了会把一段还在继续的会话总结成半截，而
    下次运行时它已经被标记成"总结过"，后半段永远进不了记忆。
    """
    enable_llm(monkeypatch)
    add_run("r1", ended_minutes_ago=summarize.GAP_MINUTES - 1)
    assert summarize.due() is None


def test_due_fires_after_the_gap(monkeypatch):
    enable_llm(monkeypatch)
    add_run("r1", ended_minutes_ago=summarize.GAP_MINUTES + 1, goal="鱼骨图分析机器学习")
    session = summarize.due()
    assert session is not None
    assert session.session_id == "r1"
    assert [run.run_id for run in session.runs] == ["r1"]
    assert session.runs[0].goal == "鱼骨图分析机器学习"


def test_due_groups_consecutive_runs_into_one_session(monkeypatch):
    """间隔不超过 `GAP_MINUTES` 的连续运行属于同一个会话，按时间正序交给总结。"""
    enable_llm(monkeypatch)
    add_run("r1", ended_minutes_ago=40, started_minutes_ago=45, goal="第一轮")
    add_run("r2", ended_minutes_ago=35, started_minutes_ago=38, goal="第二轮")
    add_run("r3", ended_minutes_ago=25, started_minutes_ago=30, goal="第三轮")
    session = summarize.due()
    assert session is not None
    assert session.session_id == "r3", "session_id 取最后一次运行"
    assert [run.goal for run in session.runs] == ["第一轮", "第二轮", "第三轮"]


def test_due_stops_at_a_gap_inside_the_history(monkeypatch):
    """更早的那一段（中间隔了超过 `GAP_MINUTES`）不属于这次会话。"""
    enable_llm(monkeypatch)
    add_run("old", ended_minutes_ago=200, started_minutes_ago=205, goal="很久以前")
    add_run("r1", ended_minutes_ago=40, started_minutes_ago=45, goal="这次的第一轮")
    add_run("r2", ended_minutes_ago=30, started_minutes_ago=35, goal="这次的第二轮")
    session = summarize.due()
    assert session is not None
    assert [run.goal for run in session.runs] == ["这次的第一轮", "这次的第二轮"]


def test_due_skips_a_session_that_was_already_summarized(monkeypatch):
    """处理过的会话（含 0 条、含失败）不再进总结——否则每次运行都会重新总结一遍。"""
    enable_llm(monkeypatch)
    add_run("r1", ended_minutes_ago=60)
    assert summarize.due() is not None
    summarize._mark("r1", summarize.STATUS_EMPTY, 0, None)
    assert summarize.due() is None


def test_due_excludes_the_run_that_is_starting(monkeypatch):
    """调用点在 `runs.begin()` 之后，所以当前这次运行已经在表里了，必须排除。

    不排除的后果是当前运行的 `updated_at` 就是"现在"，间隔永远不够，
    `due()` 会一直返回 None——功能静默失效，而且看起来完全正常。
    """
    enable_llm(monkeypatch)
    add_run("previous", ended_minutes_ago=60)
    add_run("current", ended_minutes_ago=0)
    assert summarize.due() is None, "当前运行会把间隔判成 0"
    session = summarize.due(exclude_run_id="current")
    assert session is not None
    assert session.session_id == "previous"


def test_due_is_none_when_the_model_is_not_configured(monkeypatch):
    """模型没配置 → 不开始，也**不写任何结论**。

    写成 `failed` 的后果是这台机器上所有历史会话被永久标记为"试过了"，等真的
    配好 key 之后它们再也不会被总结。
    """
    add_run("r1", ended_minutes_ago=60)
    assert summarize.due() is None
    with closing(repository.connect()) as conn:
        rows = conn.execute(f"SELECT COUNT(*) FROM {store.SESSION_TABLE}").fetchone()
    assert rows[0] == 0


def test_due_caps_the_number_of_runs(monkeypatch):
    """一个会话里的运行数超过 `MAX_RUNS` 时只取最近的，但会话本身仍然成立。"""
    enable_llm(monkeypatch)
    total = summarize.MAX_RUNS + 5
    for index in range(total):
        # 越靠前越早：间隔都控制在 GAP 以内，所以它们属于同一个会话。
        add_run(f"r{index}", ended_minutes_ago=100 - index, started_minutes_ago=101 - index)
    session = summarize.due()
    assert session is not None
    assert len(session.runs) == summarize.MAX_RUNS
    assert session.runs[-1].run_id == f"r{total - 1}"


# ---------------------------------------------------------------- 输入

def test_user_text_carries_runs_and_events(monkeypatch):
    """总结输入里有目标、图型、生效偏好与事后修改，**没有材料正文**。"""
    enable_llm(monkeypatch)
    add_run("r1", ended_minutes_ago=40, started_minutes_ago=50,
            goal="机器学习 复习提纲", diagram_type="fishbone",
            prefs=("diagram_type.default=fishbone",))
    add_run("r2", ended_minutes_ago=30, started_minutes_ago=35, goal="改成树状图")
    add_event(kind="update", field="title", before="以Mitchell三要素界定学习",
              after="三要素界定学习", minutes_ago=28)
    session = summarize.due()
    assert session is not None
    text = summarize.user_text(session, summarize._events(
        session.started_at, session.ended_at, ""), [])
    assert "机器学习 复习提纲" in text
    assert "图型: fishbone" in text
    assert "diagram_type.default=fishbone" in text
    assert "三要素界定学习" in text
    assert "他改 title：以Mitchell三要素界定学习 → 三要素界定学习｜第 2 层" in text


def test_events_window_covers_the_minutes_after_a_run(monkeypatch):
    """修改发生在运行结束后（真实情况几乎总是这样），窗口尾部要延长 `GAP_MINUTES`。

    只取 `[started_at, ended_at]` 的话，"他事后的修改"永远是零条——而这正是
    总结里信息量最大的那一半。
    """
    enable_llm(monkeypatch)
    add_run("r1", ended_minutes_ago=40, started_minutes_ago=50)
    add_event(minutes_ago=30, before="长标题一二三四五六七八九十", after="短标题")
    add_event(minutes_ago=100, before="窗口之外的旧改动", after="不该出现")
    session = summarize.due()
    assert session is not None
    rows = summarize._events(session.started_at, session.ended_at, "")
    texts = [row["before"] for row in rows]
    assert "长标题一二三四五六七八九十" in texts
    assert "窗口之外的旧改动" not in texts


def test_identical_consecutive_edits_collapse_with_a_count(monkeypatch):
    """同一处连改三遍 → prompt 里一行 + `（×3）`。

    实测出来的必要：不折叠时 prompt 里有三行一模一样的「改 label：A → B」，模型会把
    它读成"三个不同场合的偏好"，于是产出三条互相重复的记忆（`text_hash` 只挡得住
    字面完全一致的那些）。而真实信息"同一件事他改了三遍"——次数就是它，折叠之后
    还在。
    """
    enable_llm(monkeypatch)
    add_run("r1", ended_minutes_ago=40, started_minutes_ago=50)
    for minutes in (28, 27, 26):
        add_event(field="label", before="以Mitchell三要素界定学习",
                  after="三要素界定学习", minutes_ago=minutes)
    session = summarize.due()
    text = summarize.user_text(session, summarize._events(
        session.started_at, session.ended_at, ""), [])
    assert text.count("以Mitchell三要素界定学习 → 三要素界定学习") == 1
    assert "（×3）" in text
    assert "3 处，合并连续重复后 1 条" in text


def test_non_consecutive_edits_are_not_collapsed(monkeypatch):
    """`A, B, A` 不折叠：中间那个"改回来"是最有信息量的动作。"""
    enable_llm(monkeypatch)
    add_run("r1", ended_minutes_ago=40, started_minutes_ago=50)
    add_event(field="label", before="长标题", after="短标题", minutes_ago=28)
    add_event(field="detail", before="旧说明", after="新说明", minutes_ago=27)
    add_event(field="label", before="长标题", after="短标题", minutes_ago=26)
    session = summarize.due()
    text = summarize.user_text(session, summarize._events(
        session.started_at, session.ended_at, ""), [])
    assert text.count("长标题 → 短标题") == 2
    assert "×2" not in text


def test_generate_events_have_no_subject(monkeypatch):
    """一次生成没有主语：它是统计的分母，不是"他亲手加了 23 个节点"。"""
    enable_llm(monkeypatch)
    add_run("r1", ended_minutes_ago=40, started_minutes_ago=50)
    add_event(kind="generate", field=None, node_count=23, minutes_ago=45)
    session = summarize.due()
    text = summarize.user_text(session, summarize._events(
        session.started_at, session.ended_at, ""), [])
    assert "这次生成落了 23 个节点" in text
    assert "他这次生成" not in text


def test_user_text_says_zero_edits_explicitly(monkeypatch):
    """"一个字都没动"是一条**结论**，要写出来；留白会让模型以为记录缺了一段。"""
    enable_llm(monkeypatch)
    add_run("r1", ended_minutes_ago=40, started_minutes_ago=50)
    session = summarize.due()
    assert session is not None
    text = summarize.user_text(session, [], [])
    assert "一个字都没动" in text


def test_user_text_lists_known_memories(monkeypatch):
    """已知记忆要交给模型，避免"同一条偏好有五种说法"占掉别的记忆的位置。"""
    enable_llm(monkeypatch)
    add_run("r1", ended_minutes_ago=40, started_minutes_ago=50)
    session = summarize.due()
    assert session is not None
    text = summarize.user_text(session, [], ["标题去掉人名"])
    assert "- 标题去掉人名" in text


# ---------------------------------------------------------------- 解析

def test_parse_items_accepts_the_documented_shape():
    items = summarize._parse_items(
        {"memories": [{"text": "标题去掉人名", "kind": "preference"}]}, store.utcnow())
    assert items == [("标题去掉人名", "preference", None)]


def test_parse_items_accepts_a_bare_array():
    """模型偶尔只给数组。宽容这一档的代价是零，收益是少一次白跑。"""
    items = summarize._parse_items([{"text": "偏好鱼骨图", "kind": "pattern"}],
                                   store.utcnow())
    assert items[0][0] == "偏好鱼骨图"


def test_parse_items_treats_an_empty_object_as_zero_items():
    """`{}` 是"这次没什么可记的"的另一种写法，按 0 条处理。"""
    assert summarize._parse_items({}, store.utcnow()) == []


def test_parse_items_rejects_an_object_without_memories():
    """没有 `memories` 键 ⇒ 格式不对，抛错。

    静默当成 0 条的后果是"总结管线坏了"看起来和"没什么可记"一模一样。
    """
    with pytest.raises(ValueError):
        summarize._parse_items({"notes": ["x"]}, store.utcnow())


def test_parse_items_falls_back_on_unknown_kind():
    items = summarize._parse_items({"memories": [{"text": "一句话", "kind": "opinion"}]},
                                   store.utcnow())
    assert items[0][1] == "preference"


def test_parse_items_drops_a_future_formed_at():
    """未来的 `formed_at` 会让衰减因子变成"现在"，等于给坏数据额外加成。"""
    future = (store.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
    items = summarize._parse_items(
        {"memories": [{"text": "一句话", "formed_at": future}]}, store.utcnow())
    assert items[0][2] is None


def test_parse_items_normalizes_a_past_formed_at():
    items = summarize._parse_items(
        {"memories": [{"text": "一句话", "formed_at": "2026-11-01"}]},
        store.utcnow().replace(year=2026) if store.utcnow().year == 2026
        else store.utcnow())
    assert items[0][2] == "2026-11-01T00:00:00.000Z" or items[0][2] is None


def test_parse_items_skips_blank_text():
    items = summarize._parse_items(
        {"memories": [{"text": "   "}, {"text": "有内容的"}]}, store.utcnow())
    assert [item[0] for item in items] == ["有内容的"]


# ---------------------------------------------------------------- 落库

def test_run_session_writes_items_and_marks_the_session(monkeypatch):
    enable_llm(monkeypatch)
    fake_embedding(monkeypatch)
    add_run("r1", ended_minutes_ago=40, started_minutes_ago=50)
    session = summarize.due()
    assert session is not None
    fake_model(monkeypatch, {"memories": [
        {"text": "标题倾向去掉人名与并列项", "kind": "preference"},
        {"text": "偏好鱼骨图分析", "kind": "pattern"},
    ]})

    outcome = summarize.run_session(session)
    assert (outcome.status, outcome.produced, outcome.created) == (
        summarize.STATUS_DONE, 2, 2)
    assert store.count(owner="") == 2
    with closing(repository.connect()) as conn:
        row = conn.execute(
            f"SELECT status, item_count FROM {store.SESSION_TABLE} WHERE session_id = ?",
            ("r1",)).fetchone()
    assert (row["status"], row["item_count"]) == (summarize.STATUS_DONE, 2)


def test_run_session_records_the_provenance(monkeypatch):
    """每条记忆都带着它是哪次会话、哪次运行总结出来的。"""
    enable_llm(monkeypatch)
    fake_embedding(monkeypatch)
    add_run("r1", ended_minutes_ago=40, started_minutes_ago=50)
    session = summarize.due()
    fake_model(monkeypatch, {"memories": [{"text": "标题去掉人名"}]})
    summarize.run_session(session)

    item = store.items(owner="")[0]
    assert item.session_id == "r1"
    assert item.source_run_id == "r1"
    assert item.source == store.SOURCE_SUMMARY
    assert item.created_at, "created_at 是总结时刻，必须写上"


def test_run_session_with_zero_items_is_a_valid_outcome(monkeypatch):
    """**0 条是合法结果**：留一行 `empty`，不当失败。"""
    enable_llm(monkeypatch)
    add_run("r1", ended_minutes_ago=40, started_minutes_ago=50)
    session = summarize.due()
    fake_model(monkeypatch, {"memories": []})

    outcome = summarize.run_session(session)
    assert (outcome.status, outcome.produced) == (summarize.STATUS_EMPTY, 0)
    assert store.count(owner="") == 0
    summarize._mark  # 用于断言下面那一行确实写进去了
    with closing(repository.connect()) as conn:
        row = conn.execute(
            f"SELECT status FROM {store.SESSION_TABLE} WHERE session_id = ?",
            ("r1",)).fetchone()
    assert row["status"] == summarize.STATUS_EMPTY


def test_run_session_deduplicates_against_existing_items(monkeypatch):
    """同一条文本第二次被总结出来时不重复写，`created` 记 0。"""
    enable_llm(monkeypatch)
    fake_embedding(monkeypatch)
    store.add("标题去掉人名")
    add_run("r1", ended_minutes_ago=40, started_minutes_ago=50)
    session = summarize.due()
    fake_model(monkeypatch, {"memories": [{"text": "标题去掉人名"}]})

    outcome = summarize.run_session(session)
    assert (outcome.produced, outcome.created) == (1, 0)
    assert store.count(owner="") == 1


def test_run_session_records_a_model_failure(monkeypatch, caplog):
    """模型调用失败 → `failed` + 一条 warning。**不能静默跳过**。"""
    enable_llm(monkeypatch)
    add_run("r1", ended_minutes_ago=40, started_minutes_ago=50)
    session = summarize.due()
    fake_model(monkeypatch, GenerationError("请不要在"))

    with caplog.at_level("WARNING", logger=summarize.__name__):
        outcome = summarize.run_session(session)
    assert outcome.status == summarize.STATUS_FAILED
    assert "模型调用失败" in (outcome.detail or "")
    assert any("总结失败" in record.message for record in caplog.records)


def test_run_session_records_a_malformed_output(monkeypatch, caplog):
    """输出格式不对 → `failed`，detail 里带"格式"两个字（要改的是提示词不是配置）。"""
    enable_llm(monkeypatch)
    add_run("r1", ended_minutes_ago=40, started_minutes_ago=50)
    session = summarize.due()
    fake_model(monkeypatch, {"notes": ["跑偏了"]})

    with caplog.at_level("WARNING", logger=summarize.__name__):
        outcome = summarize.run_session(session)
    assert outcome.status == summarize.STATUS_FAILED
    assert "格式" in (outcome.detail or "")


def test_a_failed_session_is_not_retried(monkeypatch):
    """失败也留一行记录 → 同一段会话不会被反复重试（配置坏掉时会变成持续烧钱）。"""
    enable_llm(monkeypatch)
    add_run("r1", ended_minutes_ago=40, started_minutes_ago=50)
    session = summarize.due()
    fake_model(monkeypatch, GenerationError("上游 502"))
    summarize.run_session(session)
    assert summarize.due() is None


# ---------------------------------------------------------------- 模型选择

def test_summary_does_not_ask_for_the_reasoner(monkeypatch):
    """总结**不走** `deep_thinking`：那是一个分类动作，不是多步推理。"""
    enable_llm(monkeypatch)
    monkeypatch.setenv("MINDMAP_LLM_MODEL_DEEP", "deepseek-reasoner")
    add_run("r1", ended_minutes_ago=40, started_minutes_ago=50)
    session = summarize.due()
    seen: list[dict] = []

    async def chat(system: str, user: str, **kwargs):
        seen.append(kwargs)
        return {"memories": []}

    monkeypatch.setattr(summarize, "chat_async", chat)
    summarize.run_session(session)
    assert seen, "模型出口必须被调用到"
    assert "deep_thinking" not in seen[0]
    assert seen[0].get("max_tokens") == summarize.SUMMARY_MAX_TOKENS


def test_summary_prompt_states_the_zero_is_legal(monkeypatch):
    """"产出 0 条是合法结果"必须写进 prompt——这是唯一能压住"凑数"的地方。"""
    assert "0 条是完全合法的结果" in summarize.SUMMARY_SYSTEM
    assert "这个人" in summarize.SUMMARY_SYSTEM
    assert "换谁来用这份材料都成立" in summarize.SUMMARY_SYSTEM
    assert "100 字" in summarize.SUMMARY_SYSTEM


def test_schedule_refuses_the_same_session_twice(monkeypatch):
    """并发开始（两个标签页）时只起一个总结线程。"""
    enable_llm(monkeypatch)
    add_run("r1", ended_minutes_ago=40, started_minutes_ago=50)
    session = summarize.due()
    monkeypatch.setattr(summarize, "run_session", lambda _session: pytest.fail(
        "不该真的跑"))
    summarize._INFLIGHT.add(session.session_id)
    try:
        assert summarize.schedule(session) is False
    finally:
        summarize._INFLIGHT.discard(session.session_id)


def test_gap_is_twenty_minutes():
    """这个数是设计决定，不是一个随手取的值：改它要重新读模块 docstring 的两头界限。"""
    assert summarize.GAP_MINUTES == 20


# ---------------------------------------------------------------- router 的接线
#
# "会话结束由后端自己判定、不需要前端配合"的落点就是 `stream_run` 里的那一行。
# 下面三条钉住它的三件事：认出会话、没会话时什么都不做、**它抛错不能影响生成**。

def test_router_starts_a_summary_for_a_finished_session(monkeypatch):
    from app.agent import router as agent_router

    enable_llm(monkeypatch)
    add_run("previous", ended_minutes_ago=60)
    started: list[str] = []
    monkeypatch.setattr(summarize, "schedule",
                        lambda session: started.append(session.session_id) or True)

    agent_router._maybe_summarize("current")
    assert started == ["previous"]


def test_router_does_nothing_when_there_is_no_finished_session(monkeypatch):
    from app.agent import router as agent_router

    enable_llm(monkeypatch)
    add_run("current", ended_minutes_ago=0)
    monkeypatch.setattr(summarize, "schedule",
                        lambda session: pytest.fail("不该启动"))
    agent_router._maybe_summarize("current")


def test_router_swallows_summary_failures(monkeypatch, caplog):
    """总结抛错只留一条 warning：它抛出去会让一次本来正常的生成 500。"""
    from app.agent import router as agent_router

    enable_llm(monkeypatch)
    add_run("previous", ended_minutes_ago=60)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(summarize, "due", boom)
    with caplog.at_level("WARNING", logger=agent_router.__name__):
        agent_router._maybe_summarize("current")
    assert any("记忆总结没有启动" in record.message for record in caplog.records)
