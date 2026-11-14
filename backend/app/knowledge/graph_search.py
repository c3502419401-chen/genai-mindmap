"""图游走接进检索：query → 图实体 → 两跳路径 → 块级证据 → 与文本路融合。

位置：`service.search_parent` 的两个出口，开关 `MINDMAP_GRAPH_SEARCH`（**默认 0**）。
抽取（`graph_extract`）、消解（`graph_resolve`）、存储（`graph_store`）都不动，这一层
只消费 `neighbors` 已经返回的 `GraphPath`。

    切块 ─→ 子块匹配 ─→ 折父块 ─┐
                                 ├─ fuse_hits ─→ top_k
    query ─→ 实体 ─→ 2 跳路径 ─→ 块 ┘

## 三条硬约束（照 `docs/RAG-DECISIONS.md` 第 11 节的方案）

1. **禁止拼接引文**：一条 `EvidenceHit` 只对应一个连续区间。路径 `甲—乙—丙` 有两条
   边、两个证据区间，那就产出**两条** hit——把三段原文拼成一段「引文」之后，`quote`
   不再是任何位置的原文切片，`verify_source_refs` 的逐字校验会从"证据核验"退化成
   "这段话在别处出现过吗"。
2. **禁止取区间包络**：路径覆盖 A 与 C 但没覆盖 B 时，A.start 到 C.end 这一整段里
   包含 B，而 B 与这条路径无关。包络会把"路径没碰过的段落"当成证据交上去，且它
   同样能通过逐字校验——错得看不出来。
3. **只接受合法区间**：`0 <= start < end <= len(text)`。落库时 `chunk_end < chunk_start`
   会被 `graph_store` 拒掉，但 `chunk_end` 只在 `ON CREATE` 时写入，人手改库或换了
   切块器之后仍可能读到越界值；切出来的 `quote` 为空也要丢掉（空白证据占席位）。

## 版本一致性：为什么这一层**不需要**额外校验

`docs/RAG-DECISIONS.md` 第 11 节把「版本一致性」列成前置条件，担心的是：材料重新
解析后文本变了，旧边还指着老偏移，那时 `neighbors` 会指向一段错的原文位置，而逐字
校验照样通过（区间里确实有字）。

**这个担心在当前的库里不成立**，因为边的证据键与正文之间是双射：

  - `library.ingest` **每次都从 `text` 重算** `sha256`（`knowledge/library.py`），
    `library_sources` 上落的是 `UNIQUE(content_hash)`；
  - 于是"同一 `source_id`"等价于"同一正文"——内容变 ⇒ 指纹变 ⇒ 新行 ⇒ **新
    `source_id`**；旧 `source_id` 那一行的正文一个字节都不会变。
  - 旧边指向旧 id：`library.load(旧 id)` 要么拿到**当初那段正文**（偏移仍然精确，
    与这条边代表的关系也仍然对应），要么在 `prune` 之后拿到 `None`（这条证据丢掉）。

所以这一层不做「拿旧偏移去切新正文」这件事——它切的是**那条边自己的来源**的正文。
校验"当前来源的 content_hash 是否一致"在这里没有可判定的对象：`source_id` 本身就是
内容指纹的代理（`_resolve` 里那次 hash 比对是 id 空间的**别名归一**，不是过期判定，
见下面「两个 id 空间」）。多一道 hash 闸门只会多一个分支和一条永远不会触发的
`GRAPH_STALE_EVIDENCE` 日志，所以没有加。

**仍然残留的一个洞（不在这一层，也不在本轮的边界内）**：同一份正文、换了切块算法时
`content_hash` 不变，边却按旧切点落在新切点之外。`graph_store` 的边 `MERGE` 键是
`(src, dst, relation, source_id, chunk_start)`，`chunk_end` 只 `ON CREATE`——同
`chunk_start` 撞上时旧 `chunk_end` 不会被更新，于是区间可能比实际块长/短。它不会指向
**另一份**材料（那是上面已经排除的情形），只是可能不是当前切法的那个块。要修得给边
加版本列，那是另一轮。

## 两个 id 空间

图上的边存的是 `library.ingest` 给的**持久 id**；检索时调用方手里的 `Source.id` 可能
是本次请求现生成的 `uuid4`。`_resolve` 按三条顺序把边上的 id 落到一个能切正文的来源：

  1. **入参里就有这个 id** → 直接用（`library=True` 那条路的常态，零额外读取）；
  2. 否则 `library.load(持久 id)` → 那一行的正文（**这条边当初切的就是它**）；
  3. 拿到的那份正文若与入参里某份的 `content_hash` 相同 → 把 hit 记在**入参那份的
     id** 上：正文字节相同，偏移天然可互换，而调用方只认识它自己那套 id（把持久 id
     交回去，它会去 `library.load` 一次才知道是哪份材料，或者干脆对不上）。

`library.load` 返回 `None`（被 `prune` 了）就丢掉该条证据——不猜、不回退到"最近那份"。

## 排名与融合

图路的排名是**最短跳数优先**，同跳数按 `(source_id, start, end)` 排：跳数越少，那条
证据与查询里的实体关系越直接；同跳数下按 id 排序只是为了确定性（同一个 query 跑三次
必须给出同一个顺序）。同一个块（同 `(source_id, start, end)`）被多条路径走到时**只投
一票**，取它出现过的最短跳数。

`fuse_hits` 用**名次 RRF**（`1/(RRF_K + 名次)`，与 `service._fuse` 同一套常数）而不是
分数：图命中没有词法分也没有向量分（`score_bm25` / `score_vector` 恒为 `None`），
两种量纲无法相加。**文本路前几名保底占半数席位**——与 `_merge_batches` / `search_multi`
同一条理由：图证据不该把文本证据挤掉，文本路才是这次检索的主证据。

文本路内部去重按**坐标粒度**分流（`fuse_hits(..., parent=)`）：块坐标上用重叠率
（同一份证据被切成两块时确实要去重），父块坐标上只用区间全等——父块之间的重叠
来自切块器的句首吸附，按重叠率筛会把两个**不同**父块判成重复、静默少给一条。
图块与已选条目之间无论哪种粒度都按重叠率筛：那是两条路对同一段原文各捞了一次。

**不用 `_fuse` 重筛图块**：`_fuse` 会用 BM25 把图块重新打分并可能与文本块一起截断，
而"桥接证据"（语义相关但用词不同）的词法分恰好是 0——那正是图的唯一价值所在。图块
进融合时只带名次，不带分数。

## 开关与降级

`MINDMAP_GRAPH_SEARCH` 默认 **0**。关着、`graph_store` 没配、图里没有实体命中、
路径为空——四种情况都**原样返回文本路的结果**（同一个列表对象，不是复制一份再改），
所以"开着但图里没东西"与"没开"在输出上逐字节相同。

### 为什么默认仍是 0（2026-11 本机实测，不是没量过）

四份 `docs/` 材料全量入库（130 块 / 1406 实体 / 993 边 / 267 条别名边，DeepSeek 抽取，
165k token），在 Neo4j 5.26 Community（256m 堆 / 128m pagecache）上跑两套对照，
配置固定 `hybrid=1 parent=1 library=1 rerank=0`，只切这一个开关：

| 指标 | crossdoc 14 条（28 对）graph=0 → 1 | manual 28 条 graph=0 → 1 |
|---|---|---|
| recall@1/3/5/10 | 25.0 / 42.9 / 50.0 / 57.1 % —— **逐位相同** | 53.6 / 78.6 / 85.7 / 89.3 % —— **逐位相同** |
| mrr@3 | 32.7 % —— 相同 | 65.5 % —— 相同 |
| coverage@10 | 33.1 % → 32.6 %（**−0.5pp**） | 54.6 % —— 相同 |
| 跨文档 bridge@10 | 57.1 % —— 相同 | — |
| 逐条延迟（平均） | +86ms（BM25，1.35×）/ +85ms（hybrid，1.21×） | — |

**没有一处在正向**，跨文档套上 coverage 还掉了一点。原因在测出来的分布里，不在融合
逻辑：14 条跨文档问法里只有 8 条命中了图里的实体，命中的是「浏览器」「候选」「agent」
「向量检索」这类**泛称**（`GENERIC_TERMS` 挡不住它们，因为它们确实是材料里的节点），
走出两跳邻域后一共 58 条图命中，**落在标注目标区间上的是 0 条**（`bridge` 与
`full_bridge` 逐位相同就是这个意思）。融合按名次 RRF 给它们让出席位，挤掉的正是文本路
的第 6–10 名，coverage 因此小幅下降。

同一张图上 `search_graph` 自己是有产出的（8 条问法共 58 条命中、单次 40–250ms），
所以这不是"图没建起来"，而是**实体命中精度不足以把"跨文档"这件事做对**：图能把两份
材料连起来（`tests/test_graph_neo4j_live.py` 钉着"至少一条问法的命中跨两份材料"），但连
出来的那条路不是答题需要的那一段。要它转正，先得让 query→实体的匹配从"泛称子序列"
变成"专名/术语"，那是另一轮的事。

复现：`python scripts/evaluate_retrieval.py --suite crossdoc --parent 1 --hybrid 1
--library 1 --rerank 0 --graph 0|1`（`MINDMAP_EVAL_DB_PATH` 指向建图用的那个库，否则边上的
`source_id` 解不出正文，两遍会逐位相同——这是这套对照最容易踩的坑）。


Neo4j 抛错时降级成纯文本检索并记 warning，**不让整次检索失败**：`graph_store` 的读
接口本来就把"查不到"和"库坏了"折成同一个空值（见 `graph_store` 模块 docstring 的
「写与读的降级不对称」），这一层再兜一次是为了接住自己的异常（`library.load` 的
`sqlite3.Error`、区间校验的 `ValueError`）。

**降级还要够快**：库不可达时一次 `entity_degrees` 实测 2.04 秒、一次 `neighbors`
2.04 秒，而一次图检索是 1 + 最多 4 条——十秒级的延迟已经超出"降级"的语义。所以
`search_graph` 进来先问 `graph_store.available()`（熔断状态，不连库），失败过一次
之后按指数退避跳过（30s → 60s → … 上限 300s），一次成功立刻清零。

## 已知边界

  - **只从 top-N 实体出发**（`MAX_ENTITIES`）：`neighbors` 的调用数是实体数，一次
    检索打四条变长路径查询是上限。查询里提到第五个以后的实体时，那条路这一轮不走。
  - **query 侧的简称不做**：query 写「发改委」而图里是「国家发展和改革委员会」时，
    子序列判定两向都不成立（名字比 query 长），所以命不中。要做得枚举 query 的子串
    去比 `_abbreviation_ratio`，复杂度 O(len(query)² × 实体数)，而且 `graph_resolve`
    的实测记录里抽字缩写（央行、双碳）本来就没合上——先不在这里重复那个缺口。
  - **`entity_degrees` 是 top-N 按度数**：一个只出现过一次的实体（度 1）在实体上千
    的图上可能进不了候选表。这与 `graph_resolve` 的候选源同一个取舍，理由也相同。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Literal

from app.contracts.source import Source
from app.knowledge import graph_store, library
from app.knowledge.graph_resolve import (
    GENERIC_TERMS,
    _is_ordered_subsequence,
    _longest_common_run,
)
from app.knowledge.service import (
    CANDIDATE_FACTOR,
    RRF_K,
    EvidenceHit,
    SourceFilter,
    _dedupe_exact,
    _near_duplicate,
    _pick_ordered,
    filter_sources,
)

logger = logging.getLogger(__name__)

# 游走深度。2 跳是"跨文档长程关联"与"噪声"的分界：1 跳只是共现实体（文本检索
# 也找得到），3 跳开始大量经过泛称节点。`graph_store.MAX_HOPS` 是硬上限 5。
HOPS = 2

# 从库里取多少个实体名参与"query 里提到了谁"。与 `graph_resolve.DEFAULT_LIMIT`
# 同一个数、同一个取舍：按边数降序取前 N 个，边少的合并/匹配价值低。
ENTITY_CANDIDATES = 500

# 一次检索最多从几个实体出发游走。每个实体一条变长路径查询，4 条是
# "覆盖 query 里提到的全部主要实体"与"不让一次请求多出四条 Cypher"之间的折中。
MAX_ENTITIES = 4

# 单个实体的路径上限。检索一次只拿几十条证据，`graph_store.MAX_LIMIT` 是硬上限 200。
PATH_LIMIT = 50

# 实体名最短字数。与 `graph_extract.MIN_NAME_CHARS` 同值：抽取层丢掉的东西，
# 检索层不该又把它当实体（那种名字一个 bigram 都凑不出，匹配上纯属巧合）。
MIN_ENTITY_CHARS = 2

# 图命中的 `origin` 取值。字段在 `service.EvidenceHit` 上，这里是唯一的写入方。
ORIGIN_GRAPH: Literal["graph"] = "graph"


def _rrf(rank: int) -> float:
    """名次 → RRF 分。常数取 `service.RRF_K`，与文本路那套逐字相同。"""
    return 1.0 / (RRF_K + max(int(rank), 1))


def _mentions(name: str, query: str) -> bool:
    """query 里提没提到这个实体名。

    复用 `graph_resolve` 的两个现成判定，**不新写匹配逻辑**：

      - `_is_ordered_subsequence(name, query)`：名字按顺序出现在 query 里（可以跳字，
        不可乱序）。子序列而不是子串，是因为 query 是自然语言，实体名可能被"的职责"
        之类的字断开。
      - `2 * _longest_common_run(name, query) > len(name)`：名字里**超过一半**的字是
        **连着**的一整块。这一条挡"子序列巧合"——`谁准` 是 `谁来批准预算` 的子序列，
        但它不是一个实体，只是两个高频字碰巧按序出现。

        **这里比 `graph_resolve._abbreviation_ratio` 的第三闸（`2 * run >= len`）严
        一格**，因为两处判的是不同的东西：那里比两个**名字**（真简称总有一大块字连着，
        `董秘` 的「董」是一半，够用）；这里比"名字 vs 一句话"，而两个字的短名在
        `>=` 下只需要一个连续字就能过关——那正好是巧合最密集的一档。实测：`谁准`
        在 `谁来批准预算` 上 `run == 1`，`>=` 放行、`>` 拒绝，而 `财务部` 的 `run == 3`
        两处都过。

    两道额外的前置闸：太短的名字（1 个字）与 `GENERIC_TERMS` 里的泛称（预算、流程、
    风险、数据…）。泛称在库里可能是个真节点（抽取层抽到过），但从它出发的两跳路径
    几乎必然连接两个无关的东西——**图的路由噪声正是从泛称节点长出来的**。

    **不用 `_abbreviation_ratio` 整体**：它的第二闸是 `len(短)/len(长) >= 0.4`，那是
    "两个名字谁是谁的缩写"的度量。拿 query 当长名时这条恒不成立（一个问题通常比
    实体名长得多），会把正常命中的实体全部挡掉。
    """
    if len(name) < MIN_ENTITY_CHARS:
        return False
    if name in GENERIC_TERMS:
        return False
    if not _is_ordered_subsequence(name, query):
        return False
    return 2 * _longest_common_run(name, query) > len(name)


def entities_in_query(query: str) -> list[str]:
    """query 里提到的、图里确实存在的实体名（按度数降序，最多 `MAX_ENTITIES` 个）。

    候选来自 `graph_store.entity_degrees`：`ORDER BY degree DESC, name` 是 Cypher 里
    定死的顺序，所以同一张图、同一个 query 拿到的实体列表逐次相同——确定性从这一行
    开始，一直传到 `_evidence_blocks` 的排序键。

    别名自然包含在内：`entity_degrees` 按名字聚合、不区分主名与别名，而
    `graph_store.neighbors` 起点会换成整个别名组（`_alias_group`），所以从别名问、
    从主名问拿到的是同一批路径。
    """
    question = (query or "").strip()
    if not question:
        return []
    names: list[str] = []
    for name, _degree in graph_store.entity_degrees(limit=ENTITY_CANDIDATES):
        if _mentions(name, question):
            names.append(name)
            if len(names) >= MAX_ENTITIES:
                break
    return names


def _evidence_blocks(paths) -> list[tuple[str, int, int]]:
    """把多条路径展开成去重的块列表，**最短跳数优先**，同跳数按 `(id, start, end)`。

    展开的粒度是**边**，不是路径：一条两跳路径贡献两个块，各自独立成一条命中
    （禁止拼接、禁止包络，见模块 docstring）。

    同一个块被多条路径走到时只保留**最短**的那次（`hops` 越小，它离 query 里的实体
    越近）；这既是"同一个块只投一票"，也让排名不受路径枚举顺序影响。
    """
    best: dict[tuple[str, int, int], int] = {}
    for path in paths:
        hops = path.hops
        for triple in path.evidence:
            try:
                key = (str(triple[0]), int(triple[1]), int(triple[2]))
            except (TypeError, ValueError, IndexError):
                # `GraphPath.evidence` 的形状由 `graph_store._parse_path` 保证，这里是
                # 防御：手工构造的路径（测试或将来的旁路）不该让整次检索抛。
                logger.warning("图证据三元组格式异常，跳过一条")
                continue
            known = best.get(key)
            if known is None or hops < known:
                best[key] = hops
    ordered = sorted(best.items(),
                     key=lambda item: (item[1], item[0][0], item[0][1], item[0][2]))
    return [key for key, _hops in ordered]


class _Resolver:
    """把边上的 `source_id` 落到一个能切正文的 `Source`（见模块 docstring 的两个 id 空间）。

    逐个 `library.load` 的开销要摊掉：一次检索里同一条路径的两条边常常指向同一份
    材料，而 `load` 读的是整份正文。所以按 id 缓存一次调用内的结果。
    """

    def __init__(self, sources: list[Source], filters: SourceFilter | None) -> None:
        self._by_id = {source.id: source for source in sources}
        # 正文字节相同 ⇒ 偏移可互换。哈希只用来**归一 id**，不用来判过期
        # （见模块 docstring 的「版本一致性」）。
        self._by_hash: dict[str, Source] = {}
        for source in sources:
            digest = library.content_hash(source.text or "")
            self._by_hash.setdefault(digest, source)
        self._filters = filters
        self._loaded: dict[str, Source | None] = {}

    def resolve(self, source_id: str) -> Source | None:
        if source_id in self._by_id:
            return self._by_id[source_id]
        if source_id in self._loaded:
            return self._loaded[source_id]
        source = self._load(source_id)
        self._loaded[source_id] = source
        return source

    def _load(self, source_id: str) -> Source | None:
        try:
            stored = library.load(source_id)
        except (sqlite3.Error, OSError) as exc:
            # 库不可用是一种"这批证据拿不到"，不是"整次检索失败"。
            logger.warning("图证据的来源读取失败：%s", type(exc).__name__)
            return None
        if stored is None:
            # 被 `prune` 掉了（或这个 id 从来不在库里）。丢掉该条证据，不回退。
            logger.debug("图证据的来源已不在库里，丢弃")
            return None
        if self._filters is not None and not filter_sources([stored], self._filters):
            # 过滤条件作用在**元数据**上，从库里捞出来的来源也要过这一关：图不是
            # 绕过 `filters` 的后门。
            logger.debug("图证据的来源被过滤条件排除")
            return None
        digest = library.content_hash(stored.text or "")
        return self._by_hash.get(digest, stored)


def _hit_of(source: Source, start: int, end: int, *, rank: int) -> EvidenceHit | None:
    """一条块 → 一条 `EvidenceHit`。区间不合法或切出空白就返回 `None`。

    `quote` 严格取 `text[start:end]`（码点切片，与 `EvidenceHit` 的 `start`/`end` 同
    口径）：这是整条图路唯一被允许产出引文的地方，也是"逐字校验能成立"的原因。
    """
    text = source.text or ""
    if not (0 <= start < end <= len(text)):
        logger.debug("图证据区间越界，丢弃：%d–%d / %d 字", start, end, len(text))
        return None
    quote = text[start:end]
    if not quote.strip():
        logger.debug("图证据切出空白，丢弃：%d–%d", start, end)
        return None
    return EvidenceHit(
        source_id=source.id,
        start=start,
        end=end,
        quote=quote,
        score=round(_rrf(rank), 6),
        # 图命中没有词法分也没有向量分：`None` 是"这条根本没走那一路"，
        # 而 0.0 会被读成"走了但得了 0 分"。
        score_bm25=None,
        score_vector=None,
        # `method` 的语义是"哪条路支撑"，这里是纯图路——沿用兼容值 `lexical`，
        # 见 `docs/RAG-DECISIONS.md` 第 11 节的代价第 4 条（`method` 表达不了图）。
        # "这条来自图"由 `origin` 表达，不污染 `method`。
        method="lexical",
        origin=ORIGIN_GRAPH,
    )


def search_graph(
    sources: list[Source], query: str, top_k: int, *,
    filters: SourceFilter | None = None,
) -> list[EvidenceHit]:
    """query 里的实体在图上的两跳邻居，展开成**块级**证据，按最短跳数排序。

    返回最多 `top_k` 条，每条一个连续区间；`score` 是它在**图路内**的 RRF 分
    （`1/(60+名次)`），`score_bm25` / `score_vector` 恒为 `None`，`origin="graph"`。

    `filters` 在**游走之前**作用于入参来源（与 `search_parent` 同一条约定：被筛掉的
    来源不参与任何计算），并且在从库里捞证据来源时**再过一遍**——图不是绕过过滤的
    后门。

    任何一步拿不到东西都返回 `[]`（图没配、没实体命中、没有路径、全部区间不合法），
    与"这两份材料确实无关"同形：调用方看到空就退回纯文本检索，不需要第二个分支。
    """
    if top_k <= 0:
        return []
    question = (query or "").strip()
    if not question:
        return []
    if not graph_store.available():
        # 刚连失败过一次（见 `graph_store.available` 的「可用性熔断」）。这里必须提前
        # 返回：实测库不可达时 `entity_degrees` 2.04 秒、`neighbors` 2.04 秒，一次图
        # 检索（1 + 最多 4 条）就是十秒——那不算降级，算让检索不可用。
        logger.debug("图检索跳过：图存储刚失败过，处于冷却期")
        return []

    pool = list(sources)
    if filters is not None:
        pool = filter_sources(pool, filters)

    names = entities_in_query(question)
    if not names:
        logger.debug("图检索：query 没有命中任何图实体")
        return []

    paths = []
    for name in names:
        paths.extend(graph_store.neighbors(name, hops=HOPS, limit=PATH_LIMIT))
    if not paths:
        return []
    logger.debug("图检索：实体 %d 个 / 路径 %d 条", len(names), len(paths))

    resolver = _Resolver(pool, filters)
    hits: list[EvidenceHit] = []
    for source_id, start, end in _evidence_blocks(paths):
        source = resolver.resolve(source_id)
        if source is None:
            continue
        hit = _hit_of(source, start, end, rank=len(hits) + 1)
        if hit is None:
            continue
        hits.append(hit)
        if len(hits) >= top_k:
            break
    return hits


def _dedupe(hits: list[EvidenceHit]) -> list[EvidenceHit]:
    """按 `(source_id, start, end)` 去重，保留第一次出现（= 名次最高的那次）。

    "同一个块只投一票"的最后一处闸：`search_graph` 已经按三元组去过重，但调用方
    （测试、将来的旁路）可能直接递一批带重复的命中进来。
    """
    seen: set[tuple[str, int, int]] = set()
    kept: list[EvidenceHit] = []
    for hit in hits:
        key = (hit.source_id, hit.start, hit.end)
        if key in seen:
            continue
        seen.add(key)
        kept.append(hit)
    return kept


def fuse_hits(
    text_hits: list[EvidenceHit], graph_hits: list[EvidenceHit], top_k: int, *,
    parent: bool = False,
) -> list[EvidenceHit]:
    """把文本路与图路的命中按**名次 RRF** 融合，文本路前几名**保底占半数席位**。

    结构与 `service._merge_batches`（本次请求的那批 vs 库那批）逐条同构，理由也相同：

      - `reserve = ceil(top_k/2)` 条文本命中先占位。没有这一层时，图路的四条桥接
        证据各贡献一个 `1/61`，会把文本路的第 1 名顶到第 5 名——而文本路才是这次
        检索的主证据，图只在"文本找不到"的地方补。
      - 其余名额由两路按 RRF 分竞争；同分时短块优先，再按 `(source_id, start)` 定序
        ——**排序必须是全序**，否则同一批输入在不同进程里可能给出不同的顺序。
      - 返回的 `score` 一律是 RRF 分：两条路的原始分不可比（文本路是 BM25/余弦，
        随本次切出的块集合变；图路根本没有分数）。`score_bm25` / `score_vector`
        保留各自原值，它们回答的是"这条在它自己那路里怎么被打出来的"。

    `graph_hits` **为空时原样返回** `text_hits[:top_k]`：开关关着、图不可用、图里没
    实体命中，三种情况在输出上必须与"没接图"逐字段相同——这是接线本身唯一的回归
    契约，由 `tests/test_graph_search.py` 钉着。

    `parent` 决定**文本路内部的去重判据**，由调用方按
    `MINDMAP_PARENT_RETRIEVAL` 给出：

      - `parent=False`（块坐标）：沿用 `_pick_ordered` 的重叠率筛。同一份证据被切成
        两块时，那里确实要去重。
      - `parent=True`（父块坐标）：文本路内部按 `(source_id, start, end)` 全等去重。
        父块之间的重叠来自切块器的句首吸附（`_overlap_start`），不是重复证据；按
        重叠率筛会把两个**不同**父块判成重复，静默少给一条。**图块与已选条目之间
        仍按重叠率筛**——那是两条路对同一段原文各捞了一次，重叠就是重复。

    ⚠ `score` 是名次倒数而排序是"保底段 + 竞争段"，所以**分数不单调**：保底段最后
    一条的分可能低于竞争段第一条。与 `_merge_batches` 同一条告警——`score` 只用于
    同一次检索内比较大小，不要拿它反推名次。

    不跑 `_fuse` 重筛（见模块 docstring）：图块进融合只带名次。这里也不做词法/向量
    打分的任何调用。
    """
    if top_k <= 0:
        return []
    graph = _dedupe(graph_hits)
    if not graph:
        return list(text_hits[:top_k])
    if not text_hits:
        return [
            hit.model_copy(update={"score": round(_rrf(rank), 6)})
            for rank, hit in enumerate(graph[:top_k], 1)
        ]

    reserve = min(max(1, (top_k + 1) // 2), len(text_hits))
    kept = list(text_hits[:reserve])

    # 第三项标出"这条来自图路"：两路的去重判据在父块坐标上不同（见 docstring）。
    contenders: list[tuple[float, EvidenceHit, bool]] = []
    for rank, hit in enumerate(text_hits[reserve:], reserve + 1):
        contenders.append((_rrf(rank), hit, False))
    for rank, hit in enumerate(graph, 1):
        contenders.append((_rrf(rank), hit, True))
    contenders.sort(key=lambda item: (-item[0], item[1].end - item[1].start,
                                      item[1].source_id, item[1].start))

    fused: list[EvidenceHit] = [
        hit.model_copy(update={"score": round(_rrf(rank), 6)})
        for rank, hit in enumerate(kept, 1)
    ]
    if not parent:
        for score, hit, _from_graph in contenders:
            fused.append(hit.model_copy(update={"score": round(score, 6)}))
        # `_pick_ordered` 不排序（顺序就是保底段在前），只做近重复筛与截断。
        return _pick_ordered(fused, top_k)

    picked = _dedupe_exact(fused, top_k)
    seen = {(hit.source_id, hit.start, hit.end) for hit in picked}
    for score, hit, from_graph in contenders:
        if from_graph:
            # 跨路重叠是同一份证据被两条路各捞了一次 → 仍然按重叠率筛。
            if any(_near_duplicate(hit, chosen) for chosen in picked):
                continue
        else:
            key = (hit.source_id, hit.start, hit.end)
            if key in seen:
                continue
            seen.add(key)
        picked.append(hit.model_copy(update={"score": round(score, 6)}))
        if len(picked) >= top_k:
            break
    return picked


# 扩池倍数从 `service` 借常量：图路的候选池与文本路同一个放大规则，两处各写一份
# 迟早会有一处"图取 2 倍、文本取 3 倍"的不一致，而那种不一致没有信号。
POOL_FACTOR = CANDIDATE_FACTOR


def graph_pool(top_k: int) -> int:
    """`search_graph` 该取多少条候选（给融合留竞争者）。与文本路的池子同口径。"""
    return max(int(top_k) * POOL_FACTOR, int(top_k))
