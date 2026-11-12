"""混合检索（BM25 + 向量，RRF 融合）的降级矩阵与融合行为。

全部离线：`httpx.Client` 换成可编程替身，任何一条用例都不发真实网络请求；
向量缓存落在 tmp 目录的 SQLite 里，不会污染 `backend/data/mindmaps.db`。

真实接口只在这条路走不到的地方才有话语权——本机没有 `DASHSCOPE_API_KEY`，
所以"接上真 key 之后模型返回什么形状"这件事只能在配好凭证的机器上验收，
这里覆盖的是**拿到响应之后**的全部逻辑。
"""
from __future__ import annotations

import json
import math

import httpx
import pytest

from app.contracts.source import Source, SourceKind
from app.knowledge import embedding, vector_cache
from app.knowledge.embedding import DASHSCOPE_ENDPOINT
from app.knowledge.service import (
    CANDIDATE_FACTOR,
    RRF_K,
    EvidenceHit,
    _bm25_scores,
    _near_duplicate,
    _pick_top,
    _rrf_value,
    search_sources,
)

QUERY = "预算审批流程"


def _source(text: str) -> Source:
    return Source(kind=SourceKind.text, status="parsed", text=text)


def mapper(text: str) -> list[float] | None:
    """按文本前缀给一个 4 维向量，用来看手算余弦。

    归一化后：`[1,0,0,0]` 与查询同向（余弦 1.0），`[0.8,0.6,0,0]` 余弦 0.8，
    `[0,1,0,0]` 正交（余弦 0，应被丢弃），其余默认 0.6。
    """
    if text.startswith("预算审批流程"):
        return [1.0, 0.0, 0.0, 0.0]
    if text.startswith("财务签核"):
        return [0.8, 0.6, 0.0, 0.0]
    if text.startswith("预算审批"):
        # 正交而不是反向：只有 0 才能把"余弦 ≤ 0 丢弃"这条边界测出来，
        # -1 的话 `> 0` 和 `>= 0` 两种写法行为完全相同，测试抓不住回归。
        return [0.0, 1.0, 0.0, 0.0]
    return [0.6, 0.8, 0.0, 0.0]


class VecHttp:
    """`httpx.Client` 的替身：记录每次请求，可注入超时/服务端错误/缺项。

    响应形状按请求体自动切换——`input` 是 dict 就是 DashScope 原生协议，
    是 list 就按 OpenAI 兼容协议回 `data[i]`。这样两套协议共用一份替身，
    也顺带验证了客户端"发什么协议、读什么形状"必须自洽。
    """

    def __init__(self, mapping=mapper, *, timeout: bool = False, status: int | None = None):
        self.mapping = mapping
        self.timeout = timeout
        self.status = status
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):     # noqa: A002 - 与 httpx 同名参数
        self.calls.append({"url": url, "payload": json, "headers": headers})
        if self.timeout:
            raise httpx.ReadTimeout("模拟超时")
        if self.status is not None:
            return httpx.Response(
                self.status, json={"error": {"message": "down", "type": "server_error"}})

        native = isinstance(json.get("input"), dict)
        texts = json["input"]["texts"] if native else json["input"]
        pairs = [
            (index, vector) for index, text in enumerate(texts)
            if (vector := self.mapping(text)) is not None
        ]
        if native:
            body = {"output": {"embeddings": [
                {"text_index": index, "embedding": vector} for index, vector in pairs]}}
        else:
            body = {"data": [
                {"index": index, "embedding": vector} for index, vector in pairs],
                "usage": {"prompt_tokens": 1, "total_tokens": 1}}
        return httpx.Response(200, json=body)


def _forbidden(*args, **kwargs):
    raise AssertionError("这条路径不该构造 HTTP 客户端")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    # `conftest.py` 的全局默认是"无凭证 + 开关关"。这个文件测的正是向量路，
    # 显式打开开关；凭证仍由各用例按需 `_keyed()` 配，否则"没配置"的用例
    # 会被这里的默认值掩盖掉。
    monkeypatch.setenv("MINDMAP_HYBRID_SEARCH", "1")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("MINDMAP_DB_PATH", str(tmp_path / "vectors.db"))
    # 重试退避在测试里没有意义，别真的等 0.5 + 1.0 秒
    monkeypatch.setattr(embedding, "RETRY_BACKOFF", 0.0)


def _use(monkeypatch, fake: VecHttp) -> VecHttp:
    monkeypatch.setattr("app.knowledge.embedding.httpx.Client",
                        lambda *args, **kwargs: fake)
    return fake


def _keyed(monkeypatch, base: str = "https://maas.example.com/compatible-mode/v1") -> None:
    """配上走向量路所需的最小环境：专用 key + MaaS base。"""
    monkeypatch.setenv("MINDMAP_EMBEDDING_API_KEY", "sk-test")
    monkeypatch.setenv("MINDMAP_EMBEDDING_BASE_URL", base)


def test_no_key_falls_back_to_lexical(monkeypatch):
    """无凭证：正常返回、标 lexical、不构造 HTTP 客户端。"""
    monkeypatch.setattr("app.knowledge.embedding.httpx.Client", _forbidden)
    hits = search_sources([_source("预算审批流程由财务部统一发起。")], QUERY)
    assert hits
    assert {hit.method for hit in hits} == {"lexical"}
    assert hits[0].score == hits[0].score_bm25
    assert hits[0].score_vector is None


def test_openai_protocol_request_shape(monkeypatch):
    """默认协议是 OpenAI 兼容：base+/embeddings，input 是字符串数组。"""
    _keyed(monkeypatch, base="https://maas.example.com/compatible-mode/v1/")
    fake = _use(monkeypatch, VecHttp())
    hits = search_sources([_source("预算审批流程由财务部统一发起。")], QUERY)

    assert {hit.method for hit in hits} == {"hybrid"}     # data[i].embedding 解析成功
    call = fake.calls[0]
    assert call["url"] == "https://maas.example.com/compatible-mode/v1/embeddings"
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["payload"]["model"] == "text-embedding-v3"
    assert isinstance(call["payload"]["input"], list)
    assert all(isinstance(text, str) for text in call["payload"]["input"])
    assert "parameters" not in call["payload"]            # 网关未验证的字段不发


def test_dashscope_protocol_request_shape(monkeypatch):
    """原生协议仍可用：固定公共端点、input.texts、parameters.dimension。"""
    monkeypatch.setenv("MINDMAP_EMBEDDING_PROTOCOL", "dashscope")
    monkeypatch.setenv("MINDMAP_EMBEDDING_API_KEY", "sk-test")
    fake = _use(monkeypatch, VecHttp())
    hits = search_sources([_source("预算审批流程由财务部统一发起。")], QUERY)

    assert {hit.method for hit in hits} == {"hybrid"}     # output.embeddings 解析成功
    call = fake.calls[0]
    assert call["url"] == DASHSCOPE_ENDPOINT
    assert call["payload"]["input"]["texts"]
    assert call["payload"]["parameters"] == {"dimension": 1024, "output_type": "dense"}


def test_missing_base_url_is_not_configured(monkeypatch):
    """有 key 但没给 base：openai 协议无处可发，视为未配置——不猜公共域名。"""
    monkeypatch.setenv("MINDMAP_EMBEDDING_API_KEY", "sk-test")
    monkeypatch.setattr("app.knowledge.embedding.httpx.Client", _forbidden)

    hits = search_sources([_source("预算审批流程由财务部统一发起。")], QUERY)
    assert hits and {hit.method for hit in hits} == {"lexical"}


def test_key_falls_back_to_dashscope_env(monkeypatch):
    """没设专用 key 时回退 DASHSCOPE_API_KEY（同一个百炼账号）。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-legacy")
    monkeypatch.setenv("MINDMAP_EMBEDDING_BASE_URL", "https://maas.example.com/v1")
    fake = _use(monkeypatch, VecHttp())

    hits = search_sources([_source("预算审批流程由财务部统一发起。")], QUERY)
    assert {hit.method for hit in hits} == {"hybrid"}
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer sk-legacy"


def test_hybrid_rrf_ranks(monkeypatch):
    """两路都有候选时按 RRF 排；只在语义路命中的块也能进结果。"""
    _keyed(monkeypatch)
    fake = _use(monkeypatch, VecHttp())
    hits = search_sources([
        _source("预算审批。"),            # 只在 BM25 路（余弦 -1 被丢弃）
        _source("财务签核如流。"),        # 只在向量路（BM25 分 0）
        _source("预算审批流程。"),        # 两路都有，且都在第 1 名
    ], QUERY, top_k=5)

    assert [hit.quote for hit in hits] == [
        "预算审批流程。", "预算审批。", "财务签核如流。"]
    assert {hit.method for hit in hits} == {"hybrid"}

    # RRF 的 k 与分母写成字面量，不引用模块里的 RRF_K：引用它会让断言跟着
    # 实现一起漂移（把常量改成 1 也照样"通过"），那种测试等于没写。
    assert RRF_K == 60
    assert CANDIDATE_FACTOR == 3            # 候选余量是设计值，不许静默改小
    assert hits[0].score == round(2 / 61, 6)
    assert hits[1].score == round(1 / 62, 6)
    assert hits[2].score == hits[1].score           # 同分靠短块优先决出先后

    assert hits[1].score_bm25 > 0 and hits[1].score_vector is None
    assert hits[2].score_bm25 == 0.0
    assert hits[2].score_vector == pytest.approx(0.8)
    assert len(fake.calls) == 1                     # 3 块 + 1 查询合批


def test_embedding_timeout_degrades_with_warning(monkeypatch, caplog):
    """接口超时：降级 lexical，留 warning，重试次数符合约定。"""
    _keyed(monkeypatch)
    fake = _use(monkeypatch, VecHttp(timeout=True))
    with caplog.at_level("WARNING"):
        hits = search_sources([_source("预算审批流程由财务部统一发起。")], QUERY)

    assert hits and {hit.method for hit in hits} == {"lexical"}
    assert len(fake.calls) == 1 + embedding.MAX_RETRIES
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert any("向量" in record.getMessage() for record in warnings)
    # 日志里不许出现材料原文
    assert not any("财务部" in record.getMessage() for record in caplog.records)


def test_cache_skips_second_call(monkeypatch):
    """同一段文本检索两次：第二次一个 HTTP 请求都不发。

    第二次刻意换一个 `Source`（新 uuid）——这正是"按文本哈希而不是 source_id
    缓存"的意义：换 id 不影响命中。
    """
    _keyed(monkeypatch)
    fake = _use(monkeypatch, VecHttp())

    first = search_sources([_source("预算审批流程由财务部统一发起。")], QUERY)
    assert len(fake.calls) == 1
    second = search_sources([_source("预算审批流程由财务部统一发起。")], QUERY)

    assert len(fake.calls) == 1
    assert [(hit.start, hit.end, hit.score) for hit in first] == \
           [(hit.start, hit.end, hit.score) for hit in second]


def test_partial_vectors_stay_hybrid(monkeypatch):
    """部分块没有向量：有向量的走两路，没有的只走 BM25，整体仍是 hybrid。"""
    _keyed(monkeypatch)

    def partial(text: str):
        return None if text.startswith("预算审批。") else mapper(text)

    _use(monkeypatch, VecHttp(partial))
    hits = search_sources([
        _source("预算审批。"),
        _source("财务签核如流，审批按次序放款。"),
        _source("预算审批流程。"),
    ], QUERY, top_k=5)

    by_quote = {hit.quote: hit for hit in hits}
    assert {hit.method for hit in hits} == {"hybrid"}
    assert by_quote["预算审批。"].score_vector is None
    assert by_quote["预算审批。"].score_bm25 > 0     # 只靠 BM25 也进得来
    assert by_quote["预算审批流程。"].score_vector is not None


def test_switch_off_sends_no_http(monkeypatch):
    """`MINDMAP_HYBRID_SEARCH=0`：纯 BM25，不构造任何 HTTP 客户端。"""
    _keyed(monkeypatch)
    monkeypatch.setenv("MINDMAP_HYBRID_SEARCH", "0")
    monkeypatch.setattr("app.knowledge.embedding.httpx.Client", _forbidden)

    hits = search_sources([_source("预算审批流程由财务部统一发起。")], QUERY)
    assert hits and {hit.method for hit in hits} == {"lexical"}


def test_switch_off_matches_missing_key(monkeypatch):
    """开关关闭与无 key 是同一套词面结果，逐字段一致。"""
    monkeypatch.setattr("app.knowledge.embedding.httpx.Client", _forbidden)
    text = "预算审批流程由财务部统一发起。" + "财务签核如流，审批按次序放款。" * 20

    monkeypatch.setenv("MINDMAP_HYBRID_SEARCH", "0")
    off = search_sources([_source(text)], QUERY, top_k=3)
    monkeypatch.delenv("MINDMAP_HYBRID_SEARCH")
    no_key = search_sources([_source(text)], QUERY, top_k=3)

    shape = lambda hits: [  # noqa: E731 - 一行小工具，不值得起名
        (hit.start, hit.end, hit.quote, hit.score, hit.method) for hit in hits
    ]
    assert shape(off) == shape(no_key)
    assert off and {hit.method for hit in off} == {"lexical"}


def test_rrf_value_combines_both_routes():
    """RRF 只吃名次：两路都有的块得分是两份倒数之和。"""
    assert _rrf_value(0, {0: 1}, {0: 1}) == pytest.approx(2 / 61)
    assert _rrf_value(0, {0: 1}, {}) == pytest.approx(1 / 61)
    assert _rrf_value(0, {}, {0: 3}) == pytest.approx(1 / 63)
    assert _rrf_value(7, {0: 1}, {1: 1}) == 0.0     # 不在任一候选集里


def test_bm25_matches_closed_form():
    """单 term、等长块的手算值：钉住 IDF 公式，而不只是相对顺序。

    两块各 1 个 token，查询命中其中一块：N=2、df=1、avgdl=1，于是
    idf = ln(1 + (2-1+0.5)/(1+0.5)) = ln 2，长度归一化系数正好是 1，
    score = ln 2 ≈ 0.6931。任何把 IDF 改成常数、或漏掉 k1+1 的实现
    都会偏离这个数，而"更长的块分数更低"之类的相对断言发现不了。
    """
    scores = _bm25_scores("甲乙", ["甲乙", "乙丙"])
    assert scores[0] == pytest.approx(math.log(2), abs=1e-9)
    assert scores[1] == 0.0


def test_bm25_monotonic_in_term_frequency():
    """同长度块，查询词出现更多次的分数更高。"""
    stronger = "预算审批。" * 2
    weaker = "预算审批。" + "财务签核。"
    assert len(stronger) == len(weaker)

    scores = _bm25_scores("预算审批。", [stronger, weaker])
    assert scores[0] > scores[1] > 0


def test_hits_dump_keeps_tool_shape(monkeypatch):
    """`_rag` 的 `model_dump()` 路径：多了五个键，既有键一个不少。"""
    _keyed(monkeypatch)
    _use(monkeypatch, VecHttp())
    hits = search_sources([_source("预算审批流程由财务部统一发起。")], QUERY)

    payload = json.dumps([hit.model_dump() for hit in hits], ensure_ascii=False)
    entry = json.loads(payload)[0]
    legacy = {"source_id", "start", "end", "quote", "score"}
    assert legacy <= set(entry)
    assert set(entry) == legacy | {"score_bm25", "score_vector", "method",
                                   "leaf_start", "leaf_end"}
    # `leaf_*` 只属于父块返回模式（`search_parent`）：这条路上 `start/end` 就是
    # 匹配单元，再记一遍没有信息，所以必须是 None 而不是与 start/end 相等。
    assert entry["leaf_start"] is None and entry["leaf_end"] is None

    # 新字段都有默认值：不传也能构造，旧调用方不会被破坏
    old = EvidenceHit(source_id="s", start=0, end=1, quote="字", score=0.5)
    assert old.method == "lexical"
    assert old.score_bm25 is None and old.score_vector is None
    assert old.leaf_start is None and old.leaf_end is None


def test_dedup_drops_overlapping_copies():
    """去重分支本身：重叠超过一半的两条只留高分那条，且先去重再截断。"""
    base = EvidenceHit(source_id="s", start=0, end=100, quote="甲", score=0.5)
    copy = EvidenceHit(source_id="s", start=20, end=120, quote="乙", score=0.4)
    other = EvidenceHit(source_id="t", start=0, end=100, quote="丙", score=0.3)

    assert [hit.quote for hit in _pick_top([copy, base, other], top_k=5)] == ["甲", "丙"]
    # top_k=1 也必须留下高分那条：去重在前，截断在后
    assert _pick_top([copy, base], top_k=1) == [base]


def test_near_duplicate_filter_still_applies(monkeypatch):
    """混合路下相邻块的重叠副本仍被去掉（重叠 100 码点 < 较短块的一半）。"""
    _keyed(monkeypatch)
    _use(monkeypatch, VecHttp())
    paragraph = "预算审批流程由财务部统一发起。财务签核如流，审批按次序放款。\n"
    text = "# 第一章 总则\n\n" + paragraph * 120

    hits = search_sources([_source(text)], QUERY, top_k=5)
    assert hits
    assert {hit.method for hit in hits} == {"hybrid"}
    for index, left in enumerate(hits):
        for right in hits[index + 1:]:
            assert not _near_duplicate(left, right)


def test_cache_roundtrip_is_float32():
    """缓存读回来的是 float32 精度的向量，不再是 double。"""
    model = "text-embedding-v3"
    key = vector_cache.text_hash("预算审批流程")
    assert vector_cache.put_vectors([(key, [0.5, -0.25, 0.125])], model) == 1
    assert vector_cache.put_vectors([(key, [0.5, -0.25, 0.125])], model) == 0  # IGNORE
    stored = vector_cache.get_vectors([key], model)[key]
    assert stored == pytest.approx([0.5, -0.25, 0.125])
    assert vector_cache.get_vectors([key], "other-model") == {}
