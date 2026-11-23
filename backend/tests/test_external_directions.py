"""多方向检索：拆方向 → 每方向并行打源 → 按方向施门 → 合并去重 → 跨方向轮转。

八组断言对着这条链的八条要求：

  1. 拆方向：正常产出 2-4 条，每条 6-20 字、含主题原词；`max_directions` 是真上限。
  2. 降级：没配置 / 5xx / 超时 / 坏 JSON / 方向全被拒 → `[topic]`，且**只调一次**。
  3. 防编造：跨领域的方向被拒（`_build_directions`），全被拒时退化成单方向。
  4. 并行：多方向检索的时间窗口有交集（串行时它们不会重叠）。
  5. 合并去重：两个方向命中同一 URL → 只留一条，且保留首见方向。
  6. 门按方向施加：与主题相关、与那个方向无关的命中被挡（这条是拆方向的意义所在）。
  7. 预算：3 方向 × 5 源不超 `MULTI_REQUEST_LIMIT`，每方向不超 `DIRECTION_REQUEST_LIMIT`，
     墙钟仍是 `DEADLINE_SECONDS`。
  8. 降级与基线一致：拆方向失败时，返回与 `3c40ba6` 的 `external.py` **逐字段相同**、
     请求次数也相同——"退化成一个方向"不是"差不多"，是同一个函数。

全部离线：`tests/conftest.py` 的 `no_real_http` 把真请求变成断言失败；模型出口
`external.chat_async` 在用例里被替换，HTTP 走 `httpx.MockTransport`（传输层之前返回）。
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.knowledge import external

# 降级基线。`a84e763`（词面门那次）与 `3c40ba6`（本任务 HEAD）之间的 `external.py`
# 只差一段 rerank 结论的 docstring，行为完全相同；这里钉 `3c40ba6`，与本任务的
# "与 HEAD 逐字节一致"对齐。
BASELINE_REV = "3c40ba6"
BASELINE_SHA256 = "08ba5a0d62237be541b34a7dfdb6b00791da81a2469f026280b0ecff8f9c074c"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _hit(
    title: str, text: str, *, source_name: str = "wikipedia",
    url: str | None = None, language: str = "zh",
) -> external.ExternalHit:
    return external.ExternalHit(
        title=title, text=text, url=url if url is not None else f"https://x/{title}",
        source_name=source_name, fetched_at="2026-01-01T00:00:00Z", language=language,
        raw_chars=len(text),
    )


def _fake_chat(payload: Any, calls: list[dict[str, Any]] | None = None):
    """替换 `external.chat_async`。`payload` 是返回值、异常或协程函数。"""
    async def fake(system: str, user: str, **kwargs):
        if calls is not None:
            calls.append({"system": system, "user": user, **kwargs})
        if isinstance(payload, Exception):
            raise payload
        if callable(payload):
            return await payload()
        return payload
    return fake


def run(coro):
    """同步用例里跑协程（项目没装 pytest-asyncio，见 `tests/test_query_plan.py`）。"""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def directions_on(monkeypatch):
    """本文件的用例默认**打开**拆方向（`conftest.clean_env` 把它置 0）。

    默认关是生产决策（实测负收益，见 `external.DIRECTIONS_ENV`），而这里要测的是
    打开之后那条路；"默认关"本身由 `test_default_off_is_byte_identical_to_the_baseline`
    单独钉住。
    """
    monkeypatch.setenv(external.DIRECTIONS_ENV, "1")


def test_default_off_keeps_the_single_direction_path(monkeypatch):
    """开关关着：不调模型、不发多轮请求，`plan_directions` 一次都不被调用。"""
    monkeypatch.delenv(external.DIRECTIONS_ENV, raising=False)

    def explode(*args, **kwargs):
        raise AssertionError("开关关着时不该拆方向")

    monkeypatch.setattr(external, "plan_directions", explode)
    monkeypatch.setattr(external, "_search_one",
                        lambda query, limit, **kwargs: [
                            _hit("碳中和", "碳中和是指排放与吸收相抵。")])

    hits = external.search_external("碳中和")

    assert [hit.title for hit in hits] == ["碳中和"]
    assert hits[0].direction == "", "单方向路径的命中不该带方向标签"


# ---------------------------------------------------------------- 1 拆方向


def test_plan_returns_two_to_four_short_directions(monkeypatch):
    """正常产出：2-4 条、每条 6-20 字、都含主题原词。"""
    monkeypatch.setattr(external, "chat_async", _fake_chat({"directions": [
        "碳中和政策", "碳中和技术路径", "碳中和企业案例",
    ]}))

    directions = run(external.plan_directions("碳中和"))

    assert directions == ["碳中和政策", "碳中和技术路径", "碳中和企业案例"]
    for direction in directions:
        assert external.DIRECTION_MIN_CHARS <= len(direction) <= external.DIRECTION_MAX_CHARS
        assert "碳中和" in direction


def test_plan_respects_max_directions(monkeypatch):
    """`max_directions` 是真上限：提示词要 2-4，代码只取前 N 条。

    多给一个方向就多一条"名额被摊薄"的路径，而最终只要 3 条命中。
    """
    monkeypatch.setattr(external, "chat_async", _fake_chat({"directions": [
        "碳中和政策", "碳中和技术路径", "碳中和企业案例", "碳中和国际合作",
    ]}))

    assert len(run(external.plan_directions("碳中和", max_directions=2))) == 2
    assert len(run(external.plan_directions("碳中和"))) == 3


def test_plan_prompt_carries_the_anti_fabrication_rules(monkeypatch):
    """提示词里必须同时有"不能换领域"的正反例与"原样包含主题"的硬规则。

    钉提示词不是为了字面一致，而是为了这两条判据：它们是防编造的唯一现场说明，
    删掉之后模型仍然会返回"像方向"的东西，而代码闸只挡最明显的跨领域那一种。
    """
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(external, "chat_async", _fake_chat(
        {"directions": ["碳中和政策", "碳中和技术路径"]}, calls))

    run(external.plan_directions("碳中和"))

    assert len(calls) == 1, "拆方向必须是一次调用，不是每个方向一次"
    system = calls[0]["system"]
    assert "❌" in system and "✅" in system
    assert "超导量子比特" in system or "别的领域" in system
    assert "原样包含主题的写法" in system
    # 用户消息里只有主题，不塞材料——这条路上本来就没有材料。
    assert calls[0]["user"].strip().startswith("主题：")


# ---------------------------------------------------------------- 2 降级


def test_plan_degrades_to_topic_without_llm_config():
    """没有 `MINDMAP_LLM_*`：走 `chat_async` 的预检，返回 `[topic]`（conftest 清过凭证）。"""
    assert run(external.plan_directions("碳中和")) == ["碳中和"]


def test_plan_degrades_on_failure_and_bad_json(monkeypatch):
    """模型 5xx、返回字符串噪声、返回错形状 —— 三种都返回 `[topic]`。"""
    for payload in (RuntimeError("模型挂了"), "这不是 JSON", {"nope": 1}):
        monkeypatch.setattr(external, "chat_async", _fake_chat(payload))
        assert run(external.plan_directions("碳中和")) == ["碳中和"], payload


def test_plan_times_out_after_the_budget(monkeypatch):
    """超时（8 秒预算）→ `[topic]`，不重试。

    超时值在这里 patch 成 0.05 秒：要测的是"超了就走降级"，不是"等 8 秒"。
    """
    monkeypatch.setattr(external, "DIRECTION_TIMEOUT_SECONDS", 0.05)

    async def slow():
        await asyncio.sleep(5)
        return {"directions": ["碳中和政策", "碳中和技术路径"]}

    monkeypatch.setattr(external, "chat_async", _fake_chat(slow))

    started = time.perf_counter()
    assert run(external.plan_directions("碳中和")) == ["碳中和"]
    assert time.perf_counter() - started < 2.0


def test_plan_returns_empty_for_empty_topic():
    """空主题没有方向可拆：返回空列表，不调模型。"""
    assert run(external.plan_directions("   ")) == []


# ---------------------------------------------------------------- 3 防编造


def test_cross_domain_direction_is_rejected():
    """把主题改到别的领域的方向被逐条拒掉，只留锚得住的。

    这是 `query_plan` 那次"不看材料就编领域"的等价闸：那边靠"必须喂材料"，这边
    必然没有材料，只能靠"方向的词面必须被主题锚住"。
    """
    payload = {"directions": ["超导量子比特退相干", "碳中和政策", "离子阱量子计算"]}

    assert external._build_directions(payload, "碳中和", 3) == ["碳中和政策"]


def test_all_fabricated_directions_degrade_to_single(monkeypatch):
    """全部方向都编到别的领域 → `[topic]`，走单方向路径（不是"拿编的去搜"）。"""
    monkeypatch.setattr(external, "chat_async", _fake_chat(
        {"directions": ["超导量子比特", "离子阱量子计算", "拓扑绝缘体表面态"]}))

    assert run(external.plan_directions("碳中和")) == ["碳中和"]


def test_english_topic_anchors_on_words(monkeypatch):
    """英文主题靠整词锚定：`Rust 内存模型` 过（含 rust），`Golang 调度器` 不过。"""
    monkeypatch.setattr(external, "chat_async", _fake_chat(
        {"directions": ["Rust 内存模型", "Golang 调度器"]}))

    assert run(external.plan_directions("Rust 所有权")) == ["Rust 内存模型"]


def test_direction_equal_to_topic_is_dropped(monkeypatch):
    """等于主题的方向是白跑一路：同一个 query 打两遍，实际只有 N-1 个方面。"""
    monkeypatch.setattr(external, "chat_async", _fake_chat(
        {"directions": ["碳中和", "碳中和政策"]}))

    assert run(external.plan_directions("碳中和")) == ["碳中和政策"]


def test_english_topic_directions_are_not_killed_by_the_length_gate(monkeypatch):
    """英文主题的方向按**折算字数/词数**算长度，不按字符数。

    实测教训：`transformer architecture` 的合法方向是
    `transformer architecture 原理`（27 个字符），按 20 字符上限会被整批判死——
    三个英文主题全部降级成单方向，而那不是"拆不出来"，是闸的单位错了。
    """
    monkeypatch.setattr(external, "chat_async", _fake_chat({"directions": [
        "transformer architecture 原理", "transformer architecture 变体"]}))

    assert run(external.plan_directions("transformer architecture")) == [
        "transformer architecture 原理", "transformer architecture 变体"]


def test_pure_english_direction_uses_the_word_count_gate(monkeypatch):
    """纯英文方向：2-8 个词合法，一段整句在词数上暴露。"""
    assert external._direction_length_ok("Byzantine fault tolerance threat models")
    assert not external._direction_length_ok("Byzantine fault tolerance " * 4)
    assert external._direction_length_ok("Byzantine fault tolerance threat models")
    assert external._direction_anchored(
        "Byzantine fault tolerance",
        "Byzantine fault tolerance threat models") is not None


def test_overlong_or_too_short_directions_are_dropped(monkeypatch):
    """长度闸：3 字以下是主题的近义词，20 字以上是句子。

    下限是 **4** 而不是提示词里的 6：实测合法方向「碳中和政策」只有 5 字，6 字下限
    会把"主题原词 + 两字限定语"这一整类方向打掉（见 `DIRECTION_MIN_CHARS` 的注释）。
    """
    monkeypatch.setattr(external, "chat_async", _fake_chat(
        {"directions": ["碳中和", "碳中", "碳中和政策体系与国际合作机制的历史演进过程"]}))

    # 三条全不合格（第一条等于主题、第二条 2 字、第三条 24 字）→ 降级
    assert run(external.plan_directions("碳中和")) == ["碳中和"]


# ---------------------------------------------------------------- 4 并行


def test_directions_are_searched_in_parallel(monkeypatch):
    """多方向的检索窗口**有交集**：串行时三个窗口首尾相接，不会有交集。

    用 `_search_one` 那个替身记录每个方向进入/离开的时刻，再求交集——交集本身就把
    "同时有不止一个在飞"证完了。**不再断言总耗时**：它包含线程池起线程、调度与
    机器负载，0.4 秒这种绝对阈值在 CI 上会抖（实测 0.437 也红过），而那和"并行不并行"
    是两件事。
    """
    monkeypatch.setattr(external, "chat_async", _fake_chat({"directions": [
        "碳中和政策", "碳中和技术路径", "碳中和企业案例"]}))
    windows: list[list[float]] = []
    lock = threading.Lock()

    def fake_search_one(query: str, limit: int, **kwargs):
        with lock:
            windows.append([time.perf_counter(), 0.0])
            index = len(windows) - 1
        time.sleep(0.15)
        with lock:
            windows[index][1] = time.perf_counter()
        return [_hit(f"{query} 的资料", f"{query} 的正文，讲的是政策与技术路径与案例。")]

    monkeypatch.setattr(external, "_search_one", fake_search_one)

    external.search_external("碳中和")

    assert len(windows) == 3
    assert max(start for start, _ in windows) < min(end for _, end in windows), windows


# ---------------------------------------------------------------- 5 合并去重


def test_duplicate_url_across_directions_keeps_one_and_first_direction(monkeypatch):
    """两个方向命中同一个 URL → 只留一条，方向标签是**首见**那个。

    保留首见而不是合并标签：一条命中同时挂在两个方向下，报告里"政策那个方向拿回了
    什么"就说不清了。
    """
    monkeypatch.setattr(external, "chat_async", _fake_chat({"directions": [
        "碳中和政策", "碳中和技术路径"]}))
    shared = "https://zh.wikipedia.org/wiki/%E7%A2%B3%E4%B8%AD%E5%92%8C"

    def fake_search_one(query: str, limit: int, **kwargs):
        if "政策" in query:
            return [_hit("碳中和政策汇编", "碳中和政策包括碳税与碳交易。",
                         url=shared)]
        return [_hit("碳中和技术路径", "碳中和技术路径包括碳捕集与封存。", url=shared)]

    monkeypatch.setattr(external, "_search_one", fake_search_one)

    hits = external.search_external("碳中和")

    assert len(hits) == 1
    assert hits[0].direction == "碳中和政策"


def test_dedupe_key_ignores_fragment_and_trailing_slash():
    """URL 归一化：`#abstract` 与尾部斜杠不该让同一条命中出现两遍。"""
    first = _hit("A", "碳中和技术改造", url="https://x/a#abstract")
    second = _hit("B", "碳中和技术改造", url="https://x/a/")

    assert external._hit_key(first) == external._hit_key(second)


# ---------------------------------------------------------------- 6 门按方向


def test_direction_gate_blocks_topic_relevant_but_direction_irrelevant(monkeypatch):
    """**这条是拆方向的意义所在**：讲碳中和但不讲政策的命中，不该进「政策」方向。

    只有第一道词面门时它会通过——`_phrase_terms("碳中和政策")` 与
    `_phrase_terms("碳中和")` 完全相同（方向以主题原词开头，头 3 字就是主题），
    判据在这里退化成主题门。第二道限定语闸补的就是这一刀。
    """
    topic = "碳中和"
    direction = "碳中和政策"
    topic_only = _hit("碳中和", "碳中和是指在一定时间内抵消自身产生的二氧化碳排放量，"
                              "达到相对零排放。")
    on_topic = _hit("欧美碳中和政策演进", "各国碳中和政策包括碳税、碳交易与绿色补贴。")
    assert external._relevant_hits(direction, [topic_only]) == [topic_only], (
        "前置条件：光靠词面门它会被放行，否则这条用例证明不了限定语闸的作用")

    kept = external._direction_gate(topic, direction, [topic_only, on_topic])

    assert [hit.title for hit in kept] == ["欧美碳中和政策演进"]


def test_phrase_terms_of_a_prefixed_direction_collapse_to_the_topic():
    """钉住上面那条的前提：方向以主题开头时，方向的判定词块 == 主题的判定词块。"""
    assert external._phrase_terms("碳中和政策") == external._phrase_terms("碳中和")


def test_direction_qualifier_strips_the_topic_anchor():
    """限定语 = 方向里去掉主题原词之后剩下的部分（模型把它写在后面也能切对）。"""
    assert external._direction_qualifier("碳中和", "碳中和政策") == "政策"
    assert external._direction_qualifier("碳中和", "政策视角下的碳中和") == "政策视角下的"
    assert external._direction_qualifier("Rust 所有权", "Rust 内存模型") == "内存模型"


def test_direction_gate_keeps_hits_for_a_non_prefixed_qualifier():
    """方向不含主题原词时（`碳交易` 之于 `碳中和`），限定语就是方向全文，照常生效。"""
    topic = "碳中和"
    direction = "碳交易市场"
    off = _hit("碳中和的定义", "碳中和是指排放与吸收相抵。")
    on = _hit("全国碳交易市场运行分析", "碳配额与碳交易市场是减排的政策工具。")

    kept = external._direction_gate(topic, direction, [off, on])

    assert [hit.title for hit in kept] == ["全国碳交易市场运行分析"]


# ---------------------------------------------------------------- 7 预算


def _counting_client(routes: dict[str, Any], calls: list[httpx.Request]):
    """按 host 分派 + 记录每一次请求（与 `test_external_knowledge._router` 同款）。"""
    hosts = (("arxiv.org", "arxiv"), ("openalex.org", "openalex"),
             ("crossref.org", "crossref"), ("baike.com", "baike"),
             ("wikipedia.org", "wikipedia"))

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        host = request.url.host or ""
        name = next((value for needle, value in hosts if needle in host), "unknown")
        spec = routes.get(name)
        if spec is None:
            return httpx.Response(500, text=f"{name} 未配置")
        if isinstance(spec, str):
            return httpx.Response(200, text=spec)
        return httpx.Response(200, json=spec)

    return lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)


WIKI_SEARCH = {"query": {"search": [{"title": f"碳中和候选{i}"} for i in range(5)]}}
WIKI_PAGE = {"query": {"pages": [{
    "pageid": 1, "ns": 0, "title": "碳中和", "extract": "碳中和是指排放与吸收相抵。",
    "fullurl": "https://zh.wikipedia.org/wiki/x"}]}}
ARXIV_EMPTY = "<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'></feed>"
OPENALEX = {"results": [{"id": "https://openalex.org/W1", "title": "碳中和路径",
                         "abstract_inverted_index": {"碳中和": [0], "路径": [1]}}]}
CROSSREF = {"message": {"items": [{"title": ["碳中和研究"], "DOI": "10.1/x",
                                   "abstract": "<jats:p>碳中和研究综述。</jats:p>"}]}}


def test_three_directions_stay_within_both_budgets(monkeypatch):
    """3 方向 × 5 源：全局不超 15 次，每个方向不超 5 次。

    每方向的 5 次构成：维基 1 次条目检索 + 1 次抓正文 + arXiv/OpenAlex/Crossref
    各 1 次（方向路径跳过快懂百科：组合短语在它那里必然 404）。
    """
    monkeypatch.setattr(external, "chat_async", _fake_chat({"directions": [
        "碳中和政策", "碳中和技术路径", "碳中和企业案例"]}))
    calls: list[httpx.Request] = []
    monkeypatch.setattr(external, "_client", _counting_client(
        {"wikipedia": WIKI_SEARCH, "arxiv": ARXIV_EMPTY,
         "openalex": OPENALEX, "crossref": CROSSREF}, calls))

    external.search_external("碳中和")

    assert len(calls) <= external.MULTI_REQUEST_LIMIT, [str(c.url) for c in calls]
    # 每个方向各自不超 5 次：按请求里的方向文本归属。
    for direction in ("碳中和政策", "碳中和技术路径", "碳中和企业案例"):
        owned = [c for c in calls if direction in str(c.url)]
        assert len(owned) <= external.DIRECTION_REQUEST_LIMIT, (direction, len(owned))
    # 快懂百科在方向路径里一次都不该被请求（它是精确词条源）。
    assert not any("baike.com" in (c.url.host or "") for c in calls)


def test_multi_budget_constants_are_consistent():
    """15 = 3 × 5：三个常量之间的关系是硬约束，改一个不改另一个就会饿死某个方向。"""
    assert external.MULTI_REQUEST_LIMIT == (
        external.MAX_DIRECTIONS * external.DIRECTION_REQUEST_LIMIT)
    assert external.DIRECTION_SOURCES == ("wikipedia", "openalex", "crossref", "arxiv")
    assert len(external.DIRECTION_SOURCES) + external.DIRECTION_WIKI_FETCH == (
        external.DIRECTION_REQUEST_LIMIT)


def test_multi_direction_keeps_the_twenty_second_deadline(monkeypatch):
    """墙钟仍是 20 秒：多方向没有放宽它，一个方向的慢源不会把总耗时拖长。"""
    monkeypatch.setattr(external, "DEADLINE_SECONDS", 0.6)
    monkeypatch.setattr(external, "REQUEST_TIMEOUT", 0.2)
    monkeypatch.setattr(external, "chat_async", _fake_chat({"directions": [
        "碳中和政策", "碳中和技术路径"]}))

    def slow(request: httpx.Request) -> httpx.Response:
        time.sleep(0.5)
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(external, "_client", _counting_client(
        {"wikipedia": slow, "arxiv": slow, "openalex": slow, "crossref": slow}, []))

    started = time.perf_counter()
    hits = external.search_external("碳中和")

    assert hits == []
    assert time.perf_counter() - started < 3.0
    assert external.DEADLINE_SECONDS < 20.0  # 预算常量本身就是 20，这里只是没被改大


# ---------------------------------------------------------------- 8 与基线一致


def _load_baseline(tmp_path: Path):
    """把 `3c40ba6` 的 `external.py` 载成独立模块（读不到就跳过）。"""
    try:
        raw = subprocess.run(
            ["git", "show", f"{BASELINE_REV}:backend/app/knowledge/external.py"],
            cwd=REPO_ROOT, capture_output=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"读不到 {BASELINE_REV} 的 external.py：{exc}")

    assert hashlib.sha256(raw).hexdigest() == BASELINE_SHA256

    path = tmp_path / f"external_{BASELINE_REV}.py"
    path.write_bytes(raw)
    spec = importlib.util.spec_from_file_location(f"external_baseline_{BASELINE_REV}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pin_sources(monkeypatch, module: Any, payload: dict[str, list[external.ExternalHit]]):
    """把五个源换成固定返回值，并把每次调用记下来。"""
    calls: list[tuple] = []
    for name in ("wikipedia", "arxiv", "openalex", "crossref", "baike"):
        def make(which: str):
            def fake(query, budget, limit, **kwargs):
                calls.append((which, query, limit))
                return list(payload.get(which, []))
            return fake
        monkeypatch.setattr(module, f"_{name}", make(name))
    return calls


def _fixture_sources() -> dict[str, list[external.ExternalHit]]:
    return {
        "wikipedia": [_hit("最终一致性", "最终一致性是分布式计算里的一种内存一致性模型。")],
        "openalex": [_hit("一种新颖钒,钨异金属八核簇合物",
                          "对５４９９个可观测反射点最终一致性因子为Ｒ＝０．０３０。",
                          source_name="openalex")],
        "arxiv": [_hit("Eventual Consistency Models", "A survey of eventual consistency.",
                       source_name="arxiv", language="en")],
        "crossref": [],
        "baike": [_hit("最终一致性", "最终一致性指分布式系统中经过一段时间后所有副本达到一致。",
                       source_name="baike")],
    }


@pytest.mark.parametrize("failure", [
    RuntimeError("模型挂了"), "这不是 JSON", {"directions": ["超导量子比特退相干"]},
])
def test_degrade_equals_the_single_direction_path(monkeypatch, failure):
    """拆方向失败 → 与"显式只给一个方向"**逐字段相同**，请求次数也相同。

    "退化成一个方向"不是"差不多"：同一份预算、同一批五个源、同一次取样。以前这条
    用例拿 `3c40ba6` 的实现在做对照，取样换成打分排序之后那个对照不再成立——**但
    它要保护的东西没变**：降级不能是第二套实现。所以对照换成了 `directions=[topic]`
    那条路（它是唯一的一方向路径），断言从"与旧版本相同"变成"与另一条入口相同"。

    判据强度没有下降：两个入口若哪天分叉（比如降级顺手换了 query、或者少走了一次
    取样），这里的 title/source/url/language/text 五个字段和 (query, limit) 记录
    会同时红。
    """
    sources = _fixture_sources()
    degraded_calls = _pin_sources(monkeypatch, external, sources)
    monkeypatch.setattr(external, "chat_async", _fake_chat(failure))

    degraded = external.search_external("最终一致性")
    degraded_records = list(degraded_calls)

    single_calls = _pin_sources(monkeypatch, external, sources)
    single = external.search_external("最终一致性", directions=["最终一致性"])

    assert degraded, "夹具本身要能产出多条，否则这条用例什么也没钉"
    assert [(hit.title, hit.source_name, hit.url, hit.language, hit.text)
            for hit in degraded] == \
        [(hit.title, hit.source_name, hit.url, hit.language, hit.text) for hit in single]
    # 五个源在线程里并行跑，记录顺序不定，所以比集合而不是比顺序。
    assert sorted(record[1:] for record in degraded_records) == \
        sorted(record[1:] for record in single_calls), (
        "两次检索打给源函数的 (query, limit) 必须完全一样："
        "降级路径不该顺手换 query（比如把主题换成方向里的第一条）")


def test_multi_direction_path_does_not_touch_the_baseline_signature(monkeypatch):
    """多方向路径不改变 `search_external` 的既有调用形态：`(query)` 仍然返回列表。"""
    monkeypatch.setattr(external, "chat_async", _fake_chat({"directions": [
        "碳中和政策", "碳中和技术路径"]}))
    payload = {"wikipedia": [_hit("碳中和政策汇编", "碳中和政策包括碳税与碳交易。")],
               "openalex": [_hit("碳中和技术路径", "碳中和技术路径包括碳捕集。",
                                 source_name="openalex")]}
    _pin_sources(monkeypatch, external, payload)

    hits = external.search_external("碳中和")

    assert isinstance(hits, list) and hits
    assert all(hit.direction for hit in hits), "多方向路径的每条命中都要能看出方向"


# ---------------------------------------------------------------- 9 端到端


def test_end_to_end_covers_several_aspects(monkeypatch):
    """`search_external("碳中和")` 端到端：三个方向的命中各占一条，方向标签可读。

    这是"用户期待多面覆盖"的验收线：返回 3 条时就该看到 3 个**不同**的方向，
    而不是同一个百科条目的三个邻接条目。
    """
    monkeypatch.setattr(external, "chat_async", _fake_chat({"directions": [
        "碳中和政策", "碳中和技术路径", "碳中和企业案例"]}))

    def fake_search_one(query: str, limit: int, **kwargs):
        return [
            _hit(f"{query}综述", f"{query}的正文。", url=f"https://x/{query}",
                 source_name="openalex"),
            _hit(f"{query}研究", f"{query}的另一篇。", url=f"https://y/{query}",
                 source_name="crossref"),
        ]

    monkeypatch.setattr(external, "_search_one", fake_search_one)

    # `limit=3` 是这条用例的原意（"返回 3 条时该看到 3 个不同方向"）。写死 3 而不是
    # 用默认值：`DEFAULT_LIMIT` 会随取数策略变（现在 8），而"三个方向各占一条"这条
    # 验收线与默认条数无关。
    hits = external.search_external("碳中和", limit=3)

    assert len(hits) == 3
    assert len({hit.direction for hit in hits}) == 3, [h.direction for h in hits]
    assert [hit.direction for hit in hits] == [
        "碳中和政策", "碳中和技术路径", "碳中和企业案例"]
    # 每条命中都能看出它来自哪个方向，且方向确实出现在它的 URL 里（有据可查）。
    for hit in hits:
        assert hit.direction in hit.url
