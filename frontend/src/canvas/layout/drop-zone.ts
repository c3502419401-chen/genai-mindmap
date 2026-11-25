import type { DropZone } from '../../mindmap/drop-plan';
import type { Direction } from './tidy';

/**
 * 屏幕上的哪一侧。与 `tidy.ts::ChildSide` 是同一套字面量，但**刻意不复用**：
 * `ChildSide` 的名字里带着"这是子节点那一侧"的语义（它由 `childSide()` 按几何
 * 反推出句柄），而这里是"指针落在目标节点的哪一侧"，与树结构无关。共用一个类型
 * 会让"把指针的方位当成连线的方位"看起来像理所当然。
 */
export type DropSide = 'left' | 'right' | 'top' | 'bottom';

/**
 * 命中区在**两个轴上各自**外扩多少像素。
 *
 * 必须分开两个数，因为节点盒子是 180×48，两个方向上的"余量"根本不是一回事：
 *
 *  - `DROP_PAD_X = 20`：沿层级方向（父子）外扩。相邻层之间的空隙是
 *    `tidy.depthGap = 56`，20 不会够到下一层的节点。
 *  - `DROP_PAD_Y = 24`：沿兄弟方向外扩。相邻兄弟的中心距是 68（48 高 + 20 间距），
 *    两个 24 的外扩确实会在中间重叠 28px——**这是允许的**，因为重叠区两侧给出的
 *    答案恰好等价：对上面那个节点是"插到它后面"，对下面那个是"插到它前面"，同一个
 *    结果、同一个下标。真正的仲裁仍然靠 `dropHitOf` 的 `nearness`。
 *
 * 为什么上下不取更小（10，即间距的一半，保证零重叠）：48 像素高的盒子上，"放到
 * 上面"这个手势的可操作区域就只有 24+10=34 像素高的一条，比手指的落点误差还小。
 * 宁可让命中区重叠、由 `nearness` 决出胜负，也不要把一个合法手势做成"要瞄准"。
 */
export const DROP_PAD_X = 20;
export const DROP_PAD_Y = 24;

export interface DropRect {
  /** 左上角 */
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface DropPointer {
  x: number;
  y: number;
}

export interface DropHit {
  side: DropSide;
  /**
   * 指针离这个节点的**中心**有多远：`0` 正中，`1` 正好在命中区边界上。
   *
   * 只有一个命中区时有它没它都一样；两个节点的命中区**重叠**时，它就是唯一的
   * 仲裁量：取最小的那个 = 指针更"身处"谁的框里。没有它，胜负取决于
   * `store.nodes` 的遍历顺序——那种抖动用户只会读成"这个功能时灵时不灵"。
   */
  nearness: number;
}

/**
 * 指针落在目标的哪一侧、有多靠近中心；不在命中区里返回 `null`。
 *
 * 判据是**归一化后的最大分量**，不是"离哪条边最近"：
 *
 * 归一化（`dx` 除以半宽 + `PAD_X`、`dy` 除以半高 + `PAD_Y`）之后两轴同权，45°
 * 对角线成了真正的分界，四种手势在四个象限里各有自己的一块。按"最近边"判则是另一种
 * 划分：节点框内贴着上边缘的位置（比如正上方往里 6 像素）会被判成"上面"。
 * 两种判据在这里**会给出不同答案**，取舍如下——
 *
 *   取归一化：把"越靠近某一侧的外缘越算那一侧"当成规则，于是贴着上边缘但偏左
 *   70 像素的角落判 `left`。代价是角落上的手势有歧义。
 *
 *   取最近边：上下两条边各只有 24 像素的内圈（节点只有 48 高），左右各有 110，
 *   于是**任何偏水平的落点都算左右**，"放到上面"这个手势在做出来之前就先输了。
 *
 * 后者的失败是**系统性的**（一整类手势做不出来），前者的失败只发生在角落（少见的
 * 落点，而且有幽灵框当场纠正）。所以取归一化。
 *
 * 平局（正好在 45° 线上、或正好在中心）判**水平轴**：这套布局里"父 / 子"是比
 * "兄弟"更重的操作（前者改变层级），把边界让给重的那一侧，误触的代价是"多挂了一层"
 * ——Ctrl+Z 一步能退回来；反过来把"想改层级"判成"只挪个位次"，用户会以为拖动没生效。
 * 平局再按符号定左右，于是中心点恒为 `right`（= 子），与"把节点盖在目标身上 = 挂进去"
 * 这条直觉一致。
 */
/**
 * 指针相对一个矩形盒的**归一化偏移**。`dx` / `dy` 落在 `[-1, 1]` 之内就是"在命中区
 * 里"（那就是命中区的定义），`nearness = max(|dx|, |dy|)`。
 *
 * 抽出来单独一个函数，是因为命中判定（`dropHitOf`）与"最近的节点"（`nearestHitOf`）
 * 必须共用同一个度量：前者按椭圆截断，后者专门要用**椭圆之外**的那些点，两者只差最后
 * 那一句 `return null`。写成两份的话，"拖到边缘判给谁"和"空白处双击算离谁近"会在某
 * 一天给出互相矛盾的答案，而那种矛盾在界面上表现为"吸附位置与预览不一致"——最难查的
 * 一类。
 */
function normalizedOf(
  pointer: DropPointer,
  rect: DropRect,
  padX: number,
  padY: number,
): { dx: number; dy: number; nearness: number } | null {
  const halfW = rect.width / 2 + padX;
  const halfH = rect.height / 2 + padY;
  if (halfW <= 0 || halfH <= 0) return null;
  const dx = (pointer.x - (rect.x + rect.width / 2)) / halfW;
  const dy = (pointer.y - (rect.y + rect.height / 2)) / halfH;
  return { dx, dy, nearness: Math.max(Math.abs(dx), Math.abs(dy)) };
}

export function dropHitOf(
  pointer: DropPointer,
  rect: DropRect,
  padX: number = DROP_PAD_X,
  padY: number = DROP_PAD_Y,
): DropHit | null {
  const at = normalizedOf(pointer, rect, padX, padY);
  if (!at) return null;
  const { dx, dy, nearness } = at;
  // 归一化后落在单位正方形之外 = 不在命中区。注意这里判的是**归一化之后**，
  // 也就是说命中区是一个椭圆，而不是"矩形各边外扩"——那正是上面那段取舍的落点。
  if (Math.abs(dx) > 1 || Math.abs(dy) > 1) return null;

  const side: DropSide = Math.abs(dx) >= Math.abs(dy)
    ? (dx < 0 ? 'left' : 'right')
    : (dy < 0 ? 'top' : 'bottom');
  return { side, nearness };
}

/** `dropHitOf` 只要方位的那一半。 */
export function dropSideOf(
  pointer: DropPointer,
  rect: DropRect,
  padX: number = DROP_PAD_X,
  padY: number = DROP_PAD_Y,
): DropSide | null {
  return dropHitOf(pointer, rect, padX, padY)?.side ?? null;
}

/** 判定"哪一侧"要用的上下文：只跟这张图怎么排有关，与节点本身无关。 */
export interface ZoneContext {
  /** 树布局朝向（`tidy.ts`）。流程图与鱼骨图第一轮不上拖动。 */
  direction: Direction;
  /**
   * 双向布局（`both`）里，目标节点在中心主题的**哪一侧**：`+1` 右、`-1` 左、
   * `0` 目标就是中心主题本身（那时左右两侧都长着子节点）。
   *
   * 只有 `both` 会读它。其余三种朝向里根的子节点恒在同一侧，这个字段是噪音。
   */
  outward?: 1 | -1 | 0;
}

/**
 * 屏幕上的方位 → 树上的关系。
 *
 * 这张表就是"左父右子"那句话的全部内容，而它**随朝向翻转**——这是必须的：
 * `direction: 'left'` 时子节点画在父的左边，如果还按"左=父"翻译，用户把节点拖到
 * 目标的左边（画面上那一侧正是"更深"的方向）会得到一个方向相反的结果。同一句
 * 话在四种朝向下有四种含义，所以它只能是一张按朝向展开的表，不能是一句写死的规则。
 *
 * | direction | left | right | top | bottom |
 * |-----------|------|-------|-----|--------|
 * | `right`   | 父   | 子    | 前兄弟 | 后兄弟 |
 * | `left`    | 子   | 父    | 前兄弟 | 后兄弟 |
 * | `down`    | 前兄弟 | 后兄弟 | 父 | 子 |
 * | `both`    | 见 `outward` | 见 `outward` | 前兄弟 | 后兄弟 |
 *
 * `both` 的两侧都是"层"：靠近中心主题的一侧是父、远离的是子，所以要看目标在根的
 * 哪一边。目标就是根时（`outward === 0`）左右两侧都长着子节点，两个方向都是"子"
 * ——根没有父，把节点拖到根的左边挂成一级分支是这张图里最常见的整理动作。
 */
export function dropZoneOf(side: DropSide, context: ZoneContext): DropZone {
  const { direction, outward = 0 } = context;

  if (direction === 'down') {
    if (side === 'top') return 'parent';
    if (side === 'bottom') return 'child';
    return side === 'left' ? 'sibling-before' : 'sibling-after';
  }

  if (direction === 'both') {
    if (side === 'top') return 'sibling-before';
    if (side === 'bottom') return 'sibling-after';
    if (outward === 0) return 'child';
    const deeper = side === 'right' ? 1 : -1;
    return deeper === outward ? 'child' : 'parent';
  }

  // 水平朝向：`right` 的子节点在右，`left` 的子节点在左。
  if (side === 'top') return 'sibling-before';
  if (side === 'bottom') return 'sibling-after';
  const childSide: DropSide = direction === 'left' ? 'left' : 'right';
  return side === childSide ? 'child' : 'parent';
}

/** 目标在中心主题的哪一侧（双向布局要用）。坐标都是**中心点**。 */
export function outwardOf(targetCenterX: number, rootCenterX: number, epsilon = 1): 1 | -1 | 0 {
  if (Math.abs(targetCenterX - rootCenterX) < epsilon) return 0;
  return targetCenterX > rootCenterX ? 1 : -1;
}

/** 一个候选盒子：`id` 只是给调用方回传，几何全在 `rect` 里。 */
export interface NearCandidate {
  id: string;
  rect: DropRect;
}

export interface NearHit {
  id: string;
  /** 与 `DropHit.nearness` 同一把尺子：`0` 正中、`1` 正好在命中区边界上、`>1` 在区外。 */
  nearness: number;
}

/**
 * 空白处落点时**最近的**一个节点——"最近的"必须回答清楚，否则双击空白加节点这件事
 * 在新节点出现之前是不可预测的。
 *
 * 三个候选口径，选第三个：
 *
 *  1. **欧氏距离**（到框的边或到中心）：盒子的宽高比是 180:48，同一份"绝对像素"
 *     在水平方向与垂直方向上的含义差近四倍。欧氏距离于是几乎只听水平分量——竖直
 *     方向排得再近也算"远"，而兄弟轴恰好是竖直的。实测后果是：右上角双击会挂到
 *     右边很远的那个节点下面，而不是正上方那个。
 *  2. **圆心距离**：同上，只差一个把盒子当圆的近似。
 *  3. **命中区那套归一化度量**（这里）：先除以 `半宽 + PAD_X` / `半高 + PAD_Y`，
 *     两轴在"几个盒子的尺度上"同权。于是"往上偏一个兄弟间距"与"往右偏一层间距"
 *     可以比较，而这两个间距正是用户眼睛里真实的那两把尺子。
 *
 * 与 `dropHitOf` 只差一条：**不截断**。空白处的定义就是"椭圆之外"，那条 `> 1` 的
 * 判据在这里必须去掉，否则"最近的节点"在空白处恒为 `null`。
 */
export function nearestHitOf(
  pointer: DropPointer,
  candidates: readonly NearCandidate[],
  padX: number = DROP_PAD_X,
  padY: number = DROP_PAD_Y,
): NearHit | null {
  let best: NearHit | null = null;
  for (const candidate of candidates) {
    const at = normalizedOf(pointer, candidate.rect, padX, padY);
    if (!at) continue;
    if (!best || at.nearness < best.nearness) {
      best = { id: candidate.id, nearness: at.nearness };
    }
  }
  return best;
}
