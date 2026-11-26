import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MapRecord } from '../src/api/types';
import HistoryDrawer from '../src/components/HistoryDrawer.vue';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * **写用户手册时撞到的缺陷，逐条变成回归。**
 *
 * 三条共同的成因是"测试测的是函数、不是用户路径"：删导图这个动作在测试里从来没出现
 * 过（所以 `DELETE /api/mindmaps/{id}` 405 了很久也没人知道），于是它们不产生任何
 * 红灯形态，只有照着界面走一遍才会撞到。
 *
 * 这个文件按缺陷分块，一块对应一笔修复：本文件先只有第一块。
 */

const record = (id: string, title = '示例'): MapRecord => ({
  id,
  version: 1,
  content: { title, root: { id: 'root', label: title, children: [] } },
  updated_at: '2026-11-26T10:00:00Z',
});

interface Call {
  url: string;
  method: string;
}

let calls: Call[] = [];

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

function stubFetch(handler: (call: Call) => Response | Promise<Response>) {
  calls = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const call: Call = { url: String(input), method: init?.method ?? 'GET' };
      calls.push(call);
      return handler(call);
    }),
  );
}

function mountDrawer() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(HistoryDrawer, {
    props: {
      open: true,
      confirmDiscard: () => true,
      exporting: false,
      exportChoice: '',
      saveHint: '',
      versionLabel: '',
      modeLabel: '',
      modeTone: '',
      nodeCount: 0,
      sideOpen: false,
      samples: [],
    },
    // 三个子组件各自会发请求；这一块只关心历史列表那一截。
    global: {
      plugins: [pinia],
      stubs: { AuthPanel: true, PrefsPanel: true, ImportMindmap: true },
    },
  });
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('缺陷一：删除导图', () => {
  it('抽屉列表里两段式删除：第一下只进确认态，第二下才发 DELETE', async () => {
    const wrapper = mountDrawer();
    const store = useMindMapStore();
    store.maps = [record('m1', '校园垃圾分类推行方案')];
    stubFetch(() => jsonResponse(undefined, 204));
    await wrapper.vm.$nextTick();

    await wrapper.find('[data-test="drawer-map-delete"]').trigger('click');

    // 第一下只是个状态：不发请求，且理由写在行内（不可恢复）。
    expect(calls).toHaveLength(0);
    expect(wrapper.find('[data-test="drawer-map-delete-hint"]').text()).toContain('不可恢复');

    await wrapper.find('[data-test="drawer-map-delete-confirm"]').trigger('click');
    await flushPromises();

    expect(calls).toEqual([{ url: '/api/mindmaps/m1', method: 'DELETE' }]);
    expect(store.maps).toHaveLength(0);
    expect(wrapper.text()).not.toContain('校园垃圾分类推行方案');
  });

  it('取消之后什么都没发生', async () => {
    const wrapper = mountDrawer();
    const store = useMindMapStore();
    store.maps = [record('m1')];
    stubFetch(() => jsonResponse(undefined, 204));
    await wrapper.vm.$nextTick();

    await wrapper.find('[data-test="drawer-map-delete"]').trigger('click');
    await wrapper.find('[data-test="drawer-map-delete-cancel"]').trigger('click');

    expect(calls).toHaveLength(0);
    expect(store.maps).toHaveLength(1);
    // 回到"可以再删"的状态，而不是停在一个再也点不到的确认态。
    expect(wrapper.find('[data-test="drawer-map-delete"]').exists()).toBe(true);
  });
});
