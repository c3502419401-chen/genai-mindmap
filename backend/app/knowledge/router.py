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
import asyncio
import logging
import sqlite3
import threading
import time
from contextlib import closing
from functools import lru_cache
from typing import Literal
from uuid import uuid4

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
from app.knowledge import external, graph_resolve, graph_store, library, shelf
from app.knowledge.service import SearchRequest, SearchResponse, search
from app.mindmaps import repository

router = APIRouter(prefix="/knowledge", tags=["知识增强"])
logger = logging.getLogger(__name__)


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


class AliasEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    filename: str
    start: int
    end: int
    quote: str
    kind: Literal["graph_edge", "text_match"]


class AliasCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    alias: str
    canonical: str
    similarity: float
    reason: str
    #: 结构上"该看一眼"的提示（目前只有合称）：`碳达峰碳中和` 是两项政策的合称，
    #: `碳中和` 不是它的简写。它不改变可选性——真实的判断还是得由人做。
    notice: str = ""
    alias_evidence: list[AliasEvidence]
    canonical_evidence: list[AliasEvidence]
    selectable: bool


class AliasPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    status: Literal["running", "done", "failed", "cancelled", "applying", "applied"]
    stage: str
    error: str | None
    warnings: list[str]
    items: list[AliasCandidate]


class AliasApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=1, max_length=64)
    candidate_ids: list[str] = Field(min_length=1, max_length=500)


class AliasPair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str
    canonical: str


class AliasApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relationships_created: int
    created: list[AliasPair]
    skipped: int


# 预览不持久化；限时、限数量且最多一个运行线程，适合单机部署。
_PREVIEW_LOCK = threading.RLock()
_ALIAS_WRITE_LOCK = threading.Lock()
_PREVIEWS: dict[str, dict] = {}
_PREVIEW_TTL = 15 * 60
_MAX_PREVIEWS = 3


class _PreviewCancelled(Exception):
    pass


class _PreviewConflict(Exception):
    pass


def _prune_previews() -> None:
    now = time.monotonic()
    for job_id, job in list(_PREVIEWS.items()):
        if not job["running"] and job["status"] != "applying" and (
            now - job["finished_at"] > _PREVIEW_TTL
        ):
            del _PREVIEWS[job_id]


def _preview_view(job: dict) -> AliasPreview:
    return AliasPreview(job_id=job["job_id"], status=job["status"], stage=job["stage"],
                        error=job["error"], warnings=job["warnings"], items=job["items"])


def _preview_progress(job: dict, stage: str) -> None:
    with _PREVIEW_LOCK:
        if job["status"] == "cancelled":
            raise _PreviewCancelled
        job["stage"] = stage


def _preview_warning(job: dict, message: str) -> None:
    with _PREVIEW_LOCK:
        if job["status"] == "cancelled":
            raise _PreviewCancelled
        job["warnings"].append(message)


def _preview_evidence(
    names: list[str], coordinates: dict[str, list[tuple[str, int, int]]],
) -> dict[str, list[AliasEvidence]]:
    if not names:
        return {}
    result: dict[str, list[AliasEvidence]] = {}

    def excerpt(block: str, name: str) -> tuple[str, int, int] | None:
        position = block.find(name)
        if position < 0:
            return None
        left = max(block.rfind(mark, 0, position) for mark in "。！？\n") + 1
        stops = [block.find(mark, position + len(name)) for mark in "。！？\n"]
        right = min((stop + 1 for stop in stops if stop >= 0), default=len(block))
        left = max(left, position - 100)
        right = min(right, max(position + len(name), left + 240))
        quote = block[left:right].strip()
        return (quote, left, right) if name in quote else None

    # SQLite 只读连接不会执行 repository.connect() 的建表和迁移；只取块而非整篇。
    uri = repository.db_path().resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        @lru_cache(maxsize=32)
        def load_block(source_id: str, start: int, end: int):
            return conn.execute(
                f"SELECT filename, length(text), substr(text, ?, ?) FROM {library.SOURCES_TABLE} "
                "WHERE id = ?", (start + 1, end - start, source_id),
            ).fetchone()

        for name in names:
            for source_id, start, end in coordinates.get(name, []):
                if not (0 <= start < end and end - start <= 50000):
                    continue
                row = load_block(source_id, start, end)
                if row is None or row[1] is None or end > row[1] or row[2] is None:
                    continue
                found = excerpt(str(row[2]), name)
                if found is None:
                    continue
                # 坐标指向整个原始证据块；quote 是其中逐字摘录的短句。
                result.setdefault(name, []).append(AliasEvidence(
                    source_id=source_id, filename=row[0] or "未命名材料",
                    start=start, end=end, quote=found[0], kind="graph_edge",
                ))
                if len(result[name]) >= 2:
                    break
        missing = set(names) - result.keys()
        if missing:
            # 孤立实体没有 REL 坐标。最多扫描最近 100 篇的前 5 万字，逐字命中才展示；
            # 这只能说明材料中出现过名字，不声称这篇就是图谱抽取它的来源。
            rows = conn.execute(
                f"SELECT id, filename, substr(text, 1, 50000) FROM {library.SOURCES_TABLE} "
                "ORDER BY last_used_at DESC, id LIMIT 100"
            )
            for source_id, filename, block in rows:
                if not block:
                    continue
                for name in sorted(missing):
                    found = excerpt(str(block), name)
                    if found is None:
                        continue
                    quote, start, end = found
                    result[name] = [AliasEvidence(
                        source_id=source_id, filename=filename or "未命名材料",
                        start=start, end=end, quote=quote, kind="text_match",
                    )]
                    missing.remove(name)
                if not missing:
                    break
    return result


def _run_alias_preview(job: dict) -> None:
    try:
        _preview_progress(job, "正在读取已有别名")
        snapshot = graph_store.alias_snapshot()
        result = asyncio.run(graph_resolve.resolve_entities(
            dry_run=True, preview=True, progress=lambda stage: _preview_progress(job, stage),
            warning=lambda message: _preview_warning(job, message),
        ))
        _preview_progress(job, "正在回查材料出处")
        groups = result["candidates"]
        names = sorted({name for group in groups
                        for name in [group["canonical"], *group["aliases"]]})
        evidence = _preview_evidence(names, graph_store.entity_evidence(names))
        existing = set(snapshot)
        items: list[AliasCandidate] = []
        for group in groups:
            canonical = group["canonical"]
            for alias in group["aliases"]:
                if (alias, canonical) in existing:
                    continue
                alias_evidence = evidence.get(alias, [])
                canonical_evidence = evidence.get(canonical, [])
                items.append(AliasCandidate(
                    id=str(len(items)), alias=alias, canonical=canonical,
                    similarity=group["similarity"], reason=group["reason"],
                    notice=group.get("notice", ""),
                    alias_evidence=alias_evidence, canonical_evidence=canonical_evidence,
                    selectable=bool(alias_evidence and canonical_evidence),
                ))
        _preview_progress(job, "正在核对预览期间的别名变化")
        if graph_store.alias_snapshot() != snapshot:
            raise _PreviewConflict
        with _PREVIEW_LOCK:
            if job["status"] == "cancelled":
                raise _PreviewCancelled
            job.update(status="done", stage="候选已就绪", items=items, snapshot=snapshot,
                       running=False, finished_at=time.monotonic())
    except _PreviewCancelled:
        pass
    except Exception as exc:
        logger.warning("实体消解预览失败：%s", type(exc).__name__)
        if isinstance(exc, graph_resolve.EmbeddingNotConfigured):
            message = "向量服务未配置，无法查找候选。"
        elif isinstance(exc, graph_resolve.EmbeddingError):
            message = "向量服务调用失败或部分实体没有向量，请稍后重试。"
        elif isinstance(exc, graph_store.GraphStoreError):
            message = "图谱存储不可用，请检查连接后重试。"
        elif isinstance(exc, _PreviewConflict):
            message = "预览期间数据发生变化，请重新查找。"
        else:
            message = "候选查找或出处回查失败，请稍后重试。"
        with _PREVIEW_LOCK:
            if job["status"] != "cancelled":
                job.update(status="failed", stage="查找失败", error=message,
                           running=False, finished_at=time.monotonic())
    finally:
        with _PREVIEW_LOCK:
            job["running"] = False
            job["finished_at"] = time.monotonic()


@router.post("/aliases/candidates", response_model=AliasPreview, status_code=202)
def start_alias_preview():
    """创建只读候选任务；计算可能消耗向量额度。"""
    if not graph_store.configured():
        raise AppError("graph_unavailable", "图谱存储没有启用", status=503)
    if not graph_resolve.embedding_configured():
        raise AppError("embedding_unavailable", "向量服务未配置", status=503)
    with _PREVIEW_LOCK:
        _prune_previews()
        if any(job["running"] or job["status"] == "applying" for job in _PREVIEWS.values()):
            raise AppError("preview_busy", "已有候选任务正在运行，请稍后重试", status=409)
        while len(_PREVIEWS) >= _MAX_PREVIEWS:
            oldest = min(_PREVIEWS, key=lambda key: _PREVIEWS[key]["finished_at"])
            del _PREVIEWS[oldest]
        job = {"job_id": uuid4().hex, "status": "running", "stage": "正在启动",
               "error": None, "warnings": [], "items": [], "running": True, "finished_at": 0.0,
               "snapshot": []}
        _PREVIEWS[job["job_id"]] = job
        view = _preview_view(job)
        try:
            threading.Thread(target=_run_alias_preview, args=(job,), daemon=True).start()
        except (RuntimeError, OSError) as exc:
            del _PREVIEWS[job["job_id"]]
            raise AppError("preview_unavailable", "无法启动候选任务，请稍后重试", status=503) from exc
        return view


@router.get("/aliases/candidates/{job_id}", response_model=AliasPreview)
def read_alias_preview(job_id: str):
    with _PREVIEW_LOCK:
        _prune_previews()
        job = _PREVIEWS.get(job_id)
        if job is None:
            raise AppError("not_found", "预览不存在或已过期，请重新查找", status=404)
        return _preview_view(job)


@router.delete("/aliases/candidates/{job_id}", response_model=AliasPreview)
def cancel_alias_preview(job_id: str):
    with _PREVIEW_LOCK:
        _prune_previews()
        job = _PREVIEWS.get(job_id)
        if job is None:
            raise AppError("not_found", "预览不存在或已过期", status=404)
        if job["status"] in ("applying", "applied"):
            raise AppError("preview_conflict", "正在应用或已经应用，不能取消", status=409)
        job.update(status="cancelled", stage="已取消；已发出的请求返回后停止",
                   warnings=[], items=[], snapshot=[])
        if not job["running"]:
            job["finished_at"] = time.monotonic()
        return _preview_view(job)


@router.post("/aliases/apply", response_model=AliasApplyResult)
def apply_alias_preview(body: AliasApplyRequest):
    with _PREVIEW_LOCK:
        _prune_previews()
        job = _PREVIEWS.get(body.job_id)
        if job is None:
            raise AppError("not_found", "预览不存在或已过期，请重新查找", status=404)
        if job["status"] != "done" or job["running"]:
            raise AppError("preview_conflict", "预览尚未完成或已使用", status=409)
        choices = {item.id: item for item in job["items"]}
        if (len(set(body.candidate_ids)) != len(body.candidate_ids) or
                any(key not in choices or not choices[key].selectable
                    for key in body.candidate_ids)):
            raise AppError("invalid_candidates", "只能选择本次预览中有出处的候选", status=422)
        selected = [choices[key] for key in body.candidate_ids]
        snapshot = job["snapshot"]
        job.update(status="applying", stage="正在应用选中的别名")

    candidates = [graph_resolve.MergeCandidate(
        canonical=item.canonical, aliases=[item.alias], similarity=item.similarity,
        reason=item.reason,
    ) for item in selected]
    write_attempted = False
    try:
        # 同一进程的其他别名删除/清空与检查、写入、回读串行，避免撤回误删旧关系。
        with _ALIAS_WRITE_LOCK:
            names = sorted({name for item in selected for name in (item.alias, item.canonical)})
            evidence = _preview_evidence(names, graph_store.entity_evidence(names))
            if any(evidence.get(item.alias, []) != item.alias_evidence or
                   evidence.get(item.canonical, []) != item.canonical_evidence
                   for item in selected):
                raise _PreviewConflict
            write_attempted = True
            count = graph_store.add_aliases(candidates, expected_aliases=snapshot)
            with _PREVIEW_LOCK:
                job.update(status="applied", stage="已应用", finished_at=time.monotonic())
            current = set(graph_store.alias_snapshot())
    except _PreviewConflict as exc:
        with _PREVIEW_LOCK:
            job.update(status="failed", stage="出处已变化，请重新预览", finished_at=time.monotonic())
        raise AppError("preview_conflict", "材料或图谱出处已变化，请重新预览", status=409) from exc
    except graph_store.AliasSnapshotConflict as exc:
        with _PREVIEW_LOCK:
            job.update(status="failed", stage="别名表已变化，请重新预览", finished_at=time.monotonic())
        raise AppError("preview_conflict", "别名表已变化，请重新预览", status=409) from exc
    except graph_store.GraphStoreError as exc:
        with _PREVIEW_LOCK:
            committed = job["status"] == "applied"
            if not committed:
                job.update(status="failed", stage="写入结果未知，请先检查别名清单",
                           finished_at=time.monotonic())
        message = ("已提交写入，但无法读取结果；请刷新别名清单" if committed else
                   "写入结果未知，请先检查别名清单" if write_attempted else
                   "无法重新核对图谱出处，未执行写入")
        raise AppError("graph_unavailable", message, status=503) from exc
    except Exception as exc:
        logger.warning("实体消解应用失败：%s", type(exc).__name__)
        with _PREVIEW_LOCK:
            if job["status"] != "applied":
                job.update(status="failed", stage="写入结果未知，请先检查别名清单",
                           finished_at=time.monotonic())
        message = ("写入结果未知，请先检查别名清单" if write_attempted else
                   "无法重新核对材料出处，未执行写入")
        raise AppError("alias_apply_failed", message, status=500) from exc
    created = [AliasPair(alias=item.alias, canonical=item.canonical) for item in selected
               if (item.alias, item.canonical) in current]
    return AliasApplyResult(relationships_created=count, created=created,
                            skipped=len(selected) - len(created))


@router.get("/aliases", response_model=AliasListing)
def list_aliases(limit: int = Query(500, ge=1, le=2000)):
    """库里的实体别名（`alias → canonical`）。**"模型把两个名字认成一个"唯一看得见的地方**。

    别名是推断出来的：实体消解按构词关系与向量相似度猜"这两个名字指同一个
    东西"，产品入口让用户核对出处、逐对确认后才写入。猜错的时候，`neighbors` 会给出几条**有证据、有位置、
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
        with _ALIAS_WRITE_LOCK:
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
        with _ALIAS_WRITE_LOCK:
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
