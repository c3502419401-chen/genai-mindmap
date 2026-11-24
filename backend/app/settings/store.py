"""模型配置的落库与回落：一张单行表，一条优先级，一个打码函数。

**为什么要有这一层**：原来"用哪个模型服务"只存在于后端进程的环境变量里
（`MINDMAP_LLM_BASE_URL` / `_API_KEY` / `_MODEL`）。换一把密钥就要登服务器改
`EnvironmentFile` 再重启 systemd——而重启会把正在跑的长图打断。用户看得到的
那个页面里没有任何入口。

**为什么有 `owner` 这一维**：这一版开始有账号了（`app/auth/`），"用哪个模型服务"
变成两个层次同时成立的事——部署方给这台机器配一份，每个登录的人在面板里可以再
配自己的一份（BYOK）。所以表从"单行 + `CHECK (id = 1)`"变成"**一行一个 owner**"，
`owner` 就是主键：

  - `''`（`INSTANCE_OWNER`）= **部署方那一份**。它同时是"谁都没配过时大家借用的
    那一份"，免费额度花的就是它；也是环境变量那套配置在库里的对应位置。
  - `user:<user_id>` = 某个登录账号自己的密钥。
  - `guest:<guest_id>` = 某台浏览器（游客）自己的密钥。游客也能 BYOK——他
    不想填而已，不是不许。

**为什么不是给每行加一个可空的 `user_id`**：那样"部署方那一份"与"某个人的那一
份"会共用一个 `id = 1` 的位置，而它俩的写入者、可见范围、删除时机都不同。主键
直接写 owner 之后，"读谁的那一行"是一句 `WHERE owner = ?`，不需要在查询里猜
`user_id IS NULL` 是不是"公共"的意思。

**回落顺序（唯一一份实现，在 `resolve()` 里）**::

    当前身份自己那一行  →  部署方那一份  →  环境变量  →  空（= 未配置）

逐字段回落。中间那一档是这一版新增的，也是最容易写错的一档：**它必须存在**，
否则刚注册、还没填密钥的人一条路都走不了（连免费体验都没有）；而它**只该在
自己没有的时候**才被碰到，否则用户的 BYOK 永远会被部署方那份盖住。

**逐字段回落，不是整组回落**。只填了密钥、端点仍用环境变量，是这套配置最常见
的用法（换 key 比换供应商频繁得多）；按"整组"回落的话用户会被迫在面板里重填
一遍端点与模型名，而重填就会写错。

**密钥只出打码值**：`GET` 永远回 `…abcd`（末 4 位），明文只在进程内被
`resolve()` 与"测试连接"用到。这不是防住有服务器权限的人（他们能读 SQLite 文件），
而是防住三类真实泄漏：截图/录屏里带着整串 key、浏览器 devtools 的 Network 面板、
以及出错时被拼进某条日志或错误文案。

**明文落库**：`api_key` 在 SQLite 里是明文（与它躺在 `EnvironmentFile` 里是同一个
信任边界）。刻意不做"用一把写在代码里的密钥加密"——那只是把密钥换个地方放，
读代码的人一样能解开，却会让"忘了 `MINDMAP_DB_PATH` 指向哪"变成一种新的故障。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.errors import AppError

logger = logging.getLogger(__name__)

#: 表名与"部署方那一行"的 owner 值。
#:
#: `INSTANCE_OWNER` 是空串而不是 `"instance"` 之类的名字，因为它同时承担三件事：
#: 部署方那份配置的 owner、每个人自己没有配置时的回落来源、以及**老库迁移时那唯一
#: 一行的去处**（迁移就是把 `id = 1` 那一行搬成 `owner = ''`，不搬成任何一个 URI）。
TABLE = "model_settings"
INSTANCE_OWNER = ""

#: 迁移时老表临时叫这个名字。带版本号是为了让"迁移到一半被杀掉"这件事可以被识别：
#: 下次启动看到 `_legacy` 表还在、新表也在，就知道要补一次搬运而不是从头重来。
_LEGACY_TABLE = f"{TABLE}_legacy_v1"

#: 环境变量名。与 `generation/service.py` 里的读法保持同一组名字——回落就是
#: "库里没有才发现它在这里"，名字一旦对不上，回落会静默变成"未配置"。
BASE_URL_ENV = "MINDMAP_LLM_BASE_URL"
API_KEY_ENV = "MINDMAP_LLM_API_KEY"
MODEL_ENV = "MINDMAP_LLM_MODEL"
#: 「深度思考」用的模型名。档 1 是这里的值（库 → 环境变量），档 2/3 的启发式在
#: `generation.service._model_for()` 里——那三档的先后只该有一份实现。
DEEP_MODEL_ENV = "MINDMAP_LLM_MODEL_DEEP"
#: 只读展示用：`demo` 时生成侧与视觉侧都不调模型。本模块**不改**它（那是部署
#: 决定），只是把它报给面板，好让"面板里配好了但生成不生效"有据可查。
LLM_MODE_ENV = "MINDMAP_LLM_MODE"

#: 三个域 + 一个派生域。`saved` = 面板保存过；`env` = 环境变量；
#: `unset` = 都没有；`fallback` 只属于 `deep_model`（启发式推出来的名字）。
SOURCE_SAVED = "saved"
SOURCE_ENV = "env"
SOURCE_UNSET = "unset"
SOURCE_FALLBACK = "fallback"
#: **部署方那一行**（`owner = ''`）填的值，被当前这个人借用着。
#:
#: 它必须与 `saved` 分开：两者都"在库里"，但一个是"我自己配的"、一个是"这台机器的
#: 主人在面板里配的公共配置"。界面上的差别是决定性的——看到 `saved` 时用户改的那
#: 一行就是他自己的；看到 `instance` 时他改自己那行只是覆盖，删掉就回到公共配置。
#: 合并成一个值会让用户以为公共配置是他自己配的，而"清除"于是变成一件不敢点的按钮。
SOURCE_INSTANCE = "instance"
#: 库里有密钥，但它绑定的那台地址已经不是当前生效的地址了。**这时不交出密钥**。
#:
#: 它不是"没配"：面板要能说清"你换过地址，密钥需要重新填一次"，而 `unset` 那句话
#: （去配一个）会让用户以为密钥丢了。见 `resolve()` 里那段判定。
SOURCE_UNBOUND = "unbound"
#: 借用**免费档**：自己和部署方那行都没有密钥，但部署方配了 Command Code 的端点，
#: 而那份 coding plan 里此刻有标 `:free` 的模型。
#:
#: 与 `instance` 分开是必须的：`instance` 说的是"部署方付这笔钱"，而这一档
#: **不花钱**、有每日次数上限、而且模型名不是用户在面板里填的那个——它是运行时
#: 现问列表选出来的。合并成一个来源会让面板显示一个用户从没填过的模型名，
#: 而且额度用完之后那句"次数用完了"就无处可挂。
SOURCE_FREE = "free"
#: 用的是**部署方配的公共免费密钥**（`MINDMAP_FREE_TIER_API_KEY`），不是谁的私钥。
#:
#: 必须与 `instance` / `env` 分开，因为它是唯一一档**有次数上限**的：这份密钥是
#: 给大家试用的，每天几次由 `generation/quota.py` 记账。合并来源会让"这次要不要
#: 扣次数"无从判断——而那正是额度这件事的全部。
SOURCE_PUBLIC = "public"

#: 长度上限。base_url / model 各 200，密钥 500：它们进 SQLite、进日志、进
#: 界面输入框，超长的值只会在排查时变成噪音（也顺手挡住"把整份证书贴进来"）。
MAX_URL_CHARS = 200
MAX_MODEL_CHARS = 200
MAX_KEY_CHARS = 500

#: 走免费档时固定发这个思考档位。理由见 `Resolved.reasoning_effort`：不发它，
#: 免费模型会把整个 `max_tokens` 烧在 reasoning 上、正文一个字都不写、
#: `finish_reason` 变成 `length`，而那条路是直接判失败的。
#:
#: `"low"` 是实测出来的下限：两个免费模型都拒绝 `"off"`（400）。
FREE_REASONING_EFFORT = "low"

#: 生成侧与 probe 拼请求时一律用 `base + "/chat/completions"`，所以库里存的必须是
#: **前缀**。用户粘完整端点时由 `_check_base_url` 剥掉这一段。
_ENDPOINT_SUFFIX = "/chat/completions"

#: 写配置时等写锁的上限。这是面板里人手点的一个动作：让他看着转圈等十秒，不如
#: 立刻告诉他"正被占用，过几秒重试"（与 `agent/runs.py` 的检查点同一个理由）。
SETTINGS_LOCK_TIMEOUT = 2.0

#: 时间格式与 `mindmaps` / `runs` / `user_prefs` 完全一致。三处格式不同会在
#: "谁更晚"上错一天（`app/agent/runs.py:_NOW` 写过这条教训）。
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

_CREATE = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    owner        TEXT PRIMARY KEY,
    base_url     TEXT NOT NULL DEFAULT '',
    api_key      TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    deep_model   TEXT NOT NULL DEFAULT '',
    updated_at   TEXT,
    api_key_host TEXT NOT NULL DEFAULT ''
)"""

#: 老表（单行，`id INTEGER PRIMARY KEY CHECK (id = 1)`）的形状标记。
#:
#: 判据用"有没有 `id` 列"而不是"有没有 `owner` 列"：两者互补，但 `id` 是**老表独有**
#: 的列，而 `owner` 在新表里也一定存在——用 `owner` 判会在"迁移到一半"时把半成品
#: 当成已完成。
_LEGACY_MARKER_COLUMN = "id"

#: 降级只告警一次。库坏了（文件被删、权限变了、盘满）时每一次模型调用都会走到
#: 这里，逐次 `warning` 会把日志刷满，反而看不出"从哪一刻开始坏的"。
_DEGRADED_WARNED = False

# ---------------------------------------------------------------- 读缓存
#
# **为什么需要**：`saved()` 在每一次模型调用的开头被 `resolve()` 调到（见
# `generation.service._config`）。实测一次生成里的调用次数：
#
#   - 整树生成（`mindmap_generate`，骨架 + 结构 + 说明 3 次工具内调用，加外层
#     provider 2 轮）= **5 次**；
#   - 分片生成（`mindmap_generate_deep`，4 支 × 3 点展开 = 25 次工具内调用，加
#     外层 2 轮）= **27 次**。
#
# 每次都要 `repository.connect()`，而那个函数开头有十条 `CREATE TABLE IF NOT
# EXISTS`——代价不在那条 `SELECT` 上，在这十条 DDL 上。27 次乘以十条 DDL 是每次
# 大图生成白付的开销，而这份配置在一次生成期间根本不会变。
#
# **为什么不是纯 TTL**：`save()` 之后必须立刻生效（这是这个功能的整个卖点）。所以
# 写入侧（`save` / `clear`）会 `_invalidate()`，**同进程内保存 → 下一次读到新值**。
# TTL（1 秒）只兜住另一种情况：多 worker 部署时另一个进程改了库，本进程看不到那次
# `_invalidate`。本仓的目标形态是单机单 worker，1 秒是给"以后有人加了 worker"留的
# 上限，不是给正常路径延迟的。
#
# 键里带 `db_path()`：测试每个用例换一个 `MINDMAP_DB_PATH`，不带路径的缓存会让
# 前一个用例的行漏到后一个用例里——那种"随机绿"最难查。
_CACHE_TTL_SECONDS = 1.0
_CACHE: dict[str, tuple[float, "Ready"]] = {}
#: 写入计数器。`save()` / `clear()` 加一，缓存项随之作废。用它而不是清空字典，
#: 是为了让"进程内写过的次数"本身成为一个可断言的事实（见测试）。
_WRITES = 0


class SettingsError(AppError):
    """设置写入被拒。带着业务 code 出去，界面才有话可说。"""


def _columns(conn: sqlite3.Connection) -> set[str]:
    return _columns_of(conn, TABLE)


def _columns_of(conn: sqlite3.Connection, table: str) -> set[str]:
    """这张表现在的列名；表不存在时是空集。

    迁移要按**老表实际有什么列**来搬（见 `_copy_legacy_rows`），所以不能只有
    "当前表"这一份。
    """
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return row is not None


def _not_null_columns(conn: sqlite3.Connection, table: str = TABLE) -> set[str]:
    """这张表里所有带 `NOT NULL` 的列。

    搬行时要对它们补默认值：`INSERT OR IGNORE` 会把违反约束的整行丢掉，而老表
    可能允许 NULL（main 的 `deep_model TEXT` 就没有 `NOT NULL`）。用 `PRAGMA
    table_info` 的 `notnull` 那一位判断，不手抄列名——手抄的清单改表时不会跟着变。
    """
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})") if row[3]}


def _copy_legacy_rows(conn: sqlite3.Connection) -> None:
    """把老表那一行搬进新表，**只搬两张表都有的列**。

    列名不能写死。`api_key_host` 是 PR#27 才加到新表上的，而 main 时代的老表里
    根本没有这一列——写死成 `SELECT ..., COALESCE(api_key_host, '') FROM 老表`
    会在 `SELECT` 阶段抛 `no such column`，而这里是 `connect()` 的中段，抛出去
    就是**整个服务起不来**（不是 `saved()` 那个降级分支能兜住的）。

    实测过：拿 main 的老库跑一遍，`repository.connect()` 直接
    `sqlite3.OperationalError: no such column: api_key_host`。

    老表没有的列按"新表那次补列的语义"给默认值：`api_key_host` 缺就是空串
    （= 没绑定过，`resolve()` 按"绑定校验不生效"处理，不会在升级后把已存配置
    打成未配置）。老表有而新表没有的列（已废弃的）自然不搬。
    """
    source = _columns_of(conn, _LEGACY_TABLE)
    target = _columns(conn)
    if not source or not target:
        return
    #: 老表里没有时补什么。目前只有 `api_key_host` 是"新增列"，它的缺省语义是空串。
    defaults: dict[str, object] = {"api_key_host": ""}
    overrides: dict[str, object] = {"owner": INSTANCE_OWNER}
    carried = [name for name in sorted(target)
               if name not in overrides and name in source]
    missing = [name for name in sorted(target)
               if name not in overrides and name not in source and name in defaults]
    names = [*overrides, *carried, *missing]
    #: 新表里 `NOT NULL` 的那些列。老表可能宽容地允许 NULL（main 的
    #: `deep_model TEXT` 就没有 `NOT NULL`，而新表是 `TEXT NOT NULL DEFAULT ''`），
    #: 搬过来时要在 `SELECT` 里就地补默认值。
    required = _not_null_columns(conn)
    select_parts = ["?"] * len(overrides)
    values: list[object] = [*overrides.values()]
    for name in carried:
        if name in required:
            select_parts.append(f'COALESCE("{name}", ?)')
            values.append(defaults.get(name, ""))
        else:
            select_parts.append(f'"{name}"')
    select_parts.extend(["?"] * len(missing))
    values.extend(defaults[name] for name in missing)
    # `OR IGNORE` 而不是 `OR REPLACE`：能走到"老表还在、新表也有行"的时候，说明上一次
    # 迁移在"搬完 / 删老表"之间断了，而那一瞬间之后可能已经有人在新表上保存过配置。
    # 老表里那份是**更旧的**，拿它盖掉新行等于把用户刚改的设置回退。已经搬过去的那
    # 一份本来就一样，IGNORE 不丢东西。
    #
    # **`IGNORE` 的代价**：它连 `NOT NULL` 违规也一起吞掉（`OR REPLACE` 会换成列
    # 默认值）。实测过：main 的老库里 `deep_model` 是 NULL，直接搬会让整行被静默
    # 丢掉、`rowcount = 0`，用户看到"升级完密钥没了"。所以上面逐个 `COALESCE`。
    conn.execute(
        f"""INSERT OR IGNORE INTO {TABLE} ({", ".join(f'"{name}"' for name in names)})
            SELECT {", ".join(select_parts)} FROM {_LEGACY_TABLE}""", tuple(values))


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    """把老的单行表搬成"一行一个 owner"。幂等，且**两段都能续上**。

    老表是 `id INTEGER PRIMARY KEY CHECK (id = 1)`——它把"部署方那一份配置"钉死在
    唯一一行上。新表的主键换成 `owner`，老那一行搬成 `owner = ''`（`INSTANCE_OWNER`）。

    **为什么不用 `ALTER TABLE ADD COLUMN owner` 再补主键**：SQLite 不支持给已有表
    加主键，也不支持改主键。要么重建表，要么放弃主键约束——而放弃约束之后"同一个
    owner 两行"就成了一种可能的状态，那时 `SELECT` 得靠 `ORDER BY` 猜哪行生效，
    正是老设计用 `CHECK (id = 1)` 想避免的那件事。

    **两段式，每段都可续**：

      1. `RENAME` 老表 → `_LEGACY_TABLE`，再建新表，再 `INSERT ... SELECT` 搬那一行。
      2. 删掉 `_LEGACY_TABLE`。

    进程在任何一处被杀掉，下一次 `connect()` 看到的都是可识别的中间态：新表已建、
    老表还在 → 再搬一次（`INSERT OR IGNORE` 幂等）；老表已删、新表在 → 什么都不做。
    这是刻意的：迁移跑在一个每个请求都会调的函数里，它不能有"只许成功一次"的假设。

    **每段末尾都要 `commit()`**——这一版最贵的教训。`sqlite3` 在默认
    `isolation_level` 下只对 DML 开事务，DDL（`ALTER` / `CREATE` / `DROP`）走自动
    提交。所以"改名 → 建表 → 搬行 → 删表"这四步里，一、二、四步立刻落盘，而
    `INSERT ... SELECT` 停在一个没人提交的事务里，`connect()` 的调用方一 `close()`
    就整段回滚。磁盘上留下的是"新表空的 + 老表还在"，而判据（老表在 = 还没搬完）
    下次仍然成立，于是**又搬一次空表、老数据再也没被读过**——用户在页面上看到的是
    "我配的密钥没了"。第一次读之所以还能读到，是因为同一条连接里事务还没回滚。

    开发库上真踩到过：

        tables: [... 'model_settings_legacy_v1' ... 'model_settings']
        model_settings rows: []

    **只在真的看到老表时才动**（判据是 `id` 列且没有 `owner` 列）。老表独有的 `id`
    而不是新表也有的 `owner`：迁移到一半时新表已经有了 `owner`，用 `owner` 判会把
    半成品当成已完成。
    """
    columns = _columns(conn)
    legacy_renamed = _has_table(conn, _LEGACY_TABLE)
    if _LEGACY_MARKER_COLUMN in columns and "owner" not in columns:
        # 第 1 段。老表名还在，先改名腾出表名（`IF NOT EXISTS` 的 `_CREATE` 才能建新表）。
        conn.execute(f"ALTER TABLE {TABLE} RENAME TO {_LEGACY_TABLE}")
        legacy_renamed = True
    if legacy_renamed:
        # 新表必须在这里就建出来，不能留给 `ensure_schema()` 后面的那句 `_CREATE`：
        # 改名之后 `PRAGMA table_info({TABLE})` 是**空集**（表名已经挪走了），下面
        # 那段"看到 `owner` 列才算搬"的判据会永远为假，于是搬运与删表都不发生——
        # 结果是库里同时躺着一张空的新表和一张没搬的老表，老配置看起来"不见了"。
        conn.execute(_CREATE)
    if legacy_renamed and _has_table(conn, _LEGACY_TABLE):
        # 第 2 段。搬那一行（如果它真的有行：老库可能是空表）。
        _copy_legacy_rows(conn)
        conn.commit()
        conn.execute(f"DROP TABLE {_LEGACY_TABLE}")
    conn.commit()


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建表。由 `repository.connect()` 在同一个连接上调用，幂等。

    与 `user_prefs` / `memory_items` / `library_sources` 建在同一时刻的理由一样：
    读它的路径（**每一次模型调用**，见 `generation.service._config`）在热路径上，
    多一个"发现表不存在 → 建表"的分支只会在最不该出错的地方多一条可能出错的路径。

    **先迁移、再建表**：顺序反了的话，老库上 `CREATE TABLE IF NOT EXISTS` 会因为
    同名表已存在而静默什么都不做，`_ensure_columns` 随后补上 `deep_model` 之类的列，
    于是"迁移"看起来成功了——而表还是单行的那张，`SELECT ... WHERE owner = ?` 会抛
    `no such column: owner`，被 `saved()` 的降级分支吞成"未配置"。用户看到的是
    "我配的密钥不见了"，而不是"迁移没跑"。

    `_ensure_columns` 那一步在新表上是空操作（建表语句里已经有全部列），留着是为了
    下一个人往这张表加列时不必记得"`CREATE TABLE IF NOT EXISTS` 不会给**已经存在**
    的表补列"——线上那张表一旦有行，加列就再也进不去了，而缺列的表现是
    `SELECT` 抛 `no such column`，被下面的降级分支吞成"未配置"，看起来像"用户没设过"。
    """
    _migrate_legacy(conn)
    conn.execute(_CREATE)
    from app.mindmaps.repository import _ensure_columns

    _ensure_columns(conn, TABLE, {
        "deep_model": "TEXT",
        "updated_at": "TEXT",
        # 密钥绑定在哪台主机上。老库补这一列时全部是空串 = "没绑定过"，
        # 那种行按"绑定校验不生效"处理（见 `resolve()`），不会在升级后
        # 突然把所有已存配置打成未配置。
        "api_key_host": "TEXT NOT NULL DEFAULT ''",
    })


@dataclass(frozen=True)
class Ready:
    """库里那一行的原样值（**含明文密钥**，只在进程内流转，绝不进响应）。"""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    deep_model: str = ""
    updated_at: str | None = None
    #: 这把密钥当初是在哪台主机上填进去的。空串 = 没有记录（老行，或从没换过地址）。
    #: 它是"密钥不出门"那条防线的锚点：地址一变，锚点对不上就不交密钥。
    api_key_host: str = ""


@dataclass(frozen=True)
class Resolved:
    """一次解析的结果：四个生效值 + 每个值来自哪里。

    `*_source` 不是调试信息，它是界面上的「来自环境变量 / 你在面板里设的」标签：
    没有它，用户分不清"我改了但没生效"与"我改的本来就被环境变量盖住了"——而
    优先级是库 > 环境变量，所以"盖住"这件事在当前口径下不会发生，能发生的是
    "忘了保存"与"存了但连不上"，两者要靠来源标签 + 测试连接分开。
    """

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    deep_model: str = ""
    base_url_source: str = SOURCE_UNSET
    api_key_source: str = SOURCE_UNSET
    model_source: str = SOURCE_UNSET
    deep_model_source: str = SOURCE_UNSET
    #: 走免费档时要一起发出去的思考档位；其余情况是 None（= 不传这个字段，
    #: 请求体与加这个字段之前逐字节相同）。
    #:
    #: 免费模型实测**会把 max_tokens 全部烧在 reasoning 上**：`max_tokens=1400`
    #: 的一次调用用了 56.9 秒、`completion_tokens=1400` 其中 `reasoning_tokens=1311`、
    #: 正文是空串、`finish_reason=length`——而 provider 对 length 是直接判失败。
    #: 发 `reasoning_effort="low"` 之后同一次调用 8.9 秒、正文正常、还调了工具。
    #:
    #: 不能发 `"off"`：两个免费模型都明确拒绝（400，只接受 low/medium/high/xhigh/max）。
    reasoning_effort: str | None = None


def _connect(*, timeout: float | None = None) -> sqlite3.Connection:
    # 函数内 import：`repository.connect()` 里要用本模块的 `ensure_schema`，模块顶层
    # 反向 import 回去就成环。与 `app/prefs/style.py` / `app/memory/store.py` 同一手法。
    from app.mindmaps import repository

    if timeout is None:
        return repository.connect()
    return repository.connect(timeout=timeout)


def _norm_base(text: str | None) -> str:
    """端点：去空白 + 去掉**末尾**斜杠。

    与 `generation.service._config()` 原来的处理逐字节一致（`strip().rstrip("/")`）。
    不规范化的话，`https://x/v1/` 会让请求打到 `https://x/v1//chat/completions`——
    多数兼容端点容忍，少数直接 404，而 404 在界面上只能读成"模型名不对"。
    """
    return (text or "").strip().rstrip("/")


def _norm_text(text: str | None) -> str:
    return (text or "").strip()


def _host_of(base_url: str) -> str:
    """端点里的主机名（小写）。空串 = 地址为空或解析不出来。

    **只取主机名，不取端口、不取路径**。绑定要回答的问题是"这把密钥属于哪一台
    主机"，不是"属于哪个 URL"：同一台机器上 `/v1` 换成 `/v2`、Ollama 从 11434
    挪到 11435，密钥都还是同一把，为这些变动逼人重填只会训练出"随手再粘一次"的
    习惯——而那正是这道防线要避免的输入。反过来，换主机就意味着换了一个"会收到
    这把密钥的对方"，那才是要重新确认的事。

    解析失败（端口不是数字之类）返回空串：那种地址连 `_check_base_url` 都过不了，
    这里不必再抛一次。
    """
    try:
        return (urlsplit(_norm_base(base_url)).hostname or "").strip().lower()
    except ValueError:
        return ""


def _cache_key(owner: str) -> str:
    """缓存键：库路径 + 写入计数 + 是谁那一行。

    带路径的理由见 `_CACHE` 的注释（测试每个用例换一个库）。带写入计数的理由：
    `save()` 之后即使 TTL 还没到也必须作废，所以键里直接含"写过几次"——写一次，
    键就变了，旧项自然不再命中，不需要额外的失效逻辑去和并发读抢。

    带 owner 是这一版新增的，也是**一旦漏掉就会串号**的一处：同一个进程里 A 与 B
    前后脚请求，不带 owner 的缓存键会让 B 读到 A 的那一行配置。那不是"读到旧值"，
    是"读到别人的密钥"。
    """
    from app.mindmaps import repository

    return f"{repository.db_path()}::{_WRITES}::{owner}"


# ---------------------------------------------------------------- owner
#
# 一行配置属于谁。三种 owner，两种形状（见文件头）：实例行是空串，其余带前缀。
#
# **为什么带前缀而不是直接存 `user_id`**：游客没有 `user_id`，而两者都是不透明的
# 十六进制串——不前缀就分不清"这个串是用户还是游客"，一个 id 撞上另一个就是
# "游客拿到了账号的密钥"。前缀让这件事在**存进去的那一刻**就不可能。

_OWNER_USER = "user:"
_OWNER_GUEST = "guest:"


def owner_of(identity) -> str:
    """当前身份自己那一行的 owner。**没登录也没游客 cookie 时是空串**。

    空串在这里有两重意思，而它们恰好等价：`INSTANCE_OWNER`（部署方那一行）与
    "这个人还没有自己的行"。匿名请求不该往库里写任何东西，读的时候也直接落到
    实例行——这正是我们要的：没有身份的人借用公共配置。

    `identity` 用鸭子类型（只读 `.user` 与 `.guest_id`）而不是标成 `store.Identity`
    类型：`app.auth.identity` 会 import 本模块，反过来标类型就成环。
    """
    user = getattr(identity, "user", None)
    if user is not None:
        return f"{_OWNER_USER}{user.id}"
    guest_id = getattr(identity, "guest_id", None)
    if guest_id:
        return f"{_OWNER_GUEST}{guest_id}"
    return INSTANCE_OWNER


def _owner_for_write(scope: str) -> str:
    """这次写入落哪一行。`scope="instance"` 是"本站共享配置"，其余按当前身份。

    由 `save()` 调用。权限判定在路由层（它要能回一个带 code 的 403），这里只做
    "instance 就是一种 owner" 这件事——判定与落点分开，是因为落点还要给测试与
    "部署方自己用面板"两条路复用。
    """
    if scope == "instance":
        return INSTANCE_OWNER
    from app.auth.identity import current_identity

    return owner_of(current_identity())


def saved(owner: str | None = None) -> Ready:
    """库里某个 owner 那一行。没有行、或者库读不出来，都返回全空。

    `owner=None` 读的是**当前身份自己那一行**（匿名请求 → 实例行，见 `owner_of`）。

    **读失败时降级而不是抛**：这条路径在每一次模型调用的开头，让它因为一个读不到
    的配置表把整次生成打成失败，比"回落到环境变量"糟糕得多——后者在绝大多数部署
    上恰好是正确的行为（环境变量本来就配好了）。降级会留一条 warning，不静默。

    **降级的结果不进缓存**：库坏掉是一个可能被修好的状态（盘满了、权限改回来），
    把"读不到"缓存一秒会让修好之后的第一秒继续走环境变量。代价是坏库时每次都真的
    去试一次——那本来就该被看见。
    """
    global _DEGRADED_WARNED
    if owner is None:
        from app.auth.identity import current_identity

        owner = owner_of(current_identity())
    key = _cache_key(owner)
    cached = _CACHE.get(key)
    if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    try:
        with closing(_connect()) as conn:
            row = conn.execute(
                f"SELECT base_url, api_key, model, deep_model, updated_at, api_key_host "
                f"FROM {TABLE} WHERE owner = ?", (owner,)).fetchone()
    except sqlite3.Error as exc:
        if not _DEGRADED_WARNED:
            _DEGRADED_WARNED = True
            logger.warning(
                "读不到 %s 表（%s），模型配置整体回落到环境变量；后续同类错误不再重复告警",
                TABLE, type(exc).__name__)
        return Ready()
    ready = Ready() if row is None else Ready(
        base_url=row["base_url"] or "",
        api_key=row["api_key"] or "",
        model=row["model"] or "",
        deep_model=row["deep_model"] or "",
        updated_at=row["updated_at"],
        api_key_host=_row_host(row),
    )
    _CACHE[key] = (time.monotonic(), ready)
    return ready


def _row_host(row: sqlite3.Row) -> str:
    """兼容"还没补上 `api_key_host` 列"的库。

    `_ensure_columns` 会让新库与新连接都带上这一列，但降级路径（`saved()` 读失败
    返回 `Ready()`）与"读到一半被别的进程改成老结构"这类边角不值得一次 500：
    缺列就当"没有绑定记录"，走的是与老行相同的"不校验"分支。用 `row.keys()`
    判而不是 try/except，是为了让 `IndexError` 真的冒出来——那是真 bug。
    """
    return (row["api_key_host"] or "") if "api_key_host" in row.keys() else ""


def _read_strict(owner: str, *, timeout: float | None = None) -> Ready | None:
    """像 `saved(owner)` 一样读那一行，但**读失败时抛、不降级**。只有写路径用。

    `saved()` 的降级是给**生成路径**准备的：读不到就回落环境变量，那在绝大多数
    部署上恰好是对的，绝不能因为一个读不出来的配置表把整次生成打成失败。

    写路径不能复用那个返回值。`save()` 拿它当"库里现在那一行"去做差量与合并
    （`next_base = current.base_url if base_url is None else ...`），降级返回的空
    `Ready()` 会被当成真值：只改一个模型名，`base_url` 与 `api_key` 就一起被写成
    空串，接口还回 `changed: true`。用户看到的是一次"保存成功"，丢掉的是密钥。

    降级读在写锁竞争下真会发生：`repository.connect(timeout=10)` 忙等十秒之后
    `saved()` 返回空 `Ready()` 并只打一条 warning，而那次写照样成功。
    """
    with closing(_connect(timeout=timeout)) as conn:
        row = conn.execute(
            f"SELECT base_url, api_key, model, deep_model, updated_at, api_key_host "
            f"FROM {TABLE} WHERE owner = ?", (owner,)).fetchone()
    if row is None:
        return None
    return Ready(
        base_url=row["base_url"] or "",
        api_key=row["api_key"] or "",
        model=row["model"] or "",
        deep_model=row["deep_model"] or "",
        updated_at=row["updated_at"],
        api_key_host=_row_host(row),
    )


def _invalidate() -> None:
    """写完之后让缓存作废。由 `save()` / `clear()` 调用，别处不要碰。"""
    global _WRITES

    _WRITES += 1
    _CACHE.clear()


def _pick(own: str, instance: str, env_name: str, *, normalize) -> tuple[str, str]:
    """一个字段的回落：**自己那行 → 部署方那行 → 环境变量 → 空**。

    **逐字段**（见文件头）：自己那行有值就用它，没有才看下一档。反过来会让
    "用户在面板里改完立刻生效"变成一个谎。

    为什么中间那档（`instance`）必须存在：新注册的人自己一行都没有，若只认
    "自己那行 + 环境变量"，那么一个在面板里配好的实例（密钥只存在库里、环境变量
    是空的）会让新用户一条路都走不了——他会看到"模型还没配置"，而部署方明明配过。
    反过来，只在自己**没有**这个字段时才看实例行：否则 BYOK 永远被公共配置盖住，
    用户填了也不生效（那是更坏的形状：他以为自己的钱在花，其实是公共额度）。

    三档必须报三种不同的来源。看到 `saved` 时用户改自己那行就是改他自己的；看到
    `instance` 时他改自己那行只是**覆盖**，清掉就回到公共配置——把两者合并成一个
    "已配置"会让「清除」变成一个不敢点的按钮。
    """
    value = normalize(own)
    if value:
        return value, SOURCE_SAVED
    value = normalize(instance)
    if value:
        return value, SOURCE_INSTANCE
    value = normalize(os.getenv(env_name, ""))
    if value:
        return value, SOURCE_ENV
    return "", SOURCE_UNSET


def _binding_of(stored: Ready, api_key_source: str) -> str:
    """这把密钥**被谁验证过**：能交出密钥的主机名。空串 = 没有锚点（见 `resolve()`）。

    两个来源，对应密钥的两个来处：

      - 面板里存的密钥 → `api_key_host` 列。老行（这一列还没有时写的）兜底用
        **那一行自己的 `base_url`**：同一行、同一次写入，地址与密钥本来就是一对。
        这个兜底只在读取时算，不写库——写库的时机是 `save()`，而 `save()` 在
        "地址变了但密钥没重填"时会把这个旧锚点**如实写下来**，于是敏感的那一步
        （换地址）立刻变成"锚点对不上"。
      - 环境变量里的密钥 → 同一组环境变量里那个 `MINDMAP_LLM_BASE_URL` 的主机名。
        部署方把两者写在一起，那一次写入就是它们的绑定动作。**只给了密钥没给地址**
        时无从校验（返回空串），这种情况下"谁配的地址谁说了算"，而那种部署本来
        就把密钥放在一台谁能改地址谁就能读环境变量的机器上。
    """
    if api_key_source in (SOURCE_SAVED, SOURCE_INSTANCE):
        # `instance` 那一行也是"面板里存的"，锚点语义完全一样：它记的是部署方
        # 把那把密钥写进那台地址时的事。借用它的用户不会改它，所以锚点对不上只可能
        # 是部署方自己换过地址——那时谁都不该拿到这把旧密钥。
        return _norm_text(stored.api_key_host).lower() or _host_of(stored.base_url)
    if api_key_source == SOURCE_ENV:
        return _host_of(os.getenv(BASE_URL_ENV, ""))
    return ""


def _free_model(base_url: str) -> str:
    """当前首选的免费模型名；`MINDMAP_FREE_TIER` 没开或一个都没有时返回空串。

    这层薄封装的唯一作用是**把 import 放在函数里**：`generation.free_models` 要
    回来读本模块（`free_tier_endpoint()`），模块顶层互相 import 就成环。
    """
    from app.generation import free_models

    if not free_models.enabled():
        return ""
    return free_models.current(base_url) or ""


def host_of(base_url: str) -> str:
    """`base_url` 的主机名（小写，不含端口与路径）；解析不了时返回空串。

    公开 `_host_of` 的**唯一理由**：路由层那条"测连接"的路径要拿同一把尺子比
    "这个地址是不是密钥当初绑定的那台"。两处各写一套解析，迟早会在
    `https://x/v1` 与 `https://x:443/v1` 这种形状上给出不同答案。
    """
    return _host_of(base_url)


def bound_host(resolved: Resolved) -> str:
    """生效密钥绑定的主机名；没密钥、或无法确认绑在哪台时返回空串。

    给 `settings.router` 的"测连接"用。那条路的地址是**请求里给的**（`overrides`），
    不经过 `resolve()`，所以 `resolve()` 出口那道绑定校验管不着它：用户可以把地址
    填成任意域名、而密钥留空借走已存的那把，一次请求就把部署方的密钥发出去
    （PR#27 审查的 P0-1）。

    锚点行与 `resolve()` 里的 `_binding_of` 同一套口径：密钥来自实例行就看实例行，
    否则看当前这个人自己那行。兜底用生效地址——"只给了密钥没给地址"的部署里，
    那把密钥今天本来就会被发到那儿，返回空串会让那条常规用法突然报错。
    """
    if not resolved.api_key:
        return ""
    from app.auth.identity import current_identity

    if resolved.api_key_source == SOURCE_INSTANCE:
        anchor = saved(INSTANCE_OWNER)
    else:
        anchor = saved(owner_of(current_identity()))
    return _binding_of(anchor, resolved.api_key_source) or _host_of(resolved.base_url)


def free_tier_endpoint() -> tuple[str, str]:
    """免费档用的那台端点与密钥：**部署方那一份**，不做任何免费档替换。

    给 `generation.free_models._fetch()` 用。它不能用 `resolve()`——那条路上
    `resolve()` 会反过来调 `free_models`，直接无限递归。

    取值口径就是实例行 → 环境变量（跳过当前用户那一行）：免费模型是"部署方的
    coding plan 里那些"，与某个用户自己填的供应商无关。用户填了自己的密钥时
    `resolve()` 根本不会走免费档，所以这里也不需要关心他。

    密钥**优先用公共那把**（`MINDMAP_FREE_TIER_API_KEY`）：免费模型的名单是
    那把钥匙所属 coding plan 的事实。没有它才退回实例行/环境变量那把——那种
    部署是"部署方自己的密钥顺便开着免费档"，名单照样是同一个端点问出来的。
    """
    from app.generation import quota

    instance = saved(INSTANCE_OWNER)
    base = _norm_base(instance.base_url) or _norm_base(os.getenv(BASE_URL_ENV, ""))
    key = (
        quota.public_key()
        or _norm_text(instance.api_key)
        or _norm_text(os.getenv(API_KEY_ENV, ""))
    )
    return base, key


def resolve() -> Resolved:
    """生效的模型配置。**全仓唯一一处定义"库 > 环境变量"的地方**。

    调用方：`generation.service._config()`（生成与工具两条出口）、
    `ingestion.vision_pdf._config()`（视觉提取）、`settings.router`（面板与测试连接）。
    三处都从这里取，口径才不会分叉——AGENTS.md 那条"改一个能力的入口，要检查所有
    会走到它的路径"在这里的落法就是：**别的出口不许自己 `os.getenv` 这三个名字**。

    **密钥与地址是绑定的**（P0-1 的第二层）：这一函数是明文密钥的唯一出口，
    所以"这个地址配得上用这把钥匙吗"必须也在这里回答。地址的主机名与密钥的锚点
    不一致时，密钥**不交出去**，来源标记成 `unbound`——界面据此说"你换过地址，
    密钥需要重新填一次"，而不是悄悄把旧密钥发到新地址。

    为什么这才是那条攻击的真正止损点：`PUT /settings/model` 改地址本身不需要密钥，
    `POST /settings/model/test` 会带着**已存的**密钥去请求。绑定的判据不放在
    `probe()` 里，是因为发送点不止一个——生成、视觉提取、外部检索全都从这里取
    密钥，把判据放在出口上，所有路径一起受保护（放进入口就得记住每一个入口）。

    **回落是两级**（`_pick` 的注释里写了为什么中间那档必须有）：当前身份自己
    那一行 → 部署方那一份 → 环境变量。没有身份的人（既没登录也没游客 cookie）
    直接读实例行——这正是我们要的：没有身份的人借用公共配置。
    """
    # 函数内 import：`generation.quota` 读环境变量与库，反过来 `generation.service`
    # 又要读本模块，顶层互相 import 有环的风险（与 `_free_model` 同一个理由）。
    from app.auth.identity import current_identity
    from app.generation import quota

    identity = current_identity()
    owner = owner_of(identity)
    stored = saved(owner)
    # 实例行只在"这本来就不是实例行"时才有意义：`owner == INSTANCE_OWNER` 时
    # 同一个对象读两遍，第二遍还会撞上缓存之外的另一次查询。
    instance = Ready() if owner == INSTANCE_OWNER else saved(INSTANCE_OWNER)
    base_url, base_url_source = _pick(
        stored.base_url, instance.base_url, BASE_URL_ENV, normalize=_norm_base)
    api_key, api_key_source = _pick(
        stored.api_key, instance.api_key, API_KEY_ENV, normalize=_norm_text)
    model, model_source = _pick(
        stored.model, instance.model, MODEL_ENV, normalize=_norm_text)
    deep_model, deep_model_source = _pick(
        stored.deep_model, instance.deep_model, DEEP_MODEL_ENV, normalize=_norm_text)
    # 只在这个地址**真的存在**时校验：没有地址就无从"发错地方"，而那时
    # `configured` 已经是 False，界面上说的是另一个问题（去填地址）。
    # 锚点为空串 = 无从判断（环境里只给了密钥、没给地址），那时不拦——
    # 拦住它只会让这种部署升级后直接不可用，而它本来就把密钥交给了一台
    # "谁能改配置谁就能读环境变量"的机器。
    #
    # 锚点取自**密钥来的那一行**（`stored` 在 `saved` 时是实例行内容，但 `_pick`
    # 才知道到底用了谁的值）：借用了部署方那行时锚点必须也是那行的锚点，用自己
    # 那行（可能是空的）去比会得出"无从判断"从而放行一把被绑过的密钥。
    anchor_row = instance if api_key_source == SOURCE_INSTANCE else stored
    host = _host_of(base_url)
    if api_key and host:
        bound = _binding_of(anchor_row, api_key_source)
        if bound and bound != host:
            api_key, api_key_source = "", SOURCE_UNBOUND

    # 公共免费密钥：部署方在 `MINDMAP_FREE_TIER_API_KEY` 里放一把"只给免费模型用"
    # 的钥匙，没有自己密钥的人借它生成，每人每天有次数上限（`generation/quota.py` 记账）。
    #
    # **地址也必须是部署方的**，这是这一档唯一的安全前提：公共密钥是"部署方那把
    # 钥匙"，它只认识部署方配的那台端点。把调用方自己写的 `base_url` 与它配对，
    # 等于任何人 `PUT /settings/model {"base_url": "https://attacker.example/v1"}`
    # 就能把公共钥匙领走——不需要密钥、不需要读响应，这正是 P0-1 修过的形状。
    # 所以判据是**两个来源都在白名单里**，而不是"没有自己的密钥"这种否定式条件：
    # 白名单每多一个来源都要有人想一次"它的地址是谁选的"，否定式条件不会。
    #
    # 用户自己写地址却不填密钥时，这里不给他公共钥匙，于是 `api_key` 仍是空、
    # `configured` 为假，界面说的是"去填密钥"——那也是加公共密钥之前的行为。
    # 让他的请求发不出去，比替他挑一把钥匙发到他写的地址上要诚实。
    if base_url and base_url_source in (SOURCE_INSTANCE, SOURCE_ENV):
        if api_key_source in (SOURCE_INSTANCE, SOURCE_ENV, SOURCE_UNSET):
            public = quota.public_key()
            if public:
                api_key, api_key_source = public, SOURCE_PUBLIC

    # 免费档：**自己没有密钥、借的是部署方的**时候，把模型名换成当前免费的那个。
    #
    # 判据是 `api_key_source` 落在 `instance` / `env` / `public` 上，而不是"没有密钥"：
    # 免费模型也一样要 key（实测无 key 401 `UNAUTHORIZED`），只是那次调用不收费。所以
    # 这一档的语义是"用部署方那把 Command Code 的钥匙、但走它不收费的模型"。
    #
    # 什么时候不换：
    #   - `saved`：用户填了自己的密钥，按用户的意思走，免费档与他的钱无关；
    #   - `unset` / `unbound`：连端点或密钥都没有，免费档也发不出请求，此时该报的
    #     是"去配模型"，不是"换一个免费模型试试"。
    #
    # **免费档是慢的**（实测吞吐约 24.6 tok/s，一张四十节点的图要几分钟），所以
    # 打开它是一次明确的行为选择：部署方用自己的行填密钥就能退出这一档。
    reasoning_effort: str | None = None
    if api_key and api_key_source in (SOURCE_INSTANCE, SOURCE_ENV, SOURCE_PUBLIC):
        free_model = _free_model(base_url)
        if free_model:
            model, model_source = free_model, SOURCE_FREE
            reasoning_effort = FREE_REASONING_EFFORT
            deep_model, deep_model_source = "", SOURCE_FREE

    return Resolved(
        base_url=base_url,
        api_key=api_key,
        model=model,
        deep_model=deep_model,
        base_url_source=base_url_source,
        api_key_source=api_key_source,
        model_source=model_source,
        deep_model_source=deep_model_source,
        reasoning_effort=reasoning_effort,
    )


def deep_override() -> tuple[str, str]:
    """「深度思考」显式配置的模型名，以及它来自哪里。空串 = 没配。

    单独一个函数而不是让 `_model_for` 直接读环境变量：那一档的优先级（自己那行
    → 部署方那行 → 环境变量）必须与其余三个字段同源，否则"面板里填了深度模型"
    会对工具内部那几次调用无效，而界面上 `done.model` 显示的是外层那一次——又是
    "看起来对了，只有产物是错的"。
    """
    from app.auth.identity import current_identity

    owner = owner_of(current_identity())
    stored = saved(owner)
    instance = Ready() if owner == INSTANCE_OWNER else saved(INSTANCE_OWNER)
    return _pick(
        stored.deep_model, instance.deep_model, DEEP_MODEL_ENV, normalize=_norm_text)


def updated_at() -> str | None:
    """界面上"上次保存"该显示的时刻：自己那行有就用它，否则用部署方那行。

    为什么不是 `saved().updated_at`：一个刚从游客变成账号的人自己一行都没有，
    而画布上生效的明明是部署方的共享配置——显示 `null` 会让面板说"还没配过"，
    与它上面显示的地址/模型名自相矛盾。
    """
    from app.auth.identity import current_identity

    owner = owner_of(current_identity())
    own = saved(owner).updated_at
    if own:
        return own
    if owner == INSTANCE_OWNER:
        return None
    return saved(INSTANCE_OWNER).updated_at


def mask_api_key(key: str) -> str:
    """密钥打码：**只露末 4 位**，前缀统一是一个省略号。

    三条规则，都是从真实泄漏面倒推的：

      - 空 → 空串。界面据此显示"未配置"，而不是一个看起来像配置了的 `…`；
      - 长度 ≤ 8 → 只给 `…`。短 key（自建端点的口令常常是 8 位以内）露末 4 位
        等于把一半以上送出去，而它本来就不该指望"打码之后还认得出来"；
      - 其余 → `…` + 末 4 位。

    不保留 `sk-` 之类的前缀：那只是供应商的命名习惯，露出来对用户没有信息量，
    对截图里的旁观者倒是多一段可辨识的串。
    """
    text = (key or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "…"
    return "…" + text[-4:]


def llm_mode() -> str:
    """当前 `MINDMAP_LLM_MODE` 的原值（小写）。**只读**，供面板提示用。

    刻意不参与"配好没配好"的判定：`demo` 是部署方定的，配置面板改不动它。
    它出现在响应里是因为有一个真实的坑——`.env.example` 的默认值就是 `demo`，
    本机照抄之后即使在面板里填好了模型，agent 那条路仍然产出占位图。
    """
    return os.getenv(LLM_MODE_ENV, "").strip().lower()


def _check_base_url(text: str) -> str:
    """端点只收 http(s)。空串合法，意思是"清掉库里的值、回落到环境变量"。"""
    value = _norm_base(text)
    if not value:
        return ""
    # 用户常常从厂商文档里**连端点一起**复制过来（`https://x/v1/chat/completions`）。
    # 留着它请求会变成 `.../chat/completions/chat/completions`，404 在界面上只能
    # 读成"模型名不对"，于是人被引去改一个没问题的地方。入库前剥掉，probe 与
    # 生成两条路同时受益，面板回显的也是剥完的值。
    if value.endswith(_ENDPOINT_SUFFIX):
        value = value[: -len(_ENDPOINT_SUFFIX)].rstrip("/")
    if not value:
        # 只填了 `/chat/completions`：剥完是空串，那是"没填地址"，不是"清掉覆盖"。
        raise SettingsError(
            "settings_base_url_invalid",
            "模型地址只填了 /chat/completions，前面还差主机名，例如 https://api.deepseek.com/v1。",
            status=400)
    if len(value) > MAX_URL_CHARS:
        raise SettingsError(
            "settings_base_url_invalid",
            f"模型地址最多 {MAX_URL_CHARS} 个字符。", status=400)
    if not value.startswith(("https://", "http://")):
        # 与 `_config()` 的校验同一句话：配置面板里写错一个协议头，用户看到的
        # 必须是"地址要 http(s) 开头"，而不是等到生成时报"模型服务不可用"。
        raise SettingsError(
            "settings_base_url_invalid",
            "模型地址要以 https:// 或 http:// 开头，例如 https://api.deepseek.com/v1。",
            status=400)
    return value


def _check_text(value: str, *, label: str, limit: int) -> str:
    text = _norm_text(value)
    if len(text) > limit:
        raise SettingsError(
            "settings_value_too_long", f"{label}最多 {limit} 个字符。", status=400)
    return text


def _read_strict_for_save(owner: str) -> Ready:
    """`save()` 专用的读：**读不到就拒写**，绝不返回"空值"。

    与 `_read_strict()` 分开只是为了把"没有行"折成空 `Ready`——第一次保存时
    本来就没有行，那不是错误；而"读不出来"必须抛。
    """
    try:
        row = _read_strict(owner, timeout=SETTINGS_LOCK_TIMEOUT)
    except sqlite3.Error as exc:
        # 宁可这次保存失败，也不能把"读不到"当成"是空的"：那会把用户已存的
        # base_url 与密钥一起写成空串，还回 200「保存成功」。
        raise SettingsError(
            "settings_db_unavailable",
            "读不到配置表，这次没有保存（继续下去会把你已存的值写成空）。请过几秒重试。",
            status=503) from exc
    return Ready() if row is None else row


def save(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    deep_model: str | None = None,
    clear_api_key: bool = False,
    scope: str = "user",
) -> tuple[Resolved, bool]:
    """写配置，返回（写入后的生效配置，这次有没有真的改变）。

    `scope` 决定写谁那一行：`"user"`（缺省）= 当前身份自己那行（登录用户、游客
    都算，"他不想填而已，不是不许"）；`"instance"` = 部署方那行、全站共享。
    **谁能传 `"instance"` 由路由层判定**，这里只负责落点——判定要回带 code 的
    403、要读"谁是第一个注册的人"，那些都不该在存储层。
    **三个字段的"空串"含义不同，是有意为之**：

      - `api_key=None / ""` → **不改**。界面上那个框显示的是打码值（`…abcd`），
        用户不动它时提交的就是空串；把空串当"清空密钥"，等于每次改个模型名就
        把密钥抹掉一次。要清空必须显式 `clear_api_key=True`。
      - `base_url` / `model` / `deep_model` 的 `None` → 不改；`""` → **清掉库里的
        值，回落到环境变量**。这三个不是密钥，界面上显示的是生效值本身，空的输入
        框就是"我不要这个覆盖了"；而"回到环境变量那套配置"是真实需求（换部署、
        回滚、临时排查）。

    `changed=False` 时**不刷 `updated_at`**：同值重复保存把时间戳刷成"刚刚"，
    界面上就会出现一条看起来刚改过、其实一直没变的记录——与 `user_prefs.set`
    同一条理由。
    """
    if clear_api_key and (api_key or "").strip():
        # 两个字段同时给是矛盾的：一个说"清掉"，一个说"设成这个"。静默让其中一个
        # 赢，下一次就会有人在这里丢掉一把刚填好的密钥。宁可 400。
        raise SettingsError(
            "settings_clear_conflict",
            "不能同时提交新密钥和「清除密钥」；请只留一个。", status=400)

    owner = _owner_for_write(scope)
    current = _read_strict_for_save(owner)
    next_base = current.base_url if base_url is None else _check_base_url(base_url)
    if clear_api_key:
        next_key = ""
    elif not (api_key or "").strip():
        # `None` 与空串是同一件事：**不改**。界面上那个框显示打码值（`…abcd`），
        # 用户不动它时提交的就是空串；把空串当"清空密钥"，等于每次改个模型名就把
        # 密钥抹掉一次。要清空只有 `clear_api_key=True` 一条路。
        next_key = current.api_key
    else:
        next_key = _check_text(api_key, label="密钥", limit=MAX_KEY_CHARS)
        # 生效密钥可能是自己那行的、也可能是借用实例行的（`resolve()` 的回落链），
        # 所以比法要用**生效值**而不是只看自己那行：用户看到的那段打码值就是从
        # 生效值算出来的，拿自己那行（空的）去比会在"借用公共配置"这一路上漏掉。
        effective_key = resolve().api_key
        if next_key and next_key in (mask_api_key(current.api_key),
                                     mask_api_key(effective_key)):
            # 提交的是**打码后的样子**（`…abcd`），不是密钥本身。把它当新密钥存下去，
            # 库里那把真密钥就被这个字面量顶掉了：接口照旧回 `api_key_set: true`、
            # 来源显示"你在面板里设的"，而之后每一次模型调用都 401——坏在"看起来
            # 刚配好"上。静默当成"没改"也不行：用户以为保存成功，实际什么都没发生。
            # 宁可 400 说清楚，与 `clear_api_key` 撞车时同一条规矩。
            #
            # 两个比法都要：`current.api_key` 是库里那一行，密钥只来自环境变量时它
            # 是空的，而界面上那段打码值正是从环境变量那把算出来的——只比库会在
            # "密钥来自环境变量"这一路上漏掉，而这一路最疼：打码值一旦落库，就
            # 顶掉了环境变量里那把真密钥，且 `api_key_source` 会说"你在面板里设的"。
            raise SettingsError(
                "settings_api_key_masked",
                "提交的是打码后的密钥（形如 …abcd），不是密钥本身；"
                "要改密钥请重新粘贴完整的值，不改就别动这一栏。",
                status=400)
    next_model = (current.model if model is None
                  else _check_text(model, label="模型名", limit=MAX_MODEL_CHARS))
    next_deep = (current.deep_model if deep_model is None
                 else _check_text(deep_model, label="深度思考模型名", limit=MAX_MODEL_CHARS))

    # 密钥的锚点。三种情形，对应三件不同的事，与上面 `next_key` 那三段一一对应：
    #
    #   - 提交了新密钥 → 锚点就是**这次一起生效的那个地址**。用户粘贴密钥的那一刻
    #     已经认可了"这把钥匙给这台机器"。
    #   - 没提交新密钥 → 锚点**原样保留**。老行没有锚点时用旧地址算出来再写下来
    #     （`_binding_of` 的兜底），刻意落库而不是每次读时算：地址一变，落库的旧锚点
    #     就与新地址对不上，密钥自动不再交出。若把兜底留在读侧算，它用的会是
    #     **已经改过的新地址**，锚点跟着地址一起漂，绑定等于不存在。
    #   - 清空密钥 → 锚点一起清掉。留着它会让将来新填的密钥"继承"一个它没被验证过
    #     的地址。
    #
    # 判据是"**这次请求提交了密钥**"，不是"提交的密钥与库里那把不同"：`unbound` 的
    # 恢复动作就是"重新粘贴一次"，而用户粘回来的极可能就是同一把（他没换过供应商，
    # 只是换了地址）。拿 `next_key != current.api_key` 当判据时，这种最正常的恢复
    # 姿势会静默失败——界面提示"重新粘贴一次即可"，粘完仍是 `unbound`，而那时
    # 用户唯一能想到的解释是"这个功能坏了"。提交了密钥就是一次明确的"我认可这个
    # 地址"，与它是否等于旧值无关。攻击者这边不受影响：他改地址只能不带密钥，
    # 带密钥得先有密钥，而打码值那一道已经在上面挡掉了。
    old_host = _norm_text(current.api_key_host).lower() or _host_of(current.base_url)
    if clear_api_key:
        next_host = ""
    elif (api_key or "").strip():
        next_host = _host_of(next_base) or _host_of(os.getenv(BASE_URL_ENV, ""))
    else:
        next_host = old_host

    changed = (next_base, next_key, next_model, next_deep, next_host) != (
        current.base_url, current.api_key, current.model, current.deep_model,
        _norm_text(current.api_key_host).lower())
    if changed:
        try:
            with closing(_connect(timeout=SETTINGS_LOCK_TIMEOUT)) as conn, conn:
                conn.execute(
                    f"""INSERT INTO {TABLE}
                            (owner, base_url, api_key, model, deep_model, updated_at,
                             api_key_host)
                        VALUES (?, ?, ?, ?, ?, {_NOW}, ?)
                        ON CONFLICT(owner) DO UPDATE SET
                            base_url     = excluded.base_url,
                            api_key      = excluded.api_key,
                            model        = excluded.model,
                            deep_model   = excluded.deep_model,
                            updated_at   = excluded.updated_at,
                            api_key_host = excluded.api_key_host""",
                    (owner, next_base, next_key, next_model, next_deep, next_host),
                )
        except sqlite3.Error as exc:
            # 与 `_read_strict_for_save` 同一条规矩：写不进去也要给出能读的 code。
            # 裸 `OperationalError` 冒到 FastAPI 会变 500 + 一段 traceback，面板
            # 只能显示一句通用错误——而这条路上最可能发生的事恰恰是"过几秒就好了"。
            raise SettingsError(
                "settings_db_unavailable",
                "配置表正被占用，这次没有保存。请过几秒重试。",
                status=503) from exc
        # 写完立刻作废：这个功能的整个卖点是"保存即生效"，TTL 只能兜跨进程，
        # 不能给同进程的正常路径加延迟。
        _invalidate()
    return resolve(), changed


def clear(owner: str | None = None) -> int:
    """清空配置行（只在用例与本地调试用）。返回删掉的行数。

    `owner=None` 清**所有行**——用例与调试要的是"从头来一遍"。真实接口不清全表，
    它走 `save(..., clear_api_key=True)` 或写空值，落点由 `scope` 决定。
    """
    global _DEGRADED_WARNED
    _DEGRADED_WARNED = False
    with closing(_connect()) as conn, conn:
        if owner is None:
            rowcount = conn.execute(f"DELETE FROM {TABLE}").rowcount
        else:
            rowcount = conn.execute(
                f"DELETE FROM {TABLE} WHERE owner = ?", (owner,)).rowcount
    _invalidate()
    return rowcount


__all__ = [
    "API_KEY_ENV",
    "BASE_URL_ENV",
    "DEEP_MODEL_ENV",
    "FREE_REASONING_EFFORT",
    "INSTANCE_OWNER",
    "LLM_MODE_ENV",
    "MODEL_ENV",
    "Ready",
    "Resolved",
    "SOURCE_ENV",
    "SOURCE_FALLBACK",
    "SOURCE_FREE",
    "SOURCE_INSTANCE",
    "SOURCE_PUBLIC",
    "SOURCE_SAVED",
    "SOURCE_UNBOUND",
    "SOURCE_UNSET",
    "SETTINGS_LOCK_TIMEOUT",
    "SettingsError",
    "TABLE",
    "bound_host",
    "clear",
    "deep_override",
    "ensure_schema",
    "free_tier_endpoint",
    "host_of",
    "llm_mode",
    "mask_api_key",
    "owner_of",
    "resolve",
    "save",
    "saved",
    "updated_at",
]
