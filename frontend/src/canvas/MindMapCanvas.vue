<script setup lang="ts">
import { Background } from '@vue-flow/background';
import { VueFlow, useVueFlow } from '@vue-flow/core';
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import { useMindMapStore, type MovePreview } from '../stores/mindmap';
import { boxesOfNodes, chooseViewMode, visibleIds } from './clarity';
import { CANVAS_SHELL_ID, focusCanvasShell } from './canvas-focus';
import { INTENT_DELAY_MS, openIntentAllowed } from './intent-gesture';
import {
  dropHitOf,
  dropZoneOf,
  outwardOf,
  type DropSide,
} from './layout/drop-zone';
import { insertIntentAt } from './layout/insert-zone';
import { NODE_SIZE } from './layout/tidy';
import {
  detachPendingLabel,
  dropPendingLabel,
  type DropZone,
  type InsertIntent,
} from '../mindmap/drop-plan';
import {
  MAX_ZOOM,
  MIN_ZOOM,
  contentBounds,
  coverage,
  keepTarget,
  planView,
  type Point,
  type ViewMode,
} from './viewport';
import FishboneEdge from './FishboneEdge.vue';
import MindEdge from './MindEdge.vue';
import MindNode from './MindNode.vue';
import MindRelation from './MindRelation.vue';
import NodeIntentBox from './NodeIntentBox.vue';

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
  setCenter, setViewport, getViewport, dimensions, viewport,
  onNodeClick, onNodeDoubleClick, onPaneClick, onMoveEnd, updateNodeInternals,
  onNodeDragStart, onNodeDrag, onNodeDragStop,
  screenToFlowCoordinate, findNode, updateNode,
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
  // 走共用的入口而不是 `shell.value?.focus()`：`MindNode` 与 `NodeIntentBox` 卸载后
  // 也要把焦点还到这里，而它们拿不到这个 ref（理由见 `canvas-focus.ts`）。
  focusCanvasShell();
}

/**
 * 拖动的方位换算用**这份布局自己报的朝向**，不是 `theme.direction`。
 *
 * 树型下两者恒等（`layoutTidy` 原样返回传进去的 `direction`），所以这一句对树一个
 * 字节都没改。但流程图与鱼骨图的几何是定死的：`layoutFlow` 报 `down`（dagre TB）、
 * `layoutFishbone` 报 `right`（主骨横贯）——它们的"上下左右"与用户给树选的那个朝向
 * 毫无关系。拿 `store.direction` 翻译会得到整张反的表：在鱼骨上把原因拖到左边
 * （画面上那是**更深**的方向，因为子永远在父左侧），会被判成"我当它的父"。
 */
const zoneDirection = computed(() => store.layout?.direction ?? store.direction);

/**
 * 单击节点之后**晚一点**再把就地输入框亮出来。
 *
 * 这个延迟不是手感调优，是手势仲裁：浏览器的一次双击会先发两次 `click` 再发
 * `dblclick`。用户双击一个节点是要**改它的标题**（`MindNode` 的内联输入框），
 * 而单击是要**说意图**。判据与理由都在 `intent-gesture.ts`（纯函数，四条竞态各有
 * 一条用例）；这里只负责那一个定时器与"到点了再问一次"。
 */
let intentTimer: ReturnType<typeof setTimeout> | undefined;

function cancelIntentSoon(): void {
  if (intentTimer) clearTimeout(intentTimer);
  intentTimer = undefined;
}

function openIntentSoon(id: string): void {
  cancelIntentSoon();
  intentTimer = setTimeout(() => {
    intentTimer = undefined;
    const allowed = openIntentAllowed(id, {
      editingId: store.editingId,
      selectedId: store.selectedId,
      exists: store.nodes.some(node => node.id === id),
      canEdit: store.canEditIntent,
    });
    if (allowed) store.openIntentFor(id);
  }, INTENT_DELAY_MS);
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
  // 松手后紧跟着的那一下 click 是拖动的一部分（拖完手还在节点上），不是"点开这一处"。
  // 不拦的话，每次拖完都会顺带把落点的折叠状态翻一次——用户看到的是"拖了一下，
  // 目标节点突然冒出/收起了一批子节点"。
  if (performance.now() - dragEndedAt < 200) return;
  const additive = event.ctrlKey || event.metaKey || event.shiftKey;
  if (additive) store.toggleSelect(node.id);
  else store.select(node.id);
  if (!additive) store.toggleFoldInView(node.id);
  // 点了节点就把焦点交给画布：接下来按 Tab / Enter 是要改图，不是去别处。
  focusCanvas();
  // 圈选区（Ctrl / Cmd / Shift 点）时不弹意图框：那一下说的是"把它也加进来"，
  // 而弹出的框会立刻把焦点和视线都抢到"改这一处"上去。
  if (!additive) openIntentSoon(node.id);
});
onNodeDoubleClick(({ node }) => {
  // 双击是**改这一行的文字**（`MindNode` 的内联输入框），意图框让位。
  // 浏览器双击会先发两次 click 再发 dblclick，所以这里必须把那次待开的框掐掉，
  // 否则用户会看到"双击 → 先弹出一个框、再被编辑框顶掉"的一次闪。
  cancelIntentSoon();
  store.closeIntent();
  store.beginEdit(node.id);
});
onPaneClick(() => {
  cancelIntentSoon();
  store.closeIntent();
  store.clearSelection();
  focusCanvas();
});

/**
 * 双击**空白**：在那个位置加一个节点。
 *
 * 为什么这条手势值得做（用户点名要的三种长节点方式里的第二种）：键盘那条（Tab /
 * Enter）只能沿着"当前选中的那个节点"长，而用户常常是先看到**这一片**还空着、
 * 想在那儿放一个。位置是手势里唯一有信息量的东西，所以它必须参与落点的推导。
 *
 * 位置参与的方式只有一种合法解：**挂到哪个节点下面、排第几**。坐标的唯一来源仍然是
 * 布局（`docs/NODE-DRAG.md` §一），所以"在那儿"读作"离那儿最近的那个节点下面"。
 *
 * 手势分工写在 DOM 上：`.vue-flow__node` 上的双击归 `onNodeDoubleClick`（改名），
 * 这一层只认**不在节点里的**那些双击。判据不用坐标而用 DOM——用户看到的"节点"就是
 * 那个元素，用外接框判会在缩放 / 判定节点（菱形更大）上错开。
 */
function insertIntentFor(pointer: { x: number; y: number }): InsertIntent | null {
  // 合成根（鱼头 / 流程起点）不在 `content` 里，选中它、挂到它下面都是非法操作。
  // `selectableNodes` 已经把它们摘掉了，交叉一下 `nodes`（那里才有坐标）。
  const real = new Set(store.selectableNodes.map(entry => entry.node.id));
  const boxes = store.nodes
    .filter(node => real.has(node.id))
    .map(node => ({ id: node.id, rect: nodeRectOf(node) }));
  return insertIntentAt(pointer, boxes, {
    direction: zoneDirection.value,
    rootId: store.nodes.find(node => node.data?.depth === 0)?.id ?? null,
  });
}

function onShellDoubleClick(event: MouseEvent): void {
  const target = event.target as HTMLElement | null;
  // 节点上的双击归节点自己（改名）。
  if (target?.closest('.vue-flow__node')) return;
  // 悬浮控件（视图档位、折叠开关）上的双击是用户连点了两下按钮。
  if (isControl(target)) return;
  // 空白处双击是**加一个节点**。它同样会让那次待开的意图框作废：双击的第二下
  // 说明用户要的是"在这里放一个"，不是"改那一处"。
  cancelIntentSoon();
  store.closeIntent();
  const intent = insertIntentFor(screenToFlowCoordinate({ x: event.clientX, y: event.clientY }));
  if (!intent) return;
  event.preventDefault();
  store.addNodeAt(intent);
  focusCanvas();
}
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

/**
 * **把某个节点留在视野里**：保持当前缩放，只平移，必要时才动。算术在
 * `viewport.keepTarget`（那是纯函数，用例直接钉它）。
 *
 * 为什么不是 `applyView`：那个函数按**整张图**的外接框算缩放，展开一支之后图变大，
 * `fit-all` 就缩到看不清，`fit-width` 又锚回根节点——两种档位都不是用户点的那个
 * 位置。而"展开"这个动作的预期是**就地长出新节点**：那一支还在原来的像素位置，
 * 只是多了几个孩子。同类产品（XMind / 幕布）的默认也是"把新长出来的部分带进视野，
 * 缩放不动"。
 *
 * `animate` 只在"真的动了"时才生效；`false` 用在一批节点刚落地那一帧（内容已经换
 * 了，动画会让它看起来像"被推走"）。
 */
function keepAround(id: string, animate = true): void {
  const view = dimensions.value;
  if (!view?.width || !view?.height) return;
  const target = store.nodes.find(node => node.id === id);
  if (!target) return;
  const next = keepTarget({
    rect: nodeRectOf(target),
    viewport: view,
    current: getViewport(),
  });
  // `null` = 已经在视野里（且留白够）：一个像素都不动，只顺手核对一下"看不全"那句。
  if (!next) {
    updateClippedHint();
    return;
  }
  void setViewport(next, { duration: animate ? 250 : 0 }).then(updateClippedHint);
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
  const action = store.toggleFoldInView(id);
  // 展开：把这一处留在视野里（同 `expandBranch` 的理由，不重算全图）。
  // 收起：整张图变小了，这时不重新铺满会留下一屏空处——那是另一个方向的动作，
  // 用户要的就是"少看一点、看清一点"，所以照旧走适配。
  if (action === 'expand') void nextTick(() => keepAround(id));
  else if (action === 'collapse') void nextTick(() => applyView(viewMode.value, true));
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
  // **不重算整张图的适配**：`applyView` 按全图外接框算缩放并锚回根节点，于是
  // "点开这一支"的结果是那一支被缩到看不清、屏幕中央换成主节点——用户报的
  // 「每次展开都会回到主节点」就是这一步。这里只把这个节点留在视野里（缩放不动），
  // 新长出来的子节点贴着它出现，正是点开时想看的东西。
  void nextTick(() => keepAround(id));
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

/**
 * 档位重铺。`store.viewAdjusted` 是这里唯一的闸：用户手动调过视口就不再自动适配。
 *
 * 它保护的是**用户正在看的这一屏**。所以闸的正确性全在"这一屏属于当前这张图吗"：
 * 换图（`loadMap` / `import` / 生成落地）时必须由那一侧先把这个标志清掉——否则新树
 * 会停在旧图的缩放与偏移上（验收记录 §四.3：40 个已渲染节点只有 14 个在视口内）。
 * 复位点写在 `stores/mindmap.ts::applyRunResult`（`create` 落地）与三处换图入口；
 * 这一层不做"换图就无视用户调整"的特例，那样窗口缩放与节点增长也会跟着重置。
 */
function scheduleFit() {
  if (store.viewAdjusted) return;
  clearTimeout(fitTimer);
  // 增量节点到达很密，节流一下，避免每帧都重新适配。
  fitTimer = setTimeout(() => {
    // **正在展开某一支**：跟着那个节点走，不换档、不缩全图。
    //
    // 这条路的来由是 `nodeCount` 变了（模型把子节点一条条推到画布上）。`create`
    // 时整棵树都在长，按全图适配是对的；`expand` 时用户点的是一处，屏幕上该发生
    // 的是"那几个新节点在它旁边长出来"，而不是每长一个就把全图重缩一遍。
    if (store.expanding && store.selectedId) {
      autoModePending = false;
      keepAround(store.selectedId);
      return;
    }
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
// 拖动改结构造成的那一次除外——理由见 `contentChangedByDrag`。
watch(() => store.content, () => {
  if (contentChangedByDrag) {
    contentChangedByDrag = false;
    return;
  }
  // 一次 `edit` / `expand` 落地：**视口留在被改的那一处**，不重新适配。
  //
  // 这是缺陷一那条报道的另一半：点"展开这一支"时节点是一个一个流进来的
  // （`scheduleFit` 那条路管着），而真正落地那一刻 `content` 换了一个新引用，
  // 于是这里会再触发一次全图适配——用户看到的是"等了十几秒，图突然缩成一张全图，
  // 中央是根节点"。锚点由 store 在落地时写（`viewAnchor`），这里消费一次就清掉。
  const anchor = store.viewAnchor;
  if (anchor) {
    store.viewAnchor = null;
    // 等布局 computed 把新坐标交给 Vue Flow 再摆：此时读到的 `store.nodes`
    // 才是**新树**的位置，否则会把旧位置的那一屏算出来。
    void nextTick(() => keepAround(anchor, false));
    return;
  }
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

/**
 * 拖动改结构：**手工位置从不落盘，落点是布局给的**。
 * 布局是自动算的（`layoutDiagram` 从 content 推所有坐标），拖动是手动的，两者只能
 * 有一个赢。这一轮选布局，理由三条：
 *
 *  1. 坐标的唯一来源不能有两个。`Node.pos` 至今没有渲染层的读者，给它开一个读点
 *     意味着 `bounds` / `fitView` / 可读性选档 / 收起计数全都要分叉出"手动"这一
 *     支——而那不是用户要的东西（他要的是改关系，位置只是手势的载体）；
 *  2. 用户的原话本身就是**结构**语义（"假如要成为一个节点的父或子"）。他用手势
 *     说的是"我要挂到这儿"，不是"请把这块画布按我的手摆放"；
 *  3. 在 `tidy` 这套几何下，"拖的方向"与"布局给的落点方向"**同构**：拖到右边是
 *     子（子本来就排在右边）、拖到左上是父（父本来就排在左边一层）、拖到上下是
 *     兄弟（兄弟本来就上下排）。于是重排之后节点是**朝手势的方向吸附过去**的，
 *     而不是弹回原点。这一条是这个决定成立的前提，没有它就只剩"拖了个寂寞"。
 *
 * 剩下的观感问题（"松手前不知道会变成什么"）由 `dragOverlay` 那块解决：松手之前
 * 就画出**松手之后的真实位置**（`store.previewMove` 用同一个 `layoutTidy`、同一棵
 * **画布树**算的），于是"弹回"读作"吸附"。
 *
 * 幽灵框能不能守住这个承诺，取决于 `dragOverlay` 里那两个细节（都在那个 computed
 * 的注释里）：用 `viewport` 而不是 `getViewport()`（拖动时 autoPan 会推着视口走），
 * 以及自己算 `flow * zoom + viewport` 而不是用 `flowToScreenCoordinate`。
 */
interface DragHint {
  /**
   * 这一拖现在指向哪一种结果。
   *
   * `move` = 指针落在某个节点的命中区里（`content` 换掉一个父子关系）；
   * `detach` = 指针**不在任何命中区里**，也就是"拖到空白处松手"（把节点从父节点下面
   * 断开）。两者共用同一套预演与同一块指示层，但结论完全不同，所以种类要显式写出来
   * ——从 `targetId` 是否为空反推会在"目标节点刚好在预览里缺席"那一刻判错。
   */
  kind: 'move' | 'detach';
  /** `move` 时是落点目标；`detach` 时是**被拖的节点自己**。 */
  targetId: string;
  /** 只有 `move` 有。 */
  zone?: DropZone;
  side?: DropSide;
  preview: MovePreview;
  /** 光标位置（页面坐标）。跟随光标的那句标签用它，与落点无关。 */
  pointer: { x: number; y: number };
}

const dragHint = ref<DragHint | null>(null);

/**
 * 这一次内容变化是**拖动**引起的：不重新适配视图。
 *
 * 拖动之后必然整支重排，而"重排完就重新适配"会把用户刚调好的视角扔掉一次——他
 * 刚松手，眼睛还停在原地。落点紧挨着他拖到的那个目标（那是他看得见的东西），
 * 所以不重排视图也不会跑到视野外。
 */
let contentChangedByDrag = false;

/** 拖动刚结束的那一下：抑制紧随其后的 click（否则会顺手开合一次折叠）。 */
let dragEndedAt = 0;

/** 鼠标 / 触摸事件的页面坐标。Vue Flow 两种都发（`MouseTouchEvent`）。 */
function pointerOf(event: MouseEvent | TouchEvent): { x: number; y: number } {
  const touch = 'touches' in event
    ? (event.touches[0] ?? event.changedTouches[0])
    : null;
  if (touch) return { x: touch.clientX, y: touch.clientY };
  return { x: (event as MouseEvent).clientX, y: (event as MouseEvent).clientY };
}

/**
 * 节点在画布上的矩形（flow 坐标）。
 *
 * 树与鱼骨没有 `data.size`（`stores/mindmap` 只在流程图里发它），回退到 `NODE_SIZE`
 * ——与布局共读同一份常量，所以这里算出来的框与画出来的框逐像素相同。
 */
function nodeRectOf(node: { position: { x: number; y: number }; data?: { size?: { width: number; height: number } } }): {
  x: number; y: number; width: number; height: number;
} {
  const size = node.data?.size ?? NODE_SIZE;
  return { x: node.position.x, y: node.position.y, width: size.width, height: size.height };
}

/**
 * 指针底下是哪个节点、落在它的哪一侧。
 *
 * 遍历全部节点而不是问 Vue Flow 的命中 API：`store.nodes` 里的坐标是**布局坐标**，
 * 而被拖的那个节点此刻在屏幕上位于手拖的位置——用 Vue Flow 自己的相交判定会把
 * "指针在它自己身上"也算一次命中，于是它自己成了目标。排除它，是这里唯一需要的
 * 判断。
 */
function findDropTarget(
  pointer: { x: number; y: number },
  draggedId: string,
): { id: string; side: DropSide; nearness: number } | null {
  let best: { id: string; side: DropSide; nearness: number } | null = null;
  for (const node of store.nodes) {
    if (node.id === draggedId) continue;
    const hit = dropHitOf(pointer, nodeRectOf(node));
    if (!hit) continue;
    // 两个命中区重叠时取更贴近中心的那一个（见 `dropHitOf` 的 `nearness`）。
    if (!best || hit.nearness < best.nearness) {
      best = { id: node.id, side: hit.side, nearness: hit.nearness };
    }
  }
  return best;
}

/** 双向布局里目标在中心主题的哪一侧；其余三种朝向不读它（恒为 0）。 */
function outwardFor(targetId: string): 1 | -1 | 0 {
  // 与 `zoneDirection` 同一份朝向：`both` 只可能是树自己的朝向，鱼骨（`right`）
  // 与流程（`down`）永远走不进这一支。
  if (zoneDirection.value !== 'both') return 0;
  const root = store.nodes.find(node => node.data?.depth === 0);
  const target = store.nodes.find(node => node.id === targetId);
  if (!root || !target) return 0;
  const rootRect = nodeRectOf(root);
  const targetRect = nodeRectOf(target);
  return outwardOf(
    targetRect.x + targetRect.width / 2,
    rootRect.x + rootRect.width / 2,
  );
}

onNodeDragStart(() => {
  dragHint.value = null;
  // 拖着节点走的时候，那个输入框得让开：它贴在节点下方，而拖动会让节点从它底下
  // 穿过去。更要紧的是它此刻没有意义——用户在做的是结构编辑，不是写一句话。
  cancelIntentSoon();
  store.closeIntent();
});

/**
 * 指针底下是哪个节点、落在它的哪一侧（或一个都没有 = 空白处）。
 *
 * 三档结论，都要在**松手之前**就变成一句看得懂的话，所以这里连"没命中"也是正结果：
 *
 *  - 命中区里 → `move`：把这次拖动翻译成父子 / 兄弟关系；
 *  - 一个都没命中 → `detach`：断开。这是第二轮加的，"拖到空白处"以前是一个什么都不
 *    发生的死手势（`docs/NODE-DRAG.md` §七 把它划在了这一轮）；
 *  - `detach` 自己也可能是非法（它已经是一级分支），那时 `preview.ok=false`，指示层
 *    画红框 + 一句原因，**松手也不改内容**——与 `move` 的非法处理是同一条路。
 */
onNodeDrag(({ node, event }) => {
  const pointer = pointerOf(event);
  const flow = screenToFlowCoordinate(pointer);
  const hit = findDropTarget(flow, node.id);

  if (!hit) {
    const previous = dragHint.value;
    // 上一次已经是"同一个节点在断开"：只挪那句跟随标签。`previewDetach` 要排一次版，
    // 每帧重算是白烧（与下面 `move` 那档同一个理由）。
    if (previous && previous.kind === 'detach' && previous.targetId === node.id) {
      dragHint.value = { ...previous, pointer };
      return;
    }
    dragHint.value = {
      kind: 'detach',
      targetId: node.id,
      pointer,
      preview: store.previewDetach(node.id),
    };
    return;
  }

  const zone = dropZoneOf(hit.side, {
    direction: zoneDirection.value,
    outward: outwardFor(hit.id),
  });
  const previous = dragHint.value;
  if (previous && previous.kind === 'move' && previous.targetId === hit.id && previous.zone === zone) {
    // 目标与方向都没变：只挪那句跟随标签。**不重算预演**——`previewMove` 要构造
    // 一棵新树、排一次版，每帧重算是白烧，而结果一个像素都不会变。
    dragHint.value = { ...previous, pointer };
    return;
  }
  dragHint.value = {
    kind: 'move',
    targetId: hit.id,
    zone,
    side: hit.side,
    pointer,
    preview: store.previewMove({ draggedId: node.id, targetId: hit.id, zone }),
  };
});

/**
 * 把被拖节点放回**布局给的位置**。
 *
 * 松手的两种结局都要走这里，理由不同：
 *  - 落点合法：`content` 换了 → `store.nodes` 重算 → Vue Flow 收到新的 `:nodes`
 *    就该归位。这里写一遍是**兜底**（props 同步与 Vue Flow 内部拖动状态的先后顺序
 *    不由我们定），而且写的是同一个值，幂等。
 *  - 落点非法：`content` 没动 → `:nodes` 不变 → Vue Flow 内部那个节点还停在手拖的
 *    位置上。不写这一遍，用户会看到一个**飘在半空**、连线脱开的节点，而数据里什么
 *    都没变——看起来就像拖动坏了。
 */
function settleDraggedNode(id: string): void {
  const wanted = store.nodes.find(node => node.id === id);
  const live = findNode(id);
  if (!wanted || !live) return;
  // 亚像素差不算"没归位"：缩放会让同一份坐标在两处带上浮点尾数。
  if (Math.abs(live.position.x - wanted.position.x) < 0.5
    && Math.abs(live.position.y - wanted.position.y) < 0.5) return;
  updateNode(id, { position: { x: wanted.position.x, y: wanted.position.y } });
}

onNodeDragStop(({ node }) => {
  const hint = dragHint.value;
  dragHint.value = null;
  dragEndedAt = performance.now();

  // `preview.ok` 只有**结构**含义（落点允不允许），不含"几何算不算得出"。
  // 它曾经混着两件事：预演拿不到坐标就报 `ok:false`，于是这里直接 return、
  // 内容不变、`status` 也不写——用户看到的是"拖了一下，什么都没发生"。
  if (!hint || !hint.preview.ok) {
    settleDraggedNode(node.id);
    return;
  }

  if (hint.kind === 'detach') {
    // **不设 `contentChangedByDrag`**，这是与 `move` 唯一一处刻意的不对称。
    //
    // 普通拖动的落点紧挨着用户看得见的目标，所以松手之后不重新适配也不会跑出视野。
    // 断开的落点是**根的最后一位**——它可能在画布另一头，离原位一整屏。那时不重新
    // 适配，用户看到的就是"拖到空白处，节点不见了"。
    const result = store.detachNode(node.id);
    void nextTick(() => settleDraggedNode(node.id));
    if (!result.ok) return;
    return;
  }

  contentChangedByDrag = true;
  const result = store.moveNodeTo({
    draggedId: node.id,
    targetId: hint.targetId,
    zone: hint.zone!,
  });
  // `store.nodes` 是 computed，要等下一个 tick 才把新坐标交给 Vue Flow。归位放在
  // 那之后；此时它已经指向**新结构**给的位置，所以这一步就是"吸附"的最后一帧。
  void nextTick(() => settleDraggedNode(node.id));
  // 内容层拒掉了（环、深度）时 `content` 并没变，那个"跳过适配"的标记必须还回去，
  // 否则下一次真正的内容变化会少适配一次视图。
  if (!result.ok) contentChangedByDrag = false;
});

/**
 * 指示层要的那几个坐标。
 *
 * 全部换算到**外层 shell 的坐标系**里。这里刻意不用 Vue Flow 的
 * `flowToScreenCoordinate`：它的实现是 `(point + 容器偏移) * zoom + translate`，
 * 把容器偏移也乘了一次 zoom——容器的 y 是 48（顶栏高度），zoom 是 1.2 时它给出的
 * 位置比真实位置偏 9.7px "少一格"，而这个偏差随 zoom 变化（缩放一次，幽灵框就和
 * 落点错开一点）。视口变换的定义是 `translate → scale`，所以正确的换算是
 * `flow * zoom + viewport`，一行写完，没有任何需要猜的地方。
 *
 * 幽灵框的位置来自 `previewMove().rect`——那是 `layoutTidy` 在**假设的新树**上算
 * 出来的真位置，所以它画在哪里，松手之后节点就落在哪里。
 */
const dragOverlay = computed(() => {
  const hint = dragHint.value;
  // **先判 `hint` 再读 DOM**：`getBoundingClientRect()` 会强制一次 layout，而这个
  // computed 挂在模板的 `v-if` 上，每次重渲染都会求值。不拖动时它必须**零开销**
  // ——顺序颠倒的那一版会让"136 个节点、点几次折叠"这段路多出上百次强制重排，
  // 实测把一条 3 秒的画布用例推过了 5 秒超时。
  if (!hint) return null;
  const origin = shell.value?.getBoundingClientRect();
  if (!origin) return null;

  /**
   * 光圈画在**那条会被动的关系的另一端**上。
   *
   *  - `move`：目标节点——"这次要挂到它身上"；
   *  - `detach`：**原来的父节点**——"要断的是它俩之间那条边"。空白处没有任何视觉
   *    锚点说明会断哪条边，这一圈就是这个功能的可读性本身：没有它，"拖到空白处"
   *    与"拖了一下没对准"在松手之前长得一模一样，而两者的结果一个是改结构、一个是
   *    什么都不发生。
   *
   * 父节点可能已经被视图收起（它不在 `store.nodes` 里）——那时不画光圈，只留幽灵框
   * 与那句话；这是"看不见的关系就不去指它"，而不是画错位置。
   */
  const ringId = hint.kind === 'detach' ? hint.preview.detachedFrom : hint.targetId;
  const ring = ringId ? store.nodes.find(node => node.id === ringId) : undefined;

  /**
   * **必须读响应式的 `viewport`，不能读 `getViewport()`。**
   *
   * `getViewport()` 是一次性的取值，不会让这个 computed 与视口建立依赖。而拖动时
   * `autoPanOnNodeDrag` 会把画布慢慢推着走（拖到画布边缘就会发生），视口一变，
   * 同一个 flow 坐标对应的屏幕位置就变了——但 `dragHint` 只在目标/方向变化时才换
   * 对象，于是 computed 不重算，幽灵框停在**旧视口**算出来的位置上。
   *
   * 实测表现：拖到画布上方边缘时 autoPan 让内容下移 48px，幽灵框比真实落点高 48px。
   * 幽灵框与落点错开，恰恰是这一层要避免的那件事。
   */
  const view = viewport.value;

  const toScreen = (point: { x: number; y: number }) => ({
    left: `${point.x * view.zoom + view.x}px`,
    top: `${point.y * view.zoom + view.y}px`,
  });

  const ringRect = ring ? nodeRectOf(ring) : null;
  const ghost = hint.preview.rect;

  const labelOf = (id: string | undefined): string => {
    if (!id) return '节点';
    return store.nodes.find(node => node.id === id)?.data?.label ?? '节点';
  };

  return {
    ok: hint.preview.ok,
    target: ringRect
      ? {
        ...toScreen({ x: ringRect.x, y: ringRect.y }),
        width: `${ringRect.width * view.zoom}px`,
        height: `${ringRect.height * view.zoom}px`,
      }
      : null,
    ghost: ghost
      ? {
        ...toScreen({ x: ghost.x, y: ghost.y }),
        width: `${ghost.width * view.zoom}px`,
        height: `${ghost.height * view.zoom}px`,
      }
      : null,
    label: {
      left: `${hint.pointer.x - origin.x}px`,
      top: `${hint.pointer.y - origin.y}px`,
      // 光标靠右边缘时把标签翻到左边：那句话是这一层唯一能读的文字，被裁掉一半
      // 就等于没有。用 `transform` 而不是两个 tailwind 类叠加——后者会共用同一个
      // `--tw-translate-x` 变量，两句话只能活一句。
      transform: hint.pointer.x - origin.x > origin.width - 220
        ? 'translate(calc(-100% - 12px), -50%)'
        : 'translate(12px, -50%)',
    },
    text: hint.preview.ok
      ? (hint.kind === 'detach'
        ? detachPendingLabel(labelOf(hint.targetId), labelOf(hint.preview.detachedFrom))
        : dropPendingLabel(labelOf(hint.targetId), hint.zone!))
      : hint.preview.reason ?? '这里放不下',
  };
});

onBeforeUnmount(() => {
  clearTimeout(fitTimer);
  cancelIntentSoon();
  window.removeEventListener('keydown', onKeydown);
});
</script>

<template>
  <!--
    画布要能拿到焦点：`tabindex="0"` 让它进 Tab 序列，也让"点了画布 → 键盘改图"
    这条链成立。焦点环只在键盘走到这里时出现（`focus-visible`），鼠标点画布不长出
    一圈框。

    `id` 是**键盘模型的一部分**，不是给测试用的钩子：三个组件（本画布、节点内联
    编辑、就地输入框）在卸载后都要把焦点还给这里，而它们互相拿不到 ref。id 的常量
    定义在 `canvas-focus.ts::CANVAS_SHELL_ID`，这里是它唯一的落地点。
  -->
  <div
    :id="CANVAS_SHELL_ID"
    ref="shell"
    tabindex="0"
    aria-label="导图画布（选中节点后 Tab 加子节点、Enter 加兄弟节点、Delete 删除、F2 改名；双击空白处加节点）"
    class="relative h-full w-full outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-sky-300"
    @dblclick="onShellDoubleClick"
  >
    <VueFlow
      :nodes="store.nodes"
      :edges="store.edges"
      :nodes-draggable="true"
      :nodes-connectable="false"
      :elements-selectable="true"
      :pan-on-drag="true"
      :zoom-on-scroll="true"
      :zoom-on-double-click="false"
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
          @open-refs="store.openRefs(nodeProps.id)"
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

    <!--
      拖动落点指示。**这是"拖动改结构"能被看懂的唯一原因**，不是可选装饰：
      松手之后节点落在布局给的位置（见 `dragHint` 那段的三条理由），没有这一层，
      用户看到的就是"拖过去、弹回来"。它画的幽灵框位置取自 `store.previewMove`
      ——与真正落地时同一个 `layoutTidy`、同一棵假设树，所以"画在哪儿就落在哪儿"。

      三层信息，缺一层都会有一类错解：
        · 目标节点的光圈   → "这次要动的是**这一条**关系"（画布上同时有上百个节点）
        · 幽灵框           → "它会落在这里"。位置本身就是答案，用户不需要记住
                            "左父右子"这条约定
        · 跟随光标的一句话 → 用**关系**措辞（"成为 A 的子节点"）而不是方向措辞；
                            非法时这里换成原因，于是"禁止"发生在松手之前，而不是
                            松手之后弹一句"操作失败"
    -->
    <div
      v-if="dragOverlay"
      data-test="drag-overlay"
      class="pointer-events-none absolute inset-0 z-30"
    >
      <div
        v-if="dragOverlay.target"
        data-test="drag-target"
        class="absolute rounded-lg ring-4"
        :class="dragOverlay.ok ? 'ring-sky-400/35' : 'ring-rose-400/35'"
        :style="dragOverlay.target"
      />
      <div
        v-if="dragOverlay.ghost"
        data-test="drag-ghost"
        class="absolute rounded-lg border-2 border-dashed"
        :class="dragOverlay.ok ? 'border-sky-500 bg-sky-400/15' : 'border-rose-400 bg-rose-300/10'"
        :style="dragOverlay.ghost"
      />
      <div
        data-test="drag-label"
        class="absolute whitespace-nowrap rounded px-2 py-1 text-[11px] font-medium text-white shadow"
        :class="dragOverlay.ok ? 'bg-sky-600' : 'bg-rose-600'"
        :style="dragOverlay.label"
      >{{ dragOverlay.text }}</div>
    </div>

    <!--
      就地输入框：点节点 → 它出现在节点正下方 → 写一句话按回车 → 模型改这一处。
      它自己 `Teleport` 到 `body`（不跟着画布缩放，理由见组件头），所以放在模板的
      哪一层都不影响渲染位置；留在这里是因为**它的生命周期归画布**——点节点/点空白/
      双击/开始拖动都由本组件的四个回调关掉它，放到 `App.vue` 反而要把这套开关再抄
      一遍。
    -->
    <NodeIntentBox />

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
