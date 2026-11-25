import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { nextTick } from 'vue';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import MindNode from '../src/canvas/MindNode.vue';
import { CANVAS_SHELL_ID, focusCanvasShell, focusIsHomeless } from '../src/canvas/canvas-focus';
import type { MindMapContent } from '../src/api/types';
import type { MindNodeData } from '../src/stores/mindmap';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * **编辑框关掉之后，焦点回到哪儿**。
 *
 * 这条链原先断在一个不起眼的地方：内联输入框提交成功 → `editing` 变 false → input 被
 * `v-if` 卸掉 → 浏览器把焦点扔回 `document.body`。而画布的键盘模型
 * （`MindMapCanvas.ownsStructureKeys`）要求 `activeElement` 在画布外壳里，于是
 *
 * ```
 * 单击节点 → Enter 加兄弟 → 打名字 → Enter 提交 → 【再按 Enter 什么都不发生】
 * ```
 *
 * 而"连按 Enter 长一串兄弟"正是"纯键盘长节点"的主干（第二轮的实测表里那一行）。
 * 画面上完全看不出出错——用户只会觉得"按了没反应"。
 *
 * 修法只做一件事：**编辑框关掉时，如果焦点无处可去，就还给画布**。不扩成"Enter 在
 * 任何地方都能加节点"——那会让 Enter 在搜索框、在别的输入框里也触发改图。
 */

const DATA: MindNodeData = {
  label: '甲',
  body: '甲',
  fullLabel: '甲',
  depth: 1,
  out: ['right'],
  in: ['left'],
  refs: 0,
};

const wrappers: VueWrapper[] = [];

/** 画布外壳的替身：真外壳在 `MindMapCanvas`（jsdom 里跑不起来 Vue Flow）。 */
function placeShell(): HTMLElement {
  const shell = document.createElement('div');
  shell.id = CANVAS_SHELL_ID;
  shell.tabIndex = 0;
  document.body.appendChild(shell);
  return shell;
}

function renderNode(props: Record<string, unknown> = {}): VueWrapper {
  const wrapper = mount(MindNode, {
    props: { id: 'n1', data: DATA, ...props },
    // 句柄组件依赖 Vue Flow 的画布上下文，这一组用例只关心焦点，换成空壳。
    global: { stubs: { Handle: true } },
    attachTo: document.body,
  });
  wrappers.push(wrapper);
  return wrapper;
}

/** 父组件"提交成功之后把编辑态关掉"那一步：input 卸载，焦点随之掉到 body。 */
async function closeEditor(wrapper: VueWrapper): Promise<void> {
  await wrapper.setProps({ editing: false });
  await nextTick();
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount());
  document.body.innerHTML = '';
});

describe('焦点入口：一处定义，三处用', () => {
  it('外壳在页面上时能聚焦并返回成功；不在页面上返回 false 而不抛', () => {
    expect(focusCanvasShell()).toBe(false);
    const shell = placeShell();
    expect(focusCanvasShell()).toBe(true);
    expect(document.activeElement).toBe(shell);
  });

  it('「无处可去」只认 body / 没有元素 / 元素已被摘掉', () => {
    expect(focusIsHomeless()).toBe(true);

    const input = document.createElement('input');
    document.body.appendChild(input);
    input.focus();
    // 焦点在输入框里 —— 用户正在打字，抢回来就是坏的。
    expect(focusIsHomeless()).toBe(false);

    const button = document.createElement('button');
    document.body.appendChild(button);
    button.focus();
    // 焦点在按钮上 —— 用户接下来要按空格/回车按那个按钮。
    expect(focusIsHomeless()).toBe(false);

    button.remove();
    // 元素被摘掉了：`activeElement` 还指着它，但它已经不在文档里。
    expect(focusIsHomeless()).toBe(true);
  });
});

describe('内联编辑关闭之后，焦点回到画布（连按 Enter 才长得下去）', () => {
  it('Enter 提交：提交了，而且焦点从 body 还给画布', async () => {
    const shell = placeShell();
    const wrapper = renderNode({ editing: true });

    await wrapper.get('input').setValue('改过的标题');
    await wrapper.get('input').trigger('keydown', { key: 'Enter' });
    await closeEditor(wrapper);

    expect(wrapper.emitted('commit')).toEqual([['改过的标题']]);
    // 没有这一句，画布上按 Enter 就是"没反应"。
    expect(document.activeElement).toBe(shell);
  });

  it('Esc 取消：焦点同样还回去（取消不该让人卡在键盘死角）', async () => {
    const shell = placeShell();
    const wrapper = renderNode({ editing: true });

    await wrapper.get('input').trigger('keydown', { key: 'Escape' });
    await closeEditor(wrapper);

    expect(wrapper.emitted('cancel')).toHaveLength(1);
    expect(document.activeElement).toBe(shell);
  });

  it('Tab（加下一个兄弟）**不**抢焦点：那条链的下一个输入框自己会聚焦', async () => {
    const shell = placeShell();
    const wrapper = renderNode({ editing: true });

    await wrapper.get('input').trigger('keydown', { key: 'Tab' });
    await closeEditor(wrapper);

    expect(wrapper.emitted('next')).toHaveLength(1);
    // 两处都抢会打架：`focusInput()` 追着新输入框要焦点，`focusCanvasShell()` 把它
    // 拽回画布——最终落到哪个由 rAF 的顺序决定，那是一次"时灵时不灵"。
    expect(document.activeElement).not.toBe(shell);
  });

  it('焦点已经在别处（用户点了按钮）时，关闭不把它抢回来', async () => {
    const shell = placeShell();
    const button = document.createElement('button');
    document.body.appendChild(button);
    const wrapper = renderNode({ editing: true });
    // 让 `focusInput()`（`editing` 变真时那一次）先跑完，否则它会在后面把焦点从
    // 按钮上抢回输入框，这条用例就测不到想测的东西。
    await flushPromises();
    await new Promise(resolve => setTimeout(resolve, 60));

    button.focus();
    await wrapper.get('input').trigger('keydown', { key: 'Enter' });
    await closeEditor(wrapper);

    expect(document.activeElement).toBe(button);
    expect(document.activeElement).not.toBe(shell);
  });
});

describe('连按两次 Enter 长出两个兄弟（这就是那条链的验收）', () => {
  const TREE: MindMapContent = {
    title: '键盘长节点',
    root: {
      id: 'root',
      label: '根',
      children: [
        { id: 'a', label: '甲', children: [] },
        { id: 'b', label: '乙', children: [] },
      ],
    },
  };

  type Store = ReturnType<typeof useMindMapStore>;

  /**
   * 键盘路径的那两下。`onKeydown` 用 `store.selectedId`，打字提交用 `store.editingId`
   * ——两者都是焦点回来之后才成立的前提。
   */
  function pressEnter(store: Store): void {
    if (store.selectedId) store.addSiblingTo(store.selectedId);
  }

  function typeAndCommit(store: Store, text: string): void {
    const id = store.editingId;
    if (id) store.commitLabel(id, text);
  }

  it('两次回车之后 root 下面多出两个并列兄弟，顺序就是按键顺序', () => {
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.select('a');

    pressEnter(store);
    expect(store.editingId).toBeTruthy();
    // 新节点的输入框是自动打开的（`addSiblingTo` 里设 `editingId`），所以第二下
    // Enter 的前提是"提交之后焦点回到画布、选中还停在这个新节点上"。
    expect(store.selectedId).toBe(store.editingId);
    typeAndCommit(store, '第二个');

    pressEnter(store);
    typeAndCommit(store, '第三个');

    const labels = store.content!.root.children.map(child => child.label);
    expect(labels).toEqual(['甲', '第二个', '第三个', '乙']);
    // 四次内容改动 = 四步撤销（加兄弟 + 改名字，各两次）。逐步可退，不是"两次操作
    // 揉成一步"。
    expect(store.undoDepth).toBe(4);
  });

  it('提交之后选中停在刚加的那个节点上（下一个兄弟从它长出，不是从原来那个）', () => {
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.select('a');

    pressEnter(store);
    const first = store.editingId!;
    typeAndCommit(store, '第二个');

    expect(store.selectedId).toBe(first);
    expect(store.editingId).toBeNull();
  });
});
