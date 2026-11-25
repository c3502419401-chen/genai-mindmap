import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { ref } from 'vue';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MindMapContent } from '../src/api/types';
import { MIN_READABLE_SCALE } from '../src/canvas/viewport';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * 生成完之后**自动选档**（`fit-width` / `fit-all`），以及"还有几个节点在视野外"那句话。
 *
 * 这一组用例钉的是那一句设计判据：
 *
 *     看不全的代价是一次已知方向的操作；看不清的代价是信息为零。
 *
 * 所以判据只有一条（`clarity.chooseViewMode`）：`fit-all` 的缩放还够不够 `12/14`。
 * 够了就"看全不吃亏"，不够就停在可读比例上、把代价说出来。
 *
 * 画布整体用假 `@vue-flow/core`（jsdom 里量不出画布尺寸，真 Vue Flow 的
 * `dimensions` 恒为 0，`planView` 会直接早退，测不到任何东西）——与
 * `tests/canvas-viewport.spec.ts` 同一套假件。
 */

let currentViewport = { x: 0, y: 0, zoom: 1 };
const setViewport = vi.fn(
  async (transform: { x: number; y: number; zoom: number }) => {
    currentViewport = transform;
    return true;
  },
);
const getViewport = vi.fn(() => currentViewport);
const dimensions = ref({ width: 1600, height: 852 });
/** 拖动指示层读的是**响应式**的 `viewport`（不读 `getViewport()`，见画布那边的注释）。 */
const viewport = ref({ x: 0, y: 0, zoom: 1 });

vi.mock('@vue-flow/core', () => ({
  VueFlow: { name: 'VueFlowStub', template: '<div class="vue-flow-stub" />' },
  Handle: { name: 'HandleStub', template: '<div />' },
  Position: { Left: 'left', Right: 'right', Top: 'top', Bottom: 'bottom' },
  useVueFlow: () => ({
    dimensions,
    viewport,
    setViewport,
    getViewport,
    setCenter: vi.fn(),
    onNodeClick: vi.fn(),
    onNodeDoubleClick: vi.fn(),
    onPaneClick: vi.fn(),
    onMoveEnd: vi.fn(),
    updateNodeInternals: vi.fn(),
    // 拖动改结构那一套。这个用例不拖，但组件在 setup 阶段就要注册它们——
    // 少一个就是 `undefined is not a function`，整块画布挂不掉。
    onNodeDragStart: vi.fn(),
    onNodeDrag: vi.fn(),
    onNodeDragStop: vi.fn(),
    screenToFlowCoordinate: (point: { x: number; y: number }) => point,
    flowToScreenCoordinate: (point: { x: number; y: number }) => point,
    findNode: () => undefined,
    updateNode: vi.fn(),
  }),
}));

vi.mock('@vue-flow/background', () => ({
  Background: { name: 'BackgroundStub', template: '<div />' },
}));

const MindMapCanvas = (await import('../src/canvas/MindMapCanvas.vue')).default;

/** 3 个节点的图：整张塞进视口之后字号仍然够大 → 该走 `fit-all`。 */
function tinyTree(): MindMapContent {
  return {
    title: '小图',
    root: {
      id: 'r', label: '根',
      children: [
        { id: 'a', label: '甲', children: [] },
        { id: 'b', label: '乙', children: [] },
      ],
    },
  };
}

/** 42 节点、竖长型（909×1952）：`fit-all` 只有 0.364 → 该停在可读比例上。 */
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
  // 这一组用例钉的是"自动选哪一档"，而档位判据的输入是**画布上的节点集**。
  // 视图层默认只摊开一层（`canvas/view-collapse.ts`），42 节点的竖长图收起后只有
  // 7 个节点、也就装得下了——那会让这组断言在"其实变好了"的情况下变红。所以这里
  // 显式全展开：测档位选择时，画布上就该是整张图。
  store.expandAllInView();
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

describe('生成后自动选档', () => {
  it('小图：整张装得下且字够大 → 自动停在「全图」', async () => {
    const wrapper = await mountWith(tinyTree());
    expect(wrapper.get('[data-test="view-mode-all"]').attributes('aria-pressed')).toBe('true');
    expect(wrapper.get('[data-test="view-mode-width"]').attributes('aria-pressed')).toBe('false');
    // 小图整张看得见，也就不该有"只显示了一部分"。
    expect(wrapper.find('[data-test="view-clipped"]').exists()).toBe(false);
  });

  it('竖长型的图：fit-all 字太小 → 自动停在「适应宽度」，并把视野外的节点数说出来', async () => {
    const wrapper = await mountWith(longTree());
    expect(wrapper.get('[data-test="view-mode-width"]').attributes('aria-pressed')).toBe('true');
    const hint = wrapper.get('[data-test="view-clipped"]');
    expect(hint.text()).toContain('拖动可平移');
    // 42 个节点、0.857 倍下高度只看得见约一半 —— 视野外的数量必须是真的。
    const match = hint.text().match(/还有 (\d+) 个节点在视野外/);
    expect(match).not.toBeNull();
    const offscreen = Number(match![1]);
    expect(offscreen).toBeGreaterThan(0);
    expect(offscreen).toBeLessThan(42);
    // 缩放停在可读下限（不是 0.364）。
    expect(currentViewport.zoom).toBeCloseTo(MIN_READABLE_SCALE, 6);
  });

  it('用户手动点过档位之后，编辑不会把它改回去', async () => {
    const wrapper = await mountWith(longTree());
    const store = useMindMapStore();
    // 默认自动选中了「适应宽度」，用户改成「全图」。
    await wrapper.get('[data-test="view-mode-all"]').trigger('click');
    await flushPromises();
    expect(wrapper.get('[data-test="view-mode-all"]').attributes('aria-pressed')).toBe('true');

    // 加一个子节点：节点数变了、会重新适配，但**不该**重新选档。
    store.addChildTo('b0');
    await flushPromises();
    vi.advanceTimersByTime(200);
    await flushPromises();
    expect(wrapper.get('[data-test="view-mode-all"]').attributes('aria-pressed')).toBe('true');
  });

  it('换一张图（新内容落地）时重新判一次档位', async () => {
    const wrapper = await mountWith(longTree());
    expect(wrapper.get('[data-test="view-mode-width"]').attributes('aria-pressed')).toBe('true');

    // 换成一张小图：这一次该判成「全图」。
    useMindMapStore().preview(tinyTree(), 'right');
    await flushPromises();
    vi.advanceTimersByTime(200);
    await flushPromises();
    expect(wrapper.get('[data-test="view-mode-all"]').attributes('aria-pressed')).toBe('true');
  });
});
