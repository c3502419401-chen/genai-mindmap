import { describe, expect, it } from 'vitest';

import type { MindMapContent, MindNode } from '../src/api/types';
import {
  addChild,
  addSibling,
  charLength,
  cloneTree,
  countNodes,
  depthOf,
  findNode,
  findParent,
  LIMITS,
  newId,
  removeNode,
  renameNode,
  validateDraft,
} from '../src/mindmap/edits';

const leaf = (id: string, label = id): MindNode => ({ id, label, children: [] });

/** 根 + 甲(甲乙, 甲丙) + 乙 */
function tree(): MindNode {
  return {
    id: 'root',
    label: '根',
    children: [
      {
        id: 'a',
        label: '甲',
        children: [leaf('a1', '甲一'), leaf('a2', '甲二')],
      },
      leaf('b', '乙'),
    ],
  };
}

const content = (root: MindNode): MindMapContent => ({ title: '示例', root });

describe('查找与统计', () => {
  it('能找到任意节点和它的父节点，根节点没有父节点', () => {
    const root = tree();
    expect(findNode(root, 'a1')?.label).toBe('甲一');
    expect(findParent(root, 'a1')?.id).toBe('a');
    expect(findParent(root, 'root')).toBeNull();
    expect(findNode(root, 'missing')).toBeNull();
  });

  it('统计节点数与深度', () => {
    expect(countNodes(tree())).toBe(5);
    expect(depthOf(tree())).toBe(3);
    expect(depthOf(leaf('x'))).toBe(1);
  });
});

describe('编辑操作', () => {
  it('改文字返回新树，原树不受影响', () => {
    const root = tree();
    const next = renameNode(root, 'a1', '改过的甲一');

    expect(findNode(next, 'a1')?.label).toBe('改过的甲一');
    expect(findNode(root, 'a1')?.label).toBe('甲一');
  });

  it('加子节点追加到末尾，并返回新节点供界面选中', () => {
    const { root, node } = addChild(tree(), 'a');

    expect(node.label).toBe('新节点');
    expect(findNode(root, 'a')?.children.map(child => child.id)).toEqual(['a1', 'a2', node.id]);
    expect(node.id.startsWith('n_')).toBe(true);
  });

  it('加兄弟插在自己后面，不打乱原有顺序', () => {
    const { root, node } = addSibling(tree(), 'a1');

    expect(findNode(root, 'a')?.children.map(child => child.id)).toEqual(['a1', node.id, 'a2']);
  });

  it('根节点加兄弟退化为加子节点', () => {
    const { root, node } = addSibling(tree(), 'root');

    expect(findNode(root, 'root')?.children.some(child => child.id === node.id)).toBe(true);
  });

  it('父节点不存在时报错，不悄悄生成孤儿节点', () => {
    expect(() => addChild(tree(), 'missing')).toThrow('父节点不存在');
  });

  it('删除节点会连子树一起删掉', () => {
    const root = removeNode(tree(), 'a');

    expect(findNode(root, 'a')).toBeNull();
    expect(findNode(root, 'a1')).toBeNull();
    expect(countNodes(root)).toBe(2);
  });

  it('根节点不能删', () => {
    expect(() => removeNode(tree(), 'root')).toThrow('根节点不能删除');
  });

  it('cloneTree 是深拷贝，改副本不动原树', () => {
    const root = tree();
    const copy = cloneTree(root);
    copy.children[0].label = '被改过';

    expect(root.children[0].label).toBe('甲');
  });

  it('新节点 id 带前缀且不重复', () => {
    const ids = new Set(Array.from({ length: 50 }, () => newId()));
    expect(ids.size).toBe(50);
    for (const id of ids) expect(id).toMatch(/^n_[0-9a-f]{8}$/);
  });
});

describe('提交前校验', () => {
  it('合法导图返回 null', () => {
    expect(validateDraft(content(tree()))).toBeNull();
  });

  it('标题为空或超长都拦下', () => {
    expect(validateDraft({ ...content(tree()), title: '   ' })).toBe('标题不能为空');
    expect(validateDraft({ ...content(tree()), title: '标'.repeat(LIMITS.title + 1) })).toBe(
      `标题最多 ${LIMITS.title} 个字`,
    );
  });

  it('节点标题为空或超长都拦下', () => {
    expect(validateDraft(content({ id: 'r', label: ' ', children: [] }))).toBe('节点标题不能为空');
    expect(
      validateDraft(content({ id: 'r', label: '长'.repeat(LIMITS.label + 1), children: [] })),
    ).toBe(`节点「${'长'.repeat(12)}…」超过 ${LIMITS.label} 个字`);
  });

  it('超 50 个子节点拦下', () => {
    const root: MindNode = {
      id: 'r',
      label: '根',
      children: Array.from({ length: LIMITS.children + 1 }, (_, i) => leaf(`c${i}`)),
    };
    expect(validateDraft(content(root))).toBe(`节点「根」最多 ${LIMITS.children} 个子节点`);
  });

  it('超节点上限时拦下', () => {
    // 按 LIMITS.nodes 现算，别写死数字：上限是会调的（300 → 1000），
    // 写死一次调参数就要改一次测试。这里造「上限 + 1」个节点。
    const perBranch = 50;
    const branches = Math.ceil((LIMITS.nodes + 1 - 1) / perBranch);
    const root: MindNode = {
      id: 'r',
      label: '根',
      children: Array.from({ length: branches }, (_, i) => ({
        id: `c${i}`,
        label: `子${i}`,
        children: Array.from({ length: perBranch }, (_, j) => leaf(`c${i}-${j}`)),
      })),
    };
    expect(validateDraft(content(root))).toBe(`导图最多 ${LIMITS.nodes} 个节点`);
  });

  it('超 8 层拦下', () => {
    let node: MindNode = leaf('leaf');
    for (let depth = 1; depth < LIMITS.depth + 1; depth += 1) {
      node = { id: `d${depth}`, label: `第${depth}层`, children: [node] };
    }
    expect(validateDraft(content(node))).toBe(`导图最多 ${LIMITS.depth} 层`);
  });

  it('重复 id 拦下', () => {
    const root: MindNode = { id: 'r', label: '根', children: [leaf('x'), leaf('x', '另一个')] };
    expect(validateDraft(content(root))).toBe('节点 ID 必须唯一');
  });

  it('字数按码点算，emoji 只算一个字', () => {
    expect(charLength('😀😀')).toBe(2);
    expect(validateDraft(content({ id: 'r', label: '😀'.repeat(LIMITS.label), children: [] }))).toBeNull();
  });
});

/**
 * 手动加节点的**来历**。
 *
 * 三个入口（Tab 加子节点 / Enter 加兄弟 / 空白处双击插入）在界面层各有一个函数，
 * 但落到树上只有 `addChildAt` 与 `addSibling` 两个构造点。`origin` 写在这两处，
 * "手动加的节点没标来历"就变成不可表达的——而不是靠每个调用方记得补一行。
 *
 * 这两条必须能红：删掉构造里的 `origin: 'manual'`，它们会拿到 `undefined`。
 */
describe('手动加的节点记 origin = manual', () => {
  it('加子节点：新节点是 manual，树上原有节点不动', () => {
    const root: MindNode = { ...leaf('r'), children: [leaf('a')] };
    const { node, root: next } = addChild(root, 'r');

    expect(node.origin).toBe('manual');
    expect(findNode(next, node.id)?.origin).toBe('manual');
    // 老节点没有来历字段：这里不该顺手把它补成 manual（那是伪造来源）
    expect(findNode(next, 'a')?.origin).toBeUndefined();
  });

  it('加兄弟：同样是 manual', () => {
    const root: MindNode = { ...leaf('r'), children: [leaf('a')] };
    const { node } = addSibling(root, 'a');

    expect(node.origin).toBe('manual');
  });
});
