"""用户偏好的存取：一张 keyed 表，一个写入源。

**为什么是一层而不是三层**：`docs/RESEARCH-AGENT-MEMORY.md` 核过 WorkBuddy 的官方
文档，那里不存在"全局 / 会话 / 工作区"的分层，那是第三方教学仓的分法。而且我们
真正缺的只有一层——**跨请求、关于这个人的稳定事实**。另两层早就有对应物，只是
不叫这个名字：

  - 会话 → `runs` + `snapshot`（每次运行的身份、请求摘要、候选树，已实现）
  - 工作区 → `mindmaps.content`（这张图里有什么）

所以这里不做"统一记忆接口"，只补缺的那一层。

**为什么是 keyed 而不是一段自由文本**：`pref_key` 做主键，同一件事只能有一条。
"4 层"和"6 层"不会两条并存互相矛盾——写就是 upsert，后写的赢。自由文本做不到
这件事：它只能靠"更晚的那条覆盖更早的"，而一旦两句话说的是不同侧面（"喜欢鱼骨图"
与"节点别超过 8 个字"），覆盖就变成了互相抹除。

**三类偏好，代价完全不同**：

  - A 类（`diagram_type.default` / `thinking_model.default`）是结构化字段，正确
    位置在**请求层**：让模型"不必记"，而不是让模型"记着"。一个
    `diagram_type: "fishbone"` 比十行"用户喜欢鱼骨图"的 prompt 可靠得多。
  - B 类（`icon.enabled`）同样是结构化开关，只影响生成侧的后处理。
  - C 类（`note.*`）是自由文本，是**唯一进 prompt 的**。它的价值在于"这个人怎么
    说话、喜欢什么样的取舍"，这类东西没有字段可放。

D 类（项目/主题事实）**不做**：goal 每次自带主题、snapshot 里是全树，记这个是记
已有的东西，只会引入不一致。

**只有一个写入源**：用户显式动作。模型没有写记忆的权限，连"建议"都不由它生成。
实测依据：06:02 用户说「机器学习，用鱼骨图」，06:04 又说「机器学习，树状图」——
同一主题、两分钟内、两个相反的图型要求。任何自动总结都会在这里写出自相矛盾的
两条。派生只能产出**候选建议**（`suggestions()`），必须经用户确认才写。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from typing import Literal, get_args, get_origin

from pydantic import BaseModel, ConfigDict

from app.agent.contracts import AgentRunRequest
from app.auth import owner
from app.contracts.mindmap import DIAGRAM_LABELS
from app.core.errors import AppError
from app.generation.service import THINKING_MODELS
from app.mindmaps import repository

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 键

#: A 类：下次新建时的默认图型。`None`（模型自己判断）不是一条能存的值——
#: 那条偏好的语义是"我每次都想要某一种"，而"每次都让模型判断"等于没有偏好，
#: 删掉这条即可。
DIAGRAM_KEY = "diagram_type.default"

#: A 类：下次新建时的默认拆解方式。
THINKING_KEY = "thinking_model.default"

#: B 类：生成时要不要给一级分支 emoji。
ICON_KEY = "icon.enabled"

#: B 类：生成前要不要先去外部（中文维基 / OpenAlex / Crossref / arXiv / 快懂百科）
#: 搜一轮资料，把命中转成 `Source` 再走 RAG 链（`agent/tools.external_knowledge_enabled`
#: 是唯一的读点）。
#:
#: **为什么是偏好而不是请求字段**：它描述的是"这个人愿意为每一次生成多等 4 秒、换
#: 看得见的出处吗"，是一个跨请求的稳定事实；做成请求字段的话，用户每点一次生成都要
#: 重新回答同一个问题。偏好还有一个额外的好处——它落在 `user_prefs` 里，面板能改、
#: 能删，"上次生效"那个死表探测器顺带就有了。
#:
#: **它只是半个开关**：部署方在环境变量里关掉时（`MINDMAP_EXTERNAL_KNOWLEDGE=0`），
#: 这条偏好改不动任何东西。两个条件是与关系，判定写死在一个函数里。
EXTERNAL_SEARCH_KEY = "external_search.enabled"

#: C 类前缀。`note.1` / `note.2` …… 是自由文本备注，**唯一进 prompt 的东西**。
NOTE_PREFIX = "note."

#: C 类上限：8 条 / 500 字。超了**拒绝写入**，不做静默截断——静默截断等于用户以为
#: 记住了。8 条的理由是它比"我记得的所有事"少得多，逼用户挑出真正稳定的那几条；
#: 500 字约等于 prompt 里 300 token，对一次 20-70 秒的生成是背景噪音的量级。
MAX_NOTES = 8
MAX_NOTE_CHARS = 500

#: 单条偏好值的长度上限。备注按总字数管，这条是防"一条就把预算撑死"的兜底。
MAX_VALUE_CHARS = 500

#: 一条键的最大长度。它进 URL 路径与日志，长键只会在排查时变成噪音。
MAX_KEY_CHARS = 64

SOURCE_USER_EXPLICIT = "user_explicit"
SOURCE_SUGGESTION_ACCEPTED = "suggestion_accepted"
#: 来源的值域。写成 `Literal` 而不是 `str`：它会进 OpenAPI，前端镜像那边是同一个
#: 字面量联合——两处对齐靠契约，不靠"记得改第二份"。
PrefSource = Literal["user_explicit", "suggestion_accepted"]
SOURCES: tuple[str, ...] = get_args(PrefSource)

#: 时间格式与 `mindmaps` / `runs` / `library_*` 完全一致。三处格式不同会在"谁更晚"
#: 上错一天——这是仓库里写过的教训（`agent/runs.py:_NOW`、`knowledge/library.py`）。
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

#: 建议窗口：最近几次都选了同一个值才算"你一直都这样"。
#:
#: 3 而不是 5：图型使用率实测是 0/12，用户刚开始表达偏好时样本极少，5 条窗口要
#: 等五轮才开口。3 的代价是偶尔在"连着三次要流程图"之后给一次建议——而建议的
#: 全部成本是一句话加一个按钮，用户不点就什么都不发生。
SUGGESTION_WINDOW = 3

#: 统计建议时最多回看多少行 `runs`。上界不是"最近几次"，而是**防止全表扫**：
#: 有人连续跑五十次都不带图型时，回看 3 行会什么都找不到。
_SUGGESTION_SCAN = 40

#: "上次生效"最多回看多少行 `runs`。这个数与 `_SUGGESTION_SCAN` 分开：一个是
#: "谁在用这个面板"的探测，一个是"建议"的证据。200 是台账的典型条数（`pending()`
#: 默认 20，前端翻页也就到这里），再往前的运行与"现在这条偏好还灵不灵"无关。
_APPLIED_SCAN = 200


class PrefError(AppError):
    """写入被拒。带上业务 code，路由层不必把一句话再翻译成一种错误。"""


class Pref(BaseModel):
    """一条偏好。

    `last_applied_at` 不是存在 `user_prefs` 里的列，是**反查**出来的：它来自
    `runs.request.prefs_applied`，也就是"哪几次运行真的带着这条偏好跑过"。
    这一位是这张表的死表探测器——写进去却从来没有任何一次运行报告它，就说明
    读路径根本没接上，而这件事不需要看日志，打开面板就能看出来。

    匹配键是 `key=value` 整串而不是 `key`：用户把鱼骨图改成流程图之后，旧值的
    生效记录不该继续挂在它身上，界面该如实回到"尚未生效"。
    """

    model_config = ConfigDict(extra="forbid")
    key: str
    value: str
    scope: str = "global"
    source: PrefSource = "user_explicit"
    source_run_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    last_applied_at: str | None = None


class Saved(BaseModel):
    """`set()` 的结果。

    `changed=False` 是"这次提交没有改变任何东西"——同值重复提交不刷 `updated_at`。
    不返回这个布尔值，界面上就会出现"我刚设过"的假象：时间戳变了，实际什么都没变。
    """

    model_config = ConfigDict(extra="forbid")
    pref: Pref
    changed: bool


class Suggestion(BaseModel):
    """一条**候选**建议。它不是偏好，落库必须经过用户点一次。"""

    model_config = ConfigDict(extra="forbid")
    key: str
    value: str
    label: str
    count: int
    message: str


# ---------------------------------------------------------------- 值域


def _literal_strings(annotation: object) -> tuple[str, ...]:
    """从字段注解里取出 `Literal` 允许的字符串全集。

    直接读 `AgentRunRequest` 的注解，而不是在别处再抄一份枚举：抄一份的必然结局
    是某天两边分叉，而分叉的表现是"记忆里存了一个后端不认的值"——写的时候通过，
    下次预填进请求时 422，用户看到的是一次莫名其妙的校验失败。

    两种形状都要收：`thinking_model` 直接是 `Literal[...]`，`diagram_type` 是
    `Literal[...] | None`。所以既看注解自身，也看它 `get_args()` 出来的每一项。
    """
    pool: list[str] = []
    for candidate in (annotation, *get_args(annotation)):
        if get_origin(candidate) is Literal:
            pool.extend(value for value in get_args(candidate) if isinstance(value, str))
    return tuple(pool)


_FIELDS = AgentRunRequest.model_fields

#: A / B 类的值域。前两条**来自契约本身**，后两条是这个开关自己的两个状态。
_ALLOWED: dict[str, tuple[str, ...]] = {
    DIAGRAM_KEY: _literal_strings(_FIELDS["diagram_type"].annotation),
    THINKING_KEY: _literal_strings(_FIELDS["thinking_model"].annotation),
    ICON_KEY: ("true", "false"),
    EXTERNAL_SEARCH_KEY: ("true", "false"),
}

#: 建议能从 `runs.request` 统计出来的字段：(偏好键, 请求字段)。
_SUGGESTIBLE: tuple[tuple[str, str], ...] = (
    (DIAGRAM_KEY, "diagram_type"),
    (THINKING_KEY, "thinking_model"),
)


def _label(key: str, value: str) -> str:
    """值的人话名字。界面与建议文案共用这一份，免得同一个值有两种叫法。"""
    if key == DIAGRAM_KEY:
        return DIAGRAM_LABELS.get(value, value)
    if key == THINKING_KEY:
        return THINKING_MODELS.get(value, {}).get("label", value)
    if key == ICON_KEY:
        return "给节点配图标" if value == "true" else "不给节点配图标"
    if key == EXTERNAL_SEARCH_KEY:
        # 措辞刻意说"联网"而不是"外部知识"：用户能判断的是"要不要上网"，"外部
        # 知识"听起来像一个内部功能的开关。
        return "没材料时先联网搜资料" if value == "true" else "没材料时不联网，直接生成"
    return value


def _check_key(key: str) -> str:
    """键的结构：`[a-z0-9_.-]`、不超过 64 字、不以点开头或结尾。

    收窄字符集不是为了好看：键会进 `PUT /api/prefs/{pref_key}` 的路径。放开 `/`
    与 `..` 会先在路由层制造一串"看起来像另一个接口"的路径。
    """
    text = (key or "").strip()
    if not text or len(text) > MAX_KEY_CHARS:
        raise PrefError("pref_key_invalid",
                        f"偏好键必须是 1–{MAX_KEY_CHARS} 个字符。", status=400)
    if text != key:
        raise PrefError("pref_key_invalid", "偏好键不能带首尾空白。", status=400)
    if not all(char.isascii() and (char.isalnum() or char in "_.-") for char in text):
        raise PrefError("pref_key_invalid",
                        "偏好键只能用小写字母、数字与 _ . - 三种符号。", status=400)
    if text.startswith(".") or text.endswith("."):
        raise PrefError("pref_key_invalid", "偏好键不能以点开头或结尾。", status=400)
    return text


def _check_value(key: str, value: str, existing: dict[str, Pref]) -> str:
    """值域与容量。返回**要落库的值**（备注会去掉首尾空白）。

    三类各有各的拒绝方式，共同点是"拒绝并说清怎么改"，没有一处静默截断：

      - A / B 类：枚举外的值直接拒。它明天要进请求体，而请求层的 Literal 会 422；
        在写入处拒，用户看到的是"这个值不行"，在预填处才炸，用户看到的是一次
        莫名其妙的失败。
      - C 类：条数与总字数都拒。上限是**用户的预算**，不是我们的存储限制——
        静默截断等于让他以为自己记下了。
    """
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise PrefError("pref_value_empty", "偏好值不能为空；要清掉这条请删除它。", status=400)
    if len(text) > MAX_VALUE_CHARS:
        raise PrefError("pref_limit_exceeded",
                        f"一条偏好最多 {MAX_VALUE_CHARS} 字，这条有 {len(text)} 字。",
                        status=400)

    allowed = _ALLOWED.get(key)
    if allowed is not None:
        if text not in allowed:
            raise PrefError(
                "pref_value_invalid",
                f"{key} 只能取 {' / '.join(allowed)} 之一，收到 {text!r}。",
                status=400, detail={"allowed": list(allowed)},
            )
        return text

    if not key.startswith(NOTE_PREFIX):
        raise PrefError(
            "pref_key_unknown",
            f"不认识的偏好键 {key!r}。可写的是 {', '.join(sorted(_ALLOWED))}，"
            f"以及 {NOTE_PREFIX}* 开头的自由文本备注。",
            status=400,
        )

    # C 类：条数与总字数。统计时**排除自己**——改一条已有备注不该被自己的旧值挡住。
    others = [row for name, row in existing.items()
              if name.startswith(NOTE_PREFIX) and name != key]
    if key not in existing and len(others) >= MAX_NOTES:
        raise PrefError(
            "pref_limit_exceeded",
            f"备注最多 {MAX_NOTES} 条，已经有 {len(others)} 条了。请先删除一条再加。",
            status=400, detail={"limit": MAX_NOTES, "count": len(others)},
        )
    total = sum(len(row.value) for row in others) + len(text)
    if total > MAX_NOTE_CHARS:
        raise PrefError(
            "pref_limit_exceeded",
            f"备注合计最多 {MAX_NOTE_CHARS} 字，加上这条会变成 {total} 字。"
            f"请先删除一条，或把这条改短一点。",
            status=400, detail={"limit": MAX_NOTE_CHARS, "total": total},
        )
    return text


# ---------------------------------------------------------------- 读


def _row(row: sqlite3.Row) -> Pref:
    return Pref(
        key=row["pref_key"],
        value=row["pref_value"],
        scope=row["scope"],
        source=row["source"],
        source_run_id=row["source_run_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _connect() -> sqlite3.Connection:
    # 表在 `repository.connect()` 里与 `mindmaps` / `runs` 同一时刻建好，所以这里
    # 没有任何"发现表不存在"的分支。
    return repository.connect()


def _owner() -> str:
    """这一行数据归谁。**每次读写都现算**，不缓存：同一个进程里并发的两个请求
    会看到各自的身份，缓存它就等于把 A 的偏好在 B 的请求里用一遍（`ContextVar`
    存在的唯一理由就是这个）。

    加账号之前写下的那些行（`owner = ''`）由部署方在第一次访问时继承，见
    `_adopt_legacy()`。
    """
    return owner.current_owner()


def _adopt_legacy() -> None:
    """把"加账号之前"写下的偏好一次性记到部署方名下。

    老库里的 `user_prefs` 行没有 owner 可言——那时这台机器只有一个用户，而这个人
    只可能是部署它的人。不继承的后果是：单用户装升级上来，用户打开面板发现自己的
    备注、图型、图标开关全没了，而库里那些行还在、只是没人读得到。

    **只部署方能触发**，与 `mindmaps.update_map(allow_unowned=...)` 同一个判据：
    多用户部署里老行不能被随机某个人继承走。做完之后 `owner = ''` 的行就不再有，
    所以这段只在升级后的第一次访问里真的做一次活。
    """
    if not owner.is_deployer():
        return
    mine = _owner()
    if not mine:
        return
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE user_prefs SET owner = ? WHERE owner = ''", (mine,))


def rows() -> list[Pref]:
    """当前这个人自己的偏好，**不带** `last_applied_at`。

    `set` / `get` / `prompt_notes` 走这条：它们不关心"上次什么时候生效"，
    而反查 `runs` 是这张表之外的一次扫描。
    """
    _adopt_legacy()
    with closing(_connect()) as conn:
        # 备注按写入先后排（`note.1` < `note.10`），A/B 类按更新时间。两类混在一起
        # 排序没有意义，所以键在前、时间在后：同一类内部的相对顺序稳定就够。
        result = conn.execute(
            "SELECT * FROM user_prefs WHERE owner = ? ORDER BY pref_key",
            (_owner(),)).fetchall()
    return [_row(row) for row in result]


def get(key: str) -> Pref | None:
    _adopt_legacy()
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM user_prefs WHERE owner = ? AND pref_key = ?",
            (_owner(), key)).fetchone()
    return _row(row) if row else None


def _last_applied() -> dict[str, str]:
    """`key=value` -> 最近一次带着它生成成功的 `updated_at`。

    数据来自 `runs.request.prefs_applied`（每次运行由前端按"界面上显示的值来自
    偏好"标出来）。在 Python 里展开而不是用 SQLite 的 `json_each`：`json1` 在
    发行版之外不一定编译进去，而这张表在这个量级上（几十到几百行）解析一遍
    只要几毫秒。真到了上万行，该加的是索引与 TTL，不是把查询写得更早。

    **只看自己那几次运行**：这是"这条偏好上次真的生效是什么时候"，别人的运行
    与这个人的偏好无关——跨账号读会让界面上的日期变成"这台机器上有人用过它"。
    """
    with closing(_connect()) as conn:
        # `updated_at` 必须一起 SELECT：SQLite 允许在 `ORDER BY` 里用没取出来的列，
        # 但取不到它的值。少了这一列，界面显示的"上次生效"会变成"我们什么时候查
        # 的"——一个永远等于今天的日期，看着很好，其实是假的。
        records = conn.execute(
            "SELECT request, updated_at FROM runs WHERE status = 'done' AND owner = ? "
            "ORDER BY updated_at DESC, run_id LIMIT ?",
            (_owner(), _APPLIED_SCAN),
        ).fetchall()
    applied: dict[str, str] = {}
    for record in records:
        try:
            payload = json.loads(record["request"] or "{}")
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        for item in payload.get("prefs_applied") or []:
            if isinstance(item, str) and item not in applied:
                applied[item] = record["updated_at"]
    return applied


def list_all() -> list[Pref]:
    """面板要的完整视图：所有偏好 + 每条的"上次生效"。"""
    applied = _last_applied()
    result = rows()
    for row in result:
        row.last_applied_at = applied.get(f"{row.key}={row.value}")
    return result


# ---------------------------------------------------------------- 写


def set(  # noqa: A001 — 与 `list_all` / `get` / `delete` 同一组动词，读写对称
    key: str,
    value: str,
    *,
    source: str = SOURCE_USER_EXPLICIT,
    source_run_id: str | None = None,
) -> tuple[Pref, bool]:
    """写入一条偏好，返回（写入后的行，这次有没有真的改变）。

    **同值重复提交返回 `changed=False` 且不刷 `updated_at`**：这个时间戳是"这条
    最近一次被改动"，而 C 类的注入顺序、界面的"上次更新"都读它。用户连点两次
    "以后都这样"，把它刷成"刚刚"，界面上就会出现一条看起来刚设过、其实一直没变
    的偏好——那正是"我设了但它不生效"这类误判的起点。

    upsert 的 `WHERE pref_value <> excluded.pref_value` 就是这条语义的实现：
    条件不满足时 SQLite 什么都不做，`rowcount` 为 0。
    """
    _check_key(key)
    if source not in SOURCES:
        raise PrefError("pref_source_invalid",
                        f"来源只能是 {' / '.join(SOURCES)} 之一。", status=400)
    text = _check_value(key, value, {row.key: row for row in rows()})
    with closing(_connect()) as conn, conn:
        changed = conn.execute(
            f"""INSERT INTO user_prefs
                    (owner, pref_key, pref_value, scope, source, source_run_id,
                     created_at, updated_at)
                VALUES (?, ?, ?, 'global', ?, ?, {_NOW}, {_NOW})
                ON CONFLICT(owner, pref_key) DO UPDATE SET
                    pref_value    = excluded.pref_value,
                    source        = excluded.source,
                    source_run_id = excluded.source_run_id,
                    updated_at    = excluded.updated_at
                WHERE user_prefs.pref_value <> excluded.pref_value""",
            (_owner(), key, text, source, source_run_id),
        ).rowcount == 1
    written = get(key)
    if written is None:  # pragma: no cover — upsert 成功路径上必有一行
        raise RuntimeError(f"user_prefs 里没有 {key!r} 的行，upsert 没有生效")
    written.last_applied_at = _last_applied().get(f"{key}={written.value}")
    return written, changed


def delete(key: str) -> bool:
    """删一条。返回它本来在不在。

    **不做 `deleted_at` 软删**：条目数是个位数，硬删更干净，而且能真正兑现"删了
    就没了"——软删的语义是"我还能翻出来"，而这里用户要的恰恰是"别再拿它影响我"。
    需要审计时看的是 `runs.request.prefs_applied`（历史运行带着什么跑过），不是
    一张永远在长的墓碑表。
    """
    with closing(_connect()) as conn, conn:
        return conn.execute(
            "DELETE FROM user_prefs WHERE owner = ? AND pref_key = ?",
            (_owner(), key)).rowcount == 1


def clear() -> int:
    """清空（只在用例与本地调试用）。

    **只清当前这个人自己的**。加账号之前它是 `DELETE FROM user_prefs`（整张表），
    而一个"本地调试用"的函数清掉全站所有账号的备注，是那种只在多用户部署上出现、
    且看起来像"我的备注自己没了"的故障。
    """
    with closing(_connect()) as conn, conn:
        return conn.execute(
            "DELETE FROM user_prefs WHERE owner = ?", (_owner(),)).rowcount


# ---------------------------------------------------------------- 读路径：谁在用


def icons_enabled() -> bool:
    """生成时要不要给一级分支 emoji。没有这条偏好 = 保持原样（给）。"""
    row = get(ICON_KEY)
    return row is None or row.value != "false"


def external_search_enabled() -> bool:
    """用户这一侧允不允许生成时去联网搜资料。没有这条偏好 = 允许（默认开）。

    **只回答用户那一半**。"部署方在环境变量里关了没有"是另一个问题，由
    `agent/tools.external_knowledge_enabled()` 把两半合起来——那里是唯一该做
    与运算的地方（合在一处，才可能被测到、也才可能只有一种先后次序）。
    这个函数本身刻意不知道环境变量：让它在读的时候顺手判一次优先级，就会出现
    两个真相，而"运维关了、用户开了"这种组合恰好是最难靠日志看出来的。

    默认开而不是默认关：不设这条偏好的人绝大多数根本没打开过面板，把默认值定成
    "关"等于替他们做了那个决定——而他们从来没表达过"不想联网"。
    """
    row = get(EXTERNAL_SEARCH_KEY)
    return row is None or row.value != "false"


def _recent_choices(field: str, limit: int) -> list[str]:
    """`runs.request[field]` 最近的若干个**非空**值，新的在前。

    跳过空值是一件事关正确性的细节：`None`（"用户没说"）不是一次选择。把它算进
    窗口的话，"连着三次没说"会被读成"三次都选了同一个值"——而那个值根本不存在。

    只认 `done`：被取消/失败的那几次可能只是同一次意图的半截重试（前端每次点击
    都是新 run_id），拿它们当证据会让"最近 3 次"变成"刚才那 3 秒"。

    **只认自己那几次**：候选建议是"你最近三次都选了 X"，别人的三次不构成对
    这个人习惯的观察——那会让建议变成一句"这台机器上有人喜欢鱼骨图"。
    """
    with closing(_connect()) as conn:
        records = conn.execute(
            "SELECT request FROM runs WHERE status = 'done' AND owner = ? "
            "ORDER BY updated_at DESC, run_id LIMIT ?",
            (_owner(), _SUGGESTION_SCAN),
        ).fetchall()
    values: list[str] = []
    for record in records:
        try:
            payload = json.loads(record["request"] or "{}")
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            continue
        values.append(value)
        if len(values) >= limit:
            break
    return values


def suggestions() -> list[Suggestion]:
    """从历史运行里提出**候选**建议；一条都不成立时返回空列表。

    判据只有一条，写死在这里：**最近 `SUGGESTION_WINDOW` 次有效选择都是同一个值，
    且这个值与当前偏好不同**。

    三处刻意的不做：

      - 不统计没选的（`None`）——见 `_recent_choices`；
      - 值等于该键的内建默认值时不提（`thinking_model` 每次都发 `default`，否则
        用户会收到一条"要不要把默认值设成默认值"的建议）；
      - 不做任何自动写入——模型连建议都不生成，这条规则是纯代码的。
    """
    result: list[Suggestion] = []
    for key, field in _SUGGESTIBLE:
        recent = _recent_choices(field, SUGGESTION_WINDOW)
        if len(recent) < SUGGESTION_WINDOW:
            continue
        # 判"全都一样"用逐个比较而不是 `len(set(...))`：本模块的 `set` 是写入函数，
        # 它遮蔽了内置的那个名字。为了少写一行去 import builtins 不划算，而这个写法
        # 在 3 条窗口上比构造集合更直白。
        value = recent[0]
        if any(other != value for other in recent):
            continue
        if _is_builtin_default(key, value):
            continue
        current = get(key)
        if current is not None and current.value == value:
            continue
        label = _label(key, value)
        result.append(Suggestion(
            key=key, value=value, label=label, count=SUGGESTION_WINDOW,
            message=f"你最近 {SUGGESTION_WINDOW} 次都选了{label}，设为默认？",
        ))
    return result


def _is_builtin_default(key: str, value: str) -> bool:
    """这个值是不是"不设偏好时本来就会发生的那件事"。

    `thinking_model` 的默认是 `default`：每次请求都显式带它（前端不省略），所以
    三条 `default` 是**默认行为**，不是偏好表达。为它弹一句建议，等于把"用户什么
    都没改"读成"用户表达了一个意愿"。
    """
    return key == THINKING_KEY and value == "default"


# ---------------------------------------------------------------- 注入：只有 C 类


#: 备注那一段的表头。措辞刻意压在"背景"这一侧：它是"这个人是谁"，不是"这次做
#: 什么"，所以排在意图规则**之前**，并且自己声明让位于本次指令。
NOTE_HEADER = "关于这个人的长期偏好（只作语气与取舍的参考；与本次指令冲突时以本次指令为准）："


def prompt_notes() -> tuple[str, int]:
    """C 类备注 → 进 `_system_text` 的那一段，以及**被截掉的条数**。

    排序是 `updated_at DESC`，取前 `MAX_NOTES` 条；超过 `MAX_NOTE_CHARS` 时从
    **最旧的**截断（从最新往旧累加，放不下就停，而不是跳过它去装更早的）。

    返回 `dropped` 而不是静默丢弃：`AGENTS.md` 那条"降级要留下降过级的痕迹"在这里
    的落法就是让调用方记一条日志。用户看到的是"我写了 8 条，它好像只用了 5 条"，
    而排查的人能在日志里看到是哪一刻、因为哪个上限。

    **不做检索**：个位数条目全表扫就够，`pref_key` 本身就是索引。2C2G 上引入 BM25
    或向量是纯负债——它解决的问题（十万条里找相关）我们不存在。
    """
    notes = [row for row in rows() if row.key.startswith(NOTE_PREFIX)]
    if not notes:
        return "", 0
    notes.sort(key=lambda row: (row.updated_at, row.key), reverse=True)
    budget = MAX_NOTE_CHARS
    kept: list[str] = []
    for row in notes[:MAX_NOTES]:
        cost = len(row.value)
        if cost > budget:
            break
        budget -= cost
        kept.append(row.value)
    dropped = len(notes) - len(kept)
    if not kept:
        return "", dropped
    return NOTE_HEADER + "\n" + "\n".join(f"- {value}" for value in kept), dropped


__all__ = [
    "DIAGRAM_KEY",
    "EXTERNAL_SEARCH_KEY",
    "ICON_KEY",
    "MAX_NOTES",
    "MAX_NOTE_CHARS",
    "NOTE_PREFIX",
    "Pref",
    "PrefError",
    "SOURCE_SUGGESTION_ACCEPTED",
    "SOURCE_USER_EXPLICIT",
    "SUGGESTION_WINDOW",
    "Saved",
    "Suggestion",
    "THINKING_KEY",
    "clear",
    "delete",
    "external_search_enabled",
    "get",
    "icons_enabled",
    "list_all",
    "prompt_notes",
    "rows",
    "set",
    "suggestions",
]
