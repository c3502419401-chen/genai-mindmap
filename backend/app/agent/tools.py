"""工具注册表与四个业务工具。

注册表只负责分派与参数校验，业务逻辑在各个 `_xxx` 函数里。

四个工具的划分照「用户意图 + 作用域」而不是 CRUD 动词：
- `mindmap_generate` 只在没有导图时可用：**骨架一次 → 每支检索一次 → 结构一次
  写完 → 只写说明一次**（理由与该改动的三条实测依据见该函数的 docstring）。它是
  `create` 唯一注册的生成路
- `mindmap_generate_deep` 是分片生成（骨架 → 并发展开 → 验收 → 局部重试）。
  **代码与规格都在，但不注册到 `create`**：它服务的是"一次响应写不下 200+ 节点"
  的大图，70 余个既有用例钉着它的行为，需要时把名字加回 `_ALLOWED_BY_INTENT`
  即可（定位见 `_generate_deep_spec` 的 docstring）
- `node_expand` 只能在已选中的节点下加子节点，不能删、不能移
- `rag_query` 只读，查本次请求带来的来源**与知识库**（`library=True`，默认开；
  库命中的 `source_id` 是持久 id，收尾校验前要用 `library.load()` 取回原文）
- `mindmap_edit` 才允许改/删/移，且必须按节点 id
- `audit_tree` 只读：数出"还有哪几支一级分支没展开"，**不做任何判断**。它存在
  的理由是"模型以为自己写完了"——`expand` 意图下没有代码替它数，而它看到的大纲
  只列前 200 个节点（口径见该工具的 docstring）

`create` 只剩一条注册的生成路（整树一次生成）：它保留分片那条路的检索质量
（骨架定好之后每支各检索一次、证据按支分组一次给），又不付 1+N 次调用与跨支
重复的代价（决策与实测见 `docs/ONE-PATH.md`）。

这样切是为了把「改错地方」的风险压到协议层：`node_expand` 的入参里没有
`parent_id` 的自由度（只能是你当前选中的那个），模型想乱挂也挂不了。
`build_registry(intent)` 再按意图只放出该用的那几个——看不到的工具，
模型不会反复试着调。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import (
    Any, Callable, Collection, Iterator, Literal, Mapping, Protocol, Sequence, TypeVar,
)
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.agent.contracts import RunContext, ToolCall, ToolResult, ToolSpec
from app.agent.diagram_intent import SOURCE_MODEL, SOURCE_REQUEST
from app.agent.ops import (
    INNER_LAYER,
    STAGE_LAYER,
    TEMP_ID_PREFIX,
    apply_flow_edge_ops,
    apply_ops,
    find_node,
    new_node_id,
    normalize_ops,
    resolve_flow_edge_placement,
    validate_flow,
)
from app.agent.stream_items import StreamItem, closed_items
from app.agent import shapes  # 三种图型的结构适配（遍历 / 定位 / 出口转换）
from app.contracts import mindmap as mindmap_contract
from app.contracts.diagnostic import Diagnostic, DiagnosticLevel
from app.contracts.mindmap import (
    EDGE_KINDS,
    FISHBONE_CATEGORIES,
    GENERATED_LABEL_MAX_CHARS,
    MAX_EDGE_LABEL_CHARS,
    MAX_EDGES,
    MAX_FISHBONE_BONES,
    MAX_FISHBONE_PROBLEM_CHARS,
    MAX_NODE_DETAIL_CHARS,
    MAX_NODE_ICON_CHARS,
    MAX_TREE_NODES,
    NODE_COLORS,
    RELATION_KIND_VALUES,
    DiagramContent,
    Edge,
    FishboneContent,
    FlowContent,
    FlowStage,
    MindMapContent,
    Node,
    Relation,
    clip_label,
    normalize_color,
    normalize_icon,
)
from app.contracts.ops import OpBatch, OpError
from app.contracts.source import (
    Source,
    SourceKind,
    SourceMetadata,
    SourceUsage,
)
from app.generation import budget
from app.generation.service import (
    CONTENT_FREEDOM_CONTRACT,
    CONTEXT_TOKEN_LIMIT,
    DETAIL_NO_SOURCE_RULES,
    EXTERNAL_TRUST_RULES,
    MIN_BRANCHES,
    GenerationError,
    apply_free_tier_ceiling,
    chat_async,
    detail_prompt,
    estimate_input_tokens,
    max_tokens_cap,
    thinking_block,
    validate_generated_content,
)
from app.generation.stream_partial import collect_new_nodes, parse_partial
from app.knowledge import external as external_knowledge
from app.knowledge import graph_extract
from app.knowledge import graph_ingest
from app.knowledge import graph_store
from app.knowledge import library as knowledge_library
from app.knowledge.query_plan import _material_snippets, plan_branches, plan_query
from app.mindmaps.edges import UNRESOLVED_ENDPOINT, clean_edges
from app.knowledge.service import (
    EXTERNAL_SOURCE_KINDS,
    LEAF_MAX_CHARS,
    OUTLINE_MAX_ITEMS,
    OUTLINE_MIN_TITLE_CHARS,
    EvidenceHit,
    KeepPolicy,
    SourceFilter,
    filter_sources,
    heading_outline,
    heading_outline_by_source,
    outline_coverage,
    search_exact_blocks,
    search_multi,
    search_parent,
)
from app.optimization import nli
from app.optimization import verify as verify_step
# 跨支逐字同名的判据**只有一份**，在 `optimization/cross_branch.py`。这里保留
# `_cross_branch_duplicates` 这个名字是因为十几个测量脚本与用例按
# `from app.agent.tools import _cross_branch_duplicates` 取它——但取到的是**同一个
# 函数对象**，不是第二份实现（`test_cross_branch_judgement_has_exactly_one_
# implementation` 用 `is` 钉住）。它原先只活在生成链路里，于是「检查与建议」
# （`/optimization/suggest` -> `check()`）看不见这一档，那是这条账要修的东西。
from app.optimization.cross_branch import cross_branch_duplicates as _cross_branch_duplicates
from app.optimization.repair import AUTO_REPAIRABLE_CODES
from app.optimization.repair import READABLE_LABEL_CHARS, overlong_labels
from app.optimization.repair import repair as repair_structure
from app.optimization.service import check
from app.prefs import profile as profile_rank
from app.prefs import service as prefs
from app.optimization.source_refs import (
    MAX_NLI_CHECKS,
    EvidenceSpan,
    annotate_nli,
    verify_source_refs,
)

logger = logging.getLogger(__name__)

# 与 backend/app/contracts/mindmap.py 的 Node.children max_length 同口径
MAX_CHILDREN = 50

# 逐节点、数量随树规模线性膨胀的引用提示，**不进** `diagnostics`。
#
# 分片出来的树动辄几百节点，「这个节点没有可核验的引用」几乎对每个叶子都成立，
# 几百条同质提示会把真正有用的诊断（`SPARSE_BRANCH` / `BRANCH_FAILED`）淹掉，也不
# 给用户任何可操作的信息。所以它们只在 `data.source_refs` 里按 code 计数、在
# `content` 里聚合成一句话。
#
# 收尾的剔除（`verify_source_refs` 的回报）与结构体检（`check()` 的第三层）共用
# 这一份清单：同一个判定在两处各自维护就会分叉，而分叉的表现是"体检报了一堆、
# 聚合那句一个字没提"。
_PER_NODE_REF_CODES = ("UNSOURCED_NODE", "SOURCE_PAGE_UNVERIFIED")

# 分片生成的并发上限。原值 3 的依据是「一次打 8 个容易被限流」，但那是推测。
# 实测（2026-11-07，纯文本小请求）：并发 3/5/8/10 全部成功，总耗时从 0.99s
# 到 1.24s 几乎不变，没有一次 429。分支上限是 8，所以 10 等于取消限制，
# 但留了一个安全阀——将来谁调高 branches 上限，这里还能兜住。
# 8 条分支的收益：3 路要 3 批（9-15 秒），10 路 1 批（3-5 秒）。
# 前端图片/音频解析还有一路并发 2，最坏叠加 2+8=10，仍在实测范围内。
DEEP_CONCURRENCY = 10

# 递归深度：不再由工具自己拍一个数，直接取契约的 `MAX_TREE_DEPTH`。
#
# `expandable` 的语义是「模型自己说这一支还能不能再拆」，代码只在它说"还能拆"
# 时继续问下去。既然如此，深度就不该是工具的一个独立主张——"能生成多深的树"
# 只该有一处定义，而那一处在契约里。
#
# 曾经这里是 `DEFAULT = 3 / MAX = 4`，理由是「超过四层用户在画布上要缩到看不清
# 字才看得见结构」。可读性是真问题，但那是**渲染层**的事（折叠、聚焦、缩放），
# 生成层替用户把第四层以下砍掉，他连"这里有更细的内容"都无从知道，也没得选。
# 真嫌深就传一个更小的 `max_depth`，或者由界面在折叠做好之后来控制。
#
# 真正的边界不在层数，而在节点总数（契约 1000）与模型调用预算，这两道各有各的
# 守卫：`_trim_slots` 按节点额度裁剪，`reserve_model_call` 在额度不足时让工具
# 停止展开并记诊断。
#
# 实测（"考研数学复习计划"，六支，预算 80）：295 个节点、64 秒，树深 5 层
# （含中心主题）——也就是 root 之下 4 层，**恰好等于旧上限的值**。模型对这个主题
# 本来就要这么多，旧的 4 没有切掉任何内容。
#
# 所以这次改动的理由不是"放开会让树更深"，而是：
#   1. 同一个边界在工具和契约里各写了一个数，静默取小值，谁也不报错；
#   2. 换个要求更细的主题时，4 会先于模型和预算拦住它，而用户无从知道。
# 真正决定规模的是模型自己的 expandable 判断与调用预算，层数只该是兜底。
DEEP_DEFAULT_DEPTH = mindmap_contract.MAX_TREE_DEPTH
DEEP_MAX_DEPTH = mindmap_contract.MAX_TREE_DEPTH

# 二级及以下每个父节点最多几个子节点。**一级分支的条数由内容决定**（无上限，见
# `NO_BRANCH_CAP_RULE`；`GenerateDeepArgs.branches` 是无效字段），只由 `DEEP_MAX_BRANCHES`
# 兜住画布承载。往下的每一层必须收窄：20 × 5 × 5 × 5 连 1000 节点的契约都装不下，
# 而且每层 5 个正是"一屏能扫完"的量。
DEEP_FANOUT = 5

# 一级分支的硬上限。参数模型用 `le=DEEP_MAX_BRANCHES` 引用它，骨架那里也用它截断，
# 两处同源——否则模型无视"最多 N 条"时，一级节点会一直长到契约的单节点上限（50）。
DEEP_MAX_BRANCHES = 20

# ---------------------------------------------------------------- 两级骨架
#
# 一级分支多到二十条时，图宽得没法看：当年把 `branches` 的上限从 8 提到 20 只是把
# "塞进一支"换成了"图很宽"，问题没解决。所以骨架阶段先归成几组：**一级是组，二级才是
# 那二十节**。
#
# `SKELETON_GROUP_THRESHOLD`（N）：**超过它才分两级**。它必须是一个"再看就数不清"的位置，
# 不是"比某个默认值大就分"——在 6 就分组，普通主题会被硬拆出 2-3 个分组，而那种主题
# 本来就没有可归纳的上一层。判据读的是**材料的宽度**（目录行数 / 章数 / 图谱补条），
# 不读任何模型在读到材料之前填的数（`branches` 已不参与，见 §5.1②）。
#   10 的依据：调色板是 8 色（`NODE_COLORS`），一级色靠"一色一组"分组，8 色刚好一屏；
#   而画布横向能扫完的一行也在 10 条上下。<=10 条时扁平已经能读，分组只是多一层点击。
SKELETON_GROUP_THRESHOLD = 10
# 每组的目标支数：20 节 / 4 = 5 组（正好落在 `SKELETON_GROUP_MIN..MAX` 的中间）。
# 4 的依据是"组内还能一眼看完"：一组的子节点 3–5 个正好一屏，再多就该在组内再分类了。
SKELETON_GROUP_TARGET = 4
# 组数的可读区间：少于 3 组时"分组"等于把一级分支从 20 条砍到 2 条，那是另一种难看；
# 多于 6 组时一层横向又回到 6 条以上，分级的收益开始消失（`DETAIL_PROMPT` 对一级分支
# 的既有要求也是 3–6 条）。
SKELETON_GROUP_MIN = 3
SKELETON_GROUP_MAX = 6
# 组数的硬上限（模型没照 3–6 写时的安全阀）：与 `DEEP_MAX_BRANCHES` 同一性质——
# 只兜住病态输出，不参与正常分配。超过它的组整组丢掉（连同它的分支），
# 与既有"分支尾巴截到 20 条"是同一种处理。
SKELETON_GROUP_HARD_MAX = 8

# 一次展开至少要能挂下 2 个子节点才值得发一次调用。额度只剩 1 个节点时直接按
# "被节点预算截断"处理——花一次调用只为了挂一个节点，是把额度换成噪音。
DEEP_MIN_BREADTH = 2

# 分片 + 按支检索的每支条数。3 是 `docs/RAG-DECISIONS.md` 第 5 节定的量：一条
# 父块 450–700 字，三条已经够一次展开的上下文，再多只是把同一段材料换个开头。
#
# **成本口径实测**（`scripts/measure_deep_slicing.py`，1.2 万字材料、68 个子块）：
# 真正花 embedding 的是"本次候选块的向量 + 每条 query"，**不是命中的那 3 条**
# （命中的文本是检索的输出，不会反过来再 embed 一次）。
#   - 块向量按文本哈希缓存（`knowledge.vector_cache`），同一份材料在 N 支之间只付一次；
#   - 第一支跑完就预热了缓存，后面各支只 embed 自己那一条 query。
# 实测（假向量、冷缓存）：8 支 = 8 次调用 / 10 批 / 76 条文本；20 支 = 20 次 / 22 批 /
# 88 条文本。**不预热**时是 20 × 69 = 1380 条（56 批），17 倍——所以
# `_generate_deep` 里第一支仍然串行检索（预热闸见 `run_branch`），那不是保守，
# 是成本闸。`_retrieve_branches` 是同一策略的批量实现。
#
# **条数不再是 3，而是"分数差不多的都留着"**（`keep=DEEP_BRANCH_KEEP`）：固定取前
# 3 条在实测里会砍掉大量同分证据——`scripts/measure_topk_threshold.py`（五组真实
# 材料 × 真实小节标题，`top_k=60` 拿全量）记了三个数：第 3→4 条的跌幅中位只有
# 0.026（hybrid）/ 0.047（纯 BM25）；被砍掉的第 4–10 条里 **131/140（94%）落在
# 不同的父块**上，也就是"多给几条"是真的多给信息，不是同一段换个开头。
# 于是 3 变成**下界**，上界是 `DEEP_BRANCH_KEEP.max_k = 6`。
#
# `max_k=6` 而不是 10 是成本闸，不是口味：20 支 × 6 = 120 条父块 ≈ 6 万字，已是
# 按支检索那一次 prompt 的极限（`docs/RETRIEVAL-KEEP.md` 有真生成的 token 实测）。
# **`DEEP_BRANCH_TOP_K` 这个名字留着**：它仍是"一支至少几块"的口径，六个测试文件
# 与 `measu*` 脚本都拿它当锚，改个名字换不来任何信息。
DEEP_BRANCH_KEEP = KeepPolicy(min_k=3, max_k=6, rel=0.85)
DEEP_BRANCH_TOP_K = DEEP_BRANCH_KEEP.min_k

# 按分数取数的总开关（`MINDMAP_KEEP_BY_SCORE`，**默认关**）。
#
# ## 为什么默认是 0（不是"懒得开"，是量完之后翻回来的）
#
# 这个改动的初衷成立：固定 `top_k=3` 会砍掉同分的证据（第 3→4 条跌幅中位 0.026、
# 被砍掉的 94% 落在不同父块），代码与 `tests/test_keep_by_score.py` 都留着——那是
# 证据，删了就没人知道量过什么。但**它没有拿到"有收益"的证据**，而当时是默认开的：
#
#   1. **首轮那个 +5.9pp 是低端样本造成的假象**。`repeat=3` 之后厚批 on 91.0%、
#      off 92.1%（方向还偏向 off）——两臂分布完全重叠，**off 不动任何代码也能到
#      98.9%**。首轮 off 那次 74.8% 落在它自己的低端。
#   2. **样本间波动是 20–30pp 量级**：厚批两臂各自极差 20.6pp / 19.3pp；薄批（673 字
#      全塞、**不经过检索**）的 3.7pp 臂间差配 33.6pp 极差，是独立的量级参照。
#      小于它的臂间差读不出来。
#   3. **有一条负向信号没量测**：厚批 on 臂 6 次尝试 2 次失败（模型返回非 JSON，
#      33%），off 臂 3 次 0 失败。方向与"keep 让 prompt 变长（材料字数 1.42–1.68x）"
#      一致。那是**交不出树**，代价比覆盖率低 1.1pp 高一个量级。
#
# 一个默认开的功能让用户承担"收益未证实 + 可能交不出树"的组合，不成立。所以默认翻成 0：
# 代码、测试、量测脚本都保留，想开的人显式写 `MINDMAP_KEEP_BY_SCORE=1`。
# 下一个想翻回来的人，先把上面三条推翻——判它需要每臂 5–10 轮 + **与树规模无关**的
# 指标（`node_count` 臂内极差就有 16–64，覆盖率的分母自己会漂）。
#
# 三条口径：
#
#   - `0`（缺省）→ 四个调用点都传 `keep=None`，**逐字节回到"取排序后前 N 条"**；
#   - `1` → 按 `KeepPolicy` 取，条数落在 `[min_k, max_k]`；
#   - 真生成的 A/B（`scripts/measure_generation_strategy.py --keep on|off`）就是靠它跑。
KEEP_BY_SCORE_ENV = "MINDMAP_KEEP_BY_SCORE"


def keep_by_score_enabled() -> bool:
    """按分数取数是否生效。**默认关**；显式 `1/on/true/yes` 才开。

    正列举真值而不是"不在假值集合里"：默认关时两种写法等价，而正列举让"改默认值"这件
    事只需要改一个字面——与实际行为一致，也免得空串被当成开。
    """
    return os.getenv(KEEP_BY_SCORE_ENV, "0").strip().lower() in {
        "1", "on", "true", "yes"}


def branch_keep() -> KeepPolicy | None:
    """按支检索用的 policy；总开关关着时返回 `None`（= 旧口径）。"""
    return DEEP_BRANCH_KEEP if keep_by_score_enabled() else None


def detail_keep() -> KeepPolicy | None:
    """章内检索用的 policy；总开关关着时返回 `None`。"""
    return DETAIL_RETRIEVAL_KEEP if keep_by_score_enabled() else None


def rag_keep() -> KeepPolicy | None:
    """`rag_query` 用的 policy；总开关关着时返回 `None`。"""
    return RAG_QUERY_KEEP if keep_by_score_enabled() else None

# 一支最多发几路查询：标题 + 2 条同义问法（`query_plan.plan_branches`）。
#
# 为什么是 3 而不是 `plan_branches` 允许的 4（标题 + 3）：那 3 条问法里前两条
# 才进检索，第三条是提示词留给模型的选择余地（它给 3 条，代码取前 2 条）。
# 每多一路就多一次 embedding 往返，而 8 支 × 3 路 = 24 条 query 已经在 5 秒
# 预算的边上。`service.MAX_ROUTES=6` 是**单次检索**的口径，3 路远不到顶：
# 那个上限防的是"一个 query 列表里塞六条以上"，不是"多少支各发几路"。
BRANCH_QUERY_LIMIT = 3

# 分片检索**全部**失败时退回"整树一次检索"的条数。与 `rag_query` 的默认
# `top_k=5` 同口径：与 `rag_query` 的默认值相同，退回时用户看到的证据量与改造前
# （整树一次检索那条老路）一致，不是"缩水版的降级"。
DEEP_FALLBACK_TOP_K = 5

# ---- 限额补查（`_generate_deep` 的 `expand_branch`）---------------------------
#
# 按支检索的 query 是**分支标题本身**，实测比"主题 + 标题"好（8 支挤同一父块的
# 从 8/8 降到 3/8，覆盖率 19.2% → 28.8%），但标题与正文用词对不齐的那一支仍然
# 一条都召不回。这一层补的就是那几支，**只补一次、只补三支**：
#
#   - 每支最多 1 次（`SUPPLEMENTARY_BRANCH_LIMIT`）：补查的 query 是从这一支自己
#     刚写出来的子节点里取的，补了还没有就是检索栈的极限，再补只是换同义词；
#   - 每次运行最多 3 次（`SUPPLEMENTARY_RUN_LIMIT`）：20 支的图上放开会让 embedding
#     请求数翻倍，而收益集中在少数几支（实测 8 支里通常 2–3 支需要补）。
#
# `SUPPLEMENTARY_MIN_NODES` 是自动触发条件的门槛：一支写得够多（≥3 个要点）却一条
# 引用都没有，说明它整支都是凭知识补的——这正是"看起来完整、其实没有出处"的形态，
# 值得花一次检索换依据。少于 3 个要点的支不补：它本来就没写多少东西，补了也填不满。
SUPPLEMENTARY_BRANCH_LIMIT = 1
SUPPLEMENTARY_RUN_LIMIT = 3
SUPPLEMENTARY_MIN_NODES = 3

# 工具内单次模型调用的输出上限。这个数必须与 `app.agent.provider._MAX_TOKENS_CAP`
# 一致：provider 是外层轮次的出口，工具里的 `chat_async` 是另一条出口，两边不同
# 时 `RunBudget.max_output_tokens` 只约束了其中一半的调用。不去 import 那个私有
# 常量，是为了不让「工具」反向依赖「适配层」——适配层是可替换的。
#
# 它是**默认值**，不是生效值：生效值由 `generation.service.max_tokens_cap()` 给出
# （`MINDMAP_LLM_MAX_TOKENS` 配置值 + 服务端 400 里学到的上限，两者取小）。
# 于是"qwen 系只认 32768"这件事不需要改代码，只需要配一个环境变量。
MAX_OUTPUT_TOKENS_CAP = 65536

# 初次生成的规模上限：**80 个节点、4 层（含根）**。
#
# 这不是承载能力（契约给的是 1000/8），而是"第一次点击生成之后，用户多久能看到
# 一张完整的图"的承诺。实测（rag 形态、12106 字材料）：按契约上限一路展开会长到
# 240 个节点、7 层，`max_model_calls=80` 在第 6/7 层被烧光——最终交付的是一棵
# **残缺的大树**：上面四层很丰满，最下面两层整片留空。
#
# 换一种分配就完全不同：同样一笔调用如果只要求一棵 4 层树（1 + 6 + 18 + 54 =
# 79 个节点），每一次调用恰好对应一个父节点，树是**完整**的。用户要的是"一张能看
# 完的图"，不是"一张看不完的图的前 60%"。
#
# 真实调用数 **25**，不是 79：骨架 1 次产出第 1 层，之后每次调用展开一个父节点
# （展开 6 个分支 → 第 2 层的 18 个；展开 18 个二级 → 第 3 层的 54 个）。**最后
# 那一层不需要被展开**——它已经被上一层写出来了。加外层两轮与每支最多一次补点，
# 一次完整初次生成 = 33 次，`RunBudget` 的默认额度因此定在 100（算式见
# `_initial_call_plan` 的注释）。
#
# 扩展（`node_expand`）不受这条限制：初次紧、后续松——用户明确点了某个节点要更深，
# 那时上限回到契约的 1000 节点 / 8 层。
#
# 数值定义在 `generation.budget` 里：提示词、工具分配、契约守卫三处共用一个数。
INITIAL_MAX_NODES = budget.INITIAL_MAX_NODES

# 初次生成的深度上限，**按 root 之下算**（与 `GenerateDeepArgs.max_depth` 同口径）。
# 3 = 4 层含根。差一层是这里最容易犯的错：写成 4 就变成 5 层树，节点数上界从
# 79 变成 1+6+18+54+162 = 241，第一次生成必然撑爆调用预算。
INITIAL_MAX_DEPTH = budget.INITIAL_MAX_DEPTH

# ---------------------------------------------------------------- 知识库开关
#
# 知识库（`knowledge/library.py`）让"材料"从**一次请求的临时对象**变成**跨请求
# 可检索的资产**。检索侧能力早就有了（`search_parent(..., library=True)`），缺的
# 是生产调用方：谁把 `library=True` 传下去、谁把库命中的持久 id 并进引用校验、
# 谁告诉模型"库里还有东西"。这三件事必须同时成立，缺任何一件这条链接上去都是负
# 资产（最隐蔽的一种是"检索成功、引用写出、收尾时被静默剔除"）。
#
# 默认**开**。关掉的口子留给两类场景：一是排查"某次召回是不是库带进来的"，二是
# 库表损坏但还想继续用请求内材料。关掉之后整条链的行为与加这个开关之前**逐字节
# 相同**（`tests/test_library_wiring.py` 拿 `07a2740` 的旧模块钉着）——不是"几乎
# 相同"：那时连一次库查询都不会发生。
LIBRARY_SEARCH_ENV = "MINDMAP_LIBRARY_SEARCH"
LIBRARY_SEARCH_TRUTHY = frozenset({"1", "on", "true", "yes"})
# 数库里有几份材料时的上限。`list_sources` 的计数是 `len()`，给它一个比
# `prune(keep_count=200)` 的默认值更宽的数没有意义——超过 200 份时调用方只该
# 说"200 份以上"，而 200 正是淘汰策略的稳态规模。
LIBRARY_COUNT_LIMIT = 200


def library_search_enabled() -> bool:
    """知识库检索是否打开。默认开，显式写 `0/off/false/no/空串` 才算关。

    判定方式与 `MINDMAP_PARENT_RETRIEVAL` 一致（正列举真值 + 缺省取 `"1"`），
    理由也一样：谁改默认值，判定逻辑不用跟着改，改错的表现只剩"默认值写错了"。
    """
    return os.getenv(LIBRARY_SEARCH_ENV, "1").strip().lower() in LIBRARY_SEARCH_TRUTHY


def library_source_count(*, drop_external: bool = False) -> int:
    """库里现有多少份材料（**不含正文**，最多数到 `LIBRARY_COUNT_LIMIT`）。

    只用于两件"给模型看"的事：材料清单末尾那行"知识库中另有 N 份"，以及
    `build_registry` 要不要注册 `rag_query`。它不参与任何检索决策——检索侧自己
    会按 `last_used_at` 取一批再粗筛（`service._library_sources`）。

    库不可用（文件被锁、磁盘满、表被删）时返回 0 并记 warning，**不抛**：知识库
    是加分项，它坏了不该让"只带材料"的那条路跟着失败。这里捕获 `Exception` 而
    不是 `sqlite3.Error`，因为这条路上任何异常的正确处置都只有一种——当成空库。

    `drop_external` 把外部检索抓回来的那批一起排除掉。**它与 `external_knowledge_enabled()`
    是两件事**：那个管"这一次要不要发外网请求"，这个管"算不算数"。关掉开关之后，
    库里那些昨天入库的维基正文仍然躺在那儿，不排除的话
    「知识库里现有的 N 份材料都没有匹配」这句话会把它们算进分母——而它们恰恰是
    用户刚刚说不要的东西。
    """
    try:
        rows = knowledge_library.list_sources(limit=LIBRARY_COUNT_LIMIT)
        if drop_external:
            rows = [row for row in rows if row["kind"] not in EXTERNAL_SOURCE_KINDS]
        return len(rows)
    except Exception as exc:  # noqa: BLE001 — 库坏了就退化成空库，理由见 docstring
        logger.warning("读不到知识库（%s），本次按空库处理：%s",
                       type(exc).__name__, str(exc)[:160])
        return 0


def library_available() -> bool:
    """现在有没有可检索的库材料：开关开着 **且** 库非空。

    两个条件合成一个函数，是因为三处调用点（`_rag`、`build_registry`、
    `_intent_rule`）问的是同一个问题。分头写 `enabled() and count()` 的结果是
    三处各漏一次，而漏一次的表现是"模型看到了工具但查不到东西"或者反过来。

    **`drop_external` 必须传**：用户关掉联网检索之后，库里往往只剩下外部检索
    入库的那批（整个库就是它们堆起来的），剔掉之后库是空的。不传的话
    `rag_query` 仍然会被注册，模型照着调、拿到 `no_sources`、再试一次、撞上
    "累计失败 2 次"的闸——整轮运行直接停掉，用户看到的是"生成失败"，而真相是
    "这里没有任何可检索的东西，那个工具本就不该出现在清单里"。
    """
    return (library_search_enabled()
            and library_source_count(drop_external=library_drop_external()) > 0)


# ---------------------------------------------------------------- 库参与生成
#
# 知识库此前只有**一条**入口：模型自己想起来调 `rag_query`。`_generate` 的三步
# （骨架 / 结构 / 说明）一次都不碰库，于是"库里有 1265 块切好的原文"这件事对
# "什么都没上传、只给一个主题"的生成完全没有影响——用户看到的是纯模型知识。
#
# 这一段把同一个检索（`search_parent(..., library=True)`，与 `rag_query` 逐字段相同）
# 挪到 `_intake` 里：**生成之前由代码主动查一次库**，过门的父块作为一节证据拼进
# prompt。
#
# ## 三源优先级
#
#   ① `request.sources`（用户这次传的）→ ② 库检索（本地、免费、0.1–1 秒）→
#   ③ 外部检索（联网、6 秒、会失败）。
#
# "补足"的判据是**已有材料字数**：`_intake` 在外部检索之后算一次
# `len(sources)` 的合计，低于 `LIBRARY_SUPPLEMENT_CHARS`（3000）才查库。三条依据：
#
#   - 3000 字 ≈ 6 个父块（450–700 字/块），是一棵 20 节点树"每节点一条引用"的
#     下限（20 × ~100 字引文 + 冗余）。低于它，用户那批材料撑不起这棵树；
#   - 实测库能提供的量就在这个量级：10 个主题过门后合计 424 / 467 / 2566 / 4286 /
#     4700 字（见 `docs/LIBRARY-IN-GENERATE.md` 第 1 节）。用户自己给到 3000 字以上
#     时，库的边际贡献已被材料覆盖，而**库里没有一份是用户上传的**——两份不同主题的
#     材料混在一起只会让"按支检索"面对一堆无关候选；
#   - 不用 `SHORT_MATERIAL_CHARS`（300）当这道闸：那个数管的是"材料太短、切不出可
#     检索单元"，与"库能不能补"是两件事。300–3000 字之间恰恰是最需要补的区间。
#
# ## 为什么门用 reranker 的分，而不是 BM25 / 余弦
#
# 这是本能力能不能成立的全部。实测量了三档（`docs/LIBRARY-IN-GENERATE.md` 第 1 节）：
#
#   - `score` 是 RRF 名次分（第 1 名恒 0.0164、第 8 名恒 0.0147），**没有绝对含义**，
#     任何阈值都筛不了；
#   - `score_bm25` **方向是反的**：库里真相关但用词泛的主题（「海绵城市建设」，8 条
#     全部相关）BM25 只有 1.31–1.89，而库里没有的主题（「量子计算的基本原理」）撞上
#     课程大纲里的"基本/原理/计算"拿到 6.11–9.94。按 BM25≥5 过门 = 挡掉 8 条真证据、
#     放进 4 条噪声；
#   - `score_vector` 区分度不够：噪声能到 0.571（量子计算），真相关能低到 0.562
#     （碳中和那两篇摘要），两个分布重叠。
#
# `rerank_score`（[0,1] 的 cross-encoder 概率）在两档上分得开：8 个主题里真相关
# 0.584–0.890、无关 0.191–0.294，中间空着 0.29→0.58。所以门取 0.6。
#
# **代价是每次生成多 0.9–1.4 秒**（rerank 一次往返；不开重排时库检索本身 18–200ms）。
#
# **重排不可用时 fail-closed**：`MINDMAP_RERANK=0` 或 reranker 挂了时 `rerank_score`
# 全是 None，门就没有判据。这时**不用库**并报一条诊断——上面那三档数字说明，没有
# rerank 的库命中一半以上是跑题块，宁可不补。
LIBRARY_IN_GENERATE_ENV = "MINDMAP_LIBRARY_IN_GENERATE"
# 已有材料合计到这个字数就不查库（依据见上面那段）。
LIBRARY_SUPPLEMENT_CHARS = 3000
# 一次库检索取几条父块。与 `rag_query` 的缺省 top_k 同量级：8 条父块 ≈ 4000–5600 字，
# 再多也只是把 prompt 撑长，而模型能用的引用数由节点数决定。
LIBRARY_GENERATE_TOP_K = 8
# 过门线。实测真相关最低 0.584、无关最高 0.294（8 个主题）。
LIBRARY_RELEVANCE_GATE = 0.6
# 过门的命中少于这个数就不补：一条命中撑不起一节证据，只会在 prompt 里留一句
# 与其它材料对不上的孤证，而它占掉的仍是"每个节点都要引用"的额度。
LIBRARY_MIN_HITS = 2


def library_in_generate_enabled() -> bool:
    """生成时是否主动查库。默认**关**，显式写 `1/on/true/yes` 才算开。

    判定方式与 `library_search_enabled` / `external_knowledge_enabled` 同一套
    （正列举真值），缺省值不同是刻意的：这个能力会改变"有材料/没材料"这条主分支
    的形状（库命中会让 `sources` 变成非空，于是模型被要求每个节点都写引用），
    要由数字与用户选择决定，不由默认值决定。
    """
    return (os.getenv(LIBRARY_IN_GENERATE_ENV, "0").strip().lower()
            in LIBRARY_SEARCH_TRUTHY)


# 库检索没补上时的原因。取值与 `_library_supplement` 的出口一一对应，
# `_library_supplement_diagnostic` 每种给一句不同的话——"这次没用库"有五种来路，
# 折成一句"没命中"会让"重排没开"与"库里确实没有"看起来一模一样。
LIBRARY_SKIP_NO_LIBRARY = "no_library"
LIBRARY_SKIP_ENOUGH_MATERIAL = "enough_material"
LIBRARY_SKIP_NO_HIT = "no_hit"
LIBRARY_SKIP_NO_RERANK = "no_rerank"
LIBRARY_SKIP_FEW_HITS = "few_hits"
LIBRARY_SKIP_ERROR = "error"


# ---------------------------------------------------------------- 外部知识检索开关
#
# 用户只输一个主题时（`request.sources` 为空，或材料太短被降级），这条链路原先
# 零检索、零引用：`DETAIL_NO_SOURCE_RULES` 明确告诉模型"没有可标出处的材料"。
# 打开这个开关之后，主题先被拿去外部源（中文维基 / 快懂百科 / arXiv / OpenAlex /
# Crossref）搜一次，命中转成 `Source` 入库，然后**原样走有来源那条路**——按支检索、
# 拼证据、引用校验一行不改。所以"主题生成"与"材料梳理"在路径上是同一条：
# `_generate_deep` 里那个 `sources` 非空的分支才是主路，空的那一支只是"连外部都
# 没搜到"时的兜底（纯主题）。
#
# 默认**开**（`dceae7e` 之后翻的）。翻之前先量过一轮，理由不再是"技术上做得到"：
#
#   - 18 个真实主题 × 2 轮 = 36 次检索，35 次拿到 ≥1 条（97.2%）；唯一一次失败是
#     首轮的"碳中和"（OpenAlex / Crossref 的 TLS 连接被掐、arXiv 502，整次 6 秒内
#     全空），同一主题第二轮 2.0 秒拿到 3 条、3128 字——那是代理节点抖动，不是主题
#     搜不到，所以按 97.2% 而不是 94.4% 记；
#   - 延迟：中位 4.3–4.5 秒、P90 5.6–6.0 秒、最大 6.5 秒（墙钟上界仍是
#     `external.DEADLINE_SECONDS=20`）；这个量级用户能感知到"在查资料"，但不至于
#     以为卡死，而换来的是一棵**带引用**的树；
#   - 中文主题 14 个，返回正文里中文字符占 68%——中文场景不是"能跑但全是英文论文"。
#
# **什么时候会失败**（都实测过，不是猜测）：
#
#   1. 代理没开 / 公司网络封外网：五个源全部连接被拒，5–6 秒后返回空，然后按纯
#      主题生成（`content` 里会说"外部检索出错（ConnectError…）"）。生成本身不会
#      失败——这条链的契约是"失败即空"，见 `_external_sources`。
#   2. 快懂百科（`baike.com`）在带代理的机器上 18/18 全挂
#      （`SSL: UNEXPECTED_EOF_WHILE_READING`），实际只有四个源在跑。它是"第二个
#      中文百科视角"，挂了不影响 ≥1 条的成功率，所以没有因此回退默认值。
#   3. **搜到 ≠ 搜得准**。代码术语与长问句上明显偏差：`C++ 移动语义` 三条命中里
#      两条是语言学论文（"语义特征考察""语义记忆"）、`Rust 所有权` 拿到"农业"
#      （rust 是锈病）、一条 19 字的长问句拿到"为什么足球走向世界…"。逐字校验只
#      保证"这句话出自那个来源"，不保证"那个来源与主题相关"，所以 `content` 与
#      `GENERATION_FROM_EXTERNAL` 诊断都会明说"依据来自外部检索"，让用户知道这批
#      引用该按什么标准看。
#
#      现在抓取层有了一道**词面相关性门**（`external._relevant_hits`）：完全跑题的
#      命中会被挡掉（`rust 学习路线` 的「陳淑樺」「小米澎湃OS」「流行性感冒」、
#      `C++ 移动语义` 的「语义记忆」、`Rust 所有权` 的「农业」），实测噪声 22→10、
#      三条全相关 10/20→12/20。但**同形异义挡不住**：`Rust 所有权` 那条小麦锈病
#      论文（`Stripe Rust`）照样通过——判据看见 `Rust` 这个词形就放行；`rust 学习
#      路线` 那条教学模式论文（"深度学习路线"）甚至原样包含查询词块。残余的 10 条
#      噪声全是这一种，数字与天花板都在 `scripts/measure_external_relevance.py` 的
#      docstring 里。
#
# 关着（`MINDMAP_EXTERNAL_KNOWLEDGE=0`）时与 `dceae7e` 逐字节相同
# （`tests/test_external_knowledge.py` 拿那一版的 `tools.py` 钉着）：一次外部请求
# 都不发、提示词不动、`data` 里不多 `external` 键。显式关掉是"这台机器上不了网"
# 时唯一该做的事——不是可选优化。
EXTERNAL_KNOWLEDGE_ENV = "MINDMAP_EXTERNAL_KNOWLEDGE"
EXTERNAL_KNOWLEDGE_TRUTHY = frozenset({"1", "on", "true", "yes"})
EXTERNAL_KNOWLEDGE_DEFAULT = "1"

# 一次检索最多拿几条外部命中。3 条与 `external.DEFAULT_LIMIT` 是同一个数（引用
# 而不是重写一个 3）：那边是抓取层的默认，这边是"这次生成愿意为它花多少条清单
# 位"，改一处忘另一处会让"库里存了 5 条、模型只看到 3 条"变得没人说得清。
EXTERNAL_HIT_LIMIT = external_knowledge.DEFAULT_LIMIT

# 外部检索那两个 `stage` 帧的稳定标识（开始一帧、结束一帧，见 `_emit_stage`）。
# 取 `"external_search"` 而不是中文：它是**标识**不是文案，要出现在 SSE 帧、日志与
# 用例断言里；给人看的那句话在 `detail`（"正在检索外部资料…"），改文案不该动它。
EXTERNAL_SEARCH_STAGE = "external_search"

# 用户在界面上关掉联网检索时那句话的措辞。刻意**不复用**"没有搜到外部资料"：那句
# 说的是"试过了，没结果"，用户看完会去换一个更常见的主题词重试；而这里他该做的是
# 把开关打开——照着错的动作重试一百次，结果一模一样。
EXTERNAL_OFF_NOTE = "你在设置里关掉了联网检索"

# 第三段（只写 detail）的 `stage` 帧标识。它存在的理由与外部检索那一对同源：这一段
# 实测要 24–46 秒，而**界面上一个节点都不长**（`node_grow` 属于结构那一次）——用户
# 看到的是"树长完了，然后卡住半分钟"。帧形状与外部检索完全一样（开始一帧没有
# `count`、结束一帧带条数），所以前端不用为它分叉。
DETAIL_STAGE = "detail"

# 「按章切」那条路的结构 `stage` 帧标识。它与 `DETAIL_STAGE` 是**两段不同的等待**：
# 这条路把结构那一次调用换成了 N 次（实测 12 章 15–25 秒）。这一段里现在**也有**
# `node_grow`（每章的结构调用各自边收增量边报，见 `_slice_one`），所以这个标识
# 不再表示"一个节点都不长"，只用于区分"卡在按章写结构"还是"卡在写说明"。
# 文案由 `detail` 承载，这个标识只用于前端与日志。
SECTION_SLICE_STAGE = "section_slice"

# 一条引文的上限。与 `SourceRef.quote` 的 `max_length=300` 同源（那边是硬边界）：
# 模型偶尔会抄一整段，超过 300 时**截断**而不是整条丢掉——截断留下的是真原文，
# 丢掉留下的是零引用。提示词里写的也是这个数（`SECTION_SLICE_REF_RULES`）。
SECTION_SLICE_QUOTE_CHARS = 300

# ---------------------------------------------------------------- 一级分支概括
#
# 画布默认只画 40 个节点（`canvas/view-collapse.ts`），被截断的父节点上有一枚 `+N`
# ——它证明"这里还有 N 个直接孩子"，证明不了"这一支一共还有多少、讲的是什么"。
# 后者只有在一级分支上才答得出来，而一级分支只有几条、位置固定。
#
# 数字是免费的（前端 `countSubtree` 自己数），**文案要调模型**：这是本轮唯一新增的
# 一次调用，所以它有独立开关，且默认关。
BRANCH_DIGEST_ENV = "MINDMAP_BRANCH_DIGEST"

# 一次调用要概括几条一级分支的上限与输出预算。
#
# 上限与 `MAX_NODE_CHILDREN`（50，`Node.children` 的契约上限）**同一个数**：超过它的
# 一级分支根本存不进契约，所以 50 就是"所有合法树都能一次覆盖"的那个界。取 20 会让
# 库里那棵 50 支的探针树有 30 支永远没有概括——而"为什么这 30 支没有角标"在界面上
# 查不出来（`docs/VIEW-COLLAPSE.md` §九 记着那棵树）。
#
# 一条概括 8–12 字，50 条约 600 字 ≈ 900 token；给 2000 是给 JSON 骨架与模型偶尔
# 多写一句留的余量。**不给 `_max_tokens(ctx)`**：那条口径是"这一次要写多少个节点的
# 正文"，与"写几十句短标题"差两个数量级，照它发包只会让模型以为自己该长篇大论。
BRANCH_DIGEST_MAX_BRANCHES = 50
BRANCH_DIGEST_MAX_TOKENS = 2_000

# 每条一级分支往下看几个子节点的 label。3 个足够定性（"训练与并行"这种概括来自
# 下面那三个子节点叫什么），而**不塞 `detail`**：一段 80–150 字的说明 ×6 支会把
# 这一节的输入从两百字撑到一千字，而概括恰恰是"只看目录"比"读完正文"更准的任务。
BRANCH_DIGEST_CHILD_SAMPLES = 3


def branch_digest_enabled() -> bool:
    """是否在一级分支上写概括。默认**关**，显式写 `1/on/true/yes` 才算开。

    判定与 `library_in_generate_enabled` / `external_knowledge_enabled` 同一套。
    默认关的理由与这几天所有新功能一致：它多一次模型调用（实测 1.87 秒 / 5 组），
    而"这一支还有多少节点"这条信息在没有概括的情况下由 `+N` 与顶部「显示 N / M」
    已经说了一部分——要不要为剩下的那一半付这笔钱，由数字决定，不由默认值决定。
    """
    return (os.getenv(BRANCH_DIGEST_ENV, "0").strip().lower()
            in LIBRARY_SEARCH_TRUTHY)


# ---------------------------------------------------------------- 结构自检回路


# 这条回路是"检查"那一步，不是"生成"那一步——`docs/SUBAGENT-VS-SELFCHECK.md`
# 第六节那句话的落地：**该分的是检查，不是生成**。生成分片会失去全局视野
# （跨支重复 31% vs 12%），检查分项不会（它本来就是在全局视野下做的）。
#
# 它买到的是现状唯一给自己定义过的缺陷：`CROSS_BRANCH_DUPLICATE` 那条诊断存在、
# 15% 的图命中、且明写着"只报不删"，而**逐字判据看不见的那一类**（同义不同词的
# 跨支重复）在七条规则里没有任何一条管。实测两张真图各 +1 次调用：跨支重复
# 6→0 / 4→0（逐字召回 100%），另抓到 8 条语义重复与 2 条内容跑题；精度 22/25。
#
# **产出是清单，不是动作**：88% 的精度意味着自动改会伤到那 12%，而"可指认"正好
# 对上用户真正要的东西（哪一支错了能定位）。所以这条回路一个节点都不删。
SELF_CHECK_ENV = "MINDMAP_SELF_CHECK"

# 一次审查调用的输出预算。一条 `{node_id, kind, problem, fix}` 约 60–90 字，实测
# 两张真图报 15 / 10 条。给 4000 token 是给"问题多、每条要说清"留的余量，同时
# 不让它有机会把清单写成一篇报告——`_detail_ask` 那边的教训同源。
SELF_CHECK_MAX_TOKENS = 4_000

# 一份清单最多留几条。真实产物 10–15 条，30 条已经过了"一屏看不过来"的线；
# 超出的部分**丢掉尾部并记数**（`dropped`），不静默截断。
SELF_CHECK_MAX_ISSUES = 30

# 每条问题的两个自由文本各自的上限。它们要显示在诊断/面板上，不是文章：
# `problem` 说清"是什么问题"，`fix` 说清"怎么改"，各 80 字足够。
SELF_CHECK_TEXT_CHARS = 80

#: 只认这四类。**值域写死而不是让模型自由发挥**：一个"其他问题"桶会让清单变成
#: 谁都能往里丢东西的地方，而界面上要按 kind 分组计数（`data.self_check.counts`）。
SELF_CHECK_KINDS = (
    "cross_branch_duplicate",
    "semantic_duplicate",
    "off_topic",
    "level_odd",
)

#: kind 的中文名（诊断文案用）。与 `_AUTO_REPAIR_RULE_NAMES` 同一条规矩：诊断里
#: 说人话，`data` 里留机器值。
_SELF_CHECK_KIND_NAMES = {
    "cross_branch_duplicate": "跨支重复",
    "semantic_duplicate": "语义重复",
    "off_topic": "内容跑题",
    "level_odd": "层级异常",
}

SELF_CHECK_PROMPT = """你是思维导图的审稿人。别人已经写好了一张导图，你要找出**结构性问题**。

你只会看到**目录**：一级分支、缩进的下级节点、每个节点后面的 id。看不到说明文字，
所以只报"靠标题与位置就能判定"的问题。

只报下面四类，每条都要给出**清单里那个 id**（`(id=…)` 括号里的一串）：

1. `cross_branch_duplicate` —— 同一个说法挂在**两条不同的一级分支**下，字面相同。
2. `semantic_duplicate` —— 同一件事换了说法，在同一张图里出现不止一次（同父或跨支
   都算）。例：「监督学习：依赖标注信号」与「监督学习：从标注数据学映射」。
3. `off_topic` —— 这个节点与它所在的一级分支、以及整张图的主题都不是一回事。
4. `level_odd` —— 层级异常：父子其实是同一层的两种说法（白占一层），或者节点挂错
   了层（本该在别的分支下）。

判据要严：**宁少不滥**。这份清单会原样交给用户去核对，报错一条的代价比漏掉一条大。
拿不准就不报。

不要报下面这几类（它们不是问题）：
- 「某个节点只有一个孩子」——父子可能本来就是两层不同的内容；
- 标题长短、用词风格、有没有编号前缀；
- 缺失的细节（你看不到正文，判断不了）。

只返回一个 JSON 对象，不要别的话：

{"issues":[{"node_id":"a1","kind":"semantic_duplicate","problem":"一句话说清问题","fix":"一句话说清怎么改"}]}

没有问题时返回 {"issues":[]}。"""


def self_check_enabled() -> bool:
    """是否在结构写完之后再跑一次自检。默认**关**，显式写 `1/on/true/yes` 才算开。

    默认关的理由与这几天所有新增模型调用一致（`branch_digest_enabled` 同一套判定）：
    它是 **+1 次调用、3–5 秒、约 1.2 k 输入 token**，而一次生成已经有 3–5 次要等。
    收益是真的（上面对比度那一段），但"要不要为它多等五秒"该由数字决定，不由默认值
    决定——这条默认值本身也是数字：实测一次调用买到 22/25 条站得住的问题。

    `tests/test_agent_self_check.py::test_without_the_switch_there_is_no_self_check_call_and_no_key`
    钉着"关着时产出与没有这个功能时逐字节相同"（连 `data` 的键集都不多一个）。
    """
    return (os.getenv(SELF_CHECK_ENV, "").strip().lower()
            in GRAPH_SKELETON_TRUTHY)


# ---------------------------------------------------------------- 收尾的确定性修复
#
# `optimization/service.check()` 报七种结构码，其中**三条的修法唯一**
# （`optimization/repair.AUTO_REPAIRABLE_CODES`）：合并同级重名永远并进第一个、
# 压单子链永远外层并进内层，结果与"分段跑还是一趟跑"无关。那三条可以在生成收尾
# 顺手做掉，**零模型调用**。
#
# 收益是实测的（`docs/SUBAGENT-VS-SELFCHECK.md` 第五节）：79 张真实产物里 37 张
# 存在"一个节点只有一个孩子"的冗余层，共命中 116 次——那是模型一口气写完整棵树
# 时的典型形状。这些图今天要用户自己点一次「一键修复」才拿到同样的结果。
AUTO_REPAIR_ENV = "MINDMAP_AUTO_REPAIR"

#: 关掉它的三个写法。用 `_FALSY` 而不用 `_TRUTHY`：这条**默认开**，与
#: `graph_outline_enabled` / `mixed_skeleton_enabled` 同一套判定。
_AUTO_REPAIR_FALSY = frozenset({"0", "off", "false", "no"})


def auto_repair_enabled() -> bool:
    """生成收尾是否顺手跑一遍确定性修复。默认**开**。

    默认开而其余新功能默认关，唯一理由是这笔账的方向相反：它一次模型调用都不花
    （`optimization/repair.py` 里没有任何网络调用），买到的是 37/79 张图的冗余层。
    一个零成本、结果可撤销、只动"修法唯一"那三条的动作，默认关等于让它永远不发生。

    留着开关不是为了"要不要省这次调用"（没有调用可省），是为了**能复现接线之前
    的产物**：那份报告里所有数字都取自那条路，而"这棵树被自动修过"与"模型本来就
    这么写"在产出上必须分得开——分得开的唯一办法是能把它关掉重跑。
    """
    return (os.getenv(AUTO_REPAIR_ENV, "1").strip().lower()
            not in _AUTO_REPAIR_FALSY)


#: 修复动作的中文名（诊断里用）。与 `RepairKind` 是两个值域：这里按**规则**说，
#: 因为用户读诊断时问的是"哪条规则动手了"。
_AUTO_REPAIR_RULE_NAMES = {
    "DUPLICATE_LABEL": "合并同级重名",
    "SINGLE_CHILD": "压掉单子链",
    "HEADING_ONLY_BRANCH": "把只是标题的一级分支并进它唯一的子节点",
}

#: `data["auto_repair"]["items"]` 最多列几条。实测真实产物单张最多 74 步（190 节点
#: 那张「碳中和的主要方面」），30 条已经覆盖了"一张正常的图会被改多少"的量级；
#: 超出的报 `items_truncated`，不静默截断。
AUTO_REPAIR_MAX_ITEMS = 30


# 用户**自己把联网检索关掉**时的那句话。单独提成常量有两个理由：
#
#   - 它与"搜了但没搜到"是两件事（那条说"联网检索没有找到相关资料"），而两条路
#     （`_generate` / `_generate_deep`）都要说同一句话——两份字面量迟早会有一份先改；
#   - 向后兼容类的回归用例（`test_edges` / `test_agent_diagram_type` 拿旧版本
#     `tools.py` 逐字节比 `content`）要在比对前把这句剥掉。剥的时候需要一个**唯一
#     的**真相，抄在用例里就是第三份字面量。
#
# 这句话为什么必须有："关掉了"的正确下一步是打开开关，"没搜到"的正确下一步是换个
# 说法——共用一句会让关掉开关的人反复改主题去试一个根本不会发生的搜索。
#
# **句号在常量里**：它是"一句完整的、用户能直接看到的话"，不是半句。剥掉它做比对的
# 用例（见上）因此只需要一个 `str.replace`，拼接处不再有第二处判断。
EXTERNAL_DISABLED_NOTE = (
    "你关掉了联网检索，这张图完全基于模型知识，节点没有出处；"
    "要出处就打开联网检索再生成一次。"
)

# 材料总字数低于这个值时不走材料路径，改成"把短材料当主题提示 + 走外部检索/纯主题"
# （`_generate_deep` 的退化规则，话术见 `MATERIAL_TOO_SHORT`）。
#
# 取 `LEAF_MAX_CHARS`（300），而那个数的定义本身就是**引用字段的上限**
# （`SourceRef.quote` 的 `max_length=300`，`knowledge/service.py` 里把它钉成同一个
# 数并解释了为什么两处必须相等）。依据只有一句话：材料总字数不超过一条引用装得下
# 的字数时，**整份材料都能塞进一条引用里**——引用不再"指向某处"，只是"复述全文"，
# 定位与密度两件事同时失效，而这正是界面上"看不出哪些是编的"的来源。
#
# 两条同向的证据（用来核对 300 没有离谱，不是选它的理由）：
#
#   - `CHUNK_MIN_CHARS=150`：整篇低于 150 时 `_chunk_spans` 连切都不切，一段就是
#     一块；低于 300 时连子块都只有一块（`_leaf_spans` 在 `end-start <= 300` 时
#     直接返回整段），于是按支检索 `top_k=DEEP_BRANCH_TOP_K=3` 退化成 1 条、
#     `search_parent` 的"哪一段支撑哪一句"退化成"整篇都在这一条里"；
#   - 真实案例：8 字材料（`rust 学习路线`）走 `rag` 路径，一次检索只回 1 条 9 字
#     命中，模型据此生成 72 个节点、其中 7 个带引用（密度 9.7%）——另外 65 个是
#     模型自己的知识，而界面上它们与有引用的节点长得一模一样。
SHORT_MATERIAL_CHARS = LEAF_MAX_CHARS

# 「装得下的材料直接塞进 prompt」的闸：材料正文**合计** ≤ 它时，整份正文拼进 prompt、
# 不走按支检索（`_generate` 的 `full_text` 分支）；> 它时照旧每支检索一次。
#
# 与 `SHORT_MATERIAL_CHARS` 是两件事，别合并：那个判的是"太短，短到一条引文就能装下
# 整份材料"（引用失去指向性），这个判的是"够长，但还没长到模型看不住"。同一份 200
# 字材料两条都命中，处理方式是"当主题提示"（短的那条优先），全塞分支根本不会跑。
#
# **这个数的依据只有实测**（`scripts/measure_incontext.py`：三个长度 × 全塞/按支检索，
# 量中间位置关键事实的命中、引用存活率、引用准确性与 detail 质量）。物理上限是
# `CONTEXT_TOKEN_LIMIT`（200k token）/ 1.5 字每 token ≈ 30 万字，但注意力先于物理
# 边界失效——"看得到却用不上"发生在窗口塞满之前。
#
# 取 5 万字的实测量（1 万 / 5 万 / 15 万字三档，同一份材料、同一主题，每格两次采样，
# 原始数据见 `docs/FULL-CONTEXT.md`）：
#
#   | 长度 | 结尾段覆盖（检索 / 全塞） | 中间事实命中 | 触发裁剪（检索 / 全塞） | detail 均长 |
#   |---|---|---|---|---|
#   | 1 万 | 0/2 · **2/2** | 2/2 · 2/2 | 0/2 · 0/2 | 122 / 106 |
#   | 5 万 | 0/2 · **2/2** | 1/2 · **2/2** | 0/2 · **2/2** | 98 / 102 |
#   | 15 万 | 0/2 · 1/2 | 2/2 · 2/2 | 0/2 · **2/2** | 115 / 105 |
#
# 两条判据把闸定在 5 万：
#   - **覆盖**是这条路唯一的真实收益，而且很硬：六次检索一次都没覆盖到结尾那段
#     （那里与任何分支标题都不重合），全塞六次里覆盖了五次。那正是"按支检索的召回
#     上限由分支标题决定"这个结构性缺陷（`_search_branch` 的 28.8%）在产出上的样子；
#   - **代价不是"抄不准"，是"树写满"**（这一条是回头查原始响应才纠正的）：全塞时
#     模型产出 79–80 个节点、贴着 `INITIAL_MAX_NODES`，于是 `_clip_tree` 裁掉尾巴，
#     尾巴上的引用跟着消失（`data.source_refs.declared → kept` 的差额全部落在那几次
#     `TREE_CLIPPED_TO_INITIAL` 里，逐字校验一条都没剔）。检索那一路产出 62–75 个
#     节点，从不触发裁剪。裁剪从 5 万字档就开始（7–9% 的节点），15 万档还多丢了
#     一次覆盖——再往上开，用户拿到的就是"内容更全但树被砍短"，那不是这条路想要的形状。
#
# 反方向也要说清（别让这张表被读成"全塞更好"）：**detail 均长**全塞低 7%（104 vs 112），
# **引用密度**两路持平但检索那路自己抖动极大（1 万档两次采样 0.63 与 0.99，不足以下
# 结论）。所以这条路换来的是**覆盖**，不是出处密度也不是更好的说明。
#
# 取 0 或负数等于**关掉这条路**（一切照旧走检索）：留这个取值是为了让"哪天实测推翻了
# 全塞"有一个只改一行就能回退的开关，不必动代码结构。
FULL_TEXT_MATERIAL_CHARS = 50_000


def external_knowledge_off_reason() -> str:
    """这次为什么没走外部检索：`"deploy"` / `"user"` / `""`（开着）。

    **两个条件是与关系，次序写死在这里，全仓只有这一处**：

      1. 部署方的环境变量（`MINDMAP_EXTERNAL_KNOWLEDGE`，默认 `1`）关着 → 永远关。
         运维把它置 0 只有一种意思："这台机器上不了网"——那是一个**事实**，不是
         一个界面上某个勾能推翻的偏好；
      2. 环境变量开着 → 看用户偏好（`user_prefs.external_search.enabled`，默认开）。

    必须这一处、且必须先判环境变量。反过来（先看偏好）在"运维关了、用户在界面上
    打开了"时会让用户得到"我明明开了、它却一次都不联网"，而界面那个勾显示着"开"
    ——一个从任何一处都解释不通的状态。而"环境变量开、偏好关"没有歧义：那是用户
    自己要的，界面也如实显示着"关"。

    返回原因而不是只返回布尔值：这句话要进 `content`，而"部署方关了（你改也没用）"
    与"你在偏好里关了（你可以去打开）"是两句必须分开的话——把它们说成同一句，
    用户会照着错的那条去改。
    """
    allowed_by_deploy = (
        os.getenv(EXTERNAL_KNOWLEDGE_ENV, EXTERNAL_KNOWLEDGE_DEFAULT)
        .strip().lower() in EXTERNAL_KNOWLEDGE_TRUTHY)
    if not allowed_by_deploy:
        return "deploy"
    if not prefs.external_search_enabled():
        return "user"
    return ""


def external_knowledge_enabled() -> bool:
    """外部知识检索是否打开。**默认开**，显式写 `0/off/no` 等才算关。

    判定方式与 `library_search_enabled` 一致（同一个 truthy 集合），但缺省值不同：
    这里是 `"1"`，那个是 `"0"`。两处不该被"看起来不一致"改成一样——库检索依赖
    本地索引（随时可用），外部检索依赖一次外网往返（理由与实测见上面那段注释）。

    为什么默认开：主题生成原先在"一条依据都没有"的情况下产出整棵树，用户看不出
    哪些节点是编的；外部检索把命中转成 `Source` 之后走的是与"材料梳理"完全相同的
    RAG 链，引用逐字可校验、可点回原文。实测 97.2% 的主题能拿到 ≥1 条（数据同上），
    6 秒以内返回——这个代价换"看得见的出处"是划算的。

    什么时候会失败：代理没开/外网被封（5–6 秒后返回空，退回纯主题）、快懂百科在
    代理下 100% 失败、以及"搜到了但相关度差"（逐字校验管不了相关性）。三种都在
    `content` 与诊断里各自有一套说法，没有一种是静默的。

    显式关掉时整条外部分支被跳过，行为与 `dceae7e` 逐字节相同——那是"这台机器
    上不了网"时唯一该做的事。

    **还有第二道闸**：环境变量开着时，用户可以在「我的偏好」里把
    `external_search.enabled` 设成 `false`，于是界面上真的关得掉。优先级写死在
    `external_knowledge_off_reason()` 里（部署方优先），理由也写在那里；这个函数
    只是它的布尔形态——两处不要各写一遍判定，那正是"运维关了、界面上却显示开"
    这种状态的来源。
    """
    return not external_knowledge_off_reason()


def library_drop_external() -> bool:
    """`knowledge.service` 的 `drop_external` 参数：库检索要不要剔掉外部来源。

    `external_knowledge_enabled()` 只管"这一次要不要发外网请求"。它挡不住第二件
    事：外部命中是会入库的（`_external_sources` → `library.ingest`，见那里的三条
    理由），入库之后它们就成了知识库里普通的持久来源，`rag_query` / `node_expand`
    默认（`library_search_enabled()` 为真）会照常把它们捞回来。于是用户关掉
    「联网检索」之后，生成的图里仍然有维基/论文的依据，而 `content` 同时宣称
    "这张图完全基于模型知识"——一边否认、一边列着出处。

    判据取 `prefs.external_search_enabled()` 这**一半**，不取
    `external_knowledge_enabled()` 那个合取：部署方关掉
    （`MINDMAP_EXTERNAL_KNOWLEDGE=0`）的意思是"这台机器上不了网"，而库里已经存在
    着的东西不需要网也能读，把它也排除等于白白丢掉一台离线机器本来还能用的资料；
    用户关掉的意思是"我不要外部资料"，那才必须连复用一起停掉。

    这就是那条偏好读路径的**第二个**落点（第一个是 `_intake` 的闸）。两个落点必须
    都在，因为它们是两件事："别去外面拿"与"别拿以前的"。只做前一半，bug 就是
    "关了还在搜"。

    取反写在这一处：调用点各自写 `not ...` 的话，迟早有一处忘记取反，而那种错的
    表现恰好是"开关反着生效"，看起来像权限问题。
    """
    return not prefs.external_search_enabled()


# `source_name` → `SourceKind`。表而不是三元表达式：加一个新源时只能改这里一处，
# 而"忘了改"的表现是新源全部落进 `web`——不报错，只是界面上的类型标签错了一个字，
# 谁也不会注意到。
#
# 分类不是按"哪个站"而是按**内容是什么**：百科给的是对某个词的通俗解释（`web`，
# 与用户上传的网页材料同类），论文给的是研究（`paper`）。`generation.service` 的
# `EXTERNAL_TRUST_RULES` 会按这两类给模型不同的提示语，所以这不是纯装饰。
_EXTERNAL_KINDS = {
    "wikipedia": SourceKind.web,
    "baike": SourceKind.web,
    # DeepSeek Search 的命中是**抓回来的网页正文**，不是论文条目：它可能来自
    # `ccf.org.cn`、`cppinfo.cn`、专利库、厂商文档站——分类按内容给，所以是 `web`。
    # 落 `paper` 会让 `EXTERNAL_TRUST_RULES` 对它说"这是一篇论文"，而它可能是一页
    # 教材目录。
    "deepseek": SourceKind.web,
    "arxiv": SourceKind.paper,
    "openalex": SourceKind.paper,
    "crossref": SourceKind.paper,
}


class ToolRegistry:
    """按名字分派的工具表。

    只注册不执行任何未登记的名字：模型不能凭空调用一个没实现的工具，
    这一点由 `execute` 的第一行保证，不依赖模型自觉。
    """

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"工具 {spec.name} 重复注册")
        self._specs[spec.name] = spec

    def __contains__(self, name: str) -> bool:
        return name in self._specs

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[dict[str, Any]]:
        return [spec.schema() for spec in self._specs.values()]

    async def execute(self, call: ToolCall, ctx: RunContext) -> ToolResult:
        spec = self._specs.get(call.name)
        if spec is None:
            # 返回而不是抛：模型偶尔会编一个很像的工具名（把 mindmap_generate_deep
            # 写成 generate_mindmap）。写成一条可读错误回给模型，下一轮它通常
            # 能自己改对；抛异常则整个 run 报废。
            return ToolResult(
                ok=False,
                content=f"没有名为 {call.name} 的工具，可用的有：{', '.join(self.names()) or '（无）'}",
                data={"code": "unknown_tool", "name": call.name},
            )

        if call.parse_error:
            return ToolResult(
                ok=False,
                content=f"参数解析失败：{call.parse_error}",
                data={"code": "invalid_tool_args", "errors": [{"path": "", "reason": call.parse_error}]},
            )

        try:
            args = spec.args_model.model_validate(call.arguments)
        except ValidationError as exc:
            errors = [
                {"path": ".".join(str(part) for part in item["loc"]), "reason": item["msg"]}
                for item in exc.errors()[:5]
            ]
            detail = "；".join(f"{item['path']}: {item['reason']}" for item in errors)
            return ToolResult(
                ok=False,
                content=f"参数不合法：{detail}",
                data={"code": "invalid_tool_args", "errors": errors},
            )

        # 同步与异步工具都要支持：纯计算的工具（按 op 改树）没有 await 点，
        # 强制写成 async 只是给作者加噪音；调模型的工具必然是 async。
        result = spec.fn(args, ctx)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, ToolResult):
            raise TypeError(f"工具 {call.name} 必须返回 ToolResult，收到 {type(result).__name__}")
        return result


# ---------------------------------------------------------------- 参数模型


class GenerateDeepArgs(BaseModel):
    """分片生成的入参。

    `branches` 是**历史遗留的无效字段**（填了不影响产出，见下面那段注释）；
    `points_per_branch` 是**期望值**而不是硬约束：骨架给多给少一条都继续，
    展开不足只补那一支。把它写成严格契约，等于把模型的一次抖动升级成整次失败。
    """

    model_config = ConfigDict(extra="forbid")
    topic: str = Field(min_length=1, max_length=1000, description="中心主题，用用户的语言")
    # **不删字段，只把 description 改成它现在真正的用途（= 没有用途）**。
    # `docs/BRANCH-CAP.md` §5.1① 记的就是这个字段：一级分支条数改成由内容决定
    # （`NO_BRANCH_CAP_RULE`）之后，`app/` 里已经没有一处读它（`test_branches_arg_is_inert`
    # 钉着"填 3 与填 20 产出逐字节相同"）。
    #
    # 为什么不是删掉字段（那一节说两种处置都可以）：本模型的 `model_config` 是
    # `extra="forbid"`，删字段等于**模型凭旧记忆传 `branches` 时整次工具调用校验失败**
    # ——把"参数是噪音"升级成"参数是故障"。而仓库里四十余处用例与五个度量脚本仍在显式
    # 传它（它们钉的是别的行为，不读这个值），删字段还要连它们一起改。工具参数不进
    # `openapi.json`（同节 §2 已核），两种处置都不触发契约变更，所以选不制造新故障的那种。
    branches: int = Field(
        default=5, ge=3, le=DEEP_MAX_BRANCHES,
        description="已废弃：一级分支条数由内容决定，填什么都不会改变结果")
    points_per_branch: int = Field(default=3, ge=2, le=5, description="每条一级分支下至少要有几个要点")
    # 深度同样只是**预算**而不是硬契约：额度不够时会被截短并标 `partial`，
    # 而不是把整次生成判成失败。
    max_depth: int = Field(
        default=DEEP_DEFAULT_DEPTH, ge=2, le=DEEP_MAX_DEPTH,
        description="往下展开到第几层（root 之下算起）：2 只有两层，3 可到三级节点",
    )
    # 图型。三者共用同一套「骨架 → 按支展开 → 验收」流水线，差别只在**骨架那一次
    # 的提示词**与"要不要解析 edges"：
    #
    #   - `tree`：思维导图。一级分支是话题分类，不产出边。**默认值，行为与加这个
    #     字段之前逐字节相同**（`tests/test_edges.py` 拿 `e3ba390` 的旧模块钉着）。
    #   - `flowchart`：流程图。一级分支必须是**阶段**（按执行顺序排列），骨架可以
    #     带 `edges`：判断节点的两个出口各挂一条 `branch` 边（带"是"/"否"），
    #     回退挂 `loop` 边。这是"树 + 附加边"唯一能表达有环/有汇合的地方。
    #   - `fishbone`：鱼骨图。一级分支固定为六个类别（人/机/料/法/环/测），
    #     不产出边——鱼骨图的关系全部由层级表达，多画线只会让它变乱。
    #
    # 为什么不做成三个工具：三者除了骨架提示词以外每一步（并发、预算、检索、
    # 验收、诊断）都一样。拆成三个工具等于把那条流水线抄三份，而抄出来的第二份
    # 迟早与第一份在某个边界上分叉。
    diagram_type: Literal["tree", "flowchart", "fishbone"] = Field(
        default="tree",
        description="图型：tree 思维导图（默认）/ flowchart 流程图（可带附加边）/ fishbone 鱼骨图",
    )


class GenerateArgs(BaseModel):
    """整树一次生成的入参。

    比 `GenerateDeepArgs` 少了三个字段，少的每一个都是"分片才需要"的旋钮：

      - `points_per_branch` —— 分片按支分配额度用；整树没有"每支一次调用"，
        内容多少由一次响应里的树自己决定；
      - `max_depth` —— 分片的递归深度预算；整树的层数由 `DETAIL_PROMPT` 里的
        初次规模（`INITIAL_MAX_DEPTH`，root 之下 3 层）约束，不另开旋钮；
      - `fanout`（由 `points_per_branch` 推）—— 同上。

    留着它们会让模型以为"这次可以要求每支 5 个要点"，而整树路径根本没有那个
    分配器，填了也不生效——一个永远填了没用的参数比没有这个参数更糟。
    """

    model_config = ConfigDict(extra="forbid")
    topic: str = Field(min_length=1, max_length=1000, description="中心主题，用用户的语言")
    # 与 `GenerateDeepArgs.branches` 同一处置、同一理由（见那一处注释）：条数由内容定
    # （`NO_BRANCH_CAP_RULE`），这个字段一处都不读，只把 description 改成实话。
    branches: int = Field(
        default=5, ge=3, le=DEEP_MAX_BRANCHES,
        description="已废弃：一级分支条数由内容决定，填什么都不会改变结果")
    diagram_type: Literal["tree", "flowchart", "fishbone"] = Field(
        default="tree",
        description="图型：tree 思维导图（默认）/ flowchart 流程图（可带附加边）/ fishbone 鱼骨图",
    )


class ExpandArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(min_length=1, max_length=100, description="要展开的节点 id，取自当前大纲")
    count: int = Field(default=3, ge=2, le=4, description="要补充几个子要点")
    instruction: str | None = Field(default=None, max_length=300, description="可选的补充要求")


class FillDetailsArgs(BaseModel):
    """`mindmap_fill_details` 的参数：**只有一个可选的补充要求**。

    刻意**不给 `node_ids`**：要补哪些节点由代码按 `_detail_targets` 算
    （判据是"说明短于 `DETAIL_FILL_FLOOR`"，不是"空"）。让模型自己圈目标，
    它就会漏——它看不到整棵树（大纲只列前 200 个节点），而"漏掉的那几个"
    恰好是它以为自己写完了的那几个。这与 `audit_tree` 不接受任何参数是同一条理由：
    读的必须是服务端那棵真树。

    `count` / `max_tokens` 之类也不给：切几半、花几次调用由 `_fill_details` 按
    节点数与额度决定，多一个旋钮就多一种模型能把自己调进死路的方式。
    """

    model_config = ConfigDict(extra="forbid")
    instruction: str | None = Field(
        default=None, max_length=300,
        description="可选的补充要求（口吻、长度、侧重哪一面），会拼进写说明的提示词")


class AuditTreeArgs(BaseModel):
    """`audit_tree` 没有参数：它数的是**当前这棵树**，不是模型描述的那棵树。

    刻意不留任何旋钮。这个工具的存在理由是"模型以为自己写完了"，它的输入必须是
    客观状态（`ctx.candidate`）而不是模型的复述——一旦允许传 `branches` 之类，
    模型就会把自己记忆里那份骨架传进来，审的成了它自己的记忆。
    """

    model_config = ConfigDict(extra="forbid")


# `rag_query` 的条数口径：**`top_k` 从此是下界**，实际条数由分数定（3–8）。
#
# 为什么把它也动态化：这条是模型唯一能自己调的检索入口，而它原来被 `le=5` 钉死、
# 默认 5。实测（`scripts/measure_topk_threshold.py`）说明这不是"模型要不要更多"的
# 问题——第 4 名与第 3 名的分差中位只有 0.026，模型说"要 5 条"时第 6 条常常只差
# 0.03 分，砍掉它没有任何依据。`max_k=8` 与 `LIBRARY_GENERATE_TOP_K=8` 同量级，
# `le` 从 5 放宽到 10 只是**允许**模型要得更多（它仍可以只要 3 条）。它**不进**
# `openapi.json`：`RagArgs` 是 `ToolSpec.args_model`，schema 由
# `contracts/agent.py::ToolSpec.parameters` 在运行时 `model_json_schema()` 生成，
# FastAPI 的 `/openapi.json` 里没有工具参数（改完跑过 `scripts/export_openapi.py`，
# 无 diff）。放宽它是为了"下界"这个新语义，不是为了改 HTTP 契约。
RAG_QUERY_KEEP = KeepPolicy(min_k=3, max_k=8, rel=0.85)


class RagArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=500)
    #: **下界**，不是条数：`_rag` 把它当 `RAG_QUERY_KEEP` 的下界用。给 5 意味着
    #: "至少 5 条"，最多 8 条（`RAG_QUERY_KEEP.max_k`）。
    top_k: int = Field(default=5, ge=1, le=10)
    # 检索前置的元数据过滤条件，原样透传给 `search_sources`（形状见
    # `knowledge.service.SourceFilter`）。用 dict 而不是模型字段，是为了让
    # "模型乱填过滤条件"这件事在工具入口被吸收成"不过滤"，而不是变成一次
    # 工具调用失败——检索本身没有错，没必要连带报错。
    filters: dict[str, Any] | None = None

    @field_validator("filters", mode="before")
    @classmethod
    def _non_dict_filters_are_ignored(cls, value: Any) -> Any:
        """非 dict 的 filters 当成"没给"。

        模型偶尔把条件写成字符串（`"author=张三"`）或者干脆传个数组。让一个
        可选参数把整次 `rag_query` 判死没有意义：检索本身没错，错的只是这个
        附加值，降级成"不过滤"更划算。
        """
        if value is None or isinstance(value, dict):
            return value
        logger.debug("忽略 rag_query 的非法 filters（%s）：%r",
                     type(value).__name__, value)
        return None


class EditChange(BaseModel):
    """批量工具（`mindmap_edit`）单条 update 允许改的字段。

    只到 `label` / `detail` / `color` / `icon` 为止。引用（`source_refs`）刻意
    不开放：模型给节点加一条引用就必须逐字匹配原文，而这一步在生成路径上已经由
    `validate_generated_content` 做过了；编辑路径再开一个入口，等于多一条能凭空
    造引用的路。契约层的 `UpdateChanges` 保留了 `source_refs`（导入与原生编辑要
    它），**这一层才是模型看到的那一层**——少一个字段，模型连"能不能改"都不用猜。
    """

    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, min_length=1, max_length=200)
    detail: str | None = Field(default=None, max_length=MAX_NODE_DETAIL_CHARS)
    color: str | None = Field(default=None, max_length=20)
    icon: str | None = Field(default=None, max_length=MAX_NODE_ICON_CHARS)


class EditOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["add", "update", "remove", "move"]
    id: str = Field(min_length=1, max_length=100)
    parent_id: str | None = Field(default=None, max_length=100)
    new_parent_id: str | None = Field(default=None, max_length=100)
    index: int | None = Field(default=None, ge=0)
    label: str | None = Field(default=None, max_length=200)
    changes: EditChange | None = None
    # `add` 用的三个字段，也接受写在顶层供 `update` 使用（`_edit` 会把它们收进
    # `changes`）：实测模型更愿意把要改的字段平铺在 op 上而不是塞进 `changes`，
    # 那种写法被拒之后它下一轮才改得对——但要多花一次调用和几秒。
    detail: str | None = Field(default=None, max_length=MAX_NODE_DETAIL_CHARS)
    color: str | None = Field(default=None, max_length=20)
    icon: str | None = Field(default=None, max_length=MAX_NODE_ICON_CHARS)


class EditArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ops: list[EditOp] = Field(min_length=1, max_length=100)


class AddNodeArgs(BaseModel):
    """`add_node`：在父节点下加**一个**子节点。"""

    model_config = ConfigDict(extra="forbid")
    parent_id: str = Field(min_length=1, max_length=100, description="父节点 id，取自当前大纲")
    label: str = Field(
        min_length=1, max_length=200,
        description=f"新节点的标题，一句话短语，建议不超过 {GENERATED_LABEL_MAX_CHARS} 字，不带句号",
    )
    detail: str | None = Field(
        default=None, max_length=MAX_NODE_DETAIL_CHARS,
        description="可选：这个节点的 2–3 句说明，长内容放这里而不是塞进 label",
    )
    index: int | None = Field(
        default=None, ge=0,
        description="可选：插到父节点下的第几位（0 起）；省略＝追加到末尾",
    )
    color: str | None = Field(
        default=None, max_length=20,
        description=f"可选：语义色，只能取 {'/'.join(NODE_COLORS)} 之一",
    )
    icon: str | None = Field(
        default=None, max_length=MAX_NODE_ICON_CHARS,
        description="可选：一个 emoji 图标（一级分支才需要）",
    )

    @field_validator("color")
    @classmethod
    def color_in_palette(cls, value: str | None) -> str | None:
        return _check_color_arg(value)

    @field_validator("icon")
    @classmethod
    def icon_is_emoji(cls, value: str | None) -> str | None:
        return _check_icon_arg(value)


class UpdateNodeArgs(BaseModel):
    """`update_node`：改一个节点的字段（至少给一个）。

    没有 `source_refs`：引用只由生成路径写（理由见 `EditChange` 的 docstring）。
    这个"没有"是刻意的——参数模型是 `extra="forbid"`，模型硬塞一个
    `source_refs` 会被判参数不合法，而不是被静默忽略。
    """

    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(min_length=1, max_length=100, description="要改的节点 id")
    label: str | None = Field(default=None, min_length=1, max_length=200, description="新的标题")
    detail: str | None = Field(
        default=None, max_length=MAX_NODE_DETAIL_CHARS,
        description="新的说明；传空串表示清空详情",
    )
    color: str | None = Field(
        default=None, max_length=20,
        description=f"新的语义色（{'/'.join(NODE_COLORS)} 之一）；传空串表示去掉颜色",
    )
    icon: str | None = Field(
        default=None, max_length=MAX_NODE_ICON_CHARS,
        description="新的 emoji 图标；传空串表示去掉图标",
    )

    @field_validator("color")
    @classmethod
    def color_in_palette(cls, value: str | None) -> str | None:
        return _check_color_arg(value)

    @field_validator("icon")
    @classmethod
    def icon_is_emoji(cls, value: str | None) -> str | None:
        return _check_icon_arg(value)


class RemoveNodeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(min_length=1, max_length=100, description="要删的节点 id（根节点不可删）")


class MoveNodeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(min_length=1, max_length=100, description="要移动的节点 id")
    new_parent_id: str = Field(min_length=1, max_length=100, description="新的父节点 id")
    index: int | None = Field(
        default=None, ge=0,
        description="可选：在新父节点下的第几位（0 起）；省略＝追加到末尾",
    )


class AddEdgeArgs(BaseModel):
    """`add_edge`：在两个**已有**节点之间连一条附加边（流程图的条件分支 / 回退）。

    三个字段的取值全部来自当前大纲：`source` / `target` 是节点 id（不是标题），
    `kind` 只收三个值。之所以不给"按标题找节点"的方便写法：标题在树里可以重复，
    而按重复的标题找节点会连错线——那是一种在画布上看起来"就是我要的"，但节点
    根本不是同一个的静默错误。
    """

    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=100, description="起点节点 id，取自当前大纲")
    target: str = Field(min_length=1, max_length=100, description="终点节点 id，取自当前大纲")
    label: str | None = Field(
        default=None, max_length=MAX_EDGE_LABEL_CHARS,
        description="可选：边上的文字，例如「是」「否」「通过」「驳回」",
    )
    kind: str | None = Field(
        default=None, max_length=20,
        description="可选：flow 顺序跳转 / branch 条件出口 / loop 回退；省略等于 flow",
    )

    @field_validator("kind")
    @classmethod
    def _kind_in_set(cls, value: str | None) -> str | None:
        """非法 `kind` 在**参数层**就报错（与 `color` / `icon` 同一个位置）。

        放在这里而不是执行器里：执行器的错误会带上 `ops.0.target` 这种路径，
        对模型来说那句 `ops.0` 毫无信息量——它只知道"我调的是 add_edge"。
        """
        return _check_kind_arg(value)


class RemoveEdgeArgs(BaseModel):
    """`remove_edge`：删一条附加边（不动节点）。"""

    model_config = ConfigDict(extra="forbid")
    edge_id: str = Field(min_length=1, max_length=100, description="边 id，取自当前大纲里的附加关系")


# ---------------------------------------------------------------- 辅助


def _count_nodes(root) -> int:
    return 1 + sum(_count_nodes(child) for child in root.children)


def _take_nodes(node: Node, budget: int) -> tuple[Node | None, int]:
    """保留 `node` 及其子树的前 `budget` 个节点（前序），返回 `(节点, 实际保留数)`。

    `_clip_by_branch` 的支内裁剪器。与 `_clip_tree` 的递归是同一套前序，只是**名额
    由调用方给**——一支一个额度，用完就停，不去借邻支的。
    """
    if budget <= 0:
        return None, 0
    kept: list[Node] = []
    used = 1
    for child in node.children:
        if used >= budget:
            break
        sub, taken = _take_nodes(child, budget - used)
        if sub is not None:
            kept.append(sub)
            used += taken
    return node.model_copy(update={"children": kept}), used


def _clip_tree(
    content: MindMapContent, limit: int, *, per_branch: int | None = None,
) -> tuple[MindMapContent, int]:
    """前序保留前 `limit` 个节点，返回 `(裁剪后的内容, 丢掉的节点数)`。

    **生产路径上已经没有人调它了**（`_generate` 不再裁，`_generate_deep` 走
    `_node_limit` 与 `NODE_BUDGET_CUT`，也不走这里）。留着它 + `tests/test_branch_budget.py`
    里那几条直接打它的用例，是因为"为什么不该按前序砍尾巴"这件事需要一个**可执行的
    反面证据**，而文字说明会被后来的人当成一段历史轶事：同一个 61 节点的树、
    `limit=10`，裁完只剩 1 支——这就是它退休的理由。

    **前序**是刻意的：父节点一定排在它自己的子节点前面，所以裁出来的仍是一棵合法
    的树（不会出现"有子无父"）。代价是"一层的末尾被切掉"——第 80 个节点之后的分支
    整支消失，而不是每个分支均匀地少一点。均匀裁剪反而更难解释：用户会看到每个分支
    都缺一两个，说不出"哪里不完整"。

    但**纯前序在配额很小时不是"切掉末尾"，是"后几条分支整条消失"**：名额先被第 1 支
    的深子树吃光，第 2 支起一个节点都不剩。实测（`docs/GRAPH-SCALE.md` 第 7 节）一份
    424 字材料、骨架 3 支、`limit=10`：模型写了 39 个节点、3 支齐全，裁完只剩 1 支
    ——用户显式选了 6 条分支，拿到 1 条。所以 `per_branch` 给定时改成**按支配额**：

        root 占一个，剩下的名额按支均分，每支最多 `per_branch` 个（含分支自己），
        支内仍按前序。

    这样名额不够时缩的是**每一支的深度**，不是分支数。`per_branch=None` 是纯前序，
    也就是上面那个"6 支变 1 支"的形态——两条都在用例里钉着。

    裁剪之后要重新走一遍契约校验（`model_validate`）：`relations` 可能指向被裁掉的
    节点，`source_usage` 也不会自己变。**先裁再校验**，校验失败仍然如实报错——一次
    "悄悄留着一棵树但存不进去"的失败比解析失败难查得多。
    """
    total = _count_nodes(content.root)
    if total <= limit:
        return content, 0

    if per_branch is None:
        remaining = [max(0, limit)]

        def walk(node: Node) -> Node:
            remaining[0] -= 1
            kept: list[Node] = []
            for child in node.children:
                if remaining[0] <= 0:
                    break
                kept.append(walk(child))
            return node.model_copy(update={"children": kept})

        root = walk(content.root)
    else:
        remaining = max(0, limit - 1)
        kept_branches: list[Node] = []
        for child in content.root.children:
            if remaining <= 0:
                break
            kept_child, used = _take_nodes(child, min(per_branch, remaining))
            if kept_child is not None:
                kept_branches.append(kept_child)
            remaining -= used
        root = content.root.model_copy(update={"children": kept_branches})
    kept_ids = _node_ids(root)
    relations = [item for item in content.relations
                 if item.source in kept_ids and item.target in kept_ids]
    # 附加边同一套：端点被裁掉的边留着就是 dangling，而 dangling 在契约层是**硬拒**
    # ——不在这里一起裁，"少几个节点"会变成"整张图存不回去"，错误还指向某条边。
    edges = [edge for edge in content.edges
             if edge.source in kept_ids and edge.target in kept_ids]
    trimmed = content.model_copy(
        update={"root": root, "relations": relations, "edges": edges})
    # 走一次 dump/validate：`model_copy` 不做校验，而这里是"结构被代码改过"的唯一
    # 一处，不校验就等于相信自己的算术。
    trimmed = MindMapContent.model_validate(trimmed.model_dump())
    return trimmed, total - _count_nodes(trimmed.root)


# ------------------------------------------------- 超出可读性：不折、不裁，只报数
#
# 这一段以前是 `_fold_tree`：超过 `INITIAL_MAX_NODES`（80）就把某个父节点下多出来的
# 兄弟并成一个「其他 N 项」节点——label 报个数、detail 列被并的标题、引用取被并子树
# 的并集，三档 cap 阶梯 8 → 4 → 2，都折不动才落 `_clip_tree`。它在真实材料上的形态
# 不是"稍微窄一点"，而是**把一级分支整条抹掉**。三次真实生成（无材料、外部检索关）
# 的同一棵树上，"折"与"不折"的对照是：
#
#     85 节点 / 8 条一级分支  →  47 / 5 支   一个「其他 4 项」吞掉 38 个节点
#     88 节点 / 7 支          →  54 / 5 支   「其他 3 项」，吞掉 34 个
#     98 节点 / 9 支          →  49 / 5 支   「其他 5 项」，吞掉 49 个（一半的树）
#
# 起因是"折到装得下为止"：超出量只有 5–18 个，而 cap=4 那一趟在根上做的第一件事就是
# 把后 4 条分支整体收进一个节点。更要紧的是它**不可逆**——被折掉的子树已经不在树里，
# `node_expand` 走的是模型重新生成，拿不回原来那几个；「其他 3 项」这个 label 也不说
# 是哪三项（清单在 `detail` 里，得先点开才知道）。
#
# 所以现在**不折也不裁**：树上有什么就交什么。可读性是渲染层的事（缩放、平移，以及
# 将来画布上的折叠），不是内容层可以替用户决定的事；超限只在诊断里报一句实数，让
# "这棵树有点大"看得见。三条替代路径（塞进契约的某个字段 / 前端默认收起 / 后端另存
# 一棵被折的子树）各自卡在哪，见 `docs/FOLD-RETIRED.md`。
#
# 承载能力不是这里的事：越过**契约上限** `MAX_TREE_NODES`(1000) 的产物在 `_validate`
# 那一步就被整棵树拒了（`invalid_map`），走不到这一段。所以这条路上没有任何"裁"的
# 入口——`_clip_tree` 也因此**失去了生产调用方**（详见它的 docstring）。真实生成的
# 规模实测是 85–157 个节点，本机库里最大的一棵树 561。


def _count_refs(root) -> int:
    """整棵树的引用条数。逐字校验前后各数一次，差值就是被剔除的条数。"""
    return len(root.source_refs) + sum(_count_refs(child) for child in root.children)


def _pick_sources(ctx: RunContext, source_ids: list[str]) -> list:
    """只允许缩小到本次请求已带的来源。

    模型不能凭空引用一个不在 `ctx.sources_by_id` 里的 id，否则它给出的引用
    在界面上点不开——那不是"引用不准"，是引用不存在。
    """
    if not source_ids:
        return list(ctx.request.sources)
    picked = [ctx.sources_by_id[sid] for sid in source_ids if sid in ctx.sources_by_id]
    return picked


def _fail(code: str, message: str, **extra: Any) -> ToolResult:
    return ToolResult(ok=False, content=message, data={"code": code, **extra})


def _reason_text(exc: BaseException, limit: int = 200) -> str:
    """异常的可读摘要。有些异常（如 TimeoutError）没有 message，只用类名也比空串可读。"""
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return text[:limit]


def _note_truncation(diagnostics: list[Diagnostic], raw: Any) -> bool:
    """模型输出被长度上限截断、但回收出了完整节点时，记一条告警。

    返回"这次被截断了吗"。截断本身**不再是失败**（`SalvagedOutput` 已经带着
    回收到的节点回来了），但它必须可见：用户拿到的是一棵少了尾巴的树，而工具
    返回的 `ok=true` 与"完整成功"长得一模一样。没有这条告警，唯一的信号是
    "节点数比预期少几个"——那件事没人能判断。

    只在 `raw` 是 `SalvagedOutput` 时触发（`partial` 属性由那一类显式声明），
    普通 dict 与旧的 mock 一律不触发，这条路径对既有行为零影响。
    """
    if not getattr(raw, "partial", False):
        return False
    recovered = getattr(raw, "recovered", 0)
    diagnostics.append(Diagnostic(
        level=DiagnosticLevel.warn, code="OUTPUT_TRUNCATED_RECOVERED",
        message=(
            f"模型输出撞上长度上限被截断，已从被切断的 JSON 里回收 {recovered} 个完整节点；"
            "这一批的后续内容缺失，整棵树可能比预期浅一层"
        ),
    ))
    return True


def _max_tokens(ctx: RunContext) -> int:
    """工具内一次模型调用的输出上限。

    不显式传 `max_tokens` 时 `chat_async` 会回落到它自己的默认值，于是
    `RunBudget.max_output_tokens` 只对 provider 那条路生效——预算里写了上限，
    工具这条更大的输出口却没人守。

    夹的是 `max_tokens_cap()` 而不是字面常量：那个函数把
    `MINDMAP_LLM_MAX_TOKENS` 与服务端 400 里学到的上限取小（见它的 docstring）。
    qwen 系列只认 32768 时，配一个环境变量就能让这条出口跟着降下来。

    再夹一次免费档的 16000（`apply_free_tier_ceiling`）：不发请求的地方也读这个
    数，用它反推"这次装得下几个节点"。少这一道时免费档会按 65536 规划批次，而
    实际只发 16000，规划出的节点数根本写不完。
    """
    return apply_free_tier_ceiling(
        min(ctx.request.budget.max_output_tokens, max_tokens_cap()))


async def _chat(
    ctx: RunContext,
    system: str,
    user: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """工具内部**唯一**的模型出口：`chat_async` + 这一次的 `deep_thinking`。

    `deep_thinking` 不在调用点各传一遍，而是从 `ctx.request` 取。理由是这条链上
    有六个调用点（骨架 / 结构 / 说明的两半 / 分片展开 / node_expand），逐个手传的
    结果已经出现过：开关只换了外层工具循环的模型，六个点里一个都没换，而
    「深度思考」的承诺是整次生成更聪明。

    放在 Context 上取而不是加全局开关：两次并发请求各勾各的，环境变量式的开关会
    让先到的那个决定后到的那个用哪个模型。

    `tests/test_model_exit_single_path.py` 用 AST 钉着"tools.py 里除了这个函数
    没有别处直接调 `chat_async`"，新增调用点忘记走它也过不了 CI。

    **不勾时不把这个参数发下去**（而不是发 `deep_thinking=False`）：`chat_async`
    的缺省值就是 `False`，少传一次等于"与加这个开关之前逐字节相同"，既有那些
    按脚本回放的假出口（`chat(system, user, *, max_tokens=None, on_delta=None)`）
    一行都不用改。
    """
    if ctx.request.deep_thinking:
        kwargs["deep_thinking"] = True
    return await chat_async(system, user, **kwargs)


# 一次调用除节点之外的输入开销估计（user 消息 + 证据数组 + 系统提示词）。
# 数值取实测上界：分片展开的 user 含 3 条父块（每条 450–700 字）+ 路径 + 已有
# 要点，约 3000 字 ≈ 2000 token；系统提示词（含思维模型块）约 4000 字 ≈ 2700
# token。取 12000 是给"材料很长、路径很深"那种情况留的余量。
_INPUT_HEADROOM_TOKENS = 12_000


def _nodes_per_call(ctx: RunContext, *, system: str = "") -> int:
    """这一次调用最多能要几个"满额节点"（见 `generation.budget` 的模块注释）。

    三条边界一起算：`RunBudget.max_output_tokens`、模型输出上限、剩余上下文。
    调用方拿它去夹"这一次要求几个节点"，于是"prompt 要 100 个、单次输出 40 个
    就截断"在结构上不可能发生——要求量在发请求之前就已经被夹住了。
    """
    available = budget.available_output_tokens(
        _max_tokens(ctx),
        model_cap=max_tokens_cap(),
        context_left=max(
            0,
            CONTEXT_TOKEN_LIMIT - estimate_input_tokens(system) - _INPUT_HEADROOM_TOKENS,
        ),
    )
    return budget.max_nodes_for_tokens(available)


def _require_candidate(ctx: RunContext) -> DiagramContent | ToolResult:
    if ctx.candidate is None:
        return _fail("no_candidate", "当前还没有导图，请先调用 mindmap_generate 生成。")
    return ctx.candidate


def _has_content_nodes(content: DiagramContent) -> bool:
    """这份产物里有没有实质内容（"要不要报 `map_exists`"的判据）。

    写成"有没有内容节点"而不是"root 有没有孩子"：鱼骨与流程的产物里**没有
    root**，而它们当然也是"已经有一张图了"。空产物（只有标题、一根骨都没有）
    仍然算没有内容——那时重新生成一次是用户想要的事。
    """
    return any(True for _node in shapes.iter_nodes(content))


def _node_ids(root) -> set[str]:
    seen: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        seen.add(node.id)
        stack.extend(node.children)
    return seen


@dataclass(frozen=True)
class _SkeletonBranch:
    """骨架里的一条一级分支：标题 + 它的视觉元素。

    `color` / `icon` 是**只属于一级分支**的标记（见 `PLAN_PROMPT`）：一棵 80 节点
    的树如果每个节点都有图标，图标就不再指示任何东西，只是把画布弄花。所以这两个
    字段只由骨架解析产出，展开路径（`_slot_from`）根本不读它们——不读比"读了再
    丢掉"更彻底：代码里没有那条路，模型写多少都不会漏到树上。
    """

    label: str
    color: str | None = None
    icon: str | None = None


def _skeleton_branches(raw: Any) -> list[_SkeletonBranch]:
    """从骨架响应里取出分支（标题 + 可选 color/icon），剔除空项，标题截到 16 字。

    两种形状都收，因为它们在真实运行里都会出现：

      - `{"branches":[{"label":"甲","color":"blue","icon":"🎯"}]}`：要视觉元素时的格式；
      - `{"branches":["甲","乙"]}`：纯字符串。老模型、老 mock、以及"模型没照格式写"
        都会退化成它，硬拒等于把一次已经花掉的骨架调用判死。

    只接受字符串或带 `label` 的 dict：`{"branches":[{"name":"x"}]}` 说明模型没照
    格式写，硬转字符串会把一整个 dict 的 repr 当成节点标题挂到树上——那种"成功"
    比失败更难发现。

    截断长度是 `GENERATED_LABEL_MAX_CHARS`（16）：骨架标题就是画布上的一级分支，
    它与其余节点受同一条限制。**不做二次校验**——模型偶尔写长一点不该判整次失败，
    截断后仍是一棵合法的树。

    `color` / `icon` 走**归一化而不是拒绝**（`normalize_color` / `normalize_icon`）：
    一个 `color:"深蓝"` 说明模型没照提示词写，但它产出的结构本身是好的——
    为一个装饰字段把整次骨架判失败，代价方向完全错了。
    """
    if not isinstance(raw, dict):
        return []
    items = raw.get("branches")
    if not isinstance(items, list):
        return []
    branches: list[_SkeletonBranch] = []
    for item in items:
        if isinstance(item, str):
            label = clip_label(item.strip())
            if label:
                branches.append(_SkeletonBranch(label))
            continue
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        if not isinstance(label, str):
            continue
        title = clip_label(label.strip())
        if not title:
            continue
        branches.append(_SkeletonBranch(
            title,
            color=normalize_color(item.get("color")),
            icon=normalize_icon(item.get("icon")),
        ))
    return branches


def _skeleton_titles(raw: Any) -> list[str]:
    """只要标题时的便捷入口（诊断、用例与旧调用方读它）。"""
    return [branch.label for branch in _skeleton_branches(raw)]


def _skeleton_problem(raw: Any) -> str | None:
    """鱼骨图骨架里的 `problem`：主骨上那句「为什么 X 会 Y」。

    只接受**字符串**，折空白后按 `MAX_FISHBONE_PROBLEM_CHARS` 截断；其余一律
    `None`。不在这里判"像不像一句主骨陈述"——那是实测要回答的问题（`docs/` 里
    那份对比表），而代码在生成路径上做语义判断的后果是："模型写得不够好"被报成
    "这次生成失败"。

    `None` 与"空串"对调用方是同一件事：这一版没有论点。兜底与诊断在 `_generate`
    的出口那一段（`_fishbone_problem`），因为那里才知道 title 是什么。
    """
    if not isinstance(raw, dict):
        return None
    text = raw.get("problem")
    if not isinstance(text, str):
        return None
    folded = " ".join(text.split())
    if not folded:
        return None
    return folded[:MAX_FISHBONE_PROBLEM_CHARS]


@dataclass(frozen=True)
class _SkeletonGroup:
    """两级骨架里的一个分组：组名 + 它的视觉元素 + 它下面的分支。

    `color` / `icon` 在两级的形状里**只属于分组**（它就是画布上的一级节点）：
    二级分支不写这两个字段，它们的颜色由前端沿父链继承（`frontend/src/stores/
    mindmap.ts` 里 `inheritedColor` 那一段）。这不是省事，是"颜色 = 分组"这条
    唯一约定的前提——二级各带一个色，一眼就分不出哪几支是同一组。
    """

    label: str
    color: str | None = None
    icon: str | None = None
    branches: tuple[_SkeletonBranch, ...] = ()


@dataclass(frozen=True)
class _SkeletonPlan:
    """骨架的**最终形态**：分两级时 `groups` 非空，否则 `flat` 就是一级分支。

    `flat` 恒等于"要展开的东西"：两级时是二十节（二级），扁平时是五条（一级）。
    检索、证据分组、detail 全按它走——**组只出现在树的形状里，不出现在任何一次
    调用里**（见 `_generate` 里那段"组节点不吃调用"的注释）。
    """

    flat: tuple[_SkeletonBranch, ...] = ()
    groups: tuple[_SkeletonGroup, ...] = ()

    @property
    def grouped(self) -> bool:
        return bool(self.groups)


def _skeleton_key(text: str) -> str:
    """比对用的归一：只折空白，与小写无关（中文没有大小写）。

    与 `_cross_branch_duplicates` 的判据同源——同一个说法在两处的归一必须一致，
    否则"骨架说有这一支、树里认不出它"会变成一次静默的结构错位。
    """
    return re.sub(r"\s+", "", text or "")


def _skeleton_groups(raw: Any) -> list[_SkeletonGroup]:
    """从骨架响应里取出**两级**形状：`{"groups":[{"label":…,"children":[…]}]}`。

    只认这一种嵌套形状（`children` 里放二级分支），因为语义最直接：一次就能看清
    哪几支属于哪一组。三种情况都退化成"没给分组"，而不是失败：

      - 没有 `groups` 键（模型照老格式写了 `branches`）——老形状见 `_skeleton_branches`；
      - `groups` 不是数组 / 全是空组；
      - 某组的 `children` 不是数组或全是空项——那一组没有内容可承载，整组丢掉。

    与 `_skeleton_branches` 同一条规矩：只接受字符串或带 `label` 的 dict，`color` /
    `icon` 走归一化而不是拒绝（一个 `color:"深蓝"` 说明模型没照提示词写，但结构是好的，
    为一个装饰字段把整次骨架判失败，代价方向完全错了）。
    """
    if not isinstance(raw, dict):
        return []
    items = raw.get("groups")
    if not isinstance(items, list):
        return []
    groups: list[_SkeletonGroup] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        if not isinstance(label, str):
            continue
        title = clip_label(label.strip())
        if not title:
            continue
        children = item.get("children")
        branches: list[_SkeletonBranch] = []
        if isinstance(children, list):
            for child in children:
                if isinstance(child, str):
                    child_label = clip_label(child.strip())
                    if child_label:
                        branches.append(_SkeletonBranch(child_label))
                    continue
                if not isinstance(child, dict):
                    continue
                child_label = child.get("label")
                if not isinstance(child_label, str):
                    continue
                child_title = clip_label(child_label.strip())
                if not child_title:
                    continue
                # 二级的 color/icon 照收但**不用**（见 `_SkeletonGroup`）：收下来只是
                # 为了让"模型写了什么"有地方落，不在树上落。
                branches.append(_SkeletonBranch(
                    child_title,
                    color=normalize_color(child.get("color")),
                    icon=normalize_icon(child.get("icon")),
                ))
        if not branches:
            continue
        groups.append(_SkeletonGroup(
            title,
            color=normalize_color(item.get("color")),
            icon=normalize_icon(item.get("icon")),
            branches=tuple(branches),
        ))
    return groups


def _skeleton_group_count(branches: int) -> int:
    """这次该归成几组（M）：按每组 `SKELETON_GROUP_TARGET` 支倒推，夹在 3–6 之间。

    11 支 → 3 组（4/4/3）、13 → 4、17 → 5、20 → 5（4×5）。上界 20（`DEEP_MAX_BRANCHES`）
    时 M 恰好落在 5，与 `SKELETON_GROUP_MAX` 还有一格余量，所以这个夹取只在模型
    自作主张给到 21 支以上时才起作用。
    """
    if branches <= 0:
        return SKELETON_GROUP_MIN
    return max(SKELETON_GROUP_MIN, min(
        SKELETON_GROUP_MAX, -(-branches // SKELETON_GROUP_TARGET)))


def _skeleton_plan(raw: Any) -> _SkeletonPlan:
    """骨架响应 → 最终形态（扁平 / 两级），**并在这里决定要不要分两级**。

    判据只有一条：**要展开的东西超过 `SKELETON_GROUP_THRESHOLD` 条**。所以
    "模型给了分组但只有 6 支"会被压平（6 条本来就是可读的一行，多一层反而多一次点击），
    而"没给分组但有 20 支"保持扁平——那是老形状的兼容路径：**退化成不分两级，而不是
    失败**（骨架已经花掉了，硬拒等于把一次调用判死，与 `_skeleton_branches` 收两种
    形状是同一条理由）。
    """
    groups = _skeleton_groups(raw)
    if groups:
        # 组数安全阀：超过 `SKELETON_GROUP_HARD_MAX` 的整组丢掉（含它的分支）。
        groups = groups[:SKELETON_GROUP_HARD_MAX]
        flat = [branch for group in groups for branch in group.branches]
        # 只有一组时不分级：根 → 组 → 二十支 与 根 → 二十支 读起来没有区别，
        # 而前者多一层、多一个没有内容的节点。
        if len(flat) > SKELETON_GROUP_THRESHOLD and len(groups) > 1:
            return _SkeletonPlan(flat=tuple(flat), groups=tuple(groups))
        # 分支太少（或只有一组）：分组没有收益，压平（组名只是分类名，压平时丢掉即可）。
        return _SkeletonPlan(flat=tuple(flat))
    return _SkeletonPlan(flat=tuple(_skeleton_branches(raw)))


def _cap_plan(plan: _SkeletonPlan) -> _SkeletonPlan:
    """把总量截到契约允许的规模：二级分支合计不超过 `DEEP_MAX_BRANCHES`。

    截断按**组的顺序**走（第一组先占），与 `branches[:DEEP_MAX_BRANCHES]` 的语义相同：
    模型无视上限时保留前 N 条，把尾巴丢掉。两级形状下截断之后可能出现只有 0-1 支的
    组——1 支的组留着（它仍是这一支的归类），0 支的组丢掉（挂上去就是一个空节点）。

    顺带做**同名分支的去重**（只认第一次出现的那一组）：同一支若同时列在两个组下，
    骨架这一层就已经自相矛盾了——落到树上要么出现两次（跨支重复），要么按我们挑的
    那一组归位（模型的另一半意思被静默丢掉）。两种都比"只保留一处、并让它归第一个
    提到它的组"差，而后者正是这里做的。判据与 `_cross_branch_duplicates` 同源
    （只折空白），所以"骨架去掉了哪一条"与"树上判定重复"不会各说一套。
    """
    if not plan.grouped:
        return _SkeletonPlan(
            flat=tuple(plan.flat[:min(MAX_CHILDREN, DEEP_MAX_BRANCHES)]))
    remaining = DEEP_MAX_BRANCHES
    seen: set[str] = set()
    groups: list[_SkeletonGroup] = []
    for group in plan.groups:
        if remaining <= 0:
            break
        kept: list[_SkeletonBranch] = []
        for branch in group.branches:
            if len(kept) >= remaining:
                break
            key = _skeleton_key(branch.label)
            if not key or key in seen:
                continue
            seen.add(key)
            kept.append(branch)
        if not kept:
            continue
        remaining -= len(kept)
        groups.append(replace(group, branches=tuple(kept)))
    if len(groups) < 2:
        # 截到只剩一组（或一组都没剩）：退回扁平，别为一个分类名多插一层。
        return _SkeletonPlan(
            flat=tuple(branch for group in groups for branch in group.branches))
    return _SkeletonPlan(
        flat=tuple(branch for group in groups for branch in group.branches),
        groups=tuple(groups),
    )


def _fill_branch_colors(branches: Sequence[_SkeletonBranch]) -> list[_SkeletonBranch]:
    """给没写 `color` 的一级分支补一个调色板色：**按调色板顺序，跳过已被占用的**。

    提示词一直写着"建议每条分支都给"，而那是建议而不是契约——真实响应里模型
    经常一条都不给（主题越抽象越明显）。缺色的后果不是"难看"这么轻：一级颜色
    唯一的用途就是"按分支分组"（一级分支一个色、它的后代继承，见
    `frontend/src/stores/mindmap.ts` 里沿父链找色那一段），一条都没有时整张图
    退回中性灰，用户看不出哪几块是同一支。

    **为什么不硬校验、不重来一次**：为一个装饰字段把一次已经花掉的骨架调用连同
    整棵树判废，代价方向完全错（与 `_skeleton_branches` 对非法 color 的处理是
    同一条理由：归一化而不是拒绝）。缺了就补，比要求模型一次说对便宜得多。

    分配是确定的，不是随机的：
      - 池子是**模型没占用**的调色板色，顺序即 `NODE_COLORS` 的顺序；
      - 需要补的分支按骨架顺序依次取，取完从头循环（分支最多 20 条、调色板 8 色，
        必然出现重色；但要走到循环说明 8 种全被模型用了，那时撞不撞不由这里决定）。
      - 跳过已占用色是关键：只按序号取 `NODE_COLORS[i]` 时，模型给第 0 支写了
        `blue`、第 4 支空着正好也轮到 `blue`——两条一级分支同色，"颜色=分组"
        这个唯一约定当场失效。

    纯函数：同一条骨架响应永远得到同一组颜色（用例钉着），不做任何跨请求记忆。
    """
    return _palette_fill(branches)


class _Visual(Protocol):
    """带一级视觉元素的形状：`_SkeletonBranch` 与 `_SkeletonGroup` 都满足它。

    只声明 `_palette_fill` 真正读写的三个字段，不描述两者其余的差别
    （分组还有 `branches`）——协议窄一点，将来第四个视觉形状加进来时才不会被
    一个过宽的接口绑住。
    """

    label: str
    color: str | None
    icon: str | None


_VT = TypeVar("_VT", bound=_Visual)


def _palette_fill(items: Sequence[_VT]) -> list[_VT]:
    """给缺 `color` 的项补调色板色（按顺序取、跳过已被占用的）。

    两级骨架之后有两个调用方（一级分支 / 一级分组），算法只有一份：两边各写一遍时，
    "跳过已占用色"这条一改一处就会漏一处，而漏掉的那一边表现为**两条同级同色**——
    "颜色 = 分组"这个唯一约定当场失效，且从产出上看不出来。

    泛化而不是两次复制：`replace()` 要求同一个 dataclass，而 `_SkeletonBranch` 与
    `_SkeletonGroup` 的视觉三字段一模一样（`label` / `color` / `icon`），差别只在
    后者多一个 `branches`。
    """
    missing = [index for index, item in enumerate(items) if not item.color]
    if not missing:
        return list(items)
    taken = {item.color for item in items if item.color}
    pool = [color for color in NODE_COLORS if color not in taken] or list(NODE_COLORS)
    filled = list(items)
    for position, index in enumerate(missing):
        filled[index] = replace(filled[index], color=pool[position % len(pool)])
    return filled


def _fill_group_colors(groups: Sequence[_SkeletonGroup]) -> list[_SkeletonGroup]:
    """给没写 `color` 的分组补色。与 `_fill_branch_colors` 同一条算法、同一份池子。

    分组是**真正的一级节点**，所以"缺色就补"的理由与那条路完全一样（一级颜色唯一的
    用途就是分组，一条都没有时整张图退回中性灰）。位置也一致：在截断**之后**补，
    被丢掉的尾巴不参与配色。
    """
    return _palette_fill(groups)


def _fill_plan_colors(plan: _SkeletonPlan) -> _SkeletonPlan:
    """按形态补色：两级补**分组**，扁平补一级分支。

    为什么不分两次调用、也不在调用方判分支：配色只有一条规则（"一级才有色"），
    而"一级"在两种形态里是不同的东西。把这个判断收在一处，调用方就只需要
    `plan = _fill_plan_colors(_cap_plan(_skeleton_plan(raw)))` 一行。
    """
    if plan.grouped:
        return replace(plan, groups=tuple(_fill_group_colors(plan.groups)))
    return replace(plan, flat=tuple(_fill_branch_colors(plan.flat)))


def _strip_plan_icons(plan: _SkeletonPlan) -> _SkeletonPlan:
    """`icon.enabled=false` 时剥掉一级（分组或分支）的 emoji。

    挂在"骨架产物"这一层而不是树上：`branch_visual` 的旧位置就是这个意思——视觉元素
    唯一落进树的地方只有一处，剥在这里等于覆盖全部产出（三级骨架将来也一样）。
    二级分支本来就没有 icon（`_SkeletonGroup` 的注释），所以不用管。
    """
    if plan.grouped:
        return replace(plan, groups=tuple(
            replace(group, icon=None) for group in plan.groups))
    return replace(plan, flat=tuple(
        replace(branch, icon=None) for branch in plan.flat))


def _skeleton_edges(
    raw: Any, skeleton: Sequence[tuple[str, str]], root_id: str,
) -> list[Edge]:
    """把骨架响应里的 `edges` 解析成契约边，端点用**序号 / "root" / 分支标题**指定。

    为什么端点不能是节点 id：真实 id 是 `new_node_id` 在服务端分配的，模型在
    写响应时根本不知道它。所以约定三种它能可靠写对的写法：

      - 整数 `0`（或字符串 `"0"`）：一级分支的下标，0 起；
      - `"root"`：中心主题；
      - 分支标题原文（`"形式审查"`）：容忍——模型经常无视"写序号"照自己的习惯
        写标题，为一个端点写法把整批边丢掉不值得。

    三种都对不上就是 dangling：**静默丢掉**（由 `clean_edges` 统一处理并报一条
    聚合的 `EDGE_DANGLING`）。这里不逐条报，因为 `edges` 只是骨架的附加产物，
    一条写错的边不该在用户的诊断列表里占一行。

    `kind` / `label` 的归一：不认识的 kind 落成 `None`（等于 `flow`，前端有默认
    画法），label 按契约截到 40 字。**不为这些字段丢边**——一条"是/否"写成
    `"yes"` 的边仍然表达了那条跳转，丢掉它反而让流程缺一条分支。
    """
    if not isinstance(raw, dict):
        return []
    items = raw.get("edges")
    if not isinstance(items, list):
        return []

    by_index = {index: branch_id for index, (_, branch_id) in enumerate(skeleton)}
    by_label = {title.strip(): branch_id for title, branch_id in skeleton}

    def resolve(value: Any) -> str | None:
        if isinstance(value, bool):  # bool 是 int 的子类，`True` 会撞上 1
            return None
        if isinstance(value, int):
            return by_index.get(value)
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        if text.lower() == "root":
            return root_id
        if text.lstrip("-").isdigit():
            return by_index.get(int(text))
        return by_label.get(text)

    edges: list[Edge] = []
    for ordinal, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        unresolved = UNRESOLVED_ENDPOINT
        source = resolve(item.get("source"))
        target = resolve(item.get("target"))
        if source is None or target is None:
            # 端点解析不出来（序号越界、写了不存在的标题）：换成一个哨兵端点，
            # 交给 `clean_edges` 按 dangling 丢掉并记 `EDGE_DANGLING`。
            #
            # 这里**不能**塞成"指向自己"：自环是被静默丢弃的，那样一来"模型写了
            # 一个不存在的分支序号"在诊断里一个字都不会出现，图默默少一条分支。
            source = target = unresolved
        kind = item.get("kind")
        edges.append(Edge(
            id=f"e_{ordinal}_{uuid4().hex[:12]}",
            source=source,
            target=target,
            label=clip_edge_label(item.get("label")),
            kind=kind if kind in EDGE_KINDS else None,
        ))
    return edges


def clip_edge_label(value: Any) -> str | None:
    """边标签归一：非字符串/空白 → `None`，过长按码点截到契约上限。

    截断而不是丢弃：一条 45 字的标签说明模型没照提示词写，但它表达的跳转是真
    的——把整条边丢掉会让流程图少一个出口，那比文字长了 5 个字严重得多。
    """
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:MAX_EDGE_LABEL_CHARS] if text else None


# ---------------------------------------------------------------- 阶段内的判定出边
#
# 这一段的来历是一次实测：`FlowContent.edges` 的端点是**阶段 id**，而 `role="decision"`
# 的节点在阶段**内部**——模型标出来的判定节点，它的「是 / 否」两条出边在两个数组里
# 都没有位置，于是产出里 `branch` 边恒为 0。契约补了 `FlowStage.edges`（阶段内的边、
# 端点用该阶段的节点 id），这里负责**从模型响应里把这两条边取出来**。
#
# 为什么在**结构那一次**产出、而不是单开一次调用：
#   - 判定节点的两条出口是"这一步走哪条路"，而模型正在写这一步。等它写完一整棵树
#     再问一次"哪两条路去哪"，它得把刚写的东西在上下文里重新读一遍，多花一次完整
#     往返（十几秒 + 全额 token）换一个它此刻已经知道答案的问题；
#   - 骨架那一次问不出来：那一次的响应里只有阶段标题，阶段内部的步骤还不存在；
#   - 代价是结构那一次的提示词再长一段，而 role 那一段本来就在那里——实测
#     `FLOW_TREE_RULES` 写在 system 里时一个 decision 都没标出来，把同一件事写到
#     user 末尾才有效（见 `_generate` 里那段注释），所以这一段也写在那里。


@dataclass(frozen=True)
class _FlowBranch:
    """模型写在判定节点上的两条出口，**还没解析成节点 id**。

    `node_id` 是模型自己写的那个 id（它在同一份响应里自己编的，服务端不重分配），
    `yes` / `no` 是目标的原始写法——可能是标题、可能是 id、可能是「结束」。
    """

    node_id: str
    label: str
    yes: str | None
    no: str | None


# 判定节点的出口写成这些词时表示"流程在这里结束"，不是一个节点。
# 收小写 + 中文两种写法：模型对"结束"的英文写法在 `end` / `finish` / `stop` 之间摆，
# 而它们都表达同一件事，为一个词的选择丢掉一条出口不值得。
_FLOW_END_WORDS = frozenset({"结束", "end", "finish", "stop", "无", "none", "null"})


def _branch_ends(value: Any) -> tuple[str | None, str | None]:
    """`branch` 字段 → `(yes, no)`；认不出的形状返回 `(None, None)`。

    三种写法都收，因为它们表达的是同一个东西：
      - `{"yes": …, "no": …}`（提示词要求的形状）；
      - `{"是": …, "否": …}`（模型把标签当成键）；
      - `["…", "…"]`（两项数组，顺序即 是 / 否）。

    容错的理由与 `_skeleton_edges` 的端点写法同源：这里丢掉的是**整条出口**，
    而它比"字段名写歪了一位"严重得多。
    """
    def text_of(item: Any) -> str | None:
        if isinstance(item, str) and item.strip():
            return item.strip()
        return None

    if isinstance(value, dict):
        for yes_key, no_key in (("yes", "no"), ("是", "否"), ("true", "false"),
                                ("y", "n"), ("通过", "不通过")):
            if yes_key in value or no_key in value:
                return text_of(value.get(yes_key)), text_of(value.get(no_key))
        return None, None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return text_of(value[0]), text_of(value[1])
    return None, None


def _harvest_flow_branches(raw: Any) -> tuple[Any, list[_FlowBranch]]:
    """从结构那一次的响应里摘出判定节点的 `branch`，并从 raw 里**删掉**它。

    为什么必须删：`Node` 是 `extra="forbid"`，留着这个键整棵树会 422，用户看到的
    是"模型返回的导图不合法"——而这条信息本来完全可以解析成一条边。所以摘取发生
    在 `validate_generated_content` **之前**，且只对 `flowchart` 做（树与鱼骨的
    提示词里没有这个字段，多跑一趟只会把"模型乱写的东西"当数据）。

    只认 `role == "decision"` 的节点吗？**不认**。模型偶尔会把 `role` 写漏或写成
    `"判断"`（不在 `Literal` 里会被契约拒，但那时整张图已经失败了），而它写了
    `branch` 这件事本身就是"这一步有两条出口"的充分证据。多收一条边不会画错，
    漏收一条就少一个出口。

    深度优先、就地修改：`raw` 是刚解析出来的 dict，`deepcopy` 在
    `validate_generated_content` 里还会做一次。
    """
    if not isinstance(raw, dict):
        return raw, []
    root = raw.get("root")
    if not isinstance(root, dict):
        return raw, []

    found: list[_FlowBranch] = []
    stack: list[dict[str, Any]] = [root]
    while stack:
        node = stack.pop()
        node_id = node.get("id")
        raw_branch = node.pop("branch", None)
        if raw_branch is not None and isinstance(node_id, str) and node_id.strip():
            yes, no = _branch_ends(raw_branch)
            if yes is not None or no is not None:
                label = node.get("label")
                found.append(_FlowBranch(
                    node_id=node_id.strip(),
                    label=label.strip() if isinstance(label, str) else node_id.strip(),
                    yes=yes, no=no,
                ))
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(item for item in children if isinstance(item, dict))
    return raw, found


def _resolve_branch_target(
    text: str | None, by_id: dict[str, str], by_label: dict[str, str],
) -> str | None:
    """一个出口的原始写法 → 真实节点 id；解析不出来返回 `None`。

    三条写法依次试，与 `_skeleton_edges` 的端点解析同一条思路（"先认它能可靠写对
    的东西"）：**id 优先**（它就是那一份响应里的 id，服务端没有重分配）、标题其次
    （折叠空白后比对）、「结束」直接判无。
    """
    if text is None:
        return None
    candidate = text.strip()
    if not candidate:
        return None
    if candidate in by_id:
        return by_id[candidate]
    key = _skeleton_key(candidate)
    if key.lower() in _FLOW_END_WORDS:
        return None
    return by_label.get(key)


@dataclass
class _BranchEdgeReport:
    """阶段内边的解析结果：成功的边 + 三种没落地的原因各自的条数。"""

    edges: list[Edge] = field(default_factory=list)
    #: 判定节点自己不在树里（被裁剪掉、或模型写了一个没落树的 id）
    lost_sources: int = 0
    #: 出口写了一个对不上的目标（标题打错、指向一个不存在的节点）
    unresolved: int = 0
    #: 出口写成「结束」——不是失败，是流程在这里收尾
    ended: int = 0
    #: 两条出口指向同一个节点：两条边里只留「是」那条（画布上它们是同一条线）
    same_target: int = 0


def _flow_branch_edges(
    branches: Sequence[_FlowBranch], content: MindMapContent,
) -> _BranchEdgeReport:
    """把 `_FlowBranch` 解析成契约边（**树命名空间**，端点是节点 id）。

    为什么端点先用节点 id、到 `shapes.to_flow` 才分流：树上本来就只有一套节点 id，
    而"这条边属于哪个阶段"是**形状转换**才知道的事（`to_flow` 会把节点分回各自
    阶段）。在这里提前判阶段只会多一份"哪些节点属于哪个阶段"的映射。

    **解析不出来就丢，不判失败**：边是结构的附加产物，一条写歪的出口不该把一整棵
    已经长好的树判废（与 `_skeleton_edges` 的 `EDGE_DANGLING` 同一条取舍）。丢掉的
    数量由调用方聚合成一条诊断。
    """
    nodes = list(_walk_tree(content.root))
    by_id = {node.id: node.id for node in nodes}
    # 同名节点**不解析**：一张图里两个「校验信息」很常见，按标题连过去可能连错那一个，
    # 而错误的分支比缺失的分支更难被发现（它看起来完全合法）。
    seen_labels: dict[str, str | None] = {}
    for node in nodes:
        key = _skeleton_key(node.label)
        seen_labels[key] = node.id if key not in seen_labels else None
    by_label = {key: value for key, value in seen_labels.items() if value is not None}

    report = _BranchEdgeReport()
    for ordinal, branch in enumerate(branches):
        source = _resolve_branch_target(branch.node_id, by_id, by_label)
        if source is None:
            report.lost_sources += 1
            continue
        made: dict[str, str] = {}
        for label, raw in (("是", branch.yes), ("否", branch.no)):
            if raw is None:
                continue
            target = _resolve_branch_target(raw, by_id, by_label)
            if target is None:
                if _skeleton_key(raw).lower() in _FLOW_END_WORDS:
                    report.ended += 1
                else:
                    report.unresolved += 1
                continue
            if target == source:
                # 出口指回自己：契约层会按自环拒，而"这一步回到自己"在图上没有语义。
                report.unresolved += 1
                continue
            if target in made.values():
                report.same_target += 1
                continue
            made[label] = target
            report.edges.append(Edge(
                id=f"fb_{ordinal}_{label}_{uuid4().hex[:8]}",
                source=source,
                target=target,
                label=label,
                kind="branch",
            ))
    return report


def _walk_tree(root: Node) -> Iterator[Node]:
    """深度优先遍历一棵子树（含根）。`shapes.walk_nodes` 的树专用版本。"""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.children)


@dataclass(frozen=True)
class _BranchEvidence:
    """一条一级分支自己的检索结果：给模型看的那一段 + 用来筛引用的那几段原文。

    三份内容各有各的用处，不能互推：

      - `payload`：`_citable_hits` 的产物，进提示词（含父块 `quote` 与命中子块的
        `evidence_*`）。模型抄的就是它。
      - `spans`：本支命中的两级区间。判"这条引用是不是本支的证据"用它——按坐标判，
        比按文本判便宜，也避开了"两个分支命中同一段，文字一样"的假阳性。
      - `quotes`：本支命中文本的拼接。只在引用**没有坐标**（模型只抄了 quote）时
        兜底用它做包含判定：那种引用本来就没有区间可比，而"这段文字在不在本支
        检索结果里"仍然是可判的。

    **这一支的节点只能引用这一支的证据**：不这么收口的话，20 支的 hits 会累积成
    一个共享池，模型从别支抄一段也能通过收尾的逐字校验——"按支检索"就退化成了
    "整树检索一次、top_k 大一点"。收口在这里，不在收尾校验里：那一层拿不到
    "这条引用是谁给它的"这个信息。
    """

    payload: list[dict[str, Any]]
    spans: tuple[EvidenceSpan, ...]
    text: str
    quotes: str


def _branch_evidence(hits: list[EvidenceHit], sources: list[Source]) -> _BranchEvidence:
    """命中 → 一支的证据块。空命中不进这里（调用方用 `None` 表示"这一支没有"）。"""
    payload = _citable_hits(hits, sources)
    quotes = "\n".join(
        part for entry in payload
        for part in (entry.get("quote"), entry.get("evidence_quote"))
        if isinstance(part, str) and part
    )
    return _BranchEvidence(
        payload=payload,
        spans=tuple(
            EvidenceSpan(hit.source_id, hit.start, hit.end, hit.leaf_start, hit.leaf_end)
            for hit in hits
        ),
        text=json.dumps(payload, ensure_ascii=False),
        quotes=quotes,
    )


def _branch_query(title: str) -> str:
    """一支的检索问句：**只发分支标题**，不拼主题。

    这个拼法是实测选出来的，不是拍的（`scripts/measure_deep_slicing.py
    --query-mode {title,topic-title,topic}`，`docs/RAG-DECISIONS.md` 12,392 字、
    8 支、纯 BM25，三组消融）：

      | 拼法 | 覆盖率 | 8 支共用 top-1 | 两两 Jaccard | 并集段落 |
      |---|---|---|---|---|
      | 主题 + 标题 | 19.2% | 8/8 | 0.32 | 9/52 |
      | **只发标题** | **28.8%** | 3/8 | 0.13 | 13/52 |
      | 只发主题 | 3.8% | 8/8 | 1.00 | 2/52 |

    主题串为什么有害：它出现在文档标题行/摘要/图例里（在那份材料里是 `# RAG 与召回…`
    那一块），BM25 里这些词 IDF 高，于是**每一支的 top-1 都被拉回同一个父块**——
    8 支各查一次，实际只查到 1 处。去掉主题之后，分支标题（"2.1 切块…"、
    "一句话结论"）才真正落在各自的章节上。只发主题那一行是量"整树一次检索"的
    上限用的对照组：Jaccard 1.00 意思是 8 支拿到的证据完全一样。

    **反过来说，这里没有结论说"分支标题够用"**：标题和材料用词对不齐时同样会掉
    （把标题换成"背景/现状/方法"这类通用词，覆盖率 28.8% → 15.4%）。要往上走是
    让骨架先看一眼材料的章节标题（`_chunk_tree` 的父块本来就按标题行切开，
    `knowledge.service._HEADING_RE` 已经能识别 `#`、`第N章`、`一、`、`（一）`、`1.1`），
    那是下一轮的事——本轮只用实测证伪了"主题在前"。

    向量路未实测（本机无 embedding 凭证）：它可能没有这种"标题行高频词"问题，
    所以这一版是"在唯一测得动的条件下更好"的选择，不是普适定论。
    """
    return title.strip()


def _branch_queries(title: str, intents: Sequence[str] = ()) -> list[str]:
    """一支实际发出的查询串：**标题永远第一位**，后面接至多 `BRANCH_QUERY_LIMIT-1` 条问法。

    第一位必须是标题，这不是排序偏好而是契约：`search_multi` 的**保底席位**
    （前 `ceil(top_k/2)` 名）全部来自第一位那一路。把扩展问法放到前面，等于把
    "用户/骨架原话的召回"降级成"模型猜的问法的召回"——2.3 那次实测里，扩展
    问法把真答案顶下去过（`search_multi` 的注释钉着那个复现）。

    去重与去空白都在这里做一遍，不指望 `plan_branches` 那道闸：
      - 与标题**逐字相同**的问法要丢：`plan_branches` 已经过滤过，但这个函数
        也是 `_generate_deep` 算 `retrieval.queries` 的地方，两处口径必须同一份；
      - 重复问法要丢：`search_multi` 内部会去重，但"我以为发了 3 路、实际 2 路"
        这种事在成本核算时会变成一个说不清的差集。

    上限在这里截断而不是靠 `MAX_ROUTES`：那个上限会把**标题**留在前面、把扩展
    截掉，结论一样，但日志里会多一条"多路检索截断"的 debug——那条日志该只属于
    "调用方真的给了六七条 query"的情形。
    """
    queries: list[str] = []
    base = title.strip()
    if base:
        queries.append(base)
    for item in intents:
        text = item.strip() if isinstance(item, str) else ""
        if text and text not in queries:
            queries.append(text)
        if len(queries) >= BRANCH_QUERY_LIMIT:
            break
    return queries


def _ref_from(item: Any, evidence: _BranchEvidence) -> dict[str, Any] | None:
    """模型给的一条 `source_ref` → 收得下的契约形状；收不下或不属于本支就返回 None。

    逐条清洗都是**必要**的，不是防御性编程：`SourceRef` 的 `start`/`end` 是
    `strict=True` 的整数、`end > start`、`quote` 不超过 300 字。模型把 start 写成
    `"123"`、把 quote 抄成父块全段（450–700 字）都是常态，任何一条漏过去都会让
    `model_validate` 在**整棵树上**炸掉，用户拿到的是一次 `invalid_tree`。

    区间优先于引文判归属（理由见 `_BranchEvidence`）：有坐标就比坐标，没坐标才
    在文本里找。

    **引文一个字符都不许改，`strip()` 也不行**：带坐标的引用会被
    `_reference_problem` 拿 `quote != text[start:end]` 逐字比对，而叶子块的原文
    常常以换行/空格开头（切块按句首吸附，不是按内容吸附）。抹掉首尾空白之后
    `quote` 与区间原文就差了一个 `\n`，整条引用被判 `SOURCE_QUOTE_MISMATCH` 剔掉
    ——实测 8 支的树里有 13 条这样消失的引用，界面上看起来像"这一支没有证据"。
    """
    if not isinstance(item, dict):
        return None
    source_id = item.get("source_id")
    if not isinstance(source_id, str) or not source_id.strip():
        return None
    source_id = source_id.strip()

    quote = item.get("quote")
    quote = quote if isinstance(quote, str) else ""
    if len(quote) > _EVIDENCE_QUOTE_MAX_CHARS:
        # 超过契约上限的引文一律丢掉引文、留区间：截断一段再配原区间必然对不上，
        # 那比"没有引文"更糟（`_reference_problem` 会判 SOURCE_QUOTE_MISMATCH）。
        quote = ""

    start, end = item.get("start"), item.get("end")
    if (isinstance(start, bool) or isinstance(end, bool)
            or not isinstance(start, int) or not isinstance(end, int)
            or end <= start):
        start = end = None
    if start is None and not quote:
        return None

    if start is not None:
        owned = any(
            span.source_id == source_id and end > span.start and start < span.end
            for span in evidence.spans
        )
    else:
        owned = quote in evidence.quotes
    if not owned:
        return None

    ref: dict[str, Any] = {"source_id": source_id}
    if quote:
        ref["quote"] = quote
    if start is not None:
        ref["start"] = start
        ref["end"] = end
    # `page` 刻意不收：本次检索没有逐页映射（`SourceSpan.page` 是总页数），
    # 模型给的页码无从核实，收下来只会多一条 INVALID_SOURCE_PAGE 噪音。
    return ref


def _ref_source_id(ref: Any) -> str | None:
    """一条 `source_ref` 指向哪个来源。

    槽位里的引用是**已经摊平成 op 草案的 dict**（`_refs_from` 的产物），但同一段
    代码也可能拿到 `SourceRef` 实例——两种形状都要认，否则过滤时一半数据会
    `AttributeError`（测试先撞上，线上则是整批 op 失败）。
    """
    if isinstance(ref, dict):
        value = ref.get("source_id")
    else:
        value = getattr(ref, "source_id", None)
    return value if isinstance(value, str) else None


def _refs_from(item: dict[str, Any], evidence: _BranchEvidence | None) -> list[dict[str, Any]]:
    """一个子项上带的 `source_refs`。没有证据可依据时**一条都不收**。

    没有检索结果的分支，模型写什么引用都只能是编的（它没看到任何原文）。这一层
    直接清空，而不是等收尾的逐字校验去剔——两者的界面表现一样，但前者能让
    "这一支是没有证据才空着"在 `data` 里看得出来。
    """
    if evidence is None:
        return []
    raw = item.get("source_refs")
    if not isinstance(raw, list):
        return []
    refs = [ref for ref in (_ref_from(entry, evidence) for entry in raw) if ref]
    # `AddOp.source_refs` 的契约上限是 20，超出会在合并时报错并把整棵树判失败。
    return refs[:20]


@dataclass
class _Slot:
    """展开出来、还没进树的一个候选节点。

    `expandable` 记的是「模型自己说这个子项还能不能再拆」：新格式里每个子项可以
    带一个 `expandable` 布尔字段，填 false 表示它已经是最小可执行单元。代码按它
    决定还要不要发下一次调用——层数上限只是兜底，不再替模型决定某支该不该深挖。

    `expandable_declared` 记的是模型**有没有真的写这个键**：`leaf_declared` 那类
    统计必须能区分「模型说不能再拆」与「模型忘了写、代码按 true 兜底」，两者混在
    一起，这个信号到底有没有被模型理解就统计不出来了。

    旧格式 `{"points":[...]}` 是一串扁平字符串，语义就是列到这里的叶子：标
    `expandable=False`，但 `expandable_declared` 保持 False——停在这里是格式决定
    的，不是模型的判断，不该算进声明计数。

    `children` 是模型**多给**的一层（新格式里容忍，见 `_candidate_slots`）：
    一次调用只要求它写一层，给了两层就收下，比再追问一次便宜。自带 `children`
    的节点已经有内容了，它自己不再被追问，它的子节点各按自己的标记决定。

    `source_refs` 只在 `rag` 形态且这一支真的检索到东西时才会有值（见
    `_refs_from`）。它跟着 slot 一路走到合并那一步，最后作为 `add` 的
    `source_refs` 挂进树里——**不在** `emit()` 里重新解释一遍。

    `detail` 是模型为这个节点写的 2–3 句说明（与 `label` 一起构成两层节点）。
    它在 `_slot_from` 里已经按 `MAX_NODE_DETAIL_CHARS` 截过，之后一路原样带到
    `add` 草案上，中间不再改写：这棵树是模型的作品，代码只负责长度与形状。

    `truncated` 记的是「这一支**本来还能问**，但被额度挡住了」——节点预算不足
    （`level_cut`）、模型调用额度用完（`level_skipped`）、展开抛错、或模型这次
    什么都没给。它只有一个去处：落树时把这个节点的 `expandable` 写成
    `"unknown"` 而不是 `"yes"`（判定表见 `mindmap.NODE_EXPANDABLE`）。
    两者对用户的意思相反：`yes` 是"还能展开"，`unknown` 是"没展开完，要不要
    再点一次由用户决定"——把被截断的那批记成 `yes` 也没错，但记成 `no` 就是
    谎报"到头了"，而那正是这次要分开的那件事。
    """

    label: str
    detail: str | None = None
    children: list["_Slot"] = field(default_factory=list)
    expandable: bool = True
    expandable_declared: bool = False
    truncated: bool = False
    source_refs: list[dict[str, Any]] = field(default_factory=list)


def _candidate_slots(
    raw: Any, limit: int, depth_left: int = 0, *,
    evidence: _BranchEvidence | None = None,
) -> list[_Slot]:
    """从一次展开响应里取出候选子节点，最多 `limit` 个。

    接受两种形状：
      - `{"children":[{"label":"要点一","expandable":false}]}`：新格式。
        `children` 里再嵌一层会被收下，但最多 `depth_left` 层——**不主动要求**
        模型一次写两层，实测那样写到后面会被输出上限截断。
      - `{"points":["要点一","要点二"]}`：旧格式，纯字符串。语义是「这支到此
        为止」，收下的节点标 `expandable=False`，不再往下追问。

    只认字符串和带 `label` 的 dict：`{"children":[{"name":"x"}]}` 说明模型没照
    格式写，硬转字符串会把整个 dict 的 repr 当成节点标题挂到树上——那种"成功"
    比失败更难发现。
    """
    if not isinstance(raw, dict) or limit <= 0:
        return []
    items = raw.get("children")
    legacy = False
    if not isinstance(items, list):
        items = raw.get("points")
        legacy = True
    if not isinstance(items, list):
        return []
    slots: list[_Slot] = []
    for item in items:
        if len(slots) >= limit:
            break
        slot = _slot_from(item, limit, depth_left, legacy, evidence=evidence)
        if slot is not None:
            slots.append(slot)
    return slots


def _slot_from(
    item: Any, limit: int, depth_left: int, legacy_leaf: bool, *,
    evidence: _BranchEvidence | None = None,
) -> _Slot | None:
    """单个候选项 → `_Slot`。空标签、非字符串/非 dict 的项一律丢掉。

    `expandable` 只有**布尔值**才算一次声明：模型写成 `"false"` 字符串、写成 0、
    或干脆没写，一律按可展开处理。缺省按 true 是有意的——模型偶尔漏写一个键，
    不该因此让整支内容凭空变成叶子，那是一种代码替它做的决定。

    `label` 截到 `GENERATED_LABEL_MAX_CHARS`（16）；超出的**不**升级成 `detail`：
    那不是模型写的说明，是标题的后半截，拼进去会得到一句从中间断开的话。宁可
    丢掉那半句，也不要制造一段读不通的正文——真正的长内容应该由 `detail` 承载。

    这条判定在 20 棵验收树上量过（`docs/LABEL-CLIP-VERDICT.md`）：44 个 16 字 label
    的 `detail` **全部非空、且全部已经含全称**（Kruskal / VLAN / BGP / PNG / Word /
    21ms / #218 / PostgreSQL / Element Plus……）。所以"丢掉那半句"丢的不是信息，
    只是标题自己的可读性——残尾没有可搬的必要。**但"切在半个 token 上"是另一件事**，
    它不归这里管，归 `clip_label`（`#21` 不是 `#218` 的缩写，是另一个 issue）。

    `evidence` 是**这一支**的检索结果，逐层传下去：模型多给的那一层里的引用同样
    只能落在本支证据上，否则"只带该支的 hits"就只对第一层成立。
    """
    if isinstance(item, str):
        label = item.strip()
        if not label:
            return None
        # 裸字符串没有任何层级信息：旧 points 里它是叶子，新格式里按缺省可展开，
        # 与引入 `expandable` 之前的行为逐节点相同。
        return _Slot(clip_label(label), expandable=not legacy_leaf)
    if not isinstance(item, dict):
        return None
    label = item.get("label")
    if not isinstance(label, str) or not label.strip():
        return None
    flag = item.get("expandable")
    declared = isinstance(flag, bool)
    slot = _Slot(
        clip_label(label.strip()),
        detail=_detail_text(item.get("detail")),
        expandable=flag if declared else True,
        expandable_declared=declared,
        source_refs=_refs_from(item, evidence),
    )
    nested = item.get("children")
    if depth_left > 0 and isinstance(nested, list):
        for child in nested[:limit]:
            child_slot = _slot_from(child, limit, depth_left - 1, False, evidence=evidence)
            if child_slot is not None:
                slot.children.append(child_slot)
    return slot


def _detail_text(raw: Any) -> str | None:
    """模型给的 `detail` → 可落库的字符串（或 `None`）。

    三件事一起做：非字符串当没给、折掉换行（`Node.detail` 与 `label` 同在导出里
    会破坏结构）、按契约上限截断。**不判空**——纯空白在契约层也会被归一成
    `None`，但这里提前归一能少一次无意义的校验。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    return text[:MAX_NODE_DETAIL_CHARS]


def _keep_new_slots(existing: list[_Slot], incoming: list[_Slot]) -> tuple[list[_Slot], int]:
    """把新候选并入已有列表，丢掉重复项，返回 `(留下的, 丢掉几个)`。

    去重是**验收**的一部分而不是洁癖：提示词里写了「不要重复」，模型仍会重复
    （实测同一要点返回两次）。不去重的话「点数够不够」会被重复项骗过去——
    数量验收通过，用户看到的却是同一句话挂在两个节点上。
    """
    seen = {slot.label for slot in existing}
    kept: list[_Slot] = []
    for slot in incoming:
        if slot.label in seen:
            continue
        seen.add(slot.label)
        kept.append(slot)
    return kept, len(incoming) - len(kept)


def _trim_slots(slots: list[_Slot], allowance: int) -> tuple[list[_Slot], int]:
    """把候选子树裁到 `allowance` 个节点以内，返回 `(留下的, 实际节点数)`。

    模型多给一层时可能一次吐出远超额度的节点。不裁的话「按层预分配」就只是一句
    口头承诺，实际节点数会随模型心情漂，最后在 `apply_ops` 的整树校验上炸掉——
    那时候已经花完所有调用了。
    """
    kept: list[_Slot] = []
    used = 0
    for slot in slots:
        if used + 1 > allowance:
            break
        slot.children, nested = _trim_slots(slot.children, allowance - used - 1)
        used += 1 + nested
        kept.append(slot)
    return kept, used


def _count_slots(slots: list[_Slot]) -> int:
    return sum(1 + _count_slots(slot.children) for slot in slots)


def _count_expand_flags(slots: list[_Slot]) -> tuple[int, int]:
    """统计模型显式给出的展开标记：`(标为不可展开的, 标为可展开的)`。

    这两个数字回答的是「模型到底有没有用这个字段」，所以缺省一律不计数：
    没写 `expandable` 的节点按 true 展开，那是代码的兜底而不是模型的意思，算进
    任何一边都会让统计失去意义。旧 `points` 格式同理——它停在叶子是格式决定的。
    """
    leaf = expandable = 0
    stack = list(slots)
    while stack:
        slot = stack.pop()
        if slot.expandable_declared:
            if slot.expandable:
                expandable += 1
            else:
                leaf += 1
        stack.extend(slot.children)
    return leaf, expandable


def _nested_capacity(fanout: int, levels: int) -> int:
    """一次响应里最多容忍多少个"多给的下层"节点。`levels = 0` 时一个都不容忍。"""
    return sum(fanout**level for level in range(1, max(0, levels) + 1))


def _walk_slots(
    slots: list[_Slot], depth: int, path: str, max_depth: int,
    prefix: tuple[int, ...] = (),
):
    """列出还能继续展开的节点：`(节点, 它现在在第几层, 到它为止的路径, 子索引链)`。

    每一层开始前都会再走一遍，所以这里就是「该不该继续拆」的筛选点：候选只包含
    `expandable=True` 且**没有** `children` 的节点。

    带 `children` 的节点已经长出下一层了，它自己不再追问，但它的子节点要在
    `depth + 1` 层接着排——否则"模型一次多给了一层"会让这一支就此停住。
    `expandable=False` 的节点按模型的意思留在原地：它是最小可执行单元，再拆只会
    得到重复内容，还白花一次调用。`max_depth` 退化成兜底，不再替模型决定深浅。

    `path` 是给人（也给模型）看的文字路径，末位的索引链是给 `node_grow` 用的：
    两者由同一次遍历给出，免得调用方自己再走一遍树、两处口径慢慢分叉。索引链的
    起点由 `prefix` 决定——它通常是"这条分支在根下面排第几"。
    """
    for index, slot in enumerate(slots):
        here = f"{path} > {slot.label}" if path else slot.label
        indices = (*prefix, index)
        if slot.children:
            yield from _walk_slots(slot.children, depth + 1, here, max_depth, indices)
        elif slot.expandable and depth < max_depth:
            yield slot, depth, here, indices


async def _emit_grow(
    ctx: RunContext,
    seen: set[tuple[tuple[int, ...] | None, str]],
    path: tuple[int, ...] | None,
    label: str,
    expandable: bool | None,
) -> None:
    """上报一帧节点生长事件（预览用）。

    `path` 指向父节点：`()` 是"挂在根上"，`None` 是根自己。两者不能都用 `()`——
    那样就分不清第一个分支和中心主题，只能靠发送顺序兜着，顺序一乱前端就把分支
    画成根，而且不报错。

    同一 `(path, label)` 只报一次：同一个父节点下重复的 label 在最终树里本来就会
    被 `_keep_new_slots` 丢掉，预览里也不该出现两遍。

    通道为 None 时直接返回 —— 这条路上工具的行为必须与加生长事件之前逐字相同。
    """
    emit = ctx.emit
    if emit is None:
        return
    key = (path, label)
    if key in seen:
        return
    seen.add(key)
    await emit({
        "type": "node_grow",
        "path": list(path) if path is not None else None,
        "label": label,
        "expandable": expandable,
    })


async def _emit_stage(
    ctx: RunContext,
    stage: str,
    detail: str,
    *,
    count: int | None = None,
) -> None:
    """上报一帧阶段消息（`RunContext.emit` 的第二类帧，第一类是 `node_grow`）。

    它存在的理由只有一个：**用户点完"生成"之后有一段静默**。外部检索要 4–6 秒
    （实测中位 4.3s），这段时间里界面上什么都不动——`run()` 在 `await` 工具执行，
    工具在等 HTTP 往返。看起来像卡死，而人已经准备再点一次。`node_grow` 解决不了
    它：那一帧说的是"树长出第 N 个节点"，而检索阶段一个节点都还没有。

    帧形状与 `node_grow` 同构（一个 `dict`，`type` 打头），由 `service._stage_frame`
    转成带 seq 的 `StageEvent`、再经 `stream.encode_event` 出去的既有信封。
    字段只有三个：`stage` 稳定标识 / `detail` 给人看的一句话 / `count` 数量结果。

    `count` 只在"这件事有数量结果"时给（外部检索的结束帧给命中条数），不用 `0`
    冒充"没有"：`0` 与 None 在这里含义不同——"一条都没搜到"是结论，"还没结束"
    是过程，两者的界面表现必须不同。

    通道为 None 时**一次 await 都不多发**，与加这帧之前逐字相同：没有 SSE 消费者
    的路径（直接调工具的用例、库内调用）不该因为界面需要而多出任何行为。
    """
    emit = ctx.emit
    if emit is None:
        return
    payload: dict[str, Any] = {"type": "stage", "stage": stage, "detail": detail}
    if count is not None:
        payload["count"] = count
    await emit(payload)


@dataclass
class _GrowStream:
    """一路展开调用的增量上报器：delta 喂进来，`node_grow` 发出去。

    只在 `RunContext.emit` 不为 None 时才会被创建（见 `_generate_deep` 里的
    `ask_model`），所以这里不判空 —— 判空只留一处，两处各判一遍迟早有一处写反。

    `quota` 是**这一路真正会被装进树里的节点数上限**（调用方传 `_trim_slots`
    用的那个 allowance）。它存在的理由只有一条：预览必须与最终树同形。模型一次
    多给一层时会吐出远超额度的节点，那些多出来的会在合并前被 `_trim_slots` 裁掉
    ——如果预览照单全收，用户就会眼睁睁看着几个节点"长出来又消失"，而消失的那
    几个从来没有进过树。
    """

    ctx: RunContext
    seen: set[tuple[tuple[int, ...] | None, str]]
    parent: tuple[int, ...]     # 父节点在树里的子索引链
    base_index: int = 0         # 这一路产出的第一个子项在父节点下的位置
    quota: int | None = None    # 这一路最多报几个节点；None = 不设限
    buffer: str = ""
    reported: int = 0
    accepted: int = 0
    # 这一路最多报几层（1 = 只报第一层）。None = 不设限。
    #
    # 存在的理由与 `quota` 同源：预览必须与最终树同形。按章切那条路（`_slice_one`）
    # 用 `SECTION_SLICE_DEPTH` 砍层，而 `closed_items` 会把模型写出的**所有**层都
    # 收进来——模型偶尔多给一层，那些节点在预览里长出来、在最终树里根本不存在。
    # 深层生成那条路不传它（`None`），行为与加这个字段之前逐字相同。
    max_level: int | None = None

    async def feed(self, delta: str) -> None:
        """收到一段模型输出，把刚写完整的子项报出去。

        `reported` 记的是**扫描到第几个**，与去重无关：同一个 label 被模型写了
        两次时，第二次不该因为"没上报"而把后面的偏移整体错位。
        """
        self.buffer += delta
        items = closed_items(self.buffer)
        fresh = items[self.reported:]
        self.reported = len(items)
        for offset, item in enumerate(fresh):
            await self._report(self.parent, self.base_index + offset, item)

    async def _report(
        self, parent: tuple[int, ...], index: int, item: StreamItem,
        level: int = 1,
    ) -> None:
        """先报自己，再报模型额外给的下一层。

        事件里的 `path` 指向**父节点**，所以同一路响应里的子项共用 `parent`；
        `index` 是 item 自己在 `parent` 下面的位置，只用来给它的孩子拼路径——
        模型多给的那一层，父节点就是这个 item。

        额度用完就整支不再报（含它的孩子）：`_trim_slots` 是按同一顺序、同一个
        节点计数裁的，所以这里停下的位置与最终树丢掉的位置一致。
        """
        if self.quota is not None and self.accepted >= self.quota:
            return
        if self.max_level is not None and level > self.max_level:
            return
        self.accepted += 1
        await _emit_grow(self.ctx, self.seen, parent, item.label, item.expandable)
        own = (*parent, index)
        for position, child in enumerate(item.children):
            await self._report(own, position, child, level + 1)


@dataclass
class _GenerateGrowStream:
    """整树响应的增量上报器：响应里自带节点 id，要换成预览协议里的父索引链。

    与 `_GrowStream` 并列而不是合并：那个认的是"一次只写一层"的
    `{"children":[...]}`（`stream_items.closed_items`），这个认的是整棵树
    （`{"root":{...}}`，`generation.stream_partial`）。两套提取器对应两种响应
    形状，合起来会得到一个要判"这次是哪种形状"的分支——而形状由调用方决定，
    不是由文本决定。

    `path_of` / `child_counts` 是"节点 id → 它在树里的子索引链 / 已经报了几个
    孩子"。之所以要自己记：预览协议的 `path` 是**父节点的索引链**，而整树响应里
    只给了父 id，索引要按出现顺序现数。

    `pending` 处理增量提取的顺序异常：先拿到孩子、后拿到父亲时，孩子先存着，
    等父亲报出去之后再补报。直接跳过的话，那几个孩子在此后的 delta 里再也不会
    出现（提取器已经把它们记进 `seen` 了）。

    通道为 None 时这个类根本不会被创建（`_generate` 里判过一次），所以这里不判空。
    """

    ctx: RunContext
    buffer: str = ""
    seen: dict[str, str] = field(default_factory=dict)
    reported: set[tuple[tuple[int, ...] | None, str]] = field(default_factory=set)
    path_of: dict[str, tuple[int, ...]] = field(default_factory=dict)
    child_counts: dict[str, int] = field(default_factory=dict)
    pending: dict[str, list[dict]] = field(default_factory=dict)

    async def feed(self, delta: str) -> None:
        self.buffer += delta
        obj = parse_partial(self.buffer)
        if obj is None:
            return
        for parent_id, node in collect_new_nodes(obj, self.seen, self.buffer):
            await self._report(parent_id, node)

    async def _report(self, parent_id: str | None, node: dict) -> None:
        if parent_id is not None and parent_id not in self.path_of:
            # 增量提取顺序异常时可能先拿到孩子；提取器已记住孩子的 id，
            # 这里若直接跳过，后续 delta 就再也不会返回这些孩子。
            self.pending.setdefault(parent_id, []).append(node)
            return

        path: tuple[int, ...] | None = None
        own: tuple[int, ...] = ()
        if parent_id is not None:
            path = self.path_of[parent_id]
            index = self.child_counts.get(parent_id, 0)
            self.child_counts[parent_id] = index + 1
            own = (*path, index)

        # 整树提取器只保留 id/label，不能把它补出的空 children 当作不可展开。
        await _emit_grow(self.ctx, self.reported, path, node["label"].strip()[:200], None)
        self.path_of[node["id"]] = own
        for child in self.pending.pop(node["id"], []):
            await self._report(node["id"], child)


def _audit_branches(
    content: MindMapContent, minimum: int,
) -> tuple[list[tuple[int, str, str, int]], list[str]]:
    """确定性验收：只用代码看合并后的树，一次模型调用都不发。

    返回 `(要点不足的分支 (下标, id, 标题, 实际点数), 要点超过上限的分支标题)`。
    两者性质不同：不足是模型质量的正常波动，接受现状并记诊断；超过
    `MAX_CHILDREN` 是结构错误——那样的树连契约都存不进去，必须判失败。

    带下标是因为同一支可能既"点数不足"又"根本没展开成"：调用方要靠下标把
    重复的告警剔掉，否则一个因额度被跳过的分支会同时得到两条诊断。
    """
    sparse: list[tuple[int, str, str, int]] = []
    overfull: list[str] = []
    for index, branch in enumerate(content.root.children):
        count = len(branch.children)
        if count < minimum:
            sparse.append((index, branch.id, branch.label, count))
        if count > MAX_CHILDREN:
            overfull.append(branch.label)
    return sparse, overfull


# 契约模块里承载节点上限的常量名。名字对不上时退到源码里的数字，再不行就用
# 真实的校验行为把它试出来——三条路都不写死数字：契约刚把上限从 300 改成 1000，
# 工具里的额度预分配必须跟着那个数走，否则「按层分配」分的是个过期预算。
_NODE_LIMIT_CONSTANTS = ("MAX_TREE_NODES", "MAX_NODES", "MAX_NODE_COUNT", "NODE_LIMIT")
# 校验器里的两种写法：直接比较数字，或比较常量再拼进错误文案。
_NODE_LIMIT_PATTERNS = (r"len\(\s*seen\s*\)\s*>\s*(\d+)", r"最多\s*(\d+)\s*个节点")


@lru_cache(maxsize=1)
def _node_limit() -> int:
    """整树节点上限（契约口径）。

    为什么不直接 `from app.contracts.mindmap import MAX_TREE_NODES`：常量名会随
    重构改，而"改了之后这里还静默用旧值"是最难发现的错——多出来的节点要到
    `apply_ops` 的整树校验才被拒，那时调用额度已经花完了，用户拿到的是失败
    而不是一棵稍小的树。所以这里按「常量 → 源码 → 行为」三级退让，全都不是
    硬编码的数字。
    """
    for name in _NODE_LIMIT_CONSTANTS:
        value = getattr(mindmap_contract, name, None)
        if isinstance(value, int) and value > 0:
            return value
    source = _validator_source()
    for pattern in _NODE_LIMIT_PATTERNS:
        match = re.search(pattern, source)
        if match:
            return int(match.group(1))
    return _probe_node_limit()


def _validator_source() -> str:
    """契约里那段整树校验的源码；拿不到（打包/动态生成）时返回空串。"""
    try:
        return inspect.getsource(mindmap_contract.MindMapContent.validate_tree)
    except (OSError, TypeError):  # pragma: no cover — 源码不可得是环境问题
        return ""


def _probe_node_limit() -> int:  # pragma: no cover — 只在名字与源码都读不到时用
    """最后一条路：拿契约自己的校验器当尺子，二分出它能接受的最大节点数。

    只在常量名与源码都读不到时才会走到（例如校验器改成动态计算上限）。树用
    扇出 50 广度优先铺开，深度压在 3 层以内：这样先撞上的一定是节点上限而不是
    深度上限——两个上限混在一起就分不清是哪一个在拒。
    """

    def accepts(nodes: int) -> bool:
        try:
            mindmap_contract.MindMapContent.model_validate(_wide_tree(nodes))
        except ValidationError:
            return False
        return True

    low, high = 1, 1
    while accepts(high) and high < 200_000:
        low, high = high, high * 2
    while low + 1 < high:
        middle = (low + high) // 2
        if accepts(middle):
            low = middle
        else:
            high = middle
    return low


def _wide_tree(nodes: int) -> dict[str, Any]:
    """造一棵有 `nodes` 个节点、扇出不超过 50、深度不超过 3 的树（给探针用）。"""
    root: dict[str, Any] = {"id": "n0", "label": "x", "children": []}
    frontier = [root]
    made = 1
    while made < nodes and frontier:
        next_frontier: list[dict[str, Any]] = []
        for parent in frontier:
            while len(parent["children"]) < 50 and made < nodes:
                child = {"id": f"n{made}", "label": "x", "children": []}
                parent["children"].append(child)
                next_frontier.append(child)
                made += 1
        frontier = next_frontier
    return {"title": "x", "root": root}


@dataclass
class _TreeAudit:
    """整树验收结果。全部来自纯代码遍历，一次模型调用都不发。"""

    depths: dict[int, int]  # 层号（root 为 0）-> 该层节点数
    total: int
    deepest: int  # 最深一层是第几层（root 为 0）
    blank: list[str]  # label 为空白的节点 id
    missing: list[int]  # 本该有、实际一个节点都没有的层


def _audit_tree(root: Node, expected: set[int]) -> _TreeAudit:
    """确定性验收：总节点、层级覆盖、空节点。

    三条都是"结构错误"而不是"模型质量波动"，但处理方式不同——只看总量与空标签
    （那两样连契约都存不进去，必须判失败）；层级缺失是**报告项**：某一层没有节点
    可能是额度不够、模型没给出可用的下一层，此时该交出一棵浅一点的树并说清楚，
    而不是把已经生成好的部分整张作废。

    `expected` 只传本次真的尝试展开过的层号。没试过的层不该被算成"缺失"：
    旧格式（纯字符串要点）本来就表示这一支到此为止，把它的下一层记成缺失等于
    为一件没发生的事报警。
    """
    depths: dict[int, int] = {}
    blank: list[str] = []
    stack: list[tuple[Node, int]] = [(root, 0)]
    total = 0
    while stack:
        node, depth = stack.pop()
        total += 1
        depths[depth] = depths.get(depth, 0) + 1
        if not node.label.strip():
            blank.append(node.id)
        stack.extend((child, depth + 1) for child in node.children)
    deepest = max(depths) if depths else 0
    missing = [depth for depth in sorted(expected) if not depths.get(depth)]
    return _TreeAudit(depths=depths, total=total, deepest=deepest, blank=blank, missing=missing)


def _edit_targets(args: EditArgs) -> list[str]:
    """一批 op 会碰到哪些节点——只用于"有没有越出选区"的**提示**（不是拦截）。

    两条口径：

      - `add` 比的是 `parent_id` 而不是 `id`。新节点的 id 是本批临时名
        （`$tmp1`），它根本不在树里，"落在谁的子树内"对它没有意义；
        真正决定"改到哪去"的是父节点。
      - 同批新建的父节点（`$` 开头）跳过：它自己会被"它挂靠在谁下面"那条覆盖，
        报一个树里还不存在的 id 只会让提示里出现一串模型自己编的名字。

    为什么要单独抽一个函数：批量与六个细粒度工具都要回答"这次动到哪儿"，
    各写一遍的话，加第七种 op 时必然只改一处。
    """
    targets: list[str] = []
    for item in args.ops:
        if item.op == "add":
            # 缺 parent_id 由后面的 invalid_ops 给更准确的错误，这里不抢答
            if item.parent_id:
                targets.append(item.parent_id)
        else:
            targets.append(item.id)
        if item.op == "move" and item.new_parent_id:
            targets.append(item.new_parent_id)
    # 本批新建的临时 id（`$tmp1`）一律不提：树里还没有这个名字，提示里列出它
    # 只会让模型以为自己在改一个真实节点。它挂靠在谁下面已经由上面那条约束住了。
    return [target for target in targets if not target.startswith(TEMP_ID_PREFIX)]


EXPAND_PROMPT = (
    "你在给思维导图的一个节点补充子要点。只返回一个 JSON 对象，不要代码围栏。"
    '格式为 {"children":[{"label":"要点一","detail":"这个要点的说明"}]}。'
    f"每个 label 是一句话短语，不超过 {GENERATED_LABEL_MAX_CHARS} 字，不带句号；"
    "detail 是 2–3 句说明，可以带数字与条件。"
    "不要编号，不要解释，不要重复已有子节点，不要编造具体数据、人名或文献。"
    # 视觉元素只属于一级分支（见 `PLAN_PROMPT`）：这一层写出来也会被忽略，
    # 明确说一句比让模型自己猜"为什么我写的图标不见了"便宜得多。
    "这里**不要**给子节点写 color 或 icon：视觉元素只用在骨架的一级分支上。"
    # 旧格式（纯字符串数组）仍然收：老模型、老 mock、以及"模型只写了一半"
    # 这几种情况都会退化成它，硬拒等于把一次可用的响应判死。
    '也接受 {"points":["要点一","要点二"]} 这种旧格式，这时节点没有 detail。'
)

# 分片生成第一步：只定骨架，不写内容。一次调用同时要求模型「定框架 + 每支展开」，
# 它会把注意力放在先写出来的那几支上，后面的分支越写越敷衍——这是质量不稳的
# 直接来源。骨架单独一次调用，模型只需回答「分成哪几块」。
#
# `color` / `icon` 只在这一层要，而且**只有这一层**：一级分支是画布上唯一"横向
# 铺开、需要一眼区分"的行，一根分支一个语义色 + 一个图标，读图的人能靠颜色定位；
# 二级往下再上色就成了噪音——80 个节点 80 个图标，图标就不再指示任何东西。
# 这条边界靠代码兜底（`_slot_from` 根本不读这两个字段），提示词只是让模型不要把
# 力气花在会被丢掉的地方。
PLAN_PROMPT = (
    "你在为一张思维导图只定一级骨架，不写任何子要点。"
    "只返回一个 JSON 对象，不要代码围栏，格式为 "
    '{"branches":[{"label":"分支一","color":"blue","icon":"🎯"},'
    '{"label":"分支二","color":"amber","icon":"⚠️"}]}。'
    # 16 字与 detail 的那两条是同一批改动：骨架标题**就是**画布上的一级分支，
    # 它比任何节点都更该短。20 字的旧上限画出来已经是两行半。
    f"每条分支是一句话短语，不超过 {GENERATED_LABEL_MAX_CHARS} 字，不带句号；"
    "彼此覆盖的范围不重叠，按逻辑顺序排列；不要编号，不要解释，不要编造具体数据、人名或文献。"
    "材料自带章节时优先采用它们作为一级分支；目录为空或不完整时再自行归纳。"
    # 视觉元素的规格必须写死，否则模型会给 `#2563eb`、"深蓝"、"🔧⚙️" 这类东西。
    f"color 与 icon 是给**一级分支**的视觉标记，建议每条分支都给（可选，不必勉强）："
    f"color 只能取 {' / '.join(NODE_COLORS)} 之一；"
    f"icon 是**一个** emoji，不超过 {MAX_NODE_ICON_CHARS} 个字符。"
    "✅ 正确："
    '{"branches":[{"label":"选题与范围","color":"blue","icon":"🎯"},'
    '{"label":"数据准备","color":"teal","icon":"📊"}]}'
    "❌ 错误："
    '{"branches":[{"label":"选题与范围","color":"#2563eb"},{"label":"数据准备","icon":"放大镜"}]}'
    "（花括号里那个是十六进制、不在调色板里；icon 写成了文字）"
    "❌ 错误："
    '{"branches":[{"label":"选题与范围","color":"blue","icon":"🎯 🔧","children":[{"label":"子项"}]}]}'
    "（icon 塞了两个 emoji，而且骨架不要写子节点）"
)

# 材料是**要整理的对象**，不是"这类文档怎么写"的范例。**只在有材料时**拼在
# `plan_system` 末尾（`_generate` 与 `_generate_deep` 同一处，见 `AGENTS.md` 的
# "骨架被两条生成路共用"）。
#
# 为什么必须显式写这一句：骨架那一次看不到材料正文（`_generate` 的"全塞"成本口径那一段
# 解释了为什么不看），它只拿到主题 + 目录 + 清单。**主题是一份文档的名字时**
# （"产品需求评审会""研发周例会""某某周报"），模型的默认读法是"用户想了解这类文档"，
# 于是把一级分支写成「定义与作用 / 内容要素 / 写作要求 / 格式规范 / 整理与发布」——
# 那不是整理，那是把材料当范文讲写作课。实测（`docs/OFF-TOPIC-AND-FEW-BRANCHES.md`）：
# 7 条会议纪要里 4 条整棵树的骨架都是这一套，而材料里的三个需求、故障根因、候选人定级
# 全被压进 detail 当举例。
#
# 判据写成"两种意图"而不是"会议纪要要怎样"：这是**通用失效**，不是某一类文档的
# bug。任何"材料本身是一份产出物、用户想整理它"的场景都中招——课程大纲、周报、
# 立项书、访谈记录、复盘的形状完全不同，但"材料 = 待整理对象 vs 材料 = 这类文档的
# 示例"这个岔口对它们每一个都存在。针对某一类文档打补丁等于把这条判据埋掉。
# 与它配对的那另一半在提取器：目录抽不出来时模型连"材料有什么块"都看不到，
# 判据再好也只能凭先验猜（`knowledge.service.STRUCTURAL_KINDS`）。
MATERIAL_AS_OBJECT_RULES = (
    "\n这次的**材料是要整理的对象，不是「这类文档怎么写」的范例**。"
    "一级分支写材料里**讲了哪几件事**（有哪些议题、结论、待办、风险、指标、分歧），"
    "不要把材料归成「定义与作用 / 内容要素 / 写作要求 / 格式规范 / 整理与发布」"
    "这类通用知识；只有材料本身就在讨论「怎么写这类文档」时，才写这些。"
)


# 骨架少于 `MIN_BRANCHES` 条时的重问提示。**只在有材料时用**（纯主题没有"材料的
# 其他内容"可指），拼在那一次 user 的末尾——位置即权重，它要压住前面所有关于
# 条数的判据（`NO_BRANCH_CAP_RULE` 说"按内容定条数"，没说"至少几条"）。
FEW_BRANCHES_RETRY_RULES = (
    "上一次只给了 {count} 条一级分支，少于成图要求的 {minimum} 条——这张图会被判无效。"
    "材料里不止讲了这些事：请按材料**实际讲到的不同事情**重新划分，至少 {minimum} 条，"
    "每一支都要能在材料里找到依据；不要把一件事拆成两支来凑数，"
    "也不要重复上一次已经给过的那几条。"
)


def _few_branches_evidence(
    sources: list[Source], outline_entries: Sequence[Any],
) -> str:
    """重问时给模型的"材料里到底有什么"——目录优先，没有目录就给开头几百字。

    为什么要有这一节：重问如果只是把同一段 user 再发一次，答案不会变（实测同材料
    两次都是 2 条）。要让它能改，必须换**输入**，而不是加重语气。

    目录（`_outline_entries`）是最便宜的证据：它是原文的子集、每条都短。而目录
    抽不出来的材料（没有编号标题的散文、访谈记录）只剩"开头几段"这一条路——
    那也正是骨架正常情况下完全看不到的东西。上限按"每份 300 字、最多 3 份"取：
    重问是一次**补救**，不该按全塞的口径花 token。
    """
    titles = [getattr(entry, "title", "") for entry in outline_entries]
    titles = [title for title in titles if title]
    if titles:
        return ("材料自带的章节（逐块至少一条一级分支）：\n"
                + "\n".join(f"{index}. {title}" for index, title in enumerate(titles, start=1)))
    heads: list[str] = []
    for source in sources[:3]:
        text = " ".join((source.text or "").split())
        if not text:
            continue
        name = (source.filename or source.id or "材料").strip()
        heads.append(f"{name}：{text[:300]}")
    return ("材料开头（这份材料真实在讲的事，就在这里）：\n" + "\n".join(heads)) if heads else ""


async def _retry_few_branches(
    ctx: RunContext,
    plan_system: str,
    ask: str,
    plan: "_SkeletonPlan",
    *,
    sources: list[Source],
    outline_entries: Sequence[Any],
) -> "_SkeletonPlan | None":
    """一级分支少于 `MIN_BRANCHES` 时的**一次**带证据重问；问不动就返回 None。

    为什么放在工具内部而不是交给模型自己再调一次工具：外层那条重试（`service.run`
    的 `max_repairs`）发的是**同一段 user**，同材料同提示词，答案确定性相同——验收里
    那三条 `too_few_branches` 就是这么死在第二次上的（"重试一次仍是 2 条"）。

    这一次的差别只有两处，都在**输入**上：
      - 末尾多一句"上一次只给了 N 条、这张图会被判无效"（`FEW_BRANCHES_RETRY_RULES`）；
      - 附上材料里**真实存在的块**（`_few_branches_evidence`：目录，或没有目录时的开头）。

    额度：这条补救要占一次模型调用，**占不到就不做**（返回 None，按原条数继续走
    降级交付）。它不该因为一次补救把整次运行推到 `budget_exhausted`——那等于为了
    "多一条分支"把已经花掉的骨架调用也废掉。
    """
    if not ctx.reserve_model_call():
        return None
    ctx.extra_model_calls += 1
    hint = FEW_BRANCHES_RETRY_RULES.format(count=len(plan.flat), minimum=MIN_BRANCHES)
    evidence = _few_branches_evidence(sources, outline_entries)
    tail = "\n" + hint + ("\n" + evidence if evidence else "")
    raw = await _chat(ctx, plan_system, ask + tail, max_tokens=_max_tokens(ctx))
    return _cap_plan(_skeleton_plan(raw))


# 流程图骨架。与 `PLAN_PROMPT` 共用同一套 JSON 骨架（`branches` 的形状一字不差），
# 只多两个东西：**一级分支必须是阶段**，以及**一个 edges 数组**。
#
# 为什么阶段必须是流程推进的环节而不是话题分类：流程图的主干 = 执行的先后顺序，
# 而"话题分类"之间本来就没有方向。模型按分类给一级分支，画出来是一张思维导图；
# 按阶段给，父链才真的等于"下一步"。
#
# 为什么边只连**阶段之间**：产品里的流程图现在是 `FlowContent`（`stages` +
# 阶段间跳转），边端点的命名空间是**阶段 id**——中心主题在产物里根本不存在。
# 骨架这一层因此只要求阶段序号；旧写法里的 `"root"` 仍然被 `_skeleton_edges`
# 认得（老前端、老模型都会写它），但转换时那几条边会被丢掉并记
# `FLOW_EDGE_DROPPED`：`root` 没有对应的阶段，"从主题出发"那条线也不比数组
# 下标多说什么。
#
# 为什么用**序号**而不是节点 id：真实 id 是服务端分配的，模型根本不知道。
# 让它写 `"n_3f2a"` 只会得到一堆 dangling。序号（`0` 起）是它能可靠写对的东西，
# 解析时由 `_skeleton_edges` 换回真实 id。
FLOW_PLAN_PROMPT = (
    "你在为一张**流程图**只定主干骨架：先定出流程的阶段，再给出阶段之间的跳转关系，"
    "不写任何子要点。只返回一个 JSON 对象，不要代码围栏，格式为 "
    '{"branches":[{"label":"提交申请","color":"blue","icon":"📝"},'
    '{"label":"形式审查","color":"amber","icon":"🔍"},'
    '{"label":"实质审查","color":"teal","icon":"🧪"}],'
    '"edges":[{"source":1,"target":2,"label":"是","kind":"branch"},'
    '{"source":1,"target":0,"label":"驳回","kind":"loop"}]}。'
    f"每条一级分支是一个**流程阶段**（谁在什么条件下做什么，往前推进的一环），"
    f"不超过 {GENERATED_LABEL_MAX_CHARS} 字，不带句号；**按执行顺序排列**，彼此不重叠；"
    "不要编号，不要解释，不要编造具体数据、人名或文献。"
    # edges 的三条硬规矩，每条都对应一个真实会画错的形态。
    "edges 里 source / target 只能写**阶段序号（0 起）**，"
    "不要写节点标题，也不要自己编 id；也不要用 \"root\"——流程图上没有中心主题这个位置。"
    "edges 只连阶段，**不要连阶段内部的步骤**（阶段内部的关系由父子层级表达）。"
    "kind 只能是 flow / branch / loop："
    "flow 表示跳过若干阶段的顺序跳转（相邻阶段的先后已经由 branches 的顺序表达，不要重复画）；"
    "branch 表示判断节点的两个出口，**必须带 label**，写「是」或「否」；"
    "loop 表示回退/返工，**必须带 label**，写回到哪一步（例如「驳回」）。"
    "label 不超过 40 字。没有条件跳转与回退时 edges 给空数组 []——不要为了凑数画线。"
    # 视觉元素：规格与思维导图一致（见 `PLAN_PROMPT`），否则模型会写十六进制与文字图标。
    f"color 与 icon 是给**一级分支**的视觉标记，建议每条分支都给（可选，不必勉强）："
    f"color 只能取 {' / '.join(NODE_COLORS)} 之一；"
    f"icon 是**一个** emoji，不超过 {MAX_NODE_ICON_CHARS} 个字符。"
    "✅ 正确："
    '{"branches":[{"label":"提交申请"},{"label":"形式审查"},{"label":"付款"}],'
    '"edges":[{"source":1,"target":2,"label":"是","kind":"branch"},'
    '{"source":1,"target":0,"label":"驳回","kind":"loop"}]}'
    "❌ 错误："
    '{"branches":[{"label":"提交申请"},{"label":"形式审查"}],'
    '"edges":[{"source":"提交申请","target":"形式审查","kind":"flow"}]}'
    "（端点写成了标题，不是序号）"
    "❌ 错误："
    '{"edges":[{"source":0,"target":1,"kind":"branch"}]}'
    "（branch 边没有 label，画出来只有一个色块，看不出走哪条）"
    "❌ 错误："
    '{"edges":[{"source":0,"target":1,"kind":"next"}]}'
    "（kind 只能是 flow / branch / loop）"
)

# 鱼骨图骨架。一级分支**固定**为六个类别，这是鱼骨图的定义而不是风格选择：
# 换一套分类，画出来就不再是鱼骨图。代码侧只做"忽略 edges"，不做"校验是不是
# 正好这六条"——模型偶尔多给一条（比如把"测"拆成"测量"与"测试"）不该判失败，
# 而少给一条时用户自己加更便宜。
#
# 骨架**还要产出主骨上那句话**（`problem`）。它是鱼骨图与思维导图最实质的一处
# 差别：鱼头写的是一个"为什么会这样"的问题，六根骨回答它；而 `title` 是文件名
# （"报销慢的原因分析"）。不要求模型写这一句时，产物里没有任何地方能放它，
# 画布上的鱼头只能复读标题——那正是这一版要消灭的形状。
FISHBONE_PLAN_PROMPT = (
    "你在为一张**鱼骨图（因果分析）**只定骨架，不写任何子要点。"
    "只返回一个 JSON 对象，不要代码围栏，格式为 "
    '{"problem":"为什么新员工要三个月才独立上岗",'
    '"branches":[{"label":"人","color":"blue","icon":"👤"},'
    '{"label":"机","color":"teal","icon":"⚙️"},'
    '{"label":"料","color":"amber","icon":"📦"},'
    '{"label":"法","color":"purple","icon":"📐"},'
    '{"label":"环","color":"green","icon":"🌐"},'
    '{"label":"测","color":"red","icon":"📏"}]}。'
    # problem 的三条硬规矩，每条都对应一种真实会写歪的形态：复读标题（等于没有
    # 论点）、写成名词短语（"报销慢的原因"没有"为什么"）、写成一句分析结论
    # （"因为审批链太长"把答案写进了问题里，六根骨就没得分析了）。
    "problem 是**主骨上那一句**：一个「为什么 X 会 Y」的疑问句，40 字以内，不带句号。"
    "它不是主题的重写——主题是这张图叫什么（用于文件名），problem 是这张图要回答的"
    "那个问题；**不要复读主题**。也不要写成一句结论或原因（「因为审批链太长」是答案，"
    "不是问题），不要写成名词短语（「报销慢的原因」缺了「为什么」）。"
    "一级分支**必须正好是这六个类别**，顺序也照这个：人 / 机 / 料 / 法 / 环 / 测，"
    "每条不超过一个字到两个字，不要改写成别的话题分类。"
    "**不要输出 edges**：鱼骨图的关系全部由层级表达，多画线只会把图弄乱。"
    f"color 只能取 {' / '.join(NODE_COLORS)} 之一；"
    f"icon 是**一个** emoji，不超过 {MAX_NODE_ICON_CHARS} 个字符。"
    "✅ 正确："
    '{"problem":"为什么设备故障停机时间一直降不下来",'
    '"branches":[{"label":"人","color":"blue"},{"label":"机","color":"teal"}]}'
    "❌ 错误："
    '{"problem":"设备故障停机分析",'
    '"branches":[{"label":"人员管理"},{"label":"设备维护"}]}'
    "（problem 写成了名词短语，一级分支改成了话题分类，都不是固定类别）"
    "❌ 错误："
    '{"problem":"因为保养计划执行不到位",'
    '"branches":[{"label":"人"},{"label":"机"}]}'
    "（problem 写成了原因/结论；六类也只给了两类，六类缺一不可）"
)

# ---- 鱼骨图的类别可变（`MINDMAP_FISHBONE_FREE_CATEGORIES`，默认关）-------------
#
# ## 为什么要放开类别
#
# 人机料法环测（5M1E）是**制造业现场管理**的维度表。对着「为什么团队协作效率低」
# 「为什么转化率上不去」这类主题，那六个维度本来就不合适，而上面的提示词明令"必须
# 正好是这六个、不许改写成别的话题分类"，模型只能扭曲归类：实证的一张图（主题是
# 人的管理）六类都在，而「料」下面写着「员工技能不足」——那是「人」里的东西。
# 那不是模型不会分，是它**没有第二个出口**。
#
# ## 放开之后靠什么不退化
#
# 真正的风险是"类别变成话题分类"，鱼骨图于是退化成普通树。现行提示词用一句
# "不要改写成别的话题分类"防它，但**没给判据**——类别名本身判不了（「人」既是
# 话题也是变量）。这里给的是可判的那条界线：
#
#   **原因维度是一个可取高低的变量；话题分类是一个领域或职能。**
#
# 三个可操作的测试（都写进提示词，正反例各一条）：
#
#   1. 因果槽位：「（这个类别）不足 / 缺失 / 偏高 → 主骨上那个为什么」成立吗？
#      「工具 → 效率低」成立；「人员管理 → 效率低」不成立（它不是变量，是题目）。
#   2. 还能不能再分：「工具」下面挂的是具体原因（"审批系统要来回切三个页面"）；
#      「人员管理」下面挂的是**另一个维度**（"招聘慢""培训少"）——那说明它是领域。
#   3. 正交性：一个原因只能落进一个类别。「工具」与「系统」两份就是重复。
#
# 判据之外还有一条**覆盖面**约束（"第三条路"）：类别名自由，但必须覆盖四个成因面
# 里的至少三个——谁来做（人）/ 用什么做（物）/ 怎么做（法）/ 在哪做（境）。这四类
# 是 5M1E 去掉"测"之后剩下的那四类，也是 8P、服务业模板都能归进去的最小公约数。
# 它保住的正是鱼骨图真正值钱的那件事：**逼你把成因面过一遍**。丢掉的是"永远是那
# 六个名字"，那件事在非制造主题上本来就没有兑现过。
#
# ## 用户点名时以用户为准
#
# `FISHBONE_TEMPLATES` 是"用户明说要按某套类别画"的词表。这条路以前**不存在**：
# 请求层没有类别字段（`AgentRunRequest` 只有 `diagram_type`），`diagram_intent`
# 只认图型词——用户说「按人机料法环测画」，那五个字会当主题的一部分进 prompt，
# 然后被"必须正好是这六个类别"覆盖。现在开关打开时它至少能被认出来并压过模型自定。
#
# 词表里只有 ≤6 个的模板，因为 `MAX_FISHBONE_BONES` 是契约硬上限 6：8P（8 个）
# 与服务业的六类模板**这次不进词表**——8P 塞进 6 根骨要靠截断，那是改用户的模板
# 而不是照它画。见 `docs/FISHBONE-CATEGORIES.md`。
FISHBONE_FREE_ENV = "MINDMAP_FISHBONE_FREE_CATEGORIES"


def fishbone_free_categories_enabled() -> bool:
    """鱼骨图类别可变开关（`MINDMAP_FISHBONE_FREE_CATEGORIES`，默认**关**）。

    真值写法与 `input_extract_enabled` / `section_slice_enabled` 一致（正列举真值、
    缺省取空串即关）：这几个改变提示词形状的开关要能用同一行 shell 一起切。
    """
    return os.getenv(FISHBONE_FREE_ENV, "").strip().lower() in GRAPH_SKELETON_TRUTHY


#: 用户明确点名过的类别模板。键是**正则**（`人机料法环测` 会被写成带空格的、
#: 写成 `5M1E` 的、写成 `5m1e` 的），值是那套类别的逐字清单。
#:
#: 只收 ≤ `MAX_FISHBONE_BONES` 个的模板（见上面那段注释）。
FISHBONE_TEMPLATES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    # 5M1E。中文连写是最常见的说法；`人机料法环` 五连写也算（用户常常漏掉"测"）。
    (re.compile(r"5\s*[Mm]\s*1\s*[Ee]|人\s*机\s*料\s*法\s*环\s*测|人\s*机\s*料\s*法\s*环"),
     ("人", "机", "料", "法", "环", "测")),
    # 4M1E：人 / 机 / 料 / 法 / 环。写在 5M1E 之后，两条都命中时先登记的那条赢。
    (re.compile(r"4\s*[Mm]\s*1\s*[Ee]"), ("人", "机", "料", "法", "环")),
)


def named_fishbone_categories(text: str) -> tuple[str, ...]:
    """用户在这句话里点名了哪一套类别；没点名返回空元组。

    与 `diagram_intent.infer` 同一条边界：**按词面判，判不出来就什么都不做**——
    猜一套类别比让模型按主题自己定更糟（模型至少看着 `problem`）。
    """
    for pattern, categories in FISHBONE_TEMPLATES:
        if pattern.search(text or ""):
            return categories
    return ()


# 自由类别版的骨架提示词。与 `FISHBONE_PLAN_PROMPT` 的差别**只在"一级分支"那一段**
# （problem 的三条硬规矩逐字相同：它们守的是"鱼头里是不是一个问题"，与类别无关）。
FISHBONE_FREE_PLAN_PROMPT = (
    "你在为一张**鱼骨图（因果分析）**只定骨架，不写任何子要点。"
    "只返回一个 JSON 对象，不要代码围栏，格式为 "
    '{"problem":"为什么新员工要三个月才独立上岗",'
    '"branches":[{"label":"技能","color":"blue","icon":"👤"},'
    '{"label":"手册","color":"teal","icon":"📕"},'
    '{"label":"带教","color":"amber","icon":"🧑‍🏫"}]}。'
    "problem 是**主骨上那一句**：一个「为什么 X 会 Y」的疑问句，40 字以内，不带句号。"
    "它不是主题的重写——主题是这张图叫什么（用于文件名），problem 是这张图要回答的"
    "那个问题；**不要复读主题**。也不要写成一句结论或原因（「因为审批链太长」是答案，"
    "不是问题），不要写成名词短语（「报销慢的原因」缺了「为什么」）。"
    # 顺序：先 problem 再类别。理由写在这里，因为它是这次改动最容易被写反的一处——
    # 类别是"这个问题的成因面"，是关于 **problem** 的函数，不是关于主题的函数。
    # 同一个主题换个问法（"为什么转化率上不去" / "为什么留存率上不去"）该长出不同的
    # 类别；先想类别再套问题，必然长出"这个主题分几块讲"。
    "**先写 problem，再定一级分支**。一级分支是**这个问题的成因面**，不是这个主题"
    "分几块讲：写完 problem 回头对一遍，每一个一级分支都要能填进「（这个分支）出了"
    "偏差 → 主骨上那个为什么」，填不进去的换掉。"
    "一级分支 3 到 6 个（少于 3 个不成图），每个 1 到 4 个字、名词性、彼此不重叠。"
    # 判据与正反例：这是整段的核心。现行提示词只写了"不要改写成话题分类"——一句
    # 没有判据的禁令，模型只能靠猜。这里给的是"变量 vs 领域"这条可判的界线。
    "判据是**变量还是领域**：原因维度是一个能取高低、有缺无的变量（技能 / 步数 /"
    "信息 / 工具 / 名额 / 天气），它下面能直接挂具体原因；话题分类是一个领域或职能"
    "（人员管理 / 设备维护 / 技术架构 / 团队建设），它下面还得再分一层维度。"
    "四个成因面里**至少覆盖三个**（名字随主题起，不要照抄这四个词）："
    "① 谁来做（人的技能、认知、意愿、编制）；② 用什么做（设备、材料、技术、数据、"
    "工具）；③ 怎么做（流程、方法、规则、标准、职责边界）；④ 在哪做（环境、外部"
    "条件、时间、资源）。主题确实不涉及的那一面可以不出现，但不要放一个填不满的类别"
    "凑数——填不进原因的类别，画出来是一根空骨。"
    "**不要输出 edges**：鱼骨图的关系全部由层级表达，多画线只会把图弄乱。"
    f"color 只能取 {' / '.join(NODE_COLORS)} 之一；"
    f"icon 是**一个** emoji，不超过 {MAX_NODE_ICON_CHARS} 个字符。"
    "✅ 正确（主题「团队协作」）："
    '{"problem":"为什么团队协作效率一直上不去",'
    '"branches":[{"label":"职责","color":"blue","icon":"🧩"},'
    '{"label":"沟通","color":"teal","icon":"💬"},'
    '{"label":"工具","color":"amber","icon":"🛠"},'
    '{"label":"会议","color":"purple","icon":"🗓"},'
    '{"label":"激励","color":"green","icon":"🎯"}]}'
    "（五个都是变量：「职责不清 → 协作慢」成立，每个下面挂得进具体原因）"
    "❌ 错误："
    '{"branches":[{"label":"人员管理"},{"label":"设备维护"},{"label":"团队建设"}]}'
    "（三个都是**领域/职能**，是「这一节讲什么」，填进「（它）出了偏差 → 为什么」里"
    "不通；它下面挂的还会是别的维度）"
    "❌ 错误："
    '{"branches":[{"label":"沟通不畅"},{"label":"工具落后"}]}'
    "（这两条**已经是一条原因**了：类别是维度，「沟通」下面才挂「沟通不畅」）"
    "❌ 错误："
    '{"branches":[{"label":"工具"},{"label":"系统"}]}'
    "（同义：同一个原因会同时落进两个类别，正交性没了）"
    "❌ 错误："
    '{"branches":[{"label":"人"},{"label":"员工"},{"label":"团队"}]}'
    "（三个都在同一个成因面上：覆盖面约束要的是至少三个不同的面）"
)

# 自由类别版的**结构规则**（根规则 + 骨下面写什么）放在 `FISHBONE_TREE_RULES` 之后：
# 它要复用 `_FISHBONE_BODY`，而那个常量在本函数下面才定义。

def fishbone_plan_prompt() -> str:
    """这一次鱼骨骨架用哪份提示词。**开关关着时返回的就是那个常量本身**。"""
    return FISHBONE_FREE_PLAN_PROMPT if fishbone_free_categories_enabled() else FISHBONE_PLAN_PROMPT


# 骨架提示词里那段"材料自带的章节"的表头。测试用它断言"目录拼进去了没有"，
# 所以必须是**一个字符串常量**而不是散在 `_skeleton_outline` 里的字面量——
# 两处各写一遍，改一处忘一处时，用例会绿着而提示词已经不再是那个形状。
OUTLINE_HEADER = "这份材料自带的章节（请优先按它划分一级分支）："

# 目录覆盖率低于它就**不拼**进骨架。
#
# 不拼不是保守，是准确：一份只覆盖 11% 的目录拼进去，模型会把它当成全文的提纲，
# 于是没被目录管住的那 89% 它再也不会想到要问——那比"没有目录"更糟，因为没有
# 目录时它至少会按主题自己归纳几块。60% 这个数来自上一轮实测：10 份真实材料里
# 一级标题覆盖字数的中位数是 97%，8/10 份 ≥60%，所以真实材料几乎总在阈值之上，
# 而"真的没结构"的材料会被这条闸挡住。
OUTLINE_MIN_COVERAGE = 0.6


def _outline_entries(sources: list[Source]) -> list:
    """这份材料的目录行——**只在"该拼给骨架"时**返回，否则空列表。

    门槛（覆盖率 ≥ `OUTLINE_MIN_COVERAGE`）放在这里而不是只放在渲染处，是因为
    两级骨架也要读它：那一次要先知道"这份材料大概有多少个方面"才能决定要不要分组、
    分几组。用**裁剪后**的目录判覆盖率，理由见 `_skeleton_outline`。

    **少于 `MIN_BRANCHES` 条的目录仍然算"有目录"**（它确实抽出了章节），只是拼给骨架时
    会多一句"这块偏少、可以更细"（`OUTLINE_FEW_BLOCKS_NOTE`，见 `_render_outline`）。
    这个区分是必要的：下游有几处问的是"这份材料有没有可用结构"（`_ingest_external_graph`
    的第五道闸、两级骨架的规模判据），把它们一起判成"没有目录"会让入图那 17.5 秒白付。
    """
    entries = heading_outline(sources)
    if not entries:
        return []
    if outline_coverage(sources, entries) < OUTLINE_MIN_COVERAGE:
        return []
    return list(entries)


def _skeleton_outline(sources: list[Source]) -> str:
    """拼给骨架的目录块；不该拼时返回空串。

    目录是**原文的子集**，和 `_material_inputs` 给 planner 的那前 200 字同一类：
    "正文不进 prompt"这句话的准确版本是"正文不进，标题行作为结构线索会进"
    （措辞见 `_material_inputs` 的 docstring 与 `generation.service` 里那段
    "材料怎么进 prompt"的注释）。这里进 prompt 的只有标题行，一条最多 40 字。

    两件事一起做：取目录（含覆盖率门槛，见 `_outline_entries`）、渲染。判覆盖率必须用
    **裁剪后**的那份目录（`outline_coverage(sources, entries)` 而不是不带参数的调用），
    否则"覆盖率 95%"可能配着一份只剩 8 条、恰好漏掉大章的目录。

    百分比的分母是原文总字数，所以"约占 X%"读起来是"这一章大约占全文多少"，
    与模型的直觉一致；小于 1% 的按 1% 显示，写"约占 0%"是在告诉模型这一段不重要，
    而它可能正好是唯一提到某件事的地方。
    """
    entries = _outline_entries(sources)
    return _render_outline(entries, sources)


def _render_outline(
    entries: Sequence[Any], sources: list[Source], *, note: str = "",
) -> str:
    """把目录行渲染成骨架提示词里那一段；`entries` 为空时返回空串。

    与 `_outline_entries` 分开，是为了让调用方**只解析一次**：两级骨架要用"有多少行"
    决定分组，也要那一段文字，两件事读同一份 `entries`（`heading_outline` 是对整份
    材料跑正则，跑两遍在大材料上不是零成本）。

    `note` 拼在目录行**之后**（默认空串 → 与改动之前逐字节相同）。它唯一的用处是
    `OUTLINE_FEW_BLOCKS_NOTE`：目录只有 1–2 条时，`OUTLINE_HEADER` 那句"请优先按它
    划分一级分支"会变成一条**结构上限**，而 `MIN_BRANCHES = 3` 会立刻判废整次生成。
    """
    if not entries:
        return ""
    total = sum(len(source.text or "") for source in sources)
    lines = [OUTLINE_HEADER]
    for index, entry in enumerate(entries, start=1):
        share = round(entry.coverage / total * 100) if total else 0
        lines.append(f"{index}. {entry.title}（约占 {max(1, share)}%）")
    if note:
        lines.append(note)
    return "\n".join(lines)


# 目录行数少于 `MIN_BRANCHES` 时补的那一句。**它取消的是"优先采用"这个锁，不是目录本身**：
# 材料真只有两块时，模型照着写两块，`MIN_BRANCHES = 3` 当场判废，用户拿到错误卡片
# （实测那三条项目立项材料的错误码就是 `too_few_branches` → 重试 → `too_many_failures`）。
# 而把目录整段丢掉又太贵：它是骨架唯一能看到的"材料有什么块"。所以留着目录、
# 把"条数跟着它"改成"块名的方向是对的，条数按材料讲到的事另定"。
OUTLINE_FEW_BLOCKS_NOTE = (
    "（这条目录只分出 {count} 块，比一张图需要的少：一级分支按材料里**讲到的事**划分，"
    "不少于 {minimum} 条——可以比上面这些块更细，也可以合并，但不要只给 {count} 条。）"
)


# 骨架那一次的**第四种**结构线索（前三种见 `_material_inputs` 的 docstring）：材料清单。
#
# 它只回答一个问题：**这次有几份东西、各多长**。没有它时骨架只看到主题 + （可能的）
# 章节目录，而 16 份独立摘要是"抽不出章节"的——于是它按主题猜"机器学习该分几块"
# （上一轮中批就是这么切出 3 支的，那 3 支要吃下 16 份材料）。`docs/GRAPH-SCALE.md`
# 第 4 节实测：一级分支数跟着**配额量级**走（上限 80 → 5–6 支，32 → 3 支，10 → 1 支），
# 因为模型手里没有任何"材料规模"的量可供参照。
#
# 为什么它比另外三条都窄（每条只有文件名与字数，正文一个字都不进）：planner 要判断
# "该分几块"，需要的是**件数与长度**，不是内容。前三种给内容的方式在这里都是浪费
# ——章节标题行是原文子集但要材料有标题，全塞要 5 万字以内，`_material_inputs` 的
# 200 字只在 `MINDMAP_QUERY_EXPANSION` 打开时才走（默认关）。
INVENTORY_HEADER = "这次的材料（合计 {chars} 字{note}）："
"""**表头不报份数**——这是 `c5465fd9` 之后修的一个实测缺陷（`docs/GRAPH-SCALE.md` 第 11 节）。

原来写的是「这次的材料（共 2 份，合计 424 字…）」。那批 16 份的 A/B 里它是有用的
（模型因此把一级分支从模板词换成了真实篇目），但在**薄批**上它是灾难：两份
openalex 摘要（210 + 214 字）时，模型把表头那个「2」当成"这次有几个方面"，
直接给 2 条一级分支 —— 而 `MIN_BRANCHES = 3`，那次生成当场判废。实测（同进程
交替，每臂 3 次，`scripts/probe_plan_inventory.py`）：

    清单开（旧表头）  3 支 / 判废 / 判废
    清单关            6 支 / 6 支 / 6 支

材料明明撑得住 6 支（清单关时三次都给了 6），所以根因不是"内容单薄"，是那个数字。
模型需要知道的只是"有哪些东西、各多长"，份数是编目事实、对"分几支"没有信息量；
规模已经由**行数与每行字数**表达，截断时另有「还有 N 份未列出」一行兜底
（`INVENTORY_MAX_LINES` 那段注释：上限不许藏住规模）。删掉它不丢信息，只丢一个锚。
"""

# 没有可用结构块时补的那半句。措辞必须与 `OUTLINE_MIN_COVERAGE` 那道闸对齐：
# 覆盖不足时目录同样不拼，"没有可用的章节标题"对"抽不出标题"与"覆盖率不够"两种
# 都成立（写"没有章节标题"在后者上就是假话）。
INVENTORY_NO_STRUCTURE = "，没有可用的章节标题"

# 一行"文件名 + 字数"约 30 字，20 行 ≈ 600 字——与一段目录（30 行 × 20 字）同量级，
# 这是它敢设成 20 而 `MAX_MATERIALS` 只能设 10 的原因：那边每份带 200 字正文
# （10 × 200 = 2,000 字），这边只带结构。
#
# 但**上限不能藏住规模**：16 份被截到 10 份时，模型看到的规模与真实规模不符，
# 清单就成了新的谎话。所以表头永远是**总数**，被截掉的部分另起一行明说还有几份。
#
# 这条不是新发明：`170aaaa` 删掉的 `_material_manifest`（整树入口带的清单）就是这么写的
# ——表头给份数、每行"标题 + 规模"、截断处明说"另有 N 份未在清单里列出"、标题夹 80 字。
# `docs/ONE-PATH.md` 第 103 行把那份清单列进"真正沉没的能力"，并写着"要拿回来，应当
# 单独做"。这次就是单独做：只给骨架、只给结构，去掉 id / 类型 / `rag_query` 提示
# （骨架那一次没有任何工具可调，给了也没有可操作性），上限改成条数而非 token
# （骨架这一侧每条都短且必须自解释）。
INVENTORY_MAX_LINES = 20

#: 清单的**实验开关**（默认开，不设 = 现行行为，逐字节相同）。
#:
#: 为什么需要能关：清单那一轮的 A/B 是把 `_material_inventory` 换成一个恒返回空串的
#: 函数（monkeypatch），那只在进程内有效——"配额 × 清单"这组联合实验要在**同一个
#: 进程里交替**四格（否则模型/网络的漂移会被算进格差），而 monkeypatch 的换法是
#: 每次进出都要记住原函数，稍有 other 代码引用就漏。环境变量是进程状态的显式入口，
#: 关闭值就是 `_material_inventory` 早退、`_plan_ask` 一字不加——与加清单之前相同。
PLAN_INVENTORY_ENV = "MINDMAP_PLAN_INVENTORY"


def plan_inventory_enabled() -> bool:
    """只有显式的假值（`0`/`off`/`false`/`no`）才关；**不设 = 开**。

    这个方向必须与 `budget.node_scale_enabled` 反过来：那个开关默认关（它改的是
    每一次生成的规模，翻它之前得先有数字），这个默认**开**——它已经在生产路径上
    跑过了（`8a4f5a8` 起），关它只是为了把"骨架看不看得见清单"重新变成一个变量。
    写成"不设 = 开"而不是"不设 = 关"，默认路径才不需要任何人记得去设一个变量。
    """
    return os.getenv(PLAN_INVENTORY_ENV, "").strip().lower() not in (
        "0", "off", "false", "no")


def _material_inventory(sources: list[Source], *, has_structure: bool) -> str:
    """拼给骨架的材料清单；没有材料时返回空串（那一支逐字节不变）。

    `has_structure` 表示 `_plan_ask` 里已经有结构块（章节或图谱）：有就不再补
    "没有可用的章节标题"——那句话在"有目录"和"图谱骨架"两种情形下都不成立。

    字数按 `strip()` 后的长度算，与 `_intake.full_text_chars` 同一口径：清单上那个
    "合计 X 字"必须能与 prompt 里真正塞进去的正文对得上，否则同一批材料在两个
    地方有两个字数。

    顺序按 `sources` 原序（与 `_full_text_sections` 同一条理由：材料在请求里的次序
    是用户表达"哪份更重要"的方式，重排会让清单与界面上的清单对不上）。被截掉的
    永远是**尾巴**，不是按长度挑——挑大的会把"还有 N 份"变成一句无法解释的话。

    `MINDMAP_PLAN_INVENTORY=0` 时直接返回空串（见 `plan_inventory_enabled`）：那
    一支走的是 `_plan_ask` 的"没有清单"分支，与加清单之前逐字节相同。
    """
    if not plan_inventory_enabled():
        return ""
    rows = [(source, (source.text or "").strip()) for source in sources]
    if not rows:
        return ""
    total = sum(len(text) for _, text in rows)
    header = INVENTORY_HEADER.format(
        chars=f"{total:,}",
        note="" if has_structure else INVENTORY_NO_STRUCTURE,
    )
    lines = [header]
    for index, (source, text) in enumerate(rows[:INVENTORY_MAX_LINES], start=1):
        # 文件名压平换行：与 `_material_snippets` 同一个理由（材料里的换行会让
        # 模型把清单的下一行当成新的一段指令）。只截长度，不改措辞。
        name = " ".join((source.filename or "").split())[:80] or "（未命名）"
        lines.append(f"  {index}. {name}（{len(text):,} 字）")
    rest = rows[INVENTORY_MAX_LINES:]
    if rest:
        lines.append(
            f"  （还有 {len(rest)} 份未列出，合计 {sum(len(t) for _, t in rest):,} 字）")
    return "\n".join(lines)


# 清单之后那句"怎么用这个数"。**光给清单不够**：模型看了 16 份也只是看了，它仍会按
# "机器学习"这个主题的直觉去切——而那 16 篇在它眼里是"一篇关于机器学习的文章"。
#
# 措辞的三条约束（防止走向"每份一条分支"的另一个极端）：
#
#   1. **给的是判据，不是条数**：只说"按件数分"，不说"每份一条分支"。后者会撞上
#      `branches` 上限（界面最多 8）与 `MIN_BRANCHES=3`，模型要么被截断要么失败；
#   2. **必须点名"合并"**：中批这 16 篇里真有重叠——两篇讲财务报销流程优化、两篇讲
#      用户注册登录的安全性、两篇讲机器学习进课堂；只说"分到不同分支下"会让它宁可拆细；
#   3. **必须说"这几件也算"**：一份材料里讲了几件不同的事，同样算几件——否则
#      小批（1 份 492 字）会被理解为"只有一件，给 1 支"，而契约要求至少 3 支。
#
# 实测（`docs/GRAPH-SCALE.md` 第 9 节，中批 A/B 各 3 次）：分支数**没有变多**
# （5.67 → 5.67；请求 8 支时两臂都 6 支），变的是**分支点在哪**——从模型的通用领域
# （金融风控/智能制造/交通）换成材料里真实存在的篇目，锚定率 16.9% → 24.2%，
# 请求 8 支那一档从 0.0% → 24.7%。所以这条判据的作用是"让宽度跟着材料走"，
# 不是"让图变宽"；后者由节点配额（`INITIAL_MAX_NODES`）决定。
INVENTORY_RULES = (
    "上面这些材料是**一堆彼此独立的篇目**，不是同一份长文的各节——一级分支按"
    "「里面有几件互不相同的事」来分：讲不同事情的篇目分到不同分支下，"
    "讲同一件事的几篇合进一支；一篇里讲了几个互不相同的方面，也算几件。"
    # 最后这一句是 `docs/GRAPH-SCALE.md` 第 11 节那个缺陷的第二道闸。表头已经不报
    # 份数（`INVENTORY_HEADER`），但清单里仍是**一行一份**：行数照样是一个可数的锚。
    # 把「份数 ≠ 分支数」写成一句独立的话，是因为原来那句"一篇里讲了几个方面，
    # 也算几件"埋在一个长句的末尾、还用了"也"字（读起来像补充说明）。实测两种
    # openalex 摘要时模型给的正是"2 份 → 2 支"，而它这 424 字里真有五六件事。
    "**材料的份数不是分支数**：行数只说编目，不说该切几块——两篇摘要里也可能讲着"
    "五六件互不相同的事，不要按行数把分支数对齐。"
)


def _full_text_sections(sources: list[Source]) -> list[str]:
    """把材料**整份**渲染成 prompt 里的分节：一份一节，标题带文件名、字数与 source_id。

    与 `EVIDENCE_SECTION_HEADER` 那套（按一级分支分组）是同一个洞的两个方向：那条路
    给的是"每支检索到的片段"，这条路给的是"每份材料的全文"。两者都要求模型**只从
    给定的内容里抄**，所以分节表头必须自带 `source_id`——全塞没有 `evidence_*` 可用，
    模型能写对 `source_refs[].source_id` 的唯一依据就是这里。

    不带偏移渲染：全文进 prompt 之后模型的坐标感不可靠，让它自己数 `start/end` 只会
    产出一批过不了逐字校验的引用。只抄 `quote`，偏移由服务端在全文里唯一定位
    （`source_refs._locate_quote`）。

    顺序按 `sources` 原序（请求里用户给的顺序），不按长度排：材料在清单里的次序本身
    是用户表达"哪份更重要"的方式，重排会让它写出来的结构对不上它看到的界面。
    """
    sections: list[str] = []
    for index, source in enumerate(
            [item for item in sources if (item.text or "").strip()], start=1):
        text = (source.text or "").strip()
        title = (source.filename or "").strip() or f"材料 {index}"
        sections.append(
            FULL_TEXT_SECTION_HEADER.format(
                index=index, title=title[:80], chars=len(text), source_id=source.id
            ) + "\n" + text)
    return sections


# ------------------------------------------ 按章切：每支只给它那一章的原文（实验开关）
#
# 这条路做的是什么，一句话：**骨架定好之后，每支一次调用，输入里只有它自己那一章的
# 原文**。第二段（结构）的产物仍然是一棵树，第三段（`_fill_details`）一个字都没改。
#
# 与既有三条路的差别只有一处，但那一处正是这次要考的：
#
#   - 一次生成 + 全塞（现状）：整份 14,120 字都进同一个 prompt，一次写整棵树。模型
#     能看住全局（跨支重复 0），但几十段内容挤在一次输出里，注意力被摊薄；
#   - 分片（已删）：每支各自【检索】top_k=3。那是**全局检索**，兄弟支的内容会被
#     捞进它的输入，于是它能写出别人已经写过的话——库里那 6 处跨支重复就是这么来的；
#   - **按章切（本开关）**：每支只看见自己那一章。兄弟支的内容不在它的输入里，
#     它写不出重复——这不是"提示词让它别重复"，是**机制上没有可重复的材料**。
#
# 为什么切得动：`OutlineEntry.start` / `coverage` 天生就是这个章节在原文里的区间
# （`text[start : start + coverage]`）。而 `_outline_lines` **只收一个级别**——markdown
# 取"井号最少且至少 `OUTLINE_MIN_ITEMS` 条"的那一级，编号行取最高频的那一级——所以
# 切出来的章**互相不相交**，每章包含它自己的 `###` 子节。实测 `docs/RAG-DECISIONS.md`：
# `#`×1 / `##`×12 / `###`×18 → 取 `##` 这一级收到 12 条，Σ coverage = 13,959 / 14,120
# = 98.9%（缺的 161 字是文件导语，见 `_chapter_slices` 怎么处理它）。
#
# 默认**关**（写法与 `MINDMAP_GRAPH_SKELETON` 一致）：它是 A/B 的一臂，而且这条路把
# 结构那一次调用换成 N 次，代价是实打实的 token。翻默认值之前先要有一组数字。
SECTION_SLICE_ENV = "MINDMAP_SECTION_SLICE"

# 每章进 prompt 的原文字数上限。取值不是配平，是两个数：
#
#   - 这份材料的章长是 144–2,894 字（实测 `docs/RAG-DECISIONS.md` 的 12 章），所以这个
#     上限在真实材料上**基本不触发**；它防的是"一章一万多字"那种极端章（`docs/PLAN.md`
#     最长一章 11,697 字）；
#   - 8,000 字按 `estimate_input_tokens` 的 1.5 字/token 折算是约 5.3k token，加上
#     system 与主题，一次调用约 7k token 输入——离 `CONTEXT_TOKEN_LIMIT`（二十万）很远。
#     所以这个上限守的不是"装不下"，而是"给一章 5k token 的原文已经够它写出这一章的
#     所有要点，再多买到的是注意力分散"。
SECTION_SLICE_MAX_CHARS = 8_000

# 每支（每章）能写几个节点：**平分**，不是"自由写然后裁"。
#
# `INITIAL_MAX_NODES = 80` 是全树的上限，而 12 支各写一棵子树时谁都不知道别人写了
# 多少——自由写的总量会到 200+，然后 `_clip_tree` **按前序裁尾巴**：被整支砍掉的恰好
# 是最后几章，于是"每章覆盖率"在尾部直接归零，而尾部归零正是这次要消掉的失效形态。
# 平分让每章先拿到确定的份额（80 - 1 根 - 12 支 = 67，67 // 12 = 5 个后代/支），
# 总量恒在上限之内，覆盖率也就有了可解释的分母。
SECTION_SLICE_MIN_PER_BRANCH = 3

# 每支子树最多几层（**不含分支本身**，即分支下还能有几层后代）。
# `INITIAL_MAX_DEPTH = 3` 是 root 之下的层数：root(第 0 层) → 分支(第 1 层) → 两层
# 后代正好用完。新路不请求两级骨架（分组会再吃一层），所以这一笔账是干净的。
SECTION_SLICE_DEPTH = 2

# 同时发几条请求。12 章串行按单次 5 秒算是 60 秒；四路并发是 3 轮。不取更大：
# DeepSeek 有 RPM 约束，而这条路本来就要付 N 次调用的钱——把并发拉到 8 换来的墙钟
# 收益，抵不上被限流之后重试的代价。
SECTION_SLICE_CONCURRENCY = 4


def section_slice_enabled() -> bool:
    """按章切开关。默认**关**：A/B 要能同一条命令切换（`MINDMAP_SECTION_SLICE=1`）。

    默认关的第二个理由与 `graph_skeleton_enabled` 不同：这条路不依赖任何外部服务，
    但它**改变调用次数**（4 次变 4+N 次）。一个改变账单的默认值不该悄悄生效。
    """
    return os.getenv(SECTION_SLICE_ENV, "").strip().lower() in GRAPH_SKELETON_TRUTHY


@dataclass(frozen=True)
class _Chapter:
    """一章原文——这条路**唯一**的输入单位。

    `start` / `end` 是**原文里的绝对偏移**（码点），与 `SourceRef.start` 同一口径，
    所以模型抄回来的 quote 可以直接在这一段里定位、直接写成引用坐标。
    """

    index: int          # 1 起：提示词里的"第 N 章"与节点 id 的序号都用它
    title: str
    source_id: str
    start: int
    end: int
    chars: int          # 这一章原始字数（`end - start`，含被截掉的那部分）
    prompt_text: str    # 实际进 prompt 的那一段（超上限时是前缀）
    truncated: bool


def _chapter_slices(sources: Sequence[Source]) -> list[_Chapter]:
    """把材料切成**每章一段**。空列表 = 这条路这次走不了（调用方退回现状）。

    三条前置条件，任一条不成立都返回空，**不报错**（与 `_graph_outline` 的降级同一条
    原则：能力不可用时沉默退回改动之前的行为）：

      - **恰好一份材料**：`OutlineEntry.start` 是"那一段文本内部"的偏移，多份材料时
        这个数无法归属——拿 B 材料的偏移去切 A 材料，切出来的是一段随机文本；
      - 目录可用：`heading_outline` 非空，覆盖率 ≥ `OUTLINE_MIN_COVERAGE`——与
        `_skeleton_outline` 拼进骨架 prompt 的是**同一份**判据，否则"骨架按目录分、
        按章切按另一份目录切"，两边对不上；
      - 至少两章：一章就是"整份材料"，那不叫切章。

    **第 1 章吃掉文件导语**（`[0, 第 1 章标题)` 那 161 字）：`coverage` 从标题算起，
    导语不属于任何一章，而它常常写着"本文的结论是…"。不补的话全文总有 1% 的内容
    任何一支都拿不到——`outline_coverage` 报的 98.9% 就是这么来的，这个函数把它补成
    100%，于是"每章覆盖率"的分母是全文而不是"被目录管住的那部分"。
    """
    usable = [source for source in sources if (source.text or "").strip()]
    if len(usable) != 1:
        return []
    source = usable[0]
    text = source.text or ""
    entries = heading_outline([source])
    if len(entries) < 2:
        return []
    if outline_coverage([source], entries) < OUTLINE_MIN_COVERAGE:
        return []
    chapters: list[_Chapter] = []
    for index, entry in enumerate(entries):
        # 第 1 章从 0 开始（吃掉导语），其余从自己的标题行开始。
        start = 0 if index == 0 else entry.start
        end = min(len(text), entry.start + entry.coverage)
        if end <= start:
            continue
        body = text[start:end]
        truncated = len(body) > SECTION_SLICE_MAX_CHARS
        chapters.append(_Chapter(
            index=len(chapters) + 1,
            title=entry.title,
            source_id=source.id,
            start=start,
            end=end,
            chars=end - start,
            prompt_text=body[:SECTION_SLICE_MAX_CHARS],
            truncated=truncated,
        ))
    return chapters


def _slice_budget(chapters: int) -> int:
    """每支子树能写几个节点（含分支节点自己）：平分 `INITIAL_MAX_NODES`。

    `80 - 1 - N` 是"扣掉根与 N 个分支之后剩下的"，再按 N 平分——所以 12 章时每支拿到
    6 个（分支 + 5 个后代），12 × 6 + 1 = 73 ≤ 80。下限 `SECTION_SLICE_MIN_PER_BRANCH`
    兜的是"章多到平分下来不够写"的情形（30 章时平分只有 1 个），这时宁可口子略大、
    交给既有的 `_clip_tree` 收尾，也不让某一章只剩一个光秃秃的标题。
    """
    if chapters <= 0:
        return SECTION_SLICE_MIN_PER_BRANCH
    share = (INITIAL_MAX_NODES - 1 - chapters) // chapters
    return max(SECTION_SLICE_MIN_PER_BRANCH, share)


# ---------------------------------------------------------------- 一级分支条数：不设限
#
# **这一次要几条一级分支，代码不给数。** 这是产品决定，不是默认值的口味。
#
# 被它替掉的那套机制是"提示词里写一句最多 N 条"，而那个 N 只有一个来源可用——模型
# 在**读到材料之前**自己填的工具参数（`GenerateDeepArgs.branches`，字段默认值 5）。
# 于是三次生成三次被同一句"最多 5 条"框住，实战退化成"root → 5 条竖链 × 3 节点，一个分叉都没有"，
# 而材料里明明有十几个方面。数的来源是预感，作用却是上限——方向一开始就错。
#
# 现在改成**给判据不给数**：材料里有几个方面，就长几条分支。规模因此由材料自己
# 决定（有材料时还叠着“每个节点必须有引用”那一条——找不到依据的方面写不成节点），
# 代码只守两条与内容无关的边界：`MIN_BRANCHES`（少于 3 条不成图，是结构下限）与
# `DEEP_MAX_BRANCHES`（画布承载能力）。
#
# 另一半在 `NO_BRANCH_CAP_RULE` 的后半句：上限一松，最容易出的错从“为凑数硬拆”
# 变成“为整齐硬合”。两个错在同一次调用里会互相拉扯，判据必须把两头都说到。
NO_BRANCH_CAP_RULE = (
    "按内容实际包含的方面定条数：层级多就用足，内容单薄就少给；"
    "不要为了凑数把不同的事塞进同一支，也不要为了整齐把不同的事合成一支。")


# 不设上限时替掉「这一次要多大」那一句的**判据**。
#
# 它替掉的是 `generation.service._INITIAL_SCALE_LINE`：「第一次生成本身请控制在 80 个节点、
# 4 层（含中心主题）以内：宁可少两层，也要让每一层都写完整。」——三个数（节点、
# 层数、以及“宁可少两层”那个取舍）全是配额口径。
#
# **为什么是判据而不是一个更大的数**：配额问的是“能写多少”（答案是模型的填额
# 目标），而这一轮要的是“有多少能写”：有材料时由「每个节点必须有引用」约束（找不到依据的
# 方面写不成节点），纯主题时由单次输出预算约束。两条都不需要提示词里有一个数。
#
# 层数那一半也一样：契约层的 `MAX_TREE_DEPTH=8` 照旧生效（`DETAIL_PROMPT` 自己就写着
# “层级上限 8 是可读性上限，不是要拆到的目标”），而“本次 4 层”那一个是配额，走人。
#
# 后半句“更细的内容留给用户点开某个节点后再展开”原样保留：它不是配额，是产品
# 分工（`node_expand` 那条路确实存在，而界面上一棵一次长到 8 层的树没人读得完）。
SCALE_BY_MATERIAL_RULE = (
    "这一次写多少节点、拆到几层，都由内容决定：还能拆出更具体的方面就继续拆，"
    "拆不动了就停；不要为了少写几个就把两件事合成一个节点。")

#: 去掉配额那句之后的 `DETAIL_PROMPT`：整树那一路的 system 就是它。
#:
#: 为什么不直接改 `generation.service` 里那份常量：它被 `tests/test_budget.py` 按段断言
#: （`scaled.replace("32 个节点", "80 个节点") == DETAIL_PROMPT`），而它另一半（`MINDMAP_NODE_SCALE`
#: 那套实验臂）这一轮不动——它仍是“配额是多少”的唯一实现，留着给测量脚本
#: 与将来的对照用。**变的是生成路径**：那一路的 system 是在本模块拼的，所以换句子发生在这里。
#:
#: 两个匹配锚点是刻意的：它们依赖 `DETAIL_PROMPT` 的措辞，而那是一份公共常量。措辞一改
#: 就必须在这里**失败**而不是静默地什么都不换：静默的后果是“提示词里又出现了
#: 一个数”，而那正是这一轮唯一要消灭的东西。
_SCALE_SENTENCE_HEAD = "第一次生成本身请控制在 "
_SCALE_SENTENCE_TAIL = "更细的内容留给用户点开某个节点后再展开。"


def _open_scale_rule(prompt: str) -> str:
    """把 `prompt` 里那句配额换成 `SCALE_BY_MATERIAL_RULE`。锚点不在就报错。"""
    head, sep, tail = prompt.partition(_SCALE_SENTENCE_HEAD)
    if not sep:
        raise RuntimeError(
            "生成提示词里已经没有「" + _SCALE_SENTENCE_HEAD +
            "」了：`generation.service` 那一句改了措辞，同步更新 `tools._open_scale_rule`")
    _body, sep2, rest = tail.partition(_SCALE_SENTENCE_TAIL)
    if not sep2:
        raise RuntimeError(
            "生成提示词里已经没有「" + _SCALE_SENTENCE_TAIL +
            "」了：同步更新 `tools._open_scale_rule`")
    return head + SCALE_BY_MATERIAL_RULE + sep2 + rest


#: 整树那一路的 system 里「这一次要多大」那句的定稿。`_generate` 取它，不再取
#: `detail_prompt(...)`——后者的每一个分支都带一个数。
#:
#: 取 `detail_prompt()` 的返回值而不另 import `DETAIL_PROMPT` 常量：那个函数的默认
#: 分支（`node_limit is None`）**返回的就是常量本身**（`is` 同一性有用例钉着），
#: 所以两次拿到的是同一份文本，而这里少一个跨模块的常量依赖。
DETAIL_PROMPT_OPEN = _open_scale_rule(detail_prompt())


def _plan_ask(
    topic: str, *, outline: str = "", materials: str = "",
    diagram_type: str = "tree",
) -> str:
    """骨架调用的 user 消息。**条数那一段永远是判据，不是数**（`NO_BRANCH_CAP_RULE`）。

    这个函数以前收一个 `branches`，把"最多 N 条"写进 user。撤掉它的理由见
    `NO_BRANCH_CAP_RULE` 上面那一段：那个 N 的唯一来源是模型在读到材料之前填的
    工具参数，拿预感当上限。现在没有这个参数，也就没有"某次调用忘了传 None 就
    退回配额"这条路——不设限是常量，不是一个可以被悄悄绕过的默认值。

    单条长度、互不重叠、逻辑顺序三条照旧重申：system 里虽有，但这是决定骨架质量的
    三条硬约束，重复一次便宜，骨架歪了整张图都歪。

    `outline` 是材料自带的章节（`_skeleton_outline` 的产物），**放在主题之后、
    判据之前**：顺序即权重，先给它看材料的结构，再让它按结构定条数——反过来先
    见到"按方面定条数"时，它倾向先数一个数再找话说。空串表示这份材料没有可用目录，
    那一字不加（与加目录之前逐字相同）。

    `materials` 是材料清单（`_material_inventory` 的产物：份数 / 每份文件名与字数），
    位置与 `outline` 同一条理由、**紧随其后**：它也是"材料长什么样"，只是比目录更粗
    ——目录说"有哪几块"，清单说"有几份、各多长"。空串表示这次没有材料（纯主题），
    那一字不加（与加清单之前**逐字节相同**，有回归用例钉着）。

    清单之后必须紧跟 `INVENTORY_RULES` 那一句：只给数不给判据时，模型会"看了但
    不用"（实测措辞与依据见那个常量）。两句一起出现、一起消失——半截的清单等于
    一个没有用处的数字。

    `diagram_type` 只影响末尾补的那一两句：`tree` 时**一个字符都不加**（有逐字节
    回归用例钉着），`flowchart` 补"阶段顺序 + edges 只连骨架"，`fishbone` 补
    "六个类别固定"。system 里的图型差异（`FLOW_PLAN_PROMPT` 等）与这里的分工是
    「规则在 system，这一次的重点在 user」——与 `PLAN_PROMPT` / 本函数的既有分工
    一致。
    """
    parts = [f"主题：{topic}"]
    if outline:
        parts += ["", outline, ""]
    if materials:
        parts += ["", materials, ""]
    parts.append(NO_BRANCH_CAP_RULE)
    if materials:
        parts.append(INVENTORY_RULES)
    # 这个数必须与系统提示词和代码截断同源（`GENERATED_LABEL_MAX_CHARS`）。
    # 这里曾经写死 20：`PLAN_PROMPT` 说 16、`_skeleton_titles` 按 16 截、
    # 而 user 消息说 20——模型照 user 写，写出来就被截，用户看到的是
    # 一级分支标题从中间断掉。三条口径现在都读同一个常量。
    parts.append(
        f"每条不超过 {GENERATED_LABEL_MAX_CHARS} 字，互不重叠，按逻辑顺序排列。")
    if diagram_type == "flowchart":
        parts.append(
            "这次是流程图：一级分支按**执行先后**排列，每条是一个流程阶段；"
            "有判断出口或回退时，用 edges 补上（source/target 只写阶段序号）。")
    elif diagram_type == "fishbone":
        # 类别可变那一版（`MINDMAP_FISHBONE_FREE_CATEGORIES`）：user 这末尾的一两句是
        # "这一次的重点"。关着时加的是原来那一句，逐字节不变——它被
        # `test_edges.py::TestTreeIsByteIdentical` 的兄弟用例与 `test_agent_shapes.py`
        # 的鱼骨用例读着。
        if fishbone_free_categories_enabled():
            parts.append(
                "这次是鱼骨图：一级分支是**这道题的成因面**，不是固定的六类——"
                "按 system 里的判据自己定 3 到 6 个（每个 1 到 4 字、名词性、彼此不重叠，"
                "要能填进「（这个分支）出了偏差 → 主骨上那个为什么」），"
                "并覆盖四个成因面里的至少三个；不要输出 edges；"
                "同时给出 problem——**先写它**，再拿它校准一级分支。"
                "problem 是**主骨上那一句**「为什么 X 会 Y」，40 字以内，"
                "不要复读主题，也不要写成原因或名词短语。")
            # 用户点名过一套类别时，**以用户为准**。这条路以前不存在（请求层没有类别
            # 字段、`diagram_intent` 只认图型词），所以它是"开关打开之后比关着时多出来
            # 的那一件事"，必须紧跟在那句之后说——位置即权重。
            named = named_fishbone_categories(topic)
            if named:
                parts.append(
                    "主题里点名了一套类别（" + " / ".join(named) + "）："
                    "一级分支**逐字就用这一套**，顺序也不改，不要自己另立；"
                    "problem 仍由你按主题定。")
        else:
            parts.append(
                "这次是鱼骨图：一级分支固定为人 / 机 / 料 / 法 / 环 / 测 六条，不要输出 edges；"
                "同时给出 problem——**主骨上那一句**「为什么 X 会 Y」，40 字以内，"
                "不要复读主题，也不要写成原因或名词短语。")
    return "\n".join(parts)


# 按章切第二步：**一次调用只写一章的子树**。
#
# 与 `BRANCH_EXPAND_PROMPT`（分片那条路的逐支展开）的差别是输入侧，不是输出侧：
# 那边给的是"这一支的检索命中"，这里给的是"这一章的完整原文"。产出形状也不同——
# 那边只要一层（`{"children":[...]}`，字符串或对象都收），这里要能表达"这一章里
# 还有下级"，所以是嵌套的 `children`，层数由 `SECTION_SLICE_DEPTH` 夹住。
#
# 三条硬要求都写在 system 里而不是 user 末尾，除了**引用**那一段：引用规则必须紧挨着
# 输出位置说（`_generate` 的"全塞"那一段与 `_detail_ask` 的自查句是同一个教训——
# system 里那句距离输出隔着一万字时，模型一条引用都不写）。
SECTION_SLICE_PROMPT = (
    "你在为一棵思维导图的**一条一级分支**写它下面的结构。这一次只写这一支，"
    "别的分支由别的调用各自去写，你看不到、也不需要管它们。"
    "只返回一个 JSON 对象，不要代码围栏，格式为 "
    '{"children":[{"label":"要点一","children":[{"label":"细节"}]},'
    '{"label":"要点二"}]}。'
    # 层级与深度必须说清：给的是"这一章"的原文，而一章里通常有一个 `###` 级的结构，
    # 模型不被告知上限时会一路拆到叶子，把一层的预算摊成四层。
    "children 里可以再套 children 表达下级，但**最多两层**："
    "第一层是这一章的主要方面，第二层是那方面的具体要点。"
    "每个节点的 label 是一句话短语，不超过 "
    f"{GENERATED_LABEL_MAX_CHARS} 字，不带句号。"
    # 与 `_SKELETON_BODY` 逐字同源的那一条：这一轮不写说明，说明由第三段统一写。
    "**这一轮不要写 detail**：每个节点都不要带 detail 字段，说明由紧接着的一次调用"
    "统一补写。省下来的输出额度用在层级与要点上。\n"
    # "只写这一章有依据的内容"是这条路与分片最大的行为差别，必须明写：模型知道输入
    # 是"某一章的原文"之后，倾向会把这一章里没提的东西也按自己的知识补进来（补出来
    # 的内容没有引用可挂，覆盖率的分母就白算了）。
    "只写这一章原文里**确有依据**的内容：原文没提到的方面不要凭印象补，"
    "宁可这一支少几个节点，也不要把别处的内容搬进来。"
    "同级之间不要写同一个标题（重复的说法会被画成两块）。\n"
)

# 有材料时**每个节点都必须有引用**。这一句是三条引用规约（`EVIDENCE_REF_FIELDS` /
# `FULL_TEXT_EVIDENCE_RULES` / `SECTION_SLICE_REF_RULES`）共用的那半句，抽出来是因为
# 它替掉的正是那三条里各写一遍的**逃逸口**：「只有你自己补充的内容才给空数组——不挂
# 引用是对的，编造不是」。
#
# 那句话在语义上没错（不编造引用确实比编造好），但它给了一个**恰好模型最爱的出口**：
# 只要它认为某个节点"这是我补充的"，就可以不挂引用。实测（碳中和那棵，16 个节点）
# 只有 5 个节点有引用 = 31%，而模型并没有编造——它只是几乎每个节点都自称"补充"。
# 引用的作用是**约束**，不是**记录**：一旦允许留空，节点数就不再受材料约束，回到了
# 没有依据也能长的那条路上。
#
# 所以现在只留一种正确做法：**材料里找不到依据的方面，不要写成一个节点**。这同时完成
# 了"规模由材料决定"那件事——材料 424 字，可引的段落就那么几处，节点自然少；材料
# 66,000 字，可引的多，节点自然多。限额外挂一个数（`INITIAL_MAX_NODES`）反而是多余的，
# 因为它量的是"能写多少"，而这一句量的是"有多少能写"。
#
# 措辞里三处不是随便写的：
#   - **"每一个节点"** 而不是"凡是内容出自材料"：后者要求模型先自判"这段算不算出自
#     材料"，而它每次都判成"算我补充的"；
#   - **"宁可这一支短"**：不给这条出路，模型会为了保住分支数去凑节点——那正是它编造
#     引用的起点。短是可以接受的；
#   - **"不要写一个没有出处的节点"**：把话说在**动作**上（写/不写节点），而不是在字段上
#     （给/不给引用）。字段可以留空，节点不能凭空存在。
MUST_CITE_EVERY_NODE = (
    "**每一个节点都必须至少有一条 source_refs**。"
    "材料里找不到依据的方面，不要写成一个节点——宁可这一支短，"
    "也不要写一个没有出处的节点。")

# 引用规则：**拼在** system 之后。与 `FULL_TEXT_EVIDENCE_RULES` 同源，只改一处——
# 范围从"全部材料"收窄到"这一章"，因为模型的输入本来就是这一章，而"只能从本章抄"
# 是这条路唯一能在机制上成立的引用约束（跨章的句子它根本看不见）。
SECTION_SLICE_REF_RULES = (
    "\nuser 消息末尾是**这一章的完整原文**（不是检索片段）。"
    '引用写在节点自己的 source_refs 数组里，例如 {"source_id":"…","quote":"…"}；'
    "source_refs 与 label 平级，不要写进任何文字字段。"
    "quote 必须**逐字**照抄上面原文里连续的一段（不超过 300 字），"
    "不要改写、不要概括；start / end 不用写（服务端会按这一章的范围定位并补上），"
    "page 不用写。" + MUST_CITE_EVERY_NODE.replace("材料", "这一章") + "\n"
)


def _slice_ask(
    topic: str, chapter: _Chapter, *, budget: int, depth: int,
) -> str:
    """按章切的 user 消息：主题 + 这一支是第几章 + 这一章的原文。

    顺序刻意是「主题 → 这一支是谁 → 原文 → 收尾要求」：与 `_plan_ask` 把目录放在
    主题之后同一条理由——先给它身份与范围，再给它材料。收尾要求（预算与引用）放在
    原文**之后**，因为模型对紧挨着输出位置的那句话最敏感。

    `budget` 是**含这一支自己**的节点数上限，`depth` 是分支下还能有几层：两个数都
    来自代码侧的预算（`_slice_budget` / `SECTION_SLICE_DEPTH`），不写进提示词时模型
    会按"这一章看起来有多少内容"自由发挥，12 支的总量就没人守得住。
    """
    descendants = max(1, budget - 1)
    tail = [
        f"\n这一支（含它自己）最多 {budget} 个节点，也就是下面最多写 {descendants} 个，"
        f"最多 {depth} 层；宁可少写几个，也不要把一个要点拆成两句。",
    ]
    if chapter.truncated:
        tail.append(
            f"（这一章原文共 {chapter.chars} 字，为控制单次输入只给了前 "
            f"{SECTION_SLICE_MAX_CHARS} 字；没给到的部分不要凭空补。）")
    return (
        f"主题：{topic}\n\n"
        f"这一支是材料「第 {chapter.index} 章」：{chapter.title}\n"
        f"（本章 {chapter.chars} 字，source_id={chapter.source_id}）\n\n"
        "下面是这一章的全部原文：\n\n"
        f"{chapter.prompt_text}\n"
        + "\n".join(tail)
    )


# 分片生成第二步：按骨架逐支展开。与 `EXPAND_PROMPT` 的差别只有上下文——
# 那个服务于"用户选中某个节点再展开"，语义保持不动；这个要额外告诉模型它写的是
# 哪一支、以及别越到同级别的分支上去（并发展开时它看不到其他分支写了什么）。
#
# 格式从 `{"points":[字符串]}` 改成 `{"children":[{"label":...}]}`：纯字符串数组
# 里没有层级，产出永远是两层。对象形式让"这一支还有下级"有地方表达，也就是
# `expandable`——它在模型写这次响应的同时顺手判一句，**不额外花任何调用**，代码
# 按它决定还要不要追问下一层（见 `_walk_slots`）。旧格式仍被接受，只是收下的
# 节点按叶子处理。
BRANCH_EXPAND_PROMPT = (
    "你在给思维导图的一条一级分支补充子要点。只返回一个 JSON 对象，不要代码围栏，"
    '格式为 {"children":[{"label":"要点一","detail":"这个要点的说明","expandable":true},'
    '{"label":"要点二","detail":"这个要点的说明","expandable":false}]}。'
    "只写一层，不要在 children 里再套 children。"
    # 两层节点：label 短、detail 长。这一条与 `DETAIL_PROMPT` 里那句同源，但
    # 必须各自写一遍——两份提示词是两条独立的路（整树 vs 分片），共用一句会
    # 让其中一条在改动时被静默漏掉。
    f"label 是一句话短语，不超过 {GENERATED_LABEL_MAX_CHARS} 字，不带句号；"
    "detail 是 2–3 句说明，可以带数字与条件，不超过 2000 字。"
    "每个子项都必须带 expandable 字段，它是布尔值：这个子项还能不能拆出更具体的"
    "下级。如果它已经是最小可执行单元、再拆只会重复它自己，就填 false；"
    "如果它还是一句概括、下面明显还有具体内容可写，就填 true。"
    "如实判断，不要为了凑数硬拆，也不要一律填 true。"
    # 视觉元素只属于一级分支。子节点上写出来会被忽略（`_slot_from` 不读这两个
    # 字段），所以这里要明确说"别写"——比让它猜"为什么我写的图标不见了"便宜。
    "不要给子项写 color 或 icon：视觉元素只用在骨架的一级分支上。"
    "不要编号，不要解释，只写这条分支范围内的内容，不要重复同级其他分支，"
    "不要编造具体数据、人名或文献。"
)

# 分片生成第三层往下：展开一个已经有上下文的节点。与上面的差别只在"它是谁"——
# 这一层并发展开时，模型看不到兄弟节点写了什么，只给一个 label 会让它写出与上一级
# 同一句话的内容，所以 user 里要带一条从一级分支到这里的路径。
# 同样只要求一层：实测一次要三层，写到第二段之后就开始敷衍甚至被输出上限截断。
# `expandable` 与上一层同义：`max_depth` 大于 3 时，某一支该不该继续深挖同样由
# 模型的判断决定，代码不再一律拉到上限。
SUBNODE_EXPAND_PROMPT = (
    "你在给思维导图里一个已有的节点补充下一层子节点。只返回一个 JSON 对象，"
    '不要代码围栏，格式为 {"children":[{"label":"要点一","detail":"这个要点的说明",'
    '"expandable":true},{"label":"要点二","detail":"这个要点的说明","expandable":false}]}。'
    "只写一层，不要在 children 里再套 children。"
    f"label 是一句话短语，不超过 {GENERATED_LABEL_MAX_CHARS} 字，不带句号；"
    "detail 是 2–3 句说明，可以带数字与条件。"
    "每个子项都必须带 expandable 字段，它是布尔值：这个子项还能不能拆出更具体的"
    "下级。已经是最小可执行单元、再拆只会重复它自己的填 false；"
    "明显还有具体内容可写的填 true。如实判断，不要为了凑数硬拆。"
    "不要给子项写 color 或 icon：视觉元素只用在骨架的一级分支上。"
    "不要编号，不要解释，"
    "只写这个节点范围内的内容，不要重复同一父节点下其他节点的说法，"
    "不要编造具体数据、人名或文献。"
)


# 分片 + 按支检索：有证据时的附加规则。**拼在**上面两份提示词之后，不改基础格式
# ——改基础格式等于给"这一支没有证据"那条路也塞一个 `source_refs` 字段，而那条路
# 上一个字都不该抄。
#
# 逐字照抄那两句与 `rag_query` 返回的形状同源（同一套 `_citable_hits` 渲染），
# 但不能复用一份提示词：`rag_query` 回给模型的是一串裸 JSON、它知道来自哪个工具；
# 这里模型手里已经有一支的命中，措辞必须指向本次 user 消息末尾的 evidence 数组。
#
# 内容与出处分两句说（与 `generation.service.CONTENT_FREEDOM_CONTRACT` 同一契约）：
# 上一版只写了"引用只能从本支的 evidence 里抄…找不到依据就给空数组"，模型把前半句
# 读成了内容范围，于是 evidence 没覆盖的方面整个消失——覆盖率就是这样掉下来的。
BRANCH_EVIDENCE_RULES = (
    "这一支的检索结果在 user 消息的 evidence 数组里：每项含 source_id、quote"
    "（父块原文）与 evidence_start / evidence_end / evidence_quote（真正命中的那一小段）。"
    "内容可以自由展开：evidence 没覆盖的方面按你的知识补全，不要留空节点。"
    '每个子项可以带一个 source_refs 数组，例如 {"label":"要点二","expandable":false,'
    '"source_refs":[{"source_id":"…","start":0,"end":12,"quote":"…"}]}。'
    # 引用是**节点**的字段，与 label / detail 平级。写进 detail 的引用不会被
    # 收尾的逐字校验看到（它只读 `node.source_refs`），界面上也就点不开。
    "source_refs 与 label、detail 平级，不要写进 detail 的文字里。"
    "但凡引用、转述、概括材料的，出处只能从**本支**的 evidence 里抄：source_id 照抄；"
    "优先用 evidence_* 那一组，start、end、quote 分别取它们的值；没有 evidence_* 时"
    "只抄 quote 里连续的一段（不超过 300 字），这时不要写 start 和 end。"
    "引文必须逐字照抄，不要改写、不要概括、不要自己算偏移，也不要写 page。"
    "自己补的内容 source_refs 给空数组——不挂引用是对的，编造不是。"
    # 「这一支要不要再查一次」是模型唯一比代码多知道的事：它刚写完这些论断，只有
    # 它知道哪些地方缺依据。信号走响应里的一个布尔字段，不额外花一次判断调用
    # （判据与限额见 `SUPPLEMENTARY_*`）。
    "如果你判断这一支里有关键论断需要材料依据、而 evidence 不足以支撑，"
    '在响应里带一个与 children 同级的字段 "needs_evidence": true，'
    "系统会为这一支再检索一次。\n"
)

# 这一支没有检索结果时的附加规则。**不能什么都不说**：模型看不到这句话时，会把
# "我没拿到证据"读成"材料里大概还有"，于是按主题自由展开，产出一棵看起来完整、
# 实际没有出处的子树——那正是这次改动要消灭的静默缺失。
#
# 上一版的结论是"如实留空、返回空 children"，代价是 8 支里有 5 支变成空壳：
# 材料里真有这块内容却没被这一支的问句召回，是检索的常态（top_k=3），把它判死等于
# 让检索质量决定导图完整度。新契约只保留"不许冒充材料"那一半：照常展开写满，
# 引用一律留空。
BRANCH_NO_EVIDENCE_RULES = (
    "注意：这一支这次**没有检索到材料依据**（evidence 数组为空）。"
    "未检索到不等于材料里没有——照常把这一支展开写满，不要留空节点、"
    "也不要在节点里写「材料中没有相关内容」。"
    "source_refs 一律给空数组：没有依据可标时挂一个引用就是编造。"
    '如果你判断这一支里有关键论断需要材料依据，在响应里带一个与 children 同级的'
    '字段 "needs_evidence": true，系统会为这一支再检索一次。\n'
)


# ---------------------------------------------------------------- 整树一次生成
#
# `_generate` 与 `_generate_deep` 的区别只有**生成的调用次数**：骨架、检索、
# 收尾校验三件事是同一套（`_intake` / `_skeleton_*` / `_retrieve_branches`）。
# 所以下面这几段只描述"这一次的一级分支已经由上一次调用定好了"，不复述树规则
# ——树规则在 `DETAIL_PROMPT` 里，只此一份。

# 骨架必须出现在**生成**那一次的 user 消息里。不写这一句时模型会按
# `DETAIL_PROMPT` 自己的"3-6 条一级分支"重排一遍：骨架那次调用的产物（分支名、
# 颜色、flowchart 的 edges 端点）在树上找不到对应节点，等于那一整次调用白花，
# 而用户看到的是"我选的 8 条变成了 5 条"。
#
# "不要重复其他分支"这一条**代码兜不住**（跨支重复只能靠模型自己看全局），
# 这也正是整树一次生成相对分片的结构优势：分片展开时每一支只看得到自己那条
# 标题链，兄弟支写了什么它不知道（`PLAN_PROMPT` 里那句"彼此覆盖的范围不重叠"
# 模型并不听）；这里全部骨架与全部证据在同一次响应里，它至少有可能看住。
_SKELETON_ROOT_RULE = (
    "\n这次的一级分支已经定好了，就是 user 消息里列出的那几条："
    "根节点下必须恰好是这几条，顺序、名称都不改，不增不减。"
)
_SKELETON_BODY = (
    "每条分支只写它标题说的范围，同一件事不要挂在两条分支下——"
    "重复的内容会被画成两块，用户还要自己删一个。"
    # 泛化标题也会被判成重复（`_cross_branch_duplicates` 只做空白折叠，不做语义归一，
    # 这是"宁可漏、不误报"的那一半）：实测 6 次采样里有 4 次出现同一个「典型应用」
    # 挂在三条分支下，那 3 个节点全部计入跨支重复。所以这里点名要求换一个更具体的
    # 标题——它同时也让说明更好写（"典型应用"这四个字本身没有可写的内容）。
    "不要在多条分支下挂同一个泛化标题（「典型应用」「常见问题」「训练与评估」这类）："
    "需要交叉指认时换成更具体的写法，或者直接指向已有节点。\n"
    # **这一轮不写说明**：说明由紧接着的第三段统一写（`DETAIL_ONLY_PROMPT`）。
    #
    # 上一版在这里写的是"每个节点的 detail 不少于 60 字"，实测无效（deepseek-chat：
    # 加之前 detail 平均 45/28 字，加之后 28.5/26.2 字，反而更短）——瓶颈不是提示词，
    # 是一次输出里要塞几十段说明，注意力被摊薄，而且越写到后面越短（第 1 支 27.5 字
    # → 第 5 支 23.6 字）。所以这里改成**明确不写**：结构那一轮只花 token 写 id 与
    # label，说明交给一次只写说明的调用（数字见 `docs/ONE-CALL.md` 的三段式一节）。
    #
    # 必须写成"空字符串"而不是"写短一点"：后者模型会写一句短语，而那些短语既不会
    # 被第三段覆盖（`_needs_detail` 的判据是长度，一句短语通常仍在阈值下，但口径
    # 就模糊了），也解释不了"为什么这一段这么短"。
    "\n**这一轮不要写 detail**：每个节点的 detail 一律给空字符串 \"\"，"
    "说明由紧接着的一次调用统一补写。省下来的输出额度用在结构上："
    "该拆的层要拆够、每条分支的要点要写全。\n"
)

# 扁平的完整规则 = 根规则 + 共用的那两段。拆开的理由是两级骨架**只换根规则**
# （根下挂的是分组、不是分支），而"不要跨支重复"与"这一轮不写 detail"必须逐字共用：
# 同一个约束在一处改了、另一处没改，两条路的产出就会在某个细节上分叉，而这种分叉
# 从产出上看不出来。
SKELETON_TREE_RULES = _SKELETON_ROOT_RULE + _SKELETON_BODY

# 两级骨架的**根规则**：根下挂的是分组，分组下挂的是分支。
#
# 三条都得说，各对应一种会真出错的行为：
#   - 模型把分组与分支摆成同一层（等于没分组，图还是二十条宽）；
#   - 给分组再挂"自己的要点"（分组是分类名，它一旦有内容，二十节里就会有一节被它挤掉）；
#   - 分组算掉一层，于是分支下面还想再拆（第一次生成的上限是 4 层含根，
#     `INITIAL_MAX_DEPTH=3` 到这里正好用完）。
_GROUPED_ROOT_RULE = (
    "\n这次的一级分支已经定好了，**分成两级**（见 user 消息里那份缩进清单）："
    "根节点下必须恰好是那几个**分组**，每个分组下面必须恰好是它自己列出的那几支**分支**，"
    "顺序、名称都不改，不增不减。"
    "分组下面直接挂分支，不要在分组与分支之间再插一层，也不要给分组挂它自己的内容节点"
    "——分组是分类名，它的内容就是它下面那几支。"
    "层数是：中心主题 → 分组 → 分支 → 要点，正好 4 层（这已经是第一次生成的层数上限）；"
    "分支下面只写一层要点，不要再往下拆。"
)
GROUPED_TREE_RULES = _GROUPED_ROOT_RULE + _SKELETON_BODY

# ---- 流程图的结构规则 ---------------------------------------------------------
#
# 流程图的一级分支是**阶段**，二级是阶段内部的步骤，而"步骤里哪一个是判定"必须
# 由模型说出来：`Node.role` 在契约里，`Edge.kind` 的 `branch` 已经默认它的起点是
# 个判定，靠出边数去猜会让一个刚画了一条边的节点闪变（见 `Node.role` 的注释）。
#
# 根规则与 `SKELETON_TREE_RULES` 只差一个词（"分支" → "阶段"），但那一个词值得
# 单写一份：模型对"分支"的先验是话题分类，而流程图上阶段之间存在**方向**。
_FLOW_ROOT_RULE = (
    "\n这次的一级分支已经定好了，就是 user 消息里列出的那几个**流程阶段**："
    "根节点下必须恰好是这几个阶段，顺序、名称都不改，不增不减。"
)
_FLOW_BODY = (
    "每个阶段下面写它自己的步骤（1–4 个），按这个阶段里的先后排列，"
    "最多再往下拆一层，不要把一个阶段写成另一张导图。"
    "步骤写的是**做的那件事**，不要写成对阶段的解释。\n"
    # role 六值。写不进契约的值会被 422（`Node.role` 是 Literal），所以这里把
    # 取值集合写死，并给一个可对照的例子——只描述语义时模型会写 "judge" / "判断"。
    "\n**每个节点可以带一个 role 字段，说明它在流程里扮演什么**：\n"
    '- "decision"：需要判断的地方（「材料齐全吗」「金额超过十万吗」）。'
    "一个阶段里通常 0–2 个，不要给普通步骤标它；\n"
    '- "start"：流程的起点，只在第一个阶段的第一步上标一个；\n'
    '- "end"：流程的终点，只在最后一个阶段收尾的那个节点上标一个；\n'
    '- "data"：**输入或输出**的一步——这一步产出了材料、交出了单据、读进了金额，'
    "它就是一份数据（「录入报销单」「出具审查意见」「接收回执」）；\n"
    '- "database"：**读写存储**的一步——数据落进某个库/台账/档案，或从里面取'
    "（「写入审批台账」「查询员工档案」「归档到系统」）；\n"
    '- 普通步骤写 "step" 或者干脆不写这个字段。\n'
    "role 只能是 step / decision / start / end / data / database 这六个值之一。\n"
    # data / database 与"处理"的界线要一句话说清：模型很容易把"填表"当 data、
    # 把"保存"当 step，两种都会让平行四边形与圆柱变成装饰。判据只有一条——这一步
    # 的对象是**数据本身**（产出它、交出它、存下它、取出它）才标，处理业务规则
    # 的那一步仍然是 step。
    "判据只有一条：这一步**打交道的对象是数据本身**（产出 / 交出 / 存下 / 取出）"
    "才标 data 或 database；在数据之上做业务判断或加工的那一步仍然是 step。"
    "摄入与落库都算，只是别给一张图标满——通常每个阶段 0–2 个。\n"
    # 判定节点的两条出口。**没有这一段时 `branch` 边恒为 0**：role 标了，边没地方
    # 写，`FlowStage.edges`（阶段内的边）就一直是空的。目标写**节点标题**而不是 id
    # —— id 是模型自己编的，它经常写错；标题它刚写完，抄得准。解析时 id 也认
    # （见 `_resolve_branch_target`），所以两种写法都能落地。
    '\n**标了 decision 的节点必须写出它的两条出口**：加一个 branch 字段，例如 '
    '{"id":"…","label":"材料齐全吗","role":"decision",'
    '"branch":{"yes":"转入审查","no":"通知补正"},"children":[]}。\n'
    "yes / no 的值写**目标节点的标题原文**（同一份响应里某个节点的 label），"
    "它可以是这个阶段后面的某一步，也可以是另一个阶段里的一步——判定本来就常常"
    '跨阶段分支。流程在这里结束时写 "结束"。\n'
    "**两条都要写**：缺一条，画布上那个判定就只有一个出口，读图的人看不出另一条路"
    "去哪。也不要两条写成同一个节点（那是同一条线，等于只有一条分支）。\n"
    + _SKELETON_BODY
)
FLOW_TREE_RULES = _FLOW_ROOT_RULE + _FLOW_BODY

# ---- 鱼骨图的结构规则 ---------------------------------------------------------
#
# 鱼骨图上"骨下面挂的是什么"有唯一正确的答案：**原因**。模型最常见的写歪方式是把
# 它写成"这一类东西的介绍"（「人：指操作者」），那样六根骨就退化成了六个词条，
# 而鱼骨图要回答的是"为什么主骨上那个问题会发生"。这一段就是为这件事写的。
#
# `role` 明确不要：鱼骨图没有判定节点，`Node.role` 在这套结构里不参与任何判定
# （见 `Node.role` 的注释与 `docs/CONTRACT_V2.md`）。
_FISHBONE_ROOT_RULE = (
    "\n这次的一级分支已经定好了，就是 user 消息里列出的那六个**类别**："
    "根节点下必须恰好是这六个类别，顺序、名称都不改，不增不减。"
)
_FISHBONE_BODY = (
    "每个类别下面写的不是「这一类是什么」的介绍，而是**属于这一类的原因**："
    "一句话说清什么导致了主骨上那个问题（例如「新员工手册只有 PDF，找不到操作步骤」），"
    "不要写建议、不要写定义、不要写现象。每个类别 2–4 条，按影响大小排列。\n"
    "**不要给节点写 role**：鱼骨图没有判定节点。\n"
    + _SKELETON_BODY
)
FISHBONE_TREE_RULES = _FISHBONE_ROOT_RULE + _FISHBONE_BODY


def _group_plan_rules(sections: int) -> str:
    """两级骨架的提示词尾巴：拼在 `PLAN_PROMPT` **之后**，覆盖它的输出格式。

    为什么是"拼在 PLAN_PROMPT 之后"而不是另写一份两级骨架提示词：

      - `PLAN_PROMPT` 与 `_plan_ask` 有逐字节回归用例钉着（`test_edges.py` 的
        `TestTreeIsByteIdentical`），改一个字那条用例就红——而它守的是"扁平这条路
        一个字都没变"，把两级塞进去会同时毁掉这个保证和它对老行为的证明力；
      - `PLAN_PROMPT` 的后半段（16 字、互不重叠、逻辑顺序、color 只能取调色板、
        icon 一个 emoji、✅/❌ 对照）在两级的**二级分支**上完全适用，复制一份就是
        给自己留一处会漂的重复文字。

    代价是 system 里同时存在"一层格式"与"两级格式"两种要求，所以这一段必须以
    "覆盖"开头、给出完整的 JSON 形状与一个 ✅ 例子——只在末尾轻描淡写加一句
    "也可以分组"，模型会照前面那个更详细的格式写（实测里它执行的是格式的**最后**
    一份定义）。

    `sections` 是"这次大概有多少个方面"（目录行数 / 用户要的条数），用来把组数写成
    具体的数与每组支数区间——不给数字时模型会自己挑一个组数，20 支挤成 2 组或拆成
    12 组都出现过。
    """
    groups = _skeleton_group_count(sections)
    return (
        "\n\n**这一次请输出两级骨架**（下面这一种格式覆盖上面那一种）："
        '只返回一个 JSON 对象，形如 {"groups":[{"label":"基础与定义","color":"blue",'
        '"icon":"🧩","children":[{"label":"概念界定"},{"label":"发展脉络"}]},'
        '{"label":"工程实践","color":"teal","icon":"🛠","children":[{"label":"数据处理"},'
        '{"label":"部署"}]}]}。'
        f"分组是**一级**：这次大约 {sections} 个方面，请归成 {groups} 组"
        f"（{SKELETON_GROUP_MIN}–{SKELETON_GROUP_MAX} 组之间，每组 2–6 支，"
        f"合计不超过 {DEEP_MAX_BRANCHES} 支）。"
        # 自指，**不再指向 user**：`_plan_ask` 撤掉"最多 N 条"之后，user 里已经没有任何
        # 条数（只有判据 `NO_BRANCH_CAP_RULE` 与单条字数），原来那句"用户消息里那个条数"
        # 于是变成一条指不到东西的指令。现在指 system 里紧挨着的这个 `{sections}`。
        f"上面那个 {sections} 说的是**二级分支**的总数，不是分组数。"
        "分组的依据是这些方面的共同点，组名要能一句话说清这一组在讲什么"
        "（「基础与定义」「方法与模型」「工程实践」这种），"
        "不要用「其他」「综合」「杂项」这类什么都没说的名字；"
        "只有一支能归进去时不值得单独成组，把它并进最接近的那一组。"
        "color 与 icon 只给**分组**：二级分支不要写这两个字段（它们的颜色随分组继承）。"
        f"二级分支仍然是一句话短语，不超过 {GENERATED_LABEL_MAX_CHARS} 字，不带句号，"
        "组内按逻辑顺序排列、组与组之间也按逻辑顺序排列。"
        "材料自带章节时，章节就是这里的二级分支，不要改成别的话题。"
        "✅ 正确："
        '{"groups":[{"label":"基础与定义","color":"blue","icon":"🧩",'
        '"children":[{"label":"概念界定"},{"label":"发展脉络"}]}]}'
        "❌ 错误："
        '{"branches":[{"label":"概念界定","color":"blue"},{"label":"发展脉络"}]}'
        "（还是写成了一层；这一次要的是 groups 套 children）"
    )


def _skeleton_block(plan: _SkeletonPlan) -> str:
    """结构那一次 user 消息里的骨架清单：扁平时编号一行一条，两级时缩进。

    两级用**缩进**而不是"分组：甲、乙、丙"这种行内写法：缩进在文本上就是层级，
    模型照抄形状的准确率比它读一句话再自己摆层级高得多（`_detail_ask` 用
    `位置：A / B` 的路径写说明，也是同一个理由）。
    """
    if not plan.grouped:
        return "\n".join(
            f"{index}. {branch.label}"
            for index, branch in enumerate(plan.flat, start=1))
    lines: list[str] = []
    for group_index, group in enumerate(plan.groups, start=1):
        lines.append(f"{group_index}. {group.label}")
        for branch_index, branch in enumerate(group.branches, start=1):
            lines.append(f"   {group_index}.{branch_index} {branch.label}")
    return "\n".join(lines)

# 自由类别版（`MINDMAP_FISHBONE_FREE_CATEGORIES`，定义见 `FISHBONE_FREE_PLAN_PROMPT`
# 上面那段注释）。与固定版的差别只有"六个**类别**"→"几个**原因维度**"——它说的仍是
# 同一件事（一级分支已定，照抄不增删），换那个词是为了让第二段调用也守住"这是原因
# 维度"这句话，而不是在那一次悄悄滑回"类别"。
_FISHBONE_FREE_ROOT_RULE = (
    "\n这次的一级分支已经定好了，就是 user 消息里列出的那几个**原因维度**："
    "根节点下必须恰好是这几个，顺序、名称都不改，不增不减。"
)
# 骨下面写什么与类别固不固定无关：`_FISHBONE_BODY` 一个字都不用改（它说的是"属于
# 这一类的原因"，对自定的维度同样成立），所以这里直接复用——两份同样的文字迟早漂。
FISHBONE_FREE_TREE_RULES = _FISHBONE_FREE_ROOT_RULE + _FISHBONE_BODY


def fishbone_tree_rules() -> str:
    """这一次鱼骨第二段用哪份结构规则。关着时返回 `FISHBONE_TREE_RULES` 本身。"""
    return FISHBONE_FREE_TREE_RULES if fishbone_free_categories_enabled() else FISHBONE_TREE_RULES


# **引用字段的形状**。抽出来是因为它有**三个**消费者：按支分组（`BRANCHED_EVIDENCE_RULES`）、
# 图谱分组（`GRAPH_EVIDENCE_RULES`）、以及全塞那条路自己写了一份（`FULL_TEXT_EVIDENCE_RULES`
# 的字段说明与这里同源，只是不要 start/end）。三处各写一份的话，模型在两条路上写出的
# `source_refs` 会长得不一样，而下游的 `verify_source_refs` 只认一种。
#
# 为什么强调"与 label、detail 平级"：模型很爱把引用写成 detail 末尾的一行（"（出处：…）"），
# 那样引用既不能被校验，也不能被点开。
EVIDENCE_REF_FIELDS = (
    '引用写在节点自己的 source_refs 数组里，例如 {"source_id":"…","start":0,'
    '"end":12,"quote":"…"}；source_refs 与 label、detail 平级，不要写进 detail。'
    "优先用 evidence_* 那一组：start、end、quote 分别取 evidence_start、"
    "evidence_end、evidence_quote 的值；没有 evidence_* 时只抄 quote 里连续的"
    "一段（不超过 300 字），这时不要写 start 和 end。"
    "引文逐字照抄，不要改写、不要概括、不要自己算偏移，也不要写 page。"
    + MUST_CITE_EVERY_NODE + "\n"
)

# 有证据时拼在 `DETAIL_PROMPT` 之后的那一段。与 `BRANCH_EVIDENCE_RULES` 是同一份
# 引用契约的两种投递方式：那边每支一个数组（只给一支看），这边按分支分组一次给
# 全部。字段形状必须说到与那边一致，否则同一个模型在两条路上的引用写法会分叉。
#
# 为什么要求"只能引用本组"：证据全在这一次响应里，模型从第 3 支抄一句挂到第 5 支
# 的节点上不会被任何代码拦住（`verify_source_refs` 只看"这句在不在原文里"，
# 不看"它属于哪一支"）。分组是提示词层唯一的约束，所以必须明写。
BRANCHED_EVIDENCE_RULES = (
    "\n本次已经**按一级分支各做过一次检索**，结果按分支分组附在 user 消息末尾"
    "（每组一个 evidence 数组，与 rag_query 的返回同形，组标题写明它属于哪一支）。"
    "写某一支的内容时，出处只能从**那一组**里抄；不要跨组引用，"
    "也不要把一组里的句子挂到另一支的节点上。"
    + EVIDENCE_REF_FIELDS
)

# 分组渲染的表头。为什么带序号与标题：`evidence` 是裸 JSON 数组，同一次响应里有
# 八组、每组形状一样，不带归属的话模型无法知道哪一条命中对应哪一支（这正是分片
# 那条路不需要、而整树这条路必须解决的问题）。
EVIDENCE_SECTION_HEADER = "第 {index} 支「{title}」的检索结果（evidence 数组）："

# ---------------------------------------------------------------- 库证据这一节
#
# `MINDMAP_LIBRARY_IN_GENERATE` 打开时，`_intake` 会从知识库召回到几段原文，由
# `_library_evidence_section` 渲染成 `sections` 里的**最后一节**。
#
# 为什么是一节独立证据，而不是并进 `sources`：见 `_Intake` 的 docstring（按支检索
# 会把它当语料，20 支各切一遍 = 噪声重新进来）。这一节的形状因此更接近 `full_text`
# 那套而不是"第 N 支的 evidence 数组"——它不属于任何一支，是**主题级**的补充。
#
# 两条措辞必须明写，各有来路：
#   - **"不是用户这次传的材料"**：模型会把 user 里的每一段都当成"用户的材料"，
#     然后在 content/诊断里说"依据来自用户提供的材料"。库里的东西与用户材料在
#     界面上是两种来源（一个随请求来、一个是持久资产），说错就是错的账；
#   - **"库里没有的方面不要硬写"**：这一节只有几段，而主题可能覆盖十支。不写这句
#     时模型会把一节材料摊到全部二十个节点上（实测 `MUST_CITE_EVERY_NODE` 下它
#     宁可重复引同一句）。宁可让树小一点，也不要一棵靠同一段话反复支撑的树。
LIBRARY_EVIDENCE_HEADER = (
    "知识库检索到的相关原文（**不是本次上传的材料**，是库里以前存下的来源；"
    "共 {count} 段 / {chars} 字）："
)

LIBRARY_EVIDENCE_RULES = (
    "\nuser 消息末尾还有一节「知识库检索到的相关原文」——它不是本次上传的材料，"
    "是知识库（以前入库的来源）里与主题最相关的几段。那几段与其它证据同等有效，"
    "**引用可以照抄**：source_id 与区间照那一节每段末尾给的值，quote 逐字抄原文；"
    "这一节不属于任何一支，任何分支都可以引用它。"
    "但它只是**几段**，不是整份材料：库里没有覆盖到的方面，就不要为了凑节点硬写。"
    + EVIDENCE_REF_FIELDS
)

# 库那一节**末尾**那句（拼在 user 里，紧挨着输出位置）。
#
# 为什么同一个要求要写两遍（system 一次、user 末尾一次）：这是全塞那条路上的
# 实测教训（`_generate` 里 `FULL_TEXT_EVIDENCE_RULES` 之后那段注释）——system 里
# 那句距离输出位置隔着一整节正文时，模型**一条引用都不写**，而同一次调用里它明明
# 把正文的内容用上了。这里更险：库节在 `sources` 为空时是 user 里唯一的证据段，
# system 又刚说过"这不是你上传的材料"，模型很容易把它当背景知识。
LIBRARY_SECTION_FOOTER = (
    "\n\n上面每一段都可以引用：`source_id` 照抄该段最后的 `source_id:` 值，"
    "start / end 取 `evidence_start` / `evidence_end`，quote 逐字抄正文里连续的一段。"
    '引用写在节点自己的 source_refs 数组里，例如 {"source_id":"…","start":0,'
    '"end":12,"quote":"…"}；source_refs 与 label、detail 平级，不要写进 detail。'
    "**每一个节点都要有一条**：上面这几段没有覆盖到的方面，不要写成节点。"
)


def _library_evidence_section(
    supplement: _LibrarySupplement, *, cite: bool = True,
) -> str:
    """把过门的父块渲染成 `sections` 里的那一节。

    每段一个标题行（份名 / 序号 / 字符区间 / 字数），正文是父块原文，末尾单独一行
    写 `source_id` 与两个偏移——引用要照抄的就是这三个值。

    为什么把 `source_id` 单独放在最后一行而不是塞进标题：标题是给人读的（文件名
    可能很长，混进 uuid 会把它挤掉），而模型抄引用时找的是"这一段属于哪个来源"
    这个明确的位置。两者分开，模型抄错的概率更低，且 `evidence_*` 那一组值就在
    同一处，不用跨行拼。

    `cite=False` 是给骨架那一次用的（它只定一级分支、不写引用）：那一轮拼
    `LIBRARY_SECTION_FOOTER` 等于让模型去写它这一轮被明确禁止写的字段。
    """
    lines = [LIBRARY_EVIDENCE_HEADER.format(
        count=len(supplement.hits), chars=supplement.chars)]
    by_id = {source.id: source for source in supplement.sources}
    for index, hit in enumerate(supplement.hits, start=1):
        source = by_id.get(hit.source_id)
        name = (source.filename if source is not None and source.filename
                else hit.source_id[:8])
        lines.append(
            f"\n【库 {index}｜{name}｜第 {hit.start}–{hit.end} 字】\n"
            f"{hit.quote.strip()}\n"
            f'source_id: "{hit.source_id}"  evidence_start: {hit.start}  '
            f"evidence_end: {hit.end}"
        )
    text = "\n".join(lines)
    return text + LIBRARY_SECTION_FOOTER if cite else text



# ---------------------------------------------------------------- 全塞：正文进 prompt
#
# 与上面那套是**同一个契约的两种投递方式**：字段形状一致（`source_refs` 的写法一个字
# 都没变），差别只有"给的是检索片段还是完整正文"。分两套是刻意的——同一句话不能同时
# 说"只能从本组抄"和"全文都在这里"，模型会按前一句自我限制。
#
# 三处与按支那套**必须**不同的措辞，各有实测依据：
#   - "这是完整正文，不是片段"：不说清时模型把它当检索结果用，只挑开头几段；
#   - "每一份材料都应当被用上"：全塞买到的正是**覆盖**（按支检索的覆盖率实测 28.8%，
#     见 `_search_branch`），不点名要求覆盖，模型会集中写它最先读到的那一份；
#   - **不要求 `evidence_*`**：全塞没有命中区间，写了 `start/end` 反而过不了逐字校验
#     （偏移是它自己数的）。只抄 quote，服务端自己在全文里唯一定位并补偏移
#     （`source_refs._locate_quote`）。
FULL_TEXT_EVIDENCE_RULES = (
    "\n材料已经**整份**给你了：user 消息末尾是全部材料的完整正文，按材料分节"
    "（每节标题写明文件名、字数与 source_id）。它比检索片段更全——检索只返回命中的"
    "几段，这里是全文，所有内容都在里面，你可以直接精读并引用任意一处。\n"
    '引用写在节点自己的 source_refs 数组里，例如 {"source_id":"…","quote":"…"}；'
    "source_refs 与 label、detail 平级，不要写进 detail。"
    "quote 必须**逐字**照抄原文里连续的一段（不超过 300 字），"
    "不要改写、不要概括；start / end 可以不写（服务端会在全文里定位），"
    "要写就必须与 quote 对得上，page 不用写。\n"
    "**凡是内容出自材料，就必须给这个节点挂一条 source_refs**；"
    "每一份材料都要用上——一段材料的核心内容应当落在某个节点的 detail 与引用里，"
    "不要只写最先读到的那一份。"
    + MUST_CITE_EVERY_NODE + "\n"
)

# 全塞时的分节表头。为什么要带**字数**：模型据此判断这一份该给多少节点，
# 而不是把一份 200 字的材料与一份 4 万字的材料按同一权重摊（全塞的优势就是它可以
# 按实际体量分配注意力）。
FULL_TEXT_SECTION_HEADER = "第 {index} 份材料「{title}」（全文 {chars} 字，source_id={source_id}）："

# 第三段（只写说明）在全塞时的引导句。与 `DETAIL_ONLY_EVIDENCE_RULES` 的区别：
# 那个说"以本组为准"（分组是检索给的），这里没有分组，只有全文。
DETAIL_ONLY_FULL_TEXT_RULES = (
    "\nuser 消息末尾附的是材料的**完整正文**（与结构那一轮同一份，按材料分节）。"
    "写某个节点的说明时，先在这一份正文里找到它对应的那段再写，不要凭印象；"
    "原文与你的知识冲突时按原文写。不要新增或改动引用。\n"
)


# 第三段的第三种证据形态（`MINDMAP_DETAIL_RETRIEVAL=1`）：每个节点只拿到**它自己
# 那一章里**命中的那几块原文。三条边界都必须写出来，缺一条就是一类静默走样：
#
#   - "块里标着第几章"：模型据此知道这一块属于谁，也才不会把两章的块缝成一句；
#   - "没列到的节点按你的知识写"：不给这一句，模型会把"这一章里没命中"读成"材料里
#     没有"，然后写一句"材料中未提及"——`DETAIL_NO_SOURCE_RULES` 挡的是同一个失效，
#     但那条说"本次没有材料"，与"有材料、只是这个节点没命中"是两件事；
#   - "不要新增或改动引用"：引用是第二段的产物（`_slice_refs` 在章内定位），这一轮
#     连 `source_refs` 都不该出现。少这一句时模型会顺手写引用，而 `_detail_map` 只
#     认 `detail`——那些引用会被静默丢掉，用户端看不出任何异常。
DETAIL_ONLY_RETRIEVED_RULES = (
    "\nuser 消息末尾分两部分：**原文块**（每条都标着它属于第几章，按文档顺序编号），"
    "以及**各节点命中的块**（每个 id 对应它自己那一章里命中的那几块）。"
    "写某个节点的说明时，只以它命中的那几块为依据，不要用别的 id 命中的块，"
    "也不要把两章的块拼成一句；没列到的节点说明它那一章里没有直接对应的段落，"
    "那种情况按你的知识写，不要因此说「材料中没有」。"
    "原文与你的知识冲突时按原文写。不要新增或改动引用。\n"
)


# ---------------------------------------------------------------- 第三段：只写 detail
#
# 为什么要有这一段：把"骨架 + 每支的结构 + 每段说明"塞进一次输出时，模型的注意力
# 被几十段说明摊薄——五采样 detail 平均 45.3/28.0/28.5/26.2/27.4 字，越往后越短，
# 而在 system 里加"不少于 60 字"反而更短（28.5 → 26.2）。瓶颈不是提示词，是同一次
# 输出里的并发任务太多。第三段把"想结构"整个拿走：user 里给的就是一张定好的
# id / label / 层级路径清单，它只需要逐个写说明。
#
# 原型实测（60 个短 detail 节点，一次调用全部返回）：平均 114.4 字、最短 96、
# 0 条不足 40。
#
# 两个数字各管一件事，**不能合并**：
#   - `DETAIL_MIN_CHARS` / `DETAIL_MAX_CHARS` 是**这一轮要写多长**（写进提示词的
#     目标区间）；
#   - `DETAIL_FILL_FLOOR` 是**哪些节点需要补**（代码侧判据）。取 40 是因为它就是
#     验收红线：结构那一轮若自己写出了 60 字的说明，不该再花一次输出覆盖它。
DETAIL_MIN_CHARS = 80
DETAIL_MAX_CHARS = 150
DETAIL_FILL_FLOOR = 40

# 第三段从"一次写全部"改成"两次并发各写一半"的**下限**：少于这个数就不拆。
#
# 拆半买到的是"墙钟跟着输出量走"：56 个节点 × 105 字 ≈ 5900 字，输出侧要 30 秒
# 上下，而两半并发之后最慢的那一半只有 2950 字的输出（实测 44.5s → 26s，见
# `docs/ONE-CALL.md`）。代价是两次首 token 延迟（实测 1–2 秒）与两份 system 提示词
# ——固定成本，与节点数无关。小树上这笔固定成本比省下来的还多：10 个节点的一半是
# 5 段说明，模型预热的时间比写 5 段还长。
#
# 取 12 的依据不是"配平"而是实测的树规模分布：`INITIAL_MAX_NODES` 是上限，真实
# 产出在 20–60 个节点之间浮动，12 让常见的树都拆；而"三条分支、每支两个要点"这种
# 演示级小树（10 个节点）不拆。这个数也是回归面：`tests/test_detail_round.py` 里
# 那棵树正好 10 个节点，走的是**不拆**那条路，所以那一整批用例的断言与拆分之前
# 逐字相同。
DETAIL_SPLIT_MIN = 12

# 第三段的 system。**不复用 `DETAIL_PROMPT`**：那一份是"整棵树"的契约（树规则、
# 深度、分支数），拿它来写说明会让模型又开始想着要不要再多拆一层。这里只留三件事：
# 输出形状（id → 说明）、长度、以及"不许动结构、不许动引用"。
#
# **长度要求放在最后一段**：前两轮采样里它写在中间，实测仍有 1–3 个节点只写一句
# （21–46 字）。这是提示词位置问题，不是措辞问题——同一批采样里平均长度 104–138，
# 说明它读懂了要求，只是对"简单的节点"自行通融。所以把最要紧的两条挪到末尾（模型
# 对 system 末尾与 user 末尾更敏感），并在 user 末尾再加一句自查。
#
# **只有两个数字是可变的**（`min_chars` / `max_chars`，由 `budget.detail_span` 给）：
# 句子数不跟着区间伸缩，它由 `MINDMAP_DETAIL_SENTENCES` 单独开关（默认关 = 仍要求
# "至少 3 句"）。"至少 3 句"是拿 21–46 字的一句半量出来的质量线，但四臂读数
# （`docs/DETAIL-SENTENCES.md`）显示它更像"每段末尾固定挂一句最短的收尾"——薄批 66%
# 的段里末句就是全段最短的那一句，而放开之后均长只掉 4.7%、ρ 与 κ 不变。所以它不再
# 是一条不需要理由的常量：要放开就翻开关，翻之前先看那一节的读数。材料撑不住 3 句时
# 该减的仍是节点数（`MINDMAP_NODE_SCALE`），见 `generation.budget.DETAIL_SENTENCE_FLOOR`。
#: 两份「只写说明」prompt 共用的前半段：输出形状、禁占位话、不动引用。
#
# 抽成常量是为了让"句子数"成为**唯一**可变的尾段：`_detail_only_prompt` 与
# `_detail_only_prompt_open` 的差别只在最后一段，读 diff 的人不必逐行比对四段没变的
# 规则。抽取本身不改字节——拼接结果与抽取之前逐字相同（`test_detail_sentences` 用
# sha256 钉着 `DETAIL_ONLY_PROMPT`，那条断言在抽取前后都必须过）。
_DETAIL_ONLY_SHAPE_RULES = (
    "这一次只写思维导图节点的「说明」，不写结构。"
    "节点已经定好了：id、label、它在一级分支下的位置都在 user 消息里，"
    "一个都不许增删改名，也不许改动父子关系。\n"
    "只返回一个 JSON 对象，不要代码围栏：键是节点 id（原样照抄），值是说明正文，"
    '例如 {"d1a":"…","d1b":"…"}。'
    "每一个 id 都必须出现，一个都不能漏。\n"
    "不要写「简要说明」「详见教材」「同上」「可参考」这类占位话；"
    "也不要把 label 换个说法重复一遍——画布上已经有 label 了，"
    "detail 是点开之后才能看到的正文。\n"
    "**这一轮不要写 source_refs**：引用已经在结构那一轮按证据定好了，"
    "这里再写会与那些引用对不上。\n"
)


def _detail_only_prompt(min_chars: int, max_chars: int) -> str:
    """第三段的 system，长度区间参数化。`(80, 150)` 下与加伸缩之前逐字节相同。"""
    sentences = budget.DETAIL_MIN_SENTENCES
    return (
        _DETAIL_ONLY_SHAPE_RULES
        # 下面是**最后**一段，两条都是拿真实采样改出来的：
        #   - 偏短的都是**并列型 label**（「常见变体」「循环与序列网络」「K-means 与层次
        #     聚类」）：模型只把名字列一遍就收（21–46 字，低于 60 的下限）。所以点名要求
        #     逐个说清，并给出"至少 3 句"这个比字数更难自我通融的下限；
        #   - 不压上限会写出 165/170 字（超过 150 的目标），多出来的字既读不完，也拖慢
        #     这一次调用（说明那一次是总耗时的大头）。
        + f"\n每一段说明写 {min_chars}–{max_chars} 字、**至少 {sentences} 句完整的话**"
        f"（不是 {sentences} 个短语），少于 {sentences} 句就是没写完。"
        "如果这个节点的标题是一串并列的东西（几个算法、几种变体、几类方法、几个阶段），"
        "要逐个说清它们各自做什么、差别在哪，不要只把名字列一遍就收。"
        f"超过 {max_chars} 字就挑最关键的两三点，长文留给节点下面的子节点。\n"
    )


#: 现状区间的那一份（`MINDMAP_DETAIL_SCALE` 关着、或没有材料时用它，逐字节不变）。
DETAIL_ONLY_PROMPT = _detail_only_prompt(DETAIL_MIN_CHARS, DETAIL_MAX_CHARS)


# ---------------------------------------------------------------- 句子数：放开
#
# "至少 3 句完整的话"是**第二道**防"越写越短"的加固（第一道就是 80 字下限）。它的
# 来历写在上面第三段那段注释里：把「结构 + 说明」压在同一次输出时，五采样 detail 是
# 45.3 / 28.0 / 28.5 / 26.2 / 27.4 字，越写越短；而在 system 里加"不少于 60 字"反而
# 更短（28.5 → 26.2）。所以真正救它的是"单独一段只写说明"，句子数只是加固。
#
# 加固的另一面是"凑句子"：句数下限会把"这一段没那么多可说"的节点逼成三句**结构上
# 合格、信息上重复**的话，而 80 字下限已经独立地挡住了"塌到 40 字以下"。这条是不是
# 在制造噪声，用三批读数回答（`docs/DETAIL-SENTENCES.md`：三臂 × 两批 × 每批 2 次）。
#
# 开关默认**关**：关着时 `_detail_prompt` 直接返回 `_detail_only_prompt` 的输出，
# system 与加这个开关之前逐字节相同（`tests/test_detail_sentences.py` 用 sha256 钉着）。
DETAIL_SENTENCES_ENV = "MINDMAP_DETAIL_SENTENCES"


# ---------------------------------------------------------------- 字数：放开
#
# 上一条（句子数）只拆了"几句"，`min_chars` 一个字没动 —— 因为它当时是**唯一**还在
# 挡"越写越短"的东西（`docs/DETAIL-SENTENCES.md` 的 C 臂：拆了它，薄批 86/86 段塌成
# 1 句、均长 −45%、最短 26 字）。
#
# 这一条是产品决定要连字数一起放开：**每一段的长短按它在材料里有多少分量决定**，
# 不设上下限。所以它必须与"节点数"配套读 —— 薄材料上 670 字给 44 个节点，每节点只
# 分到 15 字材料，此时"写 80 字"就是硬凑 5 倍。放开字数之后那批节点会各自写短，
# 而**树的形状不变**（这一轮不动节点预算、不动折叠、不动 `initial_nodes`）。
#
# 与 C 臂的差别只有一条：C 臂说"按材料分量决定"但没说并列型 label 怎么办，于是
# 那类节点（「常见变体」「循环与序列网络」）又被列名字糊过去了。这一份把 A 臂那段
# 并列要求留着 —— 那条与字数无关，是"别只报菜名"。
DETAIL_LENGTH_ENV = "MINDMAP_DETAIL_LENGTH"


def detail_length_free() -> bool:
    """`MINDMAP_DETAIL_LENGTH=free` → 说明那一轮不设字数上下限。

    用值而不是布尔，是因为这一族已经有两档语义了（原样 / 去句数），再加一档"去字数"
    之后 "1" 到底指哪一档会变成靠记忆的事实。显式写 `free` 让它与
    `MINDMAP_DETAIL_SENTENCES=1` 不互相解释。
    """
    return os.getenv(DETAIL_LENGTH_ENV, "").strip().lower() == "free"


def _detail_only_prompt_free() -> str:
    """不设字数上下限的那一份。

    没有 `min_chars` / `max_chars` 入参：这一份里那两个数一个都不出现，留着参数只会
    让调用方以为它们还在起作用。
    """
    return (
        _DETAIL_ONLY_SHAPE_RULES
        # 并列型 label 那一条与 A 臂逐字相同：它管的是"别只把名字列一遍"，
        # 与"写多少字"是两件事，放开字数不该把它一起放开。
        + "\n每一段说明的长短按这个节点在材料里有多少分量决定：材料里讲得多就写长，"
        "讲得少就写短，一句话能说清就一句话。"
        "如果这个节点的标题是一串并列的东西（几个算法、几种变体、几类方法、几个阶段），"
        "要逐个说清它们各自做什么、差别在哪，不要只把名字列一遍就收。\n"
    )


def detail_sentences_open() -> bool:
    """`MINDMAP_DETAIL_SENTENCES=1` → 说明那一轮不再要求"至少 N 句"。

    与 `MINDMAP_DETAIL_SCALE` / `MINDMAP_NODE_SCALE` 同一个约定：改的是模型的写法，
    翻它之前得先有一组读数，而且要能单独回滚。
    """
    return os.getenv(DETAIL_SENTENCES_ENV, "").strip().lower() in (
        "1", "on", "true", "yes")


def _detail_only_prompt_open(min_chars: int, max_chars: int) -> str:
    """开着时的那一份：**去句数，留字数下限**。

    为什么一个字都不动 `min_chars`：句子数管的是"怎么分段"，字数管的是"写多少"，
    两件事不是同一条约束的两种写法。放开句子数之后**唯一**还在挡"越写越短"的就是
    这个下限；把它一起拆掉（把长度交给"材料分量"）那一臂的读数在
    `docs/DETAIL-SENTENCES.md`，结论是拆不得。

    为什么补一句"不要把一句话硬拉长到 {min} 字"：去掉句数之后最差的形态不是"写短"，
    而是"一句话硬撑到下限"——一条 80 字的句子既不好读，也说明模型在拿字数下限当
    配额填。这一句只禁"硬撑"，不给句数配额（"几句都行"正是这次要放开的东西）。
    不补"一句话就能说清也够"这类话：它与 `min_chars` 直接矛盾，模型会拿它当逃生口，
    退回 21–46 字那个已经被量过的失效形态。
    """
    return (
        _DETAIL_ONLY_SHAPE_RULES
        # 最后一段的前两条与 A 那一份同源（并列型 label 逐个说清、上限压住），换掉的
        # 只有句子数那半句：句数配额 → 按意思分句 + 不许硬撑。
        + f"\n每一段说明写 {min_chars}–{max_chars} 字，少于 {min_chars} 字就是没写完。"
        "分几句按意思走，几句都行，但不要把一句话硬拉长到"
        f" {min_chars} 字——那样的说明读不下去。"
        "如果这个节点的标题是一串并列的东西（几个算法、几种变体、几类方法、几个阶段），"
        "要逐个说清它们各自做什么、差别在哪，不要只把名字列一遍就收。"
        f"超过 {max_chars} 字就挑最关键的两三点，长文留给节点下面的子节点。\n"
    )


def _detail_prompt(min_chars: int, max_chars: int) -> str:
    """第三段 system 的**唯一入口**：开关关着时逐字节等于 `DETAIL_ONLY_PROMPT`。

    单独一个分发函数而不是在 `_fill_details_half` 里写 if：`_detail_only_prompt` 被
    "区间参数化之后与常量逐字节相同"这条回归钉着（`test_detail_scale`），让它自己读
    环境变量会让那条断言变成"看环境变量行事"。纯函数保持纯，开关只在这一层。
    """
    if detail_length_free():
        # 最高优先：它把字数那两条一起放开，比"只去句数"更彻底。
        return _detail_only_prompt_free()
    if detail_sentences_open():
        return _detail_only_prompt_open(min_chars, max_chars)
    return _detail_only_prompt(min_chars, max_chars)


def is_detail_only_prompt(system: str) -> bool:
    """这一段 system 是不是「只写说明」那一轮。**判据是共用前半段，不是整份常量**。

    为什么需要它：开关一开，system 就不再以 `DETAIL_ONLY_PROMPT` 那 467 个字符开头
    （尾段换了措辞），而"按 system 前缀分类哪一轮"这件事散在测试与测量脚本里
    （`measure_section_slice` / `measure_two_tier` / 十几个 `startswith(...)`）。那些
    断言在**默认关**的前提下仍然成立，但只要有人打开开关跑一遍，第三段就会被误判成
    结构轮——返回一个没有 `detail` 的树，看上去像"模型没听话"。

    两份 prompt 的前半段是同一份 `_DETAIL_ONLY_SHAPE_RULES`（输出形状 / 禁占位话 /
    不动引用），句子数只动尾段。所以这一个判据同时认两种写法。
    """
    return system.startswith(_DETAIL_ONLY_SHAPE_RULES)


def _default_detail_span() -> budget.DetailSpan:
    """没有伸缩时的区间：就是 `DETAIL_MIN_CHARS`–`DETAIL_MAX_CHARS` 与 3 句。

    单独成函数而不是在两处各写一遍 `span or budget.default_detail_span(...)`：
    这两个调用点（system 与自查句）必须给同一个区间，写两遍就会在某次改动里漂开。
    """
    return budget.default_detail_span(
        floor_chars=DETAIL_MIN_CHARS, ceiling_chars=DETAIL_MAX_CHARS)

# 有证据时拼在 `DETAIL_ONLY_PROMPT` 之后。与 `BRANCHED_EVIDENCE_RULES` 的区别是
# 它只要"读"，不要"抄"：引用是上一轮的事，这一轮读原文是为了让**说明**与那些引用
# 讲同一件事（引用说的是 A、说明讲的是 B，是这条路最容易出现的错位）。
DETAIL_ONLY_EVIDENCE_RULES = (
    "\nuser 消息末尾附的是结构那一轮用过的原文（按一级分支分组，与上一轮同一份）。"
    "写某一支的说明时以**那一组**为准，不要写成另一支的内容；"
    "原文与你的知识冲突时按原文写。不要新增或改动引用。\n"
)


def _iter_paths(root: Node, path: tuple[str, ...] = ()):
    """前序遍历，带上"根 → 父"的标题链（不含自己）。"""
    yield root, path
    for child in root.children:
        yield from _iter_paths(child, (*path, root.label))


def _needs_detail(node: Node) -> bool:
    """这个节点的说明要不要第三段补。

    判据是**长度**而不是"空不空"：结构那一轮偶尔会自己写出几句，26–28 字那种
    恰恰是要消灭的形态，按"空才补"会把它整批放过。
    """
    return len((node.detail or "").strip()) < DETAIL_FILL_FLOOR


def _detail_targets(
    root: Node, *, skip: Collection[str] = (),
) -> list[tuple[Node, tuple[str, ...]]]:
    """要补说明的节点（前序，父在子前）与它的层级路径。

    `skip` 是两级骨架里的**分组节点**：它是分类名，没有内容可写。为它写一段 105 字
    的说明只会得到一段解释"这一组在讲什么"的空话，而它占掉的正是那二十节里某一节
    该有的注意力（第三段的输出预算是按节点数摊的）。
    """
    skipped = set(skip)
    return [(node, path) for node, path in _iter_paths(root)
            if node.id not in skipped and _needs_detail(node)]


# ------------------------------ 第三段的证据：每个节点在**它自己那一章**内检索
#
# 这条路把第三段（`_fill_details`）的**证据范围**从"整支/整份"改成"这一章"。
#
# 为什么：原来的证据范围与节点范围不一致。全塞时每个节点都在读 14,120 字的整份材料
# （`FULL_TEXT_MATERIAL_CHARS = 50_000`，本仓库的真实材料都在闸内），按支时读的是
# 12 组 × 约 2,000 字的全局检索片段。而一个节点要说的是**一点具体做什么**——8–16 字
# 的 label 在那两万字里找自己那一段，注意力摊在大半与它无关的章上。
#
# 按章切（`MINDMAP_SECTION_SLICE=1`）之后，**章节边界本身就是最有效的限定**：每个
# 节点能从祖先链上唯一确定"我在第几章"，而那一章是 1,300–2,900 字的连续原文。于是
# 证据可以按节点分层：
#
#   - 一级分支（就是章标题）→ 它那一章的原文（`chapter.prompt_text`，8,000 字上限与
#                              第二段同一条闸）。说明要概括"这一章在讲什么"，证据
#                              只能是整章，切小了反而写不出跨度；
#   - 章内节点              → label 在**本章范围内**检索 2–3 段（每段一个父块，
#                              450–700 字）。说明只讲"这一点具体做什么"。
#
# **不要拿"层级路径"来替代这件事**：那条理由是为"整份 14,120 字进同一个 prompt"
# 写的——在那个尺度上 16 字的 label 会飘；在 1,300 字的一章里它不会。层级路径本身
# **照旧保留**（`_detail_ask` 清单里那一列），全塞那条路仍然需要它。
#
# 范围是怎么限定的：**不用过滤候选块，而是把来源本身切成这一章**。`_chapter_source`
# 造一个 `text = 原文[章首:章末]` 的来源副本，检索只能在章的正文里切块——于是"命中
# 落在本章内"是构造保证，不是事后过滤（事后过滤要先取一个更深的池子，被剔掉的候选
# 仍然占掉了 top_k 的名额）。偏移再整体平移 `chapter.start` 回到全文坐标，`quote`
# 逐字等于 `原文[start:end]`，与 `SourceRef` 同一口径。
DETAIL_RETRIEVAL_ENV = "MINDMAP_DETAIL_RETRIEVAL"

# 每个节点取几段：**至少 3，至多 5**（`DETAIL_RETRIEVAL_KEEP`）。一章切出来只有 3–5
# 个父块（144–2,894 字 / 450–700 字一块），所以这里的旋钮更像"要不要把这一章看全"：
# 固定 3 段在 5 个父块的章里会漏掉两块，而那两块常常只是第 3 块差 0.02 分。
# `max_k=5` 与"一章 3–5 个父块"对齐，`DETAIL_RETRIEVAL_MAX_CHARS=16,000` 仍是总闸。
DETAIL_RETRIEVAL_KEEP = KeepPolicy(min_k=3, max_k=5, rel=0.85)
DETAIL_RETRIEVAL_TOP_K = DETAIL_RETRIEVAL_KEEP.min_k

# 检索并发。检索本身是 `asyncio.to_thread` 里的同步函数（BM25 + 向量缓存），61 个
# 节点串行发完是十几秒。取 6 而不是更大：真正的等待只在每章第一个节点（块向量要
# 现算，之后同章的块全走 `vector_cache`），再高只是把峰值并发堆在那一小段上。
DETAIL_RETRIEVAL_CONCURRENCY = 6

# 半次调用里原文块的总字数上限。取值依据（`docs/RAG-DECISIONS.md` 实测）：12 章
# 61 个节点、每节点 3 段，**跨节点去重之后**的并集就是这 12 章各自的父块并集
# ≈ 全文 14,120 字，两半各拿一半 ≈ 7,000 字。16,000 字给"一章一万多字"那种形状
# （`docs/PLAN.md` 最长 11,697 字）留一倍余量，按 `estimate_input_tokens` 折算约
# 10.7k token——仍小于现状那"整份材料 × 两半"的 28,240 字。
DETAIL_RETRIEVAL_MAX_CHARS = 16_000


def detail_retrieval_enabled() -> bool:
    """第三段按节点章内检索的开关。默认**关**：它是 A/B 的一臂，也是另一笔 token。

    与 `section_slice_enabled` 同一种写法与同一条理由：一个改变 prompt 范围与
    检索次数的默认值不该悄悄生效——它要有一组数字撑着才配翻默认值。
    """
    return os.getenv(DETAIL_RETRIEVAL_ENV, "").strip().lower() in GRAPH_SKELETON_TRUTHY


@dataclass
class _DetailBlock:
    """一段可引用的原文块：**章内的一个父块**，坐标已平移回全文口径。"""

    chapter_index: int
    chapter_title: str
    source_id: str
    start: int
    end: int
    quote: str
    score: float


@dataclass
class _DetailEvidence:
    """第三段这一次检索的全部产物：块池 + 节点 → 块的指针 + 一笔账。

    为什么是**块池 + 指针**而不是"每个节点各渲染一遍"：同一章的 5 个节点命中的是
    同一批父块，逐个渲染会把这一章复制 5 遍——那正是要消灭的"注意力摊薄"，只是换成
    了浪费 token 的版本。块按 `(source_id, start, end)` 去重，节点只记编号；两半各渲染
    自己节点用到的那部分（`_detail_evidence_section`），所以"这一半的 prompt 里只有
    这一半的章"。
    """

    blocks: list[_DetailBlock] = field(default_factory=list)
    by_node: dict[str, list[int]] = field(default_factory=dict)
    queries: int = 0        # 真发出去的检索次数（= 有 label 又有章的节点数）
    hits: int = 0           # 检索返回的条数（去重前）
    failed: int = 0         # 抛错的检索次数（那一个节点退回"没有证据"）
    chapters: int = 0       # 覆盖到几章
    seconds: float = 0.0

    def empty(self) -> bool:
        return not self.blocks

    def report(self) -> dict[str, Any]:
        """进 `data["detail"]["evidence"]` 的那笔账（只在开关打开时存在）。"""
        return {
            "queries": self.queries,
            "hits": self.hits,
            "failed": self.failed,
            "blocks": len(self.blocks),
            "chapters": self.chapters,
            "chars": sum(len(block.quote) for block in self.blocks),
            "nodes_with_evidence": len(self.by_node),
            "top_k": DETAIL_RETRIEVAL_TOP_K,
            "seconds": self.seconds,
        }


def _node_chapters(root: Node, chapters: Sequence[_Chapter]) -> dict[str, _Chapter]:
    """节点 id → 它所在的那一章。根节点**不在**里面（它不属于任何一章）。

    判据按可靠性排序，三条都要（与 `_align_chapters` / `_apply_skeleton` 的"label
    优先、序号兜底"同源，只是这里的优先项是 **id**）：

      1. `s{章号}`：`_slice_structure` 组装时给一级分支的唯一 id，最确定；
      2. 章标题：模型若把分支名改了一个字，id 仍是它，但一道兜底不多花钱；
      3. 位置：只有前两条都对不上时才用（那份树不是这条路产出的），宁可给错一个
         章也不要不给——错章的代价是证据偏，不给的代价是这一段完全没有依据。

    返回的是**整棵子树**的映射：章内节点不会有别的归属来源，逐层判一次只是多花
    几十次字典写入。
    """
    if not chapters:
        return {}
    by_id = {f"s{chapter.index}": chapter for chapter in chapters}
    by_key = {_skeleton_key(chapter.title): chapter for chapter in chapters}
    mapping: dict[str, _Chapter] = {}
    for position, child in enumerate(root.children):
        chapter = by_id.get(child.id) or by_key.get(_skeleton_key(child.label))
        if chapter is None and position < len(chapters):
            chapter = chapters[position]
        if chapter is None:
            continue
        stack = [child]
        while stack:
            node = stack.pop()
            mapping[node.id] = chapter
            stack.extend(node.children)
    return mapping


def _detail_queries(
    targets: Sequence[tuple[Node, tuple[str, ...]]],
    mapping: Mapping[str, _Chapter],
) -> dict[str, str]:
    """节点 id → 检索问句。**label 为主**，同章内重名时才补父 label。

    为什么不一律拼父 label：父是这一章的方面名，拼上去等于把每一句都往"这一章"
    的方向再拉一遍，而章内检索的范围**本来就只有这一章**——多出来的是噪声。
    但同一个 label 在一章里出现两次时（"实施要点"在两个方面下各一个）两者会拿到
    完全相同的证据，那一章的 5 个节点就有一对写出同一段话。这时用「父 + 自己」，
    差别的正是它们真正的区别。

    父 label 取 `path[-1]`（`_iter_paths` 给的"根 → 父"链的最后一项）：一级分支的
    path 是空的，它自己不重名（章标题就是它）。
    """
    seen: dict[tuple[int, str], int] = {}
    for node, _path in targets:
        chapter = mapping.get(node.id)
        label = (node.label or "").strip()
        if chapter is None or not label:
            continue
        key = (chapter.index, _skeleton_key(label))
        seen[key] = seen.get(key, 0) + 1
    questions: dict[str, str] = {}
    for node, path in targets:
        chapter = mapping.get(node.id)
        label = (node.label or "").strip()
        if chapter is None or not label:
            continue
        parent = (path[-1].strip() if path else "")
        repeated = seen[(chapter.index, _skeleton_key(label))] > 1
        # 父与子同名时不拼（"一句话结论 一句话结论"只是把同一个词发两遍，白白拉长了
        # 词面匹配的 query）：那种形状下它们本来就分不开，拼了也不加分。
        if repeated and parent and _skeleton_key(parent) != _skeleton_key(label):
            questions[node.id] = f"{parent} {label}"
        else:
            questions[node.id] = label
    return questions


def _chapter_source(source: Source, chapter: _Chapter, text: str) -> Source:
    """把一份来源**裁成这一章**：检索的候选块只可能从这段正文里切出来。

    这就是"范围过滤"的实现：不做候选块的事后过滤（那要先取更深的池子，被剔掉的
    候选仍占着 `top_k` 的名额），而是让切块这件事只看得见这一章。命中坐标因此是
    **章内相对**的，由调用方 `+chapter.start` 平移回全文口径。

    只改 `text`：`id` 必须留着（引用、证据集、界面高亮共用这个键），`kind` /
    `filename` / `metadata` 与检索无关但也不该凭空改——这份副本不落库、不出函数。
    """
    return source.model_copy(update={"text": text[chapter.start:chapter.end]})


async def _detail_evidence(
    targets: Sequence[tuple[Node, tuple[str, ...]]], *,
    mapping: Mapping[str, _Chapter], source: Source,
) -> _DetailEvidence:
    """每个节点在**它自己那一章**里检索 `DETAIL_RETRIEVAL_TOP_K` 段 → 块池 + 指针。

    两段并发（与 `_retrieve_branches` 的"第一支先串一条"同一个理由，只是按章分）：
    块向量按文本哈希缓存，同一章的 5 个节点若同时发出，每个都会认为自己是第一次
    （5 × 15 条块文本现算）；先每章发一个，缓存填上之后其余节点只需要 embed 自己
    那一条 query。**每章**而不是全局一条：章与章之间块文本不重叠，预热第一章对
    第二章是白费。

    失败**不抛**（与 `_fill_details` 同一条原则）：某一次检索抛错只让那个节点退回
    "没有证据"，不能让整段说明写不出来。整棵树一次都没命中时由调用方退回现状
    （`_fill_details` 那一边判 `empty()` 并保留原来的 `sections`）。
    """
    evidence = _DetailEvidence()
    started = time.perf_counter()
    questions = _detail_queries(targets, mapping)
    if not questions:
        return evidence

    evidence.queries = len(questions)
    evidence.chapters = len({mapping[node_id].index for node_id in questions})
    chapters = {chapter.index: chapter for chapter in mapping.values()}
    limit = asyncio.Semaphore(DETAIL_RETRIEVAL_CONCURRENCY)

    async def run(node_id: str) -> list[tuple[str, EvidenceHit]]:
        chapter = chapters[mapping[node_id].index]
        async with limit:
            try:
                hits = await asyncio.to_thread(
                    search_exact_blocks,
                    [_chapter_source(source, chapter, source.text or "")],
                    questions[node_id], DETAIL_RETRIEVAL_TOP_K,
                    keep=detail_keep())
            except Exception as exc:  # noqa: BLE001 — 检索失败只降级，理由见 docstring
                logger.warning("第三段按节点检索失败（%s）：%s: %s",
                               questions[node_id][:40], type(exc).__name__, str(exc)[:160])
                return []
        return [(node_id, hit) for hit in hits]

    # 第一段：每章一个节点，把这一章的块向量填进缓存。章按序发，互不等待。
    first: dict[int, str] = {}
    for node_id in questions:
        first.setdefault(mapping[node_id].index, node_id)
    results = list(await asyncio.gather(*(run(node_id) for node_id in first.values())))
    # 第二段：其余节点。同章第一轮已经结束，这里不需要再分章。
    rest = [node_id for node_id in questions if node_id not in set(first.values())]
    results.extend(await asyncio.gather(*(run(node_id) for node_id in rest)))

    index_of: dict[tuple[str, int, int], int] = {}
    per_node: dict[str, list[EvidenceHit]] = {}
    text = source.text or ""
    for group in results:
        for node_id, hit in group:
            evidence.hits += 1
            per_node.setdefault(node_id, []).append(hit)
            start, end = hit.start + mapping[node_id].start, hit.end + mapping[node_id].start
            key = (hit.source_id, start, end)
            if key in index_of:
                continue
            if start < 0 or end > len(text) or end <= start:
                # 章内相对坐标平移之后越界：只可能是上游换了切块口径。丢掉这条而
                # 不是把它塞进块池——`quote` 与坐标对不上的证据比没有证据更糟。
                continue
            chapter = chapters[mapping[node_id].index]
            index_of[key] = len(evidence.blocks)
            evidence.blocks.append(_DetailBlock(
                chapter_index=chapter.index,
                chapter_title=chapter.title,
                source_id=hit.source_id,
                start=start,
                end=end,
                quote=text[start:end],
                score=hit.score,
            ))
    for node_id, hits in per_node.items():
        picked = [index_of[(hit.source_id,
                            hit.start + mapping[node_id].start,
                            hit.end + mapping[node_id].start)]
                  for hit in hits
                  if (hit.source_id, hit.start + mapping[node_id].start,
                      hit.end + mapping[node_id].start) in index_of]
        if picked:
            evidence.by_node[node_id] = picked
    _budget_blocks(evidence)
    evidence.seconds = round(time.perf_counter() - started, 3)
    return evidence


def _budget_blocks(evidence: _DetailEvidence) -> None:
    """按分数从高到低留块，总字数封顶 `DETAIL_RETRIEVAL_MAX_CHARS`（就地改）。

    真实材料上这个闸**不触发**（12 章的块并集约 7,000 字 / 半），它守的是"一章
    一万多字 × 十几章"那种形状：不定闸就等于把整份材料又塞回 prompt，而这条路
    的全部意义是把范围收窄。

    为什么按分数而从文档序留：块池是按节点命中拼出来的，尾部节点的块在文档序上
    靠后——按文档序截断等于系统性地饿死最后几章（`_clip_tree` 那个失效形态的翻版）。
    留下的块**重新按文档序编号**，因为 `_detail_evidence_section` 要让模型按阅读
    顺序看到它们。
    """
    if not evidence.blocks:
        return
    total = sum(len(block.quote) for block in evidence.blocks)
    if total > DETAIL_RETRIEVAL_MAX_CHARS:
        order = sorted(range(len(evidence.blocks)),
                       key=lambda index: (-evidence.blocks[index].score, index))
        kept: set[int] = set()
        used = 0
        for index in order:
            size = len(evidence.blocks[index].quote)
            if kept and used + size > DETAIL_RETRIEVAL_MAX_CHARS:
                continue
            kept.add(index)
            used += size
        index_of = {old: new for new, old in enumerate(sorted(kept))}
        evidence.blocks = [evidence.blocks[old] for old in sorted(kept)]
        evidence.by_node = {
            node_id: [index_of[index] for index in indices if index in index_of]
            for node_id, indices in evidence.by_node.items()
        }
    # 一块都没留下的节点从指针表里摘掉：留着空列表会让 `nodes_with_evidence` 把
    # "这个节点其实没有证据"数成有证据。
    evidence.by_node = {node_id: indices for node_id, indices in evidence.by_node.items()
                        if indices}
    # 块池按文档序排好（章号 → 章内偏移），渲染时的编号就是"读下去的第几块"。
    order = sorted(range(len(evidence.blocks)),
                   key=lambda index: (evidence.blocks[index].chapter_index,
                                      evidence.blocks[index].start))
    if order != list(range(len(evidence.blocks))):
        index_of = {old: new for new, old in enumerate(order)}
        evidence.blocks = [evidence.blocks[old] for old in order]
        evidence.by_node = {
            node_id: sorted(index_of[index] for index in indices if index in index_of)
            for node_id, indices in evidence.by_node.items()
        }


def _detail_evidence_section(
    targets: Sequence[tuple[Node, tuple[str, ...]]], evidence: _DetailEvidence,
) -> str:
    """这一半节点的证据：块池（每块带它属于哪一章）+ "哪个节点命中了哪几块"。

    只渲染**这一半节点用到的块**：两半各跑一次调用，把另一半的章也塞进来等于把
    "按章限定"这件事在最后一步又取消掉。

    指针表必须在块的**后面**且紧挨着清单：模型对紧挨着输出位置的那段话最敏感
    （同 `_detail_ask` 末尾那句自查）。块与指针分两次读也不冲突——编号是唯一锚点。
    """
    if not evidence.blocks:
        return ""
    picked: list[int] = []
    seen: set[int] = set()
    for node, _path in targets:
        for index in evidence.by_node.get(node.id, ()):
            if index not in seen:
                seen.add(index)
                picked.append(index)
    if not picked:
        return ""
    picked.sort()
    renumber = {old: new for new, old in enumerate(picked, start=1)}
    lines = [
        "下面是每个节点**在它自己那一章里**命中的原文段落（块按文档顺序编号，"
        "每条都标着它属于第几章）：",
    ]
    for old in picked:
        block = evidence.blocks[old]
        lines.append(
            f"\n[块 {renumber[old]}｜第 {block.chapter_index} 章"
            f"「{block.chapter_title}」]")
        lines.append(block.quote)
    pointers = [
        f"id={node.id} → " + "、".join(f"块 {renumber[index]}"
                                      for index in evidence.by_node.get(node.id, ())
                                      if index in renumber)
        for node, _path in targets
        if any(index in renumber for index in evidence.by_node.get(node.id, ()))
    ]
    if pointers:
        lines.append("\n各节点命中的块（没列到的节点，它的那一章里没有与它直接对应的段落，"
                     "按你的知识写，不要因此说「材料中没有」）：")
        lines.extend(pointers)
    return "\n".join(lines)


def _split_nodes(
    targets: Sequence[tuple[Node, tuple[str, ...]]],
) -> list[list[tuple[Node, tuple[str, ...]]]]:
    """把要补说明的节点切成两半：**按节点数均分，切点吸附到父边界**。

    为什么按节点数而不是按一级分支：墙钟由**输出量**决定，不由调用次数决定。
    "3 支 33 个 / 2 支 23 个"这种分法里，慢的那半要比均分多写 43% 的字，而两半并发
    的总耗时看的就是最慢的那一次——按支分等于把 26 秒退回 33 秒。按支分的唯一好处
    是"同一支一起写、说明更连贯"，可它挡不住模型写到第 20 段时把第 3 段忘掉，而
    那正是三段式本身要解决的注意力摊薄：用切分去救它，等于用一个更弱的手段去补
    一个已经被更强的手段（只写说明、不写结构）解决过的问题。

    **但"劈开一支"有一个代价是真的**：同一个父节点下的几个孩子要互相区分。三支
    各三个要点都挤在一起了还写成三段互不相干的话，读者看到的是"同一件事说了三
    遍"——这一条与跨支重复不同，代码测不出来，只能靠切点避开。所以切点吸附到最近
    的**父边界**（前序里同一父的孩子必然连续，因此"与上一项的 path 不同"就是换了
    一个父节点），两侧最多移动 `slack` 位；`slack` 内找不到边界（例如整棵树是
    "根 + 56 个并列一级子节点"）就直接按中点切——那种形状里本来就没有"兄弟要互相
    区分"的单元。56 个里最多挪 7 位，对墙钟的影响在 3% 以内。

    返回 **1 或 2** 个子序列，长度之和恒等于 `len(targets)`、各自保持前序。调用方
    按返回顺序合并，不需要再对号。
    """
    count = len(targets)
    if count < max(2, DETAIL_SPLIT_MIN):
        return [list(targets)]
    cut = (count + 1) // 2      # 前半拿 ceil：两半差一个节点，输出量差 3.5%
    slack = max(1, count // 8)

    def is_boundary(index: int) -> bool:
        # 前序里同一父节点的孩子 path 必然相同；跨支重名会让这个判据把边界认到
        # 更早一位——那是均分误差，不是错误。
        return targets[index][1] != targets[index - 1][1]

    low, high = max(1, cut - slack), min(count - 1, cut + slack)
    boundaries = [index for index in range(low, high + 1) if is_boundary(index)]
    if boundaries:
        # 距离相同取靠后的：前半多拿一点比后半多拿一点好——额度只够一次时
        # `_fill_details` 优先跑前半，让被保留的那一次覆盖更多节点。
        cut = min(boundaries, key=lambda index: (abs(index - cut), -index))
    return [list(targets[:cut]), list(targets[cut:])]


def _detail_ask(
    topic: str, targets: Sequence[tuple[Node, tuple[str, ...]]],
    sections: Sequence[str], *, full_text: bool = False,
    instruction: str | None = None, evidence: str = "",
    span: budget.DetailSpan | None = None,
) -> str:
    """第三段的 user 消息：主题 + 节点清单（id / label / 位置）+ 可选的原文。

    清单必须带**层级路径**：一个 16 字的 label（"实施要点""风险"）几乎在任何主题下
    都成立，模型不知道它在哪一层、上面那层讲的是什么，写出来的说明会飘到别的主题上
    （与 `_node_path` 对 `node_expand` 的理由同源）。**这条路径与"哪个节点的证据在
    哪一章"是两件事**：后者由 `evidence` 那一节给（见 `_detail_evidence_section`），
    而全塞那条路（没有章节边界可用）仍然只能靠它。

    带 id 是这一段唯一能对上号的东西：**不靠顺序**——顺序错一位，整棵树的说明就会
    整体串行错位，而且从产出上看不出来（每段话本身都通顺）。

    `full_text` 只影响末尾那段**引导句**，`sections` 本身的形状决定了它们是"按支分组
    的检索结果"还是"按材料分节的完整正文"：同一份内容配上错的引导句（"以本组为准"
    挂在全文上）会让模型以为每组之间互不相干。

    `evidence` 是第三种形态（按节点章内检索）：它是**已经渲染好的块池 + 指针表**，
    与 `sections` 互斥（调用了这条路就不再拼整支/整份）。空串表示这条路没走，此时
    本函数的输出与加这个参数之前**逐字节相同**——`tests/test_detail_round.py` 与
    `test_fill_details_tool.py` 里的回归断言比的就是那些 case。

    `span` 是这次的长度区间（`budget.detail_span`）。`None` = 没开伸缩，末尾那句自查
    用 `DETAIL_MIN_CHARS`/`DETAIL_MAX_CHARS`，与加这个参数之前逐字节相同。
    """
    span = span or _default_detail_span()
    lines = [
        f"{index}. id={node.id}｜{node.label}｜位置：{' / '.join(path) or '中心主题'}"
        for index, (node, path) in enumerate(targets, start=1)
    ]
    user = (
        f"主题：{topic}\n\n"
        f"这张图的结构已经定好了，共 {len(targets)} 个节点需要写说明。"
        "返回一个 JSON 对象，键是下面每个 id（原样照抄），值是它的说明：\n\n"
        + "\n".join(lines)
    )
    if sections:
        guide = (
            "\n\n结构那一轮用过的**完整正文**（按材料分节；写某个节点时先在它对应的"
            "那一份里找到那段原文）：\n\n"
            if full_text else
            "\n\n结构那一轮用过的原文（按一级分支分组；写说明时以本组为准）：\n\n")
        user += guide + "\n\n".join(sections)
    # 按节点章内检索那一条路：`sections` 此时是空的（两件事互斥，见 `_fill_details`），
    # 引导句因此写在这一节自己的开头（`_detail_evidence_section` 的第一行）。
    if evidence:
        user += "\n\n" + evidence
    # 补充要求挨着清单、在自查句**之前**（与 `_expand_ask` 同一个位置）：它约束的是
    # "这些段话怎么写"，自查句约束的是"写完有没有漏"。放在自查句之后会被读成
    # 自查的一部分（模型已经在准备输出了，那行字只当成格式提醒）。
    #
    # 只在调用方给了非空串时才出现：这一段为空时，user 与加这个参数之前逐字节相同
    # ——单条生成路径（`_generate` 永远不传）的回归断言比的就是这个。
    if instruction and instruction.strip():
        user += f"\n\n补充要求（写说明时遵守）：{instruction.strip()}"
    # 自查句放在**最末尾**：它紧挨着模型开始输出的位置。上面每一条规则都在 system 里，
    # 而实测"每段至少 3 句"这一条只有写在最后才会被逐条执行（见 `DETAIL_ONLY_PROMPT`
    # 的注释——前两轮有 1–3 个节点只写了一句）。
    #
    # 自查句必须跟着 system 走（`MINDMAP_DETAIL_SENTENCES`）：system 里已经放开句子数，
    # 这一行还在问"是不是都有 3 句以上"，等于把那条约束从末尾又装回去——而它恰是模型
    # 读到的最后一行。关着时这两句与加开关之前逐字节相同。
    user += _detail_self_check(len(targets), span)
    return user


def _detail_self_check(count: int, span: budget.DetailSpan) -> str:
    """user 末尾那句自查（`"\\n\\n"` 开头，直接拼在清单/证据之后）。

    单独一个函数而不是内联：它是**检查项**，不是要求——模型不会把它当成新规则，只会
    照着逐条过一遍。所以它的三项必须与 system 里的要求一模一样，而 system 那一份现在
    有开关（句子数）。两处各写一遍会漂开：改了一处、另一处还在查旧的那条，模型于是
    被"自查一遍"这四个字领着回去补齐一条已经放开的约束。

    计数用的是 `count`（这次点名了几个 id）而不是树规模：模型只需要核对清单，不必再
    数一遍树。
    """
    if detail_length_free():
        # 放开字数之后自查句不能再问"是不是都在 80–150 字"——那会把刚放开的那条
        # 又当成检查项要回来。只留"都写了"与"长短对不对"两件。
        return (f"\n\n写完后自查一遍：上面 {count} 个 id 是不是都写了？"
                "每一段的长短是不是跟它在材料里的分量相称？"
                "讲得多的别写短、讲得少的别硬拉长，然后再返回。")
    if detail_sentences_open():
        return (f"\n\n写完后自查一遍：上面 {count} 个 id 是不是都写了？"
                f"每一段是不是都在 {span.min_chars}–{span.max_chars} 字？"
                "不够的补写，然后再返回。")
    return (f"\n\n写完后自查一遍：上面 {count} 个 id 是不是都写了？"
            f"每一段是不是都有 {span.sentences} 句以上、{span.min_chars}–"
            f"{span.max_chars} 字？"
            "不够的补写，然后再返回。")


_DETAIL_WRAPPERS = ("details", "detail", "nodes", "data", "map")


def _detail_map(
    raw: Any, known: Sequence[str],
) -> tuple[dict[str, str], list[str], list[str]]:
    """模型返回 → `(id → 说明, 缺的 id, 不存在的 id)`。

    三种形状都认，因为它们都出现过：`{"d1":"…"}`（提示词要的那种）、
    `{"details":{…}}`（多包一层）、`[{"id":"d1","detail":"…"}]`（模型忘了"用 id
    做键"这条，改成列表）。识别只做在**形状**上，不做在内容上：值不是字符串就当
    没给，空串当没给——**不替模型编一段**。

    `known` 之外的键进第三个返回值（`unknown`）。它不是致命错（模型偶尔把
    `root` 写成 `"root "`），但必须能看见：一个都不匹配时，这张图的说明会整批
    空着，而"模型写了 60 段话"和"模型什么都没写"在产出上长得一样。
    """
    if isinstance(raw, dict) and len(raw) == 1:
        key = next(iter(raw))
        inner = raw[key]
        if key in _DETAIL_WRAPPERS and isinstance(inner, (dict, list)):
            raw = inner
    pairs: list[tuple[str, Any]] = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, dict):
                value = value.get("detail")
            pairs.append((str(key), value))
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            node_id = item.get("id") or item.get("node_id")
            pairs.append((str(node_id or ""), item.get("detail")))
    allowed = set(known)
    applied: dict[str, str] = {}
    unknown: list[str] = []
    for node_id, value in pairs:
        key = node_id.strip()
        if not key:
            continue
        if key not in allowed:
            unknown.append(key)
            continue
        text = _detail_text(value)
        if text:
            applied[key] = text
    missing = [node_id for node_id in known if node_id not in applied]
    return applied, missing, unknown


def _with_details(node: Node, mapping: dict[str, str]) -> Node:
    """把 `id → 说明` 套回树上（重建节点，不动任何其他字段）。"""
    children = [_with_details(child, mapping) for child in node.children]
    update: dict[str, Any] = {"children": children}
    text = mapping.get(node.id)
    if text:
        update["detail"] = text
    return node.model_copy(update=update)


@dataclass
class _DetailHalf:
    """半次「只写说明」调用的产物与账。

    为什么是一个类型而不是继续用 dict：两半并发跑，**谁先回来不确定**，而每半的
    账必须跟着它自己的 id 清单走（"第 2 半缺了 3 个、分别是哪几个"里的 label 只能
    从这一半的 `missing` 推）。拼成一个 dict 再拆，就得在合并处重新猜"这半原本
    要写哪几个"。
    """

    requested: int = 0
    # 这一半**要写**的节点 id，按切分时的前序。与 `applied` 的键不同：那个只装
    # 写回来的，这个是清单本身。留着它是因为补完说明之后树上再也还原不出切分
    # （补过的节点不再满足 `_needs_detail`），而"两半各写了哪些节点"正是要量的东西。
    target_ids: list[str] = field(default_factory=list)
    applied: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    seconds: float = 0.0
    # "error"（这一次调用抛错）/ None。`"budget"` 不在这里：那半**根本没发请求**，
    # 由 `_fill_details` 直接决定，进不到这个类型里。
    skipped: str | None = None
    failure: str = ""

    def account(self) -> dict[str, Any]:
        """进 `data["detail"]["halves"]` 的那份账。键与整体那份同名前缀，方便对比。

        `ids` 也在这里：没有任何别的地方能还原"哪一半写了哪些节点"——切分是按当时
        的待补清单算的，而这个函数返回时那棵树已经被改过（补过的节点不再满足
        `_needs_detail`），事后从树上重建切分必然错位。两半的风格是否分叉、两半的
        输出量是否均衡，这两件要量的事都靠它；`calls` 那样的纯测量字段早就在
        `data` 里，它不是新引入的负担。
        """
        return {
            "requested": self.requested,
            "filled": len(self.applied),
            "missing": len(self.missing),
            "unknown_keys": len(self.unknown),
            "seconds": self.seconds,
            "skipped": self.skipped,
            "ids": list(self.target_ids),
        }


async def _fill_details_half(
    targets: Sequence[tuple[Node, tuple[str, ...]]],
    *,
    ctx: RunContext,
    topic: str,
    sections: Sequence[str],
    external_rules: str,
    full_text: bool = False,
    instruction: str | None = None,
    evidence: _DetailEvidence | None = None,
    span: budget.DetailSpan | None = None,
) -> _DetailHalf:
    """发这一次「只写说明」的请求并解析。**预算是调用方的事**（见 `_fill_details`）。

    这里只兜一种失败：调用抛 `GenerationError`。两件事它**不**做，各有理由：

      - **不占额度**：额度在 `_fill_details` 里按半**串行**预占完再并发发请求。
        各自占的话，"只剩一份额度该写哪一半"就由 asyncio 的调度顺序决定，那是
        不确定行为，测试也钉不住。
      - **不往共享的 `diagnostics` 里写**：两半并发，谁先 append 不确定，而诊断是
        给人按顺序读的（"第 1 半缺了 2 个"应该排在"第 2 半缺了 3 个"前面）。诊断
        因此由 `_fill_details` 在合并时按半序生成。

    `evidence` 非空时走第三种形态（按节点章内检索）：块池是**全树共享**的，但每一半
    只渲染自己节点用到的那部分——两半各跑一次调用，"另一半的章"对它就是无关材料，
    这正是这条路要减掉的东西。三份 system 互斥：全塞 / 按支分组 / 按节点章内，
    引导句与给的证据形状必须成对，错配会让模型把"这一章的块"读成"整份材料"。
    """
    half = _DetailHalf(requested=len(targets),
                       target_ids=[node.id for node, _ in targets])
    span = span or _default_detail_span()
    section = _detail_evidence_section(targets, evidence) if evidence is not None else ""
    # 区间参数化只换两个数字：`span` 是默认区间时 `_detail_prompt` 的输出与
    # `DETAIL_ONLY_PROMPT` 逐字节相同（同一个构造函数，同一组数；句子数开关关着时
    # 它就是 `_detail_only_prompt`）。
    system = _detail_prompt(span.min_chars, span.max_chars)
    if section:
        system += DETAIL_ONLY_RETRIEVED_RULES + external_rules
    elif sections and full_text:
        # 全塞：给的是完整正文，没有"本组"这个概念（分组来自按支检索）。用错引导句
        # 会让模型以为每份材料各管一支、跨份的内容不写——那正好浪费掉全塞的覆盖优势。
        system += DETAIL_ONLY_FULL_TEXT_RULES + external_rules
    elif sections:
        system += DETAIL_ONLY_EVIDENCE_RULES + external_rules
    else:
        system += DETAIL_NO_SOURCE_RULES
    started = time.perf_counter()
    try:
        raw = await _chat(
            ctx, system,
            _detail_ask(topic, targets, sections, full_text=full_text,
                        instruction=instruction, evidence=section, span=span),
            max_tokens=_max_tokens(ctx))

    except GenerationError as exc:
        half.skipped = "error"
        half.failure = str(exc)
        half.seconds = round(time.perf_counter() - started, 3)
        return half
    half.seconds = round(time.perf_counter() - started, 3)
    applied, missing, unknown = _detail_map(raw, [node.id for node, _ in targets])
    half.applied, half.missing, half.unknown = applied, missing, unknown
    return half


def _settle_halves(
    results: Sequence[Any],
    pending: Sequence[Sequence[tuple[Node, tuple[str, ...]]]],
) -> list[_DetailHalf]:
    """`gather(return_exceptions=True)` 的结果 → 每半一份账。**先重抛取消**。

    `return_exceptions=True` 会把 `CancelledError` 一并收进返回值。吞掉它等于让
    `max_seconds` 超时与用户中途取消失效：工具还在跑、模型请求还在计费，而
    `_run_tool` 那边以为已经停了（它靠 `task.cancel()` + `await` 收尾）。所以这里
    第一件事就是把它原样抛回去，一个都不能漏。

    非预期异常（`GenerationError` 之外的）只影响它自己那一半：另一半的结果必须
    保住——这正是 `return_exceptions=True` 在这里的全部意义。记成
    `skipped="error"`，`failure` 里带上异常类型：`GenerationError` 之外的错多半是
    代码问题，只看"调用失败"四个字分不出是哪一类。
    """
    settled: list[_DetailHalf] = []
    for index, item in enumerate(results):
        if isinstance(item, asyncio.CancelledError):
            raise item
        if isinstance(item, BaseException):
            settled.append(_DetailHalf(
                requested=len(pending[index]), skipped="error",
                failure=f"{type(item).__name__}: {item}",
            ))
            continue
        settled.append(item)
    return settled


def _incomplete_message(
    settled: Sequence[_DetailHalf],
    targets: Sequence[tuple[Node, tuple[str, ...]]],
    applied: dict[str, str],
    missing: Sequence[str],
) -> str:
    """`DETAIL_ROUND_INCOMPLETE` 的那句话。**拆过的话要说清是哪一半缺的**。

    单半（节点数不到 `DETAIL_SPLIT_MIN`）时的措辞与拆分之前**逐字相同**：那一批
    量本来就小，"哪一半"这个维度不存在，多一句只是噪声；而且回归用例钉着它。

    拆过时按半报，三件事都要能读出来：这一半缺了几个、是哪些、以及"没额度"与
    "调用失败"这两种**根本没发出请求**的情形与"模型少写了"分开——三者的处置
    完全不同（前两种换一次重试就有救，第三种重试也未必）。
    """
    labels = {node.id: node.label for node, _ in targets}
    total = len(targets)
    if len(settled) == 1:
        head = "、".join(f"「{labels[node_id]}」" for node_id in missing[:5])
        return (
            f"补写说明的那一次调用只写回了 {len(applied)}/{total} 个节点，"
            f"其余 {len(missing)} 个保持原样（{head}"
            + ("…" if len(missing) > 5 else "")
            + "）；缺的说明没有被补写——不替模型编一段"
        )
    parts: list[str] = []
    for index, half in enumerate(settled, start=1):
        if half.skipped == "budget":
            parts.append(f"第 {index} 半（{half.requested} 个）没有额度、请求没发出去")
            continue
        if half.skipped == "error":
            parts.append(f"第 {index} 半（{half.requested} 个）调用失败")
            continue
        # 另一半越界返回、把这个 id 填上了的话，这里就不该再说它"缺"。
        gap = [node_id for node_id in half.missing if node_id not in applied]
        if not gap:
            parts.append(f"第 {index} 半（{half.requested} 个）齐全")
            continue
        head = "、".join(f"「{labels.get(node_id, node_id)}」" for node_id in gap[:3])
        parts.append(
            f"第 {index} 半只写回 {len(half.applied)}/{half.requested}"
            f"（缺 {len(gap)} 个：{head}"
            + ("…" if len(gap) > 3 else "")
            + "）"
        )
    return (
        f"补写说明的两次调用一共写回了 {len(applied)}/{total} 个节点："
        + "；".join(parts)
        + "。缺的说明没有被补写——不替模型编一段"
    )


async def _fill_details(
    content: MindMapContent, *, ctx: RunContext, topic: str,
    sections: Sequence[str], external_rules: str,
    diagnostics: list[Diagnostic], skip_ids: Collection[str] = (),
    full_text: bool = False,
    instruction: str | None = None,
    chapters: Sequence[_Chapter] = (),
    source: Source | None = None,
    material_chars: int = 0,
) -> tuple[MindMapContent, dict[str, Any]]:
    """第三段：给缺说明的节点补 detail。**只读结构、只写 detail**。

    **拆成两次并发调用**（节点数够的话，见 `DETAIL_SPLIT_MIN`）：慢的原因是输出量
    而不是调用次数——56 个节点 × 105 字 ≈ 5900 字，输出侧就要 30 秒。两半各写一半
    的输出，墙钟由最慢的那一半决定。总调用从 3 变 4，墙钟从 44.5 秒降到 26 秒上下
    （数字见 `docs/ONE-CALL.md`）。

    五条兜底各对应一种真实失败，缺任何一条都会让它比不加这一段更糟：

      - **少返回几个**：缺的节点保持原样（多半是空），记一条 warn 把 id 列出来。
        **不替它编一段**——编出来的说明与引用、与 label 都没有关系，而且看不出来。
      - **返回不存在的 id**：忽略，但把个数记进 `data` 与诊断（对不上号时这是唯一
        的线索）。
      - **返回空串 / 非字符串**：按"没给"处理（`_detail_map`）。
      - **一半失败 / 一半成功**：成功那半的结果必须落地（`return_exceptions=True`
        + 各自的兜底），失败的只记它自己那一半。
      - **两半都失败**：整棵树照样交付（`detail` 是可选字段），只记 warn。为了
        "说明写短了"把已经生成好的结构丢掉，是拿确定的好换不确定的好。

    引用在这条路上**不可能被改动**：函数只从返回值里读 `id → 字符串`，写回的字段
    只有 `detail`。`source_refs` 仍然是结构那一轮的产物（`EVIDENCE_UNUSED` 的分母
    也因此不受影响）。

    `instruction` 是**从检查点续写**那条路专用的（`_fill_details_tool`）：用户在
    界面上说"说明写长一点/口语一点"时，它进 `_detail_ask` 的补充要求那一段。生成
    路径永远不传，所以那条路上的 user 与加这个参数之前逐字节相同。

    `chapters` / `source` 是**按节点章内检索**那条路的输入（`MINDMAP_DETAIL_RETRIEVAL`）：
    有了章才谈得上"这一章"。两个都为空的调用方（`_fill_details_tool` 的续写路径、
    多份材料、没开按章切）走的仍是原来那条"整支/整份"证据——**prompt 与 data 逐
    字节相同**。给了章但一次命中都没有时同样退回原来那条（见下面的 `fallback`），
    因为"检索没成"不该把本来能用的证据一起丢掉。

    `material_chars` 是这次材料的**总字数**（含外部检索补进来的量），只在
    `MINDMAP_DETAIL_SCALE` 开着时用来算"每段说明该写多长"。分母是**真的会写说明的
    节点数**（`targets`）而不是配额：树被裁到 30 个节点时，每段分到的是 30 份材料。
    不传（0）时按"没有材料"处理，退回 `DETAIL_MIN_CHARS/MAX_CHARS` 那一份常量区间。
    """
    targets = _detail_targets(content.root, skip=skip_ids)
    report: dict[str, Any] = {
        "requested": len(targets), "filled": 0, "missing": 0,
        "unknown_keys": 0, "seconds": 0.0,
    }
    if not targets:
        # 结构那一轮自己就写够了说明（或被裁到只剩几个节点）：不花这次调用。
        report["skipped"] = "nothing_to_fill"
        return content, report

    # ---- 长度：这一段写多长由材料撑不撑得住决定 --------------------------------
    #
    # 与节点配额是**同一件事的两半**：`MINDMAP_NODE_SCALE` 决定"摊几个节点"，这里决定
    # "每个节点写多长"。只开前一个时，48 个节点 × 130 字仍是 6,240 字——对 5,616 字
    # 的材料还是膨胀（κ 0.90）。`span.scaled` 为假时两个数与常量逐字节相同：system、
    # 自查句、`data["detail"]` 的键集都不变（`test_detail_round` 钉着）。
    span = budget.detail_span(
        material_chars=material_chars, nodes=len(targets),
        floor_chars=DETAIL_MIN_CHARS, ceiling_chars=DETAIL_MAX_CHARS)
    if span.scaled:
        # 只在真接管时多一个键：默认路径的 `data["detail"]` 键集必须与改动前相同。
        report["chars"] = span.as_dict()
        # 句子数放开时把 `sentences` 报成 0（"这次不设句数下限"）：留在 3 会让 `data`
        # 说一句与 prompt 相反的话，而这一格正是事后判"模型听没听话"的依据。
        if detail_sentences_open():
            report["chars"]["sentences"] = 0
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.info, code="DETAIL_SCALE_APPLIED",
            message=(
                f"这次按材料规模要求每段说明写 {span.min_chars}–{span.max_chars} 字"
                f"（材料 {material_chars} 字 / {len(targets)} 个待写节点 = 每节点"
                f" {span.support:.1f} 字支撑，目标压缩比 {span.kappa:g}；"
                f"开关 `{budget.DETAIL_SCALE_ENV}`）；不设该开关时恒为 "
                f"{DETAIL_MIN_CHARS}–{DETAIL_MAX_CHARS} 字"
            ),
        ))

    # 句子数放开（`MINDMAP_DETAIL_SENTENCES`）：只在真接管时多一个键 + 一条 info。
    # 不加这条诊断的话，"这次没有句数下限"只存在于 prompt 里，事后看 `data` 分辨不出
    # 一段说明偏短是模型没听话，还是这一次本来就没要求它写满三句。
    if detail_sentences_open():
        report["sentences_open"] = True
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.info, code="DETAIL_SENTENCES_OPEN",
            message=(
                f"这次每段说明不设句数下限，只要求 {span.min_chars}–{span.max_chars} 字"
                f"（开关 `{DETAIL_SENTENCES_ENV}`）；不设该开关时会要求至少 "
                f"{span.sentences} 句完整的话"
            ),
        ))

    # ---- 证据：每个节点在它自己那一章里检索（开关 + 两种输入都齐了才走）---------
    #
    # 检索放在**切半之前**：两半的节点天然不相交，但块池必须共享（同一章的 5 个
    # 节点命中的是同一批父块），在这里算一次、两半各渲染自己那部分。
    evidence: _DetailEvidence | None = None
    if chapters and source is not None and detail_retrieval_enabled():
        evidence = await _detail_evidence(
            targets, mapping=_node_chapters(content.root, chapters), source=source)
        # 一条都没命中（材料不是这份来源、检索全部抛错、标签全是模型造的词）时不
        # 把 `sections` 丢掉：那是**已有**的证据，换成一个空块池等于把这一段退化成
        # "凭知识写"。账里留 `fallback`，否则"这条路这次没生效"在产出上无从分辨。
        report["evidence"] = {**evidence.report(),
                              "fallback": "" if not evidence.empty() else "no_hits"}
        if evidence.empty():
            evidence = None
        else:
            # 块池已经是逐节点的章内证据，整支/整份那份不必再进 prompt——两份并存
            # 等于把范围重新放大，这条路就没有意义了。
            sections = ()

    halves = _split_nodes(targets)
    # ---- 额度：一次占完，两半只负责花 -------------------------------------
    #
    # `reserve_model_call()` 读-判-写之间**没有 await**，在 asyncio 的单线程里不会
    # 被切走，所以两半并发各占一次也不会算错（多占、少占都不会发生）。但"不会算错"
    # 之外还有一件更要紧的事：**只剩一份额度时该写哪一半**。交给调度器决定（谁先跑
    # 到谁拿）就成了不确定行为，测试也钉不住。所以在这里串行占完，够几份就写几半。
    granted = 0
    for _ in halves:
        if not ctx.reserve_model_call():
            break
        ctx.extra_model_calls += 1
        granted += 1
    if not granted:
        report["skipped"] = "budget"
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="DETAIL_SKIPPED_NO_BUDGET",
            message=(
                f"模型调用额度已用完，{len(targets)} 个节点的说明没有补写；"
                "结构与引用都已完成，只是说明会偏短"
            ),
        ))
        return content, report

    pending = halves[:granted]
    # 帧由这里统一推，**两半自己不推**：前端 `stageText` 是覆盖式单值，两半各推一帧
    # "正在给 28 个节点写说明"只会互相顶掉（同一句话闪两遍）；而各推一帧"已写完
    # 28"更糟——总共 56 个，那句话在另一半还没回来时就是谎报。所以计数一律用切半
    # **前**的总数，帧数与文案与拆分之前逐字相同（开始一帧、结束一帧）。
    await _emit_stage(ctx, DETAIL_STAGE, f"正在给 {len(targets)} 个节点写说明…")
    results = await asyncio.gather(*(
        _fill_details_half(half, ctx=ctx, topic=topic, sections=sections,
                           external_rules=external_rules, full_text=full_text,
                           instruction=instruction, evidence=evidence, span=span)
        for half in pending
    ), return_exceptions=True)
    settled = _settle_halves(results, pending)
    if granted < len(halves):
        # 只占到最后一份额度：后半**根本没发请求**。补一条账，让 `halves` 的条数与
        # 节点数对得上——少了它，`halves` 看起来像"这棵树本来就只切了一半"。
        skipped = [node for node, _ in halves[granted]]
        settled.append(_DetailHalf(
            requested=len(skipped), skipped="budget",
            target_ids=[node.id for node in skipped],
            missing=[node.id for node in skipped]))

    # ---- 合并 ------------------------------------------------------------
    #
    # 两半的 id **天然不相交**，不需要运行时去重：`_detail_map` 按各半自己的 id
    # 清单过滤，不在清单里的键一律进 `unknown`、不进 `applied`。而越界的那批 id
    # 也**不能**采用——那一半的 user 里没有这些节点的 label 与位置（`_detail_ask`
    # 只列本半的清单），模型为它们写的话是凭空编的，却长得完全像一段正常说明。
    # 所以这里只管累加，落空的那点由 `unknown_keys` 报出来。
    applied: dict[str, str] = {}
    for half in settled:
        applied.update(half.applied)
    missing: list[str] = []
    for half in settled:
        missing.extend(item for item in half.missing
                       if item not in applied and item not in missing)
    unknown = {key for half in settled for key in half.unknown}

    report["filled"] = len(applied)
    report["missing"] = len(missing)
    report["unknown_keys"] = len(unknown)
    # `seconds` 是**这一步的墙钟**，不是两次之和：两半并发，`calls["detail"]` 要与
    # 总耗时对得上，把两个 17 秒加成 34 秒只会让对比表整列失真。每半各自的秒数在
    # `halves` 里——那正是"两半的输出量是否均衡"的证据。
    report["seconds"] = round(max((half.seconds for half in settled), default=0.0), 3)
    if len(settled) > 1:
        # 只在**真的拆过**时才有这个键，理由与 `data["external"]` 同源：没拆时
        # `data["detail"]` 的键集与加这套并发之前逐字节相同，回归用例比的是键集。
        report["halves"] = [half.account() for half in settled]

    if applied:
        content = content.model_copy(
            update={"root": _with_details(content.root, applied)})
    await _emit_stage(ctx, DETAIL_STAGE,
                      f"已写完 {len(applied)} 个节点的说明", count=len(applied))

    if not applied and not [half for half in settled if half.skipped != "error"]:
        # 有额度的半**全部**调用失败。这时候再说"没有返回任何可用的说明"是拿一个
        # 更笼统的原因盖住真原因（模型 500 / 超时），下面那条 `DETAIL_CALL_FAILED`
        # 才是要读的东西。原单次实现也是这个分工：失败就 return，不报 EMPTY。
        pass
    elif not applied:
        # 只报"整体一段都没写回来"。有半失败、另半写回来了，走下面的 INCOMPLETE
        # ——那一条里会说清楚是哪一半。
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="DETAIL_ROUND_EMPTY",
            message=(
                f"补写说明的这次调用没有返回任何可用的说明"
                f"（{len(targets)} 个节点一个都没对上"
                + (f"，其中 {len(unknown)} 个键不是节点 id" if unknown else "")
                + "），这些节点的说明保持为空；内容会明显短于预期"
            ),
        ))
    elif missing or granted < len(halves):
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="DETAIL_ROUND_INCOMPLETE",
            message=_incomplete_message(settled, targets, applied, missing),
        ))
    if len(settled) > 1:
        for index, half in enumerate(settled, start=1):
            if half.skipped == "error":
                diagnostics.append(Diagnostic(
                    level=DiagnosticLevel.warn, code="DETAIL_CALL_FAILED",
                    message=(
                        f"补写说明的第 {index} 半调用失败：{half.failure}；"
                        f"这一半的 {half.requested} 个节点说明保持原样（多半是空），"
                        "另一半的结果已落地，结构与引用不受影响"
                    ),
                ))
        if all(half.skipped == "error" for half in settled):
            # 两半都失败 = 整件事"一次也没做成"，与不拆时的那条出路同名同义，
            # 读 `data.detail.skipped` 的代码不需要为拆分多分一个分支。
            report["skipped"] = "error"
    elif settled and settled[0].skipped == "error":
        report["skipped"] = "error"
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="DETAIL_CALL_FAILED",
            message=(
                f"补写说明的那一次调用失败：{settled[0].failure}；"
                f"{len(targets)} 个节点的说明保持原样（多半是空），结构与引用不受影响"
            ),
        ))
    return content, report


# ---------------------------------------------------------------- 一级分支概括


BRANCH_DIGEST_PROMPT = (
    "你在给一张已经写好的思维导图做目录批注。它的一级分支已经定下来，每一条下面"
    "挂着若干具体节点。\n\n"
    "任务：给**每一条一级分支**写一句概括，让没读过这张图的人一眼看出这一支讲什么。\n\n"
    "硬要求：\n"
    "1. 8–12 个汉字，写成名词短语；不要写成一整句话，也不要以动词开头。\n"
    "2. **不许复用分支标题里的词**：标题里出现过的两字以上的实词，一个都不要写进概括。"
    "标题就印在节点上，概括如果只是把它换一种说法（「相似度打分与权重分配」→"
    "「相似度打分与归一化权重」），那个位置就白占了。\n"
    "3. 从这一支**下位节点的词**里取词。它们比标题具体得多，而这就是这条概括唯一的"
    "信息来源——概括要说的是「这一支的下位节点在讲什么」，不是「这一支叫什么」。\n"
    "4. 几条之间不许同义重复：两条概括说得像同一件事，说明至少有一条没读下位节点。\n"
    "5. 不要出现「这一支」「本部分」「相关内容」「核心要点」「介绍」这类空词。\n"
    "6. 不要编号、不要书名号、结尾不要标点。\n\n"
    "返回一个 JSON 对象：键是每条一级分支的 id（原样照抄），值是那句概括。"
    "不要解释，不要多余的话。"
)

# 一句概括的字数上限。提示词要 8–12，这里卡 14 是**容错而不是目标**：模型偶尔多写
# 两三个字，截掉比整条丢掉好（截掉的仍是这一支的真实概括，丢掉就只剩数字）。
# 上限比提示词宽 2 个字，是为了让"12 个字 + 一个没数进去的标点"不至于被腰斩。
BRANCH_DIGEST_MAX_CHARS = 14

# 概括要剥掉的首尾字符。**只剥标点与引号，不剥字**：`「训练与并行」`与
# `训练与并行。` 都是模型常给的形状，留着它们在角标上就是一层多余的框。
_DIGEST_TRAILING = " \t　。．.，,、；;：:！!？?…·「」『』\"'“”‘’（）()《》"


@dataclass
class _BranchDigestReport:
    """这一次概括调用的账（进 `data["branch_digest"]`）。与 `_DetailHalf` 同一条规矩：
    一个会花掉秒级时间、会多一次模型调用的步骤，必须留下决定。
    """

    branches: int = 0
    written: int = 0
    # 少了哪几支（模型没写回 / 写回来的是空串）。角标对这几支**不画**——它退化成
    # "这一支还有 N 个"，而那件事 `+N` 与顶部「显示 N / M」已经说过一遍了。
    missing: list[str] = field(default_factory=list)
    # 模型给了 id 但对不上号（它把自己编的分支名当成了键）。非致命，但必须能看见：
    # "模型写了 6 段话" 与 "一段都没对上" 在产出上长得一模一样。
    unknown: list[str] = field(default_factory=list)
    seconds: float = 0.0
    # None = 真的调了；"few_branches" / "budget" / "error" = 没调成或没调。
    skipped: str | None = None
    failure: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def account(self) -> dict[str, Any]:
        return {
            "branches": self.branches,
            "written": self.written,
            "missing": list(self.missing),
            "unknown": list(self.unknown),
            "skipped": self.skipped,
            "failure": self.failure,
            "seconds": self.seconds,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


def _branch_digest_ask(rows: Sequence[tuple[Node, Sequence[str]]], topic: str) -> str:
    """这一次调用的 user：主题 + 每条一级分支一行（id / 标题 / 下位节点的标题）。

    **给 id 而不是靠顺序**，理由与 `_detail_ask` 同一条：顺序错一位，六条概括就会
    整体串行错位，而从产出上看不出来（每条本身都通顺）。

    **只给下位节点的 label，不给 `detail`**：一段 80–150 字的说明 ×6 支会把这一节
    从两百字撑到一千字，而"只看目录"恰恰比"读完正文"更容易写出准确的概括——正文
    里的细节会把概括拽向其中某一句，得到的是一段摘要而不是这一支的边界。

    主题那一行是**锚**：同一批标题在"注意力机制"与"报销流程"两张图里该概括成
    不同的词，没有它，模型只能按标签的字面去猜。
    """
    lines = []
    for index, (node, children) in enumerate(rows, start=1):
        tail = f"｜下位：{'、'.join(children)}" if children else ""
        lines.append(f"{index}. id={node.id}｜{node.label}{tail}")
    return (
        f"主题：{topic}\n\n"
        f"这张图的一级分支共 {len(rows)} 条：\n\n" + "\n".join(lines)
        + f"\n\n请给这 {len(rows)} 条各写一句概括，用上面给出的 id 做键。"
    )


def _digest_text(raw: Any) -> str | None:
    """模型给的一句概括 → 能放上画布的字符串（或 `None`）。

    四件事：非字符串当没给、折掉换行（角标只有一行高）、剥掉首尾标点与引号、超过
    `BRANCH_DIGEST_MAX_CHARS` 截断。**不判空之外的语义**——不改写、不补全。概括写得
    不如标题，是这一支该不该显示角标的问题（前端那条判据），不是这里能修的事。
    """
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.split())
    for prefix in ("一句概括：", "一句话概括：", "概括：", "概括:", "概括——"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.strip(_DIGEST_TRAILING)
    if not text:
        return None
    return text[:BRANCH_DIGEST_MAX_CHARS]


def _digest_map(
    raw: Any, known: Sequence[str],
) -> tuple[dict[str, str], list[str], list[str]]:
    """模型返回 → `(id → 概括, 缺的 id, 对不上的键)`。

    三种形状都认（与 `_detail_map` 同样宽容，理由是同一个：三种都真的出现过）：
    `{"d1": "…"}`、`{"digests": {...}}`、`[{"id": "d1", "summary": "…"}]`。

    **不复用 `_detail_map`**：值的清洗规则不同（那是 2000 字允许换行的一段说明，
    这是 14 字不许换行的一句标题），列表形状的取值键也不同（`summary` / `digest` /
    `detail` 三选一）。把差别塞成参数，两个调用点就会各带一半参数、而"哪个参数
    决定行为"再也读不出来——这一处只有 20 行，分开写更清楚。
    """
    if isinstance(raw, dict) and len(raw) == 1:
        key = next(iter(raw))
        inner = raw[key]
        if key in ("digests", "digest", "branches", "summary", "data", "map") \
                and isinstance(inner, (dict, list)):
            raw = inner
    pairs: list[tuple[str, Any]] = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, dict):
                value = value.get("summary") or value.get("digest") or value.get("detail")
            pairs.append((str(key), value))
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            node_id = item.get("id") or item.get("node_id")
            value = item.get("summary") or item.get("digest") or item.get("detail")
            pairs.append((str(node_id or ""), value))
    allowed = set(known)
    applied: dict[str, str] = {}
    unknown: list[str] = []
    for node_id, value in pairs:
        key = node_id.strip()
        if not key:
            continue
        if key not in allowed:
            unknown.append(key)
            continue
        text = _digest_text(value)
        if text:
            applied[key] = text
    missing = [node_id for node_id in known if node_id not in applied]
    return applied, missing, unknown


async def _fill_branch_digests(
    content: MindMapContent, ctx: RunContext, *, topic: str,
) -> tuple[MindMapContent, _BranchDigestReport]:
    """一次批量调用，给每条一级分支写一句概括（`MINDMAP_BRANCH_DIGEST`）。

    **时机是"说明那一次之后"**：那时每个节点的 `detail` 都写完了，一级分支也已经是
    最终形态（骨架分组、裁剪、配色全部落定）。放在结构那一次之后也调得通，但那时
    一级分支的标题还是骨架的原话、下属节点还没定稿——用它写出来的概括会在二十秒后
    过期。

    **失败一律降级**：调用抛错、额度用完、模型一条都没写回，都只是让
    `content.branch_digests` 保持空表，角标退回"只显示数字"（前端那条判据）。为一句
    概括把一棵已经成型的树判成失败，是拿确定的好换不确定的好——与 `_fill_details`
    同一条方向。

    **只对树**：鱼骨的一级是固定六类（人机料法环测，概括就是这六个字），流程的一级
    是阶段（标题本身就是它的定义）。两者都调不出新信息，而那两条路的产物根本没有
    `branch_digests` 字段。
    """
    report = _BranchDigestReport()
    branches = list(content.root.children)
    report.branches = len(branches)
    # 少于两条分支时不调：一条分支的概括只能是它的标题，而单支的树在画布上也不会
    # 被截成"这一支还有 N 个"（根 + 那一支就在预算里）。
    if len(branches) < 2:
        report.skipped = "few_branches"
        return content, report
    rows = [
        (node, [child.label for child in node.children[:BRANCH_DIGEST_CHILD_SAMPLES]])
        for node in branches[:BRANCH_DIGEST_MAX_BRANCHES]
    ]
    user = _branch_digest_ask(rows, topic)
    # 额度在**发请求之前**占：与骨架那一步同一规矩（请求一旦发出就是一次真实计费）。
    if not ctx.reserve_model_call():
        report.skipped = "budget"
        return content, report
    ctx.extra_model_calls += 1
    report.prompt_tokens = estimate_input_tokens(BRANCH_DIGEST_PROMPT) + \
        estimate_input_tokens(user)
    started = time.perf_counter()
    try:
        raw = await _chat(
            ctx, BRANCH_DIGEST_PROMPT, user,
            max_tokens=min(BRANCH_DIGEST_MAX_TOKENS, _max_tokens(ctx)))
    except GenerationError as exc:
        report.skipped = "error"
        report.failure = _reason_text(exc)
        report.seconds = round(time.perf_counter() - started, 3)
        return content, report
    report.seconds = round(time.perf_counter() - started, 3)
    applied, missing, unknown = _digest_map(raw, [node.id for node in branches])
    report.written = len(applied)
    report.missing = missing
    report.unknown = unknown
    if not applied:
        report.skipped = "error"
        report.failure = "模型没有给出任何可用的概括"
        return content, report
    report.completion_tokens = estimate_input_tokens(
        json.dumps(applied, ensure_ascii=False))
    return content.model_copy(update={"branch_digests": applied}), report


# ---------------------------------------------------------------- 结构自检回路


@dataclass
class _SelfCheckReport:
    """这一次自检调用的账（进 `data["self_check"]`）。

    与 `_BranchDigestReport` 同一条规矩：一个会花掉秒级时间、会多一次模型调用的
    步骤，必须留下决定——但这一份还多一个职责：**它是那份清单本身**。诊断只说一句
    "发现 N 处"，可指认性（哪个节点、什么问题、怎么改）全在这里。
    """

    #: `None` = 真的调了；`"budget"` / `"error"` / `"no_outline"` = 没调成或没调。
    skipped: str | None = None
    failure: str = ""
    #: 清单。每一条是 `{node_id, kind, problem, fix, path}`，`path` 是给人读的位置。
    issues: list[dict[str, Any]] = field(default_factory=list)
    #: 按 kind 计数（诊断文案与 `data` 都用它）。
    counts: dict[str, int] = field(default_factory=dict)
    #: 丢掉的条目：`unknown_kind`（模型自创了一类）、`unknown_id`（id 对不上号）、
    #: `over_limit`（超出 `SELF_CHECK_MAX_ISSUES`）、`bad_item`（不是一个对象）。
    #: **不能静默丢**：报 15 条、落地 8 条，而"少了 7 条"在产出上看不出来。
    dropped: dict[str, int] = field(default_factory=dict)
    #: 这一次喂进去的清单规模（节点数 / 行数 / 字数）。它是"输入很小"这句判断的证据。
    nodes: int = 0
    lines: int = 0
    chars: int = 0
    seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def account(self) -> dict[str, Any]:
        return {
            "skipped": self.skipped,
            "failure": self.failure,
            "issues": list(self.issues),
            "counts": dict(self.counts),
            "dropped": dict(self.dropped),
            "outline": {"nodes": self.nodes, "lines": self.lines, "chars": self.chars},
            "seconds": self.seconds,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


def _self_check_outline(root: Node) -> tuple[str, dict[str, str]]:
    """树 → 缩进清单 + `id → 位置路径`。**只给 label 与 id，不给 detail**。

    不给 `detail` 是这条回路的核心取舍，三条理由都在实测里（报告 4.1）：

      - 输入规模：一张 80 节点的图，目录约 1.0–1.2 k token；加上正文会翻三倍以
        上，而这张图的全部收益（跨支重复 / 语义重复 / 跑题 / 层级异常）**没有一类
        需要正文**——它们判的都是标题与位置；
      - 判据纯度：给了正文，"标题与内容不符"会盖住"结构本身有问题"，而后者才是
        这一轮要的那件事（前者归 `MINDMAP_VERIFY_AFTER_GENERATE` 那条路）；
      - 时机：它跑在**说明那一次之前**，那时树上根本没有 detail（`_fill_details`
        还没跑）。等写完再看，输入一个字都不会多。

    一级分支单独一行带 `【一级分支】`，其余按深度缩进；**每一行都有 id**，因为
    清单要按 id 指认。没有 id 的清单只能靠"第几个分支第几个节点"描述，而那个描述
    在模型改了措辞之后就对不上了。
    """
    lines: list[str] = []
    paths: dict[str, str] = {}

    def walk(node: Node, depth: int, chain: tuple[str, ...]) -> None:
        for child in node.children:
            label = child.label.strip()
            if depth == 0:
                lines.append(f"【一级分支】{label}  (id={child.id})")
                paths[child.id] = label
                walk(child, depth + 1, (label,))
                continue
            lines.append(f"{'  ' * depth}- {label}  (id={child.id})")
            paths[child.id] = " / ".join((*chain, label))
            walk(child, depth + 1, (*chain, label))

    walk(root, 0, ())
    return "\n".join(lines), paths


def _self_check_text(raw: Any) -> str:
    """一条 `problem` / `fix` 的清洗：折空白、截断。空值返回空串（那条不显示）。"""
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.split())[:SELF_CHECK_TEXT_CHARS]


def _self_check_issues(
    raw: Any, paths: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """模型返回 → `(清单, 丢掉的计数)`。

    **只做结构清洗，不判真假**：类不在四类里就丢（自创一类说明它没按契约答，
    留进来会让界面出现无法分组的条目）、id 不在树里就丢（用户看到"某条问题指向
    一个不存在的节点"只会更困惑）、不是对象的条目丢。

    一条都不"复核"是刻意的：`optimization` 那七条规则能复核的三类（逐字重复 /
    同级重名 / 空链）这里**故意不比**——`repair()` 与 `check()` 已经在报它们了，
    这条回路的价值恰恰在"逐字判据看不见的那一类"（报告：8 条 `unjudged` 里 7 条
    成立）。在这里再判一次，等于用同一把尺子量它量不到的东西。
    """
    issued: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    items = raw
    if isinstance(items, dict):
        items = items.get("issues")
        # 模型偶尔套一层 `{"issues": {"items": [...]}}`：认一次，不多认。
        if isinstance(items, dict):
            items = items.get("issues") or items.get("items")
    if not isinstance(items, list):
        items = []

    for item in items:
        if not isinstance(item, dict):
            dropped["bad_item"] = dropped.get("bad_item", 0) + 1
            continue
        kind = str(item.get("kind") or "").strip()
        if kind not in SELF_CHECK_KINDS:
            dropped["unknown_kind"] = dropped.get("unknown_kind", 0) + 1
            continue
        # `nodes` 是列表形状的宽容（实测两种都出现过）：一条报了两个节点就摊成两条，
        # 每条各自带同一个 `problem` / `fix`——清单的消费方是"按节点核对"。
        ids = item.get("node_id") or item.get("nodes") or []
        if isinstance(ids, str):
            ids = [ids]
        if not isinstance(ids, list) or not ids:
            dropped["unknown_id"] = dropped.get("unknown_id", 0) + 1
            continue
        for node_id in ids:
            key = str(node_id or "").strip()
            if key not in paths:
                dropped["unknown_id"] = dropped.get("unknown_id", 0) + 1
                continue
            issued.append({
                "node_id": key,
                "kind": kind,
                "path": paths[key],
                "problem": _self_check_text(item.get("problem")) or _self_check_text(
                    item.get("why")),
                "fix": _self_check_text(item.get("fix")),
            })
    if len(issued) > SELF_CHECK_MAX_ISSUES:
        dropped["over_limit"] = dropped.get("over_limit", 0) + (
            len(issued) - SELF_CHECK_MAX_ISSUES)
        issued = issued[:SELF_CHECK_MAX_ISSUES]
    return issued, dropped


async def _run_self_check(
    content: MindMapContent, ctx: RunContext, *, topic: str,
) -> _SelfCheckReport:
    """一次审查调用：结构写完之后回头看一遍，报一份**可指认的问题清单**。

    ## 位置：结构之后、说明之前

    那一刻标签已经定稿（说明那一次只填 `detail`，不改任何 `label`），而清单要的
    只有标签与 id——等说明写完再看，输入一个字都不会多，代价是多等十几秒。
    往前挪到结构之前也不行：那时树上只有骨架，下位节点还没写。

    ## 它不改树

    `_generate` 拿到这份清单之后**一个节点都不动**（`content` 原样往下走）：
    精度 22/25 = 88%，自动落地会伤到那 12%，而合并是不可逆的
    （`docs/FOLD-RETIRED.md` 的教训）。产出是"哪一支、什么问题、怎么改"，落地交给
    用户——这也正是"可指认"要的东西。

    ## 失败一律降级

    额度用完（`budget`）/ 调用抛错（`error`）/ 树上没有可审的东西（`no_outline`）
    都只让这次没有自检，生成照常交付。为一份清单把一棵已经成型的树判成失败，是拿
    确定的好换不确定的好——与 `_fill_branch_digests` 同一条方向。
    """
    report = _SelfCheckReport()
    outline, paths = _self_check_outline(content.root)
    report.nodes = len(paths)
    report.lines = outline.count("\n") + 1 if outline else 0
    report.chars = len(outline)
    # 少于三条一级分支时不调：一张两三条分支的小图上"跨支重复"要么不存在，要么
    # 一眼可见；而这条回路每次要花一次调用。判据与 `MIN_BRANCHES` 无关，是经济性。
    if len(content.root.children) < 3:
        report.skipped = "no_outline"
        return report

    user = (
        f"主题：{content.title.strip()}\n\n"
        f"这张图有 {len(content.root.children)} 条一级分支、共 {report.nodes} 个节点。"
        f"目录如下（缩进表示层级，(id=…) 是该节点的 id）：\n\n{outline}\n\n"
        "请按系统提示里的四类，报出你**确实能判定**的问题。"
    )
    # 额度在**发请求之前**占：与骨架、概括那两步同一规矩（请求一旦发出就是一次真实计费）。
    if not ctx.reserve_model_call():
        report.skipped = "budget"
        return report
    ctx.extra_model_calls += 1
    report.prompt_tokens = (estimate_input_tokens(SELF_CHECK_PROMPT)
                            + estimate_input_tokens(user))
    started = time.perf_counter()
    try:
        raw = await _chat(
            ctx, SELF_CHECK_PROMPT, user,
            max_tokens=min(SELF_CHECK_MAX_TOKENS, _max_tokens(ctx)))
    except GenerationError as exc:
        report.skipped = "error"
        report.failure = _reason_text(exc)
        report.seconds = round(time.perf_counter() - started, 3)
        return report
    report.seconds = round(time.perf_counter() - started, 3)
    issues, dropped = _self_check_issues(raw, paths)
    report.issues = issues
    report.dropped = dropped
    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue["kind"]] = counts.get(issue["kind"], 0) + 1
    report.counts = counts
    report.completion_tokens = estimate_input_tokens(
        json.dumps(issues, ensure_ascii=False))
    return report


@dataclass(frozen=True)
class _ExternalFetch:
    """一次外部检索的产物。

    `sources` 为空串（空列表）表示"没有能用的外部资料"，`failure` 区分两种原因：
    非空是**检索本身出错**（代理没开、源 500、超时），None 是**源里确实没有**
    （中文主题在 arXiv 上 0 条是常态）。两者给用户的文案必须分开——把"这次没查成"
    说成"网上没有"是谎报，反过来则是让人白等一次重试。

    `cached` 说明这批结果是**缓存复用的**（24 小时内的同一个 query）还是这次真搜的。
    它只用于 `data.external.cached` 与日志，**不影响任何下游判断**：缓存命中的那一批
    与真搜回来的那一批在后面走的是同一条路（转 `Source`、入库、检索、引用校验），
    入库那一步会因 `content_hash` 判重变成 no-op，那是对的。
    """

    sources: list[Source]
    hits: int
    elapsed: float
    failure: str | None = None
    cached: bool = False


def _external_source(hit: external_knowledge.ExternalHit) -> Source:
    """一条外部命中 → `Source`。**外部内容进入契约的唯一入口。**

    五个字段各有理由，改任何一个都会在链路上留下一个静默失败：

      - `filename = f"{source_name}: {title}"`：材料清单里要能一眼看出这是维基的
        哪一条、arXiv 的哪一篇、快懂百科的哪个词条。只写标题分不出源，只写源没有
        信息量——而"这条从哪来"正是用户判断可信度的唯一依据。
      - `kind` 按源表映射成 `web` / `paper`：界面要能把"我的材料"与"外面抓的"
        分开——两者可信度不同（见 `EXTERNAL_TRUST_RULES`）。
      - `metadata.url`：原文地址，前端渲染成可点击链接。这是"引用能点回原文"的
        唯一输入。
      - `metadata.raw_chars`：截断前的长度。没有它，"这条本来就有 8000 字"与
        "被砍掉了九成"长得一模一样。
      - `status="parsed"`：正文已经在手上。`_verify_sources` 与
        `source_refs._reference_problem` 都按这个值判"这份来源能不能当校验依据"，
        少一个条件就是一类静默剔除。

    `content_hash` **不填**：`library.ingest` 会按正文重算，这里填一个错的值只会
    多一条 warning（见它的 docstring）。

    `fetched_at` 与 `language` **不进** `Source`：`SourceMetadata` 里没有对应字段，
    硬塞进 `tags` / `category` 会让过滤语义漂移（那两个字段的约定是"用户给的分类"）。
    它们是抓取过程的描述——与 `library.load` 不还原页数、截断标记是同一条取舍。

    `authority` / `rerank_score` 进 `metadata` 的同名字段：那是 DeepSeek Search
    自己给的排序元数据，**不参与检索打分**（理由见契约里的注释）。把它塞进 `tags`
    会让它们跟着标签过滤漂；塞进 `category` 更糟——那个字段的约定是"用户给的业务
    分类"，写进去之后用户按分类筛就会筛出两个他不认识的分类名。

    `site`（搜索源给的站点名）进 `tags` 的**第二项**：`tags[0]` 仍然是抓取层的
    `source_name`（`_external_source_names` 靠它拼"来自哪几个站"），站点名是附加
    信息。空串不塞——`tags=["deepseek", ""]` 会让 `clean_tags` 之外的读者看到一项
    空白标签。
    """
    text = external_knowledge.truncate_text(hit.text)
    tags = [hit.source_name]
    if hit.site:
        tags.append(hit.site)
    return Source(
        kind=_EXTERNAL_KINDS.get(hit.source_name, SourceKind.web),
        # 外部标题可能很长（arXiv 的标题有 200 字以上的），文件名跟着长会把
        # `list_sources` 与日志都撑坏；200 字足够认出是哪一条。
        filename=f"{hit.source_name}: {hit.title}"[:200],
        text=text,
        status="parsed",
        metadata=SourceMetadata(
            url=hit.url, raw_chars=max(hit.raw_chars, len(text)),
            tags=tags,
            authority=hit.authority,
            rerank_score=hit.rerank_score,
        ),
    )


def _external_query(topic: str, short_sources: Sequence[Source]) -> str:
    """这次外部检索用的查询词。

    平时就是主题。**材料太短时把材料并进来**：一份 8 字的 `rust 学习路线` 当材料是
    废物（切不出一块、引用落不到段落），当检索词却正好是用户想说的那件事——它往往
    比一个空泛的主题更接近意思（"帮我规划学习路线"这种主题单独拿去搜，回来的全是
    泛泛的"学习法"）。

    两条去重规则只为了避免把同一句话查两遍：材料常常就是主题的复述（用户把主题
    粘进上传框），拼起来会得到 `rust 学习路线 rust 学习路线`——`QUERY_MAX_CHARS=50`
    还不知道会砍掉哪一半。

    超长由抓取层自己截断（`external.QUERY_MAX_CHARS`），这里不重复一套长度规则。
    """
    base = (topic or "").strip()
    hint = " ".join((source.text or "").strip() for source in short_sources).strip()
    if not hint:
        return base
    if hint in base:
        return base
    if base and base in hint:
        return hint
    return f"{base} {hint}".strip()


def _external_source_names(sources: Sequence[Source]) -> str:
    """这批来源来自哪几个站，拼成"openalex、wikipedia"。

    只用于给用户看的句子：`Source.metadata.tags` 的第一项就是抓取层的
    `source_name`（见 `_external_source`），所以不需要第二张映射表。取不到时退回
    `filename` 的前缀（`"wikipedia: 碳中和"` → `wikipedia`），再取不到就说"外部"——
    一个说不出来源的句子没有意义，但也不该因此抛错。
    """
    names: list[str] = []
    for source in sources:
        tags = list(source.metadata.tags or [])
        name = tags[0] if tags else (source.filename or "").split(":", 1)[0].strip()
        if name and name not in names:
            names.append(name)
    return "、".join(names) or "外部"


def _has_external_source(sources: Sequence[Source]) -> bool:
    """这批来源里有没有外部检索抓回来的（`kind` 是 `web` / `paper`）。

    只用于决定要不要拼 `EXTERNAL_TRUST_RULES`：给一批纯用户上传的材料加一句
    "这是不可信数据"会让模型对用户的材料也过敏，加免责声明、写得更保守。
    """
    return any(source.kind in (SourceKind.web, SourceKind.paper) for source in sources)


def _external_stage_text(fetch: _ExternalFetch) -> str:
    """外部检索结束那一帧给人看的话。四种结果四句话。

    用词与 `content` 里的 `GENERATION_WITHOUT_EXTERNAL` / `GENERATION_FROM_EXTERNAL`
    对齐（"没有搜到外部资料"/"外部检索出错"）：同一次运行的界面与正文各说一套话，
    用户会以为是两件事。

    "找到了但没能入库"单独一句，因为把它说成"没有搜到"是谎报（抓到了，只是存不下），
    而说成"已找到 N 条"又是空头支票（模型一条也用不上）。
    """
    if fetch.sources:
        return f"已找到 {len(fetch.sources)} 条外部资料"
    if fetch.hits:
        return f"找到 {fetch.hits} 条外部资料，但没能存进资料库"
    if fetch.failure:
        return "外部检索没成功，按主题生成"
    return "联网检索没有找到相关资料，按主题生成"


async def _external_sources(topic: str, ctx: RunContext) -> _ExternalFetch:
    """主题 → 外部检索 → 入库 → 本次可用的来源。

    三件事必须同时成立，缺任何一件这条链接上去都是负资产：

      1. **入库拿持久 id**。`Source.id` 默认是请求内的临时 uuid，而引用要能跨请求
         点回原文——只有 `library.ingest` 给的持久 id 做得到（`library.load()` 是
         界面上"点开一条引用"唯一的回查入口）。
      2. **并进 `ctx.record_library_sources`**。收尾的 `_verify_sources(ctx)` 只看
         `request.sources + ctx.library_sources`；不进这里的外部来源，其引用会在
         `verify_source_refs(remove_invalid=True)` 那一步被判"来源不存在"并静默
         剔除——检索成功、引用写出、最后消失，这是整条链上最隐蔽的一种失败。
      3. **失败即空**。外部源全挂时返回空列表，调用方退回"只凭主题"的那条路，
         并在 `content` 里说明。生成不能因为一次外网超时失败。

    耗时与条数各记一行日志：这是用户点"生成"之后**多出来的**一次等待，混在总耗时
    里就永远看不出"慢在哪"（与 `_search_branch` 的同一条规矩）。

    `cached` 由 `search_external` 的返回值带出来（`ExternalHits.cached`），用
    `getattr` 取：这条链的十几条用例把这个函数 `monkeypatch` 成返回普通 `list` 的
    lambda，那上面没有这个属性，而"没有标记"在这里的正确读法就是"没命中缓存"。
    """
    started = time.perf_counter()
    query = (topic or "").strip()
    try:
        # 同步函数，最多 `external_knowledge.REQUEST_LIMIT` 次 HTTP 往返、墙钟上限
        # `DEADLINE_SECONDS`。直接在协程里调会把事件循环按住这么久——同进程里别人的
        # 流式响应会跟着卡（同 `_search_branch`）。
        hits = await asyncio.to_thread(
            external_knowledge.search_external, query, limit=EXTERNAL_HIT_LIMIT)
    except Exception as exc:  # noqa: BLE001 — 外部检索失败只降级，理由见 docstring
        elapsed = time.perf_counter() - started
        reason = _reason_text(exc)
        logger.warning("外部检索失败（耗时 %.2fs）：%s", elapsed, reason)
        return _ExternalFetch([], 0, elapsed, reason)

    cached = bool(getattr(hits, "cached", False))
    built: list[Source] = []
    for hit in hits:
        source = _external_source(hit)
        try:
            source_id = knowledge_library.ingest(source)
        except Exception as exc:  # noqa: BLE001 — 单条入库失败不废整次检索
            logger.warning("外部来源入库失败（%s / %s）：%s：%s",
                           hit.source_name, hit.title[:40],
                           type(exc).__name__, str(exc)[:160])
            continue
        built.append(source.model_copy(update={"id": source_id}))
    if built:
        ctx.record_library_sources(built)

    elapsed = time.perf_counter() - started
    logger.info(
        "外部检索：主题=%r 入库 %d 条（抓取 %d 条，%s），耗时 %.2fs",
        query[:50], len(built), len(hits), "缓存命中" if cached else "本次真搜", elapsed)
    return _ExternalFetch(built, len(hits), elapsed, None, cached)


# ---------------------------------------------------------------- 工具实现


def _profile_pool(ctx: RunContext) -> str:
    """这次运行的方向池键。**新图还没有 `map_id`，所以退到 `run:<run_id>`。**

    依据（判据与推导见 `profile.pool_key` 的 docstring）：`AgentRunRequest` /
    `DiagramContent` / `RunContext` 三处都没有 `map_id`——图在服务端要等
    `repository.create_map` 才拿到 uuid4，而生成发生在保存之前。`run_id` 是唯一
    在生成开始前就存在、且能区分"这次运行"的标识。

    所以"一张图一次生成 = 一个方向池"。**新图天然是一个新池**（新 run_id），也就是
    用户要的那句"新开一张导图就不推了"；两张不同主题的图之间没有任何共享的向量，
    跨图污染在结构上就不可能，不是靠某处 if 挡住的。

    保存之后再 `expand` 同一张图：又是新 run_id、新池，但材料还是同一批
    （`request.sources` 带着），而聚类是确定性的——算出来是同一批方向。池键只决定
    "存在哪一行、按谁读回"，不决定"算出来是什么"。
    """
    from app.prefs import profile as profile_rank

    map_id = getattr(ctx.request, "map_id", None)
    run_id = getattr(ctx.request, "run_id", None)
    return profile_rank.pool_key(map_id=map_id, run_id=run_id)


def _run_directions(
    ctx: RunContext, sources: Sequence[Any],
) -> list[profile_rank.Direction]:
    """这次运行的画像方向：**只从这张图的材料算，一次运行只算一次**。

    三件事同时在这里落地：

      1. **隔离**：输入只有 `sources`（用户为这张图传的材料），没有一次库查询、
         没有别的图的任何痕迹。图 A 的方向不可能出现在图 B 的列表里；
      2. **零往返**：`build_directions_from_sources` 只读 `chunk_vectors` 里现成的
         块向量，不调 `embed_texts`。开关打开也不再"多花几次 embedding"；
      3. **一次**：结果挂 `ctx`（理由见 `RunContext.profile_directions`）。

    失败一律降级成空列表（= 这次不加权 + 一条诊断）：画像是排序上的二阶修正，
    算不出来时正确的行为是"这次不做个性化"，不是"这次检索失败"。
    """
    pool = _profile_pool(ctx)
    if ctx.profile_directions is not None and ctx.profile_pool == pool:
        return ctx.profile_directions
    if not profile_rank.enabled():
        # 开关关着时**一次查询都不发**（不读 `chunk_vectors`、不聚类），并且不把
        # 结果写进 `ctx`：写进去会让"开关在运行中途被打开"读到一份开关关着时
        # 缓存的空列表，而那个空列表与"真的没有方向"分不开。
        return []
    try:
        directions = profile_rank.build_directions_from_sources(list(sources))
    except Exception as exc:  # noqa: BLE001 — 见 docstring 最后一段
        logger.warning("画像方向算不出来（%s），这次不做个性化加权",
                       type(exc).__name__)
        directions = []
    ctx.profile_directions = directions
    ctx.profile_pool = pool
    return directions


async def _profile_rank(
    hits: list[EvidenceHit], directions: Sequence[Any],
) -> tuple[list[EvidenceHit], list[str], str]:
    """按**这张图的**画像重排检索命中。**只改顺序，不改集合**。

    返回（重排后的列表，被抬上来的那几条的定位键 `source_id:start`，未加权的原因）。
    第二个返回值是**过滤气泡检验**的唯一入口：把"哪些命中是因为画像才进了前几名"
    记下来，才可能在事后问出"这些命中后来被删掉了几条"。定位键只到偏移、不带原文
    ——引文是材料正文，不进日志（与 `knowledge.embedding` 的注释同一条纪律）。
    第三个返回值是"这次为什么没有个性化"（空串 = 正常加权过）：没有它，用户看到的
    只有"个性化好像没生效"，而"这张图的材料还没有块向量"与"开关关着"长得一样。

    `directions` 由调用方给（`_run_directions`），`boost` 不再自己去读库：读全表
    正是跨图串味的那条路。漏传在这里是 `TypeError`，不是静默的全局加权。

    `app/prefs/profile.py` 的三条硬约束在这里落成三行：

      1. 返回的是同一个列表的另一个顺序——没有一条被加进来或去掉。画像决定
         "这几条谁先"，不决定"要不要这条"；拿去过滤就是过滤气泡。
      2. 开关（`MINDMAP_PROFILE_RERANK`）默认关。**打开之后也不再新增 embedding
         往返**（方向复用的是块向量缓存），这条约束的成本理由已经基本消失。
      3. 任何失败都返回原顺序：画像是排序上的一个二阶修正，它算不出来时
         正确的降级是"这次不做个性化"，不是"这次检索失败"。

    放进 `to_thread` 是因为 `boost` 里有一次同步的 embedding HTTP（0.3–1 秒，命中
    缓存时为 0）：直接在事件循环里调用会把同一台服务器上别人的流一起卡住——与
    `_search_branch` 把 `search_parent` 扔进线程池同一个理由。
    """
    if not hits or not profile_rank.enabled():
        return hits, [], ""
    try:
        result = await asyncio.to_thread(
            profile_rank.boost, hits, directions=list(directions))
    except Exception as exc:  # noqa: BLE001 — 见 docstring 第 3 条
        logger.warning("画像加权跳过（%s），这次按原顺序交付", type(exc).__name__)
        return hits, [], ""
    return result.hits, [_hit_key(hit) for hit in result.lifted], result.reason



def _hit_key(hit: Any) -> str:
    """一条命中的定位键：`source_id:start`。只到偏移，不带原文。"""
    return f"{getattr(hit, 'source_id', '')}:{getattr(hit, 'start', '')}"


async def _search_branch(
    sources: list[Source], title: str, intents: Sequence[str] = (),
    *, directions: Sequence[Any] | None = None,
) -> list[EvidenceHit] | None:
    """一条分支的检索：标题（`_branch_query`）**加至多 2 条同义问法**，各 top_k=3。

    为什么不是"主题 + 标题"——**实测选出来的**，三组消融（`docs/RAG-DECISIONS.md`
    12,392 字、8 支、纯 BM25，`scripts/measure_deep_slicing.py --query-mode`）：

      | 分支 query | 导图覆盖率 | 8 支共用 top-1 | 两两 Jaccard | 并集段落 |
      |---|---|---|---|---|
      | 主题 + 标题 | 19.2% | 8/8 | 0.32 | 9/52 |
      | **只发标题（现在这一版）** | **28.8%** | 3/8 | 0.13 | 13/52 |
      | 只发主题 | 3.8% | 8/8 | 1.00 | 2/52 |

    "8 支共用 top-1" 是这里的关键指标：它数的是"有几支问了同一件事"。主题串让这个
    数字变成 8/8——每支都先把文档标题块（主题词在那里反复出现）取回来，8 次检索
    实际只覆盖一处。去掉主题后降到 3/8，覆盖率随之从 19.2% 涨到 28.8%。

    同时记下它的天花板：只发标题也不是万能——骨架把标题猜成"背景/现状/方法"这类
    通用词时，覆盖率掉到 15.4%。要突破得让骨架看到材料的章节标题（见 `_branch_query`
    的注释），那是下一轮的事。

    抛错返回 `None`——"没检索到"与"检索失败"要能分开记：前者是材料里确实没这块
    （这一支按知识展开、不挂引用是对的），后者是这次检索没成（可能换个配置就有了）。
    `None` 与 `[]` 就是这两个意思。

    **两条路径，按"有没有扩展"分岔，不是按开关分岔**：

      - `intents` 为空（开关关着、`plan_branches` 降级成 `{}`、或这一支模型没给
        问法）→ 与这个能力上线之前**逐字节相同**：`to_thread(search_parent, ...)`，
        父块返回、子块匹配。这一条是本次改动的降级契约。
      - `intents` 非空 → `search_multi`（多路 + RRF + 原 query 保底席位）。它的
        粒度**跟着 `MINDMAP_PARENT_RETRIEVAL` 走**，与单路那条完全同构：关着时是
        单级块（`start/end` 就是匹配单元、`leaf_*` 为 None），打开时是"子块匹配 →
        子块级融合 → 折父块 → 重排"（`leaf_*` 是命中的那 200 字）。所以两条路径
        返回的证据形状一致，`_citable_hits` 不需要第二套定义。

    不另写融合：RRF、去重、保底席位、`MAX_ROUTES` 全在 `search_multi` 里，这里
    再实现一遍等于让同一件事有两处真相，而两处迟早分叉。

    单路那条路径放进 `asyncio.to_thread`：`search_parent` 是同步函数，里面有一次
    embedding 网络往返。直接在协程里调用等于把 20 支串成一条队；线程池让它们真的
    同时发。多路那条**不需要** to_thread：`search_multi` 是协程，它内部已经把
    向量往返扔进线程池（见 `service._route_vectors`）。

    `directions` 不传 = **这一支不做画像加权**，不是"读全局方向"。这个默认值是
    "按图隔离"的边界：`_retrieve_branches`（批量口径，留作钉桩）与单测不传它，
    于是它们的行为与画像这一层上线之前逐字节一致；生成链路的 `safe_search` 会传
    `_run_directions(ctx, sources)`，那是唯一有权加权的地方。
    """
    query = _branch_query(title)
    queries = _branch_queries(title, intents)
    try:
        if len(queries) <= 1:
            found = await asyncio.to_thread(
                search_parent, sources, query, DEEP_BRANCH_TOP_K,
                keep=branch_keep())
        else:
            found = await search_multi(sources, queries, DEEP_BRANCH_TOP_K,
                                       keep=branch_keep())
    except Exception as exc:  # noqa: BLE001 — 检索失败只降级，不该废掉整张图
        logger.warning("分支检索失败（%s / %d 路）：%s: %s",
                       title, len(queries), type(exc).__name__, str(exc)[:160])
        return None
    # 画像只改这一支内部"这三条谁先"（`DEEP_BRANCH_TOP_K = 3`），不改这一支有没有
    # 命中。抬上来的那几条不需要在这里记账：过滤气泡检验看的是 `_rag` 那一路
    # （模型主动检索），骨架的每支检索没有可供事后对齐的节点级痕迹。
    ranked, _, _ = await _profile_rank(found, directions or ())
    return ranked


async def _expand_branches(
    sources: list[Source], titles: list[str],
) -> dict[str, list[str]]:
    """分支标题 → 同义问法；**成本闸就在这里**：单支不值得多一次模型往返。

    为什么单支不扩展：一条分支的检索本来只有一次多路融合的收益，却要多付一次
    8 秒上限的模型调用；而"多支"时这一次调用是**共享**的（`plan_branches` 一次
    处理全部标题），摊薄之后才划算。这也是它该放在这里的理由——调用点不该知道
    "几条分支才值得扩展"这条政策。

    永不抛：`plan_branches` 自己已经把所有失败收成 `{}`，这里再兜一层是为了
    `CancelledError` 之外的意外（例如 `_material_inputs` 被将来的改动弄成会抛）。
    扩展失败不该让整张图失败——它只是"这一轮少了几路召回"。
    """
    if len(titles) < 2:
        return {}
    try:
        return await plan_branches(titles, materials=_material_inputs(sources))
    except Exception as exc:  # noqa: BLE001
        logger.warning("分支扩展失败（%d 支）：%s: %s",
                       len(titles), type(exc).__name__, str(exc)[:160])
        return {}


async def _retrieve_branches(
    sources: list[Source], titles: list[str], *,
    expansions: dict[str, list[str]] | None = None,
) -> list[list[EvidenceHit] | None]:
    """N 支各检索一次：**第一支先单独跑，其余并行**。

    `_generate_deep` 已改走逐支流水线（`run_branch`），**不再经过这个函数**。它留
    下来是这条"批量检索"口径的唯一实现：预热策略、并发形状、第一支串行那道闸的
    成本数字都写在这里，而流水线复用的正是同一条策略——只是把"其余支检索完才
    展开"换成了"各自检索完就展开"。`tests/test_branch_expansion.py` 第 2 节还拿
    它钉住"降级时每支只发标题、走 `search_parent`"，所以它不该跟着入口一起删。

    为什么要先串一条：`search_parent` 每次都要把本次候选块的向量拿到手，而块向量
    是按文本哈希缓存的（`knowledge.vector_cache`）。N 支同时发出去的后果是**每一支
    都认为自己是第一次**——实测 8 支冷启动发出 8 × 69 = 552 条向量文本（24 个批次），
    20 支发出 1380 条（60 个批次），是"预热之后"的 17 倍。第一支跑完，缓存里已经有
    这批块的向量，后面 19 支各自只需要 embed 自己那一条 query。

    代价是"最慢的一支"从 max 变成 first + max(其余)，也就是多一次检索的延迟
    （本机纯 BM25 几毫秒，带向量时约一次 embedding 往返）。换的是向量请求量从
    N×块数 降到 块数 + N，这是成本闸该有的形状。

    **`expansions` 不影响这个结构**：扩展只是让每支多带一两条 query，第一支仍然
    是串行的——多路时"预热"发生在 `_route_vectors` 的块向量那一次线程池批里，
    与单路是同一笔成本，先跑一支同样把缓存填上。

    失败就地降成 `None`（不用 `gather(return_exceptions=True)` 再逐点判类型）：
    调用方只需要知道"这一支有没有证据"。第一支失败不影响其余各支照常并行。
    """
    if not titles:
        return []
    expanded = expansions or {}
    first = await _search_branch(sources, titles[0], expanded.get(titles[0], ()))
    if len(titles) == 1:
        return [first]
    rest = await asyncio.gather(
        *(_search_branch(sources, title, expanded.get(title, ()))
          for title in titles[1:]))
    return [first, *rest]


async def _retrieve_whole(
    sources: list[Source], topic: str, titles: list[str],
) -> list[EvidenceHit]:
    """整树一次检索：分片检索**全部**拿不到证据时的兜底（`DEEP_FALLBACK_TOP_K`）。

    query 是**主题 + 全部一级分支标题**。这里与按支检索的规则刻意不同：那条路每支
    一条 query，需要**区分度**（主题串会让 8 支的 top-1 撞成同一个父块，实测见
    `_search_branch`），所以不拼主题；这条路只有一条 query、要的是**召回**，多拼
    几个词只有好处——用户填的主题经常是"整理管理办法"这种指令，光拿它去查 BM25
    可能一个 token 都不命中（实测：材料正文里只有"预算审批""绩效考核"，"管理办法"
    只出现在文件名里），而分支标题正是模型认为这张图该覆盖的几块。

    条数取 5 而不是把各支的 3 加起来：兜底的语义是"退回改造之前的行为"
    （整树一次检索那条老路拿到的就是 top_k=5），不是"缩水版降级"。
    """
    query = " ".join([topic.strip(), *titles]).strip()
    try:
        return await asyncio.to_thread(
            search_parent, sources, query, DEEP_FALLBACK_TOP_K)
    except Exception as exc:  # noqa: BLE001 — 兜底也失败时只剩"无证据展开"，不是失败
        logger.warning("整树兜底检索失败：%s: %s", type(exc).__name__, str(exc)[:160])
        return []


async def _search_text(sources: list[Source], query: str) -> list[EvidenceHit] | None:
    """按一段自由文本检索——**补查专用**，与 `_search_branch` 是同一条检索栈。

    为什么单独一个函数而不是复用 `_search_branch`：那个函数的 query 由
    `_branch_query(title)` 从**分支标题**拼出来，语义是"这一支叫什么"；补查要发的是
    这一支里**最长的那句子节点文字**，两者不是一回事。硬塞进去会让"分支 query 就是
    标题本身"这条被实测钉过的规则出现一个例外，下一个人再读就看不出边界在哪。

    失败返回 `None`（与 `_search_branch` 一致）：补查失败只等于"这一支仍然没有依据"，
    不该废掉已经展开好的内容。
    """
    text = (query or "").strip()
    if not text:
        return None
    try:
        return await asyncio.to_thread(search_parent, sources, text, DEEP_BRANCH_TOP_K,
                                       keep=branch_keep())
    except Exception as exc:  # noqa: BLE001 — 补查失败只降级
        logger.warning("补查检索失败（%d 字 query）：%s: %s",
                       len(text), type(exc).__name__, str(exc)[:160])
        return None


def _merge_branch_evidence(
    old: "_BranchEvidence | None", new: _BranchEvidence, sources: list[Source],
) -> _BranchEvidence:
    """把补查到的证据并进这一支：`spans` / `payload` / `quotes` 三份一起扩。

    不能只换掉 `payload`：`spans` 是收尾校验判"这条引用是不是本支证据"的依据，
    只扩 payload 会让新抄进来的引用在 `_ref_from` 那里被当成越界丢掉；反过来只扩
    spans 会让模型看不到新证据。两份必须同源。

    去重按 `(source_id, start, end, leaf_start, leaf_end)`：补查的 query 是从这一支
    自己的内容里取的，与原 query 命中同一段是常态，不去重会把同一段文字在 prompt 里
    印两遍。
    """
    if old is None:
        return new
    spans = tuple(dict.fromkeys((*old.spans, *new.spans)))
    payload = [*old.payload]
    seen = {(item.get("source_id"), item.get("start"), item.get("end"))
            for item in payload}
    payload.extend(
        item for item in new.payload
        if (item.get("source_id"), item.get("start"), item.get("end")) not in seen
    )
    return _BranchEvidence(
        payload=payload,
        spans=spans,
        text=json.dumps(payload, ensure_ascii=False),
        quotes="\n".join(
            part for entry in payload
            for part in (entry.get("quote"), entry.get("evidence_quote"))
            if isinstance(part, str) and part
        ),
    )


def _slot_ref_count(slots: list[_Slot]) -> int:
    """这一支（含多给的下层）一共写了多少条引用。触发补查的判据之一。"""
    return sum(len(slot.source_refs) + _slot_ref_count(slot.children) for slot in slots)


def _longest_slot_label(slots: list[_Slot]) -> str:
    """这一支里**最长**的子节点文字，补查用它当 query。

    为什么不是再发一次分支标题：标题已经查过了，再查一遍回来的是同一批命中
    （`_branch_query` 的消融数字就是这么测出来的）。展开之后才有的东西是这一支
    写出来的具体说法——它更长、更接近正文用词，是唯一还没被试过的问法。
    """
    labels: list[str] = []
    stack = list(slots)
    while stack:
        slot = stack.pop()
        labels.append(slot.label)
        stack.extend(slot.children)
    return max(labels, key=len).strip() if labels else ""


def _needs_supplementary(raw: Any, slots: list[_Slot]) -> bool:
    """这一支要不要补查一次（限额见 `SUPPLEMENTARY_*`）。

    两个触发条件，都在展开**之后**判：

      1. 模型自己说这支配不上依据：响应里带 `needs_evidence: true`。它刚写完这些
         论断，只有它知道哪几处是硬凑的——这是代码拿不到的信息；
      2. 一支写得够多（≥ `SUPPLEMENTARY_MIN_NODES` 个要点）却一条引用都没有。
         这是"展开成功、内容全是常识补的"的沉默信号，也是最容易变成
         "看起来完整、其实没有出处"的那一类子树。

    只判一次：补查的结果不再回判，这就是"每支最多补 1 次"的实现。
    """
    if isinstance(raw, dict) and raw.get("needs_evidence") is True:
        return True
    if len(slots) < SUPPLEMENTARY_MIN_NODES:
        return False
    return _slot_ref_count(slots) == 0


@dataclass(frozen=True)
class _LibrarySupplement:
    """生成前那次库检索的产物：过门的父块 + 它们所属的**整份**来源 + 账。

    `hits` 与 `sources` 是两件不同的东西，不能合并：

      - `hits` 是**父块**（450–700 字，`start`/`end`/`quote` 都是原文坐标）。它是
        进 prompt 的东西——把整份 26K 字的开发文档贴进 prompt 会把上下文撑爆；
      - `sources` 是命中的 `source_id` 用 `library.load()` 取回来的**整份原文**，
        带持久 id。它是收尾校验要的东西：`verify_source_refs` 用
        `{source.id: source}` 查原文，只给块文本时模型写的引用会被判
        `SOURCE_TEXT_UNAVAILABLE` 静默剔掉（与 `_verify_sources` 的 docstring
        同一个坑）。

    所以这一节里"给模型看块、给校验器看全文"是刻意的分工，不是重复取数。

    `skip` 为空串表示补上了；其余取值见 `LIBRARY_SKIP_*`。`chunks` / `sources` /
    `seconds` 三个数只给诊断用——"这次用了库"与"这次没用"必须能分辨。
    """

    hits: list[EvidenceHit]
    sources: list[Source]
    skip: str = ""
    seconds: float = 0.0

    @property
    def chars(self) -> int:
        """过门父块的合计字数（**不是**整份来源的字数）。"""
        return sum(hit.end - hit.start for hit in self.hits)


def _library_supplement_diagnostic(
    supplement: _LibrarySupplement, *, cite: bool = True,
) -> Diagnostic | None:
    """库这一步给人看的那一行。补上了报账，没补上说清为什么。

    与 `GRAPH_INGEST_*` 同一条规矩：一个会花掉秒级时间、会改变提示词形状的步骤，
    必须在诊断里留下"做了/没做/为什么"。`_LibrarySupplement` 恒为 None 的两种来路
    （开关关着、库不可用）由调用点各说各的，这里只管真正查过的那些。

    `cite` 只有 `_generate` 那边是 True。`_generate_deep` 把库片段**只给骨架**看
    （一级分支从哪儿来），它的引用路径（按支证据 / 章内证据）一条都没接库——所以
    那边的诊断不能承诺"引用可以照抄"，否则用户会看到一句代码做不到的话。
    """
    if not supplement.skip:
        if cite:
            message = (
                f"这次没有材料，从知识库里检索了 {len(supplement.hits)} 段相关原文"
                f"（{len(supplement.sources)} 份来源 / {supplement.chars} 字 / "
                f"{supplement.seconds:.1f} 秒），已作为独立的一节证据交给模型；"
                "这些片段的出处与本次材料同等对待，引用可以照抄。")
        else:
            message = (
                f"这次没有材料，从知识库里检索了 {len(supplement.hits)} 段相关原文"
                f"（{len(supplement.sources)} 份来源 / {supplement.chars} 字 / "
                f"{supplement.seconds:.1f} 秒），已交给模型用来定一级分支；"
                "这条生成路的引用来自按支检索，所以这一次不会有库片段的引用。")
        return Diagnostic(
            level=DiagnosticLevel.info, code="LIBRARY_SUPPLEMENT", message=message)
    if supplement.skip == LIBRARY_SKIP_ENOUGH_MATERIAL:
        # 正常出口，不报：材料够就不该查库，报出来只是噪声。
        return None
    if supplement.skip == LIBRARY_SKIP_NO_RERANK:
        return Diagnostic(
            level=DiagnosticLevel.warn, code="LIBRARY_SUPPLEMENT_SKIPPED",
            message=(
                "这次没用知识库：库命中的相关性只能靠重排分数判断，而重排这次没有生效"
                "（`MINDMAP_RERANK` 关着，或 reranker 调用失败）。没有这道门时实测一半"
                "以上的命中是跑题块（「城市雨污分流改造」8 条里 6 条来自开发文档），"
                "所以宁可不补。"),
        )
    if supplement.skip == LIBRARY_SKIP_FEW_HITS:
        return Diagnostic(
            level=DiagnosticLevel.info, code="LIBRARY_SUPPLEMENT_SKIPPED",
            message=(
                f"这次没用知识库：检索到了片段，但过相关性门的只有 {len(supplement.hits)} "
                f"段（至少要 {LIBRARY_MIN_HITS} 段）。一条孤证撑不起一节证据。"),
        )
    if supplement.skip == LIBRARY_SKIP_NO_HIT:
        return Diagnostic(
            level=DiagnosticLevel.info, code="LIBRARY_SUPPLEMENT_SKIPPED",
            message="这次没用知识库：库里没有与这个主题相关的片段（检索返回空）。",
        )
    if supplement.skip == LIBRARY_SKIP_ERROR:
        return Diagnostic(
            level=DiagnosticLevel.warn, code="LIBRARY_SUPPLEMENT_SKIPPED",
            message=("这次没用知识库：库检索出错（详见日志），已按没有库证据继续生成。"),
        )
    return Diagnostic(
        level=DiagnosticLevel.info, code="LIBRARY_SUPPLEMENT_SKIPPED",
        message="这次没用知识库：库里没有可检索的材料。",
    )


async def _library_supplement(
    topic: str, ctx: RunContext, *, existing_chars: int,
) -> _LibrarySupplement:
    """生成前查一次库，返回**过门的父块**与它们所属的整份来源。

    四道闸，每一道都是"少查一次"而不是"查了再丢"：

      1. 开关（`MINDMAP_LIBRARY_IN_GENERATE`，默认关）——关着时这个函数只做一次
         布尔判断，零查询、零网络；
      2. 已有材料够（`existing_chars >= LIBRARY_SUPPLEMENT_CHARS`）——判据与依据见
         `LIBRARY_SUPPLEMENT_CHARS` 那段注释；
      3. 库可用（`library_available()`，它自己会剔掉外部检索那批，见那里的 docstring）；
      4. 相关性门（`rerank_score >= LIBRARY_RELEVANCE_GATE`），重排不可用时按失败处理
         ——理由与三档实测见 `LIBRARY_RELEVANCE_GATE` 上面的注释。

    检索本身放在线程池里：`search_parent` 是同步函数，里面有一次 embedding 往返
    （0.9–1.4 秒），直接在事件循环里跑会把这期间的所有并发堵死。

    **命中区间记进 `ctx.evidence`、来源记进 `ctx.library_sources`**：模型照这一节
    抄的引用必须在收尾校验时查得到原文（`_verify_sources`），也必须参与
    `SOURCE_REF_OUTSIDE_EVIDENCE` 那道"引的这句在命中的那段里吗"。两件都在这里做，
    调用点不用管。
    """
    if not library_in_generate_enabled():
        # 调用点（`_intake`）已经判过开关，这里再判一次是给"直接调用本函数"留的
        # 保险：开关关着时返回一个 `skip` 非空的对象，调用方不会误读成"查过但没命中"。
        return _LibrarySupplement([], [], LIBRARY_SKIP_NO_LIBRARY)
    if existing_chars >= LIBRARY_SUPPLEMENT_CHARS:
        return _LibrarySupplement([], [], LIBRARY_SKIP_ENOUGH_MATERIAL)
    if not library_available():
        return _LibrarySupplement([], [], LIBRARY_SKIP_NO_LIBRARY)

    query = (topic or "").strip()
    if not query:
        return _LibrarySupplement([], [], LIBRARY_SKIP_NO_HIT)

    started = time.perf_counter()
    try:
        hits = await asyncio.to_thread(
            search_parent, [], query, LIBRARY_GENERATE_TOP_K,
            library=True, drop_external=library_drop_external())
    except Exception as exc:  # noqa: BLE001 — 库是加分项，坏了不该废掉整次生成
        logger.warning("生成前的库检索失败（%s）：%s",
                       type(exc).__name__, str(exc)[:160])
        return _LibrarySupplement([], [], LIBRARY_SKIP_ERROR,
                                  time.perf_counter() - started)
    seconds = time.perf_counter() - started

    if not hits:
        return _LibrarySupplement([], [], LIBRARY_SKIP_NO_HIT, seconds)
    if all(hit.rerank_score is None for hit in hits):
        # 一条都没有重排分 = 重排这一步没跑（开关关着 / 调用失败降级）。门没有判据，
        # 按失败处理，理由见 `LIBRARY_RELEVANCE_GATE` 上面那段实测。
        return _LibrarySupplement([], [], LIBRARY_SKIP_NO_RERANK, seconds)

    kept = [hit for hit in hits
            if (hit.rerank_score or 0.0) >= LIBRARY_RELEVANCE_GATE]
    if len(kept) < LIBRARY_MIN_HITS:
        return _LibrarySupplement(kept, [], LIBRARY_SKIP_FEW_HITS, seconds)

    # 取回整份原文（持久 id）。`_remember_library_hits` 就是 `_rag` 用的那一份实现：
    # 查不到（被 prune 删了）只跳过那一份并记 warning，不让整次生成失败。它把结果
    # 记进 `ctx.library_sources`，于是 `_verify_sources` 收尾时认这些来源。
    before = set(ctx.library_sources)
    _remember_library_hits(kept, ctx)
    sources = [source for source_id, source in ctx.library_sources.items()
               if source_id not in before]
    if not sources:
        # 命中的来源一份都取不回来（库被换掉 / 被清空）：保留 `hits` 只为让诊断说出
        # 真实条数，但**不参与生成**——没有整份原文，模型的引用一定过不了校验。
        return _LibrarySupplement(kept, [], LIBRARY_SKIP_NO_LIBRARY, seconds)

    ctx.record_evidence([
        EvidenceSpan(hit.source_id, hit.start, hit.end, hit.leaf_start, hit.leaf_end)
        for hit in kept
    ])
    return _LibrarySupplement(kept, sources, "", seconds)


@dataclass(frozen=True)
class _Intake:
    """生成前那三段材料处理的产物。两个生成工具共用同一份实现。

    `sources` 是**这次真正可以检索的来源**：空正文的已滤掉、太短的已被换成
    `short_material`、没有材料时由外部检索补上。

    `hint_block` 与 `short_material` 是同一件事的两个面向：太短的材料不当语料
    （它切不出可区分的检索单元，只会产生落不到段落的假依据），但**用户写的东西
    一个字都不丢**——它以"主题补充说明"的身份进 prompt，并明确要求不要从它里面
    挑引用。

    `external` 为 None 表示这次没走外部检索（有材料，或开关关着）。它同时决定
    `data` 里有没有那个键——开关关着时的键集必须与加这个能力之前逐字节相同。

    `full_text` 是"这次走全塞"：材料正文合计不超过 `FULL_TEXT_MATERIAL_CHARS`。
    它在这里只**算一个信号**，不改变任何既有分支（`sources` 仍是"可检索的来源"）：
    改它会让短材料退化、外部检索补位、按支检索三处的语义同时漂移。谁消费这个信号
    （目前只有 `_generate`）谁负责把正文拼进 prompt。

    `full_text_chars` 与 `material_chars` **必须是两个数**：后者只统计用户随请求带来的
    那批（`picked`），而前者是 `sources` 的合计——外部检索补进来的几份也在里面。
    用 `material_chars` 判闸会漏掉整条外部检索路：那条路上 `material_chars` 恒为 0
    （用户没给材料），于是"0 字，装得下"会让每一次外部检索结果都被整份塞进 prompt。

    `ingested` 是"这一步顺手把检索材料抽进图"的那笔账（见 `_ingest_external_graph`），
    None 表示没做（开关关着 / 没有外部材料 / 图不可用）。它**只用来上报**：不改
    `sources`、不改任何既有分支——这一步的产物在图里，不在这个 dataclass 里。

    `graph_ingest_skip` 是"没做"的**原因**（`GRAPH_SKIP_*`，`None` 时为空元组）。
    它与 `ingested` 互斥、不能合并：`ingested is None` 有四种来路，而"没做"这件事
    对用户有四句不同的话要说（没配 / 连不上 / 冷却 / 有目录）。原来这四种全部沉默，
    用户只能看到"图里没有这批材料"这个结果，看不到任何决定。

    `library` 是"生成前主动查库"的产物（`MINDMAP_LIBRARY_IN_GENERATE`，默认关）。
    它**不并进 `sources`**，这是一条硬边界：

      - `sources` 是"按支检索的语料"。库命中的整份来源动辄 26K 字（`AGENT_PLAN.md`），
        并进去之后每一支都会在它上面切块，20 支 × 无关块 = 噪声重新进来，而噪声是
        这个功能唯一要防的东西；
      - 它也不进 `full_text_chars`：那个数的语义是"真的会被整份拼进 prompt 的字数"，
        库来源从来不会被整份拼进去（进 prompt 的是命中的父块）。

    所以库证据走**单独一节**（`_library_evidence_section`），由 `_generate` 拼到
    `sections` 末尾。开关关着时它是 None，`sources` / `full_text` / `full_text_chars`
    与加这个能力之前逐字段相同。
    """

    sources: list[Source]
    short_material: list[Source]
    material_chars: int
    hint_block: str
    external: _ExternalFetch | None
    full_text: bool = False
    full_text_chars: int = 0
    ingested: dict[str, Any] | None = None
    graph_ingest_skip: tuple[str, ...] = ()
    library: _LibrarySupplement | None = None


async def _intake(topic: str, ctx: RunContext) -> _Intake:
    """材料入口三段：只喂有正文的来源、短材料退化、没有材料时先做外部检索。

    这三段原来长在 `_generate` 里，整树生成删掉之后搬进 `_generate_deep`；现在
    两个生成工具都在，再搬一次不如抽成一份——两处各写一遍的话，"材料太短"的
    阈值与"外部检索什么时候发"迟早会有一个分支先改。

    空正文的来源不参与（`status` 是给界面看的，能检索的只有 `text`）；不滤的话，
    一个还没解析成功的空来源会让检索与 `source_usage` 各自撞上一次空正文。

    **不改 `ctx.request.sources`**：材料是用户给的，界面上还要显示它、它还在收尾
    校验的可查来源里（`_verify_sources`）。被改掉的只是"这次喂给模型什么"。

    外部检索排在额度预探**之后**（调用方先判额度再调这里）：额度已经用光时，
    白等一次 6 秒的外网往返没有意义。

    **入图排在外部检索之后、`full_text` 判定之前**（`_ingest_external_graph`）：它只
    处理检索回来的那批（用户自己上传的材料不归这一步管——那批已经随请求带着走完整条
    链），产物在图里而不在这里，所以对下面三行**一个字都不改**。默认关着，关着时
    这一行是一次函数调用、零查询、零网络（理由见那个函数的注释）。

    **全塞的判定放在最后一行**：材料总量不超过 `FULL_TEXT_MATERIAL_CHARS` 时才为真。
    它必须排在短材料那道闸之后（太短的材料先被换成 `hint_block`，`sources` 已经是
    空的），也必须排在外部检索之后（外部补进来的那几份同样可以整份塞进去）。
    阈值取 0 或负数时这个信号恒为 False，这条路上的一切与加它之前逐字节相同。

    **库检索排在外部检索之后、`full_text` 判定之前**（`_library_supplement`，默认关）：
    顺序就是三源优先级（用户材料 → 库 → 外部），而 `full_text` 必须在它之前算定
    ——库命中不改 `sources`、也不改 `full_text_chars`（理由见 `_Intake` 的
    docstring），所以这一步对那两行**一个字节都不改**。开关关着时它是一次布尔
    判断、零查询、零网络。
    """
    picked = [source for source in _pick_sources(ctx, []) if (source.text or "").strip()]
    material_chars = sum(len((source.text or "").strip()) for source in picked)
    short_material = picked if 0 < material_chars < SHORT_MATERIAL_CHARS else []
    sources = [] if short_material else picked
    hint_block = ""
    if short_material:
        hint_block = (
            "\n\n用户还附了这段文字（{chars} 字，太短，不足以作为可检索的材料，"
            "只当作主题的补充说明，不要从它里面挑句子当引用）：\n{hint}"
        ).format(chars=material_chars,
                 hint=" ".join((source.text or "").strip() for source in short_material))
    external: _ExternalFetch | None = None
    if not sources and external_knowledge_enabled():
        # 检索期间界面上什么都不动，看起来像卡死（实测中位 4.3 秒、最坏 20 秒）。
        # 一帧"开始"、一帧"结束"，结束帧**一定带 count**（包括 0）：前端靠
        # "有没有这个字段"区分开始与结束，`0` 与"还没结束"必须分得开。
        await _emit_stage(ctx, EXTERNAL_SEARCH_STAGE, "正在检索外部资料…")
        external = await _external_sources(_external_query(topic, short_material), ctx)
        await _emit_stage(
            ctx, EXTERNAL_SEARCH_STAGE, _external_stage_text(external),
            count=len(external.sources),
        )
        sources = external.sources
    # 只入"这次要用的"：`external` 为 None 说明这次没走检索（有材料或开关关着），
    # 那批材料与图这一档无关，一个字节都不该花在它上面。
    ingest_skip: list[str] = []
    ingested = (await _ingest_external_graph(sources, skip_sink=ingest_skip)
                if external is not None else None)
    full_text_chars = sum(len((source.text or "").strip()) for source in sources)
    full_text = bool(sources) and 0 < full_text_chars <= FULL_TEXT_MATERIAL_CHARS
    # 判"够不够"用 `full_text_chars`（用户材料 + 外部检索合计）而不是
    # `material_chars`：三源优先级里库是**第二**顺位，用户没给料但外部已经补上
    # 3000 字以上时，库再补一批不同主题的片段只会把两份互不相干的材料混进同一棵树。
    library = await _library_supplement(
        topic, ctx, existing_chars=full_text_chars) if library_in_generate_enabled() else None
    out = _Intake(sources, short_material, material_chars, hint_block, external,
                  full_text, full_text_chars, ingested, tuple(ingest_skip), library)
    # "这一次到底吃了几份材料"是好几条分支的判据（骨架条数、全塞、按章切），而它
    # 在日志里从来没有过一行：出问题时只能靠"界面上看到几份"与 SSE 里 external_search
    # 的 count 去推——那两处一个是请求给的、一个是检索报的，都不是**这一步的产物**。
    # 级别取 warning 不是手滑：本仓库的 app logger 没有配置 handler，root 默认
    # WARNING，info 一律看不见（`检索材料入图` 那些 info 行在 backend.log 里一条都没有）。
    if library is None:
        # 开关关着时这一行与加这个能力之前**逐字相同**：那一条"逐字节相同"的约定
        # 连日志也包括，否则"关掉之后就与旧版本一致"这句话在排查时对不上。
        logger.warning(
            "材料入口：%d 份 / %d 字（外部检索 %s，入图 %s）｜%s",
            len(out.sources), full_text_chars,
            "未走" if external is None else f"补 {len(external.sources)} 份",
            "未做" if ingested is None else "已做",
            "、".join((source.filename or source.id)[:28] for source in out.sources) or "（无）",
        )
    else:
        logger.warning(
            "材料入口：%d 份 / %d 字（外部检索 %s，入图 %s，库 %s）｜%s",
            len(out.sources), full_text_chars,
            "未走" if external is None else f"补 {len(external.sources)} 份",
            "未做" if ingested is None else "已做",
            "未补" if library.skip else f"补 {len(library.hits)} 段",
            "、".join((source.filename or source.id)[:28] for source in out.sources)
            or "（无）",
        )
    return out


# ---------------------------------------------------------------- 输入：一段话 → 意图 + 主题
#
# **这一步解决的是"输入的字面"与"它要表达的东西"分不开**，与本模块另外两条同一族：
# `INVENTORY_RULES` 的「材料的份数不是分支数」、`NO_BRANCH_CAP_RULE` 的「不要为了
# 整齐把不同的事合成一支」。用户输入的是**一段话**，而提示词里 `主题：{topic}` 那一行
# 要的是一个**中心词**——这两者之间的翻译从来没有人做过。
#
# 实测（`scripts/measure_input_extract.py`，五条真实长输入、走完整 agent 链路，所以记的
# 是**模型填出来的** `args.topic`）：外层 agent 确实会压缩（33 字 → 23 字），但它压掉的
# 是不该压的东西——
#
#   ①「我想了解怎么在制造业团队里推行AI工具，公司300人，担心员工抵触」
#      → topic "制造业300人团队推行AI工具：路径与抵触化解"（23 字，带冒号从句）
#      → 11 条一级分支、123 个节点（可读性参考值是 80）
#   ③「下周要给老板讲清楚为什么我们的检索效果差，要有数据支撑，别太技术」
#      → topic "为什么我们的检索效果差：给老板的数据化说明"（21 字）
#      → 5 条一级分支是「现状 / 根因 / 指标 / 对比 / 改进」——一个通用分析框架，
#        而"给老板讲清楚"这件事在树上一处都没有体现，"别太技术"被删掉了
#
# 所以缺的不是"再压一次长度"，是**把压缩过程中丢掉的三样东西捞出来**：他要拿这张图做
# 什么（intent）、他真正问的是哪几件事（questions）、他给了什么限定（constraints）。
#
# ## 谁消费它（"意图必须真的影响什么"）
#
#   - `topic` 换掉 `主题：` 那一行**与** `_intake` 的检索 query（同一份文本，两处都有用）；
#   - `intent` / `questions` / `constraints` 拼成 user 里的**一小段**（`_intent_block`），
#     进骨架与结构这两次调用。
#
# **`args.branches` 一个字都不动**：整树这条路上它不进提示词（`NO_BRANCH_CAP_RULE` 与
# `docs/BRANCH-CAP.md` 那件事），所以"questions 有几条 → 建议几条一级分支"这件事没有
# 落点；而且实测那条路的方向是反的——真正的问题不是分支太少（②三件事 → 6 支），是
# ①一条诉求 → 11 支 123 个节点。给一个下界解决不了上界那边的问题。
#
# ## 为什么默认关
#
# 它多**一次模型调用**（`INPUT_EXTRACT_MAX_TOKENS` 的输出很小，但输入是用户原话）。
# 这几天所有改变提示词形状的能力都是这个默认值（`MINDMAP_SECTION_SLICE` /
# `MINDMAP_GRAPH_OUTLINE` / `MINDMAP_LIBRARY_IN_GENERATE`），关着时这一整段只有一次
# 字符串长度判断，`data` 的键集与改动之前逐字节相同。
INPUT_EXTRACT_ENV = "MINDMAP_INPUT_EXTRACT"

# 「这段话已经不是一个主题」的阈值。守的不是"用户写了多少字"，而是"进提示词的那个
# 字符串有多像一个主题"：五条基线里两个真主题是 9 / 11 字，三条"一段话"是 19 / 21 / 23
# 字，20 落在两簇中间（两臂各留 1 字余量）。
INPUT_EXTRACT_MIN_CHARS = 20

# 提取出来的主题上限。真主题的实测上界是 23 字（①那条），24 是那一位的余量；超了按这个
# 数截断而不是判失败——它是画布上的一个标题，不是正文。
INPUT_EXTRACT_MAX_TOPIC_CHARS = 24

# 这一次提取的输出上限。四个字段合计 150–250 字，800 token（约 500 字）是余量。
#
# 上一版是 400，实测**贴边**：③ 那条原话一次跑出 `GenerationError`（返回内容不是合规
# JSON）——四个字段稍写长一点就撞上限，被截断的 JSON 在 `parse_model_json` 那里就是
# "不合规"。输出上限只影响"最多能写多少"，不动平时那 200 字的成本，所以放宽是免费
# 买到的可靠性。刻意**不**跟 `_max_tokens(ctx)` 走：那是写整棵树的额度，给这一步 8192
# 它就开始解释自己了。
INPUT_EXTRACT_MAX_TOKENS = 800

# 「短但碎」的两个形状判据。④那条（`"AI学习 怎么学 路线"`，11 字）是纯长度判据必然
# 漏掉的反例：它短，但一个"怎么"加一个冒号已经说明它不是名词短语。
#
# 括号也算子句标记，来历是一次实测漏网：③那条原话（32 字）被外层 agent 压成了
# 「检索效果差的原因分析（面向老板汇报）」（**18 字**，两次运行分别落在 18 / 21 字
# 两侧）。纯长度判据在这一次上会放行——而"面向老板汇报"正是最该被捞出来的那件事
# （它决定口径与深浅）。括号里的东西天然是"用途/口径的注释"，不是主题的一部分。
_CLAUSE_MARK = re.compile(r"[，,；;：:、？?！!（）()]")
_ASKED_MARK = re.compile(
    r"帮我|帮忙|帮我我们|我想|我想要|我们想|希望|需要|麻烦|请|汇报|讲解|"
    r"怎么|如何|为什么|为何|哪些|是否|能不能|要不要|该不该|值不值得|有没有")

# ---------------------------------------------------------------- 自检：线索有没有被接住
#
# 上一次的 A/B 里 ③ 两次提取都**没有**把"别太技术"收进 `constraints`，当时记的归因是
# "模型判定它不是硬条件"。一验就翻：那是**看错了输入**，不是判断失误——
#
#     提取看**原话**（`ctx.request.goal`）：constraints = ["下周汇报", "面向老板", "别太技术"]
#     提取看**压缩后的 topic**：            constraints = ["面向老板汇报"]
#
# 工具层的 `args.topic` 是外层 agent 压过一遍的（32 字 → 18 字），"别太技术"在这一步
# 就已经不在输入里了。所以这一轮的主修是**输入源**（`_extract_source`：原话优先），
# 自检只是第二道——
#
# **自检是纯规则，不新增调用**：提取那一次的输入与输出都不变，只在拿到结果之后拿原话
# 对一遍四类线索。漏的那些**按原文片段追加**进 `constraints`，不改写：那一类词的价值
# 恰恰在"原样保留"（"别太技术"改写成"通俗表达"就已经不是他要的了）。
INPUT_EXTRACT_SELFCHECK_ENV = "MINDMAP_INPUT_SELFCHECK"

# 原话进提取调用的长度上限。2,000 字 ≈ 1.3k token。
#
# 现在**撞不到**：`AgentRunRequest.goal` 的契约上限是 1,000 字、`GenerateArgs.topic` 也是
# 1,000，两个来源都到不了这里。留着它是给"哪天把上限放宽"的防御——但这个数一旦变成了
# 实际生效的截断，就意味着一整段话的后半截（而约束常常写在末尾："…另外，别太技术"）
# 会在提取之前被切掉，那与这一步的目的正相反。放宽那两层之前先回来看这一段。
INPUT_EXTRACT_MAX_SOURCE_CHARS = 2_000

# 自检最多追加几条：多了就是把原话抄进 constraints，反而稀释真正的那两三条。
_SELFCHECK_MAX_ADDED = 3

# 四类线索，判据写在正则里（每一条都能从表上读出会怎么判错）：
#
#   amount   数字 + 量词：300 人 / 两周 / 三个 / 十万。**必须带量词**：光秃秃的数字
#            在主题里通常是型号（RAG 2.0 / GPT 4），补进 constraints 只添噪声；
#   audience 受众/场景：给老板 / 向客户 / 新员工 / 内部。这一半是"讲给谁听"，
#            它决定口径与深浅，是提取最该捞的那一类；
#   tone     口吻/约束：别太技术 / 要有数据支撑 / 尽量简单 / 量化。原话里几乎全是
#            "别/要/尽量"开头的祈使句，而模型最爱把它们算成"不是硬条件"；
#   deadline 时间压力：下周 / 明天 / 这个月。
_SELFCHECK_CUES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("amount", re.compile(r"\d+\s*(?:人|个|万|千|百|条|天|周|月|年|分钟|小时|次|倍|%|％)")),
    ("audience", re.compile(
        r"(?:给|向|面向|讲给|说给|对)[^，,。；;：:\s]{0,6}(?:看|听|讲|汇报|说明|解释)"
        r"|老板|领导|客户|员工|新人|同事|高管|董事会|面试官")),
    ("tone", re.compile(
        r"别太[^\s，,。；;]{0,6}|不要太[^\s，,。；;]{0,6}|不要写[^\s，,。；;]{0,6}"
        r"|不要用[^\s，,。；;]{0,6}|要有[^\s，,。；;]{0,6}|尽量[^\s，,。；;]{0,6}"
        r"|务必[^\s，,。；;]{0,6}|最好[^\s，,。；;]{0,6}|通俗|深入浅出|量化|举例")),
    ("deadline", re.compile(r"下周|本周|明天|后天|这个月|月底|季度|三天|一周|两周|马上|尽快|下班前")),
)

# 子句边界：摘片段时的左右界（与 `_CLAUSE_MARK` 分开写，因为这里要的是"切开的边界"，
# 不含问号叹号——它们通常是子句的结尾而不是分隔）。
_SNIPPET_EDGE = "，,。；;：:\n！!？?"


def input_selfcheck_enabled() -> bool:
    """自检开关（`MINDMAP_INPUT_SELFCHECK`，默认**开**，只在提取真的跑过时生效）。

    默认开的理由与其它开关相反，但同一条标准——**它有没有成本**：这一步是一次正则扫描
    （原话 ≤2,000 字、四条正则），零模型调用、零网络。主开关 `MINDMAP_INPUT_EXTRACT`
    默认关，所以"开着"并不意味着一上线就生效。写成 `=0` 可单臂关掉，A/B 要的就是这一臂。
    """
    return os.getenv(INPUT_EXTRACT_SELFCHECK_ENV, "1").strip().lower() not in {
        "0", "off", "false", "no"}


def _cue_snippet(match: re.Match[str], limit: int = 12) -> str:
    """把一处线索摘成**原文片段**：直接用正则匹配到的那一段，不改写、不补上下文。

    接过一版"以匹配为中心取 16 字窗口"的写法，实测补出来的是
    `"下周要给老板讲清楚为什么我们的检"`——半句、还被截在词中间。而这条线索要说的
    只是"有下周这个时间压力"，`"下周"` 两个字已经说完了。**短而准 > 长而像文章。**
    """
    return match.group().strip(" ，,。；;：:、\n\t")[:limit]


def _overlaps(left: str, right: str, floor: int = 6) -> bool:
    """两段片段是不是"同一句话的两刀"（包含关系，或首/尾共 ≥ `floor` 字）。

    要它是因为同一条线索常被两条正则同时扫到：`"下周要给老板讲清楚…"` 里 `下周`（deadline）
    与 `给老板讲`（audience）摘出来的是几乎一样的一段。两段都补进 `constraints` 就是同一件
    事写两遍——而 `constraints` 总共只放得下五条。
    """
    if left in right or right in left:
        return True
    for size in range(min(floor, len(left), len(right)), 0, -1):
        if left[:size] == right[:size] or left[-size:] == right[-size:]:
            return True
    return False


# audience 那一条的引导/收尾词：判"接住"时要剥掉它。`"给老板讲"` 的核心是 `"老板"`，
# 而 `constraints` 里可能已经躺着 `"面向老板"`——不剥就会把同一个受众补第二遍（实测就
# 这么补出过 `["面向老板", "给老板讲"]` 这种一对同义词）。
_AUDIENCE_LEAD = re.compile(r"^(?:给|向|面向|讲给|说给|对)")
_AUDIENCE_TAIL = re.compile(r"(?:看|听|讲|汇报|说明|解释)$")


def _cue_core(hit: str, kind: str) -> str:
    """线索的核心词。只对 `audience` 剥引导词——`tone` 剥了会出事：
    `"别太技术"` 剥成 `"技术"` 之后，只要主题里出现"技术"就被判成已接住。"""
    if kind != "audience":
        return hit
    core = _AUDIENCE_TAIL.sub("", _AUDIENCE_LEAD.sub("", hit))
    return core.strip(" ，,。；;：:、")


def _selfcheck(text: str, shape: _InputShape) -> _InputShape:
    """拿**原话**对一遍那四类线索，漏的按原文片段追加进 `constraints`。

    判"漏"用的是**字面包含**：线索的匹配文本（或它所在的片段）只要在 topic / intent /
    questions / constraints 任一处出现过就算接住了。这是死板的判据，但死板在这里是对的
    ——自检要做的是"别把找到的东西丢掉"，不是"再判断一次模型判得对不对"（那需要语义，
    而语义那一步已经花过了）。

    不改写、不重排、不调模型：新增的几条一律追加在 `constraints` 末尾，原来的顺序一个
    不动（顺序是模型给的，动了就等于替它重排优先级）。
    """
    if not input_selfcheck_enabled() or shape.source != "model":
        return shape
    known = " ".join((shape.topic, shape.intent, *shape.questions, *shape.constraints))
    added: list[str] = []
    missed: list[str] = []
    cues = caught = 0
    for name, pattern in _SELFCHECK_CUES:
        for match in pattern.finditer(text):
            cues += 1
            hit = match.group().strip()
            core = _cue_core(hit, name) if hit else ""
            if not hit or hit in known or (len(core) >= 2 and core in known):
                caught += 1
                continue
            snippet = _cue_snippet(match)
            if not snippet or snippet in known or snippet in added:
                caught += 1
                continue
            if any(_overlaps(snippet, item) for item in added):
                # 同一段话已经被补过（两条正则抓的是同一句）：算接住，不重复补
                caught += 1
                continue
            missed.append(f"{name}:{hit}")
            if len(added) < _SELFCHECK_MAX_ADDED:
                added.append(snippet)
    report = {
        "cues": cues,
        "caught": caught,
        "added": list(added),
        "missed": missed,
        "skipped": missed[len(added):] if len(missed) > len(added) else [],
    }
    if not added:
        return replace(shape, selfcheck=report)
    return replace(
        shape,
        constraints=(*shape.constraints, *added)[:5],
        selfcheck=report,
    )



def input_extract_enabled() -> bool:
    """输入提取开关（`MINDMAP_INPUT_EXTRACT`，默认**关**）。

    真值写法与 `graph_skeleton_enabled` / `section_slice_enabled` 一致（正列举真值、
    缺省取空串即关）——这四个开关要能用同一行 shell 一起切，口径就必须一样。
    """
    return os.getenv(INPUT_EXTRACT_ENV, "").strip().lower() in GRAPH_SKELETON_TRUTHY


# 「提取没做成」的四种来路 → 给用户的一句话。`short` / `empty` / `off` 刻意**不在**
# 这张表里：那三个是设计（短主题不为它多付一次调用），不是失败。
_INPUT_EXTRACT_FAILURE: dict[str, str] = {
    "budget": "模型调用额度不够，那一次调用没发出去",
    "failed": "那一次调用失败了（模型侧报错）",
    "unshaped": "模型没有返回一个 JSON 对象",
    "no_topic": "模型返回的主题是空的",
}


@dataclass(frozen=True)
class _InputShape:
    """一次「读输入」的结果。**唯一进提示词的字段是 `topic`**，其余三个进
    `_intent_block`（user 里的一小段），另外全部进 `data["input"]`。

    `source` 两态：`raw` = 原样用（开关关着 / 太短 / 额度不够 / 提取失败），
    `model` = 提取到了。它与 `skip` 不重复：`source="raw"` 时 `skip` 说明**为什么**，
    而"提取了但只拿到 topic"这种情况是 `source="model"` + 另外三个字段为空。
    """

    topic: str
    original: str
    intent: str = ""
    questions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    source: str = "raw"
    seconds: float = 0.0
    skip: str = ""
    # 这一次调用的输入 token 估算（`estimate_input_tokens`，与 `chat_async` 的物理边界
    # 同一个函数）。它是**这一步的代价**，必须能与"省下来的东西"摆在一起看：一次提取
    # 约 0.4k token 输入，而它替换掉的是一棵按整句话长的树。
    prompt_tokens: int = 0
    # 这一次提取看的是哪一段文本：`goal`（用户原话，首选）/ `topic`（工具的 topic 参数，
    # 只在没有 goal 或原话过长时用）。分两态上报不是洁癖——实测同一句原话，看 goal 与看
    # 压缩后的 topic 给出的 `constraints` 是 ["下周汇报","面向老板","别太技术"] 与
    # ["面向老板汇报"]，差的那两条正是"提取不全"的全部内容。
    read_from: str = "topic"
    # 自检的账（`_selfcheck`）：四类线索各扫到几处、接住几处、补了哪几条。`None` 表示
    # 自检没跑（开关关着 / 没提取到）。
    selfcheck: dict[str, Any] | None = None


# 提取那一次的 system。四条判据各对应一种实测会跑偏的写法：
#
#   1) topic 写成"这句话的缩写"（①那条的 23 字就是），于是根节点还是从句；
#   2) intent 写成一段解释——它要的是从固定几类里挑一个，因为下游拿它决定口气；
#   3) questions 合成一条（②那条三个问题，模型很爱写成"RAG 召回与选型与成本"）；
#   4) constraints 收进情绪铺垫（"担心员工抵触"不是限制，是要讨论的内容本身）。
INPUT_EXTRACT_PROMPT = (
    "你在把用户的一段话读成一张思维导图**要用到的几件事**。"
    "只返回一个 JSON 对象，不要代码围栏，不要解释。格式：\n"
    '{"topic":"10-20 字的主题","intent":"他要拿这张图做什么",'
    '"questions":["他真正想问的事，一件一条"],"constraints":["他给的条件与限定"]}\n'
    "四条判据：\n"
    "1) topic 是**这张图的中心词**，不是这句话的改写：写成 6-20 字的名词短语，"
    "不要带请求语（帮我 / 我想 / 请）、不要带疑问词（怎么 / 如何 / 为什么），"
    "不要带冒号后面的解释，也不要带「给老板讲」这类用途；\n"
    "2) intent 从这几类里挑一个，最多 8 字：想了解 / 想对比 / 想规划 / 想排查原因 / "
    "想讲解给别人听 / 想做决策 / 想动手做；\n"
    "3) questions 按**他问的事**拆，一件一条，最多 5 条，每条不超过 30 字；"
    "他说了三件事就三条，不要把三件事合成一条，也不要替他加他没问的问题；\n"
    "4) constraints 只收**会影响内容的硬条件**（人群、规模、时间、深浅、口径、"
    "他明确不要的写法），每条不超过 12 字，没有就给空数组；他担心的事、他想达成的事"
    "属于 questions，不是限制。\n"
    "不要把这段话的原句搬进任何字段，也不要评价他问得好不好。"
)


def _too_short_to_extract(topic: str) -> str | None:
    """这一次要不要提取。`None` = 要；否则返回"为什么不"（进 `data["input"]["skip"]`）。

    两个判据是**或**的关系：长度够长，或者形状已经不像一个主题词。写成一串早退而不是
    一个布尔表达式，是因为三个原因要能分开报——"太短"与"空"在排查时不是一件事。
    """
    text = topic.strip()
    if not text:
        return "empty"
    if len(text) >= INPUT_EXTRACT_MIN_CHARS:
        return None
    if _CLAUSE_MARK.search(text) or _ASKED_MARK.search(text):
        return None
    return "short"


def _extract_source(topic: str, ctx: RunContext) -> tuple[str, str]:
    """这一次提取看哪段文本。返回 `(文本, 来源标记)`，标记进 `data["input"]["from"]`。

    取**更长的那一个**（原话 / `topic`），不是无条件取原话：原话通常更长也更保真
    （③ 的"别太技术"只存在于原话里），但 `goal` 也可能比 `topic` 短（用户只打了
    「AI」而模型把它补成了「AI 学习路线」），那时拿原话等于把模型补的信息又丢掉一次。
    信息量大的那个更接近"他到底说了什么"，而这一步的判据只有这一条。

    超过 `INPUT_EXTRACT_MAX_SOURCE_CHARS` 时截断并记 `*_truncated`：那种输入是"用户贴了
    一整篇文章"，材料入口会接手；提取只需要他要什么，前 2,000 字已经够判断。
    """
    goal = (ctx.request.goal or "").strip()
    candidates = [(name, text) for name, text in (("goal", goal), ("topic", topic.strip()))
                  if text]
    if not candidates:
        return topic, "topic"
    read_from, text = max(candidates, key=lambda item: len(item[1]))
    if len(text) > INPUT_EXTRACT_MAX_SOURCE_CHARS:
        return text[:INPUT_EXTRACT_MAX_SOURCE_CHARS], f"{read_from}_truncated"
    return text, read_from


def _extract_ask(topic: str) -> str:
    """提取那一次的 user。原话**一个字都不改**地整段给它：这一步的全部价值就在于看到
    原话，先做一次摘要再把摘要给它，等于把要捞回来的东西再丢一遍。"""
    return f"他的话：\n{topic}"


def _clean_line(value: Any, limit: int) -> str:
    """模型给的短字段：去空白、去首尾引号与标点、截到 `limit` 字。不合法时返回空串。"""
    if not isinstance(value, str):
        return ""
    text = value.strip().strip("\"'“”「」（）() \t\r\n。.，,；;")
    return text[:limit]


def _clean_topic(value: Any) -> str:
    """主题：比 `_clean_line` 多一步"别把冒号从句带回来"。

    模型偶尔会把原话里的从句连冒号一起抄进 topic（`"检索效果差：给老板的数据化说明"`）。
    冒号后面那半句通常正是**用途**（该进 `intent`），所以这里从冒号处截断——它是这一步
    唯一要挡的形状，挡不住的其它歪法交给 `_InputShape.source` 与 A/B 去暴露。
    """
    text = _clean_line(value, INPUT_EXTRACT_MAX_TOPIC_CHARS * 2)
    for mark in ("：", ":"):
        if mark in text:
            text = text.split(mark, 1)[0].strip()
    text = text.strip("\"'“”「」（）() \t\r\n。.，,；;")
    return text[:INPUT_EXTRACT_MAX_TOPIC_CHARS]


def _clean_items(value: Any, *, limit: int, count: int) -> tuple[str, ...]:
    """列表字段：收 list / 单个字符串（模型有时写成 `"a，b"`），去空、去重、截条数。

    去重按**原样**比（不做归一）：`"成本怎么控"` 与 `"成本控制"` 在这一层是两件事，
    合并它们是语义判断，而这里没有做语义判断的能力——多说一件的代价是提示词多一行，
    说少一件的代价是那件事从树上消失。
    """
    if isinstance(value, str):
        raw_items: list[Any] = re.split(r"[，,、；;\n]", value)
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        return ()
    out: list[str] = []
    for item in raw_items:
        text = _clean_line(item, limit)
        if text and text not in out:
            out.append(text)
        if len(out) >= count:
            break
    return tuple(out)


def _shape_from(raw: Any, topic: str, *, seconds: float,
                prompt_tokens: int = 0, read_from: str = "topic") -> _InputShape:
    """把模型那一次的输出收拾成 `_InputShape`。任何一处不合法都退回原 topic。

    退回而不是"照它能给的部分用"：`topic` 是这张图的中心，一个空的或半截的中心比原话
    更糟——原话至少还是用户自己写的。
    """
    elapsed = round(seconds, 3)
    if not isinstance(raw, dict):
        return _InputShape(topic=topic, original=topic, seconds=elapsed, skip="unshaped",
                           prompt_tokens=prompt_tokens, read_from=read_from)
    shaped = _clean_topic(raw.get("topic"))
    if not shaped:
        return _InputShape(topic=topic, original=topic, seconds=elapsed, skip="no_topic",
                           prompt_tokens=prompt_tokens, read_from=read_from)
    return _InputShape(
        topic=shaped,
        original=topic,
        intent=_clean_line(raw.get("intent"), 16),
        questions=_clean_items(raw.get("questions"), limit=40, count=5),
        constraints=_clean_items(raw.get("constraints"), limit=16, count=5),
        source="model",
        seconds=elapsed,
        prompt_tokens=prompt_tokens,
        read_from=read_from,
    )


def _intent_block(shape: _InputShape) -> str:
    """意图那三样在 user 里的落点。空串表示这次没有可加的东西（开关关着 / 没提取到）。

    **为什么不把 constraints 拼进 `主题：` 那一行**：那一行必须短（它的长度直接决定
    根节点 label 的长度，而根节点是画布中央那一个框）。限定写在主题后面，就是把
    "中心词"重新变回"一句话"——正是这一步要修的那个毛病。所以它们分成两块：主题行
    只有一个名词短语，其余三样各占一行，位置紧跟在主题之后（顺序即权重：先定中心，
    再给对齐用的那几行）。

    **为什么不进 system**：这四样是**这一次**的输入，不是树规则。本仓库已经三次记过
    同一条教训（`role=decision`、`_detail_ask` 的自查句、全塞模式下的引用规则）——
    写在 system 里的具体指令会被那段长而稳定的规则稀释，挪到 user 里才生效。
    """
    if shape.source != "model":
        return ""
    lines: list[str] = []
    if shape.intent:
        lines.append(f"· 他要拿这张图做的事：{shape.intent}")
    if shape.questions:
        lines.append("· 他真正想问的：" + "；".join(shape.questions))
    if shape.constraints:
        lines.append("· 他给的条件与限定（写每一支时都要照顾到）：" + "、".join(shape.constraints))
    if not lines:
        return ""
    return (
        "\n\n（下面几行是**从他原话里读出来的**，不是新的主题，也不改变上面那个主题："
        "写完每一支之前对一遍，别写出与它们相悖的内容）\n" + "\n".join(lines)
    )


async def _read_input(topic: str, ctx: RunContext) -> _InputShape:
    """把这次输入读成「意图 + 主题 + 要覆盖的事」。

    **读的是用户原话（`ctx.request.goal`），不是 `topic` 参数**。这一条是实测逼出来的：
    同一句原话（③，32 字），看原话给出的 `constraints` 是
    `["下周汇报", "面向老板", "别太技术"]`，看外层 agent 压缩后的 `topic`
    （"检索效果差的原因分析（面向老板汇报）"，18 字）只有 `["面向老板汇报"]`。
    差的那两条不是模型判错，是**输入里本来就没有**——压缩发生在工具被调用之前。

    位置在 `_generate` 的开头、`_intake` 之前：**它必须比检索更早**，因为 `_intake`
    拿去查外部资料与库的那条 query 就是主题（`_external_query(topic, …)`），主题换了
    那两条 query 跟着换。放在 service 层不行：那会让每一次 agent 循环都多一次调用，
    而绝大多数轮次（改一个节点、删一条边）根本不需要它。

    三条退化都**不失败**，只是退回原样并记下原因：开关关着（`off`）、太短（`short` /
    `empty`）、额度不够（`budget`）、调用或解析失败（`failed` / `unshaped` / `no_topic`）。
    一张图值不值得为"提取没成功"报错？不值得——原话当主题是这一步之前的行为，它是
    已知可用的，只是不够好。

    判据（`_too_short_to_extract`）仍然只看 `topic`：它是"外层 agent 把这段输入压成了
    什么"，是判"要不要多花一次调用"最省事的信号；真要看的是原话，那是**输入源**，两件
    事分开。
    """
    if not input_extract_enabled():
        return _InputShape(topic=topic, original=topic, skip="off")
    reason = _too_short_to_extract(topic)
    if reason is not None:
        return _InputShape(topic=topic, original=topic, skip=reason)
    # 额度在调用之前占，与 `_plan_ask` 那一次同一个约定：请求一旦发出就是一次真实计费。
    if not ctx.reserve_model_call():
        return _InputShape(topic=topic, original=topic, skip="budget")
    ctx.extra_model_calls += 1
    source, read_from = _extract_source(topic, ctx)
    started = time.perf_counter()
    # 输入 token 与成败无关（它由 system + 这一次的文本决定），所以先算出来：失败那次
    # 也照样花了这些 token，账不能只在成功时记。
    prompt_tokens = estimate_input_tokens(INPUT_EXTRACT_PROMPT + _extract_ask(source))
    try:
        raw = await _chat(
            ctx, INPUT_EXTRACT_PROMPT, _extract_ask(source),
            max_tokens=INPUT_EXTRACT_MAX_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001 — 提取失败就是降级，理由见 docstring 末段
        logger.warning(
            "输入提取失败（%s: %s），这一次把原话当主题",
            type(exc).__name__, _reason_text(exc))
        return _InputShape(
            topic=topic, original=topic, read_from=read_from,
            seconds=round(time.perf_counter() - started, 3), skip="failed",
            prompt_tokens=prompt_tokens)
    shape = _shape_from(raw, topic, seconds=time.perf_counter() - started,
                        prompt_tokens=prompt_tokens, read_from=read_from)
    # 自检：拿**原话**对一遍（`source`，不是 topic）。纯规则、不新增调用。
    return _selfcheck(source, shape)


def _apply_skeleton(
    content: MindMapContent, plan: _SkeletonPlan, raw_plan: Any,
    *, diagram_type: str,
) -> tuple[MindMapContent, list[Diagnostic]]:
    """把**骨架那一次**的产物（颜色 / 图标 / flowchart 的附加边）套到生成出来的树上。

    两件产物都来自骨架响应，而树来自生成响应，两者唯一的共同坐标是"第几支"。
    匹配按 label 优先、序号兜底：名字完全一致时按名字更安全（模型可能真的换了
    顺序），名字被改写了一个字（加了"的"、少了括号）时序号还能把它对回去。

    为什么必须做这一步：骨架每次调用都会给出（或由 `_fill_branch_colors` 补出）
    一级分支的颜色，而生成响应里不含这个字段。不套的话，"一级分支一个颜色、
    后代继承"这个唯一的分组信号整张图都没有——用户看到一片中性灰，看不出哪几块
    是同一支。边同理：流程图的条件分支与回退线全在骨架响应里。

    **两级时套的是分组**（`plan.groups`），不是那二十节：分组才是画布上的一级节点，
    二级分支不写 color/icon，它们的颜色由前端沿父链继承（`frontend/src/stores/
    mindmap.ts` 里 `inheritedColor`）。这也是"一级颜色 = 分组"这条约定在两级下
    仍然成立的全部原因——给二级也套色，一组四支会画出四种颜色。

    端点用**裁剪后**的真实 id 建表：`_skeleton_edges` 认得序号 / "root" / 标题，
    而 `clean_edges` 要把指向已裁剪节点的边整条丢掉——拿裁剪前的表建就丢不掉。
    """
    children = list(content.root.children)
    branches = plan.groups if plan.grouped else plan.flat
    by_label = {branch.label: branch for branch in branches}
    applied: list[Node] = []
    for index, child in enumerate(children):
        branch = by_label.get(child.label) or (
            branches[index] if index < len(branches) else None)
        if branch is None or (branch.color is None and branch.icon is None):
            applied.append(child)
            continue
        # 模型自己写的 color/icon 优先：骨架是"该长什么样"的建议，而生成响应里
        # 若它显式给了一个，那个才是它对这一支的最终判断。
        applied.append(child.model_copy(update={
            "color": child.color or branch.color,
            "icon": child.icon or branch.icon,
        }))
    if applied != children:
        content = content.model_copy(
            update={"root": content.root.model_copy(update={"children": applied})})
    # 两级只出现在思维导图上（`_generate` 的 `grouped` 判定里排除了 flowchart /
    # fishbone）：边连的是阶段，分组与阶段顺序无关。这里再判一次，是因为"哪天
    # 有人给流程图开了分组"不该变成一堆指向分组节点的边。
    if diagram_type != "flowchart" or not applied or plan.grouped:
        return content, []
    skeleton = [(child.label, child.id) for child in applied]
    skeleton_ids = {content.root.id} | {child.id for child in applied}
    edges, edge_diagnostics = clean_edges(
        _skeleton_edges(raw_plan, skeleton, content.root.id), skeleton_ids)
    if edges:
        content = content.model_copy(update={"edges": edges})
    return content, edge_diagnostics


def _apply_groups(
    content: MindMapContent, plan: _SkeletonPlan,
) -> tuple[MindMapContent, int, list[str]]:
    """按骨架定好的两级把树摆好：模型写对时它什么都不改，写歪时它只搬位置。

    为什么要有这一步（以及为什么它**不是**"生成后归并"）：分成哪几组是**骨架那一次**
    调用定的——模型在那里看到了全部二十节，它比任何事后聚类都更会判断哪些该归一起。
    这里只执行那个决定：把分支摆到对应分组下面、缺的分组节点补出来。它不发任何
    调用，也不改任何节点的 label / detail / 引用（搬的是整棵子树）。

    为什么不能只靠提示词：`GROUPED_TREE_RULES` 说了"根下必须是那几个分组"，但模型
    偶尔仍按老习惯写成二十条并列（老响应、长输出尾部走样、或者它压根没读那段）。
    照原样落树，用户看到的正是这次改动要消灭的东西——一条二十支宽的一级。所以形状
    在这里兜底：**提示词负责让它写对，这一步负责让"没写对"不致命**。

    三条排布规则：
      - 认得出的分支按骨架顺序排进它的分组（同一支在两个组下出现时只认第一次）；
      - 分组里认不出的节点留在**那个分组**末尾，不下沉也不上提——把模型写的内容
        放到别处比留在原处更糟（它可能只是改了一个字）；
      - 根上认不出的节点原样留在根的最后：它们是模型自己加的东西，删掉是丢内容。

    返回 `(树, 被搬动/新建的节点数, 分组节点 id)`。第三个数给第三段用：分组是分类名，
    不该为它写一段说明（见 `_generate` 传 `skip_ids` 那一行）。
    """
    if not plan.grouped:
        return content, 0, []
    taken = _node_ids(content.root)
    group_index = {_skeleton_key(g.label): i for i, g in enumerate(plan.groups)}
    branch_index: dict[str, int] = {}
    for index, group in enumerate(plan.groups):
        for branch in group.branches:
            branch_index.setdefault(_skeleton_key(branch.label), index)

    wrappers: dict[int, Node] = {}
    found: dict[str, Node] = {}
    kept_inside: dict[int, list[Node]] = {index: [] for index in range(len(plan.groups))}
    loose: list[Node] = []
    moved = 0
    for child in content.root.children:
        index = group_index.get(_skeleton_key(child.label))
        if index is not None and index not in wrappers:
            # 模型自己写对了这一层：留下它的 id 与它可能写的 detail，孩子重新排。
            wrappers[index] = child
            continue
        if index is not None:
            # 同名的第二个分组：整枝留在根上（不合并孩子的顺序，那会悄悄改结构）。
            loose.append(child)
            continue
        index = branch_index.get(_skeleton_key(child.label))
        if index is None:
            loose.append(child)
            continue
        found.setdefault(_skeleton_key(child.label), child)
        moved += 1          # 它从根上搬到分组下，位置变了
    for index, wrapper in wrappers.items():
        for grandchild in wrapper.children:
            key = _skeleton_key(grandchild.label)
            target = branch_index.get(key)
            if target == index and key not in found:
                found[key] = grandchild
                continue
            # 认不出的、或属于别的组却挂在这个组下的：留在本组末尾。
            kept_inside[index].append(grandchild)
            if target is not None and target != index:
                moved += 1

    children: list[Node] = []
    group_ids: list[str] = []
    for index, group in enumerate(plan.groups):
        kids = [found[_skeleton_key(branch.label)] for branch in group.branches
                if _skeleton_key(branch.label) in found]
        kids.extend(kept_inside[index])
        if not kids:
            # 一支都没认出来：不挂一个空分组（那是个纯占位的节点）。
            continue
        wrapper = wrappers.get(index)
        if wrapper is None:
            node = Node(id=new_node_id(taken), label=group.label, children=kids)
            taken.add(node.id)
            moved += 1      # 新建的分组节点
        else:
            node = wrapper.model_copy(update={"children": kids})
        children.append(node)
        group_ids.append(node.id)
    if len(children) < 2:
        # 一组都没凑齐（或只剩一组）：两级不成立，原样交回模型写的树。
        return content, 0, []
    children.extend(loose)
    if children == list(content.root.children):
        # 已经是对的：一个节点都不用重建，返回原对象（调用方按 id 判等）。
        return content, 0, group_ids
    return content.model_copy(
        update={"root": content.root.model_copy(update={"children": children})},
    ), moved, group_ids


def _raw_ref_count(raw: Any) -> int:
    """模型**声明**了几条引用（逐字校验之前）。

    与校验之后数一次配对用：只报"最后剩下 0 条"会把"它没写"与"写了但全被剔除"
    压成同一句话，而这两件事对用户的下一步完全不同（前者是提示词没被遵守，
    后者是引文抄错了）。
    """
    stack = [raw.get("root") if isinstance(raw, dict) else None]
    total = 0
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        refs = node.get("source_refs")
        if isinstance(refs, list):
            total += len(refs)
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(children)
    return total


def _fishbone_problem(
    raw_plan: Any, title: str, diagnostics: list[Diagnostic],
) -> str:
    """鱼骨图主骨上那一句：模型给了就用它的，没给就用标题兜底——两种情况都说出来。

    兜底不是"把字段凑满"：`FishboneContent.problem` 是 `min_length=1` 的必填项，
    没有它整张图存不进去；用主题占位至少让用户看见鱼头里写的是什么，同时
    `FISHBONE_PROBLEM_MISSING` 明说那不是模型写的。反过来凭空造一句"为什么会这样"
    等于替它编一个论点，那比缺一句话严重得多。

    **复读标题**单独一条诊断：实测最常见的失败形态就是 `problem` 原样照抄主题
    （甚至在"为什么"三个字前面接上主题）。它不判失败（用户可能真的就想这么命题），
    但必须可见——一张鱼头写着主题的鱼骨图，读图的人看不出这张图在回答什么。
    """
    problem = _skeleton_problem(raw_plan)
    if problem is None:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="FISHBONE_PROBLEM_MISSING",
            message=(
                "模型没有给出主骨上那句话（problem），鱼头这次用主题占位："
                "「" + title + "」。这张图因此少了一个明确的论点——"
                "六根骨列举的是原因的类别，读图的人不知道它们在回答什么问题"
            ),
        ))
        return title
    if _skeleton_key(problem) == _skeleton_key(title):
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="FISHBONE_PROBLEM_ECHOES_TITLE",
            message=(
                f"主骨上的那句话与主题完全相同（「{problem}」）：鱼头通常写的是"
                "「为什么 X 会 Y」这样的疑问句，而不是这张图的名字"
            ),
        ))
    return problem


def _fishbone_shape_diagnostics(
    content: FishboneContent, diagnostics: list[Diagnostic],
) -> None:
    """六类齐不齐——**warn，不判失败**。

    `FISHBONE_CATEGORIES` 是推荐词表而不是契约白名单（见那个常量的注释）：模型写
    "人员"而不是"人"时硬绑的表现是整张图 422，而用户宁可要一张类别名不太标准但
    内容对的鱼骨图。所以这里只数"少了哪些"，缺的几根在画布上确实是空的。

    类别可变开着时（`MINDMAP_FISHBONE_FREE_CATEGORIES`）这条判据**不成立**：那次
    的类别就是按主题定的，"少了人/机/料/法/环/测"不是缺内容，而是设计。那种情况
    下改报一条 info，说清这次有几个类别、它们是什么——用户仍然要知道"这张图没有
    走 5M1E"这件事，只是它不再是缺陷。
    """
    got = {_skeleton_key(bone.category) for bone in content.bones}
    if fishbone_free_categories_enabled():
        if not content.bones:
            return
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.info, code="FISHBONE_CATEGORIES_UNFIXED",
            message=(
                f"这次的一级分支是按主题定的 {len(content.bones)} 个原因维度"
                f"（{'、'.join(bone.category for bone in content.bones)}），"
                f"不是 5M1E 的 {'/'.join(FISHBONE_CATEGORIES)}；"
                "每根骨下面写的是属于那个维度的原因"
            ),
        ))
        return
    missing = [name for name in FISHBONE_CATEGORIES if _skeleton_key(name) not in got]
    if not missing:
        return
    diagnostics.append(Diagnostic(
        level=DiagnosticLevel.warn, code="FISHBONE_CATEGORIES_INCOMPLETE",
        message=(
            f"5M1E 的六类里少了 {'、'.join(missing)}（这次实际是 "
            f"{'、'.join(bone.category for bone in content.bones)}）："
            f"鱼骨图的一级分支固定是 {'/'.join(FISHBONE_CATEGORIES)}，"
            "缺的那几根在画布上是空的，原因的覆盖面因此不完整"
        ),
    ))


# 「这一步的对象是数据本身」的两个角色。收在这里是因为有两处要读同一份口径：
# `_stamp_flow_roles` 挑终点时要躲开它们（`role` 是单值字段，改写会把形状抹掉），
# 而新增第三种数据形状时也必须在这两处一起想到。
_FLOW_DATA_ROLES = frozenset({"data", "database"})


def _stamp_flow_roles(
    content: FlowContent, diagnostics: list[Diagnostic],
) -> FlowContent:
    """给流程图补上**整张图一个都没有**的那两种角色：入口与出口。

    只在"模型一个都没标"时补，不做"补一半"：模型标了 start 却没标 end 时替它挑
    一个终点，等于代码替它决定"流程在哪里结束"——那句话是它对业务的理解，不是
    结构事实。而**一个都没有**时的情形不同：起点就是第一个阶段的第一步、终点就是
    最后那个收尾的叶子，这两件事由 `stages` 的顺序与 `children` 的形状直接决定。

    `decision` 一律不补：哪个步骤需要判断，只有写这张图的模型知道。`data` /
    `database` 同样不补——但终点那一格会**躲开**它们（见下面那段注释）。
    """
    roles = {node.role for node in shapes.iter_nodes(content)}
    stages = list(content.stages)
    if not stages:
        return content

    def rebuild(items: list[FlowStage]) -> FlowContent:
        return FlowContent(
            title=content.title, stages=items, edges=content.edges,
            theme=content.theme, relations=content.relations,
            source_usage=content.source_usage,
        )

    if "start" not in roles and stages[0].root.role is None:
        first = stages[0].root
        stages[0] = stages[0].model_copy(update={
            "root": first.model_copy(update={"role": "start"}),
        })
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.info, code="FLOW_ROLE_START_DEFAULTED",
            message=(
                "模型没有标出流程的起点，已把第一个阶段的第一步"
                f"（「{first.label}」）标成 start"
            ),
        ))

    if "end" not in roles:
        last = stages[-1]
        leaves = [node for node in shapes.walk_nodes(last.root) if not node.children]
        target = leaves[-1] if leaves else last.root
        # 末端的存储步骤（「归档到系统」「写入台账」）很常见，而 `role` 是**单值**
        # 字段：把它改成 end 就等于把模型标的那只圆柱抹掉。所以终点往前挪一步，
        # 两边语义都保住——`end` 是结构事实（流程在哪结束），`data` / `database`
        # 是模型对这一步的理解，后者不该被前者吃掉。
        if target.role in _FLOW_DATA_ROLES:
            plain = [node for node in leaves if node.role not in _FLOW_DATA_ROLES]
            if plain:
                target = plain[-1]
                diagnostics.append(Diagnostic(
                    level=DiagnosticLevel.info, code="FLOW_ROLE_END_MOVED_UP",
                    message=(
                        f"最后一个阶段收尾的那一步标了 {leaves[-1].role}"
                        f"（「{leaves[-1].label}」），形状要留给它；"
                        f"终点因此标在前一步（「{target.label}」）上"
                    ),
                ))
            else:
                # 整个阶段都是数据步骤（一个"收尾但不碰数据"的节点都没有）：
                # 终点比形状重要（流程图必须有出口），但这一处取舍要说出来，
                # 否则用户只会看到"我标的圆柱少了一个"，而没有任何线索。
                diagnostics.append(Diagnostic(
                    level=DiagnosticLevel.info, code="FLOW_ROLE_END_OVERRIDES_DATA",
                    message=(
                        f"最后一个阶段的每一步都在跟数据打交道，终点只能落在最后一步"
                        f"（「{target.label}」）上，原来标的 {target.role} 让位给 end"
                    ),
                ))
        if target.role == "start":
            # 单阶段单步：start 与 end 会落在同一个节点上。起点优先——那个节点
            # 同时是入口与出口时说两遍没有多出任何信息。
            return rebuild(stages)
        wrapped = rebuild(stages)
        updated = shapes.replace_node(
            wrapped, target.id,
            lambda node: node.model_copy(update={"role": "end"}),
        )
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.info, code="FLOW_ROLE_END_DEFAULTED",
            message=(
                "模型没有标出流程的终点，已把最后一个阶段收尾的那个节点"
                f"（「{target.label}」）标成 end"
            ),
        ))
        return updated if isinstance(updated, FlowContent) else wrapped

    return rebuild(stages)


def _top_level_rows(content: DiagramContent) -> list[dict[str, Any]]:
    """`data["branches"]` 的行：画布最外那一层（一级分支 / 六根骨 / 各阶段）。

    键名与形状刻意不变（`id` / `label` / `points`）：读它的脚本与前端不该为了
    一个图型改一次读法。`points` 在鱼骨里是"这根骨下有几条原因"，在流程里是
    "这个阶段里有几个步骤"，两者都是"这一层下面挂了多少内容"。

    `id` 的取值跟着**这一层自己的命名空间**：树与鱼骨给节点 id（骨就是那个
    `Node`），流程给**阶段 id**（`s_…`）——阶段 id 才是 `FlowContent.edges` 的
    端点，给节点 id 会让读它的人去连一条连不上的线。
    """
    if isinstance(content, FishboneContent):
        return [
            {"id": bone.root.id, "label": bone.category,
             "points": len(bone.root.children)}
            for bone in content.bones
        ]
    if isinstance(content, FlowContent):
        return [
            {"id": stage.id, "label": stage.root.label,
             "points": len(stage.root.children)}
            for stage in content.stages
        ]
    return [
        {"id": child.id, "label": child.label, "points": len(child.children)}
        for child in content.root.children
    ]


def _shape_output(
    content: MindMapContent, *, diagram_type: str, raw_plan: Any,
    diagnostics: list[Diagnostic],
) -> DiagramContent:
    """生成出口：把内部那棵树折成请求的图型；`tree` 原样返回。

    为什么折叠放在收尾的**最后一步**，而不是建树的那一刻：`_fill_details`、
    `_stamp_expandable`、`check()`、`annotate_nli()`、`_cross_branch_duplicates`
    全部按"一棵树"工作（它们读 `root` / `children` / 层深）。早折一步，每一个都得
    写第二份实现；而它们的效果**只看节点本身**，与节点挂在哪一层容器里无关——
    最后一次折叠因此是无损的。

    `tree` 这条分支一个字节都不动：不做 `model_copy`、不加 `diagram_type`（那本来
    就是默认值）。`tests/test_edges.py::TestTreeIsByteIdentical` 用旧模块钉着它。

    **`origin` 在这里统一刷成 `generated`**，排在折形状之前：三种产物共用同一个
    `Node`，折形状（`to_fishbone` / `to_flow`）是搬对象不是复制，所以刷一次三种形状
    同时有；反过来先折再刷就要在两套容器上各走一遍。
    """
    _stamp_origin(content)
    if diagram_type == "fishbone":
        problem = _fishbone_problem(raw_plan, content.title, diagnostics)
        shaped, extras = shapes.to_fishbone(content, problem=problem)
        if extras:
            # 上限那一句只换主语：关着时"比六类多了"是对的（六类就是上限），开着时
            # 上限仍是 6 但类别不是那六个，说"比六类多了"会让用户以为模型漏了 5M1E。
            ceiling = (
                f"一级分支比 {MAX_FISHBONE_BONES} 个多了 {extras} 条"
                if fishbone_free_categories_enabled()
                else f"一级分支比六类多了 {extras} 条")
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn, code="FISHBONE_EXTRA_BRANCH",
                message=(
                    f"{ceiling}，已并入最后一根骨"
                    f"（「{shaped.bones[-1].category}」）：鱼骨图最多 "
                    f"{MAX_FISHBONE_BONES} 根骨，多出来的内容不丢，但位置按规则摆"
                ),
            ))
        _fishbone_shape_diagnostics(shaped, diagnostics)
        return shaped
    if diagram_type == "flowchart":
        shaped, loss = shapes.to_flow(content)
        if loss.dropped:
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn, code="FLOW_EDGE_DROPPED",
                message=(
                    f"{loss.dropped} 条边连的是中心主题而不是阶段，已丢弃："
                    "流程的主干由阶段的先后（数组顺序）表达，"
                    "「从主题出发」那条线在流程图上没有对应的位置"
                ),
            ))
        if loss.demoted:
            # 降级**必须说出来**：端点从"阶段 2 的某一步"退到"阶段 2"，画出来的
            # 那条线还在，但它不再指向用户写的那一步。不说的话，用户会以为自己
            # 的"驳回到补正资料"没被采纳。
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.info, code="FLOW_EDGE_DEMOTED",
                message=(
                    f"{loss.demoted} 条跨阶段的步骤跳转已按**阶段**落线："
                    "阶段之间的边端点只能是阶段（`FlowContent.edges`），"
                    "所以精确到步骤的那一端保不住，方向保留"
                ),
            ))
        shaped = _stamp_flow_roles(shaped, diagnostics)
        return shaped
    return content.model_copy(update={"diagram_type": diagram_type})


# ------------------------------------------------------- 图谱两段式（生成路径二）
#
# ## 它要解决的问题
#
# 三段式的骨架是**用材料自带的章节标题定的**（`_skeleton_outline`）：`heading_outline`
# 把 `#` / `第N章` / `一、` 这些行抓出来当一级分支。这条路有两个天花板，都是实测的：
#
#   - **覆盖率被"标题能不能命中原文"卡住**。骨架定出分支后，每支拿**分支标题**去检索
#     （`_branch_query`），召回上限由标题与材料用词的对齐程度决定：实测 28.8%，把标题
#     换成"背景/现状/方法"这类通用词时掉到 15.4%。而一条真实材料的要点，往往既不在
#     章节标题里，也不在任何一个能猜中的短语下面。
#   - **标题不是内容**。章节标题说的是"这一节叫什么"，不是"这一节里有哪些概念、它们
#     怎么相互依赖"。关系（A 依赖 B、C 是 D 的一部分）在标题层面根本不存在，所以三段式
#     产出的树天然只有父子层级，没有跨分支的语义连线——`relations` 字段一直是空的。
#
# ## 两段怎么切
#
# ```
# 第一段  图谱 → 节点结构 + relations     一次调用
#         输入：图的头部实体与它们之间的边（按度数截取）+ 按实体名检索到的证据
#         输出：{"root": {...}, "relations": [...]}
# 第二段  只写 detail                    一到两次并发（复用 `_fill_details`）
# ```
#
# 与三段式的差别只有一处但这个差别是本质的：**第一段不再看章节标题**。一级分支从
# 图里来——图上度数高的实体就是材料里被反复连接的概念，它们比章节标题更接近"这个
# 材料在讲哪几件事"。检索的 query 也从"模型猜的分支标题"换成"材料里真实出现的实体
# 名"，这是覆盖率能上去的原因（实测见 `docs/GRAPH-TWO-PASS.md`）。
#
# ## 代价（必须写在代码里，否则下一个人会以为是白赚的）
#
# 少一次调用（骨架那一次并入第一段），但 detail 这一段的输入变多了：图谱块要一起进去。
# 更实际的代价是**结构质量**：章节标题是人的组织方式，图谱是关系抽取的副产物，后者
# 会出现"实体对但不成体系"的情况。这个取舍由开关控制，A/B 能同时跑。

GRAPH_SKELETON_ENV = "MINDMAP_GRAPH_SKELETON"
GRAPH_SKELETON_TRUTHY = frozenset({"1", "on", "true", "yes"})

# 提示词里放多少个实体、多少条边。上限不是性能问题，是**注意力**问题：60 个实体已经是
# "一屏能读完的清单"，再多的名字模型只会盯着前几行看（与 `DETAIL_ONLY_PROMPT` 的
# 输出预算是同一类判断）。
GRAPH_ENTITY_LIMIT = 60
GRAPH_LINK_LIMIT = 120

# 拿几个实体名去检索。`DEEP_BRANCH_TOP_K = 3` 是每支的条数，这里是**支数**：12 个实体
# × 3 条 = 36 段原文，与三段式 20 支的检索量同量级，但 query 是材料里的真词。
GRAPH_QUERY_LIMIT = 12

# 跨分支关系的上限。用户口径是"40 节点最多 30 条边"：一条虚线要占掉两个节点之间的
# 走线空间，图上超过 30 条就成了一团毛线，那时用户要的其实是子节点。
GRAPH_MAX_RELATIONS = 30

# 一条关系被收下的最低置信度（模型自报的 0–1）。低于它的直接丢——这是"超了上限按
# 置信度取"的下半句：不设地板时，30 条名额会被 30 条 0.3 的边占满，而 0.9 的第 31 条
# 反而被截掉。
GRAPH_MIN_CONFIDENCE = 0.3


def graph_skeleton_enabled() -> bool:
    """图谱两段式开关。默认**关**：A/B 要能同一条命令切换（`MINDMAP_GRAPH_SKELETON=1`）。

    默认关而不是开：这条路依赖 Neo4j 活着且图已建好，而"图没建"在一台新机器上是常态。
    默认开会让所有新部署的第一张图都掉进降级分支（`_graph_entities` 为空 → 退回三段式），
    白付一次 `graph_store.stats()` 的连接超时。
    """
    return os.getenv(GRAPH_SKELETON_ENV, "").strip().lower() in GRAPH_SKELETON_TRUTHY


def _graph_block(
    entities: Sequence[graph_store.GraphEntity],
    links: Sequence[graph_store.GraphLink],
) -> str:
    """把子图渲染成第一段提示词里那两段清单。

    渲染成**两份清单**而不是"实体 + 邻接"：模型要做的两件事是分开的——先挑出一级分支
    （读实体清单，按度数），再写跨分支关系（读边清单）。混成一份邻接表时它会照抄整张
    图，产出是一张"图的照片"而不是一张"关于主题的导图"。

    度数写进提示词：它是"这个概念在材料里被反复连接"的唯一信号，不写的话模型不知道
    60 个名字里哪几个撑得起一级分支。
    """
    lines = ["【图谱：材料里最重要的概念（按连接数排序）】"]
    for item in entities:
        kind = f"（{item.type}）" if item.type else ""
        lines.append(f"- {item.name}{kind} 连接数 {item.degree}")
    if links:
        lines += ["", "【图谱：这些概念之间的关系（箭头是方向，括号里是材料里的说法）】"]
        for item in links:
            lines.append(f"- {item.src} → {item.dst}：{item.relation}")
    return "\n".join(lines)


# 第一段的 system 尾巴。与 `DETAIL_PROMPT`（树规则）拼在一起用：树规则那一份只此一份，
# 这里只加"一级分支从哪来"与"关系怎么写"。
#
# 为什么这两件事必须分别成段而不是塞进 `DETAIL_PROMPT`：那一份有三个调用点
# （纯主题生成、全塞、按支检索），塞进去会让另两条路也收到"按图谱分支"的要求，
# 而它们根本没有图。

# "一级分支从图谱里怎么挑"这一条**两处要用**：两段式（`GRAPH_TREE_RULES`，一次写完
# 整棵树）与第三档骨架（`GRAPH_PLAN_RULES`，只定一级分支）。抽成常量而不是各写一遍：
# 同一个图谱在两条路上给出不同的挑法时，A/B 就分辨不出"图谱好不好"与"规则松紧"。
_GRAPH_BRANCH_SOURCE = (
    "- 一级分支从「最重要的概念」里挑，优先挑**连接数高**且彼此不重复的；"
    "概念之间是整体与部分的关系时，合成一条分支（用整体当分支名），不要拆成两条；\n"
)

GRAPH_TREE_RULES = (
    "\n这次**不用章节标题定骨架**，一级分支从 user 消息里那张图谱来：\n"
    "- 根节点是主题本身；\n"
    + _GRAPH_BRANCH_SOURCE +
    "- 每个一级分支下面写它的具体要点；要点可以是图上与它相连的概念，"
    "也可以是材料里支持这个分支的细节；\n"
    "- 图谱只是**选材依据**，不是要照抄的清单：图上没有但材料里有的内容照样写，"
    "写成对应分支下的要点。\n"
)

# 跨分支关系。三条硬约束各对应一种会真出错的形态，写法比"要写关系"重要得多：
#
#   - **只允许跨分支**：同一父节点下的两个子节点之间画一条线，树已经表达了它们同属
#     一个父节点；再加线就是 N² 条，图上全是对角线。
#   - **必须有 kind 与 why**：没有理由的边是噪声。`why` 不是给代码看的，是给**读图的
#     人**看的——他点开一条虚线，要能读到"为什么这两个节点有关"。
#   - **有上限**：40 个节点最多 30 条边。模型不会自己收敛，必须给数。
GRAPH_RELATION_RULES = (
    "\n**另外给出一组跨分支的关系**（`relations` 数组，与 root 同级）。"
    "这些线表达的是**树表达不了的**东西：依赖、因果、冲突、整体与部分。\n"
    "每条关系的形状："
    '{"source":"节点id","target":"节点id","kind":"depends_on","why":"一句话说清为什么",'
    '"confidence":0.8}\n'
    "四条规矩，缺一条这条边就不该写：\n"
    "1. **只能连不同一级分支下的两个节点**：同一个父节点下面的两个节点之间有关系，"
    "树已经表达了，不要再画线。\n"
    "2. `kind` 只能取这六个值之一：depends_on（A 依赖 B）/ causes（A 导致 B）/ "
    "conflicts（A 与 B 冲突、互斥）/ part_of（A 是 B 的组成部分）/ "
    "refines（A 是 B 的更具体说法）/ references（材料里明说 A 见 B）。\n"
    "3. `why` 必须写，一句话（不超过 60 字）说清**这两个节点为什么会有关**，"
    "例如「B 是 A 的输入，原文第 2 节把两者并列写出」。写不出理由的边不要写。\n"
    "4. `confidence` 是你对这条边的把握（0–1 的小数）。不确定就写 0.4 以下。\n"
    f"最多 {GRAPH_MAX_RELATIONS} 条。没有值得连的关系就给空数组 []——"
    "不要为了凑数连线，图上每一根虚线都要能被读者问一句「凭什么」并答得上来。\n"
    "`source` / `target` 写的是**节点 id**（你在这份响应里给节点写的那个 id），"
    "不要写节点标题，也不要写实体名。\n"
    # 位置必须逐字写死。实测：说"与 root 同级"时模型把 relations 塞进 **root 里面**
    # （它把 root 当成"主体"，于是"同级"被理解成"和 root 的字段同级"），而 `Node` 是
    # `extra="forbid"`，那一次生成直接判废。`_generate` 里还有一道兜底把 root 内部的
    # relations 摘出来，但提示词这一句才是让它第一次就写对的东西。
    "**位置**：`relations` 与 `title`、`root` 放在**同一个对象**里（最外层），"
    '形如 {"title":"…","root":{…},"relations":[…]}；'
    "**不要**把它放进 root 里面（root 里只有 id / label / children 这些节点字段）。\n"
)

# 交叉指认（**非图谱路**的 `relations` 规则）。与 `GRAPH_RELATION_RULES` 是同一件事的
# 两份，差别在视野：图谱路一次写出整张图 + 六类语义（依赖/因果/冲突/部分/细化/引用），
# 这一份只解决"同一件事被写了两遍"。
#
# 为什么必须有两份而不是共用一份：图谱路的六个 kind 有图谱支撑（材料里明写了谁依赖谁），
# 而三条路（三段式 / 全塞 / 分片）根本没有图，让模型去判"依赖 / 因果"只会得到凭空的线；
# 它们真正需要的只有"这个条目在别处也出现过"。
#
# 为什么非要有这一段：`_SKELETON_BODY` 对它说"需要交叉指认时……**直接指向已有节点**"，
# 而"指向"的写法（`relations`）此前只在 `graph_mode` 下追加（见 `_generate` 末尾那句
# `if graph_mode: system += GRAPH_RELATION_RULES`）。于是那条禁令**不可执行**，模型只剩
# 两条路——删掉一支该有的内容，或者复制一个节点并把 detail 改写成另一个说法。
# 实测（`docs/CROSS-BRANCH-DUPLICATES.md` §4.1）：S02 那 9 处重复里 detail 被改写成
# 材料里根本没有的"考核比重"（重叠 0.46–0.78 而不是接近 1）——**判据的压力制造了改写，
# 改写又制造了捏造**。所以这一段写的不是"别复制"，而是"不复制的时候怎么写"。
#
# 上限 8 而不是 `GRAPH_MAX_RELATIONS`(30)：三段式的树是给定骨架的填充，一屏里能读懂的
# 交叉指认就那么几处；**超过 8 条时正确的动作是合并节点**，不是再加线——这一句要写进
# 提示词，因为"给个上限"只能让模型收敛，说清"超了该干什么"才会让它改结构。
CROSS_REFERENCE_MAX = 8

CROSS_REFERENCE_RULES = (
    "\n**同一件事需要在两条分支下都提到时，用关系指认，不要复制节点**："
    "在输出对象里加一个 `relations` 数组"
    '（形如 {"source":"节点id","target":"节点id","kind":"references",'
    '"why":"这两处为什么是同一件事"}）。\n'
    "什么时候写：一个条目在材料里属于 A，而 B 处也非提它不可"
    "（「课程内容」里的章节名又要在「学时分配」里出现；「设计要求」里的验收项"
    "又要在「里程碑」里被引用）。这时**A 下写节点、B 下只加一条关系，不要在 B 下"
    "再写一遍同一个东西**。\n"
    # 上一版这里只有一句"A 下写节点、B 下不写同一个节点"。三次真跑（`docs/
    # CROSS-BRANCH-DUPLICATES.md` §八）的结论是：**这一句模型没有照做，而且它的
    # 不照做是最危险的一种**——它把 B 处的标题改写成近义再补一条关系，于是逐字判据
    # 看见 red = 0，而图上仍有两份同一件事（8 条关系线的两端**全都还是两个已存在的
    # 节点**）。所以这里换成一句它**自己能判、判据也能看见**的触发器：不去问"材料里
    # 有没有第二个视角"（那需要读材料语义，模型判不准、代码也判不了），而是让它
    # 对比自己刚写下的两个标题——只差一个序号或学时数，就是同一件事写了两份。
    "**先检查一遍你写的标题**：若两条分支下各有一个标题，去掉序号（「第 3 章」"
    "「M2」）与数量（「4 学时」「7 次」）之后**剩下的是同一句话**，那不是交叉指认，"
    "是同一件事写了两份——**并成一处**，另一处若确实要提到，用一条 references 指"
    "过去。图里若出现这种「同核两处」，检查会把它当成「没指认」报出来。\n"
    "**优先给这一类和上面那种情况写关系**：它们比别的交叉指认重要，上限不够时宁可"
    "合并那两处，也不要漏掉它们的指认。\n"
    "`kind` 只用两个值：references（B 处提到的东西就是 A 处那个）/ "
    "part_of（B 是 A 的一部分）。别的语义（依赖、因果、冲突）在这条路上没有依据，不要写。\n"
    "`why` 必填，一句话（不超过 60 字）说清**为什么这两处是同一件事**"
    "（「学时分配表里的第 3 章就是课程内容里的第 3 章」）。写不出这句理由，"
    "说明它们其实不是同一件事——那就各写各的，不要连线。\n"
    "`source` / `target` 写**节点 id**（你在这份响应里给节点写的那个 id），不要写标题。\n"
    f"最多 {CROSS_REFERENCE_MAX} 条；没有就写空数组 []。"
    "**超过这个数说明你该合并节点，而不是再加关系**：两条分支各有一半内容在指同一件事时，"
    "正确的动作是把它们并成一处、另一处用一条关系指过去——加第九条线不解决任何问题。\n"
    # 位置这一句与 `GRAPH_RELATION_RULES` 末尾那段逐字同源，理由也同源：说"与 root
    # 同级"时模型会把 relations 塞进 root 里面，而 `Node` 是 `extra="forbid"`。
    "**位置**：`relations` 与 `title`、`root` 放在**同一个对象**里（最外层），"
    '形如 {"title":"…","root":{…},"relations":[…]}；'
    "**不要**把它放进 root 里面（root 里只有 id / label / children 这些节点字段）。\n"
)

# 第一段 user 消息的收尾要求。与 `_plan_ask` 的分工相同（重点在 user、规则在 system），
# 但内容不同：这里要的是"整棵树 + 关系"，不是"几条分支"。
_GRAPH_ASK_TAIL = (
    "\n\n按上面那张图谱把这张导图**一次写完**：节点只写 id、label、children 与"
    "（一级分支的）color / icon，detail 一律给空字符串 \"\"——说明由紧接着的一次调用"
    "统一补写。最后在 root 同级给出 relations 数组。"
)

# 两段式的证据分节表头。与 `EVIDENCE_SECTION_HEADER` 只差一个名词（"支" → "概念"），
# 但这个名词是模型判断"这段证据属于谁"的唯一线索——写成"第 3 支"而它从没见过第三条
# 分支时，它会去树里找一个不存在的分支，然后少抄引用（不会报错）。
GRAPH_EVIDENCE_HEADER = "第 {index} 个概念「{title}」的检索结果（evidence 数组）："

# 两段式的引用规约。与 `BRANCHED_EVIDENCE_RULES` 的两处差别都不是措辞问题：
#   - 分组单位是**概念**不是分支（理由同上）；
#   - 额外要求"引用挂到讲这个概念的那个节点上"。按概念检索到 3 条命中时，模型很容易
#     把它们全挂到一级分支上——那个节点在画布上是一行标题，挂三条引用等于三条都看不见。
GRAPH_EVIDENCE_RULES = (
    "\n本次已经**按图谱里的概念各做过一次检索**，结果按概念分组附在 user 消息末尾"
    "（每组一个 evidence 数组，与 rag_query 的返回同形，组标题写明是哪个概念）。"
    "写某一部分的内容时，出处只能从**对应的那一组**里抄；不要跨组引用。"
    "引用挂在**真正讲到那个概念的那个节点**上（通常是要点，不是一级分支）："
    "挂在一级分支上等于三条引用都挤在同一行。"
    + EVIDENCE_REF_FIELDS + "\n"
)

_NODE_COLOR_HINT = " / ".join(NODE_COLORS)


def _graph_plan_ask(
    topic: str, branches: int, *, graph: str, evidence: str = "",
) -> str:
    """第一段的 user 消息：主题 → 图谱 → 证据 → 要求。

    顺序即权重（与 `_plan_ask` 把目录放在主题之后同一条理由）：先给它图（选材依据），
    再给它证据（事实依据），最后才是要求。反过来先见到"最多 8 条分支"时，它会根据
    这个数字去图里凑，而不是根据图去决定几条。
    """
    parts = [f"主题：{topic}"]
    if graph:
        parts += ["", graph, ""]
    parts.append(
        f"一级分支不超过 {branches} 条，每条不超过 {GENERATED_LABEL_MAX_CHARS} 字，"
        "彼此覆盖的范围不重叠，按逻辑顺序排列。")
    parts.append(
        "一级分支给一个 color（只能取 "
        f"{_NODE_COLOR_HINT} 之一）与一个 icon（**一个** emoji）。")
    if evidence:
        parts += ["", "下面这些是本次按图谱里的概念检索到的原文：", "", evidence]
    return "\n".join(parts) + _GRAPH_ASK_TAIL


def _resolve_endpoint(
    value: Any, *, by_id: dict[str, str], by_label: dict[str, str],
) -> str | None:
    """关系的端点：先认 id，再认 label。

    与 `_skeleton_edges` 的取舍同源（"先认它能可靠写对的东西"）：模型在同一次响应里
    刚写完 id，抄得比标题准；但它也常把标题当端点写，那一种同样收——两者的正确率
    合起来明显高于只认一种。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return by_id.get(text) or by_label.get(text)


def _relations_from(
    raw: Any, content: MindMapContent, *, limit: int = GRAPH_MAX_RELATIONS,
) -> tuple[list[Relation], dict[str, int]]:
    """模型给的 `relations` → 契约内的关系线，外加一份丢弃计数。

    四道过滤，每一道都对应一种真的会出现的形态：

      1. **端点解析不出来**（dangling / 写成了实体名 / 写成了标题但标题重复）→ 丢。
         留着它的后果不是"少一条线"，是**整张图存不回去**：契约
         `_validate_relations` 对 dangling 是硬拒。
      2. **没有 kind 或 why** → 丢。没有理由的边在画布上是一根说不出来由的虚线，
         它给读者的信息是负的。
      3. **同父**（两端挂在同一个节点下）→ 丢。树已经表达了"它们同属一个父节点"，
         再加一条线就是 N² 条对角线。
      4. **重复**（同一对端点的同一个 kind）→ 丢后一条。

    然后按 `confidence` 降序取前 `limit` 条，`confidence` 低于 `GRAPH_MIN_CONFIDENCE`
    的一并不收。**排序在这里做而不是在提示词里**：提示词只能"请求"模型克制，而这个
    上限是产品口径（画布上画不下第 31 条），必须由代码保证。

    返回的计数进诊断，不静默——用户看到的边数少了，要能查到是"模型没写"还是"写了被
    这里筛掉了"。
    """
    drops = {"dangling": 0, "no_reason": 0, "same_parent": 0, "duplicate": 0,
             "low_confidence": 0, "limited": 0, "unparsed": 0}
    if not isinstance(raw, list):
        return [], drops

    by_id, by_label, parent_of = {}, {}, {}
    stack: list[tuple[Node, str | None]] = [(content.root, None)]
    while stack:
        node, parent = stack.pop()
        by_id[node.id] = node.id
        # 同名节点取先序遍历里第一个：`_skeleton_edges` 的 `by_label` 也是这个口径
        # （后写覆盖先写会让"标题重复"这件事的结果随遍历顺序漂移）。
        by_label.setdefault(node.label.strip(), node.id)
        parent_of[node.id] = parent
        stack.extend((child, node.id) for child in node.children)

    ranked: list[tuple[float, int, Relation]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            drops["unparsed"] += 1
            continue
        source = _resolve_endpoint(item.get("source"), by_id=by_id, by_label=by_label)
        target = _resolve_endpoint(item.get("target"), by_id=by_id, by_label=by_label)
        if not source or not target or source == target:
            drops["dangling"] += 1
            continue
        kind = str(item.get("kind") or "").strip().lower()
        why = str(item.get("why") or "").strip()
        if not why or not kind:
            drops["no_reason"] += 1
            continue
        if kind not in RELATION_KIND_VALUES:
            # 认不出来的 kind 与"没写 kind"是同一件事：契约只收那六个值，而在这里
            # 放过去会让 `Relation(...)` 抛 ValidationError、整张图报 500。
            drops["no_reason"] += 1
            continue
        if parent_of.get(source) == parent_of.get(target):
            drops["same_parent"] += 1
            continue
        key = (source, target, kind)
        if key in seen:
            drops["duplicate"] += 1
            continue
        seen.add(key)
        confidence = item.get("confidence")
        try:
            score = float(confidence) if confidence is not None else 0.5
        except (TypeError, ValueError):
            score = 0.5
        if score < GRAPH_MIN_CONFIDENCE:
            drops["low_confidence"] += 1
            continue
        ranked.append((score, index, Relation(
            # id 由这里分配：模型写的 id 与节点 id 在同一个命名空间里混着，容易撞。
            id=f"rel_{index + 1}",
            source=source,
            target=target,
            kind=kind,
            why=why,
            provenance="ai",
        )))

    ranked.sort(key=lambda row: (-row[0], row[1]))
    kept = [relation for _, _, relation in ranked[:limit]]
    drops["limited"] = max(0, len(ranked) - limit)
    return kept, drops


# -------------------------------------- 外部检索材料入图（`_intake` 的那一步）
#
# ## 缺口在哪
#
# 图谱在生成侧有两处出口（两段式 `MINDMAP_GRAPH_SKELETON` 与第三档 `_graph_outline`），
# 两处吃的是同一个前提：**这批材料在图里**。而材料进图只有一条路
# ——`scripts/ingest_graph_corpus.py`（离线、人工、整库重灌）。生成链路自己从不建图：
#
#     外部检索（`_external_sources`）→ library.ingest 入库（快，无模型）   ✅ 有
#     library_sources → 建图（抽取，慢，每条材料一块一次 LLM）             ⚠️ 只有脚本
#
# 于是"全新主题 + 联网搜索"这条真实流程永远过不了第三档的第 4 道闸
# （`GRAPH_OUTLINE_MIN_ENTITIES`）：图里没有这批材料的边，`scoped_entity_count`
# 恒为 0，`_graph_outline` 每次返回空。这一档在有材料的场景里实际上**从未生效过**，
# 生效过的只有那 22 份被脚本重灌过的旧材料。
#
# 同样的道理也落在两段式上（`MINDMAP_GRAPH_SKELETON` 的 `GRAPH_SKELETON_UNAVAILABLE`），
# 但这一档**只补第三档**：两段式换的是"一级分支从哪来"，是另一个开关的事。
#
# ## 只入这一次要用的
#
# 不是全库、不是后台补历史：`_external_sources` 的命中已经过词面门
# （`external._relevant_hits`），那 3 条就是这次生成要用的 3 条。入别的东西等于拿用户的
# 钱替下一次请求铺路，而"下一次"是不是同一批材料没人知道。
#
# ## 幂等靠三把锁，第三把省的是钱
#
#   1. `library.ingest` 按 `content_hash` 去重——同一份正文只落一行（外部来源在
#      `_external_sources` 里已经过了这一步，所以这里直接把 `source.id` 显式传给
#      `ingest_graph(..., source_id=)`，不再写一次库）；
#   2. `graph_store` 的边 `UNIQUE (src, dst, relation, source_id, chunk_start)`——
#      重复抽同一块不会长出新边；
#   3. **`scoped_entity_count([source.id]) > 0` 就跳过这份**。前两把锁保证"重复不写"，
#      但保证不了"重复不抽"：抽取是每块一次 LLM 调用，第二把锁要等**抽完**才生效。
#      实测同一主题连续生成两次时，外部检索大概率命中缓存（`external_cache` 24 小时），
#      材料一模一样——少了这一把，第二次生成要原价再付一遍抽取的钱，换来的边还全是
#      重复的。
#
# 第三把锁的代价写在明处：**部分入过图的材料会被整体跳过**。熔断在中途（第 3 块写库
# 失败）之后再生成一次，剩下那几块不会补。这里选了"宁可少几块，不要每次重付"：
# 那些块本来也只能靠人工重灌补齐，而"每次生成偷偷多花一遍钱"没有任何人看得见。
#
# ## 成本与默认值（实测，`scripts/measure_graph_ingest.py`）
#
# 主题「海绵城市」，真实检索 3 条 / 2 816 字 / 7 块（维基 2 427 字 5 块 + 两份纯摘要
# 各 1 块）：
#
#     串行 7 块            20.86 秒      （长维基那 5 块占 17.29 秒，摘要各 1.8 秒）
#     并发 7 块（产品实现） 17.53 秒      95 实体 / 79 边
#     抽取那一层            7 次调用    输入 6 827 token + 输出 6 385 token = 13 212
#
# 单块 2.5–3.4 秒、约 1.9k token，与"179 块 / 610 秒 / 306k token"的离线口径一致
# （`data/graph-ingest-report.json`）。而一次生成是 38–97 秒——**入图这一下等于
# +17.5 秒，也就是 +40% 左右**，并发已经把串行那 20.9 秒压掉了 16%。
#
# ## 它到底改好了什么（这才是默认值取关的真正理由）
#
# 同一个主题的 A/B 跑了两种材料形状，结果完全不同：
#
#   - **真实检索形状**（维基 1 份 + 摘要 2 份）：全批目录 7 条 / 覆盖率 0.718，
#     过得了 `OUTLINE_MIN_COVERAGE`，于是 `_graph_outline` 那道 `if not outline_entries`
#     根本不进——两组的一级分支**逐字相同**（背景 / 历史 / 六大措施 / 争议 / 相关条目 /
#     参考资料与外部链接），骨架忠实度同为 66.7%。**入图贡献 0，代价 +17.5 秒。**
#   - **目录不可用**（只喂那两份摘要）：A 组是「建设背景与理念 / 技术措施与设施 /
#     规划设计与标准 / 运行维护与管理 / 效益评估与成效 / 问题挑战与展望」，**忠实度
#     0%**——一条都不在原文里；B 组是「海绵城市理念 / 海绵城市建设试点 / 汉阳四新示范区
#     / 海绵+升级理念 / 海绵城市设施措施 / 自然排水雨洪管理系统」，**忠实度 66.7%，
#     其中 4 条只能来自那两份摘要**。这一档的真实作用域就是这个形状。
#
# 所以成本只在"这一档**有可能**接管"时才付——第五道闸（`_outline_entries` 非空就不入）
# 就是这件事，见 `_ingest_external_graph` 的注释。于是这个开关打开之后的行为是：
#
#     目录可用（真实检索里最常见）→ 零成本、零变化（一次本地抽标题，不发网络）
#     目录不可用（全摘要，或维基那条没命中）→ +17.5 秒，一级分支从"模板词"变成材料原词
#
# 默认值仍取**关**：第二行那种形状在真实检索里是小概率，而它的收益目前只有一次 A/B
# （每组 1 次采样）。要翻默认值，先把 `measure_graph_ingest.py --repeat 3` 跑够样本。
GRAPH_INGEST_ENV = "MINDMAP_GRAPH_INGEST"

# 与 `MINDMAP_GRAPH_EXTRACTION`（`graph_extract.ENABLED_ENV`）的关系：**两个开关都要
# 开**才真的抽。抽取那一个管"要不要花 LLM 的钱抽"，默认关；这一个管"生成时要不要顺手
# 建图"。这里**不读**抽取的开关——多一处开关判断就多一处会与 `graph_extract` 分叉的
# 地方（同 `graph_ingest` 模块 docstring 那条规矩）。关着时的表现是 `chunks > 0` 而
# `entities == 0`，那笔账如实记在日志里（一条 warning），不假装成功。
_GRAPH_INGEST_TRUTHY = {"1", "true", "yes", "on", "y", "t"}

GRAPH_INGEST_MAX_CHUNKS = 6
"""每份材料最多抽多少块。`graph_ingest.DEFAULT_MAX_CHUNKS`（20）是给"上传一份文件"
那条路定的；这里是**每次生成都要付**的钱，闸要更紧。

6 块 ≈ 4 200 字（`CHUNK_MAX_CHARS` 700 + 重叠），而外部命中的单份上限是
`external.TEXT_MAX_CHARS` = 8000 字（约 12 块）——所以长维基条目只能进前一半。
这个取舍是刻意的：骨架只需要"这份材料讲了哪几件事"，那只在前几段里；而截断
**只影响长材料**，摘要类的 1 块就全进了（那正是上一轮"论文摘要一条标题都没贡献"
的那批）。"""

GRAPH_INGEST_CONCURRENCY = 3
"""并发入图的材料数。外部命中上限就是 3 条（`EXTERNAL_HIT_LIMIT`），所以常规情况下
一次 `gather` 全部发出去；写 3 而不是 6 是给"将来命中上限调大"留的闸——抽取是
LLM 调用，并发越多越容易撞限流，而多等一轮比 429 之后全体重试便宜。"""


def graph_ingest_enabled() -> bool:
    """入图那一步是否启用（**默认 `False`**）。

    默认关的理由与 `docs/GRAPH-OUTLINE.md` 那一档相反：那一档是"补一个缺口、不额外
    花钱"，所以默认开；这一步要为每次生成多付十几秒与一万多 token，所以默认关，
    由实测（`scripts/measure_graph_ingest.py` 的 A/B）决定要不要翻。
    """
    return os.getenv(GRAPH_INGEST_ENV, "").strip().lower() in _GRAPH_INGEST_TRUTHY


def _graph_ingest_pending(sources: Sequence[Source]) -> list[Source]:
    """这批材料里**还没入过图**的那些。

    先查一次并集再逐份查，而不是直接逐份：全新主题（= 这一档真正要服务的场景）里
    并集恒为 0，一次只读 Cypher 就能判定"全都要入"，省掉 N 次往返；命中过的那条路
    才逐份定位。两种情况的判据是同一个函数（`scoped_entity_count`），不会出现
    "并集说有、逐份说没有"这种自相矛盾——它的口径就是 `source_id` 上的边。
    """
    if not sources:
        return []
    if graph_store.scoped_entity_count([source.id for source in sources]) <= 0:
        return list(sources)
    return [source for source in sources
            if graph_store.scoped_entity_count([source.id]) <= 0]


async def _ingest_external_graph(
    sources: Sequence[Source], *, skip_sink: list[str] | None = None,
) -> dict[str, Any] | None:
    """把这次检索回来的材料抽进图；不做时返回 `None`。

    五道闸按代价从低到高排，前四道与 `_graph_outline` 同构（它们问的是同一个部署
    问题），第五道是**这一档独有的、也是最重要的**：

      0. `graph_ingest_enabled()` 为假（默认）→ None，一次查询都不发；
      1. 没有材料 → None；
      2. `configured()` 为假（没配密码）→ None，**不发起任何连接**；
      3. `available()` 为假（刚连失败、冷却中）→ None，同样不连——库不在时把
         3 份材料各付一遍 15 秒连接超时，等于让用户为一次注定失败的建图等一分钟。
         这一条比 `_graph_outline` 更要紧：那边只付一次，这边是 N 份并行；
      4. **这批材料还有能用的章节目录**（`_outline_entries` 非空）→ None。判据与
         `_generate` 里那道 `if not outline_entries` 是同一个函数：**目录可用时第三档
         根本不会被问到**，入图的产物一次都不会被读。实测（`measure_graph_ingest.py`
         的形状 1）就是这一种：全批 7 条目录 / 覆盖率 0.718，两组一级分支**逐字相同**，
         而这一边白付了 17.5 秒。这是五道闸里唯一"拦掉的是注定零收益"的那一道。

    返回值是**这一趟的账**（不是抽取结果）：`materials` / `skipped` / `chunks` /
    `entities` / `edges` / `failed` / `aborted` / `seconds`。它进诊断与日志——
    "入图到底跑了没有、跑了多久"必须能从 `data` 与日志里看出来，否则用户只会说
    "图谱好像没生效"，而这正是上一轮最难查的那件事。

    **失败一律降级**：任何一份抛错（写库失败、切块异常）只记 warning 并从统计里
    剔除，生成照常继续。这一整步是"让骨架更好"的加分项，它失败不该让用户拿不到图。

    `skip_sink` 是**给用户看的那句话的出口**：跳过时把原因（`GRAPH_SKIP_*`）追加进去，
    由调用点转成诊断。出参而不是返回值，是因为 `None` 的语义已经被"没做"占住了
    （开关关 / 没材料 / 图不可用 / 有目录，四种都返回 None，而它们对用户是四件事）。
    """
    if not graph_ingest_enabled() or not sources:
        return None
    if not graph_store.configured():
        _note_graph_skip(skip_sink, GRAPH_SKIP_UNCONFIGURED)
        return None
    if not graph_store.available():
        _note_graph_skip(skip_sink, GRAPH_SKIP_COOLING)
        return None
    if _outline_entries(list(sources)):
        # 目录可用 = 第三档不接管 = 入图的产物无人消费。记一行是刻意的：用户打开了
        # 开关却没看到任何成本与任何变化，日志里必须有话说。
        logger.warning("检索材料入图跳过：这批材料有可用章节目录，第三档不会接管")
        _note_graph_skip(skip_sink, GRAPH_SKIP_HAS_OUTLINE)
        return None

    pending = _graph_ingest_pending(sources)
    stats: dict[str, Any] = {
        "materials": len(sources), "skipped": len(sources) - len(pending),
        "chunks": 0, "entities": 0, "edges": 0, "failed": 0, "aborted": False,
        "seconds": 0.0,
    }
    if not pending:
        # 全都入过了：这不是"没做"，是"做过"。返回统计而不是 None，诊断里那一行
        # 才有得说（"3 份材料已在图里，跳过"），否则用户看到的是一片沉默。
        logger.info("检索材料入图：%d 份都已在图里，跳过（零成本）", len(sources))
        return stats

    started = time.perf_counter()
    semaphore = asyncio.Semaphore(GRAPH_INGEST_CONCURRENCY)

    async def _one(source: Source) -> dict | Exception:
        async with semaphore:
            try:
                return await graph_ingest.ingest_graph(
                    source, max_chunks=GRAPH_INGEST_MAX_CHUNKS, source_id=source.id)
            except Exception as exc:  # noqa: BLE001 — 单份失败不废整次入图
                logger.warning(
                    "检索材料入图失败（%s）：%s：%s",
                    (source.filename or source.id)[:40], type(exc).__name__,
                    str(exc)[:160])
                return exc

    outcomes = await asyncio.gather(*(_one(source) for source in pending))
    for outcome in outcomes:
        if isinstance(outcome, Exception):
            # 抛出来的那份**必须**计进 `failed`：原来这里直接 `continue`，于是
            # "Neo4j 停机、每份都抛 `GraphStoreError`"这种形状在统计里长得与
            # "没有材料可入图"一模一样（chunks=0 / entities=0 / failed=0）——
            # 而它恰恰是最该被看见的那一种。
            stats["failed"] += 1
            continue
        for key in ("chunks", "entities", "edges"):
            stats[key] += int(outcome.get(key) or 0)
        # 抽取层自己也报失败（块没写进去 / 被熔断中止），两份数分开加不重不漏：
        # 此地只加它返回的那一份，抛出异常的份已经在上面的分支里计过了。
        stats["failed"] += int(outcome.get("failed") or 0)
        stats["aborted"] = stats["aborted"] or bool(outcome.get("aborted"))
    stats["seconds"] = round(time.perf_counter() - started, 2)

    logger.info(
        "检索材料入图：%d 份（跳过 %d 份已在图里的）→ %d 块 / 实体 %d / 边 %d / "
        "失败 %d%s，耗时 %.2fs",
        len(pending), stats["skipped"], stats["chunks"], stats["entities"],
        stats["edges"], stats["failed"], "（中止）" if stats["aborted"] else "",
        stats["seconds"],
    )
    if stats["failed"] and not stats["entities"]:
        # **一份都没成功**（开关开着、材料在、一块都没写进去）：先报这一句，别再说
        # "抽进了图谱"——`chunks` 非 0 只说明"材料被切开了"，不说明"进去了"。
        #
        # 两种来路，必须都说出来：抽取层一次调用都没发（`extract_graph` 返回 None：
        # 开关关着 / LLM 凭据缺失 / 调用失败）与写库失败（Neo4j 连不上）。判据是
        # **耗时**：0.0 秒说明请求根本没发出去，3 秒说明发了、是写不进去。只指一个
        # 方向的指向会把用户带到错的地方——实测两种都在本轮出现过。
        logger.warning(
            "检索材料入图：%d 份全部失败（已切 %d 块、实体 0、耗时 %.1f 秒）——"
            "耗时约 0 说明抽取层一次调用都没发（看 %s 与 LLM 凭据），"
            "耗时大于 0 说明请求发了、写不进去（看 check-neo4j.ps1）",
            stats["failed"], stats["chunks"], float(stats["seconds"]),
            graph_extract.ENABLED_ENV)
    elif stats["chunks"] and not stats["entities"]:
        # 抽取开关（`graph_extract.ENABLED_ENV`）关着时就是这个形状：块切了、一次
        # 调用都没发、实体为 0。说清楚，否则"入图开着却什么都没进图"只能靠翻代码解释。
        logger.warning(
            "检索材料入图：%d 块一条实体都没抽到——检查 %s 是否打开",
            stats["chunks"], graph_extract.ENABLED_ENV)
    return stats


def _graph_ingest_diagnostic(stats: dict[str, Any]) -> Diagnostic:
    """入图那一步给人看的那一行。三种出口三句话。

    "跑了几块 / 花了多久"是这一档唯一的可见成本，必须报出来：用户等的那十几秒
    除了诊断与日志之外没有任何痕迹（前端只认 `external_search` 那一对 stage 帧）。
    "全都已在图里"也必须是一句独立的结论——它和"什么都没抽到"在界面上长得一样，
    而前者是零成本命中、后者是配置错了。
    """
    if stats.get("failed") and not stats.get("entities"):
        # **一份都没成功**（开关开着、材料在、库不可写）：这一支必须排在 `chunks` 前面。
        # `chunks` 有数只说明"材料被切开了"，不说明"进去了"——按原来的顺序，Neo4j
        # 停机那一次会得到一句"这次把 2 份检索材料抽进了图谱（2 块 / 0 实体 / 0 边）"，
        # 而它恰恰是最该被当成故障的形状。实测这句话就是本轮反例里出现的那一条。
        return Diagnostic(
            level=DiagnosticLevel.warn, code="GRAPH_INGEST_FAILED",
            message=(
                f"这次 {stats['failed']} 份检索材料一份都没能写进图谱（{stats['chunks']} "
                f"块切开、实体 0、耗时 {float(stats.get('seconds') or 0):.1f} 秒）——"
                "这一次的图谱骨架也就没有材料可读。耗时约 0 说明抽取层一次调用都没发"
                f"（`{graph_extract.ENABLED_ENV}` 或 LLM 凭据）；耗时大于 0 说明请求发了、"
                "是写不进去（`check-neo4j.ps1`，日志里有每份的异常类名）。"),
        )
    if stats.get("chunks"):
        materials = int(stats.get("materials") or 0) - int(stats.get("skipped") or 0)
        notes = "（中止）" if stats.get("aborted") else ""
        failed = int(stats.get("failed") or 0)
        if failed:
            # 部分失败也要说出来：上面那一支管"全失败"，这一支管"进了一部分"。
            notes += f"（其中 {failed} 份失败）"
        return Diagnostic(
            level=DiagnosticLevel.info, code="GRAPH_INGEST_DONE",
            message=(
                f"这次把 {materials} 份检索材料抽进了图谱（{stats['chunks']} 块 / "
                f"{stats['entities']} 实体 / {stats['edges']} 边，耗时 "
                f"{float(stats.get('seconds') or 0):.1f} 秒）{notes}。"),
        )
    if stats.get("skipped"):
        return Diagnostic(
            level=DiagnosticLevel.info, code="GRAPH_INGEST_DONE",
            message=(f"{stats['skipped']} 份检索材料已经在图谱里，这次跳过抽取"
                     "（零成本）。"),
        )
    return Diagnostic(
        level=DiagnosticLevel.info, code="GRAPH_INGEST_DONE",
        message="这次没有可入图的检索材料。",
    )


# ------------------------------------------- 第三档：没有章节标题时用图谱定骨架
#
# ## 缺口在哪
#
# 骨架的第一档是**材料自带的章节**（`_skeleton_outline` → `heading_outline`，覆盖率
# ≥ `OUTLINE_MIN_COVERAGE` 才拼）。抽不出标题时覆盖率是 0，过不了闸，`entries` 为空，
# `_render_outline` 返回空串——于是 `PLAN_PROMPT` 里那句「材料自带章节时优先采用它们
# 作为一级分支；**目录为空或不完整时再自行归纳**」生效。
#
# 问题是"自行归纳"手上什么都没有：**骨架那一次调用的输入只有主题**（正文要到第三步
# "结构"才进 prompt，见 `_intake` 的 `full_text` 与 `_retrieve_branches`）。所以它给
# 的是**模板词**：8 次采样（`docs/GRAPH-OUTLINE.md` 第 5 节）里，没有这一档时一级分支
# 的**逐字忠实度是 0/24**——24 条分支没有一条能在本次材料的原文里找到；有这一档时
# 6/23。那 24 条是「基础理论 / 医疗健康 / 金融风控 / 智能制造 / 交通出行 / 挑战与趋势」
# 这类说法：它们对"机器学习在各领域的应用"这个主题永远成立，对**这几份材料**一条都
# 不成立。
#
# ## 为什么图谱补得上
#
# 抽取层在**短材料**上抽得反而不差（`data/graph-ingest-report.json` 那批外部检索正文
# 里，349 字的摘要 17 个实体 / 16 条边、111 字的 9 个 / 8 条，边∶实体 ≈ 1∶1；而
# 2 万字的大文档是 45–53 字一个实体，反而稀）。摘要那种文本通篇是"A 是 B 的一种"
# "A 用了 B"，抽出来正好是一张概念图——而"这份材料讲了哪几件事"正是骨架要问的。
#
# ## 三条闸，缺一条就会误触发
#
# 1. **只在第一档不可用时进**（调用方判 `not _outline_entries(sources)`）。有章节的
#    材料一个字符都不变——那条路钉着逐字节用例（`tests/test_single_call_generate.py`、
#    `tests/test_outline.py`）。
# 2. **图里必须有这批材料自己的带边实体**，数量 ≥ `GRAPH_OUTLINE_MIN_ENTITIES`。
#    判据只看 scope，**不看** `subgraph` 的返回条数：那个函数在 scope 落空时会回退
#    全图（"好过两段式直接降级"），拿它当判据等于"库里只要有图，每批材料都算有图"。
# 3. **图不可用就当没有这一档**（没配密码 / 连不上 / 熔断中）：退回"模型自行归纳"，
#    与加这一档之前逐字节相同。
#
# ## 与两段式（`MINDMAP_GRAPH_SKELETON`）的关系
#
# 两段式是**整棵树都不看章节标题**：一级分支直接从图谱挑，骨架那一次调用被省掉。
# 这一档只在"章节不可用"时接管**骨架这一次调用**——后面三步（检索 / 结构 / 说明）
# 一个字不动，`titles` 仍由模型给（带 color / icon），所以三级分组、按支检索、
# `_fill_details` 全部照旧。两段式那个开关管的不是这件事，所以这一档有**自己的**
# 开关（见 `GRAPH_OUTLINE_ENV`）。

GRAPH_OUTLINE_MIN_ENTITIES = 8
"""这批材料在图里至少要有的**带边实体**数，低于它就不用图谱定骨架。

实测（`data/graph-outline-probe.json`，那 15 份既无章节标题、又有正文的外部材料，
每份自己的带边实体数）：

    3  4  4  4  5  6  8  8  9  10  10  10  16  17  18      中位数 8 / 最小 3 / 最大 18

取 **8（= 中位数）**，取的不是最小值：实体数不是分支数的上界，里面还夹着 `其他`
类型的名字（`service.py`、`w1`）与按规则要合并的整体-部分对（`_GRAPH_BRANCH_SOURCE`），
而一级分支至少要 `MIN_BRANCHES` = 3 条、请求一般要 5–6 条。按"每条分支至少有一个
能与别的概念相连的概念"算，8 是还能给出 3 条以上互不重叠分支的下限；3–5 个实体的
材料（那 15 份里有 5 份）最多凑出两条，与"让模型自己归纳"的差别落在噪声里。

注意判据作用于**本次这一批材料的并集**（`scope` 传的是全部 source_id），不是单份：
一次请求给三份 4 个实体的短摘要时，并集通常已经够 8 条——跨材料共享的实体在图上
本来就是同一个节点。
"""

GRAPH_OUTLINE_HEADER = "这份材料没有章节标题，但它在图谱里有这些概念（请按它们划分一级分支）："
"""第三档拼进骨架提示词的表头。与 `OUTLINE_HEADER` 平行、措辞必须区分开：
说成"材料自带的章节"时模型会去原文里找章，而原文里没有。"""

# 第三档的开关。**默认开**：它是补一个缺口（目录不可用那条路上模型手上没有任何
# 结构信息），不是加一个新能力，所以默认就该生效。留着它只为两件事——A/B 要能跑
# "没有这一档"的那一组（`scripts/measure_graph_outline.py`），以及出问题时能一键
# 退回改动之前（`MINDMAP_GRAPH_OUTLINE=0`）。
#
# 与 `MINDMAP_GRAPH_SKELETON` / `MINDMAP_GRAPH_SEARCH` 的写法一致（正列举假值，
# 默认值写在这一处）：不设变量时取默认，显式写 `0/off/false/no` 才关。
#
# 默认开的代价只有"配了图密码的部署"要付：`_graph_outline` 先看 `configured()`
# （只看密码，不连库），没配图的机器这条闸一次连接都不会发。
GRAPH_OUTLINE_ENV = "MINDMAP_GRAPH_OUTLINE"
_GRAPH_OUTLINE_FALSY = {"0", "off", "false", "no"}


def graph_outline_enabled() -> bool:
    """第三档是否启用（默认 `True`）。"""
    return os.getenv(GRAPH_OUTLINE_ENV, "1").strip().lower() not in _GRAPH_OUTLINE_FALSY


GRAPH_PLAN_RULES = (
    "\n这次**没有可用的章节标题**，一级分支从 user 消息里那张图谱来：\n"
    + _GRAPH_BRANCH_SOURCE +
    "- 图谱只是**选材依据**，不是要照抄的清单：图上没有但材料里有的内容，"
    "归进最接近的那条分支；\n"
    # 这一条打在"模板词"上：模型在拿不到结构时最省力的做法是套一组放到任何材料上
    # 都成立的说法（「概述 / 现状 / 挑战 / 展望」）。要求它用图上那些**原词**，等于
    # 要求分支名能在材料里找到依据——这也是 A/B 里"骨架忠实度"那个指标的判据。
    "- 分支名优先用材料里的**原词**（上面这张图谱里的概念名就是材料里的词）；"
    "要把两个概念合成一条时，用它们在材料里的说法拼，不要换成「概述 / 现状 / 挑战 / "
    "应用前景」这类放到任何材料上都成立的空词；`service.py`、`w1` 这种抽取层的名字"
    "不必照抄，但也不要改写成材料里没有的说法；\n"
    "- 这一步**只定一级分支**：不写子要点、不写 relations。\n"
)
"""第三档的 system 尾巴。与 `GRAPH_TREE_RULES` 共用"分支从哪来"那一条
（`_GRAPH_BRANCH_SOURCE`），差别只在收尾：那一份要模型一次写完整棵树 + relations，
这一份只要几条分支名。给错规则时模型会照两段式输出 children，而 `_skeleton_plan`
只认 `branches`——那一次生成会以「分支太少」失败。"""


# ------------------------------------ 「图谱这一档为什么没跑」：四种原因，四句话
#
# 降级本身是对的（不为一条请求付一次连接超时），**不说**是错的：用户在界面上拿到的
# 那张图与"图谱骨架真的定了骨架"那张长得一模一样，唯一能分辨的东西就是诊断。这四道
# 闸原来一律 `return "", 0`，SSE 里一条 diagnostic 都没有，日志里只有 warning——
# 于是"图谱这条路在跑吗"只能靠翻代码回答。这一节就是补那四句话。
#
# 四种原因必须**分开报**，因为它们要的处置完全不同：
#
#     没配密码  → 改配置（配上 `MINDMAP_NEO4J_PASSWORD` 并重启后端）
#     连不上    → 起服务（本机是 `run-neo4j.bat`，它不会自启）
#     熔断冷却  → 等着（指数退避 30s → 300s；重启后端反而把计数清零、下次更慢）
#     实体不足  → 什么都不用做（材料太少，是正常结果）
#
# 合成一句"图谱不可用"等于让用户去猜该做哪一件——那与不说没有区别。
GRAPH_SKIP_DISABLED = "disabled"
GRAPH_SKIP_UNCONFIGURED = "unconfigured"
GRAPH_SKIP_UNREACHABLE = "unreachable"
GRAPH_SKIP_COOLING = "cooling"
GRAPH_SKIP_TOO_FEW = "too_few_entities"
GRAPH_SKIP_HAS_OUTLINE = "has_outline"

_GRAPH_NOTICES: set[str] = set()
"""这个进程里已经报过的 `(code: reason)`。

为什么去抖：冷却从 30 秒起，这 30 秒里用户可能连点五次生成——五次都报"Neo4j 连不上"
比一句不报更烦，还会把这条提示挤成"又一条可以无视的东西"。**日志里不去抖**（每次
一条 warning）——诊断给正在看图的人，日志给排障的人，两份读者的容忍度不一样。
"""


def reset_graph_notices() -> None:
    """清空"已经报过"的账。测试用例之间必须隔离，否则第二个用例永远看不到诊断。"""
    _GRAPH_NOTICES.clear()


def _graph_notice_once(code: str, reason: str) -> bool:
    """`(code, reason)` 这个组合在本进程里第一次出现时返回 `True`。"""
    key = f"{code}:{reason}"
    if key in _GRAPH_NOTICES:
        return False
    _GRAPH_NOTICES.add(key)
    return True


def _note_graph_skip(sink: list[str] | None, reason: str) -> None:
    """把跳过原因写进出参（`_ingest_external_graph` 的 `skip_sink`）。"""
    if sink is not None:
        sink.append(reason)


def _graph_outline_skip_diagnostic(
    reason: str, entities: int, *, mixed: int = 0,
) -> Diagnostic | None:
    """第三档跳过时给人看的那一行；`None` = 这次不该报（开关是用户自己关的）。

    `mixed` 是"这批里有多少份没有章节的材料"（>0 = 混合材料，走的是补充档）。它只改
    **后果那一句**：整批没章节时后果是"一级分支全由模型归纳"，混合批的后果是
    "有章节的那几份照旧、这几份没有分支指向它们"——同一句"模型自己归纳的"在混合批上
    是假话（骨架明明来自章节）。
    """
    if not reason or reason == GRAPH_SKIP_DISABLED:
        return None
    uri, _database = graph_store.endpoint()
    cooldown = graph_store.cooldown_remaining()
    consequence = (
        f"这次只有自带章节的那几份定了骨架，{mixed} 份没有章节的材料没有分支指向它们。"
        if mixed else "这次的一级分支是模型按主题自己归纳的。"
    )
    if reason == GRAPH_SKIP_UNCONFIGURED:
        level = DiagnosticLevel.info
        head = (f"图谱这一档没生效：这台机器没有配 {graph_store.PASSWORD_ENV}，"
                "图谱骨架与材料入图整条都不走——")
        hint = "要用图谱就配上密码并重启后端。"
    elif reason == GRAPH_SKIP_UNREACHABLE:
        level = DiagnosticLevel.warn
        head = (f"图谱这一档没生效：连不上 Neo4j（{uri}），刚失败一次，接下来约 "
                f"{max(1, round(cooldown))} 秒内不会再试——")
        hint = "把这台机器上的 Neo4j 起起来（`run-neo4j.bat`）就恢复。"
    elif reason == GRAPH_SKIP_COOLING:
        level = DiagnosticLevel.warn
        head = (f"图谱这一档暂时跳过：Neo4j（{uri}）还在熔断冷却里，约 "
                f"{max(1, round(cooldown))} 秒后才会再试——")
        hint = ("冷却会自己结束，不用重启后端；要立刻恢复就把 Neo4j 起起来"
                "（`run-neo4j.bat`）。")
    elif mixed:
        level = DiagnosticLevel.info
        head = (f"图谱这一档没补上：那 {mixed} 份没有章节的材料在图里只有 {entities} 个"
                "带连接的实体，取不出能指到它们的概念——")
        hint = ("这不是故障——材料本身没问题，是抽出来的概念还彼此连不起来；把这几份"
                "重新入图（`scripts/ingest_graph_corpus.py`）能补上。")
    else:
        level = DiagnosticLevel.info
        head = (f"图谱这一档没触发：这批材料在图里只有 {entities} 个带连接的实体"
                f"（少于 {GRAPH_OUTLINE_MIN_ENTITIES}），按材料太少处理，")
        hint = "这不是故障——材料本身没问题，是抽出来的概念还彼此连不起来。"
    message = head + consequence + hint
    # 混合批与整批的账分开报：前者要说明"哪几份没位置"，后者才是"骨架从哪来"。
    key = f"{reason}:mixed" if mixed else reason
    if not _graph_notice_once("GRAPH_OUTLINE_UNAVAILABLE", key):
        # 诊断只报第一次；日志**每次**留一条（那才查得出"这次也降级了"），但重复的
        # 那几次走 info：warning 每次一条时，冷却 30 秒里连点几次就把日志刷成噪声。
        logger.info("图谱骨架跳过（%s，已报过，不再进诊断）：%s", reason, message)
        return None
    logger.warning("图谱骨架跳过（%s，首次即报）：%s", reason, message)
    return Diagnostic(level=level, code="GRAPH_OUTLINE_UNAVAILABLE", message=message)


def _graph_ingest_skip_diagnostic(reason: str) -> Diagnostic | None:
    """入图跳过时给人看的那一行；`None` = 这次不该报（开关关 / 没材料）。"""
    if not reason or reason == GRAPH_SKIP_DISABLED:
        return None
    if reason == GRAPH_SKIP_HAS_OUTLINE:
        level, code = DiagnosticLevel.info, "GRAPH_INGEST_SKIPPED"
        message = ("检索材料没有入图：这批材料自带可用的章节目录，骨架走章节那条路，"
                   "入图的产物这一次不会被读到——省下的正是抽取那一笔钱。")
    elif reason == GRAPH_SKIP_UNCONFIGURED:
        level, code = DiagnosticLevel.info, "GRAPH_INGEST_UNAVAILABLE"
        message = (f"检索材料没有入图：这台机器没有配 {graph_store.PASSWORD_ENV}，"
                   f"{GRAPH_INGEST_ENV} 开着也不会做——图谱那一整条路都不走。")
    else:
        uri, _database = graph_store.endpoint()
        level, code = DiagnosticLevel.warn, "GRAPH_INGEST_UNAVAILABLE"
        message = (f"检索材料没有入图：Neo4j（{uri}）"
                   + ("连不上，刚失败一次" if reason == GRAPH_SKIP_UNREACHABLE
                      else "还在熔断冷却里")
                   + f"，约 {max(1, round(graph_store.cooldown_remaining()))} 秒内不会再试"
                   "——这次的材料没进图，图谱骨架也就没有东西可读。")
    if not _graph_notice_once(code, reason):
        logger.info("检索材料入图跳过（%s，已报过，不再进诊断）：%s", reason, message)
        return None
    logger.warning("检索材料入图跳过（%s，首次即报）：%s", reason, message)
    return Diagnostic(level=level, code=code, message=message)


def _graph_outline_attempt(
    sources: Sequence[Source],
) -> tuple[str, int, str]:
    """第三档的骨架线索 + **为什么没触发**：`(块, 实体数, 跳过原因)`。

    三个返回值的分工：块进提示词，实体数进 `GRAPH_OUTLINE_USED` 那句诊断，
    跳过原因进 `_graph_outline_skip_diagnostic`。把原因带出来而不是在调用点重判一遍，
    是因为"闸在哪"只该有一处（重判迟早与这里分叉）。

    四道闸与 `_graph_outline` 的 docstring 一一对应，一个字节都不白花钱。
    """
    if not graph_outline_enabled() or not sources:
        return "", 0, GRAPH_SKIP_DISABLED
    if not graph_store.configured():
        return "", 0, GRAPH_SKIP_UNCONFIGURED
    if not graph_store.available():
        return "", 0, GRAPH_SKIP_COOLING
    scope = [source.id for source in sources]
    count = graph_store.scoped_entity_count(scope)
    if graph_store.cooldown_remaining() > 0:
        # 这一条查询把熔断点亮了 = 连不上。**必须与"图里没有这批材料"分开**：
        # `scoped_entity_count` 把两种情形都折成 0（读接口的降级契约），而它们的
        # 处置一个是"起服务"、一个是"材料太少"。熔断状态是唯一能分开它们的信号。
        return "", 0, GRAPH_SKIP_UNREACHABLE
    if count < GRAPH_OUTLINE_MIN_ENTITIES:
        return "", count, GRAPH_SKIP_TOO_FEW
    entities, links = graph_store.subgraph(
        entities=GRAPH_ENTITY_LIMIT, links=GRAPH_LINK_LIMIT, scope=scope)
    if len(entities) < GRAPH_OUTLINE_MIN_ENTITIES:
        # 计数够了、取回来不够：只可能是两次查询之间图被改了（建图中途）。按不够处理，
        # 并把取回的真实条数报上去——静默降级时的那个数字是唯一能查的证据。
        return "", len(entities), GRAPH_SKIP_TOO_FEW
    return GRAPH_OUTLINE_HEADER + "\n" + _graph_block(entities, links), len(entities), ""


def _graph_outline(
    sources: Sequence[Source],
) -> tuple[str, int]:
    """第三档的骨架线索：这批材料的图谱块（已带表头）；不可用时返回 `("", 0)`。

    第二个返回值是**为什么没触发/触发了多少**的那笔账：实体数。它不是调试信息，
    是诊断与测量共同要读的唯一数字（"这一档到底走没走"必须能从 `data` 里看出来，
    否则用户只会说"图谱好像没生效"）。

    四道闸按代价从低到高排，一个字节都不白花钱：

      0. `graph_outline_enabled()` 为假（`MINDMAP_GRAPH_OUTLINE=0`）→ 空。A/B 的
         那一组与"退回改动之前"都靠它；
      1. 没有材料 → 空。没材料时模型本来就该按主题写（`DETAIL_NO_SOURCE_RULES`）；
      2. `configured()` 为假（没配密码）→ 空，**不发起任何连接**。"这台机器不用图"
         是部署选择，不该为它每条请求付一次连接超时；
      3. `available()` 为假（刚连失败过、还在熔断冷却里）→ 空，同样不连；
      4. 这批材料的带边实体数 < `GRAPH_OUTLINE_MIN_ENTITIES` → 空。

    四道都过了才 `subgraph`（两条 Cypher + 一次类型回查，这是这条路上真正的花费）。

    **这一层只回答"有没有"，不回答"为什么没有"**：原因由
    `_graph_outline_attempt` 带出来（生成侧两个入口都读它）。这个两元组签名留着是
    因为测试与测量脚本按它断言（`test_graph_outline.py` 全文件），改签名会让一批
    只看"结果对不对"的用例被迫重写——而它们要验的东西与"为什么"无关。

    `scope` 必须传且必须与判据用的是同一份：图是跨材料共享的，不限定范围时"上一次
    那批报销材料"的实体会混进这一次的主题。第 4 道闸已经证明 scope 有货，所以
    `subgraph` 不会走到它自己的"回退全图"分支——**判据先行**在这里不是风格，
    它是"取回的实体一定属于本批材料"的唯一保证。
    """
    block, count, _reason = _graph_outline_attempt(sources)
    return block, count


# ------------------------------- 混合材料：有章节的走章节、没章节的走图谱，合成一套骨架
#
# ## 缺陷是什么
#
# 前三档（章节 / 清单 / 图谱）都是**整批材料一个结论**：`_outline_entries` 把全部
# 材料拼成一份目录再裁剪，`_graph_outline_attempt` 也只看"这批材料在图里有没有实体"。
# 于是一批材料里只要有**一部分**能抽出章节，另一部分的去处就没有人问。实测（库里
# 那 29 份材料，`scripts/measure_mixed_skeleton.py --probe`）：
#
#     只有 markdown（4 份 / 66,536 字）        30 条章节，覆盖 0.91
#     只有 paper（20 份 / 6,658 字）            0 条（没有标题）
#     markdown + paper 混（24 份 / 73,194 字）  30 条章节，覆盖 0.83 ← paper 那 20 份
#                                              一条都没有，骨架里没有它们的位置
#
# 同一条缺陷更早的一次观测：维基 1 份（9 条）+ 论文摘要 2 份（0 条）→ 9 条全来自
# 维基，论文那 2,393 字（占 24%）只在按支检索时被零星捞上来。**判据是"标题总共管住了
# 多少字"，不是"哪些材料有结构、哪些没有"。**
#
# ## 改法：分组，各自出骨架，再合并
#
# `_mixed_outline` 逐份判（判据与 `_outline_entries` 同源：目录非空且覆盖 ≥
# `OUTLINE_MIN_COVERAGE`），把这一批分成两组：
#
#     有章节   → 章节目录（`_merge_chapter_entries`，**每份至少留一条**）
#     没章节   → 图谱里的概念（`_graph_supplement_attempt`，scope **只给这一组**）
#     没章节且图里也没有 → 一段规则（`_mixed_plain_rules`），让模型按主题归位
#
# 两组都非空才走这条路；只有一组时走的是原来的那一档，一个字节都不变
# （`MINDMAP_MIXED_SKELETON=0` 是 A/B 与回滚用的开关）。
#
# ## 合并规则（顺序 / 总量 / 去重）——三条都对着实测定的
#
# ① **顺序：章节在前，图谱在后。** 章节是**作者自己划的结构**，本来就该是一级分支的
#    首选（`PLAN_PROMPT` 那句"材料自带章节时优先采用它们"就是这个意思）；图谱那一段
#    是"这几份没有结构，别忘了它们"，放在章节之后、条数要求之前，作用是把遗漏说出来
#    而不是与章节争夺一级。按材料原始顺序穿插的写法被否掉了：那样章节的"约占 X%"要
#    与抽出来的概念混编，模型分不清哪些是原文结构、哪些是抽取层的产物——两者的可信度
#    不同，混在一起就没法各自打折。
#
# ② **总量：两边各有硬上限，合并后不新造第三个数。** 章节 ≤ `OUTLINE_MAX_ITEMS`
#    （30，既有）+ 图谱 ≤ `GRAPH_SUPPLEMENT_MAX_ITEMS`（8）。合并后最多 38 条"方面"，
#    与"30 章 + 20 份摘要"仍是同一量级：它只影响 `sections_expected`（两级骨架的
#    判据），而那一头在 30 章时早就 > `SKELETON_GROUP_THRESHOLD`（10），**形状不变**。
#    真正会改变形状的是"少章节 + 多摘要"那种批（4 章 + 8 概念 = 12 > 10）：那确实
#    多了几块，该分组——而不是把概念藏起来、装作只有 4 块。
#
# ③ **去重：同一个说法只留一条，留下的是章节那一条。** 比折叠后的键（`_concept_key`：
#    去空白与标点、折小写），判等与互相包含（`碳中和` ⊂ `碳中和碳达峰`）都算重复。
#    留章节是因为它带 `start` / `coverage`——能回到原文的那一条，比抽出来的名字更可信。
#    图谱内部也去重：互相包含的只留更短（更概括）的，且**贪心按"还能覆盖几份材料"
#    选**——同一件事的几种说法不该各占一条分支（见 `_pick_supplement`）。
MIXED_SKELETON_ENV = "MINDMAP_MIXED_SKELETON"
"""混合材料分组的开关。默认**开**：它补的是"一部分材料被静默丢掉"，不是新能力。"""

GRAPH_SUPPLEMENT_MAX_ITEMS = 8
"""图谱补充档最多给几条概念。

数与 `GRAPH_OUTLINE_MIN_ENTITIES`（8）刻意相同，但含义不同：那一头是"够不够定义整副
骨架"，这一头是"补一个缺口的预算"。上限必须有——实测这 20 份无章节材料在图里有
**169 个带边实体**、`subgraph` 按度数取回 60 个（`scripts/measure_mixed_skeleton.py
--probe`），整段塞进提示词是 2,489 字，会把 30 条章节压成背景，也让模型在 60 个名字里
挑分支。8 条与"每条分支至少有一个能与别的概念相连的概念 + 请求一般 5–8 支"同一量级。
"""

GRAPH_SUPPLEMENT_HEADER = "这几份材料没有章节标题，但它们在图谱里有这些概念（每一份都要有分支指向）："
"""补充档的表头。与 `OUTLINE_HEADER` / `GRAPH_OUTLINE_HEADER` 都不同、且必须不同：
说成"材料自带的章节"时模型会去原文里找章（那几份没有），说成整批"没有章节标题"时
与上一段自相矛盾。`每一份都要有分支指向`是整个改动的判据，写在表头而不是只写在
`_mixed_plan_rules` 里：它是这一段的**用途**，读这一段时一定要看到。"""

def _mixed_plan_rules(items: int) -> str:
    """混合材料的 system 尾巴。三条与 `GRAPH_PLAN_RULES` 同构（都从"分支从哪来"出发），
    差别是这里**同时**有两类材料：`PLAN_PROMPT` 那句"目录为空或不完整时再自行归纳"
    在混合批上是错的（目录不为空，只是只覆盖了一部分材料），所以它必须被覆盖掉。

    **第二类的条数写成"至少 N 条"而不是"每一条概念都要有分支"**：后者在实测里是
    **不可能满足**的指令——8 个概念、请求 4–6 条分支，模型怎么排都缺，于是它整段
    无视（`data/ab-mixed-small.json`：B 组三次里有一次一个概念都没用上，分支名回到
    "城市与生态建设 / 低碳与可持续发展"这类通用词）。N 取
    `min(3, 概念条数)`：一次请求 5–8 条分支，让出 3 条给"没有章节的那几份"是可行的，
    而且它正好等于 `MIN_BRANCHES`——低于它那张图本来就不成立。
    """
    floor = min(3, max(1, items))
    return (
        "\n这次的材料**分成两类**，上面那两份清单就是它们：第一份是**自带章节**的材料"
        "（作者自己划的结构），第二份是**没有章节**的那几份在**图谱**里的概念。\n"
        "- 第一类的章节优先做一级分支，顺序照原文；条数不够时先合并互不重叠的小章，"
        "不要整章丢掉；\n"
        f"- 第二类要有**至少 {floor} 条**一级分支落在那些概念上：讲同一件事的几份材料"
        "合成一支，分支名就用材料里的原词；不要因为第一类占了条数就把第二类整批丢掉；\n"
        "- 两边都出现同一个说法时只留一条分支，不要列两条；\n"
        "- 这一步**只定一级分支**：不写子要点、不写 relations。\n"
    )


def mixed_skeleton_enabled() -> bool:
    """混合材料分组是否启用（默认 `True`，与 `MINDMAP_GRAPH_OUTLINE` 同一套写法）。"""
    return os.getenv(MIXED_SKELETON_ENV, "1").strip().lower() not in _GRAPH_OUTLINE_FALSY


def _mixed_plain_rules(count: int) -> str:
    """有章节 + 无章节、但图谱里没有那几份概念时补的那段规则。

    "留给模型归纳"不是什么都不说：那几份在图里连一个概念都没有时，骨架手上唯一的
    材料信息是清单里的文件名（`_material_inventory`），模型不会自己想到"没标题的
    那几份也要有位置"——它看到目录就认为材料讲的就是目录上那些事。这句话补的正是
    那一步，成本是一行字。
    """
    return (
        f"\n这次的材料**分成两类**：上面那份清单是自带章节的那几份；另有 {count} 份"
        "**没有章节标题**，图谱里也还没有它们的概念。\n"
        "- 章节优先做一级分支；\n"
        "- 没有章节的那几份按它们讲的事归进最接近的分支，讲的事与已有章节都不同的，"
        "单独给一条分支——**不要因为它们没有标题就不给它们位置**；\n"
        "- 这一步只定一级分支：不写子要点、不写 relations。\n"
    )


_CONCEPT_STRIP_RE = re.compile(r"[\s，。、；：！？·「」『』（）()【】《》〈〉\"'“”‘’—\-_/]+")


def _concept_key(title: str) -> str:
    """概念名的比较键：去空白与标点、折小写。

    与 `_skeleton_key`（只折空白）分开：那一处比的是**分支标题**（同一支写了两次），
    这里比的是"章节标题"与"抽取出来的概念名"——`碳中和（概念）`、`碳中和`、`碳中和 `
    是同一个东西，而它们来自两个完全不同的提取器。
    """
    return _CONCEPT_STRIP_RE.sub("", title or "").lower()


@dataclass(frozen=True)
class _OutlineSplit:
    """一批材料按"有没有可用章节"分成的两组，以及有章节那几份的章节目录。"""

    titled: tuple[Source, ...]
    plain: tuple[Source, ...]
    chapters: tuple[Any, ...]

    @property
    def mixed(self) -> bool:
        return bool(self.titled) and bool(self.plain)


@dataclass(frozen=True)
class _Supplement:
    """图谱补充档的结果：块、给了几条、覆盖了几份、以及"为什么没有"。"""

    block: str
    items: int
    covered: int
    plain: int
    entities: int
    skip: str


@dataclass(frozen=True)
class _MixedOutline:
    """混合材料这一次的骨架输入：章节 + 图谱补充 + 两组的份数。"""

    chapters: list[Any]
    supplement: _Supplement
    titled: int
    plain: int


def _merge_chapter_entries(pairs: Sequence[tuple[Source, list[Any]]]) -> list[Any]:
    """几份材料的章节目录 → 一份**每份都至少有一条**的合并目录（≤ `OUTLINE_MAX_ITEMS`）。

    为什么不直接用 `heading_outline`：它按 coverage 挑前 30 条，**挑的粒度是整批**
    ——一份 6 章的材料在同批的大文档面前可能一条都留不下，那与"无章节被丢掉"是同一个
    缺陷的另一种形状（实测纯 markdown 批没有触发：41 条裁到 30 条时四份各剩 8/4/11/7，
    `scripts/measure_mixed_skeleton.py --probe` 的读数）。所以这里的顺序是：

      1. **先占位**：每份材料 coverage 最大的那一章无条件入选；
      2. **去重**：跨材料的同名章节只留先出现的那一条（模型最后只会画一条分支，
         留两条等于白占目录额度）；
      3. **补满**：剩下的槽按 coverage 降序给（同 coverage 按材料顺序、再按原文顺序）；
      4. **交出去按材料顺序**：一级分支的先后要读得通，"1. 引言 2. 相关工作"一旦乱序
         就成了密码表（与 `_trim_outline` 的取舍方向一致）。

    覆盖数字（`coverage`）在合并后**只在同一份材料内部可比**，所以这里不重算、
    不改写——`_render_outline` 的百分比分母是整批字数，与原来同一口径。
    """
    if not pairs:
        return []
    slots = OUTLINE_MAX_ITEMS
    # ① 每份的第一条（它自己最大的那一章）；`-entry.start` 让同 coverage 时取靠前的。
    guaranteed: list[tuple[int, Any]] = []
    for index, (_source, entries) in enumerate(pairs):
        if not entries:
            continue
        best = max(entries, key=lambda entry: (entry.coverage, -entry.start))
        guaranteed.append((index, best))
    # ② 跨材料去重（同键只留先出现的那份）。
    seen: set[str] = set()
    picked: list[tuple[int, Any]] = []
    for index, entry in guaranteed:
        key = _concept_key(entry.title)
        if key in seen:
            continue
        seen.add(key)
        picked.append((index, entry))
    if len(picked) > slots:
        # 有章节的材料份数本身就超过上限时才发生（30 份以上）。按 coverage 留大的，
        # 这时"每份至少一条"与"不超过 OUTLINE_MAX_ITEMS"无法同时成立，取后者。
        picked = sorted(picked, key=lambda item: (-item[1].coverage, item[0]))[:slots]
    # ③ 补满：剩下的条目按 coverage 降序。
    taken = {(index, id(entry)) for index, entry in picked}
    pool = [
        (index, entry)
        for index, (_source, entries) in enumerate(pairs)
        for entry in entries
        if (index, id(entry)) not in taken and _concept_key(entry.title) not in seen
    ]
    pool.sort(key=lambda item: (-item[1].coverage, item[0], item[1].start))
    for item in pool:
        if len(picked) >= slots:
            break
        key = _concept_key(item[1].title)
        if key in seen:
            continue
        seen.add(key)
        picked.append(item)
    # ④ 交出去按材料顺序、再按原文顺序。
    picked.sort(key=lambda item: (item[0], item[1].start))
    return [entry for _index, entry in picked]


def _outline_split(sources: Sequence[Source]) -> _OutlineSplit:
    """逐份判"有没有可用章节"，顺带把有章节那几份的目录合出来。

    判据与 `_outline_entries` **逐字同源**：`heading_outline` 非空 + 覆盖率 ≥
    `OUTLINE_MIN_COVERAGE`。区别只在粒度——那边判的是整批（分母是全部材料的字数，
    一份 200 字的摘要就能把整批的覆盖率拉下来），这里判的是**每一份**。
    """
    materialized = list(sources)
    groups = heading_outline_by_source(materialized)
    pairs: list[tuple[Source, list[Any]]] = []
    plain: list[Source] = []
    for source, entries in zip(materialized, groups):
        if entries and outline_coverage([source], entries) >= OUTLINE_MIN_COVERAGE:
            pairs.append((source, entries))
        else:
            plain.append(source)
    return _OutlineSplit(
        titled=tuple(source for source, _entries in pairs),
        plain=tuple(plain),
        chapters=tuple(_merge_chapter_entries(pairs)),
    )


def _supplement_candidates(
    entities: Sequence[graph_store.GraphEntity], plain: Sequence[Source],
) -> list[tuple[graph_store.GraphEntity, tuple[str, ...]]]:
    """子图实体 → `(实体, 它逐字出现在哪几份材料里)`。

    **忠实度过滤在这里**：一个概念若在那几份材料的原文里找不到，它就不能当分支名
    ——`scripts/measure_graph_outline.py` 那一轮已经把这条量过：图谱里混着 `service.py`、
    `w1` 这类抽取层的名字，以及`全国`、`中国`这类放之四海的词。判据是**折叠后的
    子串**（`_concept_key`），不是分词：中文没有空格可切，而概念名恰好是短名词。

    单字概念直接丢（`OUTLINE_MIN_TITLE_CHARS` 同一条理由：一个字的"概念"是残片）。
    """
    texts = [(source.id, _concept_key(source.text or "")) for source in plain]
    rows: list[tuple[graph_store.GraphEntity, tuple[str, ...]]] = []
    for entity in entities:
        key = _concept_key(entity.name)
        if len(key) < OUTLINE_MIN_TITLE_CHARS:
            continue
        hits = tuple(source_id for source_id, text in texts if key and key in text)
        if not hits:
            continue
        rows.append((entity, hits))
    return rows


def _pick_supplement(
    rows: Sequence[tuple[graph_store.GraphEntity, tuple[str, ...]]],
    scope: Sequence[str], limit: int,
) -> tuple[list[tuple[graph_store.GraphEntity, tuple[str, ...]]], set[str]]:
    """挑哪几条概念进骨架：**贪心按"还能覆盖几份材料"**，返回 `(选中, 没覆盖到的)`。

    为什么不是"按度数取前 N"（`subgraph` 已经按度数排好了）：那 60 个实体的度数分布
    与"材料覆盖"不是一回事——`机器学习` 度数最高，但它出现在 8 份里，选它一次就
    把这 8 份一起覆盖；而度数第 9 的 `海绵城市建设试点` 只出现在 1 份里，按度数取 8
    条时它落选，那 1 份材料就没有分支指向它了。**这个改动的判据是材料覆盖，不是概念
    热度**，所以排序键也得是它。

    三条并列时的取舍（都为了可复现，不是随机）：
      - 新增覆盖一样多 → 取度数高的（它在材料里被反复连接，撑得起一级分支）；
      - 度数也一样 → 取更短的（`碳中和` 优于 `碳中和碳达峰路径`，更概括）；
      - 还一样 → 按名字字典序。

    两条停止条件：
      - 覆盖满了（没有未覆盖的材料）→ 停。同一件事的几种说法不该各占一条分支，
        这正是"每份材料一条分支"与"每件不同的事一条分支"的分界（§4 的自评）；
      - 剩下的候选都只覆盖已覆盖的材料 → 停（同上）。

    互相包含的候选（`碳中和` / `碳中和与碳达峰`）只留一条：先选中的那条赢，后面的
    直接跳过——它们是同一个概念的两种长度，画出来是两条几乎同名的分支。
    """
    uncovered = set(scope)
    remaining = list(rows)
    picked: list[tuple[graph_store.GraphEntity, tuple[str, ...]]] = []
    keys: list[str] = []
    while remaining and len(picked) < limit:
        ranked = sorted(remaining, key=lambda row: (
            -len(set(row[1]) & uncovered), -row[0].degree, len(row[0].name), row[0].name))
        entity, hits = ranked[0]
        remaining.remove(ranked[0])
        key = _concept_key(entity.name)
        if any(key in other or other in key for other in keys):
            continue
        if picked and not (set(hits) & uncovered):
            # 已经有分支覆盖了它那几份：再选一条只是同义重复。
            continue
        picked.append((entity, hits))
        keys.append(key)
        uncovered -= set(hits)
        if not uncovered:
            break
    return picked, uncovered


def _graph_supplement_attempt(
    plain: Sequence[Source], *, chapter_titles: Sequence[str] = (),
) -> _Supplement:
    """无章节那几份的**补充骨架**：图谱概念块 + 它覆盖了几份 + 没覆盖到的几份。

    四道闸与 `_graph_outline_attempt` 同构（开关 / 没配密码 / 熔断 / 图里没这批材料），
    差别只有三处，都是"补充档"这个定位决定的：

      - **scope 只给无章节的那几份**：图是跨材料共享的，把有章节那几份的实体也拉进来，
        补出来的分支会与章节重复，而那正是 `_pick_supplement` 的第 ③ 条要去重的东西
        ——在源头收窄比事后去重便宜，也不会挤掉真正的补充名额；
      - **不要求 `GRAPH_OUTLINE_MIN_ENTITIES`（8）**：那个阈值是"图里的概念够不够撑起
        **整副**骨架"（3 条以上互不重叠的分支）。补充档只需要"这几份里有概念可指"，
        一条概念也比零条好；质量由两道筛子守——逐字出现（`_supplement_candidates`）
        与覆盖优先（`_pick_supplement`），而不由数量守；
      - **`chapter_titles` 里的说法一律不进**：同一个概念两栏各列一遍，模型会画两条
        几乎同名的分支（章节那一条带 `start`/`coverage`、能回到原文，比抽出来的名字
        可信，所以留章节那一条）。
    """
    total = len(plain)
    if not plain:
        return _Supplement("", 0, 0, 0, 0, GRAPH_SKIP_DISABLED)
    if not graph_outline_enabled():
        return _Supplement("", 0, 0, total, 0, GRAPH_SKIP_DISABLED)
    if not graph_store.configured():
        return _Supplement("", 0, 0, total, 0, GRAPH_SKIP_UNCONFIGURED)
    if not graph_store.available():
        return _Supplement("", 0, 0, total, 0, GRAPH_SKIP_COOLING)
    scope = [source.id for source in plain]
    count = graph_store.scoped_entity_count(scope)
    if graph_store.cooldown_remaining() > 0:
        return _Supplement("", 0, 0, total, 0, GRAPH_SKIP_UNREACHABLE)
    if count <= 0:
        return _Supplement("", 0, 0, total, 0, GRAPH_SKIP_TOO_FEW)
    # `links=1`：补充档只用概念名，边不进提示词（那一段是章节的补充，不是一张图）。
    entities, _links = graph_store.subgraph(
        entities=GRAPH_ENTITY_LIMIT, links=1, scope=scope)
    rows = _supplement_candidates(entities, plain)
    if chapter_titles:
        blocked = [_concept_key(title) for title in chapter_titles]
        blocked = [key for key in blocked if key]
        rows = [
            row for row in rows
            if not any(
                _concept_key(row[0].name) in key or key in _concept_key(row[0].name)
                for key in blocked)
        ]
    picked, uncovered = _pick_supplement(rows, scope, GRAPH_SUPPLEMENT_MAX_ITEMS)
    if not picked:
        return _Supplement("", 0, 0, total, len(entities), GRAPH_SKIP_TOO_FEW)
    lines = [GRAPH_SUPPLEMENT_HEADER]
    for index, (entity, _hits) in enumerate(picked, start=1):
        kind = f"（{entity.type}）" if entity.type else ""
        lines.append(f"{index}. {entity.name}{kind}")
    return _Supplement(
        block="\n".join(lines), items=len(picked),
        covered=total - len(uncovered), plain=total, entities=len(entities), skip="",
    )


def _mixed_outline(sources: Sequence[Source]) -> _MixedOutline | None:
    """混合材料的骨架输入；**不该走这条路时返回 `None`**（调用方退回原来那一档）。

    返回 `None` 的三种情形，每一条都对应"这条路不该插手"：
      - 开关关着（`MINDMAP_MIXED_SKELETON=0`，A/B 与回滚）；
      - 没有材料；
      - 只有一组（全都有章节，或全都没有）。全都没有那一组由第三档
        （`_graph_outline_attempt`）负责，且它看的是**整批** scope——那是对的，
        整批都没章节时不存在"谁的骨架被谁挤掉"。
    """
    if not mixed_skeleton_enabled() or not sources:
        return None
    split = _outline_split(sources)
    if not split.mixed:
        return None
    return _MixedOutline(
        chapters=list(split.chapters),
        supplement=_graph_supplement_attempt(
            split.plain,
            chapter_titles=[entry.title for entry in split.chapters]),
        titled=len(split.titled),
        plain=len(split.plain),
    )


def _mixed_diagnostic(mixed: _MixedOutline) -> Diagnostic | None:
    """混合骨架这一次的账：用了图谱 / 为什么没用（一句话，各一种处置）。

    没章节的那几份在图里**没有**概念时同样要报——那不是故障（材料还没入图），但
    "这几份这次没有分支指向它们"必须能从 diagnostics 里看出来，否则这个改动就变成
    了另一种静默（与 `GRAPH_SKIP_*` 那一族的理由逐字相同）。
    """
    supplement = mixed.supplement
    if supplement.block:
        return Diagnostic(
            level=DiagnosticLevel.info, code="GRAPH_OUTLINE_USED",
            message=(
                f"材料分两类：{mixed.titled} 份自带章节、{mixed.plain} 份没有。"
                f"后面这几份在图谱里取了 {supplement.items} 个概念进骨架，"
                f"覆盖其中 {supplement.covered}/{supplement.plain} 份的原文用词。"),
        )
    return _graph_outline_skip_diagnostic(
        supplement.skip, supplement.entities, mixed=mixed.plain)


def _count_dict_nodes(nodes: list[dict[str, Any]]) -> int:
    """还没过契约校验的裸 dict 树里有多少个节点（含它们自己）。"""
    return sum(1 + _count_dict_nodes(node.get("children") or []) for node in nodes)


def _slice_nodes(
    raw: Any, *, prefix: str, budget: int, depth: int,
) -> list[dict[str, Any]]:
    """模型返回的 `children` → 组装用的节点列表（id 由这里给，label 由这里裁）。

    **宽容解析**（与 `_skeleton_branches` 收两种形状同一条理由）：模型可能给
    `{"children":[...]}`、`{"points":[...]}`，也可能直接给一个数组。三样都收——
    形状不对时把它整支判废，代价是这一章白花一次调用，而收下来的信息是好的。

    **id 在这里生成**（`{prefix}-1-2` 这样的路径编号）：模型写 id 会让 12 支各写一遍
    `n1`、`n2`，撞号之后 `MindMapContent` 的 id 唯一性校验会把整棵树判废。确定性
    编号同时让"重跑一次"可复现。

    **两个上限都在这里落**：`depth`（分支下还能有几层）与 `budget`（含分支自己的
    节点数）。超预算的节点按前序丢掉——与 `_clip_tree` 的方向一致，只是发生在**每一
    支内部**，所以不会出现"整支被砍掉"。
    """
    if isinstance(raw, dict):
        children = raw.get("children")
        if not isinstance(children, list):
            children = raw.get("points")
    elif isinstance(raw, list):
        children = raw
    else:
        children = None
    if not isinstance(children, list):
        return []
    remaining = [max(0, budget - 1)]

    def walk(items: list[Any], path: tuple[int, ...], level: int) -> list[dict[str, Any]]:
        built: list[dict[str, Any]] = []
        for item in items:
            if remaining[0] <= 0 or level > depth:
                break
            if isinstance(item, str):
                label, refs, nested = item, [], []
            elif isinstance(item, dict):
                label = item.get("label")
                refs = item.get("source_refs")
                nested = item.get("children") if isinstance(item.get("children"), list) else []
            else:
                continue
            title = clip_label(label.strip()) if isinstance(label, str) else ""
            if not title:
                continue
            remaining[0] -= 1
            index = len(built) + 1
            node = {
                "id": f"{prefix}-{'-'.join(str(part) for part in (*path, index))}",
                "label": title,
                "children": walk(nested, (*path, index), level + 1),
            }
            # **这一轮不写 detail**：组装时一律留空，第三段统一补（与
            # `_SKELETON_BODY` 的约定同一条）。模型偶尔仍会写，直接丢掉——
            # 保留它会让"结构那一轮写了 30 字"混进 detail 统计，那是另一类问题。
            if isinstance(refs, list) and refs:
                node["source_refs"] = refs
            built.append(node)
        return built

    return walk(children, (), 1)


def _locate_in_chapter(
    text: str, quote: str, window: tuple[int, int],
) -> tuple[str, tuple[int, int] | None]:
    """在**这一章的范围里**定位引文：`("unique", (start,end))` / `("ambiguous", None)`
    / `("missing", None)`。

    与 `optimization.source_refs._locate_quote` 是同一个判据，只多一条范围限制——
    这正是"start / end **从这一章里算**"的实现：模型只抄 quote，偏移由服务端在
    `[章首, 章末)` 内数出来。让模型自己数偏移是行不通的（`_full_text_sections` 的
    注释里已经踩过一次：一万字进 prompt 之后它的坐标感不可靠）。

    多处命中不猜：猜一个坐标比没有坐标更糟（`_locate_quote` 的原话）。这时把 quote
    留着、不带偏移，下游的逐字校验会把它标成「待核」并保留。
    """
    if not quote:
        return "missing", None
    low, high = window
    positions: list[int] = []
    position = text.find(quote, low)
    while 0 <= position and position + len(quote) <= high:
        positions.append(position)
        position = text.find(quote, position + 1)
    if len(positions) == 1:
        return "unique", (positions[0], positions[0] + len(quote))
    return ("ambiguous", None) if positions else ("missing", None)


def _slice_refs(
    nodes: list[dict[str, Any]], chapter: _Chapter, text: str, report: dict[str, Any],
) -> None:
    """把每支子树的 `source_refs` 落到本章坐标上（就地改）。

    三件事，都只在这一处做：

      - `source_id` **强制**成这一章的来源：模型只看到一个来源，写错就是笔误，纠正它
        比丢一条真引用好；
      - `quote` 截到 `SECTION_SLICE_QUOTE_CHARS` 以内（与提示词里那句"不超过 300 字"同源，
        模型写长了不该整条作废）；
      - 偏移由 `_locate_in_chapter` 在本章范围内数出来：唯一就写 `start/end`，
        多处就只留 quote，找不到就**丢掉这条引用**（节点保留）并记数。

    最后那一条是这条路的机制保证：引用只可能落在它自己那一章里，所以"每章覆盖率"
    这个指标不会被跨章引用污染。
    """
    window = (chapter.start, chapter.end)
    stack = list(nodes)
    while stack:
        node = stack.pop()
        refs = node.get("source_refs")
        if isinstance(refs, list):
            kept: list[dict[str, Any]] = []
            for ref in refs:
                quote = ref.get("quote") if isinstance(ref, dict) else None
                if not isinstance(quote, str) or not quote.strip():
                    report["refs_dropped"] += 1
                    continue
                quote = quote.strip()[:SECTION_SLICE_QUOTE_CHARS]
                outcome, fix = _locate_in_chapter(text, quote, window)
                if outcome == "missing":
                    report["refs_dropped"] += 1
                    continue
                entry: dict[str, Any] = {"source_id": chapter.source_id, "quote": quote}
                if fix is not None:
                    entry["start"], entry["end"] = fix
                    report["refs_located"] += 1
                else:
                    report["refs_ambiguous"] += 1
                kept.append(entry)
            if kept:
                node["source_refs"] = kept
            else:
                node.pop("source_refs", None)
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(children)


async def _slice_one(
    chapter: _Chapter, *, topic: str, budget: int, ctx: RunContext,
    text: str, report: dict[str, Any],
    parent: tuple[int, ...] | None = None,
    seen: set[tuple[tuple[int, ...] | None, str]] | None = None,
) -> list[dict[str, Any]]:
    """一章一次调用：发出去、解析、把引用落到本章坐标上。失败时返回空列表。

    失败**不抛**（与第三段同一条原则）：这一章写不出来时，它的一级分支照样在树上
    （只是一个还没有要点的分支），别的章一个字都不受影响。整条路因为第 7 章失败而
    交出半张图，比"这一章空着"糟得多。

    接的**不只有 `GenerationError`**：这条路把一次调用换成了 N 次，任何一次抛个别的
    异常（httpx 超时、解析器意外的 TypeError）都会被 `gather` 原样抛出去，把另外
    N-1 次已经付过钱的产出一起丢掉。所以这里兜 `Exception` 并记下类型——与
    `_settle_halves` 在第三段做的同一件事（那里也只兜住非取消的那一半）。

    **这一章的结构也走增量上报**（`on_delta` → `_GrowStream`）。这条路是整树生成
    唯一不切片的旁路：`_generate` 的"一次写完"那支早在 `stream_kwargs` 里接过
    通道，切章之后它根本不会被执行，于是通篇一个 `node_grow` 帧都没有——界面在
    几十秒里一片空白，然后一次性落 73 个节点（实测 12 章 × 每支 6 个 + 根）。
    `parent` 是**这一支在根下面的索引链**（`(章的位置,)`），由 `_slice_structure`
    按 `aligned` 的顺序给出：那个顺序就是最终树里根的孩子顺序。
    """
    if not ctx.reserve_model_call():
        report["skipped"] += 1
        return []
    ctx.extra_model_calls += 1
    ask = _slice_ask(topic, chapter, budget=budget, depth=SECTION_SLICE_DEPTH)
    system = SECTION_SLICE_PROMPT + SECTION_SLICE_REF_RULES
    # 通道为 None 时连 `on_delta` 都不传：这条路在加生长事件之前的行为是逐字不变的
    # （`_GrowStream` 根本不会被创建，与 `_generate` 那次判断同一条约定）。
    stream_kwargs: dict[str, Any] = {}
    if ctx.emit is not None and parent is not None:
        # 预览的上限与 `_slice_nodes` 的裁剪**同一个数**：`budget - 1` 是这支最多
        # 能写几个后代（`remaining = [budget - 1]`），`SECTION_SLICE_DEPTH` 是分支
        # 下最多几层（`level > depth` 就停）。两把尺子不一致时，界面会看到节点
        # "长出来又消失"——而消失的那些从来没有进过树。
        stream = _GrowStream(
            ctx, seen if seen is not None else set(), parent,
            quota=max(0, budget - 1), max_level=SECTION_SLICE_DEPTH,
        )
        stream_kwargs["on_delta"] = stream.feed
    try:
        raw = await _chat(ctx, system, ask, max_tokens=_max_tokens(ctx), **stream_kwargs)
    except Exception as exc:  # noqa: BLE001 — 失败的原因要回给人看，见 docstring
        report["failed"].append({"chapter": chapter.index, "title": chapter.title,
                                 "reason": _reason_text(exc)})
        return []
    nodes = _slice_nodes(
        raw, prefix=f"s{chapter.index}", budget=budget, depth=SECTION_SLICE_DEPTH)
    _slice_refs(nodes, chapter, text, report)
    return nodes


def _align_chapters(
    plan: _SkeletonPlan, chapters: Sequence[_Chapter],
) -> list[tuple[_Chapter, _SkeletonBranch | None]]:
    """把骨架的分支与材料的章对起来：`[(章, 骨架分支或 None)]`，**按章的顺序**返回。

    对齐规则只有一条：**按标题匹配优先，序号兜底**（与 `_apply_skeleton` 套颜色时
    同一条判据——那里也是 label 优先、序号兜底）。三条边界各自有归宿：

      - 骨架给了 12 支、材料 12 章：名字能对上就按名字，对不上按序号（模型可能真的
        换了顺序，也可能只是改了一个字）；
      - 骨架给的比章少：缺的那几章补出来（`branch=None`，只少一个颜色/图标，
        内容照写）——**宁可多一支，也不要让某一章没有支**，那正是"每章覆盖率"要防的；
      - 骨架给的比章多：多出来的支没有章可给，丢弃并让调用方记一条诊断。给一支
        无内容的空分支比丢掉它更糟：画布上会多一个点不开的节点。
    """
    by_key = {_skeleton_key(branch.label): branch for branch in plan.flat}
    used: set[str] = set()
    aligned: list[tuple[_Chapter, _SkeletonBranch | None]] = []
    ordered = list(plan.flat)
    for index, chapter in enumerate(chapters):
        key = _skeleton_key(chapter.title)
        branch = by_key.get(key)
        if branch is not None and key not in used:
            used.add(key)
        elif index < len(ordered) and _skeleton_key(ordered[index].label) not in used:
            branch = ordered[index]
            used.add(_skeleton_key(branch.label))
        else:
            branch = None
        aligned.append((chapter, branch))
    return aligned


async def _slice_structure(
    *, topic: str, chapters: Sequence[_Chapter], plan: _SkeletonPlan,
    ctx: RunContext, text: str, diagnostics: list[Diagnostic],
) -> tuple[Any, dict[str, Any]]:
    """第二段的替代实现：**每章一次调用**，返回组装好的整树 `raw` 与这一段的账。

    返回的形状（`{"title":…, "root":{…}}`）与"一次写完"那条路**完全一样**，所以
    它下面的收尾（校验 / 套色 / 裁剪 / 第三段 / 重复检测）一行都不用改。

    开事件通道时，这一段自己负责"节点一个个长出来"：根与 N 条一级分支先报（顺序
    就是 `aligned`），随后每章的结构调用边收增量边报孩子（见 `_slice_one`）。这条
    路的存在恰恰是"整树生成没有流式输出"的成因——它绕开了 `_generate` 里那次带
    `on_delta` 的结构调用，所以接在这两个地方，而不是在那次调用上。
    """
    aligned = _align_chapters(plan, chapters)
    if len(plan.flat) > len(chapters):
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.info, code="SECTION_SLICE_BRANCH_DROPPED",
            message=(
                f"骨架给了 {len(plan.flat)} 条一级分支，材料只有 {len(chapters)} 章，"
                f"多出的 {len(plan.flat) - len(chapters)} 条没有原文可写，已丢弃"
            ),
        ))
    covered = sum(chapter.chars for chapter in chapters)
    if covered < len(text):
        # 目录被 `_trim_outline` 裁过（`OUTLINE_MAX_ITEMS = 30`，40 章的材料会撞上），
        # 于是有一截正文不属于任何一章。**不阻断**（切出来的章仍然正确、互不相交），
        # 但要说出来：那一段的覆盖率恒为 0，不说明的话它就是"模型没写"。
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.info, code="SECTION_SLICE_PARTIAL_COVERAGE",
            message=(
                f"目录只切出 {len(chapters)} 章、覆盖 {covered}/{len(text)} 字"
                f"（目录上限 {OUTLINE_MAX_ITEMS} 条），剩下 {len(text) - covered} 字"
                "不属于任何一章，这部分不会有节点引用它"
            ),
        ))
    budget = _slice_budget(len(chapters))
    report: dict[str, Any] = {
        "chapters": len(chapters),
        "per_branch_nodes": budget,
        "depth": SECTION_SLICE_DEPTH,
        "truncated": [chapter.index for chapter in chapters if chapter.truncated],
        "failed": [],
        "skipped": 0,
        "nodes": 1,                 # 根节点
        "refs_located": 0,
        "refs_ambiguous": 0,
        "refs_dropped": 0,
    }
    started = time.perf_counter()
    await _emit_stage(
        ctx, SECTION_SLICE_STAGE,
        f"正在按 {len(chapters)} 章分别写结构（每次只给它这一章的原文）…")
    limit = asyncio.Semaphore(SECTION_SLICE_CONCURRENCY)

    # ---- 预览的骨架：根与一级分支先立起来 --------------------------------------
    #
    # 必须在 `gather` **之前**、而且按 `aligned` 的顺序发：各章的孩子报出来的
    # `path` 是"父节点的索引链"，父就是这里的一级分支；分支帧没到，那些孩子在前端
    # 只能进暂存（`mindmap.ts` 的 `queuePreviewNode`），挂不上的永远不显示。
    #
    # 根帧 `path=None`、一级分支 `path=()`：两者不能用同一个值，否则前端分不清
    # 中心主题与第一个分支（`_emit_grow` 的 docstring 记着同一条）。
    #
    # 通道为 None 时这一段整体不发生，工具行为与加预览之前逐字相同。
    grow_seen: set[tuple[tuple[int, ...] | None, str]] = set()
    if ctx.emit is not None:
        await _emit_grow(ctx, grow_seen, None, topic, None)
        for chapter, _ in aligned:
            await _emit_grow(ctx, grow_seen, (), chapter.title, None)

    async def run(position: int, chapter: _Chapter) -> list[dict[str, Any]]:
        async with limit:
            return await _slice_one(
                chapter, topic=topic, budget=budget, ctx=ctx, text=text, report=report,
                parent=(position,), seen=grow_seen)

    subtrees = await asyncio.gather(
        *(run(position, chapter) for position, (chapter, _) in enumerate(aligned)))
    report["seconds"] = round(time.perf_counter() - started, 3)
    report["calls"] = len(chapters) - report["skipped"]
    # 结束帧与开始帧配对（`_emit_stage` 的既有约定，第三段与外部检索都是这么做的）：
    # 只在开始那一帧，界面会把"正在按 12 章分别写结构"一直挂到第三段开始。
    await _emit_stage(
        ctx, SECTION_SLICE_STAGE,
        f"已写完 {len(subtrees) - len(report['failed']) - report['skipped']}"
        f"/{len(chapters)} 章的子树", count=report["calls"])

    children: list[dict[str, Any]] = []
    for (chapter, branch), subtree in zip(aligned, subtrees):
        node: dict[str, Any] = {
            "id": f"s{chapter.index}",
            "label": clip_label(chapter.title),
            "children": subtree or [],
        }
        # 颜色/图标照骨架那一份给：`_apply_skeleton` 后面还会按 label / 序号套一次，
        # 但那时它读的是**落树后的一级分支顺序**，而这里正是那个顺序的来源。
        if branch is not None and branch.color is not None:
            node["color"] = branch.color
        children.append(node)
    # 这一段的节点数是**组装后的真实数**（含每支的两层后代），不是各支顶层要点之和
    # ——后者会少算一层，读 `data.section_slice.nodes` 的人会以为这条路只写了三十几个。
    report["nodes"] = 1 + sum(1 + _count_dict_nodes(child["children"]) for child in children)
    if report["failed"]:
        failed = report["failed"]
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="SECTION_SLICE_CALL_FAILED",
            message=(
                f"{len(failed)} 章的子树没有写出来（"
                + "、".join(f"第 {item['chapter']} 章" for item in failed[:5])
                + ("…" if len(failed) > 5 else "")
                + "）：这些章的一级分支仍在树上，只是还没有要点。原因："
                + failed[0]["reason"]
            ),
        ))
    if report["skipped"]:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="SECTION_SLICE_NO_BUDGET",
            message=(
                f"模型调用额度不够：{report['skipped']} 章的结构没有生成请求，"
                "这些章的一级分支仍是空的"
            ),
        ))
    if report["refs_dropped"]:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.info, code="SECTION_SLICE_REF_DROPPED",
            message=(
                f"{report['refs_dropped']} 条引用写的内容不在它自己那一章的原文里，"
                "已丢弃（引用必须落在本章内，否则「每章覆盖率」就没有意义）；"
                f"另有 {report['refs_ambiguous']} 条在本章内出现多次，只保留了引文"
            ),
        ))
    raw = {
        "title": topic,
        "diagram_type": "tree",
        "root": {
            "id": "root",
            "label": topic,
            "children": children,
        },
    }
    return raw, report


# ---------------------------------------------------------------- 生成之后的核对
#
# 这一步补的是整条路上唯一一次"回头看"：**在此之前没有任何一步问过'这棵树是不是
# 真的'**。骨架凭主题猜、结构凭主题写、说明凭主题写，而 `DETAIL_NO_SOURCE_RULES`
# 又明令不许写年份、人名、论文标题与数字统计——于是产物是一大棵既没有具体内容、
# 也无法核对的树。材料那条路有逐字校验，但它管的是"引文出处在不在原文里"，不是
# "这个节点说的对不对"；纯主题那条路连前者都没有。
#
# 形态与代价（判据见 `app/optimization/verify.py` 与 `docs/GENERATION-VERIFY.md`）：
# `标题 + 一级分支`拼一个 query、**搜一次**、拿到 3–5 篇正文、让模型逐节点判三态。
# 逐节点各搜一次是它的 40 倍成本（40 节点 × 0.9 秒 vs 0.9 秒），所以在"这个便宜版本
# 够不够用"这件事有数字之前不先做贵的那个。
#
# 三种状态的处置**只有一种是对的**：
#
#     supported     留下，挂引用（quote 是搜到正文里的原始切片，过 `verify_source_refs`）
#     contradicted  留下但标出来，让用户看
#     unverified    保留，标 unverified
#
# "未证实"是个状态，不是待删项：删是不可逆的，而"小众但正确"与"模型编的"在"搜不到"
# 这个结果上长得一模一样。
VERIFY_STAGE_DETAIL = "正在用一次联网搜索核对这棵树…"

# `ContentBase.source_usage` 的 `max_length`。与契约同值：多写一条就是 422，而报错
# 指向的是"来源太多"，与真正的原因（这一步并了几篇进来）隔了三层。
_SOURCE_USAGE_MAX = 50

# 核对探针一次能带几个节点。与 `Node.children` 的上限同值（契约的
# `MAX_NODE_CHILDREN`）：探针是"所有挂了引用的节点"的一次性副本，一棵 80 节点的树
# 轻松超过 50 条，不分批就是一次 `ValidationError`（实测 58 条就崩）。
_PROBE_CHILDREN = 50


async def _verify_after_generate(
    content: MindMapContent, *, ctx: RunContext, sources: Sequence[Source],
    diagnostics: list[Diagnostic],
) -> dict[str, Any] | None:
    """跑一次核对，把三态放进 `data["verify"]`。**开关关着时一次都不进来。**

    五条边界，每条都对应一种"这一步把生成搞坏了"的失败：

      1. **额度**：判定是一次模型调用，`reserve_model_call()` 拿不到就整段跳过并记
         一条诊断——核对是生成之后多出来的一步，它没有资格把已经成型的树拖成失败；
      2. **搜索失败 = 空报告**，不是失败（`verify.search_articles` 自己吞异常）；
      3. **引用只在 95% 逐字命中时才挂**（`attach_refs` 用 `locate_quote` 拿原文
         切片）：挂一条对不上原文的引用，比不挂更糟——`verify_source_refs` 会把它
         剔掉，而"剔掉"在用户那里与"这里没有依据"长得一样；
      4. **不碰已经有引用的节点**：那是材料那一步的产物，两件事的可信度不同；
      5. **不改 `Node` 契约**：三态进 `data`（`Node` 是 `extra="forbid"`，加字段要
         重导 `openapi.json` 与 `schema.d.ts`）。

    `sources` 是本次运行**可校验的来源**（`_verify_sources(ctx)` 的同一份）：新抓回来
    的那几篇会先入库、再并进 `ctx`，否则刚挂上的引用在收尾校验里会因为"来源不存在"
    被静默剔掉——检索成功、引用写出、最后消失，是这条链上最隐蔽的一种失败。
    """
    from app.optimization import verify as verify_step
    from app.knowledge import library as knowledge_library

    if not ctx.reserve_model_call():
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.info, code="VERIFY_SKIPPED_NO_BUDGET",
            message=("模型调用额度已用完，这次没有做「生成之后用一次搜索核对」；"
                     "树本身不受影响"),
        ))
        return None
    await _emit_stage(ctx, EXTERNAL_SEARCH_STAGE, VERIFY_STAGE_DETAIL)

    # 一次检索：query 由 `verify.build_query` 拼（标题 + 一级分支，去重截断）。
    query = verify_step.build_query(content)
    search_started = time.perf_counter()
    try:
        hits = await asyncio.to_thread(verify_step.search_articles, query)
    except Exception as exc:  # noqa: BLE001 — 见 docstring 第 2 条
        logger.warning("核对检索失败（%r）：%s: %s",
                       query[:40], type(exc).__name__, str(exc)[:160])
        hits = []
    # 检索在这一层发出去，所以耗时也在这一层量：`verify_tree` 收下现成的正文时不自己
    # 计时（它只知道"没搜"），"这一步让用户多等了几秒"就成了 0——而这条链上唯一的
    # 成本问题就是那几秒。
    search_seconds = time.perf_counter() - search_started
    await _emit_stage(ctx, EXTERNAL_SEARCH_STAGE, f"核对检索拿到 {len(hits)} 篇正文",
                      count=len(hits))

    built: list[Source] = []
    for hit in hits:
        try:
            source = _external_source(hit)
            source = source.model_copy(update={"id": knowledge_library.ingest(source)})
        except Exception as exc:  # noqa: BLE001 — 单条入库失败不废整次核对
            logger.warning("核对来源入库失败（%s / %s）：%s: %s",
                           hit.source_name, hit.title[:40],
                           type(exc).__name__, str(exc)[:160])
            continue
        built.append(source)
    if built:
        ctx.record_library_sources(built)
        _note_verify_sources(content, built)

    async def chat(system: str, user: str, **kwargs: Any) -> Any:
        # 走 `_chat` 而不是 `chat_async`：`deep_thinking` 从那一个出口取
        # （`tests/test_model_exit_single_path.py` 钉着这条，别在这里直连）。
        return await _chat(ctx, system, user, **kwargs)

    report = await verify_step.verify_tree(
        content, articles=hits, chat=chat, max_tokens=_max_tokens(ctx))
    report.search_seconds = search_seconds
    report.attached = verify_step.attach_refs(content, report, built)
    if report.attached:
        _drop_unverifiable_refs(content, report, built, diagnostics)
    diagnostics.append(Diagnostic(
        level=(DiagnosticLevel.warn if report.count(verify_step.CONTRADICTED)
               else DiagnosticLevel.info),
        code="GENERATION_VERIFIED",
        message=verify_step.summarize(report, built),
    ))
    return report.as_dict()


def _note_verify_sources(content: MindMapContent, sources: Sequence[Source]) -> None:
    """把核对抓回来的那几篇并进 `content.source_usage`。

    **必须做，不是可选的整理**：`MindMapContent` 的契约校验要求每条 `source_refs` 的
    `source_id` 都在 `source_usage` 里——不并进去，下一步挂引用会直接抛
    `ValidationError`，而那句报错指向"节点引用的来源不在 source_usage 中"，看起来
    像生成那边写歪了。

    顺带它也解决界面上那件事：保存的图**不带** `Source`，重新打开时只有这份摘要——
    引用要能点回原文，靠的就是这里的 `url`。
    """
    known = {item.source_id for item in content.source_usage}
    allowance = _SOURCE_USAGE_MAX - len(content.source_usage)
    for source in sources:
        if allowance <= 0:
            break
        text = (source.text or "").strip()
        if source.id in known or not text:
            continue
        content.source_usage.append(SourceUsage(
            source_id=source.id, kind=source.kind, filename=source.filename,
            char_count=len(text), url=source.metadata.url))
        allowance -= 1


def _drop_unverifiable_refs(
    content: MindMapContent, report: Any, sources: Sequence[Source],
    diagnostics: list[Diagnostic],
) -> None:
    """核对挂上的引用逐条过一遍 `verify_source_refs`，不合格的摘掉。

    判据用的是**现成的那一份实现**，不另写一套：`quote` 已经取自原文切片，正常情况下
    一条都不会被摘——但"正常情况下"不是理由，`SOURCE_QUOTE_AMBIGUOUS`（同一句话在
    正文里出现多次）与它将来可能加的规则都会在这里生效，而重写一份校验只会多一处
    迟早分叉的真相。

    只在**一棵探针树**上跑：直接对整棵树跑会给每个没有引用的节点各记一条
    `UNSOURCED_NODE`（几十上百条），把真正要看的诊断淹掉。探针树带着同一份
    `source_usage`——契约要求引用指向已登记的来源，少带一条就是一次
    `ValidationError`。
    """
    from app.contracts.mindmap import Node as _Node

    # **只碰这一步亲手挂上的引用**（`report.attached_nodes`）。把树上原有的引用一起
    # 塞进来会做两件不该做的事：重新判定一遍生成那一步已经校验过的引用，以及——
    # 更糟——用 `built`（这次抓回来的那几篇）当来源表，原有的引用会因此在探针里
    # 校验失败、被当成"引文对不上"删掉。实测就是这么崩的（58 条引用一次建探针）。
    mine = set(report.attached_nodes)
    attached = [node for node in _walk_tree(content.root)
                if node.id in mine and node.source_refs]
    if not attached:
        return
    registered = {source.id for source in sources}
    usage = [item.model_copy() for item in content.source_usage
             if item.source_id in registered]
    kept: set[str] = set()
    # 探针分批发：`Node.children` 有上限（契约的 `MAX_NODE_CHILDREN`），一棵 80 节点的
    # 树挂上四五十条引用时一次塞进去会直接 `ValidationError`——而那句报错说的是
    # "children 太多"，与本意（校验引用）隔了两层。实测：58 条引用一次建探针就崩。
    for start in range(0, len(attached), _PROBE_CHILDREN):
        batch = attached[start:start + _PROBE_CHILDREN]
        probe = MindMapContent(
            title=content.title, source_usage=usage,
            root=_Node(id="verify-probe", label="核对探针", children=[
                _Node(id=node.id, label=node.label, source_refs=list(node.source_refs))
                for node in batch]),
        )
        verify_source_refs(probe, list(sources), remove_invalid=True)
        kept |= {child.id for child in probe.root.children if child.source_refs}
    dropped = [node for node in attached if node.id not in kept]
    for node in dropped:
        node.source_refs = []
    report.dropped = len(dropped)


async def _generate(args: GenerateArgs, ctx: RunContext) -> ToolResult:
    """整树生成：**骨架一次、证据一份、结构一次（或按章 N 次）、说明一到两次，
    超上限折叠、可选再核对一次**。

    接线（前两段与 `_generate_deep` 完全共用，后几段是这条路独有的）：

      1. 骨架：`_intake` 定材料 → **三档并列**定一级分支（`_plan_ask`）：
           ① 章节骨架：`_skeleton_outline` 抽得出目录且覆盖率 ≥60%；
           ② 图谱骨架：①不可用且图里这批材料的带边实体 ≥8（`_graph_outline`）；
           ③ 模型归纳：以上都不行，凭主题 + 材料清单。
         **混合材料分开处理再合并**（`_mixed_outline`）：有章节的那几份走章节，
         没有章节的那几份从图里取概念，合并成一套；顺序是章节在前（作者划的结构
         该当一级分支），同一说法只留章节那条（它有 start/coverage、能回原文）。
         **一级分支数不再设人工上限**（`NO_BRANCH_CAP_RULE`）：原来那句"最多 N 条"
         已删，`args.branches` 也不再进提示词 —— 它从来没有被前端传过，见
         `docs/BRANCH-CAP.md`。
      2. 证据：`_retrieve_branches` 每支各检索一次（第一支先行预热块向量，其余
         并行）。命中按分支分组渲染进 user 消息，**带分支归属**——这是与旧版
         整树生成最大的差别：旧版只做一次主题级 top_k=5 预检索，实测对 14,120 字
         材料的覆盖率 5.1%，而按支检索是 30.5%（`docs/RAG-DECISIONS.md` 第 5 节）。
         **材料装得下时这一步整段跳过**（`intake.full_text`，闸是
         `FULL_TEXT_MATERIAL_CHARS`）：改成把每份材料的全文拼进 user 消息。检索的
         覆盖率上限由"分支标题命中不命中原文"决定，而全文在上下文里时它不再是瓶颈
         ——代价是输入 token，见下面那段成本口径。
         **库那一节挂在末尾**（`MINDMAP_LIBRARY_IN_GENERATE`，默认关）：`_intake`
         在"这次没有材料"时查了一次库，过门的父块由 `_library_evidence_section`
         渲染成 `sections` 的最后一节，并同时拼进骨架那次调用的 user（一级分支是
         覆盖上限，只给结构那一步看会错位）。它**不进 `sources`**：库来源不参与按支
         检索——那会让 20 支各自在 26K 字的开发文档上切一遍块，噪声以"按支检索"的
         形式重新进来。三档实测与门的依据见 `docs/LIBRARY-IN-GENERATE.md`。
         开关关着时 `intake.library` 是 None，以上每一条与这段文字之前逐字节相同。
      3. 结构：一次 `chat_async`，骨架 + 全部命中一起给，**只写 id / label / 层级**
         ——`SKELETON_TREE_RULES` 明确要求 detail 留空，引用在这一轮按证据写。
         **有材料时每个节点都必须有引用**（`MUST_CITE_EVERY_NODE`）：原来那句
         "只有你自己补充的内容才给空数组"是个逃逸口（实测薄批只有 31% 的节点带
         引用），现在改成"材料里找不到依据的方面，不要写成一个节点"。于是规模
         由材料决定而不是由配额决定 —— 可引的段落少，节点就少。
         按章切开着（`MINDMAP_SECTION_SLICE`）时这一步是 **N 次**（每章一次，
         各给它那一章的原文），见 `_slice_structure`。
      4. 规模：**不折、不裁，只报数**。可读性参考值是 `INITIAL_MAX_NODES`(80)，超过它
          只记一条 info（`TREE_OVER_READABILITY`），树上一个节点都不动——实测真实生成
          常在 85–157 个节点，而以前那套"折到装得下为止"会把它们压到 47–54，代价是
          2–4 条一级分支整体消失（三组同树对照见 `docs/FOLD-RETIRED.md`）。唯一的硬
          边界是契约的 `MAX_TREE_NODES`(1000)，越过它才落 `_clip_tree`——那一条在真实
          生成里从不触发，是"存不下"的保险，不是配额。

      5. 说明：`_fill_details` 发**两次并发的 `chat_async`**（节点够多时，见
         `DETAIL_SPLIT_MIN`），每半的输入是"这半节点（id / label / 层级路径）+
         同一份原文"，输出是 `{"id":"说明"}`，**按 id 对号**（不靠顺序）。这一轮
         不碰结构、不碰引用。
          **分组节点（两级骨架）不进这一轮**（并进 `skip_ids`）：它是分类名，写出来
          只有一句"这一组在讲什么"的空话，而它占掉的正是那几节的输出预算。

      6. 核对（`MINDMAP_VERIFY_AFTER_GENERATE`，默认关）：用 title + 一级分支拼
         一个 query 搜一次，逐节点判 supported / unverified / contradicted，三态
         只进 `data["verify"]`。实测 supported 17.5%、引文逐字 95.2%，代价 +8 秒
         —— 精度够、召回不够，所以它只能当标注，不能当删节点的依据。

    **一级分支太多时先归成两级**（两级骨架，`SKELETON_GROUP_THRESHOLD` 以上）：
    材料有二十节时，扁平骨架就是二十条一级分支——图宽得没法看。所以**第一步**多问
    一句"这些方面该归成哪几组"，那一次调用的产出就变成"分组 → 分支"两层：

      - 触发：`max(目录行数 + 图谱补充, 章数) > SKELETON_GROUP_THRESHOLD`（10）。
        ★ 这个式子原来读 `args.branches`（"预感要几条"），而不设限之后那个预感
        既不存在也不该由它决定 —— 现在问的是"材料本身有多少宽度可说"。
        低于阈值保持扁平——十支以内一行能扫完，多一层只是多一次点击；
      - 组数：`_skeleton_group_count`，每组 4 支倒推、夹在 3–6 之间（二十节就请它
        归成 5 组；实测模型有时给 4 组，也算它照做）；
      - **不多花一次调用**：那句要求拼在同一次骨架请求的 system 里（`_group_plan_rules`），
        模型这时已经看到了全部二十节——它比任何事后聚类都更会判断哪些该归一起；
      - **组节点一次调用都不吃**：检索仍按二级分支各一次（二十次本地/向量运算，
        不占模型额度）、结构仍是那一次、第三段仍按节点数切两半。组只是树的形状，
        它既不在 `titles` 里，也不进 detail 清单（`skip_ids`）；
      - 形状有兜底：模型仍写成二十条并列时，`_apply_groups` 按骨架的分组把它摆正
        （只搬位置、不改内容），并记一条 `SKELETON_GROUPS_APPLIED`。

    **为什么回到一次生成**（三条实测依据，缺一条这个改动就站不住）：

      - 真实用法 100% 是无材料：`runs` 里 16 次运行 `sources` 全是 0 份。
      - 分片的覆盖率优势需要材料才成立（30.5% vs 5.1%）；没有材料时分片付了
        全部代价（1+N 次调用、三十秒）拿到零收益。
      - 分片有结构性缺陷：**跨支重复**。每支展开时只看到自己那条标题链，兄弟支
        写了什么它不知道，`PLAN_PROMPT` 里"彼此覆盖的范围不重叠"模型并不听
        （实测那张 56 节点的图里，「无监督学习：无标注找结构」在两条不同的
        分支下一字不差出现了两次）。一次生成时全部骨架在同一份响应里，模型至少
        有可能看住全局。

    **为什么要有第四步（第三段）**：把"结构 + 每支说明"压在同一次输出里时，detail
    五采样只有 45.3/28.0/28.5/26.2/27.4 字（红线 40），而且越写越短；在 system 里加
    "不少于 60 字"反而更短（28.5 → 26.2）。瓶颈不是提示词，是一次输出里要塞几十段
    说明。拆出"只写说明"的一次调用后，说明拿到 80–150 字的预算与全部注意力，节点数
    与层数仍由 `INITIAL_MAX_NODES` / `INITIAL_MAX_DEPTH` 守着（初次 80 节点 / 4 层
    含根），`MAX_NODE_DETAIL_CHARS` 也没动。数字见 `docs/ONE-CALL.md`。

    **为什么还要把第三步再拆两半**：这一步的墙钟由**输出量**决定（56 个节点 × 105 字
    ≈ 5900 字，输出侧就要 30 秒），与调用次数无关——加一次调用不会变慢，但要让它
    真的省时间，只能让每次写得更少。两半各写一半输出，墙钟由最慢的那半决定：
    44.5 秒 → 26 秒上下，而 detail 平均与跨支重复都没有退化（同上文档）。


    **总调用 3 次**（骨架 + 结构 + 说明）；带材料时检索是本地/向量运算，不占模型额度。
    第 4 步的失败一律**降级不失败**（见 `_fill_details`）：`detail` 是可选字段，
    为了"说明写短了"把已经成型的结构与引用丢掉是拿确定的好换不确定的好。

    **全塞的成本口径**（这是它唯一的代价，必须能一口说出来）：正文进 prompt 的只有
    两次内容调用——结构那一次与说明的那两次（说明是**两次并发**、各自带全文，所以
    是 3 次带正文的请求）。10 万字按 1.5 字/token 折算是 6.7 万 token，于是
    **3 × 6.7 万 ≈ 20 万 token 输入**；骨架那一次不带正文（它只需要目录：标题行本来
    就是原文的子集，见 `_skeleton_outline`），把 6.7 万 token 花在"让它分出几条
    一级分支"上买不到任何东西。按支检索那条路的输入是"N 支 × top_k=3 × 父块
    ≈ 6–8 千 token"，相差一个数量级——所以闸门（`FULL_TEXT_MATERIAL_CHARS`）宁紧勿松。

    **"全部支都没命中"不兜底整树检索**（与 `_generate_deep` 不同）。那条路的兜底
    是必要的：空支在它那里就是一块空白。这里所有证据本来就在同一次响应里，
    "全空"就是真的全空，模型照 `DETAIL_NO_SOURCE_RULES` 写满内容、引用留空——
    再加一次主题级 top_k=5 检索，正是上面被淘汰的那条 5.1% 的路，而且它还会让
    一棵没有依据的树看起来有依据。

    `partial` 在这条路上有两个来源：输出被长度上限截断（`SalvagedOutput`），以及
    节点数超过 `INITIAL_MAX_NODES` 被裁剪。两者都不是失败（树仍然可用），但用户
    必须能看见——`ok=true` 与"完整成功"长得一模一样，差别只有这两个字段。
    说明没补全**不**进 `partial`（它不是"树少了一块"，树是完整的），走
    `data["detail"]` 那笔账与 `DETAIL_ROUND_*` 诊断。
    """
    if ctx.request.intent != "create":
        return _fail("wrong_intent", "只有新建导图时才能调用这个工具；修改现有导图请用 node_expand 或 mindmap_edit。")
    if ctx.candidate is not None and _has_content_nodes(ctx.candidate):
        return _fail("map_exists", "已经有一张导图了，请用 node_expand 或 mindmap_edit 修改它。")

    # ---- 第 0 步：把这段输入读成「意图 + 主题」（`MINDMAP_INPUT_EXTRACT`，默认关）----
    #
    # **必须在 `_intake` 之前**：材料入口拿去查外部资料与库的那条 query 就是主题
    # （`_external_sources(_external_query(topic, …))` / `_library_supplement(topic, …)`）。
    # 提取放在后面，等于检索查的是一段话、树讲的是另一句，两条路的主题不是同一个。
    #
    # 下面所有消费点都写 `topic`，**`args.topic` 在这条路上一个都不留**：开关关着时
    # `_read_input` 只做一次字符串判断并原样返回 `args.topic`（`skip="off"`），于是整棵
    # 树的输入与改动之前逐字节相同。
    shape = await _read_input(args.topic, ctx)
    topic = shape.topic
    # 意图 / 要覆盖的事 / 限定在 user 里的那一小段（`_intent_block`）。开关关着时恒为
    # 空串，下面两处 `+=` 等于没写。
    intent_block = _intent_block(shape)

    # ---- 材料入口（与 `_generate_deep` 同一份实现）------------------------------
    if ctx.model_calls_used >= ctx.request.budget.max_model_calls:
        return _fail("budget_exhausted", "本次运行的模型调用额度已用完，连骨架都出不了，无法生成。")
    intake = await _intake(topic, ctx)
    sources = intake.sources
    short_material = intake.short_material
    material_chars = intake.material_chars
    hint_block = intake.hint_block
    external = intake.external
    # 材料装得下 → 整份正文进 prompt，不走按支检索（第二步整段跳过）。信号在
    # `_intake` 里算好，这里只消费：判据（`FULL_TEXT_MATERIAL_CHARS`）只该有一处。
    full_text = intake.full_text
    # 真的会被拼进 prompt 的那份字数（含外部检索补进来的）。与 `material_chars`
    # （只数用户随请求带来的）分开报：诊断里说"合计 X 字"必须能与 prompt 对上。
    full_text_chars = intake.full_text_chars
    # 生成前主动查库的产物（`MINDMAP_LIBRARY_IN_GENERATE`，默认关）。它**不进
    # `sources`、不进 `full_text_chars`**，走单独一节证据（理由见 `_Intake` 的
    # docstring）。开关关着时 `intake.library` 是 None，下面三处消费点全部跳过。
    library = intake.library

    diagnostics: list[Diagnostic] = []
    # 提取没做成时**必须说出来**：开关打开了、树却还是按原话长的，用户唯一的线索就是
    # 这一条。四种来路四句话（"开关开了却没生效"这件事在 `SECTION_SLICE_UNAVAILABLE`
    # 那里已经记过一遍）。`short` / `empty` / `off` 不报：那三个是**设计**——短主题本来
    # 就不该为它多付一次调用，不是失败。
    if shape.skip in _INPUT_EXTRACT_FAILURE:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="INPUT_EXTRACT_UNAVAILABLE",
            message=f"输入提取没做成（{_INPUT_EXTRACT_FAILURE[shape.skip]}），"
                    "这一次把原话当主题生成；画出来的可能是一棵讲这句话的树。",
        ))
    # 入图那一步的账（开关关着时恒为 None，什么都不加——键集与加它之前逐字节相同）。
    if intake.ingested is not None:
        diagnostics.append(_graph_ingest_diagnostic(intake.ingested))
    for reason in intake.graph_ingest_skip:
        # "没做"也要有一句话：这一档是**花钱买来的加分项**，用户打开了开关却什么都没
        # 发生时，唯一能分辨"配置没生效"与"材料太少"的东西就是这一条。
        skip = _graph_ingest_skip_diagnostic(reason)
        if skip is not None:
            diagnostics.append(skip)
    if library is not None:
        # 库这一步的账：补上了报"几段 / 几份 / 多少字 / 多久"，没补上说清为什么
        # （六种来路六句话，见 `_library_supplement_diagnostic`）。与入图那一步同一条
        # 规矩——一个会花掉秒级时间、会改变提示词形状的步骤必须留下决定。
        note = _library_supplement_diagnostic(library)
        if note is not None:
            diagnostics.append(note)
    thinking = thinking_block(ctx.request.thinking_model)
    # 图型：请求层权威（理由与 `_generate_deep` 同一段注释——模型看不见界面，
    # `diagram_type` 只要默认值存在它就会走默认值）。
    diagram_type = ctx.request.diagram_type or args.diagram_type
    source_of_diagram = ctx.diagram_source or (
        SOURCE_REQUEST if ctx.request.diagram_type else SOURCE_MODEL)
    if ctx.request.diagram_type and args.diagram_type != ctx.request.diagram_type:
        logger.debug(
            "图型以请求为准：请求=%s，工具参数=%s（模型的写法不生效）",
            ctx.request.diagram_type, args.diagram_type,
        )

    # ---- 第一步：定一级分支（两条路在这里分岔，出口形状相同）--------------------
    #
    # 两个分支的产物都是 `titles` + `plan` + `raw_plan`，所以**第二步之后的代码一个字
    # 都不用改**——两段式要换的只有"分支从哪来"，不是"分支怎么用"。
    #
    #   - 三段式（默认）：骨架调用 → `raw_plan` → `_skeleton_plan` → `plan`。
    #     有材料时优先用材料自带的章节（`_skeleton_outline`：覆盖率 ≥60% 才拼）。
    #   - 两段式（`MINDMAP_GRAPH_SKELETON=1`）：**不调模型**，一级候选与检索 query
    #     都来自图谱的头部实体；`plan` / `raw_plan` 是 None（没有骨架产物需要套色）。
    #
    # `graph_mode` 只在 `tree` 上开：流程图的一级是**阶段**（先后顺序来自材料，图上
    # 的边没有时间轴），鱼骨是六个固定类别（换一套分类就不再是鱼骨图）。两条路继续
    # 走三段式——这不是偷懒，是那两种图型的"一级分支"根本不由概念决定。
    graph_mode = graph_skeleton_enabled() and diagram_type == "tree"
    # ---- 按章切：先切章（只读原文，不发请求）-----------------------------------
    #
    # **必须在骨架之前**：骨架那一次要知道"这次按这份材料的 N 章分"，否则它按自己的
    # 直觉给 6 条，而材料有 12 章，"每支给它这一章"这个前提从一开始就不成立——那时
    # 每一支拿到的都会是半章，覆盖率的分母也就说不清了。
    #
    # 只在 `tree` 上开：流程图的一级是**阶段**、鱼骨的一级是固定六类，章的边界与它们
    # 没有对应关系。
    slice_chapters = (
        _chapter_slices(sources)
        if section_slice_enabled() and diagram_type == "tree" and not graph_mode
        else []
    )
    if section_slice_enabled() and not slice_chapters:
        # 开关开了却没切成：**必须说出来**。不说的话，用户以为自己在看 A/B 的 B 组，
        # 而实际跑的是一次完全一样的现状——那是最坏的一种"实验"。
        reason = (
            "这次没有可切章的材料" if not sources else
            f"材料有 {len(sources)} 份（切章要求恰好一份：`OutlineEntry.start` 是"
            "单份文本内部的偏移）" if len(sources) != 1 else
            "材料抽不出可用的章节标题（目录为空或覆盖率不足）"
        )
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.info, code="SECTION_SLICE_UNAVAILABLE",
            message=f"{reason}，这一次退回原来那条路（一次写完）。",
        ))
    graph_entities: list[graph_store.GraphEntity] = []
    graph_links: list[graph_store.GraphLink] = []
    # 章节目录：只在三段式的骨架那一步算一次（`heading_outline` 是对整份材料跑正则，
    # 大材料上不是零成本）。两段式没有"用目录定骨架"这件事，留空。规模判据要用它。
    outline_entries: list[Any] = []
    raw_plan: Any = None
    plan: _SkeletonPlan | None = None
    skeleton_seconds = 0.0
    # 骨架两次都低于 `MIN_BRANCHES` 且这次有材料 → **按实际条数交付**（理由见下面
    # 骨架那一段）。它同时决定收尾那道校验要不要守 `MIN_BRANCHES`：那道闸问的是
    # "模型整图生成的一级分支够不够"，而这里已经明知只够两条并记了一条诊断，
    # 再在出口判死一次等于把这条降级路径整个抵消掉。
    branches_degraded = False
    if graph_mode:
        # `scope` 是**本次材料**的 source_id 集合：图是跨材料共享的，不限定范围时
        # "上一次放过的那批报销材料"的实体会混进这一次的主题（图里没人知道哪个实体
        # 属于哪一批，只有边上的 `source_id` 知道）。id 对不上（材料没入过图）时
        # `subgraph` 自己回退全图，不在这里判。
        graph_entities, graph_links = graph_store.subgraph(
            entities=GRAPH_ENTITY_LIMIT, links=GRAPH_LINK_LIMIT,
            scope=[source.id for source in sources] or None)
        if len(graph_entities) < MIN_BRANCHES:
            # 这是**真降级**，必须说出来：开关开着、图却没建（或建了但全是孤立实体）
            # 时，用户拿到的是一张"骨架照旧"的图。不说的话他以为两段式生效了，而
            # 覆盖率数字会莫名其妙地退回三段式的水平。
            graph_mode = False
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn, code="GRAPH_SKELETON_UNAVAILABLE",
                message=(
                    f"图谱里只有 {len(graph_entities)} 个带连接的实体"
                    f"（少于 {MIN_BRANCHES} 个），这一次退回章节骨架那条路；"
                    "把材料重新入图（`scripts/ingest_graph_corpus.py`）之后再试两段式"
                ),
            ))
    if graph_mode:
        # 检索 query 就是实体名本身：它们是材料里**真实出现过**的词，而三段式那条路
        # 发的是模型猜的分支标题。这是覆盖率能上去的第一个原因（第二个是下面的
        # 结构调用拿到了整张子图）。
        titles = [item.name for item in graph_entities[:GRAPH_QUERY_LIMIT]]
    else:
        # 额度在调用之前占：请求一旦发出就是一次真实计费，哪怕它随后抛错。
        if not ctx.reserve_model_call():
            return _fail(
                "budget_exhausted", "本次运行的模型调用额度已用完，出不了骨架，无法生成导图。")
        ctx.extra_model_calls += 1
        # 混合材料：有章节的那几份出章节骨架，没有的那几份**另起一组**（图谱概念）。
        # 只有"两组都有"时才走这条路（`_mixed_outline` 自己判），纯章节 / 纯摘要那两
        # 种批走的是下面原来那一档，一个字节都不变。
        mixed = _mixed_outline(sources)
        outline_entries = (
            list(mixed.chapters) if mixed is not None else _outline_entries(sources))
        # 目录少于 `MIN_BRANCHES` 条时补一句"这条目录偏窄"（`OUTLINE_FEW_BLOCKS_NOTE`）：
        # `OUTLINE_HEADER` 说的是"优先按它划分一级分支"，模型照做，于是两块的材料
        # 长出两条一级分支，被判废。修法不是丢掉目录（那是骨架唯一能看到的材料结构），
        # 而是取消"条数跟着块数走"这个锁。
        outline_note = (
            OUTLINE_FEW_BLOCKS_NOTE.format(count=len(outline_entries), minimum=MIN_BRANCHES)
            if 0 < len(outline_entries) < MIN_BRANCHES else "")
        outline = _render_outline(outline_entries, sources, note=outline_note)
        if mixed is not None and mixed.supplement.block:
            # 章节在前、图谱在后（顺序的理由见那一大段注释里的①）。
            outline = outline + "\n\n" + mixed.supplement.block
        # 第三档：目录不可用（`entries` 为空，含"抽不出标题"与"覆盖率不足"两种）时，
        # 用图谱定骨架，而不是让模型凭前 200 字自行归纳。判据 / 阈值 / 代价见
        # `_graph_outline` 那一段的注释——这里只消费它的两个出口。
        #
        # 为什么不在 `_outline_entries` 里就把图谱拼进去：那是个纯函数（只读原文、
        # 不碰网络、被 `_measure` 与两级骨架的判据共用），塞一次 Neo4j 往返会让它
        # 从"读文本"变成"读文本 + 读库"。
        graph_outline, graph_outline_entities = "", 0
        if mixed is not None:
            # 混合批的账（用了几个概念、覆盖几份；或者为什么没补上）在这里报一次。
            diagnostic = _mixed_diagnostic(mixed)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
        elif not outline_entries:
            graph_outline, graph_outline_entities, graph_skip = (
                _graph_outline_attempt(sources))
            if graph_outline:
                outline = graph_outline
                # 记一条 info：用户看到的分支名与他自己材料里的词对得上时，得有一条
                # 记录说明那是"图谱定的"而不是模型瞎猜对了。
                diagnostics.append(Diagnostic(
                    level=DiagnosticLevel.info, code="GRAPH_OUTLINE_USED",
                    message=(
                        f"材料抽不出章节标题（{material_chars} 字），改用图谱里的 "
                        f"{graph_outline_entities} 个概念定骨架。"),
                ))
            else:
                # 降级方向也要说：判据说"没图是部署常态"是**沉默的理由**，但不是
                # "用户不该知道开关没生效"的理由。四种原因各一句（`GRAPH_SKIP_*`）。
                skip = _graph_outline_skip_diagnostic(graph_skip, graph_outline_entities)
                if skip is not None:
                    diagnostics.append(skip)
        base_plan = (
            PLAN_PROMPT if diagram_type == "tree"
            else FLOW_PLAN_PROMPT if diagram_type == "flowchart"
            # 类别可变的那一版（`MINDMAP_FISHBONE_FREE_CATEGORIES`）：关着时这个调用
            # 返回的就是 `FISHBONE_PLAN_PROMPT` 本身，逐字节不变。
            else fishbone_plan_prompt()
        )
        # **两级骨架的判定点**：这一次大概会有多少个方面。**只数材料的宽度**——
        # 目录行数（`_skeleton_outline` 拼进去的那一份，覆盖率不够时是 0 行）、图谱
        # 补的那几条、按章切的章数。
        #
        # `args.branches` 已经从式子里拿掉了，理由是它**不是材料宽度**：那是模型在读到
        # 材料之前填的一个数（字段默认 5），而条数本身这一轮已经不由代码定
        # （`NO_BRANCH_CAP_RULE`）。拿它当"这次大概有 5 个方面"，等于用一个预感去决定
        # 要不要给更宽的画布——而预感恒为 5，永远小于阈值 10，于是这一档永不触发。
        #
        # 为什么要**预判**而不是等骨架回来再决定：骨架只调用一次，要让它一次就吐两级，
        # 这句要求必须在请求里。等回来再判就只能再发一次"请把刚才的 20 条归成 5 组"，
        # 那正是这次要避免的第二次调用。
        #
        # 预判错了不致命，两个方向都有出路：判多（要了两级，回来只有 8 支）→
        # `_skeleton_plan` 压平成扁平；判少（要了扁平，回来 20 支）→ 也走扁平，
        # 与改动之前逐字节相同。所以这条判据只影响"什么时候给它更宽的画布"，
        # 不影响成不成功。
        #
        # **纯主题因此恒不请求分组**（三项都是 0）：这与改动之前同一条结果——那时
        # `args.branches` 取默认 5，5 < 10 同样不请求。差别只在于现在这个"不请求"
        # 是**材料真的没有宽度可说**，而不是"预感恰好不够大"。它留下的那格风险
        # （模型自由发挥给到十几条一级分支时是一张宽图）由 `SKELETON_GROUP_THRESHOLD`
        # 之外的机制兜：`_fill_plan_colors` 与画布的横向排布都不按条数截断，而一级
        # 分支条数超过 20 时 `_cap_plan` 仍按 `DEEP_MAX_BRANCHES`（画布承载）收口。
        #
        # 只有 `tree` 分组：流程图的一级是**阶段**（分组会打断顺序），鱼骨图的一级是
        # 固定的六类（换一套分类就不再是鱼骨图）。
        #
        # 混合批要把图谱那几条也算进"这次有几个方面"：它们是真的会变成分支的东西，
        # 少算就少要一级（`grouped_request` 判在 10 上），多算只是提前分组——两个方向
        # 的兜底见上面那段（`_skeleton_plan` 会压平 / 保持扁平）。
        supplement_items = mixed.supplement.items if mixed is not None else 0
        sections_expected = max(
            len(outline_entries) + supplement_items, len(slice_chapters))
        # 按章切**不请求分组**：这条路的一级分支就是章，本来是一维序列；而分组会多
        # 吃一层深度（`INITIAL_MAX_DEPTH=3`：root → 组 → 章 → 章内要点，章里只剩
        # 一层），把"这一章有 ### 子节"这件事挤没。
        grouped_request = (
            diagram_type == "tree" and not slice_chapters
            and sections_expected > SKELETON_GROUP_THRESHOLD)
        plan_system = base_plan + (
            _group_plan_rules(sections_expected) if grouped_request else "")
        # 第三档的规则尾巴接在 base_plan 之后、分组规则之后：它说的是"一级分支从
        # 哪来"，与 `PLAN_PROMPT` 里那句「目录为空或不完整时再自行归纳」直接对冲，
        # 越靠后越压得住那句（模型对紧挨着输出位置的规则更敏感）。
        # 有章节标题时**一个字都不加**，那条路径逐字节不变。
        if graph_outline:
            plan_system += GRAPH_PLAN_RULES
        elif mixed is not None:
            # 混合批：两类材料各一句话（用了图谱 / 图谱里没有）。位置与
            # `GRAPH_PLAN_RULES` 相同——它也要压在「目录为空时自行归纳」之后。
            plan_system += (
                _mixed_plan_rules(mixed.supplement.items)
                if mixed.supplement.block
                else _mixed_plain_rules(mixed.plain))
        # 材料是**要整理的对象**，不是"这类文档怎么写"的范例（判据与实测见
        # `MATERIAL_AS_OBJECT_RULES`）。只在这一批**有材料**时拼：纯主题那几档没有
        # 材料，也就没有"把材料当范文"这个歧义，多一句只会把"按主题归纳"挤走，
        # 还会让没有材料的那条路径与改动之前不再逐字节相同。
        if sources:
            plan_system += MATERIAL_AS_OBJECT_RULES
        # 没有可检索材料时补那两句纯文本契约（与 `_generate_deep` 的骨架段同一条
        # 判定）：它们不含输出格式，不会与 `PLAN_PROMPT` 的"只返回 branches"打架。
        #
        # 库补了片段时**只补前一句**：`DETAIL_NO_SOURCE_RULES` 说的是"没有可标出处的
        # 材料"，而这一轮的 user 末尾正摆着库里的原文——两句话并存会让骨架以为这批
        # 片段不算数，于是照样按"凭主题自由发挥"定分支，而分支决定整棵树的覆盖。
        library_block = (
            "\n\n" + _library_evidence_section(library, cite=False)
            if library is not None and library.hits else "")
        if not sources:
            plan_system += "\n" + CONTENT_FREEDOM_CONTRACT
            if not library_block:
                plan_system += DETAIL_NO_SOURCE_RULES
        plan_started = time.perf_counter()
        ask = _plan_ask(
            topic,
            outline=outline,
            # 清单紧跟在 `outline` 之后进 prompt：三个条件（有材料、没材料、按章切）
            # 共用同一个渲染器，`sources` 为空时它是空串，`_plan_ask` 一字不加。
            # `has_structure` 看的是**已经拼进去的那个块**（章节或图谱），不是
            # "有没有标题"——图谱骨架那一档同样不该被补"没有可用的章节标题"。
            materials=_material_inventory(sources, has_structure=bool(outline)),
            diagram_type=diagram_type,
        )
        # 库片段也给骨架那一次看：一级分支是整棵树的覆盖上限，只给结构那一步看
        # 的话，分支仍会按模型的先验（政策背景/技术路径/案例分析）铺开，而库里那几段
        # 只能挂在其中一两支上——树与证据错位。开关关着时 `library_block` 恒空。
        ask += library_block
        # 意图那几行拼在骨架的 user **末尾**（`_plan_ask` 的返回值之后、按章那句之前
        # 或之后都行——它不与任何一条规则冲突，它们是内容，不是格式）：
        #
        # 一级分支决定整棵树的覆盖，所以"他真正想问的是哪几件事"最该先给这一步看。
        # 空串时（开关关着 / 太短 / 提取失败）这里一字不加。
        ask += intent_block
        if slice_chapters:
            # 拼在 `_plan_ask` 的返回值**之后**，不改进那个函数：它与
            # `PLAN_PROMPT` 一起被逐字节回归用例钉着（`test_outline.py`）。
            ask += (
                f"\n这次的一级分支**就是这份材料的 {len(slice_chapters)} 章**："
                "一条分支对应一章，顺序按材料里的顺序，标题照抄章节标题，不增不减。"
            )
        try:
            raw_plan = await _chat(
                ctx,
                plan_system,
                ask + hint_block,
                max_tokens=_max_tokens(ctx),
            )
        except GenerationError as exc:
            return _fail("generation_failed", f"骨架生成失败：{exc}")
        skeleton_seconds = time.perf_counter() - plan_started
        _note_truncation(diagnostics, raw_plan)

        # 骨架 → 最终形态（扁平 / 两级）。`_skeleton_plan` 自己也判一次"要不要两级"，
        # 判据与上面的 `grouped_request` 同源（分支数 > `SKELETON_GROUP_THRESHOLD`）：
        # 这里判"要不要问"，那里判"回来的东西够不够两级"。
        plan = _cap_plan(_skeleton_plan(raw_plan))
        if slice_chapters and plan.grouped:
            # 按章切**不分组**（理由见 `grouped_request`）：模型自发给了分组时压平，
            # 否则 `_apply_groups` 会把 12 条章摆到 5 个组下面，章内就只剩一层
            # ——而这一章里明明有 `###` 子节可写。
            plan = _SkeletonPlan(flat=plan.flat)
        # ---- 一级分支太少：先带证据重问一次，再决定是交树还是判死 ------------------
        #
        # 分三种情形，判据与修法见 `docs/OFF-TOPIC-AND-FEW-BRANCHES.md`：
        #
        #   ① **纯主题**（没有材料）：2 条就是模型没话说，直接判失败。与改动之前逐字相同。
        #   ② **有材料、第一次少于 3 条**：先重问一次。重问必须换**输入**（材料自带的
        #      章节，或材料开头几百字，见 `_few_branches_evidence`）——同一段 user 再发
        #      一次，答案是同一串（实测同材料两次都回 2 条，这也是验收里那三条
        #      "重试仍失败"的由来）。**不改 `MIN_BRANCHES`**：那三条材料的正确分支数是
        #      6（`一、背景 … 六、风险`），2 条是"没读到材料"，不是"材料只有两件事"。
        #   ③ **重问之后仍少**：交树 + 一条 warn 诊断。这时它是一棵**有引用、能点开、
        #      能继续编辑**的树，而判失败给用户的是一张错误卡片——`partial` 机制救不了
        #      后者（它要求"至少长出过一棵树"）。质量门槛没有被拆掉：门槛守的是
        #      "不要主动交两支架構"，而这里交出去的是"模型坚持只能给两条"的那棵树，
        #      并且它带一条诊断，用户看得见差在哪。
        if len(plan.flat) < MIN_BRANCHES:
            if sources and not slice_chapters:
                retry = await _retry_few_branches(
                    ctx, plan_system, ask + hint_block, plan,
                    sources=sources, outline_entries=outline_entries,
                )
                if retry is not None and len(retry.flat) > len(plan.flat):
                    plan = retry
                    # 骨架这一段的时间包含重问那次：不然"骨架 3 秒"会把一次多花的
                    # 调用藏起来，而它正是"为什么这次慢了"的答案。
                    skeleton_seconds = time.perf_counter() - plan_started
            if len(plan.flat) < MIN_BRANCHES and not sources:
                return _fail(
                    "too_few_branches",
                    f"模型只给了 {len(plan.flat)} 条一级分支，少于 {MIN_BRANCHES} 条，无法成图。",
                    branches=len(plan.flat),
                )
            if len(plan.flat) < MIN_BRANCHES:
                branches_degraded = True
                diagnostics.append(Diagnostic(
                    level=DiagnosticLevel.warn, code="BRANCHES_BELOW_MIN",
                    message=(
                        f"模型两次都只给了 {len(plan.flat)} 条一级分支（少于 {MIN_BRANCHES} 条），"
                        "这张图按实际条数交付：结构偏窄，请展开节点补充或重新生成。"
                    ),
                ))
        # 配色在截断（`_cap_plan`）之后：只给真的要上画布的那几条分配颜色，否则
        # "第 4 支拿到第 4 个颜色"这种事会因为一条被截掉的尾巴而错位。两级时补的是
        # **分组**的色（一级颜色只属于分组）。
        plan = _fill_plan_colors(plan)
        if not prefs.icons_enabled():
            plan = _strip_plan_icons(plan)
        titles = [branch.label for branch in plan.flat]
        # **只在按章切时报"条数不对"**：那条路要求"一条分支 = 一章"，少一条就有一章
        # 没有支（那一章的覆盖率恒为 0），那是结构约定，不是配额。其余情况一律不报——
        # 已经没有任何地方在要求一个条数了，没有期望就没有落差（以前那条诊断比对的是
        # 模型自己在工具参数里猜的数，"请求 5 条、模型给了 9 条"里"请求"两个字是假的）。
        requested_branches = len(slice_chapters) if slice_chapters else None
        if requested_branches is not None and not (
                requested_branches - 1 <= len(titles) <= requested_branches + 1):
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn, code="SKELETON_SIZE_OFF",
                message=f"请求 {requested_branches} 条一级分支，模型给了 {len(titles)} 条，"
                        "已按实际条数继续。",
            ))

    # ---- 第二步：每支各检索一次（并行，第一支预热）------------------------------
    #
    # 这一段是"保留分片那条路的检索质量"的全部：query 是分支标题本身
    # （`_branch_query`，三组消融见该函数），top_k 与分片一致（`DEEP_BRANCH_TOP_K`），
    # 预热策略也一致（`_retrieve_branches` 的"第一支先串一条"）。
    #
    # **材料装得下时整段跳过**（`intake.full_text`）：全塞买到的正是检索做不到的那件事
    # ——覆盖率。按支检索的召回上限由"分支标题能不能命中原文"决定（实测 28.8%，
    # 见 `_search_branch`），而全文进 prompt 之后，材料里每一段都在模型的上下文里，
    # 覆盖率不再取决于它的 query 写得好不好。代价是输入 token（见
    # `FULL_TEXT_MATERIAL_CHARS` 的成本口径），所以闸门按实测的注意力拐点定。
    #
    # ★ **全塞 = 不要检索，正文就是依据。** 下面那道 `if` 不是"条件没满足"而是
    # **刻意跳过**：全塞时一次 `search_parent` / `search_multi` 都不发，`sections`
    # 换成 `_full_text_sections(sources)`（每份材料一整节）。这也是这一步唯一的收益
    # 来源——省下的那些检索调用与向量往返正好可以付给生成之后那一次核对
    # （`_verify_after_generate`）：**同一笔钱从"召回材料"换成"核对这棵树"**。
    #
    # 一条边界（实测见 `scripts/measure_fulltext_retrieval.py`，文档
    # `docs/GENERATION-VERIFY.md` 第 3 节）：第三段那条按节点章内检索
    # （`MINDMAP_DETAIL_RETRIEVAL`）另有一道前置条件 `chapters`——它是按章切出来的，
    # 而按章切要求"恰好一份材料且能抽出目录"。所以**多份材料全塞时三条检索路径全是
    # 0 次**；但一份带标题的小 markdown 会让全塞与按章切同时成立，那时第三段仍然会
    # 检索（那是另一条路的开关，不归这里管）。
    evidences: list[_BranchEvidence | None] = []
    retrieval_queries: list[str] = []
    expansions: dict[str, list[str]] = {}
    retrieval_seconds = 0.0
    if sources and not full_text:
        retrieval_started = time.perf_counter()
        # 两段式**不做 query 扩展**：`plan_branches` 的价值在于"骨架标题是模型臆造的
        # 通用词，让模型再猜两条同义问法"（见 `_expand_branches`），而两段式的 query
        # 是材料里真实出现过的实体名——再花一次模型调用把真词换成猜的词，方向是反的。
        if not graph_mode:
            expansions = await _expand_branches(sources, titles)
        retrieval_queries = [
            query for title in titles
            for query in _branch_queries(_branch_query(title), expansions.get(title, ()))
        ]
        hits_by_branch = await _retrieve_branches(
            sources, titles, expansions=expansions)
        retrieval_seconds = time.perf_counter() - retrieval_started
        for hits in hits_by_branch:
            evidence = _branch_evidence(hits, sources) if hits else None
            evidences.append(evidence)
            if evidence is not None:
                # 命中并进本次运行的证据集：收尾的 `verify_source_refs` 要按它判
                # "这条引用落在可见的父块里吗"。少记一条，模型照它抄的引用就会
                # 被判成编的（与 `_rag` / `_generate_deep` 记的是同一份东西）。
                ctx.record_evidence(evidence.spans)
    with_evidence = sum(1 for item in evidences if item is not None)

    # ---- 第三步：一次写完 ------------------------------------------------------
    #
    # 分组渲染是这一段唯一的新东西：同一次响应里可能有十几组形状相同的 evidence
    # 数组，不带归属的话模型无法知道哪条命中属于哪一支（分片那条路不需要这层，
    # 它每次只给一支看）。
    #
    # 全塞时 `sections` 换成"每份材料一整节"。两种渲染的**形状**必须与 system 里那句
    # 引用规则一致（"每组一个 evidence 数组" vs "每份一整节"）：规则说按分支分组、
    # 正文按材料分节，模型会把材料当成"第 N 支的证据"，然后只从第一份里抄。
    # 证据分节的表头跟着"检索是按什么分的"走：三段式按一级分支，两段式按概念。
    # 混用会让模型把"第 3 支"读成一个它没见过的分支名，然后去树里找一个不存在的
    # 一级分支（那一步不会报错，只会少抄几条引用）。
    evidence_header = GRAPH_EVIDENCE_HEADER if graph_mode else EVIDENCE_SECTION_HEADER
    sections = (
        _full_text_sections(sources) if full_text else
        [
            evidence_header.format(index=index, title=title)
            + "\n" + evidence.text
            for index, (title, evidence) in enumerate(zip(titles, evidences), start=1)
            if evidence is not None
        ]
    )
    # 库那一节挂在**最后**（`MINDMAP_LIBRARY_IN_GENERATE`，默认关）：它不属于任何
    # 一支、也不是整份材料，放末尾之后上面那几条"按支分组 / 按材料分节"的表头仍然
    # 说的是它们各自的那批，模型不会把库片段当成第 N 支的证据。
    #
    # 它与 `full_text` **可以同时成立**（用户传了 300–3000 字的薄材料时：既够
    # `SHORT_MATERIAL_CHARS`、又不够 `LIBRARY_SUPPLEMENT_CHARS`），所以不能写成
    # 上面那条三元表达式的另一臂——那样会把全塞的正文挤掉。
    library_section = ""
    if library is not None and library.hits:
        library_section = _library_evidence_section(library)
        sections = [*sections, library_section]

    # ---- 第三步（按章切）：每章一次调用，组装成整棵树 ---------------------------
    #
    # 这一段把"一次写完"换成 N 次（每章一次）。除了调用方式，**下游一个字都没改**：
    # 组装的 `raw` 形状与"一次写完"那条路完全相同（`{"title","root"}`），所以校验 /
    # 套色 / 裁剪 / 第三段 / 重复检测走的还是同一套。这也是它能当干净 A/B 一臂的
    # 前提——两组的差异只可能来自第二段，不可能来自收尾。
    structure_seconds = 0.0
    slice_report: dict[str, Any] | None = None
    # ---- 规模：不再由代码给一个数 -------------------------------------------------
    #
    # 这一段以前算的是"这一次要求几个节点"（`budget.initial_nodes`：默认 80，开
    # `MINDMAP_NODE_SCALE` 则按材料伸缩，`MINDMAP_NODE_CAP` 还能手动压），那个数同时
    # 去了三处：system 里那句"控制在 N 个节点以内"、`_clip_tree` 的上限、`data["node_limit"]`。
    #
    # 现在三处只留一处：**不许写进提示词**。理由与本模块 `NO_BRANCH_CAP_RULE` 那一段
    # 同源——配额是"能写多少"，而这一轮要的是"有多少能写"：有材料时由"每个节点必须
    # 有引用"约束（找不到依据的方面写不成节点），纯主题时由单次输出预算约束。给一个数
    # 只会让模型把它当填额目标（实测：材料翻 135 倍、说明总量只涨 30%，见
    # `docs/GRAPH-SCALE.md`）。
    #
    # 留下的这一个数**换了语义**：`INITIAL_MAX_NODES` 从"不许超过"变成"超过就记一条
    # 诊断"（`TREE_OVER_READABILITY`）。它量的是**可读性**，不是产能，所以它与材料无关
    # 地固定在那里是对的——一棵 300 个节点的图在画布上就是读不完，那与材料多厚没有
    # 关系。而"读不完"该怎么处理属于渲染层（缩放、收起），不属于内容层：曾经在这里
    # 折过，代价是整条一级分支被换成一行「其他 N 项」（`docs/FOLD-RETIRED.md`）。
    initial_nodes = INITIAL_MAX_NODES

    # 外部来源的可信度提示：两条路共用（第三段也用它），所以必须在 if/else **之前**
    # 定下来。判据只看 `sections` 与来源类型，与"结构那一次怎么发"无关。
    external_rules = (
        EXTERNAL_TRUST_RULES
        if sections and any(source.kind in (SourceKind.web, SourceKind.paper)
                            for source in sources)
        else ""
    )
    if slice_chapters:
        raw, slice_report = await _slice_structure(
            topic=topic, chapters=slice_chapters,
            plan=plan if plan is not None else _SkeletonPlan(flat=()),
            ctx=ctx, text=(sources[0].text or ""), diagnostics=diagnostics,
        )
        structure_seconds = slice_report["seconds"]
    else:
        if graph_mode:
            # 两段式：一级分支还没定，要给的是**图谱**。`_skeleton_block(plan)` 在这里
            # 拿不到东西（plan 是 None）——那正是这个分支存在的理由。
            skeleton_intro = (
                "\n\n这次**不按材料章节分**：一级分支请从下面这张图谱里挑"
                "（挑法与关系的写法见 system 的规则）：\n"
            )
            skeleton_block = _graph_block(graph_entities, graph_links)
        else:
            skeleton_block = _skeleton_block(plan)
            # 两级的开头那句必须跟着变：它说的是"这是一层还是两层"，与清单的形状必须一致，
            # 否则模型看到缩进却读到"一级分支"，会按扁平的根规则摆（那就白分组了）。
            skeleton_intro = (
                "\n\n这次的两级骨架（已定好；缩进的是分组下面的二级分支，按此顺序，不要增删改名）：\n"
                if plan.grouped else
                "\n\n这次的一级分支（已定好，按此顺序，不要增删改名）：\n"
            )
        user = f"主题：{topic}" + intent_block + hint_block + skeleton_intro + skeleton_block
        # 流程图的 `role` 必须**在 user 末尾**再说一遍（system 里已有 `FLOW_TREE_RULES`，
        # 但实测第一版把它只写在 system 里时，两次采样一个 `decision` 都没标出来，
        # 而那份流程里明明有「校验信息完整性」「验证账号唯一性」这种判定步骤）。
        #
        # 位置的理由与 `_detail_ask` 结尾那句自查同源：模型对**紧挨着输出位置**的
        # 那句话最敏感，而 system 里的 role 说明夹在 `DETAIL_PROMPT` 的格式定义与
        # 「这一轮不要写 detail」之间，被读成了一段泛泛的附加要求。
        if diagram_type == "flowchart":
            user += (
                "\n\n**每个阶段里凡是需要判断走哪一条路的那一步**（校验 / 审核 / 检查 / "
                "是否通过 / 是否超限这类），在它的节点对象上加一个字段 "
                '"role": "decision"；流程的第一步加 "role": "start"，最后收尾的那一步加 '
                '"role": "end"，普通步骤不用加。例如：'
                '{"id":"…","label":"校验信息完整性","role":"decision","children":[]}。'
                # 两条出口与 role 写在同一处：它们是**同一个决定**的两半——标了判定却
                # 没写出口，那个节点在画布上就只有一个出口（甚至零个），比不标还费解。
                # 实测（`docs`/阶段 2 的记录）：只要求 role 时模型会标出 3–5 个判定节点，
                # 而它们的出边数为 0——因为契约当时没有位置。
                '\n**标了 decision 的节点，紧接着把它的两条出口一起写出来**：'
                '在同一个节点对象上加 "branch" 字段，'
                '{"yes":"走「是」时到的那个节点的标题","no":"走「否」时到的那个节点的标题"}。'
                "标题要**逐字**抄这份响应里另一个节点的 label（可以是本阶段后面的某一步，"
                '也可以是另一个阶段里的一步）；这条路到此结束时写 "结束"。'
                # 新形状（平行四边形 / 圆柱）的判据必须写在这里，理由与上面那段 `role`
                # 完全相同：系统提示词里的 `_FLOW_BODY` 有一段一样的说明，而 stage 2 的
                # 实测是"同一句话写在 system 里，两次采样一个都没标出来"。
                #
                # 措辞刻意用**对象是数据本身**这一条判据，而不是"读写数据"——后者太宽，
                # 模型会把每个填单子的步骤都标成 data（实测见
                # `scripts/measure_flow_data_roles.py` 的报告），平行四边形于是变成装饰。
                '\n**哪一步在跟数据打交道，就把它标出来**（这两种形状在画布上是平行四边形'
                '与圆柱，读图的人靠它分清算数据与干活）：'
                '这一步**产出了数据**（录入 / 填单 / 出具 / 生成报表）、或者**交出了数据**'
                '（提交 / 上报 / 发送回执）就加 "role": "data"；'
                '这一步**写进存储**（落库 / 入台账 / 归档 / 记账）或**从存储里取**'
                '（查询 / 调取档案 / 读取余额）就加 "role": "database"。'
                '判据是这一步**对象就是数据本身**，在数据之上做业务判断的那一步仍然是普通步骤'
                "（那样的一步如果确实要选路，它才该标 decision）。"
                '例如：{"id":"…","label":"录入报销单","role":"data","children":[]}、'
                '{"id":"…","label":"写入审批台账","role":"database","children":[]}。'
            )
        if sections and full_text:
            user += ("\n\n下面是**材料的完整正文**（按材料分节，每节标题写明文件名与字数）；"
                     "它不是检索片段——写任何一支时都先在这些正文里找对应内容，"
                     "引用逐字从里面抄：\n\n" + "\n\n".join(sections))
            # 引用要求必须在**正文之后**再说一遍，不能只留在 system 里。
            #
            # 这不是"多提醒一遍更保险"，而是位置问题：全塞把上万个字的正文放在 user 末尾，
            # system 里那句"凡是内容出自材料就挂 source_refs"距离输出位置隔了一万字。
            # 实测（`scripts/measure_incontext.py`，1 万字档）只写在 system 里时模型
            # **一条引用都不写**（declared=0），而同一次调用里它明明把中间的备案号抄进了
            # detail——内容用上了、出处没标。同一个教训在 `_detail_ask` 的自查句与 flowchart
            # 的 role 说明上都出现过：模型对紧挨着输出位置的那句话最敏感。
            user += (
                '\n\n写结构时每个节点的引用字段一起写：内容出自上面正文的，挂 source_refs'
                '（[{"source_id":"…","quote":"逐字抄原文连续一段"}]，source_id 照抄那一节'
                "标题里的值）；**每一个节点都要有一条**，正文里找不到依据的方面不要写成"
                "一个节点。"
            )
        elif sections and graph_mode:
            # 两段式的证据是按**概念**检索的（query 就是图谱里的实体名），不是按分支。
            # 说"按分支分组"会让模型去找一条它没见过的一级分支，然后从第一组里挑内容。
            user += ("\n\n下面每个概念都各自检索过一次（查询串就是那个概念名），结果按概念分组；"
                     "引用只能从本组的 evidence 里抄：\n\n" + "\n\n".join(sections))
        elif sections:
            user += ("\n\n下面每条一级分支都各自检索过一次，结果按分支分组；"
                     "引用只能从本组的 evidence 里抄：\n\n" + "\n\n".join(sections))

        # 形状规则按**实际形态**选（不是按"问没问过分组"）：问了但回来只有 8 支时
        # `_skeleton_plan` 已经压平，这里也必须用扁平那份规则，否则 system 要求根下挂
        # 分组、user 里列的却是八条分支，模型只能二选一地写错。
        #
        # 图型也在这里分叉：流程图与鱼骨的"一级分支"分别是阶段与类别，它们的根规则
        # 与树不同（阶段要在阶段内部说出谁是判定，类别下面要写原因而不是词条定义）。
        # 三份规则共用 `_SKELETON_BODY`（不跨支重复 / 这一轮不写 detail），差别只在
        # 根规则与那一段专属说明。
        shape_rules = (
            # 两段式用自己那份：`SKELETON_TREE_RULES` 开头就说"一级分支已经定好了…顺序、
            # 名称都不改"，而两段式的一级分支**正是这一次要模型定的**。给错规则时模型会
            # 等一份它没收到的清单，然后按自己最早读到的"3-6 条分支"随手拆。
            GRAPH_TREE_RULES if graph_mode
            else GROUPED_TREE_RULES if plan.grouped
            else FLOW_TREE_RULES if diagram_type == "flowchart"
            else fishbone_tree_rules() if diagram_type == "fishbone"
            else SKELETON_TREE_RULES
        )
        # 树规则那一份里只有「这一次要多大」那一句与常量不同（`DETAIL_PROMPT_OPEN`：把配额
        # 换成判据）。不再调 `detail_prompt(...)`：它的每一个分支都带一个数，而这一轮要的就是
        # “提示词里一个数都不出现”。
        base_system = DETAIL_PROMPT_OPEN
        # 这一次到底有没有把**结构那一段**（`shape_rules`）给出去。下面的交叉指认规则
        # 要跟着它走：无材料的降级路径不给 `shape_rules`（没有分支要摆），那条 system 里
        # 也就没有"不要跨支重复"这条禁令需要兑现——在那种 system 里教它"用关系指代"，
        # 等于让它去指一棵没被告知形状的树。默认 True，只有最后那个 `else` 会翻。
        structure_rules_given = True
        if sections and full_text:
            # 全塞：引用规则换成"这是全文"那一套。**不能复用 `BRANCHED_EVIDENCE_RULES`**
            # ——它开头就说"已经按一级分支各做过一次检索"，与这次真的没检索相矛盾；
            # 模型会据此推断"没被检索到的内容不存在"，然后只写第一份材料覆盖的部分。
            system = (base_system + thinking + shape_rules
                      + FULL_TEXT_EVIDENCE_RULES + external_rules)
        elif sources and sections and graph_mode:
            # 两段式的证据是按概念检索的（同 `user` 那一段的理由），所以引用规约也要自己
            # 一份：`BRANCHED_EVIDENCE_RULES` 说的是"按一级分支分组"。
            system = (base_system + thinking + shape_rules
                      + GRAPH_EVIDENCE_RULES + external_rules)
        elif sources and sections:
            system = (base_system + thinking + shape_rules
                      + BRANCHED_EVIDENCE_RULES + external_rules)

        elif sources:
            # 有材料但这一轮一条都没命中：与旧的整树路径同一条降级——按无来源写满，
            # 引用留空。**不把 `sources` 判成 None**：模型在本次运行前几轮可能已经
            # `rag_query` 到过真片段，逐字校验照跑，留下的只可能是真依据。
            system = base_system + thinking + shape_rules + DETAIL_NO_SOURCE_RULES
        elif library_section:
            # 这次没有可检索的用户材料/外部检索，但库补了几段（
            # `MINDMAP_LIBRARY_IN_GENERATE`）。**不能走下面 `else` 那一支**：那里拼的
            # `DETAIL_NO_SOURCE_RULES` 明说"没有可标出处的材料"，而 user 末尾正摆着
            # 一节库原文——模型会照着 system 那句话把库里的句子当自己的知识写，
            # 引用一条不给，于是这次库检索白查。
            system = (base_system + thinking + shape_rules + LIBRARY_EVIDENCE_RULES)
        else:
            system = base_system + thinking + DETAIL_NO_SOURCE_RULES
            if graph_mode:
                # 没有可检索材料时上面四条分支都不带 `shape_rules`——三段式在这条路上也
                # 不需要它（没有分支要摆）。两段式仍然需要：那一段正是"一级分支从图谱里
                # 挑"的规则，少了它模型拿到的是一张没有图谱的普通整树提示词，而 user 里
                # 那段实体清单会被它当成参考信息随手引用。
                system += shape_rules
            # 三段式走这一支时 system 里没有结构规则，交叉指认也就无从谈起（见下面那句
            # `elif structure_rules_given`）。
            structure_rules_given = graph_mode
        if library_section and sources:
            # 既有本次材料、又有库片段（薄材料那种：300–3000 字）。上面那几支的引用
            # 规则都只说"从本组抄 / 从正文抄"，库那一节没有归属，要单独补一句。
            system += LIBRARY_EVIDENCE_RULES
        if graph_mode:
            # 关系规则放在**最后**：它与输出形状（`relations` 数组）绑在一起，而模型对
            # 紧挨着输出位置的那段话最敏感（同 `_detail_ask` 末尾那句自查）。
            system += GRAPH_RELATION_RULES
        elif structure_rules_given:
            # 非图谱路（S02 这类：第三档骨架拿到 `GRAPH_OUTLINE_UNAVAILABLE` 之后退回
            # 三段式）此前**只有禁令、没有手段**：`_SKELETON_BODY` 对它说"需要交叉指认
            # 时……直接指向已有节点"，而"指向"的写法只在上面那个 `graph_mode` 分支里
            # 追加。位置与上面那句完全相同，理由也相同——它必须紧挨输出形状。
            #
            # 与 `GRAPH_RELATION_RULES` 互斥：两段式已经有六类 kind 那一份了，再加这一份
            # 会让同一个 `relations` 字段收到两套规矩（上限 30 与 8、六值与前两个值）。
            system += CROSS_REFERENCE_RULES

        if not ctx.reserve_model_call():
            return _fail("budget_exhausted", "本次运行的模型调用额度已用完，无法生成导图内容。")
        ctx.extra_model_calls += 1
        # 通道为 None 时连 `on_delta` 都不传：这条路上工具行为与加生长事件之前逐字
        # 相同（`_GenerateGrowStream` 根本不会被创建）。
        stream_kwargs = {}
        if ctx.emit is not None:
            stream_kwargs["on_delta"] = _GenerateGrowStream(ctx).feed
        structure_started = time.perf_counter()
        try:
            raw = await _chat(ctx, system, user, max_tokens=_max_tokens(ctx), **stream_kwargs)
        except GenerationError as exc:
            return _fail("generation_failed", f"生成失败：{exc}")
        structure_seconds = time.perf_counter() - structure_started

    # 两段式的树与关系在**同一次响应**里。关系先摘出来，理由不是洁癖：
    # `MindMapContent.model_validate` 走 `_validate_relations`，对 dangling 端点是
    # **硬拒**——模型写错一个 id 就会把整棵树判废，而正确的处理是丢掉那条边。
    # 摘出来的关系在裁剪之后清洗（那时节点 id 才定型）。
    #
    # **三处都要摘**：模型经常把 `relations` 塞进 `root` 里面（它把 root 当成"主体"，
    # 于是"与 root 同级"被理解成"和 root 的字段同级"）。实测第一次跑就是这样，
    # 而 `Node` 是 `extra="forbid"`，那一次生成直接判废——提示词已经补了位置说明，
    # 但这件事不能只靠提示词兜。
    #
    # **三段式也摘**（此前不摘，因为那条路没有关系规则，模型本来不该写）：现在
    # `CROSS_REFERENCE_RULES` 明确要求它在结构那一轮写 `relations`，而这条路的端点
    # 写着写着就会写成标题或写错 id——不在这里摘走，`_validate_relations` 会对
    # dangling 端点硬拒，**一条连错的关系换掉整张图**。摘出来交给 `_relations_from`
    # 按同一套过滤收下：认不出端点的丢一条、没写 why 的丢一条，树一个字不动。
    raw_relations: Any = []
    if isinstance(raw, dict):
        raw_relations = raw.pop("relations", []) or []
        root_raw = raw.get("root")
        if isinstance(root_raw, dict):
            nested = root_raw.pop("relations", None)
            if isinstance(nested, list) and nested and not raw_relations:
                raw_relations = nested

    # ---- 收尾：校验、裁剪、体检（与旧的整树出口同一套）--------------------------
    declared_refs = _raw_ref_count(raw)
    verify_sources = _verify_sources(ctx)
    # 判定节点的两条出口在**校验之前**摘出来：`Node` 是 `extra="forbid"`，留着
    # `branch` 这个键整棵树会 422，而它本来就是一条可解析的边（见 `_harvest_flow_branches`）。
    flow_branches: list[_FlowBranch] = []
    if diagram_type == "flowchart":
        raw, flow_branches = _harvest_flow_branches(raw)
    # 证据只在真给过的时候传：空序列与"没给"必须是同一条路（`verify_source_refs`
    # 的空证据等价无证据，用例钉着逐字节不变）。
    evidence = tuple(ctx.evidence) if ctx.evidence else None
    try:
        content, validated = validate_generated_content(
            raw, sources=verify_sources or None, evidence=evidence,
            # 骨架那一步已经判过"这次只有 N 条"并记了 `BRANCHES_BELOW_MIN`：
            # 再在出口用同一条下限判死，等于让那条降级路径一步也走不到（实测：
            # `invalid_map` 与 `too_few_branches` 是同一条判据的两个出口）。
            enforce_min_branches=not branches_degraded)
    except Exception as exc:  # noqa: BLE001 — 校验失败的原因就是要回给模型的文本
        return _fail("invalid_map", f"模型返回的导图不合法：{exc}")
    if branches_degraded and not content.root.children:
        # 放开的只是"少"，不是"没有"：一个光杆根节点没有任何可读内容，
        # 那种图交给用户不如给一句明确的失败。
        return _fail(
            "too_few_branches",
            "模型没给出任何一级分支（只有一个根节点），无法成图。", branches=0)
    diagnostics.extend(validated)
    truncated = _note_truncation(diagnostics, raw)

    # 两级骨架的形状在这里落地（**裁剪之前**：裁剪按节点数砍尾巴，而分组节点必须
    # 已经在那棵树上，否则八十个节点的预算会被晚到的五个分组挤出去）。
    # 模型照规则写时这一步什么都不做（返回原对象、`moved == 0`），写歪了它会搬位置
    # ——判据与理由见 `_apply_groups`。
    #
    # 两段式没有骨架产物（`plan is None`）：一级分支是模型直接写进树里的，没有第二份
    # 形状要摆正，分组这一层在它那里也不存在（图谱分支天然扁平）。
    groups_moved, group_ids = 0, []
    if plan is not None:
        content, groups_moved, group_ids = _apply_groups(content, plan)
        if plan.grouped and not group_ids:
            # 一组都没落地（模型把这一层写成了别的东西，连标签都对不上）：退回扁平。
            # 不这么退的话，下游（`_apply_skeleton` 的配色、`data["skeleton"]` 的形状说明、
            # 第三段的 `skip_ids`）都会按"这是两级"走，而树上是一层——那种半开状态比
            # 退化本身更难查。
            plan = _SkeletonPlan(flat=plan.flat)
        if groups_moved and plan.grouped:
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.info, code="SKELETON_GROUPS_APPLIED",
                message=(
                    f"模型没有完全按两级骨架落树，已按骨架的分组摆正"
                    f"（搬动/补出 {groups_moved} 个一级节点）：分组与分支的归属来自骨架"
                    f"那一次调用，内容一个字都没有改"
                ),
            ))

    # ---- 超出可读性上限：不折、不裁，只报数 ------------------------------------------
    #
    # 这一段的历史有两代，都被撤掉了，撤的理由不一样：
    #
    #   `_clip_tree(content, initial_nodes)`  按前序砍尾巴。坏形态：后几条一级分支整条
    #       消失；小配额下 3 支被砍成 1 支（用户选了 6 条分支，拿到 1 条）。
    #   `_fold_tree(content, initial_nodes)`  把多出来的兄弟并成「其他 N 项」。坏形态：
    #       为了少 5 个节点折掉 38 个——`cap=4` 那一趟在根上做的第一件事就是把后 4 条
    #       分支整体收进一个节点（实测 85/8 支 → 47/5 支）。而且不可逆：被折掉的子树
    #       已经不在树里，`node_expand` 是模型重新生成，拿不回原来那几个。
    #
    # 共同点是**替用户做了一个他没要求过的决定**：把内容拿走，换一个据说读得完的形状。
    # 可读性是渲染层能处理的事（缩放、平移，以及将来画布上的折叠），内容层只该如实交付。
    #
    # 契约上限那一道**不需要在这里补**：超过 `MAX_TREE_NODES`(1000) 的产物在
    # `_validate` 那一步就被整棵树拒掉（`invalid_map`），走不到这里——这也说明
    # "存不下"从来是契约在管的事，`INITIAL_MAX_NODES` 从头到尾只是可读性口味。
    tree_nodes = _count_nodes(content.root)
    if tree_nodes > initial_nodes:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.info, code="TREE_OVER_READABILITY",
            message=(
                f"模型写了 {tree_nodes} 个节点、{len(content.root.children)} 条一级分支，"
                f"超过可读性参考值 {initial_nodes}：内容一个都没有折、也没有裁，"
                "画布上每一处都在。嫌挤可以缩一档看图，或者删掉几支不需要的"
                "（以前这里会把几条分支并成一行「其他 N 项」，那等于把结构丢掉）"
            ),
        ))

    # 骨架的产物（颜色 / 图标 / flowgraph 的边）套上去。放在树定形之后：端点表必须
    # 用最终还活着的 id 建，否则 `clean_edges` 会把边留在指向已消失节点的状态。
    #
    # 两段式跳过：它的颜色是结构那一次直接写在节点上的（一级分支由模型自己挑，
    # 不存在"骨架产物与树按第几支对齐"这件事），`raw_plan` 也是 None。
    if plan is not None:
        content, edge_diagnostics = _apply_skeleton(
            content, plan, raw_plan, diagram_type=diagram_type)
        diagnostics.extend(edge_diagnostics)

    # 跨分支关系（两段式才有）。放在树定形之后：端点表要按还活着的节点建，
    # `_relations_from` 的第一道过滤就是"端点不在树里就丢"——拿一张旧表建，
    # 留下来的会是几条指向已消失节点的虚线，而契约会因此拒收整张图。
    relation_drops: dict[str, int] = {}
    #: 这一次收关系的上限：两段式 30（画布能放多少），三段式 8（规则里那个数，
    #: `CROSS_REFERENCE_MAX`——超过 8 条正确的动作是合并节点）。放在这里而不是在
    #: 调用处各写一遍：`data` 里的 `relation_limit` 与诊断里那句话必须同源。
    relation_limit = GRAPH_MAX_RELATIONS if graph_mode else CROSS_REFERENCE_MAX
    # 条件里带 `raw_relations`：三段式没写关系时**不进这里**（既不建空数组，也不
    # 报一条"写了 0 条收下 0 条"的诊断——那是噪声）。
    if graph_mode or raw_relations:
        relations, relation_drops = _relations_from(
            raw_relations, content, limit=relation_limit)
        if relations:
            content = content.model_copy(update={"relations": relations})
        if sum(relation_drops.values()):
            breakdown = [
                ("端点对不上", relation_drops.get("dangling", 0)),
                ("没写 kind 或 why", relation_drops.get("no_reason", 0)),
                ("两端同父（树已表达）", relation_drops.get("same_parent", 0)),
                ("重复", relation_drops.get("duplicate", 0)),
                ("置信度太低", relation_drops.get("low_confidence", 0)),
                ("超出上限", relation_drops.get("limited", 0)),
            ]
            declared = len(raw_relations) if isinstance(raw_relations, list) else 0
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn,
                code="GRAPH_RELATION_DROPPED" if graph_mode else "CROSS_REFERENCE_DROPPED",
                message=(
                    f"模型写了 {declared} 条交叉指认，收下 {len(relations)} 条"
                    f"（上限 {relation_limit}）："
                    + "、".join(f"{name} {count} 条" for name, count in breakdown if count)
                    + "。被丢掉的不影响树，只是画布上少一根虚线"
                ),
            ))

    # 判定节点的「是 / 否」两条出边（阶段**内部**的边）。也放在裁剪之后，理由与上面
    # 那一句相同：端点表要按裁剪后还活着的节点建，否则 `clean_edges` 会把一条指向
    # 已消失节点的边留在树里。这里只解析成**树命名空间**的边，分流到
    # `FlowStage.edges` / `FlowContent.edges` 是 `_shape_output` 收尾时的事。
    if flow_branches:
        branch_report = _flow_branch_edges(flow_branches, content)
        if branch_report.edges:
            kept, branch_diag = clean_edges(
                branch_report.edges, shapes.node_ids(content))
            diagnostics.extend(branch_diag)
            # 骨架的阶段间边在前、判定出边在后：`clean_edges` 去重按
            # `(source, target, label)`，两批之间的重复（同一条出口被骨架也画了一次）
            # 会在保存时被契约层去重，这里不合并判一次是因为那会把骨架那批的
            # 诊断再报一遍。
            content = content.model_copy(update={"edges": [*content.edges, *kept]})
        dropped = branch_report.lost_sources + branch_report.unresolved
        if dropped:
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn, code="FLOW_BRANCH_UNRESOLVED",
                message=(
                    f"{dropped} 条判定出口没有落到节点上（判定节点不在最终树里 "
                    f"{branch_report.lost_sources} 条 / 目标写了一个对不上的标题 "
                    f"{branch_report.unresolved} 条）：这些判定在画布上会缺一条分支。"
                    "保留的边数见本条诊断之外的结构说明"
                ),
            ))

    # ---- 检查点：树已经能读了，落一份再往下走 -------------------------------
    #
    # 从这里到 `publish_tree` 之间还要跑 `_fill_details`（实测 26 秒上下）与
    # `check` / `annotate_nli` / `_cross_branch_duplicates`。那些时间里这棵树已经
    # 过契约校验、已裁剪、骨架已套，**是合法的完整树，只是 detail 全空**——进程
    # 被杀、重启、合盖，这一轮本该白跑，有了这一步至少留下结构。
    #
    # 更早不行：中途只有 `node_grow` 预览帧，那些节点只有 id 与 label，没有引用、
    # 没有 `expandable`，写进库是一棵契约不合法的树，还得为它再写一套读取格式。
    # 更晚不必：`publish_tree` 那一刻整棵树已经写进 `ctx.candidate`，`on_close`
    # 兜得住。
    #
    # **存下去的那一份必须是用户要的图型**：真正的折叠在出口那一步（`_shape_output`）
    # 才做，所以那时 `content` 还是一棵内部树。直接把它存进库，崩溃后捡回来的就成
    # 了一张思维导图——而用户当初要的是鱼骨图/流程图，他会以为这一轮连图型都没认对。
    # 这里对**副本**先折一次：折叠是纯函数（`to_fishbone` / `to_flow` 都不就地改输入），
    # 折的是副本，下游 `_fill_details` 等仍按一棵树工作。
    #
    # 诊断给一次性空表：那是**最终产物**的读数，一份快照不该替它提前报一遍，也不该
    # 把同一批诊断算两次。
    #
    # `expandable` 要在快照上补一次：正式那一次打标在这条线之后（`_stamp_expandable`），
    # 快照里的节点因此还是缺省的 `unknown` 之外的旧值。这里对副本按同一张表打一遍，
    # 让"捡回来的那一份"与"最终交付的那一份"在三态上一致。
    if ctx.checkpoint is not None:
        snapshot = content.model_copy(deep=True)
        _stamp_expandable(snapshot, {}, max_depth=mindmap_contract.MAX_TREE_DEPTH)
        await ctx.checkpoint(_shape_output(
            snapshot, diagram_type=diagram_type,
            raw_plan=raw_plan, diagnostics=[]))

    # ---- 结构自检回路（`MINDMAP_SELF_CHECK`，默认关，+1 次调用）-----------------
    #
    # **位置：结构那一次之后、说明那一次之前。** 三条理由：
    #
    #  1. 那一刻标签已经定稿：说明段只填 `detail`，它一个 `label` 都不会改，所以
    #     再往后等，清单的输入一个字都不会多——只是把用户多按五秒；
    #  2. 往前不行：结构之前树上只有骨架，下位节点还不存在，审出来的问题一半是
    #     "这一支是空的"；
    #  3. 它和说明段**串行**：说明段本身是两半并发，把它插在说明之后就是"并发跑完
    #     再单独等一次"，插在之前则是"先把这一次等完、再并发写说明"。总耗时一样，
    #     但前者的清单出来时用户已经在等最后一步了。
    #
    # **它不改树**（`content` 原样往下走）：精度 22/25 = 88%，自动落地会伤到那
    # 12%，而合并不可逆。产出是清单 + 一条诊断，落地由用户决定——这也正是他要的
    # "哪一支错了能定位"。
    #
    # **只在树上做**：鱼骨的一级是固定类别、流程的一级是阶段，`SELF_CHECK_PROMPT`
    # 里那四类问题的措辞是按"分支"写的（与 `optimization` 的 tree-only 闸同一条理由）。
    self_check_report: _SelfCheckReport | None = None
    if diagram_type == "tree" and self_check_enabled():
        self_check_report = await _run_self_check(content, ctx, topic=topic)
        if self_check_report.skipped in ("error", "budget"):
            if self_check_report.skipped == "budget":
                reason = "本次运行的模型调用额度已用完"
            else:
                reason = (f"调用失败：{self_check_report.failure}"
                          if self_check_report.failure else "调用出错")
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn, code="SELF_CHECK_SKIPPED",
                message=(f"这次没做结构自检（{reason}）；这张图本身不受影响，"
                         "只是没有那份「模型自己回头看到的问题」清单。"),
            ))
        elif self_check_report.counts:
            head = "、".join(
                f"{_SELF_CHECK_KIND_NAMES.get(kind, kind)} {count} 处"
                for kind, count in sorted(self_check_report.counts.items(),
                                          key=lambda pair: -pair[1]))
            sample = "；".join(
                f"「{issue['path']}」（{_SELF_CHECK_KIND_NAMES.get(issue['kind'], issue['kind'])}"
                + (f"：{issue['problem']}" if issue["problem"] else "") + "）"
                for issue in self_check_report.issues[:3])
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.info, code="SELF_CHECK_ISSUES",
                message=(
                    f"模型自检发现 {len(self_check_report.issues)} 处可能的问题（{head}）："
                    f"{sample}"
                    + ("…" if len(self_check_report.issues) > 3 else "")
                    + "。**没有自动改**——这份清单是给你核对的，"
                    "改哪个由你决定（清单在这一次运行的读数里）。"
                ),
            ))

    # ---- 第四步（三段式的第三段）：只写 detail ---------------------------------
    #
    # 放在这里而不是更早：裁剪已经做完（被裁掉的节点不必为它付说明的 token），
    # 骨架的颜色/边已经套上（它们与本段无关，但重建节点要带上），而 `check()` 与
    # `annotate_nli()` 都还没跑——那两个函数读的是**最终**的 detail，说明必须在它们
    # 之前落地。
    # 第三段：**分组节点不进清单**（`skip_ids`）。分组是分类名、没有内容，给它写一段
    # 说明只会得到一句"这一组在讲什么"的空话，而它占掉的正是那二十节里某一节的输出
    # 预算。这一步因此**不因为分组多花一次调用**——它本来就是按节点数切两半的。
    #
    # `chapters` / `source` 只在按章切真的生效时才传：这条路唯一的输入是"哪一章"，
    # 没有章就退回"整支/整份"那份证据（`MINDMAP_DETAIL_RETRIEVAL` 的两条前置条件）。
    # 传的是**单份材料本身**：`_chapter_slices` 只在恰好一份材料时才产出章，两者同源。
    content, detail_report = await _fill_details(
        content, ctx=ctx, topic=topic, sections=sections,
        external_rules=external_rules, diagnostics=diagnostics,
        # 两级骨架的分组节点不进这份清单：它是分类名，写出来只有一句"这一组在讲什么"
        # 的空话，而它占掉的正是那二十节里某一节的输出预算。
        skip_ids=[*group_ids],
        full_text=full_text,
        chapters=tuple(slice_chapters or ()),
        source=sources[0] if slice_chapters else None,
        # 每段说明写多长（`MINDMAP_DETAIL_SCALE`）：口径与节点配额同一行代码，
        # `max(...)` 的理由也一样——外部检索补进来的那份也在这一次的 prompt 里。
        material_chars=max(material_chars, full_text_chars))

    # `expandable` 三态：这一轮没有任何节点因为"装不下"被拿走（不折也不裁），所以
    # 除了契约的 8 层算"到底了"，其余一律 `yes`——它们本来就能再展开。
    _stamp_expandable(content, {}, max_depth=mindmap_contract.MAX_TREE_DEPTH)

    # ---- 一级分支的概括（`MINDMAP_BRANCH_DIGEST`，默认关）---------------------
    #
    # 位置就在说明之后：一级分支已经是最终形态（分组 / 裁剪 / 配色全部落定），下位
    # 节点的标题也定稿了，而 `check()` 还没跑（它是复核，该读最终内容）。往前挪到
    # 结构那一步之后也调得通，但那时分支标题还是骨架原话，写出来的概括会在二十秒
    # 后过期。
    #
    # 产物进 `content.branch_digests`（契约字段，**会落库**——"这一支讲什么"是这
    # 张图的属性，刷新一次就没了的东西不该占契约位置，而每次重开都再调一次模型是
    # 拿钱买一个本来就在手里的答案）。**不进 `data`**：`data` 是一次运行的读数。
    #
    # 关着时这一段一行都不执行：`content` 一个字节不动、`data` 的键集不变、模型
    # 调用次数不变——`test_edges` 那批逐字节回归用例钉的就是这条。
    digest_report: _BranchDigestReport | None = None
    if diagram_type == "tree" and branch_digest_enabled():
        content, digest_report = await _fill_branch_digests(content, ctx, topic=topic)
        if digest_report.skipped in ("error", "budget"):
            # 一句概括没写成不该让整棵树失败，但也不能沉默：用户开了开关、界面上却
            # 只有数字，唯一的线索就是这一条。
            reason = (
                "本次运行的模型调用额度已用完" if digest_report.skipped == "budget"
                else f"调用{('失败：' + digest_report.failure) if digest_report.failure else '出错'}"
            )
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn, code="BRANCH_DIGEST_SKIPPED",
                message=(
                    f"一级分支的概括没写成（{reason}）；"
                    "画布上的角标只显示「还有 N 个」，没有那句概括。"
                ),
            ))
        elif digest_report.missing:
            labels = {node.id: node.label for node in content.root.children}
            head = "、".join(
                f"「{labels.get(node_id, node_id)}」" for node_id in digest_report.missing[:5])
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.info, code="BRANCH_DIGEST_INCOMPLETE",
                message=(
                    f"{len(digest_report.missing)}/{digest_report.branches} 条一级分支没写出"
                    f"概括（{head}{'…' if len(digest_report.missing) > 5 else ''}）；"
                    "这几支的角标只显示「还有 N 个」。"
                ),
            ))

    # ---- 收尾的确定性修复（零模型调用，`MINDMAP_AUTO_REPAIR`）-------------------
    #
    # **位置就在"七条结构规则"这一带，而且必须排在 `check()` 之前**：`check()` 是
    # 复核、`repair()` 是动作。反过来（先报后修）的结果是诊断里躺着一批刚刚被修掉
    # 的码，用户点开面板看到 116 条 `SINGLE_CHILD`、而树上一个都没有——一个"报的
    # 和看到的不一致"的面板比没有面板更糟。
    #
    # **为什么要在这里修、而不是留给用户点「一键修复」**：修复的输入是模型这一次
    # 写下的树，输出是同一棵树的去冗余版本，中间没有任何决策需要用户做——它就是
    # "这一次生成"的一部分。放到界面上让用户点，等于让他为一段自己没看见过的中间态
    # 付账（前端 `replaceContent` 的注释记着同一件事：撤销粒度按"用户按了几次扳机"
    # 分）。在这里修完再 `publish_tree`，撤销栈里仍然是**一条**（这一次生成），
    # 用户按一次 Ctrl+Z 回到生成之前——而不是"第 1 处修复之前"。
    #
    # **只修三条**（`AUTO_REPAIRABLE_CODES`）不是保守，是判据：剩下四条各自的不唯一
    # 性是具体的——`NEAR_DUPLICATE_LABEL` 要人判（字面像不等于语义同，实测 40 对里
    # 0 对真近义）、`SHALLOW_BRANCH` / `MISSING_DETAIL` 要模型写内容、`DEPTH_LIMIT`
    # 没有可执行动作。把它们自动落地等于替用户做一次不可逆的决定。
    #
    # **只在树上做**：鱼骨的六根骨是并列类别、流程的阶段是各自独立命名空间，
    # `check()` 那七条判据是按树的形状写的（与 `optimization/router.py::_TREE_ONLY`
    # 同一条理由）。诊断在别的图型上照旧会响，但**动手改**是另一件事。
    #
    # **一个副作用要交代**：压链保留内层的 id，所以一级分支可能换 id，而概括
    # （`branch_digests`）是按外层的 id 写的。那几条概括的键在树上已经不存在了——
    # **不搬给内层**：那句话说的是"外层那支讲什么"，挂到另一个标题底下是错的归属。
    # 契约不校验这张表的键（`MindMapContent.branch_digests` 的注释），渲染层按 id
    # 查不到就不画，角标退回只显示数字。这里顺手把死键删掉，免得落库一份指向不存在
    # 节点的表。
    #
    # **跨支重复的判据要跑在这次修复之前**（`duplicates` 在下面几行就取好了）：
    # 压链会改 label（`外层 / 内层`），而 `_cross_branch_duplicates` 只比逐字。
    # 先修后判的话，"两个一级分支下各写了一个「迁移学习」、而两支都只有这一个孩子"
    # 这种形状会在修完之后变成「甲 / 迁移学习」与「乙 / 迁移学习」，判据就再也看不见
    # 它了——**而那是模型真的写了两遍**。诊断说的是"模型写了什么"，不是"修完之后
    # 还剩什么"，所以取修复之前的那一份（节点 id 在压链后仍然有效：压链保留内层 id）。
    duplicates = _cross_branch_duplicates(content.root)
    auto_repair: dict[str, Any] | None = None
    #: 修复前的一级分支数。修复**能**把它压到 `MIN_BRANCHES` 以下（合并两个同名的一级
    #: 分支就是 5 条变 4 条），而生成入口那道 `enforce_min_branches` 在修复**之前**就
    #: 判完了——不留这个数，"修复把一张合规的图改成分支不足"这件事没人能量到。
    branches_before = len(content.root.children)
    if diagram_type == "tree" and auto_repair_enabled():
        nodes_before = shapes.count_nodes(content)
        outcome = repair_structure(content, codes=AUTO_REPAIRABLE_CODES)
        if outcome.steps:
            content = outcome.content
            if isinstance(content, MindMapContent) and content.branch_digests:
                top_level = {node.id for node in content.root.children}
                kept = {key: text for key, text in content.branch_digests.items()
                        if key in top_level}
                if len(kept) != len(content.branch_digests):
                    content = content.model_copy(update={"branch_digests": kept})
        per_code: dict[str, int] = {}
        for item in outcome.applied:
            per_code[item.code] = per_code.get(item.code, 0) + 1
        # 每一步做了什么：**这是"不悄悄改"的可查证据**。
        #
        # 诊断只说一句"N 处"，用户能接受"收尾帮我修了"的前提是**能查清改了哪些**
        # ——每条都带 `RepairItem.message`（「把「甲」并进了同级同名节点「乙」：子树
        # 取并集（2 + 1 个），引用取并集（0 条）」）与被动过的节点 id。上限 30 条：
        # 实测真实产物单张最多 74 步（190 节点那张），全量塞进 `data` 会把一次运行的
        # 读数变成一份日志；超出部分报数（`items_truncated`），不静默截断。
        listed = [
            {"code": item.code, "node_id": item.node_id, "message": item.message}
            for item in outcome.applied[:AUTO_REPAIR_MAX_ITEMS]
        ]
        auto_repair = {
            "steps": outcome.steps,
            "codes": per_code,
            # 看到了但没修的（引用并集超过契约上限、或被合并后子节点超过 50 个）。
            # **必须报出来**：静默跳过会让"这棵树怎么还有一处重名"变成一个只在
            # 用户那里发生、查不出来的问题。
            "skipped": [
                {"code": item.code, "node_id": item.node_id, "reason": item.reason}
                for item in outcome.skipped
            ],
            "items": listed,
            "items_truncated": max(0, outcome.steps - len(listed)),
            "nodes_before": nodes_before,
            "nodes_after": shapes.count_nodes(content),
        }
        if outcome.steps or outcome.skipped:
            detail = "、".join(
                f"{_AUTO_REPAIR_RULE_NAMES.get(code, code)} {count} 处"
                for code, count in sorted(per_code.items(), key=lambda pair: -pair[1]))
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.info, code="STRUCTURE_AUTO_REPAIRED",
                message=(
                    f"生成收尾顺手修了 {outcome.steps} 处结构冗余"
                    + (f"（{detail}）" if detail else "")
                    + f"，节点 {nodes_before} → {auto_repair['nodes_after']}。"
                    "只动了「修法唯一」的那几条：被并掉节点的子树、引用、说明都并进了"
                    "保留者，没有删掉任何内容"
                    + (f"；另有 {len(outcome.skipped)} 处没修（合并后超出契约上限或诊断"
                       "过期），它们在结构体检里仍然看得到"
                       if outcome.skipped else "")
                    + "。"
                ),
            ))

    # ---- 修完之后再量一遍：修复自己也可能造出"没人量"的缺陷 --------------------
    #
    # 这一段的来历是一个真实的回归：压链把两层的标题拼成一句长话（31 字的
    # 「基础：CSS 布局与事件循环清楚 / 浏览器渲染流程回答不完整」），契约（200 字）不
    # 管它、`check()` 七条码里也没有"标题太长"这一条，于是它一路交付到画布上才被
    # `line-clamp-2` 截成半句。修法已经改了（`repair._joined_label`），但**"改对了"
    # 与"以后不会再错"是两件事**：没有这一道闸，下一次改动会以同样的方式再漏一遍。
    #
    # 两处都跑、分别报，而不是把生成入口那条挪到后面：
    #   生成入口那条（`LABEL_CLIPPED_TO_GENERATED`）说的是"**模型**写了超过 16 字的
    #   标题，已截短"；这一条说的是"**交付的这棵树上**有节点会被截成半句"。合成一条
    #   就再也分不开"模型写长了"与"修复拼长了"这两种失效。
    if isinstance(content, MindMapContent):
        overlong = overlong_labels(content)
        if overlong:
            preview = "、".join(f"「{label}」" for _, label in overlong[:3])
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn, code="LABEL_OVERLONG_IN_DELIVERY",
                message=(
                    f"{len(overlong)} 个节点的标题超过 {READABLE_LABEL_CHARS} 字，画布上"
                    f"会被截成半句（{preview}{'…' if len(overlong) > 3 else ''}）；"
                    "完整标题在悬停提示里，长内容应该在 detail 里"
                ),
            ))
        # 修复把一级分支压到下限以下：生成入口那道闸判的是修复**之前**的树，所以
        # "合并两个同名一级分支"这条唯一能让它再掉一档的动作，修完没人复核。
        branches_after = len(content.root.children)
        if branches_before >= MIN_BRANCHES > branches_after:
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn, code="STRUCTURE_FEW_BRANCHES_AFTER_REPAIR",
                message=(
                    f"收尾修复把一级分支从 {branches_before} 条并成了 {branches_after} 条"
                    f"（少于 {MIN_BRANCHES} 条），这棵树只剩 {branches_after} 支；"
                    "合并的是同名分支，内容并进了保留者，要拆开可以撤销这一步"
                ),
            ))

    # 结构与语义两层复核（与 `_generate_deep` 的收尾同一份）：诊断只是提示，
    # 不影响这棵树能不能用——契约校验在上一步就决定过了。
    #
    # **剔掉 `check()` 里那条 `CROSS_BRANCH_DUPLICATE`**（判据只有一份，在
    # `optimization/cross_branch.py`，`check()` 自然会产出它）。剔的原因是**判在
    # 什么树上**：`check()` 跑在收尾修复**之后**，而这条诊断说的是"模型写了什么"，
    # 它的判据必须跑在修复**之前**——压链会改 label（`外层 / 内层`），先修就判不到
    # 了。所以这里用上面那个修前取好的 `duplicates` 自己说一遍，位置与文案都与把
    # 判据搬走之前逐字相同（`test_cross_branch_duplicates_are_judged_before_the_repair`
    # 钉着这个顺序）。
    diagnostics.extend(item for item in check(content)
                       if item.code != "CROSS_BRANCH_DUPLICATE")
    if verify_sources:
        diagnostics.extend(annotate_nli(content, verify_sources))

    # 跨支重复：分片那条路的结构性缺陷，也是这次回到一次生成要说清的那条收益。
    # **只报不删**（判据与理由见 `_cross_branch_duplicates`）。放在 `check()` 之后：
    # 它比同级重名更"远"，读诊断的人应该先看到结构问题、再看到内容重复。
    #
    # 判据本身跑在**修复之前**（见上面那一段最后一条注释）：这里只负责把它说出来，
    # 所以这一行不再是计算，`duplicates` 早就取好了。
    if duplicates:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="CROSS_BRANCH_DUPLICATE",
            message=(
                f"{sum(len(places) for _, places in duplicates)} 个节点跨分支重复"
                f"（{len(duplicates)} 个说法各写了 "
                f"{'/'.join(str(len(places)) for _, places in duplicates[:3])} 遍）："
                + "；".join(
                    f"「{label}」（{'、'.join(places)}）" for label, places in duplicates[:5]
                )
                + ("…" if len(duplicates) > 5 else "")
                + "。重复的节点没有被删——要保留哪一处由你决定。"
            ),
        ))

    # 退化成"只凭主题"的那一版：请求带了材料，但这次一条依据都没拿到。
    #
    # **全塞不算退化**（`not full_text`）：那条路上根本没检索，`with_evidence` 恒为 0，
    # 不排除它的话，每一次成功的全塞生成都会被判成"材料里没有相关内容"——一条在
    # 事实上错、而且会盖住真正的检索失败（有材料、检索返回空）的诊断。
    unsourced = bool(sources) and not full_text and not with_evidence

    if unsourced:
        # 逐节点重复几十遍的 `UNSOURCED_NODE` 会淹掉别的诊断，聚成一条说清
        # "为什么整张图没有引用"（与 `_generate_deep` 的处理一致）。
        diagnostics = [item for item in diagnostics if item.code != "UNSOURCED_NODE"]
    external_empty = external is not None and not external.sources
    external_used = external is not None and bool(external.sources)
    # 第三种：闸门关着，这次压根没出去搜。判据是"没有材料 且 外部分支没跑过"——
    # `_intake` 里只有一种情况会同时满足：`not sources` 为真却没进那个 `if`，
    # 也就是 `external_knowledge_enabled()` 是假。
    #
    # **只认"用户在界面上关的"这一种原因**。部署方关掉（环境变量）时这里一律为
    # False，行为与加这个开关之前逐字节相同：那种情况下这台机器上不了网是一个事实，
    # 每次生成都多一句"联网检索被部署方关掉了"是噪音，用户也没有任何动作可以做。
    # 而用户自己关的时候，沉默就变成了谎话——他会以为搜了但没搜到。
    external_off = bool(external is None and not sources
                        and external_knowledge_off_reason() == "user")

    # ---- 出口：把内部那棵树折成请求的图型 ---------------------------------------
    #
    # 这是"三条生成路径各产出自己的结构"落地的地方，也是整条流水线上唯一认识
    # `FishboneContent` / `FlowContent` 的一行。上面全部步骤都在树上跑（理由见
    # `_shape_output`），换形状只发生在交出产物之前这一刻。
    #
    # **核对这一步排在折形状之前**：它要往节点上挂引用，而折形状会把树拷进
    # `bones[i].root` / `stages[i].root`——挂完之后再折，引用跟着一起过去；反过来
    # 就要在两套结构上各挂一遍。
    #
    # `refs_before_verify` 只在开核对时量：下面那两条诊断（`EVIDENCE_UNUSED` /
    # `MATERIAL_UNUSED`）问的是"生成那一步给的证据有没有被用上"，核对补的引用不算
    # 数——让它算进去，一次"模型一条都没抄"就会被核对挂上的几条洗成"用上了"。
    # 开关关着时它是 None，那两条诊断读到的仍是 `kept_refs`，与加这段之前逐字节相同。
    refs_before_verify: int | None = None
    verify_data: dict[str, Any] | None = None
    if verify_step.verify_enabled() and isinstance(content, MindMapContent):
        refs_before_verify = shapes.count_refs(content)
        verify_data = await _verify_after_generate(
            content, ctx=ctx, sources=verify_sources, diagnostics=diagnostics)
    content = _shape_output(
        content, diagram_type=diagram_type, raw_plan=raw_plan,
        diagnostics=diagnostics)

    ctx.publish_tree(content)
    count = shapes.count_nodes(content)
    kept_refs = shapes.count_refs(content)
    # 引用条数**两处口径各一个名字**：生成那一笔用 `generated_refs`（核对之前量的），
    # 交付时树上有几条用 `kept_refs`。开关关着时两者恒等。
    generated_refs = kept_refs if refs_before_verify is None else refs_before_verify

    # 证据拼进了 prompt、树上却一条引用都没留下：`rag` 形态下唯一没有信号的失败面
    # ——"把证据给了模型"与"模型用了证据"在产出上长得不一样，而失败的样子却与
    # "本来就没有证据"完全相同。分母按 `(source_id, start, end)` 去重后数，
    # 逐支相加会把同一段命中说成好几条。
    evidence_total = len({
        (entry.get("source_id"), entry.get("start"), entry.get("end"))
        for branch in evidences if branch is not None
        for entry in branch.payload
    })
    evidence_unused = bool(evidence_total) and not generated_refs
    if evidence_unused:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="EVIDENCE_UNUSED",
            message=(
                f"本次把 {evidence_total} 条检索到的原文（按支分组）拼进了提示词，"
                f"但生成的 {count} 个节点最终 0 条 source_refs"
                + (f"（模型写了 {declared_refs} 条，全部没通过逐字校验）"
                   if declared_refs else "（模型一条引用都没有写）")
                + "；证据给了却没被用上，不等于材料里没有相关内容"
            ),
        ))
    # 全塞的同一件事，**另起一个码**：失败面和按支那条不一样。这里"材料全在
    # prompt 里"是既成事实，零引用只能是模型没抄（提示词/模型问题），没有"检索
    # 没召回"这一档可能；混进 `EVIDENCE_UNUSED` 会让"分支标题写歪了"与"全文给了
    # 也不抄"在遥测里再也分不开，而用户能做的动作完全不同。
    material_unused = bool(full_text) and bool(sections) and not generated_refs
    if material_unused:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="MATERIAL_UNUSED",
            message=(
                f"本次把 {len(sections)} 份材料的全文（合计 {full_text_chars} 字）"
                f"整份拼进了提示词，但生成的 {count} 个节点最终 0 条 source_refs"
                + (f"（模型写了 {declared_refs} 条，全部没通过逐字校验）"
                   if declared_refs else "（模型一条引用都没有写）")
                + "；全文就在上下文里，没抄不等于材料里没有相关内容"
            ),
        ))

    if unsourced:
        reason = ("检索出错" if any(item is None for item in evidences) and not sections
                  else "材料里没有查到与这个主题相关的原文片段")
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="GENERATION_WITHOUT_EVIDENCE",
            message=(
                f"{reason}：这张图只基于主题「{topic}」生成，"
                "没有任何材料依据，节点上也不会有引用"
            ),
        ))

    # 三种退化各自一条诊断 + 一句给用户的话，三句话术互不相同：用户要能从
    # `content` 里读懂"这次为什么没有引用（或为什么有）"。与 `_generate_deep`
    # 那三段的措辞保持同源，同一个功能在两条路上不该有第二套说法。
    lead: list[Diagnostic] = []
    short_note = None
    if short_material:
        short_note = (
            f"材料只有 {material_chars} 字，不足以支撑检索"
            f"（低于 {SHORT_MATERIAL_CHARS} 字时整份材料塞得进一条引用，引用落不到具体段落），"
            "这次按主题生成"
        )
        if with_evidence:
            short_note += (f"，并用外部检索到的 {len(sources)} 条资料"
                           f"（{_external_source_names(sources)}）补全依据。")
        elif external_used:
            short_note += "，只用模型自己的知识（外部资料没能检索出可用片段）。"
        elif external is not None:
            short_note += "，只用模型自己的知识（联网检索没有找到相关资料）。"
        else:
            # 到这一支时 `external is None` 只可能是闸门关着：材料被判成"太短"之后
            # `sources` 为空，闸门开着就一定会去搜一次。于是"谁关的"决定这句话——
            # 用户自己关的要说出来（他能去打开），部署方关的保持原样。
            short_note += (
                "，只用模型自己的知识"
                f"（{EXTERNAL_OFF_NOTE if external_off else '外部检索未开启'}）。")
        lead.append(Diagnostic(
            level=DiagnosticLevel.warn, code="MATERIAL_TOO_SHORT",
            message=(
                f"材料总字数只有 {material_chars} 字（阈值 {SHORT_MATERIAL_CHARS} 字）："
                f"这个长度切不出可区分的检索单元，按材料生成只会得到"
                f"「引用落不到具体段落」的假依据，所以这次没有走材料路径，"
                f"{len(short_material)} 份材料只当作主题提示"
                + ("，依据由外部检索补上" if with_evidence else "，依据来自模型自己的知识")
            ),
        ))
    external_used_note = None
    if external_used:
        lead.append(Diagnostic(
            level=DiagnosticLevel.info, code="GENERATION_FROM_EXTERNAL",
            message=(
                f"本次引用来自外部检索的 {len(external.sources)} 条资料"
                f"（{_external_source_names(external.sources)}）；"
                "逐字校验保证引文出自这些来源，不保证来源本身与主题高度相关"
            ),
        ))
        if not short_material:
            external_used_note = (
                f"本次有 {len(external.sources)} 条依据来自外部检索"
                f"（{_external_source_names(external.sources)}）："
                "引用逐字可校验、能点回原文，但外部内容不像你上传的材料那样权威，"
                "当作参考资料看即可。"
            )
    external_note = None
    if external_off:
        # 与下面那一支**不是同一个 code**：`GENERATION_WITHOUT_EXTERNAL` 的语义是
        # "试过了，一条都没拿到"（可以换个时间重试）；这一支是"根本没试"（重试一百
        # 次也一样，除非去把开关打开）。遥测按 code 聚合，混成一个码会让"外部源
        # 挂了"与"用户关了开关"在报表里再也分不开。
        external_note = EXTERNAL_OFF_NOTE
        # 码与下面那支分开：`GENERATION_WITHOUT_EXTERNAL` 的语义是"试过了，一条都没
        # 拿到"（可以换个时间重试），这一支是"根本没试"（重试一百次也一样，除非去把
        # 开关打开）。遥测按 code 聚合，混成一个码会让"外部源挂了"与"开关是关的"
        # 在报表里再也分不开。
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.info, code="GENERATION_EXTERNAL_DISABLED",
            message=(
                f"{EXTERNAL_DISABLED_NOTE}（主题「{topic}」）"
                "打开联网检索再生成，引用就能逐字校验、点回原文"
            ),
        ))
    elif external_empty:
        external_note = (f"外部检索出错（{external.failure}）" if external.failure
                         else "联网检索没有找到相关资料")
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="GENERATION_WITHOUT_EXTERNAL",
            message=(
                f"{external_note}：这张图只基于主题「{topic}」生成，"
                "没有外部资料依据，节点上也不会有指向原文的引用"
            ),
        ))
    diagnostics = lead + diagnostics

    readiness = _source_readiness_diagnostic(ctx)
    if readiness is not None:
        diagnostics.append(readiness)

    summary = f"已生成导图「{content.title}」，共 {count} 个节点。"
    if short_note:
        summary += short_note
    elif unsourced:
        summary += (
            "材料里没有查到与这个主题相关的原文片段，这张图只基于主题生成，"
            "不含任何材料依据；要一张有依据的图，请换一个更贴近材料内容的说法后重试。"
        )
    if external_note and not short_note:
        if external_off:
            # 关着的时候**不能照抄下面那句**："换一个更常见的主题词后重试"在这个
            # 情况下是错的动作——重试一百次也不会联网。这里给的是唯一有效的那个：
            # 去把开关打开。
            # 直接用那个完整句，不再往后补一句：它已经说了"完全基于模型知识、
            # 节点没有出处"，以及唯一有效的动作（打开开关再生成一次）。补一句
            # "这张图只基于主题「X」生成"就与前半句同义反复了。
            summary += EXTERNAL_DISABLED_NOTE
        else:
            summary += (
                f"{external_note}，这张图只基于主题「{topic}」生成，"
                "节点上不会有引用；要引用真实出处，请换一个更常见的主题词后重试。"
            )
    if external_used_note:
        summary += external_used_note
    if evidence_unused:
        summary += (
            f"本次按支检索到 {evidence_total} 条原文并已拼进提示词，"
            "但这张图的节点没有留下任何引用（模型没写，或写了没通过逐字校验）；"
            "这不代表「材料里没有相关内容」，换一个更贴近原文的说法重试可能就好了。"
        )
    if material_unused:
        summary += (
            f"本次把 {len(sections)} 份材料的全文（合计 {full_text_chars} 字）整份给了模型，"
            "但这张图的节点没有留下任何引用（模型没写，或写了没通过逐字校验）；"
            "全文就在上下文里，这不代表「材料里没有相关内容」。"
        )
    if detail_report["requested"] and detail_report["filled"] < detail_report["requested"]:
        # 只在**没补全**的时候说一句：补全是常态（结构那一轮按提示词留空、第三段
        # 一次写回），把它写进 every-run 的话术只是在噪声里加一行。
        summary += (
            f"有 {detail_report['requested'] - detail_report['filled']} 个节点的说明没写回来"
            f"（共 {detail_report['requested']} 个需要补写），这几个节点的说明保持为空；"
            "结构、层级与引用都不受影响。"
        )

    data: dict[str, Any] = {
        # 中心主题的 id。鱼骨与流程的产物里**没有中心主题**（主骨是 `problem`、
        # 主干是阶段序列），所以它们是 None——编一个"最外层的那个节点"来填这个
        # 字段，会让读它的脚本把一根骨当成树的根。
        "root_id": content.root.id if isinstance(content, MindMapContent) else None,
        "node_count": count,
        "revision": ctx.revision,
        # 要展开的方面数：两级时是二十节（二级），扁平时就是一级分支。用户看到的
        # "我要了 20 条"在这里必须还是 20，不能因为多了五个分组变成 5。
        #
        # 两段式没有"骨架定的条数"这个概念（一级分支就是树的最外层，模型直接写进
        # 树里），所以报**落树后的**实际条数——这是同一个问题的答案，只是来源不同。
        #
        # 数的那一层走 `shapes.top_branches`，与下面 `branches` 的 `_top_level_rows`
        # 是同一个口径（树是一级分支、鱼骨是六根骨、流程是各阶段）。**不写
        # `content.root.children`**：那样写等于要求"走到这里的 `content` 一定是树"，
        # 而那个保证在三百行之外——`plan is None` 只出现在两段式成功那一条
        # （`if graph_mode:` 的 if 分支），`graph_mode` 只在 `diagram_type == "tree"`
        # 时为真，`diagram_type` 是一次快照，`_shape_output` 收的是同一个值。四步
        # 推理都对才成立；其中任何一步改了（给两段式加鱼骨、出口的返回约定变了），
        # 症状是 `AttributeError` 打穿整次生成，而不是"这一支数错了"。
        "planned_branches": (
            len(plan.flat) if plan is not None else len(shapes.top_branches(content))),
        # 初次规模的两个参考值：报出来才能解释"为什么这棵树只有这么深/这么大"。
        # 节点那一个是**可读性参考值**（不是配额，也不是裁剪线：超过它只记一条
        # `TREE_OVER_READABILITY`，节点一个不少；真正的硬边界是契约的
        # `MAX_TREE_NODES`）。
        # 深度上限仍是常量——材料量只改规模，不改层数（层数由可读性定）。
        "node_limit": initial_nodes,
        "depth_limit": INITIAL_MAX_DEPTH,
        # 唯一来源是输出被截断（`SalvagedOutput`）：这条路上没有折叠、也没有裁剪，
        # 所以"少了一块"只可能来自模型没写完。`ok=true` 与"完整成功"长得一模一样，
        # 这个位就是它们之间唯一的差别。
        "partial": bool(truncated),
        # 这条路上恒为 0（不折也不裁），留着是为了与 `mindmap_generate_deep` 的 data
        # 同形——读这两个工具产物的脚本，不该因为换了工具就少一个键。
        "cut_nodes": 0,
        # 跨支重复的**节点数**（判据见 `_cross_branch_duplicates`）：库里那张
        # 56 节点的分片图量出来是 6（3 个说法各写 2 遍），这个数就是这次改动最想
        # 改善的指标。只在诊断里说一句"有重复"的话，事后比较两条路还得人肉数。
        "cross_branch_duplicates": sum(len(places) for _, places in duplicates),
        # 根下挂的东西：**不分两级时就是一级分支**，两级时是分组（`points` 因此是
        # "这一组下有几支"）。键名不变是刻意的——读这个字段的前端与脚本不该为了
        # 多一层而改一遍读法。
        "branches": _top_level_rows(content),
        "retrieval": {
            # 三种模式：`full` = 正文整份进 prompt、一条检索都没发（`FULL_TEXT_MATERIAL_CHARS`
            # 那条闸）；`branch` = 每支检索一次；`off` = 没有可检索的材料。
            # 三个值分开报而不是合成一个布尔：`full` 与 `off` 都能让
            # `branches_with_hits == 0`，而它们的原因、成本、以及"下次该改什么"
            # 完全不同（前者是设计、后者是没材料）。
            "mode": "full" if full_text else ("branch" if sources else "off"),

            "top_k": DEEP_BRANCH_TOP_K,
            "queries": retrieval_queries,
            "expanded_branches": len(expansions),
            "branches_with_hits": with_evidence,
            "empty_branches": [title for title, item in zip(titles, evidences)
                               if item is None],
            "hits": sum(len(item.payload) for item in evidences if item is not None),
            # 这一次检索是用户点"生成"之后**多出来的**一次等待（N 支各一次，
            # 第一支串行预热），混进总耗时就永远看不出"慢在哪"。
            "seconds": round(retrieval_seconds, 3),
        },
        "source_refs": {"declared": declared_refs, "kept": kept_refs},
        # 四步各自的耗时：总耗时只能回答"慢不慢"，这一笔回答"慢在哪"。骨架与
        # 结构在这里、检索在 `retrieval.seconds`、说明那一步（两次并发）在自己的
        # `seconds` 里 ——`scripts/measure_single_call.py` 的对比表就是按这几列拼的。
        #
        # 说明那一步记的是**墙钟**（两半取大的那个），不是两次之和：并发跑的时候
        # 两者差一倍，而"这一步让用户等了多久"只有前者答得对。两半各自的秒数在
        # `detail.halves` 里。
        "calls": {
            "skeleton": round(skeleton_seconds, 3),
            "structure": round(structure_seconds, 3),
            "detail": detail_report["seconds"],
        },
        # 三段式的第三段（只写 detail）**本身**的账：要补几个、写回几个、花了多久、
        # 有没有对不上号的键。`detail_avg` 那种质量指标不在这里算（它属于产出，
        # 直接量树即可，`scripts/measure_single_call.py` 就是这么做的）。
        "detail": {
            **detail_report,
            "floor": DETAIL_FILL_FLOOR,
            # 分组节点不算"说明偏短"：它们**本来就不该有说明**（`skip_ids`），
            # 把它们数进来会让两级这一版的 `short_nodes` 恒比扁平多 5，读起来像退化。
            "short_nodes": sum(1 for node in shapes.iter_nodes(content)
                               if node.id not in set(group_ids)
                               and _needs_detail(node)),
        },
        "diagnostics": [item.model_dump() for item in diagnostics],
    }
    if digest_report is not None:
        # 只有开关开着、且真的走到了那一步才有这个键（关着时 `data` 的键集与改动
        # 之前逐字节相同——回归用例比的是键集，不只是值）。与 `data["input"]` /
        # `data["verify"]` 同一条规矩。
        #
        # 账放这里、产物放契约：`branch_digests` 是这张图的属性（要落库），而"这次
        # 调了几秒、输入输出多少 token、哪几支没写出来"是一次运行的读数。
        data["branch_digest"] = digest_report.account()
    if self_check_report is not None:
        # 开关开着、图型是树才有这个键（关着时 `data` 的键集与接线之前逐字节相同
        # ——回归用例比的是键集，不只是值）。与 `data["branch_digest"]` 同一条规矩：
        # 账进读数，产物留在这份 `data` 里而不是给契约加字段——"这次自检说了什么"
        # 是一次运行的读数，不是这张图的属性（用户没改它，图不该因为它变脏）。
        #
        # **这也就是那份清单唯一的去处**：诊断只说一句"N 处"，可指认性（哪个节点、
        # 什么问题、怎么改）在这里。前端与脚本读的都是这一份。
        data["self_check"] = self_check_report.account()
    if auto_repair is not None:
        # 开关关着 / 非树图型时没有这个键（那时 `data` 的键集与接线之前逐字节相同
        # ——回归用例比的是键集，不只是值）。开关**默认开**，所以缺这个键等于
        # "这次收尾没做确定性修复"，是一个需要能读出来的状态。
        #
        # 报的是读数而不是产物：树上已经没有那些冗余了（`check()` 也报不出来），
        # 而"修了几处、哪几条规则动手、少修了几处"只能从这里读。
        data["auto_repair"] = auto_repair
    if shape.skip != "off":
        # 开关开着才有这个键（关着时 `data` 的键集与改动之前逐字节相同——回归用例比的
        # 是键集，不只是值）。放进 `data` 而不是给 `Node` / 契约加字段：`data` 是
        # `dict[str, Any]`，而"这一次从输入里读到了什么"本来就是一次运行的读数，
        # 不是这张图的一部分。诊断只留失败那一档，成功的读数在这里（一次运行一条 info
        # 诊断是噪声——`data` 才是给脚本与界面看的地方）。
        data["input"] = {
            "original": shape.original,
            "topic": shape.topic,
            "chars": {"original": len(shape.original), "topic": len(shape.topic)},
            "source": shape.source,
            "intent": shape.intent,
            "questions": list(shape.questions),
            "constraints": list(shape.constraints),
            "seconds": shape.seconds,
            # 这一次看的是哪段文本（`goal` = 用户原话 / `topic` = 工具参数）。这一位
            # 之所以必须报：同一句原话两种输入给出的 constraints 差两条，而"提取不全"
            # 与"提取不准"在读数上长得一模一样，只有这一位能分开。
            "from": shape.read_from,
            # 自检的账：四类线索扫到几处、接住几处、补了哪几条、哪几条因为超上限没补。
            # 它证明"补进 constraints 的那几条是原文片段"，不是模型改写。
            "selfcheck": shape.selfcheck,
            # 这一步的代价：一次提取的输入 token 估算。它能与"这一次少走的那条路"
            # 直接比较——关着时那一行主题是一整句话，开着时它是一个名词短语。
            "prompt_tokens": shape.prompt_tokens,
            # 没提取的四个原因（`off` 不会到这里）：`short` / `empty` / `budget` /
            # `failed` / `unshaped` / `no_topic`。前两个是设计，后四个在上面有诊断。
            "skip": shape.skip,
            "threshold": INPUT_EXTRACT_MIN_CHARS,
        }
    if verify_data is not None:
        # 只有真的开了核对才有这个键（开关关着时 `data` 的键集与加这段之前逐字节
        # 相同——回归用例比的是键集，不只是值）。
        #
        # 三态**只能进这里**：`Node` 是 `extra="forbid"`，给它加字段就要重导
        # `openapi.json` 与 `schema.d.ts`；`data` 是 `dict[str, Any]`，形状自由，
        # 而"哪个节点是 supported / unverified / contradicted"本来就是一次运行的
        # 读数，不是这棵树的一部分。前端要按状态过滤时读的就是这份表。
        data["verify"] = verify_data
    if plan is not None and plan.grouped:
        # 只有真的分成两级时才有这个键：扁平时 `data` 的键集与这次改动之前逐字节
        # 相同（`test_edges.py` 的 `TestTreeIsByteIdentical` 比的就是键与值）。
        #
        # 报的是**骨架的决定**（分组与它下面的分支），不是落树后的样子：裁剪会砍掉
        # 尾部，而"骨架本来要了二十节"这件事必须留在可观测面上——它是"图会有多宽"
        # 的因，树被裁到 80 个节点是果。落树后的形状看 `data["branches"]`。
        data["skeleton"] = {
            "two_tier": True,
            "threshold": SKELETON_GROUP_THRESHOLD,
            "groups": [
                {"label": group.label,
                 "branches": [branch.label for branch in group.branches]}
                for group in plan.groups
            ],
            "shuffled_nodes": groups_moved,
        }
    if graph_mode:
        # 两段式的账：图给了多少材料、关系收下多少、丢在哪一档。
        # 与 `skeleton` 那个键**互斥**（两段式没有骨架），所以键集不会重叠。
        data["graph"] = {
            "entities": len(graph_entities),
            "links": len(graph_links),
            "queries": titles[:GRAPH_QUERY_LIMIT],
            "relations": len(content.relations),
            "relation_drops": {k: v for k, v in relation_drops.items() if v},
            "relation_limit": relation_limit,
            "relation_kinds": sorted({
                relation.kind.value for relation in content.relations
                if relation.kind is not None
            }),
        }
    if not graph_mode and raw_relations:
        # 三段式的交叉指认账（`CROSS_REFERENCE_RULES`）。**只在模型真写过时才出现**：
        # 一个恒为 0 的键会让"这条路有没有关系规则"与"模型这次没写"看起来一样。
        # 与上面 `graph` 那个键互斥（`not graph_mode`），键集不会重叠。
        data["cross_reference"] = {
            "declared": len(raw_relations) if isinstance(raw_relations, list) else 0,
            "relations": len(content.relations),
            "relation_drops": {k: v for k, v in relation_drops.items() if v},
            "relation_limit": relation_limit,
            "relation_kinds": sorted({
                relation.kind.value for relation in content.relations
                if relation.kind is not None
            }),
        }
    if external is not None:
        # 只在外部队列真的走过时才有这个键：开关关着时 `data` 的键集与加这个
        # 能力之前逐字节相同（回归用例比的是键集，不只是值）。
        data["external"] = {
            "hits": external.hits,
            "sources": len(external.sources),
            "seconds": round(external.elapsed, 3),
            "failed": external.failure,
            # 这次是缓存命中还是真搜了一次网。少了它就分不开"1.9 秒"与"0.02 秒"——
            # 而那个差别正是"下次同一个主题还要不要等"的唯一依据。
            "cached": external.cached,
        }
    if short_material:
        data["material"] = {
            "chars": material_chars,
            "sources": len(short_material),
            "threshold": SHORT_MATERIAL_CHARS,
            "degraded": True,
        }
    if full_text:
        # 与上面那个 `material` 块**互斥**（短材料的 `sources` 已经空了，`full_text`
        # 恒为 False）：同一个键不会在一份 `data` 里出现两次。
        #
        # 报 `token_estimate` 而不是只报字数：这道闸的**唯一**代价就是输入 token，
        # 而"6.7 万 token × 4 次调用"这个数是用户唯一能拿去和检索成本比较的口径。
        # 估算口径与 `chat_async` 的物理边界同一个函数（`estimate_input_tokens`）。
        data["material"] = {
            "chars": full_text_chars,
            "sources": len(sources),
            "threshold": FULL_TEXT_MATERIAL_CHARS,
            "degraded": False,
            "full_text": True,
            "token_estimate": estimate_input_tokens(
                "".join((source.text or "") for source in sources)),
        }

    if slice_report is not None:
        # 只有真的走了按章切才有这个键（开关关着 / 没切成时 `data` 的键集与加这条路
        # 之前逐字节相同）。三个数回答三个不同的问题：切了几章（分母）、每章给了
        # 几个节点的预算（分子）、引用在哪一步被丢掉（覆盖率为什么不是 100%）。
        data["section_slice"] = {
            "chapters": slice_report["chapters"],
            "calls": slice_report["calls"],
            "per_branch_nodes": slice_report["per_branch_nodes"],
            "depth": slice_report["depth"],
            "truncated_chapters": slice_report["truncated"],
            "failed_chapters": [item["chapter"] for item in slice_report["failed"]],
            "skipped_chapters": slice_report["skipped"],
            "nodes": slice_report["nodes"],
            "refs": {
                "located": slice_report["refs_located"],
                "ambiguous": slice_report["refs_ambiguous"],
                "dropped": slice_report["refs_dropped"],
            },
            "seconds": slice_report["seconds"],
        }

    if diagram_type != "tree":
        # 图型与附加边：`diagram_type` 是**输入**，`edges` 是**产出**。"选了流程图
        # 但一条边都没画出来"与"选了思维导图"在树上长得一样，而前者是要去补的动作。
        #
        # 边数按**产物**数：鱼骨图没有 `edges` 字段（它的关系全由层级表达），
        # 报 0 比让 `AttributeError` 落到工具结果里准确得多。
        data["diagram"] = {
            "type": diagram_type,
            "source": source_of_diagram,
            "edges": len(content.edges) if not isinstance(content, FishboneContent) else 0,
        }
    return ToolResult(ok=True, content=summary, data=data)


async def _generate_deep(args: GenerateDeepArgs, ctx: RunContext) -> ToolResult:
    """分片生成：先出骨架，再按层并发展开，最后确定性验收并只补不合格的那一支。

    为什么要拆：整图生成的契约允许上千节点 × 200 字标签，最坏是十几万 token，
    而单次调用再高的输出上限也有尽头——大图只能靠模型自己收敛，收敛不住就是截断。
    分片后每次调用只装自己那一支，单次输出量就与整图上限脱钩了。
    第二个收益是质量：一次调用同时要求「定框架 + 每支展开」，模型会把注意力
    花在先写出来的分支上；把骨架单列成一次调用，它只需要回答「分成哪几块」。

    递归怎么走（`args.max_depth` 按 root 之下算层数）：
      - 第 2 层：并发展开每条一级分支，要点不足的单独补一次。这一段与改多级之前
        逐行同义，`max_depth=2` 时的产出与老实现逐节点相同。
      - 第 3 层往下：**层间串行、层内并发**。下一层有哪些节点，要等上一层全部落定
        才知道，所以层是串的；同一层的父节点彼此无关，共用一个信号量限流。
      - 每次展开只要求模型写一层。实测一次要三层，写到后面会被输出上限截断；
        模型自愿多给一层就收下（`_candidate_slots`），代码不逼它一次吐到底。
      - **某支还要不要继续拆，由模型说**：每次展开的响应里，每个子项都带一个
        `expandable`——它本来就在写这次响应，判一句不额外花任何调用。false 的节点
        直接留作叶子，不再进入下一层的候选（`_walk_slots`）。`max_depth` 退回兜底
        位置，默认值仍是 3：这次只验证模型会不会用这个标记（看 `leaf_declared`），
        深度策略等真实运行的数据出来再定。

    预算怎么分（不这么分，前面几支会把额度吃光）：
      - **初次限额**：`INITIAL_MAX_NODES=80` / `INITIAL_MAX_DEPTH=3`（root 之下，
        即 4 层含根）。满额 4 层是 1+6+18+54=79 个节点、**25 次内容调用**；再往
        下是 241 个节点，第一次生成必然撑爆预算并交出半棵树。
      - 接口层先扣：root + 一级分支 = 1 + branches
      - 剩下的按该层的父节点数平分；每个父节点拿到的份额再与两个上限取小：
        结构上限（扇出 ×「本层 + 容忍的一层」）与**单次输出容量**
        （`_nodes_per_call`，由 `generation.budget` 反解）。后者是这一轮新加的：
        额度算术允许一支写 30 个节点，不代表这一次请求装得下 30 个满额节点。
      - 额度不足就**停手**，已经展开的部分照常交出，标 `partial` 并说明原因。
        少交一层不是失败，"报完整成功却少了半棵树"才是。

    **展开之后还有一步限额补查**（`supplement`）：首路 query 是分支标题，与正文用词
    对不齐的那一支会一条都召不回。判据是"模型自己说这支配不上依据"或"这一支写了
    ≥`SUPPLEMENTARY_MIN_NODES` 个要点却零引用"；命中后换 query（该支最长子节点
    文字）再检索一次、重写这一支并与原结果合并。上限：每支 1 次、每次运行
    `SUPPLEMENTARY_RUN_LIMIT` 次，**不占 `max_tool_calls`**（与按支检索同一条规矩）。

    四段都有各自的失败语义，不要合并：
      1. 骨架拿不到 → 整次失败（连框架都没有，没有可交付的东西）
      2. 某支展开抛错 → 那一支留空，其余照常（一次 429 不该废掉整张图）
      3. 验收发现不足 → 只补那一支，骨架不重来（这是分片结构的全部价值）
      4. 额度不足 → 立刻停手，把已展开的部分交出去并显式标 `partial`

    顺带往上推**节点生长预览**（`node_grow`）：`RunContext.emit` 不为 None 时，
    展开调用改走流式请求，每写出一个完整的子项就上报一帧（根与一级分支在骨架
    落定时先报）。它只是预览——最终树仍然是这里合并、校验后交给 `snapshot` 的
    那一棵，两者不一致是正常的（预览可能多、可能少）。通道为 None 时这条路径
    **一行都不走**，工具行为与加预览之前完全相同。

    **每一支的「检索 → 展开」是一条流水线**（分片 + 按支检索，`docs/RAG-DECISIONS.md`
    第 5 节）：骨架落定后先检索第一支（它同时预热块向量缓存），之后**每一支自己的
    检索一回来就发它自己的展开，不等其他支**。上一版是两阶段（全部检索完再一批
    展开），骨架到第一个子节点之间隔着"最慢的一支检索"；流水线之后那段空窗只剩
    第一支的检索。query 是**分支标题本身**（不含主题，理由与三组消融数字见
    `_search_branch`），走 `search_parent`（父块返回，默认开），
    `top_k=DEEP_BRANCH_TOP_K`。这个工具读来源有两处，形态都不是全文：骨架那一次
    读的是**标题行**（`_skeleton_outline`，原文的子集），展开读的是**检索结果**。

    流水线有两条边界，改节奏时最容易漏的就是这两处：
      - **块向量预热那道闸不能拆**：第一支仍然串行检索——N 支同时发出去会让缓存
        逐个击穿，8 支的向量请求量从 76 条涨到 552 条（数字与实测口径见
        `_retrieve_branches`）。闸门只需要**一支**跑完，不需要所有支，所以它与
        流水线不冲突。
      - **"全部支都没证据 → 整树兜底"需要一处屏障**：那个判定要等全部门落定，而
        流水线不允许等。屏障因此只挡住**没拿到证据的那几支**（`settle_fallback`），
        有证据的支照常展开；空支在等之前先让出并发名额，否则 20 支时名额会被占满
        成死锁。

    为什么必须按支检索：模型看不到正文，整棵树只有**每支各一次**检索机会。
    `top_k=5` 对八千字材料约覆盖 25%、对三万字约 8%，于是"没想到要问的章节"
    会静默缺失——用户拿到一份看起来正常但缺了几块的导图。分片之后覆盖度由
    **骨架**决定，不由模型的查询创意决定。

    三条硬约束（违反任意一条，这个能力就变成负资产）：
      1. **不占 `max_tool_calls`**：检索发生在工具内部，是服务端自己的并发检索，
         模型看到的仍然只是一次 `mindmap_generate_deep` 调用。
      2. **成本有闸**：`top_k=3` × N 支，N 上限 `DEEP_MAX_BRANCHES=20`，块向量按
         文本哈希缓存，实测口径见 `DEEP_BRANCH_TOP_K` 的注释。
      3. **降级彻底**：没配 embedding 时纯 BM25 照常；某支抛错只让那一支无证据
         展开；**所有支都拿不到证据**时退回"整树一次检索"（`DEEP_FALLBACK_TOP_K`
         条），绝不因为检索失败交出一棵空树。

    注册面：**这是 `create` 唯一的生成路**（整树一次成型在"正文不进 prompt"下
    实测覆盖 5.1%，这里按支检索 30.5%）。所以 `slicing` 只回答一件事——这次有没有
    可检索的正文；没有再没有任何形态分支需要判。

    **材料入口三段**（原属已删除的 `_generate`）：只喂有正文的来源、材料太短退化、
    没有材料时先做一次外部检索。三条都必须在这里，因为这是唯一的生成入口。
    """
    if ctx.request.intent != "create":
        return _fail("wrong_intent", "只有新建导图时才能调用这个工具；修改现有导图请用 node_expand 或 mindmap_edit。")
    if ctx.candidate is not None and _has_content_nodes(ctx.candidate):
        return _fail("map_exists", "已经有一张导图了，请用 node_expand 或 mindmap_edit 修改它。")

    # ---- 材料准备：只喂有正文的来源、短材料退化、没有材料时先做外部检索 --------
    #
    # 三段都在 `_intake` 里，与 `_generate` 共用一份实现（两处各写一遍的话，
    # "材料太短"的阈值与"外部检索什么时候发"迟早会有一个分支先改）。
    #
    # 先探一次额度再外呼：外部检索不花模型调用，但"额度在别的工具里已经烧光"时
    # 白等一次 6 秒的外网往返没有意义（`reserve_model_call` 只是这份额度的记账，
    # 这里只读不占）。
    if ctx.model_calls_used >= ctx.request.budget.max_model_calls:
        return _fail("budget_exhausted", "本次运行的模型调用额度已用完，连骨架都出不了，无法分片生成。")
    intake = await _intake(args.topic, ctx)
    sources = intake.sources
    short_material = intake.short_material
    material_chars = intake.material_chars
    hint_block = intake.hint_block
    external = intake.external
    # 分片检索的前提：真的有可检索的正文。空正文的来源不参与。
    #
    # **这条路不采用全塞**（`intake.full_text` 在这里故意不读），理由是结构性的：
    # 分片的每一次展开调用只装自己那一支，这正是它"单次输出量不受整图上限约束"的
    # 来源。把全文塞进那 20 次调用 = 20 × 6.7 万 token，比 `_generate` 的 3 次贵一个
    # 数量级，而换来的覆盖优势它已经有了（覆盖度由骨架决定，不靠 query）。
    # 全塞是为 `_generate`（一次成型、正文只有进 prompt 才能被看到）设计的那条路。
    slicing = bool(sources)

    need = min(args.points_per_branch, MAX_CHILDREN)
    # 深度上限在参数层已经夹过一次（ge/le），这里再夹一次是防"绕过参数模型直接
    # 调函数"：递归的每一层都要靠它判停，一个异常的 max_depth 会让循环失去上界。
    #
    # **初次生成只有 4 层（含根）**：`INITIAL_MAX_DEPTH=3` 是按 root 之下算的，
    # 3 + 中心主题 = 4。差一层就完全不同——4 层满额是 79 个节点、79 次调用，
    # 5 层满额是 241 个节点、241 次调用，后者必然撑爆预算并交出半棵树。
    # 更深的树由 `node_expand` 逐步长（那时上限回到契约的 8 层 / 1000 节点）。
    max_depth = max(2, min(args.max_depth, DEEP_MAX_DEPTH, INITIAL_MAX_DEPTH))
    # 二级及以下的扇出。`need` 本身是 2..5，再按 DEEP_FANOUT 兜一层——将来谁把
    # `points_per_branch` 的上限调大，深度带来的节点膨胀也不会跟着翻倍。
    fanout = max(2, min(need, DEEP_FANOUT))
    # 节点上限取契约与**初次规模**的较小者：契约是承载能力（1000），初次承诺是
    # 80 个节点。取小之后，按层预分配的每一份都是"这一次真的会用到的预算"。
    node_limit = min(_node_limit(), INITIAL_MAX_NODES)
    # 一次调用最多要几个满额节点（三个上限取最小后反解，见 `generation.budget`）。
    # 它夹住下面每一个"这一次要几个节点"的量，所以要求量永远装得进单次输出。
    per_call_nodes = _nodes_per_call(ctx, system=BRANCH_EXPAND_PROMPT)
    diagnostics: list[Diagnostic] = []
    # 入图那一步的账。与 `_generate` 同一份判断（开关关着时恒为 None）。
    if intake.ingested is not None:
        diagnostics.append(_graph_ingest_diagnostic(intake.ingested))
    for reason in intake.graph_ingest_skip:
        skip = _graph_ingest_skip_diagnostic(reason)
        if skip is not None:
            diagnostics.append(skip)
    if intake.library is not None:
        # 与 `_generate` 同一份诊断。**这条路只说骨架那半句**：库片段在这条路上只进
        # 骨架那次调用（定一级分支），不进每支的展开——把 5 段库原文拼进 20 次展开
        # 是 20 倍的输入代价。所以这里的 `LIBRARY_SUPPLEMENT` 文案不能声称"引用可以
        # 照抄"，改由 `_library_supplement_diagnostic(..., cite=False)` 说清。
        note = _library_supplement_diagnostic(intake.library, cite=False)
        if note is not None:
            diagnostics.append(note)
    # 思维模型两条路都要拼：骨架决定怎么分块，展开决定每块怎么拆。
    thinking = thinking_block(ctx.request.thinking_model)

    # 根节点的标题在骨架之前就定了：它同时是树的 title，提前算出来还能在上报
    # 根节点时直接用它，不必等到最后合并那一步。
    #
    # 与节点标题同一条限制（16 字）：根也是一块画布节点，用户填的主题经常是
    # "帮我整理这份预算管理办法"这种指令句，整句画在中心会撑满一屏。
    root_label = clip_label(args.topic.strip()) or "思维导图"

    # 已上报过的 `(父路径, label)`，全局一份：预览的去重口径是"同一个父节点下
    # 同一句文字只出现一次"，跨路（不同展开调用）也成立。
    reported_grow: set[tuple[tuple[int, ...] | None, str]] = set()

    async def ask_model(
        prompt: str, user: str, parent: tuple[int, ...] | None = None,
        base_index: int = 0, quota: int | None = None,
    ) -> Any:
        """发一次模型调用，顺带把「已经写完的子项」推给事件流。

        `thinking` 统一在这里拼，四个调用点不再各拼一遍。

        `parent` 只在开了上报通道时才有意义，也只有在有通道时才会传 `on_delta`：
        通道为 None 时连流式请求都不发，这条路上工具的行为与加生长事件之前
        **逐字相同**（现有测试全靠这一点保持有效）。

        `quota` 是这一路会被真正装进树的节点数（`_trim_slots` 的额度）。预览
        按它截断，于是"长出来又消失"的节点不会出现在界面上。
        """
        system = prompt + thinking
        if ctx.emit is None or parent is None:
            raw = await _chat(ctx, system, user, max_tokens=_max_tokens(ctx))
        else:
            stream = _GrowStream(ctx, reported_grow, parent, base_index, quota=quota)
            raw = await _chat(
                ctx, system, user, max_tokens=_max_tokens(ctx), on_delta=stream.feed,
            )
        # 截断在这里统一记账：四个调用点（骨架 / 分支 / 补点 / 深层）都从这里出去，
        # 各判一次的结果是"某一条路截断了但没人说"。判定只看返回值类型，不改形状
        # ——`raw` 仍是一个 dict，往下的 `_candidate_slots` / `_skeleton_titles`
        # 一行都不用改。
        _note_truncation(diagnostics, raw)
        return raw

    # 第一步：出骨架。额度在调用**之前**占——请求一旦发出就是一次真实计费。
    #
    # 有材料时这一次调用会带上**材料自带的章节**（`_skeleton_outline`）：正文仍然
    # 不进 prompt，进的是每份材料自己的标题行（原文的子集，见 `_material_inputs`
    # 的 docstring）。为什么放在骨架这一次而不是展开：分片之后**覆盖度由骨架决定**
    # ——模型凭空想 8 支时，材料里它没想到的章节就再也不会有
    # 人问；给它一份目录，它至少知道"这份材料有这几块"。实测数字见
    # `scripts/measure_outline_skeleton.py`。没有材料时这一项自然是空串。
    if not ctx.reserve_model_call():
        return _fail("budget_exhausted", "本次运行的模型调用额度已用完，连骨架都出不了，无法分片生成。")
    ctx.extra_model_calls += 1
    # 混合材料与整树那条**共用同一个调用**（`_mixed_outline`）：两条路对同一批材料
    # 必须给出同一个骨架来源，否则只会在把分片重新注册回 `create` 的那天才暴露
    # （`AGENTS.md` 关于两条生成路的那条要求）。`slicing` 为假时整段跳过，一个字符
    # 都不算——这条路上没有可检索正文时骨架本来就不看材料。
    mixed = _mixed_outline(sources) if slicing else None
    outline = (
        _render_outline(mixed.chapters, sources) if mixed is not None
        else (_skeleton_outline(sources) if slicing else ""))
    if mixed is not None and mixed.supplement.block:
        outline = outline + "\n\n" + mixed.supplement.block
    # 第三档（`_graph_outline`）在这里**必须同步接**：分片与整树共用骨架那一次调用
    # 的输入，只接一条会得到一个"两个入口对同一批材料给出不同骨架来源"的分叉，
    # 而那种分叉只在把分片重新注册到 `create` 的那天才暴露（见 `AGENTS.md` 关于
    # 两条生成路的那条要求）。判据与整树那条完全一样：目录不可用时才问图谱。
    graph_outline = ""
    if mixed is not None:
        diagnostic = _mixed_diagnostic(mixed)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
    elif slicing and not outline:
        graph_outline, graph_outline_entities, graph_skip = (
            _graph_outline_attempt(sources))
        if graph_outline:
            outline = graph_outline
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.info, code="GRAPH_OUTLINE_USED",
                message=(
                    f"材料抽不出章节标题（{material_chars} 字），改用图谱里的 "
                    f"{graph_outline_entities} 个概念定骨架。"),
            ))
        else:
            # 与整树那条逐字同一个出口（`AGENTS.md`：两条生成路对同一批材料必须给出
            # 同一个骨架来源，否则只会在重新注册分片的那天才暴露）。
            skip = _graph_outline_skip_diagnostic(graph_skip, graph_outline_entities)
            if skip is not None:
                diagnostics.append(skip)
    # ---- 图型的唯一判定点 -------------------------------------------------------
    # **请求给了就用请求的，没给才用工具参数。** 这一行是"用户说了画流程图"能被
    # 兑现的全部原因：模型看不见界面，`diagram_type` 这种"可以填也可以不填"的工具
    # 参数，只要默认值存在它就会走默认值——用户说"画个流程图"，它把这句话当主题
    # 描述消化掉，然后规规矩矩用默认的 tree。提到请求层之后，「图型是什么」不再
    # 由模型的记性决定。
    #
    # 为什么仍然保留工具参数这一支：老前端不带 `request.diagram_type`（None），
    # 那时唯一知道用户想要什么图的地方就是模型自己的判断，行为与加请求层之前
    # 逐字节相同——`tests/test_agent_diagram_type.py` 用 `2ed92ac` 的旧模块钉着。
    #
    # 请求指定时 registry 那边**根本不把这个字段发给模型**（见
    # `_generate_deep_spec`），所以"模型填了会怎样"只剩两种情况：它凭老习惯凭空
    # 补一个（校验收下、这里覆盖、记一条 debug），或者它压根不知道有这个字段。
    # 两种都不会改变产出——覆盖发生在读参数的第一步，后面再没人读 `args` 的那份。
    diagram_type = ctx.request.diagram_type or args.diagram_type
    # 「这次图型是怎么定下来的」。请求层解析过（`ctx.diagram_source` 非空）就用它
    # 记的那个值：那里才知道"是下拉选的"还是"从 `goal` 的文字里读出来的"。没有
    # 解析层时只能按请求有没有带值推——带了就是用户选的，没带就是模型的默认值。
    # 两种情况下都不是**这里**说了算；这里只是把结论记下来。
    source_of_diagram = ctx.diagram_source or (
        SOURCE_REQUEST if ctx.request.diagram_type else SOURCE_MODEL)
    if ctx.request.diagram_type and args.diagram_type != ctx.request.diagram_type:
        logger.debug(
            "图型以请求为准：请求=%s，工具参数=%s（模型的写法不生效）",
            ctx.request.diagram_type, args.diagram_type,
        )
    # 骨架这一路只在**没有可检索材料**时补那两句（`CONTENT_FREEDOM_CONTRACT` +
    # `DETAIL_NO_SOURCE_RULES`）：它们是纯文本契约、不含输出格式，不会与
    # `PLAN_PROMPT` 的"只返回 branches"打架。有材料时一个字都不加——那时引用的
    # 规矩由每支的 `BRANCH_EVIDENCE_RULES` / `BRANCH_NO_EVIDENCE_RULES` 说。
    #
    # 图型只在**这一处**分叉：三套提示词都要求同一个 JSON 骨架，流水线其余部分
    # 一行都不用改。`tree` 这一支是逐字节回归的守卫点——它的两个字符串变量与
    # 加 `diagram_type` 之前完全相同。
    base_plan = (
        PLAN_PROMPT if diagram_type == "tree"
        else FLOW_PLAN_PROMPT if diagram_type == "flowchart"
        # 与整树那条逐字同一处（`AGENTS.md`：骨架被两条生成路共用）。
        else fishbone_plan_prompt()
    )
    plan_system = base_plan if slicing else (
        base_plan + "\n" + CONTENT_FREEDOM_CONTRACT + DETAIL_NO_SOURCE_RULES)
    # 库补了片段时把后一句换掉（与 `_generate` 同一处、同一个理由）：user 末尾正摆着
    # 库里的原文，再说"没有可标出处的材料"会让骨架照旧凭主题铺分支。
    library_block = (
        "\n\n" + _library_evidence_section(intake.library, cite=False)
        if intake.library is not None and intake.library.hits else "")
    if library_block and not slicing:
        plan_system = base_plan + "\n" + CONTENT_FREEDOM_CONTRACT
    # 与整树那条同一句、同一位置（理由见 `_generate` 里那段注释）：它要在
    # `PLAN_PROMPT` 的「目录为空时自行归纳」之后，才压得住那一句。
    if graph_outline:
        plan_system += GRAPH_PLAN_RULES
    elif mixed is not None:
        # 与整树那条逐字同一处（`AGENTS.md`：骨架被两条生成路共用）。
        plan_system += (
            _mixed_plan_rules(mixed.supplement.items)
            if mixed.supplement.block
            else _mixed_plain_rules(mixed.plain))
    # 与整树那条逐字同一句、同一位置（`MATERIAL_AS_OBJECT_RULES`）：那条路拼了这条
    # 判据而这条不拼时，两个入口对同一批材料给出的骨架不是同一个（这条分叉被
    # `test_mixed_skeleton.py::test_both_generation_paths_compose_the_same_skeleton` 钉着）。
    if sources:
        plan_system += MATERIAL_AS_OBJECT_RULES
    try:
        raw_plan = await ask_model(
            plan_system,
            _plan_ask(
                args.topic, outline=outline,
                # 与整树那条逐字同一处（`AGENTS.md`：骨架被两条生成路共用，
                # 只改一条会得到一个"两个入口对同一批材料给出不同骨架"的分叉）。
                materials=_material_inventory(sources, has_structure=bool(outline)),
                diagram_type=diagram_type,
            ) + hint_block + library_block)
    except GenerationError as exc:
        return _fail("generation_failed", f"骨架生成失败：{exc}")

    branches = _skeleton_branches(raw_plan) or list(_skeleton_plan(raw_plan).flat)
    # 为什么这里也要收 `groups` 形状：这条路（分片）**不发两级骨架的提示词**
    # （`_group_plan_rules` 只在 `_generate` 里拼），所以正常情况下它只会看到扁平的
    # `branches`。但"模型自己给了一个 groups 响应"并不该让整次生成失败——把它压成
    # 一层二十支继续跑，代价只是图宽，而不是一次已经花掉的骨架调用被判死。
    #
    # **这条路不实现两级**：它的每一步都是"一支一次调用"，分组节点在那套流水线里
    # 会变成一个没有内容却要展开的节点（正好是这次改动要避免的浪费）。真要把两级
    # 搬到这里，正确做法是"分组只是树的形状、完全不进 `skeleton` 那张表"，
    # 而那需要另一轮实测，不在这条路上顺手做。
    if len(branches) < MIN_BRANCHES:
        return _fail(
            "too_few_branches",
            f"模型只给了 {len(branches)} 条一级分支，少于 {MIN_BRANCHES} 条，无法成图。",
            branches=len(branches),
        )
    # 一级分支的条数上限与参数模型同一个常量：模型偶尔会无视"最多 N 条"，
    # 这时最多留 20 条，再多的既不在一级分支的可读区间里，也挤占下层预算。
    branches = branches[:min(MAX_CHILDREN, DEEP_MAX_BRANCHES)]
    # 缺色的分支在这里补齐（调色板顺序、跳过模型已占用的）。放在截断**之后**：
    # 只给真的要上画布的那几条分配，被丢掉的尾巴不参与配色，否则"第 4 支拿到
    # 第 4 个颜色"这种事会因为一条被截掉的分支而错位。
    branches = _fill_branch_colors(branches)
    # 条数**只做结构下限校验**，没有上限也没有期望值：一级分支有几条由材料与模型
    # 决定（见 `NO_BRANCH_CAP_RULE`），这里不再出现"请求几条、给了几条"那条诊断。

    # 分支节点的 id 在骨架阶段就定好：展开是并发跑的，合并时要按 id 把要点挂回去，
    # 而树这时还不存在——只能先分配 id，合并时一次性落进树里。
    taken: set[str] = set()
    root_id = new_node_id(taken)
    taken.add(root_id)
    skeleton: list[tuple[str, str]] = []
    # 分支 id -> 它的视觉元素（color/icon）。单独一张表而不是把 skeleton 扩成四元
    # 组：`skeleton[index][0]` 是十几处诊断与结果里的"这一支叫什么"，改元数会
    # 让那些地方全部要跟着改一遍，而它们与视觉元素无关。
    branch_visual: dict[str, _SkeletonBranch] = {}
    for branch in branches:
        branch_id = new_node_id(taken)
        taken.add(branch_id)
        skeleton.append((branch.label, branch_id))
        branch_visual[branch_id] = branch

    # B 类偏好：`icon.enabled=false` 时一级分支不带 emoji。
    #
    # 位置在**视觉元素唯一落进树的地方**（`branch_visual` 是下面建树的唯一来源），
    # 所以一条判断同时覆盖三个图型的全部产出，不用在 `tree` / `flowchart` /
    # `fishbone` 三条路上各写一遍。
    #
    # 为什么在这里剥、而不是让 `_skeleton_branches` 去问数据库：那个函数是纯解析，
    # 用例直接调它，它不该忽然开始认库。也为什么不改提示词里那句"建议每条分支都给
    # icon"：为省几十个 token 把三份骨架提示词常量都改成函数，代价大于收益；而用户
    # 能看到的结果（树上没有 emoji）与改提示词完全一致。
    #
    # 只剥不给：偏好是关掉的，没有"换成另一个图标"这回事。
    if not prefs.icons_enabled():
        branch_visual = {
            branch_id: replace(branch, icon=None)
            for branch_id, branch in branch_visual.items()
        }

    # ---- 附加边（只有 flowchart 会有）-----------------------------------------
    # 位置必须在这里：边端点是**骨架层的节点 id**，而 id 刚在上面的循环里分配完。
    # `fishbone` 与 `tree` 一进来就是空列表，后面所有地方（落树、导出、前端）
    # 都不用判图型——空列表走的是同一条路，这正是"树 + 边"这个折中省下来的东西。
    #
    # `clean_edges` 负责丢掉自环、dangling、重复、超限与超长环，并把每一类都记成
    # 一条 warn。**不在这里硬拒**：边是骨架的附加产物，一条写错的边不该把已经
    # 花掉的那次骨架调用连同整棵树判废。
    edges: list[Edge] = []
    if diagram_type == "flowchart":
        # 端点只可能是 root 或一级分支——`_skeleton_edges` 的解析表就是这么建的，
        # 所以这里能构造的 id 集合也只需要这两个。
        skeleton_ids = {root_id} | {branch_id for _, branch_id in skeleton}
        edges, edge_diag = clean_edges(
            _skeleton_edges(raw_plan, skeleton, root_id), skeleton_ids)
        diagnostics.extend(edge_diag)

    # 骨架落定的这一刻就把根与一级分支报出去：它们不需要等任何展开调用，而
    # 展开是并发的、动辄十几秒，先让用户看见框架比多等一个字段值不值的问题更值。
    # 顺序必须是根在前——父节点一定先于子节点出现在流里，前端据此直接挂。
    # 根用 `path=None`，一级分支用 `path=()`（挂在根上）：两者形态不同，前端不必
    # 靠"谁是第一帧"去猜哪个是中心主题。
    await _emit_grow(ctx, reported_grow, None, root_label, None)
    for title, _ in skeleton:
        # 骨架只有字符串，没有 expandable 可报：`None` 表示"模型没写"，
        # 而不是"模型说不能再拆"。
        await _emit_grow(ctx, reported_grow, (), title, None)

    # ---- 第二步之前：按支检索（`docs/RAG-DECISIONS.md` 第 5 节的第一条）---------
    # 位置就在这里：骨架已经定了"分成哪几块"，而每一支的展开还没发出去。早一步
    # （骨架之前）没有分支标题可当 query，晚一步（展开之后再补）就只能把证据塞进
    # 下一层，模型已经按主题写完一轮了。
    #
    # 它**不占** `max_tool_calls`：模型看到的仍然只是一次 `mindmap_generate_deep`
    # 调用，检索是工具内部的服务端行为。
    branch_evidence: list[_BranchEvidence | None] = [None] * len(skeleton)
    retrieval_queries: list[str] = []
    retrieval_mode = "off"
    # 检索的两份结局：`search_outcome` 按支记 "hits" / "empty" / "failed"（还没跑
    # 为 None），屏障与兜底读它；`retrieval_failed` 单独留一份，是因为整树兜底会把
    # outcome 整体改写成 "hits" 或 "empty"，而"这一支检索抛错过"是诊断要说明的
    # 事实，不能跟着兜底一起消失。
    search_done: list[bool] = [False] * len(skeleton)
    search_outcome: list[str | None] = [None] * len(skeleton)
    retrieval_failed: list[int] = []
    retrieval_empty: list[int] = []
    # 检索诊断延后到展开之后再汇总：它们原本紧跟在骨架诊断之后、展开期诊断
    # （补查那条）之前，位置由这个下标钉住。
    diagnostics_before_search = len(diagnostics)
    retrieval_diagnostics: list[Diagnostic] = []
    branch_titles: list[str] = []
    # 第一支的检索结果（它同时是块向量预热的闸，见下面的检索段）。流水线里这支
    # 不再重复检索第二次。
    first_hits: list[EvidenceHit] | None = None
    # 整树兜底的命中条数；None = 没走兜底。诊断在汇总处统一生成，这里只留事实。
    whole_count: int | None = None
    # 分支标题 → 同义问法。没有可检索的材料时不做按支检索，所以这里恒为空——
    # 它同时是 `retrieval.expanded_branches` 的来源，两处共用一个变量才不会有半开状态。
    expansions: dict[str, list[str]] = {}

    async def safe_search(title: str, index: int) -> list[EvidenceHit] | None:
        """按支检索：**永不把异常抛进流水线**。

        `_search_branch` 自己已经把检索异常收成 `None`（"没检索到"与"检索失败"
        的区别在它那里定义），这里再兜一层是给"将来的检索栈会抛"留的位置：
        流水线里没有 `_retrieve_branches` 那层 `gather` 接着，一个抛出去的异常会
        让这一支变成"整支留空"，而它的语义只该是"这一支没查到证据"。

        取消必须原样抛回去：那是上层被取消（客户端断开），不是这次检索失败。

        画像方向在这里传进去（而不是让 `_search_branch` 自己取）：方向要"一次运行
        只算一次"（见 `_run_directions`），而 `_search_branch` 拿不到 `ctx`。
        """
        try:
            # 开关关着（`_run_directions` 返回空）时**连这个关键字都不传**：调用
            # 形态与画像这一层上线之前逐字节相同，`test_deep_pipeline` 里那些
            # 只接受三个位置参数的检索替身因此一个字都不用改。
            directions = _run_directions(ctx, sources)
            if directions:
                return await _search_branch(
                    sources, title, expansions.get(title, ()),
                    directions=directions)
            return await _search_branch(sources, title, expansions.get(title, ()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 检索失败只降级
            logger.warning("分支检索抛错（%s / 第 %d 支）：%s: %s",
                           title, index, type(exc).__name__, str(exc)[:160])
            return None

    if slicing:
        branch_titles = [title for title, _ in skeleton]
        # 查询扩展：每支从"一条标题"变成"标题 + 至多 2 条同义问法"。`expansions`
        # 为空是**常态**（开关默认关、单支时的成本闸、或模型降级），那时下面两行
        # 与这个能力上线之前逐字节相同。
        #
        # 位置必须在这里：扩展的输入是分支标题，而分支标题要等骨架落定；输出要
        # 赶在按支检索之前，否则检索已经按标题跑完了。
        expansions = await _expand_branches(sources, branch_titles)
        retrieval_queries = [
            query for title in branch_titles
            for query in _branch_queries(_branch_query(title), expansions.get(title, ()))
        ]
        retrieval_mode = "branch"
        # 第一支先检索：它是**块向量缓存的预热闸**（为什么必须有这一闸、以及不
        # 预热时 17 倍的请求量，见 `_retrieve_branches` 的 docstring）。流水线不
        # 取消它——闸门只需要**一支**跑完，不需要等所有支，所以它挡住的只是它自己
        # 那一次检索（0.6–0.9 秒），不再是"最慢的那一支"。
        #
        # "所有支都没拿到证据 → 退整整树一次检索"这条降级也没变，但它不能在
        # 这里判：那个判定要等全部门落定，写在这里就把流水线拉回两阶段了。判定
        # 搬到了 `settle_fallback`，由它只挡住没拿到证据的那几支。
        if branch_titles:
            first_hits = await safe_search(branch_titles[0], 0)
            # 第一支**不管成没成**都算跑过了：`run_branch` 靠这个标记决定它要不要
            # 再检索一次——失败时的 `None` 与"还没跑"是两件事，混成一个就会把这一
            # 支检索两遍（成本翻倍，且它自己那支的候选块也白 embed 一次）。
            search_done[0] = True
            search_outcome[0] = (
                "failed" if first_hits is None else ("hits" if first_hits else "empty"))
            if first_hits is None:
                retrieval_failed.append(0)

    def prompt_for(base: str, evidence: _BranchEvidence | None) -> str:
        """这一支该用哪份提示词：有证据 / 无证据（有材料、这一支没查到）/ 纯主题。

        三者不能合并，否则会把"这次没有材料"和"这一支没查到"说成同一件事：
          - 有证据：告诉它引用只能从**本支**的 evidence 里抄（`BRANCH_EVIDENCE_RULES`）；
            证据来自外部检索时再叠一句"这是外部抓的、不可信数据"
            （`EXTERNAL_TRUST_RULES`）——它防的是抓回来的正文里写着指令。
          - 有材料但这一支没命中：照常写满、引用留空（`BRANCH_NO_EVIDENCE_RULES`）。
            "未检索到"不等于"材料里没有"，说成后者会让这一支整块消失。
          - 纯主题（没有可检索材料：本来没传 / 材料太短被降级 / 外部也没搜到）：
            拼 `DETAIL_NO_SOURCE_RULES`，让它别写 `source_refs`——没有材料时
            `source_usage` 是空的，模型凭习惯写出的引用会让整棵树被判 `tree_invalid`。
            前面那句 `CONTENT_FREEDOM_CONTRACT` 是**内容**契约（可以自由展开、
            不要留空节点），与"引用留空"是一对，缺一句模型就退回"没依据就不写"。
        """
        if evidence is not None:
            return base + "\n" + BRANCH_EVIDENCE_RULES + (
                EXTERNAL_TRUST_RULES if _has_external_source(sources) else "")
        if slicing:
            return base + "\n" + BRANCH_NO_EVIDENCE_RULES
        return base + "\n" + CONTENT_FREEDOM_CONTRACT + DETAIL_NO_SOURCE_RULES

    def evidence_suffix(evidence: _BranchEvidence | None) -> str:
        """把这一支的命中拼在 user 消息末尾。`None` 时一个字符都不加。"""
        if evidence is None:
            return ""
        return "\n本支检索结果（evidence 数组）：\n" + evidence.text

    # 额度预分配（第一层）：root + 一级分支是"接口层"，先从总节点里扣掉，剩下的
    # 才轮到各支往下长。不先扣的话，一支自己就可能把额度吃到只剩两位数，后面
    # 十九支全部只剩骨架——用户看到的是"第一支很丰满、其余都是空壳"。
    branch_share = max(0, node_limit - (1 + len(skeleton))) // max(1, len(skeleton))
    # `per_call_nodes` 是**单次输出的物理容量**（节点数），第三个上限：额度算术
    # 允许一支写 30 个节点，但一次调用装不下 30 个满额节点时，要求 30 个就是在
    # 预订一次截断。三个数取小，要求量才真的装得进这一次请求。
    point_budget = min(need, branch_share, per_call_nodes)
    if point_budget < 1:
        return _fail(
            "node_budget_too_small",
            f"本次生成的节点上限 {node_limit} 与单次输出容量（约 {per_call_nodes} 个节点）"
            f"装不下中心主题 + {len(skeleton)} 条一级分支，无法分片生成。",
            node_limit=node_limit,
            per_call_nodes=per_call_nodes,
        )
    # 一次响应最多容忍模型多给一层。`max_depth=2` 时零容忍：那时多给的任何一层
    # 都会让产出与"改多级之前"不一致，而这正是 max_depth=2 要保证的事。
    nested_levels = 1 if max_depth > 2 else 0
    allowance = min(
        point_budget * (1 + _nested_capacity(fanout, nested_levels)),
        branch_share,
        per_call_nodes,
    )

    # 第二步：并发展开。限流必须包住整个"占额度 + 发请求"，否则额度会被
    # 排队中的任务一次性预占，实际请求却还没发出去。
    semaphore = asyncio.Semaphore(DEEP_CONCURRENCY)

    # 补查的运行级计数与可观测面（策略见 `SUPPLEMENTARY_*`）。
    #
    # 不加锁：整个工具跑在一个事件循环线程里，"判额度 + 自增"之间没有 await，
    # 两个并发分支不可能同时看到 `supplementary_used == 0`。加一把 `asyncio.Lock`
    # 只是在保护一段本来就不会被抢占的代码。
    supplementary_used = 0
    supplementary_queries: list[str] = []
    supplementary_branches: list[int] = []

    async def supplement(
        title: str, branch_index: int, slots: list[_Slot],
    ) -> list[_Slot]:
        """一支的限额补查：换一条 query 再检索一次，命中就补写内容并合并。

        四道闸，任意一道不满足就原样返回已经展开的内容：

          1. 每次运行最多 `SUPPLEMENTARY_RUN_LIMIT` 次（成本闸，理由见常量注释）；
          2. 这一支只能补一次——调用点只判一次，补查结果不再回判；
          3. 补查失败或空命中 → 保持原结果（"没补到"不等于"这一支该空着"）；
          4. 模型额度不够重写 → 保留原结果，但新证据已经记进 `branch_evidence`
             与 `ctx.evidence`，补点轮和收尾校验都能用上它。

        **合并而不是替换**：补查是为了补依据，不是重写这一支。实测第二次响应
        常比第一次少一两条要点，替换会让已经写好的内容凭空消失。
        """
        nonlocal supplementary_used
        if supplementary_used >= SUPPLEMENTARY_RUN_LIMIT:
            return slots
        query = _longest_slot_label(slots)
        if not query:
            return slots
        supplementary_used += 1
        supplementary_queries.append(query)
        supplementary_branches.append(branch_index)

        hits = await _search_text(sources, query)
        if not hits:
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.info, code="BRANCH_SUPPLEMENTARY_EMPTY",
                node_id=skeleton[branch_index][1],
                message=(f"分支「{title}」按子节点「{query[:40]}」补查了一次，"
                         "仍然没有命中材料里的片段，这一支保持无引用"),
            ))
            return slots

        evidence = _merge_branch_evidence(
            branch_evidence[branch_index], _branch_evidence(hits, sources), sources)
        branch_evidence[branch_index] = evidence
        # 新证据同样要进本次运行的证据集：收尾的 `verify_source_refs` 按它判
        # "这条引用落在可见的父块里吗"。少记一条，模型照它抄的引用就会被判成编的。
        ctx.record_evidence(evidence.spans)
        if not ctx.reserve_model_call():
            return slots
        ctx.extra_model_calls += 1
        ask = (
            f"整体主题：{args.topic}\n"
            f"本条一级分支：{title}\n"
            f"请给出 {need} 个子要点。"
            f"{evidence_suffix(evidence)}"
        )
        try:
            raw = await ask_model(
                prompt_for(BRANCH_EXPAND_PROMPT, evidence), ask,
                parent=(branch_index,), quota=allowance)
        except GenerationError:
            return slots          # 重写失败只是"仍然按原来的内容交"，不升级成失败
        more, _ = _trim_slots(
            _candidate_slots(raw, point_budget, nested_levels, evidence=evidence),
            allowance,
        )
        added, _ = _keep_new_slots(slots, more)
        # 合并后仍要过一遍节点预算：两次响应各给满 `point_budget` 时会翻倍，
        # 而 `allowance` 是这一支在整树里能占的上限。
        merged, _ = _trim_slots([*slots, *added], allowance)
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.info, code="BRANCH_SUPPLEMENTARY_HITS",
            node_id=skeleton[branch_index][1],
            message=(f"分支「{title}」按子节点「{query[:40]}」补查了一次，"
                     f"命中 {len(hits)} 条，补出 {len(added)} 个要点"),
        ))
        return merged

    async def expand_branch(
        title: str, branch_index: int, evidence: _BranchEvidence | None,
    ) -> list[_Slot] | None:
        """展开一条分支。额度不够返回 None——那是"没展开"，与"展开失败"是两件事。

        `evidence` 由调用方在**检索完成之后**传进来（`run_branch`）：它是这一支
        自己的命中，兄弟分支的证据一个字都不进来。

        **名额由调用方持有**：流水线把「检索 + 展开 + 补查」包在同一个名额里
        （见 `run_branch`），所以这里不再自己拿锁——再拿一次会把这个非重入的
        `Semaphore` 卡死。

        展开之后多一道判据：这一支要不要**补查一次**（`_needs_supplementary`）。
        判定与检索都在工具内部，所以仍然**不占** `max_tool_calls`——模型看到的只是
        一次 `mindmap_generate_deep` 调用。
        """
        if not ctx.reserve_model_call():
            return None
        ctx.extra_model_calls += 1
        ask = (
            f"整体主题：{args.topic}\n"
            f"本条一级分支：{title}\n"
            f"请给出 {need} 个子要点。"
            f"{evidence_suffix(evidence)}"
        )
        # 这一路的产出挂在第 branch_index 条一级分支下面，所以父路径是
        # `(branch_index,)`。
        raw = await ask_model(
            prompt_for(BRANCH_EXPAND_PROMPT, evidence), ask,
            parent=(branch_index,), quota=allowance)
        kept, _ = _trim_slots(
            _candidate_slots(raw, point_budget, nested_levels, evidence=evidence),
            allowance,
        )
        if not (slicing and _needs_supplementary(raw, kept)):
            return kept
        return await supplement(title, branch_index, kept)

    # ---- 流水线的两个闸：检索屏障与整树兜底 -------------------------------------
    #
    # 为什么需要一个屏障：**"所有支都没有证据 → 退整整树一次检索"这条降级要等
    # 全部门落定才成立**。把它写进流水线有两种错法：一是把判定提前到骨架之后
    # （那就得等全部检索，等于退回两阶段），二是干脆不判（全空时每支都按"无依据"
    # 展开，交出一棵没有出处的树）。这一版的做法是——屏障只挡住**没拿到证据的那
    # 几支**，有证据的支在屏障期间照样在自己那条流水线上展开。典型情形（有材料、
    # 有命中）下无人等屏障，总时长与"没有兜底这件事"相同。
    #
    # 名额必须**先释放再等**：20 支的上限下若有 10 支空着、各自占着名额等屏障，
    # 后面的支连检索都发不出去，屏障永远不会完成——那是死锁，不是慢。所以空支在
    # `async with semaphore` 退出之后才 await 屏障，真正展开时再拿一次名额。
    pending_searches = sum(1 for done in search_done if not done)
    all_searched = asyncio.Event()
    fallback_ready = asyncio.Event()
    if pending_searches == 0:
        all_searched.set()
        fallback_ready.set()

    def record_search(branch_index: int, hits: list[EvidenceHit] | None) -> None:
        """记下这一支的检索结局，并在最后一支落定时打开屏障。"""
        nonlocal pending_searches
        search_done[branch_index] = True
        search_outcome[branch_index] = (
            "failed" if hits is None else ("hits" if hits else "empty"))
        if hits is None:
            retrieval_failed.append(branch_index)
        pending_searches -= 1
        if pending_searches <= 0:
            all_searched.set()

    async def settle_fallback() -> None:
        """全部门落定之后判一次"是不是一条证据都没拿到"，是就整树兜底一次。

        这是流水线上唯一的屏障，而它挡住的只有没拿到证据的那几支（见上面那段）。
        代价与上一版相同：兜底那一次检索本来就要等所有支，只是它现在与"有证据的
        支的展开"重叠，不再把整体推后一整轮。

        `finally` 里无条件放行：兜底自己抛错（`_retrieve_whole` 已吞掉检索异常，
        但 `_branch_evidence` 仍可能被将来的改动弄成会抛）也必须让空支继续走
        "无证据展开"——否则 `fallback_ready` 永远不会置位，整条流水线挂死在这里。
        """
        nonlocal retrieval_mode, whole_count
        try:
            if not slicing:
                return
            await all_searched.wait()
            if any(outcome == "hits" for outcome in search_outcome):
                return
            whole = await _retrieve_whole(sources, args.topic, branch_titles)
            retrieval_mode = "whole"
            retrieval_queries[:] = [" ".join([args.topic.strip(), *branch_titles]).strip()]
            whole_count = len(whole)
            # 兜底命中要**原样给每一支**：这是"整树一次检索"的定义，每支看到的
            # 是同一批证据；分片的收益在这一刻归零，但那好过一棵空树。
            for index in range(len(skeleton)):
                if not whole:
                    branch_evidence[index] = None
                    search_outcome[index] = "empty"
                    continue
                evidence = _branch_evidence(whole, sources)
                branch_evidence[index] = evidence
                ctx.record_evidence(evidence.spans)
                search_outcome[index] = "hits"
        finally:
            fallback_ready.set()

    async def run_branch(title: str, branch_index: int) -> list[_Slot] | None:
        """一支的「检索 → 展开」：**检索一回来就发它自己的展开，不等其他支**。

        上一版是两阶段（`_retrieve_branches` 全部跑完 → `expand_branch` 一批并发）：
        骨架落定到第一个子节点之间隔着"最慢的一支检索"，实测 1.5–2 秒。流水线之后
        每一支的检索一回来就立刻发它自己的展开，空窗只剩**第一支的检索**——也就是
        块向量预热的那一次（`first_hits`，已经在骨架之后先跑完了）。

        一个名额 = 一支正在流水线上的活：`semaphore` 包住「检索 + 展开 + 补查」
        整体，而不是只包展开。有证据的支在自己的名额里一路跑到底。

        检索失败（`None`）与检索为空（`[]`）走同一条路：这一支按"无证据"展开，
        与 `_search_branch` 的失败语义一致，且不牵连任何其他支（`safe_search` 保证
        检索异常不会从这里漏出去）。
        """
        hits = first_hits if (slicing and search_done[branch_index]) else None
        async with semaphore:
            if slicing and not search_done[branch_index]:
                hits = await safe_search(title, branch_index)
                record_search(branch_index, hits)
            if slicing:
                if hits:
                    evidence = _branch_evidence(hits, sources)
                    branch_evidence[branch_index] = evidence
                    # 累积进 `RunContext.evidence`：去重（四元组）与 200 条上限都在
                    # 那里，这里只管往里放。收尾的 `verify_source_refs` 读的也是它
                    # ——两处同源，否则"校验依据"与"模型看到的证据"是两份不同的东西。
                    ctx.record_evidence(evidence.spans)
                    return await expand_branch(title, branch_index, evidence)
            else:
                # `full` 形态不做按支检索：这一支从第一行起就没有证据。
                return await expand_branch(title, branch_index, None)
        # 没拿到证据：先让出名额再等屏障（死锁的理由见上面那段），兜底若给了
        # 证据就带它展开，没给就按"无证据"展开。
        await fallback_ready.wait()
        async with semaphore:
            return await expand_branch(title, branch_index, branch_evidence[branch_index])

    # 第二步：流水线。`settle_fallback` 是唯一不被 semaphore 约束的任务——它只
    # 等屏障，然后最多做一次整树兜底检索。
    results = await asyncio.gather(
        settle_fallback(),
        *(run_branch(title, index) for index, (title, _) in enumerate(skeleton)),
        return_exceptions=True,
    )
    settled, results = results[0], results[1:]
    if isinstance(settled, BaseException):  # pragma: no cover — `finally` 已保证放行
        logger.warning("按支检索的全空判定出错（不影响展开）：%s: %s",
                       type(settled).__name__, str(settled)[:160])

    # 检索诊断在这里补齐，顺序与上一版逐条相同（兜底 → 抛错 → 没命中），位置由
    # `diagnostics_before_search` 钉住：骨架诊断之后、展开期诊断（补查那几条）之前。
    # 流水线化让这些事实的**产生**时刻散落在各支里，但它们在诊断列表里的位置不该
    # 因此变化——那是既有用例与用户看到的面板顺序。
    if slicing:
        retrieval_failed.sort()
        retrieval_empty = sorted(
            index for index, outcome in enumerate(search_outcome) if outcome == "empty")
        if whole_count is not None:
            retrieval_diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn, code="BRANCH_RETRIEVAL_FALLBACK",
                message=(
                    f"{len(skeleton)} 条分支按支检索都没有拿到证据，已退回整树一次检索"
                    f"（{whole_count} 条命中，全部支共用）；分片检索的覆盖收益本次没有生效"
                    if whole_count else
                    f"{len(skeleton)} 条分支按支检索都没有拿到证据，整树兜底检索也没命中，"
                    "各支将按「无依据」展开"
                ),
            ))
        for index in retrieval_failed:
            retrieval_diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn, code="BRANCH_RETRIEVAL_FAILED",
                node_id=skeleton[index][1],
                message=(
                    f"分支「{skeleton[index][0]}」的检索出错，这一支按「无检索结果」展开"
                    "（其余分支不受影响）"
                ),
            ))
        for index in retrieval_empty:
            retrieval_diagnostics.append(Diagnostic(
                level=DiagnosticLevel.info, code="BRANCH_NO_EVIDENCE",
                node_id=skeleton[index][1],
                message=(
                    f"分支「{skeleton[index][0]}」的检索没有命中材料里的任何片段，"
                    "这一支按自己的知识展开、不挂引用（未检索到不等于材料里没有）"
                ),
            ))
        diagnostics[diagnostics_before_search:diagnostics_before_search] = (
            retrieval_diagnostics)

    # 异常不许扩散到其他分支：某支 429 / 超时，只该让那一支空着。
    slots_by_branch: list[list[_Slot]] = []
    # 分支下标 -> 被丢掉的重复要点条数。首轮与补点都要记：重复是模型行为，
    # 只记补点那一轮会让「首轮就给了两条一样的」变成一次静默的少一条。
    duplicates: dict[int, int] = {}
    skipped: list[int] = []
    failed: list[tuple[int, str]] = []
    for index, (_, item) in enumerate(zip(skeleton, results)):
        if item is None:
            skipped.append(index)
            slots_by_branch.append([])
        elif isinstance(item, BaseException):
            failed.append((index, _reason_text(item)))
            slots_by_branch.append([])
        else:
            kept, dropped = _keep_new_slots([], list(item))
            if dropped:
                duplicates[index] = dropped
            slots_by_branch.append(kept)

    skipped_set = set(skipped)
    failed_set = {index for index, _ in failed}

    # 第二步（续）：局部重试。只补「要点数不足」的那几支，骨架不重来——一次
    # 质量波动不该把已经展开好的分支一起作废。抛异常的分支不在此列：它已经花掉
    # 一次调用，盲目再打一次更可能只是把 429 重放一遍。
    retried: list[int] = []
    for index, (title, _) in enumerate(skeleton):
        if index in skipped_set or index in failed_set:
            continue
        missing = need - len(slots_by_branch[index])
        if missing <= 0:
            continue
        if not ctx.reserve_model_call():
            # 额度用完就停手：继续调只会全部失败，不如把已展开的部分交出去
            break
        ctx.extra_model_calls += 1
        retried.append(index)
        # 已有要点必须一起给：模型看不到自己已经写了什么，却被要求「不要重复」，
        # 那这条要求它根本没法执行——实测同一要点被返回两次，数量验收还通过了。
        existing = "、".join(slot.label for slot in slots_by_branch[index]) or "（暂无）"
        evidence = branch_evidence[index]
        ask = (
            f"整体主题：{args.topic}\n"
            f"本条一级分支：{title}\n"
            f"已有要点：{existing}\n"
            f"还差 {missing} 个子要点，请补齐，不要重复已有要点。"
            f"{evidence_suffix(evidence)}"
        )
        try:
            # 补调出来的要点接在已有要点之后，所以这一路的子索引从"已有几个"起算；
            # 不带上这个偏移，预览会把补出来的节点挂到已有节点的位置上。
            raw = await ask_model(
                prompt_for(BRANCH_EXPAND_PROMPT, evidence), ask, parent=(index,),
                base_index=len(slots_by_branch[index]), quota=missing,
            )
        except GenerationError:
            continue  # 补调失败只是"仍然不足"，不升级成整次失败
        kept, dropped = _keep_new_slots(
            slots_by_branch[index],
            _candidate_slots(raw, missing, evidence=evidence),
        )
        if dropped:
            duplicates[index] = duplicates.get(index, 0) + dropped
        slots_by_branch[index].extend(kept)

    # 所有分支都没留下内容、且没有任何一支是因为额度被跳过 → 没有可交付的树。
    # 反过来，如果有分支是"额度不够没展开"，骨架本身仍然有效，照常交出。
    if not any(slots_by_branch) and not skipped_set:
        detail = "；".join(message for _, message in failed) or "模型没有返回可用的子要点"
        return _fail(
            "all_branches_failed",
            f"{len(skeleton)} 个分支全都展开失败：{detail}",
            failed_branches=[skeleton[index][0] for index in sorted(failed_set)],
        )

    # 第三步：第三层往下，按层递归展开。层间串行、层内并发（理由见 docstring）。
    # 每一层的额度都按"该层父节点数"重新平分，所以某一支长得再胖也不会把后面
    # 的支挤成空壳；层内先算份额再发请求，是为了不让并发的调用各自以为自己有额度。
    all_slots: list[_Slot] = [slot for slots in slots_by_branch for slot in slots]
    # 实际尝试过的层号。验收只对"试过的层"报缺失：没试过的层本来就该是空的。
    expected: set[int] = {2} if all_slots else set()
    level_cut: dict[int, int] = {}  # 层 -> 因节点预算没展开的父节点数
    level_skipped: dict[int, int] = {}  # 层 -> 因调用额度没展开的父节点数
    level_failed: dict[int, int] = {}  # 层 -> 展开抛错的父节点数
    level_reason: dict[int, str] = {}  # 层 -> 第一条失败原因（诊断里要能看出是什么错）
    level_empty: dict[int, int] = {}  # 层 -> 模型答了但没给出可用子节点的父节点数

    async def expand_node(
        slot: _Slot, path: str, indices: tuple[int, ...], count: int, nested_left: int,
        evidence: _BranchEvidence | None,
    ) -> list[_Slot] | None:
        """展开一层里的一个节点。额度不够返回 None（没展开），抛错则由调用方收拾。

        `path` 是给人看的文字路径，`indices` 是同一次遍历给出的子索引链——上报
        生长事件时后者才是指向"这是哪个节点"的东西。

        `evidence` 是**这一支**的检索结果：第 3 层往下同样只能引用本支证据。
        不传下去的话，"按支检索"就只对第 2 层成立——深层节点要么没有引用，要么
        被收尾校验剔光，两种都让这一支看起来像"材料里没有"。
        """
        async with semaphore:
            if not ctx.reserve_model_call():
                return None
            ctx.extra_model_calls += 1
            # 路径必须一起给：并发展开时它看不到同一个父节点下的兄弟写了什么，
            # 没有"我在哪"的锚点就会写出与上一级同一句话的内容。
            ask = (
                f"整体主题：{args.topic}\n"
                f"从一级分支到这里的路径：{path}\n"
                f"本节点：{slot.label}\n"
                f"请给出 {count} 个子要点。"
                f"{evidence_suffix(evidence)}"
            )
            raw = await ask_model(
                prompt_for(SUBNODE_EXPAND_PROMPT, evidence), ask, parent=indices,
                quota=count)
            return _candidate_slots(raw, count, nested_left, evidence=evidence)

    for depth in range(3, max_depth + 1):
        # `depth` 是**这一轮要产出的层号**，父节点在它上面一层：第 3 层的父节点
        # 是第 2 层的节点（一级分支的直接下级）。
        # 按分支分别遍历而不是先把第 2 层摊平：索引链的起点是"这条分支在根下面
        # 排第几"，摊平之后就只剩一个混在一起的全局下标，再也拼不出父子路径。
        parents: list[tuple[_Slot, str, tuple[int, ...], _BranchEvidence | None]] = []
        for branch_index, slots in enumerate(slots_by_branch):
            parents.extend(
                (slot, path, indices, branch_evidence[branch_index])
                for slot, parent_depth, path, indices
                in _walk_slots(slots, 2, "", max_depth, (branch_index,))
                if parent_depth == depth - 1
            )
        if not parents:
            break
        expected.add(depth)
        used = 1 + len(skeleton) + _count_slots(all_slots)
        share = max(0, node_limit - used) // len(parents)
        breadth = min(fanout, share)
        if breadth < DEEP_MIN_BREADTH:
            # 剩下的额度连两个子节点都装不下：发一次调用只换到一个节点，不值。
            # 记成"被节点预算截断"，已经展开的部分照常交出。
            level_cut[depth] = len(parents)
            # 这几个父节点没被问过，所以它们是不是"到底了"**没有人知道**。标成
            # `truncated`，落树时它们是 `unknown` 而不是 `no`——界面上按钮仍然可点，
            # 用户再点一次要么成功、要么拿到明确的 `tree_full`。
            for parent_slot, _path, _indices, _evidence in parents:
                parent_slot.truncated = True
            break
        nested_left = max(0, max_depth - depth)
        # 这一次响应最多能落多少个节点：结构上限、额度份额、单次输出容量三者取小。
        # 不取小的话，模型每层都多给一层时总节点会按扇出的幂次膨胀，最后撞在
        # 整树校验上——那时额度已经花完了。第三个上限的理由见 `_nodes_per_call`：
        # 额度算术允许 6 个节点，不代表这一次请求装得下 6 个满额节点。
        level_allowance = min(
            breadth * (1 + _nested_capacity(fanout, nested_left)),
            share,
            per_call_nodes,
        )
        level_results = await asyncio.gather(
            *(expand_node(slot, path, indices, breadth, nested_left, evidence)
              for slot, path, indices, evidence in parents),
            return_exceptions=True,
        )
        for (slot, _path, _indices, _evidence), item in zip(parents, level_results):
            # 三种"没长出来"都要在**节点自己**身上留痕：它们是 `unknown` 的来源
            # （见 `mindmap.NODE_EXPANDABLE` 与 `_Slot.truncated`）。只看层级计数
            # 不够——`level_cut` 只知道"这一层有几个没展开"，不知道是哪几个。
            if item is None:
                level_skipped[depth] = level_skipped.get(depth, 0) + 1
                slot.truncated = True
            elif isinstance(item, BaseException):
                level_failed[depth] = level_failed.get(depth, 0) + 1
                level_reason.setdefault(depth, _reason_text(item))
                slot.truncated = True
            else:
                kept, _ = _trim_slots(list(item), level_allowance)
                if kept:
                    slot.children = kept
                else:
                    level_empty[depth] = level_empty.get(depth, 0) + 1
                    slot.truncated = True

    # 第四步：合并。骨架先成树（分支 id 已在骨架里），再把所有节点按"父在子前"
    # 的顺序作为一批 add 应用上去——整批原子，半截树不会进 ctx.candidate。
    #
    # `source_usage` 必须在**建树之前**定下来：契约的第二层校验要求每条
    # `source_ref.source_id` 都出现在 `source_usage` 里，缺一条整棵树就是
    # `tree_invalid`。分片路不走 `validate_generated_content`（那条整树校验路已
    # 随 `_generate` 删除），只能自己拼——而"这条路上会产出引用"意味着不拼就一定炸。
    #
    # 用 `_verify_sources(ctx)` 而不是局部 `sources`：这条路径的引用只可能落在
    # 请求内材料上（分片检索只跑 `sources`），但"校验认的来源集合"与"source_usage
    # 列出的集合"必须是同一个，否则将来任何一方向库扩展都会先在契约校验上炸一次，
    # 而那次失败看起来会像"模型返回的树不合法"。
    verify_sources = _verify_sources(ctx) if slicing else []
    # 这次"认哪些来源"的集合：`source_usage` 与引用过滤共用它，两处口径必须一致
    # （见 `emit` 里那句注释：不一致会让整批 op 校验失败）。
    allowed_source_ids = {source.id for source in verify_sources}
    content = MindMapContent(
        title=root_label,
        root=Node(
            id=root_id,
            label=root_label,
            children=[
                # 一级分支带 `color` / `icon`（骨架那一次要出来的视觉元素，见
                # `PLAN_PROMPT`）。**只有这一层**：二级往下在建树时一律不带，
                # 与提示词里那条边界是同一个决定。
                Node(
                    id=branch_id,
                    label=title,
                    children=[],
                    color=branch_visual[branch_id].color,
                    icon=branch_visual[branch_id].icon,
                )
                for title, branch_id in skeleton
            ],
        ),
        source_usage=[
            SourceUsage(source_id=source.id, kind=source.kind,
                        filename=source.filename,
                        char_count=len((source.text or "").strip()))
            for source in verify_sources
        ],
        # `tree` / `fishbone` 到这里是空列表：上面那段只在 flowchart 时填它。
        edges=edges,
        # 产物自己声明"我是什么图型"。不加这一行的后果是这张图**无法自证**：
        # 一张六支的鱼骨图与一张六支的思维导图在树形状上可以完全一样，而
        # 「要不要摆附加关系区块」「导出文件头怎么写」两件事都只能靠这个字段。
        diagram_type=diagram_type,
    )

    drafts: list[dict[str, Any]] = []
    # 临时 id -> 这个节点落树时的 `expandable`（判定见 `_expandable_for_slot`）。
    # 用临时 id 记是因为真实 id 由 `normalize_ops` 分配；映射在它之后才有。
    slot_states: dict[str, str] = {}

    def emit(parent_id: str, key: str, slots: list[_Slot]) -> None:
        """把候选子树摊平成 add 草案：父节点一定排在自己子节点前面。"""
        for position, slot in enumerate(slots):
            child_key = f"{key}-{position}"
            child_id = f"{TEMP_ID_PREFIX}d{child_key}"
            slot_states[child_id] = _expandable_for_slot(slot)
            drafts.append({
                "op": "add",
                "id": child_id,
                "parent_id": parent_id,
                "index": position,
                "label": slot.label,
                # `detail` 原样带上（`_slot_from` 已按契约上限截过）：两层节点的
                # 长文本走这里落进树，画布上的 label 才可能真的短。
                "detail": slot.detail,
                # 引用在 `_refs_from` 里已经清洗过（本支证据、契约形状、条数），
                # 这里只再按"这次认哪些来源"筛一遍：`source_usage` 是用
                # `verify_sources` 拼的，而契约要求每条 `source_ref.source_id`
                # 都在它里面。请求带了材料但没一份通过 `_verify_sources`（比如
                # 客户端漏了 `status: "parsed"`）时，模型仍会按材料清单写引用，
                # 不过滤就会整批 op 校验失败、这次调用白跑。
                "source_refs": [
                    ref for ref in slot.source_refs
                    if _ref_source_id(ref) in allowed_source_ids
                ],
            })
            if slot.children:
                emit(child_id, child_key, slot.children)

    for index, (_, branch_id) in enumerate(skeleton):
        emit(branch_id, str(index), slots_by_branch[index])
        # 一级分支自己：它有没有下一层，只有"这一支展开过没有"知道。因额度被跳过
        # （`skipped`）或整支抛错（`failed`）的那几支从没被问过，因此是 `unknown`；
        # 展开过但模型一条要点都没给的，同样没人能断言它到底了。
        if index in skipped_set or index in failed_set or not slots_by_branch[index]:
            slot_states[branch_id] = "unknown"

    if drafts:
        ops, id_map = normalize_ops(drafts, shapes.node_ids(content))
        batch = OpBatch(batch_id=_batch_id(ctx), base_revision=ctx.revision, ops=ops)
        try:
            content = apply_ops(content, batch)
        except OpError as exc:
            return _fail(exc.code, f"{exc.path}：{exc.message}", path=exc.path)
    else:
        id_map = {}

    # `expandable` 的三态在**任何**校验之前写进树里：它不是一个附加视图，而是
    # `Node` 的字段之一，越晚写越容易漏掉某条校验路径（后面还有一次整树
    # `model_validate`，它会把写坏的值直接判成 `invalid_tree`——那正是我们要的）。
    #
    # `max_depth` 传契约的 8 层，**不是**本次的 `args.max_depth`：后者是"这一次
    # 长到哪一层"的预算，被它挡住的叶子仍然可以再展开（界面上按钮可点），把
    # 它们记成 `no` 等于告诉用户"到头了"，而这句话只有模型或第 8 层说得出口。
    _stamp_expandable(
        content,
        {id_map.get(raw_id, raw_id): state for raw_id, state in slot_states.items()},
        max_depth=mindmap_contract.MAX_TREE_DEPTH,
    )

    # `origin` 同理，而且**必须**在这里刷：这条路把节点做成 `add` 草案交给
    # `apply_ops` 落树，而 op 执行器把所有 add 出来的节点记成 `manual`（那是编辑轮
    # 的语义）。不刷的话，一张分片生成出来的整图会被界面说成"不来自生成链路"——
    # 每一支都检索过、每一条引用都逐字核过，那句话会劝用户"不必处理"。
    _stamp_origin(content)

    # 第五步：确定性验收，全部用代码，不调模型。整树重跑一次契约校验——
    # `apply_ops` 内部也做了，但"骨架没有任何要点"时不会经过它（没有 op 可应用），
    # 只靠那边会把一棵只剩根和空分支的树放过去。
    try:
        content = MindMapContent.model_validate(content.model_dump())
    except ValidationError as exc:
        detail = "；".join(error["msg"] for error in exc.errors()[:3])
        return _fail("invalid_tree", f"合并后的导图不合法：{detail}")

    # 整棵树的形状到这里才定下来，这时数「模型声明了什么」才准：第 2 层在
    # `expand_branch` 里裁过一道，第 3 层往下是逐步挂到 children 上的，早一步数
    # 会漏掉下层节点给出的标记。
    leaf_declared, expandable_declared = _count_expand_flags(all_slots)

    audit = _audit_tree(content.root, expected)
    # 总节点超契约上限 = 结构错误：这样的树连存都存不进去，不能当成功交出去。
    # 预分配与逐层裁剪都是为了让它不发生，走到这里说明预算算术本身出了岔子。
    if audit.total > node_limit:
        return _fail(
            "too_many_nodes",
            f"生成的树有 {audit.total} 个节点，超过契约上限 {node_limit}。",
            node_count=audit.total,
            node_limit=node_limit,
        )
    # 深度超预算同理：每一层展开前都判过 `depth <= max_depth`，越界说明递归的
    # 终止条件坏了。让它在这里硬失败，比交出一棵"悄悄多一层"的树更容易发现。
    if audit.deepest > max_depth:
        return _fail(
            "too_deep",
            f"生成的树深 {audit.deepest + 1} 层（含中心主题），超过本次的 {max_depth + 1} 层预算。",
            depth_limit=max_depth,
        )
    if audit.blank:
        return _fail("empty_label", f"有 {len(audit.blank)} 个节点的标题是空的，这张图不能落库。")

    sparse, overfull = _audit_branches(content, need)
    if overfull:
        return _fail(
            "too_many_points",
            f"分支「{'、'.join(overfull)}」的要点数超过上限 {MAX_CHILDREN}，这张图不能落库。",
        )

    # 第五步（续）：引用核验。分片形态下树里的每一条引用都声称来自某次分支检索，
    # 这里逐条拿原文比对，站不住的一律剔掉——**引用准确率不能靠提示词保证**。
    #
    # 为什么这里只留聚合结果：整树校验路（`validate_generated_content`，已随
    # `_generate` 删除）给出的 `UNSOURCED_NODE` 是逐节点一条的；分片出来的树动辄
    # 几百节点，几百条"没有引用"的提示会把真正有用的诊断淹掉，也不给用户任何
    # 可操作的信息。所以这里只留**聚合**结果，明细（按 code 计数）进 data。
    refs_kept = refs_dropped = 0
    refs_by_code: dict[str, int] = {}
    if slicing:
        before = _count_refs(content.root)
        raw_reports = verify_source_refs(
            content, verify_sources, remove_invalid=True, evidence=ctx.evidence or None)
        refs_kept = _count_refs(content.root)
        refs_dropped = before - refs_kept
        for item in raw_reports:
            if item.code in _PER_NODE_REF_CODES:
                continue
            refs_by_code[item.code] = refs_by_code.get(item.code, 0) + 1
        if refs_dropped:
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn, code="SOURCE_REFS_DROPPED",
                message=(
                    f"{refs_dropped} 条引用没有通过逐字校验（引文与原文对不上，"
                    f"或落在本次检索命中之外），已剔除；保留 {refs_kept} 条"
                ),
            ))

    # ---- 结构体检（`check`）：整树路删掉之后，这里是新建导图唯一的形态诊断 ------
    #
    # 这两处（本段与下面那段 `annotate_nli`）原本接在 `_generate` 的收尾，随整树生成
    # 一起被删掉，于是新建一张图既没有结构体检、也没有语义层的引用检查。
    #
    # **分片比整树更需要 `check()`**：某一支模型没展开时，树上留下的正是「一个父节点
    # 只有一个子节点」——`SINGLE_CHILD` 就是为这个准备的；而整树一次成型时模型总把
    # 每支要点数给满，它几乎不触发。深度诊断同一套道理：`max_depth` 是本次预算，
    # `check()` 用的是契约的 8 层上限，它只在"树真的长很深"时才响。
    #
    # 来源传 `_verify_sources(ctx)`（口径见它的 docstring）：上面那次剔除用的是同一份，
    # 两处必须一致，否则"校验认得的来源"与"体检认得的来源"会在库来源上分叉。没有
    # `slicing` 时传 `None`——那种树没有过一次检索，也就不该有引用，跑第三层只会给
    # 每个节点报一条 `UNSOURCED_NODE`（正是 `_PER_NODE_REF_CODES` 里那类噪声）。
    #
    # 第三层在分片路上**是冗余的**（上面已有同一函数、更宽的来源、`remove_invalid=True`
    # 跑过一遍），留着它只为两件事：来源口径不分叉；以及"多处出现的引文"这类**没有被
    # 剔除、只被标待核**的判定还能在诊断里露头一次。
    health = [item for item in check(content, verify_sources or None)
              if item.code not in _PER_NODE_REF_CODES]
    diagnostics.extend(health)
    single_child = sum(1 for item in health if item.code == "SINGLE_CHILD")

    # ---- 语义层（`annotate_nli`）：每条已通过逐字校验的引用各打一次分 -----------
    #
    # **必须走 `to_thread`**：`nli.judge` 是 onnxruntime 的同步 CPU 推理（单条实测
    # 15–44 ms，`MAX_NLI_CHECKS=100` → 上限 4.4 秒），首次调用还会把权重加载进来。
    # 直接在事件循环里跑会把这几秒里的**所有**并发堵死——与 `search_parent` 被
    # `to_thread` 包住是同一种性质，只是阻塞源从网络换成了 CPU。
    #
    # 两道闸（`nli.on_generate()` 内含 `_enabled()`）：模型没启用、或生成路径这道
    # 开关被显式关掉，这里一次调用都不发生，`annotate_nli` 的重依赖也一个都不 import。
    # 默认值按实测定（`scripts/measure_generation_diagnostics.py`）：8 支树 24 条
    # 引用 0.37–1.07 s、20 支（工具上限）40 条 0.62–1.79 s，低于"每次生成多等
    # 三秒"的界，所以**模型启用时它默认就跑**；撞满 `MAX_NLI_CHECKS=100` 才是
    # 1.5–4.4 s，那种图设 `MINDMAP_NLI_ON_GENERATE=0` 关掉即可。
    #
    # 失败一律降级：语义复核是附加评分，它自己出错不该让一次生成失败。
    nli_diagnostics: list[Diagnostic] = []
    # 记成"这次**真的**尝试过"而不是"开关开着"：没有 `slicing` 的树上没有引用可
    # 判，`data.nli.on_generate=True` + `judged=0` 会被读成"跑了但一条都没判"。
    nli_attempted = slicing and nli.on_generate()
    if nli_attempted:
        try:
            nli_diagnostics = await asyncio.to_thread(
                annotate_nli, content, verify_sources)
        except Exception as exc:  # noqa: BLE001 - 语义层失败只是没有评分
            logger.warning("NLI 语义复核失败（%s）：%s",
                           type(exc).__name__, str(exc)[:160])
            nli_diagnostics = []
    # 相反的那几条逐条留（要人核，必须能点回节点）；其余（entailment / neutral）
    # 只留一个计数——「每条引用一条 info」在一棵几百引用的树上就是噪声，与上面
    # `_PER_NODE_REF_CODES` 是同一条理由。
    contradicted = [item for item in nli_diagnostics
                    if item.code == "SOURCE_REF_CONTRADICTED"]
    diagnostics.extend(contradicted)
    nli_agree = len(nli_diagnostics) - len(contradicted)
    if nli_agree:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.info, code="SOURCE_REF_NLI",
            message=(f"{nli_agree} 条引用的语义判定不是「相反」（中性或蕴含），"
                     "无冲突，未逐条列出"),
        ))

    retried_set = set(retried)
    # 收尾仍不足的分支：(下标, 标题, 差几个)。这是 `partial` 的第三个来源——
    # 前两个（额度跳过、展开抛错）都在"展开阶段"就留了空，而这一种是"展开成功
    # 但内容不够、补点也没补上"。只看前两个的话，某支 0 个要点也会报完整成功。
    short: list[tuple[int, str, int]] = []
    for index, branch_id, title, count in sparse:
        # 因额度没展开、或因异常留空的分支已经由下面两条诊断说清楚了；
        # 这里再报一条「点数不足」等于对同一个原因告警两次。
        if index in skipped_set or index in failed_set:
            continue
        short.append((index, title, need - count))
        tail = ("已补调一次模型，仍不足，按现状接受" if index in retried_set
                else "模型调用额度不足，未能补调")
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="SPARSE_BRANCH", node_id=branch_id,
            message=f"分支「{title}」只有 {count} 个要点，少于要求的 {need} 个；{tail}",
        ))
    for index, message in failed:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="BRANCH_FAILED", node_id=skeleton[index][1],
            message=f"分支「{skeleton[index][0]}」展开失败，已留空：{message}",
        ))
    for index in skipped:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="BRANCH_SKIPPED_BUDGET", node_id=skeleton[index][1],
            message=f"因模型调用额度不足，分支「{skeleton[index][0]}」没有展开，已留空",
        ))
    for index in sorted(duplicates):
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="DUPLICATE_POINT", node_id=skeleton[index][1],
            message=(
                f"分支「{skeleton[index][0]}」返回了 {duplicates[index]} 条与已有要点重复的内容，"
                f"已去重；去重后这一支有 {len(slots_by_branch[index])} 个要点"
            ),
        ))
    for depth in sorted(level_cut):
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="NODE_BUDGET_CUT",
            message=(
                f"整树节点预算（契约上限 {node_limit}）在第 {depth} 层用尽，"
                f"{level_cut[depth]} 个节点没有继续往下展开"
            ),
        ))
    for depth in sorted(level_skipped):
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="LEVEL_SKIPPED_BUDGET",
            message=(
                f"因模型调用额度不足，第 {depth} 层有 {level_skipped[depth]} 个节点没有展开，已留空"
            ),
        ))
    for depth in sorted(level_failed):
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="LEVEL_FAILED",
            message=(
                f"第 {depth} 层有 {level_failed[depth]} 个节点展开出错，已留空："
                f"{level_reason.get(depth, '未知错误')}"
            ),
        ))
    # 试过却一层都没长出来：这是"只展开第一层就停"的直接信号。因额度或抛错
    # 而空的那几层不在此列——它们各自已经有上面那两条诊断，重复报告只会淹没原因。
    for depth in audit.missing:
        if level_skipped.get(depth) or level_failed.get(depth):
            continue
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="SHALLOW_LEVEL",
            message=(
                f"第 {depth} 层一个节点都没有：{level_empty.get(depth, 0)} 个父节点的展开"
                "没有返回可用的子节点"
            ),
        ))
    # 「模型到底会不会用 expandable」是这个改动唯一的成败判据，所以它必须在
    # diagnostics 里可见：树已经顶到层数上限，却一个 false 都没收到，说明提示词
    # 里的字段被无视了——此时树的深浅仍完全由 max_depth 决定，等于这次改动没生效。
    # 只看 `leaf_declared`：模型把每一项都标成 true，同样等于没给出"到此为止"的判断。
    if leaf_declared == 0 and audit.deepest >= max_depth:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="EXPAND_FLAG_IGNORED",
            message=(
                f"展开到第 {max_depth} 层，但模型没有把任何子项标为不可展开"
                "（expandable: false）：可展开标记可能被忽略，深浅仍由层数上限决定"
            ),
        ))

    expanded = len(skeleton) - len(skipped_set) - len(failed_set)
    cut_total = sum(level_cut.values())
    shallow = [item for item in audit.missing if not (level_skipped.get(item) or level_failed.get(item))]
    # 五种"没做满"都算 partial：额度跳过、展开抛错、补点后仍不足、节点预算截断、
    # 某一层没长出来。少最后两种时实测最贵的树仍报 partial=false——用户看不出
    # 自己拿到的是"到顶了"还是"中间断了"。
    partial = (
        bool(skipped_set) or bool(failed_set) or bool(short)
        or bool(cut_total) or bool(level_skipped) or bool(level_failed) or bool(shallow)
    )

    ctx.publish_tree(content)
    count = _count_nodes(content.root)

    # ---- 三条"这次依据从哪来 / 为什么没有依据"----------------------------------
    #
    # 三句话术互不相同，因为用户要能从 `content` 里读懂"为什么节点上没有引用"：
    #
    #   A. `MATERIAL_TOO_SHORT`  —— 材料太短，被换成了主题提示（有没有外部补全分开说）；
    #   B. `GENERATION_FROM_EXTERNAL` —— 引用来自外部检索，可信度该怎么看；
    #   C. `GENERATION_WITHOUT_EXTERNAL` —— 连外部都没搜到，只有模型自己的知识。
    #
    # 三条都**前置**（而不是 append）：前端的诊断面板只列前三条
    # （`AgentPanel.DIAGNOSTIC_LIMIT=3`），而下面有十几条逐支告警
    # （`SPARSE_BRANCH` / `BRANCH_NO_EVIDENCE` …）。append 的话用户看到的永远是
    # "某支要点不足"，而"这次依据来自外部检索"被挤到看不见——那句话恰好是他判断
    # 整棵树该信几成的唯一依据。A/B 的句子只挑一条（两句都在会读成两件不相干的事），
    # 但诊断各自保留：遥测按 code 聚合，它们是正交的事实。
    with_evidence = sum(1 for item in branch_evidence if item is not None)
    evidence_fed = with_evidence > 0
    external_empty = external is not None and not external.sources
    external_used = external is not None and bool(external.sources)

    lead: list[Diagnostic] = []
    short_note = None
    if short_material:
        short_note = (
            f"材料只有 {material_chars} 字，不足以支撑检索"
            f"（低于 {SHORT_MATERIAL_CHARS} 字时整份材料塞得进一条引用，"
            "引用落不到具体段落），这次按主题生成"
        )
        if evidence_fed:
            # 有外部正文进了 prompt：说清"依据从哪来"，别让用户以为那 8 个字撑起一棵树。
            short_note += (f"，并用外部检索到的 {len(sources)} 条资料"
                           f"（{_external_source_names(sources)}）补全依据。")
        elif external_used:
            # 搜到了资料，但这批资料里没检索出可用片段（语义上等价于"没搜到"）。
            short_note += "，只用模型自己的知识（外部资料没能检索出可用片段）。"
        elif external is not None:
            short_note += "，只用模型自己的知识（联网检索没有找到相关资料）。"
        else:
            short_note += "，只用模型自己的知识（外部检索未开启）。"
        lead.append(Diagnostic(
            level=DiagnosticLevel.warn, code="MATERIAL_TOO_SHORT",
            message=(
                f"材料总字数只有 {material_chars} 字（阈值 {SHORT_MATERIAL_CHARS} 字）："
                f"这个长度切不出可区分的检索单元，按材料生成只会得到"
                f"「引用落不到具体段落」的假依据，所以这次没有走材料路径，"
                f"{len(short_material)} 份材料只当作主题提示"
                + ("，依据由外部检索补上" if evidence_fed else "，依据来自模型自己的知识")
            ),
        ))

    # 外部检索走过、但一条都没拿到。两种原因分开说：
    # "代理没开/源挂了"换个时间重试可能就有了，"网上确实没有"重试也没用。
    external_note = None
    if external_empty:
        external_note = (f"外部检索出错（{external.failure}）" if external.failure
                         else "联网检索没有找到相关资料")
        lead.append(Diagnostic(
            level=DiagnosticLevel.warn, code="GENERATION_WITHOUT_EXTERNAL",
            message=(
                f"{external_note}：这张图只基于主题「{args.topic}」生成，"
                "没有外部资料依据，节点上也不会有指向原文的引用"
            ),
        ))

    # **第三种情况**：用户自己把联网检索关掉了。它与上面那条（搜了但没搜到）在界面
    # 上曾经共用一句话，而两者的正确反应相反：
    #
    #   - 关掉了：重试、换主题词都没有意义，唯一有用的是把开关打开；
    #   - 没搜到：开关是开的，换个说法或换个时间还有机会。
    #
    # 共用一句的后果是可观测的——用户会反复改主题去试一个**根本不会发生的搜索**，
    # 而每次那棵树都长得一样（无出处）。所以这里给它自己的 code 与自己的句子。
    #
    # 判据是三个"空"：没有走外部检索（`external is None`）、没有材料、也不是材料
    # 太短被降级（那条有自己的 `MATERIAL_TOO_SHORT`，里面已经说了"外部检索未开启"）。
    # 缺任何一条都会把"用户传了材料"误报成"你关了联网检索"。
    #
    # `info` 而不是 `warn`：这是用户主动做的选择，不是故障。
    external_disabled_note = None
    if external is None and not short_material and not sources:
        external_disabled_note = EXTERNAL_DISABLED_NOTE
        lead.append(Diagnostic(
            level=DiagnosticLevel.info, code="GENERATION_EXTERNAL_DISABLED",
            message=(
                "联网检索开关是关的：这张图完全基于模型知识生成，"
                f"节点上没有出处，也不会有指向原文的引用（主题「{args.topic}」）；"
                "打开联网检索再生成，引用就能逐字校验、点回原文"
            ),
        ))

    # 外部资料真的成了依据。这不是告警（树是好的），但用户必须知道这批引用该按
    # 什么标准看——逐字校验只保证"引文出自这个来源"，不保证"来源与主题相关"
    # （实测 `Rust 所有权` 会拿到"农业"：rust 是锈病）。
    external_used_note = None
    if external_used:
        lead.append(Diagnostic(
            level=DiagnosticLevel.info, code="GENERATION_FROM_EXTERNAL",
            message=(
                f"本次引用来自外部检索的 {len(external.sources)} 条资料"
                f"（{_external_source_names(external.sources)}）；"
                "逐字校验保证引文出自这些来源，不保证来源本身与主题高度相关"
            ),
        ))
        if not short_material:
            external_used_note = (
                f"本次有 {len(external.sources)} 条依据来自外部检索"
                f"（{_external_source_names(external.sources)}）："
                "引用逐字可校验、能点回原文，但外部内容不像你上传的材料那样权威，"
                "当作参考资料看即可。"
            )
    # 顺序是先因后果（材料太短在前，外部补全在后），与上面 `lead` 的顺序一致。
    diagnostics[:0] = lead

    content_parts = [f"已生成导图「{content.title}」，共 {count} 个节点。"]
    if short_note:
        content_parts.append(short_note)
    if external_note and not short_note:
        # C：外部检索是**用户看得见的一次等待**（可能到 20 秒），它空手而归时必须在
        # 用户读到的那句话里说清，否则"等了半分钟还是没有引用"无从解释。
        content_parts.append(
            f"{external_note}，这张图只基于主题「{args.topic}」生成，"
            "节点上不会有引用；要引用真实出处，请换一个更常见的主题词后重试。"
        )
    if external_disabled_note and not short_note:
        # C'：关掉开关的那一次。与 C 同一位置（都是"这次为什么没有引用"），但句子
        # 完全不同——这里给的下一步是"打开开关"，不是"换个主题词"。
        content_parts.append(external_disabled_note)
    if external_used_note:
        # B：搜到并用了。与 C 是两个方向的同一件事——用户要能分清"没有引用是因为
        # 没搜到"和"有引用是因为搜到了"。
        content_parts.append(external_used_note)
    if skipped_set:
        content_parts.append(
            f"因模型调用额度不足，只展开了 {expanded}/{len(skeleton)} 个分支，"
            "其余分支只保留了骨架。"
        )
    if failed_set:
        names = "、".join(skeleton[index][0] for index in sorted(failed_set))
        content_parts.append(f"{len(failed_set)} 个分支展开出错，已留空：{names}。")
    if short:
        detail = "、".join(f"「{title}」差 {missing} 个" for _, title, missing in short)
        content_parts.append(
            f"{len(short)} 个分支要点不足（{detail}，要求每支 {need} 个），已按现状交出。"
        )
    if retried:
        content_parts.append(f"其中 {len(retried)} 个分支要点不足，已各补调一次模型。")
    if cut_total:
        content_parts.append(
            f"整树节点数已到契约上限 {node_limit}，第 {min(level_cut)} 层起有 {cut_total} 个"
            "节点没有继续往下展开。"
        )
    if level_skipped:
        layers = "、".join(str(depth) for depth in sorted(level_skipped))
        content_parts.append(
            f"因模型调用额度不足，第 {layers} 层共 {sum(level_skipped.values())} 个节点没有展开，"
            "已按现状交出。"
        )
    if level_failed:
        layers = "、".join(str(depth) for depth in sorted(level_failed))
        content_parts.append(f"第 {layers} 层共 {sum(level_failed.values())} 个节点展开出错，已留空。")
    if shallow:
        layers = "、".join(str(depth) for depth in shallow)
        content_parts.append(f"第 {layers} 层没能展开：模型没有返回可用的下一层内容。")
    # 截断回收过节点：这件事必须出现在 content 里。diagnostics 只有前端面板会展开，
    # 而"这一批少了尾巴"直接影响用户对整棵树完整度的判断。
    if any(item.code == "OUTPUT_TRUNCATED_RECOVERED" for item in diagnostics):
        content_parts.append("其中一批模型输出撞上长度上限被截断，已保留回收到的完整节点。")
    # 两个计数都是 0 = 提示词里的字段一次都没被回应。这件事不能只留在 data 里：
    # 用户看到的是 content，排查的人第一眼看的也是它。
    if leaf_declared == 0 and expandable_declared == 0:
        content_parts.append(
            "模型未使用可展开标记（expandable），本次按缺省全部可展开处理。"
        )
    # 检索的事必须出现在 content 里：用户拿到的树有没有材料依据，全看这一步。
    # 只说"共 N 个节点"会让一棵"20 支里有 8 支没查到依据"的树与一棵全都查到
    # 证据的树看起来一模一样。
    if slicing:
        # 扩展生效时每支实际发 1-3 条问法，这时只说"各检索一次"就与
        # `retrieval.queries` 的长度对不上——而用户看到的正是这句话。
        line = (
            f"按支检索：{len(skeleton)} 条分支各检索一次，"
            + (f"其中 {len(expansions)} 条按标题扩展了同义问法"
               f"（共 {len(retrieval_queries)} 条问法，多路融合后每支取 "
               f"{DEEP_BRANCH_TOP_K} 条），" if expansions else "")
            + f"{with_evidence} 条拿到材料依据（每支 {DEEP_BRANCH_TOP_K} 条）"
        )
        if retrieval_mode == "whole":
            line += "；按支检索一条都没命中，已退回整树一次检索（全部支共用）"
        elif with_evidence == 0:
            line += "；一条都没命中，各支按自己的知识展开、不挂引用"
        else:
            line += (f"，{len(skeleton) - with_evidence} 条没有检索到依据，"
                     "按自己的知识展开、不挂引用")
        if supplementary_branches:
            line += (f"；其中 {len(supplementary_branches)} 条按子节点补查了一次"
                     f"（本次上限 {SUPPLEMENTARY_RUN_LIMIT} 次）")
        content_parts.append(line + "。")
        if refs_kept or refs_dropped:
            content_parts.append(
                f"引用：保留 {refs_kept} 条通过逐字校验，剔除 {refs_dropped} 条。"
            )
        # 证据给了、整棵树却一条引用都没留下：这与上面那句"一条都没命中"是两件事，
        # 而在交付的树上它们长得一样——都没有任何 `source_refs`。这是 `rag` 形态下
        # 唯一没有信号的失败面：检索成功、prompt 里有原文，模型就是没用。
        #
        # `before`（校验前模型声明的条数）与 `refs_kept`（最终留下几条）一起才
        # 说得清"是没写还是被剔光"：只说"0 条"会把两种原因压成一句空话。
        # 证据条数按 `(source_id, start, end)` 去重——`whole` 兜底时每支拿到的是
        # 同一批命中，逐支相加会把 5 条说成 20 条。
        if with_evidence and not refs_kept:
            evidence_total = len({
                (entry.get("source_id"), entry.get("start"), entry.get("end"))
                for branch in branch_evidence if branch is not None
                for entry in branch.payload
            })
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn, code="EVIDENCE_UNUSED",
                message=(
                    f"{with_evidence} 条分支拿到了材料依据（去重后 {evidence_total} 条证据"
                    "拼进了提示词），但整棵树最终 0 条引用"
                    + (f"（模型写了 {before} 条，全部没通过逐字校验）" if refs_dropped
                       else "（模型一条引用都没有写）")
                    + "；证据给了却没被用上，不等于材料里没有相关内容"
                ),
            ))
            content_parts.append(
                "按支检索给到的证据这次没有被用上：这不代表「材料里没有相关内容」，"
                "换一个更贴近原文的分支说法重试可能就好了。"
            )
    # 附加边必须出现在 content 里：用户选的是流程图，而"这张图上有几条条件分支、
    # 几条回退"正是他判断"画出来像不像他想要的流程"的第一个信号。只说节点数时，
    # 一棵有 6 条回退线的流程图与一棵纯思维导图在文字上长得一模一样。
    if diagram_type != "tree":
        if edges:
            branches_n = sum(1 for edge in edges if edge.kind == "branch")
            loops_n = sum(1 for edge in edges if edge.kind == "loop")
            content_parts.append(
                f"附加关系：{len(edges)} 条（条件分支 {branches_n} 条、回退 {loops_n} 条）。"
            )
        else:
            content_parts.append(
                "这次没有生成附加关系（条件分支 / 回退）：流程是一条直链，"
                "用节点展开补细节即可。"
            )
    content_parts.append(
        f"按 {max_depth} 层预算展开，实际 {audit.deepest + 1} 层（含中心主题），各层节点数："
        f"{' / '.join(str(audit.depths.get(depth, 0)) for depth in range(audit.deepest + 1))}。"
    )
    # 结构体检与语义复核的结论要出现在 content 里：`diagnostics` 只有前端诊断面板
    # 会展开（且只列前三条），而"某一支其实只长了一个子节点"直接影响用户对这棵树
    # 完整度的判断——那是要他去补展开的地方，不是一条可以折叠的告警。
    if single_child:
        content_parts.append(
            f"结构体检：{single_child} 个节点只有一个子节点，可能有一支没展开；"
            "可以对这些节点用 node_expand 继续展开，或与相邻节点合并。"
        )
    if nli_diagnostics:
        tail = (f"其中 {len(contradicted)} 条与原文语义相反，已逐条列出"
                if contradicted else "没有一条与原文语义相反")
        content_parts.append(
            f"语义复核：{len(nli_diagnostics)} 条引用的论断与引文做过蕴含判定，{tail}；"
            "该判定是概率评分，不改变任何引用的存在与否。"
        )

    readiness = _source_readiness_diagnostic(ctx)
    if readiness is not None:
        diagnostics.append(readiness)

    return ToolResult(
        ok=True,
        content="".join(content_parts),
        data={
            "root_id": content.root.id,
            "node_count": count,
            "revision": ctx.revision,
            "planned_branches": len(skeleton),
            "expanded_branches": expanded,
            "partial": partial,
            "depth_limit": max_depth,
            "node_limit": node_limit,
            # 这一次调用最多能要几个"满额节点"（`generation.budget` 反解出来的）。
            # 它夹住了每一次要求，所以"要求量装不进单次输出"在结构上不可能发生；
            # 报出来是为了让"为什么某层没继续长"能被解释成预算而不是模型的锅。
            "per_call_nodes": per_call_nodes,
            "levels": [
                {"depth": depth, "nodes": audit.depths.get(depth, 0)}
                for depth in range(audit.deepest + 1)
            ],
            "cut_nodes": cut_total,
            # 「模型到底有没有用上 expandable」的两个原始计数。缺省不计数：两个
            # 都是 0 说明这个字段被完全无视，深浅只剩层数上限在决定。
            "leaf_declared": leaf_declared,
            "expandable_declared": expandable_declared,
            "short_branches": [
                {"id": skeleton[index][1], "label": title, "missing": missing}
                for index, title, missing in short
            ],
            "branches": [
                {"id": branch_id, "label": title, "points": len(slots_by_branch[index])}
                for index, (title, branch_id) in enumerate(skeleton)
            ],
            # 检索这一步的可观测面：模型只能用一次工具调用的参数描述它做过什么，
            # 而真正发了几个 query、哪几支没拿到证据，只有这里说得清。
            # `max_tool_calls` 不受影响（这一整段都在工具内部）。
            "retrieval": {
                "mode": retrieval_mode,          # off / branch / whole
                "top_k": (DEEP_BRANCH_TOP_K if retrieval_mode != "whole"
                          else DEEP_FALLBACK_TOP_K),
                "queries": retrieval_queries,
                # 有几支拿到了扩展问法（`plan_branches` 的产出条数）。0 既可能是
                # "开关关着"，也可能是"开关开着但模型降级了"——这两者从
                # `queries` 的长度看不出来（8 支 24 条 vs 20 支 20 条都是"看着像
                # 没扩"）。日志里有 `outcome`，而模型看不到日志。
                "expanded_branches": len(expansions),
                "branches_with_hits": sum(
                    1 for item in branch_evidence if item is not None),
                "failed_branches": [skeleton[index][0] for index in retrieval_failed],
                "empty_branches": [skeleton[index][0] for index in retrieval_empty],
                "hits": sum(
                    len(item.payload) for item in branch_evidence if item is not None),
                # 限额补查：首路 query 是分支标题，与正文用词对不齐的那几支一条都
                # 召不回。`used` 与 `branches` 分开记——前者是成本（几次检索），
                # 后者是收益（救回几支），只报一个数看不出补查值不值。
                "supplementary": {
                    "limit_per_branch": SUPPLEMENTARY_BRANCH_LIMIT,
                    "limit": SUPPLEMENTARY_RUN_LIMIT,
                    "used": supplementary_used,
                    "branches": [skeleton[index][0] for index in supplementary_branches],
                    "queries": supplementary_queries,
                },
            },
            "source_refs": {
                "kept": refs_kept,
                "dropped": refs_dropped,
                "dropped_by_code": refs_by_code,
            },
            # 结构体检的结论与语义复核的成本。前者是"哪些节点该再展开"的机器读那
            # 一半；后者是**成本账**：`limit` + `judged` 一起才说得清"NLI 到底跑了几
            # 条、有没有被上限截断"，而那正是决定这道开关该不该默认开的唯一依据。
            "structure": {
                "single_child": single_child,
                "deepest": audit.deepest + 1,
                # 从 `check()` 里**留下来进了 diagnostics** 的条数（逐节点引用噪声
                # 已被 `_PER_NODE_REF_CODES` 挡掉），不是 `check()` 的原始产出量。
                "reported": len(health),
            },
            # 图型与附加边：`diagram_type` 是**输入**（用户选的），`edges.count`
            # 是**产出**。两个都报，因为"选了流程图但一条边都没画出来"与"选了
            # 思维导图"在树上长得一样，而前者是要去补的动作。
            #
            # `source` / `stripped` 只在**经过请求层解析**（`ctx.diagram_source`
            # 非空）时出现：它们回答的是"这次的图型是怎么定下来的"——请求层的下拉、
            # 从 `goal` 的文字里读出来的，还是模型自己填的；以及为了识别，从 `goal`
            # 里剥走了哪一段。直接构造 `RunContext` 的代码（单测、未来的其它入口）
            # 没有这两个值，那里保持**没有这两个键**的旧形状——`tests/test_edges.py`
            # 与 `test_agent_diagram_type.py` 用旧模块整份比对 `data`，多一个键就红。
            #
            # 出现条件因此是"非 tree **或** 解析过"：`tree` 且没解析时没有这个键，
            # 是加 `diagram_type` 之前就有的形状（同 `external` / `material` 那条规矩）。
            **({"diagram": {
                # 报**生效的**那一个（请求优先），不是 `args` 里那个：用户选了流程图、
                # 而模型没写参数时，`data` 里再写 tree 会让诊断面板与产出自相矛盾。
                "type": diagram_type,
                "source": source_of_diagram,
                "stripped": ctx.diagram_stripped,
                "edges": len(edges),
                "edge_kinds": {
                    kind: sum(1 for edge in edges if (edge.kind or "flow") == kind)
                    for kind in EDGE_KINDS
                },
            }} if diagram_type != "tree" or ctx.diagram_source is not None else {}),
            "nli": {
                "on_generate": nli_attempted,
                "limit": MAX_NLI_CHECKS,
                "judged": len(nli_diagnostics),
                "contradicted": len(contradicted),
            },
            "diagnostics": [item.model_dump() for item in diagnostics],
            **({"external": {
                # 只在外部检索真的走过时才有这个键：开关关着、或本来就有材料时，
                # `data` 的键集与加这个能力之前逐字节相同（回归用例比的是键集）。
                "hits": external.hits,
                "sources": len(external.sources),
                "seconds": round(external.elapsed, 3),
                "failed": external.failure,
                # 缓存命中的那一次 `seconds` 里没有 HTTP 的时间，光看它会把
                # "外部检索中位 4.5 秒"读成"有时 0.02 秒，性能不稳定"。
                "cached": external.cached,
            }} if external is not None else {}),
            **({"material": {
                # 材料明明传了、这次却没用：机器读的那一半（用户读的那一半在
                # `content` 与 `MATERIAL_TOO_SHORT` 里）。数字放这里而不是只写进
                # 一句话，是为了让"阈值调过之后还触发吗"能在日志里直接看出来。
                "chars": material_chars,
                "sources": len(short_material),
                "threshold": SHORT_MATERIAL_CHARS,
                "degraded": True,
            }} if short_material else {}),
        },
    )


def _depth_of(root: Node, node_id: str) -> int | None:
    """节点在第几层（root 为 1）。找不到返回 `None`。

    契约的 `MAX_TREE_DEPTH = 8` 是**含根**的层数，`_audit_tree` 里 root 是第 0 层
    ——两套计法在同一个项目里并存，所以这里把口径写死成"root = 1"并注明来源：
    调用方拿它与 `MAX_TREE_DEPTH` 直接比较，差一层就会把第 7 层的节点判成不能
    再加子节点（或者反过来，让树长到第 9 层再被整树校验打回）。
    """
    stack: list[tuple[Node, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if node.id == node_id:
            return depth
        stack.extend((child, depth + 1) for child in node.children)
    return None


def _iter_nodes(root: Node):
    """前序（父在子前）遍历整棵树。"""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _node_path(root: Node, node_id: str) -> list[Node] | None:
    """`根 → … → 目标节点` 的节点链；找不到返回 `None`（含目标不在树里）。

    单节点展开必须拿到它，而不是只看一个孤立的标题：一个 16 字的 `label`（"实施
    要点"、"风险"、"第三步"）几乎在任何主题下都成立，模型不知道自己在哪一层、
    上面那层讲的是什么，展开出来的东西就会飘到别的主题上去。标题链是这次调用
    唯一能拿到的层级上下文，而它就在树里，不需要额外检索或额外调用。

    用「找父链再反转」而不是「遍历时带路径栈」：深度上限只有 8，而 `_node_path`
    要被 `_expand` 与它的错误路径各调一次，写成两条路径（成功/失败）的遍历会让
    "找不到时返回什么"出现两种写法。
    """
    parents: dict[str, Node] = {}
    for node in _iter_nodes(root):
        for child in node.children:
            parents[child.id] = node
    target = find_node(root, node_id)
    if target is None:
        return None
    chain = [target]
    cursor = target
    while cursor.id in parents:
        cursor = parents[cursor.id]
        chain.append(cursor)
    chain.reverse()
    return chain


def _path_text(chain: Sequence[Node]) -> str:
    """标题链渲染成一行：给提示词看，也给日志/诊断看。"""
    return " > ".join(node.label.strip() for node in chain)


def _index_path(root: Node, node_id: str) -> list[int] | None:
    """目标节点从根出发的子索引链（`[]` = 根自己）；找不到返回 `None`。

    它只服务一件事：`node_grow` 帧里的 `path` 是**父节点**的索引链（见
    `NodeGrowEvent`）。展开时新节点的父节点就是被展开的那个节点，所以路径必须
    是它自己的位置——写成 `[]` 会把新节点画到根下面，界面上看起来像"根上又长出
    了三个分支"，而用户明明点的是第三层的某个叶子。
    """
    if root.id == node_id:
        return []
    stack: list[tuple[Node, list[int]]] = [(root, [])]
    while stack:
        node, path = stack.pop()
        for index, child in enumerate(node.children):
            if child.id == node_id:
                return [*path, index]
            stack.append((child, [*path, index]))
    return None


# ---------------------------------------------------------------- 同级去重
#
# 「不要重复已有子节点」写进了提示词，但它只是提示词。模型看不到自己上一轮写过
# 什么（单节点展开每次都是独立调用），而"同一句话挂在两个节点上"在界面上是看得
# 见的错。所以代码这一侧要有一条**确定性**的去重：
#
#   - 归一化后**相同** → 必丢。折叠空白、去掉标点与编号前缀、大小写不敏感；
#   - 归一化后**互相包含** → 也丢。这是"语义重复"里唯一能不看模型就判的一类
#     （「实施要点」vs「实施要点三条」）。再往上的同义改写（「风险」vs「潜在
#     隐患」）代码判不了，只能靠提示词：把它当确定性规则去做，会写出一个
#     "有时灵有时不灵"的东西，而它坏掉时的表现是丢内容。
#
# 去重只发生在**同一父节点的子节点之间**：不同分支下出现同样的短标题是正常的，
# 跨父节点去重会把"两支各有一个『风险』"这种合法结构删掉一个。

# 归一化时剔掉的字符：中英文标点、编号与括号。用 `str.translate` 一次过，
# 比一串 `replace` 快且不会有漏。
_PUNCT_TABLE = str.maketrans({
    char: None for char in
    "　 \t\r\n"
    "，。、；：？！“”‘’（）【】《》〈〉—…·"
    ",.;:?!\"'()[]{}<>-—…·/\\|+*_=~`^#@$%&"
})


def _fold_title(text: str) -> str:
    """标题的归一化形式：只用于比较，不用于显示。

    `casefold` 而不是 `lower`：同一句话里出现全角/半角字母时（模型偶尔这么写），
    只有 `casefold` 会给出同一个答案。
    """
    return (text or "").translate(_PUNCT_TABLE).casefold()


def _duplicate_of(label: str, others: Sequence[str]) -> str | None:
    """`label` 与 `others` 里的哪一个重复；不重复返回 `None`。

    返回"撞上了哪一个"而不是布尔：诊断里要能写出"与已有子节点「X」重复"，否则
    用户看到的是"这一批少了一个"，没人能判断少的是哪一个、为什么少。
    """
    folded = _fold_title(label)
    if not folded:
        return None
    for other in others:
        other_folded = _fold_title(other)
        if not other_folded:
            continue
        if folded == other_folded:
            return other
        # 互相包含：短的那个是长的那个的前缀式概括。长度差 1 以内的不做包含判定
        # （「甲一」与「甲二」互不包含，但「要点」与「要点二」会——后者该丢）。
        if len(other_folded) >= 2 and (folded in other_folded or other_folded in folded):
            return other
    return None


# ---------------------------------------------------------------- expandable 打标
#
# 「这个节点还能不能再展开」是**生成时**才知道的事（见 `mindmap.NODE_EXPANDABLE`），
# 建完树之后现算只能得到"有没有子节点"，而那件事回答不了"是到头了还是没展开完"。
# 所以打标紧跟在树定形之后、`ctx.candidate` 赋值之前，用生成过程里攒下的判断。


def _expandable_for_slot(slot: "_Slot") -> str:
    """一个候选节点落树时的 `expandable`。判定顺序就是优先级：

    1. 模型明确说"到底了" → `no`。这是唯一有第一手依据的结论。
    2. 被额度挡住的 → `unknown`。"没展开完"不等于"没得展开"。
    3. 其余 → `yes`。叶子默认就是 `yes`：它当然还能再拆，只是这次没拆。
    """
    if slot.expandable_declared and not slot.expandable:
        return "no"
    return "unknown" if slot.truncated else "yes"


def _stamp_expandable(
    content: DiagramContent, stamps: dict[str, str], *, max_depth: int,
) -> None:
    """把三态写进树里：`stamps` 里查得到就用它，查不到按 `yes`。

    两条覆盖规则**写在最后**，因为它们与"这一支生成到哪一步"无关，只与树的形状
    有关：

      - 深度到 `max_depth`（契约的 8 层）的节点一律 `no`：再加子节点整树就不合法，
        这不是模型的判断，是结构事实；
      - 已经有子节点的节点，若 `stamps` 说它 `no`，仍然照 `stamps`——模型可以说
        "这一层就是我要写的全部"，而那与"还能不能再加"是两个问题。

    就地改字段而不是 `model_copy`：`Node` 本来就带可变字段（`children` 在
    `apply_ops` 里也是就地插的），而这里改的是刚校验过的树、马上要赋值给
    `ctx.candidate`，多复制一次只会多一处"哪份是真的"。
    """
    for node, depth in shapes.walk_depths(content):
        node.expandable = "no" if depth >= max_depth else stamps.get(node.id, "yes")


def _stamp_origin(content: DiagramContent) -> None:
    """把"这些节点是生成链路写的"刷进整棵树。就地改，不 `model_copy`。

    与 `_stamp_expandable` 同一条位置规矩：**打标紧跟在树定形之后**，因为它是
    "这一次写入"的事实，越晚写越容易漏掉某条收尾路径（那条路径之后还有整树
    `model_validate`，写坏了会直接判 `invalid_tree`——那正是我们要的）。

    为什么**整棵**刷而不是在建节点的每一处各写一次：生成这条路里造节点的口子有
    五个（骨架、两级分支、分片 `emit` 的 add 草案、切片、单节点展开），逐个写等于
    给将来新加的那个口子留一个静默的 `unknown`——而 `unknown` 在界面上长得像
    "旧图"，用户会读到一句不该出现在新图上的话。收口在"这一次的树"上，一个口子
    都不用数。

    **没有 `origin` 参数**：这个函数只服务一件事（生成产出 → `generated`）。加一个
    参数就会有第二个取值，而"哪些节点算生成链路"是这里判的事，不该由调用方各自
    决定；编辑轮那是另一条路（`ops._apply_add`），不是这条的参数。
    """
    for node, _depth in shapes.walk_depths(content):
        node.origin = "generated"


def _walk_depths(root: Node):
    """`(节点, 层号)`，root 为 1。与 `_depth_of` 同一口径。"""
    stack: list[tuple[Node, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        yield node, depth
        stack.extend((child, depth + 1) for child in node.children)


class _ExpandInvalid(Exception):
    """单节点展开时某个子节点不合契约。

    `code` 与 `_fail` 的码表同源，`message` 会**原样**回给模型——它得知道是第几个
    子节点、错在哪，下一轮才改得对（与 `OpError.path` 同一条规矩）。异常只在
    `_expand` 内部传递：解析、去重、引用清洗全部发生在落树之前，所以任何一条抛出来
    都还没碰过树，「整批回退」是构造保证，不是回滚代码。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _ExpandItem:
    """一个解析好、但还没落树的子节点。

    `expandable` 是模型这次的声明（`None` = 没写）。它只决定落树时那个节点的三态
    取值（`yes` / `no`），不参与"要不要再问一次"——单节点展开只写一层，没有下一轮。
    """

    label: str
    detail: str | None
    expandable: bool | None
    source_refs: list[dict[str, Any]] = field(default_factory=list)


def _strict_label(raw: Any, index: int) -> str:
    """取子节点的 `label`：空白、非字符串、超过 16 码点一律判整批失败。

    **不裁剪**，与分片那条路（`_slot_from` 里的 `clip_label`）刻意不同。分片是一次
    已经花掉的调用吐出十来个节点，截断保住的是"这一次别白花"；单节点展开只产出
    2–4 个节点，而超长恰恰说明模型没按契约写（把一整句话塞进了标题）——截断之后
    画布上挂着一句从中间断开的话，用户既看不出它被截了，也判断不了这一批还能不能
    用。判失败则有明确出路：错误原样回给模型，它下一轮通常会写短。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise _ExpandInvalid(
            "invalid_label",
            f"第 {index + 1} 个子节点的 label 是空的或不是字符串，这一批没有落树。")
    label = " ".join(raw.split())
    length = len(label)
    if length > GENERATED_LABEL_MAX_CHARS:
        raise _ExpandInvalid(
            "label_too_long",
            f"第 {index + 1} 个子节点的 label 有 {length} 个字符，超过"
            f" {GENERATED_LABEL_MAX_CHARS} 个的上限（收到：「{label[:40]}」）。"
            "请压缩成一句话短语、把长内容放进 detail；这一批没有落树。")
    return label


def _strict_refs(raw: Any, index: int, known: set[str],
                 dropped: list[str] | None = None) -> list[dict[str, Any]]:
    """子节点上的 `source_refs`：**形状**错了判失败，**依据**不足就丢掉。

    两种错误分开处置，因为它们的含义不同：

      - 形状错（不是对象、没有 source_id、start/end 不成对或不是整数）说明模型
        没照格式写。留着它会在 `AddOp` 的契约校验里炸掉**整棵树**，而那条报错
        指不到"是第几个子节点"，所以在这里就判整批失败；
      - 依据不足（source_id 不在本次可见的来源里）不是格式问题，是"这条引用
        证不出来"。判失败会把一次内容本身没问题的展开整批废掉，代价方向错了；
        丢掉并记诊断才对——节点照常挂上，只是它没有出处。

    `known` 是本次**真的能核验**的来源 id 集合：请求带来的 + 从库里找回来的。
    不在这里收窄的话，一条指向本次没见过的 source_id 的引用会让
    `MindMapContent.validate_tree` 判 `tree_invalid`——报错文本是"节点 X 引用的
    来源 Y 不在 source_usage 中"，模型完全无处下手。

    `dropped` 是被丢掉那些 `source_id` 的收集器（可选）。丢掉本来不报错，但
    "少了几条引用"必须能被数出来：`data.refs.dropped` 与那条诊断都靠它对账，
    否则界面上只会看到"这一支没有出处"，而说不清是被剔的还是模型压根没写。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _ExpandInvalid(
            "invalid_refs",
            f"第 {index + 1} 个子节点的 source_refs 不是数组，这一批没有落树。")
    refs: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise _ExpandInvalid(
                "invalid_refs",
                f"第 {index + 1} 个子节点的 source_refs 里有非对象项，这一批没有落树。")
        source_id = entry.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            raise _ExpandInvalid(
                "invalid_refs",
                f"第 {index + 1} 个子节点的引用缺少 source_id，这一批没有落树。")
        start, end = entry.get("start"), entry.get("end")
        if start is not None or end is not None:
            bad = (isinstance(start, bool) or isinstance(end, bool)
                   or not isinstance(start, int) or not isinstance(end, int)
                   or end <= start)
            if bad:
                raise _ExpandInvalid(
                    "invalid_refs",
                    f"第 {index + 1} 个子节点引用的 start/end 不成对或不是整数，"
                    "这一批没有落树。")
        if source_id not in known:
            if dropped is not None:
                dropped.append(source_id)
            continue
        refs.append(entry)
    return refs


def _parse_expand_items(raw: Any, *, known: set[str],
                        dropped_refs: list[str] | None = None) -> list[_ExpandItem]:
    """单节点展开的响应 → 子节点列表，按出现顺序；不合契约就抛 `_ExpandInvalid`。

    两种形状都收，这是 `EXPAND_PROMPT` 明说的：
      - `{"children":[{"label":…,"detail":…,"source_refs":[…]}]}`：新格式；
      - `{"points":["…","…"]}`：旧格式，只有标题。

    **一处失败整批作废**：任何一项不合法就抛出去，调用方把整个响应扔掉。挂在
    一半的坏处比整批失败大得多——用户看到三个新节点、其中第二个的标题是半句话，
    而他没有任何办法知道"这批本来应该是四个"。

    与分片那条路（`_candidate_slots`）的差别不只是严格：这里**没有嵌套**。分片会
    收下模型多给的一层（`depth_left`），单节点展开只写一层，多给的那层没有下一轮
    追问去填它，收下只会得到"上面有子节点、子节点却没有下文"的假完整。
    """
    if not isinstance(raw, dict):
        raise _ExpandInvalid("invalid_points", "模型返回的不是 JSON 对象，这一批没有落树。")
    entries = raw.get("children")
    if not isinstance(entries, list):
        entries = raw.get("points")
    if not isinstance(entries, list):
        raise _ExpandInvalid(
            "invalid_points",
            "模型没有返回 children（或旧格式的 points）数组，这一批没有落树。")

    items: list[_ExpandItem] = []
    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            # 旧格式的裸字符串：只有标题，没有 detail，也没有声明可展开性。
            items.append(_ExpandItem(_strict_label(entry, index), None, None))
            continue
        if not isinstance(entry, dict):
            raise _ExpandInvalid(
                "invalid_points",
                f"第 {index + 1} 个子节点既不是字符串也不是对象，这一批没有落树。")
        flag = entry.get("expandable")
        items.append(_ExpandItem(
            label=_strict_label(entry.get("label"), index),
            detail=_detail_text(entry.get("detail")),
            expandable=flag if isinstance(flag, bool) else None,
            source_refs=_strict_refs(entry.get("source_refs"), index, known,
                                     dropped=dropped_refs),
        ))
    return items


def _verified_owned(refs: list[dict[str, Any]],
                    evidence: "_BranchEvidence | None") -> list[dict[str, Any]]:
    """只留下"属于本次命中"的引用；没有证据可依据时**一条都不收**。

    没有检索结果时模型写什么引用都只能是编的（它没看到任何原文）。这一层直接清空，
    而不是等逐字校验去剔——两者的界面表现一样，但前者能让"这一次是没有依据才空着"
    在 `data.refs` 里看得出来。

    依据按**坐标**比对、没有坐标才比文本，这套判据只该有一份实现（`_ref_from`）：
    在这里照抄一遍，等于给"引用归属"留下第二条会漂移的真相。
    """
    if evidence is None or not refs:
        return []
    return [ref for ref in (_ref_from(entry, evidence) for entry in refs) if ref]


def _expand_system(chain_text: str, evidence: "_BranchEvidence | None", *,
                   attempted: bool) -> str:
    """单节点展开的 system 消息：基础格式 + 这一支的上下文 + 证据规则。

    **路径也要进 system**，不能只放在 user 里：`EXPAND_PROMPT` 是一段通用格式
    说明，模型在写每一个子项时都会回头参考它；只在 user 里给一次路径，写到第三
    个子项时它就只剩下"补充子节点"这个任务本身了（实测这正是"越写越跑题"的
    起始点）。

    `attempted` 与 `evidence is None` 分开判：两者说的是不同的事——"这次查过但
    没命中"要让模型知道（否则它会把"我没拿到证据"读成"材料里大概还有"），而
    "这次根本没查"（请求没带材料、图上也没有出处）什么都不用说，多说一句反而
    会让它开始解释自己为什么没有引用。
    """
    parts = [EXPAND_PROMPT, f"这次展开的是路径「{chain_text}」的最后一个节点，只写它这一层。"]
    if evidence is not None:
        parts.append(BRANCH_EVIDENCE_RULES)
    elif attempted:
        parts.append(BRANCH_NO_EVIDENCE_RULES)
    return "\n".join(parts)


def _expand_ask(chain: Sequence[Node], wanted: int, *, existing: str,
                instruction: str | None) -> str:
    """单节点展开的 user 消息。

    **路径必须在里面**：`label` 只有 16 个字，一个孤立标题在几乎任何主题下都成立
    （"实施要点""风险""第三步"），模型不知道自己在展开哪一层，写出来的东西就会
    飘到别的主题上去。标题链是这次调用唯一拿得到的层级上下文，而它就在树里。

    `existing` 是**同级已有子节点**的标题。模型看不到自己上一轮写过什么（每次
    展开都是独立调用），不给它这张清单却要求"不要重复"，它根本没有办法执行——
    实测同一要点被返回两次，而数量验收还通过了（`_keep_new_slots` 的注释钉着
    同一件事）。
    """
    parts = [
        f"节点路径（根到这里）：{_path_text(chain)}",
        f"要展开的节点：{chain[-1].label.strip()}",
        f"它已有的子节点：{existing}",
        f"请补充 {wanted} 个新的子节点，只写这一层。",
    ]
    if instruction:
        parts.append(f"补充要求：{instruction}")
    return "\n".join(parts)


def _expand_sources(ctx: RunContext, node: Node) -> tuple[list[Source], dict[str, Any]]:
    """这次展开能用的来源：**先看请求带的，再从库里按 `source_refs` 找回来**。

    为什么需要这一步：正文只活在当次会话的内存里（`frontend/src/stores/sources.ts`），
    用户重新打开一张旧导图时前端手里没有正文，`request.sources` 是空的。而节点上
    存着 `source_refs`，里面的 `source_id` 是**持久 id**——库（`_rag` 早就接过同
    一条路）按它就能把原文找回来。不这么做，"重新打开旧图 → 展开某个节点"这条路上
    永远没有证据，模型只能凭标题编，节点也不会有出处。

    三种结果必须分开，因为模型该说的话不同：

      - 找到材料（`mode` 为 `request` / `library`）→ 正常的有证据展开；
      - 没找到、但这张图**声称**有出处（`mode == "missing"`）→ 降级成无证据展开。
        `content` 里必须说出来，否则用户会以为这些节点是从材料里整理出来的；
      - 这张图本来就没有出处（`mode == "none"`）→ 普通的"无材料展开"，一句话主题
        生成的图天天走这条，什么解释都不需要。

    目标节点自己没有 `source_refs` 时，把**整棵树**声称用过的来源一起找回来：
    证据是给"这一支"用的，而这一支的依据常常挂在它的父节点上（父节点写了出处，
    子节点只做细分）。这一步只读不写，找不到也只是 `missing` 里多一条。
    """
    request_sources = list(ctx.request.sources)
    if request_sources:
        return request_sources, {"mode": "request", "recovered": 0, "missing": []}

    wanted_ids: list[str] = []
    for ref in node.source_refs:
        if ref.source_id not in wanted_ids:
            wanted_ids.append(ref.source_id)
    if not wanted_ids and ctx.candidate is not None:
        for other in shapes.iter_nodes(ctx.candidate):
            for ref in other.source_refs:
                if ref.source_id not in wanted_ids:
                    wanted_ids.append(ref.source_id)

    loaded: list[Source] = []
    missing: list[str] = []
    for source_id in wanted_ids:
        if source_id in ctx.sources_by_id:
            loaded.append(ctx.sources_by_id[source_id])
            continue
        try:
            source = knowledge_library.load(source_id)
        except Exception as exc:  # noqa: BLE001 — 库坏了按"没找到"降级，理由见 docstring
            logger.warning("展开时回查库来源 %s 失败（%s）：%s",
                           source_id[:8], type(exc).__name__, str(exc)[:160])
            source = None
        # 判据与 `_verify_sources` 逐条对齐：校验器不认的来源，找回来也只会让
        # 引用在收尾时被静默剔掉（那是整条链上最隐蔽的一种失败）。
        if source is None or source.status != "parsed" or not (source.text or "").strip():
            missing.append(source_id)
            continue
        loaded.append(source)
    # 记进 ctx 的库来源缓存：`_verify_sources` 与 `_citable_hits` 都按它查原文。
    ctx.record_library_sources([item for item in loaded
                                if item.id not in ctx.sources_by_id])
    if loaded:
        return loaded, {"mode": "library", "recovered": len(loaded), "missing": missing}
    return [], {"mode": "missing" if wanted_ids else "none",
                "recovered": 0, "missing": missing}


def _expand_query(chain: Sequence[Node]) -> str:
    """单节点展开的检索问句：**目标节点标题 + 父路径最后一级**。

    为什么不只发标题：展开的目标常常是一个很短的词（"风险""分工""第三步"），
    短查询在 BM25 里被高频词吃掉、在向量路里也召回得泛——它会把整份材料里所有
    带"风险"二字的段落都拉进来。加上父级那一层（"测试方案 风险"）之后，查询带上
    了这一支的语义范围。

    为什么也不发整条链：链路越长权重越平均，最后等于把根主题又查了一遍——那正是
    `_branch_query` 的消融实验里最差的一组（8 支共用 top-1，覆盖率 3.8%）。
    目标就是根时没有"上一级"，只发根标题。
    """
    labels = [node.label.strip() for node in chain if node.label.strip()]
    if not labels:
        return ""
    if len(labels) >= 2:
        return f"{labels[-1]} {labels[-2]}"
    return labels[-1]


def _expand_reference(ctx: RunContext, chain: Sequence[Node], sources: list[Source],
                      ) -> tuple["_BranchEvidence | None", dict[str, Any]]:
    """为这次展开单独检索一次：**只查选中节点**，不重跑整支。

    `top_k` 用单支口径（`DEEP_BRANCH_TOP_K = 3` 是**下界**，实际条数由
    `DEEP_BRANCH_KEEP` 按分数定，3–6 条）：一条父块 450–700 字，三条已经够一次
    2–4 个节点的展开；多出来的那几条是同分的补充，不是"同一个开头换个说法"
    （实测第 4–10 条 94% 落在不同父块，见 `DEEP_BRANCH_KEEP` 的注释）。

    **不占 `max_tool_calls`**：检索发生在工具内部（与 `_generate_deep` 的按支检索
    同一条规矩），模型看到的仍然只有一次 `node_expand` 调用。这不只是计费口径：
    用户点的是"展开这一支"，他并不知道自己在花工具额度；而额度的每一格都是
    **一次模型往返**，让服务端自己做的检索去占一格，等于让用户为一次内部操作
    多等 5–20 秒。

    用 `search_parent` 而不是 `search_sources`：它同时给出父块区间与命中的子块区间，
    引用才判得出"落在这段命中的原文里吗"（`_ref_from` 与
    `verify_source_refs(evidence=)` 都靠这两级坐标）。
    """
    query = _expand_query(chain)
    if not query:
        return None, {"query": "", "hits": 0, "skipped": "空查询"}
    hits = search_parent(sources, query, DEEP_BRANCH_TOP_K,
                         library=library_search_enabled(),
                         keep=branch_keep(),
                         drop_external=library_drop_external())
    if not hits:
        return None, {"query": query, "hits": 0, "top_k": DEEP_BRANCH_TOP_K}
    # 库命中的持久 id 要先取回 `Source`：`_citable_hits` 没有正文就给不出
    # `evidence_quote`，收尾校验也查不到原文（两条理由都写在 `_remember_library_hits`）。
    library_used = _remember_library_hits(hits, ctx)
    # 命中区间进本次运行的证据集：`verify_source_refs(evidence=)` 按它判
    # "这条引用落在可见的父块里吗"。少记一条，模型照它抄的引用就会被判成编的。
    ctx.record_evidence([
        EvidenceSpan(hit.source_id, hit.start, hit.end, hit.leaf_start, hit.leaf_end)
        for hit in hits
    ])
    evidence = _branch_evidence(hits, sources + list(ctx.library_sources.values()))
    return evidence, {
        "query": query,
        "hits": len(hits),
        "top_k": DEEP_BRANCH_TOP_K,
        "library_sources": library_used,
    }


def _verify_expand_refs(nodes: list[Node], sources: list[Source],
                        evidence: "_BranchEvidence | None") -> int:
    """对新子节点的引用跑一遍逐字校验，返回被剔除的条数。

    引用准确率不能靠提示词保证：模型的 `quote` 是"照着抄"的，抄错一个字在界面上
    看不出来（引用摘录本来就是一段文字），而它一旦落库，用户点开抽屉对着原文就
    发现对不上。

    校验跑在**探针树**上（一个只装这批新节点的临时 `MindMapContent`），而不是整棵
    树：整树校验会把**其他节点**的旧引用一起重判，而它们可能来自本机库里没有的
    材料——那会因为"这次没带那份材料"把用户图里早就有的出处删掉，而用户点的是
    "展开这一支"。"只改选中的那一支"这条约束在这里同样成立。

    探针树里的 `Node` 与随后要落树的是同一批**对象**（pydantic 校验不复制实例），
    所以 `remove_invalid=True` 剔掉的引用直接反映在它们身上，不需要再写回一遍。
    """
    if not nodes or not sources:
        return 0
    usage = [
        SourceUsage(source_id=source.id, kind=source.kind, filename=source.filename,
                    char_count=len((source.text or "").strip()))
        for source in sources
    ]
    probe = MindMapContent(
        title="展开校验",
        root=Node(id="__expand_probe__", label="展开校验", children=nodes),
        source_usage=usage,
    )
    before = sum(len(node.source_refs) for node in nodes)
    verify_source_refs(probe, sources, remove_invalid=True,
                       evidence=evidence.spans if evidence is not None else None)
    after = sum(len(node.source_refs) for node in nodes)
    return before - after


async def _expand(args: ExpandArgs, ctx: RunContext) -> ToolResult:
    content = _require_candidate(ctx)
    if isinstance(content, ToolResult):
        return content

    node = shapes.find(content, args.node_id)
    if node is None:
        return _fail("node_not_found", f"节点 {args.node_id} 不存在。", path=f"node_id={args.node_id}")
    # 展开只能作用于用户选中的那个节点。参数里虽然有 node_id，但它是模型填的——
    # 不校验的话，用户选中 A 而模型去改 B 也会“成功”，而他根本没同意改 B。
    if ctx.request.selected_ids and args.node_id not in ctx.request.selected_ids:
        allowed = "、".join(ctx.request.selected_ids)
        return _fail("not_selected", f"只能展开当前选中的节点（{allowed}），不能改 {args.node_id}。")
    if len(node.children) + args.count > MAX_CHILDREN:
        return _fail(
            "no_room",
            f"节点「{node.label}」已有 {len(node.children)} 个子节点，上限 {MAX_CHILDREN}，放不下 {args.count} 个。",
        )

    # 扩展守的是**整图**的契约上限（1000 节点 / 8 层），不是初次生成的那条 80/4。
    # 初次紧、后续松：用户明确点了这个节点要更深，那就按承载能力给。两道判断都放在
    # 调用模型**之前**——额度一旦花掉就收不回来，而这两种情况模型再听话也改不了。
    node_limit = _node_limit()
    used = shapes.count_nodes(content)
    room = node_limit - used
    wanted = min(args.count, room)
    if wanted < 2:
        return _fail(
            "tree_full",
            f"这张图已经有 {used} 个节点，接近上限 {node_limit}，放不下这一批新节点"
            f"（剩余 {max(0, room)} 个）。请先删掉一些节点再展开。",
            node_count=used, node_limit=node_limit,
        )
    depth = shapes.depth_of(content, node.id)
    if depth is not None and depth >= mindmap_contract.MAX_TREE_DEPTH:
        return _fail(
            "too_deep",
            f"节点「{node.label}」已经在第 {depth} 层，导图最多 "
            f"{mindmap_contract.MAX_TREE_DEPTH} 层，不能再往下加了。",
            depth=depth, depth_limit=mindmap_contract.MAX_TREE_DEPTH,
        )

    # ---- 上下文：标题链 + 材料 ----
    #
    # 路径从树里取（不花任何调用），材料可能要从库里找回来（理由见
    # `_expand_sources`）。两者都在调模型之前准备好：提示词里少一样，模型就得
    # 凭一个 16 字的孤立标题写 2–4 个节点。
    chain = shapes.path_to(content, node.id) or [node]
    path_text = _path_text(chain)
    sources, material = _expand_sources(ctx, node)
    evidence: _BranchEvidence | None = None
    retrieval: dict[str, Any] = {"query": "", "hits": 0, "skipped": "没有可检索的材料"}
    if sources:
        evidence, retrieval = _expand_reference(ctx, chain, sources)

    existing_titles = "、".join(child.label for child in node.children) or "（空）"
    ask = _expand_ask(chain, wanted, existing=existing_titles, instruction=args.instruction)
    system = _expand_system(path_text, evidence, attempted=bool(sources))
    if evidence is not None:
        # 证据拼在 user 末尾（与 `_generate_deep` 的 `evidence_suffix` 同形）：
        # 引用只能从这段里抄，而"那段是哪一段"要挨着问题看。
        ask += f"\n本次对「{node.label}」单独检索到的 evidence 数组：\n{evidence.text}"

    try:
        if not ctx.reserve_model_call():
            return _fail("budget_exhausted", "本次运行的模型调用额度已用完，无法展开。")
        ctx.extra_model_calls += 1
        raw = await _chat(ctx, system, ask, max_tokens=_max_tokens(ctx))
    except GenerationError as exc:
        return _fail("generation_failed", f"展开失败：{exc}")

    diagnostics: list[Diagnostic] = []
    truncated = _note_truncation(diagnostics, raw)
    # 只有本次真的能核验的来源才允许出现在引用里（理由见 `_strict_refs`）。
    known = {source.id for source in sources} | set(ctx.library_sources)
    unknown_source_refs: list[str] = []
    try:
        items = _parse_expand_items(raw, known=known, dropped_refs=unknown_source_refs)
    except _ExpandInvalid as exc:
        return _fail(exc.code, exc.message, path="children")
    if not items:
        return _fail("invalid_points", "模型没有返回可用的子要点，请重试。")

    # ---- 同级去重：已有的 + 本批内部 ----
    taken = [child.label for child in node.children]
    kept: list[_ExpandItem] = []
    duplicates: list[dict[str, str]] = []
    for item in items:
        clash = _duplicate_of(item.label, taken)
        if clash is not None:
            duplicates.append({"label": item.label, "existing": clash})
            continue
        taken.append(item.label)
        kept.append(item)
    if not kept:
        return _fail(
            "duplicate_points",
            f"模型返回的 {len(items)} 个子节点都与「{node.label}」已有的子节点重复"
            f"（{existing_titles}），这一批没有落树。请换一个角度或补一句 instruction 再试。",
            duplicates=duplicates,
        )
    # 多给的不算错：要 3 个给了 4 个，收前 3 个即可（`count` 是用户能选的量）。
    # 少给也不算错：模型判断这一支只写得出一两个要点是它的判断，不是数据不合法——
    # 判失败会把一次内容本身没问题的展开整批废掉。只有"一个都不剩"（全被去重掉）
    # 才失败，那时确实没有任何东西可挂。
    kept = kept[:wanted]

    # ---- 引用：先按命中归属筛，再逐字核验 ----
    refs_dropped = len(unknown_source_refs)
    new_nodes: list[Node] = []
    for index, item in enumerate(kept):
        owned = _verified_owned(item.source_refs, evidence)
        refs_dropped += len(item.source_refs) - len(owned)
        new_nodes.append(Node(
            id=f"{TEMP_ID_PREFIX}e{index}",
            label=item.label,
            detail=item.detail,
            source_refs=owned,
            # 这一批是**生成链路**写的（检索 → 提示词 → 逐字核验都在上面走过了），
            # 所以是 `generated`。**只标这一批**：树上原有的节点各自保留自己的
            # `origin`——一次展开不该把用户手加的节点改写成"模型写的"，那会让
            # "没找到出处，可以补材料"套到一批本来就没有原文的节点上。
            origin="generated",
        ))
    refs_dropped += _verify_expand_refs(new_nodes, sources, evidence)
    refs_kept = sum(len(child.source_refs) for child in new_nodes)
    if refs_dropped:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="SOURCE_REFS_DROPPED",
            message=(
                f"{refs_dropped} 条引用没有通过核验（来源不在本次可核验的范围里，"
                f"或引文与原文对不上），已剔除；保留 {refs_kept} 条"
            ),
        ))

    drafts = [{
        "op": "add",
        "id": child.id,
        "parent_id": node.id,
        # 索引用**已接受的数量**而不是原始下标：本批被去重掉几条之后，用原始下标
        # 会往父节点中段插，整批 index 会与"追加到末尾"的语义不符。
        "index": len(node.children) + offset,
        "label": child.label,
        # `detail` 与 label 一起落树：单节点展开同样是"短标题 + 长说明"，
        # 少带这一项会让这一支的节点在画布上重新变回一句长话。
        "detail": child.detail,
        "source_refs": [ref.model_dump() for ref in child.source_refs],
    } for offset, child in enumerate(new_nodes)]

    # `source_usage` 必须在 apply 之前补齐：契约要求每条 `source_ref.source_id`
    # 都出现在里面，缺一条整棵树就是 `tree_invalid`。旧图上展开时才会走到（新加的
    # 引用来自库里找回来的来源，而那张图的 `source_usage` 可能只列了当时那几份）。
    used_ids = {ref.source_id for child in new_nodes for ref in child.source_refs}
    usage_ids = {item.source_id for item in content.source_usage}
    missing_usage = [source for source in sources
                     if source.id in used_ids and source.id not in usage_ids]
    base = content
    if missing_usage:
        base = content.model_copy(deep=True)
        base.source_usage = [
            *base.source_usage,
            *[SourceUsage(source_id=source.id, kind=source.kind,
                          filename=source.filename,
                          char_count=len((source.text or "").strip()))
              for source in missing_usage],
        ]

    ops, id_map = normalize_ops(drafts, shapes.node_ids(content))
    batch = OpBatch(batch_id=_batch_id(ctx), base_revision=ctx.revision, ops=ops)
    try:
        updated = _apply_ops_any(base, batch)
    except OpError as exc:
        return _fail(exc.code, exc.message, path=exc.path)

    # ---- expandable：这一批新节点 + 被展开的节点自己 ----
    #
    # 新节点：模型说"到底了"就是 `no`，其余是 `yes`（它是叶子，当然还能再拆）。
    # 被展开的节点：它现在有子节点了，但"有子节点"只说明长出了一层——除非它已经
    # 顶到单节点的上限，否则仍是 `yes`。
    stamps: dict[str, str] = {}
    for offset, item in enumerate(kept):
        raw_id = drafts[offset]["id"]
        stamps[id_map.get(raw_id, raw_id)] = "no" if item.expandable is False else "yes"
    expanded_node = shapes.find(updated, node.id)
    stamps[node.id] = "no" if (expanded_node is not None
                              and len(expanded_node.children) >= MAX_CHILDREN) else "yes"
    _stamp_expandable(updated, stamps, max_depth=mindmap_contract.MAX_TREE_DEPTH)

    # 报给界面的必须是**树上真实的取值**，不是上面那张期望表：深度覆盖规则（第 8 层
    # 一律 `no`）就发生在 `_stamp_expandable` 里，直接回期望表会给出一个"按钮可点、
    # 点下去却报 too_deep"的分叉。
    expandable: dict[str, str] = {}
    for node_id in stamps:
        found = shapes.find(updated, node_id)
        if found is not None:
            expandable[node_id] = found.expandable

    ctx.publish_tree(updated)
    ctx.batches.append(batch)

    # 检查点：与 `_commit_drafts` 同一个理由。位置在这两行之后而不是更晚——后面
    # 还要发一串 `node_grow` 预览帧，再来一次模型调用写说明；那几十秒里进程没了，
    # 这一支新长的节点本可以留下却会整批消失。`updated` 已是最终图型，直接交。
    if ctx.checkpoint is not None:
        await ctx.checkpoint(updated)

    # ---- 生长预览：落在**被展开的那个节点**下面 ----
    #
    # `path` 是父节点的子索引链，而这里的父节点就是被展开的节点本身。写成 `[]`
    # 会把新节点画到根下面，界面上看起来像"根上又长出三个分支"，而用户点的是
    # 第三层的某个叶子。算不出来就不发帧（宁可没有预览，也不给一个错位置）。
    index_path = shapes.index_path(content, node.id)
    if index_path is not None:
        seen: set[tuple[tuple[int, ...] | None, str]] = set()
        for item in kept:
            await _emit_grow(ctx, seen, tuple(index_path), item.label, item.expandable)

    summary = f"已在「{node.label}」下新增 {len(ops)} 个子节点（路径：{path_text}）。"
    if duplicates:
        summary += f"其中 {len(duplicates)} 个与已有子节点重复，已跳过。"
    if truncated:
        summary += "模型输出被长度上限截断，已保留回收到的完整子节点。"
    if wanted < args.count:
        summary += f"（整图节点接近上限 {node_limit}，这次只补 {wanted} 个）"
    if material["mode"] == "missing":
        # 降级必须说出来：这句话既是给模型看的（下一轮别声称有出处），也是给用户
        # 看的（界面上这行字来自工具结果）。找不到原文不等于没有原文，所以措辞是
        # "这次没找到"，不是"这份图没有依据"。
        summary += ("这次没找到原始材料（节点的 source_refs 里的来源在知识库里已不存在），"
                    "本次是无证据展开：新节点没有写 source_refs。")
    elif sources and not retrieval.get("hits"):
        summary += "这次没有在材料里找到与这一支相关的原文，新节点没有写 source_refs。"
    elif evidence is not None and refs_kept == 0:
        # 有命中但一条都没留下：模型没写引用，或写了但过不了逐字校验。这两种在
        # 界面上是同一件事（节点没有出处），区别留在 `data.refs` 里。
        summary += "模型这次没有写出可核验的引用，新节点没有 source_refs。"

    # 证据拼进了 prompt（`evidence` 非 None = 这一支真的检索到原文并附在 user 末尾）、
    # 新节点却一条引用都没有：这与"没检索到"是两件事，而交付出去的结果一样。
    # 判据用 `evidence` 而不是 `sources`：请求带了材料、这次一条都没命中时，`sources`
    # 非空但 `evidence` 为 None——那种情况由上一个 elif 的"没找到原文"解释。
    if evidence is not None and refs_kept == 0:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="EVIDENCE_UNUSED",
            message=(
                f"这次为「{node.label}」检索到 {len(evidence.payload)} 条原文并拼进了"
                f"提示词，但新增的 {len(new_nodes)} 个子节点最终 0 条 source_refs"
                + ("（模型写出的引用全部没通过核验）" if refs_dropped
                   else "（模型一条引用都没有写）")
                + "；证据给了却没被用上，不等于材料里没有相关内容"
            ),
        ))

    return ToolResult(
        ok=True,
        content=summary,
        data={
            "batch": batch,
            "batches": [batch],
            "id_map": id_map,
            "revision": ctx.revision,
            "labels": [item.label for item in kept],
            "partial": truncated,
            "truncated": truncated,
            "node_limit": node_limit,
            "node_count": shapes.count_nodes(updated),
            "depth": depth,
            "path": path_text,
            # 展开之后每个新节点被判成哪一态：界面据此决定按钮的文字（`no` 就是
            # "已经到底了"）。同时把"被展开的节点自己"也带上——它现在有子节点了。
            "expandable": expandable,
            "duplicates": duplicates,
            "refs": {"kept": refs_kept, "dropped": refs_dropped},
            "materials": material,
            "retrieval": retrieval,
            "diagnostics": [item.model_dump() for item in diagnostics],
        },
    )


async def _fill_details_tool(args: FillDetailsArgs, ctx: RunContext) -> ToolResult:
    """给已经长好、但说明还空着的树补 `detail`。**只写说明，不动结构**。

    它存在的原因是"检查点"那条路：生成在写完说明之前被打断时，库里留下的是
    **合法完整、`detail` 全空**的树（`runs.checkpoint` 的 docstring）。用户把它
    载回画布之后，唯一的缺口就是每个节点的说明——补它不需要重新生成一遍结构，
    而那正是从前唯一的选择（重跑一次 = 再花十几秒与三次模型调用，还可能换出
    一棵不一样的树）。

    复用 `_fill_details`：切半、五条兜底、按 id 对号、诊断这五件事在那里已经
    调好了（`docs/ONE-CALL.md` 的实测数字也是按它量的）。这里只补三件它不知道
    的事——**主题从哪来、原文从哪来、以及这不是生成**。

      - **不给 `node_ids`**：要补哪些由 `_detail_targets` 按长度判（见
        `FillDetailsArgs` 的 docstring）。
      - **原文按 `source_refs` 回查**（`_expand_sources`）：旧图重新打开时
        `request.sources` 是空的，正文只活在当次会话的内存里。用 `_intake`
        不行——它会在"没有材料"时**发起外部检索**，那等于给一棵按旧依据长出来
        的树塞一份新依据，写出的说明与结构对不上，还白等 6 秒。`_expand_sources`
        只做"找回这条链上声称用过的那些来源"，找不到就降级成无证据补写。
      - **没有原文也必须能补**：`_fill_details` 在 `sections` 为空时走
        `DETAIL_NO_SOURCE_RULES`，与生成时"一句话主题"那条路完全一样。

    返回的 `content` 是给模型看的下一步（"补了几个、还缺几个"），`data` 里是
    `_fill_details` 的原账与诊断——诊断由调用方（`service._loop`）转成告警。
    """
    current = _require_candidate(ctx)
    if isinstance(current, ToolResult):
        return current
    if not isinstance(current, MindMapContent):
        # `_fill_details` 的签名只接树（它遍历 `content.root`）。鱼骨与流程的说明
        # 在折叠那一步就已经跟着节点走了，真要补得另写一套遍历——在那之前给一句
        # 说得清的话，好过让模型看到一个它一调就崩的工具。
        return _fail(
            "not_tree",
            "只有思维导图能单独补说明；鱼骨图与流程图的说明在生成时就跟着节点写好了，"
            "现在改不了。",
        )

    diagnostics: list[Diagnostic] = []
    sources, material = _expand_sources(ctx, current.root)
    titles = [branch.label for branch in shapes.top_branches(current)]
    sections: list[str] = []
    evidence_total = 0
    if sources and titles:
        expansions = await _expand_branches(sources, titles)
        hits_by_branch = await _retrieve_branches(sources, titles, expansions=expansions)
        for title, hits in zip(titles, hits_by_branch):
            evidence = _branch_evidence(hits, sources) if hits else None
            if evidence is None:
                continue
            # 与生成那条路同一件事：命中要并进本次运行的证据集，否则模型照它抄的
            # 引用在收尾校验时会被判成编的（`verify_source_refs` 只认见过的父块）。
            ctx.record_evidence(evidence.spans)
            sections.append(
                EVIDENCE_SECTION_HEADER.format(index=len(sections) + 1, title=title)
                + "\n" + evidence.text)
            evidence_total += len(evidence.payload)
    external_rules = (
        EXTERNAL_TRUST_RULES
        if sections and any(source.kind in (SourceKind.web, SourceKind.paper)
                            for source in sources)
        else "")

    # 主题优先取树上那个标题：它是这次结构的主题，而 `goal` 是"补齐说明"那句操作
    # 指令（前端点按钮时发的固定文案），拿它当主题等于让模型给一棵"关于补齐说明"
    # 的图写说明。`goal` 只在前者为空时兜底。
    topic = (current.title or "").strip() or ctx.request.goal
    filled, report = await _fill_details(
        current, ctx=ctx, topic=topic, sections=sections,
        external_rules=external_rules, diagnostics=diagnostics,
        instruction=args.instruction,
        # 续写这条路的材料是**按引用找回来的那些来源**（`_expand_sources`），
        # 口径同样是它们的正文字数。找不到来源时是 0 → 退回常量区间。
        material_chars=sum(len(source.text or "") for source in sources))
    requested = int(report.get("requested") or 0)
    applied = int(report.get("filled") or 0)
    skipped = report.get("skipped")
    # 说明变了就是内容变了：`publish_tree` 会同时置 `produced`、推进 `revision`，
    # 收尾的 `DoneEvent` 才带得回这份新树（`_deliver_or_fail` 判的正是 `produced`）。
    # 一个都没补上时不发这一枪：那棵树与进来时逐字节相同，"产出过东西"若为真，
    # 之后某条额度耗尽的路径就会把"什么都没发生"包装成一棵半成品交付。
    if applied:
        ctx.publish_tree(filled)
        # 与 `_commit_drafts` / `_expand` 同一档：说明已经写进树了，就该落一份检查点。
        # 这里尤其该落——补说明本身实测二十几秒（还要再等一次模型往返收尾），这段
        # 窗口里进程没了，补好的说明全丢，库里退回"结构完整、说明为空"那一版，
        # 恰好是这次要消灭的状态。写失败不影响这一轮（`on_checkpoint` 自己吞异常）。
        if ctx.checkpoint is not None:
            await ctx.checkpoint(filled)
    if skipped == "nothing_to_fill":
        summary = "这棵树的节点说明都不缺，没有需要补写的地方（没有调用模型）。"
    elif skipped == "budget":
        summary = (f"模型调用额度已用完，{requested} 个节点的说明一个都没能补上；"
                   "结构没动，可以下一轮再试。")
    elif applied:
        summary = f"已给 {applied} 个节点补写了说明（共 {requested} 个待补）。"
        if int(report.get("missing") or 0):
            summary += f"还有 {report['missing']} 个没写回来，说明保持为空。"
    else:
        summary = (f"补写说明的模型调用没有返回可用的说明（{requested} 个节点），"
                   "这次没有改动任何内容。")
    if sources and not evidence_total:
        summary += "这次没在原始材料里找到相关原文，说明是凭主题写的，没有新增引用。"
    elif material.get("mode") == "missing":
        summary += "原始材料在知识库里已经找不到了，这次是无证据补写。"

    return ToolResult(
        ok=True,
        content=summary,
        data={
            "detail": report,
            "changes": int(applied),
            "materials": material,
            "diagnostics": [item.model_dump() for item in diagnostics],
        },
    )


def _rag_filters(raw: dict[str, Any] | None) -> SourceFilter | None:
    """把模型给的过滤条件解析成 `SourceFilter`；坏输入一律当成"没给"。

    过滤条件本该由外层给（用户标注 / 界面筛选），模型没有可靠依据判断"这批材料
    的作者是谁、哪段时间的"——让它编只会凭空缩小检索范围，把本该找到的证据筛掉。
    所以这里**不做任何推断**，只负责透传与校验。

    校验失败就放弃过滤，不向上抛：降级成"不过滤"永远好过让整次 `rag_query` 失败。
    空条件也折成 None，省掉一次无意义的过滤分支。

    区分"参数错误"与"没给"这一层只在这里做：`SourceFilter` 自己仍然严格校验
    （直接调用 `search_sources` 的调用方该被拦住），宽松只发生在模型这一侧。
    """
    if not raw:
        return None
    try:
        filters = SourceFilter.model_validate(raw)
    except ValidationError as exc:
        logger.debug("忽略 rag_query 的过滤条件（%s）：%s",
                     type(exc).__name__, str(exc)[:160])
        return None
    # 全部字段都是默认值 = 调用方什么都没要求，等价于没有 filters
    if not filters.model_dump(exclude_defaults=True):
        return None
    return filters


def _material_inputs(sources: list[Source]) -> list[str]:
    """给 `plan_query` 的材料摘要：每份「标题 + 正文开头」压平成一段。

    **正文进 prompt 有三条路，这条是其中最窄的一条**：
      - `_skeleton_outline` 拼进骨架的标题行（原文的子集，一条最多 40 字）；
      - **全塞**（`FULL_TEXT_MATERIAL_CHARS`）：材料合计装得下时整份正文进结构那次
        与说明那两次调用（`_full_text_sections`）；
      - 这一处：每份标题 + 前 200 字，只在 `MINDMAP_QUERY_EXPANSION` 打开时才走
        （默认关）。

    它的边界很小且是刻意的——只在前 200 字、只在这个开关打开时。planner 判断"该用
    哪些词、范围有多大"本来就需要看一眼材料长什么样，只给标题它只能靠猜。

    骨架那一次还有**第四条**线索：`_material_inventory` 的材料清单（份数 / 每份文件名
    与字数）。它比上面三条都窄——**不含正文**，只有结构——所以它不属于"正文进 prompt"
    这条路，但它是同一个问题的答案："让骨架知道这次的材料长什么样"。四条一起看才完整。

    所以"正文不出进程"这句话**不成立**，准确的说法是：检索片段（`rag_query` 的返回、
    按支检索的命中）、全塞时的整份正文、这里的 200 字（默认关）、以及骨架那一次的
    **标题行**会进 prompt；embedding 请求会离开进程但不进 prompt（按 `sha256(quote)`
    缓存）。共同点只剩下一条：**没有一处会把正文发进 prompt 而不告诉模型它是正文**
    ——全塞那一路的提示词就是这么写的（`FULL_TEXT_EVIDENCE_RULES`）。

    截断与份数上限全部交给 `_material_snippets`：两处各写一份"前 200 字、最多
    10 份"，迟早出现"提示词看到的是 A、检索依据是 B"这种没人查得出来的差集。
    它已经是幂等的，`plan_query` 内部再跑一次不会二次截断。
    """
    return _material_snippets(
        [f"{source.filename or ''}\n{source.text or ''}" for source in sources])


# 回给模型的 `quote` 在父块返回模式下是 450–700 字的父块，而 `SourceRef.quote`
# 的上限是 300 字（见 `contracts/source.py`）。模型只能截一段抄，而截完就没了
# 可抄的偏移——自己算的偏移过不了逐字校验。于是父块模式把"带偏移的引用"这条路
# 整个堵死，`EvidenceSpan` 也就永远没有输入，那半条防护等于没接。
#
# 所以这里把**命中的子块**单独给出一组 `evidence_*`，模型抄它就是一条既带偏移、
# 又落在证据上的引用；从父块里挑别的句子则要么对不上偏移、要么被判越界。
# 只加在回给模型的 `content` 上，不改 `data["hits"]`：事件流与既有的"命中形状"
# 断言都按 `EvidenceHit` 的字段走，多两个键会让它们变成两处真相。
_EVIDENCE_QUOTE_MAX_CHARS = 300


def _citable_hits(hits: list[EvidenceHit], sources: list[Source]) -> list[dict[str, Any]]:
    """把命中翻成回给模型的结构：单级模式下与原来逐字段相同，父块模式多一组证据。

    两种形状由 `MINDMAP_PARENT_RETRIEVAL` 决定，**四条检索入口一致**：
    `search_sources` 恒为单级；`search_parent` 与 `search_multi` 跟着开关走
    （关着时分别转发/退化成单级块），所以这里不需要知道这次是单路还是多路。

    子块现在**恒不超过** `LEAF_MAX_CHARS = 300`（`knowledge.service` 把它压到与
    这里同一口径，末块合并也一样压），所以在父块模式下**每一条带叶子区间的命中
    都会给出** `evidence_*`。这条不变量是引用证据链的前提，不是巧合：一旦某个
    子块超过 300，那条命中就只能不给 `evidence_*`（截断后的 quote 与偏移对不上，
    逐字校验必挂），模型随即退回"只抄 quote、不写偏移"——而
    `source_refs._evidence_problem` 对没有区间的引用不施加约束，于是"引的这句在
    命中的那段里吗"对这批命中整个失效。缝原来只有二十几个字，但那正是这条防护
    要挡的那件事。

    下面的 `<= _EVIDENCE_QUOTE_MAX_CHARS` 判断因此是**防御性**的：它守的是
    "参数被改回去"和"外部构造的 `EvidenceHit`"（比如库里存过旧参数切出的块），
    不是正常路径。真触发时行为仍然是"宁可没有偏移、也不给一段对不上的 quote"。

    `sources` 必须包含**库里来的那些来源**（`_rag` 把 `ctx.library_sources` 拼进来
    再调这里）。库命中的 `source_id` 是持久 id，只传 `request.sources` 时
    `texts.get(...)` 会拿到空串，于是"偏移有了、引文是空的"——模型照着抄就是一条
    `quote == ""` 的引用，校验时被判 `SOURCE_QUOTE_MISMATCH` 剔掉，而它自己完全
    看不出为什么。所以**取不到原文就一个 `evidence_*` 都不给**（判 `in texts`）：
    只给偏移不给引文，比两者都不给更糟。
    """
    texts = {source.id: source.text or "" for source in sources}
    payload: list[dict[str, Any]] = []
    for hit in hits:
        entry = hit.model_dump()
        if (hit.leaf_start is not None and hit.leaf_end is not None
                and hit.source_id in texts):
            quote = texts[hit.source_id][hit.leaf_start:hit.leaf_end]
            if quote and len(quote) <= _EVIDENCE_QUOTE_MAX_CHARS:
                entry["evidence_start"] = hit.leaf_start
                entry["evidence_end"] = hit.leaf_end
                entry["evidence_quote"] = quote
        payload.append(entry)
    return payload


def _verify_sources(ctx: RunContext) -> list[Source]:
    """收尾校验该认的来源：**本次请求带来的 + 本次真检索到过的库来源**。

    这是整条知识库接线里最容易漏、也最难发现的一处。`verify_source_refs` 用
    `by_id = {source.id: source}` 查原文，而库命中的 `source_id` 是持久 id：
    只传 `request.sources` 时，一条**逐字完全正确**的库引用会被判成
    `SOURCE_TEXT_UNAVAILABLE`（"未提供已解析的来源原文"），随后在
    `remove_invalid=True` 的那一步被静默删掉。用户看到的是"这一支没有证据"，
    而检索明明成功、引用明明写对了——没有任何一处日志会说清这件事。

    顺序固定为"请求的在前、库的在后"：`validate_generated_content` 用这个列表
    重建 `source_usage`，而界面上"本次材料"与"库里引用到的材料"是两种来源，
    先列请求内那批，展示顺序才与用户的心智一致。

    不去改 `verify_source_refs` 的签名（`optimization/service.py` 那条只读体检
    也在用它，它与知识库无关）：把来源拼宽是**调用方**的选择，不是校验器的
    语义。"能查到 → 就点得到"这条老规则也没有被打破——库来源是持久 id，
    `library.load()` 一定回查得到，这正是允许它进 `by_id` 的前提。

    **两处口径必须与校验器逐条一致，否则会出现"相机收进来、校验又拒绝"的静默
    剔除**：这里的判据是 `_reference_problem` 的
    `source is None or source.status != "parsed" or not text.strip()`
    去掉其中"来源本身不存在"那一半（查不到就不在这里），即
    `status == "parsed"` **且** 正文非空白。少一个条件就是一类静默失败：
    今天 `parse_bytes` 的返回一律 `status="parsed"`，所以漏掉 `status` 不会触发；
    但真引入 `status="partial"`（比如 PDF 部分页失败但有正文）那天，这个函数会
    把一份校验器拒绝的来源收进来，用户看到引用无缘无故消失，却找不到任何一处
    说清原因。半份材料不该被当成"已解析"，这条判断的权威在校验器那边。

    正文非空白这条也不是洁癖：契约要求 `source_usage` 的 `char_count >= 1`——把
    一份还没解析成功的空来源带进去，整次生成会被判 `invalid_tree`，用户只是多勾了
    一个空文件却得到"模型返回的导图不合法"。生成入口本来就有同一道过滤
    （`_generate_deep` 只喂真有正文的来源），两处共用这一个函数之后口径不会分叉。
    """
    return [
        source for source in
        (*ctx.request.sources, *ctx.library_sources.values())
        # `status` 那一条与 `_reference_problem` 对齐，别按"反正现在都是 parsed"
        # 省略掉：口径分叉的代价是一类没有日志的静默剔除（见 docstring）。
        if source.status == "parsed" and (source.text or "").strip()
    ]


def _source_readiness_diagnostic(ctx: RunContext) -> Diagnostic | None:
    """本次带了材料、但有材料进不了校验时，说清"为什么这张图没有引用"。

    为什么需要这条：`_verify_sources` 的判断是"`status == "parsed"` 且正文非空白"，
    不满足的来源会被**静默剔除**——不抛异常、不记日志，引用于是在 `remove_invalid`
    那一步消失。而契约里 `Source.status` 的默认值是 `"pending"`
    （`app/contracts/source.py`），所以任何没显式标成 `parsed` 的调用方都会看到
    "材料明明在请求里、引用一条都没有"，且找不到任何解释。

    前端自己构造来源时会显式写 `status: "parsed"`，今天不会踩；但这是"没有日志的
    失败"，与仓库那条"异常必须上报"的规矩冲突——把它变成界面上看得见的一句话，
    代价只是一条 warn 诊断。

    只在**请求自带的材料**上有话说：库来源由 `_remember_library_hits` 自己负责，
    它们没进来时不该归咎于用户勾了哪份文件。
    """
    requested = list(ctx.request.sources)
    if not requested:
        return None
    usable = {id(source) for source in _verify_sources(ctx)}
    dropped = [source for source in requested if id(source) not in usable]
    if not dropped:
        return None

    pending = sum(1 for source in dropped if source.status != "parsed")
    empty = sum(1 for source in dropped if source.status == "parsed")
    reasons = []
    if pending:
        reasons.append(f"{pending} 份未标记为已解析")
    if empty:
        reasons.append(f"{empty} 份正文为空")
    reason_text = "、".join(reasons) or "状态不可用"

    if len(dropped) == len(requested):
        message = (
            f"本次带进来的 {len(requested)} 份材料都没有参与校验（{reason_text}），"
            "因此这张图不会有任何引用——它是根据主题生成的，不是从这些材料里整理出来的。"
        )
    else:
        message = (
            f"本次带进来的 {len(requested)} 份材料里有 {len(dropped)} 份没有参与校验"
            f"（{reason_text}），引用只可能来自其余 {len(requested) - len(dropped)} 份。"
        )
    return Diagnostic(
        level=DiagnosticLevel.warn, code="SOURCE_NOT_PARSED", message=message,
    )


def _remember_library_hits(hits: list[EvidenceHit], ctx: RunContext) -> int:
    """把本次命中里**不属于本次请求**的来源取回并记进 `RunContext`。

    返回真正取到的份数（`data.library_sources` 用它，事件流里能看出"这一批命中
    有多少来自库"）。

    `load` 取不到（来源被 `prune` 删了、库文件换了、id 被手工清理过）时记 warning
    并跳过，**不让整次运行失败**：检索已经成功，用户拿到的是一批真实片段；把
    "收尾时发现来源没了"升级成一次运行失败，等于用一个边缘情况废掉整张图。跳过
    之后那条命中的引用会在校验时被判 `SOURCE_TEXT_UNAVAILABLE` 剔掉——这是可接受
    的降级，且 warning 已经在日志里说清了原因。
    """
    loaded: list[Source] = []
    seen: set[str] = set()
    for hit in hits:
        source_id = hit.source_id
        if source_id in seen or source_id in ctx.sources_by_id \
                or source_id in ctx.library_sources:
            continue
        seen.add(source_id)
        try:
            source = knowledge_library.load(source_id)
        except Exception as exc:  # noqa: BLE001 — 查不到就跳过，理由见 docstring
            logger.warning("回查库来源 %s 失败（%s）：%s",
                           source_id[:8], type(exc).__name__, str(exc)[:160])
            continue
        if source is None:
            logger.warning("库命中引用的来源 %s 已不存在（可能被 prune 删除），"
                           "跳过：这条来源上的引用会在收尾校验时被剔除",
                           source_id[:8])
            continue
        loaded.append(source)
    ctx.record_library_sources(loaded)
    return len(loaded)


async def _rag(args: RagArgs, ctx: RunContext) -> ToolResult:
    sources = _pick_sources(ctx, [])
    # 一次调用只读一次开关：这里要问它四遍（早退、库大小、检索入口、data 的键集），
    # 四次各读一次环境变量的话，"检索时看的是开、记账时看的是关"这种半开状态在
    # 理论上就可能拼出来——一次判定、多处使用是同一条规矩。
    library_on = library_search_enabled()
    # 开关关着时**一行都不往下走**：与 `07a2740` 逐字节相同的保证就在这里，
    # 包括"无来源直接 no_sources"这个早退（关掉库之后再走一遍检索是白花）。
    if not sources and not library_on:
        return _fail("no_sources", "这次请求没有附带任何资料，无法检索。")

    filters = _rag_filters(args.filters)
    # 只在开关打开时问一次库的大小：这既是"库空且请求无材料"那条判定的依据，
    # 也是空结果话术要用的数字。开关关着时它恒为 0，且一次库查询都不发生。
    library_total = library_source_count(drop_external=library_drop_external()) if library_on else 0

    # 查询计划默认关（`MINDMAP_QUERY_EXPANSION`）。关着时 `plan_query` 在返回
    # `None` 之前不发任何请求，这里的 `if` 必须让那条路**一个额外调用都没有**：
    # 开关关着与没有这个功能，召回结果必须逐字段一致。
    #
    # 只有 `source == "llm"`（模型真的产出了 intents/hyde/subqueries）才走多路。
    # `"fallback"` 计划里只有一个 corrected，它是**替换**原问句的东西，不该被
    # 当成"多一路"——那会把召回从"一个问题的答案"变成"两个问题的并集"。
    plan = await plan_query(args.question, materials=_material_inputs(sources))
    if plan is not None and plan.source == "llm":
        # 原问题**永远**第一位：`corrected` 的四道闸仍可能放过同句式换中心词的
        # 改写，只要原句在场，召回就不会整体跑偏。去重由 `search_multi` 负责。
        queries = [args.question]
        if plan.corrected:
            queries.append(plan.corrected)
        queries.extend(plan.intents)
        queries.extend(plan.subqueries)
        hits = await search_multi(sources, queries, args.top_k, filters=filters,
                                  hyde=plan.hyde, library=library_on,
                                  keep=rag_keep(),
                                  drop_external=library_drop_external())
    else:
        # 单路走 `search_parent`：**不在这里再判一次开关**。关着时它内部直接返回
        # `search_sources(...)` 的结果（同一个函数，逐字段相同由
        # `tests/test_parent_retrieval.py` 对着 `f72c14c` 钉着），两处各判一次只会
        # 多出"回了父块却没记叶子区间"这类半开状态。多路那条分支的粒度由同一个
        # 开关决定（见 `search_multi` 的 docstring），所以两条分支返回同一种证据
        # 形状——这是这一轮修掉的那个入口差异（父块级匹配比子块级低 5.7pp）。
        #
        # `library=` 是知识库那条唯一的入口：它让这次检索同时看"请求带来的"与
        # "库里存着的"两批来源，融合规则（本次命中保底占半数席位）在
        # `knowledge.service._merge_batches` 里，不在这一层。
        hits = search_parent(sources, args.question, args.top_k, filters=filters,
                             library=library_on,
                             keep=rag_keep(),
                             drop_external=library_drop_external())
    if not hits:
        # 四种"什么都没有"必须给四句不同的话——模型据此决定下一步做什么，而它
        # 看不到任何日志与指标。
        #
        # 1. 本次没带材料、库里也是空的：这不是"没命中"，是"无处可查"。判失败
        #    （`no_sources`）而不是 ok+空列表：模型该做的是告诉用户先传材料，
        #    而不是换个问法反复试。
        if not sources and not library_total:
            return _fail(
                "no_sources",
                "这次请求没有附带任何资料，知识库也是空的，没有可检索的材料。",
            )
        # 2. 过滤把**本次的**材料筛光了：材料还在，只是条件太严。给可执行的动作。
        if filters is not None and sources and not filter_sources(sources, filters):
            return ToolResult(
                ok=True,
                content=(
                    f"过滤条件筛掉了全部 {len(sources)} 份资料，没有可检索的内容。"
                    "可以放宽或去掉过滤条件后重试。"
                ),
                data={"hits": [], "method": "lexical", "filtered_out": len(sources)},
            )
        # 3. 本次没带材料、库里有东西但没匹配上：这是"库里没有"，不是"材料里
        #    没有"。换问法还有机会，先传相关材料也还有机会，两条都写出来。
        if not sources:
            return ToolResult(
                ok=True,
                content=(
                    f"知识库里现有的 {library_total} 份材料都没有匹配到这个问题"
                    "（这次请求也没有附带资料）。可以换个问法再检索，"
                    "或者先上传与这个问题相关的材料。"
                ),
                data={"hits": [], "method": "lexical",
                      "library_sources": library_total},
            )
        # 4. 本次带了材料、查不到：与加知识库之前**逐字相同**的那句话。
        return ToolResult(
            ok=True,
            content="没有在本次资料里找到相关片段。",
            data={"hits": [], "method": "lexical"},
        )

    # 画像加权：**只重排这批命中**，不增不减（见 `_profile_rank`）。放在空结果
    # 判定之后、证据累积之前——空结果那几种早退不该为一次排序付代价。
    #
    # 方向只从 **`sources`**（这张图的材料）算，一次运行一次（`_run_directions`）。
    # 这是"按图隔离"的唯一入口：另一个池的方向根本不在这个列表里，所以图 B 的
    # 命中不可能被图 A 的方向顶上来。
    hits, lifted, profile_note = await _profile_rank(
        hits, _run_directions(ctx, sources))

    # 库里来的命中带持久 id：取回 `Source` 并记进 ctx，收尾的引用校验才查得到
    # 原文（`_verify_sources`）。失败只跳过这一份，不影响本次检索结果。
    library_used = _remember_library_hits(hits, ctx)

    # 命中区间要在**这次运行内**累积：模型可能先问一次、看完再问第二次，引用
    # 来自任何一次都算数，而校验发生在生成收尾时、只剩一个 `ctx` 可读。
    # 转换写在 agent 层（一行 `EvidenceSpan`），不在 `source_refs` 里做：那一层是
    # 后处理，不该为了两个整数去 import 60KB 的检索模块（见 `EvidenceSpan` 的
    # docstring）。`_rag` 的几种空结果路径都到不了这里，所以证据集里不会混进
    # "没找到"的空批次。
    ctx.record_evidence([
        EvidenceSpan(hit.source_id, hit.start, hit.end, hit.leaf_start, hit.leaf_end)
        for hit in hits
    ])

    # 回给模型的是结构化 JSON：它要能拿到 source_id 与偏移，才能在引用里指准。
    # `method` 由每条命中的实际取值汇总，不再写死 lexical——混合检索生效时写死
    # 会让模型以为这批片段只做了关键词匹配。只要有任意一条走了向量路，本轮就是
    # hybrid（那条路贡献了候选，排序已被它影响）。
    #
    # 把 `ctx.library_sources` 拼进 `_citable_hits` 的来源表：库命中的正文不在
    # `sources` 里，不拼的话回给模型的 `evidence_quote` 会是空串（理由见该函数）。
    # 用整个缓存而不是"这一批的库来源"：它只是一份 id → 正文的查找表，多几项
    # 不影响任何输出；`hits` 之外的东西不会出现在返回值里。
    citable_sources = sources + list(ctx.library_sources.values())
    hits_data = [hit.model_dump() for hit in hits]
    data: dict[str, Any] = {
        "hits": hits_data,
        "method": "hybrid" if any(hit.method == "hybrid" for hit in hits) else "lexical",
    }
    if library_on:
        # 这批命中里有多少来自知识库（不在本次请求里的持久 id）。检索成功但界面上
        # 只有"命中 N 条"时，看不出证据是从哪来的——而这个数字正是"库到底有没有
        # 被用上"的唯一可观测信号。
        #
        # **开关关着时不加这个键**：那一侧返回给模型的 `content` 与 `data` 的键集
        # 必须与 `07a2740` 逐字节相同（`tests/test_library_wiring.py` 拿旧模块
        # 钉着），多一个恒为 0 的键就已经不是"逐字节"了。
        data["library_sources"] = library_used
    if lifted:
        # 哪几条命中是**因为画像**才进了前几名。只在真的抬上来过时才加这个键，
        # 理由同上：画像默认关着，关着时的键集一个字都不该变。
        #
        # 记号是 `source_id:start`，不是引文——原文不进日志与事件流。这个键的用途
        # 只有一个：事后回答"这些被画像抬上来的命中，后来有几个节点是用户删掉的"
        # （过滤气泡检验，见 `scripts/measure_profile_ab.py`）。
        data["profile_lifted"] = lifted
    if profile_note:
        # **"这次为什么没做个性化"。** 只在开关开着时才可能出现（开关关着时上面
        # 那句 `_profile_rank` 直接早退，`profile_note` 恒为空串），所以键集不会
        # 因为这一行多出来。
        #
        # 没有它，"个性化没生效"的唯一线索是"感觉好像差不多"，而三条完全不同的
        # 原因——这张图还没有块向量 / 画像开关关着 / α 为 0——在界面上长得一模一样。
        data["profile"] = {"applied": False, "note": profile_note}
    elif lifted:
        data["profile"] = {"applied": True, "lifted": len(lifted)}
    return ToolResult(
        ok=True,
        content=json.dumps(_citable_hits(hits, citable_sources), ensure_ascii=False),
        data=data,
    )


# ---------------------------------------------------------------- 整图审计


# 三个列表各自最多回几条。超出的只给 `more` 数字：一棵 300 节点的树里空壳可能有
# 几十条，整份塞进 tool 消息是把上下文吃了去换一堆模型用不上的 id；而一条都不给
# 又让"还差几处"无法定位。20 与 `outline_text` 的 200 行是同一个口径——够模型
# 认得出是哪几支，装不满一次响应的预算。
AUDIT_LIST_LIMIT = 20


def _audit_walk(content: DiagramContent) -> tuple[int, int]:
    """整图节点数与实际层数（每棵子树的根计 1 层）。

    显式栈而不是递归：树是模型给的，不假设深度上限成立（与 `ops.find_node`
    同一条理由）。三套结构共用这一份——`shapes.walk_depths` 按每棵并列子树各自
    从 1 起算，鱼骨图与流程图的"层数"因此仍是画布上那一列的深度。
    """
    total = 0
    deepest = 1
    for _node, depth in shapes.walk_depths(content):
        total += 1
        deepest = max(deepest, depth)
    return total, deepest


def _audit_branch_lists(content: DiagramContent) -> tuple[list[dict[str, Any]],
                                                          list[dict[str, Any]],
                                                          list[dict[str, Any]]]:
    """`missing` / `thin` / `empty` 三个列表（口径见 `_audit_tree` 的 docstring）。

    纯函数、只读：它不碰 `ctx.candidate`，所以用例可以直接拿一棵树钉住判定。
    """
    # "一级"按 `shapes.top_branches` 算：树是一级分支，鱼骨是六根骨，流程是各阶段。
    # 这三样才是"从来没被展开过"的主语。
    branches = shapes.top_branches(content)
    missing = [{"id": branch.id, "label": branch.label}
               for branch in branches if not branch.children]
    thin = [{"id": branch.id, "label": branch.label, "children": len(branch.children)}
            for branch in branches if len(branch.children) == 1]
    empty: list[dict[str, Any]] = []
    # 深度口径与 `_audit_walk` 一致：树的 root 是第 1 层、一级分支第 2 层，所以
    # 一级分支的子节点从第 3 层起；鱼骨与流程没有中心主题那一层，"最外那一层"
    # 就是第 1 层，它的子节点从第 2 层起。
    base_depth = 3 if isinstance(content, MindMapContent) else 2
    stack = [(child, base_depth) for branch in branches for child in branch.children]
    while stack:
        node, depth = stack.pop()
        if not node.children:
            empty.append({"id": node.id, "label": node.label, "depth": depth})
        stack.extend((child, depth + 1) for child in node.children)
    # 按深度升序：先补浅的（浅层空壳影响整图可读性，补它的收益最大），
    # 同深度保持发现顺序——不排序的话，"先补哪一处"由栈的实现细节决定。
    empty.sort(key=lambda item: item["depth"])
    return missing, thin, empty


def _audit_tree_tool(args: AuditTreeArgs, ctx: RunContext) -> ToolResult:
    """整图结构体检：**只数事实，不做判断**。

    **函数名带 `_tool` 后缀不是风格，是必须的**：`_audit_tree` 这个名字已经被
    `_generate_deep` 收尾那道「整树形状审计」（`_audit_tree(root, expected)` →
    `_TreeAudit`）占着。同名覆盖的后果不是报错而是**静默**——后者被替换成这里的
    实现，`_generate_deep` 会拿一棵树和一个 `set` 调它，直接 `AttributeError`。
    这一条是靠 `tests/test_agent_tools.py` 全红才发现的，所以名字不能再合并。

    为什么需要它，而不是让模型看大纲：`service.outline_text` 只列前 200 个节点，
    8 支 × 3 层就可能超；被截断之后模型看不出"最后两支下面什么都没有"，而它
    **不知道自己没看到**——这正是"模型以为自己写完了"最典型的形状。这个工具的
    每一个数都来自 `ctx.candidate`，与模型记得什么无关。

    三个列表都是可数的，没有一条是判断：

      - `missing`：一级分支里 `children` 为空的 —— "这一支从来没展开过"；
      - `thin`：一级分支只有一个子节点的 —— 展开过但只写了一条，是"这一支其实
        没写满"最便宜的证据（结构体检里 `SINGLE_CHILD` 数的是同一类事）；
      - `empty`：**非**一级分支、且没有子节点的节点 —— 树上真实存在的空壳。
        "非一级分支"这条限制是有意的：一级分支没有子节点已经由 `missing` 说了，
        两个列表说同一件事会让"还差几处"变成要先相加才读得懂。

    它读 `ctx.candidate` 而**不写**它：`revision` 与树指纹一个字节都不变，所以
    连续调它不会把 `NO_PROGRESS` 计数清零，也不会被记成一次改动。这也是它必须
    `readonly=True` 的理由——同参数的重复调用直接命中缓存，不再算一遍。

    返回的 `content` 是写给模型的行动指令（"缺的先补、不空就收尾"），`data` 是给
    前端与排查的原始计数。两者形状不同是有意的：模型要的是下一步做什么，人要看
    的是到底缺几处。
    """
    content = ctx.candidate
    if content is None:
        return _fail("no_candidate", "当前还没有导图，没有可体检的东西。")
    total, deepest = _audit_walk(content)
    missing, thin, empty = _audit_branch_lists(content)
    branches = len(shapes.top_branches(content))
    # 还能再展开的节点数（`expandable != "no"`）。树的**中心主题不算**：它永远是
    # `unknown` 或 `yes`，把它算进来会让"8 支都到底了"显示成 1 处待展开。鱼骨与
    # 流程没有中心主题那一层，六根骨与各阶段本身**要算**——它们就是最外层内容。
    pending = sum(
        1 for node in shapes.iter_nodes(content)
        if node.expandable != "no"
        and not (isinstance(content, MindMapContent) and node.id == content.root.id)
    )

    expanded = branches - len(missing)
    parts = [
        f"整图体检：{total} 个节点、{deepest} 层、一级分支 {branches} 条，"
        f"其中已展开 {expanded} 条。"
    ]
    if missing:
        names = "、".join(f"「{item['label']}」({item['id']})"
                         for item in missing[:AUDIT_LIST_LIMIT])
        # 前面已经说了总条数，这里不再重复一遍"等 N 条"——那句话读起来像
        # "还另外有 N 条"，而它数的是同一批。只说"列不全"就够了。
        tail = (f"（这里只列前 {AUDIT_LIST_LIMIT} 条）"
                if len(missing) > AUDIT_LIST_LIMIT else "")
        parts.append(f"还有 {len(missing)} 条一级分支没有任何子节点：{names}{tail}；"
                     "先补这些，再收尾。")
    if thin:
        names = "、".join(f"「{item['label']}」({item['id']})"
                         for item in thin[:AUDIT_LIST_LIMIT])
        parts.append(f"{len(thin)} 条一级分支只有 1 个子节点：{names}；"
                     "如果这一支本该更丰满，可以再补。")
    if empty:
        sample = "、".join(f"「{item['label']}」" for item in empty[:AUDIT_LIST_LIMIT])
        parts.append(f"树上有 {len(empty)} 个叶子节点（如 {sample}），"
                     "它们是终点还是没写完，由你判断。")
    if not missing and not thin:
        parts.append("没有发现没展开的一级分支，可以收尾。")

    data = {
        "branches": branches,
        "expanded": expanded,
        "depth": deepest,
        "nodes": total,
        "pending": pending,
        "missing": missing[:AUDIT_LIST_LIMIT],
        "missing_count": len(missing),
        "thin": thin[:AUDIT_LIST_LIMIT],
        "thin_count": len(thin),
        "empty": empty[:AUDIT_LIST_LIMIT],
        "empty_count": len(empty),
    }
    # 记到 ctx 上：模型读不读得到是它的事，但"它查出来还缺几支"要留在代码手里，
    # 收尾时（`service._loop`）才有依据提醒它一次。写在返回之前而不是之后——
    # 这条路径没有 await，顺序不影响正确性，但"先记账再回答"是这一层的一贯顺序。
    ctx.audit_missing = len(missing)
    return ToolResult(ok=True, content="".join(parts), data=data)


def _selection_scope(content: DiagramContent, ctx: RunContext) -> set[str] | None:
    """用户这次允许改的节点集合；没限制时返回 `None`。

    单独抽出来是因为五个工具都要问同一个问题（批量与四个细粒度）。写成
    「每个工具自己遍历一遍 selected_ids」，两处口径迟早分叉——而分叉的表现是
    "批量能改、细粒度不能改"这种看起来像 bug 的行为。
    """
    if not ctx.request.selected_ids:
        return None
    scope: set[str] = set()
    for selected in ctx.request.selected_ids:
        node = shapes.find(content, selected)
        if node is not None:
            scope |= _node_ids(node)
    return scope


def _outside_selection(
    content: DiagramContent, ctx: RunContext, targets: Sequence[str],
) -> list[str]:
    """这些目标里有哪几个落在用户选区**之外**。**只用来提醒，一律不拦。**

    为什么不再是硬拦截（`out_of_selection` 那个失败码连同它的两道判定一起删了）：
    选区描述的是"用户此刻圈着哪几块"，它是一句上下文，不是一道墙。三种真实场景
    都会被硬拦截挡死：

      1. 用户说「整张图都加上时间维度」，手上却还留着某个节点的选区——他要的是全图；
      2. 用户选了三个不相邻节点说「把这一支整体压缩」——真正要动的是它们的
         **公共祖先**，而那个祖先不在选区里；
      3. 老前端把上一版的 `selected_ids` 带过来，那些 id 在当前树里查不到，
         静态算出的范围是空集，于是**每一条**改动都被判成越界。

    代价也不像当初担心的那样大：改动只落这一轮的工作树（`ctx.candidate`），`done`
    那一刻才整树落画布（候选层删于 `acfdb5b`，中间没有人工复核点了），画布上还有
    撤销。这一层真正需要的是"让用户与模型都看见这次动到了选区之外"，而不是"拒绝执行"。

    返回的顺序与去重都保留（同一批里同一个 id 出现两次是常见形态：先 update 再
    move 它），提示里重复列同一个节点只会让那句话变长而没有新信息。
    """
    scope = _selection_scope(content, ctx)
    if scope is None:
        return []
    outside: list[str] = []
    for target in targets:
        if target in scope or target in outside:
            continue
        outside.append(target)
    return outside


def _scope_note(
    content: DiagramContent, ctx: RunContext, targets: Sequence[str],
) -> tuple[str, list[str]]:
    """`(追加到回给模型那句话末尾的提示, 越界的 id 列表)`；没越界时是 `("", [])`。

    抽成一个函数是因为**七个会越界的写工具共用同一条口径**（批量 + 六个细粒度）。
    写成"每个工具自己拼一句"，改一处忘一处时分叉的表现是"批量会提醒、细粒度不会"，
    那看起来像 bug 而不像设计。（`mindmap_fill_details` 不在这里：它按整棵树扫目标，
    没有"越界"这件事。）

    提示里要求模型"说明原因"，与 system 里 `service.SELECTION_RULE` 那句一一对应：
    提示词让它优先改选区、越界要交代；工具这里是它真的越界之后的**回声**，
    两句话说的是同一件事，模型不容易忽略。
    """
    outside = _outside_selection(content, ctx, targets)
    if not outside:
        return "", []
    allowed = "、".join(ctx.request.selected_ids)
    note = (f"\n（提示：{'、'.join(outside)} 不在用户选中的节点范围内（{allowed}），"
            "改动照样生效。若这是有意的扩权，请在回复正文里向用户说明原因；"
            "若是误伤，请自己改回去。）")
    return note, outside


_OP_NODE_FIELDS = ("id", "parent_id", "new_parent_id", "source", "target")


def _op_target_ids(batch: OpBatch) -> list[str]:
    """一批 op 里提到的**所有**节点 id（含临时 id，交给调用方去筛）。

    宁可多收不可漏收：漏掉一个 `parent_id` 就可能把"跨容器"的批当成"单容器"，
    而那个错误的后果是节点被搬进另一根骨——一种事后没人能察觉的静默结构错位。
    """
    seen: list[str] = []
    for op in batch.ops:
        for name in _OP_NODE_FIELDS:
            value = getattr(op, name, None)
            if isinstance(value, str) and value and value not in seen:
                seen.append(value)
    return seen


def _holders_of(
    content: DiagramContent, ids: Sequence[str],
) -> list[tuple[int, str]]:
    """这些 id 分别落在哪棵并列子树里：`[(子树序号, 子树根的 id)]`，去重保序。"""
    holders: list[tuple[int, str]] = []
    for index, root in enumerate(shapes.roots(content)):
        known = {node.id for node in shapes.walk_nodes(root)}
        if known & set(ids):
            holders.append((index, root.id))
    return holders


def _apply_ops_any(content: DiagramContent, batch: OpBatch) -> DiagramContent:
    """在三种结构上执行一批 op。

    树直接交给 `apply_ops`（执行器只有一份）。鱼骨与流程是**多棵并列子树**，借用
    同一条路：把命中的那棵子树当作一个临时 `MindMapContent` 的根跑一遍，再把结果
    写回原位。原子性与 id 规范化因此仍然只有一份实现——"失败一个 op 都不落"这件事
    不该被实现三遍。

    **一批 op 只允许落在一棵子树里**（一根骨 / 一个阶段）。跨子树的搬动在数据上
    没有"它该属于哪根骨"的确定答案：那是三套结构各自的语义，属于阶段 3。这里明确
    报 `cross_container` 而不是猜一个位置——猜错的后果是内容静默地换了骨。

    ## 边 op（`add_edge` / `remove_edge`）按图型分流

      - **树**：交给树的执行器（端点是节点 id）。
      - **鱼骨**：仍然明确拒绝（`edges_not_supported`）——它的顶层结构里根本
        没有 `edges`，这是个结构事实，不是"还没做"。
      - **流程**：**按层级落笔**（`ops.resolve_flow_edge_placement`）。两个端点都是
        阶段 id → `content.edges`；同属一个阶段 → 那个阶段的 `edges`。批次里既有节点
        op 又有边 op 时，**先节点后边**：先 `add` 出来的节点才能被边引用，这也正是
        模型写 op 的顺序。
    """
    if isinstance(content, MindMapContent):
        return apply_ops(content, batch)

    # 边 op 单独摘出来：鱼骨要拒，流程要走"判层级"那条路。**保留原始下标**——
    # `OpError.path` 用的是 `ops.<下标>`，重排之后报错会指向另一条 op。
    edge_items = [
        (f"ops.{index}", op) for index, op in enumerate(batch.ops)
        if op.op in ("add_edge", "remove_edge")
    ]
    if edge_items and not isinstance(content, FlowContent):
        # `path` 指到**第一条**边 op：模型要知道是哪一条没落下去（`ops.3` 比 `ops` 有用）。
        _, first = edge_items[0]
        raise OpError(
            "edges_not_supported", edge_items[0][0],
            "鱼骨图没有附加边：六根骨之间的关系全部由层级表达，"
            f"这条 {first.op} 没有位置可落。这一次没有改动任何一个节点。",
        )

    if isinstance(content, FlowContent):
        current: DiagramContent = content
        node_items = [op for op in batch.ops if op.op not in ("add_edge", "remove_edge")]
        if node_items:
            current = _apply_ops_in_container(
                current, batch.model_copy(update={"ops": node_items}), validate=False)
            # 连带删边（与树上 `_apply_remove` 同一语义）：删掉一个步骤之后，端点是它的
            # 阶段内边必须一起消失。留着它们，`validate_flow` 会以 `tree_invalid` 拒掉
            # 整批，而模型只是"删了一个节点"——它改不动任何一张带判定出边的流程。
            current = _prune_stage_edges(current)
        if edge_items:
            current = apply_flow_edge_ops(current, edge_items)
        elif node_items:
            current = validate_flow(current)
        return current

    return _apply_ops_in_container(content, batch)


def _prune_stage_edges(content: FlowContent) -> FlowContent:
    """把端点已经不存在的阶段内边丢掉。没有可丢的时候返回**同一个对象**。"""
    stages: list = []
    changed = False
    for stage in content.stages:
        alive = {node.id for node in shapes.walk_nodes(stage.root)}
        kept = [edge for edge in stage.edges
                if edge.source in alive and edge.target in alive]
        if len(kept) != len(stage.edges):
            changed = True
            stages.append(stage.model_copy(update={"edges": kept}))
        else:
            stages.append(stage)
    return content.model_copy(update={"stages": stages}) if changed else content


def _prune_dangling_relations(content: DiagramContent) -> DiagramContent:
    """把端点已经不存在的 `relations` 丢掉。没有可丢的时候返回**同一个对象**。

    `relations` 落在契约的 `ContentBase` 上——**三种形状都有**，而它的端点永远是
    节点 id。所以"删节点连带删"的范围不止 `edges`：

      - 树的 `edges` 由 `ops._apply_remove` 一起清（执行器里那一处，见它的注释）；
      - 流程的阶段内边由 `_prune_stage_edges` 清；
      - 鱼骨没有边，但**它同样有 `relations`**。

    这一步放在"整图校验之前"（`_apply_ops_in_container` 的 revalidate），因为
    子树被抽出来当临时 `MindMapContent` 跑 op 时，`relations` 根本不在那个临时
    对象里（它属于整图），执行器内的清理看不见它。漏了这一处的表现是：删掉一个
    被关系引用的骨下节点，整批 op 以 `tree_invalid` 被拒，而模型只收到"改动之后
    整张图不合法"。
    """
    if not content.relations:
        return content
    alive = shapes.node_ids(content)
    kept = [relation for relation in content.relations
            if relation.source in alive and relation.target in alive]
    if len(kept) == len(content.relations):
        return content
    return content.model_copy(update={"relations": kept})


def _apply_ops_in_container(
    content: DiagramContent, batch: OpBatch, *, validate: bool = True,
) -> DiagramContent:
    """把一批**节点** op 落进它们命中的那一棵子树（一根骨 / 一个阶段）。

    `validate=False` 让调用方在"还有别的批次要接在后面"时跳过那次整图校验——
    流程那条路会在节点 op 之后先剪掉悬空边、再落边 op，最后统一校验一次。
    中途校验会把"边马上要被清掉"的中间态判成非法。
    """
    holders = _holders_of(content, _op_target_ids(batch))
    if not holders:
        raise OpError(
            "node_not_found", "ops",
            "这一批操作提到的节点都不在当前导图里（鱼骨图与流程图按骨/阶段分片，"
            "改之前先确认节点 id 取自这份导图）",
        )
    if len(holders) > 1:
        names = "、".join(
            f"「{shapes.find(content, root_id).label if shapes.find(content, root_id) else root_id}」"
            for _, root_id in holders[:5])
        raise OpError(
            "cross_container", "ops",
            f"这一批操作跨了 {len(holders)} 处（{names}）：鱼骨图的每一根骨、流程图的"
            "每一个阶段是一棵独立的子树，一次只能改其中一处；请拆成几批",
        )
    _, root_id = holders[0]
    root = shapes.find(content, root_id)
    if root is None:  # 理论上不可达（`_holders_of` 就是从这棵子树里找到的）
        raise OpError("node_not_found", "ops", f"子树根 {root_id} 不在这份导图里")
    temp = MindMapContent(
        title=content.title,
        root=root,
        theme=content.theme,
        source_usage=list(content.source_usage),
    )
    updated_root = apply_ops(temp, batch).root
    moved = shapes.replace_node(content, root_id, lambda _node: updated_root)
    # 删节点连带删关系：`relations` 属于整图，临时 `MindMapContent` 里没有它，
    # 所以清理只能落在这里（理由见 `_prune_dangling_relations`）。
    moved = _prune_dangling_relations(moved)
    if not validate:
        return moved
    try:
        return shapes.revalidate(moved)
    except ValidationError as exc:
        details = "；".join(error["msg"] for error in exc.errors()[:3])
        raise OpError("tree_invalid", "ops", details or "改动之后整张图不合法") from exc


def _is_descendant(content: DiagramContent, ancestor_id: str, candidate_id: str) -> bool:
    """`candidate_id` 是不是 `ancestor_id` 的子孙（**含**自己）。

    与 `ops.is_descendant` 同一份语义，区别只在"从哪棵子树开始找"：鱼骨与流程里
    节点分散在各自骨/阶段下，从 `content.root` 出发的版本对它们根本不成立。
    """
    ancestor = shapes.find(content, ancestor_id)
    if ancestor is None:
        return False
    return any(node.id == candidate_id for node in shapes.walk_nodes(ancestor))


def _is_container_root(content: DiagramContent, node_id: str) -> bool:
    """这个节点是不是**某个容器/整张图的根**（树的中心主题、一根骨、一个阶段的入口）。

    它们删不得、移不得：删掉一根骨就不是鱼骨图了，移到别处则会多出一棵没有归属的
    子树。判据按形状走（`shapes.roots`），不看 `diagram_type` 字段。

    **不做**"这个节点是不是最后一个阶段"之类的三套语义判断——这里只回答一个结构
    问题：它有没有上一层容器。
    """
    if isinstance(content, MindMapContent) and node_id == content.root.id:
        return True
    return any(root.id == node_id for root in shapes.roots(content))


async def _commit_drafts(
    ctx: RunContext, content: DiagramContent, drafts: list[dict[str, Any]],
) -> tuple[DiagramContent, OpBatch, dict[str, str]] | ToolResult:
    """把一批草案落成工作树 `ctx.candidate`：规范化 id → 建批次 → 原子执行 → 记账。

    **五个写工具共用这一条路**（`mindmap_edit` 与四个细粒度工具）。抽出来不是
    为了少写几行，而是为了让"原子"这个词只有一个实现：任何一条 op 失败，`apply_ops`
    丢弃副本、`ctx.candidate` 一个字都不动，调用方拿到的只是一个 `ToolResult` 失败。
    如果让 `add_node` 自己 `find_node` + `insert`，那"失败时一个 op 都不落"就要靠
    它自己再实现一遍，而重复实现的那一份迟早与批量那一份不一样。

    返回值三态：成功是 `(树, 批次, id 映射)`，失败是 `ToolResult(ok=False)`。
    用返回值而不是抛异常，是因为失败信息要**原样回给模型**（`exc.path` 指到具体
    字段），而它本来就该是一条正常的工具结果。

    它是 `async` 的**只因为检查点**：落树之后要把这一批结果写进台账（见那条
    `await ctx.checkpoint`）。除此之外整段是纯计算，`_apply_ops_any` 不 await 任何
    东西。注册表本来就两种都收（`ToolRegistry.execute` 里 `inspect.isawaitable`），
    所以改这一处不会波及契约；代价是六个同步写工具也跟着变成 `async def`——它们
    本来就是"调一次写一把"的形状，加个 `async` 只是让这一条路能被 await 完。
    """
    try:
        ops, id_map = normalize_ops(drafts, shapes.node_ids(content))
    except ValidationError as exc:
        detail = "；".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:5]
        )
        return _fail("invalid_ops", f"操作列表不合法：{detail}")

    batch = OpBatch(batch_id=_batch_id(ctx), base_revision=ctx.revision, ops=ops)
    try:
        updated = _apply_ops_any(content, batch)
    except OpError as exc:
        # 把执行器的错误原样回给模型：它知道是哪一条、哪个字段错了才能自己改对
        return _fail(exc.code, f"{exc.path}：{exc.message}", path=exc.path)

    ctx.publish_tree(updated)
    ctx.batches.append(batch)
    # 检查点：这一批 op 已经把树落到 `ctx.candidate` 了，趁它还和台账里那棵同形，
    # 立刻让后台存一份。edit 路**不需要**像 `_generate` 那样再折一次形——`updated`
    # 是 `_apply_ops_any` 的产物，本来就是最终图型（`_shape_output` 只在创建那条路上
    # 才需要把内部树折成骨图/流程图）。
    #
    # 失败不抛：检查点自带"存不上就算了"的语义（`runs.checkpoint` 不抛出，
    # 路由层那份回调还兜了一层 warning），不该让它把一次本来能成功的编辑弄失败。
    if ctx.checkpoint is not None:
        await ctx.checkpoint(updated)
    return updated, batch, id_map


def _single_result(
    ctx: RunContext,
    updated: DiagramContent,
    batch: OpBatch,
    id_map: dict[str, str],
    content: str,
    *,
    changed: Sequence[str] = (),
    outside_selection: Sequence[str] = (),
    **data: Any,
) -> ToolResult:
    """细粒度工具的成功结果：形状与 `_edit` 一致，额外带"这次动了哪些 id"。

    `data` 的键必须与 `_edit` 对齐（`batch` / `batches` / `id_map` / `revision` /
    `outside_selection`）：事件流按 `result.data["batches"]` 发 `ops` 帧
    （`service._run_tool`），少一个键前端就收不到增量——候选树停在原地，而工具
    回给模型的是一句"已修改"。

    `outside_selection` 是"这次动到了用户选区之外的哪几个节点"，**空列表也是有效
    信息**（= 没越界）。缺键与"没越界"混在一起时，排查"这次为什么改到我别的分支"
    就只能靠读提示词，而提示词是会变的那一层。
    """
    return ToolResult(
        ok=True,
        content=content,
        data={
            "batch": batch,
            "batches": [batch],
            "id_map": id_map,
            "revision": ctx.revision,
            "node_count": shapes.count_nodes(updated),
            "changed_ids": sorted(set(changed)),
            "outside_selection": list(outside_selection),
            **data,
        },
    )


def _check_color_arg(value: str | None) -> str | None:
    """工具参数里的 `color`：归一大小写，**非法值直接报错**。

    与生成侧（`normalize_color` 把不认识的键丢掉）方向相反，理由在**上下文**，
    不在字段本身：

      - 骨架那一次调用已经花掉了，为一个装饰字段把整棵树判失败就是白花钱；
      - 工具调用可以重来，而"默默不改"是最坏的一种反馈——`ok=true` 但树没变，
        模型以为换好了颜色，用户看到的是"点了没反应"，两边都不知道发生了什么。

    所以这里给一条能照着做的错误：列出这八个键。空串是**清空**（update 路径），
    不是错误。
    """
    if value is None:
        return None
    key = value.strip().lower()
    if not key:
        return ""
    if key not in NODE_COLORS:
        raise ValueError(
            f"color 只能是 {'/'.join(NODE_COLORS)} 之一（不是任意十六进制），收到 {value!r}")
    return key


def _check_icon_arg(value: str | None) -> str | None:
    """工具参数里的 `icon`：必须真的是一个 emoji，否则报错（理由同上）。

    `normalize_icon` 返回 `None` 而输入非空，说明里面含字母或数字——模型把
    「放大镜」这种**词**写进了 icon。落到树上会变成标题前面挂着四个汉字，
    看起来像标题的一部分。空串同样是清空。
    """
    if value is None:
        return None
    text = " ".join(value.split())
    if not text:
        return ""
    if normalize_icon(text) is None:
        raise ValueError(f"icon 要一个 emoji（例如 🎯 ⚠️ 📊），收到 {value!r}")
    return text[:MAX_NODE_ICON_CHARS]


async def _add_node(args: AddNodeArgs, ctx: RunContext) -> ToolResult:
    """在父节点下加**一个**子节点。

    与 `mindmap_edit` 的 `add` 是同一条落树路（`_commit_drafts` → `apply_ops`），
    差别只在参数形态：这里没有 `op` 数组，一次只加一个。这样切的实际收益是**报错
    更准**——模型写错一个字段时，错误指向的是那次调用本身，而不是"第 3 条 op 的
    parent_id"。

    限额检查放在这里而不是交给 `apply_ops` 的整树校验：那道兜底会给出
    `tree_invalid`（"导图最多 1000 个节点"），模型得自己猜到是哪条 op 撞的；
    这里能直接说"这张图已经有 1000 个节点"。而且 `add_node` 只加一个节点，
    预检的代价是一次整数比较。

    三种结构上限（节点数 / 层数 / 单节点子数）**都在写树之前**判：越界时
    `ctx.candidate` 一个字都不动，模型看到的是"这次没加成"，而不是一个半成品。
    """
    content = _require_candidate(ctx)
    if isinstance(content, ToolResult):
        return content

    # 选区只是提示：父节点在选区外时照样加，但要在结果里说一声（`_scope_note`）。
    scope_note, outside = _scope_note(content, ctx, [args.parent_id])

    parent = shapes.find(content, args.parent_id)
    if parent is None:
        return _fail("parent_not_found", f"父节点 {args.parent_id} 不存在。")
    if len(parent.children) >= MAX_CHILDREN:
        return _fail(
            "too_many_children",
            f"节点「{parent.label}」已经有 {len(parent.children)} 个子节点，"
            f"最多 {MAX_CHILDREN} 个，加不下了。",
        )
    node_limit = _node_limit()
    used = shapes.count_nodes(content)
    if used >= node_limit:
        return _fail(
            "too_many_nodes",
            f"这张图已经有 {used} 个节点，达到上限 {node_limit}，加不了新节点。"
            "想继续加请先删掉一些节点。",
            node_count=used, node_limit=node_limit,
        )
    depth = shapes.depth_of(content, parent.id)
    if depth is not None and depth + 1 > mindmap_contract.MAX_TREE_DEPTH:
        return _fail(
            "too_deep",
            f"节点「{parent.label}」已经在第 {depth} 层，导图最多 "
            f"{mindmap_contract.MAX_TREE_DEPTH} 层，它下面加不了子节点。",
            depth=depth, depth_limit=mindmap_contract.MAX_TREE_DEPTH,
        )

    index = len(parent.children) if args.index is None else args.index
    if index > len(parent.children):
        return _fail(
            "index_out_of_range",
            f"父节点「{parent.label}」现有 {len(parent.children)} 个子节点，"
            f"index 最大为 {len(parent.children)}，收到 {index}。",
        )

    draft: dict[str, Any] = {
        "op": "add",
        # 临时 id 由 `normalize_ops` 换成真实 id：模型手里没有也造不出真实 id，
        # 写一个像 `n_1` 的名字撞上既有节点的概率远比它想象的高。
        "id": f"{TEMP_ID_PREFIX}add1",
        "parent_id": args.parent_id,
        "index": index,
        "label": args.label,
        "detail": args.detail,
        # 引用一律空：编辑路径不开放引用（`EditChange` 的 docstring 里那条理由）。
        "source_refs": [],
        # `color` / `icon` 在参数模型里已经校验并归一（`_check_color_arg` 等）。
        # 这里只是搬运：再归一一次等于给同一件事留两个实现。
        "color": args.color,
        "icon": args.icon,
    }
    committed = await _commit_drafts(ctx, content, [draft])
    if isinstance(committed, ToolResult):
        return committed
    updated, batch, id_map = committed
    node_id = id_map.get(f"{TEMP_ID_PREFIX}add1", f"{TEMP_ID_PREFIX}add1")

    summary = (f"已在「{parent.label}」下新增节点「{args.label}」"
               f"（第 {index + 1} 个，节点 id {node_id}）。")
    if args.color or args.icon:
        marks = "、".join(part for part in
                          (f"颜色 {args.color}" if args.color else "",
                           f"图标 {args.icon}" if args.icon else "") if part)
        summary += f"视觉标记：{marks}。"
    return _single_result(
        ctx, updated, batch, id_map, summary + scope_note,
        changed=[node_id, args.parent_id],
        outside_selection=outside,
        node_id=node_id, parent_id=args.parent_id, index=index,
    )


async def _update_node(args: UpdateNodeArgs, ctx: RunContext) -> ToolResult:
    """改**一个**节点的字段（至少给一个）。

    `None` = 这一项不动、空串 = 清空该项，两件事必须分开（理由见
    `contracts.ops.UpdateChanges` 的 docstring）。只给了空白字符串的 `detail`
    也算"清空"——用户的详情原本就是空白时，归一成 `None` 是一致的。

    这里**不做**节点数/层数检查：改字段不改结构，那些上限与这次操作无关。
    真正需要拦的只有两件事：节点存在、以及**引用不可改**（`UpdateNodeArgs` 里
    根本没有 `source_refs`，参数模型的 `extra="forbid"` 会让硬塞进来的那个字段
    变成一条参数错误，而不是被静默忽略）。
    """
    content = _require_candidate(ctx)
    if isinstance(content, ToolResult):
        return content

    scope_note, outside = _scope_note(content, ctx, [args.node_id])

    node = shapes.find(content, args.node_id)
    if node is None:
        return _fail("node_not_found", f"节点 {args.node_id} 不存在。")

    if (args.label is None and args.detail is None
            and args.color is None and args.icon is None):
        return _fail(
            "invalid_ops",
            "update_node 至少要给一个要改的字段（label / detail / color / icon）。",
        )

    changes: dict[str, Any] = {}
    # 变量名不叫 `field`：`dataclasses.field` 在上面被 import 进来了，同名会把它
    # 遮住（ruff 的 F402 就在守这件事，而它一旦被遮住，下一次有人在本函数里
    # 写 `field(...)` 会拿到一个字符串）。
    for name in ("label", "detail", "color", "icon"):
        value = getattr(args, name)
        if value is not None:
            changes[name] = value
    draft = {"op": "update", "id": args.node_id, "changes": changes}

    committed = await _commit_drafts(ctx, content, [draft])
    if isinstance(committed, ToolResult):
        return committed
    updated, batch, id_map = committed

    changed_fields = "、".join(changes)
    return _single_result(
        ctx, updated, batch, id_map,
        f"已更新节点「{node.label}」（{changed_fields}）。" + scope_note,
        changed=[args.node_id],
        outside_selection=outside,
        node_id=args.node_id, changed_fields=sorted(changes),
    )


def _edges_endpointed_in(content: DiagramContent, doomed: set[str]) -> list[Edge]:
    """端点落在 `doomed` 里的边。**两张边表都算**。

    流程图的边分两层：`FlowContent.edges` 的端点是**阶段 id**（删一个步骤碰不到
    它），`FlowStage.edges` 的端点才是节点 id。只数前一层的话，删掉一个带判定出边
    的步骤时这里返回空列表，于是工具报 `removed_edge_count: 0`——而画布上那两条线
    已经跟着没了。模型据此以为"一条关系都没删"，下一轮要么重复添加、要么漏掉补边。

    数的必须是**真被删掉**的那一批：清边的两个地方（`ops._apply_remove` 与
    `_prune_stage_edges`）判的都是"端点已不存在"，与这里同一个判据。
    鱼骨没有边表，返回空列表。
    """
    pools: list[list[Edge]] = []
    if isinstance(content, MindMapContent):
        pools.append(content.edges)
    elif isinstance(content, FlowContent):
        pools.append(content.edges)
        pools.extend(stage.edges for stage in content.stages)
    return [edge for pool in pools for edge in pool
            if edge.source in doomed or edge.target in doomed]


async def _remove_node(args: RemoveNodeArgs, ctx: RunContext) -> ToolResult:
    """删一个节点——**连同它的整棵子树**（级联）。

    子树策略为什么是级联，而不是"子节点提升到父节点"或"有子节点就拒绝"：

      1. **与既有语义一致**。`remove` op、前端 Delete 键、`ops.ts` 的
         `removeNode` 全都是级联。让工具做另一件事，就会看到"模型删 A，前端删 B"
         这种只有对比两条路才会发现的差异。
      2. **提升会偷偷改形状**。子节点被挂回父节点时插在哪一位没有正确答案
         （原来的位置已经不存在了），而层级还可能在提升后超过 8 层直接被契约拒。
         一次"删一个节点"变成"重排一段树"，是模型没有要求过的事。
      3. **拒绝不可行**。要先删子节点，模型就得知道子树有多大——它没有"看子树"
         的工具，只能从上下文里已有的部分猜，猜错就进入"删了一半、报错、再试"
         的循环，每次都是真实计费。
      4. **删错了能恢复**。这里有三层：整批原子（失败一个都不落）、`done` 之前
         不落画布（整棵树一次性替换，不会停在删了一半的中间态）、画布撤销栈
         （前端 50 步）。返回值里还带
         `removed_nodes`（标题列表），模型可以照着重建。

    根节点不可删：删了就没有导图了。这一条与 `_apply_remove` 一致，只是这里提前
    给出更好读的理由。
    """
    content = _require_candidate(ctx)
    if isinstance(content, ToolResult):
        return content

    scope_note, outside = _scope_note(content, ctx, [args.node_id])

    if _is_container_root(content, args.node_id):
        return _fail(
            "cannot_remove_root",
            "这一处是整张图/一根骨/一个阶段的入口，删了它这张图就不成立；"
            "要清空它请删它下面的节点。",
        )
    node = shapes.find(content, args.node_id)
    if node is None:
        return _fail("node_not_found", f"节点 {args.node_id} 不存在。")

    parent = shapes.find_parent(content, args.node_id)
    removed = [item.label for item in _iter_nodes(node)]
    # 被连带删掉的边与关系：端点落在**这棵子树**里（含子孙）。必须在提交前算，
    # 提交之后就只剩一个已经删干净的新图，"删了几条线"这个数就没了。
    doomed = _node_ids(node)
    removed_edges = _edges_endpointed_in(content, doomed)
    removed_relations = [relation for relation in content.relations
                         if relation.source in doomed or relation.target in doomed]
    draft = {"op": "remove", "id": args.node_id}

    committed = await _commit_drafts(ctx, content, [draft])
    if isinstance(committed, ToolResult):
        return committed
    updated, batch, id_map = committed

    summary = f"已删除节点「{node.label}」"
    if len(removed) > 1:
        # 级联必须说出来：用户看到的是一次"少了一整片"，而模型以为只删了一个。
        summary += f"及其 {len(removed) - 1} 个子孙节点（共 {len(removed)} 个）"
    if removed_edges:
        # 连带删边也要说出来，理由同上：模型看不到画布，不说它会以为线还在。
        summary += f"，同时删掉了 {len(removed_edges)} 条与它相关的附加关系"
    if removed_relations:
        # `relations` 界面不渲染，模型在别处也读不到它们的存续——不说，它连
        # "这两条线已经没了"都无法知道。
        summary += f"，以及 {len(removed_relations)} 条跨节点关系"
    summary += "。画布上可以撤销。"
    return _single_result(
        ctx, updated, batch, id_map, summary + scope_note,
        changed=[args.node_id] + ([parent.id] if parent is not None else []),
        outside_selection=outside,
        removed_nodes=removed,
        removed_count=len(removed),
        removed_edges=[edge.id for edge in removed_edges],
        removed_edge_count=len(removed_edges),
        removed_relations=[relation.id for relation in removed_relations],
        removed_relation_count=len(removed_relations),
        parent_id=parent.id if parent is not None else None,
    )


async def _move_node(args: MoveNodeArgs, ctx: RunContext) -> ToolResult:
    """把一个节点连同子树挂到另一个父节点下。

    两道结构检查在这里（环由 `apply_ops` 兜底，那一条不需要看深度）：

      - **根节点不能移**：执行器会把它报成 `cycle_detected`（根的每个候选父节点
        都是它自己或它的子孙），那个码对"我想移动根"这件事毫无指导意义；
      - **移动后不能超过 8 层**：新父节点的层数 + 这棵子树的高度必须装得下。
        这一条执行器**不检查**（它只看环），所以不在这里判的话，一棵 6 层的子树
        挂到第 5 层下面，会在整树校验时才炸成 `tree_invalid`——那时模型已经
        收到过"已移动"的念头，而代价是一整批白做。
    """
    content = _require_candidate(ctx)
    if isinstance(content, ToolResult):
        return content

    # 移动涉及两个节点（被移动的与新的父节点），两个都要算进"有没有出选区"。
    scope_note, outside = _scope_note(content, ctx, [args.node_id, args.new_parent_id])

    if _is_container_root(content, args.node_id):
        return _fail("cannot_move_root", "根节点不能移动：它没有父节点。")
    node = shapes.find(content, args.node_id)
    if node is None:
        return _fail("node_not_found", f"节点 {args.node_id} 不存在。")
    target = shapes.find(content, args.new_parent_id)
    if target is None:
        return _fail("parent_not_found", f"新的父节点 {args.new_parent_id} 不存在。")
    # 环检测要先做，否则下面那个深度算法会在环上转不出来（子树的高度不再有限）。
    # `is_descendant` 含自己，"挂到自己"与"挂到自己的子孙"由同一句拦住。
    if _is_descendant(content, args.node_id, args.new_parent_id):
        return _fail(
            "cycle_detected",
            f"节点 {args.new_parent_id} 在「{node.label}」的子树里，"
            "挂过去会形成环，这次没有移动。",
            path=f"{args.node_id}->{args.new_parent_id}",
        )

    parent_depth = shapes.depth_of(content, args.new_parent_id) or 1
    height = _subtree_height(node)
    if parent_depth + height > mindmap_contract.MAX_TREE_DEPTH:
        return _fail(
            "too_deep",
            f"「{node.label}」这一支有 {height} 层，挂到第 {parent_depth} 层的"
            f"「{target.label}」下面会到第 {parent_depth + height} 层，"
            f"超过导图上限 {mindmap_contract.MAX_TREE_DEPTH} 层，这次没有移动。",
            depth=parent_depth + height,
            depth_limit=mindmap_contract.MAX_TREE_DEPTH,
        )
    if len(target.children) >= MAX_CHILDREN and shapes.find_parent(content, node.id) is not target:
        return _fail(
            "too_many_children",
            f"节点「{target.label}」已经有 {len(target.children)} 个子节点，"
            f"最多 {MAX_CHILDREN} 个，放不下这一支。",
        )

    index = len(target.children) if args.index is None else args.index
    old_parent = shapes.find_parent(content, args.node_id)
    if old_parent is not None and old_parent.id == args.new_parent_id:
        # 同父移动：先摘后插，所以此刻的长度已经少了一个。索引上界按移除后算，
        # 与 `_apply_move` / 前端 `ops.ts` 是同一条语义。
        limit = max(0, len(target.children) - 1)
    else:
        limit = len(target.children)
    if index > limit:
        return _fail(
            "index_out_of_range",
            f"新父节点「{target.label}」现有 {len(target.children)} 个子节点，"
            f"index 最大为 {limit}，收到 {index}。",
        )

    draft = {"op": "move", "id": args.node_id,
             "new_parent_id": args.new_parent_id, "index": index}
    committed = await _commit_drafts(ctx, content, [draft])
    if isinstance(committed, ToolResult):
        return committed
    updated, batch, id_map = committed

    summary = f"已把「{node.label}」（含 {_count_nodes(node) - 1} 个子孙节点）移动到「{target.label}」下"
    summary += f"，位置第 {index + 1} 个。"
    return _single_result(
        ctx, updated, batch, id_map, summary + scope_note,
        changed=[args.node_id, args.new_parent_id]
        + ([old_parent.id] if old_parent is not None else []),
        outside_selection=outside,
        node_id=args.node_id, parent_id=args.new_parent_id, index=index,
    )


def _subtree_height(node: Node) -> int:
    """节点自己算一层的子树高度（叶子是 1）。"""
    return 1 + max((_subtree_height(child) for child in node.children), default=0)


def _check_kind_arg(value: str | None) -> str | None:
    """工具参数里的 `kind`：只收三个值，空串是"不声明"（等于 flow）。

    与 `_check_color_arg` 同一条方向：**非法值报错**，不静默改成 `None`。
    模型写 `kind: "next"` 时，静默落成 `flow` 会让它以为"我标了条件分支"，
    而画布上那条线是普通箭头——两边都不知道发生了什么。错误信息里列出三个
    合法值，它下一轮就能改对。
    """
    if value is None:
        return None
    key = value.strip().lower()
    if not key:
        return None
    if key not in EDGE_KINDS:
        raise ValueError(f"kind 只能是 {'/'.join(EDGE_KINDS)} 之一，收到 {value!r}")
    return key


def _fail_edges_not_supported() -> ToolResult:
    """`add_edge` / `remove_edge` 落在**鱼骨图**上的同一句人话。

    抽出来是因为两条工具必须说同一件事：两处各写一份文案，改一处忘一处时，用户
    会从"加边"和"删边"两个入口得到互相矛盾的说明。

    **只剩鱼骨这一种了**：流程图的边现在分两层落笔（阶段之间 / 阶段内部），判据在
    `ops.resolve_flow_edge_placement`。鱼骨仍然没有位置可放——它的顶层结构里根本
    没有 `edges`（六根骨的关系全部由层级表达），那是个结构事实，不是"还没做"。
    """
    return _fail(
        "edges_not_supported",
        "鱼骨图没有附加边：六根骨之间的关系全部由层级表达，没有「谁指向谁」这个槽位。"
        "这一次没有任何内容被改动。",
    )


def _flow_edge_layer_summary(content: FlowContent, placement) -> str:
    """这条边落在流程图哪一层的人话（给模型读，所以要说清**写进了哪个数组**）。"""
    if placement.layer == INNER_LAYER:
        return f"落在{_flow_stage_label(content, placement.stage_id)}内部（阶段内边，端点用该阶段的节点 id）"
    if placement.demoted:
        return ("两个端点分属不同阶段，已按阶段之间的连线记录——`FlowContent.edges` 的"
                "端点只能是阶段 id，所以精确到步骤的位置保不住，方向还在")
    return "落在阶段之间（`FlowContent.edges`，端点用阶段 id）"


def _flow_stage_label(content: FlowContent, stage_id: str | None) -> str:
    """阶段 id → 「阶段名」。找不到退回 id（排查时它至少指得回原始数据）。"""
    stage = next((item for item in content.stages if item.id == stage_id), None)
    if stage is None:
        return f"阶段 {stage_id}"
    return f"阶段「{stage.root.label.strip()}」"


def _flow_endpoint_label(content: FlowContent, endpoint_id: str) -> str:
    """边端点 → 显示名。**两个命名空间**：先当阶段（`s_…`），再当节点。

    顺序不能反：一个阶段的入口节点与那个阶段指的是同一块内容，而当 id 只可能是
    其中一种时（`s_` 前缀由后端分配，真实节点 id 不带它），先查哪种都一样；但报错
    文案里写「阶段「形式审查」」比写节点标题更能说清"这条线连的是阶段"。
    """
    stage = next((item for item in content.stages if item.id == endpoint_id), None)
    if stage is not None:
        return f"{stage.root.label.strip()}（阶段）"
    node = shapes.find(content, endpoint_id)
    return node.label.strip() if node is not None else endpoint_id


async def _add_edge(args: AddEdgeArgs, ctx: RunContext) -> ToolResult:
    """在**两个已有节点**之间连一条附加边。

    与 `add_node` 是同一条落树路（`_commit_drafts` → `apply_ops`）：整批原子，
    失败时 `ctx.candidate` 一个字都不动。预检放在这里而不是全交给执行器，是为了
    报错更准（"起点节点不存在"能指到 `source` 那个参数，而不是一句"第 0 条 op 失败"）。

    允许**成环**（A→B 之后 B→A）：那正是回退线，是流程图的核心需求。上限只有
    "单图 200 条边"（`MAX_EDGES`，与契约的诊断口径同一个数）——超过时明确拒绝
    并说明，而不是让第 201 条在保存时被 422 打回。

    ## 流程图：先判这条边属于哪一层（`ops.resolve_flow_edge_placement`）

    两个端点都是**阶段 id** → 落 `FlowContent.edges`（阶段之间的主轴）；
    两个端点同属一个阶段 → 落那个阶段的 `FlowStage.edges`（判定节点的「是 / 否」）。
    这是"一张图两处数组"的唯一分流点，与生成侧 `shapes._split_edges` 同一套规则——
    两处不同源的话，同一对端点在生成路径与编辑路径上会落进不同的数组。
    """
    content = _require_candidate(ctx)
    if isinstance(content, ToolResult):
        return content
    if isinstance(content, FishboneContent):
        return _fail_edges_not_supported()

    placement = None
    if isinstance(content, FlowContent):
        try:
            placement = resolve_flow_edge_placement(content, args.source, args.target)
        except OpError as exc:
            return _fail(exc.code, exc.message, path=exc.path)

    scope_note, outside = _scope_note(content, ctx, [args.source, args.target])

    source = shapes.find(content, args.source)
    target = shapes.find(content, args.target)
    if isinstance(content, FlowContent) and placement is not None:
        source_name = _flow_endpoint_label(content, args.source)
        target_name = _flow_endpoint_label(content, args.target)
    else:
        source_name = source.label.strip() if source is not None else args.source
        target_name = target.label.strip() if target is not None else args.target
    if source is None and not (isinstance(content, FlowContent) and placement is not None):
        return _fail("node_not_found", f"起点节点 {args.source} 不存在。")
    if target is None and not (isinstance(content, FlowContent) and placement is not None):
        return _fail("node_not_found", f"终点节点 {args.target} 不存在。")
    if args.source == args.target:
        return _fail(
            "self_loop",
            f"「{source_name}」不能连到自己：自环在画布上只会渲染成一个点。",
        )

    # 上限与去重按**这条边要落进去的那个数组**判（两层各算 200）：判整体的话，
    # 模型会被"另一个数组满了"挡住一条本来放得下的线，而错误文案也指不清是哪一层。
    if isinstance(content, FlowContent) and placement is not None:
        sibling = (content.edges if placement.layer == STAGE_LAYER else next(
            stage.edges for stage in content.stages if stage.id == placement.stage_id))
        where = "阶段之间" if placement.layer == STAGE_LAYER else "这个阶段内部"
    else:
        sibling = content.edges
        where = "这张图"
    if any(edge.source == args.source and edge.target == args.target
           and edge.label == (args.label or None) for edge in sibling):
        same = f"（标签「{args.label}」）" if args.label else ""
        return _fail(
            "duplicate_edge",
            f"「{source_name}」→「{target_name}」{same} "
            f"这条边已经存在（{where}）。要两条平行边，请给不同的 label"
            "（例如「是」与「否」）。",
        )
    if len(sibling) >= MAX_EDGES:
        return _fail(
            "too_many_edges",
            f"{where}已经有 {len(sibling)} 条附加边，达到上限 {MAX_EDGES} 条，"
            "加不了新边。请先删掉一些用不上的关系。",
            edge_count=len(sibling), edge_limit=MAX_EDGES,
        )

    draft = {
        "op": "add_edge",
        # 临时 id 由 `normalize_ops` 换成真实边 id（`e_` 前缀）。
        "id": f"{TEMP_ID_PREFIX}edge1",
        "source": args.source,
        "target": args.target,
        "label": args.label,
        "kind": args.kind,
    }
    committed = await _commit_drafts(ctx, content, [draft])
    if isinstance(committed, ToolResult):
        return committed
    updated, batch, id_map = committed
    edge_id = id_map.get(f"{TEMP_ID_PREFIX}edge1", f"{TEMP_ID_PREFIX}edge1")

    label_text = f"（{args.label}）" if args.label else ""
    kind_text = {"branch": "条件分支", "loop": "回退", None: "顺序跳转", "flow": "顺序跳转"}
    summary = (
        f"已连接「{source_name}」→「{target_name}」{label_text}，"
        f"类型：{kind_text.get(args.kind or 'flow', '顺序跳转')}（边 id {edge_id}）"
    )
    if isinstance(updated, FlowContent) and placement is not None:
        summary += f"；{_flow_edge_layer_summary(updated, placement)}"
        edge_count = (len(updated.edges) if placement.layer == STAGE_LAYER else len(next(
            stage.edges for stage in updated.stages if stage.id == placement.stage_id)))
    else:
        edge_count = len(updated.edges)
    summary += "。"
    return _single_result(
        ctx, updated, batch, id_map, summary + scope_note,
        changed=[args.source, args.target],
        outside_selection=outside,
        edge_id=edge_id, source=args.source, target=args.target,
        edge_layer=None if placement is None else placement.layer,
        edge_count=edge_count,
    )


async def _remove_edge(args: RemoveEdgeArgs, ctx: RunContext) -> ToolResult:
    """删一条附加边。**不动节点**：删线不改结构，所以不检查节点数/层数。

    存在的边才删得掉：给一个不存在的 id 直接报 `edge_not_found`，而不是
    "默默成功"——后者会让模型以为那条线没了，而画布上仍然挂着。

    流程图**两个数组都查**（`content.edges` 与各阶段自己的 `edges`）：只查前者的话，
    阶段内的判定出边列得出来却删不掉，而"删不掉"在模型那边表现为"边不存在"，
    下一轮它会继续试同一条。
    """
    content = _require_candidate(ctx)
    if isinstance(content, ToolResult):
        return content
    if isinstance(content, FishboneContent):
        return _fail_edges_not_supported()

    edge = next((item for item in content.edges if item.id == args.edge_id), None)
    layer = STAGE_LAYER
    if edge is None and isinstance(content, FlowContent):
        for stage in content.stages:
            edge = next((item for item in stage.edges if item.id == args.edge_id), None)
            if edge is not None:
                layer = INNER_LAYER
                break
    if edge is None:
        return _fail("edge_not_found", f"边 {args.edge_id} 不存在。")

    committed = await _commit_drafts(ctx, content, [{"op": "remove_edge", "id": args.edge_id}])
    if isinstance(committed, ToolResult):
        return committed
    updated, batch, id_map = committed

    summary = f"已删除「{edge.source}」→「{edge.target}」这条附加关系"
    if edge.label:
        summary += f"（{edge.label}）"
    summary += f"（{ '阶段内部' if layer == INNER_LAYER else '阶段之间' if isinstance(updated, FlowContent) else '这张图'}）"
    if isinstance(updated, FlowContent):
        remaining = len(updated.edges) + sum(len(stage.edges) for stage in updated.stages)
        summary += f"；整张图还剩 {remaining} 条。"
    else:
        summary += f"；现在还剩 {len(updated.edges)} 条。"
    # 删边不动节点，但两个端点仍然决定"这次动到了哪一片"：删一条跨选区的线，
    # 与删一条选区内部的线，对用户来说是两件不同的事。
    scope_note, outside = _scope_note(content, ctx, [edge.source, edge.target])
    return _single_result(
        ctx, updated, batch, id_map, summary + scope_note,
        changed=[edge.source, edge.target],
        outside_selection=outside,
        edge_id=args.edge_id, edge_layer=layer,
        edge_count=(len(updated.edges) if not isinstance(updated, FlowContent)
                    else len(updated.edges) + sum(len(stage.edges) for stage in updated.stages)),
    )


def _top_level_changes(item: EditOp) -> dict[str, Any]:
    """`EditOp` 顶层那四个可改字段里**给了的**那几个。

    单独一个函数是因为它被两处用：`changes` 缺省时（模型把字段平铺在 op 上），
    以及 `changes` 是空对象但顶层写了字段时（两种写法混用）。两处各写一遍
    `{k: v for ...}`，加第五个字段时就会漏掉一处，而漏掉的表现是"模型说改了，
    树没变"。
    """
    return {
        key: value for key, value in (
            ("label", item.label),
            ("detail", item.detail),
            ("color", item.color),
            ("icon", item.icon),
        ) if value is not None
    }


async def _edit(args: EditArgs, ctx: RunContext) -> ToolResult:
    content = _require_candidate(ctx)
    if isinstance(content, ToolResult):
        return content

    # 选区只是**提示**：动到选区之外照样执行，结果里附一句"这几处不在你选中的
    # 范围"（理由见 `_outside_selection` 的 docstring）。注册表按意图裁剪仍是
    # 第一层防线（expand 下连 `mindmap_edit` 都看不到），这一层不再拦第二刀。
    scope_note, outside = _scope_note(content, ctx, _edit_targets(args))

    drafts: list[dict[str, Any]] = []
    # 同一批里对同一个父节点连续 add 时，缺省 index 必须按“前面几条已经加进去之后”
    # 的长度算。直接用执行前的长度会让「先 first 后 second」变成「second、first」。
    pending: dict[str, int] = {}
    for item in args.ops:
        draft: dict[str, Any] = {"op": item.op, "id": item.id}
        if item.parent_id is not None:
            draft["parent_id"] = item.parent_id
        if item.new_parent_id is not None:
            draft["new_parent_id"] = item.new_parent_id
        # update 的 label 只能进 changes：顶层再放一份会被 UpdateOp 的
        # extra="forbid" 拒掉，那个错误信息对模型毫无指导意义。
        if item.label is not None and item.op != "update":
            draft["label"] = item.label
        # `detail` / `color` / `icon` 三条同一条规矩：`add` 放在 op 顶层，
        # `update` 收进 `changes`（模型两种写法都会出现，入口吸收掉比让它下一轮
        # 改对便宜一次调用）。三条都不在这里校验：契约层（`AddOp` / `UpdateChanges`）
        # 会把非法值连同字段路径一起报回来，`normalize_ops` 已经把它转成了
        # `invalid_ops`——在这一层再判一遍就是两处真相。
        if item.detail is not None and item.op != "update":
            draft["detail"] = item.detail
        if item.op != "update":
            for field in ("color", "icon"):
                value = getattr(item, field)
                if value is not None:
                    draft[field] = value
        if item.changes is not None:
            draft["changes"] = {
                key: value for key, value in (
                    ("label", item.changes.label),
                    ("detail", item.changes.detail),
                    ("color", item.changes.color),
                    ("icon", item.changes.icon),
                ) if value is not None
            }
            # `changes: {}` 但顶层写了字段：模型两种写法混用时的形态。吸收掉，
            # 否则 `changes` 为空会在契约层报"至少要改一个字段"，而模型明明给了。
            if not draft["changes"] and item.op == "update":
                absorbed = _top_level_changes(item)
                if absorbed:
                    draft["changes"] = absorbed
        elif item.op == "update":
            # 实测模型会把 label 直接写在 op 顶层而不是 changes 里。它被拒之后
            # 下一轮能自己改对（错误反馈是有效的），但那要多花一次模型调用和
            # 几秒等待。两种写法都能表达同一件事，就在入口吸收掉。
            absorbed = _top_level_changes(item)
            if absorbed:
                draft["changes"] = absorbed

        # 显式 index 对所有 op 都必须保留。之前只有 add 分支复制了它，
        # move 给了合法 index 反而在契约层报 move.index: Field required。
        if item.index is not None:
            draft["index"] = item.index

        if item.op == "add":
            # 草案允许省略 index，补成追加到末尾：模型经常只给 parent_id 和 label，
            # 这不该被判成参数错误。
            parent_id = item.parent_id
            if parent_id is None:
                return _fail("invalid_ops", "add 必须给出 parent_id。", path=f"ops.{item.id}.parent_id")
            if "index" not in draft:
                parent = None if parent_id.startswith("$") else shapes.find(content, parent_id)
                base = len(parent.children) if parent else 0
                draft["index"] = base + pending.get(parent_id, 0)
                pending[parent_id] = pending.get(parent_id, 0) + 1
            draft.setdefault("source_refs", [])
        if item.op == "move" and "index" not in draft:
            draft["index"] = 0

        drafts.append(draft)

    committed = await _commit_drafts(ctx, content, drafts)
    if isinstance(committed, ToolResult):
        return committed
    updated, batch, id_map = committed
    ops = batch.ops

    changed = {op.id for op in ops} | {
        op.new_parent_id for op in ops if op.op == "move" and op.new_parent_id
    }
    return ToolResult(
        ok=True,
        content=(f"已应用 {len(ops)} 条修改，导图现在有 {shapes.count_nodes(updated)} 个节点。"
                 + scope_note),
        data={
            "batch": batch,
            "batches": [batch],
            "id_map": id_map,
            "revision": ctx.revision,
            "changed_ids": sorted(changed),
            "outside_selection": outside,
        },
    )


def _batch_id(ctx: RunContext) -> str:
    return f"b{ctx.revision + 1}-{len(ctx.batches)}"


# ---------------------------------------------------------------- 注册


# 意图 -> 允许的工具。这张表就是"模型这条路能走多远"的唯一声明。
#
# `create` 只有一条生成路：**整树一次生成**（`mindmap_generate`）——骨架一次、
# 每支检索一次、内容一次写完。分片生成（`mindmap_generate_deep`）的代码与规格
# 都还在（`_TOOL_SPECS` 里有 **两个**生成工具），但**不注册到这里**：它服务的
# 是"更大的规模"，即一次响应写不下 200+ 节点时，靠 N+1 次调用把单次输出量与
# 整图上限脱钩。真实用法跑不到那个规模（`runs` 里 16 次运行 `sources` 全为 0、
# 没有材料时分片付了全部代价拿到零收益），所以默认给一次写完的那条；等真有大图
# 需求时把 `"mindmap_generate_deep"` 加进这一行即可，实现一行都不用改。
#
# 形态不再参与裁剪——有没有来源，模型看到的生成工具都是同一个。
#
# `edit` 给**八个**：六个细粒度（加/改/删/移 + 两条边）、批量 `mindmap_edit`，
# 以及只补节点说明的 `mindmap_fill_details`（它不动结构，所以不参与下面这段
# "为什么留两个入口"的权衡）。
# 为什么不只留批量：批量的参数是一张 op 表，改一处也要写出 `[{"op":"update",
# "id":…,"changes":{"label":…}}]` 这一串，而模型的失败模式恰恰在这里——少一个
# 括号、把 label 写在 op 顶层、把 index 写成字符串。细粒度工具把这些自由度
# 压成扁平参数，改一处就是一次调用、一处错误。批量留作**五处以上**的兜底：
# 一次工具调用改十处，省下的不是"额度"（50 次够细粒度改 49 处），而是四十次
# 模型往返——每次工具调用都要等模型再看一眼结果。
_ALLOWED_BY_INTENT: dict[str, tuple[str, ...]] = {
    "create": ("mindmap_generate",),
    # `create_deep` 不是一个产品意图，它是**给分片那条路留的入口**：`create` 现在
    # 只给整树一次生成，而分片的实现、规格与既有用例都还在。挂在这个名字下，
    # "分片没被注册进 `create`"这件事就是可断言的（用例断言 `create` 的名单里
    # 没有它），同时它也没有从代码里消失——`service.run` 永远不会把 intent 解成
    # 这个名字，所以生产路径看不到它，将来要把它挂回大图时把 `create` 那一行
    # 换成它（或并列）即可。
    "create_deep": ("mindmap_generate_deep",),
    # `audit_tree` 只给 `expand`：那条路上模型是**逐个** `node_expand` 展开的，
    # "还有哪几支没展开"没有任何代码在替它数——`mindmap_generate_deep` 内部那套
    # 验收（`_audit_branches` / `partial`）只覆盖生成那一次，用户在生成之后说
    # "把这张图补完"时，模型能看到的大纲还可能被 200 行截断。
    #
    # 为什么不给 `create`：那条路的循环边界由代码保证（工具内部 gather 全部支、
    # 自己判 sparse 并报 `partial`），再给一个审计工具只是把同一条事实说两遍，
    # 而"create 只有一条生成路"是一条被 `tests/test_one_path.py` 钉住的硬约束
    # ——审计不是第二条路，但它也不该在这条路上多一个可调的旋钮。
    "expand": ("node_expand", "audit_tree"),
    "edit": (
        "add_node", "update_node", "remove_node", "move_node",
        "add_edge", "remove_edge", "mindmap_fill_details", "mindmap_edit",
    ),
}

# 会读来源的意图。`create` 不在里面，而理由**不是**预算：分片生成在工具内部
# 自己检索（骨架之后每支一次、限额补查再各一次），模型再查一遍拿到的是同一批
# 片段的另一截——而 `mindmap_generate_deep` 没有任何参数能接收它。查了只能自己
# 在文字里复述，白烧一次模型往返。`max_tool_calls` 从 8 提到 50 不改变这条：
# 它是**重复**，不是容量。
_SOURCE_AWARE_INTENTS = ("expand", "edit")

# 注册顺序固定，便于日志与测试对照；用工厂而不是共享实例，避免一个 registry
# 上的改动影响到另一个（ToolSpec 目前不可变，但这个保证不该靠约定维持）。
#
# 细粒度工具排在 `mindmap_edit` **前面**：`specs()` 按注册顺序下发，模型读工具
# 列表是有先验的，先看到的那几个更容易被选。这一条不是玄学——四个细粒度工具
# 每个只花一次调用，批量则是"最后手段"，顺序把这条分工又强调了一次。
_TOOL_ORDER = (
    "mindmap_generate",
    # 未注册到任何意图（见 `_ALLOWED_BY_INTENT` 的注释）。留在顺序表里是为了
    # 将来把它挂回 `create` 时，注册顺序与它的规格一起就位。
    "mindmap_generate_deep",
    "node_expand",
    "rag_query",
    "audit_tree",
    "add_node",
    "update_node",
    "remove_node",
    "move_node",
    "add_edge",
    "remove_edge",
    "mindmap_fill_details",
    "mindmap_edit",
)

# 图型 → 一句中文说明。给**模型**读（拼进生成工具的描述），前端 `SourceInputs.vue`
# 的界面说明与它措辞一致：用户看到的与模型被告知的是同一件事，两边各写一套说法
# 迟早会分叉成"用户以为选了流程、模型以为要画导图"。
_DIAGRAM_HINTS = {
    "tree": "思维导图（一级分支按话题分解）",
    "flowchart": "流程图（一级分支是执行阶段，会额外给出条件分支与回退的连线）",
    "fishbone": "鱼骨图（一级分支固定为人/机/料/法/环/测）",
}


def _generate_spec(diagram_type: str | None = None) -> ToolSpec:
    """整树生成的工具定义。`diagram_type` 非 None 表示**用户已经选了**。

    两条分支的差别与 `_generate_deep_spec` 完全同构（参数谁来定），所以这里只写
    差异：描述里的第一句必须说清"一次调用出整棵树"，因为模型对"生成"的默认
    预期是分步的；不说的话它会在一次调用之后还想再展开一轮。

    它**不**承诺"一次调用写完全部内容"：工具内部是骨架 → 检索 → 结构 → 说明四步，
    最后一步只写 detail（理由见 `_generate` 与 `_fill_details`）。对调用方（模型）
    来说仍然是"一次调用拿到整张图"，所以描述里说的还是这一件事。
    """
    if diagram_type is None:
        diagram_sentence = (
            "diagram_type 选图型：tree 思维导图（默认，按话题分解）；"
            "flowchart 流程图（一级分支是阶段，会额外给出条件分支与回退的连线）；"
            "fishbone 鱼骨图（一级分支固定为人/机/料/法/环/测）。"
        )
    else:
        diagram_sentence = (
            f"本次图型已由用户在界面上选定：{_DIAGRAM_HINTS[diagram_type]}。"
            "按这个图型生成，不要再自行判断图型（这个参数不会下发给工具）。"
        )
    return ToolSpec(
        name="mindmap_generate",
        description=(
            "生成一张完整思维导图：一次调用出整棵树（定骨架 → 按支检索 → 写结构 → 逐个补说明）。"
            "带材料时它会先看一眼材料自带的章节标题来定一级骨架，再为每条一级分支"
            "各自检索一次，最后把骨架与全部检索结果一起交给模型写完结构与引用，"
            "再用一次调用专门给每个节点写说明；"
            "它不会把全文读进来。没有材料时直接按主题生成。"
            "初次上限是 80 个节点、4 层（含中心主题），更细的内容留给生成之后的"
            "node_expand 逐支展开。"
            + diagram_sentence
            + "只有当前还没有导图时可用。"
        ),
        args_model=GenerateArgs,
        fn=_generate,
        hidden_args=() if diagram_type is None else ("diagram_type",),
    )


def _generate_deep_spec(diagram_type: str | None = None) -> ToolSpec:
    """分片生成的工具定义。**当前未注册到任何意图**（见 `_ALLOWED_BY_INTENT`）。

    定位（写给下一个要把它挂回去的人）：

      - **它服务的是规模，不是质量**。整树一次生成（`mindmap_generate`）把全部
        分支放进一次响应，好处是跨支不重复、耗时三到八秒；代价是单次输出上限
        ——一棵 4 层满额的 200 节点图约 4 万字 detail，任何 provider 一次都写不下
        （`MAX_OUTPUT_TOKENS` 封顶 65536 token，超了就是截断，而截断出来的树
        尾部整支消失）。分片把"单次输出量"与"整图上限"解耦：每次只写一支。
      - **门槛在哪**：`INITIAL_MAX_NODES = 80`。初次生成走整树，80 节点装得进
        一次响应；要 200+ 节点时整树会先被 `_clip_tree` 裁到 80，那时才需要这一条。
      - **挂回去的方式**：把 `"mindmap_generate_deep"` 加进 `_ALLOWED_BY_INTENT`
        的 `create` 那一行（它的 spec 已经在 `_TOOL_SPECS` 里、顺序在
        `_TOOL_ORDER` 里），实现一行都不用改。
      - **代价要说清**：没有材料时它相对整树没有任何收益（真实用法 100% 无材料），
        却要 1+N 次调用与三十秒；有材料时它的覆盖率更高（30.5% vs 5.1%），但那时
        整树那条路也按支检索了（`_retrieve_branches`），两边证据同源，差别只剩下
        单次输出量。

    `diagram_type` 非 None 表示**用户已经选了**。两条分支的差别就是"这个参数谁来定"：

      - `None`（老前端、或用户选了"自动判断"）：参数留在 schema 里，由模型判断
        ——这是 `2ed92ac` 的行为，逐字节不变。
      - 非 `None`：参数从 schema 里**抹掉**（`hidden_args`），描述改成"本次图型
        已由用户选定：X"。模型既不会去填它，也不会在"该画流程还是导图"上再猜
        一次；它唯一要做的就是按这个图型生成。请求层是权威（`_generate_deep`
        的第一行就覆盖），这里是让它连填的机会都没有。

    描述里那句图型说明必须**跟着分支走**：留着"diagram_type 选图型"而 schema 里
    没有这个参数，模型会去找一个不存在的参数，或者干脆以为自己该画导图。
    """
    if diagram_type is None:
        diagram_sentence = (
            "diagram_type 选图型：tree 思维导图（默认，按话题分解）；"
            "flowchart 流程图（一级分支是阶段，会额外给出条件分支与回退的连线）；"
            "fishbone 鱼骨图（一级分支固定为人/机/料/法/环/测）。"
        )
    else:
        diagram_sentence = (
            f"本次图型已由用户在界面上选定：{_DIAGRAM_HINTS[diagram_type]}。"
            "按这个图型生成，不要再自行判断图型（这个参数不会下发给工具）。"
        )
    return ToolSpec(
        name="mindmap_generate_deep",
        description=(
            "分片生成一张完整思维导图：先定一级骨架，再按层并发展开。某一支要不要"
            "继续深挖由你在展开响应里用 expandable 声明，代码只按这个判断还问不问；"
            "默认一直问到所有支都到底、或者触到节点数与调用预算。"
            "可用 max_depth 收窄层数。最后按代码验收并只补不合格的那几支。"
            "它每次只让模型输出一支，不受单次输出上限挤压，代价是 N+1 次调用。"
            # 这句必须与 `_generate_deep` 的实际行为一致：它现在**会**检索
            # （rag 形态下每支一次，服务端在工具内部做），也**会**在骨架那一次
            # 看材料自带的章节标题。写成"不读来源"会让模型在带材料的请求里判断错。
            "带材料时它会先看一眼材料自带的章节标题来定一级骨架，再为每条一级分支"
            "各自检索一次并只让那一支看到自己的检索结果，"
            "所以带材料的主题同样可以用它；它不会把全文读进来。"
            # 图型必须说出来：模型看不到"我该画流程还是画导图"的界面，只能靠这句
            # 判断。不说的话它一律按默认的 tree 走，用户选了流程图也拿到思维导图。
            + diagram_sentence
            + "只有当前还没有导图时可用。"
        ),
        args_model=GenerateDeepArgs,
        fn=_generate_deep,
        # 请求层定死图型时，这个参数对模型**不存在**（理由见本函数的 docstring）。
        hidden_args=() if diagram_type is None else ("diagram_type",),
    )


_TOOL_SPECS: dict[str, Callable[[], ToolSpec]] = {
    # `create` 的生成路（唯一的）：骨架 + 按支检索 + **一次**写完。
    "mindmap_generate": lambda: _generate_spec(),
    # 分片生成。代码留着、规格留着，但**不在 `_ALLOWED_BY_INTENT` 里**——它服务的
    # 是"更大的规模"（一次写不下 200+ 节点时的唯一办法），需要时把它加回
    # `create` 那一行即可，不必恢复任何一行实现。
    "mindmap_generate_deep": lambda: _generate_deep_spec(),
    "node_expand": lambda: ToolSpec(
        name="node_expand",
        description=(
            "把当前选中的那个节点展开成 2–4 个子节点，只新增这一层，不改动其他节点。"
            "模型会看到从根到这个节点的标题链与它已有的子节点，所以写出来的内容要落在"
            "这一支的范围里，并且不能与已有子节点重复。"
            "节点已经到底（第 8 层 / 模型说过没有更细内容）时它会被拒，理由会一起返回。"
            # 循环写在这里，是因为它是模型在 expand 意图下**读到的第一个工具**：
            # 只在 audit_tree 自己的描述里写"它什么时候该被调"，等于要求模型先想到
            # 去读一个它还不知道要用的工具。
            "**用户要求把整张图补全 / 补齐时**：先调 audit_tree 看还有哪几条一级分支"
            "没有子节点，对每一条调一次 node_expand，补完再调一次 audit_tree 复查，"
            "missing 为空才收尾——你自己数不准，大纲只列前 200 个节点。"
        ),
        args_model=ExpandArgs,
        fn=_expand,
    ),
    "rag_query": lambda: ToolSpec(
        name="rag_query",
        description=(
            "在本次请求附带的资料与知识库里检索相关原文片段。"
            "回答关于资料的问题之前先调用它。"
        ),
        args_model=RagArgs,
        fn=_rag,
        readonly=True,
    ),
    # 审计工具的描述就是它的分工声明：**数事实的是它，补不补的是模型**。
    # 描述里写"missing 非空就继续调 node_expand"，而不是把这条写进 system：
    # 模型是逐个读工具决定下一步的，写在 system 里它读完就忘，写在这里它每次
    # 想看"还差什么"时都会重读一遍。
    "audit_tree": lambda: ToolSpec(
        name="audit_tree",
        description=(
            "体检当前这棵导图，**只数事实不产节点**：返回一级分支总数、已展开几条、"
            "还有哪几条一级分支没有任何子节点（missing）、哪几条只有一个子节点（thin）、"
            "以及树上有多少个叶子节点（empty）。"
            "用户要求「把整张图补完 / 补齐 / 看看还缺什么」时用它：先 audit_tree，"
            "对 missing 里的每一条调 node_expand，再 audit_tree 复查，"
            "直到 missing 为空才收尾。"
            "**你自己数不准**：大纲只列前 200 个节点，大图里后面的分支你看不到；"
            "这个工具读的是服务端那棵真树，不受截断影响。"
            "它不改任何东西，可以反复调。"
        ),
        args_model=AuditTreeArgs,
        fn=_audit_tree_tool,
        readonly=True,
    ),
    # 四个细粒度工具的描述都要写清"一次只改一处"与"什么时候改用批量"：
    # 分工写在**每个**工具里而不是只写在批量那一个里——模型是逐个读工具决定
    # 用哪个的，只在批量那边写"改一处请用细粒度"，它在读 add_node 时看不到。
    "add_node": lambda: ToolSpec(
        name="add_node",
        description=(
            "在指定父节点下加**一个**子节点。原子：成功就落这一个，失败一个都不落。"
            "要加两个请调用两次，或者改用 mindmap_edit 的批量 add。"
            f"label 建议不超过 {GENERATED_LABEL_MAX_CHARS} 字（不带句号），长内容放 detail。"
            f"color 只能取 {'/'.join(NODE_COLORS)} 之一，icon 是一个 emoji；两者都可选。"
            "取值写错（十六进制、颜色名、把词当图标）会被拒并列出可用的写法。"
            f"受限：整图 {MAX_TREE_NODES} 节点、{mindmap_contract.MAX_TREE_DEPTH} 层、"
            f"单节点 {MAX_CHILDREN} 个子节点，越界会被拒并说明原因。"
        ),
        args_model=AddNodeArgs,
        fn=_add_node,
    ),
    "update_node": lambda: ToolSpec(
        name="update_node",
        description=(
            "改**一个**节点的字段（label / detail / color / icon，至少给一个）。"
            '空串表示清空该项：detail="" 清掉详情，color="" / icon="" 摘掉视觉标记。'
            "颜色只收预设的语义色、图标要一个 emoji，取值写错会被拒（不会「默默不改」）。"
            "不能改引用（source_refs）：引用只由生成路径写入，编辑路径不开放这个入口。"
            "不受节点数/层数限制（改字段不改结构）。"
            "要改多个节点请调用多次，或者改用 mindmap_edit 的批量 update。"
        ),
        args_model=UpdateNodeArgs,
        fn=_update_node,
    ),
    "remove_node": lambda: ToolSpec(
        name="remove_node",
        description=(
            "删**一个**节点，**连同它的整棵子树**（级联删除：不把子节点挂到父节点上，"
            "也不要求先删子节点）。根节点不能删。"
            # 连带删边必须写进描述：模型看不到画布，它只知道"我删了一个节点"。
            # 不说的话，它下一轮会奇怪"为什么我画的回退线没了"，甚至去重建一条。
            "**与这个节点相关的附加关系（边）会一起删掉**：源或目标命中被删节点"
            "（含它的子孙）的边都不再存在，因为端点没了。"
            "返回里带被删掉的节点标题，删错了可以照着重建；画布上还能撤销。"
            "想保留子节点，请先用 move_node 把要留的那些挪到别的父节点下。"
        ),
        args_model=RemoveNodeArgs,
        fn=_remove_node,
    ),
    "move_node": lambda: ToolSpec(
        name="move_node",
        description=(
            "把**一个**节点连同它的子树挂到另一个父节点下（index 省略＝追加到末尾）。"
            f"会检查环与层级：挂到自己的子孙下、根节点、或移动后超过 "
            f"{mindmap_contract.MAX_TREE_DEPTH} 层，都会被拒且一个 op 都不落。"
        ),
        args_model=MoveNodeArgs,
        fn=_move_node,
    ),
    # 补说明：管理"结构已经长好、每个节点的说明还空着"这种状态。
    #
    # 描述里必须写清两件模型自己看不出来的事：①**它只写说明、不动结构**（不说的话
    # 模型会以为这是"再生成一遍"而先调它再调批量，白烧两轮）；②**什么时候该用**
    # （用户说"说明太短/把说明补上/这些节点没有介绍"）。这两条写在描述里而不是
    # system 里：模型是逐个读工具决定下一步的，写在 system 里它读完就忘。
    "mindmap_fill_details": lambda: ToolSpec(
        name="mindmap_fill_details",
        description=(
            "给当前导图里**说明为空或太短**的节点补写 detail。只写说明，"
            "**不改任何结构**：节点、层级、颜色、引用一个字节都不动。"
            "补哪些节点由代码自己数（它读的是服务端那棵真树，不看你记不记得），"
            "所以没有 node_id 参数，也不要把节点清单写进 instruction。"
            "**什么时候用它**：用户说「说明太短」「给这些节点补上介绍」"
            "「载入的图没有说明」；或者这张图是从检查点载回来的"
            "（结构完整、说明全空）。"
            "**不要用它**加节点或改标题——那些用 add_node / update_node / "
            "mindmap_edit。"
            "节点说明都已经写满时它不会调用模型，直接返回「没有需要补写的地方」。"
        ),
        args_model=FillDetailsArgs,
        fn=_fill_details_tool,
    ),
    "mindmap_edit": lambda: ToolSpec(
        name="mindmap_edit",
        description=(
            "按节点 id 批量修改已有导图：一次提交 1–100 条 op（add / update / remove / move），"
            "整批原子（任一条失败则整批不生效）。"
            "**改一两处不要用它**：用 add_node / update_node / remove_node / move_node，"
            "它们一次只改一处、参数是扁平的、报错指得更准。"
            "**什么时候用它**：要改五处以上，或者一次改动需要几条有先后关系的 op"
            "（先建父节点再用它的临时 id 挂子节点）——批量只花一次工具调用，"
            "而细粒度工具每个节点各花一次。"
            "不要用它整张重建。"
        ),
        args_model=EditArgs,
        fn=_edit,
    ),
    # 两条边工具。刻意**不进** `mindmap_edit` 的 op 表：批量的 `EditOp` 只覆盖
    # 四种树操作，而边是"附加关系"——把它塞进同一张表会让批量工具的提示词再长
    # 一截，而模型在批量里连边的情况本来就少（一条边改一次，开销只是多一次调用）。
    "add_edge": lambda: ToolSpec(
        name="add_edge",
        description=(
            "在**两个已有节点**之间连一条附加关系（跨父子节点的连线，不是父子层级）。"
            "用在流程图里：判断节点的两个出口各连一条 kind=branch 的边、"
            "带 label「是」/「否」；回退/返工连一条 kind=loop 的边、label 写「驳回」。"
            "source / target 必须是**当前大纲里已有的节点 id**，不能是标题；"
            "可以成环（A→B 之后 B→A），这正是回退线。"
            "label ≤ 40 字；同一条 source→target 上要挂两条线请给不同的 label。"
            f"整图最多 {MAX_EDGES} 条边，超了会被拒。"
            "原子：失败时一个 op 都不落。"
        ),
        args_model=AddEdgeArgs,
        fn=_add_edge,
    ),
    "remove_edge": lambda: ToolSpec(
        name="remove_edge",
        description=(
            "删**一条**附加关系（边），不动任何节点——树的结构一个字节都不变。"
            "edge_id 取自当前大纲里的附加关系列表。"
            "要删节点请用 remove_node（它会连带删掉与那个节点相关的边）。"
        ),
        args_model=RemoveEdgeArgs,
        fn=_remove_edge,
    ),
}


def build_registry(intent: str = "create", *, has_sources: bool = False,
                   has_library: bool | None = None,
                   diagram_type: str | None = None) -> ToolRegistry:
    """本次可用的工具集，按意图裁剪。

    早先是「四个全注册 + 各自内部校验意图」。那样做的问题是模型看得到工具、
    却每次调用都被拒：实测 expand 意图下它坚持调 `mindmap_edit` 删节点，
    失败三次才转向，白白烧掉调用额度。看不到这个工具，它就只能走对的那条路。

    裁剪规则：
      - `create` 只给整树一次生成（`mindmap_generate`），没有第二个选项
      - `expand` 给 `node_expand` + `audit_tree`（整图体检，用户要求"补完"时
        模型靠它知道还差哪几支；那条路上没有代码替它数），请求带来源时再加
        `rag_query`
      - `edit` 给**八个**写工具（`add_node` / `update_node` / `remove_node` /
        `move_node` / `add_edge` / `remove_edge` / `mindmap_edit` /
        `mindmap_fill_details`），请求带来源时再加 `rag_query`。让模型自己选粒度：
        改一两处用细粒度的（参数扁平、报错更准），改五处以上用批量（一次调用改十处，
        省下的是**模型往返**——每次调用都要一轮）；`mindmap_fill_details` 只补节点
        说明、不动结构，用在"这棵树结构已定、说明还空着"的续写场景。
      - `expand` / `edit` 在请求带来源时加 `rag_query`：那两条路不读来源，
        模型只能自己查。
      - **不**给 `create`：生成工具在内部自己检索（骨架之后每支一次）。早先这里
        是反的——那时整树还不预检索，不给工具模型就只能凭标题编。补上预检索之后，
        再给工具就只剩**重复**：模型查到的片段进不了生成工具的任何参数，它只能
        自己在文字里复述，而每一支的检索用的是那支的标题。`max_tool_calls` 从 8
        到 50 不改变这一点。**"请求不带来源、但库非空"那条路也一样**（实测两次
        `rag_query` 白烧 6.8 秒，见下面那个分支的注释）。
      - **请求不带来源、但知识库非空**时，`expand` / `edit` 加 `rag_query`
        （`create` 不加，理由同上——"库里有料"不改变"生成工具自己会检索"这件事）。

    **为什么生成只剩一条路**：分片好用的前提是**有材料**——它相对整树唯一的真
    优势是覆盖率（按支检索 30.5% vs 整树一次 top_k=5 的 5.1%，
    `scripts/measure_deep_slicing.py`，8 支、纯 BM25），而真实用法是 100% 无材料
    （`runs` 里 16 次运行 `sources` 全为 0），那时它付了 1+N 次调用与三十秒，
    换来的收益是零，还额外引入跨支重复（每支只看得到自己那条标题链）。

    所以 `create` 现在是**整树一次生成**，而它并没有退回到那 5.1%：骨架照旧由
    材料目录或模型定（`_skeleton_outline` / `_plan_ask`），证据照旧**每支各检索
    一次**（`_retrieve_branches`），只是把"并发展开"换成"一次写完"。分片那条路
    （`mindmap_generate_deep`）的代码与规格都还在 `_TOOL_SPECS` 里，服务的是
    200+ 节点那种一次写不下的大图，需要时加回上面那一行即可。

    `rag_query` 只在**真有可查的东西**时才注册：没来源也没库的检索必然返回
    「没有找到」，注册它只会诱导模型拿它当"思考"用。这里的"有"包含知识库：
    请求不带材料、库里却有 200 份时，检索是模型唯一能碰到原文的入口——不注册
    就会出现"system 里叫它先检索，工具列表里却没有这个工具"，模型于是凭标题编，
    并以 ok=True 交出一棵没有出处的树（它甚至不算说谎，只是没别的路可走）。

    `has_library=None` 表示**自己去问库**（生产调用方 `service.run` 就用这个）。
    测试与脚本要固定答案时显式传 `False / True`：否则一条用例的结论会悄悄依赖
    宿主机库里恰好存了什么。未知意图按 `create` 兜底——宁可少给工具，也不要把
    破坏性工具塞进一个没预期到的新意图里。

    `diagram_type` 是**用户在界面上选的那个图型**（`AgentRunRequest.diagram_type`）。
    非 None 时生成工具的 `diagram_type` 参数从 schema 里消失、描述改成"已选定"
    ——见 `_generate_spec` / `_generate_deep_spec`。其余九个工具与它无关：
    `expand` / `edit` 那两侧不生成骨架，图型对它们没有任何意义（用例钉着这一点）。
    """
    if has_library is None:
        has_library = library_available()
    allowed = set(_ALLOWED_BY_INTENT.get(intent, _ALLOWED_BY_INTENT["create"]))
    if has_sources:
        # `create` **不**补 `rag_query`：生成工具在内部先检索（骨架之后按支一次），
        # 模型再自行查是**重复**——它查到的片段进不了任何生成工具的参数，只能
        # 自己在文字里复述一遍。`expand` / `edit` 仍需要：那两条路不读来源。
        if intent in _SOURCE_AWARE_INTENTS:
            allowed.add("rag_query")
    if has_library and not has_sources and intent != "create":
        # `create` **同样不补**，理由与上一段那个分支同一条，实测更硬：库里存着
        # 81 份来源时，system 里那句"先调用 rag_query 检索"会让模型在外层先查两次
        # （2026-11-26 实测 `tool_start rag_query` ×2：3.52s + 3.32s），然后才调
        # `mindmap_generate`——而生成工具内部自己按章切分/按支各查一次，查到的
        # 片段进不了它的任何参数。**这 6.8 秒是纯等待**，占那次 139.5 秒总耗时的 5%。
        #
        # `expand` / `edit` 仍然补：那两条路**不读**请求里的来源（`_expand` 只在
        # 节点有 `source_refs` 时回查库，`_edit` 根本不碰正文），"按材料补充这一支"
        # 这类要求没有 rag_query 就无从下手。是否真的查由指令层收窄
        # （`service._RAG_INTENT_RULES` 的 `edit` / `expand` 两条）。
        allowed.add("rag_query")

    registry = ToolRegistry()
    for name in _TOOL_ORDER:
        if name not in allowed:
            continue
        # 两个生成工具都要看 `diagram_type`：请求层定了图型时，那个参数对模型
        # 不存在（各自的 spec 函数里处理 `hidden_args`）。
        if name in ("mindmap_generate", "mindmap_generate_deep"):
            registry.register(
                _generate_spec(diagram_type) if name == "mindmap_generate"
                else _generate_deep_spec(diagram_type))
            continue
        registry.register(_TOOL_SPECS[name]())
    return registry
