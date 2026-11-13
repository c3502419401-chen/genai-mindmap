"""工具注册表与五个业务工具。

注册表只负责分派与参数校验，业务逻辑在各个 `_xxx` 函数里。

五个工具的划分照「用户意图 + 作用域」而不是 CRUD 动词：
- `mindmap_generate` 只在没有导图时可用，一次调用产出一整棵候选树
- `mindmap_generate_deep` 同样只在没有导图时可用，但拆成「骨架 → 并发展开 →
  验收 → 局部重试」四段，复杂主题用它（理由见该函数的 docstring）。它不读任何
  来源正文，所以请求带材料时注册表根本不注册它——那条路会产出一棵与材料无关
  的树，而且成功返回
- `node_expand` 只能在已选中的节点下加子节点，不能删、不能移
- `rag_query` 只读，且只能查本次请求带来的来源
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
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.agent.contracts import RunContext, ToolCall, ToolResult, ToolSpec
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
from app.contracts.mindmap import MindMapContent, Node
from app.contracts.ops import OpBatch, OpError
from app.contracts.source import Source
from app.generation.service import (
    CONTEXT_TOKEN_LIMIT,
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
    source_mode,
    thinking_block,
    validate_generated_content,
)
from app.generation.stream_partial import collect_new_nodes, parse_partial
from app.knowledge.query_plan import _material_snippets, plan_query
from app.knowledge.service import (
    SourceFilter,
    filter_sources,
    search_multi,
    search_sources,
)
from app.optimization.service import check
from app.optimization.source_refs import annotate_nli

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

# 工具内单次模型调用的输出上限。这个数必须与 `app.agent.provider._MAX_TOKENS_CAP`
# 一致：provider 是外层轮次的出口，工具里的 `chat_async` 是另一条出口，两边不同
# 时 `RunBudget.max_output_tokens` 只约束了其中一半的调用。不去 import 那个私有
# 常量，是为了不让「工具」反向依赖「适配层」——适配层是可替换的。
MAX_OUTPUT_TOKENS_CAP = 65536

# 截断必须让模型知情，否则它会把半份材料当成全文来整理和引用。
# 校验用的 sources 始终保留完整原文，只有提示词里的副本可以裁剪。
SOURCE_TRUNCATED_HINT = "（原文过长，已截断）"


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


def _max_tokens(ctx: RunContext) -> int:
    """工具内一次模型调用的输出上限。

    不显式传 `max_tokens` 时 `chat_async` 会回落到它自己的默认值，于是
    `RunBudget.max_output_tokens` 只对 provider 那条路生效——预算里写了上限，
    工具这条更大的输出口却没人守。
    """
    return min(ctx.request.budget.max_output_tokens, MAX_OUTPUT_TOKENS_CAP)


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
    """从骨架响应里取出分支标题，剔除空项，超长截到 50 字。

    只接受字符串：`{"branches":[{"label":"x"}]}` 说明模型没照格式写，硬转字符串
    会把一整个 dict 的 repr 当成节点标题挂到树上——那种"成功"比失败更难发现。
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
        title = item.strip()
        if not title:
            continue
        titles.append(title[:50])
    return titles


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
    """

    label: str
    children: list["_Slot"] = field(default_factory=list)
    expandable: bool = True
    expandable_declared: bool = False


def _candidate_slots(raw: Any, limit: int, depth_left: int = 0) -> list[_Slot]:
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
        slot = _slot_from(item, limit, depth_left, legacy)
        if slot is not None:
            slots.append(slot)
    return slots


def _slot_from(item: Any, limit: int, depth_left: int, legacy_leaf: bool) -> _Slot | None:
    """单个候选项 → `_Slot`。空标签、非字符串/非 dict 的项一律丢掉。

    `expandable` 只有**布尔值**才算一次声明：模型写成 `"false"` 字符串、写成 0、
    或干脆没写，一律按可展开处理。缺省按 true 是有意的——模型偶尔漏写一个键，
    不该因此让整支内容凭空变成叶子，那是一种代码替它做的决定。
    """
    if isinstance(item, str):
        label = item.strip()
        if not label:
            return None
        # 裸字符串没有任何层级信息：旧 points 里它是叶子，新格式里按缺省可展开，
        # 与引入 `expandable` 之前的行为逐节点相同。
        return _Slot(label[:200], expandable=not legacy_leaf)
    if not isinstance(item, dict):
        return None
    label = item.get("label")
    if not isinstance(label, str) or not label.strip():
        return None
    flag = item.get("expandable")
    declared = isinstance(flag, bool)
    slot = _Slot(
        label.strip()[:200],
        expandable=flag if declared else True,
        expandable_declared=declared,
    )
    nested = item.get("children")
    if depth_left > 0 and isinstance(nested, list):
        for child in nested[:limit]:
            child_slot = _slot_from(child, limit, depth_left - 1, False)
            if child_slot is not None:
                slot.children.append(child_slot)
    return slot


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
    """

    ctx: RunContext
    seen: set[tuple[tuple[int, ...] | None, str]]
    parent: tuple[int, ...]     # 父节点在树里的子索引链
    base_index: int = 0         # 这一路产出的第一个子项在父节点下的位置
    buffer: str = ""
    reported: int = 0

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
        """
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


def _source_prompt_input(sources: list[Source], *, system: str = "") -> str:
    """把来源拼成模型这次的输入。**形态由 `MINDMAP_SOURCE_MODE` 决定**。

    - `rag`（默认）：只拼一份**材料清单**（标题、字数、类型），正文一个字都不进。
      模型要靠 `rag_query` 检索才有内容可写。
    - `full`：把每份来源的正文整份拼进去（受上下文预算裁剪），行为与
      `901b484` 逐字节一致，回归用例钉着这一点（`tests/test_source_mode.py`）。

    两种形态的正文可见性完全不同，这是刻意的：`full` 下模型能自己扫全文，
    检索只是辅助；`rag` 下检索是唯一入口。所以这里没有"材料小就走 full"的
    折中——行为随材料大小跳变最难排查，短材料一样走检索。
    """
    if source_mode() == SOURCE_MODE_RAG:
        return _material_manifest(sources)
    return _full_source_prompt_input(sources, system=system)


# 材料清单的上限。清单是**元信息**，超限时截断的只能是清单项，正文在任何情况下
# 都不进 prompt——"截断正文后拼进去"不是"不放"，是"放一部分"，最难排查。
MATERIAL_MANIFEST_MAX_ITEMS = 50
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


def _material_manifest(sources: list[Source]) -> str:
    """`rag` 形态的输入：只有清单，没有正文。

    形状与 `full` 形态的 sources 数组刻意不同（不是 JSON）：两种形态一眼能分出来，
    排查"这次到底走了哪条路"时不用去读提示词全文。模型也不需要从这个结构里
    解析任何东西——它只需要知道有哪几份材料、能不能查到。
    """
    lines = [f"本次附带 {len(sources)} 份材料（你看不到全文，需要用 rag_query 检索）："]
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
    lines.append("要写与材料有关的内容，先调用 rag_query 检索；"
                 "引用逐字取自检索结果。")
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
    '格式为 {"points":["要点一","要点二"]}。'
    "每个要点必须是一句能独立成立的话，不超过 30 字；不要编号，不要解释，"
    "不要重复已有子节点，不要编造具体数据、人名或文献。"
)

# 分片生成第一步：只定骨架，不写内容。一次调用同时要求模型「定框架 + 每支展开」，
# 它会把注意力放在先写出来的那几支上，后面的分支越写越敷衍——这是质量不稳的
# 直接来源。骨架单独一次调用，模型只需回答「分成哪几块」。
PLAN_PROMPT = (
    "你在为一张思维导图只定一级骨架，不写任何子要点。"
    "只返回一个 JSON 对象，不要代码围栏，格式为 "
    '{"branches":["分支一","分支二"]}。'
    "每条分支是一个能独立成立的短标题，不超过 20 字；彼此覆盖的范围不重叠，"
    "按逻辑顺序排列；不要编号，不要解释，不要编造具体数据、人名或文献。"
)


def _plan_ask(topic: str, branches: int) -> str:
    """骨架调用的 user 消息。

    期望分支数必须写进 user：只靠 system 里的通用说明时，请求 3 支与 8 支的
    模型输入一字不差，它只能按自己的直觉给一个数——而 `branches` 是用户在界面
    上选的值，被无视了。单条长度、互不重叠、逻辑顺序三条也一起重申：system 里
    虽有，但这是决定骨架质量的三条硬约束，重复一次便宜，骨架歪了整张图都歪。
    """
    return (
        f"主题：{topic}\n"
        f"根据内容实际包含的方面来定一级分支数量，最多 {branches} 条。\n"
        "材料层次多就用足、内容单薄就少给，不要为了凑数把不同的事塞进同一支。\n"
        "每条不超过 20 字，互不重叠，按逻辑顺序排列。"
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
    '格式为 {"children":[{"label":"要点一","expandable":true},'
    '{"label":"要点二","expandable":false}]}。'
    "只写一层，不要在 children 里再套 children。"
    "每个子项都必须带 expandable 字段，它是布尔值：这个子项还能不能拆出更具体的"
    "下级。如果它已经是最小可执行单元、再拆只会重复它自己，就填 false；"
    "如果它还是一句概括、下面明显还有具体内容可写，就填 true。"
    "如实判断，不要为了凑数硬拆，也不要一律填 true。"
    "每个 label 必须是一句能独立成立的话，不超过 30 字；不要编号，不要解释，"
    "只写这条分支范围内的内容，不要重复同级其他分支，不要编造具体数据、人名或文献。"
)

# 分片生成第三层往下：展开一个已经有上下文的节点。与上面的差别只在"它是谁"——
# 这一层并发展开时，模型看不到兄弟节点写了什么，只给一个 label 会让它写出与上一级
# 同一句话的内容，所以 user 里要带一条从一级分支到这里的路径。
# 同样只要求一层：实测一次要三层，写到第二段之后就开始敷衍甚至被输出上限截断。
# `expandable` 与上一层同义：`max_depth` 大于 3 时，某一支该不该继续深挖同样由
# 模型的判断决定，代码不再一律拉到上限。
SUBNODE_EXPAND_PROMPT = (
    "你在给思维导图里一个已有的节点补充下一层子节点。只返回一个 JSON 对象，"
    '不要代码围栏，格式为 {"children":[{"label":"要点一","expandable":true},'
    '{"label":"要点二","expandable":false}]}。'
    "只写一层，不要在 children 里再套 children。"
    "每个子项都必须带 expandable 字段，它是布尔值：这个子项还能不能拆出更具体的"
    "下级。已经是最小可执行单元、再拆只会重复它自己的填 false；"
    "明显还有具体内容可写的填 true。如实判断，不要为了凑数硬拆。"
    "每个 label 必须是一句能独立成立的话，不超过 30 字；不要编号，不要解释，"
    "只写这个节点范围内的内容，不要重复同一父节点下其他节点的说法，"
    "不要编造具体数据、人名或文献。"
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
                system = RAG_SOURCE_PROMPT + thinking + RAG_THINKING_GUARD
            else:
                # 有来源时必须给结构化输入：模型要靠 source_id 写引用。
                system = SOURCED_DETAIL_PROMPT + thinking + SOURCED_THINKING_GUARD
            raw = await chat_async(
                system, _source_prompt_input(sources, system=system),
                max_tokens=_max_tokens(ctx), **stream_kwargs,
            )
        else:
            raw = await chat_async(
                DETAIL_PROMPT + thinking, args.topic, max_tokens=_max_tokens(ctx),
                **stream_kwargs,
            )
    except GenerationError as exc:
        return _fail("generation_failed", f"生成失败：{exc}")

    try:
        content, diagnostics = validate_generated_content(raw, sources=sources or None)
    except Exception as exc:  # noqa: BLE001 — 校验失败的原因就是要回给模型的文本
        return _fail("invalid_map", f"模型返回的导图不合法：{exc}")

    # 结构与语义两层复核，原先只长在 `pipeline/service.run()` 的收尾里：
    #   - `check()` 只看树本身（单子节点、深度上限、同级重名），没有来源也能跑，
    #     所以对一句话主题的生成同样有效；
    #   - `annotate_nli()` 要对已通过前两层的引用与原文做语义蕴含打分，没有来源
    #     就无从比对。未启用时它立刻返回空列表，连 onnxruntime 都不 import，
    #     照搬即可，不必在调用处再判一次开关。
    # 诊断只是提示，不影响这棵树能不能用——契约校验在上一步就决定过了。
    diagnostics.extend(check(content))
    if sources:
        diagnostics.extend(annotate_nli(content, sources))

    ctx.candidate = content
    ctx.revision += 1
    count = _count_nodes(content.root)
    return ToolResult(
        ok=True,
        content=f"已生成导图「{content.title}」，共 {count} 个节点。",
        data={
            "root_id": content.root.id,
            "node_count": count,
            "revision": ctx.revision,
            "diagnostics": [item.model_dump() for item in diagnostics],
        },
    )


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
      - 接口层先扣：root + 一级分支 = 1 + branches
      - 剩下的按该层的父节点数平分；每个父节点拿到的份额再与结构上限（扇出 ×
        「本层 + 容忍的一层」）取小，得到这一次响应最多能落多少节点
      - 额度不足就**停手**，已经展开的部分照常交出，标 `partial` 并说明原因。
        少交一层不是失败，"报完整成功却少了半棵树"才是。

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

    **它不读来源正文**：`GenerateDeepArgs` 里没有来源字段，骨架与展开都只看
    主题。请求带材料时 `build_registry(has_sources=True)` 不会注册这个工具——
    那条路会产出一棵与材料无关的树，而且以 ok=True 成功返回，用户没有任何
    线索发现它错了。带材料的新建走 `mindmap_generate` 的忠实整理模式。

    `rag` 形态下这条限制**更痛**，但这一轮仍然保持：它一次检索都不发起，
    而 `rag` 形态的 prompt 里连正文都没有，等于纯靠主题自由展开。缺口记在
    `build_registry` 里：真正的解法是"每个分支先检索再展开"（分片 + 按支检索），
    本轮没做——所以"整树一次生成意味着只有一次检索机会"这句话先留在这里，
    下一轮把检索插进 `expand_branch` 之前别把它当成已经解决的问题。
    """
    if ctx.request.intent != "create":
        return _fail("wrong_intent", "只有新建导图时才能调用这个工具；修改现有导图请用 node_expand 或 mindmap_edit。")
    if ctx.candidate is not None and ctx.candidate.root.children:
        return _fail("map_exists", "已经有一张导图了，请用 node_expand 或 mindmap_edit 修改它。")

    need = min(args.points_per_branch, MAX_CHILDREN)
    # 深度上限在参数层已经夹过一次（ge/le），这里再夹一次是防"绕过参数模型直接
    # 调函数"：递归的每一层都要靠它判停，一个异常的 max_depth 会让循环失去上界。
    max_depth = max(2, min(args.max_depth, DEEP_MAX_DEPTH))
    # 二级及以下的扇出。`need` 本身是 2..5，再按 DEEP_FANOUT 兜一层——将来谁把
    # `points_per_branch` 的上限调大，深度带来的节点膨胀也不会跟着翻倍。
    fanout = max(2, min(need, DEEP_FANOUT))
    # 节点上限从契约读，绝不在这里写死：契约刚从 300 涨到 1000，工具里若留着一个
    # 旧数字，按层预分配分的就是个过期预算，多出来的节点要到整树校验才被拒。
    node_limit = _node_limit()
    diagnostics: list[Diagnostic] = []
    # 思维模型两条路都要拼：骨架决定怎么分块，展开决定每块怎么拆。
    thinking = thinking_block(ctx.request.thinking_model)

    # 根节点的标题在骨架之前就定了：它同时是树的 title，提前算出来还能在上报
    # 根节点时直接用它，不必等到最后合并那一步。
    root_label = args.topic.strip()[:200] or "思维导图"

    # 已上报过的 `(父路径, label)`，全局一份：预览的去重口径是"同一个父节点下
    # 同一句文字只出现一次"，跨路（不同展开调用）也成立。
    reported_grow: set[tuple[tuple[int, ...] | None, str]] = set()

    async def ask_model(
        prompt: str, user: str, parent: tuple[int, ...] | None = None,
        base_index: int = 0,
    ) -> Any:
        """发一次模型调用，顺带把「已经写完的子项」推给事件流。

        `thinking` 统一在这里拼，四个调用点不再各拼一遍。

        `parent` 只在开了上报通道时才有意义，也只有在有通道时才会传 `on_delta`：
        通道为 None 时连流式请求都不发，这条路上工具的行为与加生长事件之前
        **逐字相同**（现有测试全靠这一点保持有效）。
        """
        system = prompt + thinking
        if ctx.emit is None or parent is None:
            return await chat_async(system, user, max_tokens=_max_tokens(ctx))
        stream = _GrowStream(ctx, reported_grow, parent, base_index)
        return await chat_async(
            system, user, max_tokens=_max_tokens(ctx), on_delta=stream.feed,
        )

    # 第一步：出骨架。额度在调用**之前**占——请求一旦发出就是一次真实计费。
    if not ctx.reserve_model_call():
        return _fail("budget_exhausted", "本次运行的模型调用额度已用完，连骨架都出不了，无法分片生成。")
    ctx.extra_model_calls += 1
    try:
        raw_plan = await ask_model(PLAN_PROMPT, _plan_ask(args.topic, args.branches))
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

    # 额度预分配（第一层）：root + 一级分支是"接口层"，先从总节点里扣掉，剩下的
    # 才轮到各支往下长。不先扣的话，一支自己就可能把额度吃到只剩两位数，后面
    # 十九支全部只剩骨架——用户看到的是"第一支很丰满、其余都是空壳"。
    branch_share = max(0, node_limit - (1 + len(skeleton))) // max(1, len(skeleton))
    point_budget = min(need, branch_share)
    if point_budget < 1:
        return _fail(
            "node_budget_too_small",
            f"契约上限 {node_limit} 个节点，装不下中心主题 + {len(skeleton)} 条一级分支，无法分片生成。",
            node_limit=node_limit,
        )
    # 一次响应最多容忍模型多给一层。`max_depth=2` 时零容忍：那时多给的任何一层
    # 都会让产出与"改多级之前"不一致，而这正是 max_depth=2 要保证的事。
    nested_levels = 1 if max_depth > 2 else 0
    allowance = min(point_budget * (1 + _nested_capacity(fanout, nested_levels)), branch_share)

    # 第二步：并发展开。限流必须包住整个"占额度 + 发请求"，否则额度会被
    # 排队中的任务一次性预占，实际请求却还没发出去。
    semaphore = asyncio.Semaphore(DEEP_CONCURRENCY)

    async def expand_branch(title: str, branch_index: int) -> list[_Slot] | None:
        """展开一条分支。额度不够返回 None——那是"没展开"，与"展开失败"是两件事。"""
        async with semaphore:
            if not ctx.reserve_model_call():
                return None
            ctx.extra_model_calls += 1
            ask = (
                f"整体主题：{args.topic}\n"
                f"本条一级分支：{title}\n"
                f"请给出 {need} 个子要点。"
            )
            # 这一路的产出挂在第 branch_index 条一级分支下面，所以父路径是
            # `(branch_index,)`。
            raw = await ask_model(BRANCH_EXPAND_PROMPT, ask, parent=(branch_index,))
            kept, _ = _trim_slots(_candidate_slots(raw, point_budget, nested_levels), allowance)
            return kept

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
        ask = (
            f"整体主题：{args.topic}\n"
            f"本条一级分支：{title}\n"
            f"已有要点：{existing}\n"
            f"还差 {missing} 个子要点，请补齐，不要重复已有要点。"
        )
        try:
            # 补调出来的要点接在已有要点之后，所以这一路的子索引从"已有几个"起算；
            # 不带上这个偏移，预览会把补出来的节点挂到已有节点的位置上。
            raw = await ask_model(
                BRANCH_EXPAND_PROMPT, ask, parent=(index,),
                base_index=len(slots_by_branch[index]),
            )
        except GenerationError:
            continue  # 补调失败只是"仍然不足"，不升级成整次失败
        kept, dropped = _keep_new_slots(slots_by_branch[index], _candidate_slots(raw, missing))
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
    ) -> list[_Slot] | None:
        """展开一层里的一个节点。额度不够返回 None（没展开），抛错则由调用方收拾。

        `path` 是给人看的文字路径，`indices` 是同一次遍历给出的子索引链——上报
        生长事件时后者才是指向"这是哪个节点"的东西。
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
            )
            raw = await ask_model(SUBNODE_EXPAND_PROMPT, ask, parent=indices)
            return _candidate_slots(raw, count, nested_left)

    for depth in range(3, max_depth + 1):
        # `depth` 是**这一轮要产出的层号**，父节点在它上面一层：第 3 层的父节点
        # 是第 2 层的节点（一级分支的直接下级）。
        # 按分支分别遍历而不是先把第 2 层摊平：索引链的起点是"这条分支在根下面
        # 排第几"，摊平之后就只剩一个混在一起的全局下标，再也拼不出父子路径。
        parents: list[tuple[_Slot, str, tuple[int, ...]]] = []
        for branch_index, slots in enumerate(slots_by_branch):
            parents.extend(
                (slot, path, indices)
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
            break
        nested_left = max(0, max_depth - depth)
        # 这一次响应最多能落多少个节点：结构上限与额度份额取小，两者都不能破。
        # 不取小的话，模型每层都多给一层时总节点会按扇出的幂次膨胀，最后撞在
        # 整树校验上——那时额度已经花完了。
        level_allowance = min(breadth * (1 + _nested_capacity(fanout, nested_left)), share)
        level_results = await asyncio.gather(
            *(expand_node(slot, path, indices, breadth, nested_left)
              for slot, path, indices in parents),
            return_exceptions=True,
        )
        for (slot, _path, _indices), item in zip(parents, level_results):
            if item is None:
                level_skipped[depth] = level_skipped.get(depth, 0) + 1
            elif isinstance(item, BaseException):
                level_failed[depth] = level_failed.get(depth, 0) + 1
                level_reason.setdefault(depth, _reason_text(item))
            else:
                kept, _ = _trim_slots(list(item), level_allowance)
                if kept:
                    slot.children = kept
                else:
                    level_empty[depth] = level_empty.get(depth, 0) + 1

    # 第四步：合并。骨架先成树（分支 id 已在骨架里），再把所有节点按"父在子前"
    # 的顺序作为一批 add 应用上去——整批原子，半截树不会进 ctx.candidate。
    content = MindMapContent(
        title=root_label,
        root=Node(
            id=root_id,
            label=root_label,
            children=[Node(id=branch_id, label=title, children=[]) for title, branch_id in skeleton],
        ),
    )

    drafts: list[dict[str, Any]] = []

    def emit(parent_id: str, key: str, slots: list[_Slot]) -> None:
        """把候选子树摊平成 add 草案：父节点一定排在它自己的子节点前面。"""
        for position, slot in enumerate(slots):
            child_key = f"{key}-{position}"
            child_id = f"{TEMP_ID_PREFIX}d{child_key}"
            drafts.append({
                "op": "add",
                "id": child_id,
                "parent_id": parent_id,
                "index": position,
                "label": slot.label,
            })
            if slot.children:
                emit(child_id, child_key, slot.children)

    for index, (_, branch_id) in enumerate(skeleton):
        emit(branch_id, str(index), slots_by_branch[index])

    if drafts:
        ops, _ = normalize_ops(drafts, _node_ids(content.root))
        batch = OpBatch(batch_id=_batch_id(ctx), base_revision=ctx.revision, ops=ops)
        try:
            content = apply_ops(content, batch)
        except OpError as exc:
            return _fail(exc.code, f"{exc.path}：{exc.message}", path=exc.path)

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
    # 两个计数都是 0 = 提示词里的字段一次都没被回应。这件事不能只留在 data 里：
    # 用户看到的是 content，排查的人第一眼看的也是它。
    if leaf_declared == 0 and expandable_declared == 0:
        content_parts.append(
            "模型未使用可展开标记（expandable），本次按缺省全部可展开处理。"
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
            "diagnostics": [item.model_dump() for item in diagnostics],
        },
    )


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

    existing_titles = "、".join(child.label for child in node.children) or "（空）"
    ask = f"父节点：{node.label}\n已有子节点：{existing_titles}\n请补充 {args.count} 个新的子要点。"
    if args.instruction:
        ask += f"\n补充要求：{args.instruction}"

    try:
        if not ctx.reserve_model_call():
            return _fail("budget_exhausted", "本次运行的模型调用额度已用完，无法展开。")
        ctx.extra_model_calls += 1
        raw = await chat_async(EXPAND_PROMPT, ask, max_tokens=_max_tokens(ctx))
    except GenerationError as exc:
        return _fail("generation_failed", f"展开失败：{exc}")

    points = raw.get("points") if isinstance(raw, dict) else None
    if not isinstance(points, list) or not points:
        return _fail("invalid_points", "模型没有返回可用的子要点，请重试。")

    drafts = []
    for point in points:
        label = str(point).strip()
        if not label:
            continue
        # 索引用**已接受的数量**而不是原始下标：模型返回 [" ", "有效"] 时，
        # 第一条被跳过，用原始下标会往空父节点的 index 1 插，整批 index_out_of_range。
        drafts.append({
            "op": "add",
            "id": f"$e{len(drafts)}",
            "parent_id": node.id,
            "index": len(node.children) + len(drafts),
            "label": label[:200],
        })
        if len(drafts) >= args.count:
            break
    if not drafts:
        return _fail("invalid_points", "模型返回的子要点都是空的，请重试。")

    ops, id_map = normalize_ops(drafts, _node_ids(content.root))
    batch = OpBatch(batch_id=_batch_id(ctx), base_revision=ctx.revision, ops=ops)
    try:
        updated = apply_ops(content, batch)
    except OpError as exc:
        return _fail(exc.code, exc.message, path=exc.path)

    ctx.candidate = updated
    ctx.batches.append(batch)
    ctx.revision += 1
    return ToolResult(
        ok=True,
        content=f"已在「{node.label}」下新增 {len(ops)} 个子节点。",
        data={
            "batch": batch,
            "batches": [batch],
            "id_map": id_map,
            "revision": ctx.revision,
            "labels": [op.label for op in ops],
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

    截断与份数上限全部交给 `_material_snippets`：两处各写一份"前 200 字、最多
    10 份"，迟早出现"提示词看到的是 A、检索依据是 B"这种没人查得出来的差集。
    它已经是幂等的，`plan_query` 内部再跑一次不会二次截断。
    """
    return _material_snippets(
        [f"{source.filename or ''}\n{source.text or ''}" for source in sources])


async def _rag(args: RagArgs, ctx: RunContext) -> ToolResult:
    sources = _pick_sources(ctx, [])
    if not sources:
        return _fail("no_sources", "这次请求没有附带任何资料，无法检索。")

    filters = _rag_filters(args.filters)

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
                                  hyde=plan.hyde)
    else:
        hits = search_sources(sources, args.question, args.top_k, filters=filters)
    if not hits:
        # 过滤把来源筛光与查询没命中，返回给模型的必须是两句不同的话。
        # 都说"没有找到"，模型会以为材料里没有相关内容，进而改用编造或换主题；
        # 而实际情况是"条件太严，材料还在"。这类信息差靠日志补不上——模型看不到日志。
        #
        # 诊断只在空结果这条路径上做一次：正常有命中时不必再算一遍过滤，
        # 也让 mock 掉 search_sources 的用例仍能走到这里。
        if filters is not None and not filter_sources(sources, filters):
            return ToolResult(
                ok=True,
                content=(
                    f"过滤条件筛掉了全部 {len(sources)} 份资料，没有可检索的内容。"
                    "可以放宽或去掉过滤条件后重试。"
                ),
                data={"hits": [], "method": "lexical", "filtered_out": len(sources)},
            )
        return ToolResult(
            ok=True,
            content="没有在本次资料里找到相关片段。",
            data={"hits": [], "method": "lexical"},
        )

    # 回给模型的是结构化 JSON：它要能拿到 source_id 与偏移，才能在引用里指准。
    # `method` 由每条命中的实际取值汇总，不再写死 lexical——混合检索生效时写死
    # 会让模型以为这批片段只做了关键词匹配。只要有任意一条走了向量路，本轮就是
    # hybrid（那条路贡献了候选，排序已被它影响）。
    hits_data = [hit.model_dump() for hit in hits]
    return ToolResult(
        ok=True,
        content=json.dumps(hits_data, ensure_ascii=False),
        data={
            "hits": hits_data,
            "method": "hybrid" if any(hit.method == "hybrid" for hit in hits) else "lexical",
        },
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
            "仅在没有附带材料时使用：它按主题自由展开，不读任何来源正文，"
            "有材料时请用 mindmap_generate 做忠实整理。"
            "只有当前还没有导图时可用。"
        ),
        args_model=GenerateDeepArgs,
        fn=_generate_deep,
    ),
    "node_expand": lambda: ToolSpec(
        name="node_expand",
        description="把当前大纲里的某个节点展开成若干子要点，只新增子节点，不改动其他节点。",
        args_model=ExpandArgs,
        fn=_expand,
    ),
    "rag_query": lambda: ToolSpec(
        name="rag_query",
        description="在本次请求附带的资料里检索相关原文片段。回答关于资料的问题之前先调用它。",
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


def build_registry(intent: str = "create", *, has_sources: bool = False) -> ToolRegistry:
    """本次可用的工具集，按意图裁剪。

    早先是「四个全注册 + 各自内部校验意图」。那样做的问题是模型看得到工具、
    却每次调用都被拒：实测 expand 意图下它坚持调 `mindmap_edit` 删节点，
    失败三次才转向，白白烧掉调用额度。看不到这个工具，它就只能走对的那条路。

    裁剪规则：
      - `create` 给 `mindmap_generate` 与 `mindmap_generate_deep` 两个，由模型
        按主题复杂度自选（描述里写清了两者差别）
      - `expand` 只给 `node_expand`，请求带来源时再加 `rag_query`
      - `edit` 给 `mindmap_edit`，请求带来源时再加 `rag_query`
      - `rag` 形态（`MINDMAP_SOURCE_MODE=rag`）下，**任何**带来源的意图都加
        `rag_query`，包括 `create`：正文不在 prompt 里，没有检索工具时模型
        只能凭标题编，而它会以 ok=True 交出一棵看起来完整的树。

    两个生成工具必须同时在场：只留一个，等于替用户决定了"这张图多大"。而
    分片那条路的成本是 N+1 次调用，简单主题走它纯属浪费额度——这个判断
    只有拿到主题的模型能做。

    **有来源时撤掉 `mindmap_generate_deep`**：它的入参里没有任何来源字段，
    骨架与展开都只看主题，产出的树与材料无关，却会以 ok=True 成功返回。
    带材料的新建走 `mindmap_generate` 的忠实整理模式（它给模型喂结构化来源、
    还校验逐字引用）。这条不是"少给一个选项"，而是"少给一条会给出错误答案
    的路"——模型看不到它，就不会选中它。

    **`rag` 形态让这条限制更痛，这一轮仍然保持 discard**：`mindmap_generate_deep`
    自己一次检索都不发起（入参里根本没有 query），`rag` 形态下它连正文都拿不到，
    产出的树与材料完全脱钩——比 `full` 形态更离谱，那时 prompt 里至少还有全文。
    正确形态是「每个分支先检索再展开」（`docs/RAG-DECISIONS.md` 第 5 节的
    结构引导召回）：**整树一次生成意味着只有一次检索机会**，而分片 + 按支检索
    才能让每次展开都基于自己那支的证据。那条路留到下一轮，本轮保持 discard：
    宁可只给模型 `mindmap_generate` 这一条带检索能力的路，也不给它一条会以
    ok=True 交付无关树的捷径。

    `rag_query` 只在真有来源时才注册：没来源的检索必然返回「没有找到」，
    注册它只会诱导模型拿它当"思考"用。未知意图按 `create` 兜底——宁可少给
    工具，也不要把破坏性工具塞进一个没预期到的新意图里。
    """
    allowed = set(_ALLOWED_BY_INTENT.get(intent, _ALLOWED_BY_INTENT["create"]))
    if has_sources:
        allowed.discard("mindmap_generate_deep")
        if intent in _SOURCE_AWARE_INTENTS or source_mode() == SOURCE_MODE_RAG:
            allowed.add("rag_query")

    registry = ToolRegistry()
    for name in _TOOL_ORDER:
        if name in allowed:
            registry.register(_TOOL_SPECS[name]())
    return registry
