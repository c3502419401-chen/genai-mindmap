"""检索前置元数据过滤：自动提取、过滤语义、embedding 省钱、向后兼容。

四件事必须钉死，因为它们在界面上都看不出来，只能靠测试：

1. **提取只读文件自己写着的东西**。`created_at` 来自 `docProps/core.xml` /
   PDF 信息字典，不是文件系统 mtime——拿 mtime 冒充"文档创建时间"会让下载
   一份 2023 年的报告被判成今天。
2. **过滤在切块与向量化之前**。被筛掉的来源不产生任何 embedding 调用。
   反过来（先切块再逐块丢）功能上"看起来也对"，但白算一遍 BM25 还要为用不到
   的块付向量化的钱，这种退化没有任何报错会提示。
3. **元数据缺失的处理**：条件给了值而来源那一项是 None → 排除；
   条件是 None → 这一项不参与筛选。两条合起来才是"缺什么就筛不了什么"。
4. **`filters=None` 与加过滤之前逐字段一致**。这条用改动前的实现本身来对照
   （从 git 取基线 commit），不是拿新代码自己跟自己比。

真实素材（桌面上的 docx / pdf / xlsx）存在才跑，不存在就 skip 并说明——
那种用例里写死具体值，取不到就该跳过，编一个 `is not None` 的断言等于没测。
"""
from __future__ import annotations

import asyncio
import inspect
import io
import subprocess
import sys
import types
import zipfile
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.agent import tools as agent_tools
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.contracts.source import Source, SourceKind, SourceMetadata, to_utc_iso
from app.ingestion import metadata as ingestion_metadata
from app.ingestion import service as ingestion_service
from app.knowledge import embedding
from app.knowledge.service import (
    SourceFilter,
    _chunks,
    filter_sources,
    search_sources,
)

# ---------------------------------------------------------------- 素材

DESKTOP = Path.home() / "Desktop"
REAL_DOCX = (DESKTOP / "2025404100403陈嘉涛(新)" / "实验4"
             / "陈嘉涛-实验4 图形数据结构实现及其应用(2).docx")
REAL_PDF = DESKTOP / "AI大模型创新应用挑战赛" / "1706.03762v7.pdf"
REAL_XLSX = DESKTOP / "副本师友复试通过名单.xlsx"

# 引入元数据过滤之前的最后一个提交。写死 hash 而不是用 `HEAD`：HEAD 会随
# 提交前进，下一个人提交之后"旧实现对比"就退化成"新代码跟自己比"，永远通过、
# 永远不再测任何东西。写死的 hash 指向的基线不会漂。
BASELINE_COMMIT = "494ddca"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _require(path: Path) -> Path:
    if not path.is_file():
        pytest.skip(f"本机没有这份素材，跳过实测：{path}")
    return path


# ---------------------------------------------------------------- 最小 docx

CORE_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties
    xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:dcterms="http://purl.org/dc/terms/"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>张三</dc:creator>
  <cp:lastModifiedBy>李四</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">2024-03-01T08:00:00Z</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">2024-05-06T09:30:00Z</dcterms:modified>
</cp:coreProperties>
"""


def _docx_bytes(core_xml: str | None) -> bytes:
    """现场构造一个只有 docProps/core.xml 的最小 docx。

    提取函数只读这一份 XML，所以不必造出可被 Word 打开的完整包——造完整包要
    写几十个 part，`_rels/.rels` 里少一条关系提取行为都不变，纯属自找麻烦。
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if core_xml is not None:
            archive.writestr("docProps/core.xml", core_xml)
        archive.writestr("[Content_Types].xml", "<Types/>")
    return buffer.getvalue()


# ---------------------------------------------------------------- 一、自动提取


def test_docx_metadata_from_real_file():
    """真实 docx：author 取 dc:creator，不是 cp:lastModifiedBy。

    断言写的是**这份文件的实际值**。文件被改过就更新断言，不要放宽成
    `is not None`——那样等于只测"函数没抛异常"。
    这份实验报告的 creator 是模板作者（nengshan feng），lastModifiedBy 才是
    学生（陈嘉涛）。用后者覆盖前者会让"作者"变成填表人，所以规则是 creator 优先。
    """
    path = _require(REAL_DOCX)
    meta = ingestion_metadata.extract(path.name, path.read_bytes())

    assert meta.author == "nengshan feng"
    assert meta.created_at == "2025-03-16T09:37:00Z"
    assert meta.modified_at == "2026-06-13T07:26:38Z"
    # 业务分类与自由标签不进自动提取：文件里没有可靠出处
    assert meta.category is None
    assert meta.tags == []


def test_pdf_metadata_from_real_file():
    """真实 PDF（arXiv 1706.03762）：时间取得出，作者栏是空的。

    这份 PDF 的 `/Author` 就是空串，所以断言 `author is None`——如实记录
    "这份文件没写作者"，而不是换个说法让它看起来"提取到了"。
    空串必须折成 None：留着空串，作者过滤就得同时判 0 长度，多一处能忘的地方。
    """
    path = _require(REAL_PDF)
    meta = ingestion_metadata.from_pdf(path.read_bytes())

    assert meta.created_at == "2024-04-10T21:11:43Z"
    assert meta.modified_at == "2024-04-10T21:11:43Z"
    assert meta.author is None


def test_xlsx_metadata_from_real_file():
    """真实 xlsx：PPTX/XLSX 与 DOCX 共用同一份 core.xml 结构。"""
    path = _require(REAL_XLSX)
    meta = ingestion_metadata.extract(path.name, path.read_bytes())

    assert meta.author == "张雨霖"
    assert meta.created_at == "2023-05-12T11:15:00Z"
    assert meta.modified_at == "2026-10-17T12:47:04Z"


def test_minimal_docx_fixture():
    """CI 没有真实素材时也能跑的那条：四个字段全部按 core.xml 填。"""
    meta = ingestion_metadata.extract("报告.docx", _docx_bytes(CORE_XML))

    assert meta.author == "张三"
    assert meta.created_at == "2024-03-01T08:00:00Z"
    assert meta.modified_at == "2024-05-06T09:30:00Z"


def test_last_modified_by_fills_missing_creator():
    """没有 dc:creator 时退回 cp:lastModifiedBy。"""
    payload = CORE_XML.replace("<dc:creator>张三</dc:creator>", "")
    assert ingestion_metadata.extract("a.docx", _docx_bytes(payload)).author == "李四"


def test_blank_creator_falls_back_to_last_modified_by():
    """空的 `<dc:creator></dc:creator>` 与缺失同义，都走兜底。"""
    payload = CORE_XML.replace("<dc:creator>张三</dc:creator>",
                               "<dc:creator>   </dc:creator>")
    assert ingestion_metadata.extract("a.docx", _docx_bytes(payload)).author == "李四"


@pytest.mark.parametrize("raw", [
    b"",
    b"not a zip at all",
    b"PK\x03\x04 truncated",
])
def test_broken_bytes_do_not_raise(raw):
    """坏字节只意味着"没有元数据"，绝不把异常抛给上传路径。"""
    meta = ingestion_metadata.extract("a.docx", raw)
    assert meta == SourceMetadata()


def test_missing_core_xml_and_bad_xml_do_not_raise():
    """缺 core.xml、core.xml 不是合法 XML：两种都返回空元数据。"""
    assert ingestion_metadata.extract("a.docx", _docx_bytes(None)) == SourceMetadata()
    broken = "<cp:coreProperties><dc:creator>张三"
    assert ingestion_metadata.extract("a.docx", _docx_bytes(broken)) == SourceMetadata()


@pytest.mark.parametrize("filename", ["说明.md", "笔记.txt", "扫描件.png", "录音.wav", None])
def test_formats_without_metadata_stay_empty(filename):
    """文本 / 图片 / 音频没有可靠元数据，全部留 None。

    不去读 EXIF 的 Artist：那是**拍摄者**，不是"这份材料的作者"。
    也不拿 mtime 顶替创建时间——那是文件被拷到这台机器上的时间。
    """
    assert ingestion_metadata.extract(filename, b"\x00\x01") == SourceMetadata()


# ---------------------------------------------------------------- 二、PDF 日期


@pytest.mark.parametrize(("raw", "expected"), [
    ("D:20240115103000+08'00'", "2024-01-15T02:30:00Z"),   # 任务给的样例
    ("D:20240115103000-05'30'", "2024-01-15T16:00:00Z"),
    ("D:20240410211143Z", "2024-04-10T21:11:43Z"),
    ("D:20240410211143", "2024-04-10T21:11:43Z"),          # 无时区按 UTC
    ("20240410211143", "2024-04-10T21:11:43Z"),            # 没写 D: 前缀也认
    ("D:20240115103000+08'00", "2024-01-15T02:30:00Z"),    # 尾部少一个引号
    ("D:20240115103000+08", "2024-01-15T02:30:00Z"),       # 只有小时偏移
    ("D:2024", "2024-01-01T00:00:00Z"),                    # 只有年份
    ("D:202403", "2024-03-01T00:00:00Z"),
])
def test_pdf_date_to_iso(raw, expected):
    assert ingestion_metadata.pdf_date_to_iso(raw) == expected


@pytest.mark.parametrize("raw", [
    None,
    "",
    "   ",
    "now",
    "今天下午三点",
    "D:20241315103000",       # 13 月
    "D:20240230103000",       # 2 月 30 日
    "D:20240115243000",       # 24 点
    "D:20240115103000+2500",  # 偏移超过 23 小时
    "D:20240115103000+0870",  # 偏移分钟超过 59
    "D:2024-01-15",           # PDF 不用连字符
])
def test_pdf_date_malformed_is_none(raw):
    """转不了就 None，不猜。猜错的时间会让时间过滤静默漏掉正确来源。"""
    assert ingestion_metadata.pdf_date_to_iso(raw) is None


def test_to_utc_iso_normalizes_offsets():
    """来源侧与条件侧共用同一个归一化函数，所以两边口径必然一致。"""
    assert to_utc_iso("2024-01-15T10:30:00+08:00") == "2024-01-15T02:30:00Z"
    assert to_utc_iso("2024-01-15T02:30:00.750Z") == "2024-01-15T02:30:00Z"
    assert to_utc_iso("2024-01-15") == "2024-01-15T00:00:00Z"
    assert to_utc_iso("2024-01-15T02:30:00") == "2024-01-15T02:30:00Z"
    assert to_utc_iso("不是时间") is None


# ---------------------------------------------------------------- 三、挂载点


class _FakeDocument:
    def export_to_markdown(self) -> str:
        return "# 实验四\n\n图形数据结构实现及其应用。\n"


class _FakeConversion:
    pages: list = []
    document = _FakeDocument()


class _FakeConverter:
    def convert(self, stream, max_num_pages=None):  # noqa: ARG002 - 只对齐签名
        return _FakeConversion()


def test_office_parse_attaches_metadata(monkeypatch):
    """真走一遍 `_parse_office_source`：元数据要落到 `Source.metadata` 上。

    换掉 Docling 的转换器，只验证挂载这一步——正文解析本身由
    `test_ingestion.py` 覆盖，这里不该为了"接上了没"去加载 1.2GB 的版面模型。
    """
    monkeypatch.setattr(ingestion_service, "_get_converter", lambda: _FakeConverter())

    source = ingestion_service.parse_document_bytes("报告.docx", _docx_bytes(CORE_XML))

    assert source.text.startswith("# 实验四")
    assert source.metadata.author == "张三"
    assert source.metadata.created_at == "2024-03-01T08:00:00Z"
    # 正文指纹不该在解析时算：text 之后还会被规整/截断，提前算出来的值是
    # 对不上的指纹。落库方写入前算一次才是正确时机。
    assert source.content_hash is None


def test_source_content_hash_is_a_placeholder():
    """`content_hash` 现在只是契约占位：默认 None、可显式传、无写入方。"""
    source = Source(kind=SourceKind.text, text="正文", status="parsed")
    assert source.content_hash is None

    tagged = Source(kind=SourceKind.text, text="正文", status="parsed",
                    content_hash="a" * 64)
    assert tagged.content_hash == "a" * 64


def test_source_metadata_normalizes_time_on_construction():
    """契约层的归一化：`2024-01-15` 要折成当天零点，否则时间过滤漏掉整天。

    `"2024-01-15" < "2024-01-15T00:00:00Z"` 是字符串比较的必然结果——不折平，
    一份 1 月 15 日的文档会被 `since=2024-01-15` 判成"太旧"而排除。
    """
    meta = SourceMetadata(created_at="2024-01-15",
                          modified_at="2024-06-01T10:30:00+08:00")
    assert meta.created_at == "2024-01-15T00:00:00Z"
    assert meta.modified_at == "2024-06-01T02:30:00Z"

    source = _source("预算审批流程说明。", created_at="2024-01-15")
    assert search_sources([source], QUERY, filters=SourceFilter(since="2024-01-15"))
    assert search_sources([source], QUERY, filters=SourceFilter(until="2024-01-15"))

    # 解析不了折 None：与自动提取同一条规矩，不留脏字符串
    assert SourceMetadata(created_at="上周三").created_at is None


def test_source_metadata_is_optional_and_defaults_empty():
    """旧请求、旧存档不带元数据也照常构造；`filters=None` 的路径靠这个成立。"""
    source = Source(kind=SourceKind.text, text="正文", status="parsed")
    assert source.metadata == SourceMetadata()
    assert source.metadata.author is None
    assert source.metadata.tags == []

    # extra="forbid" 保持不变：拼错字段名要报错，不能静静丢掉
    with pytest.raises(ValidationError):
        SourceMetadata(auther="张三")


# ---------------------------------------------------------------- 四、过滤语义


def _source(text: str, **metadata) -> Source:
    return Source(kind=SourceKind.text, status="parsed", text=text,
                  metadata=SourceMetadata(**metadata))


def _three_sources() -> list[Source]:
    """三份都命中查询、作者与时间各不相同的来源。"""
    return [
        _source("预算审批流程由张三发起，财务部复核。", author="张三",
                created_at="2024-03-15T12:00:00Z", category="财务",
                tags=["预算", "流程"]),
        _source("预算审批流程由李四复核，总监签核。", author="Li Si",
                created_at="2025-06-01T00:00:00Z", category="财务",
                tags=["预算"]),
        _source("预算审批流程的旧版本说明，作者不明。"),
    ]


QUERY = "预算审批流程"


def test_author_filter_hits_exactly_one():
    """三份来源、三个作者：`author="张三"` 只命中一份，且是选中的那份。"""
    sources = _three_sources()
    hits = search_sources(sources, QUERY, filters=SourceFilter(author="张三"))

    assert hits
    assert {hit.source_id for hit in hits} == {sources[0].id}


def test_author_filter_is_case_insensitive_and_stripped():
    """大小写折叠 + 去首尾空白；但**不做子串匹配**。"""
    sources = _three_sources()
    hits = search_sources(sources, QUERY, filters=SourceFilter(author="  li si "))

    assert {hit.source_id for hit in hits} == {sources[1].id}
    # 子串不算命中：否则"张三"会命中"张三丰"，在 1–5 份材料的场景里等于没过滤
    assert search_sources(sources, QUERY, filters=SourceFilter(author="Li")) == []


def test_filter_to_empty_returns_list_and_logs(caplog):
    """筛完没有来源：返回 []、不抛错、留一条能看懂的日志。"""
    with caplog.at_level("INFO", logger="app.knowledge.service"):
        hits = search_sources(_three_sources(), QUERY,
                              filters=SourceFilter(author="查无此人"))

    assert hits == []
    messages = [record.getMessage() for record in caplog.records]
    assert any("筛掉了全部来源" in message for message in messages), messages
    assert any("3→0" in message for message in messages), messages


def test_missing_author_is_excluded_but_passes_without_filter():
    """元数据缺失的两面：要求 author 时出局，没要求时正常参与。"""
    sources = _three_sources()
    nameless = sources[2]

    assert search_sources(sources, QUERY, filters=SourceFilter(author="张三"))
    passed = search_sources(sources, QUERY)
    assert nameless.id in {hit.source_id for hit in passed}

    hits = search_sources(sources, QUERY, filters=SourceFilter(category="财务"))
    assert nameless.id not in {hit.source_id for hit in hits}


def test_no_time_metadata_is_excluded_from_time_filter():
    """没有任何时间可筛的来源，在 since/until 下被排除。"""
    sources = _three_sources()
    hits = search_sources(sources, QUERY, filters=SourceFilter(since="2020-01-01"))

    assert {hit.source_id for hit in hits} == {sources[0].id, sources[1].id}


def test_time_filter_falls_back_to_modified_at():
    """created_at 缺失时用 modified_at，不是"没创建时间就出局"。"""
    source = _source("预算审批流程说明。", modified_at="2025-01-01T00:00:00Z")
    assert search_sources([source], QUERY, filters=SourceFilter(since="2024-01-01"))
    assert search_sources([source], QUERY, filters=SourceFilter(since="2026-01-01")) == []


def test_since_until_boundaries_are_inclusive():
    """闭区间：两端等号都算命中，纯日期的 until 含当天整天。"""
    source = _source("预算审批流程说明。", created_at="2024-03-15T12:00:00Z")

    # 归一化本身先钉住：纯日期的 until 扩到当天最后一秒
    assert SourceFilter(since="2024-03-15").since == "2024-03-15T00:00:00Z"
    assert SourceFilter(until="2024-03-15").until == "2024-03-15T23:59:59Z"

    inclusive = [
        SourceFilter(since="2024-03-15"),                    # 当天零点 ≤ t
        SourceFilter(since="2024-03-15T12:00:00Z"),          # 正好相等
        SourceFilter(until="2024-03-15"),                    # 当天整天，含 t
        SourceFilter(until="2024-03-15T12:00:00Z"),          # 正好相等
        SourceFilter(since="2024-03-15", until="2024-03-15"),
    ]
    for filters in inclusive:
        assert search_sources([source], QUERY, filters=filters), filters

    exclusive = [
        SourceFilter(since="2024-03-15T12:00:01Z"),
        SourceFilter(until="2024-03-15T11:59:59Z"),
        SourceFilter(until="2024-03-14"),
    ]
    for filters in exclusive:
        assert search_sources([source], QUERY, filters=filters) == [], filters


def test_tags_require_all_and_category_is_exact():
    """tags 是 AND（全部命中）；category 大小写敏感、精确匹配。"""
    sources = _three_sources()

    both = search_sources(sources, QUERY,
                          filters=SourceFilter(tags=["预算", "流程"]))
    assert {hit.source_id for hit in both} == {sources[0].id}

    one = search_sources(sources, QUERY, filters=SourceFilter(tags=["预算"]))
    assert {hit.source_id for hit in one} == {sources[0].id, sources[1].id}

    # 条件侧去首尾空白；分类本身大小写敏感（受控枚举，不该被折叠合并）
    assert SourceFilter(category=" 财务 ").category == "财务"
    assert search_sources(sources, QUERY, filters=SourceFilter(category="财务"))
    assert search_sources(sources, QUERY,
                          filters=SourceFilter(category="财务部")) == []


def test_invalid_time_raises_instead_of_being_ignored():
    """条件侧写错时间要报错：静默忽略会让调用方以为筛选生效了。"""
    with pytest.raises(ValidationError):
        SourceFilter(since="2024-13-01")
    with pytest.raises(ValidationError):
        SourceFilter(until="昨天")


def test_blank_condition_is_treated_as_absent():
    """空串条件与 None 同义，否则 `author=""` 会把全部来源筛掉。"""
    filters = SourceFilter(author="  ", category="", since="", tags=[])
    assert filters.author is None and filters.category is None and filters.since is None
    assert len(filter_sources(_three_sources(), filters)) == 3


def test_filter_runs_before_chunking():
    """被筛掉的来源连块都不该产生——这是"先过滤后切块"的直接证据。

    用块数断言：留着它时 `_chunks` 会切出 N 块，过滤后这批块一个都不该存在。
    """
    long_text = "预算审批流程由李四复核。" * 200
    dropped = _source(long_text, author="李四")
    kept = _source("预算审批流程由张三发起。", author="张三")
    assert len(list(_chunks(long_text))) > 1        # 确认这份材料真会被切成多块

    hits = search_sources([kept, dropped], QUERY, filters=SourceFilter(author="张三"))
    assert {hit.source_id for hit in hits} == {kept.id}


# ---------------------------------------------------------------- 五、embedding 省钱


class _VectorHttp:
    """`httpx.Client` 的替身：记录每次请求的文本，回一个固定维度的向量。"""

    def __init__(self):
        self.payloads: list[list[str]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):     # noqa: A002 - 对齐 httpx
        texts = json["input"] if isinstance(json["input"], list) else \
            json["input"]["texts"]
        self.payloads.append(list(texts))
        return httpx.Response(200, json={
            "data": [{"index": index, "embedding": [0.6, 0.8, 0.0, 0.0]}
                     for index in range(len(texts))],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        })

    @property
    def texts(self) -> list[str]:
        return [text for payload in self.payloads for text in payload]


def _forbidden(*args, **kwargs):
    raise AssertionError("这条路径不该构造 HTTP 客户端")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    # conftest 全局默认关掉了向量路；这个文件测的是"先过滤再向量化"，
    # 需要它真的开，凭证由各用例的 `_keyed()` 配。
    monkeypatch.setenv("MINDMAP_HYBRID_SEARCH", "1")
    monkeypatch.setenv("MINDMAP_DB_PATH", str(tmp_path / "vectors.db"))
    monkeypatch.setattr(embedding, "RETRY_BACKOFF", 0.0)


def _keyed(monkeypatch) -> _VectorHttp:
    monkeypatch.setenv("MINDMAP_EMBEDDING_API_KEY", "sk-test")
    monkeypatch.setenv("MINDMAP_EMBEDDING_BASE_URL", "https://maas.example.com/v1")
    fake = _VectorHttp()
    monkeypatch.setattr("app.knowledge.embedding.httpx.Client",
                        lambda *args, **kwargs: fake)
    return fake


def test_filtered_sources_never_reach_embedding(monkeypatch):
    """被筛掉的来源一个块都不该进 embedding 请求。

    对照组是关键：同样的材料不过滤时 BRAVO 必须出现在请求里，否则"没出现"
    可能只是它根本没被切块，测不到省钱这件事。
    """
    fake = _keyed(monkeypatch)
    kept = _source("ALPHA 预算审批流程由张三发起。", author="张三")
    dropped = _source("BRAVO 预算审批流程由李四复核。", author="李四")

    search_sources([kept, dropped], QUERY, filters=SourceFilter(author="张三"))
    assert fake.texts, "过滤后仍该为剩下的来源算向量"
    assert any("ALPHA" in text for text in fake.texts)
    assert not any("BRAVO" in text for text in fake.texts)

    # 请求里的块数必须等于"过滤后来源切出的块数 + 1（查询本身）"，
    # 多出来的块就说明被筛掉的来源仍被切了块、只是没进结果
    assert len(fake.texts) == len(list(_chunks(kept.text or ""))) + 1

    # 对照组：不过滤时 BRAVO 会进请求
    unfiltered = _VectorHttp()
    monkeypatch.setattr("app.knowledge.embedding.httpx.Client",
                        lambda *args, **kwargs: unfiltered)
    search_sources([kept, dropped], QUERY)
    assert any("BRAVO" in text for text in unfiltered.texts)


def test_filter_to_empty_makes_no_http_call(monkeypatch):
    """筛到空连查询都不该向量化——连一次往返都不值得花。"""
    _keyed(monkeypatch)
    monkeypatch.setattr("app.knowledge.embedding.httpx.Client", _forbidden)

    assert search_sources(_three_sources(), QUERY,
                          filters=SourceFilter(author="查无此人")) == []


# ---------------------------------------------------------------- 六、向后兼容


def _shape(hits) -> list[tuple]:
    return [(hit.source_id, hit.start, hit.end, hit.quote, hit.score,
             hit.score_bm25, hit.score_vector, hit.method) for hit in hits]


def _load_baseline_service():
    """从 git 取出加过滤器之前的 `knowledge/service.py` 并执行成模块。"""
    try:
        completed = subprocess.run(
            ["git", "show", f"{BASELINE_COMMIT}:backend/app/knowledge/service.py"],
            cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:      # pragma: no cover
        pytest.skip(f"没有可用的 git：{exc}")
    if completed.returncode != 0:                             # pragma: no cover
        pytest.skip(f"{BASELINE_COMMIT} 不在当前仓库里（浅克隆？）："
                    f"{(completed.stderr or '').strip()[:120]}")

    module = types.ModuleType("knowledge_service_baseline")
    module.__file__ = str(REPO_ROOT / "backend" / "app" / "knowledge" / "service.py")
    # 必须注册进 sys.modules：源码有 `from __future__ import annotations`，
    # 于是 `EvidenceHit.method: Literal[...]` 是字符串注解，pydantic 要靠
    # `sys.modules[类.__module__]` 找回命名空间才能重建模型。不注册的话
    # 第一次构造 EvidenceHit 就报 "is not fully defined"。
    previous = sys.modules.get(module.__name__)
    sys.modules[module.__name__] = module
    try:
        exec(compile(completed.stdout, "<baseline>", "exec"), module.__dict__)
    finally:
        # 用完就撤：留在 sys.modules 里会让后续 import 拿到这份临时模块
        if previous is None:
            sys.modules.pop(module.__name__, None)
        else:                                            # pragma: no cover
            sys.modules[module.__name__] = previous
    return module


def test_filters_none_matches_baseline_implementation(monkeypatch):
    """`filters=None` / 不传 filters 与改动前的实现逐字段一致。

    对照物是真正的旧代码（git 基线 commit），不是"新代码调两次"。
    """
    monkeypatch.setattr("app.knowledge.embedding.httpx.Client", _forbidden)
    baseline = _load_baseline_service()
    assert "filters" not in inspect.signature(baseline.search_sources).parameters

    long_text = "# 第一章 总则\n\n" + "预算审批流程由财务部统一发起。\n\n" * 60
    sources = [
        _source(long_text, author="张三", created_at="2024-03-15T12:00:00Z"),
        _source("预算审批流程的另一份说明，作者不明。"),
        _source("会签与归档的完整流程说明。", author="李四"),
    ]

    for query in ("预算审批流程", "流程", "归档"):
        for top_k in (1, 3, 5):
            old = baseline.search_sources(sources, query, top_k)
            new = search_sources(sources, query, top_k)
            explicit_none = search_sources(sources, query, top_k, filters=None)

            assert _shape(old) == _shape(new) == _shape(explicit_none), (query, top_k)
            assert old, (query, top_k)


def test_filters_none_never_enters_the_filter_branch(monkeypatch):
    """`filters=None` 时过滤代码一条分支都不走。

    比"结果相同"更强的断言：结果相同也可能是"过滤跑了但什么都没筛掉"。
    这里让过滤函数一旦被调用就炸，直接把"没走"钉成事实。
    """
    monkeypatch.setattr("app.knowledge.embedding.httpx.Client", _forbidden)

    def _explode(*args, **kwargs):  # noqa: ARG001 - 只要被调用就失败
        raise AssertionError("filters=None 时不该进入过滤逻辑")

    monkeypatch.setattr("app.knowledge.service.filter_sources", _explode)
    hits = search_sources(_three_sources(), QUERY)
    assert hits


def test_empty_filter_object_is_a_no_op(monkeypatch):
    """显式传一个空 `SourceFilter()`：结果与 None 一致，只是多跑一遍匹配。"""
    monkeypatch.setattr("app.knowledge.embedding.httpx.Client", _forbidden)
    sources = _three_sources()
    assert _shape(search_sources(sources, QUERY)) == \
           _shape(search_sources(sources, QUERY, filters=SourceFilter()))


# ---------------------------------------------------------------- 七、_rag 透传


def _ctx(sources: list[Source]) -> RunContext:
    request = AgentRunRequest(
        goal="做一张导图", intent="expand", sources=sources,
        selected_ids=[], thinking_model="default",
        budget=RunBudget(max_model_calls=5, max_output_tokens=32768),
    )
    return RunContext(request=request, candidate=None,
                      sources_by_id={source.id: source for source in sources})


def _rag_tool_call(**arguments) -> ToolCall:
    return ToolCall(call_id="c-rag", name="rag_query", arguments=arguments)


def _execute_rag(ctx: RunContext, **arguments):
    registry = agent_tools.build_registry("expand", has_sources=True)
    return asyncio.run(registry.execute(_rag_tool_call(**arguments), ctx))


def test_rag_distinguishes_filtered_out_from_no_hit(monkeypatch):
    """过滤筛光与查询没命中要返回两句不同的话——模型看不到日志，只能靠这句话。"""
    source = _source("预算审批流程由张三发起。", author="张三")
    monkeypatch.setattr(agent_tools, "search_sources", lambda *a, **k: [])

    filtered = _execute_rag(_ctx([source]), question=QUERY,
                            filters={"author": "查无此人"})
    assert filtered.ok
    assert filtered.data["hits"] == []
    assert filtered.data["filtered_out"] == 1
    assert "筛掉" in filtered.content

    missed = _execute_rag(_ctx([source]), question="量子色动力学的渐近自由",
                          filters={"author": "张三"})
    assert missed.ok
    assert missed.data["hits"] == []
    assert "filtered_out" not in missed.data
    assert "没有在本次资料里找到" in missed.content


def test_rag_forwards_filters_as_source_filter(monkeypatch):
    """`RagArgs.filters` 透传成 `SourceFilter`，不是原样丢给检索层。"""
    source = _source("预算审批流程由张三发起。", author="张三")
    captured: dict = {}

    def fake_search(sources, query, top_k=5, *, filters=None):
        captured["filters"] = filters
        copied = {"sources": list(sources), "query": query, "top_k": top_k}
        captured.update(copied)
        return []

    monkeypatch.setattr(agent_tools, "search_sources", fake_search)
    result = _execute_rag(_ctx([source]), question=QUERY,
                          filters={"author": "张三", "tags": [" 预算 ", "预算"]})

    assert result.ok
    assert isinstance(captured["filters"], SourceFilter)
    assert captured["filters"].author == "张三"
    assert captured["filters"].tags == ["预算"]      # 去空白 + 去重


def test_rag_without_filters_passes_none(monkeypatch):
    """不带 filters 时传给检索层的是 None——不是空对象，更不是编出来的条件。"""
    captured: dict = {}

    def fake_search(sources, query, top_k=5, *, filters=None):
        captured["filters"] = filters
        return []

    monkeypatch.setattr(agent_tools, "search_sources", fake_search)
    _execute_rag(_ctx([_source("预算审批流程说明。")]), question=QUERY)
    assert captured["filters"] is None


@pytest.mark.parametrize("bad", [
    {"author": 123},          # 类型错
    {"since": "不是时间"},     # 时间解析不了
    {"auther": "张三"},        # 拼错的键（extra="forbid" 会拒）
    {"tags": "预算"},          # 该是数组
])
def test_rag_ignores_bad_filters_and_still_searches(monkeypatch, bad, caplog):
    """坏 filters 被忽略、检索照常：模型编错参数不该连累整次检索。"""
    monkeypatch.setattr("app.knowledge.embedding.httpx.Client", _forbidden)
    source = _source("十月十七日发布二点零版本。", author="张三")

    with caplog.at_level("DEBUG", logger="app.agent.tools"):
        result = _execute_rag(_ctx([source]), question="十月发布", filters=bad)

    assert result.ok, result.content
    assert result.data["hits"], "坏过滤条件不该让检索变成空结果"
    assert any("过滤条件" in record.getMessage() for record in caplog.records)


def test_rag_empty_filters_object_is_no_filter(monkeypatch):
    """`{}` 与全空字段都折成 None：省掉一次无意义的过滤。"""
    assert agent_tools._rag_filters(None) is None
    assert agent_tools._rag_filters({}) is None
    assert agent_tools._rag_filters({"author": "   "}) is None
    assert agent_tools._rag_filters({"author": "张三"}) == SourceFilter(author="张三")


def test_rag_args_field_is_optional():
    """`RagArgs` 新增字段带默认值：旧调用方（模型不传）不受影响。"""
    args = agent_tools.RagArgs(question="问点什么")
    assert args.filters is None
    assert args.top_k == 5


def test_rag_args_folds_non_dict_filters():
    """模型把 filters 写成字符串/数组：折成 None，不废掉整次调用。

    参数层的宽松只发生在模型这一侧；直接调用 `search_sources` 的代码仍然
    必须给一个真正的 `SourceFilter`。
    """
    assert agent_tools.RagArgs(question="问", filters="author=张三").filters is None
    assert agent_tools.RagArgs(question="问", filters=["张三"]).filters is None
    assert agent_tools.RagArgs(question="问",
                               filters={"author": "张三"}).filters == {"author": "张三"}
