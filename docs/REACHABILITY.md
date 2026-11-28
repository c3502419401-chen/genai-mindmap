# 第四档：函数被调用了，但那个组件从入口到不了

> 基线 `main` @ `a6d842b`。查法 `frontend/scripts/check_reachability.mjs`（棘轮进 CI）。
> 复现：
>
> ```
> cd frontend
> node scripts/check_reachability.mjs            # 读数 + 不可达逐条
> node scripts/check_reachability.mjs --edges    # 23 个 .vue 的入边（审计"0 条 a"）
> node scripts/check_reachability.mjs --gate     # 棘轮
> node scripts/check_reachability.mjs --root=app # 换成只从 App.vue 起（对照）
> ```
>
> 这一轮的读数：**69 个节点 / 66 可达 / 3 不可达（a 0、b 0、c 3、d 0、e 0）**。

---

## 零、它补的是哪一格

```
后端函数零引用                            → backend/scripts/acc3/find_unwired.py    ✓
client.ts 函数零调用                      → frontend/scripts/find_unwired.mjs       ✓
后端有路由、client 一行都没有              → frontend/scripts/check_route_coverage.mjs ✓
★ 函数被调用了，但那个组件从入口到不了      → 这一份
```

前三档都从**「有没有人引用」**这一侧出发，所以它们共同缺一层：**引用链可以在一处
没接上的地方就断掉**，而每一档都只看自己那一侧的那条边。

```
某个 .vue 里写着完整的界面（有按钮、有事件、调 client 函数）
但它没有被任何可达的组件 import
→ 用户永远看不到它
→ 而 find_unwired.mjs 会因为"那个 client 函数被调用了"而全绿
```

这正是 `docs/WIRING-GAPS.md` 那五处缺口（`sources` id、`/knowledge/search` 桩、
`link.suggest`、`thinking_options`、`diagnose_density`）的共同形态的最后一层：
**代码全在、调用链全通、就是没有入口。** 前四处是"链子没接"，这一档是"链子的起点
根本挂不上任何东西"。

---

## 一、判据

### 1.1 三条边

每个 `.vue` / `.ts` 的出边来自三种写法：

| 写法 | 边的性质 | 例子 |
|---|---|---|
| `import X from './y'` | 值边（静态） | `App.vue` → `./components/ChatDock.vue` |
| `import './y'` | 值边（副作用导入） | `main.ts` → `./styles.css`（非代码，不成节点） |
| `import type { X } from './y'` | **类型边** | `types.ts` → `./schema` |
| `import('./y')` | 值边（动态） | `App.vue:163` → `./dev/samples` |

`.vue` 用 `@vue/compiler-sfc` 的 `parse()` 切出 `<script>` / `<script setup>` 段再交给
`ts.createSourceFile`。本仓库 23 个 `.vue` **全是 `<script setup>`**，没有选项式
`components: {}`、没有 `defineAsyncComponent`、没有 `<component :is>`、没有
`import.meta.glob`（都查过）；脚本仍支持这几种写法，因为下一个人可能会用。

### 1.2 为什么只沿值边走

`import type { X } from './y'` 编译后**整条语句消失**，浏览器根本不会加载 `./y`。一个
只被类型边引用的模块被判"不可达"是对的：它在运行时不存在。

本仓库 `tsconfig.json` 是 `verbatimModuleSyntax: true`，类型导入必须显式写 `type`
（写漏了 `vue-tsc` 会红），所以"这条边是类型边"在这里**是可判的，不是猜的**。这条
前提写在脚本 docstring 里，因为它哪天被改成 `false`，这一档判据就要重判。

### 1.3 根：`src/main.ts`，不是 `App.vue`

`index.html` 的 `<script type="module" src="/src/main.ts">` 指向 `main.ts`，`App.vue`
由它挂载。只从 `App.vue` 起会漏掉**入口自己在挂载前 import 的东西**（`main.ts` 里的
`useAuthStore(pinia).probe()`）。

两条可达集的差用 `--root=app` 对照：本轮是 **0 个模块**——也就是说这个仓库恰好没有
"只被入口用、组件用不到"的东西。**0 是读出来的结论，不是省掉这一步的理由**；差非零
时那些模块归 `b` 档，note 会说明它是"另一个根文件本身"还是"只被入口挂载前的逻辑
import"。

### 1.4 五档

| 档 | 含义 | 判据 | 处理 |
|---|---|---|---|
| `a` | 真不可达：一个完整界面，谁都不 import | `.vue` + 模板里有事件绑定 / `data-test` / `v-model`，或 script 里有 `defineEmits` / `emit(` / `defineProps` / `import … api/client` | **该报**：接上入口 |
| `b` | 只经另一个根可达 | 从 `main.ts` 走得到、从 `App.vue` 走不到（或反过来） | 不是缺口，单列 |
| `c` | 纯类型 / 编译期断言 | `.d.ts`，或文件里只有 `interface`/`type`/`import type`/`declare`；或所有 `export const` 的值都是 `… satisfies T` | 不参与组件可达性 |
| `d` | 测试辅助 | 只在 `tests/**` 里被 import | 不算（`.vue` 会带 ☆） |
| `e` | 死代码 | 有运行时代码、谁都不 import、无界面痕迹 | **该报**：删掉 |

**`a` 与 `e` 必须分开报，因为动作不同。** `a` 是"写了但没入口"（下一步是接上或确认
它该不该存在），`e` 是"写了又废弃了"（下一步是删掉）。混成一档，报出来的清单就没有
下一步。

分档靠的是**启发式**（`interfaceTraces` 那五条痕迹），所以脚本说的是"**建议档**"，
最终档写在基线里，由人定。这一层是这道闸唯一需要人过一遍的地方，也是它便宜的原因：
69 个节点里只有不可达的那几条要看。

**`d` 里为什么要打 ☆**：一个完整界面组件**只在 tests 里被 import、产品代码没人
import**，是"写了界面忘接"最隐蔽的形态——`find_unwired.mjs` 里 `tests=` 非零而
`refs=` 为零是同一个形状在另一侧的投影。本轮 `d` 是 0 条，所以没有 ☆。

---

## 二、读数

一次全量跑（`main` @ `a6d842b`，干净 worktree 上逐行相同）：

```
src/** 节点：69（.vue 23 / .ts 46）
根：src/main.ts（main 与 app 两条可达集的差：0 个模块）
  可达：66（其中只经动态 import 可达：2）
  可达的 .vue：23 / 23
  ★ 不可达：3
    a 真不可达（完整界面没人 import）：0
    b 只经另一个根可达：0
    c 纯类型 / 编译期断言：3
    d 只在 tests 里被 import：0
    e 死代码：0
```

三个数各自要能自证，否则"0 条 a"只是沉默：

**① `a = 0` 是真结论。** 两条独立旁证：

- `--edges` 打出的 23 个 `.vue` 全部有值入边（`App.vue` ← `main.ts`，`ChatDock.vue` ←
  `App.vue`，`VoicePress.vue` ← `ChatDock.vue`，`AuthPanel.vue` ← `HistoryDrawer.vue`…），
  没有一条是"（没有任何值 import）"。
- `.vue` 里的 22 个**子组件 import，模板里全都有对应标签**（一次性核对：0 个 import
  了却没在模板出现）。这条不是脚本的判据，是它的前提：`vue-tsc` + `noUnusedLocals`
  不允许"import 了不用"的组件留下——所以在这个仓库里，**import 图 ≈ 模板使用图**。
  前提一旦不成立（有人关掉 `noUnusedLocals`、或用了动态组件），`a` 的读数就要重看。

**② `c = 3` 是这一档的"合理不可达"**，逐条见第三节。

**③ 动态 import 2 个：`src/dev/samples.ts`、`src/demo/presets.ts`。** 它们只出现在
`App.vue:163` 和 `App.vue:533` 的 `import()` 里。动态 import 是**真实的边**（运行时
会加载），所以它们算可达，单列只因为"只看 `import … from` 的静态读法会把它们误报成
不可达"。

**它可能查出 0 条，而那是结果不是失败。** 这一轮 `a` 就是 0：这个仓库没有"写了界面
忘接"的问题。写出来比说"没发现问题"有用——它同时给了一个可复核的形状（69/66/3），
下次读数变了就是有东西动了。

---

## 三、3 条不可达逐条判

| 文件 | 建议档 | 人定档 | 判 |
|---|---|---|---|
| `src/api/types.ts` | c | **c** | 合同镜像类型，`import type` 消费 40 余处 |
| `src/api/schema.d.ts` | c | **c** | `npm run gen:api` 生成，`.d.ts` 不产生代码 |
| `src/api/contract-check.ts` | c | **c** | 编译期契约断言，**故意**不被 import |

**`types.ts` 为什么不是 `e`。** 它有 40 多处 `import type` 引用（`App.vue`、所有 store、
`contract-check.ts` 自己），只是**全部是类型边**。按"有没有人引用"数它是热的，按
"运行时会不会加载它"数它是零——这一档取后一种口径，因为它问的是"用户能不能看到"。

**`contract-check.ts` 为什么不是 `e`。** 它的 docstring 第 26–27 行写着约定：

> 本文件没有运行时副作用：`checks` 只被类型系统读取，构建时会被摇掉；它也不需要被
> 任何模块 import，只要在 `tsconfig.json` 的 `include` 里就会被检查。

脚本识别它的方式是"每个 `export const` 的值都是 `… satisfies T`"（`isCompileTimeAssertions`）。
识别不出来的话它会落到 `e`（"有运行时代码、谁都不 import"）——**这是一处启发式的
边界，所以基线里那条 reason 写清了为什么它是 c**：`npm run type-check` 就是它的
运行方式。

---

## 四、闸门（棘轮）

与前三道同一套语义：**基线之外的不可达才红**。比的是
`frontend/scripts/reachability_baseline.json` 的 `entries`（路径 → `class` + `reason` +
`evidence`）。

四条规则：

1. 新出现的不可达（不在基线里）→ **红**；
2. 条目的 `evidence` 文件不存在 → **红**（豁免的理由没了而豁免还在）；
3. 条目的 `class` 不在 `a`–`e` 里 → **红**（防止手写错一个字母就静默降级）；
4. `class: a` 的条目除 `reason` 外还要 **`ack`**（署名）→ 缺了**红**。

第 4 条是这道闸与前三道唯一的不同，理由：`c`/`d` 是"它本来就不该被 import"，写进基线
等于**说明**；`a` 是"这个界面此刻真的看不见"，写进基线等于**承认**。说明只需要理由，
承认该留下是谁承认的。

条目已经不再不可达 → 打印提示（不红）：修好了就该把条目删掉。

**在干净 worktree（detached HEAD）里演过三场：**

```
$ node scripts/check_reachability.mjs --gate          # 新增一个完整界面组件、没人 import
  [新增] a  src/components/_probe_orphan.vue
      完整界面（模板里有事件绑定、模板里有 data-test 锚点）但谁都不 import
闸门：不通过
  · 新增不可达：src/components/_probe_orphan.vue（建议档 a：…）
[exit code: 1]

$ …把条目写进基线、只给 reason 不给 ack
闸门：不通过
  · 基线条目 src/components/_probe_orphan.vue 是 a 档（真缺口）：写进基线等于承认这个
    界面此刻真的看不见，必须留 ack（署名）
[exit code: 1]

$ …补上 ack
闸门：通过（4 条不可达全部在基线内）
[exit code: 0]
```

第三条反向检查也验过：把 `types.ts` 的 `evidence` 改成不存在的路径 →
`基线条目 src/api/types.ts 的豁免理由已经不在了：frontend/src/api/_gone.ts` → 红。

**确定性**：判据是 `src/**`、`tests/**` 的文本加一份基线 JSON；不读运行时、不读
数据库、不读 git 状态或工作区差异，路径以 `import.meta.url` 为锚。外部依赖只有
`typescript` 与 `@vue/compiler-sfc`（都是 devDependencies，`npm ci` 之后就在）。
干净 worktree 对照：**69 / 66 / 3 逐行相同，`--gate` 退出码 0**。

---

## 五、顺带量的那一格：`data-test` 与测试选择器的差集 —— 判「不做」

上一轮把它记成"可补（便宜）"。量了，**不值得进 CI**。

读数（一次性脚本，未进仓库）：

```
src 里 data-test 锚点：199 个（去重；出现处 218 次）
tests 里出现过的锚点（含 getByTestId）：182 个
有锚点但 tests 里一次都没出现：33 个
锚点被测试用到的比例：83%
同一个 data-test 值出现在多个文件：0 个
```

差集 33 条的样子：`map-delete-hint`、`save-hint`、`drag-ghost`、`node-intent-close`、
`edit-ledger-busy`、`prefs-empty`……

三条理由：

1. **这 33 条没有一条能被判成缺陷。** 它们多半是**提示性元素**（`*-hint`、`*-empty`、
   `*-busy`），测试对它们的断言走的是文本或可见性（`textContent`、`isVisible`），
   不需要选择器。闸门无法区分"忘了测"和"故意不测"，于是产出的是 33 条要人逐条看的
   清单，看完 99% 的结论是"不用管"。
2. **唯一有硬判据的变体读数是 0。** 真正能自动判红的是"同一个锚点值出现在多个元素上"
   （`getByTestId` 会歧义），本仓库 0 处。也就是说这一格**现在没有任何可查的东西**。
3. **它有更好的替代读数。** "哪些界面没被测到"用 `vitest --coverage` 看行覆盖比看
   `data-test` 差集准得多；`data-test` 的存在只说明"这里留了一个稳定选择器"，不说明
   "这里该有断言"。把后者当前者，是把工具当指标。

所以：**不做。** 读数留在这里，下次有人想补这一格时先看这三条。

---

## 六、自评：这一档算的是 import 图，不是渲染条件

这是静态可达性的根本局限，写在最前面而不是脚注里：

> 一个组件可以**被 import 了**，但在父组件里被 `v-if="条件永远为假"` 包着 ——
> 可达性查法**查不出来**。

本仓库里最长的那三条缝，都是真事：

**① `AliasResolver` 绑在一个后端能力开关上。**
`App.vue:2120` 是 `<AliasResolver v-if="aliasConfigured" />`，而 `aliasConfigured`
（`App.vue:749`）初值 `true`，随后按 `GET …/aliases` 的 `configured` 字段改——部署没配
`MINDMAP_NEO4J_PASSWORD` 时它永远是 `false`。**这个组件在那种部署里永远渲染不出来，
而可达性读数是"可达"。**

关键在下一步：这种情况下"看不见"是**正确行为**，因为 `App.vue:2125` 有一句
`图存储没有启用（缺 MINDMAP_NEO4J_PASSWORD），这一层暂时不可用。` —— 假的分支有出口。
所以"条件可能恒假"不能直接报缺口，**还得判"假的时候有没有话可说"**，而那是语义判断。

**② `StructureCheck` 自己决定不渲染。**
`App.vue:1486` 的注释写着"自己判可见性（没跑过、没有发现、没有错误时整块不渲染）"。
从 import 图看它 100% 可达；从用户视角看，它在"没有诊断"时是零像素。这类"组件内部
决定不渲染"在静态图上完全没有痕迹。

**③ `AgentPanel` 在 `<details>` 里**（`App.vue:1467`）。折叠起来不占屏幕，但那是渐进
暴露，不是缺口——**"要点一下才看见"和"永远看不见"是两件事**，而静态判据两件都判不出。

### 6.1 "查 `v-if` 是不是恒假常量"能补多少：0 处

这是最自然的下一个候选，成本也低（模板 AST 里取 `v-if` 的表达式，看是不是字面常量）。
量了一遍：

```
渲染条件（v-if / v-else-if / v-show / :disabled）共 335 处，分布在 22 个 .vue
字面常量条件（false / 0 / '' / null / undefined）：0 处
条件里出现的标识符去重：292 个
出现最多的：store(54) length(51) item(42) state(19) busy(14) error(13) check(13)
            runBlocked(11) prefs(11) saving(10) settingsStore(10) …
```

**335 处里一处都没有恒假常量。** 而这个仓库的渲染条件几乎全是**运行时状态**（`busy`、
`error`、`length`、`generating`、`runBlocked`）——它们不是"开关"，是"数据到了没有"。
`v-if` 恒假这一档在这个仓库里补出来是空的；换一个代码库可能有用，但那是给别的项目
写脚本，不是给这个项目的缺口写脚本。

### 6.2 "开关状态"那一半怎么补：分三档

| 档 | 能不能补 | 判据 | 本仓库的收益 |
|---|---|---|---|
| 字面恒假（`v-if="false"`、`v-if="0"`） | **能**，静态、便宜 | 模板 AST 取条件表达式的字面量 | **0/335**，无 |
| 绑在**能力开关**上（`aliasConfigured` 这种来自后端 `configured` / `*_enabled` 的字段） | **部分能** | 顺着 `v-if` 的标识符找到它的赋值来源，若来源是某个 feature flag 接口，就与"后端有没有实现这条能力"对齐（`check_doc_claims.py` 已经在用同一类"事实探针"） | 已知 1 处，且**有降级文案**，本来就不该报 |
| 绑在**运行时状态**上（`busy` / `error` / `length` / 选中的节点） | **不能**（静态意义上） | 没有"恒假"可言：它在某个数据下会真 | 292 个标识符里绝大多数是这一类 |
| 组件**内部**自己判可见性（`StructureCheck`） | **不能** | 判据在 `computed`/`v-if` 的组合里，等于要把组件当程序推一遍 | —— |

所以答案是：**能补的只有第二档，而它恰好是"看不见是设计"的那一档。** 第三、四档要让
机器判，只能靠运行期：真跑起来，构造数据，看渲染结果（E2E / 渲染快照）。那不是静态
闸，是测试。

### 6.3 补不上的部分该不该靠人工

该。理由不是"技术上做不到"，而是**判据会把误报当缺口**：

- 335 处渲染条件里，任何一条的"看不见"都可能**正确**（空状态、降级、权限、能力开关）。
  静态判据给不出"这次看不见是不是 bug"，它只能给"这里有一个条件"。
- 一旦判据给不出区分，进 CI 的结果只有两种：加白名单（然后白名单变成噪声），或者
  被关掉。`data-test` 那一格是同一个教训（§五）。
- 真正需要人做的那一下是**语义判断**："这个功能写了、链子通了，用户有没有办法走到它"。
  机器能做的是把**候选集缩小到几条**——这一档把 69 个节点缩到 3 条，那 3 条里没有一条
  需要判 a/e 的纠结。剩下"它渲染得出来吗"这一步，落点在 `docs/WIRING-GAPS.md`
  （缺口文献）与 `docs/BACKLOG.md`（待办状态），那里的结论是带署名和时间的人工判断。

---

<!-- claims-ok: auth_gate_active 这一行是标题里的「有没有…用户看不到的功能」，问的是"功能有没有入口"，不是"没有登录/账号功能"。`auth_gate_active` 那条正则把「没有 + 用户 + 功能」当成了缺功能的语气，这是它的误报（2026-11-28 复核：在 7ac8400 上就红，与本轮改动无关）；收紧正则要连带重验它的七条用例，属另一件事，先在这里把它可见地放行 -->
## 七、更硬的一问：这串闸加起来，能不能回答"有没有做了但用户看不到的功能"

不能。下面是能力矩阵——每一道闸答一个问题，把它们并起来仍然缺一格。

| 闸 | 它答的问题 | 它答不了 |
|---|---|---|
| `backend/…/find_unwired.py` | 后端有没有公开函数零引用 | 那个函数有没有路由、界面上有没有入口 |
| `frontend/scripts/find_unwired.mjs` | `client.ts` 的函数有没有人调 | 调它的组件用户能不能走到 |
| `check_route_coverage.mjs` | 后端路由有没有被 `client.ts` 请求 | 请求它的函数有没有人调 |
| **`check_reachability.mjs`（本轮）** | **组件从入口能不能 import 到** | **import 得到但渲染不出来** |
| `check_diagnostic_sides.py` | 诊断码有没有事后出口 | 出口在界面上是不是真的能到达 |
| `check_doc_claims.py` | 文档的说法与代码是否一致 | 代码本身对不对（它只保证"话"没错） |
| `check-test-cost.mjs` | 用例的超时上限有没有余量 | 用例测的是不是用户走的那条路 |
| `check_script_hygiene.py` | 探针脚本能不能加载、引用的名字还在不在 | 产品功能 |

八道闸全绿时，能确定的是一句话：

> **从后端函数 → 路由 → `client.ts` → 组件 → 入口，这条链上没有任何一环是"零引用"
> 或"import 不到"的。**

它仍然不能确定的，正是用户问的那件事：

> 这个功能**在某个具体部署、某个具体数据下**，用户能不能看到、点得到。

缺的那一半是 **可达性 × 开关状态的联合判据**。这一轮补了左边；右边按 §6.2 的结论
只在对"能力开关"这一小类上可补，对"运行时状态"和"组件内部判可见性"这两大类补不了
——它们不是静态问题，是"运行起来才知道"的问题。

**而这也是这一串闸的定位该被说清的地方**：它们不是"证明功能存在"，而是"证明没有
静默的断链"。断链是可以用文本判的；"用户看不看得见"不是。把前者的绿灯当成后者的
证明，就是把它当成了它不是的东西——`docs/WIRING-GAPS.md` §12.4 说棘轮的价值在"让存量
可见"而不是"以后不新增"，这一档同理：它的价值是把 69 个节点里那 3 条不可达逐条摆出
来，让"这个界面到底有没有入口"这件事**每次 push 都被重新说一遍**。
