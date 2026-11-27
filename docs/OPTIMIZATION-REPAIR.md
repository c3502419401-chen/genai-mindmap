# 自动修复：只做"修法唯一"的那三条

> 2026-11-23（§5 的"一键应用"于 2026-11-26 接上）。复现：
> ```
> cd backend  ; python -m pytest tests/test_optimization_repair.py -q
> cd backend  ; python scripts/measure_repair.py
> cd frontend ; npm test -- --run tests/repair-undo.spec.ts tests/repair-apply.spec.ts
> ```
> 结论先行：`optimization/` 从"只报不修"变成了"**三条能修、四条照旧只给建议**"。
> 三条之所以能修，不是因为它们简单，而是因为**修法唯一**——结果与"分段跑还是一趟跑"
> 无关，这一条写成了用例。另四条要么得由人判断，要么得由模型写内容，一条都没动。
> 前端那一侧（§5）现在也有按钮了：**点一次「一键修复」= 一步撤销**。

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

~~`修复后的树`必须**整体替换** `store.content`，而唯一做这件事的入口
`applyContent(next, history)` 是 `stores/mindmap.ts` 的私有函数……~~

**这一节下面那段"缺口"已经补上了，见 §5.1。** 原来那三条走不通的路记在这里不是考古：

| 出路 | 为什么不 |
|---|---|
| `map.content = next` | 绕过 `pushHistory`：能改，但**不可撤销**，且 `dirty` / `clientRevision` 不更新 |
| `map.importMap(next)` | 它会 `resetHistory()`（"导入不可撤销"是它的既定语义），正好相反 |
| `map.deleteNode` 组合 | 只有"删"，没有"搬子树"；三条规则里两条都要搬 |

### 5.1 那个"一行"最后长什么样（2026-11-26）

```ts
// frontend/src/stores/mindmap.ts
function replaceContent(next: DiagramContent): boolean {
  if (!content.value) return false;      // 空画布没有"修复前"可退
  applyContent(next, true);              // ← 全部语义都在这第二个参数上
  ensureSelection();                     // 选中的节点可能刚被合并掉
  editingId.value = null;                // 就地输入框同理
  return true;
}
```

**第二个参数控制什么。** 一次 `applyContent(next, true)` 把四件事办完（这也是为什么
不能只抄一半）：

| 状态 | 调之前 | 调之后 | 由谁负责 |
|---|---|---|---|
| `dirty` | `false` | `true` | `applyContent` 无条件置 |
| `clientRevision` | `n` | `n + 1` | 同上；生成期间的冲突判定读它 |
| `undoDepth` | `k` | `k + 1` | `pushHistory()` → `recordPast(JSON.stringify(改之前那棵树))` |
| `edit_events` | 不变 | 一条 `diffEdits(改之前, next)` | `collectEdits(next)`，`pushHistory` 抄下的那棵树是 before |

`history = false` 的那条路（导入、示例预览、换图）**连账本一起不记**：`editBase` 是空的，
`collectEdits` 自动什么都不产生——"不记历史"与"不记日志"在这套代码里是同一件事。

**一次整体替换与"逐步操作"的差别，只在撤销粒度上。** 逐项入口（`commitLabel` /
`moveNodeTo` / `deleteNode`）每次压一步，所以"改了三个节点"= 三次 Ctrl+Z；整体替换压
**一步**，所以"修了 504 处"= 一次 Ctrl+Z。

### 5.2 一条撤销还是 N 条：**一条**

定这条时考虑过三种方案：

| 方案 | 结果 |
|---|---|
| 每处修复压一步（拿 `applied[]` 逐条重放） | 504 处的那张图要按 504 次才回到原样 |
| 整体替换但**不进**撤销栈 | 修完就丢了后悔药——这是 §5 表里第一条错路 |
| **整体替换 + 一步快照** | 一次 Ctrl+Z 回到修复之前 |

选第三种，三条理由：

1. **修复是一次幂等运算跑到不动点**（`repair()` 本身就是"修到不能再修"）。那些中间态
   **用户从来没看见过**，让他按 N 次才回原样，等于为一段看不见的历史付账。
2. **逐步重放要把后端的反操作逻辑再写一遍**：`RepairUndo` 靠"槽位 + 摘尾巴 + 关系线
   重定向"还原（§2.3），前端的快照栈靠整棵树。两份实现在一起维护，迟早漂移，而漂移的
   表现是"撤销之后图更乱了"——那是最难查的一类 bug。
3. **快照栈的粒度本来就是"一次 `applyContent`"**。不为修复发明第二种粒度，接口面更小。

代价得说清：这一步存的是**整棵树的 JSON**，不是后端那份 950KB 的增量 undo。真图
「机器学习」4.9KB、修复后 4.3KB；561 节点那张异常图一步约 275KB。上限（50 步 / 10MB）
一个都没改，异常图那种规模连按十次也不会撞到字节上限。

"修复会同时动多处"这件事，因此**不是 N 条撤销**：重名合并 + 单子链压缩在同一趟里
一起落地，用户按一次就全退掉（`tests/repair-undo.spec.ts` 里那条 2 步的用例钉着）。

### 5.3 为什么它不是 `APPLIERS` 的第六项

`stores/structure.ts` 那套 `APPLIERS` / `APPLICABLE_KINDS` 是**逐条建议**
（`kind` → 对一个节点的动作），现在只有 `dedupe`：删掉那个重复的叶子。`link` / `fill` /
`merge` 没有 applier，因为后端没定义"应用它到底改成什么"——「合并进哪一个？补什么环节？」
是判断题，不是规则。

修复换了一个维度：输入是**诊断码**，动作是**整棵树跑到不动点**，一次点击改多处。
硬塞进 `APPLIERS` 会要求"每条建议都能独立应用"，而那正好是后端没有的东西。所以它是
独立一条路（面板上那块「可自动修复」），`APPLIERS` 一个字没动。

将来真要加 `link` 的 applier：前提是后端先给出"把 A 与 B 连起来"的**具体动作**
（哪两个端点、什么 `EdgeKind`、`label` 写什么），然后 ①`APPLICABLE_KINDS` 加一个 `kind`、
②`APPLIERS` 加一项、③那个动作得走一个可撤销的 store 入口（现有的 `addEdge` 一类）。
三步少一步就会造出一个"点了不能撤销"的按钮。

### 5.4 接口能调吗：能，但它不是"隐藏接口"

`include_in_schema=False` **只影响文档与生成的类型**，不影响路由。实测（游客 cookie，
与 `/check` 同一条门禁）：

```
POST /api/optimization/repair   200  {content, applied, skipped, diagnostics, steps, summary}
第二次同一张图                   200  {steps: 0, applied: [], summary: ""}
codes: ["DEPTH_LIMIT"]          422  "不能自动修的规则：DEPTH_LIMIT（可自动修的是 …）"
GET  /api/openapi.json          404  （整份 spec 都不对外）
backend/openapi.json            不含 /optimization/repair（快照没被碰）
```

所以前端调它是正常的 `POST`（`api/client.ts::repairOptimization`），**没有重导契约**。
前端那份响应类型是手写的，`RepairStep.undo` 故意声明成 `unknown`——前端不读它（撤销走
快照栈），抄一份没人读的后端结构只会在字段变化时给出假的"我懂它"。

### 5.5 落地的四道闸

1. **只有点了才发生**：后端没有自动应用接口，前端也只在 `@click` 里发请求。生成落地后
   自动跑的那次是 `check`（只读、免费）。
2. **在途期间图被改过 → 不落地**。画布在修的时候不锁（生成期间也不锁，同一条口径），
   拿回来的树是按**旧**那棵算的，装进去等于静默丢掉用户刚改的两笔。落地前比一次引用，
   变了就放弃并说清原因（`'stale'`）。
3. **修完立刻用响应里的 `diagnostics`**（后端在修完之后重跑过 `check`），不再多打一次
   `/check`：树是我们刚装进去的那棵，重跑只可能得到同一份结论。
4. **改了什么要给人看**：面板逐条显示 `applied[].message`（后端保证里面写清新标题是
   什么），`skipped` 单独一句"3 处没修"。撤销之后那块清单会标注"说的是修复当时那一棵"
   ——挂在已经撤回的画布上继续说"已修复"是最容易让人不再信任这个面板的形状。

### 5.6 真图验收（`frontend/tests/`）

`tests/fixtures/repair-real.json` 是**库里真图**（`backend/data/mindmaps.db` 的
`0b82a740`「机器学习」），`before` / `first` / `second` 全部由路由函数真跑产出、与线上
响应**逐字节相同**（实测比对过）。`tests/fixtures/repair-merge.json` 是构造输入 + 真响应
（真库里唯一有同级重名的图是 561 节点那张异常图，600KB，塞不进仓库）。

| 验收项 | 结果 |
|---|---|
| 点「检查」能看到"可自动修复 3 处（单子节点 3）" | `repair-apply.spec.ts` |
| 点「应用」树变了、能逐条看到改了什么 | 同上（3 条 message，含新标题） |
| 一次 Ctrl+Z **逐字节**回到修复前 | `repair-undo.spec.ts`（比 `JSON.stringify` 全等） |
| 再按一次重做回到修复后 | 同上 |
| 保存 / 导出 / 重新导入内容一致 | 同上（截请求体比字节） |
| 应用两次（幂等）：第二次 `steps === 0`，且**不压历史** | 同上 + 面板不出现"已修复"块 |

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
  `api/schema.d.ts`、`generation/service.py`、`agent/router.py`、`settings/*.py`、
  `auth/*`、`knowledge/external.py`、`memory/`。（这是**那一轮**的边界：当时那几份由
  账号系统那条线在改。`openapi.json` 与 `api/schema.d.ts` 已在加 `kind='repair'` 那一轮
  重导，见 §7「落地复盘」。）
- **`stores/mindmap.ts` 只加了一段**（§5.1 那个 `replaceContent` 与它的 export）。
  那一轮它是禁区（账号系统、引用跳转两条线在改同一个文件），所以落点选在
  `applyContent` 紧后面——一处新增，零改动既有行。`api/client.ts` 是新增一段
  `repairOptimization`；`stores/structure.ts` 是新增 `applyRepair` 一族（`reset` 里加了
  两行清态）。`components/StructureCheck.vue` 新增两块面板。几何、账本字段、契约一个字
  都没动。

---

## 7 自评：「自动修复」与「用户自己改」在账本里该不该分开

> **状态：已落地。** 这一节保留下的是当时的判断过程；实际落点与两处补正在末尾
> 「落地复盘」，口径的完整说明在 `docs/EDIT-LOG.md` 二·七。

**结论：该分开，但分开的维度是 `kind`，不是 `actor`。**

现状：`replaceContent` 走 `applyContent(next, true)`，`pushHistory` 的 `actor` 默认
`user`，于是这次修复产出的每条事件（`remove` / `add` / `move` / `update(label)`）在
数据库里都记成"用户改的"。这**不是**没想过，是两条都不成立：

| 记成 | 后果 |
|---|---|
| `user`（现状） | 用户回看"我改过什么"会看到一堆自己没做过的改动。而且 `SINGLE_CHILD` 的修复**是改文字**（`外层 / 内层`），它会让 `label_shrunk` / `by_field` 这些数字里混进"不是人写的"样本 |
| `agent` | 更糟。`EditActor` 里 `agent` 的意思是"模型算的"（`contracts/edits.py` 那句"`agent` 的改动是模型自己又改了一遍"），而规则修复一次模型调用都没有。把它塞进 agent 桶，等于让"模型的改动"这个分母失去意义——而这一轮修的正是"不引入 LLM 回路" |

现在为什么**不该**引入第三种 `actor`：`EditActor` 是契约里写死的
`Literal["user", "agent"]`（`contracts/edits.py:60`），加一个值要动三样东西——
后端 Literal、`backend/openapi.json`、`frontend/src/api/schema.d.ts`。后两样这一轮由
账号系统那条线在改，重导必然冲突；而且 `edit_events` 里已经躺着几千行历史数据，
枚举一变，读老数据的人就得先解释"那批是什么"。

**下一轮该做的是加 `kind='repair'`，不是加 `actor`。** 判据是这样一条分工：
`actor` 回答"谁有能动性"（人 / 模型），`kind` 回答"这是一次什么动作"。修复的能动性来自
人（他按了按钮），动作语义是"规则批量整理"，两件事分别落在两个字段上，互不污染。
这套账本已经有先例：`generate` 的 `actor` 是 `agent`，但它靠 `kind` 与"生成带来的那批
节点改动"分开——同样是"用一个字段表达动作、另一个字段表达能动性"。

落在代码上是三步，都很小：

1. `contracts/edits.py` 的 `EditKind` 加 `"repair"`（一处 Literal）；
2. 重导 `openapi.json` + `schema.d.ts`（等账号系统线落地那一轮，和 §5 里
   `include_in_schema` 那三件事一起做）；
3. `replaceContent` 加一个参数（`actor` 或 `kind`），`applyRepair` 传 `'repair'`；
   `diffEdits` 的 `emit` 已经按 `options` 走，只多一个可选字段。

给统计口径的连带影响也说清：加上之后 `by_kind` 会多一个桶，`SUM(by_kind)` 与
`total` 的不变式照旧成立；`label_shrunk` 只统计 `field='label'` 且已是
`actor="user"`，需要再补一条 `kind != 'repair'` 的过滤——否则"规则把两层标题拼成一个"
会被算进"用户把标题改短"。

在此之前（也就是现在），那点代价是**可控且可见**的：一次修复是**一条**撤销步，用户按一下
就能撤回；面板上写着"已自动修复 N 处"，`pendingEdits` 里那一批事件共享同一个 `at`
（`GROUP BY at` 能把它整批认出来）。也就是说，事实没丢，只是暂时只能靠时间戳分组，
而不是靠一个专门的 `kind`。

---

### 落地复盘（本轮）

三步都按上面走了，只有一处和当时写的不同，另有两处是当时没点到的。

1. **`EditKind` 加 `"repair"`**（`contracts/edits.py`），重导 `openapi.json` +
   `schema.d.ts`。
2. **`DiffOptions.kind`**（`mindmap/edit-log.ts`）：给了它就覆盖每条事件的 `kind`。
   `emit` 里加在 `...event` **之后**，所以它是这个文件里唯一一处"动作名可被外部改写"
   的位置。三条前端用例钉住"只动 `kind` 这一个字段"（`tests/repair-ledger.spec.ts`）。
3. **`replaceContent()` 里写死 `{ kind: 'repair' }`**，不给参数。当时写的是
   "`replaceContent` 加一个参数（`actor` 或 `kind`）"，实际判断是**参数反而更危险**：
   这个函数的语义就是"修复落地的唯一入口"（唯一调用方是 `applyRepair`），"这一次是
   规则批量整理"是它的定义，不是它的选项。传参意味着下一个用整体替换落地**别的**动作
   的人会静默记成 `repair`，或者忘了传、把修复重新记成"用户改的"——而后者正是这一轮
   修的 bug。写死之后，改它必须改这个函数，而改的人一定读得到那段注释。
   顺带：`applyContent` 的第三个参数从 `(actor: EditActor)` 换成了
   `(origin: EditOrigin)` 选项对象（`{ actor?, kind?, runId? }`）——两个都是字符串
   字面量联合，位置传反编译器只会含糊地报一句"类型不符"。存的形状与原来的
   `pushHistory('agent', runId)` 等价，`applyContent(patched, true)` 那二十多个调用点
   一个字都没动。

**当时没点到的两处，都要补：**

- **`by_field` 也要排除 `repair`。** 上一轮只说了 `label_shrunk`。但 `repair` 改文字
  的事件同样带 `field='label'`，混进 `by_field` 会让"用户最常改哪个字段"被规则自己
  改出来的样本污染；更硬的理由是——`sum(by_field) == by_kind.update + by_kind.style`
  那条恒等式会**破掉**，而它是"有没有写入路径绕过契约"的唯一自校验。做法是让
  `by_field` 的 SQL 只统计这两种 kind（后端 `FIELD_KINDS`）。
- **`changed_events` / `untouched_runs` 的 kind 集合要一起加 `repair`。** 两个集合
  必须逐字相同（后端 `CHANGE_KINDS` 一个常量管两处）：漏在两边之外，"模型给了 30 个
  节点、规则删掉 12 个"会被读成"一字未改"，而那正是整张表最贵的那个数的误报方向。

`removed_depth` / `removed_subtree` **不用改**：它们的条件是 `kind = 'remove'` 精确匹配，
而修复产出的删除事件 `kind` 已经是 `repair`，天然不进——这正是要的（"用户嫌哪一层
多余"与"规则把单子链压掉"不是同一件事）。
