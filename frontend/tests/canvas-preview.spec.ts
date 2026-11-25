import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MindMapContent } from '../src/api/types';
import MindNode from '../src/canvas/MindNode.vue';
import MindMapCanvas from '../src/canvas/MindMapCanvas.vue';
import {
  PREVIEW_ID_PREFIX,
  previewNodeId,
  useMindMapStore,
  type MindNodeData,
} from '../src/stores/mindmap';

/**
 * 画布的渲染源。
 *
 * 用户的抱怨是「节点一个个到达，但画布中间始终是空的」：预览只长在右侧面板里。
 * 这组用例守的是画布的取数规则——两级优先级 content → agentPreview，
 * 以及「看得见」与「已经在画布上」这两件事始终分得开。
 */

/** 手动推进的 SSE 响应：生成中的状态只在流没结束时存在。 */
function controllableReply() {
  const encoder = new TextEncoder();
  let push: (frame: unknown) => void = () => {};
  let finish: () => void = () => {};
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      push = frame => { controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`)); };
      finish = () => {
        controller.enqueue(encoder.encode('data: [DONE]\n\n'));
        controller.close();
      };
    },
  });
  const response = { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
  return { response, push, finish };
}

function grow(path: number[] | null, label: string, expandable: boolean | null = null) {
  return { type: 'node_grow', seq: 1, path, label, expandable };
}

/** 一份"已应用"的内容：id 是真 id，和预览的派生 id 不会撞。 */
function content(label: string): MindMapContent {
  return {
    title: '示例',
    root: { id: 'root', label, children: [{ id: 'a', label: '甲', children: [] }] },
  };
}

function startStream(goal = '做一张导图') {
  const stream = controllableReply();
  vi.stubGlobal('fetch', vi.fn(async () => stream.response));
  const store = useMindMapStore();
  const running = store.runGeneration(goal, 'create');
  return { stream, store, running };
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('预览节点的 id', () => {
  it('由路径派生，同一棵树永远得到同一套 id', () => {
    expect(previewNodeId([])).toBe('preview-root');
    expect(previewNodeId([0])).toBe('preview-0');
    expect(previewNodeId([0, 2, 1])).toBe('preview-0-2-1');
    // 同一个路径算两次必须一致：不一致的话布局缓存和 Vue 的 key 都会跟着抖
    expect(previewNodeId([0, 2, 1])).toBe(previewNodeId([0, 2, 1]));
  });

  it('不同路径不会撞车，且都带前缀', () => {
    const paths = [[], [0], [0, 0], [0, 1], [0, 1, 0], [1], [1, 0], [10]];
    const ids = paths.map(path => previewNodeId(path));
    expect(new Set(ids).size).toBe(paths.length);
    expect(ids.every(id => id.startsWith(PREVIEW_ID_PREFIX))).toBe(true);
    // 10 与 [1,0] 是两条不同的路径，不能被拼成同一个字符串
    expect(previewNodeId([10])).not.toBe(previewNodeId([1, 0]));
  });
});

describe('渲染源：content 为空时画布画预览', () => {
  it('预览边到边长，画布上的节点跟着多起来', async () => {
    const { stream, store, running } = startStream();
    expect(store.nodes).toHaveLength(0);

    stream.push(grow(null, '机器学习'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(1));
    expect(store.nodes[0].id).toBe(previewNodeId([]));
    expect(store.nodes[0].data?.label).toBe('机器学习');

    stream.push(grow([], '监督学习'));
    stream.push(grow([0], '线性回归'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(3));

    expect(store.nodes.map(node => node.id)).toEqual([
      previewNodeId([]),
      previewNodeId([0]),
      previewNodeId([0, 0]),
    ]);
    // 连线也得跟着长，否则画布上是一堆孤立的框
    expect(store.edges).toHaveLength(2);
    expect(store.layout?.positions.size).toBe(3);

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
  });

  it('增量到达时已有节点的 id 不变', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '根'));
    stream.push(grow([], '一'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(2));
    const before = store.nodes.map(node => node.id);

    // 每来一帧整棵树都会被重新派生一次；旧节点的 id 必须原样保留，
    // 否则 Vue Flow 会认为这是新节点，元素重建、视图跟着跳。
    stream.push(grow([], '二'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(3));
    expect(store.nodes.slice(0, before.length).map(node => node.id)).toEqual(before);

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
  });

  it('同一棵树重放一次，得到完全相同的 id', async () => {
    const frames = [grow(null, '根'), grow([], '甲'), grow([0], '甲的孩'), grow([], '乙')];

    const first = startStream('第一轮');
    for (const frame of frames) first.stream.push(frame);
    await vi.waitFor(() => expect(first.store.nodes).toHaveLength(4));
    const firstIds = first.store.nodes.map(node => node.id);
    first.store.cancel();
    first.stream.finish();
    await first.running;

    const second = startStream('第二轮');
    for (const frame of frames) second.stream.push(frame);
    await vi.waitFor(() => expect(second.store.nodes).toHaveLength(4));
    expect(second.store.nodes.map(node => node.id)).toEqual(firstIds);

    second.stream.push({ type: 'done', seq: 9 });
    second.stream.finish();
    await second.running;
  });
});

describe('渲染源：content 优先', () => {
  it('已有内容时预览不顶掉它', async () => {
    const store = useMindMapStore();
    store.preview(content('旧图'), 'right');
    const applied = store.nodes.map(node => node.id);
    expect(applied).toEqual(['root', 'a']);

    const { stream, running } = startStream();
    stream.push(grow(null, '新生成'));
    stream.push(grow([], '新分支'));
    await vi.waitFor(() => expect(store.agentPreview?.children).toHaveLength(1));

    // 用户已经打开了一张图：让没定稿的预览顶上去，看起来就是"我的图被改了"
    expect(store.nodes.map(node => node.id)).toEqual(applied);
    expect(store.nodes.every(node => !node.id.startsWith(PREVIEW_ID_PREFIX))).toBe(true);
    expect(store.nodes.every(node => node.selectable === true)).toBe(true);

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
  });

  it('已有内容时 snapshot 不顶掉它，done 才替换', async () => {
    const store = useMindMapStore();
    store.preview(content('旧图'), 'right');

    const { stream, running } = startStream();
    stream.push({ type: 'snapshot', seq: 3, content: content('结果根') });
    await vi.waitFor(() => expect(store.agentEvents.length).toBeGreaterThan(0));

    // snapshot 只更新工作副本：画布上是用户打开的那张图
    expect(store.nodes.map(node => node.id)).toEqual(['root', 'a']);
    expect(store.nodes[0].data?.label).toBe('旧图');

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
    expect(store.nodes[0].data?.label).toBe('结果根');
  });
});

describe('渲染源：生成中画预览，done 之后画 content', () => {
  it('snapshot 到达时画布不空一帧：预览一直撑到 done', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '预览根'));
    stream.push(grow([], '预览分支'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(2));
    expect(store.nodes[0].id.startsWith(PREVIEW_ID_PREFIX)).toBe(true);

    stream.push({ type: 'snapshot', seq: 8, content: content('快照根') });
    await vi.waitFor(() => expect(store.agentEvents.some(item => item.type === 'snapshot')).toBe(true));

    // 这一帧从前由候选层接住，候选层删掉之后由"预览继续撑"接住：
    // 画布这一瞬间**不能**掉回空态，那正是用户抱怨过的那一帧。
    expect(store.agentPreview).not.toBeNull();
    expect(store.nodes.length).toBeGreaterThan(0);
    // 但预览节点仍然不可选：它还没经过整树校验，没资格进画布
    expect(store.nodes.every(node => node.selectable === false)).toBe(true);

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;

    // done 之后画的是真正落地的 content：可选、可编辑、没有草稿标记
    expect(store.nodes.map(node => node.id)).toEqual(['root', 'a']);
    expect(store.nodes[0].data?.label).toBe('快照根');
    expect(store.nodes.every(node => node.selectable === true)).toBe(true);
    expect(store.nodes.every(node => node.data?.draft === undefined)).toBe(true);
  });

  it('落地之后画布回到正常节点，且只压一步撤销', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '预览根'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(1));

    stream.push({ type: 'snapshot', seq: 8, content: content('快照根') });
    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;

    expect(store.content?.root.label).toBe('快照根');
    expect(store.undoDepth).toBe(1);
    expect(store.nodes.map(node => node.id)).toEqual(['root', 'a']);
    expect(store.nodes.every(node => node.selectable === true)).toBe(true);
  });
});

describe('预览不能编辑', () => {
  it('预览节点不可选中，选中请求被忽略', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '根'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(1));

    const id = store.nodes[0].id;
    expect(store.nodes[0].selectable).toBe(false);

    store.select(id);
    expect(store.selectedId).toBeNull();
    expect(store.nodes[0].selected).toBe(false);
    // 详情面板据此显示"还没选中"，而不是对着一个不存在的节点说话
    expect(store.selectedNode).toBeNull();
    expect(store.breadcrumb).toEqual([]);

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
  });

  it('对预览 id 调编辑入口不会改到任何数据', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '根'));
    stream.push(grow([], '分支'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(2));
    const id = store.nodes[1].id;

    store.addChildTo(id);
    store.addSiblingTo(id);
    store.deleteNode(id);
    store.beginEdit(id);
    store.commitLabel(id, '改掉它');

    // content 一直是空的，编辑入口全部早退：预览没有被"编辑"变成真的树
    expect(store.content).toBeNull();
    expect(store.agentPreview?.children[0].label).toBe('分支');
    expect(store.nodeCount).toBe(2);
    expect(store.canUndo).toBe(false);
    expect(store.selectedId).toBeNull();
    expect(store.editingId).toBeNull();

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
  });

  it('只有预览时画布上那棵树一个字都没进 content', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '预览根'));
    stream.push(grow([], '预览分支'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(2));

    expect(store.content).toBeNull();
    // 画布上已经看得见这棵树，但它既不是 content 也不是"一次生成的结果"——
    // 它只是还在长的预览，`done` 一到就会被整树替换。
    expect(store.nodeCount).toBe(2);
    expect(store.generatedSummary).toBeNull();
    expect(store.canUndo).toBe(false);

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
  });
});

describe('预览清空后画布回到空态', () => {
  it('error 帧清掉预览，画布不留下半截树', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '根'));
    stream.push(grow([], '分支'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(2));

    stream.push({ type: 'error', seq: 8, code: 'generation_failed', message: '模型服务不可用' });
    await vi.waitFor(() => expect(store.agentPreview).toBeNull());

    expect(store.nodes).toEqual([]);
    expect(store.edges).toEqual([]);
    expect(store.nodeCount).toBe(0);
    expect(store.layout).toBeNull();

    stream.finish();
    await running;
  });

  it('取消之后同样回到空态，不留预览残影', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '根'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(1));

    store.cancel();
    expect(store.nodes).toEqual([]);
    expect(store.nodeCount).toBe(0);

    stream.finish();
    await running;
  });
});

/**
 * 画布 DOM：生成过程中节点真的长在画布上。
 *
 * ## 判 C：这里只需要画布，不需要整个应用壳
 *
 * 断言读的全在 `MindMapCanvas` 里：空态文案「还没有导图」是它模板里的
 * （`MindMapCanvas.vue` 那一句），`.vue-flow__node` 是它体内的 Vue Flow 渲出来的，
 * `done` 之后换树也发生在它体内。右侧面板在这条用例里只是名字上的反例——
 * 一个断言都没读它。
 *
 * 从前 `mount(App)` 为了这三样把 `ChatDock` / `SettingsPanel` / `HistoryDrawer` /
 * `StructureCheck` / `AgentPanel` 一起挂起来。同一进程、同一时刻的对照（三份 3 次取均值，
 * `docs/TEST-COST-3-CASES.md` §2）：挂 App 壳 869ms，只挂画布 385ms，只挂画布+密轮询
 * 318ms——`mount` 一项 318ms → 98ms。"App 壳里画布确实被装上了"由 `smoke-canvas.spec`
 * 与另外 24 个 `mount(App)` 的 spec 守着，不靠这一条。
 *
 * ## 剩下的慢是 A（真画布），所以给上限
 *
 * C 削掉的是 App 壳那一层；剩下的 3 秒是"真挂 `MindMapCanvas`"——Vue Flow 建图、
 * `MindNode` 真渲染、四次 `vi.waitFor` 轮询等流帧。那正是这条用例要证明的东西
 * （"画布上真的长出节点"），不能 stub 掉。数字：本地加压（两份全量 vitest 同时跑）
 * 最坏 3593ms、CI 估 ≈ ×1.3–2 = 4.7–7.2s，所以给 10 秒（= 本地加压 2.8×、
 * CI 估的 1.4–2.1×）。
 */
describe('画布 DOM', { timeout: 10_000 }, () => {
  /** jsdom 没有 ResizeObserver，真实 Vue Flow 挂载需要一个空壳。 */
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }

  /**
   * jsdom 也没有 DOMMatrixReadOnly，而 Vue Flow 量节点尺寸时要从视口变换里读缩放。
   * 补一个只提供 m22 的桩：jsdom 里元素尺寸恒为 0，量不出变化，后续分支不会走到。
   */
  class DOMMatrixReadOnlyStub {
    m22 = 1;
    constructor(_transform?: string) {}
  }

  it('生成过程中画布上真的长出节点，而不是只有右侧面板在动', async () => {
    vi.stubGlobal('ResizeObserver', ResizeObserverStub);
    vi.stubGlobal('DOMMatrixReadOnly', DOMMatrixReadOnlyStub);

    const pinia = createPinia();
    setActivePinia(pinia);
    const wrapper = mount(MindMapCanvas, { global: { plugins: [pinia] }, attachTo: document.body });
    const store = useMindMapStore();

    expect(store.nodes).toHaveLength(0);
    expect(wrapper.text()).toContain('还没有导图');

    const stream = controllableReply();
    vi.stubGlobal('fetch', vi.fn(async () => stream.response));
    const running = store.runGeneration('做一张导图', 'create');

    stream.push(grow(null, '机器学习'));
    stream.push(grow([], '监督学习'));

    await vi.waitFor(() => expect(wrapper.findAll('.vue-flow__node')).toHaveLength(2));
    // 有节点之后占位文案必须让位，否则它会盖在刚长出来的树上
    expect(wrapper.text()).not.toContain('还没有导图');

    // 快照到达：预览继续撑住画布等 done，节点数不能掉到 0
    stream.push({ type: 'snapshot', seq: 8, content: content('快照根') });
    await vi.waitFor(() => expect(store.agentEvents.some(item => item.type === 'snapshot')).toBe(true));
    await vi.waitFor(() => expect(wrapper.findAll('.vue-flow__node')).toHaveLength(2));
    expect(wrapper.text()).not.toContain('还没有导图');

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
    // done 之后画的是真正落地的树，节点数同样不能掉到 0（那一瞬间最刺眼）
    expect(wrapper.findAll('.vue-flow__node')).toHaveLength(2);
    wrapper.unmount();
  });
});

describe('节点样式', () => {
  function nodeData(over: Partial<MindNodeData> = {}): MindNodeData {
    return { label: '节点', body: '节点', fullLabel: '节点', depth: 1, out: [], in: [], ...over };
  }

  function box(data: MindNodeData, selected = false) {
    const wrapper = mount(MindNode, { props: { id: 'x', data, selected } });
    const hit = wrapper.find('div.rounded-md.border');
    if (!hit.exists()) throw new Error('没找到节点框');
    return { wrapper, box: hit };
  }

  it('已应用的节点样式不受新增字段影响', () => {
    const child = box(nodeData()).box;
    expect(child.classes()).toContain('border-slate-300');
    expect(child.classes()).toContain('text-slate-700');
    expect(child.classes()).not.toContain('border-dashed');
    expect(child.classes().some(name => name.startsWith('opacity-'))).toBe(false);

    const root = box(nodeData({ depth: 0 })).box;
    expect(root.classes()).toContain('bg-slate-900');

    const selected = box(nodeData(), true).box;
    expect(selected.classes()).toContain('border-slate-900');
    expect(selected.classes()).toContain('shadow-md');
  });

  it('预览节点是虚线的草稿，明确不展开的再弱一档', () => {
    const draft = box(nodeData({ draft: 'preview' })).box;
    expect(draft.classes()).toContain('border-dashed');
    expect(draft.classes()).toContain('opacity-60');

    const leaf = box(nodeData({ draft: 'preview', previewLeaf: true })).box;
    expect(leaf.classes()).toContain('border-dashed');
    expect(leaf.classes()).toContain('opacity-40');

    // 根节点也不走深色：预览的根还没被采纳，长得像正式根只会让人以为图已经生成了
    const root = box(nodeData({ draft: 'preview', depth: 0 })).box;
    expect(root.classes()).not.toContain('bg-slate-900');
    expect(root.classes()).toContain('border-dashed');
  });

  it('生成改过的节点描一圈琥珀色：画布整棵换过，用户得看得出哪里动了', () => {
    const changed = box(nodeData({ changed: true })).box;
    expect(changed.classes()).toContain('border-amber-500');
    expect(changed.classes()).toContain('ring-amber-300');
    // 不是删除/新增那一档的红绿，也不是"还在长"的虚线
    expect(changed.classes()).not.toContain('border-dashed');
  });
});
