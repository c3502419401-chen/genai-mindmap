import type { MindNode } from '../api/types';
import type { DiagramLayout } from './layout';
import { NODE_SIZE } from './layout/tidy';
import {
  MAX_ZOOM,
  MIN_ZOOM,
  MIN_READABLE_SCALE,
  NODE_FONT_PX,
  contentBounds,
  fitAllScale,
  planView,
  type Bounds,
  type NodeBox,
  type Size,
  type ViewMode,
} from './viewport';

/**
 * 「这张图看着清不清楚」的**可复现判据**，以及"该用哪一档视图"的确定性选择。
 *
 * 这个文件不画任何东西，也不改任何一个坐标：它只读 `layoutDiagram` 算出来的位置，
 * 回答两个问题——
 *
 *   1. 现在的几何在**什么情况下**不清不楚（重叠 / 越界 / 超长边 / 图太宽太深）。
 *   2. 生成完之后该给用户哪一档视图（`fit-all` 还是 `fit-width`），以及代价是多少。
 *
 * 为什么两件事放在一个文件里：它们的判据是**同一个算术**（`fitAllScale`）。分成两处
 * 写，一边改了阈值另一边不知道，于是"测出来没问题"和"自动切到了看不清"会同时成立。
 *
 * ## 阈值全部写在下面，并且给了推导
 *
 * 判据能复现的前提是"数字有出处"。凡是没出处的数，这里一个都不留：可读比例来自
 * `viewport.ts`（`12 / 14`），重叠是严格相交（相切不算），层距是**从布局结果反推**的
 * 而不是抄常量——抄常量会在几何改动之后悄悄失效，而反推的层距永远与眼前这张图一致。
 */

/** 画布上一个节点的外接矩形（左上角 + 尺寸）。 */
export interface Box {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * 布局结果 → 节点外接框。
 *
 * `positions` 是**中心点**（`LayoutResult` 的约定），所以这里减半宽半高。尺寸取
 * `layout.sizes`（只有流程图填它：判定节点是 220×110），缺省回落 `NODE_SIZE`——
 * 与 `viewport.ts::contentBounds` 同一条口径，两处必须一致，否则"画布边界"与
 * "重叠判定"会用两套尺寸。
 */
export function boxesOf(layout: DiagramLayout): Box[] {
  const boxes: Box[] = [];
  for (const [id, point] of layout.positions) {
    const size = layout.sizes?.get(id) ?? NODE_SIZE;
    boxes.push({
      id,
      x: point.x - size.width / 2,
      y: point.y - size.height / 2,
      width: size.width,
      height: size.height,
    });
  }
  return boxes;
}

/**
 * 画布上的节点（`store.nodes`，位置是左上角）→ 外接框。
 *
 * 与 `boxesOf` 是同一件事的两个入口：那个吃布局结果（测量用），这个吃渲染层已经摆好
 * 的节点（画布用）。两处共用 `NODE_SIZE` 与 `data.size` 口径——画布上判定节点的菱形
 * 比 `NODE_SIZE` 大，用统一尺寸算"看不看得见"会把它算窄。
 */
export function boxesOfNodes(nodes: readonly (NodeBox & { id?: string })[]): Box[] {
  return nodes.map((node, index) => {
    const size = node.data?.size ?? NODE_SIZE;
    return {
      // 画布上的节点有 id（Vue Flow 的节点），测量时用的布局结果没有——那时用下标
      // 兜底，因为 `visibleIds` 只数个数，不需要 id 唯一。
      id: node.id ?? String(index),
      x: node.position.x,
      y: node.position.y,
      width: size.width,
      height: size.height,
    };
  });
}

/** 严格相交的节点对。**相切不算**（`<` 而不是 `<=`）：布局把两个节点排到边界刚好
 * 相接是正常的紧凑排布，把它算成重叠会让指标永远不为零，那这个数就没有意义了。
 *
 * 复杂度 O(n²)。用它的是测量脚本与小图判据（n 最多几百，一次几十万次比较）；
 * 画布运行时不调它。
 */
export function overlappingPairs(boxes: readonly Box[]): Array<[string, string]> {
  const pairs: Array<[string, string]> = [];
  for (let i = 0; i < boxes.length; i += 1) {
    const a = boxes[i];
    for (let j = i + 1; j < boxes.length; j += 1) {
      const b = boxes[j];
      if (
        a.x < b.x + b.width && b.x < a.x + a.width
        && a.y < b.y + b.height && b.y < a.y + a.height
      ) {
        pairs.push([a.id, b.id]);
      }
    }
  }
  return pairs;
}

/** 节点 id → 父节点 id（根没有条目）。层的宽度与父子距离都靠它。 */
export function parentIndex(root: MindNode): Map<string, string> {
  const parents = new Map<string, string>();
  const walk = (node: MindNode): void => {
    for (const child of node.children) {
      parents.set(child.id, node.id);
      walk(child);
    }
  };
  walk(root);
  return parents;
}

/** 节点 id → 内容层号（根 = 1）。与 `optimization/service.py` 的"最多 8 层"同一口径。 */
export function depthIndex(root: MindNode): Map<string, number> {
  const depths = new Map<string, number>();
  const walk = (node: MindNode, depth: number): void => {
    depths.set(node.id, depth);
    for (const child of node.children) walk(child, depth + 1);
  };
  walk(root, 1);
  return depths;
}

/** 每条**一级分支**直接挂着几个子节点（⑤ 分支宽度分布）。 */
export function branchWidths(root: MindNode): number[] {
  return root.children.map(child => child.children.length);
}

export interface ClarityMetrics {
  nodes: number;
  depth: number;
  /** ① 严格相交的节点对数。 */
  overlapPairs: number;
  /** 相交的节点 id（去重、按遍历顺序），给报告定位用。 */
  overlapIds: string[];
  /** ② 在给定视口的默认一屏里**完全看不见**的节点数。 */
  offscreen: number;
  /** ③ 父子中心在**层级轴**上的投影距离超过 `2 × nominalGap` 的边数。 */
  longEdges: number;
  /** 反推出来的层距（层级轴上的固定间距）。没有父子边、或反推不出正数时为 `null`。 */
  nominalGap: number | null;
  /** 层级轴：树与鱼骨的层沿 x 推进，dagre 的分层沿 y。 */
  axis: 'x' | 'y';
  /** ④ 同一内容层上最多的节点数（"一层几十个"）。 */
  widestLayer: number;
  /** 各内容层的节点数（按层号顺序）。 */
  layers: number[];
  /** ⑤ 每条一级分支的子节点数。 */
  branchWidths: number[];
  bounds: Bounds;
  /** `fit-all` 那一档的缩放（未夹取的原始值也算进来，见 `rawFitAllZoom`）。 */
  fitAllZoom: number;
  /** 夹到 `[MIN_ZOOM, MAX_ZOOM]` 之后的 `fit-all` 缩放——用户真会看到的那个值。 */
  clampedFitAllZoom: number;
  /** `fitAllZoom / MIN_READABLE_SCALE`。< 1 就是"看不清"。 */
  readableRatio: number;
  /** `fit-all` 时字号是否仍在可读下限之上。 */
  readableAtFitAll: boolean;
  /** `fit-width` 那一档实际会停在哪（夹取之后）。 */
  fitWidthZoom: number;
}

export interface MeasureInput {
  root: MindNode;
  layout: DiagramLayout;
  viewport: Size;
}

/**
 * 五个指标 + 两档缩放，一次算完。
 *
 * ② 越界用的是 **`fit-all` 那一档**的一屏：它问的是"默认把整张图塞进来之后，有没有
 * 节点被挤到视口之外"（夹到 `MIN_ZOOM = 0.2` 时会发生——超大图连缩到 0.2 都装不下）。
 * 用 `fit-width` 会更严格，但那已经是另一个问题（见 `chooseViewMode` 的 `offscreen`）。
 */
export function measureLayout(input: MeasureInput): ClarityMetrics {
  const { root, layout, viewport } = input;
  const boxes = boxesOf(layout);
  const bounds = contentBounds(boxes.map(box => ({ position: { x: box.x, y: box.y }, data: { size: { width: box.width, height: box.height } } })))
    ?? { x: 0, y: 0, width: 0, height: 0 };

  const pairs = overlappingPairs(boxes);
  const overlapIds = [...new Set(pairs.flat())];

  const parents = parentIndex(root);
  const depths = depthIndex(root);
  const centers = new Map<string, { x: number; y: number }>();
  for (const box of boxes) {
    centers.set(box.id, { x: box.x + box.width / 2, y: box.y + box.height / 2 });
  }

  // ③ 层距反推 + 超长边。
  //
  // **为什么按主轴投影而不是欧氏距离**：父子中心距离里混着兄弟方向的偏移。实测
  // 一棵很普通的树（根下两个分支），最近的一对父子距离是 238.44 而不是层距 236——
  // 因为子节点被居中摆开，没有一个的 y 与父节点相同。用欧氏距离，"层距"这个数就
  // 变成"最近的那一对碰巧离多近"，而它该说的是**布局固定下来的层间距**。
  //
  // 主轴怎么定（不抄任何常量）：分别取两个方向上父子位移的**最小值**，大的那个
  // 是层级轴。tidy 的 x 位移恒为 236（层距），兄弟方向的 y 位移可以很小甚至为 0
  // （居中时）；dagre 反过来（y 恒为 120，x 会因为居中取到 0）。取 max 恰好把两种
  // 图型都判对，而它读的是布局结果本身——几何改了，这个数跟着改。
  let minDx: number | null = null;
  let minDy: number | null = null;
  const offsetX: number[] = [];
  const offsetY: number[] = [];
  for (const [childId, parentId] of parents) {
    const a = centers.get(parentId);
    const b = centers.get(childId);
    if (!a || !b) continue;
    const dx = Math.abs(b.x - a.x);
    const dy = Math.abs(b.y - a.y);
    offsetX.push(dx);
    offsetY.push(dy);
    minDx = minDx === null ? dx : Math.min(minDx, dx);
    minDy = minDy === null ? dy : Math.min(minDy, dy);
  }
  const axis: 'x' | 'y' = (minDx ?? 0) >= (minDy ?? 0) ? 'x' : 'y';
  const nominalGap = (axis === 'x' ? minDx : minDy) ?? null;
  const offsets = axis === 'x' ? offsetX : offsetY;
  // 层距反推不成立（=0：所有父子都在居中位置上重合到一维）时**不报超长边**：
  // 那时任何阈值都是 0，会把每一条边都算成超长，而那个结论只说明阈值没定义好。
  const longLimit = nominalGap === null || nominalGap <= 0 ? null : nominalGap * 2;
  const longEdges = longLimit === null ? 0 : offsets.filter(distance => distance > longLimit).length;

  // ④ 层宽。按内容层数，而不是按几何坐标聚类：几何上"同一列"在鱼骨/流程里没有
  // 意义（骨是斜的、dagre 的层是横排的），而"这一层有几十个节点"是任何图型下都
  // 成立的同一件事。
  const layerCounts = new Map<number, number>();
  for (const depth of depths.values()) {
    layerCounts.set(depth, (layerCounts.get(depth) ?? 0) + 1);
  }
  const depthMax = Math.max(...depths.values(), 0);
  const layers = Array.from({ length: depthMax }, (_, index) => layerCounts.get(index + 1) ?? 0);

  // ② 越界：`fit-all` 那一档的一屏。
  const fitAllPlan = planView({ mode: 'fit-all', bounds, viewport, minZoom: MIN_ZOOM, maxZoom: MAX_ZOOM });
  const seen = visibleIds(boxes, viewport, fitAllPlan);
  const offscreen = boxes.length - seen.length;

  const rawFitAll = fitAllScale(bounds, viewport);
  const clamped = Math.min(Math.max(rawFitAll, MIN_ZOOM), Math.max(MIN_ZOOM, MAX_ZOOM));
  const fitWidth = Math.min(Math.max(rawFitAll, MIN_READABLE_SCALE), Math.max(MIN_READABLE_SCALE, MAX_ZOOM));

  return {
    nodes: boxes.length,
    depth: depthMax,
    overlapPairs: pairs.length,
    overlapIds,
    offscreen,
    longEdges,
    nominalGap,
    axis,
    widestLayer: layers.length ? Math.max(...layers) : 0,
    layers,
    branchWidths: branchWidths(root),
    bounds,
    fitAllZoom: rawFitAll,
    clampedFitAllZoom: clamped,
    readableRatio: rawFitAll / MIN_READABLE_SCALE,
    readableAtFitAll: clamped >= MIN_READABLE_SCALE,
    fitWidthZoom: fitWidth,
  };
}

/**
 * 这一屏（`plan` 给出的缩放与偏移）能看见哪些节点。
 *
 * 判据是**矩形相交**，不是"中心点在视口里"：一个 180 宽的节点只要露出 10 像素，
 * 用户就能看见它并顺着它平移过去；中心点判据会把这种"露出一点"算成看不见，
 * 于是"还有 N 个节点在视野外"这个数字会比实际大，而那个数字是用来劝用户平移的。
 */
export function visibleIds(boxes: readonly Box[], viewport: Size, plan: { x: number; y: number; zoom: number }): string[] {
  if (viewport.width <= 0 || viewport.height <= 0 || plan.zoom <= 0) return [];
  const left = -plan.x / plan.zoom;
  const top = -plan.y / plan.zoom;
  const right = left + viewport.width / plan.zoom;
  const bottom = top + viewport.height / plan.zoom;
  const visible: string[] = [];
  for (const box of boxes) {
    if (box.x < right && left < box.x + box.width && box.y < bottom && top < box.y + box.height) {
      visible.push(box.id);
    }
  }
  return visible;
}

/** `chooseViewMode` 的结论。 */
export interface ViewChoice {
  mode: ViewMode;
  /** 该档位下节点正文的字号（CSS 像素）。 */
  fontPx: number;
  /** `fit-width` 时看不见的节点数；`fit-all` 时为 0（装得下才有 fit-all）。 */
  offscreen: number;
  /** 一句话说清为什么这么选（界面/日志/报告共用同一条文案）。 */
  reason: string;
}

/**
 * 生成完之后该用哪一档——**确定性判据，不含任何猜测**。
 *
 * 判据只有一条：
 *
 *     `fit-all` 的缩放 ≥ `MIN_READABLE_SCALE`（0.857）  →  fit-all
 *     否则                                              →  fit-width
 *
 * 为什么它是"看不全 vs 看不清"那句话的精确形式：`fit-all` 是**唯一**能看全的档位，
 * 而它能不能看全的代价是否可接受，只取决于它的字号是否还读得出来。语文化那条设计
 * 判据——"看不全的代价是一次已知方向的操作；看不清的代价是信息为零"——就是"先问
 * 能不能看清，再决定要不要看全"。
 *
 * **为什么不按字面写"节点数 × 每节点宽度 > 画布宽 × 可读比例"**：那条判据只在"横向
 * 是唯一瓶颈"时与实际一致。实测反例（`tests/canvas-viewport.spec.ts` 里那棵 42 节点
 * 的树）：宽 909、视口 1600，横向**装得下**；瓶颈在高度（1952 高），`fit-all` 的缩放
 * 是 0.364。按字面判据它会走 `fit-all`，字变成 5.1px——正是这一轮要修的那个 bug。
 * 所以判据落在 `fitAllScale`（两个方向取小）上，与 `planView` 用的**是同一行算术**，
 * 不可能出现"判据说看得清、视图算出来 5px"的分歧。
 */
export function chooseViewMode(input: {
  boxes: readonly Box[];
  viewport: Size;
  bounds?: Bounds;
}): ViewChoice {
  const bounds = input.bounds ?? contentBounds(input.boxes.map(box => ({
    position: { x: box.x, y: box.y },
    data: { size: { width: box.width, height: box.height } },
  }))) ?? { x: 0, y: 0, width: 0, height: 0 };

  const allPlan = planView({ mode: 'fit-all', bounds, viewport: input.viewport, minZoom: MIN_ZOOM, maxZoom: MAX_ZOOM });

  // 空画布：没有内容要看，选哪一档都一样，按默认档（可读优先）给，不制造一个"全图"
  // 的假结论。
  if (!input.boxes.length || input.viewport.width <= 0 || input.viewport.height <= 0) {
    return { mode: 'fit-width', fontPx: NODE_FONT_PX * MIN_READABLE_SCALE, offscreen: 0, reason: '画布上还没有内容' };
  }

  if (allPlan.zoom >= MIN_READABLE_SCALE) {
    return {
      mode: 'fit-all',
      fontPx: NODE_FONT_PX * allPlan.zoom,
      offscreen: 0,
      reason: `整张图能装进视口，且字号 ${(NODE_FONT_PX * allPlan.zoom).toFixed(1)}px 仍不低于 `
        + `${NODE_FONT_PX * MIN_READABLE_SCALE}px：看全不吃亏`,
    };
  }

  const widthPlan = planView({ mode: 'fit-width', bounds, viewport: input.viewport, maxZoom: MAX_ZOOM });
  const visible = visibleIds(input.boxes, input.viewport, widthPlan).length;
  return {
    mode: 'fit-width',
    fontPx: NODE_FONT_PX * widthPlan.zoom,
    offscreen: input.boxes.length - visible,
    reason: `整张图装进来要缩到 ${allPlan.zoom.toFixed(3)}（字号 `
      + `${(NODE_FONT_PX * allPlan.zoom).toFixed(1)}px < ${NODE_FONT_PX * MIN_READABLE_SCALE}px）：`
      + `停在 ${MIN_READABLE_SCALE.toFixed(3)}，把 ${input.boxes.length - visible} 个节点留在视野外`,
  };
}
