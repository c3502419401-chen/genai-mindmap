import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { MindMapContent } from '../src/api/types';
import { textSource } from '../src/sources/input';
import { useChatStore } from '../src/stores/chat';
import { useMindMapStore } from '../src/stores/mindmap';
import { useSourcesStore } from '../src/stores/sources';
import { isPrefsRequest, prefsReply } from './helpers/prefs-stub';

/**
 * 瘦顶栏与"原来那些按钮都去哪了"。
 *
 * 顶栏只剩三件东西（`☰` / 标题 / `+`），代价是另外十来个入口必须搬到一个找得到的地方。
 * 这一组用例逐个把那个地方的入口点一遍——**改版最容易出的事不是按钮变丑，是某个功能
 * 从此没人能到达**，而它不会有任何报错。
 */
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

const TREE: MindMapContent = {
  title: '预算管理',
  root: { id: 'r', label: '预算管理', children: [{ id: 'a', label: '申请与复核', children: [] }] },
};

function streamReply(frames: unknown[] = []): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`));
      }
      controller.enqueue(encoder.encode('data: [DONE]\n\n'));
      controller.close();
    },
  });
  return { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
}

function stubFetch(records: unknown[] = []) {
  const mock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (isPrefsRequest(url)) return prefsReply(url);
    if (url.endsWith('/health')) {
      return { ok: true, status: 200, json: async () => ({ version: 'v0.4.7' }) } as unknown as Response;
    }
    if (url.endsWith('/mindmaps')) {
      return { ok: true, status: 200, json: async () => records } as unknown as Response;
    }
    return streamReply();
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

const wrappers: VueWrapper[] = [];

function mountApp(): VueWrapper {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(App, { global: { plugins: [pinia] } });
  wrappers.push(wrapper);
  return wrapper;
}

function drawerOpen(wrapper: VueWrapper): boolean {
  const style = wrapper.get('[data-test="history-drawer"]').attributes('style') ?? '';
  return !style.includes('display: none');
}

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount());
  vi.unstubAllGlobals();
});

describe('瘦顶栏', () => {
  it('只有三件东西：☰、标题、+', async () => {
    stubFetch();
    const wrapper = mountApp();
    await flushPromises();

    const header = wrapper.get('header');
    // 数的是**顶栏有哪些动作**，不是"实现里有几个 button 标签"。
    // 这两种口径在加一个入口时会分叉：前者要问"这个新动作该不该在顶栏"，
    // 后者只会变成一条"数目变了，改数字"的噪音。所以按 `data-test` 数。
    const actions = header.findAll('[data-test="history-toggle"], '
      + '[data-test="open-settings"], [data-test="new-run"]');
    expect(actions).toHaveLength(3);
    expect(header.findAll('button')).toHaveLength(3);
    expect(header.find('input#title').exists()).toBe(true);
    expect(wrapper.find('[data-test="history-toggle"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="open-settings"]').exists()).toBe(true);
    expect(wrapper.get('[data-test="new-run"]').text()).toBe('+');

    // 原来那一排一个都不在顶栏里了（它们进了抽屉，见下一组用例）
    expect(header.text()).not.toContain('知识库');
    expect(header.text()).not.toContain('演示示例');
    expect(header.text()).not.toContain('导出');
    // 版本号也搬进抽屉：顶栏不再挂徽章
    expect(header.text()).not.toContain('v0.4.7');
    expect(wrapper.get('[data-test="history-drawer"]').text()).toContain('v0.4.7');
  });

  it('标题就是那个可改名的输入框（`#title` 一直在）', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    await wrapper.vm.$nextTick();

    const input = wrapper.get('input#title');
    expect((input.element as HTMLInputElement).value).toBe('预算管理');

    await input.setValue('预算管理（2027）');
    await input.trigger('change');
    expect(store.content?.title).toBe('预算管理（2027）');
  });

  it('`+` 开新的一轮：清空画布与时间线，并把对话框叫回输入位置', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    const chat = useChatStore();
    store.preview(TREE, 'right');
    chat.said('上一轮说的话');
    await wrapper.vm.$nextTick();

    await wrapper.get('[data-test="new-run"]').trigger('click');
    await flushPromises();

    expect(store.content).toBeNull();
    expect(chat.messages).toHaveLength(0);
    expect(wrapper.get('[data-test="chat-dock"]').attributes('data-faded')).toBe('false');
  });

  it('`+` 把材料、粘贴框草稿与没发出去的那句话一起清掉，并说明清掉了什么', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    const sources = useSourcesStore();
    store.preview(TREE, 'right');
    sources.items = [{
      key: 's1', id: 'doc-1', label: '材料.pdf', kind: 'document', state: 'ready',
      source: { ...textSource('材料正文'), id: 'doc-1', kind: 'document', filename: '材料.pdf' },
      error: '', seconds: 0,
    }];
    sources.draftText = '粘贴框里的草稿';
    await wrapper.get('textarea#intent').setValue('还没发出去的那句话');
    await wrapper.vm.$nextTick();

    await wrapper.get('[data-test="new-run"]').trigger('click');
    await flushPromises();

    // "新对话"的语义是干净开始：画布、时间线、材料、两处草稿都不留。
    expect(sources.items).toHaveLength(0);
    expect(sources.draftText).toBe('');
    expect((wrapper.get('textarea#intent').element as HTMLTextAreaElement).value).toBe('');
    // 但清掉了什么必须说出来，而且要留回去的路。
    const notice = wrapper.get('[data-test="notice"]');
    expect(notice.text()).toContain('已清空 1 份材料');
    expect(notice.text()).toContain('粘贴框里没添加的正文');
    expect(notice.text()).toContain('没发出去的那句话');
    expect(wrapper.find('[data-test="notice-action"]').exists()).toBe(true);
  });

  it('撤销把刚清掉的材料与两处草稿原样放回来', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    const sources = useSourcesStore();
    store.preview(TREE, 'right');
    const material = {
      key: 's1', id: 'doc-1', label: '材料.pdf', kind: 'document' as const, state: 'ready' as const,
      source: { ...textSource('材料正文'), id: 'doc-1', kind: 'document' as const, filename: '材料.pdf' },
      error: '', seconds: 0,
    };
    sources.items = [material];
    sources.draftText = '粘贴框里的草稿';
    await wrapper.get('textarea#intent').setValue('还没发出去的那句话');
    await wrapper.vm.$nextTick();

    await wrapper.get('[data-test="new-run"]').trigger('click');
    await flushPromises();
    await wrapper.get('[data-test="notice-action"]').trigger('click');
    await flushPromises();

    expect(sources.items.map(item => item.id)).toEqual(['doc-1']);
    expect(sources.draftText).toBe('粘贴框里的草稿');
    expect((wrapper.get('textarea#intent').element as HTMLTextAreaElement).value).toBe('还没发出去的那句话');
    // 撤销之后那句提示不该还挂着（它说的是"已经清空"，而东西已经回来了）。
    expect(wrapper.find('[data-test="notice"]').exists()).toBe(false);
  });
});

describe('☰ 抽屉：原来那些按钮一件不少', () => {
  it('默认关着，点 ☰ 打开；Esc 收起', async () => {
    stubFetch();
    const wrapper = mountApp();
    expect(drawerOpen(wrapper)).toBe(false);

    await wrapper.get('[data-test="history-toggle"]').trigger('click');
    await flushPromises();
    expect(drawerOpen(wrapper)).toBe(true);
    expect(wrapper.get('[data-test="history-toggle"]').attributes('aria-expanded')).toBe('true');

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await wrapper.vm.$nextTick();
    expect(drawerOpen(wrapper)).toBe(false);
  });

  it('抽屉里能点到：保存、新建、打开、演示示例、知识库、导入、导出、偏好、节点详情', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    await wrapper.vm.$nextTick();
    await wrapper.get('[data-test="history-toggle"]').trigger('click');
    await flushPromises();

    const drawer = wrapper.get('[data-test="history-drawer"]');
    for (const label of ['保存', '新建', '打开…', '演示示例', '知识库', '导入', '偏好', '节点详情']) {
      const found = drawer.findAll('button').some(button => button.text().includes(label));
      expect(found, `抽屉里找不到「${label}」`).toBe(true);
    }
    // 导出是一个下拉（四种格式），不是按钮
    const options = drawer.get('select').findAll('option').map(option => option.text());
    expect(options.join('|')).toContain('PlantUML');
    expect(options.join('|')).toContain('JSON');
  });

  it('账号区在抽屉里，默认就是一个可用的游客状态', async () => {
    stubFetch();
    const wrapper = mountApp();
    await wrapper.get('[data-test="history-toggle"]').trigger('click');
    await flushPromises();

    const drawer = wrapper.get('[data-test="history-drawer"]');
    // 账号是低频动作，放进顶栏会把最常用的那三件挤走（见 §瘦顶栏）。
    expect(wrapper.get('header').find('[data-test="auth-panel"]').exists()).toBe(false);
    expect(drawer.find('[data-test="auth-panel"]').exists()).toBe(true);
    // 游客是正常状态：有登录入口，但没有任何"请先登录才能用"的硬闸。
    expect(drawer.find('[data-test="auth-username"]').exists()).toBe(true);
    expect(drawer.get('[data-test="auth-submit"]').text()).toBe('登录');
  });

  it('抽屉里的「演示示例」真的能打开预置图列表', async () => {
    stubFetch();
    const wrapper = mountApp();
    await wrapper.get('[data-test="history-toggle"]').trigger('click');
    await flushPromises();

    const demo = wrapper.get('[data-test="history-drawer"]').findAll('button')
      .find(button => button.text() === '演示示例')!;
    await demo.trigger('click');
    await flushPromises();

    expect(wrapper.text()).toContain('预置演示导图');
  });

  it('抽屉里的历史列表点一条就能打开那张图', async () => {
    const record = {
      id: 'm1', version: 2, updated_at: '2026-11-18T00:00:00Z', content: { ...TREE, title: '旧的一张' },
    };
    const fetchMock = stubFetch([record]);
    const wrapper = mountApp();
    await wrapper.get('[data-test="history-toggle"]').trigger('click');
    await flushPromises();
    await vi.waitFor(() =>
      expect(wrapper.get('[data-test="history-drawer"]').text()).toContain('旧的一张'));

    const row = wrapper.get('[data-test="history-drawer"]').findAll('button')
      .find(button => button.text().includes('旧的一张'))!;
    await row.trigger('click');
    await flushPromises();

    const opened = fetchMock.mock.calls.some(([url]) => String(url).includes('/mindmaps/m1'));
    expect(opened).toBe(true);
  });

  it('抽屉里的偏好浮层照旧能开、能关，角标与面板都在', async () => {
    stubFetch();
    const wrapper = mountApp();
    await wrapper.get('[data-test="history-toggle"]').trigger('click');
    await flushPromises();

    const popover = wrapper.get('[data-test="prefs-popover"]');
    expect(popover.attributes('style') ?? '').toContain('display: none');
    expect(popover.find('[data-test="prefs-panel"]').exists()).toBe(true);

    await wrapper.get('[data-test="prefs-toggle"]').trigger('click');
    await wrapper.vm.$nextTick();
    expect(wrapper.get('[data-test="prefs-popover"]').attributes('style') ?? '').not.toContain('display: none');
  });

  it('抽屉里的「节点详情」把右抽屉打开', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.select('a');
    await wrapper.vm.$nextTick();
    await wrapper.get('[data-test="history-toggle"]').trigger('click');
    await flushPromises();

    await wrapper.get('[data-test="side-toggle"]').trigger('click');
    await wrapper.vm.$nextTick();

    const panel = wrapper.get('[data-test="side-panel"]');
    expect((panel.attributes('style') ?? '')).not.toContain('display: none');
  });
});

describe('材料与左栏', () => {
  it('左栏已经撤掉：材料只出现在对话框的缩略图与「+」面板里', async () => {
    stubFetch();
    const wrapper = mountApp();
    useSourcesStore().addText('一段材料');
    await wrapper.vm.$nextTick();

    const main = wrapper.get('main').element;
    // 常驻的 320px 左栏（`w-80`）一个都不该有
    expect(main.querySelectorAll('.w-80')).toHaveLength(0);
    expect(wrapper.find('[data-test="thumbs"]').exists()).toBe(true);
    expect(wrapper.get('[data-test="material-panel"]').text()).toContain('材料（可选）');
  });
});
