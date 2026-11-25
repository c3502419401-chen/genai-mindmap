import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia, type Pinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import NodeIntentBox from '../src/canvas/NodeIntentBox.vue';
import { INTENT_DELAY_MS, openIntentAllowed } from '../src/canvas/intent-gesture';
import type { FishboneContent, MindMapContent } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';
import { isPrefsRequest, prefsReply } from './helpers/prefs-stub';
import { isOptimizationRequest, optimizationReply } from './helpers/optimization-stub';

/**
 * 「点节点就地出输入框」。
 *
 * 三件事必须同时成立，缺一件这个功能就不成立（或者有害）：
 *
 *  1. **框长在节点旁边**，不是"把底部对话框叫回来"——所以它是 `Teleport` 到 body 的
 *     固定层，读节点 DOM 定位；
 *  2. **它改的是结构与内容（走模型），不是这一行的文字**——后者是双击那条路
 *     （`MindNode` 的内联输入框），两者互斥；
 *  3. **它不跟单击、双击、拖动抢手势**——仲裁在 `intent-gesture.ts`，四条竞态
 *     逐条钉在这里。
 */

const TREE: MindMapContent = {
  title: '报销流程',
  root: {
    id: 'root',
    label: '报销流程',
    children: [
      { id: 'a', label: '提交申请', children: [{ id: 'b', label: '材料齐全吗', children: [] }] },
      { id: 'c', label: '付款', children: [] },
    ],
  },
};

const FISH: FishboneContent = {
  title: '为什么报销要 11 天',
  diagram_type: 'fishbone',
  problem: '为什么报销要 11 天',
  bones: [
    { category: '人', root: { id: 'p1', label: '人', children: [{ id: 'p2', label: '人手不足', children: [] }] } },
    { category: '机', root: { id: 'm1', label: '机', children: [] } },
  ],
};

function stubFetch() {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (isPrefsRequest(url)) return prefsReply(url);
    if (isOptimizationRequest(url)) return optimizationReply();
    if (url.endsWith('/health')) {
      return new Response(JSON.stringify({ status: 'ok', version: 'test' }), {
        status: 200, headers: { 'content-type': 'application/json' },
      });
    }
    return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
  }));
}

/**
 * 画布替身节点（与 `node-toolbar.spec.ts` 同一形状）：`nodeRect()` 按
 * `.vue-flow__node[data-id]` 读它。jsdom 里没有 Vue Flow，测的是"定位与浮出"。
 */
function placeNode(id: string, rect = { left: 100, top: 200, width: 180, height: 48 }): HTMLElement {
  const node = document.createElement('div');
  node.className = 'vue-flow__node';
  node.setAttribute('data-id', id);
  node.getBoundingClientRect = () => ({
    x: rect.left, y: rect.top, top: rect.top, left: rect.left,
    right: rect.left + rect.width, bottom: rect.top + rect.height,
    width: rect.width, height: rect.height,
    toJSON: () => ({}),
  }) as DOMRect;
  document.body.appendChild(node);
  return node;
}

const wrappers: VueWrapper[] = [];

/** `teleport` 换成 stub：内容渲染在原地，`wrapper.find` 才拿得到（真 Teleport 到 body）。 */
function mountBox(pinia: Pinia): VueWrapper {
  const wrapper = mount(NodeIntentBox, {
    global: { plugins: [pinia], stubs: { teleport: true } },
  });
  wrappers.push(wrapper);
  return wrapper;
}

beforeEach(() => {
  // 定位走 requestAnimationFrame。同步执行、返回 0——返回非 0 会让 `if (frame)`
  // 一直挡住后续重测（与 `node-toolbar.spec.ts` 那条桩同一条理由）。
  vi.stubGlobal('requestAnimationFrame', (fn: FrameRequestCallback) => {
    fn(0);
    return 0;
  });
  vi.stubGlobal('cancelAnimationFrame', () => {});
  stubFetch();
});

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount());
  document.querySelectorAll('.vue-flow__node').forEach(node => node.remove());
  vi.unstubAllGlobals();
});

describe('就地输入框：出现与提交', () => {
  it('没有 intentTargetId 时什么都不画', () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const wrapper = mountBox(pinia);
    expect(wrapper.find('[data-test="node-intent"]').exists()).toBe(false);
  });

  it('打开之后浮在节点旁，标题写明改的是哪一个', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    placeNode('a');
    const wrapper = mountBox(pinia);
    const store = useMindMapStore();
    store.preview(TREE, 'right');

    store.openIntentFor('a');
    await wrapper.vm.$nextTick();

    const box = wrapper.get('[data-test="node-intent"]');
    expect(box.text()).toContain('提交申请');
    // 定位是固定的视口坐标（不走画布那个 `transform: scale()`）。
    expect((box.element as HTMLElement).style.left).not.toBe('');
    expect((box.element as HTMLElement).style.top).not.toBe('');
    // 提示语说的是"要怎么改"，不是"改成什么标题"——后者是双击那条路。
    const input = wrapper.get('[data-test="node-intent-input"]').element as HTMLTextAreaElement;
    expect(input.placeholder).toContain('要怎么改');
  });

  it('Enter 提交：交给 store 的那一个节点 + 那句话', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    placeNode('a');
    const wrapper = mountBox(pinia);
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    const run = vi.spyOn(store, 'runEditOn').mockResolvedValue();

    store.openIntentFor('a');
    await wrapper.vm.$nextTick();
    const input = wrapper.get('[data-test="node-intent-input"]');
    await input.setValue('把这一支拆成两步');
    await input.trigger('keydown', { key: 'Enter' });
    await wrapper.vm.$nextTick();

    expect(run).toHaveBeenCalledWith('a', '把这一支拆成两步');
    // 提交之后框自己收起来：那一句话已经在跑了，框留着只会让人以为还能再发一次。
    expect(store.intentTargetId).toBeNull();
  });

  it('Shift+Enter 与输入法组词中的回车都不提交', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    placeNode('a');
    const wrapper = mountBox(pinia);
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    const run = vi.spyOn(store, 'runEditOn').mockResolvedValue();

    store.openIntentFor('a');
    await wrapper.vm.$nextTick();
    const input = wrapper.get('[data-test="node-intent-input"]');
    await input.setValue('两行');

    await input.trigger('keydown', { key: 'Enter', shiftKey: true });
    expect(run).not.toHaveBeenCalled();

    // 中文意图几乎都要经过输入法候选框，那一步的回车不是"发送"。
    await input.trigger('keydown', { key: 'Enter', isComposing: true });
    expect(run).not.toHaveBeenCalled();
    expect(store.intentTargetId).toBe('a');
  });

  it('Esc 关掉，不提交也不改内容', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    placeNode('a');
    const wrapper = mountBox(pinia);
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    const before = JSON.stringify(store.content);

    store.openIntentFor('a');
    await wrapper.vm.$nextTick();
    const input = wrapper.get('[data-test="node-intent-input"]');
    await input.setValue('说了一半');
    await input.trigger('keydown', { key: 'Escape' });

    expect(store.intentTargetId).toBeNull();
    expect(JSON.stringify(store.content)).toBe(before);
    expect(store.undoDepth).toBe(0);
  });
});

describe('就地框与"双击改标题"不抢同一个手势', () => {
  it('开框会把编辑态让出来，反之亦然——两个框不能同时吃回车', () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useMindMapStore();
    store.preview(TREE, 'right');

    store.beginEdit('a');
    expect(store.editingId).toBe('a');
    store.openIntentFor('a');
    expect(store.intentTargetId).toBe('a');
    expect(store.editingId).toBeNull();

    store.beginEdit('a');
    expect(store.editingId).toBe('a');
    expect(store.intentTargetId).toBeNull();
  });

  it('换选中就把框关掉（否则它对着上一个节点说话）', () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useMindMapStore();
    store.preview(TREE, 'right');

    store.openIntentFor('a');
    store.select('c');
    expect(store.intentTargetId).toBeNull();
  });

  it('非树型不开框：后端只收树形状的快照，开了也发不出去', () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useMindMapStore();
    store.preview(FISH, 'right');

    expect(store.canEditIntent).toBe(false);
    store.openIntentFor('p2');
    expect(store.intentTargetId).toBeNull();
  });

  it('正在生成时不开框：提交会被 busy 挡住，等于骗用户打完字', () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.busy = true;

    expect(store.canEditIntent).toBe(false);
    store.openIntentFor('a');
    expect(store.intentTargetId).toBeNull();
  });
});

describe('手势仲裁：单击之后那 120ms 里发生了什么', () => {
  const base = { editingId: null, selectedId: 'a', exists: true, canEdit: true };

  it('四件都正常就弹', () => {
    expect(openIntentAllowed('a', base)).toBe(true);
    // 120ms 是"落在双击间隔之内、又低于可感知阈值"的那个数，不是随手取的。
    expect(INTENT_DELAY_MS).toBeGreaterThan(0);
    expect(INTENT_DELAY_MS).toBeLessThan(200);
  });

  it('双击改名已经接管 → 不弹', () => {
    expect(openIntentAllowed('a', { ...base, editingId: 'a' })).toBe(false);
  });

  it('选中换了人 → 不弹', () => {
    expect(openIntentAllowed('a', { ...base, selectedId: 'c' })).toBe(false);
  });

  it('节点被删了 → 不弹', () => {
    expect(openIntentAllowed('a', { ...base, exists: false })).toBe(false);
  });

  it('这张图开不了框（非树 / 正在跑）→ 不弹', () => {
    expect(openIntentAllowed('a', { ...base, canEdit: false })).toBe(false);
  });
});

describe('提交走的是同一个 runGeneration（不是第二条发送管线）', () => {
  it('runEditOn 发出 intent=edit、selected_ids 只有那一个节点', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useMindMapStore();
    store.preview(TREE, 'right');

    const bodies: Array<Record<string, unknown>> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (isPrefsRequest(url)) return prefsReply(url);
      if (isOptimizationRequest(url)) return optimizationReply();
      if (url.endsWith('/health')) {
        return new Response(JSON.stringify({ status: 'ok', version: 'test' }), {
          status: 200, headers: { 'content-type': 'application/json' },
        });
      }
      if (init?.body) bodies.push(JSON.parse(String(init.body)) as Record<string, unknown>);
      // 没有任何终态帧的流：这一条用例只关心"发出去的是什么"。
      const body = new ReadableStream<Uint8Array>({
        start(controller) { controller.close(); },
      });
      return { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
    }));

    // 用户此刻还圈着另一个节点（按住 Ctrl 圈过一片之后又点了这一支）——请求里
    // 不该出现它：就地框说的是"改我看见的这一处"。
    store.select('c');
    store.toggleSelect('a');
    expect(store.selectedIds.length).toBe(2);

    await store.runEditOn('a', '把这一支拆成两步');
    expect(bodies.length).toBe(1);
    expect(bodies[0].intent).toBe('edit');
    expect(bodies[0].selected_ids).toEqual(['a']);
    expect(bodies[0].goal).toBe('把这一支拆成两步');
    // `edit` 必须带快照（后端要从它上面改），而且发的是树。
    expect(bodies[0].snapshot).toBeTruthy();
  });
});
