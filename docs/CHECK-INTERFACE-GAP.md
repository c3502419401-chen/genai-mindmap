# 结构体检只收树型：那 12 条鱼骨 / 流程诊断卡在哪一层

一句话：**上一定论说它们"判据干净、只被 `/check` 的形状挡在外面"，只对 2 条成立；
另外 10 条不是被接口挡住的，是被"产物里根本没有这个信息"挡住的。**

复算入口（需要有 `data/acc_trees*`，`data/` 不进 git）：

    cd backend && python scripts/_probe_diagram_check_sides.py

它拿 72 棵真树跑**生成出口本身**（`agent/tools.py::_shape_output`），同时在两个位置收
读数：生成侧（那一刻真产出了哪些码）与产物侧（只读折出来的 `FishboneContent` /
`FlowContent` 能重算出哪些码）。下面所有数字都出自它，不靠读代码推测。

---

## 一、先把数改对：是 12 条，不是 13 条

上一轮写的是"13 条（`FISHBONE_*` 6 + `FLOW_*` 7）"。实际字面量是 **`FISHBONE_*` 5 条 +
`FLOW_*` 7 条 = 12 条**：

    FISHBONE_PROBLEM_MISSING        FISHBONE_PROBLEM_ECHOES_TITLE
    FISHBONE_CATEGORIES_INCOMPLETE  FISHBONE_CATEGORIES_UNFIXED
    FISHBONE_EXTRA_BRANCH
    FLOW_ROLE_START_DEFAULTED       FLOW_ROLE_END_DEFAULTED
    FLOW_ROLE_END_MOVED_UP          FLOW_ROLE_END_OVERRIDES_DATA
    FLOW_EDGE_DROPPED               FLOW_EDGE_DEMOTED       FLOW_BRANCH_UNRESOLVED

判据是 `check_diagnostic_sides.py` 的静态那一半（`app/**/*.py` 里
`Diagnostic(code="…")` 的字面量，12 条全部在 `app/agent/tools.py`）。上一轮那份文档
自己的附录列的也是 5 + 7，只是正文的合计写错了——所以整批归类的 63 条应为
**12 + 51**，`docs/DIAGNOSTIC-TWO-SIDES.md` 已按此更正。

---

## 二、逐条核实：判据函数、签名、纯不纯

12 条的产生点只有四个函数，**没有一条收 `DiagramContent`**：

| 码 | 产生点 | 判据签名 | 是判据吗 |
|---|---|---|---|
| `FISHBONE_PROBLEM_MISSING` | `_fishbone_problem` | `(raw_plan, title, diagnostics)` | 判据，但读**原始计划** |
| `FISHBONE_PROBLEM_ECHOES_TITLE` | `_fishbone_problem` | 同上 | 同上 |
| `FISHBONE_CATEGORIES_INCOMPLETE` | `_fishbone_shape_diagnostics` | `(FishboneContent, diagnostics)` | 判据，读产物 + **环境变量** |
| `FISHBONE_CATEGORIES_UNFIXED` | `_fishbone_shape_diagnostics` | 同上 | 同上 |
| `FISHBONE_EXTRA_BRANCH` | `_shape_output` | `(MindMapContent, *, diagram_type, raw_plan, diagnostics)` | **不是**：读 `to_fishbone` 的返回值 `extras` |
| `FLOW_ROLE_START_DEFAULTED` | `_stamp_flow_roles` | `(FlowContent, diagnostics) -> FlowContent` | **不是**：这是**变换**，它边改产物边报"我改了什么" |
| `FLOW_ROLE_END_DEFAULTED` | `_stamp_flow_roles` | 同上 | 同上 |
| `FLOW_ROLE_END_MOVED_UP` | `_stamp_flow_roles` | 同上 | 同上 |
| `FLOW_ROLE_END_OVERRIDES_DATA` | `_stamp_flow_roles` | 同上 | 同上 |
| `FLOW_EDGE_DROPPED` | `_shape_output` | 同上 | **不是**：读 `to_flow` 的 `FlowEdgeLoss.dropped` |
| `FLOW_EDGE_DEMOTED` | `_shape_output` | 同上 | **不是**：读 `loss.demoted` |
| `FLOW_BRANCH_UNRESOLVED` | `_shape_output`（上游 `_flow_branch_edges`） | 读 `flow_branches`（**原始计划**）+ `MindMapContent` | **不是**：判据落在**输入**上 |

`_fishbone_shape_diagnostics` 看起来是标准的"纯函数 + 产物可判"，其实是
`内容 + 环境变量`：`fishbone_free_categories_enabled()` 决定走哪一支。开关状态**没有
记进产物**，所以事后再判只能按"现在"的开关算——而它可能与生成那一刻不同。

---

## 三、产物侧实测：只有 2 条重算得出来

72 棵真树，每棵折 3 次（三种 `problem` 写法），开关关：

| 码 | 生成侧产出 | 产物侧可判 |
|---|---|---|
| `FISHBONE_CATEGORIES_INCOMPLETE` | 216 | 216 |
| `FISHBONE_PROBLEM_ECHOES_TITLE` | 72 | **144** |
| `FISHBONE_PROBLEM_MISSING` | 72 | 0 |
| `FISHBONE_EXTRA_BRANCH` | 18 | 0 |

同一个开关**打开**：

| 码 | 生成侧 | 产物侧 |
|---|---|---|
| `FISHBONE_CATEGORIES_UNFIXED` | 216 | 0 |
| `FISHBONE_CATEGORIES_INCOMPLETE` | 0 | 216 |

流程（72 次折叠，产物共 4050 个节点）：

| 码 | 生成侧 | 产物侧 |
|---|---|---|
| `FLOW_ROLE_START_DEFAULTED` | 72 | 0 |
| `FLOW_ROLE_END_DEFAULTED` | 72 | 0 |
| `FLOW_EDGE_*` / `FLOW_BRANCH_UNRESOLVED` | 0 | 0 |

四条硬结论：

1. **`ECHOES_TITLE` 的 144 是 72 + 72。** 代码给 `problem` 兜底时用的就是标题本身
   （`_fishbone_problem` 的 `return title`），所以事后看，"模型没写"与"模型照抄主题"
   在产物上是**同一个形状**。两条码搬过去会合成一条，而且会**多报一倍**。
2. **`EXTRA_BRANCH` 的 18 → 0。** `shapes.to_fishbone` 已经把第 7 根起的一级分支并进
   最后一根骨；触发过的那 6 棵树，产物里骨数全是 **6**。信息在转换那一刻就没了。
3. **`FLOW_ROLE_*` 的 72 → 0，是最刺眼的一条。** 这四条是"代码补了角色"的记录，
   而补角色本身就是**抹掉痕迹**的动作——补完之后产物里 start / end 都在，判据
   `"start" not in roles` 永远为假。也就是说这条码的语义是**"代码改过它"，不是
   "产物是这样"**；想事后判，得先把"这个角色是谁标的"记进产物。
4. **三条损失 / 输入类（`EDGE_DROPPED`、`EDGE_DEMOTED`、`BRANCH_UNRESOLVED`）在
   72 棵真树上连生成侧都是 0 次**：那批树 `edges` 全为空（72/72），没有任何一条边
   可丢；而 `FLOW_BRANCH_UNRESOLVED` 的输入 `flow_branches` 来自原始计划，产物里
   没有这个东西。它们的 `loss.dropped / demoted / inner` 三个数只存在于 `to_flow`
   的返回值里。

所以：**产物可判的只有 2 条**（`PROBLEM_ECHOES_TITLE`、`CATEGORIES_INCOMPLETE`），
且各有代价——前者与 `MISSING` 撞车，后者依赖一个没记进产物的开关。

---

## 四、卡在哪一层：**c，两端都卡**（判据是"只改后端，前端会怎样"）

| 层 | 位置 | 现状 |
|---|---|---|
| 接口 | `optimization/router.py:31` `_tree_content()` | 非树一律 **422**（`_TREE_ONLY`），闸在路由层，不在 `check()` 里 |
| 前端 store | `stores/structure.ts:202` | `if (!isTree(content))` → **不发请求**，直接返回 `'unsupported'` 并写一句"只支持思维导图" |
| 前端 store（修复） | `stores/structure.ts:385` | 同上，`applyRepair()` 也拦 |
| 前端类型 | `api/client.ts::checkOptimization` | **不挡**：`content: DiagramContent`，三个函数早就按三种图型写的类型 |
| 前端面板 | `components/StructureCheck.vue` | **不挡**：按 `code` 通用渲染（文件头写着"没有任何 `if (code === …)`"） |

判据很干脆：**只放开后端，前端仍然 100% 报 `unsupported`**，因为那道闸在 store 里、
且位置在 `fetch` 之前。所以是 c。

`router.py:18-27` 那段"不是泛化，是这里不适用"读全文之后，理由分两半：

- 有效的半：`SINGLE_CHILD` / `HEADING_ONLY_BRANCH` / `SHALLOW_BRANCH` / `MISSING_DETAIL`
  这些判据是**按树的形状**写的（"只有一个子节点""分支深度对比"）。套到鱼骨上，
  六根并列的骨就是六条噪声——这一段判断今天仍然成立，不该推翻。
- 过期的半：它当时是**在 `500` 与 `422` 之间做选择**（原文记着 `AttributeError: no
  attribute 'root'` → 500）。它没有主张"鱼骨永远不该有体检"，只主张"把树判据套上去
  是错的"。**鱼骨/流程该有的体检是另一套判据**，而那套判据里已经 12 条写好了。

结论：不是"接口不该泛化"，也不是"只差前端"。真实形态是**三件事都有活**——
先要有鱼骨/流程自己的判据（已写 2/12）、再要接口按图型分派、最后要前端放开 store。

---

## 五、如果要做，完整清单与成本

按"最小可用"排，每档都能独立停：

| 档 | 做什么 | 成本 | 能不能停在这 |
|---|---|---|---|
| A | `optimization/` 里新增 `check_fishbone()` / `check_flow()`，只搬产物可判的 2 条；`/check` 按 `isinstance` 分派 | 后端 ~60 行 + 2 条用例；`app/optimization/router.py` 的 `_tree_content` 拆成 `_tree_or_diagram` | 能。这一档**不改前端也安全**（前端仍报 unsupported，只是不再 422 而已） |
| B | 前端 `stores/structure.ts:202` 放开 + 文案改；`StructureCheck.vue` 一行不改 | ~10 行 + 1 条 vitest | 能。到此用户第一次能在鱼骨/流程上看见诊断 |
| C | 把"产物里没有的信息"补进产物（`FishboneContent` 加 `extras` / 开关状态，`FlowContent` 加 `loss` 或 `role_source`） | 契约变更 → `openapi.json` + `schema.d.ts` 都要重导；旧图兼容（字段可缺省） | 能，但这是**另一件事**：它改的是产品形状，不是接口 |
| D | `/suggest` + `/repair` 也放开 | 大：`suggest_all` 的 `_sibling_groups`、`repair` 的三条规则全部按树写 | 建议**不做**，除非有鱼骨/流程的"建议"设计 |

★ 关键取舍：**A + B 才是"这 12 条有出口"的最小闭环，而它只让 2 条真正可见。**
C 是把剩下 10 条救出来的唯一路径，但它不是接口问题——是"生成侧做过的事要不要留痕"
这个产品问题。所以这 12 条**该等的不是 `/check` 泛化，而是 C 的那个决定**；而 A+B
只对这 2 条有意义。

---

## 六、CI 上跑哪一半

`scripts/check_diagnostic_sides.py` 已经进 CI（backend job，紧跟「文档断言与代码一致」），
跑的是 `--require-documented`，**只有静态那一半**：

- 运行期那一半要 `backend/data/acc_trees*` 的 72 棵真树，而 `data/` 在 `.gitignore:37`
  ——干净 checkout 上不存在；
- 脚本因此先看有没有树：没有就**声明"运行期未测量"**，而不是打印"真树上产出 0 个码"
  （后者把"没测"说成"测了、是零"）。没有树时它**不 import `app`**，所以是纯标准库，
  排在 Test 之前与「文档断言」相邻是对的；
- `--require-documented` 的判据只落在静态那一半（`other_only` 完全由静态得出），所以
  CI 与本地读数相同：**87 个码 / 72 条 other_only / `[ok]`**（已用一个干净 worktree
  对过：exit 0，逐行一致）。

---

## 七、自评：这个仓库还有多少"后端做了、前端没接"

已经撞过的六处，按"哪道闸看得见"分三档：

| 形状 | 例子 | 谁看得见 |
|---|---|---|
| 后端函数零引用 | `link.suggest` 零调用、账本三条 | `scripts/acc3/find_unwired.py`（棘轮） |
| `client.ts` 函数零调用 | `deleteMindMap` 那一次 | `frontend/scripts/find_unwired.mjs`（棘轮） |
| **后端有路由、`client.ts` 一行都没有** | `/knowledge/search` 桩、记忆整块、`/ingestion/text` | **没人看得见** ← 缺的就是这一环 |

第三档的读数（`openapi.json` 53 个操作 vs `client.ts` 里 34 个归一化路径）：

- 上一轮报的是 **8 个操作一次都没出现**：`POST /ingestion/text`、
  `GET /ingestion/formats`、`POST /agent/tasks`、`POST /knowledge/search`、
  `GET|POST /memory`、`DELETE /memory/{id}`、`GET /memory/search`。
  按功能是五组：材料文字入库、格式清单、agent 任务提交、知识检索、记忆整块。
- 另有 4 个（`/export/{json,markdown,opml,puml}`）是**同一条动态路径**
  `/export/${format}`，属误报——归一化必修的一课。
- 反方向 1 条：`POST /optimization/repair` 在路由里但 `include_in_schema=False`
  （`router.py` 末尾写着为什么：账本系统那一轮在改快照），所以它不在 `openapi.json`
  里，基于快照的闸会把它误报成"前端有、后端没有"，需要显式白名单。

**8 这个数偏小，而且是判据口径造成的偏小**（这一轮建闸时才发现）：那一轮数的是
"路径在 `client.ts` 里出现过没有"，而 `PUT` 与 `DELETE` 共用同一串 `/prefs/${key}`，
于是 `GET /api/prefs/{pref_key}` 被这对兄弟盖住了。换成 **method + 路径**的口径之后是
**9 条**，多出来的正是它。所以下面所有读数都以 method 级为准；路径级那个 34 只用来
和上一轮对账（脚本两栏都打）。

**建的是什么**：`frontend/scripts/check_route_coverage.mjs`——用 `typescript` 的语法树
（依赖已有，`find_unwired.mjs` 就是这么做的）还原 `client.ts` 里所有请求路径，按
**method + 归一化路径**与 `backend/openapi.json` 求双向差集。取 `check_` 而不是上一轮
文档里写的 `find_`：这个仓库的 `find_*` 是"枚举出清单"（`find_unwired`），`check_*`
是"闸门"（`check_test_cost` / `check_diagnostic_sides`），而这一件的主要用途是后者。

读数（`node scripts/check_route_coverage.mjs`）：

    后端操作（去 /api 前缀）      53
    client.ts 归一化路径          34（method+路径 42）
    已覆盖（精确）                40
    已覆盖（参数化声明）            4   ← /export/{} 那四条
    ★ 断链                        9
    ★ 死链                        1   ← /optimization/repair，白名单

- **能自动化、且判据确定**：两份输入都是文本（`openapi.json` + `client.ts`），不读运行时、
  不读数据库、不读 git 状态；唯一脆弱点是路径归一化（`/x/${id}` ↔ `/x/{map_id}`、
  查询串不参与、`${id}${query}` 的尾部合并），这一条写进 docstring 就是可预期的边界。
- **进了 CI**：`.github/workflows/ci.yml` 的 frontend job，`断链棘轮（路由 → client）`
  一步，位置在「契约类型」之后（那一步保证快照与后端一致）、「Build」之前。用**棘轮**
  而不是绝对零：基线 = 上面 9 条 + `repair` 的白名单 + `export` 的参数化声明，
  否则第一天就红，然后被人关掉。
- **它不覆盖什么**：它只证明"`client.ts` 引用了这个路径"，不证明"组件调了那个函数"
  ——后者是 `find_unwired.mjs` 的活。两级串起来才闭环：
  **路由 → `client.ts` → 组件**，缺一级就有一种"后端做了、界面没有"能藏住。

### 归一化与那条唯一放宽

| 形状 | 处理 | 例子 |
|---|---|---|
| 后端占位名 | 一律塌成 `{}`（两侧名字从来不同） | `{map_id}` / `{item_id}` / `{job_id}` → `{}` |
| 模板串插值 | 每个 `${…}` → `{}`；`${BASE}` 是**空前缀**不是占位 | `` `/mindmaps/${id}` `` → `/mindmaps/{}` |
| 尾部连续占位 | 合并成一个（第二个通常是查询串变量） | `` `/knowledge/library/${id}${q}` `` → `/knowledge/library/{}` |
| 查询串 | 截掉 `?` 之后 | `` `/agent/runs?${qs}` `` → `/agent/runs` |
| `/api` 前缀 | 两侧都去（`request()` 拼 `` `${BASE}${path}` ``） | `/api/memory` → `/memory` |

占位名不参与比较，**段数与字面段参与**——`/memory/{}` 与 `/memory/search` 是两条，
因为它们的路由语义本来就不同（`memory/router.py` 专门写了一段"`/search` 必须排在
`/{item_id}` 之前"）。

唯一放宽是**参数化覆盖**，而且必须显式声明（基线里的 `param_cover`）：`/export/${format}`
一行代码对后端 4 条字面路径。脚本**不会**让 `{}` 自动匹配任意字面段——那会让
`/memory/{}` 顺手吞掉 `/memory/search`，真断链藏起来而读数变漂亮。声明的代价是每条
要写清它覆盖了谁；收益是"吞掉"这件事不可能悄悄发生。

---

## 八、那 9 条的逐条判定

判据只有一句：**这个功能该不该有界面入口**。四档不是形容词，是"下一轮该做什么"的
指令：

| 档 | 意思 | 下一轮 |
|---|---|---|
| `gap` | 该接而没接 | 接上，接完从基线删掉 |
| `stub` | 后端自己说是桩 | 等它产出真图再接 |
| `intentional` | 有意不接，理由在别处 | 不改；判据留在基线里防它悄悄变味 |
| `observability` | 不是用户功能（运维/调试面） | 不改；要做的是产品决定，不是接线 |

| 操作 | 档 | 依据 | 该等什么 |
|---|---|---|---|
| `GET /ingestion/formats` | **gap** | 后端 docstring 就是对着前端许的愿：『列出当前支持的文件类型与上传上限，前端可据此限制文件选择框』。而前端把它抄成了一份硬编码常量 `sources/input.ts::FILE_ACCEPT`（20 个扩展名）。两份清单现在一致，但一致是**人工对出来的**：后端 `AUDIO_SUFFIXES` 里补一种格式，前端 `accept` 不会跟着变 —— 用户选不到后端明明支持的文件；反向要等用户选了才吃 422 | 现在。一个 GET 换掉一份手抄清单，是全批最便宜的一条 |
| `POST /knowledge/search` | **gap** | 知识库面板已接三条（看清单 / 点开正文 / 删），唯独没有"在库里找"；而这条端点的 docstring 明确写着它是给界面用的：『空结果有两种原因，界面必须分开说：`hits` 为空而库里有材料 → 没检索到；库本身是空的 → 先去用一份材料生成一张图』。也就是说那两句文案的判据在调用方，而调用方至今不存在。库小的时候列表能替代；库一大（`prune` 的口径 90 天 / 200 条）就只能翻 | 现在，排在格式清单之后 |
| `POST /agent/tasks` | **stub** | 后端自己写着『当前只返回阶段骨架，不产生导图』（`agent/router.py:71`，模块头也写『`/tasks` 是早期留下的阶段规划桩，保留给旧调用方』）。接到界面上就是接一个假按钮：点了有响应、画布没有变化。真正的入口 `/agent/runs/stream` 早就接了 | 等它产出真图；那一轮顺手把桩删掉 |
| `POST /ingestion/text` | **intentional** | 后端 `parse_text` 的全部实现是 `strip() → Source(kind, text, status="parsed")`（`ingestion/service.py:106`），前端 `sources/input.ts::textSource` 造的是同一个形状、同一个 `strip`，本地零往返。`ChatDock` 的粘贴分流（>120 字自动变材料卡、可一键撤回）依赖的正是这份即时性；改走网络等于为一个 `strip` 换一次往返加一个加载态 | **重新判的条件**：哪天后端 `parse_text` 开始做真事（正规化 / 切分 / 字数闸），这条就从 `intentional` 变 `gap` |
| `GET /prefs/{pref_key}` | **intentional** | 后端加它的理由只有一条，且与界面无关：『`/api/prefs/{pref_key}` 之前只挂了 `PUT`/`DELETE`，于是 `GET /api/prefs/style` 会命中这条路径却被判成方法不允许（405）……现在未知键是 404』（`prefs/router.py:65`）。界面走 `listPrefs()` 一次拿全部（面板本来就要全列），单条读取对它是多余的一次往返 | 不改。★ 这条是 method 级口径新抓出来的（上一轮被 `PUT|DELETE /prefs/${key}` 盖住） |
| `GET /memory` | **observability** | 真正的消费方是生成管道：`agent/service.py:1150` 调 `memory_store.prompt_for(...)` 检索注入。HTTP 面那四条的存在理由是让链**可观察**——`memory/router.py` 的模块 docstring 明写手工入口是为了把『空表 → 总结 0 条 → 检索 0 条』这条本来要等几周的链从中间接上，『三件本来要等很久才能观察的事，现在一次请求就能看到』；这条 `GET` 自己则被写成『死表探测器』 | 不改。要做的是第九节那个产品决定 |
| `POST /memory` | **observability** | 同上。手工写一条是给开发者把链接上：『写一条进去，立刻就能用 `GET /search` 看它有没有被检索到、score 是多少、衰减有没有生效』。`source` 字段把 `user_explicit` 与 `summary` 分开，正是为了让它能替代观察而不污染数据 | 同上 |
| `DELETE /memory/{item_id}` | **observability** | 同上，是手工入口的配套（『想改内容就删了重写——`DELETE` 就在隔壁』），并带 owner 边界：别人的记忆回 404 而不是 403，因为"这个 id 存在但不属于你"本身就是不该泄漏的事实 | 有面板那天它才变 `gap` |
| `GET /memory/search` | **observability** | 理由最硬：它返回 `cosine` / `decay` / `score` / `age_days` 四个中间量，为的是让『这条为什么没进 top_k』当场可读——调参视角，不是使用视角。前端零引用（除生成的 `schema.d.ts`）；生成链路用的是同一份 `store.recall`，只是不经过 HTTP | 同上 |

死链那 1 条（`POST /optimization/repair`）不是缺口，是**快照的盲区**：后端挂着
`include_in_schema=False`（理由见 `optimization/router.py` 末尾：那一轮 `openapi.json`
与 `schema.d.ts` 由另一条线在改），所以它不在快照里。白名单里写着这一条，
等快照能重导的那一轮，`include_in_schema` 与白名单要**一起**删掉。

---

## 九、`memory` 那一组：做了没露出来，还是本来不给用户看

结论：**本来不打算给用户看**，而且"表里长期 0 条"与"前端没接"之间**没有因果**。
两件事都要给证据，因为上一轮的推断把两件不相干的事连成了一条链。

### 9.1 写入源根本不经过 HTTP

    写入 memory_items 的地方，只有三处：
      summarize.py:592     会话结束 → 模型总结 → store.add(...)      （后台线程）
      propositions.py:834  观测 → 归纳命题 → store.add(...)           （后台线程）
      propositions.py:871  修订（consolidation）→ store.add(...)      （后台线程）
    HTTP 的 POST /memory 是第四处，且只服务手工入口。

所以"界面没接 `/memory` 的 4 个操作"改变不了表的条数——一个字节都不经过它们。反过
来说：表空的时候，接上界面也不会变满。

### 9.2 实测（本机库，`backend/data/mindmaps.db`，只读打开）

    memory_items            13 条   全部 source=summary / kind=proposition
    memory_sessions         13 条   全部 status=empty
    memory_observations    110 条
    memory_propositions     13 条
    runs                   195 条
    edit_events            565 条
    user_prefs               4 条

复算：

    cd backend
    python -c "import sqlite3;c=sqlite3.connect('file:data/mindmaps.db?mode=ro',uri=True);\
    print(c.execute('select count(*) from memory_items').fetchall())"

13 条全落在 `2026-11-23T09:43–09:44` 这一分钟里，内容是"关于这个人"的判断
（『他习惯先让工具生成一版，再在图上反复小步修改』）。也就是说：**记忆链跑通过一次，
产出了 13 条，而且它们从那天起每次生成都被 `prompt_for` 检索注入**——只是从来没有
任何界面显示过它们（`docs/MEMORY-PROPOSITION.md` 起点读数里的 `memory_items 0 条`
是那个时点的事实，不是今天的事实）。

`sessions` 13 条全 `status=empty` 说明另一件事，与界面无关：**会话总结那一段每次
都合法地产出 0 条**（`summarize.py:667` 的 `STATUS_EMPTY`）。这是"总结管线在跑但没
有可记的东西"，不是"没人调端点"。

### 9.3 判据：三个模块是给 agent 用的

    memory/observations.py   观测：动作级事实，一次 run / 一次保存一条
    memory/propositions.py   命题：跨事件判断（confidence / decay / observation_ids）
    memory/recall.py         双路检索：最近观测（时间）+ 最相关命题（向量），RRF 合并

三个模块都**没有**面向用户的输出形状：`recall` 的出口是 `prompt_for(bundle)`（一段
拼进 system 的文本），`propositions` 的出口是带 `reasoning` / `confidence` / `decay`
的机器判断，`observations` 的出口是回指 `run_ids` / `event_ids` 的证据链。它们的读者
是模型。设计文档也是这么分的：`memory/__init__.py` 明写这层是"系统从历史里推断的条目"
（规模会从个位长到几百上千，所以必须向量检索），而"用户显式写的参数与备注"是另一层
`user_prefs`——**那一层才有界面**（`PrefsPanel`，已接）。

所以 `memory` 那 4 条的正确形态不是"接线"，而是一个产品决定：**"我记住了你什么"要不要
给用户看、能不能删**。要做的话缺的不是 `client.ts` 里的四个函数，是一个面板 + 那个决定；
那时基线里这四条一起从 `observability` 改成 `gap`。

---

## 十、自评：机器判据覆盖到哪一格

四类缺口，按"能不能自动化 / 值不值得"分三档：

| 缺口形状 | 现在谁看得见 | 档 | 依据 |
|---|---|---|---|
| 后端有路由、`client.ts` 一行都没有 | **本闸**（`check_route_coverage.mjs`，棘轮） | 已覆盖 | 53 个操作 × 9 条断链，双向差集，CI 一步 |
| `client.ts` 有、后端没有（**405 的静态形态**） | **本闸**（死链方向） | 已覆盖 | 历史上那条 `DELETE /api/mindmaps/{id} → 405`（`USER-PATH-TESTS` ①）就是这一格：当时 `deleteMindMap` 已在 `client.ts`，后端没路由 → 死链会红，而且早于任何后端契约测试 |
| 后端函数零引用（`link.suggest`、账本三条） | `backend/scripts/acc3/find_unwired.py` | 已覆盖 | 棘轮 |
| `client.ts` 函数零调用（`deleteMindMap`） | `frontend/scripts/find_unwired.mjs` | 已覆盖 | 棘轮 |
| 接了但没入口（函数被调，组件不可达） | 没人 | **可补** | 判据确定且静态：`@vue/compiler-sfc`（已在 `node_modules`，`@vitejs/plugin-vue` 带进来）解析每个 SFC 的模板组件标签 + `<script setup>` 的 import 图，从 `App.vue` 做可达性遍历，再看调用点所在文件在不在可达集里。~100 行，一个 CI step。漏报方向明确：`v-if` 条件为假算不出来 |
| 有按钮但点了没反应（事件没绑 / handler 空转） | 页面动作清单管了一部分（`tests/user-path-*.spec.ts`：入口可见 + 点了发哪个请求） | **可补（窄版）** | 全量要 e2e 点击 + 语义断言，不划算；但有一格很便宜：`data-test` 在 `src/**` 里 218 处，把它与 `tests/**` 里出现过的选择器求差，就能得到"有这个按钮但从来没有用例碰过它" |
| 有反应但请求 405（**运行时**：中间件 / 代理前缀 / 路由顺序遮蔽） | 没人 | **不划算** | 静态形态已被本闸吃掉（见上表第二行）；剩下的是"快照里两条都在、实际一条吃掉另一条"——`memory/router.py` 那段"`/search` 必须排在 `/{item_id}` 之前"正是这类。要抓只能起真后端逐端点打，那是一条 e2e 流水线，不是一次静态比对 |
| 功能被 env 开关关掉（代码全在、线上全关） | 没人 | **可补** | 判据是文本：`os.getenv("X", "默认值")` 的缺省值 × `docs/PLAN.md` 的开关表 × 部署 drain 出的实际值。`MINDMAP_LIBRARY_SEARCH` 就是这种形状：线上置 `0` 做纯展示，接口与界面全都健在 |
| 入口在用户到不了的地方（抽屉第三层 / 长按 / 手势） | 页面动作清单里 `visible()` 只覆盖列进清单的动作 | **不可自动化** | "用户会不会去那里"不是可判定的事实。能做的只有把它写进动作清单（那是人工维护的数据，不是判据） |
| 契约里没有这个信息（手动节点无标记，`USER-PATH-TESTS` ④） | 没有人能看见，因为信息不存在 | **不可自动化** | 缺的是字段与产品决定，不是测试；测试永远等在产品后面 |

### 最后一问：这些闸加起来，能不能回答"哪些功能做了、界面上却找不到"

**不能，而且差距不在闸的数量上，在判据的类型上。** 现在这几道闸合起来只回答一件事：
**这条管道是否连通**（路由 → `client.ts` → 函数 → 某个组件）。它们回答不了的是同一个
问题的另外三半：

1. **连了但被条件挡住**——`v-if` / env 开关 / 权限。静态看得见"有"，看不见"此刻是假"。
   （`功能被 env 开关关掉` 那一格是可补的，因为开关值住在文本里。）
2. **通了但产物不对**——点开是空列表、诊断恒 0 条、导出的文件少一半。这一档今天的判据
   是**实测脚本**（`backend/scripts/measure_*.py` 那八十多个）而不是闸门；它们证明"某个
   版本跑出过什么数"，不证明"今天仍是这个数"。
3. **用户在不在那里**——入口在抽屉第三层、长按才出、或者在折叠起来的浮层里。这一档只能
   靠页面动作清单这种**人工维护的清单**，它防的是"入口被删掉"，防不了"从来没列进清单"。

所以缺的那一块是：**可达性 × 开关状态的联合判据**。它理论上可自动化（SFC 图 + `getenv`
缺省值 + 部署清单，前两格写在上面表里都标了"可补"），而"用户在不在那里"永远不可判定——
那一半只能靠产品把入口放在显眼处。

一个可以直接拿去用的近似答案：**把"可补"那三格补上之后，剩下的不确定性就只有三样**
——`v-if` 的运行时真假、产物对不对、入口显不显眼。前两样各自有更便宜的办法（前者靠
是否真有用户路径；后者靠已有的 measure 脚本定期重跑），第三样是设计问题。今天这几道闸
把"根本没接"这一格彻底关掉了；它曾经是最贵的一格（五次人工撞见）。
