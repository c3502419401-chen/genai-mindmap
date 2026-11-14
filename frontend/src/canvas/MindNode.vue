<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core';
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';

import type { ChildSide } from '../canvas/layout/tidy';
import type { MindNodeData } from '../stores/mindmap';

const props = defineProps<{
  id: string;
  data: MindNodeData;
  selected?: boolean;
  editing?: boolean;
}>();

const emit = defineEmits<{
  commit: [label: string];
  cancel: [];
  /** Tab：提交当前文字，并请外面在同一个父节点下再加一个子节点。 */
  next: [label: string];
}>();

const POSITIONS: Record<ChildSide, Position> = {
  left: Position.Left,
  right: Position.Right,
  top: Position.Top,
  bottom: Position.Bottom,
};

/** 文字被截断时才有必要浮现卡片；没截断也照样允许悬停查看（标题本身可能就是全文）。 */
const truncated = computed(() => props.data.label !== props.data.fullLabel);

/**
 * 卡片里是否还有节点上看不到的内容。
 *
 * 两种来源：标题被截断（旧图的长标题），或正文（`detail`）与标题不是同一段文字。
 * 生成侧的标题现在 ≤16 字，画布上不会被截断——那时这张卡片的全部价值就在于
 * "点开看长文"，所以 `truncated` 这一条不能单独当作提示的判据。
 */
const hasMore = computed(
  () => truncated.value || props.data.body !== props.data.fullLabel,
);

/**
 * 节点框的样式。
 *
 * 三条真实分支原样保留——`content` 渲染出来必须和加预览之前逐像素一致。
 * 未应用的树单独走一组草稿样式：
 * - 候选：实线 + 略降不透明度，看得出「已经长完了」，但仍明显不是画布的一部分；
 * - 预览：虚线 + 更淡，一眼就是还在长；
 * - 预览里模型明确标了「不用再展开」的节点再淡一档。`null`（还没写到）不标记，
 *   否则「明确不展开」和「还没决定」在画面上会变成同一件事。
 */
const boxClass = computed(() => {
  if (props.data.draft === 'candidate') {
    return 'border-slate-400 bg-white text-slate-600 opacity-80';
  }
  if (props.data.draft === 'preview') {
    return props.data.previewLeaf
      ? 'border-dashed border-slate-200 bg-white text-slate-400 opacity-40'
      : 'border-dashed border-slate-300 bg-white text-slate-600 opacity-60';
  }
  if (props.selected) return 'border-slate-900 bg-white font-medium shadow-md';
  if (props.data.depth === 0) return 'border-slate-900 bg-slate-900 font-medium text-white';
  return 'border-slate-300 bg-white text-slate-700 hover:border-slate-400';
});

const card = ref(false);
let timer: ReturnType<typeof setTimeout> | undefined;

function scheduleCard() {
  clearTimeout(timer);
  // 200ms 延迟：鼠标扫过画布时不该到处弹卡片。
  timer = setTimeout(() => {
    card.value = true;
  }, 200);
}

function hideCard() {
  clearTimeout(timer);
  card.value = false;
}

const draft = ref('');
const input = ref<HTMLInputElement | null>(null);
/** 防止"回车提交"之后紧跟的 blur 再提交一次（第二次会带着旧值覆盖结果）。 */
let settled = false;

/**
 * 用户继续打字就把锁解开。
 *
 * 提交不一定成功：store 那边会拒绝空标题和超长标题，那时编辑态仍然开着。
 * 如果锁一直不放，用户改成合法文字后按回车、Tab、Esc 都毫无反应，看起来
 * 像编辑器坏了。input 事件说明内容又变了，自然应该允许再提交一次。
 */
function onDraftInput() {
  settled = false;
}

/**
 * 新加的节点是刚插进画布的，下一帧才真正挂到 document 上；对还没挂上的元素调 focus()
 * 是空操作，用户就得再点一下才能打字。所以每帧试一次，直到焦点确实落进来。
 */
async function focusInput() {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await nextTick();
    const el = input.value;
    if (el && el.isConnected) {
      el.focus();
      el.select();
      if (document.activeElement === el) return;
    }
    await new Promise(resolve => requestAnimationFrame(() => resolve(null)));
  }
}

watch(
  () => props.editing,
  async editing => {
    if (!editing) {
      settled = false;
      return;
    }
    draft.value = props.data.fullLabel;
    settled = false;
    await focusInput();
  },
  { immediate: true },
);

function finish(commit: boolean) {
  if (settled) return;
  settled = true;
  if (commit) emit('commit', draft.value);
  else emit('cancel');
}

/** 和回车不同：Tab 提交完后还要接着加下一个兄弟，不能只把编辑框关掉。 */
function finishAndNext() {
  if (settled) return;
  settled = true;
  emit('next', draft.value);
}

onBeforeUnmount(() => clearTimeout(timer));
</script>

<template>
  <div
    class="relative"
    @mouseenter="scheduleCard"
    @mouseleave="hideCard"
    @focusin="scheduleCard"
    @focusout="hideCard"
  >
    <Handle
      v-for="side in data.in"
      :id="`in-${side}`"
      :key="`in-${side}`"
      type="target"
      :position="POSITIONS[side]"
      class="!h-1.5 !w-1.5 !border-0 !bg-slate-300"
    />
    <Handle
      v-for="side in data.out"
      :id="`out-${side}`"
      :key="`out-${side}`"
      type="source"
      :position="POSITIONS[side]"
      class="!h-1.5 !w-1.5 !border-0 !bg-slate-300"
    />

    <div
      class="flex h-12 w-[180px] items-center justify-center rounded-md border px-3 text-center text-sm leading-tight"
      :class="boxClass"
      :title="data.fullLabel"
      tabindex="0"
    >
      <input
        v-if="editing"
        ref="input"
        v-model="draft"
        class="w-full bg-transparent text-center outline-none"
        :maxlength="200"
        @keydown.enter.prevent="finish(true)"
        @keydown.esc.prevent="finish(false)"
        @keydown.tab.prevent="finishAndNext"
        @input="onDraftInput"
        @blur="finish(true)"
      />
      <span v-else class="line-clamp-2 break-words">{{ data.label }}</span>
    </div>

    <!--
      引用角标：只给"有引用"的节点画，没引用的节点保持干净。
      用 pointer-events-none 让它不挡节点点击（角标压在节点边缘上）。
    -->
    <div
      v-if="(data.refs ?? 0) > 0"
      class="pointer-events-none absolute -right-1.5 -top-1.5 flex h-5 min-w-5 items-center justify-center rounded-full border border-slate-300 bg-white px-1 text-[10px] font-medium leading-none text-slate-600 shadow-sm"
      :title="`${data.refs ?? 0} 处来源引用`"
    >
      {{ data.refs ?? 0 }}
    </div>

    <div
      v-if="card && !editing"
      class="absolute bottom-full left-1/2 z-30 mb-2 w-72 -translate-x-1/2 rounded-md border border-slate-300 bg-white p-3 text-left shadow-lg"
    >
      <p class="max-h-56 overflow-y-auto whitespace-pre-wrap break-words text-sm text-slate-700">
        {{ data.body }}
      </p>
      <p v-if="hasMore" class="mt-2 text-xs text-slate-400">
        <span v-if="data.bodySource === 'detail'">节点上只显示标题，这里是生成时写的详情</span>
        <span v-else-if="data.bodySource === 'note'">节点上只显示标题，这里是你的备注</span>
        <span v-else>节点上只显示前 24 个字，完整内容见此处或右侧详情面板</span>
      </p>
    </div>
  </div>
</template>
