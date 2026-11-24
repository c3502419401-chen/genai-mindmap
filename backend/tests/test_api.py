"""通过 ASGI 请求验证导图持久化与契约版本兼容，不访问第三方模型。"""
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

#: 门禁要求"有身份"，而这个文件手写 ASGI scope、不经过 `TestClient` 的 cookie 罐。
#: 直接给一个合法的游客 cookie：**得用 `store.sign_guest()` 现签**，光有 32 位
#: 小写十六进制不够——游客 cookie 现在是 `<id>.<hmac>`，裸 id 会被当成坏 cookie
#: 等价于没 cookie（`store.read_guest` 返回 None），于是每个请求都 401。
#: 签名密钥从库路径派生，所以必须在 `MINDMAP_DB_PATH` 打好之后才签（见 `_cookie()`）。
#: **同一个测试类里所有请求共用这一个 id**——不共用的话第二次 `PUT` 会撞上
#: `map_forbidden`（图归上一个游客所有），那看起来像隔离出了 bug，其实是测试自己换了个人。
GUEST_ID = uuid.uuid4().hex


def _guest_cookie() -> bytes:
    """现签一次游客 cookie。**不能提到模块级常量**：那时 `MINDMAP_DB_PATH` 还没打。"""
    from app.auth import store as auth_store
    return f"mm_guest={auth_store.sign_guest(GUEST_ID)}".encode()


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = BACKEND / "data"
        data.mkdir(exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=data, ignore_cleanup_errors=True)
        cls.env = patch.dict(os.environ, {"MINDMAP_DB_PATH": str(Path(cls.tmp.name) / "test.db")})
        cls.env.start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.tmp.cleanup()

    def call(self, path, method="GET", data=None):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else b""

        async def run():
            messages = []
            delivered = False

            async def receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": payload, "more_body": False}
                return {"type": "http.disconnect"}

            async def send(message):
                messages.append(message)

            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "method": method, "scheme": "http", "path": "/api" + path,
                     "raw_path": ("/api" + path).encode(), "query_string": b"",
                     "headers": [(b"content-type", b"application/json"),
                                 (b"cookie", _guest_cookie())],
                     "client": ("127.0.0.1", 1), "server": ("test", 80)}
            await app(scope, receive, send)
            status = next(m["status"] for m in messages if m["type"] == "http.response.start")
            body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            return status, json.loads(body)

        return asyncio.run(run())

    def test_save_load_update_and_conflict(self):
        # 内容直接构造：生成那一段已收进 agent，这里只守持久化与版本号语义。
        content = {"title": "机器学习", "root": {"id": "1", "label": "机器学习", "children": [
            {"id": "2", "label": "请编辑此演示节点", "children": []}]}}
        status, saved = self.call("/mindmaps", "POST", {"content": content})
        self.assertEqual(status, 201)
        map_id = saved["id"]
        content = saved["content"]
        status, loaded = self.call("/mindmaps/" + map_id)
        self.assertEqual((status, loaded["content"]), (200, content))
        content["root"]["label"] = "已编辑"
        status, updated = self.call("/mindmaps/" + map_id, "PUT", {"version": 1, "content": content})
        self.assertEqual((status, updated["version"]), (200, 2))
        # 冲突时错误信息要能看见（统一错误格式的 code / message）
        status, conflict = self.call("/mindmaps/" + map_id, "PUT", {"version": 1, "content": content})
        self.assertEqual((status, conflict["code"]), (409, "version_conflict"))
        self.assertEqual(conflict["message"], "导图已被修改，请重新打开后再保存")
        self.assertEqual(self.call("/mindmaps/" + map_id)[1]["content"]["root"]["label"], "已编辑")

    def test_v2_payload_round_trips(self):
        """v2 的正文、布局、主题要能原样存取——前端保存的就是这些字段。"""
        payload = {"content": {
            "schema_version": 2, "title": "契约 v2", "layout": "vertical",
            "theme": {"preset": "forest", "direction": "left"},
            "root": {"id": "root", "label": "根", "note": "这是正文",
                     "children": [{"id": "a", "label": "子", "note": None, "children": []}]},
        }}
        status, created = self.call("/mindmaps", "POST", payload)
        self.assertEqual(status, 201)
        self.assertEqual(created["content"]["schema_version"], 2)
        self.assertEqual(created["content"]["root"]["note"], "这是正文")
        self.assertEqual(created["content"]["theme"]["preset"], "forest")
        status, loaded = self.call("/mindmaps/" + created["id"])
        self.assertEqual((status, loaded["content"]), (200, created["content"]))

    def test_v1_payload_still_accepted(self):
        """旧前端发来的请求没有 schema_version，读出来应当已是 v2 形状。"""
        v1 = {"content": {"title": "旧客户端", "root": {"id": "root", "label": "根", "children": []}}}
        status, created = self.call("/mindmaps", "POST", v1)
        self.assertEqual(status, 201)
        self.assertEqual(created["content"]["schema_version"], 2)
        self.assertEqual(created["content"]["layout"], "tree")

    def test_validation_and_not_found(self):
        bad = {"title": "重复", "root": {"id": "x", "label": "根", "children": [
            {"id": "x", "label": "子", "children": []}]}}
        self.assertEqual(self.call("/mindmaps", "POST", {"content": bad})[0], 422)
        self.assertEqual(self.call("/mindmaps/does-not-exist")[0], 404)


if __name__ == "__main__":
    unittest.main()
