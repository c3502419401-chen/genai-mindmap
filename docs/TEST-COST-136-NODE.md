# 136 节点那两条用例为什么慢：成本分解、判定与"该给多少超时"

> 2026-11-25，本机（Windows / Node 24 / vitest 5.0.5 / jsdom 30）实测。
> 对象：`frontend/tests/view-collapse-canvas.spec.ts`、`frontend/tests/branch-digest.spec.ts`。
> 触发：CI（`frontend` job）红在这两条上——本机 76 files / 1276 tests 全绿，两条用例各剩
> ~0.5 秒余量，CI runner 一慢就过线。
>
> **结论先行**：这两条不是"测量型"，也不是"重复计算"，是**规模型**。
> 一条用例全程只跑 **1–2 次** `layoutTidy`，合计 **<1.1ms**；整条耗时的 **99.98%** 是
> jsdom 里真渲染 40–136 个 `MindNode` 组件（≈33ms/节点，重负载）。而"136 个节点真的画在
> 画布上、界面说了什么"正是它们要证明的东西——所以给显式上限是诚实，不是掩盖慢。

---

## 0 现象：同一份代码，四档负载差 10–18 倍

```bash
cd frontend
npx vitest run tests/view-collapse-canvas.spec.ts --reporter=verbose   # ① 单跑
npx vitest run --reporter=verbose                                      # ② 全量并行
npx vitest run --reporter=verbose                                      # ③ 与 ② 同时起两份（加压）
```

| 用例（都是"在真画布上装 136 节点那棵树"） | ① 单跑 | ② 全量并行 | ③ 加压 | ④ 早期重负载 |
|---|---|---|---|---|
| `view-collapse-canvas` 点「全部展开」 | 363ms | 2043–3547ms | **8896ms** | 5332ms |
| `view-collapse-canvas` 点「收起到上限」 | 358ms | 1684–1760ms | **6912ms** | 5276 / 5469ms |
| `branch-digest` 全展开时一枚都没有 | 320ms | 1434–1557ms | **6170ms** | 3816ms |
| `branch-digest` 收起态 6 枚 | 227ms | 654–1873ms | 5644ms | 2731ms |
| `view-collapse-canvas` 装 136 节点的真图 | 234ms | 641–1761ms | 5323ms | 2989 / 3229ms |
| `branch-digest` 点角标 → 整支冒出来 | 161ms | 828–1072ms | 3633ms | 2294ms |
| `view-collapse-canvas` 展开后「−」收回去 | 151ms | 692–713ms | 3484ms | 2284ms |
| `view-collapse-canvas` 每个截断节点一枚「+N」 | 122ms | 656–771ms | 2918ms | 1796 / 1946ms |
| `branch-digest` 角标上的「还有 N 个 · 概括」 | 106ms | 713–1611ms | 2798ms | 1634ms |
| `branch-digest` 只挂在一级分支上 | 88ms | 416–750ms | 2046ms | 1061ms |
| `branch-digest` 旧图一枚都不出现 | 73ms | 443–467ms | 2009ms | 940ms |
| `branch-digest` 幽灵概括那支不画 | 67ms | 370–384ms | 1735ms | 881ms |
| `branch-digest` 只差 2 个的支不挂角标（小树） | 26ms | 92–113ms | 487ms | 323ms |
| `branch-digest` `unshown` 与 `folded` 不是同一个量 | 1ms | 1–4ms | 12ms | 8ms |
| `view-collapse-canvas` `MindNode` 单组件四条 | 2–5ms | 5–10ms | 21–42ms | 1–8ms |

**读法**：默认 `testTimeout = 5000` 在这条链上不是"还差一点"，是"余量取决于邻居"。
加压那一列里最重的 5 条全部 >5000ms —— 也就是说，**不给显式上限，加压时这个文件必然红 5 条**。

CI 拿不到日志。按 runner 比本机慢 1.3–2× 估：

> **CI ≈ 11.6–17.8 秒**（按加压列最坏 8.9s 算；这是**估算**，不是实测）。

---

## 1 排除"重复计算"（B）：布局只跑 1–2 次，合计 <1.1ms

插桩：`vi.mock('../src/canvas/layout')` 包一层真实实现，记录每次 `layoutDiagram` 的
入参节点数与自身耗时。读数（136 节点那棵树）：

| 项 | 实测 | 说明 |
|---|---|---|
| `layoutTidy(136 节点)` 单次 | **0.51ms** | 100 次取均值；Reingold–Tilford 定尺寸变体，`subtreeExtent` 带 memo |
| `viewTree(136 节点)` 单次 | **0.80ms** | `canvas/view-collapse.ts`，四遍遍历 |
| 一条用例里 `layoutDiagram` 被调用几次 | **1 次**（`preview` 时）；点「全部展开」**再 1 次** | 视图树变 → `renderSource` 失效 → `layout` computed 重算，一帧一次 |
| 那两次的耗时 | 0.43ms（40 节点视图树）+ 0.54ms（136 节点视图树） | `positions` 数量与 `nodes` 数量同步（40 → 136），没有"几何没跟上视图" |
| 布局占整条用例 | **<0.02%** | 5.3 秒里 1ms |

**"136 节点 × N 次 layout"这个假设不成立**：实际是 1 次。`store.layout` / `store.nodes`
都是 `computed`，视图状态一改才重算，同一帧里多个读者读的是同一份结果。

---

## 2 排除"重复建树"（C）：共享画布只省 ~130ms/条

两条 spec 的每个 `it` 都 `mount(MindMapCanvas)` 一次（各自新建 pinia），看起来像"每条
用例都重新 build 一棵 136 节点的树"。于是写了一组同进程、相邻执行的对照探针：

| 形态 | 三条同形用例耗时（空闲） |
|---|---|
| A：每 `it` 独立 `mount` + `preview`（现状） | 401 / 816 / 1505ms |
| B：`beforeAll` 共享一个画布，每 `it` 前 `preview` 复位 | 307 / 970 / 1039ms |

**B 没有稳定收益，原因是结构性的**：共享只能省掉 `mount` 本身（空画布 ≈130ms），而每条
用例开头**仍要 `preview` 一次**——那一次就要重挂 40 个 `MindNode`（≈0.3 秒）。把"隔离"
换成"每 it 显式复位"（`preview` 不复位 `viewExpandedNodes` / `viewCollapsedNodes`，得自己
调 `collapseViewToCap()`），换来 <10% 的收益与 it 之间的顺序耦合，不划算。

顺带排除了另一个候选：`attachTo: document.body`。空画布对比 981ms（attach）vs 132ms
（不 attach），但真实用例里移除它**没有可复现的收益**（3229 → 2989ms，噪声内），所以
两份 spec 原样保留 `attachTo`。

---

## 3 定位：成本 100% 在"jsdom 里挂 `MindNode`"

| 项 | 实测 | 推导 |
|---|---|---|
| 单个 `MindNode` 组件 `mount` | **38ms**（jsdom） | 组件 31KB，含角标 / tooltip / 字符宽度测量 |
| 40 个节点上画布（收起态） | ~0.3s（空闲） | 40 × 38ms 的量级 |
| 136 个节点（全展开） | 3.4–5.1s（重负载）/ 0.36s（空闲） | ≈**33ms/节点**，线性 |
| 「全部展开」那一步（40 → 136，新增 96 个） | 2.8–3.9s（重负载） | 96 × 38ms + Vue Flow 的节点 diff |
| 「收起到上限」那一步（136 → 40，卸 96 个） | 0.77s | 卸载比挂载便宜一个量级 |

载荷是**节点数**，不是交互次数：`unshown` / `folded` 那条纯函数用例（不挂画布）1–12ms，
`MindNode` 单组件四条 1–30ms。同一份代码重负载下慢 10–18 倍 —— 这是 **CPU 争用**，
不是在等定时器或网络。

---

## 4 判定：A（规模型），20 秒显式上限

三分法（A 测量型 / B 重复计算 / C 走了不该走的重路径）里，这两条是 A 的近亲，但要说准：

```
它们不是"跑 N 次布局"型（那是 layout-clarity 那条 58 图 × 三套几何），
是"跑 N 个节点真渲染"型：136 个节点全摊在画布上，就是它们要证明的东西本身。
```

- **不许降规模**：断言读的是 `MindNode` 上的角标 DOM（`+N` 的数字与文案、「−」的
  `opacity-100`、点角标整支冒出来）。136 → 20 等于把"数量口径"这个前提抽掉，
  这些用例存在的理由就没了。
- **不许 stub 节点**：`shallowMount` / stub 掉 `MindNode` 之后，读到的是占位符，
  不是真组件算出来的 `branchHidden` / `hiddenChildren`。
- 所以只剩一件事可做：**把上限按 CI 的量级给足，并在注释里写清它为什么该慢。**

20 秒的来历：

| 参照 | 值 | 20 秒是它的 |
|---|---|---|
| 本地加压（两份 vitest 同时跑）最坏 | 8.9s | 2.2× |
| CI 估算（加压 × 1.3–2） | 11.6–17.8s | 1.1–1.7× |

不给 30 秒：真卡死（死循环、等一个永远不来的 promise）在 20 秒内已经能暴露。
反过来说，**若 CI 比本机加压再慢一倍还会红，那时的正确答案是把 136 个节点的渲染成本
本身降下来**（例如给 `MindNode` 一条轻量渲染路径，或把角标拆成一个不含 tooltip /
字符测量的子组件），不是再把超时往上加——超时只是给"固有成本"留位置，不负责掩盖
"成本涨了"。

落地方式：**逐 `describe` 的 `timeout` option**，不动全局 `testTimeout`——只放宽"在真
画布上装 136 节点"的那几组；纯函数组（1–12ms）与 `MindNode` 单组件组（1–30ms）一个数字
都不动。全局放宽会让整个仓库失去超时保护。

---
