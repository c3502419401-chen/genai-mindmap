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
> | `history` 字节超上限时丢最旧的 | **A**（规模型） | 真实编辑次数 × 每次约 3ms 的序列化/深拷贝/diff，**零渲染** | 换负载形状（60 节点 × 满 detail × 40 步，见 §6）+ 8 秒上限 |
> | `canvas-preview` 生成中真的长出节点 | **C** → 残余 A | 为了 3 条断言挂了整个 `App` 壳 | 只挂 `MindMapCanvas`，再给 10 秒 |
> | `canvas-keys` 焦点在别的按钮上 | **C** → 残余 A | 同上，`App` 壳 223 个 DOM 里只有 49 个与断言有关 | 最小壳（画布 + `MessageFab`），再给 8 秒 |
>
> 本轮实测还顺带证伪了一个流传的说法：**"加压 2.7×"不是常数**。同一份代码、同一分钟，
> 同一条用例在同一次加压的两份进程里跑了 **365ms 和 6606ms**（18 倍）。所以上限必须按
> **见过的最坏值**定，不能按某一次读数定。
>
> 另一个后续（§6）：`history` 那条的字节上限断言原本是**恒真**的（步数上限先命中，字节
> 规则从未执行）。它被改成"抽纯函数测规则 + 换负载形状让字节真的先命中"，最后加压
> 从 6606ms 降到 **146ms**，上限从 15 秒收到 8 秒。

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
换个进程就超。本轮先给它 15 秒上限止血，随后又把它整条**改对了负载**（§6）——
换负载之后的加压读数是 **119 / 146ms**（两份 61s / 57s 那一轮），上限也跟着收到 8 秒。
也就是说：这条最终不是"靠上限活着"，是**先把不该那么大的成本降下去，再按降完的量级给上限**。

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

75 次淘汰 × 0.117ms ≈ **8.8ms**，占 281.8ms 的 **3%**。当时判"不值得为它改生产代码"——
后来改这条用例时顺手把它做了（淘汰时字节随条目一起存，见 §6），因为它和"抽纯函数"是同一处改动。

### 1.4 判定

```
A（规模型）：真实编辑次数，每次 3ms 是生产代码每改一个节点都要付的钱
            （撤销要序列化、树要不可变更新、账本要 diff）。
规模不许降：步数与节点数是"把某个上限顶满"这个前提本身。
```

### 1.5 后来的第二次改动：换负载形状（详见 §6）

上面的插桩是**旧负载**（300 节点 × 80 步 / 单份 82.5KB）的读数。那条用例后来被改造成
"**60 个满 `detail` 节点 × 40 步**（单份 364KB）"——它终于真的顶到了字节上限，
而成本反而降了一半：

| | 旧负载 | 新负载 |
|---|---|---|
| 单份快照 | 82.5KB | **364KB** |
| 步数 | 80 | **40** |
| 谁先命中上限 | 步数（50） | **字节（10MB，第 29 步）** |
| 单跑耗时 | 275–286ms | **128ms** |
| 加压耗时 | 2537 / 2640 / 3480 / 6606ms | **119 / 146ms** |

步数减半、每步成本相当（stringify 变贵但深拷贝与 diff 变便宜，两边抵掉），所以总成本
跟着步数走。**这就是"造大字节不必造多节点"的实际收益：规则测得更真，还更快。**

上限 8 秒的来历：本地单跑 128ms、本地加压最坏 146ms；但同一条用例在**旧负载**下加压
见过 6606ms（相对单跑 281ms 放大约 24×），按这个放大上界推新负载 ≈ 128 × 24 = 3.1s，
CI 估 ≈ 3.1 × 1.3–2 = **4.0–6.2s**，所以给 8 秒（CI 估的 1.3–2×）。

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
| `history` 字节超上限（换负载后） | A | 128ms | **838ms** | 1.1–1.7s | **8s** | 9.5×（实测）/ 4.7–7.3×（CI 估） |
| `canvas-preview` 生成中长节点 | C→A | 237ms | **3593ms** | 4.7–7.2s | **10s** | 2.8× |
| `canvas-keys` 焦点在别的按钮上 | C→A | 122ms | **2446ms** | 3.2–4.9s | **8s** | 3.3× |

为什么要给每一条分档："最坏"不是某一次读数，是**观测集合的上界**——

```
history        6606ms（旧负载；换负载后 119 / 146 / 838ms）
canvas-preview 3593ms（其余：658 / 1774 / 1953 / 2441 / 2708 / 2927 / 3829 / 4290）
canvas-keys    2446ms（其余：574 / 1408 / 1441 / 1904 / 2077 / 2128 / 3483 / 3801 / 3941）
```

`history` 那条的 8 秒是**两种推法里宽的那个**，值得说清：按 CI 系数（×1.3–2）推只要
1.1–1.7s，按本机实测的抖动倍数（单跑 128ms → 加压 838ms，6.5×）推要 5.4s——两者差 3 倍。
取宽的，理由是**宽的那一档也仍在"真卡死会红"的范围内**：这条用例的正常成本是 128ms，
死循环/等一个不来的 promise 不会在 8 秒里装成正常。

三条都是**逐 `describe` 的 `timeout`**，全局 `testTimeout` 一个字都没动。

为什么不统一给 20 秒：20 秒是"136 个节点全摊在画布上"那个量级的价码。这三条里最重的
也就 3.6 秒，统一按 20 秒放行等于给真卡死留了 5 倍空间。分档给，每一档都写清来历。

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

### 5.4 裁定：`NodeWrapper` 那条建议**不派**

上面那条"单开一轮修 Vue Flow 的每节点成本"提出来之后被否了，理由就写在这一节的最后一段
自身：**它的靶子（`MindNode` / `NodeWrapper` 的实例成本）动不了 CI 的大头（每个 spec 文件
一次 jsdom environment，占整轮 38–73%）**。于是它只有两种归宿——

- 要么是**产品性能优化**（用户开一张 136 节点的图更快），那与 CI 无关，该走产品那条线；
- 要么解决不了 CI 的大头，那就不该记成"测试成本问题"。

两样都不落在这一轮的主题上，所以**不派**。真要让 CI 快，下一个该看的是
"76 个文件里的 environment 成本能不能摊薄"（例如把挂真画布的 spec 合并到少数几个文件），
那是一个独立议题，需要自己的判定。

---

## 6 那个恒真的断言：查清楚之后修掉了

### 6.1 现象：它测的其实是步数上限

`history.spec.ts` 第二条的注释原本写着"每个节点 80 字中文，快照约 20KB 量级；300 个分支
足够在几十步内撑满 10MB"。实测不是：

```
一份快照 82.5KB（不是 20KB）→ 50 步（HISTORY_LIMIT 先命中）× 70.5KB = 3525KB
HISTORY_BYTES_LIMIT = 10485760 字节（10MiB）
```

`undoDepth` 稳定在 50、`undoBytes` 稳定在 3525KB，**字节上限从未触发过一次**。
所以 `expect(store.undoBytes).toBeLessThanOrEqual(HISTORY_BYTES_LIMIT)` 恒真。

### 6.2 关键算式：字节上限能不能先命中，只看单份快照多大

```
字节先命中  ⇔  单份 × 步数上限 > 字节上限  ⇔  单份 > 10MiB / 50 = 204.8KB
```

204.8KB 是分水岭。旧负载 70.5KB 在分水岭下方 3 倍，所以那条用例里字节规则**永远是死代码**。

那能不能靠 `detail` 把单份灌上去？先算清楚（汉字在 JSON 里不转义，UTF-8 是 3 字节/字）：

| 项 | 值 |
|---|---|
| `LIMITS.detail` | 2000 字 → **6000 字节** |
| `LIMITS.nodes` | 1000 |
| 一个满 `detail` + 满 `label` 的节点 | ≈ 6.6KB |
| 契约上限下**单份快照的最大值** | ≈ **6.6MB**（1000 × 6.6KB） |

所以"靠数据把**单份**灌到 10MB"确实不现实（那需要 1500+ 个满 detail 节点，超过节点上限）。
**但灌满单份根本不需要——要的是让"单份 × 50 > 10MB"**，即单份 > 204.8KB。60 个满 detail
节点就够：60 × 6.2KB ≈ **364KB**，是分水岭的 1.8 倍。

### 6.3 选 (a) 还是 (b)：都不是，是 (b) + 换负载

**否 (a)「把 LIMIT 调小到现实可测的量级」（比如 512KB）**：那是**产品行为变更**。当前
10MiB 允许"300 节点的大图撤 50 步"（3525KB）；降到 512KB，同一张图只能撤 **7 步**——用户
按 Ctrl+Z 忽然撤不动了，而理由是"为了让一条测试好写"。方向错了：测试应当描述产品行为，
不该反过来定义它。而且调小之后，"要触发仍需造 >10KB/份的负载"，问题只是搬家。

**采用 (b)**：把**规则**抽成纯函数直接测，把**链路**那条改成"有条件真"。

### 6.4 做了什么

1. **抽纯函数** `frontend/src/mindmap/history-limit.ts::evictCount(entryBytes, limits)`：
   从最旧开始要丢几份。三条边界与原 `while` 逐字对应（`kept > 1` 至少留一份、
   `kept > count`、`bytes > bytesLimit` 是**或**关系）。
2. **接线**：`recordPast()` 改成"压栈 → 问 `evictCount` → 按它说的份数 `shift()`"。
   顺手把淘汰时**重复编码**去掉了（`past` 从 `string[]` 改成 `{snap, bytes}[]`，字节随条目
   存）——原代码每淘汰一份就要把同一份字符串再 `encode` 一次，那正是 §1.3 里记的 3%。
3. **纯函数测试 8 条**（`history.spec.ts` 第二个 describe，全部 0ms）：
   不超限不淘汰 / 正好等于上限不淘汰（判据是"超过"不是"达到"）/ 刚超一点只丢最旧 /
   远超限丢到不超为止 / 步数先命中按步数丢 / **一份自己就超限时至少留一份** /
   空栈 / 按生产上限摆一遍（40 份 × 364KB 丢 12 份）。
4. **链路那条改造成真触发**：`fatTree(60)` = 60 个满 `detail` 节点，单份 364KB，
   第 29 步顶满 10MB，跑 40 步（`expect(steps).toBeLessThan(HISTORY_LIMIT)` 写在前面当前提）：

   ```ts
   expect(store.undoDepth).toBeLessThan(steps);              // 步数没到线，栈却短了 → 只可能是字节干的
   expect(store.undoBytes).toBeLessThanOrEqual(HISTORY_BYTES_LIMIT);
   expect(store.undoBytes).toBeGreaterThan(HISTORY_BYTES_LIMIT / 2);  // 丢到不超为止，不是一超就清
   ```

   这三条是**条件真**：字节规则若不生效，`undoDepth` 会是 40，第一条就红。

### 6.5 收益：更真，还更快

| | 旧（恒真） | 新（条件真 + 纯函数 8 条） |
|---|---|---|
| 单份快照 | 82.5KB | 364KB |
| 步数 | 80 | 40 |
| 谁先命中上限 | 步数（字节规则从未执行） | **字节（第 29 步）** |
| 单跑 | 275–286ms | **128ms** |
| 加压（两份全量） | 2537 / 2640 / 3480 / 6606ms | **119 / 146 / 838ms** |
| 用例条数 | 4 | 12（+8 条纯函数，0ms） |

加压最坏值降了 **45 倍**，超时上限跟着从 15 秒收到 8 秒。**"造大字节不必造多节点"这条
是这里最值钱的一句**：一个满 `detail` 节点 6KB，节点数反而从 300 降到 60——stringify 变贵
一点点，深拷贝与全树 diff 便宜一大截，总成本由步数主导（80 → 40，砍半）。

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

# 单份快照多大（决定"字节上限能不能先命中"，分水岭 204.8KB）
node -e "const d='详情'.repeat(1000);const t=n=>JSON.stringify({title:'A',root:{id:'root',label:'根',detail:d,children:Array.from({length:n},(_,i)=>({id:'n'+i,label:'分支'+i,detail:d,children:[]}))}});for(const n of [30,36,48,60])console.log(n, Buffer.byteLength(t(n)), 'bytes')"
```

插桩都是**一次性探针**，没有留在 `tests/`：`vi.mock` 不必，只要在调用序列外面套
`performance.now()` 就能把 stringify / 深拷贝 / diff / encode 分开记账；组件级对照用
"同一进程内交替挂 `App` 与最小壳，各 N 次取均值"。留下会变成第 77 个文件，它自己在 CI 上
也要占几秒——同一个问题的新来源。
