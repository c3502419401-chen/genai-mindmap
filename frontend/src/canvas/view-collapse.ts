import type { MindNode } from '../api/types';

/**
 * **视图层收起**：数据一直是完整的树，只是这一屏默认不把每一个节点都摊开。
 *
 * 与"折叠"（`7d25a85` 退休掉的 `_fold_tree`）的区别只有一条，但它是全部：
 * 折叠**改数据**（被并掉的子树连节点都不在了，保存、导出、撤销栈里全没有），
 * 收起**只改视图**（`content` 一个字节都不动，画布少画的那些节点在原处留一个
 * 「还有 N 个没显示」的入口）。
 *
 * ## 口径：**显示节点的数量上限**，不是"展开到第几层"
 *
 * 上一版（`d6f40efd`）按层数收：显示某节点的子节点 ⟺ 子节点层号 ≤ `expandDepth`。
 * 那个口径是**结构**口径，它解决不了"层数对了但节点还是太多"：一棵 6 支 × 每支 3 ×
 * 3 的树，展开 2 层是 25 个节点、`fit-all` 只有 8.3px；而展开 1 层只有 7 个节点、
 * 一屏看全却看不到任何细节。正整数层数里 1 与 2 之间没有第三档。
 *
 * 这一版换成**预算**口径，与产品负责人的原话一致（"还是有显示节点上限，但是如果生成
 * 节点多于显示节点，就不继续在导图里面显示了，但还是有生成的节点"）：
 *
 *   画布上最多画 `limit` 个节点，剩下的**一个都不删**，只在这一屏不画。
 *
 * 三个附带结论，都是这个口径的**好处**而不是巧合：
 *
 * 1. **小图完全隐形**：`总节点数 ≤ limit` 时一个都不收，不需要任何视口自适应
 *    （层数口径必须先量视口才知道该展开几层，"同一张图在两台机器上显示不同的节点"
 *    正是那个口径的近亲）。
 * 2. **上限与"一屏看得清"是两条轴**，别混：40 个节点在 1600×852 上 `fit-all` 是
 *    5.3px（见 `docs/VIEW-COLLAPSE.md` §一），所以画布会走 `fit-width` 那一档
 *    （字号 12px、纵向拖动看），而不是"一屏看全"。要一屏读全得把上限压到 15 上下。
 * 3. **截断点落在"数量"上，不在"层"上**，所以必须有一个**常驻**的"这里被截了"标记
 *    ——那是 `FoldInfo` 与渲染层那枚 `+N` 角标的全部职责（见 §"选哪些节点"）。
 *
 * ## 选哪些节点：按层铺满（BFS），不是前序（DFS）
 *
 * 实测（1600×852、库里三棵真树、上限 40）：前序会让**第一条分支连同它整棵子树吃满
 * 预算**，6 支只露出 2 支（`docs/VIEW-COLLAPSE.md` §二 的读数）。那正是折叠被退休的
 * 理由——"只看到一支"。按层铺满则是"先根、再全部一级分支、再各自的子树"，每一支都
 * 露头，用户先拿到的是**目录**，这与他看思维导图的方式一致。
 *
 * 层内保持树序（不做"每支轮转"）：轮转会让"为什么这个节点的孩子显示了、那个没有"
 * 变成一个需要单独解释的规则，而真正防住"看着完整其实缺一块"的东西是**每个被截断的
 * 父节点上都必须有可见的标记**，不是边界层的对称性。
 *
 * ## 三道显式闸门（都记在 `ViewTreeOptions` 里，优先级从高到低）
 *
 * ```
 * 用户收起过这个节点   → collapsed.has(id)   → 一个子节点都不画（哪怕预算够）
 * 用户点开过这个节点   → expanded.has(id)    → 全部子节点都画（哪怕预算不够）
 * 用户点了"全部展开"   → expandAll           → 所有节点全画（回滚到加收起之前）
 * 否则                → 按预算铺满，铺到 `limit` 为止
 * ```
 *
 * "点开的节点不受预算限制"是**用户要求**直接推出来的（"由用户决定是否展开查看"）：
 * 让预算拦住一次明确的点击，表现就是"点了没反应"。
 */

/**
 * 画布上默认最多画几个节点。
 *
 * `40` 的出处是 `docs/LAYOUT-CLARITY.md` 的分档读数（58 张真图）：`< 40` 的图里
 * 还有 65% 能读，`40–79` 那一档 **32 张全部**不可读。所以 40 是"再往上一定看不清"
 * 的分界线，不是一个"40 个节点正好好看"的审美数。
 *
 * 它**不是**"一屏看全"的保证：1600×852 上 40 个节点的 `fit-all` 只有 5.3px（实测，
 * 见 `docs/VIEW-COLLAPSE.md` §一），画布会落在 `fit-width` 那一档（字号 12px、拖动看）。
 * 两个数说的是两件事：这一档管**认知预算**（一屏上不要出现 136 个框），
 * 字号由 `clearity.chooseViewMode` 管。
 */
export const MINDMAP_VISIBLE_NODE_LIMIT = 40;

/**
 * "不限制"这个值（`limit <= 0`）。等价于**回到加收起之前的行为**（回滚路径）。
 *
 * 用 `<= 0` 而不是一个很大的数：调用方在设置里把它设成 `0` 就是"关掉这个功能"，
 * 判定只有一句 `limit > 0`，不需要第二套"这是不是特殊值"的分支。
 */
export const NO_NODE_LIMIT = 0;

/**
 * 一级分支上那枚**概括角标**最少要"还有几个"才值得画。
 *
 * `4` 的判据是**决策成本**不是审美：一支下面只差 1–3 个节点时，用户会直接点开
 * （成本一次点击），而读一句概括再决定要不要点，反而更贵；何况那 3 个数与它头上
 * 那枚 `+N` 说的是同一件事。反过来，差 6 个以上时"这一支讲什么"才真的能帮上忙
 * ——实测那棵 136 节点的真树六支分别是 12/15/21/6/18/24（都过线，一枚都没被这条
 * 判据拦掉，所以它不制造"为什么这枚没了"的困惑）。
 *
 * 与 `MINDMAP_VISIBLE_NODE_LIMIT` 不同，这个数**不影响任何可见性**：它只决定画不画
 * 那枚角标，收起与展开的规则一个字都不动。
 */
export const MINDMAP_BRANCH_BADGE_MIN_HIDDEN = 4;

/** 一个节点在画布上缺了多少东西。 */
export interface FoldInfo {
  /**
   * **直接子节点里没画出来的个数**——点一下当场会多出来的节点数。
   *
   * 角标上的数字必须是这个数，不能是整支：写 18、点开只冒出 3 个，用户下一次就不
   * 信这个数了。整支的规模放在 `closedSubtree` 里，给 `title` 与读屏用。
   *
   * 注意它**不等于"直接子节点数"**（上一版是）。数量口径下会出现"这个父节点显示了
   * 2 个孩子、还有 1 个没显示"（预算刚好在它这里用完），那时角标是 `+1`。
   */
  hidden: number;
  /** 直接子节点的总数（= 显示出来的 + `hidden`）。给 `title` 用。 */
  total: number;
  /** 整棵子树里没画出来的节点数（含更深层，不含自身）。 */
  closedSubtree: number;
}

/**
 * 一个节点的子节点**为什么**显示着。渲染层据此决定要不要给"叠回去"的入口。
 *
 * - `expanded` 用户点开过它（回程：再点一下收起）
 * - `all`      用户点了「全部展开」（回程：再点一下收起这一支，或「收起到上限」）
 * - `budget`   预算本来就够（**没有"更大/更小"可切换**，点节点只是选中它）
 *
 * "一个孩子都没显示"不在这张表里：那种节点的入口是 `+N`（走 `folded`），给它一个
 * "叠回去"的减号等于说"这里已经摊开了"，与画面对不上。
 */
export type FoldOpenReason = 'expanded' | 'all' | 'budget';

export interface ViewTreeResult {
  /**
   * 画布该画的那棵树。
   *
   * **可能不是同一个引用**：有节点被收起时是浅拷贝（只换 `children`），没有收起时
   * 原样返回入参——所以"全展开"这条路上游拿到的对象与改动之前完全一致，
   * `layoutTidy` 的入参一个字段都没变，逐像素一致是**结构上**成立的，不靠用例运气。
   */
  root: MindNode;
  /** 被截断的节点 id → 它缺了什么。没缺东西的节点没有条目（不是 `{hidden: 0}`）。 */
  folded: ReadonlyMap<string, FoldInfo>;
  /**
   * **这一支还有几个节点没画**：节点 id → 它整棵子树里没画出来的节点数（`> 0` 才有
   * 条目）。
   *
   * 与 `folded.closedSubtree` 是**同一个量的两个出口**，但覆盖面不同，这个区别就是
   * 它存在的全部理由：
   *
   *   - `folded` 只有条目 ⟺ **这个父节点自己有直接孩子没画**（`+N` 挂在这里）；
   *   - `unshown` 只要**它下面任何一处**有节点没画就有条目，哪怕它自己的孩子全画了。
   *
   * 一级分支上的概括角标要的是后者：它说"这一支还有 21 个"，而那 21 个完全可能
   * 分布在三四层深处、中间没有一处直接丁口。用 `folded` 判会漏掉那些支——它们的
   * 角标会凭空消失，而用户看不出为什么这一支没有。
   *
   * **不合并进 `folded`**：`folded.has(id)` 现在等于"这里能点开、点了会冒出 N 个"，
   * 给一个孩子全显示着的节点塞进 `folded`，`+N` 就会长在一个点下去什么都不变的位置。
   */
  unshown: ReadonlyMap<string, number>;
  /** 子节点正显示着的节点 id → 为什么显示。叶子节点没有条目。 */
  open: ReadonlyMap<string, FoldOpenReason>;
  /** 画布上真正画出来的节点数（界面那句「显示 N / M」的 N）。 */
  visible: number;
}

export interface ViewTreeOptions {
  /** 画布上最多画几个节点。`<= 0` = 不限制。 */
  limit: number;
  /**
   * 用户**点开过**的节点：无视预算，自己的全部子节点一律显示。
   *
   * 为什么记"展开过"而不是"收起过"：默认值是"少"，所以需要记住的例外是"这一个我
   * 要看"；反过来记"收起过"意味着默认必须全展开，那第一次进来就得先画 136 个节点
   * 再收起来——闪一下，而且那一帧的布局是白算的。
   */
  expanded?: ReadonlySet<string>;
  /** 用户**收起过**的节点：一个子节点都不画，哪怕预算还够。 */
  collapsed?: ReadonlySet<string>;
  /** 用户点了「全部展开」：所有节点都画（除非它自己在 `collapsed` 里）。 */
  expandAll?: boolean;
}

const NO_IDS: ReadonlySet<string> = new Set<string>();
const NO_FOLDS: ReadonlyMap<string, FoldInfo> = new Map<string, FoldInfo>();
const NO_OPEN: ReadonlyMap<string, FoldOpenReason> = new Map<string, FoldOpenReason>();
const NO_UNSHOWN: ReadonlyMap<string, number> = new Map<string, number>();

/** 一棵子树有多少节点（含自身）。折叠点的「整支还有 N 个」用它。 */
export function countSubtree(node: MindNode): number {
  let total = 1;
  for (const child of node.children) total += countSubtree(child);
  return total;
}

/** 这棵树最深几层（只有一个根时是 0）。给读数与报告用。 */
export function depthOf(root: MindNode): number {
  let deepest = 0;
  for (const child of root.children) deepest = Math.max(deepest, depthOf(child));
  return root.children.length ? deepest + 1 : 0;
}

/**
 * 按**数量上限**给出一棵视图树，并记下每个被截断的节点缺了什么。
 *
 * 四条不变量（用例逐条钉着，改动时别破坏它们）：
 *
 * 1. **节点引用只在需要时换**：被截断的节点、以及它的祖先链上是浅拷贝；其余子树仍是
 *    原对象。所以没被截断的那部分，`node === content 里的 node` 成立——下游任何"按
 *    引用判断这棵树变没变"的地方都不会被视图层搅乱。
 * 2. **层号不压缩**：收起第 3 层不会让第 4 层升到第 3 层。层号是内容属性（`depth`
 *    还决定配色继承与详情面板的面包屑），视图层不许改它。
 * 3. **不该收起时必须原样返回**：没有显式闸门、且（不限制或总数装得下）时直接返回
 *    入参对象，`folded` / `open` 是空表。否则"打开旧图不许变样"这条会在一个不可能
 *    被发现的角落里破掉。
 * 4. **可见集合是"祖先闭合"的**：一个节点在画布上 ⟹ 它的父链全部在画布上。这条是
 *    布局能直接吃这棵树的前提（见 `docs/VIEW-COLLAPSE.md` §三：`layoutTidy` 不要求
 *    形状规整，但要求它是一棵连通的、根可达的树）。
 */
export function viewTree(root: MindNode, options: ViewTreeOptions): ViewTreeResult {
  const expanded = options.expanded ?? NO_IDS;
  const collapsed = options.collapsed ?? NO_IDS;
  const expandAll = options.expandAll ?? false;
  const limit = options.limit;
  const total = countSubtree(root);

  // 快路径：没有任何显式闸门，且预算本来就够 → 一个节点都不动。
  if (!expanded.size && !collapsed.size && !expandAll && (limit <= 0 || total <= limit)) {
    return { root, folded: NO_FOLDS, open: NO_OPEN, visible: total, unshown: NO_UNSHOWN };
  }

  /**
   * **第一步：预算前缀。** 按层铺满，取前 `limit` 个节点。
   *
   * 它是一个**固定的集合**，不是"边走边扣的余额"。这个区别决定了一件用户能看见的事：
   * 收起一支之后，那一支的额度**不会**被后面的分支顶上来。否则"我收起了 A，B 的孩子
   * 却突然冒出来"——用户点的是"少看一点"，画布上的节点数却一个都没少，只是换了一批。
   *
   * 队列就是显示顺序，只用 `push` 与下标遍历（`shift` 是 O(n)，这条路径每次内容变化
   * 都会重跑，没必要）。
   */
  const visible = new Set<string>([root.id]);
  if (limit <= 0) {
    forEachNode(root, node => visible.add(node.id));
  } else {
    let budget = limit - 1; // 根一定在画布上，它吃掉自己的那一格
    const queue: MindNode[] = [root];
    for (let index = 0; index < queue.length && budget > 0; index += 1) {
      const node = queue[index];
      const take = Math.min(node.children.length, budget);
      for (const child of node.children.slice(0, take)) {
        visible.add(child.id);
        queue.push(child);
      }
      budget -= take;
    }
  }

  /**
   * **第二步：闸门。** 先加（用户点开的 / 全部展开），再减（用户收起的）。
   *
   * 加法只放**直接子节点**：角标写的是 `+N`，点下去就该冒 N 个；把整支都放出来会让
   * 角标上的数与实际出现的不一致（那正是上一版反复强调过的"这个数第二次就没人信"）。
   * 再深的地方照旧带它自己的角标，用户一层一层决定。
   *
   * 减法按**整棵子树**剪：收起一个节点，就是"这一支我这一屏不看"，留着它的孙辈在画布
   * 上会变成孤儿（不变量 4）。减法也在加法之后，所以"某个祖先被收起"永远盖过"某个
   * 后代被点开"——状态冲突时保守的一边赢。
   */
  if (expandAll) {
    forEachNode(root, node => visible.add(node.id));
  } else if (expanded.size) {
    for (const id of expanded) {
      const node = findNode(root, id);
      if (!node) continue;
      for (const child of node.children) visible.add(child.id);
    }
  }
  if (collapsed.size) {
    const prune = (node: MindNode): void => {
      if (collapsed.has(node.id)) {
        forEachNode(node, child => {
          if (child !== node) visible.delete(child.id);
        });
        return;
      }
      for (const child of node.children) prune(child);
    };
    prune(root);
  }

  /** 第三步：算每棵子树"总节点 / 画出来的节点"，把 `closedSubtree` 补上。 */
  const totalIn = new Map<string, number>();
  const shownIn = new Map<string, number>();
  const stats = (node: MindNode): void => {
    let sum = 1;
    let shown = visible.has(node.id) ? 1 : 0;
    for (const child of node.children) {
      stats(child);
      sum += totalIn.get(child.id) ?? 1;
      shown += shownIn.get(child.id) ?? 0;
    }
    totalIn.set(node.id, sum);
    shownIn.set(node.id, shown);
  };
  stats(root);

  const folded = new Map<string, FoldInfo>();
  const open = new Map<string, FoldOpenReason>();
  const kept = new Map<string, readonly MindNode[]>();
  /**
   * `unshown`：每个节点"整棵子树里还差几个"。与 `stats` 同一遍算完就够，不需要
   * 再遍历一次树——`totalIn` / `shownIn` 两个表已经给出了差。
   */
  const unshown = new Map<string, number>();
  for (const [id, sum] of totalIn) {
    const gap = sum - (shownIn.get(id) ?? 0);
    if (gap > 0) unshown.set(id, gap);
  }

  /**
   * 第四步：定"每个父节点保留哪些孩子"，并同时写下 `folded` / `open`。
   *
   * 三样东西必须**同一处**算出来：`folded.hidden`（角标写几）、`open`（给不给"叠回去"
   * 的入口）、`kept`（画哪些）三者要是各算一遍，边界上一定出现"角标写 +2、点开只冒出
   * 1 个"这种两句话都成立、画面却对不上的状态。
   */
  const decide = (node: MindNode): void => {
    if (!node.children.length) return;
    const keep = node.children.filter(child => visible.has(child.id));
    kept.set(node.id, keep);
    if (!keep.length) {
      // 一个孩子都没显示：不进 `open`。渲染层据此不给"叠回去"的入口——它头上该挂的是
      // `+N`（走 `folded`），挂一个减号在那儿等于说"这里已经摊开了"，是反的。
      //
      // 也不再往下走：孩子都不在画布上，它们的子节点更不可能在。这张表只收**画布上**
      // 的节点，否则"可见 + 被截的闭包 = 总数"这条加法会把同一批节点数两遍。
      folded.set(node.id, {
        hidden: node.children.length,
        total: node.children.length,
        closedSubtree: (totalIn.get(node.id) ?? 1) - (shownIn.get(node.id) ?? 1),
      });
      return;
    }
    const reason: FoldOpenReason = expanded.has(node.id)
      ? 'expanded'
      : expandAll ? 'all' : 'budget';
    open.set(node.id, reason);
    if (keep.length < node.children.length) {
      folded.set(node.id, {
        hidden: node.children.length - keep.length,
        total: node.children.length,
        closedSubtree: (totalIn.get(node.id) ?? 1) - (shownIn.get(node.id) ?? 1),
      });
    }
    for (const child of keep) decide(child);
  };
  decide(root);

  /**
   * 最后一遍：建视图树。**只在需要时换引用**（不变量 1）——全显示且后代一个都没变时
   * 直接返回原节点，于是"预算刚好够的那一支"与改动之前的对象是同一个。
   */
  const build = (node: MindNode): MindNode => {
    const keep = kept.get(node.id);
    if (!keep) return node;
    if (keep.length === node.children.length) {
      let changed = false;
      const next = node.children.map(child => {
        const mapped = build(child);
        if (mapped !== child) changed = true;
        return mapped;
      });
      return changed ? { ...node, children: next } : node;
    }
    return { ...node, children: keep.map(build) };
  };

  return { root: build(root), folded, open, visible: visible.size, unshown };
}

/** 前序遍历一棵树（含自身）。 */
function forEachNode(root: MindNode, visit: (node: MindNode) => void): void {
  visit(root);
  for (const child of root.children) forEachNode(child, visit);
}

/** 按 id 找节点；找不到返回 `null`（换图之后那些 id 就属于上一棵树了）。 */
function findNode(root: MindNode, id: string): MindNode | null {
  if (root.id === id) return root;
  for (const child of root.children) {
    const hit = findNode(child, id);
    if (hit) return hit;
  }
  return null;
}
export type FoldAction = 'expand' | 'collapse' | 'none';

export function foldActionOf(
  id: string,
  view: Pick<ViewTreeResult, 'folded' | 'open'>,
): FoldAction {
  if (view.folded.has(id)) return 'expand';
  const reason = view.open.get(id);
  if (reason === 'expanded' || reason === 'all') return 'collapse';
  return 'none';
}
