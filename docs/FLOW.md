# 端到端流程

本文只看"数据怎么流动"，接口细节见 [API 契约](API.md)，模块职责见 [系统架构](ARCHITECTURE.md)，补齐细节见 [增量附录](ARCHITECTURE_ADDENDUM.md)。

## 1 主链路

```mermaid
flowchart TD
  A1[文本 / Markdown] --> S[ingestion.parse]
  A2[PDF / Word / PPT / Excel / HTML] --> S
  A3[图片 / 手绘草图] --> S
  A4[语音 / WAV 音频] --> S

  S --> SRC[Source<br/>统一来源模型]
  SRC --> U[合并来源<br/>逐项带 source_id]
  U --> O[生成<br/>mindmap_generate 调模型]
  O --> V[validate<br/>MindMapContent 校验]
  V --> R[verify<br/>引用逐字校验 + 契约]
  R --> MAP[导图 JSON]

  MAP --> SAVE[(保存 / 版本)]
  MAP --> CANVAS[Vue 画布（R2/R3）]
  CANVAS --> EDIT[人工编辑<br/>改名 / 增删 / 撤销]
  EDIT --> SAVE
  MAP --> EXP[导出 puml / markdown / opml]
  SAVE --> SUG[optimization<br/>候选建议]
  SUG -.用户确认.-> EDIT
```

图里的两个环节就是第 3 节表格的"合并理解"与"大纲生成"；`understand` / `outline` 是已删除的 `app/pipeline/` 里的阶段名，不再有对应实现。来源合并由前端 `src/sources/input.ts::sourceInputError` 在提交前挡下未解析或空来源；后端**不再**做"正文合计装得进上下文"的预检——正文不进 prompt（`docs/ONE-PATH.md`），图里不再单列这一步。

## 2 入口

| 入口 | 接口 | 输入 | 输出 | 状态 |
|---|---|---|---|---|
| 生成（唯一入口） | `POST /api/agent/runs/stream` | 目标 + 来源 + 当前树 | SSE v2：阶段 / 工具进度 / 整树结果 / op 批次 / 节点生长预览 | 已实现，页面默认且唯一走这条；结果**直接落画布**并标成未保存，可一步撤销 |
| 目标驱动 | `POST /api/agent/tasks` | 一句目标 | 阶段骨架 | 桩（不产生导图） |

两条路由都挂在 agent 上，但只有 `/agent/runs/stream` 真的产出内容：它把改动落进**生成期的工作副本**（`snapshot` 整树或 `ops` 批次），`done` 那一刻整树换进画布（并压一步撤销）——没有「候选 / 接受」这一层；`/agent/tasks` 只回固定阶段列表。原先的 `POST /api/generations`（同步）、`POST /api/generations/stream`（流式）与 `POST /api/pipeline/generate`（多来源流水线）已删除，前端也不再保留对应的两条通道。

## 3 每个环节的输入输出

| 环节 | 模块 | 输入 | 输出 | 状态 |
|---|---|---|---|---|
| 来源解析 | `app/ingestion` | 文本 / Markdown 字符串 / 上传的文档、图片或 WAV / 浏览器录音 | `Source` | 文本、文档（Docling + pymupdf）、图片（多模态视觉）、语音（百炼 Qwen-Audio-ASR，带句级时间戳）均已实现 |
| 合并理解 | `app/agent/tools.py`（`_intake` 的材料入口：短材料退化 + 外部检索） | `Source[]` | 可检索来源 + 骨架的章节标题行 | 已实现；未解析或空内容的来源由前端在提交前挡下（`src/sources/input.ts::sourceInputError`），工具侧只取有正文的来源。正文**不按字数/体积卡**：它不进 prompt，所以没有"装不装得下"这道预检（`MINDMAP_SOURCE_MODE=full` 那条路已删除，见 `docs/ONE-PATH.md`） |
| 大纲生成 | `app/agent/tools.py`（`mindmap_generate`：骨架 → 每支检索 → 结构 → 说明并发写） | 目标文本（或材料清单 + 章节标题） | `MindMapContent` | 已实现（`MINDMAP_LLM_MODE=demo` 时不调模型，直接发占位树；分片的 `mindmap_generate_deep` 代码与规格还在，但**不注册到 `create`**，服务一次响应写不下 200+ 节点的情形，见 `docs/ONE-CALL.md`） |
| 契约校验 | `app/contracts/mindmap.py` | 任何模型输出 | 合法树 / 异常 | 已实现（1000 节点 / 8 层 / 50 子节点） |
| 结构诊断 | `app/optimization.check` | 导图 | `Diagnostic[]` | 单子节点、深度上限、同级重复标签三种结构提示；生成侧只在 `/api/optimization/*` 体检时跑（它原先也挂在已删除的整树生成收尾里） |
| 候选建议 | `app/optimization.suggest` | 导图 | `Suggestion[]` | 同级重复标签 → dedupe 候选，可信度未评估 |
| 知识检索 | `app/knowledge.search` | 查询串 | `KnowledgeHit[]` | 桩（返回空 + implemented=false） |
| 导出 | `app/export` | 导图 | puml / markdown / opml / json 文本 | 四种格式均已实现（JSON 随上游 2026-11-17 落地） |
| 导入 | `app/importing` | xmind / freemind / opml / puml / markdown / json 文件 | 导图 | 已实现（随上游 2026-11-17 落地） |
| 改动日志 | `app/mindmaps/edits` | 用户每次改动 | 事件行 | 已实现（`GET /api/mindmaps/{id}/edits` 与 `summary`，见 `docs/EDIT-LOG.md`） |
| 持久化 | `app/mindmaps` | 导图 + 版本号 | 记录 | 已实现（SQLite + 乐观锁） |

## 4 状态约定

- **已实现**：调用可用，有测试覆盖。
- **桩**：接口固定、返回结构正确，但内容是占位（例如 `implemented: false`、固定阶段列表）。前端应据此禁用入口，不要当成功能完成。
- **未实现**：直接返回 501，不允许返回空对象假装成功。

这条约定写进代码注释，避免半年后有人以为 `knowledge/search` 能用。

## 5 前端接下来要接的位置

| 前端位置 | 调用 | 用途 |
|---|---|---|
| 输入框（`src/App.vue`） | `POST /api/agent/runs/stream` | **已接入**：输入 → 工具循环 → 结果直接落画布（未保存，可一步撤销） |
| 画布（R2） | 无请求 | **已接入**：`src/canvas/MindMapCanvas.vue` 消费 `content`，坐标由 `src/canvas/layout/tidy.ts` 计算；可平移缩放点选，双击内联改文字，Tab / Enter / Delete 建树，撤销栈最多 50 步 |
| 保存（R3） | `POST /api/mindmaps` / `PUT /api/mindmaps/{map_id}` | **已接入**：首次保存 POST 建新图，之后 PUT 带 `version`；409 弹三选（重新加载服务端版本 / 另存为新导图 / 取消）；Ctrl+S 触发，`beforeunload` 拦未保存离开 |
| 打开列表（R3） | `GET /api/mindmaps` / `GET /api/mindmaps/{map_id}` | **已接入**：顶部"打开"弹层列出已保存导图，点开替换画布并清空撤销栈 |
| 上传面板 | `POST /api/ingestion/files` | **已接入**：`src/components/SourceInputs.vue` 上传文档 / 图片 / WAV，解析后进来源列表，再随生成请求一起发给 agent |
| 多来源生成 | `POST /api/agent/runs/stream` | **已接入**：`src/stores/mindmap.ts` 的 `runGeneration(goal, intent, sources)`，来源原样随请求发出（不拼进 goal），结果直接落画布 |
| 录音输入 | `POST /api/ingestion/files` | **已接入**：`src/components/RecordButton.vue` + `src/audio/recorder.ts` 采集 16kHz WAV 后走同一条上传链路 |
| 导出按钮 | `POST /api/export/{puml,markdown,opml,json}` | **已接入**：标题栏下拉菜单，浏览器端落盘 |
| 导入导图 | `POST /api/mindmaps/import` | **已接入**：把 xmind / freemind / opml / puml / markdown / json 文件读成导图（随上游 2026-11-17 落地），导入后是未保存状态 |
| Agent 面板 | `POST /api/agent/runs/stream` | **已接入**：`src/components/AgentPanel.vue` 显示工具进度、生成中预览、落地后的「这次改了什么」与「回到生成之前」 |
| 诊断面板（待接入） | `POST /api/optimization/check` / `/suggest` | 接口已实现，界面还没渲染 `diagnostics`；建议需用户确认后再改 |
| 引用展示 | 无请求（读 `node.source_refs`） | **已接入**：`src/mindmap/refs.ts` 负责定位与截断，节点右上角显示引用条数，选中后在详情面板列出来源与位置，点一条在右侧抽屉里看原文并高亮命中句；左侧来源行反向显示"被 N 个节点引用"，点节点名跳转（`buildRefIndex`）；原文只在本次会话可回查 |
| 正文编辑 | 随保存一起发（`note`） | **已接入**：详情面板的 textarea 编辑 `node.note`（≤2000 字，Esc 放弃），失焦或 Ctrl+S 提交；画布与导出都取 `note or label`，朝向（`layout` / `theme.direction`）也随保存与打开往返 |
| 知识库入口 | `GET /api/knowledge/library` / `.../{source_id}`、`DELETE .../{source_id}`、`GET/DELETE /api/knowledge/aliases*` | **已接入**：顶栏"知识库"抽屉列出库里材料、每份"被几张导图引用"、点开看正文前 4000 字，**并能在列表上直接删掉传错的那份**（被引用时先确认，删掉之后那些节点的引用显示「来源原文不可用」）；抽屉里还有一节「实体别名」，列出模型"把两个名字认成一个"的合并结果并可删掉错的那条（实体与证据边不受影响）。材料在**生成时**才入库，入库后的持久 id 由 `sources` 帧回传、前端就地换（`sources.remap`），所以保存的导图与节点引用重开仍对得上 |
| 运行台账 | `GET /api/agent/runs` 系列、`POST /api/agent/runs/{id}/applied` | **已接入**：`stores/chat.ts` + `ChatDock.vue` 的「上次的结果」块，启动时 `main.ts` 调 `probeResumableRun()` 找回未收尾的运行，点「载入」调 `resumeRun()`；采纳后标 applied |
| 对话与节点工具条 | `POST /api/agent/runs/stream` | **已接入**：`ChatDock.vue`（右下对话）、`MessageFab.vue`、`NodeToolbar.vue`（选中节点后的快捷操作）、`VoicePress.vue`（按住说话） |
| 用户偏好 | `GET/PUT/DELETE /api/prefs`、`GET /api/prefs/suggestions` | **已接入**：`stores/prefs.ts` + `PrefsPanel.vue`，图型与思考模型的选择会记住 |
| 记忆 | `GET/POST /api/memory`、`GET /api/memory/search`、`DELETE /api/memory/{id}` | 接口已实现（随上游 2026-11-19 落地），**界面还没接**：生成时会自动注入相关记忆，但没有让用户增删记忆的入口 |
| 改动日志 | `GET /api/mindmaps/{id}/edits`、`/edits/summary` | 接口已实现，**界面还没接**：目前只写不读，`docs/EDIT-LOG.md` 说明了它要回答的三个问题 |

实时进度只有 agent 那一套：`stage` / `tool_start` / `tool_end` 帧边跑边下发，`snapshot` / `ops` 落的是这一轮的工作树（`draft`，不碰画布），`done` 才算这一轮有了终态——那一刻整树换进画布。`/api/agent/tasks` 的阶段列表是固定文案，不能当进度用。

## 6 还没搭的部分

- `app/collab/`：协作（F17），Yjs + WebSocket，独立服务，不在本次骨架内。
- `eval/`：评估（F23–F26），跑批脚本与样本集。
- SSE 通道：`GET /api/generation-tasks/{id}/events`，等改成异步任务时再加。
