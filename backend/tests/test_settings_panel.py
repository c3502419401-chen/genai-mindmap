"""模型设置：面板里改、立即生效、密钥不出门。

这个文件钉四件事，每件都对应一种"看起来能跑、实际上是错的"形状：

1. **回落顺序**：库里的值 > 环境变量 > 空。九条路径里只要有一条自己读环境变量，
   用户在面板里改完就会得到"生成换了、视觉提取没换"的半生效系统。
2. **密钥不出门**：`GET` / `PUT` 的响应只有打码值（`…abcd`），错误文案与日志里
   不出现明文。这不是防"有服务器权限的人"（他们能读 SQLite），是防截图、devtools
   的 Network 面板、以及出错时被拼进某条日志。
3. **两种「空」的语义不同**：`api_key` 的空串 = **不改**（界面显示打码值，用户
   不动它就提交空串）；`base_url` / `model` 的空串 = **清掉覆盖、回落环境变量**。
   要清密钥必须显式 `clear_api_key=true`。
4. **测试连接是一次真请求的形状**：`max_tokens` 极小、失败按状态码给可读中文、
   200 但正文不是 JSON 也要判失败（有的端点 404 会伪装成 200）。

单测不发真网络：`tests/conftest.py` 的 `no_real_http` 会把传输层换成断言失败，
所以这里替换 `probe._client()` 造一个假客户端。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import closing

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import register_error_handlers
from app.generation import service as generation_service
from app.generation.service import DEEP_MODEL_FALLBACK, ModelNotConfigured
from app.ingestion import vision_pdf
from app.mindmaps import repository
from app.settings import probe as probe_module
from app.settings import store
from app.settings.router import router as settings_router

api = FastAPI()
register_error_handlers(api)
api.include_router(settings_router, prefix="/api")
client = TestClient(api)

#: 一把形状像真的密钥。用例里到处断言它**没有**出现在响应/日志里。
SECRET = "sk-live-abcdefghijklmnop-1234"
SECRET_TAIL = "1234"


@pytest.fixture(autouse=True)
def _reset_module_state():
    """每个用例都把两个进程级全局量复位。

    `_DEGRADED_WARNED`：不复位的话，前一个用例制造的读失败会让后一个用例看不到
    本该出现的告警——"证明降级会留痕"那条用例就会随机绿。

    `_CACHE`：读缓存跨用例存活。键里带了库路径，正常情况下不会串，但"直接写库再
    读"的用例会撞上自己上一次读过的旧值——那种失败读起来像"保存没生效"。
    """
    store._DEGRADED_WARNED = False
    store._CACHE.clear()
    yield
    store._DEGRADED_WARNED = False
    store._CACHE.clear()


def _body(**kwargs) -> dict:
    return kwargs


# ---------------------------------------------------------------- 读

def test_empty_config_is_not_configured_and_has_no_mask():
    """什么都没配：`configured=false`，打码值是空串而不是一个假的 `…`。"""
    body = client.get("/api/settings/model").json()
    assert body["configured"] is False
    assert body["base_url"] == ""
    assert body["api_key_masked"] == ""
    assert body["api_key_set"] is False
    assert body["base_url_source"] == store.SOURCE_UNSET
    assert body["api_key_source"] == store.SOURCE_UNSET
    assert body["model_source"] == store.SOURCE_UNSET
    assert body["updated_at"] is None


def test_saved_key_is_masked_down_to_the_last_four():
    """`GET` 只给打码值，且只露末 4 位。**整串不出现在响应文本里**。"""
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    response = client.get("/api/settings/model")
    body = response.json()
    assert SECRET not in response.text
    assert body["api_key_masked"] == "…" + SECRET_TAIL
    assert body["api_key_masked"].endswith(SECRET_TAIL)
    assert body["api_key_set"] is True
    # 打码值里不能带供应商前缀：那只是命名习惯，露出来对用户没信息量。
    assert "sk-" not in body["api_key_masked"]


def test_short_key_is_fully_masked():
    """8 位以内的口令只给 `…`：露末 4 位等于送出去一半以上。"""
    client.put("/api/settings/model", json=_body(api_key="abc123"))
    assert store.mask_api_key("abc123") == "…"
    assert client.get("/api/settings/model").json()["api_key_masked"] == "…"


def test_put_response_does_not_leak_the_key_either():
    """`PUT` 的响应与 `GET` 同一形状，同样只有打码值。"""
    response = client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    assert response.status_code == 200
    assert SECRET not in response.text
    assert response.json()["api_key_masked"] == "…" + SECRET_TAIL


# ---------------------------------------------------------------- 写：立即生效

def test_put_takes_effect_without_a_restart():
    """**这条是整张卡的核心**：`PUT` 之后 `_config()` 立刻拿到新值。

    `_config()` 不走缓存，每次重新解析——所以不需要重启，也就不会打断正在跑的
    长图。断言打的是"所有出口共用的那个函数"，不是某个中间变量。
    """
    assert store.resolve().base_url == ""
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))

    base, key, model = generation_service._config()
    assert (base, key, model) == ("https://api.deepseek.com/v1", SECRET, "deepseek-chat")


def test_trailing_slash_on_base_url_is_normalised():
    """`https://x/v1/` 会打到 `//chat/completions`；少数端点直接 404，而 404 只能
    读成"模型名不对"。规范化与改动前逐字节一致。"""
    client.put("/api/settings/model", json=_body(base_url="https://x.example/v1/"))
    assert store.resolve().base_url == "https://x.example/v1"


def test_saving_identical_values_reports_unchanged():
    """同值重复保存不刷 `updated_at`，并如实回 `changed=false`。

    否则界面上会出现一条"刚刚改过"、其实一直没变的记录。
    """
    payload = _body(base_url="https://api.deepseek.com/v1", api_key=SECRET,
                    model="deepseek-chat")
    first = client.put("/api/settings/model", json=payload).json()
    assert first["changed"] is True
    stamp = first["updated_at"]
    second = client.put("/api/settings/model", json=payload).json()
    assert second["changed"] is False
    assert second["updated_at"] == stamp


def test_changing_the_address_says_the_old_test_no_longer_holds():
    """改地址后回一句提醒：上一次「测试连接」成功，证明的是**旧地址**。

    这是 P0-1 的第二层。光靠第一层（拒内网）挡不住"改指向一个公网攻击者地址、
    再让别人点测试"——那条路径每一跳看起来都合法。提醒不会阻止谁，但它让
    "我刚改过地址"这件事在保存的那一刻出现在眼前，而不是等生成 401 之后。
    """
    first = client.put("/api/settings/model", json=_body(
        base_url="https://a.example/v1", api_key=SECRET, model="m")).json()
    assert first["notice"] is None, "第一次保存没有「旧地址」可言"

    moved = client.put("/api/settings/model", json=_body(base_url="https://b.example/v1")).json()
    assert moved["notice"]
    assert "测试连接" in moved["notice"]

    # 再存一次同样的地址：没有变化，就不该继续念叨。
    again = client.put("/api/settings/model", json=_body(base_url="https://b.example/v1")).json()
    assert again["notice"] is None


def test_trailing_slash_alone_is_not_a_change():
    """只补/去一个末尾斜杠不算改地址——规范化之后两者是同一个端点。

    把它当"改了"会让每次保存都弹一句"建议重新测试"，提醒一多就成了噪声。
    """
    client.put("/api/settings/model", json=_body(base_url="https://x.example/v1"))
    body = client.put("/api/settings/model", json=_body(base_url="https://x.example/v1/")).json()
    assert body["notice"] is None


def test_empty_api_key_means_keep_the_saved_one():
    """**语义 1**：界面上那个框显示打码值，用户不动它就提交空串 → 不改。

    把空串当"清空"，等于每次改个模型名就把密钥抹掉一次。
    """
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    response = client.put("/api/settings/model", json=_body(api_key="", model="deepseek-chat"))
    assert response.json()["api_key_set"] is True
    assert store.saved().api_key == SECRET


def test_posting_the_masked_key_back_is_rejected_not_saved():
    """**语义 1 的第三条边界**：把界面上看到的打码值原样提交，不许当密钥存。

    界面把 `…1234` 放在 placeholder 里，用户不填就不会提交它；但**接口**不设防时，
    任何直接调后端的人（或将来把 placeholder 换成 value 的改动）都会踩到：库里
    那把真密钥被字面量 `…1234` 顶掉，而响应照旧 `api_key_set: true`、来源写
    "你在面板里设的"——之后每一次模型调用都 401，界面却显示"刚配好"。
    """
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    masked = client.get("/api/settings/model").json()["api_key_masked"]
    assert masked == "…" + SECRET_TAIL

    response = client.put("/api/settings/model", json=_body(api_key=masked))
    assert response.status_code == 400
    assert response.json()["code"] == "settings_api_key_masked"
    # 真的没被改：库里的密钥还是原来那把。
    assert store.saved().api_key == SECRET


def test_posting_the_mask_of_an_env_key_is_rejected_too(monkeypatch):
    """密钥只在环境变量里时，打码值来自环境变量——这一段最疼，也必须挡住。

    只比"库里那一行"会漏掉这条路：库是空的，`mask_api_key("")` 是空串，判据
    不成立；于是 `…1234` 被存进库，**顶掉了环境变量里那把真密钥**，而且
    `api_key_source` 从 `env` 翻成 `saved`——用户以为"我什么都没改"。
    """
    monkeypatch.setenv(store.BASE_URL_ENV, "https://env.example/v1")
    monkeypatch.setenv(store.API_KEY_ENV, SECRET)
    monkeypatch.setenv(store.MODEL_ENV, "env-model")
    masked = client.get("/api/settings/model").json()["api_key_masked"]
    assert masked == "…" + SECRET_TAIL
    assert store.saved().api_key == ""

    response = client.put("/api/settings/model", json=_body(api_key=masked))
    assert response.status_code == 400
    assert response.json()["code"] == "settings_api_key_masked"
    # 环境变量那把仍然是生效值，来源还是 env。
    assert store.resolve().api_key == SECRET
    assert store.resolve().api_key_source == store.SOURCE_ENV


def test_clear_api_key_requires_the_explicit_flag():
    """**语义 1 的另一半**：要清密钥必须显式 `clear_api_key: true`。"""
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    body = client.put("/api/settings/model", json=_body(clear_api_key=True)).json()
    assert body["api_key_set"] is False
    assert body["api_key_masked"] == ""
    assert store.saved().api_key == ""


def test_clear_and_new_key_together_is_rejected():
    """矛盾请求宁可 400：静默让其中一个赢，下一次就会有人在这里丢掉一把刚填的密钥。"""
    response = client.put("/api/settings/model", json=_body(
        api_key=SECRET, clear_api_key=True))
    assert response.status_code == 400
    assert response.json()["code"] == "settings_clear_conflict"


def test_empty_base_url_clears_the_override_and_falls_back_to_env(monkeypatch):
    """**语义 2**：`base_url` 的空串 = 清掉库里的覆盖、回落到环境变量。

    「回到环境变量那套配置」是真实需求（换部署、回滚、临时排查），与"不改"是
    两件事。
    """
    monkeypatch.setenv(store.BASE_URL_ENV, "https://env.example/v1")
    client.put("/api/settings/model", json=_body(base_url="https://panel.example/v1"))
    assert store.resolve().base_url == "https://panel.example/v1"
    assert store.resolve().base_url_source == store.SOURCE_SAVED

    body = client.put("/api/settings/model", json=_body(base_url="")).json()
    assert body["base_url"] == "https://env.example/v1"
    assert body["base_url_source"] == store.SOURCE_ENV


def test_invalid_base_url_is_rejected_with_a_readable_code():
    """写错一个协议头，现在就要说清楚——不要等到生成时报"模型服务不可用"。"""
    response = client.put("/api/settings/model", json=_body(base_url="api.deepseek.com/v1"))
    assert response.status_code == 400
    assert response.json()["code"] == "settings_base_url_invalid"
    assert "https://" in response.json()["message"]


def test_unknown_field_is_rejected():
    """`extra="forbid"`：拼错字段名时宁可 422，也不要静默忽略。

    "我把密钥填进 `apikey` 了，保存成功但没生效"是最难自查的一类失败。
    """
    response = client.put("/api/settings/model", json=_body(apikey=SECRET))
    assert response.status_code == 422


# ---------------------------------------------------------------- 回落顺序

def test_env_is_the_fallback_when_nothing_is_saved(monkeypatch):
    """库里没有 → 环境变量。来源标成 `env`，界面据此显示「来自环境变量」。"""
    monkeypatch.setenv(store.BASE_URL_ENV, "https://env.example/v1")
    monkeypatch.setenv(store.API_KEY_ENV, "env-key-0000")
    monkeypatch.setenv(store.MODEL_ENV, "env-model")
    body = client.get("/api/settings/model").json()
    assert (body["base_url"], body["model"]) == ("https://env.example/v1", "env-model")
    assert body["base_url_source"] == store.SOURCE_ENV
    assert body["model_source"] == store.SOURCE_ENV
    assert body["api_key_masked"] == "…0000"
    assert body["configured"] is True


def test_saved_value_wins_over_env(monkeypatch):
    """库 > 环境变量。反过来会让"面板里改完立刻生效"变成一个谎。"""
    monkeypatch.setenv(store.BASE_URL_ENV, "https://env.example/v1")
    monkeypatch.setenv(store.API_KEY_ENV, "env-key-0000")
    monkeypatch.setenv(store.MODEL_ENV, "env-model")
    client.put("/api/settings/model", json=_body(
        base_url="https://panel.example/v1", model="panel-model"))
    resolved = store.resolve()
    assert (resolved.base_url, resolved.base_url_source) == (
        "https://panel.example/v1", store.SOURCE_SAVED)
    assert (resolved.model, resolved.model_source) == ("panel-model", store.SOURCE_SAVED)
    # 逐字段回落 **有例外**：环境变量那把密钥是连同 `https://env.example/v1` 一起
    # 给的，等于"只允许发给 env.example"。现在生效地址已经换成 panel.example，
    # 那把密钥不能跟着过去——它从未属于这台主机。判据在 `store._binding_of`。
    assert (resolved.api_key, resolved.api_key_source) == ("", store.SOURCE_UNBOUND)


def test_a_saved_key_travels_only_with_the_address_it_was_saved_for(monkeypatch):
    """两个地址都是环境变量给的：密钥锚在 env 那个地址上，不会被带到第三个地址。

    这条补的是 `test_saved_value_wins_over_env` 的另一半——那里只改了端点、没给
    密钥；这里**先存一把密钥**（此时绑定到 A），再从面板把地址改成 B，密钥就不能
    跟着走，`api_key_source` 会说 `unbound` 而不是 `saved`。
    """
    monkeypatch.setenv(store.BASE_URL_ENV, "https://env.example/v1")
    client.put("/api/settings/model",
               json=_body(base_url="https://a.example/v1", api_key=SECRET))
    resolved = store.resolve()
    assert (resolved.api_key, resolved.api_key_source) == (SECRET, store.SOURCE_SAVED)

    client.put("/api/settings/model", json=_body(base_url="https://b.example/v1"))
    resolved = store.resolve()
    assert (resolved.base_url, resolved.base_url_source) == (
        "https://b.example/v1", store.SOURCE_SAVED)
    assert (resolved.api_key, resolved.api_key_source) == ("", store.SOURCE_UNBOUND)


def test_partial_save_keeps_the_other_fields(monkeypatch):
    """只填密钥是最常见的用法（换 key 比换供应商频繁得多），不能逼用户重填其余两项。"""
    monkeypatch.setenv(store.BASE_URL_ENV, "https://env.example/v1")
    monkeypatch.setenv(store.MODEL_ENV, "env-model")
    client.put("/api/settings/model", json=_body(api_key=SECRET))
    resolved = store.resolve()
    assert resolved.api_key == SECRET
    assert resolved.base_url == "https://env.example/v1"
    assert resolved.model == "env-model"
    assert resolved.api_key_source == store.SOURCE_SAVED
    assert resolved.base_url_source == store.SOURCE_ENV


def test_config_error_mentions_both_env_and_the_panel(monkeypatch):
    """缺配置时的话要同时指向两条出路：部署方改环境变量，使用者点面板。

    旧的文案只有"环境变量"——页面里出现了这个入口之后，那句话就不再是唯一的
    下一步了。`test_agent_loop` 里那条断言钉着"环境变量"这个词不能丢。
    """
    with pytest.raises(ModelNotConfigured) as caught:
        generation_service._config()
    message = str(caught.value)
    assert "环境变量" in message
    assert "模型设置" in message


# ---------------------------------------------------------------- 深度思考那一档

def test_explicit_deep_model_from_the_panel_reaches_model_for():
    """面板里填的深度模型名要对**所有**出口生效，不只是外层工具循环。

    `_model_for` 的三档在 `generation.service` 里，第 1 档（显式配置）走
    `store.deep_override()`——与其余三个字段同源。
    """
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", model="deepseek-chat",
        deep_model="my-reasoner"))
    assert generation_service._model_for(
        "https://api.deepseek.com/v1", "deepseek-chat", True) == "my-reasoner"
    # 不勾深度思考时仍然是原模型名——老路径不能漂移。
    assert generation_service._model_for(
        "https://api.deepseek.com/v1", "deepseek-chat", False) == "deepseek-chat"


def test_panel_deep_model_beats_the_env_var(monkeypatch):
    """库里的深度模型名高于 `MINDMAP_LLM_MODEL_DEEP`：用户刚在页面上改完。"""
    monkeypatch.setenv(store.DEEP_MODEL_ENV, "env-reasoner")
    client.put("/api/settings/model", json=_body(deep_model="panel-reasoner"))
    assert generation_service._model_for("https://api.deepseek.com", "deepseek-chat", True) == "panel-reasoner"


def test_clearing_deep_model_falls_back_to_the_heuristic(monkeypatch):
    """清掉显式值 → 回落到启发式（deepseek 端点 → `deepseek-reasoner`）。"""
    monkeypatch.delenv(store.DEEP_MODEL_ENV, raising=False)
    client.put("/api/settings/model", json=_body(deep_model="my-reasoner"))
    client.put("/api/settings/model", json=_body(deep_model=""))
    assert generation_service._model_for(
        "https://api.deepseek.com", "deepseek-chat", True) == DEEP_MODEL_FALLBACK


def test_deep_model_effective_is_reported_even_when_not_configured():
    """面板要显示"勾了深度思考实际会发哪个名字"，否则用户以为它没生效。"""
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    body = client.get("/api/settings/model").json()
    assert body["deep_model"] == ""
    assert body["deep_model_source"] == store.SOURCE_UNSET
    assert body["deep_model_effective"] == DEEP_MODEL_FALLBACK


# ---------------------------------------------------------------- 视觉提取也走同一处

def test_vision_uses_the_panel_values(monkeypatch):
    """视觉提取原先自己读三个环境变量，于是面板改完只有生成那条路换了。

    这里钉的是"两条路同源"：库里有值、环境变量是别的值时，视觉拿到库里的。
    """
    monkeypatch.setenv(store.LLM_MODE_ENV, "model")
    monkeypatch.setenv(store.BASE_URL_ENV, "https://env.example/v1")
    monkeypatch.setenv(store.API_KEY_ENV, "env-key-0000")
    monkeypatch.setenv(store.MODEL_ENV, "env-model")
    client.put("/api/settings/model", json=_body(
        base_url="https://panel.example/v1", api_key=SECRET, model="panel-model"))
    assert vision_pdf._config() == ("https://panel.example/v1", SECRET, "panel-model")


def test_vision_still_refuses_in_demo_mode(monkeypatch):
    """`MINDMAP_LLM_MODE` 仍是部署方的开关，面板改不动它——演示模式不许联网。"""
    monkeypatch.setenv(store.LLM_MODE_ENV, "demo")
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    with pytest.raises(vision_pdf.VisionUnavailable) as caught:
        vision_pdf._config()
    assert "demo" in str(caught.value)


def test_llm_mode_is_reported_read_only(monkeypatch):
    """面板要把 `demo` 显示出来：配好了却不生效，得有据可查。"""
    monkeypatch.setenv(store.LLM_MODE_ENV, "MODEL")
    assert client.get("/api/settings/model").json()["llm_mode"] == "model"


# ---------------------------------------------------------------- 测试连接

class _FakeResponse:
    def __init__(self, status_code: int, payload: object = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    """假 httpx 客户端：记录请求，返回预设响应（或抛预设异常）。"""

    def __init__(self, *, response=None, exc=None, sent: list | None = None) -> None:
        self.response = response
        self.exc = exc
        self.sent = sent if sent is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, *, json=None, headers=None):
        self.sent.append({"url": url, "json": json, "headers": headers})
        if self.exc is not None:
            raise self.exc
        return self.response


def _use_fake(monkeypatch, **kwargs) -> list:
    """把 `probe._client` 换成假客户端，返回它记录的请求列表。"""
    sent: list = []
    fake = _FakeClient(sent=sent, **kwargs)
    monkeypatch.setattr(probe_module, "_client", lambda: fake)
    return sent


def test_probe_success_sends_one_tiny_request(monkeypatch):
    """成功路径：请求体是 `max_tokens=1` 的一次 ping，地址拼在 `/chat/completions`。"""
    sent = _use_fake(monkeypatch, response=_FakeResponse(200, {
        "model": "deepseek-chat",
        "choices": [{"message": {"role": "assistant", "content": "p"}}],
    }))
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    body = client.post("/api/settings/model/test").json()

    assert body["ok"] is True
    assert body["model"] == "deepseek-chat"
    assert "连接正常" in body["message"]
    assert sent[0]["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert sent[0]["json"]["max_tokens"] == 1
    assert sent[0]["json"]["model"] == "deepseek-chat"
    assert sent[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
    # 打码值只有末 4 位 + 一次 ping：一次测试连接的花费是"一个 token"。
    assert sent[0]["json"]["messages"] == [{"role": "user", "content": "ping"}]


def test_probe_uses_the_unsaved_form_values(monkeypatch):
    """面板上的流程是"填 → 测 → 存"：请求体里的临时值优先于已生效值。

    不这样的话，"我改了地址但还没保存，想先验一下"就走不通。
    """
    sent = _use_fake(monkeypatch, response=_FakeResponse(200, {"choices": [{"message": {}}]}))
    client.post("/api/settings/model/test", json=_body(
        base_url="https://try.example/v1", api_key="try-key-9999", model="try-model"))
    assert sent[0]["url"] == "https://try.example/v1/chat/completions"
    assert sent[0]["json"]["model"] == "try-model"
    assert sent[0]["headers"]["Authorization"] == "Bearer try-key-9999"


def test_probe_empty_overrides_fall_back_to_effective_values(monkeypatch):
    """空字段 = 用当前生效值："我只改模型名"不必再手打一遍 key。"""
    sent = _use_fake(monkeypatch, response=_FakeResponse(200, {"choices": [{"message": {}}]}))
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    client.post("/api/settings/model/test", json=_body(model="other-model"))
    assert sent[0]["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert sent[0]["json"]["model"] == "other-model"
    assert sent[0]["headers"]["Authorization"] == f"Bearer {SECRET}"


def test_probe_with_the_masked_key_is_refused_before_the_request(monkeypatch):
    """打码值不许拿去发真请求：上游会 401，界面就会把"你手里那把是好的"说成
    "密钥被拒"，用户于是去换一把本来没问题的密钥。
    """
    sent = _use_fake(monkeypatch, response=_FakeResponse(200, {"choices": [{"message": {}}]}))
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    masked = client.get("/api/settings/model").json()["api_key_masked"]

    response = client.post("/api/settings/model/test", json=_body(api_key=masked))
    assert response.status_code == 400
    assert response.json()["code"] == "settings_api_key_masked"
    assert sent == []


def test_probe_rejects_a_wrong_key_with_a_readable_message(monkeypatch):
    """401 → "密钥被拒绝"，并且正文里**没有**明文 key。

    OpenAI 系列的 401 正文常常带一段 key 原文；不替换的话，用户一截图就漏了。
    """
    _use_fake(monkeypatch, response=_FakeResponse(
        401, {"error": {"message": f"Incorrect API key provided: {SECRET}"}}))
    body = client.post("/api/settings/model/test", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat")).json()
    assert body["ok"] is False
    assert body["status"] == 401
    assert "密钥被拒绝" in body["message"]
    assert SECRET not in body["message"]


def test_probe_reports_a_wrong_model_name(monkeypatch):
    """404 同时可能是地址少 `/v1` 或模型名不对，文案得把两种都写上。"""
    _use_fake(monkeypatch, response=_FakeResponse(404, {"error": {"message": "model not found"}}))
    body = client.post("/api/settings/model/test", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="nope")).json()
    assert body["ok"] is False
    assert body["status"] == 404
    assert "/v1" in body["message"]
    assert "model not found" in body["message"]


def test_probe_treats_a_200_without_json_as_failure(monkeypatch):
    """有的兼容端点在路径不对时回 200 + 一页 HTML。那不是"连上了"。"""
    _use_fake(monkeypatch, response=_FakeResponse(200, None, text="<html>nope</html>"))
    body = client.post("/api/settings/model/test", json=_body(
        base_url="https://not-openai.example/v1", api_key=SECRET, model="m")).json()
    assert body["ok"] is False
    assert "OpenAI 兼容" in body["message"]


def test_probe_treats_a_200_without_choices_as_failure(monkeypatch):
    """模型名不被端点认识时，有的服务回 200 + 空 choices——生成侧会变成
    "返回内容里没有可解析的 JSON"，用户毫无线索。

    文案要同时给两条线索：地址可能不是 OpenAI 兼容格式（面板允许自由填地址，
    这是不做 provider 下拉的代价），或者模型名不被认识。只写后者会把"地址填错"
    引到改模型名上去。
    """
    _use_fake(monkeypatch, response=_FakeResponse(200, {"choices": []}))
    body = client.post("/api/settings/model/test", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="nope")).json()
    assert body["ok"] is False
    assert "OpenAI 兼容" in body["message"]
    assert "choices" in body["message"]


def test_probe_says_so_when_the_endpoint_is_not_openai_compatible(monkeypatch):
    """**非 OpenAI 兼容端点**的典型形状：`{"contents": [...]}`（Gemini 原生）。

    面板允许自由填地址（用户要的是这个），所以"填了一家原生协议"是必然会发生
    的输入，而不是异常输入。这条钉住那句话把用户指向地址那一行——生成阶段的
    "不是合法的 Chat Completions 响应"指不到地址。
    """
    _use_fake(monkeypatch, response=_FakeResponse(200, {
        "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
    }))
    body = client.post("/api/settings/model/test", json=_body(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key=SECRET, model="gemini-2.0-flash")).json()
    assert body["ok"] is False
    assert body["status"] == 200
    assert "OpenAI 兼容" in body["message"]
    # 别把密钥漏进"服务端原话"里：这条响应没有 error 段，但断言成本很低。
    assert SECRET not in body["message"]


def test_probe_reports_a_timeout_as_a_sentence(monkeypatch):
    """超时不能冒成 500：那是"我填错了一个字符"被读成"服务器坏了"。"""
    _use_fake(monkeypatch, exc=httpx.ConnectTimeout("timed out"))
    body = client.post("/api/settings/model/test", json=_body(
        base_url="https://slow.example/v1", api_key=SECRET, model="m")).json()
    assert body["ok"] is False
    assert "超时" in body["message"]


def test_probe_reports_a_connection_error_as_a_sentence(monkeypatch):
    _use_fake(monkeypatch, exc=httpx.ConnectError("name resolution failed"))
    body = client.post("/api/settings/model/test", json=_body(
        base_url="https://nowhere.invalid/v1", api_key=SECRET, model="m")).json()
    assert body["ok"] is False
    assert "连不上" in body["message"]


def test_probe_with_incomplete_config_is_a_400():
    """三项不齐时连"测"都测不了——这是真的业务错误，走 400。"""
    response = client.post("/api/settings/model/test", json=_body())
    assert response.status_code == 400
    assert response.json()["code"] == "settings_incomplete"


# ---------------------------------------------------------------- 密钥不出门

def test_key_never_appears_in_any_response_body(monkeypatch):
    """把三个动词全走一遍，任何一次响应正文里都不得出现明文密钥。"""
    _use_fake(monkeypatch, response=_FakeResponse(500, {"error": {"message": "boom"}}))
    payload = _body(base_url="https://api.deepseek.com/v1", api_key=SECRET,
                    model="deepseek-chat")
    texts = [
        client.get("/api/settings/model").text,
        client.put("/api/settings/model", json=payload).text,
        client.post("/api/settings/model/test").text,
    ]
    for text in texts:
        assert SECRET not in text


def test_key_never_appears_in_the_log(monkeypatch, caplog):
    """日志里也不能有：容器日志/`journalctl` 经常被贴到工单里。

    覆盖两条会打日志的路径——面板读写与一次失败的测试连接。
    """
    _use_fake(monkeypatch, response=_FakeResponse(
        401, {"error": {"message": f"Incorrect API key provided: {SECRET}"}}))
    caplog.set_level(logging.DEBUG)
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    client.get("/api/settings/model")
    client.post("/api/settings/model/test")
    for record in caplog.records:
        assert SECRET not in record.getMessage()
    assert SECRET not in caplog.text


def test_a_broken_db_degrades_to_env_instead_of_failing_generation(
        monkeypatch, tmp_path, caplog):
    """库读不出来时回落环境变量，并留一条 warning。

    这条路径在**每一次模型调用**的开头。让它因为一个读不到的配置表把整次生成
    打成失败，比"回落到环境变量"糟糕得多——后者在绝大多数部署上恰好是对的。
    "降级要留下降过级的痕迹"是 AGENTS.md 里那条约定。
    """
    monkeypatch.setenv(store.BASE_URL_ENV, "https://env.example/v1")
    monkeypatch.setenv(store.API_KEY_ENV, "env-key-0000")
    monkeypatch.setenv(store.MODEL_ENV, "env-model")
    monkeypatch.setenv("MINDMAP_DB_PATH", str(tmp_path / "broken" / "nope.db"))
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "nope.db").write_text("not a database", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        base, key, model = generation_service._config()
    assert (base, key, model) == ("https://env.example/v1", "env-key-0000", "env-model")
    assert any("回落" in record.getMessage() for record in caplog.records)


def test_saving_while_the_db_cannot_be_read_does_not_wipe_the_row(monkeypatch):
    """读不出来时**拒写**，不能把降级返回的空值当成真值写回去。

    `saved()` 的降级是给生成路径准备的（读不到就回落环境变量，那在绝大多数部署
    上恰好是对的）。`save()` 如果复用同一个返回值做差量与合并，就会把空串当成
    "用户现在存的就是空"，于是只改一个模型名，`base_url` 与密钥一起被写没。

    实测过的触发方式：写锁被占时 `repository.connect(timeout=10)` 忙等到超时，
    `saved()` 返回空 `Ready()` 并只打一条 warning，而那次写照样成功。
    """
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    original = store._read_strict

    def unreadable(owner, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_read_strict", unreadable)

    response = client.put("/api/settings/model", json=_body(model="deepseek-reasoner"))
    assert response.status_code == 503
    assert response.json()["code"] == "settings_db_unavailable"
    # 响应里不能带密钥，也不能说"保存成功"。
    assert SECRET not in response.text

    monkeypatch.setattr(store, "_read_strict", original)
    store._CACHE.clear()
    ready = store.saved()
    assert ready.base_url == "https://api.deepseek.com/v1"
    assert ready.api_key == SECRET
    assert ready.model == "deepseek-chat"


def test_full_endpoint_pasted_from_the_docs_is_stripped(monkeypatch):
    """用户常把厂商文档里那条完整 URL 粘进来，入库前要剥成前缀。

    不剥的话请求会打到 `.../chat/completions/chat/completions`，404 在界面上
    只能读成"模型名不对"——把人引去改一个没问题的地方。
    """
    body = client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1/chat/completions")).json()
    assert body["base_url"] == "https://api.deepseek.com/v1"
    assert store.saved().base_url == "https://api.deepseek.com/v1"

    # 只填端点、没有主机名：这是"没填对"，不是"清掉覆盖"。
    bad = client.put("/api/settings/model", json=_body(base_url="/chat/completions"))
    assert bad.status_code == 400
    assert bad.json()["code"] == "settings_base_url_invalid"


def test_saving_while_the_write_lock_is_held_is_a_readable_503(monkeypatch):
    """写不进去时给业务 code，不是裸 `OperationalError` → 500 + traceback。

    面板对 `error` 字段有渲染，对 500 只能显示一句通用错误；而这条路上最可能
    发生的事恰恰是"过几秒就好了"。
    """
    original = store._connect

    def locked(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_connect", locked)
    response = client.put("/api/settings/model", json=_body(model="deepseek-reasoner"))
    assert response.status_code == 503
    assert response.json()["code"] == "settings_db_unavailable"
    monkeypatch.setattr(store, "_connect", original)


def test_clear_resets_the_row():
    """`store.clear()` 只在用例与本地调试用：删掉行之后一切回到环境变量。"""
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat"))
    assert store.clear() == 1
    assert store.resolve().base_url == ""


# ---------------------------------------------------------------- 建表

def test_table_is_created_by_the_repository_connect():
    """表由 `repository.connect()` 建：读它的路径在热路径上。

    多一个"发现表不存在 → 建表"的分支，只会在最不该出错的地方多一条可能出错的路径。
    """
    with closing(repository.connect()) as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert store.TABLE in names


def test_an_old_database_without_the_table_gets_it(monkeypatch, tmp_path):
    """**旧库**（线上那份 `data/mindmaps.db`）里没有 `model_settings` 表。

    这一步是真的会发生的：上线时 `MINDMAP_DB_PATH` 指向一个已经跑了几个月、里面有
    用户导图的 SQLite 文件。`connect()` 必须把它补上，而不是因为缺表把整次生成打成
    失败。这里造一个只含 `mindmaps` 表的旧库，走一遍 `connect()` → 写 → 读。
    """
    import sqlite3

    path = tmp_path / "old.db"
    legacy = sqlite3.connect(str(path))
    try:
        # 旧库的最小形状：一张 users 会用的表 + 一个 `mindmaps` 表（不与新表重名）。
        legacy.execute("CREATE TABLE mindmaps (id TEXT PRIMARY KEY, payload TEXT)")
        legacy.execute("INSERT INTO mindmaps (id, payload) VALUES ('m1', '{}')")
        legacy.commit()
    finally:
        legacy.close()

    monkeypatch.setenv("MINDMAP_DB_PATH", str(path))
    store._CACHE.clear()

    # connect() 之后表就存在了，且旧数据还在（补表不该动别的表）。
    with closing(repository.connect()) as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert store.TABLE in names
        assert conn.execute("SELECT COUNT(*) FROM mindmaps").fetchone()[0] == 1

    # 补出来的表可以正常读写：这是"迁移成功"的真正判据。
    saved, changed = store.save(base_url="https://api.deepseek.com/v1",
                                api_key=SECRET, model="deepseek-chat")
    assert changed is True
    assert saved.base_url == "https://api.deepseek.com/v1"
    assert store.saved().model == "deepseek-chat"
    # 再连一次仍然是同一行，不是每次 connect 插一行。
    with closing(repository.connect()) as conn:
        rows = conn.execute(f"SELECT owner FROM {store.TABLE}").fetchall()
    assert [row[0] for row in rows] == [store.INSTANCE_OWNER]


def test_reading_does_not_rebuild_the_table_on_every_call(monkeypatch):
    """一次生成里 `_config()` 会被调 5–27 次（实测，见 store 里那段注释）。

    每次读都走一遍 `repository.connect()`（它开头有十来个 `CREATE TABLE IF NOT
    EXISTS`）是白付的开销。这条用例钉两件事：**同一配置连读多次只真的查一次库**，
    以及**写完立刻能读到新值**（缓存不能把"保存即生效"变成"等一秒"）。
    """
    connects: list[int] = []
    original = store._connect

    def counting_connect(**kwargs):
        connects.append(1)
        return original(**kwargs)

    monkeypatch.setattr(store, "_connect", counting_connect)

    store.save(base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat")
    connects.clear()

    for _ in range(20):
        assert store.resolve().model == "deepseek-chat"
    # 上限是 1：`save()` 结尾已经读过一次，正常情形是 0 次（全命中缓存）。
    # 断言成 `== 1` 会把"缓存更有效"判成失败。
    assert len(connects) <= 1, f"20 次读打了 {len(connects)} 次库，缓存没生效"

    # 写入必须立刻让下一次读到新值，不能等 TTL。
    store.save(model="deepseek-reasoner")
    assert store.resolve().model == "deepseek-reasoner"


def test_the_ttl_is_short_enough_to_not_hide_a_change_from_another_worker(monkeypatch):
    """另一个进程改了库（本进程收不到 `_invalidate`）：缓存最多挡 TTL 那么久。

    这条不是在测性能，是在钉住"多 worker 时用户看到旧值的最长时间"。TTL 一旦被
    改成几十秒，单机单 worker 下没问题，加了 worker 之后就会出现"保存了但页面还是
    旧值，刷新两次才好"的那种报告。
    """
    assert store._CACHE_TTL_SECONDS <= 1.0

    store.save(base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat")
    assert store.resolve().model == "deepseek-chat"

    # 模拟"另一个进程"：直接改库，绕过本进程的 save()（因此不会 _invalidate）。
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            f"UPDATE {store.TABLE} SET model = 'other-worker-model' WHERE owner = ?",
            (store.INSTANCE_OWNER,))

    # 缓存还在：这一步读到的仍是旧值——这正是 TTL 兜的那个窗口。
    assert store.resolve().model == "deepseek-chat"

    # 把时钟推过 TTL（不真 sleep，避免用例变慢也避免偶发）。
    key = store._cache_key(store.INSTANCE_OWNER)
    stamp, ready = store._CACHE[key]
    store._CACHE[key] = (stamp - store._CACHE_TTL_SECONDS - 0.01, ready)
    assert store.resolve().model == "other-worker-model"


def test_one_row_per_owner_and_no_more():
    """同一个 owner 不可能有两行——那是"哪一行生效"要靠 `ORDER BY` 猜的状态。

    改成 `owner TEXT PRIMARY KEY` 之后这条约束由 DDL 承担；不同 owner 各有各的
    一行则是这一版的正常形态（那是多用户，不是重复）。
    """
    import sqlite3

    with closing(repository.connect()) as conn, conn:
        conn.execute(
            f"INSERT INTO {store.TABLE} (owner, base_url) VALUES (?, 'https://a/v1')",
            (store.INSTANCE_OWNER,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {store.TABLE} (owner, base_url) VALUES (?, 'https://b/v1')",
                (store.INSTANCE_OWNER,))
        # 换个 owner 就该成功：多用户不是"重复行"。
        conn.execute(
            f"INSERT INTO {store.TABLE} (owner, base_url) VALUES ('user:1', 'https://c/v1')")


def test_saved_row_is_readable_as_json_shape():
    """整行读出来就是响应要的形状（字段名对得上，不是一个内嵌对象）。"""
    client.put("/api/settings/model", json=_body(
        base_url="https://api.deepseek.com/v1", api_key=SECRET, model="deepseek-chat",
        deep_model="deepseek-reasoner"))
    body = client.get("/api/settings/model").json()
    assert set(body) == {
        "base_url", "base_url_source", "api_key_masked", "api_key_source", "api_key_set",
        "model", "model_source", "deep_model", "deep_model_source",
        "deep_model_effective", "llm_mode", "configured", "updated_at",
        "can_edit_instance", "instance",
    }
    # 打码值能被 JSON 序列化（省略号是普通字符，不是控制字符）。
    json.dumps(body, ensure_ascii=False)


# ---------------------------------------------------------------- 地址不许乱填
#
# P0-1：**无鉴权 + 任意 base_url = 三步把已存密钥明文送到任意地址**。
#   PUT  {"base_url": "https://attacker.example"}   → 200
#   POST /model/test  {}                            → 空 body 走"回落生效值"，密钥出门
# 攻击者既不需要知道密钥，也不需要读得到响应。这一组钉住"发请求之前先挡一道"。


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:11434/v1",
        "http://[::1]:11434/v1",
        "http://10.0.0.5/v1",
        "http://172.16.3.4/v1",
        "http://192.168.1.1/v1",
        "http://0.0.0.0:8000/v1",
        "http://model.local/v1",
    ],
)
def test_private_and_loopback_addresses_are_refused(url):
    """本机 / 内网 / 链路本地 / `0.0.0.0` 全拒，但**开关能放开**。

    `0.0.0.0` 单列一条：它在多数系统上连的是本机，而 `ip.is_private` **不覆盖**
    它——只写 `is_private` 的实现会把它放过去。

    `169.254.169.254`（云元数据）**不在这张表里**：它与内网地址分开处理，开关也
    打不开，见下一条。
    """
    reason = probe_module._refuse(url)
    assert reason, f"{url} 被放行了，密钥可以被送到这里"
    # 拒绝理由要能指向"怎么才允许"：只说"不许"会让人以为这个地址永远填不了。
    assert probe_module.ALLOW_PRIVATE_ENV in reason


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "https://169.254.169.254/latest/meta-data",
        "http://metadata.google.internal/v1",
        "http://100.100.100.200/latest/meta-data",
    ],
)
def test_metadata_endpoints_are_refused_even_with_the_switch_on(url, monkeypatch):
    """云元数据地址**永远**拒，开关也打不开。

    与内网地址分开的理由：内网模型是真实需求（Ollama、自建网关），所以给它留了
    开关；元数据服务上没有任何模型，只有临时凭证。用户没有任何理由需要连它，
    所以"允许连我自己的模型"不该顺手允许"连任何东西"。

    断言里**不**出现开关名，是与上一条的区别——这条路的正确答案不是"打开开关"。
    """
    monkeypatch.setenv(probe_module.ALLOW_PRIVATE_ENV, "1")
    reason = probe_module._refuse(url)
    assert reason, f"{url} 被放行了，那台机器上有的是凭证不是模型"
    assert probe_module.ALLOW_PRIVATE_ENV not in reason


@pytest.mark.parametrize(
    "url",
    [
        "https://api.deepseek.com/v1",
        "https://api.openai.com/v1",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://1.2.3.4/v1",  # 公网 IP 字面量：允许（有人的自建端点在公网 IP 上）
    ],
)
def test_public_addresses_are_allowed(url):
    """公网地址一律放行——这个校验不是白名单，别把它做成"只许几家厂商"。"""
    assert probe_module._refuse(url) is None


@pytest.mark.parametrize(
    "url",
    ["http://api.example.com/v1", "http://1.2.3.4/v1"],
)
def test_public_addresses_must_use_https(url):
    """公网 `http://` 拒：密钥写在 `Authorization` 头里，明文过中间每一跳。

    私网不查这一条（规则 ② 已经提前返回）：本机 Ollama 常开在
    `http://127.0.0.1:11434`，要求它上 https 等于让人没法用。
    """
    reason = probe_module._refuse(url)
    assert reason, f"{url} 被放行了，密钥会明文过网"
    assert "https" in reason.lower()


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://example.com/v1", "not-a-url", "", "https://"],
)
def test_addresses_that_are_not_http_are_refused(url):
    """非 http(s) 与解析不出来的地址在发请求之前就拒。

    `file:///etc/passwd` 拼上 `/chat/completions` 之后的行为完全取决于 httpx 版本，
    不该由它决定；空串则应该由更早的 `settings_incomplete` 拦，这里也顺手挡住。
    """
    assert probe_module._refuse(url)


def test_the_switch_reopens_private_addresses(monkeypatch):
    """自建端点是合法需求：开关一开就全放行。

    这也顺带钉住"开关是**每次现读**的"——做成模块级常量时 `monkeypatch.setenv`
    不会生效，这条会红。
    """
    monkeypatch.setenv(probe_module.ALLOW_PRIVATE_ENV, "1")
    assert probe_module._refuse("http://127.0.0.1:11434/v1") is None
    assert probe_module._refuse("http://10.0.0.5/v1") is None
    monkeypatch.setenv(probe_module.ALLOW_PRIVATE_ENV, "0")
    assert probe_module._refuse("http://127.0.0.1:11434/v1")


def test_probe_never_opens_a_connection_to_a_private_address(monkeypatch):
    """最重要的一条：**假客户端一次都没被调用**。

    只断言"返回 ok=false"是不够的——真正的伤害发生在请求发出去的那一刻，
    而 `ProbeResult` 在超时、连不上、密钥被拒时也全是 `ok=false`。
    """
    sent = _use_fake(monkeypatch, response=_FakeResponse(200, {
        "model": "m", "choices": [{"message": {"content": "p"}}],
    }))
    body = client.post("/api/settings/model/test", json=_body(
        base_url="http://169.254.169.254", api_key=SECRET, model="m"))
    assert body.status_code == 400
    assert body.json()["code"] == "settings_base_url_refused"
    assert sent == [], "请求发出去了，密钥已经出门"


def test_probe_itself_refuses_without_opening_a_connection(monkeypatch):
    """接口层是 400，`probe()` 自己是 `ok=false` 的结论——两条路都不发请求。

    第二层兜底是给"将来有后台任务直接调 `probe()`"留的：那时没有 router 那一道，
    只有这里。`latency_ms=0` 与 `status=None` 是"一个字节都没发"的可见证据。
    """
    sent = _use_fake(monkeypatch, response=_FakeResponse(200, {"choices": []}))
    result = asyncio.run(
        probe_module.probe("http://127.0.0.1:11434/v1", SECRET, "m"))
    assert result.ok is False
    assert result.status is None
    assert result.latency_ms == 0
    assert SECRET not in result.message
    assert sent == [], "请求发出去了，密钥已经出门"


# ---------------------------------------------------------------- 密钥只认那台主机
#
# P0-1 的第二层：地址与密钥**绑定**。第一层（`probe._refuse`）挡住"填个地址就把
# 已存密钥送出去"这条最便宜的路；这一层挡的是"地址已经改好了、密钥还留在库里"
# 这种更安静的形状——它不经过测试连接，是每一次正常生成都会走的路。


def test_the_panel_says_unbound_instead_of_unset(monkeypatch):
    """界面拿到的 `api_key_source` 必须是 `unbound`，不能是 `unset`。

    两者的话术完全不同：`unset` = "去配一把密钥"，`unbound` = "你换过地址，把原来
    那把再填一次"。混成一句会让用户去供应商后台重开密钥——而库里那把一直是好的。
    """
    client.put("/api/settings/model",
               json=_body(base_url="https://a.example/v1", api_key=SECRET, model="m"))
    client.put("/api/settings/model", json=_body(base_url="https://b.example/v1"))
    body = client.get("/api/settings/model").json()
    assert body["api_key_source"] == store.SOURCE_UNBOUND
    assert body["api_key_masked"] == ""
    assert body["api_key_set"] is False
    # 还有一个更硬的后果：`configured` 必须跟着变 False。否则界面显示"配好了"
    # 而生成一路 401，坏在"看起来没问题"上。
    assert body["configured"] is False


def test_retyping_the_key_binds_it_to_the_new_address(monkeypatch):
    """重新贴一次密钥，绑定就跟着新地址走——这是唯一的恢复方式。"""
    client.put("/api/settings/model",
               json=_body(base_url="https://a.example/v1", api_key=SECRET, model="m"))
    client.put("/api/settings/model", json=_body(base_url="https://b.example/v1"))
    client.put("/api/settings/model", json=_body(api_key=SECRET + "x"))
    resolved = store.resolve()
    assert resolved.api_key == SECRET + "x"
    assert resolved.api_key_source == store.SOURCE_SAVED
    # 回头改地址，又该失效：绑的是 b，不是"永远有效"。
    client.put("/api/settings/model", json=_body(base_url="https://c.example/v1"))
    assert store.resolve().api_key_source == store.SOURCE_UNBOUND


def test_changing_only_the_path_keeps_the_key(monkeypatch):
    """同主机换路径不算换对方：`/v1` → `/v2` 是同一台机器，密钥还是同一把。

    绑定只认主机名（`store._host_of`）。把端口/路径也算进去，会为一次无关紧要的
    改动逼人重填密钥，而那恰好训练出"看到提示就再粘一次"的习惯——这条防线最怕
    的输入。
    """
    client.put("/api/settings/model",
               json=_body(base_url="https://api.example.com/v1", api_key=SECRET, model="m"))
    client.put("/api/settings/model", json=_body(base_url="https://api.example.com/v2"))
    resolved = store.resolve()
    assert resolved.api_key == SECRET
    assert resolved.api_key_source == store.SOURCE_SAVED


def test_an_old_row_without_a_host_column_still_works(monkeypatch):
    """升级前写下的行（`api_key_host` 为空）按老地址兜底，不误伤。

    `_binding_of` 用**那一行自己的 base_url** 兜底：同一行的地址与密钥本来就是
    同一次写入的一对。没有这条兜底，升级之后所有已存配置会在第一次读时被判成
    `unbound`，所有人一起"密钥丢了"。
    """
    resolved, _ = store.save(base_url="https://old.example/v1", api_key=SECRET,
                             model="m", deep_model=None, clear_api_key=False)
    with closing(repository.connect()) as conn:
        conn.execute(f"UPDATE {store.TABLE} SET api_key_host = ''")
        conn.commit()
    store._invalidate()
    resolved = store.resolve()
    assert (resolved.api_key, resolved.api_key_source) == (SECRET, store.SOURCE_SAVED)


def test_clearing_the_key_clears_the_anchor(monkeypatch):
    """清密钥时锚点一起清掉。

    留着它，将来新填的密钥会"继承"一个它从未被验证过的地址——那等于给下一次
    "换个地址 + 用旧密钥"留了一条后门。
    """
    client.put("/api/settings/model",
               json=_body(base_url="https://a.example/v1", api_key=SECRET, model="m"))
    client.put("/api/settings/model", json=_body(clear_api_key=True))
    row = store.saved()
    assert row.api_key == ""
    assert row.api_key_host == ""


def test_the_same_key_retyped_rebinds_to_the_current_address(monkeypatch):
    """恢复动作里最正常的那一种：**粘回同一把密钥**，绑定跟着新地址走。

    这条补的是 `test_retyping_the_key_binds_it_to_the_new_address` 漏掉的路——那里
    提交的是 `SECRET + "x"`，值变了。而真实用户换地址时多半没换供应商，粘回来的
    就是同一把：判据若写成"提交的密钥与库里那把不同"，这种恢复会**静默失败**，
    界面说"重新粘贴一次即可"、粘完仍是 `unbound`，用户只能认为功能坏了。
    """
    client.put("/api/settings/model",
               json=_body(base_url="https://a.example/v1", api_key=SECRET, model="m"))
    client.put("/api/settings/model", json=_body(base_url="https://b.example/v1"))
    assert store.resolve().api_key_source == store.SOURCE_UNBOUND

    # 完全相同的值，只是重新提交了一次。
    client.put("/api/settings/model", json=_body(api_key=SECRET))
    resolved = store.resolve()
    assert (resolved.api_key, resolved.api_key_source) == (SECRET, store.SOURCE_SAVED)
    assert store.saved().api_key_host == "b.example"

    # 绑定跟着走了：再换主机仍然失效，不是"一旦重贴就永久放行"。
    client.put("/api/settings/model", json=_body(base_url="https://c.example/v1"))
    assert store.resolve().api_key_source == store.SOURCE_UNBOUND


def test_testing_a_connection_after_the_address_moved_says_what_to_do(monkeypatch):
    """这时点「测试连接」不能回"三项要先填齐"。

    密钥还在库里，只是不属于新地址。要求重贴是正确答案（`settings_key_unbound`），
    而"三项要先填齐"会让人以为密钥丢了。更要紧的是**请求一次都不许发**：拿新地址
    配旧密钥去试，正是这套绑定要禁止的行为。
    """
    sent = _use_fake(monkeypatch, response=_FakeResponse(200, {
        "model": "m", "choices": [{"message": {"content": "p"}}],
    }))
    client.put("/api/settings/model",
               json=_body(base_url="https://a.example/v1", api_key=SECRET, model="m"))
    client.put("/api/settings/model", json=_body(base_url="https://b.example/v1"))
    body = client.post("/api/settings/model/test", json=_body())
    assert body.status_code == 400
    assert body.json()["code"] == "settings_key_unbound"
    assert SECRET not in body.text
    assert sent == [], "旧密钥被发到新地址去了"


def test_probe_never_sends_a_saved_key_to_a_different_host(monkeypatch):
    """P0-1 的第三条入口：**一次请求内**完成的密钥外送。

    `PUT /model/test` 可以只带 `base_url`——密钥留空就走"回落生效值"。绑定判据写在
    `store.resolve()` 的出口，而这里的地址是请求体里的 overrides，不经过那个出口，
    于是"随手填一个域名 + 借已存密钥"绕开了整条绑定。攻击者既不需要知道密钥，也
    不需要读得到响应：请求发出去，密钥就在路径上了。

    判据：已存的密钥只能发往它被绑定的那台主机；主机对不上就是 400，且**一个字节
    都不许发**（`sent == []` 是"发请求之前就挡住"的证据）。
    """
    sent = _use_fake(monkeypatch, response=_FakeResponse(200, {
        "model": "m", "choices": [{"message": {"content": "p"}}],
    }))
    client.put("/api/settings/model",
               json=_body(base_url="https://mine.example/v1", api_key=SECRET, model="m"))

    body = client.post("/api/settings/model/test",
                       json=_body(base_url="https://attacker.example/v1"))
    assert body.status_code == 400
    assert body.json()["code"] == "settings_key_unbound"
    assert SECRET not in body.text
    assert sent == [], "已存的密钥被发到了另一个主机"

    # 同主机换路径照旧放行：那不是新目的地，逼人重贴只会训练出"看到提示就再粘一次"。
    sent.clear()
    body = client.post("/api/settings/model/test",
                       json=_body(base_url="https://mine.example/v1/v2"))
    assert body.status_code == 200
    assert sent[0]["url"] == "https://mine.example/v1/v2/chat/completions"


def test_probe_allows_a_new_host_when_the_key_comes_with_it(monkeypatch):
    """自己填地址 + 自己填密钥是**合法**用法，不能被上面那条判据误伤。

    面板支持"改了地址、还没保存，先点一下测试"。这时密钥也得一起粘进来——那一对
    是用户自己给出的新绑定，与库里的旧锚点无关。
    """
    sent = _use_fake(monkeypatch, response=_FakeResponse(200, {
        "model": "m", "choices": [{"message": {"content": "p"}}],
    }))
    client.put("/api/settings/model",
               json=_body(base_url="https://mine.example/v1", api_key=SECRET, model="m"))

    body = client.post("/api/settings/model/test", json=_body(
        base_url="https://other.example/v1", api_key="sk-other-7777", model="m"))
    assert body.status_code == 200
    assert body.json()["ok"] is True
    assert sent[0]["url"] == "https://other.example/v1/chat/completions"
    assert sent[0]["headers"]["Authorization"] == "Bearer sk-other-7777"

