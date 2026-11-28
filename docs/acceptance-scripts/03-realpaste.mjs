import fs from 'node:fs';
import { open, sleep } from './lib.mjs';

const samples = JSON.parse(fs.readFileSync('D:\\genai-mindmap\\backend\\scripts\\acc_samples.json', 'utf8'));
const s = samples.samples.find(x => x.id === (process.env.SID || 'S01'));

const { browser, ctx, page, log, shot } = await open();
await page.goto('http://localhost:5173/', { waitUntil: 'networkidle', timeout: 60000 });
await sleep(1200);

await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: 'http://localhost:5173' });
await page.evaluate(t => navigator.clipboard.writeText(t), s.text);
const ta = page.locator('textarea').first();
await ta.click();
await page.keyboard.press('Control+V');
const t0 = Date.now();
for (let i = 0; i < 20; i++) {
  await sleep(500);
  const txt = await page.evaluate(() => document.body.innerText);
  if (!/还没有导图/.test(txt) || /正在解析|已经放进材料/.test(txt)) {
    console.log('reaction_after_ms', Date.now() - t0);
    break;
  }
}
await sleep(2000);
console.log('TA_VALUE', JSON.stringify(await ta.inputValue()).slice(0, 400));
console.log('BODY', (await page.evaluate(() => document.body.innerText)).slice(0, 2500));
await shot('05-real-paste');

// 智能搜索按钮状态
console.log('smart_buttons', JSON.stringify(await page.evaluate(() => [...document.querySelectorAll('button')]
  .map(b => ({ t: b.innerText.trim().slice(0, 30), dis: b.disabled, aria: b.getAttribute('aria-disabled') }))
  .filter(x => x.t))));
console.log('LOG', JSON.stringify(log).slice(0, 4000));
await sleep(500);
await browser.close();
