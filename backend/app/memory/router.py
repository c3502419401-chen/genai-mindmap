"""记忆的 HTTP 面：列、加、删、按主题检索。

**为什么要有"手工添加"这个入口**（这一层最需要解释的一件事）：

自动总结的证据链是"会话结束 → 模型总结 → 落到表里 → 下次生成时被检索到"，
它串得很长，而且**第一步在空库上会合法地产出 0 条**（`SUMMARY_SYSTEM` 里那条
"0 条是完全合法的结果"）。于是这套东西第一次跑起来的真实形态是：

    空表 → 总结出 0 条 → 检索 0 条 → 注入空串 → 与改动前的行为逐字节相同

也就是说，在用户攒出第一条记忆之前，"功能正常"与"功能根本没接上"在外部表现上
完全一样。等它自己攒够，可能是几天，也可能是几周（取决于用户用多少、会话之间
隔多久），而这段时间里没有任何东西能被验证。

手工入口把这条链**从中间接上**：写一条进去，立刻就能用 `GET /search` 看它有没有
被检索到、score 是多少、衰减有没有生效。三件本来要等很久才能观察的事，现在一次
请求就能看到。

它不替代自动总结——`source` 字段把两者分开（`user_explicit` / `summary`），列表里
一眼能看出哪条是"我说的"、哪条是"系统猜的"。这与 `user_prefs` 里 `note.*` 的
区分是同一条原则。

**`GET /search` 为什么要有**：检索的正确性（尤其"衰减只影响排序、不删除"这条）
无法从外部观察。它返回每条命中的 `cosine` / `decay` / `score` / `age_days` 四个数，
让人能当场回答"这条为什么没进 top_k"——是因为语义不符（cosine 低），还是因为太旧
（decay 低）。只返回 `score` 的接口让人无法区分这两种情况，而要改的常量不是同一个。
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth import owner
from app.core.errors import AppError
from app.memory import store

router = APIRouter(prefix="/memory", tags=["记忆"])


class MemoryItem(BaseModel):
    """列表里的一条。**不带向量**：1024 个 float 对读者没有意义，它是机器用的。"""

    model_config = ConfigDict(extra="forbid")
    id: str
    text: str
    kind: str
    #: `summary`（会话末尾自动总结的）或 `user_explicit`（手工写的）。
    source: str
    created_at: str
    #: 模型对"这条大约是什么时候形成的"的判断，可空。衰减以它为基准（有就用它）。
    formed_at: str | None = None
    last_used_at: str | None = None
    session_id: str | None = None
    #: 有没有向量。`false` 说明这条**永远检索不到**（写入时向量服务未配置或失败），
    #: 它在列表里看起来完全正常——所以必须显式暴露出来。
    has_vector: bool
    model: str | None = None


class AddMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=store.MAX_ITEM_CHARS)
    kind: str = "preference"


class AddedMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: MemoryItem
    #: `false` = 这段文本已经存在（`text_hash` 去重），返回的是已有那条。
    #:
    #: 这一层不返回 409：再写一条一模一样的，语义上就是幂等的 upsert，而不是
    #: 一次冲突。用户想改内容就删了重写——`DELETE` 就在隔壁。
    created: bool


class MemoryHit(BaseModel):
    """一次命中的**全部**中间量。

    `score = cosine * decay`。三个数都给出，是为了让"为什么排在这里"可以当场读出来。
    """

    model_config = ConfigDict(extra="forbid")
    text: str
    kind: str
    cosine: float
    decay: float
    score: float
    #: 衰减用的年龄（天）。基准是 `COALESCE(formed_at, created_at)`。
    age_days: float | None = None
    created_at: str
    formed_at: str | None = None


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    hits: list[MemoryHit]
    #: 参与检索的行数。0 说明表是空的（而不是"全都不相关"）——这两种情况在只看
    #: `hits` 时长得一样，而它们要采取的动作完全不同。
    scanned: int
    top_k: int
    min_cosine: float


def _item(row: store.Item) -> MemoryItem:
    return MemoryItem(
        id=row.id, text=row.text, kind=row.kind, source=row.source,
        created_at=row.created_at, formed_at=row.formed_at,
        last_used_at=row.last_used_at, session_id=row.session_id,
        has_vector=row.has_vector, model=row.model,
    )


@router.get("", response_model=list[MemoryItem])
def list_memories(limit: int = Query(default=200, ge=1, le=2000)) -> list[MemoryItem]:
    """最近的若干条，新的在前。

    它同时是这套东西的**死表探测器**：表一直为空，说明自动总结要么没触发过
    （会话之间没隔够 `GAP_MINUTES`），要么每次都总结了 0 条。两种都不报错。

    **只列这个人自己的**：记忆里存的是"关于这个人的推断"（在做哪些项目、习惯
    怎样），跨账号读等于让所有人共享一份人格画像。加账号之前没有这条边界，因为
    那时只有一个人。
    """
    return [_item(row) for row in store.items(limit, owner=owner.current_owner())]


@router.post("", response_model=AddedMemory, status_code=status.HTTP_201_CREATED)
def add_memory(body: AddMemoryRequest) -> AddedMemory:
    """手工加一条。`source` 恒为 `user_explicit`，与自动总结的分开。

    向量服务未配置时条目照样写进去（`has_vector=false`），不报错：那是预期状态
    （本机开发、CI），而"这条以后检索不到"比"这句话丢了"轻得多。
    """
    if body.kind not in store.KINDS:
        # 用 `AppError` 而不是 `HTTPException(detail={...})`：后者的处理链只认 `detail`
        # 是字符串时的那些 code，传字典出去顶层 `code` 会退化成 `http_422`，前端拿到的
        # 就是一句没有信息量的兜底文案（`agent/router.py` 里踩过同一件事）。
        raise AppError(
            code="invalid_kind",
            message=f"记忆档位只能是 {'/'.join(store.KINDS)} 之一",
            status=422,
            detail={"kind": body.kind, "allowed": list(store.KINDS)},
        )
    added = store.add(body.text, body.kind, owner=owner.current_owner(),
                      source=store.SOURCE_USER_EXPLICIT)
    return AddedMemory(item=_item(added.item), created=added.created)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory(item_id: str) -> Response:
    """硬删。404 而不是幂等 204：界面上"这条已经不在了"与"我删了但没删掉"必须分开。

    别人的记忆同样回 404（`owner` 参与 `WHERE`）：既不删，也不告诉对方"这个 id
    存在但不属于你"——那本身就是一条不该泄漏的事实。
    """
    if not store.delete(item_id, owner=owner.current_owner()):
        raise AppError(
            code="memory_not_found",
            message="没有这条记忆，可能已经被删掉了",
            status=404,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 路由顺序：`/search` 必须排在 `/{item_id}` 之前。现在 `/{item_id}` 只有 DELETE，
# 所以还不冲突；把顺序写对是因为下一个人加 `GET /{item_id}` 时不会记得回头调换——
# 那时 `/memory/search` 会被当成 `item_id=search` 的一条记忆。
@router.get("/search", response_model=SearchResult)
def search_memory(
    q: str = Query(min_length=1, description="按这次的主题检索，与生成时的 query 同一口径"),
    top_k: int = Query(default=store.TOP_K, ge=1, le=50),
) -> SearchResult:
    """按主题检索一次，返回每条的 cosine / decay / score。

    **只读**：不刷 `last_used_at`（那是"真的被注入过"的证据，一次人工查询不该冒充它）。

    与生成时走的是同一个 `store.recall`，所以这里看到的分数就是模型会拿到的排序。
    """
    hits = store.recall(q, top_k=top_k, touch=False, owner=owner.current_owner())
    return SearchResult(
        query=q,
        hits=[MemoryHit(
            text=hit.text, kind=hit.item.kind, cosine=hit.cosine, decay=hit.decay,
            score=hit.score, age_days=hit.age_days, created_at=hit.item.created_at,
            formed_at=hit.item.formed_at,
        ) for hit in hits],
        scanned=store.count(owner=owner.current_owner()),
        top_k=top_k,
        min_cosine=store.MIN_COSINE,
    )


__all__ = ["router"]
