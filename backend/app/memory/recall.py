"""双路检索：最近若干条观测（时间）+ 语义最相关的若干条命题（向量），按 RRF 合并。

Coco 论文的检索是**两路并用，不是二选一**，理由在数据形状上：观测没有向量（它们是
动作，不是句子），命题有向量但没有"刚刚发生"这个概念。任何一路单独用都会缺一半：

  - 只看时间：拿到的是"他刚才做了什么"，没有"他一贯怎么做"；
  - 只看语义：拿到的是"关于这个主题他一贯怎么判断"，没有"这一次上下文是什么"。

## 为什么必须 RRF，不能把两个分数加权

两路的分数**口径不同、量纲不同、而且都不可比**：

  - 时间路没有分。它是硬序（`at`），能给出的只有名次。
  - 语义路是余弦 × 持久性权重，落在 0.35–1.0 这个区间，而观测根本没有这个区间。

任何把"名次"和"余弦"塞进同一个线性组合的做法，都要先编一个换算系数——那个系数
没有任何依据，而且会随命题数量漂移（库越大，第 8 名的余弦越接近第 1 名）。

RRF 只吃名次，`1/(K + rank)`，天然对"两路各自有多少条、分数怎么分布"免疫。项目里
`app/knowledge/service.py` 已经用它融合 BM25 与向量两路（`RRF_K = 60`，那个数是
原论文的经验值），这里**沿用同一个常数**，理由写在 `RRF_K` 下面。

## 参数怎么定的

| 常数 | 值 | 依据 |
| --- | --- | --- |
| `RRF_K` | 60 | 与 `knowledge.service.RRF_K` 同值。它压住"名次 1 与名次 2 的差距"：K 越小，头部越独占；60 是原论文在异构检索路融合上的经验值，本仓库已经用它融合过 BM25/向量两路。**换它等于换一套排序偏好**，所以有一条用例钉住两个常量相等（`test_memory_recall.py`），防止两处漂移。 |
| `ROUTE_K` | 8 | 每路取 8 条进融合池。比最终条数（6）多一点：RRF 的空间来自"两路都提到"与"只被一路提到"的差别，池子等于最终条数时，第 7、8 名永远进不来，融合就退化成"固定取前 6"。 |
| `HYBRID_TOP` | 6 | 与 `store.TOP_K` 同值。理由同 `store.TOP_K` 的注释：6 条 × 一行摘要 ≈ 400 token，与 C 类备注同量级。 |

## 一个刻意的取舍：不做保底席位

`knowledge.service._fuse` 给文本路保底半数席位（"本次请求带来的来源"优先）。这里
**不照搬**：保底席位的根据是"用户这次明确带来的材料比库里的更该被引用"，而观测与
命题之间没有这种优先级——最近的观测不一定比一条稳定的判断更有用，反之亦然。让
两路纯按名次竞争，是唯一不需要额外假设的做法。

同分时**命题优先**：一条命题是跨事件的归纳，一条观测是一次动作。同样的融合分下，
前者信息密度更高。这条只在分数完全相等时生效（`1/(60+n)` 相等的组合很少），所以
它是个细节而不是一条隐含规则。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.memory import observations, propositions, store

#: RRF 的平滑常数。**与 `knowledge.service.RRF_K` 同值**，理由见模块 docstring 的
#: 参数表。不从那边 import 的原因：`knowledge.service` 会拉起 BM25、rerank、图检索
#: 一整条链，而这里只需要一个整数；用一条用例钉住两者相等，比一个跨包的 import
#: 更便宜，也更不容易在重构时被无意改掉。
RRF_K = 60

#: 每路进融合池的条数。
ROUTE_K = 8

#: 融合后返回几条。
HYBRID_TOP = store.TOP_K

ROUTE_TIME = "time"
ROUTE_SEMANTIC = "semantic"

#: 同分时的次序：命题优先（跨事件的归纳比一次动作信息密度高），其余按路上顺序。
_RANK_OF_KIND = {propositions.KIND: 0, observations.OBSERVATION_RUN: 1,
                 observations.OBSERVATION_EDIT: 2}


@dataclass(frozen=True)
class RouteHit:
    """融合后的一条命中。`routes` 记它被哪几路提到、各路名次是多少。

    **不带余弦、不带分数**：融合分是 `1/(60+名次)` 的累加，它不是"相关性"，把它当
    相关性读会得出"这条比那条相关 0.3 倍"这种没有意义的结论。要余弦就去看
    `propositions.search()` 的返回。

    `kind` 是**真实档位**：`run` / `edit`（观测）或 `proposition`。用它回答"这条是
    事实还是判断"，不要用"它在不在两路里"去猜。
    """

    kind: str
    id: str
    text: str
    score: float
    routes: tuple[tuple[str, int], ...] = ()

    @property
    def is_proposition(self) -> bool:
        return self.kind == propositions.KIND


@dataclass(frozen=True)
class Bundle:
    """一次双路检索的完整结果。两路各自的原始命中都留着，便于回答"为什么有它"。"""

    query: str
    hits: list[RouteHit]
    time_route: list[observations.Observation]
    semantic_route: list[propositions.PropHit]
    time_available: bool
    semantic_available: bool


def _rrf(rank: int) -> float:
    return 1.0 / (RRF_K + max(int(rank), 1))


def fuse(
    time_hits: list[observations.Observation],
    semantic_hits: list[propositions.PropHit],
    *, top: int = HYBRID_TOP,
) -> list[RouteHit]:
    """按名次 RRF 融合两路。纯函数，输入就是两路的原始命中。

    `score = Σ 1/(RRF_K + 名次)`。同一路里的名次从 1 开始（时间路按 `at DESC`，
    语义路按 `score DESC`）。

    两路都用 `(kind, id)` 作为身份。观测与命题的 id 都是 32 位 hex、来自不同的生成器，
    理论上可能撞——带上 kind 之后即使撞了也是两条，而不是互相覆盖。
    """
    scores: dict[tuple[str, str], float] = {}
    routes: dict[tuple[str, str], list[tuple[str, int]]] = {}
    texts: dict[tuple[str, str], str] = {}
    for rank, item in enumerate(time_hits, start=1):
        key = (item.kind, item.id)
        scores[key] = scores.get(key, 0.0) + _rrf(rank)
        routes.setdefault(key, []).append((ROUTE_TIME, rank))
        texts[key] = item.summary
    for rank, hit in enumerate(semantic_hits, start=1):
        key = (propositions.KIND, hit.proposal.item_id)
        scores[key] = scores.get(key, 0.0) + _rrf(rank)
        routes.setdefault(key, []).append((ROUTE_SEMANTIC, rank))
        texts[key] = hit.proposal.text

    ordered = sorted(
        scores.items(),
        key=lambda item: (
            -item[1],
            # 同分：命题优先（0 < 1），再按 id 稳定
            _RANK_OF_KIND.get(item[0][0], 9),
            item[0][1],
        ),
    )
    return [
        RouteHit(kind=key[0], id=key[1], text=texts[key],
                 score=round(score, 6), routes=tuple(routes.get(key, ())))
        for key, score in ordered[:max(1, top)]
    ]


def _from_observation(item: observations.Observation, score: float,
                      routes: tuple[tuple[str, int], ...]) -> RouteHit:
    return RouteHit(kind=item.kind, id=item.id, text=item.summary, score=score,
                    routes=routes)


def hybrid(query: str, *, route_k: int = ROUTE_K, top: int = HYBRID_TOP) -> Bundle:
    """一次"这个用户在这个场景下会怎么做"的检索。

    `query` 是这次的主题（生成时就是 `store.query_text(request)` 那一份）。空
    query 时**时间路照常工作**——"最近发生了什么"不依赖主题，这也是双路的价值之一：
    语义路挂了（没配 embedding、模型换了维度），时间路仍然给出可用的上下文。

    `semantic_available=False` 有三种原因，且它们在外部表现上一样（语义路空）：
    向量服务未配置、查询向量算不出来、库里没有带向量的命题。要知道是哪一个，看
    `semantic_route` 是否为空 + 日志（`store.query_vector` 会 warning）。
    """
    semantic_hits = propositions.search(query, top_k=route_k) if query.strip() else []
    time_hits = observations.recent(route_k)
    fused = fuse(time_hits, semantic_hits, top=top)
    by_time = {item.id: item for item in time_hits}
    hits: list[RouteHit] = []
    for hit in fused:
        if hit.is_proposition:
            hits.append(hit)
            continue
        source = by_time.get(hit.id)
        if source is None:
            continue
        hits.append(_from_observation(source, hit.score, hit.routes))
    return Bundle(
        query=query,
        hits=hits,
        time_route=time_hits,
        semantic_route=semantic_hits,
        time_available=bool(time_hits),
        semantic_available=bool(semantic_hits),
    )


def prompt_for(bundle: Bundle) -> str:
    """把一次双路检索的结果拼成一段可注入的文字；空 bundle 返回**空串**。

    空串而不是"（暂无记忆）"：与 `store.prompt_for` 同一条理由——空标题会让模型
    以为"这个人的记忆是空的"是一种需要它处理的输入，而真实语义是"这次没有相关的
    东西"。

    两段分开写，因为它们的**可信度不同**：命题是归纳出来的判断（可能过时、可能错），
    观测是事实（不可反驳）。合成一段会让模型分不清"他这么做过"与"我推断他会这么做"。
    """
    if not bundle.hits:
        return ""
    lines: list[str] = []
    judged = [hit for hit in bundle.hits if hit.is_proposition]
    if judged:
        lines.append("关于这个人的判断（系统归纳的，可能过时；与明确指令冲突时以后者为准）：")
        lines.extend(f"- {hit.text}" for hit in judged)
    seen = [hit for hit in bundle.hits if not hit.is_proposition]
    if seen:
        if lines:
            lines.append("")
        lines.append("他最近做过的动作（事实，按时间从新到旧）：")
        lines.extend(f"- {hit.text}" for hit in seen)
    return "\n".join(lines)


__all__ = [
    "HYBRID_TOP",
    "ROUTE_K",
    "ROUTE_SEMANTIC",
    "ROUTE_TIME",
    "RRF_K",
    "Bundle",
    "RouteHit",
    "fuse",
    "hybrid",
    "prompt_for",
]
