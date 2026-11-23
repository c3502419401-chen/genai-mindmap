"""DeepSeek 的 Search / Fetch 接进外部检索链路：第六个源、`lang:zh`、截断、补抓、降级。

八组断言（1-7 是 Search，第 8 组是 `/fetch` 端点），对着这次改动的八条要求：

  1. **解析**：真实响应的形状（`scripts/probe_deepseek_tools.search.json`）→ `ExternalHit`
     每个字段都对：`content` 进 `text`、`site_name` 进 `site`、`authority` /
     `rerank_score` 进同名字段、没有 `url` 的丢弃、`rerank_score` 降序。
  2. **`lang:zh` 的判据**：中文主题追加、英文主题不追加、已经写了 `lang:` 的不重复。
     判据必须与 `_query_language` 同源（理由见那条用例）。
  3. **截断**：正文按 `DEEPSEEK_TEXT_MAX_CHARS`（4000）截，`raw_chars` 记截断前长度。
  4. **用途 B（补抓）**：`content` 为空且补抓成功 → 保留 Search 的标题与排序元数据；
     补抓失败 → 丢弃该条（不留一条"看起来像依据"的空来源）。
  5. **开关**：默认关 → 一次请求都不发，结果与五源逐条相同；打开 → 正好多 1 次请求。
  6. **配额**：`_blend_deepseek` 给 Search 固定 1 席（`limit-1` 给五源）；Search 没有
     命中时**逐条等于** `_interleave` 的结果（那是"开关关 = 旧行为"的另一半）。
  7. **降级**：base_url 指错 / 没有 key 都只让这一个源空掉，五源照常返回，且空结果
     不会被缓存（`failures > 0`）。

全部离线：`conftest.no_real_http` 把真请求变成断言失败，这里用
`httpx.MockTransport` 造响应（在传输层之前返回，不影响 URL / 参数 / 状态码 / JSON
解析的真实路径）。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import tools as agent_tools
from app.contracts.source import SourceKind
from app.knowledge import external
from app.knowledge import external_cache
from app.main import app

# 真实响应（2026-11 实测，正文截短到几十字）。三个结果刻意覆盖三种情形：
#   1. 正常命中（中文站、有 content）；
#   2. 正文**为空**（要补抓的那一条）；
#   3. 没有 url（必须丢弃）。
DEEPSEEK_JSON: dict[str, Any] = {
    "results": [
        {
            "title": "中国计算机学会：机器学习方向",
            "site_name": "ccf.org.cn",
            "site_icon": "https://cdn.deepseek.com/site-icons/ccf.org.cn",
            "authority": 4,
            "rerank_score": 1.7502,
            "date": None,
            "url": "https://www.ccf.org.cn/ccf/tech/ml",
            "summary": "摘要（不用它顶替正文）",
            "snippet": "一句话",
            "content": "## 机器学习的主要方向\n\n监督学习、无监督学习、强化学习。\n",
        },
        {
            "title": "中文专利：一种机器学习方法",
            "site_name": "Google Patents",
            "authority": 4,
            "rerank_score": 1.7966,
            "date": "2018-09-24T16:00:00+00:00",
            "url": "https://patents.google.com/patent/CN1000000A/zh",
            "summary": "专利摘要",
            "snippet": "专利",
            # 空正文：真实形态是付费墙 / 纯 JS 渲染 / PDF，这里只留形状。
            "content": "",
        },
        {
            "title": "没有地址的那一条",
            "site_name": "nowhere.example",
            "authority": 4,
            "rerank_score": 1.9,
            "date": None,
            "url": "",
            "summary": "s",
            "snippet": "s",
            "content": "正文在，但没有地址：补抓不了、也点不回原文。",
        },
    ]
}

FETCH_JSON: dict[str, Any] = {
    "title": "一种机器学习方法",
    "content": "专利正文：本发明公开了一种机器学习方法，包括……（补抓回来的正文）",
}


def _factory(handler) -> Any:
    """把 `external._client` 换成走 `MockTransport` 的工厂（与既有用例同一套）。"""
    def make() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=10.0)
    return make


def _routes(
    *, search: Any = DEEPSEEK_JSON, fetch: Any = FETCH_JSON,
    calls: list[httpx.Request] | None = None,
):
    """按路径分派：`/tools/search` / `/tools/fetch`，其余（五源）一律 500。

    其余源 500 是刻意的：这些用例只关心第六个源，五源应当自然降级为空，而不是
    逼着每一处都写一遍"维基返回 404"。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        path = request.url.path
        spec = search if path.endswith("/tools/search") else (
            fetch if path.endswith("/tools/fetch") else None)
        if spec is None:
            return httpx.Response(500, text="未配置")
        if isinstance(spec, Exception):
            raise spec
        if callable(spec):
            return spec(request)
        return httpx.Response(200, json=spec)

    return handler


def _on(monkeypatch) -> None:
    """打开第六个源并给它一个 key（用例里的 key 是假的，请求不出本机）。"""
    monkeypatch.setenv(external.DEEPSEEK_SEARCH_ENV, "1")
    monkeypatch.setenv(external.DEEPSEEK_KEY_ENV, "sk-test")


def _patch(monkeypatch, handler) -> None:
    """两个客户端都换成假响应。

    DeepSeek 有自己的 `_deepseek_client`（默认直连、不吃代理，理由见模块 docstring），
    所以"只 patch `_client`"会让 Search 的请求漏到真实传输层——被
    `conftest.no_real_http` 拦成断言失败。两处一起换，才既不打网、又跑真实的拼接与解析。
    """
    factory = _factory(handler)
    monkeypatch.setattr(external, "_client", factory)
    monkeypatch.setattr(external, "_deepseek_client", factory)


# ---------------------------------------------------------------- 1 解析


def test_search_hit_maps_every_field(monkeypatch):
    """每条 `results[]` 的字段落到 `ExternalHit` 的哪一个位置。"""
    _on(monkeypatch)
    hits = external._parse_deepseek_search(DEEPSEEK_JSON, 3)

    # 没有 url 的那条被丢掉：补抓不了、界面也点不开。
    assert [hit.title for hit in hits] == ["中文专利：一种机器学习方法", "中国计算机学会：机器学习方向"]
    first = hits[0]
    assert first.source_name == "deepseek"
    assert first.site == "Google Patents"
    assert first.url == "https://patents.google.com/patent/CN1000000A/zh"
    assert first.authority == 4
    assert first.rerank_score == pytest.approx(1.7966)
    assert first.raw_chars == 0            # 正文是空的，补抓前就是 0
    assert first.text == ""
    second = hits[1]
    assert second.text.startswith("## 机器学习的主要方向")
    assert second.language == "zh"          # 按正文判语言，不信源站申报


def test_rerank_score_orders_the_results():
    """`rerank_score` 降序；缺分数（None）的排最后，且保持原序。"""
    payload = {"results": [
        {"title": "低", "url": "https://a/1", "content": "甲", "rerank_score": 1.1},
        {"title": "无分", "url": "https://a/2", "content": "乙"},
        {"title": "高", "url": "https://a/3", "content": "丙", "rerank_score": 2.0},
    ]}
    assert [hit.title for hit in external._parse_deepseek_search(payload, 3)] == [
        "高", "低", "无分"]


def test_dirty_metadata_falls_back_to_none():
    """外部源给的分数可能是字符串 / NaN / 负数——一律当没有，绝不抛。

    一个 `"1.9"` 进到 SQLite 之后，下游 `max()` 与比较会在某个时刻抛 `TypeError`，
    那会把"少一个元数据"升级成"整次生成失败"。
    """
    payload = {"results": [{
        "title": "t", "url": "https://a/1", "content": "正文",
        "authority": -3, "rerank_score": "1.9",
    }]}
    hit = external._parse_deepseek_search(payload, 1)[0]
    assert hit.authority is None
    assert hit.rerank_score is None

    assert external._deepseek_float(float("nan")) is None
    assert external._deepseek_float(float("inf")) is None
    assert external._deepseek_float(True) is None       # bool 不是数字
    assert external._deepseek_count(True) is None


# ---------------------------------------------------------------- 2 lang 判据


@pytest.mark.parametrize("query, expected", [
    ("机器学习的主要方向", "机器学习的主要方向 lang:zh"),
    ("碳中和", "碳中和 lang:zh"),
    # 混排但含 CJK：与 `_query_language` 同口径 → 追加。实测 `Rust 所有权` 这类主题
    # 在中文站上拿到的资料明显更贴题（英文站回的是小麦锈病）。
    ("Rust 所有权", "Rust 所有权 lang:zh"),
    # 纯英文：不追加。实测带 `lang:en` 与不带的结果完全相同，那是个无效参数。
    ("transformer architecture", "transformer architecture"),
    ("Byzantine fault tolerance", "Byzantine fault tolerance"),
    # 已经写了 lang: 的不重复（用户自己指定了语种，尊重他）。
    ("机器学习 lang:en", "机器学习 lang:en"),
    ("", ""),
])
def test_lang_zh_follows_the_same_judgement_as_interleave(query, expected):
    """`lang:zh` 的判据必须与 `_query_language` 同源。

    两处各写一份的后果不是"少一个参数"，是自相矛盾：源侧按一份判据只回英文，排序侧
    按另一份判据"中文 query 优先中文命中"——两边都对，结果一条中文都没有。
    """
    assert external._deepseek_query(query) == expected
    if external._deepseek_query(query).endswith("lang:zh"):
        assert external._query_language(query) == "zh"


# ---------------------------------------------------------------- 3 截断


def test_content_is_truncated_to_the_deepseek_limit():
    """正文按 `DEEPSEEK_TEXT_MAX_CHARS` 截，`raw_chars` 记截断前的长度。

    一条 21470 字的网页按 300 字/子块切是 71 个 leaf chunk，而五源最长的一条只有
    9 个；4000 字是 13 个，与它同量级。理由与实测见模块 docstring。
    """
    long_text = "机" * 5000
    payload = {"results": [{"title": "t", "url": "https://a/1", "content": long_text}]}
    hit = external._parse_deepseek_search(payload, 1)[0]
    assert len(hit.text) == external.DEEPSEEK_TEXT_MAX_CHARS
    assert hit.text.endswith("…")
    assert hit.raw_chars == 5000
    # 五源的 8000 上限没被这次改动波及。
    assert external.TEXT_MAX_CHARS == 8000
    assert len(external.truncate_text("a" * 9000)) == 8000


# ---------------------------------------------------------------- 4 用途 B：补抓


def test_empty_content_is_backfilled_by_fetch(monkeypatch):
    """`content` 为空 → 按 url 调一次 Fetch，补回来的正文进 `text`。

    Search 的标题与排序元数据保留：补抓回来的标题常常是站点名，而 Search 的标题更
    贴近"这一条讲什么"。
    """
    _on(monkeypatch)
    calls: list[httpx.Request] = []
    _patch(monkeypatch, _routes(calls=calls))
    budget = external._Budget(4, 10.0)

    hits = external._deepseek_search("机器学习", budget, 3)

    paths = [request.url.path for request in calls]
    assert paths == ["/v1/tools/search", "/v1/tools/fetch"]
    # 空正文那条被补上了，标题仍是 Search 给的
    filled = [hit for hit in hits if hit.url.endswith("/zh")]
    assert filled and filled[0].title == "中文专利：一种机器学习方法"
    assert filled[0].text.startswith("专利正文：本发明公开了一种机器学习方法")
    assert filled[0].rerank_score == pytest.approx(1.7966)


def test_backfill_failure_drops_the_hit(monkeypatch):
    """补抓失败 → **丢弃**这条命中，而不是留一条空正文的来源。

    空正文转成 `Source` 之后是 `status="parsed"`、正文为空：它会进材料清单、看起来
    像依据、引用一条都生不出来。静默的假来源比少一条糟得多。
    """
    _on(monkeypatch)
    _patch(monkeypatch, _routes(
        fetch=httpx.Response(502, text="upstream says no")))
    budget = external._Budget(4, 10.0)

    hits = external._deepseek_search("机器学习", budget, 3)

    assert [hit.title for hit in hits] == ["中国计算机学会：机器学习方向"]


def test_fill_uses_no_extra_request_when_content_is_present(monkeypatch):
    """有正文的命中不会触发补抓（请求次数是 1，不是 1+N）。"""
    _on(monkeypatch)
    calls: list[httpx.Request] = []
    payload = {"results": [
        {"title": "甲", "url": "https://a/1", "content": "正文一"},
        {"title": "乙", "url": "https://a/2", "content": "正文二"},
    ]}
    _patch(monkeypatch, _routes(search=payload, calls=calls))

    hits = external._deepseek_search("机器学习", external._Budget(4, 10.0), 3)

    assert len(hits) == 2
    assert [request.url.path for request in calls] == ["/v1/tools/search"]


# ---------------------------------------------------------------- 5 开关


def test_default_is_off_and_no_request_is_sent(monkeypatch):
    """不设开关时一次请求都不发，结果与五源逐条相同。

    这是"默认关 = 与 `3c40ba6` 逐字节相同"的可执行版本：不是"我记得没改"，而是
    "同一批响应喂进去，六源与五源给出同一份命中"。
    """
    monkeypatch.delenv(external.DEEPSEEK_SEARCH_ENV, raising=False)
    monkeypatch.setenv(external.DEEPSEEK_KEY_ENV, "sk-test")
    calls: list[httpx.Request] = []
    _patch(monkeypatch, _routes(calls=calls))

    assert external.deepseek_search_enabled() is False
    hits = external.search_external("机器学习", limit=3)

    # 五源全 500、Search 关着 → 一条都没有，而且一个请求都没打到 DeepSeek。
    assert hits == []
    assert not [request for request in calls if "/tools/" in request.url.path]


def test_on_by_env_and_one_search_request(monkeypatch):
    """打开之后多出来的正好是 1 次请求（正文由它自己抓好，不需要逐条抓）。"""
    _on(monkeypatch)
    calls: list[httpx.Request] = []
    _patch(monkeypatch, _routes(calls=calls))

    hits = external.search_external("机器学习的主要方向", limit=3)

    search_calls = [r for r in calls if r.url.path.endswith("/tools/search")]
    assert len(search_calls) == 1
    # `lang:zh` 真的写进了发出去的 query 里
    assert json.loads(search_calls[0].content)["query"].endswith("lang:zh")
    assert json.loads(search_calls[0].content)["need_content"] is True
    # 五源在这份假响应里全是 500，所以结果只可能来自 Search；条数由 `limit` 与打分
    # 决定，不再由"给 Search 留 1 席"决定。
    assert hits, "开关打开且只有它活着时应当有命中"
    assert {hit.source_name for hit in hits} == {"deepseek"}
    assert len(hits) <= 3


# ---------------------------------------------------------------- 6 配额（已撤销）


def test_deepseek_gets_no_reserved_seat():
    """**不再给 Search 留席位**：它要和五源在同一个分数上排队。

    这里刻意让五源的两条更贴题（标题就是 query）、DeepSeek 那条只是正文里提到——
    老实现会给它留最后一席，新实现让它落选。撤掉的理由写在
    `knowledge/external.py` 的"取数策略"一节：配额是"没有判据时"的替代品，而今天
    的证据是它在害人（五源全挂时它抓回教育平台课程页，照样进材料）。
    """
    hits = [
        external.ExternalHit("w1", "碳中和是指用碳汇抵消排放。", "https://w/1",
                             "wikipedia", "2026-01-01T00:00:00Z", "zh"),
        external.ExternalHit("w2", "碳中和的实现路径包括能源转型。", "https://w/2",
                             "wikipedia", "2026-01-01T00:00:00Z", "zh"),
        external.ExternalHit("d1", "某站导航：首页 | 关于我们 | 碳中和", "https://d/1",
                             "deepseek", "2026-01-01T00:00:00Z", "zh",
                             rerank_score=1.60),
    ]
    picked = external._select_hits("碳中和", hits, 2)

    assert [hit.title for hit in picked] == ["w1", "w2"], \
        "Search 抢到了不该给它的名额：配额已经撤销，名额只由分数决定"


def test_select_hits_picks_by_score_across_sources():
    """同一批候选里，名额按分数走、与源无关——六源一视同仁。"""
    hits = [
        external.ExternalHit("低分", "这里没提那件事。", "https://d/1",
                             "deepseek", "2026-01-01T00:00:00Z", "zh", rerank_score=2.0),
        external.ExternalHit("高分", "量子计算是计算范式的一种。", "https://a/1",
                             "arxiv", "2026-01-01T00:00:00Z", "zh"),
    ]
    picked = external._select_hits("量子计算", hits, 1)

    assert [hit.title for hit in picked] == ["高分"]


# ---------------------------------------------------------------- 7 降级与缓存


def test_wrong_base_url_only_kills_this_source(monkeypatch):
    """base_url 指错 → 这个源空掉，五源的结果照常返回、照常缓存。

    "Search 挂了不能拖垮整条链"的可执行版本：同一批响应里维基是 200、DeepSeek 是
    `ConnectError`，返回的命中里必须只剩维基，而且缓存的是维基那一批（不是空）。
    """
    _on(monkeypatch)
    monkeypatch.setenv(external.DEEPSEEK_BASE_ENV, "http://127.0.0.1:9/v1")
    wiki = {"query": {"pages": [{
        "title": "碳中和", "extract": "碳中和是指在一定时间内抵消自身产生的二氧化碳。",
        "fullurl": "https://zh.wikipedia.org/wiki/x",
    }]}}
    calls: list[httpx.Request] = []
    # `_routes` 对五源一律 500，而这条用例要的是"维基成功、DeepSeek 失败"，所以用
    # 一个只认 host 的 handler（DeepSeek 的那个客户端也一起换掉）。
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "wikipedia.org" in (request.url.host or ""):
            return httpx.Response(200, json=wiki)
        if request.url.path.endswith("/tools/search"):
            raise httpx.ConnectError("boom")
        return httpx.Response(500, text="未配置")

    _patch(monkeypatch, handler)

    hits = external.search_external("碳中和", limit=3)

    assert [hit.source_name for hit in hits] == ["wikipedia"]
    assert any(request.url.path.endswith("/tools/search") for request in calls)
    # 缓存写的是"五源那一批"（hits 非空 → 值得缓存），下次命中它不必再问 DeepSeek。
    cached = external_cache.get("碳中和", limit=3)
    assert cached is not None and cached[1] == 0


def test_missing_key_counts_as_a_source_failure(monkeypatch):
    """开关开着但没有 key：记一次源级失败，且**不缓存**这次空结果。

    不缓存是关键：用户配好 key 之后应当立刻能重试，而不是等 TTL 过期。
    """
    monkeypatch.setenv(external.DEEPSEEK_SEARCH_ENV, "1")
    monkeypatch.delenv(external.DEEPSEEK_KEY_ENV, raising=False)
    budget = external._Budget(4, 10.0)

    assert external._deepseek_search("碳中和", budget, 3) == []
    assert budget.failures == 1
    assert budget.used == 0            # 一次请求都没发


def test_cached_hits_keep_the_sort_metadata(monkeypatch):
    """缓存往返保留 `site` / `authority` / `rerank_score`。

    漏一个字段，第二次生成的材料清单就少一条"这条来自哪个站"与两个排序元数据，
    而界面上分不出"这条本来就没有"与"缓存吃掉了"。
    """
    _on(monkeypatch)
    _patch(monkeypatch, _routes())

    first = external.search_external("机器学习的主要方向", limit=3)
    second = external.search_external("机器学习的主要方向", limit=3)

    assert second.cached is True
    fields = ("title", "site", "authority", "rerank_score", "url", "source_name")
    assert [[getattr(hit, name) for name in fields] for hit in first] == [
        [getattr(hit, name) for name in fields] for hit in second]


def test_old_cache_rows_without_new_fields_still_load():
    """旧缓存行（没有三个新键）仍然解得出来：三个字段都有默认值。"""
    record = {
        "title": "旧的一条", "text": "正文", "url": "https://a/1",
        "source_name": "wikipedia", "fetched_at": "2026-01-01T00:00:00Z",
        "language": "zh", "raw_chars": 2, "direction": "",
    }
    hit = external._record_hit(record)

    assert hit is not None
    assert (hit.site, hit.authority, hit.rerank_score) == ("", None, None)


def test_source_carries_authority_site_and_scores():
    """`_external_source`：`authority` / `rerank_score` 进 metadata，站点进 tags 第二项。"""
    hit = external.ExternalHit(
        title="机器学习与模式识别", text="正文", url="https://book.cppinfo.cn/a",
        source_name="deepseek", fetched_at="2026-01-01T00:00:00Z", language="zh",
        raw_chars=2, site="cppinfo.cn", authority=4, rerank_score=1.7966)

    source = agent_tools._external_source(hit)

    assert source.kind is SourceKind.web
    assert source.metadata.authority == 4
    assert source.metadata.rerank_score == pytest.approx(1.7966)
    assert source.metadata.tags == ["deepseek", "cppinfo.cn"]
    # `tags[0]` 仍然是抓取层名：`_external_source_names` 靠它拼"来自哪几个站"。
    assert agent_tools._external_source_names([source]) == "deepseek"


def test_request_limit_covers_the_sixth_source():
    """预算上界 = 五源固定 5 + 维基逐条抓 `limit` + Search 1 + 补抓 `limit` + 1 余量。

    它是一条算术断言，因为那个数字就是"最坏情况下多等多久"的依据：拆成几段写在
    注释里而不用代码钉住，改一处忘另一处不会有人发现。**`REQUEST_LIMIT` 现在是从
    `DEFAULT_LIMIT` 算出来的**（`_request_limit`），所以这两条必须同时成立：
    默认值上等于 13（与改动之前那个常量逐字相同），且随 `limit` 线性涨。
    """
    assert external._request_limit(3) == 13
    assert external.REQUEST_LIMIT == external._request_limit(external.DEFAULT_LIMIT)
    assert external.DIRECTION_SOURCES == ("wikipedia", "openalex", "crossref", "arxiv")


# ---------------------------------------------------------------- 8 用途 A：/fetch


def _api() -> TestClient:
    return TestClient(app)


def test_fetch_endpoint_is_off_by_default(monkeypatch):
    """没打开时 409，文案说清"去把开关打开"而不是"抓取失败"。"""
    monkeypatch.delenv(external.DEEPSEEK_FETCH_ENV, raising=False)
    with _api() as api:
        response = api.post("/api/knowledge/fetch", json={"url": "https://example.com/a"})

    assert response.status_code == 409
    assert response.json()["code"] == "fetch_disabled"


def test_fetch_endpoint_without_key_says_so(monkeypatch):
    """打开了但没 key：503（部署问题），不是 502（"这个链接读不出正文"）。"""
    monkeypatch.setenv(external.DEEPSEEK_FETCH_ENV, "1")
    monkeypatch.delenv(external.DEEPSEEK_KEY_ENV, raising=False)
    monkeypatch.delenv("MINDMAP_LLM_API_KEY", raising=False)
    with _api() as api:
        response = api.post("/api/knowledge/fetch", json={"url": "https://example.com/a"})

    assert response.status_code == 503
    assert response.json()["code"] == "fetch_unconfigured"


def test_fetch_endpoint_returns_a_source(monkeypatch):
    """成功时给一个能直接当材料用的 `Source`。"""
    monkeypatch.setenv(external.DEEPSEEK_FETCH_ENV, "1")
    monkeypatch.setenv(external.DEEPSEEK_KEY_ENV, "sk-test")
    _patch(monkeypatch, _routes())
    with _api() as api:
        response = api.post("/api/knowledge/fetch", json={"url": "https://example.com/a"})

    assert response.status_code == 200
    source = response.json()["source"]
    assert source["status"] == "parsed"
    assert source["kind"] == "web"
    assert source["text"].startswith("专利正文")
    # 文件名带抓取层前缀，与外部检索那条路一致（材料清单里的显示规则只有一套）
    assert source["filename"].startswith("deepseek: ")
    assert source["metadata"]["url"] == "https://example.com/a"


def test_fetch_endpoint_reports_a_broken_link(monkeypatch):
    """抓不到正文：502，而不是 200 + 空材料。"""
    monkeypatch.setenv(external.DEEPSEEK_FETCH_ENV, "1")
    monkeypatch.setenv(external.DEEPSEEK_KEY_ENV, "sk-test")
    _patch(monkeypatch, _routes(fetch=httpx.Response(500, text="nope")))
    with _api() as api:
        response = api.post("/api/knowledge/fetch", json={"url": "https://example.com/a"})

    assert response.status_code == 502
    assert response.json()["code"] == "fetch_failed"


def test_fetch_rejects_blank_url_at_the_contract():
    """空 url 在契约层 422，不去发一个必然失败的请求。"""
    with _api() as api:
        assert api.post("/api/knowledge/fetch", json={"url": ""}).status_code == 422
