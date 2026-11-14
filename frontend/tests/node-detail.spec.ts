import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia } from 'pinia';
import { afterEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import MindNode from '../src/canvas/MindNode.vue';
import type { MindMapContent, MindNode as MindNodeModel } from '../src/api/types';
import { bodySource, displayBody, displayTitle, useMindMapStore } from '../src/stores/mindmap';

// jsdom 里没有 ResizeObserver，真正的 Vue Flow 画布跑不起来；这一组用例只关心
// 侧栏分区与悬停卡片，画布整体换成占位元素。
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

function mountApp(): VueWrapper {
  return mount(App, { global: { plugins: [createPinia()] } });
}

/** 侧栏「详情」那一块：按 aria-label 定位，与备注框、引用块区分开。 */
const DETAIL_SECTION = '[aria-label="节点详情"]';
const NOTE_TEXTAREA = 'textarea[aria-label="我的备注"]';

/** 一棵"新图"：短标题 + 生成详情 + 用户备注 + 一条引用。 */
const NEW_MAP: MindMapContent = {
  title: '预算管理',
  root: {
    id: 'r',
    label: '预算管理',
    detail: '这张图讲预算的申请、复核与执行。',
    children: [
      {
        id: 'a',
        label: '申请与复核',
        detail: '部门先提交申请，财务在 3 个工作日内完成复核。超过五十万元报总经理。',
        note: '我自己的备注：这里要和去年对比一下',
        source_refs: [{ source_id: 's1', quote: '财务在三个工作日内完成复核' }],
        children: [],
      },
    ],
  },
  source_usage: [{ source_id: 's1', kind: 'document', filename: '预算办法.docx', char_count: 120 }],
};

/** 一棵"旧图"：只有长 label，没有 detail（`note` 也可能有）。 */
const OLD_MAP: MindMapContent = {
  title: '旧图',
  root: {
    id: 'r',
    label: '根',
    children: [
      {
        id: 'old',
        label: '把预算审批的流程拆成申请、复核与执行三段，每段都要写清责任人',
        note: '以前写的备注',
        children: [],
      },
    ],
  },
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('displayBody 的回退链', () => {
  const node = (extra: Partial<MindNodeModel>): MindNodeModel => ({
    id: 'n', label: '标题', children: [], ...extra,
  });

  it('detail 优先，其次是 note，最后是 label', () => {
    expect(displayBody(node({ detail: '详情', note: '备注' }))).toBe('详情');
    expect(displayBody(node({ note: '备注' }))).toBe('备注');
    expect(displayBody(node({}))).toBe('标题');
    // 纯空白当没写
    expect(displayBody(node({ detail: '   ', note: '备注' }))).toBe('备注');
  });

  it('bodySource 说明这段文字来自哪里', () => {
    expect(bodySource(node({ detail: '详情' }))).toBe('detail');
    expect(bodySource(node({ note: '备注' }))).toBe('note');
    expect(bodySource(node({}))).toBe('label');
  });

  it('16 字以内的标题不被截断，旧图的长标题仍按 24 字截断', () => {
    expect(displayTitle(node({ label: '申请与复核' }))).toBe('申请与复核');
    const long = '把预算审批的流程拆成申请、复核与执行三段，每段都要写清责任人';
    expect(displayTitle(node({ label: long }))).toBe(`${Array.from(long).slice(0, 24).join('')}…`);
  });
});

describe('侧栏分区', () => {
  it('label 作标题、detail 作正文、note 进「我的备注」输入框', async () => {
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(NEW_MAP, 'right');
    store.select('a');
    await wrapper.vm.$nextTick();

    // 标题就是 label，一字不改（限定在右侧栏：左侧输入区也有 h2）
    expect(wrapper.find('aside h2').text()).toBe('申请与复核');
    // 详情区显示 detail 全文
    const detail = wrapper.find(DETAIL_SECTION);
    expect(detail.exists()).toBe(true);
    expect(detail.text()).toContain('部门先提交申请，财务在 3 个工作日内完成复核');
    expect(detail.text()).toContain('这次生成写的说明');
    // 备注框里是 note，而不是 detail
    const note = wrapper.find(NOTE_TEXTAREA);
    expect((note.element as HTMLTextAreaElement).value)
      .toBe('我自己的备注：这里要和去年对比一下');
  });

  it('detail 区域只读：没有第二个可编辑 detail 的输入框', async () => {
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(NEW_MAP, 'right');
    store.select('a');
    await wrapper.vm.$nextTick();

    const detail = wrapper.find(DETAIL_SECTION);
    expect(detail.find('textarea').exists()).toBe(false);
    expect(detail.find('input').exists()).toBe(false);
  });

  it('改备注不会覆盖 detail', async () => {
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(NEW_MAP, 'right');
    store.select('a');
    await wrapper.vm.$nextTick();

    const note = wrapper.find(NOTE_TEXTAREA);
    await note.setValue('新写的备注');
    await note.trigger('blur');
    await wrapper.vm.$nextTick();

    const node = store.content!.root.children[0];
    expect(node.note).toBe('新写的备注');
    // detail 一字未动——两条写入路径不该交叉
    expect(node.detail).toBe(NEW_MAP.root.children[0].detail);
    // 详情区仍显示 detail，而不是新备注
    expect(wrapper.find(DETAIL_SECTION).text()).toContain('3 个工作日内完成复核');
  });

  it('旧图没有 detail 时回退显示 note，并说明这段文字的来源', async () => {
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(OLD_MAP, 'right');
    store.select('old');
    await wrapper.vm.$nextTick();

    const detail = wrapper.find(DETAIL_SECTION);
    expect(detail.text()).toContain('以前写的备注');
    expect(detail.text()).toContain('没有生成详情');
  });

  it('旧图连 note 都没有时回退显示完整 label', async () => {
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview({
      title: '更旧的图',
      root: { id: 'r', label: '根', children: [
        { id: 'bare', label: '只有标题的节点，长度超过画布上显示的二十四个字上限', children: [] },
      ] },
    }, 'right');
    store.select('bare');
    await wrapper.vm.$nextTick();

    const detail = wrapper.find(DETAIL_SECTION);
    expect(detail.text()).toContain('只有标题的节点，长度超过画布上显示的二十四个字上限');
    expect(detail.text()).toContain('没有正文');
  });

  it('引用仍在独立分区里，没有混进详情正文', async () => {
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(NEW_MAP, 'right');
    store.select('a');
    await wrapper.vm.$nextTick();

    const detail = wrapper.find(DETAIL_SECTION);
    expect(detail.text()).not.toContain('财务在三个工作日内完成复核');
    expect(wrapper.text()).toContain('来源引用');
  });
});

describe('画布节点', () => {
  function mountNode(data: Record<string, unknown>, editing = false) {
    return mount(MindNode, {
      props: {
        id: 'n',
        data: {
          label: '申请与复核',
          body: '部门先提交申请，财务在 3 个工作日内完成复核。',
          fullLabel: '申请与复核',
          bodySource: 'detail',
          depth: 1,
          out: [],
          in: [],
          ...data,
        } as never,
        editing,
      },
    });
  }

  it('节点框里只画 label，不画详情', () => {
    const wrapper = mountNode({});
    expect(wrapper.text()).toContain('申请与复核');
    expect(wrapper.text()).not.toContain('部门先提交申请');
  });

  it('悬停卡片里是详情正文，并说明"节点上只显示标题"', async () => {
    vi.useFakeTimers();
    try {
      const wrapper = mountNode({});
      await wrapper.trigger('mouseenter');
      vi.advanceTimersByTime(300);
      await wrapper.vm.$nextTick();
      expect(wrapper.text()).toContain('部门先提交申请');
      expect(wrapper.text()).toContain('生成时写的详情');
    } finally {
      vi.useRealTimers();
    }
  });

  it('双击进入编辑时编辑的仍是 label（不是详情）', async () => {
    const wrapper = mountNode({}, true);
    const input = wrapper.find('input');
    expect(input.exists()).toBe(true);
    // 编辑框预填的是完整 label
    expect((input.element as HTMLInputElement).value).toBe('申请与复核');
  });
});
