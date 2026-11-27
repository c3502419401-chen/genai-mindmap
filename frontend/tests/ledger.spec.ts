import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { EditEventRow, EditHealth, EditSummary } from '../src/api/types';
import EditLedger from '../src/components/EditLedger.vue';
import { useLedgerStore } from '../src/stores/ledger';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * 账本界面：**把"这张图改过什么"从数据库搬到屏幕上**。
 *
 * 这组用例守四件事，每件都对应一个"不测就等于没有"的判据：
 *
 *   1. **读到的是真事件形状**。行数据按 `EditEventRow` 构造（`kind` / `actor` /
 *      `field` / `before` / `after` / `subtree_size` / `node_count`），不是随手编的
 *      对象——文案只允许用这些字段，编一个不存在的字段就会在这里红。
 *   2. **一键修复那批折叠成一行**。它是账本唯一需要折叠的东西：一次修复写进三四十
 *      条事件，逐条列出来会把同一张图上的手工编辑埋掉。
 *   3. **人话**。不许出现 `kind` 码（`remove` / `update` / `move`），除非后端给了
 *      一个界面还不认识的 `kind` —— 那时原样显示是刻意的。
 *   4. **`getEditHealth` 只在异常时出现**。它是运维读数，正常时一个字都不该显示。
 */
vi.mock('../src/api/client', async () => {
  const actual = await vi.importActual<typeof import('../src/api/client')>('../src/api/client');
  return {
    ...actual,
    getEditSummary: vi.fn(),
    listEditEvents: vi.fn(),
    getEditHealth: vi.fn(),
  };
});

const MAP = 'm1';

function row(partial: Partial<EditEventRow> & Pick<EditEventRow, 'kind'>): EditEventRow {
  return {
    id: 1,
    map_id: MAP,
    actor: 'user',
    node_id: 'c1',
    at: '2026-11-27T10:00:00.000Z',
    recorded_at: '2026-11-27T10:00:01.000Z',
    ...partial,
  };
}

function summaryOf(partial: Partial<EditSummary> = {}): EditSummary {
  return {
    total: 0,
    last_event_at: null,
    by_actor: {},
    by_kind: {},
    by_field: {},
    label_shrunk: { count: 0, avg_before: null, avg_after: null },
    removed_depth: {},
    removed_subtree: {},
    generated_runs: 0,
    generated_nodes: 0,
    changed_events: 0,
    untouched_runs: 0,
    ...partial,
  };
}

function healthOf(partial: Partial<EditHealth> = {}): EditHealth {
  return {
    window_start: '2026-11-26T00:00:00.000Z',
    window_end: '2026-11-27T00:00:00.000Z',
    edited_maps: 0,
    saved_maps: 0,
    chain_suspect: false,
    detail: '',
    ...partial,
  };
}

/**
 * 一次真实的账本形状：旧的在后（后端 `ORDER BY at DESC, id ASC`）。
 *
 * 最后那三条是**同一批**一键修复（同一个 `at`）——它的 `kind` 被整批换成 `repair`，
 * 动作粒度只剩 `field` / `subtree_size` / 位次，界面必须从载荷反推，不能读 `kind`。
 */
const EVENTS: EditEventRow[] = [
  row({
    id: 9, kind: 'repair', field: 'label', node_id: 'c9',
    before: '外层 / 内层', after: '内层', depth: 2, at: '2026-11-27T11:00:00.000Z',
  }),
  row({
    id: 8, kind: 'repair', node_id: 'c8', before: '第2位', after: '第1位',
    depth: 2, at: '2026-11-27T11:00:00.000Z',
  }),
  row({
    id: 7, kind: 'repair', node_id: 'c7', before: '多余的一支', depth: 3,
    subtree_size: 3, at: '2026-11-27T11:00:00.000Z',
  }),
  row({
    id: 6, kind: 'move', node_id: 'c6', before: '主题', after: '风险', depth: 2,
    at: '2026-11-27T10:30:00.000Z',
  }),
  row({
    id: 10, kind: 'style', field: 'color', node_id: 'c10', before: '无', after: 'amber',
    depth: 1, at: '2026-11-27T10:25:00.000Z',
  }),
  row({
    id: 5, kind: 'update', field: 'label', node_id: 'c5',
    before: '机器学习的主要方向与细分领域介绍', after: '机器学习方向', depth: 2,
    at: '2026-11-27T10:20:00.000Z',
  }),
  row({
    id: 4, kind: 'generate', actor: 'agent', node_id: undefined, node_count: 32,
    run_id: 'run-1', at: '2026-11-27T10:00:00.000Z',
  }),
];

let pinia: ReturnType<typeof createPinia>;

function wire(events = EVENTS, summary = summaryOf({ total: events.length })): void {
  vi.mocked(api.listEditEvents).mockResolvedValue(events);
  vi.mocked(api.getEditSummary).mockResolvedValue(summary);
  vi.mocked(api.getEditHealth).mockResolvedValue(healthOf());
}

async function openLedger() {
  pinia = createPinia();
  setActivePinia(pinia);
  const mindmap = useMindMapStore();
  mindmap.mapId = MAP;
  const wrapper = mount(EditLedger, { global: { plugins: [pinia] } });
  await wrapper.get('[data-test="edit-ledger-toggle"]').trigger('click');
  await flushPromises();
  return { wrapper, mindmap, ledger: useLedgerStore() };
}

beforeEach(() => {
  vi.mocked(api.listEditEvents).mockReset();
  vi.mocked(api.getEditSummary).mockReset();
  vi.mocked(api.getEditHealth).mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('账本：这张图改过什么', () => {
  it('读到真实事件，逐条长成人话，没有一个 kind 码漏到界面上', async () => {
    wire();
    const { wrapper } = await openLedger();

    const text = wrapper.text();
    expect(text).toContain('模型生成 · 32 个节点');
    expect(text).toContain('你改短了标题：「机器学习的主要方向与细分领域介绍」→「机器学习方向」');
    expect(text).toContain('你换了父节点：主题 → 风险');
    // 颜色是调色板**键**，界面必须念成人话（与节点详情那个下拉同一份标签）。
    expect(text).toContain('你改了颜色：无 → 琥珀（注意）');
    expect(text).not.toContain('amber');
    // 规则整理那一批折成了一条，不再逐条出现。
    expect(text).toContain('规则整理 · 3 处');
    expect(text).toContain('删了 3 个节点（其中 1 整支） · 改了 1 处文字 · 搬了 1 处');

    // 五个动作 = 五行（七条事件，其中三条被折叠）。
    expect(wrapper.findAll('[data-test="edit-ledger-row"]')).toHaveLength(5);
    for (const code of ['remove', 'update', 'move', 'repair', 'generate']) {
      expect(text).not.toContain(code);
    }
  });

  it('一键修复那一条点开能看到明细，动作从载荷反推（kind 已被整批覆盖）', async () => {
    wire();
    const { wrapper } = await openLedger();

    const button = wrapper.findAll('[data-test="edit-ledger-expand"]');
    expect(button).toHaveLength(1);
    expect(button[0].text()).toBe('3 条明细');
    await button[0].trigger('click');

    const children = wrapper.findAll('[data-test="edit-ledger-child"]').map(item => item.text());
    expect(children).toEqual([
      '改了标题：外层 / 内层 → 内层',
      '调了顺序：第2位 → 第1位',
      '删掉「多余的一支」整支 · 共 3 个节点',
    ]);
    // 再点一次收起：明细是"要的时候才占地方"的东西。
    await wrapper.get('[data-test="edit-ledger-expand"]').trigger('click');
    expect(wrapper.findAll('[data-test="edit-ledger-child"]')).toHaveLength(0);
  });

  it('摘要与三条读数：模型给了多少、改了多少、几次一字未改、标题短了多少', async () => {
    wire(EVENTS, summaryOf({
      total: 6,
      generated_runs: 2,
      generated_nodes: 47,
      changed_events: 6,
      untouched_runs: 1,
      label_shrunk: { count: 7, avg_before: 18.3, avg_after: 9.1 },
      removed_depth: { '2': 2, '3': 5 },
    }));
    const { wrapper } = await openLedger();

    expect(wrapper.get('[data-test="edit-ledger-headline"]').text()).toBe(
      '· 模型给了 47 个节点 · 图上 6 处改动 · 1 / 2 次生成一字未改',
    );
    const notes = wrapper.findAll('[data-test="edit-ledger-note"]').map(item => item.text());
    expect(notes).toContain('· 标题普遍被改短：平均 18.3 字 → 9.1 字（7 次）');
    expect(notes).toContain('· 零散删掉的节点集中在第 3 层 · 5 个');
  });

  it('账本可能没在记的时候才说话：正常时那一行不出现', async () => {
    wire();
    vi.mocked(api.getEditHealth).mockResolvedValue(healthOf({
      edited_maps: 0, saved_maps: 3, chain_suspect: true,
    }));
    const { wrapper } = await openLedger();

    const warning = wrapper.get('[data-test="edit-ledger-warning"]');
    expect(warning.text()).toContain('账本可能没在记');
    expect(warning.text()).toContain('昨天存过 3 张图，0 张收到改动记录');
  });

  it('链路正常时健康读数一个字都不显示', async () => {
    wire();
    vi.mocked(api.getEditHealth).mockResolvedValue(healthOf({
      edited_maps: 4, saved_maps: 4, chain_suspect: false, detail: '正常',
    }));
    const { wrapper } = await openLedger();

    expect(wrapper.find('[data-test="edit-ledger-warning"]').exists()).toBe(false);
    expect(wrapper.text()).not.toContain('正常');
  });

  it('选中节点时能只看它自己的改动', async () => {
    wire();
    const { wrapper, mindmap } = await openLedger();

    // 没有选中节点时这个开关不出现（它没有意义）。
    expect(wrapper.find('[data-test="edit-ledger-only-selected"]').exists()).toBe(false);

    mindmap.selectedId = 'c5';
    await flushPromises();
    const box = wrapper.get('[data-test="edit-ledger-only-selected"]');
    await box.setValue(true);
    expect(wrapper.findAll('[data-test="edit-ledger-row"]')).toHaveLength(1);
    expect(wrapper.text()).toContain('你改短了标题');

    // 换一个没被改过的节点：不是空白，要说清"最近这些记录里没有它"。
    mindmap.selectedId = 'c404';
    await flushPromises();
    expect(wrapper.get('[data-test="edit-ledger-filter-empty"]').text()).toContain(
      '这个节点在最近这些记录里没有改动',
    );
  });

  it('还没保存过的图不发请求，并说清账本从哪一刻开始记', async () => {
    pinia = createPinia();
    setActivePinia(pinia);
    const wrapper = mount(EditLedger, { global: { plugins: [pinia] } });
    await wrapper.get('[data-test="edit-ledger-toggle"]').trigger('click');
    await flushPromises();

    expect(wrapper.get('[data-test="edit-ledger-unsaved"]').text()).toContain('从第一次保存开始记');
    expect(api.listEditEvents).not.toHaveBeenCalled();
    expect(api.getEditSummary).not.toHaveBeenCalled();
    expect(api.getEditHealth).not.toHaveBeenCalled();
  });

  it('收起时不发请求；有未保存改动时明说那些改动还不在账本里', async () => {
    wire();
    pinia = createPinia();
    setActivePinia(pinia);
    const mindmap = useMindMapStore();
    mindmap.mapId = MAP;
    mindmap.dirty = true;
    const wrapper = mount(EditLedger, { global: { plugins: [pinia] } });

    expect(api.listEditEvents).not.toHaveBeenCalled();
    await wrapper.get('[data-test="edit-ledger-toggle"]').trigger('click');
    await flushPromises();

    expect(wrapper.get('[data-test="edit-ledger-dirty"]').text()).toContain('保存之后它们才会出现在这里');
  });

  it('换了图就清空：上一张图的账本不许留在屏幕上', async () => {
    wire();
    const { wrapper, mindmap, ledger } = await openLedger();
    expect(ledger.lines).toHaveLength(5);

    vi.mocked(api.listEditEvents).mockResolvedValue([]);
    vi.mocked(api.getEditSummary).mockResolvedValue(summaryOf());
    mindmap.mapId = 'm2';
    await flushPromises();

    expect(api.listEditEvents).toHaveBeenLastCalledWith('m2', expect.any(Number));
    expect(wrapper.get('[data-test="edit-ledger-empty"]').text()).toContain('还没有任何改动记录');
  });

  it('读不到时说一句原因，而不是空着', async () => {
    vi.mocked(api.listEditEvents).mockRejectedValue(new Error('网关超时'));
    vi.mocked(api.getEditSummary).mockResolvedValue(summaryOf());
    vi.mocked(api.getEditHealth).mockResolvedValue(healthOf());
    const { wrapper } = await openLedger();

    expect(wrapper.get('[data-test="edit-ledger-error"]').text()).toContain('网关超时');
  });

  it('健康读数坏掉不拖垮明细：它是辅助，不是主路径', async () => {
    vi.mocked(api.listEditEvents).mockResolvedValue(EVENTS);
    vi.mocked(api.getEditSummary).mockResolvedValue(summaryOf({ total: 6 }));
    vi.mocked(api.getEditHealth).mockRejectedValue(new Error('health 挂了'));
    const { wrapper, ledger } = await openLedger();

    expect(ledger.error).toBe('');
    expect(ledger.lines).toHaveLength(5);
    expect(wrapper.find('[data-test="edit-ledger-error"]').exists()).toBe(false);
  });
});
