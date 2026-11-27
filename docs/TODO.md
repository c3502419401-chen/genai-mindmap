# 待办与文件占用（RPeGio 侧）

> **分工**：本文件管**跨人决策与文件占用**（我要动哪些文件、你近期别撞），不管技术账。
> "还剩什么事没做"在 `docs/BACKLOG.md`——那里每条带机器判据，状态由 `check_doc_claims.py`
> 的 `--gate` 验。本文件最后更新于 2026-11-10，其中"这一轮/下一轮"的说法已经过期；
> 保留它是因为文件占用那一节仍然有效。

**这个文件解决什么问题**：两个人同时改同一个文件，冲突只能靠手工解。
2026-11-07 合并上游 19 个提交时，唯一冲突就落在 `frontend/src/stores/mindmap.ts`——双方各加字段、语义都没错，纯粹撞在一起。
所以先约定：**我要动的文件在这里列出来，你近期要动同一批文件时先说一声**，反过来我也照做。

- 最后更新：2026-11-10（契约 v2 前端接线已合入 `dev`）
- 当前基线：前端 358 个 vitest（23 个文件）、后端 488 个 pytest + 19 个 subtests（20 个文件）、CI 双 job 全绿

## 1. 这一轮（A：契约类型生成 + 漂移防线）

只碰前端与 CI，不动 `backend/app/` 下的业务代码。

| # | 事项 | 主要文件 |
|---|---|---|
| 1 | 后端导出 OpenAPI 快照，前端从快照生成类型 | `backend/scripts/export_openapi.py`、`backend/openapi.json`、`frontend/src/api/schema.d.ts`、`frontend/package.json` |
| 2 | 手写镜像与生成类型的编译期断言（键集合 + 形状） | `frontend/src/api/contract-check.ts`、`frontend/src/api/types.ts` |
| 3 | 快照过期要能自动发现 | `backend/tests/test_openapi_snapshot.py`、`.github/workflows/ci.yml` |
| 4 | 文档（命令、链路、已知坑） | `docs/CONTRACT_V2.md`、`START_HERE.md`、`TEAM-GUIDE.md`、`docs/PLAN.md` |

## 2. 下一轮（B：正文贯通 agent 与生成）——**动后端，先报备**

这一轮要改 `backend/app/contracts/ops.py`，属于你我共用的交界，先写在这里，你看到后回一句"可以"或"等等"再动手。

| # | 事项 | 主要文件 |
|---|---|---|
| 1 | op 契约支持正文：`AddOp.note`、`UpdateChanges.note`（`None` = 不动、`""` = 清空） | `backend/app/contracts/ops.py`、`backend/app/agent/ops.py` |
| 2 | 两端同构的 op 执行器与共用用例 | `tests/fixtures/mindmap_ops.json`、`frontend/src/mindmap/ops.ts` |
| 3 | 生成时产出 1–2 句正文（有来源时不许编材料，仍守"只引用来源"） | `backend/app/generation/service.py`、`backend/app/generation/stream_partial.py` |
| 4 | 前端把生成结果里的正文接住（`frontend/src/mindmap/ops.ts`、`stores/mindmap.ts` 的落地路径 `commitGenerated`） | `frontend/src/stores/mindmap.ts` |

## 3. 请你尽量别同时改的文件

按冲突风险从高到低：

1. `frontend/src/api/types.ts`、`frontend/src/api/contract-check.ts`——本轮类型收口就靠这两个文件，改动集中。
2. `frontend/src/stores/mindmap.ts`——B 轮会动生成落地那一小段；A 轮不碰。
3. `backend/app/contracts/ops.py`——B 轮的核心改动，等你确认后再动。

反过来说，**A 轮我不会碰**：`backend/app/` 任何业务代码、`frontend/src/agent/`、`stores/sources.ts` 的上传与泳道、`canvas/MindNode.vue` 的节点内编辑与预览态。

## 4. 已结案的两条（详见 `docs/HANDOFF.md`）

- **H1**（`note` 复用契约 v2 字段、与 v2 同批上线）：后端 PR #13、前端 PR #14 均已合入 `dev`，结案。
- **H2**（整树保存 + 前端同口径校验）：陈嘉涛在 R3 开发期即已认可，RPeGio 于 2026-11-10 复核转述，结案。

## 5. 之后排队的（不急，先占个位）

- 结构诊断面板（F15）：把 `POST /api/optimization/check` 的结果画到画布上（现在后端能诊断，前端没接）。
- 语音句级时间戳、页码级跳转（F08 剩下的部分）。
- 容器化部署骨架（F22）：Dockerfile / compose / nginx 配置，仓库里目前还没有。
- 契约 v2 里"仅落库"的那批字段（`relations` / `style` / `pos` / `collapsed` / `theme.preset`）：等画布需要手动布局与样式时再做界面。
