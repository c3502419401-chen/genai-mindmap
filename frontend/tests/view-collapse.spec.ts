import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import { REAL_TREES } from './fixtures/real-trees';
import type { MindMapContent, MindNode } from '../src/api/types';
import { boxesOf, chooseViewMode, overlappingPairs } from '../src/canvas/clarity';
import { layoutDiagram } from '../src/canvas/layout';
import { NODE_SIZE } from '../src/canvas/layout/tidy';
import {
  DEFAULT_EXPAND_DEPTH,
  EXPAND_ALL_DEPTH,
  chooseExpandDepth,
  countSubtree,
  depthOf,
  viewTree,
} from '../src/canvas/view-collapse';
import { MIN_READABLE_SCALE, NODE_FONT_PX, fitAllScale } from '../src/canvas/viewport';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * 视图层收起：**数据不动，只是这一屏默认不把每一个节点都摊开**。
 *
 * 这一组用例钉的四件事，正好是这个功能的四条失败模式：
 *
 * 1. 收起之后**看得清**——可见节点数掉下来、`fit-all` 的字号回到可读下限之上；
 * 2. 收起**不是看不见**——每个折叠点都有「还有 N 个」，点开就出来，且不与现有节点重叠；
 * 3. 收起**不动数据**——`content` / 保存 / 导出 / 撤销栈一个字节都不受影响，
 *    全展开时坐标与改动之前**逐像素相同**；
 * 4. 视图动作**不进撤销栈**——Ctrl+Z 撤的是内容，不是"我刚点了一下展开"。
 *
 * 树用 `tests/fixtures/real-trees.ts` 里从库里原样导出的真树（136 / 95 / 80 节点），
 * 不用现造的等宽树：收起的判据对分支宽度与层深极敏感，造出来的形状量不出真结论。
 */

const VIEWPORT = { width: 1600, height: 852 };

const tree136 = REAL_TREES.tree136 as MindMapContent;
const tree95 = REAL_TREES.tree95 as MindMapContent;
const tree80 = REAL_TREES.tree80 as MindMapContent;

function nodeCount(root: MindNode): number {
  return countSubtree(root);
}

/** 某个深度下 `fit-all` 的字号（CSS 像素）。收起前后对比的就是这个数。 */
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

beforeEach(() => {
  setActivePinia(createPinia());
});

describe('视图树：按层收起，节一个都不少', () => {
  it('不收起时原样返回入参（引用相等，folded 为空）', () => {
    const result = viewTree(tree136.root, { expandDepth: EXPAND_ALL_DEPTH });
    expect(result.root).toBe(tree136.root);
    expect(result.folded.size).toBe(0);
  });

  it('默认一层：136 节点 → 根 + 6 支可见，6 个折叠点', () => {
    const result = viewTree(tree136.root, { expandDepth: DEFAULT_EXPAND_DEPTH });
    expect(nodeCount(result.root)).toBe(1 + tree136.root.children.length);
    expect(result.folded.size).toBe(6);
    // 每个一级分支在真树里各挂 3 个子节点（见 fixture 的 per_depth：1 / 6 / 18 / 36 / 75）
    for (const info of result.folded.values()) {
      expect(info.children).toBe(3);
    }
    // 整支的节点数必须与真树上那一段算出来的一致
    const branch = tree136.root.children[0];
    expect(result.folded.get(branch.id)?.subtree).toBe(nodeCount(branch) - 1);
  });

  it('收起只换 children，不删节点、不改层号', () => {
    const before = JSON.stringify(tree136.root);
    const result = viewTree(tree136.root, { expandDepth: 1 });
    // 原树一个字节都没变
    expect(JSON.stringify(tree136.root)).toBe(before);
    // 可见节点的层号与真树一致（收起不压缩层号）
    const depths = new Map<string, number>();
    const walk = (node: MindNode, level: number): void => {
      depths.set(node.id, level);
      for (const child of node.children) walk(child, level + 1);
    };
    walk(result.root, 0);
    for (const level of depths.values()) expect(level).toBeLessThanOrEqual(1);
    // 一个节点都没丢：可见 + 藏起来的 = 内容总数
    const foldedNodes = [...result.folded.values()]
      .reduce((sum, info) => sum + info.subtree, 0);
    expect(nodeCount(result.root) + foldedNodes).toBe(nodeCount(tree136.root));
  });

  it('点开一个折叠点：它的直接子节点出来，带子的成为新的折叠点', () => {
    const branch = tree136.root.children[0];
    const result = viewTree(tree136.root, {
      expandDepth: 1,
      expanded: new Set([branch.id]),
    });
    const opened = result.root.children.find(child => child.id === branch.id);
    expect(opened?.children.length).toBe(branch.children.length);
    // 有子节点的那些子节点现在各自是折叠点；真树里也有叶子，叶子不该有条目
    const withKids = branch.children.filter(child => child.children.length > 0);
    expect(withKids.length).toBeGreaterThan(0);
    for (const child of branch.children) {
      if (child.children.length) {
        expect(result.folded.get(child.id)?.children).toBe(child.children.length);
      } else {
        expect(result.folded.has(child.id)).toBe(false);
      }
    }
  });

  it('没被收起的那部分保持原引用（下游的引用相等判断不被视图层搅乱）', () => {
    const result = viewTree(tree136.root, { expandDepth: 0 });
    // 根被复制了（它的 children 被换空），但折叠表里有它
    expect(result.root).not.toBe(tree136.root);
    expect(result.folded.get(tree136.root.id)?.children).toBe(tree136.root.children.length);
    // 叶子节点没有条目：折叠点是"有子节点但没画出来"的那些，不是所有节点
    expect(result.folded.has(tree136.root.children[0].children[0].id)).toBe(false);
  });
});

describe('展开深度由可读性反推', () => {
  it('136 节点在 1600×852 上只展开一层，字号回到 25.6px', () => {
    const depth = chooseExpandDepth({ root: tree136.root, viewport: VIEWPORT });
    expect(depth).toBe(1);
    const visible = viewTree(tree136.root, { expandDepth: depth }).root;
    // 4–5px → 25.6px：这条就是验收判据（≥ 12px）
    expect(fitAllFont(visible)).toBeGreaterThanOrEqual(NODE_FONT_PX * MIN_READABLE_SCALE);
    expect(fitAllFont(visible)).toBeCloseTo(25.6, 1);
    expect(nodeCount(visible)).toBe(7);
  });

  it('小树不收起：所有节点都装得下就一个都不藏', () => {
    const small: MindNode = {
      id: 'r',
      label: '根',
      children: [
        { id: 'a', label: 'A', children: [{ id: 'a1', label: 'A1', children: [] }] },
        { id: 'b', label: 'B', children: [] },
      ],
    };
    expect(chooseExpandDepth({ root: small, viewport: VIEWPORT })).toBe(EXPAND_ALL_DEPTH);
    // 全展开的判定与"整张图装得下且读得出来"是同一条算术
    expect(fitAllFont(small)).toBeGreaterThanOrEqual(NODE_FONT_PX * MIN_READABLE_SCALE);
  });

  it('返回的深度就是能让 `fit-all` 达标的**最浅**一档（再深一层就不达标）', () => {
    for (const tree of [tree136, tree95, tree80]) {
      const root = tree.root as MindNode;
      const depth = chooseExpandDepth({ root, viewport: VIEWPORT });
      const at = fitAllFont(viewTree(root, { expandDepth: depth }).root);
      expect(at).toBeGreaterThanOrEqual(NODE_FONT_PX * MIN_READABLE_SCALE);
      if (Number.isFinite(depth) && depth < depthOf(root)) {
        const deeper = fitAllFont(viewTree(root, { expandDepth: depth + 1 }).root);
        expect(deeper).toBeLessThan(NODE_FONT_PX * MIN_READABLE_SCALE);
      }
    }
  });
});

describe('store：画布上的可见数与字号', () => {
  function load(content: MindMapContent): ReturnType<typeof useMindMapStore> {
    const store = useMindMapStore();
    store.preview(content, 'right');
    return store;
  }

  it('装上 136 节点的真图：画布 7 个节点，内容仍是 136 个', () => {
    const store = load(tree136);
    expect(store.nodeCount).toBe(136);
    expect(store.nodes.length).toBe(7);
    expect(store.hiddenNodeCount).toBe(129);
    // 顶栏那个「N 个节点」说的是内容，不是这一屏——两个数都对，只是不同的问题
    expect(store.flatNodes.length).toBe(7);
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
    const childLines = store.edges.filter(edge => !edge.id.startsWith('relation:'));
    expect(childLines.length).toBe(135);
  });

  it('点开一个折叠点：子节点出现，且不与现有节点重叠', () => {
    const store = load(tree136);
    const branch = tree136.root.children[0];
    store.expandNodeInView(branch.id);
    expect(store.nodes.length).toBe(7 + branch.children.length);

    // 重排之后不许出现重叠（严格相交为 0；相切是紧凑排布，不算）
    const boxes = boxesOf(layoutDiagram({
      root: store.canvasRoot as MindNode,
      diagramType: 'tree',
      direction: 'right',
    }));
    const visible = new Set(store.nodes.map(node => node.id));
    const pairs = overlappingPairs(boxes.filter(box => visible.has(box.id)));
    expect(pairs).toEqual([]);
  });

  it('收起/展开是视图动作：不进撤销栈、不置脏、不动 content', () => {
    const store = load(tree136);
    const before = JSON.stringify(store.content);
    expect(store.undoDepth).toBe(0);

    store.expandNodeInView(tree136.root.children[0].id);
    store.expandAllInView();
    store.collapseViewToDefault();

    expect(store.undoDepth).toBe(0);
    expect(store.canUndo).toBe(false);
    expect(store.dirty).toBe(false);
    expect(JSON.stringify(store.content)).toBe(before);
  });

  it('选中一个被藏起来的节点：祖先链自动放开（选中即可见）', () => {
    const store = load(tree136);
    const deep = tree136.root.children[0].children[0].children[0];
    expect(store.nodes.some(node => node.id === deep.id)).toBe(false);
    store.select(deep.id);
    expect(store.nodes.some(node => node.id === deep.id)).toBe(true);
    expect(store.selectedId).toBe(deep.id);
  });

  it('给深层节点加子节点：新节点立刻可见（否则按 Tab 之后画面毫无变化）', () => {
    const store = load(tree136);
    const deep = tree136.root.children[0].children[0];
    expect(store.nodes.some(node => node.id === deep.id)).toBe(false);
    store.addChildTo(deep.id);
    const added = store.editingId;
    expect(added).not.toBeNull();
    expect(store.nodes.some(node => node.id === added)).toBe(true);
  });

  it('拖不动节点（画布上 `nodes-draggable=false`），所以坐标只有布局一个来源', () => {
    const store = load(tree136);
    // 节点既不可拖也不可连：`pos` 在渲染层没有任何读者（全仓只在契约类型里出现）。
    // 收起/展开后坐标一律由 `layoutDiagram` 重算，不存在"收起时坐标存哪"这个问题。
    for (const node of store.nodes) {
      expect(node.draggable).toBe(false);
      expect(node.connectable).toBe(false);
    }
  });

  it('undo 只回退内容，不改展开状态', () => {
    const store = load(tree136);
    const branch = tree136.root.children[0];
    store.expandNodeInView(branch.id);
    const visibleBefore = store.nodes.length;

    // 删一个可见节点（内容动作，进撤销栈）
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
    // 导入同一份 JSON：内容一致，收起状态不跟过来（按默认深度重新收）
    store.newMap();
    expect(store.content).toBeNull();
    store.preview(JSON.parse(all) as MindMapContent, 'right');
    expect(JSON.stringify(store.content)).toBe(fold);
    expect(store.nodes.length).toBe(7);
  });

  it('换图之后展开状态复位（上一棵树手动点开的那几支不跟过来）', () => {
    const store = load(tree136);
    store.expandNodeInView(tree136.root.children[0].id);
    store.expandNodeInView(tree136.root.children[1].id);
    expect(store.nodes.length).toBeGreaterThan(7);
    store.preview(tree95, 'right');
    expect(store.viewExpandedNodes.size).toBe(0);
    expect(store.expandDepth).toBe(DEFAULT_EXPAND_DEPTH);
    expect(store.nodes.length).toBe(1 + tree95.root.children.length);
  });
});

describe('画布上的选择：收起之后仍然"看见的才算数"', () => {
  it('`chooseViewMode` 在收起之后给出 fit-all（整张图装得下且字可读）', () => {
    const store = useMindMapStore();
    store.preview(tree136, 'right');
    const layout = layoutDiagram({
      root: store.canvasRoot as MindNode,
      diagramType: 'tree',
      direction: 'right',
    });
    const folded = viewTree(tree136.root, { expandDepth: store.expandDepth });
    const visible = boxesOf(layoutDiagram({
      root: folded.root,
      diagramType: 'tree',
      direction: 'right',
    })).filter(box => box.id !== undefined);
    expect(visible.length).toBe(7);
    const choice = chooseViewMode({ boxes: visible, viewport: VIEWPORT });
    expect(choice.mode).toBe('fit-all');
    expect(choice.fontPx).toBeGreaterThanOrEqual(12);
    expect(choice.offscreen).toBe(0);
    // 未收起时同一条判据给出的是 fit-width + 112 个节点在视野外
    const raw = chooseViewMode({ boxes: boxesOf(layout), viewport: VIEWPORT });
    expect(raw.mode).toBe('fit-width');
    expect(raw.offscreen).toBeGreaterThan(100);
  });
});
