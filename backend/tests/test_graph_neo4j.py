"""需要真 Neo4j 的用例——图存储那一半的断言在库上才成立。

**默认跑法不会红**：这一层的 `graph` fixture 先看 `MINDMAP_TEST_NEO4J_PASSWORD`
有没有，再试连一次，任何一步不成立就 `pytest.skip`（不是 skipif 的静态判断，
所以"端口开着但密码不对"也会落成 skip 并带上原因）。启动方式见
`docs/RAG-DECISIONS.md` 第 8 节或仓库 README 的集成测试一节；本机没有 Neo4j 时
这些用例一律跳过，`pytest` 仍然是绿的。

**隔离**：每个用例前后都 `MATCH (n:Entity) DETACH DELETE n`。这假设目标是**专用
实例**，所以 fixture 里有一条保护：URI 不是 loopback 且没有显式
`MINDMAP_TEST_NEO4J_ALLOW_WIPE=1` 就 skip——一行手滑的 URI 不该清掉别人的图。
Community 版只有 `neo4j` 一个库（`CREATE DATABASE` 要企业版），所以隔离靠清理而不
靠库名；真要并行跑，用 `MINDMAP_TEST_NEO4J_DATABASE` 指到另一个实例。

**驱动是模块级单例**：每个用例都把 `MINDMAP_NEO4J_*` 重新指一遍再
`close_driver()`，否则第二个用例会拿着第一个用例的连接池（`conftest.py` 里也有
一条 autouse 的兜底）。

这一层覆盖的是"原因在引擎里"的断言：幂等靠的是唯一约束与 `MERGE` 语义、去重靠的
是关系属性匹配、方向靠的是 `Path` 的起止点、回滚靠的是事务边界。没有 Neo4j 也能
断言的那一半在 `tests/test_graph_rag.py`。
"""
import os

import pytest

from app.contracts.source import Source
from app.knowledge import graph_extract, graph_ingest, graph_store
from app.knowledge.graph_store import (
    EdgeInput,
    add_edge,
    add_edges_bulk,
    close_driver,
    neighbors,
    stats,
    upsert_entity,
)

pytestmark = pytest.mark.integration

URI_ENV = "MINDMAP_TEST_NEO4J_URI"
USER_ENV = "MINDMAP_TEST_NEO4J_USER"
PASSWORD_ENV = "MINDMAP_TEST_NEO4J_PASSWORD"
DATABASE_ENV = "MINDMAP_TEST_NEO4J_DATABASE"
ALLOW_WIPE_ENV = "MINDMAP_TEST_NEO4J_ALLOW_WIPE"

DEFAULT_URI = "bolt://127.0.0.1:7687"

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _host_of(uri: str) -> str:
    """从 `bolt://host:7687` 里取 host。够用就行，不做 URI 解析。"""
    return uri.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0].strip("[]").lower()


def _settings() -> dict:
    password = os.getenv(PASSWORD_ENV, "").strip()
    if not password:
        pytest.skip(f"未设置 {PASSWORD_ENV}：跳过需要 Neo4j 的用例")
    return {
        "uri": os.getenv(URI_ENV, "").strip() or DEFAULT_URI,
        "user": os.getenv(USER_ENV, "").strip() or "neo4j",
        "password": password,
        "database": os.getenv(DATABASE_ENV, "").strip() or "neo4j",
    }


def _wipe(settings: dict) -> None:
    """清掉测试实例里的图。只删本层造的标签，不碰别的数据。"""
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(settings["uri"],
                                  auth=(settings["user"], settings["password"]),
                                  connection_timeout=5.0)
    try:
        driver.execute_query("MATCH (n:Entity) DETACH DELETE n",
                             database_=settings["database"])
    finally:
        driver.close()


@pytest.fixture(scope="session")
def neo4j_settings():
    """连不上就 skip（不是 fail）：本机没有 Neo4j 是这个项目的常态。"""
    settings = _settings()
    if _host_of(settings["uri"]) not in LOOPBACK_HOSTS:
        if os.getenv(ALLOW_WIPE_ENV, "").strip() not in {"1", "true", "yes"}:
            pytest.skip(f"拒绝清空非本机的 Neo4j（{settings['uri']}）："
                        f"确认是专用实例后设 {ALLOW_WIPE_ENV}=1")
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(settings["uri"],
                                  auth=(settings["user"], settings["password"]),
                                  connection_timeout=3.0)
    try:
        driver.verify_connectivity()
    except Exception as exc:                       # noqa: BLE001 —— 连不上的原因不重要
        pytest.skip(f"本机没有可用的 Neo4j（{type(exc).__name__}）")
    finally:
        driver.close()
    return settings


@pytest.fixture
def graph(neo4j_settings, monkeypatch):
    """把 `MINDMAP_NEO4J_*` 指到测试实例，用完清理。

    yield 出来的是 `graph_store` 模块本身，用例里 `graph.neighbors(...)` 读起来就是
    被测的那一侧。
    """
    monkeypatch.setenv("MINDMAP_NEO4J_URI", neo4j_settings["uri"])
    monkeypatch.setenv("MINDMAP_NEO4J_USER", neo4j_settings["user"])
    monkeypatch.setenv("MINDMAP_NEO4J_PASSWORD", neo4j_settings["password"])
    monkeypatch.setenv("MINDMAP_NEO4J_DATABASE", neo4j_settings["database"])
    close_driver()
    _wipe(neo4j_settings)
    try:
        yield graph_store
    finally:
        close_driver()
        _wipe(neo4j_settings)


# --------------------------------------------------------------- 1. 实体幂等


def test_upsert_is_idempotent(graph):
    """同名同类型两次必须是同一个 id，否则每条边都会挂到一个新节点上。"""
    first = upsert_entity("甲", "人物")
    second = upsert_entity("甲", "人物")
    assert first == second
    assert stats()["entities"] == 1


def test_same_name_different_type_is_two_entities(graph):
    """"苹果"是公司还是水果，是两个不同的节点 —— 合并会造出假边。"""
    company = upsert_entity("苹果", "组织")
    fruit = upsert_entity("苹果", "概念")
    assert company != fruit
    assert stats()["entities"] == 2


def test_entity_without_type_stays_idempotent(graph):
    """`type=None` 也要幂等。

    Cypher 里 `null = null` 求值为 `null`，把 null 当属性值落库会让 `MERGE` 永远
    匹配不到上一行——这条用例钉的是那个坑（落库形态是空串，见模块 docstring）。
    """
    first = upsert_entity("乙", None)
    second = upsert_entity("乙", None)
    assert first == second
    assert stats()["entities"] == 1


def test_edge_without_type_reuses_typed_entity(graph):
    """写边时不带类型，不能给已有实体再造一个空类型分身。

    这是实测踩到的：3000 个实体按 3 条边连完，`stats()` 报出 6000 个实体、3000 个
    孤立——边全挂在 `type=''` 的那个节点上，带类型的那一半从未被走过。
    """
    typed = upsert_entity("甲", "人物")
    assert add_edge("甲", "乙", "依赖", source_id="s1",
                    chunk_start=0, chunk_end=10) is True
    # 甲（人物，被复用）+ 乙（无类型，新建）= 2 个实体、0 个孤立。
    assert stats() == {"entities": 2, "edges": 1, "isolated_entities": 0}
    assert [path.nodes for path in neighbors("甲", hops=1)] == [["甲", "乙"]]
    # 边挂的正是那个带类型的节点：直接查一遍库，读接口可能自己做过变换。
    with graph._transaction(read_only=True) as transaction:
        record = transaction.run(
            "MATCH (a:Entity)-[r:REL]->() RETURN id(a) AS id").single()
    assert int(record["id"]) == typed


def test_unique_constraint_exists(graph):
    """唯一约束必须真建上了：没有它，并发下的 `MERGE` 会造出重复节点。"""
    with graph._transaction(read_only=True) as transaction:
        names = [record["name"] for record in transaction.run(
            "SHOW CONSTRAINTS YIELD name RETURN name")]
    assert graph_store.CONSTRAINT_NAME in names


# ---------------------------------------------------------------- 2. 边去重


def test_duplicate_edge_returns_false(graph):
    """同一条边重复写返回 False，且不抛错、不重复计行。"""
    first = add_edge("甲", "乙", "提出", source_id="s1", chunk_start=0, chunk_end=10)
    second = add_edge("甲", "乙", "提出", source_id="s1", chunk_start=0, chunk_end=10)
    assert first is True
    assert second is False
    assert stats()["edges"] == 1


def test_same_edge_from_another_source_is_a_new_row(graph):
    """同一条边出自另一份材料是**另一条证据**，不能被去重吃掉。"""
    add_edge("甲", "乙", "提出", source_id="s1", chunk_start=0, chunk_end=10)
    again = add_edge("甲", "乙", "提出", source_id="s2", chunk_start=50, chunk_end=60)
    assert again is True
    assert stats()["edges"] == 2


def test_edge_keeps_evidence_offsets(graph):
    """chunk_start / chunk_end 原样存回：下一轮高亮全靠它，偏一位就标错字。"""
    add_edge("甲", "乙", "依赖", source_id="doc-7", chunk_start=17, chunk_end=233)
    paths = neighbors("甲", hops=1)
    assert len(paths) == 1
    assert paths[0].evidence == [("doc-7", 17, 233)]
    with graph._transaction(read_only=True) as transaction:
        record = transaction.run(
            "MATCH ()-[r:REL]->() "
            "RETURN r.source_id AS s, r.chunk_start AS a, r.chunk_end AS b").single()
    assert (record["s"], record["a"], record["b"]) == ("doc-7", 17, 233)


def test_self_loop_is_not_written(graph):
    """自环落库只会让边数虚高：游走里它永远走不到，写它没有意义。"""
    assert add_edge("甲", "甲", "依赖", source_id="s1",
                    chunk_start=0, chunk_end=10) is False
    assert stats()["edges"] == 0


# ------------------------------------------------------- 3. 一跳 / 两跳游走


def test_neighbors_one_and_two_hops(graph):
    """A→B→C：两跳能到 C，且路径里的关系与证据逐条对齐。"""
    add_edge("甲", "乙", "提出", source_id="s1", chunk_start=0, chunk_end=100)
    add_edge("乙", "丙", "依赖", source_id="s1", chunk_start=100, chunk_end=200)

    one = neighbors("甲", hops=1)
    assert [path.nodes for path in one] == [["甲", "乙"]]
    assert one[0].relations == ["提出"]
    assert one[0].directions == ["forward"]
    assert one[0].evidence == [("s1", 0, 100)]

    two = neighbors("甲", hops=2)
    # 短路径先出：这是 `ORDER BY length(p)` 的契约，调用方拿到的前几条必须最近。
    assert [path.nodes for path in two] == [["甲", "乙"], ["甲", "乙", "丙"]]
    deep = two[-1]
    assert deep.hops == 2
    assert deep.relations == ["提出", "依赖"]
    assert deep.evidence == [("s1", 0, 100), ("s1", 100, 200)]
    # 无向之后丙能走回乙——这正是"方向判反也不断路"的意思。
    back = neighbors("丙", hops=1)
    assert [path.nodes for path in back] == [["丙", "乙"]]
    assert back[0].directions == ["reverse"]


def test_neighbors_walks_against_edge_direction(graph):
    """边是 A→B，从 B 出发一跳必须能到 A，且方向要能读出来。

    这是无向游走的全部意义：模型把方向判反时，从错误那一端出发仍然走得到，
    并且调用方还看得出"这条边原本是 A→B"。
    """
    add_edge("甲", "乙", "依赖", source_id="s1", chunk_start=0, chunk_end=10)

    forward = neighbors("甲", hops=1)
    assert [path.nodes for path in forward] == [["甲", "乙"]]
    assert forward[0].directions == ["forward"]

    reverse = neighbors("乙", hops=1)
    assert [path.nodes for path in reverse] == [["乙", "甲"]]
    assert reverse[0].directions == ["reverse"]
    # 方向不同、关系短语相同：只看 relations 会读成"乙依赖甲"，方向位才是判据。
    assert reverse[0].relations == ["依赖"]
    assert reverse[0].evidence == [("s1", 0, 10)]


def test_neighbors_two_hops_can_mix_directions(graph):
    """一条路径可以一跳顺、一跳逆：甲→乙 就是 乙←丙。"""
    add_edge("甲", "乙", "提出", source_id="s1", chunk_start=0, chunk_end=10)
    add_edge("丙", "乙", "使用", source_id="s1", chunk_start=10, chunk_end=20)

    mixed = [path for path in neighbors("甲", hops=2)
             if path.nodes == ["甲", "乙", "丙"]]
    assert len(mixed) == 1
    assert mixed[0].relations == ["提出", "使用"]
    # 第一跳顺着"甲→乙"，第二跳逆着"丙→乙"（从乙走到丙）。
    assert mixed[0].directions == ["forward", "reverse"]
    assert mixed[0].evidence == [("s1", 0, 10), ("s1", 10, 20)]


def test_neighbors_returns_empty_for_unknown_entity(graph):
    assert neighbors("不存在的东西", hops=2) == []
    assert neighbors("", hops=2) == []


def test_neighbors_respects_limit_and_hops(graph):
    """limit 截条数，hops 截深度。"""
    for target in ("乙", "丙", "丁", "戊"):
        add_edge("甲", target, "包含", source_id="s1", chunk_start=0, chunk_end=9)
    add_edge("乙", "己", "包含", source_id="s1", chunk_start=0, chunk_end=9)

    assert len(neighbors("甲", hops=1)) == 4
    assert len(neighbors("甲", hops=1, limit=2)) == 2
    # 上限收在 MAX_LIMIT 内：传一个离谱的值不会把整张图拖出来。
    assert len(neighbors("甲", hops=1, limit=9999)) == 4
    # 0 跳返回空：0 跳的路径是"只有起点、没有证据"，那不该算一次命中。
    assert neighbors("甲", hops=0) == []
    # 两跳才够到己。
    assert len(neighbors("甲", hops=2)) == 5
    assert [path.nodes[-1] for path in neighbors("甲", hops=1)].count("己") == 0


def test_cycle_does_not_loop_forever(graph):
    """两条反向边构成一个环，路径不能无限往返，节点也不能重复。

    Neo4j 的变长模式保证"关系不重复"，但**节点可以重复**（`甲-乙-甲` 是合法
    trail）；语句里的 `all(... single(...))` 把返回收成简单路径，这条用例钉住它。
    """
    add_edge("甲", "乙", "依赖", source_id="s1", chunk_start=0, chunk_end=10)
    add_edge("乙", "甲", "依赖", source_id="s1", chunk_start=10, chunk_end=20)

    paths = neighbors("甲", hops=4)
    # 两条边各走得通一条路，都停在乙：再往前会把甲重复一次，被节点唯一性挡住。
    assert {tuple(path.nodes) for path in paths} == {("甲", "乙")}
    assert all(path.nodes[0] == "甲" for path in paths)
    assert all(len(path.nodes) == len(set(path.nodes)) for path in paths)
    assert sorted(path.directions[0] for path in paths) == ["forward", "reverse"]


# ------------------------------------------------------------ 4. 跨文档关联


def test_neighbors_returns_evidence_from_both_documents(graph):
    """同一实体出现在两份材料里，两条证据都要能取回来 —— 这是这一层的目标。"""
    add_edge("甲", "乙", "提出", source_id="doc-a", chunk_start=0, chunk_end=50)
    add_edge("甲", "丙", "提出", source_id="doc-b", chunk_start=400, chunk_end=460)

    paths = neighbors("甲", hops=1)
    assert len(paths) == 2
    # 同深度内的相对顺序不是契约（Neo4j 不给保证），所以按内容比成集合。
    assert {tuple(path.nodes) for path in paths} == {("甲", "乙"), ("甲", "丙")}
    assert sorted(path.evidence[0][0] for path in paths) == ["doc-a", "doc-b"]


def test_duplicate_paths_from_same_name_are_collapsed(graph):
    """同名不同类型的节点同时当起点时，内容相同的路径只返回一次。

    `neighbors` 的起点条件是 `{name: $name}`，两个节点（"甲"人物 / "甲"组织）都会
    出发；若两者挂着同一条边，不去重就会给出两条一模一样的路径——调用方会把它们
    当成两份独立证据。
    """
    add_edge("甲", "乙", "依赖", source_id="s1", chunk_start=0, chunk_end=10,
             src_type="人物")
    add_edge("甲", "乙", "依赖", source_id="s1", chunk_start=0, chunk_end=10,
             src_type="组织")
    assert stats()["entities"] == 3          # 甲×2 + 乙
    assert len(neighbors("甲", hops=1)) == 1


# ---------------------------------------------------------------- 5. 统计


def test_stats_counts_isolated_entities(graph):
    """孤立实体数回答的是"抽出来有没有连上"，不是"抽了多少"。"""
    upsert_entity("孤", "概念")
    add_edge("甲", "乙", "依赖", source_id="s1", chunk_start=0, chunk_end=10)
    assert stats() == {"entities": 3, "edges": 1, "isolated_entities": 1}


def test_stats_on_empty_graph_is_zero(graph):
    assert stats() == {"entities": 0, "edges": 0, "isolated_entities": 0}


# ------------------------------------------------------------ 6. 批量写入


def bulk_edges(count: int, *, prefix: str = "批") -> list[EdgeInput]:
    """一批互不重复的边：src/dst 都不同，chunk_start 递进。"""
    return [
        EdgeInput(src=f"{prefix}甲{index:03d}", dst=f"{prefix}乙{index:03d}",
                  relation="依赖", source_id="doc-bulk",
                  chunk_start=index, chunk_end=index + 10)
        for index in range(count)
    ]


def test_bulk_writes_500_edges_quickly(graph):
    """500 条边一次 `UNWIND` 写完，返回条数正确，耗时远低于逐条。

    阈值给 10 秒（实测 0.3~1.1 秒，第一条含 JVM 预热；同样的 500 条逐条 `add_edge`
    要 10 秒以上），只用来兜"退化成循环发语句"这种灾难性回归，不测绝对性能。
    "一次 `UNWIND` 而不是 50 条语句"这件事由 `tests/test_graph_rag.py` 里那条数语
    句条数的用例确定性地钉住——时间比在共享机器上不可靠。
    """
    import time

    edges = bulk_edges(500)
    started = time.perf_counter()
    written = add_edges_bulk(edges)
    elapsed = time.perf_counter() - started

    assert written == 500
    assert elapsed < 10.0
    assert stats() == {"entities": 1000, "edges": 500, "isolated_entities": 0}


def test_bulk_rolls_back_entirely_on_midway_failure(graph, monkeypatch):
    """事务中途抛错时，先写进去的实体必须跟边一起退回。

    让"建边"那条语句失败：如果"建实体"那一步被单独提交了，库里就会留下一批没有边
    的实体，而它们看起来"只是没连上"。
    """
    upsert_entity("种子", "概念")
    monkeypatch.setattr(
        graph_store, "CYPHER_ADD_EDGES_BULK",
        "UNWIND $rows AS row\nRETURN this_is_not_cypher",
    )
    with pytest.raises(graph_store.GraphStoreError):
        add_edges_bulk(bulk_edges(10))
    assert stats() == {"entities": 1, "edges": 0, "isolated_entities": 1}


def test_bulk_rejects_inverted_offsets_before_writing(graph):
    """整批在事务外校验：一条坏数据不该让前面几条先进库。"""
    edges = bulk_edges(3)
    edges.append(EdgeInput(src="坏甲", dst="坏乙", relation="依赖", source_id="doc-bulk",
                           chunk_start=100, chunk_end=10))
    with pytest.raises(ValueError):
        add_edges_bulk(edges)
    assert stats() == {"entities": 0, "edges": 0, "isolated_entities": 0}


def test_bulk_skips_self_loops_and_blank_and_counts_written(graph):
    """与 `add_edge` 同一套判断：自环与空字段不写，返回值只数真正落库的。"""
    written = add_edges_bulk([
        EdgeInput(src="甲", dst="乙", relation="依赖", source_id="s1",
                  chunk_start=0, chunk_end=10),
        EdgeInput(src="甲", dst="甲", relation="依赖", source_id="s1",
                  chunk_start=0, chunk_end=10),
        EdgeInput(src="甲", dst="乙", relation="依赖", source_id="s1",
                  chunk_start=0, chunk_end=10),   # 与第一条完全重复
        EdgeInput(src="丙", dst="  ", relation="依赖", source_id="s1",
                  chunk_start=0, chunk_end=10),
    ])
    assert written == 1
    assert stats() == {"entities": 2, "edges": 1, "isolated_entities": 0}


def test_bulk_reuses_typed_entity_without_duplicating(graph):
    """批量写边不传类型时也不能造分身（与 `add_edge` 同一条规则）。"""
    typed = upsert_entity("甲", "人物")
    add_edges_bulk([EdgeInput(src="甲", dst="乙", relation="依赖",
                              source_id="s1", chunk_start=0, chunk_end=10)])
    assert stats() == {"entities": 2, "edges": 1, "isolated_entities": 0}
    with graph._transaction(read_only=True) as transaction:
        record = transaction.run(
            "MATCH (a:Entity)-[r:REL]->() RETURN id(a) AS id").single()
    assert int(record["id"]) == typed


def test_bulk_empty_input_is_a_noop(graph):
    assert add_edges_bulk([]) == 0
    assert stats() == {"entities": 0, "edges": 0, "isolated_entities": 0}


def test_bulk_is_idempotent_across_reruns(graph):
    """同一批边重跑不产生重复：第二次的返回值是 0，不是错误。"""
    edges = bulk_edges(20)
    assert add_edges_bulk(edges) == 20
    assert add_edges_bulk(edges) == 0
    assert stats() == {"entities": 40, "edges": 20, "isolated_entities": 0}


# ------------------------------------------------- 7. 编排入库端到端（真库）


def _extraction(entities, relations=()):
    return graph_extract.GraphExtraction(
        entities=[graph_extract.ExtractedEntity(name=name, type=kind)
                  for name, kind in entities],
        relations=[graph_extract.ExtractedRelation(src=s, dst=d, relation=r)
                   for s, d, r in relations],
    )


def test_ingest_writes_the_graph_end_to_end(graph, monkeypatch):
    """抽取 → 批量落库 → 游走的完整一次，只有真库能跑。

    `graph_ingest` 与 `graph_extract` 的公开接口在这条路径上没有为 Neo4j 改过一个
    字（这也是"接口抽象干不干净"的验收）：它调的是 `add_edges_bulk` 与
    `upsert_entity`，换引擎对它透明。
    """
    import asyncio

    payloads = {
        0: _extraction([("甲", "人物"), ("乙", "组织")], [("甲", "乙", "任职于")]),
        5: _extraction([("乙", "组织"), ("丙", "概念"), ("孤", "概念")],
                       [("乙", "丙", "包含")]),
    }
    calls = []

    async def fake_extract(text, *, source_id, chunk_start=0):
        # 按切点取预设结果：`ingest_graph` 会被调用两次（重跑），用调用序号取会
        # 错位——第二次的第一个块会拿到第二次的第二个 payload，于是造出一条新边。
        calls.append(chunk_start)
        return payloads[chunk_start]

    monkeypatch.setattr(graph_ingest, "extract_graph", fake_extract)
    monkeypatch.setattr(graph_ingest, "_chunk_spans", lambda text: [(0, 5), (5, 10)])
    monkeypatch.setenv("MINDMAP_GRAPH_EXTRACTION", "1")

    source = Source(id="doc-graph", kind="text", text="甲在乙任职，乙包含丙。")
    result = asyncio.run(graph_ingest.ingest_graph(source))

    assert result["chunks"] == 2
    assert result["failed"] == 0
    assert result["entities"] == 4          # 甲 / 乙 / 丙 / 孤
    assert result["edges"] == 2
    assert stats() == {"entities": 4, "edges": 2, "isolated_entities": 1}

    paths = neighbors("甲", hops=2)
    assert [path.nodes for path in paths] == [["甲", "乙"], ["甲", "乙", "丙"]]
    assert paths[-1].evidence == [("doc-graph", 0, 5), ("doc-graph", 5, 10)]

    # 重跑一次：零新边，零新实体 —— 幂等是"可以重跑"而不是"最好别重跑"。
    again = asyncio.run(graph_ingest.ingest_graph(source))
    assert again["edges"] == 0
    assert stats() == {"entities": 4, "edges": 2, "isolated_entities": 1}
