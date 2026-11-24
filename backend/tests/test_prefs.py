"""用户偏好：一张 keyed 表、三类值域、两条读路径（预填 / 注入）。

这一层验的是**这张表是不是活的**。三件事分开：

  1. 写：同一件事只能有一条（upsert），同值不刷时间戳，删了真的没了；
  2. 读：A/B 类只走请求层与生成侧（不进 prompt），C 类是唯一进 `_system_text`
     的东西，且排在意图规则**之前**；
  3. 证据：`runs.request.prefs_applied` 是"这条偏好到底生效过没有"的唯一来源，
     面板上的"上次生效 / 尚未生效"直接读它。少了这一环，表就是死的。

建议那几条刻意覆盖了三个"不提"的边界：混合值不提、全空不提、等于内建默认值也
不提（`thinking_model` 每次请求都显式带 `default`，把它当偏好表达会变成一句
"要不要把默认值设成默认值"）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import closing
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent import runs
from app.agent.contracts import AgentRunRequest, Completion, RunBudget, RunContext, ToolCall
from app.agent.router import _request_summary
from app.agent.router import router as agent_router
from app.agent.service import _INTENT_RULES, _SYSTEM_BASE, _system_text
from app.agent.tools import FISHBONE_PLAN_PROMPT, PLAN_PROMPT, build_registry
from app.contracts.mindmap import DIAGRAM_TYPES, MindMapContent
from app.core.errors import register_error_handlers
from app.mindmaps import repository
from app.prefs import service
from app.prefs.router import router as prefs_router

api = FastAPI()
register_error_handlers(api)
api.include_router(prefs_router, prefix="/api")
# agent 路由也挂上：验收那一条要跑**完整**链路（HTTP 设偏好 → 请求体 → SSE → 落库），
# 分段验的时候每一段都能过，接起来仍然可能什么都不生效。
api.include_router(agent_router, prefix="/api")
client = TestClient(api)

EMPTY_TREE = MindMapContent.model_validate({
    "title": "示例",
    "root": {"id": "root", "label": "主题", "children": []},
})


def _raw_insert(key: str, value: str, updated_at: str) -> None:
    """绕过 `service.set` 直写一行。

    只用于造"写入侧的约束管不到"的数据：读取侧的截断防御防的正是这种行（库被手改、
    旧备份被导回来、将来放宽了写入上限）。用 `service.set` 造不出超限的库。
    """
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_prefs "
            "(pref_key, pref_value, scope, source, source_run_id, created_at, updated_at) "
            "VALUES (?, ?, 'global', 'user_explicit', NULL, ?, ?)",
            (key, value, updated_at, updated_at),
        )


def _run(diagram_type: str | None, *, thinking: str = "default",
         prefs_applied: list[str] | None = None, status: str = runs.STATUS_DONE) -> str:
    """造一条有结论的运行记录，供"上次生效"与建议的统计读。"""
    body = AgentRunRequest(
        goal="做一张图", diagram_type=diagram_type, thinking_model=thinking,
        prefs_applied=prefs_applied or [])
    summary = _request_summary(body)
    run_id = runs.begin("run-" + str(len(runs.pending(statuses=(runs.STATUS_DONE,),
                                                       unresolved_only=False))), summary)
    runs.finish(run_id.record.run_id, status, candidate=EMPTY_TREE)
    return run_id.record.run_id


# ---------------------------------------------------------------- 表与 upsert


def test_upsert_keeps_one_row_per_key():
    """同一件事只能有一条：第二次写入是**覆盖**，不是新增。"""
    service.set(service.DIAGRAM_KEY, "fishbone")
    created = service.get(service.DIAGRAM_KEY)

    service.set(service.DIAGRAM_KEY, "flowchart")
    updated = service.get(service.DIAGRAM_KEY)

    assert len([row for row in service.list_all()
                if row.key == service.DIAGRAM_KEY]) == 1
    assert updated is not None and updated.value == "flowchart"
    # 改的是值，不是"又写了一条"：创建时间不动
    assert updated.created_at == created.created_at


def test_same_value_does_not_refresh_updated_at():
    """同值重复提交：`changed=False`，时间戳不动。

    这一条挡的是"我刚设过"的假象——用户连点两次勾选，界面不该显示一条看起来
    刚刚才设过、其实一直没变的偏好。
    """
    first, changed = service.set(service.THINKING_KEY, "feynman")
    assert changed is True

    again, changed_again = service.set(service.THINKING_KEY, "feynman")
    assert changed_again is False
    assert again.updated_at == first.updated_at


def test_delete_is_real():
    """硬删：删完 `get` 是 None，列表里也没有，再删一次返回 False。"""
    service.set(service.ICON_KEY, "false")
    assert service.delete(service.ICON_KEY) is True
    assert service.get(service.ICON_KEY) is None
    assert service.delete(service.ICON_KEY) is False
    assert all(row.key != service.ICON_KEY for row in service.list_all())


def test_one_pref_can_be_read_by_key():
    """`GET /api/prefs/{key}` 读一条；不存在的键是 404，不是 405。

    这条端点原本没挂 `GET`，于是 `/api/prefs/anything` 会被判成 405「方法不允许」。
    405 的字面意思与实际情况正好相反（资源存在但不能用这个方法 vs. 根本没这条偏好），
    排查时会往路由注册的方向翻，而不是往"这个键不存在"的方向。
    """
    service.set(service.ICON_KEY, "false")
    row = client.get(f"/api/prefs/{service.ICON_KEY}")
    assert row.status_code == 200
    assert row.json()["key"] == service.ICON_KEY
    assert row.json()["value"] == "false"

    missing = client.get("/api/prefs/style")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "pref_not_found"


# ---------------------------------------------------------------- 值域


def test_diagram_domain_comes_from_the_contract():
    """值域**读的是 `AgentRunRequest` 的 Literal**，不是另抄一份枚举。

    抄一份的必然结局是某天两边分叉，而分叉的表现是"记忆里存了一个后端不认的值"：
    写的时候通过，下次预填进请求时 422，用户看到的是一次莫名其妙的失败。
    """
    assert service._ALLOWED[service.DIAGRAM_KEY] == DIAGRAM_TYPES


def test_valid_diagram_values_roundtrip():
    for value in DIAGRAM_TYPES:
        pref, changed = service.set(service.DIAGRAM_KEY, value)
        assert changed is True
        assert pref.value == value
        assert service.get(service.DIAGRAM_KEY).value == value


def test_invalid_diagram_value_is_rejected():
    with pytest.raises(service.PrefError) as caught:
        service.set(service.DIAGRAM_KEY, "orgchart")
    assert caught.value.code == "pref_value_invalid"
    # 消息里要有可用的值：只说"不对"等于让用户去翻文档
    assert "fishbone" in caught.value.message
    assert service.get(service.DIAGRAM_KEY) is None


def test_thinking_model_domain_matches_the_request_literal():
    assert service._ALLOWED[service.THINKING_KEY] == (
        "default", "first-principles", "feynman", "systematic", "critical")
    service.set(service.THINKING_KEY, "critical")
    with pytest.raises(service.PrefError):
        service.set(service.THINKING_KEY, "chain-of-thought")


def test_icon_is_a_two_valued_switch():
    service.set(service.ICON_KEY, "false")
    assert service.icons_enabled() is False
    service.set(service.ICON_KEY, "true")
    assert service.icons_enabled() is True
    with pytest.raises(service.PrefError):
        service.set(service.ICON_KEY, "maybe")


def test_unknown_key_is_rejected():
    """A/B/C 之外的键一律拒：放进去没人读，就是一条"心里记着"。"""
    with pytest.raises(service.PrefError) as caught:
        service.set("colour.default", "blue")
    assert caught.value.code == "pref_key_unknown"


def test_external_search_is_a_two_valued_switch_that_defaults_on():
    """"没材料时要不要联网搜"是一条 B 类偏好：两个值、默认开、能存能删。

    默认开必须钉住，而且理由不止"与后端一致"：不设这条偏好的人绝大多数根本没打开
    过面板，默认值写成"关"等于替他们做了一个他们从没表达过的决定。
    """
    assert service._ALLOWED[service.EXTERNAL_SEARCH_KEY] == ("true", "false")
    # 库里没有这一行 = 允许联网（与 `tools.EXTERNAL_KNOWLEDGE_DEFAULT` 同一个方向）
    assert service.external_search_enabled() is True

    service.set(service.EXTERNAL_SEARCH_KEY, "false")
    assert service.external_search_enabled() is False
    # 面板上显示的是人话，不是 true / false
    assert service._label(service.EXTERNAL_SEARCH_KEY, "false") == "没材料时不联网，直接生成"

    service.set(service.EXTERNAL_SEARCH_KEY, "true")
    assert service.external_search_enabled() is True
    assert service._label(service.EXTERNAL_SEARCH_KEY, "true") == "没材料时先联网搜资料"

    with pytest.raises(service.PrefError) as caught:
        service.set(service.EXTERNAL_SEARCH_KEY, "maybe")
    assert caught.value.code == "pref_value_invalid"

    # 删掉就回到默认（开）：与"没设过"完全等价，不需要第三条状态
    assert service.delete(service.EXTERNAL_SEARCH_KEY) is True
    assert service.external_search_enabled() is True


# ---------------------------------------------------------------- C 类上限


def test_note_count_limit_rejects_the_ninth():
    """8 条写满，第 9 条**拒绝**并说清怎么办——不静默截断。"""
    for index in range(service.MAX_NOTES):
        service.set(f"{service.NOTE_PREFIX}{index}", f"第 {index} 条")
    assert len([row for row in service.list_all()
                if row.key.startswith(service.NOTE_PREFIX)]) == service.MAX_NOTES

    with pytest.raises(service.PrefError) as caught:
        service.set(f"{service.NOTE_PREFIX}{service.MAX_NOTES}", "第九条")
    assert caught.value.code == "pref_limit_exceeded"
    assert "请先删除一条" in caught.value.message


def test_note_char_limit_rejects_overflow():
    """总字数上限按**其余条目之和**算，且改短一条时不会被自己的旧值挡住。"""
    service.set(f"{service.NOTE_PREFIX}1", "a" * 400)
    service.set(f"{service.NOTE_PREFIX}2", "b" * 99)
    assert service.prompt_notes()[0] != ""

    with pytest.raises(service.PrefError) as caught:
        service.set(f"{service.NOTE_PREFIX}3", "c" * 2)
    assert caught.value.code == "pref_limit_exceeded"
    assert "500" in caught.value.message

    # 改自己：400 → 450，其余 99 不变，合计 549 > 500，仍然拒
    with pytest.raises(service.PrefError):
        service.set(f"{service.NOTE_PREFIX}1", "a" * 450)
    # 改自己不该"先把自己的旧值算一遍再判"：400 → 380 时合计 479，通过
    service.set(f"{service.NOTE_PREFIX}1", "a" * 380)
    assert service.get(f"{service.NOTE_PREFIX}1").value == "a" * 380


def test_note_value_is_trimmed_and_empty_is_rejected():
    pref, _ = service.set(f"{service.NOTE_PREFIX}1", "  喜欢短标题  ")
    assert pref.value == "喜欢短标题"
    with pytest.raises(service.PrefError) as caught:
        service.set(f"{service.NOTE_PREFIX}2", "   ")
    assert caught.value.code == "pref_value_empty"


# ---------------------------------------------------------------- 注入


def _system(**kwargs: Any) -> str:
    return _system_text(AgentRunRequest(goal="做一张图", **kwargs), None)


def test_only_notes_reach_the_prompt():
    """A/B 类**不进 prompt**：它们的位置是请求层与生成侧，模型不必记。"""
    service.set(service.DIAGRAM_KEY, "fishbone")
    service.set(service.THINKING_KEY, "feynman")
    service.set(service.ICON_KEY, "false")
    service.set(f"{service.NOTE_PREFIX}1", "喜欢短标题，不要行话")

    text = _system()

    assert "喜欢短标题，不要行话" in text
    for leaked in ("fishbone", "鱼骨图", "feynman", "费曼", "icon", "emoji"):
        assert leaked not in text


def test_notes_sit_between_identity_and_intent_rules():
    """背景排在身份之后、意图规则之前：背景不该盖住这次的指令。"""
    service.set(f"{service.NOTE_PREFIX}1", "喜欢短标题")
    text = _system()

    identity = text.index(_SYSTEM_BASE)
    notes = text.index(service.NOTE_HEADER)
    intent = text.index(_INTENT_RULES["create"])
    assert identity < notes < intent


def test_no_notes_means_no_empty_paragraph():
    """一条备注都没有时整段不出现，也不留一个空标题。"""
    text = _system()
    assert service.NOTE_HEADER not in text
    assert "\n\n" not in text


def test_overflow_is_truncated_from_the_oldest_and_logged(caplog):
    """超限时从最旧的截断，并**留下一条日志**。

    写入侧的约束管不到这种库（手改、旧备份、将来放宽上限），所以读取侧要有一道
    防御，而它必须留下痕迹——用户看到的是"我写了 8 条，它好像只用了 5 条"，
    排查的人得能在日志里找到原因。
    """
    for index in range(service.MAX_NOTES):
        _raw_insert(f"{service.NOTE_PREFIX}{index}", "x" * 100,
                    f"2026-11-0{index + 1}T00:00:00.000Z")

    with caplog.at_level(logging.WARNING, logger="app.agent.service"):
        text = _system()

    # 500 字上限 / 每条 100 字 = 只装得下 5 条
    assert text.count("- " + "x" * 100) == 5
    assert any("最旧的 3 条这次没有进提示词" in record.getMessage()
               for record in caplog.records)


def test_prompt_notes_counts_dropped_rows():
    _raw_insert(f"{service.NOTE_PREFIX}1", "x" * 400, "2026-11-01T00:00:00.000Z")
    _raw_insert(f"{service.NOTE_PREFIX}2", "y" * 400, "2026-11-02T00:00:00.000Z")

    text, dropped = service.prompt_notes()
    assert dropped == 1
    # 新的留下、旧的被截：`2026-11-02` 那条更晚
    assert "y" * 10 in text and "x" * 10 not in text


# ---------------------------------------------------------------- B 类真的改产物


def _generate_with_icons() -> MindMapContent:
    """跑一次完整的分片生成（模型 mock 掉），骨架带 emoji。

    "关掉图标"这件事发生在骨架落定的那一刻，只有跑完整条路才看得见——直接调
    `_skeleton_branches` 测不到它，因为剥离刻意没有放在那个纯解析函数里。
    """
    ctx = RunContext(
        request=AgentRunRequest(goal="做一张图", budget=RunBudget(max_model_calls=10)))

    async def fake(system: str, user: str, *, max_tokens: int | None = None) -> dict:
        if system.startswith(PLAN_PROMPT):
            return {"branches": [
                {"label": "甲", "color": "blue", "icon": "🎯"},
                {"label": "乙", "color": "teal", "icon": "📊"},
                {"label": "丙", "color": "amber", "icon": "⚠️"},
            ]}
        return {"points": ["要点一", "要点二"]}

    with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
        result = asyncio.run(build_registry("create_deep").execute(
            ToolCall(call_id="c-gen", name="mindmap_generate_deep", arguments={
                "topic": "机器学习", "branches": 3, "points_per_branch": 2}), ctx))

    assert result.ok, result.content
    assert ctx.candidate is not None
    return ctx.candidate


def test_icon_enabled_keeps_the_emoji():
    """没有这条偏好（或它是 true）时，一级分支照旧带 emoji。"""
    content = _generate_with_icons()
    assert [branch.icon for branch in content.root.children] == ["🎯", "📊", "⚠️"]


def test_icon_disabled_strips_the_emoji():
    """`icon.enabled=false` → 树上没有 emoji，而颜色和结构一个字都不动。"""
    service.set(service.ICON_KEY, "false")
    content = _generate_with_icons()

    assert [branch.icon for branch in content.root.children] == [None, None, None]
    assert [branch.label for branch in content.root.children] == ["甲", "乙", "丙"]
    # 只剥 icon：颜色是另一个维度，"不要图标"不等于"不要颜色"
    assert [branch.color for branch in content.root.children] == ["blue", "teal", "amber"]


# ---------------------------------------------------------------- 证据：prefs_applied


def test_request_summary_records_prefs_applied():
    """`runs.request` 里要有这一列——这是"这次为什么不一样"的唯一落点。"""
    body = AgentRunRequest(goal="做一张图",
                           prefs_applied=["diagram_type.default=fishbone"])
    summary = _request_summary(body)
    assert summary["prefs_applied"] == ["diagram_type.default=fishbone"]
    # `diagram_type` 的语义没被污染：记的仍是"这次请求实际是什么"
    assert summary["diagram_type"] is None


def test_prefs_applied_lands_in_the_runs_table():
    body = AgentRunRequest(goal="做一张图", diagram_type="fishbone",
                           prefs_applied=["diagram_type.default=fishbone"])
    claim = runs.begin("run-applied", _request_summary(body))
    runs.finish(claim.record.run_id, runs.STATUS_DONE, candidate=EMPTY_TREE)

    stored = runs.get("run-applied")
    assert stored is not None
    assert stored.request["prefs_applied"] == ["diagram_type.default=fishbone"]
    # 两个值在这里必须分得开：`fishbone` 是这次实际画的，`None` 是"用户让我判断"
    assert stored.request["diagram_type"] == "fishbone"


def test_last_applied_comes_from_runs_and_resets_when_value_changes():
    """面板上的"上次生效"直接读 `prefs_applied`；值一改就回到"尚未生效"。"""
    service.set(service.DIAGRAM_KEY, "fishbone")
    _run("fishbone", prefs_applied=["diagram_type.default=fishbone"])

    applied = {row.key: row for row in service.list_all()}[service.DIAGRAM_KEY]
    assert applied.last_applied_at, "带着这条偏好跑过一次，就不该是空的"

    service.set(service.DIAGRAM_KEY, "flowchart")
    fresh = {row.key: row for row in service.list_all()}[service.DIAGRAM_KEY]
    # 值换了之后"上次生效"必须清掉：旧值的生效记录不该挂在新值身上
    assert fresh.last_applied_at is None


def test_last_applied_ignores_runs_without_the_marker():
    """没标 `prefs_applied` 的运行不算生效——那正是"死表"的形状。"""
    service.set(service.DIAGRAM_KEY, "tree")
    _run("tree")  # 用户手选，不是偏好预填
    applied = {row.key: row for row in service.list_all()}[service.DIAGRAM_KEY]
    assert applied.last_applied_at is None


# ---------------------------------------------------------------- 建议


def test_three_identical_choices_become_a_suggestion():
    for _ in range(service.SUGGESTION_WINDOW):
        _run("flowchart")
    found = service.suggestions()
    assert [(item.key, item.value) for item in found] == [
        (service.DIAGRAM_KEY, "flowchart")]
    assert "你最近 3 次都选了流程图" in found[0].message


def test_mixed_choices_produce_nothing():
    _run("flowchart")
    _run("fishbone")
    _run("flowchart")
    assert service.suggestions() == []


def test_unspecified_choices_are_not_choices():
    """`None` 是"用户没说"，不是"用户选了 None"。"""
    for _ in range(service.SUGGESTION_WINDOW + 1):
        _run(None)
    assert service.suggestions() == []


def test_builtin_default_is_not_worth_suggesting():
    """`thinking_model` 每次请求都显式带 `default`，那不是一次表达。"""
    for _ in range(service.SUGGESTION_WINDOW):
        _run("tree", thinking="default")
    keys = [item.key for item in service.suggestions()]
    assert service.THINKING_KEY not in keys


def test_suggestion_disappears_once_stored():
    for _ in range(service.SUGGESTION_WINDOW):
        _run("flowchart")
    assert service.suggestions()

    # 用户点了"设为默认"
    service.set(service.DIAGRAM_KEY, "flowchart",
                source=service.SOURCE_SUGGESTION_ACCEPTED)
    assert service.suggestions() == []
    assert service.get(service.DIAGRAM_KEY).source == "suggestion_accepted"


def test_rejecting_a_suggestion_writes_nothing():
    """用户拒绝 = 什么都不发生。没有写入就没有记录，不需要留痕。"""
    for _ in range(service.SUGGESTION_WINDOW):
        _run("flowchart")
    before = service.list_all()
    service.suggestions()  # 界面刷新一次，只是再算一遍
    assert service.list_all() == before
    assert service.get(service.DIAGRAM_KEY) is None


# ---------------------------------------------------------------- HTTP 面


def test_put_list_delete_roundtrip():
    response = client.put("/api/prefs/diagram_type.default", json={"value": "fishbone"})
    assert response.status_code == 200
    body = response.json()
    assert body["changed"] is True
    assert body["pref"]["value"] == "fishbone"
    assert body["pref"]["source"] == "user_explicit"

    # 同值再来一次：200，但 changed=false
    again = client.put("/api/prefs/diagram_type.default", json={"value": "fishbone"})
    assert again.status_code == 200
    assert again.json()["changed"] is False

    listed = client.get("/api/prefs").json()
    assert [item["key"] for item in listed] == ["diagram_type.default"]
    assert listed[0]["last_applied_at"] is None

    assert client.delete("/api/prefs/diagram_type.default").status_code == 204
    assert client.get("/api/prefs").json() == []
    # 已经没了：404 而不是幂等的 204，界面才能把"我删了"和"其实还在"分开
    assert client.delete("/api/prefs/diagram_type.default").status_code == 404


def test_put_rejects_bad_value_with_a_readable_code():
    response = client.put("/api/prefs/diagram_type.default", json={"value": "orgchart"})
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "pref_value_invalid"
    assert "fishbone" in body["message"]


def test_put_rejects_note_overflow_with_actionable_text():
    for index in range(service.MAX_NOTES):
        client.put(f"/api/prefs/{service.NOTE_PREFIX}{index}", json={"value": "x"})
    response = client.put(f"/api/prefs/{service.NOTE_PREFIX}{service.MAX_NOTES}",
                          json={"value": "y"})
    assert response.status_code == 400
    assert "请先删除一条" in response.json()["message"]


def test_suggestions_endpoint_is_read_only():
    for _ in range(service.SUGGESTION_WINDOW):
        _run("fishbone")

    found = client.get("/api/prefs/suggestions").json()
    assert found[0]["key"] == "diagram_type.default"
    assert found[0]["value"] == "fishbone"
    # 只是读：库里仍然没有这条偏好
    assert client.get("/api/prefs").json() == []


def test_suggestion_accepted_records_its_source():
    for _ in range(service.SUGGESTION_WINDOW):
        _run("fishbone")
    client.put("/api/prefs/diagram_type.default",
               json={"value": "fishbone", "source": "suggestion_accepted"})
    listed = client.get("/api/prefs").json()
    # 界面按它显示"系统建议（你已同意）"而不是"你设置的"
    assert listed[0]["source"] == "suggestion_accepted"


# ---------------------------------------------------------------- 验收标准


def _frames(text: str) -> list[dict]:
    out: list[dict] = []
    for block in text.split("\n\n"):
        line = block.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            continue
        out.append(json.loads(payload))
    return out


def _branches_in(user: str) -> list[str]:
    """从生成那一次的 user 消息里取出骨架清单（`1. 甲` 这样的行）。"""
    marker = "不要增删改名）：\n"
    if marker not in user:
        return []
    titles: list[str] = []
    for line in user.split(marker, 1)[1].splitlines():
        if not line.strip():
            break
        head, _, tail = line.partition(". ")
        if head.isdigit() and tail:
            titles.append(tail)
    return titles


def _whole_tree(user: str) -> dict:
    """整树一次生成的替身：按骨架清单造一棵两层的树。"""
    titles = _branches_in(user)
    return {"title": "机器学习", "root": {
        "id": "root", "label": "机器学习", "children": [
            {"id": f"n{index}", "label": title, "children": [
                {"id": f"n{index}-1", "label": f"{title}·一", "detail": "一句说明。"},
                {"id": f"n{index}-2", "label": f"{title}·二", "detail": "另一句说明。"},
            ]}
            for index, title in enumerate(titles)
        ]}}


def _acceptance_generate(run_id: str, body: dict[str, Any]) -> dict:
    """跑一次真的生成（模型两层都 mock 掉），返回 done 帧的内容。

    骨架响应按 system 是哪个图型的提示词分派——这正是"图型偏好真的改了产物"能被
    证伪的地方：如果请求层的 `diagram_type` 根本没有传到骨架那一次调用，两边的
    提示词会长得一样，返回的分支也会一样。

    两层都要 mock：外层循环走的是 `provider.complete_tools`（`default_provider`
    在没有凭证时直接报错，**不会**走到工具内部），工具内部的展开走 `chat_async`。
    只 mock 后者时，流会在第一轮就断在"请配置模型地址"上。
    """
    async def fake_chat(system: str, user: str, *, max_tokens: int | None = None,
                        on_delta: Any = None) -> dict:
        if system.startswith(FISHBONE_PLAN_PROMPT):
            # 鱼骨的骨架现在还带一句主骨陈述（`problem`）：产物是 `FishboneContent`，
            # 那一句直接落进 `problem` 字段，不再只是一句"分了六个类"。
            return {"problem": "为什么机器学习项目总在上线前卡住",
                    "branches": ["人", "机", "料", "法", "环", "测"]}
        if system.startswith(PLAN_PROMPT):
            return {"branches": ["甲", "乙", "丙"]}
        return _whole_tree(user)

    class OneShot:
        """第一轮发一次 `mindmap_generate`，第二轮收尾。"""

        def __init__(self) -> None:
            self.rounds = 0

        async def complete_tools(self, messages, tools, *, budget):
            self.rounds += 1
            if self.rounds > 1:
                return Completion(message={"role": "assistant", "content": "好了"},
                                  finish_reason="stop")
            call = ToolCall(
                call_id="c-gen", name="mindmap_generate",
                arguments={"topic": "机器学习", "branches": 6})
            return Completion(
                message={"role": "assistant", "content": "生成", "tool_calls": [
                    {"id": call.call_id, "type": "function", "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False)}}]},
                tool_calls=[call], finish_reason="tool_calls", usage={"total_tokens": 1})

    with (
        patch("app.agent.service.default_provider", return_value=OneShot()),
        patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake_chat)),
    ):
        response = client.post("/api/agent/runs/stream", json={"run_id": run_id, **body})

    assert response.status_code == 200, response.text
    done = [frame for frame in _frames(response.text) if frame["type"] == "done"]
    assert done, response.text
    return done[-1]["content"]


def test_acceptance_a_stored_pref_changes_the_product():
    """验收标准：改一个值 → 生成一次 → `prefs_applied` 非空 → 产物确实变了。

    走的是**完整链路**：HTTP 写偏好（与面板同一个接口）→ 前端预填之后的请求体 →
    SSE 生成 → `runs` 落库。分段验的时候每一段都能过，接起来仍然可能什么都不生效
    ——那正是"死表"的形状。
    """
    # 1. 用户在面板里把默认图型改成鱼骨图
    assert client.put("/api/prefs/diagram_type.default",
                      json={"value": "fishbone"}).status_code == 200

    # 2. 前端启动拉偏好、预填下拉，于是这一次请求带着**界面上显示的值**
    listed = client.get("/api/prefs").json()
    assert [(item["key"], item["value"]) for item in listed] == [
        ("diagram_type.default", "fishbone")]
    prefill = listed[0]["value"]

    product = _acceptance_generate("run-acceptance", {
        "goal": "机器学习", "intent": "create",
        "diagram_type": prefill,
        "prefs_applied": [f"diagram_type.default={prefill}"],
    })

    # 3. 产物确实是鱼骨图：**形状**是 `bones`（不再是一棵标着 fishbone 的树），
    #    六根骨是骨架给的六个类别，主骨那句话来自骨架的 `problem`。
    assert product["diagram_type"] == "fishbone"
    assert [bone["category"] for bone in product["bones"]] == [
        "人", "机", "料", "法", "环", "测"]
    assert product["problem"] == "为什么机器学习项目总在上线前卡住"
    assert "root" not in product, "鱼骨图的产物里没有中心主题这个位置"

    # 4. 台账里留下了"这次生效了哪条偏好"
    stored = runs.get("run-acceptance")
    assert stored is not None
    assert stored.request["prefs_applied"] == ["diagram_type.default=fishbone"]

    # 5. 面板上这条偏好因此显示"上次生效"，而不是"尚未生效"
    applied = {item.key: item for item in service.list_all()}["diagram_type.default"]
    assert applied.last_applied_at, "生成过一次之后必须能反查到生效时间"


def test_acceptance_without_the_pref_the_product_is_the_default():
    """对照组：同一条链路、同一次生成，没有偏好时产物是默认图型。

    "产物确实变了"这句话只有在两个产物不一样时才成立。少了这一条，上面那条
    断言只能证明"这次生成了鱼骨图"，证不出"是因为那条偏好"。
    """
    product = _acceptance_generate("run-baseline", {"goal": "机器学习", "intent": "create"})
    assert product["diagram_type"] == "tree"
    assert [child["label"] for child in product["root"]["children"]] == ["甲", "乙", "丙"]


def test_acceptance_runs_request_keeps_none_and_tree_apart():
    """回归：`None` 与 `"tree"` 在台账里仍然分得开。

    偏好走的是**请求层预填**，不是"后端开流前偷偷塞一个值"。这一条钉住那个区别：
    没有偏好的请求，`diagram_type` 仍然记 `None`（"用户让我自己判断"）。
    """
    _acceptance_generate("run-none", {"goal": "机器学习", "intent": "create"})
    stored = runs.get("run-none")
    assert stored is not None
    assert stored.request["diagram_type"] is None
    assert stored.request["prefs_applied"] == []
