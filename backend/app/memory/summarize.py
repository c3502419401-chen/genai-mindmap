"""会话结束时的总结管线：把一段工作记录压成几条"关于这个人"的记忆。

**"会话结束"是哪个时刻**（这一节是整条管线的地基）：

    后端能自己判定的唯一时刻，是**下一次运行开始时**：如果上一次运行已经结束、
    而且距它最后一次状态变化超过 `GAP_MINUTES`，那一段就是"上一个会话"，这时
    总结它。

另外三种候选都被否掉了，各有具体理由：

  - **"点 + 开新对话"**：用户不一定点。让"记忆有没有留下"依赖一个用户从来不按的
    按钮，等于这个功能大部分时候不生效，而且失效得无声无息。
  - **"关页面"**：后端不知道。SSE 断开只说明这个观察窗口结束了，运行本身可能还在
    跑（`agent/runs.py` 的整个设计就是为这件事），把它读成"会话结束"会总结一份
    还没写完的记录。
  - **"运行结束时立刻总结"**：那会把一次会话切成 N 段——用户改完图接着改一轮，
    两次运行的间隔常常是几十秒。逐段总结的后果是同一件事被记 N 遍（去重能挡住
    重复的文本，挡不住 N 次模型调用）。

**为什么是后端判定而不是前端配合**：前端的时机与后端的事实不是一件事。"用户点了
新对话"是界面状态，"这一段工作结束了"是数据事实。让后端从 `runs` 表自己读，
两边就不需要约定一个共同的时间定义——这条也是"不动前端"这个边界的根据。

**为什么要一张 `memory_sessions` 表**：不记"哪些会话已经总结过"，每次运行开始都
会重新总结上一次会话——同一个会话被总结十遍。`text_hash` 去重能挡住重复条目，
但挡不住重复的模型调用费用。这张表也承担"失败留痕"：`status='failed'` 的那一行
本身就是证据，而且它让失败**不会**被无限重试。

**崩溃后的幂等**：`memory_sessions` 的行在**所有条目写完之后**才写。进程在中间被
杀掉 → 没有这一行 → 下次重新总结整个会话 → `text_hash` 去重挡住重复条目。所以
"重试"这件事不需要额外的状态机。

**观测与命题挂在同一条链上（"事件片"而不是"时间片"）**：会话总结跑完（不管成功、
0 条还是失败）之后，`induce_propositions` 做两件事——先从 `runs` / `edit_events`
抽观测（`observations.capture`），再把没被归纳过的观测压成命题
（`propositions.induce`）。Coco 论文里这一步是常驻 Observer 按时间片跑；2C2G 的机器
上跑不起常驻进程，而这个仓库已经有一个"上一段工作结束了"的判定，所以把它换成
**事件片**：一次 run 开始时判一次，顺带归纳。粒度不同，语义一致——都是"一段工作
结束后归纳一次"。

开关是 `MINDMAP_MEMORY_PROPOSITIONS`（默认开）。默认开的理由：它只在**有未消费观测
且模型已配置**时才真的发请求，空库上只花两次 SELECT；关掉它等于让这条链永远不跑，
而"永远不跑"与"跑了但 0 条"在外部表现上一模一样——正是 `router` 的注释里写过的那种
无法验证的状态。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.generation.service import GenerationError, _config, chat_async
from app.memory import observations, propositions, store
from app.mindmaps import repository

logger = logging.getLogger(__name__)

#: 相邻两次运行之间隔多久，就算"换了一个会话"。
#:
#: 一个数同时用于两件事——切分会话、判定"上一个会话已结束"——因为它们本来就是
#: 同一句话的两面："两次运行之间隔了 20 分钟以上，那已经是两次不同的会话了；
#: 所以如果最后一次运行已经结束 20 分钟，这个会话就结束了。"
#:
#: 20 分钟取在两头之间：
#:   - **下界**：一次生成的最长时长是 `RunBudget.max_seconds` 的默认 600 秒。同一次
#:     会话里用户连续改图，两次运行的间隔实测是几十秒到几分钟。低于 10 分钟就会
#:     把"想了两分钟才接着改"切成两次会话，于是同一条偏好被总结两遍。
#:   - **上界**：30 分钟以上，"我回来接着干"就会被读成"新的一次会话"，而用户心里
#:     那还是同一件事——代价是错过一次总结（下一个会话会把它一起带上，因为窗口
#:     是"彼此间隔不超过 20 分钟的连续运行"，而不是固定长度）。
GAP_MINUTES = 20

#: 回看多少行 `runs` 来切分会话。200 与 `prefs._APPLIED_SCAN` 同值：它要够回溯到
#: 一个会话的开头（一次会话里一次编辑就是一行 run），同时防止一次全表扫。
_RUN_SCAN = 200

#: 总结输入里最多放几条运行。一条摘要约 60–100 字，40 条 ≈ 3000 字。
#:
#: 超了**保留最近的**：一个会话里后发生的动作更能代表"这个人现在怎么想"，
#: 而最早那几条多半已经被后面的运行覆盖（改回来、再改回去）。
MAX_RUNS = 40

#: 总结输入里最多放几条编辑事件。60 条 ≈ 2400 字。
MAX_EVENTS = 60

#: 一条 goal / 一个 before-after 片段在摘要里的长度上限。摘要不是搬运原文。
MAX_GOAL_CHARS = 80
MAX_SNIPPET_CHARS = 60

#: 总结的输出预算。6 条 × 100 字 ≈ 900 token，加上 JSON 结构留到 1600。
#:
#: 它会被 `max_tokens_cap()` 再夹一次（配置值与服务端学习值取小），所以这里写
#: 一个偏松的值是安全的。
SUMMARY_MAX_TOKENS = 1600

STATUS_DONE = "done"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"

#: 观测抽取 + 命题归纳这一段的开关。默认开，理由见模块 docstring。
INDUCE_ENV = "MINDMAP_MEMORY_PROPOSITIONS"

#: 一次触发里最多归纳几轮。每轮消费最多 `propositions.MAX_INPUT_OBSERVATIONS` 条
#: 观测，所以 5 轮 ≈ 200 条。积压更多时下一次触发接着处理——不会漏，只是分几次。
INDUCE_ROUNDS = 5

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def induce_enabled() -> bool:
    """`MINDMAP_MEMORY_PROPOSITIONS` 是不是开着。清空 = 用默认（开）。

    与 `prefs/style.prompt_enabled` 同一种读法：只有显式的 0/false/no/off 才算关，
    其余（包括拼错的值）一律按开处理。理由与那处相同——拼错一个值不该静默关掉一整条
    功能，那是"我明明打开了它却没用"的另一种写法。
    """
    value = os.environ.get(INDUCE_ENV)
    if value is None or not value.strip():
        return True
    return value.strip().lower() not in ("0", "false", "no", "off")


# ---------------------------------------------------------------- 提示词

#: 总结用的 system。**每一句都在挡一种具体的污染**，改之前先读一遍它们各挡什么。
SUMMARY_SYSTEM = """\
你在读一个人使用思维导图工具的近期记录（他提的要求、他事后怎么改的）。
你的任务是提炼**关于这个人的稳定事实**，供以后的生成参考。

## 一条内容值不值得记：一个测试

把这次的原始材料原样交给另一个人使用，这条话还成立吗？

  - 换谁来用这份材料都成立 → 那是**材料或这次主题**的属性，不是这个人的。
    **不要记。** 例：「这份材料讲的是机器学习」——换谁来都是这个主题。
  - 换成别人可能不成立 → 那是**这个人的**。
    **记下来。** 例：他把「以 Mitchell 三要素界定学习」改成了「三要素界定学习」
    → 他想记的是「标题倾向去掉人名与并列项」。

同一个测试的另一种问法：这条话下次生成时**能改变我的输出吗**？
不能的话（「他这次做了一个导图」），它是记录，不是记忆。

## 硬约束

1. 只写关于「这个人」的，不写「这次的主题」。主题内容一个字都不要复述。
2. 一条一句，不超过 100 字。不要编号、不要分点、不要解释为什么记它。
3. `kind` 只有三个值：
   - `preference`：表达方式上的取舍（标题怎么取、节点多长、要不要例子、要不要图标）
   - `fact`：关于这个人的稳定事实（他做什么方向、习惯用什么术语体系）
   - `pattern`：做事习惯（通常先要骨架再补细节、反复要求同一种图型）
4. `formed_at` 可选：如果你能从记录里判断这条大约是什么时候形成的，给
   `YYYY-MM-DD`；判断不了就**留空**。不要猜，猜出来的日期会让这条记忆的权重变错。
5. **产出 0 条是完全合法的结果。** 这次会话没什么可记的就返回空数组。为了凑数
   把主题内容改写成「他关注 X」是最常见的一种污染——那种条目在检索里永远命中，
   因为它和任何一次生成都"相关"，而它一个字的信息量都没有。
6. 不要重复已知的记忆（下面会给出）。同一条换个说法再说一遍，占掉的是别的记忆
   的位置。

## 输出

只输出一个 JSON 对象，不要任何别的文字、不要代码围栏：

{"memories": [{"text": "…", "kind": "preference", "formed_at": "2026-11-01"}]}
"""


# ---------------------------------------------------------------- 结构

@dataclass(frozen=True)
class SessionRun:
    """会话里的一次运行（只留总结用得上的字段，不带正文、不带候选树）。"""

    run_id: str
    status: str
    goal: str
    intent: str
    diagram_type: str | None
    prefs_applied: tuple[str, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Session:
    """一段已经结束的会话。

    `session_id` 取这段里**最后一次运行的 run_id**，不是第一次：会话是往后长的，
    用最后一条的 id 才能让"这个会话"在任何时刻都是一个确定的、可重新算出同一个值
    的标识。用第一次的 id 也行，但那个值要等到会话结束才稳定，而我们在开始总结的
    那一刻就已经知道结束了。
    """

    session_id: str
    started_at: str
    ended_at: str
    runs: tuple[SessionRun, ...]


@dataclass(frozen=True)
class Outcome:
    """一次总结的结果。`status` 与 `memory_sessions.status` 同一套值。"""

    session_id: str
    status: str
    produced: int
    created: int
    detail: str | None = None


# ---------------------------------------------------------------- 会话判定

def _llm_ready() -> bool:
    """总结要用模型，没配置就别开始。

    与 `_config()` 共用一份判定（它抛 `GenerationError` 就是没配好）：这里再读一遍
    环境变量，就成了"配置要求改一处、判断留在两处"。

    **没配好时返回 False 而不是去尝试一次必然失败的调用**，而且**不写
    `memory_sessions` 行**：未配置是预期状态（本机开发、CI、演示模式都是），不是
    故障。写成 `failed` 的后果是这台机器上所有历史会话被永久标记为"试过了"，
    等真的配好 key 之后它们再也不会被总结。
    """
    try:
        _config()
    except GenerationError:
        return False
    return True


def _recent_runs(limit: int, exclude_run_id: str | None) -> list[Any]:
    sql = "SELECT run_id, status, request, created_at, updated_at FROM runs"
    params: tuple[Any, ...] = ()
    if exclude_run_id:
        sql += " WHERE run_id <> ?"
        params = (exclude_run_id,)
    sql += " ORDER BY created_at DESC, run_id DESC LIMIT ?"
    with closing(repository.connect()) as conn:
        return conn.execute(sql, (*params, limit)).fetchall()


def _session_run(row: Any) -> SessionRun:
    try:
        request = json.loads(row["request"] or "{}")
    except ValueError:
        request = {}
    if not isinstance(request, dict):
        request = {}
    prefs = tuple(
        str(item) for item in request.get("prefs_applied") or [] if isinstance(item, str)
    )
    diagram = request.get("diagram_type")
    return SessionRun(
        run_id=row["run_id"],
        status=row["status"],
        goal=str(request.get("goal") or "").strip()[:MAX_GOAL_CHARS],
        intent=str(request.get("intent") or "create"),
        diagram_type=diagram if isinstance(diagram, str) else None,
        prefs_applied=prefs,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _summarized(session_id: str) -> bool:
    """这个会话是不是已经处理过（含"0 条"与"失败"两种结论）。"""
    with closing(repository.connect()) as conn:
        row = conn.execute(
            f"SELECT 1 FROM {store.SESSION_TABLE} WHERE session_id = ?", (session_id,)
        ).fetchone()
    return row is not None


def due(*, exclude_run_id: str | None = None, now: datetime | None = None) -> Session | None:
    """有没有一个"刚结束、还没总结过"的会话；没有则 None。

    调用点只有一个：`agent.router.stream_run` 在**认领完这次运行之后**（所以当前
    运行那行已经进了表，必须 `exclude_run_id` 把它排除掉）。

    纯读、同步、毫秒级：三次 sqlite 查询（runs 若干行、memory_sessions 一行），
    没有网络，没有模型。真正的重活在 `run_session` 里，跑在后台线程。
    """
    if not _llm_ready():
        logger.debug("会话总结跳过：模型未配置")
        return None
    rows = _recent_runs(_RUN_SCAN, exclude_run_id)
    if not rows:
        return None
    head = rows[0]
    moment = now or store.utcnow()
    if moment.tzinfo is None:
        # 用例里传进来的 naive 时间按 UTC 解释（库里全是 UTC）：不补这一步的后果
        # 是 `TypeError: can't subtract offset-naive and offset-aware`。
        moment = moment.replace(tzinfo=timezone.utc)
    ended = store.parse_time(head["updated_at"])
    if ended is None:
        logger.warning("会话总结跳过：最近一次运行的 updated_at 读不出来（run_id=%s）",
                       head["run_id"])
        return None
    if (moment - ended).total_seconds() < GAP_MINUTES * 60:
        # 上一个会话可能还在继续（用户随时会接着改），或者刚结束还没过间隔。
        return None

    gap_seconds = GAP_MINUTES * 60
    members = [head]
    for row in rows[1:]:
        if len(members) >= MAX_RUNS:
            # 更早的运行不进来，但会话本身仍然成立：我们只是不再往回看。
            logger.info("会话 %s 超过 %d 次运行，总结只用最近的那部分",
                        head["run_id"][:8], MAX_RUNS)
            break
        previous = store.parse_time(members[-1]["created_at"])
        current = store.parse_time(row["created_at"])
        if previous is None or current is None:
            break
        if (previous - current).total_seconds() > gap_seconds:
            break
        members.append(row)

    session = Session(
        session_id=head["run_id"],
        started_at=members[-1]["created_at"],
        ended_at=head["updated_at"],
        runs=tuple(_session_run(row) for row in reversed(members)),
    )
    if _summarized(session.session_id):
        return None
    return session


# ---------------------------------------------------------------- 输入

def _events(started_at: str, ended_at: str) -> list[Any]:
    """这次会话期间（含结束后一个间隔内）的编辑事件。

    窗口的尾巴要延长 `GAP_MINUTES`：**用户的修改几乎总在运行结束之后发生**。只取
    `[started_at, ended_at]` 的话，"他连续三次把图型改成鱼骨图"这件事在事件里一条
    都看不到——那三次改动的 `recorded_at` 全都晚于当次运行的 `updated_at`。

    用 `recorded_at`（服务端入库时刻）而不是 `at`（前端时钟）：前端的时钟不可信，
    而窗口边界判错会让一次改动落进相邻的会话。库里所有时间戳都是 UTC 的
    `...Z` 定长格式，字符串比较与时间顺序一致。
    """
    end = store.parse_time(ended_at)
    if end is None:
        return []
    tail = (end + timedelta(minutes=GAP_MINUTES)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    with closing(repository.connect()) as conn:
        rows = conn.execute(
            "SELECT actor, kind, field, before, after, depth, subtree_size, node_count "
            "FROM edit_events WHERE recorded_at >= ? AND recorded_at <= ? "
            "ORDER BY recorded_at DESC, id DESC LIMIT ?",
            (started_at, tail, MAX_EVENTS),
        ).fetchall()
    return list(reversed(rows))


def _clip(text: str | None, limit: int = MAX_SNIPPET_CHARS) -> str:
    value = " ".join((text or "").split())
    return value if len(value) <= limit else value[:limit] + "…"


def _run_line(index: int, run: SessionRun) -> str:
    parts = [f"{index}. [{run.intent}] {run.goal or '（无目标文本）'}"]
    if run.diagram_type:
        parts.append(f"图型: {run.diagram_type}")
    if run.prefs_applied:
        parts.append("生效偏好: " + ", ".join(run.prefs_applied))
    if run.status != "done":
        parts.append(f"状态: {run.status}")
    return "｜".join(parts)


def _event_line(row: Any, count: int = 1) -> str:
    """一条编辑事件 → 一句摘要。

    措辞刻意用**动词开头**（"改 label：A → B"而不是"他label：A → B"）：主语由
    `actor` 决定，而 `generate` 那条不属于"谁改了什么"（它是统计的分母），不该带
    主语。第一版统一拼成"他{field}："，读出来是"他label"，而且把一次生成也写成了
    "他一次生成落了 23 个节点"——那句话读起来像他亲手加的 23 个节点。
    """
    kind = row["kind"]
    depth = f"第 {row['depth']} 层" if row["depth"] else ""
    if kind in ("update", "style"):
        body = f"改 {row['field'] or '?'}：{_clip(row['before'])} → {_clip(row['after'])}"
    elif kind == "add":
        body = f"新增「{_clip(row['after'])}」"
    elif kind == "remove":
        size = row["subtree_size"]
        tail = f"（含 {size} 个节点）" if size and size > 1 else ""
        body = f"删掉「{_clip(row['before'])}」{tail}"
    elif kind == "move":
        body = "移动了节点"
    elif kind == "generate":
        # 不带主语：这是"一次生成落了几个节点"，是统计的分母，不是一次修改。
        return f"这次生成落了 {row['node_count']} 个节点"
    else:
        body = kind
    actor = "他" if row["actor"] == "user" else "模型"
    repeat = f"（×{count}）" if count > 1 else ""
    return "｜".join(part for part in (f"{actor}{body}{repeat}", depth) if part)


def _collapse_events(rows: list[Any]) -> list[tuple[Any, int]]:
    """把**连续**完全相同的编辑折叠成一条，带上次数。

    实测出来的必要：一次会话里同一处改了三遍，prompt 里就是三行一模一样
    「改 label：以Mitchell三要素界定学习 → 三要素界定学习」。模型读到三行同样的
    改动，会把它读成"三个不同场合的偏好"（于是产出三条互相重复的记忆，去重只能
    挡掉字面完全一致的那些），而真实信息是"同一件事他改了三遍"——次数就是这条
    信息，折叠之后它还在（`（×3）`）。

    只折叠**连续**的：`A, B, A` 是"改了这个、改了那个、又改回来"，那三个动作各
    自是一件事，合并会把"改回来"这个最有信息量的动作抹掉。
    """
    grouped: list[tuple[Any, int]] = []
    previous_key: tuple[Any, ...] | None = None
    for row in rows:
        key = (row["kind"], row["field"], row["before"], row["after"],
               row["depth"], row["actor"])
        if grouped and key == previous_key:
            grouped[-1] = (grouped[-1][0], grouped[-1][1] + 1)
        else:
            grouped.append((row, 1))
        previous_key = key
    return grouped


def user_text(session: Session, events: list[Any], known: list[str]) -> str:
    """总结的 user 消息。**只放结构化摘要，不放任何正文**。

    材料正文不进这里，理由与 `runs.request` 只存 source id 相同：总结要回答的是
    "这个人怎么做事"，而正文是"这次做了什么"。一份 20 万字的正文进来，模型会
    转而复述它——那正是 `SUMMARY_SYSTEM` 里那条判据要挡的污染。
    """
    stamp = store.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"现在是 {stamp}。",
        f"下面是这个人最近一次会话（共 {len(session.runs)} 次运行，"
        f"{session.started_at} 开始，{session.ended_at} 结束）。",
        "",
        "## 他的要求（按时间顺序）",
    ]
    lines.extend(_run_line(index, run) for index, run in enumerate(session.runs, start=1))
    lines.append("")
    if events:
        collapsed = _collapse_events(events)
        lines.append(f"## 他事后的修改（{len(events)} 处，合并连续重复后 {len(collapsed)} 条）")
        lines.extend(f"- {_event_line(row, count)}" for row, count in collapsed)
    else:
        lines.append("## 他事后的修改")
        lines.append("（这次没有任何修改：生成的结果他一个字都没动）")
    if known:
        lines.append("")
        lines.append("## 已知的记忆（不要重复，也不要换个说法再说一遍）")
        lines.extend(f"- {text}" for text in known)
    return "\n".join(lines)


def _known_memories(limit: int = 40) -> list[str]:
    """把已有的记忆给模型看一眼，避免同一条被反复总结。

    去重靠 `text_hash`，而哈希只认一字不差的文本。「标题去掉人名」与「标题里不要
    写人名」是两条不同的哈希，却是同一件事。这一段的成本是 40 × 100 字 ≈ 2700 字，
    收益是少掉一整类"同一句偏好有五种说法"的重复。
    """
    return [item.text for item in store.items(limit)]


# ---------------------------------------------------------------- 解析与写库

def _formed_at(value: object, moment: datetime) -> str | None:
    """模型的 `formed_at` → 规范化时间戳；不可信就 None。

    三条都要挡：不是字符串、解析不出来、**晚于现在**。最后一条最要紧——模型偶尔会
    把一个还在进行中的习惯写成"未来形成的"，而一个未来的时间戳会让衰减因子变成
    > 1（`age_days` 被钳到 0 之后是 1.0，但它同时也把这条排到最前面），等于给一条
    坏数据额外加成。
    """
    if not isinstance(value, str):
        return None
    parsed = store.parse_time(value.strip())
    if parsed is None:
        return None
    if parsed > moment:
        logger.debug("丢弃一个未来的 formed_at：%s", value[:32])
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_items(payload: object, moment: datetime) -> list[tuple[str, str, str | None]]:
    """模型输出 → [(text, kind, formed_at)]。形状不对抛 `ValueError`。"""
    raw: object = payload
    if isinstance(payload, dict):
        raw = payload.get("memories")
        if raw is None:
            raw = payload.get("items")
    if raw is None:
        # 空对象是合法的"这次没记下什么"，但一个**没有 memories 键**的对象不是：
        # 那是模型没按格式输出，静默当成 0 条会让"总结管线坏了"看起来像"没什么
        # 可记"。区分这两种情况是这条函数唯一的抛错理由。
        if isinstance(payload, dict) and not payload:
            return []
        raise ValueError("模型输出里没有 memories 数组")
    if not isinstance(raw, list):
        raise ValueError("memories 不是数组")
    parsed: list[tuple[str, str, str | None]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        kind = entry.get("kind")
        if kind not in store.KINDS:
            # 档位只影响列表里怎么分组，不影响检索。为一个枚举值丢掉一条真记忆，
            # 代价方向错了；兜底成 `preference` 并在日志里留一句。
            logger.debug("未知的记忆档位 %r，按 preference 处理", kind)
            kind = "preference"
        parsed.append((text, str(kind), _formed_at(entry.get("formed_at"), moment)))
    return parsed


def _mark(session_id: str, status: str, count: int, detail: str | None) -> None:
    """写会话的处理结论。**失败只记 warning**：它不该让一次成功的总结变成失败。"""
    try:
        with closing(repository.connect()) as conn, conn:
            conn.execute(
                f"INSERT INTO {store.SESSION_TABLE} "
                f"(session_id, status, item_count, detail, summarized_at) "
                f"VALUES (?, ?, ?, ?, {_NOW}) "
                f"ON CONFLICT(session_id) DO UPDATE SET status = excluded.status, "
                f"item_count = excluded.item_count, detail = excluded.detail, "
                f"summarized_at = excluded.summarized_at",
                (session_id, status, count, None if detail is None else detail[:200]),
            )
    except Exception as exc:  # noqa: BLE001 — 见 docstring
        logger.warning("会话 %s 的处理结论没有落库：%s", session_id[:8], type(exc).__name__)


def _persist(
    session: Session, parsed: list[tuple[str, str, str | None]],
) -> tuple[int, int]:
    """落库；返回 (有效条数, 真正新增条数)。"""
    created = 0
    for text, kind, formed_at in parsed:
        try:
            added = store.add(
                text, kind,
                source=store.SOURCE_SUMMARY,
                source_run_id=session.session_id,
                session_id=session.session_id,
                formed_at=formed_at,
            )
        except ValueError:
            # 空文本之类：`_parse_items` 已经挡过一遍，这里只兜底，不中断整批。
            logger.warning("一条记忆没写进去：文本不合法（会话 %s）", session.session_id[:8])
            continue
        created += 1 if added.created else 0
    return len(parsed), created


async def _summarize(session: Session) -> object:
    """调模型。**不走 `deep_thinking`**：总结不需要推理。

    这条结论有具体依据：要判断的是"这句话关于这个人还是关于这次主题"，那是一个
    分类动作，不是多步推理；而推理模型在这条路上的代价是 3–10 倍的时长与输出
    （思考过程本身要几百到几千 token）。总结在后台跑，但它的输出预算与延迟仍然
    是有限的资源，花在一个分类任务上没有收益。

    走 `chat_async` 而不是直接发 HTTP：它是这个仓库里模型出口的唯一一处实现
    （`test_model_exit_single_path.py` 钉着这件事），配置读取、上下文预检、
    max_tokens 降级、错误文案都在里面。这里不传 `deep_thinking`，缺省即 `False`。
    """
    return await chat_async(
        SUMMARY_SYSTEM,
        user_text(session, _events(session.started_at, session.ended_at),
                  _known_memories()),
        max_tokens=SUMMARY_MAX_TOKENS,
    )


def run_session(session: Session) -> Outcome:
    """跑一次总结。同步、可在线程里跑、**任何失败都留痕**。

    为什么是同步函数：它要在线程里跑（`schedule`），而线程里没有运行中的事件循环，
    自己 `asyncio.run` 一个就够了。顺带的好处是用例可以直接调它，不必起 loop。

    失败分两类，处理相同但日志不同（要改的东西不一样）：

      - 模型调用失败（`GenerationError` / 超时 / 上游 5xx）→ `status=failed`，
        **不自动重试**。自动重试会在配置坏掉时每次都烧一次调用，而那是最坏的一种
        循环。要重试就删掉 `memory_sessions` 里那一行——它是一行数据，不是一个隐藏
        状态机。
      - 输出格式不对（连不上 JSON、没有 memories 键）→ 同样 `failed`。
    """
    moment = store.utcnow()
    try:
        payload = asyncio.run(_summarize(session))
    except GenerationError as exc:
        detail = f"模型调用失败：{exc}"
        logger.warning("会话 %s 的总结失败：%s", session.session_id[:8], detail[:160])
        _mark(session.session_id, STATUS_FAILED, 0, detail)
        return Outcome(session.session_id, STATUS_FAILED, 0, 0, detail)
    except Exception as exc:  # noqa: BLE001 — 后台线程里抛出去没有任何人接得住
        detail = f"总结过程异常：{type(exc).__name__}"
        logger.warning("会话 %s 的总结异常", session.session_id[:8], exc_info=True)
        _mark(session.session_id, STATUS_FAILED, 0, detail)
        return Outcome(session.session_id, STATUS_FAILED, 0, 0, detail)

    try:
        parsed = _parse_items(payload, moment)
    except ValueError as exc:
        detail = f"模型输出格式不对：{exc}"
        logger.warning("会话 %s 的总结输出不可用：%s", session.session_id[:8], detail[:160])
        _mark(session.session_id, STATUS_FAILED, 0, detail)
        return Outcome(session.session_id, STATUS_FAILED, 0, 0, detail)

    if not parsed:
        # 0 条是**合法结论**，不是失败：这次会话没什么可记的。它要留一行记录，
        # 否则同一段会话会在每次新运行时被重新总结一遍。
        _mark(session.session_id, STATUS_EMPTY, 0, None)
        logger.info("会话 %s 的总结结束了：0 条（这次没什么可记的）",
                    session.session_id[:8])
        return Outcome(session.session_id, STATUS_EMPTY, 0, 0)

    produced, created = _persist(session, parsed)
    _mark(session.session_id, STATUS_DONE, created, None)
    logger.info("会话 %s 的总结结束了：产出 %d 条，新增 %d 条（其余命中去重）",
                session.session_id[:8], produced, created)
    return Outcome(session.session_id, STATUS_DONE, produced, created)


# ---------------------------------------------------------------- 触发

#: 正在总结的会话 id。并发请求同时开始（两个标签页、刷新重发）都会调到
#: `schedule`，没有这道闸就会起两个线程总结同一个会话——两次模型调用、两份产出
#: （`text_hash` 去重只挡住重复条目，挡不住钱）。
_INFLIGHT: set[str] = set()
_LOCK = threading.Lock()

#: 归纳的串行化闸。与 `_INFLIGHT` 分开：那个按会话 id 拦重复，这个拦**跨会话**的
#: 并发——两个会话同时结束时会各起一个线程，两边都会读到同一批 `unconsumed` 观测，
#: 于是同一段证据被归纳两遍（`text_hash` 去重能挡住重复命题，挡不住两次调用）。
_INDUCE_LOCK = threading.Lock()


def induce_propositions(
    *, run_id: str | None = None, session_id: str | None = None,
    rounds: int = INDUCE_ROUNDS,
) -> list[propositions.Induction]:
    """抽一次观测，再把没归纳过的观测压成命题。返回每轮归纳的结果。

    这是"事件片"的实现：粒度是**一段工作结束**，不是论文里常驻 Observer 的时间片。
    调用点在 `schedule()` 起的后台线程里，所以它慢（两三次模型往返）也不影响任何人。

    拿不到归纳闸时**直接返回空**，不排队等待：那说明另一段工作正在归纳，而它读的是
    同一张表里的同一批观测——等它做完，这批观测已经不在 `unconsumed` 里了。

    所有异常都吞掉并记 warning：这是一条"有则更好"的旁路，它挂掉不该让会话总结
    看起来失败（那条链自己已经有失败留痕了）。
    """
    if not induce_enabled():
        logger.debug("观测/命题这一段被 %s 关掉了", INDUCE_ENV)
        return []
    if not _INDUCE_LOCK.acquire(blocking=False):
        logger.debug("另一段工作正在归纳命题，这次跳过")
        return []
    try:
        captured = observations.capture()
        if not captured.total:
            # 没有新观测时也继续：上一轮可能因为模型没配置而留下了未消费的观测。
            logger.debug("观测没有新增，仍然检查一次未归纳的积压")
        results: list[propositions.Induction] = []
        for _ in range(max(1, rounds)):
            outcome = propositions.induce(run_id=run_id, session_id=session_id)
            results.append(outcome)
            if outcome.status != STATUS_DONE:
                # skipped（模型未配置）/ failed（下次重试）/ empty（没积压了）都不该
                # 继续循环：继续只会在同一批观测上再花一次调用，或者空转。
                break
        return results
    except Exception:  # noqa: BLE001 — 旁路失败不该让会话总结看起来失败
        logger.warning("观测/命题这一段异常", exc_info=True)
        return []
    finally:
        _INDUCE_LOCK.release()


def schedule(session: Session) -> bool:
    """把一次总结丢到后台线程，返回是否真的启动了。

    用线程而不是 `asyncio.Task`：`run_session` 里的 embedding 是同步 HTTP，放在
    事件循环里会把整台服务器的响应卡住几百毫秒到几秒。线程里跑，主循环一个字节
    都不受影响。

    `daemon=True`：这是一个可以随进程一起结束的任务。中途死掉的后果由
    `memory_sessions` 的写入时机兜住（见模块 docstring 的"崩溃后的幂等"）。
    """
    with _LOCK:
        if session.session_id in _INFLIGHT:
            logger.debug("会话 %s 已经在总结了，跳过", session.session_id[:8])
            return False
        _INFLIGHT.add(session.session_id)

    def _run() -> None:
        try:
            run_session(session)
            # 会话总结跑完之后才做观测/命题：两条链写不同的表，但**顺序是刻意的**
            # ——归纳要读 `edit_events`，而会话总结不写它；真正的原因是"先把便宜、
            # 一定有产出的那一段做完"，万一归纳卡住（模型慢），用户拿到的偏好条目
            # 已经落库了。
            induce_propositions(run_id=session.session_id, session_id=session.session_id)
        finally:
            with _LOCK:
                _INFLIGHT.discard(session.session_id)

    try:
        threading.Thread(
            target=_run, name=f"memory-summary-{session.session_id[:8]}", daemon=True,
        ).start()
    except RuntimeError as exc:  # pragma: no cover — 线程起不来只可能是资源耗尽
        with _LOCK:
            _INFLIGHT.discard(session.session_id)
        logger.warning("会话 %s 的总结没能启动：%s", session.session_id[:8], exc)
        return False
    return True


def maybe_summarize(*, exclude_run_id: str | None = None) -> bool:
    """给 `agent.router.stream_run` 的一行入口：判一次、起一个后台线程。

    放在**认领完这次运行之后**、返回流之前。位置不能更早（认领之前调用会看不到
    当前运行，`exclude_run_id` 就无从排除），也不能更晚（生成一开始就没人关心
    上一次会话了）。
    """
    session = due(exclude_run_id=exclude_run_id)
    if session is None:
        return False
    logger.info("上一个会话（%s，%d 次运行）已结束，开始总结",
                session.session_id[:8], len(session.runs))
    return schedule(session)


__all__ = [
    "GAP_MINUTES",
    "INDUCE_ENV",
    "INDUCE_ROUNDS",
    "MAX_EVENTS",
    "MAX_RUNS",
    "SUMMARY_SYSTEM",
    "Outcome",
    "Session",
    "SessionRun",
    "due",
    "induce_enabled",
    "induce_propositions",
    "maybe_summarize",
    "run_session",
    "schedule",
    "user_text",
]
