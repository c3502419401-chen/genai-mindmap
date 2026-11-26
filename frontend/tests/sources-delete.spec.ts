import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useSourcesStore } from '../src/stores/sources';

/**
 * 「传错了要能删干净」这条链的前端半边。
 *
 * 后端那半边早就有了（`DELETE /api/knowledge/library/{source_id}`，被引用时 409 +
 * `force=true`），缺的是**材料列表里没有把这个动作交到用户手上**：`移除` 只把材料
 * 从这一轮生成里拿掉，库里那份不动。于是"传错一份、生成过一次"之后，用户在列表里
 * 点掉它，知识库里还留着它，还占着 200 份上限的一个位置。
 *
 * 未生成过的材料是另一种情况：它从没入过库（入库唯一入口是生成时的
 * `remember_sources`），所以 **不能** 替它发 DELETE——那只会拿到 404，然后把
 * "它早就不在"显示成"删除失败"。
 */

const json = (body: unknown, status = 200) => ({
  ok: status < 400, status, json: async () => body,
}) as Response;

const DELETED = { source_id: 'lib-1', deleted: 1, cited_by: 0 };

const flush = () => new Promise(resolve => setTimeout(resolve, 0));

let pinia: Pinia;
beforeEach(() => {
  pinia = createPinia();
  setActivePinia(pinia);
});
afterEach(() => {
  disposePinia(pinia);
  vi.unstubAllGlobals();
});

/** 只认 DELETE，其余一律抛：这条链不该顺带发别的请求。 */
function deleteFetch(handler: (url: string) => Response) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method !== 'DELETE') throw new Error(`未预期的请求：${init?.method} ${url}`);
    return handler(url);
  });
}

describe('材料的彻底删除', () => {
  it('未生成过的材料只从会话移出，不向后端发删除请求', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();
    store.addText('一份传错的正文');

    const [item] = store.items;
    // 从没入库：id 还是行身份本身。
    expect(store.inLibrary(item)).toBe(false);

    await store.forget(item.key);

    expect(store.items).toHaveLength(0);
    // 关键断言：库里本来就没有它，发 DELETE 只会拿到 404。
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('已入库的材料删除时打的是库里的持久 id，不是临时 id', async () => {
    const urls: string[] = [];
    vi.stubGlobal('fetch', deleteFetch(url => {
      urls.push(url);
      return json(DELETED);
    }));
    const store = useSourcesStore();
    store.addText('生成过一次的正文');
    const key = store.items[0].key;

    // 生成时后端把临时 id 换成库里的持久 id（`sources` 帧的 `mapping`）。
    store.remap({ [key]: 'lib-1' });
    expect(store.inLibrary(store.items[0])).toBe(true);

    await store.forget(key);

    expect(urls).toEqual(['/api/knowledge/library/lib-1']);
    expect(store.items).toHaveLength(0);
  });

  it('被导图引用时先拦一次：行留着、按钮切成"确认删除"，再点才带 force', async () => {
    const urls: string[] = [];
    vi.stubGlobal('fetch', deleteFetch(url => {
      urls.push(url);
      if (url.includes('force=true')) return json({ ...DELETED, cited_by: 2 });
      return json({
        code: 'source_cited',
        message: '这份材料还被 2 张导图引用：删掉之后那些节点会显示「来源原文不可用」，'
          + '且无法恢复。确实要删就带 force=true 再试一次。',
        detail: { cited_by: 2 },
      }, 409);
    }));
    const store = useSourcesStore();
    store.addText('被图引用的正文');
    const key = store.items[0].key;
    store.remap({ [key]: 'lib-1' });

    await store.forget(key);

    // 409 不是失败：材料还在列表里，退出码与原因都留给用户看。
    expect(store.items).toHaveLength(1);
    expect(store.items[0].deleteState).toBe('armed');
    expect(store.items[0].error).toContain('2 张导图引用');
    expect(urls).toEqual(['/api/knowledge/library/lib-1']);

    await store.forget(key);

    expect(urls).toEqual([
      '/api/knowledge/library/lib-1',
      '/api/knowledge/library/lib-1?force=true',
    ]);
    expect(store.items).toHaveLength(0);
  });

  it('删除失败（非引用冲突）时行留着、回到可再试的状态', async () => {
    vi.stubGlobal('fetch', deleteFetch(() => json(
      { code: 'library_unavailable', message: '知识库暂时读不出来，请稍后重试' }, 503,
    )));
    const store = useSourcesStore();
    store.addText('正文');
    const key = store.items[0].key;
    store.remap({ [key]: 'lib-1' });

    await store.forget(key);

    expect(store.items).toHaveLength(1);
    // 不是 `armed`：503 不是"再点一次就能过"的那种拦，用户要等的是库恢复。
    expect(store.items[0].deleteState).toBe('idle');
    expect(store.items[0].error).toContain('知识库暂时读不出来');
  });

  it('两份内容相同的材料共享一个持久 id：删掉一份，另一份退回未入库', async () => {
    vi.stubGlobal('fetch', deleteFetch(() => json(DELETED)));
    const store = useSourcesStore();
    store.addText('同样的正文');
    store.addText('同样的正文');
    const [first, second] = store.items.map(item => item.key);
    // 内容相同 → 知识库收成同一条记录（`remap` 的 docstring：映射之后 id 允许相同）。
    store.remap({ [first]: 'lib-1', [second]: 'lib-1' });
    expect(store.items.every(item => store.inLibrary(item))).toBe(true);

    await store.forget(first);

    expect(store.items).toHaveLength(1);
    // 库里那条已经删了，剩下这行不能继续自称"已在知识库"——它现在只是内存里的材料，
    // 下次生成会重新入库。
    expect(store.items[0].source?.id).toBe(second);
    expect(store.inLibrary(store.items[0])).toBe(false);
  });

  it('删除结束后错误信息不留：同一行不会带着上一次的 409 去下一次生成', async () => {
    let calls = 0;
    vi.stubGlobal('fetch', deleteFetch(() => {
      calls += 1;
      if (calls === 1) return json({ code: 'source_cited', message: '还被 1 张导图引用' }, 409);
      return json(DELETED);
    }));
    const store = useSourcesStore();
    store.addText('正文');
    const key = store.items[0].key;
    store.remap({ [key]: 'lib-1' });

    await store.forget(key);
    expect(store.items[0].error).toBe('还被 1 张导图引用');

    await flush();
    await store.forget(key);
    expect(store.items).toHaveLength(0);
  });
});
