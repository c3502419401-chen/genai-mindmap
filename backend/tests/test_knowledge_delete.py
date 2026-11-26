"""删材料（`library.forget`）的产品入口：`DELETE /api/knowledge/library/{id}`。

为什么单独一个文件：`tests/test_library.py` 钉的是级联删块（`forget` 本身早就测过），
缺的一直是**用户能不能删**。这条链上的三个决定必须在这里钉住，而不是只写在 docstring 里：

  1. 未被引用的材料直接删——这是"传错了 / 重复了"的绝大多数；
  2. 被导图引用的材料先拦一次（409 + `detail.cited_by`）。删掉之后那些节点的引用是
     `SOURCE_TEXT_UNAVAILABLE`，而这次退回不可逆：正文没了，谁也没法再回填；
  3. `force=true` 之后是硬删，而导图里的 `source_refs` **原样保留**（值对象、没有
     外键）——"引用还在、原文不可用"才是产品真实的样子，用一张真存的图证明它。

全部离线：`conftest.py` 把数据库落到本次用例的 `tmp_path`，知识库与导图表都是每个
用例一份空的。
"""
from __future__ import annotations

from conftest import guest_client

from app.contracts.mindmap import MindMapContent, Node
from app.contracts.source import Source, SourceKind, SourceRef, SourceUsage
from app.knowledge import library, shelf
from app.mindmaps import repository


def _ingest(text: str = "预算审批由部门负责人签核，超过额度的部分上报总经理复审。",
            *, filename: str = "制度.md") -> str:
    """把一份材料真的写进库，返回持久 id（与生成链路走同一条 `library.ingest`）。"""
    return library.ingest(Source(kind=SourceKind.text, status="parsed", text=text,
                                 filename=filename))


def _cite(source_id: str, *, label: str = "预算审批") -> str:
    """存一张引用这份材料的图，返回图 id。

    走 `repository.create_map` 而不是自己拼 SQL：`cited_by` 的统计口径就是"扫已保存
    导图的 `source_usage` 与节点的 `source_refs`"，用别的写法造数据等于测一个产品里
    不存在的形状。
    """
    content = MindMapContent(
        title="制度",
        # `source_usage` 必须带上：契约要求"节点引用的来源在 source_usage 里"，
        # 而 `cited_by` 的统计把两者都算进去——这正是要测的口径。
        source_usage=[SourceUsage(source_id=source_id, kind=SourceKind.text,
                                  filename="制度.md", char_count=1)],
        root=Node(id="root", label="制度", children=[
            Node(id="n1", label=label,
                 source_refs=[SourceRef(source_id=source_id, start=0, end=4, quote="预算审批")]),
        ]),
    )
    return repository.create_map(content).id


def test_delete_unreferenced_material_removes_it_from_the_list():
    """没被任何导图引用 → 直接删，回执里 `cited_by` 是 0。"""
    source_id = _ingest()
    api = guest_client()

    assert api.get("/api/knowledge/library").json()["total"] == 1
    with api:
        response = api.delete(f"/api/knowledge/library/{source_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"source_id": source_id, "deleted": True, "cited_by": 0}
    assert library.load(source_id) is None
    assert api.get("/api/knowledge/library").json()["total"] == 0


def test_delete_cited_material_is_refused_once_with_the_count():
    """被引用 → 409 且带 `cited_by`；材料**还在**，因为用户还没确认。"""
    source_id = _ingest()
    _cite(source_id)
    api = guest_client()

    with api:
        response = api.delete(f"/api/knowledge/library/{source_id}")

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "source_cited"
    assert body["detail"] == {"cited_by": 1}
    # 关键的一半：拦下来之后材料必须还在。删了才发现"只是警告"就没法补救了。
    assert library.load(source_id) is not None
    assert api.get("/api/knowledge/library").json()["total"] == 1


def test_force_delete_keeps_the_mindmap_reference_but_loses_the_text():
    """`force=true` 硬删：材料没了，而图里的 `source_refs` 一条不少——引用还在。

    这条就是"删了但引用还在"的表达方式在产品里的样子：图不会坏、节点不会少，只是
    那条引用再也查不到原文（`SOURCE_TEXT_UNAVAILABLE`）。
    """
    source_id = _ingest()
    map_id = _cite(source_id)
    api = guest_client()

    with api:
        response = api.delete(f"/api/knowledge/library/{source_id}?force=true")

    assert response.status_code == 200, response.text
    assert response.json()["cited_by"] == 1
    assert library.load(source_id) is None

    # 图还在、引用还在：删材料不碰导图表，这是"不需要 schema 迁移"的全部原因。
    record = repository.get_map(map_id)
    assert record is not None
    refs = record.content.root.children[0].source_refs
    assert [ref.source_id for ref in refs] == [source_id]
    # 而原文确实没了——引用指向的地方现在只能报"来源原文不可用"。
    assert api.get(f"/api/knowledge/library/{source_id}").status_code == 404


def test_delete_missing_material_is_a_404_not_a_silent_success():
    """删不存在的东西：404。回 200 会让界面说"删掉了"，而用户下次刷新才发现它还在。"""
    api = guest_client()
    with api:
        response = api.delete("/api/knowledge/library/no-such-source")
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"


def test_shelf_delete_reports_the_same_count_the_list_shows():
    """列表里的 `cited_by` 与删除回执里的 `cited_by` 必须同一个口径。

    两个数来自同一次扫描（`_cited_counts`），但一个走 `list_entries`、一个走
    `delete_entry(refresh=True)`：漂开的表现是"列表说被 1 张图引用，删除时却说 0
    张"，用户会以为自己看错了。
    """
    source_id = _ingest()
    _cite(source_id)
    listed = next(item for item in shelf.list_entries().items
                  if item.source_id == source_id)

    try:
        shelf.delete_entry(source_id)
    except shelf.SourceCited as refusal:
        assert refusal.cited_by == listed.cited_by == 1
    else:  # pragma: no cover - 走到这里说明拦截没了
        raise AssertionError("被引用的材料不该在没有 force 的情况下删掉")
