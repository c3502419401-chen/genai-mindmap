/**
 * 「上次的运行结果」：run 身份落到界面上的那一条链路。
 *
 * 验的是"这张表有没有人读"——`runs` 里的结果若没人捡，那四十秒只是从内存白花
 * 变成写进数据库白花。所以这里从探测一直走到"载入 → 保存 → 上报已保存"。
 *
 * 注意这里没有"候选"这个中间层：生成结果当时**已经进了画布**（只是没保存），
 * 所以「载入」是把它装回画布（可撤销），不是"把一份提案摆出来等用户点应用"。
 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { MindMapContent, RunDetail, RunSummary } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';

// jsdom 里没有 ResizeObserver，真正的 Vue Flow 画布跑不起来。
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

const RESULT: MindMapContent = {
  title: '上次那张图',
  root: {
    id: 'r',
    label: '机器学习',
    children: [{ id: 'a', label: '监督学习', children: [] }],
  },
};

function summary(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    run_id: 'run-abc',
    status: 'done',
    intent: 'create',
    goal: '机器学习',
    created_at: '2024-01-01T03:00:00Z',
    updated_at: '2024-01-01T03:12:00Z',
    applied_at: null,
    ...overrides,
  };
}

function detail(overrides: Partial<RunDetail> = {}): RunDetail {
  return { ...summary(), candidate: RESULT, mode: 'model', failure: null, ...overrides };
}

interface Call {
  url: string;
  method: string;
}

/** 按 URL 分派的 fetch 桩：每条路都回它该回的东西，并记下调用顺序。 */
function stubFetch(routes: {
  list?: RunSummary[] | 'fail';
  detail?: RunDetail | null;
}) {
  const calls: Call[] = [];
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, method: init?.method ?? 'GET' });
    if (url.endsWith('/applied')) {
      return { ok: true, status: 204, json: async () => ({}) } as unknown as Response;
    }
    if (url.includes('/api/mindmaps')) {
      // 保存：回一条记录，让 `reportRunSaved()` 真的走到
      return {
        ok: true, status: 200,
        json: async () => ({
          id: 'map-new', version: 1, content: RESULT, updated_at: '2024-01-01T04:00:00Z',
        }),
      } as unknown as Response;
    }
    if (url.includes('/agent/runs/')) {
      const found = routes.detail === undefined ? detail() : routes.detail;
      if (!found) return { ok: false, status: 404, json: async () => ({ message: '没有' }) } as unknown as Response;
      return { ok: true, status: 200, json: async () => found } as unknown as Response;
    }
    if (url.includes('/agent/runs')) {
      if (routes.list === 'fail') {
        return { ok: false, status: 500, json: async () => ({ message: '服务挂了' }) } as unknown as Response;
      }
      return { ok: true, status: 200, json: async () => routes.list ?? [] } as unknown as Response;
    }
    return { ok: true, status: 200, json: async () => ({}) } as unknown as Response;
  });
  vi.stubGlobal('fetch', mock);
  return calls;
}

function mountApp(): VueWrapper {
  return mount(App, { global: { plugins: [createPinia()] } });
}

function buttonByText(wrapper: VueWrapper, text: string) {
  const found = wrapper.findAll('button').find(item => item.text().includes(text));
  if (!found) throw new Error(`找不到按钮：${text}`);
  return found;
}

const NOTICE = '[aria-label="上次的运行结果"]';

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('上次的运行结果', () => {
  it('有"生成了但没保存"的运行 → 提示一句，不弹窗', async () => {
    stubFetch({ list: [summary()] });
    const wrapper = mountApp();

    await useMindMapStore().probeResumableRun();
    await flushPromises();

    const notice = wrapper.get(NOTICE);
    expect(notice.text()).toContain('还没保存');
    expect(notice.text()).toContain('机器学习');
    expect(buttonByText(wrapper, '载入')).toBeTruthy();
    // 提示是页面上的一行字，不是遮挡界面的对话框。
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false);
  });

  it('被中断但已经产出结果的那次同样提醒，措辞与"完成了"分开', async () => {
    stubFetch({ list: [summary({ run_id: 'run-cancel', status: 'cancelled' })] });
    const wrapper = mountApp();

    await useMindMapStore().probeResumableRun();
    await flushPromises();

    const text = wrapper.get(NOTICE).text();
    expect(text).toContain('被中断');
    expect(text).not.toContain('生成完的导图还没保存');
  });

  it('没有待处理的结果时不出现提示', async () => {
    stubFetch({ list: [] });
    const wrapper = mountApp();

    await useMindMapStore().probeResumableRun();
    await flushPromises();

    expect(wrapper.find(NOTICE).exists()).toBe(false);
  });

  it('探测失败时一声不响：用户此刻什么也没要求', async () => {
    stubFetch({ list: 'fail' });
    const wrapper = mountApp();

    await expect(useMindMapStore().probeResumableRun()).resolves.toBeUndefined();
    await flushPromises();

    expect(wrapper.find(NOTICE).exists()).toBe(false);
    // 「一声不响」= 界面上连一条状态行都没有（初始那句让位给了生成框里的提示，
    // 见 `intent-input.spec.ts`），失败原因更不该出现在任何地方。
    expect(wrapper.find('[role="status"]').exists()).toBe(false);
    expect(wrapper.text()).not.toContain('服务挂了');
  });

  it('点「载入」把上次的结果直接装进画布，可撤销', async () => {
    const calls = stubFetch({ list: [summary()] });
    const wrapper = mountApp();
    const store = useMindMapStore();

    await store.probeResumableRun();
    await flushPromises();
    await buttonByText(wrapper, '载入').trigger('click');
    await flushPromises();

    expect(calls.some(call => call.url.endsWith('/api/agent/runs/run-abc'))).toBe(true);
    // 没有"确认"这一步了：点载入就是明确要它，再让他点一次「应用」是把同一个
    // 意图问第二遍。但它仍然走 `commitGenerated`，所以撤销栈一步不少。
    expect(store.content?.title).toBe('上次那张图');
    expect(store.dirty).toBe(true);
    expect(store.canUndo).toBe(true);
    // 提示消失：结果已经在画布上，留着只会问两遍。
    expect(wrapper.find(NOTICE).exists()).toBe(false);
  });

  it('载入之后保存，才上报 applied', async () => {
    const calls = stubFetch({ list: [summary()] });
    const wrapper = mountApp();
    const store = useMindMapStore();

    await store.probeResumableRun();
    await flushPromises();
    await buttonByText(wrapper, '载入').trigger('click');
    await flushPromises();

    // 载入本身不写"已保存"：它只是把树放进画布，用户还没存。
    expect(calls.some(call => call.url.endsWith('/applied'))).toBe(false);

    await store.save();
    await flushPromises();

    expect(store.content?.title).toBe('上次那张图');
    // `applied_at` 现在的语义就是"这份结果被存下来了"，它是下次进页面要不要
    // 提醒的唯一判据——所以必须由保存成功这条路打点。
    expect(calls.some(call => call.url.endsWith('/applied') && call.method === 'POST')).toBe(true);
  });

  it('点「不用了」只在本会话静音，不写库', async () => {
    const calls = stubFetch({ list: [summary()] });
    const wrapper = mountApp();
    const store = useMindMapStore();

    await store.probeResumableRun();
    await flushPromises();
    await wrapper.get(NOTICE).findAll('button')[1].trigger('click');
    await flushPromises();

    expect(wrapper.find(NOTICE).exists()).toBe(false);
    expect(store.content).toBeNull();
    // `dismissed_at` 那一列已经删掉：没有"丢弃候选"这个动作，也就没有要上报的
    // 东西。界面上不再问，是一件纯本地的事。
    expect(calls.some(call => call.url.includes('/dismissed'))).toBe(false);

    // 再探一次也不再冒出来：这次会话里已经明确说过"不要"。
    await store.probeResumableRun();
    await flushPromises();
    expect(wrapper.find(NOTICE).exists()).toBe(false);
  });

  it('详情里没有结果时给一句实话，而不是塞一棵空树', async () => {
    stubFetch({ list: [summary()], detail: detail({ candidate: null }) });
    const wrapper = mountApp();
    const store = useMindMapStore();

    await store.probeResumableRun();
    await flushPromises();
    await buttonByText(wrapper, '载入').trigger('click');
    await flushPromises();

    expect(store.content).toBeNull();
    expect(store.generatedSummary).toBeNull();
    expect(wrapper.get('[role="status"]').text()).toContain('没有留下可用的内容');
  });

  it('待处理提示对半棵树不写"生成完的"', async () => {
    stubFetch({ list: [summary({ partial: true })] });
    const wrapper = mountApp();

    await useMindMapStore().probeResumableRun();
    await flushPromises();

    const text = wrapper.get(NOTICE).text();
    expect(text).toContain('只完成了一部分');
    expect(text).not.toContain('生成完的导图还没保存');
  });

  it('载入一份半棵树：状态行与告警都说清它不是完整交付', async () => {
    stubFetch({
      list: [summary({ partial: true })],
      detail: detail({ partial: true, partial_reason: '模型调用次数已用完' }),
    });
    const wrapper = mountApp();
    const store = useMindMapStore();

    await store.probeResumableRun();
    await flushPromises();
    await buttonByText(wrapper, '载入').trigger('click');
    await flushPromises();

    // 内容照常进画布（半棵树也是交付），但"它是半棵"这件事不能丢：
    // 实时流那条路在 done 帧上记这条告警，载入这条路必须自己补上同一笔。
    expect(store.content?.title).toBe('上次那张图');
    expect(store.agentWarnings.some(item => item.kind === 'partial')).toBe(true);
    expect(store.agentWarnings[0].message).toBe('模型调用次数已用完');
    expect(wrapper.get('[role="status"]').text()).toContain('部分分支未完成');
  });

  it('完整跑完的那次载入之后不留半棵树告警', async () => {
    stubFetch({ list: [summary()], detail: detail({ partial: null }) });
    const wrapper = mountApp();
    const store = useMindMapStore();

    await store.probeResumableRun();
    await flushPromises();
    await buttonByText(wrapper, '载入').trigger('click');
    await flushPromises();

    expect(store.agentWarnings.some(item => item.kind === 'partial')).toBe(false);
    expect(wrapper.get('[role="status"]').text()).not.toContain('部分分支未完成');
  });
});

describe('运行身份', () => {
  it('每次生成都带一个自己的 run_id', async () => {
    const calls: Array<{ url: string; body: unknown }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), body: init?.body ? JSON.parse(String(init.body)) : null });
      const encoder = new TextEncoder();
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(encoder.encode(
            `data: ${JSON.stringify({ type: 'done', seq: 1, content: RESULT, mode: 'model' })}\n\n`));
          controller.enqueue(encoder.encode('data: [DONE]\n\n'));
          controller.close();
        },
      });
      return { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
    }));

    const store = useMindMapStore();
    await store.runGeneration('机器学习', 'create');
    // 第二次生成：画布上已经有上一轮的结果，这时是新的一轮（新 run_id）
    await store.runGeneration('机器学习', 'create');

    // 只看开流那两条：别的一概不管（它们没有请求体）。
    const streams = calls.filter(call => call.url.endsWith('/agent/runs/stream'));
    const [first, second] = streams.map(call => (call.body as { run_id?: string }).run_id);
    expect(first).toMatch(/^[A-Za-z0-9_.-]+$/);
    // 两次点击就是两次运行：幂等只在"带同一个 run_id"时生效，前端每次都给新的。
    expect(second).toBeTruthy();
    expect(second).not.toBe(first);
  });
});
