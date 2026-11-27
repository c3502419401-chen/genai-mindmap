import { flushPromises, mount } from '@vue/test-utils';
import type { VueWrapper } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ActionPoint } from './helpers/user-path';
import MindNode from '../src/canvas/MindNode.vue';
import SourceContextDrawer from '../src/components/SourceContextDrawer.vue';
import type { MindNodeData } from '../src/stores/mindmap';
import { useMindMapStore } from '../src/stores/mindmap';
import { resolveRefWithText } from '../src/mindmap/refs';
import {
  SAVED_MAP,
  catalogGaps,
  cleanupPages,
  entryOf,
  mountPage,
  point,
  resetTouched,
  stubApi,
  tap,
  visible,
} from './helpers/user-path';

vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

/**
 * 第二批动作清单：**画布 / 引用 / 知识库 / 检查 / 导出**。
 *
 * 与第一批同一条判据：界面上有没有一个用户能点到的地方，点了之后发生了什么。
 *
 * 第二批里有两条**不是按钮**：拖动、双击空白。它们是手势，`coveredBy` 指向真正
 * 覆盖它们的 spec——`catalogGaps` 会核对那份文件确实存在，所以"已覆盖"这句话可查。
 */
const ACTIONS: ActionPoint[] = [
  // 画布
  { name: '改朝向', where: '画布左上角', locator: { text: '向右' } },
  { name: '改文字', where: '节点详情侧栏', locator: { text: '改文字（F2）' } },
  { name: '加子节点', where: '节点详情侧栏', locator: { text: '加子节点（Tab）' } },
  { name: '加兄弟', where: '节点详情侧栏', locator: { text: '加兄弟（Enter）' } },
  { name: '删除节点', where: '节点详情侧栏', locator: { text: '删除（Delete）' } },
  { name: '展开节点', where: '画布节点上的「+N」角标', locator: { selector: '[data-test="node-folded"]' } },
  { name: '收起节点', where: '画布节点上的「−」角标', locator: { selector: '[data-test="node-collapse"]' } },
  { name: '展开这一支', where: '画布一级分支的概括角标', locator: { selector: '[data-test="node-branch-digest"]' } },
  { name: '拖动', where: '画布（手势）', locator: { selector: '[data-test="never"]' }, coveredBy: '../node-drag.spec.ts' },
  { name: '双击空白', where: '画布空白处（手势）', locator: { selector: '[data-test="never"]' }, coveredBy: '../canvas-keys.spec.ts' },

  // 引用
  { name: '看引用清单', where: '画布节点上的引用角标', locator: { selector: '[data-test="node-refs"]' } },
  { name: '看上下文', where: '引用原文抽屉', locator: { selector: '[data-test="ref-context-toggle"]' } },
  { name: '跳原文', where: '引用原文抽屉（外部来源）', locator: { selector: '[data-test="ref-drawer"] a[target="_blank"]' } },

  // 知识库（面板是 `aside[role=dialog]`）。两个删除各带一个 `data-test`：它们曾经
  // 都只写「删除」，而左边的历史抽屉里还有第三个同名按钮——按文本来定位只能靠
  // "第几个"，文档顺序一变就悄悄点到别的动作上（缺陷与修法见 `delete-actions.spec.ts`）。
  { name: '知识库列表', where: '历史抽屉「知识库」', locator: { text: '知识库' } },
  { name: '删除材料', where: '知识库抽屉（材料行）', locator: { selector: '[data-test="library-source-delete"]' } },
  { name: '实体别名清单', where: '知识库抽屉（实体别名）', locator: { text: '查看' } },
  { name: '删除别名', where: '知识库抽屉（别名行）', locator: { selector: '[data-test="alias-delete"]' } },
  {
    name: '找出可合并的实体',
    where: '知识库抽屉（实体别名）',
    locator: { text: '找出可合并的实体', scope: 'aside[role="dialog"]' },
  },

  // 检查
  { name: '检查与建议', where: '节点详情侧栏（检查与建议）', locator: { selector: '[data-test="structure-run"]' } },
  { name: '一键修复', where: '节点详情侧栏（可自动修复）', locator: { selector: '[data-test="repair-apply"]' } },
  { name: '应用建议', where: '节点详情侧栏（建议）', locator: { selector: '[data-test="suggestion-apply"]' } },

  // 导出与导入
  { name: '导出 PlantUML', where: '历史抽屉（导出下拉）', locator: { selector: 'option[value="puml"]' } },
  { name: '导出 Markdown', where: '历史抽屉（导出下拉）', locator: { selector: 'option[value="markdown"]' } },
  { name: '导出 OPML', where: '历史抽屉（导出下拉）', locator: { selector: 'option[value="opml"]' } },
  { name: '导出 JSON', where: '历史抽屉（导出下拉）', locator: { selector: 'option[value="json"]' } },
  { name: '导入', where: '历史抽屉', locator: { selector: '[data-test="import-button"]' } },
];

const act = (name: string) => point(ACTIONS, name);

resetTouched();

afterEach(() => {
  cleanupPages();
});

async function openDrawer(wrapper: VueWrapper): Promise<void> {
  await wrapper.get('[data-test="history-toggle"]').trigger('click');
  await flushPromises();
}

/** 有图、有选中节点，且侧栏打开——侧栏那五条动作的前提。 */
async function openSidePanel(wrapper: VueWrapper): Promise<void> {
  const store = useMindMapStore();
  store.loadMap(SAVED_MAP.content, { id: SAVED_MAP.id, version: 2, updated_at: SAVED_MAP.updated_at });
  store.select('c1');
  await flushPromises();
  if (!visible(wrapper.find('[data-test="side-panel"]').element)) {
    await openDrawer(wrapper);
    await wrapper.get('[data-test="side-toggle"]').trigger('click');
    await flushPromises();
  }
}

const ExportSelect = 'select[title="导出为文本格式"]';

const DIAGNOSTICS = [{
  level: 'warn', code: 'DUPLICATE_LABEL', node_id: 'c1', message: '同级有两个「子」',
}];

const SUGGESTIONS = [{
  kind: 'dedupe', node_id: 'c1', message: '合并重复的「子」',
}];

describe('用户路径 · 画布（节点上的角标）', () => {
  function nodeOf(extra: Partial<MindNodeData> = {}) {
    const data: MindNodeData = {
      label: '子', body: '子', fullLabel: '子', depth: 1, out: ['right'], in: ['left'], refs: 0, ...extra,
    };
    return mount(MindNode, { props: { id: 'n1', data }, global: { stubs: { Handle: true } } });
  }

  it('展开节点：还有没显示的孩子时，节点上有一枚「+N」，点了请外面展开', async () => {
    const wrapper = nodeOf({ hiddenChildren: 3, hiddenSubtree: 8 });
    const badge = entryOf(wrapper, act('展开节点'));
    expect(badge.text()).toContain('3');

    await badge.trigger('click');

    // 视图动作落到 emit 上：坐标与折叠状态都在画布那一层，节点自己不判。
    expect(wrapper.emitted('toggleFold')).toHaveLength(1);
    wrapper.unmount();
  });

  it('收起节点：孩子正显示着时有一枚「−」，点了请外面叠回去', async () => {
    const wrapper = nodeOf({ visibleChildren: 2 });
    const badge = entryOf(wrapper, act('收起节点'));

    await badge.trigger('click');

    expect(wrapper.emitted('collapse')).toHaveLength(1);
    wrapper.unmount();
  });

  it('展开这一支：一级分支上有概括角标，点了整支展开', async () => {
    const wrapper = nodeOf({ branchDigest: '训练与并行', branchHidden: 21 });
    const badge = entryOf(wrapper, act('展开这一支'));
    expect(badge.text()).toContain('21');

    await badge.trigger('click');

    expect(wrapper.emitted('expandBranch')).toHaveLength(1);
    wrapper.unmount();
  });

  it('看引用清单：有引用的节点上有角标，点了把清单摆出来', async () => {
    const wrapper = nodeOf({ refs: 3 });
    const badge = entryOf(wrapper, act('看引用清单'));
    expect(badge.text()).toContain('3');

    await badge.trigger('click');

    expect(wrapper.emitted('openRefs')).toHaveLength(1);
    wrapper.unmount();
  });
});

describe('用户路径 · 画布（顶栏与侧栏）', () => {
  it('改朝向：画布左上角有四个朝向，点了之后朝向跟着变', async () => {
    const { wrapper } = await mountPage();
    const store = useMindMapStore();
    store.preview(SAVED_MAP.content, 'right');

    const entry = entryOf(wrapper, act('改朝向'));
    expect(entry.element.tagName).toBe('BUTTON');
    await tap(wrapper, act('改朝向'));

    // 四个朝向都得在（这一条只点第一个，存在性靠它们自己的文案保证）。
    for (const label of ['向左', '向下', '双向']) {
      expect(wrapper.findAll('button').some(item => item.text().trim() === label)).toBe(true);
    }
    expect(store.direction).not.toBe('');
  });

  it('加子节点 / 加兄弟 / 删除节点：侧栏三个按钮都在，且真的改树', async () => {
    const { wrapper } = await mountPage();
    await openSidePanel(wrapper);
    const store = useMindMapStore();
    const before = store.nodeCount;

    await tap(wrapper, act('加子节点'));
    expect(store.nodeCount).toBe(before + 1);

    await tap(wrapper, act('加兄弟'));
    expect(store.nodeCount).toBe(before + 2);

    // 删的是刚加出来的那个（每次加完都会选中新节点）。
    await tap(wrapper, act('删除节点'));
    expect(store.nodeCount).toBe(before + 1);
  });

  it('改文字：侧栏有「改文字（F2）」，点了进入内联编辑', async () => {
    const { wrapper } = await mountPage();
    await openSidePanel(wrapper);
    const store = useMindMapStore();

    await tap(wrapper, act('改文字'));

    expect(store.editingId).toBe('c1');
  });
});

describe('用户路径 · 引用', () => {
  const PRE = '开头的铺垫。';
  const HIT = '目标句子在这里。';
  const TEXT = PRE + HIT;
  const START = Array.from(PRE).length;

  function refWith() {
    return resolveRefWithText(
      { source_id: 's1', start: START, end: START + Array.from(HIT).length, quote: HIT },
      TEXT,
      {
        id: 's1', kind: 'document', filename: '线代.pdf', text: TEXT, span: null,
        status: 'parsed', error: null, parsed_pages: null, truncated: false, failed_pages: [],
      } as never,
    );
  }

  it('看上下文：引用抽屉里有一个开关，点了就地展开上下文', async () => {
    const wrapper = mount(SourceContextDrawer, { props: { item: refWith() } });
    await flushPromises();

    const toggle = entryOf(wrapper, act('看上下文'));
    await toggle.trigger('click');
    await flushPromises();

    expect(visible(wrapper.find('[data-test="ref-context"]').element)).toBe(true);
    wrapper.unmount();
  });

  it('跳原文：外部来源给一条能到原始地址的链接', async () => {
    const wrapper = mount(SourceContextDrawer, {
      props: {
        item: resolveRefWithText(
          { source_id: 's1', start: START, end: START + Array.from(HIT).length, quote: HIT },
          TEXT,
          {
            id: 's1', kind: 'document', filename: '维基条目', text: TEXT, span: null,
            status: 'parsed', error: null, parsed_pages: null, truncated: false, failed_pages: [],
            metadata: { url: 'https://example.com/a' },
          } as never,
        ),
      },
    });
    await flushPromises();

    const link = entryOf(wrapper, act('跳原文'));
    expect(link.attributes('href')).toBe('https://example.com/a');
    wrapper.unmount();
  });
});

describe('用户路径 · 知识库', () => {
  it('列表：「知识库」打开面板，发出 GET /knowledge/library', async () => {
    const { wrapper, api } = await mountPage();
    await openDrawer(wrapper);

    await tap(wrapper, act('知识库列表'));

    expect(wrapper.find('#library-title').exists()).toBe(true);
    expect(api.calls.some(call => call.url.includes('/knowledge/library'))).toBe(true);
  });

  it('删除材料：材料行有一个删除入口，点了发出 DELETE', async () => {
    const api = stubApi(call =>
      (call.url.includes('/knowledge/library') && call.method === 'GET' ? undefined : undefined));
    const { wrapper } = await mountPage({ api });
    await openDrawer(wrapper);
    await tap(wrapper, act('知识库列表'));

    await tap(wrapper, act('删除材料'));
    await new Promise(resolve => setTimeout(resolve, 0));

    expect(api.calls.some(call => call.method === 'DELETE'
      && call.url.includes('/knowledge/library/lib-1'))).toBe(true);
  });

  it('实体别名：清单 / 删除一条 / 找出可合并的，三个入口都在', async () => {
    const api = stubApi(call => {
      if (call.url.includes('/knowledge/aliases') && call.method === 'GET') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            items: [{ alias: '线代', canonical: '线性代数', reason: 'model' }],
            total: 1,
            configured: true,
          }),
        } as unknown as Response;
      }
      return undefined;
    });
    const { wrapper } = await mountPage({ api });
    await openDrawer(wrapper);
    await tap(wrapper, act('知识库列表'));

    await tap(wrapper, act('实体别名清单'));
    expect(api.calls.some(call => call.method === 'GET' && call.url.includes('/knowledge/aliases')))
      .toBe(true);

    // 两个删除各有自己的 `data-test`，按文档顺序取第二个的写法已经退休（见该点位的注释）。
    await tap(wrapper, act('删除别名'));
    await new Promise(resolve => setTimeout(resolve, 0));
    // 别名里的中文会被 `encodeURIComponent` 编码，所以只认路径前缀。
    expect(api.calls.some(call => call.method === 'DELETE'
      && call.url.includes('/knowledge/aliases/'))).toBe(true);

    await tap(wrapper, act('找出可合并的实体'));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(api.calls.some(call => call.url.includes('/knowledge/aliases/candidates'))).toBe(true);
  });
});

describe('用户路径 · 检查', () => {
  it('检查与建议：侧栏有一个「检查与建议」，点了把这一轮送出去做体检', async () => {
    const { wrapper, api } = await mountPage();
    await openSidePanel(wrapper);

    await tap(wrapper, act('检查与建议'));

    // 手动路径走的是 `suggest` 而不是 `check`：它要连"建议"与"补关系"一起拿回来
    // （`stores/structure.ts::runFull`），`check` 只服务生成之后那次免费体检。
    expect(api.calls.some(call => call.method === 'POST' && call.url.includes('/optimization/suggest')))
      .toBe(true);
  });

  it('一键修复：可修复时出现「一键修复」，点了发出 POST /optimization/repair', async () => {
    const api = stubApi(call => {
      if (call.url.includes('/optimization/check')) {
        return { ok: true, status: 200, json: async () => ({ diagnostics: DIAGNOSTICS }) } as unknown as Response;
      }
      if (call.url.includes('/optimization/suggest')) {
        return { ok: true, status: 200, json: async () => ({ suggestions: SUGGESTIONS, diagnostics: DIAGNOSTICS, link: null }) } as unknown as Response;
      }
      return undefined;
    });
    const { wrapper } = await mountPage({ api });
    await openSidePanel(wrapper);
    await tap(wrapper, act('检查与建议'));

    await tap(wrapper, act('一键修复'));

    expect(api.calls.some(call => call.method === 'POST' && call.url.includes('/optimization/repair')))
      .toBe(true);
  });

  it('应用建议：有可应用的建议时出现「应用」，点了按建议改图', async () => {
    const api = stubApi(call => {
      if (call.url.includes('/optimization/suggest')) {
        return { ok: true, status: 200, json: async () => ({ suggestions: SUGGESTIONS, diagnostics: DIAGNOSTICS, link: null }) } as unknown as Response;
      }
      return undefined;
    });
    const { wrapper } = await mountPage({ api });
    await openSidePanel(wrapper);
    await tap(wrapper, act('检查与建议'));
    const store = useMindMapStore();
    const before = store.nodeCount;

    await tap(wrapper, act('应用建议'));

    expect(store.nodeCount).toBe(before - 1);
  });
});

describe('用户路径 · 导出与导入', () => {
  for (const [name, format] of [
    ['导出 PlantUML', 'puml'],
    ['导出 Markdown', 'markdown'],
    ['导出 OPML', 'opml'],
    ['导出 JSON', 'json'],
  ] as const) {
    it(`${name}：下拉里有这一项，选了就发出 POST /export/${format}`, async () => {
      const { wrapper, api } = await mountPage();
      useMindMapStore().preview(SAVED_MAP.content, 'right');
      await openDrawer(wrapper);
      expect(entryOf(wrapper, act(name)).element.tagName).toBe('OPTION');

      await wrapper.get(ExportSelect).setValue(format);
      await flushPromises();

      expect(api.calls.some(call => call.method === 'POST' && call.url.includes(`/export/${format}`)))
        .toBe(true);
    });
  }

  it('导入：有一个「导入」入口，选中文件后发出 POST /mindmaps/import', async () => {
    const api = stubApi(call => {
      if (call.url.includes('/mindmaps/import')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ content: SAVED_MAP.content, warnings: [], source_format: 'json' }),
        } as unknown as Response;
      }
      return undefined;
    });
    const { wrapper } = await mountPage({ api });
    await openDrawer(wrapper);
    await tap(wrapper, act('导入'));

    // 这个 `input` 被 teleport 到 body（两个上传入口摆在一起会互相抢定位）。
    const input = document.querySelector('[data-test="import-file"]') as HTMLInputElement | null;
    expect(input, '导入的文件选择框不存在，点了「导入」没有下一步').toBeTruthy();
    Object.defineProperty(input, 'files', {
      value: [new File(['{}'], '图.json', { type: 'application/json' })],
      configurable: true,
    });
    input?.dispatchEvent(new Event('change'));
    await new Promise(resolve => setTimeout(resolve, 0));

    expect(api.calls.some(call => call.method === 'POST' && call.url.includes('/mindmaps/import')))
      .toBe(true);
  });
});

describe('清单本身', () => {
  it('每条动作都被本轮点到，或登记了一处真实存在的手势覆盖', () => {
    expect(catalogGaps(ACTIONS)).toEqual([]);
  });
});
