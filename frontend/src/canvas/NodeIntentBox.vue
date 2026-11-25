<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import { useMindMapStore } from '../stores/mindmap';
import { focusCanvasShell, focusIsHomeless } from './canvas-focus';
import {
  DETAIL_WIDTH,
  INTENT_WIDTH,
  canvasArea,
  canvasWatcher,
  nodeRect,
  planOverlays,
  sizeOf,
} from './overlay-layout';
import { overlayState } from './overlay-state';

/**
 * **点节点就地出输入框**：说一句要改什么，模型执行。
 *
 * ## 它为什么是单独的浮层，而不是"把底部对话框叫回来"
 *
 * 上一版的"点节点让模型改"要两步：选中 → 点浮出工具条上的「改」→ 底部对话框被叫回来
 * 并预填。第二步把用户的眼睛从他的节点上拽到屏幕底部，而那个对话框**在生成完之后是
 * 淡出的**（`ChatDock.shouldKeep`）——它要先被叫回来才看得见。两件事叠起来就是"我想
 * 改这一处，得先去找输入框"。
 *
 * 所以这个框就长在节点旁边：点节点 → 框出现在节点正下方 → 打完这一句按回车 → 走
 * 模型。它**不经过 `ChatDock`**，因此不受它的淡出、收起、材料面板影响。
 *
 * ## 它为什么不能自己发请求
 *
 * 提交走的是 `store.runEditOn()`，也就是**同一个** `runGeneration(intent='edit')`。
 * 撤销栈、`clientRevision`、改动账本、失败分类、生成冲突裁决全部共用那一条；这个组件
 * 只负责"把一句话交出去"。多一条发送管线就要多一套上面那些东西，而那正是这个项目
 * 反复拒绝的重复。
 *
 * ## 与"双击改标题"怎么不抢手势
 *
 * | 手势 | 做什么 | 谁 |
 * | --- | --- | --- |
 * | 单击节点 | 选中 + 开合折叠 + 就地出这个框 | `MindMapCanvas.onNodeClick` |
 * | 双击节点 | 改这一行的**文字**（`label`，本地提交） | `MindNode` 的内联输入框 |
 * | 拖节点 | 改**结构**（父子 / 位次） | `MindMapCanvas` 的拖动链 |
 *
 * 三者分得开靠两条硬约束：
 *
 *  1. **单击到出框之间有一个 120ms 的等待**（在 `MindMapCanvas` 里）。浏览器双击会先
 *     发两次 click 再发 dblclick，不等待的话双击改标题的瞬间会先弹出一个意图框、
 *     再被编辑框顶掉——一次看得见的闪。
 *  2. **这个框永远不自动抢焦点**。点节点会先 `focusCanvas()`（Tab / Enter / Delete
 *     那套结构键依赖焦点在画布外壳里）；框要是自己抢了焦点，用户点完节点想按 Tab
 *     加子节点就会变成在框里插入制表符。所以它出来时不聚焦，**点它一下**才进输入态。
 *
 * 代价写在 `docs/DRAG-ROUND3.md` §一：真正的"一步"意味着把"点节点"从"看"改成"改"，
 * 那会吞掉"点一下读这一支"这条最常用的路，本轮不付这个代价。
 */

const store = useMindMapStore();

const boxRef = ref<HTMLElement | null>(null);
const draft = ref('');
/** 这一句话还在路上（已经交给 store，等 `agentRunning` 起来）。 */
const sending = ref(false);

const visible = computed(() => !!store.intentTargetId && !!store.intentNode);

/** 这个节点叫什么（框顶上那行"改「…」"）。超长时与画布上同一套截断口径。 */
const title = computed(() => {
  const node = store.intentNode;
  if (!node) return '';
  const chars = Array.from(node.label.trim());
  return chars.length > 16 ? `${chars.slice(0, 16).join('')}…` : chars.join('');
});

/** 提示语：这个框吃的是"意图"，不是"标题"。两者混起来用户会在这里打一个新标题。 */
const placeholder = computed(() => `说说「${title.value}」要怎么改…`);

const spot = ref<{ x: number; y: number } | null>(null);

function measure(): void {
  const id = store.intentTargetId;
  if (!id) {
    spot.value = null;
    return;
  }
  const node = nodeRect(id);
  if (!node) {
    // 画布还没画出这个节点（刚落地、还没测量）：保持原位，别把它甩到左上角。
    spot.value = null;
    return;
  }
  // 自己多大要记进共享状态：`planOverlays` 要用它算"放不放得下"，而它也是
  // "意图框躲开工具条与详情卡"这条规则的输入（见 `overlay-state.ts`）。
  const own = boxRef.value?.getBoundingClientRect();
  if (own && own.height > 0) overlayState.intentSize = sizeOf(own);

  const toolbarAnchor = overlayState.toolbarId ? nodeRect(overlayState.toolbarId) : null;
  const detailAnchor = overlayState.detailId ? nodeRect(overlayState.detailId) : null;
  const plan = planOverlays({
    area: canvasArea(),
    intent: { anchor: node, size: overlayState.intentSize },
    toolbar: toolbarAnchor ? { anchor: toolbarAnchor, size: overlayState.toolbarSize } : null,
    detail: detailAnchor
      ? { anchor: detailAnchor, size: { ...overlayState.detailSize, width: DETAIL_WIDTH } }
      : null,
  });
  if (!plan.intent) {
    spot.value = null;
    return;
  }
  spot.value = { x: plan.intent.x, y: plan.intent.y };
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
  () => [store.intentTargetId, store.nodeCount, store.direction, store.focusTick] as const,
  async () => {
    sending.value = false;
    await nextTick();
    watcher.ensure();
    schedule();
  },
  { immediate: true },
);

/**
 * 框第一次真的画出来之后再量一次。
 *
 * 第一次 `measure` 时它还不存在（`v-if` 跟着 `spot` 走，而 `spot` 又要先量它），
 * 量不到自己的宽高，贴边的节点会让它踩着边缘飘出去半个框。`visible` 变真之后补这一枪
 * ——只补一次，不在 `spot` 上循环（那会在 rAF 里自激）。
 */
watch(visible, async on => {
  if (!on) return;
  await nextTick();
  schedule();
});

onMounted(() => watcher.ensure());
onBeforeUnmount(() => {
  if (frame) cancelAnimationFrame(frame);
  watcher.dispose();
  // 本组件是画布那棵树上的常驻组件（不是 `v-if` 出来的），卸载即换页：
  // 把共享状态里那份"我在、我多大"还回去，别留给下一张图。
  overlayState.intentSize = { width: INTENT_WIDTH, height: 0 };
});

function close(): void {
  draft.value = '';
  sending.value = false;
  store.closeIntent();
  // 关掉之后焦点还给画布：用户接下来按 Tab / Enter 是在改图，不是在打字。
  // 只在这个框真的握着焦点时才有意义——`focusIsHomeless()` 会把"用户已经去了别处"
  // 那几种情形放行（理由见 `canvas-focus.ts`）。
  void nextTick(() => {
    if (focusIsHomeless()) focusCanvasShell();
  });
}

async function submit(): Promise<void> {
  const id = store.intentTargetId;
  const text = draft.value.trim();
  if (!id || !text || sending.value) return;
  sending.value = true;
  draft.value = '';
  store.closeIntent();
  await store.runEditOn(id, text);
  sending.value = false;
}

/**
 * Enter 提交、Shift+Enter 换行。
 *
 * 输入法候选框里的回车**不是**提交：中文意图几乎都要经过那一步，而老写法
 * （`@keydown.enter.prevent`）会在组词过程中就把半截话发出去。与 `ChatDock` 的
 * `onIntentKeydown` 同一条判据，两处不许各写一套。
 */
function onKeydown(event: KeyboardEvent): void {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    close();
    return;
  }
  if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.metaKey) return;
  event.preventDefault();
  void submit();
}
</script>

<template>
  <!--
    `Teleport` + `fixed`：与悬停卡片同一条理由（见 `MindNode.vue` 的 `cardAt`）。
    画布整块挂在 `transform: scale()` 上，框留在节点内部就会跟着一起缩——默认档
    0.857 时 13px 的字还能读，切到「全图」0.364 就是 4.7px，那时用户点开的是一个
    看不清的框。这个框要接键盘，它的字号不能由图有多挤决定。
  -->
  <Teleport to="body">
    <div
      v-if="visible && spot"
      ref="boxRef"
      data-test="node-intent"
      role="dialog"
      aria-label="让模型改这个节点"
      class="fixed z-40 w-60 rounded-lg border border-slate-300 bg-white p-1.5 shadow-lg"
      :style="{ left: `${spot.x}px`, top: `${spot.y}px` }"
    >
      <div class="flex items-center gap-1 px-0.5 pb-1">
        <span class="min-w-0 flex-1 truncate text-[11px] font-medium text-slate-600">
          改「{{ title }}」
        </span>
        <button
          type="button"
          data-test="node-intent-close"
          class="shrink-0 rounded px-1 text-[11px] leading-none text-slate-400 hover:bg-slate-100 hover:text-slate-700"
          aria-label="关闭"
          title="关闭（Esc）"
          @click="close"
        >✕</button>
      </div>
      <textarea
        v-model="draft"
        rows="1"
        data-test="node-intent-input"
        aria-label="要模型怎么改这个节点"
        class="block w-full resize-none rounded border border-slate-300 px-1.5 py-1 text-xs leading-snug outline-none focus:border-slate-500 disabled:opacity-50"
        :placeholder="placeholder"
        :disabled="!store.canEditIntent"
        @keydown="onKeydown"
      />
      <div class="mt-1 flex items-center gap-1 px-0.5">
        <span class="min-w-0 flex-1 truncate text-[10px] leading-tight text-slate-400">
          Enter 提交 · Shift+Enter 换行
        </span>
        <button
          type="button"
          data-test="node-intent-submit"
          class="shrink-0 rounded bg-slate-900 px-2 py-0.5 text-[11px] text-white disabled:opacity-30"
          :disabled="!draft.trim() || !store.canEditIntent"
          title="让模型改这一处"
          @click="submit"
        >{{ sending ? '已提交' : '改' }}</button>
      </div>
    </div>
  </Teleport>
</template>
