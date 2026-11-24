"""导入：四种（六种）格式、五件边界处理、与导出的闭环。

结构的组织方式是"每条断言对应一个真实的坑"，不是按函数分：

    - `DetectTest`          扩展名 + 嗅探的判据，以及两者冲突时谁赢
    - `*RoundTripTest`      每种格式：本仓库导出 → 导入 → 与原树等价（闭环核心）
    - `XmindTest`           新版 content.json、旧版 content.xml、zip 里没有 XMind 成员
    - `LimitTest`           1001 节点 / 9 层 / 51 子：截断 + 告警，**不整份拒绝**
    - `SourceRefTest`       引用被清空且有告警
    - `IdTest`              id 全部重新生成、边端点跟着重建
    - `ErrorTest`           未知格式 → unsupported_format；损坏文件 → parse_failed
    - `FreemindTest`        .mm 的 icon/color/richcontent/FOLDED 映射
    - `InterfaceTest`       ASGI 打一次 multipart，确认响应形状与"只解析不落库"

闭环用例里有一条刻意留下的**不**等价，写在这里以免下一个人以为是 bug：
markdown / opml / puml 的**标题槽**只有一个，导出取 `note or label`，所以带 `note`
的节点往返之后 `note` 变成 `label`（文字不丢、字段位置变）。`note` 转成 `label`
这件事由 `test_note_text_survives_as_label` 明确钉住。`detail` 不在那个槽里——它
有各格式自己的第二个通道（`>` 引用块 / `'` 注释 / `_note` 属性），所以它能原样
回去；markdown 这条路有用例逐字断言（`MarkdownDetailTest`）。
"""
from __future__ import annotations

import json
import unittest
import zipfile
from io import BytesIO

from conftest import guest_client

from app.contracts.mindmap import FishboneContent, FlowContent, MindMapContent
from app.export.service import to_json, to_markdown, to_opml, to_puml
from app.importing.detect import detect_format
from app.importing.errors import ParseFailure, UnsupportedFormat
from app.importing.service import parse_import


def build(**overrides) -> dict:
    """一棵有代表性但不大的树：多分支、多层、带颜色/图标/正文。"""
    data = {
        "title": "季度规划",
        "root": {
            "id": "root", "label": "产品",
            "children": [
                {"id": "m", "label": "市场", "children": [
                    {"id": "m1", "label": "竞品调研"},
                    {"id": "m2", "label": "用户访谈"},
                ]},
                {"id": "t", "label": "技术", "children": [
                    {"id": "t1", "label": "架构设计", "children": [
                        {"id": "t1a", "label": "存储选型"},
                    ]},
                ]},
            ],
        },
    }
    data.update(overrides)
    return data


def parse_text(name: str, text: str):
    return parse_import(name, text.encode("utf-8"))


def shape(node) -> tuple:
    """结构 + 文字。id 不参与比较（导入一定重新生成）。"""
    return (node.label, [shape(child) for child in node.children])


def text_shape(node) -> tuple:
    """导出文字口径的结构（`note or label`），用于 markdown/opml/puml 的往返。"""
    return (node.note or node.label, [text_shape(child) for child in node.children])


def names_of(root) -> dict[str, str]:
    """`id → 导出文字`，把边的端点翻译成人读得懂的名字。"""
    out: dict[str, str] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        out[node.id] = node.note or node.label
        stack.extend(node.children)
    return out


def codes(warnings) -> list[str]:
    return [warning.code for warning in warnings]


class DetectTest(unittest.TestCase):
    def test_zip_magic_wins_over_json_suffix(self):
        """题目里的那条判据：`.json` 但有 zip 魔数 → 其实是 xmind。"""
        self.assertEqual(detect_format("导出.json", b"PK\x03\x04rest"), "xmind")

    def test_strong_content_beats_a_wrong_suffix(self):
        """强内容特征只可能来自那一种格式，扩展名写错也按内容读。"""
        self.assertEqual(
            detect_format("a.json", to_opml(MindMapContent.model_validate(build())).encode()),
            "opml")
        self.assertEqual(
            detect_format("a.md", '<map><node TEXT="x"/></map>'.encode()), "freemind")

    def test_weak_content_yields_to_the_suffix(self):
        """markdown 的嗅探是最弱一档：`.json` 里的 `- 项` 仍按 JSON 读并报错。

        反过来（让 markdown 赢）的后果更糟：用户以为自己给了一份 JSON，实际拿到
        一张按列表解析出来的图，而"这不是 JSON"这句真话永远不会出现。
        """
        self.assertEqual(detect_format("a.json", "- 项\n# 标题\n".encode()), "json")
        self.assertEqual(detect_format("a.puml", "* 根\n** 子\n".encode()), "puml")

    def test_unknown_returns_none(self):
        self.assertIsNone(detect_format("a.csv", b"a,b,c\n1,2,3\n"))
        self.assertIsNone(detect_format("", b"\xff\xfe\x00\x01garbage binary"))

    def test_txt_needs_outline_content(self):
        """`.txt` 只在内容也像大纲时才算 markdown——它是所有人的默认格式。"""
        self.assertEqual(detect_format("a.txt", "- 一\n  - 二\n".encode()), "markdown")
        self.assertIsNone(detect_format("a.txt", "今天开会讨论了三件事，没有结论。".encode()))

    def test_each_format_is_recognised_from_content(self):
        content = MindMapContent.model_validate(build())
        self.assertEqual(detect_format("x", to_json(content).encode()[:8192]), "json")
        self.assertEqual(detect_format("x", to_markdown(content).encode()[:8192]), "markdown")
        self.assertEqual(detect_format("x", to_opml(content).encode()[:8192]), "opml")
        self.assertEqual(detect_format("x", to_puml(content).encode()[:8192]), "puml")


class JsonRoundTripTest(unittest.TestCase):
    """JSON 是唯一无损的一条路：结构、视觉、图型、边全部保住。"""

    def rich(self) -> MindMapContent:
        return MindMapContent.model_validate(build(
            diagram_type="flowchart",
            theme={"preset": "slate", "direction": "left"},
            edges=[{"id": "e1", "source": "m1", "target": "t1", "label": "是",
                    "kind": "branch"}],
            root={
                "id": "root", "label": "产品", "note": "根备注", "detail": "根详情",
                "children": [
                    {"id": "m", "label": "市场", "color": "green", "icon": "🚀",
                     "collapsed": True, "children": [{"id": "m1", "label": "竞品调研"}]},
                    {"id": "t", "label": "技术", "children": [{"id": "t1", "label": "架构设计"}]},
                ],
            },
        ))

    def test_roundtrip_is_lossless(self):
        original = self.rich()
        decoded = parse_text("map.json", to_json(original))
        result = decoded.content

        self.assertEqual(decoded.source_format, "json")
        self.assertEqual(shape(result.root), shape(original.root))
        self.assertEqual(result.title, original.title)
        self.assertEqual(result.diagram_type, "flowchart")
        self.assertEqual(result.theme.direction, "left")
        self.assertEqual(len(result.edges), 1)
        names = names_of(result.root)
        self.assertEqual(names[result.edges[0].source], "竞品调研")
        self.assertEqual(names[result.edges[0].target], "架构设计")
        self.assertEqual(result.root.note, "根备注")
        self.assertEqual(result.root.detail, "根详情")

    def test_visual_fields_survive(self):
        result = parse_text("map.json", to_json(self.rich())).content
        branch = result.root.children[0]
        self.assertEqual(branch.color, "green")
        self.assertEqual(branch.icon, "🚀")
        self.assertTrue(branch.collapsed)

    def test_save_request_and_map_record_envelopes(self):
        """`{"content": …}`（保存请求体）与带 id/version 的库记录都要读得出来。"""
        body = build()
        for envelope in ({"content": body},
                         {"id": "abc", "version": 3, "content": body, "updated_at": "x"}):
            result = parse_text("saved.json", json.dumps(envelope, ensure_ascii=False)).content
            self.assertEqual(result.title, "季度规划")

    def test_layout_manual_with_positions_is_kept(self):
        body = build()
        body["layout"] = "manual"
        body["root"]["pos"] = {"x": 10, "y": 20}
        result = parse_text("manual.json", json.dumps(body, ensure_ascii=False))
        self.assertEqual(result.content.layout.value, "manual")
        self.assertEqual(result.content.root.pos.x, 10)

    def test_manual_layout_without_positions_is_downgraded(self):
        """契约要求 manual 至少一个坐标，否则整份 422——所以降级而不是失败。"""
        body = build()
        body["layout"] = "manual"
        result = parse_text("manual.json", json.dumps(body, ensure_ascii=False))
        self.assertEqual(result.content.layout.value, "tree")
        self.assertIn("fields_dropped", codes(result.warnings))

    def test_positions_are_dropped_when_layout_is_not_manual(self):
        body = build()
        body["root"]["pos"] = {"x": 1, "y": 2}
        result = parse_text("pos.json", json.dumps(body, ensure_ascii=False))
        self.assertIsNone(result.content.root.pos)
        self.assertIn("fields_dropped", codes(result.warnings))

    def test_relations_are_rebuilt_with_import_provenance(self):
        body = build()
        body["relations"] = [
            {"id": "r1", "source": "m1", "target": "t1", "type": "cause", "label": "导致"},
            {"id": "r2", "source": "m1", "target": "nowhere", "type": "cause"},
        ]
        result = parse_text("rel.json", json.dumps(body, ensure_ascii=False))
        self.assertEqual(len(result.content.relations), 1)
        relation = result.content.relations[0]
        self.assertEqual(relation.provenance, "import")
        self.assertEqual(names_of(result.content.root)[relation.source], "竞品调研")
        self.assertIn("relations_dropped", codes(result.warnings))


class MarkdownRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.original = MindMapContent.model_validate(build())

    def test_tree_and_title_are_equivalent(self):
        decoded = parse_text("map.md", to_markdown(self.original))
        self.assertEqual(decoded.source_format, "markdown")
        self.assertEqual(shape(decoded.content.root), shape(self.original.root))
        self.assertEqual(decoded.content.title, "季度规划")
        self.assertEqual(decoded.warnings, [])

    def test_note_text_survives_as_label(self):
        """markdown 只有一个文字槽位：正文往返之后落在 `label` 里。

        文字没丢、位置变了——这不是 bug，是格式能力。`note` 为空是为了让"哪部分
        是标题"这个区分也可以被断言到（它确实丢了）。
        """
        original = MindMapContent.model_validate({
            "title": "机器学习",
            "root": {"id": "r", "label": "机器学习", "note": "研究如何让计算机学规律。",
                     "children": [{"id": "c", "label": "监督学习", "note": "带标签的数据。"}]},
        })
        result = parse_text("note.md", to_markdown(original)).content
        self.assertEqual(result.root.label, "研究如何让计算机学规律。")
        self.assertIsNone(result.root.note)
        self.assertEqual(result.root.children[0].label, "带标签的数据。")

    def test_edges_are_read_back(self):
        original = MindMapContent.model_validate(build(
            edges=[{"id": "e1", "source": "m1", "target": "t1", "label": "是"},
                   {"id": "e2", "source": "t1", "target": "m2", "kind": "loop"}]))
        result = parse_text("edges.md", to_markdown(original)).content
        self.assertEqual(len(result.edges), 2)
        names = names_of(result.root)
        pairs = {(names[edge.source], names[edge.target], edge.label, edge.kind)
                 for edge in result.edges}
        self.assertIn(("竞品调研", "架构设计", "是", None), pairs)
        # 没 label 的边导出成"回退"，导入要把它读回 `loop`
        self.assertIn(("架构设计", "用户访谈", "回退", "loop"), pairs)

    def test_four_space_indent_is_read_as_the_same_tree(self):
        """用户用编辑器重新缩进成 4 空格，层级不能变成两倍。"""
        text = "# 标题\n\n- 根\n    - 子\n        - 孙\n"
        result = parse_text("x.md", text).content
        self.assertEqual(shape(result.root), ("根", [("子", [("孙", [])])]))

    def test_checkbox_maps_to_visual_fields_and_leaves_label_clean(self):
        text = "# 待办\n\n- 发布\n  - [x] 写测试\n  - [ ] 补文档\n"
        result = parse_text("todo.md", text).content
        done = result.root.children[0]
        self.assertEqual(done.label, "写测试")
        self.assertEqual(done.icon, "✅")
        self.assertEqual(done.color, "green")
        todo = result.root.children[1]
        self.assertEqual(todo.label, "补文档")
        self.assertEqual(todo.color, "amber")

    def test_escaped_angle_brackets_are_restored(self):
        original = MindMapContent.model_validate({
            "title": "泛型", "root": {"id": "r", "label": "Map<K,V>", "children": []}})
        result = parse_text("g.md", to_markdown(original)).content
        self.assertEqual(result.root.label, "Map<K,V>")

    def test_code_fence_content_is_ignored(self):
        text = "# 标题\n\n- 根\n\n```\n- 这不是节点\n```\n"
        result = parse_text("f.md", text).content
        self.assertEqual(len(result.root.children), 0)

    def test_multiple_top_level_items_are_attached_under_root(self):
        text = "# 标题\n\n- 甲\n- 乙\n"
        result = parse_text("multi.md", text)
        self.assertEqual([child.label for child in result.content.root.children], ["乙"])
        self.assertIn("format_notice", codes(result.warnings))

    def test_no_list_items_is_a_parse_failure(self):
        with self.assertRaises(ParseFailure):
            parse_text("empty.md", "# 只有标题\n\n一段话。\n")


class MarkdownDetailTest(unittest.TestCase):
    """`detail`（生成出来的说明）在 markdown 里的往返。

    这一路以前是"三种文本格式全丢 detail，只有 JSON 保得住"。丢的不是装饰字段：
    `detail` 是生成的核心产物（实测平均 142 字），而 `note` 可能根本是空的——
    读者拿到的文件里，一个节点只剩一行标题。

    落点是节点行下面一句引用块（`  > …`）：不进列表缩进层级，所以不会被读成
    下一层节点，也不可能与标题挤在同一行。
    """

    def tree_with_detail(self) -> MindMapContent:
        return MindMapContent.model_validate({
            "title": "报销",
            "root": {"id": "r", "label": "提交申请",
                     "detail": "把报销单和发票拍照上传，缺一张都要退回。",
                     "children": [
                         {"id": "a", "label": "填单", "detail": "写清事由与金额。"},
                         {"id": "b", "label": "上传发票"},
                     ]},
        })

    def test_detail_round_trips_in_markdown(self):
        restored = parse_text("d.md", to_markdown(self.tree_with_detail())).content
        self.assertEqual(restored.root.label, "提交申请")
        self.assertEqual(restored.root.detail, "把报销单和发票拍照上传，缺一张都要退回。")
        self.assertEqual(restored.root.children[0].detail, "写清事由与金额。")
        self.assertIsNone(restored.root.children[1].detail)

    def test_detail_does_not_get_into_the_title(self):
        """`detail` 一旦并进标题行，大纲就没法扫读了——那是这场修复的底线。"""
        restored = parse_text("d.md", to_markdown(self.tree_with_detail())).content
        self.assertNotIn("把报销单和发票拍照上传", restored.root.label)
        self.assertEqual(len(restored.root.children), 2)

    def test_multiline_quote_is_joined_into_one_detail(self):
        """用户把一句说明手动折成两行引用块：拼回一句，不留下换行。"""
        text = "# 标题\n\n- 甲\n  > 第一句。\n  > 第二句。\n"
        restored = parse_text("m.md", text).content
        self.assertEqual(restored.root.detail, "第一句。 第二句。")

    def test_quote_attaches_to_the_node_above_it(self):
        """归属看**位置**不看缩进：用户重排缩进之后说明仍然挂在同一个节点上。"""
        text = "# 标题\n\n- 甲\n  - 乙\n> 乙的说明（顶格写也不跑）\n"
        restored = parse_text("q.md", text).content
        self.assertEqual(restored.root.children[0].detail, "乙的说明（顶格写也不跑）")
        self.assertIsNone(restored.root.detail)


class OpmlRoundTripTest(unittest.TestCase):
    def test_tree_title_and_edges_roundtrip(self):
        original = MindMapContent.model_validate(build(
            edges=[{"id": "e1", "source": "m1", "target": "t1", "label": "驳回"},
                   {"id": "e2", "source": "t1", "target": "m2", "kind": "loop"}]))
        decoded = parse_text("map.opml", to_opml(original))
        self.assertEqual(decoded.source_format, "opml")
        self.assertEqual(shape(decoded.content.root), shape(original.root))
        self.assertEqual(decoded.content.title, "季度规划")
        names = names_of(decoded.content.root)
        pairs = {(names[edge.source], names[edge.target], edge.label, edge.kind)
                 for edge in decoded.content.edges}
        self.assertIn(("竞品调研", "架构设计", "驳回", None), pairs)
        self.assertIn(("架构设计", "用户访谈", "回退", "loop"), pairs)

    def test_edges_section_is_not_a_branch(self):
        original = MindMapContent.model_validate(build(
            edges=[{"id": "e1", "source": "m1", "target": "t1", "label": "是"}]))
        result = parse_text("map.opml", to_opml(original)).content
        labels = [node.label for node in result.root.children]
        self.assertNotIn("附加关系", labels)

    def test_multiple_top_level_outlines_are_attached(self):
        text = """<?xml version="1.0" encoding="UTF-8"?>
        <opml version="2.0"><head><title>t</title></head><body>
          <outline text="甲"/>
          <outline text="乙"><outline text="乙一"/></outline>
        </body></opml>"""
        result = parse_text("multi.opml", text)
        self.assertEqual(result.content.root.label, "甲")
        self.assertEqual([child.label for child in result.content.root.children], ["乙"])
        self.assertEqual(result.content.root.children[0].children[0].label, "乙一")
        self.assertIn("format_notice", codes(result.warnings))

    def test_doctype_is_refused(self):
        """实体递归能把 1KB 的文件撑成 GB 级内存；我们不产出 DOCTYPE，直接拒。"""
        text = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
                '<opml version="2.0"><body><outline text="&lol;"/></body></opml>')
        with self.assertRaises(ParseFailure):
            parse_text("bomb.opml", text)


class PumlRoundTripTest(unittest.TestCase):
    def test_diagram_type_is_read_from_the_header_comment(self):
        original = MindMapContent.model_validate(build(diagram_type="fishbone"))
        result = parse_text("map.puml", to_puml(original))
        self.assertEqual(result.source_format, "puml")
        self.assertEqual(result.content.diagram_type, "fishbone")

    def test_tree_is_equivalent(self):
        original = MindMapContent.model_validate(build())
        result = parse_text("map.puml", to_puml(original)).content
        self.assertEqual(shape(result.root), shape(original.root))

    def test_separator_comment_is_not_read_as_an_edge(self):
        original = MindMapContent.model_validate(build(
            edges=[{"id": "e1", "source": "m1", "target": "t1", "label": "是"}]))
        result = parse_text("map.puml", to_puml(original)).content
        self.assertEqual(len(result.edges), 1)
        self.assertEqual(result.edges[0].label, "是")

    def test_missing_start_marker_is_a_parse_failure(self):
        with self.assertRaises(ParseFailure):
            parse_text("x.puml", "* 根\n** 子\n")


def xmind_zip(sheets: list[dict]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("content.json", json.dumps(sheets, ensure_ascii=False))
        archive.writestr("metadata.json", "{}")
    return buffer.getvalue()


def xmind_sheet(root_topic: dict, title: str = "画布 1") -> dict:
    return {"id": "sheet-1", "class": "sheet", "title": title, "rootTopic": root_topic}


class XmindTest(unittest.TestCase):
    def test_new_version_content_json(self):
        payload = xmind_zip([xmind_sheet({
            "id": "r", "title": "中心主题",
            "children": {"attached": [
                {"id": "a", "title": "分支甲", "children": {"attached": [
                    {"id": "a1", "title": "子甲一"}]}},
                {"id": "b", "title": "分支乙"},
            ]},
        })])
        result = parse_import("测试.xmind", payload)
        self.assertEqual(result.source_format, "xmind")
        self.assertEqual(shape(result.content.root),
                         ("中心主题", [("分支甲", [("子甲一", [])]), ("分支乙", [])]))
        self.assertEqual(result.content.title, "中心主题")

    def test_notes_style_markers_and_structure_class(self):
        payload = xmind_zip([xmind_sheet({
            "id": "r", "title": "根",
            "structureClass": "org.xmind.ui.logic.right",
            "children": {"attached": [
                {"id": "a", "title": "甲",
                 "notes": {"plain": {"content": "甲备注"}},
                 "markers": [{"markerId": "star-red"}],
                 "style": {"properties": {"svg:fill": "#FF0000"}}},
                {"id": "b", "title": "乙",
                 "style": {"properties": {"svg:fill": "#123456"}}},
            ]},
        }, title="我的画布")])
        result = parse_import("s.xmind", payload).content
        first, second = result.root.children
        self.assertEqual(first.note, "甲备注")
        self.assertEqual(first.color, "red")
        self.assertEqual(first.icon, "⭐")
        # 映射不到调色板的 hex 只留在 style.color（界面不渲染，但保存不丢）
        self.assertIsNone(second.color)
        self.assertEqual(second.style.color, "#123456")
        self.assertEqual(result.layout.value, "logic")

    def test_fishbone_structure_sets_diagram_type(self):
        payload = xmind_zip([xmind_sheet({
            "id": "r", "title": "鱼骨",
            "structureClass": "org.xmind.ui.fishbone.leftHeaded",
            "children": {"attached": [{"id": "a", "title": "人"}]},
        })])
        result = parse_import("f.xmind", payload).content
        self.assertEqual(result.diagram_type, "fishbone")
        self.assertEqual(result.layout.value, "fishbone")

    def test_multiple_sheets_only_the_first_is_imported(self):
        payload = xmind_zip([
            xmind_sheet({"id": "r1", "title": "第一张"}, title="A"),
            xmind_sheet({"id": "r2", "title": "第二张"}, title="B"),
        ])
        result = parse_import("two.xmind", payload)
        self.assertEqual(result.content.root.label, "第一张")
        self.assertIn("format_notice", codes(result.warnings))

    def test_detached_topics_are_dropped_with_a_notice(self):
        payload = xmind_zip([xmind_sheet({
            "id": "r", "title": "根",
            "children": {"attached": [{"id": "a", "title": "甲"}],
                         "detached": [{"id": "d", "title": "游离"}]},
        })])
        result = parse_import("d.xmind", payload)
        self.assertEqual(len(result.content.root.children), 1)
        self.assertIn("format_notice", codes(result.warnings))

    def test_old_version_content_xml(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <xmap-content xmlns="urn:xmind:xmap:xmlns:content:2.0" version="2.0">
          <sheet id="s"><title>旧画布</title>
            <topic id="r" structure-class="org.xmind.ui.org-chart.down">
              <title>旧中心</title>
              <children><topics type="attached">
                <topic id="a"><title>旧分支</title>
                  <notes><plain>旧备注</plain></notes>
                  <marker-refs><marker-ref marker-id="priority-2"/></marker-refs>
                  <children><topics type="attached">
                    <topic id="a1"><title>旧子</title></topic>
                  </topics></children>
                </topic>
              </topics></children>
            </topic>
          </sheet>
        </xmap-content>
        """
        result = parse_text("old.xmind", xml)
        self.assertEqual(result.source_format, "xmind")
        self.assertEqual(shape(result.content.root),
                         ("旧中心", [("旧分支", [("旧子", [])])]))
        self.assertEqual(result.content.root.children[0].note, "旧备注")
        self.assertEqual(result.content.layout.value, "vertical")
        self.assertEqual(result.content.title, "旧画布")

    def test_bare_content_xml_is_accepted(self):
        """用户从 `.xmind` 里解出单个 content.xml 也能导入。"""
        xml = ('<xmap-content xmlns="urn:xmind:xmap:xmlns:content:2.0">'
               '<sheet><topic><title>裸根</title></topic></sheet></xmap-content>')
        self.assertEqual(parse_text("content.xml", xml).content.root.label, "裸根")

    def test_zip_without_xmind_members_is_unsupported(self):
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", "<w:document/>")
        with self.assertRaises(UnsupportedFormat):
            parse_import("fake.xmind", buffer.getvalue())

    def test_broken_zip_is_a_parse_failure(self):
        with self.assertRaises(ParseFailure):
            parse_import("bad.xmind", b"PK\x03\x04" + b"\x00" * 40)


class FreemindTest(unittest.TestCase):
    XML = """<map version="1.0.1">
      <node TEXT="根" COLOR="#00B050" FOLDED="true">
        <node TEXT="子">
          <icon BUILTIN="idea"/>
          <richcontent TYPE="NODE"><html><body><p>子备注</p></body></html></richcontent>
          <node TEXT="孙"/>
        </node>
        <node TEXT="未映射色" COLOR="#7f7f7f">
          <icon BUILTIN="不认识的图标"/>
        </node>
      </node>
    </map>"""

    def test_tree_visuals_and_note(self):
        result = parse_text("map.mm", self.XML)
        self.assertEqual(result.source_format, "freemind")
        self.assertEqual(shape(result.content.root),
                         ("根", [("子", [("孙", [])]), ("未映射色", [])]))
        root = result.content.root
        self.assertEqual(root.color, "green")
        self.assertTrue(root.collapsed)
        child = root.children[0]
        self.assertEqual(child.icon, "💡")
        self.assertEqual(child.note, "子备注")
        # 精确表里没有的色值：不猜，只留在 style.color
        unmapped = root.children[1]
        self.assertIsNone(unmapped.color)
        self.assertIsNone(unmapped.icon)
        self.assertEqual(unmapped.style.color, "#7f7f7f")

    def test_multiple_roots_are_attached_under_first(self):
        xml = '<map><node TEXT="甲"/><node TEXT="乙"><node TEXT="乙一"/></node></map>'
        result = parse_text("multi.mm", xml)
        self.assertEqual(result.content.root.label, "甲")
        self.assertEqual(result.content.root.children[0].label, "乙")
        self.assertEqual(result.content.root.children[0].children[0].label, "乙一")

    def test_missing_text_attribute_gets_a_placeholder(self):
        result = parse_text("blank.mm", '<map><node TEXT="根"><node/></node></map>')
        self.assertEqual(result.content.root.children[0].label, "（无标题）")
        self.assertIn("label_missing", codes(result.warnings))


class LimitTest(unittest.TestCase):
    def test_1001_nodes_are_truncated_by_level_order(self):
        """20 个分支 × 50 个孩子 = 1021 个节点（每个节点都没超 50 子）。"""
        root = {"id": "r", "label": "根", "children": [
            {"id": f"b{i}", "label": f"分支{i}", "children": [
                {"id": f"b{i}-{j}", "label": f"子{i}-{j}"} for j in range(50)]}
            for i in range(20)]}
        result = parse_text("big.json", json.dumps({"root": root}, ensure_ascii=False))
        self.assertEqual(len(result.content.root.children), 20)
        kept = 1 + sum(1 + len(branch.children) for branch in result.content.root.children)
        self.assertEqual(kept, 1000)
        self.assertIn("truncated_nodes", codes(result.warnings))
        # BFS：每个一级分支都留下，被裁掉的是各支最深处的子节点
        self.assertTrue(all(branch.children for branch in result.content.root.children))

    def test_nine_levels_are_cut_at_eight(self):
        level: dict = {"id": "n9", "label": "第9层"}
        for depth in range(8, 0, -1):
            level = {"id": f"n{depth}", "label": f"第{depth}层", "children": [level]}
        result = parse_text("deep.json", json.dumps({"root": level}, ensure_ascii=False))
        self.assertIn("truncated_depth", codes(result.warnings))

        deepest = 0
        stack = [(result.content.root, 1)]
        while stack:
            node, depth = stack.pop()
            deepest = max(deepest, depth)
            stack.extend((child, depth + 1) for child in node.children)
        self.assertEqual(deepest, 8)

    def test_51_children_are_cut_to_50(self):
        root = {"id": "r", "label": "根", "children": [
            {"id": f"k{i}", "label": f"子{i}"} for i in range(51)]}
        result = parse_text("wide.json", json.dumps({"root": root}, ensure_ascii=False))
        self.assertEqual(len(result.content.root.children), 50)
        self.assertIn("truncated_children", codes(result.warnings))

    def test_truncation_never_rejects_the_whole_file(self):
        """题目里那条要求：多几个节点也要给一张图，而不是什么都不给。"""
        root = {"id": "r", "label": "根", "children": [
            {"id": f"k{i}", "label": f"子{i}"} for i in range(3000)]}
        result = parse_text("huge.json", json.dumps({"root": root}, ensure_ascii=False))
        self.assertGreater(len(result.content.root.children), 0)
        self.assertTrue(result.warnings)

    def test_long_labels_are_kept_up_to_200_not_16(self):
        """导入不套用生成侧的 16 字上限——用户手写的长标题不能被截。"""
        long_label = "这是一个用户手写的、明显超过十六个字但仍然完整的节点标题"
        result = parse_text("long.json", json.dumps(
            {"root": {"id": "r", "label": long_label}}, ensure_ascii=False))
        self.assertEqual(result.content.root.label, long_label)
        self.assertNotIn("label_truncated", codes(result.warnings))

    def test_labels_over_200_are_clipped_with_a_warning(self):
        result = parse_text("long.json", json.dumps(
            {"root": {"id": "r", "label": "长" * 260}}, ensure_ascii=False))
        self.assertEqual(len(result.content.root.label), 200)
        self.assertIn("label_truncated", codes(result.warnings))


class SourceRefTest(unittest.TestCase):
    def test_refs_and_usage_are_cleared_with_a_warning(self):
        body = {
            "title": "带引用",
            "source_usage": [{"source_id": "s1", "kind": "text",
                              "filename": None, "char_count": 10}],
            "root": {"id": "r", "label": "根",
                     "source_refs": [{"source_id": "s1", "quote": "abc"}],
                     "children": [{"id": "c", "label": "子",
                                   "source_refs": [{"source_id": "s1",
                                                    "start": 0, "end": 3}]}]},
        }
        result = parse_text("refs.json", json.dumps(body, ensure_ascii=False))
        self.assertEqual(result.content.root.source_refs, [])
        self.assertEqual(result.content.root.children[0].source_refs, [])
        self.assertEqual(result.content.source_usage, [])
        self.assertIn("source_refs_cleared", codes(result.warnings))

    def test_content_still_validates_without_refs(self):
        """清空之后 `validate_tree` 不会因为"引用了没有 source_usage 的来源"而炸。"""
        body = build()
        body["root"]["source_refs"] = [{"source_id": "外部来源", "quote": "x"}]
        self.assertIsNotNone(parse_text("r.json", json.dumps(body)).content)


class IdTest(unittest.TestCase):
    def test_all_ids_are_regenerated(self):
        original = MindMapContent.model_validate(build())
        result = parse_text("map.json", to_json(original)).content
        old_ids = set(names_of(original.root))
        new_ids = set(names_of(result.root))
        self.assertTrue(old_ids.isdisjoint(new_ids))
        self.assertEqual(len(new_ids), len(old_ids))

    def test_edges_follow_the_new_ids(self):
        original = MindMapContent.model_validate(build(
            edges=[{"id": "e1", "source": "m1", "target": "t1", "label": "是"}]))
        result = parse_text("map.json", to_json(original)).content
        edge = result.edges[0]
        node_ids = set(names_of(result.root))
        self.assertIn(edge.source, node_ids)
        self.assertIn(edge.target, node_ids)
        self.assertNotEqual(edge.id, "e1")
        names = names_of(result.root)
        self.assertEqual((names[edge.source], names[edge.target]), ("竞品调研", "架构设计"))

    def test_edge_to_a_cut_node_is_dropped(self):
        """端点被限额裁掉时那条边必须消失：dangling 会让前端白屏（契约硬拒）。"""
        root = {"id": "r", "label": "根", "children": [
            {"id": f"k{i}", "label": f"子{i}"} for i in range(60)]}
        body = {"root": root,
                "edges": [{"id": "e1", "source": "k0", "target": "k59"}]}
        result = parse_text("cut.json", json.dumps(body, ensure_ascii=False))
        self.assertEqual(result.content.edges, [])
        self.assertIn("edges_dropped", codes(result.warnings))

    def test_self_loop_and_duplicate_edges_are_dropped(self):
        body = build()
        body["edges"] = [
            {"id": "e1", "source": "m1", "target": "m1"},
            {"id": "e2", "source": "m1", "target": "t1", "label": "是"},
            {"id": "e3", "source": "m1", "target": "t1", "label": "是"},
        ]
        result = parse_text("loop.json", json.dumps(body, ensure_ascii=False))
        self.assertEqual(len(result.content.edges), 1)
        self.assertIn("edges_dropped", codes(result.warnings))


class ErrorTest(unittest.TestCase):
    def test_unknown_format(self):
        with self.assertRaises(UnsupportedFormat) as caught:
            parse_import("数据.csv", b"a,b\n1,2\n")
        self.assertEqual(caught.exception.code, "unsupported_format")

    def test_broken_json_points_at_the_line(self):
        with self.assertRaises(ParseFailure) as caught:
            parse_import("bad.json", b'{\n  "root": {,\n}')
        self.assertEqual(caught.exception.code, "parse_failed")
        self.assertIn("第 2 行", str(caught.exception))

    def test_broken_xml_points_at_the_line(self):
        with self.assertRaises(ParseFailure) as caught:
            parse_import("bad.opml", b'<opml><body>\n<outline text="x">\n')
        self.assertIn("第", str(caught.exception))

    def test_empty_file(self):
        with self.assertRaises(ParseFailure):
            parse_import("empty.json", b"")

    def test_json_without_root_is_unsupported_not_a_crash(self):
        with self.assertRaises(UnsupportedFormat):
            parse_import("a.json", b'{"hello": "world"}')


class InterfaceTest(unittest.TestCase):
    def test_multipart_returns_content_warnings_and_format(self):
        content = MindMapContent.model_validate(build(
            edges=[{"id": "e1", "source": "m1", "target": "t1", "label": "是"}]))
        payload = to_markdown(content).encode("utf-8")
        with guest_client() as client:
            response = client.post(
                "/api/mindmaps/import",
                files={"file": ("规划.md", payload, "text/markdown")})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["source_format"], "markdown")
        self.assertEqual(body["content"]["title"], "季度规划")
        self.assertEqual(body["warnings"], [])
        self.assertEqual(len(body["content"]["edges"]), 1)

    def test_warnings_have_only_code_and_message(self):
        body = build()
        body["root"]["source_refs"] = [{"source_id": "x", "quote": "y"}]
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        with guest_client() as client:
            response = client.post(
                "/api/mindmaps/import",
                files={"file": ("r.json", payload, "application/json")})
        self.assertEqual(response.status_code, 200)
        warnings = response.json()["warnings"]
        self.assertTrue(warnings)
        self.assertEqual(set(warnings[0]), {"code", "message"})

    def test_unknown_format_returns_400_unsupported(self):
        with guest_client() as client:
            response = client.post(
                "/api/mindmaps/import",
                files={"file": ("a.csv", b"a,b\n1,2\n", "text/csv")})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "unsupported_format")

    def test_broken_file_returns_400_parse_failed(self):
        with guest_client() as client:
            response = client.post(
                "/api/mindmaps/import",
                files={"file": ("a.json", b'{"root": {', "application/json")})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "parse_failed")

    def test_import_does_not_touch_the_database(self):
        """只解析不落库：导入一次之后列表里不该多出记录。"""
        content = MindMapContent.model_validate(build())
        with guest_client() as client:
            before = client.get("/api/mindmaps").json()
            response = client.post(
                "/api/mindmaps/import",
                files={"file": ("m.json", to_json(content).encode(), "application/json")})
            after = client.get("/api/mindmaps").json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(after), len(before))


class HardeningTest(unittest.TestCase):
    """外部文件能带进来的三类"脏"东西：控制字符、压缩炸弹、超长清单。

    这一组不是为了"更严格"，是为了**不在 2C2G 的机器上 OOM、不让终端控制序列
    落到 label 里**。每一条都在 `app/importing/` 里有对应的一道闸。
    """

    def test_control_characters_are_stripped_from_labels(self):
        # NUL 与 ESC（`\x1b[31m` 的前导）都能塞进 JSON 字符串
        body = {"root": {"id": "r", "label": "前\x00后\x1b[31m红"}}
        result = parse_text("ctl.json", json.dumps(body, ensure_ascii=False))
        label = result.content.root.label
        self.assertNotIn("\x00", label)
        self.assertNotIn("\x1b", label)
        # 折成空格而不是删掉：`前 后` 是两个词，不是"前后"一个词
        self.assertEqual(label, "前 后 [31m红")

    def test_zip_member_larger_than_the_limit_is_refused(self):
        """10MB 的 zip 能解出 10GB。必须在读之前看中央目录里的 file_size。"""
        payload = b"a" * (21 * 1024 * 1024)  # 高度可压缩，zip 本身很小
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("content.json", payload)
        self.assertLess(len(buffer.getvalue()), 1024 * 1024)

        with self.assertRaises(ParseFailure) as caught:
            parse_import("bomb.xmind", buffer.getvalue())
        self.assertIn("上限", str(caught.exception))

    def test_markdown_with_too_many_items_stops_reading(self):
        """一份 10MB 的 markdown 能有几十万行 `- x`；解析期就要截住。"""
        text = "# 标题\n\n- 根\n" + "".join(f"  - 项{i}\n" for i in range(5100))
        result = parse_text("huge.md", text)
        self.assertIn("hard_cap", codes(result.warnings))
        # 仍然给出一张能用的图，而不是整份失败
        self.assertEqual(len(result.content.root.children), 50)

    def test_puml_with_too_many_lines_stops_reading(self):
        text = "@startmindmap\n* 根\n" + "".join(f"** 项{i}\n" for i in range(5100))
        text += "@endmindmap\n"
        result = parse_text("huge.puml", text)
        self.assertIn("hard_cap", codes(result.warnings))
        self.assertEqual(len(result.content.root.children), 50)


# ---------------------------------------------------------------- 三种图型


def walk(node):
    """前序遍历一棵子树（父在子前）。"""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def label_maps(content) -> tuple[dict[str, str], dict[str, str]]:
    """`(节点 id → 标题, 阶段 id → 阶段标题)` 两张翻译表。"""
    labels: dict[str, str] = {}
    stages: dict[str, str] = {}
    if isinstance(content, MindMapContent):
        roots = [content.root]
    elif isinstance(content, FishboneContent):
        roots = [bone.root for bone in content.bones]
    else:
        roots = [stage.root for stage in content.stages]
        for stage in content.stages:
            stages[stage.id] = stage.root.label
    for root in roots:
        for node in walk(root):
            labels[node.id] = node.label
    return labels, stages


def canonical(content) -> dict:
    """一份内容压成"与 id 无关"的规范形——"逐字段相等"的可实现定义。

    导入**一定**重新生成 id（`normalize` 的第 ① 条：外部 id 与库里碰撞的后果是
    "引用了另一个节点"这种完全静默的错）。所以"导出 → 导入 → 逐字段相等"只能
    定义成：**除了 id 之外全相等，边的端点按它指向的节点标题比较**。

    去掉的只有三样，每一样都有明确理由：
      - `id`（节点 / 边 / 关系 / 阶段）：重新生成，见上；
      - `source_refs` / `source_usage`：**故意**清空（`normalize` 的第 ⑤ 条），
        单独有用例断言它们确实是空的；
      - 边的 `id` 之外一个字段都不去——`label` / `kind` 都在，因为"判定节点的
        是/否两个出口丢了"正是这条路径最容易犯的错。
    """
    labels, stages = label_maps(content)
    dump = content.model_dump(mode="json")

    def endpoint(value: str) -> str:
        return labels.get(value) or stages.get(value) or value

    def node_dump(node: dict) -> dict:
        out = {k: v for k, v in node.items() if k not in ("id", "source_refs")}
        out["children"] = [node_dump(child) for child in out["children"]]
        return out

    def edge_dump(edge: dict) -> dict:
        return {
            "source": endpoint(edge["source"]),
            "target": endpoint(edge["target"]),
            "label": edge.get("label"),
            "kind": edge.get("kind"),
        }

    out: dict = {
        "title": dump["title"],
        "theme": dump["theme"],
        "relations": [
            {
                "source": endpoint(item["source"]),
                "target": endpoint(item["target"]),
                "type": item["type"],
                "label": item["label"],
                "evidence": item["evidence"],
                "provenance": item["provenance"],
            }
            for item in dump["relations"]
        ],
    }
    if "root" in dump:
        out["shape"] = "tree"
        out["root"] = node_dump(dump["root"])
        out["layout"] = dump["layout"]
        out["diagram_type"] = dump["diagram_type"]
        out["edges"] = [edge_dump(edge) for edge in dump["edges"]]
    elif "bones" in dump:
        out["shape"] = "fishbone"
        out["problem"] = dump["problem"]
        out["bones"] = [
            {
                "category": bone["category"],
                "color": bone["color"],
                "icon": bone["icon"],
                "root": node_dump(bone["root"]),
            }
            for bone in dump["bones"]
        ]
    else:
        out["shape"] = "flowchart"
        out["stages"] = [
            {"root": node_dump(stage["root"]),
             "edges": [edge_dump(edge) for edge in stage["edges"]]}
            for stage in dump["stages"]
        ]
        out["edges"] = [edge_dump(edge) for edge in dump["edges"]]
    return out


def sample_fishbone() -> FishboneContent:
    """一张有代表性的鱼骨图：六根骨里只写三根、带视觉元素、带关系与引用。"""
    return FishboneContent.model_validate({
        "title": "交付延期分析",
        "problem": "为什么三次交付都延期",
        "theme": {"preset": "forest", "direction": "both", "accent": "#1e40af"},
        "source_usage": [{"source_id": "src-1", "kind": "text",
                          "filename": "复盘会.txt", "char_count": 1200}],
        "bones": [
            {"category": "人", "color": "red", "icon": "👥", "root": {
                "id": "b_people", "label": "人", "color": "red", "children": [
                    {"id": "b_people_1", "label": "关键人休假", "note": "九月两周",
                     "detail": "两位主程同时不在", "role": "step", "icon": "🌴",
                     "expandable": "no", "collapsed": True,
                     "source_refs": [{"source_id": "src-1", "start": 0, "end": 4,
                                      "quote": "休假"}]},
                    {"id": "b_people_2", "label": "交接不完整"},
                ]}},
            {"category": "机", "root": {
                "id": "b_machine", "label": "机", "children": [
                    {"id": "b_machine_1", "label": "构建机排队"},
                ]}},
            {"category": "料", "root": {"id": "b_material", "label": "料"}},
        ],
        "relations": [
            {"id": "rel-1", "source": "b_people_2", "target": "b_machine_1",
             "type": "cause", "label": "导致", "evidence": "复盘会第 3 段",
             "provenance": "ai"},
        ],
    })


def sample_flow() -> FlowContent:
    """一张有代表性的流程图：三个阶段、阶段间三种边（含回退）、阶段内判定出边。"""
    return FlowContent.model_validate({
        "title": "报销流程",
        "theme": {"preset": "slate"},
        "stages": [
            {"id": "s_1", "root": {
                "id": "n_submit", "label": "提交", "role": "start", "children": [
                    {"id": "n_fill", "label": "填单", "role": "step"},
                    {"id": "n_attach", "label": "上传发票", "icon": "🧾"},
                ]}},
            {"id": "s_2", "root": {
                "id": "n_review", "label": "审查", "role": "decision", "children": [
                    {"id": "n_form", "label": "形式审查"},
                    {"id": "n_substance", "label": "实质审查",
                     "color": "amber", "expandable": "yes"},
                ]},
             "edges": [
                 {"id": "in-1", "source": "n_review", "target": "n_form",
                  "label": "是", "kind": "branch"},
                 {"id": "in-2", "source": "n_review", "target": "n_substance",
                  "label": "否", "kind": "branch"},
             ]},
            {"id": "s_3", "root": {"id": "n_pay", "label": "付款", "role": "end"}},
        ],
        "edges": [
            {"id": "out-1", "source": "s_1", "target": "s_2", "kind": "flow"},
            {"id": "out-2", "source": "s_2", "target": "s_3", "label": "通过",
             "kind": "branch"},
            {"id": "out-3", "source": "s_3", "target": "s_1", "label": "驳回",
             "kind": "loop"},
        ],
        "relations": [
            {"id": "rel-f", "source": "n_attach", "target": "n_pay",
             "type": "depends", "label": "凭证"},
        ],
    })


class ThreeShapeRoundTripTest(unittest.TestCase):
    """"导出 → 导入"在**三种图型**上都成立：除 id 与被清空的引用外逐字段相等。

    这是这次改动的核心判据。它钉住的是一件很容易蒙混过关的事：一份鱼骨图导回来
    如果变成树，`content.title` 还在、节点文字也都在，**看起来导入成功了**——只有
    `bones` / `problem` 没了。所以这三条用例比的不是"文字还在不在"，是完整 dump。
    """

    def roundtrip(self, content, name: str = "map.json"):
        result = parse_text(name, to_json(content))
        self.assertEqual(result.source_format, "json")
        return result.content

    def test_tree_roundtrip_is_field_by_field(self):
        original = MindMapContent.model_validate(build(
            diagram_type="flowchart",
            theme={"preset": "slate", "direction": "left", "font": "serif"},
            edges=[{"id": "e1", "source": "m1", "target": "t1", "label": "是",
                    "kind": "branch"}],
            relations=[{"id": "r1", "source": "m1", "target": "t1", "type": "ref",
                        "label": "参见", "provenance": "user"}],
            root={
                "id": "root", "label": "产品", "note": "根备注", "detail": "根详情",
                "color": "blue", "icon": "🎯", "role": "start",
                "children": [
                    {"id": "m", "label": "市场", "color": "green", "icon": "🚀",
                     "collapsed": True, "expandable": "no",
                     "style": {"bold": True, "shape": "pill", "color": "#112233"},
                     "children": [{"id": "m1", "label": "竞品调研"}]},
                    {"id": "t", "label": "技术", "children": [{"id": "t1", "label": "架构设计"}]},
                ],
            },
        ))
        restored = self.roundtrip(original)
        self.assertEqual(canonical(restored), canonical(original))
        self.assertEqual(restored.source_usage, [])
        self.assertEqual(restored.root.source_refs, [])

    def test_fishbone_roundtrip_is_field_by_field(self):
        original = sample_fishbone()
        restored = self.roundtrip(original, "fishbone.json")
        self.assertIsInstance(restored, FishboneContent)
        self.assertEqual(canonical(restored), canonical(original))

    def test_flow_roundtrip_is_field_by_field(self):
        original = sample_flow()
        restored = self.roundtrip(original, "flow.json")
        self.assertIsInstance(restored, FlowContent)
        self.assertEqual(canonical(restored), canonical(original))

    def test_fishbone_keeps_bones_problem_and_visuals(self):
        original = sample_fishbone()
        restored = self.roundtrip(original, "fishbone.json")
        self.assertEqual(restored.problem, original.problem)
        self.assertEqual([bone.category for bone in restored.bones], ["人", "机", "料"])
        self.assertEqual(restored.bones[0].color, "red")
        self.assertEqual(restored.bones[0].icon, "👥")
        # 骨下面的原因一条不少，视觉字段也都在
        causes = restored.bones[0].root.children
        self.assertEqual([cause.label for cause in causes], ["关键人休假", "交接不完整"])
        self.assertEqual(causes[0].icon, "🌴")
        self.assertEqual(causes[0].note, "九月两周")
        self.assertEqual(causes[0].role, "step")
        self.assertEqual(causes[0].expandable, "no")
        self.assertTrue(causes[0].collapsed)

    def test_flow_keeps_stages_both_edge_layers_and_roles(self):
        """流程图的四个关键物：阶段、阶段间边、阶段内边、`role`。

        `role` 是「判定节点画菱形」的唯一依据；`stages[i].edges` 是「是 / 否」两个
        出口的唯一位置。少了任何一个，图能打开、能保存，但画出来已经不是流程图。
        """
        restored = self.roundtrip(sample_flow(), "flow.json")
        self.assertEqual([stage.root.label for stage in restored.stages],
                         ["提交", "审查", "付款"])
        # 阶段间：三条，含一条回退
        self.assertEqual(len(restored.edges), 3)
        kinds = {edge.kind for edge in restored.edges}
        self.assertEqual(kinds, {"flow", "branch", "loop"})
        # 阶段内：两条判定出边，端点在**这个阶段自己**的节点里
        review = restored.stages[1]
        self.assertEqual(len(review.edges), 2)
        local_ids = {node.id for node in walk(review.root)}
        for edge in review.edges:
            self.assertIn(edge.source, local_ids)
            self.assertIn(edge.target, local_ids)
        self.assertEqual({edge.label for edge in review.edges}, {"是", "否"})
        # role
        self.assertEqual(restored.stages[0].root.role, "start")
        self.assertEqual(restored.stages[1].root.role, "decision")
        self.assertEqual(restored.stages[2].root.role, "end")
        self.assertIsNone(restored.stages[0].root.children[1].role)

    def test_relations_keep_their_provenance(self):
        """`provenance` 是**内容**（"这条关系是谁连的"），不是导入元数据。

        以前一律盖成 `import`：对外部格式是对的（它们没有这个字段），对自家 JSON
        就是丢字段——而"导出再导入必须一样"正是这条路存在的理由。
        """
        restored = self.roundtrip(sample_fishbone(), "fishbone.json")
        self.assertEqual([item.provenance for item in restored.relations], ["ai"])

    def test_ids_are_regenerated_in_all_three_shapes(self):
        for original in (sample_fishbone(), sample_flow()):
            restored = self.roundtrip(original)
            before = set(label_maps(original)[0]) | set(label_maps(original)[1])
            after = set(label_maps(restored)[0]) | set(label_maps(restored)[1])
            self.assertEqual(before & after, set(), "导出的 id 不该原样出现在导入结果里")

    def test_refs_are_cleared_in_all_three_shapes(self):
        """三种形状都要清引用。鱼骨这份样例带着 1 处引用 + 1 条来源摘要。"""
        result = parse_text("x.json", to_json(sample_fishbone()))
        self.assertEqual(result.content.source_usage, [])
        for node in _all_nodes(result.content):
            self.assertEqual(node.source_refs, [])
        self.assertIn("source_refs_cleared", codes(result.warnings))

    def test_flow_without_refs_reports_no_ref_warning(self):
        """没有引用的图不该出现"引用已清空"——那条告警是给"确实清过东西"的图。"""
        self.assertNotIn("source_refs_cleared",
                         codes(parse_text("x.json", to_json(sample_flow())).warnings))


def _all_nodes(content):
    if isinstance(content, MindMapContent):
        roots = [content.root]
    elif isinstance(content, FishboneContent):
        roots = [bone.root for bone in content.bones]
    else:
        roots = [stage.root for stage in content.stages]
    for root in roots:
        yield from walk(root)


class ShapeJudgeTest(unittest.TestCase):
    """判据：**形状只看文件自述**——JSON 看顶层键，markdown 看文件头的标记；
    其余格式（opml / puml / xmind / freemind）只产树。

    早先这里是"只有 JSON 认完整结构，markdown 一律退化成树"，而那正是 P1-③ 那个
    缺陷的**成因**：`markdown.py::parse` 只把第一个 `#` 当标题，后面的 heading 一律
    `continue`，于是流程图的 `### 1. 提交` 没有落点——阶段名整段消失、边清单那几行
    反倒成了节点。现在多级 heading 是容器、形状由标记声明，两条路都能如实还原。
    """

    def test_flow_markdown_keeps_shape_and_stages(self):
        """流程图 → markdown → 导入：**还是流程图**，阶段名与两层边都在。

        这条用例以前钉的是"变回树、阶段名丢失"。它红了是好事——那说明修复生效了。
        现在钉的是修复之后的具体数字：三个阶段、两条阶段间边（含一条回退）、
        两条阶段内的判定出边。少一个就说明某一层又掉回 infer-nothing 的老路。
        """
        restored = parse_text("flow.md", to_markdown(sample_flow())).content
        self.assertIsInstance(restored, FlowContent)
        self.assertEqual(restored.title, "报销流程")
        self.assertEqual([stage.root.label for stage in restored.stages],
                         ["提交", "审查", "付款"])
        # 阶段之间：三条。端点与文字都回来了，**`kind` 只在那条没有文字的边上
        # 回来**（导出写 `（顺序）`，导入把它读成 kind=flow）；另外两条的槽被
        # `通过` / `驳回` 占着，kind 就落不下来。这是 markdown 只有一个边后缀槽的
        # 既有取舍（见 `export.service._edge_suffix` 与 `EDGE_KIND_LABELS` 的注释），
        # 不是这次修复引入的——树的边清单一直是同一个行为
        # （`MarkdownRoundTripTest.test_edges_are_read_back` 钉着）。
        self.assertEqual(len(restored.edges), 3)
        self.assertEqual({(edge.label, edge.kind) for edge in restored.edges},
                         {("顺序", "flow"), ("通过", None), ("驳回", None)})
        # 阶段内部：两条判定出边，落在**审查**这个阶段自己的节点上
        review = restored.stages[1]
        self.assertEqual(len(review.edges), 2)
        self.assertEqual({edge.label for edge in review.edges}, {"是", "否"})
        own_ids = {node.id for node in walk(review.root)}
        for edge in review.edges:
            self.assertIn(edge.source, own_ids)
            self.assertIn(edge.target, own_ids)
        self.assertEqual({node.label for node in walk(review.root)},
                         {"审查", "形式审查", "实质审查"})

    def test_fishbone_markdown_keeps_bones_and_problem(self):
        """鱼骨图 → markdown → 导入：骨头（类别）与主骨上那句话都回来。

        骨与骨下面的原因在 markdown 里只差一级缩进，所以导出必须把"谁挂在谁下面"
        写对（`  - 人` / `    - 关键人休假`）。写错时读起来是"七个并列要点"，
        导回来则是"主骨下面挂着原因、没有骨"——两张图都叫鱼骨，形状全错。
        """
        restored = parse_text("fishbone.md", to_markdown(sample_fishbone())).content
        self.assertIsInstance(restored, FishboneContent)
        self.assertEqual(restored.problem, "为什么三次交付都延期")
        self.assertEqual([bone.category for bone in restored.bones], ["人", "机", "料"])
        self.assertEqual([cause.label for cause in restored.bones[0].root.children],
                         ["九月两周", "交接不完整"])

    def test_markdown_without_marker_is_still_a_tree(self):
        """没标记 = 树。**这是兼容性那一半**：手写大纲、老文件、被人删掉标记的文件
        都走这一条，行为与加标记之前逐字段相同。"""
        restored = parse_text("plain.md", to_markdown(
            MindMapContent.model_validate(build()))).content
        self.assertIsInstance(restored, MindMapContent)

    def test_container_headings_keep_their_names_without_the_marker(self):
        """没有标记时多级 heading 也是容器——**容器名不再整段丢失**。

        这是 codex 报的 P1-③ 的最小形态：删掉标记之后，`### 1. 提交` 仍然要落成
        一个节点（名字带序号，因为这时没有"这是阶段"的依据，序号就是文字的一部分）。
        形状退化成树是**这条路的已知损失**，名字丢掉不是。
        """
        text = to_markdown(sample_flow()).replace(
            "<!-- genai-mindmap: shape=flowchart -->\n", "")
        restored = parse_text("flow.md", text).content
        self.assertIsInstance(restored, MindMapContent)
        labels = {node.label for node in _all_nodes(restored)}
        self.assertIn("1. 提交", labels)
        self.assertIn("2. 审查", labels)
        self.assertIn("填单", labels)

    def test_flow_markdown_without_stage_headings_degrades_with_a_notice(self):
        """标着流程图却一个阶段标题都没有：按树读，并**说出降级**。

        静默换一种形状比降级本身更难排查——用户会把"我的流程变成了导图"当成丢数据。
        """
        result = parse_text("odd.md", "# 标题\n<!-- genai-mindmap: shape=flowchart -->\n\n- 甲\n- 乙\n")
        self.assertIsInstance(result.content, MindMapContent)
        self.assertIn("format_notice", codes(result.warnings))

    def test_empty_bones_is_a_legal_fishbone(self):
        """只有主骨的草稿是合法的：`bones: []` 不该被当成"不是鱼骨图"。"""
        content = FishboneContent.model_validate(
            {"title": "草稿", "problem": "为什么慢", "bones": []})
        restored = parse_text("draft.json", to_json(content)).content
        self.assertIsInstance(restored, FishboneContent)
        self.assertEqual(restored.bones, [])

    def test_problem_falls_back_to_title_when_missing(self):
        result = parse_text("noproblem.json", json.dumps(
            {"title": "为什么慢", "bones": []}, ensure_ascii=False))
        self.assertEqual(result.content.problem, "为什么慢")
        self.assertIn("fields_dropped", codes(result.warnings))

    def test_duplicate_bone_categories_get_numbers(self):
        """骨的类别必须唯一（契约硬拒）。重复的加序号，而不是丢掉那根骨。"""
        body = {
            "problem": "为什么慢",
            "bones": [
                {"category": "人", "root": {"label": "人"}},
                {"category": "人", "root": {"label": "人也算", "children": [
                    {"label": "第二个原因"}]}},
            ],
        }
        result = parse_text("dup.json", json.dumps(body, ensure_ascii=False))
        self.assertEqual([bone.category for bone in result.content.bones],
                         ["人", "人（2）"])
        self.assertEqual(len(result.content.bones[1].root.children), 1)
        self.assertIn("category_duplicate", codes(result.warnings))

    def test_too_many_bones_are_truncated_with_a_warning(self):
        bones = [{"category": f"类{i}", "root": {"label": f"骨{i}"}} for i in range(8)]
        result = parse_text("bones.json", json.dumps(
            {"problem": "为什么慢", "bones": bones}, ensure_ascii=False))
        self.assertEqual(len(result.content.bones), 6)
        self.assertIn("truncated_bones", codes(result.warnings))

    def test_empty_stages_is_unsupported(self):
        with self.assertRaises(UnsupportedFormat):
            parse_text("empty.json", json.dumps({"stages": []}, ensure_ascii=False))

    def test_stage_inner_edge_to_another_stage_is_dropped(self):
        """一条跨阶段的"阶段内边"必须丢掉：契约会硬拒整份图，而那是 400 不是降级。"""
        body = {
            "title": "流程",
            "stages": [
                {"id": "s1", "root": {"id": "a", "label": "甲"}},
                {"id": "s2", "root": {"id": "b", "label": "乙"},
                 "edges": [{"id": "bad", "source": "b", "target": "a"}]},
            ],
        }
        result = parse_text("cross.json", json.dumps(body, ensure_ascii=False))
        self.assertEqual(result.content.stages[1].edges, [])
        self.assertIn("edges_dropped", codes(result.warnings))


class RoundTripLossTest(unittest.TestCase):
    """codex 复审第 2–5 项：几条"导出写了、导入读不回来"的路。

    每一条都是**用户给的文件往返一次之后少了一点东西**，而其中有几条当时是
    静默的（不报警）。判据统一是"从本仓库导出 → 导入 → 字段相等"。
    """

    # --- P2-2：骨的 category 与骨根 note --------------------------------

    def test_bone_category_and_note_survive_markdown(self):
        """`category=People` / `label=Staff` / 另有 `note`：三个字段各归各位。

        此前文字槽里只有 `label`，于是 `category` 退回 `Staff`、`note` 整段消失，
        **而且没有任何告警**——用户不会知道正文去哪了。
        """
        original = FishboneContent.model_validate({
            "title": "交付延期", "problem": "为什么三次交付都延期",
            "bones": [{"category": "People", "root": {
                "id": "b1", "label": "Staff", "note": "两位主程同时不在",
                "detail": "骨的生成说明",
                "children": [{"id": "c1", "label": "休假"}]}}],
        })
        restored = parse_text("fb.md", to_markdown(original)).content
        self.assertIsInstance(restored, FishboneContent)
        bone = restored.bones[0]
        self.assertEqual(bone.category, "People")
        self.assertEqual(bone.root.label, "Staff")
        self.assertEqual(bone.root.note, "两位主程同时不在")
        self.assertEqual(bone.root.detail, "骨的生成说明")

    def test_bone_without_extra_fields_still_takes_category_from_the_line(self):
        """没有注释行的文件（手写大纲、上一版导出）行为不变：类别取骨行那行字。"""
        restored = parse_text("old.md", to_markdown(sample_fishbone())).content
        self.assertEqual([bone.category for bone in restored.bones], ["人", "机", "料"])


    # --- P2-3：边标签里的括号 ------------------------------------------

    def test_edge_label_with_parentheses_keeps_the_whole_edge(self):
        """`Rejected (missing file)`：标签里的括号不再让**整条边**丢失。

        此前后缀正则不许括号套括号，标签认不出来就留在端点上——端点对不上任何
        节点，整条边被丢掉（边数 1→0 + `edges_dropped`）。
        """
        original = FlowContent.model_validate({
            "title": "报销流程",
            "stages": [{"id": "s1", "root": {"id": "n1", "label": "提交"}},
                       {"id": "s2", "root": {"id": "n2", "label": "审查"}}],
            "edges": [{"id": "e1", "source": "s1", "target": "s2",
                       "label": "Rejected (missing file)"}],
        })
        result = parse_text("flow.md", to_markdown(original))
        self.assertIsInstance(result.content, FlowContent)
        self.assertEqual(len(result.content.edges), 1)
        edge = result.content.edges[0]
        self.assertEqual(edge.label, "Rejected (missing file)")
        _labels, stages = label_maps(result.content)
        self.assertEqual((stages[edge.source], stages[edge.target]), ("提交", "审查"))
        self.assertNotIn("edges_dropped", codes(result.warnings))

    def test_stage_inner_edge_label_with_parentheses_survives(self):
        original = FlowContent.model_validate({
            "title": "流程",
            "stages": [{"id": "s1", "root": {
                "id": "n1", "label": "审查",
                "children": [{"id": "n11", "label": "形式审查"},
                             {"id": "n12", "label": "实质审查"}]},
                "edges": [{"id": "i1", "source": "n11", "target": "n12",
                           "label": "Rejected (missing file)"}]}],
        })
        restored = parse_text("inner.md", to_markdown(original)).content
        self.assertEqual(len(restored.stages[0].edges), 1)
        self.assertEqual(restored.stages[0].edges[0].label, "Rejected (missing file)")

    def test_nested_full_width_parentheses_survive(self):
        original = MindMapContent.model_validate(build(
            edges=[{"id": "e1", "source": "m1", "target": "t1",
                    "label": "驳回（材料不全）"}]))
        restored = parse_text("nested.md", to_markdown(original)).content
        self.assertEqual(len(restored.edges), 1)
        self.assertEqual(restored.edges[0].label, "驳回（材料不全）")


    # --- P2-4：HTML 实体守恒 --------------------------------------------

    def test_html_entities_written_by_the_user_are_conserved(self):
        """用户正文里写着的 `&lt;token&gt;` 往返一次还是那六个字符。

        此前导出只转 `<`/`>`，不动 `&`；导入把 `&lt;` 还原成 `<`——用户写的字被
        改了，而且没有告警。现在导出先转 `&`，导入按相反顺序还原。
        """
        original = MindMapContent.model_validate({
            "title": "实体", "root": {
                "id": "r", "label": "根",
                "detail": "原文 &lt;token&gt; 与 &amp; 与 a<b",
                "children": [{"id": "c", "label": "子", "detail": "&lt;div&gt;"}]},
        })
        restored = parse_text("ent.md", to_markdown(original)).content
        self.assertEqual(restored.root.detail, "原文 &lt;token&gt; 与 &amp; 与 a<b")
        self.assertEqual(restored.root.children[0].detail, "&lt;div&gt;")

    def test_escaped_angle_brackets_still_come_back_as_brackets(self):
        """另一半没有变：导出为 `<` 写的 `&lt;` 仍然还原成 `<`。"""
        original = MindMapContent.model_validate({
            "title": "泛型", "root": {"id": "r", "label": "Map<K,V>", "children": []}})
        restored = parse_text("g.md", to_markdown(original)).content
        self.assertEqual(restored.root.label, "Map<K,V>")

    def test_entity_in_an_edge_label_survives(self):
        original = MindMapContent.model_validate(build(
            edges=[{"id": "e1", "source": "m1", "target": "t1",
                    "label": "a<b & c"}]))
        restored = parse_text("edge-ent.md", to_markdown(original)).content
        self.assertEqual(restored.edges[0].label, "a<b & c")


    # --- P3-5：OPML / PlantUML 的 detail --------------------------------

    def test_opml_note_attribute_comes_back_as_detail(self):
        """`_note` 是导出写的第二通道，导入侧读它（此前只写不读）。"""
        original = MindMapContent.model_validate({
            "title": "报销", "root": {
                "id": "r", "label": "提交申请", "detail": "把单据拍照上传。",
                "children": [{"id": "c", "label": "填单", "detail": "写清事由。"}]},
        })
        restored = parse_text("d.opml", to_opml(original)).content
        self.assertEqual(restored.root.detail, "把单据拍照上传。")
        self.assertEqual(restored.root.children[0].detail, "写清事由。")

    def test_puml_note_comment_comes_back_as_detail(self):
        original = MindMapContent.model_validate({
            "title": "报销", "root": {
                "id": "r", "label": "提交申请", "detail": "把单据拍照上传。",
                "children": [{"id": "c", "label": "填单", "detail": "写清事由。"}]},
        })
        restored = parse_text("d.puml", to_puml(original)).content
        self.assertEqual(restored.root.detail, "把单据拍照上传。")
        self.assertEqual(restored.root.children[0].detail, "写清事由。")

    def test_puml_note_does_not_grow_an_edge(self):
        """`' 说明：…` 里的箭头一旦被 `_EDGE` 抢先读成边，就凭空多出一条关系。

        导出侧已经把 `-->` 压成 `→`（`export._puml_note`，单向替换），这条钉住
        导入侧读回说明时**不长边**、并且如实留下压过的文字。
        """
        original = MindMapContent.model_validate({
            "title": "t", "root": {"id": "r", "label": "根",
                                   "detail": "先 A --> B 再回 C", "children": []}})
        restored = parse_text("d.puml", to_puml(original)).content
        self.assertEqual(restored.edges, [])
        self.assertEqual(restored.root.detail, "先 A → B 再回 C")


class FlowMarkdownTest(unittest.TestCase):
    """流程图 markdown 的两个细节：阶段根的**正文**、阶段内边的端点归属。"""

    def flow(self):
        return FlowContent.model_validate({
            "title": "报销流程",
            "stages": [
                {"id": "s_1", "root": {
                    "id": "n1", "label": "审查", "note": "查单据与金额",
                    "children": [{"id": "n11", "label": "形式审查"}]},
                 "edges": [{"id": "i1", "source": "n1", "target": "n11",
                            "label": "是", "kind": "branch"}]},
            ],
        })

    def test_stage_root_note_is_restored_from_the_heading(self):
        """`### 2. 审查｜查单据与金额`：竖线左边是标题，右边是正文。

        不留这一手的话，阶段根只剩「审查」，而生成出来的那句"查单据与金额"——画布上
        真正显示的那一行——在文件里和最外层标题里都还是它，导入却没了。
        """
        restored = parse_text("f.md", to_markdown(self.flow())).content
        self.assertEqual(restored.stages[0].root.label, "审查")
        self.assertEqual(restored.stages[0].root.note, "查单据与金额")

    def test_inner_edge_endpoint_on_the_stage_root_still_resolves(self):
        """阶段内边的端点写的是**正文优先**的那个名字（`查单据与金额`），而导入之后
        阶段根的标题是 `审查`。这一层不换回来，那条边会以"端点找不到"的名义丢掉——
        图能打开、看着正常，只是"是/否"其中一个出口没了。"""
        result = parse_text("f.md", to_markdown(self.flow()))
        self.assertEqual(len(result.content.stages[0].edges), 1)
        self.assertNotIn("edges_dropped", codes(result.warnings))

    def test_unmatched_inner_edge_is_reported_not_silently_dropped(self):
        """行首的 `[阶段名]` 对不上任何阶段：丢掉，但说出来。"""
        text = ("# t\n<!-- genai-mindmap: shape=flowchart -->\n\n"
                "### 1. 甲\n\n- 乙\n\n"
                "## 附加关系（阶段内部）\n\n- [9. 不存在] 丙 → 丁（是）\n")
        result = parse_text("x.md", text)
        self.assertIn("format_notice", codes(result.warnings))


class ContainerBfsTest(unittest.TestCase):
    """容器（骨 / 阶段）的 BFS：**轮转**而不是"第一根截满、其余全灭"。

    判据不是"实现了某个算法"，而是用户能看到什么：一张鱼骨图少了五根骨，就不再是
    鱼骨图；一张流程少了两个阶段，流程就断了。而"每根骨少几个最深处的原因"是
    BFS 在树上一贯的取舍口径（"没读到这里"，不是"这里没有了"）。
    """

    def fishbone_with(self, causes: int, leaves: int) -> dict:
        return {
            "title": "超限鱼骨",
            "problem": "为什么慢",
            "bones": [
                {"category": f"类{index}", "root": {
                    "label": f"骨{index}",
                    "children": [
                        {"label": f"骨{index}原因{cause}", "children": [
                            {"label": f"骨{index}原因{cause}细节{leaf}"}
                            for leaf in range(leaves)
                        ]}
                        for cause in range(causes)
                    ],
                }}
                for index in range(6)
            ],
        }

    def test_every_bone_keeps_its_content_when_over_the_node_limit(self):
        """6 根骨 × 50 原因 × 5 细节 = 1806 个节点，超过 1000 的上限。

        层序保留的结果：第 1 层六根骨全在，第 2 层每根骨的 50 个原因全在（共 300），
        第 3 层的额度按容器顺序分配。**关键的断言是"后三根骨的第 1、2 层也在"**——
        逐个容器截满时它们一个节点都不会剩下。
        """
        result = parse_text("huge.json", json.dumps(
            self.fishbone_with(50, 5), ensure_ascii=False))
        bones = result.content.bones
        self.assertEqual(len(bones), 6, "六根骨必须都在")
        for bone in bones:
            self.assertEqual(len(bone.root.children), 50,
                             f"{bone.category} 的原因被整段砍掉了")
        # 前几根骨的第 3 层有内容，后几根没有——这是"层序"而不是"平均分"
        self.assertGreater(len(bones[0].root.children[0].children), 0)
        self.assertEqual(len(bones[5].root.children[0].children), 0)
        self.assertIn("truncated_nodes", codes(result.warnings))

    def test_every_stage_keeps_its_steps_when_over_the_node_limit(self):
        """3 个阶段 × 40 步 × 10 细节 = 1323 个节点。三个阶段都必须还在。"""
        stages = [
            {"id": f"s{index}", "root": {
                "label": f"阶段{index}",
                "children": [
                    {"label": f"阶段{index}步{step}", "children": [
                        {"label": f"阶段{index}步{step}细节{leaf}"} for leaf in range(10)
                    ]}
                    for step in range(40)
                ],
            }}
            for index in range(3)
        ]
        result = parse_text("huge.json", json.dumps(
            {"title": "超限流程", "stages": stages}, ensure_ascii=False))
        self.assertEqual(len(result.content.stages), 3, "三个阶段必须都在")
        for stage in result.content.stages:
            self.assertEqual(len(stage.root.children), 40,
                             f"{stage.root.label} 的步骤被整段砍掉了")
        self.assertIn("truncated_nodes", codes(result.warnings))

    def test_depth_limit_is_counted_per_container(self):
        """深度按**每棵子树**从 1 数（契约 `_scan_tree` 同口径），不是从虚拟根数。"""
        deep = {"label": "层1", "children": []}
        cursor = deep
        for level in range(2, 10):
            child = {"label": f"层{level}", "children": []}
            cursor["children"].append(child)
            cursor = child
        body = {"problem": "为什么慢", "bones": [
            {"category": "人", "root": deep},
            {"category": "机", "root": {"label": "机", "children": [
                {"label": "机原因", "children": [{"label": "机原因细节"}]}]}},
        ]}
        result = parse_text("deep.json", json.dumps(body, ensure_ascii=False))
        self.assertIn("truncated_depth", codes(result.warnings))
        # 另一根骨的内容不受影响：深度是各自的
        self.assertEqual(len(result.content.bones[1].root.children), 1)


if __name__ == "__main__":
    unittest.main()
