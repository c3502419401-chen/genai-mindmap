import { flushPromises } from '@vue/test-utils';
import type { VueWrapper } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ActionPoint } from './helpers/user-path';
import { textSource } from '../src/sources/input';
import { useMindMapStore } from '../src/stores/mindmap';
import { useSourcesStore } from '../src/stores/sources';
import {
  SAVED_MAP,
  catalogGaps,
  cleanupPages,
  entryOf,
  mountPage,
  pendingStream,
  point,
  resetTouched,
  stubApi,
  tap,
  visible,
} from './helpers/user-path';

/** 画布换成占位：这一批动作全在对话框与抽屉里，画布里的那些归另一份。 */
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

/**
 * 第一批动作清单：**材料 / 生成 / 导图**。
 *
 * 这张表是从用户手册的角度抄下来的，不是从组件树抄的。差别在于：组件树里每个
 * `@click` 都在，而用户手册写的是"你能做什么"。四个实测出来的缺陷（删导图 405、
 * 移除/删除并排、拖动预算、手动节点无标记）当时都处在同一种状态里——**代码在、
 * 用例绿、用户走不到**。这张表就是为了让"走不到"有红灯形态。
 *
 * 每条用例只断言两件事：入口在（而且此刻可见）、点了之后发生了什么。文案与错误分支
 * 归既有 spec，这里不重复。
 */
const ACTIONS: ActionPoint[] = [
  // 材料（要先把面板展开：默认收起，收起时那些按钮不可见）
  { name: '添加材料', where: '底部对话框', locator: { selector: '[data-test="attach"]' } },
  { name: '管理材料', where: '底部对话框（材料缩略图行）', locator: { selector: '[data-test="manage-materials"]' } },
  { name: '粘贴正文', where: '材料面板', locator: { selector: '#material-text' } },
  { name: '添加文本到来源', where: '材料面板', locator: { text: '添加文本到来源' } },
  { name: '上传文档', where: '材料面板', locator: { selector: '[data-test="material-panel"] input[type="file"]' } },
  { name: '上传图片', where: '材料面板', locator: { selector: '[data-test="material-panel"] input[type="file"]' } },
  { name: '录音', where: '材料面板', locator: { text: '录音添加来源' } },
  { name: '移除', where: '材料面板', locator: { labelPrefix: '从本轮移除' } },
  { name: '清空来源', where: '材料面板', locator: { text: '清空来源' } },
  { name: '彻底删除', where: '材料面板（已入库的材料）', locator: { text: '彻底删除' } },

  // 生成
  { name: '输入主题', where: '底部对话框', locator: { selector: '#intent' } },
  { name: '选拆解方式', where: '底部对话框（拆解方式与图型）', locator: { selector: '#thinking-model' } },
  { name: '选图型', where: '底部对话框（拆解方式与图型）', locator: { selector: '#diagram-type' } },
  { name: '发送', where: '底部对话框', locator: { labelPrefix: '生成导图' } },
  { name: '取消', where: '底部对话框（生成中那一行状态）', locator: { selector: '[data-test="run-status-cancel"]' } },
  { name: '深度思考', where: '底部对话框', locator: { selector: '[data-test="deep-thinking"]' } },
  { name: '智能搜索', where: '底部对话框', locator: { selector: '[data-test="external-search"]' } },

  // 导图
  { name: '打开历史与更多操作', where: '顶栏', locator: { selector: '[data-test="history-toggle"]' } },
  { name: '保存', where: '历史抽屉', locator: { text: '保存' } },
  { name: '新建', where: '历史抽屉', locator: { text: '新建' } },
  { name: '打开', where: '历史抽屉（历史导图列表）', locator: { selector: '[title^="打开「"]' } },
  { name: '打开列表弹窗', where: '历史抽屉', locator: { selector: '[data-test="history-open-list"]' } },
  { name: '删除导图', where: '历史抽屉', locator: { selector: '[data-test="drawer-map-delete"]' } },
  { name: '删除导图（列表弹窗）', where: '已保存的导图弹窗', locator: { selector: '[data-test="map-delete"]' } },
];

const act = (name: string) => point(ACTIONS, name);

/**
 * 一次清空就够：`touched` 记的是"这份文件里有没有用例碰过这条动作"，而最后那条
 * meta 用例要与整份文件的执行结果比。放进 `beforeEach` 会把前面所有用例的登记一起
 * 抹掉——那样 meta 永远在第一句就红。
 */
resetTouched();

afterEach(() => {
  cleanupPages();
});

/**
 * 展开材料面板。
 *
 * 走「+」而不是「管理材料」：后者只在缩略图那一行里渲染，而那一行**一份材料都没有
 * 时不渲染**。所以「+」是新用户唯一的入口——它还顺手弹出系统文件框，所以这条路径
 * 必须自己也能把面板打开（`pickFiles` 里两步都有）。
 */
async function openMaterials(wrapper: VueWrapper): Promise<void> {
  await tap(wrapper, act('添加材料'));
}

async function openDrawer(wrapper: VueWrapper): Promise<void> {
  await tap(wrapper, act('打开历史与更多操作'));
}

/** 一份"已经进过知识库"的材料：`source.id` 与行 key 不同就是入库过。 */
function libraryItem() {
  return {
    key: 'row-1',
    id: 'row-1',
    label: '讲义.pdf',
    kind: 'document' as const,
    state: 'ready' as const,
    source: { ...textSource('讲义的正文'), id: 'lib-1' },
    error: '',
    seconds: 0,
  };
}

/** 等一轮 microtask + 一次 setTimeout(0)：请求链上的 promise 才真的走完。 */
const settle = () => new Promise(resolve => setTimeout(resolve, 0));

describe('用户路径 · 材料', () => {
  it('粘贴正文：展开材料面板后有一个能写正文的框，写进去会变成待添加的草稿', async () => {
    const { wrapper } = await mountPage();
    await openMaterials(wrapper);

    const box = entryOf(wrapper, act('粘贴正文'));
    expect(box.element.tagName).toBe('TEXTAREA');
    await box.setValue('一段要整理的文字');

    // 草稿落在 store 里（不落在组件里），所以关掉面板也不丢——这是它住在 store 的理由。
    expect(useSourcesStore().draftText).toBe('一段要整理的文字');
  });

  it('添加文本到来源：点一下多出一行材料，并且一个请求都不发', async () => {
    const { wrapper, api } = await mountPage();
    await openMaterials(wrapper);
    const sources = useSourcesStore();
    sources.draftText = '一段要整理的文字';
    await flushPromises();
    const before = api.calls.length;

    await tap(wrapper, act('添加文本到来源'));

    expect(sources.items).toHaveLength(1);
    expect(sources.items[0].state).toBe('ready');
    expect(sources.draftText).toBe('');
    expect(api.calls.length).toBe(before);
  });

  it('上传文档 / 上传图片：材料面板里有文件框，接受这两类', async () => {
    const { wrapper } = await mountPage();
    await openMaterials(wrapper);

    const accept = entryOf(wrapper, act('上传文档')).attributes('accept') ?? '';
    // 两类都要在 accept 里：它们走同一条上传端点，缺一类用户在选择框里就选不到。
    // 这份白名单是**扩展名**而不是 MIME，所以按后缀断言。
    expect(accept).toMatch(/\.pdf|\.docx?/i);
    expect(accept).toMatch(/\.png|\.jpe?g/i);
    expect(entryOf(wrapper, act('上传图片')).element).toBeTruthy();
  });

  it('录音：有一个"录音添加来源"的入口', async () => {
    const { wrapper } = await mountPage();
    await openMaterials(wrapper);

    expect(entryOf(wrapper, act('录音')).element.tagName).toBe('BUTTON');
  });

  it('移除：只把这一行从本轮拿掉，不发删除请求', async () => {
    const { wrapper, api } = await mountPage();
    await openMaterials(wrapper);
    const sources = useSourcesStore();
    sources.items.push(libraryItem());
    await flushPromises();
    const before = api.calls.length;

    await tap(wrapper, act('移除'));

    expect(sources.items).toHaveLength(0);
    // 材料还在知识库里——「移除」发删除请求就等于把两个动作干成了一件事。
    expect(api.calls.filter(call => call.method === 'DELETE')).toHaveLength(0);
    expect(api.calls.length).toBe(before);
  });

  it('清空来源：点了之后列表空了，草稿正文留着', async () => {
    const { wrapper } = await mountPage();
    await openMaterials(wrapper);
    const sources = useSourcesStore();
    sources.draftText = '还没点添加的正文';
    sources.items.push(libraryItem());
    await flushPromises();

    await tap(wrapper, act('清空来源'));

    expect(sources.items).toHaveLength(0);
    expect(sources.draftText).toBe('还没点添加的正文');
  });

  it('彻底删除：已入库的材料发出 DELETE', async () => {
    const { wrapper, api } = await mountPage();
    await openMaterials(wrapper);
    const sources = useSourcesStore();
    sources.items.push(libraryItem());
    await flushPromises();

    await tap(wrapper, act('彻底删除'));
    await settle();

    expect(api.calls.some(call => call.method === 'DELETE'
      && call.url.includes('/knowledge/library/lib-1'))).toBe(true);
    expect(sources.items).toHaveLength(0);
  });
});

describe('用户路径 · 生成', () => {
  it('添加材料 / 管理材料：面板默认收起，两个入口都能把它打开', async () => {
    const { wrapper } = await mountPage();
    const panel = () => wrapper.find('[data-test="material-panel"]');
    // 条件一：挂载后它**在 DOM 里但不可见**（v-show 常驻）。
    expect(panel().exists()).toBe(true);
    expect(visible(panel().element)).toBe(false);

    await tap(wrapper, act('添加材料'));
    expect(visible(panel().element)).toBe(true);

    useSourcesStore().items.push(libraryItem());
    await flushPromises();
    const collapse = wrapper.findAll('button').find(item => item.text().startsWith('收起材料'));
    expect(collapse, '材料有内容时，缩略图那一行应当有一个收起/管理入口').toBeTruthy();
    await collapse?.trigger('click');
    await flushPromises();
    expect(visible(panel().element)).toBe(false);

    // 有了材料之后「管理材料」才存在——它挂在缩略图那一行里。
    await tap(wrapper, act('管理材料'));
    expect(visible(panel().element)).toBe(true);
  });

  it('输入主题 → 发送：发出 POST /agent/runs/stream，请求体带主题', async () => {
    const { wrapper, api } = await mountPage();
    await entryOf(wrapper, act('输入主题')).setValue('机器学习入门');

    await tap(wrapper, act('发送'));

    const sent = api.calls.find(call => call.url.includes('/agent/runs/stream'));
    expect(sent, '写了主题点发送，却没有发出生成请求').toBeTruthy();
    expect(sent?.method).toBe('POST');
    expect(String(sent?.body)).toContain('机器学习入门');
  });

  it('选拆解方式 / 选图型：两个下拉都在，选了之后请求体带上', async () => {
    const { wrapper, api } = await mountPage();

    const thinking = entryOf(wrapper, act('选拆解方式'));
    const diagram = entryOf(wrapper, act('选图型'));
    expect(thinking.element.tagName).toBe('SELECT');
    expect(diagram.element.tagName).toBe('SELECT');

    // 下拉里必须有真选项，否则"能选"只是有个空壳。
    expect(thinking.findAll('option').length).toBeGreaterThan(1);
    expect(diagram.findAll('option').length).toBeGreaterThan(1);

    await thinking.setValue(thinking.findAll('option')[1].attributes('value') ?? '');
    await diagram.setValue(diagram.findAll('option')[1].attributes('value') ?? '');
    await entryOf(wrapper, act('输入主题')).setValue('机器学习入门');
    await tap(wrapper, act('发送'));

    const body = String(api.calls.find(call => call.url.includes('/agent/runs/stream'))?.body);
    expect(body).toContain('thinking_model');
    expect(body).toContain('diagram_type');
  });

  it('取消：生成中那一行状态里有取消，点了之后这一轮被中止', async () => {
    const api = stubApi(call => (call.url.includes('/agent/runs/stream') ? pendingStream() : undefined));
    const { wrapper } = await mountPage({ api });
    const store = useMindMapStore();
    await entryOf(wrapper, act('输入主题')).setValue('机器学习入门');
    await tap(wrapper, act('发送'));
    expect(store.agentRunning).toBe(true);

    await tap(wrapper, act('取消'));

    expect(store.agentRunning).toBe(false);
  });

  it('深度思考 / 智能搜索：两个开关都在，深度思考点了会带上推理模型', async () => {
    const { wrapper, api } = await mountPage();
    expect(entryOf(wrapper, act('深度思考')).element.tagName).toBe('BUTTON');
    expect(entryOf(wrapper, act('智能搜索')).element.tagName).toBe('INPUT');

    await tap(wrapper, act('深度思考'));
    expect(wrapper.get('[data-test="deep-thinking"]').attributes('aria-pressed')).toBe('true');

    await entryOf(wrapper, act('输入主题')).setValue('机器学习入门');
    await tap(wrapper, act('发送'));
    expect(String(api.calls.find(call => call.url.includes('/agent/runs/stream'))?.body)).toContain('deep');
  });
});

describe('用户路径 · 导图', () => {
  it('保存：画布上有图时点保存，发出 POST /mindmaps', async () => {
    const { wrapper, api } = await mountPage();
    const store = useMindMapStore();
    store.preview(SAVED_MAP.content, 'right');
    await openDrawer(wrapper);

    await tap(wrapper, act('保存'));

    const sent = api.calls.find(call => call.url.endsWith('/api/mindmaps') && call.method === 'POST');
    expect(sent, '画布上有图、点了保存，却没有发出创建请求').toBeTruthy();
  });

  it('打开：抽屉里的历史列表点一条，发出 GET /mindmaps/{id}', async () => {
    const { wrapper, api } = await mountPage();
    await openDrawer(wrapper);

    await tap(wrapper, act('打开'));

    expect(api.calls.some(call => call.method === 'GET'
      && call.url.includes(`/mindmaps/${SAVED_MAP.id}`))).toBe(true);
  });

  it('新建：画布清空', async () => {
    const { wrapper } = await mountPage();
    const store = useMindMapStore();
    store.preview(SAVED_MAP.content, 'right');
    await openDrawer(wrapper);

    await tap(wrapper, act('新建'));

    expect(store.content).toBeNull();
  });

  it('删除导图：抽屉里点删除 → 二次确认 → 发出 DELETE /mindmaps/{id}', async () => {
    const { wrapper, api } = await mountPage();
    await openDrawer(wrapper);

    await tap(wrapper, act('删除导图'));
    // 第一下只是进入确认态：不可恢复的动作**不能**在这一下就发请求。
    expect(api.calls.filter(call => call.method === 'DELETE')).toHaveLength(0);
    expect(wrapper.find('[data-test="drawer-map-delete-hint"]').exists()).toBe(true);

    const confirm = wrapper.find('[data-test="drawer-map-delete-confirm"]');
    expect(confirm.exists(), '删导图的确认按钮不存在：第一次点进去没有出口').toBe(true);
    await confirm.trigger('click');
    await settle();

    expect(api.calls.some(call => call.method === 'DELETE'
      && call.url.endsWith(`/mindmaps/${SAVED_MAP.id}`))).toBe(true);
  });

  it('删除导图（列表弹窗）：打开… → 弹窗里删除 → 确认 → 发出 DELETE', async () => {
    const { wrapper, api } = await mountPage();
    await openDrawer(wrapper);

    await tap(wrapper, act('打开列表弹窗'));
    await tap(wrapper, act('删除导图（列表弹窗）'));
    expect(api.calls.filter(call => call.method === 'DELETE')).toHaveLength(0);

    const confirm = wrapper.find('[data-test="map-delete-confirm"]');
    expect(confirm.exists()).toBe(true);
    await confirm.trigger('click');
    await settle();

    expect(api.calls.some(call => call.method === 'DELETE'
      && call.url.endsWith(`/mindmaps/${SAVED_MAP.id}`))).toBe(true);
  });
});

describe('清单本身', () => {
  it('每条动作都被某条用例走到过（防止清单与用例各走各的）', () => {
    expect(catalogGaps(ACTIONS)).toEqual([]);
  });
});
