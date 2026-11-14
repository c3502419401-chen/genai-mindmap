"""图游走接进检索（`knowledge/graph_search.py`）的行为约束。

八组，每组钉一类错法：

1. **块级证据**。query 命中图实体 → 两跳路径展开成块 → 每条 hit 的 `quote` 是它自己
   那个区间的原文切片（`text[start:end]`）。区间错位的表现是"高亮标在别处"，而它
   照样能通过逐字校验。
2. **禁止拼接**。一条跨两段的路径产出**两条** hit。拼成一条之后 `quote` 不再是任何
   位置的原文切片，引用校验退化成"这段话在别处出现过吗"。
3. **禁止包络**。路径覆盖 A 与 C 而没覆盖 B 时，B 不出现在任何 hit 里。包络会把
   路径没碰过的段落当成证据交上去。
4. **版本一致性**。`library_sources` 的唯一键是 `content_hash`，所以"内容变 ⇒ 新
   `source_id`"：旧边永远指向**当初那段正文**，这一层不需要额外的过期校验。三条
   推论各一条用例：同正文同 id / 改正文换 id / 旧 id 带着旧正文仍能精确切片。
5. **融合**。文本路前几名保底占半数席位；图块不被词法重筛（图命中没有词法分）。
6. **开关关着 = 没接**。与 `1342942` 的旧模块逐字段相同——这是接线唯一的回归契约。
7. **降级**。Neo4j 抛错时退回纯文本并记 warning，不抛给调用方。
8. **确定性**。同一 query、同一批路径，换返回顺序跑三次，图路输出顺序一致。

全部离线：`conftest.py` 清空了 `MINDMAP_NEO4J_*`，图存储用 monkeypatch 替身（本机
**没有**跑 Neo4j，也不该在单测里装一个）。`add_edge` 手工建图那条路在
`tests/test_graph_neo4j.py` 里用假事务覆盖；这里构造 `GraphPath` 直接喂给
`search_graph`，等价于"边上已经有这些证据"。
"""
from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from app.contracts.source import Source, SourceKind
from app.knowledge import graph_search, graph_store, library, service
from app.knowledge.graph_search import fuse_hits, search_graph
from app.knowledge.graph_store import FORWARD, GraphPath
from app.knowledge.service import (
    EvidenceHit,
    SourceFilter,
    _near_duplicate,
    search_parent,
)

# 本轮改动前的 HEAD。写死而不跟随 HEAD：跟随 HEAD 等于拿自己跟自己比，永远绿。
# 同时钉内容 sha256——只钉 rev 时，改掉常量就能悄悄换一个"基线"。
BASELINE_REV = "1342942"
BASELINE_SHA256 = "30994712bfda614030ec957d53c82efef99e9cb98eea6aebea622da0194795d0"
REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- 材料与替身


# 三段互不相干的正文。段落之间用两个换行，偏移一眼可算。
FIRST = "甲公司向乙公司采购低温泵，合同金额两千万元。"
SECOND = "合同约定分三期付款，每期须出具验收报告。"
THIRD = "逾期付款按日万分之三计违约金。"

TEXT = FIRST + "\n\n" + SECOND + "\n\n" + THIRD
SPAN_FIRST = (0, len(FIRST))
SPAN_SECOND = (len(FIRST) + 2, len(FIRST) + 2 + len(SECOND))
SPAN_THIRD = (SPAN_SECOND[1] + 2, SPAN_SECOND[1] + 2 + len(THIRD))


def _text_source(text: str = TEXT, *, source_id: str = "s-1") -> Source:
    return Source(id=source_id, kind=SourceKind.text, text=text)


def _path(nodes: list[str], evidence: list[tuple[str, int, int]],
          relations: list[str] | None = None) -> GraphPath:
    """手工构造一条路径。`relations` 缺省时按跳数补（内容不影响本层的判断）。

    形状先自检：`len(nodes) - 1 == len(evidence)` 是 `GraphPath` 的契约，
    构造错了会让"这条路径几跳"变成一个没人检查的数——而排名完全靠它。
    """
    hops = len(evidence)
    assert len(nodes) - 1 == hops, "节点数必须比证据/关系多 1"
    return GraphPath(
        nodes=nodes,
        relations=relations or [f"关系{index}" for index in range(hops)],
        evidence=list(evidence),
        directions=[FORWARD] * hops,
    )


def install_graph(monkeypatch, *, entities: list[str], paths: dict[str, list[GraphPath]],
                  configured: bool = True) -> None:
    """把图存储换成替身：只实现 `search_graph` 真正用到的那三个读接口。"""
    monkeypatch.setattr(graph_store, "configured", lambda: configured)
    monkeypatch.setattr(
        graph_store, "entity_degrees",
        lambda *, limit=500: [(name, 10) for name in entities])

    def fake_neighbors(name, *, hops=2, limit=50):
        return list(paths.get(name, []))

    monkeypatch.setattr(graph_store, "neighbors", fake_neighbors)


def _hit(source_id: str, start: int, end: int, quote: str, score: float,
         *, origin=None) -> EvidenceHit:
    return EvidenceHit(source_id=source_id, start=start, end=end, quote=quote,
                       score=score, origin=origin)


# ---------------------------------------------------------------- 1 块级证据


def test_query_entity_becomes_block_evidence(monkeypatch):
    """query 里的实体 → 两跳路径 → 块；`quote` 是那个区间的原文切片。"""
    install_graph(
        monkeypatch,
        entities=["甲公司", "乙公司"],
        paths={"甲公司": [
            _path(["甲公司", "乙公司"],
                  [("s-1", SPAN_FIRST[0], SPAN_FIRST[1])]),
            _path(["甲公司", "乙公司", "低温泵"],
                  [("s-1", SPAN_FIRST[0], SPAN_FIRST[1]),
                   ("s-1", SPAN_THIRD[0], SPAN_THIRD[1])]),
        ]},
    )
    hits = search_graph([_text_source()], "甲公司的采购合同怎么约定的", 5)

    assert hits, "query 命中了实体，必须给出证据"
    for hit in hits:
        # 区间与引文必须互相对得上：这是"逐字校验"唯一能成立的原因。
        assert hit.quote == TEXT[hit.start:hit.end]
        assert 0 <= hit.start < hit.end <= len(TEXT)
        assert hit.source_id == "s-1"
        # 图命中没有词法/向量分；`method` 沿用兼容值，`origin` 标出来源。
        assert hit.score_bm25 is None and hit.score_vector is None
        assert hit.method == "lexical"
        assert hit.origin == "graph"
    assert [hit.start for hit in hits] == [SPAN_FIRST[0], SPAN_THIRD[0]]
    assert hits[0].quote == FIRST


def test_query_without_graph_entity_returns_nothing(monkeypatch):
    """图里没有 query 提到的实体时返回 `[]`（调用方据此退回纯文本）。"""
    install_graph(monkeypatch, entities=["乙公司", "低温泵"], paths={})
    assert search_graph([_text_source()], "报销要哪些材料", 5) == []


def test_generic_terms_and_scattered_chars_do_not_match(monkeypatch):
    """泛称与"子序列巧合"都不算命中——图的路由噪声正是从这两类长出来的。

    `预算` 在 `GENERIC_TERMS` 里（单独出现是上位概念）；`谁准` 是 `谁来批准预算`
    的顺序子序列，但两个字在原文里不连续，不是实体名。
    """
    install_graph(monkeypatch, entities=["预算", "谁准", "财务部"], paths={})
    assert graph_search.entities_in_query("谁来批准预算") == []
    assert graph_search.entities_in_query("财务部批准预算") == ["财务部"]


# ---------------------------------------------------------------- 2、3 禁止拼接与包络


def test_one_path_across_two_blocks_yields_two_hits(monkeypatch):
    """一条跨两段的路径 → **两条** hit，不是一条合并的引文。"""
    install_graph(
        monkeypatch,
        entities=["甲公司"],
        paths={"甲公司": [
            _path(["甲公司", "乙公司", "违约金"],
                  [("s-1", SPAN_FIRST[0], SPAN_FIRST[1]),
                   ("s-1", SPAN_THIRD[0], SPAN_THIRD[1])]),
        ]},
    )
    hits = search_graph([_text_source()], "甲公司", 5)

    assert len(hits) == 2, "两段证据必须产出两条 hit"
    assert [hit.quote for hit in hits] == [FIRST, THIRD]
    joined = "".join(hit.quote for hit in hits)
    assert SECOND not in joined, "禁止把没被覆盖的段落拼进来"
    assert "\n\n" not in joined, "引文里不该出现拼接痕迹"


def test_blocks_between_two_evidence_points_are_not_enveloped(monkeypatch):
    """路径覆盖 A 与 C 但没覆盖 B → B 不进任何 hit（禁止取区间包络）。"""
    install_graph(
        monkeypatch,
        entities=["甲公司"],
        paths={"甲公司": [
            _path(["甲公司", "乙公司", "违约金"],
                  [("s-1", SPAN_FIRST[0], SPAN_FIRST[1]),
                   ("s-1", SPAN_THIRD[0], SPAN_THIRD[1])]),
        ]},
    )
    hits = search_graph([_text_source()], "甲公司", 5)

    for hit in hits:
        assert not (hit.start <= SPAN_SECOND[0] and hit.end >= SPAN_SECOND[1]), \
            "包络会把 B 也算成证据"
        assert SECOND not in hit.quote
    assert SPAN_SECOND[1] - SPAN_SECOND[0] > 0
    # A 与 C 之间那段（B 及其分隔符）不该被任何区间覆盖
    covered = [(hit.start, hit.end) for hit in hits]
    assert covered == [SPAN_FIRST, SPAN_THIRD]


def test_invalid_spans_are_dropped(monkeypatch):
    """越界、倒置、零宽、切出空白的区间一律丢掉，不抛。"""
    text = "甲乙丙丁" * 10
    install_graph(
        monkeypatch,
        entities=["甲公司"],
        paths={"甲公司": [
            _path(["甲公司", "乙"], [("s-1", 0, 10_000)]),      # 越界
            _path(["甲公司", "丙"], [("s-1", 8, 3)]),           # 倒置
            _path(["甲公司", "丁"], [("s-1", 5, 5)]),           # 零宽
            _path(["甲公司", "戊"], [("s-1", 4, 6)]),           # 合法
        ]},
    )
    hits = search_graph([_text_source(text)], "甲公司", 5)
    assert [(hit.start, hit.end) for hit in hits] == [(4, 6)]
    assert hits[0].quote == text[4:6]


# ---------------------------------------------------------------- 4 版本一致性


def test_same_text_keeps_the_same_source_id(monkeypatch):
    """`content_hash` 唯一 ⇒ 同正文同 id、改正文换 id。这是"不需要过期校验"的依据。"""
    first = library.ingest(_text_source())
    again = library.ingest(_text_source())
    changed = library.ingest(_text_source("甲公司向丙公司采购低温泵，合同金额两千万元。"))

    assert first == again, "同一份正文必须落到同一个 source_id"
    assert changed != first, "正文变了就是新 source_id"
    # 旧 id 的那一行正文一个字节都没变——偏移仍然精确。
    assert library.load(first).text == TEXT
    assert library.content_hash(library.load(first).text) != \
        library.content_hash(library.load(changed).text)


def test_old_edge_still_slices_the_old_text(monkeypatch):
    """边绑的是旧 id：它切出来的永远是**当初那段正文**，不会拿旧偏移切新正文。

    这正是 `docs/RAG-DECISIONS.md` 第 11 节担心的那个错法（"指向一段错的原文位置，
    而逐字校验照样通过"）。构造：同一份材料重新解析后**前面插了一段**，所有偏移
    整体后移；旧 id 与新 id 并存时，旧边给出的引文必须仍是老位置上的原文。
    """
    stored = library.ingest(_text_source())
    # 重新解析：开头插了一段，旧偏移全部失效
    reparsed = "补充说明：本材料经重新解析。\n\n" + TEXT
    fresh = library.ingest(_text_source(reparsed))
    assert fresh != stored

    install_graph(
        monkeypatch,
        entities=["甲公司"],
        paths={"甲公司": [
            _path(["甲公司", "乙公司"], [(stored, SPAN_FIRST[0], SPAN_FIRST[1])]),
        ]},
    )
    hits = search_graph([], "甲公司", 5)

    assert len(hits) == 1
    assert hits[0].source_id == stored
    assert hits[0].quote == FIRST
    # 用旧偏移去切新正文会得到下面这段（错的那段），它不该出现
    assert hits[0].quote != reparsed[SPAN_FIRST[0]:SPAN_FIRST[1]]


def test_pruned_source_is_dropped_without_raising(monkeypatch):
    """来源被 `prune` 掉了 → 那条证据丢掉，不猜、不回退、不抛。"""
    stored = library.ingest(_text_source())
    install_graph(
        monkeypatch,
        entities=["甲公司"],
        paths={"甲公司": [
            _path(["甲公司", "乙公司"], [(stored, SPAN_FIRST[0], SPAN_FIRST[1])]),
        ]},
    )
    assert search_graph([], "甲公司", 5)
    library.forget(stored)
    assert search_graph([], "甲公司", 5) == []


def test_request_source_reuses_its_own_id_by_content_hash(monkeypatch):
    """库里有同一份正文时，hit 记在**调用方那份的 id** 上（正文字节相同，偏移可换）。"""
    stored = library.ingest(_text_source())
    request_source = _text_source(source_id="uuid4-of-this-request")
    install_graph(
        monkeypatch,
        entities=["甲公司"],
        paths={"甲公司": [
            _path(["甲公司", "乙公司"], [(stored, SPAN_FIRST[0], SPAN_FIRST[1])]),
        ]},
    )
    hits = search_graph([request_source], "甲公司", 5)

    assert len(hits) == 1
    assert hits[0].source_id == "uuid4-of-this-request"
    assert hits[0].quote == FIRST


def test_filters_apply_to_graph_evidence(monkeypatch):
    """`filters` 在游走前作用于入参，从库里捞来的证据也要再过一遍——图不是后门。"""
    stored = library.ingest(_text_source())
    install_graph(
        monkeypatch,
        entities=["甲公司"],
        paths={"甲公司": [
            _path(["甲公司", "乙公司"], [(stored, SPAN_FIRST[0], SPAN_FIRST[1])]),
        ]},
    )
    assert search_graph([_text_source()], "甲公司", 5,
                        filters=SourceFilter(author="张三")) == []
    assert search_graph([_text_source()], "甲公司", 5,
                        filters=SourceFilter()) != []


def test_duplicate_blocks_get_one_vote(monkeypatch):
    """同一个块被多条路径走到只投一票（取最短跳数）。"""
    install_graph(
        monkeypatch,
        entities=["甲公司"],
        paths={"甲公司": [
            _path(["甲公司", "乙公司"], [("s-1", *SPAN_FIRST)]),
            _path(["甲公司", "丙", "丁"],
                  [("s-1", *SPAN_SECOND), ("s-1", *SPAN_FIRST)]),
        ]},
    )
    hits = search_graph([_text_source()], "甲公司", 5)
    spans = [(hit.start, hit.end) for hit in hits]

    assert spans.count(SPAN_FIRST) == 1, "同一个块投了两票"
    assert spans == [SPAN_FIRST, SPAN_SECOND], "一跳的在前"


# ---------------------------------------------------------------- 5 融合


def _text_hit(index: int) -> EvidenceHit:
    """一条"文本路"命中：分数递减（名次 1 最高），区间互不重叠。"""
    start = index * 100
    return EvidenceHit(source_id="text", start=start, end=start + 50,
                       quote=f"文本块{index}", score=1.0 / (index + 1),
                       score_bm25=1.0 / (index + 1))


def test_text_route_keeps_half_the_seats(monkeypatch):
    """文本路前 `ceil(top_k/2)` 条保底占位：图证据不能把文本证据挤掉。"""
    text_hits = [_text_hit(index) for index in range(4)]
    graph_hits = [
        EvidenceHit(source_id="graph", start=index * 100, end=index * 100 + 40,
                    quote=f"图块{index}", score=1.0 / (index + 1), origin="graph")
        for index in range(4)
    ]
    fused = fuse_hits(text_hits, graph_hits, 4)

    assert len(fused) <= 4
    reserved = (4 + 1) // 2
    assert [hit.source_id for hit in fused[:reserved]] == ["text"] * reserved
    assert fused[0].quote == text_hits[0].quote
    # 保底段的分是 RRF 分，不是文本路原来的 BM25 分
    assert fused[0].score == pytest.approx(1.0 / (service.RRF_K + 1), abs=1e-6)


def test_graph_block_is_not_rescored_lexically(monkeypatch):
    """图块不带词法分，也不会被词法重筛：桥接证据（用词不同）必须留下。"""
    text_hits = [_text_hit(0), _text_hit(1)]
    bridging = EvidenceHit(source_id="graph", start=0, end=30,
                           quote="完全不含查询词的一段", score=1.0,
                           score_bm25=None, score_vector=None, origin="graph")
    fused = fuse_hits(text_hits, [bridging], 3)

    assert bridging.quote in [hit.quote for hit in fused], "图块被词法重筛掉了"
    kept = next(hit for hit in fused if hit.quote == bridging.quote)
    assert kept.score_bm25 is None and kept.origin == "graph"


def test_fuse_without_graph_hits_returns_text_top_k(monkeypatch):
    """图路没给出东西时原样返回文本路——这是"开着但图里没东西 = 没开"的那条契约。"""
    text_hits = [_text_hit(index) for index in range(5)]
    assert fuse_hits(text_hits, [], 3) == text_hits[:3]
    assert fuse_hits([], [EvidenceHit(source_id="graph", start=0, end=5,
                                      quote="图", score=1.0, origin="graph")], 3)[0].score > 0
    assert fuse_hits([], [], 3) == []
    assert fuse_hits(text_hits, [], 0) == []


def test_fuse_keeps_the_given_graph_order():
    """图路的名次**就是**它进来的顺序（排序责任在 `search_graph`），且重复块只一票。

    `fuse_hits` 不重排图块：图命中之间没有可比的分数（`score_bm25` 恒为 `None`），
    重排只能靠调用方给的顺序。这条用例钉住"它确实照单全收"，也钉住去重。
    """
    first = EvidenceHit(source_id="b", start=10, end=20, quote="乙", score=0.0,
                        origin="graph")
    second = EvidenceHit(source_id="a", start=10, end=20, quote="甲", score=0.0,
                         origin="graph")
    text_hits = [_text_hit(0)]

    once = fuse_hits(text_hits, [first, second], 5)
    twice = fuse_hits(text_hits, [first, second], 5)
    assert [hit.quote for hit in once] == [hit.quote for hit in twice]
    assert [hit.quote for hit in once][1:] == ["乙", "甲"], "图路顺序即名次"

    doubled = fuse_hits(text_hits, [first, first, second], 5)
    assert [hit.quote for hit in doubled].count("乙") == 1, "同一个块只投一票"


def test_fuse_keeps_distinct_parent_blocks_in_parent_mode():
    """父块坐标上两个**不同**父块都必须留下，图证据不该顶替掉第二个。

    `MINDMAP_PARENT_RETRIEVAL=1` 时文本路递进来的是父块区间，父块之间的重叠来自
    切块器的句首吸附（`_overlap_start`），不是"同一份证据被切成两块"。在父块坐标
    上跑 `_pick_ordered` 的重叠率判据，会把这两个父块判成重复、静默少给一条——
    而且只在图路给出一条桥接证据时才暴露：图路为空时 `fuse_hits` 原样返回
    `text_hits`，`_pick_ordered` 根本没被调用。
    """
    first = EvidenceHit(source_id="s", start=0, end=160, quote="甲" * 160, score=0.9)
    second = EvidenceHit(source_id="s", start=60, end=760, quote="乙" * 700, score=0.8)
    bridging = EvidenceHit(source_id="graph", start=0, end=30, quote="图",
                           score=0.0, origin="graph")
    assert _near_duplicate(first, second), "前提：按重叠率它们是'重复'的"

    fused = fuse_hits([first, second], [bridging], 5, parent=True)
    keys = [(hit.source_id, hit.start, hit.end) for hit in fused]
    assert ("s", 0, 160) in keys, "保底段的第一个父块不见了"
    assert ("s", 60, 760) in keys, "第二个父块被重叠率判据吞掉了"

    # 块坐标那条路（父块模式关着）判据不变：同一份证据被切成两块仍然只留一条。
    # 这是"不要一刀切删掉 `_near_duplicate`"的另一半。
    blocks = fuse_hits([first, second], [bridging], 5)
    block_keys = [(hit.source_id, hit.start, hit.end) for hit in blocks]
    assert ("s", 60, 760) not in block_keys, "块坐标上的近重复筛被误删了"


# ---------------------------------------------------------------- 6 开关关着 = 没接


def _load_baseline_module(tmp_path):
    """把 `1342942` 的 `service.py` 载成独立模块（本文件改动的基线）。"""
    try:
        raw = subprocess.check_output(
            ["git", "show", f"{BASELINE_REV}:backend/app/knowledge/service.py"],
            cwd=REPO_ROOT, stderr=subprocess.PIPE)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        pytest.skip(f"读不到 {BASELINE_REV} 的 service.py：{exc}")
    assert hashlib.sha256(raw).hexdigest() == BASELINE_SHA256

    path = tmp_path / f"service_{BASELINE_REV}.py"
    path.write_bytes(raw)
    spec = importlib.util.spec_from_file_location(f"service_baseline_{BASELINE_REV}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _dump(hits) -> list[dict]:
    """`model_dump()` 并补齐基线没有的 `origin`（与 `leaf_*` / `rerank_score` 同一处理）。"""
    payload = []
    for hit in hits:
        entry = hit.model_dump()
        entry.setdefault("origin", None)
        payload.append(entry)
    return payload


def test_switch_off_is_byte_identical_to_baseline(tmp_path, monkeypatch):
    """`MINDMAP_GRAPH_SEARCH=0`：与 `1342942` 的 `search_parent` 逐字段相同。

    图存储即使"可用且有实体命中"也不许被碰——开关关着时的行为必须与没接图一致。
    """
    baseline = _load_baseline_module(tmp_path)
    monkeypatch.setenv("MINDMAP_GRAPH_SEARCH", "0")
    install_graph(
        monkeypatch,
        entities=["甲公司"],
        paths={"甲公司": [_path(["甲公司", "乙公司"], [("s-1", *SPAN_FIRST)])]},
    )
    sources = [_text_source()]
    for query in ("甲公司的采购", "违约金怎么算", "乙公司"):
        for top_k in (1, 3, 5):
            assert _dump(search_parent(sources, query, top_k)) == \
                _dump(baseline.search_parent(sources, query, top_k)), query

    assert service._graph_search_enabled() is False


def test_switch_on_but_no_graph_entities_keeps_text_result(monkeypatch):
    """开着但 query 没命中任何图实体 → 与关着逐字段相同。"""
    sources = [_text_source()]
    plain = search_parent(sources, "违约金怎么算", 3)
    monkeypatch.setenv("MINDMAP_GRAPH_SEARCH", "1")
    install_graph(monkeypatch, entities=["甲公司"], paths={})
    with_graph = search_parent(sources, "违约金怎么算", 3)
    assert _dump(with_graph) == _dump(plain)


def test_switch_on_with_graph_evidence_changes_the_result(monkeypatch):
    """开着且图里有证据 → 结果里出现 `origin="graph"` 的条目。

    图证据落在**另一份材料**上（只在库里、不在本次请求的 `sources` 里）：那正是图
    该补的缺口——文本路只给入参那几份切块，跨文档的那一块它看不见。
    """
    other = "违约金上限为合同总额的百分之五。"
    other_id = library.ingest(_text_source(other, source_id="another"))
    sources = [_text_source()]

    monkeypatch.setenv("MINDMAP_GRAPH_SEARCH", "1")
    install_graph(
        monkeypatch,
        entities=["违约金"],
        paths={"违约金": [
            _path(["违约金", "甲公司"], [(other_id, 0, len(other))]),
        ]},
    )
    hits = search_parent(sources, "违约金", 5)

    graph_hits = [hit for hit in hits if hit.origin == "graph"]
    assert graph_hits, hits
    assert graph_hits[0].source_id == other_id
    assert graph_hits[0].quote == other
    # 文本路那几条仍在（保底席位），只是不再是全部
    assert any(hit.origin is None for hit in hits)


def test_switch_on_without_configured_store_keeps_text_result(monkeypatch):
    """开关打开、图**没配**（生产里最常见的"开了但没装 Neo4j"）→ 逐字段相同。

    这条与上面那条不同：上一条是"图配了但没有实体命中"，这一条是"根本没配"。
    两者都必须与关着时一致，但走的是不同分支（`configured()` / 空路径）。
    """
    sources = [_text_source()]
    plain = search_parent(sources, "违约金怎么算", 3)

    monkeypatch.setenv("MINDMAP_GRAPH_SEARCH", "1")
    monkeypatch.delenv(graph_store.PASSWORD_ENV, raising=False)
    monkeypatch.setattr(graph_store, "configured",
                        lambda: bool(graph_store._config()[2]))
    assert search_parent(sources, "违约金怎么算", 3) == plain


def test_store_unconfigured_short_circuits(monkeypatch):
    """没配 Neo4j 时连 `entity_degrees` 都不发——部署选择不该刷告警。"""
    monkeypatch.setenv("MINDMAP_GRAPH_SEARCH", "1")
    calls: list[str] = []
    monkeypatch.setattr(graph_store, "configured", lambda: False)
    monkeypatch.setattr(graph_store, "entity_degrees",
                        lambda *, limit=500: calls.append("degrees") or [("甲公司", 1)])
    hits = search_parent([_text_source()], "违约金", 3)
    assert hits
    assert calls == [], "未配置时不该发起任何图查询"


# ---------------------------------------------------------------- 7 降级


def test_neo4j_failure_degrades_to_text_with_warning(monkeypatch, caplog):
    """图存储抛错 → 退回纯文本 + warning，不让整次检索失败。"""
    sources = [_text_source()]
    plain = search_parent(sources, "违约金怎么算", 3)

    monkeypatch.setenv("MINDMAP_GRAPH_SEARCH", "1")
    monkeypatch.setenv("MINDMAP_NEO4J_PASSWORD", "fake")
    monkeypatch.setattr(graph_store, "configured", lambda: True)
    monkeypatch.setattr(graph_store, "entity_degrees", lambda *, limit=500: [("违约金", 3)])

    def boom(*_args, **_kwargs):
        raise graph_store.GraphStoreError("图存储不可用")

    monkeypatch.setattr(graph_store, "neighbors", boom)
    with caplog.at_level("WARNING", logger="app.knowledge.service"):
        hits = search_parent(sources, "违约金怎么算", 3)

    assert _dump(hits) == _dump(plain)
    assert any("降级为纯文本" in record.message for record in caplog.records)


def test_search_graph_survives_library_errors(monkeypatch, caplog):
    """库读不出来时丢掉那条证据（不抛），其余证据照常返回。"""
    install_graph(
        monkeypatch,
        entities=["甲公司"],
        paths={"甲公司": [_path(["甲公司", "乙公司"], [("gone", *SPAN_FIRST)])]},
    )

    def boom(_source_id):
        raise OSError("磁盘不可读")

    monkeypatch.setattr(library, "load", boom)
    assert search_graph([], "甲公司", 5) == []
    assert any("来源读取失败" in record.message for record in caplog.records)


# ---------------------------------------------------------------- 8 确定性


def test_graph_order_is_stable_across_runs(monkeypatch):
    """同一批路径换返回顺序跑三次，图路输出顺序一致（最短跳数 → (id, start)）。"""
    blocks = [
        _path(["甲公司", "乙"], [("s-1", *SPAN_THIRD)]),
        _path(["甲公司", "丙"], [("s-1", *SPAN_FIRST)]),
        # 两跳：第一条边指向第三段（与上面重复，取最短跳数 1），第二条边指向第二段
        _path(["甲公司", "丁", "戊"],
              [("s-1", *SPAN_THIRD), ("s-1", *SPAN_SECOND)]),
    ]
    orders = [blocks, list(reversed(blocks)),
              [blocks[1], blocks[2], blocks[0]]]
    results = []
    for order in orders:
        install_graph(monkeypatch, entities=["甲公司"], paths={"甲公司": order})
        results.append([(hit.start, hit.end) for hit in
                        search_graph([_text_source()], "甲公司", 5)])
    assert results[0] == results[1] == results[2]
    # 最短跳数优先：两条一跳的在前，两跳的最后
    assert results[0] == [SPAN_FIRST, SPAN_THIRD, SPAN_SECOND]


def test_entity_candidates_are_bounded(monkeypatch):
    """一次检索最多从 `MAX_ENTITIES` 个实体出发（每个实体是一条路径查询）。"""
    names = ["甲公司", "乙公司", "丙公司", "丁公司", "戊公司", "己公司"]
    install_graph(monkeypatch, entities=names, paths={})
    matched = graph_search.entities_in_query("甲公司与乙公司、丙公司、丁公司、戊公司")
    assert len(matched) == graph_search.MAX_ENTITIES
    assert matched == names[:graph_search.MAX_ENTITIES]


# ---------------------------------------------------------------- 9 熔断（降级要够快）


def test_configured_reads_password_only(monkeypatch):
    """`configured()` 只看密码，不连库：没配 = 这个部署不用图。"""
    monkeypatch.delenv(graph_store.PASSWORD_ENV, raising=False)
    assert graph_store.configured() is False
    monkeypatch.setenv(graph_store.PASSWORD_ENV, "whatever")
    assert graph_store.configured() is True


def test_available_is_true_until_a_failure(monkeypatch):
    """从没失败过 → 可用；失败一次 → 冷却；连续失败 → 退避翻倍。"""
    import time as _time

    graph_store.close_driver()
    assert graph_store.available() is True, "没有失败记录时必须是可用的"

    graph_store._note_failure()
    assert graph_store.available() is False, "刚失败过必须进冷却"

    # 退避按连续失败次数翻倍：第 1 次 30s、第 2 次 60s
    graph_store._note_failure()
    graph_store._FAILED_AT = _time.monotonic() - graph_store.FAILURE_COOLDOWN_SECONDS - 1
    assert graph_store.available() is False, "第二次失败的冷却应当是 60s（30s 还不够）"
    graph_store._FAILED_AT = _time.monotonic() - 2 * graph_store.FAILURE_COOLDOWN_SECONDS - 1
    assert graph_store.available() is True, "过了翻倍后的冷却就该重试"

    graph_store._note_success()
    assert graph_store.available() is True


def test_close_driver_resets_the_breaker(monkeypatch):
    """换一套环境变量（测试之间就是这样）之后，上一个用例的失败不该传染。"""
    graph_store._note_failure()
    assert graph_store.available() is False
    graph_store.close_driver()
    assert graph_store.available() is True


def test_search_graph_skips_while_the_breaker_is_open(monkeypatch):
    """熔断期内的图检索**一条查询都不发**——这才是"降级"与"挂十秒"的区别。"""
    calls: list[str] = []
    install_graph(monkeypatch, entities=["甲公司"], paths={})
    monkeypatch.setattr(graph_store, "entity_degrees",
                        lambda *, limit=500: calls.append("degrees") or [("甲公司", 1)])
    monkeypatch.setattr(graph_store, "available", lambda: False)

    assert search_graph([_text_source()], "甲公司", 5) == []
    assert calls == [], "熔断期内不该去问图里有哪些实体"
