# agent 生成流的节点级事件

「模型还在输出，节点就一个个出现」这层能力原先长在普通生成流（`POST /api/generations/stream`，已删除）上，
增量解析器还在 `generation/stream_partial.py`：流式接收侧逐 `data:` 帧收 `delta.content`，攒够一小段就交给
`parse_partial` 补括号解析，再用 `collect_new_nodes` 挑出没推过的节点。现在它只服务于 agent 的
`node_grow` 预览。

agent 路径（`POST /api/agent/runs/stream`）上**两条生成路都接上了这一层**，工具内部
边收模型分片边解析，把已经写完整的节点报成 `node_grow` 帧；整树只在 `snapshot` /
`done` 帧里给出。没有这一层时，两条路都要发十几次调用，用户看到的是几十秒空屏
然后整棵树同时出现。

两条路的接法不同，因为响应形状不同：

- **深层生成**（`mindmap_generate_deep`）：`ask_model` 给每次展开调用带 `on_delta`。
  响应是 `{"children":[{"label":…}]}`，**没有 id**，父子关系由"这一路在展开哪个
  slot"决定 → `agent/stream_items.closed_items` + `_GrowStream`。
- **整树生成**（`mindmap_generate`）：
  - 结构那一次调用就带 `on_delta`。响应是一棵自带 `id` 的整树（`{"root":{…}}`）
    → `generation/stream_partial.parse_partial` + `collect_new_nodes`，工具层再把
    id 换成预览协议的父索引链（`_GenerateGrowStream`）。
  - **按章切（`MINDMAP_SECTION_SLICE=1`）时结构那一次换成"每章一次"**
    （`_slice_structure` → `_slice_one`），响应变成 `{"children":[…]}`，于是那 N 次
    调用各自用 `_GrowStream`（与展开响应同形）；根与 N 条一级分支由
    `_slice_structure` 在并发**之前**先报，孩子的 `path` 才挂得上。

  ⚠ 第二条以前是断的：`_generate` 里 `if slice_chapters: … else: …` 互斥，而
  `on_delta` 只接在 `else` 那支上——切了章，那次调用根本不发生，于是整棵树要等 N 次
  调用全部 `gather` 完、在 `_slice_structure` 末尾组装好才一次性落画布（实测 37,973
  字的 PDF：0 帧，73 个节点在几十秒后同时出现 = `12 × 6 + 1`，正好是
  `_slice_budget` 的公式）。2026-11-25 修掉。

本文定义 agent 侧的节点生长事件，粒度对齐普通生成：**节点写完就上屏**。

## 与普通生成的差别

普通生成是一棵树一个 JSON，节点自带 `id`，父子关系能直接从 JSON 里读出来。（该路径已删除，
下面的对比保留是因为解析器与提示词仍按同样的形状工作。）

深层生成的每次展开调用长这样，**没有 id**：

```json
{"children": [{"label": "按章节通读教材", "expandable": false}]}
```

父节点是谁由调用方决定（这一路在展开哪个 slot），所以事件里的位置不能靠
JSON 内自带的 id，只能由工具层带上路径。

## 事件

沿用现有 SSE 信封 `{seq, type, ...}`，新增一种：

```json
{"seq": 31, "type": "node_grow", "path": [0, 2], "label": "按章节通读教材",
 "expandable": false}
```

中心主题自己用 `null`：

```json
{"seq": 1, "type": "node_grow", "path": null, "label": "考研数学复习计划",
 "expandable": true}
```

- `path`：从根出发的子节点索引链，指向**父节点**。`[0, 2]` 表示根的第 0 个
  孩子的第 2 个孩子；`[]` 表示挂在根上；**`null` 表示这一帧就是根自己**。

  根不能用 `[]`：`[]` 已经是「挂在根上的孩子」的意思，两者撞在一起时只能靠
  「根帧必须最先到」来区分。那个依赖是隐式的，发送顺序一变，前端就会把第一个
  分支当作中心主题，而且不会报任何错——错误的结果比失败难发现得多。用 `null`
  让两者在类型上就不同，根帧丢了也只是整棵预览树长不出来，不会长错。

- `label`：节点标题，已 `strip`，最长 200 字符（与 `Node.label` 一致）。
- `expandable`：模型对这个节点写出的 `expandable` 布尔值；模型还没写到、
  或写成非布尔时给 `null`。**`null` 与 `false` 语义不同**，前端不要当成同一件事。
  注意这一项与 `path` 的 `null` 含义无关，两者各自独立。
- 同一 `(path, label)` 只发一次。

## 顺序保证

- 父节点的 `node_grow` 一定先于它自己的孩子发出：孩子是父节点那次调用的产物。
- 根节点（`path` 为 `null` 的那一帧）最先发出，紧跟骨架 plan 之后。
- 并发展开多路时，不同 `path` 之间的事件可以任意交错；同一 `path` 内保持
  `children` 数组顺序。

## 收尾

`node_grow` 是**预览**。真正的结果只在 `snapshot` / `done` 帧里给出，它们来自后端
这一轮的工作树（`RunContext.candidate`；名字是历史遗留，不再含"等用户确认"的意思
——候选层删于 `acfdb5b`，`done` 一到整树就落进画布）。两者可能不一致：节点生长到
一半失败、某一路被裁掉、去重丢掉重复项，都会让最终树比预览少几个节点。所以预览
不能当成最终结果。

**但 `snapshot` 到了并不作废预览。** `snapshot` 只把整树写进工作副本 `draft`，不碰
`content`；此时画布还是空的，预览是用户唯一看得见的东西。这一帧清掉预览就会空
一帧，然后 `done` 又把整棵树塞进来——正是用户抱怨过的闪烁。所以预览一直撑到
`done` / `error`，只有终态才清。

## 后端职责

1. 工具内部的展开调用走流式请求（`stream: true`），边收 `delta.content`
   边做增量解析，找出已完整闭合的子项对象。
2. 每找到一个就通过 `RunContext` 上的通道上报，由 `service.run()` 转成
   `node_grow` 帧。
3. 任一环节失败都不影响主流程：流式只用于预览，失败就退回一次性解析。
   增量解析拿不到就少发几帧，绝不能因此让整次生成失败。

## 前端职责

1. 收到 `node_grow` 时按 `path` 在预览树上挂节点；父节点还没到时先缓存，
   等父节点到达再挂（顺序有保证，但客户端可能丢帧）。
2. 收到 `snapshot` 时把整树存进工作副本 `draft`，**不替换预览、也不清缓存**——
   预览要撑到 `done`，否则画布会空一帧（见上一节）。`done` / `error` 到达时才
   清预览与缓存。
3. `expandable === false` 的节点可以在界面上区别显示；`null` 不显示任何标记。

## `done` 帧上的模型原话与思考过程

`done` 除了整棵树（`content`，前端拿到就直接落画布并标成未保存），还可能在
`text` / `reasoning` / `model` 上带三样东西。规则：

1. `text`（模型收尾那一轮说的话）**只在没有产出导图时填**，截断到 4000 字
   （`MODEL_TEXT_LIMIT`）。有 `content` 说明这轮是正常结果，不需要把整篇输出塞进事件流。
   它是排障用的：线上出现过推理型模型把工具当成"需要时再用"，直接用文字把导图讲了一遍，
   于是 `content` 为 `null`，用户只看到"模型这次没有产出导图"。
2. `reasoning`（思考过程，`reasoning_content`）**成功时也填**——走推理模型
   （`deep_thinking=true` → `deepseek-reasoner`）时，思考过程正是用户勾那个开关要看的东西，
   成功时反而藏起来等于开关白勾。上限是 `REASONING_BYTE_LIMIT = 65536`——**编码后的
   字节**，不是字数，**也不是** 4000：实测思考过程就是 3000–8000 字（汉字 24KB、
   emoji 32KB、控制字符 48KB，全都整段过去）。为什么按字节：同样"2 万字"，汉字
   60KB、emoji 80KB、控制字符 120KB（JSON 把 `U+0000` 转义成 `\u0000`），按字数
   设上限等于让最坏那一档跑出 6 倍。超过上限时截在**字符边界**上（不切半个 UTF-8），
   并在文本里写明"已截断，原文 N 字"，不静默少一段。
   一帧 8000 汉字的 `data:` 约 24KB——SSE 按 `\n\n` 切帧、前端不设单帧上限，
   `.visioncheck` 那条 mock 上游链路实测这一帧照常落地。
3. `model` 是这一轮**真正发出去的模型名**（`deepseek-chat` / `deepseek-reasoner` / 配置值），
   由 provider 在发请求那一刻决定。缺字段＝没说（演示模式、按脚本回放的假 provider），
   界面据此显示"模型名未上报"，不替它认成 chat。
4. 契约上三者都是可选字段：老版本后端不发，前端也不应该因此报错。
5. 前端把 `reasoning` 放进悬浮球的消息时间线里，用 `<details>` 折叠展示全文——
   "模型当时在想什么"是这个开关唯一的可解释证据。

## `done` 帧上的 `partial`：半棵树也是交付

从前"没跑完"一律发 `error`：预算耗尽、工具超时、连续失败、空转收尾、意外收尾、
工具次数到顶，六条路径各自写了一份收尾逻辑，其中只有两条按规矩交出手上的树。
结果是用户看着它跑了三分钟，最后两手空空——而 `ctx.candidate` 里其实已经长出了
大半棵树，只是前端收到 `error` 会清掉预览、`content` 又一点没动。

现在统一走 `backend/app/agent/service.py::_deliver_or_fail`：
**这一轮产出过树就交树，一棵都没长出来才报错。**

"产出过"这个判据很具体，不是"候选位里有东西"：`edit` / `expand` 必须带
`request.snapshot`，那份快照就是用户自己的树，构造 `RunContext` 时它直接落进
`candidate`。按"候选非空"判的话，编辑意图下模型没配置、模型报错、工具全挂都会
返回 `done(partial=true, content=用户原树)`——前端把它当新结果落进画布、记一条
0 改动的生成事件，而"去配模型"那张卡片永远不会出现。所以判据是
`RunContext.produced`，由 `publish_tree()` 在四处产出点（`_generate` /
`_generate_deep` / `_expand` / `_commit_drafts`）统一置位。

台账读取接口（`GET /api/agent/runs/{id}` 与列表）同样带 `partial` /
`partial_reason`：只在实时流上说过一次的话，隔天点「载入」时那份半棵树与一次
完整生成长得一模一样。重开同一个 `run_id` 时这两个字段会被清空——它们描述的是
上一轮交付了什么，而 `finish` 的 `COALESCE` 会让旧值赖着不走。

```json
{"seq": 12, "type": "done", "content": {…}, "mode": "model",
 "partial": true, "partial_reason": "模型调用次数已用完（已执行 6 次工具调用）"}
```

- `partial` 与 `error` 是**互斥的两种收尾**，不会同时出现：有 `content` 就一定有
  `partial` 这条信息（要么缺字段，要么 `true`），没有 `content` 才走 `error`。
- 缺字段 = **没说**，不是"完整"。老后端、`_replay` 之外的正常收尾都不带这两个键
  （`exclude_none=True`，所以正常帧与加字段之前逐字节相同），**前端判断必须用
  `=== true`**。把缺字段当成"不完整"会让每次生成都挂着一条"部分完成"。
- `partial_reason` 与 `error.message` 同源（`_budget_message` 那一套），可以直接显示。
  它说的是"为什么没长完"，而这决定了用户下一步该做什么：额度用完该把目标写小，
  工具超时该等一会儿，两者不是一件事。
- 前端把它收进 `agentWarnings`（`kind: 'partial'`），状态行据此说"已落到画布
  （部分分支未完成）"。**不要改写成 `agentError`**：树已经在画布上、可以继续改，
  弹一张"这次没成功"的卡片与用户眼前的事实矛盾。
- 告警落地之后**不清**：用户改到一半才发现某几支是空的，那时比落地之前更需要这句话。

## 检查点：写说明之前那一份

`partial` 解决的是"收尾时手上还有树"，但它救不了一种更常见的失败：**进程根本没
走到收尾**。`runs` 表的终态由 `_frames` 的 `finally` 写，进程被杀/机器重启/容器
重建时那个 `finally` 根本不会跑，于是库里留下一条永远 `running` 的行，而它其实
已经攒了大半棵树——只是没有任何一帧把它带出去。

补的是一个**中途落库点**。它有四个位置，都在"下一段是长耗时模型调用"之前：

```python
# 创建（backend/app/agent/tools.py::_generate）—— 第四步（写说明）之前
if ctx.checkpoint is not None:
    snapshot = content.model_copy(deep=True)
    _stamp_expandable(snapshot, {node_id: "unknown" for node_id in cut_ids},
                      max_depth=mindmap_contract.MAX_TREE_DEPTH)
    await ctx.checkpoint(_shape_output(
        snapshot, diagram_type=diagram_type,
        raw_plan=raw_plan, diagnostics=[]))

# 按支展开（::_expand）—— 批次落树之后、生长预览与写说明之前
# 编辑（::_commit_drafts，`mindmap_edit` 与六个细粒度写工具共用）—— 同上
# 补说明（::_fill_details_tool）—— 补上之后、等模型收尾之前
ctx.publish_tree(updated)
ctx.batches.append(batch)
if ctx.checkpoint is not None:
    await ctx.checkpoint(updated)
```

三个位置的选择理由各不相同：

- **创建这条路**：只有此刻的树是契约合法的。**更早不行**——结构那段响应是流式解析的，
  中途的 `node_grow` 预览帧只有 `id`/`label`，没有 `detail`、没有 `source_refs`、没有
  `expandable`，把它写成 `candidate` 就是往库里塞一棵 `model_validate` 过不了的树。
  **更晚不必**——`publish_tree()` 那一刻 `on_close` 兜得住，那本来就是终态交付。
  而这一刻正好卡在整轮最长的一段前面：裁剪已做完、骨架的颜色与边已套上，接下来是两次
  并发调用写说明（56 节点实测 26 秒上下，见 `_fill_details` 的 docstring）。
- **创建这条路要多折一次形**：出口的 `_shape_output` 把内部树折成鱼骨/流程图，而检查点
  在它之前。直接存内部 `MindMapContent`，崩溃后捡回来的是一张思维导图——用户当初明明说
  要鱼骨图。所以落库前对**副本**先折一次（`to_fishbone`/`to_flow` 是纯函数，下游仍按一棵
  树工作），`diagnostics=[]` 给一次性空表（`_shape_output` 会往传入的列表里 append，快照
  不该替最终产物提前报诊断、更不该让同一批诊断算两遍）。
- **创建这条路还要在快照上补一次 `expandable` 打标**：正式那一次 `_stamp_expandable` 在
  检查点**之后**（它要用裁剪后的 `cut_ids`，位置由最终产物决定），所以快照里被裁掉子节点
  的那些会带着缺省的 `yes`——用户点开以为还能长，点下去却报 `too_deep`，而被裁的恰好是
  唯一还能继续长的地方。快照上按同一张表补成 `unknown`（"没长完，不是到底了"），与最终
  交付那份的标记口径一致。
- **编辑 / 展开这两条路不需要折形**：`updated` 是 `_apply_ops_any` 的产物，本来就是最终
  图型（`_shape_output` 只服务创建那条路）。它们保的是"已经改完的那一批 op"——一次二十分钟
  的编辑在写完说明前崩掉，若不打这个点，用户重新打开看到的还是**编辑前**的树。
- **补说明这一条路打点最划算**：它本身就要跑二十几秒（`_fill_details` 与创建那条路同一个
  量级），跑完 `publish_tree` 之后还要再等一次模型往返收尾。这段窗口里进程没了，补好的说明
  全丢，库里退回"结构完整、说明为空"——恰好是这条路要消灭的那个状态。判据与 `publish_tree`
  一致：一个都没补上（`applied == 0`）就不打点，否则等于给下一轮留一份和进来时一模一样的
  "成果"。

`ctx.checkpoint` 是 `async` 的，回调实现方自己把阻塞的写交给线程池
（`CheckpointFn = Callable[[DiagramContent], Awaitable[None]]`）。工具跑在事件循环上，
同步写库等于拿整个循环去等 SQLite 的写锁，同进程其它 SSE 会一帧都发不出来。

三个约束：

1. **只对 `running` 生效**（`WHERE status = 'running'`）。迟到的检查点不许把成品
   盖成半棵树——取消路径上工具里的检查点调用可能晚一步到达。
2. **交出成品时清掉标记**（`finish` 里的 `CASE WHEN ? IS NULL`）。`checkpointed_at`
   有值 = "这份 `candidate` 是跑到一半截下来的"，成品带着标记会让界面一直提示
   "说明还没写"。
3. **写失败不抛**。检查点自己出问题不该让一轮本来能成功的生成失败。写锁等 2 秒
   （`CHECKPOINT_LOCK_TIMEOUT`）就放弃：检查点是"少丢一点"的补丁，为它多等一秒都
   是这一轮在等。

读侧两个字段（`GET /api/agent/runs` 与 `/{id}`）：

| 字段 | 含义 |
| --- | --- |
| `checkpointed_at` | 这份结果是检查点。**结构是全的，`detail` 是空的** |
| `node_count` | 检查点里有多少节点。只在这些行上算一遍，列表 20 条不值得数 |

它与 `partial` **是两件事**，不要合并：`partial` 说"树没长完"，`checkpointed_at`
说"树长全了、描述没写"。用户下一步完全不同——前者要重跑或接着补，后者直接载入
就能改。前端因此把它记成 `kind: 'diagnostic'` 而不是 `partial`。

**覆盖范围**：只有 `create` / `edit` / `expand` 三条产品意图。`create_deep`
（`mindmap_generate_deep`）不打点——`_ALLOWED_BY_INTENT` 里它只挂在 `create_deep`
下，而 `service.run` 永远不会把请求解析成它（那是"一次响应写不下 200+ 节点"的备用
路径，未注册到任何入口），所以它在生产上不可达。

崩溃残留还有半个问题：那条 `running` 行会让同一个 `run_id` **永远 409**
（`begin()` 见 `running` 就拒绝认领），而 `pending()` 只看终态，检查点存得再好
也没人读。所以 `begin()` 在认领之前先跑一次 `runs.sweep_abandoned()`，判据有两条，
**缺一不可**：

1. 不在本进程的活跃登记里（`runs._ACTIVE`，由 `_frames` 在生成器第一行 `active()`、
   `finally` 里 `inactive()`）。单靠时间判不出来：一轮生成里最长的静默是**单次模型
   调用**，它受 `max_seconds` 约束、上限 3600 秒，比下面那个阈值还长。
2. `updated_at` 超过 `RUN_ABANDONED_SECONDS`（1800 秒，是线上默认 `max_seconds` 的
   两倍）。自己还在跑的 run 一律不扫，1800 秒只对"登记表里没有、又很久没动"的行生效。

判中的行变成 `failed` + `failure.code = "run_abandoned"`，**不动 `candidate`**（清理的是
状态，不是那棵树）。放在 `begin()` 里而不是启动钩子，是因为这台机器只有一个 worker、
也可能长期不重启：启动时清一次，故障后第一批用户仍然会撞 409。判错的代价是自愈的——
真正还活着的运行收尾时 `finish` 仍能改动（它只对 `done` 冻结）。

## 从检查点继续：补说明，不重跑

检查点把"结构已经长好、说明还是空的"这一份救了下来，但它只解决**留下**。用户载入
之后仍然缺那些说明，而从前的唯一补救是重跑一次生成——再花三次模型调用、十几秒，
还可能换出一棵不一样的树，把用户刚看着觉得满意的结构顶掉。

补的是一条独立的路：`mindmap_fill_details`，只写 `detail`，结构一个字节都不动。

| 决定 | 理由 |
| --- | --- |
| **复用 `intent="edit"`**，不新增意图 | 它本来就要求带 `snapshot`、本来就是"在现有这棵树上改"，而补说明要的正是这两件事。多一个意图要同时改 `_validate`、前端三道闸门、按意图取选区的三处分支，换不来任何表达力 |
| **沿用那条 `run_id`** | 服务端那一行还在（`cancelled` / `failed`），`begin()` 对这两种状态是**重开同一行**：清 `partial` 标记、换掉 `request` 摘要、不动 `candidate`。补完的树落回同一行，台账上这是同一次生成的下半场。换新 id 会让一次生成变成两次运行，而用户看到的明明是同一张图。那一行若还在 `running`（进程没死、只是前端断过线），`begin()` 给 409——那也正是对的，真正在跑的那一轮不该被第二个请求顶掉 |
| **不给 `node_ids`** | 要补哪些由 `_detail_targets` 按 `DETAIL_FILL_FLOOR`（长度 < 40）数出来。模型看不到整棵树，让它圈只会漏；而"少写一个"没有任何兜底能发现 |
| **只加 `instruction`** | 口吻/长度/侧重是模型真能做主的，给一个字符串就够。`count`、`max_tokens` 这类旋钮留给代码算 |
| **不用 `_intake`** | 那条路在"没有材料"时会发起外部检索，等于给一棵按旧依据长出来的树塞新依据——写出来的说明与结构对不上。补说明改用 `_expand_sources`：按整棵树声称用过的 `source_refs` 从知识库把原文找回来，找不到就按"无原文"写 |

两条硬边界：

- **非树直说**。`_fill_details` 的签名只接 `MindMapContent`（鱼骨/流程的说明在生成时
  跟着节点一起写了），给 `not_tree` 而不是崩在 `content.root` 上。
- **一个都没补上不算产出**。`ctx.publish_tree()` 只在真的补上节点时才调：那棵树与进来
  时逐字节相同，"产出过东西"若为真，之后某条额度耗尽的路径就会把"什么都没发生"
  包装成一棵半成品交付。

界面上它不是一个单独入口，而是挂在那条「上次没接住的结果」提示上：**那一行能被重开
（`cancelled` / `failed`）时就出现**，点它先载入检查点、再用同一条 `run_id` 发一轮
`edit`。顺序写死在 `ChatDock.vue::fillDetailsAfterLoad` 里，`run_id` 必须在载入**之前**
读——载入成功会清 `resumableRun`，之后再读只会拿到 null，然后悄悄新开一个身份。

判据为什么不是 `checkpointed_at`：那个时间戳只在**收尾时交出候选**的那一刻被清，而
"补说明"失败那一轮照样会交出 edit 的快照，于是失败一次之后按钮就永久消失了——用户明明
还缺说明，文案却退化成"生成完的导图还没保存"。而"能被重开"才是"这次点下去真的会跑"的
判据：`begin()` 对 `cancelled`/`failed` 是重开同一行，对 `done` 是幂等重放（点了也不会补，
只会把旧结果再放一遍）。何况树上没有可补的节点时工具零调用返回 `nothing_to_fill`，多摆
一个按钮不花任何额度。

## 错误码与界面的下一步

`error` 帧的 `code` / `limit` 决定界面给什么按钮、按钮能不能点。映射是纯函数
`frontend/src/mindmap/failures.ts::guideForFailure`（逐条有用例），文档这里只记**为什么**
某个码必须给某一种下一步——这些理由丢了，后来的人会把它当成可以合并的文案分支。

| `code` | `limit` | 用户能做什么 |
| --- | --- | --- |
| `model_not_configured` | — | 改服务端环境变量，**重试没用** |
| `budget_exhausted` | `max_seconds` / `max_model_calls` / `max_tool_calls` / `max_repairs` | 把目标写小再试 |
| `too_many_failures` | `max_repairs` | 先查材料，再把目标拆小 |
| `unexpected_finish` | — | 说得更具体再试（被长度或内容过滤截断） |
| `generation_failed` | — | 直接重试（上游限流/临时故障） |
| `agent_failed` | — | 重试；反复出现就把原话发给维护的人 |
| `no_progress` | `no_progress` | 把目标拆成几件小事 |
| `run_in_progress` | — | 等这一轮结束（同一个 `run_id` 不能重叠跑） |
| `invalid_sources`（前端合成） | — | 改输入：材料还没解析好 |
| `no_content`（前端合成） | — | 重试；这也是最容易过去的一种 |
| `connection_lost`（前端合成） | — | 重试，并提醒会再花一次额度 |

三条硬规则：

1. **`model_not_configured` 是唯一一个把重试按死的失败。** 其它 `GenerationError`
   （上游 5xx、超时、响应不合规）重试有意义；配置没填时重试一百次也是同一个结果。
   它在后端是 `backend/app/generation/service.py::ModelNotConfigured`（`GenerationError`
   的子类，`_config()` 抛它），`except ModelNotConfigured` 必须排在 `except GenerationError`
   之前——顺序写反就退回成"上游出错"，界面会给出一个骗人的重试按钮。
2. **被禁用（`disabled`）的按钮必须写明原因。** 只置灰等于沉默：用户看着一个点不动的
   「重试」，只能猜是自己点错了；而被禁用的按钮在多数浏览器里连 `title` 悬停提示都不弹。
3. **后端原话折在 `<details>` 里，不当第一句话。** 那句话是写给写代码的人看的。
   原话与 `code=` / `limit=` 一起保留，因为排障时它是唯一能与后端日志对上的线索。

## `heartbeat` 帧

模型思考期间这条流会长时间一个字节都不发。线上实测（2026-11-16，GLM-5.3-Flash，
3 路并发）最长静默 **25.1 秒**，单路首字延迟 5–10 秒。中间链路的空闲超时常见是
30–60 秒，静默超过它就会掐断连接——那种断法在页面上表现为"生成到一半没动静了"，
既没有 `error` 也没有 `done`。

```json
{"seq": 7, "type": "heartbeat", "waited_ms": 10200}
```

- `waited_ms`：**本次静默**已经持续多久（毫秒），不是整次运行的耗时。每次静默
  从 0 重新计，所以它可以用来判断"还要等多久"的体感，但不能累加成总时长。
- 触发间隔 `HEARTBEAT_INTERVAL_SECONDS = 10.0`（`backend/app/agent/service.py`）。
  取 10 秒的依据：必须小于最紧的常见空闲阈值 30 秒，同时不能太密——每次唤醒
  都要走一遍 SSE 编码与网络写。
- 两个静默点都发：模型调用 `await` 期间（主因）与工具执行期间的等待队列上。

规则：

1. **心跳不代表任何业务进度**。它不带阶段名，前端不应因为收到它就改状态行文案。
   真实进度看 `stage` / `tool_start` / `tool_end` / `node_grow`。
2. 前端把 `waited_ms` 换算成秒用于显示；**任何非 `heartbeat` 的帧到达时都要把等待
   计时归零**，否则"已等 25 秒"会一直挂在状态行上，看起来像卡死，而那会儿节点
   其实已经在长。
3. 静默达到 10 秒时状态行文案换成"模型正在思考，已等 N 秒"，替掉原来的阶段文案
   （不是两句并列）——阶段文案描述的是上一个动作，此刻它已经不成立了。
4. 心跳是**保活手段，不是保证**。客户端断线仍可能发生，所以"既没 `done` 也没
   `error` 就 EOF"必须继续按"连接中断"处理，不能因为有心跳就当成正常结束。

## `sources` 帧

材料在**生成开始时**才进知识库（`backend/app/knowledge/remember.py`），入库拿到的
是库里的持久 id，与前端上传解析时拿到的临时 id 不同。这条帧就是把映射送过去：

```json
{"seq": 1, "type": "sources", "mapping": {"3f2c…(临时)": "9a71…(持久)"}}
```

- 入库排在所有数据帧之前，所以流一开始先刷一行 **SSE 注释帧**
  `: 正在把材料收进知识库`，让代理与浏览器立刻看到"连接已建立"——入库最多要等
  一批 sqlite 写，这段静默落在首帧之前时，客户端连响应头都还看不到。注释帧不是
  数据帧：`postSse` 只认 `data: ` 行，也不会占用 `seq`。
- 只列**真的换了 id** 的那些。空映射不发这一帧（无材料、入库关了、或全部入库失败）。
- 位置固定在第一帧，早于任何 `stage` / `node_grow` / `snapshot`：前端要在第一张
  增量节点到达之前就知道"手上的来源 id 变了"，否则那一轮图里的引用一开始就指错。
- 后面帧的 `seq` 接着这一帧往下编（`run(..., first_seq=seq + 1)`），不会出现两帧
  都叫 `seq=1` 的情况。
- 前端收到后把本地材料条目的 id 就地换掉（`sources.remap`）：只换 `item.id` 与
  `item.source.id`，**行身份 `key` 不动**——`key` 是列表 key 与 `files` 映射的键，
  换了它已解析的文件就再也找不回来，而 `item.id` 才是会话内反向索引与"被引用次数"
  用的 id。两份内容相同的材料会换到同一个持久 id，所以列表渲染必须按 `key` 而不是
  `id` 去重。
- 入库失败不影响这一轮：该材料保留原 id 继续参与生成，只是没进库（下一轮打开
  导图时取不回它的正文）。
