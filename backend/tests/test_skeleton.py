"""骨架冒烟测试：验证新增模块的流程能走通、桩接口按约定返回。

不访问模型、不写真实数据库（用临时库）。
"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.main import app
from app.ingestion.service import NotSupportedSource, parse_bytes
from app.contracts.source import SourceKind

BACKEND = Path(__file__).resolve().parents[1]

#: 门禁要求"有身份"。本文件手写 ASGI scope、不经过 `TestClient` 的 cookie 罐，
#: 所以自己签一个游客 cookie——游客 cookie 是 `<id>.<hmac>`，裸的 32 位十六进制
#: 已经不算身份了（会被当成坏 cookie，等价于没 cookie）。写死 id 而不是 `uuid4()`：
#: 出错时日志里那个 id 每次都一样，能一眼看出"是同一个人"还是"换了人"。
GUEST_ID = "0123456789abcdef0123456789abcdef"


def _guest_cookie() -> bytes:
    """现签一次游客 cookie。签名密钥从库路径派生，必须在 `MINDMAP_DB_PATH` 打好之后调用。"""
    from app.auth import store as auth_store
    return f"mm_guest={auth_store.sign_guest(GUEST_ID)}".encode()

MAP = {
    "title": "测试导图",
    "root": {
        "id": "root",
        "label": "根",
        "children": [
            {"id": "n1", "label": "重复项", "children": []},
            {"id": "n2", "label": "重复项", "children": []},
        ],
    },
}


def request(method: str, path: str, body=None):
    """最小 ASGI 调用器，避免额外依赖。"""
    import json as _json
    import uuid

    payload = b"" if body is None else _json.dumps(body, ensure_ascii=False).encode()
    path_only, _, query = path.partition("?")

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
            # 门禁要求"有身份"。这里手写 scope、不经过 TestClient 的 cookie 罐，
            # 所以直接给一个合法游客 id（32 位小写十六进制）。
            "headers": [(b"content-type", b"application/json"),
                        (b"cookie", _guest_cookie())],
            "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 8000),
        }
        await app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return status, raw

    return asyncio.run(run())


class SkeletonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = BACKEND / "data"
        data.mkdir(exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=data, ignore_cleanup_errors=True)
        cls.env = patch.dict(os.environ, {
            "MINDMAP_DB_PATH": str(Path(cls.tmp.name) / "skeleton.db"),
        })
        cls.env.start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.tmp.cleanup()

    def test_health(self):
        status, raw = request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertIn(b'"status":"ok"', raw)
        # 版本号必须在，页面标题旁显示的就是它；本机跑源码时是 dev。
        self.assertIn(b'"version"', raw)

    def test_app_version_follows_env(self):
        """`MINDMAP_VERSION` 由 install.sh 写进环境变量；没配时回落 dev。

        写死一个版本号会让本机看起来也像发布版，所以空值必须是 `dev`。
        """
        from app.main import _app_version

        with patch.dict(os.environ, {"MINDMAP_VERSION": "v0.4.7"}):
            self.assertEqual(_app_version(), "v0.4.7")
        with patch.dict(os.environ, {"MINDMAP_VERSION": "   "}):
            self.assertEqual(_app_version(), "dev")

    def test_text_source_round_trips_through_a_saved_map(self):
        """文本来源解析出的摘要能随导图一起存取。

        原先是「来源 → 流水线生成 → 保存」的整条链路；生成那一段已经收进 agent，
        这里只守两端：ingestion 把文本解析成带 id 的来源，mindmaps 存取时把
        `source_usage` 原样带回来。
        """
        status, raw = request("POST", "/api/ingestion/text", {"text": "人工智能的应用"})
        self.assertEqual(status, 200)
        source = json.loads(raw)["source"]
        self.assertEqual(source["status"], "parsed")
        usage = {"source_id": source["id"], "kind": source["kind"],
                 "filename": source["filename"],
                 "char_count": len(source["text"].strip())}
        content = {"title": "人工智能的应用",
                   "root": {"id": "r", "label": "人工智能的应用", "children": []},
                   "source_usage": [usage]}
        status, saved_raw = request("POST", "/api/mindmaps", {"content": content})
        self.assertEqual(status, 201)
        saved = json.loads(saved_raw)
        status, reloaded = request("GET", "/api/mindmaps/" + saved["id"])
        self.assertEqual(status, 200)
        # `SourceUsage.url` 是这一轮新增的可选字段（外部检索来源的原文地址）。
        # 用户上传的文本来源没有原文地址，所以它恒为 null——契约的可选字段在
        # 序列化时会带回默认值，这不算"没原样带回来"。去掉它再比，是为了让这条
        # 用例继续守"原样带回"这件事本身，而不是被一个新默认值绑住。
        restored = json.loads(reloaded)["content"]["source_usage"]
        self.assertEqual([{k: v for k, v in item.items() if k != "url"} for item in restored],
                         [usage])
        self.assertEqual([item.get("url") for item in restored], [None])

    def test_optimization_detects_duplicate(self):
        status, raw = request("POST", "/api/optimization/check", {"content": MAP})
        self.assertEqual(status, 200)
        self.assertIn("DUPLICATE_LABEL", raw.decode())
        status, raw = request("POST", "/api/optimization/suggest", {"content": MAP})
        suggestion = json.loads(raw)["suggestions"][0]
        self.assertEqual(suggestion["evidence"], "n1")
        self.assertIsNone(suggestion["confidence"])

    def test_repeated_label_under_different_parents_is_not_flagged(self):
        content = {"title": "学科", "root": {"id": "r", "label": "学科", "children": [
            {"id": "a", "label": "物理", "children": [{"id": "a1", "label": "应用", "children": []}]},
            {"id": "b", "label": "数学", "children": [{"id": "b1", "label": "应用", "children": []}]},
        ]}}
        status, raw = request("POST", "/api/optimization/suggest", {"content": content})
        self.assertEqual(status, 200)
        # 这条用例只回答"重复"这件事：不同父节点下的两个「应用」不是重复。
        # 断言从 `suggestions == []` 收窄成"没有 dedupe"，因为 `suggest()`
        # 现在也把结构诊断（这里是一级分支只有一条子支）转成 expand 建议了——
        # "一条建议都没有"在树上还有别的毛病时本来就不该成立。
        kinds = [item["kind"] for item in json.loads(raw)["suggestions"]]
        self.assertNotIn("dedupe", kinds)

    def test_export_puml(self):
        status, raw = request("POST", "/api/export/puml", {"content": MAP})
        self.assertEqual(status, 200)
        text = raw.decode()
        self.assertTrue(text.startswith("@startmindmap"))
        self.assertIn("@endmindmap", text)
        self.assertIn("* 根\n", text)
        self.assertNotIn("* 测试导图\n", text)

    def test_export_markdown(self):
        status, raw = request("POST", "/api/export/markdown", {"content": MAP})
        self.assertEqual(status, 200)
        text = raw.decode()
        self.assertTrue(text.startswith("# 测试导图"))
        self.assertIn("- 根", text)
        self.assertIn("    - 重复项", text)

    def test_export_opml(self):
        status, raw = request("POST", "/api/export/opml", {"content": MAP})
        self.assertEqual(status, 200)
        text = raw.decode()
        self.assertTrue(text.startswith('<?xml version="1.0"'))
        self.assertIn('version="2.0"', text)
        self.assertIn('text="根"', text)

    def test_knowledge_reports_not_implemented(self):
        status, raw = request("POST", "/api/knowledge/search", {"query": "碳中和"})
        self.assertEqual(status, 200)
        self.assertIn('"implemented":false', raw.decode())

    def test_agent_returns_plan_without_map(self):
        status, raw = request("POST", "/api/agent/tasks", {"goal": "设计一款智能家居产品"})
        self.assertEqual(status, 200)
        body = raw.decode()
        self.assertIn('"implemented":false', body)
        self.assertNotIn('"root"', body)

    def test_file_upload_requires_actual_file(self):
        """文件入口改成 multipart 后，不带文件不能假装成功。"""
        status, _ = request("POST", "/api/ingestion/files", {})
        self.assertEqual(status, 422)

    def test_text_endpoint_rejects_image_kind(self):
        status, _ = request("POST", "/api/ingestion/text", {"text": "内容", "kind": "image"})
        self.assertEqual(status, 422)

    def test_bytes_parser_rejects_blank_text(self):
        with self.assertRaises(NotSupportedSource):
            parse_bytes(SourceKind.text, "empty.txt", b"  ")


if __name__ == "__main__":
    unittest.main()
