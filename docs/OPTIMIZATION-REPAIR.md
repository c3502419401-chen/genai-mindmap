# 自动修复：只做"修法唯一"的那三条

> 2026-11-23。复现：
> ```
> cd backend  ; python -m pytest tests/test_optimization_repair.py -q
> cd backend  ; python scripts/measure_repair.py
> ```
> 结论先行：`optimization/` 从"只报不修"变成了"**三条能修、四条照旧只给建议**"。
> 三条之所以能修，不是因为它们简单，而是因为**修法唯一**——结果与"分段跑还是一趟跑"
> 无关，这一条写成了用例。另四条要么得由人判断，要么得由模型写内容，一条都没动。

---

## 1 为什么是这三条

| 诊断码 | 动作 | 修法唯一？ | 这一轮 |
|---|---|---|---|
| `DUPLICATE_LABEL` | 合并两个同级同名节点（子树并集 + 引用并集） | **是**：合并进该父下**第一个** | 自动修 |
| `SINGLE_CHILD` | 把"一层只有一个孩子"压成一层 | **是**：外层并进内层 | 自动修 |
| `HEADING_ONLY_BRANCH` | 同上，只是判据多了"别的分支更深" | **是** | 自动修 |
| `NEAR_DUPLICATE_LABEL` | "是不是同一件事" | 否 | 只给建议 |
| `SHALLOW_BRANCH` | 补什么内容 | 否（要模型） | 只给建议 |
| `MISSING_DETAIL` | 写什么说明 | 否（要模型） | 只给建议 |
| `DEPTH_LIMIT` | —— | 无动作可做 | 只给诊断 |

### 1.1 "唯一"的可检验形式

不是形容词。`repair()` 把两段**固定**成"先全部合并、再全部压链"，交替到不动点。于是：

```python
repair(x)                                    # 一趟全修
repair(repair(x, codes=["DUPLICATE_LABEL"]),  # 分两段跑
       codes=["SINGLE_CHILD", "HEADING_ONLY_BRANCH"])
```

两棵树**逐字节相同**（`test_order_of_repairs_does_not_change_the_result`）。

顺序为什么必须是"先合并后压链"：压链**会改 label**（`外层 / 内层`）。先压链会把两个
同名兄弟压成 `甲 / 子` 与 `乙 / 子`，它们就再也不是同名了——合并那一步被前面的步骤
无意中取消。反过来的顺序不会：合并只减少同名节点，压链只改已经独一无二的标题。

### 1.2 `SINGLE_CHILD` 的"唯一"要说清代价

它把两层标题拼成 `外层 / 内层`（`_joined_label`）。包含关系时不拼（「监督学习」/
「有监督学习」只留后者），超 200 字放弃这条修复。这是**改文字**，不是纯排版，所以：

- 每条修复的 `message` 里写清新标题是什么；
- `repair_summary()` 给出"合并了 N 处、压掉了 M 处"；
- 撤销后逐字节回到原样。

真图实测：这张图上 `SINGLE_CHILD` 的存量是 **55 条**（58 张图累加），`DUPLICATE_LABEL`
是 45 条，`HEADING_ONLY_BRANCH` **0 条**（它要求"一级分支只有一个叶子 + 别的分支 ≥3 层"，
实测的 58 张里没有一张同时满足）。所以第三条规则是**按定义实现、在真图上还没被用到**，
这一点如实写在这里，而不是假装它验证过。

---

## 2 三条硬约束怎么保证

### 2.1 幂等

`repair(repair(x))` 的 `applied` 为空，两棵树逐字节相同。

两件事保证它：

1. **每轮重新 `check()`**。不是"拿一次 check 的结果去套"——树在变，旧诊断会过期
   （`node_id` 指向的节点可能已经被上一步并掉了）。取不到可修诊断就停。
2. **预算 = 节点数**。每次修复**严格让节点少一个**（合并删一个、压链删一层），所以
   "最多 n 步"既是终止证明，也保证第一趟就修到不动点。

第 2 条是真图上门诊出来的：第一版把上限写成常数 200，561 节点那张图有 **200+ 处同级
重名**，第一趟修 200 步就停了，第二趟继续修——同一个输入两次调用给出两棵不同的树。
`scripts/measure_repair.py` 现在把幂等当断言逐张核：**58 张全部成立**。

### 2.2 引用并集（不是覆盖）

合并/压链时两边的 `source_refs` 按五元组 `(source_id, page, start, end, quote)`
去重保序取并集：

```python
# a: s1@0-4, s2@10-14      b: s2@10-14, s3@20-24
repair(...)  →  a: s1, s2, s3
```

`detail` / `note` 同理（不同则 `\n\n` 拼接）。**超上限就放弃这条修复**，不截断：

- 引用并集 > 20（契约 `source_refs` 的 `max_length`）→ `skipped`，理由写清"超出契约上限"；
- 正文并集 > 2000（`MAX_NODE_DETAIL_CHARS`）→ 同上；
- 子节点并集 > 50（`MAX_NODE_CHILDREN`）→ 同上。

截断是丢证据，比不修更糟：用户会得到一棵"看起来修好了"的树，其中一半引文消失了，
而且没有任何地方提示这件事发生过。`skipped` 会出现在响应里，界面能说"3 处没修"。

### 2.3 可撤销

每条修复带一条 `RepairUndo`，`revert()` 逆序应用回去，
`revert(repair(x).content, repair(x).applied)` 与 `x` **逐字段相同**（真图 58/58 成立）。

**为什么不用现有的 op 机制**（`agent/ops.py` 的 `OpBatch`）：op 是**增量编辑**的通道
（模型改图），它没有"把两个节点的子树并起来"这种复合操作，也不记录被删节点的原字段
——而撤销要的恰恰是那些字段。给它加一个只能撤销、不能重放的 `merge` op，两边都不合适。

**undo 存字段，不存子树**。第一版存整棵被替换的子树，真图上一量就爆：561 节点那张图的
200 步合并攒出 **3.6MB** 的 undo，端点响应塞不进去。现在每一步只记：

```
parent_id + at                 槽位
replaced: NodeFields           该槽位上原来那个节点的**字段**（不含 children）
replaced_children: int         它原来有几个孩子
inserted: [{index, fields, children}]   被并掉的那个（字段 + 它有几个孩子）
inner: NodeFields | None       压链时内层节点的字段
redirected: [...]              关系线端点的改写记录
```

子树靠"摘尾巴"还原：合并后 kept 的孩子是 `原孩子 + 被并进来的孩子`，尾巴就是后者，
一个计数就够。改完之后同一张图的 undo 从 3.6MB 降到 **950KB**（那是唯一的异常图；
中位数 0，因为 41/58 张根本没有可修项）。

---

## 3 关系线与附加边：重定向，不删除

合并让一个节点 id 消失。指向它的 `relations` / `edges` 有三种处理：

| 做法 | 后果 |
|---|---|
| 删掉那条线 | 丢用户的表达 |
| 留着不管 | 画布上一条指向不存在节点的线（`MindRelation` 拿不到端点位置） |
| **把端点改到保留者** | 线的语义不变（"A 与重复的那个有关" → "A 与留下来的那个有关"） |

选第三种，并把每一次改写记进 `RepairUndo.redirected`，撤销时改回来
（`test_revert_restores_redirected_relation_endpoints`）。

---

## 4 真图实测（58 张，去重）

```
三条规则的存量（修复前，逐张累加）
  DUPLICATE_LABEL     45
  SINGLE_CHILD        55
  HEADING_ONLY_BRANCH  0
有可修项的图           17 / 58 张
总步数                 559（跳过 0）
每张图的步数           中位 0 / 最大 504
撤销数据               中位 0 KB / 最大 950.1 KB
幂等                   全部成立
可撤销                 全部成立
```

| 节点 | 步 | 其中合并/压缩 | 撤销 | 标题 |
|---|---|---|---|---|
| 561 | 504 | 504 / 0 | 950.1 KB | 机器学习的主要方向 |
| 16 | 10 | 0 / 10 | 18.7 KB | 碳中和的主要方面 |
| 16 | 10 | 0 / 10 | 17.7 KB | 碳中和的主要方面 |
| 16 | 10 | 0 / 10 | 17.8 KB | 机器学习 |
| 61 | 6 | 0 / 6 | 7.7 KB | 检索与召回的取舍 |

两点必须说出来：

1. **41/58 张图一处都不需要修**。所以这个能力不是"每次生成完自动跑一遍"的东西，
   而是用户看到建议后点一下的东西。
2. **561 节点那张是异常图**（504 处同级重名），它一次会改掉 504 个节点。界面上必须
   先显示步数再让用户点——`RepairResult.steps` 与 `summary` 就是为这个存在的。

---

## 5 端点与「一键应用」的落地缺口

### 端点

```
POST /api/optimization/repair
{ "content": {...}, "codes": ["DUPLICATE_LABEL"] }      # codes 可省，缺省三条全修
→ { content, applied[{code, kind, message, undo}], skipped[], diagnostics[], steps, summary }
```

树型之外的图 **422**，与 `/check`、`/suggest` 同一条闸（`_tree_content`）。

这条路由 **不进 openapi 快照**（`include_in_schema=False`）。理由不是偷懒：
`backend/openapi.json` 与 `frontend/src/api/schema.d.ts` 这一轮由**另一条线在改**
（账号系统），加进 schema 就必须重导那两个文件；而 `tests/test_openapi_snapshot.py`
钉着"快照与代码一致"，所以"先加路由、稍后补快照"会让全量测试变红——那不是可以留给
下一个人的债。于是这一步走"接口存在、快照不动"，
`test_repair_route_stays_out_of_the_openapi_snapshot` 把这件事钉成用例，
免得下一个人看到它不在快照里时顺手把装饰器参数删掉。

**等账号系统落地、快照可以重导的那一轮，三条都要做**：删掉 `include_in_schema=False`、
重导快照、把前端手写类型换成生成的类型。

### 缺口：前端还没有那个按钮

`修复后的树`必须**整体替换** `store.content`，而唯一做这件事的入口
`applyContent(next, history)` 是 `stores/mindmap.ts` 的私有函数，这个文件这一轮是
**禁区**（另一条线在加账号系统）。三条现有出路都不成立：

| 出路 | 为什么不 |
|---|---|
| `map.content = next` | 绕过 `pushHistory`：能改，但**不可撤销**，且 `dirty` / `clientRevision` 不更新 |
| `map.importMap(next)` | 它会 `resetHistory()`（"导入不可撤销"是它的既定语义），正好相反 |
| `map.deleteNode` 组合 | 只有"删"，没有"搬子树"；三条规则里两条都要搬 |

所以这一轮**没有做**前端按钮，而不是做一个"点了就丢撤销"的按钮。要接的话，缺口是
一行：`stores/mindmap.ts` export 一个走 `applyContent(next, /*history*/ true)` 的方法。
在那之前，结构检查面板里 `dedupe` 的「应用」仍然只对**叶子**放行（它走
`deleteNode`，天然可撤销）——那是既有的、正确的边界，这一轮没有动它。

---

## 6 不碰的东西

- **不调模型**：`repair.py` 一次网络调用都没有。这一轮的核心约束就是"不引入新的
  LLM 回路"。（Coco 那篇论文的形式化里 `a_t` 只有"是否介入"、输出只有 `y_t ∈ {0,1}`
  加一段理由，全文没有"把节点移到哪"的表示——它解决的是*什么时候*介入，不是*改什么*。
  而布局是约束满足问题，项目里已经有确定性信号 `style_profile`（`width=uneven`、
  `diagram_type=tree`，confidence 0.9+），缺的从来不是又一个模型。）
- **不自动改层级**：压缩只去掉"一层只有一个孩子"这种没有信息量的层，不做"补一层"。
  不可逆的方向一律不碰（与论文 Appendix A 第一条 limitation 同一条口径：用户想看到、
  改正、忘掉）。
- **不碰三套几何**：`tidy.ts` / `fishbone.ts` / `flow.ts` 一个字节都没改
  （`TestTreeIsByteIdentical` 那一类逐像素用例继续钉着）。
- **不碰协作者的文件**：`mindmaps/repository.py`、`tests/conftest.py`、`openapi.json`、
  `api/schema.d.ts`、`stores/mindmap.ts`、`generation/service.py`、`agent/router.py`、
  `settings/*.py`、`auth/*`、`knowledge/external.py`、`memory/`。
