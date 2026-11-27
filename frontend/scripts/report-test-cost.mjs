// 把两份 measure-test-cost 快照拼成一张可复现的表：每条用例的空闲 / 满载耗时、
// 倍数、**现有上限**与**满载余量**。
//
// 上限不是猜的：静态解析 `frontend/tests/**/*.spec.ts` 里 `describe/it/test` 的第二个参数
// （`{ timeout: N }`，支持 `N` 为本文件内的数字常量），没写的按 vitest 默认 5000ms。
// 解析方式与 `scripts/find_unwired.mjs` 一致：`typescript` 的 `createSourceFile`，不联网、
// 不跑测试、只读文本。
//
// 用法：
//   node scripts/report-test-cost.mjs --idle scripts/_test-cost-idle.json \
//                                    --loaded scripts/_test-cost-loaded.json \
//                                    --md docs/TEST-LOAD-SENSITIVITY-TABLE.md
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DEFAULT_TIMEOUT, collectLimits, walkSpecs } from './test-cost-limits.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND = resolve(HERE, '..');
const RESERVE_DANGER = 0.3; // 满载余量低于 30% 视为风险

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    if (key === '--idle') out.idle = argv[++i];
    else if (key === '--loaded') out.loaded = argv[++i];
    else if (key === '--solo') out.solo = argv[++i];
    else if (key === '--md') out.md = argv[++i];
    else if (key === '--baseline') out.baseline = argv[++i];
  }
  return out;
}

function pct(values, q) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  return Math.round(sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))] * 100) / 100;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.idle || !args.loaded) throw new Error('需要 --idle 与 --loaded 两份快照');
  const idle = JSON.parse(readFileSync(resolve(FRONTEND, args.idle), 'utf8'));
  const loaded = JSON.parse(readFileSync(resolve(FRONTEND, args.loaded), 'utf8'));

  const limitsByFile = new Map();
  for (const file of walkSpecs(resolve(FRONTEND, 'tests'))) {
    const rel = relative(resolve(FRONTEND, 'tests'), file).replace(/\\/g, '/');
    limitsByFile.set(rel, collectLimits(file));
  }

  const index = new Map(loaded.tests.map((t) => [`${t.file}::${t.full}`, t]));
  const solo = args.solo ? JSON.parse(readFileSync(resolve(FRONTEND, args.solo), 'utf8')) : null;
  const soloIndex = new Map((solo?.tests ?? []).map((t) => [`${t.file}::${t.full}`, t]));
  const rows = [];
  for (const t of idle.tests) {
    const other = index.get(`${t.file}::${t.full}`);
    if (!other) continue;
    const limit = limitsByFile.get(t.file)?.get(t.full) ?? null;
    const cap = limit?.cap ?? DEFAULT_TIMEOUT;
    const soloMs = soloIndex.get(`${t.file}::${t.full}`)?.ms ?? null;
    rows.push({
      file: t.file,
      name: t.full,
      idle: t.ms,
      loaded: other.ms,
      ratio: t.ms > 0 ? Math.round((other.ms / t.ms) * 100) / 100 : null,
      solo: soloMs,
      // 相对"单文件串行"的倍数：这是放大来自哪一层的判据（见文档 §3）
      soloRatio: soloMs && soloMs > 0 ? Math.round((other.ms / soloMs) * 100) / 100 : null,
      cap,
      // 上限来自静态解析还是默认值（后者是"裸奔在默认线上"的用例）
      explicit: limit?.explicit ?? false,
      reservePct: Math.round(((cap - other.ms) / cap) * 1000) / 10,
      status: other.status,
    });
  }

  const byRatio = [...rows].sort((a, b) => (b.ratio ?? 0) - (a.ratio ?? 0));
  const byLoaded = [...rows].sort((a, b) => b.loaded - a.loaded);
  const ratios = rows.map((r) => r.ratio ?? 0);
  const risky = rows.filter((r) => r.reservePct < RESERVE_DANGER * 100).sort((a, b) => a.reservePct - b.reservePct);
  const buckets = [
    ['< 1.5×', (r) => r < 1.5],
    ['1.5–3×', (r) => r >= 1.5 && r < 3],
    ['3–10×', (r) => r >= 3 && r < 10],
    ['10–30×', (r) => r >= 10 && r < 30],
    ['≥ 30×', (r) => r >= 30],
  ].map(([label, test]) => ({ label, n: ratios.filter(test).length }));

  const fmt = (n) => `${Math.round(n * 10) / 10}`;
  const hasSolo = Boolean(solo);
  const head = hasSolo
    ? '| 用例 | 单跑 | 空闲全量 | 满载全量 | 倍数（满载/空闲） | 现有上限 | 满载余量 |'
    : '| 用例 | 空闲 | 满载 | 倍数 | 现有上限 | 满载余量 |';
  const sep = hasSolo ? '|---|---|---|---|---|---|---|' : '|---|---|---|---|---|---|';
  const line = (r) => (hasSolo
    ? `| \`${r.file}\` › ${r.name} | ${r.solo == null ? '—' : `${fmt(r.solo)}ms`} | ${fmt(r.idle)}ms | ` +
      `${fmt(r.loaded)}ms | ${r.ratio}× | ${r.cap}${r.explicit ? '' : '（默认）'} | ${r.reservePct}% |`
    : `| \`${r.file}\` › ${r.name} | ${fmt(r.idle)}ms | ${fmt(r.loaded)}ms | ${r.ratio}× | ` +
      `${r.cap}${r.explicit ? '' : '（默认）'} | ${r.reservePct}% |`);

  const L = [];
  L.push('# 负载敏感性实测表（脚本生成，勿手改）');
  L.push('');
  L.push(`> 生成：\`node scripts/report-test-cost.mjs --idle ${args.idle} --loaded ${args.loaded}${hasSolo ? ` --solo ${args.solo}` : ''}\``);
  L.push('> 口径：同一份代码、同一台机器、同一套用例跑多次；唯一变量是机器上有没有别的重活。');
  L.push('');
  L.push('## 各轮的机器状态');
  L.push('');
  L.push('| 轮次 | 时间 | 外部负载 | 开跑前 CPU | 全程 CPU 均值 / 峰值 | 墙钟 | 结果 |');
  L.push('|---|---|---|---|---|---|---|');
  for (const s of [idle, loaded, ...(solo ? [solo] : [])]) {
    L.push(
      `| ${s.label} | ${s.startedAt.replace('T', ' ').slice(0, 19)} | ${s.note || '—'} | ` +
      `${s.cpu.baselinePct}% | ${s.cpu.mean}% / ${s.cpu.max}% | ${(s.wallMs / 1000).toFixed(1)}s | ` +
      `${s.totals.passed}/${s.totals.tests} 通过，${s.totals.failed} 失败 |`,
    );
  }
  L.push('');
  L.push('## 倍数分布（满载 ÷ 空闲，两轮都是全量）');
  L.push('');
  L.push(`共 ${rows.length} 条（两轮都记到的用例）。中位 **${pct(ratios, 0.5)}×**、p75 ${pct(ratios, 0.75)}×、` +
    `p90 **${pct(ratios, 0.9)}×**、p99 ${pct(ratios, 0.99)}×、最大 **${pct(ratios, 1)}×**。`);
  L.push('');
  L.push('| 倍数区间 | 条数 | 占比 |');
  L.push('|---|---|---|');
  for (const b of buckets) {
    L.push(`| ${b.label} | ${b.n} | ${((b.n / rows.length) * 100).toFixed(1)}% |`);
  }
  L.push('');
  L.push(`## 表：按倍数降序（前 60 条；关注集见文末 CSV，全 1462 条重跑脚本即可）`);
  L.push('');
  L.push(head);
  L.push(sep);
  for (const r of byRatio.slice(0, 60)) L.push(line(r));
  L.push('');
  L.push(`## 风险清单：满载余量 < ${RESERVE_DANGER * 100}%（按余量升序，${risky.length} 条）`);
  L.push('');
  if (!risky.length) {
    L.push('没有一条满载余量低于 30%。');
  } else {
    L.push(head);
    L.push(sep);
    for (const r of risky) L.push(line(r));
  }
  L.push('');
  L.push('## 满载耗时 top 20（绝对值；与"倍数榜"未必同一批）');
  L.push('');
  L.push(head);
  L.push(sep);
  for (const r of byLoaded.slice(0, 20)) L.push(line(r));
  if (hasSolo) {
    const amplified = byLoaded.slice(0, 20).filter((r) => r.soloRatio != null);
    const soloRatios = amplified.map((r) => r.soloRatio);
    L.push('');
    L.push('## 放大来自哪一层：单跑 → 全量（同一批 top 20）');
    L.push('');
    L.push(`这 20 条里，**单跑 → 全量满载** 的中位放大 **${pct(soloRatios, 0.5)}×**、最大 **${pct(soloRatios, 1)}×**；` +
      `而 **空闲 → 满载**（外部负载那一层）的中位是 **${pct(amplified.map((r) => r.ratio ?? 0), 0.5)}×**。`);
    L.push('');
    L.push('| 用例 | 单跑 | 全量空白 | 全量满载 | 全量 ÷ 单跑 | 满载 ÷ 空闲 |');
    L.push('|---|---|---|---|---|---|');
    for (const r of amplified) {
      L.push(`| \`${r.file}\` › ${r.name} | ${fmt(r.solo)}ms | ${fmt(r.idle)}ms | ${fmt(r.loaded)}ms | ` +
        `${r.soloRatio}× | ${r.ratio}× |`);
    }
  }
  L.push('');
  L.push('## 关注集（CSV）：满载 ≥ 100ms 或写了显式上限的用例');
  L.push('');
  L.push('> 全 1462 条不落库（224KB 的表没人会读）：重跑上面的命令即可得到完整版。');
  L.push('');
  L.push('```csv');
  L.push(`file,test,solo_ms,idle_ms,loaded_ms,ratio,cap_ms,explicit_cap,reserve_pct`);
  for (const r of byLoaded.filter((row) => row.loaded >= 100 || row.explicit)) {
    L.push([
      r.file, `"${r.name.replace(/"/g, '""')}"`,
      r.solo == null ? '' : r.solo, r.idle, r.loaded, r.ratio, r.cap,
      r.explicit ? 'yes' : 'no', r.reservePct,
    ].join(','));
  }
  L.push('```');
  L.push('');

  if (args.baseline) {
    // 只落"值得被闸门盯住"的条目：满载 ≥ 300ms，或本来就写了显式上限。
    // 1462 条全落库会让基线变成一份没人看的日志，闸门也就没人维护。
    // 另外跳过静态解析不到上限的用例（`it.each` 的模板串标题）：闸门读不出它们的上限，
    // 留在基线里只会变成一条永远消不掉的"找不到"。
    const entries = byLoaded
      .filter((r) => (r.loaded >= 300 || r.explicit) && limitsByFile.get(r.file)?.has(r.name))
      .map((r) => ({
        file: r.file,
        test: r.name,
        loadedMs: r.loaded,
        idleMs: r.idle,
        soloMs: r.solo,
        capMs: r.cap,
      }));
    const baseline = {
      note: '由 scripts/report-test-cost.mjs --baseline 生成；改了测试成本就重跑 `npm run test:cost`',
      measuredAt: loaded.startedAt,
      measuredUnder: loaded.note || loaded.label,
      machine: { cores: loaded.cpu.cores, cpuMeanPct: loaded.cpu.mean },
      entries,
    };
    writeFileSync(resolve(FRONTEND, args.baseline), `${JSON.stringify(baseline, null, 2)}\n`, 'utf8');
    console.log(`—— 基线写入 ${args.baseline}（${entries.length} 条）`);
  }

  if (args.md) {
    writeFileSync(resolve(FRONTEND, '..', args.md), `${L.join('\n')}\n`, 'utf8');
  }
  console.log(L.slice(0, 40).join('\n'));
  console.log(`\n—— 完整表写入 ${args.md ?? '(未指定 --md)'}`);
  console.log(`条数 ${rows.length}｜中位 ${pct(ratios, 0.5)}×｜p90 ${pct(ratios, 0.9)}×｜最大 ${pct(ratios, 1)}×｜超 10× ${ratios.filter((r) => r > 10).length} 条｜风险 ${risky.length} 条`);
}

main();
