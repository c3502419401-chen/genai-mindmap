"""图存储 / 抽取 / 编排三层的用例——**不需要 Neo4j 的那一半**。

这一层跑在默认 `pytest` 里，不碰任何服务：

  - **纯函数**：`normalize_name` 的三条折叠规则、`GraphPath` / `EdgeInput` 的构造契约；
  - **未配置时的降级**：`conftest.py` 把 `MINDMAP_NEO4J_*` 全清了，所以"图存储没
    配置"是这里的默认前提——写接口抛 `GraphStoreError`（`OSError` 子类），读接口
    返回空。这几条钉的正是 `graph_ingest` 那条 `except (sqlite3.Error, OSError,
    ValueError)` 还接得住新引擎的异常；
  - **抽取**（`graph_extract`）全部用例：它不碰存储；
  - **编排**（`graph_ingest`）里凡是能把 store 换掉的：块数、成本闸、`failed` 计数、
    孤立实体只对没边的实体 upsert；
  - **Cypher 的静态校验**：占位符与传参键一致、唯一一处内插只能来自 int、路径解析
    的方向判定（用假 Path 对象，不连库）。

需要真 Neo4j 的那一半在 `tests/test_graph_neo4j.py`，带 `integration` 标记，连不上
就 skip。两边的分界不是"好不好测"，而是"这条断言的原因是否在存储引擎里"：比如
"同名同类型幂等"必须真写一次才知道，"归一化把全角折平"跟引擎无关。

这里一律不联网：`chat_async` 在用例里被替换，`conftest.py` 又把 LLM 凭证清空、
把真实传输层封死，所以"该降级却发了请求"会当场变成断言失败而不是悄悄成功。
项目没装 pytest-asyncio，异步入口统一用 `asyncio.run` 在同步用例里跑。
"""
import asyncio
import inspect
import json
import re
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.contracts.source import Source
from app.knowledge import graph_extract, graph_ingest, graph_store
from app.knowledge.graph_extract import extract_graph
from app.knowledge.graph_ingest import ingest_graph
from app.knowledge.graph_store import (
    FORWARD,
    REVERSE,
    EdgeInput,
    GraphPath,
    GraphStoreError,
    GraphStoreNotConfigured,
    add_edge,
    add_edges_bulk,
    neighbors,
    normalize_name,
    stats,
    upsert_entity,
)
from app.knowledge.service import _chunk_spans


def run(coro):
    return asyncio.run(coro)


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


# ---------------------------------------------------------------- 1. 归一化


def test_normalize_whitespace_and_fullwidth():
    """空白与全角必须折叠到同一个键，否则抽取层的轻微差异每次都造一个新节点。"""
    assert normalize_name("  张三 ") == "张三"
    assert normalize_name("张三") == "张三"
    assert normalize_name("ＡＢＣ") == "abc"
    assert normalize_name("\u3000张三\u3000") == "张三"


def test_normalize_english_case():
    """英文大小写折叠；中文原样保留，不拆词。"""
    assert normalize_name("Transformer") == normalize_name("transformer")
    assert normalize_name("张三丰") == "张三丰"


def test_normalize_drops_control_chars_and_handles_empty():
    """控制字符一并剔除（日志与终端会被它搞乱）；空输入的出口只有一个空串。"""
    assert normalize_name("甲\x00乙\x1f丙") == "甲乙丙"
    assert normalize_name("") == ""
    assert normalize_name(None) == ""
    # 非字符串先转字符串：模型输出什么都可能给，不想在上游逐字段断言类型。
    assert normalize_name(1) == "1"


# ------------------------------------------------------- 2. GraphPath / EdgeInput


def test_graph_path_hops_counts_relations():
    """`hops` 是关系条数，不是节点数——差一条就会让"走了几步"读错。"""
    path = GraphPath(nodes=["甲", "乙", "丙"], relations=["提出", "依赖"],
                     evidence=[("s1", 0, 10), ("s1", 10, 20)],
                     directions=[FORWARD, REVERSE])
    assert path.hops == 2
    assert len(path.directions) == path.hops
    assert len(path.evidence) == path.hops


def test_graph_path_fills_missing_directions_as_forward():
    """手工构造时漏写 `directions` 兜成"全正向"：静默补齐长度会掩盖存储层 bug，
    所以只兜空列表这一种。"""
    path = GraphPath(nodes=["甲", "乙"], relations=["依赖"])
    assert path.directions == [FORWARD]


def test_graph_path_without_relations_has_no_hops():
    """0 跳路径不存在；真构造出来时 `hops` 是 0，不是 1。"""
    path = GraphPath(nodes=["甲"])
    assert path.hops == 0
    assert path.directions == [] and path.relations == [] and path.evidence == []


def test_edge_input_keeps_field_order_by_name():
    """`EdgeInput` 存在的理由就是"字段名可见"：位置参数调换不该被无声接受。"""
    item = EdgeInput(src="甲", dst="乙", relation="依赖", source_id="doc-1",
                     chunk_start=17, chunk_end=233, src_type="人物")
    assert (item.src, item.dst, item.relation) == ("甲", "乙", "依赖")
    assert (item.chunk_start, item.chunk_end) == (17, 233)
    assert item.src_type == "人物" and item.dst_type is None


# ------------------------------------------------- 3. 未配置时的降级契约


def test_unconfigured_writes_raise_graph_store_error():
    """没配密码 → 写接口抛专用异常，且它必须是 `OSError` 的子类。

    后一条是接口契约：`graph_ingest` 按 `except (sqlite3.Error, OSError,
    ValueError)` 计 `failed`，异常类型换了却没进这棵树的话，`failed` 会静默失效。
    """
    with pytest.raises(GraphStoreNotConfigured):
        upsert_entity("甲", "人物")
    with pytest.raises(GraphStoreError):
        add_edge("甲", "乙", "依赖", source_id="s1", chunk_start=0, chunk_end=10)
    with pytest.raises(GraphStoreError):
        add_edges_bulk([EdgeInput(src="甲", dst="乙", relation="依赖", source_id="s1",
                                  chunk_start=0, chunk_end=10)])
    assert issubclass(GraphStoreError, OSError)
    assert issubclass(GraphStoreNotConfigured, GraphStoreError)


def test_unconfigured_reads_return_empty():
    """读接口降级成空值：查询侧不该为"图没建好"准备第二个分支。"""
    assert neighbors("甲", hops=2) == []
    assert stats() == {"entities": 0, "edges": 0, "isolated_entities": 0}


def test_empty_name_is_rejected_before_any_connection():
    """空名字落库会让所有空名字撞成同一行，必须在拿连接之前就拒绝。"""
    with pytest.raises(ValueError):
        upsert_entity("   ", "人物")


def test_self_loop_and_blank_fields_are_not_written():
    """自环与空字段返回 False，不是错误——它们不该让整次入库算失败。"""
    assert add_edge("甲", "甲", "依赖", source_id="s1", chunk_start=0,
                    chunk_end=10) is False
    assert add_edge("甲", "  ", "依赖", source_id="s1", chunk_start=0,
                    chunk_end=10) is False
    assert add_edge("甲", "乙", "   ", source_id="s1", chunk_start=0,
                    chunk_end=10) is False


def test_inverted_offsets_are_rejected_before_writing():
    """区间倒置是调用方的 bug：落库前就炸，别等回填高亮时才发现标错字。"""
    with pytest.raises(ValueError):
        add_edge("甲", "乙", "依赖", source_id="s1", chunk_start=100, chunk_end=10)
    with pytest.raises(ValueError):
        add_edges_bulk([EdgeInput(src="甲", dst="乙", relation="依赖", source_id="s1",
                                  chunk_start=100, chunk_end=10)])


def test_bulk_sends_one_statement_per_role(monkeypatch):
    """批量写是 `UNWIND`，不是循环：50 条边只发三条语句。

    这条比"批量比逐条快"可靠得多——时间比在共享机器上会被 JVM 预热与调度噪声吃掉，
    而"语句条数"是确定的。退化成 `for row in rows: tx.run(...)` 时这里立刻变成 50。
    """
    transaction = install_fake_transaction(
        monkeypatch,
        RecordingTransaction(answers={"ORDER BY name, created_at": []}),
    )
    edges = [EdgeInput(src=f"批甲{index:03d}", dst=f"批乙{index:03d}", relation="依赖",
                       source_id="doc-bulk", chunk_start=index, chunk_end=index + 10)
             for index in range(50)]
    add_edges_bulk(edges)

    assert len(transaction.calls) == 3
    picked, entities, relations = transaction.calls
    # 取候选：一次带上全部名字（100 个，去重后）
    assert len(picked[1]["names"]) == 100
    # 建实体：一次带上全部缺失的 (name, type)
    assert len(entities[1]["rows"]) == 100
    # 建边：一次带上全部 50 行，每行自带解析后的类型
    assert len(relations[1]["rows"]) == 50
    assert relations[1]["rows"][0]["src_type"] == ""


def test_empty_bulk_input_is_a_noop():
    """空批次直接返回 0，一次往返都不发。"""
    assert add_edges_bulk([]) == 0


def test_unreachable_server_degrades_like_unconfigured(monkeypatch):
    """配了密码但连不上：读接口仍然返回空，写接口抛 `GraphStoreError`。

    指向 1 号端口（被拒）而不是一个黑洞地址，连接会立刻失败；三个超时也压到 0.2 秒
    ——真实默认值是秒级，这里要的是"降级路径通"，不是测驱动的重试时长。
    """
    monkeypatch.setenv("MINDMAP_NEO4J_URI", "bolt://127.0.0.1:1")
    monkeypatch.setenv("MINDMAP_NEO4J_PASSWORD", "not-a-real-password")
    monkeypatch.setattr(graph_store, "CONNECT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(graph_store, "ACQUISITION_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(graph_store, "RETRY_TIME_SECONDS", 0.2)
    assert neighbors("甲", hops=1) == []
    assert stats() == {"entities": 0, "edges": 0, "isolated_entities": 0}
    with pytest.raises(GraphStoreError):
        upsert_entity("甲", "人物")


# ------------------------------------------------------------- 4. 抽取解析


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
    """白名单外的类型折成 None，不猜同义词 —— 猜错会让同一实体分裂成两个节点。"""
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


# ------------------------------------------------------------- 5. 抽取降级


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


# --------------------------------------------------- 6. 编排入库（store 被换掉）


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


def fake_store(monkeypatch, *, bulk_raises=None, upsert_raises=None):
    """把 `graph_ingest` 真正会调的两件套换掉，返回调用记录。

    编排层要断言的是"调了谁、拿了几次、失败怎么记"，不是"数据最后在哪"——后者是
    `tests/test_graph_neo4j.py` 的事。换掉 store 之后这一层的用例与有没有 Neo4j
    完全无关，也不再需要每个用例都起一遍库。
    """
    calls = {"bulk": [], "upsert": []}

    def bulk(edges):
        batch = list(edges)
        calls["bulk"].append(batch)
        if bulk_raises is not None:
            raise bulk_raises
        return len(batch)

    def upsert(name, kind=None):
        calls["upsert"].append((name, kind))
        if upsert_raises is not None:
            raise upsert_raises
        return 1

    monkeypatch.setattr(graph_ingest.graph_store, "add_edges_bulk", bulk)
    monkeypatch.setattr(graph_ingest.graph_store, "upsert_entity", upsert)
    return calls


def test_ingest_counts_chunks_entities_and_edges(monkeypatch):
    enable(monkeypatch)
    one_chunk(monkeypatch, [(0, 5), (5, 10), (10, 15)])
    payloads = [
        extraction([("甲", "人物"), ("乙", "组织")], [("甲", "乙", "任职于")]),
        extraction([("乙", "组织"), ("丙", "概念")], [("乙", "丙", "包含")]),
        extraction([("甲", "人物")]),
    ]
    calls = fake_extract(monkeypatch, payloads)
    store = fake_store(monkeypatch)

    result = run(ingest_graph(make_source("甲在乙任职，乙包含丙。" + "填充" * 3)))

    assert result["chunks"] == 3
    assert result["failed"] == 0
    assert result["entities"] == 3          # 甲 / 乙 / 丙 去重后
    assert result["edges"] == 2
    assert result["elapsed_ms"] >= 0
    assert [call["chunk_start"] for call in calls] == [0, 5, 10]
    # 每块一次批量写入，块内的证据区间就是切点本身 —— 偏一位高亮就标错字。
    assert len(store["bulk"]) == 3
    first = store["bulk"][0][0]
    assert (first.src, first.dst, first.relation) == ("甲", "乙", "任职于")
    assert (first.chunk_start, first.chunk_end) == (0, 5)
    assert (first.src_type, first.dst_type) == ("人物", "组织")


def test_ingest_max_chunks_truncates(monkeypatch, caplog):
    """max_chunks 是成本闸：超出的块不抽，且要留痕。"""
    enable(monkeypatch)
    one_chunk(monkeypatch, [(0, 2), (2, 4), (4, 6), (6, 8), (8, 10)])
    calls = fake_extract(monkeypatch, [extraction([("甲", "人物")])])
    fake_store(monkeypatch)
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
    fake_store(monkeypatch)
    result = run(ingest_graph(make_source("甲甲乙乙甲甲乙乙")))
    assert result["chunks"] == 2
    assert result["failed"] == 1
    assert result["entities"] == 1


def test_ingest_counts_chunk_failed_when_store_raises(monkeypatch):
    """写库失败只影响这一块：`failed` +1，异常不冒泡。

    `GraphStoreError` 在这里被当成 `OSError` 接住——这条断言就是"换引擎不能换
    异常树"的现场证明。
    """
    enable(monkeypatch)
    one_chunk(monkeypatch, [(0, 4), (4, 8)])
    fake_extract(monkeypatch, [
        extraction([("甲", "人物"), ("乙", "组织")], [("甲", "乙", "任职于")]),
        extraction([("丙", "概念"), ("丁", "概念")], [("丙", "丁", "包含")]),
    ])
    fake_store(monkeypatch, bulk_raises=GraphStoreError("模拟写库中断"))
    result = run(ingest_graph(make_source("甲在乙任职，丙丁相关。填充填充")))

    assert result["chunks"] == 2
    assert result["failed"] == 2      # 两块都写失败
    assert result["edges"] == 0


def test_ingest_skips_blank_text(monkeypatch):
    """没有正文的材料返回全 0，不是异常。"""
    enable(monkeypatch)
    calls = fake_extract(monkeypatch, [extraction([("甲", "人物")])])
    store = fake_store(monkeypatch)
    assert run(ingest_graph(make_source("   "))) == {
        "chunks": 0, "entities": 0, "edges": 0, "failed": 0, "elapsed_ms": 0,
    }
    assert calls == [] and store["bulk"] == [] and store["upsert"] == []


def test_ingest_uses_real_chunking(monkeypatch):
    """真实切块路径：块数就是 `_chunk_spans` 给出的块数。"""
    enable(monkeypatch)
    payloads = [extraction([(f"实体{index:02d}", "概念")]) for index in range(10)]
    fake_extract(monkeypatch, payloads)
    fake_store(monkeypatch)
    text = "甲" * 1800
    spans = _chunk_spans(text)
    assert len(spans) >= 2
    result = run(ingest_graph(make_source(text)))
    assert result["chunks"] == len(spans)
    assert spans[0][0] == 0 and spans[-1][1] == len(text)


def test_ingest_upserts_only_entities_without_edges(monkeypatch):
    """批量入口已经把两端实体写进去了，孤立实体才需要单独 upsert。

    对每个实体都再 upsert 一遍是几十次多余往返；漏掉孤立实体则会让"抽出来没连上"
    的那一半彻底消失（`stats` 里的孤立实体数永远是 0，那个信号就废了）。
    """
    enable(monkeypatch)
    one_chunk(monkeypatch, [(0, 10)])
    fake_extract(monkeypatch, [
        extraction([("甲", "人物"), ("乙", "组织"), ("孤", "概念")],
                   [("甲", "乙", "任职于")]),
    ])
    store = fake_store(monkeypatch)
    run(ingest_graph(make_source("甲在乙任职。甲乙丙丁戊己庚辛")))

    assert store["upsert"] == [("孤", "概念")]


def test_ingest_isolated_upsert_failure_does_not_fail_chunk(monkeypatch):
    """孤立实体写失败只记警告：图的主体（边）已经落库了，把它算作失败会谎报。"""
    enable(monkeypatch)
    one_chunk(monkeypatch, [(0, 10)])
    fake_extract(monkeypatch, [
        extraction([("甲", "人物"), ("乙", "组织"), ("孤", "概念")],
                   [("甲", "乙", "任职于")]),
    ])
    fake_store(monkeypatch, upsert_raises=GraphStoreError("模拟孤立实体写入失败"))
    result = run(ingest_graph(make_source("甲在乙任职。甲乙丙丁戊己庚辛")))

    assert result["failed"] == 0
    assert result["edges"] == 1


# ------------------------------------------------------- 7. Cypher 的静态校验


class FakeCounters:
    def __init__(self, created: int = 0) -> None:
        self.relationships_created = created
        self.nodes_created = 0


class FakeResult:
    def __init__(self, records=(), created: int = 0) -> None:
        self._records = list(records)
        self._summary = SimpleNamespace(counters=FakeCounters(created))

    def single(self):
        return self._records[0] if self._records else None

    def consume(self):
        return self._summary

    def __iter__(self):
        return iter(self._records)


class RecordingTransaction:
    """记下每条 Cypher 与它的参数，并按语句内容返回预先安排的结果。

    用它就能在没有 Neo4j 的机器上跑完 `upsert_entity` / `add_edge` /
    `add_edges_bulk` / `stats` / `neighbors` 的**语句层**，从而断言"每个 `$占位符`
    都有对应参数"——这条断言在真库上只会以"参数缺失异常"的形式出现，排查成本高
    得多。
    """

    def __init__(self, answers=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.answers = answers or {}

    def run(self, query: str, params=None, **_kwargs):
        params = dict(params or {})
        self.calls.append((query, params))
        for needle, payload in self.answers.items():
            if needle in query:
                if isinstance(payload, int):
                    return FakeResult(created=payload)
                return FakeResult(records=payload)
        return FakeResult()


@contextmanager
def _pass_through(transaction):
    yield transaction


def install_fake_transaction(monkeypatch, transaction):
    monkeypatch.setattr(
        graph_store, "_transaction",
        lambda read_only=False: _pass_through(transaction),
    )
    return transaction


def test_every_cypher_placeholder_is_bound(monkeypatch):
    """每条语句里的 `$x` 都必须出现在传参里，且传参里没有多余键。

    Neo4j 对缺失参数报错、对多余参数沉默——两边都会让"改了语句却忘了改传参"这类
    错误活到运行时。这条用例把两侧钉死在一起。
    """
    transaction = install_fake_transaction(
        monkeypatch,
        RecordingTransaction(answers={
            # 三条 needle 各自唯一，按插入顺序匹配：建实体 → 批量取候选 → 单条取候选。
            "RETURN id(e) AS id": [{"id": 1}],
            "ORDER BY name, created_at": [],
            "RETURN e.type AS type": [{"type": "人物"}],
        }),
    )
    upsert_entity("甲", "人物")
    add_edge("甲", "乙", "依赖", source_id="s1", chunk_start=0, chunk_end=10)
    add_edges_bulk([EdgeInput(src="甲", dst="乙", relation="依赖", source_id="s1",
                              chunk_start=0, chunk_end=10)])
    stats()
    neighbors("甲", hops=2)

    assert transaction.calls, "一条语句都没发，用例本身失效了"
    for query, params in transaction.calls:
        placeholders = set(re.findall(r"\$([A-Za-z_]\w*)", query))
        assert placeholders <= set(params), f"参数缺失：{placeholders - set(params)}\n{query}"
        assert set(params) <= placeholders, f"多余参数：{set(params) - placeholders}\n{query}"


def test_walk_query_interpolates_only_clamped_ints():
    """变长路径的唯一一处内插：值只能是 0~MAX_HOPS 的 int。

    这是防注入的正面证据——Cypher 的 `[*1..$hops]` 不接受参数，只能拼字符串，所以
    "拼进去的东西是什么"必须被钉住。非整数进不去（`int()` 会先抛），超上限也会被夹。
    """
    assert "[*1..5]-(other" in graph_store._walk_query(999)
    assert "[*1..3]-(other" in graph_store._walk_query("3")
    assert graph_store._walk_query(0) == graph_store.CYPHER_WALK.replace("{hops}", "0")
    for hostile in ("1]-(x) DETACH DELETE x //", "2 MATCH (n) DELETE n", None):
        with pytest.raises((TypeError, ValueError)):
            graph_store._walk_query(hostile)
    assert graph_store._clamp_hops(-3) == 0
    assert graph_store._clamp_limit(0) == 1


def test_labels_are_module_constants_used_by_every_statement():
    """标签、关系类型、约束名都在常量里，且每条语句都从常量取。

    改标签却漏改某一条语句的结果是"写入看不见、查询永远空"——这种错在测试里表现为
    某条用例偶然失败，在线上表现为整层静默失效。
    """
    queries = [value for name, value in vars(graph_store).items()
               if name.startswith("CYPHER_") and isinstance(value, str)]
    assert len(queries) >= 9, "CYPHER_* 常量少了几条？"
    assert f":{graph_store.ENTITY_LABEL}" in graph_store.CYPHER_WALK
    assert f":{graph_store.EDGE_TYPE}" in graph_store.CYPHER_ADD_EDGE
    assert graph_store.CONSTRAINT_NAME in graph_store.CYPHER_CONSTRAINT
    for query in queries:
        # 旧 SQLite 版的表名不该以任何形式残留。
        assert "graph_entities" not in query and "graph_edges" not in query


def test_no_user_value_is_interpolated_into_cypher():
    """语句常量里没有 `%s` / `%(name)s`，内插占位只有一个且只出现在游走语句里。

    这条是静态的、粗糙的，但它挡住的是最容易复发的一类改动：有人为了"少一次往返"
    把实体名格式化进语句。值只能走参数绑定；唯一的例外是变长路径的长度，因为
    Cypher 的 `[*1..$hops]` 不接受参数（见上一条用例）。
    """
    for name, value in vars(graph_store).items():
        if not (name.startswith("CYPHER_") and isinstance(value, str)):
            continue
        assert "%s" not in value, name
        assert "%(" not in value, name
        if "{hops}" in value:
            assert name == "CYPHER_WALK", name
    # 唯一的替换点：整个模块里只有一处把 {hops} 换成值。
    source = inspect.getsource(graph_store)
    assert source.count('replace("{hops}"') == 1


class FakeNode:
    def __init__(self, name: str, element_id: str) -> None:
        self._props = {"name": name}
        self.element_id = element_id

    def __getitem__(self, key):
        return self._props[key]


class FakeRelationship:
    def __init__(self, props, start, end) -> None:
        self._props = props
        self.start_node = start
        self.end_node = end

    def __getitem__(self, key):
        return self._props[key]


class FakePath:
    def __init__(self, nodes, relationships) -> None:
        self.nodes = nodes
        self.relationships = relationships


def test_path_parser_reads_directions_from_relationship_ends():
    """方向靠起止点比对读出来：`nodes[i]` 是关系起点即 FORWARD，是终点即 REVERSE。

    用假 Path 对象测——这是纯逻辑，不该为了它起一个库。真库上的同一条断言在
    `tests/test_graph_neo4j.py`，那一条证明的是"Neo4j 返回的 Path 形状与这里假设
    的一致"。
    """
    first, second = FakeNode("甲", "n1"), FakeNode("乙", "n2")
    forward_edge = FakeRelationship(
        {"relation": "提出", "source_id": "s1", "chunk_start": 0, "chunk_end": 10},
        first, second)
    path = graph_store._parse_path({"p": FakePath([first, second], [forward_edge])})
    assert path is not None
    assert path.nodes == ["甲", "乙"]
    assert path.relations == ["提出"]
    assert path.directions == [FORWARD]
    assert path.evidence == [("s1", 0, 10)]

    # 同一条边，但路径从乙出发：起止点没变，位置反了 → REVERSE。
    reverse_edge = FakeRelationship(
        {"relation": "提出", "source_id": "s1", "chunk_start": 0, "chunk_end": 10},
        first, second)
    path = graph_store._parse_path({"p": FakePath([second, first], [reverse_edge])})
    assert path is not None
    assert path.directions == [REVERSE]


def test_path_parser_drops_malformed_paths():
    """形状不对整条丢掉：错位的路径会让证据指向错误的关系，界面上看不出来。"""
    first, second = FakeNode("甲", "n1"), FakeNode("乙", "n2")
    missing_props = FakeRelationship({"relation": "提出"}, first, second)
    assert graph_store._parse_path({"p": FakePath([first, second],
                                                  [missing_props])}) is None
    # 关系数与节点数对不上（少一条边）也算坏路径。
    good = FakeRelationship(
        {"relation": "提出", "source_id": "s1", "chunk_start": 0, "chunk_end": 10},
        first, second)
    assert graph_store._parse_path({"p": FakePath([first, second, second], [good])}) is None
