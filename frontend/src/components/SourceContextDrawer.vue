<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';

import { readLibrarySource } from '../api/client';
import {
  CONTEXT_PAD,
  beyondLoaded,
  contextSlice,
  cycleIndex,
  growPad,
} from '../mindmap/context-window';
import { locateAll, type RefLocation, type ResolvedRef } from '../mindmap/refs';

/**
 * 引用原文抽屉：**点引用 → 看到那句 → 看上下文**。
 *
 * 这条链路的数据分两处住，各自缺一块，组件必须两边都问：
 *
 *   1. **本次会话**解析过的来源：`activeRef.source.text` 里有全文，直接定位；
 *   2. **已保存的导图**重开之后：`sources` store 是空的，`source_refs` 只剩
 *      `source_id` + `start/end` + `quote`。这时正文只能按 id 从知识库取
 *      （`/knowledge/library/{id}`），而那个接口默认只回 4000 字——`start/end`
 *      是**全文码点偏移**，37973 字的材料里第 20000 字处的引用，用预览永远定位
 *      不到。所以这里有`MAX_READ_CHARS` 这个上限并如实传 `total_chars`。
 *
 * 三个"看起来能用其实不能用"的降级，各自有明确措辞（顶部 `note` 计算属性）：
 *   - 来源不在知识库里（粘贴的临时文本、已被清理的材料）→ 只能看摘录；
 *   - 正文超过单次上限 → 只载入了前 N 字，位置之后的引用看不到；
 *   - 引文在正文里定位不到 → 不高亮，只显示引文本身，并说明原因。
 * 把三者混成一句"原文不可用"，用户就分不清是功能坏了还是这份材料本来就没依据。
 */
const props = defineProps<{
  /** 要看的引用；null 表示抽屉关着（父组件用 v-if 控制）。 */
  item: ResolvedRef | null;
}>();

const emit = defineEmits<{ close: [] }>();

/**
 * 单次最多取多少字正文。与后端 `shelf.MAX_READ_CHARS` 同一个数。
 *
 * 不在请求里传超大值让后端夹：那个接口的 `le` 会把越界的 `limit` 挡成 422，
 * 传 100 万会直接报错而不是"少给一点"。所以这里就必须知道上限是多少。
 */
const MAX_READ_CHARS = 200_000;

/** 正文缓存：同一个 source 在一张图里会被几十个节点引用，每次点开都重拉一遍没有意义。 */
const cache = new Map<string, { text: string; total: number; truncated: boolean }>();

const text = ref('');
const total = ref(0);
const truncated = ref(false);
const loading = ref(false);
const error = ref('');
/** 上下文默认折叠：用户先确认"是不是这句"，再决定要不要读它的上下文。 */
const showContext = ref(false);
const pad = ref(CONTEXT_PAD);
/** 多次命中时当前停在第几处。 */
const hitIndex = ref(0);

const ref0Id = computed(() => props.item?.ref.source_id ?? '');

/**
 * 定位一次，三个视图（当前处、命中总数、窗口切片）共用。
 *
 * 不在这里分开算：`locateAll` 要扫一遍正文，37973 字的材料上是毫秒级，
 * 而刚才那版把它在 `location` 与 `hits` 里各调一次，等于每次渲染扫两遍。
 */
const located = computed<RefLocation | null>(() => {
  const item = props.item;
  if (!item || !text.value) return null;
  const found = locateAll(text.value, item.ref);
  return found.via === 'none' ? null : found;
});

/** 同一句引文在正文里的**候选处数**。有坐标的引用恒为 1。 */
const hits = computed(() => {
  const found = located.value;
  if (!found) return 0;
  return found.via === 'quote' ? 1 + found.others.length : 1;
});

/** 当前停在哪一处。多次命中时按 `hitIndex` 轮换。 */
const location = computed<RefLocation | null>(() => {
  const found = located.value;
  if (!found) return null;
  const starts = [found.start, ...found.others];
  const index = ((hitIndex.value % starts.length) + starts.length) % starts.length;
  const start = starts[index];
  const length = found.end - found.start;
  // 按**索引**过滤而不是按值：同一处重复出现时（重叠命中）按值会把两处一起删掉。
  return { start, end: start + length, others: starts.filter((_, i) => i !== index), via: found.via };
});

const slice = computed(() => {
  const item = props.item;
  const at = location.value;
  if (!item || !at) return null;
  return contextSlice(text.value, at.start, at.end, total.value, pad.value);
});

/** 引用位置超出了这次载入的正文（正文被上限截断时才会发生）。 */
const beyond = computed(() => {
  const at = location.value;
  if (at) return false;
  const item = props.item;
  if (!item) return false;
  const start = item.ref.start ?? 0;
  return beyondLoaded(start, Array.from(text.value).length, total.value);
});

/** 已经贴到某一边，就不给那一边的"再读"按钮：点了没反应比没有按钮更糟。 */
const canGrowBefore = computed(() => slice.value?.moreBefore ?? false);
const canGrowAfter = computed(() => slice.value?.moreAfter ?? false);

const where = computed(() => {
  const at = location.value;
  if (!at) return props.item?.where || '';
  return `第 ${at.start + 1}–${at.end} 字 · 共 ${total.value.toLocaleString('zh-CN')} 字`;
});

/**
 * 一句"现在是什么情况"，按三档降级写。
 *
 * 空串表示**一切正常**（有正文、定位到了）——界面不该在正常时说废话。
 */
const note = computed(() => {
  const item = props.item;
  if (!item) return '';
  if (error.value) return error.value;
  if (loading.value) return '正在读取原文…';
  if (!text.value) return '这份来源没有可回查的原文：它可能是一次性粘贴的文本，或者已被从知识库里清理。下面只是生成时记下的摘录。';
  if (beyond.value) {
    return `这份材料共 ${total.value.toLocaleString('zh-CN')} 字，界面一次只载入前 ${MAX_READ_CHARS.toLocaleString('zh-CN')} 字，这条引用在第 ${(item.ref.start ?? 0) + 1} 字处，落在没载入的部分。`;
  }
  if (!location.value) {
    return '这段引文在这份材料的正文里定位不到（逐字比对不上）。摘录仍然保留，但无法高亮原文。';
  }
  if (location.value.via === 'quote') {
    return `这条引用没有坐标，是按引文在原文里搜到的位置。${hits.value > 1 ? `同一句在全文出现 ${hits.value} 次，可用「上一处 / 下一处」逐个核对。` : ''}`;
  }
  if (truncated.value) {
    return `这份材料共 ${total.value.toLocaleString('zh-CN')} 字，已全部定位；上面显示的是引用前后的一段。`;
  }
  return '';
});

/** 逐字原文：优先用引用自己带的 `quote`（后端校验过），没有再退回定位到的那段。 */
const quoteText = computed(() => {
  const item = props.item;
  if (!item) return '';
  if (item.quote) return item.quote;
  return location.value ? slice.value?.hit ?? '' : '';
});

const markRef = ref<HTMLElement | null>(null);

async function scrollToHit(): Promise<void> {
  await nextTick();
  const mark = markRef.value;
  // jsdom 里没有 `scrollIntoView`：测试挂载这个组件时不该因为"没法滚动"而报错。
  if (mark && typeof mark.scrollIntoView === 'function') {
    mark.scrollIntoView({ block: 'center' });
  }
}

async function load(item: ResolvedRef): Promise<void> {
  error.value = '';
  hitIndex.value = 0;
  pad.value = CONTEXT_PAD;
  showContext.value = false;

  const local = item.source?.text ?? '';
  if (local) {
    text.value = local;
    total.value = Array.from(local).length;
    truncated.value = false;
    return;
  }

  const id = item.ref.source_id;
  const cached = cache.get(id);
  if (cached) {
    text.value = cached.text;
    total.value = cached.total;
    truncated.value = cached.truncated;
    return;
  }

  text.value = '';
  total.value = 0;
  truncated.value = false;
  loading.value = true;
  try {
    const payload = await readLibrarySource(id, MAX_READ_CHARS);
    // 等响应期间用户可能已经关掉或换了另一条：只让当前这条落地。
    if (ref0Id.value !== id) return;
    const plain = payload.text ?? '';
    cache.set(id, { text: plain, total: payload.total_chars, truncated: payload.truncated });
    text.value = plain;
    total.value = payload.total_chars;
    truncated.value = payload.truncated;
  } catch (cause) {
    if (ref0Id.value !== id) return;
    error.value = `原文没能读取：${(cause as Error).message}`;
  } finally {
    if (ref0Id.value === id) loading.value = false;
  }
}

watch(
  () => props.item,
  item => {
    if (!item) return;
    void load(item);
  },
  { immediate: true },
);

async function openContext(): Promise<void> {
  showContext.value = true;
  await scrollToHit();
}

function grow(direction: 'before' | 'after'): void {
  const at = location.value;
  if (!at) return;
  pad.value = growPad(pad.value, direction, at.start, at.end, total.value);
  void scrollToHit();
}

function cycle(delta: number): void {
  hitIndex.value = cycleIndex(hitIndex.value, delta, hits.value);
  void scrollToHit();
}
</script>

<template>
  <aside
    v-if="item"
    class="fixed inset-y-0 right-0 z-40 flex w-[520px] max-w-full flex-col border-l border-slate-200 bg-white shadow-2xl"
    role="dialog"
    aria-modal="true"
    aria-labelledby="ref-title"
    data-test="ref-drawer"
  >
    <header class="flex items-start justify-between gap-3 border-b border-slate-200 p-4">
      <div class="min-w-0">
        <h2 id="ref-title" class="truncate text-sm font-semibold">{{ item.name }}</h2>
        <p class="mt-0.5 text-xs text-slate-500" data-test="ref-where">{{ where || '未标注位置' }}</p>
      </div>
      <button
        type="button"
        class="shrink-0 text-xs text-slate-500 hover:text-slate-900"
        @click="emit('close')"
      >
        关闭
      </button>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-4">
      <!-- 这句引文本身。逐字来自后端校验过的 `quote`，与下面的高亮分开显示：
           高亮可能因为"定位不到/没载入"而缺席，而这句引用始终要看得见。 -->
      <p class="mb-3 whitespace-pre-wrap break-words rounded-md border border-slate-200 bg-slate-50 p-3 text-sm leading-relaxed text-slate-700" data-test="ref-quote">
        {{ quoteText || '（这条引用没有摘录）' }}
      </p>

      <div class="flex flex-wrap items-center gap-2">
        <button
          v-if="location"
          type="button"
          data-test="ref-context-toggle"
          class="rounded-md border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:border-slate-900 hover:bg-slate-900 hover:text-white"
          @click="showContext ? (showContext = false) : openContext()"
        >{{ showContext ? '收起上下文' : '看上下文' }}</button>

        <template v-if="hits > 1">
          <button
            type="button"
            data-test="ref-prev"
            class="rounded-md border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:border-slate-900"
            @click="cycle(-1)"
          >上一处</button>
          <span class="text-xs text-slate-500" data-test="ref-hit-index">
            第 {{ (hitIndex % hits) + 1 }} / {{ hits }} 处
          </span>
          <button
            type="button"
            data-test="ref-next"
            class="rounded-md border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:border-slate-900"
            @click="cycle(1)"
          >下一处</button>
        </template>
      </div>

      <!-- 上下文：命中前后各留一段，孤零零一句读不出意思（`CONTEXT_PAD` 的来历）。 -->
      <section
        v-if="showContext && slice"
        class="mt-3"
        data-test="ref-context"
        aria-label="原文上下文"
      >
        <button
          v-if="canGrowBefore"
          type="button"
          data-test="ref-grow-before"
          class="mb-2 text-xs text-sky-700 underline decoration-sky-300 underline-offset-2 hover:text-sky-900"
          @click="grow('before')"
        >再读上文</button>
        <p class="whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-700">
          {{ slice.before }}<mark ref="markRef" class="rounded bg-amber-200 px-0.5 text-slate-900">{{ slice.hit }}</mark>{{ slice.after }}
        </p>
        <button
          v-if="canGrowAfter"
          type="button"
          data-test="ref-grow-after"
          class="mt-2 text-xs text-sky-700 underline decoration-sky-300 underline-offset-2 hover:text-sky-900"
          @click="grow('after')"
        >再读下文</button>
      </section>

      <!-- 没有坐标时也给一条出口：外部来源（维基/arXiv）可以到原始地址核对。 -->
      <p v-if="item.url" class="mt-4 text-xs leading-relaxed text-slate-500">
        <a
          :href="item.url"
          target="_blank"
          rel="noopener noreferrer"
          class="text-sky-700 underline decoration-sky-300 underline-offset-2 hover:text-sky-900"
        >在新窗口打开原始出处</a>
      </p>

      <p
        v-if="note"
        data-test="ref-note"
        class="mt-4 rounded-md bg-slate-50 p-3 text-xs leading-relaxed text-slate-500"
      >{{ note }}</p>
    </div>
  </aside>
</template>
