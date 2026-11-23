# 改动日志：先让"用户最常改什么"变成事实

这一版**只记录、不总结**。

用户的完整设想是"记录修改 → 总结 → 喂给训练模型 → 个性化"。后面几步（DPO 微调）
需要几百条偏好对，而一天几次的生成攒够要几个月。但第 1 步值得马上做，因为很可能的
结论是：改动集中在一两个具体问题上（"80% 的改动是把长标题改短"），那种用规则一天
就能修，不需要训练。

所以这一版的唯一目标：**能回答"用户最常改什么"**。

不做的事，逐条都有理由：不做 LLM 总结（实测同一主题两分钟内出现过两个相反要求，
自动总结会写出互相矛盾的条目）；不做训练数据导出（等有几百条再说）；不做实时上报
（攒到保存）；不做 `sendBeacon` 兜底——"不保存就关页面 → 事件丢失"是可接受的
（那次编辑本来也没保存），用半截方案假装能挽回只会让丢失变得更难查。

---

## 一、diff 怎么算才算有意义

实现在 `frontend/src/mindmap/edit-log.ts`（纯函数）与 `frontend/src/stores/mindmap.ts`
（`pushHistory` / `collectEdits`）。分三份比：节点字段、树结构、边。

三条判断，都直接影响统计怎么读：

**一个字段一条事件。** 改一个节点的 `label` 同时改 `color` 会产生 2 条，不是 1 条。
合并成一条的话，`by_field` 就必须把一条事件同时算进两个桶，`sum(by_field)` 与
`by_kind.update + by_kind.style` 对不上——统计一旦不能自校验，读的人得先怀疑数字再
怀疑结论。代价是"一次操作产生几条事件"只留在 `at` 里：同一次提交内所有事件共享
同一个时刻字符串，`GROUP BY at` 能把它还原成一次操作。

**id 变了就是 remove + add，不做相似度匹配。** 节点 id 由 `newId()`（`n_` + 随机）
或后端分配，"删掉再加一个同名节点"必然是新 id。按 label 或位置去猜"这其实是同一次
修改"会把统计变成一件需要解释算法的事，而它现在只需要读 SQL。

**顺序变化只在"这一层成员没变"时才算 move。** 单纯比 sibling index 会炸：一次
"在 50 个兄弟中间插一个"会让后面每个兄弟的 index 都 +1，于是 1 次真实操作变成 50 条
move。所以 index 变化只有在**该父节点的子 id 集合完全没变**时才上报——那才是重排
（agent 的 `move` op 带 index 就会走到这里）。

## 二、`EditEvent` 的形状

```ts
type EditEvent = {
  actor: 'user' | 'agent'
  kind: 'add' | 'remove' | 'update' | 'move' | 'edge' | 'style' | 'accept'
  nodeId?: string          // edge 事件填源节点；title 事件没有节点
  field?: EditField        // 只在 update / style 上有值
  before?: string          // 摘要，不是内容
  after?: string
  depth?: number           // 节点层数，根是 1
  subtreeSize?: number     // remove：这条一共删掉几个节点（含自己）
  changeCount?: number     // accept：这次接受带来几处改动，0 = 一字未改
  runId?: string           // 这次改动动的是哪个 run 的产物
  at: string               // 同一次提交共享同一个值
}
```

`field` 只在 `update` / `style` 上有值，这条规则换来两条恒等式：
`sum(by_field) == by_kind.update + by_kind.style`、`sum(by_actor) == total`。

摘要规则：

| 字段 | 存什么 | 为什么 |
|---|---|---|
| `label` / `title` | 全文（≤200 字） | 统计要算长度，截了就没法算 |
| `detail` / `note` | `"1800字"` | 2000 字正文进日志会把表撑成第二个 `mindmaps.content`，而"从 1800 字改成 700 字"本身就是情报 |
| `color` / `icon` | 原值或 `"无"` | 它是键不是内容 |
| `move` | 父标题或 `"第2位"` | id 对统计没用，标题读得懂 |
| `edge` | `"A → B（是）"` | 边只有 id，人对不上 |
| `depth` | 层数，`remove` 填删除前所在层 | `removed_depth` 按它分组（只对零散删除） |
| `subtreeSize` | `remove` 删掉的节点数，含自己 | 一整支被删只该有一条事件 |
| `changeCount` | `accept` 带来的改动条数（**历史列，新数据不写**） | `0` 是"一字未改"的旧写法 |
| `nodeCount` | `generate` 落进画布那棵树的节点数（**分母**） | 在生成完成那一刻定死，保存时不重算 |

`depth` 是这一版最容易被忽略、又最值钱的一个字段：没有它，"删掉的集中在第几层"
就只能靠事后猜。

## 二·五、三处"新增"，都是为了不让结论朝一个方向偏

**① `generate`：补上分母。** `create` 意图落地时 diff 是 0 条，于是"生成完一字
不改直接保存"在 `edit_events` 里是 0 行，与"这个用户从没用过"完全不可区分。10 张图
里 9 张一字未改、1 张改 10 处，只统计改动会得出"标题被普遍改短"，然后去把 16 字上限
压到 12——而那 9 张本来合适的图在表里根本没出现过。**缺的不是样本，是分母**，偏差
方向永远朝"生成质量更差"。

所以每次生成落地（`create` / `expand` / `edit` 三条路）都记**一条** `generate`，
`actor='agent'`、`node_id` / `field` / `depth` 全空，只带两个数：`node_count`（落进
画布的那棵树有几个节点，**在生成完成那一刻定死**）与 `run_id`。

> **换人说明（候选层删掉之后）。** 这条事件的前身是 `accept`：那时生成结果先进
> "候选"，用户点「应用到画布」才记一条 `accept`（`actor='user'`，载荷是
> `change_count`）。候选层删掉之后没有"应用"这个动作了，于是：
>
> - 事件改叫 `generate`，`actor` 变成 `agent`（产出这棵树的是模型）；
> - `change_count` 不再写——"这次生成有没有被改"直接查"有没有同一个 `run_id` 的
>   改动事件"就够，不需要前端算好塞进来；
> - 老数据里的 `accept` 行原样保留，`by_kind` 仍然读得到（所以后端的 `EditKind`
>   里两个字面量都在）。

`node_count` 用**独立的整数列**而不是塞进 `after`：`after` 的语义是"截断后的文本
摘要"，把计数混进去，`CAST(after AS INTEGER)` 会对每一条文字摘要静默返回 0——一个看
起来合理、实际全错的聚合，比报错糟得多。

统计口径（`GET /mindmaps/{id}/edits/summary`）：

```
分母      generated_nodes  = SUM(node_count) WHERE kind='generate'
分子      changed_events   = COUNT(*) WHERE kind IN ('update','remove','add','move','style')
生成次数  generated_runs   = COUNT(*) WHERE kind='generate'
零改动    untouched_runs   = 那个 run_id 下一条改动事件都没有的 generate 行数
```

`edge` **不进分子**：分母数的是节点，"加了一条关系"算进节点改动会让比例失去意义。
零改动那边的 kind 集合与分子**逐字相同**——只要一边多认一种，就会出现"既不算改动、
也不算一字未改"的黑洞，而那条恒等式（`changed_events = 0 ⇒ untouched_runs =
generated_runs`）正是读这份统计的前提。`run_id` 为空的老 generate 行不算零改动：
那不是"一字未改"，是"当时没记"。

`label_shrunk` / `removed_depth` / `removed_subtree` 三个指标没变，只是分母换了人。

**② `subtreeSize`：别把"删了一支"读成"每层都在删"。** 以前删一整支会在 depth 2…5
各留几条 `remove`，`removed_depth` 看起来"每层都在被删"，于是"初次生成挖太深"这个
结论**直接不成立**（不是方向偏）。现在一条 `remove` 代表一整棵被删的子树：只有"父节
点还在新树里"的那个节点是子树根，后代不各记一条。数的是**真的消失了**的节点——删除
时子节点被改派到别处活下来，只有根自己算 1。

统计随之分叉：`removed_depth` 只统计 `subtree_size = 1` 的零散删除；`removed_subtree`
是删除规模直方图 `{size_1: 5, size_12: 1, size_unknown: 2}`。加列之前的老数据是
`NULL`，**不做回填**，只进 `size_unknown`，不进 `removed_depth`——否则那次污染还在。

**③ `label_shrunk` 只看 `user`。** `agent` 的改动是"模型自己又改了一遍"，不是用户的
偏好。实测 B 图只有 3 条 user 的缩短，不过滤会读成 4 条（多出来的是 agent 的 13→10）。

## 二·六、`parent_id`：让"新节点挂在哪"从猜变成可判

`edit_events` 跑了几周，攒下 565 条：`update` 559 / `style` 4 / `remove` 2，而
**`add` 零条**。不是用户从没加过节点，是"加节点"这件事从来没被记成一行——于是
"用户新加的节点挂在哪"没有任何样本，任何"这个新节点与哪条论证线同源"的判断都只能猜。

补的是两处，它们各自解决一个不同的问题：

| 位置 | 补什么 | 为什么在这里 |
|---|---|---|
| 前端 `mindmap/edit-log.ts` | `EditEvent.parentId`，`add` 事件填上 | `indexForest` 的 `Indexed` 里**本来就有** `parentId`（用于判 `move`）。emit 只是没把它带出来，不是缺数据 |
| 后端 `mindmaps/edits.py` | `edit_events.parent_id` 列，值从**保存的那棵树**算 | 树是权威、事件是它的描述；描述跟着权威算，不会出现"父 id 指向不存在的节点"这种要另写校验才拦得住的状态 |

### 前端：一个已经算好、却没被带出来的字段

`diffEdits` 的 `indexForest()` 给每个节点建 `{node, parentId, depth, index}`，`parentId`
早在那里——`move` 事件就是靠它判的。`add` 那条 emit 只是没把它写出来：

```ts
emit({ kind: 'add', nodeId: id, after: clip(...), depth: entry.depth,
       parentId: entry.parentId })   // ← 只有这一处是新的
```

**`undefined` 与 `null` 是两件事**：`undefined` 表示这类事件不记父节点
（`update` / `style` / `remove` / `edge` / `title` / `generate`），`null` 表示它就是
某棵子树的根。混成一个值，读的人就分不清"根"和"没记"。

### 后端：为什么不把 `parentId` 塞进请求体

`EditEventIn` 是 `extra="forbid"`，给它加字段就要重导 `openapi.json` 与
`schema.d.ts`——那是另一条线正在改的两个文件。而这一列要回答的事实，在保存那一刻的
**树**上是唯一确定的，不需要谁把它抄一遍：

```
保存 → body.content（整棵树）→ edits.parents_of(content) → node_id → 父 id
     → INSERT ... parent_id
```

三个好处，都不是"省事"：

- **不会不一致**：前端算的 `parentId` 与后端的 `parents_of` 用同一套口径（见下），
  但真正进库的是从落库那棵树算出来的那一份，与 `content` 永远对得上；
- **不动契约**：`openapi.json` / `schema.d.ts` / `EditEventRow` 一个字节没变；
- **两种图型免费**：`agent/shapes.roots` 已经是三型统一的读侧适配层。

口径（三处必须逐条一致，否则同一条挂载关系会读出两个答案）：

| 图型 | 什么叫"根" |
|---|---|
| 树 | 中心主题（`root`） |
| 鱼骨 | **六根骨各自是根**——它在画布上挂在主骨上，但那是 `FishboneBone` 的关系，不是 `Node` 的父子关系 |
| 流程 | **各阶段各自是根**，理由同上（`FlowStage`） |

### 列的语义，一句话

> `parent_id` = **保存那一刻 `node_id` 在树上的父节点**。

所以：`add` 有值（"新节点挂在哪"）；`remove` 一定是 `NULL`（那个节点已经不在树上了
——不是"删除前的父"，要那个得另记一列）；`title` / `generate` 没有节点所以 `NULL`；
`edge` 的 `node_id` 是边的源节点，它在树上，于是有值（如实记它，不因为"这条事件不改
节点"就抹掉一个成立的事实）。

### 列加在哪：为什么不是 `repository.py`

那张表的 DDL 确实在 `mindmaps/repository.py`，而它正在被别人改。补列写在了
`mindmaps/edits.py::ensure_schema`——`edit_events` 从建表到读写全在那个模块（仓储层
只剩建表语句与一个索引），所以"这张表后来加的列"归它的唯一使用方补齐，是职责收口。

形状照 `knowledge/library.py::ensure_schema` 与 `memory/store.py::ensure_schema`
（模块自持 DDL、幂等、拿到连接后立刻调），判据照 `repository._ensure_columns`：
**只吞 `duplicate column name`**，别的错误继续抛——缺列的后果是 `record()` 的 INSERT
抛 "no such column" 被它自己的 `except` 吞掉，**所有事件静默丢失**，`total` 恒为 0，
看着和"没人改过"一模一样。

老行上是 `NULL`，不做回填：`NULL` 在这里有确切含义（"当时没记"），比编出来的空串诚实。

### 读路径：先只服务库内读者

`EditEventRow` 是 `GET /mindmaps/{id}/edits` 的响应模型，给它加字段就要重导
`openapi.json`。所以这一列现在只有一个读入口 `edits.parent_ids(map_id)`，返回
`(kind, node_id, parent_id)`，缺省只取 `add` / `move`（"放置动作"）。

**这不是将就**：`node_count` 当初也是先落库、后有统计口径的。等这些读数证明它有用，
再决定要不要进接口。



`POST` / `PUT /api/mindmaps` 的请求体里带 `events`（可省略）。

决定性的理由：**第一次保存之前根本没有 `map_id`**。一个
`POST /api/mindmaps/{id}/edits` 要么发不出去，要么必须先 create 再补发——于是多出
"内容存了、事件没存"这种半截状态，而统计从此只看见部分改动，且没有任何地方能察觉。

并进保存之后：两者同一次请求、同一次成败；409 冲突时一起拒，重试一次就都补上，不会
出现"事件已写、内容被拒"的统计漂移。

单次最多 500 条。超出的部分**不是丢弃**，而是留在前端队列里等下一次保存（分批），
只把条数上报成一条服务端 warning——不在表里插一条"我截断了"的假事件，因为 `total`
必须是真实改动数。

事件写失败**不让保存失败**（`edits.record` 内部吞掉并记 warning）。为了让统计写进去
而让用户看到"保存失败"、进而重试撞 409，方向完全错了。

## 四、刻意不记的三件事

| 不记 | 理由 |
|---|---|
| 撤销 / 重做 | 一次被撤销的编辑不是用户的最终意图。记了它，`label_shrunk` 会同时收到"18→9"和"9→18"，平均值被拉回中性、次数却翻倍——比不记更糟。"用户反复撤销"该是另一个指标，这一版不做。 |
| `create` 落地的**整棵树** | 整棵树都是"新增"，那不是 diff，是把整棵树重念一遍。与 `diffCandidate` 对空基线返回 `null` 同一条判断。注意这条只否掉了"整棵树"这个载荷，**没有否掉"生成"这个动作**——动作记成一条 `generate`（见二·五）。 |
| 换朝向 / 换布局 | 它写在 `content` 里，但回答不了"生成侧该改什么"，记下来只是噪音。 |

## 四·五、死表探测器：一张永远 0 行的表不会自己喊

`user_prefs` 有"尚未生效"这种标记，`edit_events` 原本没有——于是"永远是 0 行"这种
故障能静默存在很久。实测有过 40 分钟：8000 跑着旧代码，前端在发事件、后端在丢，而每
一张图的 `summary.total` 都是 0，看起来和"没人改过"一模一样。

两条读数：

- 单图：`summary` 多了 `last_event_at`（最近一条事件的**入库**时刻，空表是 `null`）。
  `total=0` 与"这张图没人改过"长得一样，`last_event_at=null` 说的是另一件事——这张
  图从来没有过任何事件。
- 跨图：`GET /api/mindmaps/edits/health` 给昨日编辑过的图数 / 昨日保存的图数。
  **判据："保存了但没有任何事件"在正常情况下不该发生**（每次生成落地都记一条
  `generate`，连一处没改也记），所以 `saved > 0 且 edited == 0` 就是链路断了。

两个方向不对称，这很重要：链路真断时它**一定**报警；反过来只会漏报（`saved_maps`
是下界：`mindmaps` 只留最后一次保存时刻），不会因为"数据少"就报成健康。唯一的误报
窗口是跨零点那一次保存——`updated_at` 写在事件之前几毫秒，可能落进相邻两天。

## 五、局限：不要让日志看起来能回答所有问题

一次真实跑批（20 节点的旧图，用户改 12 条标题、上 3 个色、写 2 条备注、挪 1 个节点、
加 4 个、零散删 7 个、把一支 4 个节点的整支删掉，再让 agent 生成一轮）得到：

```
total 37   by_actor {user:31, agent:6}
by_kind {update:20, remove:8, add:4, style:3, move:1, generate:1}
by_field {label:16, color:3, detail:2, note:2}
label_shrunk {count:13, avg_before:14.4, avg_after:6.0}   ← 只算 user
removed_depth {3:2, 4:4, 5:1}                             ← 只算零散删除（7 条）
removed_subtree {size_1:7, size_4:1}                      ← 另有一支 4 个节点整支被删
generated_runs 1   generated_nodes 22   changed_events 36   untouched_runs 0
last_event_at "2026-11-17T09:35:11.208Z"
```

（`by_kind` 里 `generate` 的位置在过去是 `accept`：候选层删掉之前，那一轮是"用户点了
应用"才记的。老图上的历史行仍然显示 `accept`，口径见二·五。）

看起来能直接推导出三条处方：**标题太长了**（13 条缩短，14.4 → 6.0 字）、**生成挖得
太深**（零散删掉的 7 个里 4 个在第 4 层）、**有一整支不该生成**（4 个节点被一次删掉）。
这三条推论**都可能是错的**，而且错法各不相同：

- **缩短 ≠ 太长。** 用户也可能只是在压缩视觉密度，或者这张图本来就要放进一页 PPT。
  工具有个"画布 24 字截断"，用户把 14 字改成 6 字也可能是在给节点框留白。
  要区分这两种，需要**同一个节点上没有其它改动**的样本，以及"缩短后图的总节点数
  变没变"——这一版都没记。
- **删在第 4 层 ≠ 初次太深。** 也可能是因为第 4 层的内容质量最差（模型在深层只会写
  "进一步细化"这种废话），那该改的是提示词而不是深度参数。日志只有 `depth`，没有
  "被删的那句话写了什么"——按这一版的取舍，`label` 存了全文，所以**这一条其实能查**：
  `GET /edits` 里 `kind='remove'` 的 `before` 就是那些标题。这是设计里少有的"事后
  还能补查"的地方。
- **整支被删 ≠ 某一层被嫌弃。** `size_4` 那条说的是"这一支整体没用"，它可能很浅也
  可能很深；`removed_depth` 现在只数零散删除，正是为了让这两件事别混成一条。
- **`by_actor` 里 `agent` 那 6 条是生成一次性落地的**，不代表用户认同每一处——用户
  可能只想改 1 条，落地带来了 6 条。判断"用户是不是根本没动"看的是
  `untouched_runs` / 同一个 `run_id` 下有没有改动事件，而不是看某一条事件。
- **"一次生成改了几处"与"这次生成有没有被改"是两个问题。** 前者是 `changed_events`
  与 `generated_nodes` 的比，后者是 `untouched_runs`。只看前者会得出"每节点被改 0.8
  次"这种看不出方向的数；加上后者才知道那是"普遍改了 0.8 次"还是"9 次一字未改、
  1 次改了 8 处"——两种情况的处方完全相反。
- **"改了又改回去"会被记成两次改动。** 撤销重做刻意不记，但用户手工把 9 字改回 18
  字是一次真实的 `label` 改动：`label_shrunk` 收到 18→9，也收到 9→18（后者不计入
  `count`，因为没变短）；两条都留在 `by_field` 里。要判"这个用户改来改去"需要按
  `node_id` 串时间线，这一版没有这个视图。
- **"用户删了自己刚加的节点"与"删了模型生成的节点"分不开。** 两者都是
  `kind='remove'`。要分开必须顺着 `nodeId` 回溯"它是这棵树里本来就有的、还是本次
  会话里 `add` 出来的"——可行，但要一次跨事件的关联查询，这一版不做。
- **样本量。** 37 条来自一张图。一张图的改动分布与"这个人怎么用这个工具"是两件事，
  而这张表只按 `map_id` 存。要回答后者，得跨图聚合（`GROUP BY map_id` 再平均），
  这一版没有这个接口（`/edits/health` 只回答"链路还活着吗"）。

一句话：**这张表能告诉你"改动集中在哪"，不能告诉你"为什么"。** 前者已经够砍掉一
大半的猜测（不用再开会讨论"用户到底嫌哪里不好"），后者仍然得靠看具体的那几十条
`before` / `after`——所以 `GET /edits` 明细那条路径不是附属品，它是把分布翻译回
人话的唯一入口。

## 六、代码位置

| 位置 | 内容 |
|---|---|
| `frontend/src/mindmap/edit-log.ts` | 类型 + `diffEdits` / `acceptEvent` 纯函数（三条判断与两处"分母"都在这里） |
| `frontend/src/stores/mindmap.ts` | `pendingEdits` / `pushHistory(actor, runId, accept)` / `collectEdits`；`save()` 里分批提交、成功才清 |
| `frontend/src/api/client.ts` | `saveBody`（空数组不发字段）、`getEditSummary`、`listEditEvents`、`getEditHealth` |
| `backend/app/contracts/edits.py` | `EditEventIn` / `EditEventRow` / `EditSummary` / `EditHealth` |
| `backend/app/mindmaps/edits.py` | `record` / `summary` / `health` / `recent` / `clear` |
| `backend/app/mindmaps/repository.py` | `edit_events` 表、索引与幂等补列（`_ensure_columns`） |
| `backend/tests/test_edit_events.py` | 36 条：落库、统计、三个关键指标、两处分母、旧表补列、五个边界 |
| `frontend/tests/edit-log.spec.ts` | 33 条：diff 正确性、actor 区分、子树规模、accept 三条路、只存长度、500 上限、清空时机、撤销不记 |
