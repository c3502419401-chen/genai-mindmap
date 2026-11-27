# Agent 化改造实施计划

> **这是一份历史设计稿（立项前后的规划），不是现状。** 正文原样保留，用于回答"当初为什么这么设计"。
> 两处已与代码分叉，读之前先知道：
> 1. **候选层已删除**（`acfdb5b`）：生成完成即落画布并标成未保存，没有「应用到画布」/「接受」/「丢弃」这些动作。
>    下文出现的 `acceptCandidate` / `discardCandidate` / "候选就绪" / "待接受" 都是那一版的接口，现在不存在。
>    当前落地点是 `frontend/src/stores/mindmap.ts::commitGenerated`。
> 2. 阅读当下实现请看 `docs/AGENT_STREAM_PROTOCOL.md`、`docs/API.md`、`docs/FLOW.md`。
> 若要做"把候选层请回来"这类改动，先读 `acfdb5b` 的提交说明——那里逐条写了删掉它换来了什么。

## 0. 先修三件小事（对应要求 11）

以下均为待实施设计；路径以本仓库为准。`B=backend/app`、`BT=backend/tests`、`F=frontend/src`、`FT=frontend/tests`，表内路径按此前缀展开。
源码校正：`B/generation/service.py::DETAIL_PROMPT` 的主题分支已经写了“每支 2–4 个要点”；缺口在材料分支和 ❌/✅ 示例。视觉检查实际位于 `B/ingestion/vision_pdf.py::_call_vision()`。非流式解析会补括号；`stream_partial.py::_stream_events()` 的最终解析已经严格，不能混为一谈。

| 独立切片 | 修改位置与接口 | 新增验证及验收指标 | 独立回滚 |
|---|---|---|---|
| 0a：拒绝截断成功 | `service.py` 新增 `ensure_finish_reason(reason: str \| None, *, allow_tools: bool = False) -> None`；`chat()`、`chat_async()` 在 `parse_model_json()` 前调用；`stream_partial.py::_stream_chat(system, user) -> AsyncIterator[str]` 记录终止原因并在结束前检查。普通生成只接受 `stop`；`length`、缺失原因、过滤/未知原因均抛 `GenerationError`；流还须收到供应商 `[DONE]`。 | `BT/test_generation.py`、`BT/test_stream.py` 覆盖同步、异步、真实传输层假 SSE 三条路径；对完整 JSON 与可补齐 JSON 分别注入 `length`，6/6 不得返回成功；缺终止原因、无结束标记也失败；`stop` 的合法树全部成功。旧接口仍为 HTTP 502 或 `error`→`[DONE]`，错误日志含原因。 | 单独回退该补丁构建；回退期间先设 `MINDMAP_LLM_MODE=demo`，避免恢复静默截断。 |
| 0b：提高分支下限 | `service.py::MIN_BRANCHES=3`；`validate_generated_content(result, *, sources=None, enforce_min_branches: bool = True)` 只在模型整图生成启用下限；`generate()` 的 demo 显式传 `False`，流式 demo 保持原占位结构。`MindMapContent.validate_tree()` 不加此质量门槛，旧图与人工删支仍合法。 | 同步与 SSE 各测 0/1/2/3 个一级分支，前 3 类失败、3 分支成功；demo 仍为根加 1 子节点。材料不足不能凑数，明确失败；固定短材料集记录误拒率，不能只统计成功结果。`test_single_branch_is_accepted`、`test_two_branches_pass` 改为拒绝断言。 | 单独回退下限与对应错误文案，或部署 0a 构建；保存契约和数据库无变化。 |
| 0c：补材料要点与引用示例 | 同文件 `DETAIL_PROMPT` 材料分支要求“每个可分解分支至少 2 个要点、目标 2–4 个，不足依据不补写”；`SOURCED_DETAIL_PROMPT` 补“原文：会议决定下周发布；❌ quote=计划近期上线；✅ quote=下周发布”。主题分支保留现有数量要求；新增 `diagnose_density(content: MindMapContent) -> list[Diagnostic]` 标记不足；旧接口只写质量日志，agent 才合入候选诊断，不把材料稀疏当作事实错误。 | `BT/test_generation.py` 断言两种输入均含数量约束，`BT/test_source_refs.py` 验证示例改写引文被移除、逐字引文保留。固定充分材料集的分支要点达标率目标 ≥90%；逐字匹配引用比例 ≥95%，引用覆盖率较旧提示词提高 ≥10 个百分点或已达 ≥90%；新增无依据事实为 0。 | 仅回退提示词及密度诊断，保留 0a/0b；原提示词版本随构建留存。 |

`GenerationError.__init__(message: str, *, code: str = "generation_failed", finish_reason: str | None = None)` 保留消息位置参数；旧路由保持既有错误体，v2 事件附带新字段。`parse_model_json()` 的语法修复不代表完整性证明，终止检查必须先执行。
先建立 `backend/eval/agent_cases.json` 与 `backend/eval/agent_quality.py::evaluate(cases, runner, repeats=3) -> QualityReport`：10 个主题、10 份材料、10 组混合来源，各重复 3 次，对照旧版及 0a/0b/0c 逐步累积版；材料组含 2 份本就不足 3 分支的反例，数量达标率只对人工标注“充分材料”计算。
`service.py`、`stream_partial.py` 记录 `request_id/finish_reason/prompt_tokens/completion_tokens/latency_ms/branch_count/points_per_branch/diagnostic_codes/prompt_version`；不记录原文和密钥。充分输入的统计分母包含所有失败请求，预设不足材料负例单列正确拒绝率。逐字匹配率=有效引用数/校验前模型声明引用数（零声明记 0）；覆盖率=有有效引文节点数/应有依据节点数，事实支持由人工标注抽查，不能用字符串匹配替代。
用既有现网匿名样本或同分布样本先观察 100 次有效请求：若 0 阶段已使结构成功率 ≥95%、人工关键点覆盖率 ≥90%，不默认启用多步生成；只有局部修改需求或剩余质量缺口可量化时，才推进对应能力。各阈值是待校准的放量门槛，不是已测结论。

## 1. 循环位置与框架取舍

选择后端 Python 自写受限 tool-calling 循环，放在已有 `B/agent/`；`B/generation/` 继续负责单次生成和校验，来源准备当时放在 `B/pipeline/`（该包已删除，现在由 `B/agent/tools.py` 的 `_pick_sources()` / `_source_prompt_input()` 承担），前端只展示、接受候选。当前 `agent.service.plan(goal)` 是桩，不能当作可用循环。
| 方案 | 取舍与代价 |
|---|---|
| `httpx.AsyncClient` + Pydantic + 自写循环（采用） | 复用 `_config()`、错误类型及取消语义，不新增运行时依赖；必须自己维护工具消息配对、预算、去重、终止与协议测试，集中在下列 4 个模块。 |
| 引入 Python agent 框架（暂缓） | 即便封装编排，仍需实现本项目的 op、引用核验和前端事务；新增依赖、状态适配、升级回归成本。出现持久恢复/复杂分支工作流后，再用相同契约与故障样本作替换实验。 |
| 搬用参考项目的 Node/Vercel AI SDK 后端（不选） | 增加第二套服务及 JSON/Pydantic 边界，当前单机部署收益不清楚；只借鉴四工具、可见调用和近期编辑上下文，参见 [MindGeniusAI README](research/readmes/xianjianlf2__MindGeniusAI.md)。 |

新增 `B/agent/contracts.py`（请求、工具、事件模型）、`provider.py::complete_tools(messages: list[dict], tools: list[dict], *, budget: RunBudget) -> Completion`、`tools.py::execute(call: ToolCall, ctx: RunContext) -> ToolResult`；在 `service.py` 新增 `async run(request: AgentRunRequest) -> AsyncIterator[AgentEvent]`，保留 `plan()`。
`Completion={message,tool_calls,finish_reason,usage}`；`complete_tools()` 调用 `ensure_finish_reason(..., allow_tools=True)`，只接受 stop/tool_calls 且与调用列表相符，length 时即使参数完整也不执行。工具调用轮必须保留原 assistant 消息，将每个结果按 `tool_call_id` 以 `role=tool` 回填，再传入更新后的候选树及诊断。供应商协议依据 [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)；实际配置的兼容端点仍须实测。
状态机：校验请求→构造上下文→模型选择工具→参数校验→串行执行→确定性校验→反馈→下一轮；`stop` 且无工具调用才结束。创建没有有效候选时发 error/no_candidate；展开/编辑没有有效变更时发 done/no_change 并返回原快照，前端不提供接受按钮，不把自然语言答复伪装成完成导图。
工具调度只对白名单名称分派；参数用严格 JSON 解析与 `extra="forbid"` 校验，禁止 `eval` 和补括号后执行。一次回复含多个工具时按返回顺序执行，每个都计数；模型不能调用保存、任意网络或文件工具，来源文本只作为数据。
`RunBudget={max_model_calls:8,max_tool_calls:8,max_seconds:120,max_output_tokens:32768,max_repairs:1}`；规划单次 ≤2048、整图 ≤16384、展开 ≤2048 token，子生成和修复均计入总调用/输出预算；每次 max_tokens 取单次上限与剩余额度较小值，调用前预留，返回后用 usage 结算，缺 usage 按预留计。单次网络总限时 ≤60 秒且不超过剩余总时限，响应 ≤2MB。
新增 `service.py::build_context(ctx) -> dict`：每轮带当前 id/父子关系/原 label、已选节点、近期编辑和最新诊断；长来源经检索取片段。`provider.py::check_context(messages, budget) -> None` 在发出前核对已配置的上下文上限，超限明确失败，不暗截节点或来源；字符到 token 估算先以实测校准。
全轮共享 1 次修复额度：结构/参数错误反馈字段路径；`length` 不原样重试，整图修复时将节点预算减半；网络、鉴权、超时不自动重试。`callId` 在整轮内幂等，同 id 异参数报协议错；再按 `(工具名,规范化参数,planningRevision)` 去重，planningRevision 是该模型回复看到的候选版本，同一回复中的重复调用返回缓存回执，不再产生 op；预算耗尽返回终态错误，候选不可接受。
代价可观测：最坏 8 次模型调用而非一次，用户等待和 token 增加；`asyncio` 取消必须贯穿 `run()`、`execute()`、httpx，不能把同步 `generate()` 扔线程后宣称可取消。首版不引入 MCTS、多角色或自动探索树。

## 2. 真相、会话与编辑同步

保留 `F/stores/mindmap.ts::content` 为未保存编辑的真相，SQLite `MapRecord` 为已保存快照；后端只持有本次请求的候选副本，结束/断开即释放，不持有跨请求的权威会话。下一轮始终上传当前快照，避免双主同步。
`F/api/types.ts` 新增 `AgentRunRequest={runId:string,intent:"create"|"expand"|"edit",goal:string,input:TextInput|SourcesInput|NoInput,snapshot:MindMapContent|null,selectedIds:string[],scope:"selection"|"map",baseClientRevision:number,recentEdits:RecentEdit[],historyTruncated:boolean}`；`TextInput={kind:"text",text:string}`、`SourcesInput={kind:"sources",sources:Source[]}`、`NoInput={kind:"none"}`。
`B/agent/router.py::async stream_run(body: AgentRunRequest, request: Request) -> StreamingResponse` 对应 `POST /api/agent/runs/stream`；`create` 必须带 goal、snapshot=null（sources 可空）；`expand/edit` 必须有 snapshot，选中节点必须存在，expand 恰选 1 个节点。scope=selection 只允许改所选子树（含新增后代），scope=map 才能改全图/标题；`execute()` 与规范化器均检查范围。goal 1–1000 字、sources ≤50 个，来源校验走 `AgentRunRequest.sources_within_context`（开流前按 token 估算）；合并长度不再按字数卡（后端已删掉 `MAX_INPUT_CHARS = 20000`，改由真实调用前的 token 估算决定）；非法请求在开始 SSE 前返回 422。
`RunContext={runId,baseClientRevision,candidate:MindMapContent|null,candidateRevision:0,scopeIds,sourcesById,messages,executedCalls,budget}`；`Candidate={runId,mapSession,baseClientRevision,revision,content,batches,diagnostics,state:"running"|"ready"|"stale"}`。前端 snapshot 原样发送整棵树（原计划要过 `toContractV1()` 把 `note` 裁掉，该函数已随契约 v2 落地删除）；服务端记录快照来源，不从 `mapId` 重读数据库来覆盖未保存编辑。
store 新增 `clientRevision`（每次已提交编辑/撤销/重做递增）、`candidate`、`activeRun`；沿用 `mapSession` 隔离切图。`commitLabel()`、`addChildTo()`、`addSiblingTo()`、`deleteNode()` 通过 `commitOps(ops: MindMapOp[], origin: "user"|"agent") -> boolean` 不可变更新。
`recentEdits` 为最多 50 条 `{fromRevision,toRevision,kind:"ops",ops}` 或 `{fromRevision,toRevision,kind:"snapshot",reason:"undo"|"redo"|"title"}`；超出标 `historyTruncated`，当前快照始终完整。它只解释差异，不在后端重放；请求开始后新增的编辑留给下一轮，避免重复执行。
`acceptCandidate(runId: string): "applied"|"stale"|"invalid"` 仅在 ready、同 mapSession、当前 revision=baseClientRevision、无节点/标题输入草稿时执行；有草稿先提交或取消，再重新检查 revision。`discardCandidate(runId: string): void` 仅删除候选；接受成功即清候选并记已接受 runId，重复点击不得二次提交。
展开/编辑接受时：在当前树副本顺序重放全部规范化 batch，和 done 快照按 v1 契约归一化比较，一致才 `pushHistory()` 一次并替换 content、清 future、置 dirty、revision+1、`ensureSelection()`；保留未变节点的本地 `note`。一次 agent 运行只是一条撤销记录。
新图仅在点击“采用为新图”时重置 mapId/version、mapSession、历史及选择；生成失败/取消不清旧图。新图沿用现有生成的“新作品”语义，不能撤销成旧图却继续使用新身份；展开/编辑则保留原 mapId/version。
`undo()/redo()` 继续恢复整树 JSON（含 refs/source_usage/note），50 步上限不变；恢复后递增 clientRevision，未接受候选变 stale。`renameTitle()` 同样递增，标题独立于根 label；不把整树快照塞入节点 op。
运行期间可编辑当前图，候选预览只读；`applyContent()` 的编辑禁令仅留给旧流式直接写 content 的路径。任何手动修改令候选 stale，提供“基于当前图重新计算”；首版不自动重基，也不再用引用相等或 confirmReplace 直接覆盖修改。
`newMap()/loadMap()/preview()` 中统一取消两种生成，递增 mapSession；所有事件回调先核对 runId、mapSession、AbortSignal，不能只在 finally 检查。取消只丢候选，不恢复启动时快照从而抹掉期间手工改动。
`save()/saveAsNew()` 只保存 content；`revisionAtStart` 与 mapSession 校验保存响应，在途编辑/接受候选仍保留 dirty。保存成功更新持久化 version，不递增 clientRevision；两个版本号职责分离。

## 3. op、ID 与整树保存

新增 `B/contracts/ops.py` 与 `F/mindmap/ops.ts`，下列为唯一执行格式；字段用既有前端的 `parentId` 命名，模型草案必须先规范化。
| op | JSON 结构示例 | 约束 |
|---|---|---|
| add | `{"op":"add","id":"n_<32位hex>","parentId":"root","index":0,"label":"实施步骤","source_refs":[]}` | id 为新增节点；parentId 已存在；index 为当时子列表的 0..length 整数；只加叶节点，嵌套通过父在前的多个 add 表达。 |
| update | `{"op":"update","id":"n1","changes":{"label":"新标题","source_refs":[]}}` | changes 至少一项，仅 label/source_refs；遗漏字段保留，refs=[] 显式清空；禁止改 id/children/父节点。 |
| remove | `{"op":"remove","id":"n1"}` | 删除节点及完整子树；缺失 id 或删除根节点报错，不静默成功。 |

`OpBatch={batchId:string,baseRevision:number,revision:number,ops:MindMapOp[],title?:string,sourceUsage?:SourceUsage[]}`；title/sourceUsage 是整图元数据，同批原子应用，前者与根 label 分开，后者只能由真实来源及原快照摘要确定，不能相信模型声明。每批 0–100 个 op，但 op/title/sourceUsage 至少一项有效变更；空变化回 no_change、版本不增长；整次运行 ≤500 个 op。
`B/agent/ops.py::apply_ops(content: MindMapContent, batch: OpBatch) -> MindMapContent` 与 `F/mindmap/ops.ts::applyOps(content: MindMapContent, batch: OpBatch): MindMapContent`：在副本上按序执行，检查所有目标，再校验最终整树；任何失败整批不提交，输入对象不得变化。
`execute()` 与前端候选 reducer 负责核对 baseRevision；纯 apply_ops/applyOps 不持会话状态。每个成功 batch 的 revision=baseRevision+1；1000 节点/8 层/50 子节点、label/title 长度、ID 唯一和来源关联由 `MindMapContent.validate_tree()` 兜底；人工编辑与局部 op 不执行整图生成的 MIN_BRANCHES 门槛。
后端 `B/agent/ops.py::new_node_id(existing: set[str]) -> str` 用 `n_`+uuid4.hex 并查重；新图所有模型 id 一次映射后再发 snapshot。`F/mindmap/edits.ts::newId(existingIds?: ReadonlySet<string>): string` 改成完整 UUID 随机量，HTTP 下用 getRandomValues 回退，不截成 8 位；无随机源明确报错。
旧 id（数字字符串、root、n_8位等）原样保留，只要求现契约的 1–100 字符及全树唯一，不能通过 label 推断身份。模型 add 草案只分配 `$tmp1` 这类临时 id，parentId 可指此前临时 id；`normalize_ops(drafts: list[DraftOp], ctx: RunContext) -> tuple[list[MindMapOp], dict[str,str]]` 映射为服务端新 id 并返回 idMap。
新增/修改引用复用 `B/contracts/source.py::SourceRef`：source_id 必填，quote ≤300 字，page≥1，start/end 必须成对且为原文 Unicode 码点的 [start,end)。前端定位用 `Array.from(text)`，不能直接用 UTF-16 下标。
`B/agent/ops.py::validate_patch_refs(before: MindMapContent, after: MindMapContent, sources: list[Source], changed_ids: set[str]) -> list[Diagnostic]` 只处理受影响节点：新引用必须有本次真实原文；无效引用剔除并形成规范化 update。旧节点原引用无原文时保留、标未核验；不能对整张旧图调用 `validate_generated_content(..., sources=None)` 而清空所有引用。
扩展 `B/optimization/source_refs.py::verify_source_refs(content, sources, *, remove_invalid=False, node_ids: set[str] | None=None)`，默认行为不变；label 变化使旧语义评分失效，重新诊断。`annotate_nli(content, sources, *, node_ids: set[str] | None=None, max_checks: int=MAX_NLI_CHECKS) -> list[Diagnostic]` 保留旧默认行为；agent 仅传机械核验通过的受影响节点与剩余额度，整轮最多 100 条，仍只给提示、不自动删节点。所有引用修正必须先计入 batch，再发事件。
`POST /api/mindmaps` 仍为 `{content}`→MapRecord，`PUT /api/mindmaps/{id}` 仍为 `{version,content}`→MapRecord；没有 PUT 集合接口，也不新增自动持久化 op 接口。`B/mindmaps/repository.py::update_map(map_id, version, content) -> str` 的乐观锁继续防止跨标签覆盖。
`F/mindmap/edits.ts::validateDraft()` 补齐 id 长度、refs≤20、source_usage≤50/唯一及关联检查；`ops.py/ops.ts` 的 `OpError(code, path, message)` 给出同一失败位置。前端新增 `SourceRef={source_id:string,page?:number|null,start?:number|null,end?:number|null,quote?:string|null}`，整数/码点规则与后端一致。
前置修复 `F/api/types.ts::MindNode.source_refs?: SourceRef[]`：早期 `client.ts::toContractV1()` 会在保存时把引用整批裁掉，该函数已随契约 v2 删除，现在前端直接发送整树、引用不经过任何裁剪；历史、人工编辑及导出同样保留引用。`MapRecord.version` 只在保存成功后 +1，`candidateRevision` 和 batchId 永不作为数据库版本。

## 4. 四个工具及复用边界

首版仍取四工具，区分整图、局部展开、读来源、确定性改树；校验器始终由服务端强制执行，不交给模型选择调用。`B/agent/tools.py` 四个同名函数统一签名 `async <tool>(args: <Args>, ctx: RunContext) -> ToolResult`；JSON 中空数组表示对应已定义类型的列表。
| 工具 | 入参 JSON | 成功出参 JSON | 内部调用与权限 |
|---|---|---|---|
| mindmap_generate | `{"topic":"学习计划","source_ids":[]}` | `{"ok":true,"root_id":"n_...","candidateRevision":1,"node_count":25,"diagnostics":[]}` | 仅 create 且候选为空时可用；无来源用 DETAIL_PROMPT，有来源用 SOURCED_DETAIL_PROMPT；复用 `service.chat_async()`、`validate_generated_content()` 以及 `optimization` 的 `check()` / `annotate_nli()`，落在 `B/agent/tools.py::_generate()`（计划里的 `generate_async(...) -> GenerateResponse` 没有落地，那个响应类已随旧通道删除）。结果放 ctx 并发 snapshot；不落库。 |
| node_expand | `{"node_id":"n1","instruction":"补充实施要点","count":3,"source_ids":["s1"]}` | `{"ok":true,"batchId":"...","candidateRevision":2,"ops":[],"idMap":{},"diagnostics":[]}` | count 2–4，节点必须存在且有剩余容量；`chat_async(EXPAND_PROMPT, 当前节点及路径/已有孩子/证据)` 只返回新增孩子草案，最多 4 个、不重写原 label；按第 3 节生成 add 并调用 apply_ops、引用校验与 `optimization.service.check()`。 |
| rag_query | `{"question":"发布时间依据","source_ids":["s1"],"top_k":5}` | `{"ok":true,"hits":[{"source_id":"s1","start":0,"end":9,"quote":"会议决定下周发布。","score":1.0}],"method":"lexical"}` | top_k 1–5，仅查本轮附件；新增 `B/knowledge/service.py::search_sources(sources: list[Source], query: str, top_k: int = 5) -> list[EvidenceHit]`，用原始文本定长/段落分块及中文词片重合排序，每片 ≤300 码点；返回原始偏移，空命中如实返回。现有 search() 是桩，不能当 RAG 复用；来源取自 `_pick_sources()`，只查本轮附件，无 embedding 调用。 |
| mindmap_edit | `{"ops":[{"op":"update","id":"n1","changes":{"label":"发布时间"}}],"title":null}` | `{"ok":true,"batchId":"...","candidateRevision":3,"ops":[],"idMap":{},"diagnostics":[]}` | 输入为 DraftOp[]（add id 使用临时名），title=null 表示不改；只调用 normalize_ops、apply_ops、validate_patch_refs、check，不再调用模型；根不能删，越界/未知目标整批退回错误。 |

统一失败结构 `{"ok":false,"code":"invalid_tool_args","message":"...","errors":[{"path":"ops.0.id","reason":"节点不存在"}],"retryable":true}`；执行失败 candidateRevision 不变。已验证结构/引用诊断随工具结果回到模型，成功结果同时提供实际新 id，后续工具才能精确定位。
`source_ids` 只能缩小到本次请求已有来源；有来源的新图必须使用全部已接受来源，禁止以空列表绕开忠实整理模式；编辑无原文时可改结构但不能新造引用。来源摘要沿用真实输入重建逻辑；来源正文不写入 MindMapContent。
新增 `service.chat_async(..., max_tokens=None, observer: Callable[[CompletionMeta],None] | None=None) -> dict`，保留旧参数默认值；observer 将 token/finish/耗时交给共用预算。tool_result 给模型只回上述摘要、规范化 op 和诊断，完整树通过下一轮上下文及 SSE 提供。
`rag_query` 的 score 只表示检索排序；`verify_source_refs()` 的匹配只证明引文存在；二者都不证明结论正确。首版不接持久知识库、跨请求来源存储或外部联网检索。

## 5. SSE 与前端通道

旧 `B/generation/router.py` 的两条同步/流式生成路由与 `generate_stream_partial(text)` 已删除，本节的 v2 帧是现在**唯一**的流式协议。`B/agent/stream.py::encode_event(event: AgentEvent) -> str` 输出 `data: {JSON}\n\n`，不照搬附录中的 `event:` 格式。
每个 v2 JSON 帧的信封只有 `{seq, type}`（`backend/app/agent/contracts.py::_EventBase`）：`seq` 由服务端从 1 连续递增补齐，没有 `v` / `runId` / revision 字段，轮次与候选版本在各事件自己的 payload 里。`F/api/client.ts` 用独立的 `AgentEvent` 联合，不把 v2 偷塞进旧 StreamEvent。
| 帧 | 除公共字段外的结构 | 前端动作 |
|---|---|---|
| stage | `{stage,detail}` | 展示阶段；计时不阻塞执行。 |
| tool_start | `{callId,name,argsSummary}` | 显示调用卡片；不暴露整份来源文本、密钥或内部推理。 |
| ops | `{callId,batch:OpBatch}` | 仅在候选副本原子应用；batchId 去重；绝不能直接写 content。 |
| tool_end | `{callId,name,ok,resultSummary,latencyMs}`（诊断在 `resultSummary` 里，没有独立字段） | 更新对应卡片；可修复错误留在工具反馈中，候选 revision 不增长。 |
| snapshot（新增） | `{content:MindMapContent}` | create 第一次完整生成且校验成功后初始化候选；编辑模式禁止用它替换现有树。 |
| done | `{content?,usage,mode}`（`B/agent/contracts.py::DoneEvent`，按 `exclude_none=True` 编码，正常帧必带 `mode`） | 用 `content` 校对候选 revision 后进入待接受，`usage` 并入本次运行用量；不是自动保存/应用。 |
| error | `{code,message,limit?}`（`limit` 只在预算耗尽时给出是哪一个） | 终止并使候选不可接受，保留当前图与来源。 |

成功序列为 stage→tool_start→snapshot 或 ops→tool_end→下一轮→done→`[DONE]`；失败为工具反馈后继续一次，或直接 error→`[DONE]`。连接正常时恰有一个终态；取消/断网不承诺终态帧，且不能补发假 done。
新增 `client.ts::readSse<T>(response: Response, onEvent: (event:T)=>void, signal?: AbortSignal): Promise<void>` 与 `runAgentStream(body: AgentRunRequest, onEvent: (event:AgentEvent)=>void, signal?: AbortSignal): Promise<void>`；支持 UTF-8 分片、CRLF/多 data 行，JSON 解析与业务回调分开捕错。
v2 重复同 seq 同内容忽略、同 seq 异内容/跳号/损坏 JSON/未知变更帧立即中止；reader 在 finally 释放。`[DONE]` 或 EOF 本身不算成功，必须收到合法 done；不做自动重连或重发 POST，首版无事件重放和重启恢复能力。
`F/stores/mindmap.ts::runGeneration(goal: string, intent: 'create'|'expand'|'edit', sources?: Source[], thinkingModel?: string): Promise<void>` 统一走 agent、共用 activeRun/controller/candidate/reducer；原先的 `generate(text)` 与 `generateSources(sources, options?)` 两条前端通道已删除，没有 `agentEnabled` 开关可回退。空文本不转成"带来源材料"，避免丢掉主题展开语义。
`cancel()` 是唯一的取消出口（换图也走它），同时复位 `busy` 与 `agentRunning`，不再维护两套异步所有权。`F/components/SourceInputs.vue::generate()/cancel()` 接新调度；`F/App.vue` 只挂 `<AgentPanel />`，候选预览、接受/丢弃与工具卡片都在 `F/components/AgentPanel.vue` 里（`acceptCandidate()` / `discardCandidate()`）。
`F/components/AgentPanel.vue` 定义 `props:{candidate:Candidate|null,trace:ToolTrace[]}`、`emits:{accept:[runId:string],discard:[runId:string]}`；`F/api/types.ts` 同步声明 Diagnostic（level/code/node_id/message/evidence）及 `Source.sentences?: AudioSentence[]`，避免以 unknown 丢弃诊断与音频来源字段。
阶段 3 当初用 `VITE_MINDMAP_AGENT` 在两条旧 HTTP 通道与 `/api/agent/runs/stream` 之间切换；**该开关与那两条旧通道都已删除**，前端只剩 agent 这一条，没有"回旧入口"的分支。后端 `MINDMAP_AGENT_ENABLED=0` 仍在：开流前返回 503/agent_disabled（`B/agent/router.py::stream_run`）。
代理候选首版按已验证整图/完整 op 批展示，不使用 parse_partial() 的半截节点当执行依据；代价是首节点出现可能更晚，stage/tool_start 提供即时反馈。旧 `skeleton` / `node` 帧已随旧生成通道一起删除。

## 6. 兼容、测试逐文件迁移与数据库

<!-- claims-ok: knowledge_search_implemented 计划文档，记的是当时的落地状态 -->
旧 `POST /api/generations`、`POST /api/generations/stream`、`POST /api/pipeline/generate` 已删除（连同 `generation/router.py`、`pipeline/` 整包）；`POST /api/agent/tasks`、`POST /api/knowledge/search` 保留且继续 implemented=false。生成与编辑现在只有 `/api/agent/runs/stream` 一条出口。
数据库 schema 不变：`B/mindmaps/repository.py::connect()` 仍只有 mindmaps(id,version,content,updated_at)。op、运行日志、候选状态不落业务库；旧图默认空引用即可读取。刷新会丢未接受候选，已保存内容不丢；持久任务/协作须另立迁移，不暗中加 session 表。
测试基线：下面第一组是**计划期**的静态枚举值（后端 196 用例/12 文件，含参数展开；前端 168/13），写在计划当时，只作对照，不代表现在的规模；**当前实测**为后端 `pytest` 488 passed + 19 subtests（20 个测试文件）、前端 `vitest` 358 用例 / 23 文件。以下为预期改动，本次只写计划，未运行会产生缓存或临时数据库的测试。

| 后端现有文件（BT/） | 会破的断言与逐项处理 |
|---|---|
| test_generation.py（17） | `body_of()` 和坏 JSON 的 mock 补 finish_reason=stop，否则会提前因终止原因失败；`test_model_response`、伪造引用、默认 token 上限的成功树改成 3 分支；`test_root_only_tree_is_rejected` 更新数量错误文案。`test_explicit_max_tokens_still_wins` 只测 chat 不必扩树；`test_missing_closing_braces_are_filled` 保留为解析器单测，删除“截断仍可用”语义，另测 length 不会进入解析器。补 chat_async 与 0a/0c 用例。 |
| test_stream.py（14） | 单分支接受、两分支通过两例必破，分别改为拒绝并新增 3 分支通过；伪造引用成功树补 3 分支。现有 `_run()` mock 绕过真实 `_stream_chat`，必须另加 httpx 流 mock 检查 finish/EOF；demo 帧顺序与 PartialParserTest 的 5 例保持。 |
| test_source_refs.py（46） | 生成相关测试的两分支 tree 新建专用 3 分支 fixture（新增分支配有效引文），包括提示词/清引用/非法引用形状，保持原诊断集合；不要改影响精确 r/a/b 断言的公共 fixture。demo 两条 UNSOURCED_NODE 不变；保存、缺原文、Unicode、无迁移断言保留，补局部 op 不清旧引用。 |
| test_api.py（2） | demo 不执行数量质量门槛，生成→保存→读取→PUT→409 两例原样；补接受候选前库记录不变、接受后仍需显式保存。 |
| test_skeleton.py（17） | `test_agent_returns_plan_without_map`、`test_knowledge_reports_not_implemented` 原样保留，不能为新功能改成 true；pipeline 四阶段、demo 及来源摘要断言不变，新路由在独立文件测。 |
| test_errors.py（4） | 原有 mock `generation.router.generate`、统一 502/detail/request_id 保留；补 agent 开流前 422/503 与开流后 error 区别，不把工具可修复错误当 HTTP 重试。 |
| test_dependencies.py（3） | 原断言不变；新代码只用已有依赖与标准库，若引入新 import 此测试应明确报缺依赖。 |
| test_ingestion.py（32） | 不改解析器/上传协议；保留 truncated 页失败与完整页接受两例，用其相同 finish_reason 故障形态补生成回归。 |
| test_vision.py（14） | `_call_vision()` 不改，`test_truncated_output_is_failure` 原样；确保没有因抽工具层误改视觉调用。 |
| test_asr.py（29） | 不改采样、分片/声道、时间戳与错误映射；新来源请求保留 sentences，复用现有数据作一次端到端输入。 |
| test_nli.py（9） | 默认关闭、矛盾只警告和 `test_cite_is_never_modified` 原样；新 agent 用例另测整轮检查上限，不能改变 NLI 的只读契约。 |
| test_export.py（9） | 文本/层级/XML 断言原样；op 后仍产出标准 MindMapContent，新增结果样本而不改导出格式。 |

| 前端现有文件（FT/） | 会破的断言与逐项处理 |
|---|---|
| store.spec.ts | 旧开关关闭的用例保留。新路径将破坏“半图写入 content”“生成时禁止编辑”“done 直接落图”断言：分别改测 candidate、手工编辑令候选 stale、accept 后落图；取消/无 done/失败断言升级为 content/身份/历史全部保留，每个回调加迟到帧测试。 |
| upload.spec.ts | 旧 JSON pipeline mock 只给旧开关用；新路径改用 v2 SSE fixture。等待期间节点/标题草稿、切图的 4 类保护仍保留；`confirmReplace=true` 例新路径改成 stale 不可强盖、重新计算后显式接受；“没有改动正常落图”增加接受动作；“合并前确认”例新路径改为采用新图时确认。multipart、限长、解析计时、失败页与录音的断言不改。 |
| sources.spec.ts | readySources、校验及解析队列不改；“取消后重试”和“两条链路互斥”在新开关下改 mock `/agent/runs/stream`，断言唯一 controller、旧 runId 不能写新状态；20000 码点边界和 50 来源限制原样。 |
| save.spec.ts | 保留 POST/PUT/409、note 裁剪、保存期间编辑/切图断言；来源摘要例扩展为根/子/深层 refs 往返不丢。“保存期间重新生成”的新路径在 accept 为新图后才清身份，旧保存响应不得串图；编辑候选接受后 mapId 不变、dirty 为真。 |
| store-edit.spec.ts | 原 50 步快照、Tab/失焦、删除子树、标题规则原样；加一次 agent 批只推 1 步、undo/redo 引用与顺序完全恢复、撤销也使旧候选失效。 |
| edits.spec.ts | id 测试仍保留 n_ 前缀/唯一断言，增加完整随机量及 HTTP 回退；增删改的不可变性/顺序/容量断言不改，内部转为共用 applyOps。 |
| node-edit.spec.ts | Enter/Esc/Tab/blur 8 项行为不改；补候选就绪时尚在输入的内容不被接受动作吞掉。 |
| app.spec.ts | 旧生成完成提示保留旧开关用例；新开关下 done 只显示“候选就绪”，接受后才更新节点数/dirty；Ctrl+S 继续先 blur 再保存当前图，新增工具卡片的 start/end/error 对应。 |
| export.spec.ts | 8 项下载与错误断言不改；补生成中导出当前图而非 candidate，引用字段不在客户端被裁掉。 |
| audio.spec.ts | 9 项原样；录音状态和 WAV 编码不进 agent 调度。 |
| samples.spec.ts | 全部既有示例仍需 ID 唯一、布局覆盖、不重叠；预览示例应中断 agent 并清候选，不改 SAMPLE_MAPS。 |
| smoke-canvas.spec.ts | Vue Flow 容器挂载断言原样；候选面板不替换画布引擎。 |
| tidy.spec.ts | 8 项方向/边界/重叠断言原样；新增 op 前后布局样本放 ops 测试，不重写布局算法。 |

新增测试文件（实际落地的）：`BT/test_agent_ops.py`、`test_agent_tools.py`、`test_agent_stream.py`、`test_agent_loop.py`、`test_agent_node_grow.py`、`test_agent_demo.py`、`FT/ops.spec.ts`、`ops-contract.spec.ts`、`agent.spec.ts`、`agent-ops.spec.ts`、`agent-preview.spec.ts`；同一组 JSON 用例放 `tests/fixtures/mindmap_ops.json`，两端读取并比较完整归一化树及错误位置（`test_ops_contract_sync.py` + `ops-contract.spec.ts`），不能各写一套只验证自身实现的快照。

## 7. 可独立上线与回滚的切片

阶段按顺序累积；每个版本只依赖前一已验收版本，可单独部署，并可关闭本阶段入口回到前一行为。0a/0b/0c 各自发布；后续表中的验收是在实现时执行，不代表本次已运行。
| 阶段 | 改/增文件与接口 | 新增测试 | 可观测验收 | 回滚方式 |
|---|---|---|---|---|
| 0a–0c：质量基线 | 第 0 节 service.py/stream_partial.py、backend/eval 两文件；`ensure_finish_reason()`、`diagnose_density()` | 第 0 节列明的三条传输链、数量边界和引用对照 | 截断静默成功=0；0/1/2/3 分支两入口一致；报告每个样本成功/失败、覆盖、引用、P50/P95 和 token，不删除失败样本 | 各小补丁单独回退，策略见第 0 节；无数据迁移 |
| 1：引用保真与共用 op | `F/api/types.ts`、`client.ts`、`F/mindmap/edits.ts`、新增 ops.ts、扩展 `F/stores/mindmap.ts::commitOps()`；`B/contracts/ops.py`、`B/agent/ops.py`、`source_refs.py`，手工编辑经 commitOps | test_agent_ops.py、ops.spec.ts、共同 fixture；扩展 save/store-edit | ≥40 组正常/非法/边界 batch 两端结果一致；任一第 k 个 op 失败时原树零变化；50 步撤销/重做后树、引用、子项顺序相同；真实前端 POST→GET→PUT 引用数量不减 | 部署仅保留引用序列化修复、恢复旧编辑函数的前一构建；该修复保留为回滚基线，已保存内容无需转换 |
| 2：后端可调用 agent | 新增 `B/agent/contracts.py/provider.py/tools.py/stream.py`，扩展 agent/service.py/router.py、generation/service.py、knowledge/service.py、source_refs.py；开放 run SSE，旧 UI 不变 | test_agent_tools.py/stream.py/loop.py，旧 errors/skeleton 回归 | 模型模拟“检索→生成→改支”须有匹配 callId 与反馈后的第二轮；未知工具/坏参数零执行；最多 8 次模型与 8 次工具，120 秒截止；20 次中断中 httpx 连接均在 1 秒内关闭；运行前后业务库内容相同 | `MINDMAP_AGENT_ENABLED=0`，新路由返回明确未执行；旧入口现已删除（见阶段 3 的回滚说明），无新增表需清理 |
| 3：前端统一运行与候选接受 | `F/api/client.ts/types.ts`、`stores/mindmap.ts`、`App.vue`、`components/SourceInputs.vue`，新增 `components/AgentPanel.vue`；runGeneration/acceptCandidate/cancelRun | agent.spec.ts、agent-ops.spec.ts、agent-preview.spec.ts，按第 6 节迁移 store/upload/sources/save/app | 各执行 20 次手改/未提交草稿/undo/切图/保存并发/取消重试，静默覆盖=0；一轮接受仅 1 步历史；重复 batch 不加节点；拆分 SSE 每字节仍得同树；候选未接受时保存/导出内容不变 | 这条回滚已失效：`VITE_MINDMAP_AGENT` 开关与两条旧前端通道都已删除，候选只能靠界面上的"丢弃"作废；后端功能仍可用 `MINDMAP_AGENT_ENABLED=0` 关掉 |
| 4：实测后放量 | 扩展 `backend/eval/agent_quality.py::compare()`、cases.json，新增 `backend/eval/agent_acceptance_cases.json`；更新 `docs/API.md`、`ARCHITECTURE.md`、`ARCHITECTURE_ADDENDUM.md` 的过期说明与开关 | 30 条局部操作人工标注任务、每条 3 次；两种部署开关组合的端到端用例 | 90 次局部请求精确完成率 ≥90%、未指向节点/子树误改=0、有效引用保留率=100%；受控机器端到端 P95≤60 秒、相同任务 token 中位数≤整图重生的 2 倍；先 10% 试用，100 次后仍达标再扩大 | 未达任一质量/成本门槛即 `MINDMAP_AGENT_ENABLED=0` 停用（前端已无开关可关）；保留质量修复和 op，不回退已保存数据 |

阶段 2 真实供应商验收先用 20 次最小工具回填及取消探针；demo 必须全程 0 次网络调用、显式标 mode=demo。阶段 3 协议与竞态主要用可控时钟/延迟假流，不用真实模型随机性掩盖状态错误。
实施后的常规检查入口为后端 `python -m pytest -q`、前端 `npm test` 与 `npm run build`；每阶段记录新增失败归属与上述业务计数。数据库回滚演练必须用副本，两标签同 version 保存应恰有一次成功、一次 409。

## 8. 预计提交序列

每条是独立可审阅的中文提交主题，不在提交信息中出现工具产品名或模型名；括号内沿用现有模块范围。
1. `fix(generation): 检查同步与流式终止原因，拒绝把截断结果当成成功`
2. `fix(generation): 将模型整图分支下限提高到三支，保留演示与旧图契约`
3. `fix(generation): 补齐材料要点数量与逐字引文的正反示例`
4. `test(generation): 增加质量对照样本并记录失败率、覆盖率和调用用量`
5. `fix(refs): 保存与重新打开导图时保留节点引用`
6. `feat(mindmap): 定义增删改操作与原子批次的两端契约`
7. `refactor(editor): 手工编辑复用操作执行器并保留整树撤销历史`
8. `feat(generation): 为异步生成补齐调用元数据与预算计量`
9. `feat(knowledge): 在本次来源中检索片段并返回原文位置`
10. `feat(agent): 接入整图生成、节点展开和精确改树工具`
11. `feat(agent): 增加有调用预算和错误反馈的后端工具循环`
12. `feat(agent): 输出工具进度与候选操作的版本化事件流`
13. `refactor(api): 共用流读取器并校验候选事件的顺序与终态`
14. `feat(editor): 以候选副本承接生成结果并支持整轮接受和撤销`
15. `refactor(sources): 合并两条生成调度并统一取消与切图保护`
16. `feat(editor): 展示工具进度、引用诊断与候选差异`
17. `test(agent): 覆盖重复事件、并发保存和取消后的迟到响应`
18. `docs(agent): 按实测质量与成本设置灰度门槛并补齐回滚说明`

## 9. 最可能出错的三个地方

| 风险 | 具体故障注入与判据 |
|---|---|
| 候选、历史与保存版本串线 | `FT/agent.spec.ts::test_cancel_save_switch_race()` 延迟 run A 与 save A，执行改名→undo→切图 B→接受/取消→启动 run B，再倒序释放所有帧和响应；断言 B 的 mapId/version/content/past/future/dirty 不受 A 影响。同一图手改后接受旧 candidate 必须 stale，save 返回后仍 dirty。 |
| 预览/修复被误当成功、重复工具造成双写 | `BT/test_agent_provider.py::test_finish_and_call_dedupe()`、`test_agent_stream.py::test_missing_terminal()` 注入“完整 JSON 但 length”、工具参数半截 JSON、重复 callId、不同 callId 同参数、seq 跳号及只有 [DONE]；`FT/sse.spec.ts` 按每字节拆帧。要求坏参数 0 次 execute、同 revision 同请求 1 次变更、无 done 0 次接受、错误批前后树一致。 |
| 引用、ID 与局部操作关联失真 | `BT/test_agent_ops.py::test_ops_reference_roundtrip()` 与 `FT/ops.spec.ts` 共读 `tests/fixtures/mindmap_ops.json`，含中文+emoji 偏移、跨来源同句、源 id 重复、旧图缺原文、root/n_8位旧 id、删除父后再改子、重复 add id；两端逐项比较错误与结果，再通过真实 save/GET/PUT/409 闭环核对全部 refs。NLI 矛盾只增加诊断；未编辑节点不得丢引用。 |

## 10. 推断清单与必须实测的确认动作

| 推断（不作为现有能力承诺） | 确认位置与动作 |
|---|---|
| 现网兼容端点能稳定完成 tool_calls 回填 | `B/agent/provider.py::complete_tools()` 做 20 次非推理工具调用探针，记录 finish、参数、tool_call_id、usage 和取消；仅在验证支持时发送相应推理模式参数。供应商 [Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion/) 不能替代当前部署实测；首版不依赖 strict/Beta。 |
| 三分支与材料每支两个要点更好，且不会诱发补写 | `backend/eval/agent_quality.py::evaluate()` 对充分/不足材料分开复核覆盖、误拒和无依据事实；若不足材料误拒不可接受，另设计显式紧凑模式，不偷偷放松全局门槛。 |
| 16384 token 足以覆盖当前产品的合理输出 | 同脚本加入 1000 节点×200 字、密集引用、emoji 与 20000 字来源边界，核对 usage/length；不能沿用调研“每字约 0.6 token”当安全证明，必要时在生成策略中降低节点预算，保存契约仍支持 1000 节点。 |
| 四工具比直接生成更值得付出延迟与 token | `compare()` 用同一输入比较第 0 阶段单次生成和 agent 的人工质量、局部修改成功率与成本；纯主题已达门槛则继续默认旧生成，扩展/编辑可单独启用。 |
| 附件词片检索足以支持来源定位 | `search_sources()` 在 30 个有标注问题上测 Recall@5，目标 ≥90%；失败时允许小输入直接提供完整原文，超过上下文预算明确失败，再评估索引方案，不能把 search() 桩当能力。 |
| 1000 节点下整树快照、候选重放和布局开销可接受 | `F/mindmap/ops.ts::applyOps()` 与 store layout 在目标浏览器/2 核部署环境跑 50 次大树批次，记录 P95 与内存；目标本地接受处理 P95<100ms、无重复重排风暴，未达则做批量渲染优化。 |
| 连接断开能及时停止上游计费 | `test_agent_stream.py` 的连接关闭只证明本地取消；另记录供应商取消后实际 usage/账单，与 1 秒本地关闭指标分开，不宣称关连接就必然零后续费用。 |
| 前端权威状态足够满足使用方式 | `stores/mindmap.ts` 的刷新/多标签验收确认未接受候选可丢、已接受未保存编辑按当前提示处理；若需求转为跨设备续跑，再设计持久任务、事件重放和来源存储，不能靠本计划的内存 RunContext 达成。 |
