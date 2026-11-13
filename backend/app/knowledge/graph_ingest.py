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

## 边的 `source_id` 是持久 id（`library.ingest` 给的）

原来存的是 `Source.id`，而它每次都不同（`uuid4` 默认值），所以 `neighbors` 返回
的 `evidence` 在下一个请求里指向一个不存在的来源——这正是 `docs/RAG-DECISIONS.md`
第 11 节列的第一个前置条件。现在先 `library.ingest(source)` 拿到跨请求稳定的 id，
再拿它当证据键；`ingest_graph(..., source_id=...)` 允许调用方显式指定（重跑入库、
导入历史数据时不想再写一次正文）。

**幂等的语义因此变得更强**：同一份正文（`content_hash` 相同）无论文件名、上传
次数、来源 `uuid4` 怎么变，都落到同一个 `source_id` 上，所以重复入库命中的是同一
批边，而不是"看起来一样的另一批"。

`chunk_start` / `chunk_end` **仍然会漂**：材料重新解析后文本可能变（换解析器、
改清洗规则），同一个 `source_id` 下的旧边指向的偏移就失效了——`UNIQUE` 里带着
`chunk_start`，所以那批旧边不会被覆盖，而是与新边并存。这是第 11 节的**第二个**
前置条件（版本一致性），本轮没有解决：要解决得给 `library_sources` 加版本列并在
重解析时让旧边失效，那是另一轮的事。

## 每块一次事务

落库走 `graph_store.add_edges_bulk`：一个块的所有边在**一个事务**里写完。
块级（而不是整份材料一次）是有意的：失败粒度与 `failed` 的语义保持一致，一块
坏掉只丢那一块。

没有出现在任何关系里的实体另走 `upsert_entity`——它们本来就没有边，跟着批量
写进去也没东西可写；数量通常在个位数，不值得为此多加一个批量接口。

## 失败熔断

每块一次事务带来一个副作用：**写库失败要按块付钱**。图库不可用时一次写入要付
连接超时 + 重试（默认各 3 秒），20 块就是两分钟，而调用方（上传接口）在那儿等着。
所以连续 `MAX_CONSECUTIVE_FAILURES` 块写库失败就停止，不再抽后面的块——那些块
的 LLM 调用本来就是白花的（抽出来也落不了库）。

三种失败要分开处理，因为它们的"可恢复性"完全不同：

  - `GraphStoreNotConfigured`（没配密码）：**第一块就停**。这是配置错误，不是偶发
    抖动，等三块只是多花两次 LLM 调用。
  - 其余 `GraphStoreError`：**累计**，连续 3 块才停。偶发的一次锁冲突不该让整份
    材料作废，而连续三次说明引擎确实不在了。
  - `ValueError`（区间倒置、空名这类数据问题）：**只算这一块**，既不累计也不清零。
    一块数据坏不代表引擎坏了，熔断它等于把"数据的错"当成"引擎的错"。

熔断后 `chunks` 小于总块数，`aborted` 为真——**不要**用 `failed` 变大来暗示中止，
那样调用方分不清"跑完了但有 3 块失败"与"停在第 3 块、后面 17 块没试过"。
成本闸（`max_chunks`）造成的截断不置 `aborted`：那是配置好的行为，不是故障。

## 统计口径

  - `chunks`：**尝试抽过的块数**（含失败的），不是成功的块数。熔断后它就是"停在
    第几块"。
  - `failed`：其中返回 `None`（降级）或写库时抛错的块数。`chunks - failed`
    就是真正贡献了东西的块数。
  - `aborted`：是否因为存储不可用而**提前停止**（还有块没试过）。成本闸截断不算。
  - `entities`：本次成功落库的去重实体数（按 `(name, type)`），包含没有边的孤立实体。
    落库失败的块不计入。
  - `edges`：**新写进库**的边数。重复的边不计入——`add_edges_bulk` 只统计被
    `UNIQUE` 放行的那些，重复不是失败，是"上次已经写过"。
"""
from __future__ import annotations

import logging
import time

from app.contracts.source import Source
from app.knowledge import graph_store, library
from app.knowledge.graph_extract import extract_graph
from app.knowledge.service import _chunk_spans

logger = logging.getLogger(__name__)

# 一次入库最多抽多少块。见模块 docstring 的「成本闸」。
DEFAULT_MAX_CHUNKS = 20

# 连续多少块写库失败就熔断。3 是"偶发一次不误伤、真挂了也不会等满 20 块"的折中：
# 引擎真不在时，3 块付的连接超时约 18 秒，20 块是两分钟。
MAX_CONSECUTIVE_FAILURES = 3


def _empty_stats() -> dict:
    """没有任何东西可做时的返回值。键必须齐——调用方不会为"空材料"走另一个分支。"""
    return {"chunks": 0, "entities": 0, "edges": 0, "failed": 0, "aborted": False,
            "elapsed_ms": 0}


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


async def ingest_graph(
    source: Source, *, max_chunks: int = DEFAULT_MAX_CHUNKS,
    source_id: str | None = None,
) -> dict:
    """把一份材料的正文抽成图并落库，返回统计。

    `source.text` 为空（解析失败、纯图片材料）时返回全 0 的统计：这不是错误，
    是"这份材料没有可抽的正文"，调用方不该为此走到异常分支。

    **边的 `source_id` 是持久 id**：先把材料写进知识库（`library.ingest`）拿到
    跨请求稳定的 id，再拿它当边的证据键。这一点是本轮改动的全部要害——原来存的是
    `Source.id`，而那是每次请求现生成的 `uuid4`，于是 `neighbors` 返回的
    `evidence` 在下一个请求里指向一个根本不存在的来源（`docs/RAG-DECISIONS.md`
    第 11 节的第一个前置条件）。`source_id` 参数保留给"调用方已经知道该用哪个 id"
    的场景（重跑入库、导入历史数据），显式给了就不再去库里查或写。

    入库失败（磁盘满、库被锁）时**直接抛**，不在这一层吞掉：没有持久 id 就没有
    这一轮要的语义，继续抽块只会写出一批跨请求对不上的边——那比整次入库失败更难
    发现，因为图看起来是建成了的。

    开关关着时每一次 `extract_graph` 都立刻返回 `None`，于是这里会得到
    `chunks == failed == 块数`。**这条路径也走切块**——不额外加一层"开关查询"，
    是因为多一处开关判断就多一处可能与 `graph_extract` 的判断分叉的地方。

    注意开关关着时**正文照样进库**（`library.ingest` 在循环之前）。这是刻意的：
    "把材料存下来"与"要不要花 LLM 的钱抽边"是两个能力，耦合起来会变成"关掉图
    就等于关掉知识库"。代价是开关全关的部署里 `library_sources` 也会长——但它有
    `prune` 与 90 天/200 条的默认阈值，而图那边一旦关了就是零成本，两者不对称
    是合理的。`max_chunks=0`（"这轮不花钱"）同理：正文进库，一块都不抽。
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

    persistent_id = source_id or library.ingest(source)

    spans = _chunk_spans(text)
    if len(spans) > max_chunks:
        logger.info(
            "图谱入库截断：材料 %d 块，只抽前 %d 块（成本闸）",
            len(spans), max_chunks,
        )
        spans = spans[:max_chunks]

    entities: set[tuple[str, str | None]] = set()
    consecutive_failures = 0
    for start, end in spans:
        chunk = text[start:end]
        extraction = await extract_graph(chunk, source_id=persistent_id, chunk_start=start)
        stats["chunks"] += 1
        if extraction is None:
            # 抽取层降级：这个块没碰存储，所以既不算写失败也不重置计数器——
            # 引擎有没有恢复，这一块回答不了。
            stats["failed"] += 1
            continue
        try:
            pairs = _unique_entities(extraction)
            kind_of = dict(pairs)
            written = graph_store.add_edges_bulk([
                graph_store.EdgeInput(
                    src=relation.src, dst=relation.dst, relation=relation.relation,
                    source_id=persistent_id, chunk_start=start, chunk_end=end,
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
                except graph_store.GraphStoreError as exc:
                    # 边已经落库，图的主体是完整的；缺一个孤立实体不影响任何路径，
                    # 所以只记警告，不把整块算作失败（那会让 `failed` 谎报"边也没了"）。
                    logger.warning(
                        "图谱孤立实体写入失败（材料=%s 位置=%d）：%s",
                        persistent_id[:8], start, type(exc).__name__,
                    )
        except graph_store.GraphStoreNotConfigured as exc:
            # 配置错误不是偶发抖动：第一块就停，省掉后面所有块的 LLM 调用。
            stats["failed"] += 1
            stats["aborted"] = True
            logger.warning(
                "图谱存储未配置，停止本次入库（材料=%s 已试 %d/%d 块）：%s",
                persistent_id[:8], stats["chunks"], len(spans), type(exc).__name__,
            )
            break
        except graph_store.GraphStoreError as exc:
            # 引擎级失败：之前块落的实体与边留着（它们本身是有效的），下一块继续；
            # 但连着失败 MAX_CONSECUTIVE_FAILURES 块就说明引擎不在了，继续抽块
            # 只是把 LLM 调用白花在落不了库的结果上。
            stats["failed"] += 1
            consecutive_failures += 1
            logger.warning(
                "图谱入库写入失败（材料=%s 位置=%d）：%s",
                persistent_id[:8], start, type(exc).__name__,
            )
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                stats["aborted"] = True
                logger.warning(
                    "图谱入库熔断：连续 %d 块写库失败，剩余 %d 块不再抽取（材料=%s）",
                    consecutive_failures, len(spans) - stats["chunks"],
                    persistent_id[:8],
                )
                break
        except ValueError as exc:
            # 数据问题只影响这一块：一块数据坏不代表引擎坏了，所以既不累计也不
            # 清零——把它算进熔断等于让一条坏数据终止整份材料。
            stats["failed"] += 1
            logger.warning(
                "图谱入库写入被拒绝（材料=%s 位置=%d）：%s",
                persistent_id[:8], start, type(exc).__name__,
            )
        else:
            consecutive_failures = 0

    stats["entities"] = len(entities)
    stats["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    logger.info(
        "图谱入库完成 材料=%s 块=%d 失败=%d 中止=%s 实体=%d 边=%d 耗时=%dms",
        persistent_id[:8], stats["chunks"], stats["failed"], stats["aborted"],
        stats["entities"], stats["edges"], stats["elapsed_ms"],
    )
    return stats
