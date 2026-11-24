import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { InstanceSettings, ModelSettings, TestModelSettingsResponse } from '../src/api/types';
import { apiKeyPlaceholder, settingSourceLabel, useSettingsStore } from '../src/stores/settings';
import { isPrefsRequest, prefsReply } from './helpers/prefs-stub';

/**
 * 模型设置面板（`/api/settings/model`）与顶栏那个齿轮。
 *
 * 这一组用例钉的是**四条会让"配置"变成谎话**的性质，每一条都对应一个具体的坏结果：
 *
 * 1. **密钥永不明文回显**。`GET` 只给 `…abcd`；要是有任何一条路径把明文送回界面，
 *    它会出现在 devtools 的 Network 面板、用户的截图、以及"把这段 JSON 发我看看"
 *    的群里。所以这里不只看占位符，还要扫整个 `wrapper.html()`。
 * 2. **留空 ≠ 清空**。框里是打码值，用户"不动它"提交的就是空串。把空串当清空，
 *    等于每次改个模型名就抹掉一次密钥——而界面只会说"已保存"。
 * 3. **只发改过的字段**。表单初始值来自"生效值"，可能是环境变量。整张表原样 PUT
 *    回去，等于把环境变量里的值搬进库里，之后部署方改 `.env` 不再生效，而用户
 *    什么都没做。
 * 4. **`demo` 模式必须说出来**。`MINDMAP_LLM_MODE=demo` 时面板里填什么都白填
 *    （agent 走占位内容、视觉提取直接拒绝）。不说的话用户会以为是密钥错了。
 *
 * 另外钉住顶栏那条"只有三件东西"的口径：入口**不是 `<button>`**。这不是审美问题，
 * `shell-actions.spec.ts` 用 `header.findAll('button')` 恰好 2 个来防止顶栏再长一排
 * 按钮；齿轮必须换个标签表达同一个语义，同时又真的能用键盘打开。
 */

vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

/** 打码前的真密钥：整份用例里它的明文一次都不许出现在响应体、DOM 或日志文案里。 */
const SECRET = 'sk-live-abcdefghijklmnop-1234';
const MASKED = '…1234';

interface Recorded {
  url: string;
  method: string;
  body: Record<string, unknown> | null;
}

function settingsPayload(overrides: Partial<ModelSettings> = {}): ModelSettings {
  return {
    base_url: 'https://api.deepseek.com/v1',
    base_url_source: 'saved',
    api_key_masked: MASKED,
    api_key_source: 'saved',
    api_key_set: true,
    model: 'deepseek-chat',
    model_source: 'saved',
    deep_model: '',
    deep_model_source: 'unset',
    deep_model_effective: 'deepseek-reasoner',
    llm_mode: 'model',
    configured: true,
    can_edit_instance: false,
    instance: null,
    updated_at: '2026-11-20T10:00:00.000Z',
    ...overrides,
  };
}

interface StubOptions {
  /** `GET /settings/model` 的响应。 */
  current?: Partial<ModelSettings>;
  /** `PUT /settings/model` 的响应覆盖（`changed` 默认 true）。 */
  saved?: Partial<ModelSettings>;
  /** `PUT` 响应里的 `notice`：改了地址时后端会带一句提醒。 */
  notice?: string | null;
  /** `POST /settings/model/test` 的响应。 */
  probe?: Partial<TestModelSettingsResponse>;
  /** 让某一个动词返回业务错误（`{status, code, message}`）。 */
  fail?: 'get' | 'put' | 'test';
}

/**
 * 按 URL 分派的 fetch 替身。
 *
 * 三类请求分别记进 `gets` / `puts` / `tests`，因为**它们本身就是被测对象**：
 * PUT 的请求体里到底带了哪几个字段、留空时有没有偷偷塞空串、测试连接用的是表单值
 * 还是已经存下的值——这些只看界面是看不出来的。
 */
function stubBackend(options: StubOptions = {}) {
  const gets: Recorded[] = [];
  const puts: Recorded[] = [];
  const tests: Recorded[] = [];

  const current = settingsPayload(options.current);
  const saved = settingsPayload({ ...options.current, ...options.saved });

  const error = (status: number, code: string, message: string) =>
    ({
      ok: false,
      status,
      json: async () => ({ code, message, detail: null, request_id: 'r1' }),
    }) as unknown as Response;

  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;

    if (isPrefsRequest(url)) return prefsReply(url);
    if (url.endsWith('/health')) {
      return { ok: true, status: 200, json: async () => ({ status: 'ok', version: 'test' }) } as unknown as Response;
    }
    if (url.endsWith('/settings/model/test')) {
      tests.push({ url, method, body });
      if (options.fail === 'test') return error(400, 'settings_incomplete', '地址、密钥、模型名三项要先填齐才能测试连接。');
      return {
        ok: true,
        status: 200,
        json: async () => ({
          ok: true,
          message: '连接正常（320 毫秒）',
          model: 'deepseek-chat',
          status: 200,
          latency_ms: 320,
          ...options.probe,
        }),
      } as unknown as Response;
    }
    if (url.endsWith('/settings/model')) {
      if (method === 'PUT') {
        puts.push({ url, method, body });
        if (options.fail === 'put') return error(400, 'settings_base_url_invalid', '服务地址要以 http:// 或 https:// 开头。');
        return {
          ok: true,
          status: 200,
          json: async () => ({ ...saved, changed: true, notice: options.notice ?? null }),
        } as unknown as Response;
      }
      gets.push({ url, method, body: null });
      if (options.fail === 'get') return error(500, 'internal_error', '读取失败。');
      return { ok: true, status: 200, json: async () => current } as unknown as Response;
    }
    return { ok: true, status: 200, json: async () => ({}) } as unknown as Response;
  });

  vi.stubGlobal('fetch', mock);
  return { mock, gets, puts, tests };
}

const wrappers: VueWrapper[] = [];

function mountApp(): VueWrapper {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(App, { global: { plugins: [pinia] } });
  wrappers.push(wrapper);
  return wrapper;
}

/** 打开面板并等它的 `GET` 落地。 */
async function openPanel(wrapper: VueWrapper): Promise<void> {
  await wrapper.get('[data-test="open-settings"]').trigger('click');
  await flushPromises();
}

const PANEL = '[data-test="settings-panel"]';

function has(wrapper: VueWrapper, selector: string): boolean {
  return wrapper.find(selector).exists();
}

function text(wrapper: VueWrapper, selector: string): string {
  return wrapper.get(selector).text();
}

beforeEach(() => {
  // Pinia 的 store 挂在 pinia 实例上，每个用例自己新建一个（`mountApp` 里做）。
  vi.spyOn(console, 'warn').mockImplementation(() => {});
});

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount());
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('顶栏入口', () => {
  it('齿轮是顶栏里一个真按钮，带 `aria-label` 与 `data-test`', async () => {
    stubBackend();
    const wrapper = mountApp();
    await flushPromises();

    const header = wrapper.get('header');
    // 顶栏现在有三个动作：`☰`、齿轮、`+`。这条口径在 `shell-actions.spec.ts` 里按
    // `data-test` 数（数的是"有哪些动作"，不是"有几个 button 标签"）。
    expect(header.findAll('button')).toHaveLength(3);
    const toggle = wrapper.get('[data-test="open-settings"]');
    expect(toggle.element.tagName).toBe('BUTTON');
    expect(toggle.attributes('aria-label')).toBe('模型设置');
    expect(toggle.attributes('aria-haspopup')).toBe('dialog');
    // 齿轮没有可见文字：顶栏的可见文字仍是"☰ / 图名 / +"（见 shell-actions 那条
    // `not.toContain('知识库')` 一类断言）。
    expect(toggle.text()).toBe('');
  });

  it('点击打开，再点关闭；`aria-expanded` 跟着变', async () => {
    stubBackend();
    const wrapper = mountApp();
    await flushPromises();

    const toggle = wrapper.get('[data-test="open-settings"]');
    expect(toggle.attributes('aria-expanded')).toBe('false');

    await toggle.trigger('click');
    await flushPromises();
    expect(has(wrapper, PANEL)).toBe(true);
    expect(toggle.attributes('aria-expanded')).toBe('true');

    await toggle.trigger('click');
    await flushPromises();
    expect(has(wrapper, PANEL)).toBe(false);
    expect(toggle.attributes('aria-expanded')).toBe('false');
  });

  it('不打开面板时不请求 `/settings/model`：不该为一次没点的功能付一次往返', async () => {
    const backend = stubBackend();
    const wrapper = mountApp();
    await flushPromises();

    expect(backend.gets).toHaveLength(0);
    expect(has(wrapper, PANEL)).toBe(false);
  });
});

describe('打开面板：读的是"当前生效值"，密钥只有打码值', () => {
  it('列出地址 / 模型名 / 深度模型名，并标出来源', async () => {
    const backend = stubBackend();
    const wrapper = mountApp();
    await openPanel(wrapper);

    expect(backend.gets).toHaveLength(1);
    expect((wrapper.get('[data-test="settings-base-url"]').element as HTMLInputElement).value)
      .toBe('https://api.deepseek.com/v1');
    expect((wrapper.get('[data-test="settings-model"]').element as HTMLInputElement).value).toBe('deepseek-chat');
    // 深度模型没配：框是空的，但"实际会发什么"要写出来（勾了深度思考时用深模型）。
    expect((wrapper.get('[data-test="settings-deep-model"]').element as HTMLInputElement).value).toBe('');
    expect(text(wrapper, '[data-test="settings-deep-effective"]')).toBe('deepseek-reasoner');
    // 来源标签：自己设的 vs 环境变量，不标出来用户分不清"为什么这里不是我填的值"。
    expect(wrapper.get(PANEL).text()).toContain('我的设置');
  });

  it('密钥框是密码类型、占位符里是打码值，**整个 DOM 里没有明文**', async () => {
    stubBackend();
    const wrapper = mountApp();
    await openPanel(wrapper);

    const input = wrapper.get('[data-test="settings-api-key"]').element as HTMLInputElement;
    expect(input.type).toBe('password');
    expect(input.value).toBe('');
    expect(input.getAttribute('placeholder')).toBe(`${MASKED}（留空＝不改）`);
    // 最硬的一条：不管明文从哪条路径漏进来，这张断言都会抓到。
    expect(wrapper.html()).not.toContain(SECRET);
    expect(wrapper.html()).not.toContain('abcdefghijklmnop');
  });

  it('环境变量的值也要能显示（配置不全时用户才知道该填什么）', async () => {
    stubBackend({
      current: {
        base_url: 'https://api.example.com/v1',
        base_url_source: 'env',
        api_key_masked: '',
        api_key_source: 'unset',
        api_key_set: false,
        model: 'gpt-4o-mini',
        model_source: 'env',
        configured: false,
      },
    });
    const wrapper = mountApp();
    await openPanel(wrapper);

    expect((wrapper.get('[data-test="settings-base-url"]').element as HTMLInputElement).value)
      .toBe('https://api.example.com/v1');
    expect((wrapper.get('[data-test="settings-api-key"]').element as HTMLInputElement)
      .getAttribute('placeholder')).toBe('sk-…');
    expect(wrapper.get(PANEL).text()).toContain('环境变量');
    // 没 key 时不该出现"清除"按钮——点了也没东西可清。
    expect(has(wrapper, '[data-test="settings-clear-key"]')).toBe(false);
    // 三项不齐：右侧要给一句结论，而不是让人对着空表猜。
    expect(wrapper.get(PANEL).text()).toContain('三项填齐后');
  });

  it('地址换过之后，密钥那一栏要自己说清「重贴一次」，不能只显示「未设置」', async () => {
    // 后端此刻的状态：库里那把密钥仍在，但它绑定的是旧主机，所以 `store.resolve()`
    // 已经把生效值清空（`api_key_set: false`、`api_key_masked: ''`）。界面上如果
    // 只说"未设置"，用户会去供应商后台重开一把——而库里那把一直是好的。
    stubBackend({
      current: {
        base_url: 'https://new.example.com/v1',
        api_key_masked: '',
        api_key_source: 'unbound',
        api_key_set: false,
        configured: false,
      },
    });
    const wrapper = mountApp();
    await openPanel(wrapper);

    const hint = wrapper.get('[data-test="settings-key-unbound"]').text();
    expect(hint).toContain('重新粘贴');
    expect(hint).toContain('旧地址');
    // 来源标签也得说这一档，而不是退回"未设置"。
    expect(wrapper.get(PANEL).text()).toContain('地址换过，待重填');
    // 没有可清的密钥，所以"清除"按钮不该出现。
    expect(has(wrapper, '[data-test="settings-clear-key"]')).toBe(false);
  });

  it('没换过地址时不该出现那条提示：绝大多数打开都是正常状态', async () => {
    stubBackend();
    const wrapper = mountApp();
    await openPanel(wrapper);

    expect(has(wrapper, '[data-test="settings-key-unbound"]')).toBe(false);
  });

  it('地址框下面写明「只支持 OpenAI 兼容格式」：不做 provider 下拉的代价要说出来', async () => {
    stubBackend();
    const wrapper = mountApp();
    await openPanel(wrapper);

    // 用户要的是"自由填"，那这一行就是唯一的护栏：填了原生协议的地址时，
    // 报错会以"不是合法的 Chat Completions 响应"的形式出现在生成阶段——
    // 不指着地址这一行的话，没有人会想到是地址的问题。
    const hint = wrapper.get('[data-test="settings-base-url-hint"]');
    expect(hint.text()).toContain('OpenAI 兼容');
    expect(hint.text()).toContain('/chat/completions');
    // 提示属于地址框那个 label，位置对了才读得懂。
    expect(hint.element.closest('label')?.querySelector('[data-test="settings-base-url"]'))
      .not.toBeNull();
  });

  it('读取失败要说出来，而不是显示一张空表', async () => {
    stubBackend({ fail: 'get' });
    const wrapper = mountApp();
    await openPanel(wrapper);

    expect(text(wrapper, '[data-test="settings-error"]')).toContain('读取失败');
  });
});

describe('保存：只发用户真改过的字段', () => {
  it('什么都没改时保存按钮是禁用的，点了也不发请求', async () => {
    const backend = stubBackend();
    const wrapper = mountApp();
    await openPanel(wrapper);

    const save = wrapper.get('[data-test="settings-save"]');
    expect(save.attributes('disabled')).toBeDefined();
    await save.trigger('click');
    await flushPromises();
    expect(backend.puts).toHaveLength(0);
  });

  it('只改模型名：PUT 里**只有** `model`，地址与密钥一个字都不带', async () => {
    const backend = stubBackend();
    const wrapper = mountApp();
    await openPanel(wrapper);

    await wrapper.get('[data-test="settings-model"]').setValue('deepseek-reasoner');
    await wrapper.get('[data-test="settings-save"]').trigger('click');
    await flushPromises();

    expect(backend.puts).toHaveLength(1);
    // 地址的初始值来自环境变量时，把它原样 PUT 回去就等于"把环境变量搬进库里"——
    // 之后部署方改 `.env` 不再生效，而用户什么都没做。所以这里只能有改过的字段。
    expect(backend.puts[0].body).toEqual({ model: 'deepseek-reasoner', clear_api_key: false });
  });

  it('填了新密钥才发 `api_key`；留空时绝不发（留空＝不改）', async () => {
    const backend = stubBackend();
    const wrapper = mountApp();
    await openPanel(wrapper);

    // 只动地址 → 请求里没有 api_key
    await wrapper.get('[data-test="settings-base-url"]').setValue('https://api.moonshot.cn/v1');
    await wrapper.get('[data-test="settings-save"]').trigger('click');
    await flushPromises();
    expect(backend.puts[0].body).toEqual({ base_url: 'https://api.moonshot.cn/v1', clear_api_key: false });
    expect(backend.puts[0].body).not.toHaveProperty('api_key');

    // 再填一把新的 → 这次带上了，而且是**用户新打的那一段**
    await wrapper.get('[data-test="settings-api-key"]').setValue(SECRET);
    await wrapper.get('[data-test="settings-save"]').trigger('click');
    await flushPromises();
    expect(backend.puts[1].body).toEqual({ api_key: SECRET, clear_api_key: false });
  });

  it('保存成功后表单重新对齐服务端返回值，脏标记清空、按钮变回禁用', async () => {
    const backend = stubBackend({ saved: { model: 'deepseek-reasoner' } });
    const wrapper = mountApp();
    await openPanel(wrapper);

    await wrapper.get('[data-test="settings-model"]').setValue('deepseek-reasoner');
    await wrapper.get('[data-test="settings-save"]').trigger('click');
    await flushPromises();

    expect(backend.puts).toHaveLength(1);
    expect((wrapper.get('[data-test="settings-model"]').element as HTMLInputElement).value)
      .toBe('deepseek-reasoner');
    expect(wrapper.get('[data-test="settings-save"]').attributes('disabled')).toBeDefined();
  });

  it('保存失败要把后端那句话显示出来', async () => {
    stubBackend({ fail: 'put' });
    const wrapper = mountApp();
    await openPanel(wrapper);

    await wrapper.get('[data-test="settings-base-url"]').setValue('ftp://nope');
    await wrapper.get('[data-test="settings-save"]').trigger('click');
    await flushPromises();

    expect(text(wrapper, '[data-test="settings-error"]')).toContain('http://');
  });

  it('改了地址：保存成功但要提醒"上次测试的结论不成立了"', async () => {
    // P0-1 的第二层。第一层（拒内网）挡不住"改指向一个公网攻击者地址"——那一跳
    // 每一处看起来都合法。提醒不拦人，但它让"我刚改过地址"出现在保存的那一刻。
    stubBackend({ notice: '地址改了。之前那次「测试连接」的结论对新地址不成立，建议现在点一次「测试连接」。' });
    const wrapper = mountApp();
    await openPanel(wrapper);

    await wrapper.get('[data-test="settings-base-url"]').setValue('https://api.moonshot.cn/v1');
    await wrapper.get('[data-test="settings-save"]').trigger('click');
    await flushPromises();

    expect(text(wrapper, '[data-test="settings-notice"]')).toContain('测试连接');
    // 提醒是中性色，不能长成红色的失败——保存确实成功了。
    expect(has(wrapper, '[data-test="settings-error"]')).toBe(false);
  });

  it('没改地址时不弹那句提醒：提醒一多就成了噪声', async () => {
    stubBackend({ notice: null });
    const wrapper = mountApp();
    await openPanel(wrapper);

    await wrapper.get('[data-test="settings-model"]').setValue('deepseek-reasoner');
    await wrapper.get('[data-test="settings-save"]').trigger('click');
    await flushPromises();

    expect(has(wrapper, '[data-test="settings-notice"]')).toBe(false);
  });

  it('点一次「测试连接」之后那句提醒就该消失', async () => {
    stubBackend({ notice: '地址改了。…建议现在点一次「测试连接」。' });
    const wrapper = mountApp();
    await openPanel(wrapper);

    await wrapper.get('[data-test="settings-base-url"]').setValue('https://api.moonshot.cn/v1');
    await wrapper.get('[data-test="settings-save"]').trigger('click');
    await flushPromises();
    expect(has(wrapper, '[data-test="settings-notice"]')).toBe(true);

    await wrapper.get('[data-test="settings-test"]').trigger('click');
    await flushPromises();
    // 测过就不再是"还没验证过的新地址"。留着会让人以为测试没生效。
    expect(has(wrapper, '[data-test="settings-notice"]')).toBe(false);
  });
});

describe('清除密钥是单独的动作', () => {
  it('「清除密钥」发的是 `clear_api_key: true`，不是空串', async () => {
    const backend = stubBackend({ saved: { api_key_masked: '', api_key_source: 'unset', api_key_set: false } });
    const wrapper = mountApp();
    await openPanel(wrapper);

    await wrapper.get('[data-test="settings-clear-key"]').trigger('click');
    await flushPromises();

    expect(backend.puts).toHaveLength(1);
    // 空串在 `PUT` 里是"不改"。清空必须走单独那个字段——否则这个按钮点一下
    // 什么都不会发生，而界面会说"已保存"。
    expect(backend.puts[0].body).toEqual({ clear_api_key: true });
    expect(backend.puts[0].body?.api_key).toBeUndefined();
  });
});

describe('本站共享配置：只有部署方看得到', () => {
  const instance: InstanceSettings = {
    base_url: 'https://shared.example/v1',
    api_key_masked: '…9999',
    api_key_set: true,
    model: 'shared-model',
    deep_model: '',
    configured: true,
    updated_at: '2026-11-20T09:00:00.000Z',
  };

  it('部署方：显示共享那一行的内容与状态', async () => {
    stubBackend({ current: { can_edit_instance: true, instance } });
    const wrapper = mountApp();
    await openPanel(wrapper);

    expect(has(wrapper, '[data-test="settings-instance"]')).toBe(true);
    expect(text(wrapper, '[data-test="settings-instance-base-url"]')).toBe('https://shared.example/v1');
    expect(text(wrapper, '[data-test="settings-instance-key"]')).toBe('…9999');
    expect(text(wrapper, '[data-test="settings-instance-state"]')).toBe('已配置');
  });

  it('非部署方：整块都不出现（不是显示成灰的）', async () => {
    stubBackend({ current: { can_edit_instance: false, instance: null } });
    const wrapper = mountApp();
    await openPanel(wrapper);

    // 藏起来而不是置灰：一个点了就 403 的表单，用户会以为是网络问题。
    expect(has(wrapper, '[data-test="settings-instance"]')).toBe(false);
  });

  it('共享行没配好时状态说"还没配好"，别让人以为游客能用', async () => {
    stubBackend({
      current: { can_edit_instance: true, instance: { ...instance, configured: false, api_key_set: false, api_key_masked: '' } },
    });
    const wrapper = mountApp();
    await openPanel(wrapper);

    expect(text(wrapper, '[data-test="settings-instance-state"]')).toBe('还没配好');
    expect(text(wrapper, '[data-test="settings-instance-key"]')).toBe('（空）');
  });

  it('「存为共享配置」带 `scope: "instance"`，且把四项一起发出去', async () => {
    const backend = stubBackend({ current: { can_edit_instance: true, instance } });
    const wrapper = mountApp();
    await openPanel(wrapper);

    await wrapper.get('[data-test="settings-save-instance"]').trigger('click');
    await flushPromises();

    expect(backend.puts).toHaveLength(1);
    // 语义是"共享配置 = 这一份"，所以整个覆盖：空字段也要发（空串 = 清掉）。
    expect(backend.puts[0].body).toEqual({
      clear_api_key: false,
      scope: 'instance',
      base_url: 'https://api.deepseek.com/v1',
      model: 'deepseek-chat',
      deep_model: '',
    });
    // 没打字就不带 `api_key`：空串在后端是"不改"，共享那把密钥要原样留着。
    expect(backend.puts[0].body?.api_key).toBeUndefined();
  });

  it('在共享行里打了新密钥就把新密钥带上', async () => {
    const backend = stubBackend({ current: { can_edit_instance: true, instance } });
    const wrapper = mountApp();
    await openPanel(wrapper);

    await wrapper.get('[data-test="settings-api-key"]').setValue(SECRET);
    await wrapper.get('[data-test="settings-save-instance"]').trigger('click');
    await flushPromises();

    expect(backend.puts[0].body?.api_key).toBe(SECRET);
    expect(backend.puts[0].body?.scope).toBe('instance');
  });
});

describe('测试连接', () => {
  it('用表单里的值（含还没保存的改动）发一次，结果显示在面板里', async () => {
    const backend = stubBackend();
    const wrapper = mountApp();
    await openPanel(wrapper);

    await wrapper.get('[data-test="settings-base-url"]').setValue('https://api.moonshot.cn/v1');
    await wrapper.get('[data-test="settings-api-key"]').setValue(SECRET);
    await wrapper.get('[data-test="settings-test"]').trigger('click');
    await flushPromises();

    expect(backend.tests).toHaveLength(1);
    expect(backend.tests[0].method).toBe('POST');
    expect(backend.tests[0].body).toEqual({
      base_url: 'https://api.moonshot.cn/v1',
      api_key: SECRET,
      // 模型名没被改动，于是发的是表单里那份生效值。这不算"偷偷保存"：测试连接
      // 只读不写，用它等价于让后端自己回落——**PUT 里绝不能出现同样的字段**才是
      // 那条真规矩（见上面「只改模型名」那条）。
      model: 'deepseek-chat',
    });
    expect(text(wrapper, '[data-test="settings-result"]')).toContain('连接正常');
    // 测完不该顺手保存：这是两件事，混起来等于"点一下测连接就改了线上配置"。
    expect(backend.puts).toHaveLength(0);
  });

  it('没动表单时发的是当前生效值（等价于让后端自己回落）', async () => {
    const backend = stubBackend();
    const wrapper = mountApp();
    await openPanel(wrapper);

    await wrapper.get('[data-test="settings-test"]').trigger('click');
    await flushPromises();

    expect(backend.tests).toHaveLength(1);
    expect(backend.tests[0].body).toEqual({
      base_url: 'https://api.deepseek.com/v1',
      model: 'deepseek-chat',
    });
    // 打码值不许被当成密钥发回去：那会把 `…1234` 存成真密钥。
    expect(backend.tests[0].body).not.toHaveProperty('api_key');
  });

  it('`ok=false` 是结论不是异常：红字显示、面板不崩、也不报错', async () => {
    stubBackend({
      probe: { ok: false, message: '密钥被拒绝（HTTP 401）', status: 401, latency_ms: 90 },
    });
    const wrapper = mountApp();
    await openPanel(wrapper);

    await wrapper.get('[data-test="settings-test"]').trigger('click');
    await flushPromises();

    const result = wrapper.get('[data-test="settings-result"]');
    expect(result.text()).toContain('密钥被拒绝');
    expect(result.text()).toContain('HTTP 401');
    expect(has(wrapper, '[data-test="settings-error"]')).toBe(false);
    expect(wrapper.get('[data-test="settings-result"]').classes().join(' ')).toContain('rose');
  });

  it('三项都没配时后端给 400：显示那句话，而且**不含密钥**', async () => {
    stubBackend({ fail: 'test' });
    const wrapper = mountApp();
    await openPanel(wrapper);

    await wrapper.get('[data-test="settings-test"]').trigger('click');
    await flushPromises();

    expect(text(wrapper, '[data-test="settings-error"]')).toContain('三项');
    expect(wrapper.html()).not.toContain(SECRET);
  });
});

describe('demo 模式必须说出来', () => {
  it('`llm_mode=demo` 时面板顶部有醒目提示：配好了也不会调模型', async () => {
    stubBackend({ current: { llm_mode: 'demo' } });
    const wrapper = mountApp();
    await openPanel(wrapper);

    const hint = wrapper.get('[data-test="settings-demo-hint"]');
    // 提示里得同时说清三件事：为什么、影响什么、怎么改。
    expect(hint.text()).toContain('MINDMAP_LLM_MODE=demo');
    expect(hint.text()).toContain('占位');
    expect(hint.text()).toContain('重启');
  });

  it('`llm_mode=model` 时不出现那条提示', async () => {
    stubBackend();
    const wrapper = mountApp();
    await openPanel(wrapper);

    expect(has(wrapper, '[data-test="settings-demo-hint"]')).toBe(false);
  });
});

describe('关闭', () => {
  it('Esc 关掉；点遮罩也关；点卡片内部不关', async () => {
    stubBackend();
    const wrapper = mountApp();
    await openPanel(wrapper);

    // 点输入框（卡片内部）不许关——少了 `.stop` 就会"点一下就没了"。
    await wrapper.get('[data-test="settings-model"]').trigger('click');
    await flushPromises();
    expect(has(wrapper, PANEL)).toBe(true);

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await flushPromises();
    expect(has(wrapper, PANEL)).toBe(false);

    await openPanel(wrapper);
    await wrapper.get('[data-test="settings-backdrop"]').trigger('click');
    await flushPromises();
    expect(has(wrapper, PANEL)).toBe(false);
  });

  it('✕ 也能关', async () => {
    stubBackend();
    const wrapper = mountApp();
    await openPanel(wrapper);

    await wrapper.get('[data-test="settings-close"]').trigger('click');
    await flushPromises();
    expect(has(wrapper, PANEL)).toBe(false);
  });

  it('Tab 圈在面板里：走到最后一个再按 Tab 回到第一个', async () => {
    // `aria-modal` 对读屏有效，对键盘无效：不接住的话 Tab 会跑到遮罩背后的画布上，
    // 用户看着面板还在、按键却已经操作别处了（与知识库抽屉同一套做法）。
    //
    // 这条用例要真的 `focus()`，所以必须挂到 document 上——`@vue/test-utils` 默认
    // 的游离节点里 `document.activeElement` 永远是 `body`。
    stubBackend();
    const pinia = createPinia();
    setActivePinia(pinia);
    const wrapper = mount(App, { attachTo: document.body, global: { plugins: [pinia] } });
    wrappers.push(wrapper);
    await openPanel(wrapper);

    const panel = wrapper.get(PANEL);
    const focusable = panel.findAll<HTMLElement>(
      'button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    );
    expect(focusable.length).toBeGreaterThan(2);

    const last = focusable[focusable.length - 1];
    const first = focusable[0];
    last.element.focus();
    expect(document.activeElement).toBe(last.element);

    await panel.trigger('keydown', { key: 'Tab' });
    expect(document.activeElement).toBe(first.element);

    // 反向：从第一个按 Shift+Tab 回到最后一个。
    await panel.trigger('keydown', { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(last.element);
  });

  it('再打开会重拉一次：别的标签页刚改过的值不该被上一次的快照盖住', async () => {
    const backend = stubBackend();
    const wrapper = mountApp();
    await openPanel(wrapper);
    await wrapper.get('[data-test="settings-close"]').trigger('click');
    await flushPromises();
    await openPanel(wrapper);

    expect(backend.gets).toHaveLength(2);
  });
});

describe('store 与纯函数', () => {
  it('来源标签七档：saved / instance / public / free / env / unset / unbound', () => {
    expect(settingSourceLabel('saved')).toBe('我的设置');
    // 借用共享配置是独立一档：清掉自己那行会回到它，用户得知道这件事。
    expect(settingSourceLabel('instance')).toBe('本站共享配置');
    // 公共免费密钥也只能自己一档：它是唯一一档"有次数上限"的来源，混进
    // `instance` 会让用户以为可以随便用。
    expect(settingSourceLabel('public')).toBe('本站免费额度');
    // 免费档也单独一档：模型名会变成一个不认识的 id，得能分辨这是部署方开的功能
    // 还是自己配错了。
    expect(settingSourceLabel('free')).toBe('免费模型');
    expect(settingSourceLabel('env')).toBe('环境变量');
    expect(settingSourceLabel('unset')).toBe('未设置');
    // `unbound` 必须自己一档：它与 `unset` 的下一句完全不同（重贴一次 vs 去配一把）。
    expect(settingSourceLabel('unbound')).toBe('地址换过，待重填');
    // 后端加了新的来源值时不能炸，退回"未设置"比显示 `undefined` 诚实。
    expect(settingSourceLabel('future')).toBe('未设置');
  });

  it('密钥占位符：有 key 给打码值 + 留空说明，没 key 给形状示例', () => {
    expect(apiKeyPlaceholder(settingsPayload())).toBe(`${MASKED}（留空＝不改）`);
    expect(apiKeyPlaceholder(settingsPayload({ api_key_set: false, api_key_masked: '' }))).toBe('sk-…');
    expect(apiKeyPlaceholder(null)).toBe('sk-…');
  });

  it('地址换过之后，占位符不能还是打码值——那会让人以为配好了', () => {
    // 库里那把密钥仍然存在（`mask_api_key` 拿得到），但它属于旧地址。
    // 只要 placeholder 还在显示 `…1234`，用户就不会去重贴，而生成一路 401。
    expect(apiKeyPlaceholder(settingsPayload({
      api_key_source: 'unbound',
      api_key_set: false,
    }))).toBe('地址换过了，请重新粘贴密钥');
  });

  it('store 的 `isDemoMode` 只认 `demo`，空串不算', async () => {
    stubBackend({ current: { llm_mode: '' } });
    const wrapper = mountApp();
    await openPanel(wrapper);

    const store = useSettingsStore();
    expect(store.isDemoMode).toBe(false);
    expect(store.configured).toBe(true);
    expect(has(wrapper, '[data-test="settings-base-url"]')).toBe(true);
  });
});
