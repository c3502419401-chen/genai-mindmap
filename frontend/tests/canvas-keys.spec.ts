import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { defineComponent, h } from 'vue';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import MindMapCanvas from '../src/canvas/MindMapCanvas.vue';
import MessageFab from '../src/components/MessageFab.vue';
import type { MindMapContent } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';
import { isPrefsRequest, prefsReply } from './helpers/prefs-stub';

/**
 * 结构键（Tab / Enter / Delete / F2）的归属。
 *
 * 这一组钉的是一个数据损坏级的形状：这些键原先挂在 `window` 上、只排除输入框。
 * 于是"选中一个节点"之后，焦点落在哪个按钮上都不影响它——用户按 Tab 想切到下一个
 * 控件，图里就多了一个子节点；按 Enter 想按下那个按钮，又多了一个兄弟节点。而
 * `preventDefault` 还把焦点移动本身吞掉了，用户看到的是"Tab 坏了"。
 *
 * 两条用例必须同时在：一条挡"不该动的时候动了图"，一条挡"该动的时候不动图"。
 *
 * ## 为什么只挂这两件东西，而不是整个 `App`（判 C：减挂载）
 *
 * 判据在 `MindMapCanvas` 身上：`onKeydown` 挂在 `window` 上，`ownsStructureKeys()`
 * 读 `shell.contains(document.activeElement)` 与 `isControl()`。所以这个 bug 的
 * 两个必要条件是"画布外壳存在"和"有个画布**外**的真实控件能拿焦点"。
 *
 * 从前 `mount(App)` 就为了这两样，把 `ChatDock` / `SettingsPanel` / `HistoryDrawer` /
 * `StructureCheck` / `NodeToolbar` / `AgentPanel` 一起挂了起来——223 个 DOM 节点、
 * 首次求值实测 1710ms（冷）/ 318ms（热），而这两样只需要 49 个节点、98ms。
 * 加压时那个差值就是它裸奔在 5 秒线上的全部原因（`docs/TEST-COST-3-CASES.md` §3）。
 *
 * 换掉的是**无关的兄弟组件**，不是断言对象：画布还是 `MindMapCanvas`，
 * 画布外那个按钮还是 `MessageFab` 真实渲染的 `[data-test="message-fab"]`，
 * `store.nodeCount` / `defaultPrevented` 一个都没动。
 *
 * ## 剩下的 2.4 秒是 A（真画布），所以给上限
 *
 * 减挂载之后最坏的一条仍要 2446ms（本地加压、两份全量同时跑）。剩下的钱花在
 * `MindMapCanvas` 的首次求值上：Vue Flow 建图 + `MindNode` 真渲染 + `window`
 * 键盘监听的注册。这三样正是被侧的行为本身（判据在组件里），stub 掉就没得测了。
 * CI 估 ≈ 2446 × 1.3–2 = 3.2–4.9s——压在 5 秒线边上，所以给 8 秒
 * （= 本地加压 3.3×、CI 估的 1.6–2.5×）。
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

const CONTENT: MindMapContent = {
  title: '示例',
  root: { id: 'r', label: '根', children: [{ id: 'a', label: '甲', children: [] }] },
};

const CANVAS = '[aria-label^="导图画布"]';
const OUTSIDE_BUTTON = '[data-test="message-fab"]';

/**
 * 最小壳：画布 + 画布外那个真实按钮。
 *
 * `MessageFab` 留在里面而不是用裸 `<button>`：结构键的判据之一是 `isControl()`，
 * 它读的是真实控件的 `tagName` / `role`。用合成按钮就把"按钮长什么样"这件事从
 * 被测行为里偷走了。
 */
const Shell = defineComponent({
  components: { MindMapCanvas, MessageFab },
  setup: () => () => h('div', [h(MindMapCanvas), h(MessageFab)]),
});

function mountShell(): VueWrapper {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(Shell, { attachTo: document.body, global: { plugins: [pinia] } });
}

function keydown(target: EventTarget, key: string): KeyboardEvent {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
}

/** 挂一张图并选中一个节点——"选中之后按键"才是这个 bug 的入口。 */
async function withSelectedNode(wrapper: VueWrapper): Promise<ReturnType<typeof useMindMapStore>> {
  await flushPromises();
  const store = useMindMapStore();
  store.loadMap(CONTENT, { id: 'm1', version: 1, updated_at: '2026-11-05' });
  store.select('a');
  await wrapper.vm.$nextTick();
  return store;
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  vi.stubGlobal('DOMMatrixReadOnly', DOMMatrixReadOnlyStub);
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (isPrefsRequest(url)) return prefsReply(url);
    return { ok: true, status: 200, json: async () => ({}) } as unknown as Response;
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

describe('画布的结构键只在画布有焦点时生效', { timeout: 8_000 }, () => {
  it('焦点在别的按钮上：Tab / Enter 不改图，也不吞掉默认的焦点移动', async () => {
    const wrapper = mountShell();
    const store = await withSelectedNode(wrapper);

    const button = wrapper.get(OUTSIDE_BUTTON).element as HTMLButtonElement;
    button.focus();
    const before = store.nodeCount;

    const tab = keydown(button, 'Tab');
    expect(tab.defaultPrevented).toBe(false);
    expect(store.nodeCount).toBe(before);

    const enter = keydown(button, 'Enter');
    expect(enter.defaultPrevented).toBe(false);
    expect(store.nodeCount).toBe(before);

    // Delete 是最危险的那一个：不拦的话，用户想删一个字就把选中的节点删了。
    const back = keydown(button, 'Delete');
    expect(back.defaultPrevented).toBe(false);
    expect(store.nodeCount).toBe(before);

    wrapper.unmount();
  });

  it('焦点在画布上：Tab 加子节点、Enter 加兄弟节点，原来的键盘操作仍在', async () => {
    const wrapper = mountShell();
    const store = await withSelectedNode(wrapper);

    const canvas = wrapper.get(CANVAS).element as HTMLElement;
    canvas.focus();
    expect(document.activeElement).toBe(canvas);
    const before = store.nodeCount;

    const tab = keydown(canvas, 'Tab');
    await wrapper.vm.$nextTick();
    expect(tab.defaultPrevented).toBe(true);
    expect(store.nodeCount).toBe(before + 1);

    // 新节点是编辑态，Enter 归输入框；先退出编辑再验 Enter 那条。
    store.endEdit();
    store.select('a');
    await wrapper.vm.$nextTick();
    canvas.focus();
    keydown(canvas, 'Enter');
    await wrapper.vm.$nextTick();
    expect(store.nodeCount).toBe(before + 2);

    wrapper.unmount();
  });
});
