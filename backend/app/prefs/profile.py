"""用户画像的**第二层**：内容方向（画像向量），以及把它接进检索排序的那一项。

## 为什么不是一个平均向量

把这个人所有的材料与导图平均成一个向量，得到的是一根"什么都不像"的方向：

```
考研 + 报销 + 机器学习 + Rust  →  mean  →  对什么都有点像 = 没有偏好
```

平均向量在检索里是**负资产**：它对任何主题都有 0.3 左右的余弦（因为它是所有主题
的平均），于是它给每条命中都加一个差不多的分——排序一点没变，却让人以为个性化
生效了。真正有用的是**分方向**：聚成 3–5 个簇、每簇一根向量，打分时取
`max(cos(hit, v) for v in vectors)`。这样"考研的人搜机器学习"时，考研那根向量
不会被拉进来，机器学习的命中拿到的是"与他自己那个机器学习方向"的相似度。

## 三条硬约束（写死在这里，改之前先看代价）

1. **只在排序用，不在召回用。** 它决定"这几条谁先"，不决定"要不要这条"。
   拿去过滤就是过滤气泡：用户只看得见他已经知道的东西，而且**看不出来**——
   界面上没有任何一处会说"这次少了 3 条因为画像不喜欢"。
2. **α 有上限，且只在命中本身已过主题相关门时加分。** 见 `MAX_ALPHA` 与
   `GATE_RATIO`：一条主题上明显不相关的命中，不能因为"像这个人的风格"被抬进
   前几名——那是最坏的一种错，它看起来像是偏好生效了。
3. **画像向量带时间衰减**，复用 `memory.store` 的 `0.5 ** (age / 30)`：
   同一个人的方向会变（换课程、换工作）。衰减进两处——质心的加权平均、以及
   簇的 `weight`（这个方向现在占多重）。

## 聚类为什么是"球面 k-means + 纯 Python"

`text-embedding-v3` 的输出已在 `embedding.embed_texts` 里 L2 归一化，所以余弦就是
点积，"球面 k-means"（质心归一化后再分配）是正确的那一档。**不引入 numpy /
sklearn**：`requirements.txt` 里没有它们，为一个 40 个样本、1024 维、跑一次几百
毫秒的离线任务新增两个部署依赖不划算。初始化也不用随机数——用"最大最小距离"的
确定性版本，于是同一批样本永远聚出同一批簇，`profile_vectors` 不会每跑一次
就换个方向（那会让 A/B 的差异全变成噪声）。

## 方向按导图隔离（`map_id` / 池键）

上面那一节说"这个人的每个方向都是一个独立的兴趣"。**这句话在跨图维度上是错的**：
"碳中和"那张图的方向被拿去给"机器学习"那张图加权时，用户写「机器学习」却看到碳中和
的引用被顶上来。而那批被顶上去的碳中和材料又会成为新样本——**越用越窄**，这正是
过滤气泡的机制，只是它发生在"图与图之间"而不是"人与人之间"。

所以方向池按**一次生成**隔离：

```
pool_key = "run:<run_id>"          # 一次运行一个池（判据见 pool_key 的 docstring）
stored(map_id=pool_key)            # 只读这个池；map_id 为空的旧全局方向**不返回**
```

`style`（统计层）不动：那 8 个参数是"这个人怎么做事"，该跨图；内容方向是"这张图
讲什么"，不该跨图。

## 为什么方向可以零 embedding 往返

旧路径从三张表取 60 条"文件名 + 前 600 字"**拼接文本**再去 embed。那批文本从来没人
embed 过（`chunk_vectors` 里存的是**块**的向量），所以每条都是 cache miss：60 条
÷ `embedding.MAX_BATCH`(10) = **6 次 HTTP**。开关默认关的理由就是这个成本。

新路径的样本换成**这张图自己的材料块**，而块向量检索时已经写进 `chunk_vectors` 了
（`service._vector_scores` 按块文本哈希缓存）。取现成的即可：

```
这张图的材料 → _chunk_tree 切块 → level=1 子块 → text_hash → chunk_vectors 查
                                                                  ↑ 0 次 HTTP
```

本机实测（`data/mindmaps.db`，18 份来源 / 80 个子块）：**子块命中 76 条 = 95%**。
父块只有 21%（父块只在 `search_sources` 单级路径下被 embed，`MINDMAP_PARENT_RETRIEVAL`
开着时那条路不走），所以样本取**子块**。

没命中的块**不补 embed**——这是"零成本"的全部含义。少几条样本的后果只是这次方向
粗一点，而补 embed 的后果是开关一打开就多 N 次往返，那正是默认关着的理由。
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from app.knowledge import embedding, vector_cache
from app.memory.store import age_days, decay, utcnow

logger = logging.getLogger(__name__)

VECTOR_TABLE = "profile_vectors"

# ---------------------------------------------------------------- 刻度

#: 聚几个方向。3 是下限（少于 3 个方向时"分方向"就没意义了——一个人手上通常
#: 同时有两三件不相关的事），5 是上限（再多就变成"每个主题一个方向"，那等于
#: 没有泛化，新主题永远落在某个簇的边缘）。中间由 `_pick_k` 按样本量取。
MIN_CLUSTERS = 3
MAX_CLUSTERS = 5

#: 少于这么多样本不建画像。与 `style.MIN_SAMPLES` 同一个数字、同一条理由
#: （3 个方向需要至少 3 个样本能各自成立，但要聚出"方向"而不是"分组"，
#: 每个方向至少要有两个成员——5 是能同时满足这两条的最小值）。
MIN_SAMPLES = 5

#: 最多取多少样本。一次 rebuild 要对每个样本 embed 一次（走 `vector_cache`，
#: 命中就不花钱），上限 60 意味着最坏情况 6 批 HTTP（`embedding.MAX_BATCH = 10`）。
#: 超过上限时**按来源类型分层**截断（见 `_samples`），不是简单地砍尾巴。
#:
#: 隔离之后这条路只剩脚本在用（`rebuild()` 不传材料时的老全局口径）；生成链路走
#: `build_directions_from_sources`，它按**材料**轮流取块，同样是这个上限——理由
#: 一样（60 个 1024 维向量的球面 k-means 实测 89 ms，再多就得不偿失）。
MAX_SAMPLES = 60

#: 迭代上限。球面 k-means 在 40 个样本上通常 4–6 轮就稳定，12 是留足余量的上界；
#: 不设"收敛即停"的提前退出，因为这批数据小到多跑 6 轮也只要几毫秒，而"是否
#: 收敛"本身需要一个容差常量——多一个可调参数，多一处可能调错的地方。
MAX_ITERATIONS = 12

#: 每个样本进向量的文本上限（码点）。材料的正文前 600 字足以定方向：标题 + 摘要
#: 就在前 600 字里，再往后是论证细节，对"这是哪个方向"没有增量。
SAMPLE_CHARS = 600

#: 一张图至少要有这么多节点，才算"一次真实的表达"。
#:
#: **这条闸是本机实测出来的，不是拍脑袋**：23 张已保存的图里，4–15 节点的那 15 张
#: 全是验收与基线图（标题是"树基线（改动前后逐像素对比用）"、"E2E 无阶段内边"、
#: "六种形状基线"、"判定节点细节检验"）。它们的标题是脚手架而不是主题，进了样本
#: 就会把一个"测试"方向聚出来——那是**画像里最坏的一种污染**：它看起来像偏好，
#: 实际上是这个仓库的测试套件。
#:
#: 20 这个数的另一面：它也顺带滤掉了真正的短图（"机器学习"那张 13 节点的）。
#: 代价可以接受——一张 13 节点的图表达不了多少方向，而留下的 7 张覆盖了 24–561 节点。
#: 加过滤前后，同一批数据上 k=4 的轮廓系数从 0.127 升到 0.248（脚本
#: `scripts/measure_profile_ab.py --cluster-report`）。
MIN_MINDMAP_NODES = 20

#: 时间衰减半衰期：**复用 `memory.store.HALF_LIFE_DAYS`（30 天），这里不另立一个数**。
#: 两处各写一份的后果是其中一处迟早被改而另一处没改，于是"记忆 30 天、画像 45 天"
#: 变成两个说不清谁对的时间尺度。这里额外说明为什么画像也该衰减：偏好比事实稳，
#: 但一个人换了课题，旧材料的方向就不该继续给他的新检索加权——那正是"越用越窄"
#: 的机制，也是过滤气泡在时间维度上的形态。

#: α 的**默认值**：排序分里"画像贴合度"这一项最多贡献多少。
#:
#: 打分口径是 `norm(原分) + α * max_cos`，`norm` 是 min-max 到 [0,1] 的融合分，
#: 所以 α = 0.15 的含义是"画像贴合度满分的条目，最多相当于原分差距的 15%"。
#: 为什么是这个量级：
#:
#:   - 排序的**主要依据必须仍然是"与这次问题的相关性"**。画像只该在"两条差不多
#:     相关"时决定先后——这正是个性化该起作用的范围。α 大到 0.4 以上就是让画像
#:     与相关性平起平坐，实测过的那种退化（同题不同意图，画像把带过的材料顶上来）
#:     会重新出现；
#:   - 也不能太小。`max_cos` 在真实数据上的分布很窄（同一语料内常在 0.2–0.6），
#:     α = 0.05 时这一项的极差只有 0.02，落在 RRF 的舍入误差里，等于没加。
#:
#: 0.15 是"能改变相邻两条的顺序、改不动相隔三名以上的顺序"。
ALPHA = 0.15

#: α 的硬上限。调用方（脚本、将来的接口）可以传更小的值，传更大的会被夹到这里。
#: 上限存在的原因不是性能，是**可解释性**：α > 0.25 之后，"这条为什么排第一"
#: 已经无法只靠相关性回答，而检索侧没有第二处能把这个信息补给用户。
MAX_ALPHA = 0.25

#: 主题相关门：只有归一化分数不低于本批最高分的这个比例的命中，才允许被画像加分。
#:
#: 0.5 的依据：`norm` 是本批内的相对分，0.5 对应"至少达到最强命中一半的相关性"。
#: 低于它的命中即使画像贴合度是 1.0（α 满额 0.15），最终分也只有 0.65，抬不过
#: 一条 norm=0.6 的命中——**加分改得了相邻顺位，改不了"与主题无关"这个判断**。
GATE_RATIO = 0.5

#: 贴合门：与最佳方向的余弦低于这个值的命中**不算"属于这张图"**，不加分。
#:
#: 这个闸是**实测逼出来的**，不是补一个漂亮常数。两张不同主题的图上
#: （`scripts/measure_profile_isolation.py --run`）：
#:
#:   | 命中来自 | 本图材料的余弦 | 另一张图材料的余弦 |
#:   |---|---|---|
#:   | 图 B（碳中和） | 0.918 / 0.958 / 0.962 | 0.487 / 0.497 |
#:   | 图 A（机器学习） | 0.843 / 0.916 / 0.927 | 0.518 |
#:
#: 两个分布之间空着一大段（0.52 ↔ 0.84），0.6 落在空档里。没有这道门时实测出现过
#: 这样一次排序：一条跨图命中 cos = 0.497，一条本图命中 cos = 0.9625，两条的
#: `norm` 并列（RRF 分相同），而画像把它们排成了跨图的在前——因为 α 还乘了方向的
#: 权重，跨图那条"最像"的是重方向（权重 0.44 → α 0.15），本图那条"最像"的是轻方向
#: （权重 0.22 → α 0.075），`0.497 × 0.15 > 0.9625 × 0.075`。**那是真的错排序**，
#: 而它只有逐条看分数才看得出来（汇总的条数一个都没变，见 `boost` 的约束 1）。
#:
#: 隔离之后这道门才真正有牙齿：同一批材料算出来的方向，对**别的图**的材料余弦
#: 掉到 0.5 附近（跨主题基线），过不了门；而隔离之前，图 A 的材料与图 A 的方向
#: 余弦是 0.90+，一路过门。**"跨图污染为 0"就是这条 + 隔离一起成立的。**
MIN_COSINE = 0.6

#: 一次最多给多少条命中算画像余弦。top_k 通常是 3（`DEEP_BRANCH_TOP_K`）或 5，
#: 8 留出余量。这个闸是成本闸：每条命中要一次 embedding（有缓存则免），
#: 20 支分支串起来就是 20 批；截到"前 8 条"时，排在后面的本来就轮不到被加权
#: （它们过不了 `GATE_RATIO`）。
MAX_HITS = 8

#: 多久之后算过期。`SNIFF` 在生成前读一次，过期就**不重算**（那是脚本/面板的事），
#: 只用旧值并记一条 info——画像的降级方向永远是"这次少一条个性化"，不是"这次
#: 生成失败"。
STALE_HOURS = 24.0

#: 开关。**默认关**：打开之后每次生成会多出 N 次 embedding 往返（N = 检索次数），
#: 在收益被 A/B 证明之前，默认值不该是"多花这些时间"。
#: `scripts/measure_profile_ab.py` 用它把两组分开。
#:
#: **按导图隔离 + 复用块向量之后，这个理由已经弱到几乎不成立**：方向从"这张图的
#: 材料"现成的块向量算，不再有"新 embed"那一次往返（实测 80 个子块里 76 条命中
#: 缓存，剩下 4 条不补）。剩下的成本只有聚类本身（60 样本 89 ms，一次运行一次）。
#: 但默认值不改在这一轮：先把"跨图污染是不是真的降到 0"测出来，再谈默认值。
#: 见 `docs/PROFILE-ISOLATION.md` 的两图实测。
SWITCH_ENV = "MINDMAP_PROFILE_RERANK"

#: 池键的前缀：**新图还没保存时用 `run:<run_id>`**。判据见 `pool_key`。
POOL_RUN_PREFIX = "run:"

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

#: `map_id` 是**池键**（不是"导图主键"）：
#:   - 空串 / NULL = 隔离之前那批全局方向（`stored(map_id=None)` 的全量视图里还在，
#:     给脚本与排查用；`stored(map_id="…")` 时**不返回**——它们属于"没有隔离概念的
#:     时代"，任何一张图都不该被它们加权）；
#:   - `run:<run_id>` = 一次生成的方向池（新图还没保存时的形态，见 `pool_key`）；
#:   - 已保存图的 `map_id` = 同一张图的多轮生成可以共用（接口就位，当前生成链路
#:     拿不到它，见 `pool_key`）。
_CREATE = f"""CREATE TABLE IF NOT EXISTS {VECTOR_TABLE} (
    id         TEXT PRIMARY KEY,
    vector     BLOB NOT NULL,
    label      TEXT NOT NULL,
    weight     REAL NOT NULL,
    samples    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    map_id     TEXT
)"""


# ---------------------------------------------------------------- 值对象


@dataclass(frozen=True)
class Sample:
    """一个样本：给聚类用的一段文本 + 它属于哪儿 + 什么时候形成的。

    `kind` 分三种来源（材料 / 导图 / 记忆），不是为了统计，是为了**分层截断**：
    材料的条数与导图的条数常常差一个量级（18 份材料 vs 35 张图），不按类型分层
    取样的话，样本会整体偏到"图"那一侧，聚出来的方向是"这个人常生成什么主题"
    而不是"他手上有什么材料"。两者都是真实偏好的一部分，所以各占一份配额。
    """

    text: str
    kind: str
    at: str | None
    tag: str = ""


@dataclass(frozen=True)
class Direction:
    """一个画像方向：一根单位向量 + 它现在有多重 + 一句可读的 label。"""

    label: str
    vector: list[float]
    weight: float
    samples: int


@dataclass(frozen=True)
class Boost:
    """一次画像加权的全部可观测结果。

    五个字段都留着（而不只返回排好序的列表）：
    `lifted` 是"过滤气泡检验"的唯一输入——加权上来的命中后来被删掉了几条，
    这个问题的答案只能从"哪几条是被抬上来的"出发；
    `cosines` 让排查能区分"没加分"与"加了但都一样"；
    `bonuses` 是**每条命中实际拿到的加分**（下标 → `final - base`，没加分的是 0）。
    它是"跨图污染"唯一干净的量化：`cosines` 只说"像不像"，而"像"过不了门
    （`MIN_COSINE`）就不该算污染；`bonuses` 说"到底加了几分"，一分没加就是没被
    这张图的方向碰过。`lifted` 单独看不出来——它数的是名次变化，而名次会**互相
    挤压**（别的条目被降下去，不动的那条也会相对上升）；
    `reason` 是**这次为什么没加权**（空串 = 正常加权过）。没有它，用户看到的
    只是"个性化好像没生效"——而"这张图的材料还没有块向量"与"开关关着"在界面上
    长得一模一样。
    """

    hits: list[Any]
    lifted: list[Any]
    cosines: dict[int, float] = field(default_factory=dict)
    bonuses: dict[int, float] = field(default_factory=dict)
    reason: str = ""


# ---------------------------------------------------------------- 建表


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建表，并给已存在的表补上 `map_id` 列（幂等）。

    补列是必须的：`CREATE TABLE IF NOT EXISTS` 只保证"表存在"，不保证"列齐全"。
    老库里有 4 行隔离之前的全局方向，缺列的后果不是报错，是 `stored(map_id=…)`
    那句 `WHERE map_id = ?` 直接 `no such column`——**画像整层静默失效**，而唯一的
    信号是那条被 `stored` 自己吞掉的异常。

    补出来的列在老行上是 NULL，而 NULL 在这里有确切含义（"当时还没有隔离这个概念"），
    正是 `stored(map_id="…")` 必须把它们排除掉、`stored(map_id=None)` 必须把它们
    包含进来的依据。不做数据回填：给它们编一个 `map_id` 等于伪造归属。
    """
    conn.execute(_CREATE)
    from app.mindmaps.repository import _ensure_columns

    _ensure_columns(conn, VECTOR_TABLE, {"map_id": "TEXT"})


def _connect() -> sqlite3.Connection:
    from app.mindmaps import repository

    return repository.connect()


def _owner() -> str:
    """当前请求的 owner。只在有请求上下文的地方调（线程里会拿到空串）。"""
    from app.auth import owner as owner_module

    return owner_module.current_owner()


def pool_key(*, map_id: str | None = None, run_id: str | None = None) -> str:
    """一次生成用哪个方向池：**有 `map_id` 用它，没有用 `run:<run_id>`**。

    为什么不是纯 `map_id`：**新图在服务端根本没有 id**。`repository.create_map` 才
    发 uuid4（`SaveRequest` 的注释写着"第一次保存之前没有 map_id"），而
    `DiagramContent` / `AgentRunRequest` / `RunContext` 三处都没有这个字段——
    生成链路拿不到一个还不存在的东西。纯 `map_id` 的后果是"首次生成永远没有池"，
    而那恰恰是最需要个性化的一刻（用户刚传完 3 份材料）。

    为什么不是纯 `run_id`：已保存图的多轮生成（`expand` / `edit`）本该共用一张图的
    方向。`run_id` 每轮都换，池也跟着换——用户看到的是"个性化时有时无"，比跨图串味
    更难理解。所以接口按"有 map_id 优先"设计，等生成链路能拿到它时直接生效。

    这条判据要一直成立，因为**"新图还没保存"这个状态会永远存在**（每次 create 都
    是它）。所以别把 `run:` 当成过渡形态。

    另一个性质让纯 `run_id` 在实践中没那么糟：聚类是**确定性**的（`cluster` 的
    最大最小距离初始化，没有随机数），所以同一批材料在两轮里会算出同一批方向。
    `pool_key` 只决定"存在哪一行、按谁读回"，不决定"算出来是什么"。
    """
    if map_id:
        return str(map_id)
    if not run_id:
        # 两边都没有：老脚本直接调 `rebuild()` 时就是这种。返回空串 = 隔离之前那个
        # 全局池，`stored(map_id="")` 与它的语义一致。
        return ""
    return f"{POOL_RUN_PREFIX}{run_id}"


def enabled() -> bool:
    """画像加权是否打开。`MINDMAP_PROFILE_RERANK` 未设或为 0 时关闭。"""
    return os.getenv(SWITCH_ENV, "0").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------- 取样


def _mindmap_text(payload: dict[str, Any]) -> str:
    """一张图 → 一段文本。标题 + 一级分支 + 前几个二级分支。

    只用结构与标签，不用 detail：detail 是模型写的说明，风格上人人相似，把它算进
    方向里会把所有图拉向同一个中心（"说明文字的平均"）。标题与分支标签才是这个人
    选择谈什么的地方。
    """
    parts: list[str] = []
    title = str(payload.get("title") or "").strip()
    if title:
        parts.append(title)
    root = payload.get("root")
    if isinstance(root, dict):
        for branch in (root.get("children") or [])[:8]:
            if not isinstance(branch, dict):
                continue
            label = str(branch.get("label") or "").strip()
            if label:
                parts.append(label)
            for child in (branch.get("children") or [])[:3]:
                if isinstance(child, dict):
                    deeper = str(child.get("label") or "").strip()
                    if deeper:
                        parts.append(deeper)
    elif isinstance(payload.get("bones"), list):
        parts.extend(str(bone.get("label") or "").strip()
                     for bone in payload["bones"][:8] if isinstance(bone, dict))
    elif isinstance(payload.get("stages"), list):
        parts.extend(str(stage.get("label") or "").strip()
                     for stage in payload["stages"][:8] if isinstance(stage, dict))
    return "｜".join(part for part in parts if part)[:SAMPLE_CHARS]


def _quota(kind: str) -> int:
    """每种来源的配额。材料与导图各 2/5，记忆 1/5（它最少、也最稳）。"""
    return {
        "material": int(MAX_SAMPLES * 0.4),
        "mindmap": int(MAX_SAMPLES * 0.4),
        "memory": MAX_SAMPLES - 2 * int(MAX_SAMPLES * 0.4),
    }.get(kind, MAX_SAMPLES)


def _node_count(payload: dict[str, Any]) -> int:
    """一张图的节点数。三套结构各数一遍，坏数据返回 0。

    不构造 pydantic 模型：这里只需要一个整数，而 `_mindmap_text` 已经在用同一份
    dict 了。两处各解析一次会让"样本被过滤掉了"与"文本是空的"变成两个分不开的原因。
    """
    def walk(node: Any) -> int:
        if not isinstance(node, dict):
            return 0
        children = node.get("children") or []
        return 1 + sum(walk(child) for child in children)

    root = payload.get("root")
    if isinstance(root, dict):
        return walk(root)
    bones = payload.get("bones")
    if isinstance(bones, list):
        return sum(walk(bone) for bone in bones)
    stages = payload.get("stages")
    if isinstance(stages, list):
        total = 0
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            for node in (stage.get("nodes") or []):
                total += walk(node)
            # 阶段本身也算一个节点（与 `shapes.count_nodes` 同口径）。
            total += 1
        return total
    return 0


def _samples(owner: str) -> list[Sample]:
    """从三张表取样本。任何一张表的失败都不影响另外两张。

    **只有 `memory_items` 按 `owner` 过滤**，另外两张表**故意读全库**：

      - `library_sources` 与 `mindmaps` 在这个产品里是**共享数据**（`docs/DEPLOY.md`
        写明"多加账号 = 共用一个知识库"），它们的样本描述的是"这台机器上有什么
        材料、生成过什么图"，不是"某个人偏好什么"；
      - `memory_items` 是私有的（PR#27 审 P0-2 的形状），把它当共享样本会让别人的
        偏好变成我的画像方向。

    这条区分不是宽松，是**边界必须与文档一致**：如果连共享表也按 owner 过滤，
    多人共用一台机器时画像会因为"每个人自己的材料都很少"而集体失效——那个失败
    比串味更难发现（画像静默变空，生成质量下降但没有任何报错）。
    """
    collected: list[Sample] = []
    with closing(_connect()) as conn:
        try:
            rows = conn.execute(
                "SELECT filename, text, created_at FROM library_sources "
                "ORDER BY last_used_at DESC, created_at DESC LIMIT ?",
                (_quota("material"),),
            ).fetchall()
            for row in rows:
                body = f"{row['filename'] or ''}\n{row['text'] or ''}"[:SAMPLE_CHARS]
                if body.strip():
                    collected.append(Sample(body, "material", row["created_at"],
                                             str(row["filename"] or "")[:40]))
        except sqlite3.Error as exc:
            logger.warning("画像取样：材料表读不出来（%s），这次只用导图与记忆",
                           type(exc).__name__)
        try:
            rows = conn.execute(
                "SELECT content, updated_at FROM mindmaps "
                "ORDER BY updated_at DESC LIMIT ?",
                (_quota("mindmap"),),
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["content"] or "{}")
                except ValueError:
                    continue
                if not isinstance(payload, dict):
                    continue
                # 见 `MIN_MINDMAP_NODES`：验收图与基线图的标题是脚手架，不是主题。
                if _node_count(payload) < MIN_MINDMAP_NODES:
                    continue
                body = _mindmap_text(payload)
                if body.strip():
                    collected.append(Sample(body, "mindmap", row["updated_at"],
                                             str(payload.get("title") or "")[:40]))
        except sqlite3.Error as exc:
            logger.warning("画像取样：导图表读不出来（%s），这次只用材料与记忆",
                           type(exc).__name__)
        try:
            rows = conn.execute(
                "SELECT text, COALESCE(formed_at, created_at) at FROM memory_items "
                "WHERE owner = ? ORDER BY created_at DESC LIMIT ?",
                (owner, _quota("memory")),
            ).fetchall()
            for row in rows:
                text = str(row["text"] or "").strip()
                if text:
                    collected.append(Sample(text, "memory", row["at"], text[:40]))
        except sqlite3.Error:
            # `memory_items` 在没写过记忆的库上可能整张表都是空的，这不是故障。
            # 也可能是老库还没补 `owner` 列——那同样不该让整次取样失败。
            pass
    # 同一段文本只取一次（材料与导图可能都写了同一个主题），按出现顺序保留第一个。
    seen: set[str] = set()
    unique: list[Sample] = []
    for sample in collected:
        key = vector_cache.text_hash(sample.text)
        if key in seen:
            continue
        seen.add(key)
        unique.append(sample)
    return unique


# ---------------------------------------------------------------- 聚类


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm > 0 else list(vector)


def _pick_k(count: int) -> int:
    """样本量 → 簇数：`round(sqrt(n / 1.5))`，夹在 [3, 5]。

    每个簇大致 `sqrt(1.5n)` 个成员：n = 18 → 3，n = 24 → 4，n = 32 → 5。

    **1.5 这个系数是本机实测定的**，不是从哪篇论文抄的：24 条样本上对比 k = 3/4/5，
    轮廓系数分别是 0.189 / 0.248 / 0.259，但 k = 5 的分布是 `[15, 6, 2, 1, 1]`
    ——三个单样本簇，它们不是"方向"，是仓库里碰巧只有一份材料的三个题目。
    k = 4 的 `[15, 6, 3, 1]` 在"轮廓更高"与"每个方向都站得住"之间取得平衡。

    夹到 5 的上限还有一个非统计的理由：提示词与面板里列 6 条以上方向时，用户
    读不出"哪几条是他主要的兴趣"，而那正是这张表唯一要回答的问题。
    """
    raw = round(math.sqrt(max(1, count) / 1.5))
    return max(MIN_CLUSTERS, min(MAX_CLUSTERS, raw))


def silhouette(matrix: Sequence[Sequence[float]],
               groups: Sequence[tuple[list[float], list[int]]]) -> float:
    """平均轮廓系数（余弦距离版）。**它只进日志与排查脚本，不参与打分。**

    这个数回答一个打分回答不了的问题：这批数据到底有没有"方向"可分。

      - ≥ 0.35：簇之间真的隔开了，画像可以当真；
      - 0.15–0.35：有弱结构，方向大体对、边界模糊（本机就在这一档：24 条样本、
        k=4 时 0.248）；
      - < 0.15：基本等于把一堆互不相关的文本切成了几份，那时的 α 加权是噪声。

    单样本簇对轮廓的贡献按 0 计（sklearn 同口径）：一个只含自己的簇，它的
    "与同类有多近"没有定义。
    """
    if len(groups) < 2 or not matrix:
        return 0.0
    label: dict[int, int] = {}
    for slot, (_, members) in enumerate(groups):
        for index in members:
            label[index] = slot
    values: list[float] = []
    for index in range(len(matrix)):
        own_slot = label.get(index)
        if own_slot is None:
            continue
        own = [other for other in label if label[other] == own_slot and other != index]
        if not own:
            values.append(0.0)
            continue
        inner = sum(1.0 - _dot(matrix[index], matrix[other]) for other in own) / len(own)
        outer = min(
            sum(1.0 - _dot(matrix[index], matrix[other]) for other in members) / len(members)
            for slot, (_, members) in enumerate(groups) if slot != own_slot and members
        )
        scale = max(inner, outer)
        values.append((outer - inner) / scale if scale > 0 else 0.0)
    return round(sum(values) / len(values), 4) if values else 0.0


def cluster(
    vectors: Sequence[Sequence[float]], k: int, weights: Sequence[float] | None = None,
) -> list[tuple[list[float], list[int]]]:
    """球面 k-means：返回 [(归一化质心, 成员下标)]，只保留非空簇。

    **初始化是确定性的**（最大最小距离法）：先取全局加权质心，第一个中心取离它
    最远的那条样本，之后每次取"到最近中心最远"的样本。没有随机数，所以同一批
    样本永远聚出同一批簇——这一点比"多跑几次取最好"重要得多：`profile_vectors`
    是落库的，每跑一次换一批方向，A/B 的差异就全变成聚类噪声。

    空簇直接丢掉而不是重抽中心：几十个样本切 5 份时出现空簇意味着样本本身就
    只有 4 个方向，硬凑第 5 个只会得到一根复述已有方向的向量。
    """
    count = len(vectors)
    if count == 0:
        return []
    k = max(1, min(k, count))
    weights = list(weights) if weights else [1.0] * count
    mass = sum(weights) or 1.0

    # 全局加权质心 → 第一个中心（离它最远的样本）
    global_center = _normalize([
        sum(weights[i] * vectors[i][dim] for i in range(count)) / mass
        for dim in range(len(vectors[0]))
    ])
    first = max(range(count), key=lambda i: (1.0 - _dot(vectors[i], global_center), -i))
    centers: list[list[float]] = [list(vectors[first])]

    while len(centers) < k:
        best, best_distance = -1, -1.0
        for index in range(count):
            nearest = max(_dot(vectors[index], center) for center in centers)
            distance = 1.0 - nearest
            if distance > best_distance + 1e-12:
                best, best_distance = index, distance
        if best < 0:
            break
        centers.append(list(vectors[best]))

    assignment = [-1] * count
    for _ in range(MAX_ITERATIONS):
        moved = False
        for index in range(count):
            scores = [_dot(vectors[index], center) for center in centers]
            pick = max(range(len(centers)), key=lambda c: (scores[c], -c))
            if assignment[index] != pick:
                assignment[index] = pick
                moved = True
        for slot in range(len(centers)):
            members = [i for i in range(count) if assignment[i] == slot]
            if not members:
                continue
            total = sum(weights[i] for i in members) or 1.0
            centers[slot] = _normalize([
                sum(weights[i] * vectors[i][dim] for i in members) / total
                for dim in range(len(vectors[0]))
            ])
        if not moved:
            break

    grouped: list[tuple[list[float], list[int]]] = []
    for slot in range(len(centers)):
        members = [i for i in range(count) if assignment[i] == slot]
        if members:
            grouped.append((centers[slot], members))
    return grouped


def _direction_label(samples: Sequence[Sample], members: Sequence[int],
                     matrix: Sequence[Sequence[float]], usable: Sequence[int],
                     center: Sequence[float]) -> str:
    """一个方向叫什么：簇内离中心最近的那个样本的 tag（文件名 / 图标题 / 记忆首句）。

    用"最近的那个样本"而不是"簇的关键词"：关键词要分词、去停用词、还要解释
    "为什么是这几个词"，而最近样本是一个可以点开核对的东西。label 只进日志与
    排查面板，不参与打分——它错了不影响排序，这一点让这个选择变得便宜。

    两个下标空间不要混：`members` 是 `matrix` 的行号，`usable` 把它映射回
    `samples` 的下标（有些样本没有向量，被跳过了）。
    """
    best = max(members, key=lambda position: _dot(matrix[position], center))
    sample = samples[usable[best]]
    tag = sample.tag.strip() or sample.text.strip()[:24]
    return tag[:32] or f"{len(members)} 个样本"


def build_directions(samples: Sequence[Sample]) -> list[Direction]:
    """样本 → 画像方向。**没有向量服务时返回空列表**（降级，不是故障）。"""
    if len(samples) < MIN_SAMPLES:
        logger.info("画像：样本 %d 条，少于 %d 条，不建方向", len(samples), MIN_SAMPLES)
        return []
    if not embedding.configured():
        logger.info("画像：向量服务未配置，跳过（画像不参与排序，功能不受影响）")
        return []
    texts = [sample.text for sample in samples]
    # 先吃缓存：`vector_cache` 里可能已经有这些连接词文本的向量（材料块命中的那
    # 一批就是这样来的），命中一条就少一次付费调用。
    model = embedding.model_name()
    keys = [vector_cache.text_hash(text) for text in texts]
    cached = vector_cache.get_vectors(keys, model)
    pending = [(index, text) for index, text in enumerate(texts) if keys[index] not in cached]
    fetched: dict[int, list[float]] = {}
    if pending:
        try:
            vectors = embedding.embed_texts([text for _, text in pending])
        except embedding.EmbeddingError as exc:
            logger.warning("画像：向量服务失败（%s），本次不建方向", type(exc).__name__)
            return []
        fresh: list[tuple[str, list[float]]] = []
        for (index, _), vector in zip(pending, vectors):
            if vector:
                fetched[index] = vector
                fresh.append((keys[index], vector))
        if fresh:
            vector_cache.put_vectors(fresh, model)

    moment = utcnow()
    usable: list[int] = []
    matrix: list[list[float]] = []
    weights: list[float] = []
    for index in range(len(samples)):
        vector = cached.get(keys[index]) or fetched.get(index)
        if not vector:
            continue
        usable.append(index)
        matrix.append(_normalize(vector))
        weights.append(decay(age_days(samples[index].at, now=moment)))
    if len(matrix) < MIN_SAMPLES:
        logger.info("画像：有效向量 %d 条，少于 %d 条，不建方向", len(matrix), MIN_SAMPLES)
        return []

    groups = cluster(matrix, _pick_k(len(matrix)), weights)
    total = sum(weights) or 1.0
    directions: list[Direction] = []
    for center, members in groups:
        directions.append(Direction(
            label=_direction_label(samples, members, matrix, usable, center),
            vector=center,
            weight=round(sum(weights[position] for position in members) / total, 4),
            samples=len(members),
        ))
    directions.sort(key=lambda item: (-item.weight, item.label))
    logger.info("画像：%d 条样本聚成 %d 个方向（%s），轮廓系数 %.3f",
                len(matrix), len(directions),
                "；".join(f"{d.label}({d.samples})" for d in directions),
                silhouette(matrix, groups))
    return directions


# ---------------------------------------------------------------- 按图取方向
#
# 这一节是"按导图隔离"的落地，也是"零 embedding 往返"的落地。两件事其实是同一件
# 事的两面：**方向只从这张图的材料算，而那些材料已经是切好的块、块向量已经在
# `chunk_vectors` 里了**。所以"隔离"不但没让成本变高，反而把唯一那笔成本（60 条
# 拼接文本的 embed）整笔省掉了。


@dataclass(frozen=True)
class ChunkSample:
    """一个块样本：**向量来自 `chunk_vectors`，不来自任何新的 HTTP**。

    `tag` 只进 `Direction.label`（日志与排查面板），不参与打分。用
    "文件名#块序" 而不是块原文：label 要能一眼指回是哪份材料的哪一段，而块原文
    是材料正文，不进日志（与 `embedding` 的注释同一条纪律）。
    """

    vector: list[float]
    tag: str


def _sub_spans(text: str) -> list[tuple[int, int]]:
    """这份材料按检索那套切块器切出来的**子块**区间。

    取 `level == 1` 而不是父块：实测同一批材料上，子块的块向量有 95% 命中
    `chunk_vectors`，父块只有 21%。原因是检索**实际 embed 的是子块**
    （`service._collect_leaves` 只把子块当匹配单元，父块是"被子块找到之后返回"
    的窗口），而父块只在 `search_sources` 单级路径下才 embed——那条路在
    `MINDMAP_PARENT_RETRIEVAL=1`（默认）时不走。

    延迟 import `_chunk_tree`：`service` 顶层会 import 本模块（经 `prefs` 的
    其它入口），反向 import 就是环。切块算法**只有这一份**，这里只是消费它。

    子块为空的材料（零宽父块，见 `_chunk_tree` 的说明）退回父块：退回去最多是
    这几条样本命中不了缓存，不会比"这份材料一条样本都不贡献"更差。
    """
    from app.knowledge.service import _chunk_tree

    tree = _chunk_tree(text)
    leaves = [(node.start, node.end) for node in tree if node.level == 1]
    return leaves or [(node.start, node.end) for node in tree]


def chunk_samples(sources: Sequence[Any]) -> list[ChunkSample]:
    """这批材料的块 → **现成向量**。任何一条取不到就跳过，绝不补 embed。

    "不补 embed"是这一整节存在的理由，不是保守：

      - 补一次 embed 就是开关一打开多 N 次往返，而那正是默认关着的理由；
      - 取不到的块只在**这份材料还没被任何检索碰过**时出现（刚上传、立刻生成）。
        那一刻的正确行为是"这次没有方向、不加权"，不是"先花 6 次往返凑一批"。

    取块用**轮流**（round-robin）而不是"前 60 条按顺序砍"：一张图 3 份材料、
    第一份 200 块、另两份各 10 块时，砍尾巴等于方向全部来自第一份材料。轮流取的
    效果是每份材料各占 1/3 配额，与 `_samples` 按来源类型分层配额同一条理由。
    """
    model = embedding.model_name()
    per_source: list[list[ChunkSample]] = []
    for source in sources:
        text = str(getattr(source, "text", "") or "")
        if not text.strip():
            continue
        name = str(getattr(source, "filename", "") or getattr(source, "id", "") or "")
        spans = _sub_spans(text)
        keys = [vector_cache.text_hash(text[start:end]) for start, end in spans]
        cached = vector_cache.get_vectors(keys, model)
        found: list[ChunkSample] = []
        for (start, _end), key in zip(spans, keys):
            vector = cached.get(key)
            if vector:
                found.append(ChunkSample(vector=vector, tag=f"{name}#{start}"))
        if found:
            per_source.append(found)
    return _round_robin(per_source, MAX_SAMPLES)


def _round_robin(groups: Sequence[Sequence[ChunkSample]], limit: int) -> list[ChunkSample]:
    """按份轮流取，每份最多 `limit` 条，总数不超过 `limit`。"""
    ordered: list[ChunkSample] = []
    cursor = 0
    while len(ordered) < limit and any(cursor < len(group) for group in groups):
        for group in groups:
            if cursor < len(group) and len(ordered) < limit:
                ordered.append(group[cursor])
        cursor += 1
    return ordered


def build_directions_from_sources(sources: Sequence[Any]) -> list[Direction]:
    """**这张图的材料** → 这张图的方向。零 embedding 往返，没有向量服务也能用。

    与 `build_directions`（老路径：库里三张表的全局样本 + 现 embed）的分工：

      - 这个函数是生成链路的唯一入口。它的输入是"用户为这张图传的材料"，所以
        两张不同主题的图**没有任何共享的自由度**——图 B 的检索不可能被图 A 的
        方向加权，那是隔离的全部内容；
      - `build_directions` 留着给脚本与面板（`rebuild()` 不带材料时的老口径），
        它仍然是全局的，也就仍然是"跨图串味"的，**不要在生成链路里调它**。

    不检查 `embedding.configured()`：这一路**不发请求**，只用 `chunk_vectors` 里
    已有的向量。缓存是别的（配了凭证的）进程写进去的，它们同样有用——按"没凭证
    就等于没有方向"把它挡掉，会让"有向量但刚换了环境变量"这种情况白白降级。

    样本不足（`MIN_SAMPLES`）返回空列表，**不抛错**：新图没有材料时这就是正常
    路径——没有样本 → 没有方向 → 不加权。这条降级不特判，也不记 warning；调用方
    用 `Boost.reason` 给用户一句可读的诊断。
    """
    samples = chunk_samples(sources)
    if len(samples) < MIN_SAMPLES:
        logger.info("画像：这张图的材料只给出 %d 条块向量（需要 %d 条），本次没有方向",
                    len(samples), MIN_SAMPLES)
        return []
    matrix = [_normalize(sample.vector) for sample in samples]
    # 块之间**不带时间衰减**：它们是同一张图这次用的材料，彼此没有先后。衰减是
    # 给"一个人换了课题"用的（`build_directions` 那条路），在这里加只会让
    # "刚上传的另一份材料"无故变轻。
    weights = [1.0] * len(matrix)
    groups = cluster(matrix, _pick_k(len(matrix)), weights)
    total = float(len(matrix)) or 1.0
    directions: list[Direction] = []
    for center, members in groups:
        best = max(members, key=lambda position: _dot(matrix[position], center))
        directions.append(Direction(
            label=samples[best].tag[:32] or f"{len(members)} 块",
            vector=center,
            weight=round(len(members) / total, 4),
            samples=len(members),
        ))
    directions.sort(key=lambda item: (-item.weight, item.label))
    logger.info("画像：这张图 %d 份材料 / %d 条块向量聚成 %d 个方向（%s）",
                len(sources), len(matrix), len(directions),
                "；".join(f"{d.label}({d.samples})" for d in directions))
    return directions


# ---------------------------------------------------------------- 落库与读回


def rebuild(
    map_id: str | None = None, *, sources: Sequence[Any] | None = None,
    owner: str | None = None,
) -> list[Direction]:
    """重算方向并按池键落库。样本不足时**清空这个池**并返回空列表。
    两种口径，由 `sources` 是否给出来分开，**不要混着用**：

      - `sources is None`（默认）→ 老全局口径：`_samples(owner)` 三张表的样本 + 现
        embed（最坏 6 次 HTTP）。这是 `--report` 与面板用的，它会跨图串味，因为
        它本来就是"这个人所有材料"的平均；
      - `sources` 给了 → 这张图的材料（`build_directions_from_sources`，零往返）。
        生成链路走这条（由 `rebuild(map_id=pool, sources=request.sources)` 显式
        触发，或不落库直接算）。

    `owner` 只影响 `_samples` 里 `memory_items` 那一张（见那条 docstring）：
    记忆是私有的，材料与导图是这台机器共享的。

    清空的范围是**这个池**，不是整张表：`DELETE FROM ... WHERE map_id = ?`。
    否则给一张图重建方向会把其它图（以及隔离之前的全局方向）一起抹掉——那是
    "隔离"最不该有的副作用。`map_id=None` 归一成空串，正好只清"全局池"那一批。

    主键 `id` 从 `d0` 改成 `{pool}#d{index}`：`profile_vectors.id` 是主键，
    多个池共用 `d0` 会让第二个池的第一条方向静默写不进去（`INSERT` 撞主键抛错，
    而这里没有 `OR IGNORE`）。改动是纯机械的，`stored` 从不读 `id`。
    """
    pool = "" if map_id is None else str(map_id)
    directions = (
        build_directions_from_sources(sources) if sources is not None
        else build_directions(_samples(_owner() if owner is None else owner))
    )
    with closing(_connect()) as conn, conn:
        conn.execute(
            f"DELETE FROM {VECTOR_TABLE} WHERE COALESCE(map_id, '') = ?", (pool,))
        for index, direction in enumerate(directions):
            conn.execute(
                f"""INSERT INTO {VECTOR_TABLE}
                        (id, vector, label, weight, samples, created_at, updated_at, map_id)
                    VALUES (?, ?, ?, ?, ?, {_NOW}, {_NOW}, ?)""",
                (f"{pool}#d{index}", vector_cache.pack(direction.vector), direction.label,
                 direction.weight, direction.samples, pool),
            )
    if directions:
        logger.info("画像向量已更新：%d 个方向（池 %s）", len(directions), pool or "全局")
    return directions


@dataclass(frozen=True)
class Stored:
    label: str
    vector: list[float]
    weight: float
    samples: int
    updated_at: str
    #: 这个方向属于哪个池。空串 = 隔离之前的全局方向（`stored(map_id=None)` 才看得到）。
    map_id: str = ""


def stored(*, map_id: str | None) -> list[Stored]:
    """读某个池现成的方向。**不重算、不抛错**（读失败就当没有画像）。

    `map_id` **没有默认值**，这是刻意的：漏传时 Python 直接抛 `TypeError`，而不是
    静默退化成"读全部池"——**那正是这个 bug 的形状**（全局方向被当成这张图的
    方向用）。加一个 `= None` 的默认值，代码里就再也不会有人注意到这件事。

    三种取值，语义各不相同：

      - `None` → **全量**（含隔离之前的全局行）。只给脚本与排查用：`--report`
        要能看到"老的那 4 个方向还在不在"；
      - `"…"`（含空串）→ 只这个池。`WHERE COALESCE(map_id,'') = ?` 让隔离之前那批
        NULL 行**不返回**：它们属于"没有隔离概念的时代"，任何一张图都不该被它们
        加权。空串是显式要"全局池"的写法，与 `None`（全量）不是一回事；
      - 池里没有行 → 空列表。新图没材料时就是这条，调用方据此走"不加权"。

    维度校验与 `memory.store.recall` 同一条规矩：换了 embedding 模型/维度之后，
    库里的旧向量与新的查询向量不在同一个空间里，点积会得到一个"看起来合理、
    实际是垃圾"的数。对不上就整条跳过，绝不截断对齐。
    """
    dim = embedding.dimension()
    columns = "label, vector, weight, samples, updated_at, COALESCE(map_id, '') AS pool"
    with closing(_connect()) as conn:
        if map_id is None:
            rows = conn.execute(
                f"SELECT {columns} FROM {VECTOR_TABLE} "
                "ORDER BY weight DESC, label").fetchall()
        else:
            rows = conn.execute(
                f"SELECT {columns} FROM {VECTOR_TABLE} "
                "WHERE COALESCE(map_id, '') = ? "
                "ORDER BY weight DESC, label", (str(map_id),)).fetchall()
    result: list[Stored] = []
    for row in rows:
        vector = vector_cache.unpack(row["vector"], dim)
        if not vector:
            continue
        result.append(Stored(
            label=str(row["label"]), vector=vector, weight=float(row["weight"]),
            samples=int(row["samples"]), updated_at=str(row["updated_at"]),
            map_id=str(row["pool"])))
    return result


def stale(*, map_id: str | None = None, now: datetime | None = None) -> bool:
    """这个池的画像是否过期：最旧那条 `updated_at` 距现在超过 `STALE_HOURS`。

    取**最旧**而不是最新：几条方向是一起 rebuild 出来的，但某一条可能是更早那轮
    留下的。只有所有方向都还新鲜，这个画像才新鲜。

    `map_id` 默认 `None`（全量）而不是强制显式：这个函数只进日志与面板，判断错了
    不影响任何一次排序——而它的调用方（`--report`）本来就在看全量。
    """
    rows = stored(map_id=map_id)
    if not rows:
        return True
    ages = [age for age in (age_days(row.updated_at, now=now) for row in rows)
            if age is not None]
    if not ages:
        return True
    return max(ages) * 24.0 >= STALE_HOURS


# ---------------------------------------------------------------- 加进排序


def _cosines(
    hits: Sequence[Any], vectors: Sequence[Sequence[float]],
) -> dict[int, tuple[float, int]]:
    """每条命中 → (与所有方向里最贴的那个的余弦, 那个方向的下标)。

    取 max 而不是平均，是这一层唯一的数学主张：这个人的每个方向都是一个独立的
    兴趣，一条命中属于其中一个就已经是"他的东西"。取平均会把"只属于一个方向"的
    命中稀释成 1/N——而"完全属于某个方向"恰恰是最强的偏好信号。

    连"是哪个方向赢的"一起返回，是因为 α 要乘那个方向的权重（见 `boost`）：
    只回余弦的话，"不像"与"方向本来就轻"在最终分上长得一样，排查时分不开。
    """
    texts: list[str] = []
    indexes: list[int] = []
    for index, hit in enumerate(list(hits)[:MAX_HITS]):
        quote = str(getattr(hit, "quote", "") or "").strip()
        if not quote:
            continue
        texts.append(quote[:SAMPLE_CHARS])
        indexes.append(index)
    if not texts:
        return {}
    model = embedding.model_name()
    keys = [vector_cache.text_hash(text) for text in texts]
    cached = vector_cache.get_vectors(keys, model)
    pending = [(pos, text) for pos, text in enumerate(texts) if keys[pos] not in cached]
    fetched: dict[int, list[float]] = {}
    if pending:
        try:
            fresh_vectors = embedding.embed_texts([text for _, text in pending])
        except embedding.EmbeddingError as exc:
            logger.info("画像加权跳过：向量服务失败（%s）", type(exc).__name__)
            return {}
        written: list[tuple[str, list[float]]] = []
        for (pos, _), vector in zip(pending, fresh_vectors):
            if vector:
                unit = _normalize(vector)
                fetched[pos] = unit
                written.append((keys[pos], unit))
        if written:
            vector_cache.put_vectors(written, model)

    scores: dict[int, tuple[float, int]] = {}
    for pos, hit_index in enumerate(indexes):
        query = cached.get(keys[pos]) or fetched.get(pos)
        if not query:
            continue
        query = _normalize(query)
        best = max(range(len(vectors)),
                   key=lambda slot: (_dot(query, vectors[slot]), -slot))
        scores[hit_index] = (_dot(query, vectors[best]), best)
    return scores


def boost(
    hits: Sequence[Any],
    *,
    directions: Sequence[Stored | Direction],
    alpha: float = ALPHA,
) -> Boost:
    """把画像贴合度加进**排序**，返回重排后的列表与被抬上来的那几条。

    ```
    norm    = min_max(hit.score)                 # 本批内的相对分
    final   = norm + alpha * max(cos(hit, v))    # v 遍历**这张图**的方向
    ```

    `directions` **是必填的、且不接受默认值**。它曾经默认 `stored()`（读全表），
    而那正是跨图串味的来源：一张讲机器学习的图，检索时被"碳中和"那个方向加权，
    而且没有任何地方看得出来。改成必填之后，调用方必须显式回答"用哪个池"，
    漏答是 `TypeError`（立刻、可见），而不是一次静默的全局加权。

    `v` 只遍历传入的这几根向量，所以"两张图互不加权"不是靠调用方自觉——**另一个
    池的方向根本不在这个列表里**。这是隔离在打分层的落点。

    **为什么不能直接 `hit.score + alpha * cos`**（也就是"候选分数 = BM25 + 余弦 +
    RRF + α·…"的字面写法）：本仓库的 `hit.score` 在不同路径上是三种口径
    （BM25 原始分、单路 RRF 分、多路 RRF 累加），多路 RRF 落在 0.02–0.08 这个
    量级，而余弦在 [0,1]。直接把 `0.15 * 0.9 = 0.135` 加进去，会**完全压倒**融合分
    ——排序变成"只按画像"，相关性彻底失效。所以先在本批内归一化到 [0,1]，α 才是
    "画像能撼动多少"的可解释刻度（见 `ALPHA` 的注释）。

    **只在排序用**：返回的列表与输入**等长、同一批对象**，只是顺序不同。
    没有一条被删、没有一条被换。约束 1 就是靠这一行保证的。

    `score` 字段**不改**：它是检索侧的融合分，改了会让"这条为什么是 0.07"再也
    说不清，而 `EvidenceHit` 是 `extra="forbid"` 的契约类型，也没有地方放第二个分数。
    """
    if not hits:
        return Boost(hits=[], lifted=[])
    current = list(directions)
    if not current:
        # 这条分支是**新图的正常路径**，不是错误：没有材料 → 没有样本 → 没有方向
        # → 不加权。给一句人话，否则用户只能靠"好像没生效"来猜。
        return Boost(hits=list(hits), lifted=[],
                     reason="这张图还没有可用于算方向的材料块向量，这次不做个性化加权")
    limit = min(max(0.0, float(alpha)), MAX_ALPHA)
    if limit <= 0:
        return Boost(hits=list(hits), lifted=[], reason="画像加权的 α 为 0，这次不加权")
    # 权重进 α：一个只占 5% 样本的方向不该与占 40% 的方向同样重。用相对权重
    # （最快最强那个方向 = 1.0）而不是绝对值，因为 `weight` 是占比、总和为 1。
    top = max((item.weight for item in current), default=0.0) or 1.0
    scaled = [(item, limit * (item.weight / top)) for item in current]

    cosines = _cosines(hits, [item.vector for item in current])
    if not cosines:
        return Boost(hits=list(hits), lifted=[])

    raw = [float(getattr(hit, "score", 0.0) or 0.0) for hit in hits]
    low, high = min(raw), max(raw)
    span = high - low
    norm = [((value - low) / span) if span > 0 else 1.0 for value in raw]
    gate = max(norm) * GATE_RATIO

    final: list[float] = []
    bonuses: dict[int, float] = {}
    for index, base in enumerate(norm):
        picked = cosines.get(index)
        if picked is None or base < gate:
            # 过不了主题门：这条**不参与画像加分**，但仍留在列表里（只是不加分）。
            final.append(base)
            continue
        cosine, slot = picked
        if cosine < MIN_COSINE:
            # 过不了**贴合门**：与这张图的任何一个方向都不够像，说明它不属于这张图
            # （多半是知识库里另一张图的材料被 `library=True` 带进来的）。加分给它
            # 就是跨图污染——理由与实测见 `MIN_COSINE`。
            final.append(base)
            continue
        # α 由"赢的那个方向有多重"定标，而不是一律用 `limit`：一个只占 5% 样本
        # 的方向不该与占 40% 的方向同样重。除以 `top` 是把权重归一成"最强的方向
        # = 1.0"，这样默认参数下最强方向拿到满额 α，弱方向按比例缩。
        alpha_for = scaled[slot][1]
        bonus = cosine * alpha_for
        final.append(base + bonus)
        bonuses[index] = round(bonus, 6)

    order = sorted(range(len(hits)), key=lambda i: (-final[i], i))
    reordered = [hits[index] for index in order]
    position = {id(hit): rank for rank, hit in enumerate(hits)}
    lifted = [hit for rank, hit in enumerate(reordered)
              if position.get(id(hit), rank) > rank]
    if lifted:
        logger.info("画像加权：%d 条命中里 %d 条被抬上来（α=%.2f，%d 个方向）",
                    len(hits), len(lifted), limit, len(current))
    return Boost(hits=reordered, lifted=lifted, bonuses=bonuses,
                 cosines={index: value for index, (value, _) in cosines.items()})


def prompt_directions(*, map_id: str | None = None) -> str:
    """画像方向 → 一行可读的描述（进日志或面板，**不进提示词**）。

    方向不进提示词：模型不需要知道"这个人有 4 个兴趣方向"这件事，它需要的只是
    这次的材料与这次的图。把方向写进 system 会让它开始猜"这次该往哪个方向靠"，
    而那不是它的活。

    `map_id` 默认 `None`（全量）：这个函数只进日志与排查面板，把全量列出来正好
    回答"隔离之前那批全局方向还在不在"。要看某一个池就显式传池键。
    """
    rows = stored(map_id=map_id)
    if not rows:
        return ""
    return "；".join(f"{row.label}（{row.samples} 条，权重 {row.weight:.2f}）"
                    for row in rows)


__all__ = [
    "ALPHA",
    "GATE_RATIO",
    "MAX_ALPHA",
    "MAX_CLUSTERS",
    "MAX_HITS",
    "MAX_SAMPLES",
    "MIN_CLUSTERS",
    "MIN_COSINE",
    "MIN_MINDMAP_NODES",
    "MIN_SAMPLES",
    "POOL_RUN_PREFIX",
    "SAMPLE_CHARS",
    "STALE_HOURS",
    "SWITCH_ENV",
    "VECTOR_TABLE",
    "Boost",
    "ChunkSample",
    "Direction",
    "Sample",
    "Stored",
    "boost",
    "build_directions",
    "build_directions_from_sources",
    "chunk_samples",
    "cluster",
    "enabled",
    "ensure_schema",
    "pool_key",
    "prompt_directions",
    "rebuild",
    "stale",
    "stored",
]
