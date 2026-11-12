"""把抽取与存储接起来：一份材料 → 切块 → 实体与边。

这一层**只做编排**：切块复用 `service._chunk_spans`（与检索用的是同一套切点，
证据偏移才能在下一轮和 `EvidenceHit` 对上），抽取交给 `graph_extract`，落库交给
`graph_store`。它自己不判断"什么算实体"，也不写 SQL——那两个判断各有一份实现，
在这里重做一遍就会出现第二套口径。

## 成本闸

`max_chunks` 不是性能参数，是**钱**的闸：一份 30 页的 PDF 有上百块，逐块抽取
就是上百次 LLM 调用。20 块是"一份典型材料的前 20 段"与"一次入库最多 20 次调用"
之间的折中。超出的部分记一条日志就跳过——静默截断会让用户以为整份材料都进图了。

## 落库是幂等的

同一份材料重复入库不会产生重复实体或重复边：实体靠 `UNIQUE(name, type)`、
边靠 `UNIQUE(src_id, dst_id, relation, source_id, chunk_start)`。所以"重跑一次
入库"是安全的，也是修数据时的首选动作（不需要先删后写）。

## 每块一次事务

落库走 `graph_store.add_edges_bulk`：一个块的所有边在**一个连接一个事务**里写完。
逐条 `add_edge` 每条边一次 fsync，实测 12000 行要 136 秒，单事务只要 34 毫秒。
块级（而不是整份材料一次）是有意的：失败粒度与 `failed` 的语义保持一致，一块
坏掉只丢那一块。

没有出现在任何关系里的实体另走 `upsert_entity`——它们本来就没有边，跟着批量
写进去也没东西可写；数量通常在个位数，不值得为此多加一个批量接口。

## 统计口径

  - `chunks`：**尝试抽过的块数**（含失败的），不是成功的块数。
  - `failed`：其中返回 `None`（降级）或写库时抛错的块数。`chunks - failed`
    就是真正贡献了东西的块数。
  - `entities`：本次成功落库的去重实体数（按 `(name, type)`），包含没有边的孤立实体。
    落库失败的块不计入。
  - `edges`：**新写进库**的边数。重复的边不计入——`add_edges_bulk` 只统计被
    `UNIQUE` 放行的那些，重复不是失败，是"上次已经写过"。
"""
from __future__ import annotations

import logging
import sqlite3
import time

from app.contracts.source import Source
from app.knowledge import graph_store
from app.knowledge.graph_extract import extract_graph
from app.knowledge.service import _chunk_spans

logger = logging.getLogger(__name__)

# 一次入库最多抽多少块。见模块 docstring 的「成本闸」。
DEFAULT_MAX_CHUNKS = 20


def _empty_stats() -> dict:
    """没有任何东西可做时的返回值。键必须齐——调用方不会为"空材料"走另一个分支。"""
    return {"chunks": 0, "entities": 0, "edges": 0, "failed": 0, "elapsed_ms": 0}


def _unique_entities(extraction) -> list[tuple[str, str | None]]:
    """块内的实体去重，同名不同 type 只留第一条。

    一块之内一个名字指同一个东西。落两行（"甲"既是人物又是概念）会让
    `add_edge` 拿到的 `src_type` 与另一行失配，边落在其中一行上，另一行成为
    永远连不上的孤立实体——这种半残节点在游走时看不出问题，只会让"跨文档关联"
    少一条路。
    """
    seen: dict[str, str | None] = {}
    for entity in extraction.entities:
        seen.setdefault(entity.name, entity.type)
    return list(seen.items())


async def ingest_graph(source: Source, *, max_chunks: int = DEFAULT_MAX_CHUNKS) -> dict:
    """把一份材料的正文抽成图并落库，返回统计。

    `source.text` 为空（解析失败、纯图片材料）时返回全 0 的统计：这不是错误，
    是"这份材料没有可抽的正文"，调用方不该为此走到异常分支。

    开关关着时每一次 `extract_graph` 都立刻返回 `None`，于是这里会得到
    `chunks == failed == 块数`。**这条路径也走切块**——不额外加一层"开关查询"，
    是因为多一处开关判断就多一处可能与 `graph_extract` 的判断分叉的地方。
    """
    started = time.perf_counter()
    stats = _empty_stats()
    text = source.text or ""
    if not text.strip():
        return stats
    if max_chunks <= 0:
        # 0 是"这轮不要花钱"的显式写法，与"没设"（默认 20）不同。
        logger.info("图谱入库跳过：max_chunks=%d", max_chunks)
        return stats

    spans = _chunk_spans(text)
    if len(spans) > max_chunks:
        logger.info(
            "图谱入库截断：材料 %d 块，只抽前 %d 块（成本闸）",
            len(spans), max_chunks,
        )
        spans = spans[:max_chunks]

    entities: set[tuple[str, str | None]] = set()
    for start, end in spans:
        chunk = text[start:end]
        extraction = await extract_graph(chunk, source_id=source.id, chunk_start=start)
        stats["chunks"] += 1
        if extraction is None:
            stats["failed"] += 1
            continue
        try:
            pairs = _unique_entities(extraction)
            kind_of = dict(pairs)
            written = graph_store.add_edges_bulk([
                graph_store.EdgeInput(
                    src=relation.src, dst=relation.dst, relation=relation.relation,
                    source_id=source.id, chunk_start=start, chunk_end=end,
                    src_type=kind_of.get(relation.src),
                    dst_type=kind_of.get(relation.dst),
                )
                for relation in extraction.relations
            ])
            stats["edges"] += written
            entities.update(pairs)
            # 边两端之外的实体（一次关系都没抽到的那种）才需要单独写。批量入口
            # 已经把每条边两端的实体写进去了，再逐条 upsert 一遍就是几十次多余往返。
            linked = {name for relation in extraction.relations
                      for name in (relation.src, relation.dst)}
            for name, kind in pairs:
                if name in linked:
                    continue
                try:
                    graph_store.upsert_entity(name, kind)
                except (sqlite3.Error, OSError) as exc:
                    # 边已经落库，图的主体是完整的；缺一个孤立实体不影响任何路径，
                    # 所以只记警告，不把整块算作失败（那会让 `failed` 谎报"边也没了"）。
                    logger.warning(
                        "图谱孤立实体写入失败（材料=%s 位置=%d）：%s",
                        source.id[:8], start, type(exc).__name__,
                    )
        except (sqlite3.Error, OSError, ValueError) as exc:
            # 写库失败只影响这一块：之前块落的实体与边留着（它们本身是有效的），
            # 下一块继续。整批放弃的代价是这份材料从头再来一次。
            stats["failed"] += 1
            logger.warning(
                "图谱入库写入失败（材料=%s 位置=%d）：%s",
                source.id[:8], start, type(exc).__name__,
            )

    stats["entities"] = len(entities)
    stats["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    logger.info(
        "图谱入库完成 材料=%s 块=%d 失败=%d 实体=%d 边=%d 耗时=%dms",
        source.id[:8], stats["chunks"], stats["failed"], stats["entities"],
        stats["edges"], stats["elapsed_ms"],
    )
    return stats
