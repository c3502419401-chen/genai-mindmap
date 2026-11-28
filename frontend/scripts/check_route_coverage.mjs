/**
 * 路由到 `client.ts` 的断链查法：`backend/openapi.json` 的每个操作，`src/api/client.ts`
 * 里有没有人请求它。
 *
 * ## 为什么这一环到今天才补
 *
 * 三档查法原来缺中间那一档：
 *
 *     后端函数零引用            → backend/scripts/acc3/find_unwired.py（棘轮）
 *     client.ts 函数零调用      → frontend/scripts/find_unwired.mjs（棘轮）
 *     后端有路由、client 一行都没有 → 【没人看得见】← 这个脚本
 *
 * 第一档看得见"路由写了没人调"，第二档看得见"函数写了没人调"；而"路由在、函数根本
 * 不存在"这一档，两边的判据都从自己那一侧出发，谁都覆盖不到弯折处。撞出来的五处
 * （`/knowledge/search` 桩、`link.suggest`、账本三条、实体消解、relations 生成端）
 * 全是人工发现的，这一条是最贵的那处：**`memory` 整块 4 个操作从来没有被前端请求过**。
 *
 * ## 判据
 *
 * 两份**文本**求双向差集，不读运行时、不读数据库、不读 git 状态：
 *
 * 1. `backend/openapi.json`：全部 `path` + `method`（去掉 `/api` 前缀）；
 * 2. `frontend/src/api/client.ts`：**TypeScript 语法树**里所有"像请求路径"的字面量
 *    （`StringLiteral` / `NoSubstitutionTemplateLiteral` / `TemplateExpression`），
 *    再顺着父链找它是哪个调用的第 0 个实参，据此定 method。
 *
 * 求差集时：
 *
 * - 后端有、client 没有 → **断链**（要报的那一档）；
 * - client 有、后端没有 → **死链**（性质不同：前端会打到 404/405）；
 * - 两边都有 → 已覆盖。
 *
 * ## 路径归一化（这是全部脆弱点，所以逐条写清）
 *
 * | 形状 | 处理 | 例子 |
 * |---|---|---|
 * | 后端占位名 | 一律塌成 `{}`——两边的名字从来不同 | `{map_id}` / `{item_id}` / `{job_id}` → `{}` |
 * | 模板串插值 | 每个 `${…}` 塌成 `{}`，`${BASE}` 是**空前缀**不是占位 | `` `/mindmaps/${id}` `` → `/mindmaps/{}` |
 * | 尾部连续占位 | 合并成一个：`${id}${query}` 里第二个是查询串变量 | `` `/knowledge/library/${id}${q}` `` → `/knowledge/library/{}` |
 * | 查询串 | 截掉 `?` 之后（后端操作与查询参数无关） | `` `/agent/runs?${qs}` `` → `/agent/runs` |
 * | `/api` 前缀 | 两侧都去：`request()` 拼的是 `` `${BASE}${path}` ``，`BASE` 默认 `/api` | `/api/memory` → `/memory` |
 *
 * 占位名不参与比较，**段数与字面段参与**：`/memory/{}` 与 `/memory/search` 是两条，
 * 这是有意的——它们的路由语义本来就不同（后端 `memory/router.py` 里专门写了一段
 * "`/search` 必须排在 `/{item_id}` 之前"）。
 *
 * ## 唯一的显式放宽：参数化覆盖（`param_cover`）
 *
 * `/export/${format}`（4 种格式同一行代码）在后端是 4 条字面路径。按段严格比较会
 * 报 4 条断链——那是**误报**，因为一条模板串确实覆盖了它们。这类覆盖**必须在基线里
 * 显式声明**（`param_cover`），脚本绝不自作主张把 `{}` 当成能匹配任意字面段：
 * 那会让 `/memory/{}` 顺手吞掉 `/memory/search`，于是真断链藏起来而读数变漂亮。
 *
 * ## 跑法
 *
 *     cd frontend
 *     node scripts/check_route_coverage.mjs            # 汇总 + 断链 / 死链清单
 *     node scripts/check_route_coverage.mjs --gaps     # 只列断链
 *     node scripts/check_route_coverage.mjs --gate     # 闸门：退出码非 0 表示有新增
 *     node scripts/check_route_coverage.mjs --openapi=../tmp.json   # 换一份快照（验红用）
 *
 * ## 闸门是棘轮，不是绝对零
 *
 * 与 `find_unwired.mjs` 同一套语义：绝对判据（"断链一条都不许有"）第一天就会红，
 * 然后被人关掉。闸门比的是 `scripts/route_coverage_baseline.json`：
 *
 * 1. `entries`：已知断链，逐条写"为什么它可以不接"（`gap` 真缺口 / `stub` 桩 /
 *    `internal` 给管道内部用 / `observability` 运维面）；
 * 2. `whitelist`：client 有、后端快照没有的路径。目前只有一条
 *    `POST /optimization/repair`——后端挂着 `include_in_schema=False`；
 * 3. `param_cover`：上面那条显式放宽。
 *
 * 两条反向检查防基线腐烂：条目里的 `evidence` 文件不存在 → **红**（豁免的理由没了
 * 而豁免还在）；条目对应的断链已经消失 → 打印提示（不红），修好了就该删掉它。
 *
 * ## 不覆盖什么
 *
 * 它只证明"`client.ts` 引用了这个路径"，不证明"哪个组件调了那个函数"——后者是
 * `find_unwired.mjs` 的活。两级串起来才闭环：路由 → client → 组件。
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import ts from 'typescript';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(HERE, '..');
const REPO = path.resolve(FRONTEND, '..');
const CLIENT = path.join(FRONTEND, 'src', 'api', 'client.ts');
const DEFAULT_OPENAPI = path.join(REPO, 'backend', 'openapi.json');
const BASELINE = path.join(HERE, 'route_coverage_baseline.json');

const METHODS = new Set(['get', 'post', 'put', 'patch', 'delete', 'head', 'options']);
const PLACEHOLDER = '{}';
/** 承载 API 前缀的那个标识符；`${BASE}` 在路径里是空前缀，不是占位。 */
const BASE_NAME = 'BASE';

/** client.ts 里 `BASE` 的缺省值。取不到就报错——静默猜一个会让两侧路径对不齐。 */
function apiPrefix() {
  const text = fs.readFileSync(CLIENT, 'utf8');
  const match = /VITE_API_BASE\s*\?\?\s*'([^'"]*)'/.exec(text);
  if (!match) {
    throw new Error('client.ts 里找不到 `VITE_API_BASE ?? \'…\'` 的缺省值，无法把两侧路径对齐');
  }
  return match[1].replace(/\/+$/, '');
}

function lineOf(file, node) {
  return file.getLineAndCharacterOfPosition(node.getStart()).line + 1;
}

/** 一个字面量/模板串的原始文本；非路径表达式返回 null。 */
function pathText(node) {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
  if (!ts.isTemplateExpression(node)) return null;
  let out = node.head.text;
  for (const span of node.templateSpans) {
    out += ts.isIdentifier(span.expression) && span.expression.text === BASE_NAME ? '' : PLACEHOLDER;
    out += span.literal.text;
  }
  return out;
}

/** 对象字面量里某个字符串属性的值（只看直接写在面上的，不追踪变量）。 */
function stringProp(object, name) {
  if (!object || !ts.isObjectLiteralExpression(object)) return null;
  for (const property of object.properties) {
    if (!ts.isPropertyAssignment(property)) continue;
    const key = property.name;
    const literalName = ts.isIdentifier(key) ? key.text
      : ts.isStringLiteral(key) ? key.text : null;
    if (literalName !== name) continue;
    if (ts.isStringLiteral(property.initializer)) return property.initializer.text;
  }
  return null;
}

// 注意字段名：TypeScript 的 AST 里调用表达式的被调方是 `expression`，不是 ESTree 的 `callee`。
function calleeName(callee) {
  if (callee && ts.isIdentifier(callee)) return callee.text;
  if (callee && ts.isPropertyAccessExpression(callee) && ts.isIdentifier(callee.name)) return callee.name.text;
  return null;
}

/** 这个节点是不是某个调用的第 0 个实参；是就返回那个调用。 */
function enclosingCallArg(node) {
  let current = node.parent;
  while (current) {
    if (ts.isCallExpression(current) && current.arguments[0] === node) return current;
    current = current.parent;
  }
  return null;
}

/**
 * 路径所在的请求用哪个方法。
 *
 * `postSse` 固定 POST；`request` / `fetch` 看第 1 个实参的 `method` 字段，缺省 GET。
 * 认不出来的（路径常量赋值、拼在别处的字面量）记 `*`：与后端的任意方法匹配，
 * 但在汇总里单独一栏打出来——那是"判据看不见"的地方，不该被算成精确覆盖。
 */
function methodOf(node) {
  const call = enclosingCallArg(node);
  if (!call) return '*';
  const name = calleeName(call.expression);
  if (name === 'postSse') return 'POST';
  if (name === 'request' || name === 'fetch') {
    const declared = stringProp(call.arguments[1], 'method');
    return (declared ?? 'GET').toUpperCase();
  }
  return '*';
}

function normalizeClientPath(raw, prefix) {
  let out = raw;
  const query = out.indexOf('?');
  if (query >= 0) out = out.slice(0, query);
  // `${id}${query}`：末段连着两个插值时，后一个通常是查询串变量。
  out = out.replace(/(?:\{\}){2,}/g, PLACEHOLDER);
  if (!out.startsWith('/')) out = `/${out}`;
  out = out.replace(/\/{2,}/g, '/');
  if (out.length > 1) out = out.replace(/\/+$/, '');
  while (prefix && out.startsWith(`${prefix}/`)) out = out.slice(prefix.length);
  return out || '/';
}

/** `client.ts` 里所有看起来是请求路径的表达式。 */
function clientPaths(prefix) {
  const text = fs.readFileSync(CLIENT, 'utf8');
  const file = ts.createSourceFile(CLIENT, text, ts.ScriptTarget.ESNext, true);
  const found = [];
  const visit = (node) => {
    const raw = pathText(node);
    if (raw !== null) {
      const cut = raw.indexOf('?');
      const head = cut >= 0 ? raw.slice(0, cut) : raw;
      // 只认以 `/` 开头的：那正是"绝对路径"的形状。`BASE` 的缺省值 `/api` 会被排除。
      if (head.startsWith('/') && head !== prefix) {
        found.push({
          method: methodOf(node),
          path: normalizeClientPath(raw, prefix),
          line: lineOf(file, node),
        });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return found;
}

/** `openapi.json` 的全部操作，路径已去 `/api` 前缀并塌掉占位名。 */
function backendOps(openapiPath, prefix) {
  if (!fs.existsSync(openapiPath)) {
    throw new Error(`找不到 openapi 快照：${openapiPath}（先跑 backend/scripts/export_openapi.py）`);
  }
  const doc = JSON.parse(fs.readFileSync(openapiPath, 'utf8'));
  const ops = [];
  for (const [rawPath, item] of Object.entries(doc.paths ?? {})) {
    let route = rawPath;
    if (prefix && route.startsWith(`${prefix}/`)) route = route.slice(prefix.length);
    const normalized = route.replace(/\{[^/}]*\}/g, PLACEHOLDER);
    for (const method of Object.keys(item ?? {})) {
      if (!METHODS.has(method)) continue;
      ops.push({ method: method.toUpperCase(), path: normalized, raw: rawPath });
    }
  }
  return ops;
}

const keyOf = (method, routePath) => `${method} ${routePath}`;
const segments = (routePath) => routePath.split('/').filter(Boolean);

/** 参数化覆盖：基线里声明的 `{}` 段可匹配后端的任意单段。 */
function patternMatches(pattern, routePath) {
  const left = segments(pattern);
  const right = segments(routePath);
  if (left.length !== right.length) return false;
  return left.every((part, index) => part === PLACEHOLDER || part === right[index]);
}

function loadBaseline() {
  if (!fs.existsSync(BASELINE)) return { entries: {}, whitelist: {}, param_cover: {} };
  const parsed = JSON.parse(fs.readFileSync(BASELINE, 'utf8'));
  return { entries: parsed.entries ?? {}, whitelist: parsed.whitelist ?? {}, param_cover: parsed.param_cover ?? {} };
}

function evidenceProblems(name, item) {
  const problems = [];
  for (const evidence of item.evidence ?? []) {
    if (!fs.existsSync(path.join(REPO, evidence))) {
      problems.push(`${name} 的豁免理由已经不在了：${evidence}`);
    }
  }
  return problems;
}

function scan(openapiPath, prefix) {
  const backend = backendOps(openapiPath, prefix);
  const client = clientPaths(prefix);
  const baseline = loadBaseline();

  const clientSet = new Map();
  for (const item of client) {
    const key = keyOf(item.method, item.path);
    if (!clientSet.has(key)) clientSet.set(key, []);
    clientSet.get(key).push(item.line);
  }
  const wildcard = new Map();
  for (const item of client) {
    if (item.method !== '*') continue;
    if (!wildcard.has(item.path)) wildcard.set(item.path, []);
    wildcard.get(item.path).push(item.line);
  }

  const covered = [];
  const gaps = [];
  for (const op of backend) {
    const exact = clientSet.get(keyOf(op.method, op.path));
    const loose = op.path === '/' ? wildcard.get('/') : wildcard.get(op.path);
    if (exact || loose) {
      covered.push({ ...op, how: exact ? 'exact' : 'wildcard', lines: exact ?? loose });
      continue;
    }
    const declared = Object.entries(baseline.param_cover)
      .filter(([pattern]) => patternMatches(pattern.split(' ')[1] ?? '', op.path))
      .map(([pattern]) => pattern);
    if (declared.length) {
      covered.push({ ...op, how: 'param', lines: clientSet.get(declared[0]) ?? [] });
      continue;
    }
    gaps.push(op);
  }

  const backendSet = new Set(backend.map(op => keyOf(op.method, op.path)));
  const backendPaths = new Set(backend.map(op => op.path));
  const paramHits = Object.fromEntries(Object.keys(baseline.param_cover).map(pattern => [
    pattern,
    backend.filter(op => patternMatches(pattern.split(' ')[1] ?? '', op.path)).length,
  ]));
  /**
   * 一条 client 模板串（如 `POST /export/{}`）与后端的字面条路径本来就不相等，
   * 所以它必然会落进 dead。它是不是死链取决于**有没有在基线上声明覆盖谁**：
   * 声明了且真的匹配到后端操作 → 是参数化覆盖，不是死链。
   */
  const declaredParam = item => (paramHits[keyOf(item.method, item.path)] ?? 0) > 0;
  const dead = [];
  for (const item of client) {
    if (declaredParam(item)) continue;
    if (item.method === '*') {
      if (!backendPaths.has(item.path)) dead.push(item);
      continue;
    }
    if (!backendSet.has(keyOf(item.method, item.path))) dead.push(item);
  }

  // 去重：同一个 (method, path) 只报一次，带行号。
  const dedupe = (items, render) => {
    const seen = new Map();
    for (const item of items) {
      const key = keyOf(item.method, item.path);
      if (!seen.has(key)) seen.set(key, []);
      seen.get(key).push(item);
    }
    return [...seen.entries()].map(([key, list]) => ({ key, list, text: render(key, list) }));
  };

  return {
    backend, client, clientSet, wildcard, covered, baseline, paramHits,
    gaps: dedupe(gaps, (key, list) => `${key}  （${list.map(i => `openapi:${i.raw}`)[0]}）`),
    dead: dedupe(dead, (key, list) => `${key}  （client.ts:${list.map(i => i.line).join(',')}）`),
  };
}

/** `gapsOnly` 时不打汇总与死链，只列断链（`--gaps`）。 */
function report(result, gapsOnly = false) {
  const { backend, client, covered, gaps, dead, baseline } = result;
  const paramCovered = covered.filter(item => item.how === 'param');
  const wildcardCovered = covered.filter(item => item.how === 'wildcard');

  if (!gapsOnly) {
    console.log(`后端操作（openapi.json，去 ${apiPrefix()} 前缀）：${backend.length}`);
    console.log(`client.ts 归一化请求路径：${new Set(client.map(i => keyOf(i.method, i.path))).size} 条（method+路径）`);
    console.log(`  其中不看 method 的路径去重：${new Set(client.map(i => i.path)).size} 条`);
    console.log(`  已覆盖（精确 method + 路径）：${covered.filter(i => i.how === 'exact').length}`);
    console.log(`  已覆盖（参数化声明）：${paramCovered.length}`);
    console.log(`  已覆盖（method 判不出，按路径匹配）：${wildcardCovered.length}`);
    console.log(`  ★ 断链（后端有、client 没有）：${gaps.length}`);
    console.log(`  ★ 死链（client 有、后端快照没有）：${dead.length}`);
    console.log();
    console.log('判据：后端 `openapi.json` 的 path+method，与 `client.ts` 语法树里请求路径的');
    console.log('归一化形式求双向差集；占位名不参与比较，段数与字面段参与。');
    console.log();
  }

  if (gaps.length) {
    console.log('断链：');
    for (const gap of gaps) {
      const note = baseline.entries[gap.key];
      const status = note ? `[基线 ${note.status}]` : '[新增]';
      console.log(`  ${status} ${gap.text}`);
      if (note?.reason) console.log(`      ${note.reason}`);
    }
    console.log();
  }
  if (gapsOnly) return;
  if (dead.length) {
    console.log('死链：');
    for (const item of dead) {
      const note = baseline.whitelist[item.key];
      console.log(`  ${note ? '[白名单]' : '[新增]'} ${item.text}`);
      if (note?.reason) console.log(`      ${note.reason}`);
    }
    console.log();
  }
  if (wildcardCovered.length) {
    console.log('method 判不出的覆盖（不该有，有就说明路径没写在请求调用的第 0 个实参上）：');
    for (const item of wildcardCovered) console.log(`  ${keyOf(item.method, item.path)}  client.ts:${item.lines.join(',')}`);
    console.log();
  }
}

function gate(result) {
  const { gaps, dead, baseline, paramHits } = result;
  const problems = [];

  for (const gap of gaps) {
    if (gap.key in baseline.entries) continue;
    problems.push(`新增断链：${gap.key}（后端有路由，client.ts 一行都没有）`);
  }
  for (const item of dead) {
    if (item.key in baseline.whitelist) continue;
    problems.push(`新增死链：${item.key}（client.ts 请求了后端快照里没有的路径）`);
  }
  for (const [name, item] of Object.entries(baseline.entries)) {
    problems.push(...evidenceProblems(name, item));
    if (!gaps.some(gap => gap.key === name)) {
      console.log(`（提示）基线条目已不再是断链，可以删掉它了：${name}`);
    }
  }
  for (const [name, item] of Object.entries(baseline.whitelist)) {
    problems.push(...evidenceProblems(name, item));
    if (!dead.some(entry => entry.key === name)) {
      console.log(`（提示）白名单条目现在已经在后端快照里，可以删掉它了：${name}`);
    }
  }
  for (const [pattern, hits] of Object.entries(paramHits)) {
    const item = baseline.param_cover[pattern] ?? {};
    problems.push(...evidenceProblems(pattern, item));
    if (hits === 0) problems.push(`参数化覆盖声明已过期：${pattern} 一条后端操作都没匹配上`);
  }

  if (problems.length) {
    console.log('闸门：不通过');
    for (const line of problems) console.log(`  · ${line}`);
    console.log('\n每条新增断链都要么接上 client，要么在 scripts/route_coverage_baseline.json');
    console.log('里写清"为什么它可以不接"（status + reason + evidence）。');
    return 1;
  }
  console.log(`闸门：通过（${gaps.length} 条断链 / ${dead.length} 条死链全部在基线内）`);
  return 0;
}

function main(argv) {
  const openapiArg = argv.find(item => item.startsWith('--openapi='));
  const openapiPath = openapiArg ? path.resolve(process.cwd(), openapiArg.slice('--openapi='.length)) : DEFAULT_OPENAPI;
  const prefix = apiPrefix();
  const result = scan(openapiPath, prefix);

  const gapsOnly = argv.includes('--gaps');
  if (argv.includes('--gate')) {
    report(result, gapsOnly);
    return gate(result);
  }
  report(result, gapsOnly);
  if (gapsOnly) return 0;

  console.log('已覆盖（参数化声明：一条模板串对多条字面路径）：');
  for (const [pattern, item] of Object.entries(result.baseline.param_cover)) {
    const hits = result.covered.filter(entry => entry.how === 'param'
      && patternMatches(pattern.split(' ')[1] ?? '', entry.path));
    console.log(`  ${pattern}  →  ${hits.map(entry => keyOf(entry.method, entry.path)).join('、') || '（无）'}`);
    if (item.reason) console.log(`      ${item.reason}`);
  }
  return 0;
}

process.exit(main(process.argv.slice(2)));
