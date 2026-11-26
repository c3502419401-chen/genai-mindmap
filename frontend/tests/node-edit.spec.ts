import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import MindNode from '../src/canvas/MindNode.vue';
import type { MindNodeData } from '../src/stores/mindmap';

const data: MindNodeData = {
  label: '截断后的短标题…',
  body: '完整标题全文',
  fullLabel: '完整标题全文',
  depth: 1,
  out: ['right'],
  in: ['left'],
  refs: 0,
};

function render(props: Record<string, unknown> = {}) {
  return mount(MindNode, {
    props: { id: 'n1', data, ...props },
    // 句柄组件依赖 Vue Flow 的画布上下文，这里只关心节点自身的编辑行为，替换成空壳。
    global: { stubs: { Handle: true } },
    attachTo: document.body,
  });
}

describe('节点内联编辑', () => {
  it('平时只显示截断后的短标题', () => {
    const wrapper = render();

    expect(wrapper.find('input').exists()).toBe(false);
    expect(wrapper.text()).toContain('截断后的短标题…');
  });

  it('进入编辑时输入框里是完整标题', () => {
    const wrapper = render({ editing: true });

    const input = wrapper.get('input');
    expect((input.element as HTMLInputElement).value).toBe('完整标题全文');
    wrapper.unmount();
  });

  it('回车提交新文字', async () => {
    const wrapper = render({ editing: true });

    const input = wrapper.get('input');
    await input.setValue('改过的标题');
    await input.trigger('keydown', { key: 'Enter' });

    expect(wrapper.emitted('commit')).toEqual([['改过的标题']]);
    expect(wrapper.emitted('cancel')).toBeUndefined();
    wrapper.unmount();
  });

  it('Esc 取消，不提交', async () => {
    const wrapper = render({ editing: true });

    const input = wrapper.get('input');
    await input.setValue('不要这个');
    await input.trigger('keydown', { key: 'Escape' });

    expect(wrapper.emitted('cancel')).toHaveLength(1);
    expect(wrapper.emitted('commit')).toBeUndefined();
    wrapper.unmount();
  });

  it('提交被拒后继续打字，仍能再次提交或取消', async () => {
    // store 会拒绝空标题和超长标题，那时编辑态仍然开着。若提交锁不放，
    // 用户改成合法文字后按回车、Tab、Esc 都毫无反应，看起来像编辑器坏了。
    const wrapper = render({ editing: true });
    const input = wrapper.get('input');

    // 清空后回车：父组件（store）会拒绝，这里只是模拟它没有关闭编辑态
    await input.setValue('');
    await input.trigger('keydown.enter');
    expect(wrapper.emitted('commit')?.length).toBe(1);

    // 继续打字，锁应该解开
    await input.setValue('合法的标题');
    await input.trigger('keydown.enter');
    expect(wrapper.emitted('commit')?.length).toBe(2);
    expect(wrapper.emitted('commit')?.[1]).toEqual(['合法的标题']);

    wrapper.unmount();
  });

  it('提交被拒后按 Esc 也能取消', async () => {
    const wrapper = render({ editing: true });
    const input = wrapper.get('input');

    await input.setValue('');
    await input.trigger('keydown.enter');
    await input.setValue('x');
    await input.trigger('keydown.esc');

    expect(wrapper.emitted('cancel')?.length).toBe(1);
    wrapper.unmount();
  });

  it('Tab 走 next 事件：提交文字后还要接着加下一个', async () => {
    const wrapper = render({ editing: true });

    const input = wrapper.get('input');
    await input.setValue('第一个');
    await input.trigger('keydown', { key: 'Tab' });

    expect(wrapper.emitted('next')).toEqual([['第一个']]);
    expect(wrapper.emitted('commit')).toBeUndefined();
    wrapper.unmount();
  });

  it('失焦也算提交，且不会因为回车后再失焦重复提交一次', async () => {
    const wrapper = render({ editing: true });

    const input = wrapper.get('input');
    await input.setValue('失焦提交');
    await input.trigger('blur');

    expect(wrapper.emitted('commit')).toEqual([['失焦提交']]);
    wrapper.unmount();
  });
});

describe('引用角标', () => {
  it('没有引用时不画角标', () => {
    const wrapper = render();

    expect(wrapper.find('[data-test="node-refs"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it('有引用时显示条数', () => {
    const wrapper = render({ data: { ...data, refs: 3 } });

    const badge = wrapper.get('[data-test="node-refs"]');
    expect(badge.text()).toBe('3');
    wrapper.unmount();
  });

  it('角标可以点：点它发出 openRefs（原来它是 pointer-events-none 的纯装饰）', async () => {
    const wrapper = render({ data: { ...data, refs: 3 } });

    await wrapper.get('[data-test="node-refs"]').trigger('click');

    expect(wrapper.emitted('openRefs')).toHaveLength(1);
    wrapper.unmount();
  });
});