"""工具注册表与五个业务工具。

注册表只负责分派与参数校验，业务逻辑在各个 `_xxx` 函数里。

五个工具的划分照「用户意图 + 作用域」而不是 CRUD 动词：
- `mindmap_generate` 只在没有导图时可用，一次调用产出一整棵候选树
- `mindmap_generate_deep` 同样只在没有导图时可用，但拆成「骨架 → 并发展开 →
  验收 → 局部重试」四段，复杂主题用它（理由见该函数的 docstring）。它不读任何
  来源正文，所以请求带材料时注册表根本不注册它——那条路会产出一棵与材料无关
  的树，而且成功返回
- `node_expand` 只能在已选中的节点下加子节点，不能删、不能移
- `rag_query` 只读，查本次请求带来的来源**与知识库**（`library=True`，默认开；
  库命中的 `source_id` 是持久 id，收尾校验前要用 `library.load()` 取回原文）
- `mindmap_edit` 才允许改/删/移，且必须按节点 id

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
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.agent.contracts import MAX_SOURCES, RunContext, ToolCall, ToolResult, ToolSpec
from app.agent.ops import (
    TEMP_ID_PREFIX,
    apply_ops,
    find_node,
    new_node_id,
    normalize_ops,
)
from app.agent.stream_items import StreamItem, closed_items
from app.contracts import mindmap as mindmap_contract
from app.contracts.diagnostic import Diagnostic, DiagnosticLevel
from app.contracts.mindmap import (
    GENERATED_LABEL_MAX_CHARS,
    MAX_NODE_DETAIL_CHARS,
    MindMapContent,
    Node,
    clip_label,
)
from app.contracts.ops import OpBatch, OpError
from app.contracts.source import Source, SourceUsage
from app.generation import budget
from app.generation.service import (
    CONTEXT_TOKEN_LIMIT,
    DETAIL_NO_SOURCE_RULES,
    DETAIL_PROMPT,
    MIN_BRANCHES,
    RAG_SOURCE_PROMPT,
    RAG_THINKING_GUARD,
    SOURCE_MODE_RAG,
    SOURCED_DETAIL_PROMPT,
    SOURCED_THINKING_GUARD,
    GenerationError,
    chat_async,
    ensure_input_within_context,
    estimate_input_tokens,
    max_tokens_cap,
    source_mode,
    thinking_block,
    validate_generated_content,
)
from app.generation.stream_partial import collect_new_nodes, parse_partial
from app.knowledge import library as knowledge_library
from app.knowledge.query_plan import _material_snippets, plan_branches, plan_query
from app.knowledge.service import (
    EvidenceHit,
    SourceFilter,
    filter_sources,
    heading_outline,
    outline_coverage,
    search_multi,
    search_parent,
)
from app.optimization.service import check
from app.optimization.source_refs import EvidenceSpan, annotate_nli, verify_source_refs

logger = logging.getLogger(__name__)

# 与 backend/app/contracts/mindmap.py 的 Node.children max_length 同口径
MAX_CHILDREN = 50

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

# 二级及以下每个父节点最多几个子节点。一级分支由 `GenerateDeepArgs.branches`
# 控制（上限 `DEEP_MAX_BRANCHES`），往下的每一层必须收窄：20 × 5 × 5 × 5 连 1000
# 节点的契约都装不下，而且每层 5 个正是"一屏能扫完"的量。
DEEP_FANOUT = 5

# 一级分支的硬上限。参数模型用 `le=DEEP_MAX_BRANCHES` 引用它，骨架那里也用它截断，
# 两处同源——否则模型无视"最多 N 条"时，一级节点会一直长到契约的单节点上限（50）。
DEEP_MAX_BRANCHES = 20

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
# `_retrieve_branches` 里第一支是串行的，那不是保守，是成本闸。
DEEP_BRANCH_TOP_K = 3

# 一支最多发几路查询：标题 + 2 条同义问法（`query_plan.plan_branches`）。
#
# 为什么是 3 而不是 `plan_branches` 允许的 4（标题 + 3）：那 3 条问法里前两条
# 才进检索，第三条是提示词留给模型的选择余地（它给 3 条，代码取前 2 条）。
# 每多一路就多一次 embedding 往返，而 8 支 × 3 路 = 24 条 query 已经在 5 秒
# 预算的边上。`service.MAX_ROUTES=6` 是**单次检索**的口径，3 路远不到顶：
# 那个上限防的是"一个 query 列表里塞六条以上"，不是"多少支各发几路"。
BRANCH_QUERY_LIMIT = 3

# 分片检索**全部**失败时退回"整树一次检索"的条数。与 `rag_query` 的默认
# `top_k=5` 同口径：那正是 `mindmap_generate` 那条路拿到的量，退回时用户看到的
# 证据量与改造前一致，不是"缩水版的降级"。
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

# 截断必须让模型知情，否则它会把半份材料当成全文来整理和引用。
# 校验用的 sources 始终保留完整原文，只有提示词里的副本可以裁剪。
SOURCE_TRUNCATED_HINT = "（原文过长，已截断）"


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
# `prune(keep_count=200)` 的默认值更宽的数没有意义——超过 200 份时清单里写
# "200 份以上"（见 `_material_manifest`），而 200 正是淘汰策略的稳态规模。
LIBRARY_COUNT_LIMIT = 200


def library_search_enabled() -> bool:
    """知识库检索是否打开。默认开，显式写 `0/off/false/no/空串` 才算关。

    判定方式与 `MINDMAP_PARENT_RETRIEVAL` 一致（正列举真值 + 缺省取 `"1"`），
    理由也一样：谁改默认值，判定逻辑不用跟着改，改错的表现只剩"默认值写错了"。
    """
    return os.getenv(LIBRARY_SEARCH_ENV, "1").strip().lower() in LIBRARY_SEARCH_TRUTHY


def library_source_count() -> int:
    """库里现有多少份材料（**不含正文**，最多数到 `LIBRARY_COUNT_LIMIT`）。

    只用于两件"给模型看"的事：材料清单末尾那行"知识库中另有 N 份"，以及
    `build_registry` 要不要注册 `rag_query`。它不参与任何检索决策——检索侧自己
    会按 `last_used_at` 取一批再粗筛（`service._library_sources`）。

    库不可用（文件被锁、磁盘满、表被删）时返回 0 并记 warning，**不抛**：知识库
    是加分项，它坏了不该让"只带材料"的那条路跟着失败。这里捕获 `Exception` 而
    不是 `sqlite3.Error`，因为这条路上任何异常的正确处置都只有一种——当成空库。
    """
    try:
        return len(knowledge_library.list_sources(limit=LIBRARY_COUNT_LIMIT))
    except Exception as exc:  # noqa: BLE001 — 库坏了就退化成空库，理由见 docstring
        logger.warning("读不到知识库（%s），本次按空库处理：%s",
                       type(exc).__name__, str(exc)[:160])
        return 0


def library_available() -> bool:
    """现在有没有可检索的库材料：开关开着 **且** 库非空。

    两个条件合成一个函数，是因为三处调用点（`_rag`、`build_registry`、
    `_intent_rule`）问的是同一个问题。分头写 `enabled() and count()` 的结果是
    三处各漏一次，而漏一次的表现是"模型看到了工具但查不到东西"或者反过来。
    """
    return library_search_enabled() and library_source_count() > 0


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
            # 返回而不是抛：模型偶尔会编一个很像的工具名（把 mindmap_generate
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


class GenerateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str = Field(min_length=1, max_length=1000, description="中心主题，用用户的语言")


class GenerateDeepArgs(BaseModel):
    """分片生成的入参。

    `branches` 与 `points_per_branch` 是**期望值**而不是硬约束：骨架给多给少一条
    都继续，展开不足只补那一支。把它们写成严格契约，等于把模型的一次抖动
    升级成整次失败。
    """

    model_config = ConfigDict(extra="forbid")
    topic: str = Field(min_length=1, max_length=1000, description="中心主题，用用户的语言")
    # 上限 20 是按契约反推的，不是拍的：最少每支 2 个要点，一个分支占 3 个节点，
    # 1000 节点理论能放 333 支；真正限制它的是可读性——超过二十条一级分支，
    # 图会宽到没法看。原来的 8 没有任何依据，长材料（比如二十几节的讲义）
    # 天然会有更多一级节点，卡在 8 等于逼模型把不相关的东西塞进同一支。
    branches: int = Field(default=5, ge=3, le=DEEP_MAX_BRANCHES, description="一级分支数量上限")
    points_per_branch: int = Field(default=3, ge=2, le=5, description="每条一级分支下至少要有几个要点")
    # 深度同样只是**预算**而不是硬契约：额度不够时会被截短并标 `partial`，
    # 而不是把整次生成判成失败。
    max_depth: int = Field(
        default=DEEP_DEFAULT_DEPTH, ge=2, le=DEEP_MAX_DEPTH,
        description="往下展开到第几层（root 之下算起）：2 只有两层，3 可到三级节点",
    )


class ExpandArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(min_length=1, max_length=100, description="要展开的节点 id，取自当前大纲")
    count: int = Field(default=3, ge=2, le=4, description="要补充几个子要点")
    instruction: str | None = Field(default=None, max_length=300, description="可选的补充要求")


class RagArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=5)
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
    """第一版只允许改标题文字。

    引用（`source_refs`）刻意不开放：模型给节点加一条引用就必须逐字匹配原文，
    而这一步在生成路径上已经由 `validate_generated_content` 做过了；编辑路径
    再开一个入口，等于多一条能凭空造引用的路。
    """

    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=200)


class EditOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["add", "update", "remove", "move"]
    id: str = Field(min_length=1, max_length=100)
    parent_id: str | None = Field(default=None, max_length=100)
    new_parent_id: str | None = Field(default=None, max_length=100)
    index: int | None = Field(default=None, ge=0)
    label: str | None = Field(default=None, max_length=200)
    changes: EditChange | None = None


class EditArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ops: list[EditOp] = Field(min_length=1, max_length=100)


# ---------------------------------------------------------------- 辅助


def _count_nodes(root) -> int:
    return 1 + sum(_count_nodes(child) for child in root.children)


def _clip_tree(content: MindMapContent, limit: int) -> tuple[MindMapContent, int]:
    """前序保留前 `limit` 个节点，返回 `(裁剪后的内容, 丢掉的节点数)`。

    **前序**是刻意的：父节点一定排在它自己的子节点前面，所以裁出来的仍是一棵合法
    的树（不会出现"有子无父"）。代价是"一层的末尾被切掉"——第 80 个节点之后的分支
    整支消失，而不是每个分支均匀地少一点。均匀裁剪反而更难解释：用户会看到每个分支
    都缺一两个，说不出"哪里不完整"。

    裁剪之后要重新走一遍契约校验（`model_validate`）：`relations` 可能指向被裁掉的
    节点，`source_usage` 也不会自己变。**先裁再校验**，校验失败仍然如实报错——一次
    "悄悄留着一棵树但存不进去"的失败比解析失败难查得多。
    """
    total = _count_nodes(content.root)
    if total <= limit:
        return content, 0

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
    kept_ids = _node_ids(root)
    relations = [item for item in content.relations
                 if item.source in kept_ids and item.target in kept_ids]
    trimmed = content.model_copy(update={"root": root, "relations": relations})
    # 走一次 dump/validate：`model_copy` 不做校验，而这里是"结构被代码改过"的唯一
    # 一处，不校验就等于相信自己的算术。
    trimmed = MindMapContent.model_validate(trimmed.model_dump())
    return trimmed, total - _count_nodes(trimmed.root)


def _shortened_ids(original: Node, trimmed: Node) -> set[str]:
    """裁剪之后**掉了子节点**的那些节点的 id。

    `_clip_tree` 只做前序裁剪，它不会增删已保留节点的其他字段，所以"子节点个数
    变少"就是"这一支被切掉了尾巴"。这批节点随后被标成 `expandable="unknown"`：
    它们是"没长完"，不是"到底了"——把被裁的那批标成 `no`，界面上恰好是唯一还能
    继续长的地方灰着按钮。
    """
    kept = {node.id: node for node in _iter_nodes(trimmed)}
    return {
        node.id for node in _iter_nodes(original)
        if (survivor := kept.get(node.id)) is not None
        and len(survivor.children) < len(node.children)
    }


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
    """
    return min(ctx.request.budget.max_output_tokens, max_tokens_cap())


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


def _require_candidate(ctx: RunContext) -> MindMapContent | ToolResult:
    if ctx.candidate is None:
        return _fail("no_candidate", "当前还没有导图，请先调用 mindmap_generate 生成。")
    return ctx.candidate


def _node_ids(root) -> set[str]:
    seen: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        seen.add(node.id)
        stack.extend(node.children)
    return seen


def _skeleton_titles(raw: Any) -> list[str]:
    """从骨架响应里取出分支标题，剔除空项，超长截到 16 字。

    只接受字符串：`{"branches":[{"label":"x"}]}` 说明模型没照格式写，硬转字符串
    会把一整个 dict 的 repr 当成节点标题挂到树上——那种"成功"比失败更难发现。

    截断长度改成 `GENERATED_LABEL_MAX_CHARS`（16）：骨架标题就是画布上的一级
    分支，它与其余节点受同一条限制。**不做二次校验**——模型偶尔写长一点不该判整
    次失败（那会浪费一次已经完成的骨架调用），截断后仍是一棵合法的树。
    """
    if not isinstance(raw, dict):
        return []
    items = raw.get("branches")
    if not isinstance(items, list):
        return []
    titles: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        title = clip_label(item.strip())
        if not title:
            continue
        titles.append(title)
    return titles


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
        self.accepted += 1
        await _emit_grow(self.ctx, self.seen, parent, item.label, item.expandable)
        own = (*parent, index)
        for position, child in enumerate(item.children):
            await self._report(own, position, child)


@dataclass
class _GenerateGrowStream:
    """整树响应自带 id，需要把父 id 转成预览协议里的父节点索引链。"""

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


def _source_prompt_input(
    sources: list[Source], *, system: str = "", retrieval_hint: bool = True,
) -> str:
    """把来源拼成模型这次的输入。**形态由 `MINDMAP_SOURCE_MODE` 决定**。

    - `rag`（默认）：只拼一份**材料清单**（标题、字数、类型），正文一个字都不进
      **这一处**——内容要么靠检索命中，要么靠模型自己的知识。（整次请求里还有检索
      命中、骨架那一次的章节标题，见 `_material_inputs` 的四种说法。）
    - `full`：把每份来源的正文整份拼进去（受上下文预算裁剪），行为与
      `901b484` 逐字节一致，回归用例钉着这一点（`tests/test_source_mode.py`）。

    两种形态的正文可见性完全不同，这是刻意的：`full` 下模型能自己扫全文，
    检索只是辅助；`rag` 下检索是唯一入口。所以这里没有"材料小就走 full"的
    折中——行为随材料大小跳变最难排查，短材料一样走检索。

    `retrieval_hint` 只对 `rag` 生效：整树生成（`_generate`）在拼之前已经做过一次
    主题预检索、命中就附在 user 末尾，这时再指示模型"先调用 `rag_query`"是重复的
    （见 `RAG_SOURCE_PROMPT` 的注释）。别的入口（`rag_query` / 展开 / 改树）那里
    模型确实要自己检索，保留提示。
    """
    if source_mode() == SOURCE_MODE_RAG:
        # 清单是模型**唯一**能知道"手上有什么"的地方，而知识库里的材料不在
        # `sources` 里——不在这里说一句，模型连"库里有东西可查"都无从知道。
        return _material_manifest(
            sources, library_total=library_source_count(), retrieval_hint=retrieval_hint)
    return _full_source_prompt_input(sources, system=system)


# 材料清单的上限。清单是**元信息**，超限时截断的只能是清单项，正文在任何情况下
# 都不进 prompt——"截断正文后拼进去"不是"不放"，是"放一部分"，最难排查。
#
# 条数上限与契约的 `MAX_SOURCES` 是同一个数（请求最多带 50 份），直接引用而不是
# 再写一个 50：两处各写一次，改一处忘一处的结果是"请求收下了、清单里少两份"，
# 模型连那两份材料存在都不知道。渲染器这里仍然要判一次——`_material_manifest`
# 也能被直接调用（测试、将来的建库路径），不能假定调用方已经过了契约那关。
MATERIAL_MANIFEST_MAX_ITEMS = MAX_SOURCES
# 清单本身的 token 上限。实测 50 条常规行（uuid + 中文标题 + 字数）约 1000 token，
# 所以常规批量下先撞到的是条数上限；这条 1500 是给"标题很长"那一类兜底的——
# 80 字标题 × 50 条能到 3000，那时截的是清单项。清单存在的意义只是"知道自己手上
# 有什么"，涨到两千 token 就本末倒置了。
MATERIAL_MANIFEST_TOKEN_LIMIT = 1500
# 单条标题的长度上限。文件名可以是任意长的一串，不夹的话一份材料就能把清单吃满。
MATERIAL_TITLE_MAX_CHARS = 80


def _material_title(source: Source) -> str:
    """清单里显示的名字：优先文件名（去掉扩展名），没有就用 id 兜底。

    "未命名材料"不写成统一占位，是因为同一批里可能有多份没名字的：都叫
    "未命名材料"时模型分不清第 1 条和第 3 条是两份不同的东西。
    """
    name = (source.filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    if name:
        stem, dot, suffix = name.rpartition(".")
        # `a.b.c` 这种多点名字只吃掉最后一段；没有点、或"扩展名"长得不像扩展名
        # （超过 5 个字符）时保留原名，免得把《2024 年度报告》里的点当分隔符。
        if dot and 0 < len(suffix) <= 5:
            name = stem or name
    if not name:
        return source.id
    return name if len(name) <= MATERIAL_TITLE_MAX_CHARS else name[:MATERIAL_TITLE_MAX_CHARS] + "…"


def _material_type_label(source: Source) -> str:
    """类型标签：有扩展名就给扩展名（pdf/docx/mp3），否则退回 `kind`。

    扩展名比 `kind` 细——`document` 分不出 pdf 与 docx，而模型判断"这份材料能问
    什么"时，扫描件和表格类文档的可问法并不一样。
    """
    name = (source.filename or "").strip()
    if "." in name:
        suffix = name.rsplit(".", 1)[-1].strip().lower()
        if 0 < len(suffix) <= 5 and suffix.isalnum():
            return suffix
    return source.kind.value


def _material_scale(source: Source) -> str:
    """规模：文档给字数，语音额外给时长。

    字数取的是**完整** `text` 的长度，不是将要进 prompt 的那部分——rag 形态下没有
    "进 prompt 的那部分"，这个数字的作用是让模型知道"这份材料值不值得查"。
    """
    parts = [f"共 {len(source.text or ''):,} 字"]
    if source.duration_seconds:
        total = int(source.duration_seconds)
        parts.append(f"时长 {total // 60}:{total % 60:02d}")
    if source.sentences:
        parts.append(f"{len(source.sentences)} 句带时间戳")
    return "，".join(parts)


def _material_manifest(
    sources: list[Source], *, library_total: int = 0, retrieval_hint: bool = True,
) -> str:
    """`rag` 形态的输入：只有清单，没有正文。

    形状与 `full` 形态的 sources 数组刻意不同（不是 JSON）：两种形态一眼能分出来，
    排查"这次到底走了哪条路"时不用去读提示词全文。模型也不需要从这个结构里
    解析任何东西——它只需要知道有哪几份材料、能不能查到。

    `library_total` 是知识库里**另外**还有几份材料（`library_source_count()` 的
    结果，0 表示关着或空库）。非 0 时在清单末尾加一行，且**只给数字不列文件名**：

      - 不列是刻意的。库的稳态规模是 200 份（`library.prune` 的 `keep_count`），
        列出来就是又一份"材料清单"，而清单的 1500 token 上限正是为了防止这件事。
        再说模型对它们的操作只有 `rag_query` 一个，名字没有可操作性。
      - 不说是"哪一类"也是刻意的：库里的材料与这次请求无关，任何概括都会变成
        一条错误的先验（"原来库里都是财务制度"）。

    `retrieval_hint=False` 时改写末行：整树生成这条路已经由服务端做过一次主题
    预检索、命中就附在 user 末尾（`_retrieve_topic`），这时再让模型"先调用
    `rag_query`"就是让它重复查同一件事——它拿不到新东西，却多花一次 embedding。
    首行也一并去掉工具名：那一行只需要回答"正文在不在提示词里"。

    默认 0 而不是自己去查库：这个函数是纯渲染器，被测试与将来别的入口直接调用，
    让它内部读一次持久化状态，等于让"渲染"依赖"库的当前内容"，两次调用之间库变了
    就会得到两份不同的提示词，而那种差异只会在真机上偶发。
    """
    head = ("本次附带 {count} 份材料（你看不到全文，正文不在提示词里）："
            if not retrieval_hint else
            "本次附带 {count} 份材料（你看不到全文，需要用 rag_query 检索）：")
    lines = [head.format(count=len(sources))]
    used = estimate_input_tokens(lines[0])
    listed = 0
    for index, source in enumerate(sources, start=1):
        line = (f"{index}. 《{_material_title(source)}》"
                f"（id={source.id}，{_material_scale(source)}，{_material_type_label(source)}）")
        cost = estimate_input_tokens(line)
        # 两个上限都要判：条数防"份数爆掉"，token 防"单条超长把预算吃满"。
        # 截断的永远是清单项——正文从一开始就没打算进来。
        if listed >= MATERIAL_MANIFEST_MAX_ITEMS or used + cost > MATERIAL_MANIFEST_TOKEN_LIMIT:
            break
        lines.append(line)
        used += cost
        listed += 1
    if listed < len(sources):
        # 必须说出来。少列几份却不说，模型会以为手上的材料就这些，
        # 于是"清单里没有"被它读成"材料里没有"。
        lines.append(f"（另有 {len(sources) - listed} 份材料未在清单里列出，"
                     "同样可以用 rag_query 检索到。）")
    if library_total > 0:
        # `LIBRARY_COUNT_LIMIT` 是计数上限，撞到它说明库比这个数更多；写成
        # "200 份以上"而不是"200 份"——四舍五入成一个确定数字会让模型以为
        # 它数得清，而它接下来要判断的是"要不要去查"，不是"到底几份"。
        shown = (f"{library_total} 份以上" if library_total >= LIBRARY_COUNT_LIMIT
                 else f"{library_total} 份")
        lines.append(f"知识库中另有 {shown}过往材料，同样可以用 rag_query 检索到。")
    lines.append(
        "要写与材料有关的内容，先调用 rag_query 检索；引用逐字取自检索结果。"
        if retrieval_hint else
        "与材料有关的原文片段由系统检索后附在本条消息末尾（evidence 数组）："
        "引用逐字取自它，不必自己再查。"
    )
    return "\n".join(lines)


def _full_source_prompt_input(sources: list[Source], *, system: str = "") -> str:
    """`full` 形态：把来源拼成 `SOURCED_DETAIL_PROMPT` 约定的 sources 数组。

    必须逐项带 `source_id`：提示词要求模型按 id 写 `source_refs`，而旧流水线
    的 `_understand()`（已随 `app/pipeline/` 删除）返回的是纯文本合并，里面一个
    id 都没有——模型看不见 id 就永远写不出合法引用，引用功能等于从来没通。那种
    形态服务于限长与 demo 路径，语义不同，不能在这里复用。

    用 `ensure_ascii=False`：中文原样传输，模型看到的 token 更少，
    也免得它把 `\\u4e2d` 这种转义当成正文抄进 quote。
    """
    items = [
        {
            "source_id": source.id,
            "text": source.text or "",
        }
        for source in sources
    ]

    def render() -> str:
        return "sources 数组（JSON）：\n" + json.dumps(items, ensure_ascii=False)

    system_tokens = estimate_input_tokens(system)

    def fits() -> bool:
        return estimate_input_tokens(render()) + system_tokens <= CONTEXT_TOKEN_LIMIT

    if fits():
        return render()

    # 预算属于整次请求：系统提示词、所有来源的 JSON 转义与截断提示都要算。
    # 先给后续来源留出标识与提示，再按原顺序用剩余额度，避免每份各占一整窗。
    for item in items:
        item["text"] = SOURCE_TRUNCATED_HINT if item["text"] else ""
    ensure_input_within_context(
        render(), limit=CONTEXT_TOKEN_LIMIT, extra_tokens=system_tokens,
    )
    for source, item in zip(sources, items):
        def fits_source(text: str) -> bool:
            item["text"] = text
            return fits()

        item["text"] = _clip_source_text(source.text or "", fits=fits_source)
    return render()


def _clip_source_text(text: str, *, fits: Callable[[str], bool]) -> str:
    """用实际提示词的 token 估算找最长前缀，避免中英文共用一个字符闸。"""
    if fits(text):
        return text
    low, high = 0, len(text) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if fits(text[:middle] + SOURCE_TRUNCATED_HINT):
            low = middle
        else:
            high = middle - 1
    return text[:low] + SOURCE_TRUNCATED_HINT


def _selection_scope_error(
    content: MindMapContent, args: EditArgs, ctx: RunContext,
) -> ToolResult | None:
    """把可改范围收窄到用户选中节点的子树。

    注册表已经按意图裁掉了 expand 下的 `mindmap_edit`，但那是**一层**防线：
    注册表被误配、或被调用方直接传进一个全量 registry 时，这一层就没了。
    所以执行层自己再判一次——改错节点是不可逆的（用户手写的分支会被删）。

    两处口径说明：
      - `add` 比的是 `parent_id` 而不是 `id`。新节点的 id 是本批临时名
        （`$tmp1`），它根本不在树里，"落在谁的子树内"对它没有意义；
        真正决定"改到哪去"的是父节点。
      - 同批新建的父节点（`$` 开头）放行：它自己会被"它挂靠在谁下面"
        那条检查约束住，放行不会漏出范围。
    """
    if not ctx.request.selected_ids:
        return None

    scope: set[str] = set()
    for selected in ctx.request.selected_ids:
        node = find_node(content.root, selected)
        if node is not None:
            scope |= _node_ids(node)
    allowed = "、".join(ctx.request.selected_ids)

    for index, item in enumerate(args.ops):
        if item.op == "add":
            if item.parent_id is None:
                # 缺 parent_id 由后面的 invalid_ops 给更准确的错误，这里不抢答
                continue
            target, field = item.parent_id, "parent_id"
            if target.startswith(TEMP_ID_PREFIX):
                continue
        else:
            target, field = item.id, "id"
        if target not in scope:
            return _fail(
                "out_of_selection",
                f"只能改当前选中节点及其子树（{allowed}），第 {index} 条操作指向 {target}。",
                path=f"ops.{index}.{field}",
            )
        if item.op == "move" and item.new_parent_id not in scope:
            return _fail(
                "out_of_selection",
                f"移动的目标父节点 {item.new_parent_id} 不在选中范围（{allowed}）内。",
                path=f"ops.{index}.new_parent_id",
            )
    return None


EXPAND_PROMPT = (
    "你在给思维导图的一个节点补充子要点。只返回一个 JSON 对象，不要代码围栏。"
    '格式为 {"children":[{"label":"要点一","detail":"这个要点的说明"}]}。'
    f"每个 label 是一句话短语，不超过 {GENERATED_LABEL_MAX_CHARS} 字，不带句号；"
    "detail 是 2–3 句说明，可以带数字与条件。"
    "不要编号，不要解释，不要重复已有子节点，不要编造具体数据、人名或文献。"
    # 旧格式（纯字符串数组）仍然收：老模型、老 mock、以及"模型只写了一半"
    # 这几种情况都会退化成它，硬拒等于把一次可用的响应判死。
    '也接受 {"points":["要点一","要点二"]} 这种旧格式，这时节点没有 detail。'
)

# 分片生成第一步：只定骨架，不写内容。一次调用同时要求模型「定框架 + 每支展开」，
# 它会把注意力放在先写出来的那几支上，后面的分支越写越敷衍——这是质量不稳的
# 直接来源。骨架单独一次调用，模型只需回答「分成哪几块」。
PLAN_PROMPT = (
    "你在为一张思维导图只定一级骨架，不写任何子要点。"
    "只返回一个 JSON 对象，不要代码围栏，格式为 "
    '{"branches":["分支一","分支二"]}。'
    # 16 字与 detail 的那两条是同一批改动：骨架标题**就是**画布上的一级分支，
    # 它比任何节点都更该短。20 字的旧上限画出来已经是两行半。
    f"每条分支是一句话短语，不超过 {GENERATED_LABEL_MAX_CHARS} 字，不带句号；"
    "彼此覆盖的范围不重叠，按逻辑顺序排列；不要编号，不要解释，不要编造具体数据、人名或文献。"
    "材料自带章节时优先采用它们作为一级分支；目录为空或不完整时再自行归纳。"
)

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


def _skeleton_outline(sources: list[Source]) -> str:
    """拼给骨架的目录块；不该拼时返回空串。

    目录是**原文的子集**，和 `_material_inputs` 给 planner 的那前 200 字同一类：
    `rag` 形态下"正文不进 prompt"这句话的准确版本是"正文不进，标题行作为结构线索
    会进"（措辞见 `_material_inputs` 的 docstring 与 `generation.service` 里
    `SOURCE_MODE_ENV` 的注释）。这里进 prompt 的只有标题行，一条最多 40 字。

    三件事一起做：取目录、判覆盖率、渲染。判覆盖率必须用**裁剪后**的那份目录
    （`outline_coverage(sources, entries)` 而不是不带参数的调用），否则"覆盖率
    95%"可能配着一份只剩 8 条、恰好漏掉大章的目录。

    百分比的分母是原文总字数，所以"约占 X%"读起来是"这一章大约占全文多少"，
    与模型的直觉一致；小于 1% 的按 1% 显示，写"约占 0%"是在告诉模型这一段不重要，
    而它可能正好是唯一提到某件事的地方。
    """
    entries = heading_outline(sources)
    if not entries:
        return ""
    coverage = outline_coverage(sources, entries)
    if coverage < OUTLINE_MIN_COVERAGE:
        return ""
    total = sum(len(source.text or "") for source in sources)
    lines = [OUTLINE_HEADER]
    for index, entry in enumerate(entries, start=1):
        share = round(entry.coverage / total * 100) if total else 0
        lines.append(f"{index}. {entry.title}（约占 {max(1, share)}%）")
    return "\n".join(lines)


def _plan_ask(topic: str, branches: int, *, outline: str = "") -> str:
    """骨架调用的 user 消息。

    期望分支数必须写进 user：只靠 system 里的通用说明时，请求 3 支与 8 支的
    模型输入一字不差，它只能按自己的直觉给一个数——而 `branches` 是用户在界面
    上选的值，被无视了。单条长度、互不重叠、逻辑顺序三条也一起重申：system 里
    虽有，但这是决定骨架质量的三条硬约束，重复一次便宜，骨架歪了整张图都歪。

    `outline` 是材料自带的章节（`_skeleton_outline` 的产物），**放在主题之后、
    条数要求之前**：顺序即权重，先给它看材料的结构，再让它数条数——反过来先
    见到"最多 8 条"时，它倾向凑满 8 条再找话说。空串表示这份材料没有可用目录，
    那一字不加（与加目录之前逐字相同）。
    """
    parts = [f"主题：{topic}"]
    if outline:
        parts += ["", outline, ""]
    parts.append(f"根据内容实际包含的方面来定一级分支数量，最多 {branches} 条。")
    parts.append("材料层次多就用足、内容单薄就少给，不要为了凑数把不同的事塞进同一支。")
    parts.append("每条不超过 20 字，互不重叠，按逻辑顺序排列。")
    return "\n".join(parts)


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
    "不要编号，不要解释，"
    "只写这个节点范围内的内容，不要重复同一父节点下其他节点的说法，"
    "不要编造具体数据、人名或文献。"
)


# 分片 + 按支检索：有证据时的附加规则。**拼在**上面两份提示词之后，不改基础格式
# ——改基础格式等于给"这一支没有证据"那条路也塞一个 `source_refs` 字段，而那条路
# 上一个字都不该抄。
#
# 逐字照抄那一句与 `RAG_SOURCE_PROMPT` 同源（那里是整树生成用的），但不能复用：
# 那份提示词讲的是"你看不到全文，检索结果附在末尾"，而这里模型手里已经有一支的
# 命中，措辞必须指向 evidence 数组本身。
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


# `rag` 形态下生成前那次主题检索取几条。对齐 `RagArgs.top_k` 的默认值（5）而不是
# `DEEP_BRANCH_TOP_K=3`：这里是**整棵树**一次覆盖主题的全部方面，而分片那 3 条是
# **一支**的量。父块折叠（`_fold_to_parents`）还会再减一轮，实际落到模型手上的
# 条数常少于 5；往下调省的是两三千字符，代价是漏掉整节内容，划不来。
GENERATE_RETRIEVAL_TOP_K = 5

# 命中渲染进 user 消息时的表头。`_rag` 回给模型的是一串裸 JSON 数组（那就是工具
# 返回值的全部内容，模型知道它来自哪个工具），而这里同一段 JSON 出现在 prompt 里、
# 没有工具名作上下文：不说清"这是什么、与 rag_query 的返回同形"，模型会把一段
# 来历不明的数组当成参考资料，或者干脆当没看见。
GENERATE_EVIDENCE_HEADER = (
    "\n本次已对该主题做过一次检索，结果如下"
    "（evidence 数组，与 rag_query 的返回同形）：\n"
)

# 有命中时拼在 system 末尾的那一句。
#
# `RAG_SOURCE_PROMPT` 已经写明"与主题相关的原文片段由系统检索后附在输入末尾"，
# 这一句仍要单独留着：它把"这次已经查过了"说成一句明确的话。少了它，模型一边看着
# evidence 一边觉得"我还没检索过"，会再查一遍同一件事——成本是一次 embedding +
# 一轮对话，回报是零（同一份清单、同一个问法，回来的还是那几条）。
#
# 位置与 `BRANCH_EVIDENCE_RULES` 同理，拼在基础提示词与思维限定之后：它只描述
# 这次的输入形状，不改树规则。
GENERATE_EVIDENCE_RULES = (
    "\n本次的 user 消息末尾已经附上「对主题做过的一次检索结果」（evidence 数组）："
    "这一次不必再检索同一件事，直接用它的片段写内容与引用。\n"
)


@dataclass(frozen=True)
class _TopicEvidence:
    """`rag` 形态下生成前那一次主题检索的产物。

    `text` 为空串 = "没有能交给模型的证据"，这时 `failure` 区分两种原因：非空是
    **检索本身出错**（换个配置或重试可能就有了），非空之外的 None 是**材料里确实
    没命中**（问法能改，材料不会变）。两者给用户的文案必须分开——把"这次没查成"
    说成"材料里没有"是谎报，反过来则是让人白等一次重试。
    """

    hits: list[EvidenceHit]
    payload: list[dict[str, Any]]
    spans: tuple[EvidenceSpan, ...]
    text: str
    elapsed: float
    failure: str | None = None


async def _retrieve_topic(sources: list[Source], topic: str) -> _TopicEvidence:
    """`rag` 形态生成前的**一次**主题级检索：先把材料里与主题有关的原文拿到手。

    为什么必须有这一步：`_source_prompt_input` 在 `rag` 形态下只拼材料清单（标题、
    字数、类型），而 `mindmap_generate` 自己**没有检索入口**——模型的 `rag_query`
    命中留在对话里，进不了这次生成的 user 消息。于是模型手里一个字都没有：旧契约
    下它会照 `RAG_SOURCE_PROMPT` 说"检索不到就不要写"交出一条"材料中没有相关内容：
    未检索到任何原文依据"，被 `MIN_BRANCHES=3` 判成 `invalid_map`；新契约下它倒是
    敢写了，但一个字的原文都没见过，"标明出处"就无从谈起——所以这一次预检索不是
    可选项，它是引用能成立的前提。

    实测：同一主题、同一份材料，`full` 形态给 5 条分支，加了这一步之前 `rag` 形态
    只给 1 条"没检索到"——差别不在模型，在输入。新契约上线后提示词侧还额外删掉了
    "让模型自己调 `rag_query`"那条要求（重复检索拿不到新东西，见 `RAG_SOURCE_PROMPT`
    的注释），命中只有这一个入口。

    检索栈与 `_rag` 是同一套（`search_parent`：子块匹配、父块返回、`MINDMAP_RERANK`
    打开时带重排），所以命中形状不需要第二套定义；渲染也复用 `_citable_hits`，
    模型看到的 `evidence_*` 与 `rag_query` 返回的逐字段相同——两套形状会让它按一种
    抄、被另一种校验。

    只对真有正文的 `sources` 做：调用方（`_generate`）已经滤掉空来源，这里不重复
    过滤——两处各滤一次，口径迟早分叉。

    永不抛：检索失败只降级（退回"无来源"生成），不该废掉整次生成。失败与空命中
    靠 `failure` 分开（见 `_TopicEvidence`）。

    耗时单独记一行日志：这是用户点"生成"之后**多出来的**一次等待（embedding 一次
    往返 + 重排 0.6–0.9 秒），混在总耗时里就永远看不出"慢在哪"。
    """
    query = (topic or "").strip()
    started = time.perf_counter()
    try:
        # 与 `_search_branch` 同理：`search_parent` 是同步函数，里面有一次 embedding
        # 网络往返。直接在协程里调用会把整个事件循环按住，同一个进程里别人的流式
        # 响应跟着卡住。
        hits = await asyncio.to_thread(
            search_parent, sources, query, GENERATE_RETRIEVAL_TOP_K)
    except Exception as exc:  # noqa: BLE001 — 检索失败只降级，理由见 docstring
        elapsed = time.perf_counter() - started
        reason = _reason_text(exc)
        logger.warning("mindmap_generate 主题预检索失败（top_k=%d，耗时 %.2fs）：%s",
                       GENERATE_RETRIEVAL_TOP_K, elapsed, reason)
        return _TopicEvidence((), (), (), "", elapsed, reason)

    elapsed = time.perf_counter() - started
    payload = _citable_hits(hits, sources)
    logger.info(
        "mindmap_generate 主题预检索：%d 条命中（top_k=%d），耗时 %.2fs%s",
        len(payload), GENERATE_RETRIEVAL_TOP_K, elapsed,
        "" if payload else "；没有命中，这次按无来源形态生成",
    )
    return _TopicEvidence(
        hits=hits,
        payload=payload,
        spans=tuple(
            EvidenceSpan(hit.source_id, hit.start, hit.end, hit.leaf_start, hit.leaf_end)
            for hit in hits
        ),
        text=(GENERATE_EVIDENCE_HEADER + json.dumps(payload, ensure_ascii=False)
              if payload else ""),
        elapsed=elapsed,
    )


# ---------------------------------------------------------------- 工具实现


async def _generate(args: GenerateArgs, ctx: RunContext) -> ToolResult:
    if ctx.request.intent != "create":
        return _fail("wrong_intent", "只有新建导图时才能调用这个工具；修改现有导图请用 node_expand 或 mindmap_edit。")
    if ctx.candidate is not None and ctx.candidate.root.children:
        return _fail("map_exists", "已经有一张导图了，请用 node_expand 或 mindmap_edit 修改它。")

    # 只喂真的有正文的来源：`status` 是给界面看的，模型能用的是 text。
    # 不过滤的话，一个还没解析成功的空来源会让 validate_generated_content
    # 重建 source_usage 时撞上 char_count >= 1，把整次生成判成失败——
    # 用户只是多勾了一个空文件，却得到"模型返回的导图不合法"。
    sources = [source for source in _pick_sources(ctx, []) if (source.text or "").strip()]
    # 额度在调用**之前**占：请求一旦发出就是一次真实计费，哪怕它随后抛错。
    # 只在成功后记账会让预算在工具执行期间被超支使用（预算 1 实测跑出 4 次）。
    if not ctx.reserve_model_call():
        return _fail("budget_exhausted", "本次运行的模型调用额度已用完，无法生成导图。")
    ctx.extra_model_calls += 1
    # `rag` 形态下这一次主题检索的产物；`full` 形态与无来源那条路恒为 None。
    topic_evidence: _TopicEvidence | None = None
    try:
        # 思维模型只改「怎么拆」，不改「拆什么」：拼在基础提示词之后、来源输入
        # 之前（来源在 user 消息里）。有来源时要多带一句限定，否则「必须举反例」
        # 这类要求会被当成展开许可，模型开始补写材料里没有的内容。
        thinking = thinking_block(ctx.request.thinking_model)
        # 无消费者时连 on_delta=None 都不传，保留原来的一次性调用约定。
        # 回调失败的隔离与流式退回仍交给 chat_async，预览不参与候选树校验。
        stream_kwargs = {}
        if ctx.emit is not None:
            stream_kwargs["on_delta"] = _GenerateGrowStream(ctx).feed
        if sources:
            # 两种形态的提示词不能混用（理由见 `RAG_SOURCE_PROMPT` 的注释）：
            # `rag` 下输入里没有正文，`full` 下的提示词却在教模型怎么读 sources 数组。
            # 判定放在**拼 system 的地方**而不是 `_source_prompt_input` 内部：
            # 那一层只负责"给点什么"，提示词与输入必须是同一次判定的产物，
            # 否则一次请求里两次读环境变量，理论上能拼出 A 的提示词配 B 的输入。
            if source_mode() == SOURCE_MODE_RAG:
                # `rag` 下清单里一个字的正文都没有，而这个工具**自己不做检索**：
                # 不在这里补一次，模型手里就一个字都没有——按旧契约它会交出一条
                # "材料中没有相关内容"的分支（详见 `_retrieve_topic`）。
                topic_evidence = await _retrieve_topic(sources, args.topic)
                # 命中并进本次运行的证据集：收尾的 `verify_source_refs` 要按它判
                # "这条引用落在可见的父块里吗"。与 `_rag` 累积的是同一份东西，
                # 两条路各记一份的话，"校验依据"会与"模型看到的证据"分叉。
                ctx.record_evidence(topic_evidence.spans)
                if topic_evidence.text:
                    system = (RAG_SOURCE_PROMPT + thinking + RAG_THINKING_GUARD
                              + GENERATE_EVIDENCE_RULES)
                    raw = await chat_async(
                        system,
                        _source_prompt_input(
                            sources, system=system, retrieval_hint=False
                        ) + topic_evidence.text,
                        max_tokens=_max_tokens(ctx), **stream_kwargs,
                    )
                else:
                    # 检索为空/出错 → **退回无来源形态**（用户至少拿到一张能用的图）。
                    #
                    # 另一条路是"仍然按 rag 提示词生成"：那样模型会说"没检索到"，
                    # 一条分支、`invalid_map`——就是这次的 bug。所以走这条：
                    # `DETAIL_PROMPT` 是"按主题展开框架"的提示词，它不承诺材料依据，
                    # 也不教模型写引用。输入只给主题，不给清单：清单里那句"你看不到
                    # 全文"会让模型以为还有别的东西没拿到，反而不敢写。
                    #
                    # 校验仍然带着来源与证据（不在这里把 `sources` 判成 None）：
                    # 模型在**本次运行的前几轮**可能已经 `rag_query` 到过真实片段，
                    # 它据此写出的引用是合法的，不该因为这一次主题检索没命中就全删。
                    # 逐字校验照跑，留下的只可能是真依据——"没有引用"是可见的
                    # （聚合诊断 + `ToolResult.content`），"假装有依据"不可能。
                    system = DETAIL_PROMPT + thinking + DETAIL_NO_SOURCE_RULES
                    raw = await chat_async(
                        system, args.topic, max_tokens=_max_tokens(ctx), **stream_kwargs,
                    )
            else:
                # 有来源时必须给结构化输入：模型要靠 source_id 写引用。
                system = SOURCED_DETAIL_PROMPT + thinking + SOURCED_THINKING_GUARD
                raw = await chat_async(
                    system, _source_prompt_input(sources, system=system),
                    max_tokens=_max_tokens(ctx), **stream_kwargs,
                )
        else:
            raw = await chat_async(
                DETAIL_PROMPT + thinking + DETAIL_NO_SOURCE_RULES, args.topic,
                max_tokens=_max_tokens(ctx), **stream_kwargs,
            )
    except GenerationError as exc:
        return _fail("generation_failed", f"生成失败：{exc}")

    # 退化成"只凭主题"的那一版：请求带了材料，但这次一条依据都没拿到。
    unsourced = bool(sources) and topic_evidence is not None and not topic_evidence.text

    try:
        # `evidence` 只在这条链、且只在 `rag` 形态下交出去。
        #
        # 为什么按形态门控：`full` 下材料正文整份在 prompt 里，模型引用一段"这次
        # 检索没命中"的原文是合法且常见的——`full` 形态本来就不该有"你只能引命中
        # 的那一段"这条约束。不门控的话，`full` + 父块开会把落在父块中非命中段里的
        # 合法引用判成越界并剔除（`remove_invalid=True`），而用户看不出原因。
        #
        # 形态与"检索回什么粒度"无关（`_rag` 里不读 `source_mode()`），两件事各管
        # 各的：粒度是检索的事，约束是 prompt 形态的事。
        evidence = (ctx.evidence or None) if source_mode() == SOURCE_MODE_RAG else None
        # 为空（没检索过、或检索全空）时是 None：`verify_source_refs` 走的是加这个
        # 参数之前的原路，两条路的差异只有"给了非空证据"这一种
        # （`tests/test_evidence_wiring.py` 里"空证据等价无证据"钉着逐字节不变）。
        #
        # `sources` 用 `_verify_sources(ctx)` 而不是提示词里那批：库命中的引用指向
        # 持久 id，只拿 `request.sources` 去校验会把它判成"来源不存在"并静默剔除
        # （理由见 `_verify_sources`）。开关关着、或这次没查过库时，两者逐元素相同。
        verify_sources = _verify_sources(ctx)
        content, diagnostics = validate_generated_content(
            raw, sources=verify_sources or None, evidence=evidence)
    except Exception as exc:  # noqa: BLE001 — 校验失败的原因就是要回给模型的文本
        return _fail("invalid_map", f"模型返回的导图不合法：{exc}")

    if unsourced:
        # 退化路径上"每个节点都没有引用"是**一件事**，只是按节点数重复了几十遍；
        # 逐条留着会把别的诊断淹掉（分片那条路同样的处理，见 `_generate_deep`）。
        # 聚成一条放在最后，明说这张图没有材料依据——用户要能一眼看出"为什么没有引用"。
        diagnostics = [item for item in diagnostics if item.code != "UNSOURCED_NODE"]

    # 初次规模：整树生成没有"层"的概念，只能守节点总数。
    #
    # 为什么是**裁剪**而不是判失败：这一次调用已经花掉了，模型给的树里前 80 个
    # 节点是一棵合法的、父在子前的树——裁掉尾部之后用户仍得到一张能用的图；判失败
    # 就什么都没有，而且下一轮大概率还是给这么多。代价是尾部那几个分支会整支消失，
    # 所以它必须作为诊断说出来（`TREE_CLIPPED_TO_INITIAL`），不能静默。
    # 模型输出被长度上限截断时（`SalvagedOutput`），回收出来的节点已经在树里了，
    # 但"这次少了一块"必须让用户看见——否则 `ok=true` 与完整成功长得一模一样。
    _note_truncation(diagnostics, raw)

    before_clip = _count_nodes(content.root)
    clipped = 0
    cut_ids: set[str] = set()
    if before_clip > INITIAL_MAX_NODES:
        original_root = content.root
        content, clipped = _clip_tree(content, INITIAL_MAX_NODES)
        cut_ids = _shortened_ids(original_root, content.root)
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="TREE_CLIPPED_TO_INITIAL",
            message=(
                f"模型返回了 {before_clip} 个节点，超过初次生成的 {INITIAL_MAX_NODES} 个上限，"
                f"已保留前 {INITIAL_MAX_NODES} 个（尾部 {clipped} 个未保留）；"
                "想要更大的树，请生成后用 node_expand 逐支展开"
            ),
        ))

    # `expandable` 三态：被裁掉子节点的那些是**没长完**（`unknown`），不是"到底了"。
    # 这个区别在这里最实：裁剪只会发生在初次生成，而用户下一步就是"点开某个节点
    # 继续展开"——把被裁的那几个标成 `no`，界面上恰好是唯一能再长的地方灰着。
    _stamp_expandable(content, {node_id: "unknown" for node_id in cut_ids},
                      max_depth=mindmap_contract.MAX_TREE_DEPTH)

    # 结构与语义两层复核，原先只长在 `pipeline/service.run()` 的收尾里：
    #   - `check()` 只看树本身（单子节点、深度上限、同级重名），没有来源也能跑，
    #     所以对一句话主题的生成同样有效；
    #   - `annotate_nli()` 要对已通过前两层的引用与原文做语义蕴含打分，没有来源
    #     就无从比对。未启用时它立刻返回空列表，连 onnxruntime 都不 import，
    #     照搬即可，不必在调用处再判一次开关。
    # 诊断只是提示，不影响这棵树能不能用——契约校验在上一步就决定过了。
    diagnostics.extend(check(content))
    if verify_sources:
        diagnostics.extend(annotate_nli(content, verify_sources))

    if unsourced:
        reason = (f"检索出错（{topic_evidence.failure}）" if topic_evidence.failure
                  else "材料里没有查到与这个主题相关的原文片段")
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="GENERATION_WITHOUT_EVIDENCE",
            message=(
                f"{reason}：这张图只基于主题「{args.topic}」生成，"
                "没有任何材料依据，节点上也不会有引用"
            ),
        ))

    ctx.candidate = content
    ctx.revision += 1
    count = _count_nodes(content.root)
    summary = f"已生成导图「{content.title}」，共 {count} 个节点。"
    if unsourced:
        # 一句话必须写进 `content`：这是模型唯一会读回去的地方，也是遥测/日志里
        # 唯一的"这次没依据"信号。写进 data 不够——用户看的是这句话。
        summary += (
            f"{reason}，这张图只基于主题生成，不含任何材料依据；"
            "要一张有依据的图，请换一个更贴近材料内容的说法后重试。"
        )
    data: dict[str, Any] = {
        "root_id": content.root.id,
        "node_count": count,
        "revision": ctx.revision,
        "diagnostics": [item.model_dump() for item in diagnostics],
    }
    if topic_evidence is not None:
        # 只加在 `rag` + 有材料这条路上：这是本次改动唯一多出来的等待，总耗时里
        # 看不出来"慢在哪"，得把这一次检索单独报出来（与那条 info 日志同源）。
        data["retrieval"] = {
            "hits": len(topic_evidence.payload),
            "top_k": GENERATE_RETRIEVAL_TOP_K,
            "seconds": round(topic_evidence.elapsed, 3),
            "failed": topic_evidence.failure,
        }
    return ToolResult(ok=True, content=summary, data=data)


async def _search_branch(
    sources: list[Source], title: str, intents: Sequence[str] = (),
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
    """
    query = _branch_query(title)
    queries = _branch_queries(title, intents)
    try:
        if len(queries) <= 1:
            return await asyncio.to_thread(
                search_parent, sources, query, DEEP_BRANCH_TOP_K)
        return await search_multi(sources, queries, DEEP_BRANCH_TOP_K)
    except Exception as exc:  # noqa: BLE001 — 检索失败只降级，不该废掉整张图
        logger.warning("分支检索失败（%s / %d 路）：%s: %s",
                       title, len(queries), type(exc).__name__, str(exc)[:160])
        return None


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
    （`mindmap_generate` 在 `rag` 形态下拿到的就是 top_k=5），不是"缩水版降级"。
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
        return await asyncio.to_thread(search_parent, sources, text, DEEP_BRANCH_TOP_K)
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

    **`rag` 形态下骨架之后、并发展开之前，每条一级分支各检索一次**（分片 + 按支
    检索，`docs/RAG-DECISIONS.md` 第 5 节）：query 是**分支标题本身**（不含主题，
    理由与三组消融数字见 `_search_branch`），走 `search_parent`（父块返回，默认开），
    `top_k=DEEP_BRANCH_TOP_K`。这个工具读来源有两处，形态都不是全文：骨架那一次
    读的是**标题行**（`_skeleton_outline`，原文的子集），展开读的是**检索结果**。

    为什么必须按支检索：`rag` 形态下模型看不到正文，整树一次生成 = **整棵树只有
    一次检索机会**。`top_k=5` 对八千字材料约覆盖 25%、对三万字约 8%，于是"没想到
    要问的章节"会静默缺失——用户拿到一份看起来正常但缺了几块的导图。分片之后
    覆盖度由**骨架**决定，不由模型的查询创意决定。

    三条硬约束（违反任意一条，这个能力就变成负资产）：
      1. **不占 `max_tool_calls`**：检索发生在工具内部，是服务端自己的并发检索，
         模型看到的仍然只是一次 `mindmap_generate_deep` 调用。
      2. **成本有闸**：`top_k=3` × N 支，N 上限 `DEEP_MAX_BRANCHES=20`，块向量按
         文本哈希缓存，实测口径见 `DEEP_BRANCH_TOP_K` 的注释。
      3. **降级彻底**：没配 embedding 时纯 BM25 照常；某支抛错只让那一支无证据
         展开；**所有支都拿不到证据**时退回"整树一次检索"（`DEEP_FALLBACK_TOP_K`
         条，与 `mindmap_generate` 同量），绝不因为检索失败交出一棵空树。

    `full` 形态**不做**这些检索（下面的 `slicing` 只在 `rag` 下为真）：正文整份
    已经在 prompt 里，分片检索是白花；注册表那边也不会在有来源时给出这个工具
    （`build_registry` 按形态决定），这里判一次是给"绕过注册表直接调"兜底。
    """
    if ctx.request.intent != "create":
        return _fail("wrong_intent", "只有新建导图时才能调用这个工具；修改现有导图请用 node_expand 或 mindmap_edit。")
    if ctx.candidate is not None and ctx.candidate.root.children:
        return _fail("map_exists", "已经有一张导图了，请用 node_expand 或 mindmap_edit 修改它。")

    # 分片检索的两个前提：真的有可检索的正文，且当前是 `rag` 形态。
    # 空正文的来源不参与（`status` 是给界面看的，能检索的只有 `text`）。
    sources = [source for source in _pick_sources(ctx, []) if (source.text or "").strip()]
    slicing = bool(sources) and source_mode() == SOURCE_MODE_RAG

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
            raw = await chat_async(system, user, max_tokens=_max_tokens(ctx))
        else:
            stream = _GrowStream(ctx, reported_grow, parent, base_index, quota=quota)
            raw = await chat_async(
                system, user, max_tokens=_max_tokens(ctx), on_delta=stream.feed,
            )
        # 截断在这里统一记账：四个调用点（骨架 / 分支 / 补点 / 深层）都从这里出去，
        # 各判一次的结果是"某一条路截断了但没人说"。判定只看返回值类型，不改形状
        # ——`raw` 仍是一个 dict，往下的 `_candidate_slots` / `_skeleton_titles`
        # 一行都不用改。
        _note_truncation(diagnostics, raw)
        return raw

    # 第一步：出骨架。额度在调用**之前**占——请求一旦发出就是一次真实计费。
    #
    # `rag` 形态下这一次调用会带上**材料自带的章节**（`_skeleton_outline`）：正文
    # 仍然不进 prompt，进的是每份材料自己的标题行（原文的子集，见
    # `_material_inputs` 的 docstring）。为什么放在骨架这一次而不是展开：分片之后
    # **覆盖度由骨架决定**——模型凭空想 8 支时，材料里它没想到的章节就再也不会有
    # 人问；给它一份目录，它至少知道"这份材料有这几块"。实测数字见
    # `scripts/measure_outline_skeleton.py`。
    #
    # `full` 形态**不拼**：正文整份已经在 prompt 里，模型自己就能读到标题，
    # 目录是同一份内容的第二份拷贝——白花 token，还会诱导它把目录当全文提纲、
    # 忽略正文里的细节。所以这个开关跟着 `slicing` 走，不单独判一次形态。
    if not ctx.reserve_model_call():
        return _fail("budget_exhausted", "本次运行的模型调用额度已用完，连骨架都出不了，无法分片生成。")
    ctx.extra_model_calls += 1
    outline = _skeleton_outline(sources) if slicing else ""
    try:
        raw_plan = await ask_model(
            PLAN_PROMPT, _plan_ask(args.topic, args.branches, outline=outline))
    except GenerationError as exc:
        return _fail("generation_failed", f"骨架生成失败：{exc}")

    titles = _skeleton_titles(raw_plan)
    if len(titles) < MIN_BRANCHES:
        return _fail(
            "too_few_branches",
            f"模型只给了 {len(titles)} 条一级分支，少于 {MIN_BRANCHES} 条，无法成图。",
            branches=len(titles),
        )
    # 一级分支的条数上限与参数模型同一个常量：模型偶尔会无视"最多 N 条"，
    # 这时最多留 20 条，再多的既不在一级分支的可读区间里，也挤占下层预算。
    titles = titles[:min(MAX_CHILDREN, DEEP_MAX_BRANCHES)]
    # 数量只做宽容校验（允许 ±1）。为了一两条差异整张重来，成本是又一轮全额调用，
    # 而多一条少一条对这张图并不致命；即使超出宽容范围也用实际返回的继续，
    # 只记一条诊断说明"没按要求的条数走"。
    if not args.branches - 1 <= len(titles) <= args.branches + 1:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="SKELETON_SIZE_OFF",
            message=f"请求 {args.branches} 条一级分支，模型给了 {len(titles)} 条，已按实际条数继续。",
        ))

    # 分支节点的 id 在骨架阶段就定好：展开是并发跑的，合并时要按 id 把要点挂回去，
    # 而树这时还不存在——只能先分配 id，合并时一次性落进树里。
    taken: set[str] = set()
    root_id = new_node_id(taken)
    taken.add(root_id)
    skeleton: list[tuple[str, str]] = []
    for title in titles:
        branch_id = new_node_id(taken)
        taken.add(branch_id)
        skeleton.append((title, branch_id))

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
    retrieval_failed: list[int] = []
    retrieval_empty: list[int] = []
    # 分支标题 → 同义问法。`full` 形态不做按支检索，所以这里恒为空——它同时是
    # `retrieval.expanded_branches` 的来源，两处共用一个变量才不会有半开状态。
    expansions: dict[str, list[str]] = {}
    if slicing:
        branch_titles = [title for title, _ in skeleton]
        # 查询扩展：每支从"一条标题"变成"标题 + 至多 2 条同义问法"。`expansions`
        # 为空是**常态**（开关默认关、单支时的成本闸、或模型降级），那时下面两行
        # 与这个能力上线之前逐字节相同。
        #
        # 位置必须在这里：扩展的输入是分支标题，而分支标题要等骨架落定；输出要
        # 赶在 `_retrieve_branches` 之前，否则检索已经按标题跑完了。
        expansions = await _expand_branches(sources, branch_titles)
        retrieval_queries = [
            query for title in branch_titles
            for query in _branch_queries(_branch_query(title), expansions.get(title, ()))
        ]
        hits_by_branch = await _retrieve_branches(
            sources, branch_titles, expansions=expansions)
        retrieval_mode = "branch"
        retrieval_failed = [
            index for index, hits in enumerate(hits_by_branch) if hits is None]
        # 没有任何一支拿到证据（全抛错、全空，或两者混合）→ 退回整树一次检索。
        # 不退回的话，每支都会带着"没有证据就留空"的提示词展开，产出一棵**空树**
        # ——而"检索失败"不该等于"材料里没有"。这正是降级要彻底的那一条。
        if all(not hits for hits in hits_by_branch):
            whole = await _retrieve_whole(sources, args.topic, branch_titles)
            retrieval_mode = "whole"
            retrieval_queries = [" ".join([args.topic.strip(), *branch_titles]).strip()]
            # 兜底命中要**原样给每一支**：这是"整树一次检索"的定义，每支看到的
            # 是同一批证据；分片的收益在这一刻归零，但那好过一棵空树。
            hits_by_branch = [whole for _ in hits_by_branch]
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn, code="BRANCH_RETRIEVAL_FALLBACK",
                message=(
                    f"{len(skeleton)} 条分支按支检索都没有拿到证据，已退回整树一次检索"
                    f"（{len(whole)} 条命中，全部支共用）；分片检索的覆盖收益本次没有生效"
                    if whole else
                    f"{len(skeleton)} 条分支按支检索都没有拿到证据，整树兜底检索也没命中，"
                    "各支将按「无依据」展开"
                ),
            ))
        retrieval_empty = [
            index for index, hits in enumerate(hits_by_branch)
            if hits is not None and not hits]
        for index, hits in enumerate(hits_by_branch):
            if not hits:
                continue
            evidence = _branch_evidence(hits, sources)
            branch_evidence[index] = evidence
            # 累积进 `RunContext.evidence`：去重（四元组）与 200 条上限都在那里，
            # 这里只管往里放。收尾的 `verify_source_refs` 读的也是它——两处同源，
            # 否则"校验依据"与"模型看到的证据"会是两份不同的东西。
            ctx.record_evidence(evidence.spans)
        for index in retrieval_failed:
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn, code="BRANCH_RETRIEVAL_FAILED",
                node_id=skeleton[index][1],
                message=(
                    f"分支「{skeleton[index][0]}」的检索出错，这一支按「无检索结果」展开"
                    "（其余分支不受影响）"
                ),
            ))
        for index in retrieval_empty:
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.info, code="BRANCH_NO_EVIDENCE",
                node_id=skeleton[index][1],
                message=(
                    f"分支「{skeleton[index][0]}」的检索没有命中材料里的任何片段，"
                    "这一支按自己的知识展开、不挂引用（未检索到不等于材料里没有）"
                ),
            ))

    def prompt_for(base: str, evidence: _BranchEvidence | None) -> str:
        """这一支该用哪份提示词：有证据 / 无证据（分片形态）/ 不看证据（`full`）。

        三者不能合并：`full` 形态下 `evidence is None` 是"这个工具不读来源"，
        而分片形态下 `evidence is None` 是"这一支没查到"——后一种要告诉模型
        "照常写、但不要挂引用"，前一种说这句话反而是误导（那里正文就在 prompt 里）。
        """
        if evidence is not None:
            return base + "\n" + BRANCH_EVIDENCE_RULES
        if slicing:
            return base + "\n" + BRANCH_NO_EVIDENCE_RULES
        return base

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

    async def expand_branch(title: str, branch_index: int) -> list[_Slot] | None:
        """展开一条分支。额度不够返回 None——那是"没展开"，与"展开失败"是两件事。

        **只带这一支的 hits**：`branch_evidence[branch_index]` 是这一步之前按
        「主题 + 分支标题」单独检索出来的那一批，兄弟分支的证据一个字都不进来。

        展开之后多一道判据：这一支要不要**补查一次**（`_needs_supplementary`）。
        判定与检索都在工具内部，所以仍然**不占** `max_tool_calls`——模型看到的只是
        一次 `mindmap_generate_deep` 调用。
        """
        async with semaphore:
            if not ctx.reserve_model_call():
                return None
            ctx.extra_model_calls += 1
            evidence = branch_evidence[branch_index]
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

    results = await asyncio.gather(
        *(expand_branch(title, index) for index, (title, _) in enumerate(skeleton)),
        return_exceptions=True,
    )

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
    # `tree_invalid`。`_generate` 那条路由 `validate_generated_content` 重建它，
    # 这里没有那个函数，只能自己拼——而"分片形态会产出引用"意味着不拼就一定会炸。
    #
    # 用 `_verify_sources(ctx)` 而不是局部 `sources`：这条路径的引用只可能落在
    # 请求内材料上（分片检索只跑 `sources`），但"校验认的来源集合"与"source_usage
    # 列出的集合"必须是同一个，否则将来任何一方向库扩展都会先在契约校验上炸一次，
    # 而那次失败看起来会像"模型返回的树不合法"。
    verify_sources = _verify_sources(ctx) if slicing else []
    content = MindMapContent(
        title=root_label,
        root=Node(
            id=root_id,
            label=root_label,
            children=[Node(id=branch_id, label=title, children=[]) for title, branch_id in skeleton],
        ),
        source_usage=[
            SourceUsage(source_id=source.id, kind=source.kind,
                        filename=source.filename,
                        char_count=len((source.text or "").strip()))
            for source in verify_sources
        ],
    )

    drafts: list[dict[str, Any]] = []
    # 临时 id -> 这个节点落树时的 `expandable`（判定见 `_expandable_for_slot`）。
    # 用临时 id 记是因为真实 id 由 `normalize_ops` 分配；映射在它之后才有。
    slot_states: dict[str, str] = {}

    def emit(parent_id: str, key: str, slots: list[_Slot]) -> None:
        """把候选子树摊平成 add 草案：父节点一定排在它自己的子节点前面。"""
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
                # 这里原样带上：这一层再"顺手补一条"就会把校验过的引用变成没校验过的。
                "source_refs": list(slot.source_refs),
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
        ops, id_map = normalize_ops(drafts, _node_ids(content.root))
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
    # 与 `_generate` 的差别：那条路走 `validate_generated_content`，它给出的
    # `UNSOURCED_NODE` 是逐节点一条的；分片出来的树动辄几百节点，几百条"没有
    # 引用"的提示会把真正有用的诊断淹掉，也不给用户任何可操作的信息。所以这里
    # 只留**聚合**结果，明细（按 code 计数）进 data。
    refs_kept = refs_dropped = 0
    refs_by_code: dict[str, int] = {}
    if slicing:
        before = _count_refs(content.root)
        raw_reports = verify_source_refs(
            content, verify_sources, remove_invalid=True, evidence=ctx.evidence or None)
        refs_kept = _count_refs(content.root)
        refs_dropped = before - refs_kept
        for item in raw_reports:
            if item.code in ("UNSOURCED_NODE", "SOURCE_PAGE_UNVERIFIED"):
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

    ctx.candidate = content
    ctx.revision += 1
    count = _count_nodes(content.root)

    content_parts = [f"已生成导图「{content.title}」，共 {count} 个节点。"]
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
    # 检索的事必须出现在 content 里：`rag` 形态下用户拿到的树有没有材料依据，
    # 全看这一步。只说"共 N 个节点"会让一棵"20 支里有 8 支没查到依据"的树
    # 与一棵全都查到证据的树看起来一模一样。
    if slicing:
        with_evidence = sum(1 for item in branch_evidence if item is not None)
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
    content_parts.append(
        f"按 {max_depth} 层预算展开，实际 {audit.deepest + 1} 层（含中心主题），各层节点数："
        f"{' / '.join(str(audit.depths.get(depth, 0)) for depth in range(audit.deepest + 1))}。"
    )

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
            "diagnostics": [item.model_dump() for item in diagnostics],
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
    content: MindMapContent, stamps: dict[str, str], *, max_depth: int,
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
    for node, depth in _walk_depths(content.root):
        node.expandable = "no" if depth >= max_depth else stamps.get(node.id, "yes")


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
        for other in _iter_nodes(ctx.candidate.root):
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

    `top_k` 用单支口径（`DEEP_BRANCH_TOP_K = 3`）：一条父块 450–700 字，三条足够
    一次 2–4 个节点的展开，再多只是把同一段材料换个开头。

    **不占 `max_tool_calls`**：检索发生在工具内部（与 `_generate_deep` 的按支检索
    同一条规矩），模型看到的仍然只有一次 `node_expand` 调用。这不只是计费口径：
    `max_tool_calls` 默认 8，把它算进去时一次"先查再展开"就吃掉两次，而用户点的
    是"展开这一支"，他并不知道自己在花工具额度。

    用 `search_parent` 而不是 `search_sources`：它同时给出父块区间与命中的子块区间，
    引用才判得出"落在这段命中的原文里吗"（`_ref_from` 与
    `verify_source_refs(evidence=)` 都靠这两级坐标）。
    """
    query = _expand_query(chain)
    if not query:
        return None, {"query": "", "hits": 0, "skipped": "空查询"}
    hits = search_parent(sources, query, DEEP_BRANCH_TOP_K,
                         library=library_search_enabled())
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

    node = find_node(content.root, args.node_id)
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
    used = _count_nodes(content.root)
    room = node_limit - used
    wanted = min(args.count, room)
    if wanted < 2:
        return _fail(
            "tree_full",
            f"这张图已经有 {used} 个节点，接近上限 {node_limit}，放不下这一批新节点"
            f"（剩余 {max(0, room)} 个）。请先删掉一些节点再展开。",
            node_count=used, node_limit=node_limit,
        )
    depth = _depth_of(content.root, node.id)
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
    chain = _node_path(content.root, node.id) or [node]
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
        raw = await chat_async(system, ask, max_tokens=_max_tokens(ctx))
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

    ops, id_map = normalize_ops(drafts, _node_ids(content.root))
    batch = OpBatch(batch_id=_batch_id(ctx), base_revision=ctx.revision, ops=ops)
    try:
        updated = apply_ops(base, batch)
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
    expanded_node = find_node(updated.root, node.id)
    stamps[node.id] = "no" if (expanded_node is not None
                              and len(expanded_node.children) >= MAX_CHILDREN) else "yes"
    _stamp_expandable(updated, stamps, max_depth=mindmap_contract.MAX_TREE_DEPTH)

    # 报给界面的必须是**树上真实的取值**，不是上面那张期望表：深度覆盖规则（第 8 层
    # 一律 `no`）就发生在 `_stamp_expandable` 里，直接回期望表会给出一个"按钮可点、
    # 点下去却报 too_deep"的分叉。
    expandable: dict[str, str] = {}
    for node_id in stamps:
        found = find_node(updated.root, node_id)
        if found is not None:
            expandable[node_id] = found.expandable

    ctx.candidate = updated
    ctx.batches.append(batch)
    ctx.revision += 1

    # ---- 生长预览：落在**被展开的那个节点**下面 ----
    #
    # `path` 是父节点的子索引链，而这里的父节点就是被展开的节点本身。写成 `[]`
    # 会把新节点画到根下面，界面上看起来像"根上又长出三个分支"，而用户点的是
    # 第三层的某个叶子。算不出来就不发帧（宁可没有预览，也不给一个错位置）。
    index_path = _index_path(content.root, node.id)
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
    elif sources and refs_kept == 0:
        # 有命中但一条都没留下：模型没写引用，或写了但过不了逐字校验。这两种在
        # 界面上是同一件事（节点没有出处），区别留在 `data.refs` 里。
        summary += "模型这次没有写出可核验的引用，新节点没有 source_refs。"

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
            "node_count": _count_nodes(updated.root),
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

    **这是 `rag` 形态下主动把正文塞进 LLM prompt 的两处之一**（另一处是
    `_skeleton_outline` 拼进骨架的章节标题；检索命中不算：那就是检索的产出）。
    它的边界很小且是刻意的——只在前 200 字、只在 `MINDMAP_QUERY_EXPANSION` 打开
    时才走（默认关），而 planner 判断"该用哪些词、范围有多大"本来就需要看一眼
    材料长什么样，只给标题它只能靠猜。

    所以"`rag` 形态下正文不出进程"这句话**不成立**，准确的四种说法：
      - 进 prompt：只有清单（生成/展开/改树）+ 检索片段（`rag_query` 的返回）
        + 这里的前 200 字（默认关）+ `mindmap_generate_deep` 骨架那一次的**标题行**
        （`_skeleton_outline`，原文的子集，一条最多 40 字）；
      - 离开进程但不进 prompt：混合检索的 embedding 请求（每个参与打分的块发一次，
        按 `sha256(quote)` 缓存），两种形态都一样，与这个开关无关；
      - `full` 形态额外把每份正文整份放进 prompt。
      - 三种形态共同点：**结构线索（标题行）会进，正文段不进**。目录不改变这一点，
        它只是把"标题行"这条边从"顺带"变成了"明确的一条输入"。

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

    正文非空白这条也不是洁癖：`validate_generated_content` 会用这个列表重建
    `source_usage`，而契约要求 `char_count >= 1`——把一份还没解析成功的空来源
    带进去，整次生成会被判 `invalid_tree`，用户只是多勾了一个空文件却得到
    "模型返回的导图不合法"。`_generate` 里本来就有同一道过滤（只喂真有正文的
    来源），两处共用这一个函数之后，口径不会再分叉。
    """
    return [
        source for source in
        (*ctx.request.sources, *ctx.library_sources.values())
        # `status` 那一条与 `_reference_problem` 对齐，别按"反正现在都是 parsed"
        # 省略掉：口径分叉的代价是一类没有日志的静默剔除（见 docstring）。
        if source.status == "parsed" and (source.text or "").strip()
    ]


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
    # 理论上就可能拼出来——与 `_generate` 里"提示词与输入必须出自同一次形态判定"
    # 是同一条规矩。
    library_on = library_search_enabled()
    # 开关关着时**一行都不往下走**：与 `07a2740` 逐字节相同的保证就在这里，
    # 包括"无来源直接 no_sources"这个早退（关掉库之后再走一遍检索是白花）。
    if not sources and not library_on:
        return _fail("no_sources", "这次请求没有附带任何资料，无法检索。")

    filters = _rag_filters(args.filters)
    # 只在开关打开时问一次库的大小：这既是"库空且请求无材料"那条判定的依据，
    # 也是空结果话术要用的数字。开关关着时它恒为 0，且一次库查询都不发生。
    library_total = library_source_count() if library_on else 0

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
                                  hyde=plan.hyde, library=library_on)
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
                             library=library_on)
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
    return ToolResult(
        ok=True,
        content=json.dumps(_citable_hits(hits, citable_sources), ensure_ascii=False),
        data=data,
    )


async def _edit(args: EditArgs, ctx: RunContext) -> ToolResult:
    content = _require_candidate(ctx)
    if isinstance(content, ToolResult):
        return content

    # 纵深防御：注册表已经按意图裁剪过，这里再按"用户选中了哪些节点"收窄一次。
    # 注册表配错时这条仍然拦得住，被测过（expand 意图下删除选中节点之外的分支）。
    scope_error = _selection_scope_error(content, args, ctx)
    if scope_error is not None:
        return scope_error

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
        if item.changes is not None:
            draft["changes"] = {"label": item.changes.label}
        elif item.op == "update" and item.label is not None:
            # 实测模型会把 label 直接写在 op 顶层而不是 changes 里。它被拒之后
            # 下一轮能自己改对（错误反馈是有效的），但那要多花一次模型调用和
            # 几秒等待。两种写法都能表达同一件事，就在入口吸收掉。
            draft["changes"] = {"label": item.label}

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
                parent = None if parent_id.startswith("$") else find_node(content.root, parent_id)
                base = len(parent.children) if parent else 0
                draft["index"] = base + pending.get(parent_id, 0)
                pending[parent_id] = pending.get(parent_id, 0) + 1
            draft.setdefault("source_refs", [])
        if item.op == "move" and "index" not in draft:
            draft["index"] = 0

        drafts.append(draft)

    try:
        ops, id_map = normalize_ops(drafts, _node_ids(content.root))
    except ValidationError as exc:
        detail = "；".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:5]
        )
        return _fail("invalid_ops", f"操作列表不合法：{detail}")

    batch = OpBatch(batch_id=_batch_id(ctx), base_revision=ctx.revision, ops=ops)
    try:
        updated = apply_ops(content, batch)
    except OpError as exc:
        # 把执行器的错误原样回给模型：它知道是第几条、哪个字段错了才能自己改对
        return _fail(exc.code, f"{exc.path}：{exc.message}", path=exc.path)

    changed = {op.id for op in ops} | {
        op.new_parent_id for op in ops if op.op == "move" and op.new_parent_id
    }
    ctx.candidate = updated
    ctx.batches.append(batch)
    ctx.revision += 1
    return ToolResult(
        ok=True,
        content=f"已应用 {len(ops)} 条修改，导图现在有 {_count_nodes(updated.root)} 个节点。",
        data={
            "batch": batch,
            "batches": [batch],
            "id_map": id_map,
            "revision": ctx.revision,
            "changed_ids": sorted(changed),
        },
    )


def _batch_id(ctx: RunContext) -> str:
    return f"b{ctx.revision + 1}-{len(ctx.batches)}"


# ---------------------------------------------------------------- 注册


# 意图 -> 允许的工具。这张表就是"模型这条路能走多远"的唯一声明。
_ALLOWED_BY_INTENT: dict[str, tuple[str, ...]] = {
    "create": ("mindmap_generate", "mindmap_generate_deep"),
    "expand": ("node_expand",),
    "edit": ("mindmap_edit",),
}

# 会读来源的意图。`create` 在 `full` 形态下不在里面：整张从零生成时正文已经在
# prompt 里，再给检索入口只会让模型把"先查一下"当成规定动作（实测 10 次里 10 次
# 都先查一遍，白白多花调用）。`rag` 形态下这个判断整个反转——它看不到正文，
# 没有 `rag_query` 就一个字都写不出来，所以在 `build_registry` 里按形态补进来。
_SOURCE_AWARE_INTENTS = ("expand", "edit")

# 注册顺序固定，便于日志与测试对照；用工厂而不是共享实例，避免一个 registry
# 上的改动影响到另一个（ToolSpec 目前不可变，但这个保证不该靠约定维持）。
_TOOL_ORDER = (
    "mindmap_generate",
    "mindmap_generate_deep",
    "node_expand",
    "rag_query",
    "mindmap_edit",
)

_TOOL_SPECS: dict[str, Callable[[], ToolSpec]] = {
    "mindmap_generate": lambda: ToolSpec(
        name="mindmap_generate",
        description=(
            "一次调用生成一张完整思维导图，适合分支少、内容浅的简单主题。"
            "只有当前还没有导图时可用；复杂主题（分支多、每支都要展开几个要点）"
            "改用 mindmap_generate_deep，它更慢但更不容易被单次输出上限截断。"
        ),
        args_model=GenerateArgs,
        fn=_generate,
    ),
    "mindmap_generate_deep": lambda: ToolSpec(
        name="mindmap_generate_deep",
        description=(
            "分片生成一张完整思维导图：先定一级骨架，再按层并发展开。某一支要不要"
            "继续深挖由你在展开响应里用 expandable 声明，代码只按这个判断还问不问；"
            "默认一直问到所有支都到底、或者触到节点数与调用预算。"
            "可用 max_depth 收窄层数。最后按代码验收并只补不合格的那几支。"
            "适合复杂主题——它每次只让模型输出一支，不受单次输出上限挤压，"
            "因此比 mindmap_generate 更稳。"
            "简单主题用 mindmap_generate（一次调用就够，更快）。"
            # 这句必须与 `_generate_deep` 的实际行为一致：它现在**会**检索
            # （rag 形态下每支一次，服务端在工具内部做），也**会**在骨架那一次
            # 看材料自带的章节标题。写成"不读来源"会让模型在带材料的请求里选错
            # 工具，也会让读描述判断行为的人判断错。
            "带材料时它会先看一眼材料自带的章节标题来定一级骨架，再为每条一级分支"
            "各自检索一次并只让那一支看到自己的检索结果，"
            "所以带材料的复杂主题同样可以用它；它不会把全文读进来。"
            "只有当前还没有导图时可用。"
        ),
        args_model=GenerateDeepArgs,
        fn=_generate_deep,
    ),
    "node_expand": lambda: ToolSpec(
        name="node_expand",
        description=(
            "把当前选中的那个节点展开成 2–4 个子节点，只新增这一层，不改动其他节点。"
            "模型会看到从根到这个节点的标题链与它已有的子节点，所以写出来的内容要落在"
            "这一支的范围里，并且不能与已有子节点重复。"
            "节点已经到底（第 8 层 / 模型说过没有更细内容）时它会被拒，理由会一起返回。"
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
    "mindmap_edit": lambda: ToolSpec(
        name="mindmap_edit",
        description="按节点 id 精确修改已有导图：增删改节点或移动子树。不要用它整张重建。",
        args_model=EditArgs,
        fn=_edit,
    ),
}


def build_registry(intent: str = "create", *, has_sources: bool = False,
                   has_library: bool | None = None) -> ToolRegistry:
    """本次可用的工具集，按意图裁剪。

    早先是「四个全注册 + 各自内部校验意图」。那样做的问题是模型看得到工具、
    却每次调用都被拒：实测 expand 意图下它坚持调 `mindmap_edit` 删节点，
    失败三次才转向，白白烧掉调用额度。看不到这个工具，它就只能走对的那条路。

    裁剪规则：
      - `create` 给 `mindmap_generate` 与 `mindmap_generate_deep` 两个，由模型
        按主题复杂度自选（描述里写清了两者差别）
      - `expand` 只给 `node_expand`，请求带来源时再加 `rag_query`
      - `edit` 给 `mindmap_edit`，请求带来源时再加 `rag_query`
      - `expand` / `edit` 在请求带来源时加 `rag_query`：那两条路不读来源，
        模型只能自己查。
      - **不**给 `create`：两条生成路都会在工具内部先检索一次（`mindmap_generate`
        做一次主题预检索、`mindmap_generate_deep` 按支检索）。早先这里是反的——
        那时整树还不预检索，不给工具模型就只能凭标题编；现在补上预检索之后，
        再给工具反而让模型连查八次把 `max_tool_calls` 用光、一次生成都没调。
      - **请求不带来源、但知识库非空**时，任何意图都加 `rag_query`（见下）。

    两个生成工具必须同时在场：只留一个，等于替用户决定了"这张图多大"。而
    分片那条路的成本是 N+1 次调用，简单主题走它纯属浪费额度——这个判断
    只有拿到主题的模型能做。

    **有来源时是否撤掉 `mindmap_generate_deep`，按形态决定**：

      - `full`：**撤掉**。正文整份已经在 prompt 里，模型自己就能扫全文；而它的
        入参里没有任何来源字段、骨架与展开都只看主题，产出的树与材料脱钩，却会
        以 ok=True 成功返回。这条不是"少给一个选项"，是"少给一条会给出错误答案
        的路"——模型看不到它，就不会选中它。带材料的新建走 `mindmap_generate`
        的 `full` 提示词（它给模型喂结构化来源、回填 source_usage 并逐字校验引用）。
      - `rag`：**保留**（这一轮放开的那一条）。`_generate_deep` 现在会为每条
        一级分支各检索一次（`docs/RAG-DECISIONS.md` 第 5 节的结构引导召回），
        每支只看到自己的命中，引用也只能落在自己那批证据上。**整树一次生成意味着
        只有一次检索机会**——`top_k=5` 对八千字材料约覆盖 25%，"没想到要问的
        章节"会静默缺失；分片之后覆盖度由骨架决定，不由模型的查询创意决定。
        在 `full` 下做这件事是白花（正文已在 prompt），所以那里的 discard 不变。

    `rag_query` 只在**真有可查的东西**时才注册：没来源也没库的检索必然返回
    「没有找到」，注册它只会诱导模型拿它当"思考"用。这里的"有"包含知识库：
    请求不带材料、库里却有 200 份时，检索是模型唯一能碰到原文的入口——不注册
    就会出现"system 里叫它先检索，工具列表里却没有这个工具"，模型于是凭标题编，
    并以 ok=True 交出一棵没有出处的树（它甚至不算说谎，只是没别的路可走）。

    `has_library=None` 表示**自己去问库**（生产调用方 `service.run` 就用这个）。
    测试与脚本要固定答案时显式传 `False / True`：否则一条用例的结论会悄悄依赖
    宿主机库里恰好存了什么。未知意图按 `create` 兜底——宁可少给工具，也不要把
    破坏性工具塞进一个没预期到的新意图里。
    """
    rag_mode = source_mode() == SOURCE_MODE_RAG
    if has_library is None:
        has_library = library_available()
    allowed = set(_ALLOWED_BY_INTENT.get(intent, _ALLOWED_BY_INTENT["create"]))
    if has_sources:
        if not rag_mode:
            allowed.discard("mindmap_generate_deep")
        # `create` 在 rag 形态下**不再**补 `rag_query`：`mindmap_generate` 与
        # `mindmap_generate_deep` 都会在工具内部先检索一次（前者主题预检索、后者
        # 按支检索），模型再自行查是纯浪费——实测它连查 8 次把 `max_tool_calls`
        # 用光，一次生成都没调，用户拿到"工具调用次数已用完"和 0 个节点。
        # `expand` / `edit` 仍需要：那两条路不读来源，模型只能自己查。
        if intent in _SOURCE_AWARE_INTENTS:
            allowed.add("rag_query")
    if has_library and not has_sources:
        allowed.add("rag_query")

    registry = ToolRegistry()
    for name in _TOOL_ORDER:
        if name in allowed:
            registry.register(_TOOL_SPECS[name]())
    return registry
