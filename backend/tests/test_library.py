"""知识库（`knowledge/library.py`）与双源检索（`library=True`）的行为约束。

四组，各自钉一类错误：

1. **入库的身份**。同一份正文只存一条（`UNIQUE(content_hash)`）、改名不产生新
   记录、`load` 还原的 `Source` 与入库前逐字段相等（含 metadata 的二次归一化）。
   指纹算错的表现是"跨请求复用失效"或"两份不同正文合成一条"，两者都不会报错。
2. **切块台账**。`library_chunks` 的区间必须逐行等于 `service._chunk_tree` 的输出
   ——两份切块实现一旦分叉，回查出来的父子关系与检索实际用的就不是同一个东西。
3. **默认行为不变**。`library=False` 时与 `038e5fe` 的旧模块逐字段对比：`score`
   是浮点、`quote` 是原文切片，只有逐字段比才看得出"重构有没有动过原行为"。
4. **双源融合**。本次请求的命中保底占半席、库的只补位、`filters` 对库照样生效、
   `prune` 按天数与条数淘汰并级联删切块、`parse_bytes` 盖上与正文一致的指纹、
   图边用的是持久 id（能 `library.load` 回查）。

全部离线：`conftest.py` 已清空凭证并把开关置 0，数据库落在本次用例的 `tmp_path`。
需要向量路的用例显式打开 `MINDMAP_HYBRID_SEARCH` 并替换 `embed_texts`，不会走到
`httpx` 的传输层。
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.contracts.source import Source, SourceKind, SourceMetadata
from app.ingestion.service import parse_bytes, write_content_hash
from app.knowledge import graph_extract, graph_ingest, graph_store, library, service
from app.knowledge.service import (
    LIBRARY_PICK,
    SourceFilter,
    _chunk_tree,
    search_parent,
    search_sources,
)
from app.mindmaps import repository

# 写死，不跟随 HEAD：HEAD 之后 `service.py` 就被这一轮改过了，拿 HEAD 对比等于拿
# 自己跟自己比，永远是绿的。rev 与内容各钉一道——只有 rev 时，改掉常量就能悄悄
# 换一个"基线"；只有 hash 时，看不出这份基线是哪一版。
BASELINE_REV = "038e5fe"
BASELINE_SHA256 = "6cf968f59814c06ae0478527cdbc115b337adfe5647803e64e8ef3b4bf977741"

REPO_ROOT = Path(__file__).resolve().parents[2]


def run(coro):
    return asyncio.run(coro)


def _material(chapter: int = 1, *, filler: int = 30) -> str:
    """一份结构完整、长度够切出多个父块的制度文件。"""
    head = (
        f"# 第{chapter}章 预算审批制度\n\n"
        "一、总则\n\n"
        "本制度规定公司全部费用支出的申请、预算审批与报销流程。各部门在每月五日"
        "前提交下月预算，由财务部汇总后报总经理核准。\n\n"
        "二、审批权限\n\n"
        "单笔金额在五万元以内的预算审批由部门负责人签核；五万元至二十万元由财务"
        "经理核准；超过二十万元须经总经理办公会审议。\n\n"
        "三、报销材料\n\n"
        "费用报销须提交发票原件、审批单与验收记录。缺少任何一项，财务部退回报销"
        "申请并说明原因。\n\n"
    )
    rest = "本章其余条款在规定范围内继续有效，具体解释权归财务部。\n\n"
    return head + rest * (filler + chapter)


def _two_chapters() -> str:
    """两章拼起来的材料：两块都含查询词，本次那批能凑出两条以上命中。"""
    return _material(1) + _material(2)


def _source(text: str, **kwargs) -> Source:
    return Source(kind=SourceKind.text, status="parsed", text=text, **kwargs)


def _row_count(table: str) -> int:
    with repository.connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------- 1 入库身份


def test_ingest_is_idempotent_by_content_hash():
    """同正文两次入库：同一个 id，表里一条。

    复用分支**不重算切块**（`INSERT OR IGNORE` 都省了）：重复写会让
    `library_chunks` 的 `AUTOINCREMENT` 主键一路涨，行数是幂等的、id 却一直在变。
    """
    text = _material(1)
    first = library.ingest(_source(text))
    second = library.ingest(_source(text))

    assert first == second
    assert _row_count("library_sources") == 1
    chunks_after_first = library.chunks(first)
    assert len(chunks_after_first) == len(_chunk_tree(text))
    assert len(library.chunks(second)) == len(chunks_after_first)


def test_renaming_does_not_create_a_second_record():
    """指纹只算 `text`：改文件名不该在库里多出一条。"""
    text = _material(2)
    first = library.ingest(_source(text, filename="预算制度.md"))
    second = library.ingest(_source(text, filename="预算制度(改).md"))

    assert first == second
    assert _row_count("library_sources") == 1
    summary = library.list_sources()
    assert len(summary) == 1
    # 首次入库的文件名保留，后到的描述性信息不覆盖——见 `ingest` 的 docstring。
    assert summary[0]["filename"] == "预算制度.md"


def test_different_text_creates_two_records():
    """同名不同正文必须各存一条：覆盖等于静默丢数据。"""
    first = library.ingest(_source(_material(3), filename="报告.md"))
    second = library.ingest(_source(_material(4), filename="报告.md"))
    assert first != second
    assert _row_count("library_sources") == 2


def test_load_restores_source_field_by_field():
    """`load` 还原的 `Source` 与入库前逐字段相等（DDL 覆盖的字段全部）。

    用 `parse_bytes` 产出的 Source 做回环：那份带 `content_hash`、带 `filename`，
    与真实上传路径一致。唯一的差异是 `id`——入库的语义就是把它换成持久 id。
    """
    text = _material(5)
    original = parse_bytes(SourceKind.text, "预算制度.md", text.encode("utf-8"))
    original.metadata = SourceMetadata(
        author="  张三 ", created_at="2024-01-15",
        modified_at="2024-06-01T10:30:00+08:00",
        category="财务", tags=[" 预算 ", "预算", "", "制度"],
    )

    source_id = library.ingest(original)
    loaded = library.load(source_id)

    assert loaded is not None
    assert loaded.model_dump() == {**original.model_dump(), "id": source_id}
    # 二次归一化确实发生过：库里存的是 UTC 秒级，读回来还是同一个形状。
    assert loaded.metadata.created_at == "2024-01-15T00:00:00Z"
    assert loaded.metadata.modified_at == "2024-06-01T02:30:00Z"
    assert loaded.metadata.tags == ["预算", "制度"]
    # `parse_text` 会 strip 首尾空白，指纹跟着**最终**正文走——这正是"写入方必须在
    # 定稿之后"那条约束的可观测形态。
    assert loaded.content_hash == hashlib.sha256(
        (original.text or "").encode("utf-8")).hexdigest()


def test_load_missing_returns_none():
    assert library.load("不存在") is None


def test_ingest_rejects_blank_text():
    with pytest.raises(ValueError):
        library.ingest(_source("   \n  "))


def test_forget_cascades_to_chunks():
    source_id = library.ingest(_source(_material(6)))
    assert library.chunks(source_id)
    assert library.forget(source_id) is True
    assert library.chunks(source_id) == []
    assert _row_count("library_chunks") == 0
    # 再删一次是 False，不是错误。
    assert library.forget(source_id) is False


def test_ingest_prefers_text_over_stale_declared_hash(caplog):
    """正文在下游被改过时，以正文为准并留一条 warning。

    拿"声明"的指纹落库会把唯一键指向一份不存在的正文——那份正文将来永远无法
    通过指纹找到，而另一份真身会被判成"已经有了"。
    """
    stale = _source(_material(7), content_hash="0" * 64)
    with caplog.at_level("WARNING"):
        source_id = library.ingest(stale)
    assert any("正文指纹与解析时不一致" in record.message for record in caplog.records)
    loaded = library.load(source_id)
    assert loaded.content_hash == hashlib.sha256(
        (stale.text or "").encode("utf-8")).hexdigest()


def test_list_sources_omits_text_and_counts_chunks():
    text = _material(8)
    source_id = library.ingest(_source(text, filename="报告.md"))
    summary = library.list_sources()
    assert len(summary) == 1
    entry = summary[0]
    assert entry["id"] == source_id
    assert entry["chars"] == len(text)
    assert entry["chunks"] == len(_chunk_tree(text))
    assert "text" not in entry, "摘要不该带正文"


# ---------------------------------------------------------------- 2 切块台账


def test_chunk_table_matches_chunk_tree():
    """区间、level、parent 三项逐行相等，顺序也相等。

    `parent` 存的是**本来源内父块的序号**，不是 `_chunk_tree` 里的列表下标：列表
    下标混着父块与子块，跨来源没有意义。
    """
    text = _material(9)
    source_id = library.ingest(_source(text))
    stored = library.chunks(source_id)

    tree = _chunk_tree(text)
    ordinal_of: dict[int, int] = {}
    for index, node in enumerate(tree):
        if node.level == 0:
            ordinal_of[index] = len(ordinal_of)
    expected = [
        (node.start, node.end, node.level,
         None if node.parent is None else ordinal_of[node.parent])
        for node in tree
    ]

    assert [(row.start, row.end, row.level, row.parent) for row in stored] == expected
    # 两级都在：只有父块说明子块那一步没落库。
    assert {row.level for row in stored} == {0, 1}
    assert len(tree) > len([node for node in tree if node.level == 0])


# ---------------------------------------------------------------- 3 默认行为不变


def _load_baseline_module(tmp_path):
    """把 `BASELINE_REV` 的 `service.py` 载成一个独立模块，并核对它的 sha256。"""
    try:
        raw = subprocess.run(
            ["git", "show", f"{BASELINE_REV}:backend/app/knowledge/service.py"],
            cwd=REPO_ROOT, capture_output=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:      # pragma: no cover
        pytest.skip(f"读不到 {BASELINE_REV} 的 service.py：{exc}")

    assert hashlib.sha256(raw).hexdigest() == BASELINE_SHA256

    path = tmp_path / f"service_{BASELINE_REV}.py"
    path.write_bytes(raw)
    spec = importlib.util.spec_from_file_location(
        f"service_baseline_{BASELINE_REV}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _corpus() -> list[Source]:
    return [
        _source(_material(1), filename="制度.md"),
        _source(_material(2), filename="制度续.md"),
        _source("预算审批由部门负责人签核。"),
        _source("   "),
    ]


def test_library_false_matches_baseline_search_sources(tmp_path):
    """`library=False`（默认值）与 `038e5fe` 逐字段相同。

    对比的是旧模块的 `search_sources`、当前的 `search_sources`（默认参数）与显式
    传 `library=False` 的那次调用。两者都要相同——前者证明重构没动行为，后者证明
    这个参数的默认值是"关"。
    """
    baseline = _load_baseline_module(tmp_path)
    sources = _corpus()

    compared = 0
    for query in ("预算审批", "费用报销要什么材料", "紫铜铃兰"):
        for top_k in (1, 3, 5):
            expected = [hit.model_dump() for hit in
                        baseline.search_sources(sources, query, top_k)]
            default = [hit.model_dump() for hit in search_sources(sources, query, top_k)]
            explicit = [hit.model_dump() for hit in
                        search_sources(sources, query, top_k, library=False)]
            assert default == expected, f"search_sources 变了：{query} / {top_k}"
            assert explicit == expected, f"library=False 不等于默认：{query} / {top_k}"
            compared += 1
    assert compared == 9


def test_library_false_matches_baseline_search_parent(tmp_path, monkeypatch):
    """父块模式打开时，**父块这一层**与基线逐字段相同：粒度重构没有动召回。

    比的是"返回了哪些父块、按什么顺序"——`source_id` / `start` / `end` / `quote`
    加列表顺序。刻意不比 `score` 与 `leaf_*`：

      - `leaf_*` 就是子块本身，`score` 则是**子块**的 BM25 分（父块模式的分数来自
        子块匹配）。两者都随 `LEAF_MAX_CHARS` 走，而那个常量由 `SourceRef.quote`
        的 300 字契约决定（子块超过它，那条命中就交不出 `evidence_*`），属于引用
        链的参数，不是召回逻辑。
      - 更实际的一点：BM25 的 idf 与平均块长在整个块集合上算，切块参数一变、块数
        一变，**同一段文字的分数必然跟着变**。把 score 钉进基线等于要求"切块参数
        永不动"，那不是这条用例要守的东西。

    父块边界由 `_chunk_spans` 与 `CHUNK_*` 决定，这一轮一个都没改——"父块层逐字段
    不变"才是可断言、且真正有意义的那条不变量，它正是"改子块粒度不会挪动父块"
    这件事的证据。
    """
    baseline = _load_baseline_module(tmp_path)
    monkeypatch.setenv("MINDMAP_PARENT_RETRIEVAL", "1")
    sources = _corpus()

    def parent_layer(hits):
        return [(hit.source_id, hit.start, hit.end, hit.quote) for hit in hits]

    compared = 0
    for query in ("预算审批", "谁能批钱"):
        for top_k in (2, 5):
            expected = baseline.search_parent(sources, query, top_k)
            current = search_parent(sources, query, top_k, library=False)
            assert parent_layer(current) == parent_layer(expected), \
                f"search_parent 的父块层变了：{query} / {top_k}"
            # 子块那两级仍然必须是父块里的一段：改了切块参数也不许它跑出父块。
            for hit in current:
                assert hit.leaf_start is not None and hit.leaf_end is not None
                assert hit.start <= hit.leaf_start < hit.leaf_end <= hit.end
                # `quote` 是父块正文，按 offset 切出来的子块必须非空
                leaf = hit.quote[hit.leaf_start - hit.start:hit.leaf_end - hit.start]
                assert leaf.strip(), "子块区间落在父块正文里，切出来不能是空白"
            compared += 1
    assert compared == 4


# ---------------------------------------------------------------- 4 双源融合


def test_library_batch_cannot_displace_the_request_sources():
    """本次请求的命中保底占 `ceil(top_k/2)` 席，库的只补位。

    库里放三份同样高度相关的材料——它们切出来的块更多、BM25 分更高（df 在更大的
    块集合上算），纯 RRF 累加会把请求内那两份挤到后面去。`_merge_batches` 的保底段
    就是为了这个：用户刚上传的材料是这次请求的对象，库是补充。
    """
    for chapter in (10, 11, 12):
        library.ingest(_source(_material(chapter)))

    source = _source(_two_chapters())
    top_k = 4
    base = search_sources([source], "预算审批", top_k)
    assert len(base) >= 2, "本次材料要够切出两块，否则保底席无从谈起"

    hits = search_sources([source], "预算审批", top_k, library=True)
    reserve = (top_k + 1) // 2

    request_ids = {source.id}
    assert [hit.source_id for hit in hits[:reserve]] == [source.id] * reserve
    assert [hit.source_id for hit in hits[:reserve]] == \
           [hit.source_id for hit in base[:reserve]]
    assert [(hit.start, hit.end) for hit in hits[:reserve]] == \
           [(hit.start, hit.end) for hit in base[:reserve]]
    # 库确实补了位：剩下的席位里至少有一条来自库（持久 id）。
    rest = hits[reserve:]
    assert rest, "库里有高度相关的材料，不该一条都补不进来"
    assert any(hit.source_id not in request_ids for hit in rest)


def test_library_hits_carry_persistent_ids_that_load():
    """库命中的 `source_id` 是持久 id，能直接回查。"""
    source_id = library.ingest(_source(_material(13)))
    hits = search_sources([], "预算审批", 3, library=True)

    assert hits
    assert all(hit.source_id == source_id for hit in hits)
    loaded = library.load(hits[0].source_id)
    assert loaded is not None
    assert loaded.text[hits[0].start:hits[0].end] == hits[0].quote


def test_library_filters_apply_to_library_sources():
    """`filters` 在库里照样生效：元数据筛不过的来源连切块都不发生。"""
    finance = library.ingest(_source(
        _material(14), metadata=SourceMetadata(category="财务", author="张三")))
    library.ingest(_source(
        _material(15), metadata=SourceMetadata(category="人事", author="李四")))

    hits = search_sources([], "预算审批", 5, library=True,
                          filters=SourceFilter(category="财务"))
    assert hits
    assert {hit.source_id for hit in hits} == {finance}

    # 筛光了就和"库里没东西"一样：返回空，不抛错。
    assert search_sources([], "预算审批", 5, library=True,
                          filters=SourceFilter(category="法务")) == []


def test_library_filters_apply_in_parent_mode():
    """父块模式同理：筛不过的库来源不产生任何父块命中。"""
    finance = library.ingest(_source(
        _material(16), metadata=SourceMetadata(category="财务")))
    library.ingest(_source(_material(17), metadata=SourceMetadata(category="人事")))

    hits = search_parent([], "预算审批", 3, library=True,
                         filters=SourceFilter(category="财务"))
    assert hits
    assert {hit.source_id for hit in hits} == {finance}


def test_duplicate_content_is_not_counted_twice():
    """同一份正文既在本次请求里、又在库里：只占一个席位。

    库那批按 `content_hash` 排除已经在场的正文——否则同一段原文会以两个不同的
    `source_id`（请求内 uuid4 / 持久 id）各占一条，`top_k` 直接少一格，而
    `_near_duplicate` 按 `source_id` 比、拦不住这种重复。
    """
    text = _material(18)
    library.ingest(_source(text))
    hits = search_sources([_source(text)], "预算审批", 5, library=True)
    assert len({(hit.start, hit.end, hit.quote) for hit in hits}) == len(hits)


def test_library_pick_is_capped(monkeypatch):
    """库里的候选有上限：`_library_sources` 最多返回 `LIBRARY_PICK` 份。

    这是"不能把整个库都切块"那条约束的落点。造 8 份材料，断言只挑出 5 份。
    """
    for chapter in range(20, 28):
        library.ingest(_source(_material(chapter)))

    picked = service._library_sources(
        "预算审批", None, [])
    assert len(picked) == LIBRARY_PICK
    assert len({item.id for item in picked}) == LIBRARY_PICK


def test_library_unavailable_degrades_to_request_sources(monkeypatch):
    """库坏了不影响这次检索：退化成"只看本次请求的材料"，不抛错。"""
    def boom(*args, **kwargs):  # noqa: ARG001 - 只对齐签名
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service.knowledge_library, "recent", boom)
    source = _source(_material(1))
    base = search_sources([source], "预算审批", 3)
    assert search_sources([source], "预算审批", 3, library=True) == base


def test_library_empty_matches_library_false_exactly():
    """库里没有可用来源时，`library=True` 的输出与 `library=False` 逐字段相同。

    连 `score` 都不重算：不该因为"库里有别的材料"而让同一批命中看起来变了。
    """
    source = _source(_material(1))
    base = [hit.model_dump() for hit in search_sources([source], "预算审批", 5)]
    with_library = [hit.model_dump() for hit in
                    search_sources([source], "预算审批", 5, library=True)]
    assert with_library == base


# ---------------------------------------------------------------- 5 清理


def _age(source_id: str, stamp: str) -> None:
    """把一份来源的 `last_used_at` 手动拨回去（模拟"很久没人用"）。"""
    with repository.connect() as conn:
        conn.execute("UPDATE library_sources SET last_used_at = ? WHERE id = ?",
                     (stamp, source_id))


def test_prune_by_days_cascades_to_chunks():
    old = library.ingest(_source(_material(30)))
    fresh = library.ingest(_source(_material(31)))
    _age(old, "2020-01-01T00:00:00.000Z")

    assert library.prune(keep_days=90, keep_count=200) == 1
    assert library.load(old) is None
    assert library.load(fresh) is not None
    # 级联：旧来源的切块没有留下孤儿行。
    assert library.chunks(old) == []
    assert _row_count("library_chunks") == len(library.chunks(fresh))


def test_prune_by_count_keeps_the_most_recent():
    ids = [library.ingest(_source(_material(40 + index))) for index in range(4)]
    for index, source_id in enumerate(ids):
        _age(source_id, f"2026-01-0{index + 1}T00:00:00.000Z")

    assert library.prune(keep_days=3650, keep_count=2) == 2
    survivors = {entry["id"] for entry in library.list_sources()}
    assert survivors == {ids[2], ids[3]}
    assert _row_count("library_chunks") == sum(
        len(library.chunks(source_id)) for source_id in ids[2:])


def test_prune_keeps_vector_cache(monkeypatch, tmp_path):
    """`chunk_vectors` 不跟着删：缓存按文本哈希索引，删了只会重算。"""
    monkeypatch.setenv("MINDMAP_DB_PATH", str(tmp_path / "vectors.db"))
    from app.knowledge import vector_cache

    with repository.connect() as conn:
        conn.execute(vector_cache._CREATE)
    vector_cache.put_vectors([(vector_cache.text_hash("一段文本"), [0.5, 0.5])], "m")

    source_id = library.ingest(_source(_material(50)))
    _age(source_id, "2020-01-01T00:00:00.000Z")
    assert library.prune(keep_days=1) == 1
    with repository.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 1


# ---------------------------------------------------------------- 6 指纹写入方


def test_parse_bytes_stamps_content_hash():
    """`parse_bytes` 之后 `content_hash` 非空且等于 `sha256(text)`。"""
    raw = _material(60).encode("utf-8")
    for kind in (SourceKind.text, SourceKind.markdown):
        source = parse_bytes(kind, "材料.md", raw)
        assert source.text
        assert source.content_hash is not None
        assert source.content_hash == hashlib.sha256(
            source.text.encode("utf-8")).hexdigest()


def test_write_content_hash_leaves_blank_text_alone():
    """空正文不盖指纹：`sha256("")` 会让所有空材料在库里挤成一条。"""
    assert write_content_hash(_source("   ")).content_hash is None
    assert write_content_hash(_source("正文")).content_hash == hashlib.sha256(
        "正文".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 7 图边用持久 id


def test_graph_edges_use_persistent_source_id(monkeypatch):
    """`ingest_graph` 写的边用持久 id，`neighbors` 的 `evidence` 能回查。

    不需要真 Neo4j：`add_edges_bulk` 被换掉，捕获的 `EdgeInput` 就是
    `graph_store` 会落进边属性里的那几个字段（`source_id` / `chunk_start` /
    `chunk_end`），而 `GraphPath.evidence` 正是这三样。
    """
    captured: list[graph_store.EdgeInput] = []

    def bulk(edges):
        batch = list(edges)
        captured.extend(batch)
        return len(batch)

    monkeypatch.setattr(graph_ingest.graph_store, "add_edges_bulk", bulk)
    monkeypatch.setattr(graph_ingest.graph_store, "upsert_entity",
                        lambda name, kind=None: 1)

    async def fake_extract(text, *, source_id, chunk_start=0):  # noqa: ARG001
        return graph_extract.GraphExtraction(
            entities=[graph_extract.ExtractedEntity(name="甲", type="人物"),
                      graph_extract.ExtractedEntity(name="乙", type="组织")],
            relations=[graph_extract.ExtractedRelation(
                src="甲", dst="乙", relation="任职于")],
        )

    monkeypatch.setattr(graph_ingest, "extract_graph", fake_extract)

    # 短材料：一块一条边，好断言。长材料每块都抽到同一条边，`edges` 会累加。
    source = _source("甲在乙任职。")
    assert run(graph_ingest.ingest_graph(source))["edges"] == 1
    assert captured

    edge = captured[0]
    assert edge.source_id != source.id, "边不该用请求内那个 uuid4"
    loaded = library.load(edge.source_id)
    assert loaded is not None
    assert loaded.text == source.text
    # `neighbors` 返回的 evidence 就是这三样，回查成立。
    evidence = graph_store.GraphPath(
        nodes=["甲", "乙"], relations=["任职于"],
        evidence=[(edge.source_id, edge.chunk_start, edge.chunk_end)],
    ).evidence[0]
    assert library.load(evidence[0]) is not None
    assert 0 <= evidence[1] < evidence[2] <= len(loaded.text)


def test_graph_ingest_accepts_explicit_source_id(monkeypatch):
    """显式给 id 时不再写库、也不再查库：调用方自己掌管身份。"""
    captured: list[graph_store.EdgeInput] = []
    monkeypatch.setattr(graph_ingest.graph_store, "add_edges_bulk",
                        lambda edges: captured.extend(edges) or len(list(edges)))
    monkeypatch.setattr(graph_ingest.graph_store, "upsert_entity",
                        lambda name, kind=None: 1)

    async def fake_extract(text, *, source_id, chunk_start=0):  # noqa: ARG001
        return graph_extract.GraphExtraction(
            entities=[graph_extract.ExtractedEntity(name="甲", type="人物")],
            relations=[graph_extract.ExtractedRelation(
                src="甲", dst="乙", relation="任职于")],
        )

    monkeypatch.setattr(graph_ingest, "extract_graph", fake_extract)
    run(graph_ingest.ingest_graph(_source("甲在乙任职。"), source_id="doc-42"))

    assert {edge.source_id for edge in captured} == {"doc-42"}
    assert _row_count("library_sources") == 0


def test_graph_ingest_skips_library_for_blank_text(monkeypatch):
    """空正文不进库：`_empty_stats` 的键也不能为此多一个。"""
    assert run(graph_ingest.ingest_graph(_source("   "))) == {
        "chunks": 0, "entities": 0, "edges": 0, "failed": 0, "aborted": False,
        "elapsed_ms": 0,
    }
    assert _row_count("library_sources") == 0
