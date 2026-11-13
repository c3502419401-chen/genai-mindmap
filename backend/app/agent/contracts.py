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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts.mindmap import MindMapContent
from app.contracts.ops import OpBatch
from app.contracts.source import Source
from app.generation.service import (
    SOURCE_MODE_RAG,
    GenerationError,
    ensure_input_within_context,
    source_mode,
)
from app.optimization.source_refs import EvidenceSpan

logger = logging.getLogger(__name__)

# 一次运行最多保留多少条命中区间。模型可能反复调 `rag_query`（改一条长材料时
# 实测能刷十几次），不设上限就是"内存随运行时长线性增长"。200 条覆盖 40 次
# `top_k=5` 的检索；超了丢**最旧**的，因为引用校验只需要"这条引用落在哪次命中
# 的哪一段"，而模型总是先检索、后生成——最旧的条目被丢时，对应的引用通常已经
# 写完了。丢的时候记 debug：这是容量策略生效，不是错误。
MAX_EVIDENCE_SPANS = 200


# ---------------------------------------------------------------- 预算


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
    # 这仍然是成本护栏而不是内容边界：每次调用都要付钱，没有上限时一个跑飞的
    # 循环能烧掉任意多额度。但它不该比模型的判断更早替用户做决定，所以取"够走完
    # 常见规模"的值，而不是"够防住算错"的最小值。
    #
    # 超过预算不再等于失败：工具内部会在额度不足时停止展开并记一条诊断，外层
    # 则以已经建好的树收尾（见 `service._budget_stop`）。用户拿到的是「树 + 少
    # 展开了几支」，不是「什么都没有 + 一条错误」。
    max_model_calls: int = Field(default=80, ge=1, le=120)
    max_tool_calls: int = Field(default=8, ge=1, le=50)
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

    def schema(self) -> dict[str, Any]:
        """转成 OpenAI tools 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }


@dataclass
class RunContext:
    """一次 run 的可变状态。

    跨轮存活，所以不含任何与模型对话相关的东西——那些在 `service.run()` 的
    局部变量里，生命周期更短也更清楚。
    """

    request: "AgentRunRequest"
    candidate: MindMapContent | None = None
    revision: int = 0
    batches: list[OpBatch] = field(default_factory=list)
    sources_by_id: dict[str, Source] = field(default_factory=dict)
    # call_id -> (参数指纹, 结果)。存指纹是为了识别"同一个 id 换了参数"这种协议违规，
    # 那必须报错而不是返回旧结果。
    executed: dict[str, tuple[str, "ToolResult"]] = field(default_factory=dict)
    # (工具名, 规范化参数) -> 结果。只对只读工具生效。
    dedupe: dict[str, "ToolResult"] = field(default_factory=dict)
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
    # 本次运行累积的命中区间（`tools._rag` 每检索一次并一批进来）。它是
    # `verify_source_refs(..., evidence=...)` 的唯一输入，也是"父块模式"下唯一能
    # 区分"引用落在命中的子块里"与"引用落在父块里与查询无关的一半"的东西。
    #
    # 为什么存在这里而不是 `ToolResult.data`：hits 只活在 `_rag` 的返回值里，而
    # 校验发生在 `mindmap_generate` 收尾（`validate_generated_content`），中间隔着
    # 整个循环。`ToolResult.data` 是**一次调用**的产物，循环从不汇总它；要汇总就得
    # 在 `service.run()` 里按工具名特判 `data["hits"]` 的形状，等于让循环认识检索
    # 工具的私有字段。`RunContext` 本来就是"跨轮存活的可变状态"，证据正好是这种
    # 东西，且 `_rag` 与 `_generate` 拿的是同一个 ctx，不需要任何新管道。
    evidence: list[EvidenceSpan] = field(default_factory=list)

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


# ---------------------------------------------------------------- 请求


# 单次请求能带的来源份数上限。**这也是清单的条数上限**：`tools.MATERIAL_MANIFEST_MAX_ITEMS`
# 引用的就是这个常量，两处一个数。分两次写（这里 50、渲染器 50）迟早分叉，而分叉
# 的表现是"请求收下了、清单里却少两份"，模型连这两份材料存在都不知道。
MAX_SOURCES = 50


class AgentRunRequest(BaseModel):
    """一次 agent 运行。

    `snapshot` 是前端当前的树。服务端**不**从数据库重读——用户可能有一堆没保存
    的编辑，重读数据库等于把它们丢掉。
    """

    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=1000)
    intent: Literal["create", "expand", "edit"] = "create"
    # 同一句主题换一种拆解方法，结构就不同（表在 `generation.service.THINKING_MODELS`）。
    # 用 Literal 而不是自由字符串：拼错的值会被 `model_config` 挡在开流之前，
    # 否则它会静默退化成 default，用户在下拉里选的东西等于没生效。
    thinking_model: Literal[
        "default", "first-principles", "feynman", "systematic", "critical"
    ] = "default"
    snapshot: MindMapContent | None = None
    selected_ids: list[str] = Field(default_factory=list, max_length=50)
    sources: list[Source] = Field(default_factory=list, max_length=MAX_SOURCES)
    budget: RunBudget = Field(default_factory=RunBudget)

    @model_validator(mode="before")
    @classmethod
    def sources_not_too_many(cls, data: Any) -> Any:
        """份数上限：两种形态的"为什么不行"完全不同，文案必须分开。

        - `rag`：正文不进 prompt，所以"材料字太多"根本不是问题；卡住的是**清单**。
          清单列不下的材料，模型连"有这一份"都不知道——那比拒绝更糟，因为树看起来
          是完整的，只是少了两块。
        - `full`：每份正文都要进 prompt，份数上限与上下文是同一件事的两个方向。

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
        if source_mode() == SOURCE_MODE_RAG:
            raise ValueError(
                f"本次附带 {count} 份材料，超过单次请求上限 {MAX_SOURCES} 份。"
                f"rag 形态下正文不占上下文，卡住的是材料清单：清单只列得下 "
                f"{MAX_SOURCES} 条，多出来的材料模型连存在都不知道。请分批生成。")
        raise ValueError(
            f"本次附带 {count} 份材料，超过单次请求上限 {MAX_SOURCES} 份。"
            "请分批生成。")

    @model_validator(mode="after")
    def sources_within_context(self) -> "AgentRunRequest":
        """`full` 形态下：来源正文合计要装得进模型上下文。

        原先这里卡的是「合计 20000 字」。那个数按一句话主题拍的，没有实测依据，
        而且与 pipeline 的同一口径互相抄写——两处一起错、一起被当成边界。真正
        的边界只有一条：模型的上下文窗口装不装得下。所以改判 token，超了仍然
        在开流之前拒绝，文案给出两条出路（精简 / 分批）。

        **`rag` 形态下这条预检直接跳过**：正文根本不进 prompt，没有可测量的对象。
        留着它的后果不是"保守"，是错——一份 25 万字的材料在 `rag` 形态下只贡献
        一行清单（几十 token），却会在开流前被 422 拒掉，而它本来是能处理的。
        清单自己的 token 上限由 `tools._material_manifest` 负责，超了截的是**清单项**，
        不是拒绝整个请求：删掉两行索引，比劝用户去精简一份他并不需要精简的材料好。

        份数上限是另一个量，见 `sources_not_too_many`（两种形态都要拦）。

        另两处同名的 token 预检（`provider.complete_tools` 与
        `generation.chat_async`）量的是**实际要发出去的消息**，不是材料体积：
        `rag` 形态下那些消息里没有正文，自然不会被它们拦。

        这里必须抛 `ValueError`：它发生在 FastAPI 解析请求体的时候，只有
        ValidationError 才会被收成 422 并把文案带回前端；`GenerationError`
        冒出去会变成 500，用户看到的是"服务器内部错误"。
        """
        if not self.sources or source_mode() == SOURCE_MODE_RAG:
            return self
        text = "\n".join(source.text or "" for source in self.sources)
        if not text:
            return self
        try:
            ensure_input_within_context(text, label="来源正文合计")
        except GenerationError as exc:
            raise ValueError(str(exc)) from exc
        return self


# ---------------------------------------------------------------- 事件流


class _EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int = Field(ge=1)


class StageEvent(_EventBase):
    type: Literal["stage"] = "stage"
    stage: str
    detail: str = ""


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
    content: MindMapContent


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


class DoneEvent(_EventBase):
    type: Literal["done"] = "done"
    content: MindMapContent | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    # 这棵树是怎么来的，前端接受候选时要照它标记来源，不能一律写成"模型生成"。
    # 缺省 None 而不是 "model"：没填的帧就是"没说"，把它默认成模型产出会让
    # 占位树继续冒充满分答案——缺省值只该表示未知。
    mode: Literal["demo", "model"] | None = None


class ErrorEvent(_EventBase):
    type: Literal["error"] = "error"
    code: str
    message: str
    # 预算耗尽时说明是哪一个，界面据此给可操作的提示
    limit: str | None = None


AgentEvent = Annotated[
    Union[
        StageEvent, ToolStartEvent, ToolEndEvent, OpsEvent, SnapshotEvent,
        NodeGrowEvent, DoneEvent, ErrorEvent,
    ],
    Field(discriminator="type"),
]
