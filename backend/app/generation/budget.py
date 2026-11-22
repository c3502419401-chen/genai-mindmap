"""单次模型调用的产出预算：**要求多少，就得装得下多少**。

这个文件只做一件事：把那条一直散落在注释里的约束写成可断言的算术。

    ┌──────────────────────────────────────────────────────────────────┐
    │ 一批（一次模型调用）要求的产出规模，必须装进三个上限的最小值：      │
    │                                                                  │
    │   节点数 × (满额 label + detail + refs 的 token)                  │
    │   + JSON 开销 + 推理余量                                          │
    │   ≤ min(单次 max_output_tokens, 模型输出上限, 剩余上下文)          │
    └──────────────────────────────────────────────────────────────────┘

为什么必须写成代码：在这之前，"一次调用最多要几个节点"是由 `points_per_branch`
与 `fanout` 决定的（`DEEP_FANOUT=5`、一层容忍再多给一层 → 一次最多 5 + 25 = 30 个
节点），而"一次调用能装下多少"从来没人算过。两件事各自都"看起来合理"，乘起来
却会撞在 `max_tokens` 上——表现是模型写到一半被硬切，`finish_reason=length`，
用户拿到半棵树。**从提示词那边看，模型完全照做了；从预算这边看，要求本身就不该
发出去。**

三条边界的来源不同，所以取最小值而不是取某一个：

  - `max_output_tokens`：`RunBudget` 里用户/调用方可配的单次上限；
  - 模型输出上限：服务端真正的接受范围（`MINDMAP_LLM_MAX_TOKENS`，以及 400 时
    从 `Range of max_tokens should be [1, N]` 里解析回来的那个 N）；
  - 剩余上下文：`CONTEXT_TOKEN_LIMIT` 减去本轮输入占用。上下文是输入输出共享的，
    输入吃满时输出空间会跟着缩水。

反向的用法同样重要：给定可用输出预算，反解出"这一次最多能要几个节点"
（`nodes_per_call`）。调用方拿它去夹 `_generate_deep` 的 `allowance` 与
`point_budget`，于是"prompt 要求 100 节点、单次输出 40 节点就截断"这件事在
结构上不可能发生——要求量在发请求之前就已经被预算夹住了。
"""
from __future__ import annotations

import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.contracts.mindmap import GENERATED_LABEL_MAX_CHARS

# 输出侧的 token 折算率。与 `generation.service.estimate_input_tokens` 同一口径
# （汉字 1.5 字/token、ASCII 4 字符/token），但不直接 import 它：那个函数的
# docstring 与默认参数都是给"输入装不装得进上下文"这个问题写的，输出侧还要多算
# 转义与结构开销，混用一个函数会让两边的人都以为改一处能同时改两边。
_CJK_TOKENS_PER_CHAR = 1 / 1.5
_ASCII_TOKENS_PER_CHAR = 1 / 4

# 「这次运行最多要几个节点」的第一次承诺。与 `generation.service` 的提示词、
# `agent.tools._generate_deep` 的分配算术共用同一个数：三处各写一个 80，改一处
# 忘一处的结果是"提示词说要 80、工具按 100 分、用户拿到 100"。
INITIAL_MAX_NODES = 80

# 初次生成的深度上限，**按 root 之下算**（与 `GenerateDeepArgs.max_depth` 同口径）。
# 3 = 4 层含根。差一层是这里最容易犯的错：写成 4 就变成 5 层树，节点数上界从
# 79 变成 1+6+18+54+162 = 241，第一次生成必然撑爆调用预算。
INITIAL_MAX_DEPTH = 3

# 一个节点在 JSON 里的固定开销：键名（"label"/"detail"/"expandable"/
# "source_refs"）、引号、冒号、逗号、大括号。实测一个只有 label 的节点序列化成
# `{"label":"…","expandable":false}` 约 34 token，取 40 留一点余量。
NODE_STRUCTURE_TOKENS = 40

# 「满额 detail」的口径。这里刻意**不用**契约上限 2000：
#
#   - 2000 是"防跑飞"的硬上限（`MAX_NODE_DETAIL_CHARS`），不是每次都会写满的
#     期望值。提示词要的是"2–3 句说明"，中文一两句 20–40 字，2–3 句 40–120 字，
#     200 是给足发挥余量的那一侧；
#   - 按 2000 算的后果是公式自己失效：单节点约 2500 token，一次 65536 的调用
#     只"装得下" 26 个节点，而实际请求只要 2–5 个——于是每一次正常请求都会被
#     判成超预算，守卫变成阻塞器。
#
# 两个口径都要有，但用在不同地方：常规预算判定用 200（这个常量），"满额守卫"
# 测试（`tests/test_budget.py`）显式传 `detail_chars=MAX_NODE_DETAIL_CHARS`，
# 验的是"极端输入要么装得下、要么被拆批"。
PROMPT_DETAIL_MAX_CHARS = 200

# 单次响应除节点之外的固定开销：顶层对象/数组、`children` 键、收尾括号。
ENVELOPE_TOKENS = 24

# 一条引用的 token：`{"source_id":"…","start":…,"end":…,"quote":"…"}` 里 quote
# 的实际上限是 300 字（`RAG_SOURCE_PROMPT` 写死的那个数），300 字 ≈ 200 token，
# 加上其余字段约 30。取 230。
REF_STRUCTURE_TOKENS = 30
REF_QUOTE_TOKENS = 200

# 每个节点按几条引用算。分片生成的实测分布是 0–2 条（`DEEP_BRANCH_TOP_K=3` 条
# 命中，模型通常只给其中一两处挂引用），按 2 算是**上界**：按平均值算会在某些
# 批次上系统性低估，而低估的代价是截断。
REFS_PER_NODE = 2

# 推理余量。推理模型（DeepSeek-R1 一类）的思考 token 也算在 `max_tokens` 里，
# 且不会出现在正文里。35% 来自实测：同一份材料、同一提示词，带思考的模型正文
# 与思考的比例大致 2:1 到 3:1；取 0.35 是"足够装下思考"的那一侧。
REASONING_MARGIN_RATIO = 0.35

# 一次响应至少要能装下的节点数。算出 0 时不是"允许发一次只写一个节点的请求"，
# 而是"这一批根本不该发"——调用方看到 0 应当停手（`_generate_deep` 的
# `DEEP_MIN_BREADTH` 是同一个意思的另一个入口）。
MIN_NODES_PER_CALL = 1

# ---------------------------------------------------------------- 规模随材料伸缩
#
# `INITIAL_MAX_NODES = 80` 是**与材料无关**的常数，而实测它是模型的填额目标：
#
#     材料 492 字（1 份）   → 80 节点 / 9.3–10.5 千字说明   压缩比 0.05:1
#     材料 5,616 字（16 份）→ 80 节点 / 10.3–11.0 千字说明  压缩比 0.51–0.54:1
#     材料 66,536 字（4 份）→ 80 节点 / 12.2 千字说明       压缩比 5.45:1
#
# 材料量翻 135 倍，说明总量只涨 30%——图不是材料的函数，是那个 80 的函数。数字见
# `scripts/measure_scale.py` 与 `data/scale-baseline.json`。
#
# 这里的三个系数是**三个候选判据**，各自在哪一批上是唯一合理的那个：
#
#   - 字数（`CHARS_PER_NODE`）：永远可用。66,536/800 = 83 撞上限，与现状吻合；
#     但它在"多份短材料"上系统性低估——5,616 字是 16 篇独立摘要，不是一篇
#     5,616 字的文章，按字数它只是 7 个节点。
#   - 份数（`SOURCES_PER_NODE`）：补的正是上面那一格。16 份 → 32；而 4 份长文档
#     只给 8，那 4 份里有 30 章、66,536 字，"每份 2 个节点"是荒谬的。
#   - 章节（`CHAPTERS_PER_NODE`）：最接近"材料里有多少个要点"，实测 4 份长文档
#     抽出 30 条（覆盖 91%），30 × 5 = 150 撞上限；但纯摘要上它**完全失效**：
#     小批 1 份 492 字、中批 16 份 5,616 字，`heading_outline` 都是 0 条。
#
# 所以判据不是"选一个"，是**取三者的最大值**：每个判据都在某一格上失效，而失效
# 的方向永远是低估，取 max 只会在最该大的那一格上大。下限 `MIN_INITIAL_NODES`
# 是结构算术（3 支 × 每支 2 个要点 + 根，见下面那个常量的注释）；上限仍是
# `INITIAL_MAX_NODES`——那是可读性上限，不该随材料量涨（5 万字材料想要更多细节时，
# `node_expand` 是既有的那条路）。
NODE_SCALE_ENV = "MINDMAP_NODE_SCALE"

#: 多少材料字数摊一个节点。800 的依据是现状：66,536/800 = 83，与"首次 80 节点"
#: 这个已经跑了几百次的值吻合——它保证**大材料的行为不变**，改的只是小材料。
CHARS_PER_NODE = 800

#: 每份材料至少摊几个节点。2 = 一份材料至少要在图上占两处（标题 + 一处结论），
#: 低于它时"材料被总结了"这件事在图上看不出来。
SOURCES_PER_NODE = 2

#: 每个章节至少摊几个节点。5 = 一章至少「标题 + 两三个要点 + 一个收束」。
CHAPTERS_PER_NODE = 5

#: 下限：**结构算术**，不是口味。`MIN_BRANCHES=3`（契约）× 每支至少 2 个要点
#: （`_SKELETON_BODY`）+ 根 = 7，再留一格给"根下直接挂的收束"，取 10。
#:
#: **但它不是宽度的杠杆，实测说得很清楚**：492 字材料 / 同一主题，把上限设成
#: 8 / 10 / 13 时，六次采样落树后**都只有 1 条一级分支**（`{'1':1,'2':1,'3':5,'4':3}`
#: 这种一条链的形状；骨架那一次要的是 6 条，`MIN_BRANCHES` 的校验只作用在骨架上）；
#: 同一批材料在上限 32 时给 3 支、上限 80 时给 5–6 支。
#: 也就是说宽度跟着**配额的量级**走，不跟着下限走——想让小材料也铺成 3 支，
#: 得改提示词里"拆深"与"铺宽"的权重（`DETAIL_PROMPT` 的展开规则占了那一份的
#: 大半），而不是把这个数调大。这一条必须留在这里：调大下限只会让说明更膨胀
#: （实测压缩比 8→0.44:1、10→0.33:1、13→0.26:1），一级分支一条都不多。
MIN_INITIAL_NODES = 10


def node_scale_enabled() -> bool:
    """`MINDMAP_NODE_SCALE=1` 才按材料伸缩；不设 = 关（保持 80，与改动前逐字节相同）。

    默认关的理由与其它 A/B 开关一致：这个数改的是**每一次生成的规模**，翻它之前
    得先有一组数字（`data/scale-baseline.json` 是现状那一组）。开关而不是直接改
    默认值，是为了让"要不要翻"成为一个可以单独回滚的决定。
    """
    return os.getenv(NODE_SCALE_ENV, "").strip().lower() in ("1", "on", "true", "yes")


def scaled_initial_nodes(
    *,
    material_chars: int,
    sources: int,
    chapters: int,
    ceiling: int = INITIAL_MAX_NODES,
) -> int:
    """这次生成该要求几个节点：三条判据取最大值，夹在 `[MIN_INITIAL_NODES, ceiling]`。

    三个入参都可以是 0（没有材料 / 抽不出章节），此时对应那一项不参与比较。
    返回的是**要求量**，不是上限承诺——模型仍可以少写，`_clip_tree` 仍按这个数裁。
    """
    votes = [
        max(0, material_chars) / CHARS_PER_NODE,
        max(0, sources) * SOURCES_PER_NODE,
        max(0, chapters) * CHAPTERS_PER_NODE,
    ]
    target = max(votes) if votes else 0.0
    return max(MIN_INITIAL_NODES, min(max(1, ceiling), int(round(target))))


# ---------------------------------------------------------------- 手动配额（实验用）
#
# `scaled_initial_nodes` 把配额绑在"材料有多大"上，于是它**问不出**"配额本身是不是
# 损失的来源"：中批上它永远给 32，改不出 48。而"配额 × 材料清单"这组实验要的正是
# 同一批材料上只动配额——
#
#     格 1  32 + 清单关      格 2  32 + 清单开
#     格 3  48 + 清单关      格 4  48 + 清单开
#
# 所以要有一个人为的旋钮。**它只接管"要求几个节点"这一个数**，其余链路（提示词里
# 那句要求、`_clip_tree` 的上限、`data["node_limit"]`）仍走同一个 `initial_nodes`，
# 否则又会造出"要 32 / 裁 80 / 报 80"那种自相矛盾的产物。
#
# 默认不设 = 现状：这一格连 `scaled_initial_nodes` 都不查（开关关着时它不参与），
# 逐字节回到 `INITIAL_MAX_NODES`。实验用的旋钮，不该改变任何人已有的行为。
NODE_CAP_ENV = "MINDMAP_NODE_CAP"


def node_cap() -> int | None:
    """`MINDMAP_NODE_CAP` 的值；不设、空、非整数、非正数时 `None`（= 不接管）。

    边界与 `scaled_initial_nodes` 用同一套 `[MIN_INITIAL_NODES, INITIAL_MAX_NODES]`：
    手动配额不是绕过可读性上限的通道（超过 `INITIAL_MAX_NODES` 的部分没有输出预算
    装它，见 `batch_output_tokens` 那套算术），也不该低到装不下 `MIN_BRANCHES`
    的结构算术。**不设时返回 `None` 而不是 80**——`None` 才是"没接管"，80 是"接管
    成 80"，两者在 `initial_nodes` 里要走不同的分支，混成一个值就没法证明默认路径
    没变。
    """
    raw = os.getenv(NODE_CAP_ENV, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value <= 0:
        return None
    return max(MIN_INITIAL_NODES, min(value, INITIAL_MAX_NODES))


def initial_nodes(
    *,
    material_chars: int,
    sources: int,
    chapters: int,
    ceiling: int = INITIAL_MAX_NODES,
) -> int:
    """本次初次生成要求几个节点。三个来源按**优先级**取，不是取最小值：

        1. `MINDMAP_NODE_CAP` 设了 → 就是它（手动配额压过一切，实验需要）；
        2. 否则 `MINDMAP_NODE_SCALE` 开着 → `scaled_initial_nodes`（按材料伸缩）；
        3. 否则 → `INITIAL_MAX_NODES`（80，现状，与加伸缩之前逐字节相同）。

    优先级而不是"谁小听谁"：配额是**实验变量**，它必须能在同一批材料上取任意值，
    否则四格里的 48 那一列根本跑不出来（伸缩判据只会给 32）。不设开关时第一个
    分支不成立，第二个也不成立，返回值就是 `ceiling`——那条路径与改动前逐字节相同。
    """
    cap = node_cap()
    if cap is not None:
        return cap
    if node_scale_enabled():
        return scaled_initial_nodes(
            material_chars=material_chars, sources=sources, chapters=chapters,
            ceiling=ceiling)
    return ceiling


# ---------------------------------------------------------------- 每支预算（预算不该压掉分支数）
#
# `initial_nodes` 算出来的是**一个总量池**，而它同时去了三处：提示词里那句"控制在 N
# 个节点以内"、`_clip_tree` 的上限、`data["node_limit"]`。总量池 + **前序裁剪**这两件
# 事叠在一起，会把一级分支数一起压掉——实测（`docs/GRAPH-SCALE.md` 第 7 节）：
#
#     同一份材料（openalex 两篇摘要 424 字）、同一个骨架（3 条一级分支）
#     initial_nodes=80 → 3 支 / 64 节点 / 裁掉 0
#     initial_nodes=10 → **1 支** / 10 节点 / 裁掉 29
#
# 那次裁剪**之前**树上明明有 39 个节点、3 条分支，模型完全照骨架写了。是 `_clip_tree`
# 按前序保留前 10 个，而第 1 支的子树一口气吃掉 9 个名额，第 2、3 支一个都没剩下。
# 所以这里要修的不是"上限大小"，是"名额怎么分"。
#
# 分支数是**用户显式选的**（界面下拉）加骨架定下的，不该被内部预算压掉；材料量该影响
# 的是"每支多深/多厚"，不是"几支"。于是总量池折成**每支预算**：
#
#     M     = clamp(round(N / 支数), 3, 20)   ← 提示词里说的那个数
#     total = max(N, 支数 × M)                ← 裁剪上限（仍夹在 INITIAL_MAX_NODES 内）
#     share = (total - 1) / 支数               ← 裁剪时每条一级分支的名额
#
# `total` 取 `max(N, 支数 × M)` 而不是 `支数 × M`：M 有下限 3，薄材料上 `支数 × M`
# 会小于 N（424 字 3 支 → 9 < 10），而输出预算不该因为分支变多就缩——N 是"这次装得下
# 多少"，它不归支数管。反过来 M 撞上限时（N=80、6 支 → 13），总量也仍以 N 为底。
#
# 两个夹角的依据都是结构算术，不是口味：
#   - 下限 3 = 「分支自己 + 两个要点」。`_SKELETON_BODY` 要的是每支至少 2 个要点，而
#     一条分支只剩 1 个节点时，它在画布上就是个孤立标签：点开什么都没有，比"这一支被
#     砍掉"更难解释（被砍掉至少看起来是"没展开"）。
#   - 上限 20 = "这一支在吃别人的预算"那条线。`INITIAL_MAX_NODES / MIN_BRANCHES` 是 26，
#     一支占到 20 已经不是分摊而是独吞。
BRANCH_MIN_NODES = 3
BRANCH_MAX_NODES = 20

#: 实验开关：恢复"总量池 + 前序裁剪"的旧口径（默认不设 = 新的按支摊）。
#:
#: 为什么留一个这样的旋钮：这个改动同时动了提示词与裁剪两处，而"哪一处在起作用"只能
#: 靠同一份代码上的两组数回答。`MINDMAP_NODE_SCALE` 关着时它完全不参与（默认路径连
#: 这个函数都不会调用），所以它不会成为第二个语义入口。
NODE_POOL_ENV = "MINDMAP_NODE_POOL"


def node_pool_mode() -> bool:
    """`MINDMAP_NODE_POOL=1` → 退回旧口径（总量池、整句进提示词、前序裁剪）。"""
    return os.getenv(NODE_POOL_ENV, "").strip().lower() in ("1", "on", "true", "yes")


def nodes_per_branch(*, initial_nodes: int, branches: int) -> int:
    """这一次**每条一级分支**能写几个节点（含分支自己）。

    `branches` 是画布上的一级节点数（两级时是分组数，不是分组下面那二十节）。
    """
    count = max(1, branches)
    return max(
        BRANCH_MIN_NODES,
        min(BRANCH_MAX_NODES, int(round(max(1, initial_nodes) / count))),
    )


def branch_budget(
    *, initial_nodes: int, branches: int, ceiling: int = INITIAL_MAX_NODES,
) -> tuple[int, int]:
    """把总量池折成 `(每支预算, 总量上限)`。

    两个数都要回给调用方：提示词说"每条分支 M 个"，裁剪按"总量 total、每支 share"，
    而 `data["node_limit"]` 报的是 total——三处仍用同一个口径，只是这个口径现在**随
    支数走**（8 支的图比 2 支的图能装下更多节点，这正是它该有的行为）。

    `total` 取 `max(N, 1 + 支数 × M)`：`1` 是中心主题。少了这个 1，"每支 3 个 × 6 支"
    折出来的总量是 18，而裁剪要的是 1 + 18 = 19——`branch_share` 于是算出 2，
    与提示词里说的 3 对不上，模型被要求写 3 个却只能留 2 个。
    """
    per_branch = nodes_per_branch(initial_nodes=initial_nodes, branches=branches)
    total = max(max(1, initial_nodes), 1 + max(1, branches) * per_branch)
    return per_branch, min(max(1, ceiling), total)


def branch_share(*, total: int, branches: int) -> int:
    """裁剪时每条一级分支能留下的节点数（含分支自己）。

    除的是**支数**而不是"总节点数"：`total - 1` 是先扣掉中心主题，剩下的名额按支均分。
    整除的余数留在根上（`_clip_tree` 不裁根），所以总量不会超 `total`。
    """
    return max(1, (max(1, total) - 1) // max(1, branches))


# ---------------------------------------------------------------- 每节点说明字数随材料伸缩
#
# 上一轮只把**节点数**接上了材料（`MINDMAP_NODE_SCALE`），每段说明的长度仍是常量，
# 于是同一批材料上两条路都还是配额驱动：
#
#     48 节点 × 130 字（现状实测均长）= 6,240 字 · 材料 5,616 字 → κ 0.90（还是膨胀）
#
# `INITIAL_MAX_NODES` 与 `DETAIL_MIN_CHARS/MAX_CHARS` 各占膨胀的一半，只动一个等于
# 只关掉半扇门。这里的算术与 `scaled_initial_nodes` 同源但方向相反：那边问"材料有多少、
# 该摊出几个节点"，这边问"这一次的节点有多少材料撑着、每段说明该写多长"。
#
#     support 支撑   = 材料字数 / 这一次要写的节点数
#     mean    目标均长 = support / κ目标        （κ = 材料字数 / Σdetail，与监控同一个方向）
#     hi      上限   = mean / 0.867            ← 实测：现状 [80,150] 的均长是 130
#     lo      下限   = 0.533 × hi              ← 实测：80/150
#
# 两条比例不是拍的，是**从现状反解出来的**：把 mean=130 代进这两条式子，得到
# [80, 150]——也就是"不打开关时这条公式的产物就是现状"，开关只是把 mean 换成材料给的量。
#
# 下限为什么不能再低：`DETAIL_ONLY_PROMPT` 要求"至少 3 句完整的话"，而实测一句完整
# 中文说明约 25 字（130 字 / 5 句），3 句 = 75 字。所以 80 这个数**本来就是 3 句的
# 四舍五入**，能降的空间只有 5 个字——它不是配额，是质量线。字数伸缩于是有一道与
# 材料无关的地板（`DETAIL_SENTENCE_FLOOR`）：材料撑不住时，该减的是节点数
# （`MINDMAP_NODE_SCALE`），不是把一段完整说明压成一句半。
DETAIL_SCALE_ENV = "MINDMAP_DETAIL_SCALE"

#: 目标压缩比 κ = 材料字数 / Σdetail。1.0 = 说明总量与材料等量（既不膨胀也不压缩）。
#: 取 1.0 而不是 1.2：κ 是本仓库一直在量的那个数（`measure_quad` 的 `compression`），
#: 目标直接写成同一个量的 1.0，A/B 出来的 κ 与它就是同一个数，不需要再换算。
DETAIL_SCALE_KAPPA = 1.0

#: 现状区间 [80,150] 上实测的均长是 130，于是均长/上限 = 0.867、下限/上限 = 0.533。
#: 只有**一个**标定点这件事要记住：130 来自 42 次采样的均值（`docs/ONE-CALL.md`
#: 与 `data/scale-quad-medium.json` 的 113–130）。换模型或换语言要重标。
DETAIL_MEAN_TO_HI = 0.867
DETAIL_LO_TO_HI = 0.533

#: 一句完整中文说明的实测均长。3 句 = 75 字，就是 `DETAIL_MIN_CHARS = 80` 的出处。
DETAIL_SENTENCE_CHARS = 25
DETAIL_MIN_SENTENCES = 3
DETAIL_SENTENCE_FLOOR = DETAIL_SENTENCE_CHARS * DETAIL_MIN_SENTENCES


@dataclass(frozen=True)
class DetailSpan:
    """这一次"每段说明写多长"的区间与它的来处。

    为什么不直接返回 `(min, max)`：这个区间的两个数都是算出来的，而"为什么这一次是
    75–135"必须能从 `data` 里读出来——否则事后看到一棵说明偏短的树，分辨不出是模型
    没听话还是这次就要它短。`support` 与 `kappa` 就是那两个因。
    """

    min_chars: int
    max_chars: int
    #: 这个区间折出来的**目标均长**（不是材料支撑本身）：[75,135] → 117、[80,150] → 130。
    #: 与 `support` 分开两个字段，是因为"材料每节点摊到 832 字"与"这次让模型写 130 字"
    #: 恰恰是这条伸缩最重要的两个读数——大材料上它们差 6 倍，而 κ 5.45 就来自这个差。
    mean_chars: int
    support: float
    kappa: float
    sentences: int = DETAIL_MIN_SENTENCES
    #: 开关是否真的接管了这一次。False 时 `min/max` 就是 `DETAIL_MIN_CHARS/MAX_CHARS`，
    #: 提示词与出账与加这个开关之前逐字节相同。
    scaled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "min": self.min_chars, "max": self.max_chars,
            "mean": self.mean_chars, "support": round(self.support, 1),
            "kappa": self.kappa, "sentences": self.sentences,
        }


def default_detail_span(*, floor_chars: int, ceiling_chars: int) -> DetailSpan:
    """现状区间：`[DETAIL_MIN_CHARS, DETAIL_MAX_CHARS]`，均长按同一个 0.867 折出来。"""
    return DetailSpan(
        min_chars=floor_chars, max_chars=ceiling_chars,
        mean_chars=int(round(ceiling_chars * DETAIL_MEAN_TO_HI)),
        support=0.0, kappa=DETAIL_SCALE_KAPPA)


def detail_scale_enabled() -> bool:
    """`MINDMAP_DETAIL_SCALE=1` 才按材料伸缩；不设 = 关（保持 80–150，逐字节相同）。

    与 `MINDMAP_NODE_SCALE` 同一个理由：改的是每一次生成的规模，翻它之前得先有一组
    数字（`data/detail-scale-ab.json` 是现状那一组），而且"要不要翻"要能单独回滚。
    """
    return os.getenv(DETAIL_SCALE_ENV, "").strip().lower() in ("1", "on", "true", "yes")


def scaled_detail_span(
    *,
    material_chars: int,
    nodes: int,
    floor_chars: int,
    ceiling_chars: int,
    kappa: float = DETAIL_SCALE_KAPPA,
) -> DetailSpan:
    """这次每段说明写多长：材料支撑 ÷ 目标 κ，夹在「3 句的地板」与可读性上限之间。

    `nodes` 是**这一次真的要写说明的节点数**（不是配额）：树被裁到 30 个节点时，
    每段能分到的材料是 30 份而不是配额那份数。

    返回的是**目标区间**而不是硬约束：模型仍可以写短（`_needs_detail` 兜底补写），
    也可以写长（契约上限 2000 兜底）。
    """
    support = max(0.0, material_chars) / max(1, nodes)
    # 地板压在 mean 上而不是压在 lo 上：压 lo 会得到 [75,75] 那种退化的区间
    # （lo == hi 时"写 A–B 字"既没有余地也读不出意图）。
    mean = max(DETAIL_SENTENCE_FLOOR, support / max(kappa, 0.01))
    hi = min(ceiling_chars, int(round(mean / DETAIL_MEAN_TO_HI)))
    lo = max(DETAIL_SENTENCE_FLOOR, min(hi, int(round(DETAIL_LO_TO_HI * hi))))
    return DetailSpan(
        min_chars=lo, max_chars=hi,
        # 报的是**这个区间**折出来的均长，不是 `mean`：上限一旦夹到 150，`mean` 就
        # 变成了"材料想给的量"（大批 832），而模型这一次只会写到 150 附近。
        mean_chars=int(round(hi * DETAIL_MEAN_TO_HI)),
        support=support, kappa=kappa, scaled=True)


def detail_span(
    *, material_chars: int, nodes: int, floor_chars: int, ceiling_chars: int,
) -> DetailSpan:
    """本次的说明区间。开关关着、或没有材料/没有节点时，返回**现状那个区间**。

    "没有材料"不是"材料是 0 字所以每段写 0 字"：没有材料时模型写的是自己的知识，
    长度要求与加这个开关之前一样，因此退回常量。这条分支也是默认路径唯一的入口——
    它连着 `default_detail_span`，而那个函数返回的就是 `DETAIL_MIN_CHARS/MAX_CHARS`。
    """
    if not detail_scale_enabled() or material_chars <= 0 or nodes <= 0:
        return default_detail_span(floor_chars=floor_chars, ceiling_chars=ceiling_chars)
    return scaled_detail_span(
        material_chars=material_chars, nodes=nodes,
        floor_chars=floor_chars, ceiling_chars=ceiling_chars)


def _tokens(chars: int) -> int:
    """按汉字密度折算 token 数（输出侧几乎全是中文）。"""
    return math.ceil(max(0, chars) * _CJK_TOKENS_PER_CHAR)


def per_node_tokens(
    *,
    label_chars: int = GENERATED_LABEL_MAX_CHARS,
    detail_chars: int = PROMPT_DETAIL_MAX_CHARS,
    refs: int = REFS_PER_NODE,
) -> int:
    """一个"满额"节点在输出里占多少 token。

    默认参数取的是**提示词要求的上限**：16 字标题 + 200 字详情 + 2 条引用。
    用它算出来的批容量偏保守，代价是每次调用少要几个节点（多几次调用），收益是
    不会被截断。反过来按平均值算，遇到"模型这一批恰好都写满了"就必炸。

    要算"契约满了"的极端情况，显式传 `detail_chars=MAX_NODE_DETAIL_CHARS`——
    那是满额守卫测试的用法，不是常规路径。
    """
    return (
        NODE_STRUCTURE_TOKENS
        + _tokens(label_chars)
        + _tokens(detail_chars)
        + max(0, refs) * (REF_STRUCTURE_TOKENS + REF_QUOTE_TOKENS)
    )


def batch_output_tokens(nodes: int, **per_node: int) -> int:
    """一次调用要求 `nodes` 个满额节点时，需要多少输出 token（含余量）。

    余量按比例加在**节点部分**上，不加在信封上：推理开销随内容长度增长，不会
    因为顶层多一个大括号就变多。
    """
    if nodes <= 0:
        return ENVELOPE_TOKENS
    content = nodes * per_node_tokens(**per_node)
    return ENVELOPE_TOKENS + content + math.ceil(content * REASONING_MARGIN_RATIO)


def available_output_tokens(
    budget_max_output_tokens: int,
    *,
    model_cap: int | None = None,
    context_left: int | None = None,
) -> int:
    """三个上限里最小的那个：本次调用真正能用于输出的 token 数。

    `model_cap` / `context_left` 为 `None` 表示"这条边界这次不设限"（还不知道
    服务端的真实上限、或调用方没有上下文占用信息），而不是"上限是 0"。
    """
    limits = [max(0, budget_max_output_tokens)]
    if model_cap is not None:
        limits.append(max(0, model_cap))
    if context_left is not None:
        limits.append(max(0, context_left))
    return min(limits)


def max_nodes_for_tokens(available_tokens: int, **per_node: int) -> int:
    """反解：`available_tokens` 装得下几个满额节点（向下取整，可为 0）。"""
    node_cost = per_node_tokens(**per_node)
    if node_cost <= 0:
        return 0
    # 余量与信封一起扣掉，剩下的才是节点部分能用的量。
    usable = available_tokens - ENVELOPE_TOKENS
    if usable <= 0:
        return 0
    nodes = usable // math.ceil(node_cost * (1 + REASONING_MARGIN_RATIO))
    return max(0, int(nodes))


def nodes_per_call(
    budget_max_output_tokens: int,
    *,
    model_cap: int | None = None,
    context_left: int | None = None,
    **per_node: int,
) -> int:
    """这一次调用最多能要几个满额节点。0 = 装不下任何一个，别发。

    这是调用方唯一该用的入口：它把"三个上限取最小"和"反解节点数"两件事合成
    一次判断，调用方不必自己算，也就不会漏掉其中一条边界。
    """
    available = available_output_tokens(
        budget_max_output_tokens, model_cap=model_cap, context_left=context_left)
    return max_nodes_for_tokens(available, **per_node)


def plan_batches(total_nodes: int, per_call: int) -> list[int]:
    """把 `total_nodes` 个节点按每批 `per_call` 拆开，返回每批的节点数。

    `per_call <= 0` 时返回空列表（一个都装不下 = 一个都别要）。调用方看到空
    列表应当停手，而不是退化成"每批 1 个"——那样会发出几十次注定被截断的请求。
    """
    if total_nodes <= 0 or per_call <= 0:
        return []
    batches = []
    left = total_nodes
    while left > 0:
        take = min(per_call, left)
        batches.append(take)
        left -= take
    return batches


def level_sizes(branches: int, fanout: int, depth: int, *, root: int = 1) -> list[int]:
    """一棵"满额"分片树的逐层节点数：`[1, b, b*f, b*f^2, …]`。

    `depth` 是**含根**的层数（4 层 = root + 一级 + 二级 + 三级），与
    `GenerateDeepArgs.max_depth`（root 之下算起）差一，换算只在这里做：调用方
    传 `max_depth + 1`。这个差一是本次最容易被写错的地方——`max_depth=3` 是
    4 层树，不是 3 层。
    """
    if depth <= 0:
        return []
    sizes = [root]
    count = root
    for level in range(1, depth):
        # 第 1 层（一级分支）由骨架决定，往下的每一层乘扇出。写成两个显式分支
        # 而不是一个三元表达式：`sizes[-1] == root` 在 `branches == root` 时会
        # 误判，而那种误判只会让调用数少算一层——正是这次要防的那类错误。
        count = branches if level == 1 else count * max(1, fanout)
        sizes.append(count)
    return sizes


def calls_for_levels(sizes: Sequence[int]) -> int:
    """这棵"满额树"要发多少次内容调用。

    **不是节点数**。一次调用 = 一个父节点（外加骨架那一次）：骨架产出第 1 层，
    然后展开第 1 层的每个节点产出第 2 层、展开第 2 层的每个节点产出第 3 层……
    所以最后那一层**不需要**再被展开一次，它已经由上一层"写出来"了。

        4 层满额（1 + 6 + 18 + 54 个节点）：
            骨架 1 + 展开 6 个分支 + 展开 18 个二级 = 25 次调用

    这个"节点数 ≠ 调用数"的差是本次最容易算错的地方——把 54 也算进去会得出 79，
    进而误判"80 的预算装不下 4 层树"，把初次白白降到 3 层。实测 `branches=6,
    points_per_branch=3` 一次完整初次生成用掉 `RunContext.model_calls_used == 25`。
    """
    if not sizes:
        return 0
    return 1 + sum(max(0, count) for count in sizes[1:-1])


__all__ = [
    "BRANCH_MAX_NODES",
    "BRANCH_MIN_NODES",
    "CHAPTERS_PER_NODE",
    "CHARS_PER_NODE",
    "DETAIL_LO_TO_HI",
    "DETAIL_MEAN_TO_HI",
    "DETAIL_MIN_SENTENCES",
    "DETAIL_SCALE_ENV",
    "DETAIL_SCALE_KAPPA",
    "DETAIL_SENTENCE_CHARS",
    "DETAIL_SENTENCE_FLOOR",
    "DetailSpan",
    "ENVELOPE_TOKENS",
    "MIN_INITIAL_NODES",
    "MIN_NODES_PER_CALL",
    "NODE_CAP_ENV",
    "NODE_POOL_ENV",
    "NODE_SCALE_ENV",
    "NODE_STRUCTURE_TOKENS",
    "PROMPT_DETAIL_MAX_CHARS",
    "REASONING_MARGIN_RATIO",
    "REFS_PER_NODE",
    "SOURCES_PER_NODE",
    "available_output_tokens",
    "batch_output_tokens",
    "branch_budget",
    "branch_share",
    "calls_for_levels",
    "default_detail_span",
    "detail_scale_enabled",
    "detail_span",
    "initial_nodes",
    "level_sizes",
    "max_nodes_for_tokens",
    "node_cap",
    "node_pool_mode",
    "node_scale_enabled",
    "nodes_per_branch",
    "nodes_per_call",
    "per_node_tokens",
    "plan_batches",
    "scaled_detail_span",
    "scaled_initial_nodes",
]
