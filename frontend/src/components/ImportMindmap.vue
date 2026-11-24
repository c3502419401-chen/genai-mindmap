<script setup lang="ts">
import { computed, ref } from 'vue';

import { ApiError, importMindMap } from '../api/client';
import type { ImportWarning } from '../api/types';
import { kindOf, shapeLabel } from '../mindmap/shape';
import { useMindMapStore } from '../stores/mindmap';

/**
 * 「导入」入口。
 *
 * 为什么是独立组件而不是塞进 `App.vue` 的顶栏：它自己带文件选择、进度、结果与
 * 告警列表，全写进 App.vue 会让那个文件再多出七八个 ref（而它已经 1000 多行）。
 * 顶栏只需要一行 `<ImportMindmap :confirm-discard="confirmDiscard" />`。
 *
 * 三条交互决定：
 *
 *  1. **有未保存修改时先问一句**（走 App 传进来的 `confirm-discard`）。导入会整棵
 *     换掉画布，与"新建""打开另一张"是同一类破坏性动作。
 *  2. **告警在这里列出来，不只写日志**。"截断了 37 个节点""引用已清空"是用户必须
 *     知道的事：少了节点他不会自己发现，而"为什么我的引用没了"没有别的解释入口。
 *     面板可以被关掉，但不会自己消失（成功提示那种 3 秒浮层不够读完 5 条告警）。
 *  3. **失败要说清是哪一步**。后端已经给出"第 12 行第 5 列"这种定位，界面只负责
 *     原样显示 `message`，不再自己编一句"导入失败"把它盖掉。
 *
 * 导入的产物**可能是三种形状**（树 / 鱼骨 / 流程），所以这里不碰 `content.root`：
 * 图型一律走 `shape.ts::kindOf()`。那句"已导入一张鱼骨图"是这条路径唯一说得出
 * "后端真的导出了另一种形状"的地方——少了它，一张鱼骨图导进来之后画布长得像鱼骨，
 * 而界面上没有任何一句话承认过这件事。
 */

const props = defineProps<{
  /** 有未保存修改时确认是否丢弃。由 App 注入（与「新建」「打开」共用一套）。 */
  confirmDiscard: (action: string) => boolean;
}>();

const store = useMindMapStore();

const input = ref<HTMLInputElement | null>(null);
const busy = ref(false);
const error = ref('');
const warnings = ref<ImportWarning[]>([]);
const importedName = ref('');
/** 识别出来的格式名 → 界面上的说法。认不出的原样显示。 */
const FORMAT_LABELS: Record<string, string> = {
  json: 'JSON（无损）',
  markdown: 'Markdown 大纲',
  opml: 'OPML',
  xmind: 'XMind',
  freemind: 'FreeMind',
  puml: 'PlantUML',
};

/** `accept` 只是文件选择框的过滤条件，真正的格式判定在后端（扩展名 + 内容嗅探）。 */
const ACCEPT = '.json,.md,.markdown,.opml,.xmind,.mm,.puml,.plantuml,.txt';

const sourceLabel = computed(
  () => FORMAT_LABELS[importedName.value] ?? importedName.value,
);

/**
 * 导进来的是哪种图（"思维导图" / "鱼骨图" / "流程图"）。
 *
 * 读的是 `store.content` 而不是 `result.content`：两者是同一个对象（`importMap` 原样
 * 装进画布），但前者在 `finally` 之后仍然可用——面板是**异步显示**的，而 `result`
 * 只在 `try` 块里活着。这也顺带证明了一件事：形状信息经过 `slot()` 之后一点没变。
 */
const importedShape = computed(
  () => (store.content ? shapeLabel(kindOf(store.content)) : ''),
);

function pick(): void {
  if (busy.value) return;
  input.value?.click();
}

async function onChange(event: Event): Promise<void> {
  const target = event.target as HTMLInputElement;
  const file = target.files?.[0];
  // 先清空 value：同一个文件连选两次时 change 不会再触发（与导出下拉同一个坑）。
  target.value = '';
  if (!file) return;

  if (!props.confirmDiscard('导入另一个文件')) {
    // 用户取消：不动画布，也不留下上一轮的告警（那说的是另一份文件）。
    return;
  }

  busy.value = true;
  error.value = '';
  warnings.value = [];
  importedName.value = '';
  try {
    const result = await importMindMap(file);
    store.importMap(result.content);
    warnings.value = result.warnings;
    importedName.value = result.source_format;
    // 成功时也把 `store.status` 之外的那句话补上：状态栏在左侧折叠区下面，用户
    // 不一定看得到；这里给一个明确的「已导入」。
    if (result.warnings.length === 0) {
      store.status = `已导入「${result.content.title}」（${sourceLabel.value}，`
        + `${importedShape.value}），还没有保存。`;
    }
  } catch (err) {
    // 后端两种 400 都会把原因写在 `message` 里（"第 12 行第 5 列解析失败" /
    // "这是一个 zip，但里面没有 XMind 的 content.json"）。原样显示，别用一句
    // 笼统的"导入失败"盖掉它——那句话是用户唯一能拿去搜的线索。
    error.value = err instanceof ApiError
      ? `导入失败：${err.message}`
      : '导入失败：请重试，或换一个文件。';
  } finally {
    busy.value = false;
  }
}

function dismissWarnings(): void {
  warnings.value = [];
}

function dismiss(): void {
  if (error.value) {
    error.value = '';
    return;
  }
  dismissWarnings();
}
</script>

<template>
  <div class="relative">
    <button
      type="button"
      data-test="import-button"
      class="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 disabled:opacity-40"
      :disabled="busy"
      title="导入已有的思维导图（JSON / Markdown / OPML / XMind / FreeMind / PlantUML）"
      @click="pick"
    >
      {{ busy ? '读取…' : '导入' }}
    </button>
    <!--
      文件选择框。**teleport 到 body**，不留在组件子树里。
      理由很实际：`SourceInputs` 的上传测试用 `wrapper.get('input[type="file"]')`
      定位它自己的选择框，而 `wrapper.get` 只在组件子树里找。两个 file input 摆
      在一起时，先出现的那个（顶栏这个）会被它抓到，于是"运行期间冻结上传框"
      之类的断言全部错位。把这一颗 teleport 出去，两条入口各自的选择器都不再
      互相干扰——这是一处只有放在一起才会出现的冲突。
    -->
    <Teleport to="body">
      <input
        ref="input"
        type="file"
        data-test="import-file"
        class="hidden"
        :accept="ACCEPT"
        @change="onChange"
      />
    </Teleport>

    <!--
      结果面板。`absolute right-0 top-full` 贴在按钮下方，不挤动顶栏布局。
      `role=alert` 在失败时用：屏幕阅读器会读出来；成功+告警用 `status`。
    -->
    <div
      v-if="error || warnings.length"
      data-test="import-panel"
      class="absolute right-0 top-full z-30 mt-1 w-[420px] max-w-[90vw] rounded-md border bg-white p-2 text-xs shadow-lg"
      :class="error ? 'border-rose-300' : 'border-amber-300'"
      :role="error ? 'alert' : 'status'"
      aria-live="polite"
    >
      <div class="flex items-start justify-between gap-2">
        <p class="min-w-0 break-words leading-relaxed" :class="error ? 'text-rose-700' : 'text-amber-800'">
          <span v-if="error">{{ error }}</span>
          <span v-else>
            已导入「{{ store.content?.title }}」（{{ sourceLabel }}，{{ importedShape }}），
            有 {{ warnings.length }} 条提示：
          </span>
        </p>
        <button
          type="button"
          data-test="import-dismiss"
          class="shrink-0 text-slate-400 hover:text-slate-700"
          @click="dismiss"
        >
          关闭
        </button>
      </div>
      <ul v-if="warnings.length" class="mt-1.5 flex flex-col gap-1">
        <li
          v-for="(warning, index) in warnings"
          :key="`${warning.code}-${index}`"
          data-test="import-warning"
          class="rounded bg-amber-50 px-2 py-1 leading-relaxed text-amber-900"
        >
          {{ warning.message }}
        </li>
      </ul>
      <p v-if="warnings.length" class="mt-1.5 leading-relaxed text-slate-500">
        导入的内容算未保存的修改，点保存才会写进库。
      </p>
    </div>
  </div>
</template>
