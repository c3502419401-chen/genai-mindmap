"""导图的 HTTP 面：保存 / 读取 / 版本，以及**改动日志的两条读路径**。

改动日志的写入口不在这里单开一个端点——它并进 `POST` / `PUT` 的请求体（见
`contracts.mindmap.SaveRequest` 的注释）。第一次保存之前没有 `map_id`，一个
`POST /mindmaps/{id}/edits` 要么发不出去，要么必须先 create 再补发，于是多出
"内容存了、事件没存"这种半截状态，而统计从此只看见部分改动。

读有两条：`summary` 是这一版的交付物（分布），`edits` 是明细（那 42 条具体是什么）。
没有明细，一个反常的平均值只能连数据库才查得下去。
"""
import logging

from fastapi import APIRouter, HTTPException, Query, status

from app.auth import identity as identity_module
from app.auth import owner as owner_module
from app.auth import store as auth_store
from app.contracts.edits import EditEventRow, EditHealth, EditSummary
from app.contracts.mindmap import MapRecord, SaveRequest, UpdateRequest
from app.core.errors import AppError
from app.mindmaps import edits, repository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mindmaps", tags=["导图管理"])


def _deployer() -> bool:
    """当前请求是不是部署方（这台机器上第一个注册的账号）。

    与 `settings/router.py::_deployer` 同一个判据，也同一个理由：不给 `MINDMAP_ADMIN`
    环境变量，因为那会多一种"忘了设 / 设错了"的故障形态，而这台机器上第一个注册的人
    只可能是部署它的人。两处各写一份是因为它们读的是两套不同的东西（那边判"能不能改
    共享配置"，这里判"能不能接管无主的历史导图"），合并成一个公共函数反而会让改一个
    影响另一个。
    """
    user = identity_module.current_user()
    if user is None:
        return False
    first = auth_store.first_user()
    return first is not None and first.id == user.id


def _record_edits(body: SaveRequest, map_id: str) -> None:
    """内容已经落库之后再写事件。

    顺序不能反：`create_map` 之前没有 `map_id`。而失败方向也是刻意的——事件写不进
    只记 warning（`edits.record` 内部处理），不让用户的保存失败：为了统计丢掉一次
    真实保存，代价方向完全错了。

    `truncated` 只进日志，不落表：往行为事件表里插一条"我截断了"的假事件，会让每个
    统计口径的 `total` 都变脏。它不是丢弃——前端把超出的部分留在队列里等下次保存。

    `parent_id` 从**这次保存的那棵树**算（`edits.parents_of`），不从请求体读一个
    前端算好的值：树是权威，事件是它的描述，描述跟着权威算就不会出现"父 id 指向
    一个不存在的节点"。这也让这一列不需要动契约（`EditEventIn` 是
    `extra="forbid"`，加字段就要重导 `openapi.json` 与 `schema.d.ts`）。
    """
    if body.truncated:
        logger.warning("编辑事件分批：map_id=%s 本次提交 %d 条，另有 %d 条留到下次保存",
                       map_id, len(body.events), body.truncated)
    edits.record(map_id, body.events, parents=edits.parents_of(body.content),
                 owner=owner_module.current_owner())


@router.get("", response_model=list[MapRecord])
def list_maps():
    return repository.list_maps()


@router.post("", response_model=MapRecord, status_code=status.HTTP_201_CREATED)
def create_map(body: SaveRequest):
    record = repository.create_map(body.content)
    _record_edits(body, record.id)
    return record


# 跨图的健康检查，**必须排在 `/{map_id}` 系列之前**。理由与下面 `edits/summary`
# 那条注释同源：`/edits/health` 与 `/{map_id}/edits` 都是两个路径段，现在靠第二段
# 是 `health` 还是 `edits` 区分；一旦哪天有人把这里改成 `/{map_id}/health`，顺序就
# 不再是"看不出来但没事"，而是直接串路。放在前面最便宜。
@router.get("/edits/health", response_model=EditHealth)
def edits_health():
    """昨日编辑过的图数 / 昨日保存的图数：**死表探测器**。

    `user_prefs` 有"尚未生效"这种标记，`edit_events` 原本没有——于是"永远是 0 行"
    这种故障能静默存在很久。实测有过 40 分钟：8000 跑着旧代码，前端在发事件、
    后端在丢，而每一张图的 `summary.total` 都是 0，看起来和"没人改过"一模一样。

    跨图而不是按图，是因为"这张表还在工作吗"根本不是某一张图的属性。
    """
    return edits.health()


@router.get("/{map_id}", response_model=MapRecord)
def get_map(map_id: str):
    result = repository.get_map(map_id)
    if result is None:
        raise HTTPException(status_code=404, detail="导图不存在")
    return result


@router.put("/{map_id}", response_model=MapRecord)
def update_map(map_id: str, body: UpdateRequest):
    result = repository.update_map(map_id, body.version, body.content, allow_unowned=_deployer())
    if result == "missing":
        raise HTTPException(status_code=404, detail="导图不存在")
    if result == "forbidden":
        # 403 而不是 404：这张图你**看得见**（读是共享的），所以假装它不存在只会让人
        # 以为自己的图丢了。说清是权限问题，并给出唯一可行的下一步（另存为一份新的）。
        raise AppError(
            code="map_forbidden",
            message="这张导图不是你创建的，只有创建者能改。想保留自己的版本，"
                    "可以用「另存为」新建一份。",
            status=403,
        )
    if result == "conflict":
        # 内容没落库，事件也就无处归属：一起拒。重试一次两者都补上，
        # 不会出现"事件已写、内容被拒"这种统计漂移。
        raise HTTPException(status_code=409, detail="导图已被修改，请重新打开后再保存")
    _record_edits(body, map_id)
    return repository.get_map(map_id)


# `/{map_id}/edits/summary` 必须排在 `/{map_id}/edits` **之前**：两条路径段数不同，
# 现在的匹配不受顺序影响；把它写对是因为下一个人加 `/{map_id}/edits/xxx` 时不会
# 记得回头调换，而那时 `summary` 会被当成一个 `limit` 值或另一条子路径。
@router.get("/{map_id}/edits/summary", response_model=EditSummary)
def edit_summary(map_id: str):
    """这张图的改动分布。**这一版真正的交付物。**

    404 而不是"空统计"：给一个不存在的 id 查统计是调用方错了，返回一份全零的
    报告会让它以为"这张图没人改过"——那是完全不同的一件事。
    """
    if repository.get_map(map_id) is None:
        raise HTTPException(status_code=404, detail="导图不存在")
    return edits.summary(map_id)


@router.get("/{map_id}/edits", response_model=list[EditEventRow])
def list_edits(
    map_id: str,
    limit: int = Query(default=edits.RECENT_DEFAULT, ge=1, le=edits.RECENT_LIMIT),
):
    """最近的改动明细，新的在前。`limit` 有上限：这是给人读的一屏，不是导出通道。"""
    if repository.get_map(map_id) is None:
        raise HTTPException(status_code=404, detail="导图不存在")
    return edits.recent(map_id, limit)
