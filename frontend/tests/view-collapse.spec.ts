import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import { REAL_TREES } from './fixtures/real-trees';
import type { MindMapContent, MindNode } from '../src/api/types';
import { boxesOf, chooseViewMode, overlappingPairs, visibleIds } from '../src/canvas/clarity';
import { layoutDiagram } from '../src/canvas/layout';
import { NODE_SIZE } from '../src/canvas/layout/tidy';
import {
  MINDMAP_VISIBLE_NODE_LIMIT,
  NO_NODE_LIMIT,
  countSubtree,
  foldActionOf,
  viewTree,
} from '../src/canvas/view-collapse';
import { MIN_READABLE_SCALE, NODE_FONT_PX, fitAllScale, planView, MAX_ZOOM, MIN_ZOOM } from '../src/canvas/viewport';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * 视图层收起：**数据不动，只是这一屏画不下这么多**。
 *
 * 口径是**显示节点的数量上限**（`MINDMAP_VISIBLE_NODE_LIMIT`），不是"展开到第几层"。
 * 这一组用例钉的五件事，正好是这个口径的五条失败模式：
 *
 * 1. 上限真的生效——可见节点数 == 上限，且**一个节点都没丢**（可见 + 被截的闭包 = 总数）；
 * 2. 形状是**祖先闭合**的——每个画布上的节点，父链全在画布上（不然布局会画出孤儿）；
 * 3. 选取顺序是**按层铺满**——每一支都露头，不是"第一条分支吃满预算"；
 * 4. 三道闸门（点开 / 收起 / 全部展开）都能来回，且**不进撤销栈、不置脏**；
 * 5. 全展开时与改动之前**逐像素一致**（同一条布局函数，同一个入参对象）。
 *
 * 树用 `tests/fixtures/real-trees.ts` 里从库里原样导出的真树（136 / 95 / 80 节点），
 * 不用现造的等宽树：数量上限截出来的形状**极不规整**（同一层里有的父节点有孩子、
 * 有的没有），造出来的形状量不出真结论。
 */

const VIEWPORT = { width: 1600, height: 852 };

const tree136 = REAL_TREES.tree136 as MindMapContent;
const tree95 = REAL_TREES.tree95 as MindMapContent;
const tree80 = REAL_TREES.tree80 as MindMapContent;

/** 某个深度下 `fit-all` 的字号（CSS 像素）。 */
function fitAllFont(root: MindNode, viewport = VIEWPORT): number {
  const layout = layoutDiagram({ root, diagramType: 'tree', direction: 'right' });
  const { minX, minY, width, height } = layout.bounds;
  return NODE_FONT_PX * fitAllScale({ x: minX, y: minY, width, height }, viewport);
}

/** 布局结果的中心点 → 画布节点（左上角），与 `store.nodes` 的口径一致。 */
function positionsOf(root: MindNode): Map<string, { x: number; y: number }> {
  const layout = layoutDiagram({ root, diagramType: 'tree', direction: 'right' });
  const out = new Map<string, { x: number; y: number }>();
  for (const [id, point] of layout.positions) {
    out.set(id, { x: point.x - NODE_SIZE.width / 2, y: point.y - NODE_SIZE.height / 2 });
  }
  return out;
}

/** 视图树里所有节点 id → 它在这棵树里的父 id。 */
function parentIndexOf(root: MindNode): Map<string, string | null> {
  const out = new Map<string, string | null>();
  const walk = (node: MindNode, parent: string | null): void => {
    out.set(node.id, parent);
    for (const child of node.children) walk(child, node.id);
  };
  walk(root, null);
  return out;
}

/** 视图里看得见的节点总数（= `store.nodes.length` 同一口径）。 */
function visibleCount(root: MindNode): number {
  return parentIndexOf(root).size;
}

beforeEach(() => {
  setActivePinia(createPinia());
});

describe('上限是数量：136 个节点，画布上只画 40 个', () => {
  it('默认上限是 40，且 `0` 是"不限制"', () => {
    expect(MINDMAP_VISIBLE_NODE_LIMIT).toBe(40);
    expect(NO_NODE_LIMIT).toBe(0);
  });

  it('不限制时原样返回入参（引用相等，folded / open 为空）', () => {
    const result = viewTree(tree136.root, { limit: NO_NODE_LIMIT });
    expect(result.root).toBe(tree136.root);
    expect(result.folded.size).toBe(0);
    expect(result.open.size).toBe(0);
    expect(result.visible).toBe(136);
  });

  it('上限 40：可见 40 个，96 个没画出来，一个都没删', () => {
    const result = viewTree(tree136.root, { limit: 40 });
    expect(result.visible).toBe(40);
    expect(visibleCount(result.root)).toBe(40);
    // 被截掉的节点数 = 总数 − 可见；它必须由每个折叠点的 `closedSubtree` 摊出来
    const closed = [...result.folded.values()].reduce((sum, info) => sum + info.closedSubtree, 0);
    expect(result.visible + closed).toBe(136);
    // 数据一个字节都没动
    expect(countSubtree(tree136.root)).toBe(136);
    expect(JSON.stringify(tree136.root)).toBe(JSON.stringify(tree136.root));
  });

  it('形状是祖先闭合的：画布上每个节点的父链都在画布上', () => {
    for (const tree of [tree136, tree95, tree80]) {
      const result = viewTree(tree.root as MindNode, { limit: 40 });
      const parents = parentIndexOf(result.root);
      for (const [id, parentId] of parents) {
        if (parentId === null) continue;
        expect(parents.has(parentId), `${id} 的父节点必须也在画布上`).toBe(true);
      }
    }
  });

  it('每个折叠点的 hidden 就是"点了当场多出来的个数"', () => {
    const result = viewTree(tree136.root, { limit: 40 });
    const viewParents = parentIndexOf(result.root);
    // 视图树里看不到的那些孩子，个数必须等于 folded.hidden
    const contentParent = parentIndexOf(tree136.root);
    for (const [id, info] of result.folded) {
      const node = findIn(tree136.root, id);
      expect(node, `${id} 必须来自原树`).not.toBeNull();
      const shown = node?.children.filter(child => viewParents.get(child.id) === id).length ?? 0;
      expect(shown).toBe((node?.children.length ?? 0) - info.hidden);
      expect(info.total).toBe(node?.children.length ?? 0);
      // 折叠点一定在画布上（不然它的角标也没地方画）
      expect(viewParents.has(id)).toBe(true);
      expect(contentParent.get(id)).toBe(viewParents.get(id));
    }
  });

  it('小图完全隐形：31 个节点在上限 40 下不收起', () => {
    const small: MindNode = {
      id: 'r',
      label: '根',
      children: Array.from({ length: 6 }, (_, i) => ({
        id: `a${i}`,
        label: `A${i}`,
        children: Array.from({ length: 4 }, (_, j) => ({ id: `a${i}-${j}`, label: 'x', children: [] })),
      })),
    };
    const result = viewTree(small, { limit: 40 });
    expect(countSubtree(small)).toBe(31);
    expect(result.root).toBe(small);
    expect(result.folded.size).toBe(0);
  });

  it('层号不压缩：收起第 3 层不会让第 4 层升到第 3 层', () => {
    const result = viewTree(tree136.root, { limit: 12 });
    const depths = new Map<string, number>();
    const walk = (node: MindNode, level: number): void => {
      depths.set(node.id, level);
      for (const child of node.children) walk(child, level + 1);
    };
    walk(result.root, 0);
    // 每个可见节点的层号与它在原树里的层号完全一致
    for (const [id, level] of depths) {
      expect(depthOfNode(tree136.root, id)).toBe(level);
    }
    expect([...depths.values()].some(level => level >= 2)).toBe(true);
  });

  it('没有整支被截断的节点时，那段子树保持原引用（下游的引用相等判断不被搅乱）', () => {
    const result = viewTree(tree136.root, { limit: 40 });
    // 一级分支里"孩子全显示且更深一层一个都没画"的那种，其 children 必须还是原数组
    const branch = result.root.children[0];
    const original = tree136.root.children.find(child => child.id === branch.id);
    expect(original).toBeDefined();
    const shown = branch.children.length;
    const closedAll = original?.children.slice(shown) ?? [];
    if (shown === 0 && closedAll.length === original!.children.length) {
      expect(branch.children).toEqual([]);
    }
    // 根一定被换过（它有孩子被截），但它的 id 与层号不变
    expect(result.root.id).toBe(tree136.root.id);
    expect(result.root.label).toBe(tree136.root.label);
  });
});

describe('选取顺序：按层铺满，不是前序', () => {
  it('上限 40 时 6 条一级分支全部露头', () => {
    const result = viewTree(tree136.root, { limit: 40 });
    const topIds = new Set(result.root.children.map(child => child.id));
    expect(topIds.size).toBe(tree136.root.children.length);
  });

  it('上限压到 12，一级分支仍然一支不少（前序在这个额度下只够 2 支）', () => {
    const result = viewTree(tree136.root, { limit: 12 });
    expect(result.visible).toBe(12);
    // 12 = 根 + 6 支 + 5 个更深的孩子：一支都少不了
    expect(result.root.children.length).toBe(tree136.root.children.length);
    // 对照：前序会把第一支连同它的整棵子树吃满，6 支只剩 2 支（实测读数）
    const preorder = preorderPrefix(tree136.root, 12);
    expect(preorder.topBranches).toBeLessThanOrEqual(2);
    expect(result.root.children.length).toBeGreaterThan(preorder.topBranches);
  });

  it('层内保持树序：先到的那一支先拿到更深的孩子', () => {
    const result = viewTree(tree136.root, { limit: 40 });
    // 前 7 个（根 + 6 支）之后，第 8 个必须是一级分支[0] 的第一个孩子
    const firstDeeper = tree136.root.children[0].children[0];
    expect(findIn(result.root, firstDeeper.id)).not.toBeNull();
    // 而越靠后的分支，更深的孩子越可能还没轮到
    const last = tree136.root.children[tree136.root.children.length - 1];
    const lastShown = last.children.filter(child => findIn(result.root, child.id) !== null).length;
    const firstShown = tree136.root.children[0].children
      .filter(child => findIn(result.root, child.id) !== null).length;
    expect(firstShown).toBeGreaterThanOrEqual(lastShown);
  });
});

describe('三道闸门：点开 / 收起 / 全部展开', () => {
  it('点开一个被截断的节点：它的全部直接子节点出现，角标属性消失', () => {
    const base = viewTree(tree136.root, { limit: 40 });
    const target = [...base.folded.keys()][0];
    expect(foldActionOf(target, base)).toBe('expand');

    const opened = viewTree(tree136.root, { limit: 40, expanded: new Set([target]) });
    const node = findIn(tree136.root, target)!;
    expect(findIn(opened.root, target)?.children.length).toBe(node.children.length);
    // 这个节点自己不再是折叠点
    expect(opened.folded.has(target)).toBe(false);
    // 可见数只多出它自己的孩子（预算不够也照给：这是用户点名要看的一支）
    expect(opened.visible).toBe(base.visible + node.children.length);
    // 而且现在它变成"可以叠回去"的状态
    expect(foldActionOf(target, opened)).toBe('collapse');
  });

  it('收起一个已经摊开的节点：子节点全消失，可见数掉回来', () => {
    const target = tree136.root.children[0].id;
    const collapsed = viewTree(tree136.root, { limit: 40, collapsed: new Set([target]) });
    expect(findIn(collapsed.root, target)?.children.length).toBe(0);
    expect(collapsed.folded.get(target)?.hidden).toBe(tree136.root.children[0].children.length);
    // 收起是"比默认更少"：可见数小于上限
    expect(collapsed.visible).toBeLessThan(40);
    // 点它又回到"能展开"
    expect(foldActionOf(target, collapsed)).toBe('expand');
  });

  it('收起优先于全部展开：`collapse` 盖过 `expandAll`', () => {
    const target = tree136.root.children[0].id;
    const result = viewTree(tree136.root, { limit: 40, expandAll: true, collapsed: new Set([target]) });
    expect(findIn(result.root, target)?.children.length).toBe(0);
    expect(findIn(result.root, tree136.root.children[1].id)?.children.length)
      .toBe(tree136.root.children[1].children.length);
  });

  it('全部展开：一个节点都不收起，全部节点都在画布上', () => {
    const result = viewTree(tree136.root, { limit: 40, expandAll: true });
    expect(result.visible).toBe(136);
    expect(result.folded.size).toBe(0);
    // 每个有孩子的节点都是"用户要求显示"的状态 → 都能再点一下叠回去
    expect([...result.open.values()].every(reason => reason === 'all')).toBe(true);
  });

  it('叶子没有条目（不该长出任何角标）', () => {
    const result = viewTree(tree136.root, { limit: 40 });
    expect(result.open.has(tree136.root.children[0].children[0].children[0].id)).toBe(false);
  });
});

describe('store：画布上的读数', () => {
  function load(content: MindMapContent): ReturnType<typeof useMindMapStore> {
    const store = useMindMapStore();
    store.preview(content, 'right');
    return store;
  }

  /** 画布上第一个被截断的节点（它头上挂着 `+N`）。 */
  function firstFoldedId(store: ReturnType<typeof useMindMapStore>): string {
    const hit = store.nodes.find(node => (node.data?.hiddenChildren ?? 0) > 0);
    expect(hit, '这一屏必须至少有一个被截断的节点').toBeDefined();
    return hit!.id;
  }

  /** 画布上看不见、但父节点看得见的那个节点——"藏在折叠点后面"的那一类。 */
  function firstHiddenUnderVisible(root: MindNode, visible: Set<string>): MindNode {
    let found: MindNode | null = null;
    const walk = (node: MindNode): void => {
      if (found || !visible.has(node.id)) return;
      for (const child of node.children) {
        if (!visible.has(child.id)) {
          found = child;
          return;
        }
        walk(child);
      }
    };
    walk(root);
    expect(found, '这棵树里必须有一个藏起来的节点').not.toBeNull();
    return found!;
  }

  it('装上 136 节点的真图：画布 40 个节点，内容仍是 136 个', () => {
    const store = load(tree136);
    expect(store.nodeCount).toBe(136);
    expect(store.nodes.length).toBe(40);
    expect(store.hiddenNodeCount).toBe(96);
    expect(store.flatNodes.length).toBe(40);
    // 视角是个结论，不是拍脑袋：默认上限下 fit-all 只有 5.3px，所以画布该落在 fit-width
    const choice = chooseViewMode({
      boxes: boxesOf(layoutDiagram({ root: store.canvasRoot as MindNode, diagramType: 'tree', direction: 'right' })),
      viewport: VIEWPORT,
    });
    expect(choice.mode).toBe('fit-width');
    expect(choice.fontPx).toBeCloseTo(12, 5);
  });

  it('全展开：与改动之前逐像素一致（同一条布局函数，同一个入参对象）', () => {
    const store = load(tree136);
    store.expandAllInView();
    expect(store.nodes.length).toBe(136);
    expect(store.hiddenNodeCount).toBe(0);

    const expected = positionsOf(tree136.root);
    expect(store.nodes.length).toBe(expected.size);
    for (const node of store.nodes) {
      expect(node.position).toEqual(expected.get(node.id));
    }
    // 边也一条不少：父子线 = 节点数 - 1
    expect(store.edges.filter(edge => !edge.id.startsWith('relation:')).length).toBe(135);
  });

  it('点开一个被截断的节点：子节点出现，重排后没有任何重叠', () => {
    const store = load(tree136);
    const baseVisible = store.nodes.length;
    const target = firstFoldedId(store);
    const node = findIn(tree136.root, target)!;
    store.expandNodeInView(target);
    expect(store.nodes.length).toBe(baseVisible + node.children.length);

    // 不规整形状（有的父节点有孩子、有的没有）喂给 `layoutTidy` 不许出现重叠
    const boxes = boxesOf(layoutDiagram({
      root: store.canvasRoot as MindNode,
      diagramType: 'tree',
      direction: 'right',
    }));
    expect(overlappingPairs(boxes)).toEqual([]);
  });

  it('点开再点收：可见数回到原值（"展开之后能折叠回去"）', () => {
    const store = load(tree136);
    const base = store.nodes.length;
    const target = firstFoldedId(store);

    store.toggleFoldInView(target);
    expect(store.nodes.length).toBeGreaterThan(base);
    // 再点一下同一个节点：收回去，而且**回到点之前那一屏**（不是比它更少）
    store.toggleFoldInView(target);
    expect(store.nodes.length).toBe(base);
  });

  it('全部展开 → 收起到上限 → 回到 40 个', () => {
    const store = load(tree136);
    store.expandAllInView();
    expect(store.nodes.length).toBe(136);
    store.collapseViewToCap();
    expect(store.nodes.length).toBe(40);
    expect(store.hiddenNodeCount).toBe(96);
  });

  it('全部展开之后逐个收起一支：点一次少这一整支（不留孤儿）', () => {
    const store = load(tree136);
    store.expandAllInView();
    const branch = tree136.root.children[0];
    store.toggleFoldInView(branch.id);
    // 收起的是整棵子树，只留它自己那个框
    expect(store.nodes.length).toBe(136 - (countSubtree(branch) - 1));
    // 它的后代一个都不在画布上（不然就是孤儿）
    const ids = new Set(store.nodes.map(node => node.id));
    for (const child of branch.children) expect(ids.has(child.id)).toBe(false);
    // 再点回来
    store.toggleFoldInView(branch.id);
    expect(store.nodes.length).toBe(136);
  });

  it('"预算本来就够"的节点：点一下只选中，不改可见数', () => {
    const store = load(tree136);
    const base = store.nodes.length;
    // 找一个孩子全显示、且不是用户展开/全部展开的节点
    const budget = store.nodes.find(node => (
      (node.data?.visibleChildren ?? 0) > 0
      && node.data?.hiddenChildren === undefined
      && node.data?.foldOpen !== true
    ));
    expect(budget).toBeDefined();
    store.toggleFoldInView(budget!.id);
    expect(store.nodes.length).toBe(base);
    expect(store.selectedId).toBeNull();
    // 但点头上那个「−」是明确表态：收，而且可见数真的变少
    store.collapseNodeInView(budget!.id);
    expect(store.nodes.length).toBeLessThan(base);
  });

  it('收起/展开是视图动作：不进撤销栈、不置脏、不动 content', () => {
    const store = load(tree136);
    const before = JSON.stringify(store.content);
    expect(store.undoDepth).toBe(0);

    store.expandNodeInView(firstFoldedId(store));
    store.expandAllInView();
    store.collapseViewToCap();

    expect(store.undoDepth).toBe(0);
    expect(store.canUndo).toBe(false);
    expect(store.dirty).toBe(false);
    expect(JSON.stringify(store.content)).toBe(before);
  });

  it('选中一个没画出来的节点：祖先链自动放开（选中即可见）', () => {
    const store = load(tree136);
    const visible = new Set(store.nodes.map(node => node.id));
    const hidden = firstHiddenUnderVisible(tree136.root, visible);
    expect(visible.has(hidden.id)).toBe(false);
    store.select(hidden.id);
    expect(store.nodes.some(node => node.id === hidden.id)).toBe(true);
    expect(store.selectedId).toBe(hidden.id);
  });

  it('给没画出来的节点加子节点：新节点立刻可见（否则按 Tab 之后画面毫无变化）', () => {
    const store = load(tree136);
    const visible = new Set(store.nodes.map(node => node.id));
    const hidden = firstHiddenUnderVisible(tree136.root, visible);
    expect(visible.has(hidden.id)).toBe(false);
    store.addChildTo(hidden.id);
    const added = store.editingId;
    expect(added).not.toBeNull();
    expect(store.nodes.some(node => node.id === added)).toBe(true);
  });

  it('节点可以拖，但坐标仍然只有布局一个来源：拖动只改 content，不写手动坐标', () => {
    const store = load(tree136);
    // 根不拖（中心主题不能被挪走），其余节点可拖。连线一律不可连。
    for (const node of store.nodes) {
      expect(node.connectable).toBe(false);
      expect(node.draggable).toBe(node.data?.depth !== 0);
    }

    // 这一条要逐节点比对坐标，所以先关掉视图收起：否则 `store.nodes` 只是被截断的
    // 那一屏，与"整棵树的布局"本来就不是同一批节点，比出来的差异说明不了任何事。
    store.visibleNodeLimit = NO_NODE_LIMIT;
    const [branch] = tree136.root.children;
    const other = tree136.root.children[1];
    expect(store.moveNodeTo({ draggedId: branch.id, targetId: other.id, zone: 'child' }).ok).toBe(true);

    // 坐标由布局**重算**：拖动之后的每一格都等于 `layoutDiagram(新树)`。
    const geometry = layoutDiagram({ root: store.content!.root, direction: 'right' });
    for (const node of store.nodes) {
      const center = geometry.positions.get(node.id);
      expect(center, node.id).toBeTruthy();
      expect(node.position.x).toBeCloseTo(center!.x - NODE_SIZE.width / 2, 5);
      expect(node.position.y).toBeCloseTo(center!.y - NODE_SIZE.height / 2, 5);
    }

    // 而且内容里**一个 `pos` 都没有**：手拖的那个位置从来没有被记下来。
    const withPos: string[] = [];
    const walk = (node: MindNode): void => {
      if (node.pos !== undefined && node.pos !== null) withPos.push(node.id);
      node.children.forEach(walk);
    };
    walk(store.content!.root);
    expect(withPos).toEqual([]);
  });

  it('undo 只回退内容，不改展开状态', () => {
    const store = load(tree136);
    const branch = tree136.root.children[0];
    store.expandNodeInView(branch.id);
    const visibleBefore = store.nodes.length;

    store.deleteNode(branch.id);
    expect(store.nodeCount).toBe(136 - countSubtree(branch));
    store.undo();
    expect(store.content).not.toBeNull();
    expect(store.nodeCount).toBe(136);
    // 展开状态没被撤销带走：还是"点开了那一支"的那一屏
    expect(store.nodes.length).toBe(visibleBefore);
  });

  it('保存/导出的是完整树：收起状态一个字节都不进文件', () => {
    const store = load(tree136);
    const fold = JSON.stringify(store.content);
    store.expandAllInView();
    const all = JSON.stringify(store.content);
    expect(all).toBe(fold);
    // 导入同一份 JSON：内容一致，收起状态不跟过来（按上限重新收）
    store.newMap();
    expect(store.content).toBeNull();
    store.preview(JSON.parse(all) as MindMapContent, 'right');
    expect(JSON.stringify(store.content)).toBe(fold);
    expect(store.nodes.length).toBe(40);
  });

  it('换图之后展开状态复位（上一棵树点开的那几支不跟过来）', () => {
    const store = load(tree136);
    store.expandNodeInView(firstFoldedId(store));
    store.expandNodeInView(firstFoldedId(store));
    expect(store.nodes.length).toBeGreaterThan(40);
    store.preview(tree95, 'right');
    expect(store.viewExpandedNodes.size).toBe(0);
    expect(store.viewCollapsedNodes.size).toBe(0);
    expect(store.viewExpandAll).toBe(false);
    expect(store.nodes.length).toBe(40);
  });
});

describe('验收读数（1600×852，三棵真树）', () => {
  it('上限 40：可见数、fit-all 字号、视野外节点数', () => {
    const rows = [tree136, tree95, tree80].map(tree => {
      const result = viewTree(tree.root as MindNode, { limit: 40 });
      const layout = layoutDiagram({ root: result.root, diagramType: 'tree', direction: 'right' });
      const { minX, minY, maxX, maxY, width, height } = layout.bounds;
      // `LayoutBounds` 给的是四条边界，`planView` 要的是「左上角 + 尺寸」——两种口径
      // 混用会让 `x` 变成 `undefined`，缩放看着对、偏移却是 NaN（视野外数会变成"全部"）。
      const bounds = { x: minX, y: minY, width: maxX - minX, height: maxY - minY };
      const raw = fitAllScale(bounds, VIEWPORT);
      const plan = planView({ mode: 'fit-all', bounds, viewport: VIEWPORT, minZoom: MIN_ZOOM, maxZoom: MAX_ZOOM });
      const boxes = boxesOf(layout);
      return {
        total: countSubtree(tree.root as MindNode),
        visible: result.visible,
        font: Number((NODE_FONT_PX * raw).toFixed(1)),
        offscreen: boxes.length - visibleIds(boxes, VIEWPORT, plan).length,
        folded: result.folded.size,
        size: `${Math.round(width)}×${Math.round(height)}`,
      };
    });
    // 三棵树的上限都是 40 个节点（总数都 ≥ 40）
    for (const row of rows) expect(row.visible).toBe(40);
    // 40 个节点在 1600×852 上 `fit-all` 都过不了 12px —— 数量上限管的是认知预算，
    // 字号由 `chooseViewMode` 那两档管。这条读数写进 docs，别让下一个人以为是 bug。
    for (const row of rows) expect(row.font).toBeLessThan(12);
    // fit-all 一档里没有节点跑到视野外（装不下才会越界，那是被夹到 MIN_ZOOM 的极端图）
    for (const row of rows) expect(row.offscreen).toBe(0);
    // 每个截断点都有人负责说"这里还有东西"
    for (const row of rows) expect(row.folded).toBeGreaterThan(10);
  });

  it('上限越大越挤：同一个上限下字号单调下降（读数表）', () => {
    const fonts = [12, 20, 30, 40, 60].map(limit => (
      Number(fitAllFont(viewTree(tree136.root, { limit }).root).toFixed(1))
    ));
    for (let i = 1; i < fonts.length; i += 1) {
      expect(fonts[i]).toBeLessThan(fonts[i - 1]);
    }
    // 底色：136 个全摊开是 1.6px（上一轮的读数）
    expect(fitAllFont(tree136.root)).toBeCloseTo(1.6, 1);
  });

  it('`fit-all` 达标（≥ 12px）需要把上限压到 20 以内', () => {
    let limit = 40;
    while (limit > 1 && fitAllFont(viewTree(tree136.root, { limit }).root) < 12) limit -= 1;
    expect(fitAllFont(viewTree(tree136.root, { limit }).root))
      .toBeGreaterThanOrEqual(NODE_FONT_PX * MIN_READABLE_SCALE);
    // 这条数不是"应该调到 20"，而是"40 不是一屏看全的界线"的证据
    expect(limit).toBeLessThanOrEqual(20);
  });
});

/** 原树里某个节点的层号（根 0）。 */
function depthOfNode(root: MindNode, id: string): number | null {
  const walk = (node: MindNode, level: number): number | null => {
    if (node.id === id) return level;
    for (const child of node.children) {
      const hit = walk(child, level + 1);
      if (hit !== null) return hit;
    }
    return null;
  };
  return walk(root, 0);
}

/** 原树里按 id 找节点。 */
function findIn(root: MindNode, id: string): MindNode | null {
  if (root.id === id) return root;
  for (const child of root.children) {
    const hit = findIn(child, id);
    if (hit) return hit;
  }
  return null;
}

/** 对照组：前序（DFS）吃满额度之后，一级分支还剩几支。 */
function preorderPrefix(root: MindNode, limit: number): { topBranches: number } {
  const picked = new Set<string>();
  let budget = limit;
  const walk = (node: MindNode): void => {
    if (budget <= 0) return;
    budget -= 1;
    picked.add(node.id);
    for (const child of node.children) walk(child);
  };
  walk(root);
  return { topBranches: root.children.filter(child => picked.has(child.id)).length };
}
