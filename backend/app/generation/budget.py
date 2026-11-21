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
#: 8 / 10 / 13 时，六次采样**全部只长出 1 条一级分支**（`{'1':1,'2':1,'3':5,'4':3}`
#: 这种一条链的形状）；同一批材料在上限 32 时给 3 支、上限 80 时给 5–6 支。
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
    "CHAPTERS_PER_NODE",
    "CHARS_PER_NODE",
    "ENVELOPE_TOKENS",
    "MIN_INITIAL_NODES",
    "MIN_NODES_PER_CALL",
    "NODE_SCALE_ENV",
    "NODE_STRUCTURE_TOKENS",
    "PROMPT_DETAIL_MAX_CHARS",
    "REASONING_MARGIN_RATIO",
    "REFS_PER_NODE",
    "SOURCES_PER_NODE",
    "available_output_tokens",
    "batch_output_tokens",
    "calls_for_levels",
    "level_sizes",
    "max_nodes_for_tokens",
    "node_scale_enabled",
    "nodes_per_call",
    "per_node_tokens",
    "plan_batches",
    "scaled_initial_nodes",
]
