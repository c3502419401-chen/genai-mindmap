"""编辑日志的读写：一张表、两条读路径、一个写入口。

这一版**只记录、不总结**：后面那几步（LLM 总结 → DPO 微调）要几百条偏好对，
一天几次的生成攒够要几个月；但"改动集中在一两个具体问题上"（比如"80% 的改动
是把长标题改短"）跑一次统计就能看出来，而那种问题用规则一天就能修。

所以这个模块只服务一句话：**用户最常改什么**。形状（`EditEventIn` /
`EditSummary`）在 `app/contracts/edits.py`，那里写了四个取舍的来由。
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from typing import Mapping, Sequence

from app.contracts.edits import (
    EditEventIn,
    EditEventRow,
    EditHealth,
    EditSummary,
    LabelShrunk,
)
from app.contracts.mindmap import DiagramContent
from app.mindmaps import repository

logger = logging.getLogger(__name__)

#: 时间格式与 `mindmaps` / `runs` / `user_prefs` / `library_*` 完全一致。
#: 三处格式不同会有人"谁更准"上错一次——这是仓库里写过的教训（`agent/runs.py:_NOW`）。
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

#: `recent()` 的默认条数与上限。上限存在的理由与别处一样：这是给人读的一屏，
#: 不是导出通道（要导出该另开一条，而不是把 `limit` 放到一万）。
RECENT_DEFAULT = 50
RECENT_LIMIT = 500

#: `parent_id` 这一列后来才加（见 `ensure_schema`）。写成常量是因为它同时出现在
#: 三处 SQL 里（补列、INSERT、读），散着写迟早有一处拼错，而拼错的表现是
#: "这一列永远是 NULL"——与"没人加过节点"长得一模一样。
PARENT_COLUMN = "parent_id"

#: `by_field` 只统计这两种 `kind` 的行。`field` 本来就只在 `update` / `style` 上有
#: 意义（见 `contracts/edits.py` 文件头那条恒等式），而 `repair` 事件改文字时虽然
#: 也带 `field`，它**不是人写的**——混进"最常改哪个字段"会让规则自己改出来的
#: `label` 计入用户的偏好。与 `label_shrunk` 排除 `repair` 是同一条理由。
FIELD_KINDS = ("update", "style")

#: 被算作"节点改动"的 `kind` 集合。**`changed_events` 与 `untouched_runs` 共用它**：
#: 这两个集合必须逐字相同，否则会出现"既不算改动事件、也不算一字未改"的黑洞，而那
#: 正是 `changed_events == 0 ⇒ untouched_runs == generated_runs` 这条恒等式破掉的
#: 样子。原来两处各写一遍 `IN (...)`、靠一句注释守着"别改歪"，现在靠一个常量守着。
#:
#: `edge` 不在里面（分母数的是节点）；`repair` 在里面（一键修复确实删了/搬了/改了
#: 节点，不算进来，"模型给了 30 个节点、规则删掉 12 个"会被读成"一字未改"）。
CHANGE_KINDS = ("update", "remove", "add", "move", "style", "repair")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """补上 `edit_events` 后来才加的 `parent_id` 列。幂等。

    **为什么补列写在这里，而不是 `mindmaps/repository.py` 的 DDL 区**：那张表从
    建表到读写全在本模块（`grep edit_events` 只有本模块与仓储层的建表语句），
    所以"这张表后来加的列"归它的唯一使用方补齐，职责是收口不是散开。仓储层
    一个字节没动——它正在被别人改。

    发货形状与 `knowledge/library.py::ensure_schema` / `memory/store.py::ensure_schema`
    同一套（模块自持 DDL、幂等、由调用方在拿到连接后立刻调），那两处是既有先例；
    差别只在**它们建自己的表，这里补已有的表**——因为 `edit_events` 的建表语句在
    禁改的仓储层里，而这一列不能等。

    判据与 `repository._ensure_columns` 逐条一致（照抄，不重复发明）：

      - **只吞"列已经存在"**。`ALTER` 还可能因为表名拼错、声明非法而失败，那些
        必须继续抛——否则迁移出问题时会被静默跳过，而缺列的后果是"事件静默丢失"
        （INSERT 抛 "no such column" 被 `record` 自己吞掉，`total` 恒为 0，看着
        和"没人改过"一模一样）。
      - **不做数据回填**。老行上是 NULL，而 NULL 在这里有确切含义（"当时没记"），
        比编出来的空串诚实。

    SQLite 没有 `ADD COLUMN IF NOT EXISTS`，"先查再补"不是原子的，两个连接同时
    第一次打开时会各发一条 `ALTER`，后到的抛 `duplicate column name`。本地单线程
    撞不到，但"保存"这条路上并发量本来就是常态。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(edit_events)")}
    if PARENT_COLUMN in existing:
        return
    try:
        conn.execute(f"ALTER TABLE edit_events ADD COLUMN {PARENT_COLUMN} TEXT")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def parents_of(content: DiagramContent) -> dict[str, str | None]:
    """**保存那一刻**每个节点在树上的父节点 id；子树根是 `None`。

    这张表就是 `parent_id` 这一列的取值来源。它从**落库的那棵树**算，不从请求里
    读一个前端算好的值——两个理由：

      1. 前端算的那份要经过契约（`EditEventIn` 是 `extra="forbid"`，加字段就要
         重导 `openapi.json` 与 `schema.d.ts`，那是另一条线在改的东西）。而这里
         要回答的事实，在保存那一刻的树上是**唯一确定**的，不需要谁把它抄一遍；
      2. 抄一遍就可能抄错。树是权威，事件是描述，描述跟着权威算，不会出现
         "父 id 指向一个不存在的节点"这种要另写校验才拦得住的状态。

    口径与前端 `mindmap/edit-log.ts::indexForest` 逐条对齐：

      - 树只有一根（中心主题），**它是根**；
      - 鱼骨有六根骨、流程有若干阶段，**每一根本身都是自己的根**——它在画布上
        挂在主骨/阶段容器上，但那层挂载关系由 `FishboneBone` / `FlowStage` 表达，
        不是 `Node` 的父子关系（`agent/shapes.py::find_parent` 同一条判断）；
      - 同一个 id 出现两次（历史脏数据）时，**后写的覆盖先写的**，与前端
        `entries.set` 的行为一致，不另立规则。

    `agent/shapes.roots` 是三型统一的读侧适配层（"节点藏在哪儿"只有一份实现），
    这里复用它而不是再写一遍 `isinstance` 三分支：重复的那份迟早与它分叉，而分叉
    的表现是"鱼骨图的 add 事件没有父节点"。
    """
    from app.agent import shapes

    parents: dict[str, str | None] = {}
    for root in shapes.roots(content):
        parents[root.id] = None
        stack = [root]
        while stack:
            node = stack.pop()
            for child in node.children:
                parents[child.id] = node.id
                stack.append(child)
    return parents


def record(
    map_id: str,
    events: Sequence[EditEventIn],
    *,
    parents: Mapping[str, str | None] | None = None,
    owner: str = "",
) -> int:
    """把一批事件写进 `edit_events`，返回写入条数。

    单个事务、一条 `executemany`：一批事件是**一次保存**的产物，半批落库会让
    `by_kind` 出现"改了 20 处但只记了 7 条"这种无法解释的缺口。

    `parents` 是 `parents_of(content)` 的返回值：`node_id → 父 id`。缺省 `None`
    时该列全为 NULL（"这次没算"），而不是凭空猜一个——和 `subtree_size` 加列
    之前的老行同一个口径。

    列的语义只有一句：**保存那一刻 `node_id` 在树上的父节点**。所以：

      - `add` 有值 → "用户新加的节点挂在哪"，这是这一列存在的全部理由；
      - `remove` 的节点已经不在树上 → NULL（不是"删除前的父"，那要另记一列）；
      - `title` / `generate` 没有节点 → NULL；
      - `edge` 的 `node_id` 是边的源节点，它在树上，于是有值——如实记它，不因为
        "这条事件不改节点"就抹掉一个成立的事实。

    **失败不抛。** 调用方是保存接口，那时内容已经落库；为了统计写失败而让用户看到
    "保存失败"、进而重试撞 409，方向完全是错的。这里只记 warning——服务端日志里
    有 map_id 与条数，够定位。

    `owner` 是**谁做的这次改动**，与 `map_id`（哪张图）是两件事：图是共享的，
    但"他把标题改短了"这条观察只能进他自己的记忆总结。`owner` 由调用方显式给，
    不能在这里读 `current_identity()`——那条约定来自 `app/auth/owner.py`（后台线程
    读不到 ContextVar，而这条路径以后可能从线程里调）。
    """
    if not events:
        return 0
    index = parents or {}
    rows = [
        (map_id, event.run_id, owner, event.actor, event.kind, event.node_id, event.field,
         event.before, event.after, event.depth, event.subtree_size, event.change_count,
         event.node_count, index.get(event.node_id), event.at)
        for event in events
    ]
    try:
        with closing(repository.connect()) as conn, conn:
            ensure_schema(conn)
            conn.executemany(
                f"""INSERT INTO edit_events
                        (map_id, run_id, owner, actor, kind, node_id, field, before, after,
                         depth, subtree_size, change_count, node_count, {PARENT_COLUMN}, at,
                         recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, {_NOW}), {_NOW})""",
                rows,
            )
    except Exception:  # noqa: BLE001 — 统计写失败不该让用户的保存失败
        logger.warning("编辑事件写入失败：map_id=%s 条数=%d", map_id, len(rows), exc_info=True)
        return 0
    return len(rows)


def _count_by(conn, column: str, map_id: str,
              kinds: Sequence[str] | None = None) -> dict:
    """按一列分组计数。

    排序固定（先次数降序、再键升序）：同样的数据永远同样的输出，两次调用之间不会
    因为 SQLite 的扫描顺序而抖动——抖动的统计没法对比"这周和上周"。
    空值（比如 `field` 在 add/remove 上是 NULL）不进结果：那不是一"种"字段。

    `kinds` 是"只看这几种 kind 的行"。只有 `by_field` 传它（见 `FIELD_KINDS`）。
    """
    sql = (f"SELECT {column}, COUNT(*) FROM edit_events WHERE map_id = ? "
           f"GROUP BY {column} ORDER BY COUNT(*) DESC, {column}")
    params: tuple = (map_id,)
    if kinds:
        marks = ",".join("?" for _ in kinds)
        sql = (f"SELECT {column}, COUNT(*) FROM edit_events "
               f"WHERE map_id = ? AND kind IN ({marks}) "
               f"GROUP BY {column} ORDER BY COUNT(*) DESC, {column}")
        params = (map_id, *kinds)
    return {row[0]: row[1] for row in conn.execute(sql, params) if row[0] is not None}


def summary(map_id: str) -> EditSummary:
    """这张图的改动统计。**这是这一版真正的交付物。**

    `label_shrunk` 与 `removed_depth` / `removed_subtree` 是特意要的三个数，因为它们
    直接对应生成侧该改什么：

      - 标题普遍被缩短（**只算 user、且不算 `kind='repair'`**）→ 生成侧 16 字上限可能
        还是太长，或者提示词让它写得太长；
      - 零散删掉的集中在第 3 层 → 初次深度太深；
      - 删掉的是整支还是零散 → "整支被删"更像"这一支根本不该生成"，与"挖太深"是
        两件事，混在一起读会得出错的处方。

    **分母是 `kind='generate'` 的 `node_count` 之和**（候选层删掉之后的口径，见
    `contracts/edits.py`）。三个数一起读才成立：

      - `generated_nodes` = SUM(node_count)：模型一共交出多少个节点；
      - `changed_events`：这些图上发生了多少条节点改动（含用户手工改的）；
      - `untouched_runs`：关掉"改动事件"这一轴之后，有多少次生成一处都没被碰。

    只看前两个会得出"每个节点被改 0.8 次"这种看不出方向的数；加上第三个才知道
    "是普遍改了 0.8 次"还是"9 次一字未改、1 次改了 8 处"——这两种情况的处方完全
    相反。

    它们只说"发生了什么"，不说"为什么"——局限见 `docs/EDIT-LOG.md`。
    """
    with closing(repository.connect()) as conn:
        totals = conn.execute(
            "SELECT COUNT(*), MAX(recorded_at) FROM edit_events WHERE map_id = ?",
            (map_id,)).fetchone()
        total, last_event_at = totals[0], totals[1]
        by_actor = _count_by(conn, "actor", map_id)
        by_kind = _count_by(conn, "kind", map_id)
        by_field = _count_by(conn, "field", map_id, kinds=FIELD_KINDS)

        # 分母与生成次数。`COALESCE(SUM(...), 0)`：只有 generate 行而它们全都没有
        # `node_count`（加列之前的老行）时，`SUM` 是 NULL 而不是 0，直接进 JSON
        # 会变成一个 `null` 分母。
        generated = conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(node_count), 0) FROM edit_events
               WHERE map_id = ? AND kind = 'generate'""",
            (map_id,),
        ).fetchone()

        # 分子。集合见 `CHANGE_KINDS`（`edge` 不进：分母数的是节点，把"加了一条关系"
        # 算成节点改动，比例就再也读不出"每个节点被改了几次"）。
        changed_marks = ",".join("?" for _ in CHANGE_KINDS)
        changed_events = conn.execute(
            f"""SELECT COUNT(*) FROM edit_events
                WHERE map_id = ? AND kind IN ({changed_marks})""",
            (map_id, *CHANGE_KINDS),
        ).fetchone()[0]

        # 零改动次数。**与分子的 kind 集合是同一个常量**（见 `CHANGE_KINDS`：只要
        # 一边多认一种，就会出现"既不算改动事件、也不算一字未改"的黑洞，而那正是
        # `changed_events == 0 ⇒ untouched_runs == generated_runs` 这条恒等式破掉的样子）。
        # `g.run_id IS NOT NULL`：老数据里 generate 行可能没有 run_id
        # （NULL = NULL 在 SQL 里不成立，会被 `NOT EXISTS` 判成"没有改动事件"），
        # 那条数不是"一字未改"，是"当时没记"。
        untouched = conn.execute(
            f"""SELECT COUNT(*) FROM edit_events AS g
                WHERE g.map_id = ? AND g.kind = 'generate' AND g.run_id IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM edit_events AS e
                     WHERE e.map_id = g.map_id AND e.run_id = g.run_id
                       AND e.kind IN ({changed_marks})
                  )""",
            (map_id, *CHANGE_KINDS),
        ).fetchone()[0]

        # `length()` 对 TEXT 在 UTF-8 下按**字符**算，与前端 `charLength`（码点）
        # 同一口径。三处过滤各管一件事：
        #  - `field='label'`：`title`（导图标题）的改动是另一件事，混进来会让"节点
        #    标题被改短"这个信号被导图标题稀释；
        #  - `actor='user'`：`agent` 那批是"生成自己的改动记在模型名下"，性质完全
        #    不同。实测 B 图只有 3 条 user 的缩短，不过滤会读成 4 条。
        #  - `kind != 'repair'`：一键修复的 actor 也是 `user`（扳机是用户按的），而
        #    `SINGLE_CHILD` 那条修法**就是改文字**（"外层 / 内层"拼成一个，必然更短）。
        #    不过滤，"规则把两层标题拼成一个"会被算成"用户把标题改短"——而这个数字
        #    的唯一用途正是给生成侧定"标题该多长"，拿规则自己造成的缩短去论证它，
        #    是循环论证。
        shrunk = conn.execute(
            """SELECT COUNT(*), AVG(length(before)), AVG(length(after))
               FROM edit_events
               WHERE map_id = ? AND field = 'label' AND actor = 'user'
                 AND kind != 'repair'
                 AND before IS NOT NULL AND after IS NOT NULL
                 AND length(after) < length(before)""",
            (map_id,),
        ).fetchone()

        # **只统计零散删除。** 一条 remove 现在代表一整棵被删的子树，根所在的层不等于
        # "这一层的内容被嫌弃"；`subtree_size IS NULL` 的老数据同理（当时没记，无法
        # 判断它是零散还是整支），两者都不进这个分布——否则"每层都在被删"那次污染还在。
        #
        # `kind = 'remove'` 是**精确匹配**，所以一键修复删掉的那些节点（`kind='repair'`）
        # 自动不进这个分布，也不进下面的 `removed_subtree`。这正是要的：这个数回答的是
        # "用户嫌哪一层的节点多余"，规则删掉的（重名合并、单子链压缩）不是同一件事。
        # 代价：修复删了多少节点**没有专门口径**，只能到 `GET /edits` 的明细里按
        # `kind='repair'` + `subtree_size` 数（见 `docs/EDIT-LOG.md` 的局限那一段）。
        depths = conn.execute(
            """SELECT depth, COUNT(*) FROM edit_events
               WHERE map_id = ? AND kind = 'remove' AND depth IS NOT NULL
                 AND subtree_size = 1
               GROUP BY depth ORDER BY depth""",
            (map_id,),
        ).fetchall()

        sizes = conn.execute(
            """SELECT subtree_size, COUNT(*) FROM edit_events
               WHERE map_id = ? AND kind = 'remove'
               GROUP BY subtree_size""",
            (map_id,),
        ).fetchall()

    return EditSummary(
        total=total,
        last_event_at=last_event_at,
        by_actor=by_actor,
        by_kind=by_kind,
        by_field=by_field,
        label_shrunk=LabelShrunk(
            count=shrunk[0] or 0,
            avg_before=round(shrunk[1], 1) if shrunk[1] is not None else None,
            avg_after=round(shrunk[2], 1) if shrunk[2] is not None else None,
        ),
        removed_depth={row[0]: row[1] for row in depths},
        removed_subtree=_subtree_histogram(sizes),
        generated_runs=generated[0] or 0,
        generated_nodes=generated[1] or 0,
        changed_events=changed_events,
        untouched_runs=untouched,
    )


def _subtree_histogram(rows: Sequence[tuple[int | None, int]]) -> dict[str, int]:
    """`[(subtree_size, 次数)]` → `{"size_1": 5, "size_12": 1, "size_unknown": 2}`。

    排序固定（规模升序、`size_unknown` 最后）：同样的数据永远同样的输出，两次调用
    之间不会因为 SQLite 的扫描顺序而抖动——抖动的统计没法对比"这周和上周"。
    """
    ordered = sorted(rows, key=lambda row: (1, 0) if row[0] is None else (0, row[0]))
    return {
        "size_unknown" if row[0] is None else f"size_{row[0]}": row[1]
        for row in ordered
    }


def health() -> EditHealth:
    """跨图的链路健康检查：昨日编辑过的图数 vs 昨日保存的图数。

    判据只有一句：**"保存了但没有任何事件"在正常情况下不该发生**。每次生成完成都记
    一条 `generate`（连一字未改也记），所以 `saved > 0 且 edited == 0` 只能是链路断了。

    两个方向不对称，这很重要：链路真断时这个判据**一定**报警（事件收不到 → edited
    恒为 0）；反过来它只会漏报，不会因为"数据少"而误报成健康。唯一的误报窗口是跨
    零点那一次保存——`updated_at` 写在事件之前几毫秒，两者可能落进相邻两天，于是
    "昨天存了一张、事件记在今天"。代价是一天至多一次假警报，比不上为它引入一套时间
    窗口膨胀逻辑。

    `saved_maps` 是**下界**：`mindmaps` 只留最后一次保存时刻，昨天存过、今天又存过的
    图不在昨天的窗里。下界只会让报警更难触发（漏报），不会凭空报警。
    """
    with closing(repository.connect()) as conn:
        window_start, window_end = conn.execute(
            "SELECT date('now','-1 day') || 'T00:00:00.000Z', "
            "       date('now') || 'T00:00:00.000Z'").fetchone()
        edited = conn.execute(
            """SELECT COUNT(DISTINCT map_id) FROM edit_events
               WHERE recorded_at >= ? AND recorded_at < ?""",
            (window_start, window_end)).fetchone()[0]
        saved = conn.execute(
            "SELECT COUNT(*) FROM mindmaps WHERE updated_at >= ? AND updated_at < ?",
            (window_start, window_end)).fetchone()[0]

    suspect = saved > 0 and edited == 0
    if suspect:
        detail = (f"昨日保存了 {saved} 张图，却没有收到任何编辑事件——日志链路可能断了："
                  "前端在发、后端在丢，或事件根本没进请求体。")
    elif saved == 0:
        detail = (f"昨日没有保存（{edited} 张图有编辑事件）——没有保存时零事件是正常的，"
                  "判据不适用。")
    else:
        detail = f"昨日保存 {saved} 张图，其中 {edited} 张有编辑事件，链路正常。"

    return EditHealth(
        window_start=window_start,
        window_end=window_end,
        edited_maps=edited,
        saved_maps=saved,
        chain_suspect=suspect,
        detail=detail,
    )


def recent(map_id: str, limit: int = RECENT_DEFAULT) -> list[EditEventRow]:
    """最近的改动明细，新的在前。

    统计给的是分布；明细回答"那 42 条具体是什么"。没有它，一个反常的平均值只能连
    数据库才查得下去——而这张表存在的意义就是让人不连数据库也能看见。

    `at DESC, id ASC` 而不是 `id DESC`：同一批事件的 `at` **完全相同**（一次操作
    共享一个时刻，见前端 `collectEdits`），按 id 降序会把批内顺序整个翻过来，
    读起来像"先删了标题再改的标题"。跨批次仍然新的在前，因为那时 `at` 不同。
    """
    size = max(1, min(limit, RECENT_LIMIT))
    with closing(repository.connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM edit_events WHERE map_id = ? ORDER BY at DESC, id ASC LIMIT ?",
            (map_id, size),
        ).fetchall()
    return [
        EditEventRow(
            id=row["id"], map_id=row["map_id"], run_id=row["run_id"], actor=row["actor"],
            kind=row["kind"], node_id=row["node_id"], field=row["field"],
            before=row["before"], after=row["after"], depth=row["depth"],
            subtree_size=row["subtree_size"], change_count=row["change_count"],
            node_count=row["node_count"],
            at=row["at"], recorded_at=row["recorded_at"],
        )
        for row in rows
    ]


def parent_ids(
    map_id: str, *, kinds: Sequence[str] = ("add", "move"), limit: int = RECENT_LIMIT,
) -> list[tuple[str, str, str | None]]:
    """这张图上"用户把哪个节点放在了哪个父节点下面"的样本：`(kind, node_id, parent_id)`。

    **当前没有任何产品调用方。** 这句写在最前面，因为原来这里是另一句话：那一版说它
    "服务库内的读者（统计、`optimization/link.py` 的候选、`scripts/measure_link_placement.py`）"
    ——**那条已经过期了**。查实的引用分布是 `tests=6 / scripts=0`，而 `app` 内只有
    `repository.py` 的一条注释提到这个名字，`link.py` 从来没有 import 过它。留着过期
    注释比没有注释更坏：下一个人会以为这一列已经有读者了。

    ## 为什么当初以为 `link.py` 要读它，而实际上不要

    `link.py` 生成"该挂到谁下面"的候选有两条来源，都不碰这张表：

      - `parent_choices()` 读**当前**这棵树的 `_walk()`。候选父必须来自此刻的树——
        事件表里那些"曾经当过的父"可能是已经不在的节点；
      - `history_placements()` 读**其它图**的 `content` 全量遍历（`_walk()`），问的是
        "别人把这个概念放在哪"。全量树比事件表完整，也更实时。

    事件表这条路径**极度稀疏**：只有"用户手工 add / move 过的节点"才带 `parent_id`，
    而这份日志跑了几周之后 `add` 一直是 0 条（见 `tests/test_edit_parent.py` 文件头）。
    接上去等于用一个只覆盖手工动作的样本，替掉 link 现在能读到的全量树——建议质量会
    变差，不会变好。

    ## 那为什么留着，而不是删掉

    这一列**每次保存都在写**（`parents_of` 从保存那一刻的树上算）。删掉这个函数之后，
    这一列就只剩"写"和"测试直接查库"两件事，而测试里的直查是手段、不是读者。将来真
    要接（比如界面按父归组"哪些新节点挂在同一个父下"），得从零重写这段 SQL，而重写时
    最容易错的是下面那条 `IS NOT NULL` 的口径——它已经踩过一次。

    所以它现在的身份是**这一列唯一的具名读路径**，"零调用方"这件事登记在
    `docs/EDIT-LOG.md` 第七节，不靠 docstring 提醒。

    ## 接上它需要一次契约变更，本版不做

    `EditEventRow` 是 `GET /mindmaps/{id}/edits` 的响应模型，给它加字段就要重导
    `openapi.json` 与 `schema.d.ts`。而**界面显示"挂在谁下面"已经有更好的来源**：
    `move` 事件的 `before` / `after` 存的是父节点**标签**（`"主题 → 风险"`），能直接
    念出来；`parent_id` 给界面的是 id，还得再查一次树才可读。可读性更差、还要多一条
    契约变更——等真有"按父归组"这类需求再动。

    `kinds` 缺省只取 `add` 与 `move`——它们才是"放置动作"。`update` 的 `parent_id`
    是"改文字那一刻它在谁下面"，回答的是另一个问题（同一列，不同语义的**同一个
    事实**：父节点)。要看那个就传 `kinds=("update",)`。

    **没有 `parent_id` 的老样本不进结果**（`IS NOT NULL` 过滤）：它们不是"挂在根
    下面"，是"当时没记"。两者混在一起，"根下面有几个新节点"立刻就会虚高。
    """
    size = max(1, min(limit, RECENT_LIMIT))
    marks = ",".join("?" for _ in kinds)
    with closing(repository.connect()) as conn:
        ensure_schema(conn)
        rows = conn.execute(
            f"""SELECT kind, node_id, {PARENT_COLUMN} FROM edit_events
                WHERE map_id = ? AND kind IN ({marks}) AND node_id IS NOT NULL
                  AND {PARENT_COLUMN} IS NOT NULL
                ORDER BY at DESC, id ASC LIMIT ?""",
            (map_id, *kinds, size),
        ).fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


def clear(map_id: str | None = None) -> int:
    """清空（只在用例与本地调试用）。传 `map_id` 只清那一张。"""
    with closing(repository.connect()) as conn, conn:
        if map_id is None:
            return conn.execute("DELETE FROM edit_events").rowcount
        return conn.execute("DELETE FROM edit_events WHERE map_id = ?", (map_id,)).rowcount


__all__ = [
    "PARENT_COLUMN",
    "RECENT_DEFAULT",
    "RECENT_LIMIT",
    "clear",
    "ensure_schema",
    "health",
    "parent_ids",
    "parents_of",
    "recent",
    "record",
    "summary",
]
