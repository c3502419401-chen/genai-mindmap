import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { REAL_TREES } from './fixtures/real-trees';
import type { MapRecord, MindMapContent } from '../src/api/types';
import HistoryDrawer from '../src/components/HistoryDrawer.vue';
import SourceInputs from '../src/components/SourceInputs.vue';
import { useMindMapStore } from '../src/stores/mindmap';
import { useSourcesStore } from '../src/stores/sources';

/**
 * **写用户手册时撞到的缺陷，逐条变成回归。**
 *
 * 三条共同的成因是"测试测的是函数、不是用户路径"：删导图这个动作在测试里从来没出现
 * 过（所以 `DELETE /api/mindmaps/{id}` 405 了很久也没人知道），于是它们不产生任何
 * 红灯形态，只有照着界面走一遍才会撞到。
 *
 * 这个文件按缺陷分块，一块对应一笔修复：本文件先只有第一块。
 */

const record = (id: string, title = '示例'): MapRecord => ({
  id,
  version: 1,
  content: { title, root: { id: 'root', label: title, children: [] } },
  updated_at: '2026-11-26T10:00:00Z',
});

interface Call {
  url: string;
  method: string;
}

let calls: Call[] = [];

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

function stubFetch(handler: (call: Call) => Response | Promise<Response>) {
  calls = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const call: Call = { url: String(input), method: init?.method ?? 'GET' };
      calls.push(call);
      return handler(call);
    }),
  );
}

function mountDrawer() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(HistoryDrawer, {
    props: {
      open: true,
      confirmDiscard: () => true,
      exporting: false,
      exportChoice: '',
      saveHint: '',
      versionLabel: '',
      modeLabel: '',
      modeTone: '',
      nodeCount: 0,
      sideOpen: false,
      samples: [],
    },
    // 三个子组件各自会发请求；这一块只关心历史列表那一截。
    global: {
      plugins: [pinia],
      stubs: { AuthPanel: true, PrefsPanel: true, ImportMindmap: true },
    },
  });
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('缺陷一：删除导图', () => {
  it('抽屉列表里两段式删除：第一下只进确认态，第二下才发 DELETE', async () => {
    const wrapper = mountDrawer();
    const store = useMindMapStore();
    store.maps = [record('m1', '校园垃圾分类推行方案')];
    stubFetch(() => jsonResponse(undefined, 204));
    await wrapper.vm.$nextTick();

    await wrapper.find('[data-test="drawer-map-delete"]').trigger('click');

    // 第一下只是个状态：不发请求，且理由写在行内（不可恢复）。
    expect(calls).toHaveLength(0);
    expect(wrapper.find('[data-test="drawer-map-delete-hint"]').text()).toContain('不可恢复');

    await wrapper.find('[data-test="drawer-map-delete-confirm"]').trigger('click');
    await flushPromises();

    expect(calls).toEqual([{ url: '/api/mindmaps/m1', method: 'DELETE' }]);
    expect(store.maps).toHaveLength(0);
    expect(wrapper.text()).not.toContain('校园垃圾分类推行方案');
  });

  it('取消之后什么都没发生', async () => {
    const wrapper = mountDrawer();
    const store = useMindMapStore();
    store.maps = [record('m1')];
    stubFetch(() => jsonResponse(undefined, 204));
    await wrapper.vm.$nextTick();

    await wrapper.find('[data-test="drawer-map-delete"]').trigger('click');
    await wrapper.find('[data-test="drawer-map-delete-cancel"]').trigger('click');

    expect(calls).toHaveLength(0);
    expect(store.maps).toHaveLength(1);
    // 回到"可以再删"的状态，而不是停在一个再也点不到的确认态。
    expect(wrapper.find('[data-test="drawer-map-delete"]').exists()).toBe(true);
  });
});

describe('缺陷二：移除与删除并排', () => {
  it('两个动作的文案一眼能分清，并且各有一句就地说明', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const sources = useSourcesStore();
    const wrapper = mount(SourceInputs, { global: { plugins: [pinia] } });

    sources.addText('一份传错的正文');
    const key = sources.items[0].key;
    // 生成过一次 → 后端把临时 id 换成库里的持久 id，从此这一行才有"彻底删除"。
    sources.remap({ [key]: 'lib-1' });
    await wrapper.vm.$nextTick();

    const labels = wrapper.findAll('button').map(button => button.text());
    // 两个词不再共用一个"把这个东西拿掉"的语感：一个带范围（本轮），一个带后果（彻底）。
    expect(labels).toContain('从本轮移除');
    expect(labels).toContain('彻底删除');
    expect(labels).not.toContain('移除');
    expect(labels).not.toContain('删除');
    // 就地说明（不是"写在手册里"）：两个词各指什么，就在列表上方。
    const legend = wrapper.find('[data-test="source-actions-legend"]');
    expect(legend.exists()).toBe(true);
    expect(legend.text()).toContain('从本轮移除');
    expect(legend.text()).toContain('彻底删除');
  });

  it('被引用时点第一次：按钮变成「确认彻底删除」，并说明再点一次就不可恢复', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    stubFetch(() => jsonResponse({ code: 'source_cited', message: '还被 1 张导图引用' }, 409));
    const sources = useSourcesStore();
    const wrapper = mount(SourceInputs, { global: { plugins: [pinia] } });
    sources.addText('被引用的正文');
    const key = sources.items[0].key;
    sources.remap({ [key]: 'lib-1' });
    await wrapper.vm.$nextTick();

    const target = wrapper.findAll('button').find(button => button.text() === '彻底删除');
    expect(target, '要有「彻底删除」这个入口').toBeTruthy();
    await target!.trigger('click');
    await flushPromises();

    // 第一次点击的结果必须是**可见的**（这正是"要点两次"曾经的问题：用户看不出
    // 第一次点了个寂寞）。
    const labels = wrapper.findAll('button').map(button => button.text());
    expect(labels).toContain('确认彻底删除');
    expect(wrapper.text()).toContain('再点一次即永久删除');
  });
});

describe('缺陷三：拖到第 2 层节点下面', () => {
  it('在预算截断的树上，落点算得出来并且有幽灵框', () => {
    const store = useMindMapStore();
    store.preview(REAL_TREES.tree136 as MindMapContent, 'right');
    // 默认预算 40 / 136 个节点：画布只画前 40 个，被拖节点与目标都在这一屏里。
    expect(store.visibleNodeLimit).toBe(40);

    // 找一对结构合法的"挂到第 2 层节点下面"：`descendant`（目标在自己这一支里）
    // 是**正确拒绝**，不属于这个缺陷；规模由下一条穷举用例钉住。
    let intent: { draggedId: string; targetId: string; zone: 'child' } | null = null;
    outer: for (const dragged of store.nodes) {
      if (dragged.data?.depth !== 1) continue;
      for (const target of store.nodes) {
        if (target.id === dragged.id || (target.data?.depth ?? 0) !== 2) continue;
        const probe = { draggedId: dragged.id, targetId: target.id, zone: 'child' as const };
        if (store.previewMove(probe).ok) {
          intent = probe;
          break outer;
        }
      }
    }
    expect(intent, '这一屏里要有一对能挂进去的').not.toBeNull();
    const chosen = intent!;

    const preview = store.previewMove(chosen);

    expect(preview.code).not.toBe('missing');
    expect(preview.ok).toBe(true);
    // 不是"允许但画不出来"：幽灵框必须有坐标，否则那句话就成了新的空头承诺。
    expect(preview.rect).toBeTruthy();

    // 而且松手之后真的落得下去，落点就是预演给的那个位置。
    const result = store.moveNodeTo(chosen);
    expect(result.ok).toBe(true);
    const landed = store.nodes.find(node => node.id === chosen.draggedId);
    expect(landed, '拖完之后它必须还在画布上').toBeTruthy();
    expect(landed!.position.x).toBeCloseTo(preview.rect!.x, 5);
    expect(landed!.position.y).toBeCloseTo(preview.rect!.y, 5);
  });

  it('预算截断时"拖到第 2 层节点下面"整体不再是死手势', () => {
    // 缺陷的规模：修之前这 684 种组合里有 **385 种**返回 `missing`（「算不出落点」）。
    // 这里穷举一遍，钉住"一个都没有"——单点回归会漏掉另一半组合。
    const store = useMindMapStore();
    store.preview(REAL_TREES.tree136 as MindMapContent, 'right');
    const buried: string[] = [];

    for (const dragged of store.nodes) {
      if ((dragged.data?.depth ?? 0) < 1) continue;
      for (const target of store.nodes) {
        if (target.id === dragged.id || (target.data?.depth ?? 0) !== 2) continue;
        const preview = store.previewMove({
          draggedId: dragged.id, targetId: target.id, zone: 'child',
        });
        if (!preview.ok && preview.code === 'missing') {
          buried.push(`${dragged.id}→${target.id}`);
        }
      }
    }

    expect(buried).toEqual([]);
  });

  it('几何算不出来时，"能不能落"仍然由结构说了算（不再冒充拒绝）', () => {
    // `preview.ok` 只表示**结构允许**。曾经的写法把"拿不到坐标"和"这个落点不允许"
    // 合成一个 `ok:false`，于是画布（拿 `ok` 当"能不能落"）直接放弃，松手什么都不
    // 发生、`status` 也不写。这条钉住那个语义：`planDrop` 说行，就不许因为画不出来
    // 而改口。
    const store = useMindMapStore();
    store.preview({
      title: '小图',
      root: {
        id: 'r', label: '根', children: [
          { id: 'a', label: '甲', children: [] },
          { id: 'b', label: '乙', children: [] },
        ],
      },
    }, 'right');

    const preview = store.previewMove({ draggedId: 'b', targetId: 'a', zone: 'child' });

    expect(preview.ok).toBe(true);
    expect(preview.rect).toBeTruthy();
  });

  it('真的超深时（正确拒绝）：那句话要给出路，不只是报墙', () => {
    // 缺陷三的另一种可能：`too-deep` 是**正确拒绝**。判据不改，但文案必须让用户知道
    // 下一步做什么——只写"最多 8 层"的话，同一个手势再试一次还是同一句话，而写手册
    // 的人正是据此得出了"是不是不支持挂到第 2 层"的错误结论。
    const store = useMindMapStore();
    // 一条 8 层深的链：把节点挂到最深处会到第 9 层。
    const deep = (depth: number): { id: string; label: string; children: unknown[] } => {
      const node: { id: string; label: string; children: unknown[] } = {
        id: `n${depth}`, label: `第${depth}层`, children: [],
      };
      if (depth > 1) node.children.push(deep(depth - 1));
      return node;
    };
    store.preview({
      title: '很深的图',
      root: { id: 'r', label: '根', children: [deep(8) as never, { id: 'x', label: '孤支', children: [] }] },
    }, 'right');

    const preview = store.previewMove({ draggedId: 'x', targetId: 'n1', zone: 'child' });

    expect(preview.ok).toBe(false);
    expect(preview.code).toBe('too-deep');
    expect(preview.reason).toContain('删掉');
    expect(preview.reason).toContain('浅一点');
  });
});
