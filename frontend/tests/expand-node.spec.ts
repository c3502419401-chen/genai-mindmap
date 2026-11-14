import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { MindMapContent } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';

// jsdom 里没有 ResizeObserver，真正的 Vue Flow 画布跑不起来；这一组用例只关心
// 详情面板上那个按钮与它发出的请求，画布整体换成占位元素。
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

/**
 * 一棵带三态的树：
 *   - `risk` 是能展开的叶子（`yes`）；
 *   - `done` 已经到底（`no`）；
 *   - `old` 是不确定（旧图没有这个字段时也是它）。
 * 三个节点都在第 2 层，索引链都是 `[0, n]`。
 */
const TREE: MindMapContent = {
  title: '测试方案',
  root: {
    id: 'root', label: '测试方案', expandable: 'yes', children: [
      {
        id: 'a', label: '验收', expandable: 'yes', children: [
          { id: 'risk', label: '风险', expandable: 'yes', children: [] },
          { id: 'done', label: '已到底', expandable: 'no', children: [] },
          { id: 'old', label: '旧节点', children: [] },
        ],
      },
    ],
  },
};

/** 把若干事件包成 SSE 帧，模拟后端 /api/agent/runs/stream。 */
function agentReply(frames: unknown[]): Response {
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

/** 记录每次 fetch 的 URL 与 body，方便断言请求形态。 */
function stubFetch(reply: () => Response) {
  const calls: Array<{ url: string; body: unknown }> = [];
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), body: init?.body ? JSON.parse(String(init.body)) : null });
    return reply();
  });
  vi.stubGlobal('fetch', mock);
  return calls;
}

/** 手动控制节奏的流：状态行与按钮文字只在流进行中才有值。 */
function controllableReply() {
  const encoder = new TextEncoder();
  let push: (frame: unknown) => void = () => {};
  let finish: () => void = () => {};
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      push = (frame) => {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`));
      };
      finish = () => {
        controller.enqueue(encoder.encode('data: [DONE]\n\n'));
        controller.close();
      };
    },
  });
  const response = { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
  return { response, push, finish };
}

function mountApp(): VueWrapper {
  return mount(App, { global: { plugins: [createPinia()] } });
}

/** 详情面板里的那个按钮（按分区 aria-label 定位，与「加子节点」那些区分开）。 */
const EXPAND_SECTION = '[aria-label="展开这一支"]';

function expandButton(wrapper: VueWrapper) {
  const button = wrapper.find(`${EXPAND_SECTION} button`);
  if (!button.exists()) throw new Error('详情面板里没有「展开这一支」按钮');
  return button;
}

/** 打开这棵树并选中某个节点（组件挂载之后调，`select` 才找得到节点）。 */
function openAndSelect(nodeId: string) {
  const store = useMindMapStore();
  store.preview(TREE, 'right');
  store.select(nodeId);
  return store;
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('展开按钮的三态', () => {
  it('能展开的节点：按钮可点，说明写清结果先落候选', async () => {
    const wrapper = mountApp();
    openAndSelect('risk');
    await wrapper.vm.$nextTick();

    const button = expandButton(wrapper);
    expect(button.text()).toBe('展开这一支');
    expect(button.attributes('disabled')).toBeUndefined();
    expect(wrapper.find(EXPAND_SECTION).text()).toContain('确认后才进画布');
  });

  it('expandable=no：按钮禁用，并说明已经到底了', async () => {
    const wrapper = mountApp();
    openAndSelect('done');
    await wrapper.vm.$nextTick();

    const section = wrapper.find(EXPAND_SECTION);
    expect(expandButton(wrapper).attributes('disabled')).toBeDefined();
    // 灰着不解释等于让用户以为功能坏了
    expect(section.text()).toContain('已经到底了');
  });

  it('旧图没有 expandable 字段：仍然可点，并说明只是不确定', async () => {
    const wrapper = mountApp();
    openAndSelect('old');
    await wrapper.vm.$nextTick();

    expect(expandButton(wrapper).attributes('disabled')).toBeUndefined();
    expect(wrapper.find(EXPAND_SECTION).text()).toContain('不确定');
  });
});

describe('点击之后的请求形态', () => {
  it('走 /agent/runs/stream：intent=expand、snapshot=当前整棵树、selected_ids 只有一个', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 1, content: TREE }]));
    const wrapper = mountApp();
    openAndSelect('risk');
    await wrapper.vm.$nextTick();

    await expandButton(wrapper).trigger('click');
    await vi.waitFor(() => expect(calls).toHaveLength(1));

    expect(calls[0].url).toContain('/agent/runs/stream');
    const body = calls[0].body as Record<string, unknown>;
    expect(body.intent).toBe('expand');
    expect(body.selected_ids).toEqual(['risk']);
    // 整棵树都要带上（用户可能有一堆没保存的编辑，后端不能从库里重读）
    expect((body.snapshot as MindMapContent).root.id).toBe('root');
    expect(body.goal).toContain('风险');
  });

  it('没带材料也照样发：正文只在会话内存里，找回原文是后端的事', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 1, content: TREE }]));
    const wrapper = mountApp();
    openAndSelect('risk');
    await wrapper.vm.$nextTick();

    await expandButton(wrapper).trigger('click');
    await vi.waitFor(() => expect(calls).toHaveLength(1));

    const body = calls[0].body as Record<string, unknown>;
    // 空数组不发（旧后端 extra="forbid"）；不传就是"没有材料"
    expect(body.sources ?? []).toEqual([]);
  });

  it('expandable=no 时点击不发请求，直调 store 也被拦下', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 1, content: TREE }]));
    const wrapper = mountApp();
    const store = openAndSelect('done');
    await wrapper.vm.$nextTick();

    // 第一层是 DOM：按钮 disabled，浏览器根本不派发 click
    await expandButton(wrapper).trigger('click');
    await wrapper.vm.$nextTick();
    expect(calls).toHaveLength(0);

    // 第二层是 store：绕过组件直调（测试、将来的快捷入口）时也得拦住，并给出原因
    await store.expandSelected();
    expect(calls).toHaveLength(0);
    expect(store.status).toContain('已经到底了');
  });
});

describe('展开中与生成中互斥', () => {
  it('展开期间按钮转「展开中…」并禁用', async () => {
    const ctrl = controllableReply();
    stubFetch(() => ctrl.response);
    const wrapper = mountApp();
    const store = openAndSelect('risk');
    await wrapper.vm.$nextTick();

    const running = store.expandSelected();
    await vi.waitFor(() => expect(store.expanding).toBe(true));
    await wrapper.vm.$nextTick();

    expect(store.generating).toBe(false);
    expect(expandButton(wrapper).text()).toBe('展开中…');
    expect(expandButton(wrapper).attributes('disabled')).toBeDefined();

    ctrl.push({ type: 'done', seq: 1, content: TREE });
    ctrl.finish();
    await running;
    await wrapper.vm.$nextTick();
    expect(expandButton(wrapper).text()).toBe('展开这一支');
  });

  it('展开期间再点生成不会发出第二个请求', async () => {
    const ctrl = controllableReply();
    const calls = stubFetch(() => ctrl.response);
    const wrapper = mountApp();
    const store = openAndSelect('risk');
    await wrapper.vm.$nextTick();

    const running = store.expandSelected();
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    await store.runGeneration('重新生成一张', 'create');
    expect(calls).toHaveLength(1);

    ctrl.push({ type: 'done', seq: 1, content: TREE });
    ctrl.finish();
    await running;
  });

  it('生成期间「展开这一支」是禁用的，点了也不会插队', async () => {
    const ctrl = controllableReply();
    const calls = stubFetch(() => ctrl.response);
    const wrapper = mountApp();
    const store = openAndSelect('risk');
    await wrapper.vm.$nextTick();

    const generating = store.runGeneration('重新生成一张', 'create');
    await vi.waitFor(() => expect(store.generating).toBe(true));
    await wrapper.vm.$nextTick();

    expect(expandButton(wrapper).attributes('disabled')).toBeDefined();
    expect(wrapper.find(EXPAND_SECTION).text()).toContain('正在生成');
    await store.expandSelected();
    expect(calls).toHaveLength(1);

    ctrl.push({ type: 'done', seq: 1, content: TREE });
    ctrl.finish();
    await generating;
  });
});

describe('候选 → 确认 → 应用', () => {
  it('ops 批次落进候选，点应用后新节点挂在被展开的节点下', async () => {
    const batch = {
      batch_id: 'b1-0',
      base_revision: 0,
      ops: [
        { op: 'add', id: 'n1', parent_id: 'risk', index: 0, label: '冒烟范围', detail: '先跑冒烟。' },
        { op: 'add', id: 'n2', parent_id: 'risk', index: 1, label: '回归范围', detail: '回归覆盖核心链路。' },
      ],
    };
    stubFetch(() => agentReply([
      { type: 'tool_start', seq: 1, call_id: 'c1', name: 'node_expand' },
      { type: 'ops', seq: 2, call_id: 'c1', batch },
      { type: 'tool_end', seq: 3, call_id: 'c1', name: 'node_expand', ok: true, result_summary: {} },
      { type: 'done', seq: 4, mode: 'model' },
    ]));
    const wrapper = mountApp();
    const store = openAndSelect('risk');
    await wrapper.vm.$nextTick();

    await expandButton(wrapper).trigger('click');
    await vi.waitFor(() => expect(store.candidate).not.toBeNull());

    // 候选只是"看得见"，画布还没变
    expect(store.content?.root.children[0].children[0].children).toEqual([]);
    const candidateRisk = store.candidate!.root.children[0].children[0];
    expect(candidateRisk.children.map(child => child.label)).toEqual(['冒烟范围', '回归范围']);

    expect(store.acceptCandidate()).toBe('applied');
    const risk = store.content!.root.children[0].children[0];
    expect(risk.children.map(child => child.label)).toEqual(['冒烟范围', '回归范围']);
    expect(risk.children[0].detail).toBe('先跑冒烟。');
    // 其余分支一个节点都没动
    expect(store.content!.root.children[0].children.map(child => child.label))
      .toEqual(['风险', '已到底', '旧节点']);
  });
});

describe('展开的生长预览', () => {
  it('node_grow 落在被展开的那个节点下（不是根下）', async () => {
    const ctrl = controllableReply();
    stubFetch(() => ctrl.response);
    const wrapper = mountApp();
    const store = openAndSelect('risk');
    await wrapper.vm.$nextTick();

    const running = store.expandSelected();
    await vi.waitFor(() => expect(store.expanding).toBe(true));

    // 后端给的 path 是**父节点**的索引链：[0, 0] = root > 验收 > 风险
    ctrl.push({ type: 'node_grow', seq: 1, path: [0, 0], label: '冒烟范围', expandable: true });
    await wrapper.vm.$nextTick();

    const preview = store.agentPreview;
    expect(preview).not.toBeNull();
    // 预览是一条祖先链：根 → 验收 → 风险，新节点挂在最下面那一层
    expect(preview!.children[0].label).toBe('验收');
    const target = preview!.children[0].children[0];
    expect(target.label).toBe('风险');
    expect(target.children.map(child => child.label)).toEqual(['冒烟范围']);
    // 面板标题行说的是"正在展开谁"，而不是中心主题
    expect(store.agentPreviewScope).toBe('风险');
    expect(wrapper.text()).toContain('正在展开：风险');

    ctrl.push({ type: 'done', seq: 2, content: TREE });
    ctrl.finish();
    await running;
    // 终态一到，预览整棵作废（它没经过整树校验）
    expect(store.agentPreview).toBeNull();
    expect(store.agentPreviewScope).toBeNull();
  });
});
