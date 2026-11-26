import { defineStore } from 'pinia';
import { computed, onScopeDispose, ref } from 'vue';

import { ApiError, deleteLibrarySource, fetchUrlSource, uploadDocument } from '../api/client';
import type { Source, SourceKind } from '../api/types';
import { fileKind, MAX_AUDIO_BYTES, MAX_SOURCES, MAX_UPLOAD_BYTES, newSourceId, sourceInputError, textSource } from '../sources/input';

export interface SourceItem {
  /**
   * 这一行的本地身份：列表 key、`files`/`active` 的键，`remap` 永远不动它。
   *
   * 不能拿 `id` 兼任：两份内容相同的材料会被知识库收成同一份，映射之后
   * `id` 允许相同，但行必须还是两行独立可删的。
   */
  key: string;
  /** 材料在库里的标识；生成前是临时 id，入库后由 `remap` 换成持久 id。 */
  id: string;
  label: string;
  kind: SourceKind;
  state: 'queued' | 'parsing' | 'ready' | 'failed';
  source: Source | null;
  error: string;
  seconds: number;
  /**
   * 「彻底删除」的两步状态（`forget`）。
   *
   * `armed` 是**已经看到引用后果、再点一次才真删**的那一步：材料被导图引用时后端
   * 先回 409（`detail.cited_by`），那正是"删掉之后那几张图的引用会显示「来源原文
   * 不可用」，且无法恢复"——这句代价必须落到某个按钮上，而不是弹一个与材料清单
   * 脱节的确认框。缺省（`undefined`）与 `idle` 等价。
   */
  deleteState?: 'idle' | 'armed' | 'busy';
}

/** 解析泳道：文档在本地 CPU 上跑，图片/语音是网络往返。 */
type Lane = 'document' | 'media';

/**
 * 「清空」之前的一份可撤销快照（`snapshot()` / `restore()`）。
 *
 * 只给「新对话」用：那个动作会把材料清干净，而材料可能是用户传了十几分钟、
 * 刚解析完还没生成过的东西。带文件的条目要靠 `files` 才能重试，所以连它一起留。
 */
export interface SourcesSnapshot {
  items: SourceItem[];
  draftText: string;
  files: Array<[string, File]>;
  /** 链接材料的地址，重试要用（见 `addFetchedUrl` / `retry`）。 */
  urls: Array<[string, string]>;
}

/**
 * 文档泳道并发度 1，保持串行。
 *
 * 文档走 Docling / pymupdf，是本地 CPU 且 Docling 内部有进程级单例锁，
 * 并发只会互相等锁并把内存叠上去，总耗时不会变好。
 */
const DOCUMENT_CONCURRENCY = 1;

/**
 * 图片 / 语音泳道并发度 2。
 *
 * 两者都是网络往返（视觉识别、云端语音转写），2 路足以把一张图的等待时间
 * 藏在另一张的处理过程里，又不会把识别服务打到限流。
 */
const MEDIA_CONCURRENCY = 2;

/** 本页的输入工作区；File、计时器和 UI 状态不属于 Source 契约。 */
export const useSourcesStore = defineStore('sources', () => {
  const items = ref<SourceItem[]>([]);
  const error = ref('');
  /**
   * 「粘贴材料正文」框里还没点「添加」的草稿。
   *
   * **必须住在这里，不能住组件里**：那个框住在底部对话框的材料面板，而整个对话框
   * 主体在"生成中"那一段被 `v-if="compact"` 换成一条细进度——组件被卸载，本地
   * `ref` 里的几百字正文连同它的 `beforeunload` 提醒一起消失。用户切一下语音、
   * 生成一次，草稿就没了，而界面上没有任何提示。
   *
   * 放在 store 之后，它同时被三件事看见：`ChatDock` 的淡出刹车（有草稿就不淡出）、
   * `beforeunload`（关页面提醒，挂在常驻的 `ChatDock` 上）、以及「新对话」的清空。
   */
  const draftText = ref('');
  /**
   * 正在录音。
   *
   * 放在这里而不是某个组件里，因为**两个组件都要看它**：`RecordButton` 那边决定
   * 按钮形态，画布右上角的生成按钮要据此禁用——录音停下来的那一刻会新增一份材料，
   * 此刻发出去的请求里一定缺这一块。
   */
  const recording = ref(false);
  const files = new Map<string, File>();
  /**
   * 链接材料那一行的原始地址。
   *
   * 文件的重试靠 `files` 拿回字节；链接没有本地副本，地址不留下的话「重试」会走成
   * 文件那条路（`parseItem` 找不到 File 直接 return），条目永远停在"解析中"。
   */
  const urls = new Map<string, string>();
  const active = new Map<string, AbortController>();
  let textNumber = 0;
  let draining: Promise<void> | null = null;

  const parsing = computed(() => items.value.some(item => item.state === 'queued' || item.state === 'parsing'));
  /**
   * 送去生成的材料，按 id 去重。
   *
   * 两份一模一样的材料会入库成同一条记录，`remap` 之后它们共享一个 id；
   * 请求体里重复两次没有意义（后端按 id 建索引），`sourceInputError` 还会
   * 判成"来源 ID 重复"把生成按钮锁死。
   */
  const readySources = computed(() => {
    const seen = new Set<string>();
    const sources: Source[] = [];
    for (const item of items.value) {
      if (item.state !== 'ready' || !item.source || seen.has(item.source.id)) continue;
      seen.add(item.source.id);
      sources.push(item.source);
    }
    return sources;
  });
  const problem = computed(() => {
    if (parsing.value) return '来源正在解析，请等待完成后生成。';
    if (items.value.some(item => item.state === 'failed')) return '有来源解析失败，请重试或移除后生成。';
    return sourceInputError(readySources.value);
  });
  /**
   * 材料作为**可选**输入时的闸门：一份都没有不是错误。
   *
   * `problem` 对空列表返回「请先添加至少一个来源」，那是"合并生成"时代的判据
   * （生成必须要来源）。现在材料可以不传，谁把 `problem` 直接摆到生成按钮上，
   * 用户就会看到一个永远禁用的按钮和一句要求他去加材料的话。
   */
  const materialProblem = computed(() => (items.value.length ? problem.value : ''));

  function hasRoom(count: number): boolean {
    error.value = '';
    if (items.value.length + count <= MAX_SOURCES) return true;
    error.value = `最多添加 ${MAX_SOURCES} 个来源，本次未添加，请减少数量后重试。`;
    return false;
  }

  function addText(text: string): boolean {
    if (!text.trim()) {
      // 这个入口加的是**材料**：说"请输入主题或文本"会让用户以为主题也该贴在这里。
      error.value = '粘贴的内容是空的，没有加进来源。';
      return false;
    }
    if (!hasRoom(1)) return false;
    const source = textSource(text);
    items.value.push({
      key: source.id, id: source.id, label: `文本 ${++textNumber}`, kind: 'text',
      state: 'ready', source, error: '', seconds: 0,
    });
    return true;
  }

  /**
   * 把草稿加进来源，成功才清空草稿。
   *
   * 失败时**保留正文**：超上限、内容为空这两种失败下清空输入框，等于把用户刚贴的
   * 那段文字扔了——而失败原因恰恰要求他改一改再试。
   */
  function addDraft(): boolean {
    if (!addText(draftText.value)) return false;
    draftText.value = '';
    return true;
  }

  /**
   * 一段粘贴进来的正文直接变成材料，返回新条目的 `key`（没加进去是 null）。
   *
   * `addText` 只回 true/false，而"撤回"需要知道刚加的是哪一条：对话框里那句
   * "已放进材料"必须能一键把正文放回输入框（并撤掉那条材料），否则这个自动分流
   * 就成了一次没法反悔的抢夺。条目的 `key` 是行身份，`remove(key)` 认它。
   */
  function addPastedText(text: string): string | null {
    if (!addText(text)) return null;
    return items.value[items.value.length - 1]?.key ?? null;
  }

  /**
   * 列表里那一行。**必须是它，不能是 push 进去的那个原始对象**。
   *
   * `items` 是 `ref<SourceItem[]>`，push 之后数组元素被包成响应式代理；此后改原始
   * 对象不会触发任何依赖，`parsing`（`items.value.some(...)`）会一直停在 `true`
   * ——界面上就是"永久解析中"，而条目自己的 `state` 早就变成 `ready` 了。
   */
  function rowOf(key: string): SourceItem | undefined {
    return items.value.find(entry => entry.key === key);
  }

  /**
   * 抓一个链接并落到那一行上。失败就落在同一行上写原因。
   *
   * 取行用 `rowOf(key)` 而不是闭包里的对象：抓取期间用户可能把它删了、或者撤销
   * 「清空」重建了一行同名 key 的条目。响应回来时以**当下这一行**为准。
   */
  async function loadUrl(key: string, url: string): Promise<void> {
    try {
      const source = await fetchUrlSource(url);
      // 抓取期间用户可能已经把它删了：删掉的条目不能被迟到的响应复活。
      const item = rowOf(key);
      if (!item) return;
      item.source = source;
      item.id = source.id;
      item.kind = source.kind;
      item.label = source.filename ?? item.label;
      item.state = 'ready';
    } catch (cause) {
      const item = rowOf(key);
      if (!item) return;
      item.state = 'failed';
      // 后端的三种失败（没打开 / 没配 key / 抓不到）各有一句给人看的话，直接用。
      item.error = cause instanceof Error ? cause.message : '抓取失败，请重试或改贴正文。';
    }
  }

  /**
   * 贴进来的一个链接 → 后端读成一份材料，返回新条目的 `key`（没加成是 null）。
   *
   * **异步但立刻占一行**：抓取是一次网络往返（本机实测 0.9 秒，慢站点更久），
   * 先把条目以 `parsing` 插进去，用户马上看得到"这条在读了"；失败就落在同一行上
   * 显示原因（`state = 'failed'`），而不是弹一个与材料清单脱节的提示。这个形态与
   * 文件上传完全一致——两种材料在列表里长得一样，用户不必学第二套。
   *
   * `label` 先填主机名，成功后换成后端给的 `filename`（`deepseek: 网页标题`）：
   * 抓取期间还没有标题，而"example.com"比"文本 3"更能让用户认出是哪一条。
   */
  async function addFetchedUrl(url: string): Promise<string | null> {
    if (!hasRoom(1)) return null;
    const key = newSourceId();
    const host = (() => {
      const rest = url.split('://')[1] ?? url;
      return rest.split('/')[0] || url;
    })();
    items.value.push({
      key, id: key, label: host, kind: 'web', state: 'parsing',
      source: null, error: '', seconds: 0,
    });
    urls.set(key, url);
    await loadUrl(key, url);
    return key;
  }

  async function parseItem(item: SourceItem): Promise<void> {    const file = files.get(item.key);
    if (!file) return;
    item.error = '';
    item.seconds = 0;
    const limit = fileKind(file) === 'audio' ? MAX_AUDIO_BYTES : MAX_UPLOAD_BYTES;
    if (!file.size || file.size > limit) {
      item.state = 'failed';
      item.error = file.size
        ? `「${file.name}」超过 ${limit / (1024 * 1024)}MB 上限，请压缩或拆分后再上传。`
        : '文件内容为空';
      return;
    }

    const controller = new AbortController();
    active.set(item.key, controller);
    item.state = 'parsing';
    const started = Date.now();
    const timer = setInterval(() => { item.seconds = Math.floor((Date.now() - started) / 1000); }, 1000);
    try {
      const source = await uploadDocument(file, controller.signal);
      // 已移除/清空的条目不能被迟到的响应重新加回来。
      if (controller.signal.aborted || !files.has(item.key)) return;
      if (source.status !== 'parsed' || !source.text?.trim()) {
        throw new Error(source.error?.trim() || '这个来源没有解析出可用文字，请重试或移除。');
      }
      item.source = source;
      item.kind = source.kind;
      item.state = 'ready';
    } catch (cause) {
      if (controller.signal.aborted || !files.has(item.key)) return;
      item.state = 'failed';
      item.error = cause instanceof Error ? cause.message : '上传失败，请重试。';
    } finally {
      clearInterval(timer);
      active.delete(item.key);
    }
  }

  /**
   * 判泳道用本地文件的类型：item.kind 在解析成功后会改成后端返回的 kind。
   *
   * 只有图片 / 语音走并发泳道，其余（含文本）都归到串行泳道——宁可慢一点，
   * 也不让本地 CPU 的解析两路抢核。
   */
  function laneOf(item: SourceItem): Lane {
    const file = files.get(item.key);
    const kind = file ? fileKind(file) : item.kind;
    return kind === 'image' || kind === 'audio' ? 'media' : 'document';
  }

  /**
   * 跑一条泳道：最多 limit 个解析同时进行，取队时按 items 顺序。
   *
   * 每次都重新找下一个 queued，而不是起跑前切一份快照：解析期间用户可能
   * 追加文件或点重试，它们要能被已经跑起来的泳道接走，而不是等下一轮 drain。
   */
  async function runLane(lane: Lane, limit: number): Promise<void> {
    const skipped = new Set<string>();
    const worker = async (): Promise<void> => {
      for (;;) {
        const next = items.value.find(item =>
          item.state === 'queued' && !skipped.has(item.key) && laneOf(item) === lane);
        if (!next) return;
        await parseItem(next);
        // 没有对应 File 的异常条目不会进入任何终态，不跳过的话同一个 find
        // 会在原地转圈。
        if (next.state === 'queued') skipped.add(next.key);
      }
    };
    await Promise.all(Array.from({ length: limit }, () => worker()));
  }

  /**
   * 按类型分两条泳道：文档串行，图片 / 语音并发 2 路。
   *
   * 只并发「解析过程」：条目入列后就地更新状态，从不按完成顺序重排，
   * 所以 items 的顺序始终等于添加顺序，用户不会看到列表因后加的图片先解析
   * 完而跳动。单条失败只影响它自己——parseItem 内部捕获异常写进 item.error，
   * 泳道继续跑下一项。
   */
  function drain(): Promise<void> {
    if (draining) return draining;
    draining = Promise.all([
      runLane('document', DOCUMENT_CONCURRENCY),
      runLane('media', MEDIA_CONCURRENCY),
    ]).then(() => undefined).finally(() => { draining = null; });
    return draining;
  }

  async function addFiles(selected: File[]): Promise<void> {
    if (!selected.length || !hasRoom(selected.length)) return;
    for (const file of selected) {
      const key = newSourceId();
      files.set(key, file);
      items.value.push({
        key, id: key, label: file.name, kind: fileKind(file), state: 'queued',
        source: null, error: '', seconds: 0,
      });
    }
    await drain();
  }

  async function retry(key: string): Promise<void> {
    const item = rowOf(key);
    if (!item || item.state !== 'failed') return;
    const url = urls.get(key);
    if (url) {
      // 链接材料没有本地文件，重试必须回到那个 URL：走 `queued` 那条路时
      // `parseItem` 第一行在 `files` 里找不到东西就 return，条目会永远停在
      // "解析中"——用户点「重试」看到的就是"没反应"。
      item.error = '';
      item.seconds = 0;
      item.state = 'parsing';
      await loadUrl(key, url);
      return;
    }
    item.state = 'queued';
    await drain();
  }

  /**
   * 按服务端给的对照表把来源 id 换成持久 id。
   *
   * 生成开始时后端会把材料写进知识库，同时用 `sources` 帧告诉前端"旧 id → 新 id"。
   * 只换 id：`item.source.id` 是导图引用与引用计数用的，`item.id` 是给用户看的
   * 材料标识。行身份是 `item.key`，`files`/`active` 都挂在它上面，这里一律不碰——
   * 两份内容相同的材料映射到同一个持久 id 时，两行才还能各自删除、各自解析。
   */
  function remap(ids: Record<string, string>): void {
    const entries = Object.entries(ids);
    if (!entries.length) return;
    for (const item of items.value) {
      const next = ids[item.id] ?? (item.source ? ids[item.source.id] : undefined);
      if (!next || next === item.id) continue;
      if (item.source) item.source = { ...item.source, id: next };
      item.id = next;
    }
  }

  /**
   * 一次可回滚的清空：把"清掉的东西"整体留一份，供「撤销」还原。
   *
   * `files` 也要一起留：没有它，还原回来的失败条目点「重试」会找不到文件而静静停在
   * `queued`（`parseItem` 的第一行就 return），用户看到的是"重试没反应"。
   *
   * 还原时把 `queued` / `parsing` 重新丢给泳道跑一遍：清空那一刻正在解析的条目，
   * 它的 `AbortController` 已经随着 `clear()` 被 abort 且从 `active` 里删掉，
   * 直接放回列表会停在 `parsing` 一秒钟都不会动。
   */
  function snapshot(): SourcesSnapshot {
    return {
      items: [...items.value],
      draftText: draftText.value,
      files: [...files.entries()],
      urls: [...urls.entries()],
    };
  }

  function restore(saved: SourcesSnapshot): void {
    active.forEach(controller => controller.abort());
    active.clear();
    files.clear();
    for (const [key, file] of saved.files) files.set(key, file);
    urls.clear();
    for (const [key, url] of saved.urls) urls.set(key, url);
    items.value = saved.items.map(item =>
      item.state === 'queued' || item.state === 'parsing'
        ? { ...item, state: 'queued' as const, seconds: 0 }
        : item);
    draftText.value = saved.draftText;
    error.value = '';
    void drain();
  }

  /**
   * 这份材料**已经进过知识库**吗。
   *
   * 判据是"行身份没变、但材料 id 变过"：`remap` 只把入库成功的临时 id 换成库里的
   * 持久 id，而行的 `key` 从头到尾不动。所以 `source.id !== key` 等价于
   * "`remember_sources` 收过它"——这是前端唯一能拿到"在不在库里"的凭据，
   * 不必为它多查一次 `/knowledge/library`。
   *
   * 未生成过的材料永远走不进这个分支：上传与粘贴只产出本次请求内有效的 uuid，
   * 入库的唯一入口是生成时那次 `remember_sources`（`knowledge/remember.py` 的
   * 「为什么是"生成时入库"」）。
   */
  function inLibrary(item: SourceItem): boolean {
    return !!item.source && item.source.id !== item.key;
  }

  /**
   * 彻底删除一份材料：库里有就连库一起删，然后从本次会话移出。
   *
   * **为什么还需要它，`remove` 不够吗**：`remove` 是"这一轮不用它"——材料仍留在
   * 知识库里，那是它的本意。而"我传错了"是另一件事：用户要的是"它从我的世界里
   * 消失"。这两件事界面上一度只有一个按钮（「移除」），后果是——传错一份、生成过
   * 一次之后，用户在材料列表里点掉它，知识库里那份**还在**：占着 200 份上限的一个
   * 位置，而且因为 `content_hash` 是唯一键，下次传同一份文件会被静默复用（连上一次
   * 的标签一起，见 `library.py` 的「复用已有记录时只更新 `last_used_at`」）。
   *
   * **未生成过的材料走同一条路，但只有一个动作**：它从没入过库，没有 DELETE 可发，
   * 直接从会话移出就是"删干净"。`inLibrary` 那道判断不是性能优化，是语义——
   * 对一份库里根本没有的材料发删除请求，只会拿到 404，然后界面把"它早就不在"说成
   * "删除失败"。
   *
   * **被导图引用时要两步**：409 不是失败，它带着可执行的下一步。第一次点把后端那句
   * 代价（"还被 N 张导图引用，删掉之后那些节点会显示「来源原文不可用」"）写进
   * `item.error` 并把按钮切成「确认删除」；第二次点才带 `force=true`。
   */
  async function forget(key: string): Promise<void> {
    const item = rowOf(key);
    if (!item || item.deleteState === 'busy') return;
    const force = item.deleteState === 'armed';
    item.deleteState = 'busy';
    item.error = '';
    const libraryId = inLibrary(item) && item.source ? item.source.id : '';
    if (libraryId) {
      try {
        await deleteLibrarySource(libraryId, force);
      } catch (cause) {
        const cited = cause instanceof ApiError && cause.code === 'source_cited';
        item.deleteState = cited ? 'armed' : 'idle';
        item.error = cause instanceof Error ? cause.message : '删除没能完成，请重试。';
        return;
      }
      // 两份内容相同的材料会被知识库收成同一条记录（见 `remap` 的 docstring），
      // 所以删掉这一份的库记录，其他握同一个 id 的行也一起失效——把它们退回
      // "未入库"，而不是留着一个指向已删记录的 id、继续在界面上标"已进知识库"。
      for (const other of items.value) {
        if (other.key !== key && other.source && other.source.id === libraryId) {
          other.source = { ...other.source, id: other.key };
          other.id = other.key;
        }
      }
    }
    remove(key);
  }

  function remove(key: string): void {
    active.get(key)?.abort();
    files.delete(key);
    urls.delete(key);
    items.value = items.value.filter(item => item.key !== key);
    error.value = '';
  }

  function clear(): void {
    active.forEach(controller => controller.abort());
    files.clear();
    urls.clear();
    items.value = [];
    error.value = '';
    recording.value = false;
    // `draftText` **不在这里清**：这个函数服务的是"清空来源"那个按钮，而草稿是
    // 还没成为来源的一段正文。用户点"清空来源"时把他在粘贴框里写了半天的字一起
    // 抹掉，是另一件事。「新对话」要清它时自己清（`App.newConversation`）。
  }

  onScopeDispose(clear);
  return {
    items, error, parsing, recording, readySources, problem, materialProblem,
    draftText,
    addText, addDraft, addPastedText, addFetchedUrl, addFiles, retry, remove, clear, remap,
    inLibrary, forget,
    snapshot, restore,
  };
});
