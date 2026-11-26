import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import { KEEP_MARGIN, keepTarget } from '../src/canvas/viewport';
import { useMindMapStore } from '../src/stores/mindmap';
import type { MindMapContent } from '../src/api/types';

/**
 * 展开一支之后，视口该往哪儿去。
 *
 * 这一组钉的是用户报的那句话：「现在每次展开都会回到主节点那里」。根因在画布那一步
 * ——展开之后调用的是**整张图的适配**（`applyView` → `planView`）：图变大，`fit-all`
 * 就缩到看不清，`fit-width` 又锚回根节点。两种档位都把屏幕中央从"我刚点开的那一支"
 * 换成"这张图的主角"，而用户想看的是**新长出来的那几个子节点**。
 *
 * 所以判据改成两条，写成一个纯函数（`viewport.keepTarget`）：
 *
 *  1. 目标完整在视野里、四周留白够 → **返回 `null`，一个像素都不动**；
 *  2. 否则保持**当前缩放**，只平移，把目标放到正中。
 *
 * 纯函数而不是组件内逻辑：jsdom 里 Vue Flow 的 `dimensions` 恒为 0，组件级的展开
 * 动作测不出任何视口变化（`tests/canvas-auto-view.spec.ts` 的整块假件就是为这件事
 * 存在的）。算术单独拎出来，才能逐条钉死"缩放不变"这条最要紧的性质。
 */

/** 树与鱼骨的节点尺寸（`layout/tidy.ts::NODE_SIZE` 同口径）。 */
const NODE = { width: 160, height: 44 };

function rect(x: number, y: number) {
  return { ...NODE, x, y };
}

beforeEach(() => {
  setActivePinia(createPinia());
});

describe('展开之后：把这一支留在视野里', () => {
  it('目标已经在视野里且留白够 → 一个像素都不动', () => {
    const next = keepTarget({
      rect: rect(300, 200),
      viewport: { width: 1600, height: 852 },
      current: { x: 0, y: 0, zoom: 1 },
    });
    expect(next).toBeNull();
  });

  it('目标缩到视口左上角外 → 保持缩放到小数位不变，把它挪回正中', () => {
    const current = { x: 0, y: 0, zoom: 1.4 };
    const next = keepTarget({
      rect: rect(-400, -300),
      viewport: { width: 1600, height: 852 },
      current,
    });
    expect(next).not.toBeNull();
    // **缩放必须原样保留**：这一条就是"展开不该像整图适配那样把字缩小"。
    expect(next!.zoom).toBe(current.zoom);
    // 目标的中心落在视口中心上（画布坐标 → 屏幕坐标 = point * zoom + offset）。
    const cx = (-400 + NODE.width / 2) * current.zoom + next!.x;
    const cy = (-300 + NODE.height / 2) * current.zoom + next!.y;
    expect(cx).toBeCloseTo(1600 / 2, 6);
    expect(cy).toBeCloseTo(852 / 2, 6);
  });

  it('只贴边一点点也算"看不见"：右边留白不足时照样挪', () => {
    // 左边缘刚好压在 KEEP_MARGIN 上、右边超出视口 —— 展开的子节点一定在视野外。
    const next = keepTarget({
      rect: rect(1600 - NODE.width, 100),
      viewport: { width: 1600, height: 852 },
      current: { x: 0, y: 0, zoom: 1 },
    });
    expect(next).not.toBeNull();
    expect(next!.zoom).toBe(1);
    expect(KEEP_MARGIN).toBeGreaterThan(0);
  });

  it('容器还没量出来 / 缩放非法 → 什么都不做（而不是搬到 (0,0)）', () => {
    const rect0 = rect(100, 100);
    expect(keepTarget({ rect: rect0, viewport: { width: 0, height: 0 },
      current: { x: 0, y: 0, zoom: 1 } })).toBeNull();
    expect(keepTarget({ rect: rect0, viewport: { width: 1600, height: 852 },
      current: { x: 0, y: 0, zoom: 0 } })).toBeNull();
  });
});

describe('折叠动作要能被画布看见', () => {
  it('`toggleFoldInView` 回报刚做的那一下，画布据此决定动不管视口', () => {
    const longTree: MindMapContent = {
      title: '长图',
      root: {
        id: 'r', label: '根',
        children: Array.from({ length: 4 }, (_, i) => ({
          id: `b${i}`,
          label: `分支${i}`,
          children: Array.from({ length: 6 }, (_, j) => ({
            id: `b${i}-${j}`, label: `叶子${i}-${j}`, children: [],
          })),
        })),
      },
    };
    const store = useMindMapStore();
    store.preview(longTree, 'right');
    // 现状返回 `undefined`：画布拿不到"刚才是展开还是收起"，只能一律重新适配
    // ——那正是"点一下角标，视图跳回根节点"的另一半。
    expect(['expand', 'collapse', 'none']).toContain(store.toggleFoldInView('b0'));
  });
});
