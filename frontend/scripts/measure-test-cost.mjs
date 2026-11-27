// 量"同一套用例在两种负载下的耗时"：跑一次全量 vitest，连同机器负载一起记下来。
//
// 为什么不是"读一次日志估一估"：本仓库已经三次在**负载**上翻车
// （docs/TEST-COST-136-NODE.md、docs/TEST-COST-3-CASES.md、docs/TEST-LOAD-SENSITIVITY.md），
// 每次都是靠"这一条加宽"止血，而没有一张"哪些用例对负载敏感"的表。这个脚本就是那张表的
// 取样端：它记录的每条耗时都带当时的 CPU 占用，所以"空闲 420ms / 满载 4100ms"这种读数
// 没法靠回忆编出来。
//
// 用法：
//   node scripts/measure-test-cost.mjs --label idle            # 机器上没有别的重活时跑
//   node scripts/measure-test-cost.mjs --label loaded --note "backend pytest 全量在跑"
//   node scripts/measure-test-cost.mjs --label solo --solo --files tests/a.spec.ts,tests/b.spec.ts
//                                        # 单文件串行：开发者本地验证的形状
//   node scripts/measure-test-cost.mjs --compare a.json b.json  # 生成对照表（按倍数降序）
//
// 只读：不改测试、不碰 src。产物是 JSON 快照（默认写到 scripts/.test-cost/ 之外的
// 调用方指定路径），供 docs/TEST-LOAD-SENSITIVITY.md 引用。
import { spawn } from 'node:child_process';
import { cpus, totalmem, freemem } from 'node:os';
import { mkdirSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND = resolve(HERE, '..');

function parseArgs(argv) {
  const out = { label: null, note: '', out: null, compare: null, solo: false, files: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    if (key === '--label') out.label = argv[++i];
    else if (key === '--note') out.note = argv[++i];
    else if (key === '--out') out.out = argv[++i];
    else if (key === '--solo') out.solo = true;
    else if (key === '--files') out.files = argv[++i].split(',').map((s) => s.trim()).filter(Boolean);
    else if (key === '--compare') out.compare = [argv[++i], argv[++i]];
  }
  return out;
}

/** os.cpus() 的 times 差分 —— 不引入任何依赖，也不需要管理员权限。 */
function cpuSnapshot() {
  let idle = 0;
  let total = 0;
  for (const cpu of cpus()) {
    const t = cpu.times;
    idle += t.idle;
    total += t.user + t.nice + t.sys + t.idle + t.irq;
  }
  return { idle, total };
}

function waitPid(child) {
  return new Promise((resolvePromise) => {
    child.on('close', (code, signal) => resolvePromise({ code, signal }));
  });
}

/** 开跑前的静默采样：证明"外部负载"是什么状态，而不是靠回忆写一句"当时机器很闲"。 */
async function baselineCpu(ms = 3000) {
  const before = cpuSnapshot();
  await new Promise((r) => setTimeout(r, ms));
  const after = cpuSnapshot();
  const dTotal = after.total - before.total;
  const dIdle = after.idle - before.idle;
  return dTotal > 0 ? Math.round((1 - dIdle / dTotal) * 1000) / 10 : 0;
}

async function measure(label, note, outPath, options = {}) {
  const baseline = await baselineCpu();
  const samples = [];
  let prev = cpuSnapshot();
  const timer = setInterval(() => {
    const now = cpuSnapshot();
    const dTotal = now.total - prev.total;
    const dIdle = now.idle - prev.idle;
    if (dTotal > 0) samples.push(Math.round((1 - dIdle / dTotal) * 1000) / 10);
    prev = now;
  }, 500);

  const jsonPath = resolve(FRONTEND, 'scripts', '_test-cost-last.json');
  rmSync(jsonPath, { force: true });

  const startedAt = Date.now();
  // 默认（--label idle / loaded）：一个字都不加，跟 `npm test` / CI 完全一致。
  // 追加档（--solo）：只跑指定文件、且不并行 —— 这是"开发者本地验证一条用例"的形状，
  // 用来回答"到底是外部负载把它推过线的，还是全量并行本身"。
  const extra = [];
  if (options.solo) {
    extra.push('--no-file-parallelism', ...(options.files ?? []));
  }
  const child = spawn(
    process.platform === 'win32' ? 'npx.cmd' : 'npx',
    ['vitest', 'run', '--reporter=json', `--outputFile=${jsonPath}`, ...extra],
    { cwd: FRONTEND, stdio: ['ignore', 'pipe', 'pipe'], shell: process.platform === 'win32' },
  );
  let stdout = '';
  child.stdout.on('data', (chunk) => { stdout += chunk.toString(); });
  child.stderr.on('data', (chunk) => { stdout += chunk.toString(); });

  const exit = await waitPid(child);
  const wallMs = Date.now() - startedAt;
  clearInterval(timer);

  const raw = JSON.parse(readFileSync(jsonPath, 'utf8'));
  rmSync(jsonPath, { force: true });

  const tests = [];
  for (const file of raw.testResults ?? []) {
    for (const assertion of file.assertionResults ?? []) {
      tests.push({
        file: String(file.name ?? '').replace(/\\/g, '/').split('/tests/').pop(),
        full: assertion.fullName,
        ms: Math.round((assertion.duration ?? 0) * 10) / 10,
        status: assertion.status,
      });
    }
  }

  const snapshot = {
    label,
    note,
    startedAt: new Date(startedAt).toISOString(),
    wallMs,
    exitCode: exit.code,
    cpu: {
      cores: cpus().length,
      baselinePct: baseline,
      memFreeGB: Math.round(freemem() / 1e9 * 10) / 10,
      memTotalGB: Math.round(totalmem() / 1e9 * 10) / 10,
      samples: samples.length,
      mean: Math.round(samples.reduce((a, b) => a + b, 0) / Math.max(samples.length, 1) * 10) / 10,
      max: samples.length ? Math.max(...samples) : 0,
    },
    totals: {
      files: raw.numTotalTestSuites,
      tests: raw.numTotalTests,
      failed: raw.numFailedTests,
      passed: raw.numPassedTests,
      slowestTestMs: tests.reduce((a, t) => Math.max(a, t.ms), 0),
    },
    tests,
  };

  mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, `${JSON.stringify(snapshot, null, 2)}\n`, 'utf8');
  return snapshot;
}

function pct(values, q) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))];
}

function compare(aPath, bPath) {
  const a = JSON.parse(readFileSync(aPath, 'utf8'));
  const b = JSON.parse(readFileSync(bPath, 'utf8'));
  const index = new Map(b.tests.map((t) => [`${t.file}::${t.full}`, t]));
  const rows = [];
  for (const t of a.tests) {
    const other = index.get(`${t.file}::${t.full}`);
    if (!other) continue;
    rows.push({
      file: t.file,
      name: t.full,
      idle: t.ms,
      loaded: other.ms,
      ratio: t.ms > 0 ? Math.round((other.ms / t.ms) * 100) / 100 : null,
      statusLoaded: other.status,
    });
  }
  rows.sort((x, y) => (y.ratio ?? 0) - (x.ratio ?? 0));
  const ratios = rows.map((r) => r.ratio ?? 0);
  const over10 = rows.filter((r) => (r.ratio ?? 0) > 10);
  const summary = {
    idle: { label: a.label, note: a.note, at: a.startedAt, wallMs: a.wallMs, cpu: a.cpu, totals: a.totals },
    loaded: { label: b.label, note: b.note, at: b.startedAt, wallMs: b.wallMs, cpu: b.cpu, totals: b.totals },
    medianRatio: pct(ratios, 0.5),
    p90Ratio: pct(ratios, 0.9),
    maxRatio: ratios.length ? Math.max(...ratios) : 0,
    over10: over10.length,
    rows,
  };
  writeFileSync(resolve(FRONTEND, 'scripts', '_test-cost-compare.json'),
    `${JSON.stringify(summary, null, 2)}\n`, 'utf8');

  const lines = [];
  lines.push(`| 用例 | 空闲 | 满载 | 倍数 |`);
  lines.push(`|---|---|---|---|`);
  for (const r of rows.slice(0, 40)) {
    lines.push(`| ${r.file} › ${r.name} | ${r.idle}ms | ${r.loaded}ms | ${r.ratio}× |`);
  }
  lines.push('');
  lines.push(`中位 ${summary.medianRatio}× / p90 ${summary.p90Ratio}× / 最大 ${summary.maxRatio}× / 超 10× 的 ${summary.over10} 条 / 共 ${rows.length} 条`);
  console.log(lines.join('\n'));
  return summary;
}

const args = parseArgs(process.argv.slice(2));
if (args.compare) {
  compare(args.compare[0], args.compare[1]);
} else {
  if (!args.label) throw new Error('需要 --label（idle / loaded / 其他可读标签）');
  const outPath = resolve(FRONTEND, 'scripts', args.out ?? `_test-cost-${args.label}.json`);
  const snap = await measure(args.label, args.note, outPath, { solo: args.solo, files: args.files });
  console.log(JSON.stringify({
    label: snap.label,
    wallMs: snap.wallMs,
    cpuMean: snap.cpu.mean,
    cpuMax: snap.cpu.max,
    totals: snap.totals,
    out: outPath,
  }, null, 2));
}
