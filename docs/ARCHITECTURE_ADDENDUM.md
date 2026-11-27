# 架构增量附录

本文**不重复** [系统架构](ARCHITECTURE.md) 的内容，只补三块它标为"规划中"但没给出可执行细节的部分：存储迁移、流式协议、生成流水线。

所有命名、路径、限制以现有代码为准：

- 节点字段是 `label`，不是 `topic`
- 接口前缀 `/api`，不是 `/api/v1`
- 上限 1000 节点 / 8 层 / 每节点 50 个子节点（本文写作时为 300，2026-11-06 起为提高生成宽度调到 1000）
- 唯一数据结构是 `MindMapContent`（`backend/app/contracts/mindmap.py`）

---

## 1 存储：从 SQLite 迁到 PostgreSQL

### 1.1 迁移时机

现状是 SQLite 单文件（`backend/data/mindmaps.db`），适合当前本地原型。出现下列需求时评估迁移：

- 需要多实例部署或并发写入
- 需要全文检索、向量检索
- 实测数据量、写入并发或查询性能超出需求
- 需要更完整的服务器端运维、恢复或部署能力

不存在适用于所有项目的固定记录数或文档大小迁移阈值；SQLite 也支持备份，具体取舍应以测试、运维需求和团队能力为准。

### 1.2 表设计

对齐现有 `MapRecord`（`id / version / content / updated_at`）：

```sql
create table mindmaps (
  id         uuid        primary key,
  version    integer     not null default 1,
  content    jsonb       not null,          -- MindMapContent 序列化
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index mindmaps_updated_at_idx on mindmaps (updated_at desc);

-- 只有在真要做"按节点文字搜导图"时才建
create index mindmaps_content_gin on mindmaps using gin (content jsonb_path_ops);
```

初期可不拆 nodes 表：现有契约把整棵树当作一个文档读写。节点级搜索、权限、引用或协作成熟后，再根据查询模式和实测结果决定是否拆表；不预先断言两种方案的性能差距。

### 1.3 乐观锁保持不变

现有语义是版本号比对，迁移后原样保留：

```sql
update mindmaps
   set content = $2, version = version + 1, updated_at = now()
 where id = $1 and version = $3;
-- 影响行数为 0 → 检查记录是否存在，区分 404 和 409
```

### 1.4 迁移步骤

1. `alembic init` + 建表迁移脚本
2. 写一次性导入脚本：读 SQLite 的 `MapRecord` → 批量写 PG
3. `backend/app/mindmaps/repository.py` 换实现，**对外接口签名不变**，路由层与测试不受影响
4. 双写观察一周，比对两库记录数与抽样内容
5. 确认迁移和回滚方案后切换；按备份保留策略保存旧库，避免立即删除唯一副本

`repository.py` 目前只有几百行且职责集中，这是迁移成本低的原因，也是前面建议"先别急着上 PG"的原因。

---

## 2 流式协议（SSE）

> **本章是设计稿，与当前实现有差异。** 落地版本是「单次调用 + 增量 JSON 解析」，
> 事件用 `data:` 里带 `type` 字段（不用 `event:` 行），字段名是 `content` / `latencyMs`
> 而不是这里的 `mapId` / `nodeCount`，代码见 `backend/app/generation/stream_partial.py`。
> 本章保留的价值是记录「任务化 + 断线重放」这条演进路线，尚未实现。

对应 [API 契约](API.md) 里规划的 `GET /api/generation-tasks/{id}/events`。现行只有一条流：`POST /api/agent/runs/stream`（SSE v2，帧格式见 [AGENT_PLAN](AGENT_PLAN.md) 第 5 节），没有同步生成接口，也没有任务化与断线重放——短输入与演示模式同样走这条（`MINDMAP_LLM_MODE=demo` 时 `run()` 只发三帧、不调模型）。

### 2.1 事件格式

```
event: stage
data: {"stage":"understand","progress":0.2}

event: node
data: {"parentId":"root","node":{"id":"node-3","label":"多模态输入","children":[]}}

event: diagnostic
data: {"level":"warn","nodeId":"node-7","message":"该节点在原文中缺少直接依据"}

event: done
data: {"mapId":"...","nodeCount":18,"latencyMs":6420}

event: error
data: {"code":"MODEL_TIMEOUT","message":"模型服务请求超时"}
```

约定：

- `node` 事件里的 node 是 `Node` 的合法子集（`id` / `label` / `children`），前端用同一份 `MindMapContent` 校验逻辑逐节点校验
- 收到 `done` 后前端**必须**再拉一次 `GET /api/mindmaps/{id}` 对齐，防止中间丢包
- 断线重连带 `Last-Event-ID`，服务端按任务 id 重放最近 N 条事件
- 反向代理必须关缓冲：响应头 `X-Accel-Buffering: no`，Nginx 侧 `proxy_buffering off`
- 事件只推"增量节点"，不推整棵树

### 2.2 与现行流式接口的关系

| 场景 | 用哪个 |
|---|---|
| 演示模式、短文本 | `POST /api/agent/runs/stream`（`MINDMAP_LLM_MODE=demo` 时只发 `stage` / `snapshot` / `done` 三帧，不调模型） |
| 长文本、文档、多模态 | 异步任务 + SSE |
| 断线恢复 | `GET /api/generation-tasks/{id}` 查状态，再决定是否重连 |

---

## 3 生成流水线

> **本章记的是流水线时代的形态。** `backend/app/pipeline/` 已整包删除（`POST /api/pipeline/generate` 一并删除）；
> `understand → outline → validate → verify` 里 verify 那一步还活着：结构诊断 `check()` 与语义复核
> `annotate_nli()` 现在**两条生成路各自跑**——`_generate_deep` 的收尾有（`verify_source_refs` 之后），
> 2026-11-18 回到一次生成之后 `_generate` 里也有一份（`tools.py` 的 `diagnostics.extend(check(content))`
> 与 `annotate_nli(...)` 两处调用）。中间那两天它们曾随被删的 `_generate` 一起没有调用点，现在的状态
> 是"都在"。下表「现在」列描述的是已删除的那套同步实现，保留它是为了对照目标形态，不是当前事实。

现状：生成走 agent 的工具循环——`mindmap_generate_deep` 拆成「骨架 → 按支检索 → 并发展开 → 验收」，每一步的输出由工具自己解析成 `MindMapContent`/`Node`，失败记成工具结果里的诊断而不是抛给调用方。

原先的多来源流水线有四个同步阶段，且不支持单独重跑。未来目标形态：

```
understand → outline → validate → (verify) → respond
```

| 阶段 | 现在 | 目标 | 失败兜底 |
|---|---|---|---|
| understand | 合并来源，来源不可用或为空时明确报错；长度不在这里判断 | 支持长文本分段和摘要，并保留来源关联 | 失败时提示用户，不静默截断 |
| outline | 单次 `/chat/completions` | 提示词按场景切换（通用/课程大纲/会议纪要/项目立项） | — |
| validate | `model_validate` | 不变 | **带错误摘要重试一次** |
| verify | 基础结构提示，未核验来源 | 检查节点在原文是否有依据 | 标 `unverified`，不阻断返回 |

### 3.1 重试策略

- 只对"结构不合法"重试一次：把 `ValidationError` 的字段路径与原因附进第二次提示词
- 超时、网络错误、5xx **不重试**（避免倍数放大成本与延迟）
- 每次调用记录 `model` / `prompt_tokens` / `completion_tokens` / `latency_ms`，为 F21（微调评估）与 F26（响应时间）积累数据

### 3.2 结构化输出的现实做法

现有实现依赖"提示词要求模型只返回 JSON"。更稳的做法是优先用供应商的原生结构化输出（JSON Schema / `response_format`），把 `MindMapContent` 直接作为 schema 传下去；供应商不支持时回退到提示词约束 + `model_validate`。

两种情况都必须保留 `model_validate`——它是最后一道闸，不能因为用了结构化输出就撤掉。

---

## 4 什么时候改成异步任务

改造触发条件（任一）：

- 单次生成 P95 超过 10 秒
- 接入文档解析（PDF 常见几十页，解析本身就慢）
- 接入图片识别或语音转写（**两者均已接入**：图片走多模态视觉，语音走百炼 HTTP 接口，单次最长 300 秒音频约 12 秒返回。**该条件已经触发，但持久任务尚未实现**——目前仍是同步阻塞，用户要等整个解析加生成跑完）

改造内容：

1. 新增 `generation_tasks` 表：`id / status / input_ref / created_at / finished_at / error_code`
2. `POST /api/generation-tasks` 返回 task id，后台 worker 消费
3. SSE 从任务状态表读进度，而不是从内存读
4. 前端从"等响应"改成"订阅事件"

当前没有任务队列。是否引入队列应根据持久化、重试和并发要求决定；内存队列不能保证进程重启后任务不丢失。

---

## 5 与功能清单的对应

| 功能编号 | 落在本文哪一节 |
|---|---|
| F01 / F02 | 第 3 节 outline + validate |
| F05 / F06 / F07 | 第 3 节 understand 阶段的解析扩展 |
| F15 | 第 3 节 verify |
| F20 | 导出 PUML（见主文档第 5 条，puml 只作导出） |
| F26 | 第 3 节 latency 埋点，第 2 节 streaming 首反馈 |
| F22 | 可先用 Compose 搭建单实例；多实例与数据库迁移按实际需求评估 |

---

## 6 两条需要明确的前提

**① PUML 只做导出，不做渲染主链路**

主文档（`ARCHITECTURE.md` 技术选择第 5 条）已经指出 `plantuml-encoder` 与 `viz.js` 职责不同。补充实测结论：`plantuml-encoder` 输出的是 URL 片段（一段压缩编码后的字符串），真正出图要靠 PlantUML 服务端（JVM）；`viz.js` 是 Graphviz 的 WASM，只认 DOT 语言，解析不了 PlantUML 语法。

因此 F20 的实现应当是：`MindMapContent` → 生成 puml 文本 → 导出给用户；若要在页面内预览，需要额外部署 `plantuml/plantuml-server`（GPL-3.0）并接受"预览是图片、不能节点级编辑"。

**② 当前是同步单进程，多人协作接不了**

现有乐观锁（版本号比对）能防覆盖，但不是实时合并方案。F17 要动 Yjs + WebSocket，那是一次独立改造，不能指望在现有 `PUT /api/mindmaps/{id}` 上叠加解决。
