"""偏好的 HTTP 面：读全部、写一条、删一条，以及"要不要建议"。

**三个动词就是这一层存在的全部理由**（调研报告原话）：如果一条记忆写进去但从不
被读，那它就只是"心里记着"——用户看不到效果，也改不了。所以面板要能列出、能改、
能删，而这三件事各对应一个接口。没有它们，`user_prefs` 就是一张死表。

一条刻意的边界：**这里没有任何"自动学习"的入口**。`PUT` 只能由用户动作触发
（面板里改、或者点了建议里的按钮），`GET /suggestions` 只产出候选、不落库。
把"系统建议"做成一条会自动写入的规则是这次设计里最贵的错误——实测同一主题
两分钟内有两次相反的图型要求，自动总结会在那里写出两条互相矛盾的事实。
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict

from app.prefs import service

router = APIRouter(prefix="/prefs", tags=["用户偏好"])


class SetPrefRequest(BaseModel):
    """`PUT /prefs/{key}` 的请求体。

    `source` 允许调用方声明"这条是我点了建议才写的"：两种来源在界面上显示不同
    的标签（"你设置的" / "系统建议（你已同意）"），而那个区别只有写入的那一刻
    知道——事后再去猜"这条是不是建议来的"是猜不出来的。
    """

    model_config = ConfigDict(extra="forbid")
    value: str
    source: Literal["user_explicit", "suggestion_accepted"] = "user_explicit"
    #: 建议是从哪一次运行推出来的。可空：用户自己在下拉里勾"以后都这样"时没有
    #: 某一次具体的运行可指。
    source_run_id: str | None = None


# `suggestions` 必须排在带路径参数的路由**之前**。现在没有 `GET /prefs/{key}`，
# 所以顺序还不影响解析；把顺序写对是因为下一个人加 `GET /prefs/{key}` 时不会
# 记得回头调换——那时 `/prefs/suggestions` 会被当成 key=suggestions 的一条偏好。
@router.get("/suggestions", response_model=list[service.Suggestion])
def list_suggestions() -> list[service.Suggestion]:
    """从历史运行里提出候选建议。**只读，不落库**。

    判据在 `service.suggestions()` 里写死：最近 3 次有效选择都是同一个值，且与
    当前偏好不同。用户点了"设为默认"才会走 `PUT`，点了"不用"则什么都不发生——
    没有写入就没有记录，一次拒绝不需要留下痕迹。
    """
    return service.suggestions()


@router.get("", response_model=list[service.Pref])
def list_prefs() -> list[service.Pref]:
    """面板的全部内容：键、值、来源、两个时间戳，以及**上次生效**。

    `last_applied_at` 是这一层最该被读的一列：它来自 `runs.request.prefs_applied`，
    也就是"哪几次运行真的带着这条偏好跑过"。它为空不一定是错，但**所有条目都为空**
    就一定说明读路径没接上——那时面板上写满"尚未生效"，不用翻日志就能看出来。
    """
    return service.list_all()


@router.get("/{pref_key}", response_model=service.Pref)
def get_pref(pref_key: str) -> service.Pref:
    """读一条。不存在 404，不是 200 + null。

    **为什么要这个端点**：`/api/prefs/{pref_key}` 之前只挂了 `PUT` / `DELETE`，于是
    `GET /api/prefs/style` 这类请求会命中这条路径却被判成"方法不允许"（405）。405
    的字面意思是"这个资源存在，但这个方法不行"，而实际含义是"根本没有这个偏好"——
    同一件事两种错法，排查时会往错误的方向翻代码。现在未知键是 404，与 `DELETE`
    的口径一致。
    """
    pref = service.get(pref_key)
    if pref is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "pref_not_found", "message": f"没有这条偏好：{pref_key}"},
        )
    return pref


@router.put("/{pref_key}", response_model=service.Saved)
def set_pref(pref_key: str, body: SetPrefRequest) -> service.Saved:
    """写一条。同值重复提交返回 `changed=false`，且不刷新 `updated_at`。

    非法值（A 类枚举外、C 类超条数/超字数）由 `service.set` 抛 `PrefError`，
    `core/errors.py` 的统一出口会把它变成 `{code, message, detail, request_id}`：
    错误码分得开，界面才能显示"请先删除一条"而不是一句泛泛的失败。
    """
    pref, changed = service.set(
        pref_key, body.value, source=body.source, source_run_id=body.source_run_id)
    return service.Saved(pref=pref, changed=changed)


@router.delete("/{pref_key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_pref(pref_key: str) -> Response:
    """删一条。硬删，没有软删。

    404 而不是幂等 204：界面上"这条已经不在了"和"我删了但没删掉"必须分得开，
    否则下一次刷新面板会看见它还在，而用户以为删过了。
    """
    if not service.delete(pref_key):
        raise HTTPException(
            status_code=404,
            detail={"code": "pref_not_found", "message": "没有这条偏好，可能已经被删掉了"},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
