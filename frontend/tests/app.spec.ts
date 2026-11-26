import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia } from 'pinia';
import { afterEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { LibraryEntry, LibraryList, LibrarySource, MindMapContent } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';
import { useSourcesStore } from '../src/stores/sources';
import { isPrefsRequest, prefsReply } from './helpers/prefs-stub';

// jsdom 里没有 ResizeObserver，真正的 Vue Flow 画布跑不起来；
// 界面测试只关心输入区与详情面板，画布替换成占位元素。
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

/** 构造一个假的 SSE 响应：把若干 JSON 事件包成 `data: ...\n\n` 塞进 ReadableStream。 */
function streamReply(frames: unknown[], status = 200): Response {
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
  return { ok: status < 400, status, body, json: async () => ({}) } as unknown as Response;
}

const CONTENT: MindMapContent = {
  title: '机器学习',
  root: {
    id: 'r',
    label: '机器学习',
    children: [
      { id: 'c', label: '监督学习', children: [{ id: 'g', label: '梯度下降', children: [] }] },
    ],
  },
};

/** agent 的收尾帧：`mode` 说明这棵树是演示规则拼的还是模型产出的。 */
const doneFrame = (mode: 'demo' | 'model') => ({ type: 'done', seq: 2, content: CONTENT, mode });

function mountApp() {
  return mount(App, { global: { plugins: [createPinia()] } });
}

/**
 * 只数业务请求。
 *
 * 页面挂载时会探一次 `/api/health` 取版本号；断言"不该发请求"的用例关心的是
 * 生成、保存这类业务请求，把它算进去只会得到一个假失败。
 */
function apiCalls(fetchMock: ReturnType<typeof vi.fn>) {
  return fetchMock.mock.calls.filter(([url]) => {
    const u = String(url);
    return !u.endsWith('/health') && !isPrefsRequest(u);
  });
}

/**
 * 左侧输入区也有一个 textarea，按标签定位详情面板里的备注框。
 *
 * 标签从「节点正文」改成「我的备注」是这一轮的分区结果：`detail` 是生成产出的
 * 详情（只读），`note` 是用户自己写的备注。两者共用"正文"这个词会让用户以为
 * 自己写的那段会被重新生成覆盖。
 */
const NOTE_TEXTAREA = 'textarea[aria-label="我的备注"]';

/**
 * 画布右上角那个框（合并后的 #2）：**唯一**说一句话的地方。
 *
 * 按 id 定位而不是 `get('textarea')`：左侧材料区还有一个「粘贴材料正文」的
 * textarea，按标签取会随组件顺序拿到错误的那一个。
 */
const INTENT = 'textarea#intent';

/**
 * 按文字找按钮：开发期示例选择器会异步插入一排按钮，
 * 用 `findAll('button')[0]` 会随加载时机拿到不同的东西。
 */
function buttonByText(wrapper: VueWrapper, text: string) {
  const found = wrapper.findAll('button').find(item => item.text().includes(text));
  if (!found) throw new Error(`找不到按钮：${text}`);
  return found;
}

/** 这次被测动作真的打出去的 URL（偏好那几个背景请求不算）。 */
/**
 * 只数业务请求：挂载时的 `/api/prefs` 与 `/api/health` 都是背景动作。
 *
 * `/health` 是这一轮合并进来的（顶栏要显示后端版本号），不算业务请求——
 * 不排除它，"没有发出生成请求"这类断言会被它顶成假失败。
 */
function agentUrls(mock: ReturnType<typeof vi.fn>): string[] {
  return mock.mock.calls
    .map(call => String(call[0]))
    .filter(url => !isPrefsRequest(url) && !url.endsWith('/health'));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('App', () => {
  it('空输入时按钮是禁用的，并说明「要写一个主题」', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (isPrefsRequest(url)) return prefsReply(url);
      return streamReply([]);
    });
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mountApp();

    // 没主题、没材料：按钮点不动，原因写在那行提示里。合并之前这里是"点得下去、
    // 然后弹一句「请在下方输入主题或文本」"——那个提示指向的框现在已经不存在了。
    const submit = wrapper.get('[data-test="intent-box"] button');
    expect(submit.attributes('disabled')).toBeDefined();
    await submit.trigger('click');

    // 挂载时会拉一次偏好（生成面板的两个下拉要带着它），所以断言的粒度是
    // "没有发出生成请求"，而不是"一次 fetch 都没有"。
    expect(agentUrls(fetchMock)).toEqual([]);
    expect(wrapper.get('[data-test="intent-box"]').text()).toContain('写一个主题，或者点「+」添加材料');  });

  it('点生成把请求打到 agent 路由，结果直接落画布', async () => {
    const fetchMock = vi.fn(async () => streamReply([
      { type: 'stage', seq: 1, stage: '准备' },
      doneFrame('model'),
    ]));
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mountApp();

    await wrapper.get(INTENT).setValue('机器学习');
    await buttonByText(wrapper, '生成导图').trigger('click');

    // 生成只剩这一条通道
    await vi.waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith('/api/agent/runs/stream', expect.anything()));
    // 没有「应用」这一步了：done 一到树就进画布，并标成未保存
    await vi.waitFor(() => expect(useMindMapStore().content).not.toBeNull());
    expect(useMindMapStore().dirty).toBe(true);
    expect(wrapper.text()).not.toContain('应用到画布');
  });

  it.each([
    { mode: 'model' as const, badge: '模型生成' },
    { mode: 'demo' as const, badge: '演示模式（非 AI 生成）' },
  ])('落地后按 done 帧的 $mode 显示来源徽标', async ({ mode, badge }) => {
    // 演示模式产出的是一棵占位树，界面不能替它贴上"模型生成"。
    // 来源只有 done 帧说得准，所以整条链路（帧 → 落地 → 徽标）一起验。
    vi.stubGlobal('fetch', vi.fn(async () => streamReply([doneFrame(mode)])));
    const wrapper = mountApp();

    await wrapper.get(INTENT).setValue('机器学习');
    await buttonByText(wrapper, '生成导图').trigger('click');

    await vi.waitFor(() => expect(wrapper.text()).toContain('3 个节点'));
    expect(wrapper.text()).toContain(badge);
  });

  it('没有选中节点时详情面板给出说明', () => {
    const wrapper = mountApp();
    expect(wrapper.text()).toContain('点击画布上的任意节点');
  });

  it('后端返回非 2xx 时展示 message 文案', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 502,
        body: null,
        json: async () => ({ code: 'http_502', message: '模型服务暂时不可用' }),
      }) as unknown as Response),
    );
    const wrapper = mountApp();

    await wrapper.get(INTENT).setValue('x');
    await buttonByText(wrapper, '生成导图').trigger('click');
    await vi.waitFor(() =>
      expect(wrapper.get('[role="status"]').text()).toContain('模型服务暂时不可用'));
  });

  it('流里出现 error 事件时显示消息且不崩', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        streamReply([{ type: 'error', seq: 1, code: 'generation_failed', message: '模型返回超时' }])),
    );
    const wrapper = mountApp();

    await wrapper.get(INTENT).setValue('x');
    await buttonByText(wrapper, '生成导图').trigger('click');
    await vi.waitFor(() =>
      expect(wrapper.get('[role="status"]').text()).toContain('模型返回超时'));
  });

  it('开发期示例按钮不经过后端就能装载导图', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (isPrefsRequest(url)) return prefsReply(url);
      return streamReply([]);
    });
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mountApp();

    await vi.waitFor(() => expect(buttonByText(wrapper, '单节点')).toBeTruthy());
    await buttonByText(wrapper, '单节点').trigger('click');

    // 示例装载走的是本地 `dev/samples`，一条生成/保存请求都不该有。
    expect(agentUrls(fetchMock)).toEqual([]);    expect(wrapper.text()).toContain('1 个节点');
    expect(wrapper.text()).toContain('演示模式');
  });

  it('点保存把当前导图发给后端，成功后显示版本号', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 201,
      json: async () => ({
        id: 'm1',
        version: 1,
        content: { title: '单节点', root: { id: 's', label: '单节点', children: [] } },
        updated_at: '2026-11-04T00:00:00Z',
      }),
    }) as unknown as Response);
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mountApp();

    await vi.waitFor(() => expect(buttonByText(wrapper, '单节点')).toBeTruthy());
    await buttonByText(wrapper, '单节点').trigger('click');
    await buttonByText(wrapper, '保存').trigger('click');

    await vi.waitFor(() =>
      expect(wrapper.get('[role="status"]').text()).toContain('已保存，版本 1'));
    expect(fetchMock).toHaveBeenCalledWith('/api/mindmaps', expect.objectContaining({ method: 'POST' }));
  });

  it('Ctrl+S 保存前让正在输入的控件失焦，草稿才会被提交', async () => {
    // 点「保存」按钮时浏览器已经让输入框失焦了，问题只出在键盘快捷键：
    // Ctrl+S 不会触发 blur，草稿留在输入框里，保存发出去的是旧内容，
    // 界面却显示"已保存"。
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 201,
      json: async () => ({
        id: 'm1', version: 1,
        content: { title: '单节点', root: { id: 's', label: '单节点', children: [] } },
        updated_at: '2026-11-04T00:00:00Z',
      }),
    })) as unknown as typeof fetch);
    const wrapper = mountApp();

    await vi.waitFor(() => expect(buttonByText(wrapper, '单节点')).toBeTruthy());
    await buttonByText(wrapper, '单节点').trigger('click');

    const titleInput = wrapper.get('#title');
    (titleInput.element as HTMLInputElement).focus();
    // jsdom 的 activeElement 需要显式设置：focus() 在无头环境里不一定生效
    Object.defineProperty(document, 'activeElement', {
      value: titleInput.element, configurable: true,
    });
    const blurSpy = vi.spyOn(titleInput.element as HTMLInputElement, 'blur');

    window.dispatchEvent(new KeyboardEvent('keydown', {
      key: 's', ctrlKey: true, bubbles: true, cancelable: true,
    }));

    expect(blurSpy).toHaveBeenCalled();
  });

  it('打开列表列出已保存的导图，点开后进入该导图', async () => {
    const record = {
      id: 'm9',
      version: 2,
      content: {
        title: '打开这张',
        root: {
          id: 'r',
          label: '打开这张',
          children: [{ id: 'a', label: '甲', children: [] }],
        },
      },
      updated_at: '2026-11-04T03:00:00Z',
    };
    const fetchMock = vi.fn(async (url: string) => ({
      ok: true,
      status: 200,
      json: async () => (url === '/api/mindmaps' ? [record] : record),
    }) as unknown as Response);
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mountApp();

    await buttonByText(wrapper, '打开').trigger('click');
    await vi.waitFor(() => expect(wrapper.text()).toContain('打开这张'));

    await wrapper.get('ul li button').trigger('click');
    await vi.waitFor(() =>
      expect(wrapper.get('[role="status"]').text()).toContain('已打开导图，版本 2'));
    expect(wrapper.text()).toContain('2 个节点');
  });
});

describe('节点引用', () => {
  const withRefs = (sourceRefs: { source_id: string; page?: number; start?: number; end?: number; quote?: string }[]) => ({
    title: '带引用',
    root: {
      id: 'r',
      label: '根',
      children: [{ id: 'a', label: '甲', children: [], source_refs: sourceRefs }],
    },
  });

  it('选中带引用的节点时，详情面板列出它的引用', async () => {
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(withRefs([{ source_id: 's1', page: 2, quote: '矩阵的秩' }]), 'right');
    store.select('a');
    await wrapper.vm.$nextTick();

    expect(wrapper.text()).toContain('来源引用（1）');
    expect(wrapper.text()).toContain('第 2 页');
    expect(wrapper.text()).toContain('矩阵的秩');
  });

  it('没有引用的节点明确说明没有，并给出全图覆盖率（不是一个坏掉的功能）', async () => {
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(withRefs([]), 'right');
    store.select('r');
    await wrapper.vm.$nextTick();

    const empty = wrapper.get('[data-test="ref-empty"]');
    expect(empty.text()).toContain('这个节点没有原文引用');
    // 全图 0 个节点有引用 → 说清是"没有可回查的材料"，而不是让用户去点空白
    expect(empty.text()).toContain('都没有原文引用');
  });

  it('点引用打开右侧抽屉；来源不在知识库里时说明只能看摘录', async () => {
    // 抽屉会按 `source_id` 去知识库取原文——这份临时来源（`s1`）不在库里，后端 404。
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false,
      status: 404,
      json: async () => ({ code: 'not_found', message: '这份来源不在知识库里' }),
    })));
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(withRefs([{ source_id: 's1', page: 2, quote: '矩阵的秩' }]), 'right');
    store.select('a');
    await wrapper.vm.$nextTick();

    await buttonByText(wrapper, '矩阵的秩').trigger('click');
    await flushPromises();
    const drawer = wrapper.find('[data-test="ref-drawer"]');
    expect(drawer.exists()).toBe(true);
    // 摘录始终看得见：取不到原文不该连这句话一起消失。
    expect(drawer.text()).toContain('矩阵的秩');
    expect(drawer.find('[data-test="ref-note"]').text()).toContain('原文没能读取');
  });

  it('本次会话解析过的来源能在抽屉里高亮命中句', async () => {
    const wrapper = mountApp();
    const sources = useSourcesStore();
    sources.addText('矩阵的秩等于行向量组的极大线性无关组的个数。');
    const sourceId = sources.readySources[0].id;

    const store = useMindMapStore();
    store.preview(withRefs([{ source_id: sourceId, start: 0, end: 6, quote: '矩阵的秩等于' }]), 'right');
    store.select('a');
    await wrapper.vm.$nextTick();

    await buttonByText(wrapper, '矩阵的秩等于').trigger('click');
    const drawer = wrapper.find('[data-test="ref-drawer"]');
    // ① 先看到那句引用本身（不点上下文也有）。
    expect(drawer.text()).toContain('矩阵的秩等于');

    // ② 点「看上下文」才展开高亮——分两步是为了"先确认是不是这句"。
    await drawer.get('[data-test="ref-context-toggle"]').trigger('click');
    await flushPromises();
    expect(drawer.find('mark').text()).toBe('矩阵的秩等于');
    expect(drawer.find('[data-test="ref-note"]').exists()).toBe(false);
  });
});

describe('导出提示', () => {
  const CONTENT = {
    title: '季度规划',
    root: { id: 'r', label: '产品', children: [] },
  };

  /** 真实导出会建 blob 链接并点一次 <a>，jsdom 没有这两样。 */
  function stubDownload() {
    Object.defineProperty(URL, 'createObjectURL', { value: () => 'blob:stub', configurable: true });
    Object.defineProperty(URL, 'revokeObjectURL', { value: () => {}, configurable: true });
  }

  async function pickFormat(wrapper: VueWrapper, format: string) {
    await wrapper.find('select[title="导出为文本格式"]').setValue(format);
    // onExport 是 async：请求落地、浮层渲染都要等一轮微任务。
    await flushPromises();
  }

  it('后端拒绝时底部浮层给出后端文案，而不是只有左侧小字', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 422,
      json: async () => ({ code: 'validation_error', message: '请求参数校验失败' }),
    }));
    const wrapper = mountApp();
    useMindMapStore().preview(CONTENT, 'right');
    await wrapper.vm.$nextTick();

    await pickFormat(wrapper, 'markdown');

    const alert = wrapper.find('[role="alert"]');
    expect(alert.exists()).toBe(true);
    expect(alert.text()).toContain('请求参数校验失败');
  });

  it('导出成功时给出成功浮层', async () => {
    stubDownload();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => '@startmindmap\n@endmindmap\n',
    }));
    const wrapper = mountApp();
    useMindMapStore().preview(CONTENT, 'right');
    await wrapper.vm.$nextTick();

    await pickFormat(wrapper, 'puml');

    const status = wrapper.find('[role="status"]');
    expect(status.exists()).toBe(true);
    expect(wrapper.text()).toContain('已导出 PlantUML');
  });
});

describe('正文编辑', () => {
  const mapWith = (note?: string) => ({
    title: '正文',
    root: {
      id: 'r',
      label: '根',
      children: [{ id: 'a', label: '甲', note, children: [] }],
    },
  });

  it('选中节点后正文在输入框里可以改，失焦即提交', async () => {
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(mapWith(), 'right');
    store.select('a');
    await wrapper.vm.$nextTick();

    const textarea = wrapper.find(NOTE_TEXTAREA);
    expect(textarea.exists()).toBe(true);
    expect((textarea.element as HTMLTextAreaElement).value).toBe('');

    await textarea.setValue('矩阵的秩是主元个数');
    await textarea.trigger('blur');
    await wrapper.vm.$nextTick();

    expect(store.content?.root.children[0].note).toBe('矩阵的秩是主元个数');
    expect(store.dirty).toBe(true);
  });

  it('已有正文时打开就把正文填进去，并显示字数', async () => {
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(mapWith('已经写过的正文'), 'right');
    store.select('a');
    await wrapper.vm.$nextTick();

    const textarea = wrapper.find(NOTE_TEXTAREA);
    expect((textarea.element as HTMLTextAreaElement).value).toBe('已经写过的正文');
    expect(wrapper.text()).toContain('我的备注 7 / 2000');
  });

  it('按 Esc 放弃这次输入，回到原来的正文', async () => {
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(mapWith('原来的正文'), 'right');
    store.select('a');
    await wrapper.vm.$nextTick();

    const textarea = wrapper.find(NOTE_TEXTAREA);
    await textarea.setValue('改了一半');
    await textarea.trigger('keydown', { key: 'Escape' });
    await wrapper.vm.$nextTick();

    expect((wrapper.find(NOTE_TEXTAREA).element as HTMLTextAreaElement).value).toBe('原来的正文');
    expect(store.content?.root.children[0].note).toBe('原来的正文');
  });
});

describe('演示预置导图', () => {
  /** 只要不带 health 的请求就算业务请求，这里一条都不该有。 */
  function stubOffline() {
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ status: 'ok', version: 'dev' }) }) as unknown as Response);
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  it('点「演示示例」列出三张预置图，载入时不请求后端', async () => {
    const fetchMock = stubOffline();
    const wrapper = mountApp();
    await flushPromises();

    await buttonByText(wrapper, '演示示例').trigger('click');
    // 预置内容是动态 import 进来的，要多等一拍才知道弹层里有什么。
    await vi.waitFor(() => expect(wrapper.text()).toContain('Rust 学习路线'));
    expect(wrapper.text()).toContain('RAG 方案选型');
    expect(wrapper.text()).toContain('一次产品评审');

    const loadButtons = wrapper.findAll('button').filter(item => item.text() === '载入');
    expect(loadButtons).toHaveLength(3);
    await loadButtons[0].trigger('click');
    await flushPromises();

    const store = useMindMapStore();
    expect(store.content?.title).toBe('Rust 学习路线');
    expect(store.nodeCount).toBeGreaterThan(20);
    expect(store.status).toContain('已载入预置演示导图');
    // 预置图不落盘、也不能凭空造出记录身份：第一次保存必须是新建。
    expect(store.mapId).toBeNull();
    expect(apiCalls(fetchMock)).toHaveLength(0);
  });

  it('不同预置图用不同朝向，双向那张排成左右两列', async () => {
    stubOffline();
    const wrapper = mountApp();
    await flushPromises();

    await buttonByText(wrapper, '演示示例').trigger('click');
    await vi.waitFor(() => expect(wrapper.findAll('button').filter(item => item.text() === '载入')).toHaveLength(3));
    const loadButtons = wrapper.findAll('button').filter(item => item.text() === '载入');
    await loadButtons[2].trigger('click');
    await flushPromises();

    const store = useMindMapStore();
    expect(store.content?.title).toBe('一次产品评审的结论');
    expect(store.direction).toBe('both');
  });

  it('模型失败时不会自动载入预置图', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (String(url).endsWith('/api/health')) {
        return { ok: true, status: 200, json: async () => ({ status: 'ok', version: 'dev' }) } as unknown as Response;
      }
      return streamReply([{ type: 'stage', seq: 1, stage: '准备', detail: 'create 意图' }, { type: 'done', seq: 2, content: null, mode: 'model' }]);
    });
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mountApp();
    await flushPromises();

    await wrapper.find('textarea').setValue('随便来点');
    await buttonByText(wrapper, '生成导图').trigger('click');
    await flushPromises();

    const store = useMindMapStore();
    expect(store.content).toBeNull();
    expect(wrapper.text()).toContain('演示示例');
    expect(store.status).not.toContain('已载入预置演示导图');
  });
});

describe('版本徽章', () => {
  function stubHealth(body: unknown) {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, status: 200, json: async () => body }) as unknown as Response),
    );
  }

  it('标题旁显示后端版本号，tag 的 v 前缀不会重复', async () => {
    stubHealth({ status: 'ok', version: 'v0.4.7' });
    const wrapper = mountApp();
    await flushPromises();

    expect(wrapper.text()).toContain('v0.4.7');
    expect(wrapper.text()).not.toContain('vv0.4.7');
  });

  it('本机 dev 版原样显示 dev', async () => {
    stubHealth({ status: 'ok', version: 'dev' });
    const wrapper = mountApp();
    await flushPromises();

    expect(wrapper.text()).toContain('dev');
  });

  it('后端连不上时不显示徽章，也不额外报错', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('offline');
      }),
    );
    const wrapper = mountApp();
    await flushPromises();

    expect(wrapper.find('header').text()).not.toContain('dev');
  });
});

describe('知识库入口', () => {
  const entry = (overrides: Partial<LibraryEntry> = {}): LibraryEntry => ({
    source_id: 'src-1',
    filename: '管理办法.pdf',
    kind: 'document',
    chars: 1200,
    chunks: 8,
    created_at: '2026-11-16T10:00:00.000Z',
    last_used_at: '2026-11-16T11:00:00.000Z',
    cited_by: 2,
    ...overrides,
  });

  /** 按路径路由：列表与正文预览是两个不同的 GET。 */
  function stubLibrary(list: LibraryList, source?: LibrarySource) {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/health')) {
        return { ok: true, status: 200, json: async () => ({ status: 'ok', version: 'dev' }) } as unknown as Response;
      }
      if (url.includes('/knowledge/library/') && source) {
        return { ok: true, status: 200, json: async () => source } as unknown as Response;
      }
      if (url.includes('/knowledge/library')) {
        return { ok: true, status: 200, json: async () => list } as unknown as Response;
      }
      return { ok: true, status: 200, json: async () => ({}) } as unknown as Response;
    });
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  it('点「知识库」列出材料，被引用次数与未被引用都写清楚', async () => {
    stubLibrary({
      total: 2,
      items: [entry(), entry({ source_id: 'src-2', filename: '旧笔记.md', kind: 'markdown', cited_by: 0 })],
    });
    const wrapper = mountApp();
    await flushPromises();

    await buttonByText(wrapper, '知识库').trigger('click');
    await vi.waitFor(() => expect(wrapper.text()).toContain('管理办法.pdf'));

    expect(wrapper.text()).toContain('被 2 张导图引用');
    expect(wrapper.text()).toContain('还没有导图引用它');
    expect(wrapper.text()).toContain('旧笔记.md');
    // 总数来自后端，不是当前列表长度：断言整句，免得页面上任何一个 "2" 都能过。
    expect(wrapper.text()).toMatch(/库里现在有\s*2\s*份/);
  });

  it('库里超过一页时说明只显示了最近几份', async () => {
    stubLibrary({
      total: 137,
      items: [entry(), entry({ source_id: 'src-2', filename: '旧笔记.md', cited_by: 0 })],
    });
    const wrapper = mountApp();
    await flushPromises();

    await buttonByText(wrapper, '知识库').trigger('click');
    await vi.waitFor(() => expect(wrapper.text()).toContain('管理办法.pdf'));

    // 137 份只显示了 2 条：必须说清楚，否则用户会以为材料丢了。
    expect(wrapper.text()).toMatch(/库里现在有\s*137\s*份/);
    expect(wrapper.text()).toMatch(/这里只显示最近\s*2\s*份/);
  });

  it('Esc 能关掉知识库抽屉，点遮罩也能关', async () => {
    stubLibrary({ total: 1, items: [entry()] });
    const wrapper = mountApp();
    await flushPromises();

    await buttonByText(wrapper, '知识库').trigger('click');
    await vi.waitFor(() => expect(wrapper.text()).toContain('管理办法.pdf'));

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await flushPromises();
    expect(wrapper.find('[aria-labelledby="library-title"]').exists()).toBe(false);

    await buttonByText(wrapper, '知识库').trigger('click');
    await vi.waitFor(() => expect(wrapper.text()).toContain('管理办法.pdf'));

    // 声明成模态（aria-modal）就必须有一条"点外面能关"的路。
    const backdrop = wrapper.findAll('div[aria-hidden="true"]')
      .find(node => node.classes().includes('inset-0'));
    expect(backdrop).toBeTruthy();
    await backdrop!.trigger('click');
    await flushPromises();
    expect(wrapper.find('[aria-labelledby="library-title"]').exists()).toBe(false);
  });

  it('列表标出「最近使用」，创建时间放在 title 里', async () => {
    stubLibrary({ total: 1, items: [entry()] });
    const wrapper = mountApp();
    await flushPromises();

    await buttonByText(wrapper, '知识库').trigger('click');
    await vi.waitFor(() => expect(wrapper.text()).toContain('管理办法.pdf'));

    // 只写一个时间戳会被读成创建时间；这里钉住"最近使用"这几个字与完整 title。
    expect(wrapper.text()).toContain('最近使用');
    const stamped = wrapper.find('[title*="最近使用"]');
    expect(stamped.exists()).toBe(true);
    expect(stamped.attributes('title')).toContain('收录');
  });

  it('点一份材料看正文预览，截断时说明只显示前 4000 字', async () => {
    stubLibrary(
      { total: 1, items: [entry()] },
      {
        source_id: 'src-1',
        filename: '管理办法.pdf',
        kind: 'document',
        text: '第一章 总则',
        truncated: true,
        total_chars: 12000,
      },
    );
    const wrapper = mountApp();
    await flushPromises();

    await buttonByText(wrapper, '知识库').trigger('click');
    await vi.waitFor(() => expect(wrapper.text()).toContain('管理办法.pdf'));
    await buttonByText(wrapper, '管理办法.pdf').trigger('click');
    await vi.waitFor(() => expect(wrapper.text()).toContain('第一章 总则'));

    expect(wrapper.text()).toContain('只显示前 4000 字');
    expect(wrapper.text()).toContain('12000');

    await buttonByText(wrapper, '回到列表').trigger('click');
    await flushPromises();
    expect(wrapper.text()).not.toContain('第一章 总则');
  });

  it('正文读不出来时只影响这一份：列表还在，能重试也能退回', async () => {
    let previewCalls = 0;
    const list: LibraryList = { total: 1, items: [entry()] };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/health')) {
        return { ok: true, status: 200, json: async () => ({ status: 'ok', version: 'dev' }) } as unknown as Response;
      }
      if (url.includes('/knowledge/library/')) {
        previewCalls += 1;
        if (previewCalls === 1) {
          return { ok: false, status: 503, json: async () => ({ code: 'library_unavailable', message: '知识库暂时读不出来' }) } as unknown as Response;
        }
        return {
          ok: true,
          status: 200,
          json: async () => ({
            source_id: 'src-1',
            filename: '管理办法.pdf',
            kind: 'document',
            text: '第一章 总则',
            truncated: false,
            total_chars: 6,
          }),
        } as unknown as Response;
      }
      return { ok: true, status: 200, json: async () => list } as unknown as Response;
    }));
    const wrapper = mountApp();
    await flushPromises();

    await buttonByText(wrapper, '知识库').trigger('click');
    await vi.waitFor(() => expect(wrapper.text()).toContain('管理办法.pdf'));
    await buttonByText(wrapper, '管理办法.pdf').trigger('click');
    await vi.waitFor(() => expect(wrapper.text()).toContain('正文没能读取'));

    // 抽屉停在预览壳里：能退回列表，也能原地重试，不必关掉重开
    expect(wrapper.text()).toContain('回到列表');
    expect(wrapper.text()).toContain('再试一次');

    await buttonByText(wrapper, '再试一次').trigger('click');
    await vi.waitFor(() => expect(wrapper.text()).toContain('第一章 总则'));
    expect(previewCalls).toBe(2);

    await buttonByText(wrapper, '回到列表').trigger('click');
    await flushPromises();
    expect(wrapper.text()).toContain('被 2 张导图引用');
  });

  it('退回列表再点另一份：迟到的正文不能盖掉后点的那一份', async () => {
    const pending: Array<(response: Response) => void> = [];
    const list: LibraryList = {
      total: 2,
      items: [entry(), entry({ source_id: 'src-2', filename: '旧笔记.md', kind: 'markdown' })],
    };
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/health')) {
        return Promise.resolve({ ok: true, status: 200, json: async () => ({ status: 'ok', version: 'dev' }) } as unknown as Response);
      }
      if (url.includes('/knowledge/library/')) {
        return new Promise<Response>((resolve) => { pending.push(resolve); });
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => list } as unknown as Response);
    }));
    const wrapper = mountApp();
    await flushPromises();

    const preview = (text: string): LibrarySource => ({
      source_id: 'src-x', filename: '材料', kind: 'document', text, truncated: false, total_chars: text.length,
    });

    await buttonByText(wrapper, '知识库').trigger('click');
    await vi.waitFor(() => expect(wrapper.text()).toContain('旧笔记.md'));

    // 第一份还在路上，用户退回列表点了第二份
    await buttonByText(wrapper, '管理办法.pdf').trigger('click');
    await buttonByText(wrapper, '回到列表').trigger('click');
    await buttonByText(wrapper, '旧笔记.md').trigger('click');

    pending[1]({ ok: true, status: 200, json: async () => preview('第二份正文') } as unknown as Response);
    await flushPromises();
    expect(wrapper.text()).toContain('第二份正文');

    // 第一份的响应现在才到：它已经过期了，不能把正文换掉
    pending[0]({ ok: true, status: 200, json: async () => preview('第一份正文') } as unknown as Response);
    await flushPromises();
    expect(wrapper.text()).not.toContain('第一份正文');
  });
  it('知识库读不出来时给出错误文案，而不是空白面板', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/health')) {
        return { ok: true, status: 200, json: async () => ({ status: 'ok', version: 'dev' }) } as unknown as Response;
      }
      return { ok: false, status: 500, json: async () => ({ code: 'internal_error', message: '服务器出错了' }) } as unknown as Response;
    }));
    const wrapper = mountApp();
    await flushPromises();

    await buttonByText(wrapper, '知识库').trigger('click');
    await vi.waitFor(() => expect(wrapper.text()).toContain('知识库没能读取'));
  });

  it('空库时说明为什么是空的', async () => {
    stubLibrary({ total: 0, items: [] });
    const wrapper = mountApp();
    await flushPromises();

    await buttonByText(wrapper, '知识库').trigger('click');
    await vi.waitFor(() => expect(wrapper.text()).toContain('还是空的'));
    expect(wrapper.text()).toContain('带着材料生成一次导图');
  });
});
