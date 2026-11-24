<script setup lang="ts">
import { computed } from 'vue';

import RecordButton from './RecordButton.vue';
import { FILE_ACCEPT, MAX_INPUT_CHARS, MAX_SOURCES, mergedCharCount, SOURCE_LABELS, sourceWarning } from '../sources/input';
import { useMindMapStore } from '../stores/mindmap';
import { useSourcesStore } from '../stores/sources';

/**
 * **材料区**：它现在住在底部对话框的「+」面板里（`ChatDock.vue`），不再是一个常驻
 * 左栏——那 320px 让画布永远窄一截，而材料只在"准备"那一步要看。
 *
 * 这里只放材料：粘贴的正文、上传的文档/图片、录音，以及它们的来源列表与清空。
 * 主题和「怎么改」不在这里——那是画布底部那条对话框（`ChatDock.vue` 的 `#intent`）。
 *
 * **正文草稿不在这个组件里**（`sources.draftText`）。它曾经是这里的 `ref`，而这个
 * 组件在生成中会被 `v-if` 整个卸载（对话框收成一条细进度）：还没点「添加」的正文
 * 当场消失，用户以为是自己弄丢了。`beforeunload` 提醒同理由跟着搬到常驻的
 * `ChatDock`。
 */
const store = useMindMapStore();
const sources = useSourcesStore();

/** 关页面提醒的挂载点放在 `ChatDock`（它常驻），这里只留一个说明性引用。 */

const recording = computed(() => sources.recording);
/** 生成期间整块冻结：中途加的来源对这一轮不生效，让用户以为它生效比禁用更糟。 */
const generating = computed(() => store.busy);

const charCount = computed(() => mergedCharCount(sources.readySources));
const problem = computed(() => sources.materialProblem);

/**
 * 有导图时才谈"这份来源有没有被引用"。
 *
 * 还没生成就摆一条 amber 提示，等于催用户去做他正要做的下一步；而且刚添加完
 * 材料时所有来源都还没被引用，满屏黄字只会让人以为出了问题。
 */
const showRefState = computed(() => !!store.content);


function refCount(sourceId: string): number {
  return store.refIndex.counts[sourceId] ?? 0;
}

function refHits(sourceId: string) {
  return store.refIndex.hits[sourceId] ?? [];
}

async function onPickFiles(event: Event) {
  const input = event.target as HTMLInputElement;
  const selected = Array.from(input.files ?? []);
  // 再选同一个文件也应触发 change。
  input.value = '';
  await sources.addFiles(selected);
}
</script>

<template>
  <div class="flex flex-col gap-3">
    <div class="flex items-center justify-between">
      <h2 class="text-sm font-semibold">材料（可选）</h2>
      <span class="text-xs text-slate-400">文本 · 文档 · 图片 · 语音</span>
    </div>
    <!-- 去掉输入框之后必须说明这里是什么；最后那句"不传材料就按主题生成"是**当前
         默认行为**（`MINDMAP_EXTERNAL_KNOWLEDGE` 默认开），不写用户不知道会联网。 -->
    <p class="text-xs leading-relaxed text-slate-500">
      上传文档、粘贴文字或录音；不传材料就按主题生成（会去外部搜资料）。
    </p>

    <label for="material-text" class="text-xs font-medium text-slate-600">粘贴材料正文</label>
    <textarea
      id="material-text"
      v-model="sources.draftText"
      rows="3"
      :disabled="generating"
      class="resize-y rounded-md border border-slate-300 p-2 text-sm outline-none focus:border-slate-500 disabled:opacity-50"
      placeholder="把要整理的文字贴在这里，它会作为一份材料参与生成"
    />
    <button
      type="button"
      class="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
      :disabled="generating || !sources.draftText.trim()"
      @click="sources.addDraft()"
    >添加文本到来源</button>
    <p class="text-xs leading-relaxed text-slate-400">
      这里加的都算材料；写主题、说「怎么改」在画布底部那个对话框里。
    </p>

    <label
      class="rounded-md border border-dashed border-slate-300 px-3 py-2 text-center text-xs text-slate-600 focus-within:ring-2 focus-within:ring-slate-200"
      :class="generating || recording ? 'opacity-50' : 'cursor-pointer hover:bg-slate-50'"
    >
      <input
        type="file"
        multiple
        class="sr-only"
        :accept="FILE_ACCEPT"
        :disabled="generating || recording"
        @change="onPickFiles"
      />
      添加文件（可多选）
    </label>
    <p class="text-xs leading-relaxed text-slate-400">
      PDF / Word / PPT / Excel / HTML / Markdown / 图片 / WAV。文档、图片每份 10MB，WAV 每份 20MB；PDF 最多识别 20 页。
    </p>
    <RecordButton
      :disabled="generating || sources.items.length >= MAX_SOURCES"
      @active="sources.recording = $event"
      @recorded="sources.addFiles([$event])"
    />

    <div class="flex items-center justify-between text-xs">
      <h3 class="font-medium text-slate-600">来源列表（{{ sources.items.length }}/{{ MAX_SOURCES }}）</h3>
      <button
        v-if="sources.items.length"
        type="button"
        class="text-slate-500 hover:text-slate-900 disabled:opacity-50"
        :disabled="generating"
        @click="sources.clear()"
      >清空来源</button>
    </div>
    <p v-if="!sources.items.length" class="text-xs leading-relaxed text-slate-400">
      多个来源会一起生成；不加材料也行。
    </p>
    <ul v-else aria-label="待生成来源" class="flex flex-col gap-2">
      <li v-for="item in sources.items" :key="item.key" class="rounded-md border border-slate-200 p-2">
        <div class="flex items-start gap-2 text-xs">
          <span class="shrink-0 rounded bg-slate-100 px-1 py-0.5 text-slate-500">{{ SOURCE_LABELS[item.kind] }}</span>
          <span class="min-w-0 flex-1 break-words font-medium text-slate-700">{{ item.label }}</span>
          <button
            type="button"
            class="shrink-0 text-slate-400 hover:text-rose-600 disabled:opacity-50"
            :aria-label="`移除 ${item.label}`"
            :disabled="generating"
            @click="sources.remove(item.key)"
          >移除</button>
        </div>
        <p v-if="item.state === 'queued'" class="mt-1 text-xs text-slate-500">等待解析…</p>
        <p v-else-if="item.state === 'parsing'" class="mt-1 text-xs text-slate-500" aria-live="polite">
          正在解析…已等待 {{ item.seconds }} 秒
        </p>
        <template v-else-if="item.state === 'ready' && item.source">
          <p class="mt-1 text-xs text-emerald-700">
            已就绪 · {{ Array.from(item.source.text?.trim() ?? '').length }} 字
            <span v-if="item.source.parsed_pages != null"> · {{ item.source.parsed_pages }} 页</span>
            <span v-if="item.source.duration_seconds != null"> · {{ Math.round(item.source.duration_seconds) }} 秒</span>
          </p>
          <p v-if="sourceWarning(item.source)" role="alert" class="mt-1 text-xs leading-relaxed text-amber-700">
            {{ sourceWarning(item.source) }}
          </p>
          <details class="mt-1 text-xs text-slate-500">
            <summary class="cursor-pointer">查看正文</summary>
            <p class="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap break-words leading-relaxed">{{ item.source.text }}</p>
          </details>
          <!-- 反向引用：这份材料被哪些节点用上了。没有导图时不提，否则等于在指责用户还没做的事。 -->
          <template v-if="showRefState">
            <details v-if="refCount(item.source.id)" class="mt-1 text-xs text-slate-600">
              <summary class="cursor-pointer">被 {{ refCount(item.source.id) }} 个节点引用</summary>
              <ul class="mt-1 flex flex-col gap-1">
                <li v-for="hit in refHits(item.source.id)" :key="hit.id">
                  <button
                    type="button"
                    class="break-words text-left underline decoration-slate-300 hover:text-slate-900"
                    :title="hit.path.join(' / ')"
                    @click="store.locate(hit.id)"
                  >{{ hit.label }}</button>
                </li>
              </ul>
            </details>
            <p v-else class="mt-1 text-xs leading-relaxed text-amber-700">
              这份来源没有被任何节点引用——生成时可能没用上。
            </p>
          </template>
        </template>
        <template v-else-if="item.state === 'failed'">
          <p role="alert" class="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-rose-700">{{ item.error }}</p>
          <button type="button" class="mt-1 text-xs text-slate-700 underline disabled:opacity-50" :disabled="generating" @click="sources.retry(item.key)">
            重试解析
          </button>
        </template>
      </li>
    </ul>
    <p v-if="sources.parsing" class="text-xs leading-relaxed text-slate-400">
      文件按顺序解析；PDF 逐页识别，约每页 5–10 秒。
    </p>
    <p v-if="sources.error" role="alert" class="text-xs leading-relaxed text-rose-700">{{ sources.error }}</p>
    <template v-if="sources.items.length">
      <p class="text-xs" :class="charCount > MAX_INPUT_CHARS ? 'text-rose-700' : 'text-slate-500'">
        合并后 {{ charCount }} / {{ MAX_INPUT_CHARS }} 字（含分隔换行）
      </p>
      <p v-if="problem" class="text-xs leading-relaxed text-amber-700">{{ problem }}</p>
      <p class="text-xs leading-relaxed text-slate-400">来源正文仅在本页保留；保存导图会保留来源摘要。</p>
    </template>
  </div>
</template>
