"""知识增强路由。

`/search` 是早期留下的检索桩（`implemented=false`）；`/library` 两个 GET 是
知识库的**只读入口**：列出库里有哪些材料、每份被几张已保存导图引用、点开看
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

from app.contracts.source import (
    CreateTextSourceResponse,
    FetchUrlRequest,
    Source,
    SourceKind,
    SourceMetadata,
)
from app.core.errors import AppError
from app.knowledge import external, shelf
from app.knowledge.service import SearchRequest, SearchResponse, search

router = APIRouter(prefix="/knowledge", tags=["知识增强"])


@router.post("/search", response_model=SearchResponse)
def search_knowledge(body: SearchRequest):
    """知识库检索（F12/F13）。

    当前实现返回空结果且 `implemented=false`，前端应据此禁用相关入口，
    不要把它当成"已经接好了知识库"。
    """
    hits = search(body.query, body.top_k)
    # 是否实现与是否命中是两件事；接入真实检索时须显式改为 True。
    return SearchResponse(hits=hits, implemented=False)


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
