"""统一错误响应：不管错误在哪一层抛出，出去都是一样的 JSON 结构。

固定格式::

    {"code": "...", "message": "中文说明", "detail": <原始细节>, "request_id": "8 位十六进制"}

`request_id` 优先沿用调用方传来的 `X-Request-ID`，没有就新生成 8 位十六进制；
它既写进响应体，也通过 `X-Request-ID` 响应头回传，方便前后端对齐同一次请求。
"""
from __future__ import annotations

from typing import Any, Mapping
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic_core import to_jsonable_python
from starlette.exceptions import HTTPException as StarletteHTTPException

REQUEST_ID_HEADER = "X-Request-ID"

# 调用方自带的 request id 只保留可见 ASCII，避免超长或非法字符污染日志与响应头。
_MAX_REQUEST_ID_LENGTH = 128


class AppError(Exception):
    """业务异常基类：携带业务 code、中文说明、HTTP 状态和原始细节。

    `headers` 是给少数"响应头本身有意义"的场景准备的，目前只有一处：
    登录被限速时的 `Retry-After`——它告诉客户端"过几秒再试"，写进响应体的话
    标准客户端（以及任何中间层的重试逻辑）都读不到。
    """

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        detail: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail
        self.headers = dict(headers) if headers else None


# 能从 detail 文案判断出业务含义时用描述性 code，判断不出就退回 http_{status}。
# 顺序即优先级，先命中的先生效。
_DETAIL_CODE_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("已被修改", "版本冲突"), "version_conflict"),
    (("不存在", "找不到"), "not_found"),
    (("待实现", "未实现", "尚未实现"), "not_implemented"),
    (("重复",), "duplicate"),
    (("超过", "过大", "过长"), "limit_exceeded"),
    (("不支持",), "unsupported"),
)

# 框架自带的英文 detail：message 换成中文，原始英文仍留在 detail 里。
_FRAMEWORK_MESSAGES = {
    "Not Found": "请求的地址不存在",
    "Method Not Allowed": "请求方法不被允许",
    "Not Acceptable": "无法提供客户端要求的响应格式",
    "Unsupported Media Type": "不支持的请求内容类型",
    "Request Entity Too Large": "请求体过大",
    "Internal Server Error": "服务器内部错误",
}


def _clean_request_id(value: str) -> str:
    """只保留可安全写回响应头的可见 ASCII 字符。"""
    cleaned = "".join(ch for ch in value if "!" <= ch <= "~")
    return cleaned[:_MAX_REQUEST_ID_LENGTH]


def _request_id(request: Request) -> str:
    """优先沿用 X-Request-ID 请求头，没有就用 uuid4 的前 8 位。"""
    cached = getattr(request.state, "request_id", None)
    if cached:
        return cached
    incoming = _clean_request_id((request.headers.get(REQUEST_ID_HEADER) or "").strip())
    request_id = incoming or uuid4().hex[:8]
    # 记到 state 上，同一次请求里多个出口取值一致。
    request.state.request_id = request_id
    return request_id


def _json_safe(value: Any) -> Any:
    """把细节转成可 JSON 序列化的结构，认不出的对象退化成字符串。"""
    return to_jsonable_python(value, fallback=str)


def _code_from(status: int, text: str | None) -> str:
    if text:
        for keywords, code in _DETAIL_CODE_HINTS:
            if any(keyword in text for keyword in keywords):
                return code
    return f"http_{status}"


def _message_from(status: int, text: str | None) -> str:
    if text:
        return _FRAMEWORK_MESSAGES.get(text) or text
    return f"请求处理失败（HTTP {status}）"


def _error_response(
    request: Request,
    *,
    status: int,
    code: str,
    message: str,
    detail: Any,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """所有出口共用：拼统一结构并回传 request id。"""
    request_id = _request_id(request)
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": message,
            "detail": _json_safe(detail),
            "request_id": request_id,
        },
        headers={**(headers or {}), REQUEST_ID_HEADER: request_id},
    )


def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return _error_response(request, status=exc.status, code=exc.code,
                           message=exc.message, detail=exc.detail,
                           headers=exc.headers)


def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """把 FastAPI / Starlette 的 HTTPException（含 404、405）转成统一格式。"""
    detail = exc.detail
    text = detail if isinstance(detail, str) else None
    return _error_response(
        request,
        status=exc.status_code,
        code=_code_from(exc.status_code, text),
        message=_message_from(exc.status_code, text),
        detail=detail,
        headers=exc.headers,
    )


def _validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """请求体 / 查询参数校验失败：原始 errors 列表原样留在 detail。"""
    return _error_response(
        request,
        status=422,
        code="validation_error",
        message="请求参数校验失败",
        detail=exc.errors(),
    )


def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底：未预料的异常也不裸奔成纯文本 500，内部信息不外泄。"""
    return _error_response(
        request,
        status=500,
        code="internal_error",
        message="服务器内部错误，请稍后重试",
        detail=None,
    )


def register_error_handlers(app: FastAPI) -> None:
    """一次性挂上全部错误处理器，在 main.py 里一行调用。"""
    app.add_exception_handler(AppError, _app_error_handler)
    # FastAPI 的 HTTPException 继承自 Starlette 的，注册一次即可覆盖两者。
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
