"""本地 SQLite 仓储；生产环境 PostgreSQL 仓储尚待实现。"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Sequence
from uuid import uuid4
from app.contracts.mindmap import DiagramContent, MapRecord, migrate_content

logger = logging.getLogger(__name__)


def db_path() -> Path:
    configured = os.getenv("MINDMAP_DB_PATH", "").strip()
    return Path(configured) if configured else Path(__file__).resolve().parents[2] / "data" / "mindmaps.db"


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """给已有的表补上后来才加的列。幂等。

    `CREATE TABLE IF NOT EXISTS` 只保证"表存在"，不保证"列齐全"：表一旦建过，后面往
    DDL 里加多少列都不会生效。这里显式补，是因为那张表已经有几百行真实数据，而缺列的
    后果不是报错，是 `edits.record()` 的 INSERT 抛错被自己吞掉——**事件全部静默丢失**。

    不做数据回填：新列在老行上是 NULL，而 NULL 在这里有确切含义（"当时没记"），
    比一个编出来的 0 诚实。

    **`PRAGMA` 与 `ALTER` 之间有一个竞态**，而 SQLite 没有 `ADD COLUMN IF NOT EXISTS`：
    "先查再补"不是原子的，两个连接同时第一次打开一个新库，都读到"列不存在"，于是各发
    一条 `ALTER TABLE ADD COLUMN`，后到的那条抛 `duplicate column name`。

    本地单线程跑不出来，但两处线上/CI 都会撞到它：`test_agent_runs` 那条"八个并发
    `begin()`"在 Linux 上必红（CI 就是这么红的），而线上是 `/runs/stream` 的八路并发
    认领——它本来就靠写锁串行，不该被一个建列动作带崩。

    所以这里 catch 住"列已经存在"这一种失败并当作成功 —— 它要达成的事已经达成了；
    不吞的话，第一个访问新库的并发请求会莫名其妙失败一次。
    **只吞这一个错误码**：`ALTER` 还可能因为表名拼错、声明非法而失败，那些要继续抛，
    否则迁移出问题时会被静默跳过，而缺列的后果是"事件全部静默丢失"（见上）。

    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, declaration in columns.items():
        if name in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def _drop_columns(conn: sqlite3.Connection, table: str, columns: Sequence[str]) -> None:
    """删掉不再有语义的列。幂等，且**失败只记 warning**。

    与 `_ensure_columns` 对称：`CREATE TABLE IF NOT EXISTS` 同样不会替老库删列。
    但删列比补列危险得多（`DROP COLUMN` 要 SQLite ≥ 3.35，而且会重写整张表），
    所以这里逐列 try：删不掉就留着那一列——它在代码里已经没有读者，一列恒为 NULL
    的文本字段既不占空间也不影响任何查询。**没删掉不是故障，代码不读它才是。**
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name in columns:
        if name not in existing:
            continue
        try:
            conn.execute(f'ALTER TABLE {table} DROP COLUMN "{name}"')
        except sqlite3.Error:
            logger.warning("%s.%s 应该删掉但没删成，代码已不再读写它", table, name)


def connect(*, timeout: float = 10) -> sqlite3.Connection:
    """开一条连接。`timeout` 是等写锁的上限，默认 10 秒。

    写成形参是为了让"在事件循环上等锁"的那条路能自己调短：生成中途落检查点的
    写库跑在线程池里，它等锁的每一秒都是这一轮生成在等，而检查点本身是"存不上
    就算了"的东西——它不值得陪一把慢锁耗满默认的十秒。
    """
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS mindmaps (
        id TEXT PRIMARY KEY, version INTEGER NOT NULL,
        content TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    # `runs` 是 agent 运行的身份行（见 `app/agent/runs.py`）。它必须与 `mindmaps`
    # 同一时刻建好：那张表的写入方是 SSE 生成器的 `finally`，而那条路上**不允许**
    # 再有一次"发现表不存在 → 建表"的分支——被取消时多一个可能抛错的分支，就等于
    # 把唯一要落库的草稿丢掉。
    #
    # 两列时间戳各管一件事，别合并：`updated_at` 是这一行最后一次状态变化（提示里
    # 那个"12:03"用它），`applied_at` 是这次生成的结果**真的被保存了**。
    #
    # `applied_at` 曾经的意思是"候选进了画布"（用户点了应用）。候选层删掉之后，
    # 生成结果直接落画布，那一刻没有用户动作可记；真正有区分度、也是 `pending()`
    # 唯一读者要的那个信号变成了"这份结果后来有没有被存下来"——生成完关掉页面
    # 却不保存的那一次，才是下次进页面该被捡回来的那一次。
    #
    # `dismissed_at` 已删：没有"丢弃候选"这个动作了。留下它只会让"这两个时间戳
    # 都为空 = 还没处理"这类判据永远带着一个死分支。
    conn.execute("""CREATE TABLE IF NOT EXISTS runs (
        run_id       TEXT PRIMARY KEY,
        status       TEXT NOT NULL,
        request      TEXT,
        candidate    TEXT,
        failure      TEXT,
        mode         TEXT,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        applied_at   TEXT
    )""")
    _drop_columns(conn, "runs", ["dismissed_at"])
    # 收尾那两个字段（`model` / `reasoning`）后来才加：`CREATE TABLE IF NOT EXISTS`
    # 不会给已有的表补列，而线上那张表已经有行。缺列时 `runs.finish()` 的 UPDATE
    # 会抛 `no such column`，被它自己的日志吞掉——**每次运行的终态都不落库**，
    # 而 `pending()` 只是少返回几条，看着像"没人生成过"。
    #
    # 两列都可空：老行上是 NULL，意思是"当时没记"（那时 done 帧也没有这两个字段），
    # 比编一个空串诚实——重放时据此决定"这条帧不带 model/reasoning"。
    _ensure_columns(conn, "runs", {"model": "TEXT", "reasoning": "TEXT"})
    # `served_model` 是第三个观察列（服务端回报的模型名）。理由同上：缺列时
    # `runs.finish()` 的 UPDATE 抛 `no such column` 并被自己的日志吞掉，
    # 结果是**每次运行的终态都不落库**，而现象只是 `pending()` 少返回几条。
    _ensure_columns(conn, "runs", {"served_model": "TEXT"})
    # "这棵树没长完"（预算耗尽、工具超时、连续失败、空转收尾）。理由同上：表早就
    # 建过，补列只能靠 `_ensure_columns`。
    #
    # 它必须落库而不是只在 `done` 帧里活着：同一个 run_id 第二次投递时走
    # `_replay`，从库里读出来重放——不存这一位，重放出来的那一帧就成了"完整生成"，
    # 而用户重连之前看到的是"部分完成"。同一个 run 两种结论比缺一个字段更糟。
    #
    # 用 INTEGER 而不是 TEXT：SQLite 没有布尔类型，`0/1` 与 `IS NULL` 三态
    # （NULL = 老行，当时还没这个字段）分得开。
    #
    # `partial_reason` 是给人看的那句话（"模型调用次数已用完（已执行 6 次工具调用，
    # 树上是 41 个节点）…"），与 `failure` 分开：`failure` 描述"这次失败了"，
    # 而半棵树是**成功交付但内容不全**，混进同一个字段会让界面只能二选一。
    _ensure_columns(conn, "runs", {"partial": "INTEGER", "partial_reason": "TEXT"})
    # 这一行里的 `candidate` 是**跑到一半截下来的检查点**，不是收尾交出的成品。
    #
    # 补这一列而不是新建一张表，是因为检查点与终态结果是同一种东西（一棵合法的
    # `DiagramContent`），区别只在"什么时候写的"。分成两张表，读的地方就要先问
    # "成品在不在"，不在再去另一张表拼——而拼出来的树与成品树是同一种形状，分开
    # 存只是把一次读变成两次读。`candidate` 本来就是"这次运行的那棵树"，检查点
    # 只是它更早的一份快照。
    #
    # 写这一列的那一刻顺手刷新 `updated_at`，`sweep_abandoned()` 的超龄判据就靠它：
    # 活着的运行最长的静默期是结构那一次调用，一定短于 `RUN_ABANDONED_SECONDS`。
    _ensure_columns(conn, "runs", {"checkpointed_at": "TEXT"})
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, updated_at)")
    # 用户偏好（`app/prefs/service.py`）。与上面两张表建在同一时刻、同一个连接上，
    # 理由同 `runs`：读它的两条路径（生成前的预填、`_system_text` 的注入）都不该
    # 再有一次"发现表不存在 → 建表"的分支。
    #
    # `pref_key` 是主键，这是整个设计的关键：**同一件事只能有一条**。"4 层"与
    # "6 层"不会两条并存互相矛盾——写就是 upsert，后写的赢。自由文本做不到这件事。
    #
    # `scope` 现在恒为 `'global'`，留着它不是预留"会话/工作区"——那两层在我们的
    # 结构里根本不是偏好（会话是 `runs` + `snapshot`，工作区是 `mindmaps.content`，
    # 见 `docs/RESEARCH-AGENT-MEMORY.md`）。它存在的唯一理由是让"这条偏好属于哪
    # 一层"在库里有据可查，而不是靠读代码猜。
    #
    # `source` 只有两个值：`user_explicit`（用户自己设的）与 `suggestion_accepted`
    # （系统从历史运行里提了候选、用户点了同意）。**没有第三个**：模型没有写记忆的
    # 权限，也没有"系统自动学到的"这一类。
    conn.execute("""CREATE TABLE IF NOT EXISTS user_prefs (
        pref_key      TEXT PRIMARY KEY,
        pref_value    TEXT NOT NULL,
        scope         TEXT NOT NULL DEFAULT 'global',
        source        TEXT NOT NULL,
        source_run_id TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )""")
    # 编辑事件（`app/mindmaps/edits.py`）。与上面三张表建在同一时刻、同一个连接上，
    # 理由同 `runs` / `user_prefs`：读它的路径（统计接口）不该再有"发现表不存在 →
    # 建表"的分支。
    #
    # 这张表只回答一个问题：**用户最常改什么**。所以每一列都为统计服务，而
    # `before` / `after` 是**摘要**不是内容（`detail` 只存 `"1800字"`）：一份 2000 字
    # 的正文进这张表，它就变成了第二个 `mindmaps.content`。
    #
    # `depth` 是节点层数（根=1）。`removed_depth` 只对 `subtree_size = 1` 的零散删除
    # 分组——"删掉的集中在第 3 层"就是"初次生成挖得太深"的证据，而一整支被删时根所在
    # 的层并不代表"这一层被嫌弃"。
    #
    # `subtree_size` 是这条 remove 一共删掉几个节点（含自己）。一条事件代表**一整棵**
    # 被删的子树：每个后代各记一条会让 `removed_depth` 在 2…5 层各留几条，看起来
    # "每层都在被删"，而真实动作只有一个。
    #
    # `change_count` 是 `kind='accept'` 那条事件的载荷（历史列，候选层删掉之后不再
    # 写入）：那次接受带来几处改动，0 = 一字未改。**与 `after` 分列**：`after` 是文本
    # 摘要，把计数混进去，`CAST(after AS INTEGER)` 会对每条文字摘要静默返回 0——一个
    # 看起来合理、实际全错的聚合。
    #
    # `node_count` 是 `kind='generate'` 那条事件的载荷，也是**统计的分母**：这次生成
    # 落进画布的那棵树有几个节点。它与 `at` / `recorded_at` 一样属于"发生那一刻的
    # 事实"，保存时不再重算——用户改完之后树上的节点数已经变了。
    #
    # `at` 由前端给（事件发生的时刻），`recorded_at` 是入库时刻。分开是为了能诊断
    # "事件说它 09:31 发生、实际 09:35 才入库"——前端的时钟不可信。
    #
    # 不做外键到 `mindmaps`：`map_id` 是调用方给的字面量，加外键只会在"导图被删"
    # （现在还没有删除接口）时多一个级联分支；而统计按 `map_id` 过滤本来就不依赖它。
    conn.execute("""CREATE TABLE IF NOT EXISTS edit_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        map_id      TEXT NOT NULL,
        run_id      TEXT,
        actor       TEXT NOT NULL,
        kind        TEXT NOT NULL,
        node_id     TEXT,
        field       TEXT,
        before      TEXT,
        after       TEXT,
        depth       INTEGER,
        subtree_size INTEGER,
        change_count INTEGER,
        node_count  INTEGER,
        at          TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )""")
    # `CREATE TABLE IF NOT EXISTS` 不会给**已经存在**的表补列，而线上那张表早就有
    # 565 行了。少了这一步，`edits.record()` 的 INSERT 会因为"no such column"抛错，
    # 被它自己的 `except` 吞掉 → **所有事件静默不落库**，正好是这张表最擅长的那种坏法
    # （`total` 一直是 0，看着和"没人改过"一模一样）。所以补列必须幂等且在这里做。
    _ensure_columns(conn, "edit_events",
                    {"subtree_size": "INTEGER", "change_count": "INTEGER",
                     "node_count": "INTEGER"})
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edit_events_map ON edit_events(map_id, at)")
    # 知识库的 `library_sources` / `library_chunks` 与上面这张表建在同一时刻、
    # 同一个连接上：三条 DDL 全部幂等，所以不需要迁移脚本，也不会有"忘了跑脚本"
    # 的窗口（与 `_record` 读时补齐 schema_version 是同一条理由）。
    #
    # import 放在函数内是**为了避免环**，不是风格：`library` 顶层要
    # `from app.mindmaps import repository` 拿 `connect` / `db_path`，在模块顶层
    # 反向 import 回去就成环。函数内的 import 在第一次连接之后由 sys.modules 缓存
    # 命中，没有反复开销。
    from app.knowledge.library import ensure_schema

    ensure_schema(conn)
    # 跨会话记忆（`app/memory/store.py`）。同样在这里建、同样幂等、同样为了消掉
    # "发现表不存在 → 建表"那条分支：读它的两条路径（每次生成前的一次检索、
    # 会话结束时的总结）都在热路径上，多一个可能抛错的分支没有任何好处。
    #
    # import 同样放在函数内：`store` 顶层要 `from app.mindmaps import repository`
    # 拿 `connect`，模块顶层反向 import 回去就成环。函数内的 import 在第一次连接
    # 之后由 sys.modules 缓存命中，没有反复开销。
    from app.memory.store import ensure_schema as ensure_memory_schema

    ensure_memory_schema(conn)
    # 风格画像（`app/prefs/style.py` 的统计层 + `app/prefs/profile.py` 的向量层）。
    # import 放在函数内：两个模块顶层都要 `from app.mindmaps import repository`
    # 拿 `connect`，模块顶层反向 import 回去就成环。与 `library` / `store` 同一条理由。
    from app.prefs.profile import ensure_schema as ensure_profile_schema
    from app.prefs.style import ensure_schema as ensure_style_schema

    ensure_style_schema(conn)
    ensure_profile_schema(conn)
    return conn


def _record(row: sqlite3.Row) -> MapRecord:
    # 库里可能还躺着没有 schema_version 的 v1 记录；补默认值这一步放在读取处，
    # 而不是写一个一次性迁移脚本——数据量小、只有一个写入方，读时补齐足够，
    # 而且永远不会有"忘了跑脚本"的窗口。
    return MapRecord(id=row["id"], version=row["version"],
                     content=migrate_content(json.loads(row["content"])),
                     updated_at=row["updated_at"])


def list_maps(*, tolerant: bool = False) -> list[MapRecord]:
    """按更新时间倒序加载全部导图。

    `tolerant=True` 只给**旁路统计**用（如知识库的"被几张导图引用"）：单行坏数据
    （坏 JSON、v1 之后又演进的字段）跳过并记 warning，不让一行脏数据把整个知识库
    入口打成 500。列表页/导出必须用默认的 `False`——坏数据要暴露给排查的人，
    而不是被静默跳过。
    """
    with closing(connect()) as conn, conn:
        rows = conn.execute("SELECT * FROM mindmaps ORDER BY updated_at DESC, id").fetchall()
    records: list[MapRecord] = []
    for row in rows:
        try:
            records.append(_record(row))
        except Exception:  # noqa: BLE001 - tolerant 模式下"任何读不出来的行"都算坏行
            if not tolerant:
                raise
            logger.warning("跳过一张读不出来的导图：id=%s", row["id"], exc_info=True)
    return records


def maps_fingerprint() -> tuple[str, int, str | None]:
    """导图表当前状态的廉价指纹：库位置 + 条数 + 最后更新时间。

    "被几张导图引用"每次开抽屉都要算，而算它得把每张图的正文全量解析一遍。这个
    查询只走聚合，不读正文——任何新建或保存都会改动指纹（`update_map` 一定刷新
    `updated_at`），所以拿它当缓存键比"纯按时间过期"更准。

    库文件位置也进指纹：测试与本地调试会切 `MINDMAP_DB_PATH`，换了库当然不能沿用
    上一个库的计数（否则两个测试库恰好"条数相同、最后一毫秒相同"时会互相串）。
    """
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, MAX(updated_at) AS latest FROM mindmaps").fetchone()
    return str(db_path()), int(row["total"]), row["latest"]


def get_map(map_id: str) -> MapRecord | None:
    with closing(connect()) as conn, conn:
        row = conn.execute("SELECT * FROM mindmaps WHERE id = ?", (map_id,)).fetchone()
    return _record(row) if row else None


def create_map(content: DiagramContent) -> MapRecord:
    map_id = str(uuid4())
    with closing(connect()) as conn, conn:
        conn.execute("INSERT INTO mindmaps VALUES (?, 1, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                     (map_id, content.model_dump_json()))
    return get_map(map_id)  # type: ignore[return-value]


def update_map(map_id: str, version: int, content: DiagramContent) -> str:
    with closing(connect()) as conn, conn:
        result = conn.execute("""UPDATE mindmaps SET content = ?, version = version + 1,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE id = ? AND version = ?""", (content.model_dump_json(), map_id, version))
        if result.rowcount:
            return "updated"
        exists = conn.execute("SELECT 1 FROM mindmaps WHERE id = ?", (map_id,)).fetchone()
        return "conflict" if exists else "missing"
