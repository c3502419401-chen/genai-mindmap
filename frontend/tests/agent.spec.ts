import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import AgentPanel from '../src/components/AgentPanel.vue';
import App from '../src/App.vue';
import SourceInputs from '../src/components/SourceInputs.vue';
import type { MindMapContent, Source } from '../src/api/types';
import { textSource } from '../src/sources/input';
import { useMindMapStore } from '../src/stores/mindmap';
import { useSourcesStore } from '../src/stores/sources';

// jsdom 里没有 ResizeObserver，真正的 Vue Flow 画布跑不起来；挂整个 App 的那些
// 用例只关心状态行，画布替换成占位元素。
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

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

const content = (label = '根'): MindMapContent => ({
  title: '示例',
  root: { id: 'root', label, children: [{ id: 'a', label: '甲', children: [] }] },
});

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

const doneFrame = (payload: MindMapContent) => ({ type: 'done', seq: 4, content: payload });

/** 一次性 JSON 响应：openMap / save 走 request()，只读 json，不读流。 */
function jsonReply(payload: unknown): Response {
  return { ok: true, status: 200, body: null, json: async () => payload } as unknown as Response;
}

/** 已保存导图的记录，字段与 MapRecord 一致。 */
function mapRecord(id: string, payload: MindMapContent) {
  return { id, version: 3, content: payload, updated_at: '2024-01-01T00:00:00Z' };
}

/** 先把一张有 id 的导图打开，供"生成落地之后身份怎么变"的用例起手。 */
async function openExisting(id: string) {
  vi.stubGlobal('fetch', vi.fn(async () => jsonReply(mapRecord(id, content('原图')))));
  const store = useMindMapStore();
  await store.openMap(id);
  return store;
}

/**
 * 手动控制节奏的 SSE 响应：状态行这些"只在流进行中才有值"的字段，
 * 用一次性返回的流测不到——流一结束收尾就把它们清空了。
 */
function controllableReply() {
  const encoder = new TextEncoder();
  let push: (frame: unknown) => void = () => {};
  let finish: () => void = () => {};
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      push = (frame) => { controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`)); };
      finish = () => {
        controller.enqueue(encoder.encode('data: [DONE]\n\n'));
        controller.close();
      };
    },
  });
  const response = { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
  return { response, push, finish };
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('agent 运行', () => {
  it('create 意图不带 snapshot，请求打到 agent 路由', async () => {
    const calls = stubFetch(() => agentReply([
      { type: 'stage', seq: 1, stage: '准备' },
      doneFrame(content()),
    ]));
    const store = useMindMapStore();

    await store.runGeneration('给机器学习做一张导图', 'create');

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain('/agent/runs/stream');
    const body = calls[0].body as Record<string, unknown>;
    expect(body.intent).toBe('create');
    // create 带 snapshot 会被后端 422 拒，前端不能顺手传一个
    expect(body.snapshot ?? null).toBeNull();
  });

  it('edit 意图带上当前树', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content('改后'))]));
    const store = useMindMapStore();
    store.preview(content(), 'right');

    await store.runGeneration('把根改名', 'edit');

    const body = calls[0].body as Record<string, unknown>;
    expect(body.intent).toBe('edit');
    expect((body.snapshot as MindMapContent).root.label).toBe('根');
  });

  it('done 直接落到画布，并标成未保存', async () => {
    stubFetch(() => agentReply([
      { type: 'stage', seq: 1, stage: '准备' },
      { type: 'tool_start', seq: 2, call_id: 'c1', name: 'mindmap_generate' },
      { type: 'tool_end', seq: 3, call_id: 'c1', name: 'mindmap_generate', ok: true, latency_ms: 12 },
      doneFrame(content('新图')),
    ]));
    const store = useMindMapStore();
    store.preview(content('旧图'), 'right');

    await store.runGeneration('做一张导图', 'create');

    // 核心行为：没有候选、没有「应用」，done 那一刻树就进画布了
    expect(store.content?.root.label).toBe('新图');
    expect(store.dirty).toBe(true);
    expect(store.generatedSummary?.nodes).toBe(2);
    expect(store.agentEvents.map(event => event.type))
      .toEqual(['stage', 'tool_start', 'tool_end', 'done']);
  });

  /**
   * 「落地后视图是偏的」那一条（验收记录 §四.3：40 个已渲染节点只有 14 个在视口内）。
   *
   * 画布侧的自动适配有一道闸：`store.viewAdjusted`（"用户手动调过视口，别把他拽回去"）。
   * 它保护的是**用户正在看的这一屏**——而生成出来的新树属于下一屏。这个标志从来没在
   * "换了一张图"时复位，于是闸门把落地那一次适配也挡掉了，视口停在旧图的缩放与偏移上。
   */
  it('create 落地是换了一张图：用户之前调过视口也复位，落地那次适配不会被挡掉', async () => {
    stubFetch(() => agentReply([doneFrame(content('新图'))]));
    const store = useMindMapStore();
    store.preview(content('旧图'), 'right');
    // 生成前随手平移过画布，或生成期间点过一次引用跳转（`focusTick` 那条 watch
    // 无条件置真）——两种都很常见，落地时都会把这一屏留在旧图上。
    store.viewAdjusted = true;

    await store.runGeneration('做一张导图', 'create');

    expect(store.content?.root.label).toBe('新图');
    expect(store.viewAdjusted).toBe(false);
  });

  it('edit 落地是就地改：用户正看着的这一屏必须留着', async () => {
    stubFetch(() => agentReply([doneFrame(content('改后'))]));
    const store = useMindMapStore();
    store.preview(content('旧图'), 'right');
    store.viewAdjusted = true;

    await store.runGeneration('把根改名', 'edit');

    expect(store.viewAdjusted).toBe(true);
  });

  it('error 事件写进 agentError 且画布不变', async () => {    stubFetch(() => agentReply([
      { type: 'error', seq: 1, code: 'generation_failed', message: '模型服务不可用' },
    ]));
    const store = useMindMapStore();
    store.preview(content('旧图'), 'right');

    await store.runGeneration('做一张导图', 'create');

    expect(store.agentError).toBe('模型服务不可用');
    expect(store.content?.root.label).toBe('旧图');
    expect(store.generatedSummary).toBeNull();
    expect(store.agentRunning).toBe(false);
  });

  it('空目标不发请求', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const store = useMindMapStore();

    await store.runGeneration('   ', 'create');

    expect(calls).toHaveLength(0);
    // 提示必须指出是哪个框：用户把主题写进「导图标题」时，只说"请输入主题"
    // 会让他以为系统坏了。合并之后那个框在画布右上角，提示也得跟着搬家。
    expect(store.status).toContain('右上角');
    expect(store.status).toContain('导图标题');
  });

  it('没有导图时不允许 edit', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const store = useMindMapStore();

    await store.runGeneration('改一下', 'edit');

    expect(calls).toHaveLength(0);
    expect(store.status).toContain('还没有导图');
  });

  it('流只发 tool_start 就 EOF：报中断，不留「正在与模型协作」', async () => {
    stubFetch(() => agentReply([
      { type: 'tool_start', seq: 1, call_id: 'c1', name: 'mindmap_generate' },
    ]));
    const store = useMindMapStore();

    await store.runGeneration('做一张导图', 'create');

    // 既没有 done 也没有 error，不能当成正常结束
    expect(store.agentError).not.toBe('');
    expect(store.agentError).toContain('中断');
    expect(store.status).toContain('中断');
    expect(store.agentRunning).toBe(false);
    expect(store.busy).toBe(false);
    expect(store.generatedSummary).toBeNull();
  });

  it('状态行显示的是人话，不是工具名', async () => {
    const stream = controllableReply();
    vi.stubGlobal('fetch', vi.fn(async () => stream.response));
    const store = useMindMapStore();
    const running = store.runGeneration('做一张导图', 'create');

    stream.push({ type: 'tool_start', seq: 1, call_id: 'c1', name: 'rag_query' });
    await vi.waitFor(() => expect(store.stageText).toBe('正在查资料'));

    stream.push({ type: 'tool_end', seq: 2, call_id: 'c1', name: 'rag_query', ok: true, latency_ms: 900 });
    await vi.waitFor(() => expect(store.stageText).toBe('已查好资料'));

    stream.push(doneFrame(content()));
    stream.finish();
    await running;
  });

  it('心跳帧累积等待时长，真进度帧一到就归零', async () => {
    const stream = controllableReply();
    vi.stubGlobal('fetch', vi.fn(async () => stream.response));
    const store = useMindMapStore();
    const running = store.runGeneration('做一张导图', 'create');

    stream.push({ type: 'heartbeat', seq: 1, waited_ms: 10200 });
    await vi.waitFor(() => expect(store.waitedMs).toBe(10));

    // 心跳连着来是"还在等"，数字要跟着涨
    stream.push({ type: 'heartbeat', seq: 2, waited_ms: 20400 });
    await vi.waitFor(() => expect(store.waitedMs).toBe(20));

    // 真进度到了，静默结束：计时必须归零，否则「已等 20 秒」会一直挂着
    stream.push({ type: 'node_grow', seq: 3, path: [], label: '根' });
    await vi.waitFor(() => expect(store.waitedMs).toBe(0));

    stream.push(doneFrame(content()));
    stream.finish();
    await running;
  });

  it('心跳不改阶段文案：它不带业务进度', async () => {
    const stream = controllableReply();
    vi.stubGlobal('fetch', vi.fn(async () => stream.response));
    const store = useMindMapStore();
    const running = store.runGeneration('做一张导图', 'create');

    stream.push({ type: 'tool_start', seq: 1, call_id: 'c1', name: 'mindmap_generate' });
    await vi.waitFor(() => expect(store.stageText).toBe('正在定框架'));

    stream.push({ type: 'heartbeat', seq: 2, waited_ms: 11000 });
    await vi.waitFor(() => expect(store.waitedMs).toBe(11));
    // 让心跳把阶段文案顶掉，会让用户以为进度回退了
    expect(store.stageText).toBe('正在定框架');

    stream.push(doneFrame(content()));
    stream.finish();
    await running;
  });

  it('心跳不进事件列表：它只驱动等待计时，面板里没人读它', async () => {
    const stream = controllableReply();
    vi.stubGlobal('fetch', vi.fn(async () => stream.response));
    const store = useMindMapStore();
    const running = store.runGeneration('做一张导图', 'create');

    stream.push({ type: 'stage', seq: 1, stage: '准备' });
    await vi.waitFor(() => expect(store.agentEvents.map(event => event.type)).toEqual(['stage']));

    stream.push({ type: 'heartbeat', seq: 2, waited_ms: 10200 });
    await vi.waitFor(() => expect(store.waitedMs).toBe(10));
    // 计时更新了，但心跳不进数组：长静默会累积几十帧，每帧整数组复制没必要。
    expect(store.agentEvents.map(event => event.type)).toEqual(['stage']);

    stream.push(doneFrame(content()));
    stream.finish();
    await running;
  });

  it('取消之后等待计时归零：不能一边说已取消、一边说还在思考', async () => {
    const stream = controllableReply();
    vi.stubGlobal('fetch', vi.fn(async () => stream.response));
    const store = useMindMapStore();
    const running = store.runGeneration('做一张导图', 'create');

    stream.push({ type: 'heartbeat', seq: 1, waited_ms: 20400 });
    await vi.waitFor(() => expect(store.waitedMs).toBe(20));

    store.cancel();
    // fetch mock 的读端不会自己因为 abort 结束，手动收尾让 runGeneration 退出
    stream.finish();
    await running;

    expect(store.waitedMs).toBe(0);
    expect(store.status).toBe('已取消。');
  });

  it('断流之后等待计时归零：不再挂着「模型正在思考」', async () => {
    const stream = controllableReply();
    vi.stubGlobal('fetch', vi.fn(async () => stream.response));
    const store = useMindMapStore();
    const running = store.runGeneration('做一张导图', 'create');

    stream.push({ type: 'heartbeat', seq: 1, waited_ms: 31000 });
    await vi.waitFor(() => expect(store.waitedMs).toBe(31));

    // 没有 done / error 就 EOF：连接断了，这一轮已经结束
    stream.finish();
    await running;

    expect(store.waitedMs).toBe(0);
    expect(store.agentError).toContain('中断');
  });
});

describe('部分完成与诊断', () => {
  it('tool_end 标了 partial 时，状态行说明部分分支未完成', async () => {
    stubFetch(() => agentReply([
      // 后端分片生成的真实形态：ok=true，但分支没展开完
      {
        type: 'tool_end',
        seq: 1,
        call_id: 'c1',
        name: 'mindmap_generate',
        ok: true,
        latency_ms: 1200,
        result_summary: { planned_branches: 4, expanded_branches: 1, partial: true, diagnostics: [] },
      },
      doneFrame(content('候选')),
    ]));
    const store = useMindMapStore();

    await store.runGeneration('做一张导图', 'create');

    expect(store.agentWarnings.some(item => item.kind === 'partial')).toBe(true);
    // 只看 ok 的话这里只会说「已落到画布」，与全部成功一模一样——正是要修掉的：
    // 树已经在画布上了，而它少了几个分支，这句话必须出现在落地时那一行里。
    expect(store.status).toContain('已落到画布');
    expect(store.status).toContain('部分分支未完成');
  });

  it('带 diagnostics 时面板列出诊断 message，超过三条折成计数', async () => {
    const messages = [
      '分支「神经网络」展开失败，已留空',
      '因模型调用额度不足，分支「优化理论」没有展开，已留空',
      '分支「概率图模型」只有 2 个要点，少于要求的 4 个',
      '节点「过拟合」与「泛化误差」重复',
    ];
    stubFetch(() => agentReply([
      {
        type: 'tool_end',
        seq: 1,
        call_id: 'c1',
        name: 'mindmap_generate',
        ok: true,
        latency_ms: 900,
        result_summary: {
          partial: true,
          diagnostics: messages.map((message, index) => ({
            level: 'warn',
            code: `DIAG_${index}`,
            message,
          })),
        },
      },
      doneFrame(content('候选')),
    ]));
    const store = useMindMapStore();
    await store.runGeneration('做一张导图', 'create');

    const wrapper = mount(AgentPanel);
    const text = wrapper.text();
    // 部分完成标记与诊断正文都要出现在候选区上方
    expect(text).toContain('部分完成');
    expect(text).toContain(messages[0]);
    expect(text).toContain(messages[2]);
    // 前三条之后折起来，**但留着一条能走到全部的路**：只写"还有 1 条"等于把后几条锁住
    expect(text).not.toContain(messages[3]);
    const expand = wrapper.get('[data-test="diagnostics-expand"]');
    expect(expand.text()).toContain('还有 1 条');
    expect(expand.text()).toContain('4 条');

    await expand.trigger('click');
    // 展开之后四条全在，按钮变成"收起"
    expect(wrapper.text()).toContain(messages[3]);
    expect(wrapper.get('[data-test="diagnostics-expand"]').text()).toBe('收起');

    await wrapper.get('[data-test="diagnostics-expand"]').trigger('click');
    expect(wrapper.text()).not.toContain(messages[3]);
  });

  it('结果不完整时告警留在面板上，落地之后也不清', async () => {
    stubFetch(() => agentReply([
      {
        type: 'tool_end',
        seq: 1,
        call_id: 'c1',
        name: 'mindmap_generate',
        ok: true,
        result_summary: { partial: true, diagnostics: [] },
      },
      doneFrame(content('候选')),
    ]));
    const store = useMindMapStore();
    await store.runGeneration('做一张导图', 'create');
    expect(store.agentWarnings).toHaveLength(1);

    // 树已经在画布上了，而它少了几个分支——此刻比"落地之前"更需要这句话。
    // 清掉就只剩一句"已落到画布"，用户改到一半才发现某几支是空的。
    expect(store.content?.root.label).toBe('候选');
    expect(store.agentWarnings).toHaveLength(1);
  });

  it('done 帧标了 partial 时，告警用的是后端给的理由', async () => {
    stubFetch(() => agentReply([
      {
        type: 'done',
        seq: 2,
        content: content('半棵树'),
        mode: 'model',
        partial: true,
        partial_reason: '模型调用次数已用完（已执行 6 次工具调用）',
      },
    ]));
    const store = useMindMapStore();

    await store.runGeneration('做一张导图', 'create');

    expect(store.content?.root.label).toBe('半棵树');
    const partial = store.agentWarnings.find(item => item.kind === 'partial');
    // 兜底文案只说"有分支没生成完整"，而后端知道得更具体：是把模型额度用完了。
    // 丢掉这句话，用户就不知道该把目标写小还是该等一会儿再来。
    expect(partial?.message).toContain('模型调用次数已用完');
    expect(store.status).toContain('部分分支未完成');
    // 半棵树不是失败：树已经在画布上了，只是没长完。把它记成 `agentError`
    // 会让面板弹一张"这次没成功"的卡片，而用户眼前明明有一棵能改的树。
    expect(store.agentError).toBe('');
  });

  it('done 帧没带 partial 时不凭空加一条「部分完成」', async () => {
    stubFetch(() => agentReply([doneFrame(content('新图'))]));
    const store = useMindMapStore();

    await store.runGeneration('做一张导图', 'create');

    // 缺字段 = 没说（正常收尾、加这两个字段之前的老帧）。当成"不完整"会让人
    // 以为每次生成都缺分支；这也是这里用 `=== true` 而不是真值判断的原因。
    expect(store.agentWarnings).toHaveLength(0);
    expect(store.status).not.toContain('部分分支未完成');
  });
});

describe('生成落地', () => {
  async function prepare() {
    stubFetch(() => agentReply([doneFrame(content('候选'))]));
    const store = useMindMapStore();
    store.preview(content('原图'), 'right');
    await store.runGeneration('做一张图', 'create');
    return store;
  }

  it('落地只压一步撤销，撤销回到生成之前', async () => {
    const store = await prepare();
    expect(store.content?.root.label).toBe('候选');

    expect(store.canUndo).toBe(true);
    store.undo();
    expect(store.content?.root.label).toBe('原图');
  });

  it('「回到生成之前」在画布还是生成结果时可用，改过之后失效', async () => {
    const store = await prepare();
    expect(store.canUndoGenerate).toBe(true);

    expect(store.undoGenerate()).toBe(true);
    expect(store.content?.root.label).toBe('原图');

    // 已经退回生成前，这一步没有可退的了
    expect(store.canUndoGenerate).toBe(false);
    expect(store.undoGenerate()).toBe(false);
  });

  it('生成之后又改过画布，「回到生成之前」不再自称一步到位', async () => {
    const store = await prepare();
    store.commitLabel('a', '我改的');

    // 画布已经不是生成结果本身了：一次撤销退掉的是上面那次编辑，
    // 把它说成"回到生成前"是骗人。
    expect(store.canUndoGenerate).toBe(false);
    expect(store.undoGenerate()).toBe(false);
    expect(store.content?.root.children[0].label).toBe('我改的');
  });

  it('生成期间改过树：不覆盖，两棵树都留着等用户选', async () => {
    const store = await prepare();

    // prepare 之后再生成一次，中途改树（现在的编辑不再被锁住）
    const reply = controllableReply();
    stubFetch(() => reply.response);
    const running = store.runGeneration('再生成一张', 'create');
    reply.push(doneFrame(content('第二次')));
    // done 到达之前先改一处：这正是"那 25 秒里我顺手改了个标题"
    store.commitLabel('a', '改过的甲');
    reply.finish();
    await running;

    // 画布保持用户的修改，生成结果放进 conflictDraft
    expect(store.content?.root.children[0].label).toBe('改过的甲');
    expect(store.conflictDraft).not.toBeNull();

    expect(store.resolveConflict('generated')).toBe('applied');
    expect(store.content?.root.label).toBe('第二次');
    // 覆盖仍然可撤销：撤销栈顶是"落地之前的画布"，也就是他改过的那一棵，
    // 再撤一步才回到更早的状态。
    store.undo();
    expect(store.content?.root.children[0].label).toBe('改过的甲');
  });

  it('冲突时选择保留我的，生成结果丢掉', async () => {
    const store = await prepare();
    const reply = controllableReply();
    stubFetch(() => reply.response);
    const running = store.runGeneration('再生成一张', 'create');
    reply.push(doneFrame(content('第二次')));
    store.commitLabel('a', '改过的甲');
    reply.finish();
    await running;

    expect(store.resolveConflict('mine')).toBe('kept');
    expect(store.content?.root.children[0].label).toBe('改过的甲');
    expect(store.conflictDraft).toBeNull();
  });

  it('生成期间正在编辑：不打断输入，走同一个裁决', async () => {
    const store = await prepare();
    const reply = controllableReply();
    stubFetch(() => reply.response);
    const running = store.runGeneration('再生成一张', 'create');
    reply.push(doneFrame(content('第二次')));
    store.beginEdit('a');
    reply.finish();
    await running;

    // 手打了一半的标题不能丢：进裁决，用户还能选"保留我的"
    expect(store.conflictDraft).not.toBeNull();
    expect(store.content?.root.label).toBe('候选');
  });

  it('没有冲突时 resolveConflict 什么都不做', () => {
    setActivePinia(createPinia());
    const store = useMindMapStore();
    expect(store.resolveConflict('generated')).toBe('kept');
  });

  it('结果本身不合法时不落画布', async () => {
    // 模型产出是外部数据：label 长度、引用区间、节点数都可能越界。
    // 不在落地之前拦一次，脏数据会先进画布，直到用户点保存才被后端 422
    // 拒绝——那时他已经在这棵树上改了半天。
    const dirty = content('脏候选');
    dirty.root.children[0].label = '超'.repeat(201);
    stubFetch(() => agentReply([doneFrame(dirty)]));
    const store = useMindMapStore();
    store.preview(content('原图'), 'right');

    await store.runGeneration('做一张图', 'create');

    expect(store.content?.root.label).toBe('原图');
    expect(store.generatedSummary).toBeNull();
    expect(store.status).toContain('不合法');
  });

  it('生成中取消：画布一点没变', async () => {
    const store = await prepare();
    const reply = controllableReply();
    stubFetch(() => reply.response);
    const running = store.runGeneration('再生成一张', 'create');
    reply.push(doneFrame(content('第二次')));
    store.cancel();
    reply.finish();
    await running;

    // 取消是真正的"不要了"：结果从未进过画布（draft 只是工作副本）
    expect(store.content?.root.label).toBe('候选');
    expect(store.conflictDraft).toBeNull();
  });
});

describe('生成结果与换图', () => {
  const record = (id: string) => ({ id, version: 1, updated_at: '2024-01-01T00:00:00Z' });

  /** 先在画布上跑完一次生成，返回 store，供各用例接上"换图"这一步。 */
  async function prepared() {
    stubFetch(() => agentReply([doneFrame(content('结果'))]));
    const store = useMindMapStore();
    store.loadMap(content('原图'), record('map-a'));
    await store.runGeneration('做一张图', 'create');
    expect(store.content?.root.label).toBe('结果');
    return store;
  }

  it('落地之后 loadMap 打开另一张图，生成记录被清掉', async () => {
    const store = await prepared();

    store.loadMap(content('另一张'), record('map-b'));

    expect(store.generatedSummary).toBeNull();
    expect(store.generatedChanges).toBeNull();
    expect(store.content?.root.label).toBe('另一张');
    expect(store.mapId).toBe('map-b');
  });

  it('preview 换图同样清掉生成记录', async () => {
    const store = await prepared();

    store.preview(content('示例'), 'right');

    expect(store.generatedSummary).toBeNull();
    expect(store.conflictDraft).toBeNull();
  });

  it('载入期间换了图，上次的结果不装进来', async () => {
    const store = useMindMapStore();
    store.loadMap(content('原图'), record('map-a'));
    store.resumableRun = {
      run_id: 'r1', status: 'done', intent: 'create', goal: '上次那张',
      created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z',
    };
    // 单条详情在途时用户切到了另一张图
    vi.stubGlobal('fetch', vi.fn(async () => {
      store.newMap();
      return jsonReply({
        run_id: 'r1', status: 'done', intent: 'create', goal: '上次那张',
        created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z',
        candidate: content('上次的结果'), failure: null, mode: 'model',
      });
    }));

    await store.resumeRun();

    // 会话号变了，结果属于上一张图：装上去就是把 A 写进 B
    expect(store.content).toBeNull();
    expect(store.status).toContain('换了一张图');
  });

  it('载入一份检查点时，说清它缺的是说明而不是结构', async () => {
    const store = useMindMapStore();
    store.newMap();
    store.resumableRun = {
      run_id: 'cp-1', status: 'failed', intent: 'create', goal: '崩掉那张',
      created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z',
      checkpointed_at: '2024-01-01T00:01:00Z', node_count: 12,
    };
    stubFetch(() => jsonReply({
      run_id: 'cp-1', status: 'failed', intent: 'create', goal: '崩掉那张',
      created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z',
      checkpointed_at: '2024-01-01T00:01:00Z', node_count: 12,
      candidate: content('崩之前长的树'), failure: null, mode: 'model',
    }));

    await store.resumeRun();

    // 树装进来了（结构是完整的）。
    expect(store.content?.root.label).toBe('崩之前长的树');
    const warning = store.agentWarnings.find(item => item.code === 'CHECKPOINT');
    expect(warning?.message).toContain('说明');
    // 而且不能被记成"半棵树"：那会让状态行按"分支没长完"提示。
    expect(store.agentWarnings.some(item => item.kind === 'partial')).toBe(false);
  });
});

describe('撤销 / 重做与生成落地', () => {
  /** 先留一步可撤销的历史，再跑一次「改这张图」：撤销栈跨生成保留。 */
  async function preparedWithHistory() {
    stubFetch(() => agentReply([doneFrame(content('结果'))]));
    const store = useMindMapStore();
    store.preview(content('原图'), 'right');
    // 改一次节点：撤销栈里才有可撤的那一步
    store.commitLabel('a', '改过的甲');
    // 用 `edit` 而不是 `create`：`create` 是一份新作品，落地时会清空历史
    // （只留「生成之前」一步），那一点另有专门用例守着。
    await store.runGeneration('改这张图', 'edit');
    expect(store.content?.root.label).toBe('结果');
    return store;
  }

  it('落地之后撤销一次，退回生成开始之前的那棵树', async () => {
    const store = await preparedWithHistory();

    store.undo();
    // 撤销栈顶是"落地之前的画布"，也就是用户自己改过的那一棵
    expect(store.content?.root.label).toBe('原图');
    expect(store.content?.root.children[0].label).toBe('改过的甲');

    // 再撤一步才退回到他那次编辑之前
    store.undo();
    expect(store.content?.root.children[0].label).toBe('甲');
  });

  it('落地之后重做一次，回到生成结果', async () => {
    const store = await preparedWithHistory();

    // 先撤销再重做，制造出可重做的状态
    store.undo();
    store.redo();
    expect(store.content?.root.label).toBe('结果');
    // 重做把画布换成了一个**新对象**（快照 JSON.parse 出来的），所以"一步回到
    // 生成前"的快捷动作失效——它判的是引用相等，见 `canUndoGenerate`。
    expect(store.canUndoGenerate).toBe(false);
  });

  it('空画布生成落地后，撤销与重做都能回到空白', async () => {
    stubFetch(() => agentReply([doneFrame(content('结果'))]));
    const store = useMindMapStore();
    expect(store.content).toBeNull();

    await store.runGeneration('做一张图', 'create');
    expect(store.content?.root.label).toBe('结果');

    // 撤销回到落地之前的空画布：pushHistoryOrEmpty 压进去的是字符串 'null'
    store.undo();
    expect(store.content).toBeNull();
    expect(store.selectedId).toBeNull();
    expect(store.editingId).toBeNull();

    // 重做回到结果。'null' 解析出来就是 null，不处理这条路径的话
    // 这一步会把「空画布」从撤销栈里弄丢（缺陷 3）
    store.redo();
    expect(store.content?.root.label).toBe('结果');

    // 于是再撤销仍然回得到空白
    store.undo();
    expect(store.content).toBeNull();
  });
});

describe('生成落地之后的身份', () => {
  it('create 意图落地后清空 mapId，撤销只回到落地之前', async () => {
    const store = await openExisting('map-a');
    expect(store.mapId).toBe('map-a');

    stubFetch(() => agentReply([doneFrame(content('新建的图'))]));
    await store.runGeneration('做一张新图', 'create');

    // 新作品不能顶着上一张图的身份：否则下一次保存会走 PUT，把 map-a 整条覆盖
    expect(store.mapId).toBeNull();
    expect(store.version).toBe(0);
    expect(store.savedAt).toBeNull();
    expect(store.mode).toBe('model');
    expect(store.dirty).toBe(true);

    // 撤销栈里只有一步：撤销回到落地之前的画布，再撤也没有了
    expect(store.canUndo).toBe(true);
    store.undo();
    expect(store.content?.root.label).toBe('原图');
    expect(store.canUndo).toBe(false);
  });

  it('edit 意图落地后沿用原记录的身份', async () => {
    const store = await openExisting('map-a');

    stubFetch(() => agentReply([doneFrame(content('改后'))]));
    await store.runGeneration('把根改名', 'edit');

    // 改的就是这张图：保存仍打 map-a 的 PUT，版本还是开图时看到的那个
    expect(store.mapId).toBe('map-a');
    expect(store.version).toBe(3);
    expect(store.savedAt).toBe('2024-01-01T00:00:00Z');
  });
});

describe('snapshot 帧', () => {
  it('snapshot 只更新工作副本，done 才落画布', async () => {
    stubFetch(() => agentReply([
      // 后端整树校验通过后先推一帧 snapshot
      { type: 'snapshot', seq: 1, content: content('快照图') },
      // done 故意不带内容：结果只可能来自上面那一帧 snapshot
      { type: 'done', seq: 2 },
    ]));
    const store = useMindMapStore();
    store.preview(content('原图'), 'right');

    await store.runGeneration('做一张图', 'create');

    // 两帧都在，但落地只发生一次（done 那一刻），所以只压了一步撤销
    expect(store.content?.root.label).toBe('快照图');
    expect(store.generatedSummary?.nodes).toBe(2);
    expect(store.agentEvents.map(event => event.type)).toEqual(['snapshot', 'done']);
    store.undo();
    expect(store.content?.root.label).toBe('原图');
  });
});

describe('来源进入请求体', () => {
  it('来源随请求原样送出，不塞进 goal', async () => {
    // 远超原来 1000 字的截断线：材料必须整份送到，一个字都不在本地裁
    const material = '材料正文'.repeat(400);
    const source: Source = {
      id: 's1',
      kind: 'text',
      filename: null,
      text: material,
      span: null,
      status: 'parsed',
      error: null,
      parsed_pages: null,
      truncated: false,
      failed_pages: [],
    };
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const store = useMindMapStore();

    await store.runGeneration('请基于附带的 1 份材料生成导图', 'create', [source]);

    const body = calls[0].body as Record<string, unknown>;
    const sent = body.sources as Source[];
    expect(sent).toHaveLength(1);
    expect(Array.from(sent[0].text ?? '')).toHaveLength(Array.from(material).length);
    // goal 是"要做什么"，一个字材料正文都不许混进去
    expect(body.goal).toBe('请基于附带的 1 份材料生成导图');
    expect(String(body.goal)).not.toContain('材料正文');
  });

  it('没有来源时请求体不带 sources', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const store = useMindMapStore();

    await store.runGeneration('做一张导图', 'create');

    // 空数组也不发：旧后端对多出来的字段是 extra="forbid"
    expect((calls[0].body as Record<string, unknown>).sources).toBeUndefined();
  });
});

describe('思维模型进入请求体', () => {
  it('选中某一套拆解方式时，请求体带 thinking_model', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const store = useMindMapStore();

    await store.runGeneration('做一张导图', 'create', undefined, 'first-principles');

    const body = calls[0].body as Record<string, unknown>;
    expect(body.thinking_model).toBe('first-principles');
  });

  it('没有指定时请求体里是 default，与后端默认值一致', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const store = useMindMapStore();

    await store.runGeneration('做一张导图', 'create');

    // 两种形态（省略字段 / 显式发 default）选后者并保持一致：排查请求体时
    // 一眼能看出用户选了哪套拆解方式，不用去猜"没这个字段是什么意思"。
    expect((calls[0].body as Record<string, unknown>).thinking_model).toBe('default');
  });
});

describe('静默提示', () => {
  function panel() {
    // 挂整个 App：合并之后状态行在画布右上角那个框里（`App.vue` 的 `stageLine`），
    // `SourceInputs` 只剩材料区，不再渲染生成状态。挂载时 App 会拉偏好与版本号，
    // 按 URL 应答掉，断言只关心状态行那一段文字。
    const mock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/prefs') || url.endsWith('/prefs/suggestions')) return jsonReply([]);
      if (url.endsWith('/health')) return jsonReply({ status: 'ok', version: 'test' });
      return jsonReply({});
    });
    vi.stubGlobal('fetch', mock);
    return mount(App, { global: { plugins: [createPinia()] } });
  }

  it('静默到 10 秒时把阶段文案换成「模型正在思考」，两句话不并存', async () => {
    const wrapper = panel();
    const store = useMindMapStore();
    store.busy = true;
    store.stageText = '正在定框架';
    await wrapper.vm.$nextTick();
    expect(wrapper.text()).toContain('正在定框架');
    expect(wrapper.text()).not.toContain('模型正在思考');

    store.waitedMs = 12;
    await wrapper.vm.$nextTick();
    expect(wrapper.text()).toContain('模型正在思考，已等 12 秒');
    // 阶段文案被顶掉，不是两句叠在一起
    expect(wrapper.text()).not.toContain('正在定框架');
  });

  it('10 秒以内仍显示阶段文案，不额外加括号提示', async () => {
    const wrapper = panel();
    const store = useMindMapStore();
    store.busy = true;
    store.stageText = '正在定框架';
    store.waitedMs = 3;
    await wrapper.vm.$nextTick();

    expect(wrapper.text()).toContain('正在定框架');
    // 后端心跳最小间隔就是 10 秒，`waitedMs < 10` 只在测试里造得出来；
    // 渲染层不为此再留一条永远走不到的分支。
    expect(wrapper.text()).not.toContain('已等待');
    expect(wrapper.text()).not.toContain('模型正在思考');
  });

  it('没有阶段文案时也不因为等待计时凭空多出一行', async () => {
    const wrapper = panel();
    const store = useMindMapStore();
    store.busy = true;
    store.stageText = '';
    store.waitedMs = 0;
    await wrapper.vm.$nextTick();
    expect(wrapper.text()).not.toContain('正在思考');
  });

  it('这一轮结束之后，残留的等待计时不再渲染', async () => {
    // store 会在取消/断流时归零，但渲染不该依赖"没忘"：多一道守卫，
    // 界面就不可能在"已取消。"底下再挂一句"模型正在思考"。
    const wrapper = panel();
    const store = useMindMapStore();
    store.stageText = '正在定框架';
    store.waitedMs = 20;
    await wrapper.vm.$nextTick();

    expect(wrapper.text()).not.toContain('模型正在思考');
    expect(wrapper.text()).not.toContain('正在定框架');
  });
});

describe('面板文案与生成结果', () => {
  /** 把状态铺好再挂载，首帧渲染就带着内容，不用等 nextTick 再断言。 */
  function panel(seed: (store: ReturnType<typeof useMindMapStore>) => void) {
    const store = useMindMapStore();
    seed(store);
    return mount(AgentPanel);
  }

  it('工具进度显示人话与耗时，不显示工具名', () => {
    const wrapper = panel((store) => {
      store.agentEvents = [
        { type: 'tool_start', seq: 1, call_id: 'c1', name: 'mindmap_generate' },
        { type: 'tool_end', seq: 2, call_id: 'c1', name: 'mindmap_generate', ok: true, latency_ms: 4341 },
        { type: 'tool_start', seq: 3, call_id: 'c2', name: 'node_expand' },
      ];
    });

    const text = wrapper.text();
    expect(text).toContain('已定好框架 · 4.3 秒');
    // 进行中的那一条不带耗时
    expect(text).toContain('正在展开分支');
    // 工程名不该露给用户
    expect(text).not.toContain('mindmap_generate');
    expect(text).not.toContain('node_expand');
  });

  it('生成落地后说清规模、没保存、以及怎么退回去', () => {
    const wrapper = panel((store) => {
      store.preview(content('原图'), 'right');
      store.generatedSummary = { nodes: 14, changes: 3 };
    });

    const text = wrapper.text();
    // 三个信息缺一不可：落了多大、改了几处、还没保存
    expect(text).toContain('已落到画布 · 14 个节点 · 改了 3 处');
    expect(text).toContain('还没保存');
    // 没有「应用」这一步了：那句话不该再出现
    expect(text).not.toContain('应用到画布');
    expect(text).not.toContain('候选已就绪');
  });

  it('画布本来是空的时说"整张新图"而不是"改了 0 处"', () => {
    const wrapper = panel((store) => {
      store.generatedSummary = { nodes: 9, changes: null };
    });

    expect(wrapper.text()).toContain('整张新图');
  });

  it('画布还是生成结果时提供「回到生成之前」，点了就退回去', async () => {
    stubFetch(() => agentReply([doneFrame(content('结果'))]));
    const store = useMindMapStore();
    store.preview(content('原图'), 'right');
    await store.runGeneration('做一张导图', 'create');

    const wrapper = mount(AgentPanel);
    const button = wrapper.find('[data-test="undo-generate"]');
    expect(button.exists()).toBe(true);

    await button.trigger('click');
    expect(store.content?.root.label).toBe('原图');
    // 退回去之后这个按钮就该消失：再点一次没有可退的了
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="undo-generate"]').exists()).toBe(false);
  });

  it('生成期间画布被改过：面板摆出两棵树让用户选', async () => {
    stubFetch(() => agentReply([doneFrame(content('结果'))]));
    const store = useMindMapStore();
    store.preview(content('原图'), 'right');
    store.commitLabel('a', '我改的');

    const wrapper = panel((target) => {
      // 落地被判成冲突之后的样子（正常路径上画布被锁，见 store 的冲突判定）
      target.generatedSummary = null;
      target.conflictDraft = { next: content('结果'), meta: { runId: 'r1', intent: 'create', mode: 'model', base: null } };
    });

    const box = wrapper.find('[data-test="generate-conflict"]');
    expect(box.exists()).toBe(true);
    expect(box.text()).toContain('生成期间你改过这张图');
    expect(box.text()).toContain('用模型的结果');
    expect(box.text()).toContain('保留我的');
    expect(store.content?.root.children[0].label).toBe('我改的');
  });

  it('工具失败时在对应进度行下方直接显示 code 与 message', () => {
    const wrapper = panel((store) => {
      store.agentEvents = [
        { type: 'tool_start', seq: 1, call_id: 'c1', name: 'mindmap_generate' },
        {
          type: 'tool_end', seq: 2, call_id: 'c1', name: 'mindmap_generate', ok: false,
          result_summary: {
            code: 'invalid_map', message: '模型返回的导图不合法：引用与原文不匹配',
            diagnostics: [{ code: 'QUOTE_MISMATCH' }], failed_branches: ['分支一'],
          },
        },
      ];
    });

    const rows = wrapper.findAll('ol > li');
    expect(rows).toHaveLength(1);
    // 失败行也要说人话：用户看到的是动作名，不是 `mindmap_generate` 这种函数名。
    expect(rows[0].text()).toContain('生成导图 未成功');
    expect(rows[0].text()).not.toContain('mindmap_generate');
    expect(rows[0].find('p').text()).toBe('invalid_map：模型返回的导图不合法：引用与原文不匹配');
    expect(rows[0].find('p').isVisible()).toBe(true);
  });

  it.each([
    { summary: undefined, expected: '没有给出原因。' },
    { summary: {}, expected: '没有给出原因。' },
    { summary: { diagnostics: [], failed_branches: ['分支一'] }, expected: '没有给出原因。' },
    { summary: { code: 'generation_failed' }, expected: 'generation_failed' },
    { summary: { message: '模型服务返回 HTTP 429' }, expected: '模型服务返回 HTTP 429' },
    { summary: { code: null, message: { detail: '异常对象' } }, expected: '没有给出原因。' },
    { summary: { code: '', message: '  ' }, expected: '没有给出原因。' },
  ])('失败摘要缺字段或字段不可读时仍能渲染：$expected', ({ summary, expected }) => {
    const wrapper = panel((store) => {
      store.agentEvents = [{
        type: 'tool_end', seq: 1, call_id: 'c1', name: 'mindmap_generate', ok: false,
        result_summary: summary,
      }];
    });

    expect(wrapper.find('ol > li > p').text()).toBe(expected);
    expect(wrapper.text()).not.toContain('undefined');
    expect(wrapper.text()).not.toContain('[object Object]');
  });

  it('没见过的工具名原样显示，不硬编一个中文词', () => {
    const wrapper = panel((store) => {
      store.agentEvents = [{
        type: 'tool_end', seq: 1, call_id: 'c1', name: 'brand_new_tool', ok: false,
        result_summary: {},
      }];
    });

    // 翻不出来就照原样写：至少能和后端日志对上，比编一个词强
    expect(wrapper.get('ol > li').text()).toContain('brand_new_tool 未成功');
  });

  it('成功工具不显示失败原因，重试成功不会盖掉之前失败的原因', () => {
    const wrapper = panel((store) => {
      store.agentEvents = [
        {
          type: 'tool_end', seq: 1, call_id: 'c1', name: 'mindmap_generate', ok: false,
          result_summary: { code: 'generation_failed', message: '模型服务返回 HTTP 429' },
        },
        {
          type: 'tool_end', seq: 2, call_id: 'c2', name: 'mindmap_generate', ok: true,
          result_summary: { code: 'ok', message: '已恢复' },
        },
      ];
    });

    const rows = wrapper.findAll('ol > li');
    expect(rows[0].find('p').text()).toContain('模型服务返回 HTTP 429');
    expect(rows[1].find('p').exists()).toBe(false);
  });
});

describe('材料入库后的 id 重映射', () => {
  const readyItem = (id: string) => ({
    key: id,
    id,
    label: '资料.pdf',
    kind: 'document' as const,
    state: 'ready' as const,
    source: { ...textSource('正文'), id, kind: 'document' as const, filename: '资料.pdf' },
    error: '',
    seconds: 0,
  });

  it('收到 sources 帧就把来源条目的 id 换成持久 id', async () => {
    const sourcesStore = useSourcesStore();
    sourcesStore.items = [readyItem('tmp-1')];
    stubFetch(() => agentReply([
      { type: 'sources', seq: 1, mapping: { 'tmp-1': '持久-1' } },
      doneFrame(content()),
    ]));

    await useMindMapStore().runGeneration('整理材料', 'create', sourcesStore.readySources);

    expect(sourcesStore.items[0].id).toBe('持久-1');
    expect(sourcesStore.items[0].source?.id).toBe('持久-1');
    expect(sourcesStore.readySources[0].id).toBe('持久-1');
  });

  it('两份材料认领成同一个持久 id：列表仍是两行，删一行只删一行', async () => {
    const wrapper = mount(SourceInputs, {
      global: { plugins: [createPinia()] },
      props: { confirmDiscard: () => true },
    });
    const sourcesStore = useSourcesStore();
    sourcesStore.items = [readyItem('tmp-a'), readyItem('tmp-b')];
    sourcesStore.remap({ 'tmp-a': '持久-同', 'tmp-b': '持久-同' });
    await wrapper.vm.$nextTick();

    const rows = wrapper.findAll('ul[aria-label="待生成来源"] > li');
    expect(rows).toHaveLength(2);
    // 两行的 id 相同不能当成"重复 key"：行身份挂在 key 上
    expect(sourcesStore.items.map(item => item.key)).toEqual(['tmp-a', 'tmp-b']);

    const removes = rows[0].findAll('button').filter(button => button.text() === '从本轮移除');
    await removes[0].trigger('click');

    expect(wrapper.findAll('ul[aria-label="待生成来源"] > li')).toHaveLength(1);
    expect(sourcesStore.items.map(item => item.key)).toEqual(['tmp-b']);
  });

  it('没有这一帧时来源原样不动', async () => {
    const sourcesStore = useSourcesStore();
    sourcesStore.items = [readyItem('tmp-2')];
    stubFetch(() => agentReply([doneFrame(content())]));

    await useMindMapStore().runGeneration('整理材料', 'create', sourcesStore.readySources);

    expect(sourcesStore.items[0].id).toBe('tmp-2');
  });
});
