# 三条裸奔在 5 秒线上的用例：慢在哪、判什么、修成什么

> 2026-11-25，本机（Windows / Node 24 / vitest 5.0.5 / jsdom 30）实测。
> 对象：`frontend/tests/history.spec.ts`、`frontend/tests/canvas-preview.spec.ts`、
> `frontend/tests/canvas-keys.spec.ts`。
> 前置：`docs/TEST-COST-136-NODE.md`（那里的四条规矩与"136 节点是规模型"的结论）。
>
> **结论先行**：三条的病因**不一样**，不能整批套同一个结论。
>
> | 用例 | 判据 | 慢在哪 | 修法 |
> |---|---|---|---|
> | `history` 字节超上限时丢最旧的 | **A**（规模型） | 80 次真实编辑 × 每次 2.97ms 的序列化/深拷贝/diff，**零渲染** | 15 秒显式上限 |
> | `canvas-preview` 生成中真的长出节点 | **C** → 残余 A | 为了 3 条断言挂了整个 `App` 壳 | 只挂 `MindMapCanvas`，再给 10 秒 |
> | `canvas-keys` 焦点在别的按钮上 | **C** → 残余 A | 同上，`App` 壳 223 个 DOM 里只有 49 个与断言有关 | 最小壳（画布 + `MessageFab`），再给 8 秒 |
>
> 本轮实测还顺带证伪了一个流传的说法：**"加压 2.7×"不是常数**。同一份代码、同一分钟，
> 同一条用例在同一次加压的两份进程里跑了 **365ms 和 6606ms**（18 倍）。所以上限必须按
> **见过的最坏值**定，不能按某一次读数定。

---

## 0 改前读数：三条都在默认 5000ms 的线上

加压条件：两份 `npx vitest run --reporter=verbose` 同时跑（各 76 files / 1276 tests）。
两轮独立加压，取每次两份的逐条读数：

| 用例 | 加压·改前（A / B） | 加压·改后·二次（A / B） | 加压·改后·最终（A / B） | 默认上限 |
|---|---|---|---|---|
| `history` 字节超上限时丢最旧的 | 2537 / 2640ms | **6606** / 365ms | 3480 / 3106ms | 5000 |
| `canvas-preview` 生成中真的长出节点 | 3829 / 4290ms | 2927 / 3593ms | 658 / 1953ms | 5000 |
| `canvas-keys` 焦点在别的按钮上 | 3941 / 3801ms | 2128 / 2446ms | 1441 / 1904ms | 5000 |

三列的条件相同（两份全量同时跑），差别只在"哪一刻的机器"：改前那轮总时长 75–78s、
二次 75–78s、最终 41s——**同样的负载比例，机器的绝对速度差一倍**。这正是本文最想留下的
一条方法论：加压读数不能只取一次，上限必须按**见过的最坏值**定（§4 表就是按最坏值算的）。

**6606ms > 5000ms 那条就是"裸奔"的实锤**：改前那一轮里它靠的是运气（2537ms 过线），
换个进程就超。本轮加了 15 秒上限之后，同样的抖动不再能把 CI 打红。

---

## 1 `history`：判 A，因为**它不是渲染型**

`docs/TEST-COST-136-NODE.md` 的四条规矩里，A/B/C 的分水岭是"固有成本 / 重复计算 /
走了不该走的重路径"。这一条最容易被误判成"渲染型"（它的名字像在造大图），实际要反过来看：

```ts
// frontend/tests/history.spec.ts —— 整个文件一行 mount 都没有
store.preview(bigContent(300), 'right');
for (let i = 0; i < 80; i += 1) store.commitLabel(`n${i % 300}`, `第 ${i} 次改动`);
```

没有组件、没有 DOM、没有 jsdom 布局。唯一的载荷是"一张 300 节点的树 × 80 次编辑"。

### 1.1 插桩：钱花在四个地方，加起来 97%

探针（一次性，跑完即删）在 `commitLabel` 外层计时，并单独量它内部的四步：

| 项 | 实测 | 说明 |
|---|---|---|
| 一份快照的规模 | **82.5KB** | 300 节点 × 240 字节 label（80 汉字） |
| `bigContent(300)` 构造 | 0.09ms | 可忽略 |
| `preview(300 节点)` | 0.39ms | 可忽略 |
| `JSON.stringify(响应式 content)` | **0.939ms** | 同一份数据的 `toRaw` 版本只要 0.081ms —— **11.56×**，全是 Vue Proxy 的读拦截 |
| `TextEncoder.encode(82.5KB)` | 0.117ms | `new TextEncoder()` 每次都建，成本在 0.002ms 量级，不是问题 |
| `replaceContainer + renameNode`（300 节点深拷贝） | **1.187ms** | 不可变更新：改一个节点要复制整棵树的路径 |
| `diffEdits(300 vs 300)` | **0.634ms** | 改动账本的全树 diff |
| **`commitLabel` × 80** | **sum 281.8ms / p50 2.97ms** | min 2.43、max 18.01 |

四项相加 = 0.939 + 0.117 + 1.187 + 0.634 = **2.877ms**，实测 p50 **2.97ms** → 占 **97%**。

### 1.2 有没有"越到后面越慢"（B 的典型症状）

没有。`commitLabel` 的前 5 次 = 3.09 / 3.13 / 2.69 / 2.65 / 2.47ms，后 5 次 =
3.04 / 3.28 / 3.24 / 3.26 / 2.88ms，持平。原因是 `HISTORY_LIMIT = 50` 早就把栈钉住了
（`undoDepth` 稳定在 50），序列化的对象大小不随时间涨。

### 1.3 唯一的重复计算：占 3%，不值得为它改生产代码

`recordPast()` 淘汰一份旧快照时要**再编码一次**才能算字节：

```ts
pastBytes.value += snapshotBytes(snap);              // 编码第 1 次
while (past.value.length > 1 && (...)) {
  const dropped = past.value.shift()!;
  pastBytes.value -= snapshotBytes(dropped);         // 编码第 2 次（同一份字符串）
}
```

75 次淘汰 × 0.117ms ≈ **8.8ms**，占 281.8ms 的 **3%**。把它改成 `past: {snap, bytes}[]`
能省掉这 3%，但要动生产代码的撤销栈结构——收益与风险不成比例。

### 1.4 判定

```
A（规模型）：80 次真实编辑，每次 3ms 是生产代码每改一个节点都要付的钱
            （撤销要序列化、树要不可变更新、账本要 diff）。
规模不许降：300 节点 / 80 步就是"几十步内把栈顶满"这个前提本身。
```

上限 15 秒的来历：本地单跑 286ms、本地加压**最坏 6606ms**、CI 估 ≈ 6606 × 1.3–2 =
**8.6–13.2s**。15 秒 = 本地加压 2.3×、CI 估的 1.1–1.7×。

---

## 2 `canvas-preview`：判 C —— 它挂的是整个 `App`，需要的只有画布

这条的名字（"画布上真的长出节点，而不是只有右侧面板在动"）像 A，实际是 C。

### 2.1 断言读的是谁

| 断言 | 归属 |
|---|---|
| `wrapper.text()).toContain('还没有导图')` | `MindMapCanvas.vue:1063` 的模板句 |
| `wrapper.findAll('.vue-flow__node')).toHaveLength(2)` | `MindMapCanvas` 体内的 Vue Flow |
| `store.agentEvents / store.nodes / running` | store，与组件树无关 |

右侧面板在这条用例里**一个断言都没读**——它只是名字里的反例修辞。

### 2.2 同进程对照（避开机器抖动）

同一进程内交替跑三种形态，各 3 次取均值（一次性探针，跑完即删）：

| 形态 | total | `mount` | `waitFor`(节点) | snapshot + `waitFor`×2 | done + await |
|---|---|---|---|---|---|
| 挂 `App` 壳 + `waitFor`(50ms) | **869.5ms** | 318.5 | 270.4 | 68.9 | 207.0 |
| 只挂 `MindMapCanvas` + `waitFor`(50ms) | **384.6ms** | 102.8 | 140.7 | 66.4 | 74.1 |
| 只挂画布 + `waitFor`(5ms) | **317.5ms** | 97.6 | 124.7 | 18.6 | 75.9 |

`mount` 一项 318.5 → 102.8ms（**3.1×**），整条 869.5 → 384.6ms（**2.3×**）。

注意第三行：把 `vi.waitFor` 的轮询间隔从 50ms 压到 5ms 还能再省 67ms。**本轮没做**——
它改的是"等多久才检查一次"，与这条用例要证明的事无关，但那属于另一类优化（让等待更密，
失败时更费 CPU），留着当备选。

### 2.3 C 之后还剩什么（残余 A）

改后加压最坏 **3593ms**，其中大头是"真挂画布"：Vue Flow 建图 + `MindNode` 真渲染 +
四次 `waitFor` 轮询等流帧。那正是这条用例存在的理由（"节点**真的**长在画布上"），
stub 掉就没有断言可读了。所以上限 10 秒：本地加压 3593ms × 2.8、CI 估
（3593 × 1.3–2 = 4.7–7.2s）的 1.4–2.1×。

---

## 3 `canvas-keys`：判 C，而且这一条最该判 C

它测的是"焦点落在**别的按钮**上时按 Tab / Enter / Delete 不改图"。判据在
`MindMapCanvas.vue`：

```ts
function ownsStructureKeys(event: KeyboardEvent): boolean {
  if (isTyping(event.target)) return false;
  const el = shell.value;
  if (!el) return false;
  const active = document.activeElement;
  if (!active || !el.contains(active)) return false;   // 焦点必须在画布外壳里
  return !isControl(active as HTMLElement);            // 且不落在控件上
}
```

所以这条用例需要的东西只有两样：**画布外壳存在**、**画布外有个真实控件能拿焦点**。
从前为了这两样 `mount(App)`，把 `ChatDock` / `SettingsPanel` / `HistoryDrawer` /
`StructureCheck` / `NodeToolbar` / `AgentPanel` 全挂了起来。

### 3.1 同进程对照（6 次均值，热后）

| 段 | 挂 `App` 壳 | 最小壳（画布 + `MessageFab`） |
|---|---|---|
| `mount` | 15.8ms | **6.7ms** |
| `flushPromises` | 2.8ms | 0.6ms |
| `loadMap` + `select` + `nextTick` | 8.5ms | 5.4ms |
| 3 次 `keydown` | 1.66ms | 0.89ms |
| `unmount` | 1.6ms | 0.8ms |
| **合计** | **30.4ms** | **14.4ms** |
| DOM 元素数 | **223** | **49** |

冷启动第一条（含组件首次求值）：`mount` **132.1ms**、整条 168.3ms（挂 `App`）。

所以 223 个 DOM 里只有 49 个与断言有关，**78% 的挂载成本与断言无关**。

### 3.2 减少的是"无关的兄弟组件"，不是断言对象

换成最小壳之后：

- 画布还是 `MindMapCanvas`（`shell`、`ownsStructureKeys`、`window` 监听都在它身上）；
- 画布外的按钮还是 `MessageFab` **真实渲染**的 `[data-test="message-fab"]`
  ——不用裸 `<button>`，因为判据之一 `isControl()` 读的就是真实控件的 `tagName` / `role`；
- `store.nodeCount` / `defaultPrevented` / `document.activeElement` 一个断言都没改。

覆盖面：`App` 壳自己的键盘行为（Esc 关抽屉、`Ctrl+S`、抽屉的 Tab trap）**不在这条用例的
断言里**，由 `shell-actions` / `shell-layout` / `app.spec` 覆盖。

### 3.3 C 之后还剩什么（残余 A）

改后加压最坏 **2446ms**，几乎全是 `MindMapCanvas` 的首次求值（Vue Flow 建图 +
`MindNode` 真渲染 + 键盘监听注册）——判据在组件里，stub 掉就没得测。上限 8 秒：
本地加压 2446ms × 3.3、CI 估（2446 × 1.3–2 = 3.2–4.9s）的 1.6–2.5×。

---

## 4 三条的上限与余量

| 用例 | 判 | 本地单跑 | 本地加压最坏 | CI 估（×1.3–2） | 上限 | 距上限余量 |
|---|---|---|---|---|---|---|
| `history` 字节超上限 | A | 286ms | **6606ms** | 8.6–13.2s | **15s** | 2.3× |
| `canvas-preview` 生成中长节点 | C→A | 237ms | **3593ms** | 4.7–7.2s | **10s** | 2.8× |
| `canvas-keys` 焦点在别的按钮上 | C→A | 122ms | **2446ms** | 3.2–4.9s | **8s** | 3.3× |

为什么要给每一条分档："最坏"不是某一次读数，是**观测集合的上界**——

```
history       6606ms  （其余：365 / 2537 / 2640 / 3106 / 3480）
canvas-preview 3593ms （其余：658 / 1953 / 2927 / 3829 / 4290）
canvas-keys    2446ms （其余：1441 / 1904 / 2128 / 3483 / 3801 / 3941）
```

三条都是**逐 `describe` 的 `timeout`**，全局 `testTimeout` 一个字都没动。

为什么不统一给 20 秒：20 秒是"136 个节点全摊在画布上"那个量级的价码。这三条里最重的
也就 6.6 秒，统一按 20 秒放行等于给真卡死留了 3 倍空间。分档给，每一档都写清来历。

---

## 5 自评：根因是 jsdom，还是我们的组件太重？

**两边都不是主因。真正的放大器是"宿主机状态"，但组件确实有可砍的东西。**

### 5.1 jsdom 不是根因

jsdom 不做 layout、不做 paint，创建一个 DOM 元素的成本在 0.05ms 量级。实测：一个
`MindNode` 渲染出来的**元素总数是 6 个**（探针读数 `querySelectorAll('*').length = 5`），
整套 2 节点画布的 DOM 只有 36 个——这点 DOM 在 jsdom 里不超过 0.5ms。

所以 2–5ms/节点（空闲）、21–42ms/节点（加压）这些数字里，**DOM 几乎不占**。它们花在：

1. Vue 组件实例化：一个 `MindNode` 有 ~20 个 `computed`、2 个 `immediate: true` 的 `watch`
   （其中一个在挂载时就跑一次）、一个 `Teleport`（悬停卡片）；
2. Vue Flow 的**每节点管理器**：`NodeWrapper` 的 handle 边界计算、`ResizeObserver` 注册、
   dimensions 更新、每次 patch 的 diff；
3. SFC 模块的首次求值（Vite transform 产物执行一次，之后共享）。

### 5.2 38ms/节点"正常"吗

不正常，但也不神秘：空闲时 2–5ms/节点，加压时 ×8–19。**乘法因子来自 CPU 争用，不是代码。**
本轮最有说服力的数字是：同一份代码、同一分钟、同一次加压的两份进程里，
`history` 那条跑了 365ms 和 6606ms（**18×**）；`canvas-keys.spec.ts` 单跑跑过 1.65s
的 `Duration`，也跑过 19.26s——同一台机器。

### 5.3 该不该单开一轮修组件

值得单开一轮，但**靶子不该是 "MindNode 的初始化"，而是 Vue Flow 的每节点成本**：

- 本仓 76 个 spec 文件，每轮的 `environment`（每个文件建一次 jsdom）占整轮 **38–73%**。
  这部分是 vitest 的 per-file 成本，改 `MindNode` 一点都动不了它。
- `MindNode` 里能砍的"每实例初始化"（两个 watcher、Teleport、20 个 computed）在真浏览器里
  也要付——砍它是**产品性能优化**，不是测试优化，收益面是用户而不是 CI。
- 真正"砍一刀全仓受益"的位置是：**每个真渲染的节点都要经过一次 `NodeWrapper`**。本仓有
  20+ 个 spec 挂真画布，136 节点那两条就是被这个成本顶到 20 秒的。如果 `NodeWrapper` 的
  handle 计算 / ResizeObserver 注册能惰性化（只在真有连线或真量尺寸时才做），收益是全局的。
- **不建议**的做法：给测试换一个"轻量 MindNode"。断言读的就是真组件算出来的
  `branchHidden` / `hiddenChildren` / 角标文案（见 `TEST-COST-136-NODE.md` §4 的规矩 ④），
  换掉等于把要证明的东西一起换掉。

一句话：**本轮该做的（C）已经做了，剩下的是"产品渲染成本"，属于另一个议题，要有独立的
判定与收益核算，不能顺手塞进测试超时里。**

---

## 6 顺带发现的洞：`history` 的字节上限断言目前恒真

`history.spec.ts` 第二条的注释写着"每个节点 80 字中文，快照约 20KB 量级；300 个分支足够
在几十步内撑满 10MB"。实测不是：

```
一份快照 82.5KB（不是 20KB）→ 50 步（HISTORY_LIMIT 先命中）× 70.5KB = 3525KB
HISTORY_BYTES_LIMIT = 10240KB
```

`undoDepth` 稳定在 50、`undoBytes` 稳定在 3525KB，**字节上限从未触发过一次**。
所以 `expect(store.undoBytes).toBeLessThanOrEqual(HISTORY_BYTES_LIMIT)` 这条断言恒真，
这条用例实际测的是**步数上限**。

要让字节上限真正生效，每份快照得 > 200KB（例如 1000 节点 × 160 字 label），那条会
比现在更慢（3 倍规模）——**不该和"修超时"混在一轮做**。本轮只记录，不动它；下一轮如果要
补这个洞，得连同上限一起重新算。

---

## 7 复现

```bash
cd frontend
# 单跑
npx vitest run tests/history.spec.ts --reporter=verbose
npx vitest run tests/canvas-preview.spec.ts --reporter=verbose
npx vitest run tests/canvas-keys.spec.ts --reporter=verbose

# 全量 / 加压（两份同时跑）
npx vitest run --reporter=verbose
npx vitest run --reporter=verbose & npx vitest run --reporter=verbose

# 改前对照：先回退这三个文件再跑同一条件
git stash push -- frontend/tests/{history,canvas-preview,canvas-keys}.spec.ts
# ... 跑完再 git stash pop
```

插桩的三段都是**一次性探针**，没有留在 `tests/`：`vi.mock` 不必，只要在调用序列外面套
`performance.now()` 就能把 stringify / 深拷贝 / diff / encode 分开记账；组件级对照用
"同一进程内交替挂 `App` 与最小壳，各 N 次取均值"。留下会变成第 77 个文件，它自己在 CI 上
也要占几秒——同一个问题的新来源。
