import { describe, expect, it } from 'vitest';

import { REAL_TREES } from './fixtures/real-trees';
import type { MindNode } from '../src/api/types';
import { boxesOf, chooseViewMode, depthIndex } from '../src/canvas/clarity';
import { layoutDiagram } from '../src/canvas/layout';
import { MIN_READABLE_SCALE, NODE_FONT_PX, fitAllScale } from '../src/canvas/viewport';

/**
 * 全展开时的可读性基线：**这几棵树整棵摊在 1600×852 上，字是不可读的**。
 *
 * 这一组读数的作用是把"为什么要在渲染层收起"钉成可复现的数字，而不是一句说法。
 * 三个数都是量出来的（真树来自 `backend/data/mindmaps.db`，见 fixture 文件头）：
 *
 * | 节点 | 外接框 | `fit-all` 缩放 | 字号 |
 * | --- | --- | --- | --- |
 * | 136 | 1124×6236 | 0.114 | **1.6px** |
 * | 95 | 888×4944 | 0.144 | **2.0px** |
 * | 80 | 888×3244 | 0.219 | **3.1px** |
 *
 * 判据是 `MIN_READABLE_SCALE`（`12 / 14`）。这些树连"缩到 0.857"都远远够不上，
 * 而默认档 `fit-width` 只能保证字够大——代价是 100+ 个节点在视野之外。
 *
 * 用例写在这里而不是某个组件用例里：它测的是**数据形状 + 布局**，与渲染层无关；
 * 收起之后的对照读数在 `tests/view-collapse.spec.ts`。
 */
const VIEWPORT = { width: 1600, height: 852 };

/** 节点数 / 层深：这份读数要能独立成立，所以不借收起模块里的同名工具。 */
function countNodes(node: MindNode): number {
  return 1 + node.children.reduce((sum, child) => sum + countNodes(child), 0);
}

function depthOf(node: MindNode): number {
  return node.children.length ? 1 + Math.max(...node.children.map(depthOf)) : 0;
}

function metrics(content: (typeof REAL_TREES)[string]) {
  const root = content.root as MindNode;
  const layout = layoutDiagram({ root, diagramType: 'tree', direction: 'right' });
  const { minX, minY, width, height } = layout.bounds;
  const zoom = fitAllScale({ x: minX, y: minY, width, height }, VIEWPORT);
  return {
    nodes: countNodes(root),
    depth: depthOf(root),
    width,
    height,
    zoom,
    font: NODE_FONT_PX * zoom,
    choice: chooseViewMode({ boxes: boxesOf(layout), viewport: VIEWPORT }),
  };
}

describe('全展开的可读性基线（这一轮收起的理由）', () => {
  it('136 节点 / 6 支：整棵塞进视口只有 1.6px 的字', () => {
    const m = metrics(REAL_TREES.tree136);
    expect(m.nodes).toBe(136);
    expect(m.depth).toBe(4);
    expect(Math.round(m.width)).toBe(1124);
    expect(Math.round(m.height)).toBe(6236);
    expect(Number(m.zoom.toFixed(3))).toBe(0.114);
    expect(Number(m.font.toFixed(1))).toBe(1.6);
    // 连可读下限的四分之一都够不到
    expect(m.zoom).toBeLessThan(MIN_READABLE_SCALE / 4);
  });

  it('默认档只能"看清"半张图：112 个节点在视野之外', () => {
    const m = metrics(REAL_TREES.tree136);
    expect(m.choice.mode).toBe('fit-width');
    expect(m.choice.fontPx).toBe(NODE_FONT_PX * MIN_READABLE_SCALE);
    // 136 个节点里只有 24 个落在这一屏（`visibleIds` 的矩形相交口径）
    expect(m.choice.offscreen).toBe(112);
  });

  it('95 与 80 节点两份真树同样不可读（不是一棵树的个例）', () => {
    for (const tree of [REAL_TREES.tree95, REAL_TREES.tree80]) {
      const m = metrics(tree);
      expect(m.zoom).toBeLessThan(MIN_READABLE_SCALE);
      expect(m.choice.mode).toBe('fit-width');
      expect(m.choice.offscreen).toBeGreaterThan(40);
    }
  });

  it('宽度不是瓶颈，**高度**是：每层兄弟沿竖轴摊开，叶子数决定画布多高', () => {
    const m = metrics(REAL_TREES.tree136);
    // 6 层深 × 236 = 1416 的宽度远小于 1600；而 75 个叶子沿 y 排开就是 6000+ 像素
    expect(m.width).toBeLessThan(VIEWPORT.width);
    expect(m.height).toBeGreaterThan(VIEWPORT.height * 6);
    // 最后一层有多宽：这就是"按层收起"能立竿见影的原因
    const layers = depthIndex(REAL_TREES.tree136.root as MindNode);
    const counts = new Map<number, number>();
    for (const level of layers.values()) counts.set(level, (counts.get(level) ?? 0) + 1);
    expect(counts.get(1)).toBe(1);
    expect(counts.get(2)).toBe(6);
    expect(counts.get(5)).toBe(75);
  });
});
