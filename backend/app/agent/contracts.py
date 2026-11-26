"""agent 循环的契约：请求、预算、模型交互、事件流。

这里只放**跨模块边界的数据结构**，不放业务逻辑。工具的真实实现（生成、展开、
检索、改树）在 `tools.py`，模型适配在 `provider.py`，循环在 `service.py`。

事件用 discriminated union 而不是一个大而全的模型：帧的种类会随需求增长，
用一个带十几个可选字段的类，调用方永远分不清"这个字段为 None 是因为不该有，
还是因为忘了填"。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Annotated, Any, Awaitable, Callable, Iterable, Literal, Protocol, Union

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from app.contracts.diagnostic import Diagnostic
from app.contracts.mindmap import DiagramContent, DiagramKind
from app.contracts.ops import OpBatch
from app.contracts.source import Source, SourceIdentity
from app.optimization.source_refs import EvidenceSpan

logger = logging.getLogger(__name__)

# 一次运行最多保留多少条命中区间。模型可能反复调 `rag_query`（改一条长材料时
# 实测能刷十几次），不设上限就是"内存随运行时长线性增长"。200 条覆盖 40 次
# `top_k=5` 的检索；超了丢**最旧**的，因为引用校验只需要"这条引用落在哪次命中
# 的哪一段"，而模型总是先检索、后生成——最旧的条目被丢时，对应的引用通常已经
# 写完了。丢的时候记 debug：这是容量策略生效，不是错误。
MAX_EVIDENCE_SPANS = 200

# 一次运行最多缓存多少份**从知识库回查出来的**来源（含正文）。库命中的
# `source_id` 是持久 id，不在本次 `request.sources` 里，收尾的引用校验要按它
# 逐字比对原文，就必须把 `Source` 先取回内存。
#
# 50 不是拍的：它是**与 `MAX_SOURCES` 对齐**（单次请求的材料份数上限，见本文件
# 下方的「材料」那一段）。库来源与请求内来源在校验里是同一种东西——`tools._verify_sources`
# 把两者拼成一个列表交给 `verify_source_refs`——所以库这边比请求那边更紧没有道理。
#
# 20 太小的后果是可复现的：一次检索命中 25 份库来源时，FIFO 把最旧的 5 份挤出去，
# 后 5 份的 `Source` 进不了缓存，那些来源上的引用全部被判 `SOURCE_TEXT_UNAVAILABLE`
# 并在 `remove_invalid=True` 那一步静默剔除——检索成功、引用写对，用户看到的却是
# "这一支没有证据"。库越大、问法越泛，这个窗口越容易撞上。
#
# 代价可控：一份典型材料 30 KB，50 份 1.5 MB，而一次运行只用一个进程。
# 超了仍丢**最旧**的：模型先检索、后写引用，最旧那批的引用通常已经写完。
MAX_LIBRARY_SOURCES = 50

# 记住最近多少次成功调用，供"连续重复调用"判据回溯。
#
# 判定窗口（`service.REPEAT_CALL_WINDOW = 3`）比这个数小，是故意的：窗口是
# **语义**（连续几次算重复），容量是**内存**。留出余量之后，将来把窗口调大
# 不需要动这份状态，也不会出现"窗口 5 但只记了 3 条"的静默错判。
# 内存量级：一条 = 一个 call_id + 一串参数 JSON。工具参数只有话题、节点 id、
# label、问句这类短字段——材料正文在 `request.sources` 里，从不进工具参数。
MAX_RECENT_CALLS = 8


# ---------------------------------------------------------------- 预算


# 模型调用额度的上界。理由是算术，不是口味：分片生成的每一次调用恰好展开一个
# 父节点，每次至少落 `DEEP_MIN_BREADTH`（2）个节点，所以 1000 节点的承载上限
# 对应约 500 次调用；取 400 是"覆盖绝大部分反复扩展的场景，同时仍拦得住跑飞的
# 循环"的那一侧。要更高得重新算这条算式（见 `_initial_call_plan` 的注释）。
MAX_MODEL_CALLS_LIMIT = 400

# 初次生成的规模，与 `generation.budget` / `agent.tools` 共用同一组数。
_INITIAL_BRANCHES = 6
_INITIAL_FANOUT = 3
# 每支最多一次补点（`tools.SUPPLEMENTARY_BRANCH_LIMIT`），逐支记得上界是分支数。
_INITIAL_RETRIES = _INITIAL_BRANCHES
# 外层对话轮次：一轮发起工具调用、一轮收尾（`_budget_stop` 那两帧之间）。
_OUTER_ROUNDS = 2


def _initial_call_plan() -> int:
    """一次完整初次生成需要多少次模型调用——**算出来的，不是拍的**。

    算式就是分片生成的结构本身：骨架产出第 1 层，之后**每次调用展开一个父节点**，
    而最后一层是由上一层写出来的、不再需要展开。所以

        骨架 1 + 展开 6 个分支 + 展开 18 个二级 = 25 次内容调用

    注意这里**不是** 79：79 是 4 层满额树的**节点数**（1+6+18+54），不是调用数。
    把最后一层也算成一次调用会得出 79，进而误判"80 的预算装不下 4 层树"——那正是
    差点让初次生成白白降到 3 层的算术错误（实测 `branches=6, points_per_branch=3`
    的完整初次生成用掉 25 次）。

    再加"要点不足的那几支各补一次"（≤6，`SUPPLEMENTARY_BRANCH_LIMIT`）与收尾两轮
    （2）= 33。

    这条函数存在的意义不是算出一个精确数字，而是让"预算够不够"变成可验证的：
    `tests/test_budget.py` 与 `tests/test_generation_limits.py` 用它去断言默认预算
    装得下一次满额 4 层树、且那次生成 `partial=false`。写死一个数之后，将来谁把
    初次深度调到 5 层，没有任何地方会红。
    """
    from app.generation.budget import INITIAL_MAX_DEPTH, calls_for_levels, level_sizes

    sizes = level_sizes(_INITIAL_BRANCHES, _INITIAL_FANOUT, INITIAL_MAX_DEPTH + 1)
    return calls_for_levels(sizes) + _INITIAL_RETRIES + _OUTER_ROUNDS


# 一次 run 里按几倍"初次生成"配额度。
#
# 不是拍脑袋的倍数：一个真实会话里，用户会**重做一次**（第一张不合意就再来一张，
# 而失败的那次同样烧调用），之后还会点几次「展开这个节点」（每次 1 次调用）。
# 3 倍覆盖"初次 + 重做一次 + 十几个扩展"，同时仍是一道能在跑飞的循环上刹住的闸。
_INITIAL_CALL_PLANS_PER_RUN = 3


# 工具调用额度的上界。装配在「一次工具调用 = 一次模型往返」这条算术上，而不是
# 一个整数能写多大：`max_tool_calls` 每加一，`max_model_calls` 至少要跟得上一次
# provider 请求，所以它必须留在模型额度之下（见 `max_tool_calls` 的字段注释）。
# 120 是"够细粒度工具改一百来处"的量级，同时仍小于 `MAX_MODEL_CALLS_LIMIT`
# 的 400——两者不是同一个量纲，不该共用一个天花板。
MAX_TOOL_CALLS_LIMIT = 120


def _default_max_model_calls() -> int:
    """默认额度 = 初次生成需要量 × 3，向上取整到 100。

    33 × 3 = 99 → 100。这个数比旧的 80 高，但**高的理由不是"猜着不够"**：
    4 层满额树要 33 次（含补点与外层轮次），一个会话里再来一张就是 66，加十几次
    扩展正好一百上下。

    旧值 80 为什么"被烧光"：它对应的是**没有层数上限**的年代——7 层、240 个节点
    的树要一百多次调用，80 在第 6/7 层用尽，于是最下面两层整片留空。深度锁到
    4 层之后 80 其实够用；这次的改动是让默认值与算式对齐，并把上界从 120 放到
    400（要更大规模的用户配得上，而不是被一个为"六支分片"拍的数字挡住）。
    """
    return -(-_initial_call_plan() * _INITIAL_CALL_PLANS_PER_RUN // 10) * 10


class RunBudget(BaseModel):
    """一次 run 的资源上限。

    `max_model_calls` 数的是**真实调用次数**，不是外层轮数：循环里每一轮 provider
    调用、以及工具内部的每一次 `chat_async` 调用都算一次，共用 `RunContext` 上
    同一个账本。默认值按最贵的一条路（六支分片）算，算式见字段注释：
    「查 → 建 → 改 → 修」这种多轮对话只用其中几次，多出来的余量不会被无故花掉。

    `max_output_tokens` 同时约束两条出口：provider 那一轮，以及工具内部每一次
    `chat_async`——参数由 `tools._max_tokens()` 夹到 provider 认的上限再发出去。
    只约束前者时，工具内这条出口等于没有边界。

    `max_repairs` 约束的是**工具执行失败次数**（`ToolResult.ok == False` 的次数），
    与模型调用次数无关：一次成功的模型响应里发三个工具、其中两个失败，只消耗
    这里两次。全轮共享而不是按轮重置——按轮重置等于给模型无限次重试，
    每次重试都照样计费。
    """

    model_config = ConfigDict(extra="forbid")
    # 预算按「放开层数之后实际会走多深」定，不再按固定层数的算式拍。
    #
    # 深层生成的深度上限已经统一到契约的 `MAX_TREE_DEPTH`（8 层，见
    # `tools.DEEP_MAX_DEPTH`），某一支要不要继续拆由模型的 `expandable` 决定。
    # 实测：预算 40 时六支只走到第 5 层（含中心主题）就用完了，产出 159 个节点、
    # 耗时 35 秒——也就是说 40 已经成了比层数更硬的限制，正好卡在"刚有点意思"
    # 的地方停下。80 对应约 6~7 层、70 秒上下，仍在 `max_seconds` 之内。
    #
    # **默认值现在由"初次 4 层满额树"的真实调用数算出来**（`_initial_call_plan()`）：
    #
    #     骨架 1 + 展开 6 个分支 + 展开 18 个二级 = 25 次内容调用
    #     + 每支最多一次补点（6） + 外层两轮对话（发起 / 收尾）= 33
    #
    # 默认取 3 倍（100）：一个真实会话里还会重做一张、再点十几次扩展。旧值 80 对应
    # 的是"没有层数上限"的年代（7 层 / 240 节点要一百多次调用），80 在第 6/7 层
    # 用尽、最下面两层整片留空——深度锁到 4 层之后，问题不再出在预算上。
    #
    # 上界放到 400：分片生成的每一次调用恰好展开一个父节点，而每次展开至少落
    # 2 个节点（`DEEP_MIN_BREADTH`），所以 1000 节点的承载上限对应约 500 次调用；
    # 400 覆盖绝大多数反复扩展的场景，同时仍是一道能拦住"跑飞的循环"的闸。
    # 想再高就得重新算这条算式，而不是把数字调大。
    max_model_calls: int = Field(
        default=_default_max_model_calls(), ge=1, le=MAX_MODEL_CALLS_LIMIT)
    # 50 不是"容量"，是**给细粒度工具留的空间**。
    #
    # 8 在它被写下的那一年是对的：那时 `create` 只有一条路（一次
    # `mindmap_generate_deep` 就是整张图），8 挡的是"查一下→再查一下"的死循环
    # ——实测 10 次里 10 次都先检索一遍、把 8 次用光、一次生成都没调。那时的
    # 8 是一道闸，不是一条容量线：正常路径只用 1–2 次。
    #
    # 现在 `edit` 意图有七个写工具（四个细粒度 + 两条边 + 批量），改 9 处以上
    # 细粒度就**物理装不下**：第 9 次调用会被判 `budget_exhausted`，用户看到
    # "工具调用次数已用完"而图只改了一半。所以闸门必须放开。
    #
    # 放开闸门就得补真防线（`service.REPEAT_CALL_WINDOW` / `NO_PROGRESS_*`）：
    # 数字变大之后，"模型原地打转"不再会被次数上限自然掐断，只能靠
    # 「连续重复调用」与「连续无进展」两条判据去认。上限 120 是同样的口径
    # （够改一百多处），而不是把闸拆掉。
    #
    # **与 `max_model_calls` 的关系**：两者量纲不同——工具调用是"模型发来一个
    # tool_call"，模型调用是"一次 provider 请求"。但每一次工具调用之后，模型都要
    # 再看一眼结果才能决定下一步，所以 **50 次工具调用 ≈ 50 次模型往返**（一次
    # completion 里并发发多个 tool_call 时才会少几次）。默认 `max_model_calls`
    # 是 100，正好是"50 次工具 + 50 次收尾余量"的两倍，不会先撞上它。
    # 反过来说：把 `max_tool_calls` 调到 100 以上而不动 `max_model_calls`，
    # 就不再是工具额度先停，而是模型额度先停——两个上限必须一起看。
    max_tool_calls: int = Field(default=50, ge=1, le=MAX_TOOL_CALLS_LIMIT)
    # 时间上限从 120 提到 600：120 秒是照「一句话主题」的短路径拍的，实测
    # 12106 字材料出 526 个节点要 48.6 秒，慢速档 97 tok/s 时同样的材料光是
    # 输出就要 136 秒，多轮工具循环必然超时。上限放到一小时：这是"卡住不动"
    # 的物理边界，不是给正常路径设的目标值。
    max_seconds: float = Field(default=600.0, gt=0, le=3600)
    # 这是**单次**模型调用的输出上限，不是整轮累计——它直接作为 provider 请求里
    # 的 `max_tokens` 发出去，每次调用都按这个值重新计。整轮累计的上界是
    # 「这个值 × 实际调用次数」，而调用次数由 `max_model_calls` 约束，因此
    # 不需要再加一个"累计输出"字段：两个上界相乘已经封顶。
    #
    # 默认值 65536 = 实测 DeepSeek 接受的最大 max_tokens（HTTP 200）。按每节点
    # 25 token 算装得下 2600 个节点，高于契约允许的 1000 个。上界仍是 131072
    # ——它比默认值宽，不构成瓶颈；真正发出去的值还会被 provider 的
    # `_MAX_TOKENS_CAP` 夹一次。
    max_output_tokens: int = Field(default=65536, ge=1024, le=131072)
    # 容忍几次工具执行失败。模型第一次把 label 写错位置是常态，它看到错误后
    # 下一轮会改对；超过这个次数说明它卡住了，继续烧调用没有意义。
    # 计的是累计失败数，不是"连续失败数"：成功的工具调用不会把计数清零。
    max_repairs: int = Field(default=1, ge=0, le=5)


# ---------------------------------------------------------------- 模型交互


class ToolCall(BaseModel):
    """模型发起的一次工具调用。

    `raw_arguments` 与 `parse_error` 是为了区分两种失败：模型给了合法的 JSON 但
    字段不对（交给 ToolSpec 的参数模型报错），还是模型把 JSON 写坏了（这里就
    记下来）。两者都要能变成一条可回给模型的具体错误，而不是整个 run 崩掉。
    """

    call_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: str | None = None


class Completion(BaseModel):
    """一轮模型调用的结果。

    `message` 原样保存，因为它要连同 `tool_calls` 一起回填进下一轮的消息历史——
    少了它，模型看到的"自己说过的话"就是不完整的，`tool_call_id` 也对不上。
    """

    message: dict[str, Any]
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    # 响应体里的 `model`：**服务端说它实际跑了哪个模型**。
    #
    # 与请求里写出去的名字（`provider.model_name`）是两件事：实测这个 API 接受
    # `deepseek-chat` 并把请求路由到 `deepseek-flash`（两个名字是同一个模型的两个
    # 别名，区别只在思不思考）。把响应里的这一位丢掉，界面上就只能显示"我要的那个
    # 名字"，用户永远看不出请求被路由到了别处。
    #
    # 缺省 None 而不是空串：没这个字段＝对面没说，界面据此写"未知"，不编一个名字。
    served_model: str | None = None


class ToolProvider(Protocol):
    """模型适配层接口。

    抽这一层不是为了好看：循环里的逻辑（结果配对、幂等、去重、预算、终止判断）
    全是最容易出错也最该被测的部分，但它们跟"谁来产生工具调用"无关。有了接口，
    测试可以用一个按脚本返回预设结果的实现驱动循环，不依赖网络、不耗额度、
    不会随机失败。
    """

    async def complete_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        budget: RunBudget,
    ) -> Completion: ...


# 工具 -> 循环的上报通道。用 dict 而不是具体的事件模型：工具不该依赖事件流的
# 形状（事件模型与编号属于调用层），循环收到后自己校验、补 seq、丢掉不认识的帧。
EmitFn = Callable[[dict[str, Any]], Awaitable[None]]


# 工具 -> 落库的检查点通道。参数是"那一刻已经长好的树"，调用方负责写进 `runs`。
#
# 与 `EmitFn` 一样是**异步**回调，理由却不是对称美学：落库要把整棵树序列化再写
# SQLite，而这条调用发生在工具内部——工具本身是 async 的、跑在事件循环上。同步
# 写库在这里等于拿整个循环去等一把写锁（`BEGIN IMMEDIATE` 撞上别的写入时最长
# 等满 `sqlite3.connect` 的忙等上限），同一个进程里别的 SSE 流会跟着一帧都发不
# 出来。交给线程池之后，阻塞被关在那个 worker 里。
#
# 用回调而不是让工具直接 import `app.agent.runs`，是为了让工具层不知道"运行台账"
# 这件事——它只需要"我这儿有一棵可以交出去的树了"。
CheckpointFn = Callable[[DiagramContent], Awaitable[None]]


class ToolResult(BaseModel):
    """工具执行结果。

    `content` 回给模型（自然语言或 JSON 串），`data` 只用于事件流与后续逻辑，
    不塞进消息历史——工具的内部数据（比如完整的新树）进上下文会迅速吃满窗口。
    """

    ok: bool
    content: str
    data: dict[str, Any] = Field(default_factory=dict)


@dataclass
class ToolSpec:
    """一个可被模型调用的工具。

    用 dataclass 而不是 BaseModel：要装 `args_model` 这个类和 `fn` 这个函数，
    pydantic 存它们需要 arbitrary_types_allowed，徒增噪音。

    参数校验用 pydantic 模型而不是 JSON Schema 库：`jsonschema` 在本机是 docling
    的传递依赖，并不在 requirements.txt 里，靠它等于给 CI 埋一个"本地能过、
    CI 崩"的雷。
    """

    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[[Any, "RunContext"], Any]
    # 只读工具才允许按参数去重。写操作（改树）即使参数完全相同，第二次执行时
    # 树的状态已经变了，返回上次的结果等于谎报成功。
    readonly: bool = False
    # 不给模型看的参数名：`schema()` 会把它们从 `properties` 里删掉。
    #
    # 用在"这个参数已经由请求层定死"的场合（比如用户在界面上选了图型）。
    # **只是不给它看，不是不许它写**：`args_model` 里字段还在，模型凭老习惯补一个
    # 也照样校验通过、随后被请求覆盖。反过来做（字段删掉 + `extra="forbid"`）的
    # 代价是一次"参数不合法"，模型下一轮才改对——为一个我们本来就要覆盖的值
    # 白烧一轮调用。
    hidden_args: tuple[str, ...] = ()

    def schema(self) -> dict[str, Any]:
        """转成 OpenAI tools 格式。

        `hidden_args` 里的字段从这里抹掉：模型读的就是这份 JSON Schema，看不见的
        参数它不会去填。`model_json_schema()` 每次返回新对象，就地删不会污染
        别的 spec。

        `required` 也一起清：隐藏一个必填字段等于让这个工具**永远**调不通
        （模型不知道要填，服务端又要求它有），而那种失败在日志里长得像"模型不会
        用工具"。当前唯一的隐藏字段是有默认值的，这一条是给将来的调用方留的护栏。
        """
        parameters = self.args_model.model_json_schema()
        if self.hidden_args:
            properties = parameters.get("properties")
            if isinstance(properties, dict):
                for key in self.hidden_args:
                    properties.pop(key, None)
            required = parameters.get("required")
            if isinstance(required, list):
                parameters["required"] = [
                    key for key in required if key not in self.hidden_args
                ]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }


@dataclass
class RunContext:
    """一次 run 的可变状态。

    跨轮存活，所以不含任何与模型对话相关的东西——那些在 `service.run()` 的
    局部变量里，生命周期更短也更清楚。
    """

    request: "AgentRunRequest"
    # 这一轮正在长的那棵树。**名字是历史遗留**：候选层删掉（`acfdb5b`）之后，
    # 它不再"等用户点应用"，`done` 帧一到就整树交给前端落进画布（前端标成未保存）。
    # 改名成本高、收益低，所以字段名留着，读它时按"生成中的工作树"理解。
    #
    # **初值不是 None**：`edit` / `expand` 两个意图必须带 `request.snapshot`
    # （`router._validate` 强制），构造 ctx 时它就落进这个字段。所以"candidate 非空"
    # 只说明"手上有一棵树"，不说明"这一轮产出过东西"——判"该不该把树当成模型交付"
    # 必须看下面的 `produced`。
    candidate: DiagramContent | None = None
    # 这一轮**真的产出过**树（`publish_tree()` 置位）。它是 `_deliver_or_fail`
    # 唯一该信的判据。
    #
    # 为什么不能用 `revision > 0`：语义上 `revision` 是"批次序号"，将来若拿它做
    # 别的比较（比如前端对账），会连带改掉"半棵树要不要交"这个用户可见的决定。
    # 一个只管交付判据的字段，改它时只需要想清楚交付这件事。
    produced: bool = False
    revision: int = 0
    batches: list[OpBatch] = field(default_factory=list)
    sources_by_id: dict[str, Source] = field(default_factory=dict)
    # call_id -> (参数指纹, 结果)。存指纹是为了识别"同一个 id 换了参数"这种协议违规，
    # 那必须报错而不是返回旧结果。
    executed: dict[str, tuple[str, "ToolResult"]] = field(default_factory=dict)
    # (工具名, 规范化参数) -> 结果。只对只读工具生效。
    dedupe: dict[str, "ToolResult"] = field(default_factory=dict)
    # 最近若干次**成功执行**的 (call_id, 参数指纹)，供"连续重复调用"判据使用
    # （`service._duplicate_anchor`）。
    #
    # 为什么记在 ctx 上而不是循环的局部变量里：判定点在 `_execute_once`（那里才
    # 同时拿得到 call_id 幂等、只读缓存与工具注册表三样东西），而它是独立函数。
    # 为什么只记成功的：一次失败的检索/写入之后，模型用**完全相同的参数**重试是
    # 合法恢复动作；把它也算成"重复"等于在模型最需要重试时堵住它。
    recent_calls: list[tuple[str, str]] = field(default_factory=list)
    # 循环层自己产出、不来自任何一次工具调用的诊断。目前只有 `NO_PROGRESS`
    # （连续多次工具调用没有改变候选树）。
    #
    # 与 `ToolResult.data["diagnostics"]` 并存而不是并进去：那一条路上诊断是
    # **某次工具调用**的产物，随 `tool_end` 一帧落到前端；无进展是**跨调用**的
    # 事实，不属于任何一次调用——硬塞进最后一次 `ToolResult` 会让"这条诊断说的是
    # 哪一次"变成需要猜的事。
    loop_diagnostics: list[Diagnostic] = field(default_factory=list)
    # 模型调用的**权威账本**：外层每一轮 provider 调用与工具内部每一次 chat_async
    # 调用共用这一个计数。额度必须在请求发出**之前**通过 `reserve_model_call()`
    # 占掉——事后记账等于先花钱后记账，工具执行期间预算就会被超支使用。
    model_calls_used: int = 0
    # 工具内部直接调模型的次数，由工具自己上报。它是对账兜底与观测值，
    # **不**参与判停：判停看 `model_calls_used`，只有它同时覆盖外层与工具内部。
    extra_model_calls: int = 0
    # 工具内部的上报通道。`service.run()` 在执行工具期间挂上它，工具每产出一个
    # 可以上屏的增量就 await 一次，由循环转成事件帧。
    #
    # 默认 None 是硬要求：单测与直接调工具的代码没有消费者，这条路上工具的行为
    # 必须与加通道之前**逐字相同**（连流式请求都不发）。判空只在
    # `tools._emit_grow` 与 `tools._GrowStream` 的入口处做一次。
    emit: EmitFn | None = None
    # 检查点通道。`service.run()` 在整轮运行期间挂上它，工具在"树已经能读了、但还要
    # 接着跑很久"的那一刻调用一次，由循环写进 `runs` 表。
    #
    # 与 `emit` 同样默认 None、同样要求判空只在调用点做一次：单测与直接调工具的代码
    # 没有台账可写，这条路上工具的行为必须与加通道之前逐字相同（连一次库调用都没有）。
    #
    # 为什么是"回调"而不是工具直接调 `runs.checkpoint()`：工具层不该知道运行台账的
    # 存在。它只知道"我这儿有一棵可以交出去的树"，落不落库、落到哪由调用层决定。
    checkpoint: CheckpointFn | None = None
    # 本次运行累积的命中区间（`tools._rag` 每检索一次并一批进来）。它是
    # `verify_source_refs(..., evidence=...)` 的唯一输入，也是"父块模式"下唯一能
    # 区分"引用落在命中的子块里"与"引用落在父块里与查询无关的一半"的东西。
    #
    # 为什么存在这里而不是 `ToolResult.data`：hits 只活在 `_rag` 的返回值里，而
    # 校验发生在生成工具的收尾（`tools._generate_deep` 的 `verify_source_refs`），
    # 中间隔着整个循环。`ToolResult.data` 是**一次调用**的产物，循环从不汇总它；
    # 要汇总就得在 `service.run()` 里按工具名特判 `data["hits"]` 的形状，等于让循环
    # 认识检索工具的私有字段。`RunContext` 本来就是"跨轮存活的可变状态"，证据正好
    # 是这种东西，且 `_rag` 与 `_generate_deep` 拿的是同一个 ctx，不需要新管道。
    evidence: list[EvidenceSpan] = field(default_factory=list)
    # 本次运行里从知识库回查出来的来源（持久 id -> Source），`_rag` 命中后填入。
    #
    # 它存在的**唯一**理由是收尾的引用校验：`verify_source_refs` 用
    # `by_id = {source.id: source}` 查原文，而库命中的 `source_id` 是持久 id、
    # 不在 `request.sources` 里。不把它并进校验，一条逐字正确的库引用会被判成
    # "来源不存在（SOURCE_TEXT_UNAVAILABLE）"、在 `remove_invalid=True` 时被静默
    # 剔除——检索成功、引用写出、最后收尾时消失，这是整条链上最隐蔽的一种失败。
    #
    # 只装**真的被本次检索命中过**的库来源，不是整库：校验的语义是"你引的东西
    # 这次确实查到过"，而不是"库里恰好有这份材料"。
    library_sources: dict[str, Source] = field(default_factory=dict)
    # 「这次图型是怎么定的」以及从 `goal` 里剥掉的那一段（见 `diagram_intent`）。
    #
    # 默认 `None` = **这一层没跑**（单测与直接构造 `RunContext` 的代码就是这种）。
    # 这个默认值是有用的：`tests/test_agent_diagram_type.py` / `test_edges.py` 用
    # 旧模块钉着"请求不带图型时产出逐字节一致"，那些断言里 `data` 没有 `source`
    # 与 `stripped` 两个键——工具只在**解析过**时才补它们（见 `_generate_deep` 的
    # 「图型与附加边」那一段）。
    diagram_source: str | None = None
    diagram_stripped: str | None = None
    # `audit_tree` 最后一次数出来"还有几条一级分支没有子节点"。0 表示上次体检
    # 是干净的，`None` 表示这次运行**从没体检过**——两者必须分得开：没体检过时
    # 循环不该替模型判断"它是不是漏了"，那是凭空多出来的停止条件。
    #
    # 谁写谁读：`tools._audit_tree_tool` 写（每次体检覆盖），`service._loop` 在
    # 模型准备收尾时读。放在 ctx 上而不是 `ToolResult.data` 里，理由与
    # `loop_diagnostics` 相同——它是**跨调用**的事实，不属于任何一次调用。
    audit_missing: int | None = None
    # 「已经提醒过模型一次了」。收尾提醒必须是一次性的：模型听完还是要停，
    # 那是它的判断（或者它有自己的理由），再催就变成代码替它写这张图。
    audit_nagged: bool = False
    # 这次运行要在 system 里注入的**记忆那一段**（`app/memory/store.py` 检索出来的，
    # 已经渲染成文本；没有命中时是空串）。
    #
    # 为什么是 `RunContext` 而不是 `_system_text` 里现算：那个函数**每一轮**都会被
    # 调一次（`_refresh_outline`），检索放进去就是 N+4 次 embedding（骨架 1 次 +
    # 结构 1 次 + detail 2 次 + 工具循环 N 轮），每次 0.3–1 秒，延迟翻几倍。算在
    # `_loop` 开头、存在这里、之后所有段照抄——这是这次改动里最硬的一条性能约束。
    #
    # 默认空串是硬要求：单测与直接构造 `RunContext` 的代码没有检索过，注入的那一段
    # 必须与加这个字段之前**逐字节相同**（`_system_text` 里那个 `if part` 过滤）。
    memory_hints: str = ""
    # 这次运行拼进 **user 末尾**的**风格画像**那一段（`app/prefs/style.py` 从历史
    # 产物里数出来的形状描述；没有画像时是空串）。
    #
    # 与 `memory_hints` 分开两个字段，而不是合成一段：两者的位置不同（记忆在
    # system、画像在 user），可信度也不同（记忆是"关于这个人的话"，画像是
    # "他过去的图长什么样"）。合成一段就得在 `_system_text` 里再拆开，而拆分
    # 依据是一个字符串前缀——那是把位置信息编码进文本内容，最容易被改坏。
    #
    # 默认空串同样是为了"接线没接通时与旧版本逐字节相同"。
    style_hints: str = ""
    # 这次运行的**内容方向池**（`app/prefs/profile.py` 的 `Direction` 列表）与它的
    # 池键。方向按图隔离之后，它必须"一次运行算一次"：
    #
    #   - 算一次：从这批材料的块向量聚类，实测 ~100 ms（60 样本 × 1024 维 × 球面
    #     k-means）。20 支分支各算一遍就是 20 × 100 ms 的纯 Python 循环抢 GIL，
    #     而它们读的是**同一批材料**，算出来也必然是同一批方向（聚类是确定性的）；
    #   - 存 ctx 而不是 `lru_cache`：缓存键是"这批材料"，而 `Source` 不可哈希，
    #     拿 `id(sources)`、文件名或材料份数做键都会在"换了一批材料但份数相同"时
    #     把上一张图的方向喂给这一张图——那是隔离最不该有的漏洞。
    #
    # `None` = **还没算**（而不是"没有方向"）；算出来是空列表才是"这张图没有方向"。
    # 两者必须分得开，否则每次调用都会重算一遍那个"确定没有"的结果。
    profile_directions: list[Any] | None = None
    #: 上面那批方向属于哪个池（`profile.pool_key()` 的结果）。池变了就重算。
    profile_pool: str = ""

    def record_library_sources(self, sources: Iterable[Source]) -> None:
        """把库来源并进缓存：跳过本次请求已带的、重复的，超上限丢最旧的。

        跳过请求内来源不是优化而是正确性：同一份正文可能既是本次上传的、又在库里
        （`source_id` 不同），两个身份都要留着——引用写的是哪个身份就按哪个校验，
        缓存只负责"库那个身份查得到原文"。

        逐出策略与 `record_evidence` 同向（FIFO 丢最旧）：模型先检索、后写引用，
        最旧那批的引用通常已经写完，丢它的代价最小；而"拒绝新增"会让长循环里
        后面所有库引用都失去校验依据。
        """
        for source in sources:
            if source.id in self.sources_by_id or source.id in self.library_sources:
                continue
            self.library_sources[source.id] = source
        overflow = len(self.library_sources) - MAX_LIBRARY_SOURCES
        if overflow > 0:
            for source_id in list(self.library_sources)[:overflow]:
                del self.library_sources[source_id]
            logger.debug("库来源缓存达到 %d 份上限，丢弃最旧的 %d 份",
                         MAX_LIBRARY_SOURCES, overflow)

    def record_evidence(self, spans: Iterable[EvidenceSpan]) -> None:
        """把一批命中区间并进本次运行的证据集：去重、限量、丢最旧。

        去重键是 `(source_id, start, end, leaf_start, leaf_end)`——比"父块三元组"
        多两位，理由是可证伪的：同一个父块被两次不同问法命中时，两次命中的**子块**
        往往不同（父块 450–700 字，子块 200 字上下，一个父块 2–4 个子块），而校验
        问的正是"这条引用落在哪个子块里"。只按父块区间去重会把后一次的叶子区间
        丢掉，那条本来合法的引用随后被判成"落在父块里与查询无关的部分"——用一次
        去重换来一次系统性误伤，代价方向完全错了。重试同一次检索时两个叶子区间
        也相同，所以"同一批 hits 累两次只有一份"照样成立。

        逐出策略是 FIFO 而不是"拒绝新增"：新证据总是比旧证据有用（模型刚检索完
        就会写引用），而拒绝新增会让长循环里后面所有引用都失去校验依据。
        """
        seen = {
            (span.source_id, span.start, span.end, span.leaf_start, span.leaf_end)
            for span in self.evidence
        }
        for span in spans:
            key = (span.source_id, span.start, span.end, span.leaf_start, span.leaf_end)
            if key in seen:
                continue
            seen.add(key)
            self.evidence.append(span)
        overflow = len(self.evidence) - MAX_EVIDENCE_SPANS
        if overflow > 0:
            del self.evidence[:overflow]
            logger.debug("证据区间达到 %d 条上限，丢弃最旧的 %d 条",
                         MAX_EVIDENCE_SPANS, overflow)

    def record_call(self, call_id: str, fingerprint: str) -> None:
        """记一次**成功执行**的调用，供"连续重复调用"判据回溯。

        只由 `service._execute_once` 在工具真的返回 `ok=True` 之后调用。失败的
        调用不进这里，理由写在 `recent_calls` 的字段注释里：用相同的参数重试一次
        失败的检索/写入，是模型正常的恢复动作，不该被判成"重复"。

        容量是 `MAX_RECENT_CALLS`（比判定窗口宽），超了丢最旧——判据只看最近几次，
        更早的条目即使留着也不会被任何一次判定读到。
        """
        self.recent_calls.append((call_id, fingerprint))
        overflow = len(self.recent_calls) - MAX_RECENT_CALLS
        if overflow > 0:
            del self.recent_calls[:overflow]

    def reserve_model_call(self) -> bool:
        """占掉一次模型调用额度，拿不到返回 False。

        循环与工具都走这里，所以这段判断只该有一份实现：两处各写一份判断、
        各记一份数，迟早出现「判的时候以为还有额度、记的时候又多记一次」的
        分叉——预算为 1 的 run 实测发出过 4 次真实请求，就是这么来的。
        """
        if self.model_calls_used >= self.request.budget.max_model_calls:
            return False
        self.model_calls_used += 1
        return True

    def publish_tree(self, content: "DiagramContent") -> None:
        """把这一轮新长出的树交出去：写候选、记批次号、标"这一轮产出过"。

        三件事必须一起做，所以只留这一个入口。四处产出点（`_generate` /
        `_generate_deep` / `_expand` / `_commit_drafts`）从前各写一行
        `ctx.candidate = ...` 一行 `ctx.revision += 1`，加产出标记时只要有一处漏写，
        那条路上"半棵树要不要交"就退回到按 `candidate` 判——而 edit/expand 下
        `candidate` 初值就是用户原树，漏一处就多一条假成功。
        """
        self.candidate = content
        self.produced = True
        self.revision += 1


# ---------------------------------------------------------------- 请求


# 单次请求能带的来源份数上限。**这也是清单的条数上限**：清单就是 `sources` 这份
# 数组渲染出来的，条数上限与它同一个数。分两次写（这里 50、渲染器 50）迟早分叉，
# 而分叉的表现是"请求收下了、清单里却少两份"，模型连这两份材料存在都不知道。
MAX_SOURCES = 50


class AgentRunRequest(BaseModel):
    """一次 agent 运行。

    `snapshot` 是前端当前的图。服务端**不**从数据库重读——用户可能有一堆没保存
    的编辑，重读数据库等于把它们丢掉。
    """

    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=1000)
    intent: Literal["create", "expand", "edit"] = "create"
    # 这次运行的**身份**。带同一个 id 重复投递时，服务端据它决定"真跑 / 409 /
    # 直接回放已有候选"，见 `app/agent/runs.py`。
    #
    # 由调用方给、可选，而不是服务端自己发一个再返回：服务端发的话，客户端要
    # 先请求一次才能拿到 id，那一次请求本身就成了"第一次运行"——刷新重发时它
    # 照样没有 id 可用，等于什么都没解决。老客户端不带也能跑（服务端补一个 uuid4），
    # 只是拿不到幂等。
    #
    # 字符集收窄到 `[A-Za-z0-9_.-]`：这个值会进 `GET /api/agent/runs/{run_id}` 的
    # 路径。放开 `/` 与 `..` 会先在路由层制造一串"看起来像另一个接口"的路径。
    run_id: str | None = Field(default=None, min_length=1, max_length=64,
                               pattern=r"^[A-Za-z0-9_.-]+$")
    # 同一句主题换一种拆解方法，结构就不同（表在 `generation.service.THINKING_MODELS`）。
    # 用 Literal 而不是自由字符串：拼错的值会被 `model_config` 挡在开流之前，
    # 否则它会静默退化成 default，用户在下拉里选的东西等于没生效。
    thinking_model: Literal[
        "default", "first-principles", "feynman", "systematic", "critical"
    ] = "default"
    # 图型：界面上那个下拉里用户选的东西。**None = 让模型自己判断**，也就是
    # "用户没说"——这一条是向后兼容的全部内容：老前端不带这个字段，走的就是
    # `args.diagram_type` 那条老路，行为与加这个字段之前逐字节相同。
    #
    # 为什么必须有这一层：模型看不见界面。「画个流程图」这句话在 `goal` 里被它
    # 当成主题描述消化掉之后，它不会再回头把工具参数设成 `flowchart`——实测三次
    # 里三次都走 `GenerateDeepArgs.diagram_type` 的默认值 `tree`，用户看到的是
    # 一张思维导图。只要"这个参数模型可以不填"，它就会不填；把它提到请求层，
    # 「用户说了要流程图」这件事就不再依赖模型的记性。
    #
    # 用 Literal 而不是自由字符串，理由与 `thinking_model` 同：拼错的值在开流
    # 之前就该被拒，而不是静默退化成"自动判断"。
    diagram_type: DiagramKind | None = None
    # 「这一次图型是怎么定的」，由 `diagram_intent.apply()` 在开流之前写：
    # `request`（下拉）/ `inferred`（从 `goal` 的文字里读出来）/ `model`（两边都没说）。
    # 附带的 `_diagram_stripped` 是那次从 `goal` 里真的剥走的那一段原文。
    #
    # **私有属性**（不是 pydantic 字段）：它不序列化、不进 OpenAPI、不进前端镜像，
    # 也不该进——它是"这次请求内部发生的一件事"，对外面的契约面没有一个字的增加。
    # 前端要的答案在别的两个地方：产物里的 `MindMapContent.diagram_type`（这张图
    # 是什么），与工具结果的 `data["diagram"]`（它是怎么定下来的）。
    _diagram_source: str | None = PrivateAttr(default=None)
    _diagram_stripped: str | None = PrivateAttr(default=None)
    # 这一次运行**实际带着哪些偏好**，形如 `["diagram_type.default=fishbone"]`。
    #
    # 由调用方（前端）标，不由后端补：只有前端知道"下拉里这个值是偏好预填的、
    # 用户没动过"。后端开流前把偏好塞进 `diagram_type` 是不行的——那会让上面那条
    # "`None` 与 `tree` 必须分得开"的性质失效，事后排查会看到"用户选了流程图"
    # 而用户根本没选过。
    #
    # 它落进 `runs.request.prefs_applied`，是偏好面板上"上次生效"的**唯一**数据源。
    # 少了它，一条偏好写进去之后没有任何办法证明它影响过任何一次生成——那正是
    # "心里记着"与"真的在起作用"的区别。
    prefs_applied: list[str] = Field(default_factory=list, max_length=50)
    # 这张图**现在长什么样**：`expand` / `edit` 的输入，也是服务端唯一的形状来源。
    #
    # 三种图型共用这一份（`DiagramContent` 联合），与同一轮 `snapshot` / `done` 事件
    # 的类型一致。收窄成 `MindMapContent` 时的后果是可复现的：鱼骨图与流程图发
    # `expand` / `edit` 必然 422，而后端为它们写的 `_expand` / `_apply_ops_any`
    # （按骨 / 阶段分片执行）对前端**不可达**——那两条路有实现、有用例，却没有人
    # 能走到。
    #
    # 判别靠形状（`DiagramContent` 的 `union_mode="left_to_right"`，见
    # `mindmap.DiagramContent` 的注释），不靠 `diagram_type`：库里与前端手里有一批
    # "自述是鱼骨、形状是树"的旧记录，收窄会在它们下一次保存时 422。
    #
    # 三种形状以外的 payload 由 `snapshot_is_a_known_shape` 拦在 Pydantic 之前
    # 并给人话——联合类型的默认报错是一棵 `anyOf` 错误树，用户从里面读不出
    # "我该改什么"。
    snapshot: DiagramContent | None = None
    selected_ids: list[str] = Field(default_factory=list, max_length=50)
    sources: list[Source] = Field(default_factory=list, max_length=MAX_SOURCES)
    budget: RunBudget = Field(default_factory=RunBudget)
    # 「这一次」要不要走推理模型（界面上那枚「深度思考」按钮）。
    #
    # 它是**这一次运行的开关，不是设置**：同一个用户会在"随便出一张图"和"这段逻辑
    # 要拆准"之间来回切，写进偏好就把一次性的选择变成了永久状态。所以它不落库、
    # 不进 `prefs_applied`，只活在这一次请求里。
    #
    # 缺省 `False` 与加这个字段之前逐字节相同：老前端不带，发出去的 model 名照旧
    # 是 `MINDMAP_LLM_MODEL`。
    #
    # 它**不改任何生成链路**：三段式、两级骨架、每支检索全都不看这个字段。它只
    # 决定 `provider.OpenAICompatibleProvider` 往上游发的 `model` 是哪一个
    # （见 `provider._model_for`）——两个模型同一个 endpoint，差别只有名字。
    deep_thinking: bool = False

    @model_validator(mode="before")
    @classmethod
    def sources_not_too_many(cls, data: Any) -> Any:
        """份数上限：正文不进 prompt，卡住的是**清单**。

        清单列不下的材料，模型连"有这一份"都不知道——那比拒绝更糟，因为树看起来
        是完整的，只是少了两块。原来的另一条测（`full` 形态下"正文合计装不装得进
        上下文"）已随那个形态删除：正文从不进 prompt，那条预检量的是一个不存在的
        对象（一份 25 万字材料现在只贡献一行清单，几十 token）。

        字段上的 `max_length` 仍然留着（OpenAPI 要有 `maxItems`，前端据此先拦一步），
        这里在它之前给出能照着做的文案：pydantic 的 "List should have at most 50
        items" 读不出多了几份、也读不出为什么，用户唯一能做的动作是"删到 50 份"。

        放在 `mode="before"`：`mode="after"` 的校验器跑不到这一步（字段校验先失败）。
        """
        if not isinstance(data, dict):
            return data
        sources = data.get("sources") or []
        # 只认列表：传进来一个 60 字符的字符串时，"份数"是它自身的长度，按份数报错
        # 会给出一个用户看不懂的数字。类型错留给字段校验去报。
        if not isinstance(sources, (list, tuple)):
            return data
        count = len(sources)
        if count <= MAX_SOURCES:
            return data
        raise ValueError(
            f"本次附带 {count} 份材料，超过单次请求上限 {MAX_SOURCES} 份。"
            f"正文不占上下文，卡住的是材料清单：清单只列得下 "
            f"{MAX_SOURCES} 条，多出来的材料模型连存在都不知道。请分批生成。")

    # 这里**没有**"来源正文合计装得进上下文"的预检，但理由已经换过一次了，读的人
    # 要按新理由理解：
    #
    #   - 早先那条理由是"正文从不进 prompt，没有可测量的对象"；
    #   - 现在正文**会**进 prompt（`tools.FULL_TEXT_MATERIAL_CHARS` 那条闸：材料合计
    #     装得下时整份拼进结构/说明那三次调用），但那条闸本身就远低于物理上限
    #     （`CONTEXT_TOKEN_LIMIT` 是 200k token，1.5 字/token 折算到 30 万字；闸取
    #     注意力先失效的那个位置，比它小得多），所以"预检能拦下什么"仍然是空集。
    #
    # 留着它的后果不是"保守"，是错——一份 25 万字的材料只贡献一行清单（几十 token），
    # 却会在开流前被 422 拒掉，而它本来是能处理的：超过闸的材料一律走按支检索，正文
    # 一个字节都不进 prompt。清单自己的 token 上限在 `tools` 那边（超了截的是**清单
    # 项**，不是拒绝整个请求）。
    #
    # 另两处 token 预检（`provider.complete_tools` 与 `generation.chat_async`）量的
    # 是**实际要发出去的消息**，它们才是全塞那条路的物理兜底：正文真的超了，请求在
    # `chat_async` 的 `ensure_input_within_context` 那里被拦住并给出一句可操作的话，
    # 而不是打到模型服务上换一个 400。

    @model_validator(mode="before")
    @classmethod
    def snapshot_is_a_known_shape(cls, data: Any) -> Any:
        """`snapshot` 必须长得像三种图型之一，否则在 Pydantic 之前给一句人话。

        为什么要有这一条：`snapshot` 是 `DiagramContent`（三个成员的联合），而
        pydantic 对一个"哪个成员都不匹配"的 payload 会报一棵 `anyOf` 错误树——
        三层 `loc`、每层一句"field required"，用户从里面读不出"我该改什么"。
        这一条把最常见的那一类（**根本不是导图**，或者键名全错）提前拦成一句话。

        判据是**形状**而不是 `diagram_type`（与 `DiagramContent` 的判别同一条规矩）：
        树有 `root`，鱼骨有 `problem` + `bones`，流程有 `stages`。三个键都没出现时
        这份 payload 一定不是导图，不必让联合类型去试。

        不判得更细（比如"有 root 但 root 里没有 label"）：那些错**应该**由契约逐字段
        报出来，它们的报错文案本来就能读。这里只兜"完全认不出形状"这一档。
        """
        if not isinstance(data, dict):
            return data
        snapshot = data.get("snapshot")
        # 只判"原始 dict"这一档：已经是模型实例时（内部调用、用例直接把
        # `FlowContent` 传进来）它一定合法；是别的类型时该由字段校验报"类型不对"，
        # 这里替它报"形状不对"只会把用户引到错的方向。
        if not isinstance(snapshot, dict) or not snapshot:
            return data
        if {"root", "problem", "stages"} & set(snapshot):
            return data
        raise ValueError(
            "snapshot 看起来不是一份导图：三种图型分别需要 "
            "root（思维导图）/ problem + bones（鱼骨图）/ stages（流程图）"
            "其中之一。请确认发的是当前这张图的内容，而不是别的对象。")


# ---------------------------------------------------------------- 事件流


class _EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int = Field(ge=1)


class StageEvent(_EventBase):
    """一句"现在在干什么"，以及（可选）这件事的条数结果。

    `stage` 是**稳定标识**，`detail` 是给人看的那句话。前端只读 `detail`
    （`store.runGeneration` 的 `case 'stage'`），`stage` 供日志、用例与"按阶段
    换样式/抑制"用——所以它不必是人话，但**不能随手改**：`_stage_frame` 的用例
    与遥测都按它对齐。

    `count` 只在"这件事有个数量结果"的帧上出现：`external_search` 的结束帧带
    命中条数，开始帧没有这个键（`exclude_none=True` 把它整条抹掉）。`0` 是有效
    值，与"还没结束"必须分得开——"一条都没搜到"是用户要知道的正经结论。
    """

    type: Literal["stage"] = "stage"
    stage: str
    detail: str = ""
    count: int | None = Field(default=None, ge=0)


class ToolStartEvent(_EventBase):
    type: Literal["tool_start"] = "tool_start"
    call_id: str
    name: str
    # 只给摘要：完整参数里可能带整份来源正文，不该出现在事件流里
    args_summary: dict[str, Any] = Field(default_factory=dict)


class ToolEndEvent(_EventBase):
    type: Literal["tool_end"] = "tool_end"
    call_id: str
    name: str
    ok: bool
    result_summary: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0


class OpsEvent(_EventBase):
    type: Literal["ops"] = "ops"
    call_id: str
    batch: OpBatch


class SnapshotEvent(_EventBase):
    type: Literal["snapshot"] = "snapshot"
    # 三种形状的联合：鱼骨图与流程图现在是**真的**产出（见 `contracts.mindmap`
    # 的 `DiagramContent`），这一帧要是只收树，一次成功的鱼骨生成会在发事件时
    # 变成一个 500——而工具那边什么都没做错。
    content: DiagramContent


class NodeGrowEvent(_EventBase):
    """深层生成还在工具内部跑的时候，逐个节点推出去的预览。

    `path` 指向**父节点**：`[0, 2]` 是根的第 0 个孩子的第 2 个孩子，`[]` 是挂在
    根上，`None` 是根自己。根必须与"挂在根上的孩子"在类型上就分开——两者都用
    `[]` 时只能靠"根帧最先到"这个隐式顺序区分，顺序一变前端就把第一个分支当成
    中心主题，而且不会报任何错。

    因为展开响应里没有 id，父子关系也只能由工具层带上，不能像普通生成那样从
    JSON 里读出来。

    `expandable` 为 None 表示模型没写这个字段、或写的不是布尔值——与 `false`
    （模型明确说这个节点不能再拆）不是一回事，前端要能分开显示。它与 `path` 的
    None 各管各的，没有关系。
    """

    type: Literal["node_grow"] = "node_grow"
    # 必填但可以为 null：根帧就是 path=null。层数上限是契约的 8 层
    # （`tools.DEEP_MAX_DEPTH` 就是 `MAX_TREE_DEPTH`），索引链最长 7 位；留到
    # 16 是给将来放宽层数留余量，同时挡住畸形上报把帧撑大。
    path: list[int] | None = Field(max_length=16)
    label: str = Field(min_length=1, max_length=200)
    expandable: bool | None = None


class SourcesEvent(_EventBase):
    """材料入库后，临时 id 被换成持久 id 的通知。

    解析接口给每份材料发的是本次请求内有效的 uuid，而库里给的是另一个持久 id：
    入库时 `uuid4()` 生成，正文 sha256 只做去重判断、不充当 id（见
    `app/knowledge/library.py::ingest`）。两套 id 并存时导图里记的
    `source_refs.source_id` 在重新打开后就谁也找不到。所以入库之后要把"旧 id →
    持久 id"告诉前端，让它就地替换手上的来源条目。

    只在真有 id 变化时发：没有材料、或用不到库（开关关掉）时这一帧根本不出现，
    既有帧序列逐字节不变。

    **`sources` 与 `mapping` 回答的是同一个问题的两半，两个都要**：

      - `mapping` 是**换算**（旧 → 新），给"手上已经有正文"的调用方用：它按这张
        表把自己的材料改个 id 就对齐了。前端 `stores/sources.ts::remap()` 走这条。
      - `sources` 是**最终清单**（`SourceIdentity`，不带正文），给"只解析事件流、
        不打算自己算差集"的调用方用：`id` 逐个等于导图里 `source_refs.source_id`，
        `filename` / `chars` 足够认出是哪份材料。

    之前只有 `mapping`，等于把"最终清单是什么"留给调用方自己算：前端算了（`remap`），
    脚本与第三方没算——它们拿 ingestion 应答里的旧 id 去 `/optimization/check`，会得到
    一条不落的 `SOURCE_TEXT_UNAVAILABLE`。这不是"少一个便利字段"，是**接口对外只给了
    一半答案**。`sources` 就是把另一半补上，`mapping` 保留（删了会让前端白改一遍）。
    """

    type: Literal["sources"] = "sources"
    # 旧 id → 持久 id。空字典不该发这一帧，前端也不必处理空表。
    mapping: dict[str, str] = Field(default_factory=dict)
    # 入库之后的**全部**材料（含没换 id 的那些，含入库失败按原 id 继续的那些）。
    # 与 `RunContext.request.sources` 同一份、同一个顺序，只是不带正文。
    sources: list[SourceIdentity] = Field(default_factory=list)


class HeartbeatEvent(_EventBase):
    """等模型/等工具期间的"我还活着"帧。

    线上实测单轮静默能到 25 秒（3 路并发下模型思考）。这期间事件流里一个字节
    都没有，中间链路只要有 30–60 秒的空闲超时（企业代理、Cloudflare、部分移动
    网络）就会把连接掐断，用户看到"连接中断"，而模型其实还在跑、还在计费。
    nginx 那边是 900 秒，救不了更靠前的链路。

    心跳只做两件事：让连接上一直有字节流动；把"已经等了多少秒"送到页面。
    它不携带任何业务信息，前端也不该把它当进度——真实进度由 `stage` /
    `tool_start` / `node_grow` 负责。

    `waited_ms` 是**本次等待**已经持续了多久，不是总耗时：页面上"已等 N 秒"要说
    的是"卡了多久"，不是"跑了多久"。实现上它以"开始等这一步"为起点；工具段里
    每次收到工具事件都会重置（即使那个事件最终没产帧），所以这个值可能比"距上
    一帧"略小——它是"本轮等待时长"的近似，不是精确的帧间隔。
    """

    type: Literal["heartbeat"] = "heartbeat"
    waited_ms: int = Field(ge=0)


class DoneEvent(_EventBase):
    type: Literal["done"] = "done"
    content: DiagramContent | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    # 这棵树是怎么来的，前端落地时要照它标记来源，不能一律写成"模型生成"。
    # 缺省 None 而不是 "model"：没填的帧就是"没说"，把它默认成模型产出会让
    # 占位树继续冒充满分答案——缺省值只该表示未知。
    mode: Literal["demo", "model"] | None = None
    # 收尾这一轮模型说的话（都在 MODEL_TEXT_LIMIT 内截断），**只在没产出导图时填**。
    #
    # 线上出现过"模型只写字、一个工具都不调"：那时 content 是 None，前端只能显示
    # "模型这次没有产出导图"，而 journalctl 里未必捞得到日志（级别、输出去向都
    # 可能让它消失）。把原话挂在这一帧上，页面上就能直接看到它到底说了什么。
    # 有产出时不填：那属于正常结果，不需要把整篇输出塞进事件流。
    text: str = ""
    # 模型这一轮的思考过程（`reasoning_content`）。与 `text` 不同，它**成功时也填**：
    # 走推理模型（`deep_thinking=True` → `deepseek-reasoner`）时，思考过程是用户
    # 主动要看的东西——界面上那个折叠面板就是为它做的，成功时反而藏起来等于这个
    # 开关白勾了。
    #
    # 上限见 `service.REASONING_BYTE_LIMIT`（按**编码后的字节**算，64 KiB；实测的
    # 3000–8000 字连一半都用不到，等于"不截"）。
    reasoning: str = ""
    # 这一轮真正发出去的模型名（`deepseek-chat` / `deepseek-reasoner` / 配置里的任意值）。
    #
    # 由 `_loop` 从 provider 上读，不另存一份：模型名是 provider 在**发请求那一刻**
    # 决定的（配置缺失时还有回退），循环层再算一次就是两处真相。取不到（演示模式、
    # 测试里按脚本回放的假 provider）时是 None，帧里整条不存在——"没说"不该长得
    # 像"说了 chat"。
    model: str | None = None
    # 服务端在响应体的 `model` 里回报的模型名——**它实际跑的是哪个**。
    #
    # 与上面那个 `model` 是两件事，两个都要显示：请求名是我们发出去的意图
    # （`deepseek-chat`），服务名是对面的回答（实测是 `deepseek-flash`）。只报请求名，
    # 用户永远看不出请求被路由到了别处；这一位丢了整整一周才被发现，就是因为它
    # 从没出现在任何一帧上。
    #
    # 取不到（演示模式、按脚本回放的假 provider、老版本服务端）时是 None，帧里整条
    # 不存在，界面写"未知"——不拿请求名冒充答案。
    served_model: str | None = None
    # 这一帧交出去的那棵树**没有生成完**：预算耗尽、工具超时、连续失败、空转都算。
    #
    # 从前这几条收尾路径一律发 `error` 帧，哪怕树上已经长出了大半个能用的结构——
    # 前端收到 `error` 会清掉预览、`content` 也不动，于是用户看着它跑了三分钟，
    # 最后什么都没拿到。现在统一成"有树就交树，没树才报错"，`partial` 就是
    # "交了，但不是完整的一次生成"这个事实的落点。
    #
    # 缺省 None 而不是 False：`exclude_none=True` 会把它整条抹掉，正常收尾的帧
    # 与加这个字段之前**逐字节相同**——老客户端、`_replay` 重放都不受影响。
    partial: bool | None = None
    # 为什么是半棵树（"模型调用次数已用完（已执行 6 次工具调用，树上是 41 个节点）…"）。
    # 与 `partial` 成对出现；前端把它显示成告警文案，不要只给一个布尔值。
    partial_reason: str | None = None


class ErrorEvent(_EventBase):
    type: Literal["error"] = "error"
    code: str
    message: str
    # 预算耗尽时说明是哪一个，界面据此给可操作的提示
    limit: str | None = None


AgentEvent = Annotated[
    Union[
        StageEvent, ToolStartEvent, ToolEndEvent, OpsEvent, SnapshotEvent,
        NodeGrowEvent, SourcesEvent, HeartbeatEvent, DoneEvent, ErrorEvent,
    ],
    Field(discriminator="type"),
]
