# 起步说明：这是一套**已上线**的系统，不是开发骨架

本仓库不是"开发起点"，也不是"最小文本演示闭环"——那是 2026-11-03 的状态。它已经上线：**[https://genai-mindmap.rpegio.top/](https://genai-mindmap.rpegio.top/)**，申报书里的功能大部分已实现，少数仍是桩、待评估或本期不做，**逐条状态以 [功能清单与分工](docs/FEATURES_AND_OWNERS.md) 为准，不要按"骨架"的旧印象判断它能做什么**。系统设计见 [系统架构](docs/ARCHITECTURE.md)，里程碑与工程门见 [项目执行计划](docs/PLAN.md)，目标契约见 [CONTRACT_V2](docs/CONTRACT_V2.md)（**v2 已两端落地（2026-11-10）**：`note` / `layout` / `theme.direction` 已接线，`relations` / `style` / `pos` 仅落库、尚无界面读取），需要双方确认的跨人决策见 [交接文档](docs/HANDOFF.md)。

## 当前可运行范围

- 前端 Vue 3 + TypeScript：文本 / 文档 / 图片 / 录音多来源输入 → agent 工具循环生成 → `@vue-flow/core` 画布（平移、缩放、点选节点看全文，支持向右 / 向左 / 向下 / 双向四种布局）→ 双击改文字 / Tab 加子节点 / Enter 加兄弟 / Delete 删子树 / 撤销重做 / Ctrl+S 保存（`version` 乐观锁，409 时三选弹层）→ 打开已保存导图 → 导入（JSON / Markdown / OPML / XMind / FreeMind / PlantUML）→ PUML / Markdown / OPML / JSON 四种导出 → 顶栏「知识库」（库里的材料列表、被几张导图引用、点开看正文）与「我的偏好」→ Agent 面板（工具循环的结果直接落画布、标成未保存，没有「应用」这一步；旧 React 骨架已打 tag `v0.1.0-react-scaffold` 存档）。生成与编辑现在只有 `POST /api/agent/runs/stream` 一条通道。
- 后端 FastAPI：agent 工具循环（`/api/agent/runs/stream`，工具**按意图注册**：`create` 只有 `mindmap_generate`；`expand` 给 `node_expand` + `audit_tree`；`edit` 给八个——六条细粒度（`add_node` / `update_node` / `remove_node` / `move_node` / `add_edge` / `remove_edge`）+ 批量 `mindmap_edit` + 只补说明的 `mindmap_fill_details`；产物是 op 批次或整树、`done` 时直接落画布）、结构校验、SQLite 保存和读取、版本冲突检测、改动日志（`edit_events`：谁改了什么、删的是零散还是整支）、用户偏好（`user_prefs`）、多来源输入（文本 / 文档 / 图片 / 语音，随生成请求一起交给 agent）、文档上传（PDF 逐页视觉识别、Docling 结构化格式、LibreOffice 转旧格式、`xlrd` 读 `.xls`、百炼 Qwen-Audio-ASR 转写 WAV）、节点级来源引用与三层校验、导入与四种格式导出、统一错误体、基础结构提示。旧的同步生成接口 `POST /api/generations`、流式 `POST /api/generations/stream` 与多来源流水线 `POST /api/pipeline/generate` 已删除。

  **生成侧是"两条实现、一条注册"**（`backend/app/agent/tools.py::_ALLOWED_BY_INTENT`）：
  `create` 只注册整树一次成型的 **`mindmap_generate`**（内部四步：骨架 → 每支检索 → 结构 → 只写说明，节点多时拆两半并发，实测见 `docs/ONE-CALL.md`）；
  分片的 **`mindmap_generate_deep`** 代码、规格与用例都还在，但**不注册到 `create`**——它挂在非产品意图 `create_deep` 下，服务的是"一次响应写不下 200+ 节点"的情形，`service.run` 永远不会把意图解成这个名字。
  `docs/ONE-PATH.md`（2026-11-16）删掉的是**形态开关 `MINDMAP_SOURCE_MODE` 与 `full` 那两份提示词**，不是整树生成；整树生成在 2026-11-18 以另一种接法回来了（`bc3494e`，`docs/ONE-CALL.md`）。
<!-- claims-ok: create_registers_generate 告诫句：「别再写」后面引的正是这句禁语 -->
  **别再写"新建导图只剩分片这一条路"**：把活的写成死的，下一个人会按错模型改提示词。这条被 `backend/tests/test_one_path.py` 钉着，`AGENTS.md` 第 49 行也写着同样的口径。
- 设 `MINDMAP_LLM_MODE=demo` 时生成固定结构，用于联调，**不会调用 AI**；agent 只认显式的 `demo`，没设这个变量不会自动降级成占位图（缺模型配置会直接报错）。设 `model` 并配好 `MINDMAP_LLM_BASE_URL` / `MINDMAP_LLM_API_KEY` / `MINDMAP_LLM_MODEL` 才会走 Chat Completions 兼容服务，实际能力取决于服务商和模型（线上用 `z-ai/glm-5.3-flash` 经 `https://api.commandcode.ai/provider/v1`，2026-11-16 起；此前的 `deepseek-flash` 走官方端点）。
- **尚未实现/未接界面**：知识图谱的**嵌入与节点关系优化算法**（抽取、消解、别名边、两跳图检索都已落地，实测零收益所以 `MINDMAP_GRAPH_SEARCH` 默认 0，见 `docs/FEATURES_AND_OWNERS.md` F14）、协作（F17）、翻译（F19）、评测（F24–F26）、主题预设与字体主色的**界面渲染**（`Theme.preset` / `font` / `accent` 前端一个都没渲染；v2 的 `relations` / `style` / `pos` 只落库、界面不读。注意：**四种朝向是真的有**，见上一行）、浏览器内 PlantUML 渲染（只生成+下载）、容器化（Docker/compose 写了但从未真机构建，线上的 systemd + nginx 方案见 `docs/DEPLOY.md` 第 9 节）。
  **几处容易写反的现状**：`/api/knowledge/search` **早已不是桩**（`backend/app/knowledge/router.py` 返回 `implemented: true`，桩只剩 `/api/agent/tasks` 一个）；"库检索参与生成"这条在部署里置 0 做纯展示（`MINDMAP_LIBRARY_SEARCH`，代码默认 1），那是**部署开关**不是桩。图片与语音处理已在 2026-11-05 落地；知识库的**只读入口**（顶栏「知识库」：材料列表、被几张导图引用、点开看正文前 4000 字）与"生成时把用到的材料收进库"在 2026-11-16 落地，库内材料的**删除**（被引用时先 409 确认）与实体别名清单也已做；流程图与鱼骨图（`diagram_type` + 附加边 `edges`）、节点颜色与图标、导入、用户偏好、改动日志在 2026-11-17 落地。
- agent 的忠实整理模式记录整图来源摘要与节点级引用（`source_refs`，机械核验 + 可选 NLI），都能随导图保存；**界面已能展示引用**：节点右上角显示来源条数，选中后在右侧详情面板列出来源与位置，点一条在抽屉里看原文并高亮命中句。原文不持久化，所以跨会话重新打开只剩生成时记下的摘录。来源正文合计不按固定字数封顶：后端在开流前按 token 预算判断（`backend/app/generation/service.py` 的 `CONTEXT_TOKEN_LIMIT`，200000 token，超了给可操作的错误而不是截断），前端来源面板的提示上限是 300000 字（`frontend/src/sources/input.ts:19`）。

**它已经在公网上跑**（单机 2C2G ECS + systemd + nginx，同源反代所以不需要 CORS）：`/api/` 下每条路由都必须带身份，游客 cookie 或登录都算，匿名 401 `auth_required`；账号体系在 `backend/app/auth/`（scrypt 口令、`sessions` 表、HttpOnly cookie、身份中间件 + 门禁中间件），导图存到自己名下、**别的账号能看不能改**（403 `map_forbidden`）。**仍然没有的**：协作式的权限隔离与共享管理、限流与滥用防护、备份之外的运维自动化——所以别往里面放敏感数据。

## 启动（Windows PowerShell）

**完整的开发者上手文档在 [README.md](README.md)**（在线体验、60 秒跑起来、三个 `*.bat` 的用法、可选 Neo4j、五道闸的命令、目录导览、功能状态表、已知的坑、文档地图）。本节只留最短路径与两处易错点。

需要 Python 3.12+ 与 Node.js 20.19+ 或 22.12+（CI 用 3.12 / 24）。在项目根目录分别打开两个 PowerShell：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

```powershell
cd frontend
npm install
npm run dev
```

打开终端打印的前端地址（默认 http://localhost:5173）。接口文档为 http://127.0.0.1:8000/docs。数据默认保存在 `backend/data/mindmaps.db`，已由 `.gitignore` 排除。

**不建 venv 也能跑**：本机装好依赖后，在 `backend` 目录直接 `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000` 即可（这份说明在 Python 3.13.5 + 全局依赖上验过，`/api/health` 返回 `{"status":"ok","version":"dev"}`）。本机的惯用入口是仓库根目录的 `run-backend.bat`（把全部环境变量配好并顺带拉起 Neo4j）与 `run-neo4j.bat` / `ensure-neo4j.bat`；`.gitignore` 里排除了 `run-backend.bat`，因为它装着真实密钥，**别提交它**。

后端测试：在 `backend` 目录执行 `pytest -q`（`pytest.ini` 已设 `pythonpath = .`，也可用 `python -m pytest`）；前端测试与构建检查：在 `frontend` 目录分别执行 `npm test` 和 `npm run build`。CI 配置见 `.github/workflows/ci.yml`，两个 job 分别对应这两组命令。

改了 `backend/app/contracts/` 里的任何字段后，要按这个顺序更新前端类型（漏了哪一步，`pytest`、`vue-tsc` 或 CI 会各报一处）：

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\export_openapi.py   # 更新 backend/openapi.json
cd ..\frontend
npm run gen:api                                        # 重新生成 src/api/schema.d.ts
```

不使用虚拟环境激活也可以用 `.\.venv\Scripts\python -m pip ...` 和 `.\.venv\Scripts\python -m uvicorn ...`。如果 PowerShell 限制脚本执行，可使用后一种方式。

### 配置真实模型（可选）

在**启动后端的同一个 PowerShell 窗口**设置以下变量并启动服务；接口必须兼容 `/chat/completions`，而且模型能够按提示输出 JSON：

```powershell
$env:MINDMAP_LLM_MODE = 'model'
$env:MINDMAP_LLM_BASE_URL = 'https://你的服务商地址/v1'
$env:MINDMAP_LLM_MODEL = '你的模型名称'
$env:MINDMAP_LLM_API_KEY = '你的密钥'
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

请以供应商实际文档为准，不同供应商的 URL、参数、区域和模型能力可能不同。不要把密钥提交进仓库；`backend/.env.example` 仅为变量清单，程序不会自动读取 `.env`。切回演示模式：`$env:MINDMAP_LLM_MODE = 'demo'`。调用真实模型会产生外部请求和可能的费用，避免上传未经授权的资料。

### 另外几个可选变量

| 变量 | 用途 |
|---|---|
| `DASHSCOPE_API_KEY` | 语音转写（百炼 Qwen-Audio-ASR），没配时 `.wav` 上传返回 422 |
| `MINDMAP_SOFFICE_CMD` | 指到 LibreOffice 的 `soffice` 路径，处理 `.doc`/`.ppt`/`.xls` 之外的老格式；不配则从 `PATH` 里找 |
| `MINDMAP_AGENT_ENABLED` | 设为 `0` 关闭 `/api/agent/runs/stream` |
| `MINDMAP_DB_PATH` | SQLite 文件位置，默认 `backend/data/mindmaps.db` |
| `MINDMAP_NLI_ENABLED`、`MINDMAP_NLI_MODEL_DIR`、`MINDMAP_NLI_MIN_FREE_MB` | 可选的引用语义校验（ONNX 模型），不配时只做机械核验 |

PDF 视觉识别复用上面的 `MINDMAP_LLM_*`，并要求 `MINDMAP_LLM_MODE=model`；`demo` 模式下 PDF 只回落到文本层，扫描件会返回 422。

## 团队从这里开始

1. RPeGio 与陈嘉涛先共同阅读 [系统架构](docs/ARCHITECTURE.md)、[API 契约](docs/API.md) 和 [CONTRACT_V2](docs/CONTRACT_V2.md)，固定导图 JSON 格式；契约改动必须经 RPeGio 与陈嘉涛双人 review 并附迁移脚本。
2. 分工按 [执行计划第 11 节](docs/PLAN.md)：RPeGio 负责契约 / Vue 画布 / 布局主题导出 / 部署与 CI，陈嘉涛负责后端地基 / 生成链路 / 多模态解析 / RAG / 诊断。任务与依赖仍以 [功能清单与分工](docs/FEATURES_AND_OWNERS.md) 的 F 编号为准。
3. RPeGio 与陈嘉涛先在本人电脑跑通生成（R1）→ 画布（R2）→ 编辑与保存（R3）整条链路，再按功能清单实现剩余能力。
4. 分支：`main` ← `dev` ← `feat/*`·`fix/*`，**PR 目标一律 `dev`**，提交信息用中文 Conventional Commits，PR 模板三问必填；**`main` 是生产分支**（ECS 上跑的就是它），`dev` 是集成分支，里程碑时 `dev` → `main` 合入并在 `main` 上打 tag（`v0.4.0-deploy` 起）。CI 必须绿才能合并。
