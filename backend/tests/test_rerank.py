"""召回后的重排（`knowledge/rerank.py`）：扩池、重排、降级、以及"关着时零痕迹"。

四条主线，各自钉一类错误：

1. **关着 = 没接**。`c38684c`（接重排之前）的 `service.py` 整份载进来对比，
   `MINDMAP_RERANK=0` 与"=1 但没凭证"两种情形都要与它逐字段相同，而且都不能
   构造 HTTP 客户端。扩池本身会改变 RRF 的名次映射，白扩一次池就等于让同一批
   命中的 `score` 悄悄变一个数——这条等价关系是"默认关"唯一站得住的理由。
2. **扩池与重排各自生效**。池子是 `max(top_k*4, 10)`（用 mock 收到的 documents
   条数直接断言），重排真的换顺序（用"倒序打分"的替身，顺序必须整体反转）。
3. **四种降级**：无 key / 超时 / 5xx / 非法 JSON，一律返回**原顺序的前 top_k**
   条，且 `rerank_score` 全为 None——降级不能留下"被打过分"的痕迹。
4. **只重排不改变证据**。重排后每条的 `source_id`/`start`/`end`/`quote`/
   `leaf_*`/`score` 与重排前逐字段相同，只有顺序与 `rerank_score` 变了。

全部离线：`httpx.Client` 换成可编程替身，`conftest.py` 已经清空凭证并把
`MINDMAP_RERANK` 置 0（线上默认是 1）；需要重排的用例自己开开关并配 key。
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import logging
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app.contracts.source import Source, SourceKind
from app.knowledge import rerank
from app.knowledge.service import (
    EvidenceHit,
    _search_parent_core,
    search_multi,
    search_parent,
)

# 写死，不跟随 HEAD：HEAD 之后 `service.py` 就被这一轮改过了（新增重排接线）。
# sha256 一并写死——只写 rev 的话，改掉常量就能悄悄换一个"基线"。
BASELINE_REV = "c38684c"
BASELINE_SHA256 = "b5214316dc93606d034bae277fd5dd9c5cd80f72089201975b368d7773dc2bca"

REPO_ROOT = Path(__file__).resolve().parents[2]

QUERY = "预算审批"
RERANK_ENV = "MINDMAP_RERANK"


# ---------------------------------------------------------------- 测试材料


def _material(paragraphs: int = 70) -> str:
    """够切出十几个父块的正文。

    每段都含「预算审批」，所以 BM25 对所有块都有分数、排序有区分度；段号让每条
    命中在文本上可辨认（断言"重排只是换顺序"时靠它对齐）。
    """
    segments = []
    for index in range(paragraphs):
        head = f"预算审批第{index:02d}笔由部门负责人签核。"
        middle = f"本段为第{index:02d}项说明，附经办人意见与归档编号 {index:02d}。"
        filler = "原始凭证于次月十日前归档，逾期由财务复核。" * 4
        segments.append(head + middle + filler)
    return "\n\n".join(segments)


def _source(text: str | None = None) -> Source:
    return Source(kind=SourceKind.text, status="parsed", text=_material() if text is None else text)


def _corpus() -> list[Source]:
    return [_source(), _source("费用报销需要附发票与审批单。"), _source("   ")]


# ---------------------------------------------------------------- HTTP 替身


def _native_response(payload: dict, scores) -> httpx.Response:
    """按原生 DashScope 形状回包：`output.results[i].index/relevance_score`。"""
    documents = payload["input"]["documents"]
    body = {"output": {"results": [
        {"index": index, "relevance_score": scores[index]}
        for index in range(len(documents))
    ]}, "usage": {"total_tokens": 1}, "request_id": "test"}
    return httpx.Response(200, json=body)


def _descending(payload: dict) -> httpx.Response:
    """分数随输入顺序递减：名次 0 分最高，重排后顺序应当不变。"""
    count = len(payload["input"]["documents"])
    return _native_response(payload, [1.0 - index / (count + 1) for index in range(count)])


def _reversed(payload: dict) -> httpx.Response:
    """分数随输入顺序**递增**：名次 0 分最低，重排后顺序应当整体反转。"""
    count = len(payload["input"]["documents"])
    return _native_response(payload, [index / (count + 1) for index in range(count)])


class RerankHttp:
    """`httpx.Client` 的替身：按 URL 决定行为，记录每一次请求。"""

    def __init__(self, handler=None, *, routes: dict[str, object] | None = None):
        self.handler = handler or _descending
        self.routes = routes or {}
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):     # noqa: A002 - 与 httpx 同名参数
        self.calls.append({"url": url, "payload": json, "headers": headers})
        for suffix, behaviour in self.routes.items():
            if url.endswith(suffix):
                return self._respond(behaviour, json)
        return self._respond(self.handler, json)

    @staticmethod
    def _respond(behaviour, payload):
        if isinstance(behaviour, Exception):
            raise behaviour
        if callable(behaviour):
            return behaviour(payload)
        return behaviour


def _forbidden(*args, **kwargs):
    raise AssertionError("这条路径不该构造 HTTP 客户端")


def _use(monkeypatch, fake: RerankHttp) -> RerankHttp:
    monkeypatch.setattr("app.knowledge.rerank.httpx.Client", lambda *a, **k: fake)
    return fake


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """每个用例从"没判过任何端点不可用"开始。

    `_UNUSABLE` 是进程级的（跨请求复用，避免每次检索都白打一次已知无效的路由），
    测试之间必须隔离——否则前一条用例把公共域名判死，后一条就看不到请求了。
    """
    monkeypatch.setattr(rerank, "_UNUSABLE", set())


def _keyed(monkeypatch, base: str = "https://maas.example.com/compatible-mode/v1") -> None:
    """配上重排所需的最小环境：开关 + 专用 key + MaaS base，并打开父块召回。

    `MINDMAP_PARENT_RETRIEVAL` 必须显式打开：`conftest.py` 的全局默认是 0，而
    `search_parent` 在父块模式关着时会转发给 `search_sources`——那条路上没有接
    重排（见 `search_parent` 的 docstring）。不打开的话这一整个文件的"断言重排
    发生了"都会退化成"断言什么都没发生"，连降级用例都会假绿。
    """
    monkeypatch.setenv("MINDMAP_RERANK", "1")
    monkeypatch.setenv("MINDMAP_PARENT_RETRIEVAL", "1")
    monkeypatch.setenv("MINDMAP_EMBEDDING_API_KEY", "sk-test")
    monkeypatch.setenv("MINDMAP_EMBEDDING_BASE_URL", base)


def _dump(hits) -> list[dict]:
    return [hit.model_dump() for hit in hits]


def _spans(hits) -> list[tuple[str, int, int, str]]:
    return [(hit.source_id, hit.start, hit.end, hit.quote) for hit in hits]


# ---------------------------------------------------------------- 1 关着 = 没接


def _load_baseline_module(tmp_path):
    """把 `BASELINE_REV` 的 `service.py` 载成独立模块，并核对 sha256。"""
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
    spec = importlib.util.spec_from_file_location(f"service_baseline_{BASELINE_REV}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _baseline_dump(hits) -> list[dict]:
    """基线模块的 `EvidenceHit` 没有 `rerank_score`，补齐 None 再逐键比。"""
    payload = []
    for hit in hits:
        entry = hit.model_dump()
        entry.setdefault("rerank_score", None)
        payload.append(entry)
    return payload


def test_switch_off_is_byte_identical_to_baseline(tmp_path, monkeypatch):
    """`MINDMAP_RERANK=0`：`search_parent` 与 `c38684c` 逐字段相同，且不发 HTTP。

    比两个入口（`search_parent` 单路 / `search_multi` 多路），因为扩池这两个入口
    都改了：`_search_parent_core` 与 `_search_multi_core` 各多一个 `depth` 参数。
    少比任何一个，那个入口的"关着时没变"就没有证据。
    """
    baseline = _load_baseline_module(tmp_path)
    monkeypatch.setattr("app.knowledge.rerank.httpx.Client", _forbidden)
    monkeypatch.setenv(RERANK_ENV, "0")
    sources = _corpus()
    queries = ("预算审批", "费用报销要什么材料", "紫铜铃兰")

    compared = 0
    for query in queries:
        for top_k in (1, 3, 5):
            expected = _baseline_dump(baseline.search_parent(sources, query, top_k))
            actual = _dump(search_parent(sources, query, top_k))
            assert actual == expected, f"search_parent 变了：{query} / {top_k}"

            expected_multi = _baseline_dump(
                asyncio.run(baseline.search_multi(sources, [query], top_k)))
            actual_multi = _dump(asyncio.run(search_multi(sources, [query], top_k)))
            assert actual_multi == expected_multi, f"search_multi 变了：{query} / {top_k}"
            compared += 1
    assert compared == 9


def test_switch_on_without_credentials_is_also_identical(tmp_path, monkeypatch):
    """`MINDMAP_RERANK=1` 但没 key：**连池子都不扩**，同样逐字段相同。

    扩池会改变 `_fuse` / `_search_multi_core` 里每路截断的 `pool`，进而改变 RRF
    名次映射。所以"没配凭证还扩池"不是无害的多余动作，而是会让 `score` 变一个数。
    这条用例就是那句 `_rerank_depth` 的第二个条件（`configured()`）的证据。
    """
    baseline = _load_baseline_module(tmp_path)
    monkeypatch.setattr("app.knowledge.rerank.httpx.Client", _forbidden)
    monkeypatch.setenv(RERANK_ENV, "1")
    sources = _corpus()

    for query in ("预算审批", "费用报销要什么材料"):
        for top_k in (1, 3, 5):
            expected = _baseline_dump(baseline.search_parent(sources, query, top_k))
            assert _dump(search_parent(sources, query, top_k)) == expected
            expected_multi = _baseline_dump(
                asyncio.run(baseline.search_multi(sources, [query], top_k)))
            assert _dump(asyncio.run(search_multi(sources, [query], top_k))) == expected_multi


# ---------------------------------------------------------------- 2 扩池


def test_pool_size_formula():
    """`max(top_k * 4, 10)`，逐点算一遍。"""
    assert rerank.pool_size(1) == 10
    assert rerank.pool_size(2) == 10
    assert rerank.pool_size(3) == 12
    assert rerank.pool_size(5) == 20
    assert rerank.pool_size(10) == 40
    assert rerank.pool_size(0) == 0


def test_default_model_is_qwen3_rerank():
    """默认模型钉在 `qwen3-rerank`：区分度是选它的唯一理由。

    `gte-rerank-v2` 在同样的三条候选上给 0.357 / 0.141 / 0.006，而 `qwen3-rerank`
    给 0.798 / 0.379 / 0.157——前者全挤在低分区，相邻名次的差值更小，重排的稳定性
    会明显缩水（见 `rerank.py` 里 `DEFAULT_MODEL` 上那段注释）。这条断言防的是
    "有人顺手把默认值改回 gte"，那是个不会报错、只会悄悄变差的决定。
    """
    assert rerank.DEFAULT_MODEL == "qwen3-rerank"
    assert rerank.model_name() == "qwen3-rerank"


def test_switch_defaults_to_on(monkeypatch):
    """`MINDMAP_RERANK` 默认 **1**（评估跑出 +14.2pp 之后翻的）。

    这个默认值是被数据翻过来的，所以要用一条断言钉着——否则它就是个没人看、
    谁也说不清为什么是 1 的字符串。
    """
    monkeypatch.delenv(RERANK_ENV, raising=False)
    assert rerank.enabled() is True
    for value in ("0", "off", "false", "no", ""):
        monkeypatch.setenv(RERANK_ENV, value)
        assert rerank.enabled() is False, f"{value!r} 应当算关"
    for value in ("1", "on", "true", "yes", "YES"):
        monkeypatch.setenv(RERANK_ENV, value)
        assert rerank.enabled() is True, f"{value!r} 应当算开"


def test_candidate_pool_is_pool_size(monkeypatch):
    """端到端：reranker 收到的候选数 = `pool_size(top_k)`，而返回仍是 `top_k` 条。"""
    _keyed(monkeypatch)
    fake = _use(monkeypatch, RerankHttp())

    for top_k in (3, 5):
        fake.calls.clear()
        hits = search_parent(_corpus(), QUERY, top_k)
        assert len(fake.calls) == 1
        documents = fake.calls[0]["payload"]["input"]["documents"]
        assert len(documents) == rerank.pool_size(top_k)
        assert len(hits) == top_k


def test_core_depth_controls_pool(monkeypatch):
    """`depth` 是候选池的开关：不传就还是 `top_k`，传了就返回那么多条。"""
    monkeypatch.setenv("MINDMAP_PARENT_RETRIEVAL", "1")
    sources = _corpus()
    assert len(_search_parent_core(sources, QUERY, 3, None)) == 3
    pooled = _search_parent_core(sources, QUERY, 3, None, depth=12)
    assert len(pooled) == 12
    # 池子只是更长，前 3 条与不扩池时相同（扩池不改名次，只多给候选）
    assert _spans(pooled[:3]) == _spans(_search_parent_core(sources, QUERY, 3, None))


# ---------------------------------------------------------------- 3 重排生效


def test_reversed_scores_reverse_order(monkeypatch):
    """倒序打分的替身：最终顺序必须是原顺序的**反转**（截到 top_k）。

    这条是"重排真的生效"的直接证据：分数随输入下标递减时，若实现按分数升序排、
    或者根本没排，断言都会红。

    两次调用必须共用**同一份** `sources`：`_source()` 每次生成新的 uuid4，
    各建一次语料会让 `source_id` 对不上，于是"顺序相同"这件事永远为假、
    断言也就抓不到任何东西。
    """
    _keyed(monkeypatch)
    _use(monkeypatch, RerankHttp(_reversed))
    sources = _corpus()

    pool = _search_parent_core(sources, QUERY, 3, None, depth=12)
    hits = search_parent(sources, QUERY, 3)
    assert _spans(hits) == _spans(list(reversed(pool))[:3])
    assert all(hit.rerank_score is not None for hit in hits)


def test_cost_pool_for_search_multi(monkeypatch):
    """多路：池子同样是 `pool_size`，且**只调一次** reranker（不是每路一次）。"""
    _keyed(monkeypatch)
    fake = _use(monkeypatch, RerankHttp())
    queries = ["预算审批", "谁签核预算", "报销要什么材料"]

    hits = asyncio.run(search_multi(_corpus(), queries, 3))
    assert len(fake.calls) == 1, "多路各自重排会让调用次数变成路数倍"
    assert len(fake.calls[0]["payload"]["input"]["documents"]) == rerank.pool_size(3)
    assert len(hits) == 3
    # 打分用的 query 是**第一路**（调用方放的原始问题），不是扩展问法
    assert fake.calls[0]["payload"]["input"]["query"] == "预算审批"


# ---------------------------------------------------------------- 4 降级四种


@pytest.mark.parametrize("case", ["no_key", "timeout", "server_error", "bad_json",
                                  "results_missing", "count_mismatch"])
def test_fallback_returns_original_prefix(monkeypatch, case):
    """四种降级（外加两种解析失败）：返回原顺序的前 `top_k` 条，`rerank_score` 全是 None。"""
    calls: list[dict] = []
    if case == "no_key":
        monkeypatch.setenv("MINDMAP_RERANK", "1")
        monkeypatch.setattr("app.knowledge.rerank.httpx.Client", _forbidden)
    else:
        _keyed(monkeypatch)
        behaviours = {
            "timeout": lambda _payload: (_ for _ in ()).throw(httpx.ReadTimeout("模拟超时")),
            "server_error": lambda _payload: httpx.Response(
                500, json={"code": "InternalError", "message": "down"}),
            "bad_json": lambda _payload: httpx.Response(200, content=b"<html>not json</html>"),
            "results_missing": lambda _payload: httpx.Response(200, json={"output": {}}),
            "count_mismatch": lambda payload: _native_response(
                {**payload, "input": {**payload["input"],
                                      "documents": payload["input"]["documents"] + ["多一条"]}},
                [0.5] * (len(payload["input"]["documents"]) + 1)),
        }
        fake = _use(monkeypatch, RerankHttp(behaviours[case]))
        calls = fake.calls

    sources = _corpus()
    pool = _search_parent_core(sources, QUERY, 3, None, depth=12)
    hits = search_parent(sources, QUERY, 3)

    assert _spans(hits) == _spans(pool[:3]), f"{case}：降级必须回到原顺序的前 top_k"
    assert all(hit.rerank_score is None for hit in hits)
    if case == "no_key":
        assert not calls, "没 key 时不该发请求"
    else:
        assert len(calls) == 1, f"{case}：超时/5xx 都不重试，只该发一次"


def test_no_request_when_pool_is_not_larger_than_top_k(monkeypatch):
    """候选池不比 `top_k` 多时不调用 rereanker：没得挑，换顺序只会把无关的排前。"""
    _keyed(monkeypatch)
    fake = _use(monkeypatch, RerankHttp())
    # 材料只有一个父块：`search_parent(top_k=3)` 折出来就 1–2 条，池子天然不足
    hits = search_parent([_source("预算审批由部门负责人签核。" * 6)], QUERY, 3)
    assert not fake.calls
    assert all(hit.rerank_score is None for hit in hits)


# ---------------------------------------------------------------- 5 只重排不改证据


def test_shape_and_spans_unchanged(monkeypatch):
    """重排后每条的坐标、原文、`score`、`leaf_*` 与重排前逐字段相同。

    这是"只重排不改变证据"的保证：顺序可以变，证据不能变。比的是**同一集合**
    （按 span 排序后比），因为顺序本身就是要变的东西。
    """
    _keyed(monkeypatch)
    _use(monkeypatch, RerankHttp(_reversed))

    sources = _corpus()
    pool = _search_parent_core(sources, QUERY, 3, None, depth=12)
    hits = search_parent(sources, QUERY, 3)

    def keyed(entry):
        return (entry.source_id, entry.start, entry.end)

    before = {keyed(hit): hit for hit in pool}
    assert {keyed(hit) for hit in hits} <= set(before)
    for hit in hits:
        original = before[keyed(hit)]
        assert hit.quote == original.quote
        assert (hit.leaf_start, hit.leaf_end) == (original.leaf_start, original.leaf_end)
        assert hit.score == original.score
        assert hit.score_bm25 == original.score_bm25
        assert hit.score_vector == original.score_vector
        assert hit.method == original.method
        assert hit.rerank_score is not None
        # 引用区间仍逐字对应原文
        assert hit.quote


def test_rerank_score_does_not_overwrite_score(monkeypatch):
    """`score` 保留融合口径，`rerank_score` 另开一列——两者不互相覆盖。

    用一手造的命中直接打 `rerank.rerank`：真实语料里同一批父块的 RRF 分常常
    完全相同（同一段文本被切成等长块），端到端断言"顺序 != score 降序"会输在
    "两者都是同一个顺序"上，抓不到任何东西。这里把 score 设成与重排**相反**的
    单调递减，覆盖 score 的实现会让两列同时反过来。
    """
    _keyed(monkeypatch)
    _use(monkeypatch, RerankHttp(_reversed))
    hits = [
        EvidenceHit(source_id="s", start=index * 10, end=index * 10 + 9,
                    quote=f"第{index}段原文", score=0.5 - index * 0.1)
        for index in range(5)
    ]
    result = rerank.rerank("查什么", hits, 2)

    assert [hit.quote for hit in result] == ["第4段原文", "第3段原文"]
    # score 是原来的融合分（未动），rerank_score 是 0–1 的模型分
    assert [hit.score for hit in result] == [pytest.approx(0.5 - 4 * 0.1),
                                             pytest.approx(0.5 - 3 * 0.1)]
    assert [hit.rerank_score for hit in result] == [pytest.approx(4 / 6),
                                                   pytest.approx(3 / 6)]


# ---------------------------------------------------------------- 6 端点与请求体


def test_endpoint_derived_from_maas_base(monkeypatch):
    """OpenAI 兼容 base → 剥掉尾巴、拼原生 rerank 路由。"""
    _keyed(monkeypatch, base="https://ws.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")
    fake = _use(monkeypatch, RerankHttp())
    search_parent(_corpus(), QUERY, 3)
    assert fake.calls[0]["url"] == (
        "https://ws.cn-beijing.maas.aliyuncs.com"
        "/api/v1/services/rerank/text-rerank/text-rerank")
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer sk-test"


def test_endpoint_falls_back_to_public_domain_on_404(monkeypatch):
    """MaaS 网关没有 rerank 路由（404）→ 退到公共域名，且**只多打一次**。

    第二次调用不该再打那个 404 的地址：`_UNUSABLE` 记着它。否则每次检索都要为
    一个已知不存在的路由付一次往返。
    """
    _keyed(monkeypatch, base="https://gateway.example.com/v1")
    fake = _use(monkeypatch, RerankHttp(routes={
        # 只要请求打向私网网关就回 404；公共域名落到默认 handler（正常打分）
        "gateway.example.com/api/v1/services/rerank/text-rerank/text-rerank":
            httpx.Response(404, json={"code": "NotFound", "message": "no route"}),
    }))

    search_parent(_corpus(), QUERY, 3)
    urls = [call["url"] for call in fake.calls]
    assert urls == [
        "https://gateway.example.com/api/v1/services/rerank/text-rerank/text-rerank",
        f"{rerank.PUBLIC_ROOT}/api/v1/services/rerank/text-rerank/text-rerank",
    ]
    assert rerank._UNUSABLE == {
        "https://gateway.example.com/api/v1/services/rerank/text-rerank/text-rerank"}

    fake.calls.clear()
    search_parent(_corpus(), QUERY, 3)
    assert [call["url"] for call in fake.calls] == [
        f"{rerank.PUBLIC_ROOT}/api/v1/services/rerank/text-rerank/text-rerank"]


def test_request_body_shape(monkeypatch):
    """请求体：原生形状，`top_n` = 候选数，`return_documents=false`。"""
    _keyed(monkeypatch)
    fake = _use(monkeypatch, RerankHttp())
    search_parent(_corpus(), QUERY, 3)

    payload = fake.calls[0]["payload"]
    documents = payload["input"]["documents"]
    assert payload["model"] == "qwen3-rerank"
    assert payload["input"]["query"] == QUERY
    assert isinstance(documents, list) and documents and all(isinstance(d, str) for d in documents)
    assert payload["parameters"] == {"return_documents": False, "top_n": len(documents)}
    # 父块（450–700 字）原样上送：重排看到的就是模型将要读的那段上下文
    assert all(100 <= len(document) <= 700 for document in documents)


def test_timeout_is_eight_seconds_and_single_attempt(monkeypatch):
    """超时 8 秒、只发一次。这两个数写错，一次检索会被拖成十几秒。"""
    assert rerank.TIMEOUT == 8.0
    _keyed(monkeypatch)
    fake = _use(monkeypatch, RerankHttp(
        lambda _payload: (_ for _ in ()).throw(httpx.ReadTimeout("模拟超时"))))
    search_parent(_corpus(), QUERY, 3)
    assert len(fake.calls) == 1


# ---------------------------------------------------------------- 7 日志不含内容


def test_logs_carry_no_query_or_fragment(monkeypatch, caplog):
    """日志只有条数与耗时：query 与片段是材料原文，一律不进日志。"""
    _keyed(monkeypatch)
    _use(monkeypatch, RerankHttp())
    caplog.set_level(logging.DEBUG, logger="app.knowledge.rerank")

    hits = search_parent(_corpus(), QUERY, 3)
    assert hits
    messages = [record.getMessage() for record in caplog.records]
    assert messages, "成功路径至少要留一条 info"
    for message in messages:
        assert QUERY not in message
        for hit in hits:
            assert hit.quote[:12] not in message


def test_scores_helper_is_exposed_for_evaluation(monkeypatch):
    """`score_documents` 给评估脚本用：返回**与输入等长、未排序**的分数。

    评估要看分数分布（父块 450–700 字之间能不能拉开），排好序的 hits 已经丢了
    "哪个分数对应哪个候选"的对应关系，所以这一层必须单独可用。
    """
    _keyed(monkeypatch)
    _use(monkeypatch, RerankHttp(_reversed))
    scores = rerank.score_documents(QUERY, ["甲", "乙", "丙"])
    assert len(scores) == 3
    assert scores == sorted(scores)          # 倒序替身：原地递增，未排序
    assert all(0.0 <= value <= 1.0 for value in scores)


def test_hit_shape_has_rerank_field():
    """`EvidenceHit` 新增一列，默认 None——旧调用方构造不受影响。"""
    hit = EvidenceHit(source_id="s", start=0, end=1, quote="字", score=0.5)
    assert hit.rerank_score is None
    assert "rerank_score" in hit.model_dump()
