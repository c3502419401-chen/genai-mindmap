import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { RepairResult } from '../src/api/client';
import type { Diagnostic, MindMapContent } from '../src/api/types';
import StructureCheck from '../src/components/StructureCheck.vue';
import { normalizeDiagnostics } from '../src/mindmap/diagnostics';
import { repairPlan, repairPlanLine } from '../src/mindmap/repair';
import { useMindMapStore } from '../src/stores/mindmap';
import { useStructureStore } from '../src/stores/structure';
import realJson from './fixtures/repair-real.json';

/**
 * 「一键修复」这个按钮：出现在哪、点了发生什么、面板怎么把"改了什么"说清楚。
 *
 * 三件容易悄悄坏掉的事：
 *
 *  1. **按钮不该自己按下去**。检查是只读的（生成落地后自动跑），修复会改用户的作品，
 *     所以它必须是"用户点了才发生"。
 *  2. **修完要能指出改了什么**。静默替换的树，用户下一次打开会以为这张图一直是这个样子
 *     ——而 `SINGLE_CHILD` 的修复是**改文字**（`外层 / 内层`），不是纯排版。
 *  3. **撤销之后面板不许继续撒谎**。"已修复 3 处"挂在"已经撤回修复前"的画布上，是
 *     最容易让人不再信任这个面板的形状。
 */
interface Fixture {
  before: MindMapContent;
  first: RepairResult;
  second: RepairResult;
  diagnostics_before: Diagnostic[];
}

const REAL = realJson as unknown as Fixture;

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

interface Stub {
  check: number;
  repair: number;
  /** 每次 `POST /optimization/repair` 的请求体（核对"发上去的是不是眼前这棵树"）。 */
  bodies: { content: MindMapContent }[];
}

/**
 * `/check` 与 `/repair` 的替身。
 *
 * `repair` 可以是**一串**响应（按顺序消费，最后一个一直重复）：用来表达"第一次修好了、
 * 第二次后端说没有可修的了"，也用来核对第二次请求体是不是撤销后那棵树。
 */
function stubBackend(payload: { check?: Diagnostic[]; repair?: RepairResult | RepairResult[] }): Stub {
  const hits: Stub = { check: 0, repair: 0, bodies: [] };
  const queue = ([] as RepairResult[]).concat(payload.repair ?? [REAL.first]);
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/optimization/check')) {
      hits.check += 1;
      return json({ diagnostics: payload.check ?? [] });
    }
    if (url.endsWith('/optimization/repair')) {
      hits.repair += 1;
      if (init?.body) hits.bodies.push(JSON.parse(String(init.body)));
      const next = queue.length > 1 ? queue.shift()! : queue[0];
      return json(next);
    }
    if (url.endsWith('/optimization/suggest')) {
      return json({ suggestions: [], diagnostics: payload.check ?? [] });
    }
    return json({});
  }));
  return hits;
}

let wrapper: VueWrapper | null = null;

function mountPanel() {
  const pinia = createPinia();
  setActivePinia(pinia);
  wrapper = mount(StructureCheck, { global: { plugins: [pinia] } });
  return wrapper;
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  vi.unstubAllGlobals();
});

describe('判据（纯函数）：哪些码能修、怎么数给用户看', () => {
  it('只认三个可修码，忽略过的不算，顺序按后端那张表', () => {
    const notes = normalizeDiagnostics([
      { level: 'info', code: 'SINGLE_CHILD', node_id: 'a', message: '一' },
      { level: 'warn', code: 'DEPTH_LIMIT', node_id: 'b', message: '太深了' },
      { level: 'warn', code: 'DUPLICATE_LABEL', node_id: 'c', message: '重名' },
      { level: 'info', code: 'SINGLE_CHILD', node_id: 'd', message: '二' },
    ]);
    expect(repairPlan(notes)).toEqual([
      { code: 'DUPLICATE_LABEL', label: '同级重名', count: 1 },
      { code: 'SINGLE_CHILD', label: '单子节点', count: 2 },
    ]);
    // `SINGLE_CHILD` 是 info（结构常态），但它能修：判据是码，不是 level。
    expect(repairPlanLine(repairPlan(notes))).toBe('同级重名 1 处、单子节点 2 处');
    expect(repairPlan([])).toEqual([]);
  });
});

describe('面板：出现、点击、说清改了什么', () => {
  it('检查只读：不点按钮，画布一个字都不动', async () => {
    stubBackend({ check: REAL.diagnostics_before });
    const panel = mountPanel();
    const store = useMindMapStore();
    const structure = useStructureStore();
    store.preview(structuredClone(REAL.before), 'right');
    const before = JSON.stringify(store.content);

    await structure.run();
    await flushPromises();

    expect(panel.get('[data-test="repair-block"]').text()).toContain('可自动修复');
    expect(panel.get('[data-test="repair-plan"]').text()).toContain('单子节点 3 处');
    // 按钮在，但没人按：树、撤销栈、脏标记都还是原样。
    expect(panel.find('[data-test="repair-apply"]').exists()).toBe(true);
    expect(JSON.stringify(store.content)).toBe(before);
    expect(store.undoDepth).toBe(0);
    expect(panel.find('[data-test="repair-report"]').exists()).toBe(false);
  });

  it('点一下：树变了、面板逐条说清改了什么、并且提示一步撤销', async () => {
    const hits = stubBackend({ check: REAL.diagnostics_before, repair: REAL.first });
    const panel = mountPanel();
    const store = useMindMapStore();
    const structure = useStructureStore();
    store.preview(structuredClone(REAL.before), 'right');
    await structure.run();
    await flushPromises();

    await panel.get('[data-test="repair-apply"]').trigger('click');
    await flushPromises();

    expect(hits.repair).toBe(1);
    expect(JSON.stringify(store.content)).toBe(JSON.stringify(REAL.first.content));
    expect(store.undoDepth).toBe(1);

    const report = panel.get('[data-test="repair-report"]');
    expect(report.get('[data-test="repair-summary"]').text()).toBe('压掉了 3 处单子链');
    // 逐条 message 来自后端（里面写清了新标题是什么），不是前端拼的。
    const changes = panel.findAll('[data-test="repair-change"]');
    expect(changes).toHaveLength(3);
    expect(changes[0].text()).toContain('监督学习 / 利用带标签的数据训练模型');
    expect(report.text()).toContain('Ctrl+Z');
    // 修完之后那 3 条诊断没了（响应体里带的是重跑过的 check 结论），按钮也跟着消失。
    expect(panel.find('[data-test="repair-block"]').exists()).toBe(false);
  });

  it('改动条数超过上限时折叠，但仍然点得到全部', async () => {
    const many: RepairResult = {
      ...REAL.first,
      // 同一批动作重复十遍：这里要验的是"折叠与展开"，不是后端的步数。
      applied: Array.from({ length: 10 }, () => REAL.first.applied[0]),
      steps: 10,
      summary: '压掉了 10 处单子链',
    };
    stubBackend({ check: REAL.diagnostics_before, repair: many });
    const panel = mountPanel();
    const store = useMindMapStore();
    const structure = useStructureStore();
    store.preview(structuredClone(REAL.before), 'right');
    await structure.run();
    await flushPromises();

    await panel.get('[data-test="repair-apply"]').trigger('click');
    await flushPromises();

    expect(panel.findAll('[data-test="repair-change"]')).toHaveLength(8);
    const expand = panel.get('[data-test="repair-changes-expand"]');
    expect(expand.text()).toContain('还有 2 处');
    await expand.trigger('click');
    expect(panel.findAll('[data-test="repair-change"]')).toHaveLength(10);
  });

  it('没修的那几条必须报出来（静默跳过 = 查不出来的状态）', async () => {
    const partial: RepairResult = {
      ...REAL.first,
      skipped: [
        { code: 'DUPLICATE_LABEL', node_id: 'x', reason: '引用并集超出契约上限' },
        { code: 'DUPLICATE_LABEL', node_id: 'y', reason: '正文并集超出契约上限' },
      ],
    };
    stubBackend({ check: REAL.diagnostics_before, repair: partial });
    const panel = mountPanel();
    const store = useMindMapStore();
    const structure = useStructureStore();
    store.preview(structuredClone(REAL.before), 'right');
    await structure.run();
    await flushPromises();

    await panel.get('[data-test="repair-apply"]').trigger('click');
    await flushPromises();

    const skipped = panel.get('[data-test="repair-skipped"]');
    expect(skipped.text()).toContain('2 处没修');
    expect(skipped.text()).toContain('同级重名 2 处');
  });

  it('0 步：不出现「已修复」块，也不压历史', async () => {
    stubBackend({ check: REAL.diagnostics_before, repair: REAL.second });
    const panel = mountPanel();
    const store = useMindMapStore();
    const structure = useStructureStore();
    store.preview(structuredClone(REAL.before), 'right');
    await structure.run();
    await flushPromises();
    const before = JSON.stringify(store.content);

    await panel.get('[data-test="repair-apply"]').trigger('click');
    await flushPromises();

    expect(panel.find('[data-test="repair-report"]').exists()).toBe(false);
    expect(JSON.stringify(store.content)).toBe(before);
    expect(store.undoDepth).toBe(0);
    expect(store.status).toContain('没有可以自动修');
  });
});

describe('撤销之后面板不许继续说"已修复"', () => {
  it('按一次 Ctrl+Z 回到原样，清单标注为"修复当时那一棵"', async () => {
    stubBackend({ check: REAL.diagnostics_before, repair: REAL.first });
    const panel = mountPanel();
    const store = useMindMapStore();
    const structure = useStructureStore();
    store.preview(structuredClone(REAL.before), 'right');
    const before = JSON.stringify(store.content);
    await structure.run();
    await flushPromises();
    await panel.get('[data-test="repair-apply"]').trigger('click');
    await flushPromises();

    // 一次 Ctrl+Z = 回到修复之前（那 3 处一起退掉，不是退一处）。
    store.undo();
    await flushPromises();

    expect(JSON.stringify(store.content)).toBe(before);
    expect(store.undoDepth).toBe(0);
    // 清单还在（用户要能回看"刚才那次修了什么"），但它明说已经不作数了。
    expect(panel.get('[data-test="repair-report"]').text()).toContain('压掉了 3 处单子链');
    expect(panel.find('[data-test="repair-report-stale"]').exists()).toBe(true);
    // 撤销之后手里那份体检结论已经过期（它说的是修复后那棵树），所以按钮先收起来，
    // 面板上那条「这张图在检查之后又改过」就是出路：重新检查一次，按钮回来。
    expect(panel.find('[data-test="structure-stale"]').exists()).toBe(true);
    expect(panel.find('[data-test="repair-apply"]').exists()).toBe(false);

    await structure.run();
    await flushPromises();
    expect(panel.get('[data-test="repair-plan"]').text()).toContain('单子节点 3 处');
    expect(panel.find('[data-test="repair-apply"]').exists()).toBe(true);
  });
});

describe('接口层（不经过面板）：发什么、什么时候不落地', () => {
  it('发上去的是眼前这棵树；撤销之后再点一次，发上去的是撤销后那棵', async () => {
    const hits = stubBackend({ repair: [REAL.first, REAL.first] });
    const store = useMindMapStore();
    const structure = useStructureStore();
    store.preview(structuredClone(REAL.before), 'right');
    const before = JSON.stringify(store.content);

    await expect(structure.applyRepair()).resolves.toBe('ok');
    expect(JSON.stringify(hits.bodies[0].content)).toBe(before);

    store.undo();
    await structure.applyRepair();
    // 否则用户会看到"修完又变回去"：请求体还带着修复前那棵树，而后端会照它修。
    expect(JSON.stringify(hits.bodies[1].content)).toBe(before);
  });

  it('在途期间画布被改过 → 拿回来的树不落地，并且说清原因', async () => {
    // 一个"请求还没回来"的闸门：先让 applyRepair() 停在 fetch 里，趁机改一笔。
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/optimization/repair')) {
        await gate;
        return json(REAL.first);
      }
      return json({});
    }));

    const store = useMindMapStore();
    const structure = useStructureStore();
    store.preview(structuredClone(REAL.before), 'right');

    const pending = structure.applyRepair();
    // 请求还在飞的时候用户改了一笔（画布在修的时候不锁）。
    store.commitLabel(REAL.before.root.id, '我改的');
    const edited = JSON.stringify(store.content);

    release();
    await expect(pending).resolves.toBe('stale');

    // ★ 修复结果**没有**装进画布：用户刚改的那一笔还在，撤销栈也没多一步。
    expect(JSON.stringify(store.content)).toBe(edited);
    expect(store.undoDepth).toBe(1);
    expect(structure.error).toContain('又改过');
  });

  it('第二次应用（后端 0 步）：内容不变、历史不多一格', async () => {
    stubBackend({ repair: [REAL.first, REAL.second] });
    const store = useMindMapStore();
    const structure = useStructureStore();
    store.preview(structuredClone(REAL.before), 'right');

    await structure.applyRepair();
    const repaired = JSON.stringify(store.content);

    await expect(structure.applyRepair()).resolves.toBe('none');
    expect(JSON.stringify(store.content)).toBe(repaired);
    // ★ 没有历史被压进来。多压一步会让"按一次撤销"变成"什么都没变，但栈少了一格"。
    expect(store.undoDepth).toBe(1);
    expect(structure.repairableCount).toBe(0);
  });

  it('非树型图不发请求：后端那三条规则读的是树的根节点', async () => {
    const hits = stubBackend({});
    const structure = useStructureStore();
    const store = useMindMapStore();
    store.preview({
      diagram_type: 'flowchart',
      title: '流程',
      stages: [{ id: 's1', label: '第一步', steps: [{ id: 'p1', label: '做点什么', children: [] }] }],
      edges: [],
    } as unknown as MindMapContent, 'right');

    await expect(structure.applyRepair()).resolves.toBe('unsupported');
    expect(hits.repair).toBe(0);
    expect(structure.error).toContain('只支持思维导图');
  });
});
