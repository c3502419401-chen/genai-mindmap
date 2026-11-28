import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Source } from '../src/api/types';
import { UPLOAD_TIMEOUT_MS } from '../src/api/client';
import { MAX_AUDIO_BYTES, MAX_INPUT_CHARS, MAX_SOURCES, MAX_UPLOAD_BYTES, TOPIC_MAX_CHARS, fileTitle, materialTitle, mergedCharCount, sourceInputError, textSource } from '../src/sources/input';
import { useMindMapStore } from '../src/stores/mindmap';
import { useSourcesStore } from '../src/stores/sources';

const json = (body: unknown, status = 200) => ({
  ok: status < 400, status, json: async () => body,
}) as Response;

function parsed(id = 's1', overrides: Partial<Source> = {}): Source {
  return { ...textSource('已解析正文'), id, kind: 'document', filename: '资料.pdf', ...overrides };
}

function file(name = '资料.pdf', size?: number): File {
  const result = new File(['content'], name);
  if (size !== undefined) Object.defineProperty(result, 'size', { value: size });
  return result;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

/** 等一拍：fetch → json() → 状态落盘要跨好几个微任务。 */
const flush = () => new Promise(resolve => setTimeout(resolve, 0));

/** 上传时用的文件名。并发下 fetch 的调用先后不稳定，不能靠 mockResolvedValueOnce 排队。 */
function uploadName(init?: RequestInit): string {
  const form = init?.body as FormData;
  return (form.get('file') as File).name;
}

/** 按文件名路由响应，与调用顺序无关。 */
function routedFetch(routes: Record<string, () => Response | Promise<Response>>) {
  return vi.fn(async (_url: string, init?: RequestInit) => {
    const name = uploadName(init);
    const route = routes[name];
    if (!route) throw new Error(`未预期的上传：${name}`);
    return route();
  });
}

let pinia: Pinia;
beforeEach(() => {
  pinia = createPinia();
  setActivePinia(pinia);
});
afterEach(() => {
  disposePinia(pinia);
  vi.unstubAllGlobals();
});

describe('来源收集', () => {
  it('HTTP 环境没有 randomUUID 时也能加入文本和文件，来源 ID 不重复', async () => {
    vi.stubGlobal('crypto', {});
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ source: parsed('uploaded') })));
    const store = useSourcesStore();
    expect(store.addText('文本一')).toBe(true);
    expect(store.addText('文本二')).toBe(true);
    await store.addFiles([file()]);
    expect(store.items).toHaveLength(3);
    expect(new Set(store.items.map(item => item.id)).size).toBe(3);
    expect(new Set(store.readySources.map(source => source.id)).size).toBe(3);
    expect(store.problem).toBe('');
  });

  it('文本、文档、图片、音频按添加顺序收集，解析不生成且不修改旧图', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({ source: parsed('doc') }))
      .mockResolvedValueOnce(json({ source: parsed('image', { kind: 'image', filename: '草图.png' }) }))
      .mockResolvedValueOnce(json({ source: parsed('audio', { kind: 'audio', filename: '录音.wav', duration_seconds: 12 }) }));
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();
    const map = useMindMapStore();
    const old = { title: '旧图', root: { key: 'old', id: 'old', label: '旧图', children: [] } };
    map.loadMap(old, { id: 'map-old', version: 3, updated_at: '2026-11-05' });
    store.addText('  补充说明  ');
    await store.addFiles([file(), file('草图.png'), file('录音.wav')]);

    expect(store.readySources.map(source => source.kind)).toEqual(['text', 'document', 'image', 'audio']);
    expect(new Set(store.readySources.map(source => source.id)).size).toBe(4);
    expect(store.readySources[0].text).toBe('补充说明');
    expect(store.readySources[3].duration_seconds).toBe(12);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(Array(3).fill('/api/ingestion/files'));
    expect(map.content).toEqual(old);
    expect(map.mapId).toBe('map-old');
    expect(map.dirty).toBe(false);
  });

  it('单个文件失败不终止后续文件；仅重试失败项且顺序不变', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({ source: parsed('a') }))
      .mockResolvedValueOnce(json({ detail: '图片识别暂不可用' }, 422))
      .mockResolvedValueOnce(json({ source: parsed('c') }))
      .mockResolvedValueOnce(json({ source: parsed('b', { kind: 'image' }) }));
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();
    await store.addFiles([file('a.pdf'), file('b.png'), file('c.wav')]);

    expect(store.items.map(item => item.state)).toEqual(['ready', 'failed', 'ready']);
    expect(store.items[1].error).toBe('图片识别暂不可用');
    expect(store.problem).toContain('重试或移除');
    await store.retry(store.items[1].key);
    expect(store.readySources.map(source => source.id)).toEqual(['a', 'b', 'c']);
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(store.problem).toBe('');
  });

  it.each(['remove', 'clear'] as const)('%s 中止在途解析，迟到响应不能恢复已移除来源', async action => {
    const pending = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValue(pending.promise);
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();
    const task = store.addFiles([file(), file('排队.pdf')]);
    const signal = fetchMock.mock.calls[0][1].signal as AbortSignal;
    expect(store.parsing).toBe(true);
    expect(store.items.map(item => item.state)).toEqual(['parsing', 'queued']);
    if (action === 'clear') store.clear();
    else store.items.map(item => item.key).forEach(key => store.remove(key));
    expect(signal.aborted).toBe(true);
    pending.resolve(json({ source: parsed() }));
    await task;

    expect(store.items).toEqual([]);
    expect(store.parsing).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // 清空之后仍可正常追加新来源。
    fetchMock.mockResolvedValue(json({ source: parsed('new') }));
    await store.addFiles([file('新的.pdf')]);
    expect(store.readySources[0].id).toBe('new');
  });

  it('追加文件会进入同一个串行队列', async () => {
    const pending = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValueOnce(pending.promise)
      .mockResolvedValue(json({ source: parsed('second') }));
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();
    const first = store.addFiles([file('第一份.pdf')]);
    const second = store.addFiles([file('第二份.pdf')]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    pending.resolve(json({ source: parsed('first') }));
    await Promise.all([first, second]);
    expect(store.readySources.map(source => source.id)).toEqual(['first', 'second']);
  });

  it('两份文档 + 两张图片同时添加：文档串行，图片并发', async () => {
    const running = deferred<Response>();
    const fetchMock = routedFetch({
      '第一份.pdf': () => running.promise,
      '第二份.pdf': () => Promise.resolve(json({ source: parsed('doc-2') })),
      '图一.png': () => Promise.resolve(json({ source: parsed('img-1', { kind: 'image' }) })),
      '图二.png': () => Promise.resolve(json({ source: parsed('img-2', { kind: 'image' }) })),
    });
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();

    const task = store.addFiles([file('第一份.pdf'), file('第二份.pdf'), file('图一.png'), file('图二.png')]);
    const started = fetchMock.mock.calls.map(([, init]) => uploadName(init as RequestInit));
    // 第二份文档被第一份挡住（本地 CPU 串行），两张图片则已经各自发出
    expect(started).not.toContain('第二份.pdf');
    expect(new Set(started)).toEqual(new Set(['第一份.pdf', '图一.png', '图二.png']));
    expect(store.items.map(item => item.state)).toEqual(['parsing', 'queued', 'parsing', 'parsing']);

    running.resolve(json({ source: parsed('doc-1') }));
    await task;
    expect(store.items.map(item => item.state)).toEqual(['ready', 'ready', 'ready', 'ready']);
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it('图片并发：两张图片的解析请求同时在途', async () => {
    const both = [deferred<Response>(), deferred<Response>()];
    const fetchMock = routedFetch({
      '图一.png': () => both[0].promise,
      '图二.png': () => both[1].promise,
    });
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();

    const task = store.addFiles([file('图一.png'), file('图二.png')]);
    // 第一张还没回来，第二张已经发出去了——两个请求的时间区间是重叠的
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(store.items.map(item => item.state)).toEqual(['parsing', 'parsing']);

    both[1].resolve(json({ source: parsed('img-2', { kind: 'image', filename: '图二.png' }) }));
    both[0].resolve(json({ source: parsed('img-1', { kind: 'image', filename: '图一.png' }) }));
    await task;
    expect(store.items.map(item => item.state)).toEqual(['ready', 'ready']);
  });

  it('items 顺序始终等于添加顺序，后加的图片先解析完也不重排', async () => {
    const slowDoc = deferred<Response>();
    const slowImage = deferred<Response>();
    const fetchMock = routedFetch({
      '资料.pdf': () => slowDoc.promise,
      '慢图.png': () => slowImage.promise,
      '快图.png': () => Promise.resolve(json({ source: parsed('fast', { kind: 'image', filename: '快图.png' }) })),
    });
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();

    const task = store.addFiles([file('资料.pdf'), file('慢图.png'), file('快图.png')]);
    const order = store.items.map(item => item.label);
    await flush();
    // 第三项已经就绪、第二项还在解析，列表位置不能因此调换
    expect(store.items.map(item => item.state)).toEqual(['parsing', 'parsing', 'ready']);
    expect(store.items.map(item => item.label)).toEqual(order);

    slowImage.resolve(json({ source: parsed('slow', { kind: 'image', filename: '慢图.png' }) }));
    slowDoc.resolve(json({ source: parsed('doc') }));
    await task;
    expect(store.items.map(item => item.label)).toEqual(order);
    // 解析完成的先后不影响交给生成的顺序
    expect(store.readySources.map(source => source.id)).toEqual(['doc', 'slow', 'fast']);
  });

  it('单个条目失败只影响自己：文档队列继续，图片互不牵连', async () => {
    const fetchMock = routedFetch({
      '坏.pdf': () => Promise.resolve(json({ detail: '文档解析失败' }, 422)),
      '好.pdf': () => Promise.resolve(json({ source: parsed('doc-2') })),
      '坏图.png': () => Promise.resolve(json({ detail: '图片识别暂不可用' }, 422)),
      '好图.png': () => Promise.resolve(json({ source: parsed('img-2', { kind: 'image' }) })),
    });
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();

    await store.addFiles([file('坏.pdf'), file('好.pdf'), file('坏图.png'), file('好图.png')]);

    expect(store.items.map(item => item.state)).toEqual(['failed', 'ready', 'failed', 'ready']);
    expect(store.items[0].error).toBe('文档解析失败');
    expect(store.items[2].error).toBe('图片识别暂不可用');
    expect(store.readySources.map(source => source.id)).toEqual(['doc-2', 'img-2']);
  });

  it.each([
    ['大图.png', MAX_UPLOAD_BYTES + 1, '10MB'],
    ['长录音.WAV', MAX_AUDIO_BYTES + 1, '20MB'],
    ['空文件.pdf', 0, '内容为空'],
  ])('本地拦截无效文件 %s，不上传', async (name, size, message) => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();
    await store.addFiles([file(name, size)]);
    expect(store.items[0].error).toContain(message);
    expect(store.items[0].state).toBe('failed');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('10MB 以上、20MB 以内的 WAV 可以上传', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ source: parsed('audio', { kind: 'audio' }) }));
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();
    await store.addFiles([file('录音.WAV', MAX_AUDIO_BYTES)]);
    expect(store.items[0].state).toBe('ready');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    { text: '   ' }, { status: 'failed' as const, error: '识别失败' }, { status: 'pending' as const },
  ])('后端返回空正文或未解析来源时不标记就绪：%j', async overrides => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ source: parsed('bad', overrides) })));
    const store = useSourcesStore();
    await store.addFiles([file()]);
    expect(store.items[0].state).toBe('failed');
    expect(store.readySources).toEqual([]);
  });

  it('50 个来源上限包含文本与文件，超出批次整体拒绝且不丢原来源', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();
    for (let i = 0; i < MAX_SOURCES - 1; i++) store.addText(`文本 ${i}`);
    await store.addFiles([file('a.pdf'), file('b.pdf')]);
    expect(store.items).toHaveLength(49);
    expect(store.error).toContain('本次未添加');
    expect(store.addText('最后一条')).toBe(true);
    expect(store.addText('超出')).toBe(false);
    expect(store.items).toHaveLength(50);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('空白文本不加入列表', () => {
    const store = useSourcesStore();
    expect(store.addText(' \n\t ')).toBe(false);
    expect(store.items).toEqual([]);
  });
});

describe('入库认领之后的行身份', () => {
  it('两份相同内容的材料映射到同一个持久 id：行还是两行，只删一行', () => {
    const store = useSourcesStore();
    store.addText('同一段正文');
    store.addText('同一段正文');
    const [first, second] = store.items;
    expect(first.key).not.toBe(second.key);

    // 知识库按正文判重：两份内容一样，入库后就是同一条材料
    store.remap({ [first.id]: '持久-同', [second.id]: '持久-同' });

    // id 允许相同，行身份不变，各自还能删
    expect(store.items.map(item => item.key)).toEqual([first.key, second.key]);
    expect(store.items.map(item => item.id)).toEqual(['持久-同', '持久-同']);
    expect(store.items.map(item => item.source?.id)).toEqual(['持久-同', '持久-同']);

    // 同一个 id 只送一次：重复送会被 sourceInputError 判成"来源 ID 重复"，
    // 生成按钮直接锁死，而这恰恰是正常入库之后的状态
    expect(store.readySources).toHaveLength(1);
    expect(store.problem).toBe('');

    store.remove(first.key);
    expect(store.items).toHaveLength(1);
    expect(store.items[0].key).toBe(second.key);
  });
});

describe('合并生成边界', () => {
  it('长度按 Unicode 码点并计入分隔换行，刚好到上限可提交，超出拦截', () => {
    const valid = [textSource('😀'.repeat(MAX_INPUT_CHARS - 3)), textSource(' 字 ')];
    expect(mergedCharCount(valid)).toBe(MAX_INPUT_CHARS);
    expect(sourceInputError(valid)).toBe('');
    expect(sourceInputError([...valid, textSource('多')])).toContain(`${MAX_INPUT_CHARS + 3} 字`);
  });

  it.each([
    { sources: [] as Source[], message: '至少一个来源' },
    { sources: [parsed(), parsed()], message: '来源 ID 重复' },
    { sources: [parsed('bad', { status: 'failed' })], message: '尚未解析成功' },
    { sources: Array.from({ length: 51 }, (_, i) => parsed(String(i))), message: '最多添加 50' },
  ])('无效来源过不了界面校验：$message', ({ sources, message }) => {
    // 校验是纯函数：界面拿它决定生成按钮按不按得下去。直接测它，就不必再借
    // 一条已经删掉的生成通道来断言"没发请求"。
    expect(sourceInputError(sources)).toContain(message);
  });
});

/**
 * 「只上传材料、不写主题」时用材料标题当主题。
 *
 * 判据是"这个标题有没有携带信息"：`未命名`、`扫描件`、`IMG_001` 的长度都过得了
 * "非空"这道关，可它们描述的是文件而不是内容——拿它当中心主题，生成的是一张以
 * 垃圾词为根的图，比让用户补一句主题更糟。
 */
describe('材料标题能不能当主题', () => {
  it('去掉扩展名，但不按"最后一个点"切', () => {
    expect(fileTitle('机器学习导论.pdf')).toBe('机器学习导论');
    expect(fileTitle('  年度总结  .docx')).toBe('年度总结');
    // `方案 v1.2` 按点切会变成 `方案 v1`，而它本来就该原样当标题
    expect(fileTitle('方案 v1.2')).toBe('方案 v1.2');
    expect(fileTitle('没有扩展名')).toBe('没有扩展名');
  });

  it.each([
    '未命名.txt', '扫描件.pdf', 'IMG_001.png', 'screenshot_20241117.png',
    '新建文本文档.txt', '12345.txt', '(2).pdf', 'a.pdf', '录音.wav', 'untitled.md',
  ])('%s 当不了主题，并给出一句原因', (filename) => {
    const result = materialTitle([{ ...parsed('s1'), filename }]);
    expect(result.topic).toBe('');
    // 原因必须说得出来：灰着不解释，用户只会以为功能坏了
    expect(result.reason.length).toBeGreaterThan(4);
  });

  it('原因里带上那个标题，用户才知道说的是哪一份', () => {
    const result = materialTitle([{ ...parsed('s1'), filename: '未命名.txt' }]);
    expect(result.reason).toContain('未命名');
    expect(result.reason).toContain('通用词');
  });

  it('太长的标题也不当主题（那是文件名，不是主题）', () => {
    const long = `${'很长的文件名'.repeat(TOPIC_MAX_CHARS)}.pdf`;
    expect(materialTitle([{ ...parsed('s1'), filename: long }]).topic).toBe('');
  });

  it('常规标题能用，并说明取自哪一份', () => {
    const result = materialTitle([{ ...parsed('s1'), filename: '机器学习导论.pdf' }]);
    expect(result.topic).toBe('机器学习导论');
    expect(result.from).toBe('机器学习导论.pdf');
    expect(result.reason).toBe('');
  });

  it('剪掉「(2)」「- 副本」这类尾巴', () => {
    expect(materialTitle([{ ...parsed('s1'), filename: '手册 (2).pdf' }]).topic).toBe('手册');
    expect(materialTitle([{ ...parsed('s1'), filename: '手册 - 副本.pdf' }]).topic).toBe('手册');
  });

  it('粘贴的文本没有标题：不猜，让用户写主题', () => {
    const result = materialTitle([textSource('一段正文')]);
    expect(result.topic).toBe('');
    expect(result.reason).toContain('粘贴的材料没有标题');
  });

  it('第一份不可用时往后找，用第二份的标题', () => {
    const result = materialTitle([
      { ...parsed('a'), filename: 'IMG_2041.png' },
      { ...parsed('b'), filename: '碳中和路线图.pdf' },
    ]);
    expect(result.topic).toBe('碳中和路线图');
    expect(result.from).toBe('碳中和路线图.pdf');
  });

  it('没有材料时不给原因：那不是错误，只是还没加材料', () => {
    expect(materialTitle([])).toEqual({ topic: '', from: '', reason: '' });
  });
});

describe('材料是可选的，不是必需的', () => {
  it('没有材料时 materialProblem 是空串（problem 却是"请先添加来源"）', () => {
    const store = useSourcesStore();
    expect(store.problem).toContain('至少一个来源');
    // 合并之后生成可以没有任何材料：谁把 problem 直接摆到按钮上，用户就会看到
    // 一个永远禁用的按钮和一句要求他去加材料的话。
    expect(store.materialProblem).toBe('');
  });

  it('有材料时两者一致', () => {
    const store = useSourcesStore();
    store.addText('正文');
    expect(store.materialProblem).toBe(store.problem);
    expect(store.materialProblem).toBe('');
  });

  it('解析失败时材料闸门给出可操作的一句话', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ detail: '解析失败' }, 422)));
    const store = useSourcesStore();
    await store.addFiles([file('坏.pdf')]);
    expect(store.materialProblem).toContain('重试或移除');
  });
});

/**
 * 链接材料是唯一一条"异步而且没有本地文件"的输入路径。
 *
 * 它出过一次会永久卡住的 bug：行先以 `parsing` 插进列表，响应回来之后改的却是
 * push 进去的**原始对象**。原始对象不在响应式图里，`parsing` 这个 computed 的依赖
 * 一点没脏——行自己（经由代理读到同一份 target）已经是 `ready`，而界面上的
 * "解析中"再也不消失，重试又按本地文件处理、同样永远不动。
 */
describe('链接材料', () => {
  it('抓取完成之后 parsing 跟着变成 false（要改列表里那一行，不是原始对象）', async () => {
    const pending = deferred<Response>();
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(pending.promise));
    const store = useSourcesStore();

    const task = store.addFetchedUrl('https://example.com/a');
    // 先读一次：`parsing` 在这里被求值并缓存。此后那一行若不是响应式代理，
    // 它再也不会重算——这条断言就是那个 bug 的现场。
    expect(store.parsing).toBe(true);
    expect(store.items.map(item => item.state)).toEqual(['parsing']);

    pending.resolve(json({
      source: parsed('web-1', { kind: 'web', filename: 'deepseek: 示例页' }),
    }));
    await task;

    expect(store.items[0].state).toBe('ready');
    expect(store.items[0].label).toBe('deepseek: 示例页');
    expect(store.parsing).toBe(false);
  });

  it('失败后重试按原链接重抓，不会去等一个没有文件的任务', async () => {
    const first = deferred<Response>();
    const fetchMock = vi.fn()
      .mockReturnValueOnce(first.promise)
      .mockResolvedValueOnce(json({
        source: parsed('web-2', { kind: 'web', filename: 'deepseek: 回来了' }),
      }));
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();

    const task = store.addFetchedUrl('https://example.com/b');
    // 后端对"抓不到"给的是 502 + 一句人话，前端直接把它写在那一行上。
    first.resolve(json({ detail: '抓不到这个页面' }, 502));
    await task;
    expect(store.items[0].state).toBe('failed');
    expect(store.items[0].error).toBe('抓不到这个页面');
    expect(store.parsing).toBe(false);

    await store.retry(store.items[0].key);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toBe('/api/knowledge/fetch');
    expect(String(fetchMock.mock.calls[1][1].body)).toContain('https://example.com/b');
    expect(store.items[0].state).toBe('ready');
    expect(store.parsing).toBe(false);
    expect(store.readySources.map(source => source.id)).toEqual(['web-2']);
  });

  it('抓取途中被删掉：迟到的响应不复活那一行', async () => {
    const pending = deferred<Response>();
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(pending.promise));
    const store = useSourcesStore();

    const task = store.addFetchedUrl('https://example.com/c');
    store.remove(store.items[0].key);
    pending.resolve(json({ source: parsed('web-3', { kind: 'web' }) }));
    await task;

    expect(store.items).toEqual([]);
    expect(store.parsing).toBe(false);
  });
});

/**
 * 上传卡在「正在解析…N 秒」——手册第一条上手路径上那处（验收记录 §四.2 / §五）。
 *
 * 这一组钉的不是"跑得快"（CI 机器的速度不可控），而是**每一条出口都必须落到终态**：
 * 一份材料进了 `parsing` 之后，要么 ready，要么 failed + 一句能照做的话。停在
 * `parsing` 就是用户看到的那一幕——秒数在涨、按钮一直灰、没有出口。
 *
 * 现场的机制是请求本身慢（后端把 `.md` 交给 Docling，冷启动 8.9 秒；进程级
 * `_parse_lock` 串行，多份一起进门还要排队），而前端那条 fetch **没有超时**：
 * 响应迟到多久它就等多久，等到用户以为坏了。所以这里的第一条用例就是那条线。
 */
describe('上传解析的出口', () => {
  /** 真 fetch 在 signal abort 时拒绝；桩不照做的话超时根本走不到 catch。 */
  function hangingFetch() {
    return vi.fn((_url: string, init?: RequestInit) => new Promise<Response>((_ok, fail) => {
      init?.signal?.addEventListener('abort', () => {
        fail(new DOMException('aborted', 'AbortError'));
      });
    }));
  }

  it('请求挂住超过上限：落 failed，并给出可照做的下一步', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', hangingFetch());
    const store = useSourcesStore();

    const task = store.addFiles([file('方案.md')]);
    expect(store.items[0].state).toBe('parsing');
    expect(store.parsing).toBe(true);

    await vi.advanceTimersByTimeAsync(UPLOAD_TIMEOUT_MS + 1);
    await task;

    expect(store.items[0].state).toBe('failed');
    // 文案要能照做：说清等了多久、为什么可能这么慢、下一步点哪里。
    expect(store.items[0].error).toContain('重试解析');
    expect(store.items[0].error).toContain(`${Math.round(UPLOAD_TIMEOUT_MS / 1000)} 秒`);
    expect(store.parsing).toBe(false);
    vi.useRealTimers();
  });

  it('清空后撤销：那一行落到可重试的失败态，不会停在「正在解析」', async () => {
    const second = deferred<Response>();
    let n = 0;
    // 第一次请求被 `restore` 里的 abort 打断，第二次（用户点重试之后）才回包。
    vi.stubGlobal('fetch', vi.fn((_url: string, init?: RequestInit) => {
      n += 1;
      if (n > 1) return second.promise;
      return new Promise<Response>((_ok, fail) => {
        init?.signal?.addEventListener('abort', () => fail(new DOMException('aborted', 'AbortError')));
      });
    }));
    const store = useSourcesStore();

    const task = store.addFiles([file('方案.md')]);
    const saved = store.snapshot();
    // `restore` 用 `map(...)` 造新对象：列表里那一行不再是 `parseItem` 闭包里抓住
    // 的那个对象，而且它会 abort 掉在途的那一条。落点必须是**当下这一行**，
    // 否则界面停在「正在解析…」不动（验收记录 §四.2 那张 H0 截图）。
    store.restore(saved);
    await task;
    await flush();

    expect(store.items[0].state).toBe('failed');
    expect(store.items[0].error).toContain('重试');
    expect(store.parsing).toBe(false);

    const retry = store.retry(store.items[0].key);
    second.resolve(json({ source: parsed('s-restored') }));
    await retry;
    await flush();

    expect(store.items[0].state).toBe('ready');
    expect(store.readySources.map(source => source.id)).toEqual(['s-restored']);
    expect(store.parsing).toBe(false);
  });

  it('解析成功但正文是空的：落 failed，不留一个"就绪"的空来源', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      json({ source: parsed('blank', { text: '   ', status: 'parsed' }) }),
    ));
    const store = useSourcesStore();

    await store.addFiles([file('扫描件.pdf')]);

    expect(store.items[0].state).toBe('failed');
    expect(store.items[0].error).toContain('没有解析出可用文字');
    expect(store.readySources).toEqual([]);
  });
});
