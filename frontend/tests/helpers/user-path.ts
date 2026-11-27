import { flushPromises, mount } from '@vue/test-utils';
import type { DOMWrapper, VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { vi } from 'vitest';

import App from '../../src/App.vue';
import { isPrefsRequest, prefsReply } from './prefs-stub';

/**
 * 「用户路径」清单基建：**每个用户以为存在的动作，界面上有没有一个能点的地方**。
 *
 * ## 它和其余 87 份 spec 的判据不一样
 *
 * 那些用例回答"这个函数算得对不对"——它们喂函数、看返回值。这一份回答的是另一个
 * 问题：**用户能不能走到那个函数前面**。两者可以同时为真也可以同时为假：
 * `DELETE /api/mindmaps/{id}` 曾经是 405（后端没有这条路由），而当时 3311 条用例
 * 一条都没红——因为没有任何一条从"界面上有个删除按钮"这个前提出发。
 *
 * 所以这里的每条用例只断言两件事，多一件都不加：
 *
 * 1. **有一个可点入口**（在 DOM 里，而且此刻真的可见）；
 * 2. **点了之后发出了什么请求 / 改了什么状态**。
 *
 * 具体文案、二次确认、错误分支都由既有 spec 负责，这里不重复。
 *
 * ## 为什么必须判"可见"
 *
 * 这个仓库的浮层几乎都用 `v-show` 常驻挂载（`HistoryDrawer`、`ChatDock` 的材料面板、
 * 偏好浮层），理由是"收起只是看不见，DOM 与键盘顺序都还在"。代价是
 * `wrapper.find('[data-test="x"]')` **在收起状态下照样能找到**——只看 DOM 的判据
 * 会把一个用户看不见的按钮算成入口，于是整份清单永远绿。
 *
 * `visible()` 沿 `parentElement` 上溯看每一层的内联 `display`：`v-show=false` 写的
 * 就是 `display: none`，jsdom 不会替我们算继承，这一段必须自己做。
 */

export type EntryLocator =
  | { selector: string }
  | { text: string; scope?: string }
  | { labelPrefix: string; scope?: string };

export interface ActionPoint {
  /** 用户视角的动作名（照着用户手册的写法）。 */
  name: string;
  /** 这个动作在哪一屏，报错时要指出来。 */
  where: string;
  locator: EntryLocator;
  /**
   * 这个动作不是"一个按钮"时，写明它由哪份 spec 覆盖（相对本文件的路径，
   * 例如 `'../node-drag.spec.ts'`）。
   *
   * 拖动与双击空白都是**手势**：界面上没有一个可点的入口，`entryOf` 找不到东西
   * 是正常的。但"找不到东西"不能变成清单永久红——那样它会被删掉。所以要求登记一处
   * 覆盖，`catalogGaps` 会核对那份文件真的存在。
   */
  coveredBy?: string;
}

function describeLocator(locator: EntryLocator): string {
  if ('selector' in locator) return `选择器 ${locator.selector}`;
  const where = locator.scope ? `在 ${locator.scope} 里` : '';
  if ('labelPrefix' in locator) return `${where} aria-label 以「${locator.labelPrefix}」开头`;
  return `${where} 按钮文案「${locator.text}」`;
}

/** 元素此刻会不会出现在屏幕上（自己或任一祖先被 `v-show` 关掉就不算）。 */
export function visible(el: Element | null | undefined): boolean {
  if (!el) return false;
  let node: Element | null = el;
  while (node) {
    const style = (node as HTMLElement).style;
    if (style && style.display === 'none') return false;
    node = node.parentElement;
  }
  return true;
}

const CLICKABLE = 'button, [role="button"], a, label, summary, input, select, textarea';

/**
 * `scope` 是"只在某个容器里找"的前缀选择器。
 *
 * 有它是因为同一个屏幕上真的会出现两个同名的按钮：左抽屉开着的时候，导图列表行有
 * 「删除」，知识库抽屉叠上来之后材料行也有「删除」。两个都没带 `data-test`，只能靠
 * 容器区分——**这件事本身值得记一笔**，它和"移除/删除并排"是同一种语义混淆。
 */
function matches(wrapper: VueWrapper, locator: EntryLocator): DOMWrapper<Element>[] {
  if ('selector' in locator) return wrapper.findAll(locator.selector);
  const scope = locator.scope ? `${locator.scope} ` : '';
  if ('labelPrefix' in locator) {
    const prefix = locator.labelPrefix;
    return wrapper
      .findAll(`${scope}[aria-label]`)
      .filter(el => (el.attributes('aria-label') ?? '').startsWith(prefix));
  }
  const text = locator.text;
  return wrapper.findAll(`${scope}${CLICKABLE}`).filter(el => el.text().trim() === text);
}

function missing(point: ActionPoint): Error {
  return new Error(
    `用户路径缺口：${point.where}没有「${point.name}」的可点入口`
    + `（找不到 ${describeLocator(point.locator)}，或它此刻被 v-show 收起来了）。`
    + '如果这个动作还在，就是入口被改没了；如果它已经下线，请从清单里删掉这一条。',
  );
}

/**
 * 取入口元素；**不可见也算缺口**，理由见文件头。
 *
 * `index` 用于同一个文案在同一屏里出现多次的情形（例如知识库抽屉里，材料行的
 * 「删除」与别名行的「删除」是同一句话）。这不是懒——那两个按钮**本来就没有**
 * `data-test` 或 `aria-label` 可区分，只能按文档顺序取。
 */
export function entryOf(wrapper: VueWrapper, point: ActionPoint, index = 0): DOMWrapper<Element> {
  // 只查不改的用例也算"碰过这条动作"：它们核对的正是入口存在性。
  touched.add(point.name);
  const shown = matches(wrapper, point.locator).filter(el => visible(el.element));
  if (shown.length <= index) throw missing(point);
  return shown[index];
}

/** 已经点过的动作名（用于"清单没有漂移"那条检查）。 */
const touched = new Set<string>();

export function resetTouched(): void {
  touched.clear();
}

/** 清单里没有用例碰过的动作。 */
export function untouched(points: readonly ActionPoint[]): string[] {
  return points.filter(item => !touched.has(item.name)).map(item => item.name);
}

const SPEC_FILES = new Set(Object.keys(import.meta.glob('../*.spec.ts')));

/**
 * 清单里**既没被本轮点到、也没有登记手势覆盖**的动作。
 *
 * 这是这张表防腐烂的唯一机制：动作删了而清单没删，或者清单加了一条没人验证的动作，
 * 都在这里红。手势类动作靠 `coveredBy` 过关，所以"拖动由 node-drag.spec.ts 覆盖"
 * 这句话是可核对的，不是一句注释。
 */
export function catalogGaps(points: readonly ActionPoint[]): string[] {
  const gaps: string[] = [];
  for (const item of points) {
    if (touched.has(item.name)) continue;
    if (item.coveredBy && SPEC_FILES.has(item.coveredBy)) continue;
    gaps.push(item.coveredBy ? `${item.name}（登记的覆盖文件不存在：${item.coveredBy}）` : item.name);
  }
  return gaps;
}

/** 按名字取清单条目：拼错名字会当场红，而不是静默跑一个不存在的动作。 */
export function point(points: readonly ActionPoint[], name: string): ActionPoint {
  const found = points.find(item => item.name === name);
  if (!found) throw new Error(`清单里没有这个动作：${name}`);
  return found;
}

/**
 * 找到入口、点一下。
 *
 * 查找到此为止——**点了之后要断言什么，由调用方写**，因为"该发什么请求"每个动作都
 * 不一样，塞进这张表只会让表变成第二份产品代码。
 */
export async function tap(wrapper: VueWrapper, entry: ActionPoint, index = 0): Promise<void> {
  touched.add(entry.name);
  const el = entryOf(wrapper, entry, index);
  await el.trigger('click');
  await flushPromises();
}

// ---------------------------------------------------------------- 网络桩

export interface Call {
  url: string;
  method: string;
  body: unknown;
}

export const MAP_ID = 'm1';

export const SAVED_MAP = {
  id: MAP_ID,
  version: 2,
  updated_at: '2026-11-26T10:00:00Z',
  content: {
    title: '甲图',
    root: { id: 'r', label: '甲图', children: [{ id: 'c1', label: '子', children: [] }] },
  },
};

const LIBRARY = {
  items: [{
    source_id: 'lib-1',
    filename: '讲义.pdf',
    kind: 'document',
    char_count: 120,
    cited_by: 0,
    created_at: '2026-11-20T00:00:00Z',
    last_used_at: '2026-11-21T00:00:00Z',
  }],
  total: 1,
};

function json(body: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => body } as unknown as Response;
}

function text(body: string): Response {
  return {
    ok: true, status: 200, text: async () => body, json: async () => ({}),
  } as unknown as Response;
}

/** 默认应答：每个端点都给一个**形状正确**的空壳，让界面能渲染到"等用户动手"那一屏。 */
function defaultReply(url: string): Response {
  if (isPrefsRequest(url)) return prefsReply(url);
  if (url.endsWith('/health')) return json({ status: 'ok', version: 'dev' });
  if (url.includes('/auth/me')) return json({ kind: 'guest', guest_id: 'g1' });
  if (url.includes('/knowledge/library')) return json(LIBRARY);
  if (url.includes('/knowledge/aliases')) return json({ items: [], total: 0, configured: false });
  if (url.includes('/export/')) return text('@startmindmap\n@endmindmap\n');
  if (url.includes('/agent/runs')) return json([]);
  if (url.includes('/mindmaps/')) return json(SAVED_MAP);
  if (url.includes('/mindmaps')) return json([SAVED_MAP]);
  return json({});
}

export interface ApiStub {
  calls: Call[];
  mock: ReturnType<typeof vi.fn>;
}

/**
 * 装一个 `fetch` 替身。
 *
 * `override` 拿到 `(call)`，返回 `undefined` 就落回默认应答——这样每条用例只需要写
 * 它真正关心的那一个端点。
 */
export function stubApi(override?: (call: Call) => Response | undefined): ApiStub {
  const calls: Call[] = [];
  const mock = vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = String(input);
    const call: Call = { url, method: init?.method ?? 'GET', body: init?.body ?? null };
    if (!isPrefsRequest(url) && !url.endsWith('/health')) calls.push(call);
    return override?.(call) ?? defaultReply(url);
  });
  vi.stubGlobal('fetch', mock);
  return { calls, mock };
}

// ---------------------------------------------------------------- 挂载

export interface Page {
  wrapper: VueWrapper;
  api: ApiStub;
}

/**
 * 挂载整页。
 *
 * 画布必须由调用方在自己的 spec 顶部 `vi.mock` 成占位：jsdom 没有 ResizeObserver，
 * 真 Vue Flow 在这里算不出尺寸、节点画不出来（`smoke-canvas.spec.ts` 只证明了容器
 * 能建）。所以**画布里的动作不在这条路上测**——那些走
 * `user-path-workspace.spec.ts` 里单独挂 `MindNode` 的路径。
 */
export async function mountPage(options: { api?: ApiStub } = {}): Promise<Page> {
  const api = options.api ?? stubApi();
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(App, { global: { plugins: [pinia] } });
  mounted.push(wrapper);
  await flushPromises();
  return { wrapper, api };
}

const mounted: VueWrapper[] = [];

/**
 * 卸掉本文件挂过的所有页面，并撤掉 `fetch` 替身。
 *
 * 不卸会漏 `document` 级监听（抽屉的 `pointerdown` 是挂在 document 上的），上一条
 * 用例的页面会继续响应下一条用例的点击。
 */
export function cleanupPages(): void {
  while (mounted.length) mounted.pop()?.unmount();
  vi.unstubAllGlobals();
}

/**
 * 一个**永远不结束**的 SSE 响应：用来把界面停在"正在生成"那一屏。
 *
 * 「取消」这个动作只在 running 状态存在，而 running 是网络状态——没有它，那条
 * 用例只能靠直接改 store，那就绕过了"入口是否存在"这个判据本身。
 */
export function pendingStream(): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('data: {"type":"progress","seq":1}\n\n'));
      // 故意不 close：客户端的 reader.read() 挂在这里，界面保持 running。
    },
  });
  return { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
}
