import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { MindMapContent, Pref, PrefSuggestion } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';
import { DIAGRAM_PREF_KEY, THINKING_PREF_KEY } from '../src/stores/prefs';
import { diagramPref, isPrefsRequest, prefsReply, thinkingPref } from './helpers/prefs-stub';

/**
 * 偏好：预填、面板、建议，以及"这次到底用了哪条偏好"这条证据链。
 *
 * 这一组钉住的是这次改动里最容易被做丢的两条性质：
 *
 * 1. **预填不是偷偷填**。用户没显式选时，请求体里带的必须是**界面上看得见**的那个
 *    值；而他显式选过之后，偏好不许再覆盖它。前者决定 `prefs_applied` 记不记，
 *    后者决定偏好会不会把人刚按下的选择顶回去。
 * 2. **面板是活的**。列出、改、删、来源标注、"上次生效 / 尚未生效"——少任何一样，
 *    用户就会以为系统没记住。最后那一项是死表探测器：一条从来没被任何运行报告过的
 *    偏好会永远显示「尚未生效」。
 */

vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

const INTENT = 'textarea#intent';
const SUBMIT = '[data-test="intent-box"] button';

const content = (label = '根'): MindMapContent => ({
  title: '示例',
  root: { id: 'root', label, children: [{ id: 'a', label: '甲', children: [] }] },
});

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

const doneFrame = (payload: MindMapContent) => ({ type: 'done', seq: 4, content: payload });

interface Recorded {
  url: string;
  method: string;
  body: unknown;
}

/**
 * 一个能同时应答偏好接口与生成接口的桩。
 *
 * 偏好那三个动词各自记录，因为它们**就是**被测对象：`PUT` 的 key / value / source，
 * `DELETE` 的键，以及"点不用时到底有没有发出 PUT"。生成请求单独记，方便断言请求体。
 */
function stubBackend(options: {
  prefs?: Pref[];
  suggestions?: PrefSuggestion[];
} = {}) {
  const prefs = [...(options.prefs ?? [])];
  const suggestions = options.suggestions ?? [];
  const writes: Recorded[] = [];
  const deletes: string[] = [];
  const runs: Array<{ url: string; body: Record<string, unknown> }> = [];

  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? 'GET';

    if (url.includes('/agent/runs/stream')) {
      runs.push({ url, body: JSON.parse(String(init?.body)) as Record<string, unknown> });
      return agentReply([doneFrame(content())]);
    }
    if (url.includes('/prefs/suggestions')) return prefsReply(url, [], suggestions);
    if (isPrefsRequest(url)) {
      if (method === 'PUT') {
        const body = JSON.parse(String(init?.body)) as { value: string; source: string };
        writes.push({ url, method, body });
        const pref: Pref = {
          ...diagramPref(body.value),
          key: decodeURIComponent(url.split('/prefs/')[1]),
          source: body.source as Pref['source'],
        };
        return {
          ok: true, status: 200, json: async () => ({ pref, changed: true }),
        } as unknown as Response;
      }
      if (method === 'DELETE') {
        deletes.push(decodeURIComponent(url.split('/prefs/')[1]));
        return { ok: true, status: 204, json: async () => ({}) } as unknown as Response;
      }
      return prefsReply(url, prefs);
    }
    return agentReply([]);
  }));
  return { writes, deletes, runs, prefs };
}

function mountApp(): VueWrapper {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(App, { global: { plugins: [pinia] } });
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------- 预填


describe('预填：偏好决定初始值，用户的选择决定最终值', () => {
  it('没显式选时，下拉带上偏好值并标明来源', async () => {
    stubBackend({ prefs: [diagramPref('flowchart'), thinkingPref('feynman')] });
    const wrapper = mountApp();
    await vi.waitFor(() =>
      expect((wrapper.get('#diagram-type').element as HTMLSelectElement).value).toBe('flowchart'));

    expect((wrapper.get('#thinking-model').element as HTMLSelectElement).value).toBe('feynman');
    // 用户必须看得出这个值不是他此刻选的
    expect(wrapper.get('[data-test="intent-box"]').text()).toContain('来自偏好：流程图');
  });

  it('请求体里带的是**界面上显示的那个值**，并标进 prefs_applied', async () => {
    const backend = stubBackend({ prefs: [diagramPref('fishbone')] });
    const wrapper = mountApp();
    await vi.waitFor(() =>
      expect((wrapper.get('#diagram-type').element as HTMLSelectElement).value).toBe('fishbone'));

    await wrapper.get(INTENT).setValue('做一张机器学习图');
    await wrapper.get(SUBMIT).trigger('click');
    await vi.waitFor(() => expect(backend.runs).toHaveLength(1));

    const body = backend.runs[0].body;
    // 带的是看见的值：界面上是 fishbone，请求里就是 fishbone
    expect(body.diagram_type).toBe('fishbone');
    // 并且**说明它是从偏好来的**——这一列是面板上"上次生效"的唯一数据源
    expect(body.prefs_applied).toEqual([`${DIAGRAM_PREF_KEY}=fishbone`]);
  });

  it('用户显式选了之后，偏好不再覆盖，也不再记进 prefs_applied', async () => {
    const backend = stubBackend({ prefs: [diagramPref('fishbone')] });
    const wrapper = mountApp();
    await vi.waitFor(() =>
      expect((wrapper.get('#diagram-type').element as HTMLSelectElement).value).toBe('fishbone'));

    await wrapper.get('#diagram-type').setValue('flowchart');
    // 来源标注消失：这个值是他自己选的
    expect(wrapper.get('[data-test="intent-box"]').text()).not.toContain('来自偏好');

    await wrapper.get(INTENT).setValue('做一张图');
    await wrapper.get(SUBMIT).trigger('click');
    await vi.waitFor(() => expect(backend.runs).toHaveLength(1));

    expect(backend.runs[0].body.diagram_type).toBe('flowchart');
    // 用户的选择不该被说成系统的预填
    expect('prefs_applied' in backend.runs[0].body).toBe(false);
  });

  it('没有偏好时一切照旧：不预填、不带 diagram_type、不带 prefs_applied', async () => {
    const backend = stubBackend();
    const wrapper = mountApp();
    await vi.waitFor(() => expect(wrapper.find('#diagram-type').exists()).toBe(true));

    await wrapper.get(INTENT).setValue('做一张图');
    await wrapper.get(SUBMIT).trigger('click');
    await vi.waitFor(() => expect(backend.runs).toHaveLength(1));

    const body = backend.runs[0].body;
    expect('diagram_type' in body).toBe(false);
    expect('prefs_applied' in body).toBe(false);
  });
});

// ---------------------------------------------------------------- 「以后都这样」


describe('「以后都这样」是主要写入源', () => {
  it('勾上之后先写偏好、再生成，于是这一次就记进了 prefs_applied', async () => {
    const backend = stubBackend();
    const wrapper = mountApp();
    await vi.waitFor(() => expect(wrapper.find('#diagram-type').exists()).toBe(true));

    await wrapper.get('#diagram-type').setValue('flowchart');
    await wrapper.get('[data-test="sticky-diagram"]').setValue(true);
    await wrapper.get(INTENT).setValue('做一张图');
    await wrapper.get(SUBMIT).trigger('click');

    await vi.waitFor(() => expect(backend.runs).toHaveLength(1));
    expect(backend.writes).toHaveLength(1);
    expect(backend.writes[0].body).toEqual({ value: 'flowchart', source: 'user_explicit' });
    // 关键的一条：写完偏好之后选项回到"跟随偏好"，所以这一次的 prefs_applied 非空。
    // 少了这一步，用户刚勾完就会看到面板上写着"尚未生效"。
    expect(backend.runs[0].body.prefs_applied).toEqual([`${DIAGRAM_PREF_KEY}=flowchart`]);
  });

  it('不勾就不写：一次生成不该在库里留下任何偏好', async () => {
    const backend = stubBackend();
    const wrapper = mountApp();
    await vi.waitFor(() => expect(wrapper.find('#diagram-type').exists()).toBe(true));

    await wrapper.get('#diagram-type').setValue('flowchart');
    await wrapper.get(INTENT).setValue('做一张图');
    await wrapper.get(SUBMIT).trigger('click');

    await vi.waitFor(() => expect(backend.runs).toHaveLength(1));
    expect(backend.writes).toEqual([]);
  });

  it('edit 场景没有这个勾选：那条路不生成骨架，图型没有意义', async () => {
    stubBackend();
    const wrapper = mountApp();
    useMindMapStore().preview(content('原图'), 'right');
    await wrapper.vm.$nextTick();

    expect(wrapper.find('[data-test="sticky-diagram"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="sticky-thinking"]').exists()).toBe(false);
  });
});

// ---------------------------------------------------------------- 面板


describe('偏好面板', () => {
  it('列出键、值、来源，以及"上次生效 / 尚未生效"', async () => {
    stubBackend({
      prefs: [
        diagramPref('flowchart', { last_applied_at: '2026-11-13T08:00:00.000Z' }),
        thinkingPref('feynman', { source: 'suggestion_accepted' }),
      ],
    });
    const wrapper = mountApp();
    await vi.waitFor(() => expect(wrapper.findAll('[data-test="pref-item"]')).toHaveLength(2));

    const text = wrapper.get('[data-test="prefs-panel"]').text();
    expect(text).toContain('默认图型');
    expect(text).toContain('flowchart');
    expect(text).toContain('你设置的');
    // 死表探测器：生效过的显示日期
    expect(wrapper.get(`[data-test="pref-applied-${DIAGRAM_PREF_KEY}"]`).text())
      .toContain('2026-11-13');
    // 从没被任何运行报告过的那一条，如实写"尚未生效"
    expect(wrapper.get(`[data-test="pref-never-${THINKING_PREF_KEY}"]`).text())
      .toContain('尚未生效');
    // 来源要能分辨：这条是用户同意过的系统建议
    expect(text).toContain('系统建议（你已同意）');
  });

  it('能改：点「改」写新值 → PUT 发出去', async () => {
    const backend = stubBackend({ prefs: [diagramPref('flowchart')] });
    const wrapper = mountApp();
    await vi.waitFor(() =>
      expect(wrapper.find(`[data-test="pref-edit-btn-${DIAGRAM_PREF_KEY}"]`).exists()).toBe(true));

    await wrapper.get(`[data-test="pref-edit-btn-${DIAGRAM_PREF_KEY}"]`).trigger('click');
    await wrapper.get(`[data-test="pref-edit-${DIAGRAM_PREF_KEY}"]`).setValue('fishbone');
    await wrapper.get(`[data-test="pref-save-${DIAGRAM_PREF_KEY}"]`).trigger('click');

    await vi.waitFor(() => expect(backend.writes).toHaveLength(1));
    expect(backend.writes[0].url).toContain(DIAGRAM_PREF_KEY);
    expect(backend.writes[0].body).toEqual({ value: 'fishbone', source: 'user_explicit' });
  });

  it('能删：点「删除」→ DELETE 发出去，条目从列表里消失', async () => {
    const backend = stubBackend({ prefs: [diagramPref('flowchart')] });
    const wrapper = mountApp();
    await vi.waitFor(() => expect(wrapper.findAll('[data-test="pref-item"]')).toHaveLength(1));

    await wrapper.get(`[data-test="pref-del-${DIAGRAM_PREF_KEY}"]`).trigger('click');

    await vi.waitFor(() => expect(backend.deletes).toEqual([DIAGRAM_PREF_KEY]));
    await vi.waitFor(() => expect(wrapper.findAll('[data-test="pref-item"]')).toHaveLength(0));
  });

  it('空状态说的是"怎么加一条"，而不是一片空白', async () => {
    stubBackend();
    const wrapper = mountApp();
    await vi.waitFor(() =>
      expect(wrapper.get('[data-test="prefs-panel"]').text()).toContain('还没有任何偏好'));
    expect(wrapper.get('[data-test="prefs-panel"]').text()).toContain('以后都这样');
  });
});

// ---------------------------------------------------------------- 建议


describe('建议：只产出候选，用户点了才写', () => {
  const hint: PrefSuggestion = {
    key: DIAGRAM_PREF_KEY,
    value: 'flowchart',
    label: '流程图',
    count: 3,
    message: '你最近 3 次都选了流程图，设为默认？',
  };

  it('显示建议，并说明它来自最近几次选择', async () => {
    stubBackend({ suggestions: [hint] });
    const wrapper = mountApp();
    await vi.waitFor(() => expect(wrapper.find('[data-test="pref-suggestion"]').exists()).toBe(true));
    expect(wrapper.get('[data-test="pref-suggestion"]').text()).toContain('你最近 3 次都选了流程图');
  });

  it('用户拒绝：什么都不写，只是不再显示', async () => {
    const backend = stubBackend({ suggestions: [hint] });
    const wrapper = mountApp();
    await vi.waitFor(() => expect(wrapper.find('[data-test="pref-suggestion"]').exists()).toBe(true));

    const decline = wrapper.findAll('[data-test="pref-suggestion"] button')[1];
    await decline.trigger('click');

    expect(wrapper.find('[data-test="pref-suggestion"]').exists()).toBe(false);
    // 拒绝不落库：没有 PUT，也就没有一行"已被拒绝的建议"
    expect(backend.writes).toEqual([]);
  });

  it('用户同意：写入时标成 suggestion_accepted', async () => {
    const backend = stubBackend({ suggestions: [hint] });
    const wrapper = mountApp();
    await vi.waitFor(() => expect(wrapper.find('[data-test="pref-suggestion"]').exists()).toBe(true));

    await wrapper.findAll('[data-test="pref-suggestion"] button')[0].trigger('click');

    await vi.waitFor(() => expect(backend.writes).toHaveLength(1));
    expect(backend.writes[0].body).toEqual({
      value: 'flowchart', source: 'suggestion_accepted',
    });
  });
});
