# 项目执行计划 v1.4

> 定稿 2026-11-02 · 答辩 2026-12-01 · RPeGio 与陈嘉涛 · 各 20–25 小时/周
> **冲突时以本文件为准**，并回写 `README.md`（申报书整理版只保留叙述，不再承载技术栈承诺）。

## 0 一页摘要

| 项 | 结论 |
|---|---|
| 验收对象 | 学校 Web 社团复试：① 讲清代码逻辑 ② 独立部署运行（含数据库与基建）③ GitHub 留档 ④ 允许用 AI、严禁完全依赖 |
| 交付物 | 线上系统 + 本地演示环境 + 架构文档 + 5 分钟演示脚本 + PPT + 20 题自测 |
| 分工 | **A**（RPeGio）：契约、前端、画布/布局/主题/导出、协作客户端、部署基建、CI；**B**（陈嘉涛）：生成链路、多模态解析、RAG、诊断、代理、评估集、后端地基 |
| 交界 | 契约、SSE 协议、协作协议、上线联调 —— 改动必须双人 review |
| 前端 | Vue 3 + TypeScript + Vite + Pinia + Tailwind + `@vue-flow/core` + Vitest |
| 后端 | FastAPI + Pydantic v2 + httpx + `sqlite3` 直连；`pydantic-settings` / SQLAlchemy / Alembic 顺延到验收之后（Q35），当前配置经 `os.getenv` 读取，见第 10 节 |
| 数据 | PostgreSQL 16（compose 容器）+ pgvector（W3） |
| 协作 | Yjs + `y-websocket`（独立 Node 容器） |
| 部署 | 阿里云 ECS 2C2G / 40G ESSD / Alibaba Cloud Linux 3.2104 / 华南1深圳 / 无 GPU，`docker compose` 五容器，公网 `IP:8080` |
| 铁律 | 深度优先；每个功能能口头讲清数据流；不返回假成功；**11/22 功能冻结** |

## 1 考核点 → 硬门

| 考核点 | 落地动作 | 面试证据 |
|---|---|---|
| 讲清代码逻辑 | 每个 PR 附「讲解稿」：做什么 / 为什么这么写 / 边界与失败路径；每周互讲 30 分钟；面试前 20 题抽问 | PR 历史 + 白板讲架构 |
| 独立部署运行 | Compose 五容器 + PG 迁移 + 备份脚本 + 健康探针 + 一键部署脚本 | 现场 `curl /api/health`，`docker compose ps` |
| 含数据库及基建 | PG 实例、Alembic 版本、每日 `pg_dump`、Nginx 反代、systemd/compose 自启、swap | 打开备份目录与迁移历史 |
| GitHub 留档 | `main` / `dev` / `feat/*`·`fix/*`，PR 双人 assign，Conventional Commits（中文），阶段 tag | 分支图 + tag 列表 |
| 不许完全依赖 AI | 讲解稿制度 + 抽问 + 每人对**另一侧**至少一个核心模块可讲 | 交叉抽问当场答 |

## 2 范围：L1 / L2 / L3

**L1（≈60% 工时，必须讲到实现细节）**

| 编号 | 内容 | 验收门 |
|---|---|---|
| — | 契约 v2 + 迁移 + OpenAPI 类型生成 | 双端类型零手写 |
| F01 | 文本/关键词/段落 → 层次节点（真实模型） | 10 条中文样本人工通过 |
| F04 | 画布：增删改、拖拽、撤销重做、搜索定位、折叠 | 快捷键可用，操作有测试 |
| F09a | 自动布局：导图 / 逻辑结构 / 时间轴 | 三种可切换且稳定不重叠 |
| F10 | 主题：3 套预设 + 主色/字体/方向自定义，随导图保存 | 重开可恢复 |
| F20 | 导出：Markdown / PNG / SVG / PUML | 中文不乱码 |
| — | 保存/打开/乐观锁 409、未保存提醒 | 冲突场景脚本可复现 |
| F22 | 容器化部署 + 备份 + 健康检查 | 一键脚本从零起服务 |
| F23a | pytest + Vitest + CI 双端绿灯 | 覆盖率 ≥60% |
| F26 | 延迟埋点与 P50/P95 报告 | 报告含模型与并发说明 |
| — | 最小 JWT 登录 + 每用户 BYOK（Fernet 加密） | 密钥明文永不出服务端 |

**L2（≈30%，每项一条能跑通的演示链路，允许薄、不允许假）**

| 编号 | 内容 | 薄切片的边界 |
|---|---|---|
| F05 | PDF / DOCX / PPTX 导入 | **已实现（与原计划不同）**：PDF 不再是「只取文本层」，改为逐页渲染交模型视觉识别，扫描件同样适用，视觉不可用时才回落文本层；页数由上下文预算动态算出（另有 300 页护栏），不再有固定页数上限。DOCX/PPTX/XLSX/HTML/MD 走 Docling，`.doc`/`.ppt` 经 LibreOffice 转 PDF，`.xls` 用 xlrd 直读。见 [部署文档](DEPLOY.md) |
| F06 | 图片/草图 → 导图 | **已实现基础链路**：单图经 `MINDMAP_LLM_MODEL` 多模态直转 Markdown（与 PDF 逐页共用调用），不收 SVG；「识别结果先给候选、人工修正」未做 |
| F07 | 语音 → 导图 | **已实现（与原计划两度不同）**：先放弃 sidecar 改走云端，再把 ISI 一句话识别换成百炼 `qwen-audio-3.1-asr-flash`。当前能力：≤300 秒单次、句级时间戳、双声道分路识别后合并。见 [部署文档](DEPLOY.md) 第 6 节 |
| F08 | 混合输入（文本+图+音） | 复用 `Source[]` 合并，保留来源标签 |
| F03 | 会议纪要/碎片文本整理 | 专用 prompt + 2 个测试 |
| F02 | 目标驱动框架生成（AI 代理） | 四阶段可见，产出导图，不承诺多轮代理 |
| F12 | 上传自己的文档当知识库 | 单用户、单知识库、手动重建索引 |
| F13 | RAG 带引用 | pgvector；节点级 `source_refs`；不宣称"事实验证" |
| F15 | 结构诊断 + 建议确认 | 3 条规则 + 1 次 LLM 检查；建议必须用户确认 |
| F16 | 跨节点关系建议 | 只产出 `relations` 候选，不自动改树 |
| F17 | 实时协作 | 单房间、两角色、在线同步；离线恢复不做（Q36：保留，排期延后） |
| F18 | 协作中的 AI 汇总 | 房间消息 → 一次性总结，不自动改图 |
| F19 | 翻译 | 选中节点单次翻译，不承诺"实时" |
| F11 | 偏好学习 | 只记录最近使用的主题/布局并给推荐按钮 |
| F24/F25 | 评估口径与首轮报告 | 见第 5 节 |

**L3（≈10%，只写文档与 PPT，明确标注"未实现"）**
F09 鱼骨图、F14 知识图谱嵌入对比实验、F21 模型微调、F22 Kubernetes、F23 压力测试集群、F19 多语言实时同传。

> 规则：L3 项在系统内**不出现入口**（不返回占位数据），只在文档与 PPT 的"路线图"页出现。

## 3 技术架构定稿

```
浏览器 (Vue3 SPA)
  ├── REST /api/*  ──────────────►  api (FastAPI, uvicorn)
  ├── SSE /api/generation-tasks/{id}/events ─┘
  └── WS  ──►  ys (y-websocket, Node)  ──► 快照落盘 + PG
        api ──► PG16 (业务数据 / 用户 / llm_calls / 知识库向量)
        api ──► asr (sidecar: moss | aliyun | faster-whisper)
        api ──► DeepSeek (deepseek-flash: 文本 + 视觉)
        nginx ──► 静态文件 + 反向代理 + TLS（W4）
```

**关键取舍（答辩要能讲）**

1. **生成改单次流式**：一次模型调用，边收 token 边解析 JSON，节点逐个推送（首节点 1–2 秒、完整 2–3 秒）。曾在「两段式（先骨架再展开）」与「单次增量」之间做过对比，最终保留单次增量：调用次数减半、代码与测试都更简单。代价是中途失败没有「保留骨架」的降级结果。详见 [ARCHITECTURE_ADDENDUM.md](ARCHITECTURE_ADDENDUM.md)。
2. **存储换 PG 不再保留 SQLite 双轨**：部署要求"含数据库"，协作快照、`llm_calls`、pgvector 都要真库；双轨会让测试与迁移分叉。开发与 CI 都用 compose 起的 PG，单测用事务回滚隔离。
3. **协作必须真 CRDT**：现有乐观锁只能防覆盖，无法合并并发编辑。Yjs 单房间 + 房间 token（api 签发）+ 30s 快照。映射层（`MindMapContent` ↔ `Y.Map`）自研，这是能讲清的技术点。
4. **视觉直接用 `deepseek-flash`，不引入第二家**：该模型原生支持图片输入，三种传法（base64 内联 / 外链 / Files API `file_id`），单图上限 32 MiB（base64）或 64 MiB（file_id），请求体 48 MiB，每图最多计 1024 token，且图片只能出现在 `user` 消息中。我们走**前端压缩到 1600px → base64 内联**，草图为一次性输入，不需要 Files API 复用。
5. ~~**ASR 做成可插拔 sidecar**~~ **（已推翻，2026-11-04；后续又换过一次实现）**：原计划 `moss`（默认，带说话人分离）/ `aliyun`（线上备选）/ `faster-whisper`（降级档 + 评测基线），线上 2C2G 不常驻 MOSS 模型。第一次实测后排除了本地方案——`sherpa-onnx` paraformer-zh-small 在 2C2G 上常驻 143 MB、短音频 RTF 0.013，但架构吃不下长输入（33.8 秒整段 CER 86.4%，60 秒直接返回空串并抛 onnxruntime Reshape 错误）。第二次实测又把云端接口从 ISI 一句话识别换成百炼 Qwen-Audio-ASR：后者自带句级/词级时间戳、60 秒音频快 2.8 倍、便宜 3.9 倍、单次时长从 60 秒放宽到 300 秒（见 v1.7）。多后端抽象始终没有价值——每次都只有一条路能跑通，留着切换开关等于维护几个用不上的分支。数字见 [部署文档](DEPLOY.md) 第 6 节。
6. **不做**：Celery/Redis、K8s、微服务拆分、Docling、本地 LLM、多租户 RBAC、OSS/CDN。

**2C2G 内存预算（含 swap 2G）**

| 容器 | 常驻估算 |
|---|---|
| 系统 + Docker | 300 MB |
| postgres | 180 MB（`shared_buffers=128MB`、`work_mem=4MB`） |
| api（1 worker） | 200 MB |
| ys（Node） | 90 MB |
| nginx | 20 MB |
| asr | **默认不启动**（compose profile `asr`） |
| 合计 | ≈790 MB，余 1.2 GB 给突发 |

## 4 四周里程碑

| 周 | 交付物 | 周五验收门（能演示的东西） |
|---|---|---|
| **W1** 11/2–11/8 | 契约 v2 + 迁移 + OpenAPI 类型生成；Vue 前端重建；单次流式生成（SSE + 增量解析）+ 真实模型；后端地基（settings/httpx/pytest/alembic/错误体/日志/request_id）；CI；Compose 骨架；**IP:8080 上线空壳**；四份文档同步改 | 浏览器里输入一句话 → 画布上逐节点长出导图，刷新后能打开 |
| **W2** 11/9–11/15 | 三布局；主题；四格式导出；PDF/DOCX/PPTX 导入；JWT + BYOK（画布编辑与撤销重做已由 R3 在 11/4 提前完成，本行不再占用 W2 工时） | 导入一份 PDF 出导图；同一导图在另一窗口打开后保存会提示版本冲突（409） |
| **W3** 11/16–11/22 | 图片草图；语音转写接入（原计划 sidecar，实际走云端 API）；RAG + pgvector + 节点引用；诊断/建议；AI 代理；翻译；评估脚本与首轮报告；（余量）Yjs 两窗口协作、PG 迁移 + 备份（Q35/Q36） | 上传一张手绘草图 → 导图；问知识库一个问题 → 导图节点带引用 |
| **W4** 11/23–11/29 | **功能冻结（11/22 24:00）**；Bug 修复；演示脚本彩排；20 题抽问；PPT + ADR + README 收尾；域名/CF 切换（可选）；`v1.0.0` tag | 5 分钟完整演示不卡壳，全部本地 + 线上双跑 |
| **缓冲** 11/30–12/1 | 只修 P0；现场环境预热；静态截图与录屏备份 | 断网也能讲完 |

每周节奏：周一 30 分钟对齐（本周目标/风险）；周三 15 分钟同步；周五 30 分钟互讲 + 合并 `dev`；周日 `dev` → `main` 并打 tag。

**上线节奏（Q32 定）**：W1 只上 `IP:8080`（安全组放通 22 + 8080），日常开发与答辩演示都以"公网 IP 在校园网可达"为准；域名 + Cloudflare 推迟到 W3 视备案进度，W4 仍作可选项。W1 不做 TLS，nginx 先只做静态与反代。

## 5 验收门与指标口径

**工程门（12 条，全部可勾选）**
1. 后端覆盖率 ≥60%（pytest + `--cov`） 2. 前端组件测试覆盖关键交互 3. CI 双端 lint/test/build 全绿 4. 统一错误体 `{code, message, detail, request_id}` 5. 结构化日志 + `request_id` + `llm_calls` 埋点 6. 单一配置源 + `.env.example`（当前经 `os.getenv` 读 5 个 `MINDMAP_*` 变量，样例文件已齐；`pydantic-settings` 随 Q35 顺延） 7. `docker compose up -d` 一键起 8. `/api/health` + readiness 9. ADR 每决策一页 10. PR 模板 + 契约变更 checklist 11. 种子数据脚本 12. 部署与回滚文档

**指标口径**
- **结构合理性 >85%**：20 条中文样本（课程大纲 / 会议纪要 / 项目立项），双人独立盲评 4 维度（覆盖、层次、无捏造、可读），报一致率与分歧，通过率 = 双人一致判"合格"的比例。
- **多模态支持率 ≥90%**：文本 / 图片 / 语音各 30 个样本；"成功" = 解析出非空 `Source` **且** 生成合法 `MindMapContent`。
- **响应时间 <3s / <5s**：分开报**首节点延迟**与**完整导图延迟**的 P50/P95，注明模型、并发数、是否流式；不承诺绝对值（申报书两处目标本身矛盾，已在 `docs/FEATURES_AND_OWNERS.md` F26 标注）。

## 6 风险表

| ID | 风险 | 概率 | 影响 | 触发信号 | 预案 |
|---|---|---|---|---|---|
| R1 | 域名备案 7–20 天不可控 | 高 | 中 | 11/15 未过审 | 全程 IP:8080 演示，域名只作 PPT 里的"已备案/已解析"截图 |
| R2 | CF 代理国内访问波动 | 中 | 中 | 首包 >1s 或超时 | 演示脚本默认走 IP；CF 版仅作公开展示 |
| R3 | 2C2G 内存吃紧（PG+api+ys） | 中 | 高 | OOM killer / swap 抖动 | swap 2G；PG 调参；asr 不常驻；必要时把 ys 与 api 合并进程 |
| R4 | 语音质量/时长不达预期 | 中 | 中 | 单次 ≤300 秒，超出返回 400 | 已放宽到 300 秒（原计划 60 秒）；不用本地模型，无 MOSS 依赖 |
| R5 | 草图识别质量差 | 中 | 中 | 20 张样本合格率 <60% | 改为"草图 → 候选节点列表，用户勾选后生成"，产品上更诚实 |
| R6 | Vue 重建拖到 W2 | 中 | 高 | 11/6 画布不能拖 | 砍主题自定义与鱼骨，先保编辑与保存 |
| R7 | Yjs 映射层比预期复杂 | 中 | 中 | 11/13 两窗口仍不同步 | 降级为"在线状态 + 最后写入者胜 + 冲突提示"，并在文档写明 |
| R8 | B 侧 RAG/代理进度 | 中 | 中 | 11/18 未跑通 | 降 L3 只讲不做，保 F01/F05/F06/F15 |
| R9 | 模型结构化输出不稳定 | 中 | 中 | 校验失败率 >20% | 原生 JSON Schema 优先 + 提示词回退 + 一次修复重试 |
| R10 | 整体滑期 | 中 | 高 | 任一周五验收门未过 | **周末补**，不从 W4 缓冲借；连续两周滑则砍 L2 后三项 |
| R11 | 密钥/额度被刷 | 低 | 高 | 异常调用量 | BYOK + 每用户日限 200 次 + 演示账号单独限额 |
| R12 | 契约变更冲突 | 中 | 中 | 同一文件双改 | 契约文件 CODEOWNERS 双人、变更走 PR + 迁移脚本 |

## 7 协作流程

- **分支**：`main`（保护，仅 PR 合入）← `dev`（集成分支）← `feat/*`、`fix/*`。`dev` 阶段性合入 `main` 并打 tag（`v0.1.0`、`v0.2.0`、`v1.0.0`）。
- **提交**：中文 Conventional Commits（`feat(canvas): 支持节点拖拽`），单提交 ≤ 半天工作量，禁止 `--force`。
- **PR**：模板三问必填——「这段代码做什么 / 为什么这么写 / 我怎么验证的」；**必须另一人 review 并 assign 后**才可合并；契约相关 PR 追加迁移脚本与双端类型生成结果。
- **互讲**：每周五 30 分钟，讲的人不得照读代码；听的人负责在 20 题清单里勾掉对应问题。
- **文档**：改契约 → 同步 `docs/CONTRACT_V2.md` 与 `frontend` 生成类型；改流程 → 改本文件并升版本号。

## 8 5 分钟演示脚本

| 时间 | 动作 | 讲到什么 |
|---|---|---|
| 0:00–0:30 | 打开线上地址，登录 | 架构一句话 + 部署形态（compose / PG / IP） |
| 0:30–1:10 | 输入「帮大二学生规划一学期机器学习学习路线」 | 流式生成：SSE 逐节点长出来，首节点 1–2 秒、整棵树 2–3 秒 |
| 1:10–2:10 | 画布编辑：拖拽、折叠、撤销、切布局、换主题 | 数据结构 v2、布局算法（tidy tree）、undo 栈 |
| 2:10–2:50 | 上传一张手绘草图 → 生成；上传一份 PDF → 生成 | 多模态：视觉模型与文档解析、来源记录 |
| 2:50–3:30 | 开第二个窗口，实时协作；@AI 总结 | Yjs/CRDT 如何解冲突、房间 token |
| 3:30–4:10 | 知识库提问 → 节点带引用；点"结构诊断" → 确认建议 | RAG 检索链路、建议必须用户确认 |
| 4:10–4:40 | 导出 PNG/Markdown/PUML | 导出一致性 |
| 4:40–5:00 | 切到 GitHub：分支图、tag、CI 绿灯、PR 讲解稿 | 留档与工程纪律 |

## 9 明确不做（评委问起就这么答）

Docker Swarm/K8s、多租户与权限矩阵、离线协作与冲突合并 UI、模型微调训练、知识图谱嵌入落地、压测集群、i18n、移动端适配、支付与配额计费、对象存储/CDN。

## 10 配置项

### 10.1 已实现（代码经 `os.getenv` 读取）

`.env` 文件不会自动加载，须在启动服务前设入进程环境变量；样例见 `backend/.env.example`。下表只列主要项，检索链路与图存储的几个开关（`MINDMAP_PARENT_RETRIEVAL`、`MINDMAP_GRAPH_SEARCH`、`MINDMAP_EMBEDDING_MODEL`、`MINDMAP_EMBEDDING_DIM`、`MINDMAP_NEO4J_URI/USER/PASSWORD/DATABASE`）和 `MINDMAP_VERSION` 见 `.env.example` 与 `docs/RAG-DECISIONS.md`。

| 变量 | 默认 | 说明 |
|---|---|---|
| `MINDMAP_LLM_MODE` | `demo` | `demo` 不联网、返回本地占位；`model` 才调 Chat Completions 兼容接口。写成其他值会被拒 |
| `MINDMAP_LLM_BASE_URL` | — | 模型服务地址，须以 `http://` 或 `https://` 开头 |
| `MINDMAP_LLM_API_KEY` | — | 调用密钥，只从环境变量读，不进代码库与日志 |
| `MINDMAP_LLM_MODEL` | — | 模型名，由服务商决定。当前线上用 `z-ai/glm-5.3-flash`（端点 `https://api.commandcode.ai/provider/v1`），此前用 `deepseek-flash`。PDF 视觉识别与图片识别复用同一组变量 |
| `MINDMAP_DB_PATH` | `backend/data/mindmaps.db` | SQLite 文件路径，留空即取默认值 |
| `DASHSCOPE_API_KEY` | — | 百炼语音转写（F07）。没配时 `.wav` 上传返回 422 |
| `MINDMAP_SOFFICE_CMD` | — | `soffice` 完整路径；不配则从 `PATH` 找。`.doc`/`.ppt` 转换需要 |
| `MINDMAP_AGENT_ENABLED` | `1` | 设为 `0` 关闭 `/api/agent/runs/stream` |
| `MINDMAP_NLI_ENABLED` | `0` | 打开引用语义复核（ONNX 模型），否则只做机械核验 |
| `MINDMAP_NLI_MODEL_DIR` | — | ONNX 模型目录（含 `model.onnx` / `tokenizer.json` / `config.json`） |
| `MINDMAP_NLI_MIN_FREE_MB` | — | 加载模型前要求的最小可用内存，不够则跳过 NLI |
| `MINDMAP_LIBRARY_SEARCH` | `1` | 生成时要不要从本地知识库捞材料。线上置 `0` 做纯展示（等记忆模块落地再开）；**不影响** `GET /api/knowledge/library` 只读入口 |
| `MINDMAP_LIBRARY_REMEMBER` | `1` | 生成时把用到的材料收进知识库。只有 `"0"` 表示关。与上面那个开关独立 |

### 10.2 规划中（当前代码不读这些变量）

| 变量 | 依赖功能 | 状态 |
|---|---|---|
| `APP_ENV` | 区分 dev / prod | 未排期 |
| `DATABASE_URL` | PostgreSQL 迁移 | Q35：顺延到验收之后 |
| `JWT_SECRET` / `JWT_TTL_HOURS` / `FERNET_KEY` / `DAILY_LLM_QUOTA` | 登录签发、BYOK 加密、每日配额 | F12 为单用户单库，暂不需要 |
| `ASR_BACKEND` / `ASR_URL` / `ASR_MAX_SECONDS` / `ASR_WHISPER_MODEL` / `ASR_WHISPER_COMPUTE` | 语音转写（F07） | **已作废**。原计划的多后端（MOSS / faster-whisper / aliyun）没有落地。实际语音只用一个变量 `DASHSCOPE_API_KEY`（阿里云百炼），见 [部署文档](DEPLOY.md) 第 6 节。选型理由：本地方案在 2C2G 上吃不下长输入（33.8 秒整段 CER 86.4%），云端接口则自带时间戳且更便宜 |

### 10.3 不是环境变量，是代码常量

改动这两项要改代码并重新部署：

| 常量 | 位置 | 当前值 |
|---|---|---|
| `MAX_UPLOAD_BYTES` | `backend/app/ingestion/router.py` | 10 MB（文档） |
| `MAX_AUDIO_BYTES` | 同上；前端 `stores/mindmap.ts` 同口径 | 20 MB（WAV，约 11 分钟） |
| `MAX_SLICE_SECONDS` | `backend/app/ingestion/asr.py` | 300（Qwen-Audio-ASR 服务端硬限，360 秒返回 `AUDIO_DURATION_TOO_LONG`） |

## 11 决策记录（Q26–Q36）

| # | 决策 | 理由 / 影响 |
|---|---|---|
| Q27 | ~~ASR 默认 MOSS，`faster-whisper` small-int8 为降级档~~ **已推翻**：改用云端 API，不做 sidecar 也不保留多后端开关。云端具体用哪家后来又换过一次（ISI 一句话识别 → 百炼 Qwen-Audio-ASR，见 v1.7） | 原理由是"2C2G 上 large-v3 不可用"。实测后更彻底：本地方案连长一点都吃不下——33.8 秒整段 CER 86.4%、60 秒直接返回空串，只能处理 10 秒以内片段。只有一条路能跑通时，可插拔抽象只剩维护成本。见第 4 节第 5 条与 [部署文档](DEPLOY.md) 第 6 节 |
| Q28 | 全部服务跑在同一台 ECS，`IP:8080` 暴露 | 2C2G 无法拆机；单机 compose 也最符合"独立部署"的考核表述 |
| Q29 | BYOK：用户自带 key、服务端 Fernet 加密存库、每用户每日 200 次、前端不落明文、演示账号单独限额 | 密钥不进代码库、不进日志；额度与滥用风险由 R11 兜底 |
| Q30 | `main` ← `dev` ← `feat/*`·`fix/*`；PR 目标一律 `dev` | `dev` 已从 `main` 拉出（空提交起点），`docs/project-plan` 的 PR 目标改 `dev` |
| Q31 | A：契约 / Vue 画布 / 布局主题导出 / 协作客户端 / 部署基建 / CI；B：后端地基 / 生成链路 / 多模态解析 / RAG / 诊断 / 代理 / 评估集 | W1 两条线并行：B 做 D1–D3，A 做 D2 契约 + D4 画布 + D5 部署，W2 起交叉 review |
| Q32 | W1 只上 `IP:8080`，域名与 CF 推迟 W3 | 备案不可控（R1）；验收只看"能独立部署并访问"，TLS 不是硬门 |
| Q33 | 前端换 Vue 生态（陈嘉涛已同意）；**先做 D4**（R1 骨架、R2 画布、R3 编辑保存） | 前端负责人只熟 Vue；R1 已完成（PR #3），画布是答辩主视觉，优先于后端地基 |
| Q34 | 双人 review **仅**在 fork → 上游 PR 时强制；本仓库 dev 分支的 PR 由提交人自己 review 后合并 | 仓库是 fork，日常产出先落在自己仓库；向上游提 PR 时再走 `CODEOWNERS`，由 RPeGio 与陈嘉涛互相 assign |
| Q35 | **后端地基压缩**：采纳陈嘉涛在 `NEXT-PLAN.md` 的建议，W1 只做「统一错误体 + `urllib`→`httpx`」两项；SQLAlchemy / Alembic / PostgreSQL 迁移顺延到**验收之后**，v1 继续 `sqlite3` 直连 | RPeGio 2026-11-03 确认。理由：契约 v2 与 ORM 迁移无法在 W2 前同时装进两地排期；错误体与 httpx 已由陈嘉涛在 `027b0c9` 落地。影响：第 3 节架构图中的 PG16、第 5 节验收门第 7 条（compose 五容器含 `db`）同批顺延到验收后，W1–W3 以 SQLite 单文件 + 备份脚本兑现"含数据库"的部署要求 |
| Q36 | **实时协作（F17）保留，但排期延后**：不设 W2 硬门，W3 有余量再做，无余量则降级为文档说明 | 陈嘉涛倾向实现协作，RPeGio 同意保留该功能（`NEXT-PLAN.md` 原提议"W2 不做协作"不再成立）；协作依赖画布稳定与 save/undo 语义，先做完 R2–R3 再评估余量，避免两条线并行时相互阻塞 |
| Q37 | **`main` 转为生产分支**：`main` ← `dev` ← `feat/*`；里程碑时 `dev` → `main` 合入并在 `main` 上打 tag，部署与回滚都以 tag 为准，ECS 只跟 `main` | 核心功能（生成、画布、编辑保存、多来源、引用、导出）已打通，需要一个稳定的"线上是什么"的答案；此前 main 与 dev 长期同步推进，无法区分"能演示"与"正在改"。`dev` 保留为集成分支，未验证完的改动不落到生产 |

## 12 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-11-02 | 首版定稿（基于四轮需求澄清，见 PR 描述） |
| v1.1 | 2026-11-02 | 补 Q27–Q32 决策、ASR 降级档与 `ASR_WHISPER_*` 配置、上线节奏（W1 仅 IP:8080） |
| v1.2 | 2026-11-02 | 补 Q33（Vue 生态 + 先做 D4）/ Q34（review 边界）；R1 已完成，R2–R3 进行中 |
| v1.3 | 2026-11-03 | 补 Q35（后端地基压缩：错误体 + httpx，ORM/PG 顺延验收后）/ Q36（实时协作保留但延后，W2 去掉 Yjs 硬门）；分工与决策行改具名（RPeGio / 陈嘉涛），不再用"队友""双方" |
| v1.4 | 2026-11-04 | R3 完成：画布编辑、撤销重做（整树快照 50 步）、保存与乐观锁 409、打开列表落地，W2 相应工时腾出；前端用例 81；新节点正文复用 v2 `Node.note`（H1，待陈嘉涛在 PR 审查确认） |
| v1.5 | 2026-11-04 | F07 语音落地，推翻 Q27：放弃 MOSS / faster-whisper 与 sidecar 方案，改走云端。补 `MAX_AUDIO_BYTES` 与 `MAX_SLICE_SECONDS` 常量；前端录音受限于安全上下文，与 Q32「IP:8080 不做 TLS」冲突，已记入架构文档 |
| v1.6 | 2026-11-05 | F06 图片输入落地；F08 混合输入与节点级引用（三层校验 + 可选 NLI）；F20 三种导出接通前端 |
| v1.7 | 2026-11-05 | 语音接口二次切换：ISI 一句话识别 → 百炼 `qwen-audio-3.1-asr-flash`。换来句级时间戳、300 秒单次上限、60 秒音频快 2.8 倍且便宜 3.9 倍；依赖 `websockets` 与 `aliyun-python-sdk-core` 一并移除，`MAX_SLICE_SECONDS` 60 → 300 |
| v1.8 | 2026-11-06 | 生成链路 Agent 化：受限 tool-calling 循环（四个工具、`RunBudget` 预算）、op 契约与两端共读用例、SSE v2 事件、前端候选确认（`docs/AGENT_PLAN.md`）。`/api/agent/tasks` 仍是桩；F02/F03/F16 转「部分实现」 |
| v1.9 | 2026-11-07 | 文档对账：功能清单加实现日期、能力与端点按代码现状重写（当时 19 个端点、无 501 路由、`source_refs` 属 v1）、配置项补 6 个变量；`NEXT-PLAN.md` 加日期同步说明 |
| v1.10 | 2026-11-07 | 节点来源引用进界面（F08，`fc8f82f`）：画布角标显示条数、详情面板列来源与位置、右侧抽屉看原文并高亮命中句；原文不落库，跨会话只剩 `quote`。「每个来源被几个节点引用」的反向索引放 P2 |
| v1.11 | 2026-11-08 | 引用反向索引（P2）：左侧来源行标注被几个节点引用、内联节点列表可跳转、未引用来源标 amber；撤销栈改按 10MB 字节上限（节点上限提到 1000 后只按步数会失控）；新增 docs/TODO.md 记录文件占用，避免两人同改 stores/mindmap.ts |
| v1.12 | 2026-11-10 | 契约 v2 在后端落地（P3 第一步）：`schema_version`、`note`、`layout`（新增 `vertical`）、`theme`、`relations`、`style`/`pos`/`collapsed` 进 `backend/app/contracts/mindmap.py`；旧记录读时补默认值（`migrate_content()`），不跑一次性脚本；导出改取 `note or label`；v1 请求体继续接受 |
| v1.13 | 2026-11-10 | 契约 v2 前端接线（P3 第二步）：详情面板可直接编辑节点正文（`note`，≤2000 字），保存 / 打开 / 导出都按 v2 发，`toContractV1()` 与 `toExportV1()` 删除；朝向（`layout` + `theme.direction`）随内容持久化，重新打开能恢复原布局，向下对应 `layout: vertical`；`relations` / `style` / `pos` / `collapsed` 原样透传不丢。H1 结案（陈嘉涛已在 PR #13 认可） |
| v1.14 | 2026-11-10 | 契约类型生成与漂移防线：新增 `backend/scripts/export_openapi.py` 导出 `backend/openapi.json` 快照，`npm run gen:api` 用 openapi-typescript 生成 `frontend/src/api/schema.d.ts`；手写镜像保留，但由 `frontend/src/api/contract-check.ts` 在 `vue-tsc` 期间断言与生成类型一致（键集合 + 抹平可空性后的形状）；`pytest` 增快照过期检查、CI 两个 job 各加"重新生成 + `git diff --exit-code`" |
| v1.15 | 2026-11-10 | ECS 上线骨架（F22 部分实现）：`deploy/install.sh`（Alibaba Cloud Linux 3：dnf 依赖 → swap → `genai` 服务账号与目录 → uv 装 CPython 3.12 与依赖 → systemd → nginx 站点 + Basic Auth + 每日备份 cron）、`deploy/genai-mindmap.service`、`deploy/nginx.conf`（同源反代、SSE 关缓冲、上传 24m、读超时 900s）、`deploy/backup.sh`（`sqlite .backup` 而非 `cp`）、`docs/DEPLOY.md` 第 9、10 节。仍然不做 Docker/compose |
| v1.16 | 2026-11-11 | `main` 转为生产分支（`Q37`）：里程碑 `dev` → `main` + 打 tag，ECS 只跟 `main`；`docs/DEPLOY.md` 第 9.1、10 节按此改写（生产 clone 默认分支、更新走 tag、回滚步骤与契约回滚的数据风险）；`START_HERE.md` 分支规则同步 |
| v1.17 | 2026-11-11 | 部署骨架按交叉复核修八处：日志目录交给 systemd `LogsDirectory`（原 `ReadWritePaths` 指向不存在目录会 226/NAMESPACE）；`EnvironmentFile` 去掉 `-`，缺文件即启动失败而非静默回落只读路径；`TimeoutStopSec` 20 → 660 对齐 600 秒生成预算；nginx 的 `listen [::]:80` 改为按 IPv6 可用性生成（`nginx -t` 查不出 bind 失败）；`/opt` 下 dist 补 SELinux 上下文 `httpd_sys_content_t`；certbot 改 `certonly --webroot`，443 由 `deploy/nginx-tls.conf` 渲染，重跑安装脚本不再冲掉 TLS；覆盖前把代码快照到 `/var/lib/genai-mindmap/releases/`，第 10 节回滚改为解包快照（`/opt` 不是 git 工作区，`git switch` 改不到在跑的代码）；`Q37` 登记进 `docs/HANDOFF.md` 的 H3 |
| v1.18 | 2026-11-11 | 依赖安装不再依赖公网：`deploy/setup-python.sh` 的 PyPI 源改成"公网阿里云 → ECS 内网镜像 → 官方"依次尝试（http 源自动带 `--trusted-host`），新增 `docs/DEPLOY.md` 第 11 节讲出网受限时的探测与离线安装；同时修掉 DEPLOY 章节号重复——原 ECS 两节顺延为第 9、10 节，交叉引用（`deploy/install.sh`、`START_HERE.md`、`TEAM-GUIDE.md`、`FEATURES_AND_OWNERS.md`）一并改指 |
| v1.19 | 2026-11-13 | 可观测性（线上排障被"看不见模型说了什么"卡住）：`/api/health` 的 `version` 改为读 `MINDMAP_VERSION`（缺省 `dev`），页面标题旁显示版本徽章；`done` 事件在没有产出候选时带 `text` / `reasoning`（各截断 4000 字），右侧"模型协作"面板直接显示模型原话与思考，不再依赖 `journalctl` 里那条未必出现的 WARNING；`deploy/install.sh` 新增 `--version` 参数（tar 包没有 `.git`，`git describe` 拿不到 tag） |
| v1.20 | 2026-11-13 | 部署自动化：新增 `.github/workflows/deploy.yml`，**打 tag 即发布**（构建前端 → `git archive` → scp → `install.sh --version <tag>` → 换 dist → reload → 用 `/api/health` 比对版本号自检，不一致让 job 红）；`docs/DEPLOY.md` 第 12 节写一次性配置（4 个 secret、部署专用 ed25519 密钥、22 端口必须公网可达的安全前提与 fail2ban）、日常发布与 `workflow_dispatch` 回滚。选择"Actions 推"而不是"服务器拉"的原因：这台 ECS 连不上 GitHub，self-hosted runner 也起不来 |
| v1.21 | 2026-11-14 | CD 换成零入方向端口：ECS 实测出站已恢复（`curl https://github.com` 与 `api.github.com` 均 200），于是放弃"Actions 直连 SSH"（那要求 22 对 `0.0.0.0/0` 开放，是全套部署里最大的敞口），改为在 ECS 上装 **self-hosted runner**（标签 `genai-ecs`），构建仍放 GitHub 托管机、artifact 交给 runner 安装。新增 `deploy/cd-apply.sh`（root 拥有的唯一特权入口，由 `install.sh` 装到 `/usr/local/sbin/genai-cd-apply`）与 `deploy/install-runner.sh`（取 runner 最新版、建 `ghrunner` 用户、注册服务、写单行 sudoers）；`deploy.yml` 改成 `build` + `deploy` 两个 job；不再需要 `ECS_HOST`/`ECS_USER`/`ECS_SSH_KEY` 三个 secret，只用可选仓库变量 `ECS_DOMAIN`；`docs/DEPLOY.md` 第 12 节重写（为什么改、一次性配置三步、排障表） |
| v1.22 | 2026-11-16 | 演示兜底：新增 `frontend/src/demo/presets.ts`（三张预置导图，打成独立 chunk 约 6.6 kB，生产也带）与右上角「演示示例」按钮，**只在用户点击时加载**——模型失败仍然如实报错，不自动顶替（否则演示现场分不清哪张是真生成的），载入后徽章显示「演示模式（非 AI 生成）」、`mapId` 为空所以第一次保存是新建。另修两处"材料不可用"的静默失败：`SOURCE_NOT_PARSED` 诊断（带了几份材料、几份没参与校验）与深层生成按 `source_usage` 过滤引用（此前整批 op 校验失败，白跑一轮模型） |
| v1.23 | 2026-11-16 | 换模型服务商：线上从 `api.deepseek.com` + `deepseek-flash` 换成 `https://api.commandcode.ai/provider/v1` + `z-ai/glm-5.3-flash`（官方 API 峰谷计费，峰时价格翻倍，压测烧钱）。代码零改动即可换家——端点只要满足 `tools` 真被调用、`stream: true` 出增量、`max_tokens` 有上限可学。`agent/provider.py` 的 `DeepSeekProvider` 改名 `OpenAICompatibleProvider`（类名带供应商名会让下一个人以为换家要改代码）；`docs/DEPLOY.md` 第 8 节补「换模型服务商」三条硬要求与换家后的自检清单。换家实测 3/3 出图（80 / 69 / 51 节点），带材料那条 14 条引用逐字对上原文 |
| v1.24 | 2026-11-16 | 长静默心跳：`/api/agent/runs/stream` 新增 `heartbeat` 帧（`{seq, type, waited_ms}`，静默每满 10 秒一帧），两个静默点都发——模型调用 `await` 期间与工具执行期间的等待队列；线上实测 3 路并发最长静默 25.1 秒，而中间链路常见空闲超时是 30–60 秒，超了就会静默掐断连接。前端把 `waitedMs` 接上（此前这个计数器没有任何写入方，`SourceInputs.vue` 的"已等待 N 秒"永不显示），静默达 10 秒时状态行文案换成"模型正在思考，已等 N 秒"（替掉阶段文案而非并列），任何非心跳帧到达即归零。心跳只保活、不带业务进度，EOF 无终态帧仍按连接中断处理 |
| v1.25 | 2026-11-16 | 知识库只读入口：新增 `GET /api/knowledge/library`（列表 + 每份被几张已保存导图引用）与 `GET /api/knowledge/library/{source_id}`（正文前 4000 字），顶栏「知识库」抽屉展示，点开看正文。**材料在生成时才入库**（新 `backend/app/knowledge/remember.py`，开关 `MINDMAP_LIBRARY_REMEMBER`）——上传时还不知道这份材料会不会被用到，废文件会占满库且 `prune` 按 `last_used_at` 保留会一直留着它们。入库后持久 id 与解析时的临时 id 不同，所以事件流最前面多一帧 `sources`（`{seq, type:"sources", mapping:{旧:新}}`），前端就地改本地条目 id；之后保存的导图、节点引用、取原文都用持久 id，重开也能对上。`cited_by` 按"几张导图"计（`source_usage` 与节点 `source_refs` 都算、同图去重），不按命中次数——用户要的是"这份材料还在被用"。入口只读且**不受 `MINDMAP_LIBRARY_SEARCH` 影响**：那个开关管的是生成时要不要从库里捞材料，线上先置 0 做纯展示，等记忆模块落地再开。另修 404 文案：`HTTPException(detail=...)` 会被全局处理器兜成"请求处理失败（HTTP 404）"，改成 `AppError("not_found", …)` 后顶层 `message` 才是真正原因 |
| v1.26 | 2026-11-19 | 合入上游 140 个提交（`aeed033..6674acb`）。**这次的方向是反的**：上游 `28cb730 merge: 合并协作者 dev` 已把本仓库的 `dev` 整体并进去，所以对 `main` 合上游零冲突、合并后的树哈希与上游完全一致（`270d2b7f…`）——本仓库此前所有工作（知识库入口、心跳、部署骨架、CI `fetch-depth` 修复）都在其中，不再是"我们追上游"。真正新增的是 `28cb730` 之后的 85 个提交：三种图型各自的契约与几何（`ContentBase` + `MindMapContent`(树) / `FishboneContent`(手写 60° 主骨) / `FlowContent`(dagre 分层)，`DiagramKind = tree\|flowchart\|fishbone`，六种节点形状）、生成链路改回一次生成并拆成四段（骨架 → 每支检索 → 结构 → 说明两次并发，实测 detail 142 字 / 38.8 秒 / 跨支重复 2，见 `docs/ONE-CALL.md`）、**候选层已删**（`acfdb5b`，生成结果直接落画布，`edit-log.ts` 的 `accept` 降级为历史字面量、接替者是 `generate`）、改动日志（`/api/mindmaps/{id}/edits` 三接口 + `docs/EDIT-LOG.md`）、记忆模块（`backend/app/memory/`，生成时注入相关记忆）、前端重构（`ChatDock` / `MessageFab` / `NodeToolbar` / `VoicePress` / `HistoryDrawer`，画布宽度 800 → 1440）。接口 25 路径/28 端点 → **30 路径 / 34 端点**。基线 1630+610 → **后端 2120（另有 29 subtest）、前端 900（55 文件）** |
| v1.27 | 2026-11-19 | 半棵树也是交付，失败要能操作。此前收尾有六条路径各写一份逻辑，其中**只有两条**按"有树就交树"处理（`_budget_stop` / `_no_progress_stop`），另外四条（工具超时、模型调用额度、连续失败、空转收尾）无条件发 `error`——而前端收到 `error` 会清预览、`content` 也不动，于是用户看着跑了三分钟，最后什么都没拿到，`RunContext.candidate` 里那大半棵树直接作废。现在六条统一走 `_deliver_or_fail()`：有候选就交 `done` 并标 `partial: true` + `partial_reason`，真没树才报错。`partial` 缺省 `None` 而非 `False`（`exclude_none=True`，正常帧与加字段前逐字节相同；缺字段=没说，与"明确完整"不是一回事，前端判 `=== true`），并一路落库（`runs` 表加 `partial`/`partial_reason`，`_ensure_columns` 补列）以便重放同形。另一个拆出来的码是 `model_not_configured`（`backend/app/generation/service.py` 的 `ModelNotConfigured(GenerationError)`，`_config()` 抛它）：它是**唯一一个重试一百次还是同一个结果**的失败，与上游 5xx/超时那类"重试有意义"的必须分开，否则界面只能给出一个骗人的按钮（`except` 顺序也必须排在父类之前，写反就退回成 `generation_failed`）。前端把 `error` 的原话从"直接当文案"改成失败卡片：标题 + 下一步 + 按钮，判据是纯函数 `frontend/src/mindmap/failures.ts::guideForFailure`（逐条有用例），**被禁用的按钮必须写明原因**（只置灰等于沉默，用户只能猜是自己点错了），后端原话与 `code=`/`limit=` 折进 `<details>` 给排障的人看；「重试」复用上一轮参数但**新开 run 身份**（沿用旧 id 会被后端按幂等判成已 done，把上一份失败原样重放回来）。基线 2120 → **后端 2145（另有 29 subtest）、前端 920（57 文件）** |
| v1.28 | 2026-11-20 | 状态保存第一层：**检查点落盘**。`partial` 只解决"收尾时手上还有树"，救不了**进程根本没走到收尾**的那一类——`runs` 的终态由 `_frames` 的 `finally` 写，进程被杀/机器重启时那个 `finally` 不会跑，库里留下一条永远 `running` 的行，而它已经攒了大半棵树却没有任何一帧带出去。现在在 `_generate` 的**第四步（写说明）之前**打一个点：那一刻树已过 `validate_generated_content`、已裁剪（`_clip_tree`）、骨架的颜色与边已套上，是**契约合法的完整树、只是 `detail` 全空**；而紧接着要发两次并发调用写说明，实测整轮最长的一段（56 节点 26 秒上下）。更早不行——结构响应流式解析的中途预览帧只有 `id`/`label`，写进库是 `model_validate` 过不了的树；更晚不必——`publish_tree()` 那一刻 `on_close` 兜得住。通道是 `RunContext.checkpoint`（`CheckpointFn`，同步回调，与 `emit` 同样默认 `None`，不传时行为与加它之前逐字相同）：工具层因此不知道"运行台账"这件事，`runs.checkpoint()` 只对 `status='running'` 生效（迟到的检查点不许把成品盖成半棵树），`finish()` 交出候选时清掉标记（`checkpointed_at` 有值 = 这是半路截的，成品带着它界面会一直提示"说明还没写"），写失败只 warning 不抛。另一半问题是崩溃留下的化石：那条 `running` 行会让同一个 `run_id` **永远 409**，而 `pending()` 只看终态、检查点存得再好也没人读 → `begin()` 在认领前先跑 `runs.sweep_abandoned()`，`updated_at` 超过 `RUN_ABANDONED_SECONDS`（1800 秒 = 线上默认 `max_seconds` 的两倍）的 `running` 判 `failed` + `failure.code="run_abandoned"`，**不动 candidate**；放在 `begin()` 而不是启动钩子，因为单 worker 的机器可能长期不重启，启动时清一次的话故障后第一批用户仍然撞 409，判错的代价也是自愈的（活着的运行收尾时 `finish` 仍能改）。读侧两个字段 `checkpointed_at` / `node_count`（只在检查点行上数一遍节点），`list_runs` 缺省状态加 `failed`（`run_abandoned` 就在里面）。前端 `resumableText` 多一档**排在 `partial` 之前**："上次生成在写说明之前断了，结构还在，已保存 N 个节点"——它不是半棵树，说成"只完成了一部分"会让用户以为要重跑；`resumeRun()` 记一条 `kind:'diagnostic'` 的 `CHECKPOINT` 告警而不是 `partial`。真实链路验证：假模型端点在写说明那一段睡 120 秒，读到一个心跳就断开 → 库里 `cancelled` + `checkpointed_at` + 10 节点、`detail` 全空；**强杀后端进程**（客户端连接保持打开）→ 留下 `running` + `checkpointed_at`，老化 `updated_at` 后下次 `begin()` 把它判成 `failed`/`run_abandoned`，列表里 `node_count=10`。基线 2145 → **后端 2171（另有 29 subtest）、前端 923（57 文件）** |
| v1.29 | 2026-11-20 | 从检查点继续：**补说明，不重跑**。检查点把"结构长好、说明空着"那一份救了下来，但用户载入之后仍然缺说明，而从前的唯一补救是重跑一次生成——再花三次模型调用、十几秒，还可能换出一棵不一样的树，把用户刚看着满意的那份结构顶掉。补的是独立一条路：新工具 `mindmap_fill_details`，只写 `detail`、结构一个字节都不动，复用现成的 `_fill_details`（拆两半并发、五条兜底、额度串行预占）而不是让模型拿 op 一条条改——80 个节点就是 80 条 op（`EditArgs.ops` 上限 100、`selected_ids` 上限 50），且模型会把 `DETAIL_ONLY_PROMPT` 那套调好的东西重写一遍、丢掉"少返回不替它编"这些兜底。五个决定：① **复用 `intent='edit'`**，不新增意图（它本来就要求带 `snapshot`、本来就是"在现有这棵树上改"）；② **沿用那条 `run_id`**——`begin()` 对 `failed`/`cancelled` 是重开同一行、清 `partial`、换掉 `request` 摘要、不动 `candidate`，补完的树落回同一行，台账上是同一次生成的下半场；换新 id 会让一次生成变成两次运行，而那一行若还在 `running`（进程没死、只是前端断过线）就该 409；③ **不给 `node_ids`**（要补哪些由 `_detail_targets` 按 `len(detail) < DETAIL_FILL_FLOOR` 数，模型看不到整棵树、让它圈只会漏）；④ **只加 `instruction`**；⑤ **不用 `_intake`**——它会在没材料时发起外部检索，等于给一棵按旧依据长出来的树塞新依据，改用 `_expand_sources` 按整棵树声称用过的 `source_refs` 从知识库把原文找回来，找不到就按无原文写。两条硬边界：非树给 `not_tree` 不崩在 `content.root` 上；`ctx.publish_tree()` 只在真补上节点时才调（那棵树与进来时逐字节相同，"产出过东西"若为真，之后某条额度耗尽的路径就会把"什么都没发生"包装成半成品交付）。前端不是新入口，而是挂在那条「上次没接住的结果」提示上：**那一行能被重开（`cancelled` / `failed`）时就出现**「补齐说明」（判据不用 `checkpointed_at`——那个戳在收尾交出候选时就被清，失败一次入口就永久消失），点它先载入检查点、再用同一条 `run_id` 发一轮 `edit`（`run_id` 必须在载入**之前**读——载入成功会清 `resumableRun`，之后再读只会拿到 null 然后悄悄新开身份）。新 `RunOptions.runId` 缺省 `undefined` = 老行为。基线 2171 → **后端 2187（另有 29 subtest）、前端 926（57 文件）** |
