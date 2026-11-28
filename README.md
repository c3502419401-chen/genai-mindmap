# 基于生成式 AI 的思维导图系统

> 输入一段文字、一份文档、一张图片或一段录音，自动生成一张逻辑清晰、结构完整、**可继续编辑**的思维导图。
>
> 它最不一样的一点：**每个节点都能点回原文**——节点挂着它出自材料哪一段，点开跳回去并高亮。生成结果直接进画布、标成未保存，错了用撤销栈退回。
>
> 在线跑着：**[genai-mindmap.rpegio.top](https://genai-mindmap.rpegio.top/)** —— 打开即用，不用本地部署。

**申报书原文**（项目简介 / 研究内容 / 国内外研究现状 / 创新点 / 拟解决的关键问题 / 预期成果）已搬到 [docs/PROPOSAL.md](docs/PROPOSAL.md)，那份文件是**目标**，本文件讲**现状与怎么跑**。

---

## 这个项目是什么

**粘一份材料进去，出来一棵能改、也能查证的树。**

比如一份 3000 字的会议纪要：粘到左侧「材料」里，写一句"校园垃圾分类推行方案"，点发送。5 秒左右中心主题上屏，30–40 秒整棵树落地，画布上是 33–111 个节点（20 棵实测样本的区间）。

它不是一个画好的图片：节点能改字、能拖动换位置；想知道某个节点凭什么这么说，点它右上角的引用角标，抽屉里写着「第 423–432 字 · 共 1,029 字」，材料原文里那一段当场高亮。

### 三条和别的思维导图工具不一样的地方

1. **每个节点带原文引用，点得回去。** 引用不是装饰，是可核验的数据（`source_refs`：来源 + 引文 + 页码或字符区间）。实测**节点引用覆盖率 69.6%–86%**（薄批 78–80%、厚批 86%、同一薄批三次真生成 81.1/77.5/69.6%）；本机库里 731 条引用中带坐标的 729 条**逐字等于原文**（100%）。
2. **结构在画布上改，不用改完文本重新生成。** 拖到左边当它的父、右边当它的子、上下排成兄弟（默认右朝向下的映射，换朝向后左右互换）；断关系、删节点、新增、`Tab`/`Enter`/`Delete` 建树都在画布上做，撤销栈 50 步。树 / 流程图 / 鱼骨图三种图型，换图型不换工具——都在同一块画布上编辑。
3. **自托管，一台 2C2G 就跑得动。** 数据落本地 SQLite 单文件，线上实例就是 2C2G 无 GPU 的 ECS；Neo4j 只是可选件，不装就降级，不挡任何一条主链路。

### 现在能用吗

能用，但它会让你等。三个数字：**可交付率 100%**（20/20 棵都拿到了能存、能导出的树）、**结构合规 85%**（17/20，自动 A–F 判据）、**响应时间只有首帧达标**——首帧 0.09 秒、中心主题 5.4 秒、**完整树 31–41 秒**（极端组合到 139.5 秒）。多人实时协作和翻译还没做。逐项"做了哪部分 / 没做哪部分 / 依据在哪个文件"见下面的「功能状态表」与 [docs/FEATURES_AND_OWNERS.md](docs/FEATURES_AND_OWNERS.md)。

### 技术上怎么回事

生成不是一次成型，是**骨架 → 结构 → 说明**三段：先只定一级分支，再一次写整棵树连同每个节点的引用坐标，最后并发送每个节点的说明——中间夹一次按支检索，合计 3–4 次模型调用。检索按材料长短分岔：**材料合计 ≤5 万字就整份塞进上下文**，超过才按支检索（`FULL_TEXT_MATERIAL_CHARS = 50_000`）。前端 Vue 3 + `@vue-flow/core`，后端 FastAPI + SQLite，图谱可选接 Neo4j；为什么这么选见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 60 秒跑起来（Windows PowerShell）

需要 **Python 3.12+**（CI 用 3.12，本机在 3.13.5 上验过）与 **Node.js 20.19+ / 22.12+**（CI 用 24）。

### 路线 A：用仓库根目录的三个 `*.bat`（本机惯用入口）

| 脚本 | 干什么 | 什么时候用 |
|---|---|---|
| `run-backend.bat` | 配好本机全部环境变量 → 调 `ensure-neo4j.bat` 确保图数据库在跑 → 起 uvicorn（`127.0.0.1:8000`，日志追加到 `.visioncheck\backend.log`，环境快照写 `.visioncheck\env-dump.txt`） | 日常开发，一条命令起后端 |
| `run-neo4j.bat` | 控制台模式起 Neo4j 5.26（`JAVA_HOME` / `NEO4J_HOME` **写死在文件里**，换机器要改这两行；日志 `.visioncheck\neo4j.log`） | 需要图谱那几段时手动起 |
| `ensure-neo4j.bat` | 幂等：7687 已在监听就立刻返回，否则拉起 `run-neo4j.bat` 并最多等 40 秒；**等不到也照样返回 0**（图那几段会自己降级，不会挡住后端） | `run-backend.bat` 自动调用它 |

> **`run-backend.bat` 里写着真实密钥（模型 / 语音 / embedding 三套），`.gitignore` 已把它排除。别提交、别贴到 issue、别复制进别的目录。**
> 换机器时不要照抄它的值，只照抄**变量名**——下面那张表就是变量名清单。

前端另开一个窗口：

```powershell
cd frontend
npm install
npm run dev
```

### 路线 B：不用 bat，两个窗口

后端：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

首次装依赖要拖 `docling` + `torch`（CPU 版），**几个 GB、几分钟**，只有文档解析用得上。不想建 venv 也能跑：本机依赖装齐后直接 `python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000`。

前端（第二个窗口）：

```powershell
cd frontend
npm install
npm run dev
```

### 怎么确认它真的起来了

```powershell
Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/health' -UseBasicParsing | Select-Object -Expand Content
# {"status":"ok","version":"dev"}

Invoke-WebRequest -Uri 'http://localhost:5173' -UseBasicParsing | Select-Object -Expand StatusCode
# 200

Invoke-WebRequest -Uri 'http://localhost:5173/api/health' -UseBasicParsing | Select-Object -Expand Content
# {"status":"ok","version":"dev"}   ← 走通了 Vite 的 /api → 127.0.0.1:8000 代理
```

打开 <http://localhost:5173> 就是工作台；接口文档在 <http://127.0.0.1:8000/docs>。数据默认落 `backend/data/mindmaps.db`（SQLite，已被 `.gitignore` 排除）。

**默认不联网**：不设 `MINDMAP_LLM_MODE` 直接生成会报"缺模型配置"，而不是悄悄给你一张占位图。想先看界面动起来，就设 `$env:MINDMAP_LLM_MODE = 'demo'`——它返回固定结构、一次模型都不调。

### 环境变量（变量名在下面，值自己填）

`.env` 不会被自动加载，必须在**启动后端的那个窗口**里先 `$env:名字='值'` 再起服务。

**要真跑模型（四个变量缺一不可）**

| 变量 | 说明 |
|---|---|
| `MINDMAP_LLM_MODE` | `demo` = 不联网发占位树；`model` = 走 Chat Completions 兼容接口 |
| `MINDMAP_LLM_BASE_URL` / `MINDMAP_LLM_API_KEY` / `MINDMAP_LLM_MODEL` | 服务商地址 / 密钥 / 模型名 |
| `MINDMAP_LLM_MODEL_DEEP` | 可选，"深度思考"开关用的那个模型 |

也可以用页面内配置代替环境变量：顶栏齿轮 → 模型服务（落 SQLite 单行表、立即生效、密钥只回打码值，库里没值时逐字段回落环境变量）。

**可选，按需**

| 变量 | 不配会怎样 |
|---|---|
| `DASHSCOPE_API_KEY` | `.wav` 上传返回 422（百炼 Qwen-Audio-ASR 转写） |
| `MINDMAP_SOFFICE_CMD` | `.doc` / `.ppt` 返回 422（要给 LibreOffice 的 `soffice` 路径，或让它从 `PATH` 里找） |
| `MINDMAP_DB_PATH` | 默认 `backend/data/mindmaps.db` |
| `MINDMAP_AGENT_ENABLED` | 默认开；设 `0` 关闭 `/api/agent/runs/stream` |
| `MINDMAP_NLI_ENABLED` / `MINDMAP_NLI_MODEL_DIR` / `MINDMAP_NLI_MIN_FREE_MB` | 只做机械核验，不做引用语义复核（ONNX 模型，默认关） |
| `MINDMAP_EMBEDDING_*`（`PROTOCOL` / `BASE_URL` / `API_KEY` / `MODEL` / `DIM`） | 检索退回纯 BM25，没有向量那一路 |
| `MINDMAP_RERANK` / `MINDMAP_RERANK_MODEL` | 不重排 |
| `MINDMAP_LIBRARY_SEARCH` / `MINDMAP_LIBRARY_REMEMBER` | 分别为"库里的材料参与生成"与"生成时把用到的材料收进库"，代码默认都开；**线上把检索那条置 0 做纯展示** |
| `MINDMAP_EXTERNAL_KNOWLEDGE` / `MINDMAP_EXTERNAL_PROXY` | 主题生成时先联网检索五源（默认开）；代理缺省是本机 Clash Verge 的 `127.0.0.1:7897`，写 `none` 表示直连 |
| `MINDMAP_EXTERNAL_DEEPSEEK_SEARCH` / `MINDMAP_EXTERNAL_DEEPSEEK_FETCH` | 第 6 个外部源与"贴链接抓正文"，**默认关**，要 key |
| `MINDMAP_DEEPSEEK_TOOLS_API_KEY` / `MINDMAP_DEEPSEEK_TOOLS_BASE_URL` | 上面两个功能用的 key；不填就回落到 `MINDMAP_LLM_*` |
| `MINDMAP_FREE_TIER` / `MINDMAP_FREE_TIER_API_KEY` / `MINDMAP_FREE_QUOTA_LIMIT` | 免费档与每人每天次数（默认 3；不配 key 就没有免费档） |

**生成侧的开关**（`run-backend.bat` 里逐个写着开还是关，改之前先读它旁边的注释——每个都是实测后定的）：`MINDMAP_SECTION_SLICE`、`MINDMAP_GRAPH_INGEST`、`MINDMAP_GRAPH_EXTRACTION`、`MINDMAP_DETAIL_RETRIEVAL`、`MINDMAP_NODE_SCALE`、`MINDMAP_DETAIL_SCALE`、`MINDMAP_DETAIL_LENGTH`、`MINDMAP_OPTIMIZATION_CROSS_MAP`、`MINDMAP_VERIFY_AFTER_GENERATE`。

### Neo4j 是可选的——不配就退化，不会挡住任何一条主链路

`backend/app/knowledge/graph_store.py` 的密码**没有默认值**：不设 `MINDMAP_NEO4J_PASSWORD` 就等于"没配置"。

| 不配 Neo4j 时 | 表现 |
|---|---|
| 写接口（图入图、实体抽取落库） | 抛错，但只在显式写的时候 |
| 读接口（两跳图检索） | 返回空；`MINDMAP_GRAPH_SEARCH` 本来就默认 `0`（实测零收益，理由见 [F14](docs/FEATURES_AND_OWNERS.md) / [OPTIMIZATION-CROSS-MAP.md](docs/OPTIMIZATION-CROSS-MAP.md)） |
| 第三档骨架（材料抽不出章节标题时用图谱定分支） | 跳过那一段，诊断里出现 `GRAPH_OUTLINE_UNAVAILABLE`，**不判失败** |

真要开：`MINDMAP_NEO4J_URI` / `MINDMAP_NEO4J_USER` / `MINDMAP_NEO4J_PASSWORD` / `MINDMAP_NEO4J_DATABASE`（默认 `bolt://127.0.0.1:7687` / `neo4j` / — / `neo4j`），本机怎么起见 [NEO4J-LOCAL.md](docs/NEO4J-LOCAL.md)。

---

## 怎么验证没改坏（和 CI 一样的命令）

顺序与 [.github/workflows/ci.yml](.github/workflows/ci.yml) 的两个 job 逐条对应，照着从上往下跑就是 CI。

后端（在 `backend` 目录，`pytest.ini` 已设 `pythonpath = .`）：

```powershell
python -m ruff check .                                # ① Lint，CI 钉版本 ruff==0.9.10
python scripts/check_script_hygiene.py --gate         # ② 探针脚本周边的五档卫生检查
python scripts/check_doc_claims.py --gate             # ③ 文档断言与代码一致（双向反句表）
python -m pytest -q                                   # ④ 全量（规模看最后一行：它会涨，本篇不写死）
python scripts/acc3/find_unwired.py --gate            # ⑤ "用例测了函数、产品没接上" 的棘轮
python scripts/export_openapi.py; git diff --exit-code openapi.json   # ⑥ OpenAPI 快照必须无差异
```

前端（在 `frontend` 目录）：

```powershell
npm run gen:api; git -C .. diff --exit-code frontend/src/api/schema.d.ts   # ① 生成类型必须与提交一致
npm test                                              # ② 全量 vitest（同一个道理：规模看输出，不抄在这里）
npm run test:cost:gate                                # ③ 用例的成本棘轮（超时上限 vs 满载实测）
node scripts/find_unwired.mjs --gate                  # ④ 前端同一道零引用棘轮
npm run build                                         # ⑤ vue-tsc --noEmit && vite build
```

两组的对应关系写在 [.github/workflows/ci.yml](.github/workflows/ci.yml)，PR 上两个 job 必须双绿。

**③ 是"文档也会漂"的那一道**：它把"文档说的事"与"代码现在做的事"对表——`create` 到底注册了哪个生成工具、`knowledge/search` 是不是桩、有没有门禁、哪些开关默认是开的、画布有几种朝向。发现矛盾时它先告诉你**这条事实凭什么成立**（用例名或文件），再让你选改文档还是改代码。怎么加一行、怎么豁免，见 [DOC-CLAIMS.md](docs/DOC-CLAIMS.md)。

**改了 `backend/app/contracts/` 里的任何字段**，按这个顺序走，别漏（漏哪一步，`pytest` / `vue-tsc` / CI 会各报一处）：

```powershell
cd backend
python scripts/export_openapi.py     # 更新 backend/openapi.json
cd ..\frontend
npm run gen:api                      # 重新生成 src/api/schema.d.ts
```

再手工同步 `frontend/src/api/types.ts`（手写镜像）——`contract-check.ts` 会在编译期断言镜像与生成类型的键集合一致。

---

## 架构（一屏）

```
来源：文本 / 文档(PDF·docx·pptx·xlsx·md·html·旧 Office) / 图片 / WAV
   │
   │  POST /api/agent/runs/stream   （SSE v2，生成与编辑的唯一入口）
   ▼
backend/app/agent/  工具循环，工具按意图注册（_ALLOWED_BY_INTENT）
   ├── create → mindmap_generate            四步、3–4 次模型调用
   │      ① 骨架：只定一级分支（>10 支先归成 3–6 组）
   │      ② 检索：knowledge/ 混合检索，每支各一次
   │      ③ 结构：一次写整棵树 + source_refs（detail 留空）
   │      ④ 只写说明：按节点数切两半并发，按 id 对号写回
   ├── expand → node_expand / audit_tree
   └── edit   → 六条细粒度 + mindmap_edit + mindmap_fill_details
   │
   │  done 帧：整棵树直接落画布（不落库、不需要点"应用"）
   ▼
frontend/src/  Pinia store → @vue-flow/core 画布（四种朝向）
   │  Ctrl+S / 保存（version 乐观锁，409 走三选弹层）
   ▼
backend/app/mindmaps/  SQLite（data/mindmaps.db）+ edit_events 改动账本
```

**为什么"按意图注册"这件事要单独记住**：生成侧有**两条实现、一条注册**——`mindmap_generate`（整树一次成型，`create` 唯一注册的那条）与 `mindmap_generate_deep`（分片，代码/规格/用例都在但**不注册到 `create`**，挂在非产品意图 `create_deep` 下，服务"一次响应写不下 200+ 节点"的大图）。两条共用材料准备（`_intake`）、骨架（`_skeleton_outline`）与按支检索（`_retrieve_branches`），**改这几处等于同时改两条路**。历史上有四次"只改了两三条路径"，单点测试全绿而链路是断的——这条警告原文在 [AGENTS.md](AGENTS.md) 第 49 行，被 `backend/tests/test_one_path.py` 钉着。

### 目录导览

```
backend/app/
  main.py              应用装配，全部路由全挂 /api（组数 / 路径数 / 端点数见 `backend/openapi.json`，CI ⑥ 钉着它的快照）
  contracts/           数据契约：树 / 流程图 / 鱼骨图三种形状、Node、请求响应、ops、edits（★改这里要通知另一人）
  agent/               AI 代理：工具循环、op 执行器、SSE v2、运行台账 —— 生成与编辑的唯一入口
  generation/          单次模型调用（demo / model）、上下文预算、增量解析、免费档与额度
  ingestion/           多模态输入 → Source（文本 / 文档 / 图片 / WAV）
  knowledge/           切块、混合检索、rerank、知识库（只读入口 + 生成时入库）、外部五源、图谱
  optimization/        结构诊断（三条规则 + 引用核验 + NLI）、自动修复、跨图参照
  importing/           外部文件 → 导图（xmind / freemind / opml / puml / markdown / json）
  export/              导出 puml / markdown / opml / json
  mindmaps/            保存 / 读取 / 版本冲突 / 改动日志（repository 层）
  prefs/               用户偏好（显式偏好 + 风格画像 + 从历史推建议）
  memory/              记忆模块（生成时注入、会话结束总结）
  settings/            模型服务的页面内配置（SQLite 单行表，回落环境变量）
  auth/                账号与会话（scrypt 口令、sessions 表、HttpOnly cookie、门禁中间件）
  core/                进程级配置
  tests/               后端用例（规模现跑：`pytest --collect-only`）

frontend/src/
  App.vue              工作台（画布居中、左材料、右对话与详情）
  canvas/              Vue Flow 容器、自定义节点、三种几何（tidy 树 / dagre 流程 / 手写鱼骨）
  mindmap/             纯函数：改名/加删节点/断关系（edits.ts）、op 执行（ops.ts）、形状与排版、改动账本、引用
  stores/              Pinia：内容与撤销栈、来源、对话、偏好、模型设置
  api/                 fetch 封装 + ApiError、types.ts（手写镜像）、schema.d.ts（生成，勿手改）、contract-check.ts
  components/          AgentPanel / HistoryDrawer / EditLedger / AliasResolver / SourceInputs / RecordButton …
  sources/ audio/      输入侧常量与校验、浏览器录音（16kHz WAV）
  demo/ dev/           生产也带的三张预置导图、仅 `npm run dev` 加载的布局示例
  tests/               前端用例（规模现跑：`npm test`）
```

---

## 技术栈与实测口径

| 位置 | 选型 | 说明 |
|---|---|---|
| UI | Vue 3（`<script setup>`）+ TypeScript | 申报书原写 React，React 骨架打过 tag `v0.1.0-react-scaffold` 存档 |
| 状态 | Pinia | 不用 Zustand |
| 画布 | `@vue-flow/core` + 自研 tidy-tree | 流程用 dagre，鱼骨手写 60° 主骨；不引通用图布局库 |
| 样式 | TailwindCSS 4 | — |
| 网络 | 原生 `fetch` + 直接读 SSE 流 | 非 axios；错误统一 `ApiError` |
| 校验 | 后端 Pydantic v2；前端 `openapi-typescript` 生成 + 编译期契约断言 | 不引运行期 schema 库（非 Zod） |
| 后端 | FastAPI + Pydantic v2，单进程 | 端点数与路径数以 `backend/openapi.json` 为准（CI ⑥ 用 `git diff` 钉着） |
| 存储 | SQLite 单文件 + 版本号乐观锁 | 不用 PG；`repository.py` 直连 |
| 检索 | 本机 SQLite 缓存向量 + numpy 余弦矩阵乘（1024 维 `text-embedding-v3`，BM25 混合、RRF 融合） | **不引独立向量库**：同规模实测 Milvus Lite 单次 179 ms、常驻内存 +119 MB、官方不支持 Windows，而本机方案全库 3652 节点单次约 15 ms；复评门槛是历史节点达 50 万条（[OPTIMIZATION-CROSS-MAP.md](docs/OPTIMIZATION-CROSS-MAP.md)） |
| 图谱 | Neo4j（可选） | 开关默认 0，实测零收益（F14） |
| 部署 | 单机 systemd + nginx 同源反代（ECS 2C2G / 无 GPU） | 同源所以不需要 CORS，鉴权在应用层；`docker compose` 写了但**从未在真机上完成一次完整构建与运行验证**（[DOCKER.md](docs/DOCKER.md) 第 9 节的待确认清单） |
| 模型 | 任意 OpenAI 兼容 `/chat/completions`；线上 `z-ai/glm-5.3-flash` | `demo` 模式不联网 |

**延迟分三段报，不报单一数字**（`docs/ACCEPTANCE-METRICS.md` §4.2，20 条材料样本、单并发）：

| 段 | 你感觉到什么 | P50 | P95 |
|---|---|---|---|
| 首帧 | 界面有反应了 | **0.09 秒** | 0.12 秒 |
| 中心主题上屏 | 画布开始长 | **5.4 秒** | 9.5 秒 |
| 完整树落地 | 可以保存/导出了 | **31.3 秒** | 41.4 秒（无材料冷启动 74–89 秒） |

极端组合（205 节点、外部检索冷启动、生产开关全开）端到端实测 **139.5 秒**；耗时近似 `节点数 × 0.35 秒`，由模型输出速度决定，不由本系统决定（[LATENCY-BREAKDOWN.md](docs/LATENCY-BREAKDOWN.md)）。申报书写的"整图 < 3 秒 / 复杂场景 < 5 秒"是**目标值**，物理上做不到，口径怎么改的见 [PLAN-REWORDING.md](docs/PLAN-REWORDING.md) 第一节。

---

## 功能状态表

**逐条状态以 [docs/FEATURES_AND_OWNERS.md](docs/FEATURES_AND_OWNERS.md) 的 F 编号追踪表为准**（F01 起、每项一个编号，新增功能往后加号，所以这里不写总数），那张表每一项都写到"做了哪部分 / 没做哪部分 / 依据在哪个文件"。申报书与实现的逐条核对（含未实现项、口径修正、复现脚本）在 [docs/PLAN-VS-IMPLEMENTATION.md](docs/PLAN-VS-IMPLEMENTATION.md)。

**这张表只说"做没做"，不说"凭什么做得到、前提是什么"**：边界、前置条件、以及"哪些是这个形态下根本做不到的"的判定在 [docs/SCOPE.md](docs/SCOPE.md)。

粗分三档：

| 档 | 是什么 |
|---|---|
| **能用** | 文本/文档/图片/录音多来源 → 生成（demo 或真实模型，`mindmap_generate` 四步）｜三种图型（树 / 流程 / 鱼骨，各自独立几何）｜画布编辑（改字、Tab/Enter/Delete 建树、拖动改父子、撤销重做 50 步）｜保存与版本冲突（409 三选）｜导入 6 种 / 导出 4 种｜节点级来源引用（三层校验，界面可点回原文）｜结构诊断 + 引用核验 + NLI｜外部五源检索｜账号与会话（游客 cookie + 注册/登录，导图存自己名下）｜改动账本｜用户偏好与风格画像｜记忆模块 |
| **部分** | 知识图谱（抽取/消解/两跳检索都写了，**实测零收益所以默认关**）｜主题预设与字体主色的**界面渲染**（契约有字段，界面不渲染）｜`relations` / `style` / `pos`（只落库、界面不读）｜容器化（写了没真机验证）｜评测（85% / 90% / 5 秒三条指标**没有实测数据**） |
| **没做** | 多人实时协作（F17）、协作场景的 AI 汇总（F18）、多语言翻译（F19）、模型微调（F21）、浏览器内 PlantUML 渲染（语法上不可能，见 [PLAN-REWORDING.md](docs/PLAN-REWORDING.md) 第五节）、Kubernetes |

<!-- claims-ok: knowledge_search_implemented 这句在纠正旧说法，引用的是被否定的原话 -->
**唯一的桩**是 `POST /api/agent/tasks`（返回固定四阶段、不产生导图，`implemented: false`）。知识检索 `/api/knowledge/search` **不是桩**——早先的文档说"返回空 + implemented:false"，那句已经过时。全站没有返回 501 的路由。

---

## 已知限制与坑

**它会让你等**

- 一次生成 **31–140 秒**，看节点数与开关。这不是检索慢，是输出量大（3–4 次模型往返，≈ 节点数 × 0.35 秒）。要快就关 `MINDMAP_NODE_SCALE`（实测 −43%）；"深度思考"开着会更慢。
- **引用覆盖不到三成**：同一份 14,120 字材料、纯 BM25 下材料覆盖率实测 **30.5%**。界面上表现为大量节点没有可核验引用——一次 64 节点的图 20 条诊断里绝大多数是这一条。它是提示不是错误。

**输入格式是窄的**

- **语音只收 8k / 16k 的 WAV**（`SUPPORTED_RATES = (8000, 16000)`，16bit 单声道）。手机录音默认 44.1 kHz，直接传会 422；浏览器内录的那个按钮产出的是合规 WAV。
- `.doc` / `.ppt` 要装 LibreOffice（约 1.5 GB），没装返回 422 而不是降级；`.xls` 要 `xlrd`（已在 `requirements.txt`）。各格式的前提表在 [TEAM-GUIDE.md](TEAM-GUIDE.md) 第 4 节。
- PDF 视觉识别要求 `MINDMAP_LLM_MODE=model` + 密钥；`demo` 下只回落文本层，扫描件 422。

**数据上的边界**

- **改动账本记不住"为什么"**：`edit_events` 只记"谁（`user` / `agent`）在哪个字段、哪一层、改了什么"，不记动机；也**记不住是哪个用户**——`actor` 只有 `user` / `agent` 两个取值。想回答"用户为什么嫌这一层多余"，现在只能看分布、靠人猜（`docs/EDIT-LOG.md`）。一张典型导图约 37 条事件。
- **引用的原文不落库**：`source_refs`（`source_id` + `quote` + 可选页码与区间）随导图保存，但 `Source.text` 只活在本次会话里。刷新重开，角标与引文还在，抽屉里没有前后文。
- **后端只在生成时校验引用，保存时不复检**：手工改过节点文字之后，旧引用可能已经对不上原文。
- **导入会丢东西**：只有本仓库 JSON 无损；其余格式形状会退化、引用被清空。
- 视图默认只画 40 个节点（收起状态不持久化）；导图最多 8 层；流程图/鱼骨图不支持跨容器拖动，阶段与骨的次序拖不动；坐标只由布局算，不能自由摆放。

**工程上的坑**

- `run-backend.bat` / `run-neo4j.bat` / `ensure-neo4j.bat` **写死本机路径**（`D:\genai-mindmap`、`C:\tools\jdk-21…`、`D:\neo4j\neo4j-community-5.26.0`），换机器要改；`.bat` 用纯 ASCII 注释，cmd 在 936 代码页下会把 UTF-8 注释切成半个字符当命令执行。
- Neo4j 在这台机器上**不会自启**（没有 service，装 service 要管理员），所以 `run-backend.bat` 才去调 `ensure-neo4j.bat`。
- 本机跑后端测试若报 `No module named 'xlrd'`，是依赖没装齐，不是代码问题。
- `backend/app/pipeline/` 目录下只剩 `__pycache__`（源码整包删了），别照着名字找实现。

---

## 文档地图

`docs/` 下分八组（份数会随提交变，现算：`git ls-files '*.md' | wc -l`）。**只想知道"现在是什么状态"读前三份就够了**：`PROJECT-STATUS.md` → `FLOW.md` → `FEATURES_AND_OWNERS.md`。

**每一份各属于哪一档（`entry` 入口 / `reference` 按需查 / `archive` 存档 / `ai-only` 只给 AI）在 [docs/INDEX.md](docs/INDEX.md)**：第三节是全量登记，由 `backend/scripts/check_doc_map.py --write` 从 `git ls-files '*.md'` 现算生成、`--gate` 钉着"一份不漏、没有死链、`entry` 两跳可达"。下面这张表是**推荐路径**（人去读的顺序），不是清单——清单在那边，所以这张表漏了什么不会变成烂账。

| 组 | 先读这些 | 讲什么 |
|---|---|---|
| **入门 / 现状** | `PROJECT-STATUS.md`、`FLOW.md`、`ARCHITECTURE.md`、`ARCHITECTURE_ADDENDUM.md`、`API.md`、`CONTRACT_V2.md` | 现状总览、端到端流程 ← 第一份该读的、模块边界与选型、接口逐条实现到哪、契约 v2 |
| **计划 / 分工 / 交接** | `PLAN.md`、`FEATURES_AND_OWNERS.md`、`HANDOFF.md`、`TODO.md`、`W1_DAY1.md` | 里程碑与验收门（含决策记录 Q1–Q36）、26 项功能追踪与 A/B 分工、需双方确认的跨人决策、文件占用 |
| **生成链路**（最常改） | `ONE-CALL.md`、`ONE-PATH.md`、`GENERATION-PIPELINE-NOTES.md`、`AGENT_STREAM_PROTOCOL.md`、`AGENT_PLAN.md`、`GRAPH-OUTLINE.md`、`SECTION-SLICE.md`、`MIXED-SKELETON.md`、`BRANCH-CAP.md`、`BRANCH-DIGEST.md`、`INPUT-EXTRACT.md`、`OFF-TOPIC-AND-FEW-BRANCHES.md`、`GENERATE-REPAIR-SELFCHECK.md` | 四步生成为什么这么切（含实测与红线判定）、删/恢复整树生成的那两轮、SSE 帧、骨架三档、按章切、两级骨架、分支数与概括角标、收尾自检 |
| **检索 / 知识 / 图谱** | `RAG-DECISIONS.md`、`RETRIEVAL-EVAL.md`、`RETRIEVAL-KEEP.md`、`SOURCE-REFERENCES.md`、`DETAIL-RETRIEVAL.md`、`EXTERNAL-SELECTION.md`、`EXTERNAL-RELEVANCE.md`、`LIBRARY-MEASUREMENTS.md`、`ENTITY-RESOLVE.md`、`GRAPH-TWO-PASS.md`、`NEO4J-LOCAL.md` | 混合检索与 RRF、评估基线、按分数取数、三层引用校验、外部五源与相关性门、库与实体别名、图谱两段式 |
| **画布 / 交互 / 导出** | `NODE-EDIT.md`、`NODE-DRAG.md`、`DRAG-ROUND3.md`、`VIEW-COLLAPSE.md`、`LAYOUT-CLARITY.md`、`FISHBONE-CATEGORIES.md`、`IMPORT.md`、`EDIT-LOG.md`、`NODE-ORIGIN.md` | 三个编辑动作的判据、拖动语义、收起与可读性、鱼骨类别、导入退化、改动账本字段 |
| **质量 / 验收 / 核算** | `ACCEPTANCE-REPORT.md`、`ACCEPTANCE-METRICS.md`、`ACCEPTANCE-EVIDENCE.md`、`BLIND-SCORING.md`、`PLAN-VS-IMPLEMENTATION.md`、`PLAN-REWORDING.md`、`LATENCY-BREAKDOWN.md`、`USER-PATH-TESTS.md`、`WIRING-GAPS.md`、`SCRIPT-HYGIENE.md`、`DOC-CLAIMS.md`、`TEST-COST-136-NODE.md` | 三条指标的口径与实测、"代码在、用例绿、产品没接上"的三种形状、申报书的替换句、延迟分段账、**"文档说的事与代码对不对"那道闸门怎么加一行** |
| **部署 / 依赖 / 成本** | `DEPLOY.md`、`DOCKER.md`、`LEGACY-FORMAT-COST.md`、`PDF-PARSER-SELECTION.md`、`FREE-MODELS.md`、`OPTIMIZATION-CROSS-MAP.md` | systemd + nginx 一条命令装完、compose 的未验证清单、旧 Office 解析代价、免费模型档、向量库选型实测 |
| **历史 / 已退休 / 调研** | `ONE-PATH.md`、`FOLD-RETIRED.md`、`TWO-ARCH-COMPARISON.md`、`EXTERNAL-SELECTION.md`、`MEMORY-PROPOSITION.md`、`PROFILE-ISOLATION.md`、`research/` | 决策留档与被推翻的部分（**引用前先读文件头的订正块**）、外部调研汇总 |

<!-- claims-ok: create_registers_generate 「读文档的纪律」一节里引用旧说法当反例 -->
**读文档的一条纪律**：这个仓库的文档写得很细，也因此有"反了"的风险——**说某个东西死了，可能它活着**（`ONE-PATH.md` 说"只剩分片"，实际 `create` 注册的是整树生成）；**说某个东西是桩，可能它已经接了**（知识检索）。凡涉及"已删除 / 仍是桩 / 默认关"的断言，去代码或 `backend/tests/` 里核一次再引用——这三类断言都有自己的钉子用例。

**而这件"核一次"现在有机器在做了**：`python scripts/check_doc_claims.py --gate`（CI 第三步，机制见 [DOC-CLAIMS.md](docs/DOC-CLAIMS.md)）把"文档说的事"与"代码现在做的事"对表——`create` 注册了哪个生成工具、`knowledge/search` 是不是桩、有没有门禁、每个开关各自的默认值（闸门自己数的全量，不是抽验）、画布有几种朝向；它顺带判第四类——**文档里不许写会漂的数**（用例数 / 文件数 / 端点数 / md 份数，理由见 `docs/BACKLOG.md` 第 12 条）。上面那句"去核一次"仍然成立，只是**漏核会红在 CI 上**，而不是等下一轮的人读到时才发现。

---

## 关于本仓库

- 申报书原文（项目简介、研究内容、国内外研究现状、创新点、拟解决的关键问题、预期成果）与"技术路线"那一节的原文摘录，见 [docs/PROPOSAL.md](docs/PROPOSAL.md)；本文件写的是**实现侧的现状**。
- 申报书原文写作 `pluml-encode`，这里按 npm 实际包名写作 `plantuml-encoder`（另有 `plantuml-encode` 存在，是不同包）。
- 用户视角的操作说明见 [docs/USER-GUIDE.md](docs/USER-GUIDE.md)（从装好到做出一张有用的图）；组员上手与分工见 [TEAM-GUIDE.md](TEAM-GUIDE.md)；仓库根目录的 [START_HERE.md](START_HERE.md) 是状态与易错点清单。
- **"某一件事该看哪份文档"的答案是 [docs/INDEX.md](docs/INDEX.md)**（人读前两节：从哪儿开始、按问题查；第三节是全量登记，由 `backend/scripts/check_doc_map.py --write` 生成、`--gate` 钉着）。
- 2026-11-02 的排期评估快照 [NEXT-PLAN.md](docs/archive/NEXT-PLAN.md) 已从仓库根目录移入 `docs/archive/`——它整篇过期，而根目录是最强的入口位，读者会先看到它、然后读到过期的话；替代物是 [PROJECT-STATUS.md](docs/PROJECT-STATUS.md)。
- 协作规则：`main` 是生产分支（线上跑的就是它）、`dev` 是集成分支、PR 目标一律 `dev`；提交信息用中文 Conventional Commits；CI 必须绿才能合并。改 `backend/app/contracts/` 必须两人同时知道。
