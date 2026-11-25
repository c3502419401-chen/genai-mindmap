<script setup lang="ts">
import { Background } from '@vue-flow/background';
import { VueFlow, useVueFlow } from '@vue-flow/core';
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import { useMindMapStore } from '../stores/mindmap';
import { boxesOfNodes, chooseViewMode, visibleIds } from './clarity';
import { NODE_SIZE } from './layout/tidy';
import {
  MAX_ZOOM,
  MIN_ZOOM,
  contentBounds,
  coverage,
  planView,
  type Point,
  type ViewMode,
} from './viewport';
import FishboneEdge from './FishboneEdge.vue';
import MindEdge from './MindEdge.vue';
import MindNode from './MindNode.vue';
import MindRelation from './MindRelation.vue';

/**
 * 画布是**纯渲染**：节点坐标、连线句柄、鱼骨的骨线折线都由 store 算好
 * （分派在 `canvas/layout/index.ts` 的 `layoutDiagram`，按 `content.diagram_type`）。
 *
 * 分派为什么不在这里：本组件手上没有那棵树，只有扁平的 `nodes` / `edges`；而连线
 * 的句柄是按节点坐标反推的，两处各算一次布局，节点与连线立刻分家。这里唯一按图型
 * 分叉的地方是连线的**渲染器**——`smoothstep`（父子线）/ `mindEdge`（附加关系）/
 * `fishbone`（骨线）。
 */
const store = useMindMapStore();
const {
  setCenter, setViewport, getViewport, dimensions,
  onNodeClick, onNodeDoubleClick, onPaneClick, onMoveEnd, updateNodeInternals,
} = useVueFlow();

/**
 * 画布外壳。结构键（Tab / Enter / Delete / F2）只服务它，所以它必须能拿到焦点。
 *
 * **为什么要有这一层**：这些键原先挂在 `window` 上，只排除了输入框。于是选中一个
 * 节点之后，焦点随便落在哪个按钮上，按 Tab（想切到下一个控件）都会往图里加一个子
 * 节点、按 Enter 会加一个兄弟节点——用户以为自己只是在用键盘导航，图已经被改了。
 * 这不是"少一次校验"，是数据损坏。
 */
const shell = ref<HTMLElement | null>(null);

function focusCanvas(): void {
  // `preventScroll`：画布可能比视口大，聚焦外壳时页面跳一下会让人以为图动了。
  shell.value?.focus({ preventScroll: true });
}

// 预览节点也在同一份 nodes 里，但 store.select 只接受 content 中真实存在的 id：
// 画布这边不必自己判断来源，点不中就是点不中。
//
// 按住 Ctrl / Cmd / Shift 点是**追加选区**：「改这张图」吃的是整个选区（用户圈三个
// 节点说「这几个合并成一个」，模型得知道是哪三个）。不加修饰键的单击仍然是
// "只看这一个"，并把追加选区清掉——那才是用户在画布上随手点一下的预期。
//
// **单击节点本身就是开合**（产品要求："用户点击节点后就可以看到后面还有没有显示的
// 节点，由用户决定是否展开查看"）。它与"点节点开详情"不冲突，因为详情不需要独占这一下：
// 单击照旧 `select`（右侧详情区块换成它、工具条浮出来），**额外**把这一处还能展开的
// 东西放出来 / 把已经摊开的叠回去。两条路共用一个判断（`store.toggleFoldInView` →
// `foldActionOf`），所以角标说能展开、点节点也一定展开。
//
// 为什么只在**无修饰键**时开合：Ctrl 点是在圈选区，圈着圈着让图重排是灾难。
// 为什么叶子不受影响：`toggleFoldInView` 对没有子节点的节点是空操作，所以"点一下读
// 正文"这条最常用的路径上，画布一个像素都不会动。
onNodeClick(({ node, event }) => {
  const additive = event.ctrlKey || event.metaKey || event.shiftKey;
  if (additive) store.toggleSelect(node.id);
  else store.select(node.id);
  if (!additive) store.toggleFoldInView(node.id);
  // 点了节点就把焦点交给画布：接下来按 Tab / Enter 是要改图，不是去别处。
  focusCanvas();
});
onNodeDoubleClick(({ node }) => store.beginEdit(node.id));
onPaneClick(() => {
  store.clearSelection();
  focusCanvas();
});
// 用户手动平移或缩放后停止自动适配，否则每长一个节点视图就会被拽回全图。
onMoveEnd(() => {
  store.viewAdjusted = true;
  // 手动挪过之后"装不装得下"也变了：这句提示跟着真实视口走，不与某次适配绑定。
  updateClippedHint();
});

/** 焦点在输入框里时不能抢键：那边的 Tab、Delete 归输入框自己管。 */
function isTyping(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  return el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable;
}

/**
 * 控件：它们的 Enter 是"点一下"，Tab 是"去下一个"，都不归画布。
 *
 * 节点卡片自己也带 `tabindex="0"`（可聚焦、可读屏），那是**画布内**的焦点，
 * 结构键归画布——所以这里只排除真正的交互控件。
 */
function isControl(el: HTMLElement | null): boolean {
  if (!el) return false;
  const tag = el.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'BUTTON' || tag === 'A') {
    return true;
  }
  const role = el.getAttribute('role');
  return role === 'button' || role === 'link' || role === 'textbox' || role === 'menuitem';
}

/**
 * 这一下按键该不该归画布：焦点必须在画布里，且不落在控件上。
 *
 * 撤销 / 重做（Ctrl+Z）不在这条判据里——那是全局操作，焦点在哪都该生效。
 */
function ownsStructureKeys(event: KeyboardEvent): boolean {
  if (isTyping(event.target)) return false;
  const el = shell.value;
  if (!el) return false;
  const active = document.activeElement;
  if (!active || !el.contains(active)) return false;
  return !isControl(active as HTMLElement);
}

function onKeydown(event: KeyboardEvent) {
  if (isTyping(event.target)) return;

  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
    event.preventDefault();
    if (event.shiftKey) store.redo();
    else store.undo();
    return;
  }

  // 焦点不在画布里就到此为止：Tab 要能继续在表单控件之间走，Enter 要能按下那个
  // 按钮。这一步之前漏了，选中节点后在别处按 Tab / Enter 会改图。
  if (!ownsStructureKeys(event)) return;

  const id = store.selectedId;
  if (!id) return;

  switch (event.key) {
    case 'Tab':
      event.preventDefault();
      store.addChildTo(id);
      break;
    case 'Enter':
      event.preventDefault();
      store.addSiblingTo(id);
      break;
    case 'Delete':
    case 'Backspace':
      event.preventDefault();
      store.deleteNode(id);
      break;
    case 'F2':
      event.preventDefault();
      store.beginEdit(id);
      break;
  }
}

onMounted(() => window.addEventListener('keydown', onKeydown));

/**
 * 视图档位。默认**适应宽度**：缩放下限由字号反推（`viewport.ts::MIN_READABLE_SCALE`），
 * 于是"图大到装不下"时不再继续缩，而是只给一块能读的。
 *
 * 旧行为（整张图塞进视口）留给「全图」这一档。为什么默认值要换：43 个节点 × 1600 宽的
 * 画布实测 `scale = 0.364`，节点正文 14px × 0.364 = **5.1 CSS 像素**——用户看到的是
 * 一片灰，第一反应是"这次生成失败了"，而图其实是好的。
 */
const viewMode = ref<ViewMode>('fit-width');

/**
 * 下一次适配时要不要**重新选档**（`fit-all` 还是 `fit-width`）。
 *
 * 语义只有一句：**用户手动选过档位，就按用户的；没选过，就按可读性自动调**。
 * 所以：
 *   - 换图（`mode` / `mapId` / `generatedSummary` 变）→ 清掉"用户选过"的标记，重判；
 *   - 内容变了（`content` 换引用）→ 没手动选过就重判（图长大了该切到可读档，
 *     这是自动档该做的事），手动选过就不动（那是他明确表达过的偏好）；
 *   - 用户手动平移/缩放（`viewAdjusted`）→ 连适配都不做，见 `scheduleFit` 第一行。
 */
let autoModePending = true;

/** 用户自己点过档位按钮。换图时复位。 */
let userPickedMode = false;

/**
 * 切到「全图」之前那一屏。切回来要回到原位——那可能是用户手动平移、缩放到某一支之后
 * 的位置，回到"重新适配"等于把他刚调好的视角扔掉一次。
 */
let savedViewport: { x: number; y: number; zoom: number } | null = null;

/** 这一屏装不下整张图（只给了根附近的一块）：界面据此说一句"拖动可平移"。 */
const clipped = ref(false);

/**
 * 这一屏**看不见**的节点数。
 *
 * 它是"看不全"那句提示的第二半：只说"只显示了一部分"，用户不知道还剩多少；说
 * "还有 32 个节点在视野外"，他就能判断是"拖一下"还是"切全图"。判据在
 * `clarity.visibleIds`（矩形相交，露出一点就算看得见——见那个函数的注释）。
 */
const offscreen = ref(0);

/** `store.nodes` → 外接框。位置是左上角，尺寸取节点自己的 `data.size`（菱形更大）。 */
function canvasBoxes() {
  return boxesOfNodes(store.nodes);
}

/**
 * 「装不下」这句话怎么说：看**真实的视口**，而不是某一次适配算出来的结论。
 *
 * 为什么不能只看 `planView().clipped`：切回「适应宽度」时是把存下的那一屏原样还回去，
 * 根本没重算过视图；用户手动平移/缩放之后同理。用真实视口算，这句话就永远与屏幕上
 * 看到的一致（`coverage` 也就是"宽度/高度各看得见多少"）。
 */
function updateClippedHint(): void {
  const view = dimensions.value;
  const boxes = canvasBoxes();
  const bounds = contentBounds(store.nodes);
  if (!view?.width || !view?.height || !bounds) {
    clipped.value = false;
    offscreen.value = 0;
    return;
  }
  const plan = getViewport();
  const seen = coverage(bounds, view, plan);
  clipped.value = seen.width < 0.999 || seen.height < 0.999;
  offscreen.value = boxes.length - visibleIds(boxes, view, plan).length;
}

/**
 * 局部聚焦的锚点：根节点的中心。
 *
 * 三种图型（树 / 鱼骨 / 流程）的根都被布局放在原点附近（`tidy` 把根平移到原点，
 * 鱼骨的鱼头固定在 (0,0)，`layoutFlow` 也把根平移到原点），它是唯一的"入口"。
 * 装不下时对到它，用户第一眼看到的是"这张图在讲什么"，而不是中间的某几格。
 */
function rootAnchor(): Point | null {
  const root = store.nodes.find(node => node.data?.depth === 0);
  if (!root) return null;
  const width = root.data?.size?.width ?? NODE_SIZE.width;
  const height = root.data?.size?.height ?? NODE_SIZE.height;
  return { x: root.position.x + width / 2, y: root.position.y + height / 2 };
}

/** 按当前档位（或指定档位）把视图摆好。缩放与偏移的算术全在 `viewport.ts`。 */
function applyView(mode: ViewMode, animate = false): void {
  const view = dimensions.value;
  // 还没量到画布大小（首帧、被折叠）：真算下去只会把视图搬到 (0,0)。
  if (!view?.width || !view?.height) return;
  const bounds = contentBounds(store.nodes);
  if (!bounds) {
    clipped.value = false;
    return;
  }
  const plan = planView({
    mode,
    bounds,
    viewport: view,
    anchor: rootAnchor(),
    // 与 `<VueFlow>` 上那两个属性同源，别在这里写第二份数字。
    minZoom: mode === 'fit-width' ? undefined : MIN_ZOOM,
    maxZoom: MAX_ZOOM,
  });
  clipped.value = plan.clipped;
  // 动画走完之后再按真实视口核一遍（动画期间 `getViewport()` 还是旧的）。
  void setViewport({ x: plan.x, y: plan.y, zoom: plan.zoom }, { duration: animate ? 250 : 0 })
    .then(updateClippedHint);
}

function setView(next: ViewMode): void {
  if (next === viewMode.value) return;
  // 用户表态了：此后内容变化不再自动改档（换图时才复位）。
  userPickedMode = true;
  if (next === 'fit-all') {
    savedViewport = getViewport();
    viewMode.value = 'fit-all';
    applyView('fit-all', true);
  } else {
    viewMode.value = 'fit-width';
    const back = savedViewport;
    savedViewport = null;
    // 有存下来的那一屏就原样还回去（"切回来要回到原位"），没有才重新算。
    if (back) {
      void setViewport(back, { duration: 250 }).then(updateClippedHint);
    } else {
      applyView('fit-width', true);
    }
  }
  // **不碰 `store.viewAdjusted`**：换档位是"我想怎么看"，不是"以后都别再自动适配"。
  // 把它也当成手动调整，会得到一个很别扭的后果——点过「全图」之后再生成，
  // 新图会停留在旧图那一屏上（`scheduleFit` 被这个标志挡住）。
  // `setViewport` 本身不发 `moveEnd`，所以这里不动它，它就保持原样。
}

/**
 * 「全部展开」/「收起到上限」：用户对"这一屏画多少个"的直接表态。
 *
 * 点完立刻重排这一屏，不走 `scheduleFit`：后者被 `viewAdjusted` 挡着（用户平移过
 * 画布就不再自动适配），而这两个按钮是明确要求换一种看法，必须当场生效。
 *
 * 也不能只改 store 就完事：展开/收起会重排**每一个节点**，屏幕上"原来的位置"已经
 * 不存在了，不重新适配就会停在一片空白或半张图上。
 */
function setFolded(all: boolean): void {
  if (all) store.expandAllInView();
  else store.collapseViewToCap();
  // 等 computed 落地（`store.nodes` 换了新坐标）再摆视图。
  void nextTick(() => applyView(viewMode.value, true));
}

/**
 * 「收起到上限」悬停时那句解释。
 *
 * 上限被关掉（`visibleNodeLimit <= 0`，回滚路径）时不能说"只画前 0 个节点"——那时
 * 这个按钮做的其实是"把我手动收起的那几支放回来"。
 */
const capHint = computed(() => (
  store.visibleNodeLimit > 0
    ? `只画前 ${store.visibleNodeLimit} 个节点（数据一个都不动）`
    : '把手动收起的那几支放回来（上限已关闭）'
));

/**
 * 点了一处折叠点（节点本身，或它头上的 `+N`/`−`）：换一处看待它，并把这一屏重新摆好。
 *
 * 与 `setFolded` 同一套理由（重排之后旧的那一屏没意义了），但**只动这一处**：
 * 那是"这一支我要看/我不要看"，不是"以后都按这个数量画"。
 *
 * `+N` 与 `−` 两个角标各走各的 store 入口（见 `MindNode` 的 `toggleFold` / `collapse`）：
 * 点角标是**明确表态**，点节点本体可能只是想选中它。
 */
function toggleFold(id: string): void {
  store.toggleFoldInView(id);
  void nextTick(() => applyView(viewMode.value, true));
}

/** 点「−」：明确把这一支叠起来（预算给的也照收——那是"清爽"这条要求本身）。 */
function collapseFold(id: string): void {
  store.collapseNodeInView(id);
  void nextTick(() => applyView(viewMode.value, true));
}

/**
 * 点一级分支上那枚**概括角标**：把这一支**整支**摊开。
 *
 * 与 `toggleFold`（`+N`）分开的判据只有一条：**角标上的数字是哪一层的数，动作就是
 * 哪一层的动作**。`+N` 写"这一处缺 3 个直接孩子"，所以只放一层；概括角标写"这一支
 * 还有 21 个"，所以放整支。两者共用"点下去冒出来的个数必须与数字相等"这条规矩。
 */
function expandBranch(id: string): void {
  store.expandBranchInView(id);
  void nextTick(() => applyView(viewMode.value, true));
}

/**
 * 换了一张图，之前存下的那一屏就作废了。
 *
 * 不复位的后果：在全图档下打开另一张图，再切回「适应宽度」，视口会被还回到**上一张
 * 图**的某一屏——用户看到的是一片空白，而图其实在别处。
 */
watch(() => store.content, () => {
  savedViewport = null;
});
let fitTimer: ReturnType<typeof setTimeout> | undefined;

/** 图换了：下一次适配先按 `chooseViewMode` 定档，再摆视图。 */
function requestAutoMode(): void {
  autoModePending = true;
  scheduleFit();
}

function scheduleFit() {
  if (store.viewAdjusted) return;
  clearTimeout(fitTimer);
  // 增量节点到达很密，节流一下，避免每帧都重新适配。
  fitTimer = setTimeout(() => {
    if (autoModePending) {
      autoModePending = false;
      const choice = chooseViewMode({ boxes: canvasBoxes(), viewport: dimensions.value });
      if (choice.mode !== viewMode.value) {
        viewMode.value = choice.mode;
        // 自动换档之后，"切回原位"存的那一屏属于另一档，作废。
        savedViewport = null;
      }
    }
    applyView(viewMode.value, true);
  }, 150);
}

watch(() => store.nodeCount, scheduleFit);
watch(() => store.direction, scheduleFit);
// 画布尺寸第一次量出来（以及窗口缩放）时补一次：`fit-view-on-init` 已经关掉，
// 首屏的视图完全由这里给——不补这一枪，默认档位在"进来就有一张图"时不会生效。
watch(() => [dimensions.value.width, dimensions.value.height] as const, requestAutoMode);

// 换了一张图：档位重新由可读性决定（用户上一次的选择属于上一张图）。
watch([() => store.mode, () => store.mapId, () => store.generatedSummary], () => {
  userPickedMode = false;
  requestAutoMode();
});

// 内容换了：用户没手动选过档位时跟着可读性走。
watch(() => store.content, () => {
  if (!userPickedMode) requestAutoMode();
});

// 从引用列表跳过来时把目标挪到视野中心。位置就是布局算出的绝对坐标，
// 中心点 = 左上角 + 半个节点；不需要访问 Vue Flow 内部的节点实例。
// 跳转后关掉自动适配：用户按自己的意志看某一支，下一帧被拉回全图会很难受。
//
// 半个节点取**这个节点自己的**外接框（`data.size`）：判定节点的菱形是 220×110，
// 按 `NODE_SIZE` 加半个会把判定节点跳偏 20/31 像素，而偏移在跳转动画里看不出来。
// 树与鱼骨没有 `data.size`，回退到 `NODE_SIZE`，与改动前逐像素相同。
watch(() => store.focusTick, () => {
  const target = store.nodes.find(node => node.id === store.selectedId);
  if (!target) return;
  store.viewAdjusted = true;
  const width = target.data?.size?.width ?? NODE_SIZE.width;
  const height = target.data?.size?.height ?? NODE_SIZE.height;
  void setCenter(
    target.position.x + width / 2,
    target.position.y + height / 2,
    { duration: 250, zoom: 1 },
  );
});

// 换方向会改掉每个节点的句柄位置（左 / 右 / 上 / 下）。Vue Flow 只在自己重新测量节点时
// 才知道句柄动了，否则连线依旧按旧坐标绘制——表现为线头悬空、或从错误的一侧进出。
// 用"每条连线用了哪两个句柄"做签名：它变了才需要重测，标签更新之类的变化不用。
const handleSignature = computed(() =>
  store.edges.map(edge => `${edge.sourceHandle}>${edge.targetHandle}`).join('|'),
);

/**
 * 节点外接框的签名。
 *
 * 判定节点的菱形比 `NODE_SIZE` 大，句柄的**绝对位置**跟着变（左右句柄在 x 上偏
 * 20 像素、上下句柄在 y 上偏 31 像素）。Vue Flow 的 ResizeObserver 通常能自己发现，
 * 但"测量"与"句柄注册"是两条路，漏一次的表现为"线还挂在旧坐标上"。这里把尺寸也
 * 算进重测的触发条件。
 *
 * 树与鱼骨没有 `data.size`（`stores/mindmap` 只在流程图里发它），签名恒为空串，
 * 这条 watch 对它们一次都不会额外触发。
 */
const sizeSignature = computed(() =>
  store.nodes
    .map(node => `${node.id}:${node.data?.size?.width ?? 0}x${node.data?.size?.height ?? 0}`)
    .join('|'),
);

watch(
  [() => store.direction, () => store.nodeCount, handleSignature, sizeSignature],
  async () => {
    await nextTick();
    updateNodeInternals(store.nodes.map(node => node.id));
  },
  { flush: 'post' },
);

onBeforeUnmount(() => {
  clearTimeout(fitTimer);
  window.removeEventListener('keydown', onKeydown);
});
</script>

<template>
  <!--
    画布要能拿到焦点：`tabindex="0"` 让它进 Tab 序列，也让"点了画布 → 键盘改图"
    这条链成立。焦点环只在键盘走到这里时出现（`focus-visible`），鼠标点画布不长出
    一圈框。
  -->
  <div
    ref="shell"
    tabindex="0"
    aria-label="导图画布（选中节点后 Tab 加子节点、Enter 加兄弟节点、Delete 删除、F2 改名）"
    class="relative h-full w-full outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-sky-300"
  >
    <VueFlow
      :nodes="store.nodes"
      :edges="store.edges"
      :nodes-draggable="false"
      :nodes-connectable="false"
      :elements-selectable="true"
      :pan-on-drag="true"
      :zoom-on-scroll="true"
      :min-zoom="MIN_ZOOM"
      :max-zoom="MAX_ZOOM"
      :fit-view-on-init="false"
      class="h-full w-full"
    >
      <Background :gap="20" pattern-color="#e2e8f0" />
      <template #node-mind="nodeProps">
        <MindNode
          v-bind="nodeProps"
          :editing="store.editingId === nodeProps.id"
          @commit="label => store.commitLabel(nodeProps.id, label)"
          @next="label => store.commitAndAddChild(label)"
          @toggle-fold="toggleFold(nodeProps.id)"
          @collapse="collapseFold(nodeProps.id)"
          @expand-branch="expandBranch(nodeProps.id)"
          @cancel="store.endEdit()"
        />
      </template>
      <!--
        附加边（条件分支 / 汇合 / 回退）。注册成 `edge-mindEdge` 就够——
        `stores/mindmap` 只给附加边挂 `type: 'mindEdge'`，父子线仍然走内置的
        smoothstep。两条路分开是刻意的：父子线是布局的一部分（必须跟着 tidy 的
        句柄走），附加边是叠加语义（要标签、要按 kind 换画法），合成一个组件只会
        让父子线的渲染被附加边的逻辑拖住。
      -->
      <template #edge-mindEdge="edgeProps">
        <MindEdge v-bind="edgeProps" />
      </template>
      <!--
        跨节点关系线（`relations`）。第三种渲染器，理由见 `MindRelation.vue`：
        它的两个端点没有共同父节点，折线会从中间的节点上拐直角。
      -->
      <template #edge-mindRelation="edgeProps">
        <MindRelation v-bind="edgeProps" />
      </template>
      <!--
        鱼骨图的骨线（主骨段 + 60° 斜骨、以及原因那截水平短线）。点的来源是
        `layout/fishbone.ts` 算好的折线，这里只描边——几何不在这层猜。
      -->
      <template #edge-fishbone="edgeProps">
        <FishboneEdge v-bind="edgeProps" />
      </template>
    </VueFlow>

    <!-- 空态用「画布上一个节点都没有」判断，而不是「content 为空」：生成中预览
         会长出节点，这时再压一条占位文字，正好盖住用户想看的生长过程。
         文案要指出**输入框在哪**：这一版它固定在画布底部那一条对话框里。 -->
    <p
      v-if="store.nodeCount === 0 && !store.busy"
      class="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-slate-400"
    >
      还没有导图：在下方对话框写一个主题，或点「+」添加材料。
    </p>
    <p
      v-else-if="store.nodeCount === 0 && store.busy"
      class="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-slate-400"
    >
      正在生成，稍候…
    </p>

    <!--
      视图档位。两档必须同时在场：默认档的可读字号下限意味着**一屏看不全**，
      出路只有一条——让用户知道"还能看全"，以及怎么回去。所以不是一个开关，
      是两个互斥按钮（当前档位亮着），切回去时回到切换之前那一屏（见 `setView`）。
    -->
    <div
      v-if="store.nodeCount > 0"
      class="absolute right-3 top-3 z-20 flex flex-col items-end gap-1"
    >
      <div
        class="flex items-center gap-0.5 rounded-lg border border-slate-200 bg-white/95 p-0.5 text-[11px] shadow-sm"
        role="group"
        aria-label="视图范围"
      >
        <button
          type="button"
          data-test="view-mode-width"
          class="rounded px-2 py-1"
          :class="viewMode === 'fit-width' ? 'bg-slate-900 text-white' : 'text-slate-600 hover:bg-slate-100'"
          :aria-pressed="viewMode === 'fit-width'"
          title="保证节点文字不小于 12 像素；装不下的部分拖动平移查看"
          @click="setView('fit-width')"
        >适应宽度</button>
        <button
          type="button"
          data-test="view-mode-all"
          class="rounded px-2 py-1"
          :class="viewMode === 'fit-all' ? 'bg-slate-900 text-white' : 'text-slate-600 hover:bg-slate-100'"
          :aria-pressed="viewMode === 'fit-all'"
          title="整张图塞进视口（节点多时字会很小）"
          @click="setView('fit-all')"
        >全图</button>
      </div>
      <!--
        **收起状态**：这一行是这个功能的诚实声明，不是可选装饰。

        默认收起之后，画布上可能只有 40 个节点，而顶栏写着「136 个节点」。两个数都对，
        但不在画布上说出"显示 40 / 136"，用户扫一眼就会以为树就这么大——那正是折叠退休
        （`docs/FOLD-RETIRED.md` §1）要躲开的判断失误。所以这行**常驻**（有截断就显示），
        并且带一个「全部展开」的出口：不是"可点开就行"，是"随时能看见总量 + 一键看全"。
      -->
      <div
        v-if="store.hiddenNodeCount > 0"
        data-test="view-fold-state"
        class="flex items-center gap-1 rounded-lg border border-slate-200 bg-white/95 p-0.5 pl-2 text-[11px] shadow-sm"
      >
        <span class="text-slate-600">显示 {{ store.nodes.length }} / {{ store.nodeCount }} 个节点</span>
        <button
          type="button"
          data-test="view-expand-all"
          class="rounded px-2 py-1 text-slate-600 hover:bg-slate-100 hover:text-slate-900"
          title="一个节点都不收起（重新载入这张图会回到上限以内）"
          @click="setFolded(true)"
        >全部展开</button>
        <button
          v-if="store.viewExpandAll || store.viewExpandedNodes.size || store.viewCollapsedNodes.size"
          type="button"
          data-test="view-fold-cap"
          class="rounded px-2 py-1 text-slate-600 hover:bg-slate-100 hover:text-slate-900"
          :title="capHint"
          @click="setFolded(false)"
        >收起到上限</button>
      </div>
      <!--
        全展开／全摊开之后那一档：这一行同样要说明"现在是全部"，并且留一条**一次到位**
        的回程路。没有它，「全部展开」就是一次性的破坏（字小到看不清，还得一个个点回去）。
      -->
      <div
        v-else-if="store.nodeCount > 0 && (store.viewExpandAll || store.viewExpandedNodes.size)"
        data-test="view-fold-state"
        class="flex items-center gap-1 rounded-lg border border-slate-200 bg-white/95 p-0.5 pl-2 text-[11px] shadow-sm"
      >
        <span class="text-slate-600">显示全部 {{ store.nodeCount }} 个节点</span>
        <button
          type="button"
          data-test="view-fold-cap"
          class="rounded px-2 py-1 text-slate-600 hover:bg-slate-100 hover:text-slate-900"
          :title="capHint"
          @click="setFolded(false)"
        >收起到上限</button>
      </div>
      <!-- 装不下时说一句去哪儿看、还剩多少。这一档的代价必须写出来，而不是让用户猜：
           "只显示了一部分"告诉他发生了什么，"还有 N 个节点在视野外"告诉他代价有多大。 -->
      <p
        v-if="clipped && viewMode === 'fit-width'"
        data-test="view-clipped"
        class="pointer-events-none rounded bg-white/80 px-1.5 py-0.5 text-[10px] text-slate-500 shadow-sm"
      >只显示了一部分 · 拖动可平移<template v-if="offscreen > 0">（还有 {{ offscreen }} 个节点在视野外）</template></p>
    </div>
  </div>
</template>
