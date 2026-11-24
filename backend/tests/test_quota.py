"""公共免费额度：每 IP 每天几次，用完就 429。

这一层验四件事：

1. **记账本身**：`consume` 数到上限就返回 False；`refund` 还回去；跨天自动归零；
   跨 IP 互不影响（否则"同一个人"与"所有人"没有区别）。
2. **降级方向**：表读写失败当作还有额度。与 `auth/throttle.py` 同一条理由——
   反过来会把所有人关在门外，而攻击者没法让表坏掉。
3. **接线**：`resolve()` 在有公共密钥、而调用方没有自己密钥时把来源标成
   `public`；用户自己存了密钥就不标（他花自己的钱）。
4. **闸的位置**：429 发生在**开流之前**，且只在真会用公共密钥时。演示模式与
   自带密钥的人都不该被扣——包括"同一个 run_id 重放"和"撞上 409"这两种
   **什么都没做**的请求，占过的那一格必须还回去。

时间在测试里不睡：`day_key()` 与 `seconds_until_tomorrow()` 都接受 `now`，
跨天那条用例直接把 `now` 往后拨一天。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.agent.router as agent_router
from app.agent import runs
from app.agent.contracts import AgentRunRequest, DoneEvent, RunBudget
from app.agent.router import router
from app.contracts.mindmap import MindMapContent
from app.core.errors import register_error_handlers
from app.generation import free_models, quota
from app.generation.service import GenerationError
from app.settings import store
from app.settings.router import router as settings_router

PUBLIC_KEY = "user-public-key-9999"
URL = "/api/agent/runs/stream"

TREE = {
    "title": "示例",
    "root": {"id": "root", "label": "主题", "children": [
        {"id": "a", "label": "甲", "children": []},
        {"id": "b", "label": "乙", "children": []},
        {"id": "c", "label": "丙", "children": []},
    ]},
}
CANDIDATE = MindMapContent.model_validate(TREE)

# 只挂 agent 与 settings 两条路由：这一层验的是"闸在哪、扣几次"，不是中间件或身份层。
app = FastAPI()
register_error_handlers(app)
app.include_router(router, prefix="/api")
app.include_router(settings_router, prefix="/api")
client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean():
    """每个用例从"空表、没开关"开始。

    额度表与实际数据同库，不清的话前一个用例用掉的次数会让后一个用例
    一开始就 429——那种失败会表现得像"偶尔 429"。
    """
    quota.clear_all()
    free_models.reset()
    yield
    quota.clear_all()
    free_models.reset()


def _db() -> sqlite3.Connection:
    from app.mindmaps import repository

    return repository.connect()


def _rows() -> list[tuple]:
    conn = _db()
    try:
        return list(conn.execute(
            f"SELECT bucket, day, used FROM {quota.TABLE} ORDER BY bucket, day"))
    finally:
        conn.close()


def _enable(monkeypatch, *, limit: int | None = None) -> None:
    monkeypatch.setenv(quota.PUBLIC_KEY_ENV, PUBLIC_KEY)
    if limit is not None:
        monkeypatch.setenv(quota.LIMIT_ENV, str(limit))


def _llm_env(monkeypatch) -> None:
    """一套"能发请求"的模型配置，且**没有**调用方自己存的密钥。"""
    monkeypatch.setenv("MINDMAP_LLM_BASE_URL", "https://api.commandcode.ai/provider/v1")
    monkeypatch.setenv("MINDMAP_LLM_API_KEY", "user-deployer-key-1111")
    monkeypatch.setenv("MINDMAP_LLM_MODEL", "vendor/paid-model")


async def _scripted(*events):
    for event in events:
        yield event


def _post(run_id: str, *, ip: str = "10.0.0.1"):
    return client.post(URL,
                       json={"goal": "做一张导图", "intent": "create", "run_id": run_id},
                       headers={"X-Real-IP": ip})


# ---------------------------------------------------------------- 1. 记账


def test_the_switch_is_the_key_itself():
    assert quota.enabled() is False
    assert quota.public_key() == ""


def test_a_public_key_turns_it_on(monkeypatch):
    _enable(monkeypatch)
    assert quota.enabled() is True
    assert quota.public_key() == PUBLIC_KEY


def test_the_limit_defaults_to_three(monkeypatch):
    assert quota.limit() == quota.DEFAULT_LIMIT == 3
    monkeypatch.setenv(quota.LIMIT_ENV, "7")
    assert quota.limit() == 7


def test_a_broken_limit_falls_back_to_the_default(monkeypatch, caplog):
    """写错就退回默认值并留下痕迹：静默用 3 次会让人以为环境变量没生效。"""
    monkeypatch.setenv(quota.LIMIT_ENV, "三次")
    with caplog.at_level(logging.WARNING):
        assert quota.limit() == quota.DEFAULT_LIMIT
    assert quota.LIMIT_ENV in caplog.text


@pytest.mark.parametrize("raw", ["0", "-1", "101", "1000"])
def test_a_limit_outside_the_sane_range_is_refused(monkeypatch, raw):
    monkeypatch.setenv(quota.LIMIT_ENV, raw)
    assert quota.limit() == quota.DEFAULT_LIMIT


def test_an_empty_address_still_shares_one_bucket():
    """没有 IP 不是"不限"，是"所有人共用一个桶"。"""
    assert quota.bucket("") == "?"
    assert quota.bucket("   ") == "?"


def test_consume_counts_up_to_the_limit(monkeypatch):
    _enable(monkeypatch)
    assert [quota.consume("10.0.0.1") for _ in range(4)] == [True, True, True, False]
    assert quota.remaining("10.0.0.1") == 0


def test_remaining_never_goes_negative(monkeypatch):
    _enable(monkeypatch)
    quota.clear_all()
    assert quota.remaining("10.0.0.9") == 3
    quota.consume("10.0.0.9")
    assert quota.remaining("10.0.0.9") == 2


def test_another_address_has_its_own_count(monkeypatch):
    _enable(monkeypatch)
    for _ in range(3):
        quota.consume("10.0.0.1")
    assert quota.consume("10.0.0.1") is False
    assert quota.consume("10.0.0.2") is True


def test_refund_gives_the_slot_back(monkeypatch):
    _enable(monkeypatch)
    for _ in range(3):
        quota.consume("10.0.0.1")
    quota.refund("10.0.0.1")
    assert quota.consume("10.0.0.1") is True


def test_refund_never_makes_the_count_negative(monkeypatch):
    _enable(monkeypatch)
    quota.refund("10.0.0.1")
    quota.refund("10.0.0.1")
    assert quota.remaining("10.0.0.1") == 3
    assert _rows() == []


def test_a_new_day_starts_over(monkeypatch):
    _enable(monkeypatch)
    for _ in range(3):
        quota.consume("10.0.0.1")
    assert quota.consume("10.0.0.1") is False
    # 把"今天"往后拨一天：旧的桶键对不上，新的一天从 0 开始。
    tomorrow = quota.day_key()
    with patch.object(quota, "day_key", return_value=f"{tomorrow}+1"):
        assert quota.consume("10.0.0.1") is True


def test_yesterday_rows_are_cleaned_up(monkeypatch):
    """表只按天用，旧行白占空间——顺手清掉是唯一一次写入时做的事。"""
    _enable(monkeypatch)
    conn = _db()
    try:
        conn.execute(
            f"INSERT INTO {quota.TABLE} (bucket, day, used, last_at) VALUES (?, ?, 3, 0)",
            ("10.0.0.1", "1999-01-01"))
        conn.commit()
    finally:
        conn.close()
    assert len(_rows()) == 1
    quota.consume("10.0.0.2")
    assert [row[1] for row in _rows()] == [quota.day_key()]


def test_seconds_until_tomorrow_is_within_a_day():
    wait = quota.seconds_until_tomorrow()
    assert 1 <= wait <= 24 * 3600


def test_day_key_is_local_and_zero_padded():
    key = quota.day_key(now=1_700_000_000)
    assert len(key) == 10 and key[4] == "-" and key[7] == "-"


def test_clear_all_wipes_the_table(monkeypatch):
    _enable(monkeypatch)
    quota.consume("10.0.0.1")
    quota.consume("10.0.0.2")
    assert quota.clear_all() == 2
    assert _rows() == []


# ---------------------------------------------------------------- 2. 降级方向


def test_a_broken_table_lets_everyone_in(monkeypatch, caplog):
    _enable(monkeypatch)

    def broken():
        raise sqlite3.OperationalError("database is locked")

    with patch.object(quota, "_connect", new=broken):
        with caplog.at_level("ERROR"):
            assert quota.consume("10.0.0.1") is True
            assert quota.remaining("10.0.0.1") == quota.limit()
    assert any("免费额度" in record.message for record in caplog.records)


def test_a_failed_refund_only_warns(monkeypatch, caplog):
    """还不回去只是多扣一格，不该让一条已经结束的流变成 500。"""
    _enable(monkeypatch)

    def broken():
        raise sqlite3.OperationalError("database is locked")

    with patch.object(quota, "_connect", new=broken):
        with caplog.at_level("WARNING"):
            quota.refund("10.0.0.1")  # 不抛就是通过
    assert any("退回" in record.message for record in caplog.records)


def test_the_table_comes_up_with_the_repository():
    quota.clear_all()
    conn = _db()
    try:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()
    assert quota.TABLE in names


# ---------------------------------------------------------------- 3. 接线：来源


def test_the_public_key_serves_whoever_has_no_key_of_their_own(
        monkeypatch):
    _enable(monkeypatch)
    _llm_env(monkeypatch)
    resolved = store.resolve()
    assert resolved.api_key == PUBLIC_KEY
    assert resolved.api_key_source == store.SOURCE_PUBLIC


def test_the_public_key_does_not_cover_a_users_own_key(monkeypatch):
    """用户自己填了密钥就按他的意思走——与免费档同一条分界。"""
    _enable(monkeypatch)
    _llm_env(monkeypatch)
    response = client.put("/api/settings/model", json={
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-mine-0000",
        "model": "deepseek-chat",
    })
    assert response.status_code == 200, response.text
    assert response.json()["api_key_source"] == store.SOURCE_SAVED
    assert response.json()["api_key_masked"] == "…0000"


def test_no_public_key_leaves_the_borrowed_key_alone(monkeypatch):
    _llm_env(monkeypatch)
    resolved = store.resolve()
    assert resolved.api_key == "user-deployer-key-1111"
    assert resolved.api_key_source == store.SOURCE_ENV


def test_the_public_key_takes_the_free_tier_with_it(monkeypatch):
    """公共密钥**就是**给免费模型用的：来源是 `public`，模型名照样换成免费的。"""
    _enable(monkeypatch)
    _llm_env(monkeypatch)
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    base = "https://api.commandcode.ai/provider/v1"
    with free_models._LOCK:
        free_models._CACHE[base] = (10**9, ["recursive/flint-v1.0:free"])
    resolved = store.resolve()
    assert resolved.model == "recursive/flint-v1.0:free"
    assert resolved.model_source == store.SOURCE_FREE
    assert resolved.reasoning_effort == store.FREE_REASONING_EFFORT


def test_the_free_model_list_is_asked_for_with_the_public_key(monkeypatch):
    _enable(monkeypatch)
    _llm_env(monkeypatch)
    base, key = store.free_tier_endpoint()
    assert base == "https://api.commandcode.ai/provider/v1"
    assert key == PUBLIC_KEY


# ---------------------------------------------------------------- 4. 闸的位置


def _allow(events: tuple[Any, ...] = ()):
    """把 `run()` 换掉：不碰模型，按脚本出帧。"""
    return _scripted(*(events or (DoneEvent(seq=1, content=CANDIDATE, mode="model"),)))


def test_no_limit_without_a_public_key():
    completed: list[AgentRunRequest] = []

    def fake_run(body, **_kwargs):
        completed.append(body)
        return _allow()

    with patch.object(agent_router, "run", new=fake_run):
        for index in range(5):
            response = _post(f"free-{index}")
            assert response.status_code == 200, response.text
    assert len(completed) == 5


def test_three_runs_then_429(monkeypatch):
    _enable(monkeypatch)
    _llm_env(monkeypatch)
    with patch.object(agent_router, "run", new=lambda body, **kw: _allow()):
        for index in range(quota.DEFAULT_LIMIT):
            assert _post(f"ok-{index}").status_code == 200
        response = _post("over")
    assert response.status_code == 429
    body = response.json()
    assert body["code"] == "free_quota_exhausted"
    assert "明天" in body["message"]
    # 头里给的是"到本地零点还有多久"，不是固定 5 秒：这是"明天再来"不是"等一会"。
    assert 1 <= int(response.headers["Retry-After"]) <= 24 * 3600


def test_the_429_happens_before_any_run_is_recorded(monkeypatch):
    """闸在门口：被拒的那次不建台账行、也不发模型调用。"""
    _enable(monkeypatch)
    _llm_env(monkeypatch)
    for _ in range(quota.DEFAULT_LIMIT):
        quota.consume("10.0.0.1")
    started: list[str] = []

    def should_not_start(body, **_kwargs):
        started.append(body.goal)
        return _allow()

    with patch.object(agent_router, "run", new=should_not_start):
        response = _post("blocked")
    assert response.status_code == 429
    assert started == []
    assert runs.get("blocked") is None


def test_your_own_key_is_never_counted(monkeypatch):
    """`saved` 的人花自己的钱：超额也不该被拦。"""
    _enable(monkeypatch)
    _llm_env(monkeypatch)
    response = client.put("/api/settings/model", json={
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-mine-0000",
        "model": "deepseek-chat",
    })
    assert response.status_code == 200, response.text
    for _ in range(quota.DEFAULT_LIMIT + 2):
        quota.consume("10.0.0.1")  # 先把公共池用光
    with patch.object(agent_router, "run", new=lambda body, **kw: _allow()):
        assert _post("mine").status_code == 200


def test_demo_mode_is_not_counted(monkeypatch):
    """演示模式一次模型都不调，扣它等于"看了个样例就没额度了"。"""
    _enable(monkeypatch)
    _llm_env(monkeypatch)
    monkeypatch.setenv("MINDMAP_LLM_MODE", "demo")
    for _ in range(quota.DEFAULT_LIMIT):
        quota.consume("10.0.0.1")
    with patch.object(agent_router, "run", new=lambda body, **kw: _allow()):
        assert _post("demo").status_code == 200


def test_a_duplicate_run_id_gives_the_slot_back(monkeypatch):
    """409 那次什么都没执行，不该白扣一格。"""
    _enable(monkeypatch)
    _llm_env(monkeypatch)
    runs.begin("dup", {"goal": "g"})
    response = _post("dup")
    assert response.status_code == 409
    assert quota.remaining("10.0.0.1") == quota.DEFAULT_LIMIT


def test_replaying_a_finished_run_gives_the_slot_back(monkeypatch):
    """刷新页面捡回上一次结果不调模型，不是新的一次生成。"""
    _enable(monkeypatch)
    _llm_env(monkeypatch)
    runs.begin("done-1", {"goal": "g"})
    runs.finish("done-1", runs.STATUS_DONE, candidate=CANDIDATE, mode="model")
    response = _post("done-1")
    assert response.status_code == 200
    assert "done" in response.text
    assert quota.remaining("10.0.0.1") == quota.DEFAULT_LIMIT


def test_a_run_that_produces_nothing_gives_the_slot_back(monkeypatch):
    """失败那次用户什么都没拿到，额度要还回去。"""

    async def failing():
        raise GenerationError("模型服务不可用")
        yield  # pragma: no cover

    _enable(monkeypatch)
    _llm_env(monkeypatch)
    with patch.object(agent_router, "run", new=lambda body, **kw: failing()):
        response = _post("failed")
    assert response.status_code == 200  # 错误在流里，不是一个 HTTP 错误
    assert "generation_failed" in response.text
    assert quota.remaining("10.0.0.1") == quota.DEFAULT_LIMIT


def test_a_partial_tree_is_still_charged(monkeypatch):
    """半棵树交付是有产出的、真的调用过模型，照扣。

    判据是"有没有候选"而不是"有没有 error 帧"：这两者在半棵树那条路上
    同时成立，按帧判会把每一次"跑到一半预算用完"都退成免费。
    """

    def partial_run(body, **_kwargs):
        return _scripted(DoneEvent(seq=1, content=CANDIDATE, mode="model",
                                   partial=True, partial_reason="预算用完了"))

    _enable(monkeypatch)
    _llm_env(monkeypatch)
    with patch.object(agent_router, "run", new=partial_run):
        assert _post("partial").status_code == 200
    assert quota.remaining("10.0.0.1") == quota.DEFAULT_LIMIT - 1


def test_two_addresses_do_not_share_the_public_pool(monkeypatch):
    _enable(monkeypatch)
    _llm_env(monkeypatch)
    with patch.object(agent_router, "run", new=lambda body, **kw: _allow()):
        for index in range(quota.DEFAULT_LIMIT):
            assert _post(f"a-{index}", ip="10.0.0.1").status_code == 200
        assert _post("a-over", ip="10.0.0.1").status_code == 429
        assert _post("b-1", ip="10.0.0.2").status_code == 200


def test_the_budget_of_a_request_is_untouched():
    """额度是"次数"，不是"这一轮能调几次模型"——两者都被叫 budget 过，别混。"""
    request = AgentRunRequest(goal="g", budget=RunBudget(max_model_calls=12))
    assert request.budget.max_model_calls == 12


# ------------------------------------------------- 5. 公共密钥只发给部署方选的地址


def test_a_caller_chosen_address_gets_no_public_key(monkeypatch):
    """**谁能选地址，谁就能领走公共密钥**——所以地址必须也是部署方选的。

    这是 P0-1 那个形状的复发点，而且比 P0-1 更容易：P0-1 需要"改地址 + 点测试连接"
    两步，这里只要一步 `PUT /settings/model {"base_url": ...}`（不带密钥，所以连
    `settings_key_unbound` 那道防线都不经过），下一次生成就会把公共钥匙发过去。
    """
    _enable(monkeypatch)
    _llm_env(monkeypatch)
    response = client.put("/api/settings/model",
                          json={"base_url": "https://attacker.example/v1"})
    assert response.status_code == 200, response.text
    # 用户自己写的地址被如实记下来了……
    assert response.json()["base_url"] == "https://attacker.example/v1"

    resolved = store.resolve()
    # ……但公共密钥**不跟着走**：它只认识部署方配的那台端点。而且这里其实有
    # 两道防线先后生效，值得写下来：P0-1 的绑定校验先把那把环境变量密钥判成
    # `unbound`（它的锚点是 commandcode，新地址是 attacker），公共密钥这一档
    # 再看 `base_url_source` 是 `saved` —— 两道都指向同一个结论"不交出去"。
    # 只留一道的话，任何一处判据写松都会静默复发，所以两条都按下。
    assert resolved.api_key_source != store.SOURCE_PUBLIC
    assert resolved.api_key == ""
    assert resolved.api_key_source == store.SOURCE_UNBOUND


def test_the_deployers_address_still_gets_the_public_key(monkeypatch):
    """上一半的反面：地址来自部署方（实例行/环境变量）时公共密钥照给。

    没有这一条，上面那半用"永远不给"也能过——那会让整个公共额度不可用。
    """
    _enable(monkeypatch)
    _llm_env(monkeypatch)
    resolved = store.resolve()
    assert resolved.base_url == "https://api.commandcode.ai/provider/v1"
    assert resolved.base_url_source == store.SOURCE_ENV
    assert resolved.api_key == PUBLIC_KEY
    assert resolved.api_key_source == store.SOURCE_PUBLIC


def test_a_stored_key_is_never_replaced_by_the_public_one(monkeypatch):
    """用户自己存过密钥时，地不地址是他自己的事，公共钥匙不参与。"""
    _enable(monkeypatch)
    _llm_env(monkeypatch)
    assert client.put("/api/settings/model", json={
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-mine-0000",
    }).status_code == 200
    resolved = store.resolve()
    assert resolved.api_key == "sk-mine-0000"
    assert resolved.api_key_source == store.SOURCE_SAVED


def test_a_caller_chosen_address_is_not_charged_against_the_public_pool(
        monkeypatch):
    """顺便钉住闸与来源一致：拿不到公共密钥的请求，不该被扣一格。"""
    _enable(monkeypatch)
    _llm_env(monkeypatch)
    assert client.put("/api/settings/model",
                      json={"base_url": "https://attacker.example/v1"}).status_code == 200
    with patch.object(agent_router, "run", new=lambda body, **kw: _allow()):
        # 没密钥发不出去，但"没扣额度"这件事与它能不能发出去无关：判据是来源。
        _post("chosen")
    assert quota.remaining("10.0.0.1") == quota.DEFAULT_LIMIT
    before = _rows()
    assert before == [] or all(row[2] == 0 for row in before)
