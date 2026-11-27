import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia } from 'pinia';
import { afterEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import { isPrefsRequest, prefsReply } from './helpers/prefs-stub';

vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

/**
 * **破坏性动作的覆盖扫描**（"用户会做、但用例里零出现"的那一类）。
 *
 * 起因是一条契约用例：`DELETE /api/mindmaps/{id}` 曾经实测 405，而当时 3311 条用例
 * 一条都没抓到——因为前端的 `fetch` 被替身，405 只在 HTTP 层存在。补它的同时按同一
 * 把尺子过了一遍所有破坏性动作，逐个核对"后端有没有契约用例、界面这一侧有没有人点过"：
 *
 * | 动作 | 后端契约 | 界面点击 |
 * | --- | --- | --- |
 * | 删除导图 `DELETE /mindmaps/{id}` | `tests/test_map_delete.py`（8 条） | `defects.spec.ts` / `user-path-input.spec.ts` |
 * | 彻底删除材料 `DELETE /knowledge/library/{id}` | `tests/test_knowledge_delete.py` | `app.spec.ts` / `sources-delete.spec.ts` |
 * | 删除一条别名 `DELETE /knowledge/aliases/{alias}` | `tests/test_knowledge_aliases.py` | `app.spec.ts` |
 * | **清空全部别名 `DELETE /knowledge/aliases`** | `tests/test_knowledge_aliases.py` | **零** ← 就是这一条 |
 * | 取消候选任务 `DELETE /aliases/candidates/{job}` | `tests/test_alias_preview.py` | `alias-resolver.spec.ts` |
 * | 删记忆 `DELETE /memory/{id}` | `tests/test_memory_router.py` | （无产品入口） |
 * | 删偏好 `DELETE /prefs/{key}` | `tests/test_prefs.py` | `prefs.spec.ts` |
 *
 * 唯一空着的是「清空全部别名」：**整表不可恢复**，只能靠 `window.confirm` 挡一下，
 * 而"确认之后发了什么、取消之后是不是真的一条都不发"两件事都没人验过。它坏掉时的
 * 形状与当初那个 405 完全一样——按钮还在、点了没反应，没有任何红灯。
 */
const ALIAS = {
  alias: '市场监管总局',
  canonical: '国家市场监督管理总局',
  source: 'substring',
  similarity: 0.93,
  created_at: '2026-11-16T10:00:00.000Z',
};

function json(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

/** 别名清单 + 整表清空两个端点；其余背景请求（健康检查 / 偏好）给空答复。 */
function stubAliases() {
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (isPrefsRequest(url)) return prefsReply(url);
    if (url.endsWith('/health')) return json({ status: 'ok', version: 'test' });
    if (init?.method === 'DELETE' && url.endsWith('/knowledge/aliases')) {
      // 整表清空的回执是 `AliasClearance`：只有 `deleted`，没有"哪一条"。
      return json({ deleted: 1 });
    }
    if (url.includes('/knowledge/aliases')) return json({ items: [ALIAS], total: 1, configured: true });
    if (url.includes('/knowledge/library')) return json({ items: [], total: 0 });
    return json({});
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

function mountApp(): VueWrapper {
  return mount(App, { global: { plugins: [createPinia()] } });
}

function byText(wrapper: VueWrapper, text: string) {
  const found = wrapper.findAll('button').find(item => item.text() === text);
  if (!found) throw new Error(`找不到按钮：${text}`);
  return found;
}

/** 打开知识库抽屉并把「实体别名」展开——清空按钮只在那里出现。 */
async function openAliasList(wrapper: VueWrapper): Promise<void> {
  await byText(wrapper, '知识库').trigger('click');
  await byText(wrapper, '查看').trigger('click');
  await vi.waitFor(() => expect(wrapper.text()).toContain(ALIAS.alias));
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('清空全部别名', () => {
  it('确认之后才发 DELETE（整表端点），并说出清掉了几条', async () => {
    const fetchMock = stubAliases();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const wrapper = mountApp();
    await flushPromises();
    await openAliasList(wrapper);

    await byText(wrapper, '清空全部别名').trigger('click');
    await vi.waitFor(() => expect(wrapper.text()).toContain('已清空'));

    // 确认框必须写清代价：清空之后只能重跑实体消解，而重跑会按同一批阈值重新猜一遍。
    const asked = String(confirm.mock.calls[0]?.[0] ?? '');
    expect(asked).toContain('重跑');
    expect(asked).toContain('实体与证据边');
    // 打的是**整表**那个端点（路径到 `/knowledge/aliases` 为止，没有 id 段）。
    const cleared = fetchMock.mock.calls.find(([url, init]) =>
      (init as RequestInit | undefined)?.method === 'DELETE'
      && String(url).endsWith('/knowledge/aliases'));
    expect(cleared, '没有发出清空请求').toBeTruthy();
    // 回执里的 `deleted` 要落到界面上，否则用户不知道刚清掉几条。
    expect(wrapper.text()).toContain('已清空 1 条别名');
  });

  it('取消就一个请求都不发，清单还在', async () => {
    const fetchMock = stubAliases();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const wrapper = mountApp();
    await flushPromises();
    await openAliasList(wrapper);

    await byText(wrapper, '清空全部别名').trigger('click');
    await flushPromises();

    expect(confirm).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls.some(
      ([, init]) => (init as RequestInit | undefined)?.method === 'DELETE')).toBe(false);
    expect(wrapper.text()).toContain(ALIAS.alias);
  });
});
