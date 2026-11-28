/**
 * ① 机制判据：把 `/api/ingestion/files` 的响应按三种方式改写，看前端状态机怎么走。
 *
 *   A: 30 秒后才放行（慢，但会到）
 *   B: 20 秒后中断（连接失败）
 *   C: 永不响应（后端挂住 / 冷启动太久 / 连接被半关）
 *
 * 2026-11-28 的读数（修复前）：A 38 秒自己变就绪；B 落 failed + 「重试」；C 46 秒仍在
 * 「正在解析…46 秒」、按钮灰着、**没有任何出口**。C 就是验收记录 §四.2 那张截图的形状，
 * 也是"上传卡在正在解析"的机制——请求本身慢，而前端那条 fetch 没有超时。
 *
 * 修复后（`uploadDocument` 带超时 + 失败落状态）：C 会在超时上限到点时落 failed 并给
 * 「重试解析」。本脚本只打印读数，不断言——它的用途是"看一眼现场"，判据在
 * `frontend/tests/sources.spec.ts::上传解析的出口` 里。
 *
 * 用法: node 20-slow-upload.mjs [A|B|C|all]
 *
 * 跑法同本目录其余脚本：`playwright-core` **不在** `frontend/package.json` 里，把整个
 * 目录复制到装了它的隔离目录再跑（`lib.mjs` 的 `EXE` 是本机 Chromium 的绝对路径）。
 */
import fs from 'node:fs';
import { open, sleep, SHOTS } from './lib.mjs';

const which = process.argv[2] ?? 'all';
const samples = JSON.parse(fs.readFileSync('D:\\genai-mindmap\\backend\\scripts\\acc_samples.json', 'utf8'));
const s = samples.samples.find(x => x.id === (process.env.SID || 'S08'));
const file = `D:\\_acc-placeholder\\${s.id}-${s.title}.md`;
if (!fs.existsSync(file)) {
  fs.mkdirSync('D:\\_acc-placeholder', { recursive: true });
  fs.writeFileSync(file, s.text, 'utf8');
}

const SCENARIOS = {
  A: { wait: 30000, after: route => route.continue(), observe: 44000 },
  B: { wait: 20000, after: route => route.abort('failed'), observe: 30000 },
  C: { wait: 200000, after: null, observe: 46000 },
};

const { browser, ctx, page, log } = await open();

async function run(name) {
  const cfg = SCENARIOS[name];
  const seen = [];
  let t0 = Date.now();
  await page.route('**/api/ingestion/files', async (route) => {
    seen.push(`routed@${((Date.now() - t0) / 1000).toFixed(1)}s`);
    await sleep(cfg.wait);
    if (cfg.after) await cfg.after(route);
    seen.push(`handled@${((Date.now() - t0) / 1000).toFixed(1)}s`);
  });
  await page.goto('http://localhost:5173/', { waitUntil: 'networkidle', timeout: 60000 });
  await sleep(1200);
  t0 = Date.now();
  await page.setInputFiles('input[type=file]', file);

  const samples = [];
  for (let i = 0; i < cfg.observe / 2000; i++) {
    await sleep(2000);
    const state = await page.evaluate(() => {
      const lines = document.body.innerText.split('\n').map(x => x.trim()).filter(Boolean);
      const pick = lines.filter(l => /正在解析|等待解析|已就绪|合并后|解析失败|重试|取消|超时|文档 ·/.test(l));
      const btn = [...document.querySelectorAll('button')]
        .find(b => (b.getAttribute('aria-label') || '') === '生成导图');
      return { pick, disabled: btn?.disabled ?? null };
    });
    samples.push(`${((Date.now() - t0) / 1000).toFixed(0)}s ${state.pick.join(' | ') || '(无状态行)'} ${state.disabled ? '[灰]' : '[可点]'}`);
  }
  await page.screenshot({ path: `${SHOTS}\\R1-slow-${name}.png` });
  console.log(`--- ${name} ---`);
  console.log('首次读数：', samples.slice(0, 3).join('\n           '));
  console.log('末尾读数：', samples.slice(-3).join('\n           '));
  console.log('网络：', seen.join(' '));
  console.log('控制台：', JSON.stringify(log.console.slice(0, 3)));
  await page.unrouteAll({ behavior: 'ignoreErrors' });
}

for (const name of (which === 'all' ? ['A', 'B', 'C'] : [which])) await run(name);
await browser.close();
