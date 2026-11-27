# 项目现状总览

> 截至 2026-11-18。这份文档回答三个问题：**这是什么**、**现在能做什么**、**还缺什么**。
> 局部细节不在这里：架构看 `ARCHITECTURE.md`、流程看 `FLOW.md`、功能清单看
> `FEATURES_AND_OWNERS.md`、生成链路的实测看 `ONE-CALL.md`。

## 一句话

**上传材料或写一个主题 → agent 生成一张可编辑的思维导图 / 流程图 / 鱼骨图 → 在画布上改、存、导出。**

部署目标是 **2C2G 单机 + SQLite 单文件**，任何改动先问"这台机器扛得住吗"。

## 技术栈

```
后端    FastAPI + Pydantic v2（extra="forbid"）+ SQLite
检索    BM25 + 向量（text-embedding-v3, 1024 维）+ RRF 融合 + qwen3-rerank
前端    Vue 3 + Pinia + @vue-flow/core + Tailwind + Vite
模型    DeepSeek（chat / reasoner），OpenAI 兼容协议
```

## 现在能做什么

### 一、生成

**三种图型**，各有自己的结构、自己的生成提示词、自己的几何算法：

| 图型 | 契约 | 布局 | 说明 |
|---|---|---|---|
| 树 | `MindMapContent{root}` | `tidy.ts` | 经典思维导图 |
| 鱼骨 | `FishboneContent{problem, bones[]}` | `fishbone.ts` | 手写几何：主骨 + 六类斜插 60° + 中骨 |
| 流程图 | `FlowContent{stages[], edges}` | `flow.ts` | dagre 分层；阶段间边 + **阶段内判定出边** |

**六种节点形状**：`step`（矩形）/ `decision`（菱形）/ `start`·`end`（胶囊）/
`data`（平行四边形）/ `database`（圆柱）。

**一次生成 = 4 次模型调用**：

```
① 骨架      1 次   有材料看章节，>10 条先归成 3–6 组（两级骨架）
② 证据      0 次   每支各检索一次（并行，不占模型额度）
③ 结构      1 次   写整棵树 + label，detail 留空，引用在这一步定
④ 说明      2 次   按节点数切两半并发，只写 detail
```

**深度思考开关**：勾上则四段全走 `deepseek-reasoner`（不是只换一步，那是曾经的一个 bug）。

### 二、输入

```
文本粘贴    >120 字自动变成材料（可撤回），短的就是主题
文件上传    PDF / Word / PPT / Excel / HTML / Markdown / AsciiDoc / 图片 / WAV
语音        按住说话，浏览器内录音，走 ASR 转写
外部检索    五源（中文维基 / 快懂百科 / arXiv / OpenAlex / Crossref），可关
```

### 三、编辑

七个细粒度工具：`add_node` / `update_node` / `remove_node` / `move_node` /
`add_edge` / `remove_edge` / `mindmap_edit`（批量）。

画布上还可以直接改：双击改文字、Tab 加子节点、Enter 加兄弟、Delete 删子树、
撤销 50 步、Ctrl+S 保存（`version` 乐观锁，409 三选弹层）。

### 四、导入导出

**导入六格式**：本仓库 JSON（**无损往返**）/ Markdown / OPML / XMind（新旧两版）/
FreeMind / PlantUML。只有 JSON 认完整结构，其余一律导入成树。

**导出四格式**：PUML / Markdown / OPML / JSON。

### 五、记忆

```
user_prefs          用户偏好（A 类参数走请求层，C 类 note.* 进 prompt）
memory_items        会话总结出的记忆（向量检索 + 时间衰减，半衰期 30 天）
runs                每次运行的身份与产物（关页面能捡回）
edit_events         用户改了什么（不含 accept 之后的分母改造）
library_sources     材料（不是记忆）
```

### 六、界面

```
顶栏 48px       ☰ 历史抽屉 / 可改名标题 / + 新对话
画布            全宽（左右栏都撤了，材料进对话框）
底部对话框       缩略图 + 输入框 + [深度思考][智能检索][+][🎤][↑]
               生成中收成 40px 进度条，生成完 2 秒淡出（三条刹车）
悬浮球          未读角标 + 消息时间线
节点工具条      选中节点浮出 [改][展开][删][色]
```

## 进度

### 测试与提交

```
后端    2123 passed / 36 skipped（36 skip 需要 Neo4j 或网络）
前端     900 passed / 55 files
ruff    All checks passed
vue-tsc 0 error
CI      GitHub Actions 两个 job 双绿
```

### 功能清单（26 条，详见 `FEATURES_AND_OWNERS.md`）

```
已实现    3 条   F01 F04 F09
部分实现  13 条  F02 F03 F05 F06 F07 F08 F10 F12 F13 F14 F15 F16 F20 F22
待实现    9 条   F11 F17 F18 F19 F21 F33 …
待评估    3 条   F24 F25 F26（要量指标，不是要写代码）
```

**注意 F14 知识图谱是"部分实现"不是"待实现"**：全链已落地，但实测切开关后
recall@1/3/5/10 与 mrr@3 **逐位相同**、coverage@10 −0.5pp、+85ms、58 条图命中落在
标注区间 **0 条**，所以默认关。记成"待实现"会让下一个人再去做一遍。

### 这一周的实测数字

生成链路（同一主题「机器学习的主要方向」，`deepseek-chat`）：

| 方案 | detail 平均 | 耗时 | 跨支重复 | 调用 |
|---|---|---|---|---|
| 一次写完 | 27 字 | 11–15s | 0 | 2 |
| 三段式不拆 | 118.9 字 | 54.8s | 1 | 3 |
| **三段式 + 说明拆两半（现在）** | **142 字** | **38.8s** | **2** | **4** |
| 分片（已删） | 82 字 | 17–22s | **6** | 1+N |

其他：

```
检索覆盖率     一次主题预检索 5.1%  →  按支检索 30.5%
外部检索        97.2% 主题能拿到 ≥1 条，中位 4.3 秒
外部噪声        22 条 → 10 条（前缀 3 字相关性门），误杀 1
画布宽度        800 → 1440（撤掉左右栏）
跨支重复        6 → 0（一次写完整树，模型能看见全局）
```

## 已知缺口

按"做完了能换来什么"排序：

**1. 三个指标零数据（F24–F26）**

```
导图结构合理性 >85%      20 条中文样本双人盲评，报一致率与分歧
多模态支持率 ≥90%        按文本/图片/语音分别采样
响应时间 <3 秒 / <5 秒    首反馈与完整结果分开报 P50/P95
```

唯一能"对数字"的三条，2–3 天，现在是空的。

**2. 还有一个桩没删**

```
POST /api/agent/tasks       返回固定阶段的桩
POST /api/knowledge/search  已接（2026-11，见 WIRING-GAPS.md 第二节）
```

半小时能换掉被问穿的窟窿——要么实现，要么在接口文档里标清楚。

**3. F15 主动体检没有入口**

后端 `POST /api/optimization/check` 能跑，**前端 0 次调用**。缺的是"给旧图主动体检"
那个按钮。

**4. 40 节点以上的流程图读不清**

这是真问题，而且不是布局的锅：

```
40 节点流程图  原始宽度约 3700px
画布 1440 时 fitView 只能到 scale 0.276   →  单字约 2.8px
即使全屏 1440 全给画布，scale 上限也只有 0.39
```

瓶颈是"N 个阶段横向一字排开 + fitView 只做全图塞进视口"。可选方向：阶段纵向换行、
画布给「全图 / 适应宽度」两档、超宽时按宽度适配 + 横向平移。

**5. 记忆还没有真模型验证过**

总结管线、检索、衰减的**机制**都实测过（20 条混排的 score 表、一次生成只检 1 次），
但"模型总结出来的话像不像'关于这个人'"**没验过**——那需要真跑一次会话。

**6. 多人协作 / 多语言 / 微调**（F17 / F18 / F19 / F21）

都不是现在该做的：F19 依赖 F17；F21 该先做评估基线而不是直接 DPO。

## 怎么跑

```bash
# 后端
cd backend
python -m uvicorn app.main:app --reload --port 8000

# 前端
cd frontend
npm run dev

# 测试
cd backend  ; python -m pytest -o addopts="" -q     # -o addopts="" 必须带
cd frontend ; npm test -- --run
```

环境变量见 `backend/.env.example`。模型与 embedding 都需要各自的 base_url 与 key。

## 怎么验证一个改动

```
后端  python -m pytest -o addopts="" -q && python -m ruff check .
前端  npm test -- --run && npm run type-check
契约  改了就 python scripts/export_openapi.py + npm run gen:api（CI 会查这两个的幂等）
```

**CI 的「契约类型」那一步**是 `npm run gen:api && git diff --exit-code schema.d.ts`——
改了 `openapi.json` 却忘了重导前端镜像，会在那里红。

## 读代码前先看这三条

**① `AGENTS.md` 的"约定"一节**，每条都对应一次真实 bug，别只当规范看。

**② `Edge` 的 docstring** 解释了为什么是"树 + 附加边"而不是扁平的 `nodes + edges`——
那些理由现在仍然成立。

**③ `ONE-CALL.md`** 记着生成链路为什么是现在这个形状：每一条都有实测数字，
删掉任何一条依据这个形状就站不住。
