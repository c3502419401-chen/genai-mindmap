import type { MindNode } from '../api/types';
import { layoutDiagram } from './layout';
import type { Direction } from './layout/tidy';
import {
  MIN_READABLE_SCALE,
  fitAllScale,
  type Size,
} from './viewport';

/**
 * **视图层收起**：数据一直是完整的树，只是这一屏默认不把每一个节点都摊开。
 *
 * 与"折叠"（`7d25a85` 退休掉的 `_fold_tree`）的区别只有一条，但它是全部：
 * 折叠**改数据**（被并掉的子树连节点都不在了，保存、导出、撤销栈里全没有），
 * 收起**只改视图**（`content` 一个字节都不动，画布按层数少画几层，每个收起的地方
 * 留一个「还有 N 个」的入口）。
 *
 * 这个模块是收起的**纯函数**部分：给定一棵树与"展开到第几层"，算出
 *
 *   1. 画布上该画的那棵树（浅拷贝，只换 `children` 数组）；
 *   2. 哪些节点在画布上被收起了、各藏了多少（渲染层拿去画「还有 N 个」）；
 *   3. 这个视口下**最深可以展开到几层**（由 `fitAllScale` 反推，见 `chooseExpandDepth`）。
 *
 * 坐标、连线、句柄全都不在这里算：那是 `layoutDiagram` 与 store 的事。这里只负责
 * "少画哪几层"。
 */

/**
 * 默认展开到第几层（根的层号是 0）。
 *
 * `1` = 根 + 一级分支。这个数不是拍的，是**量出来的**（`tests/view-collapse.spec.ts`
 * 与 `docs/VIEW-COLLAPSE.md` 的读数表，1600×852、三棵真树）：
 *
 * | 展开到 | 可见节点（136 节点那棵） | `fit-all` 字号 |
 * | --- | --- | --- |
 * | 全展开 | 136 | 1.6px |
 * | 3 层 | 61 | 3.5px |
 * | 2 层 | 25 | 8.3px |
 * | 1 层 | 7 | **25.6px** |
 *
 * 判据是"整张图塞进视口之后字还读得出来"（≥ `MIN_READABLE_FONT_PX`）。同一条判据
 * 在 2 层那里就崩了（8.3px），所以默认只能是一层——**不是审美选择，是算术结论**。
 *
 * 这同时解释了为什么"再收浅一点"这条路走不通：正整数层数里，1 与 2 之间没有第三档。
 * 想按"每支前 N 个"再细分，得先有一套分组标题（`docs/FOLD-RETIRED.md` §5 记过
 * 一条批量概括调用的实测：1.87 秒/5 组）——这一轮不做。
 */
export const DEFAULT_EXPAND_DEPTH = 1;

/**
 * "不收起"这个值。设成它等价于**回到加收起之前的行为**（回滚路径）。
 *
 * 用 `Infinity` 而不是 `-1` 或一个很大的数：判定只有一句 `层号 <= expandDepth`，
 * 无穷大天然让所有节点通过，不需要第二套"这是不是特殊值"的分支。
 */
export const EXPAND_ALL_DEPTH = Number.POSITIVE_INFINITY;

/** 一个折叠点在画布上藏了什么。 */
export interface FoldInfo {
  /**
   * **直接子节点数**——点一下当场会多出来的节点数。
   *
   * 「还有 N 个」的 N 必须是这个数，不能是整支：按钮上写 18、点开只冒出 3 个，
   * 用户下一次就不信这个数了。整支的规模放在 `subtree` 里，给 `title` 与读屏用。
   */
  children: number;
  /** 整棵子树（含更深层、含自身以外的全部后代）的节点数。 */
  subtree: number;
}

export interface ViewTreeResult {
  /**
   * 画布该画的那棵树。
   *
   * **可能不是同一个引用**：有节点被收起时是浅拷贝（只换 `children`），没有收起时
   * 原样返回入参——所以"全展开"这条路上游拿到的对象与改动之前完全一致，
   * `layoutTidy` 的入参一个字段都没变，逐像素一致是**结构上**成立的，不靠用例运气。
   */
  root: MindNode;
  /** 被收起的节点 id → 它藏了多少。没收起的节点没有条目（不是 `{children: 0}`）。 */
  folded: Map<string, FoldInfo>;
}

export interface ViewTreeOptions {
  /** 展开到第几层（根的层号 0）。`EXPAND_ALL_DEPTH` = 不收。 */
  expandDepth: number;
  /**
   * 用户**点开过**的节点。这个集合里的节点无视层数限制，自己的子节点一律显示。
   *
   * 为什么记"展开过"而不是"收起过"：默认值是"浅"，所以需要记住的例外是"这一个我
   * 要看"；反过来记"收起过"意味着默认必须全展开，那第一次进来就得先画 136 个节点
   * 再收起来——闪一下，而且那一帧的布局是白算的。
   */
  expanded?: ReadonlySet<string>;
}

const NO_IDS: ReadonlySet<string> = new Set<string>();

/** 一棵子树有多少节点（含自身）。折叠点的「整支还有 N 个」用它。 */
export function countSubtree(node: MindNode): number {
  let total = 1;
  for (const child of node.children) total += countSubtree(child);
  return total;
}

/** 这棵树最深几层（只有一个根时是 0）。 */
export function depthOf(root: MindNode): number {
  let deepest = 0;
  for (const child of root.children) deepest = Math.max(deepest, depthOf(child));
  return root.children.length ? deepest + 1 : 0;
}

/**
 * 按"展开到第几层"给出一棵**视图树**，并记下每个折叠点藏了多少。
 *
 * 三条不变量（用例逐条钉着，改动时别破坏它们）：
 *
 * 1. **节点引用只在需要时换**：被收起的节点、以及它的祖先链上是浅拷贝；其余子树
 *    仍是原对象。所以没被收起的那部分，`node === content 里的 node` 成立——
 *    下游任何"按引用判断这棵树变没变"的地方（`generatedContent` 的引用相等判断）
 *    都不会被视图层搅乱。
 * 2. **层号不压缩**：收起第 3 层不会让第 4 层升到第 3 层。层号是内容属性
 *    （`depth` 还决定配色继承与详情面板的面包屑），视图层不许改它。
 * 3. **全展开时必须原样返回**：`expandDepth >= depthOf(root)` 且没有 `expanded`
 *    时直接返回入参对象，`folded` 是空表。否则"打开旧图不许变样"这条会在
 *    一个不可能被发现的角落里破掉。
 */
export function viewTree(root: MindNode, options: ViewTreeOptions): ViewTreeResult {
  const expanded = options.expanded ?? NO_IDS;
  const depth = options.expandDepth;
  // 快路径：不收起。`Infinity >= x` 恒真，所以这里不需要给无穷大单开一条分支。
  if (!expanded.size && depth >= depthOf(root)) {
    return { root, folded: new Map() };
  }

  const folded = new Map<string, FoldInfo>();

  const walk = (node: MindNode, level: number): MindNode => {
    const children = node.children;
    if (!children.length) return node;
    // 这个节点的子节点要不要画：用户点开过，或者子节点的层号还在默认范围内。
    const open = expanded.has(node.id) || level + 1 <= depth;
    if (!open) {
      folded.set(node.id, { children: children.length, subtree: countSubtree(node) - 1 });
      return { ...node, children: [] };
    }
    let changed = false;
    const next: MindNode[] = [];
    for (const child of children) {
      const mapped = walk(child, level + 1);
      if (mapped !== child) changed = true;
      next.push(mapped);
    }
    // 一个后代都没被收起 → 这个节点保持原引用（见不变量 1）。
    return changed ? { ...node, children: next } : node;
  };

  return { root: walk(root, 0), folded };
}

/**
 * 这个视口下**最深可以展开到几层**：从全展开往下试，取第一个"整张图装得下、字号
 * 还在可读下限之上"的层数。
 *
 * 判据与 `clarity.chooseViewMode` 是同一条算术（`fitAllScale` ≥ `MIN_READABLE_SCALE`），
 * 只换了自变量：那边问"该用哪一档视图"，这边问"该展开到第几层"。共用同一条判据的
 * 理由很实际——分两处写，就会出现"收起之后自动切到全图档、字仍然只有 8px"这种
 * 两句话都成立、画面却读不出来的状态。
 *
 * 返回 `EXPAND_ALL_DEPTH` 的情形是**小图**：全展开就能装下且可读，一个节点都不收起。
 * 这条分支是"别替用户决定他看什么"的兑现口——30 个节点以下的图完全看不到收起这回事。
 *
 * 怎么试都装不下（层数 1 仍然超宽/超高，例如一个根挂 50 支）时返回 `1`：那是层数
 * 这个旋钮的尽头，剩下的交给已有的「适应宽度」档（拖动平移）与「全图」按钮。
 */
export function chooseExpandDepth(input: {
  root: MindNode;
  viewport: Size;
  direction?: Direction;
}): number {
  const { root, viewport, direction } = input;
  if (viewport.width <= 0 || viewport.height <= 0) return DEFAULT_EXPAND_DEPTH;
  const deepest = depthOf(root);

  const readableAt = (candidate: MindNode): boolean => (
    fitZoom(candidate, viewport, direction) >= MIN_READABLE_SCALE
  );

  // 全展开先试：够清楚就一个都不收，这是绝大多数小图与中等图的结论。
  if (readableAt(root)) return EXPAND_ALL_DEPTH;
  // 从深往浅找第一个达标的层数——达标随层数递减，所以第一个就是最深的那个。
  for (let layer = deepest - 1; layer >= 1; layer -= 1) {
    if (readableAt(viewTree(root, { expandDepth: layer }).root)) return layer;
  }
  return DEFAULT_EXPAND_DEPTH;
}

/** 整张图按 `fit-all` 塞进视口需要多大缩放。布局走与画布同一个分派函数。 */
function fitZoom(root: MindNode, viewport: Size, direction?: Direction): number {
  const layout = layoutDiagram({
    root,
    diagramType: 'tree',
    ...(direction === undefined ? {} : { direction }),
  });
  const { minX, minY, width, height } = layout.bounds;
  return fitAllScale({ x: minX, y: minY, width, height }, viewport);
}
