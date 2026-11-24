"""一次 agent 运行的**身份行**（`runs` 表）。

这张表存在的理由只有一个：run 此前只活在一个 HTTP 连接的寿命里。由此长出两个
症状，而它们其实是同一个缺口：

  1. **重复提交生成两张图**。请求里没有 run id，服务端就没有能力识别"这是同一次
     运行又投递了一遍"——换标签页、刷新重发、客户端重试都会新开一次真跑，花两倍
     钱产出两份结果，两个标签页各存一次就是两张图。原有的两道防线都在错的那一层：
     run 内的 `ctx.dedupe` 只活在一次运行里，保存层的版本乐观锁只认得"同一张已
     保存的图"。
  2. **关掉页面那 40 秒白花**。连接一断，生成器被 `aclosing` 关掉、工具里的模型
     请求被取消，已经付过钱的调用不回滚，而结果只活在内存里。用户回来什么都看不到。

所以：**SSE 从"run 本身"降级成"这一行的一次观察窗口"**（帧格式一个字节都没动）。
连接断了不代表运行没了，重发请求也不等于第二次运行。

两件事都在这张表上做，而不是各加一套机制：

- 身份：`begin()` 是唯一的入口，它在**一条 SQL 事务里**决定"这次是真跑、还是重复
  投递"。判在进程内的字典里不行——uvicorn 多 worker 时两个进程各有一份字典，
  "只有一个真跑"立刻变成"每个进程各跑一次"。
- 结果：`finish()` 在运行结束时（**含被取消**）把产出写进来。写在 `finally` 里，
  所以取消、异常、正常收尾三条路都只写一次。这一份**不是待确认的草稿**：生成结果
  已经直接进了画布（见 `docs/` 与前端 `stores/mindmap.ts`），存下来只为两件事——
  关掉页面还能捡回来，以及将来做生成历史。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Sequence

from pydantic import TypeAdapter

from app.contracts.mindmap import DiagramContent
from app.mindmaps import repository

logger = logging.getLogger(__name__)

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"

#: 不会再变的三种终态。`done` 之后**冻结**（见 `finish`），另两种可以重开。
TERMINAL_STATUSES = (STATUS_DONE, STATUS_CANCELLED, STATUS_FAILED)

#: 允许用同一个 run_id 重开的终态：用户主动重试。
RESTARTABLE_STATUSES = (STATUS_CANCELLED, STATUS_FAILED)

#: 一行 `running` 安静多久之后，可以断定它背后已经没有进程在跑了。
#:
#: 判据只能是 `updated_at`：这张表没有心跳列，而 run 最长能活 `max_seconds`
#: （请求上限 3600 秒，线上默认 600 秒）。取 1800 秒是"比线上默认最长的那一轮
#: 还多一倍"：活着的运行在 10 分钟内一定已经写过检查点（`checkpoint()` 会顺手
#: 刷新 `updated_at`），再往后还没有动静的，要么进程没了，要么前端早就放弃它了。
#:
#: 单靠这个数字**不足以**判定"活着"：一轮生成里最长的静默是**单次模型调用**，
#: 它受 `max_seconds` 约束，最长能到 3600 秒——比这个阈值还长。所以真正的第一道
#: 判据是本进程的活跃登记（`active()`）：自己还在跑的 run 一律不扫。1800 秒只对
#: "登记表里没有、又很久没动"的行生效，那种行要么属于已经死掉的进程，要么属于
#: 另一个进程——而部署是单 worker（见 `AGENTS.md`：不要引入多 worker 写假设）。
#:
#: 判错的代价是自愈的：把一个其实还活着的运行误判成 `failed` 之后，那一轮真正
#: 收尾时 `finish` 仍能改动（它只对 `done` 冻结），终态与最终那棵树都会盖回来。
RUN_ABANDONED_SECONDS = 1800

#: 本进程里正在跑的 run_id。`sweep_abandoned()` 跳过它们。
#:
#: 没有这张表，"活跃"就只能靠 `updated_at` 猜，而猜错的代价很实：一轮正在写说明的
#: 生成被判成 `failed` 之后，它后面的 `checkpoint()` 全部静默失效（那条 UPDATE 带
#: `WHERE status = 'running'`），用户下次进来看到的是一棵不再更新的树。
#:
#: 加锁是因为 `sweep_abandoned()` 可能从线程池里被调用（检查点那条路就是这么做的），
#: 而登记来自事件循环线程。
_ACTIVE: set[str] = set()
_ACTIVE_LOCK = threading.Lock()


def active(run_id: str) -> None:
    """登记一个正在跑的 run。调用方必须在 `finally` 里配 `inactive()`。"""
    with _ACTIVE_LOCK:
        _ACTIVE.add(run_id)


def inactive(run_id: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.discard(run_id)


def _active_ids() -> tuple[str, ...]:
    with _ACTIVE_LOCK:
        return tuple(_ACTIVE)


#: 进程没了的那种收尾。它只进台账（`failure`），不会作为 SSE 帧发给浏览器——
#: 断掉的那条连接早就没有读者了。
ABANDONED_FAILURE = {
    "code": "run_abandoned",
    "message": "上一轮生成没有跑完就断了（进程或连接中断），留下的是它当时已经生成的结构。",
}

#: 时间格式与 `mindmaps` / `library_*` 完全一致（SQLite 的 `%f` 是毫秒）。
#: 三处格式不同就会在"谁更晚"的比较上错一天。
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


@dataclass(frozen=True)
class RunRecord:
    """`runs` 里的一行。`candidate` 已按契约解析过，不是原始 JSON 字符串。

    `candidate` 的语义变了：它不再是"等你点应用的草稿"，而是**这次生成产出的那棵树
    的快照**。生成结果已经直接进了画布，这份存下来是为了两件事——关掉页面再回来
    还能把上次的结果捡回来（`pending`），以及将来做生成历史。它不再有任何"落地
    与否"的含义，那个由前端画布的 `dirty` 与 `applied_at` 回答。
    """

    run_id: str
    status: str
    request: dict[str, Any]
    candidate: DiagramContent | None
    failure: dict[str, Any] | None
    created_at: str
    updated_at: str
    #: 这次生成的结果**被保存进 `mindmaps` 了**。空值 = 生成过、但还没存下来。
    #:
    #: 它曾经是"用户点了应用"，候选层删掉之后那一天没有用户动作可记；剩下的、
    #: 也是唯一有区分度的判据是"后来有没有存"。
    applied_at: str | None = None
    #: 候选是怎么来的（`demo` / `model`）。重放时原样回填 `done` 帧的 `mode`，
    #: 否则一棵占位树会被贴上"模型生成"的标签——那正是 `DoneEvent.mode` 要防的。
    mode: str | None = None
    #: 这一轮真正发出去的模型名与模型的思考过程（`done` 帧上的那两个字段）。
    #:
    #: 落库的唯一理由是**重放**：同一个 run_id 再投一次时，前端拿到的应该与第一次
    #: 那条 `done` 完全相同。不存它们，重放出来的帧就少了"这次走的哪条路"和
    #: 思考过程——同一个 run 两条路径给两种结果，用户会以为重放丢了东西。
    #:
    #: 老行上两者都是 None（那时还没这两列），重放时整条字段不存在，与"没说"同义。
    model: str | None = None
    #: 服务端在响应体 `model` 里回报的名字（**它实际跑的**），与上面那个请求名分开存。
    #:
    #: 落库理由与 `model` 一样是重放：同一个 run_id 再投一次，界面上的
    #: 「请求 X → 服务 Y」不能只剩半句。老行上它是 NULL，重放时字段整条不存在。
    served_model: str | None = None
    reasoning: str | None = None
    #: 这次收尾**交出的树没长完**（预算耗尽、工具超时、连续失败、空转），以及为什么。
    #:
    #: 落库的理由与 `model` / `reasoning` 一样，都是为了让 `_replay` 与第一次
    #: 完全同形：重连后拿到的若是"完整生成"，用户会以为自己上一轮真的成功了。
    #:
    #: `partial` 用 `bool | None` 而不是 `bool`：老行上是 NULL，意思是"这条记录
    #: 早于这个字段"——与"明确知道它是完整的"（False）不是一回事。
    partial: bool | None = None
    partial_reason: str | None = None
    #: 这条记录里的 `candidate` 是**跑到一半截下来的检查点**，不是收尾交出的成品。
    #:
    #: 有值就说明那一轮在写完说明之前就断了（进程被杀、机器重启、连接永久断开），
    #: 树的结构已经长好、说明还是空的。没有这一列，界面只能说"生成完的导图还没
    #: 保存"——那句话对一棵结构完整但一句说明都没有的树是错的。
    #:
    #: `finish` 交出新的候选时会把它清掉：那一刻 `candidate` 不再是被截下来的半成品。
    checkpointed_at: str | None = None
    #: 这次生成是谁发起的（owner 字符串，与 `mindmaps.owner` 同一套口径）。
    #:
    #: **`runs` 的可见性比导图更严**：导图读共享（团队协作要能看别人的图），而
    #: `runs` 里是还没保存的树、模型的思考过程和失败原因——那些属于发起它的人。
    #: 所以列表与详情都按这一列过滤，压不过去就是"不存在"。
    #:
    #: 老行上是空串：加账号之前生成的记录，没有主人可考，于是对所有人都不可见。
    owner: str = ""


@dataclass(frozen=True)
class Claim:
    """`begin()` 的结果：这一次调用有没有真的拿到运行权。

    `claimed=False` 时 `record` 是**库里已有的那一行**，调用方据此决定是回 409
    还是把它的结果重放出来。判断与写入同一条事务，所以不存在"查的时候还在跑、
    写的时候已经结束"的空窗。
    """

    record: RunRecord
    claimed: bool


#: 落检查点时等写锁的上限（秒）。比默认的 10 秒短得多：检查点是"跑不完就少丢
#: 一点"的东西，它等锁的每一秒都是这一轮真正在跑的时间。拿不到锁就放弃，本轮
#: 照常往下走——收尾时 `finish()` 还会再写一次。
CHECKPOINT_LOCK_TIMEOUT = 2.0


def _connect(*, timeout: float = 10) -> sqlite3.Connection:
    """连接 + 关掉 Python 的隐式事务。

    `isolation_level=None` 是必需的：`begin()` 要显式开 `BEGIN IMMEDIATE` 才能让
    "读到的状态"和"写下的状态"在同一把写锁里。留在默认的隐式事务模式下，
    `BEGIN` 会撞上 Python 自己刚开的那条，抛 `cannot start a transaction within
    a transaction`——而那条错误只在并发时才出现，平时一点痕迹都没有。

    `timeout` 透传给 `repository.connect()`：只有检查点那条路会调短它。
    """
    conn = repository.connect(timeout=timeout)
    conn.isolation_level = None
    return conn


#: 读库时用的一份联合解析器：形状判别在契约层（`DiagramContent` 的注释），
#: 这里只负责"读得回来"。用 `TypeAdapter` 而不是某个成员类，是因为库里现在真的
#: 会同时有 `root` / `bones` / `stages` 三种形状的记录。
_CONTENT_ADAPTER: TypeAdapter[DiagramContent] = TypeAdapter(DiagramContent)


def _dump_candidate(candidate: DiagramContent | None) -> str | None:
    return None if candidate is None else candidate.model_dump_json()


def _loads(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        # 库里被人手改坏的 JSON 不该让整条读路径崩掉：一次运行的历史值不值得
        # 为它 500 一整个页面。认不出来就当没有。
        return None


def _record(row: sqlite3.Row) -> RunRecord:
    raw_candidate = _loads(row["candidate"])
    candidate: DiagramContent | None = None
    if isinstance(raw_candidate, dict):
        try:
            # 三套结构共用一份联合类型：形状判别在契约层（`DiagramContent` 的
            # 注释），这里只负责"读得回来"。用 `TypeAdapter` 而不是某个成员类，
            # 是因为库里现在真的会有 `bones` / `stages` 两种形状的记录。
            candidate = _CONTENT_ADAPTER.validate_python(raw_candidate)
        except ValueError:
            candidate = None
    request = _loads(row["request"])
    failure = _loads(row["failure"])
    # `row["partial"]` 只在补过列的新库上有；老库这一行读出来会是 NULL。
    # `sqlite3.Row` 对不存在的列会抛 `IndexError`，所以取值要按列名列表判一下——
    # 但 `_ensure_columns` 在每次连接时都跑，正常路径上列一定在。
    raw_partial = row["partial"] if "partial" in row.keys() else None
    return RunRecord(
        run_id=row["run_id"],
        status=row["status"],
        request=request if isinstance(request, dict) else {},
        candidate=candidate,
        failure=failure if isinstance(failure, dict) else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        applied_at=row["applied_at"],
        mode=row["mode"],
        model=row["model"],
        served_model=row["served_model"],
        reasoning=row["reasoning"],
        owner=(row["owner"] or "") if "owner" in row.keys() else "",
        partial=None if raw_partial is None else bool(raw_partial),
        partial_reason=row["partial_reason"] if "partial_reason" in row.keys() else None,
        checkpointed_at=row["checkpointed_at"] if "checkpointed_at" in row.keys() else None,
    )


def begin(run_id: str, request: dict[str, Any], *, owner: str | None = None) -> Claim:
    """认领一次运行：新 id 建行，旧 id 按它的状态分三种去向。

    - 不存在 → 建一行 `running`，`claimed=True`（这次真跑）。
    - `running` → 原样返回，`claimed=False`。调用方回 409：同一个 run 并发跑两次
      会互相打架（两次都往同一个候选上写，谁也说不清最终那棵树是哪个的）。
    - `done` → 原样返回，`claimed=False`。调用方把已有候选重放一遍，**不再调模型**。
    - `cancelled` / `failed` → 用户主动重试，把这行重新认领成 `running`。

    **任何分支都不碰 `candidate`**：同一 id 重复 `begin`（两个标签页同时点）不能把
    上一轮已经产出的草稿抹掉；重跑期间用户回来查，仍能看见那份草稿，直到新的
    终态写进新的一份。

    **重开时清 `partial` / `partial_reason`**：这两列描述的是"上一次那一轮交付了
    什么"，新一轮开始后就不再成立。不清的话，`finish` 的 `COALESCE` 会把上一轮的
    `partial` 保住，于是"这一轮什么都没产出、报错收尾"的台账上仍写着
    `partial=1`——前端点「载入」时按半棵树提示，而那份内容早就不是半棵树了。

    **重开时还要换掉 `request`**：重开意味着**另一个请求**在用这行跑（「补齐说明」
    就是拿检查点那行的 id 发 `intent="edit"`）。`request` 是台账上"这一行在做什么"
    的唯一来源——`router._summary()` 的 `intent` / `goal` 都从它取，前端据此决定把
    结果当"新作品"还是"改现有图"。不换的话，编辑一轮跑完，台账上还写着上一轮的
    `{"intent": "create"}`，前端载入时按 create 重置 `mapId`，用户点保存会变成新建
    一条而不是覆盖原记录。`candidate` 反过来**必须**留着：重开跑的是"在已有树上
    继续"，抹掉它等于把这次续写的起点删了。

    **重开只允许本人**：`WHERE ... AND owner = ?`。没有这个条件时，任何知道别人
    `run_id` 的人都能把他的 `failed` 行重开成自己的——`UPDATE` 里那句 `owner = ?`
    会把行改归发起人，原主人之后连 `GET /runs/{id}` 都是 404（审查实测过这条：
    B 用 A 的 `failed` id 发一次流，那行就变成 B 的了）。老行（`owner = ''`，加账号
    之前留下的）因此谁都不能重开：它们是没有主人的历史草稿，重新生成一次比"猜
    该归谁"更安全，也不需要再引入一个"部署方可以接管别人的行"的旁路。
    """
    payload = None if not request else json.dumps(request, ensure_ascii=False)
    if owner is None:
        from app.auth.identity import current_identity
        from app.settings.store import owner_of

        owner = owner_of(current_identity())
    # 先把僵住的 `running` 行收掉，再认领。顺序不能反：这一行如果是上一次崩溃
    # 留下的化石，先 `sweep` 才能把它判成 `failed`，下面那条 `UPDATE` 才有资格
    # 重开它。放在事务外面，是因为 `sweep_abandoned` 自己开一条 `BEGIN IMMEDIATE`，
    # 套在这里面的 `BEGIN IMMEDIATE` 里会直接报"事务里不能开事务"。
    try:
        sweep_abandoned()
    except Exception:  # noqa: BLE001 — 清理是维护动作，坏掉不该挡住用户这一次生成
        logger.warning("超龄 running 的清理没有跑成，这次照常认领", exc_info=True)
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            created = conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, status, request, created_at, updated_at, owner) "
                f"VALUES (?, ?, ?, {_NOW}, {_NOW}, ?)",
                (run_id, STATUS_RUNNING, payload, owner),
            )
            claimed = created.rowcount == 1
            if not claimed:
                reopened = conn.execute(
                    f"UPDATE runs SET status = ?, failure = NULL, "
                    f"partial = NULL, partial_reason = NULL, checkpointed_at = NULL, "
                    # 重开是**另一个请求**在用这行跑，`request` 必须换成这一轮的摘要；
                    # 空摘要（内部调用方不传）用 COALESCE 保住旧值，等于"这次没带摘要"。
                    # `owner` 不换：`WHERE ... AND owner = ?` 已经限定只有本人能重开，
                    # 这一句在语义上是冗余的自证（写回同一个值），留着是为了让 SQL
                    # 自解释"这行的主人还是他"。
                    f"request = COALESCE(?, request), owner = ?, updated_at = {_NOW} "
                    "WHERE run_id = ? AND status IN (?, ?) AND owner = ?",
                    (STATUS_RUNNING, payload, owner, run_id, *RESTARTABLE_STATUSES, owner),
                )
                claimed = reopened.rowcount == 1
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:  # pragma: no cover — 插入或重开的成功路径上必有一行
        raise RuntimeError(f"runs 里没有 run_id={run_id} 的行，插入与重开都没生效")
    return Claim(record=_record(row), claimed=claimed)


def finish(
    run_id: str,
    status: str,
    *,
    candidate: DiagramContent | None = None,
    failure: dict[str, Any] | None = None,
    mode: str | None = None,
    model: str | None = None,
    served_model: str | None = None,
    reasoning: str | None = None,
    partial: bool | None = None,
    partial_reason: str | None = None,
) -> bool:
    """写终态。返回这一行有没有真的被改动。

    两条不对称的规矩，都是为了"已经付过钱的产出不许被一条迟到的收尾抹掉"：

    - `candidate` 用 `COALESCE`：传 None 表示"这次没有新草稿"，**不清空**库里
      已有的那一份。被取消的运行常常是"树已经生成完、用户关了页面"，落库那一步
      绝不能因为某个分支没拿到 candidate 就把草稿写成 NULL。
    - `status = done` 之后冻结：`WHERE status <> 'done'`。不做这个限制，一次超时
      取消就可能把"已经交付给用户的候选"改写成 `cancelled`，前端下次加载时就不
      再提示它了。

    `failure` 反过来是直接赋值：它是"这一次的终态为什么是这样"，属于最新事实，
    上一次的失败原因留在上面只会让人读错。

    `model` / `served_model` / `reasoning` 用 `COALESCE`，与 `mode` 同一档：它们是
    **观察值**，缺了只说明这次没收尾到那一步（比如取消），不该把上一轮已经记下的值
    抹成 NULL——那一行代表的始终是同一次运行的结果。

    `partial` / `partial_reason` 也走 `COALESCE`：同一次运行的终态只可能写一次
    （`done` 之后冻结），传 None 的含义是"这次没说"，不是"这次是完整的"。

    `checkpointed_at` 在**这次真的交出候选**时被清掉：那一刻 `candidate` 已经是
    收尾交出的成品，不再是被截下来的半成品，界面不该还按"说明是空的"提示。
    """
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"finish 只接受终态，收到 {status!r}")
    dumped = _dump_candidate(candidate)
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            changed = conn.execute(
                "UPDATE runs SET status = ?, candidate = COALESCE(?, candidate), "
                "failure = ?, mode = COALESCE(?, mode), "
                "model = COALESCE(?, model), served_model = COALESCE(?, served_model), "
                "reasoning = COALESCE(?, reasoning), "
                "partial = COALESCE(?, partial), "
                "partial_reason = COALESCE(?, partial_reason), "
                "checkpointed_at = CASE WHEN ? IS NULL THEN checkpointed_at ELSE NULL END, "
                "updated_at = " + _NOW +
                " WHERE run_id = ? AND status <> ?",
                (status, dumped,
                 None if failure is None else json.dumps(failure, ensure_ascii=False),
                 mode, model, served_model, reasoning,
                 None if partial is None else int(partial),
                 partial_reason,
                 dumped,
                 run_id, STATUS_DONE),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return changed.rowcount == 1


def checkpoint(run_id: str, content: DiagramContent) -> bool:
    """把**跑到一半的树**存进这一行，别等收尾。

    存在的理由是一段实测过的空窗：结构那一次模型调用返回之后，树已经能读了
    （过了契约校验、裁剪完、骨架套上），但紧接着要发两次并发调用去写说明，实测
    这是整轮里最长的一段（56 个节点 26 秒上下，见 `_fill_details` 的 docstring）。
    这段时间里进程被杀、机器重启、笔记本合盖，那一整轮就白跑了——`finish()` 只在
    `finally` 里写一次，而 `finally` 根本没跑到。

    落库的时机选在"结构就绪、说明之前"，因为只有这一刻的树是**契约合法的完整树**：
    中途的预览帧只有 id 与 label（没有 detail、没有引用、没有 `expandable`），
    把它们当检查点存下来，读回来是一棵不合法也不完整的树，续写还得认第二套格式。

    只对 `running` 的行生效（`WHERE status = 'running'`）：一轮已经收尾了，这个迟到的
    检查点不该把终态的结果盖成半棵树。

    返回这一行有没有真的被写。写不进去不抛异常——调用方在生成的中途，为了一个
    "存不上就算了"的保险不该让整轮失败。

    等写锁的上限是 `CHECKPOINT_LOCK_TIMEOUT`（2 秒），不是默认的 10 秒：这条路上
    的阻塞发生在事件循环里（调用方在线程池里跑它，但等的是这一轮生成的时间），
    一把慢锁不值得陪满十秒。超时抛的 `sqlite3.OperationalError` 照常往外抛——由
    调用方决定记日志还是继续，这里不替它吞。
    """
    with closing(_connect(timeout=CHECKPOINT_LOCK_TIMEOUT)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            changed = conn.execute(
                "UPDATE runs SET candidate = ?, checkpointed_at = " + _NOW + ", "
                "updated_at = " + _NOW + " "
                "WHERE run_id = ? AND status = ?",
                (_dump_candidate(content), run_id, STATUS_RUNNING),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return changed.rowcount == 1


def sweep_abandoned(*, seconds: int = RUN_ABANDONED_SECONDS) -> list[str]:
    """把"说是在跑、其实早没人跑"的行判成 `failed`，返回被清扫的 run_id。

    这些行是崩溃的化石：进程在 `finish()` 之前没了，那一行就永远停在 `running`。
    它有两个后果，都不是"看起来不干净"而已：

    - **同一个 run_id 永远 409**：`begin()` 见到 `running` 就拒绝认领，用户重发
      那一轮会一直被告知"已经在跑了"，而他既看不到结果也没有可点的东西。
    - **台账里那棵树没人能捡**：`pending()` 只看 `done` / `cancelled`，`running`
      不在其中，所以检查点存得再好也没人读得到。

    判据是**两条**，缺一不可：

    1. 不在本进程的活跃登记里（`_ACTIVE`）。自己还在跑的 run 一律不碰——这一条
       才是"活着"的硬证据。
    2. `updated_at` 比阈值更旧。

    只靠第 2 条不够：一轮生成里最长的静默是**单次模型调用**，它受 `max_seconds`
    约束（请求上限 3600 秒），比阈值还长。只按时间来猜，这种轮次会被判成 `failed`，
    而它后面的 `checkpoint()` 带 `WHERE status = 'running'`，从此全部静默失效——
    用户下次进来看到的是一棵停在某一刻、再也不会更新的树。

    第 1 条只在单进程里有效，这与部署一致（`AGENTS.md`：不要引入多 worker 写假设）。
    多 worker 部署时，另一进程的活跃 run 只能靠时间兜——那也正是阈值取到 1800 秒
    这么宽的原因。

    截止时刻由 **SQLite 自己算**（`strftime(..., 'now', '-N seconds')`），不在这里
    用 Python 拼时间串：`updated_at` 是 SQLite 按 UTC 写下的同一格式，两边各算一次
    就有时区与精度两个地方会分叉，而分叉的表现是"清理要么一直不触发、要么清掉刚开
    的那一轮"，两种都很难从日志上认出来。
    """
    protected = _active_ids()
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT run_id FROM runs WHERE status = ? AND updated_at < "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
                (STATUS_RUNNING, f"-{max(1, seconds)} seconds"),
            ).fetchall()
            swept = [row["run_id"] for row in rows if row["run_id"] not in protected]
            if swept:
                placeholders = ", ".join("?" for _ in swept)
                conn.execute(
                    f"UPDATE runs SET status = ?, failure = ?, updated_at = {_NOW} "
                    f"WHERE run_id IN ({placeholders})",
                    (STATUS_FAILED,
                     json.dumps(ABANDONED_FAILURE, ensure_ascii=False),
                     *swept),
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return swept


def get(run_id: str) -> RunRecord | None:
    """按 id 取一行，**不过滤 owner**。

    只给内部调用方用（`stream_run` 判"这个 run_id 现在什么状态"、重放、`sweep`）。
    对外那条 `GET /runs/{run_id}` 必须用 `get_for(run_id, owner)`：`run_id` 是客户端
    自己生成并传上来的，猜中一个就等于读到别人还没保存的树，这与"runs 只对发起者
    可见"是同一条口径的两半。
    """
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return _record(row) if row else None


def get_for(run_id: str, owner: str | None = None) -> RunRecord | None:
    """按 id 取一行，**并且必须属于 `owner`**（缺省按当前身份算）。

    别人的行一律返回 `None`，让调用方回 404——不是 403：`run_id` 是随机 id，
    "这个 id 存在但不属于你"本身就是一条不该给的信息。
    """
    if owner is None:
        from app.auth.identity import current_identity
        from app.settings.store import owner_of

        owner = owner_of(current_identity())
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE run_id = ? AND owner = ?", (run_id, owner)).fetchone()
    return _record(row) if row else None


def pending(
    *,
    limit: int = 20,
    statuses: Sequence[str] = (STATUS_DONE,),
    unresolved_only: bool = True,
    owner: str | None = None,
) -> list[RunRecord]:
    """有结果、而且后来没被保存过的运行，最新的在前。

    默认只要 `done`。前端展示时传 `(done, cancelled, failed)`：后两种里也可能有
    值得捡回来的东西——被取消但已经产出结果的运行（那 40 秒就是这么省回来的），
    以及被 `sweep_abandoned()` 判死的运行（它留下的是崩溃前那个检查点）。两种措辞
    与"上次完成了"不同，但"有没有东西可捡"由 `candidate IS NOT NULL` 判，不由状态判。

    判据是 `applied_at`，不是"结果有没有写进 `mindmaps` 表"：那份内容可能被 PUT 到
    一张已有导图上（推不出新记录），也可能用户存成了另一张——用 `mindmaps` 反推
    只会得到一堆猜。

    `dismissed_at` 那半个条件已经删掉：没有"丢弃候选"这个动作了，而"用户没保存就
    走了"与"用户不要这份结果"在删掉候选层之后是同一件事——他现在可以撤销、可以
    直接改，也可以什么都不做。

    `unresolved_only=False` 时不去管"保存过没有"，只按状态列：那是查台账的用法
    （"上次那次失败到底怎么失败的"），与"提醒用户"是两件事，所以做成一个开关
    而不是两条 SQL。

    `owner` 缺省按当前身份算，**并且永远是过滤条件**：这一列不是可选优化，而是
    "`runs` 只对发起者可见"这条口径本身。想让某一行对所有人可见，那是导图的事，
    由 `mindmaps` 那张表回答。
    """
    if not statuses:
        return []
    if owner is None:
        from app.auth.identity import current_identity
        from app.settings.store import owner_of

        owner = owner_of(current_identity())
    placeholders = ", ".join("?" for _ in statuses)
    unresolved = ("AND candidate IS NOT NULL AND applied_at IS NULL"
                  ) if unresolved_only else ""
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"""SELECT * FROM runs
                 WHERE status IN ({placeholders}) AND owner = ? {unresolved}
                 ORDER BY updated_at DESC, run_id
                 LIMIT ?""",
            (*statuses, owner, max(1, limit)),
        ).fetchall()
    return [_record(row) for row in rows]


def mark_applied(run_id: str, *, owner: str | None = None) -> bool:
    """这次生成的结果**被保存进库了**。幂等：已经有时间戳就不再改。

    前端在保存成功之后调用一次。删掉候选层之后它是"生成过但没保存"的唯一反向证据：
    没有它，每次生成都会在下次进页面时重新问一遍"要不要捡回上次的结果"。

    `owner` 是过滤条件而不是可选项：否则知道别人 `run_id` 的人能替他标掉这一位，
    那条记录再也不会出现在他的「上次的结果」里——一条安静的数据丢失。
    """
    return _mark(run_id, "applied_at", owner=owner)


def _mark(run_id: str, column: str, *, owner: str | None = None) -> bool:
    # 只有一个固定列名会走到这里，不是拼接外部输入。
    assert column == "applied_at"
    if owner is None:
        from app.auth.identity import current_identity
        from app.settings.store import owner_of

        owner = owner_of(current_identity())
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            changed = conn.execute(
                f"UPDATE runs SET {column} = COALESCE({column}, {_NOW}) "
                "WHERE run_id = ? AND owner = ?",
                (run_id, owner),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return changed.rowcount == 1


__all__ = [
    "ABANDONED_FAILURE",
    "CHECKPOINT_LOCK_TIMEOUT",
    "Claim",
    "RESTARTABLE_STATUSES",
    "RUN_ABANDONED_SECONDS",
    "RunRecord",
    "STATUS_CANCELLED",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_RUNNING",
    "TERMINAL_STATUSES",
    "active",
    "begin",
    "checkpoint",
    "finish",
    "get",
    "get_for",
    "inactive",
    "mark_applied",
    "pending",
    "sweep_abandoned",
]
