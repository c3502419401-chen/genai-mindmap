import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia } from 'pinia';
import { afterEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { DiagramContent, MindMapContent, PrefSuggestion } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';
import { usePrefsStore } from '../src/stores/prefs';
import { isPrefsRequest, prefsReply } from './helpers/prefs-stub';

/**
 * 侧栏收窄之后的**外壳结构**：右栏是覆盖式抽屉、偏好是顶栏浮层、进度只剩一行。
 *
 * 为什么这些用例要在 `App` 层面钉：布局的真实像素只有真浏览器量得出来
 * （`.visioncheck/measure.py`：1440px 视口下画布从 800 → 1120，抽屉 360 覆盖且宽度不变），
 * 而 jsdom 能钉住的是**结构**——谁在流里、谁盖在谁身上、哪一块默认不渲染。
 * 结构错了，真浏览器上的数字一定跟着错。
 */
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

const TREE: MindMapContent = {
  title: '预算管理',
  root: {
    id: 'r',
    label: '预算管理',
    children: [{ id: 'a', label: '申请与复核', children: [] }],
  },
  source_usage: [{ source_id: 's1', kind: 'document', filename: '预算办法.docx', char_count: 120 }],
};

// 流程图的形状与树互斥（`stages` 对 `root`），所以它归 `DiagramContent` 而不是
// `MindMapContent`——这里必须是那个联合类型，否则 type-check 会指着 `stages` 说多余。
const FLOW: DiagramContent = {
  title: '报销流程',
  diagram_type: 'flowchart',
  stages: [
    {
      id: 's1',
      root: {
        id: 'n1', label: '提交申请', role: 'start',
        children: [{ id: 'n2', label: '材料齐全吗', role: 'decision', children: [] }],
      },
    },
    { id: 's2', root: { id: 'n3', label: '付款', role: 'end', children: [] } },
  ],
  edges: [{ id: 'e1', source: 's1', target: 's2', kind: 'flow' }],
  source_usage: [{ source_id: 's1', kind: 'document', filename: '报销办法.docx', char_count: 300 }],
};

const wrappers: VueWrapper[] = [];

/** 一段 SSE：`done` 帧之后流就结束，与后端真实形状一致。 */
function streamReply(frames: unknown[]): Response {
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

function stubFetch(options: { suggestions?: PrefSuggestion[]; stream?: unknown[] } = {}) {
  const mock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (isPrefsRequest(url)) return prefsReply(url, [], options.suggestions ?? []);
    if (url.endsWith('/health')) {
      return { ok: true, status: 200, json: async () => ({ version: 'test' }) } as unknown as Response;
    }
    if (url.endsWith('/agent/runs/stream') && options.stream) return streamReply(options.stream);
    return { ok: true, status: 200, json: async () => ({}) } as unknown as Response;
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

function mountApp(): VueWrapper {
  const wrapper = mount(App, { global: { plugins: [createPinia()] } });
  wrappers.push(wrapper);
  return wrapper;
}

const DRAWER = '[data-test="side-panel"]';
const POPOVER = '[data-test="prefs-popover"]';
const STATUS = '[data-test="run-status"]';

function drawerStyle(wrapper: VueWrapper): string {
  return wrapper.get(DRAWER).attributes('style') ?? '';
}

function drawerOpen(wrapper: VueWrapper): boolean {
  return !drawerStyle(wrapper).includes('display: none');
}

afterEach(() => {
  // 组件必须卸载：状态行那条跑秒计时器是 `setInterval`，不卸就是泄漏。
  wrappers.splice(0).forEach(wrapper => wrapper.unmount());
  vi.unstubAllGlobals();
});

describe('右栏改成覆盖式抽屉', () => {
  it('主区只剩一个 flex 兄弟（画布）：左栏撤掉之后它拿到全宽', () => {
    stubFetch();
    const wrapper = mountApp();
    // 左栏那个 320px 常驻区块没了，材料改走对话框里的缩略图 + `+` 面板；
    // `main` 里因此只剩画布一个 section（右抽屉与左抽屉都是覆盖层）。
    const sections = wrapper.get('main').element.querySelectorAll(':scope > section');
    expect(sections).toHaveLength(1);
    expect(sections[0].querySelector('.vue-flow, [data-test="canvas"]')).toBeTruthy();
  });

  it('抽屉默认关着，而且是绝对定位的覆盖层（不是流里的第三块）', () => {
    stubFetch();
    const wrapper = mountApp();
    const aside = wrapper.get(DRAWER);
    expect(drawerOpen(wrapper)).toBe(false);
    expect(aside.classes()).toContain('absolute');
    expect(aside.classes()).toContain('w-[360px]');
    // 非模态：它是 complementary，不是 dialog。模态会挡住画布交互，
    // 也会和知识库、原文两个真模态抽屉抢 Esc。
    expect(aside.attributes('role')).toBeUndefined();
    expect(aside.attributes('aria-modal')).toBeUndefined();
  });

  it('顶栏那个开关能打开、也能收起', async () => {
    stubFetch();
    const wrapper = mountApp();
    const toggle = wrapper.get('[data-test="side-toggle"]');

    await toggle.trigger('click');
    expect(drawerOpen(wrapper)).toBe(true);
    expect(toggle.attributes('aria-expanded')).toBe('true');

    await toggle.trigger('click');
    expect(drawerOpen(wrapper)).toBe(false);
    expect(toggle.attributes('aria-expanded')).toBe('false');
  });

  it('抽屉里的「关闭」与 Esc 都能收起来', async () => {
    stubFetch();
    const wrapper = mountApp();

    await wrapper.get('[data-test="side-toggle"]').trigger('click');
    await wrapper.get('[data-test="side-close"]').trigger('click');
    expect(drawerOpen(wrapper)).toBe(false);

    await wrapper.get('[data-test="side-toggle"]').trigger('click');
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await wrapper.vm.$nextTick();
    expect(drawerOpen(wrapper)).toBe(false);
  });

  it('选中节点不会自动弹抽屉（那比右栏更挡），但入口按钮会亮', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.select('a');
    await wrapper.vm.$nextTick();

    expect(drawerOpen(wrapper)).toBe(false);
    // `side-toggle` 现在在 `☰` 抽屉里（顶栏瘦成三件东西），`aria-expanded` 仍然是
    // "抽屉开着没有"，而"有东西可看"由 `☰` 抽屉打开后那枚按钮的实心样式表达。
    const toggle = wrapper.get('[data-test="side-toggle"]');
    expect(toggle.attributes('aria-expanded')).toBe('false');
    expect(toggle.text()).toContain('节点详情');
  });

  it('节点详情、附加关系、本图来源都在抽屉里，一块没少', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(FLOW, 'right');
    store.select('n2');
    await wrapper.vm.$nextTick();

    const aside = wrapper.get(DRAWER);
    expect(aside.find('[aria-label="节点详情"]').exists()).toBe(true);
    // 流程图唯一的补边入口：它在抽屉里，不能因为收起来就丢掉
    expect(aside.find('[aria-label="附加关系"]').exists()).toBe(true);
    expect(aside.text()).toContain('本图来源（1）');
    expect(aside.find('h2').text()).toBe('材料齐全吗');
  });

  it('「明细」按钮把抽屉打开并直接展开本次生成明细', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.generatedSummary = { nodes: 6, changes: null };
    await wrapper.vm.$nextTick();

    await wrapper.get('[data-test="run-status-detail"]').trigger('click');
    await wrapper.vm.$nextTick();

    expect(drawerOpen(wrapper)).toBe(true);
    const detail = wrapper.get('[data-test="agent-detail"]');
    expect((detail.element as HTMLDetailsElement).open).toBe(true);
  });
});

describe('偏好进顶栏齿轮', () => {
  it('浮层默认关着，点齿轮才出来；面板本身一直挂在 DOM 里', async () => {
    stubFetch();
    const wrapper = mountApp();
    const popover = wrapper.get(POPOVER);
    expect(popover.attributes('style') ?? '').toContain('display: none');
    // 收起来不等于没渲染：刷新按钮与条目仍然在，只是看不见
    expect(popover.find('[data-test="prefs-panel"]').exists()).toBe(true);

    await wrapper.get('[data-test="prefs-toggle"]').trigger('click');
    expect(wrapper.get(POPOVER).attributes('style') ?? '').not.toContain('display: none');
  });

  it('有待确认的建议时，齿轮上挂角标；没有就不挂', async () => {
    stubFetch({ suggestions: [{ key: 'diagram_type.default', value: 'flowchart', label: '流程图', count: 3, message: '你最近 3 次都选了流程图' }] });
    const wrapper = mountApp();
    await flushPromises();

    expect(usePrefsStore().suggestions).toHaveLength(1);
    expect(wrapper.get('[data-test="prefs-badge"]').text()).toBe('1');
    // 角标之外还有 title 说明：一个点不解释清楚，用户只会以为界面花了
    expect(wrapper.get('[data-test="prefs-toggle"]').attributes('title')).toContain('建议');
  });

  it('没有建议、没有失败时齿轮上没有角标', async () => {
    stubFetch();
    const wrapper = mountApp();
    await flushPromises();
    expect(wrapper.find('[data-test="prefs-badge"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="prefs-badge-error"]').exists()).toBe(false);
  });

  it('偏好写失败时即使面板关着也会弹一条错误提示', async () => {
    stubFetch();
    const wrapper = mountApp();
    const prefs = usePrefsStore();
    prefs.error = '写偏好失败：连接被拒绝';
    await wrapper.vm.$nextTick();

    const alert = wrapper.find('[role="alert"]');
    expect(alert.exists()).toBe(true);
    expect(alert.text()).toContain('连接被拒绝');
  });
});

describe('画布底部那一行', () => {
  it('生成中：一行里有动词、阶段文案与秒数位，且不再另起一行重复', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.busy = true;
    store.stageText = '正在定框架';
    store.status = '正在与模型协作…';
    await wrapper.vm.$nextTick();

    const row = wrapper.get(STATUS);
    expect(row.text()).toContain('正在生成');
    expect(row.text()).toContain('正在定框架');
    // 生成框里那行 `store.status` 此刻要藏起来：两行说同一件事只会互相削弱
    expect(wrapper.find('[role="status"]').exists()).toBe(false);
  });

  it('静默 10 秒后阶段文案被替换，不是两句并排', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.busy = true;
    store.stageText = '正在定框架';
    store.waitedMs = 12;
    await wrapper.vm.$nextTick();

    const text = wrapper.get(STATUS).text();
    expect(text).toContain('模型正在思考，已等 12 秒');
    expect(text).not.toContain('正在定框架');
  });

  it('完成后变成结果摘要，并留着「回到生成之前」', async () => {
    // 走一次真生成：`canUndoGenerate` 只有在"画布还是这次生成的结果"时才为真，
    // 直接改 `generatedSummary` 造不出那个条件。
    stubFetch({
      stream: [{ type: 'done', seq: 1, content: TREE, mode: 'model' }],
    });
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    await store.runGeneration('把这一支再拆细一点', 'edit');
    await flushPromises();
    await wrapper.vm.$nextTick();

    const text = wrapper.get(STATUS).text();
    expect(text).toContain('已落到画布');
    expect(text).toContain('还没保存');
    expect(wrapper.find('[data-test="run-status-undo"]').exists()).toBe(true);
  });

  it('失败要醒目：文案里有原因，配色是玫红', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.agentError = '模型服务暂时不可用';
    await wrapper.vm.$nextTick();

    const row = wrapper.get(STATUS);
    expect(row.text()).toContain('生成失败：模型服务暂时不可用');
    expect(row.classes().join(' ')).toContain('rose');
  });

  it('部分完成与失败分开说：树已经落画布，只是某几支空着', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.agentWarnings = [{ kind: 'partial', code: 'PARTIAL', message: '2 个分支没能生成' }];
    await wrapper.vm.$nextTick();

    const row = wrapper.get(STATUS);
    expect(row.text()).toContain('部分完成：2 个分支没能生成');
    expect(row.classes().join(' ')).toContain('amber');
  });

  it('冲突在同一行里就能裁决：两个按钮点下去真的改了 store', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.conflictDraft = {
      next: TREE,
      meta: { runId: 'r1', intent: 'create', mode: 'model', base: null } as never,
    };
    await wrapper.vm.$nextTick();

    expect(wrapper.get(STATUS).text()).toContain('两棵树都还在');
    const keep = wrapper.get('[data-test="run-status-keep"]');
    expect(keep.text()).toBe('保留我的');

    await keep.trigger('click');
    await flushPromises();
    // "保留我的"= 丢掉这次生成，两棵树的抉择结束
    expect(store.conflictDraft).toBeNull();
  });

  it('没有任何状态时这一行不渲染（不留一条空壳）', async () => {
    stubFetch();
    const wrapper = mountApp();
    await flushPromises();
    expect(wrapper.find(STATUS).exists()).toBe(false);
  });

  it('「上次的生成结果」现在挂在状态行上，不是右栏', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.resumableRun = {
      run_id: 'run-1', status: 'completed', goal: '机器学习', updated_at: '2026-11-18T11:39:00.000Z',
    } as never;
    await wrapper.vm.$nextTick();

    const notice = wrapper.get('[aria-label="上次的生成结果"]');
    expect(notice.text()).toContain('还没保存');
    expect(wrapper.get(DRAWER).text()).not.toContain('上次的生成结果');
    // 它本身就是全部内容：不需要再给一个"明细"
    expect(wrapper.find('[data-test="run-status-detail"]').exists()).toBe(false);
  });
});
