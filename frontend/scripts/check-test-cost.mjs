// 闸门：核对每条"贵用例"的超时上限是否还够，以及是否被放宽到掩盖慢。
//
// 由来：本仓库三次在**负载**上翻车（2026-11-25 两次、2026-11-27 一次），三次的处置都是
// "这一条加宽"，而下一次换一条继续翻。原因不是有人不小心，而是**判据缺失**：没有人知道
// 一条用例在全量并行下的耗时是多少，也就没法判断"5000 够不够"。这份基线与闸门把那个
// 判断变成一次静态比对：
//
//   cap < 1.5 × 基线满载 → 红（余量 < 33%，就是今天 defects.spec.ts 那条的位置）
//   cap < 2.0 × 基线满载 → 黄（CI runner 通常比本机慢 1.3–2×，2× 才算安全线）
//
// 只查"不够"，不查"过宽"：describe 级上限是按组内最慢那条定的，对它管辖的每条快用例
// 都算一遍"宽不宽"会产出上百条噪声——那正是仓库里 ruff 那条注释警告的形态
// （"人们学会无视这条检查"）。宽上限的代价另由 docs/TEST-LOAD-SENSITIVITY.md §3 的
// "别超过满载 × 4"这条写作约定来管。
//
// 它是**棘轮**不是绝对零（与 `scripts/find_unwired.mjs` 同一套语义）：基线之外的用例
// 不阻断，只提示"跑一次 `npm run test:cost` 把它记进来"。基线文件是
// `scripts/test-cost-baseline.json`，由 `report-test-cost.mjs --baseline` 生成。
//
// 用法：
//   node scripts/check-test-cost.mjs            # 报告（总是 0）
//   node scripts/check-test-cost.mjs --gate     # 红项 → 退出码 1
import { readFileSync } from 'node:fs';
import { dirname, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DEFAULT_TIMEOUT, collectLimits, walkSpecs } from './test-cost-limits.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND = resolve(HERE, '..');
const BASELINE = resolve(HERE, 'test-cost-baseline.json');

const HARD = 1.5; // 余量 < 33% → 红
const SAFE = 2.0; // 上限应 ≥ 2 × 满载

function main() {
  const gate = process.argv.includes('--gate');
  const baseline = JSON.parse(readFileSync(BASELINE, 'utf8'));

  const limits = new Map();
  for (const file of walkSpecs(resolve(FRONTEND, 'tests'))) {
    const rel = relative(resolve(FRONTEND, 'tests'), file).replace(/\\/g, '/');
    limits.set(rel, collectLimits(file));
  }

  const rows = [];
  const missing = [];
  for (const entry of baseline.entries) {
    const hit = limits.get(entry.file)?.get(entry.test);
    if (!hit) {
      missing.push(entry);
      continue;
    }
    const ratio = hit.cap / entry.loadedMs;
    rows.push({
      ...entry,
      cap: hit.cap,
      explicit: hit.explicit,
      ratio: Math.round(ratio * 100) / 100,
      reservePct: Math.round((1 - entry.loadedMs / hit.cap) * 1000) / 10,
      level: ratio < HARD ? 'red' : ratio < SAFE ? 'yellow' : 'ok',
    });
  }

  // 第二条判据（可算的那条）：单文件串行 > 500ms 的用例**必须**有显式上限。
  // 500ms × 6（实测的并行放大下界）= 3.0s，已经压到默认 5000ms 的 2× 安全线以下——
  // 也就是说，"单跑超过半秒"就是"默认上限迟早不够"的最早可观测信号，不必等全量翻车。
  const unbounded = rows.filter((r) => r.soloMs != null && r.soloMs >= 500 && !r.explicit);

  const red = rows.filter((r) => r.level === 'red');
  const yellow = rows.filter((r) => r.level === 'yellow');

  console.log(`基线：${baseline.entries.length} 条（实测于 ${baseline.measuredAt?.slice(0, 19).replace('T', ' ')}，` +
    `${baseline.measuredUnder || '全量并行'}）`);
  console.log(`机器：${baseline.machine?.cores} 逻辑核，测量期 CPU 均值 ${baseline.machine?.cpuMeanPct}%`);
  console.log('');

  const show = (title, list) => {
    if (!list.length) return;
    console.log(`${title}（${list.length} 条）`);
    for (const r of list) {
      console.log(`  ${r.level === 'red' ? '✗' : '·'} ${r.file} › ${r.test}`);
      console.log(`      实测满载 ${r.loadedMs}ms ／ 上限 ${r.cap}ms${r.explicit ? '' : '（默认 5000）'} ／ ` +
        `余量 ${r.reservePct}%（${r.ratio}×）`);
    }
    console.log('');
  };

  show('无余量：上限不到满载的 1.5 倍（余量 < 33%）', red);
  show('偏紧：上限不到满载的 2 倍（CI runner 慢 1.3–2× 就会过线）', yellow);

  if (unbounded.length) {
    console.log(`裸奔：单文件串行已 ≥ 500ms，却还在默认 5000ms 上（${unbounded.length} 条）`);
    for (const r of unbounded) {
      console.log(`  ✗ ${r.file} › ${r.test}`);
      console.log(`      单跑 ${r.soloMs}ms ／ 满载 ${r.loadedMs}ms ／ 上限 ${r.cap}ms（默认）`);
    }
    console.log('');
  }

  if (missing.length) {
    console.log(`基线里这 ${missing.length} 条在测试文件里找不到了（改名/删除/挪了 describe？）：`);
    for (const m of missing.slice(0, 12)) console.log(`  ? ${m.file} › ${m.test}`);
    if (missing.length > 12) console.log(`  …还有 ${missing.length - 12} 条`);
    console.log('  → 重跑 `npm run test:cost` 重建基线（scripts/test-cost-baseline.json）。');
    console.log('');
  }

  const ok = rows.filter((r) => r.level === 'ok').length;
  console.log(`合计：${ok}/${rows.length} 条落在 [2×, +∞) 的安全带里。`);
  if (!red.length && !yellow.length && !unbounded.length) console.log('没有一条用例的余量低于安全线。');

  if (gate && (red.length || unbounded.length)) {
    console.error('');
    console.error('闸门失败：上面 ✗ 那几条在满载下没有余量（或压根没写上限）。');
    console.error('按判据逐条处置（加宽 / 减负载 / 条件跳过，见 docs/TEST-LOAD-SENSITIVITY.md §3），');
    console.error('改完重跑 `npm run test:cost` 更新基线。');
    process.exit(1);
  }
}

main();

