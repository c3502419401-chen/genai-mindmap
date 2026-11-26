"""知识库只读入口（`knowledge/shelf.py` + `/api/knowledge/library`）的行为约束。

五组，各自钉一类错误：

1. **列表的口径**。`total` 必须是库里的总数而不是本次返回的条数——写成
   `len(items)` 时列表页会显示"共 50 份"，而库里可能有 137 份，用户据此判断
   "我的材料丢了"。
2. **被引用次数按导图去重**。同一张图里既出现在 `source_usage`、又在多个节点的
   `source_refs` 里，只能算一张。逐次累加会算出"被 5 张导图引用"，而实际上只有
   一张图用过它。
3. **预览要能被识破**。正文超过截断长度时 `truncated` 必须为真，
   `total_chars` 要报全文长度；否则用户会拿前 4000 字当全文。
4. **库坏了不等于入口挂了**。库读不出来时列表返回空 + `total=0`，不抛 500；
   导图表里有一行坏 JSON 时该行跳过、`cited_by` 按 0 计，列表照常返回——
   这条路上"库坏了"的正确表现是一条空列表，而不是整页打不开。预览这条路上
   库故障也不能冒充 404：材料不存在与库读不出来是两件事（503 `library_unavailable`
   vs 404 `not_found`），混在一起会让用户以为材料被删了。
5. **引用计数要缓存，且缓存要立刻失效**。算一次 `cited_by` 得把全库导图正文解析
   一遍，开抽屉不该每次都扫；缓存键取导图表指纹（条数 + 最后更新时间），所以存完
   或改完一张图再开抽屉，计数必须当场变；指纹不变的旧数据最多旧 30 秒（TTL）。

全部离线：`conftest.py` 已清空凭证、开关置 0、数据库落在本次用例的 `tmp_path`。
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from conftest import guest_client

from app.contracts.mindmap import (
    FishboneBone,
    FishboneContent,
    FlowContent,
    FlowStage,
    MindMapContent,
    Node,
)
from app.contracts.source import Source, SourceKind, SourceUsage
from app.knowledge import library, shelf
from app.mindmaps import repository


def _source(text: str, *, filename: str | None = None) -> Source:
    return Source(kind=SourceKind.text, status="parsed", text=text, filename=filename)


def _material(chapter: int = 1, *, filler: int = 30) -> str:
    """一份长度超过预览上限、能切出多个父块的材料。"""
    body = "\n\n".join(
        f"### 第{chapter}章 第{index}节\n\n本节讨论预算审批的第 {index} 条规则。" for index in range(filler)
    )
    # 重复到远超 PREVIEW_CHARS（4000 字），专门用来验 truncated。
    return f"# 第{chapter}章 预算审批制度\n\n{body}\n\n" + ("补充说明：制度以财务部解释为准。\n\n" * 300)


def _ingest(text: str, *, filename: str | None = None) -> str:
    return library.ingest(_source(text, filename=filename))


def _save_map(*, usage: list[str], refs: list[str] = (), title: str = "用到了这些材料") -> str:
    """存一张引用了给定持久 source_id 的导图。"""
    content = MindMapContent.model_validate(
        {
            "title": title,
            "root": {
                "id": "root",
                "label": "根",
                "children": [
                    {
                        "id": f"child-{index}",
                        "label": f"分支 {index}",
                        "source_refs": [{"source_id": source_id} for source_id in refs],
                    }
                    for index in range(1)
                ],
            },
            "source_usage": [
                {"source_id": source_id, "kind": "text", "char_count": 10}
                for source_id in usage
            ],
        }
    )
    return repository.create_map(content).id


def _insert_raw_map(map_id: str, content: str) -> None:
    """绕过契约直接往导图表塞一行原始 `content`：模拟坏 JSON / 旧版本残留。

    **显式列出列名**而不是 `INSERT ... VALUES (?, 1, ?, ...)`：`mindmaps` 后来加了
    `owner` 列（最小数据隔离），按位置写的 INSERT 会在加列当天静默错位——把时间戳写进
    第 4 列、`owner` 拿不到值，而报错是"NOT NULL constraint failed"，看起来与
    "这行数据坏了"完全无关。
    """
    with repository.connect() as conn:
        conn.execute(
            "INSERT INTO mindmaps (id, version, content, updated_at) "
            "VALUES (?, 1, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (map_id, content),
        )


@pytest.fixture
def client():
    with guest_client() as api:
        yield api


# ---------------------------------------------------------------- 1 列表口径


def test_empty_library_lists_nothing(client):
    body = client.get("/api/knowledge/library").json()
    assert body == {"items": [], "total": 0}


def test_total_is_the_library_size_not_the_page_size(client):
    ids = [_ingest(_material(chapter)) for chapter in (1, 2, 3)]

    body = client.get("/api/knowledge/library", params={"limit": 2}).json()

    assert len(body["items"]) == 2
    # 库里 3 份、这次只返回 2 份：total 必须是 3，不是 2。
    assert body["total"] == 3
    assert {item["source_id"] for item in body["items"]} <= set(ids)


def test_entry_fields_match_the_stored_material(client):
    source_id = _ingest(_material(1), filename="预算制度.md")

    item = client.get("/api/knowledge/library").json()["items"][0]

    assert item["source_id"] == source_id
    assert item["filename"] == "预算制度.md"
    assert item["kind"] == SourceKind.text.value
    assert item["chars"] > 0
    assert item["chunks"] > 0
    # 没被任何导图引用过：0，而不是缺字段。
    assert item["cited_by"] == 0


@pytest.mark.parametrize("limit", ["0", "-5", "201"])
def test_list_limit_is_bounded(client, limit):
    """`limit` 有上下界：0/负数不该被悄悄吞成一条，大值也不该一次拉整库。

    `max(int(limit), 1)` 那种写法会把 `limit=0` 变成"最近 1 份"——调用方分不清
    "我要一份"和"我写错了"。这里要的是 422，让它自己在请求里改。
    """
    assert client.get("/api/knowledge/library", params={"limit": limit}).status_code == 422


# ---------------------------------------------------------------- 2 被引用次数


def test_cited_by_counts_saved_maps(client):
    cited = _ingest(_material(1))
    _save_map(usage=[cited], title="第一张")
    _save_map(usage=[cited], title="第二张")
    _ingest(_material(2))

    items = {item["source_id"]: item for item in client.get("/api/knowledge/library").json()["items"]}

    assert items[cited]["cited_by"] == 2


def test_citation_in_both_usage_and_refs_counts_as_one_map(client):
    """同一张图里 `source_usage` 与节点 `source_refs` 都指向它，只算一张图。"""
    source_id = _ingest(_material(1))
    _save_map(usage=[source_id], refs=[source_id])

    item = client.get("/api/knowledge/library").json()["items"][0]

    assert item["cited_by"] == 1


# ------------------------------------------- 2a 引用计数要认三种 content 形状


def _save_flow_map(*, refs: list[str], title: str = "流程") -> str:
    """存一张**流程图**，引用只挂在第二个阶段内部的孙节点上。

    挂得这么深是刻意的：`FlowContent` 没有 `.root`，节点藏在 `stages[i].root`
    里。一个"只取 `stages[0].root`"或者"只读阶段根"的半吊子实现都会漏掉这条引用，
    于是这条用例能同时钉住"不崩"和"真的遍历进去了"。

    `source_usage` 里也要带上同一份来源：契约硬拒"节点引用了没写进 `source_usage`
    的来源"，而真实流程图两者都有。
    """
    content = FlowContent(
        title=title,
        source_usage=[_usage(ref) for ref in refs],
        stages=[
            FlowStage(id="s_1", root=Node(id="f_a", label="提交", children=[
                Node(id="f_a1", label="填表"),
            ])),
            FlowStage(id="s_2", root=Node(id="f_b", label="审核", children=[
                Node(id="f_b1", label="核验", children=[
                    Node(id="f_b11", label="对账",
                         source_refs=[{"source_id": ref} for ref in refs]),
                ]),
            ])),
        ],
    )
    return repository.create_map(content).id


def _save_fishbone_map(*, refs: list[str], title: str = "鱼骨") -> str:
    """存一张**鱼骨图**，引用只挂在第二根骨内部的孙节点上（理由同上）。"""
    content = FishboneContent(
        title=title,
        problem="为什么报销要 11 天",
        source_usage=[_usage(ref) for ref in refs],
        bones=[
            FishboneBone(category="人", root=Node(id="h_1", label="人", children=[
                Node(id="h_11", label="审批人休假"),
            ])),
            FishboneBone(category="法", root=Node(id="l_1", label="法", children=[
                Node(id="l_11", label="制度含糊", children=[
                    Node(id="l_111", label="没写额度",
                         source_refs=[{"source_id": ref} for ref in refs]),
                ]),
            ])),
        ],
    )
    return repository.create_map(content).id


def _usage(source_id: str) -> SourceUsage:
    return SourceUsage(source_id=source_id, kind=SourceKind.text, char_count=10)


def test_listing_survives_flow_and_fishbone_maps(client):
    """库里存着一张流程图 + 一张鱼骨图时，`GET /api/knowledge/library` 必须 200。

    **这条用例钉的正是"扫描只认一种形状"这个 500**：`_scan_cited_counts` 原先直接
    读 `content.root`，而 `FlowContent`（阶段在 `stages[i].root`）与 `FishboneContent`
    （骨在 `bones[i].root`）都没有这个属性——一张流程图躺在库里就足以让整个知识库
    列表抛 `AttributeError`，而"被几张图引用"本来只是**旁路展示**，它不该把入口
    打挂（`shelf` 的模块注释里那一条承诺）。

    断言分三半：
      - 200（形状不认识也只是计数错，不能 500）；
      - 读回来的形状**真的是**流程图/鱼骨图，否则这条用例测的是一个不存在的形状
        （判别一变，它就静默变成"两张树"然后永远绿）；
      - 两份材料的 `cited_by` 都是 1，引用挂在阶段/骨的深处——证明遍历真的走进了
        `stages[i]` / `bones[i]` 内部，而不是"没崩但一个节点都没数"。
    """
    in_flow = _ingest(_material(1))
    in_bone = _ingest(_material(2))
    flow_id = _save_flow_map(refs=[in_flow])
    bone_id = _save_fishbone_map(refs=[in_bone])
    assert isinstance(repository.get_map(flow_id).content, FlowContent)
    assert isinstance(repository.get_map(bone_id).content, FishboneContent)

    response = client.get("/api/knowledge/library")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    items = {item["source_id"]: item for item in body["items"]}
    assert items[in_flow]["cited_by"] == 1
    assert items[in_bone]["cited_by"] == 1

# ------------------------------------------------- 2b 引用计数的缓存


@pytest.fixture(autouse=True)
def _fresh_citation_cache():
    """引用计数的缓存挂在模块级：每个用例都从干净状态开始。

    不清的话，"刚存进去的图应该立刻算进计数"这类断言会读到上一个用例留下的
    数字；而"缓存命中没有"本身就是本组要钉的行为。
    """
    shelf._cited_cache = None
    yield
    shelf._cited_cache = None


def _count_scans(monkeypatch) -> list[int]:
    """统计真正扫了一遍导图表的次数（缓存命中时不该增加）。"""
    calls = [0]
    original = repository.list_maps

    def counting(*, tolerant: bool = False):
        calls[0] += 1
        return original(tolerant=tolerant)

    monkeypatch.setattr(repository, "list_maps", counting)
    return calls


def test_listing_twice_scans_the_maps_once(client, monkeypatch):
    """连开两次抽屉：第二次走缓存，不重复解析全库导图。"""
    _ingest(_material(1))
    scans = _count_scans(monkeypatch)

    client.get("/api/knowledge/library")
    client.get("/api/knowledge/library")

    assert scans[0] == 1


def test_saving_a_map_makes_the_count_visible_without_waiting(client, monkeypatch):
    """缓存键是导图表指纹，不是纯时间：存完图再开抽屉，计数必须立刻变。"""
    source_id = _ingest(_material(1))
    scans = _count_scans(monkeypatch)
    before = client.get("/api/knowledge/library").json()["items"][0]
    assert before["cited_by"] == 0

    _save_map(usage=[source_id])
    after = client.get("/api/knowledge/library").json()["items"][0]

    assert after["cited_by"] == 1
    assert scans[0] == 2


def test_updating_a_map_makes_the_count_visible_without_waiting(client):
    """条数没变、只有 `updated_at` 变了（改图保存），指纹也必须失效。

    `update_map` 会刷新 `updated_at`；这里再显式改一次时间戳，避免"创建与更新
    落在同一毫秒"导致指纹恰好相同——那属于 TTL 兜底的边角，不该让主路径的用例
    偶发。
    """
    source_id = _ingest(_material(1))
    map_id = _save_map(usage=[])
    client.get("/api/knowledge/library")  # 先把 cited_by=0 缓存下来
    record = repository.get_map(map_id)
    assert record is not None

    content = MindMapContent.model_validate(
        {
            "title": "改过的一张",
            "root": {"id": "root", "label": "根", "children": []},
            "source_usage": [
                {"source_id": source_id, "kind": "text", "char_count": 10}
            ],
        }
    )
    assert repository.update_map(map_id, record.version, content) == "updated"
    _stamp_map_updated_at(map_id, "2030-01-01T00:00:00.000Z")

    item = client.get("/api/knowledge/library").json()["items"][0]

    assert item["cited_by"] == 1


def test_citation_cache_expires_after_ttl(client, monkeypatch):
    """指纹不变也要有有效期：坏数据在不重扫的情况下最多旧 30 秒。"""
    _ingest(_material(1))
    scans = _count_scans(monkeypatch)
    client.get("/api/knowledge/library")

    monkeypatch.setattr(shelf, "_CITED_CACHE_TTL_SECONDS", -1.0)  # 已过期
    client.get("/api/knowledge/library")

    assert scans[0] == 2


def test_refresh_bypasses_the_cache(client, monkeypatch):
    """`refresh=True` 是给调用方主动重算用的口子（后台统计、调试）。"""
    _ingest(_material(1))
    scans = _count_scans(monkeypatch)

    shelf._cited_counts()
    shelf._cited_counts()
    assert scans[0] == 1

    shelf._cited_counts(refresh=True)
    assert scans[0] == 2


def _stamp_map_updated_at(map_id: str, stamp: str) -> None:
    with repository.connect() as conn:
        conn.execute("UPDATE mindmaps SET updated_at = ? WHERE id = ?", (stamp, map_id))


# ---------------------------------------------------------------- 3 预览与截断


def test_preview_is_truncated_and_says_so(client):
    source_id = _ingest(_material(1))

    body = client.get(f"/api/knowledge/library/{source_id}").json()

    assert body["source_id"] == source_id
    assert body["truncated"] is True
    assert len(body["text"]) == shelf.PREVIEW_CHARS
    assert body["total_chars"] > shelf.PREVIEW_CHARS


def test_short_material_is_not_marked_truncated(client):
    source_id = _ingest("短材料：只有一句话。")

    body = client.get(f"/api/knowledge/library/{source_id}").json()

    assert body["truncated"] is False
    assert body["text"] == "短材料：只有一句话。"
    assert body["total_chars"] == len("短材料：只有一句话。")


def test_unknown_source_is_404(client):
    response = client.get("/api/knowledge/library/没这份")

    assert response.status_code == 404
    body = response.json()
    # 统一错误体：`code` / `message` 在顶层，前端读的是这两项。
    assert body["code"] == "not_found"
    assert body["message"] == "这份来源不在知识库里"


def test_listing_does_not_touch_last_used_at(client):
    """看列表不等于用到材料：`last_used_at` 是 `prune` 的保留依据，不能因浏览而改。"""
    source_id = _ingest(_material(1))
    before = library.list_sources()[0]["last_used_at"]
    _stamp(source_id, "2020-01-01T00:00:00.000Z")

    client.get("/api/knowledge/library")

    assert library.list_sources()[0]["last_used_at"] == "2020-01-01T00:00:00.000Z"
    assert before != "2020-01-01T00:00:00.000Z"


def _stamp(source_id: str, stamp: str) -> None:
    with repository.connect() as conn:
        conn.execute("UPDATE library_sources SET last_used_at = ? WHERE id = ?", (stamp, source_id))


# ---------------------------------------------------------------- 4 库坏了


def test_listing_survives_a_broken_library(client, monkeypatch):
    """库读不出来时列表空着返回，不把入口整个打成 500。"""
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: library_sources")

    monkeypatch.setattr(library, "list_sources", boom)

    response = client.get("/api/knowledge/library")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


def test_broken_library_on_preview_is_503_not_404(client, monkeypatch):
    """库本身读不出来 ≠ 这份材料不存在：前者 503，后者 404。

    原来的实现把 sqlite 故障翻成 `SourceNotFound`，前端于是说"这份来源不在知识
    库里"——用户会以为材料被删了，怎么重试都没用。真正的 404 由
    `test_unknown_source_is_404` 覆盖。
    """
    def boom(source_id):
        raise sqlite3.OperationalError("no such table: library_sources")

    monkeypatch.setattr(library, "load", boom)

    response = client.get("/api/knowledge/library/任意id")

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "library_unavailable"
    assert body["message"] == "知识库暂时读不出来，请稍后重试"


def test_listing_survives_a_corrupt_map_row(client):
    """导图表里有一行坏 JSON：该行跳过，健康的那张照常计数，入口仍是 200。

    这是 `_cited_counts` docstring 里的承诺：坏数据只该让"它自己那张图"消失，
    不能把整个知识库列表打成 500。
    """
    cited = _ingest(_material(1))
    _save_map(usage=[cited], title="健康的一张")
    _insert_raw_map("broken-json", "{这不是 JSON")

    response = client.get("/api/knowledge/library")

    assert response.status_code == 200
    items = {item["source_id"]: item for item in response.json()["items"]}
    assert items[cited]["cited_by"] == 1


def test_corrupt_map_row_still_surfaces_in_strict_mode(client):
    """`tolerant` 只给旁路统计用：列表页/单图读取走的严格模式仍然要抛。"""
    _save_map(usage=[])
    _insert_raw_map("broken-json", "{这不是 JSON")

    with pytest.raises(json.JSONDecodeError):
        repository.list_maps()
    # 同一批数据在容错模式下只剩健康的那张。
    assert len(repository.list_maps(tolerant=True)) == 1


def test_listing_survives_a_broken_mindmap_table(client, monkeypatch):
    """导图表整个读不出来时，列表照常返回，只是 `cited_by` 一律 0。"""
    _ingest(_material(1))

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: mindmaps")

    monkeypatch.setattr(repository, "list_maps", boom)

    response = client.get("/api/knowledge/library")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["cited_by"] == 0