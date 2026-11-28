/**
 * `journey.spec.mjs` 的基建：环境自检、静默采样、视口读数、按名定位、手册清单。
 *
 * ## 为什么另起一份而不是复用 `lib.mjs`
 *
 * `lib.mjs` 是前两轮那 8 个探针共用的，它把本机绝对路径写死在文件顶层
 * （`EXE = 'C:\\Users\\tao\\...'`、`SHOTS = 'D:\\genai-mindmap\\...'`）。那对"一次
 * 性验收、当场读一张图"是对的；`journey.spec` 要**长期存在**、要"一个人拿着它跑出
 * 同样的结果"，硬编码路径对它就是错的：换台机器、换个用户名、换个体检目录，脚本
 * 连启动都做不到，而失败信息会指向一行路径常量。
 *
 * 所以这一份的三条硬规矩：
 *
 * 1. **路径一律以 `import.meta.url` 为锚**，不写本机任何绝对路径；
 * 2. **依赖从仓库解析**：`playwright-core` 用 `frontend/package.json` 做解析锚点，
 *    不在本文件里写 `node_modules` 在哪；
 * 3. **浏览器单独探测**：`playwright-core` 不带浏览器（这正是选它而不是
 *    `playwright` 的原因），可执行文件从 `JOURNEY_CHROME` 或本机缓存里找，
 *    找不到就报"缺什么、装哪一条命令"，而不是打一行 undefined 再继续。
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

/** 本文件所在目录（`docs/acceptance-scripts/`）。 */
export const HERE = path.dirname(fileURLToPath(import.meta.url));
/** 仓库根。所有相对路径都以它为基准，不用 `process.cwd()`——那取决于人在哪儿敲命令。 */
export const REPO = path.resolve(HERE, '..', '..');

export const WEB_URL = process.env.JOURNEY_WEB ?? 'http://127.0.0.1:5173/';
export const API_URL = process.env.JOURNEY_API ?? 'http://127.0.0.1:8000';

/** 环境不满足（服务没起、依赖没装、浏览器找不到）。退出码 2——与"断言红了"分开。 */
export class EnvError extends Error {}

// ---------------------------------------------------------------- 依赖

/**
 * 取 `playwright-core`。
 *
 * `playwright-core` 现在在 `frontend/package.json` 的 devDependencies 里（判据见
 * `docs/JOURNEY-SPEC.md` §「依赖放哪儿」）。解析锚点是那份 `package.json`，所以
 * 在 `docs/` 里、在 `frontend/` 里、在仓库根敲命令，拿到的都是同一份。
 */
export function loadPlaywright() {
  const req = createRequire(path.join(REPO, 'frontend', 'package.json'));
  try {
    return req('playwright-core');
  } catch (cause) {
    throw new EnvError(
      '找不到 playwright-core。它是 frontend 的 devDependency，在仓库根执行：\n'
      + '  cd frontend && npm install\n'
      + `（解析锚点：${path.join(REPO, 'frontend', 'package.json')}）\n`
      + `原始错误：${cause instanceof Error ? cause.message : String(cause)}`,
    );
  }
}

// ---------------------------------------------------------------- 浏览器

const CHROME_NAMES = {
  win32: [path.join('chrome-win64', 'chrome.exe'), path.join('chrome-win', 'chrome.exe')],
  darwin: [path.join('chrome-mac', 'Chromium.app', 'Contents', 'MacOS', 'Chromium')],
  linux: [path.join('chrome-linux', 'chrome'), path.join('chrome-linux64', 'chrome')],
};

function browserRoots() {
  const roots = [];
  // Playwright 自己的浏览器缓存（`npx playwright install chromium` 落在这儿）。
  if (process.env.PLAYWRIGHT_BROWSERS_PATH) roots.push(process.env.PLAYWRIGHT_BROWSERS_PATH);
  if (process.platform === 'win32' && process.env.LOCALAPPDATA) {
    roots.push(path.join(process.env.LOCALAPPDATA, 'ms-playwright'));
  }
  if (process.env.HOME) {
    roots.push(path.join(process.env.HOME, '.cache', 'ms-playwright'));
    roots.push(path.join(process.env.HOME, 'Library', 'Caches', 'ms-playwright'));
  }
  return roots;
}

/** 带版本号的目录按"数字段"排，取最大那份（`chromium-999` 要排在 `chromium-1248` 前）。 */
function newestFirst(dirs) {
  return dirs.sort((a, b) => {
    const num = s => (s.match(/\d+/g) ?? []).map(Number);
    const [x, y] = [num(a), num(b)];
    for (let i = 0; i < Math.max(x.length, y.length); i += 1) {
      const d = (y[i] ?? 0) - (x[i] ?? 0);
      if (d) return d;
    }
    return a.localeCompare(b);
  });
}

/**
 * 找 Chromium 可执行文件。
 *
 * 顺序：`JOURNEY_CHROME` → `ms-playwright` 里最新的那份 → 系统 Chrome。
 * 三步都不命中就报错退出（退出码 2），**不**回落到"随便启动一个"——真浏览器是这条
 * 闸的全部立身之本（"用户手里那张图不在任何语法树里"），悄悄换成一个 headless
 * 壳子等于把闸的门槛挪了。
 */
export function findChrome() {
  const explicit = process.env.JOURNEY_CHROME;
  if (explicit) {
    if (fs.existsSync(explicit)) return explicit;
    throw new EnvError(`JOURNEY_CHROME 指向的文件不存在：${explicit}`);
  }

  const names = CHROME_NAMES[process.platform] ?? CHROME_NAMES.linux;
  for (const root of browserRoots()) {
    if (!fs.existsSync(root)) continue;
    const versions = newestFirst(fs.readdirSync(root).filter(d => d.startsWith('chromium-')));
    for (const dir of versions) {
      for (const name of names) {
        const file = path.join(root, dir, name);
        if (fs.existsSync(file)) return file;
      }
    }
  }

  const system = process.platform === 'win32'
    ? [
      path.join(process.env['PROGRAMFILES'] ?? 'C:\\Program Files', 'Google', 'Chrome', 'Application', 'chrome.exe'),
      path.join(process.env['PROGRAMFILES(X86)'] ?? 'C:\\Program Files (x86)', 'Google', 'Chrome', 'Application', 'chrome.exe'),
    ]
    : ['/usr/bin/google-chrome', '/usr/bin/chromium', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'];
  for (const file of system) if (fs.existsSync(file)) return file;

  throw new EnvError(
    '找不到 Chromium。装一份（约 130MB，只装一次，落在用户目录，不进仓库）：\n'
    + '  npx playwright install chromium\n'
    + '已有别的 Chrome 就指过去：JOURNEY_CHROME=<可执行文件路径>\n'
    + `找过这些目录：${browserRoots().join(' ; ') || '(无)'}`,
  );
}

// ---------------------------------------------------------------- 服务自检

async function reachable(url) {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(5000) });
    return { ok: res.ok, status: res.status };
  } catch (cause) {
    return { ok: false, status: 0, error: cause instanceof Error ? cause.message : String(cause) };
  }
}

/**
 * 前后端都要在跑。**先查再启动浏览器**：这条路的失败信息差别很大——"服务没起"是
 * 环境问题（退出码 2），"落地 40 秒没有新字"是闸抓到的产品问题（退出码 1）。
 * 混成一个 1 就等于让人每周重判一次"这次红是环境还是产品"。
 */
export async function requireServices() {
  const health = await reachable(`${API_URL}/api/health`);
  if (!health.ok) {
    throw new EnvError(
      `后端不在：${API_URL}/api/health → ${health.status || health.error}\n`
      + '  起法：cd backend && python -m uvicorn app.main:app --port 8000',
    );
  }
  const web = await reachable(WEB_URL);
  if (!web.ok) {
    throw new EnvError(
      `前端不在：${WEB_URL} → ${web.status || web.error}\n`
      + '  起法：cd frontend && npm run dev',
    );
  }
}

// ---------------------------------------------------------------- 静默采样

/**
 * 把"计时器在走"从"界面有新字"里剔掉。
 *
 * ④ 这条断言问的是**界面有没有新东西可说**，不是"那段字有没有变"。生成期前端有一个
 * 每秒 +1 的秒表（`ChatDock.vue::elapsedSec`），上传期也有（`SourceInputs` /
 * `sources.ts` 的 `item.seconds`）。秒表在走的时候界面**每秒都变**，而对用户来说
 * 它就是"还在转的那颗圈"——上一轮那个坑（"一直显示正在解析…38 秒"）正是这个形状：
 * 数字涨到 46，用户仍然一无所知。
 *
 * 所以采样前先把 `N 秒` 归一化成 `<t> 秒`。**只抹秒数，别的数字一律保留**——
 * "40 / 136 个节点"、"20 条诊断"这些是业务读数，它们变了就是真的有新东西。
 */
export function normalizeForSilence(text) {
  return text.replace(/\d+(?:\.\d+)?\s*秒/g, '<t> 秒');
}

/**
 * 界面文本流：记录"最后一次出现新字是什么时候"。
 *
 * 刻意不写阈值——阈值只有一个来源（`docs/JOURNEY-SPEC.md` 里带依据的那个数），
 * 由调用方传进来；这里只负责测。
 */
export class SilenceWatch {
  constructor(limitMs) {
    this.limitMs = limitMs;
    this.last = null;
    this.lastChangeAt = Date.now();
    this.worstMs = 0;
    this.stuckAt = null;
    this.samples = 0;
    this.frames = [];
  }

  /** 喂一帧界面文本；返回"距离上一帧新字过了多久"。 */
  feed(text) {
    this.samples += 1;
    const norm = normalizeForSilence(text);
    if (norm !== this.last) {
      this.last = norm;
      this.lastChangeAt = Date.now();
      this.frames.push({ at: new Date().toISOString(), text: norm.slice(0, 240) });
      if (this.frames.length > 12) this.frames.shift();
      return 0;
    }
    const silent = Date.now() - this.lastChangeAt;
    if (silent > this.worstMs) this.worstMs = silent;
    if (silent > this.limitMs) this.stuckAt = { silent, last: norm.slice(0, 400) };
    return silent;
  }

  get silentMs() {
    return Date.now() - this.lastChangeAt;
  }

  get breached() {
    return this.stuckAt !== null;
  }
}

// ---------------------------------------------------------------- 页面读数

/**
 * 画布读数：视口矩形、已渲染节点、视口内节点、根节点、当前档位、该图的物理上限。
 *
 * `cap` 是"适应宽度档在不缩放的前提下最多能看见多少"：**按同一口径算**——用节点中心
 * 点的纵向跨度比视口高度。这样"可见比例"与"物理上限"量的是同一件事（中心点在不在
 * 视口里），两个数才能直接比。上一轮判词要的就是这个：
 * "区分'没适配'与'适配到看不全的档'"。
 */
export const READ_CANVAS = () => {
  const pane = document.querySelector('.vue-flow');
  if (!pane) return { error: 'no .vue-flow' };
  const pr = pane.getBoundingClientRect();
  const els = [...document.querySelectorAll('.vue-flow__node')];
  const rects = els
    .map(el => el.getBoundingClientRect())
    .filter(r => r.width > 0 && r.height > 0);
  const nodes = els.map((el) => {
    const r = el.getBoundingClientRect();
    return {
      label: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 24),
      x: r.x + r.width / 2,
      y: r.y + r.height / 2,
    };
  });

  // **口径必须与产品同源**：`MindMapCanvas` 自己算"还有 N 个节点在视野外"用的是
  // "节点矩形与视口矩形有没有交集"，不是"节点中心点在不在视口里"。两把尺子量同一件
  // 事，红的时候说不清是产品坏了还是闸太严——上一轮那个 0.563/0.656 的读数就是这么来的。
  const overlaps = r => r.right > pr.left && r.left < pr.right && r.bottom > pr.top && r.top < pr.bottom;
  const visible = rects.filter(overlaps).length;
  const insideCenter = nodes.filter(n => n.x > pr.left && n.x < pr.right && n.y > pr.top && n.y < pr.bottom);

  // 该图的物理上限：装进视口高度最多能看见多少。用**矩形并集的纵向跨度**（含节点自身
  // 高度）而不是中心点跨度——后者的分母少了一个节点高，上限会被高估，于是"适配对了"
  // 也判红。
  const top = rects.length ? Math.min(...rects.map(r => r.top)) : 0;
  const bottom = rects.length ? Math.max(...rects.map(r => r.bottom)) : 0;
  const graphH = bottom - top;

  const root = nodes.slice().sort((a, b) => a.x - b.x)[0] ?? null;
  const rootInside = !!root
    && root.x > pr.left && root.x < pr.right && root.y > pr.top && root.y < pr.bottom;
  const clipped = document.querySelector('[data-test="view-clipped"]')?.innerText?.trim() ?? null;
  const claimed = clipped ? Number((clipped.match(/还有\s*(\d+)\s*个节点/) ?? [])[1] ?? NaN) : null;

  return {
    rendered: rects.length,
    inside: visible,
    ratio: rects.length ? visible / rects.length : 0,
    // 中心点口径只作读数留档：换尺子的时候得有旧数可比。
    centerRatio: nodes.length ? insideCenter.length / nodes.length : 0,
    viewport: { w: Math.round(pr.width), h: Math.round(pr.height), left: pr.left, top: pr.top },
    graphHeight: Math.round(graphH),
    root: root ? { x: Math.round(root.x), y: Math.round(root.y), label: root.label } : null,
    rootInside,
    cap: Math.min(1, pr.height / (graphH || 1)),
    mode: document.querySelector('[data-test="view-mode-all"]')?.getAttribute('aria-pressed') === 'true'
      ? 'fit-all' : 'fit-width',
    clipped,
    claimedOffscreen: Number.isFinite(claimed) ? claimed : null,
    offscreen: rects.length - visible,
  };
};

/** 采一帧界面纯文本（不含 `<script>`/`<style>`，`innerText` 本来就不含）。 */
export const BODY_TEXT = () => document.body.innerText;

export const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

// ---------------------------------------------------------------- 按名定位

/**
 * 手册里"用户被要求点的东西"，按界面时点分组。
 *
 * ⑤ 的判据是双向的，两半都必须机械可核：
 *
 *   手册 → 清单：每条 `name` 必须在 `docs/USER-GUIDE.md` 里以 `「name…」` 的形式出现
 *                （前缀匹配到 `」` 之前，所以「添加材料 +」能对上 `添加材料`）。
 *                手册改了名字而清单没改 → 红。
 *   清单 → 页面：每条 `name` 必须在对应时点的页面上**按名点到** → 见 `LOCATE`。
 *
 * 为什么不在页面上反查"所有按钮都在手册里"：界面有 180 多个 `data-test`，其中
 * 大半是状态位与读数（`view-clipped`、`save-hint`…），不是动作。反查会把整张清单
 * 变成一份产品代码副本，然后没人再读它。**只核手册承诺过的那 20 条**，这是这道闸
 * 的取舍：窄、可复核、红了 blame 得清。
 *
 * `kind` 只影响定位优先级：
 *   `action`  按钮/链接/下拉：`aria-label` 等值 → `aria-label` 前缀 → 可见文本等值
 *   `field`   输入框：`aria-label` 等值 → placeholder 前缀
 */
export const MANUAL_POINTS = [
  // 首屏：还没有图的那一屏（手册第 1–3 步）
  { group: 'idle', name: '生成导图', kind: 'action' },
  { group: 'idle', name: '添加材料', kind: 'action' },
  { group: 'idle', name: '主题或要求', kind: 'field' },
  { group: 'idle', name: '深度思考', kind: 'action' },
  { group: 'idle', name: '智能搜索', kind: 'action' },

  // 有材料之后：缩略块右边那个按钮才叫「管理材料」（0 份时它不叫这个名字——
  // 手册第 2 步描述的也正是"加了材料之后"那一屏）
  { group: 'ready', name: '管理材料', kind: 'action' },

  // 语音态：点 🎤 之后「按住说话」才存在（手册第 40 行与 §532 都这么写）
  { group: 'voice', name: '按住说话', kind: 'action' },

  // 生成中（手册第 5 步）：对话框收成一条进度，「取消」是唯一出口
  { group: 'running', name: '取消', kind: 'action' },

  // 落地后（手册第 6–7 步）
  { group: 'landed', name: '适应宽度', kind: 'action' },
  { group: 'landed', name: '全图', kind: 'action' },
  { group: 'landed', name: '改这张图', kind: 'action' },

  // ☰ 抽屉（手册第 7 节）
  { group: 'drawer', name: '保存', kind: 'action' },
  { group: 'drawer', name: '知识库', kind: 'action' },
  { group: 'drawer', name: '打开…', kind: 'action' },
  { group: 'drawer', name: '导入', kind: 'action' },
  { group: 'drawer', name: '导出…', kind: 'action' },

  // 右侧抽屉（手册第 7 节 ②③④）
  { group: 'side', name: '检查与建议', kind: 'action' },
  { group: 'side', name: '改动记录', kind: 'action' },

  // 引用抽屉（手册第 7 步 ④）。`「看上下文」点完之后自己会变成「收起上下文」`，
  // 所以按名找的时候两个名字都算——这不是放宽判据，是同一个按钮的两种状态。
  { group: 'ref', name: '看上下文', kind: 'action', also: ['收起上下文'] },
];

/**
 * 手册提过、但这张清单**故意不收**的名字，以及每条的理由。
 *
 * 这张表是防止"清单被收窄到永远绿"的那一半：把"为什么不收"写下来，才看得见它是不是
 * 在偷懒。三条判据之一不满足就不收：
 *
 *   · 出现条件是**数据形状**而不是手册承诺的常态（`上一处`/`下一处`/`再读下文` 要
 *     同一句引文命中多处才有；`修复` 要有可自动修的三类诊断；`另存为` 界面根本没有）；
 *   · 出现条件是**生成结果里的偶发状态**（`回到生成之前` 要 `canUndoGenerate`）；
 *   · 名字在手册里没有以 `「…」` 的形式写出来（`新建`、`关闭`、`演示示例`）——
 *     那不是"页面少了入口"，是**手册少了一句**，属于另一类缺口。
 */
export const MANUAL_SKIPPED = [
  { name: '全部展开', reason: '只有节点数超过折叠上限（40）时才出现，而节点数是模型给的（实测 30–82）：拿它当条律等于抽奖，换个材料就红' },
  { name: '新建', reason: '手册全文没有「新建」二字（`docs/USER-GUIDE.md` 实测 0 命中）：界面有、手册没写，是文档缺口不是页面缺口' },
  { name: '关闭', reason: '手册没写这个按钮名（用户在抽屉里靠 ✕/点外面关），清单不收手册没承诺的名字' },
  { name: '演示示例', reason: '同上：手册未以「…」形式提到' },
  { name: '另存为', reason: '界面上**不存在**这个按钮；手册 §7.4 已改为"没有「另存为」这个按钮"' },
  { name: '回到生成之前', reason: '只在 `store.canUndoGenerate` 为真时出现，属生成结果的偶发态' },
  { name: '修复', reason: '只有诊断里存在"修法唯一"的三类时才出现' },
  { name: '上一处', reason: '同一句引文命中多处（`hits > 1`）才有' },
  { name: '下一处', reason: '同上' },
  { name: '再读上文', reason: '引文不在正文开头时才有（`canGrowBefore`）' },
];

/**
 * 在页面里按名找。
 *
 * `visible` 沿祖先链查 `display/visibility/opacity`（这个仓库的浮层几乎都用 `v-show`
 * 常驻挂载：`HistoryDrawer`、材料面板、偏好浮层都是"收起只是看不见，DOM 还在"，
 * 只看 `querySelector` 会把一个用户看不见的按钮算成入口）。
 *
 * `inViewport` **只报读数、不判红**：手册承诺的是"点 ☰ → 导出…"，用户能不能点到
 * 靠滚动，不该被"1440×900 首屏放不下 26 条历史"判死——那是一条已知的列表变长
 * 后果（`ACCEPTANCE-WALKTHROUGH-2026-11-28.md` §6.4），把它写进闸等于给闸发一张
 * 立刻要加豁免的红牌。真要治它，治的是抽屉的信息层级，不是这条断言。
 */
export const LOCATE = (point) => {
  const visible = (el) => {
    let node = el;
    while (node && node.nodeType === 1) {
      const style = getComputedStyle(node);
      if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
      }
      node = node.parentElement;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const norm = s => (s ?? '').replace(/\s+/g, ' ').trim();
  const detail = (el) => {
    const rect = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      text: norm(el.innerText || el.value || el.getAttribute('aria-label') || '').slice(0, 40),
      aria: el.getAttribute('aria-label'),
      disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      visible: visible(el),
      inViewport: rect.top < window.innerHeight && rect.bottom > 0
        && rect.left < window.innerWidth && rect.right > 0,
      rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
    };
  };

  const name = point.name;
  // `also` 是同一个按钮的另一种状态文案（「看上下文」点完变「收起上下文」）。
  // 手册承诺的是那个按钮，不是那两个字。
  const names = [name, ...(point.also ?? [])];

  if (point.kind === 'field') {
    const fields = [...document.querySelectorAll('input:not([type="file"]), textarea')];
    const byAria = fields.find(el => names.some(n => norm(el.getAttribute('aria-label')) === n));
    if (byAria) return { found: true, via: 'aria-exact', ...detail(byAria) };
    // 手册写的是**界面上那句话**，而 `aria-label` 是给读屏的另一个名字：
    // 「主题或要求」这个框的 `aria-label` 是"主题或修改指令"。所以按可见文案找，
    // 再回到它旁边（3 层祖先以内）的那个输入框。3 层是个刻意的窄口径——再往上
    // 就退化成"整个页面里有这几个字就算命中"，那等于没查。
    const byLabel = fields.find((el) => {
      let scope = el.parentElement;
      for (let i = 0; i < 3 && scope; i += 1) {
        if (names.some(n => norm(scope.innerText).includes(n))) return true;
        scope = scope.parentElement;
      }
      return false;
    });
    if (byLabel) return { found: true, via: 'nearby-text', ...detail(byLabel) };
    const byPlaceholder = fields.find(el => names.some(n => norm(el.getAttribute('placeholder')).startsWith(n)));
    return byPlaceholder ? { found: true, via: 'placeholder', ...detail(byPlaceholder) } : { found: false };
  }

  const actionable = [...document.querySelectorAll('button, a, select, [role="button"], label')];
  for (const target of names) {
    const exactAria = actionable.find(el => norm(el.getAttribute('aria-label')) === target);
    if (exactAria) return { found: true, via: 'aria-exact', matched: target, ...detail(exactAria) };
    // `aria-label` 前缀：`添加材料 +`、`按住说话` 这类带后缀的写法
    const prefixAria = actionable.find(el => norm(el.getAttribute('aria-label')).startsWith(target));
    if (prefixAria) return { found: true, via: 'aria-prefix', matched: target, ...detail(prefixAria) };
    // `sr-only` 正文：生成按钮的真实文本是它的 `aria-label`，`innerText` 里只有 CSS 箭头
    const exactText = actionable.find(el => norm(el.innerText) === target);
    if (exactText) return { found: true, via: 'text-exact', matched: target, ...detail(exactText) };
    // 下拉里 `<option>` 的初始文案（「导出…」是 select 的占位 option）
    const option = [...document.querySelectorAll('option')].find(el => norm(el.innerText) === target);
    if (option) return { found: true, via: 'option', matched: target, ...detail(option.parentElement) };
    const prefixText = actionable.find(el => norm(el.innerText).startsWith(target));
    if (prefixText) return { found: true, via: 'text-prefix', matched: target, ...detail(prefixText) };
  }
  return { found: false };
};
