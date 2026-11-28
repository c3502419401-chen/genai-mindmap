# 文件占用约定（谁在动哪些文件）

> **本文件只管一件事：同一批文件别两个人同时动。**
> "还剩什么事没做"在 [`BACKLOG.md`](BACKLOG.md)——那里每条带机器判据，状态由
> `check_doc_claims.py --gate` 验；"需要对方知悉或确认的跨人决策"在 [`HANDOFF.md`](HANDOFF.md)。
>
> 2026-11-28 瘦身：原 §1 / §2 的 A、B 两轮排期表**已结案**，删去（原文在
> `git log --follow -- docs/FILE-OWNERSHIP.md`，本文件此前叫 `docs/TODO.md`）。
> 删它们不是因为它们没价值，是因为**读者会先看到它们，然后读到过期的话**——排期表
> 一结案就变成历史，而"别同时改这些文件"至今仍然生效。

**这个文件解决什么问题**：两个人同时改同一个文件，冲突只能靠手工解。
2026-11-07 合并上游 19 个提交时，唯一冲突就落在 `frontend/src/stores/mindmap.ts`——
双方各加字段、语义都没错，纯粹撞在一起。所以先约定：**我要动的文件在这里列出来，
你近期要动同一批文件时先说一声**，反过来我也照做。

基线读数不写在这里：用例数与文件数会随别人正常提交而变，现跑
`cd backend; pytest --collect-only -q | tail -1` 与 `cd frontend; npm test`。

## 1. 请你尽量别同时改的文件

按冲突风险从高到低：

1. `frontend/src/api/types.ts`、`frontend/src/api/contract-check.ts`——契约类型收口就靠
   这两个文件，改动集中，且 `contract-check.ts` 是手写镜像与生成类型的编译期断言。
2. `frontend/src/stores/mindmap.ts`——生成落地、画布状态、撤销栈的汇合点；
   2026-11-07 那次冲突就在这一份。
3. `backend/app/contracts/ops.py`——你我共用的交界（op 契约），动之前先报一声。

反过来说，**我近期不碰**：`backend/app/` 下的业务代码、`frontend/src/agent/`、
`stores/sources.ts` 的上传与泳道、`canvas/MindNode.vue` 的节点内编辑与预览态。

## 2. 已结案的两条（详见 [`HANDOFF.md`](HANDOFF.md)）

- **H1**（`note` 复用契约 v2 字段、与 v2 同批上线）：后端 PR #13、前端 PR #14 均已合入
  `dev`，结案。
- **H2**（整树保存 + 前端同口径校验）：陈嘉涛在 R3 开发期即已认可，RPeGio 于 2026-11-10
  复核转述，结案。

## 3. 为什么这份文件不叫 `TODO.md` 了

它原来叫 `TODO.md`，而里面的内容从来不是待办清单：待办在 `BACKLOG.md`。一个叫 TODO 而
装的是"文件占用"的文件，正是"该看哪份"这个问题最直接的坑——读者按名字找待办，找到一份
讲协作约定的文件，然后以为待办只剩这几条。名字改对，比加一条索引更有用。

**收尾时这条自问**：你正要动的那个文件，在这份清单里吗？在就先说一声。
