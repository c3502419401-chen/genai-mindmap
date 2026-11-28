import fs from 'node:fs';
import { chromium } from 'playwright-core';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const EXE = 'C:\\Users\\tao\\AppData\\Local\\ms-playwright\\chromium-1248\\chrome-win64\\chrome.exe';
const SHOTS = 'D:\\genai-mindmap\\docs\\acceptance-shots';
const OUT = 'D:\\_acc-playwright\\out';
const T0 = Date.now();
const mark = (l) => console.log(`[${((Date.now() - T0) / 1000).toFixed(1)}s] ${l}`);
const dump = (n, t) => fs.writeFileSync(`${OUT}\\${n}.txt`, t, 'utf8');
const step = async (label, fn) => {
  try { const r = await fn(); mark(`OK ${label} :: ${typeof r === 'string' ? r : JSON.stringify(r)}`); return r; }
  catch (e) { mark(`FAIL ${label} :: ${String(e).split('\n')[0].slice(0, 220)}`); }
};
const ctx = await chromium.launchPersistentContext('D:\\_acc-playwright\\profile', {
  executablePath: EXE, headless: false, viewport: { width: 1440, height: 900 },
});
const page = ctx.pages()[0] ?? await ctx.newPage();
let shotN = 0;
const shot = async (n) => { shotN++; return page.screenshot({ path: `${SHOTS}\\90-${String(shotN).padStart(2, '0')}-${n}.png` }); };
const bodyText = () => page.evaluate(() => document.body.innerText);
const lines = async (re) => (await bodyText()).split('\n').filter(l => re.test(l));
const btnTap = (src) => page.evaluate((s2) => {
  const re = new RegExp(s2);
  const el = [...document.querySelectorAll('button,[role=button]')].filter(e => re.test(((e.innerText || '') + ' ' + (e.getAttribute('title') || '') + ' ' + (e.getAttribute('aria-label') || '')).trim()) && e.getBoundingClientRect().width > 4)[0];
  if (!el) return null;
  el.click(); return (el.innerText || '').trim().slice(0, 24);
}, src);
const canvasNodes = () => page.evaluate(() => [...document.querySelectorAll('span.line-clamp-2')]
  .map(e => { const r = e.getBoundingClientRect(); return { t: (e.textContent || '').trim(), x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), w: Math.round(r.width), inView: r.x > 20 && r.x < 1100 && r.y > 110 && r.y < 620 }; })
  .filter(o => o.w > 0));

await page.goto('http://localhost:5173/', { waitUntil: 'networkidle', timeout: 60000 });
await sleep(1500);

await step('open_a_tree_map', async () => {
  await btnTap('历史导图与更多操作'); await sleep(1300);
  const opened = await page.evaluate(() => {
    const items = [...document.querySelectorAll('[data-test=history-drawer] button')].filter(e => (e.getAttribute('title') || '').startsWith('打开「'));
    if (!items.length) return 'none';
    const pick = items.find(e => /树基线/.test(e.getAttribute('title') || '')) || items[0];
    pick.click(); return pick.getAttribute('title');
  });
  await sleep(2500);
  await shot('opened');
  return { opened, stats: (await lines(/显示 \d+ \/ \d+|个节点|已打开导图/)).slice(0, 4) };
});

await step('fit_width', async () => {
  await btnTap('^适应宽度$');
  await sleep(1500);
  await shot('fit-width');
  const ns = await canvasNodes();
  return { nodes: ns.length, inView: ns.filter(n => n.inView).length };
});

let dragInfo = null;
await step('drag_child_to_child', async () => {
  const all = await canvasNodes();
  const inView = all.filter(n => n.inView && n.t.length > 2);
  if (inView.length < 4) throw new Error('可见节点太少 inView=' + inView.length + ' total=' + all.length);
  // 源：不在根位（x 最大）的节点；目标：另一个不同层的节点
  const from = inView[Math.floor(inView.length / 2)];
  const to = inView.find(n => n.t !== from.t && Math.abs(n.y - from.y) > 25) || inView[0];
  dragInfo = { from, to };
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  for (let i = 1; i <= 18; i++) { await page.mouse.move(from.x + ((to.x + 55 - from.x) * i) / 18, from.y + ((to.y - from.y) * i) / 18); await sleep(35); }
  await sleep(1200);
  const prev = await lines(/成为「|插到「|断开「|算不出|不允许|层了|落点|红框/);
  await shot('drag-preview');
  await page.mouse.up();
  await sleep(1800);
  await shot('drag-done');
  const t = await bodyText();
  dump('j_drag', t);
  const after = await canvasNodes();
  const moved = after.find(n => n.t === from.t);
  return { from: from.t, to: to.t, preview: prev.slice(-3), after: (await lines(/已改|已移|撤销|结构|父|兄弟|节点/)).slice(-3), movedTo: moved ? { x: moved.x, y: moved.y } : null, was: { x: from.x, y: from.y } };
});

await step('undo1', async () => {
  await page.keyboard.press('Control+Z'); await sleep(1300); await shot('undo1');
  const after = await canvasNodes();
  const back = after.find(n => n.t === dragInfo?.from?.t);
  return { status: (await lines(/已撤销|还没保存|已保存|修改/)).slice(-2), pos: back ? { x: back.x, y: back.y } : null };
});

await step('drag_to_blank', async () => {
  const inView = (await canvasNodes()).filter(n => n.inView && n.t.length > 2);
  if (!inView.length) throw new Error('无可见节点');
  const from = inView[Math.min(2, inView.length - 1)];
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  for (let i = 1; i <= 12; i++) { await page.mouse.move(from.x - i * 25, from.y + i * 3); await sleep(30); }
  await sleep(1200);
  const prev = await lines(/断开「|成为一级分支|落点/);
  await shot('drag-blank-preview');
  await page.mouse.up();
  await sleep(1500);
  const t = await bodyText();
  dump('j_drag_blank', t);
  return { from: from.t, preview: prev.slice(-2), after: (await lines(/已|断开|撤销/)).slice(-3) };
});

await step('check_advice', async () => {
  await page.keyboard.press('Control+Z'); await sleep(800);
  // 先选中一个节点，让检查按钮可用
  const n = (await canvasNodes()).filter(x => x.inView)[1] || (await canvasNodes())[0];
  await page.mouse.click(n.x, n.y);
  await sleep(900);
  const opened = await btnTap('检查与建议');
  await sleep(1500);
  await shot('check-open');
  const s1 = (await lines(/还没检查过|没有发现问题|结构问题|只读|引用能否|检查与建议/)).slice(0, 5);
  const run = await btnTap('重新检查|^检查$|开始检查');
  await sleep(5000);
  await shot('check-result');
  const t = await bodyText();
  dump('j_check', t);
  return { opened, s1, run, result: (await lines(/检查|建议|问题|引用能否|修复|结构|没有发现/)).slice(-14) };
});

await step('ledger', async () => {
  const o = await page.evaluate(() => {
    const b = [...document.querySelectorAll('[data-test=edit-ledger-toggle]')][0];
    if (!b) return 'no-ledger-toggle';
    b.click(); return 'opened';
  });
  await sleep(2500);
  await shot('ledger');
  const t = await bodyText();
  dump('j_ledger', t);
  return { o, rows: (await lines(/改动记录|条事件|模型生成|你改|规则整理|未保存的改动/)).slice(0, 10) };
});

await step('layout_small', async () => {
  await page.setViewportSize({ width: 1280, height: 720 }); await sleep(1300); await shot('1280');
  const a = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
  await page.setViewportSize({ width: 1024, height: 768 }); await sleep(1300); await shot('1024');
  const b = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
  await page.setViewportSize({ width: 1440, height: 900 }); await sleep(600);
  return { a, b };
});

await sleep(400);
await ctx.close();
