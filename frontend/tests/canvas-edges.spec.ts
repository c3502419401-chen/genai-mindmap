import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeAll, beforeEach, describe, expect, it } from 'vitest';

import type { MindMapContent } from '../src/api/types';
import MindMapCanvas from '../src/canvas/MindMapCanvas.vue';
import { layoutFishbone } from '../src/canvas/layout/fishbone';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * 画布真的把三种连线画出来了（真挂 `@vue-flow/core`，不 mock）。
 *
 * 这一组用例补的是"布局算对了"与"屏幕上画出来了"之间的那条缝：`type: 'fishbone'`
 * 这个字符串有没有注册到 `<template #edge-…>`、Vue Flow 认不认这几个句柄、`data`
 * 有没有原样传到组件里——这些只有真渲染一次才知道。
 *
 * jsdom 缺两个浏览器 API，补上最小实现：`ResizeObserver`（Vue Flow 量节点用）与
 * `DOMMatrixReadOnly`（它从 viewport 的 `style.transform` 里读缩放）。
 */
beforeAll(() => {
  const scope = globalThis as unknown as Record<string, unknown>;
  scope.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (window as any).DOMMatrixReadOnly = class {
    m22 = 1;
  };
});

function mountCanvas(): VueWrapper {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(MindMapCanvas, { global: { plugins: [pinia] } });
}

/** 等 Vue Flow 把节点/边挂完（它有自己的一轮测量，nextTick 一次不够）。 */
async function settle(wrapper: VueWrapper) {
  for (let i = 0; i < 4; i += 1) {
    await wrapper.vm.$nextTick();
    await new Promise(resolve => setTimeout(resolve, 0));
  }
}

const fishboneContent: MindMapContent = {
  title: '交付延期',
  diagram_type: 'fishbone',
  root: {
    id: 'r',
    label: '交付延期',
    children: ['人', '法'].map(name => ({
      id: name,
      label: name,
      children: [{ id: `${name}1`, label: `${name}原因`, children: [] }],
    })),
  },
};

const treeContent: MindMapContent = {
  title: '树',
  root: {
    id: 'r',
    label: '根',
    children: [{ id: 'a', label: '甲', children: [] }],
  },
};

beforeEach(() => {
  setActivePinia(createPinia());
});

/**
 * 为什么这一组有 10 秒显式上限（判 **A 规模型**：真挂 `@vue-flow/core` 的固有成本）。
 *
 * 实测（`docs/TEST-LOAD-SENSITIVITY.md` 的表，同一天同一份代码）：
 *
 *   鱼骨那条：单文件串行 151ms ／ 全量并行·空闲 1450ms ／ 全量并行 + 后端 pytest 1956ms
 *
 * 放大 ~13× 几乎全在**全量并行**那一层（单跑 → 全量），不是外部负载。三条用例每条都
 * `mount(MindMapCanvas)` 一次，Vue Flow 的挂载 + 自己那轮测量是固有成本；这一组测的正是
 * "真挂上去之后边画出来了没有"，换成 stub 就没有断言了——所以 **b（减负载）已经用完**
 * （壳早已是最小的 `MindMapCanvas`，不再是 `App` 壳，见 `canvas-preview.spec.ts` 的注释）。
 *
 * 10 秒 = 满载 1956 × 5.1（也 = 满载 × 2 的 2.6 倍），与 `canvas-preview.spec.ts` 同一尺度；
 * 按 CI runner 慢 1.3–2× 折算仍有 2.6–3.9× 余量。
 */
describe('画布上的三种连线', { timeout: 10_000 }, () => {
  it('鱼骨：骨线与短线都进了 SVG，点的坐标就是布局算出来的那些', async () => {
    const wrapper = mountCanvas();
    useMindMapStore().preview(fishboneContent, 'right');
    await settle(wrapper);

    const bones = wrapper.findAll('g.vue-flow__edge-fishbone');
    expect(bones).toHaveLength(4); // 两根骨 + 两条原因短线

    const paths = wrapper.findAll('path.vue-flow__edge-path').map(item => item.attributes('d') ?? '');
    const expected = layoutFishbone(fishboneContent.root);
    for (const [id, points] of expected.fishbone.links) {
      const d = points
        .map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`)
        .join(' ');
      expect(paths, `${id} 的折线没画出来`).toContain(d);
    }
    // 骨端确实是 60°：主骨→人那一段的水平位移 = 竖直位移 / tan60
    const bone = paths.find(item => item.startsWith('M -90.00 0.00 L -138.00 0.00'));
    expect(bone, '第一根骨的折线').toBeDefined();
    const points = [...bone!.matchAll(/(-?\d+\.\d+) (-?\d+\.\d+)/gu)]
      .map(match => ({ x: Number(match[1]), y: Number(match[2]) }));
    expect(points).toHaveLength(3);
    const anchor = points[1]!;
    const tip = points[2]!;
    expect(anchor.y).toBe(0); // 锚点在主骨上
    expect(Math.abs(tip.x - anchor.x) / Math.abs(tip.y - anchor.y))
      .toBeCloseTo(1 / Math.tan(Math.PI / 3), 4);
  });

  it('树：父子线仍然是内置的 smoothstep，没有一条鱼骨线', async () => {
    const wrapper = mountCanvas();
    useMindMapStore().preview(treeContent, 'right');
    await settle(wrapper);

    expect(wrapper.findAll('g.vue-flow__edge-fishbone')).toHaveLength(0);
    expect(wrapper.findAll('g.vue-flow__edge-smoothstep').length).toBeGreaterThan(0);
    // 树上一个箭头都没有：主骨末端那个三角形是鱼骨独有的
    expect(wrapper.findAll('polygon.fishbone-spine-arrow')).toHaveLength(0);
  });

  it('鱼骨：主骨末端的箭头只画一份，且落在它自己那条骨线的组里', async () => {
    const wrapper = mountCanvas();
    useMindMapStore().preview(fishboneContent, 'right');
    await settle(wrapper);

    const arrows = wrapper.findAll('polygon.fishbone-spine-arrow');
    // 主骨那一小段被两条骨线各画了一遍；箭头只有一份（布局指定锚点最靠右的那条）
    expect(arrows).toHaveLength(1);
    expect(arrows[0]!.attributes('points')).toBe('-90.00,0.00 -102.00,-5.00 -102.00,5.00');
    // 它确实画在 `r->人` 那条边的 `<g>` 里，而不是飘在画布上
    const group = arrows[0]!.element.closest('g.vue-flow__edge-fishbone');
    expect(group).not.toBeNull();
    const ownerPath = layoutFishbone(fishboneContent.root).fishbone.links.get('r->人')!;
    const d = ownerPath
      .map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`)
      .join(' ');
    expect(group!.querySelector('path.vue-flow__edge-path')?.getAttribute('d')).toBe(d);
  });

  it('流程图：附加边画成 mindEdge（带 kind，能换色/换线型）', async () => {
    const wrapper = mountCanvas();
    useMindMapStore().preview({
      title: '流程',
      diagram_type: 'flowchart',
      root: {
        id: 'r',
        label: '报销',
        children: [
          { id: 'a', label: '提交申请', children: [] },
          { id: 'b', label: '形式审查', children: [] },
        ],
      },
      edges: [{ id: 'e1', source: 'b', target: 'a', label: '驳回', kind: 'loop' }],
    }, 'right');
    await settle(wrapper);

    expect(wrapper.findAll('g.vue-flow__edge-mindEdge')).toHaveLength(1);
    // 回退边绕左侧：SVG 里那条线从两端节点的左沿出发（句柄 id 写在 g 的 data 上）
    const attach = wrapper.find('g.vue-flow__edge-mindEdge');
    expect(attach.exists()).toBe(true);
  });
});
