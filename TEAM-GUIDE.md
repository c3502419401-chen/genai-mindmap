# 给组员的阅读说明

这个包**已经上线了**（[https://genai-mindmap.rpegio.top/](https://genai-mindmap.rpegio.top/)），不是待填实现的开发骨架。功能按模块分好，接口和数据结构已经定死；剩下的活是补缺口，逐条状态以 [功能清单与分工](docs/FEATURES_AND_OWNERS.md) 为准（已实现 / 部分实现 / 待实现 / 待评估）。

打开顺序建议：先看本文把项目跑起来，再读 `docs/FLOW.md` 搞清楚数据怎么流动，最后按自己负责的部分读 `docs/` 下对应的文档。

---

## 1 五分钟跑起来

需要 Python 3.10+ 和 Node.js 20.19+ / 22.12+。开两个 PowerShell 窗口，都从解压后的根目录出发。

**后端**（第一个窗口）：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

看到 `Uvicorn running on http://127.0.0.1:8000` 就好了。接口文档在 http://127.0.0.1:8000/docs 。

如果 PowerShell 不让执行脚本，用后一种方式绕开激活步骤：

```powershell
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

**前端**（第二个窗口）：

```powershell
cd frontend
npm install
npm run dev
```

打开终端打印的地址（默认 http://localhost:5173）。前端通过 Vite 代理把 `/api` 转到 `127.0.0.1:8000`，后端没起会报“无法连接后端”。

**默认是演示模式**，不调用任何 AI，生成出来的是固定占位结构。要接真实模型，在启动后端的那个窗口先设环境变量：

```powershell
$env:MINDMAP_LLM_MODE = 'model'
$env:MINDMAP_LLM_BASE_URL = 'https://你的服务商/v1'
$env:MINDMAP_LLM_MODEL = '模型名'
$env:MINDMAP_LLM_API_KEY = '你的密钥'
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

模型服务必须兼容 `/chat/completions`，且能按提示词返回 JSON。切回演示模式：`$env:MINDMAP_LLM_MODE = 'demo'`。

数据默认存在 `backend/data/mindmaps.db`（SQLite，已排除在版本控制外）。

---

## 2 怎么验证没改坏

```powershell
# 后端
cd backend
pytest -q            # pytest.ini 已设 pythonpath = .

# 前端
cd frontend
npm test
npm run build
```

当前基线（2026-11-27 本机复核，两个数字都是现跑的）：后端 `pytest -q` 全绿，收集 **3435 条用例 / 153 个测试文件**（`integration` 那一批要本机 Neo4j，没配会自动 skip）；前端 `npm test` **93 个文件 / 1462 条用例全绿**。CI（`.github/workflows/ci.yml`）跑的是同一组命令（Python 3.12 / Node 24），PR 上两个 job 必须双绿。**别把这里的数字当断言用**——它随提交变，要断言就现跑一次。

本机跑后端测试时如果出现 `LegacyOfficeTest::test_xls_cell_formatting` 失败、报 `No module named 'xlrd'`，是本地依赖没装齐（`.xls` 直读要用它），`pip install xlrd` 即可，不是代码问题。

开发服务器左上角有一排「布局示例」按钮（单节点 / 向右 / 向左 / 向下 / 双向）：点一下就能不经过后端装载一份示例导图，用来肉眼检查布局与文本截断。数据在 `frontend/src/dev/samples.ts`，只在 `npm run dev` 下按需加载，生产构建里不进包。

右上角另有「演示示例」按钮，装的是**生产也带**的三张预置导图（Rust 学习路线 / RAG 方案选型 / 一次产品评审），数据在 `frontend/src/demo/presets.ts`，打成独立 chunk（约 6.6 kB），点按钮时才取。它只在用户点的时候加载：模型生成失败仍然如实报错，不会自动拿预置图顶替——演示时哪张是真生成的、哪张是预置的，必须一眼分得清（预置图载入后顶部徽章显示「演示模式（非 AI 生成）」，`mapId` 为空，第一次保存会新建记录而不是覆盖已有导图）。

`unittest` 默认可能将进度输出到 stderr；这本身不代表失败。请同时检查最后的 `OK` 和 `$LASTEXITCODE`，测试成功时退出码应为 0。

---

## 3 目录结构

```
backend/
  app/
    main.py            应用装配，注册 11 组路由（43 个路径 / 53 个端点）
    contracts/         数据契约（改这里要通知另一个人）
      mindmap.py       ContentBase 与三个子类（树 / 流程图 / 鱼骨图）、Node、请求响应模型
      source.py        Source / SourceUsage / SourceRef（节点引用）
      diagnostic.py    Diagnostic / Suggestion
      ops.py           导图操作（add / update / remove / move / edge）
      edits.py         改动日志事件（含生成落画布的 `generate`）
    importing/         外部文件 → 导图（xmind / freemind / opml / puml / markdown / json）
    ingestion/         多模态输入 → Source（文本 / 文档 / 图片 / WAV）
    generation/        模型调用与上下文预算（demo / model）+ `stream_partial.py` 增量解析器
    optimization/      结构诊断与建议（三条结构规则 + 引用核验）
    knowledge/         切块 / 混合检索 / rerank / 知识库（只读入口 + 生成时入库）/ 外部检索
    memory/            记忆模块（生成时注入相关记忆、会话结束总结）
    prefs/             用户偏好（读写 + 从历史推建议）
    settings/          模型服务配置（页面内改地址/密钥/模型，落 SQLite 单行表，回落环境变量）
    auth/              账号与会话（scrypt 口令、sessions 表、HttpOnly cookie、身份中间件 + 门禁中间件）
    agent/             AI 代理（工具循环 / op 执行器 / SSE v2 / 运行台账，生成与编辑的唯一入口）
    export/            导出 puml / markdown / opml / json
    mindmaps/          保存 / 读取 / 版本冲突 / 改动日志
  tests/               3435 条用例 / 153 个文件（2026-11-27 复核）
frontend/
  src/
    App.vue            工作台（画布居中、左侧材料、右侧对话与详情，含保存、打开、冲突弹层、导出、来源面板）
    main.ts            入口（挂 Pinia 与 Vue Flow 样式，启动时探测上次未收尾的运行）
    styles.css         Tailwind 入口
    api/client.ts      fetch 封装 + ApiError（导图 CRUD 4 个、导入 1 个、改动日志 3 个、上传 1 个、导出 4 个、agent 流与台账 4 个、偏好 4 个、记忆 4 个、知识库 2 个、账号 5 个）
    api/types.ts       契约 v2 的 TS 镜像（手写，读起来比生成类型省事）
    api/schema.d.ts    openapi-typescript 从 backend/openapi.json 生成，勿手改
    api/contract-check.ts 编译期断言：镜像与生成类型的键集合、形状必须一致
    canvas/MindMapCanvas.vue  Vue Flow 容器：平移 / 缩放 / 点选 / 键盘建树
    canvas/MindNode.vue       自定义节点：截断 + 悬停看全文 + 内联编辑
    canvas/layout/tidy.ts     树 / 双向布局（自研紧凑树）
    canvas/layout/fishbone.ts 鱼骨图几何（手写 60° 主骨与斜插）
    canvas/layout/flow.ts     流程图分层（dagre）
    mindmap/edits.ts   纯函数：改名 / 加子节点 / 加兄弟 / 删子树 / 提交前校验
    mindmap/ops.ts     纯函数：applyOps（与后端 agent/ops.py 共读同一份用例）
    mindmap/edit-log.ts 改动日志：把用户每次改动转成事件（含生成落画布的 `generate`）
    mindmap/shape.ts   六种节点形状的几何与文本排版
    mindmap/run-status.ts 运行台账的状态映射
    sources/input.ts   来源收集常量与校验（格式白名单、字数与大小上限）
    audio/             浏览器录音（16kHz WAV）与 WAV 头处理
    components/        AgentPanel / RecordButton / SourceInputs / ChatDock / MessageFab / NodeToolbar / VoicePress / HistoryDrawer / PrefsPanel / SettingsPanel
    stores/mindmap.ts  Pinia：内容、布局、选中、编辑、撤销重做、保存状态、agent 生成落地、运行台账
    stores/sources.ts  Pinia：来源列表
    stores/chat.ts     Pinia：对话消息
    stores/prefs.ts    Pinia：用户偏好
    stores/settings.ts Pinia：模型服务配置（读 / 存 / 测试连接）
    demo/presets.ts    三张预置演示导图（生产也带，点按钮才加载）
    dev/samples.ts     开发期布局示例（仅 `npm run dev` 动态加载）
  tests/               1462 条 vitest 用例 / 93 个文件（2026-11-27 复核；纯函数 / 布局 / store / 编辑 / 保存 / 界面 / 引用 / agent / 对话 / 记忆 / 导入 / 三种图型 / 偏好与体检 / 模型设置）
docs/
  FLOW.md              端到端流程 ← 先读这个
  API.md               每条路由实现到什么程度
  ARCHITECTURE.md      模块边界、技术选型及来源
  ARCHITECTURE_ADDENDUM.md  存储迁移、SSE 演进路线、生成链路细节
  PLAN.md              里程碑、验收门、风险表、决策记录（Q1–Q36）
  CONTRACT_V2.md       契约 v2（两端 2026-11-10 已落地；`types.ts` 是 v2 镜像，`schema.d.ts` 由 `openapi.json` 生成，`contract-check.ts` 编译期断言两者一致）
  HANDOFF.md           需要对方知悉或确认的跨人决策（待确认 / 已确认）
  TODO.md              文件占用与交接（协作文件，动共用文件前先看这里）
  W1_DAY1.md           W1 逐日开工清单
  FEATURES_AND_OWNERS.md    26 项功能追踪与 A/B 分工
  PROJECT-STATUS.md    现状总览（比这份指南更权威）
  ONE-CALL.md          生成四步为什么这么切（当前主路径的实测与红线判定）
  ONE-PATH.md          删 `full` 形态那次决策留档（★ 文件头有订正块，引用前先读）
  PROPOSAL.md          申报书原文（目标，不是现状）
  PLAN-VS-IMPLEMENTATION.md  申报书 22 项逐条核对
  USER-GUIDE.md        用户视角的操作手册
README.md              开发者上手文档（在线体验 / 60 秒跑起来 / 五道闸的命令 / 功能状态表 / 文档地图）← 新人先读这个
START_HERE.md          当前可运行范围与易错点清单
NEXT-PLAN.md           2026-11-02 的排期评估快照（已过期，只作历史）
```

---

## 4 现在到底有什么

**能用的**：文本生成导图（demo 或真实模型，走 `mindmap_generate`——一次生成拆四段：骨架 → 每支检索 → 结构 → 说明并发写，实测见 `docs/ONE-CALL.md`；分片的 `mindmap_generate_deep` 代码与规格还在但不注册到 `create`，服务一次响应写不下 200+ 节点的情形）、三种图型（树 / 流程图 / 鱼骨图，各有独立几何）、多来源混合输入（文本 / 文档 / 图片 / WAV / 浏览器录音，合并后一次生成）、结构诊断（单子节点 / 深度上限 / 同级重复标签）与候选建议、节点级来源引用（三层校验）、四种导出（PUML / Markdown / OPML / JSON）、导入外部文件为导图（xmind / freemind / opml / puml / markdown / json）、保存与版本冲突检测、agent 运行（工具循环 + **生成即落画布，没有候选确认层**，`POST /api/agent/runs/stream` 是生成与编辑的**唯一**入口）、修改日志与统计、用户偏好、记忆模块（生成时注入相关记忆）。

**是桩的**：只剩 `POST /api/agent/tasks`（返回固定四阶段，不产生导图；真正的 agent 走 `/agent/runs/stream`）。**知识检索 `/api/knowledge/search` 已经不是桩**——`backend/app/knowledge/router.py` 返回 `implemented: true`，混合检索（BM25 + `text-embedding-v3` 向量）在生成链路内部按支跑；早先那句"返回空 + `implemented:false`"已不成立。

**没有 501 的导出**：Markdown / OPML / JSON 均已实现，四种格式都从浏览器直接下载。

这条规矩写在代码注释里，也写在 `docs/FLOW.md` 第 4 节：**不允许返回空对象假装成功**。桩接口必须带 `implemented:false`，让前端能据此禁用入口。

### 各格式解析的前提

| 格式 | 走哪条路 | 前提 |
|---|---|---|
| `.pdf` | 逐页渲染后交模型视觉识别 | 要 `MINDMAP_LLM_MODE=model` 与密钥；≤20 页；demo 模式回落文本层、扫描件 422 |
| `.docx` `.pptx` `.xlsx` `.html` `.md` | Docling 读结构化格式 | 不需要模型，离线可用 |
| `.doc` `.ppt` | LibreOffice 转 PDF 再视觉识别 | 要装 LibreOffice（约 1.5 GB），没装时 422 并附安装命令 |
| `.xls` | `xlrd` 直读单元格 | 要 `xlrd` 依赖 |
| `.png` `.jpg` 等 | 复用多模态调用 | 同 PDF，demo 模式或未配密钥 422 |
| `.wav` | 百炼 Qwen-Audio-ASR | 要 `DASHSCOPE_API_KEY`；≤300 秒，更长自动切片 |

详细依赖、体积与耗时见 `docs/DEPLOY.md`；`.env` 不会自动加载，必须在启动前把变量设进进程环境。

---

## 5 接口一览（43 个路径 / 53 个端点）

```
基础
GET  /api/health

生成（唯一入口）
POST /api/agent/runs/stream        目标 + 来源 + 当前树 → SSE v2 事件；生成结果直接落画布
POST /api/agent/tasks              目标 → 阶段骨架（桩，implemented:false）
GET  /api/agent/runs               运行台账列表（可筛状态 / 未应用）
GET  /api/agent/runs/{id}          单次运行的详情（含草稿）
POST /api/agent/runs/{id}/applied  标记这次结果已被用户采纳

输入（多来源）
POST /api/ingestion/text           文本 → Source
POST /api/ingestion/files          上传文档 / 图片 / WAV → Source（multipart，≤10MB，音频 ≤20MB）
GET  /api/ingestion/formats        支持的扩展名与上传上限

优化
POST /api/optimization/check       导图 → 诊断（可带 sources 做引用核验）
POST /api/optimization/suggest     导图 → 候选建议 + 诊断

导出
POST /api/export/puml              导图 → PlantUML 文本
POST /api/export/markdown          导图 → Markdown 文本
POST /api/export/opml              导图 → OPML 文本
POST /api/export/json              导图 → JSON

保存与导入
GET  /api/mindmaps                 列表
POST /api/mindmaps                 新建（version 从 1 起）
GET  /api/mindmaps/{id}            读取
PUT  /api/mindmaps/{id}            更新（带版本号，冲突返回 409）
POST /api/mindmaps/import          外部文件（xmind / freemind / opml / puml / markdown / json）→ 导图
GET  /api/mindmaps/{id}/edits      改动日志
GET  /api/mindmaps/{id}/edits/summary  改动统计（按 kind / field 分组）
GET  /api/mindmaps/edits/health    改动日志表的健康检查

用户偏好
GET  /api/prefs                    已保存的偏好
PUT  /api/prefs/{pref_key}         写一条偏好
DELETE /api/prefs/{pref_key}       删一条偏好
GET  /api/prefs/suggestions        从历史里推出来的偏好建议

记忆
GET  /api/memory                   记忆列表
POST /api/memory                   手工添加一条记忆
GET  /api/memory/search            按主题检索（带时间衰减）
DELETE /api/memory/{item_id}       删除一条记忆

检索与知识库
POST /api/knowledge/search         检索（已实现；"库里材料参与生成"另由 MINDMAP_LIBRARY_SEARCH 控制）
POST /api/knowledge/fetch          贴一个 URL，抓回正文变成一份材料（受 MINDMAP_EXTERNAL_DEEPSEEK_FETCH 约束）
GET  /api/knowledge/library        知识库列表（含被引用次数）
GET  /api/knowledge/library/{id}   单份材料正文预览（前 4000 字）

模型服务（页面内配置，改完立即生效）
GET  /api/settings/model           当前生效值 + 每个字段的来源（库 / 环境变量 / 未配）
PUT  /api/settings/model           改地址 / 密钥 / 模型名（密钥只回打码值，清空要显式声明）
POST /api/settings/model/test      测试连接（失败也是 200 + 结论，不是 5xx）

账号与会话（`/api/` 下必须带身份：游客或登录都算，匿名 401 `auth_required`）
GET  /api/auth/me                  当前是谁（永远 200）；全站唯一发游客 cookie 的地方
POST /api/auth/register            注册并直接登录（重名 409，不区分大小写）
POST /api/auth/login               登录（账号或密码不对统一 401，不区分是哪一项）
POST /api/auth/logout              登出（幂等，本来没登录也是 204）
POST /api/auth/password            改密码（需已登录 + 旧密码；改完撤销全部会话）
```

---

## 6 RPeGio 与陈嘉涛的分工

按 `docs/FEATURES_AND_OWNERS.md` 的 F 编号划分，具体见 `docs/PLAN.md` 第 11 节 Q31：

- **A（RPeGio）**：契约与前端（Vue 画布、布局、主题、导出、协作客户端）、部署基建、CI。
- **B（陈嘉涛）**：后端地基、生成链路、多模态解析、RAG 与诊断、AI 代理、评估集。

W1 两条线并行：B 做 D1–D3，A 做 D2 契约 + D4 画布 + D5 部署，W2 起交叉 review。

交界只有一个：`backend/app/contracts/`。改这里的任何字段，必须两个人同时知道，并同步三样东西：`docs/CONTRACT_V2.md`（语义）、`backend/openapi.json` + `frontend/src/api/schema.d.ts`（重新导出与生成）、`frontend/src/api/types.ts`（手写镜像）。漏掉后两步时，`pytest`、`vue-tsc` 或 CI 会各报一处，不会静默通过。

---

## 7 动手前必须知道的四件事

**一、`MindMapContent` 是唯一数据结构。** 节点字段是 `id` / `label` / `children` / `source_refs`（后者可选，节点级引用），不是 `topic`、不是 `text`。上限 1000 节点、8 层、每节点 50 个子节点、标题与节点文字各 200 字，超了直接 422。`note`（正文，≤2000 字）已随契约 v2 上线，保存与导出都直接发送，前端不再裁剪。

**二、保存是乐观锁。** `PUT /api/mindmaps/{id}` 必须带上读到的 `version`，服务端版本号变了就返回 409，前端要提示用户重新打开，不能强行覆盖。

**三、生成接口不自动保存。** `POST /api/agent/runs/stream` 在 `done` 那一刻把整棵树直接装进画布（不落库、也不需要你点确认），落库仍要显式保存。要退回去用「回到生成之前」或撤销。

**四、AI 生成的树算你的作品，但只在本地。** 生成结果直接进画布、标成未保存，错了靠撤销栈与「回到生成之前」；生成期间你照常能改画布。结构诊断与优化建议仍然只读、不自动改树。

---

## 8 已知的坑

- **文字上限有两种口径**：`/api/ingestion/text` 仍是单段 10000 字（`backend/app/contracts/source.py:123`）；提交给 `/api/agent/runs/stream` 的来源正文合计不按字数封顶，改由开流前的 token 估算判断（`backend/app/agent/contracts.py::AgentRunRequest.sources_within_context` → `generation/service.py::ensure_input_within_context`），装不下就在开流前 422。前端来源面板按 300000 字提示（`frontend/src/sources/input.ts:19`），所以 15000 字随生成请求发出去没问题、直接贴进 `/api/ingestion/text` 仍会被 422 挡下。「来源」总条数上限 50（`AgentRunRequest.sources` 的 `max_length`）。
- **引用的原文不落库**：`source_refs` 随导图一起保存（`source_id` + `quote` + 可选的页码与字符区间），但 `Source.text` 只活在本次会话的来源列表里（`frontend/src/stores/sources.ts`）。刷新后重新打开导图，节点角标、来源名与 `quote` 还在，抽屉里却没有前后文——界面会显示"原文没有随导图保存"。另外后端**只在生成时校验引用**，保存时不复检，所以手工改过节点文字之后，旧引用可能已经对不上原文。
- **导出用根节点文字，不是标题**：三种导出取的是根节点的 `_node_text`（`backend/app/export/service.py`，即 `note or label`）。前端“导图标题”和根节点文字是两个独立的输入框，改得不一致时，导出的中心主题会跟标题对不上。
- **导出取节点正文，不再折进标题**：前端把整棵树原样发出去（`toExportV1()` 与 `toContractV1()` 都已删除），由后端 `backend/app/export/service.py::_node_text` 取 `node.note or node.label`：有正文就导正文，没有才回落短标题。所以导出文件里是完整的正文，不是画布上那句被截断的短标题；三种格式都会把换行压成空格（PUML 另把 `*` 换成 `·`），多行正文不会破坏缩进。
- **`stages` 不是实时进度**：只有 `/api/agent/tasks` 那个桩返回固定阶段列表。实时进度看 `/api/agent/runs/stream` 的 `stage` / `tool_start` / `tool_end` 帧。
- **前端还没接的接口**：`POST /api/ingestion/text`、`GET /api/ingestion/formats`、`POST /api/optimization/check` 与 `/suggest`。上传格式白名单目前写死在 `frontend/src/sources/input.ts` 的 `FILE_ACCEPT`，与后端 `formats` 接口有漂移风险；诊断接口已有返回值但界面不渲染（`frontend/src/api/types.ts` 里 `diagnostics` 标注为"目前只用于排障"）。
- **编辑不记协作方**：保存以整棵树为单位（`version` 乐观锁），没有字段级合并；两个人同时编辑同一张图，后保存的会拿到 409，只能选择覆盖（重新加载）或另存为新导图。
- **撤销栈只在内存**：刷新页面后撤销历史清空，最多 50 步或 10MB（先到者为准，节点上限提到 1000 后单份快照可能几十 KB，只按步数会涨到几十 MB）；保存不写历史。
- **旧格式要系统依赖**：`.doc` / `.ppt` 依赖 LibreOffice（约 1.5 GB），没装时 422 而不是降级；`.xls` 依赖 `xlrd`。部署机装不装，直接决定这几个格式能不能用。
- **鉴权在应用层，不在 nginx**：`/api/` 下每条路由都要身份（游客 cookie 或登录，匿名 401 `auth_required`），账号体系在 `backend/app/auth/`（scrypt、`sessions` 表、HttpOnly cookie、身份中间件 + 门禁中间件）。导图存到自己名下、别的账号能看不能改（403 `map_forbidden`）。**仍然没有的**：协作式权限隔离与共享管理、限流与滥用防护——所以别往里放敏感数据。

---

## 9 下一步优先级

如果只有一个人有余力，按这个顺序：

1. **诊断面板**（RPeGio，F15）：接 `POST /api/optimization/check` 与 `/suggest`，把 `diagnostics` 渲染出来（单子节点、深度上限、重复标签、引用核验失败原因），建议仍是候选、需用户确认。
2. **v2 的界面渲染**（RPeGio，F09/F10）：契约 v2 两端已落地，缺的是画布的多布局与主题样式渲染；`relations` / `style` / `pos` 目前只落库、没有界面读它们。
3. **真实密钥环境整体验收**（RPeGio 与陈嘉涛）：真实模型链路只在 2026-11-04 的单份英文论文样本上量过（`docs/LLM-OUTLINE-ACCURACY.md`），中文样本、语音、图片都还没有系统评测。
4. **上线**（RPeGio，F22，2026-11-10 已出可执行方案）：`deploy/install.sh` 一条命令在 Alibaba Cloud Linux 3 上装完 systemd + nginx + Python 3.12，同源反代因此不需要 CORS，**鉴权在应用层**（门禁 + 账号体系；nginx 那层 Basic Auth 已弃用），步骤见 `docs/DEPLOY.md` 第 9 节。**没做**：`Dockerfile` 与 compose 已写但从未在任何机器上构建过——2C2G 上构建 docling+torch 镜像容易 OOM，演示窗口只有一个，容器化等验收后再补。

已完成：**把引用搬到界面上**（F08，2026-11-07，`fc8f82f`）——节点右上角显示引用条数，选中后在右侧详情面板列出来源名与位置（`第 19–37 字`，有页码时一并给出），点一条在抽屉里看原文并把命中句标黄；原文不落库，跨会话只剩生成时记下的 `quote`。

其余按 `FEATURES_AND_OWNERS.md` 的 F 编号推进，范围取舍以 `docs/PLAN.md` 第 2 节为准。

别急着上 Celery、Redis 这些东西。当前是单进程同步架构，RPeGio 与陈嘉涛的时间花在功能上比花在运维上划算得多。

Milvus 是**已经实测过才没上**的，不是没想过：同一批向量、同一台机器，本机 SQLite + numpy 的余弦矩阵乘单次检索约 15 ms，Milvus Lite 单次查询 179 ms、进程常驻内存 +119 MB、官方不支持 Windows，而它的 FLAT 索引内部同样是暴力扫描，数千条量级不产生收益。复评门槛写在 `docs/OPTIMIZATION-CROSS-MAP.md`（历史节点达 50 万条时再看）。所以这条要当"有数据支撑的裁定"讲，别当成拖延。
