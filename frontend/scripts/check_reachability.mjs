/**
 * 组件可达性查法：**函数被调用了，但那个组件从入口到不了**。
 *
 * ## 三档查法的最后一环
 *
 *     后端函数零引用                      → backend/scripts/acc3/find_unwired.py
 *     client.ts 函数零调用                → frontend/scripts/find_unwired.mjs
 *     后端有路由、client 一行都没有        → frontend/scripts/check_route_coverage.mjs
 *     函数被调用了，但那个组件从入口到不了  → 【这个脚本】
 *
 * 前三档都从"有没有人引用"这一侧出发，所以共同缺一层：**引用链在某个没接上的地方
 * 就断了**。一个 `.vue` 里写着完整界面（按钮、事件、调 `client` 函数），它只要没被
 * 任何可达组件 `import`，用户就永远看不到；而 `find_unwired.mjs` 会因为那个 client
 * 函数"被调用了"而全绿。今天撞过的五处缺口共同的形态就是这一层：**代码全在、
 * 调用链全通、就是没有入口**。
 *
 * ## 判据：import 图上的 BFS，只沿"值边"走
 *
 * 1. `@vue/compiler-sfc` 的 `parse()` 切出每个 `.vue` 的 `<script>` / `<script setup>`
 *    段（本仓库 23 个 `.vue` 全是 `<script setup>`，没有选项式 `components: {}`），
 *    交给 `ts.createSourceFile`；`.ts` 直接解析。
 * 2. 每个模块的出边来自三种写法：`import … from '…'`（静态）、`import('…')`
 *    （动态，`defineAsyncComponent(() => import(…))` 的那个 `import()` 也在其中——
 *    它就是一个 `CallExpression`，不需要单独认 `defineAsyncComponent`）、
 *    `import '…'`（副作用导入）。
 * 3. 从 `src/main.ts` 做 BFS：`index.html` 的 `<script type="module">` 指向它，
 *    `App.vue` 由它挂载。`--root=app` 换成只从 `App.vue` 起，用来对照读数。
 * 4. 走不到的 `.ts` / `.vue` 就是不可达，再分五档。
 *
 * **为什么只沿值边走**：`import type { X } from './y'` 编译后整条语句被删掉，浏览器
 * 根本不会加载 `./y`。本仓库 `verbatimModuleSyntax: true`，类型导入必须显式写
 * `type`（写漏了 `vue-tsc` 会红），所以"这条边是类型边"在这里是可判的，不是猜的。
 *
 * ## 五档（分级是这道闸能不能用的关键）
 *
 * | 档 | 含义 | 判据 | 处理 |
 * |---|---|---|---|
 * | `a` | 真不可达：一个完整界面组件，谁都不 import | `.vue` + 模板里有事件绑定/`data-test`/`v-model`，或 script 里有 `emit`/`props`/调 `client` | **该报**：接上入口 |
 * | `b` | 只经动态 import 可达 | 唯一入边是 `import()` | 不是不可达（单列） |
 * | `c` | 纯类型 | `.d.ts`，或文件里只有 `interface`/`type`/`declare`/`import type` | 不参与组件可达性 |
 * | `d` | 测试辅助 | 只在 `tests/**` 里被 import | 不算（`.vue` 会带 ☆，见下） |
 * | `e` | 死代码 | 有运行时代码、谁都不 import、无界面痕迹 | **该报**：删掉 |
 *
 * `a` 与 `e` 的区别不是"该不该看"，而是**下一步动作不同**：`a` 是接上入口，`e` 是
 * 删掉。脚本给的是**建议档**（启发式，见 `interfaceTraces`），最终档写在基线里。
 *
 * `d` 里的 `.vue` 会打一个 ☆：一个完整界面组件**只在 tests 里被 import、产品代码
 * 没人 import**，是"写了界面忘接"最隐蔽的形态——`find_unwired.mjs` 里 `tests=` 非零
 * 而 `refs=` 为零正是同一个形状。所以它虽然按 `d` 豁免，报告里必须看得见。
 *
 * ## 跑法
 *
 *     cd frontend
 *     node scripts/check_reachability.mjs             # 汇总 + 不可达逐条（带建议档）
 *     node scripts/check_reachability.mjs --root=app  # 只从 App.vue 起（对照）
 *     node scripts/check_reachability.mjs --gate      # 闸门：退出码非 0 表示有新增
 *     node scripts/check_reachability.mjs --json      # 机器可读的读数
 *
 * ## 闸门是棘轮，与前三道同一套语义
 *
 * 基线 `scripts/reachability_baseline.json` 的 `entries` 逐条写 `.vue`/`.ts` 的路径、
 * 档、为什么它可以不可达。`a` 类条目额外要 `ack`：**a 是真缺口，写进基线等于承认
 * "这个界面此刻真的看不见"，署名比理由更该留下**。三条反向检查防基线腐烂：
 *
 * 1. 条目的 `evidence` 文件不存在 → **红**（豁免的理由没了而豁免还在）；
 * 2. 条目现在可达了 → 提示（不红），修好了就该删掉它；
 * 3. 档不在 `a`–`e` 里 → 红（防止手写错一个字母就静默降级）。
 *
 * ## 不覆盖什么（这是静态可达性的根本局限，写在这里而不是留在口头上）
 *
 * 它算的是 **import 图**，不是**渲染条件**。一个组件被 `import` 了、在父组件里被
 * `<Foo v-if="永远为假" />` 包着，或者按钮 `:disabled="true"`、`v-show` 在折叠区里
 * 从未展开——这些在这张图上一律"可达"。判据只能证明"打包器会包含它、运行时会创建
 * 它的 vnode 树的那一支有机会执行到"，不能证明"用户点得到"。
 *
 * 补这一半要另一条判据：读模板 AST 里 `v-if`/`v-show`/`:disabled` 的条件表达式，
 * 看它是不是字面常量、或绑在某个 store 开关上，再对着那个开关的初始值判。这条路径
 * 的代价与边界见 `docs/REACHABILITY.md` 的最后一节。
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { parse as parseSfc } from '@vue/compiler-sfc';
import ts from 'typescript';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(HERE, '..');
const SRC = path.join(FRONTEND, 'src');
const TESTS = path.join(FRONTEND, 'tests');
const BASELINE = path.join(HERE, 'reachability_baseline.json');

/** 入口：`index.html` 里 `<script type="module" src="/src/main.ts">`。 */
const ROOT_MAIN = 'src/main.ts';
/** 对照根：`App.vue` 由 main.ts 挂载；只从它起会漏掉入口级 import 的模块。 */
const ROOT_APP = 'src/App.vue';

/** 解析相对 import 时依次试的后缀。`.json` / `.css` 不是这条图的节点。 */
const SUFFIXES = ['', '.ts', '.d.ts', '.vue', '/index.ts', '/index.vue', '/index.d.ts'];
const NON_CODE = /\.(json|css|svg|png|jpg|jpeg|gif|webp|woff2?|ttf|mp3|wav)$/;
const CLASSES = new Set(['a', 'b', 'c', 'd', 'e']);

const rel = file => path.relative(FRONTEND, file).split(path.sep).join('/');

/** `src` 下所有会被当成节点的文件（`.d.ts` 也算，它自带 `c` 档判据）。 */
function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, out);
    else if (/\.(ts|vue)$/.test(entry.name)) out.push(full);
  }
  return out;
}

/**
 * 一条 import 边是值边还是类型边。
 *
 * - `import './x'`（没有 importClause）→ 值边（副作用导入）；
 * - `import type { A } from './x'` → 类型边；
 * - `import { type A } from './x'`（全部 specifier 都带 `type` 且没有默认导入）→ 类型边；
 * - 其余（默认导入、普通具名导入、混合）→ 值边。
 */
function edgeKind(node) {
  const clause = node.importClause;
  if (!clause) return 'value';
  if (clause.isTypeOnly) return 'type';
  const bindings = clause.namedBindings;
  if (!clause.name && bindings && ts.isNamedImports(bindings) && bindings.elements.length) {
    if (bindings.elements.every(element => element.isTypeOnly)) return 'type';
  }
  return 'value';
}

function lineOf(file, node) {
  return file.getLineAndCharacterOfPosition(node.getStart()).line + 1;
}

/** 语法树里所有 import 边（静态 / 类型 / 动态）与"用了动态组件"的痕迹。 */
function collectImports(fileName, source, template) {
  const file = ts.createSourceFile(fileName, source, ts.ScriptTarget.ESNext, true, ts.ScriptKind.TS);
  const edges = [];
  const dynamicTargets = [];
  let usesAsyncComponent = false;

  const visit = (node) => {
    if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
      edges.push({
        spec: node.moduleSpecifier.text,
        kind: edgeKind(node),
        line: lineOf(file, node),
      });
    } else if (ts.isExportDeclaration(node) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
      // `export * from './x'` / `export { y } from './x'`：转发也是值边。
      edges.push({
        spec: node.moduleSpecifier.text,
        kind: node.isTypeOnly ? 'type' : 'value',
        line: lineOf(file, node),
      });
    } else if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) {
      const arg = node.arguments[0];
      if (arg && ts.isStringLiteral(arg)) {
        dynamicTargets.push({ spec: arg.text, line: lineOf(file, node) });
        edges.push({ spec: arg.text, kind: 'dynamic', line: lineOf(file, node) });
      }
    } else if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)
      && node.expression.text === 'defineAsyncComponent') {
      usesAsyncComponent = true;
    }
    ts.forEachChild(node, visit);
  };
  visit(file);

  return { edges, dynamicTargets, usesAsyncComponent, template };
}

/** 文件里除了类型声明什么都没有（编译成空模块）。 */
function isTypeOnly(source) {
  const file = ts.createSourceFile('probe.ts', source, ts.ScriptTarget.ESNext, true, ts.ScriptKind.TS);
  if (!file.statements.length) return true;
  for (const statement of file.statements) {
    if (ts.isInterfaceDeclaration(statement) || ts.isTypeAliasDeclaration(statement)) continue;
    if (ts.isImportDeclaration(statement)) {
      if (statement.importClause && statement.importClause.isTypeOnly) continue;
      return false;
    }
    if (ts.isExportDeclaration(statement) && statement.isTypeOnly) continue;
    if (ts.isModuleDeclaration(statement) && (statement.flags & ts.NodeFlags.Ambient)) continue;
    if (ts.isVariableStatement(statement) && (statement.declarationList.flags & ts.NodeFlags.Ambient)) continue;
    return false;
  }
  return true;
}

/**
 * 编译期断言文件：`export const checks = { … true satisfies SameKeys<A, B> … }`。
 *
 * 这类文件**故意不被任何模块 import**——`tsconfig.json` 的 `include` 会检查它，
 * 打包时会被摇掉，浏览器里不会有它。按"有没有运行时代码"数，它是运行时语句；
 * 按"用户能不能看到"判，它和不存在的区别只是一份类型断言。特征很硬：每个
 * `export const` 的初始化器都带 `satisfies`。本仓库只有 `api/contract-check.ts`。
 */
function isCompileTimeAssertions(source) {
  const file = ts.createSourceFile('probe.ts', source, ts.ScriptTarget.ESNext, true, ts.ScriptKind.TS);
  let assertions = 0;
  const isAssertion = (expression) => {
    if (!ts.isSatisfiesExpression(expression)) return false;
    assertions += 1;
    return true;
  };
  for (const statement of file.statements) {
    if (ts.isImportDeclaration(statement) || ts.isInterfaceDeclaration(statement)
      || ts.isTypeAliasDeclaration(statement)) continue;
    if (!ts.isVariableStatement(statement)) return false;
    const exported = (statement.modifiers ?? []).some(mod => mod.kind === ts.SyntaxKind.ExportKeyword);
    if (!exported) return false;
    for (const declaration of statement.declarationList.declarations) {
      const init = declaration.initializer;
      if (!init) return false;
      // `export const checks = { a: true satisfies SameKeys<A, B>, … }`
      if (ts.isObjectLiteralExpression(init)) {
        for (const property of init.properties) {
          if (!ts.isPropertyAssignment(property) || !isAssertion(property.initializer)) return false;
        }
        continue;
      }
      if (!isAssertion(init)) return false;
    }
  }
  return assertions > 0;
}

/** 界面的痕迹：用来给 `.vue` 建议 `a` 还是 `e`（启发式，最终档在基线里）。 */
function interfaceTraces(template, script) {
  const traces = [];
  if (/@[\w.-]+\s*=|v-on:/.test(template)) traces.push('模板里有事件绑定');
  if (/v-model/.test(template)) traces.push('模板里有 v-model');
  if (/data-test=/.test(template)) traces.push('模板里有 data-test 锚点');
  if (/\bdefineEmits\b|\bemit\s*\(/.test(script)) traces.push('script 里 emit 事件');
  if (/\bdefineProps\b/.test(script)) traces.push('script 里声明 props');
  if (/from\s+'[^']*api\/client'/.test(script)) traces.push('script 里 import 了 client');
  return traces;
}

/** 建立 `src/**` 的完整节点表与出边。 */
function buildGraph() {
  const nodes = new Map();
  for (const full of walk(SRC)) {
    const key = rel(full);
    const raw = fs.readFileSync(full, 'utf8');
    let script = raw;
    let template = '';
    let sfcError = null;

    if (full.endsWith('.vue')) {
      const parsed = parseSfc(raw, { filename: full });
      if (parsed.errors && parsed.errors.length) {
        // 解析失败时不能静默当"零出边"——那会把整个文件误报成不可达。
        sfcError = String(parsed.errors[0]?.message ?? parsed.errors[0]);
        script = '';
      } else {
        const parts = [];
        if (parsed.descriptor.script) parts.push(parsed.descriptor.script.content);
        if (parsed.descriptor.scriptSetup) parts.push(parsed.descriptor.scriptSetup.content);
        script = parts.join('\n');
        template = parsed.descriptor.template ? parsed.descriptor.template.content : '';
      }
    }

    const isDts = full.endsWith('.d.ts');
    const collected = sfcError
      ? { edges: [], dynamicTargets: [], usesAsyncComponent: false }
      : collectImports(full, script, template);

    nodes.set(key, {
      key,
      file: full,
      isVue: full.endsWith('.vue'),
      isDts,
      sfcError,
      script,
      template,
      edges: collected.edges,
      usesAsyncComponent: collected.usesAsyncComponent,
      typeOnly: isDts || (!full.endsWith('.vue') && !sfcError && isTypeOnly(script)),
      assertions: !full.endsWith('.vue') && !sfcError && isCompileTimeAssertions(script),
      traces: full.endsWith('.vue') && !sfcError ? interfaceTraces(template, script) : [],
    });
  }
  return nodes;
}

/** 相对 spec → 节点 key；外部包 → null；非代码资源 → 'asset'；解析不出 → undefined。 */
function resolveSpec(nodes, fromKey, spec) {
  if (NON_CODE.test(spec)) return 'asset';
  if (!spec.startsWith('.')) return null;
  const base = path.posix.normalize(path.posix.join(path.posix.dirname(fromKey), spec));
  for (const suffix of SUFFIXES) {
    const candidate = base + suffix;
    if (nodes.has(candidate)) return candidate;
  }
  return undefined;
}

/** 每条边落成目标 key，顺带记下解析不出的边（那是判据的洞，要能看见）。 */
function linkEdges(nodes) {
  const unresolved = [];
  for (const node of nodes.values()) {
    for (const edge of node.edges) {
      edge.target = resolveSpec(nodes, node.key, edge.spec);
      if (edge.target === undefined) {
        unresolved.push({ from: node.key, spec: edge.spec, line: edge.line });
        edge.target = null;
      }
    }
  }
  return unresolved;
}

/** 从某个根沿值边 + 动态边做 BFS。`dynamicOnly` 记"唯一入边是动态 import"的节点。 */
function reachableFrom(nodes, rootKey) {
  const seen = new Set([rootKey]);
  const dynamicOnly = new Map([[rootKey, false]]);
  const typeRefs = new Map();
  const queue = [rootKey];

  while (queue.length) {
    const key = queue.shift();
    const node = nodes.get(key);
    if (!node) continue;
    for (const edge of node.edges) {
      if (!edge.target || !nodes.has(edge.target)) continue;
      // 类型边在编译后整条消失，不构成运行时可达；只记下"谁按类型引用了它"。
      if (edge.kind === 'type') {
        if (!typeRefs.has(edge.target)) typeRefs.set(edge.target, new Set());
        typeRefs.get(edge.target).add(key);
        continue;
      }
      const dynamic = edge.kind === 'dynamic';
      if (!seen.has(edge.target)) {
        seen.add(edge.target);
        dynamicOnly.set(edge.target, dynamic);
        queue.push(edge.target);
        continue;
      }
      if (dynamicOnly.get(edge.target) === true && !dynamic) dynamicOnly.set(edge.target, false);
    }
  }
  return { seen, dynamicOnly, typeRefs };
}

/** `tests/**` 里 import 到的 `src` 模块集合（`d` 档的判据）。 */
function testImports(nodes) {
  const hits = new Set();
  if (!fs.existsSync(TESTS)) return hits;
  const files = [];
  const scan = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) scan(full);
      else if (/\.(ts|mts|vue)$/.test(entry.name)) files.push(full);
    }
  };
  scan(TESTS);
  for (const full of files) {
    const raw = fs.readFileSync(full, 'utf8');
    const script = full.endsWith('.vue') ? (parseSfc(raw).descriptor.scriptSetup?.content ?? '') : raw;
    const collected = collectImports(full, script, '');
    for (const edge of collected.edges) {
      const absolute = edge.spec.startsWith('.')
        ? path.resolve(path.dirname(full), edge.spec)
        : null;
      if (!absolute) continue;
      for (const suffix of SUFFIXES) {
        const candidate = absolute + suffix;
        const key = rel(candidate);
        if (nodes.has(key)) hits.add(key);
      }
    }
  }
  return hits;
}

let typeRefs = new Map();

/** 不可达节点的建议档（最终档以基线为准）。 */
function classify(node, { otherRoot, tests }) {
  if (node.typeOnly) {
    return { class: 'c', note: node.isDts ? '.d.ts，由 `npm run gen:api` 生成' : '文件里只有类型声明，编译成空模块' };
  }
  if (node.assertions) {
    return { class: 'c', note: '编译期断言文件（`satisfies`），故意不被 import，打包时被摇掉' };
  }

  if (tests.has(node.key)) {
    const suspicious = node.isVue && node.traces.length > 0;
    return {
      class: 'd',
      note: suspicious
        ? '☆ 只在 tests 里被 import，而它有完整界面——与"写了界面忘入口"同形'
        : '只在 tests 里被 import',
    };
  }

  if (otherRoot.has(node.key)) {
    // 从一个根走不到、从另一个根走得到：只被入口本身的挂载前逻辑 import。
    return { class: 'b', note: '只被入口 main.ts 的挂载前逻辑 import，从 App.vue 走不到' };
  }

  if (node.isVue) {
    if (node.traces.length) return { class: 'a', note: `完整界面（${node.traces.join('、')}）但谁都不 import` };
    return { class: 'e', note: '组件没有任何界面痕迹，也没有人 import' };
  }
  return { class: 'e', note: '有运行时代码，谁都不 import' };
}

function loadBaseline() {
  if (!fs.existsSync(BASELINE)) return { entries: {} };
  return JSON.parse(fs.readFileSync(BASELINE, 'utf8'));
}

function scan(rootKey) {
  const nodes = buildGraph();
  const unresolved = linkEdges(nodes);
  const tests = testImports(nodes);

  // 两个根都算：`main` 是 `index.html` 的入口（默认），`app` 只到 `<App>` 自己。
  // 两条读数的差集就是"只被入口挂载前逻辑 import"的模块。
  const mainReach = reachableFrom(nodes, ROOT_MAIN);
  const appReach = reachableFrom(nodes, ROOT_APP);
  const from = rootKey === ROOT_APP ? appReach : mainReach;
  const other = rootKey === ROOT_APP ? mainReach : appReach;

  const unreachable = [...nodes.values()].filter(node => !from.seen.has(node.key));
  const classified = unreachable
    .map(node => ({ ...node, ...classify(node, { otherRoot: other.seen, tests }) }))
    .sort((a, b) => a.key.localeCompare(b.key));

  // 每个 .vue 的入边：`a` 档读数是"0 条"，这个清单是它可被审计的依据。
  const inEdges = new Map();
  for (const node of nodes.values()) {
    for (const edge of node.edges) {
      if (!edge.target || edge.kind === 'type') continue;
      if (!inEdges.has(edge.target)) inEdges.set(edge.target, []);
      inEdges.get(edge.target).push(`${node.key}${edge.kind === 'dynamic' ? '（动态）' : ''}`);
    }
  }

  return {
    rootKey,
    nodes,
    unresolved,
    seen: from.seen,
    dynamicOnly: from.dynamicOnly,
    typeRefs: from.typeRefs,
    classified,
    inEdges,
    totals: {
      all: nodes.size,
      vue: [...nodes.values()].filter(n => n.isVue).length,
      ts: [...nodes.values()].filter(n => !n.isVue).length,
      reachable: from.seen.size,
      unreachable: classified.length,
      dynamicReachable: [...from.seen].filter(key => from.dynamicOnly.get(key) === true).length,
      reachableVue: [...from.seen].filter(key => nodes.get(key)?.isVue).length,
      rootMismatch: [...mainReach.seen].filter(key => key !== ROOT_MAIN && !appReach.seen.has(key)).length,
    },
  };
}

function report(result) {
  const { totals, classified, dynamicOnly, nodes, unresolved } = result;
  const baseline = loadBaseline();
  const byClass = (name) => classified.filter(node => node.class === name);

  console.log(`src/** 节点：${totals.all}（.vue ${totals.vue} / .ts ${totals.ts}）`);
  console.log(`根：${result.rootKey}（main 与 app 两条可达集的差：${totals.rootMismatch} 个模块）`);
  console.log(`  可达：${totals.reachable}（其中只经动态 import 可达：${totals.dynamicReachable}）`);
  console.log(`  可达的 .vue：${totals.reachableVue} / ${totals.vue}`);
  console.log(`  ★ 不可达：${totals.unreachable}`);
  console.log(`    a 真不可达（完整界面没人 import）：${byClass('a').length}`);
  console.log(`    b 只经另一个根可达：${byClass('b').length}`);
  console.log(`    c 纯类型 / 编译期断言：${byClass('c').length}`);
  console.log(`    d 只在 tests 里被 import：${byClass('d').length}`);
  console.log(`    e 死代码：${byClass('e').length}`);
  console.log();
  console.log('判据：`@vue/compiler-sfc` 切出 `<script>` / `<script setup>`，`ts.createSourceFile`');
  console.log('建 import 图，从入口沿**值边**（`import type` 编译后被删）做 BFS。');
  console.log('分档见脚本 docstring：a 该接入口，e 该删，c/d 豁免，b 单列。');
  console.log();

  if (classified.length) {
    console.log('不可达逐条（建议档 + 基线档）：');
    for (const node of classified) {
      const known = baseline.entries?.[node.key];
      const mark = known ? `[基线 ${known.class}]` : '[新增]';
      console.log(`  ${mark} ${node.class}  ${node.key}`);
      console.log(`      ${node.note}`);
      if (known?.reason) console.log(`      基线理由：${known.reason}`);
    }
    console.log();
  }

  const dyn = [...dynamicOnly.entries()].filter(([, isDynamic]) => isDynamic).map(([key]) => key);
  if (dyn.length) {
    console.log('只经动态 import 可达（b：打包器会单独切块，静态图上看不见那条边）：');
    for (const key of dyn) {
      const node = nodes.get(key);
      const asyncNote = node?.usesAsyncComponent ? '（该文件用了 defineAsyncComponent）' : '';
      console.log(`  ${key}${asyncNote}`);
    }
    console.log();
  }

  if (unresolved.length) {
    console.log('解析不出的相对 import（判据的洞，不是缺口的读数）：');
    for (const item of unresolved) console.log(`  ${item.from}:${item.line} → ${item.spec}`);
    console.log();
  }

  if (process.argv.includes('--edges')) {
    console.log('全部 .vue 的入边（用来审计"0 条 a"这个读数）：');
    for (const node of [...nodes.values()].filter(item => item.isVue).sort((a, b) => a.key.localeCompare(b.key))) {
      const sources = result.inEdges.get(node.key) ?? [];
      const mark = result.seen.has(node.key) ? '可达' : '不可达';
      console.log(`  [${mark}] ${node.key} ← ${sources.length ? sources.join('、') : '（没有任何值 import）'}`);
    }
    console.log();
  }
}

function gate(result) {
  const baseline = loadBaseline();
  const entries = baseline.entries ?? {};
  const problems = [];
  const present = new Set(result.classified.map(node => node.key));

  for (const node of result.classified) {
    const known = entries[node.key];
    if (!known) {
      problems.push(`新增不可达：${node.key}（建议档 ${node.class}：${node.note}）`);
      continue;
    }
    if (!CLASSES.has(known.class)) {
      problems.push(`基线条目 ${node.key} 的档写错了：${known.class}（只能是 a/b/c/d/e）`);
    }
    if (known.class === 'a' && !known.ack) {
      problems.push(`基线条目 ${node.key} 是 a 档（真缺口）：写进基线等于承认这个界面此刻真的看不见，必须留 ack（署名）`);
    }
    for (const evidence of known.evidence ?? []) {
      if (!fs.existsSync(path.join(FRONTEND, '..', evidence))) {
        problems.push(`基线条目 ${node.key} 的豁免理由已经不在了：${evidence}`);
      }
    }
  }
  for (const key of Object.keys(entries)) {
    if (!present.has(key)) console.log(`（提示）基线条目已不再不可达，可以删掉它了：${key}`);
  }

  if (problems.length) {
    console.log('闸门：不通过');
    for (const line of problems) console.log(`  · ${line}`);
    console.log('\n每条新增要么接上入口（import 它、或删掉它），要么在');
    console.log('scripts/reachability_baseline.json 里写清为什么它可以不可达（class + reason + evidence）。');
    return 1;
  }
  console.log(`闸门：通过（${result.classified.length} 条不可达全部在基线内）`);
  return 0;
}

function main(argv) {
  const rootArg = argv.find(item => item.startsWith('--root='));
  const rootKey = rootArg
    ? (rootArg.slice('--root='.length) === 'app' ? ROOT_APP : ROOT_MAIN)
    : ROOT_MAIN;

  if (!fs.existsSync(path.join(FRONTEND, rootKey))) {
    console.error(`找不到根文件：${rootKey}`);
    return 1;
  }

  const result = scan(rootKey);
  result.baseline = loadBaseline();

  if (argv.includes('--json')) {
    console.log(JSON.stringify({
      root: result.rootKey,
      totals: result.totals,
      unreachable: result.classified.map(node => ({ key: node.key, class: node.class, note: node.note })),
      dynamic: [...result.dynamicOnly.entries()].filter(([, d]) => d).map(([key]) => key),
      unresolved: result.unresolved,
    }, null, 2));
    return 0;
  }

  report(result);
  if (argv.includes('--gate')) return gate(result);
  return 0;
}

process.exit(main(process.argv.slice(2)));
