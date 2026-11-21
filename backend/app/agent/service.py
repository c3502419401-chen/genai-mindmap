"""AI 代理（F02 / F18）。

位置：用户给一个目标（"设计一款智能家居产品"），先拆成阶段，再逐阶段生成分支。

状态：**骨架**。plan() 返回固定的三段结构，不含模型调用；
真正实现要做的是：目标 → 阶段划分 → 每阶段调用 run() → 合并成一棵树。

设计约束：
  - 每一步都要记录来源与依据，不只是结果
  - 生成完成即落画布，没有"用户确认后才写进导图"这一步（候选层删于 `acfdb5b`：
    落地不再需要一次人工复核，撤销栈与「回到生成之前」接替了它）
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.contracts import (
    AgentEvent,
    AgentRunRequest,
    CheckpointFn,
    DoneEvent,
    ErrorEvent,
    HeartbeatEvent,
    NodeGrowEvent,
    OpsEvent,
    RunBudget,
    RunContext,
    SnapshotEvent,
    StageEvent,
    ToolCall,
    ToolProvider,
    ToolResult,
    ToolStartEvent,
    ToolEndEvent,
)
from app.agent import diagram_intent, shapes
from app.agent.provider import default_provider
from app.agent.tools import (
    ToolRegistry,
    _path_text,
    build_registry,
    library_available,
)
from app.contracts.diagnostic import Diagnostic, DiagnosticLevel
from app.contracts.mindmap import DiagramContent
from app.generation.service import (
    GenerationError,
    ModelNotConfigured,
    _demo_content,
)
from app.memory import store as memory_store
from app.prefs import service as prefs
from app.prefs import style as prefs_style

DEFAULT_STAGES = ["目标澄清", "要点拆解", "分支展开", "风险与验证"]
logger = logging.getLogger(__name__)

# 校验异常可能带上模型输出，日志只需足够定位的摘要，不能随材料或树的大小增长。
MAX_LOG_MESSAGE_CHARS = 500

# 模型把工具当"可选"、直接用文字回答时，最多再提醒几次。
# 只提醒一次：推理型模型被叫一次通常就改走工具了，叫不动的话继续空转只会烧额度。
MAX_NUDGES = 1

# 推给界面的模型原话上限：这是排障用的摘要，不是把整篇输出搬进事件流。
MODEL_TEXT_LIMIT = 4000

# 静默多久补一帧心跳。
#
# 线上实测（3 路并发，GLM-5.3-Flash）最长静默 25 秒，单轮首字延迟平时 5–10 秒。
# 这段时间事件流里一个字节都没有，中间链路只要有 30–60 秒的空闲超时（企业代理、
# Cloudflare、部分移动网络）就会把连接掐断——用户看到"连接中断"，而模型其实还在
# 跑、还在计费。nginx 那边配的是 900 秒，救不了更靠前的链路。
#
# 取 10 秒而不是 25 秒：必须小于最紧的常见空闲阈值（30 秒），且留一次重试余量。
# 再小就是把流刷成噪音，用户看到的仍是"没进展"。
HEARTBEAT_INTERVAL_SECONDS = 10.0


class AgentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str
    stages: list[str] = Field(default_factory=list)
    implemented: bool = False


class AgentTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=500)


def plan(goal: str) -> AgentPlan:
    """把目标拆成阶段。

    TODO(F02)：用模型做阶段划分，并为每个阶段生成"需要哪些输入"，
    再逐阶段调用本文件的 run()，最后把各阶段产出的树合并成一棵。
    现在返回固定阶段，只用来固定接口形状与前端交互顺序。
    """
    return AgentPlan(goal=goal, stages=list(DEFAULT_STAGES), implemented=False)


# ---------------------------------------------------------------- 多轮工具循环

_SYSTEM_BASE = (
    "你在操作一张思维导图，可以通过工具读写它。"
    "只能调用下面列出的工具；需要改图时用工具，不要只在文字里描述。"
)

# 按意图分叉指令，不是一套 system 打天下。实测教训：system 里只要写了
# "需要外部信息必须先查"，模型就会对"给考研数学复习计划生成导图"这种
# 完全不需要检索的请求也先调一次检索（10 次里 10 次都这样）。
_INTENT_RULES = {
    "create": "这次是新建导图。直接生成，不要先去检索资料。",
    "expand": "这次是展开某个已选中的节点。只补充子节点，不要重写整张图。",
    "edit": "这次是精确修改已有导图。必须按节点 id 改，禁止整张重新生成。",
}

# `rag` 形态下的 create 指令，与上面那条**语义相反**，而且必须是相反的：
# 正文不在上下文里，`don't retrieve` 等于让模型凭标题编。上面那条实测结论
# （"别让它先查"）成立于 `full` 形态——那时正文就在 prompt 里，先查纯属浪费；
# 形态一换，前提没了，结论就得跟着翻。
#
# 只覆盖 `create`：`expand` / `edit` 的指令里没有"不要检索"这句话，也就没有
# 矛盾要消除，多写一句反而会诱导它们在本不需要材料时也去查。
_RAG_INTENT_RULES = {
    "create": "这次是新建导图。材料正文不在上下文里，先调用 rag_query 检索，"
              "再按检索到的片段生成；检索不到的部分如实留空，不要编造。",
}


# 「用户选中了哪些节点」那一段的表头与规矩。两条都是**稳定字符串常量**：
# 用例按它们断言"选中时这一段出现了、没选中时整段不出现"，散成字面量的话，
# 某天改一句措辞，用例会绿着而提示词已经不是那个形状。
#
# 为什么必须把选中节点写进 system，而不是只靠那句 `goal`：用户圈了三个节点说
# 「这几个合并成一个」，`goal` 里没有 id、也没有标题，模型只能从整棵树里猜是哪三个。
# 大纲里虽然有全部 id，但"哪些是用户此刻圈住的"这个信息**根本不在树里**——
# 树只描述结构，描述不了选区。
SELECTION_HEADER = "用户这次在画布上选中了这些节点（id｜标题｜层级路径）："

# 这一段的核心分寸：**提示，不是作用域**。
#
# 写成硬约束（"只能改选中的节点"）在两种真实场景下会挡住本来正确的改动：
#
#   1. 用户说「整张图都加上时间维度」却顺手选着一个节点——他要的是全图；
#   2. 用户选了三个不相邻的节点说「把这一支整体压缩」——真正要动的是它们的
#      公共祖先，而那个祖先并不在选区里。
#
# 所以这里只要求"优先 + 说明"，不要求"只能"。执行层也不再拦（见
# `tools._outside_selection`），口径与这句话一一对应。
SELECTION_RULE = (
    "先改这些节点及其子树、以及它们周围紧邻的地方。"
    "但这不是允许改动的范围限制：如果用户的意图明显不限于选中的节点"
    "（例如「整张图都加上时间维度」，或要动的是这几个节点的公共祖先），"
    "照样可以改别处——只要在回复的正文（content）里说清为什么必须动到选区之外。"
)


def selection_text(request: AgentRunRequest, content: DiagramContent | None) -> str:
    """把"用户选中了哪些节点"渲染成 system 里的一段；没有选中时返回**空串**。

    空串而不是"用户选中了：\\n（无）"：一个空的小标题会让模型以为"用户选了 0 个
    节点"是一种需要它处理的输入，而真实语义是"这一轮没有选区，别按选区想事情"。
    调用方按空串整段跳过（`_system_text` 里那个 `if part` 过滤）。

    只在 `edit` 意图下拼。`expand` 不用这一段：那条路只展开**一个**节点，`_expand`
    自己会把从根到它的标题链写进工具调用的提示里（`_expand_ask`），system 再写
    一遍就是同一件事的两处口径——而两处口径迟早会分叉成"工具里说 A、system 说 B"。
    `create` 更没有选区可言：那时画布还是空的。

    找不到的 id 静默跳过（用户选完又删了节点、或老前端发来了上一个版本的 id）。
    全部找不到时同样返回空串：提示词里列不出任何一条，就不该留一个空段落。
    """
    if request.intent != "edit" or not request.selected_ids or content is None:
        return ""
    lines: list[str] = []
    for node_id in request.selected_ids:
        # 路径与展开共用同一份实现（`tools._node_path` / `_path_text`）：
        # "从根到这里的标题链"只该有一种写法，否则同一次运行里模型会看到两种格式。
        chain = shapes.path_to(content, node_id)
        if chain is None:
            continue
        lines.append(f"- [{chain[-1].id}] {chain[-1].label.strip()}｜路径：{_path_text(chain)}")
    if not lines:
        return ""
    return "\n".join([SELECTION_HEADER, *lines, SELECTION_RULE])


def outline_text(content: DiagramContent | None, limit: int = 200) -> str:
    """把图压成带 id 的大纲，让模型能引用具体节点。

    没有 id 的大纲模型就只能靠文字猜"改哪一个"，而文字是会被它自己改写的。

    三套结构共用这一份：`shapes.walk_depths` 让每棵并列子树（鱼的六根骨、流程
    的若干阶段）各从第 1 层起算——大纲里的缩进因此仍然是"这一支自己的深度"，
    与画布上那一列一致。树只有一棵，缩进与改动之前逐字节相同。
    """
    if content is None:
        return "（当前没有导图）"
    lines: list[str] = []
    for node, depth in shapes.walk_depths(content):
        if len(lines) >= limit:
            break
        lines.append("  " * (depth - 1) + f"[{node.id}] {node.label}")
    if len(lines) >= limit:
        lines.append(f"……（只列前 {limit} 个节点）")
    return "\n".join(lines)


def _intent_rule(request: AgentRunRequest) -> str:
    """这次意图该配哪条指令。检索优先的指令只覆盖**有东西可查**的 `create`。

    没有来源、库里也没有材料时不改：这时模型手里真的没有任何可查的东西，换成
    检索优先的指令，只会让它去查一个空列表。

    条件从 `request.sources` 扩成 `request.sources or 库非空`：知识库接上以后，
    "这次请求有没有材料"不再等于"有没有东西可查"。库里存着过往材料时，那句
    "直接生成，不要先去检索资料"就成了错的前提——那份材料根本不在上下文里。
    判据必须跟着**可检索的东西**走，而不是跟着请求来的那几份走。

    代价是每轮刷新 system 时会读一次库（`library_source_count`，一次本地
    sqlite 计数查询，不读正文）。这里不做缓存：库可能在运行期间被别的请求写入，
    而"这一轮才发现库里有材料"并不会让前面的轮次变错——模型下一轮就会看到新
    指令，比一条运行开始时算死的布尔值更接近事实。
    """
    if request.sources or library_available():
        override = _RAG_INTENT_RULES.get(request.intent)
        if override is not None:
            return override
    return _INTENT_RULES.get(request.intent, "")


def _notes_text() -> str:
    """C 类偏好（自由文本备注）那一段；没有备注时返回**空串**。

    **只有 C 类进 prompt**。A 类（图型 / 拆解方式）与 B 类（要不要 emoji）是
    结构化字段，它们的正确位置是请求层与生成侧的后处理——让模型"不必记"，而不是
    让模型"记着"：一个 `diagram_type: "fishbone"` 字段比十行"用户喜欢鱼骨图"可靠
    得多，而且不花 token。

    超限时**从最旧的截断**并把条数记进日志（`AGENTS.md`：降级要留下降过级的痕迹）。
    这里不往上抛：一条备注装不下不该让整次生成失败，而用户看到的"我写了 8 条它只
    用了 5 条"必须有一个可查的出处。
    """
    text, dropped = prefs.prompt_notes()
    if dropped:
        logger.warning(
            "偏好备注超过 %d 条 / %d 字的上限，最旧的 %d 条这次没有进提示词",
            prefs.MAX_NOTES, prefs.MAX_NOTE_CHARS, dropped)
    return text


def _system_text(
    request: AgentRunRequest,
    content: DiagramContent | None,
    *,
    memory: str = "",
) -> str:
    """这次运行要下发的 system。

    六段的顺序有讲究，不能随手调：
    **身份 → 这个人写下的偏好 → 关于这个人的记忆 → 这次做什么 → 这次对着哪儿 → 树**。

    备注（C 类偏好）排在 `_SYSTEM_BASE` **之后**、意图规则**之前**：意图规则是
    "这次要做什么"的强约束，备注是"这个人是谁"的背景，**背景不该盖住这次的指令**。
    排在前面的原因不是重要性，是阅读顺序——模型先读到"这个人的习惯"，再读到"这次
    必须怎么做"，冲突时后者自然更近。备注那一段自己也写了让位的话（`NOTE_HEADER`）。

    `memory`（检索出来的记忆条目，见 `app/memory/store.py`）排在备注**之后**、
    意图规则之前，位置是有讲究的：**显式声明在前、系统推断在后**。手写的 `note.*`
    是用户明确要的，检索出来的条目是系统猜的；两段挨着但分开，模型读到的优先级
    顺序与事实一致。合成一段的后果是用户无法判断"哪几句话是我说的"——那正是这次
    改动要避免的混乱。

    `memory` 由调用方**已经检索好**再传进来，这个函数只负责拼接。原因见
    `_loop` 里那段"一次生成只检索一次"：这个函数每一轮都会被调一次，检索放进来
    就是 N+4 次 embedding。

    选区排在"当前导图"之前，因为它描述的是"接下来那句话指哪儿"，先读到它，
    后面那棵大纲才有一个"重点看这几支"的读法。

    空段整段跳过（`if part`）：没有选区时 `selection_text` 返回空串、没有备注时
    `_notes_text` 也一样、没有记忆时 `memory` 是空串——直接 join 会在 system 里留
    一行空白，看起来像"这里本该有东西但没填上"。
    """
    return "\n".join(part for part in (
        _SYSTEM_BASE,
        _notes_text(),
        memory,
        _intent_rule(request),
        selection_text(request, content),
        "当前导图：",
        outline_text(content),
    ) if part).strip()


def _style_text() -> str:
    """用户风格画像那一段（读 `style_profile` 表算出来的一句形状描述）。

    **拼在 user 末尾，不在 system 里**，所以这个函数只负责取文本、不负责拼接
    （拼接在 `_initial_messages`）。位置的理由见 `_initial_messages` 的 docstring。

    失败一律返回空串：画像是附属信息，它读不出来（表被删、字段是上一版实现的、
    库锁住）不该让一次生成失败。这与 `_notes_text` 里"超限只记日志"同一条方向
    ——降级的方向永远是"这次少一句个性化"。

    同步调用而不是 `to_thread`：这是一次本地 sqlite 查询（8 行），微秒级，
    与 `_intent_rule` 每轮读一次库同一个理由。
    """
    if not prefs_style.enabled():
        return ""
    try:
        return prefs_style.prompt_block()
    except Exception as exc:  # noqa: BLE001 — 见 docstring 第 2 段
        logger.warning("风格画像读取失败（%s），这次不注入", type(exc).__name__)
        return ""


def _initial_messages(
    request: AgentRunRequest, memory: str = "", style: str = "",
) -> list[dict[str, Any]]:
    """开头的两条消息。`style` 拼在 **user 末尾**。

    三条规律叠加起来决定了这个位置，不是随手放的：

      1. **不进 system**。本仓库已经验证过三次（`role=decision`、`_detail_ask`
         的自查句、全塞模式下的引用规则），写在 system 里的具体指令会被那一段
         长且稳定的背景稀释；挪到 user 里才生效。
      2. **在 user 的末尾，不在开头**。开头是用户这次的原话，它的权重最高；
         把画像插在它前面等于替用户改写了需求陈述。
      3. **只拼一次，不每轮刷新**。`_refresh_outline` 每轮只换 system[0]，
         user 那条从头到尾不变——画像也该是这次运行的一份常量。

    `style` 为空串时返回的消息与加这个参数之前**逐字节相同**（`{"role": "user",
    "content": request.goal}`），这是既有 2162 条用例的前提。
    """
    goal = request.goal if not style else f"{request.goal}\n\n{style}"
    return [
        {"role": "system", "content": _system_text(request, request.snapshot, memory=memory)},
        {"role": "user", "content": goal},
    ]


def _refresh_outline(messages: list[dict[str, Any]], ctx: RunContext) -> None:
    """每轮把第一条 system 消息换成最新的树。

    不刷新的话，模型看到的大纲始终是请求开始时的快照：它刚生成的那些节点
    根本没有 id 可引用，于是下一轮只能整张重来——那正是这次改造要消灭的行为。
    原地替换而不是追加，是因为追加会让消息历史随轮数线性增长，几轮之后
    光大纲就把上下文吃满了。

    记忆那一段从 `ctx.memory_hints` 读，**不重新检索**：它是这次运行开头算好的一份
    常量，每一轮都照抄。理由见 `_loop` 里的那段注释。
    """
    messages[0] = {"role": "system", "content": _system_text(
        ctx.request, ctx.candidate, memory=ctx.memory_hints)}


def _nudge_text(registry: ToolRegistry) -> str:
    """模型只用文字回答、没调工具时的提醒。

    推理型模型（实测 `deepseek-flash`）会把工具理解成"需要时再用"，于是把导图
    直接说成一段文字，接口层只能返回空结果。提醒必须把要求说死：走工具、别用
    文字描述内容、现在就调。
    """
    names = "、".join(registry.names()) or "可用工具"
    return (
        "你刚才没有调用任何工具，只写了文字，这次结果里没有任何导图。"
        f"现在必须调用 {names} 把导图作为工具参数提交："
        "不要用文字描述导图内容，也不要解释你要怎么做，直接发出工具调用。"
    )


def _clip(text: str, limit: int = MODEL_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


# 思考过程推给界面的上限。**不是** `MODEL_TEXT_LIMIT`：那 4000 字是"排障摘要"的
# 口径，而 reasoner 的思考过程实测就是 3000–8000 字，用它去截等于每次都截掉一半。
#
# **上限为什么是字节、不是字数**——同一句"2 万字"，编码之后差 6 倍（实测 `done` 帧，
# 编码函数就是 `stream.encode_event`）：
#
#     2 万汉字       60076 字节   （3 字节/字）
#     2 万 emoji     80076 字节   （4 字节/字）
#     2 万控制字符  120076 字节   （JSON 把 U+0000 转义成 `\u0000`，6 字节/字）
#
# 按字符设上限，管的是"模型想了多少"；真正压到链路上的是"这一帧多大"。两者在
# 控制字符那一档差 6 倍，所以口径换成编码后的字节。
#
# 65536（64 KiB）这一个数同时做到三件事：
#   · 实测上界（8000 字）整段过去、一个字不动：汉字 24KB / emoji 32KB / 控制字符 48KB；
#   · 比原来的"2 万字"更宽也更安全——汉字约 21800 字（原来 60KB），emoji 约 16300 字
#     （原来 80KB），控制字符约 10900 字（原来 120KB）；
#   · 最坏一帧 ≈ 64 KiB + 一百多字节帧头，任何一层都不必为它调参（前端 `postSse` 按
#     `\n\n` 切帧、不设单帧上限，nginx 那边是 900 秒长连接）。
#
# 留这条上限不是为了"截"，是为了防跑飞：模型吐 200 万字符时不能把它整段塞进
# 事件流（也塞不进 `_MAX_RESPONSE_BYTES` 之外的地方）。截断时补一句说明，
# 让"少了一段"这件事在界面上是看得见的。
REASONING_BYTE_LIMIT = 65_536


def _encoded_size(text: str) -> int:
    """这段文本落进 SSE 帧之后占多少字节：JSON 转义与 UTF-8 都算进去。

    量的是**编码之后**。控制字符会被 JSON 转义成 `\u0000`（1 字节 → 6 字节），
    那正是字符口径看不见的一半；`ensure_ascii=False` 与 `stream.encode_event` 同一个
    口径，汉字 3 字节、emoji 4 字节。`surrogatepass` 只为让孤立代理项也能度量
    （真按 strict 编会在截断路径里自己抛出去，而那不是这条函数该处理的事）。
    """
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8", "surrogatepass"))


def _reasoning_text(text: str) -> str:
    """思考过程原样推给前端，只挡"跑飞"那一档。"""
    if _encoded_size(text) <= REASONING_BYTE_LIMIT:
        return text
    note = f"…（思考过程过长，已截断，原文 {len(text)} 字）"
    # 截在**字符**边界上：二分找的是"多长的前缀还装得下"，`text[:k]` 按码位切，
    # 不会切开一个 3/4 字节的 UTF-8 序列——所以这里不需要事后修补半个字符。
    budget = REASONING_BYTE_LIMIT - _encoded_size(note)
    lo, hi = 0, min(len(text), REASONING_BYTE_LIMIT)  # 一个字至少 1 字节，先压一档
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _encoded_size(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + note


def _dedupe_key(call: ToolCall) -> str:
    """同工具同参数的指纹。

    规范化用 sort_keys：模型两次给的参数键序可能不同，但那显然是同一次调用。
    """
    return call.name + "\0" + json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str)


# ---------------------------------------------------------------- 两道行为防线
#
# `max_tool_calls` 从 8 提到 50（见 `contracts.RunBudget.max_tool_calls`）之后，
# 原来那道"次数闸"不再能自然掐断"模型原地打转"：50 次空转 = 50 次模型往返，
# 用户等几分钟换回一棵没变的树。所以提额度的同时补两条**行为**判据，不再靠数字。

# 防线一：连续重复调用。
#
# 判的是"**连续**"：从最近一次往回扫，只要一路上都是同一个参数指纹就算重复；
# 中途只要插入过任何别的调用就放行——用户可能在两次提问之间往材料里补了东西，
# 那时再问同一个问题是**对的**，拒掉等于让模型用旧检索结果编。
#
# 为什么是"连续"而不是"N 次窗口内出现过"：后者会把 `A, B, A` 也判成重复
# （A 在最近 3 次里出现过），而 `A, B, A` 正是"先查、再改、回来复核"的正常形状。
# 窗口 3 只约束扫描长度：连续重复在第二次就被拒了，扫不到更远；留着这个数是为了
# 将来把判据换成"窗口内出现两次"时只改一处。
REPEAT_CALL_WINDOW = 3

# 防线二：无进展。
#
# 连续这么多次工具调用之后候选树一个字节都没变 → 记一条 `NO_PROGRESS` 警告。
# 8 的依据是**旧闸的宽度**：8 正是被放开的那道 `max_tool_calls`，也就是"原来一整轮
# 空转最多能烧掉几次调用"。旧值下这类循环会被次数上限自然掐断，现在同样的浪费量
# 只值一条警告；到 16（两倍）说明模型没有任何恢复迹象——再给它 34 次也只是把
# 刚才那 16 次重演一遍，所以中止。
NO_PROGRESS_WARN = 8
NO_PROGRESS_ABORT = 16


def _duplicate_anchor(ctx: RunContext, key: str) -> str | None:
    """这次调用是不是"刚做过同一件事"；是的话返回那次的 call_id。

    返回**最早那次**连续相同的调用：模型连着发三次同一个 `rag_query`，第二、
    第三次都该被告知"这个查询在第一次的 tool_call 里已经做过了"。指回中间那次
    会让它以为"上一次被判重复，那我再试一次"。
    """
    anchor: str | None = None
    for call_id, fingerprint in reversed(ctx.recent_calls[-REPEAT_CALL_WINDOW:]):
        if fingerprint != key:
            break
        anchor = call_id
    return anchor


def _tree_signature(ctx: RunContext) -> str:
    """候选树的内容指纹，用来判"这次工具调用有没有改变树"。

    三个候选判据都试过，只有这一个站得住：

      - `ctx.revision`：它只在**写入路径**里 +1，而 `node_expand` 给一个节点加
        一整层子节点时根本不碰它（那条路径直接改 `ctx.candidate`）。用它当判据，
        "连续展开 8 次"会被误判成无进展。
      - 节点数：`update_node` 反复把 label 改来改去时节点数一个都不变，而那正是
        "50 次空转"里最常见的一半。
      - 整棵树序列化后的哈希：所有改动路径都绕不过它。

    代价是一次工具调用算一次。一棵 1000 节点的树 dump + 哈希是毫秒级，而工具调用
    本身是「一次模型往返」，这个开销在噪声里。
    """
    if ctx.candidate is None:
        return ""
    return hashlib.blake2b(
        ctx.candidate.model_dump_json().encode("utf-8"), digest_size=8).hexdigest()


def _no_progress_message(no_progress: int, tool_calls: int) -> str:
    return (f"连续 {no_progress} 次工具调用没有改动导图（本次已调用 {tool_calls} 次），"
            f"模型可能在原地打转；到 {NO_PROGRESS_ABORT} 次会停止这次运行。")


def _no_progress_frame(
    ctx: RunContext, seq: "itertools.count[int]", no_progress: int, tool_calls: int,
) -> StageEvent:
    """警告：连续多次调用没有改变树。

    三处都要写，缺一处就有人看不见：`ctx.loop_diagnostics` 是给调用方与用例的
    结构化记录，日志是给排查的，`StageEvent` 是给用户的（前端那行"现在在干什么"
    会显示出来——它是唯一能让用户知道"这 30 秒在空转"的通道）。
    """
    message = _no_progress_message(no_progress, tool_calls)
    ctx.loop_diagnostics.append(Diagnostic(
        level=DiagnosticLevel.warn, code="NO_PROGRESS", message=message,
        evidence=f"tool_calls={tool_calls},no_progress={no_progress}",
    ))
    _log_failure(ctx, tool_calls, code="NO_PROGRESS", message=message)
    return StageEvent(seq=next(seq), stage="no_progress", detail=message)


def _no_progress_stop(
    ctx: RunContext,
    seq: "itertools.count[int]",
    usage_total: dict[str, Any],
    tool_calls: int,
    no_progress: int,
    *,
    model: str | None = None,
    served_model: str | None = None,
    reasoning: str = "",
) -> DoneEvent | ErrorEvent:
    """连续无进展到硬阈值：收尾。

    与 `_budget_stop` 走同一个出口（`_deliver_or_fail`）：已经有候选就当作完成
    （那棵树是能用的，报成失败只会让用户不知道该不该用），一份都没有才报错。

    `model` / `served_model` / `reasoning` 也要带上：这一帧和"模型自己收尾"那一条在
    界面上是同一种成功（都落进了画布），少字段就会出现"同样是生成成功，一次看得到
    模型名与思考过程，另一次什么都没有"——而用户完全不知道差别来自哪种收尾。
    """
    message = (f"连续 {no_progress} 次工具调用没有改动导图，模型在原地打转，已停止。")
    return _deliver_or_fail(
        ctx, seq, usage_total,
        code="no_progress", message=message, limit="no_progress",
        tool_calls=tool_calls, model=model, served_model=served_model,
        reasoning=reasoning,
    )


def _args_summary(call: ToolCall, width: int = 80) -> dict[str, Any]:
    """事件流里只放参数摘要：完整参数可能带整份来源正文。"""
    summary: dict[str, Any] = {}
    for key, value in list(call.arguments.items())[:6]:
        text = value if isinstance(value, (int, float, bool)) else str(value)
        if isinstance(text, str) and len(text) > width:
            text = text[:width] + "…"
        summary[key] = text
    return summary


def _merge_usage(total: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
    merged = dict(total)
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if isinstance(usage.get(key), int):
            merged[key] = int(merged.get(key, 0)) + int(usage[key])
    return merged


def _remaining_seconds(budget: RunBudget, started: float) -> float:
    return budget.max_seconds - (time.monotonic() - started)


def _budget_limit(budget: RunBudget, tool_calls: int, started: float) -> str | None:
    """时间与工具次数两种预算的判停。

    **模型调用额度不在这里判**：它由 `RunContext.reserve_model_call()` 统一裁决。
    外层轮次与工具内部的 `chat_async` 必须共用同一个账本，而这里只看得见外层，
    在这里判就成了第二份真相——预算 1 实测发出 4 次请求，正是这么来的。
    """
    if tool_calls >= budget.max_tool_calls:
        return "max_tool_calls"
    if _remaining_seconds(budget, started) <= 0:
        return "max_seconds"
    return None


_BUDGET_HINT = {
    "max_model_calls": "模型调用次数已用完",
    "max_tool_calls": "工具调用次数已用完",
    "max_seconds": "这次运行超过了时间上限",
}


def _log_failure(
    ctx: RunContext,
    tool_calls: int,
    *,
    code: str,
    message: str,
    call: ToolCall | None = None,
    level: int = logging.WARNING,
) -> None:
    # 不记录参数、result.data 或异常堆栈：它们可能夹带整份正文或整棵树。
    # JSON 转义换行，防止多行错误把一次失败拆成难以关联的多条日志。
    logger.log(level, "agent 运行异常 %s", json.dumps({
        "call_id": call.call_id if call else None,
        "tool": call.name if call else None,
        "code": code,
        "message": message[:MAX_LOG_MESSAGE_CHARS],
        "model_calls_used": ctx.model_calls_used,
        "tool_calls_used": tool_calls,
    }, ensure_ascii=False))


def _result_summary(result: ToolResult) -> dict[str, Any]:
    if result.ok:
        return result.data
    # 注册表的旧错误只把原因写在 content 中；补给事件流后界面与日志才能说同一件事。
    summary = dict(result.data)
    for key, fallback in (
        ("code", "tool_failed"),
        ("message", result.content or "工具未返回具体失败原因。"),
    ):
        if not isinstance(summary.get(key), str) or not summary[key].strip():
            summary[key] = fallback
    return summary


@dataclass
class _ToolOutcome:
    """一次工具执行的去向。

    放进一个容器里是因为 `_run_tool` 是 async generator：它要一边 yield 事件帧，
    一边把结果交回调用方，而生成器没法 `return` 一个值。
    """

    result: ToolResult | None = None
    error: BaseException | None = None
    timed_out: bool = False


def _budget_message(ctx: RunContext, limit: str, tool_calls: int) -> str:
    """预算停下来的那句话。时间上限这一条要带上"走到哪了"。

    `max_seconds` 从 120 提到 600 之后，它是一条**只在长循环上**才撞到的上限，
    而撞上它的那一刻恰恰是最需要解释的时候：用户等了十分钟，看到的如果只是
    「这次运行超过了时间上限」，他无从判断"我的图改完没有""这一版能不能用"。
    预算的作用是限制继续花钱，不是把已经做了一半的事说成一句没有下文的错误。

    次数上限那两条不加进度：`max_model_calls` / `max_tool_calls` 停在**某一轮
    的开头**，此时树的内容与上一帧 tool_end 完全一致，多写一句只是重复。

    这里**不**谎报"还剩几处没做"：模型这一轮还没发出的调用不在任何地方，编一个
    数字比不说更糟。能诚实说的是"用掉多少、做到哪、之后的没执行"。
    """
    base = _BUDGET_HINT[limit]
    if limit != "max_seconds":
        return base
    nodes = shapes.count_nodes(ctx.candidate) if ctx.candidate is not None else 0
    done = f"，树上是 {nodes} 个节点" if ctx.candidate is not None else "，还没产出导图"
    return (f"{base}（已执行 {tool_calls} 次工具调用{done}）。"
            "这之前的改动都已进树里，之后的没有执行——把目标拆小一点再跑一次，"
            "或者调大 max_seconds。")


def _deliver_or_fail(
    ctx: RunContext,
    seq: "itertools.count[int]",
    usage_total: dict[str, Any],
    *,
    code: str,
    message: str,
    limit: str | None = None,
    tool_calls: int,
    call: ToolCall | None = None,
    model: str | None = None,
    served_model: str | None = None,
    reasoning: str = "",
    log_code: str | None = None,
) -> DoneEvent | ErrorEvent:
    """收尾的唯一口径：**这一轮产出过树就交树，一棵都没长出来才报错。**

    六条终态路径（预算用尽、空转、工具超时、连续失败、意外收尾、工具次数到顶）
    从这里一起出：从前它们各写一份，其中两条按规矩交候选、四条无条件发 error，
    于是同一件事——"跑到一半停了"——在界面上有两种截然不同的样子。用户看到的差别
    不是"停的原因不同"，而是"一次拿到了半棵树、一次什么都没拿到"，而后者往往还
    是跑了三五分钟的那次。

    **判据是 `ctx.produced`，不是 `ctx.candidate is not None`。** 后者看着等价，
    实际差得很远：`edit` / `expand` 必须带 `request.snapshot`（`router._validate`），
    而 `RunContext(candidate=request.snapshot)` 把它直接装进了候选位。于是编辑意图下
    "候选非空"在**第一帧之前**就成立了——那棵树是用户自己的，不是模型交付的。按
    `candidate` 判的结果是：模型没配置、模型报错、工具全挂，一律返回
    `done(partial=True, content=用户原树)`，前端把它当新结果落进画布、记一条 0 改动的
    生成事件、状态行写"结果已落到画布"——用户以为模型改过了，而"去配模型"那张卡片
    永远不会出现。

    交出去的树标 `partial`：它确实不是一次完整的生成（有分支没长出来、说明可能
    没写完），**必须让用户知道**，否则"半棵树"会被当成模型认为的完整答案。但
    "不完整"与"失败"是两件事：前者可以改、可以保存、可以接着展开，后者只能重跑。

    `message` 在两条分支上都要用：作为 `partial_reason` 显示在结果旁边，或作为
    error 帧的 `message`。所以它必须是人能读的一句话，不能只是错误码。

    `model` / `served_model` / `reasoning` 只在成功分支带（与"模型自己收尾"那一帧
    同形）：同一份产出，不该因为停的原因不同，一次看得到模型名与思考过程、另一次
    什么都没有。`served_model` 与 `model` 是两件事，两个都要带（见 `DoneEvent`）。

    `log_code` 只在**日志**与出站 `code` 不一致时才传（现在只有工具超时这一条：
    帧上仍是 `budget_exhausted`/`max_seconds`，而日志要写 `tool_timeout`）。把它
    并成一个 code 会丢掉排查时唯一的区分线索——"这次是工具卡住了还是单纯没额度"
    决定了下次该调 `max_seconds` 还是 `max_model_calls`。
    """
    _log_failure(ctx, tool_calls, call=call, code=log_code or code, message=message)
    if ctx.produced and ctx.candidate is not None:
        return DoneEvent(
            seq=next(seq), content=ctx.candidate, usage=usage_total,
            mode="model", model=model, served_model=served_model,
            reasoning=_reasoning_text(reasoning),
            partial=True, partial_reason=message,
        )
    return ErrorEvent(
        seq=next(seq), code=code, message=message, limit=limit,
    )


def _budget_stop(
    ctx: RunContext,
    seq: "itertools.count[int]",
    usage_total: dict[str, Any],
    limit: str,
    tool_calls: int,
    call: ToolCall | None = None,
    *,
    model: str | None = None,
    served_model: str | None = None,
    reasoning: str = "",
) -> DoneEvent | ErrorEvent:
    """预算用尽时的收尾帧：树上已经有东西就当作完成，空树才算失败。

    深层生成会在额度不足时提前停止展开并记一条诊断，所以走到这里时通常
    已经是一棵能用的树。这时再报「模型调用次数已用完」，用户看到的是刚长出来的
    结果和一条错误同时出现，不知道该不该用那一版——预算的作用是限制继续花钱，
    不是把已经产出的结果报成失败。

    真的一棵树都没有（第一轮就没额度、工具失败前没产出）时仍然报错，
    那种情况下用户确实什么都没拿到。

    `model` / `served_model` / `reasoning` 是**成功分支**必须带的字段（与模型自己收尾
    那一帧同形）：预算耗尽只是"没继续花钱"，不代表这一次的产出不该被当作一次正常完成。
    少了它们，"预算到点交付"与"模型主动交付"在界面上长得不一样，而用户看不出为什么。
    `reasoning` 取**最后一次模型响应**的思考过程——那是这一轮真正决定"可以收尾了"
    的那次思考，前面的轮次属于过程。
    """
    return _deliver_or_fail(
        ctx, seq, usage_total,
        code="budget_exhausted",
        message=_budget_message(ctx, limit, tool_calls),
        limit=limit,
        tool_calls=tool_calls, call=call,
        model=model, served_model=served_model, reasoning=reasoning,
    )


def _node_grow(payload: dict[str, Any], seq: "itertools.count[int]") -> NodeGrowEvent | None:
    """工具上报的字典 → 带 seq 的帧；认不出来就返回 None 丢掉。

    预览是附加信息，一帧坏掉不能打断正在跑的工具，所以这里不抛异常。取号放在
    字段校验之后：校验失败的帧不占号，前端"seq 连续"这条判断才不会被一串缺号
    误导成丢帧。
    """
    if payload.get("type") != "node_grow":
        return None
    path = payload.get("path")
    label = payload.get("label")
    # path 可以是 None（根帧），但给了就必须是索引数组
    if (path is not None and not isinstance(path, list)) or not isinstance(label, str):
        return None
    flag = payload.get("expandable")
    try:
        return NodeGrowEvent(
            seq=next(seq), path=path, label=label,
            expandable=flag if isinstance(flag, bool) else None,
        )
    except ValidationError:
        return None


def _stage_frame(payload: dict[str, Any], seq: "itertools.count[int]") -> StageEvent | None:
    """工具上报的阶段帧 → 带 seq 的 `StageEvent`；认不出来就丢掉。

    与 `_node_grow` 同一套规矩，多一条 `count`：

      - `stage` 必须是稳定标识（`external_search`），`detail` 是给人看的那句话；
      - `count` 可以缺席（开始帧），给了就必须是非负整数。`0` 是**有效值**
        （"一条都没搜到"是结论），与 None（"还没结束"）不是一回事，所以不能用
        `count or None` 之类的写法折叠它们。

    取号同样放在字段校验之后：校验失败的帧不占号，前端"seq 连续"这条判断才不会被
    一串缺号误导成丢帧。
    """
    if payload.get("type") != "stage":
        return None
    stage = payload.get("stage")
    detail = payload.get("detail", "")
    count = payload.get("count")
    if not isinstance(stage, str) or not stage or not isinstance(detail, str):
        return None
    # bool 是 int 的子类：`count=True` 会被 pydantic 收成 1，这里先挡掉。
    # `>= 0` 也在这里判，不留给 pydantic：`next(seq)` 在构造之前求值，靠
    # `ValidationError` 兜底的话，一个非法帧照样会领走一个号，而前端的"seq 连续"
    # 判断会把它读成丢帧。
    if count is not None and (
            isinstance(count, bool) or not isinstance(count, int) or count < 0):
        return None
    try:
        return StageEvent(seq=next(seq), stage=stage, detail=detail, count=count)
    except ValidationError:
        return None


def _tool_frame(payload: dict[str, Any], seq: "itertools.count[int]") -> AgentEvent | None:
    """工具上报的字典 → 事件帧。**多类帧的唯二分派点。**

    工具层不认识 `seq`，只负责把"发生了什么"塞进一个 `dict`；哪类帧、按什么顺序
    编号，全在这一层决定。目前两类：`node_grow`（树在长）与 `stage`（一件事在
    进行/结束了）。认不出来的类型返回 None 丢掉——预览与阶段都是附加信息，一帧
    坏掉不能打断正在跑的工具。
    """
    kind = payload.get("type")
    if kind == "node_grow":
        return _node_grow(payload, seq)
    if kind == "stage":
        return _stage_frame(payload, seq)
    return None
@dataclass
class _Waited:
    """装 `_await_quietly` 的返回值。

    与 `_ToolOutcome` 同一个理由：那个函数是 async generator（要一边等一边 yield
    心跳帧），而生成器没法 `return` 一个值。
    """

    value: Any = None


async def _await_quietly(
    awaitable: "asyncio.Future[Any] | Any",
    seq: "itertools.count[int]",
    out: _Waited,
) -> AsyncIterator[HeartbeatEvent]:
    """等一件事完成，期间按 `HEARTBEAT_INTERVAL_SECONDS` 补心跳帧。

    只该包在**会产生长静默**的等待上（模型调用、工具内部执行）。心跳不带业务
    含义，真实进度仍由 `stage` / `tool_start` / `node_grow` 负责。

    被等的 awaitable 自己负责超时（调用方传的是 `asyncio.wait_for(...)`），这里不
    另设时限：两处都判超时的话，哪个先到就成了第二份真相。
    """
    task = asyncio.ensure_future(awaitable)
    silence_started = time.monotonic()
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_INTERVAL_SECONDS)
            if task in done:
                # 在这一句里抛出被等者的异常（超时、上游失败），与直接 await 一致
                out.value = task.result()
                return
            yield HeartbeatEvent(
                seq=next(seq),
                waited_ms=int((time.monotonic() - silence_started) * 1000),
            )
    finally:
        # 客户端中途断开时调用方会被关掉：不取消这个任务，模型请求还在跑、还在计费。
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _run_tool(
    call: ToolCall,
    ctx: RunContext,
    registry: ToolRegistry,
    *,
    seq: "itertools.count[int]",
    timeout: float,
    outcome: _ToolOutcome,
) -> AsyncIterator[AgentEvent]:
    """执行一次工具，同时把工具内部的增量转成事件推出去。

    `run()` 是 async generator：它 `await` 工具执行的这段时间里**一帧都发不出去**。
    深层生成要发十几次模型调用，全憋在里面就是十几秒空屏，所以工具内部的上报
    只能走队列——工具往队列里放，这里一边等工具一边把队列里的东西转成帧。

    结果与异常也塞进同一条队列，好处是只有一个等待点：工具抛错时已经收到的预览
    帧不会丢（它们排在异常之前），超时也只需要取消这一个任务。超时语义与原先
    `wait_for` 包住整次执行一致：预算按墙钟算，产出预览帧的时间也算在里面。
    """
    updates: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def emit(payload: dict[str, Any]) -> None:
        updates.put_nowait(("emit", payload))

    async def worker() -> None:
        try:
            result = await _execute_once(call, ctx, registry)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 原样交回主循环上抛，别在这里改类型
            updates.put_nowait(("error", exc))
            return
        updates.put_nowait(("result", result))

    ctx.emit = emit
    task = asyncio.create_task(worker())
    deadline = time.monotonic() + timeout
    silence_started = time.monotonic()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome.timed_out = True
                return
            try:
                # 按心跳间隔切分等待：工具里可能连着跑十几次模型调用，中途只有
                # `node_grow` 偶尔冒一帧，其余时间同样是几十秒的空档。
                kind, payload = await asyncio.wait_for(
                    updates.get(), timeout=min(remaining, HEARTBEAT_INTERVAL_SECONDS),
                )
            except (asyncio.TimeoutError, TimeoutError):
                if deadline - time.monotonic() <= 0:
                    outcome.timed_out = True
                    return
                # 没到截止时间，只是又静默了一个心跳周期
                yield HeartbeatEvent(
                    seq=next(seq),
                    waited_ms=int((time.monotonic() - silence_started) * 1000),
                )
                continue
            silence_started = time.monotonic()
            if kind == "emit":
                event = _tool_frame(payload, seq)
                if event is not None:
                    yield event
                continue
            if kind == "error":
                outcome.error = payload
            else:
                outcome.result = payload
            return
    finally:
        # 通道只在这段执行期间有效：留着它，下一次工具的增量就会推进一个已经
        # 没人读的队列，控制器看见的是一串永远不出现的帧。
        ctx.emit = None
        task.cancel()
        # 已结束的任务再 cancel 是空操作；这里等它收尾，避免留下
        # "Task was destroyed but it is pending" 和一个还在跑的模型请求。
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _execute_once(call: ToolCall, ctx: RunContext, registry: ToolRegistry) -> ToolResult:
    """带两级幂等 + 一道重复判据的执行。

    call_id 幂等是协议要求的：模型偶尔会把同一个 tool_call 重复发一遍。
    参数级去重是防"同一轮里两个不同 id 但参数完全一样"的调用——实测模型
    并行发起两个 node_expand(n1) 是会发生的事，执行两次就会加两遍子节点。

    最前面那道 `duplicate_call`（防线一）是 `max_tool_calls` 提到 50 之后新增的：
    次数闸放开以后，"同一个调用连着发 20 次"不再会被额度自然掐断，只能在这里认。
    """
    key = _dedupe_key(call)

    cached = ctx.executed.get(call.call_id)
    if cached is not None:
        cached_key, cached_result = cached
        if cached_key != key:
            # 同一个 call_id 换了参数是协议违规。返回旧结果会让模型以为新参数
            # 生效了，而实际上没有任何改动发生。
            return ToolResult(
                ok=False,
                content=f"call_id {call.call_id} 已经用于另一次参数不同的调用，请换一个新的 call_id 重试。",
                data={"code": "call_id_conflict"},
            )
        return cached_result

    # 防线一排在只读缓存**之前**，顺序是有意的：缓存命中会返回 `ok=True` 的旧结果，
    # 模型读到的就是"又查成功了"，它根本没有机会知道自己在重复。判在这里，它拿到的
    # 是一句能改行为的话（"结果在上一条 tool 消息里"）。
    anchor = _duplicate_anchor(ctx, key)
    if anchor is not None:
        result = ToolResult(
            ok=False,
            content=(f"这次调用与 {anchor} 完全相同（工具 {call.name}、参数一字不差），"
                     "没有执行：结果就在上一条工具结果里。"
                     "请直接用它；如果确实要再跑一次（比如材料变了），中间先做一次别的调用，"
                     "或者换一个不同的参数。"),
            data={"code": "duplicate_call", "duplicate_of": anchor},
        )
        ctx.executed[call.call_id] = (key, result)
        return result

    spec = registry.get(call.name)
    readonly = spec is not None and spec.readonly
    if readonly:
        # 只读工具按参数去重是安全的：同样的查询必然得到同样的结果。
        # 写操作不能这样——即使参数一字不差，第二次执行时树已经变了，
        # 返回上次的结果等于谎报成功（实测：改成 X、Y、再改回 X，第三次被
        # 缓存吃掉，模型以为改成功了，实际停在 Y）。
        hit = ctx.dedupe.get(key)
        if hit is not None:
            ctx.executed[call.call_id] = (key, hit)
            return hit

    result = await registry.execute(call, ctx)
    ctx.executed[call.call_id] = (key, result)
    if readonly:
        ctx.dedupe[key] = result
    # 只记成功的：参数一字不差的**重试**是模型对失败（超时、节点已不存在）的
    # 正常恢复动作，把它判成重复等于在它最需要重试的时候堵住它。
    if result.ok:
        ctx.record_call(call.call_id, key)
    return result


async def run(
    request: AgentRunRequest,
    *,
    provider: ToolProvider | None = None,
    tools: ToolRegistry | None = None,
    on_close: Callable[[DiagramContent | None], None] | None = None,
    on_checkpoint: CheckpointFn | None = None,
    first_seq: int = 1,
) -> AsyncIterator[AgentEvent]:
    """跑一次多轮工具循环，边跑边发事件。

    `first_seq` 只给 `router._frames` 用：它在调这里之前可能已经发过一帧
    （材料入库后的 `sources`），所以要接着那个号往下编，否则两帧都叫 `seq=1`。
    这一层只做一件事：**在生成器被关掉时把 `ctx.candidate` 交出去**。循环本体在
    `_loop` 里，一行都没改。

    为什么必须在这里交、而不是让调用方从事件流里拼：

      - 被取消时最后那棵树**不在任何一帧里**。`edit` / `expand` 的改动只以 op
        批次下发，客户端手里只有一堆增量，拼不回整树；而 `ctx.candidate` 是
        `apply_ops` 的整树兜底，它一定是完整、校验过的。
      - `finally` 覆盖三条出路：正常收尾、抛异常、被 `aclose()` / `task.cancel()`
        关掉。取消路径最容易漏——那正是"关掉页面那 40 秒白花"的那一条。

    回调是**同步**的，不给调用方留 `await` 的机会：任务被取消时，`finally` 里第一个
    `await` 会立刻再抛一次 `CancelledError`，唯一要做的那件事（落草稿）反而被跳过。
    落库是本地 SQLite 写，微秒级，同步调用不会把事件循环卡住——与
    `_intent_rule` 每轮读一次库同一个理由。

    `on_close` 不传时行为与加它之前**逐字相同**（连一次库调用都没有），单测与
    直接调 `run()` 的代码不受影响。

    `on_checkpoint` 是同一件事的**中途**版本：`on_close` 只在结束时有一次机会，
    而一轮生成里有很长一段（写说明，实测 26 秒上下）是"树已经能读了但还没跑完"。
    这段时间进程被杀 / 重启 / 合盖，`finally` 根本没跑到，那一轮连半棵树都不留。
    工具在那一刻调一次 `ctx.checkpoint(...)`，这里把它交给调用方落库。

    它**不走同步回调**：这里不是取消路径（工具正常跑到那一步才调），而落库要把整
    棵树序列化再写 SQLite，同步做等于拿事件循环去等写锁——同进程的其它 SSE 流会
    跟着卡住。所以它是 `async` 的，调用方（路由层）自己把阻塞的写交给线程池。
    """
    # 「这次图型是怎么定的」随请求一起进来（`diagram_intent.apply` 在开流前写好）。
    # 直接构造请求的调用方没经过那一层，两个私有属性都是 `None`——那表示"这一层
    # 没跑"，不是"模型决定的"。工具按这个区别决定要不要往 `data` 里补 `source`。
    intent = diagram_intent.of(request)
    ctx = RunContext(
        request=request,
        candidate=request.snapshot,
        sources_by_id={source.id: source for source in request.sources},
        diagram_source=intent.source,
        diagram_stripped=intent.stripped,
        checkpoint=on_checkpoint,
    )
    try:
        async with contextlib.aclosing(
            _loop(request, ctx, first_seq=first_seq, provider=provider, tools=tools)
        ) as events:
            async for event in events:
                yield event
    finally:
        if on_close is not None:
            on_close(ctx.candidate)


async def _memory_hints(request: AgentRunRequest) -> str:
    """这一次生成要注入的记忆那一段；没有命中或任何失败都返回**空串**。

    三件事都在这里定死：

      1. **进线程**：`store.prompt_for` 里有一次同步的 embedding HTTP（0.3–1 秒）。
         直接 await 会把整个事件循环卡住——同一台服务器上别的用户那条已经在流的
         SSE 会跟着停。
      2. **失败不抛**：记忆检索是附属动作。它抛出去会让一次本来正常的生成变成
         `agent_failed`，而用户要的是那张图。
      3. **空串**：没有命中时返回空串而不是"（暂无记忆）"，调用方整段跳过。一个
         空的小标题会让模型以为"这个人的记忆是空的"是一种需要它处理的输入。
    """
    try:
        return await asyncio.to_thread(memory_store.prompt_for, request)
    except Exception as exc:  # noqa: BLE001 — 见 docstring 第 2 条
        logger.warning("记忆检索失败（%s），这次不注入记忆", type(exc).__name__)
        return ""


async def _loop(
    request: AgentRunRequest,
    ctx: RunContext,
    *,
    first_seq: int = 1,
    provider: ToolProvider | None = None,
    tools: ToolRegistry | None = None,
) -> AsyncIterator[AgentEvent]:
    """多轮工具循环本体。
    终止只有四种：模型以 stop 收尾（done）、预算耗尽（error）、上游失败（error）、
    工具累计失败超过 `max_repairs`（error）。
    不存在"静默截断后假装成功"——那会让前端拿到一棵看起来正常、实际缺了一半
    的树，比直接报错难排查得多。

    预算记账只有一个账本（`RunContext.model_calls_used`）：外层 provider 调用与
    工具内部的 `chat_async` 调用共用它，且都在**请求发出之前**占位。分两处判、
    两处记，就会出现"判的时候以为还有额度"的超支。
    """
    # 演示模式：机器上没有模型密钥时（评审演示、新人上手）走 agent 也要能出一张图，
    # 否则这里是 `chat_async` 的第一句 `GenerationError`，一张图都生成不出来。
    #
    # 判断放在这个函数的入口，而不是 `_generate` / `_generate_deep`：那两个是工具层
    # 各自的入口，写两遍就是同一个判断两处真相，而且外层循环会先花掉一次真正的
    # 模型调用才轮到工具。
    #
    # 只认显式的 `demo`（`.env.example` 就是这么发的）。缺省值**不**取 demo：旧路径
    # 那套「没配置就走 demo」的默认照搬到 agent 上，等于把默认路径静默降级成占位图，
    # 而 agent 正是要成为默认的那条路。
    if os.getenv("MINDMAP_LLM_MODE", "").strip().lower() == "demo":
        # 占位内容复用旧路径的 `_demo_content`：同一份占位结构只该有一个定义，
        # 两处各写一份，改文案时必然只改一处。
        #
        # **按请求的图型产出**（`request.diagram_type`，`None` 就是树）：演示模式的
        # 用途是没有密钥的机器上也能看到一张图，而恒为树的占位图演示不了鱼骨与
        # 流程——界面上选了"流程图"却出来一棵导图，用户会以为这个功能坏了。
        # 图型已经由 `apply_diagram_intent` 在开流前解析过（下拉 / goal 里的话 /
        # 模型默认），所以这里读到的是**这次真正要的图型**。
        demo = _demo_content(request.goal, request.diagram_type or "tree")
        # 也记到 ctx 上：草稿要按"这次运行产出的那棵树"落库，占位树同样是产出。
        # 不写这里的话，演示模式的 run 会以"没产出"收尾，回放时前端只看到
        # 一句"模型这次没有产出导图"。
        ctx.publish_tree(demo)
        demo_seq = itertools.count(first_seq)
        yield StageEvent(seq=next(demo_seq), stage="demo", detail="演示模式，不调用模型")
        yield SnapshotEvent(seq=next(demo_seq), content=demo)
        yield DoneEvent(seq=next(demo_seq), content=demo, usage={}, mode="demo")
        return

    active_provider = provider or default_provider(deep_thinking=request.deep_thinking)
    # 意图决定模型看得到哪些工具：expand 下根本没有 mindmap_edit，
    # 它也就不会去试"删掉另一个分支"。
    #
    # `diagram_type` 一起传下去：用户在图型下拉里选了什么，生成的工具定义就该
    # 跟着变（那个参数不再下发给模型）。`expand` / `edit` 的工具集里没有生成
    # 工具，这个值对它们没有影响。
    registry = tools if tools is not None else build_registry(
        request.intent, has_sources=bool(request.sources),
        diagram_type=request.diagram_type,
    )
    # 记忆检索：**一次生成只检索一次**，就在这一行。
    #
    # 位置不能换到 `_system_text` 里：那个函数每一轮都会被 `_refresh_outline`
    # 调一次，检索放进去就是 N+4 次 embedding（骨架 1 次 + 结构 1 次 + detail 2 次
    # + 工具循环 N 轮）。每次 embedding 0.3–1 秒，10 轮的运行会因此多等好几秒，
    # 而那几秒比这段记忆值钱得多。
    #
    # 也不能换到 `run()` 里：那里在 demo 模式分支之前（`_loop` 开头就 return 了），
    # 演示模式不发 system，检索一次纯属浪费。
    #
    # query 是 `goal` + 材料标题（见 `store.query_text`），所以它天然是"这次的主题"。
    ctx.memory_hints = await _memory_hints(request)
    # 风格画像：**一次运行读一次**，拼进 user 末尾（见 `_initial_messages`）。
    #
    # 它不进 `_system_text`：那个函数每轮被 `_refresh_outline` 调一次，而 user
    # 那条从头到尾不变——画像也是这样一份常量。放进 system 还会顺带每轮多查一次库。
    #
    # 位置在这一行（`_memory_hints` 之后）只是顺序：两者互不依赖，检索是会等的
    # 那次（0.3–1 秒），读表是立即的。
    ctx.style_hints = _style_text()
    if ctx.style_hints:
        logger.info("风格画像已注入 user 末尾（%d 字）", len(ctx.style_hints))
    budget = request.budget
    messages = _initial_messages(request, ctx.memory_hints, ctx.style_hints)
    seq = itertools.count(first_seq)
    started = time.monotonic()
    tool_calls = 0
    failures = 0
    nudges = 0
    usage_total: dict[str, Any] = {}
    last_call: ToolCall | None = None
    # 收尾帧要带的两个字段。写成循环里的局部变量而不是在收尾那一刻再算：
    #   - `model_name` 是 provider 在**发请求那一刻**决定的（配置缺失时还有回退），
    #     预算/空转这两条收尾路径上没有"当前 provider"可问，只能一路带着；
    #   - `last_reasoning` 取**最后一次**模型响应的思考过程。预算耗尽与空转结束都是
    #     "这一轮跑完了、可以交付了"，与模型自己收尾的 done 帧同形，少了它就会出现
    #     "同样是成功，一次有思考过程一次没有"，而用户不知道差别来自哪种收尾。
    model_name: str | None = None
    last_reasoning = ""
    # 服务端回报的模型名，与 `model_name` 不是一回事：一个是请求里写出去的意图，
    # 一个是响应体 `model` 说它实际跑的（实测请求 `deepseek-chat`、服务 `deepseek-flash`）。
    # 一路带着的理由与 `model_name` 相同：预算/空转那两条收尾路径上没有"当前 provider"可问。
    served_model: str | None = None
    # 防线二的状态：树的内容指纹 + 连续多少次调用没让它变。
    #
    # 初值在**进入循环之前**取，而不是在第一轮之后：`request.snapshot` 是用户
    # 现在的树，模型第一次调用若只是检索，那就已经是"没有改变树"的第一次了。
    tree_signature = _tree_signature(ctx)
    no_progress = 0

    yield StageEvent(
        seq=next(seq),
        stage="准备",
        detail=f"{request.intent} 意图，{len(registry.names())} 个工具可用",
    )

    while True:
        limit = _budget_limit(budget, tool_calls, started)
        if limit is not None:
            yield _budget_stop(
                ctx, seq, usage_total, limit, tool_calls, last_call,
                model=model_name, served_model=served_model, reasoning=last_reasoning)
            return

        # 先占额度、再发请求。反过来做（调用成功后才记账）时，一旦工具内部
        # 也要调模型，额度会在工具执行期间被用掉而循环还不知道。
        if not ctx.reserve_model_call():
            yield _budget_stop(
                ctx, seq, usage_total, "max_model_calls", tool_calls, last_call,
                model=model_name, served_model=served_model, reasoning=last_reasoning)
            return

        # 每轮刷新大纲：模型必须看到上一轮产出节点的真实 id，否则它无法精确改
        _refresh_outline(messages, ctx)
        waited = _Waited()
        try:
            # 用剩余时间做超时，而不是让单次调用跑到 provider 自己的 60 秒上限：
            # 否则总时限形同虚设，一轮慢调用就能把预算耗尽而循环还在等。
            #
            # 用 `_await_quietly` 而不是直接 await：单轮首字延迟平时 5–10 秒、
            # 3 路并发下实测到过 25 秒，这段时间一帧都不发，链路空闲超时会掐断
            # 连接（见 HEARTBEAT_INTERVAL_SECONDS 的注释）。
            async with contextlib.aclosing(_await_quietly(
                asyncio.wait_for(
                    active_provider.complete_tools(messages, registry.specs(), budget=budget),
                    timeout=max(0.1, _remaining_seconds(budget, started)),
                ),
                seq, waited,
            )) as waiting:
                async for beat in waiting:
                    yield beat
            completion = waited.value
        except asyncio.CancelledError:
            # 必须原样抛出：吞掉它会让"用户点了取消"变成一次正常结束
            raise
        except (asyncio.TimeoutError, TimeoutError):
            yield _budget_stop(
                ctx, seq, usage_total, "max_seconds", tool_calls,
                model=model_name, served_model=served_model, reasoning=last_reasoning)
            return
        except ModelNotConfigured as exc:
            # 单独一条：这一类失败重试没有意义，界面要给的是"去配模型"而不是
            # "再试一次"（见 `ModelNotConfigured` 的 docstring）。
            yield _deliver_or_fail(
                ctx, seq, usage_total,
                code="model_not_configured", message=str(exc),
                tool_calls=tool_calls, model=model_name, served_model=served_model, reasoning=last_reasoning,
            )
            return
        except GenerationError as exc:
            # 日志由 `_deliver_or_fail` 统一记（它带 `log_code`，两条分支都记一次）。
            # 这里再写一条会把同一次失败记成两条——两条都带 `model_calls_used`，
            # 长得像两次独立事件，按 code 数失败次数就会翻倍。
            yield _deliver_or_fail(
                ctx, seq, usage_total,
                code="generation_failed", message=str(exc),
                tool_calls=tool_calls, model=model_name, served_model=served_model, reasoning=last_reasoning,
            )
            return
        except Exception as exc:  # noqa: BLE001 — 留下摘要后仍交给路由处理，不改变异常语义
            _log_failure(
                ctx, tool_calls, code="agent_failed",
                message=f"{type(exc).__name__}: {exc}", level=logging.ERROR,
            )
            raise

        messages.append(completion.message)
        usage_total = _merge_usage(usage_total, completion.usage)
        # 每一轮都覆盖：收尾帧要的是**最后一次**响应，不是第一次。
        model_name = getattr(active_provider, "model_name", None)
        # 服务端回报的名字同样取最后一次。两条来源都试（provider 记的与 Completion 带的），
        # 哪个有值用哪个：假 provider 不会记在实例上，真 provider 两个都有。
        served_model = (getattr(active_provider, "served_model", None)
                        or completion.served_model)
        last_reasoning = str(completion.message.get("reasoning_content") or "").strip()

        if not completion.tool_calls:
            # 收尾前的一次性提醒：模型**自己**刚查出还有几支没展开，却直接收尾了。
            #
            # 判据只依赖它自己调 `audit_tree` 的结果（`ctx.audit_missing`），所以
            # 这不算代码替它判断"该不该继续"：它已经知道缺什么，这里只是把
            # "你知道吗"变成"那你补完再收"。**没体检过（None）时一个字都不加**——
            # 用户的请求可能就只是展开一个节点，那时缺 7 支是正常的，凭空要求
            # 补全等于代码替用户改需求。
            #
            # 只提醒一次（`audit_nagged`）：它听完还是要停，那是它的判断（可能
            # 它有别的理由，比如用户只是问了一句）；再催就变成代码在写这张图。
            #
            # 体检结论**不随后续写入失效**：模型审计完补了一支又收尾时，那句
            # "还有 2 条没有子节点"已经过时了，但提醒仍然是对的——它上一次的
            # 判断是"缺 2 支"，而它没有再说"现在不缺了"。多花一轮往返，换的是
            # "补到一半就收尾"不会静默通过。
            if (completion.finish_reason == "stop" and ctx.audit_missing
                    and not ctx.audit_nagged):
                ctx.audit_nagged = True
                reminder = (
                    f"你刚才的体检显示还有 {ctx.audit_missing} 条一级分支没有任何子节点。"
                    "请继续用 node_expand 展开它们；如果你判断就该停在这里，直接说明原因。"
                )
                messages.append({"role": "user", "content": reminder})
                yield StageEvent(seq=next(seq), stage="audit_pending", detail=reminder)
                continue
            if completion.finish_reason == "stop":
                # 模型一句"好的"就收工、一个工具都没调：接口层会正常返回 done，
                # 前端只能显示"模型这次没有产出导图"，看不出模型到底说了什么。
                # 把它的原话记进日志（截断），下次这类空结果才有得查。
                said = str(completion.message.get("content") or "").strip()
                reasoning = str(completion.message.get("reasoning_content") or "").strip()
                if ctx.candidate is None:
                    _log_failure(
                        ctx, tool_calls, code="no_tool_call",
                        message=f"模型没有调用任何工具就结束，原话：{said[:200] or '（空回复）'}",
                        level=logging.WARNING,
                    )
                    if nudges < MAX_NUDGES and tool_calls == 0:
                        # 整轮一个工具都没调过、也没有产出：这是模型把导图当"作文"
                        # 写了。把要求说死再给它一次机会；已经调过工具才开始写字的
                        # 情况不在此列（那是它可以收尾的形态，提醒只会打乱节奏）。
                        # 只提醒一次，叫不动就认，不在演示里空烧额度。
                        nudges += 1
                        messages.append({"role": "user", "content": _nudge_text(registry)})
                        continue
                yield DoneEvent(
                    seq=next(seq), content=ctx.candidate, usage=usage_total, mode="model",
                    # 没产出时把原话带上：页面上直接能看到模型说了什么，排障不用
                    # 登服务器翻 journalctl。有产出就不填，那是正常结果。
                    text="" if ctx.candidate is not None else _clip(said),
                    # 思考过程**成功时也留**：走 reasoner 时它正是用户勾「深度思考」
                    # 要看的东西，藏起来等于那个开关白勾了。截断上限见
                    # `REASONING_BYTE_LIMIT`（不是 `MODEL_TEXT_LIMIT`）。
                    reasoning=_reasoning_text(reasoning),
                    # 与两条收尾路径（预算耗尽 / 空转停止）同一个来源：`model_name`
                    # 是这一轮 provider 真正发出去的名字。
                    model=model_name,
                    # 服务端回报的那个名字（实测请求 chat、服务 flash）。两个都带上：
                    # 只有请求名时，用户看不出这次被路由到了别处。
                    served_model=served_model,
                )
            else:
                message = f"模型以 {completion.finish_reason or '未知原因'} 结束，且没有调用任何工具"
                # 已经有树就交树（标 partial），没有才报错：`length` 截断、内容
                # 过滤这些收尾方式，前面长出来的那棵树一样是能用的。
                yield _deliver_or_fail(
                    ctx, seq, usage_total,
                    code="unexpected_finish", message=message,
                    tool_calls=tool_calls, model=model_name, served_model=served_model, reasoning=last_reasoning,
                )
            return

        for call in completion.tool_calls:
            if tool_calls >= budget.max_tool_calls:
                # 同上：这一轮还没执行的工具不做了，但已经长出来的树要交出去。
                yield _deliver_or_fail(
                    ctx, seq, usage_total,
                    code="budget_exhausted", message=_BUDGET_HINT["max_tool_calls"],
                    limit="max_tool_calls",
                    tool_calls=tool_calls, call=call,
                    model=model_name, served_model=served_model, reasoning=last_reasoning,
                )
                return

            yield ToolStartEvent(
                seq=next(seq), call_id=call.call_id, name=call.name,
                args_summary=_args_summary(call),
            )
            # 已发起但超时或抛错的工具同样消耗一次调用，日志不能把它们记成零次。
            tool_calls += 1
            last_call = call
            ledger_before = ctx.model_calls_used
            extra_before = ctx.extra_model_calls
            had_candidate = ctx.candidate is not None
            begun = time.monotonic()
            outcome = _ToolOutcome()
            # 工具执行与它的增量上报并发进行：`_run_tool` 每收到一帧就 yield 出去，
            # 期间工具仍在跑（`node_grow` 就是靠这个在十几秒的生成里陆续上屏）。
            #
            # 用 `aclosing` 而不是裸 `async for`：客户端中途断开时 `run()` 会被关掉，
            # 不显式关这个子生成器，它的收尾（复位通道、取消工具任务）就要等垃圾
            # 回收才执行——那段时间工具里的模型请求还在跑、还在计费。
            async with contextlib.aclosing(_run_tool(
                call, ctx, registry, seq=seq,
                timeout=max(0.1, _remaining_seconds(budget, started)),
                outcome=outcome,
            )) as tool_events:
                async for event in tool_events:
                    yield event
            if outcome.timed_out:
                timeout_message = _budget_message(ctx, "max_seconds", tool_calls)
                # 超时也是一次「工具结束了」。少了这一帧，事件序列就是
                # `stage → tool_start → error`，前端那行工具永远停在「执行中」——
                # `max_repairs` 判停那条路径修过同一个问题，超时这里当时漏了。
                yield ToolEndEvent(
                    seq=next(seq), call_id=call.call_id, name=call.name, ok=False,
                    result_summary={
                        "code": "tool_timeout",
                        "message": timeout_message,
                    },
                    latency_ms=int((time.monotonic() - begun) * 1000),
                )
                # 超时前已经写进 `ctx.candidate` 的树要交出去。这一条从前是无条件
                # 发 error 的，而 `_run_tool` 超时会 `task.cancel()`——被取消那次
                # 工具内部的分片结果拿不回来，能救的只有"之前已经落到候选里的"。
                yield _deliver_or_fail(
                    ctx, seq, usage_total,
                    code="budget_exhausted", message=timeout_message,
                    limit="max_seconds",
                    tool_calls=tool_calls, call=call,
                    model=model_name, served_model=served_model, reasoning=last_reasoning,
                    log_code="tool_timeout",
                )
                return
            if outcome.error is not None:
                # 工具内部抛的异常：**这一轮已经产出过树时走统一收尾**，否则原样上抛。
                #
                # 从前这里一律上抛，由路由那三条 `except` 兜成 error 帧——而路由
                # 只看得到异常，看不到 `ctx`，于是"工具里模型挂了、但树上已经有东西"
                # 这条路仍然把半棵树丢了。`_deliver_or_fail` 存在的全部意义就是不丢这
                # 棵树，那么最后这条绕过它的路也得堵上，否则六条变七条、缺口仍在。
                #
                # 没产出时必须继续上抛：把一个"什么都没做成"的运行由这里改写成
                # `_deliver_or_fail` 的 error 帧，等于替路由做决定（它还要落库、
                # 发 DONE_FRAME），两条出口对同一件事各说一遍容易漂移。
                if ctx.produced and isinstance(outcome.error, (ModelNotConfigured, GenerationError)):
                    config_error = isinstance(outcome.error, ModelNotConfigured)
                    yield _deliver_or_fail(
                        ctx, seq, usage_total,
                        code="model_not_configured" if config_error else "generation_failed",
                        message=str(outcome.error),
                        tool_calls=tool_calls, call=call,
                        model=model_name, served_model=served_model, reasoning=last_reasoning,
                    )
                    return
                # 这里的 `_log_failure` 只覆盖"继续上抛"的那一半：走上面分支时
                # `_deliver_or_fail` 自己会记，记两次等于一次失败两条日志。
                _log_failure(
                    ctx, tool_calls, call=call,
                    code="generation_failed" if isinstance(outcome.error, GenerationError) else "agent_failed",
                    message=f"{type(outcome.error).__name__}: {outcome.error}",
                    level=logging.ERROR,
                )
                raise outcome.error
            if outcome.result is None:  # pragma: no cover — 三种出路必命中一种
                _log_failure(
                    ctx, tool_calls, call=call, code="agent_failed",
                    message="工具执行既没有结果也没有异常", level=logging.ERROR,
                )
                raise RuntimeError("工具执行既没有结果也没有异常")
            result = outcome.result
            # 对账：工具有没有自己调了模型却没先占额度（新写的工具最容易漏这一步，
            # 漏一次就多一次免费调用）。只在账本这一轮没动过时按 extra 增量补记，
            # 避免与 reserve 的正常记账重复计数——宁可多记，不可漏记。
            if (ctx.extra_model_calls > extra_before
                    and ctx.model_calls_used == ledger_before):
                ctx.model_calls_used += ctx.extra_model_calls - extra_before

            summary = _result_summary(result)
            if not result.ok:
                _log_failure(
                    ctx, tool_calls, call=call, code=summary["code"], message=summary["message"],
                )

            # ToolEnd 必须在这个函数返回之前发出去，且要在失败判停**之前**：
            # 否则那一次 tool_start 永远等不到配对帧，前端会一直显示"执行中"。
            yield ToolEndEvent(
                seq=next(seq), call_id=call.call_id, name=call.name, ok=result.ok,
                result_summary=summary,
                latency_ms=int((time.monotonic() - begun) * 1000),
            )
            if not result.ok:
                # 模型给错参数是常态，它看到错误后下一轮通常能改对；但一直错下去
                # 就是在烧调用，超过容忍次数直接收尾。计的是累计失败数（不是连续），
                # 成功的工具调用不会把它清零。
                #
                # `duplicate_call` 是**例外，不计入**：`max_repairs` 的语义是
                # "模型把参数写错了、改不过来"，而重复调用是"模型在原地打转"，
                # 那条由防线一拒绝、由防线二计数中止。把它计进来的直接后果是：
                # 默认 `max_repairs=1` 下，模型重复两次就掐死整个 run——一次可
                # 自我纠正的提示变成了终止条件，比它要修的问题更糟。
                if summary["code"] != "duplicate_call":
                    failures += 1
                    if failures > budget.max_repairs:
                        # 文案把两个数都写出来。原来只写"（上限 N 次）"，读起来像
                        # "第 N 次就停"，而判据是 `>`——`max_repairs=1` 实际允许
                        # 2 次失败。差一个的边界写在括号里没人看得懂，而它会让
                        # 下一个人（包括写这行的人）误判成"阈值太紧"。
                        message = (
                            f"工具执行已失败 {failures} 次，"
                            f"最多允许重试 {budget.max_repairs} 次，继续重试没有意义，已停止。"
                        )
                        # 与其它五条收尾同规矩：已经长出来的树交出去，别让它白跑。
                        yield _deliver_or_fail(
                            ctx, seq, usage_total,
                            code="too_many_failures", message=message,
                            limit="max_repairs",
                            tool_calls=tool_calls, call=call,
                            model=model_name, served_model=served_model, reasoning=last_reasoning,
                        )
                        return

            # 防线二：这次调用有没有让树变样。
            #
            # 位置选在 ToolEnd 之后：那一帧必须紧跟上一次 tool_start（前端靠配对
            # 结束"执行中"），中间插一条 stage 会让它多闪一下"在做什么"。
            #
            # 判据是整树内容指纹而不是"调用成功与否"：工具可能"成功"却什么都没做
            # （`update_node` 把 label 改成同一个值、`rag_query` 查到同样的片段），
            # 那正是 50 次空转里最常见的一半。
            signature = _tree_signature(ctx)
            if signature == tree_signature:
                no_progress += 1
            else:
                no_progress = 0
                tree_signature = signature
            if no_progress == NO_PROGRESS_WARN:
                yield _no_progress_frame(ctx, seq, no_progress, tool_calls)
            if no_progress >= NO_PROGRESS_ABORT:
                yield _no_progress_stop(
                    ctx, seq, usage_total, tool_calls, no_progress,
                    model=model_name, served_model=served_model, reasoning=last_reasoning)
                return

            # 候选树从"没有"变成"有"（只可能是 mindmap_generate_deep 成功）时补一帧
            # snapshot：前端立刻就能渲染候选内容，不必等到 done。
            # 位置选在 ToolEnd 之后、OpsEvent 之前：前端收到 tool_end 会先把工具
            # 结果落进状态，紧随其后的整树快照与它一致；若放到 OpsEvent 之后，
            # 同一批改动会先在增量里应用一次、再被整树覆盖一次。
            # content 一定是完整校验过的树：ctx.candidate 只由分片生成与
            # apply_ops 的整树兜底写入，半成品进不来。
            if not had_candidate and ctx.candidate is not None:
                yield SnapshotEvent(seq=next(seq), content=ctx.candidate)

            # 工具若产生了 op 批次，单独发帧：前端据此增量应用，不必等整棵树。
            # 用 data 而不是解析 content，省掉一次 JSON 往返，也避免把回给模型的
            # 自然语言当成协议数据。
            for batch in result.data.get("batches") or []:
                yield OpsEvent(seq=next(seq), call_id=call.call_id, batch=batch)

            messages.append({
                "role": "tool",
                "tool_call_id": call.call_id,
                "content": result.content,
            })

