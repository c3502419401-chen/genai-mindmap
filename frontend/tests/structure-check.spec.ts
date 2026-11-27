import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { Diagnostic, MindMapContent, Suggestion } from '../src/api/types';
import {
  diagnosticLabel,
  diagnosticLevelOf,
  diagnosticTone,
  normalizeDiagnostics,
  normalizeSuggestions,
  structureCounts,
  structureHint,
  structureSummaryLine,
} from '../src/mindmap/diagnostics';
import { useMindMapStore } from '../src/stores/mindmap';
import { useStructureStore } from '../src/stores/structure';
import { isPrefsRequest, prefsReply } from './helpers/prefs-stub';

/**
 * 结构检查（`/optimization/check` + `/optimization/suggest`）接进界面之后的行为。
 *
 * 这份用例盯的是四件容易悄悄坏掉的事：
 *
 *  1. **后端加一个新诊断码时，界面不改也能显示它**。另一个 agent 正在加码（近义冗余、
 *     缺失环节），所以"未知码渲染成空白"是这里最贵的回归——它不会报错，只会让用户在
 *     一个有问题的图上看到一片空白。
 *  2. **建议绝不自动应用**。只有点「应用」才改图，而且改的那一下要能撤销。
 *  3. **忽略之后不许反复出现**。后端规则是纯函数，重跑得到同一个 message，所以
 *     "忽略"必须靠稳定键挡住它。
 *  4. **自动那一趟只跑 check**。`suggest` 后端正在加模型层，自动跑它等于自动花钱。
 */
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

/** 一棵有两个同名兄弟的树：后端会给 `DUPLICATE_LABEL` + 一条 `dedupe` 建议。 */
const TREE: MindMapContent = {
  title: '机器学习',
  root: {
    id: 'r',
    label: '机器学习',
    children: [
      { id: 'n1', label: '监督学习', children: [] },
      { id: 'n2', label: '无监督学习', children: [] },
      { id: 'n3', label: '监督学习', children: [] },
    ],
  },
};

interface Payload {
  check?: Diagnostic[];
  suggest?: { suggestions?: Suggestion[]; diagnostics?: Diagnostic[] };
}

/** 按 URL 分派的 fetch 替身；同时记下结构检查两个端点各被调了几次。 */
function stubFetch(payload: Payload = {}) {
  const hits = { check: 0, suggest: 0, stream: 0 };
  const mock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input);
    if (isPrefsRequest(url)) return prefsReply(url);
    if (url.endsWith('/health')) return json({ status: 'ok', version: 'test' });
    if (url.includes('/optimization/check')) {
      hits.check += 1;
      return json({ diagnostics: payload.check ?? [] });
    }
    if (url.includes('/optimization/suggest')) {
      hits.suggest += 1;
      return json({
        suggestions: payload.suggest?.suggestions ?? [],
        diagnostics: payload.suggest?.diagnostics ?? [],
      });
    }
    hits.stream += 1;
    return streamReply([{ type: 'done', seq: 2, content: TREE, mode: 'model' }]);
  });
  vi.stubGlobal('fetch', mock);
  return hits;
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

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

const wrappers: VueWrapper[] = [];

function mountApp(): VueWrapper {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(App, { global: { plugins: [pinia] } });
  wrappers.push(wrapper);
  return wrapper;
}

const DUPLICATE: Diagnostic = {
  level: 'warn',
  code: 'DUPLICATE_LABEL',
  node_id: 'n3',
  message: '同级节点「监督学习」与 n1 文字相同，请确认是否重复',
  evidence: 'n1',
};

const DEDUPE: Suggestion = {
  id: 'uuid-per-request',
  kind: 'dedupe',
  node_id: 'n3',
  message: '同级节点「监督学习」与 n1 文字相同，请确认是否重复',
  evidence: 'n1',
};

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount());
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------- 未知码

describe('按 code 通用渲染：后端加码时前端不用改', () => {
  it('前端不认识的 code 仍然渲染出来（显示原码，不是空白）', async () => {
    stubFetch({
      check: [{
        level: 'warn',
        code: 'NEAR_DUPLICATE_BRANCH',
        node_id: 'n2',
        message: '「无监督学习」与「半监督学习」语义近乎重复',
      }],
      suggest: {
        suggestions: [{
          kind: 'near-dedupe',
          node_id: 'n2',
          message: '两个分支意思几乎一样，考虑合并',
        }],
        // 手动路径只打 `/suggest` 一个端点：后端那个 handler 自己会先跑 `check()`，
        // 再来一次 `/check` 只是把同一份结论算两遍（见 `optimization/router.py`）。
        diagnostics: [{
          level: 'warn',
          code: 'NEAR_DUPLICATE_BRANCH',
          node_id: 'n2',
          message: '「无监督学习」与「半监督学习」语义近乎重复',
        }],
      },
    });
    const wrapper = mountApp();
    const structure = useStructureStore();
    const store = useMindMapStore();
    store.preview(structuredClone(TREE), 'right');

    await structure.run({ withSuggest: true });
    await flushPromises();

    // 抽屉里那块必须出现，而且**码本身**要在场：这是用户唯一能对上后端日志的东西。
    const panel = wrapper.get('[data-test="structure-check"]');
    expect(panel.text()).toContain('NEAR_DUPLICATE_BRANCH');
    expect(panel.text()).toContain('语义近乎重复');
    // 未知码的标题就是码本身（`diagnosticLabel` 的 `?? code`），所以它藏不掉；
    // 但它同样能展开出原码那一行，跟已知码走同一条路。
    const toggle = panel.get('[data-test="diagnostic-code-toggle"]');
    expect(panel.find('[data-test="diagnostic-code"]').exists()).toBe(false);
    await toggle.trigger('click');
    expect(panel.get('[data-test="diagnostic-code"]').text()).toBe('NEAR_DUPLICATE_BRANCH');
    // 未知 kind 也照原样显示，并且**没有**「应用」按钮
    expect(panel.text()).toContain('near-dedupe');
    expect(panel.findAll('[data-test="suggestion-apply"]')).toHaveLength(0);
    expect(panel.text()).toContain('这类建议还没有对应的应用动作');
  });

  it('已知 code 给人话，原码默认收起、点开才露出来', async () => {
    stubFetch({ check: [DUPLICATE] });
    const wrapper = mountApp();
    const structure = useStructureStore();
    useMindMapStore().preview(structuredClone(TREE), 'right');

    await structure.run();
    await flushPromises();

    const panel = wrapper.get('[data-test="structure-check"]');
    expect(panel.text()).toContain('同级重名');
    // 默认收起：卡片正文里没有 `DUPLICATE_LABEL`，只有一个能点开的入口
    expect(panel.find('[data-test="diagnostic-code"]').exists()).toBe(false);
    expect(panel.text()).not.toContain('DUPLICATE_LABEL');
    const toggle = panel.get('[data-test="diagnostic-code-toggle"]');
    expect(toggle.attributes('title')).toContain('DUPLICATE_LABEL');

    // 点开之后原码准确到位（不是只给个空壳）
    await toggle.trigger('click');
    expect(panel.get('[data-test="diagnostic-code"]').text()).toBe('DUPLICATE_LABEL');
    // 还能再点回去——收与放两种状态都钉住
    await panel.get('[data-test="diagnostic-code-toggle"]').trigger('click');
    expect(panel.find('[data-test="diagnostic-code"]').exists()).toBe(false);
  });

  it('面板底部的「显示原码」一次展开全部诊断，排障不用逐条点', async () => {
    stubFetch({
      check: [
        DUPLICATE,
        { level: 'warn', code: 'DEPTH_LIMIT', node_id: 'n4', message: '这一支太深了' },
        { level: 'info', code: 'SINGLE_CHILD', node_id: 'n1', message: '节点「监督学习」只有一个子节点' },
      ],
    });
    const wrapper = mountApp();
    const structure = useStructureStore();
    useMindMapStore().preview(structuredClone(TREE), 'right');
    await structure.run();
    await flushPromises();

    const panel = wrapper.get('[data-test="structure-check"]');
    expect(panel.findAll('[data-test="diagnostic-code"]')).toHaveLength(0);
    const all = panel.get('[data-test="diagnostic-code-all"]');
    expect(all.text()).toBe('显示原码');

    await all.trigger('click');
    // 两条 warn 的原码一次全露；info 折在「提示」里，展开后也一起露
    expect(panel.findAll('[data-test="diagnostic-code"]').map(item => item.text()))
      .toEqual(['DUPLICATE_LABEL', 'DEPTH_LIMIT']);
    // 总开关开着时单条的「查看/收起」不出现，收起归总开关管
    expect(panel.find('[data-test="diagnostic-code-toggle"]').exists()).toBe(false);
    await panel.get('[data-test="diagnostic-hints-toggle"]').trigger('click');
    expect(panel.get('[data-test="diagnostic-hint-code"]').text()).toBe('SINGLE_CHILD');
    expect(panel.get('[data-test="diagnostic-code-all"]').text()).toBe('隐藏原码');
    // 关掉之后全部收回去，包括展开过的那条提示
    await panel.get('[data-test="diagnostic-code-all"]').trigger('click');
    expect(panel.findAll('[data-test="diagnostic-code"]')).toHaveLength(0);
    expect(panel.find('[data-test="diagnostic-hint-code"]').exists()).toBe(false);
  });

  it('有动作入口但在别处的 kind：说清怎么去做，而不是"暂不支持"', () => {
    // `expand` / `add_detail` 后端已经定义了，它们的动作是一次模型调用（选中节点后
    // 走工具条），所以这里不给按钮，但也不能撒谎说"没有对应的动作"。
    const [expand] = normalizeSuggestions([{ kind: 'expand', node_id: 'n1', message: '这一支还只有一层' }]);
    expect(expand.applicable).toBe(false);
    expect(expand.blockedReason).toContain('选中那个节点');

    const [merge] = normalizeSuggestions([{ kind: 'merge', node_id: 'n1', message: '层级可以归并' }]);
    expect(merge.blockedReason).toContain('还没有对应的应用动作');

    const [nameless] = normalizeSuggestions([{ kind: 'dedupe', message: '有重复，但没说哪两个' }]);
    expect(nameless.applicable).toBe(false);
    expect(nameless.blockedReason).toContain('没有指明节点');
  });

  it('未知 level 按 info 处理：进折叠区，不冒充「出问题了」', () => {
    expect(diagnosticLevelOf('notice')).toBe('info');
    expect(diagnosticTone(diagnosticLevelOf('notice'))).toBe('slate');
    expect(diagnosticTone(diagnosticLevelOf('warn'))).toBe('amber');
    expect(diagnosticTone(diagnosticLevelOf('error'))).toBe('rose');
  });

  it('码为空时兜底成一句人话，不是空串', () => {
    expect(diagnosticLabel('')).toBe('未知诊断');
    expect(diagnosticLabel('SINGLE_CHILD')).toBe('单子节点');
    expect(diagnosticLabel('BRAND_NEW_CODE')).toBe('BRAND_NEW_CODE');
  });
});

// ---------------------------------------------------------------- 判据

describe('状态行与折叠的判据', () => {
  it('info 不进状态行，只有 warn 与建议进', () => {
    const diagnostics = normalizeDiagnostics([
      { level: 'info', code: 'SINGLE_CHILD', node_id: 'a', message: '只有一个子节点' },
      { level: 'info', code: 'SINGLE_CHILD', node_id: 'b', message: '只有一个子节点' },
      { level: 'warn', code: 'DUPLICATE_LABEL', node_id: 'c', message: '重名' },
    ]);
    const suggestions = normalizeSuggestions([{ kind: 'dedupe', node_id: 'c', message: '重名' }]);
    const counts = structureCounts(diagnostics, suggestions);

    expect(counts).toEqual({ attention: 1, suggestions: 1, hints: 2 });
    // 「2 条提示」一个字都不出现在状态行那半句里
    expect(structureHint(counts)).toBe('1 条建议、1 处要注意');
    // 抽屉里的总结必须把它们说出来，否则收起就成了隐瞒
    expect(structureSummaryLine(counts)).toBe('1 处要注意 · 1 条建议 · 2 条提示');
  });

  it('只有 info 时状态行那半句是空串（不制造一个永不为零的数字）', () => {
    const diagnostics = normalizeDiagnostics([
      { level: 'info', code: 'SINGLE_CHILD', node_id: 'a', message: '只有一个子节点' },
    ]);
    expect(structureHint(structureCounts(diagnostics, []))).toBe('');
  });

  it('稳定键在同一份内容上不变（忽略才能跨过一次重新检查）', () => {
    const first = normalizeSuggestions([DEDUPE])[0];
    // 后端的 id 每次都变，键不能跟着变
    const second = normalizeSuggestions([{ ...DEDUPE, id: 'another-uuid' }])[0];
    expect(first.key).toBe(second.key);
  });
});

// ---------------------------------------------------------------- store

describe('建议的应用与忽略', () => {
  it('「应用」删掉重复节点，而且能撤销', async () => {
    stubFetch({
      check: [DUPLICATE],
      suggest: { suggestions: [DEDUPE], diagnostics: [DUPLICATE] },
    });
    mountApp();
    const structure = useStructureStore();
    const store = useMindMapStore();
    store.preview(structuredClone(TREE), 'right');

    await structure.run({ withSuggest: true });
    expect(structure.visibleSuggestions).toHaveLength(1);

    const note = structure.visibleSuggestions[0];
    expect(note.applicable).toBe(true);
    expect(structure.applySuggestion(note)).toBe(true);
    await flushPromises();

    // 图真的变了：那个同名兄弟不在了
    const ids = (store.content?.root.children ?? []).map(child => child.id);
    expect(ids).toEqual(['n1', 'n2']);
    // 走的是 store 的编辑路径，所以撤销栈里有这一步
    expect(store.canUndo).toBe(true);
    expect(structure.visibleSuggestions).toHaveLength(0);

    store.undo();
    expect((store.content?.root.children ?? []).map(child => child.id)).toEqual(['n1', 'n2', 'n3']);
  });

  it('指向有子节点的建议不给「应用」：删掉会丢那一支', async () => {
    // 后端这一版把 `NEAR_DUPLICATE_LABEL` 也翻成 `dedupe`，而那条诊断的文案是
    // "请确认是不是同一件事"——判断在人手上。若那个节点下面还有内容，一键删除
    // 就是静默丢一支，而按钮上只写着「应用」。
    stubFetch({
      suggest: {
        suggestions: [{ kind: 'dedupe', node_id: 'n2', message: '「无监督学习」与「聚类」只差一个字' }],
      },
    });
    mountApp();
    const structure = useStructureStore();
    const store = useMindMapStore();
    store.preview(structuredClone(TREE), 'right');
    // 给 n2 挂一个子节点，它就不再是叶子
    store.select('n2');
    store.addChildTo('n2');
    const added = store.content?.root.children[1]?.children[0];
    expect(added).toBeTruthy();
    store.commitLabel(added!.id, '聚类');

    await structure.run({ withSuggest: true });
    const note = structure.visibleSuggestions[0];
    expect(note.applicable).toBe(false);
    expect(note.blockedReason).toContain('删掉会丢内容');
    expect(structure.applySuggestion(note)).toBe(false);
    // 树一个节点都没少（r + n1 + n2 + n3 + 新加的那个）
    expect(store.nodeCount).toBe(5);

    // 按钮的显隐只是一层 UI：公开方法被直调时必须自己挡住，而不是指望调用方看过
    // `applicable`。造一个"自称可应用"的 note 再走一次。
    expect(structure.applySuggestion({ ...note, applicable: true })).toBe(false);
    expect(store.status).toContain('要合并请先');
    expect(store.nodeCount).toBe(5);
  });

  it('节点已经不在了的建议不再显示（报告过期时的产物）', async () => {
    stubFetch({ suggest: { suggestions: [DEDUPE] } });
    mountApp();
    const structure = useStructureStore();
    const store = useMindMapStore();
    store.preview(structuredClone(TREE), 'right');

    await structure.run({ withSuggest: true });
    expect(structure.visibleSuggestions).toHaveLength(1);

    // 用户自己先删了那个节点：报告还没重跑，但这条建议已经无处可落
    store.deleteNode('n3');
    expect(structure.suggestions).toHaveLength(1);
    expect(structure.visibleSuggestions).toHaveLength(0);
  });

  it('「忽略」之后它消失，而且重新检查不会再冒出来', async () => {
    stubFetch({
      check: [DUPLICATE],
      suggest: { suggestions: [DEDUPE], diagnostics: [DUPLICATE] },
    });
    mountApp();
    const structure = useStructureStore();
    useMindMapStore().preview(structuredClone(TREE), 'right');

    await structure.run({ withSuggest: true });
    const note = structure.visibleSuggestions[0];
    structure.dismiss(note.key);
    expect(structure.visibleSuggestions).toHaveLength(0);
    // 状态行上的数字跟着减（不是"点进去只剩两条"）
    expect(structure.counts.suggestions).toBe(0);

    // 后端是纯函数：同一份内容重跑会给出同一条建议、同一个 message
    await structure.run({ withSuggest: true });
    expect(structure.suggestions).toHaveLength(1);
    expect(structure.visibleSuggestions).toHaveLength(0);
  });

  it('节点已经不在了：说真话，不假装应用成功', async () => {
    stubFetch({ check: [], suggest: { suggestions: [DEDUPE] } });
    mountApp();
    const structure = useStructureStore();
    const store = useMindMapStore();
    store.preview(structuredClone(TREE), 'right');

    await structure.run({ withSuggest: true });
    const note = structure.visibleSuggestions[0];
    store.deleteNode('n3');

    expect(structure.applySuggestion(note)).toBe(false);
    expect(store.status).toContain('已经不在图里');
  });

  it('鱼骨 / 流程图：不发请求，给一句能读懂的话', async () => {
    const hits = stubFetch();
    mountApp();
    const structure = useStructureStore();
    useMindMapStore().preview({
      title: '鱼骨',
      diagram_type: 'fishbone',
      problem: '为什么报销要 11 天',
      bones: [{ category: '人', root: { id: 'b1', label: '人', children: [] } }],
    }, 'right');

    expect(await structure.run()).toBe('unsupported');
    expect(hits.check).toBe(0);
    expect(structure.error).toContain('只支持思维导图');
  });
});

// ---------------------------------------------------------------- 组件

describe('抽屉里的那块面板', () => {
  it('info 默认折叠，点开才逐条列出', async () => {
    stubFetch({
      check: [
        { level: 'info', code: 'SINGLE_CHILD', node_id: 'n1', message: '节点「监督学习」只有一个子节点' },
        { level: 'info', code: 'SINGLE_CHILD', node_id: 'n2', message: '节点「无监督学习」只有一个子节点' },
      ],
    });
    const wrapper = mountApp();
    const structure = useStructureStore();
    useMindMapStore().preview(structuredClone(TREE), 'right');
    await structure.run();
    await flushPromises();

    // 两条都是 info：没有 warn 列表，只有一个折叠按钮
    expect(wrapper.find('[data-test="diagnostic-list"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="diagnostic-hint-item"]').exists()).toBe(false);
    const toggle = wrapper.get('[data-test="diagnostic-hints-toggle"]');
    expect(toggle.text()).toContain('展开 2 条提示');

    await toggle.trigger('click');
    expect(wrapper.findAll('[data-test="diagnostic-hint-item"]')).toHaveLength(2);
    expect(wrapper.get('[data-test="diagnostic-hints-toggle"]').text()).toBe('收起提示');
  });

  it('诊断多于 6 条时给一条能走到全部的出路（不把后面几条锁起来）', async () => {
    // 实测的规模：库里那张 561 节点的图跑一次 check 有 45 条 DUPLICATE_LABEL。
    const many: Diagnostic[] = Array.from({ length: 8 }, (_, index) => ({
      level: 'warn',
      code: 'DUPLICATE_LABEL',
      node_id: `d${index}`,
      message: `第 ${index} 条诊断`,
    }));
    stubFetch({ check: many });
    const wrapper = mountApp();
    const structure = useStructureStore();
    useMindMapStore().preview(structuredClone(TREE), 'right');
    await structure.run();
    await flushPromises();

    expect(wrapper.findAll('[data-test="diagnostic-item"]')).toHaveLength(6);
    const expand = wrapper.get('[data-test="diagnostic-expand"]');
    expect(expand.text()).toContain('展开全部 8 条诊断（还有 2 条）');

    await expand.trigger('click');
    expect(wrapper.findAll('[data-test="diagnostic-item"]')).toHaveLength(8);
    expect(wrapper.get('[data-test="diagnostic-expand"]').text()).toBe('收起诊断');
  });

  it('图改过之后报告标成过期，而不是悄悄按旧结论说话', async () => {
    stubFetch({ check: [DUPLICATE] });
    const wrapper = mountApp();
    const structure = useStructureStore();
    const store = useMindMapStore();
    store.preview(structuredClone(TREE), 'right');
    await structure.run();
    await flushPromises();
    expect(wrapper.find('[data-test="structure-stale"]').exists()).toBe(false);

    store.commitLabel('n2', '半监督学习');
    await wrapper.vm.$nextTick();
    expect(wrapper.get('[data-test="structure-stale"]').text()).toContain('又改过');
  });

  it('检查没跑成时只留一句提示，不改任何既有结论', async () => {
    stubFetch();
    const wrapper = mountApp();
    const structure = useStructureStore();
    useMindMapStore().preview(structuredClone(TREE), 'right');
    await structure.run();
    await flushPromises();

    vi.stubGlobal('fetch', vi.fn(async () => new Response('boom', { status: 500 })));
    expect(await structure.run()).toBe('failed');
    await flushPromises();

    expect(wrapper.get('[data-test="structure-error"]').text()).toContain('请求失败');
  });
});

// ---------------------------------------------------------------- 名字与请求
//
// 「界面上叫什么」与「实际打什么」必须对得上。这一条是**判定的钉子**，不是回归：
// 手动那一下打的只有 `/optimization/suggest`，而它的响应对里本来就带
// `diagnostics = check(content, sources)`。所以按钮叫「检查结构」是**低报**——用户
// 点完看到的是问题 + 建议 + 补关系 + 引用核验，名字只说了第一样。改叫「检查与建议」。

describe('入口文案与实际请求对得上', () => {
  it('「检查与建议」只打一次 /suggest，不再补一次 /check', async () => {
    const hits = stubFetch({
      check: [DUPLICATE],
      suggest: { suggestions: [DEDUPE], diagnostics: [DUPLICATE] },
    });
    const wrapper = mountApp();
    useMindMapStore().preview(structuredClone(TREE), 'right');
    await flushPromises();

    const entry = wrapper.get('[data-test="structure-check-run"]');
    expect(entry.text()).toBe('检查与建议');

    await entry.trigger('click');
    await flushPromises();

    expect(hits.suggest).toBe(1);
    // 这一条是那个判定的可执行版本：suggest 的响应里已经带着 check 的结论，
    // 再打一次 `/check` 等于同一份东西算两遍（带材料时还要多传一遍全文）。
    expect(hits.check).toBe(0);
    // 而且那份结论真的落到了面板上——否则"只发一个请求"可以靠"什么都没做"通过。
    expect(wrapper.find('[data-test="suggestion-apply"]').exists()).toBe(true);
    expect(wrapper.get('[data-test="structure-summary"]').text()).toContain('1 条建议');
  });

  it('两个入口同一个名字：侧栏那块面板的标题也读「检查与建议」', async () => {
    stubFetch();
    const wrapper = mountApp();
    useMindMapStore().preview(structuredClone(TREE), 'right');
    await flushPromises();

    const panel = wrapper.get('[data-test="structure-check"]');
    expect(panel.attributes('aria-label')).toBe('检查与建议');
    expect(wrapper.get('[data-test="structure-run"]').text()).toBe('检查与建议');
    // 说明里写的也是这个名字：抄错一次，用户就会去找一个不存在的按钮。
    expect(wrapper.get('[data-test="structure-empty"]').text()).toContain('点「检查与建议」');
  });
});

// ---------------------------------------------------------------- 自动那一趟

describe('生成落地之后自动跑一次检查', () => {
  it('只跑 check（不跑 suggest），并把结果并进状态行', async () => {
    const hits = stubFetch({
      check: [DUPLICATE],
      suggest: { suggestions: [DEDUPE] },
    });
    const wrapper = mountApp();
    await wrapper.get('textarea#intent').setValue('机器学习');
    await wrapper.get('[data-test="intent-box"] button').trigger('click');
    await flushPromises();

    expect(hits.stream).toBe(1);
    // 自动那一趟**只**是 check：suggest 后端正在加模型层，自动跑它等于自动花钱
    expect(hits.check).toBe(1);
    expect(hits.suggest).toBe(0);

    const status = wrapper.get('[data-test="run-status-text"]').text();
    expect(status).toContain('已落到画布');
    expect(status).toContain('1 处要注意');
  });

  it('一轮新的生成开始：上一份报告作废（它说的是上一棵树）', async () => {
    stubFetch({ check: [DUPLICATE] });
    const wrapper = mountApp();
    const structure = useStructureStore();
    const store = useMindMapStore();

    await wrapper.get('textarea#intent').setValue('机器学习');
    await wrapper.get('[data-test="intent-box"] button').trigger('click');
    await flushPromises();
    expect(structure.visibleDiagnostics.length).toBeGreaterThan(0);

    store.newMap();
    await wrapper.vm.$nextTick();
    expect(structure.visibleDiagnostics).toHaveLength(0);
    expect(structure.neverRun).toBe(true);
  });
});
