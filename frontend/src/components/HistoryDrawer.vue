<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import AuthPanel from './AuthPanel.vue';
import ImportMindmap from './ImportMindmap.vue';
import PrefsPanel from './PrefsPanel.vue';
import { exportLabel, type ExportFormat } from '../api/client';
import { useMindMapStore } from '../stores/mindmap';
import { usePrefsStore } from '../stores/prefs';

/**
 * `☰` 打开的左抽屉：历史导图 + 原来顶栏那一排按钮。
 *
 * 顶栏从十来个按钮瘦成三件东西（`☰` / 标题 / `+`），代价是"保存、打开、导入、导出、
 * 演示、知识库、偏好、节点详情"必须有一个新家。它们全在这里，一件不少——每一条都
 * 有 `data-test` 或原有文案，便于逐个点一遍（改版最容易出的事就是某个功能被收进
 * 抽屉之后再也没人找得到）。
 *
 * 抽屉用 `v-show` **常驻挂载**而不是 `v-if`：偏好浮层、导入按钮、导出下拉都在里面，
 * 收起来只是看不见，DOM 与键盘顺序都还在（Esc/焦点归还由 `App` 管）。
 */
const props = defineProps<{
  open: boolean;
  /** 有未保存修改时，导入会问一句——判据与行为都留在 `App`。 */
  confirmDiscard: (action: string) => boolean;
  exporting: boolean;
  exportChoice: ExportFormat | '';
  saveHint: string;
  versionLabel: string;
  modeLabel: string;
  modeTone: string;
  nodeCount: number;
  sideOpen: boolean;
  /** 开发期的布局示例（生产构建传空数组）。 */
  samples: { id: string; label: string }[];
}>();

const emit = defineEmits<{
  close: [];
  save: [];
  newMap: [];
  openList: [];
  openMap: [id: string];
  demo: [];
  library: [];
  importDone: [count: number];
  export: [format: ExportFormat];
  updateExport: [format: ExportFormat | ''],
  loadSample: [id: string];
  toggleSide: [];
  togglePrefs: [];
  prefsOpen: [value: boolean];
}>();

const store = useMindMapStore();
const prefs = usePrefsStore();

const prefsOpen = ref(false);
const prefsWrap = ref<HTMLElement | null>(null);

/**
 * 正在确认删除的那张图。
 *
 * 两段式而不是 `window.confirm`：这个动作**不可恢复**，而系统模态既不受控又说不清
 * "会连什么一起删"。两段式的第一下是可见状态——用户能看见自己点到了哪一行、
 * 旁边写着会失去什么，而且它和材料列表里的「彻底删除」是同一套交互。
 */
const pendingDelete = ref<string | null>(null);

async function deleteMap(id: string): Promise<void> {
  const ok = await store.deleteMap(id);
  if (ok) pendingDelete.value = null;
}

const prefsSuggestCount = computed(() => prefs.suggestions.length);

const prefsTitle = computed(() => {
  if (prefsSuggestCount.value) return `${prefsSuggestCount.value} 条偏好建议待确认`;
  if (prefs.error) return `偏好操作失败：${prefs.error}`;
  return '偏好';
});

function togglePrefs(): void {
  prefsOpen.value = !prefsOpen.value;
  emit('prefsOpen', prefsOpen.value);
}

/**
 * 点浮层外面关掉它。
 *
 * 两个细节都是踩出来的：
 *   · 用 `pointerdown` 而不是 `click`：抽屉里的按钮若在 pointerdown 之后就改变了 DOM，
 *     click 可能落在别的元素上，那样"点一下别处"会变成一次没头没尾的重开；
 *   · 挂在**捕获阶段**：Vue Flow 的 pane 在 `mousedown` 里做了 `stopImmediatePropagation`
 *     （d3-zoom 的常规做法），冒泡阶段根本收不到——不捕获的话，点画布关不掉浮层。
 */
function onDocumentPointerDown(event: PointerEvent): void {
  if (!prefsOpen.value) return;
  const box = prefsWrap.value;
  if (box && event.target instanceof Node && box.contains(event.target)) return;
  prefsOpen.value = false;
  emit('prefsOpen', false);
}

onMounted(() => document.addEventListener('pointerdown', onDocumentPointerDown, true));
onBeforeUnmount(() => document.removeEventListener('pointerdown', onDocumentPointerDown, true));

const EXPORT_FORMATS: ExportFormat[] = ['puml', 'markdown', 'opml', 'json'];

function onExportChange(event: Event): void {
  const value = (event.target as HTMLSelectElement).value as ExportFormat | '';
  emit('updateExport', value);
  if (value) emit('export', value);
}
</script>

<template>
  <div
    v-show="props.open"
    data-test="history-drawer"
    aria-label="历史导图与更多操作"
    class="absolute inset-y-0 left-0 z-40 flex w-[320px] max-w-full flex-col border-r border-slate-200 bg-white shadow-2xl"
  >
    <div class="flex items-center justify-between border-b border-slate-200 px-4 py-2">
      <p class="text-xs font-semibold text-slate-700">历史导图</p>
      <button
        type="button"
        data-test="history-close"
        class="text-xs text-slate-500 hover:text-slate-900"
        title="关闭（Esc）"
        @click="emit('close')"
      >关闭</button>
    </div>

    <div class="min-h-0 flex-1 overflow-y-auto px-4 py-3 text-xs">
      <!-- 历史：就是原来「打开」那个列表。点一条直接装进画布。 -->
      <p v-if="store.listBusy" class="text-slate-400">正在读取…</p>
      <p v-else-if="!store.maps.length" class="leading-relaxed text-slate-400">
        还没有保存过任何导图。生成之后点「保存」，它就会出现在这里。
      </p>
      <ul v-else class="flex flex-col divide-y divide-slate-100">
        <li v-for="item in store.maps" :key="item.id" class="flex items-start gap-2 py-1.5">
          <button
            type="button"
            class="min-w-0 flex-1 text-left"
            :title="`打开「${item.content?.title ?? '未命名'}」`"
            @click="emit('openMap', item.id)"
          >
            <span class="block truncate text-[13px] text-slate-800">{{ item.content?.title ?? '未命名' }}</span>
            <span class="block text-[11px] text-slate-400">
              版本 {{ item.version }} · {{ item.updated_at.slice(0, 10) }}
            </span>
          </button>
          <template v-if="pendingDelete === item.id">
            <div class="flex shrink-0 flex-col items-end gap-1">
              <button
                type="button"
                data-test="drawer-map-delete-confirm"
                class="rounded bg-rose-600 px-2 py-0.5 text-[11px] text-white hover:bg-rose-700 disabled:opacity-50"
                :disabled="store.deleting === item.id"
                @click="deleteMap(item.id)"
              >{{ store.deleting === item.id ? '删除中…' : '确认删除' }}</button>
              <button
                type="button"
                data-test="drawer-map-delete-cancel"
                class="text-[11px] text-slate-500 hover:text-slate-900"
                @click="pendingDelete = null"
              >取消</button>
            </div>
          </template>
          <button
            v-else
            type="button"
            data-test="drawer-map-delete"
            class="shrink-0 text-[11px] text-slate-400 hover:text-rose-600"
            :title="`彻底删除「${item.content?.title ?? '未命名'}」（不可恢复）`"
            :aria-label="`彻底删除 ${item.content?.title ?? '未命名'}`"
            @click="pendingDelete = item.id"
          >删除</button>
          <!-- 确认态的说明写在下一行（整行宽度），而不是挤在按钮边上：这句话是
               "删了会失去什么"，读的人正在决定要不要点第二个按钮。 -->
          <p
            v-if="pendingDelete === item.id"
            data-test="drawer-map-delete-hint"
            class="w-full text-[11px] leading-relaxed text-rose-700"
          >
            彻底删除这张导图，连同它的改动记录。不可恢复。
          </p>
        </li>
      </ul>
      <button
        type="button"
        data-test="history-open-list"
        class="mt-2 w-full rounded-md border border-slate-300 px-3 py-1.5 text-[11px] text-slate-700 hover:bg-slate-50 disabled:opacity-40"
        :disabled="store.listBusy"
        @click="emit('openList')"
      >打开…</button>

      <!-- 原来顶栏那一排：一件不少 -->
      <div class="mt-4 flex flex-col gap-2 border-t border-slate-200 pt-3">
        <p class="text-[11px] font-medium text-slate-600">操作</p>
        <div class="grid grid-cols-2 gap-2">
          <button
            type="button"
            class="rounded-md bg-slate-900 px-3 py-1.5 text-[11px] text-white disabled:opacity-40"
            :disabled="!store.content || store.saving"
            title="保存（Ctrl+S）"
            @click="emit('save')"
          >保存</button>
          <button
            type="button"
            class="rounded-md border border-slate-300 px-3 py-1.5 text-[11px] text-slate-700 hover:bg-slate-50"
            @click="emit('newMap')"
          >新建</button>
          <button
            type="button"
            class="rounded-md border border-slate-300 px-3 py-1.5 text-[11px] text-slate-700 hover:bg-slate-50"
            @click="emit('demo')"
          >演示示例</button>
          <button
            type="button"
            class="rounded-md border border-slate-300 px-3 py-1.5 text-[11px] text-slate-700 hover:bg-slate-50"
            @click="emit('library')"
          >知识库</button>
        </div>
        <!-- 导入：JSON / Markdown / OPML / XMind / FreeMind / PlantUML，整棵换掉画布 -->
        <ImportMindmap :confirm-discard="props.confirmDiscard" />
        <select
          :value="props.exportChoice"
          class="w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-[11px] text-slate-700 disabled:opacity-40"
          :disabled="!store.content || props.exporting"
          title="导出为文本格式"
          @change="onExportChange"
        >
          <option value="" hidden>{{ props.exporting ? '导出中…' : '导出…' }}</option>
          <option v-for="format in EXPORT_FORMATS" :key="format" :value="format">
            {{ exportLabel(format) }}{{ format === 'json' ? '（可再导入）' : '' }}
          </option>
        </select>

        <div v-if="props.samples.length" class="flex flex-wrap items-center gap-1">
          <span class="text-[11px] text-slate-500">布局示例</span>
          <button
            v-for="item in props.samples"
            :key="item.id"
            type="button"
            class="rounded border border-slate-200 px-2 py-0.5 text-[11px] hover:bg-slate-100"
            @click="emit('loadSample', item.id)"
          >{{ item.label }}</button>
        </div>
      </div>

      <!-- 状态：版本、来历、节点数、有没有没存的东西 -->
      <div class="mt-4 flex flex-col gap-1 border-t border-slate-200 pt-3 text-[11px] text-slate-500">
        <p v-if="props.saveHint" :class="store.dirty ? 'text-amber-600' : 'text-slate-400'" data-test="save-hint">
          {{ props.saveHint }}
        </p>
        <p class="flex flex-wrap items-center gap-2">
          <span class="rounded px-2 py-0.5" :class="props.modeTone">{{ props.modeLabel }}</span>
          <span>{{ props.nodeCount }} 个节点</span>
          <span v-if="props.versionLabel" class="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-slate-500">
            {{ props.versionLabel }}
          </span>
        </p>
        <p class="leading-relaxed text-slate-400">
          双击节点改文字，Tab 加子节点，Enter 加兄弟，Delete 删除，Ctrl+Z 撤销。
        </p>
      </div>

      <!-- 账号：当前身份与登录/注册。低频动作，和保存/打开同级放在抽屉里。 -->
      <AuthPanel />

      <!-- 偏好与节点详情：两个原来在顶栏右侧的入口 -->
      <div class="mt-4 flex items-center gap-2 border-t border-slate-200 pt-3">
        <div ref="prefsWrap" class="relative">
          <button
            type="button"
            data-test="prefs-toggle"
            class="relative flex items-center gap-1 rounded-md border px-2 py-1 text-[11px]"
            :class="prefsOpen ? 'border-slate-900 bg-slate-100 text-slate-900' : 'border-slate-300 text-slate-600 hover:bg-slate-50'"
            :aria-expanded="prefsOpen"
            aria-controls="prefs-popover"
            :title="prefsTitle"
            @click="togglePrefs"
          >
            偏好
            <span
              v-if="prefsSuggestCount"
              data-test="prefs-badge"
              class="ml-1 min-w-[15px] rounded-full bg-sky-600 px-1 text-center text-[10px] leading-[15px] text-white"
            >{{ prefsSuggestCount }}</span>
            <span
              v-else-if="prefs.error"
              data-test="prefs-badge-error"
              class="ml-1 inline-block h-2 w-2 rounded-full bg-rose-500"
              aria-hidden="true"
            />
          </button>
          <!-- 浮层：绝对定位挂在按钮下面，关掉时只改 display，面板内容一直在 DOM 里。 -->
          <div
            v-show="prefsOpen"
            id="prefs-popover"
            data-test="prefs-popover"
            class="absolute left-0 top-full z-50 mt-2 max-h-[calc(100vh-5rem)] w-[300px] max-w-[calc(100vw-2rem)] overflow-y-auto rounded-lg border border-slate-200 bg-white p-2 shadow-2xl"
          >
            <div class="flex items-center justify-between px-1 pb-1">
              <p class="text-xs font-semibold text-slate-700">偏好</p>
              <button
                type="button"
                data-test="prefs-popover-close"
                class="text-[11px] text-slate-500 hover:text-slate-900"
                title="关闭（Esc）"
                @click="prefsOpen = false; emit('prefsOpen', false)"
              >关闭</button>
            </div>
            <PrefsPanel />
          </div>
        </div>

        <button
          type="button"
          data-test="side-toggle"
          class="flex items-center gap-1 rounded-md border px-2 py-1 text-[11px]"
          :class="props.sideOpen
            ? 'border-slate-900 bg-slate-900 text-white'
            : 'border-slate-300 text-slate-600 hover:bg-slate-50'"
          :aria-expanded="props.sideOpen"
          aria-controls="side-panel"
          title="节点详情与本次改动"
          @click="emit('toggleSide')"
        >节点详情</button>
      </div>
    </div>
  </div>
</template>
