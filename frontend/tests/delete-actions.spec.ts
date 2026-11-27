import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia } from 'pinia';
import { afterEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { LibraryEntry, LibraryList, MapRecord } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';
import { isPrefsRequest, prefsReply } from './helpers/prefs-stub';

vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

/**
 * **同屏三个「删除」必须可区分。**
 *
 * 这是"写用户手册时撞到的第四条"：左抽屉里删导图、知识库抽屉里删材料、同一个抽屉里
 * 删别名——三个按钮的可见文字过去都是「删除」。当时的用例只能靠"文档顺序取第几个"
 * 来点它（`user-path-workspace.spec.ts` 里那句注释就是这么写的），而文档顺序不是
 * 契约：加一行列表、调一次布局，同一个 `findAll('button')[3]` 就悄悄点到了别的动作上。
 *
 * 修法不是"再写一条按顺序点的用例"，而是让三个动作**各自可命名**：
 *
 *   - 可见文字带宾语（`删除导图` / `删除材料` / `删除别名`），读屏与按文本定位的工具
 *     都不用数第几个；
 *   - 每个都有 `data-test`（稳定的机器名）与非空 `aria-label`（带宾语，导图还带标题）；
 *   - 三个 `aria-label` 互不相同——同名的用途（"把这个东西拿掉"）之间也必须能分辨
 *     拿掉的是哪一类东西。
 *
 * 判据放在**整页**上而不是单个组件上：这三个按钮从来不在同一个组件里，缺陷恰恰是
 * "各自看都没问题，凑到一屏就分不清"。
 */

const MAP: MapRecord = {
  id: 'm1',
  version: 3,
  content: { title: '碳中和路线图', root: { id: 'r', label: '碳中和路线图', children: [] } },
  updated_at: '2026-11-26T10:00:00.000Z',
};

const ENTRY: LibraryEntry = {
  source_id: 'src-1',
  filename: '管理办法.pdf',
  kind: 'document',
  chars: 1200,
  chunks: 8,
  created_at: '2026-11-16T10:00:00.000Z',
  last_used_at: '2026-11-16T11:00:00.000Z',
  cited_by: 0,
};

const ALIAS = {
  alias: '市场监管总局',
  canonical: '国家市场监督管理总局',
  source: 'substring',
  similarity: 0.93,
  created_at: '2026-11-16T10:00:00.000Z',
};

interface Call {
  url: string;
  method: string;
}

function json(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

function stubAll(library: LibraryList = { total: 1, items: [ENTRY] }) {
  const calls: Call[] = [];
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    calls.push({ url, method });
    if (isPrefsRequest(url)) return prefsReply(url);
    if (url.endsWith('/health')) return json({ status: 'ok', version: 'test' });
    if (url.includes('/knowledge/aliases/candidates')) return json({ job_id: 'j1', status: 'done', items: [] });
    if (url.includes('/knowledge/aliases')) {
      if (method === 'DELETE') return json({ alias: ALIAS.alias, deleted: 1 });
      return json({ items: [ALIAS], total: 1, configured: true });
    }
    if (url.includes('/knowledge/library')) {
      if (method === 'DELETE') {
        return json({ source_id: ENTRY.source_id, deleted: true, cited_by: 0 });
      }
      return json(library);
    }
    if (url.includes('/mindmaps')) return json([MAP]);
    return json({});
  });
  vi.stubGlobal('fetch', mock);
  return { mock, calls };
}

function mountApp() {
  return mount(App, { global: { plugins: [createPinia()] } });
}

function byText(wrapper: VueWrapper, text: string) {
  const found = wrapper.findAll('button').find(item => item.text().includes(text));
  if (!found) throw new Error(`找不到按钮：${text}`);
  return found;
}

/** 左侧历史抽屉 + 知识库抽屉都打开——也就是用户真实会遇到的"同屏"。 */
async function openBothPanels(wrapper: VueWrapper): Promise<void> {
  await wrapper.get('[data-test="history-toggle"]').trigger('click');
  await flushPromises();
  await byText(wrapper, '知识库').trigger('click');
  await vi.waitFor(() => expect(wrapper.text()).toContain('管理办法.pdf'));
  await byText(wrapper, '查看').trigger('click');
  await vi.waitFor(() => expect(wrapper.text()).toContain('市场监管总局'));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('同屏三个删除', () => {
  it('三个都在一屏上，各有宾语与 aria-label，没有任何一个只写「删除」', async () => {
    stubAll();
    const wrapper = mountApp();
    await flushPromises();
    useMindMapStore().maps = [MAP];
    await openBothPanels(wrapper);

    const expected: [string, string][] = [
      ['drawer-map-delete', '删除导图'],
      ['library-source-delete', '删除材料'],
      ['alias-delete', '删除别名'],
    ];

    // 先证明"同屏"这件事是真的：三个入口同时可点，不是三个用例各测一个。
    for (const [test, text] of expected) {
      const button = wrapper.find(`[data-test="${test}"]`);
      expect(button.exists(), `缺少入口：${test}`).toBe(true);
      expect(button.text()).toBe(text);
    }

    // 可见文字两两不同：读屏与按文本定位的工具都不必数"第几个"。
    const texts = expected.map(([test]) => wrapper.get(`[data-test="${test}"]`).text());
    expect(new Set(texts).size).toBe(expected.length);

    // aria-label 非空且两两不同。导图那个还带标题——同一个标题下有两张图时也分得开。
    const labels = expected.map(([test]) => wrapper.get(`[data-test="${test}"]`).attributes('aria-label') ?? '');
    for (const [index, label] of labels.entries()) {
      expect(label.trim(), `${expected[index][0]} 的 aria-label 是空的`).not.toBe('');
    }
    expect(new Set(labels).size).toBe(expected.length);
    expect(labels[0]).toContain('碳中和路线图');

    // 整页上不再有裸「删除」：这条是把"两个可区分"推广成"没有不可命名的删除"。
    // 将来有人再加一个只写「删除」的按钮，这条会先红，而不是等下一次写手册时才撞到。
    const bare = wrapper.findAll('button').filter(button => button.text() === '删除');
    expect(bare.map(button => button.html())).toEqual([]);
  });

  it('按 data-test 点：三个动作各发各的端点，顺序与文档位置无关', async () => {
    const { calls } = stubAll();
    const wrapper = mountApp();
    await flushPromises();
    useMindMapStore().maps = [MAP];
    await openBothPanels(wrapper);

    const deletes = () => calls.filter(call => call.method === 'DELETE');

    // 导图：第一下只进确认态（不可恢复的动作不能在这一下就发请求）。
    await wrapper.get('[data-test="drawer-map-delete"]').trigger('click');
    await flushPromises();
    expect(deletes()).toHaveLength(0);
    expect(wrapper.find('[data-test="drawer-map-delete-hint"]').exists()).toBe(true);

    // 材料：直接删（没被引用就不问），发的是知识库那条路。
    await wrapper.get('[data-test="library-source-delete"]').trigger('click');
    await vi.waitFor(() => expect(deletes().length).toBe(1));
    expect(deletes()[0].url).toContain('/knowledge/library/src-1');

    // 别名：发的是别名那条路，路径里的中文要编码。
    await wrapper.get('[data-test="alias-delete"]').trigger('click');
    await vi.waitFor(() => expect(deletes().length).toBe(2));
    expect(deletes()[1].url).toContain(`/knowledge/aliases/${encodeURIComponent(ALIAS.alias)}`);

    // 三条路互不串台：一次点击只发一条 DELETE。
    expect(deletes().filter(call => call.url.includes('/mindmaps/'))).toHaveLength(0);
  });
});
