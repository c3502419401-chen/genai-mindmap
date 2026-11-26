"""`POST /api/knowledge/search` **走路由**的行为：它到底接上没有。

这一组存在的理由就是原来那组用例的位置错了：`search_sources` / `search_parent`
被十几个文件覆盖着，而对外那条路由返回 `{"hits": [], "implemented": false}`
很久都没人发现——**用例测的是函数，不是链路**。所以这里全部经过 HTTP，
而且第一条断言就是"库里有明显匹配的材料时，路由必须给出命中"：
接上之前它恒为空，接上之后它必红。

全部离线：`conftest.py` 把数据库落在本次用例的 `tmp_path`、开关置 0（纯 BM25）、
凭证清空，所以不会有真请求，也不依赖宿主机上那份 4913 条的库。
"""
from __future__ import annotations

from conftest import guest_client

from app.contracts.source import Source, SourceKind
from app.knowledge import library

# 一份"一眼能看出该命中哪段"的材料：主题句在第一段，后面是凑长度用的背景，
# 好让切块器切出多个块（`CHUNK_MAX_CHARS` 是 700）。
MATERIAL = (
    "# 缓存设计评审\n\n"
    "本次评审的核心问题是缓存命中率对查询延迟的影响。\n\n"
    "命中率从 62% 提到 90% 之后，P95 延迟从 480 毫秒降到 120 毫秒。\n\n"
    "结论：先把 read-through 的 TTL 调对，再谈扩容。\n\n"
    + "背景：集群共 8 个节点，单节点 16C32G，峰值 QPS 2400，写入比 1:9。\n\n" * 20
)

OTHER = (
    "# 报销流程\n\n"
    "报销从提交到入账平均要 11 天，主要卡在财务复核与发票补正两个环节。\n\n"
    + "背景：每月约 320 单，其中 18% 会被退回补正。\n\n" * 20
)


def _ingest(text: str, filename: str) -> str:
    return library.ingest(Source(kind=SourceKind.markdown, status="parsed",
                                 text=text, filename=filename))


def test_route_returns_real_hits_from_the_library():
    """库里有匹配材料 → 路由必须返回命中，且 `source_id` 是**持久 id**。

    这条是"接线"用例：`implemented=false` 与 `hits=[]` 同时在的那个版本里，
    它第二次断言就红。
    """
    source_id = _ingest(MATERIAL, "cache-review.md")

    with guest_client() as api:
        response = api.post("/api/knowledge/search",
                            json={"query": "缓存命中率对查询延迟的影响", "top_k": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["implemented"] is True, "implemented 只回答'接上了没有'，接上之后恒为真"
    assert body["hits"], "库里有一份明显匹配的材料，路由必须给出命中"

    top = body["hits"][0]
    assert top["source_id"] == source_id
    assert top["filename"] == "cache-review.md"
    assert top["kind"] == "markdown"
    assert "缓存" in top["snippet"]


def test_offsets_slice_the_original_text_exactly():
    """`start` / `end` 是 `snippet` 在**全文**里的码点区间，逐字对得上。

    偏一位的后果不是"少一个字"，是界面高亮标错位置——而它不会报错，
    只会让用户怀疑自己看错了原文。
    """
    _ingest(MATERIAL, "cache-review.md")

    with guest_client() as api:
        body = api.post("/api/knowledge/search", json={"query": "缓存命中率"}).json()

    hit = body["hits"][0]
    assert 0 <= hit["start"] < hit["end"] <= len(MATERIAL)
    assert MATERIAL[hit["start"]:hit["end"]] == hit["snippet"]


def test_a_hit_can_be_opened_through_the_library_route():
    """命中给出的 `source_id` 必须**点得开**：`/api/knowledge/library/{id}` 能读到正文。

    这是"检索结果可用"与"检索结果只是一串 id"的分界线。
    """
    _ingest(MATERIAL, "cache-review.md")

    with guest_client() as api:
        hit = api.post("/api/knowledge/search",
                       json={"query": "read-through 的 TTL"}).json()["hits"][0]
        opened = api.get(f"/api/knowledge/library/{hit['source_id']}")

    assert opened.status_code == 200
    detail = opened.json()
    assert detail["filename"] == "cache-review.md"
    assert detail["text"][hit["start"]:hit["end"]] == hit["snippet"]


def test_unrelated_query_returns_empty_but_still_implemented():
    """"没命中"与"没接上"必须是两件事。

    这条与第一条正相反：库里有材料，但查询与它们都不沾。此时 `hits` 为空而
    `implemented` 仍为真——两者混在一个字段里时，"库是空的"会被读成"功能没做"
    （这正是接上之前那个字段的用法）。
    """
    _ingest(MATERIAL, "cache-review.md")
    _ingest(OTHER, "reimbursement.md")

    with guest_client() as api:
        body = api.post("/api/knowledge/search",
                        json={"query": "量子色动力学中的渐近自由"}).json()

    assert body["implemented"] is True
    assert body["hits"] == []


def test_repeated_search_finds_the_same_source_among_several():
    """库里有多份材料时，命中要落在**对的那一份**上，不只是"有结果"。

    少了这一条，一个"总是返回库里最近用得最多的那份"的实现也能通过前三条。
    """
    cache_id = _ingest(MATERIAL, "cache-review.md")
    _ingest(OTHER, "reimbursement.md")

    with guest_client() as api:
        body = api.post("/api/knowledge/search",
                        json={"query": "发票补正退回", "top_k": 5}).json()

    assert body["hits"], "「发票补正退回」只在报销那份里出现"
    assert body["hits"][0]["source_id"] != cache_id
    assert "发票" in body["hits"][0]["snippet"]


def test_search_marks_the_sources_it_touched(monkeypatch):
    """命中过的库材料要记一次"被用到"（`last_used_at`）。

    它是 `prune` 保留与否的唯一依据：检索从不记账的话，用户天天查的那份材料
    会在 90 天后被当成"没人用过"删掉。断在 `touch` 的入参上而不是时间戳上：
    后者是毫秒精度，同一毫秒内前后相等，断言会变成"几乎恒真"。
    """
    source_id = _ingest(MATERIAL, "cache-review.md")
    touched: list[str] = []
    monkeypatch.setattr(library, "touch", lambda ids: touched.extend(ids) or 0)

    with guest_client() as api:
        assert api.post("/api/knowledge/search",
                        json={"query": "缓存命中率"}).json()["hits"]

    assert touched == [source_id]
