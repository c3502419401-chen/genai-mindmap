import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import AgentPanel from '../src/components/AgentPanel.vue';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * 失败卡片在真组件里的样子。
 *
 * `mindmap/failures.spec.ts` 已经逐条钉了判据（哪个 code 该给什么下一步），这一层
 * 只钉三件判据管不到的事：卡片真的渲染出来了、禁用的按钮**旁边有字说明为什么**、
 * 点「重试」真的把上一次的请求原样再发一遍。
 */

const encoder = new TextEncoder();

function sse(frames: unknown[]): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`));
      controller.enqueue(encoder.encode('data: [DONE]\n\n'));
      controller.close();
    },
  });
  return { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
}

/** 记录每次 fetch 的请求体：重试要拿它跟第一次逐字节比。 */
function stubFetch(reply: () => Response) {
  const bodies: Array<Record<string, unknown>> = [];
  const mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    bodies.push(init?.body ? JSON.parse(String(init.body)) : {});
    return reply();
  });
  vi.stubGlobal('fetch', mock);
  return bodies;
}

const errorReply = (code: string, message: string, limit?: string) => () => sse([
  { type: 'error', seq: 1, code, message, ...(limit ? { limit } : {}) },
]);

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('失败卡片', () => {
  it('没配置模型时不给一个点得动的「重试」，并说清该去配什么', async () => {
    stubFetch(errorReply('model_not_configured', '请在后端环境变量中配置模型地址、密钥和模型名称'));
    const store = useMindMapStore();
    await store.runGeneration('做一张导图', 'create');

    const wrapper = mount(AgentPanel);
    const text = wrapper.text();

    expect(text).toContain('模型还没配置好');
    // 首选路径是页面右上角的面板（这个功能就是为了不用登服务器），环境变量是兜底。
    expect(text).toContain('模型设置');
    // 用户能做的动作必须写出来：光说"配置有问题"，他还是不知道该改哪。
    expect(text).toContain('MINDMAP_LLM_API_KEY');

    const retry = wrapper.get('[data-test="failure-retry"]');
    expect(retry.attributes('disabled')).toBeDefined();
    // 只置灰等于沉默：用户看着点不动的按钮，只能猜是自己点错了。
    expect(wrapper.get('[data-test="failure-why-retry"]').text()).toContain('重试');
  });

  it('后端原话折在详情里，不当成用户读到的第一句话', async () => {
    stubFetch(errorReply('generation_failed', '模型服务返回 HTTP 503'));
    const store = useMindMapStore();
    await store.runGeneration('做一张导图', 'create');

    const wrapper = mount(AgentPanel);
    // 默认收起：卡片第一屏是"发生了什么、接下来怎么办"，不是栈信息。
    const details = wrapper.get('details');
    expect(details.attributes('open')).toBeUndefined();
    expect(details.text()).toContain('模型服务返回 HTTP 503');
    // 原话里带上分类码，排障时才能与后端日志对上。
    expect(details.text()).toContain('code=generation_failed');
  });

  it('可重试的失败给一个点得动的按钮，点了原样再发一遍（换新 run 身份）', async () => {
    const bodies = stubFetch(errorReply('generation_failed', '模型服务返回 HTTP 503'));
    const store = useMindMapStore();
    await store.runGeneration('做一张导图', 'create');

    expect(bodies).toHaveLength(1);

    const wrapper = mount(AgentPanel);
    const retry = wrapper.get('[data-test="failure-retry"]');
    expect(retry.attributes('disabled')).toBeUndefined();
    await retry.trigger('click');
    await vi.waitFor(() => expect(bodies).toHaveLength(2));

    const [first, second] = bodies;
    expect(second.goal).toBe(first.goal);
    expect(second.intent).toBe(first.intent);
    expect(second.thinking_model).toBe(first.thinking_model);
    // **新身份**：沿用旧 run_id 会被后端按幂等判成"同一次已经 done"，
    // 直接把上一份失败重放回来——用户点了「重试」却看到同一句错误。
    expect(second.run_id).toBeTruthy();
    expect(second.run_id).not.toBe(first.run_id);
  });

  it('正在跑的时候重试按钮禁用，并说明在等什么', async () => {
    const store = useMindMapStore();
    // 直接摆一个失败态，再把"正在跑"打开：不必真发两轮请求。
    store.agentError = '模型调用次数已用完';
    store.agentRunning = true;
    store.busy = true;

    const wrapper = mount(AgentPanel);
    expect(wrapper.get('[data-test="failure-retry"]').attributes('disabled')).toBeDefined();
    expect(wrapper.get('[data-test="failure-why-retry"]').text()).toContain('正在跑');
  });
});
