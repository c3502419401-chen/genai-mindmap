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

- **8 个操作在 `client.ts` 里一次都没出现**：`POST /ingestion/text`、
  `GET /ingestion/formats`、`POST /agent/tasks`、`POST /knowledge/search`、
  `GET|POST /memory`、`DELETE /memory/{id}`、`GET /memory/search`。
  按功能是五组：材料文字入库、格式清单、agent 任务提交、知识检索、记忆整块。
- 另有 4 个（`/export/{json,markdown,opml,puml}`）是**同一条动态路径**
  `/export/${format}`，属误报——归一化必修的一课。
- 反方向 1 条：`POST /optimization/repair` 在路由里但 `include_in_schema=False`
  （`router.py` 末尾写着为什么：账本系统那一轮在改快照），所以它不在 `openapi.json`
  里，基于快照的闸会把它误报成"前端有、后端没有"，需要显式白名单。

**该建什么**：`frontend/scripts/find_route_calls.mjs`——用 `typescript` 的语法树
（依赖已有，`find_unwired.mjs` 就是这么做的）还原 `client.ts` 里所有
`TemplateExpression` / `StringLiteral` 的路径，按 **method + 归一化路径**与
`backend/openapi.json` 求双向差集。

- **能自动化、且判据确定**：两份输入都是文本（`openapi.json` + `client.ts`），不读运行时、
  不读数据库、不读 git 状态；唯一脆弱点是路径归一化（`/x/${id}` ↔ `/x/{map_id}`、
  查询串不参与），这一条写进 docstring 就是可预期的边界。
- **能不能进 CI**：能，而且该进。但要用**棘轮**而不是绝对零：基线 = 上面那 8 条 +
  `repair` 的白名单（理由：刻意不进 schema），否则第一天就红，然后被人关掉。
- **它不覆盖什么**：它只证明"`client.ts` 引用了这个路径"，不证明"组件调了那个函数"
  ——后者是 `find_unwired.mjs` 的活。两级串起来才闭环：
  **路由 → `client.ts` → 组件**，缺一级就有一种"后端做了、界面没有"能藏住。
