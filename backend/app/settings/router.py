"""模型设置的 HTTP 面：读当前生效配置、写一份新的、测一次连接。

三个动词就是面板的全部行为，一一对应：`GET` 填表、`PUT` 保存、`POST .../test` 验一次。

**这里没有"从环境变量导入"的入口**。库里的值与环境变量是两个来源，优先级写死在
`store.resolve()` 里；再加一个"把环境变量拷进库"的按钮，只会制造第三态——
"拷过了，之后改环境变量为什么不生效"。用户的动作只有一种：填、存、测。

**响应里没有任何明文密钥**。`api_key_masked` 是唯一会出门的密钥形状（`…abcd`），
连"长度"都不给：长度对撞库没有帮助，对截图里的旁观者倒是有。

**配置按人分开存**（第三片）：每个人（登录账号、游客）在 `model_settings` 里
自己一行，读的时候按"自己那行 → 部署方那行 → 环境变量"回落。部署方那行是全站
共享配置（游客免费额度用的就是它），只有部署方能改——判定在 `_deployer()`。
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from app.auth import identity as identity_module
from app.auth import store as auth_store
from app.generation.service import _model_for
from app.settings import probe as probe_module
from app.settings import store

router = APIRouter(prefix="/settings", tags=["模型设置"])


def _deployer() -> bool:
    """当前请求是不是"部署方"（这台机器上第一个注册的账号）。

    为什么用"第一个注册的人"而不是一个 `MINDMAP_ADMIN` 环境变量：这台机器上第一个
    注册的人只可能是部署它的人（部署完自己先注册一个），而环境变量会多出一种故障
    形态——"忘了设 / 设错了"，那时代码能做的只有拒绝所有人。**也没有"账号密码等于
    管理权"的额外秘密**：注册是开放的，谁先注册谁就是部署方，这件事在部署完成的那
    一刻就已决定。

    匿名与游客一律 `False`：他们能改的是自己那行（BYOK），不是全站共享那行。
    """
    user = identity_module.current_user()
    if user is None:
        return False
    first = auth_store.first_user()
    return first is not None and first.id == user.id


class InstanceSettings(BaseModel):
    """全站共享那一行（`owner = ''`）的样子。

    **只给部署方**（`can_edit_instance` 为真时才出现在响应里）。给别人看没有用：
    他们改不了，那是"一片点不动的表单"；而 `base_url` 是部署方用的供应商，不该
    成为所有人的可见信息。
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str
    #: `…abcd`；没配时是空串。
    api_key_masked: str
    api_key_set: bool
    model: str
    deep_model: str
    #: 三项齐了没有。游客与没填自己密钥的人靠这一份生成。
    configured: bool
    #: 这一行最后一次被保存的时刻；从没存过是 `null`。
    updated_at: str | None = None


class ModelSettings(BaseModel):
    """生效的模型配置。**密钥只有打码值**。"""

    model_config = ConfigDict(extra="forbid")

    base_url: str
    #: `saved`（你自己那行）/ `instance`（借用部署方的共享配置）/ `env` / `unset`。
    base_url_source: str
    #: `…abcd`；没配时是空串。**永远不返回明文**。
    api_key_masked: str
    api_key_source: str
    #: 有没有密钥。界面用它决定"清除密钥"按钮可不可点。
    api_key_set: bool
    model: str
    model_source: str
    #: 显式配置的「深度思考」模型名，空串 = 没配（走 `_model_for` 的启发式）。
    deep_model: str
    deep_model_source: str
    #: 勾了「深度思考」时**实际会发出去**的那个名字。没配显式值时它由
    #: `_model_for` 的三档启发式算出（deepseek 端点 → `deepseek-reasoner`，
    #: 否则沿用原模型名）。面板显示它，用户才不会以为"深度思考没生效"。
    deep_model_effective: str
    #: `MINDMAP_LLM_MODE` 的原值（`demo` / `model` / 空）。面板用它提示"本机是
    #: 演示模式，配好了也不会调模型"。**只读**，面板改不动它。
    llm_mode: str
    #: 三项（地址 / 密钥 / 模型名）齐了没有。判据与 `_config()` 一致。
    configured: bool
    #: 库里那一行最后一次被保存的时刻；没保存过是 `null`。
    updated_at: str | None = None
    #: 当前请求**能不能改全站共享那行**（即是不是部署方）。界面据此决定要不要
    #: 显示"本站共享配置"区块——不显示比显示一个点不动的按钮好。
    can_edit_instance: bool = False
    #: 全站共享那一行的内容。**只有部署方拿得到**（否则是 `null`）。
    instance: InstanceSettings | None = None


def _instance_view() -> InstanceSettings | None:
    """全站共享那一行的视图。非部署方拿 `None`。

    刻意**不复用 `_view()`**：那一份回答的是"我这次生成会用哪份配置"，会一路回落到
    环境变量；而这里要回答的是"共享那一行**存了什么**"，环境变量不是它的一部分
    （部署方清空共享行之后，共享行就是空的，即使环境变量还配着）。
    """
    if not _deployer():
        return None
    row = store.saved(store.INSTANCE_OWNER)
    return InstanceSettings(
        base_url=row.base_url,
        api_key_masked=store.mask_api_key(row.api_key),
        api_key_set=bool(row.api_key),
        model=row.model,
        deep_model=row.deep_model or "",
        configured=(
            row.base_url.startswith(("https://", "http://"))
            and bool(row.api_key) and bool(row.model)
        ),
        updated_at=row.updated_at,
    )


def _view(resolved: store.Resolved, *, updated_at: str | None) -> ModelSettings:
    """`Resolved` → 响应体。**密钥在这里被换成打码值，这是唯一一处出口**。"""
    effective = _model_for(resolved.base_url, resolved.model, True) if resolved.model else ""
    return ModelSettings(
        base_url=resolved.base_url,
        base_url_source=resolved.base_url_source,
        api_key_masked=store.mask_api_key(resolved.api_key),
        api_key_source=resolved.api_key_source,
        api_key_set=bool(resolved.api_key),
        model=resolved.model,
        model_source=resolved.model_source,
        deep_model=resolved.deep_model,
        deep_model_source=resolved.deep_model_source,
        deep_model_effective=effective,
        llm_mode=store.llm_mode(),
        configured=_configured(resolved),
        updated_at=updated_at,
        can_edit_instance=_deployer(),
        instance=_instance_view(),
    )


def _configured(resolved: store.Resolved) -> bool:
    """与 `generation.service._config()` 同一个判据：地址要 http(s) 开头，三项都非空。"""
    return (resolved.base_url.startswith(("https://", "http://"))
            and bool(resolved.api_key) and bool(resolved.model))


@router.get("/model", response_model=ModelSettings)
def read_model_settings() -> ModelSettings:
    """当前生效的配置 + 每一项的来源。

    `changed` 之类的写入信息不在这里——`GET` 只回答"现在是什么"。
    """
    resolved = store.resolve()
    return _view(resolved, updated_at=store.updated_at())


class SaveModelSettingsRequest(BaseModel):
    """`PUT /settings/model` 的请求体。**每个字段的"空"含义不同**（见 `store.save`）。

      - `api_key` 省略或空串 → **不改**（界面上那个框显示打码值，用户不动它就发空串）；
        要清空必须 `clear_api_key: true`。
      - `base_url` / `model` / `deep_model` 省略 → 不改；空串 → 清掉库里的覆盖，
        回落到环境变量。
      - `scope`：`"user"`（缺省）= 写你自己那行；`"instance"` = 写全站共享那行
        （只有部署方可以，别人 403 `settings_forbidden`）。

    `extra="forbid"` 是仓库的默认风格：拼错字段名时宁可 422，也不要静默忽略——
    "我把密钥填进 `apikey` 了，保存成功但没生效"是最难自查的一类失败。
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    deep_model: str | None = None
    #: 显式清除已经保存的密钥。与同时给 `api_key` 冲突（400）。
    clear_api_key: bool = False
    #: 写到哪一行。`user` 缺省，`instance` 仅部署方可用。别的值 422。
    scope: Literal["user", "instance"] = "user"


class SaveModelSettingsResponse(ModelSettings):
    """保存的结果：写入后的生效配置 + 这次有没有真的改变。

    `changed=False` 是"这次提交没有改变任何东西"（同值重复保存不刷 `updated_at`）。
    不返回这个布尔值，界面上就会出现"我刚存过"的假象。

    `notice` 是**改了地址之后要额外说的一句话**：地址一变，之前那次连接成功的结论
    就不再证明这个新地址通得过了。这里不强制先测通再落库（那会让"我想先填好、等
    供应商开白名单"这种正常操作变成不可能），但要让界面有机会提醒一句。
    """

    changed: bool
    #: 需要用户看一眼的提醒。`None` = 没什么要额外说的（绝大多数保存都是这样）。
    notice: str | None = None


@router.put("/model", response_model=SaveModelSettingsResponse)
def save_model_settings(body: SaveModelSettingsRequest) -> SaveModelSettingsResponse:
    """保存。**改完立即生效**：所有出口每次调用都重新 `resolve()`，没有进程内缓存。

    `store.SettingsError`（`AppError` 子类）由 `core/errors.py` 的统一出口转成
    `{code, message, detail, request_id}`——错误码分得开，界面才能说清"是地址不合法
    还是密钥太长"。
    """
    if body.scope == "instance" and not _deployer():
        # 403 而不是 400：请求形状没问题，是"你不能改这一份"。说清是权限而不是
        # 拼错了字段——后者会让用户去检查一个没错的东西。
        raise store.SettingsError(
            "settings_forbidden",
            "全站共享的模型配置只有部署方（这台机器上第一个注册的账号）能改。"
            "你自己的密钥请直接保存，会存在你自己名下。",
            status=403)
    before = store.resolve().base_url
    resolved, changed = store.save(
        base_url=body.base_url,
        api_key=body.api_key,
        model=body.model,
        deep_model=body.deep_model,
        clear_api_key=body.clear_api_key,
        scope=body.scope,
    )
    return SaveModelSettingsResponse(
        **_view(resolved, updated_at=store.updated_at()).model_dump(),
        changed=changed,
        notice=_notice_for(before, resolved.base_url),
    )


class TestModelSettingsRequest(BaseModel):
    """`POST /settings/model/test` 的请求体：可选的临时覆盖。

    空字段 = 用**当前生效**的值。这样面板可以"先测再存"：用户在输入框里改了地址
    或模型名，不必先落库就能验一次。密钥同样：不填就用已保存/环境变量里那把，
    所以"我只改模型名"的场景不需要再手打一遍 key。
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class TestModelSettingsResponse(BaseModel):
    """一次测试连接的结论。`ok=false` 也是 200——它是结论，不是 HTTP 失败。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    message: str
    #: 这次真正拿去测的模型名（可能在深度思考档位之外，就是生效值）。
    model: str
    status: int | None = None
    latency_ms: int = 0


@router.post("/model/test", response_model=TestModelSettingsResponse)
async def test_model_settings(
    body: TestModelSettingsRequest | None = None,
) -> TestModelSettingsResponse:
    """发一次最小 chat 请求，验证地址 / 密钥 / 模型名。

    **为什么是 200 而不是 4xx**：失败原因分成"地址不通""密钥被拒""模型不存在"
    三种，界面要按 `ok` 显示在面板里（一句红字），而不是让前端把 4xx 当成"请求
    没发出去"。真正的业务错误（比如三项都没配）走 400，因为那时连测都没法测。
    """
    overrides = body or TestModelSettingsRequest()
    resolved = store.resolve()
    given_base = (overrides.base_url or "").strip().rstrip("/")
    given_key = (overrides.api_key or "").strip()
    base_url = given_base or resolved.base_url
    api_key = given_key or resolved.api_key
    model = (overrides.model or "").strip() or resolved.model
    if not api_key and resolved.api_key_source == store.SOURCE_UNBOUND:
        # 库里有密钥，但它是给**另一个地址**存下的（`store._binding_of` 那条判定
        # 把生效值清空了）。这里必须与"三项没填齐"分开说：那句话会让人以为密钥
        # 丢了，于是去供应商后台重开一把——而库里那把一直是好的，只是不能发给新
        # 地址。也不该默默用新地址加旧密钥去试：那正是这套绑定要禁止的事。
        raise store.SettingsError(
            "settings_key_unbound",
            "地址换过了，而库里那把密钥是为旧地址保存的。确认新地址可信之后，"
            "重新粘贴一次密钥再测试连接。",
            status=400)
    if not base_url or not api_key or not model:
        _raise_incomplete()
    if not given_key:
        # 密钥是**已存的**（库里或环境里那把），地址却可以来自 `given_base`。
        # 绑定判定只写在 `store.resolve()` 的出口，而这里 `base_url` 是 overrides，
        # 不经过那个出口——于是"地址随手填一个域名 + 密钥借已存的"就成了一条绕开
        # 绑定的路，且**一次请求内就完成外送**，攻击者既不需要知道密钥，也不需要
        # 读得到响应（PR 自查复现过这条）。所以这里补同一把尺子。
        #
        # 比**主机名**而不是直接拒绝"地址非空"：面板的常规用法是"地址填着不动、
        # 点一下测试"，那不传递新目的地，应当放行；只有主机名变了才是新地址。
        #
        # `bound_host` 在锚点为空时用生效地址兜底——"只给了密钥没给地址"那种
        # 部署里，它今天会被发到哪儿，就应该只允许发到哪儿。
        bound = store.bound_host(resolved)
        if not bound or bound != store.host_of(base_url):
            raise store.SettingsError(
                "settings_key_unbound",
                "已保存的密钥只会发往它当初保存时的那台地址。这次要测的地址不是它"
                "（或无法确认它属于哪台），请把密钥一起重新粘贴一次；要测已保存的"
                "配置，把地址那一栏清空。",
                status=400)
    if api_key in (store.mask_api_key(resolved.api_key),
                   store.mask_api_key(store.saved().api_key)):
        # 提交的是打码值（`…abcd`）。真拿去发请求，上游会 401，而这里会把它翻译成
        # "密钥被拒"——用户于是去供应商后台换密钥，而他手里那把其实一直是好的。
        # 与 `save()` 里那条判据同一个理由：打码值不是密钥，任何一次都不能当密钥用。
        raise store.SettingsError(
            "settings_api_key_masked",
            "提交的是打码后的密钥（形如 …abcd），不是密钥本身；"
            "要测连接请重新粘贴完整的值，或者留空直接用已保存的那把。",
            status=400)
    # 地址不合法要在**发请求之前**说成 400，而不是让 `probe()` 返回一句
    # `ok=false` 的结论。两种情况都想拦，但给人的信号不同：400 是"这个地址不许
    # 填"，`ok=false` 是"这个地址填了但不通"。`probe()` 里那道同样的检查是给
    # 直接调它的人兜底的（比如将来的后台任务），不是重复。
    refusal = probe_module._refuse(base_url)
    if refusal is not None:
        raise store.SettingsError("settings_base_url_refused", refusal, status=400)
    result = await probe_module.probe(base_url, api_key, model)
    return TestModelSettingsResponse(
        ok=result.ok, message=result.message, model=result.model,
        status=result.status, latency_ms=result.latency_ms,
    )


def _notice_for(before: str, after: str) -> str | None:
    """改了地址要不要提醒一句。没改就返回 `None`（绝大多数保存都是这样）。

    为什么只提醒、不强制"先测通才能存"：这条接口的合法用法里有一种是"先把地址
    填上，等供应商开完白名单再试"——强制联网才能存，等于逼用户把一个还没生效的
    配置留在输入框里，刷新页面就丢了。提醒放在保存的**响应**里，是因为界面上只有
    那一刻能确定"地址真的变了"（同值重复保存 `changed=False`，也不该唠叨）。

    `before` 是空串时同样不提醒：从"什么都没配"到"填了一个地址"是首次配置，
    没有任何一次旧的成功可以作废。那时弹"之前那次测试的结论不成立"会让人去找
    一次并不存在的测试。
    """
    if not before or before == after:
        return None
    return "地址改了。之前那次「测试连接」的结论对新地址不成立，建议现在点一次「测试连接」。"


def _raise_incomplete() -> None:
    """三项不齐时抛统一业务错误。抽出来只为让上面那段的判断读起来是一句话。"""
    raise store.SettingsError(
        "settings_incomplete",
        "地址、密钥、模型名三项要先填齐才能测试连接。", status=400)


__all__ = ["router"]
