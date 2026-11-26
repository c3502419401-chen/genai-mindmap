"""知识增强路由。

`/search` 是**库内检索**（候选集是用户知识库，不是本次请求的材料）：返回片段 +
它在原文里的码点区间 + 持久 `source_id`，调用方可据此回查全文。`/library` 两个
GET 是知识库的**只读入口**：列出库里有哪些材料、每份被几张已保存导图引用、点开看
正文预览。`/fetch` 是按 URL 读一篇网页（DeepSeek 的 Fetch 工具 API），读到的正文
原样变成一份 `Source`，与"粘贴一段正文"走完全同一条路。

两条职责不同，别把它们的功能互相当前提：`/library` 不受
`MINDMAP_LIBRARY_SEARCH` 影响——那个开关管的是"生成时要不要从库里捞材料参与"，
关掉它只应该让生成变回不查库，不该让用户看不见自己库里的东西。

`/fetch` 同样**不受** `MINDMAP_EXTERNAL_KNOWLEDGE` 影响，但受它自己的开关
（`MINDMAP_EXTERNAL_DEEPSEEK_FETCH`）约束：前者管"生成时自动去搜一轮"，这里是一次
用户显式动作（贴了一个链接），两种意愿不该共用一个闸。
"""
from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from app.contracts.source import (
    CreateTextSourceResponse,
    FetchUrlRequest,
    Source,
    SourceKind,
    SourceMetadata,
)
from app.core.errors import AppError
from app.knowledge import external, graph_store, shelf
from app.knowledge.service import SearchRequest, SearchResponse, search

router = APIRouter(prefix="/knowledge", tags=["知识增强"])


@router.post("/search", response_model=SearchResponse)
def search_knowledge(body: SearchRequest):
    """知识库检索（F12/F13）：在**用户自己的知识库**里找相关片段。

    返回的 `source_id` 是库里的**持久 id**，可以拿它去
    `GET /api/knowledge/library/{source_id}` 取全文（`start` / `end` 是片段在该份
    原文里的码点偏移，用来看"为什么是这段"）。

    空结果有两种原因，界面必须分开说：`hits` 为空而库里有材料 → "没检索到"；
    库本身是空的 → "先去用一份材料生成一张图"（入库发生在生成时，见
    `knowledge/remember.py`）。接口不能替调用方猜，所以 `implemented` 只说
    "这条路接上了没有"，恒为 true。
    """
    hits = search(body.query, body.top_k)
    return SearchResponse(hits=hits, implemented=True)


@router.get("/library", response_model=shelf.LibraryList)
def list_library(limit: int = Query(shelf.DEFAULT_LIMIT, ge=1, le=200)):
    """库里的材料列表，最近使用的在前。`total` 是库里的总数，不是返回条数。

    只读：不改任何状态，也不刷 `last_used_at`（"看了一下列表"不等于"用到了
    这份材料"——那个字段是 `prune` 保留与否的依据）。

    `limit` 有上下界：`ge=1` 让 `limit=0/-5` 直接 422 而不是被悄悄吞成一条，
    `le=200` 防止一次请求把整库都拉出来（库上限就是 200 份，再大没有意义）。
    """
    return shelf.list_entries(limit=limit)


@router.get(
    "/library/{source_id}",
    response_model=shelf.LibrarySource,
    responses={
        404: {"description": "库里没有这份材料（`code=not_found`）"},
        503: {"description": "知识库暂时读不出来（`code=library_unavailable`）"},
    },
)
def read_library_source(
    source_id: str,
    limit: int = Query(shelf.PREVIEW_CHARS, ge=1, le=shelf.MAX_READ_CHARS),
):
    """单份材料的正文，默认前 4000 字（`truncated` 说明是否截断）。

    `limit` 给"点引用跳原文"用：`SourceRef.start/end` 是全文码点偏移，界面要
    定位到第 N 字就必须能拿到那之前的正文。传 `total_chars`（列表里就有）即可
    拿到整份；上限 `MAX_READ_CHARS` 由 schema 的 `le` 直接挡成 422，而不是在
    服务层悄悄夹一下——一个越界的 `limit` 是调用方的错，静默纠正会让它以为
    自己拿到的是全文。
    """
    # 这里用 AppError 而不是 HTTPException：后者会被全局处理器兜成"请求处理失败
    # （HTTP 404）"，前端读顶层 message，用户看到的就是那句没信息量的话。
    #
    # 404 与 503 必须分开：前者是"这份材料没了，重试也没用"，后者是"库暂时读不
    # 出来，稍后再看"。把库故障说成 404，用户会以为材料被删了。
    try:
        return shelf.read_entry(source_id, limit=limit)
    except shelf.SourceNotFound as error:
        raise AppError("not_found", "这份来源不在知识库里", status=404) from error
    except shelf.LibraryUnavailable as error:
        raise AppError(
            "library_unavailable", "知识库暂时读不出来，请稍后重试", status=503,
        ) from error


def _fetched_source(hit: external.ExternalHit) -> Source:
    """一条抓回来的网页 → `Source`（**材料**，不是外部检索的命中）。

    与 `agent.tools._external_source` 长得像，但刻意不复用：那个函数在 agent 层，
    还要按 `_EXTERNAL_KINDS` 判 `web`/`paper`——而路由层 import agent 会把
    依赖方向倒过来（agent 依赖 knowledge，不是反过来）。这里只有一条路径：用户贴了
    一个链接，它是网页，`kind` 就是 `web`。

    字段与那条路对齐（`filename` 带来源前缀、`status="parsed"`、`raw_chars` 是截断前
    长度、`tags` 第一项是抓取层名）——**对齐是必须的**：前端拿到 `Source` 之后把它
    当材料用，两条路的形状不同会让材料清单里出现两种显示规则。
    """
    tags = [hit.source_name]
    if hit.site:
        tags.append(hit.site)
    return Source(
        kind=SourceKind.web,
        # `: ` 之后是网页标题；标题常为空（部分站点不给 `<title>`），那时退回主机名
        # ——`deepseek: ` 这种尾巴在材料清单里等于没有信息。
        filename=f"{hit.source_name}: {hit.title}"[:200],
        text=hit.text,
        status="parsed",
        metadata=SourceMetadata(
            url=hit.url, raw_chars=max(hit.raw_chars, len(hit.text)), tags=tags,
        ),
    )


@router.post(
    "/fetch",
    response_model=CreateTextSourceResponse,
    responses={
        409: {"description": "这个功能没打开（`code=fetch_disabled`）"},
        502: {"description": "抓取失败：地址打不开、正文为空（`code=fetch_failed`）"},
    },
)
def fetch_url_source(body: FetchUrlRequest):
    """把一个 URL 读成一份材料。**用户在粘贴框里贴链接**走的就是这里。

    四种结果分开说，因为用户的下一步各不相同：

      - 开关关着（409 `fetch_disabled`）：去把它打开，重试同一个链接；说成"抓取
        失败"会让他反复试一个永远不会成功的东西；
      - 没配置 key（503 `fetch_unconfigured`）：这是部署问题，用户改不了；
      - 抓取失败 / 正文为空（502 `fetch_failed`）：换一个链接，或者直接把正文贴进来；
      - 成功：返回 `Source`，前端把它当材料（与"粘贴长正文"完全同一条路）。

    **失败不抛裸异常**：`_deepseek_fetch` 自己吞掉一切并返回 None，所以这里只判
    返回值。外部抓取是这个接口唯一的目的，拿不到就是失败，没有"部分成功"可报。
    """
    if not external.deepseek_fetch_enabled():
        raise AppError(
            "fetch_disabled",
            "按链接抓取没有打开（需要设置 MINDMAP_EXTERNAL_DEEPSEEK_FETCH=1），"
            "可以改成直接粘贴正文。",
            status=409,
        )
    if not external.deepseek_configured():
        raise AppError(
            "fetch_unconfigured",
            "按链接抓取还没有配置 key（MINDMAP_DEEPSEEK_TOOLS_API_KEY 或 "
            "MINDMAP_LLM_API_KEY）。这一项要在部署时配好，可以先用粘贴正文。",
            status=503,
        )
    hit = external.fetch_url(body.url)
    if hit is None:
        raise AppError(
            "fetch_failed",
            "这个链接读不出正文：地址打不开，或者页面里没有可用的文字。"
            "可以换成直接粘贴正文。",
            status=502,
        )
    return CreateTextSourceResponse(source=_fetched_source(hit))


@router.delete(
    "/library/{source_id}",
    response_model=shelf.LibraryDeletion,
    responses={
        404: {"description": "库里没有这份材料（`code=not_found`）"},
        409: {
            "description": "这份材料还被导图引用（`code=source_cited`，`detail.cited_by` "
                           "是被几张图引用）——加 `?force=true` 才真删",
        },
        503: {"description": "知识库暂时读不出来（`code=library_unavailable`）"},
    },
)
def delete_library_source(
    source_id: str,
    force: bool = Query(
        False,
        description="已经看到引用后果，仍然删掉（那几张图里的引用会变成"
                    "「来源原文不可用」）",
    ),
):
    """删掉库里的一份材料。**这是"传错了材料"唯一的出口**。

    三件事分开说，因为用户的下一步不同：

      - 没这份材料（404）：它已经被删了，刷新列表即可；
      - 还被 N 张导图引用（409，`detail.cited_by=N`）：先看这句话再决定。删掉之后
        那几张图**不会坏**，但引用那些材料的节点打开时是"来源原文不可用"——
        `source_refs` 是值对象，没有外键，删材料不会把它们清掉，也没法再回填原文。
        确认要删就带 `force=true` 再来一次；
      - 删掉了（200）：`cited_by` 说的是删除前的引用数，界面用它可以如实告诉用户
        "有 N 张图受影响"。

    为什么不静默硬删：材料正文是导图证据的最终依据，而这个删除不可逆。为什么不
    "软删"（留墓碑 + 清正文）：那是拿一次 schema 迁移和一遍读路径过滤，换一句更好
    的诊断措辞——`SOURCE_TEXT_UNAVAILABLE` 已经把"删了但引用还在"表达清楚了。
    """
    try:
        return shelf.delete_entry(source_id, force=force)
    except shelf.SourceNotFound as error:
        raise AppError("not_found", "这份来源不在知识库里", status=404) from error
    except shelf.SourceCited as error:
        raise AppError(
            "source_cited",
            f"这份材料还被 {error.cited_by} 张导图引用：删掉之后那些节点会显示"
            "「来源原文不可用」，且无法恢复。确实要删就带 force=true 再试一次。",
            status=409,
            detail={"cited_by": error.cited_by},
        ) from error
    except shelf.LibraryUnavailable as error:
        raise AppError(
            "library_unavailable", "知识库暂时读不出来，请稍后重试", status=503,
        ) from error


class AliasEntry(BaseModel):
    """一条已落库的实体别名：`alias` 是 `canonical` 的另一个叫法。"""

    model_config = ConfigDict(extra="forbid")
    alias: str
    canonical: str
    #: 落库时的判定依据：`substring`（构词关系）/ `embedding`（余弦兜底）。
    source: str
    similarity: float
    created_at: str


class AliasListing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[AliasEntry] = Field(default_factory=list)
    total: int = Field(ge=0)
    #: 图谱这一层开没开。**必须单独给**：没配 Neo4j 时列表也是空的，界面若只看
    #: `total == 0` 会把"图存储没启用"说成"没有别名"，而那是两条完全不同的建议。
    configured: bool


class AliasDeletion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str
    deleted: int = Field(ge=0)


class AliasClearance(BaseModel):
    """清空别名的结果。与 `AliasDeletion` 分开：清空没有"哪一条"这个字段。"""

    model_config = ConfigDict(extra="forbid")
    deleted: int = Field(ge=0)


@router.get("/aliases", response_model=AliasListing)
def list_aliases(limit: int = Query(500, ge=1, le=2000)):
    """库里的实体别名（`alias → canonical`）。**"模型把两个名字认成一个"唯一看得见的地方**。

    别名是模型推断出来的：实体消解按构词关系与向量相似度猜"这两个名字指同一个
    东西"，猜完直接写进图库。猜错的时候，`neighbors` 会给出几条**有证据、有位置、
    还通得过校验**的假路径——比少一条路难发现得多。所以这一层必须有可读的清单，
    也必须有删掉错的那一条的入口（`DELETE /api/knowledge/aliases/{alias}`）。

    读不到图（没配密码 / 连不上）时返回空列表 + `configured=false`，不抛 5xx：
    知识库面板不该因为图存储没开就打不开。
    """
    configured = graph_store.configured()
    if not configured:
        return AliasListing(items=[], total=0, configured=False)
    edges = graph_store.list_aliases(limit=limit)
    return AliasListing(
        items=[AliasEntry(alias=edge.alias, canonical=edge.canonical,
                          source=edge.source, similarity=edge.similarity,
                          created_at=edge.created_at) for edge in edges],
        total=len(edges),
        configured=True,
    )


@router.delete(
    "/aliases/{alias}",
    response_model=AliasDeletion,
    responses={
        404: {"description": "库里没有这条别名（`code=not_found`）"},
        503: {"description": "图谱存储暂时不可用（`code=graph_unavailable`）"},
    },
)
def delete_alias(alias: str, canonical: str | None = Query(None)):
    """删掉一条别名边（`alias → canonical`），实体与证据边一条不碰。

    删完之后这两个名字重新是两个实体：挂在它们身上的 `REL` 原样留着，路径从"被合并
    后的一条"回到各自的原样——所以这个动作是可逆的语义修正，不需要备份。

    `canonical` 可选：同名不同类型的节点上，一个别名可能指向两个主名，给上它只删
    这一对。不给就把这个别名的全部指向都删掉（界面按"这一对"删，见
    `GET /api/knowledge/aliases` 的返回项）。

    **不要用重跑实体消解来"修正"猜错**：它按同一批阈值重新算，删掉的那条会一起回来。
    """
    try:
        removed = graph_store.delete_alias(alias, canonical)
    except graph_store.GraphStoreNotConfigured as error:
        raise AppError(
            "graph_unavailable",
            "图谱存储没有启用（未配置 MINDMAP_NEO4J_PASSWORD），别名这一层不可用。",
            status=503,
        ) from error
    except graph_store.GraphStoreError as error:
        raise AppError(
            "graph_unavailable", "图谱存储暂时不可用，这条别名没能删除", status=503,
        ) from error
    if not removed:
        raise AppError("not_found", "库里没有这条别名", status=404)
    return AliasDeletion(alias=alias, deleted=removed)


@router.delete(
    "/aliases",
    response_model=AliasClearance,
    responses={503: {"description": "图谱存储暂时不可用（`code=graph_unavailable`）"}},
)
def clear_aliases():
    """清空全部别名边——**"这一批合并全错了"的退路**，不是常规修正手段。

    只错一两条时用 `DELETE /api/knowledge/aliases/{alias}`。整体清空之后想恢复只能
    重跑实体消解，而重跑会按同一批阈值重新猜一遍：删掉的那些猜错的会一起回来。
    实体与 `REL` 一条不碰，所以清空之后图就是"从没合并过"的样子。
    """
    try:
        removed = graph_store.clear_aliases()
    except graph_store.GraphStoreNotConfigured as error:
        raise AppError(
            "graph_unavailable",
            "图谱存储没有启用（未配置 MINDMAP_NEO4J_PASSWORD），别名这一层不可用。",
            status=503,
        ) from error
    except graph_store.GraphStoreError as error:
        raise AppError(
            "graph_unavailable", "图谱存储暂时不可用，别名没能清空", status=503,
        ) from error
    return AliasClearance(deleted=removed)
