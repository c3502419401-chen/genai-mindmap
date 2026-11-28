import fs from 'node:fs';
import { chromium } from 'playwright-core';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const EXE = 'C:\\Users\\tao\\AppData\\Local\\ms-playwright\\chromium-1248\\chrome-win64\\chrome.exe';
const SHOTS = 'D:\\genai-mindmap\\docs\\acceptance-shots';
const OUT = 'D:\\_acc-playwright\\out';
const mark = (l) => console.log(`[${l}]`);
const ctx = await chromium.launchPersistentContext('D:\\_acc-playwright\\profile', {
  executablePath: EXE, headless: false, viewport: { width: 1440, height: 900 },
});
const page = ctx.pages()[0] ?? await ctx.newPage();
let shotN = 0;
const shot = async (n) => { shotN++; return page.screenshot({ path: `${SHOTS}\\D0-${String(shotN).padStart(2, '0')}-${n}.png` }); };
const bodyText = () => page.evaluate(() => document.body.innerText);
const lines = async (re) => (await bodyText()).split('\n').filter(l => re.test(l));
const btnTap = (src) => page.evaluate((s2) => {
  const re = new RegExp(s2);
  const el = [...document.querySelectorAll('button,[role=button]')].filter(e => {
    const t = ((e.innerText || '') + ' ' + (e.getAttribute('title') || '')).trim();
    return re.test(t) && e.getBoundingClientRect().width > 4;
  })[0];
  if (!el) return null;
  el.click(); return (el.innerText || '').trim().slice(0, 20);
}, src);
const nodes = () => page.evaluate(() => [...document.querySelectorAll('span.line-clamp-2')]
  .map(e => { const r = e.getBoundingClientRect(); return { t: (e.textContent || '').trim(), x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) }; })
  .filter(o => o.x > 0));

await page.goto('http://localhost:5173/', { waitUntil: 'networkidle', timeout: 60000 });
await sleep(1400);
await btnTap('历史导图与更多操作'); await sleep(1300);
await page.evaluate(() => {
  const it = [...document.querySelectorAll('[data-test=history-drawer] button')].filter(e => (e.getAttribute('title') || '').includes('树基线'));
  if (it[0]) it[0].click();
});
await sleep(2200);
await btnTap('^适应宽度$'); await sleep(1200);
const ns = await nodes();
mark('nodes=' + JSON.stringify(ns));

// 叶子甲（二级节点）拖到确定空白 (200,200)
const leaf = ns.find(n => n.t === '叶子甲');
if (leaf) {
  await page.mouse.move(leaf.x, leaf.y);
  await page.mouse.down();
  for (let i = 1; i <= 24; i++) { await page.mouse.move(leaf.x + ((200 - leaf.x) * i) / 24, leaf.y + ((200 - leaf.y) * i) / 24); await sleep(30); }
  await sleep(1500);
  await shot('leaf-to-empty-preview');
  const prevText = await bodyText();
  fs.writeFileSync(OUT + '\\n_preview.txt', prevText, 'utf8');
  mark('preview_lines=' + JSON.stringify(prevText.split('\n').filter(l => /断开|成为|插到|落点|不允许|一级/.test(l))));
  const ariaLive = await page.evaluate(() => [...document.querySelectorAll('[aria-live],[role=status],[role=tooltip],[data-test=notice]')].map(e => (e.innerText || '').trim().slice(0, 120)).filter(Boolean));
  mark('aria=' + JSON.stringify(ariaLive));
  await page.mouse.up();
  await sleep(1800);
  await shot('leaf-to-empty-done');
  fs.writeFileSync(OUT + '\\n_after.txt', await bodyText(), 'utf8');
  mark('after=' + JSON.stringify((await lines(/已|断开|撤销|落点/)).slice(-4)));
  const ns2 = await nodes();
  mark('nodes_after=' + JSON.stringify(ns2));
}

// 再试右下空白
const ns3 = await nodes();
const leaf2 = ns3.find(n => n.t === '叶子丙');
if (leaf2) {
  await page.mouse.move(leaf2.x, leaf2.y);
  await page.mouse.down();
  for (let i = 1; i <= 24; i++) { await page.mouse.move(leaf2.x + ((1320 - leaf2.x) * i) / 24, leaf2.y + ((700 - leaf2.y) * i) / 24); await sleep(30); }
  await sleep(1400);
  await shot('leaf2-to-empty-preview');
  mark('preview2=' + JSON.stringify((await lines(/断开|成为|插到|落点|不允许/)).slice(-3)));
  await page.mouse.up();
  await sleep(1500);
  mark('after2=' + JSON.stringify((await lines(/已|断开|撤销/)).slice(-3)));
  await shot('leaf2-to-empty-done');
}
await sleep(300);
await ctx.close();
