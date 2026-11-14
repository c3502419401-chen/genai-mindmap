"""本地演示和 OpenAI Chat Completions 兼容服务接入。"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
from pydantic import ValidationError

from app.contracts.diagnostic import Diagnostic, DiagnosticLevel
from app.contracts.mindmap import (
    GENERATED_LABEL_MAX_CHARS,
    MAX_TREE_DEPTH,
    MAX_TREE_NODES,
    MindMapContent,
    Node,
    clip_label,
)
from app.contracts.source import Source, SourceUsage
from app.generation.budget import INITIAL_MAX_DEPTH, INITIAL_MAX_NODES
from app.optimization.source_refs import EvidenceSpan, verify_source_refs

# 单次模型调用的超时（秒）与响应体上限（字节）：超时避免请求无限挂起，
# 上限避免异常响应把内存读满。
#
# 超时 60 → 600。60 秒是照「一句话主题」的短输出拍的：实测 12106 字材料
# → 13243 token 输出 → 48.6 秒，已经贴着上限；慢速档 97 tok/s 时同样的材料
# 要 136 秒。超时的代价是整次请求报废（不是"稍后重试"），而模型本身仍在
# 计费，所以它必须由物理时间决定，不能由产品口味决定。
_TIMEOUT_SECONDS = 600.0
_MAX_RESPONSE_BYTES = 2_000_000

# 输出 token 上限。服务端默认 8192，实测 12106 字材料 → 13243 token 输出，
# 16384 只剩 3000 token 余量；而 DeepSeek 接受 max_tokens=65536（HTTP 200）。
# 按每节点约 25 token 算，65536 装得下 2600 个节点，高于契约允许的 1000 个，
# 所以输出上限不再是内容被截断的原因——真被截断时 `finish_reason=length`
# 会明确报错，不会静默交付半棵树。
#
# **这个数是默认值，不是唯一值**。DeepSeek 系列接受 65536，qwen 系列的上限是
# 32768，混元/本地部署的兼容端点各不相同。写死一个数意味着换一个模型就整条路
# 报 400，而报错文案是"模型服务返回 HTTP 400，请检查服务配置"——用户无从知道
# 该改哪个环境变量。所以：
#
#   1. `MINDMAP_LLM_MAX_TOKENS` 覆盖它（`max_tokens_cap()` 读）；
#   2. 仍然超限时，从服务端的 400 正文里解析
#      `Range of max_tokens should be [1, N]`，学到 N 并**自动降级重试一次**
#      （`_chat_once`）。学到值只在进程内存里，不写任何配置文件：它是服务端的
#      事实，不是用户的配置，落盘之后没人知道该在什么时候删掉它。
MAX_OUTPUT_TOKENS = 65536

# 覆盖上面那个默认值的环境变量。
MAX_TOKENS_ENV = "MINDMAP_LLM_MAX_TOKENS"

# 服务端在 400 里自报的上限（进程内学习值）。`None` = 还没学到任何东西。
# 一旦学到就一直是它与配置值取小：同一进程里的每一个出口（生成、展开、agent
# 轮次）都该发同一个数，否则"三处各写一个上限"这个老问题会以另一种形式回来。
_LEARNED_MAX_TOKENS: int | None = None

# 服务端 400 的正文形状。实测 DeepSeek 写的是
# `Range of max_tokens should be [1, 65536]`；OpenAI 兼容端点还见过
# `max_tokens is too large: 65536. This model supports at most 32768`。
# 两种都认，任何一种解析不出来就不降级（宁可抛原来那个错误，也不要瞎猜一个数）。
_MAX_TOKENS_RANGE = re.compile(r"[Rr]ange of max_tokens should be \[\s*(\d+)\s*,\s*(\d+)\s*\]")
_MAX_TOKENS_AT_MOST = re.compile(r"supports at most (\d+)")


def parse_max_tokens_limit(text: str) -> int | None:
    """从服务端的错误正文里解析出它真正接受的 `max_tokens` 上界。

    解析不出来返回 `None`——调用方据此**不降级**。猜一个数比报错更糟：猜大了
    再撞一次 400（用户多等一轮），猜小了静默削掉输出能力（截断率上升，而原因
    在日志里看不出来）。
    """
    body = text or ""
    match = _MAX_TOKENS_RANGE.search(body)
    if match:
        upper = int(match.group(2))
        return upper if upper > 0 else None
    match = _MAX_TOKENS_AT_MOST.search(body)
    if match:
        upper = int(match.group(1))
        return upper if upper > 0 else None
    return None


def configured_max_tokens() -> int:
    """`MINDMAP_LLM_MAX_TOKENS` 的值；没配或不是正整数时回落到默认值。

    非法值不抛错：这是一个可选调优项，写错时的正确处置是"按默认值继续"，而不是
    让整次生成失败在一条环境变量上（与 `source_mode()` 对非法取值的处理同一条
    道理）。只 warning 一次，避免每次拼请求都刷一行日志。
    """
    raw = os.getenv(MAX_TOKENS_ENV, "").strip()
    if not raw:
        return MAX_OUTPUT_TOKENS
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value > 0:
        return value
    if raw not in _WARNED_MAX_TOKENS:
        _WARNED_MAX_TOKENS.add(raw)
        logger.warning("%s=%r 不是正整数，按默认值 %d 处理",
                       MAX_TOKENS_ENV, raw, MAX_OUTPUT_TOKENS)
    return MAX_OUTPUT_TOKENS


# 非法环境变量的告警去重集合（口径同 `_WARNED_SOURCE_MODES`）。
_WARNED_MAX_TOKENS: set[str] = set()


def max_tokens_cap() -> int:
    """当前进程允许发出的 `max_tokens` 上限：配置值与服务端学习值取小。

    三个出口都要用这一个函数：`chat_async`（工具内的生成/展开）、
    `provider.complete_tools`（agent 轮次），以及任何将来新增的模型调用。
    各写一处 `min(...)` 的结果，是三条路上限慢慢分叉，而分叉的表现只是
    "某条路径偶尔 400"。
    """
    cap = configured_max_tokens()
    if _LEARNED_MAX_TOKENS is not None:
        cap = min(cap, _LEARNED_MAX_TOKENS)
    return cap


def learn_max_tokens(value: int) -> None:
    """记下服务端自报的上限（只在内存里，见 `_LEARNED_MAX_TOKENS` 的注释）。"""
    global _LEARNED_MAX_TOKENS
    if value <= 0:
        return
    if _LEARNED_MAX_TOKENS is None or value < _LEARNED_MAX_TOKENS:
        if _LEARNED_MAX_TOKENS != value:
            logger.warning("模型服务自报 max_tokens 上限为 %d，本次进程后续请求将降到该值",
                           value)
        _LEARNED_MAX_TOKENS = value


def reset_learned_max_tokens() -> None:
    """清掉学习到的上限。只给测试用——生产里它是服务端的事实，不该被手动抹掉。"""
    global _LEARNED_MAX_TOKENS
    _LEARNED_MAX_TOKENS = None

# 模型上下文（输入侧）上限，单位 token。**这是保守值，不是实测边界**：
# 探测 api.deepseek.com 需要密钥，而本机没有可用密钥（占位密钥只拿到 401），
# 无法把输入压到边界看它在哪个 token 数上返回 400。DeepSeek 标称 128K，
# 但"标称窗口"与"扣掉系统提示词、输出预算之后还能塞多少"不是一回事，
# 取不到一半留足余量。60000 token 约合 9 万汉字，实测 12106 字材料只用到
# 一万多输入 token，正常材料离这条线很远。
CONTEXT_TOKEN_LIMIT = 200000

logger = logging.getLogger(__name__)

# 「材料怎么进 prompt」的开关：`full` 把每份来源的正文整份拼进去，`rag` 只给清单。
#
# 默认 `rag`。这不是性能优化，是**形态切换**：`full` 下模型自己就能扫全文，
# `rag_query` 只是"帮我定位该引用哪一段"的辅助；`rag` 下模型只看得见检索回来的
# 片段，检索从辅助变成了唯一入口。两种形态的提示词、引用来源、失败方式都不同，
# 所以是一个显式的两态开关，而**不是**"材料小就走 full"的自适应——行为随材料
# 大小跳变是最难排查的一类 bug：同一份请求换个字号，结论就变了。
#
# `rag` 形态下"正文不出进程"这句话**不成立**，准确说法见
# `agent/tools._material_inputs` 的 docstring：进 prompt 的只有清单 + 检索片段 +
# 查询扩展那每份 200 字（`MINDMAP_QUERY_EXPANSION`，默认关）+ **材料自带的章节
# 标题**（`agent/tools._skeleton_outline` 给骨架的那份目录）；离开进程的还有
# 混合检索的 embedding 请求（两种形态都一样）。
#
# 别把最后那一项读成"正文也进去了"：目录是原文的子集，只是一级标题行（一条最多
# 40 字），覆盖率不到 60% 的材料一条都不进。准确口径是"正文**段**不进，标题行
# 作为结构线索会进"——分片生成下覆盖度由骨架决定，看不到结构的骨架只能凭主题猜
# 分块，材料里它没想到的章节就永远没人问（`docs/RAG-DECISIONS.md` 第 5 节）。
SOURCE_MODE_ENV = "MINDMAP_SOURCE_MODE"
SOURCE_MODE_FULL = "full"
SOURCE_MODE_RAG = "rag"
DEFAULT_SOURCE_MODE = SOURCE_MODE_RAG

# 非法取值只告警一次。`source_mode()` 每拼一次提示词就调一次（一次请求里十几次），
# 每次都 warning 会把日志刷满、把真正要看的东西埋掉——与 Neo4j `id()` 弃用通知
# 的处理同一个道理（`docs/RAG-DECISIONS.md` 9.3）。
_WARNED_SOURCE_MODES: set[str] = set()


def source_mode() -> str:
    """这次走哪种形态。只认 `full` / `rag`，其余（含未设置）一律按默认值处理。

    不做记忆化：环境变量在一个进程里是常量，但测试要能逐条切换（`patch.dict`），
    缓存会把"这条用例显式设成 full"变成"取决于上一条用例跑过什么"。

    非法值按 `rag` 而不是 `full` 兜底：`full` 会把整份正文塞进 prompt，是一个
    **有成本、有泄露面**的行为；配置写错时落到更省的那一侧，代价只是多调几次
    检索，反过来则是白花 token 且没人预期到。
    """
    raw = os.getenv(SOURCE_MODE_ENV, "").strip().lower()
    if raw in (SOURCE_MODE_FULL, SOURCE_MODE_RAG):
        return raw
    if raw and raw not in _WARNED_SOURCE_MODES:
        _WARNED_SOURCE_MODES.add(raw)
        logger.warning(
            "%s=%r 不是合法取值（只认 %r / %r），按默认值 %r 处理",
            SOURCE_MODE_ENV, raw, SOURCE_MODE_FULL, SOURCE_MODE_RAG, DEFAULT_SOURCE_MODE,
        )
    return DEFAULT_SOURCE_MODE


# 「内容可以自由展开、引用必须标出处」——**三份生成提示词共用的唯一一句契约**。
#
# 上一版的契约是"检索不到就不要写，空着是正确结果"。代价实测过：同一份 12392 字
# 材料，全文可见时覆盖率 100%，检索形态只有 28.8%，而且模型会写出"材料中没有相关
# 内容：未检索到任何原文依据"这种节点——产品要的是一张完整的导图，不是"材料里有
# 什么就画什么"。
#
# 新契约把两件事拆开：**内容**允许模型凭自己的知识补全，**出处**不许冒充材料。
# 两半必须同时说，只说一半都会坏事：
#   - 只说"自由展开"：模型会给自主补充的内容挂上编造的引用，"标明出处"退化成
#     "随便挂一个看起来相关的引用"；
#   - 只说"必须依据材料"：模型退回"没检索到就不写"，正是这次要消灭的那种空心树。
#
# 措辞只此一份。三份提示词（无来源 / `full` / `rag`）各自再补自己那种形态的细节
# （引用怎么抄、检索不到怎么处理），但这一句必须逐字相同——它约束的是同一个行为，
# 三种说法会让"到底能不能补全"随形态漂移，而那种漂移在界面上看不出来。
CONTENT_FREEDOM_CONTRACT = (
    "内容可自由展开：材料没覆盖的方面，按你的知识补全，不要留空节点。"
    "但凡引用、转述、概括材料的，必须标出处；自主补充的内容不要冒充材料。"
)

# 一次性生成完整导图的提示词（非流式接口、流式接口的第二段都用它）。
#
# 输入有两种形态，必须分开说：早先只写了「整理」和「不要捏造」，模型对
# 一句话的主题（如「机器学习」）就严格照做——那四个字里确实没有可整理的
# 分支，于是只回一个光杆根节点，被 MIN_BRANCHES 拦下。而这个产品本来的
# 用法就是「说一个主题，给一张图」，所以要把「围绕主题展开框架」和
# 「带材料的整理」区分开。
#
# 两种形态在**深度与覆盖**上的规则现在是同一条（能拆就拆、材料没覆盖的按知识
# 补全），差别只剩「出处怎么标」：有材料的那一份由 `SOURCED_DETAIL_PROMPT` /
# `RAG_SOURCE_PROMPT` 补上引用纪律。上一版把差别写成"后者不许凭空加内容"，
# 实测把整棵树压成了材料的目录（覆盖率 28.8%），这一段因此改写。
DETAIL_PROMPT = (
    "把用户输入变成一张思维导图。只返回一个 JSON 对象，不要代码围栏。"
    "格式为 {\"title\":\"标题\",\"root\":{\"id\":\"唯一id\","
    "\"label\":\"节点文字\",\"detail\":\"节点说明\",\"children\":[]}}。"
    "每个节点均含 id、label、detail、children，"
    # 节点是**两层**的：label 短、detail 长。这一条不改任何结构规则，它改的是
    # "一个字放在哪个字段"。上一版只有一个 label，模型把整句解释塞进去，实测
    # 240 个节点的树里 label 普遍 20–30 字，画布上挤成一团——而画布只截断显示，
    # 内容其实一个都没少，少的是"短标题 + 长正文"这个形态。
    #
    # 两个上限都写死成可读的字数而不是 token 数：模型对"16 字"的理解比对
    # "11 token"稳定得多。detail 的 2–3 句同样是可执行的要求，"写详细一点"
    # 这类形容词它只能自己发挥。
    f"label 是一句话短语，不超过 {GENERATED_LABEL_MAX_CHARS} 字，不带句号；"
    "detail 是给这个节点看的说明，2–3 句，可以带数字与条件；"
    "画布上只显示 label，detail 是点开之后看到的正文。\n"
    # 这里的两个数字取自契约常量：写死过一次，结果契约从 300 放到 1000 之后
    # 提示词还在叫模型"不超过 300"，模型自我设限，契约放宽等于没生效。
    f"ID 全局唯一，层级不超过 {MAX_TREE_DEPTH}，节点不超过 {MAX_TREE_NODES}。"
    # 上面那句是**契约**上限（承载能力），这一句是**这一次**的规模。两句都要在：
    # 只说 1000 时模型会照着写一百多个节点，一次重响应既容易撞输出上限，画布上
    # 也读不完；只说 80 时它会把 80 当硬边界，复杂主题上主动砍内容。
    f"第一次生成本身请控制在 {INITIAL_MAX_NODES} 个节点、"
    f"{INITIAL_MAX_DEPTH + 1} 层（含中心主题）以内：宁可少两层，也要让每一层都写完整。"
    "更细的内容留给用户点开某个节点后再展开。"
    # 展开规则必须单独成段，判据只能是「还能不能再拆」。
    #
    # 早先这条规则被塞在分支数量里（「每个分支再给 2-4 个要点」），模型把它读成
    # 结构定义：根 → 分支 → 要点，三级就收。实测一份 38036 字的论文只长到 4 层、
    # 103 个节点——上限 8 一次都没被碰到，因为没有任何一句话在要求它往下走。
    #
    # 反向的坑同样要堵：写成「越深越好」模型会开始制造层级。所以这里给的是
    # **停止条件**（最小可执行单元 / 单一概念），并把层数上限明确成可读性上限、
    # 不是目标：深度由内容决定，不由数字决定。
    f"层级上限 {MAX_TREE_DEPTH} 是可读性上限，不是要拆到的目标。"
    "一个节点如果还能拆成更具体的子项，就继续拆；拆不动了就停："
    "要么已经是最小可执行单元（能直接照着做），要么本身就是单一概念"
    "（再拆只是把它自己换个说法重复一遍）。"
    # 「单一概念」必须给出可判定的定义，否则模型容易把任何名词都当成概念就停手：
    # 「多头注意力」「编码器堆栈」这类词背后明明还有组成部分和参数。
    "判断「单一概念」只看一条：它还能不能分成两个并列的成分。"
    "模块、流程、表格、公式这类有组成和参数的东西不算单一概念，"
    "它们的组成部分就该是子节点。"
    # 五套思考模型里都有「每个分支至少做什么」这类步骤。它们说的是**覆盖角度**，
    # 不是深度终点；不点明这层区别，模型会把「每个分支配一个类比」当成「这一支
    # 已经写完了」，深度规则就被它自己的思考步骤盖掉。
    "思考方式里的步骤只决定按什么角度拆，不决定拆到哪一层。\n"
    "输入分两种，按情况处理：\n"
    "1) 带材料的整理（会议纪要、文章、笔记）：材料已经写到的方面按它的结构走；"
    "材料没覆盖的方面按你的知识补全，不要留「材料中没有相关内容」这种空节点。"
    "每个可分解的分支至少 2 个要点，具体几个由内容决定；"
    "不要为了凑数把同一件事拆成两条。\n"
    "2) 简短的主题或目标（如「机器学习」「帮我规划毕业论文」）：围绕它展开成 "
    "3-6 个一级分支，每个分支再按上面那条「还能不能再拆」继续往下拆。"
    "这是框架展开；做法、步骤、判断标准按你的知识写，"
    "但不要编造看起来可核查的出处（书名、论文编号、链接）。\n"
    "两种情况下所有一级分支加起来不少于 3 条，根节点都必须有子节点，"
    "只有根节点的结果不合格。"
    # 正反例沿用手写示例的写法（下面 quote 那段也是 ❌/✅）：只说「更具体」时
    # 模型每次都按自己的尺度解释，给一组形态对照它才有稳定的判据。
    # 例子刻意不写任何领域内容——无来源时会被当成可抄的事实，有来源时又不在
    # 材料里，两边都会变成凭空多出来的节点。
    "\n拆到底还是提前停手，看这几组对照：\n"
    "❌ 根 → 分支 → 要点，到此为止：这是两级结构，而那个要点下面明明还有可写的内容\n"
    "✅ 根 → 分支 → 要点 → 具体做法或具体结论，直到每片叶子都能直接执行或直接成立\n"
    "❌ 要点写得很长、下面却不再拆：那是把同一层写胖，不是把结构写深\n"
    "✅ 每一层都比上一层更具体，父子不能是同一件事的两种说法\n"
    "不要停在两级就收；也不要为了凑层数制造节点——拆不动的那一支到此为止是正常的。"
    + CONTENT_FREEDOM_CONTRACT
)

# 没有材料可标出处的那两条路（纯主题生成、`rag` 形态检索为空后的退化）追加的一句。
#
# 为什么单独一句而不是塞进 `DETAIL_PROMPT`：那份提示词是 `SOURCED_DETAIL_PROMPT`
# 与 `RAG_SOURCE_PROMPT` 的前缀，写死"这次没有材料"会与后面那句"这次输入的是带了
# 来源的材料"直接打架。每条路只描述自己那种形态，是这三份提示词一直以来的分工。
DETAIL_NO_SOURCE_RULES = (
    "\n这次没有可标出处的材料：不要写 source_refs，也不要在节点里写"
    "「材料中没有相关内容」——按主题把你的知识写全，引用一律留空。\n"
)

SOURCED_DETAIL_PROMPT = DETAIL_PROMPT + (
    "注意：这次输入的是带了来源的材料，按上面第 1 种形态处理。"
    # 深度规则在有来源时要换成「材料能撑到多细，撑不住就按自己的知识接着拆」。
    # 上一版写的是"判据只看材料，材料没说的就停"，实测把整棵树压成材料的目录：
    # 材料没覆盖的角度全都不出现，用户拿到的是一份复印件而不是一张导图。
    # 新判据保留"材料有更细的说法就照它拆"，但材料没说的部分改为可以凭知识补——
    # 代价是多出来的那一层没有出处，所以同一句里把引用纪律钉上。
    "「还能不能再拆」这条同样适用：原文里还有更具体的表述支撑，就继续拆一层；"
    "材料到这里就没有更细的说法了，也可以按你的知识继续拆——只是那一层不挂引用。"
    # 「更具体的表述」也要给形状。只说「更具体」时模型只去找正文句子，
    # 而原文自带的分层（标题、编号、列表、表格行、公式步骤）同样是依据，
    # 而且比「哪句更像细节」更稳定——它是原文写下的，不是模型判断出来的。
    "原文自己的分层就是最直接的依据：标题、编号、列表项、表格的行、公式的每一步，"
    "都是材料给出的分点——遇到它们就照原样落成一层，不要把一整节压进一个节点。"
    "用户输入为 sources 数组，每项含 source_id 和 text，text 是待整理的原文。"
    "每个引用到材料的节点增加 source_refs 数组，格式为"
    '[{"source_id":"输入中已有的来源ID","quote":"逐字摘录的原文片段"}]。'
    # 引用是**节点**的字段，与 label、detail 平级。写进 detail 的引用过不了
    # `verify_source_refs`（它只看 `node.source_refs`），而且会变成一段无法点击的
    # 纯文本——用户看到的"有出处"和系统认为的"有出处"不是同一件事。
    "source_refs 与 label、detail 平级，不要写进 detail 的文字里。"
    "quote 不超过 300 字，必须出自对应来源的 text：可以只截取原文里的一段，"
    "但不能改写、概括、拼接或跨来源引用。\n"
    "原文：会议决定在十月十七日发布二点零版本。\n"
    '❌ quote="计划近期上线" —— 这是概括，原文里逐字找不到这句\n'
    '✅ quote="十月十七日发布二点零版本" —— 原文里的连续片段\n'
    # 两句话分工：材料里有的话要挂引用；材料里没有的（自己补的）不挂。
    # 只写后者会退化成"能引就引、引不到就编"，只写前者会退化成"没有依据的节点删掉"。
    "有原文依据的地方就写 source_refs；自己补的内容不要挂引用，也不要写成"
    "「材料指出」——那是在冒充材料。出处确实没有时 source_refs 留空，不要编造。"
    "不要输出 source_usage；不要猜测 page、start、end。"
)


# 思维模型表：同一句主题换一种拆解方法，长出来的树结构就不同。
#
# 每条 `prompt` 都是**给模型看的思考步骤**，不是给人看的标签：只写「第一性原理」
# 四个字，模型会照字面编一段百科式定义；写成有序的追问动作，它才会真的按那套
# 顺序去做。`label` 才是界面上的中文名。
#
# 键是前后端共用的字符串标识（前端下拉的 value 原样发给后端），改动要两端同步。
THINKING_MODELS: dict[str, dict[str, str]] = {
    "default": {
        "label": "通用",
        "prompt": (
            "1. 定中心：把输入浓缩成一句话放到根节点，不要用「XX 分析」这类空壳标题。\n"
            "2. 定骨架：挑 3-6 个互不重叠的大维度充当一级分支，并列或递进都行，但彼此不能是同一件事的两种说法。\n"
            # 「一般 2-4 层」是同一个压深度的来源：它直接给了一个层数区间，
            # 模型照抄区间，而 `DETAIL_PROMPT` 的深度规则是「能拆就拆、拆不动才停」。
            # 两处口径打架时模型听具体的那个数字，所以这里只描述向下拆的条件。
            "3. 逐层细化：每个分支继续往下拆成做法、要点或例子；只要某一项还能拆出更具体的内容"
            "就再拆一层，拆到它成为能直接执行的动作或单一概念为止，不要停在两级。\n"
            "4. 校正：回头检查维度有没有缺、节点有没有放错层、有没有只剩标题没有内容的空分支；凑数的节点直接删掉。\n"
        ),
    },
    "first-principles": {
        "label": "第一性原理",
        "prompt": (
            "1. 剥离既有结论：先把输入里那些「大家都这么做」「行业惯例如此」的说法单独拎出来，它们不能直接当骨架。\n"
            "2. 拆到基本事实：连续追问为什么，直到剩下不能再拆、且能被单独验证的事实或硬约束（物理限制、成本、时间、人的真实需求）。\n"
            "3. 自下而上重建：只从这些基本事实出发一步步推出应当有什么，结论允许和通行做法不一样。\n"
            "4. 拒绝类推移花接木：不写「像别的行业那样」「别人都这么干」，每条结论都要能指出它由哪条基本事实支撑。\n"
        ),
    },
    "feynman": {
        "label": "费曼",
        "prompt": (
            "1. 说人话：每个节点都用日常用语写；出现专业名词就在同一句里换成白话解释，不要靠术语撑场面。\n"
            "2. 必须打比方：每个一级分支至少配一个生活里的类比，并说明它像什么、哪一点像、哪一点其实不像。\n"
            "3. 必须举反例：每个分支至少写出一条「这个说法在什么情况下不成立」的反例或边界情形。\n"
            "4. 以讲得通为完成标准：想象合上讲义把它讲给别人听，凡是需要含糊带过的地方都说明还没真懂，改到能一口气讲通再收。\n"
        ),
    },
    "systematic": {
        "label": "5W1H",
        "prompt": (
            "1. 按六个维度铺开，一维一条一级分支：What（它是什么、包含什么）、Why（为什么要做、为什么重要）、"
            "Who（谁参与、谁受影响）、When（什么时候做、先后顺序）、Where（在什么场景或范围内）、How（具体怎么做）。\n"
            "2. 每个维度下面至少写一个要点；某个维度确实无从下笔时，明确写「暂不明确」并说明缺什么信息，不要拿空话填，也不要悄悄省掉这一维。\n"
            "3. 六个维度之间允许交叉，但同一件事只在一处展开，交叉的地方在更深一层互相指认。\n"
            "4. 收尾核对：六维是否齐全，缺任意一维都算结构不完整，补齐后再输出。\n"
        ),
    },
    "critical": {
        "label": "批判性",
        "prompt": (
            "1. 摆出论点：把输入里的每个主张改写成一句可以被反驳的话，作为一级分支；不要写「关于 XX 的讨论」这种无法证伪的标题。\n"
            "2. 盯住证据：每条论点下面写清它靠什么成立——数据、来源、实验，还是某人的判断；依据薄弱要明说薄弱。\n"
            "3. 找出反例：每条论点至少给一个反例或相反的证据；实在写不出反例时，说明为什么这条很难被推翻。\n"
            "4. 划出局限：写清这条结论在什么条件下会失效（样本、时间、地域、前提假设）。\n"
            "5. 留待验证：还不能下结论的部分单列成待验证项，并写清怎么验证；不要用推测冒充结论。\n"
        ),
    },
}

# 有来源时的限定句。它约束的是**出处**，不是内容范围：思维模型（尤其费曼那套
# 「必须举反例」）会自然长出材料里没有的角度，上一版的写法把那些角度一律禁掉，
# 结果整棵树退化成材料的目录。现在放开内容、只要求"自己补的不冒充材料"——
# 这两件事必须分开说，合在一句里的话，模型只会执行更保守的那一半。
SOURCED_THINKING_GUARD = (
    "\n上面这套思考方式只决定「按什么角度整理」，不是对内容范围的限制："
    "每一步都可以写，材料没覆盖的部分按你的知识补全，不要因为材料没提到就跳过；"
    "但凡引用、转述、概括材料的，必须标出处，自主补充的内容不要挂 source_refs。\n"
)

# `rag` 形态的提示词。**不能复用 `SOURCED_DETAIL_PROMPT`**：那一份写着「用户输入
# 为 sources 数组，每项含 source_id 和 text，text 是待整理的原文」，而 rag 形态里
# 一个字的正文都没有——照抄会让模型去找一个不存在的 text 字段，再把"没看见"读成
# "材料里没有"，最后交出一棵空心树。两份提示词共用 `DETAIL_PROMPT` 的树规则，
# 之后的每一句都只描述自己那种形态。
RAG_SOURCE_PROMPT = DETAIL_PROMPT + (
    "注意：这次输入的是带了来源的材料，按上面第 1 种形态处理。"
    # 形态必须先讲清楚。模型不知道"还有东西没给我"时会把清单当成全文，直接从标题
    # 推内容；这类错最难查，因为树看起来有依据，其实全是常识补的。
    #
    # 这里**不再要求模型自己调 `rag_query`**：`_generate` 在拼 system 之前已经做过
    # 一次主题预检索（`tools._retrieve_topic`），命中就拼在 user 消息末尾。再让它
    # 自行检索是重复的——它拿不到新东西，却多花一次 embedding + 一轮对话轮次，
    # 而且"没检索到"的自我判断会把整棵树又拉回空心形态。
    "\n你看不到材料全文：输入里只有一份材料清单（标题、字数、类型），正文一个字"
    "都没有进来；与主题相关的原文片段由系统检索后附在输入末尾（evidence 数组），"
    "那是你这次能引用的全部原文。\n"
    "❌ 清单里有一份《预算管理办法》，就直接写「预算需经财务审批」并挂上引用"
    "——这是从标题猜的\n"
    "✅ 引用只从 evidence 里抄；evidence 没覆盖的方面按自己的知识写，不挂引用\n"
    # 引用怎么抄必须逐字段点名。只说"引用要准确"时，模型会给出一条 quote 看着对、
    # 偏移却自己算的引用；逐字校验（`optimization/source_refs._check_reference`）
    # 会把它整条剔掉，界面上就成了"这篇材料一个字都没被引用"。
    "引用必须来自 evidence：source_refs 里每个字段都从 evidence 的返回里抄，"
    "不要自己算。"
    # 与 `SOURCED_DETAIL_PROMPT` 同一句：引用是节点字段，不是正文的一部分。
    "source_refs 与 label、detail 平级，不要写进 detail 的文字里。"
    "source_id 用返回的 source_id；quote 用返回的 quote，整段照抄时连着 start、end "
    "一起照抄。"
    "引文必须逐字照抄，不要改写、不要概括、也不要自己算偏移。"
    # 父块返回模式下 hit 的 quote 是 450–700 字的父块，超过 300 字的上限就只能截段，
    # 而截完没有偏移可抄——校验于是只剩"逐字在原文里"，落没落在命中的那一段查不出来。
    # `evidence_*` 就是给这条路准备的：它是命中的那一小段（约 200 字，带绝对值偏移），
    # 抄它既过逐字校验、又过"落在证据上"那道。
    "返回里如果还带 evidence_start / evidence_end / evidence_quote，优先用这一组："
    "start、end、quote 分别抄它们的值——那是这次检索真正命中的那一小段。"
    "返回的 quote 超过 300 字（引用字段的长度上限）时，只截取其中连续的、不超过 300 "
    "字的一段，这时不要写 start 和 end——偏移自己算是算不准的；只给 quote 时"
    "服务端会在原文里定位它，定位不到才会判失败。\n"
    '❌ quote="预算由财务经理核准"——evidence 里没有这句，是你概括的\n'
    "✅ 把某条 hit 的 quote 原样抄过来（太长就截一段连续的），source_id 照抄\n"
    # 这一形态最容易出事的地方：模型看不见内容，却仍然要把导图填满。
    # 上一版在这里写的是"检索不到就不要写…空着是正确结果"，实测代价是覆盖率
    # 从 100% 掉到 28.8%，树上还会长出"材料中没有相关内容"的空节点。
    "检索不到就按自己的知识展开，但不要写引用：未检索到不等于材料里没有，"
    "也不要在节点里写「材料中没有相关内容」。\n"
    "没有依据可标时 source_refs 留空，不要编造引用。"
    "不要输出 source_usage；不要猜测 page。"
)

# 与 `SOURCED_THINKING_GUARD` 同义、不同词：那一句的措辞是"sources 覆盖到的范围"，
# 而 rag 形态下 `sources` 根本不在输入里，只有 evidence 数组。共用一句会让模型去找
# 那个不存在的字段。位置同理必须排**在思维步骤之后**：它约束的正是上面那套步骤。
RAG_THINKING_GUARD = (
    "\n上面这套思考方式只决定「按什么角度整理」，不是对内容范围的限制："
    "每一步都可以写，evidence 没覆盖的部分按你的知识补全，不要因为没检索到就跳过；"
    "但凡引用、转述、概括材料的，必须标出处，自主补充的内容不要挂 source_refs。\n"
)


def thinking_block(model: str) -> str:
    """把选定的思维模型拼成 prompt 的一段。未知值回退 default。

    返回空字符串表示「不要这段」，但当前五个模型都该有内容——`default` 也是一种
    拆解方法（通用归纳），不是「不加提示」。回退而不是抛错：这是人手传进来的
    字符串，前端版本不一致时多一个陌生值不该让整次生成失败。
    """
    entry = THINKING_MODELS.get(model) if isinstance(model, str) else None
    if entry is None:
        entry = THINKING_MODELS["default"]
    return f"\n思考方式（按这个顺序拆解）：\n{entry['prompt']}\n"


def thinking_options() -> list[dict[str, str]]:
    """返回 [{value, label}]，前端下拉的候选。

    顺序即 `THINKING_MODELS` 的定义顺序，`default` 永远排第一：前端默认选中项
    与后端默认值必须是同一个，否则「不选就生成」出来的树和界面显示的方式不一致。
    """
    return [
        {"value": value, "label": entry["label"]}
        for value, entry in THINKING_MODELS.items()
    ]


# 一级分支下限：少于这个数说明模型把一棵树压成了一条链 —— 那是「层次结构」
# 最差的一种形态，比光杆根节点好不了多少。下限只加在**模型整图生成**这条路径上，
# 而且是质量要求、不是合法性要求：`MindMapContent.validate_tree()` 不设门槛，
# 旧图和用户手工删到一条分支的图照常能存能开；demo 是固定占位结构（本来就只有
# 一个子节点），也显式跳过。同步接口、流式接口共用这一个值，改一处就全改 ——
# 否则同一个模型结果会被一个入口判成功、另一个判失败。
MIN_BRANCHES = 3


class GenerationError(Exception):
    pass


def estimate_input_tokens(text: str) -> int:
    """估算一段文本的 token 数，用于在**发请求之前**判断它装不装得进上下文。

    汉字约 1.5 字/token、ASCII 约 4 字符/token。这是离线可算的粗估，不是
    分词器的真值：为了一次边界判断去接一份词表（还要跟模型版本同步）不划算，
    而这里要回答的只是「离 6 万还差得远不远」。两个方向都偏向保守——汉字按
    1.5 字/token 折算比真实中文 tokenizer 更费 token，ASCII 按 4 字符折算与
    BPE 的经验值一致；高估会在边界上多拒一点，低估则是请求真的打到 400 上。
    """
    if not text:
        return 0
    # 0x2E80 起是 CJK 部首扩展：汉字、假名、全角标点都在这个区间之上，
    # 它们的 token 密度与 ASCII 差四倍，必须分开算。
    cjk = sum(1 for ch in text if ord(ch) >= 0x2E80)
    return math.ceil(cjk / 1.5 + (len(text) - cjk) / 4)


def context_overflow_error(
    *, chars: int, tokens: int, limit: int, label: str = "材料",
) -> GenerationError:
    """上下文超限的错误对象。文案只有这一份实现。

    多个入口（同步生成、工具内生成、agent 循环）都在守同一道边界，各写一句
    文案的结果是同一件事故在界面上有三种说法。文案必须可操作：现在多长、
    换算成多少 token、上限多少、用户能做什么。
    """
    return GenerationError(
        f"{label}约 {chars} 字，估算 {tokens} token，超过模型上下文上限 {limit}。"
        "请精简后重试，或分成多次生成。")


def ensure_input_within_context(
    text: str, *, label: str = "材料", limit: int = CONTEXT_TOKEN_LIMIT,
    extra_tokens: int = 0,
) -> int:
    """输入装不进模型上下文时抛一条能直接展示给用户的错误；否则返回估算值。

    这是**物理**边界，不是产品口径：超了模型不会给你半份结果，只会返回 400
    并把整次请求作废。

    `extra_tokens` 是同一次请求里除 `text` 之外的固定开销（系统提示词等），
    只参与判断，不改变文案里那个用户看得见的长度。
    """
    tokens = estimate_input_tokens(text) + extra_tokens
    if tokens <= limit:
        return tokens
    raise context_overflow_error(chars=len(text), tokens=tokens, limit=limit, label=label)


# 模型偶尔无视「不要代码围栏」的指示，把 JSON 包进 ```json … ``` 里。
# 裸 json.loads 会因此直接抛错，用户侧表现为随机失败 —— 实测 18 次调用里
# 撞到 1 次。解析前先剥干净，比要求模型「务必听话」可靠。
_FENCE_OPEN = re.compile(r"^\s*```(?:json|JSON)?\s*")
_FENCE_CLOSE = re.compile(r"\s*```\s*$")


def _balance_brackets(text: str) -> str:
    """修正括号收支不平衡：多出来的闭合符丢掉，缺失的补上。

    两个方向都实测出现过：
      - 多一个 `}`：输入 100000 字符时 3 次里 2 次，模型收尾多写一个花括号，
        `finish_reason` 仍然是 `stop`（它认为自己写完了），整份 JSON 报废。
      - 少若干闭合符：输出撞上 max_tokens 被硬切。
    只动括号，不碰字符串内部 —— 节点文字里的 `{` `}` 必须原样保留。
    """
    out: list[str] = []
    stack: list[str] = []
    in_string = False
    escaped = False

    for ch in text:
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
        elif ch in "{[":
            stack.append(ch)
            out.append(ch)
        elif ch in "}]":
            expected = "{" if ch == "}" else "["
            if stack and stack[-1] == expected:
                stack.pop()
                out.append(ch)
            # 没有配对的开启符：这个闭合符是多余的，直接丢弃
        else:
            out.append(ch)

    if in_string:
        out.append('"')
    for opener in reversed(stack):
        out.append("}" if opener == "{" else "]")
    return "".join(out)


def parse_model_json(content: str) -> Any:
    """解析模型返回的 JSON，容忍代码围栏、多余文字与括号收支错位。"""
    text = (content or "").strip()
    if not text:
        raise ValueError("模型返回了空内容")
    text = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", text))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 括号收支错位：这是实测最常见的一种失败，先修它再试
    balanced = _balance_brackets(text)
    if balanced != text:
        try:
            return json.loads(balanced)
        except json.JSONDecodeError:
            pass

    # 还有解释文字包着时，截取最外层的对象或数组
    for left, right in (("{", "}"), ("[", "]")):
        start, end = text.find(left), text.rfind(right)
        if start < 0 or end <= start:
            continue
        candidate = _balance_brackets(text[start:end + 1])
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ValueError("模型返回内容里没有可解析的 JSON")


class SalvagedOutput(dict):
    """被长度上限截断、但**回收出了完整节点**的解析结果。

    仍然是一个 `dict`：调用方（`_generate` / `_generate_deep` / `_expand`）把它
    当普通响应继续用，pydantic 校验、`json.dumps` 全都照常。多出来的只是一个
    事实——"这次输出是被切断的，里面少了东西"。调用方据此标 `partial` 并告警，
    而不是让整棵树判失败。

    为什么不是普通 dict + 一个旁路参数：**预览与校验读的是同一个返回值**，
    多一条并行通道就意味着"修复了生成路径但忘了 agent 工具"这种漏接。子类是
    唯一不需要改所有调用方签名的做法。
    """

    #: 这次输出被截断了（内容不完整）。
    partial: bool = True

    def __init__(self, data: dict, recovered: int = 0) -> None:
        super().__init__(data)
        self.recovered = max(0, int(recovered))


# 顶层的两个形状：整树生成（`root`）与分片展开（`children` / 旧格式 `points`）。
_ROOT_KEY = re.compile(r'"root"\s*:\s*\{')
_CHILDREN_KEY = re.compile(r'"children"\s*:\s*\[')
_POINTS_KEY = re.compile(r'"points"\s*:\s*\[')
_JSON_STRING = r'"((?:[^"\\]|\\.)*)"'


def _closed_object_spans(text: str) -> tuple[list[tuple[int, int]], bool]:
    """列出所有**已闭合**的对象片段，以及这段 JSON 是否被切断。

    返回 `(spans, truncated)`。`spans` 只保留最外层那些：嵌套片段会被外层包住，
    取外层才有完整的子树（内层单独拿出来会丢掉它自己的兄弟）。
    `truncated=True` 表示扫描结束时还有没闭合的括号——也就是"这段 JSON 是被
    切断的"，这是回收与"正常解析"的分界线。

    **任何深度的闭合都要记**（不只是栈空时）：被切断时最外层恰恰永远不闭合，
    真正能回收的正是它内部那些已经闭合的子树。只记栈空的话，回收函数在
    `finish_reason=length` 这种唯一会用到它的场合永远返回 None。
    """
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    in_string = False
    escaped = False
    for index, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(index)
        elif ch in "}]":
            if not stack:
                # 多余的闭合符：跳过它，别让偏移整体错位
                continue
            start = stack.pop()
            if text[start] == "{":
                spans.append((start, index + 1))

    # 去掉被外层已闭合片段包住的内层片段（只保留最外层的完整子树）。
    # 闭合片段之间只有两种关系：嵌套或不相交，所以按起点排序后一遍扫描就够。
    spans.sort()
    outermost: list[tuple[int, int]] = []
    for span in spans:
        if outermost and span[0] < outermost[-1][1]:
            continue
        outermost.append(span)
    return outermost, bool(stack) or in_string


def _first_json_string(pattern: re.Pattern[str], text: str) -> str | None:
    """取第一个匹配的 JSON 字符串值（按 JSON 转义规则解码）。"""
    match = pattern.search(text)
    if not match:
        return None
    try:
        return json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return None


def salvage_model_json(content: str) -> SalvagedOutput | None:
    """从被切断的 JSON 里回收**已经完整闭合**的节点对象。

    这是"截断不再等于整棵树报废"的实现。`_balance_brackets` 那条老路会把半截
    JSON 补成"合法"，于是缺了一块的树被当成完整结果交出去；而一律抛错又会把
    已经写好的几十个节点一起丢掉。回收走第三条路：**只收闭合了的对象**。

    做法与判据：

      1. 扫描出所有已闭合的对象片段（括号收支在同一层平衡），只留最外层；
      2. 每个片段必须能 `json.loads`，且是带非空 `label` 的对象——那才是一个
         "完整的节点"；写了一半的 label（截在字符串中间）根本不会闭合，自然
         进不来；
      3. 按顶层形状重组：
         - 有 `"root"` → 整树生成，回收出来的子树挂到 root 的 children 上；
         - 有 `"children"` → 分片展开，直接就是 children 数组；
         - 两者都没有 → 认不出形状，返回 None（不猜）。

    返回 `None` 表示"没有任何可回收的完整节点"，调用方应当按原来的语义失败。
    """
    text = (content or "").strip()
    if not text:
        return None
    spans, _truncated = _closed_object_spans(_FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", text)))
    if not spans:
        return None

    nodes: list[dict[str, Any]] = []
    recovered = 0
    for start, end in spans:
        try:
            parsed = json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
        recovered += _count_labeled(parsed)
        if isinstance(parsed, dict) and isinstance(parsed.get("label"), str) \
                and parsed["label"].strip():
            nodes.append(parsed)
    if not nodes:
        return None

    if _ROOT_KEY.search(text):
        root = _salvage_root(text, nodes)
        if root is None:
            return None
        data: dict[str, Any] = {"root": root}
        title = _first_json_string(re.compile(r'"title"\s*:\s*' + _JSON_STRING), text)
        if title:
            data["title"] = title
        return SalvagedOutput(data, recovered=recovered)
    if _CHILDREN_KEY.search(text):
        return SalvagedOutput({"children": nodes}, recovered=recovered)
    if _POINTS_KEY.search(text):
        # 旧格式的 `points` 是字符串数组：闭合的元素不在上面的对象扫描里，
        # 回收不出东西就别硬凑（返回 None 让调用方按失败处理）。
        return None
    return None


def _count_labeled(value: Any) -> int:
    """递归数出"带非空 label 的对象"有几个。

    `recovered` 报的是**节点数**，不是"回收了几个顶层片段"：整树形状下最外层
    只可能是一个片段（`root`），按片段数报会得到恒定的 1，而用户与日志真正关心
    的是"救回了多少个节点"。
    """
    if isinstance(value, dict):
        own = 1 if isinstance(value.get("label"), str) and value["label"].strip() else 0
        return own + sum(_count_labeled(item) for item in value.values())
    if isinstance(value, list):
        return sum(_count_labeled(item) for item in value)
    return 0


def _salvage_root(text: str, nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """拼出被截断那棵树 `root` 形状：已写出的 id/label + 回收到的子树。

    root 自己的 `id` / `label` 一定写在 `children` 之前（模型输出的键序，
    以及提示词里的格式示例都是这个顺序），所以只在前半段里找。找不到 label
    就放弃——一棵没有根的树没有任何用处。
    """
    start = _ROOT_KEY.search(text)
    if start is None:
        return None
    body = text[start.end() - 1:]
    children_at = _CHILDREN_KEY.search(body)
    head = body[:children_at.start()] if children_at else body
    label = _first_json_string(re.compile(r'"label"\s*:\s*' + _JSON_STRING), head)
    if not label or not label.strip():
        return None
    node_id = _first_json_string(re.compile(r'"id"\s*:\s*' + _JSON_STRING), head)
    return {
        "id": node_id or "root",
        "label": label,
        "children": nodes,
    }


def _truncated_output_error() -> GenerationError:
    """输出撞上 `max_tokens` 被硬切、且一个完整节点都回收不出来时的错误。

    文案只有这一份：一次性响应与流式响应都会拦到同一种截断，两处各写一句的结果
    是同一个事故在日志与界面上有两种说法。
    """
    return GenerationError("模型输出撞上长度上限被截断，结果不完整，请重试")


def _choice_message(body: Any) -> tuple[str, bool]:
    """从响应体里取出模型 content 与"是否被长度上限截断"。

    `finish_reason == "length"` 表示输出在 max_tokens 处被硬切。这个信号**不再
    直接判失败**：调用方先试着用 `salvage_model_json` 回收完整节点，收得回来就
    交一棵"少了尾巴但可用"的树并标 partial，一个都收不回来才失败。

    `stop` 与字段缺失时按"没被截断"处理：老版本的 mock、部分 OpenAI 兼容端点
    都不返回这个字段，那不是"被截断"，只是"没说原因"。
    """
    choice = body["choices"][0]
    truncated = isinstance(choice, dict) and choice.get("finish_reason") == "length"
    return choice["message"]["content"], truncated


def _config() -> tuple[str, str, str]:
    """读模型配置。缺失或格式不对时抛 GenerationError，让上层给出可读文案。"""
    base = os.getenv("MINDMAP_LLM_BASE_URL", "").strip().rstrip("/")
    key = os.getenv("MINDMAP_LLM_API_KEY", "").strip()
    model = os.getenv("MINDMAP_LLM_MODEL", "").strip()
    if not base.startswith(("https://", "http://")) or not key or not model:
        raise GenerationError("请在后端环境变量中配置模型地址、密钥和模型名称")
    return base, key, model


def _stream_frame(data: str) -> tuple[str, str | None] | None:
    """一帧 `data:` 里的 `(delta.content, finish_reason)`；形状不对返回 None。

    `content` 与 `finish_reason` 都可能只出现在其中一部分帧里（首帧常见
    `role`、末帧才带 `finish_reason`），所以两者各自缺省，调用方不该指望
    "一帧里同时拿到正文和结束原因"。
    """
    try:
        choice = json.loads(data)["choices"][0] or {}
        delta = choice.get("delta") or {}
        text = delta.get("content")
        reason = choice.get("finish_reason")
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError):
        return None
    return (text if isinstance(text, str) else "",
            reason if isinstance(reason, str) else None)


async def _chat_streamed(
    base: str,
    request_body: dict[str, Any],
    headers: dict[str, str],
    on_delta: Callable[[str], Awaitable[None]],
) -> dict[str, Any] | None:
    """试一次 `stream: true` 请求：成功返回与一次性路径同形的解析结果。

    返回 None 表示**这次流式尝试不算数**，调用方应当退回一次性请求。只有两种
    情况故意抛出，因为它们与流式无关、一次性路径同样会拒：响应体超限、输出被
    长度截断——重来一遍只是多花一次钱。

    其余一切（服务端不认 `stream` 直接回整包 JSON、流里一段正文都没有、流中途
    断掉、拼起来的正文解析不出对象、网络层报错）都退回 None：预览少几帧可以，
    整次生成不能因为预览机制而失败。
    """
    content = ""
    finish_reason: str | None = None
    ended = False          # 见到 `[DONE]` 或 finish_reason 才算这次流完整结束
    callback: Callable[[str], Awaitable[None]] | None = on_delta
    body = dict(request_body, stream=True)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS,
                                     follow_redirects=True) as client:
            async with client.stream("POST", base + "/chat/completions",
                                     content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers=headers) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    # 有的兼容端点写 `data:{...}` 不带空格，两种都要认
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        ended = True
                        break
                    frame = _stream_frame(data)
                    if frame is None:
                        continue
                    text, reason = frame
                    if reason is not None:
                        finish_reason = reason
                        ended = True
                    if not text:
                        continue
                    content += text
                    # UTF-8 下一个字符至少占一个字节，按字符数卡上限只会比按字节
                    # 更早放弃，不会放一个超限响应把内存读满。
                    if len(content) > _MAX_RESPONSE_BYTES:
                        raise GenerationError("模型响应过大")
                    if callback is None:
                        continue
                    try:
                        await callback(text)
                    except Exception:  # noqa: BLE001 — 预览出错不能连累这次生成
                        # 回调是上层做的增量解析，它自己坏了就让后续正文不再上报，
                        # 但正文照收：返回值仍是一次性路径那份解析结果。
                        callback = None
    except GenerationError:
        raise
    except Exception:  # noqa: BLE001 — 含 httpx 各类错误，一律退回一次性请求
        return None
    # 截断判定与一次性路径同源（`_parse_choice` 是唯一实现）。
    if finish_reason == "length":
        # 流式路径上的截断**可以回收**：`content` 里已经有模型写出来的全部正文，
        # 只是尾巴没了。回收不出完整节点时才退回一次性请求——那条路会拿到同一份
        # 被切的正文，结果相同，还不如在这里直接判。
        salvaged = salvage_model_json(content)
        if salvaged is not None:
            logger.warning("流式输出被长度上限截断，已回收 %d 个完整节点", salvaged.recovered)
            return salvaged
        raise _truncated_output_error()
    # 没有结束标记说明这条流被切断了。此时半截正文交给 `parse_model_json` 会被
    # 补齐括号"解析成功"，一棵缺了半边的树就这样当成功交出去——退回一次性请求，
    # 由它按完整响应的规则判定。
    if not ended or not content:
        return None
    try:
        return parse_model_json(content)
    except ValueError:
        return None


async def _chat_once(
    base: str, request_body: dict[str, Any], headers: dict[str, str],
) -> dict[str, Any]:
    """一次性请求：收满整个响应体再解析。

    `chat_async` 在没有预览消费者时的只有它也走这条；流式尝试被服务端拒掉
    （不认 `stream`、流中途断掉）时同样退回这里。

    **`max_tokens` 超限的自动降级就在这里**：服务端用 400 回一句
    `Range of max_tokens should be [1, N]` 时，把 N 记下来（`learn_max_tokens`）
    并用它重试**一次**。不降级的后果是"换一个模型就整条生成路径报错"，而错
    文案指向的是"请检查服务配置"——配置本身是对的，只是上限不同。
    """
    payload = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=True) as client:
            async with client.stream("POST", base + "/chat/completions",
                                     content=payload, headers=headers) as response:
                # 400 的正文里才有服务端真正的上限；流式响应必须先读出来。
                # mock 出来的响应对象没有 `aread`，所以取不到就算了（下面那条
                # `_error_body` 会退化成空串，降级逻辑本身不受影响）。
                # 状态码同样按 `getattr` 取：好几个测试替身（以及某些代理包装）
                # 根本没有这个属性，用 `response.status_code` 会抛 AttributeError，
                # 而那个异常会被下面的 except 归成"返回内容不合法"——一条与真实
                # 原因无关的错误文案。
                if getattr(response, "status_code", 200) >= 400:
                    reader = getattr(response, "aread", None)
                    if reader is not None:
                        try:
                            await reader()
                        except Exception:  # noqa: BLE001 — 读错误体失败不影响主流程
                            pass
                response.raise_for_status()
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > _MAX_RESPONSE_BYTES:
                        raise GenerationError("模型响应过大")
        body = json.loads(buffer)
        return _parse_choice(body, request_body)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        downgraded = _downgrade_max_tokens(request_body, exc.response)
        if downgraded is not None:
            logger.warning("模型服务拒绝 max_tokens=%s（HTTP 400，%s），按服务端上限 %d 重试一次",
                           request_body.get("max_tokens"), downgraded.reason, downgraded.cap)
            return await _chat_once(base, downgraded.body, headers)
        raise GenerationError(f"模型服务返回 HTTP {code}，请检查服务配置") from exc
    except (httpx.RequestError, httpx.InvalidURL, OSError) as exc:
        raise GenerationError("无法连接模型服务或请求超时") from exc
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise GenerationError("模型返回内容不是符合约定的导图 JSON，请重试") from exc


@dataclass(frozen=True)
class _Downgrade:
    """一次 max_tokens 降级：新的请求体、学到的上限、以及原因（日志用）。"""

    body: dict[str, Any]
    cap: int
    reason: str


def _error_body(response: Any) -> str:
    """尽力取出错误响应正文。取不到就返回空串——降级是加分项，不该自己抛。"""
    for attr in ("text",):
        try:
            value = getattr(response, attr, "")
            if isinstance(value, str) and value:
                return value
        except Exception:  # noqa: BLE001 — ResponseNotRead 等
            continue
    try:
        content = getattr(response, "content", b"")
        if isinstance(content, (bytes, bytearray)):
            return content.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _downgrade_max_tokens(
    request_body: dict[str, Any], response: Any,
) -> _Downgrade | None:
    """400 里能解析出更小的上限时，返回一次降级请求；否则 `None`（不重试）。

    只降一次：重试的请求体里已经带着学到的上限，再撞 400 说明问题不在这个数
    （密钥、模型名、账户余额都会给 400），继续重试只是多花一次网络往返。
    """
    if getattr(response, "status_code", None) != 400:
        return None
    limit = parse_max_tokens_limit(_error_body(response))
    if limit is None:
        return None
    current = request_body.get("max_tokens")
    if isinstance(current, int) and current <= limit:
        return None
    learn_max_tokens(limit)
    return _Downgrade(body={**request_body, "max_tokens": limit}, cap=limit,
                      reason=f"服务端上限 {limit}")


def _strict_json(content: str) -> Any | None:
    """只认**语法完整**的 JSON：不做补齐、不截取片段。失败返回 `None`。

    与 `parse_model_json` 的分工是刻意的：那个函数会补括号、会从一段废话里抠对象，
    是"尽量解析出来"；这个只有一个用途——判断"模型其实写完了，只是服务端在
    `finish_reason` 上说了 length"。两者混用会把被切断的 JSON 判成完整。
    """
    text = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", (content or "").strip()))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _parse_choice(body: Any, request_body: dict[str, Any] | None = None) -> Any:
    """把一次完整响应变成解析结果：正常解析，或截断时回收完整节点。

    三条出口共用这一份（`_chat_once`，以及 `chat_async` 的两条路径最终都到这里）：
    解析规则、截断判定、回收策略各写一份的结果，是同一次截断在三条路上有三种
    结局——其中两条静默成功。

    截断时的顺序不能反：

      1. 先看**语法完整**的 JSON（`_strict_json`）。模型偶尔在最后一帧被切，
         但它其实已经写完了整个对象——那时 `finish_reason=length` 只是"到点了"，
         内容一个字没少。这一档当完整结果用，只标 `partial` 让上层知道有过截断。
      2. 再试着从被切的文本里回收已闭合的节点（`salvage_model_json`）。
      3. 两样都不成立才失败——那时确实没有可交付的东西。
    """
    content, truncated = _choice_message(body)
    if not truncated:
        return parse_model_json(content)
    complete = _strict_json(content)
    if isinstance(complete, dict):
        logger.warning(
            "模型输出标记为 length，但 JSON 语法完整；按完整结果使用并标 partial（%d 个节点）",
            _count_labeled(complete),
        )
        return SalvagedOutput(complete, recovered=_count_labeled(complete))
    salvaged = salvage_model_json(content)
    if salvaged is not None:
        logger.warning(
            "模型输出撞上 max_tokens=%s 被截断，已回收 %d 个完整节点（标 partial，不整棵判失败）",
            (request_body or {}).get("max_tokens"), salvaged.recovered,
        )
        return salvaged
    raise _truncated_output_error()


async def chat_async(
    system: str,
    user: str,
    *,
    max_tokens: int | None = None,
    on_delta: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """agent 的工具（生成 / 展开 / 编辑）唯一的模型出口。

    全程异步是硬要求：客户端断开时 Starlette 会 cancel 生成器，异步 httpx
    会随之关闭连接、停止计费；同步 httpx 跑在线程里，取消只能丢掉结果，
    请求本身仍会跑完并产生费用。

    `on_delta` 只服务「让上层边收边做预览」，返回的仍然是**同一份**解析结果：
    每收到一段 `delta.content` 调一次回调，最后还是走 `parse_model_json`。
    它不存在"另一套输出"——任何一步不成立（服务端不认 stream、流被切断、正文
    拼不起来、回调自己抛错）都退回一次性请求。宁可少几帧预览，也不能让这次
    生成失败。

    `max_tokens` 由调用方给：工具这条出口的上限来自 `RunBudget`，在这里再写
    一个默认值，预算里写的数与请求里发的数就会各走各的。
    """
    base, key, model = _config()
    # 输入侧的物理边界，与调用方是谁无关：装不装得进模型窗口由这里统一守，
    # 否则每个出口各守一个数，迟早分叉。
    ensure_input_within_context(user, extra_tokens=estimate_input_tokens(system))
    # 上限在**发请求之前**夹一次：`max_tokens_cap()` 是配置值与服务端学习值的
    # 较小者（见它的 docstring）。放在这里而不是调用方，是因为工具、agent 轮次、
    # 一次性接口都从这里出去——一处夹住，三条出口同值。
    requested = max_tokens or MAX_OUTPUT_TOKENS
    request_body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": min(requested, max_tokens_cap()),
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if on_delta is not None:
        streamed = await _chat_streamed(base, request_body, headers, on_delta)
        if streamed is not None:
            return streamed
    return await _chat_once(base, request_body, headers)


def _demo_content(text: str) -> MindMapContent:
    """显式的占位内容，不伪装为模型生成。"""
    label = text[:60]
    return MindMapContent(title=label, root=Node(id=str(uuid4()), label=label,
        children=[Node(id=str(uuid4()), label="请编辑此演示节点")]))


def _branch_shortfall(count: int) -> GenerationError:
    """一级分支不足时的错误文案：说清现状、下限与下一步怎么做。

    只说「不合格」用户没法行动；这里给出两条出路——换个说法重问，或者补充材料，
    因为这两种情况正是模型给不出分支的两大原因（主题太窄、材料太少）。
    """
    if count == 0:
        return GenerationError(
            "模型只返回了一个根节点，没有任何分支，请换一种说法或补充材料后重试")
    return GenerationError(
        f"模型只给了 {count} 条一级分支，少于要求的 {MIN_BRANCHES} 条；"
        "请换一种说法，或补充更多材料后重试")


def validate_generated_content(
    result: Any, *, sources: list[Source] | None = None,
    enforce_min_branches: bool = True,
    evidence: Sequence[EvidenceSpan] | None = None,
) -> tuple[MindMapContent, list[Diagnostic]]:
    """以真实输入重建来源摘要，剔除模型伪造的关联，再执行契约与原文校验。

    agent 的 `mindmap_generate`（`app/agent/tools.py::_generate()`）走这个出口：
    无来源时在契约校验之前清空所有节点的来源声明，避免伪造摘要与引用互相背书，
    也不让错误的引用拖垮有效的树。

    `enforce_min_branches` 是**质量**门槛而不是契约门槛：模型整图生成要求一级
    分支不少于 `MIN_BRANCHES` 条，demo 占位结构与用户已保存的旧图不走这道关。

    `evidence` 是本次运行累积的命中区间（`RunContext.evidence`），只在这条链上
    传：它回答的是"这条引用落在本次可见的那几段原文里吗"，而不是"逐字在不在原文里"。
    不给（或给空序列）时与加这个参数之前**逐字节一致**——`full` 形态下模型看得见
    全文，引用任何段落都是合法的，那条路本来就不该带这个约束。

    三条路的差别必须留住，合并任意两条都会误伤：

      - `full`（正文全在 prompt，调用方不传 `evidence`）：引用可以是任意段落，
        不做父块约束；
      - `rag` 且有证据（传 `evidence`）：引用要在本次可见的父块内，落在命中的
        子块之外只给 warn（`SOURCE_REF_OUTSIDE_EVIDENCE`），**不剔除**——模型现在
        写的是自己的话、引的是材料里的某段，它看到的是 450–700 字的父块，
        要求它精确落在命中的那 200 字里过于苛刻；
      - `rag` 且零证据（`evidence` 为 None，且树上不该有引用）：允许零引用通过，
        `GENERATION_WITHOUT_EVIDENCE`（warn）负责说明这次没有依据。它不是错误，
        诊断从不参与 ok 判定——一棵没有依据但结构完整的树仍然可用。
    """
    raw = deepcopy(result)
    diagnostics: list[Diagnostic] = []
    allowed_ids = {source.id for source in sources or []}
    # 生成侧的标题上限（16 码点）。整树生成时模型直接给整棵树，这条归一化就是
    # "AI 生成路径的 label 一定不超过 16 字"的唯一实现点——分片那条路在
    # `tools._slot_from` 里裁，两条路各裁一次，但用的是同一个契约常量。
    clipped_labels = 0
    if isinstance(raw, dict):
        raw["source_usage"] = [
            SourceUsage(source_id=source.id, kind=source.kind, filename=source.filename,
                        char_count=len((source.text or "").strip())).model_dump()
            for source in sources or []
        ]
        stack = [raw.get("root")]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            label = node.get("label")
            if isinstance(label, str) and len(label) > GENERATED_LABEL_MAX_CHARS:
                node["label"] = clip_label(label)
                clipped_labels += 1
            if sources is None:
                node["source_refs"] = []
            elif isinstance(node.get("source_refs"), list):
                kept = []
                for ref in node["source_refs"]:
                    if (isinstance(ref, dict) and isinstance(ref.get("source_id"), str)
                            and ref["source_id"] not in allowed_ids):
                        diagnostics.append(Diagnostic(
                            level=DiagnosticLevel.warn, code="UNKNOWN_SOURCE_REF",
                            node_id=node.get("id") if isinstance(node.get("id"), str) else None,
                            message=f"已移除引用：来源 {ref['source_id']} 不在本次输入中",
                        ))
                    else:
                        kept.append(ref)
                node["source_refs"] = kept
            children = node.get("children")
            if isinstance(children, list):
                stack.extend(reversed(children))
    try:
        content = MindMapContent.model_validate(raw)
    except ValidationError as exc:
        raise GenerationError("模型返回内容不是符合约定的导图 JSON，请重试") from exc
    if clipped_labels:
        # 聚合一条而不是逐节点报告：一棵 80 节点的树里出现十几条"标题太长"会把别的
        # 诊断淹掉，而用户能做的事只有一件——想要长内容就写进详情。
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="LABEL_CLIPPED_TO_GENERATED",
            message=(
                f"{clipped_labels} 个节点的标题超过 {GENERATED_LABEL_MAX_CHARS} 字，已截短；"
                "画布上只显示标题，长内容应该在 detail 里"
            ),
        ))
    if enforce_min_branches and len(content.root.children) < MIN_BRANCHES:
        raise _branch_shortfall(len(content.root.children))
    if sources is not None:
        # `evidence or None`：空序列与"没给"必须是同一条路。留着空列表会让
        # `verify_source_refs` 进到"有 evidence"的分支，虽然判定结果相同，但
        # "这条链有没有接上"就被这个细节绑住了——接线的可观测差异只该来自
        # 非空证据。
        diagnostics.extend(verify_source_refs(
            content, sources, remove_invalid=True, evidence=evidence or None))
    return content, diagnostics


def diagnose_density(content: MindMapContent, *, minimum: int = 2) -> list[Diagnostic]:
    """标出要点过少的分支。

    只作为提示，不构成失败：材料本身稀疏时，某个分支只有一个要点是合理的，
    强行要求补写会逼模型编造。所以它产出 warn 级诊断，由调用方决定怎么用。
    """
    diagnostics: list[Diagnostic] = []
    for branch in content.root.children:
        count = len(branch.children)
        if count >= minimum:
            continue
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="SPARSE_BRANCH", node_id=branch.id,
            message=(f"分支「{branch.label}」只有 {count} 个要点，"
                     f"少于建议的 {minimum} 个；材料稀疏时可以保留，不必强行补写"),
        ))
    return diagnostics
