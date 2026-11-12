"""图存储 / 抽取 / 编排三层的用例。

这里一律不联网：`chat_async` 在用例里被替换，`conftest.py` 又把 LLM 凭证清空、
把真实传输层封死，所以"该降级却发了请求"会当场变成断言失败而不是悄悄成功。

数据库由 `conftest.py` 的 `MINDMAP_DB_PATH` 指到每个用例自己的 `tmp_path`，
用例之间不共享库；`test_ingest_leaves_other_tables_alone` 会显式检查这一点，
而不是相信它。

项目没装 pytest-asyncio，异步入口统一用 `asyncio.run` 在同步用例里跑。
"""
import asyncio
import json
import sqlite3
import time
from contextlib import closing

import pytest

from app.contracts.source import Source
from app.knowledge import graph_extract, graph_ingest, graph_store
from app.knowledge.graph_extract import extract_graph
from app.knowledge.graph_ingest import ingest_graph
from app.knowledge.graph_store import (
    EdgeInput,
    add_edge,
    add_edges_bulk,
    neighbors,
    normalize_name,
    stats,
    upsert_entity,
)
from app.knowledge.service import _chunk_spans
from app.mindmaps import repository


def run(coro):
    return asyncio.run(coro)


def rows_in(table: str) -> int:
    """某个表当前的行数；表还不存在时算 0。

    `chunk_vectors` 是被动建的（第一次写向量时才 `CREATE`），所以"不存在"是
    常态，不是异常。
    """
    with closing(sqlite3.connect(repository.db_path())) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            return 0
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def fake_chat(monkeypatch, result):
    """替换模型出口，返回调用记录。

    `result` 可以是返回值、异常对象或协程函数。不参与解析——这几条用例要断言的
    正是"解不出来时怎么办"，让 `chat_async` 的真身去解析反而把被测行为绕开了。
    """
    calls = []

    async def fake(system, user, **kwargs):
        calls.append({"system": system, "user": user, **kwargs})
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return await result()
        return result

    monkeypatch.setattr(graph_extract, "chat_async", fake)
    return calls


def enable(monkeypatch):
    monkeypatch.setenv("MINDMAP_GRAPH_EXTRACTION", "1")


# --------------------------------------------------------------- 1. 实体幂等


def test_upsert_is_idempotent(monkeypatch):
    """同名同类型两次必须是同一个 id，否则每条边都会挂到一个新节点上。"""
    first = upsert_entity("甲", "人物")
    second = upsert_entity("甲", "人物")
    assert first == second
    assert stats()["entities"] == 1


def test_same_name_different_type_is_two_entities(monkeypatch):
    """"苹果"是公司还是水果，是两张不同的实体行 —— 合并会造出假边。"""
    company = upsert_entity("苹果", "组织")
    fruit = upsert_entity("苹果", "概念")
    assert company != fruit
    assert stats()["entities"] == 2


def test_entity_without_type_stays_idempotent(monkeypatch):
    """`type=None` 也要幂等。

    SQLite 的 `UNIQUE` 认为两个 NULL 互不相等，直接把 NULL 落库会让同名实体
    每写一次就多一行——这条用例钉的是那个坑。
    """
    first = upsert_entity("乙", None)
    second = upsert_entity("乙", None)
    assert first == second
    assert stats()["entities"] == 1


def test_edge_without_type_reuses_typed_entity(monkeypatch):
    """写边时不带类型，不能给已有实体再造一个空类型分身。

    这是实测踩到的：3000 个实体按 3 条边连完，`stats()` 报出 6000 个实体、
    3000 个孤立——边全挂在 `type=''` 的那一行上，带类型的那一半从未被走过。
    """
    typed = upsert_entity("甲", "人物")
    assert add_edge("甲", "乙", "依赖", source_id="s1", chunk_start=0, chunk_end=10) is True
    # 甲（人物，被复用）+ 乙（无类型，新建）= 2 个实体、0 个孤立。
    assert stats() == {"entities": 2, "edges": 1, "isolated_entities": 0}
    assert [path.nodes for path in neighbors("甲", hops=1)] == [["甲", "乙"]]
    # 边挂的正是那一行带类型的实体，而不是一个空壳副本。
    with closing(sqlite3.connect(repository.db_path())) as conn:
        src_id = conn.execute("SELECT src_id FROM graph_edges").fetchone()[0]
    assert src_id == typed


def test_empty_name_is_rejected():
    """空名字落库会让所有空名字撞成同一行，必须当场拒绝。"""
    with pytest.raises(ValueError):
        upsert_entity("   ", "人物")


# ---------------------------------------------------------------- 2. 归一化


def test_normalize_whitespace_and_fullwidth():
    """空白与全角必须折叠到同一个键。"""
    assert normalize_name("  张三 ") == "张三"
    assert normalize_name("张三") == "张三"
    assert normalize_name("ＡＢＣ") == "abc"
    assert normalize_name("\u3000张三\u3000") == "张三"
    padded = upsert_entity("  张三 ", "人物")
    assert padded == upsert_entity("张三", "人物")


def test_normalize_english_case():
    """英文大小写折叠；中文原样保留，不拆词。"""
    assert normalize_name("Transformer") == normalize_name("transformer")
    assert normalize_name("张三丰") == "张三丰"
    first = upsert_entity("Transformer", "概念")
    assert first == upsert_entity("transformer", "概念")


# ------------------------------------------------------------- 3. 边去重


def test_duplicate_edge_returns_false():
    """同一条边重复写返回 False，且不抛错、不重复计行。"""
    first = add_edge("甲", "乙", "提出", source_id="s1", chunk_start=0, chunk_end=10)
    second = add_edge("甲", "乙", "提出", source_id="s1", chunk_start=0, chunk_end=10)
    assert first is True
    assert second is False
    assert stats()["edges"] == 1


def test_same_edge_from_another_source_is_a_new_row():
    """同一条边出自另一份材料是**另一条证据**，不能被 UNIQUE 吃掉。"""
    add_edge("甲", "乙", "提出", source_id="s1", chunk_start=0, chunk_end=10)
    again = add_edge("甲", "乙", "提出", source_id="s2", chunk_start=50, chunk_end=60)
    assert again is True
    assert stats()["edges"] == 2


# ------------------------------------------------------------- 4. 边带证据


def test_edge_keeps_evidence_offsets():
    """chunk_start / chunk_end 原样存回：下一轮高亮全靠它，偏一位就标错字。"""
    add_edge("甲", "乙", "依赖", source_id="doc-7", chunk_start=17, chunk_end=233)
    paths = neighbors("甲", hops=1)
    assert len(paths) == 1
    assert paths[0].evidence == [("doc-7", 17, 233)]
    # 直接查库再确认一次：读接口可能自己做过变换，这里要的是存储事实。
    with closing(sqlite3.connect(repository.db_path())) as conn:
        row = conn.execute(
            "SELECT source_id, chunk_start, chunk_end FROM graph_edges"
        ).fetchone()
    assert row == ("doc-7", 17, 233)


def test_edge_rejects_inverted_offsets():
    """区间倒置是调用方的 bug，落库前就要炸——留到高亮时才发现会沉默很久。"""
    with pytest.raises(ValueError):
        add_edge("甲", "乙", "依赖", source_id="s1", chunk_start=100, chunk_end=10)


# ------------------------------------------------------- 5. 一跳 / 两跳游走


def test_neighbors_one_and_two_hops():
    """A→B→C：两跳能到 C，且路径里的关系与证据逐条对齐。"""
    add_edge("甲", "乙", "提出", source_id="s1", chunk_start=0, chunk_end=100)
    add_edge("乙", "丙", "依赖", source_id="s1", chunk_start=100, chunk_end=200)

    one = neighbors("甲", hops=1)
    assert [path.nodes for path in one] == [["甲", "乙"]]
    assert one[0].relations == ["提出"]
    assert one[0].directions == ["forward"]
    assert one[0].evidence == [("s1", 0, 100)]

    two = neighbors("甲", hops=2)
    assert [path.nodes for path in two] == [["甲", "乙"], ["甲", "乙", "丙"]]
    deep = two[-1]
    assert deep.hops == 2
    assert deep.relations == ["提出", "依赖"]
    assert deep.evidence == [("s1", 0, 100), ("s1", 100, 200)]
    # 无向之后丙能走回乙——这正是"方向判反也不断路"的意思。
    back = neighbors("丙", hops=1)
    assert [path.nodes for path in back] == [["丙", "乙"]]
    assert back[0].directions == ["reverse"]


def test_neighbors_walks_against_edge_direction():
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


def test_neighbors_two_hops_can_mix_directions():
    """一条路径可以一跳顺、一跳逆：甲→乙 就是乙←丙。"""
    add_edge("甲", "乙", "提出", source_id="s1", chunk_start=0, chunk_end=10)
    add_edge("丙", "乙", "使用", source_id="s1", chunk_start=10, chunk_end=20)

    paths = neighbors("甲", hops=2)
    mixed = [path for path in paths if path.nodes == ["甲", "乙", "丙"]]
    assert len(mixed) == 1
    assert mixed[0].relations == ["提出", "使用"]
    # 第一跳顺着"甲→乙"，第二跳逆着"丙→乙"（从乙走到丙）。
    assert mixed[0].directions == ["forward", "reverse"]
    assert mixed[0].evidence == [("s1", 0, 10), ("s1", 10, 20)]


def test_neighbors_returns_empty_for_unknown_entity():
    assert neighbors("不存在的东西", hops=2) == []
    assert neighbors("", hops=2) == []


# ------------------------------------------------ 6. limit / hops / 环安全


def test_neighbors_respects_limit_and_hops():
    """limit 截条数（短路优先），hops 截深度。"""
    for target in ("乙", "丙", "丁", "戊"):
        add_edge("甲", target, "包含", source_id="s1", chunk_start=0, chunk_end=9)
    add_edge("乙", "己", "包含", source_id="s1", chunk_start=0, chunk_end=9)

    assert len(neighbors("甲", hops=1)) == 4
    capped = neighbors("甲", hops=1, limit=2)
    assert len(capped) == 2
    # 上限收在 MAX_LIMIT 内：传一个离谱的值不会把整张图拖出来。
    assert len(neighbors("甲", hops=1, limit=9999)) == 4
    # 0 跳返回空：0 跳的路径是"只有起点、没有证据"，那不该算一次命中。
    assert neighbors("甲", hops=0) == []
    # 两跳才够到己。
    assert len(neighbors("甲", hops=2)) == 5
    assert [path.nodes[-1] for path in neighbors("甲", hops=1)].count("己") == 0


def test_cycle_does_not_loop_forever():
    """一条边就够来回，无向游走必须靠 `visited` 挡住无限往返。"""
    add_edge("甲", "乙", "依赖", source_id="s1", chunk_start=0, chunk_end=10)
    add_edge("乙", "甲", "依赖", source_id="s1", chunk_start=10, chunk_end=20)

    paths = neighbors("甲", hops=4)
    # 两条边各走得通一条路，都停在乙：再往前会把甲重复一次，被 visited 挡住。
    assert {tuple(path.nodes) for path in paths} == {("甲", "乙")}
    assert all(path.nodes[0] == "甲" for path in paths)
    assert all(len(path.nodes) == len(set(path.nodes)) for path in paths)
    assert sorted(path.directions[0] for path in paths) == ["forward", "reverse"]


def test_self_loop_is_not_written():
    """自环落库只会让边数虚高：游走里它永远走不到，写它没有意义。"""
    assert add_edge("甲", "甲", "依赖", source_id="s1", chunk_start=0, chunk_end=10) is False
    assert stats()["edges"] == 0
    assert add_edge("甲", "乙", "依赖", source_id="s1", chunk_start=0, chunk_end=10) is True


# ------------------------------------------------------------ 7. 跨文档关联


def test_neighbors_returns_evidence_from_both_documents():
    """同一实体出现在两份材料里，两条证据都要能取回来 —— 这是这一层的目标。"""
    add_edge("甲", "乙", "提出", source_id="doc-a", chunk_start=0, chunk_end=50)
    add_edge("甲", "丙", "提出", source_id="doc-b", chunk_start=400, chunk_end=460)

    paths = neighbors("甲", hops=1)
    assert len(paths) == 2
    assert [path.nodes for path in paths] == [["甲", "乙"], ["甲", "丙"]]
    sources = sorted(path.evidence[0][0] for path in paths)
    assert sources == ["doc-a", "doc-b"]


def test_duplicate_paths_from_same_name_are_collapsed():
    """同名不同类型的实体同时当起点时，内容相同的路径只返回一次。

    `neighbors` 的起点条件是 `name = ?`，两行（"甲"人物 / "甲"组织）都会出发；
    若两行上挂着同一条边，不去重就会给出两条一模一样的路径——调用方会把它们
    当成两份独立证据。
    """
    add_edge("甲", "乙", "依赖", source_id="s1", chunk_start=0, chunk_end=10,
             src_type="人物")
    add_edge("甲", "乙", "依赖", source_id="s1", chunk_start=0, chunk_end=10,
             src_type="组织")
    assert stats()["entities"] == 3          # 甲×2 + 乙
    assert len(neighbors("甲", hops=1)) == 1


def test_stats_counts_isolated_entities():
    """孤立实体数回答的是"抽出来有没有连上"，不是"抽了多少"。"""
    upsert_entity("孤", "概念")
    add_edge("甲", "乙", "依赖", source_id="s1", chunk_start=0, chunk_end=10)
    result = stats()
    assert result == {"entities": 3, "edges": 1, "isolated_entities": 1}


# ------------------------------------------------------------- 8. 抽取解析


GOOD_JSON = {
    "entities": [
        {"name": "乙公司", "type": "组织"},
        {"name": "甲某", "type": "人物"},
        {"name": "丁概念", "type": "概念"},
    ],
    "relations": [
        {"src": "甲某", "dst": "乙公司", "relation": "任职于"},
        # src 不在实体表里：必须丢
        {"src": "他", "dst": "乙公司", "relation": "属于"},
        # dst 不在实体表里：必须丢
        {"src": "甲某", "dst": "戊某", "relation": "依赖"},
        # 单字关系：必须丢
        {"src": "乙公司", "dst": "丁概念", "relation": "依"},
        # 自环：必须丢
        {"src": "甲某", "dst": "甲某", "relation": "依赖"},
    ],
}


def test_extract_parses_entities_and_relations(monkeypatch):
    enable(monkeypatch)
    calls = fake_chat(monkeypatch, GOOD_JSON)
    result = run(extract_graph("甲某任职于乙公司。", source_id="doc-1"))

    assert len(calls) == 1
    assert result is not None
    assert sorted(result.names) == ["丁概念", "乙公司", "甲某"]
    assert [(rel.src, rel.dst, rel.relation) for rel in result.relations] == [
        ("甲某", "乙公司", "任职于"),
    ]


def test_extract_drops_dangling_relations(monkeypatch):
    """关系的 src/dst 不在实体表里时逐条丢弃，但合法实体照常留下。"""
    enable(monkeypatch)
    fake_chat(monkeypatch, GOOD_JSON)
    result = run(extract_graph("材料", source_id="doc-1"))
    assert result is not None
    assert len(result.entities) == 3
    assert len(result.relations) == 1


def test_extract_drops_names_out_of_length_range(monkeypatch):
    """1 字与 31 字的"实体名"都不落库：一个不成词，一个是整句。"""
    enable(monkeypatch)
    fake_chat(monkeypatch, {"entities": [
        {"name": "甲", "type": "人物"},
        {"name": "乙" * 31, "type": "概念"},
        {"name": "正常实体", "type": "概念"},
    ], "relations": []})
    result = run(extract_graph("材料", source_id="doc-1"))
    assert result is not None
    assert result.names == ["正常实体"]


def test_extract_drops_information_free_relations(monkeypatch):
    """"相关""涉及"这类 2 字关系能通过长度闸，必须单独拦。

    它们落库后等价于"这两个名字连着"，会把同一段里共现过的任意两个实体连起来。
    """
    enable(monkeypatch)
    fake_chat(monkeypatch, {
        "entities": [{"name": "乙公司", "type": "组织"}, {"name": "甲某", "type": "人物"}],
        "relations": [
            {"src": "甲某", "dst": "乙公司", "relation": "相关"},
            {"src": "甲某", "dst": "乙公司", "relation": "涉及"},
            {"src": "甲某", "dst": "乙公司", "relation": "任职于"},
        ],
    })
    result = run(extract_graph("材料", source_id="doc-1"))
    assert result is not None
    assert [rel.relation for rel in result.relations] == ["任职于"]


def test_extract_folds_unknown_type_to_none(monkeypatch):
    """白名单外的类型折成 None，不猜同义词 —— 猜错会让同一实体分裂成两行。"""
    enable(monkeypatch)
    fake_chat(monkeypatch, {"entities": [
        {"name": "监管机构", "type": "机构"},
        {"name": "审批流程", "type": "流程"},
    ], "relations": []})
    result = run(extract_graph("材料", source_id="doc-1"))
    assert result is not None
    assert [entity.type for entity in result.entities] == [None, None]


def test_extract_tolerates_string_wrapped_json(monkeypatch):
    """模型把 JSON 当字符串又包一层时，要能解出来（与 query_plan 同一套解析器）。"""
    enable(monkeypatch)
    fake_chat(monkeypatch, json.dumps({"entities": [{"name": "正常实体", "type": "概念"}],
                                       "relations": []}))
    result = run(extract_graph("材料", source_id="doc-1"))
    assert result is not None
    assert result.names == ["正常实体"]


def test_extract_truncates_long_input(monkeypatch, caplog):
    """超长输入截断到 MAX_INPUT_CHARS，且截断这件事要留痕。"""
    enable(monkeypatch)
    calls = fake_chat(monkeypatch, {"entities": [{"name": "正常实体", "type": "概念"}],
                                    "relations": []})
    with caplog.at_level("INFO"):
        result = run(extract_graph("字" * (graph_extract.MAX_INPUT_CHARS + 500),
                                   source_id="doc-1"))
    assert result is not None
    assert len(calls[0]["user"]) < graph_extract.MAX_INPUT_CHARS + 200
    assert any("截断" in record.message for record in caplog.records)


def test_extract_logs_shape_not_content(monkeypatch, caplog):
    """日志里不出现实体名与原文：材料可能是合同、病历、内部制度。"""
    enable(monkeypatch)
    fake_chat(monkeypatch, GOOD_JSON)
    with caplog.at_level("INFO"):
        run(extract_graph("甲某任职于乙公司，负责丁概念。", source_id="doc-1"))
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "图谱抽取" in logged
    for secret in ("甲某", "乙公司", "丁概念", "任职于"):
        assert secret not in logged


# ------------------------------------------------------------- 9. 抽取降级


def test_extract_returns_none_when_switch_off(monkeypatch):
    """开关关着时零调用 —— 不是"调用了但不用结果"。"""
    monkeypatch.setenv("MINDMAP_GRAPH_EXTRACTION", "0")
    calls = fake_chat(monkeypatch, GOOD_JSON)
    assert run(extract_graph("甲某任职于乙公司", source_id="doc-1")) is None
    assert calls == []


def test_extract_returns_none_without_model_config(monkeypatch):
    """开了开关但没配模型：走 `chat_async` 真身，它自己抛，出口仍是 None。"""
    enable(monkeypatch)
    calls = []

    async def spy(system, user, **kwargs):
        calls.append(1)
        # 真身在这里会因为缺少 MINDMAP_LLM_* 抛 GenerationError，而 conftest 已经
        # 把凭证清空了 —— 所以下面这行不需要 mock，也不会发任何请求。
        from app.generation.service import chat_async as real
        return await real(system, user, **kwargs)

    monkeypatch.setattr(graph_extract, "chat_async", spy)
    assert run(extract_graph("甲某任职于乙公司", source_id="doc-1")) is None
    assert len(calls) == 1


def test_extract_returns_none_on_timeout_without_retry(monkeypatch):
    """15 秒超时兜住，且**不重试**：重试只会把入库时间乘以二。"""
    enable(monkeypatch)
    monkeypatch.setattr(graph_extract, "TIMEOUT_SECONDS", 0.05)

    async def slow():
        await asyncio.sleep(1)
        return GOOD_JSON

    calls = fake_chat(monkeypatch, slow)
    assert run(extract_graph("甲某任职于乙公司", source_id="doc-1")) is None
    assert len(calls) == 1


@pytest.mark.parametrize("bad", ["这不是 JSON", "", None, [1, 2, 3]])
def test_extract_returns_none_on_unparsable_output(monkeypatch, bad):
    """非 dict、非 JSON 的返回一律 None，且只调用一次。"""
    enable(monkeypatch)
    calls = fake_chat(monkeypatch, bad)
    assert run(extract_graph("甲某任职于乙公司", source_id="doc-1")) is None
    assert len(calls) == 1


def test_extract_returns_none_when_model_raises(monkeypatch):
    """模型层抛什么都只是"这段没抽到"，不重试、不冒泡。"""
    enable(monkeypatch)
    calls = fake_chat(monkeypatch, RuntimeError("模型挂了"))
    assert run(extract_graph("甲某任职于乙公司", source_id="doc-1")) is None
    assert len(calls) == 1


def test_extract_returns_none_for_blank_text(monkeypatch):
    enable(monkeypatch)
    calls = fake_chat(monkeypatch, GOOD_JSON)
    assert run(extract_graph("   ", source_id="doc-1")) is None
    assert calls == []


def test_extract_returns_none_when_everything_is_filtered(monkeypatch):
    """所有实体都被长度闸删干净、又没有关系：返回 None 而不是空壳对象。"""
    enable(monkeypatch)
    fake_chat(monkeypatch, {"entities": [{"name": "甲", "type": "人物"}], "relations": []})
    assert run(extract_graph("材料", source_id="doc-1")) is None


# ------------------------------------------------------------- 10. 编排入库


def make_source(text: str, source_id: str = "doc-1") -> Source:
    return Source(id=source_id, kind="text", text=text)


def fake_extract(monkeypatch, payloads):
    """替换 ingest 看到的 `extract_graph`，按调用顺序取出预设结果。"""
    calls = []

    async def fake(text, *, source_id, chunk_start=0):
        calls.append({"text": text, "source_id": source_id, "chunk_start": chunk_start})
        return payloads[min(len(calls) - 1, len(payloads) - 1)]

    monkeypatch.setattr(graph_ingest, "extract_graph", fake)
    return calls


def one_chunk(monkeypatch, spans):
    """把切块固定成给定区间，让块数完全可控。"""
    monkeypatch.setattr(graph_ingest, "_chunk_spans", lambda text: list(spans))


def extraction(entities, relations=()):
    return graph_extract.GraphExtraction(
        entities=[graph_extract.ExtractedEntity(name=name, type=kind)
                  for name, kind in entities],
        relations=[graph_extract.ExtractedRelation(src=s, dst=d, relation=r)
                   for s, d, r in relations],
    )


def test_ingest_counts_chunks_entities_and_edges(monkeypatch):
    enable(monkeypatch)
    one_chunk(monkeypatch, [(0, 5), (5, 10), (10, 15)])
    payloads = [
        extraction([("甲", "人物"), ("乙", "组织")], [("甲", "乙", "任职于")]),
        extraction([("乙", "组织"), ("丙", "概念")], [("乙", "丙", "包含")]),
        extraction([("甲", "人物")]),
    ]
    calls = fake_extract(monkeypatch, payloads)

    result = run(ingest_graph(make_source("甲在乙任职，乙包含丙。" + "填充" * 3)))

    assert result["chunks"] == 3
    assert result["failed"] == 0
    assert result["entities"] == 3          # 甲 / 乙 / 丙 去重后
    assert result["edges"] == 2
    assert result["elapsed_ms"] >= 0
    assert [call["chunk_start"] for call in calls] == [0, 5, 10]
    # 三个实体都连上了，所以没有孤立实体 —— 这是"一块一块接起来"的成功形状。
    assert stats() == {"entities": 3, "edges": 2, "isolated_entities": 0}


def test_ingest_max_chunks_truncates(monkeypatch, caplog):
    """max_chunks 是成本闸：超出的块不抽，且要留痕。"""
    enable(monkeypatch)
    one_chunk(monkeypatch, [(0, 2), (2, 4), (4, 6), (6, 8), (8, 10)])
    calls = fake_extract(monkeypatch, [extraction([("甲", "人物")])])
    with caplog.at_level("INFO"):
        result = run(ingest_graph(make_source("0123456789"), max_chunks=2))

    assert result["chunks"] == 2
    assert len(calls) == 2
    assert any("成本闸" in record.message for record in caplog.records)


def test_ingest_counts_failed_chunks(monkeypatch):
    """`extract_graph` 返回 None 的块计入 failed，其余照常落库。"""
    enable(monkeypatch)
    one_chunk(monkeypatch, [(0, 4), (4, 8)])
    fake_extract(monkeypatch, [None, extraction([("甲", "人物")])])
    result = run(ingest_graph(make_source("甲甲乙乙甲甲乙乙")))
    assert result["chunks"] == 2
    assert result["failed"] == 1
    assert result["entities"] == 1


def test_ingest_is_idempotent_across_reruns(monkeypatch):
    """同一份材料重跑入库不产生重复实体或重复边。"""
    enable(monkeypatch)
    one_chunk(monkeypatch, [(0, 10)])
    fake_extract(monkeypatch, [extraction([("甲", "人物"), ("乙", "组织")],
                                          [("甲", "乙", "任职于")])])
    source = make_source("甲在乙任职。甲乙丙丁戊己庚辛")
    first = run(ingest_graph(source))
    second = run(ingest_graph(source))
    assert first["edges"] == 1
    assert second["edges"] == 0     # 第二次没有新边，不是失败
    assert stats() == {"entities": 2, "edges": 1, "isolated_entities": 0}


def test_ingest_skips_blank_text(monkeypatch):
    """没有正文的材料返回全 0，不是异常。"""
    enable(monkeypatch)
    calls = fake_extract(monkeypatch, [extraction([("甲", "人物")])])
    assert run(ingest_graph(make_source("   "))) == {
        "chunks": 0, "entities": 0, "edges": 0, "failed": 0, "elapsed_ms": 0,
    }
    assert calls == []


def test_ingest_uses_real_chunking(monkeypatch):
    """真实切块路径：块数就是 `_chunk_spans` 给出的块数。"""
    enable(monkeypatch)
    payloads = [extraction([(f"实体{index:02d}", "概念")]) for index in range(10)]
    fake_extract(monkeypatch, payloads)
    text = "甲" * 1800
    spans = _chunk_spans(text)
    assert len(spans) >= 2
    result = run(ingest_graph(make_source(text)))
    assert result["chunks"] == len(spans)
    assert spans[0][0] == 0 and spans[-1][1] == len(text)


# ------------------------------------------------------------ 11. 批量写入


def bulk_edges(count: int, *, prefix: str = "批") -> list[EdgeInput]:
    """一批互不重复的边：src/dst 都不同，chunk_start 递进。"""
    return [
        EdgeInput(src=f"{prefix}甲{index:03d}", dst=f"{prefix}乙{index:03d}",
                  relation="依赖", source_id="doc-bulk",
                  chunk_start=index, chunk_end=index + 10)
        for index in range(count)
    ]


class FlakyConnection:
    """第 N 次 `executemany` 抛错的连接代理，用来验证事务回滚。

    只包住真正需要观察的那一个方法，其余全部转发——这样被测代码拿到的仍然是一个
    行为正确的 sqlite3 连接（`with` 的提交/回滚、`close`、`execute` 都还是真的）。
    """

    def __init__(self, conn: sqlite3.Connection, *, fail_on: int = 2) -> None:
        self._conn = conn
        self._calls = 0
        self._fail_on = fail_on

    def executemany(self, sql, rows):
        self._calls += 1
        if self._calls == self._fail_on:
            raise sqlite3.OperationalError("模拟写库中断")
        return self._conn.executemany(sql, rows)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._conn.__exit__(*exc_info)


def test_bulk_writes_500_edges_quickly():
    """500 条边一次事务写完，返回条数正确、耗时远低于逐条。"""
    edges = bulk_edges(500)
    started = time.perf_counter()
    written = add_edges_bulk(edges)
    elapsed = time.perf_counter() - started

    assert written == 500
    # 宽松阈值：逐条写 500 条在同样机械盘上约 5 秒，这里给 2 秒只用来兜"退化成
    # 逐条"这种回归，不测绝对性能。
    assert elapsed < 2.0
    assert stats() == {"entities": 1000, "edges": 500, "isolated_entities": 0}


def test_bulk_is_much_faster_than_one_by_one():
    """同一批边：批量事务必须比逐条快一个量级（差异来自 fsync 次数）。"""
    count = 50
    started = time.perf_counter()
    add_edges_bulk(bulk_edges(count, prefix="快"))
    bulk = time.perf_counter() - started

    started = time.perf_counter()
    for index in range(count):
        add_edge(f"慢甲{index:03d}", f"慢乙{index:03d}", "依赖",
                 source_id="doc-slow", chunk_start=index, chunk_end=index + 10)
    single = time.perf_counter() - started

    assert bulk * 5 < single


def test_bulk_rolls_back_entirely_on_midway_failure(monkeypatch):
    """事务中途抛错时，先写进去的实体必须跟边一起退回。

    批量入口里 `executemany` 有两次（实体、边），让第二次失败——如果实体那次
    的写入被单独提交了，库里就会留下一批没有边的实体，而它们看起来"只是没连上"。
    """
    upsert_entity("种子", "概念")
    monkeypatch.setattr(
        graph_store, "_open",
        lambda: FlakyConnection(repository.connect(), fail_on=2),
    )
    with pytest.raises(sqlite3.Error):
        add_edges_bulk(bulk_edges(10))
    assert stats() == {"entities": 1, "edges": 0, "isolated_entities": 1}


def test_bulk_rejects_inverted_offsets_before_writing():
    """整批在事务外校验：一条坏数据不该让前面几条先进库。"""
    edges = bulk_edges(3)
    edges.append(EdgeInput(src="坏甲", dst="坏乙", relation="依赖", source_id="doc-bulk",
                           chunk_start=100, chunk_end=10))
    with pytest.raises(ValueError):
        add_edges_bulk(edges)
    assert stats() == {"entities": 0, "edges": 0, "isolated_entities": 0}


def test_bulk_skips_self_loops_and_blank_and_counts_written():
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


def test_bulk_reuses_typed_entity_without_duplicating():
    """批量写边不传类型时也不能造分身（与 `add_edge` 同一条规则）。"""
    typed = upsert_entity("甲", "人物")
    add_edges_bulk([EdgeInput(src="甲", dst="乙", relation="依赖",
                              source_id="s1", chunk_start=0, chunk_end=10)])
    assert stats() == {"entities": 2, "edges": 1, "isolated_entities": 0}
    with closing(sqlite3.connect(repository.db_path())) as conn:
        assert conn.execute("SELECT src_id FROM graph_edges").fetchone()[0] == typed


def test_bulk_empty_input_is_a_noop():
    assert add_edges_bulk([]) == 0
    assert stats() == {"entities": 0, "edges": 0, "isolated_entities": 0}


# ----------------------------------------------------- 12. 不污染现有表


def test_ingest_leaves_other_tables_alone(monkeypatch):
    """图存储只写 `graph_*` 两张表；`chunk_vectors` 与 `mindmaps` 行数不变。"""
    enable(monkeypatch)
    one_chunk(monkeypatch, [(0, 10)])
    fake_extract(monkeypatch, [extraction([("甲", "人物"), ("乙", "组织")],
                                          [("甲", "乙", "任职于")])])

    before = {"mindmaps": rows_in("mindmaps"), "chunk_vectors": rows_in("chunk_vectors")}
    run(ingest_graph(make_source("甲在乙任职。甲乙丙丁戊己庚辛")))
    after = {"mindmaps": rows_in("mindmaps"), "chunk_vectors": rows_in("chunk_vectors")}
    assert before == after
    # 顺便证明图确实写进去了，否则上面的"不变"可能只是什么都没做。
    assert stats()["edges"] == 1


def test_graph_tables_are_prefixed():
    """表名前缀是硬约束：共库意味着任何同名都可能是别人的表。"""
    assert graph_store.ENTITY_TABLE.startswith("graph_")
    assert graph_store.EDGE_TABLE.startswith("graph_")
