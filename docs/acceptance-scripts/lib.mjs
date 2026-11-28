import { chromium } from 'playwright-core';
import fs from 'node:fs';
import path from 'node:path';

export const EXE = 'C:\\Users\\tao\\AppData\\Local\\ms-playwright\\chromium-1248\\chrome-win64\\chrome.exe';
export const SHOTS = 'D:\\genai-mindmap\\docs\\acceptance-shots';
fs.mkdirSync(SHOTS, { recursive: true });

export async function open(url, { width = 1440, height = 900 } = {}) {
  const browser = await chromium.launch({ executablePath: EXE, headless: false, args: ['--start-maximized'] });
  const ctx = await browser.newContext({ viewport: { width, height } });
  const page = await ctx.newPage();
  const log = { console: [], errors: [], failed: [], http: [] };
  page.on('console', m => {
    const t = m.type();
    if (t === 'error' || t === 'warning') log.console.push({ type: t, text: m.text().slice(0, 500), t: Date.now() });
  });
  page.on('pageerror', e => log.errors.push({ text: String(e).slice(0, 800), t: Date.now() }));
  page.on('requestfailed', r => log.failed.push({ url: r.url().slice(0, 300), err: r.failure()?.errorText }));
  page.on('response', r => {
    if (r.status() >= 400) log.http.push({ status: r.status(), url: r.url().slice(0, 300), method: r.request().method() });
  });
  let n = 0;
  const shot = async (name) => {
    n += 1;
    const file = path.join(SHOTS, `${String(n).padStart(2, '0')}-${name}.png`);
    await page.screenshot({ path: file, fullPage: false });
    return file;
  };
  return { browser, ctx, page, log, shot };
}

export const sleep = ms => new Promise(r => setTimeout(r, ms));
