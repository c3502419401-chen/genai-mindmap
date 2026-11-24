"""契约 v2（`docs/CONTRACT_V2.md`）。

重点不是"字段都在"，而是三件容易出事的事：
    1. 没有 `schema_version` 的 v1 记录读得进来（不需要迁移脚本、不需要停机）；
    2. 新字段的默认值不会把旧数据的语义改掉；
    3. 那几条横跨整棵树的校验（关系端点、手动布局坐标）真的会拦住坏数据。
"""
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from app.contracts.mindmap import (
    MAX_RELATIONS,
    MAX_TREE_NODES,
    MindMapContent,
    migrate_content,
)
from app.mindmaps import repository


def content(root=None, **extra):
    return {"title": "测试导图", "root": root or {"id": "root", "label": "根", "children": []}, **extra}


def node(node_id, label=None, children=None, **extra):
    return {"id": node_id, "label": label or node_id, "children": children or [], **extra}


def wide_tree(total: int) -> dict:
    """造一棵 `total` 个节点、层级尽量浅的树（每层铺满 50 个子节点）。"""
    root = node("n0", "根")
    nodes = [root]
    cursor = 0
    index = 1
    while index < total:
        parent = nodes[cursor]
        cursor += 1
        for _ in range(50):
            if index >= total:
                break
            child = node(f"n{index}")
            parent["children"].append(child)
            nodes.append(child)
            index += 1
    return root


class DefaultsTest(unittest.TestCase):
    def test_v1_payload_gets_v2_defaults(self):
        """v1 的请求体没有 schema_version / layout / theme / relations，也要能过。"""
        parsed = MindMapContent.model_validate(content())
        self.assertEqual(parsed.schema_version, 2)
        self.assertEqual(parsed.layout.value, "tree")
        self.assertEqual(parsed.theme.preset, "default")
        self.assertEqual(parsed.theme.direction, "right")
        self.assertEqual(parsed.relations, [])
        self.assertIsNone(parsed.root.note)
        self.assertFalse(parsed.root.collapsed)

    def test_vertical_layout_is_accepted(self):
        """画布上的"向下"是 vertical，不是 logic（logic 是缩进清单）。"""
        parsed = MindMapContent.model_validate(content(layout="vertical"))
        self.assertEqual(parsed.layout.value, "vertical")

    def test_unknown_schema_version_is_rejected(self):
        with self.assertRaises(ValidationError):
            MindMapContent.model_validate(content(schema_version=3))


class TextFoldingTest(unittest.TestCase):
    def test_label_newlines_are_folded(self):
        parsed = MindMapContent.model_validate(content(node("root", "第一行\n第二行")))
        self.assertEqual(parsed.root.label, "第一行 第二行")

    def test_blank_label_is_rejected(self):
        with self.assertRaises(ValidationError):
            MindMapContent.model_validate(content(node("root", "   \n ")))

    def test_blank_note_becomes_none(self):
        """纯空白的正文等于没写；留空串会让前端多一个"有正文但显示为空"的分支。"""
        parsed = MindMapContent.model_validate(content(node("root", note="  \n ")))
        self.assertIsNone(parsed.root.note)

    def test_note_is_kept_and_bounded(self):
        parsed = MindMapContent.model_validate(content(node("root", note="正文" * 1000)))
        self.assertEqual(len(parsed.root.note), 2000)
        with self.assertRaises(ValidationError):
            MindMapContent.model_validate(content(node("root", note="正文" * 1001)))


class RelationTest(unittest.TestCase):
    def build(self, relations):
        root = node("root", children=[node("a"), node("b")])
        return content(root, relations=relations)

    def test_valid_relation_is_accepted(self):
        parsed = MindMapContent.model_validate(self.build([
            {"id": "r1", "source": "a", "target": "b", "type": "depends", "label": "先修"},
        ]))
        self.assertEqual(parsed.relations[0].provenance, "user")

    def test_endpoint_must_exist(self):
        with self.assertRaises(ValidationError) as caught:
            MindMapContent.model_validate(self.build([
                {"id": "r1", "source": "a", "target": "missing", "type": "ref"},
            ]))
        self.assertIn("不在导图中", str(caught.exception))

    def test_self_loop_is_rejected(self):
        with self.assertRaises(ValidationError) as caught:
            MindMapContent.model_validate(self.build([
                {"id": "r1", "source": "a", "target": "a", "type": "cause"},
            ]))
        self.assertIn("不能是同一个节点", str(caught.exception))

    def test_same_triple_twice_is_rejected(self):
        link = {"source": "a", "target": "b", "type": "conflict"}
        with self.assertRaises(ValidationError) as caught:
            MindMapContent.model_validate(self.build([
                {"id": "r1", **link}, {"id": "r2", **link},
            ]))
        self.assertIn("同类关系不能重复", str(caught.exception))

    def test_duplicate_relation_id_is_rejected(self):
        with self.assertRaises(ValidationError) as caught:
            MindMapContent.model_validate(self.build([
                {"id": "r1", "source": "a", "target": "b", "type": "ref"},
                {"id": "r1", "source": "b", "target": "a", "type": "ref"},
            ]))
        self.assertIn("关系 ID 必须唯一", str(caught.exception))

    def test_relation_count_is_bounded(self):
        relations = [{"id": f"r{i}", "source": "a", "target": "b", "type": "ref"}
                     for i in range(MAX_RELATIONS + 1)]
        with self.assertRaises(ValidationError):
            MindMapContent.model_validate(self.build(relations))


class PositionTest(unittest.TestCase):
    def test_manual_layout_requires_a_position(self):
        with self.assertRaises(ValidationError) as caught:
            MindMapContent.model_validate(content(layout="manual"))
        self.assertIn("至少需要一个节点带坐标", str(caught.exception))

    def test_manual_layout_with_position_is_accepted(self):
        parsed = MindMapContent.model_validate(
            content(node("root", pos={"x": 120.5, "y": -40}), layout="manual"))
        self.assertEqual(parsed.root.pos.x, 120.5)

    def test_auto_layout_rejects_positions(self):
        """自动布局带着历史坐标，换布局时会被当成用户意图。"""
        with self.assertRaises(ValidationError) as caught:
            MindMapContent.model_validate(content(node("root", pos={"x": 1, "y": 2})))
        self.assertIn("只有手动布局才允许", str(caught.exception))


class TreeLimitTest(unittest.TestCase):
    def test_node_limit_follows_current_capacity(self):
        MindMapContent.model_validate(content(wide_tree(MAX_TREE_NODES)))
        with self.assertRaises(ValidationError):
            MindMapContent.model_validate(content(wide_tree(MAX_TREE_NODES + 1)))


class MigrateTest(unittest.TestCase):
    def test_v1_record_gains_defaults(self):
        migrated = migrate_content({"title": "旧图", "root": {"id": "r", "label": "根", "children": []}})
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["layout"], "tree")
        self.assertEqual(migrated["relations"], [])
        self.assertEqual(migrated["theme"], {})
        self.assertEqual(migrated["title"], "旧图")

    def test_existing_fields_are_not_overwritten(self):
        migrated = migrate_content({"title": "旧图", "root": {"id": "r", "label": "根", "children": []},
                                    "layout": "vertical", "theme": {"preset": "forest"}})
        self.assertEqual(migrated["layout"], "vertical")
        self.assertEqual(migrated["theme"], {"preset": "forest"})

    def test_v2_record_is_returned_as_is(self):
        record = {"schema_version": 2, "title": "新图", "root": {"id": "r", "label": "根", "children": []}}
        self.assertIs(migrate_content(record), record)

    def test_non_object_is_rejected(self):
        with self.assertRaises(TypeError):
            migrate_content([])  # type: ignore[arg-type]


class RepositoryCompatibilityTest(unittest.TestCase):
    """仓储层：库里躺着 v1 记录时读得出来，保存后写成 v2。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = patch.dict(os.environ, {"MINDMAP_DB_PATH": str(Path(self.tmp.name) / "repo.db")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def insert_v1_row(self, map_id="legacy"):
        with closing(sqlite3.connect(repository.db_path())) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS mindmaps (id TEXT PRIMARY KEY, version INTEGER NOT NULL,"
                         " content TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute("INSERT INTO mindmaps (id, version, content, updated_at) "
                         "VALUES (?, 1, ?, '2026-11-01T00:00:00.000Z')",
                         (map_id, json.dumps({"title": "旧图", "root": {"id": "r", "label": "根", "children": []}},
                                             ensure_ascii=False)))

    def test_v1_row_is_readable(self):
        self.insert_v1_row()
        record = repository.get_map("legacy")
        self.assertIsNotNone(record)
        self.assertEqual(record.content.schema_version, 2)
        self.assertEqual(record.content.theme.direction, "right")

    def test_saving_upgrades_row_to_v2(self):
        self.insert_v1_row()
        parsed = repository.get_map("legacy").content
        repository.update_map("legacy", 1, parsed)
        with closing(sqlite3.connect(repository.db_path())) as conn, conn:
            raw = json.loads(conn.execute("SELECT content FROM mindmaps WHERE id = 'legacy'").fetchone()[0])
        self.assertEqual(raw["schema_version"], 2)
        self.assertEqual(raw["layout"], "tree")


if __name__ == "__main__":
    unittest.main()
