import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { ref } from 'vue';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MindMapContent } from '../src/api/types';
import {
  MAX_ZOOM,
  MIN_READABLE_FONT_PX,
  MIN_READABLE_SCALE,
  NODE_FONT_PX,
  contentBounds,
  coverage,
  fitAllScale,
  paddingOf,
  planView,
} from '../src/canvas/viewport';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * 视口算术与两档视图。
 *
 * 这一组用例钉的是"默认看不清"那个 bug 的判据本身：**字号下限是从 14px 反推的**
 * （`12 / 14 = 0.857`），不是拍出来的一个 0.9。所以第一条用例就是那条算式，
 * 后面所有缩放断言都以它为准。
 *
 * 画布整体用假 `@vue-flow/core`（jsdom 里量不出画布尺寸，真 Vue Flow 的
 * `dimensions` 恒为 0，`planView` 会直接返回早退，测不到任何东西）。假件只做两件事：
 * 把视口尺寸给成真值、把 `setViewport` 的参数记下来。
 */

/**
 * 假画布记住**当前**视口：真 `setViewport` 之后 `getViewport()` 就返回新值，
 * 而"装不下"那句提示是按真实视口算的（`updateClippedHint`），假件必须同形。
 */
let currentViewport = { x: 12, y: 34, zoom: 0.93 };
const setViewport = vi.fn(
  async (transform: { x: number; y: number; zoom: number }, _options?: { duration?: number }) => {
    currentViewport = transform;
    return true;
  },
);
const getViewport = vi.fn(() => currentViewport);
const dimensions = ref({ width: 1600, height: 852 });

/** 最近一次真正写进画布的变换。 */
function lastViewport(): { x: number; y: number; zoom: number } {
  const calls = setViewport.mock.calls;
  return calls[calls.length - 1][0];
}

vi.mock('@vue-flow/core', () => ({
  VueFlow: { name: 'VueFlowStub', template: '<div class="vue-flow-stub" />' },
  Handle: { name: 'HandleStub', template: '<div />' },
  Position: { Left: 'left', Right: 'right', Top: 'top', Bottom: 'bottom' },
  useVueFlow: () => ({
    dimensions,
    setViewport,
    getViewport,
    setCenter: vi.fn(),
    onNodeClick: vi.fn(),
    onNodeDoubleClick: vi.fn(),
    onPaneClick: vi.fn(),
    onMoveEnd: vi.fn(),
    updateNodeInternals: vi.fn(),
  }),
}));

vi.mock('@vue-flow/background', () => ({
  Background: { name: 'BackgroundStub', template: '<div />' },
}));

const MindMapCanvas = (await import('../src/canvas/MindMapCanvas.vue')).default;

/** 一棵纵向很长的树：42 个节点、宽 888、高约 1952——线上那张图的形状。 */
function longTree(): MindMapContent {
  const branches = Array.from({ length: 6 }, (_, i) => ({
    id: `b${i}`,
    label: `分支${i}`,
    children: Array.from({ length: 6 }, (_, j) => ({
      id: `b${i}-${j}`,
      label: `叶子${i}-${j}`,
      children: [],
    })),
  }));
  return { title: '长图', root: { id: 'r', label: '根', children: branches } };
}

const wrappers: VueWrapper[] = [];

async function mountWith(content: MindMapContent): Promise<VueWrapper> {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(MindMapCanvas, { global: { plugins: [pinia] } });
  wrappers.push(wrapper);
  const store = useMindMapStore();
  store.preview(content, 'right');
  // **全展开**：这一组用例钉的是视口算术与两档切换——"装不下的时候怎么办"。它的
  // 前提是画布上画着整张图；视图层默认只摊开一层（`canvas/view-collapse.ts`）会让
  // 竖长的图变得装得下，于是这组断言测的就不再是它要测的东西。收起的读数在
  // `tests/view-collapse.spec.ts` 里，两边互不遮蔽。
  store.expandAllInView();
  // 两拍：`nodeCount` 的 watch 在微任务里跑，它排那一枪 150ms 的节流；
  // 定时器要等它排上之后才推得动。
  await flushPromises();
  vi.advanceTimersByTime(200);
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  vi.useFakeTimers();
  setViewport.mockClear();
  getViewport.mockClear();
  dimensions.value = { width: 1600, height: 852 };
});

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount());
  vi.useRealTimers();
});

describe('可读字号下限的算术', () => {
  it('12 / 14：下限是推导出来的，不是拍的', () => {
    expect(NODE_FONT_PX).toBe(14);
    expect(MIN_READABLE_FONT_PX).toBe(12);
    expect(MIN_READABLE_SCALE).toBeCloseTo(0.857142857, 9);
    // 判据本身：把字号乘回去，正好是 12 像素。
    expect(NODE_FONT_PX * MIN_READABLE_SCALE).toBeCloseTo(MIN_READABLE_FONT_PX, 9);
  });

  it('边距公式与 Vue Flow 的 `padding: 0.2` 逐像素相同', () => {
    // floor((1600 - 1600/1.2) * 0.5) = floor(133.3) = 133
    expect(paddingOf(1600)).toBe(133);
    expect(paddingOf(852)).toBe(71);
  });
});

describe('两档的缩放', () => {
  /** 42 个节点的树：横 888（同列最宽 9 个 180 宽的节点），竖 1952。 */
  const bounds = { x: -90, y: -844, width: 909, height: 1952 };
  const viewport = { width: 1600, height: 852 };

  it('全图 = 旧行为：整张塞进视口，字号掉到 6 像素以下', () => {
    const plan = planView({ mode: 'fit-all', bounds, viewport });
    expect(plan.zoom).toBeCloseTo(fitAllScale(bounds, viewport), 9);
    // 高度是瓶颈：(852 - 2*71) / 1952 = 0.364
    expect(plan.zoom).toBeCloseTo(0.3638, 3);
    expect(NODE_FONT_PX * plan.zoom).toBeLessThan(6);
    // 装得下，所以它是"整张图"，也不该有裁剪结论。
    expect(plan.clipped).toBe(false);
  });

  it('适应宽度 = 停在 0.857，字 12 像素；代价是纵向看不全', () => {
    const plan = planView({ mode: 'fit-width', bounds, viewport });
    expect(plan.zoom).toBe(MIN_READABLE_SCALE);
    expect(NODE_FONT_PX * plan.zoom).toBeCloseTo(12, 9);
    // 宽度：909 × 0.857 = 779 ≤ 1334（装得下）；高度：1952 × 0.857 = 1673 > 710（看不全）
    expect(plan.clipped).toBe(true);
    const seen = coverage(bounds, viewport, plan);
    expect(seen.width).toBe(1);
    // 852 / 0.857 = 994px 的可视高度，占 1952px 的 51%：横向装得下、纵向一半。
    expect(seen.height).toBeCloseTo(0.51, 2);
  });

  it('装不下时按轴分别对锚：装得下的方向居中、装不下的方向对到根上', () => {
    const anchor = { x: 0, y: 0 };
    const plan = planView({ mode: 'fit-width', bounds, viewport, anchor });
    // 横向：909 × 0.857 = 779 ≤ 1334，装得下 → 按图片中心放（左右都没浪费）。
    expect(plan.x).toBeCloseTo(viewport.width / 2 - (bounds.x + bounds.width / 2) * plan.zoom, 6);
    // 纵向：1952 × 0.857 = 1673 > 710，装不下 → 把根（流坐标 y=0）放在视口正中。
    expect(plan.y).toBeCloseTo(viewport.height / 2, 6);
    expect(plan.clipped).toBe(true);
  });

  it('两个方向都装不下时，两个方向都对到根上', () => {
    const tall = { x: -4000, y: -4000, width: 8000, height: 8000 };
    const plan = planView({ mode: 'fit-width', bounds: tall, viewport, anchor: { x: 0, y: 0 } });
    expect(plan.x).toBeCloseTo(viewport.width / 2, 6);
    expect(plan.y).toBeCloseTo(viewport.height / 2, 6);
  });

  it('装得下时以图片中心为锚（与旧 fitView 一致，小图一个像素都不变）', () => {
    const small = { x: 0, y: 0, width: 600, height: 300 };
    const width = planView({ mode: 'fit-width', bounds: small, viewport, anchor: { x: 0, y: 0 } });
    const all = planView({ mode: 'fit-all', bounds: small, viewport, anchor: { x: 0, y: 0 } });
    // 小图两档同解：缩放一样、位置一样——默认档换掉不该让"三节点的小图"变样。
    expect(width.zoom).toBe(all.zoom);
    expect(width.x).toBeCloseTo(all.x, 9);
    expect(width.y).toBeCloseTo(all.y, 9);
    expect(width.clipped).toBe(false);
  });

  it('缩放夹在 [0.2, 1.6]：手缩比下限更不如"看不清"，上限不放大到失控', () => {
    const huge = { x: 0, y: 0, width: 20000, height: 20000 };
    expect(planView({ mode: 'fit-all', bounds: huge, viewport }).zoom).toBeCloseTo(0.2, 9);
    const tiny = { x: 0, y: 0, width: 40, height: 20 };
    expect(planView({ mode: 'fit-all', bounds: tiny, viewport }).zoom).toBeCloseTo(MAX_ZOOM, 9);
  });
});

describe('外接框', () => {
  it('按每个节点自己的外接框算（流程图的菱形比 NODE_SIZE 大）', () => {
    const bounds = contentBounds([
      { position: { x: 0, y: 0 }, data: { size: { width: 220, height: 110 } } },
      { position: { x: 200, y: 90 } },  // 缺 data.size → 回退 NODE_SIZE(180×48)
    ]);
    expect(bounds).toEqual({ x: 0, y: 0, width: 380, height: 138 });
  });

  it('空画布返回 null（不伪造一个 (0,0) 的框）', () => {
    expect(contentBounds([])).toBeNull();
  });
});

describe('画布上的两个按钮', () => {
  it('默认停在「适应宽度」，首屏就按可读比例摆好', async () => {
    const wrapper = await mountWith(longTree());
    // 42 个节点，默认这一枪的缩放就是 0.857（不是 0.36）。
    expect(setViewport).toHaveBeenCalled();
    const last = lastViewport();
    expect(last.zoom).toBeCloseTo(MIN_READABLE_SCALE, 9);
    expect(wrapper.get('[data-test="view-mode-width"]').attributes('aria-pressed')).toBe('true');
    expect(wrapper.get('[data-test="view-mode-all"]').attributes('aria-pressed')).toBe('false');
  });

  it('点「全图」换档，点回「适应宽度」回到切换前那一屏（不丢 viewport）', async () => {
    const wrapper = await mountWith(longTree());
    const store = useMindMapStore();
    const bounds = contentBounds(store.nodes);
    expect(bounds).not.toBeNull();
    const allScale = fitAllScale(bounds!, dimensions.value);
    // 切走之前那一屏（`mountWith` 之后画布停在首屏适配的位置上）。
    const before = { ...currentViewport };
    setViewport.mockClear();

    await wrapper.get('[data-test="view-mode-all"]').trigger('click');
    await flushPromises();
    const toAll = lastViewport();
    expect(toAll.zoom).toBeCloseTo(allScale, 9);
    expect(wrapper.get('[data-test="view-mode-all"]').attributes('aria-pressed')).toBe('true');

    await wrapper.get('[data-test="view-mode-width"]').trigger('click');
    await flushPromises();
    // 原样还回去的就是切走之前那一屏，不是"重新适配一次"。
    expect(lastViewport()).toEqual(before);
  });

  it('切回「适应宽度」时"装不下"那句提示要重新算，不能沿用全图档的结论', async () => {
    const wrapper = await mountWith(longTree());
    const store = useMindMapStore();
    // 全图档：整张都看得见 → 没有提示。
    await wrapper.get('[data-test="view-mode-all"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-test="view-clipped"]').exists()).toBe(false);

    // 切回来：视口还原成"高度只看得见一部分"的那一屏，提示必须回来。
    await wrapper.get('[data-test="view-mode-width"]').trigger('click');
    await flushPromises();
    const bounds = contentBounds(store.nodes)!;
    const seen = coverage(bounds, dimensions.value, currentViewport);
    expect(seen.height).toBeLessThan(0.999);
    expect(wrapper.get('[data-test="view-clipped"]').text()).toContain('拖动可平移');
  });

  it('装不下时说清楚"还能往哪看"', async () => {
    const wrapper = await mountWith(longTree());
    expect(wrapper.get('[data-test="view-clipped"]').text()).toContain('拖动可平移');
  });
});
