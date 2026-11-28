/**
 * journey.spec —— 这个仓库里第一道问「能不能用」的闸。
 *
 * ## 它和其余十六道闸的问题不一样
 *
 * 那十六道读 AST、读快照、读文本，全都对着代码写；这一道对着**用户手里那张图**跑：
 * 真浏览器、真后端、真模型。上一轮的判词写在
 * `docs/ACCEPTANCE-WALKTHROUGH-2026-11-28.md` §6.5：
 *
 *   "十六道闸里没有一道问'能不能用'……不是能力问题，是**视野问题**：
 *    闸门长在代码上，而用户在代码之外。"
 *
 * 于是这道闸只回答一个问题：**一个新用户照着手册走，能不能拿到一张能用的图。**
 * 它故意答不了第二个问题（"改了这个函数有没有回归"），那是另外十六道的事。
 *
 * ## 五条断言（判据逐条见 `docs/JOURNEY-SPEC.md`）
 *
 *   ① 落地后视口 **按档位分**：全图档 → 可见 = 已渲染；适应宽度档 → 根节点在视口内
 *      且可见比例 ≥ 该图的物理上限。**不写"≥0.8"**——那个数在适应宽度档达不到，
 *      而达不到的原因是产品故意的可读性取舍（fit-all 会缩到 5 px，正是上一轮修掉的
 *      那个"第一眼以为生成失败"）。写成 0.8 的后果是第一次跑就红，然后被人加一个
 *      豁免——那就成了"又一道没人敢改的闸"。
 *   ② 引文逐字：引用抽屉里被 `<mark>` 高亮的那段，必须**逐字**被 `ref-quote` 的
 *      引文包含。上一轮实测命中在第 30–57 字，这条已经过了——本闸把它钉住。
 *   ③ 刷新重开一致：刷新后节点数 == 最后保存的节点数，**且最后一处改动仍在**。
 *      `--repeat=2` 时同一份材料连跑两次，两次都不许要人重试（"两次卡一次过"
 *      只跑一次永远看不出来）。
 *   ④ 最长静默上限：生成期界面连续 40 秒没有新字 = 失败。判据、阈值与依据见
 *      `SILENCE_LIMIT_MS` 与 `docs/JOURNEY-SPEC.md` §④。
 *   ⑤ 手册按钮名可点到：`docs/USER-GUIDE.md` 里以 `「…」` 写出来的每个动作名，
 *      都能在对应时点的页面上按名点到（存在 + 可见）；反过来清单里每个名字也必须在
 *      手册里逐字存在（防止清单被悄悄收窄）。
 *
 * ## 跑位：手动触发，不进 push 门
 *
 * 不进 CI 的理由不是"跑不起"，是**跑起来也不可信**：同一份材料五次生成给出
 * 50/52/59/71/82 个节点，任何数量断言都得带容差，而带容差的闸会在真回归上放行。
 * 取舍是：真后端真模型、手动触发、一次只回答一个问题；代价是它拦不住回归、
 * 只拦得住发布。什么时候该跑见 `docs/JOURNEY-SPEC.md` §「什么时候跑」。
 *
 * ## 用法
 *
 *   cd frontend && npm run test:journey          # 默认跑两轮生成
 *   node docs/acceptance-scripts/journey.spec.mjs --repeat=1        # 快跑
 *   node docs/acceptance-scripts/journey.spec.mjs --sample=S01 --keep
 *
 * 退出码：0 全绿 / 1 断言红 / 2 环境不满足（服务没起、依赖没装、浏览器找不到）。
 * 三种分开是因为后两种的"下一步该做什么"完全不同。
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import {
  BODY_TEXT,
  EnvError,
  LOCATE,
  MANUAL_POINTS,
  MANUAL_SKIPPED,
  READ_CANVAS,
  REPO,
  SilenceWatch,
  WEB_URL,
  findChrome,
  loadPlaywright,
  normalizeForSilence,
  requireServices,
  sleep,
} from './journey-lib.mjs';

// ---------------------------------------------------------------- 判据常数

/**
 * ④ 的最长静默上限：**40 秒**。
 *
 * 依据三条，缺一条这个数就站不住：
 *
 * 1. **后端自己承认的静默是 25 秒**。`backend/app/agent/service.py:82` 写着
 *    "线上实测（3 路并发，GLM-5.3-Flash）最长静默 25 秒"，心跳间隔取 10 秒
 *    （`HEARTBEAT_INTERVAL_SECONDS`）。心跳只驱动状态行那个"已等 N 秒"的秒表，
 *    **不携带来任何业务信息**——所以它不打断静默（见 `normalizeForSilence`）。
 *    上限必须 ≥ 25 秒，否则在已实测的线上形态下就会误红。
 * 2. **整期生成实测 39.5–50.1 秒**（`ACCEPTANCE-WALKTHROUGH-2026-11-28.md` §一）。
 *    也就是说 40 秒静默比"整轮生成的最短耗时"还长：真出现这种形态，不可能是
 *    "生成得慢"，只能是"这一轮什么也没说"。这正是要抓的那个形状。
 * 3. **不取 30 秒**：25 秒是 3 路并发下的**最大值**，手上没有分布；30 = 最大值 +
 *    5 秒边距，下一次模型慢一点就红。**误红的闸会被加豁免**，而这道闸的全部价值
 *    在于它不腐烂。
 * 3b. **不取 45 秒**：上传卡住那次实测是 46 秒仍无出口（`20-slow-upload.mjs` 场景 C），
 *    取 45 就等于"症状出现 45 秒后才判死"——那时用户早关页面了。
 *
 * 兜底判据：若这个数**误红**，正确的动作是改产品（后端把心跳之外的真进度帧发出来，
 * 或前端把"已等 N 秒"换成带信息量的阶段文案），**不是**把这个数抬高。抬高它等于
 * 把"界面 40 秒不说人话"这件事合法化。
 */
const SILENCE_LIMIT_MS = 40_000;

/** 采样粒度：阈值是秒级的，采样就该是秒级的。2 秒采一次会把 40 秒量成 38–42 秒。 */
const SAMPLE_MS = 1_000;

/** 生成的最长等待（静默断言之外的第二道保险：模型可能一直有字但永远不落地）。 */
const LAND_TIMEOUT_MS = 180_000;

/**
 * ① 的可见比例容差。
 *
 * `cap` 用"节点中心点的纵向跨度 ÷ 视口高度"算，而 `ratio` 是"中心点在视口里的节点数
 * ÷ 已渲染节点数"——同一个口径下的两种量化。节点分布不均匀时两者会有几个点的偏差，
 * 0.05 是"最多 5% 的节点因为分布不均被算到界外"，不是给"没适配"留的余地：
 * 没适配的那种形态（上一轮实测 0.563 对 0.656）差得远不止 5%。
 */
const CAP_TOLERANCE = 0.05;

/** 引文逐字的下限：8 个汉字。低于它的"包含"是平凡真（"的"永远被包含）。 */
const MIN_QUOTE_CHARS = 8;

// ---------------------------------------------------------------- 参数

function parseArgs(argv) {
  const opts = {
    repeat: 2, sample: process.env.JOURNEY_SAMPLE ?? 'S08', keep: false, verbose: false,
  };
  for (const arg of argv) {
    if (arg === '--keep') opts.keep = true;
    else if (arg === '--verbose') opts.verbose = true;
    else if (arg.startsWith('--repeat=')) opts.repeat = Number(arg.slice(9));
    else if (arg.startsWith('--sample=')) opts.sample = arg.slice(9);
    else if (arg.startsWith('--shots=')) opts.shots = arg.slice(8);
    else if (arg.startsWith('--silence-ms=')) opts.silenceMs = Number(arg.slice(13));
    else if (arg === '--help' || arg === '-h') {
      console.log('用法: node docs/acceptance-scripts/journey.spec.mjs '
        + '[--repeat=2] [--sample=S08] [--shots=<目录>] [--silence-ms=40000] [--keep]');
      process.exit(0);
    }
  }
  if (!Number.isFinite(opts.repeat) || opts.repeat < 1) opts.repeat = 1;
  return opts;
}

// ---------------------------------------------------------------- 结果记录

const results = [];

function record(id, ok, detail) {
  results.push({ id, ok, detail });
  const mark = ok ? 'PASS' : 'FAIL';
  console.log(`  [${mark}] ${id} — ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`);
  return ok;
}

const failures = () => results.filter(r => !r.ok);

// ---------------------------------------------------------------- 材料

/**
 * 把验收材料写成一份真文件。
 *
 * 取自 `backend/scripts/acc_samples.json`（**已入库**，20 份 490–751 字的短文）。
 * 用它而不是现场编一份：这条路每次跑的都该是同一份材料，否则"同一份材料连跑两次"
 * 这句话本身就不成立。文件落在系统临时目录，不进仓库。
 */
function writeSample(id) {
  const file = path.join(REPO, 'backend', 'scripts', 'acc_samples.json');
  const data = JSON.parse(fs.readFileSync(file, 'utf8'));
  const sample = data.samples.find(s => s.id === id);
  if (!sample) {
    throw new EnvError(`acc_samples.json 里没有 ${id}；可选：${data.samples.map(s => s.id).join(' ')}`);
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'journey-'));
  const out = path.join(dir, `${sample.id}-${sample.title}.md`);
  fs.writeFileSync(out, sample.text, 'utf8');
  return { file: out, sample };
}

// ---------------------------------------------------------------- ⑤ 的一半

/**
 * 手册 → 清单：每个名字都必须在 `docs/USER-GUIDE.md` 里以 `「name…」` 的形式出现。
 *
 * 前缀匹配到 `」` 之前，所以手册写「添加材料 +」也能对上清单里的 `添加材料`。
 * 这一半是纯文本核对（不需要浏览器），所以它先跑：清单本身漂了的话，后面的页面
 * 读数就都没意义。
 */
function checkGuidePoints() {
  const guide = fs.readFileSync(path.join(REPO, 'docs', 'USER-GUIDE.md'), 'utf8');
  const missing = MANUAL_POINTS
    .filter(p => !guide.includes(`「${p.name}`))
    .map(p => p.name);
  return record('⑤a 手册↔清单', missing.length === 0,
    missing.length ? `手册里没有：${missing.join('、')}（清单漂了，或手册改了名）`
      : `${MANUAL_POINTS.length} 条全部在 USER-GUIDE.md 里逐字存在（另有 ${MANUAL_SKIPPED.length} 条"故意不收"已写明理由）`);
}

// ---------------------------------------------------------------- ⑤ 的一半

/** 在一个时点上核一组手册动作名。 */
async function checkGroup(page, group) {
  const points = MANUAL_POINTS.filter(p => p.group === group);
  if (!points.length) return true;
  const found = [];
  const rows = [];
  for (const point of points) {
    const hit = await page.evaluate(LOCATE, point);
    found.push({ ...point, ...hit });
    rows.push(`${point.name}${hit.found ? '' : '（找不到）'}`
      + `${hit.found && !hit.visible ? '（被收起）' : ''}`
      + `${hit.found && hit.visible && !hit.inViewport ? '（需滚动）' : ''}`);
  }
  const bad = found.filter(r => !r.found || !r.visible);
  return record(`⑤ 手册名@${group}`, bad.length === 0,
    bad.length
      ? `这些按名点不到：${bad.map(r => r.name).join('、')}。读数：${JSON.stringify(found.map(r => ({ n: r.name, via: r.via, vis: r.visible, inVp: r.inViewport })))}`
      : rows.join(' / '));
}

// ---------------------------------------------------------------- ④

/** 在等待某个条件期间持续采样界面文本，并把静默记账。 */
async function waitWhileWatching(page, watch, predicate, timeoutMs, note) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const text = await page.evaluate(BODY_TEXT);
    watch.feed(text);
    if (await predicate(text)) return { ok: true, text, elapsed: Date.now() - started };
    if (watch.breached) {
      return { ok: false, text, elapsed: Date.now() - started, reason: `${note}期间静默 ${(watch.stuckAt.silent / 1000).toFixed(0)} 秒` };
    }
    await sleep(SAMPLE_MS);
  }
  return { ok: false, text: await page.evaluate(BODY_TEXT), elapsed: Date.now() - started, reason: `${note}超时 ${timeoutMs / 1000}s` };
}

// ---------------------------------------------------------------- ③ 的读数

/** 递归数一棵树里的节点（后端口径，用来和界面上那个数字比）。 */
const countNodes = root => (root ? 1 + (root.children ?? []).reduce((sum, c) => sum + countNodes(c), 0) : 0);

/**
 * 打开 ☰ 抽屉。
 *
 * **不能用"DOM 里有没有 `history-drawer`"当判据**：这个仓库的浮层几乎都用 `v-show`
 * 常驻挂载（"收起只是看不见，DOM 与键盘顺序都还在"，见 `frontend/tests/helpers/user-path.ts`），
 * 于是 `querySelector` 永远命中、`click()` 永远超时。判据必须是**此刻可见**。
 */
async function openDrawer(page) {
  const save = page.locator('[data-test="history-drawer"] button', { hasText: /^\s*保存\s*$/ }).first();
  if (await save.count() && await save.isVisible()) return true;
  const toggle = page.locator('[data-test="history-toggle"]').first();
  if (!(await toggle.count())) return false;
  await toggle.click();
  await sleep(500);
  return (await save.count()) > 0 && await save.isVisible();
}

/** 打开 ☰ 抽屉，读界面上那句「N 个节点」（`store.nodeCount`，不受折叠影响）。 */
async function drawerNodeCount(page) {
  const opened = await openDrawer(page);
  if (!opened) return { count: null, hint: '', text: '', opened: false };
  const text = await page.evaluate(() => document.querySelector('[data-test="history-drawer"]')?.innerText ?? '');
  const match = text.match(/(\d+)\s*个节点/);
  const hint = await page.evaluate(() => document.querySelector('[data-test="save-hint"]')?.innerText ?? '');
  return { count: match ? Number(match[1]) : null, hint: hint.trim(), text: text.slice(0, 400), opened: true };
}

async function closeDrawer(page) {
  const close = page.locator('[data-test="history-close"]');
  if (await close.count()) {
    await close.first().click().catch(() => {});
    await sleep(300);
  }
}

/**
 * 挑一个**确实在视口里**的节点，返回它的中心坐标。
 *
 * 不能直接用 `locator('.vue-flow__node').first()`：Vue Flow 的节点是绝对定位在变换层里
 * 的，Playwright 的"滚动到可见"对它不起作用（它滚的是容器，而节点的坐标来自
 * `transform`），于是一个落在视口外的节点会让点击一直重试到超时。而"在视口里的那个
 * 节点"本来就是这个场景要的：用户点的是他看得见的那个。
 */
async function pickNodeInViewport(page) {
  return page.evaluate(() => {
    const pane = document.querySelector('.vue-flow');
    if (!pane) return null;
    const pr = pane.getBoundingClientRect();
    const els = [...document.querySelectorAll('.vue-flow__node')];
    for (const el of els) {
      const r = el.getBoundingClientRect();
      const pad = 30;
      if (r.left > pr.left + pad && r.right < pr.right - pad
        && r.top > pr.top + pad && r.bottom < pr.bottom - pad) {
        return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), label: el.innerText.trim().slice(0, 20) };
      }
    }
    return null;
  });
}

// ---------------------------------------------------------------- 主流程

async function round1({ page, shot, watch, opts, magic }) {
  // ---- 首屏 + ⑤（idle）
  await page.goto(WEB_URL, { waitUntil: 'networkidle', timeout: 60_000 });
  await sleep(1200);
  await shot('01-idle');
  await checkGroup(page, 'idle');

  // 语音态：点 🎤 之后「按住说话」才存在，核完切回来
  const mic = page.locator('[aria-label="语音输入"]');
  if (await mic.count()) {
    await mic.first().click();
    await sleep(400);
    await shot('02-voice');
    await checkGroup(page, 'voice');
    const back = page.locator('[aria-label="切回键盘输入"]');
    if (await back.count()) await back.first().click();
    await sleep(300);
  } else {
    record('⑤ 手册名@voice', false, '页面上没有 🎤（aria-label="语音输入"）：手册第 532 行的「按住说话」无从核起');
  }

  // ---- 材料进上传链（这一段也记账，但只报读数：上传有自己的出口与时延上限，
  //      ④ 的判据只管"落地前"那一段，理由见 SILENCE_LIMIT_MS 注释）
  const upload = new SilenceWatch(Infinity);
  await page.setInputFiles('input[type=file]', opts.file);
  const ready = await waitWhileWatching(
    page, upload,
    text => /文档\s*·\s*\d+\s*字|已就绪/.test(text),
    90_000, '上传解析',
  );
  const uploadSilence = Math.round(upload.worstMs / 1000);
  record('①a 材料解析有出口', ready.ok,
    ready.ok
      ? `已就绪（上传期最长静默 ${uploadSilence}s，仅读数不判红）`
      : `90 秒内没解析完：${ready.reason}。界面最后几帧：${JSON.stringify(upload.frames.slice(-2))}`);
  if (!ready.ok) return null;
  await shot('03-material-ready');
  await checkGroup(page, 'ready');

  // ---- 点生成：⑤（running，取消）+ ④（静默）
  watch.feed(await page.evaluate(BODY_TEXT));
  watch.lastChangeAt = Date.now();
  const submit = page.locator('button[aria-label="生成导图"]');
  if (!(await submit.count())) {
    record('⑤ 手册名@idle', false, '找不到 aria-label="生成导图" 的按钮，整条路径走不下去');
    return null;
  }
  // 空主题 + 带文件名的材料：手册说这时该能点（后端 `goal` 是 min_length=1，前端用文件名兜底）
  const emptyTopic = await page.evaluate(() => {
    const btn = document.querySelector('button[aria-label="生成导图"]');
    return { disabled: btn.disabled, title: btn.getAttribute('title') };
  });
  record('⑤b 空主题可生成（带文件名）', emptyTopic.disabled === false,
    `disabled=${emptyTopic.disabled}，title=${JSON.stringify(emptyTopic.title)}（手册第 3 步：材料带文件名时可以不写主题）`);

  const t0 = Date.now();
  await submit.first().click();
  await sleep(600);
  await shot('04-running');
  await checkGroup(page, 'running');

  const landed = await waitWhileWatching(
    page, watch,
    text => /已落到画布/.test(text),
    LAND_TIMEOUT_MS, '生成落地',
  );
  const landedSec = ((Date.now() - t0) / 1000).toFixed(1);
  const silenceSec = (watch.worstMs / 1000).toFixed(1);

  // ④：生成期最长静默
  record('④ 最长静默', landed.ok && !watch.breached,
    watch.breached
      ? `静默 ${(watch.stuckAt.silent / 1000).toFixed(0)} 秒超过上限 ${watch.limitMs / 1000} 秒。卡住时界面是：${JSON.stringify(watch.stuckAt.last)}`
      : `${landedSec}s 落地；最长静默 ${silenceSec}s（上限 ${watch.limitMs / 1000}s，采样 ${watch.samples} 帧）`);

  if (!landed.ok) {
    record('① 落地后视口', false, `没落地：${landed.reason}`);
    return null;
  }
  await sleep(1500); // 适配是 150ms 节流 + 一帧
  await shot('05-landed');

  // ---- ① 视口（按档位分）
  const view = await page.evaluate(READ_CANVAS);
  if (view.error) {
    record('① 落地后视口', false, `读不到画布：${view.error}`);
  } else if (view.mode === 'fit-all') {
    record('① 落地后视口', view.inside === view.rendered,
      `档位=全图 → 要求可见=已渲染；读作 ${view.inside}/${view.rendered}=${view.ratio.toFixed(3)}`
      + `（视口 ${view.viewport.w}×${view.viewport.h}，根节点 ${JSON.stringify(view.root)}）`);
  } else {
    // 三条硬断，少了任何一条这条闸就只剩"看起来差不多"：
    //   a. 根节点在视口内 —— 上一轮那个 bug（停在上一次的平移位置）就死在它手上；
    //   b. 可见比例 ≥ 该图的物理上限（同一把尺子：矩形相交 / 矩形并集高度）；
    //   c. **界面自己说的离屏数与几何事实一致** —— 产品可以决定停在看不全的档
    //      （那是可读性取舍），但它说"还有 N 个在视野外"时这个 N 必须是真的。
    const ratioOk = view.ratio >= view.cap - CAP_TOLERANCE;
    const claimOk = view.claimedOffscreen === null
      || Math.abs(view.claimedOffscreen - view.offscreen) <= 1;
    record('① 落地后视口', view.rootInside && ratioOk && claimOk,
      `档位=适应宽度 → 要求①根节点在视口内=${view.rootInside}（${JSON.stringify(view.root)}）`
      + ` ②可见 ${view.inside}/${view.rendered}=${view.ratio.toFixed(3)} ≥ 上限 ${view.cap.toFixed(3)}-${CAP_TOLERANCE}=${ratioOk}`
      + ` ③界面自述「${view.clipped ?? '（没有那句"只显示了一部分"）'}」离屏 ${view.claimedOffscreen} vs 实际 ${view.offscreen}=${claimOk}`
      + `（中心点口径 ${view.centerRatio.toFixed(3)}，图高 ${view.graphHeight}px / 视口 ${view.viewport.h}px）`);
  }

  // ---- ⑤（landed）
  await checkGroup(page, 'landed');

  // ---- ② 引文逐字
  await checkQuote(page, shot);
  await checkGroup(page, 'ref');

  // ---- ⑤（side）：打开节点详情抽屉
  const spot = await pickNodeInViewport(page);
  if (spot) {
    await page.mouse.click(spot.x, spot.y);
    await sleep(700);
    await shot('06-side');
    await checkGroup(page, 'side');
  } else {
    record('⑤ 手册名@side', false, '视口里一个节点都没有，右抽屉无从打开');
  }

  // ---- ③ 刷新重开一致
  await checkRoundTrip({ page, shot, magic, opts });
  return { view, landedSec, magic };
}

/** ② 引文逐字：抽屉里那一句引文，必须逐字包含被高亮的原文。 */
async function checkQuote(page, shot) {
  const picked = await page.evaluate(() => {
    const badge = document.querySelector('.vue-flow__node [data-test="node-refs"]');
    if (!badge) return { ok: false, why: '画布上没有带引用的节点（没有任何一枚引用角标）' };
    badge.click();
    return { ok: true };
  });
  if (!picked.ok) return record('② 引文逐字', false, picked.why);

  await sleep(900);
  const opened = await page.evaluate(() => {
    const heading = [...document.querySelectorAll('h3')].find(h => h.innerText.includes('来源引用'));
    if (!heading) return { ok: false, why: '右抽屉里没有「来源引用」清单（点角标没打开引用列表）' };
    const rows = [...heading.closest('section').querySelectorAll('li')];
    if (!rows.length) return { ok: false, why: '引用清单是空的' };
    // 每条引用有两个可点区：名字（可能是外链 <a>）与引文预览。取最后一个 button = 引文那条。
    for (const row of rows) {
      const buttons = [...row.querySelectorAll('button')];
      if (buttons.length) {
        buttons[buttons.length - 1].click();
        return { ok: true, rows: rows.length };
      }
    }
    return { ok: false, why: `${rows.length} 条引用里没有可点的` };
  });
  if (!opened.ok) return record('② 引文逐字', false, opened.why);

  await sleep(900);
  const quote = await page.evaluate(() => {
    const el = document.querySelector('[data-test="ref-quote"]');
    return el ? el.innerText.trim() : null;
  });
  if (!quote) return record('② 引文逐字', false, '引用抽屉里没有 ref-quote（这条引用没有摘录）');

  const hasToggle = await page.evaluate(() => !!document.querySelector('[data-test="ref-context-toggle"]'));
  if (!hasToggle) {
    return record('② 引文逐字', false,
      `引文有了（${quote.length} 字），但抽屉里没有「看上下文」——这条引用没有坐标，`
      + '无从核对高亮。换一份带坐标的材料再跑，或这本身就是要修的东西。');
  }
  await page.locator('[data-test="ref-context-toggle"]').click();
  await sleep(900);
  await shot('07-ref-context');

  const marked = await page.evaluate(() => {
    const mark = document.querySelector('[data-test="ref-context"] mark');
    return mark ? mark.innerText.trim() : null;
  });
  if (!marked) return record('② 引文逐字', false, '「看上下文」铺开了，但里面没有 <mark>（高亮缺席）');

  const contained = quote.includes(marked);
  record('② 引文逐字', contained && marked.length >= MIN_QUOTE_CHARS,
    `引文 ${quote.length} 字，高亮 ${marked.length} 字，逐字包含=${contained}（下限 ${MIN_QUOTE_CHARS} 字）。`
    + `高亮：${JSON.stringify(marked.slice(0, 40))}`);
  return null;
}

/** ③ 改一处 → 保存 → 刷新 → 重新打开：节点数与那一处改动都得回来。 */
async function checkRoundTrip({ page, shot, magic, opts }) {
  // 双击一个**看得见**的节点改名（手册 §7 的"双击节点改文字"）。
  // 用坐标点而不是 `locator.dblclick()`：理由见 `pickNodeInViewport`。
  const spot = await pickNodeInViewport(page);
  if (!spot) return record('③ 刷新重开一致', false, '视口里没有可点的节点');
  await page.mouse.dblclick(spot.x, spot.y);
  await sleep(600);
  const input = page.locator('.vue-flow__node input').first();
  if (!(await input.count())) return record('③ 刷新重开一致', false, `双击「${spot.label}」没有进入编辑态（没有出现输入框）`);
  await input.fill(magic);
  await input.press('Enter');
  await sleep(700);
  await shot('08-edited');

  // 保存：走 ☰ 里的「保存」（`Ctrl+S` 上一轮实测两次一次静默无反应，不能拿来当判据）
  const drawer = await drawerNodeCount(page);
  const save = page.locator('[data-test="history-drawer"] button', { hasText: /^\s*保存\s*$/ });
  if (!(await save.count())) return record('③ 刷新重开一致', false, '☰ 抽屉里找不到「保存」按钮');
  await save.first().click();
  let saved = { hint: '' };
  for (let i = 0; i < 30; i += 1) {
    await sleep(500);
    saved = await drawerNodeCount(page);
    if (/已保存/.test(saved.hint)) break;
  }
  await closeDrawer(page);
  if (!/已保存/.test(saved.hint)) {
    return record('③ 刷新重开一致', false, `保存没落地：状态行读作 ${JSON.stringify(saved.hint)}`);
  }

  // 后端口径：刚保存的那张图里到底几个节点
  const stored = await page.evaluate(async () => {
    const res = await fetch('/api/mindmaps');
    if (!res.ok) return { error: res.status };
    return { maps: await res.json() };
  });
  if (stored.error) return record('③ 刷新重开一致', false, `读不到 /api/mindmaps：${stored.error}`);
  const latest = [...stored.maps].sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))[0];
  const savedNodes = countNodes(latest?.content?.root);

  record('③a 保存落库', savedNodes > 0 && drawer.count === savedNodes,
    `界面「${drawer.count} 个节点」，后端整树 ${savedNodes} 个${drawer.count === savedNodes ? '' : '（两个口径不一致）'}`);

  // 刷新 → 重新打开
  await page.reload({ waitUntil: 'networkidle', timeout: 60_000 });
  await sleep(1500);
  await shot('09-reloaded');
  const reopened = await page.evaluate(async () => {
    // 刷新后没有 localStorage 恢复，走 ☰ →「打开…」（手册 §7.4 的路径）
    const toggle = document.querySelector('[data-test="history-toggle"]');
    if (!toggle) return { ok: false, why: '刷新后找不到 ☰' };
    toggle.click();
    await new Promise(r => setTimeout(r, 500));
    const openList = document.querySelector('[data-test="history-open-list"]');
    if (!openList) return { ok: false, why: '☰ 里找不到「打开…」' };
    openList.click();
    return { ok: true };
  });
  if (!reopened.ok) return record('③ 刷新重开一致', false, reopened.why);

  let listReady = false;
  for (let i = 0; i < 40; i += 1) {
    await sleep(400);
    const n = await page.locator('h2#list-title').count();
    if (n) { listReady = true; break; }
  }
  if (!listReady) return record('③ 刷新重开一致', false, '「打开…」列表没弹出来');
  await shot('10-open-list');

  const openRow = page.locator('div[role="dialog"]').filter({ hasText: '已保存的导图' })
    .locator('button', { hasText: /^\s*打开\s*$/ });
  if (!(await openRow.count())) return record('③ 刷新重开一致', false, '列表里没有「打开」按钮');
  await openRow.first().click();
  await sleep(2000);

  const after = await page.evaluate(READ_CANVAS);
  const text = await page.evaluate(BODY_TEXT);
  const countAfter = await drawerNodeCount(page);
  await closeDrawer(page);
  await shot('11-reopened');

  const sameCount = countAfter.count === drawer.count && countAfter.count === savedNodes;
  const stillThere = text.includes(magic);
  record('③b 刷新重开一致', sameCount && stillThere && !after.error,
    `重开后界面「${countAfter.count} 个节点」（保存时 ${drawer.count}，后端 ${savedNodes}），`
    + `最后一处改动「${magic}」仍在=${stillThere}，画布已渲染 ${after.rendered ?? '?'} 个`);
  return null;
}

/** 第二轮：同一份材料再走一遍，**不许要人重试**。 */
async function round2({ page, shot, watch, opts }) {
  const quiet = new SilenceWatch(watch.limitMs);
  if (!(await openDrawer(page))) return record('③c 连跑两次', false, '☰ 抽屉打不开');
  const newMap = page.locator('[data-test="history-drawer"] button', { hasText: /^\s*新建\s*$/ });
  if (!(await newMap.count())) return record('③c 连跑两次', false, '☰ 里找不到「新建」');
  await newMap.first().click();
  await sleep(1200);
  await closeDrawer(page);

  await page.setInputFiles('input[type=file]', opts.file);
  const ready = await waitWhileWatching(page, quiet, text => /文档\s*·\s*\d+\s*字|已就绪/.test(text), 90_000, '第二轮上传');
  if (!ready.ok) return record('③c 连跑两次', false, `第二轮材料没就绪：${ready.reason}`);
  await shot('12-round2-ready');

  const submit = page.locator('button[aria-label="生成导图"]');
  await submit.first().click();
  const landed = await waitWhileWatching(page, quiet, text => /已落到画布|生成失败/.test(text), LAND_TIMEOUT_MS, '第二轮生成');
  const text = landed.text ?? '';
  const retry = /重试|失败|超时/.test(text);
  record('③c 连跑两次', landed.ok && !retry && !quiet.breached,
    landed.ok
      ? `第二轮 ${(landed.elapsed / 1000).toFixed(1)}s 落地，界面里没有"重试/失败"字样=${!retry}，最长静默 ${(quiet.worstMs / 1000).toFixed(1)}s`
      : `第二轮没落地：${landed.reason}；界面最后是：${JSON.stringify(normalizeForSilence(text).slice(0, 300))}`);
  await shot('13-round2-landed');
  return null;
}

// ---------------------------------------------------------------- 入口

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const silenceMs = opts.silenceMs ?? SILENCE_LIMIT_MS;
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const shotsDir = opts.shots ?? path.join(os.tmpdir(), `journey-shots-${stamp}`);
  fs.mkdirSync(shotsDir, { recursive: true });
  const magic = `验收改动-${stamp.slice(11)}`;

  console.log('journey.spec —— 第一道问「能不能用」的闸');
  console.log(`  仓库 ${REPO}`);
  console.log(`  站点 ${WEB_URL}  材料 ${opts.sample}  轮数 ${opts.repeat}  静默上限 ${silenceMs / 1000}s`);
  console.log(`  截图与读数 ${shotsDir}`);

  // ⑤ 的手册那一半先跑：它是纯文本，不需要浏览器
  checkGuidePoints();

  const sample = writeSample(opts.sample);
  opts.file = sample.file;

  await requireServices();
  const { chromium } = loadPlaywright();
  const executablePath = findChrome();
  console.log(`  浏览器 ${executablePath}`);

  const browser = await chromium.launch({
    executablePath,
    // headless=false 是**故意的**：这条闸量的是"用户眼前那张图"。headless 下没有
    // 真实的 devicePixelRatio/字体度量，缩放与折行的结果会不一样，而那正是 ① 要量的。
    headless: false,
    args: ['--start-maximized', '--window-size=1440,900'],
  });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.on('dialog', dialog => dialog.accept());   // 「新建/打开会丢掉未保存修改」那句确认
  const consoleErrors = [];
  page.on('pageerror', e => consoleErrors.push(String(e).slice(0, 300)));

  let n = 0;
  const shot = async (name) => {
    n += 1;
    const file = path.join(shotsDir, `${String(n).padStart(2, '0')}-${name}.png`);
    await page.screenshot({ path: file });
    return file;
  };

  const watch = new SilenceWatch(silenceMs);
  try {
    await round1({ page, shot, watch, opts, magic });
    if (opts.repeat >= 2) await round2({ page, shot, watch, opts });
  } finally {
    if (!opts.keep) await browser.close();
  }

  const summary = {
    when: new Date().toISOString(),
    sample: { id: sample.sample.id, title: sample.sample.title, chars: sample.sample.text.length },
    silenceLimitMs: silenceMs,
    results,
    consoleErrors,
    shots: shotsDir,
  };
  fs.writeFileSync(path.join(shotsDir, 'summary.json'), JSON.stringify(summary, null, 2), 'utf8');

  const bad = failures();
  console.log('');
  console.log(`—— 汇总：${results.length - bad.length}/${results.length} 条通过 ——`);
  for (const item of bad) console.log(`  红：${item.id} — ${item.detail}`);
  if (consoleErrors.length) console.log(`  页面异常 ${consoleErrors.length} 条：${consoleErrors.slice(0, 3).join(' | ')}`);
  console.log(`  读数写进 ${path.join(shotsDir, 'summary.json')}`);
  if (!opts.keep) console.log('  （浏览器已关闭；--keep 可留下来看最后那一屏）');

  process.exit(bad.length ? 1 : 0);
}

main().catch((error) => {
  if (error instanceof EnvError) {
    console.error(`\n环境不满足（退出码 2）：\n${error.message}\n`);
    process.exit(2);
  }
  console.error('\n脚本自己炸了：');
  console.error(error);
  process.exit(1);
});
