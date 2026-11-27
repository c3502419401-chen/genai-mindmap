# 写用例时的三条规矩（负载这块）

这个目录里的用例是在 jsdom 里跑**真的组件、真的 Vue Flow、真的几何**的，所以"慢"不是风格问题：
全量并行（CI 的口径，15 个 worker）会把慢用例放大到单跑的 9–14 倍。
完整实测与判据见 `docs/TEST-LOAD-SENSITIVITY.md`，这里只放会疼到你的那三条。

## 1 单文件跑 ≠ 验证

```bash
npx vitest run tests/defects.spec.ts   # 只用来快速看"这条有没有明显坏掉"
npm test                               # 判绿的口径：全量并行，与 CI 同形
```

一条 635ms 的用例，全量下是 4313ms。**单跑的绿灯不能作为"这条没问题"的结论**——
本仓库三次在负载上翻车，三次都是这么来的。

## 2 单跑 ≥ 500ms ⇒ 必须写显式 `timeout`

500ms × 6（实测并行放大下界）已经压到默认 5000ms 的 2× 安全线以下。所以：

```ts
// 上限 = 满载实测 × 2，并在注释里记三项读数（缺一项 review 时没法核）
// 单跑 635ms ／ 全量空闲 3653ms ／ 全量 + 后端 pytest 4313ms → 15000
it('……', { timeout: 15_000 }, () => { /* ... */ });
```

不许只按单跑定上限（会低 6–14 倍），也不许给到 30 秒以上（真卡死的用例也会判绿）。

## 3 减负载只减"与断言无关的那部分"

可以先问三个数（`docs/TEST-LOAD-SENSITIVITY.md` §3.2 有标定）：

```
满载估计 ≈ 40ms × 挂的 MindNode 数 + 400ms × 真画布挂载次数 + 6.3ms × 真几何调用次数
```

- 挂整 `App` 只为了读画布 → 换成 `mount(MindMapCanvas)`（这是唯一被鼓励的减法，
  `canvas-preview.spec.ts` / `canvas-keys.spec.ts` 已经做过）。
- 136 节点的树降到 20 个节点 → **不许**：规模就是断言的前提。
- 把 `previewMove` / `layoutTidy` mock 掉 → **不许**：断言就是"坐标真的算出来了"。

## 改完记得更新基线

```bash
cd frontend
npm run test:cost        # 空闲档（30–90 秒）
npm run test:cost:report # 拼表 + 更新 scripts/test-cost-baseline.json
npm run test:cost:gate   # 与 CI 同一条闸门
```

闸门在 CI 上跑（`.github/workflows/ci.yml` 的 `用例成本闸门`）：上限不到满载 1.5 倍、或单跑已
≥ 500ms 却还是默认 5000，都会红。**基线是产物，改测试成本就必须重跑**——这样"我调快了/调慢了"
在 diff 里看得见，而不是只写在提交信息里。
