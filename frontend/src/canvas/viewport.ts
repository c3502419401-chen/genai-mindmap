import { NODE_SIZE } from './layout/tidy';

/**
 * 画布的**视口算术**：两档视图各自的缩放与偏移。
 *
 * 这个文件存在的原因是一次实测：43 个节点、1600×852 的画布上，旧代码按"整张图塞进
 * 视口"算出 `scale = 0.364`。节点正文是 `text-sm`（14px），乘出来是 **5.1 CSS 像素**：
 * 字变成一片灰，读图的人第一反应不是"图太挤"，而是"这次生成失败了"。
 *
 * 所以默认档改成**可读优先**：缩放下限由字号反推，宁可只给一块能读的，也不要整张
 * 糊成一团。全图档原样保留（`fitAll` 就是旧行为的那条算术），给一个按钮切过去。
 *
 * 判据写成常量并给算式，理由与 `flow.ts::DATABASE_SIZE` 相同：这些数之间有关系
 * （12 / 14 = 0.857），把关系写死在两个地方的后果是改一处、另一处悄悄失效。
 */

/** 节点正文字号：`MindNode` 的 `text-sm` 就是 14px。缩放乘的就是它。 */
export const NODE_FONT_PX = 14;

/** 可读字号下限（CSS 像素）。低于 12px，中文方块字的分辨率掉得很快。 */
export const MIN_READABLE_FONT_PX = 12;

/**
 * 由字号反推的最小缩放：`12 / 14 = 0.857142857…`。
 *
 * 这个数是**推导出来的**，不是拍的：`14 × 0.857 = 12.0`。改字号或改下限，这里跟着变。
 */
export const MIN_READABLE_SCALE = MIN_READABLE_FONT_PX / NODE_FONT_PX;

/** 与 `<VueFlow :min-zoom>` / `:max-zoom` 同一个口径：用户手动缩放仍可越过可读下限。 */
export const MIN_ZOOM = 0.2;
export const MAX_ZOOM = 1.6;

/**
 * 与旧 `fitView({ padding: 0.2 })` 逐像素相同的边距口径（照抄 Vue Flow 的
 * `parsePadding`：`floor((size - size / (1 + padding)) * 0.5)`）。
 *
 * 抄它不是为了省事，是为了"全图"这一档与改动之前**给同一个缩放**：0.2 在两处
 * 各解释一遍，用户切到全图时会看到一个与昨天不同的比例，而他分不清是修好了还是坏了。
 */
export const FIT_PADDING = 0.2;

export type ViewMode = 'fit-width' | 'fit-all';

export interface Point {
  x: number;
  y: number;
}

export interface Size {
  width: number;
  height: number;
}

export interface Bounds extends Point, Size {}

export interface Transform {
  x: number;
  y: number;
  zoom: number;
}

export interface ViewPlan extends Transform {
  /**
   * 这一屏装不下整张图（只给了锚点附近的一块，或横向/纵向超出去）。
   * 界面据此只说"装不下"，不做任何额外动作——平移本来就是画布自带的能力。
   */
  clipped: boolean;
}

/** 画布上一个节点：位置是左上角，尺寸缺省与布局共读 `NODE_SIZE`。 */
export interface NodeBox {
  position: Point;
  data?: { size?: Size } | null;
}

/** 一排节点的外接框；没有节点时返回 `null`（空画布不该算出 (0,0) 这种假框）。 */
export function contentBounds(nodes: readonly NodeBox[]): Bounds | null {
  if (!nodes.length) return null;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const node of nodes) {
    const size = node.data?.size ?? NODE_SIZE;
    minX = Math.min(minX, node.position.x);
    minY = Math.min(minY, node.position.y);
    maxX = Math.max(maxX, node.position.x + size.width);
    maxY = Math.max(maxY, node.position.y + size.height);
  }
  if (!Number.isFinite(minX) || !Number.isFinite(minY)) return null;
  return { x: minX, y: minY, width: maxX - minX, height: maxY - minY };
}

/** 一个方向上的边距（像素）。与 Vue Flow 的 `parsePadding` 同一个公式。 */
export function paddingOf(size: number, padding: number = FIT_PADDING): number {
  return Math.floor((size - size / (1 + padding)) * 0.5);
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), Math.max(low, high));
}

/**
 * 整张图装进视口需要的缩放（**旧行为**：`fitView({ padding: 0.2 })` 的那条算术）。
 *
 * 不夹 `minZoom / maxZoom`：夹取在 `planView` 里统一做，否则"装不下"这个判断会被
 * 夹过的值算错（夹到 1.6 之后图仍然放不下，`clipped` 才是对的）。
 */
export function fitAllScale(bounds: Bounds, viewport: Size): number {
  const padX = paddingOf(viewport.width);
  const padY = paddingOf(viewport.height);
  const availW = Math.max(1, viewport.width - 2 * padX);
  const availH = Math.max(1, viewport.height - 2 * padY);
  if (bounds.width <= 0 || bounds.height <= 0) return MAX_ZOOM;
  return Math.min(availW / bounds.width, availH / bounds.height);
}

export interface PlanInput {
  mode: ViewMode;
  bounds: Bounds;
  viewport: Size;
  /**
   * 装不下的时候，把这一屏对到哪个点上（画布坐标）。传根节点的中心：
   * 三种图型（树 / 鱼骨 / 流程）的根都被布局放在原点附近，它是唯一的"入口"。
   * 缺省（或算不出）时才退回图片中心。
   */
  anchor?: Point | null;
  minZoom?: number;
  maxZoom?: number;
}

/**
 * 这一档视图该用多大的缩放、偏移多少。
 *
 * 两档的差别只有**缩放下限**：
 *
 * - `fit-all`：`min(availW/w, availH/h)`，夹 `[minZoom, maxZoom]`。就是旧行为。
 * - `fit-width`：同一条算术，但下界换成 `MIN_READABLE_SCALE`（0.857），上界
 *   `maxZoom`。于是"图大到装不下"时不再继续缩，而是**装不下**（`clipped`）：
 *   `14 × 0.857 = 12px`，字还能读；超出去的部分靠平移看。
 *
 * **为什么不按字面把宽度拉满**（`availW / bounds.width`，对这张 42 节点的图是 1.5）：
 * 实测过——那样高度只能看到 29%（竖长型的图，宽度 888 早就装得下，缩放该由高度决定），
 * 字从 12px 变成 21px，换来的只有"更少的节点"。字号下限的目的是**别糊**，不是**放大**。
 *
 * 装不下时**按轴分别决定锚点**：装得下的那个方向居中（别把可用空间白白浪费掉——
 * 42 个节点那张图宽只有 888，0.857 倍是 761，1600 的视口横向完全装得下；这时还按
 * 根居中会把整张图推到右边，左半屏空着），装不下的那个方向对到 `anchor`（根）上：
 * 那一条里唯一该先看见的是"这张图在讲什么"。
 */
export function planView(input: PlanInput): ViewPlan {
  const minZoom = input.minZoom ?? (input.mode === 'fit-width' ? MIN_READABLE_SCALE : MIN_ZOOM);
  const maxZoom = input.maxZoom ?? MAX_ZOOM;
  const raw = fitAllScale(input.bounds, input.viewport);
  const zoom = clamp(raw, minZoom, maxZoom);

  const padX = paddingOf(input.viewport.width);
  const padY = paddingOf(input.viewport.height);
  const availW = Math.max(1, input.viewport.width - 2 * padX);
  const availH = Math.max(1, input.viewport.height - 2 * padY);

  const fitsX = input.bounds.width * zoom <= availW + 0.5;
  const fitsY = input.bounds.height * zoom <= availH + 0.5;

  const centerX = input.bounds.x + input.bounds.width / 2;
  const centerY = input.bounds.y + input.bounds.height / 2;
  const focus: Point = {
    x: fitsX || !input.anchor ? centerX : input.anchor.x,
    y: fitsY || !input.anchor ? centerY : input.anchor.y,
  };

  return {
    zoom,
    x: input.viewport.width / 2 - focus.x * zoom,
    y: input.viewport.height / 2 - focus.y * zoom,
    clipped: !fitsX || !fitsY,
  };
}

/**
 * 这一屏实际看到内容的多少（0–1，两个方向各一个）。
 *
 * 只服务界面文案与用例：`planView` 的 `clipped` 是布尔，而"少看了多少"才解释得了
 * 为什么默认档不再把整张图塞进来。
 */
export function coverage(
  bounds: Bounds,
  viewport: Size,
  plan: Transform,
): { width: number; height: number } {
  const visibleW = Math.min(bounds.width, viewport.width / plan.zoom);
  const visibleH = Math.min(bounds.height, viewport.height / plan.zoom);
  return {
    width: bounds.width > 0 ? visibleW / bounds.width : 1,
    height: bounds.height > 0 ? visibleH / bounds.height : 1,
  };
}

/**
 * "展开之后"这个动作的贴边容忍度（CSS 像素）。
 *
 * 24 取的是节点内边距那一档的两倍：贴边的节点看起来像被裁了一半，而它旁边新长出来
 * 的子节点一定在视野外——展开之后要看的就是那几个。
 */
export const KEEP_MARGIN = 24;

export interface KeepInput {
  /** 目标节点的外接框（画布坐标）。 */
  rect: Bounds;
  viewport: Size;
  /** 当前 transform。**缩放要原样保留**，见 `keepTarget` 的说明。 */
  current: Transform;
  margin?: number;
}

/**
 * 展开某一支之后该怎么摆视口：**保持当前缩放，必要时才平移**。
 *
 * 为什么不是 `planView`：那个函数按**整张图**的外接框算缩放。展开一支之后图变大，
 * `fit-all` 就缩到看不清、`fit-width` 又锚回根节点——两种都不是用户点的那个位置。
 * "展开"的语义是**就地长出新节点**：那一支还在原来的像素位置，只是多了几个孩子。
 *
 * 返回 `null` 表示**一个像素都别动**：目标节点完整落在视口里、四周留白都够
 * `margin`。已经是要看的东西了，再摆一次只是制造一次没有意义的动画（而动画期间
 * `getViewport()` 还是旧值，会和下一次读数打架）。
 *
 * 否则按**当前 zoom** 把目标挪到视口正中。正中而不是"刚好露出来"：露一角的节点与
 * "没看见"在用户那里是同一件事，而他要接着看的是它周围新长出来的孩子——中心是
 * 那些孩子都进来的唯一位置。
 */
export function keepTarget(input: KeepInput): Transform | null {
  const margin = input.margin ?? KEEP_MARGIN;
  const { rect, viewport, current } = input;
  // 容器还没量出来，或 transform 还没初始化：什么都不做比搬到一个错的 (0,0) 好。
  if (viewport.width <= 0 || viewport.height <= 0) return null;
  if (!(current.zoom > 0) || !Number.isFinite(current.zoom)) return null;
  const left = rect.x * current.zoom + current.x;
  const top = rect.y * current.zoom + current.y;
  const right = left + rect.width * current.zoom;
  const bottom = top + rect.height * current.zoom;
  if (
    left >= margin && top >= margin
    && right <= viewport.width - margin && bottom <= viewport.height - margin
  ) {
    return null;
  }
  const cx = rect.x + rect.width / 2;
  const cy = rect.y + rect.height / 2;
  return {
    x: viewport.width / 2 - cx * current.zoom,
    y: viewport.height / 2 - cy * current.zoom,
    zoom: current.zoom,
  };
}
