<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import { NODE_COLORS, NODE_COLOR_LABELS } from '../mindmap/edits';
import { useMindMapStore } from '../stores/mindmap';
import {
  DETAIL_WIDTH,
  canvasArea,
  canvasWatcher,
  nodeRect,
  planOverlays,
  sizeOf,
} from '../canvas/overlay-layout';
import { overlayState } from '../canvas/overlay-state';

/**
 * 选中节点旁边浮出的小工具条：`[改][展开][删][色]`。
 *
 * **为什么是浮层而不是第二块面板**：这一版把常驻区域全撤了（左栏撤掉、右栏改覆盖式
 * 抽屉、对话框生成完就淡出），"选中一个节点"于是需要一个**就地**的入口——用户此刻
 * 的眼睛在节点上，不在屏幕边缘。
 *
 * **位置不由这里决定**：它和详情卡都在 `overlay-layout.ts::planOverlays` 里一起摆。
 * 原先它贴在节点上方，而详情卡也是从节点上方弹出来的——实测「明确目标分数」那一支，
 * 工具条正好压住正文；贴到画布上沿时还会被 clamp 回节点里，把节点自己糊住。
 * 现在候选顺序是**侧面 → 下方 → 上方**，并且躲开详情卡已经占下的那块矩形。
 */
const store = useMindMapStore();

/**
 * `expand` 交回 `App.vue`，与「改」同一条路。
 *
 * 工具条自己**不**调 `store.expandSelected()`：那条函数收一个 `sources` 参数，而
 * 工具条手里没有材料——只有 `useSourcesStore()` 里的当前那一份。两个组件各自读一次
 * 材料列表，就会出现"工具条传的是旧快照、App 传的是新快照"这种两条真相。
 * `App.vue` 的 `expandBranch` 是带材料的唯一入口，工具条只发一个意图。
 */
const emit = defineEmits<{ edit: [label: string]; expand: [] }>();

const anchor = ref<{ x: number; y: number } | null>(null);
const paletteOpen = ref(false);
/** 工具条自己，用来量它有多大——躲开详情卡要靠这个尺寸。 */
const barRef = ref<HTMLElement | null>(null);

const selected = computed(() => store.selectedNode);
const visible = computed(() => !!selected.value && !!anchor.value);

/** 这个节点是不是某块区域的根（中心主题 / 一根骨 / 一个阶段）——那种不能删。 */
const deleteDisabled = computed(
  () => (selected.value ? store.isContainerRootId(selected.value.node.id) : true),
);

function measure(): void {
  const id = selected.value?.node.id;
  if (!id) {
    anchor.value = null;
    return;
  }
  const node = nodeRect(id);
  if (!node) {
    // 画布还没画出这个节点（刚落地、还没测量）：保持原位，别把它甩到左上角。
    anchor.value = null;
    return;
  }
  // 自己的尺寸先记下来：详情卡要用它来给工具条让路（"一处算"的另一半输入）。
  const bar = barRef.value?.getBoundingClientRect();
  if (bar) overlayState.toolbarSize = sizeOf(bar);

  const hovering = overlayState.detailId;
  const detailNode = hovering ? nodeRect(hovering) : null;

  const plan = planOverlays({
    area: canvasArea(),
    toolbar: { anchor: node, size: overlayState.toolbarSize },
    detail: detailNode
      ? { anchor: detailNode, size: { ...overlayState.detailSize, width: DETAIL_WIDTH } }
      : null,
  });
  if (!plan.toolbar) {
    anchor.value = null;
    return;
  }
  anchor.value = { x: plan.toolbar.x, y: plan.toolbar.y };
}

let frame = 0;
function schedule(): void {
  if (frame) return;
  frame = requestAnimationFrame(() => {
    frame = 0;
    measure();
  });
}

const watcher = canvasWatcher(schedule);

watch(
  () => [selected.value?.node.id, store.nodeCount, store.direction, store.focusTick] as const,
  async () => {
    paletteOpen.value = false;
    overlayState.toolbarId = selected.value?.node.id ?? null;
    await nextTick();
    watcher.ensure();
    schedule();
  },
  { immediate: true },
);

/** 详情卡出现/消失、换尺寸时也要重摆：它是本组件"让路"的对象。 */
watch(
  () => [overlayState.detailId, overlayState.detailSize.height] as const,
  async () => {
    await nextTick();
    schedule();
  },
);

/**
 * 工具条第一次真的画出来之后再量一次。
 *
 * 第一次 `measure` 时它还不存在（`v-if` 跟着 `anchor` 走），量不到自己的宽高，
 * 贴边的节点就会踩着边缘飘出去半个工具条。`visible` 变真之后补这一枪——只补一次，
 * 而且不是在 `anchor` 上循环（那会在 rAF 里自激）。
 */
watch(visible, async on => {
  if (!on) return;
  await nextTick();
  schedule();
});

onMounted(() => {
  watcher.ensure();
});

onBeforeUnmount(() => {
  if (frame) cancelAnimationFrame(frame);
  overlayState.toolbarId = null;
  watcher.dispose();
});

function edit(): void {
  const label = selected.value?.node.label.trim();
  if (!label) return;
  emit('edit', label);
}

function expand(): void {
  // 不调 `store.expandSelected()`：那条路不带材料（见 `emit` 的注释）。
  emit('expand');
}

function remove(): void {
  const id = selected.value?.node.id;
  if (!id) return;
  store.deleteNode(id);
}

function setColor(value: string): void {
  const id = selected.value?.node.id;
  if (!id) return;
  store.setNodeVisualById(id, { color: value ? (value as (typeof NODE_COLORS)[number]) : null });
  paletteOpen.value = false;
}
</script>

<template>
  <div
    v-if="visible && anchor"
    ref="barRef"
    data-test="node-toolbar"
    class="fixed z-40 flex flex-col items-center gap-1"
    :style="{ left: `${anchor.x}px`, top: `${anchor.y}px` }"
    role="toolbar"
    aria-label="节点操作"
  >
    <div class="flex items-center gap-0.5 rounded-lg border border-slate-200 bg-white p-0.5 shadow-lg">
      <button
        type="button"
        data-test="node-action-edit"
        class="rounded px-2 py-1 text-[11px] text-slate-700 hover:bg-slate-100"
        title="让 AI 改这个节点（对话框会重新出现并预填这一句）"
        @click="edit"
      >改</button>
      <button
        type="button"
        data-test="node-action-expand"
        class="rounded px-2 py-1 text-[11px] text-slate-700 hover:bg-slate-100 disabled:opacity-40"
        :disabled="store.agentRunning || selected?.node.expandable === 'no'"
        title="展开这一支，补 3 个子节点"
        @click="expand"
      >展开</button>
      <button
        type="button"
        data-test="node-action-delete"
        class="rounded px-2 py-1 text-[11px] text-slate-700 hover:bg-slate-100 disabled:opacity-40"
        :disabled="deleteDisabled"
        title="删除这个节点"
        @click="remove"
      >删</button>
      <button
        type="button"
        data-test="node-action-color"
        class="rounded px-2 py-1 text-[11px] text-slate-700 hover:bg-slate-100"
        title="换颜色"
        :aria-expanded="paletteOpen"
        @click="paletteOpen = !paletteOpen"
      >色</button>
    </div>

    <div
      v-if="paletteOpen"
      data-test="node-palette"
      class="flex items-center gap-1 rounded-lg border border-slate-200 bg-white p-1 shadow-lg"
    >
      <button
        type="button"
        class="h-4 w-4 rounded-full border border-slate-300 bg-white text-[9px] leading-none text-slate-500"
        title="默认（继承上级）"
        @click="setColor('')"
      >–</button>
      <button
        v-for="key in NODE_COLORS"
        :key="key"
        type="button"
        class="h-4 w-4 rounded-full border border-slate-300"
        :style="{ background: `var(--mind-color-${key}, #cbd5e1)` }"
        :title="NODE_COLOR_LABELS[key]"
        :aria-label="NODE_COLOR_LABELS[key]"
        @click="setColor(key)"
      />
    </div>
  </div>
</template>
