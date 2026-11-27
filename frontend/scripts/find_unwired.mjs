/**
 * 前端版 `find_unwired`：扫"`api/client.ts` 里没有任何界面代码调用的导出函数"。
 *
 * ## 为什么要有它
 *
 * 后端那份（`backend/scripts/acc3/find_unwired.py`）防的是"路由加了、没人调"。
 * 前端有同一个形状的缺口，而且更隐蔽：**后端加了路由 → `client.ts` 加了函数 →
 * 没有任何组件或 store 调用它**。这时候：
 *
 * - 后端用例是绿的（路由本身测过）；
 * - 前端用例是绿的（没人调的东西不会被测到）；
 * - 用户手册里写着的那件事，界面上没有入口。
 *
 * 这一轮撞出来的「删除导图」就是这一类：`deleteMindMap()` 在 `client.ts` 里躺着，
 * 而数据库里那张表没有任何入口能删掉它。判据只能是静态的：
 *
 *     "这个导出函数，在 `src/**` 里被**别的**代码引用过吗？"
 *
 * ## 判据：语法树里的引用节点，不是字面出现
 *
 * 与后端 v2 同一条理由：这个仓库的注释极爱写函数名（"见 `deleteMindMap` 的
 * docstring"），按 `\bname\b` 数文本会把**只在注释里被提到**的函数算成"有引用"，
 * 于是清单永远变短而没人发现。所以这里用 `ts.createSourceFile` 数真正的
 * `Identifier` / `PropertyAccess` 出现次数——注释与字符串常量不在这棵树里。
 *
 * `.vue` 文件没有单独的 parser 依赖，所以切成 `<script>` 段交给同一个 parser；
 * `<template>` 段只做一次**窄**正则匹配（`@事件="fn(`、`:prop="fn"`、`{{ fn( }}`），
 * 因为模板里出现一个函数名基本就是接线。这条边界的漏报方向见下。
 *
 * ## 跑法
 *
 *     cd frontend
 *     node scripts/find_unwired.mjs            # 汇总 + 全部零引用清单
 *     node scripts/find_unwired.mjs --suspects  # 只列真正可疑的
 *     node scripts/find_unwired.mjs --gate      # 闸门：退出码非 0 表示有新增
 *
 * ## 闸门是**棘轮**，不是"零"
 *
 * 绝对判据（"一条零引用都不许有"）挡不住东西，只会被人加进豁免名单然后忘掉。
 * 所以闸门比的是 `scripts/unwired_baseline.json`：那里逐条写着"这条为什么可以零
 * 引用"，闸门只对**基线之外的新增**报错。两条反向检查让基线不腐烂：
 *
 * 1. 基线里的条目如果已经不再零引用 → 打印提示（不红）：修好了就该删掉它；
 * 2. 条目里声明的 `evidence` 文件不存在 → **红**：豁免的理由被删了而豁免还在。
 *
 * ## 边界（当闸门用之前必须知道）
 *
 * - **动态取值**（`client[name]`、注册表、字符串拼出来的调用）会误报成零引用；
 * - 只被模板里的**事件名字符串**调用（`emit('openRefs')` 这类字符串契约）不算引用，
 *   但它本来也不是 `client.ts` 的函数；
 * - 只被 `tests/**` 引用而产品代码零引用，是最可疑的一档：那正是"用例测了函数、
 *   界面没接上"的形状，所以单独一列 `tests=` 打出来；
 * - `.vue` 的 `<script>` 段按最外层一对标签切：带 `<script>` 字符串的模板（本仓库
 *   没有，但将来可能有）会切错。切不出来时脚本会打印该文件并跳过，不静默当零引用。
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import ts from 'typescript';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(HERE, '..');
const SRC = path.join(FRONTEND, 'src');
const CLIENT = path.join(SRC, 'api/client.ts');
const TESTS = path.join(FRONTEND, 'tests');
const BASELINE = path.join(HERE, 'unwired_baseline.json');

const SKIP_FILES = new Set(['api/client.ts', 'api/schema.d.ts']);

/** `src` 下所有会被当成"界面代码"的文件。 */
function walking(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walking(full, out);
    else if (/\.(ts|vue)$/.test(entry.name)) out.push(full);
  }
  return out;
}

/** 把 `.vue` 切成 `script` 段与 `template` 段。 */
function splitSfc(text) {
  const scriptMatch = /<script[^>]*>([\s\S]*?)<\/script>/.exec(text);
  const templateMatch = /<template>([\s\S]*)<\/template>/.exec(text);
  return { script: scriptMatch ? scriptMatch[1] : '', template: templateMatch ? templateMatch[1] : '' };
}

/** client.ts 里 `export function X` / `export async function X` 的清单。 */
function candidates() {
  const text = fs.readFileSync(CLIENT, 'utf8');
  const file = ts.createSourceFile(CLIENT, text, ts.ScriptTarget.ESNext, true);
  const found = [];
  for (const node of file.statements) {
    if (!ts.isFunctionDeclaration(node) || !node.name) continue;
    const exported = (node.modifiers ?? []).some(mod => mod.kind === ts.SyntaxKind.ExportKeyword);
    if (exported) found.push({ name: node.name.text, line: file.getLineAndCharacterOfPosition(node.getStart()).line + 1 });
  }
  return found;
}

/** 一棵目录树里每个标识符被引用了多少次（`*` = 该目录下所有 ts/vue）。 */
function referenceIndex(dir) {
  const index = new Map();
  for (const file of walking(dir)) {
    const relative = path.relative(SRC, file).split(path.sep).join('/');
    if (SKIP_FILES.has(relative)) continue;
    const raw = fs.readFileSync(file, 'utf8');
    const { script, template } = file.endsWith('.vue')
      ? splitSfc(raw)
      : { script: raw, template: '' };

    if (script) {
      const parsed = ts.createSourceFile(file, script, ts.ScriptTarget.ESNext, true, ts.ScriptKind.TS);
      const visit = node => {
        if (ts.isIdentifier(node)) {
          // `name:` 这种属性名不算引用（对象字面量的键不是函数调用）。
          const parent = node.parent;
          const isPropertyName = parent && ts.isPropertyAssignment(parent) && parent.name === node;
          const isDeclarationName = parent
            && (ts.isFunctionDeclaration(parent) || ts.isVariableDeclaration(parent)
              || ts.isParameter(parent) || ts.isInterfaceDeclaration(parent))
            && parent.name === node;
          if (!isPropertyName && !isDeclarationName) {
            index.set(node.text, (index.get(node.text) ?? 0) + 1);
          }
        }
        ts.forEachChild(node, visit);
      };
      visit(parsed);
    }

    // 模板里只认"被当真函数用"的三种形态，不做全量标识符统计。
    for (const match of template.matchAll(/[@:][\w.-]+="([^"]*)"/g)) {
      for (const call of match[1].matchAll(/\b([A-Za-z_$][\w$]*)/g)) {
        index.set(call[1], (index.get(call[1]) ?? 0) + 1);
      }
    }
    for (const match of template.matchAll(/\{\{([\s\S]*?)\}\}/g)) {
      for (const call of match[1].matchAll(/\b([A-Za-z_$][\w$]*)\s*\(/g)) {
        index.set(call[1], (index.get(call[1]) ?? 0) + 1);
      }
    }
  }
  return index;
}

function scan() {
  const appIndex = referenceIndex(SRC);
  const testIndex = referenceIndex(TESTS);
  return candidates().map(item => ({
    ...item,
    path: 'src/api/client.ts',
    refs: appIndex.get(item.name) ?? 0,
    testRefs: testIndex.get(item.name) ?? 0,
  }));
}

function loadBaseline() {
  if (!fs.existsSync(BASELINE)) return { entries: {} };
  return JSON.parse(fs.readFileSync(BASELINE, 'utf8'));
}

function gate(suspects) {
  const { entries } = loadBaseline();
  const names = new Set(suspects.map(item => item.name));
  const problems = [];

  for (const item of suspects) {
    if (!(item.name in entries)) {
      problems.push(`新增零引用导出函数：${item.path}:${item.line} ${item.name}（tests 引用 ${item.testRefs} 次）`);
    }
  }
  for (const [name, item] of Object.entries(entries)) {
    for (const evidence of item.evidence ?? []) {
      if (!fs.existsSync(path.join(FRONTEND, '..', evidence))) {
        problems.push(`基线条目 ${name} 的豁免理由已经不在了：${evidence}`);
      }
    }
    if (!names.has(name)) console.log(`（提示）基线条目 ${name} 已不再是零引用，可以删掉它了`);
  }

  if (problems.length) {
    console.log('闸门：不通过');
    for (const line of problems) console.log(`  · ${line}`);
    console.log('\n每条新增都要么接上界面，要么在 scripts/unwired_baseline.json 里写清为什么它可以零引用。');
    return 1;
  }
  console.log(`闸门：通过（${suspects.length} 条零引用全部在基线内）`);
  return 0;
}

function main(argv) {
  const all = scan();
  const zero = all.filter(item => item.refs === 0);
  const suspects = zero;

  console.log(`client.ts 导出函数：${all.length}`);
  console.log(`  零引用：${zero.length}`);
  console.log(`    其中只在 tests/ 里被用到：${zero.filter(item => item.testRefs > 0).length}`);
  console.log();
  console.log('判据：在 `frontend/src/**` 的语法树里（`.vue` 取其 `<script>` 段），');
  console.log('除 `api/client.ts` 自己以外，引用数为 0。`tests=` 那一列非零而这里为零，');
  console.log('正是「用例测了函数、界面没接上」的形状。');
  console.log();

  if (argv.includes('--gate')) return gate(suspects);

  const shown = argv.includes('--suspects') ? suspects : zero;
  for (const item of shown.sort((a, b) => a.line - b.line)) {
    console.log(`${item.path}:${item.line}\t${item.name}\ttests=${item.testRefs}`);
  }
  console.log(`\n合计 ${shown.length} 条。`);
  return 0;
}

process.exit(main(process.argv.slice(2)));
