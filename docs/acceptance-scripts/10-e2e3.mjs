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
const samples = JSON.parse(fs.readFileSync('D:\\genai-mindmap\\backend\\scripts\\acc_samples.json', 'utf8'));
const s = samples.samples.find(x => x.id === 'S01');

const ctx = await chromium.launchPersistentContext('D:\\_acc-playwright\\profile', {
  executablePath: EXE, headless: false, viewport: { width: 1440, height: 900 },
});
const page = ctx.pages()[0] ?? await ctx.newPage();
const log = { console: [], errors: [] };
page.on('console', m => { if (m.type() === 'error') log.console.push(m.text().slice(0, 200)); });
page.on('pageerror', e => log.errors.push(String(e).slice(0, 300)));
const cdp = await ctx.newCDPSession(page);
const netBad = [];
await cdp.send('Network.enable');
cdp.on('Network.responseReceived', e => { if (e.response.status >= 400) netBad.push({ s: e.response.status, u: e.response.url.slice(0, 150) }); });
const downloads = [];
page.on('download', async d => { const p = `${OUT}\\dl5-${Date.now()}-${d.suggestedFilename()}`; await d.saveAs(p).catch(() => {}); downloads.push({ name: d.suggestedFilename(), path: p }); });
let shotN = 0;
const shot = async (n) => { shotN++; return page.screenshot({ path: `${SHOTS}\\80-${String(shotN).padStart(2, '0')}-${n}.png` }); };
const bodyText = () => page.evaluate(() => document.body.innerText);
const lines = async (re) => (await bodyText()).split('\n').filter(l => re.test(l));
// 任意可见元素按文本点击（不限标签）
const tap = (src) => page.evaluate((s2) => {
  const re = new RegExp(s2);
  const cands = [...document.querySelectorAll('button,[role=button],label,select,a,div,span,p,li')]
    .filter(e => re.test((e.innerText || e.getAttribute('title') || e.getAttribute('aria-label') || '').trim()))
    .filter(e => { const r = e.getBoundingClientRect(); return r.width > 4 && r.height > 4 && r.top < 899 && r.bottom > 0; });
  if (!cands.length) return null;
  const el = cands[0];
  el.click();
  return { tag: el.tagName, cls: String(el.className).slice(0, 40), txt: (el.innerText || '').trim().slice(0, 30) };
}, src);
// 真节点：画布内的 span.line-clamp-2
const canvasNodes = () => page.evaluate(() => [...document.querySelectorAll('span.line-clamp-2')]
  .map(e => { const r = e.getBoundingClientRect(); const host = e.closest('[class*=nodrag]') || e.parentElement; return { t: (e.textContent || '').trim(), x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), w: Math.round(r.width), h: Math.round(r.height), inView: r.x > 20 && r.x < 1100 && r.y > 110 && r.y < 620 }; })
  .filter(o => o.w > 0));

await page.goto('http://localhost:5173/', { waitUntil: 'networkidle', timeout: 60000 });
await sleep(1200);

await step('generate', async () => {
  await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: 'http://localhost:5173' });
  await page.evaluate(t => navigator.clipboard.writeText(t), s.text);
  const ta = page.locator('textarea').first();
  await ta.click();
  await page.keyboard.press('Control+V');
  await sleep(2200);
  await ta.fill(s.title);
  await sleep(400);
  const t0 = Date.now();
  await page.getByRole('button', { name: /生成导图/ }).click();
  let landed = null;
  for (let i = 0; i < 200; i++) { await sleep(1000); if (/已落到画布/.test(await bodyText())) { landed = Date.now() - t0; break; } }
  await shot('landed');
  return { landed_ms: landed, status: (await lines(/已落到画布|条诊断/)).slice(0, 2) };
});

await step('node_probe', async () => {
  const ns = await canvasNodes();
  dump('i_nodes', JSON.stringify(ns, null, 1));
  return { total: ns.length, inView: ns.filter(n => n.inView).length, sample: ns.slice(0, 6) };
});

// 改文字：双击可见节点
let editTarget = null;
await step('edit_text', async () => {
  const ns = (await canvasNodes()).filter(n => n.inView && n.t.length > 2);
  if (!ns.length) throw new Error('无可双击节点');
  editTarget = ns[Math.floor(ns.length / 2)];
  mark('target=' + JSON.stringify(editTarget));
  await page.mouse.dblclick(editTarget.x, editTarget.y);
  await sleep(1000);
  await shot('editing');
  const act = await page.evaluate(() => ({ tag: document.activeElement?.tagName, val: (document.activeElement?.value ?? '').slice(0, 40), ce: document.activeElement?.isContentEditable }));
  mark('active=' + JSON.stringify(act));
  await page.keyboard.press('Control+A');
  await page.keyboard.type('改过的标题X');
  await page.keyboard.press('Enter');
  await sleep(1200);
  await shot('edited');
  const t = await bodyText();
  dump('i_edited', t);
  return { act, status: (await lines(/已修改|记得保存|改过的标题X/)).slice(0, 4), present: /改过的标题X/.test(t) };
});

// 保存：用 ☰ → 保存按钮
await step('save', async () => {
  const m = await tap('历史导图与更多操作');
  await sleep(1000);
  await page.evaluate(() => { const sc = document.querySelector('[data-test=history-drawer] .overflow-y-auto'); if (sc) sc.scrollTop = sc.scrollHeight; });
  await sleep(500);
  const b = await page.evaluate(() => {
    const btns = [...document.querySelectorAll('[data-test=history-drawer] button')].filter(e => (e.innerText || '').trim() === '保存');
    if (!btns.length) return 'no-save-btn';
    if (btns[0].disabled) return 'save-disabled';
    btns[0].click(); return 'clicked';
  });
  await sleep(3000);
  await shot('saved');
  const t = await bodyText();
  dump('i_saved', t);
  return { m, b, status: (await lines(/已保存|版本|还没保存|创建/)).slice(-4) };
});

// 刷新重开验证改动保留
await step('reopen_verify', async () => {
  await page.reload({ waitUntil: 'networkidle' }); await sleep(1600);
  await tap('历史导图与更多操作'); await sleep(1200);
  const opened = await page.evaluate(() => {
    const items = [...document.querySelectorAll('[data-test=history-drawer] button')].filter(e => /^打开「数据结构与算法/.test(e.getAttribute('title') || ''));
    if (!items.length) return 'none';
    items[0].click(); return items[0].getAttribute('title');
  });
  await sleep(2500);
  await shot('reopened');
  const t = await bodyText();
  dump('i_reopened', t);
  const ns = await canvasNodes();
  return { opened, editedKept: /改过的标题X/.test(t) || ns.some(n => n.t === '改过的标题X'), stats: (await lines(/显示 \d+ \/ \d+|已打开导图|版本/)).slice(0, 4) };
});

// 拖动换父
await step('drag', async () => {
  const ns = (await canvasNodes()).filter(n => n.inView && n.t.length > 1);
  if (ns.length < 3) throw new Error('可见节点不足');
  const from = ns[0];
  const to = ns.find(n => n.t !== from.t && Math.abs(n.y - from.y) > 30) || ns[1];
  mark('from=' + JSON.stringify(from) + ' to=' + JSON.stringify(to));
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  for (let i = 1; i <= 18; i++) { await page.mouse.move(from.x + ((to.x + 55 - from.x) * i) / 18, from.y + ((to.y - from.y) * i) / 18); await sleep(35); }
  await sleep(1100);
  const prev = await lines(/成为「|插到「|断开「|算不出|不允许|层了|落点/);
  await shot('drag-preview');
  await page.mouse.up();
  await sleep(1600);
  await shot('drag-done');
  const t = await bodyText();
  dump('i_drag', t);
  return { from: from.t, to: to.t, preview: prev.slice(-3), after: (await lines(/已改|已移|撤销|父|兄弟|结构/)).slice(-4) };
});

// 撤销
await step('undo', async () => {
  await page.keyboard.press('Control+Z');
  await sleep(1400);
  await shot('undo');
  return (await lines(/已撤销|还没保存|已保存/)).slice(-3);
});

// 检查与建议
await step('structure_check', async () => {
  const first = await tap('检查与建议');
  await sleep(1500);
  await shot('check-open');
  const s1 = (await lines(/还没检查过|没有发现问题|结构问题|只读|引用能否/)).slice(0, 4);
  const run = await tap('重新检查|^检查$|开始检查');
  await sleep(4500);
  await shot('check-result');
  const t = await bodyText();
  dump('i_check', t);
  return { first, s1, run, result: (await lines(/检查|建议|问题|引用|修复|结构/)).slice(-12) };
});

// 导出（先确保有图、抽屉开着）
await step('export', async () => {
  await page.keyboard.press('Escape'); await sleep(500);
  await tap('历史导图与更多操作'); await sleep(1000);
  await page.evaluate(() => { const sc = document.querySelector('[data-test=history-drawer] .overflow-y-auto'); if (sc) sc.scrollTop = sc.scrollHeight; });
  await sleep(600);
  const info = await page.evaluate(() => {
    const sel = document.querySelector('[data-test=history-drawer] select');
    if (!sel) return { ok: false, why: 'no-select' };
    return { ok: true, disabled: sel.disabled, value: sel.value, opts: [...sel.options].map(o => o.text.trim()) };
  });
  mark('select=' + JSON.stringify(info));
  const r1 = await page.evaluate(() => {
    const sel = document.querySelector('[data-test=history-drawer] select');
    if (!sel || sel.disabled) return 'blocked';
    sel.value = 'markdown'; sel.dispatchEvent(new Event('change', { bubbles: true })); return 'md';
  });
  await sleep(3000);
  const r2 = await page.evaluate(() => {
    const sel = document.querySelector('[data-test=history-drawer] select');
    if (!sel || sel.disabled) return 'blocked';
    sel.value = 'json'; sel.dispatchEvent(new Event('change', { bubbles: true })); return 'json';
  });
  await sleep(3000);
  await shot('exported');
  return { info, r1, r2, downloads: downloads.map(d => d.name) };
});

// 1280x720 / 1024x768 布局
await step('layout', async () => {
  await page.keyboard.press('Escape'); await sleep(500);
  await page.setViewportSize({ width: 1280, height: 720 }); await sleep(1400); await shot('1280x720');
  const a = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth, sh: document.documentElement.scrollHeight, ch: document.documentElement.clientHeight }));
  await page.setViewportSize({ width: 1024, height: 768 }); await sleep(1400); await shot('1024x768');
  const b = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth, sh: document.documentElement.scrollHeight, ch: document.documentElement.clientHeight }));
  await page.setViewportSize({ width: 1440, height: 900 }); await sleep(800);
  return { a, b };
});

// 删除（两段式）
await step('delete', async () => {
  await tap('历史导图与更多操作'); await sleep(1200);
  const d1 = await page.evaluate(() => {
    const li = [...document.querySelectorAll('[data-test=history-drawer] li')].find(e => /数据结构与算法课程知识结构/.test(e.innerText || ''));
    if (!li) return 'no-row';
    const b = li.querySelector('[data-test=drawer-map-delete]');
    if (!b) return 'no-del';
    b.click(); return 'step1';
  });
  await sleep(1000);
  await shot('delete-confirm');
  const hint = (await lines(/确认删除|彻底删除这张导图|不可恢复/)).slice(0, 3);
  const d2 = await page.evaluate(() => {
    const b = document.querySelector('[data-test=drawer-map-delete-confirm]');
    if (!b) return 'no-confirm';
    b.click(); return 'step2';
  });
  await sleep(2800);
  await shot('deleted');
  const t = await bodyText();
  dump('i_deleted', t);
  return { d1, hint, d2, after: (await lines(/已删除|不可恢复|已清空|删除/)).slice(0, 4), stillListed: /数据结构与算法课程知识结构/.test(t) };
});

mark('NET_GE400 ' + JSON.stringify(netBad.slice(0, 10)));
mark('CONSOLE ' + JSON.stringify(log.console.slice(0, 8)));
mark('PAGEERR ' + JSON.stringify(log.errors.slice(0, 4)));
mark('DOWNLOADS ' + JSON.stringify(downloads));
await sleep(500);
await ctx.close();
