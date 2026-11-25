import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import MindMapCanvas from '../src/canvas/MindMapCanvas.vue';
import MindNode from '../src/canvas/MindNode.vue';
import { REAL_TREES } from './fixtures/real-trees';
import type { MindMapContent } from '../src/api/types';
import { useMindMapStore, type MindNodeData } from '../src/stores/mindmap';

/**
 * 收起之后**界面说了什么**：这一组钉的是"用户会不会以为树就这么小"。
 *
 * 数据层的读数在 `tests/view-collapse.spec.ts`，这里只看画布上的三处证据：
 *
 * 1. 每个被截断的节点上一枚常驻的「+N」（不是 hover 才出现——数量口径下截断点是一串
 *    具体的父节点，"哪儿被截了"只能靠它标出来）；
 * 2. 顶部那行「显示 40 / 136 个节点」（总量必须与画布上的数摆在一起）；
 * 3. 两条出口：「全部展开」与「收起到上限」——展开之后必须能一次收回去。
 *
 * 三条缺一条，收起就退化成"替用户决定他看不到什么"——折叠退休时躲开的正是这个。
 */

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

/** jsdom 没有 DOMMatrixReadOnly，Vue Flow 量节点尺寸时会 new 一个。 */
class DOMMatrixReadOnlyStub {
  m11 = 1;
  m22 = 1;
  m41 = 0;
  m42 = 0;
}

const tree136 = REAL_TREES.tree136 as MindMapContent;

const wrappers: VueWrapper[] = [];

function mountCanvas(): VueWrapper {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(MindMapCanvas, { attachTo: document.body, global: { plugins: [pinia] } });
  wrappers.push(wrapper);
  return wrapper;
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  vi.stubGlobal('DOMMatrixReadOnly', DOMMatrixReadOnlyStub);
});

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount());
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

describe('画布上的收起提示', () => {
  it('装 136 节点的真图：顶部说「显示 40 / 136 个节点」，并给一个全部展开的出口', async () => {
    const wrapper = mountCanvas();
    const store = useMindMapStore();
    store.preview(tree136, 'right');
    await flushPromises();
    await wrapper.vm.$nextTick();

    const state = wrapper.get('[data-test="view-fold-state"]');
    // 两个数必须同时在场：只说 40，用户以为树就这么大；只说 136，用户找不到那 96 个
    expect(state.text()).toContain('显示 40 / 136 个节点');
    expect(wrapper.find('[data-test="view-expand-all"]').exists()).toBe(true);
    // 还没有手动展开/收起过，所以「收起到上限」不必出现（现在就在上限上）
    expect(wrapper.find('[data-test="view-fold-cap"]').exists()).toBe(false);
  });

  it('点「全部展开」：这一屏就是整棵树，那行字改成"显示全部"并留一条回程路', async () => {
    const wrapper = mountCanvas();
    const store = useMindMapStore();
    store.preview(tree136, 'right');
    await flushPromises();

    await wrapper.get('[data-test="view-expand-all"]').trigger('click');
    await flushPromises();
    await wrapper.vm.$nextTick();

    expect(store.hiddenNodeCount).toBe(0);
    expect(store.nodes.length).toBe(136);
    const state = wrapper.get('[data-test="view-fold-state"]');
    expect(state.text()).toContain('显示全部 136 个节点');
    // 全展开之后还能一次收回去：没有这条回程路，"点了全部展开"就是一次性的
    expect(state.find('[data-test="view-fold-cap"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="view-expand-all"]').exists()).toBe(false);
  });

  it('点「收起到上限」回到 40 个节点', async () => {
    const wrapper = mountCanvas();
    const store = useMindMapStore();
    store.preview(tree136, 'right');
    await flushPromises();
    await wrapper.get('[data-test="view-expand-all"]').trigger('click');
    await flushPromises();

    await wrapper.get('[data-test="view-fold-state"]').get('[data-test="view-fold-cap"]').trigger('click');
    await flushPromises();
    await wrapper.vm.$nextTick();

    expect(store.nodes.length).toBe(40);
    expect(wrapper.get('[data-test="view-fold-state"]').text()).toContain('显示 40 / 136 个节点');
  });

  it('每个被截断的节点都有一枚可点的「+N」，点了子节点就出现', async () => {
    const wrapper = mountCanvas();
    const store = useMindMapStore();
    store.preview(tree136, 'right');
    await flushPromises();
    await wrapper.vm.$nextTick();

    const chips = wrapper.findAll('[data-test="node-folded"]');
    const folded = store.nodes.filter(node => (node.data?.hiddenChildren ?? 0) > 0);
    // 角标数与"被截断的节点数"必须一一对应：少一个就是"这里被截了却没说"
    expect(chips.length).toBe(folded.length);
    expect(chips.length).toBeGreaterThan(10);
    expect(chips[0].text()).toMatch(/^\+\d+$/);

    const before = store.nodes.length;
    await chips[0].trigger('click');
    await flushPromises();
    await wrapper.vm.$nextTick();
    // 点一下至少多出一个节点，而且那一处不再是"没显示"状态
    expect(store.nodes.length).toBeGreaterThan(before);
    expect(wrapper.findAll('[data-test="node-collapse"]').length).toBeGreaterThan(0);
  });

  it('展开之后那一处出现「−」，点它收回去（可见数回到 40）', async () => {
    const wrapper = mountCanvas();
    const store = useMindMapStore();
    store.preview(tree136, 'right');
    await flushPromises();

    const first = wrapper.findAll('[data-test="node-folded"]')[0];
    await first.trigger('click');
    await flushPromises();
    expect(store.nodes.length).toBeGreaterThan(40);

    // 只点**刚才那一处**（常驻不透明的那个）——预算给孩子的那几处也有减号，
    // 但它们默认透明，DOM 顺序里第一个通常是根节点（收它 = 只剩一个框）。
    const pinned = wrapper.findAll('[data-test="node-collapse"]')
      .find(chip => chip.classes().includes('opacity-100'));
    expect(pinned, '刚展开的那一处必须有一枚常驻的「−」').toBeDefined();
    await pinned!.trigger('click');
    await flushPromises();
    expect(store.nodes.length).toBe(40);
  });
});

describe('节点上的两枚角标本身', () => {
  const base: MindNodeData = {
    label: '标题',
    body: '标题',
    fullLabel: '标题',
    depth: 1,
    out: [],
    in: [],
  };

  it('没有收起任何东西、也没有子节点时不画任何角标（绝大多数节点不该长多余的东西）', () => {
    const wrapper = mount(MindNode, { props: { id: 'n', data: base } });
    expect(wrapper.find('[data-test="node-folded"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="node-collapse"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it('收起时角标写「+N」，整支的规模在 title 里，点一下发 toggleFold', () => {
    const wrapper = mount(MindNode, {
      props: { id: 'n', data: { ...base, hiddenChildren: 3, hiddenSubtree: 17 } },
    });
    const chip = wrapper.get('[data-test="node-folded"]');
    // 数字是**没画出来的直接子节点数**：点一下当场冒出这么多，写大了第二次就没人信
    expect(chip.text()).toBe('+3');
    expect(chip.attributes('title')).toContain('17 个节点没画');
    expect(chip.attributes('aria-label')).toContain('还有 3 个子节点没显示');
    void chip.trigger('click');
    expect(wrapper.emitted('toggleFold')).toHaveLength(1);
    wrapper.unmount();
  });

  it('转成"已展开"时画「−」，点它发 collapse（不是 toggleFold）', () => {
    const wrapper = mount(MindNode, {
      props: { id: 'n', data: { ...base, visibleChildren: 3, foldOpen: true } },
    });
    expect(wrapper.find('[data-test="node-folded"]').exists()).toBe(false);
    const chip = wrapper.get('[data-test="node-collapse"]');
    expect(chip.text()).toBe('−');
    expect(chip.attributes('title')).toContain('叠起这 3 个子节点');
    // 用户自己展开过的：常驻不透明（不用 hover 才看得见）
    expect(chip.classes()).toContain('opacity-100');
    void chip.trigger('click');
    expect(wrapper.emitted('collapse')).toHaveLength(1);
    expect(wrapper.emitted('toggleFold')).toBeUndefined();
    wrapper.unmount();
  });

  it('孩子是预算给的（不是用户展开的）：减号默认透明，靠 hover / 聚焦 / 选中显形', () => {
    const wrapper = mount(MindNode, {
      props: { id: 'n', data: { ...base, visibleChildren: 3 } },
    });
    const chip = wrapper.get('[data-test="node-collapse"]');
    expect(chip.classes()).toContain('opacity-0');
    expect(chip.classes()).toContain('group-hover:opacity-100');
    wrapper.unmount();
  });
});
