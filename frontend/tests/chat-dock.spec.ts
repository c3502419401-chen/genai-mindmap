import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { MindMapContent } from '../src/api/types';
import { useChatStore } from '../src/stores/chat';
import { useMindMapStore } from '../src/stores/mindmap';
import { useSourcesStore } from '../src/stores/sources';
import { textSource } from '../src/sources/input';
import { isPrefsRequest, prefsReply } from './helpers/prefs-stub';
import { isOptimizationRequest, optimizationReply } from './helpers/optimization-stub';

/**
 * 底部对话框的状态机、语音态、材料缩略图与悬浮球。
 *
 * 这一版的核心交互就是这几条：
 *   空闲 → 完整对话框；生成中 → 一条细进度；生成完 → 淡出（只剩画布 + 悬浮球）；
 *   选中节点 → 工具条；点「改」→ 对话框回来并预填。
 *
 * jsdom 量不出像素，但**谁渲染、谁消失、谁带什么文字**全都钉得住；像素那部分在
 * `.visioncheck/verify_chat_dock.py` 的真浏览器里量。
 */
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

const TREE: MindMapContent = {
  title: '机器学习',
  root: { id: 'r', label: '机器学习', children: [{ id: 'a', label: '监督学习', children: [] }] },
};

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

function stubFetch(stream: unknown[] = []) {
  const mock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input);
    if (isPrefsRequest(url)) return prefsReply(url);
    // 生成落地后自动跑的只读结构检查：背景请求，不进"最后一条请求"的断言。
    if (isOptimizationRequest(url)) return optimizationReply();
    if (url.endsWith('/health')) return { ok: true, status: 200, json: async () => ({ version: 'test' }) } as unknown as Response;
    return streamReply(stream);
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

const wrappers: VueWrapper[] = [];

function mountApp(): VueWrapper {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(App, { global: { plugins: [pinia] } });
  wrappers.push(wrapper);
  return wrapper;
}

const DOCK_CARD = '[data-test="intent-box"]';
const SUBMIT = `${DOCK_CARD} button`;

/** 整块对话框当前是不是淡出的（`data-faded="true"` 与 `opacity-0` 同步）。 */
function dockFaded(wrapper: VueWrapper): boolean {
  return wrapper.get('[data-test="chat-dock"]').attributes('data-faded') === 'true';
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount());
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('底部对话框的状态机', () => {
  it('空闲：完整对话框（输入框、按钮行、缩略图不在）', async () => {
    stubFetch();
    const wrapper = mountApp();
    await flushPromises();

    expect(wrapper.find('textarea#intent').exists()).toBe(true);
    expect(wrapper.find('[data-test="deep-thinking"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="attach"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="voice-toggle"]').exists()).toBe(true);
    // 没有材料时不出缩略图区：空一条横条只是占地方
    expect(wrapper.find('[data-test="thumbs"]').exists()).toBe(false);
    expect(dockFaded(wrapper)).toBe(false);
  });

  it('生成中：整块收成一条细进度，输入框与参数都不渲染', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.busy = true;
    store.stageText = '正在定框架';
    await wrapper.vm.$nextTick();

    const row = wrapper.get('[data-test="run-status"]');
    expect(row.text()).toContain('正在生成');
    expect(row.text()).toContain('正在定框架');
    expect(row.find('[data-test="run-status-cancel"]').exists()).toBe(true);
    // 「收缩」不是"盖一层"：输入框、按钮行、参数全都不在了
    expect(wrapper.find('textarea#intent').exists()).toBe(false);
    expect(wrapper.find(DOCK_CARD).exists()).toBe(false);
  });

  it('生成完：2 秒后淡出，只剩画布 + 悬浮球', async () => {
    stubFetch([{ type: 'done', seq: 3, content: TREE, mode: 'model' }]);
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    await store.runGeneration('机器学习', 'edit');
    await flushPromises();
    await wrapper.vm.$nextTick();

    // 结果先落一条状态行：用户要看得见"已落到画布"
    expect(wrapper.get('[data-test="run-status"]').text()).toContain('已落到画布');
    expect(dockFaded(wrapper)).toBe(false);

    vi.advanceTimersByTime(2100);
    await wrapper.vm.$nextTick();
    expect(dockFaded(wrapper)).toBe(true);
  });

  it('失败时对话框留住（他要改一句重来，不该先翻历史）', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.agentError = '模型服务暂时不可用';
    await wrapper.vm.$nextTick();

    vi.advanceTimersByTime(5000);
    await wrapper.vm.$nextTick();

    expect(dockFaded(wrapper)).toBe(false);
    expect(wrapper.get('[data-test="run-status"]').text()).toContain('生成失败');
    expect(wrapper.find('textarea#intent').exists()).toBe(true);
  });

  it('有诊断：成功那行写着条数，按钮能直达明细，而且不淡出', async () => {
    // 后端分片生成的真实形态：ok=true，但两条诊断。
    stubFetch([
      {
        type: 'tool_end',
        seq: 1,
        call_id: 'c1',
        name: 'mindmap_generate',
        ok: true,
        result_summary: {
          diagnostics: [
            { level: 'warn', code: 'D1', message: '分支「a」展开失败，已留空' },
            { level: 'warn', code: 'D2', message: '分支「b」只有 2 个要点' },
          ],
        },
      },
      { type: 'done', seq: 2, content: TREE, mode: 'model' },
    ]);
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    await store.runGeneration('做一张导图', 'edit');
    await flushPromises();
    await wrapper.vm.$nextTick();

    // 1) 成功那行里留着可核对的**数量**
    expect(wrapper.get('[data-test="run-status-text"]').text()).toContain('有 2 条诊断');
    // 2) 入口就在同一行，写着同一个数
    expect(wrapper.get('[data-test="run-status-detail"]').text()).toBe('2 条诊断');
    // 3) `store.status` 那句（对话框淡出之后唯一还看得见的结论）也带数量
    expect(store.status).toContain('有 2 条诊断');

    // 4) 有诊断＝该看的东西还没看：两秒过去也不淡出
    vi.advanceTimersByTime(5000);
    await wrapper.vm.$nextTick();
    expect(dockFaded(wrapper)).toBe(false);
  });

  it('输入框里还有没发出去的字时，成功也不淡出', async () => {
    stubFetch([{ type: 'done', seq: 2, content: TREE, mode: 'model' }]);
    const wrapper = mountApp();
    const store = useMindMapStore();
    await wrapper.get('textarea#intent').setValue('后半句还没写完');
    store.preview(TREE, 'right');
    store.generatedSummary = { nodes: 2, changes: null };
    await wrapper.vm.$nextTick();

    vi.advanceTimersByTime(5000);
    await wrapper.vm.$nextTick();
    expect(dockFaded(wrapper)).toBe(false);
  });

  it('画布还空着就永远留着：那是唯一的入口', async () => {
    stubFetch();
    const wrapper = mountApp();
    vi.advanceTimersByTime(5000);
    await wrapper.vm.$nextTick();
    expect(dockFaded(wrapper)).toBe(false);
  });

  it('输入法组合中（v-model 还是空的）不算"没在写"：计时到点也不淡出', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.generatedSummary = { nodes: 2, changes: null };
    await wrapper.vm.$nextTick();

    const box = wrapper.get('textarea#intent');
    // 只发组合事件、不给焦点：钉住 `composing` 自己就能当刹车（有些浏览器把
    // 组合输入打进一个还没触发 focus 的元素，也可能反过来）。
    await box.trigger('compositionstart');
    expect((box.element as HTMLTextAreaElement).value).toBe('');

    vi.advanceTimersByTime(5000);
    await wrapper.vm.$nextTick();
    expect(dockFaded(wrapper)).toBe(false);
  });

  it('焦点在输入框里就不淡出，失焦且无草稿之后才走完那 2 秒', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.generatedSummary = { nodes: 2, changes: null };
    await wrapper.vm.$nextTick();

    const box = wrapper.get('textarea#intent');
    await box.trigger('focus');
    vi.advanceTimersByTime(5000);
    await wrapper.vm.$nextTick();
    expect(dockFaded(wrapper)).toBe(false);

    // 失焦：不是"立刻淡出"，而是重新计满 2 秒——用户可能只是点了一下画布。
    await box.trigger('blur');
    vi.advanceTimersByTime(1000);
    await wrapper.vm.$nextTick();
    expect(dockFaded(wrapper)).toBe(false);

    vi.advanceTimersByTime(1200);
    await wrapper.vm.$nextTick();
    expect(dockFaded(wrapper)).toBe(true);
  });

  it('上次没保存的结果独立成一条：成功时不被"已落到画布"挤掉', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.generatedSummary = { nodes: 2, changes: null };
    store.resumableRun = {
      run_id: 'run-old', status: 'done', intent: 'create', goal: '上一张图',
      created_at: '2024-01-01T03:00:00Z', updated_at: '2024-01-01T03:12:00Z', applied_at: null,
    };
    await wrapper.vm.$nextTick();

    // 两条同时在场：状态行说这一次，提示说上一次。
    expect(wrapper.get('[data-test="run-status"]').text()).toContain('已落到画布');
    const notice = wrapper.get('[data-test="resumable-notice"]');
    expect(notice.text()).toContain('还没保存');
    expect(notice.text()).toContain('上一张图');

    // 但它**不**是淡出刹车：实测当刹车之后，只要历史里有一条没保存的结果，
    // 每次生成完的对话框都不再淡出（见 ChatDock 里 `shouldKeep` 的注释）。
    vi.advanceTimersByTime(2100);
    await wrapper.vm.$nextTick();
    expect(dockFaded(wrapper)).toBe(true);

    // 处理掉它（不用了）之后提示消失。
    await notice.findAll('button')[1].trigger('click');
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="resumable-notice"]').exists()).toBe(false);
  });

  it('刚跑完的那一次不以"上次"自称（那是当前画布上的东西）', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.generatedSummary = { nodes: 2, changes: null };
    store.resumableRun = {
      run_id: 'run-current', status: 'done', intent: 'create', goal: '刚生成的这张',
      created_at: '2024-01-01T03:00:00Z', updated_at: '2024-01-01T03:12:00Z', applied_at: null,
    };
    store.pendingRunId = 'run-current';
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="resumable-notice"]').exists()).toBe(false);
  });

  it('检查点说"结构还在、已存 N 个节点"，不说"只完成了一部分"', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.resumableRun = {
      run_id: 'run-cp', status: 'failed', intent: 'create', goal: '崩掉的那张',
      created_at: '2024-01-01T03:00:00Z', updated_at: '2024-01-01T03:12:00Z', applied_at: null,
      checkpointed_at: '2024-01-01T03:05:00Z', node_count: 37,
    };
    await wrapper.vm.$nextTick();

    const notice = wrapper.get('[data-test="resumable-notice"]').text();
    // 结构是完整的，缺的只是说明——说成"只完成了一部分"会让用户以为要重跑。
    expect(notice).toContain('结构还在');
    expect(notice).toContain('37 个节点');
    expect(notice).not.toContain('只完成了一部分');
    expect(notice).not.toContain('被中断');
  });

  it('检查点里是 0 个节点时照样说"0 个节点"，不吞成没存下', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.resumableRun = {
      run_id: 'run-cp0', status: 'failed', intent: 'create', goal: '空树那张',
      created_at: '2024-01-01T03:00:00Z', updated_at: '2024-01-01T03:12:00Z', applied_at: null,
      checkpointed_at: '2024-01-01T03:05:00Z', node_count: 0,
    };
    await wrapper.vm.$nextTick();

    // 0 是合法读数（检查点落在裁剪之后、恰好空树）。`node_count ? ...` 会把它当成
    // 「没存下节点」，于是用户看到一句"结构还在"，却不知道结构是空的。
    const notice = wrapper.get('[data-test="resumable-notice"]').text();
    expect(notice).toContain('0 个节点');
  });

  it('检查点上多一个「补齐说明」，别的形态没有', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');

    // 半棵树且已经 done：缺的是内容，补说明帮不上（结构本身没长完），而且 done
    // 那一行 `begin()` 只会幂等重放，点了也不会真补。两次判据同向，按钮都不该有。
    store.resumableRun = {
      run_id: 'run-partial', status: 'done', intent: 'create', goal: '半棵',
      created_at: '2024-01-01T03:00:00Z', updated_at: '2024-01-01T03:12:00Z', applied_at: null,
      partial: true,
    };
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="run-status-fill"]').exists()).toBe(false);

    // 检查点：结构完整、只缺说明 —— 补说明是唯一"更便宜"的补救
    store.resumableRun = {
      run_id: 'run-cp', status: 'failed', intent: 'create', goal: '崩掉的那张',
      created_at: '2024-01-01T03:00:00Z', updated_at: '2024-01-01T03:12:00Z', applied_at: null,
      checkpointed_at: '2024-01-01T03:05:00Z', node_count: 5,
    };
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="run-status-fill"]').exists()).toBe(true);
  });

  it('补说明失败一次之后，入口还在（时间戳被清掉不算"不用补了"）', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');

    // 这一行是「补说明」自己跑坏留下的：`begin()` 重开时清 `checkpointed_at`，
    // `finish()` 又在交出候选（edit 的快照仍非空）时再清一次。树上还缺说明，
    // 可那个时间戳已经没了 —— 用时间戳当判据，用户就永久失去了补救入口。
    store.resumableRun = {
      run_id: 'run-fill-failed', status: 'failed', intent: 'edit', goal: '补说明',
      created_at: '2024-01-01T03:00:00Z', updated_at: '2024-01-01T03:20:00Z', applied_at: null,
      checkpointed_at: null, node_count: null,
    };
    await wrapper.vm.$nextTick();

    expect(wrapper.find('[data-test="run-status-fill"]').exists()).toBe(true);
  });

  it('点「补齐说明」先载入、再用**同一条** run_id 发 edit 请求', async () => {
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (isPrefsRequest(url)) return prefsReply(url);
      // 画布内容一变，前端就自动跑一次只读结构检查（`ChatDock` 那条 watch）：
      // 这一条用例里内容会变两回（先 `preview` 载入检查点、再收 `done` 帧），
      // 于是多出两条背景请求。它们与"点了这个入口发出什么"无关，不进 `calls`。
      if (isOptimizationRequest(url)) return optimizationReply();
      if (url.endsWith('/health')) {
        return { ok: true, status: 200, json: async () => ({ version: 'test' }) } as unknown as Response;
      }
      if (url.includes('/api/agent/runs/run-cp') && !url.includes('stream')) {
        // 台账详情：检查点里那棵树（结构在、说明空）
        return {
          ok: true, status: 200,
          json: async () => ({
            run_id: 'run-cp', status: 'failed', intent: 'create', goal: '崩掉的那张',
            created_at: '2024-01-01T03:00:00Z', updated_at: '2024-01-01T03:12:00Z',
            applied_at: null, candidate: TREE, mode: 'model',
            checkpointed_at: '2024-01-01T03:05:00Z', partial: null,
          }),
        } as unknown as Response;
      }
      calls.push({ url, body: JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown> });
      return streamReply([{ type: 'done', seq: 3, content: TREE, mode: 'model' }]);
    });
    vi.stubGlobal('fetch', fetchMock);

    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.resumableRun = {
      run_id: 'run-cp', status: 'failed', intent: 'create', goal: '崩掉的那张',
      created_at: '2024-01-01T03:00:00Z', updated_at: '2024-01-01T03:12:00Z', applied_at: null,
      checkpointed_at: '2024-01-01T03:05:00Z', node_count: 5,
    };
    await wrapper.vm.$nextTick();

    await wrapper.get('[data-test="run-status-fill"]').trigger('click');
    await flushPromises();

    expect(calls).toHaveLength(1);
    const sent = calls[0]!.body;
    // 沿用那条 run_id：服务端 `begin()` 对 failed/cancelled 是重开同一行，
    // 换新 id 会让一次生成在台账上变成两次运行。
    expect(sent.run_id).toBe('run-cp');
    expect(sent.intent).toBe('edit');
    // 补说明要的是"画布上的这棵树"，`edit` 必须带快照
    expect(sent.snapshot).toBeTruthy();
    // 选区显式给空：补说明按整棵树的长度数，不是"只补我圈的"
    expect(sent.selected_ids).toEqual([]);
  });
});

describe('材料缩略图', () => {
  /**
   * 空状态的入口。
   *
   * 以前整行缩略图（连同里面那个「管理材料」）挂在 `v-if="sources.items.length"` 上，
   * 于是第一次打开的人只看得到一个 `+` 图标——"材料能怎么给"（粘贴正文 / 上传文件 /
   * 录音）一个字都没出现在屏幕上，只有把鼠标停在 `+` 上才知道。而材料是**可选**的，
   * 所以这个入口不能只在"已经有材料"之后才存在。
   *
   * 这条钉三件事：空状态有一个词（不是「· 0 份」）、它是同一个面板、点开之后词换成
   * 「收起材料」。
   */
  it('空状态也有材料入口：「添加材料」，点开就是粘贴 / 上传 / 录音', async () => {
    stubFetch();
    const wrapper = mountApp();
    await flushPromises();

    // 空状态仍然不出缩略图区（一条空横条只是占地方），但入口在。
    expect(wrapper.find('[data-test="thumbs"]').exists()).toBe(false);
    const entry = wrapper.get('[data-test="manage-materials"]');
    expect(entry.text()).toBe('添加材料');

    await entry.trigger('click');
    await wrapper.vm.$nextTick();

    const panel = wrapper.get('[data-test="material-panel"]');
    expect(panel.attributes('style') ?? '').not.toContain('display: none');
    expect(panel.text()).toContain('粘贴材料正文');
    expect(panel.text()).toContain('添加文件（可多选）');
    expect(panel.text()).toContain('录音添加来源');
    // 展开之后同一个按钮变成"收起来"，否则它会指着一个已经打开的面板说"添加"。
    expect(entry.text()).toBe('收起材料');
  });

  it('一份材料一个缩略块，✕ 就地移除', async () => {
    stubFetch();
    const wrapper = mountApp();
    const sources = useSourcesStore();
    sources.addText('一段材料正文');
    await wrapper.vm.$nextTick();

    expect(wrapper.get('[data-test="thumbs"]').text()).toContain('文本 1');
    expect(wrapper.get('[data-test="thumbs"]').text()).toContain('字');

    await wrapper.get('[aria-label="移除 文本 1"]').trigger('click');
    expect(sources.items).toHaveLength(0);
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="thumbs"]').exists()).toBe(false);
  });

  it('解析失败的那份在缩略块里说明原因，并能重试', async () => {
    stubFetch();
    const wrapper = mountApp();
    const sources = useSourcesStore();
    sources.items = [{
      key: 'k1', id: 'k1', label: '坏文件.pdf', kind: 'document', state: 'failed',
      source: null, error: '这个来源没有解析出可用文字', seconds: 0,
    }];
    await wrapper.vm.$nextTick();

    const thumbs = wrapper.get('[data-test="thumbs"]').text();
    expect(thumbs).toContain('坏文件.pdf');
    expect(thumbs).toContain('没有解析出可用文字');
    expect(wrapper.get('[data-test="thumbs"]').find('button:not([aria-label])').exists()).toBe(true);
  });

  it('「+」打开材料面板：粘贴、上传、录音都还在', async () => {
    stubFetch();
    const wrapper = mountApp();
    expect(wrapper.get('[data-test="material-panel"]').attributes('style') ?? '').toContain('display: none');

    await wrapper.get('[data-test="attach"]').trigger('click');
    await wrapper.vm.$nextTick();

    const panel = wrapper.get('[data-test="material-panel"]');
    expect(panel.attributes('style') ?? '').not.toContain('display: none');
    expect(panel.text()).toContain('粘贴材料正文');
    expect(panel.text()).toContain('添加文件（可多选）');
    expect(panel.text()).toContain('录音添加来源');
  });

  it('还没点「添加」的材料正文在生成态之后还在（组件被卸载也不丢）', async () => {
    stubFetch();
    const wrapper = mountApp();
    await wrapper.get('[data-test="attach"]').trigger('click');
    await wrapper.vm.$nextTick();
    await wrapper.get('#material-text').setValue('还没点添加的一段正文');

    const sources = useSourcesStore();
    expect(sources.draftText).toBe('还没点添加的一段正文');

    // 进入生成态：整块对话框换成一条细进度，材料面板那个组件被 `v-if` 整个卸载。
    // 草稿以前住在那里面，这一刻就没了。
    const store = useMindMapStore();
    store.busy = true;
    await wrapper.vm.$nextTick();
    expect(wrapper.find('#material-text').exists()).toBe(false);
    expect(sources.draftText).toBe('还没点添加的一段正文');

    store.busy = false;
    await wrapper.vm.$nextTick();
    await wrapper.get('[data-test="attach"]').trigger('click');
    await wrapper.vm.$nextTick();

    const box = wrapper.get('#material-text').element as HTMLTextAreaElement;
    expect(box.value).toBe('还没点添加的一段正文');
  });

  it('草稿算刹车：有没点「添加」的正文时，成功也不淡出', async () => {
    stubFetch();
    const wrapper = mountApp();
    await wrapper.get('[data-test="attach"]').trigger('click');
    await wrapper.vm.$nextTick();
    await wrapper.get('#material-text').setValue('写了一半的材料');

    const store = useMindMapStore();
    const sources = useSourcesStore();
    store.preview(TREE, 'right');
    store.generatedSummary = { nodes: 2, changes: null };
    await wrapper.vm.$nextTick();

    vi.advanceTimersByTime(5000);
    await wrapper.vm.$nextTick();
    expect(dockFaded(wrapper)).toBe(false);

    // 草稿清掉之后才允许淡出：计时器要重新开始（watch 的源里有它）。
    sources.draftText = '';
    await wrapper.vm.$nextTick();
    vi.advanceTimersByTime(2100);
    await wrapper.vm.$nextTick();
    expect(dockFaded(wrapper)).toBe(true);
  });
});

describe('粘贴正文：长正文直接进材料', () => {
  /** jsdom 没有 `ClipboardEvent` 的构造函数，按它的形状造一个。 */
  function paste(el: Element, text: string): Event {
    const event = new Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', { value: { getData: () => text } });
    el.dispatchEvent(event);
    return event;
  }

  it('粘一段正文：一步变成材料，主题框里不留字，并说明能放回去', async () => {
    stubFetch();
    const wrapper = mountApp();
    const sources = useSourcesStore();
    const body = '这一段是要整理成导图的正文。'.repeat(20);

    const event = paste(wrapper.get('textarea#intent').element, body);
    await wrapper.vm.$nextTick();

    // 五步（点 + → 面板 → 粘贴框 → Ctrl+V → 点添加）变成一步。
    expect(event.defaultPrevented).toBe(true);
    expect(sources.items).toHaveLength(1);
    expect(sources.items[0].state).toBe('ready');
    expect((wrapper.get('textarea#intent').element as HTMLTextAreaElement).value).toBe('');

    const notice = wrapper.get('[data-test="paste-material"]');
    expect(notice.text()).toContain(`${body.length} 字`);
    expect(notice.text()).toContain('已经放进材料');
  });

  it('「放回输入框」把正文退回主题框，并撤掉那份材料', async () => {
    stubFetch();
    const wrapper = mountApp();
    const sources = useSourcesStore();
    const body = '一段要整理的材料正文'.repeat(20);
    paste(wrapper.get('textarea#intent').element, body);
    await wrapper.vm.$nextTick();

    await wrapper.get('[data-test="paste-material-undo"]').trigger('click');
    await wrapper.vm.$nextTick();

    expect(sources.items).toHaveLength(0);
    expect((wrapper.get('textarea#intent').element as HTMLTextAreaElement).value).toBe(body);
    expect(wrapper.find('[data-test="paste-material"]').exists()).toBe(false);
  });

  it('短粘贴照旧进输入框（主题是两个词，不是一篇正文）', async () => {
    stubFetch();
    const wrapper = mountApp();
    const sources = useSourcesStore();

    const event = paste(wrapper.get('textarea#intent').element, '机器学习');
    await wrapper.vm.$nextTick();

    expect(event.defaultPrevented).toBe(false);
    expect(sources.items).toHaveLength(0);
    expect(wrapper.find('[data-test="paste-material"]').exists()).toBe(false);
  });

  it('材料在别处被移除时提示跟着走（它指的是那一行）', async () => {
    stubFetch();
    const wrapper = mountApp();
    const sources = useSourcesStore();
    paste(wrapper.get('textarea#intent').element, '要整理的材料正文'.repeat(20));
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="paste-material"]').exists()).toBe(true);

    sources.remove(sources.items[0].key);
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="paste-material"]').exists()).toBe(false);
  });
});

/**
 * 「主题可以不写」那句话的两半（验收记录 §四.1 / 手册第 3 步）。
 *
 * 后端 `AgentRunRequest.goal` 是 `min_length=1`：空主题会被 422 拒掉，所以按钮不能
 * 放行。**能让的那一半只有一种情形**——材料带一个能当标题的文件名，前端拿它兜底
 * 拼出 goal。粘贴进来的正文没有文件名，兜底不存在，于是"不写也行"这一支走不通。
 *
 * 判据是"用户看到灰按钮时知道差什么"，所以这里断的是提示文案与悬停提示，不是样式。
 */
describe('主题空着时，界面要说清差什么才能点', () => {
  function paste(el: Element, text: string): Event {
    const event = new Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', { value: { getData: () => text } });
    el.dispatchEvent(event);
    return event;
  }

  it('粘的正文没有标题：按钮灰着，提示说「写一句主题才能生成」', async () => {
    stubFetch();
    const wrapper = mountApp();
    const body = '一段要整理的材料正文'.repeat(20);

    paste(wrapper.get('textarea#intent').element, body);
    await wrapper.vm.$nextTick();

    const submit = wrapper.findAll('button')
      .find(button => (button.attributes('aria-label') || '') === '生成导图');
    expect(submit).toBeTruthy();
    expect(submit!.attributes('disabled')).toBeDefined();

    const hint = wrapper.get('[data-test="intent-hint"]');
    // 这句是修复前那句反话："不写就用材料标题当中心主题"——照它做，按钮点不下去。
    expect(hint.text()).not.toContain('不写就用材料标题');
    expect(hint.text()).toContain('没有标题');
    expect(hint.text()).toContain('写一句主题');
    // 灰按钮时提示用警示色，否则 11px 的浅灰读起来像页脚说明。
    expect(hint.classes()).toContain('text-amber-700');
    // 悬停同样给原因，不再是重复一遍按钮名。
    expect(submit!.attributes('title')).toContain('写一句主题');
  });

  it('写一句主题之后按钮就可用，提示回到"这次会做什么"', async () => {
    stubFetch();
    const wrapper = mountApp();
    paste(wrapper.get('textarea#intent').element, '一段要整理的材料正文'.repeat(20));
    await wrapper.vm.$nextTick();

    await wrapper.get('textarea#intent').setValue('课程大纲');
    await wrapper.vm.$nextTick();

    const submit = wrapper.findAll('button')
      .find(button => (button.attributes('aria-label') || '') === '生成导图');
    expect(submit!.attributes('disabled')).toBeUndefined();
    expect(wrapper.get('[data-test="intent-hint"]').classes()).not.toContain('text-amber-700');
  });

  it('材料带可用文件名时确实可以不写主题（这一半照旧成立）', async () => {
    stubFetch();
    const wrapper = mountApp();
    const sources = useSourcesStore();
    sources.addText('说明');
    // 文件名就是标题的载体：`materialTitle` 拿它兜底，goal 拼成
    // "用材料标题「课程大纲」当主题…"，于是空主题也能提交。
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        source: {
          ...textSource('第一章 绪论'), id: 'doc-1', kind: 'document',
          filename: '课程大纲.md', status: 'parsed',
        },
      }),
    }));
    await sources.addFiles([new File(['第一章 绪论'], '课程大纲.md', { type: 'text/markdown' })]);
    await wrapper.vm.$nextTick();

    const submit = wrapper.findAll('button')
      .find(button => (button.attributes('aria-label') || '') === '生成导图');
    expect(submit!.attributes('disabled')).toBeUndefined();
    expect(wrapper.get('[data-test="intent-hint"]').text()).toContain('课程大纲');
  });
});

describe('材料没解析完时，改图也不能提交', () => {
  it('编辑已有图 + 材料解析中：提交按钮禁用，提示说的是材料', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    const sources = useSourcesStore();
    store.preview(TREE, 'right');
    store.select('a');
    sources.items = [{
      key: 'k1', id: 'k1', label: '大文件.pdf', kind: 'document', state: 'parsing',
      source: null, error: '', seconds: 3,
    }];
    await wrapper.get('textarea#intent').setValue('把这一支改细一点');
    await wrapper.vm.$nextTick();

    // 编辑请求也带 `sources`：正在解析的这一份会被静默漏掉，所以这里必须拦住。
    expect(wrapper.get(SUBMIT).attributes('disabled')).toBeDefined();
    expect(wrapper.text()).toContain('来源正在解析');
  });

  it('解析失败也一样拦住（失败那份同样进不了请求体）', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    const sources = useSourcesStore();
    store.preview(TREE, 'right');
    store.select('a');
    sources.items = [{
      key: 'k1', id: 'k1', label: '坏文件.pdf', kind: 'document', state: 'failed',
      source: null, error: '这个来源没有解析出可用文字', seconds: 0,
    }];
    await wrapper.get('textarea#intent').setValue('把这一支改细一点');
    await wrapper.vm.$nextTick();

    expect(wrapper.get(SUBMIT).attributes('disabled')).toBeDefined();
    expect(wrapper.text()).toContain('重试或移除');
  });

  it('材料都就绪时改图照常提交', async () => {
    stubFetch([{ type: 'done', seq: 2, content: TREE, mode: 'model' }]);
    const wrapper = mountApp();
    const store = useMindMapStore();
    const sources = useSourcesStore();
    store.preview(TREE, 'right');
    store.select('a');
    sources.items = [{
      key: 's1', id: 'doc-1', label: '材料.pdf', kind: 'document', state: 'ready',
      source: { ...textSource('材料正文'), id: 'doc-1', kind: 'document', filename: '材料.pdf' },
      error: '', seconds: 0,
    }];
    await wrapper.get('textarea#intent').setValue('把这一支改细一点');
    await wrapper.vm.$nextTick();

    expect(wrapper.get(SUBMIT).attributes('disabled')).toBeUndefined();
  });
});

describe('语音模式', () => {
  it('点 🎤 换成「按住说话」，⌨ 能切回键盘', async () => {
    stubFetch();
    const wrapper = mountApp();

    await wrapper.get('[data-test="voice-toggle"]').trigger('click');
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="press-to-talk"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="press-to-talk"]').text()).toContain('按住说话');
    expect(wrapper.find('textarea#intent').exists()).toBe(false);
    expect(wrapper.get('[data-test="voice-toggle"]').text()).toBe('⌨');

    await wrapper.get('[data-test="voice-toggle"]').trigger('click');
    await wrapper.vm.$nextTick();
    expect(wrapper.find('textarea#intent').exists()).toBe(true);
    expect(wrapper.get('[data-test="voice-toggle"]').text()).toBe('🎤');
  });

  it('按住说话复用录音能力：松手之后那份 WAV 变成一份材料', async () => {
    const recorded = new File([new Uint8Array([1, 2, 3])], 'voice.wav', { type: 'audio/wav' });
    const stop = vi.fn(async () => recorded);
    const cancel = vi.fn();
    const startRecording = vi.fn(async () => ({
      elapsedSeconds: () => 1,
      stop,
      cancel,
    }));
    vi.doMock('../src/audio/recorder', async () => {
      const actual = await vi.importActual<typeof import('../src/audio/recorder')>('../src/audio/recorder');
      return {
        ...actual,
        startRecording,
        RecordingError: actual.RecordingError,
      };
    });
    vi.resetModules();
    const { default: VoicePress } = await import('../src/components/VoicePress.vue');
    const { mount: mountPlain } = await import('@vue/test-utils');

    const wrapper = mountPlain(VoicePress);
    await wrapper.get('[data-test="press-to-talk"]').trigger('pointerdown');
    await flushPromises();
    expect(startRecording).toHaveBeenCalled();
    expect(wrapper.get('[data-test="press-to-talk"]').text()).toContain('松开');

    window.dispatchEvent(new Event('pointerup'));
    await flushPromises();
    expect(stop).toHaveBeenCalled();
    expect(wrapper.emitted('recorded')?.[0]).toEqual([recorded]);
    wrapper.unmount();
    vi.doUnmock('../src/audio/recorder');
  });
});

describe('悬浮球与消息时间线', () => {
  it('新消息在面板关着时累加角标，点开后清零', async () => {
    stubFetch();
    const wrapper = mountApp();
    const chat = useChatStore();

    chat.said('第一句');
    chat.replied({ text: '已落到画布', nodes: 6, changes: 2 });
    await wrapper.vm.$nextTick();

    expect(wrapper.get('[data-test="message-badge"]').text()).toBe('2');

    await wrapper.get('[data-test="message-fab"]').trigger('click');
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="message-badge"]').exists()).toBe(false);

    const items = wrapper.findAll('[data-test="message-item"]');
    expect(items).toHaveLength(2);
    expect(items[0].text()).toContain('第一句');
    expect(items[1].text()).toContain('已落到画布');
  });

  it('一次生成完就有一条 AI 消息，带模型名与改动条数', async () => {
    stubFetch([{ type: 'done', seq: 2, content: TREE, mode: 'model', model: 'deepseek-reasoner' }]);
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    await store.runGeneration('再加一支', 'edit');
    await flushPromises();
    await wrapper.vm.$nextTick();

    const chat = useChatStore();
    const last = chat.messages[chat.messages.length - 1];
    expect(last.role).toBe('ai');
    expect(last.model).toBe('deepseek-reasoner');
    expect(last.text).toContain('已落到画布');
    expect(wrapper.find('[data-test="message-badge"]').exists()).toBe(true);
  });

  it('模型名写成「请求 X → 服务 Y」：服务端把请求路由到别处要看得出', async () => {
    // 这一帧就是线上的形状：请求 deepseek-chat，响应回 deepseek-flash。
    stubFetch([{
      type: 'done', seq: 2, content: TREE, mode: 'model',
      model: 'deepseek-chat', served_model: 'deepseek-flash',
    }]);
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    await store.runGeneration('再加一支', 'edit');
    await flushPromises();

    expect(store.runModel).toBe('deepseek-chat');
    expect(store.runServedModel).toBe('deepseek-flash');

    await wrapper.get('[data-test="message-fab"]').trigger('click');
    await wrapper.vm.$nextTick();
    const chip = wrapper.get('[data-test="message-model"]');
    expect(chip.text()).toBe('请求 deepseek-chat → 服务 deepseek-flash');
  });

  it('服务端没回报模型名时写"未知"，不拿请求名冒充', async () => {
    stubFetch([{ type: 'done', seq: 2, content: TREE, mode: 'model', model: 'deepseek-chat' }]);
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    await store.runGeneration('再加一支', 'edit');
    await flushPromises();

    await wrapper.get('[data-test="message-fab"]').trigger('click');
    await wrapper.vm.$nextTick();
    expect(wrapper.get('[data-test="message-model"]').text()).toBe('请求 deepseek-chat → 服务 未知');
  });


  it('失败也进时间线，写成一条错误（而不是静默消失）', async () => {    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    // 走一遍真实的"开始 → 失败"：`agentRunning` 的下降沿是唯一的触发点
    store.agentRunning = true;
    store.busy = true;
    await wrapper.vm.$nextTick();
    store.agentError = '模型服务暂时不可用';
    store.agentRunning = false;
    store.busy = false;
    await wrapper.vm.$nextTick();

    const chat = useChatStore();
    expect(chat.messages.some(item => item.kind === 'error' && item.text.includes('模型服务暂时不可用'))).toBe(true);
  });

  it('「继续说一句」把对话框叫回来（淡出之后的回程入口）', async () => {
    stubFetch();
    const wrapper = mountApp();
    const chat = useChatStore();
    chat.said('第一句');
    await wrapper.vm.$nextTick();

    await wrapper.get('[data-test="message-fab"]').trigger('click');
    await wrapper.vm.$nextTick();
    await wrapper.get('[data-test="message-compose"]').trigger('click');
    await flushPromises();

    expect(chat.open).toBe(false);
    expect(wrapper.get('[data-test="chat-dock"]').attributes('data-faded')).toBe('false');
    expect(wrapper.find('textarea#intent').exists()).toBe(true);
  });

  it('「改这句」把原句填回输入框（改一个词重发，不用重打）', async () => {
    stubFetch();
    const wrapper = mountApp();
    const chat = useChatStore();
    chat.said('把报销流程画清楚');
    await wrapper.vm.$nextTick();

    await wrapper.get('[data-test="message-fab"]').trigger('click');
    await wrapper.vm.$nextTick();
    await wrapper.get('[data-test="message-reuse"]').trigger('click');
    await flushPromises();

    const input = wrapper.get('textarea#intent').element as HTMLTextAreaElement;
    expect(input.value).toBe('把报销流程画清楚');
    expect(wrapper.get('[data-test="chat-dock"]').attributes('data-faded')).toBe('false');
  });

  it('发出去的那句话立刻进时间线（对话框随后会收起并清空）', async () => {
    stubFetch([{ type: 'done', seq: 2, content: TREE, mode: 'model' }]);
    const wrapper = mountApp();
    await wrapper.get('textarea#intent').setValue('把报销流程画清楚');
    await wrapper.get(SUBMIT).trigger('click');
    await flushPromises();

    const chat = useChatStore();
    expect(chat.messages[0].role).toBe('user');
    expect(chat.messages[0].text).toBe('把报销流程画清楚');
  });

  /**
   * 用户那条消息的时间是 `NaN:NaN`，模型那条正常。
   *
   * 两条路各自漏一次：`said(text, at?)` 把 `at: undefined` 显式传进 `push`，而
   * `push` 先写默认时间、再展开 `message`，展开把它覆盖成 `undefined`；显示层又
   * 没判无效日期。写入与显示都得挡住——只修一个，另一条路照样把 `NaN:NaN` 摆上屏。
   */
  it('用户消息也带有效时间（NaN 不再来自默认值被覆盖）', async () => {
    stubFetch();
    mountApp();
    const chat = useChatStore();

    const said = chat.said('第一句');
    expect(Number.isFinite(said.at)).toBe(true);
    expect(said.at).toBeGreaterThan(0);
    // 显式给时间时用给的，不要一律改写成 now。
    expect(chat.said('第二句', 1700000000000).at).toBe(1700000000000);
  });

  it('时间无效时显示占位，不把 NaN:NaN 摆出来', async () => {
    stubFetch();
    const wrapper = mountApp();
    const chat = useChatStore();
    // 绕过 push 直接塞一条脏数据：显示层必须自己挡得住（历史数据、回放都可能带）。
    chat.messages = [{ id: 'm1', at: Number.NaN, role: 'user', text: '嗨' }];
    await wrapper.vm.$nextTick();

    await wrapper.get('[data-test="message-fab"]').trigger('click');
    await wrapper.vm.$nextTick();

    const item = wrapper.get('[data-test="message-item"]');
    expect(item.text()).not.toContain('NaN');
    expect(item.text()).toContain('--:--');
  });

  /**
   * 淡出的对话框仍然在 DOM 里（「继续说一句」要靠它叫回来），但它不该再吃键盘。
   *
   * 只设透明 + `pointer-events-none` + `aria-hidden` 只挡住鼠标和读屏：按 Tab 焦点
   * 照样会走进那个看不见的框，接着按 Enter 就是对着空气提交。
   */
  it('淡出之后整块是 inert：键盘焦点进不去', async () => {
    stubFetch([{ type: 'done', seq: 2, content: TREE, mode: 'model' }]);
    const wrapper = mountApp();
    await wrapper.get('textarea#intent').setValue('机器学习');
    await wrapper.get(SUBMIT).trigger('click');
    await flushPromises();
    await vi.advanceTimersByTimeAsync(2100);
    await wrapper.vm.$nextTick();

    const dock = wrapper.get('[data-test="chat-dock"]');
    expect(dock.attributes('data-faded')).toBe('true');
    expect(dock.attributes('inert')).toBeDefined();
    // 另一半没丢：DOM 还在，叫得回来。
    expect(wrapper.find('textarea#intent').exists()).toBe(true);
  });
});

describe('thinking 全链路：请求体与折叠展示', () => {
  it('勾「深度思考」→ 请求体带 deep_thinking:true，回复里的 reasoning 折进消息流', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (isPrefsRequest(url)) return prefsReply(url);
      if (url.endsWith('/health')) return { ok: true, status: 200, json: async () => ({ version: 'test' }) } as unknown as Response;
      const body = JSON.parse(String(init?.body ?? '{}'));
      expect(body.deep_thinking).toBe(true);
      return streamReply([{
        type: 'done', seq: 2, content: TREE, mode: 'model',
        model: 'deepseek-reasoner', reasoning: '先看材料，再定六个分支。'.repeat(3),
      }]);
    });
    vi.stubGlobal('fetch', fetchMock);

    const wrapper = mountApp();
    await wrapper.get('[data-test="deep-thinking"]').trigger('click');
    expect(wrapper.get('[data-test="deep-thinking"]').attributes('aria-pressed')).toBe('true');

    await wrapper.get('textarea#intent').setValue('机器学习');
    await wrapper.get(SUBMIT).trigger('click');
    await flushPromises();
    await wrapper.vm.$nextTick();

    const chat = useChatStore();
    const last = chat.messages[chat.messages.length - 1];
    expect(last.reasoning).toContain('先看材料');
    // 消息面板里的 `<details>` 折叠着全文（不默认展开，不截断）
    await wrapper.get('[data-test="message-fab"]').trigger('click');
    await wrapper.vm.$nextTick();
    const panel = wrapper.get('[data-test="message-panel"]');
    expect(panel.text()).toContain('模型当时的思考');
    expect(panel.text()).toContain('先看材料');
  });

  it('不勾时不发这个字段（与加开关之前逐字节相同）', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (isPrefsRequest(url)) return prefsReply(url);
      if (url.endsWith('/health')) return { ok: true, status: 200, json: async () => ({ version: 'test' }) } as unknown as Response;
      const body = JSON.parse(String(init?.body ?? '{}'));
      expect('deep_thinking' in body).toBe(false);
      return streamReply([{ type: 'done', seq: 2, content: TREE, mode: 'model' }]);
    });
    vi.stubGlobal('fetch', fetchMock);

    const wrapper = mountApp();
    await wrapper.get('textarea#intent').setValue('机器学习');
    await wrapper.get(SUBMIT).trigger('click');
    await flushPromises();
    expect(fetchMock).toHaveBeenCalled();
  });
});

/**
 * 材料面板什么时候挡在画布上。
 *
 * 判据（改动前只有第 1 条成立）：
 *
 *   1. 上传后            展开——他还要粘贴、录音、看解析进度。
 *   2. 生成中            不在画布上。
 *   3. 生成成功之后      **收起**，入口留在缩略图行（「管理材料 · N 份」）。
 *   4. 生成失败 / 部分完成 / 冲突  不收：那几种都在等他改材料重试。
 *   5. 用户自己点开的    收起一律让路，`materialBusy`（有草稿 / 录音中 / 解析中）同理。
 *
 * 第 5 条是这道改动的核心纪律：**自动行为不许覆盖用户刚做的操作**。它有两个真实
 * 窗口——生成成功后那 2 秒里他点了「管理材料」；以及他正在粘贴框里写的时候，别处
 * 触发的一轮生成落地了。
 */
describe('材料面板：什么时候挡在画布上', () => {
  /** `v-show` 收面板，所以可见性看内联样式；组件被卸载也算不可见。 */
  function panelVisible(wrapper: VueWrapper): boolean {
    const panel = wrapper.find('[data-test="material-panel"]');
    if (!panel.exists()) return false;
    return !(panel.attributes('style') ?? '').includes('display: none');
  }

  /** 造一个"成功落地"的终态：画布有树、状态行是 ok。 */
  async function succeed(wrapper: VueWrapper): Promise<void> {
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.generatedSummary = { nodes: 2, changes: null };
    await wrapper.vm.$nextTick();
  }

  it('上传后：面板展开（他还要粘贴、录音、看解析进度）', async () => {
    stubFetch();
    const wrapper = mountApp();
    expect(panelVisible(wrapper)).toBe(false);

    await wrapper.get('[data-test="attach"]').trigger('click');
    await wrapper.vm.$nextTick();
    expect(panelVisible(wrapper)).toBe(true);
  });

  it('生成中：面板跟着整块对话框一起不在（一条细进度取代它）', async () => {
    stubFetch();
    const wrapper = mountApp();
    await wrapper.get('[data-test="attach"]').trigger('click');
    await wrapper.vm.$nextTick();

    useMindMapStore().busy = true;
    await wrapper.vm.$nextTick();

    expect(wrapper.find('[data-test="material-panel"]').exists()).toBe(false);
  });

  it('生成一开始就收起：跑完回来是收起的样子，不用等成功那一刻', async () => {
    stubFetch([{ type: 'done', seq: 2, content: TREE, mode: 'model' }]);
    const wrapper = mountApp();
    await wrapper.get('[data-test="attach"]').trigger('click');
    await wrapper.vm.$nextTick();

    // 提交之后面板那一轮就归自动态管（`submitIntent` 交还用户意图）。
    await wrapper.get('textarea#intent').setValue('按主题生成');
    await wrapper.get(SUBMIT).trigger('click');
    await flushPromises();

    // 生成中整块被一条细进度取代，面板连 DOM 都没有；跑完回来时 materialOpen 已经
    // 是 false——这一条钉的正是"回来那个样子"，它是那个 bug 的直接反面。
    expect(panelVisible(wrapper)).toBe(false);
    expect(wrapper.find('[data-test="material-panel"]').exists()).toBe(true);
  });

  it('生成开始时手里有草稿、没能收起：他把草稿点进来源之后就补收', async () => {
    stubFetch([{ type: 'done', seq: 2, content: TREE, mode: 'model' }]);
    const wrapper = mountApp();
    const sources = useSourcesStore();
    await wrapper.get('[data-test="attach"]').trigger('click');
    await wrapper.vm.$nextTick();
    await wrapper.get('#material-text').setValue('一段还没点添加的正文');

    // 草稿把"生成开始那次收"拦下来了：他显然还要接着处理这段正文。
    await wrapper.get('textarea#intent').setValue('按主题生成');
    await wrapper.get(SUBMIT).trigger('click');
    await flushPromises();
    expect(panelVisible(wrapper)).toBe(true);

    // 点「添加」进来源 → 闸门松开 → 那次被拦下的收立即补办（不等下一次生成）。
    sources.addDraft();
    await wrapper.vm.$nextTick();
    expect(panelVisible(wrapper)).toBe(false);
  });

  it('生成失败：不收——他可能要改材料重试', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    await wrapper.get('[data-test="attach"]').trigger('click');
    await wrapper.vm.$nextTick();

    store.agentError = '上游 502';
    await wrapper.vm.$nextTick();
    vi.advanceTimersByTime(5000);
    await wrapper.vm.$nextTick();

    expect(panelVisible(wrapper)).toBe(true);
  });

  it('他亲手点开的：之后一轮生成落地也不自动收起', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    await wrapper.get('[data-test="attach"]').trigger('click');
    await wrapper.vm.$nextTick();

    // 这一轮是别处起的（画布「展开这一支」/ 悬浮球），不经过 `submitIntent`，
    // 所以"用户开过"这件事还挂着——自动收起必须让路。
    store.busy = true;
    await wrapper.vm.$nextTick();
    store.busy = false;
    await succeed(wrapper);

    expect(panelVisible(wrapper)).toBe(true);
  });

  it('粘贴框里有没点「添加」的正文时，成功也不自动收起', async () => {
    stubFetch();
    const wrapper = mountApp();
    await wrapper.get('[data-test="attach"]').trigger('click');
    await wrapper.vm.$nextTick();
    await wrapper.get('#material-text').setValue('刚粘进来的一大段正文');

    await succeed(wrapper);
    vi.advanceTimersByTime(5000);
    await wrapper.vm.$nextTick();

    expect(panelVisible(wrapper)).toBe(true);
    expect(useSourcesStore().draftText).toBe('刚粘进来的一大段正文');
  });

  it('有材料正在解析时，成功也不自动收起（进度正在这里显示）', async () => {
    stubFetch();
    const wrapper = mountApp();
    const sources = useSourcesStore();
    await wrapper.get('[data-test="attach"]').trigger('click');
    await wrapper.vm.$nextTick();

    sources.items = [{
      key: 'k1', id: 'k1', label: '长文档.pdf', kind: 'document', state: 'parsing',
      source: null, error: '', seconds: 3,
    }];
    await succeed(wrapper);
    vi.advanceTimersByTime(5000);
    await wrapper.vm.$nextTick();

    expect(panelVisible(wrapper)).toBe(true);
  });

  it('收起态入口：一个按钮，文案带份数，点它还能打开', async () => {
    stubFetch();
    const wrapper = mountApp();
    const sources = useSourcesStore();
    sources.addText('一段材料正文');
    await wrapper.vm.$nextTick();

    expect(wrapper.get('[data-test="manage-materials"]').text()).toBe('管理材料 · 1 份');

    await wrapper.get('[data-test="manage-materials"]').trigger('click');
    await wrapper.vm.$nextTick();
    expect(panelVisible(wrapper)).toBe(true);
    // 展开时同一个按钮变成"收起"，不再多长一个入口出来。
    expect(wrapper.get('[data-test="manage-materials"]').text()).toBe('收起材料 · 1 份');
  });

  it('最常走的那条路：点「+」传材料 → 提交 → 成功后照样收起', async () => {
    stubFetch([{ type: 'done', seq: 2, content: TREE, mode: 'model' }]);
    const wrapper = mountApp();
    const sources = useSourcesStore();
    sources.items = [{
      key: 's1', id: 'doc-1', label: '材料.pdf', kind: 'document', state: 'ready',
      source: { ...textSource('材料正文'), id: 'doc-1', kind: 'document', filename: '材料.pdf' },
      error: '', seconds: 0,
    }];
    await wrapper.get('[data-test="attach"]').trigger('click');
    await wrapper.vm.$nextTick();
    expect(panelVisible(wrapper)).toBe(true);

    await wrapper.get('textarea#intent').setValue('按材料生成');
    await wrapper.get(SUBMIT).trigger('click');
    await flushPromises();
    vi.advanceTimersByTime(2100);
    await wrapper.vm.$nextTick();
    expect(panelVisible(wrapper)).toBe(false);
  });
});

describe('材料参与生成', () => {
  it('缩略图里的材料照旧进请求体（材料没有因为换位置而丢）', async () => {
    const fetchMock = stubFetch([{ type: 'done', seq: 2, content: TREE, mode: 'model' }]);
    const wrapper = mountApp();
    const sources = useSourcesStore();
    sources.items = [{
      key: 's1', id: 'doc-1', label: '材料.pdf', kind: 'document', state: 'ready',
      source: { ...textSource('材料正文'), id: 'doc-1', kind: 'document', filename: '材料.pdf' },
      error: '', seconds: 0,
    }];
    await wrapper.vm.$nextTick();

    await wrapper.get('textarea#intent').setValue('按材料生成');
    await wrapper.get(SUBMIT).trigger('click');
    await flushPromises();

    // 按 URL 找那条**生成**请求，不取"最后一条"：生成落地之后前端还会自动跑一次
    // 只读结构检查（`/optimization/check`），它才是最后一条。断言要钉的是"材料进了
    // 生成请求体"，所以定位方式必须说清是哪一个请求。
    const streamCall = [...fetchMock.mock.calls]
      .reverse()
      .find(([input]) => String(input).includes('/agent/runs/stream'));
    const body = JSON.parse(String(streamCall?.[1]?.body ?? '{}'));
    expect(body.sources).toHaveLength(1);
  });
});
