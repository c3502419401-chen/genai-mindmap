"""多路并行检索与融合（`search_multi`）的行为。

全部离线：`embed_texts` 换成可编程替身，向量缓存落在 tmp 目录的 SQLite 里，
任何一条用例都不发真实网络请求。

第一组用例不是"再测一遍功能"：它把 `41dda8e` 的旧 `service.py` 整份载进来，
与新的 `search_sources` 逐字段对比。抽取中间层（`_collect_chunks` /
`_single_search`）之后那条对外行为必须**逐字节不变**，而这件事没有任何
运行时信号——只有这组对比能钉住。
"""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.agent import tools as agent_tools
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.contracts.source import Source, SourceKind
from app.knowledge import embedding, service, vector_cache
from app.knowledge.embedding import EmbeddingError
from app.knowledge.query_plan import QueryPlan
from app.knowledge.service import (
    MAX_ROUTES,
    RRF_K,
    search_multi,
    search_sources,
)

# 写死，不跟随 HEAD：HEAD 之后 `service.py` 就被这一轮改过了，拿 HEAD 对比
# 等于拿自己跟自己比，永远是绿的。
BASELINE_REV = "41dda8e"

REPO_ROOT = Path(__file__).resolve().parents[2]

@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    # `conftest.py` 已清空凭证并把开关关掉；这个文件要测的就是向量路，
    # 显式打开。"没有 key"的用例靠是否调用 `_keyed()` 区分，不靠全局默认值。
    monkeypatch.setenv("MINDMAP_HYBRID_SEARCH", "1")
    monkeypatch.setenv("MINDMAP_DB_PATH", str(tmp_path / "vectors.db"))
    # 重试退避在测试里没有意义，别真的等 0.5 + 1.0 秒
    monkeypatch.setattr(embedding, "RETRY_BACKOFF", 0.0)


def run(coro):
    return asyncio.run(coro)


def _source(text: str) -> Source:
    return Source(kind=SourceKind.text, status="parsed", text=text)


def _pad(text: str, size: int) -> str:
    """补到至少 `size` 字。填充句里不含任何被测关键词，只加长度不加词频。"""
    filler = "本段为填充说明，不含其他主题词。"
    while len(text) < size:
        text += filler
    return text


# 四个来源各 200 字上下：每个来源 < `CHUNK_MAX_CHARS`，所以每个恰好一块。
BOTH = _pad("预算审批由部门负责人签核。费用报销走同一套流程。" * 3, 220)
ONLY_BUDGET = _pad("预算审批的额度按金额分级。", 260)
ONLY_COST = _pad("费用报销需要附上发票。", 260)
OTHER = _pad("团队建设活动安排在季度末。", 220)


def _four_sources() -> list[Source]:
    return [_source(BOTH), _source(ONLY_BUDGET), _source(ONLY_COST), _source(OTHER)]


# 多路退化复现用的材料：十个互不相关的主题各重复三遍，拼成约 1400 字的长文
# （切成 3 块）。原始问题指向第 1 块的「预算审批」，四条扩展问法指向第 2、3 块的
# 超导/蛋白/合同/天文。四条扩展问法与材料**全都有交集**——它们是这份材料里合法
# 的主题，只是不是用户要的那一段，所以"按材料锚定过滤"在这里一条都筛不掉。
TOPICS = (
    "预算审批流程由部门负责人核准，超过额度的部分必须上报总经理复审，财务只在核准后执行付款。",
    "烹饪时美拉德反应让食材表面产生褐色风味，温度超过一百四十度后反应速率明显加快，锅具预热很关键。",
    "城市地铁的客流调度依赖实时刷卡数据，早高峰的运力缺口靠区间车与限流共同消化，站厅引导也要配合。",
    "蛋白质折叠的自由能景观由氨基酸序列与溶剂环境共同决定，错误折叠会聚集成淀粉样纤维并致病。",
    "超导体的临界温度取决于晶格振动与电子配对强度，高压下氢化物体系已接近室温区间，机制仍有争议。",
    "合同条款必须经法务审核，违约金上限与争议解决地另行约定，签署前还需确认对方授权链条完整。",
    "天文观测显示星系旋转曲线偏离牛顿预测，暗物质晕的质量分布是目前最主流的解释，但尚未直接探测。",
    "疫苗冷链运输要求全程两到八摄氏度，温度记录仪的数据要在签收时逐段核对，超温批次一律退回。",
    "森林碳汇的计量需要样方调查与遥感校验，灌木与土壤有机碳通常单独建账，重复计算是常见问题。",
    "量子隧穿解释了扫描隧道显微镜的分辨率，针尖与样品的波函数重叠决定了隧穿电流，反馈回路保持间距。",
)


def _mixed_material() -> str:
    return "\n\n".join(topic * 3 for topic in TOPICS)


def _corpus() -> list[Source]:
    """对比用的语料：有长文（切出多块）、有短文、有空来源。"""
    return [
        _source(_pad("预算审批由部门负责人签核，超过一定金额上报总经理。"
                     "费用报销需要附发票。", 320)),
        _source(_pad("费用报销的时限是三十个自然日，逾期不再受理。", 260)),
        _source(_pad("团队建设活动安排在季度末，预算另行列支。", 240)),
        _source(_pad("采购合同必须经过法务审核，金额超过五十万元另行招标。" * 6, 900)),
        _source("   "),
    ]


def _keyed(monkeypatch) -> None:
    """配上走向量路所需的最小环境：专用 key + MaaS base（openai 兼容协议）。"""
    monkeypatch.setenv("MINDMAP_EMBEDDING_API_KEY", "sk-test")
    monkeypatch.setenv("MINDMAP_EMBEDDING_BASE_URL",
                       "https://maas.example.com/compatible-mode/v1")


def _vector_of(text: str) -> list[float]:
    """按关键词给一个 4 维单位向量，方便手算余弦（单位向量下余弦就是点积）。"""
    if "团队建设" in text:
        return [0.0, 1.0, 0.0, 0.0]
    if "预算审批" in text or "签核" in text:
        return [1.0, 0.0, 0.0, 0.0]
    if "费用报销" in text:
        return [0.0, 0.0, 1.0, 0.0]
    return [0.0, 0.0, 0.0, 1.0]


def _fake_embed(monkeypatch, *, delay: float = 0.0,
                fail_on: str | None = None) -> list[dict]:
    """替换 `service.embed_texts`：记录每次调用的线程/窗口/入参，可注入延迟与失败。

    记录的是**发起**而不是成功：并发用例要证明的是"同时发出去"，不是"都成功"。
    """
    calls: list[dict] = []

    def fake(texts):
        call = {
            "thread": threading.current_thread().name,
            "start": time.perf_counter(),
            "texts": list(texts),
        }
        calls.append(call)
        if delay:
            time.sleep(delay)
        vectors = []
        for text in texts:
            if fail_on is not None and text == fail_on:
                # 429 在 `_post` 里不重试，直接抛 EmbeddingError，与线上一致
                call["end"] = time.perf_counter()
                raise EmbeddingError("HTTP 429：rate limit")
            vectors.append(_vector_of(text))
        call["end"] = time.perf_counter()
        return vectors

    monkeypatch.setattr(service, "embed_texts", fake)
    return calls


def _load_baseline_module(tmp_path):
    """把 `BASELINE_REV` 的 `service.py` 载成一个独立模块。"""
    try:
        source = subprocess.run(
            ["git", "show", f"{BASELINE_REV}:backend/app/knowledge/service.py"],
            cwd=REPO_ROOT, capture_output=True, check=True, text=True,
            encoding="utf-8",
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:      # pragma: no cover
        pytest.skip(f"读不到 {BASELINE_REV} 的 service.py：{exc}")
    path = tmp_path / "service_baseline.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        f"service_baseline_{BASELINE_REV}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _ctx(sources: list[Source]) -> RunContext:
    request = AgentRunRequest(
        goal="做一张导图", intent="expand", sources=sources,
        budget=RunBudget(max_model_calls=5, max_output_tokens=32768),
    )
    return RunContext(request=request,
                      sources_by_id={source.id: source for source in sources})


def _rag_call(question: str) -> ToolCall:
    return ToolCall(call_id="c-rag", name="rag_query",
                    arguments={"question": question, "top_k": 3})


# ---------------------------------------------------------------- 1 行为不变


def test_search_sources_is_byte_identical_to_baseline(tmp_path, monkeypatch):
    """3 个 query × 3 个 top_k 逐字段对比 `41dda8e` 的实现。

    混合检索真正跑起来（有 key、有向量）才有对比价值：只比纯 BM25 的话，
    向量路的截断、排序、`method` 标注都没被覆盖到。

    基线模块的 `EvidenceHit` 没有 `leaf_start/leaf_end`（那是后来两级切块加的）、
    也没有 `rerank_score`（重排加的），所以拿它的 dump 之前先补上三个 None：
    比的仍然是逐键逐值，当前实现若在 `search_sources` 里把这三个字段填成非 None，
    `==` 立刻会红。
    """
    baseline = _load_baseline_module(tmp_path)
    _keyed(monkeypatch)
    calls: list[dict] = []

    def fake(texts):
        calls.append({"texts": list(texts)})
        return [_vector_of(text) for text in texts]

    # 两个模块各自持有一个 `embed_texts` 名字，必须都换掉才是同一套向量
    monkeypatch.setattr(service, "embed_texts", fake)
    monkeypatch.setattr(baseline, "embed_texts", fake)

    sources = _corpus()
    for question in ("预算审批流程", "费用报销要什么材料", "团队建设"):
        for top_k in (1, 3, 5):
            expected = []
            for hit in baseline.search_sources(sources, question, top_k):
                entry = hit.model_dump()
                entry.setdefault("leaf_start", None)
                entry.setdefault("leaf_end", None)
                entry.setdefault("rerank_score", None)
                entry.setdefault("origin", None)
                expected.append(entry)
            actual = [hit.model_dump()
                      for hit in search_sources(sources, question, top_k)]
            assert actual == expected, f"{question} / top_k={top_k}"
    assert calls, "这组对比必须真的走过向量路"


# ---------------------------------------------------------------- 2 单路等价


def test_single_route_matches_search_sources_without_vectors():
    """只有一路且没有向量时，与 `search_sources` 逐字段相同（含 score 数值）。"""
    sources = _four_sources()
    for top_k in (1, 3, 5):
        expected = [hit.model_dump()
                    for hit in search_sources(sources, "预算审批", top_k)]
        actual = [hit.model_dump()
                  for hit in run(search_multi(sources, ["预算审批"], top_k))]
        assert actual == expected


def test_single_route_matches_search_sources_with_vectors(monkeypatch):
    """带向量的单路同样逐字段相同：退化不是"差不多"，是同一个函数。"""
    _keyed(monkeypatch)
    _fake_embed(monkeypatch)
    sources = _four_sources()
    expected = [hit.model_dump() for hit in search_sources(sources, "预算审批", 3)]
    actual = run(search_multi(sources, ["预算审批"], 3))
    assert [hit.model_dump() for hit in actual] == expected
    assert all(hit.method == "hybrid" for hit in actual)


# ---------------------------------------------------------------- 3 多路累加

def test_scores_accumulate_across_routes():
    """两个 query 都命中的块 = 1/61 + 1/61；只被一个命中的块 = 1/62。

    没有向量的环境最容易手算：两路都只有 BM25，名次直接决定分数。
    """
    sources = _four_sources()
    hits = run(search_multi(sources, ["预算审批", "费用报销"], 5))
    both = next(hit for hit in hits if hit.quote.startswith("预算审批由部门"))
    assert both.score == pytest.approx(round(2 / (RRF_K + 1), 6))

    budget_only = next(hit for hit in hits if hit.quote.startswith("预算审批的额度"))
    assert budget_only.score == pytest.approx(round(1 / (RRF_K + 2), 6))
    assert both.score > budget_only.score


# ---------------------------------------------------------------- 3b 保底席位


def test_reserved_seat_keeps_the_original_question_on_top():
    """实测复现：五路累加会把原始问题的答案顶掉，保底席位把它按回第 1。

    修复前的输出（同一份材料、`top_k=3`）：

        2 路：预算 0.0323 | 蛋白 0.0164 | 天文 0.0161   → 预算 第 1
        5 路：蛋白 0.0653 | 预算 0.0487 | 天文 0.0323   → 预算 第 2  ← 退化

    四条扩展问法与材料**全都有交集**（材料里确实有超导/蛋白/合同/天文），
    所以"扩展 query 与材料共享 bigram"这种锚定一条都筛不掉——退化的原因是
    票数，只能从融合结构上解决。
    """
    sources = [_source(_mixed_material())]
    question = "预算审批流程谁核准"
    extras = ["超导体的临界温度是多少", "蛋白质折叠的错误构象",
              "合同违约金上限", "星系旋转曲线的观测"]

    # 单路：真答案本来就在第一位
    single = search_sources(sources, question, 3)
    assert single[0].quote.startswith("预算审批")

    hits = run(search_multi(sources, [question, *extras], 3))
    assert hits[0].quote.startswith("预算审批")
    # 退化条件仍然成立：第 2 名的累加分比第 1 名高，纯按分数排就会换位。
    # 这一条同时说明保底席位真的在起作用，而不是碰巧没退化。
    assert hits[1].score > hits[0].score
    assert hits[1].quote.startswith("蛋白质折叠")

    # top_k=1 时只保一席，扩展路完全不能插队
    assert run(search_multi(sources, [question, *extras], 1))[0].quote.startswith("预算审批")


def test_reserved_seat_yields_when_primary_route_has_few_hits():
    """原 query 只命中一块时，保底只占一席，其余名额照样给扩展路。"""
    sources = [_source(_mixed_material())]
    hits = run(search_multi(
        sources, ["预算审批流程谁核准", "超导体的临界温度是多少"], 3))
    assert len(hits) >= 2
    assert hits[0].quote.startswith("预算审批")
    assert any("超导体的临界温度" in hit.quote for hit in hits)


# ---------------------------------------------------------------- 4 切块一次


def test_chunks_are_cut_exactly_once(monkeypatch):
    """2 路查询、1 份长材料：`_chunk_spans` 只调用一次。"""
    calls: list[str] = []
    original = service._chunk_spans

    def counting(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(service, "_chunk_spans", counting)
    long_source = _source(_pad("预算审批的流程说明。" * 10, 1600))
    run(search_multi([long_source], ["预算审批", "费用报销"], 3))
    assert len(calls) == 1


# ---------------------------------------------------------------- 5 并行


def test_queries_embed_concurrently(monkeypatch):
    """三个 query 的 embedding 必须同时在飞，而不是串行累加。"""
    _keyed(monkeypatch)
    queries = ("预算审批", "费用报销", "审批流程")
    calls = _fake_embed(monkeypatch, delay=0.15)
    sources = [_source(BOTH), _source(ONLY_BUDGET)]

    started = time.perf_counter()
    run(search_multi(sources, list(queries), 3))
    elapsed = time.perf_counter() - started

    windows = [c for c in calls if len(c["texts"]) == 1 and c["texts"][0] in queries]
    assert len(windows) == len(queries)
    # 所有 query 调用的时间窗有公共交集 = 它们真的同时在跑
    assert max(c["start"] for c in windows) < min(c["end"] for c in windows)
    # 串行至少 3 × 0.15；并发只需要一个 0.15 加上调度开销
    assert elapsed < 0.15 * 2.2
    assert all(c["thread"] != threading.main_thread().name for c in windows)


# ---------------------------------------------------------------- 6 缓存在主线程


def test_cache_access_never_leaves_the_main_thread(monkeypatch):
    """缓存读写留主线程：线程池只该跑 HTTP，并发度才只有"几个网络往返"。"""
    _keyed(monkeypatch)
    _fake_embed(monkeypatch)
    seen: list[bool] = []
    real_get = vector_cache.get_vectors
    real_put = vector_cache.put_vectors

    def get(hashes, model):
        seen.append(threading.current_thread() is threading.main_thread())
        return real_get(hashes, model)

    def put(items, model):
        seen.append(threading.current_thread() is threading.main_thread())
        return real_put(items, model)

    monkeypatch.setattr(vector_cache, "get_vectors", get)
    monkeypatch.setattr(vector_cache, "put_vectors", put)
    run(search_multi(_four_sources(), ["预算审批", "费用报销"], 3))
    assert seen
    assert all(seen)


# ---------------------------------------------------------------- 7 截断


def test_routes_are_capped_at_six(monkeypatch):
    """10 个 query 只跑 6 路，且真的少跑（不是截断结果）。"""
    calls: list[str] = []
    real = service._bm25_scores

    def counting(query, quotes):
        calls.append(query)
        return real(query, quotes)

    monkeypatch.setattr(service, "_bm25_scores", counting)
    queries = [f"预算审批第{index}种问法" for index in range(10)]
    run(search_multi(_four_sources(), queries, 3))
    assert len(calls) == MAX_ROUTES == 6


# ---------------------------------------------------------------- 8 空输入


def test_empty_queries_returns_empty_without_touching_anything(monkeypatch):
    monkeypatch.setattr(service, "_chunk_spans",
                        lambda text: pytest.fail("没有 query 不该切块"))
    monkeypatch.setattr(service, "embed_texts",
                        lambda texts: pytest.fail("没有 query 不该算向量"))
    assert run(search_multi(_four_sources(), [], 3)) == []


# ---------------------------------------------------------------- 9 无向量


def test_bm25_only_routes_still_fuse(monkeypatch):
    """没有 key 时每路只有 BM25：结果非空、逐条 lexical、分数仍然累加。"""
    assert service.embedding_configured() is False
    hits = run(search_multi(_four_sources(), ["预算审批", "费用报销"], 3))
    assert hits
    assert all(hit.method == "lexical" for hit in hits)
    both = next(hit for hit in hits if hit.quote.startswith("预算审批由部门"))
    assert both.score == pytest.approx(round(2 / (RRF_K + 1), 6))


# ---------------------------------------------------------------- 10 HYDE


def test_hyde_route_uses_vectors_only(monkeypatch):
    """hyde 那一路不调用 BM25；它靠余弦把一个字面完全不同的块捞回来。"""
    _keyed(monkeypatch)
    _fake_embed(monkeypatch)
    seen: list[str] = []
    real = service._bm25_scores

    def counting(query, quotes):
        seen.append(query)
        return real(query, quotes)

    monkeypatch.setattr(service, "_bm25_scores", counting)
    hyde = "假设性答案：团队建设在季度末安排，费用另行列支。"
    hits = run(search_multi(_four_sources(), ["预算审批"], 5, hyde=hyde))

    # BM25 只跑过真实问法那一路
    assert seen == ["预算审批"]
    # 向量路把「团队建设」那块捞了回来：它只与 hyde 的假答案同向
    assert any(hit.quote.startswith("团队建设") for hit in hits)
    assert any(hit.method == "hybrid" for hit in hits)


def test_one_route_hitting_429_degrades_to_bm25(monkeypatch):
    """某一路 429：那一路只剩 BM25，其余路照常，整批仍然是 hybrid。

    这是"六路"的真实含义——发起上限，不是成功上限。`_post` 不对 429 重试。
    """
    _keyed(monkeypatch)
    _fake_embed(monkeypatch, fail_on="团队建设")
    hits = run(search_multi(
        _four_sources(), ["预算审批", "团队建设"], 5))

    # 被限流那一路的候选只带着 BM25 名次的分，没有余弦加成
    assert hits
    assert any(hit.method == "hybrid" for hit in hits)
    other = next(hit for hit in hits if hit.quote.startswith("团队建设"))
    assert other.score == pytest.approx(round(1 / (RRF_K + 1), 6))


# ---------------------------------------------------------------- 11/12 _rag 接入


def test_rag_without_plan_uses_single_search_only(monkeypatch):
    """`plan is None` 时走单路检索：一次 `search_parent`、零次 `search_multi`。

    打桩打 `search_parent` 而不是 `search_sources`：`_rag` 直接调的是前者，开关
    关着时它内部才转到后者——打内层等于绕过了 `_rag` 的真实调用点。
    """
    sources = _four_sources()
    ctx = _ctx(sources)
    with patch.object(agent_tools, "plan_query", AsyncMock(return_value=None)), \
         patch.object(agent_tools, "search_parent", return_value=[]) as single, \
         patch.object(agent_tools, "search_multi", AsyncMock()) as multi:
        result = run(agent_tools.build_registry("expand", has_sources=True).execute(
            _rag_call("预算审批"), ctx))

    assert result.ok
    single.assert_called_once()
    multi.assert_not_called()


def test_rag_with_llm_plan_uses_multi_and_keeps_the_original_question(monkeypatch):
    """计划来自模型时走多路，`queries[0]` 必须是用户原话。"""
    sources = _four_sources()
    ctx = _ctx(sources)
    plan = QueryPlan(
        corrected="预算审批的流程", intents=["谁签核预算审批"],
        hyde="预算审批按金额分级，部门负责人签核。", subqueries=[],
        source="llm",
    )
    with patch.object(agent_tools, "plan_query", AsyncMock(return_value=plan)), \
         patch.object(agent_tools, "search_parent") as single, \
         patch.object(agent_tools, "search_multi", AsyncMock(return_value=[])) as multi:
        result = run(agent_tools.build_registry("expand", has_sources=True).execute(
            _rag_call("预算审批"), ctx))

    assert result.ok
    single.assert_not_called()
    multi.assert_awaited_once()
    queries = multi.await_args.args[1]
    assert queries[0] == "预算审批"
    assert "预算审批的流程" in queries
    assert "谁签核预算审批" in queries
    assert multi.await_args.kwargs["hyde"] == plan.hyde
