/**
 * ②③ 硬断言：真浏览器走一遍「传材料 → 不写主题 → 生成 → 落地」，量视口。
 *
 * 判据（2026-11-28 复验用这三条，理由见 `docs/ACCEPTANCE-WALKTHROUGH-2026-11-28.md` §六.2）：
 *   1. **根节点必须在视口内**——它区分"没适配"与"适配到了一个看不全的档"；
 *   2. 可见比例：落在「全图」档必须是 100%；落在「适应宽度」档要不低于这张图的物理上限
 *      （本机实测 0.66，上限由图形状决定，不是固定值）；
 *   3. 材料带可用文件名时，主题空着也点得动「生成导图」（后端 `goal` 是 `min_length=1`，
 *      前端只用文件名兜底，粘进来的正文没有文件名）。
 *
 * 生成前**先拖一次画布**：真实用户会这么干（或生成期间点过引用跳转），
 * 而 `store.viewAdjusted` 就是被这一下置真的。
 *
 * 用法: node 21-journey-viewport.mjs [SID] [tag]
 *
 * 跑法同本目录其余脚本：`playwright-core` **不在** `frontend/package.json` 里，把整个
 * 目录复制到装了它的隔离目录再跑（`lib.mjs` 的 `EXE` 是本机 Chromium 的绝对路径）。
 */
import fs from 'node:fs';
import { open, sleep, SHOTS } from './lib.mjs';

const sid = process.argv[2] ?? 'S08';
const tag = process.argv[3] ?? 'J1';
const samples = JSON.parse(fs.readFileSync('D:\\genai-mindmap\\backend\\scripts\\acc_samples.json', 'utf8'));
const s = samples.samples.find(x => x.id === sid);
const dir = 'D:\\_acc-placeholder';
fs.mkdirSync(dir, { recursive: true });
const file = `${dir}\\${s.id}-${s.title}.md`;
fs.writeFileSync(file, s.text, 'utf8');

/** 节点画布上的读数：视口矩形、节点数、可见数、根节点（最左那一列）、当前档位。 */
const readCanvas = page => page.evaluate(() => {
  const pane = document.querySelector('.vue-flow');
  if (!pane) return { error: 'no .vue-flow' };
  const pr = pane.getBoundingClientRect();
  const nodes = [...document.querySelectorAll('.vue-flow__node')].map((el) => {
    const r = el.getBoundingClientRect();
    return {
      label: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 24),
      x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
    };
  });
  const inside = nodes.filter(n => n.x > pr.left && n.x < pr.right && n.y > pr.top && n.y < pr.bottom);
  const root = nodes.slice().sort((a, b) => a.x - b.x)[0];
  const rootInside = !!root && root.x > pr.left && root.x < pr.right && root.y > pr.top && root.y < pr.bottom;
  return {
    rendered: nodes.length,
    inside: inside.length,
    ratio: nodes.length ? +(inside.length / nodes.length).toFixed(3) : 0,
    root,
    rootInside,
    mode: document.querySelector('[data-test="view-mode-all"]')?.getAttribute('aria-pressed') === 'true'
      ? 'fit-all' : 'fit-width',
  };
});

const { browser, page, log } = await open();
await page.goto('http://localhost:5173/', { waitUntil: 'networkidle', timeout: 60000 });
await sleep(1500);

await page.setInputFiles('input[type=file]', file);
const submit = () => page.evaluate(() => {
  const btn = [...document.querySelectorAll('button')]
    .find(b => (b.getAttribute('aria-label') || '') === '生成导图');
  return btn ? { disabled: btn.disabled, title: btn.getAttribute('title') } : null;
});
for (let i = 0; i < 40; i++) {
  await sleep(500);
  const state = await submit();
  if (state && !state.disabled) break;
}
console.log('空主题时：', JSON.stringify({
  ...(await submit()),
  hint: await page.evaluate(() => document.querySelector('[data-test="intent-hint"]')?.textContent?.trim() ?? null),
}));

// 用户随手拖过画布 —— 这一下把 `viewAdjusted` 置真，正是验收现场那一屏。
await page.mouse.move(700, 430);
await page.mouse.down();
for (let i = 1; i <= 8; i++) { await page.mouse.move(700 + i * 70, 430 + i * 40); await sleep(30); }
await page.mouse.up();
await sleep(400);

const t0 = Date.now();
await page.getByRole('button', { name: /生成导图/ }).click();
let landed = null;
for (let i = 0; i < 150; i++) {
  await sleep(1000);
  if (/已落到画布/.test(await page.evaluate(() => document.body.innerText))) {
    landed = Date.now() - t0;
    break;
  }
}
await sleep(1200); // 适配是 150 毫秒节流 + 一帧
await page.screenshot({ path: `${SHOTS}\\${tag}-landed.png` });

const view = await readCanvas(page);
console.log(JSON.stringify({
  landedMs: landed,
  ...view,
  verdict: {
    rootInViewport: view.rootInside,
    ratioOkIfFitAll: view.mode === 'fit-all' ? view.ratio === 1 : null,
  },
  console: log.console.slice(0, 3),
}, null, 1));

await browser.close();
