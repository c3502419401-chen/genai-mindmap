"""本地演示和 OpenAI Chat Completions 兼容服务接入。"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any
from uuid import uuid4

import httpx
from pydantic import ValidationError

from app.contracts.diagnostic import Diagnostic, DiagnosticLevel
from app.contracts.mindmap import (
    MAX_TREE_DEPTH,
    MAX_TREE_NODES,
    MindMapContent,
    Node,
)
from app.contracts.source import Source, SourceUsage
from app.optimization.source_refs import verify_source_refs

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
MAX_OUTPUT_TOKENS = 65536

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
# 查询扩展那每份 200 字（`MINDMAP_QUERY_EXPANSION`，默认关）；离开进程的还有
# 混合检索的 embedding 请求（两种形态都一样）。
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


# 一次性生成完整导图的提示词（非流式接口、流式接口的第二段都用它）。
#
# 输入有两种形态，必须分开说：早先只写了「整理」和「不要捏造」，模型对
# 一句话的主题（如「机器学习」）就严格照做——那四个字里确实没有可整理的
# 分支，于是只回一个光杆根节点，被 MIN_BRANCHES 拦下。而这个产品本来的
# 用法就是「说一个主题，给一张图」，所以要把「围绕主题展开框架」和
# 「忠实整理材料」区分开：前者允许补全结构，后者不许凭空加内容，两者
# 都不许编造具体事实。
DETAIL_PROMPT = (
    "把用户输入变成一张思维导图。只返回一个 JSON 对象，不要代码围栏。"
    "格式为 {\"title\":\"标题\",\"root\":{\"id\":\"唯一id\","
    "\"label\":\"节点文字\",\"children\":[]}}。每个节点均含 id、label、children，"
    # 这里的两个数字取自契约常量：写死过一次，结果契约从 300 放到 1000 之后
    # 提示词还在叫模型"不超过 300"，模型自我设限，契约放宽等于没生效。
    f"ID 全局唯一，层级不超过 {MAX_TREE_DEPTH}，节点不超过 {MAX_TREE_NODES}。"
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
    "1) 完整材料（会议纪要、文章、笔记）：忠实整理它的结构，不要添加材料里"
    "没有的内容。每个可分解的分支至少 2 个要点，具体几个由内容决定；"
    "依据不足时宁可少给，不要为了凑数补写。\n"
    "2) 简短的主题或目标（如「机器学习」「帮我规划毕业论文」）：围绕它展开成 "
    "3-6 个一级分支，每个分支再按上面那条「还能不能再拆」继续往下拆。"
    "这是框架展开，不算捏造；但不要编造具体的数据、人名、文献或引用。\n"
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
)

SOURCED_DETAIL_PROMPT = DETAIL_PROMPT + (
    "注意：这次输入的是带了来源的材料，按上面第 1 种形态处理，"
    "不要因为主题简短就自行展开——所有内容都必须能在 sources 里找到依据。"
    # 深度规则在有来源时要换成「材料能支撑到多细」。不换的话会两错：
    # 一是把上面那句「继续拆」当成自由展开的许可，拆出来的层级靠常识补；
    # 二是反过来把「忠实整理」理解成只抄一层，材料里再多细节也不往下走。
    # 判据必须落在原文上，这样「拆得深」和「依据不足宁可少给」才是同一条规则。
    "「还能不能再拆」这条同样适用，判据只看材料：原文里还有更具体的表述支撑，"
    "就继续拆一层；材料到这里就没有更细的说法了，就停。"
    # 「更具体的表述」也要给形状。只说「更具体」时模型只去找正文句子，
    # 而原文自带的分层（标题、编号、列表、表格行、公式步骤）同样是依据，
    # 而且比「哪句更像细节」更稳定——它是原文写下的，不是模型判断出来的。
    "原文自己的分层就是最直接的依据：标题、编号、列表项、表格的行、公式的每一步，"
    "都是材料给出的分点——遇到它们就照原样落成一层，不要把一整节压进一个节点。"
    "拆得深不等于写得满——每一层新增的节点都必须能在原文里找到依据，"
    "材料里没有的内容一律不补，这一支宁可浅一点。"
    "用户输入为 sources 数组，每项含 source_id 和 text，text 是待整理的原文。"
    "每个有原文依据的节点增加 source_refs 数组，格式为"
    '[{"source_id":"输入中已有的来源ID","quote":"逐字摘录的原文片段"}]。'
    "quote 不超过 300 字，必须出自对应来源的 text：可以只截取原文里的一段，"
    "但不能改写、概括、拼接或跨来源引用。\n"
    "原文：会议决定在十月十七日发布二点零版本。\n"
    '❌ quote="计划近期上线" —— 这是概括，原文里逐字找不到这句\n'
    '✅ quote="十月十七日发布二点零版本" —— 原文里的连续片段\n'
    "没有直接依据时 source_refs 留空，不要编造引用。"
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

# 有来源时的限定句。`SOURCED_DETAIL_PROMPT` 的语义是「忠实整理材料」，思维模型
# 只能决定「按什么角度组织」，不能变成「自由展开」——不写这句，费曼那套「必须
# 举反例」会直接诱导模型编材料里没有的内容。
SOURCED_THINKING_GUARD = (
    "\n上面这套思考方式只用来决定「按什么角度整理」，不是展开许可："
    "所有节点仍然必须落在 sources 覆盖到的范围内，材料里没有的角度宁可不要，"
    "不要为了凑齐这套步骤而补写材料之外的内容。\n"
)

# `rag` 形态的提示词。**不能复用 `SOURCED_DETAIL_PROMPT`**：那一份写着「用户输入
# 为 sources 数组，每项含 source_id 和 text，text 是待整理的原文」，而 rag 形态里
# 一个字的正文都没有——照抄会让模型去找一个不存在的 text 字段，再把"没看见"读成
# "材料里没有"，最后交出一棵空心树。两份提示词共用 `DETAIL_PROMPT` 的树规则，
# 之后的每一句都只描述自己那种形态。
RAG_SOURCE_PROMPT = DETAIL_PROMPT + (
    "注意：这次输入的是带了来源的材料，按上面第 1 种形态处理，"
    "不要因为主题简短就自行展开——所有内容都必须能在检索到的原文里找到依据。"
    # 形态必须先讲清楚。模型不知道"还有东西没给我"时会把清单当成全文，直接从标题
    # 推内容；这类错最难查，因为树看起来有依据，其实全是常识补的。
    "\n你看不到材料全文：输入里只有一份材料清单（标题、字数、类型），正文一个字"
    "都没有进来。要拿到原文，只能调用 rag_query 检索。写任何与材料有关的内容之前，"
    "先检索。\n"
    "❌ 清单里有一份《预算管理办法》，就直接写「预算需经财务审批」——这是从标题猜的\n"
    "✅ 先 rag_query 问「预算审批流程」，拿到片段之后再落节点\n"
    # 引用怎么抄必须逐字段点名。只说"引用要准确"时，模型会给出一条 quote 看着对、
    # 偏移却自己算的引用；逐字校验（`optimization/source_refs._reference_problem`）
    # 会把它整条剔掉，界面上就成了"这篇材料一个字都没被引用"。
    "引用必须来自检索结果：source_refs 里每个字段都从 rag_query 的返回值里抄，"
    "不要自己算。"
    "source_id 用返回的 source_id；quote 用返回的 quote，整段照抄时连着 start、end "
    "一起照抄。"
    "返回的 quote 超过 300 字（引用字段的长度上限）时，只截取其中连续的、不超过 300 "
    "字的一段，这时不要写 start 和 end——偏移自己算是算不准的，而只给 quote 时"
    "只要求它逐字出现在原文里。\n"
    '❌ quote="预算由财务经理核准"——检索结果里没有这句，是你概括的\n'
    "✅ 把某条 hit 的 quote 原样抄过来（太长就截一段连续的），source_id 照抄\n"
    # 这一形态最容易出事的地方：模型看不见内容，却仍然要把导图填满。
    "检索不到就不要写：某个分支查不到依据，就如实留空，并在节点里写明"
    "「材料中没有相关内容」，不要为了填满导图编造。空着是正确结果，编造不是。\n"
    # 一次问"全部要点"召回必然泛；分开问才准。这是定位型检索本身的用法，
    # 不需要判断"够不够"的多轮判断器（那套实测发散，见 `docs/RAG-DECISIONS.md` 2.3）。
    "可以多次检索：不同方面、不同问法分开问，每次只聚焦一个问题。"
    "先问最具体的那一个（如「谁能批钱」），看结果再决定下一个问法；"
    "不要一次问「这份材料讲了什么」这种什么都沾一点的问句。\n"
    "没有直接依据时 source_refs 留空。不要输出 source_usage；不要猜测 page。"
)

# 与 `SOURCED_THINKING_GUARD` 同义、不同词：那一句的措辞是"落在 sources 覆盖到的
# 范围内"，而 rag 形态下 `sources` 根本不在输入里。共用一句会让模型去找那个不存在
# 的字段。位置同理必须排**在思维步骤之后**：它约束的正是上面那套步骤。
RAG_THINKING_GUARD = (
    "\n上面这套思考方式只用来决定「按什么角度整理」，不是展开许可："
    "所有节点仍然必须落在本次检索到的片段范围内，查不到的角度宁可不要，"
    "不要为了凑齐这套步骤而补写。\n"
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


def _truncated_output_error() -> GenerationError:
    """输出撞上 `max_tokens` 被硬切时的错误。

    文案只有这一份：一次性响应与流式响应都会拦到同一种截断，两处各写一句的结果
    是同一个事故在日志与界面上有两种说法。
    """
    return GenerationError("模型输出撞上长度上限被截断，结果不完整，请重试")


def _choice_content(body: Any) -> str:
    """从响应体里取出模型 content，并拦住被长度上限截断的输出。

    `finish_reason == "length"` 表示输出在 max_tokens 处被硬切。不拦的话，
    `parse_model_json` 里的 `_balance_brackets` 会把半截 JSON 的括号补齐，
    让它"合法"——一棵缺了半边的树就这样被当成成功结果返回，比直接失败
    难排查得多。`stop` 与字段缺失时照常解析：老版本的 mock、部分 OpenAI
    兼容端点都不返回这个字段，那不是"被截断"，只是"没说原因"。
    """
    choice = body["choices"][0]
    if isinstance(choice, dict) and choice.get("finish_reason") == "length":
        raise _truncated_output_error()
    return choice["message"]["content"]


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
    # 截断判定与一次性路径同源：宁可报错，也不要把补齐括号后的半份结果当成功。
    if finish_reason == "length":
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
    """
    payload = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=True) as client:
            async with client.stream("POST", base + "/chat/completions",
                                     content=payload, headers=headers) as response:
                response.raise_for_status()
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > _MAX_RESPONSE_BYTES:
                        raise GenerationError("模型响应过大")
        body = json.loads(buffer)
        # agent 的工具（生成 / 展开）走这条路径，没有 provider 那层的
        # finish_reason 检查，所以截断必须在这里拦住。
        return parse_model_json(_choice_content(body))
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        raise GenerationError(f"模型服务返回 HTTP {code}，请检查服务配置") from exc
    except (httpx.RequestError, httpx.InvalidURL, OSError) as exc:
        raise GenerationError("无法连接模型服务或请求超时") from exc
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise GenerationError("模型返回内容不是符合约定的导图 JSON，请重试") from exc


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
    request_body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens or MAX_OUTPUT_TOKENS,
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
) -> tuple[MindMapContent, list[Diagnostic]]:
    """以真实输入重建来源摘要，剔除模型伪造的关联，再执行契约与原文校验。

    agent 的 `mindmap_generate`（`app/agent/tools.py::_generate()`）走这个出口：
    无来源时在契约校验之前清空所有节点的来源声明，避免伪造摘要与引用互相背书，
    也不让错误的引用拖垮有效的树。

    `enforce_min_branches` 是**质量**门槛而不是契约门槛：模型整图生成要求一级
    分支不少于 `MIN_BRANCHES` 条，demo 占位结构与用户已保存的旧图不走这道关。
    """
    raw = deepcopy(result)
    diagnostics: list[Diagnostic] = []
    allowed_ids = {source.id for source in sources or []}
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
    if enforce_min_branches and len(content.root.children) < MIN_BRANCHES:
        raise _branch_shortfall(len(content.root.children))
    if sources is not None:
        diagnostics.extend(verify_source_refs(content, sources, remove_invalid=True))
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
