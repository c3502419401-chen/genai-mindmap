import type { MindNode } from '../api/types';
import {
  LIMITS,
  findNode,
  findParent,
  moveNode,
  reparentAbove,
  removeNode,
} from './edits';

/**
 * 拖动一个节点落到另一个节点上时，用户想表达的**结构关系**。
 *
 * 四档，不是"四个方向"：方向是屏幕上的，关系是树上的，两者之间的换算是
 * `canvas/layout/drop-zone.ts::dropZoneOf`。这一层只认关系——于是"左父右子"
 * 换成垂直布局（"上父下子"）时，本文件一行都不用改。
 *
 * 命名取**被拖节点相对目标节点的角色**："`parent`"读作"我要当目标的父"，
 * 不是"我要拖到父的位置"。这与用户的原话一致（"假如要成为一个节点的父或子"），
 * 也是唯一一种不需要再翻译一次的说法。
 */
export type DropZone = 'parent' | 'child' | 'sibling-before' | 'sibling-after';

/** 一次落点意图：谁、落到谁身上、什么关系。 */
export interface DropIntent {
  draggedId: string;
  targetId: string;
  zone: DropZone;
}

/**
 * 落点为什么被拒。**每一种都有独立文案**：用户拖了一下什么都没发生，与"拖了一下
 * 弹出一句说清原因的话"是两种体验，而这条路径上"什么都没发生"的成因有六种。
 */
export type DropRejectionCode =
  | 'self'
  | 'descendant'
  | 'root-moved'
  | 'root-as-child'
  | 'root-as-sibling'
  | 'no-change'
  | 'too-deep'
  | 'missing';

export interface DropPlanOk {
  ok: true;
  /** 改动后的新树（原树不动）。 */
  root: MindNode;
  /** 落点父节点 id，以及落进去之后的下标——文案与测试都要能读到它。 */
  parentId: string;
  index: number;
}

export interface DropPlanRejected {
  ok: false;
  code: DropRejectionCode;
  /** 直接给用户看的那句话。 */
  reason: string;
}

export type DropPlan = DropPlanOk | DropPlanRejected;

/** 这棵子树有几层（含自身）。 */
export function subtreeHeight(node: MindNode): number {
  if (node.children.length === 0) return 1;
  return 1 + Math.max(...node.children.map(subtreeHeight));
}

function shortLabel(node: MindNode): string {
  const chars = Array.from(node.label.trim());
  return chars.length > 12 ? `${chars.slice(0, 12).join('')}…` : chars.join('');
}

/**
 * 这次 drag-and-drop 该不该落到 `content` 上，落到哪里，落完之后是哪棵树。
 *
 * **纯函数**：不读 store、不碰 content，只吃一棵树 + 一个意图。三条硬逻辑（非法
 * 情形、环、深度）全在这里，所以它们能用工学用例逐条钉住，而不必在浏览器里
 * 拖着鼠标试。
 *
 * 判据的推导顺序不是随意的，它按"用户最想知道的答案"排：
 *
 *   1. **自环**（拖到自己）—— 手势都没落在别处，最先说；
 *   2. **拖到自己的后代上** —— 唯一会产生环的一种，必须在构造任何新树之前挡住；
 *   3. **根的两条特例** —— 根不能被拖走、也不能被塞进别人下面（否则它不再是根）；
 *   4. **无变化** —— 手势合法但结果与现状相同，不该记一步撤销；
 *   5. **深度上限** —— 只有前三关都过了才值得算新树的高度。
 *
 * 「拖到自己的父上」在**结构上**并不特殊：`zone='child'` 时它是第 4 条的"无变化"
 * （已经挂在那儿了），`zone='sibling-*'` 时它是合法的"提升一级"，
 * `zone='parent'` 时它是合法的"把父顶下去当自己的子"。三种都不是错误——把这三种
 * 统一当成"非法"会让用户最自然的几个整理动作被无故拒绝。
 */
export function planDrop(root: MindNode, intent: DropIntent): DropPlan {
  const { draggedId, targetId, zone } = intent;

  if (draggedId === targetId) {
    return { ok: false, code: 'self', reason: '不能拖到自己身上' };
  }

  const dragged = findNode(root, draggedId);
  const target = findNode(root, targetId);
  if (!dragged || !target) {
    return { ok: false, code: 'missing', reason: '节点不存在（可能已经被删掉了）' };
  }
  if (draggedId === root.id) {
    return { ok: false, code: 'root-moved', reason: '中心主题不能移动；它固定在这张图的最外层' };
  }

  // 环：目标在自己这支里（含目标就是自己，上面已经拦了）。这是**唯一**一种
  // 会把树变成图的操作，所以它是唯一一条"必须在构造新树之前"挡住的判据。
  if (findNode(dragged, targetId)) {
    return {
      ok: false,
      code: 'descendant',
      reason: `「${shortLabel(target)}」在它自己这一支里，挂上去会绕成一个环`,
    };
  }

  const targetParent = findParent(root, targetId);

  if (zone === 'parent' && !targetParent) {
    // target 是根 → 拖动节点会成为新的中心主题。那是**换整张图的根**，不是
    // 调整某一支，而手势里读不出"旧根该去哪"。宁可拒绝，也不猜。
    return {
      ok: false,
      code: 'root-as-child',
      reason: '中心主题不能成为别人的子节点',
    };
  }
  if (zone !== 'parent' && !targetParent && zone !== 'child') {
    // target 是根 → 根没有兄弟。
    return {
      ok: false,
      code: 'root-as-sibling',
      reason: '中心主题没有兄弟节点；把节点拖到它的右边可以挂成一级分支',
    };
  }

  /**
   * 落点父节点与下标。四档的算法各不相同，唯一的共同点是**下标都在"摘掉被拖节点
   * 之后"的兄弟数组里算**：先摘后插，否则兄弟同层移动时下标会自己错位一格。
   */
  let parentId: string;
  let index: number;
  let next: MindNode;

  if (zone === 'child') {
    parentId = targetId;
    index = target.children.length;
    // 无变化：已经挂在它下面、而且是最后一个孩子——拖过去什么都不会变。
    if (draggedId === target.children[target.children.length - 1]?.id) {
      return { ok: false, code: 'no-change', reason: `它已经是「${shortLabel(target)}」的最后一个子节点` };
    }
    next = moveNode(root, draggedId, parentId, index);
  } else if (zone === 'parent') {
    // 「我当 target 的父」：target 那一支挂到我的 children 末尾，我顶替它在
    // 它父节点下的位置。位置感因此与手势一致——拖到左边，它就出现在目标原来的
    // 那一格上，而目标被挤到它的右边一层。
    parentId = targetParent!.id;
    const detached = removeNode(root, draggedId);
    index = findParent(detached, targetId)!.children.findIndex(child => child.id === targetId);
    next = reparentAbove(root, draggedId, targetId);
  } else {
    parentId = targetParent!.id;
    const detached = removeNode(root, draggedId);
    const siblings = findParent(detached, targetId)!.children;
    const at = siblings.findIndex(child => child.id === targetId);
    index = zone === 'sibling-before' ? at : at + 1;

    // 无变化：已经紧挨在目标的前 / 后一位。
    const currentParent = findParent(root, draggedId);
    if (currentParent?.id === parentId) {
      const own = currentParent.children.findIndex(child => child.id === draggedId);
      if (zone === 'sibling-before' && own + 1 === at) {
        return { ok: false, code: 'no-change', reason: '它已经排在那个位置了' };
      }
      if (zone === 'sibling-after' && own === at + 1) {
        return { ok: false, code: 'no-change', reason: '它已经排在那个位置了' };
      }
    }
    next = moveNode(root, draggedId, parentId, index);
  }

  // 深度：**构造完再量**，而不是按公式推。公式要分别处理四档（其中 `parent`
  // 那档的子树高度会因为多了一层 target 而变化），而四份推导=四处可以写错的地方；
  // 量一遍是 O(节点数)，与这次拖动已经付掉的代价同一量级。
  const deepest = subtreeHeight(next);
  if (deepest > LIMITS.depth) {
    return {
      ok: false,
      code: 'too-deep',
      reason: `拖过去这一支就有 ${deepest} 层了，导图最多 ${LIMITS.depth} 层`,
    };
  }

  return { ok: true, root: next, parentId, index };
}

/**
 * 松手之后那句状态文案。
 *
 * 单独一个函数，因为**四个方向的说法必须互不相同**：如果四种结果都回一句
 * "已移动"，用户就没法从反馈里确认自己刚才做的是哪一种，而"拖错的成本"在画布上
 * 完全靠这句话兜底。
 */
export function dropMessage(root: MindNode, intent: DropIntent, plan: DropPlanOk): string {
  const dragged = findNode(root, intent.draggedId);
  const target = findNode(root, intent.targetId);
  const who = dragged ? `「${shortLabel(dragged)}」` : '节点';
  const at = target ? `「${shortLabel(target)}」` : '目标';
  switch (intent.zone) {
    case 'child':
      return `已把${who}挂为${at}的子节点，可撤销。`;
    case 'parent':
      return `已把${who}升为${at}的父节点，可撤销。`;
    case 'sibling-before':
      return `已把${who}插到${at}前面（兄弟），可撤销。`;
    case 'sibling-after':
      return `已把${who}插到${at}后面（兄弟），可撤销。`;
    default:
      return `已移动${who}，可撤销（第 ${plan.index + 1} 位）。`;
  }
}

/**
 * 拖动**还没松手**时，跟着光标的那句话。
 *
 * 与 `dropMessage` 是同一套措辞的将来式，所以两者放在同一个文件里：分开放的话，
 * 某一天会出现"拖的时候说'插到前面'、松手之后说'已插到后面'"这种自相矛盾的
 * 一对，而那种 bug 只有在真机上手动对比才看得见。
 *
 * 措辞一律用**关系**（"成为 A 的父节点"），不用方向（"放到左边"）：方向只是手势，
 * 用户真正要确认的是将要发生的结构变化。「左边是父」这条约定要在松手之前就被
 * 翻译掉，否则它就成了用户必须记住的一条规则而不是一个可验证的预演。
 */
export function dropPendingLabel(targetLabel: string, zone: DropZone): string {
  const at = `「${targetLabel}」`;
  switch (zone) {
    case 'child':
      return `成为${at}的子节点`;
    case 'parent':
      return `成为${at}的父节点`;
    case 'sibling-before':
      return `插到${at}前面（兄弟）`;
    case 'sibling-after':
      return `插到${at}后面（兄弟）`;
    default:
      return `移动到这里`;
  }
}
