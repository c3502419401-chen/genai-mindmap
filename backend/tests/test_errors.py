"""错误响应格式测试：任何出口（404 / 422 / 业务异常）都必须是同一套结构。

不访问模型、不写真实数据库（用临时库）。
"""
import asyncio
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app.main import app

BACKEND = Path(__file__).resolve().parents[1]

#: 门禁要求"有身份"，而这里手写 ASGI scope、不经过 `TestClient` 的 cookie 罐。
#: 用 `store.sign_guest()` 现签一个游客 cookie——裸的 32 位十六进制已经不够了
#: （游客 cookie 是 `<id>.<hmac>`，没签名会被当成坏 cookie 等价于没 cookie）。
#: **整个文件共用一个**：换来换去会让"上一段存下的图"在下一段变成别人的，
#: 而报错看起来像隔离逻辑坏了。
GUEST_ID = uuid.uuid4().hex


def _guest_cookie() -> bytes:
    """现签一次游客 cookie。签名密钥从库路径派生，必须在 `MINDMAP_DB_PATH` 打好之后调用。"""
    from app.auth import store as auth_store
    return f"mm_guest={auth_store.sign_guest(GUEST_ID)}".encode()


def request(method: str, path: str, body=None, headers=None):
    """最小 ASGI 调用器（写法同 test_skeleton.request，额外回传响应头）。"""
    payload = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
    path_only, _, query = path.partition("?")
    raw_headers = [(b"content-type", b"application/json"),
                   (b"cookie", _guest_cookie())]
    for key, value in (headers or {}).items():
        raw_headers.append((key.lower().encode(), value.encode()))

    async def run():
        messages = []

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "path": path_only, "raw_path": path_only.encode(),
            "query_string": query.encode(), "root_path": "", "scheme": "http",
            "headers": raw_headers,
            "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 8000),
        }
        await app(scope, receive, send)
        start = next(m for m in messages if m["type"] == "http.response.start")
        raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        response_headers = {k.decode().lower(): v.decode() for k, v in start.get("headers", [])}
        return start["status"], raw, response_headers

    return asyncio.run(run())


class ErrorFormatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = BACKEND / "data"
        data.mkdir(exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=data, ignore_cleanup_errors=True)
        cls.env = patch.dict(os.environ, {
            "MINDMAP_DB_PATH": str(Path(cls.tmp.name) / "errors.db"),
        })
        cls.env.start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.tmp.cleanup()

    def assert_unified(self, body, response_headers):
        """四件套齐全，且响应头把同一个 request_id 带回来。"""
        self.assertEqual(set(body), {"code", "message", "detail", "request_id"})
        self.assertIsInstance(body["code"], str)
        self.assertTrue(body["code"])
        self.assertIsInstance(body["message"], str)
        self.assertTrue(body["message"])
        self.assertRegex(body["request_id"], r"^[0-9a-f]{8}$")
        self.assertEqual(response_headers["x-request-id"], body["request_id"])

    def test_unknown_path_returns_unified_404(self):
        status, raw, headers = request("GET", "/api/does-not-exist")
        self.assertEqual(status, 404)
        body = json.loads(raw)
        self.assert_unified(body, headers)
        self.assertEqual(body["code"], "http_404")

    def test_invalid_body_returns_validation_error(self):
        status, raw, headers = request("POST", "/api/mindmaps", {"content": {}})
        self.assertEqual(status, 422)
        body = json.loads(raw)
        self.assert_unified(body, headers)
        self.assertEqual(body["code"], "validation_error")
        self.assertEqual(body["message"], "请求参数校验失败")
        # detail 保留 Pydantic 原始 errors 列表，方便定位具体字段
        self.assertIsInstance(body["detail"], list)
        self.assertTrue(body["detail"])
        self.assertIn("loc", body["detail"][0])

    def test_request_id_header_is_echoed(self):
        calls = [
            ("GET", "/api/does-not-exist", None),
            ("POST", "/api/mindmaps", {"content": {}}),
        ]
        for method, path, body in calls:
            with self.subTest(path=path):
                status, raw, headers = request(method, path, body,
                                               headers={"X-Request-ID": "my-trace-1"})
                self.assertGreaterEqual(status, 400)
                self.assertEqual(json.loads(raw)["request_id"], "my-trace-1")
                self.assertEqual(headers["x-request-id"], "my-trace-1")


if __name__ == "__main__":
    unittest.main()
