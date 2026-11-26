import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MapRecord, MindMapContent, MindNode } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * 「删掉一张图」这条链的前端半边。
 *
 * 后端有了 `DELETE /api/mindmaps/{id}` 只是"删得掉"，用户能不能删是另一件事：
 *
 *  - 列表里要有入口，而且要**二次确认**（不可恢复，没有回收站）；
 *  - 删完之后列表里那一行要立刻消失（等一次 `loadMaps()` 等于"点了没反应"）；
 *  - **删的正好是当前打开的那张 → 画布必须清到空态**。不清的话，用户会对着
 *    一张已经不存在的图继续编辑，下一次保存要么 404、要么（更糟）以同一个内容
 *    create 出第三条记录。
 */

const tree = (): MindNode => ({
  id: 'root',
  label: '根',
  children: [{ id: 'a', label: '甲', children: [] }],
});

const sample = (): MindMapContent => ({ title: '示例', root: tree() });

const record = (id: string, title = '示例'): MapRecord => ({
  id,
  version: 1,
  content: { ...sample(), title },
  updated_at: '2026-11-26T10:00:00Z',
});

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

interface Call {
  url: string;
  method: string;
}

let calls: Call[] = [];

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

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('删除一张已保存的导图', () => {
  it('删成功：发的是 DELETE，列表里那一行立刻消失', async () => {
    const store = useMindMapStore();
    store.maps = [record('m1', '甲图'), record('m2', '乙图')];
    stubFetch(() => jsonResponse(undefined, 204));

    const ok = await store.deleteMap('m1');

    expect(ok).toBe(true);
    expect(calls).toEqual([{ url: '/api/mindmaps/m1', method: 'DELETE' }]);
    expect(store.maps.map(item => item.id)).toEqual(['m2']);
    expect(store.status).toContain('已删除导图');
    // 不可恢复这件事要写在提示里，而不是只写在按钮旁。
    expect(store.status).toContain('不可恢复');
  });

  it('删的正好是当前打开的那张：画布清到空态，身份也清掉', async () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');
    store.loadMap(sample(), { id: 'm1', version: 3, updated_at: '2026-11-26T10:00:00Z' });
    store.maps = [record('m1')];
    expect(store.content).not.toBeNull();
    stubFetch(() => jsonResponse(undefined, 204));

    const ok = await store.deleteMap('m1');

    expect(ok).toBe(true);
    // 画布空了：没有内容、没有节点、没有"当前是哪张图"，也就没有"下次保存写回旧 id"。
    expect(store.content).toBeNull();
    expect(store.mapId).toBeNull();
    expect(store.nodeCount).toBe(0);
    expect(store.status).toContain('画布已清空');
  });

  it('删的不是当前打开的那张：画布原样不动', async () => {
    const store = useMindMapStore();
    store.loadMap(sample(), { id: 'm2', version: 1, updated_at: '2026-11-26T10:00:00Z' });
    store.maps = [record('m1'), record('m2')];
    stubFetch(() => jsonResponse(undefined, 204));

    await store.deleteMap('m1');

    expect(store.mapId).toBe('m2');
    expect(store.content).not.toBeNull();
    expect(store.status).not.toContain('画布已清空');
  });

  it('403（不是创建者）：列表与画布都不动，原因来自后端', async () => {
    const store = useMindMapStore();
    store.maps = [record('m1', '别人的图')];
    stubFetch(() => jsonResponse({
      code: 'map_forbidden',
      message: '这张导图不是你创建的，只有创建者能删。',
    }, 403));

    const ok = await store.deleteMap('m1');

    expect(ok).toBe(false);
    expect(store.maps).toHaveLength(1);
    expect(store.status).toContain('只有创建者能删');
  });

  it('网络失败：不清列表，用户还能再试一次', async () => {
    const store = useMindMapStore();
    store.maps = [record('m1')];
    vi.stubGlobal('fetch', () => { throw new Error('offline'); });

    const ok = await store.deleteMap('m1');

    expect(ok).toBe(false);
    expect(store.maps).toHaveLength(1);
    expect(store.deleting).toBeNull();
  });

  it('一次只删一张：在途时第二次调用直接返回 false，不重复发请求', async () => {
    const store = useMindMapStore();
    store.maps = [record('m1'), record('m2')];
    let release: (value: Response) => void = () => {};
    stubFetch(() => new Promise<Response>(resolve => { release = resolve; }));

    const first = store.deleteMap('m1');
    const second = await store.deleteMap('m2');
    expect(second).toBe(false);
    expect(calls).toHaveLength(1);

    release(jsonResponse(undefined, 204));
    expect(await first).toBe(true);
    // 第二次没删 m2，它还留在列表里（不许因为"被挡下"就静默拿掉一行）。
    expect(store.maps.map(item => item.id)).toEqual(['m2']);
  });
});
