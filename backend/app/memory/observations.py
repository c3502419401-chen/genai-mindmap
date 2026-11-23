"""观测（observation）：带时间戳的一次具体动作，不做解释。

Coco 论文（arXiv:2611.13457）的两级压缩里，第一级是"观察"：一次具体事件，只记
发生了什么、什么时候发生，**不记为什么**。它原始形态是"桌面截图 + 键鼠流"。这个
项目没有截图流，但有一层等价物：**结构化事件**——`runs`（他提了一次要求）与
`edit_events`（他保存了一次改动）。这两张表的行都是"发生了，带时刻，不含解释"，
与论文的观测同构。缺的只是"哪几行算一次观测"这条判定，本节就是它。

## 一次观测是什么（定义与粒度依据）

**定义**：一次观测 = 一个动作边界上的全部行，由下面两条判定给出：

  1. **run 观测**：`runs` 的一行。
     依据：run 有唯一 `run_id`、有明确起止（`created_at` / `updated_at`）、有
     `request`（goal + intent）。用户的"一次要求"在库里只表达一次（一个 goal），
     不可再分——把它切成"骨架那次调用 / 结构那次调用"是切到实现细节上去了，
     那些调用对用户不可见，也不构成一次动作。

  2. **edit 观测**：同一 `map_id` 上 `at` 相同的全部 `edit_events`。
     依据：`at` 是**前端在"一次保存"时统一打上的动作时刻**，同一批改动共享它。
     实测这份库的 565 条事件只有 **4 个不同的 `at`**，而 `recorded_at` 有 16 个
     ——入库时刻被网络与批处理打散（12:23:34.567 / .569 / .571），只有 `at` 保持
     了"这一次动作"的边界。所以切分键必须是 `at`，不是 `recorded_at`。

     反过来说：`recorded_at` 也不能用作"时间相近"的判据。仓库别处用它（
     `summarize._events` 的窗口）是因为那里要的是"这段时间内入库了什么"，而这里
     要的是"这是几次动作"。

## 为什么不是"每条 edit_event 一次观测"

那份库里 550 条 `update` 事件共享同一个 `at`、覆盖 550 个不同节点、全是 `label`
改动。按行算就是 550 条观测，内容雷同、互相之间没有新信息——它会在归纳时把
一条命题的支撑数灌到 550，让"支撑数"这个数失去意义（论文里它是置信度来源）。
按动作算就是 **1 条观测，`stat=550`**：粒度对了，量也在。

## 为什么不是"一批连续编辑（同 map、时间相近）"

"时间相近"需要一个阈值，而阈值在这个项目里没有独立依据：两次保存之间的间隔
实测从 0 毫秒（同一次批量写入）到 107 秒（两次操作）都出现过。用 `at` 相等来切
不需要阈值——**同一次保存的时刻由前端给，是一个事实，不是一个需要猜的参数**。
需要"跨保存合并"的场合（一次会话里的连续微调）已经由 `memory_sessions` 那一层
负责，见 `summarize.GAP_MINUTES`；在观测这一层重复做一次只会有两个不一致的定义。

## ChatDock 的提问算不算

算，而且不需要第三类：ChatDock 的每次提问都落成一次 run（`runs.request.goal`
就是那句话）。所以"run 观测"已经覆盖它。查过 `frontend/src/components/ChatDock.vue`
——它没有独立于 run 的后端入口。

## 观测必须能回指（这是这张表存在的理由）

`refs` 列存**原始行的主键**：run 观测是 `{"run_ids": [...]}`，edit 观测是
`{"event_ids": [...]}`。命题的 `observation_ids` 指向本表，本表的 `refs` 指向
runs / edit_events 的具体行——三段链，每一段都是主键，任给一条命题都能列出它
引用的每一个原始事件。这条链是"命题不是自由摘要"的机器可查证据，也是
`tests/test_memory_observation.py` 里那条回指用例守护的东西。

## 这张表与 `memory_sessions` 的分工

`memory_sessions` 记的是"这段会话总结过没有"（一次会话 = 多 run 的一段工作，
产出 preference/fact/pattern）。本表记的是**动作级事实**，粒度比会话细、比单条
事件粗。两者不互相替代：会话总结问"这一小时他表现出什么偏好"，观测问"他做了
哪几件事、按什么顺序"。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Iterable, Sequence
from uuid import uuid4

from app.memory import store
from app.mindmaps import repository

logger = logging.getLogger(__name__)

TABLE = store.OBSERVATION_TABLE

#: 两类观测。值直接进库、进模型输入，改名等于改契约。
OBSERVATION_RUN = "run"
OBSERVATION_EDIT = "edit"
KINDS: tuple[str, ...] = (OBSERVATION_RUN, OBSERVATION_EDIT)

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

#: 一次抽取从最新往回看多少行 / 多少个批次。
#:
#: 偏置与 `store.MAX_SCAN` 同一种，也写在这里：**超过这个数时更旧的这次不进来**。
#: 增量是常态（每次 run 开始时抽一次，新数据总在最新端），所以偏置只在
#: "第一次抽一个很老的库"或"库长期没人抽"时才生效；两种情况都有一个 warning 说明。
CAPTURE_RUNS = 500
CAPTURE_BATCHES = 500

#: 一条改动在 `payload` 里的正文上限。观测不搬运原文——它记的是"哪一类改动"，
#: 一整段被改的正文既进不了模型输入（几十条就撑爆），也不是这条观测的语义。
MAX_TEXT = 120

#: 一个 edit 观测的 `payload` 里最多留几种改动。550 条事件折叠后是 54 种，超过
#: 这个数时留最前面的（折叠按首次出现排序 = 按发生顺序），并记 `omitted`。
MAX_CHANGES = 20

#: 一个 edit 观测的 `refs` 里最多留多少个原始事件 id。550 条那一批的 id 数组约
#: 2 KB，正常；但一次批量操作可能上万条，而 `refs` 的作用是"能回指"，不是
#: "把明细复制一遍"。截断时 `refs.truncated` 置真，让人知道这里少了一截。
MAX_REFS = 2000

#: run 观测里 goal 的显示上限。与 `summarize.MAX_GOAL_CHARS` 同量级：观测是索引，
#: 不是正文。
MAX_GOAL_CHARS = 80

_RUNS_SQL = """
    SELECT run_id, status, request, created_at, updated_at, applied_at, mode
    FROM runs
    ORDER BY created_at DESC, run_id DESC
    LIMIT ?
"""

_BATCHES_SQL = """
    SELECT map_id, at, COUNT(*) AS n, MIN(recorded_at) AS recorded_at
    FROM edit_events
    GROUP BY map_id, at
    ORDER BY at DESC
    LIMIT ?
"""

_BATCH_ROWS_SQL = """
    SELECT id, kind, actor, field, before, after, depth, node_id
    FROM edit_events
    WHERE map_id = ? AND at = ?
    ORDER BY id
"""


# ---------------------------------------------------------------- 形状

@dataclass(frozen=True)
class Observation:
    """一条观测。`payload` / `refs` 是已解析的 JSON；`text` 供检索与模型输入用。"""

    id: str
    kind: str
    source_key: str
    at: str
    summary: str
    payload: dict[str, Any]
    refs: dict[str, Any]
    stat: int
    map_id: str | None = None
    run_id: str | None = None
    consumed_at: str | None = None
    captured_at: str | None = None

    @property
    def text(self) -> str:
        return self.summary

    @property
    def event_ids(self) -> list[int]:
        return list(self.refs.get("event_ids") or [])

    @property
    def run_ids(self) -> list[str]:
        return list(self.refs.get("run_ids") or [])


@dataclass(frozen=True)
class Captured:
    """一次抽取的结果：新增多少条、跳过了多少条（已存在）。"""

    runs: int = 0
    edits: int = 0

    @property
    def total(self) -> int:
        return self.runs + self.edits


def _loads(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _row(row: sqlite3.Row) -> Observation:
    return Observation(
        id=row["id"],
        kind=row["kind"],
        source_key=row["source_key"],
        at=row["at"],
        summary=row["summary"],
        payload=_loads(row["payload"]),
        refs=_loads(row["refs"]),
        stat=row["stat"],
        map_id=row["map_id"],
        run_id=row["run_id"],
        consumed_at=row["consumed_at"],
        captured_at=row["captured_at"],
    )


def clip(text: object, limit: int = MAX_TEXT) -> str:
    """压成一行并截断。观测里的文字一律这么处理：不换行、不搬运原文。"""
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit] + "…"


def _connect() -> sqlite3.Connection:
    return repository.connect()


# ---------------------------------------------------------------- 抽取

def _request(row: sqlite3.Row) -> dict[str, Any]:
    value = _loads(row["request"])
    return value if value else {}


def _run_observation(row: sqlite3.Row) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """一行 `runs` → (source_key, summary, payload, refs)。

    summary 只写**事实**：意图、目标原文（截断）、终态、有没有被保存。不写"他要
    一个鱼骨图"这种读法——那是命题那一层的事。这条边界是两级压缩能成立的前提：
    第一级一旦带上解释，第二级就没有可压缩的东西了。
    """
    request = _request(row)
    intent = str(request.get("intent") or "create")
    goal = str(request.get("goal") or "").strip()
    status = str(row["status"] or "")
    applied = row["applied_at"] is not None
    diagram = request.get("diagram_type")
    prefs = [item for item in request.get("prefs_applied") or [] if isinstance(item, str)]

    head = f"[{intent}]「{clip(goal, MAX_GOAL_CHARS)}」" if goal else f"[{intent}]（无目标文本）"
    tail = f"→ {status}"
    if status == "done":
        tail += "（已保存）" if applied else "（未保存）"
    summary = head + tail
    if isinstance(diagram, str) and diagram:
        summary += f" 图型 {diagram}"

    start = store.parse_time(row["created_at"])
    end = store.parse_time(row["updated_at"])
    duration = (end - start).total_seconds() if start and end else None
    payload = {
        "goal": clip(goal, MAX_GOAL_CHARS),
        "intent": intent,
        "status": status,
        "diagram_type": diagram if isinstance(diagram, str) else None,
        "prefs_applied": prefs,
        "applied": applied,
        "mode": row["mode"],
        "duration_s": round(duration, 1) if duration is not None else None,
    }
    return f"run:{row['run_id']}", summary, payload, {"run_ids": [row["run_id"]]}


def _fold(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    """把一批改动折叠成"哪几类、各几处"，按首次出现排序。

    与 `summarize._collapse_events` 的区别：那里只折叠**连续**相同的（`A,B,A` 是
    三件事），这里按 `(kind, actor, field, before, after)` **全批**折叠。理由是
    观测不承担"顺序"语义——顺序由 `at` 与 `id` 承担，重复的同类改动在这里只是
    "这一次他一共改了 550 处 label"，数清比排定更有用。
    """
    folded: list[dict[str, Any]] = []
    index: dict[tuple[Any, ...], int] = {}
    for row in rows:
        key = (row["kind"], row["actor"], row["field"], row["before"], row["after"])
        if key in index:
            folded[index[key]]["n"] += 1
            continue
        index[key] = len(folded)
        folded.append({
            "kind": row["kind"],
            "actor": row["actor"],
            "field": row["field"],
            "before": clip(row["before"]),
            "after": clip(row["after"]),
            "n": 1,
        })
    return folded


def _edit_observation(
    map_id: str, at: str, rows: Sequence[sqlite3.Row],
    *, recorded_at: str | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """一批改动 → (source_key, summary, payload, refs)。"""
    folded = _fold(rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    actors = sorted({str(row["actor"]) for row in rows})
    who = "他" if actors == ["user"] else "模型" if actors == ["agent"] else "他+模型"
    breakdown = "、".join(
        f"{kind}×{n}" for kind, n in
        sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )
    summary = (
        f"一次保存（{who}，{len(rows)} 处）：{breakdown}"
        f"，折叠后 {len(folded)} 种"
    )
    event_ids = [int(row["id"]) for row in rows]
    truncated = len(event_ids) > MAX_REFS
    payload = {
        "events": len(rows),
        "kinds": counts,
        "actors": actors,
        "changes": folded[:MAX_CHANGES],
        "omitted": max(0, len(folded) - MAX_CHANGES),
        # 入库时刻与动作时刻分开存：两者相差很大时（网络重放、批量补写），
        # "这一次保存是什么时候真的到服务端的"是排查用的证据，不是观测的时序。
        "recorded_at": recorded_at,
    }
    refs: dict[str, Any] = {"event_ids": event_ids[:MAX_REFS]}
    if truncated:
        refs["truncated"] = True
        logger.warning("edit 观测 %s:%s 有 %d 条明细，refs 只留前 %d 条",
                       map_id[:8], at, len(event_ids), MAX_REFS)
    return f"edit:{map_id}:{at}", summary, payload, refs


def _insert(conn: sqlite3.Connection, kind: str, source_key: str, at: str, summary: str,
            payload: dict[str, Any], refs: dict[str, Any], *, map_id: str | None = None,
            run_id: str | None = None, stat: int = 1) -> bool:
    """写一条观测。已存在（`source_key` 命中）时返回 False，不覆盖。

    用 `ON CONFLICT DO NOTHING` 而不是 `INSERT OR IGNORE`：后者连 NOT NULL 违规
    一起吞掉，于是"抽取逻辑写坏了一条"会变成静默少一条观测。
    """
    result = conn.execute(
        f"INSERT INTO {TABLE} (id, kind, source_key, map_id, run_id, at, summary, "
        f"payload, refs, stat, captured_at) "
        f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {_NOW}) "
        f"ON CONFLICT(source_key) DO NOTHING",
        (uuid4().hex, kind, source_key, map_id, run_id, at, summary,
         json.dumps(payload, ensure_ascii=False), json.dumps(refs, ensure_ascii=False), stat),
    )
    return result.rowcount == 1


def capture(*, runs: int = CAPTURE_RUNS, batches: int = CAPTURE_BATCHES) -> Captured:
    """从 `runs` 与 `edit_events` 抽出还没抽过的观测。幂等、可重复调。

    抽取是**增量**的：每次从最新往回扫有限条，已存在的靠 `source_key` 跳过。所以
    这个函数可以在每次 run 开始时无条件调用一次（`summarize` 那条链就是这么做的），
    空库上它只花两次 SELECT。

    一个已知的偏置：超过 `CAPTURE_RUNS` / `CAPTURE_BATCHES` 的更旧事件这次不进观测。
    对于"每次都抽"的常态没有影响（新数据总在最新端）；对"第一次抽一个很老的库"
    有影响，所以超限时记 info，让人知道这次只覆盖了最近的一段。
    """
    created_runs = created_edits = 0
    with closing(_connect()) as conn, conn:
        run_rows = conn.execute(_RUNS_SQL, (max(1, runs),)).fetchall()
        if len(run_rows) == max(1, runs):
            logger.info("观测抽取：runs 取满了最近 %d 行，更旧的不在这次范围", runs)
        for row in run_rows:
            source_key, summary, payload, refs = _run_observation(row)
            if _insert(conn, OBSERVATION_RUN, source_key, row["created_at"], summary,
                       payload, refs, run_id=row["run_id"], stat=1):
                created_runs += 1

        batch_rows = conn.execute(_BATCHES_SQL, (max(1, batches),)).fetchall()
        if len(batch_rows) == max(1, batches):
            logger.info("观测抽取：edit 批次取满了最近 %d 个，更旧的不在这次范围", batches)
        for batch in batch_rows:
            map_id, at = batch["map_id"], batch["at"]
            if not map_id or not at:
                # 两列都是 NOT NULL，但老库里的行可能是手工造的。缺任一项就切不出
                # "哪张图上的哪一次保存"，跳过并留痕，不猜。
                logger.warning("跳过一条切不出边界的编辑批次：map_id=%r at=%r", map_id, at)
                continue
            detail = conn.execute(_BATCH_ROWS_SQL, (map_id, at)).fetchall()
            if not detail:
                continue
            source_key, summary, payload, refs = _edit_observation(
                map_id, at, detail, recorded_at=batch["recorded_at"])
            if _insert(conn, OBSERVATION_EDIT, source_key, at, summary, payload, refs,
                       map_id=map_id, stat=len(detail)):
                created_edits += 1
    if created_runs or created_edits:
        logger.info("观测抽取：新增 run %d 条、edit %d 条", created_runs, created_edits)
    return Captured(runs=created_runs, edits=created_edits)


# ---------------------------------------------------------------- 读

def count() -> int:
    with closing(_connect()) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])


def recent(limit: int = 8) -> list[Observation]:
    """最近若干条观测，新的在前。**这是双路检索里的"时间路"**。

    排序按 `at DESC, id DESC`：`at` 是动作发生时刻（硬序），`id` 只为同刻的稳定序
    ——同一毫秒的两条观测不能因为两次查询的扫描顺序不同而换位。
    """
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT * FROM {TABLE} ORDER BY at DESC, id DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
    return [_row(row) for row in rows]


def unconsumed(limit: int = 40) -> list[Observation]:
    """还没被归纳用过的观测，按时间正序。

    **正序**（最旧的在前）而不是倒序：归纳要给出一句"他这段时间怎么做"的判断，
    顺序是它的一部分（先要骨架、再补细节）。倒着喂会让"后来改回来"读成"他喜欢
    改回来"。
    """
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT * FROM {TABLE} WHERE consumed_at IS NULL ORDER BY at, id LIMIT ?",
            (max(1, limit),),
        ).fetchall()
    return [_row(row) for row in rows]


def get(ids: Iterable[str]) -> list[Observation]:
    """按 id 取。顺序与传入一致；取不到的直接不出现（调用方自己判缺失）。"""
    wanted = [str(item) for item in ids if item]
    if not wanted:
        return []
    marks = ",".join("?" * len(wanted))
    with closing(_connect()) as conn:
        rows = conn.execute(f"SELECT * FROM {TABLE} WHERE id IN ({marks})", wanted).fetchall()
    found = {row["id"]: _row(row) for row in rows}
    return [found[item] for item in wanted if item in found]


def resolve_refs(ids: Iterable[str]) -> dict[str, Any]:
    """一批观测 id → 它们能回指到的原始行。

    这是"命题不是自由摘要"的**可执行检查**：返回 `{"run_ids": [...], "event_ids": [...]}`，
    调用方（HTTP 面、脚本、用例）可以直接拿它去 `runs` / `edit_events` 里核对。
    缺失的观测 id 单独列在 `missing` 里——静默丢弃会让"回指断了"看起来像"没引用"。
    """
    wanted = [str(item) for item in ids if item]
    found = {item.id: item for item in get(wanted)}
    run_ids: list[str] = []
    event_ids: list[int] = []
    missing: list[str] = []
    for item in wanted:
        observation = found.get(item)
        if observation is None:
            missing.append(item)
            continue
        run_ids.extend(observation.run_ids)
        event_ids.extend(observation.event_ids)
    return {"run_ids": run_ids, "event_ids": event_ids, "missing": missing}


def mark_consumed(ids: Iterable[str]) -> int:
    """把这些观测标记成"已经被归纳用过"，返回改了几行。

    **只在归纳成功之后调**。失败就不标记，下一段观测进来时重试一次——重试的代价
    是一次模型调用，不重试的代价是这段证据永远进不了命题（比多花一次钱差）。
    """
    wanted = [str(item) for item in ids if item]
    if not wanted:
        return 0
    marks = ",".join("?" * len(wanted))
    with closing(_connect()) as conn, conn:
        result = conn.execute(
            f"UPDATE {TABLE} SET consumed_at = {_NOW} "
            f"WHERE id IN ({marks}) AND consumed_at IS NULL",
            wanted,
        )
    return result.rowcount


__all__ = [
    "CAPTURE_BATCHES",
    "CAPTURE_RUNS",
    "KINDS",
    "MAX_CHANGES",
    "MAX_REFS",
    "MAX_TEXT",
    "OBSERVATION_EDIT",
    "OBSERVATION_RUN",
    "TABLE",
    "Captured",
    "Observation",
    "capture",
    "clip",
    "count",
    "get",
    "mark_consumed",
    "recent",
    "resolve_refs",
    "unconsumed",
]
