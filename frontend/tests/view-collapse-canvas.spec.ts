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
 * 1. 每个折叠点上的「+N」（常驻，不是 hover 才出现）；
 * 2. 右上角那行「显示 7 / 136 个节点」（总量必须与画布上的数摆在一起）；
 * 3. 一个「全部展开」的出口，点完这一屏就是整棵树。
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
  it('装 136 节点的真图：右上角说「显示 7 / 136 个节点」，并给一个全部展开的出口', async () => {
    const wrapper = mountCanvas();
    const store = useMindMapStore();
    store.preview(tree136, 'right');
    await flushPromises();
    await wrapper.vm.$nextTick();

    const state = wrapper.get('[data-test="view-fold-state"]');
    // 两个数必须同时在场：只说 7，用户以为树就这么大；只说 136，用户找不到那 129 个
    expect(state.text()).toContain('显示 7 / 136 个节点');
    expect(wrapper.find('[data-test="view-expand-all"]').exists()).toBe(true);
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
    // 全展开之后还能收回默认档：没有这条回程路，"点了全部展开"就是一次性的
    expect(state.find('[data-test="view-fold-default"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="view-expand-all"]').exists()).toBe(false);
  });

  it('点「收起深层」回到默认的一层', async () => {
    const wrapper = mountCanvas();
    const store = useMindMapStore();
    store.preview(tree136, 'right');
    await flushPromises();
    await wrapper.get('[data-test="view-expand-all"]').trigger('click');
    await flushPromises();

    await wrapper.get('[data-test="view-fold-state"]').get('[data-test="view-fold-default"]').trigger('click');    await flushPromises();
    await wrapper.vm.$nextTick();

    expect(store.nodes.length).toBe(7);
    expect(wrapper.get('[data-test="view-fold-state"]').text()).toContain('显示 7 / 136 个节点');
  });

  it('每个折叠点都有一个可点的「+N」，点了子节点就出现', async () => {
    const wrapper = mountCanvas();
    const store = useMindMapStore();
    store.preview(tree136, 'right');
    await flushPromises();
    await wrapper.vm.$nextTick();

    const chips = wrapper.findAll('[data-test="node-folded"]');
    // 6 个一级分支，各挂 3 个子节点
    expect(chips.length).toBe(6);
    expect(chips[0].text()).toBe('+3');

    await chips[0].trigger('click');
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(store.nodes.length).toBe(7 + 3);
    // 放出来之后这一支不再是折叠点：角标必须跟着消失，否则点第二次毫无反应
    expect(store.nodes.some(node => node.id === tree136.root.children[0].id
      && node.data?.foldedChildren !== undefined)).toBe(false);
  });
});

describe('节点上的角标本身', () => {
  const base: MindNodeData = {
    label: '标题',
    body: '标题',
    fullLabel: '标题',
    depth: 1,
    out: [],
    in: [],
  };

  it('没有收起任何东西时不画角标（绝大多数节点不该长多余的东西）', () => {
    const wrapper = mount(MindNode, { props: { id: 'n', data: base } });
    expect(wrapper.find('[data-test="node-folded"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it('收起时角标写「+N」，整支的规模在 title 里，点一下发 unfold', () => {
    const wrapper = mount(MindNode, {
      props: { id: 'n', data: { ...base, foldedChildren: 3, foldedSubtree: 17 } },
    });
    const chip = wrapper.get('[data-test="node-folded"]');
    // 数字是**直接子节点数**：点一下当场冒出这么多，写大了第二次就没人信
    expect(chip.text()).toBe('+3');
    expect(chip.attributes('title')).toContain('整支 17 个');
    expect(chip.attributes('aria-label')).toContain('还有 3 个子节点');
    void chip.trigger('click');
    expect(wrapper.emitted('unfold')).toHaveLength(1);
    wrapper.unmount();
  });
});
