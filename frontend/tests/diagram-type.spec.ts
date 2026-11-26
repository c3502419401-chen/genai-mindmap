import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { DiagramTypeChoice, MindMapContent, Source } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';
import { THINKING_MODEL_OPTIONS } from '../src/stores/options';
import { isPrefsRequest, prefsReply } from './helpers/prefs-stub';

/**
 * 「拆解方式」与「图型」从界面到请求体，以及"选了之后到底改了什么"那一行说明。
 *
 * bug 的现场是：用户说"画个流程图"，生成出来的还是树——因为前端根本不发图型，
 * 模型也没回头去设工具参数。这组用例把三条钉住：
 *   1. 选择器存在、四个值、默认「自动判断」；
 *   2. 选了就随生成发出去，没选就**不带这个字段**；
 *   3. 说明文字跟着选项变（选流程图/鱼骨图时不能还是那句泛泛的话）。
 *
 * 这两个下拉在合并之后搬到了画布右上角那个框里（与「生成导图」按钮同一个区块）：
 * 用户实测 12 次生成里 0 次点过图型下拉——它在左侧材料堆里，看不出跟生成有关。
 */

vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

const INTENT = 'textarea#intent';
const SUBMIT = '[data-test="intent-box"] button';

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

function stubFetch(reply: () => Response) {
  const calls: Array<{ url: string; body: unknown }> = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    // 偏好那两个请求是挂载时的背景动作（生成面板要带着偏好预填），不属于这里
    // 被测的"选了图型之后请求体长什么样"，所以不进 calls。
    if (isPrefsRequest(url)) return prefsReply(url);
    if (url.endsWith('/health')) {
      // 挂载时探一次版本号。它是背景请求，和"点这个入口发出了什么"无关。
      return new Response(JSON.stringify({ status: 'ok', version: 'test' }), {
        status: 200, headers: { 'content-type': 'application/json' },
      });
    }
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
    return reply();
  }));
  return calls;
}

const doneFrame = (payload: MindMapContent) => ({ type: 'done', seq: 4, content: payload });

/** 输入区里那个下拉：按 id 取，按序号取会随控件增减而错位。 */
function diagramSelect(wrapper: VueWrapper) {
  return wrapper.get('#diagram-type');
}

function mountApp(): VueWrapper {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(App, { global: { plugins: [pinia] } });
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('图型选择器', () => {
  it('四个选项、按顺序、默认自动判断', () => {
    const wrapper = mountApp();
    const select = diagramSelect(wrapper);
    const options = select.findAll('option');

    expect(options.map(option => option.attributes('value'))).toEqual([
      'auto', 'tree', 'flowchart', 'fishbone',
    ]);
    expect(options.map(option => option.text())).toEqual([
      '自动判断', '思维导图', '流程图', '鱼骨图',
    ]);
    // 「自动判断」是默认：用户没说，就让生成那一次决定（后端 None）
    expect((select.element as HTMLSelectElement).value).toBe('auto');
    expect(select.attributes('disabled')).toBeUndefined();
    // 它就在生成按钮上面那一块：选择器和消费它的动作必须挨着
    expect(wrapper.get('[data-test="intent-box"]').find('#diagram-type').exists()).toBe(true);
  });

  it('生成期间禁用：改了也看不到生效，那就不许改', async () => {
    const wrapper = mountApp();
    useMindMapStore().busy = true;
    await wrapper.vm.$nextTick();
    // 生成中整个对话框收成一条细进度（那一版是这一轮的核心交互），参数连渲染都不渲染——
    // 比"灰着但还能看见"更彻底：此刻改了也确实不会生效。
    expect(wrapper.find('#diagram-type').exists()).toBe(false);
    expect(wrapper.find('#thinking-model').exists()).toBe(false);
    expect(wrapper.find('[data-test="run-status"]').exists()).toBe(true);
  });

  it('已有导图（edit 场景）时不渲染：那条路不消费图型', async () => {
    const wrapper = mountApp();
    expect(wrapper.find('#diagram-type').exists()).toBe(true);

    useMindMapStore().preview(content('原图'), 'right');
    await wrapper.vm.$nextTick();

    expect(wrapper.find('#diagram-type').exists()).toBe(false);
    expect(wrapper.find('#thinking-model').exists()).toBe(false);
  });
});

describe('拆解方式选择器', () => {
  it('下拉照原样列出后端那张表，默认选中通用，并说明它改了什么', () => {
    const wrapper = mountApp();
    const select = wrapper.get('#thinking-model');
    const options = select.findAll('option');
    // 期望值**不写在这里**：候选来自 `api/thinking-options.json`（后端生成物）。
    // 这里钉的是"组件把整张表一条不漏、按顺序渲染出来"，不是表的第二份副本——
    // 副本正是这一轮修掉的病（后端加一种思考方式、界面不报错也不变）。
    expect(options.map(option => option.attributes('value'))).toEqual(
      THINKING_MODEL_OPTIONS.map(item => item.value),
    );
    expect(options.map(option => option.text())).toEqual(
      THINKING_MODEL_OPTIONS.map(item => item.label),
    );
    expect(options.length).toBe(THINKING_MODEL_OPTIONS.length);
    expect((select.element as HTMLSelectElement).value).toBe('default');
    // 旁边那一行短说明：用户得知道选了它会影响什么
    expect(wrapper.get('[data-test="intent-box"]').text()).toContain('按这种方式拆解主题');
  });

  it('选了就随 create 请求发出去', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const wrapper = mountApp();

    await wrapper.get('#thinking-model').setValue('first-principles');
    await wrapper.get(INTENT).setValue('做一张图');
    await wrapper.get(SUBMIT).trigger('click');
    await vi.waitFor(() => expect(calls).toHaveLength(1));

    expect((calls[0].body as Record<string, unknown>).thinking_model).toBe('first-principles');
  });
});

describe('图型说明文字', () => {
  it('切换选项时说明跟着变，且指出这个图型具体改了什么', async () => {
    const wrapper = mountApp();
    const select = diagramSelect(wrapper);

    // 默认那一句：说清"不选"会发生什么
    expect(wrapper.text()).toContain('按你的描述判断图型');

    await select.setValue('flowchart');
    expect(wrapper.text()).toContain('一级分支会按执行阶段排，判断节点带是/否分支');

    await select.setValue('fishbone');
    expect(wrapper.text()).toContain('一级分支固定为人/机/料/法/环/测');

    await select.setValue('tree');
    expect(wrapper.text()).toContain('一级分支按话题分解');
    // 说明是**替换**而不是叠加：三句同时出现等于什么都没说
    expect(wrapper.text()).not.toContain('一级分支固定为人/机/料/法/环/测');
  });
});

describe('图型进入请求体', () => {
  it('选了流程图就带上 diagram_type=flowchart', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const wrapper = mountApp();

    await wrapper.get(INTENT).setValue('画个报销流程图');
    await diagramSelect(wrapper).setValue('flowchart');
    await wrapper.get(SUBMIT).trigger('click');
    await vi.waitFor(() => expect(calls).toHaveLength(1));

    const body = calls[0].body as Record<string, unknown>;
    expect(body.diagram_type).toBe('flowchart');
    expect(body.intent).toBe('create');
  });

  it('「自动判断」不带这个字段（省略 = 让模型自己判断）', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const wrapper = mountApp();

    await wrapper.get(INTENT).setValue('画个报销流程图');
    await wrapper.get(SUBMIT).trigger('click');
    await vi.waitFor(() => expect(calls).toHaveLength(1));

    // 不发 'auto' 这种假值：后端的 Literal 只认三个图型，省略才是"没说"
    expect('diagram_type' in (calls[0].body as Record<string, unknown>)).toBe(false);
  });

  it.each<DiagramTypeChoice>(['tree', 'flowchart', 'fishbone'])(
    '三种图型都能原样发出去：%s',
    async (choice) => {
      const calls = stubFetch(() => agentReply([doneFrame(content())]));
      const store = useMindMapStore();

      await store.runGeneration('做一张图', 'create', undefined, 'default', choice);

      expect((calls[0].body as Record<string, unknown>).diagram_type).toBe(choice);
    },
  );

  it('edit 意图不带图型：那条路不生成骨架', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content('改后'))]));
    const store = useMindMapStore();
    store.preview(content(), 'right');

    await store.runGeneration('把甲改成乙', 'edit', undefined, 'default', 'flowchart');

    expect('diagram_type' in (calls[0].body as Record<string, unknown>)).toBe(false);
  });

  it('expand 意图也不带：它只加子节点，没有骨架可定', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const store = useMindMapStore();
    store.preview(content(), 'right');
    store.select('a');

    await store.runGeneration('展开甲', 'expand', undefined, 'default', 'fishbone');

    expect('diagram_type' in (calls[0].body as Record<string, unknown>)).toBe(false);
  });
});

describe('图型与来源一起发', () => {
  it('带材料时两个字段同时出现，互不覆盖', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const store = useMindMapStore();
    const source: Source = {
      id: 's1',
      kind: 'text',
      filename: null,
      text: '材料正文',
      span: null,
      status: 'parsed',
      error: null,
      parsed_pages: null,
      truncated: false,
      failed_pages: [],
    };

    await store.runGeneration('按材料画流程', 'create', [source], 'default', 'flowchart');

    const body = calls[0].body as Record<string, unknown>;
    expect(body.diagram_type).toBe('flowchart');
    expect(body.sources).toHaveLength(1);
  });
});
