import fs from 'node:fs';
import { chromium } from 'playwright-core';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const EXE = 'C:\\Users\\tao\\AppData\\Local\\ms-playwright\\chromium-1248\\chrome-win64\\chrome.exe';
const SHOTS = 'D:\\genai-mindmap\\docs\\acceptance-shots';
const OUT = 'D:\\_acc-playwright\\out';
const mdPath = 'D:\\_acc-playwright\\产品需求评审会.md';
const T0 = Date.now();
const mark = (l) => console.log(`[${((Date.now() - T0) / 1000).toFixed(1)}s] ${l}`);

const ctx = await chromium.launchPersistentContext('D:\\_acc-playwright\\profile', {
  executablePath: EXE, headless: false, viewport: { width: 1440, height: 900 },
});
const page = ctx.pages()[0] ?? await ctx.newPage();
const cdp = await ctx.newCDPSession(page);
await cdp.send('Network.enable');
const reqs = [];
const bodies = new Map();
cdp.on('Network.requestWillBeSent', e => {
  if (!/\/api\//.test(e.request.url)) return;
  reqs.push({ id: e.requestId, t: Date.now() - T0, m: e.request.method, u: e.request.url.replace('http://localhost:5173', ''), post: (e.request.postData || '').slice(0, 120) });
});
cdp.on('Network.responseReceived', e => {
  if (!/\/api\//.test(e.response.url)) return;
  const r = reqs.find(x => x.id === e.requestId);
  if (r) { r.status = e.response.status; r.ms = e.response.responseTime ? Math.round(e.response.responseTime - 0) : null; }
});
cdp.on('Network.loadingFinished', async e => {
  const r = reqs.find(x => x.id === e.requestId);
  if (!r) return;
  try {
    const b = await cdp.send('Network.getResponseBody', { requestId: e.requestId });
    r.body = (b.body || '').slice(0, 400);
  } catch { }
});
const con = [];
page.on('console', m => { if (m.type() === 'error' || m.type() === 'warning') con.push(m.type() + ':' + m.text().slice(0, 200)); });
page.on('pageerror', e => con.push('PAGEERR:' + String(e).slice(0, 200)));

await page.goto('http://localhost:5173/', { waitUntil: 'networkidle', timeout: 60000 });
await sleep(1500);
mark('loaded');

page.on('filechooser', async fc => { await fc.setFiles(mdPath); });
await page.mouse.click(997, 795);
mark('clicked plus');
for (let i = 0; i < 25; i++) {
  await sleep(1000);
  if (i % 5 === 0) {
    const t = await page.evaluate(() => document.body.innerText);
    mark('t+' + i + ' :: ' + JSON.stringify(t.split('\n').filter(l => /解析|已就绪|合并后|来源列表/.test(l)).slice(0, 4)));
  }
}
await page.screenshot({ path: `${SHOTS}\\H0-stuck-parse.png` });
const final = await page.evaluate(() => document.body.innerText);
fs.writeFileSync(OUT + '\\r_stuck.txt', final, 'utf8');
mark('FINAL :: ' + JSON.stringify(final.split('\n').filter(l => /解析|就绪|合并后|来源列表|彻底删除/.test(l)).slice(0, 8)));
mark('API_TIMELINE');
for (const r of reqs) console.log(`  ${r.t}ms ${r.m} ${r.u} -> ${r.status ?? '?'} ${r.body ? '| ' + r.body.slice(0, 200) : ''}`);
mark('CONSOLE ' + JSON.stringify(con.slice(0, 12)));
fs.writeFileSync(OUT + '\\r_api.json', JSON.stringify(reqs, null, 1), 'utf8');
await sleep(300);
await ctx.close();
