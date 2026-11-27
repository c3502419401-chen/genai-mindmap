// 静态读出每个用例的超时上限：`describe/it/test` 的第二个参数 `{ timeout: N }`
// （支持本文件内的数字常量，如 `CANVAS_136_TIMEOUT = 20_000`），没写就是 vitest 默认 5000。
//
// 为什么用 AST 而不是正则：本仓库的用例名里有中文标点、模板字符串、`it.each`，
// 正则会在嵌套 describe 与跨行写法上漏。手法与 `scripts/find_unwired.mjs` 一致
// （`typescript` 的 `createSourceFile`，只读文本，不跑测试、不联网）。
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import ts from 'typescript';

export const DEFAULT_TIMEOUT = 5000;
const HOOKS = new Set(['describe', 'it', 'test', 'suite', 'bench']);

export function walkSpecs(dir, acc = []) {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) walkSpecs(full, acc);
    else if (name.endsWith('.spec.ts')) acc.push(full);
  }
  return acc;
}

function numberValue(node, consts) {
  if (ts.isNumericLiteral(node)) return Number(node.text.replace(/_/g, ''));
  if (ts.isIdentifier(node)) return consts.get(node.text) ?? null;
  // `2 * 1000` 这类算式不猜：读不出来就按默认上限，闸门会把它当"没写上限"。
  return null;
}

function timeoutOf(node, consts) {
  if (!node || !ts.isObjectLiteralExpression(node)) return null;
  for (const prop of node.properties) {
    if (!ts.isPropertyAssignment(prop)) continue;
    if ((prop.name?.getText?.() ?? '') !== 'timeout') continue;
    return numberValue(prop.initializer, consts);
  }
  return null;
}

function collectConsts(sf) {
  const consts = new Map();
  const visit = (node) => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      const v = numberValue(node.initializer, consts);
      if (v != null) consts.set(node.name.text, v);
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return consts;
}

/**
 * Map<`<describe 链 空格连接> <it 标题>`, {cap, explicit}>。
 * 键的形状与 vitest json reporter 的 `fullName` 一致（ancestorTitles.join(' ') + ' ' + title）。
 */
export function collectLimits(file) {
  const text = readFileSync(file, 'utf8');
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const consts = collectConsts(sf);
  const limits = new Map();

  const visit = (node, chain, inherited, inheritedExplicit) => {
    if (ts.isCallExpression(node)) {
      const callee = node.expression;
      let name = null;
      if (ts.isIdentifier(callee)) name = callee.text;
      else if (ts.isPropertyAccessExpression(callee) && ts.isIdentifier(callee.expression))
        name = callee.expression.text; // describe.each / it.each 等
      if (name && HOOKS.has(name)) {
        const args = node.arguments;
        const titleArg = args.find((a) => ts.isStringLiteral(a) || ts.isNoSubstitutionTemplateLiteral(a));
        const title = titleArg ? titleArg.text : null;
        let own = inherited;
        let ownExplicit = inheritedExplicit;
        for (const arg of args) {
          const t = timeoutOf(arg, consts);
          if (t != null) { own = t; ownExplicit = true; }
        }
        const isSuite = name === 'describe' || name === 'suite';
        if (title && !isSuite) limits.set([...chain, title].join(' '), { cap: own, explicit: ownExplicit });
        for (const arg of args) {
          if (ts.isArrowFunction(arg) || ts.isFunctionExpression(arg)) {
            const nextChain = title ? [...chain, title] : chain;
            if (ts.isBlock(arg.body)) {
              for (const stmt of arg.body.statements) visit(stmt, nextChain, own, ownExplicit);
            } else {
              visit(arg.body, nextChain, own, ownExplicit);
            }
          }
        }
        return;
      }
    }
    ts.forEachChild(node, (child) => visit(child, chain, inherited, inheritedExplicit));
  };

  visit(sf, [], DEFAULT_TIMEOUT, false);
  return limits;
}
