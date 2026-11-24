"""用户风格画像的**第一层**：从历史产物里数出来的 8 个结构参数。零模型调用。

## 这一层与 `service.py` 的分工

```
user_prefs      用户【显式】说的   →  意图   （`service.py`，A/B/C 三类）
style_profile   系统【推断】的     →  观察   （本模块）
```

不塞进 `user_prefs` 的理由与 `memory_items` 那次一样（见 `app/memory/__init__.py`）：
混在一张表里会重演"我明明写了它却没生效"——用户的显式声明与系统的推断一旦同表，
"这条是谁的意思"就只能靠猜，而 `note.*` 与 `memory_items` 的关系上已经踩过一次。

## 为什么是 8 个参数，为什么都用中位数

8 个参数全是**形状**，不是内容：分支数 / 深度 / 每层宽度 / detail 长 / label 长 /
图标 / 配色 / 图型。它们的共同点是"不调模型也能从 `runs.candidate` 里数出来"，
所以这一层的边际成本是零。

**中位数而不是均值**：一次只出 2 支的运行不该把画像拉到 2（离群值对均值的拉动是
无界的，对中位数不是）。样本量 35 这个量级上，一次离群就能让均值偏 20%。

## 样本 < 5 时不启用

`MIN_SAMPLES = 5` 的依据不是统计学上的显著性（5 什么都证明不了），而是**方向**：
2–3 个样本算出来的中位数等于其中某一个样本本身，那时画像描述的不是"这个人的
习惯"，而是"上一次他碰巧怎么做了"。5 是"至少有一个值出现两次"的最小样本量，
再小就只是在复述最近一次。

## 为什么不带时间衰减（与第二层不同）

第二层带衰减（`profile.decay`），这一层不带。理由写在这里省得下次有人来"补一致"：

  - 这里是**分布**统计，中位数对"哪一天做的"不敏感；而衰减会让样本变成小数，
    `samples` 那一列就再也说不清是条数还是权重和；
  - `runs` 现在的窗口是 4 天（2026-11-16 → 11-19），衰减在 4 天里最多差 0.09，
    纯粹是噪声；
  - 真要让"最近的偏好变了"生效，需要的是**滑动窗口 + 变点检测**，不是给每个
    样本乘一个因子。那是另一个量级的事，而这一层现在的职责是"把形状说清楚"。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import statistics
from contextlib import closing
from dataclasses import dataclass, field, replace
from typing import Any, Iterator

from pydantic import TypeAdapter, ValidationError

from app.agent import shapes
from app.contracts.mindmap import DiagramContent

logger = logging.getLogger(__name__)

STYLE_TABLE = "style_profile"

#: 启用门槛。见模块 docstring 最后一节。
MIN_SAMPLES = 5

#: 置信度在多少个样本上饱和。取 12 而不是 5：`MIN_SAMPLES` 是"能不能用"的闸，
#: 这个数是"用了该多当真"的刻度。12 次运行之后，中位数被单次离群移动的概率
#: 降到很小（换一个样本，中位数的变化不超过相邻两个样本的间距）；再往上加，
#: 置信度的增长速度已经低于用户习惯本身的变化速度。
FULL_CONFIDENCE_SAMPLES = 12

#: 每层宽度"均匀 / 不均"的分界，判的是**变异系数**（`stdev / mean`）。
#:
#: 0.5 的依据来自本仓库的历史产物形状：模板化产出的宽度序列是 `[2,2,2]`（cv = 0）
#: 或 `[3,3,2]`（cv ≈ 0.16），而人工拆过的图常在 `[2,5,3,1]`（cv ≈ 0.55）。
#: 0.5 卡在这两档之间：低于它意味着"每支长一样"，那正是模板化的信号。
UNEVEN_CV = 0.5

#: 进提示词的最小置信度。低于它的参数不写进那一段——一段"大概是 4 层吧"的
#: 提示比不写更坏：模型会把它当成硬约束去凑。
PROMPT_MIN_CONFIDENCE = 0.3

#: 分类量的"决定性占比"：众数占不到这个比例时**不写进提示词**。
#:
#: 0.6 的理由与置信度不是一回事：置信度回答"这条结论有多可信"，这个数回答
#: "这条结论有没有可操作性"。一个 10 : 9 的分裂（本机就是这样：`none` 10 次、
#: `by_branch` 9 次），置信度算出来 0.48 够高，但写成"基本不用颜色"是**错的**——
#: 它把一半的行为当成了偏差。5:5 的正确说法是"两种都用"，而那句话对生成没有
#: 任何指导意义，不如不说。
DECISIVE_SHARE = 0.6

#: "有图标"的比例门槛：高于它才算"用图标"。0.02 而不是 0：一张 60 节点的图里有
#: 一个节点带图标，那是漏网，不是习惯。
ICON_SHARE = 0.02

#: 同一种形状最多计几票（见 `_dedupe`）。
#:
#: **为什么需要这一条**：`runs` 里同一个 goal 被重跑、同一段脚本被反复触发是常态
#: （本机 35 份产物里，`[1,3,3]`、零 detail 的那一种形状出现了 18 次，其中两次的
#: candidate 甚至逐字节相同）。它们不是 18 个独立的观察，是 1 个观察被重复了 18 次
#: ——照着原样数，中位数会被这批重复拉到 3，置信度算出来却是"样本充足"。
#:
#: 取 2 而不是 1：形状相同但内容不同的两次生成是正常使用（改一个词再跑一次），
#: 那种重复里带着"两次都收敛到同一个形状"的信息，值得留两票。第三次起就只是在
#: 重复同一件事了。
REPEAT_CAP = 2

#: 一次最多读多少条运行。`runs` 现在 51 行，这个上限只为病态增长兜底
#: （与 `memory.store.MAX_SCAN` 同一个理由：宁可少读几条，也不要在热路径上
#: 一次读进几百 MB 的 candidate）。
MAX_RUNS = 400

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

_CREATE = f"""CREATE TABLE IF NOT EXISTS {STYLE_TABLE} (
    owner      TEXT NOT NULL DEFAULT '',
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    samples    INTEGER NOT NULL,
    confidence REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (owner, key)
)"""

#: 老库里 `style_profile` 的主键只有 `key`。SQLite 改不了主键，只能改名重建
#: （与 `user_prefs` / `model_settings` 同一套两段式，理由也一样）。
_LEGACY_TABLE = f"{STYLE_TABLE}_legacy_v1"

_LEGACY_CREATE = f"""CREATE TABLE {_LEGACY_TABLE} (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    samples    INTEGER NOT NULL,
    confidence REAL NOT NULL,
    updated_at TEXT NOT NULL
)"""

#: 老表的行归谁：无主（`''`）。部署方第一次 `refresh()` 时
#: `_adopt_legacy()` 把它们认领到自己名下——单用户升级不丢已算好的画像，
#: 多人环境里这些行只可能是升级前那台机器主人的数据。
_NO_OWNER = ""

_CONTENT_ADAPTER: TypeAdapter[DiagramContent] = TypeAdapter(DiagramContent)

#: 8 个参数的键与中文名。顺序就是提示词里的顺序（从"几条"到"长什么样"）。
PARAMS: dict[str, str] = {
    "branches": "一级分支",
    "depth": "总深度",
    "width": "每层宽度",
    "detail_chars": "节点说明",
    "label_chars": "节点标题",
    "icons": "图标",
    "colors": "配色",
    "diagram_type": "图型",
}

#: 单位。提示词里拼在数字后面（"一级分支 5–8 条"）。
UNITS: dict[str, str] = {
    "branches": "条",
    "depth": "层",
    "detail_chars": "字",
    "label_chars": "字",
}


# ---------------------------------------------------------------- 值对象


@dataclass(frozen=True)
class Param:
    """一个参数在一次统计里的结果。

    `kind` 只有两种：`number`（连续量，报区间）与 `label`（分类量，报众数+占比）。
    分两种而不是统一成浮点：`"width": 0.61` 该读成"变异系数 0.61"还是"0.61 个
    宽度"，只有 `kind` 说得清；而提示词里要写的是"每层宽度不均"这句话，不是数字。

    所有字段都保留（`low`/`high`/`detail`），不只是"够用就行"：提示词只读一部分，
    但排查"画像为什么这么写"时要能一路看到原始分布——一个只返回中位数的接口
    让人无法区分"分布很窄"与"两次极值撑出来的中位数"。
    """

    key: str
    kind: str
    value: float | str
    low: float | None
    high: float | None
    samples: int
    confidence: float
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return PARAMS.get(self.key, self.key)

    def dump(self) -> str:
        return json.dumps({
            "kind": self.kind,
            "value": self.value,
            "low": self.low,
            "high": self.high,
            "samples": self.samples,
            "confidence": round(self.confidence, 4),
            "detail": self.detail,
        }, ensure_ascii=False, sort_keys=True)

    @classmethod
    def load(cls, key: str, body: str, samples: int, confidence: float) -> "Param":
        try:
            payload = json.loads(body or "{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        detail = payload.get("detail")
        return cls(
            key=key,
            kind=str(payload.get("kind") or "number"),
            value=payload.get("value") if payload.get("value") is not None else 0.0,
            low=payload.get("low"),
            high=payload.get("high"),
            samples=samples,
            confidence=confidence,
            detail=detail if isinstance(detail, dict) else {},
        )


@dataclass(frozen=True)
class Observation:
    """一次运行数出来的原始形状。**每个运行一份**，不做跨运行合并。

    样本单位是"一次运行"而不是"一个节点"：节点级的统计会让长过一次 60 节点的图
    在画像里占四份权重，而"这个人的一次生成通常长什么样"才是要回答的问题。
    """

    run_id: str
    branches: int
    depth: int
    widths: tuple[int, ...]
    detail_chars: float
    label_chars: float
    icon_ratio: float
    color_mode: str
    shape: str


# ---------------------------------------------------------------- 建表


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建表。由 `repository.connect()` 在同一连接上调用，幂等。

    与 `memory_items` / `library_sources` 建在同一时刻的理由一样：读它的路径
    （每次生成前拼提示词）在热路径上，多一个"发现表不存在 → 建表"的分支只会在
    最不该出错的地方多一条可能出错的路径。

    **加 `owner` 之后主键必须变成 `(owner, key)`**：画像描述的是"这个人的图长什么
    样"，一个全局画像等于让新用户在第一次生成时就拿到别人的形状结论——而那段文字
    是直接进提示词的，比列表里看到一行别人的数据更影响输出。SQLite 改不了主键，
    所以走两段式：改名 → 建新表 → 搬行 → 删老表。每一步都 commit（`connect()` 的
    调用方不会替我们提交，见 `settings.store._migrate_legacy` 的实测记录）。
    """
    conn.execute(_CREATE)
    columns = {
        str(row["name"]) for row in conn.execute(f"PRAGMA table_info({STYLE_TABLE})")}
    legacy = _table_exists(conn, _LEGACY_TABLE)
    if not columns or ("owner" in columns and not legacy):
        return
    if "owner" not in columns:
        # 老表：先改名，再建新表。改名之后 `PRAGMA table_info(STYLE_TABLE)` 是空集，
        # 所以后面必须重新查，不能复用上面的 `columns`。
        conn.execute(f"ALTER TABLE {STYLE_TABLE} RENAME TO {_LEGACY_TABLE}")
        conn.commit()
    conn.execute(_CREATE)
    if legacy or _table_exists(conn, _LEGACY_TABLE):
        conn.execute(
            f"INSERT OR IGNORE INTO {STYLE_TABLE} "
            f"(owner, key, value, samples, confidence, updated_at) "
            f"SELECT ?, key, value, samples, confidence, updated_at FROM {_LEGACY_TABLE}",
            (_NO_OWNER,))
        conn.commit()
        conn.execute(f"DROP TABLE IF EXISTS {_LEGACY_TABLE}")
        conn.commit()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return row is not None


def _owner() -> str:
    """当前请求的 owner。**只在有请求上下文的地方调**。

    与 `prefs.service._owner()` 同一条：线程里读不到 `ContextVar`，所以
    `refresh()` 这类可能被脚本/后台调用的入口必须把 owner 传成形参，
    这里只是"没人传时的缺省值"。
    """
    from app.auth import owner as owner_module

    return owner_module.current_owner()


def _adopt_legacy() -> None:
    """把升级前的无主画像认领给部署方（只在部署方调用时发生一次）。

    为什么不干脆把无主行当成"所有人的"：那会让每位新用户第一次生成就收到一份
    别人的风格结论。为什么不用"谁先来归谁"：部署方是这台机器的主人，那些行本来
    就是他的数据，判据用 `is_deployer()` 而不是"第一个读到的人"。
    """
    from app.auth import owner as owner_module

    if not owner_module.is_deployer():
        return
    try:
        with closing(_connect()) as conn, conn:
            conn.execute(f"UPDATE {STYLE_TABLE} SET owner = ? WHERE owner = ?",
                         (owner_module.current_owner(), _NO_OWNER))
    except Exception as exc:  # noqa: BLE001 — 认领失败不该让生成失败
        logger.warning("无主风格画像的认领没有成功（%s），这次按空画像处理",
                       type(exc).__name__)


def _connect() -> sqlite3.Connection:
    from app.mindmaps import repository

    return repository.connect()


# ---------------------------------------------------------------- 数一次运行


def _color_mode(content: DiagramContent) -> str:
    """配色习惯：`none` / `mono` / `by_branch` / `mixed`。

    判定顺序是刻意的：先问"有没有颜色"，再问"是不是一支一色"，最后才归到 mixed。
    反过来（先判 mixed）会让"全都没有颜色"落进 mixed，而"没有颜色"与"颜色乱用"
    是两种完全不同的习惯，提示词里该给的结论正好相反。

    `by_branch` 的定义是"每个一级分支的子树里只有一种颜色，且分支之间不同"。
    这正是"一色一支"这个界面上真实存在的用法（`NODE_COLORS` 八色正好够八支）。
    """
    branches = shapes.top_branches(content)
    if not branches:
        return "none"
    per_branch: list[set[str]] = []
    total = colored = 0
    for branch in branches:
        seen: set[str] = set()
        for node in shapes.walk_nodes(branch):
            total += 1
            if node.color:
                colored += 1
                seen.add(str(node.color))
        per_branch.append(seen)
    if not colored:
        return "none"
    if all(len(seen) == 1 for seen in per_branch):
        picked = [next(iter(seen)) for seen in per_branch]
        return "by_branch" if len(set(picked)) == len(picked) else "mono"
    return "mixed"


def observe(run_id: str, content: DiagramContent) -> Observation:
    """一次运行的 8 个原始量。纯函数，不读库。

    "深度"与"每层宽度"都走 `shapes.walk_depths`：它让每棵并列子树（鱼的每根骨、
    流程的每个阶段）各从第 1 层起算，与画布上的缩进一致。这样三套图型能用同一份
    统计口径，不必为鱼骨写第二套"深度"的定义——两套定义迟早会给出两个互相矛盾的
    结论，而画像是一次性拼进提示词的，没有第二个地方能纠错。
    """
    depths = [depth for _, depth in shapes.walk_depths(content)]
    widest = max(depths) if depths else 0
    widths: list[int] = []
    for level in range(1, widest + 1):
        widths.append(sum(1 for value in depths if value == level))
    nodes = list(shapes.iter_nodes(content))
    details = [len(node.detail) for node in nodes if node.detail]
    labels = [len(node.label) for node in nodes]
    icons = sum(1 for node in nodes if node.icon)
    return Observation(
        run_id=run_id,
        branches=len(shapes.top_branches(content)),
        depth=widest,
        widths=tuple(widths),
        detail_chars=(sum(details) / len(details)) if details else 0.0,
        label_chars=(sum(labels) / len(labels)) if labels else 0.0,
        icon_ratio=(icons / len(nodes)) if nodes else 0.0,
        color_mode=_color_mode(content),
        shape=shapes.kind_of(content),
    )


def _observations(owner: str) -> list[Observation]:
    """读出**这个人**所有可用的历史运行，逐条数形状。坏数据跳过并记数。

    `owner` 进 `WHERE`：这张表读的是 `runs.candidate`（整棵树）。跨账号读的后果
    不只是"看到别人的图"，而是把别人的形状统计进我的画像、再把结论写进我的提示词
    ——一段对别人成立、对我无根据的硬约束会一直跟着每一次生成。
    """
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT run_id, candidate FROM runs "
            "WHERE owner = ? AND status = 'done' AND candidate IS NOT NULL "
            "ORDER BY created_at DESC, run_id LIMIT ?",
            (owner, MAX_RUNS),
        ).fetchall()
    result: list[Observation] = []
    broken = 0
    for row in rows:
        try:
            content = _CONTENT_ADAPTER.validate_python(json.loads(row["candidate"]))
        except (ValueError, ValidationError, TypeError):
            broken += 1
            continue
        result.append(observe(str(row["run_id"]), content))
    if broken:
        # 一条解析不了就少一个样本，不该让整次统计失败——画像的降级方向是
        # "少说一句"，不是 "这次生成失败"（与 `memory.store.query_vector` 同向）。
        logger.warning("风格统计：%d 条运行产物解析失败，已跳过", broken)
    return result


# ---------------------------------------------------------------- 汇总


def _confidence(values: list[float], samples: int) -> float:
    """置信度：样本量因子 × 一致性因子，都落在 [0,1]。

    两个因子相乘而不是取最小值：一个"样本很多但形状天天变"的画像，与"形状很稳
    但只见过两次"的画像，可信度都该低——取最小值会让另一种缺陷被完全忽略。

    一致性用 `1 - min(1, iqr / median)`：中位数做分母而不是 p75，因为离散度要相对
    "这个参数通常多大"来读。median 为 0（例如一张图一个 detail 都没有）时退回
    `1 - min(1, iqr)`，此时 iqr 本身就是绝对离散度。
    """
    if samples < MIN_SAMPLES or not values:
        return 0.0
    size = min(1.0, samples / FULL_CONFIDENCE_SAMPLES)
    median = statistics.median(values)
    spread = _iqr(values)
    scale = median if median > 0 else 1.0
    steadiness = max(0.0, 1.0 - min(1.0, spread / scale))
    return round(size * steadiness, 4)


def _iqr(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    quartiles = statistics.quantiles(values, n=4, method="inclusive")
    return quartiles[2] - quartiles[0]


def _binary_confidence(values: list[float], threshold: float) -> float:
    """零膨胀的比例型参数（目前只有"图标用不用"）的置信度。

    不能直接套 `_confidence`：那张表上一半的运行是 0、另一半是 0.09，中位数 0.07
    的 IQR 覆盖了它自己，算出来 0——而事实是"绝大多数运行根本没有图标"，这是一个
    相当稳的结论。判据该问的是"有 / 没有"这两档有多稳定，不是"比例这个数有多稳"。

    门槛的意义：比例型参数在 0 附近时，绝对离散度与中位数同量级，任何基于
    `iqr / median` 的公式都会把"稳定地没有"读成"波动很大"。
    """
    if len(values) < MIN_SAMPLES:
        return 0.0
    with_icon = sum(1 for value in values if value > threshold)
    share = max(with_icon, len(values) - with_icon) / len(values)
    size = min(1.0, len(values) / FULL_CONFIDENCE_SAMPLES)
    return round(size * share, 4)


def _number(key: str, values: list[float]) -> Param:
    return Param(
        key=key,
        kind="number",
        value=round(statistics.median(values), 2) if values else 0.0,
        low=values and round(statistics.quantiles(values, n=4, method="inclusive")[0], 2),
        high=values and round(statistics.quantiles(values, n=4, method="inclusive")[2], 2),
        samples=len(values),
        confidence=_confidence(values, len(values)),
    )


def _label(key: str, values: list[str], detail: dict[str, Any] | None = None) -> Param:
    """分类量：众数 + 占比。占比就是置信度，不再另乘样本量因子。

    样本量在这里的作用已经体现在"占比"上了吗？没有——2 个样本全一样也会给出
    占比 1.0。所以仍然乘样本量因子，只是把占比当作一致性因子。
    """
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return Param(key=key, kind="label", value="", low=None, high=None,
                     samples=0, confidence=0.0)
    top, hits = max(counts.items(), key=lambda item: (item[1], item[0]))
    size = min(1.0, len(values) / FULL_CONFIDENCE_SAMPLES) if len(values) >= MIN_SAMPLES else 0.0
    share = hits / len(values)
    payload = {"counts": counts} if detail is None else {**detail, "counts": counts}
    return Param(
        key=key,
        kind="label",
        value=top,
        low=None,
        high=None,
        samples=len(values),
        confidence=round(size * share, 4),
        detail=payload,
    )


def _fingerprint(item: Observation) -> str:
    """一次运行的形状指纹，用于把"同一件事做了 N 次"折成两票。

    判据只用结构，不读 `goal`：`goal` 里的"再改一处"这种字面无法穷尽，靠它识别
    测试产物等于让画像去认几个中文短语；而形状是**必须**由产物给出的事实。
    detail / label 长度取整到 10 字一档，避免浮点抖动把同一形状算成两种。
    """
    return "|".join((
        str(item.branches), str(item.depth), ",".join(map(str, item.widths)),
        str(int(item.detail_chars // 10)), str(int(item.label_chars // 10)),
    ))


def _dedupe(observations: list[Observation]) -> tuple[list[Observation], int]:
    """同一形状最多留 `REPEAT_CAP` 条，返回（留下的，折叠掉几条）。

    留的是**最新的那几条**（输入已经是 `created_at DESC`），折叠掉的是更旧的：
    如果这个人的形状变了，留下旧的、折掉新的会把画像钉在过去。
    """
    seen: dict[str, int] = {}
    kept: list[Observation] = []
    folded = 0
    for item in observations:
        key = _fingerprint(item)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > REPEAT_CAP:
            folded += 1
            continue
        kept.append(item)
    return kept, folded


def summarize(observations: list[Observation]) -> dict[str, Param]:
    """35 次运行 → 8 个参数。样本不足 `MIN_SAMPLES` 时返回**空字典**。

    空字典而不是"全部 confidence=0 的参数"：调用方（`prompt_block` / 面板）只要
    判一个空就能决定"这次没有画像"，不必逐条比 confidence。返回半个画像那种形态
    会让每个调用点各写一遍"哪些算数"，而两处写法迟早不一致。

    **重复形状先折叠再统计**（`_dedupe`）。折叠掉多少条会记进每个参数的
    `detail["folded_runs"]`：一个"用了 34 条样本"的画像里如果有 18 条是同一个形状，
    读的人有权知道他看到的置信度是怎么算出来的。
    """
    usable = [item for item in observations if item.depth > 0]
    kept, folded = _dedupe(usable)
    if len(kept) < MIN_SAMPLES:
        logger.info("风格统计：折叠重复后可用样本 %d 条，少于 %d 条，不启用画像",
                    len(kept), MIN_SAMPLES)
        return {}
    usable = kept

    cvs: list[float] = []
    for item in usable:
        widths = [value for value in item.widths if value > 0]
        if len(widths) >= 2:
            mean = sum(widths) / len(widths)
            if mean > 0:
                cvs.append(statistics.pstdev(widths) / mean)
    params: dict[str, Param] = {
        "branches": _number("branches", [float(item.branches) for item in usable]),
        "depth": _number("depth", [float(item.depth) for item in usable]),
        "detail_chars": _number("detail_chars", [item.detail_chars for item in usable]),
        "label_chars": _number("label_chars", [item.label_chars for item in usable]),
        "icons": replace(
            _number("icons", [item.icon_ratio for item in usable]),
            # 零膨胀分布：`_number` 给的中位数是对的（"通常 0.07 的节点带图标"），
            # 但它那套离散度判据会把这个参数的置信度算成 0。见 `_binary_confidence`。
            confidence=_binary_confidence(
                [item.icon_ratio for item in usable], ICON_SHARE),
        ),
        "width": _label(
            "width",
            ["uneven" if cv >= UNEVEN_CV else "even" for cv in cvs],
            {"cv_median": round(statistics.median(cvs), 3) if cvs else None},
        ),
        "colors": _label("colors", [item.color_mode for item in usable]),
        "diagram_type": _label("diagram_type", [item.shape for item in usable]),
    }
    # `branches` / `depth` / `icons` 的样本数与 `usable` 相同，`width` 少一种
    # （宽度只有一层的图没有变异系数）。统一把 `samples` 写成实际参与计算的条数，
    # 而不是 `len(usable)`——那一列的唯一用途就是"这条结论建立在几条上"。
    if folded:
        raw = len(usable) + folded
        params = {
            key: replace(param, detail={
                **param.detail, "folded_runs": folded, "raw_samples": raw})
            for key, param in params.items()
        }
        logger.info("风格统计：原始 %d 条里有 %d 条是重复形状，已折叠（每形状最多 %d 票）",
                    raw, folded, REPEAT_CAP)
    return params


# ---------------------------------------------------------------- 落库与读回


def refresh(owner: str | None = None) -> dict[str, Param]:
    """重算**这个人**的画像并落库；样本不足时**清空他那一份**并返回空字典。

    清空而不是留着旧值：`style_profile` 描述的是"现在的数据支持什么结论"。样本
    被删到 5 条以下时留着 20 条时的结论，会让画像与它自称的依据脱节——而那正是
    "我明明写了它却没生效"的镜像（"它明明写着这样，可数据早就不支持了"）。

    **只删自己那份**（`WHERE owner = ?`）：清空是"我的依据没了"，不该顺手把别人的
    画像也抹掉——那是最难查的一种数据丢失（对方下次生成突然没有风格段，而他
    什么都没做）。
    """
    who = _owner() if owner is None else owner
    _adopt_legacy()
    params = summarize(_observations(who))
    with closing(_connect()) as conn, conn:
        if not params:
            conn.execute(f"DELETE FROM {STYLE_TABLE} WHERE owner = ?", (who,))
            return {}
        for item in params.values():
            conn.execute(
                f"""INSERT INTO {STYLE_TABLE}
                        (owner, key, value, samples, confidence, updated_at)
                    VALUES (?, ?, ?, ?, ?, {_NOW})
                    ON CONFLICT(owner, key) DO UPDATE SET
                        value      = excluded.value,
                        samples    = excluded.samples,
                        confidence = excluded.confidence,
                        updated_at = excluded.updated_at""",
                (who, item.key, item.dump(), item.samples, item.confidence),
            )
        # 参数集合是常量，但表里可能存着上一版实现留下的键：删掉不在 `PARAMS` 里的
        # 行，"8 个参数"这件事才是表本身的性质，而不是读的时候再过滤一遍。
        marks = ",".join("?" * len(PARAMS))
        conn.execute(
            f"DELETE FROM {STYLE_TABLE} WHERE owner = ? AND key NOT IN ({marks})",
            (who, *PARAMS))
    logger.info("风格画像已更新：%d 个参数（样本 %d–%d 条）", len(params),
                min(item.samples for item in params.values()),
                max(item.samples for item in params.values()))
    return params


def read(owner: str | None = None) -> dict[str, Param]:
    """读**这个人**现成的画像。空字典 = 没算过或样本不足。**不触发重算**。

    读与算分开：`read()` 在每次生成前被调一次，它必须是纯读（一次本地查询、
    零 HTTP）；重算要读几十条 candidate 并解析成 pydantic 模型，那是 `refresh()`
    的活，由脚本或面板按钮触发。
    """
    who = _owner() if owner is None else owner
    _adopt_legacy()
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT key, value, samples, confidence FROM {STYLE_TABLE} WHERE owner = ?",
            (who,)).fetchall()
    params = {
        str(row["key"]): Param.load(
            str(row["key"]), str(row["value"]),
            int(row["samples"]), float(row["confidence"]))
        for row in rows
    }
    return {key: params[key] for key in PARAMS if key in params}


# ---------------------------------------------------------------- 进提示词


def _decisive(param: Param) -> bool:
    """这个分类量的众数够不够"决定性"（见 `DECISIVE_SHARE`）。

    `counts` 读不出来时返回 True：一条老实现写下的行没有 `counts`，那时该按
    "没证据反对"处理，而不是把它整条吞掉。
    """
    counts = param.detail.get("counts")
    if not isinstance(counts, dict) or not counts:
        return True
    total = sum(int(value) for value in counts.values())
    if total <= 0:
        return True
    return max(int(value) for value in counts.values()) / total >= DECISIVE_SHARE


def _range_text(param: Param) -> str:
    """连续量 → "5–8" / "4"。取 p25–p75 而不是 min–max：极值会写出一段
    "2–19 条"的提示，那不是形状，是噪声。两个四分位四舍五入后相等时只写一个数。
    """
    median = float(param.value or 0.0)
    low = param.low if param.low is not None else median
    high = param.high if param.high is not None else median
    first, last = round(low), round(high)
    if first == last:
        return str(round(median))
    return f"{first}–{last}"


_LABELS: dict[tuple[str, str], str] = {
    ("width", "even"): "每层宽度大致均匀",
    ("width", "uneven"): "每层宽度不均（有话则长、无话则短）",
    ("colors", "none"): "基本不用颜色",
    ("colors", "mono"): "全图一个色",
    ("colors", "by_branch"): "一支一色",
    ("colors", "mixed"): "颜色混用",
    ("diagram_type", "tree"): "树状导图",
    ("diagram_type", "fishbone"): "鱼骨图",
    ("diagram_type", "flow"): "流程图",
}

#: 结尾那句硬约束。**它必须存在**，否则模型会为凑"7 支"拆出没意义的节点——
#: 那正是现在"每支 3 个 L2、每个 2 个 L3"的模板化的成因（实测 7 支全是 [2,2,2]）。
SHAPE_RULE = ("按这个形状生成；**内容本身该怎么拆还怎么拆，"
              "不要为了凑数硬拆或硬合**。")

#: 提示词开关。**默认开**，与第二层的 `MINDMAP_PROFILE_RERANK`（默认关）正好相反——
#: 理由只有一个：代价不同。这一段是几十个汉字，不花一次 HTTP、不花一秒；
#: 而画像加权要额外 embed 每一条命中。有代价的默认关，没代价的默认开。
PROMPT_SWITCH_ENV = "MINDMAP_STYLE_PROMPT"

#: 这一段的表头。措辞与 `prefs.NOTE_HEADER` / `memory.MEMORY_HEADER` 都不同，
#: 因为它说的是**观察**而不是声明、也不是记忆：它是"过去的图长什么样"，
#: 而不是"这个人要求什么"。
STYLE_HEADER = "这位用户过去的图大致是这个形状："


def enabled() -> bool:
    """提示词注入是否打开（`MINDMAP_STYLE_PROMPT`，未设即开）。"""
    return os.getenv(PROMPT_SWITCH_ENV, "1").strip().lower() not in {
        "0", "false", "no", "off"}


def prompt_block(
    params: dict[str, Param] | None = None, owner: str | None = None,
) -> str:
    """画像 → 拼进 **user 消息末尾**的那一段；没有可说的返回**空串**。

    **为什么是 user 末尾而不是 system**：这条规律在本仓库已经验证过三次
    （`role=decision`、`_detail_ask` 的自查句、全塞模式下的引用规则，都是在
    system 里失效、挪到 user 里才生效）。机制上说得通：system 是一段长且稳定的
    背景，越靠后的具体指令越容易被它自己的长度稀释；而 user 消息是模型此刻正在
    处理的那一件事，末尾那几句离生成动作最近。

    空串而不是"（暂无画像）"：与 `memory.prompt_for` 同一个理由——一个空的小标题
    会让模型以为"画像为空"是一种需要它处理的输入。

    只写置信度 ≥ `PROMPT_MIN_CONFIDENCE` 的参数。够不着的宁可不写：一句"大概 4 层"
    会让模型当成硬约束去凑，而那正是这一段要防的事。

    `params` 与 `owner` 二选一：前者是已经算好的参数（用例与 A/B 脚本直接给），
    后者用于"自己去读那个人的画像"。两个都不给就按当前请求的身份读。
    """
    current = read(owner) if params is None else params
    if not current:
        return ""
    lines: list[str] = []
    for key in PARAMS:
        param = current.get(key)
        if param is None or param.confidence < PROMPT_MIN_CONFIDENCE:
            continue
        if key in ("branches", "depth", "detail_chars", "label_chars"):
            unit = UNITS.get(key, "")
            lines.append(f"  {param.label} {_range_text(param)} {unit}".rstrip())
        elif key == "icons":
            ratio = float(param.value or 0.0)
            lines.append("  一级分支带图标" if ratio >= 0.5 else "  不用图标")
        else:
            if not _decisive(param):
                # 一半一半的分类写进提示词等于误导，见 `DECISIVE_SHARE`。
                continue
            text = _LABELS.get((key, str(param.value)))
            if text:
                lines.append(f"  {text}")
    if not lines:
        return ""
    return "\n".join([STYLE_HEADER, *lines, SHAPE_RULE])


def provenance(owner: str | None = None) -> dict[str, Any]:
    """这批画像**是由什么样的数据算出来的**——不是给运行打标签，是让画像自报家底。

    用户问过一个问题："要不要在画像里加一条『数据来源是测试还是真实使用』的标记？"
    答案是**要**，但**不能靠给每次运行打一个布尔标签**：

      - "测试"不是一个能从产物里读出来的属性，只能靠 `goal` 里的字面（"再改一处"）
        去猜，而那种判据不可穷尽、不可解释、下一个人接手时也不知道该不该扩；
      - 更要紧的是，同一个 goal 被重跑三次到底是"测试"还是"用户在微调"，从数据侧
        根本分不开——两者的产物形状可能完全不重叠。

    所以这里报的是三个**机械可算**的量，它们合起来回答了同一个问题：

      - `kept` / `runs`：折叠之后还剩几条独立观察（本机 21 / 34）；
      - `folded`：因为"形状重复"被折掉几条（本机 13）；
      - `dominant_share`：**最大的那一种形状占多少**。这个数超过 0.5 就说明
        画像主要在描述"一件事"——本机 `[1,3,3]`、零 detail 的那一种占了原始
        样本的一半，去掉它之前一级分支的中位数是 3，去掉之后是 5。

    与之配套的两条硬判据已经在实现里：`_dedupe`（同形状最多两票）与
    `profile.MIN_MINDMAP_NODES`（不足 20 节点的图不进向量样本——本机那 15 张
    验收图的标题是脚手架）。**它们降低影响，但不消除污染**：一份才是测试产物
    的库，`dominant_share` 会诚实地高到 0.5 以上。
    """
    observations = _observations(_owner() if owner is None else owner)
    usable = [item for item in observations if item.depth > 0]
    kept, folded = _dedupe(usable)
    shapes: dict[str, int] = {}
    for item in usable:
        key = _fingerprint(item)
        shapes[key] = shapes.get(key, 0) + 1
    dominant = max(shapes.values()) / len(usable) if usable else 0.0
    return {
        "runs": len(observations),
        "usable": len(usable),
        "kept": len(kept),
        "folded": folded,
        "distinct_shapes": len(shapes),
        "dominant_share": round(dominant, 3),
        "min_samples": MIN_SAMPLES,
        "repeat_cap": REPEAT_CAP,
    }


def describe(owner: str | None = None) -> dict[str, Any]:
    """面板/脚本要的一份完整视图：8 个参数 + 是否启用 + 这批数据的来路。"""
    params = read(owner)
    return {
        "enabled": bool(params),
        "min_samples": MIN_SAMPLES,
        "params": {key: {
            "label": param.label,
            "kind": param.kind,
            "value": param.value,
            "low": param.low,
            "high": param.high,
            "samples": param.samples,
            "confidence": param.confidence,
            "detail": param.detail,
        } for key, param in params.items()},
        "provenance": provenance(owner),
    }


def iter_observations(owner: str | None = None) -> Iterator[Observation]:
    """调试/脚本入口：逐条吐出**这个人的**原始观测（不落库、不汇总）。"""
    yield from _observations(_owner() if owner is None else owner)


__all__ = [
    "FULL_CONFIDENCE_SAMPLES",
    "MAX_RUNS",
    "MIN_SAMPLES",
    "PARAMS",
    "PROMPT_MIN_CONFIDENCE",
    "PROMPT_SWITCH_ENV",
    "REPEAT_CAP",
    "SHAPE_RULE",
    "STYLE_HEADER",
    "STYLE_TABLE",
    "UNEVEN_CV",
    "Observation",
    "Param",
    "describe",
    "enabled",
    "ensure_schema",
    "iter_observations",
    "observe",
    "prompt_block",
    "provenance",
    "read",
    "refresh",
    "summarize",
]
