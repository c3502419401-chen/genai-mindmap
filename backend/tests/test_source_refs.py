"""节点引用的三层校验与 API 闭环；所有模型调用均为 mock。"""
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app.contracts.diagnostic import DiagnosticLevel
from app.contracts.mindmap import MindMapContent, Node
from app.contracts.source import Source, SourceRef, SourceUsage
from app.generation.service import (
    GenerationError,
    _demo_content,
    validate_generated_content,
)
from app.main import app
from app.mindmaps import repository
from app.optimization.service import check
from app.optimization.source_refs import EvidenceSpan, verify_source_refs


@pytest.fixture(autouse=True)
def _isolate_nli(monkeypatch):
    """这些用例断言的是前两层的行为，必须把可选的第三层关掉。

    NLI 依赖外部模型文件，是否启用取决于运行环境。不隔离的话，同一份代码
    在有模型和无模型的机器上跑出两套诊断，测试就成了环境的探测器而不是
    代码的守门人。第三层自己的用例在 tests/test_nli.py。
    """
    monkeypatch.setenv("MINDMAP_NLI_ENABLED", "0")
    from app.optimization import nli

    nli.reset()
    yield
    nli.reset()


@pytest.fixture
def sources():
    return [
        Source(id="s1", kind="text", text="  机器学习🤖用于识别图像。\n", status="parsed"),
        Source(id="s2", kind="markdown", filename="会议.md", text="会议决定下周发布。", status="parsed"),
    ]


@pytest.fixture
def tree(sources):
    # 一级分支必须是 3 条：这些树最后都交给 `validate_generated_content`，
    # 它默认要求 `MIN_BRANCHES = 3`。新增的 c 与其它分支一样带一条能逐字
    # 对上原文的引用，这样它既不会引入新诊断，也让"某条诊断只出现在某个
    # 节点"的断言保持原意。
    return {
        "title": "材料摘要",
        "root": {"id": "r", "label": "材料摘要", "source_refs": [
            {"source_id": "s1", "quote": "机器学习"},
            {"source_id": "s2", "quote": "下周发布"},
        ], "children": [
            {"id": "a", "label": "图像识别", "source_refs": [
                {"source_id": "s1", "quote": "识别图像"},
            ]},
            {"id": "b", "label": "发布安排", "source_refs": [
                {"source_id": "s2", "quote": "下周发布"},
            ]},
            {"id": "c", "label": "机器学习", "source_refs": [
                {"source_id": "s1", "quote": "机器学习"},
            ]},
        ]},
        "source_usage": [
            SourceUsage(source_id=s.id, kind=s.kind, filename=s.filename,
                        char_count=len(s.text.strip())).model_dump(mode="json") for s in sources
        ],
    }


@pytest.fixture
def client(monkeypatch):
    # 使用唯一的临时数据库文件，既不触碰开发库，也不依赖临时目录的 ACL。
    data = Path(__file__).resolve().parents[1] / "data"
    data.mkdir(exist_ok=True)
    db_file = data / f"source-refs-{uuid4().hex}.db"
    monkeypatch.setenv("MINDMAP_DB_PATH", str(db_file))
    monkeypatch.setenv("MINDMAP_LLM_MODE", "model")
    try:
        with TestClient(app) as api:
            yield api
    finally:
        db_file.unlink(missing_ok=True)


@pytest.mark.parametrize("fields", [
    {"source_id": ""}, {"source_id": " \n"}, {"source_id": "x" * 101},
    {"quote": "x" * 301}, {"unknown": 1},
    {"page": 0}, {"page": -1}, {"page": True}, {"page": 1.5}, {"page": "1"},
    {"start": -1, "end": 1}, {"start": 0}, {"end": 1},
    {"start": 1, "end": 1}, {"start": 2, "end": 1},
    {"start": False, "end": 1}, {"start": 0, "end": 1.5},
])
def test_reference_field_constraints(fields):
    with pytest.raises(ValidationError):
        SourceRef.model_validate({"source_id": "s1", **fields})


def test_blank_quotes_and_reference_limits():
    assert SourceRef(source_id="s1", quote=" \n").quote is None
    assert SourceRef(source_id="s1", quote=" 引文\n").quote == " 引文\n"
    ref = SourceRef(source_id="s1", page=1, start=0, end=1, quote="文")
    assert len(Node(id="n", label="节点", source_refs=[ref] * 20).source_refs) == 20
    with pytest.raises(ValidationError):
        Node(id="n", label="节点", source_refs=[ref] * 21)


def test_legacy_tree_has_independent_empty_reference_lists():
    content = MindMapContent(title="旧导图", root={"id": "r", "label": "根", "children": [
        {"id": "c", "label": "子"},
    ]})
    assert content.source_usage == []
    assert content.root.source_refs == content.root.children[0].source_refs == []
    assert content.root.source_refs is not content.root.children[0].source_refs


def test_referential_integrity_applies_to_nested_nodes(tree):
    tree["root"]["children"][0]["children"] = [
        {"id": "deep", "label": "深层", "source_refs": [{"source_id": "invented"}]},
    ]
    with pytest.raises(ValidationError, match="不在 source_usage 中"):
        MindMapContent.model_validate(tree)


def test_duplicate_source_summaries_are_rejected(tree):
    tree["source_usage"].append(tree["source_usage"][0])
    with pytest.raises(ValidationError, match="来源 ID 不能重复"):
        MindMapContent.model_validate(tree)


def test_quotes_and_unicode_offsets_match_the_original_text(tree, sources):
    quote = "🤖用于识别图像"
    start = sources[0].text.index(quote)
    tree["root"]["children"][0]["source_refs"] = [
        {"source_id": "s1", "start": start, "end": start + len(quote), "quote": quote},
        # 整段引用也带上引文：只给区间的引用现在会被拦（见
        # `test_range_without_quote_is_rejected`）——坐标本身说明不了引的是哪一段。
        {"source_id": "s1", "start": 0, "end": len(sources[0].text),
         "quote": sources[0].text},
    ]
    content = MindMapContent.model_validate(tree)
    before = content.model_dump()
    assert verify_source_refs(content, sources) == []
    assert content.model_dump() == before


@pytest.mark.parametrize("ref,code", [
    ({"source_id": "s1", "quote": "下周发布"}, "SOURCE_QUOTE_MISMATCH"),
    ({"source_id": "s1", "quote": "根本没有这句话"}, "SOURCE_QUOTE_MISMATCH"),
    ({"source_id": "s1", "start": 0, "end": 2, "quote": "机器学习"}, "EMPTY_SOURCE_EXCERPT"),
    # 偏移错、引文逐字存在于原文、且在可见范围内唯一定位 → 这一条**不再**判
    # `SOURCE_QUOTE_MISMATCH`：服务端按引文补正偏移。见
    # `test_offset_is_repaired_when_quote_is_unique`。
    ({"source_id": "s1", "start": 0, "end": 999}, "SOURCE_RANGE_OUT_OF_BOUNDS"),
    ({"source_id": "s1"}, "UNVERIFIED_SOURCE_REF"),
    # 放行口：区间合法、引文为空。旧实现直接通过，等于只核对了坐标。
    ({"source_id": "s1", "start": 0, "end": 4}, "UNVERIFIED_SOURCE_REF"),
])
def test_bad_evidence_is_reported_without_editing_the_map(tree, sources, ref, code):
    tree["root"]["children"][0]["source_refs"] = [ref]
    content = MindMapContent.model_validate(tree)
    before = content.model_dump()
    diagnostics = verify_source_refs(content, sources)
    assert [(d.code, d.node_id) for d in diagnostics] == [(code, "a"), ("UNSOURCED_NODE", "a")]
    assert content.model_dump() == before


def test_invalid_reference_does_not_discard_valid_sibling_reference(tree, sources):
    tree["root"]["children"][0]["source_refs"].append({"source_id": "s2", "quote": "识别图像"})
    content = MindMapContent.model_validate(tree)
    diagnostics = verify_source_refs(content, sources, remove_invalid=True)
    assert [(d.code, d.node_id) for d in diagnostics] == [("SOURCE_QUOTE_MISMATCH", "a")]
    assert [r.source_id for r in content.root.children[0].source_refs] == ["s1"]


@pytest.mark.parametrize("status,text", [("pending", "原文"), ("failed", "原文"), ("parsed", None)])
def test_unavailable_source_is_not_treated_as_verified(tree, sources, status, text):
    sources[0].status = status
    sources[0].text = text
    diagnostics = verify_source_refs(MindMapContent.model_validate(tree), sources)
    assert any(d.code == "SOURCE_TEXT_UNAVAILABLE" and d.node_id == "a" for d in diagnostics)
    assert any(d.code == "UNSOURCED_NODE" and d.node_id == "a" for d in diagnostics)


def test_missing_original_text_and_omitted_sources_are_distinguished(tree):
    content = MindMapContent.model_validate(tree)
    assert check(content) == []
    diagnostics = check(content, [])
    assert {d.code for d in diagnostics} == {"SOURCE_TEXT_UNAVAILABLE", "UNSOURCED_NODE"}
    assert {d.node_id for d in diagnostics if d.code == "UNSOURCED_NODE"} == {"r", "a", "b", "c"}


@pytest.mark.parametrize("page,code", [(1, "SOURCE_PAGE_UNVERIFIED"), (2, "INVALID_SOURCE_PAGE"),
                                       (4, "INVALID_SOURCE_PAGE")])
def test_page_claims_are_not_confused_with_quote_matching(tree, sources, page, code):
    sources[0] = Source(id="s1", kind="document", text=sources[0].text,
                        status="parsed", span={"page": 3}, failed_pages=[2])
    tree["root"]["children"][0]["source_refs"][0]["page"] = page
    diagnostics = verify_source_refs(MindMapContent.model_validate(tree), sources)
    assert any(d.code == code and d.node_id == "a" for d in diagnostics)


def test_validation_rebuilds_source_usage_and_drops_invented_references(tree, sources):
    """来源摘要以真实输入重建，模型伪造的引用在契约校验前就被剔除。

    这是 `pipeline/service.run()` 收尾留给 `validate_generated_content` 的职责，
    换成 agent 之后仍然由同一个函数承担，所以直接对它断言，不必绕过 SSE。
    """
    tree["source_usage"] = {"fabricated": "even invalid metadata must not be trusted"}
    tree["root"]["children"][0]["source_refs"].extend([
        {"source_id": "invented", "quote": "伪造来源"},
        {"source_id": "s2", "quote": "识别图像"},
    ])
    original = deepcopy(tree)

    content, diagnostics = validate_generated_content(tree, sources=sources)

    assert [s.source_id for s in content.source_usage] == ["s1", "s2"]
    assert content.source_usage[1].filename == "会议.md"
    assert content.source_usage[0].char_count == len(sources[0].text.strip())
    # 这条引用只给了引文，服务端按原文把偏移补齐了（`_check_reference` 的
    # "只有 quote"那条路）——引文一个字符都没变，多出来的是坐标。
    kept = content.root.children[0].source_refs
    assert [ref.quote for ref in kept] == ["识别图像"]
    start = sources[0].text.index("识别图像")
    assert (kept[0].start, kept[0].end) == (start, start + len("识别图像"))
    assert {(d.code, d.node_id) for d in diagnostics} == {
        ("UNKNOWN_SOURCE_REF", "a"), ("SOURCE_QUOTE_MISMATCH", "a"),
    }
    assert tree == original


def test_validation_clears_unfounded_citation_but_keeps_node(tree, sources):
    """引用核验不过时只摘掉那条引用，节点本身留着由人决定。"""
    tree["root"]["children"][0]["source_refs"][0]["quote"] = "凭空添加的内容"

    content, diagnostics = validate_generated_content(tree, sources=sources)

    assert content.root.children[0].id == "a"
    assert content.root.children[0].source_refs == []
    assert {(d.code, d.node_id) for d in diagnostics} == {
        ("SOURCE_QUOTE_MISMATCH", "a"), ("UNSOURCED_NODE", "a"),
    }


def test_validation_rejects_malformed_reference_shape(tree, sources):
    """引用字段本身不合法时整次生成失败：那是模型没按约定写，不是可以修的引用。"""
    tree["root"]["source_refs"][0]["start"] = -1

    with pytest.raises(GenerationError):
        validate_generated_content(tree, sources=sources)


def test_demo_does_not_invent_references(sources):
    """占位树不带任何引用：demo 不伪装成模型产物，也不伪造出处。

    两个节点各报一条 UNSOURCED_NODE——这是提示而非失败，占位树照常可用。
    """
    content, diagnostics = validate_generated_content(
        _demo_content("材料摘要").model_dump(), sources=sources, enforce_min_branches=False)

    assert content.root.source_refs == content.root.children[0].source_refs == []
    assert [d.code for d in diagnostics] == ["UNSOURCED_NODE", "UNSOURCED_NODE"]


@pytest.mark.parametrize("path", ["/api/optimization/check", "/api/optimization/suggest"])
def test_diagnostic_endpoints_accept_sources_and_remain_read_only(client, tree, sources, path):
    tree["root"]["children"][0]["source_refs"][0]["quote"] = "不在原文中"
    saved = client.post("/api/mindmaps", json={"content": tree})
    assert saved.status_code == 201
    payload = {"content": tree, "sources": [s.model_dump(mode="json") for s in sources]}
    response = client.post(path, json=payload)
    assert response.status_code == 200
    assert {d["code"] for d in response.json()["diagnostics"]} == {"SOURCE_QUOTE_MISMATCH", "UNSOURCED_NODE"}
    if path.endswith("suggest"):
        assert response.json()["suggestions"] == []
    assert client.get("/api/mindmaps/" + saved.json()["id"]).json() == saved.json()
    assert client.post(path, json={"content": tree}).json()["diagnostics"] == []
    payload["sources"].append(payload["sources"][0])
    assert client.post(path, json=payload).status_code == 422


def test_reference_save_load_update_list_and_conflict(client, tree, sources):
    quote = "机器学习"
    start = sources[0].text.index(quote)
    tree["root"]["source_refs"][0].update(start=start, end=start + len(quote), page=1)
    created = client.post("/api/mindmaps", json={"content": tree})
    assert created.status_code == 201
    record = created.json()
    path = "/api/mindmaps/" + record["id"]
    assert client.get(path).json() == record
    assert client.get("/api/mindmaps").json() == [record]
    content = record["content"]
    content["root"]["label"] = "修改标题保留引用"
    updated = client.put(path, json={"version": 1, "content": content})
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert updated.json()["content"] == content
    assert client.put(path, json={"version": 1, "content": content}).status_code == 409
    assert client.get(path).json() == updated.json()


def test_bad_save_and_update_do_not_write_dangling_references(client, tree):
    created = client.post("/api/mindmaps", json={"content": tree}).json()
    path = "/api/mindmaps/" + created["id"]
    tree["root"]["children"][1]["source_refs"][0]["source_id"] = "unknown"
    for response in (
        client.post("/api/mindmaps", json={"content": tree}),
        client.put(path, json={"version": 1, "content": tree}),
    ):
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"
    assert client.get(path).json() == created
    assert client.get("/api/mindmaps").json() == [created]


def test_existing_database_records_load_without_migration(client):
    legacy = {"title": "旧导图", "root": {"id": "r", "label": "根", "children": [
        {"id": "c", "label": "子", "children": []},
    ]}}
    conn = repository.connect()
    try:
        with conn:
            conn.execute("INSERT INTO mindmaps VALUES (?, ?, ?, ?)",
                         ("legacy", 1, json.dumps(legacy), "2026-01-01T00:00:00Z"))
    finally:
        conn.close()
    response = client.get("/api/mindmaps/legacy")
    assert response.status_code == 200
    content = response.json()["content"]
    assert content["root"]["source_refs"] == content["root"]["children"][0]["source_refs"] == []
    assert content["source_usage"] == []
    assert client.put("/api/mindmaps/legacy", json={"version": 1, "content": content}).status_code == 200


# ---------------------------------------------------------------- 父块返回模式
#
# `search_parent` 把 `quote` 从"命中的那一块"换成了 500 字的父块，于是"引文逐字
# 在原文里"不再等于"引在证据上"：模型从父块里挑一句与查询无关的话，逐字校验照样
# 通过。下面这组用例钉的是补上那半条约束（`EvidenceSpan`）之后的行为，以及它
# **不能**顺带破坏原来的逐字校验。

# 三段互不重叠的话，手工指定「父块 / 命中子块 / 父块之外」三种位置：
#   [0, 命中起点)          父块里与查询无关的前半段
#   [命中起点, 父块结束)    这次真正匹配上的子块
#   [父块结束, 结尾)        检索没命中、但材料整份在 prompt 里的部分
_UNRELATED = "预算审批由部门负责人签核，超过额度上报总经理复审。"
_HIT = "费用报销需要附发票，时限三十个自然日，逾期不再受理。"
_ELSEWHERE = "团队建设活动安排在季度末，预算另行列支。"
_EVIDENCE_TEXT = _UNRELATED + _HIT + _ELSEWHERE

_HIT_START = len(_UNRELATED)
_HIT_END = _HIT_START + len(_HIT)


def _evidence_span() -> EvidenceSpan:
    """一个父块 [0, 命中结束) 与它里面命中的子块 [命中起点, 命中结束)。"""
    return EvidenceSpan("s1", 0, _HIT_END, _HIT_START, _HIT_END)


def _ref_of(quote: str) -> dict:
    start = _EVIDENCE_TEXT.index(quote)
    return {"source_id": "s1", "start": start, "end": start + len(quote), "quote": quote}


def _content_with(*refs: dict) -> MindMapContent:
    # `source_usage` 必须带上 s1：`MindMapContent` 有模型级校验，节点引用的
    # source_id 一旦不在用法清单里就整棵树构造失败。
    return MindMapContent.model_validate({
        "title": "证据核验",
        "root": {"id": "r", "label": "根", "source_refs": list(refs)},
        "source_usage": [
            {"source_id": "s1", "kind": "text", "char_count": len(_EVIDENCE_TEXT)},
        ],
    })


def _the_source() -> Source:
    return Source(id="s1", kind="text", text=_EVIDENCE_TEXT, status="parsed")


def test_ref_inside_hit_span_passes():
    """引用落在命中的子块里：一条诊断都没有。"""
    content = _content_with(_ref_of(_HIT))
    assert verify_source_refs(content, [_the_source()],
                              evidence=[_evidence_span()]) == []


def test_ref_in_parent_but_outside_hit_is_kept_with_a_warning():
    """引用在父块里、但不在命中的子块里：**保留**，只给一条 warn。

    上一版在这里剔除。放宽的理由：模型现在写的是自己的话、引的是材料里的某段，
    它看到的是 450–700 字的父块；要求它精确落在命中的那 200 字里过于苛刻，
    实测会成批剔掉合法引用，界面上表现为"这一支没有证据"。
    子块约束保留下来是为了**可观测**：谁引了父块里与查询无关的部分，诊断里看得见。
    """
    content = _content_with(_ref_of(_UNRELATED))

    assert verify_source_refs(content, [_the_source()]) == []

    diagnostics = verify_source_refs(content, [_the_source()],
                                     evidence=[_evidence_span()])
    assert [d.code for d in diagnostics] == ["SOURCE_REF_OUTSIDE_EVIDENCE"]
    assert diagnostics[0].level == DiagnosticLevel.warn
    assert diagnostics[0].node_id == "r"


def test_ref_outside_all_hits_is_left_alone():
    """引用完全不在这条命中里：不施加约束。

    材料整份在 prompt 里，模型引用一次检索没命中的段落是合法且常见的形态；
    那种引用由逐字校验负责，这条新约束管不到它。
    """
    content = _content_with(_ref_of(_ELSEWHERE))
    assert verify_source_refs(content, [_the_source()],
                              evidence=[_evidence_span()]) == []


def test_ref_passes_when_any_covering_hit_contains_it():
    """一条引用落在两个父块的重叠区、只有第二个的命中区含它：仍然通过。

    父块之间有 `CHUNK_OVERLAP_CHARS` 的重叠，一条引用同时被两条命中覆盖是正常
    形态。按"任一条通过即通过"判定，否则重叠区里的合法引用会被系统性地判失败。
    """
    reference = _ref_of(_HIT)
    evidence = [
        # 覆盖整篇，但命中区在前面那段（不含这条引用）
        EvidenceSpan("s1", 0, len(_EVIDENCE_TEXT), 0, _HIT_START),
        # 第二个父块从命中区末尾前 5 字开始，命中区覆盖到结尾
        EvidenceSpan("s1", _HIT_END - 5, len(_EVIDENCE_TEXT),
                     _HIT_END - 5, len(_EVIDENCE_TEXT)),
    ]
    assert verify_source_refs(_content_with(reference), [_the_source()],
                              evidence=evidence) == []


def test_quote_mismatch_wins_over_span_check():
    """顺序不可换：逐字对不上且定位不到时只报 `SOURCE_QUOTE_MISMATCH`。

    先查区间的话，一条本来就对不上原文的引用会同时得到"位置不对"的提示，
    两条诊断里只有前一条有信息量，后一条是噪声。
    """
    reference = _ref_of(_UNRELATED)
    reference["quote"] = "换了引文"
    codes = {d.code for d in verify_source_refs(
        _content_with(reference), [_the_source()], evidence=[_evidence_span()])}
    # 引用被判掉 → 节点无来源，`UNSOURCED_NODE` 是既有语义的连带结果
    assert codes == {"SOURCE_QUOTE_MISMATCH", "UNSOURCED_NODE"}


def test_outside_hit_ref_is_no_longer_removed():
    """`remove_invalid=True` 也不剔除落在父块外的引用：它只剔除真的对不上的那些。"""
    content = _content_with(_ref_of(_HIT), _ref_of(_UNRELATED))
    diagnostics = verify_source_refs(content, [_the_source()],
                                     evidence=[_evidence_span()],
                                     remove_invalid=True)
    assert [ref.quote for ref in content.root.source_refs] == [_HIT, _UNRELATED]
    assert {d.code for d in diagnostics} == {"SOURCE_REF_OUTSIDE_EVIDENCE"}
