import type { MindNode } from '../api/types';
import {
  LIMITS,
  findNode,
  findParent,
  detachToRoot,
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
    // 判据不变，**文案给一条出路**：这句话是用户唯一能读到的反馈，而"最多 8 层"
    // 只描述了墙、没描述门。他能做的只有三件事（删浅一点 / 换个浅目标 / 先拆支），
    // 那就把最省事的那两件写出来——不写的话，同一个手势再试一次还是同一句话，
    // 用户会以为"拖到这个方向坏了"（写手册的人就是这么以为的，实测 2 次之后
    // 得出了"是不是不支持挂到第 2 层"的结论）。
    return {
      ok: false,
      code: 'too-deep',
      reason: `拖过去这一支就有 ${deepest} 层了，导图最多 ${LIMITS.depth} 层`
        + '——把这一支里最深的几层删掉，或者换一个浅一点的目标挂',
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

// ---------------------------------------------------------------- 断关系
//
// **拖到空白处**。这三档候选（拒绝 / 断成孤儿挂根下 / 游离节点）里选中间那一档，
// 论证写在 `docs/NODE-EDIT.md` §一，这里只留代码侧必须成立的那几条：
//
// 1. `content` 是一棵树，树里没有"无父节点"——除了根。所以"断成孤儿"唯一可落的地方
//    就是根的孩子列表，这不是妥协，是这棵树的定义决定的；
// 2. 它**不加深**任何节点：挂到根下一律是第 2 层，所以 `planDrop` 那条 `too-deep`
//    在这里没有对应物（反过来写一条深度判据只会是一个永远不触发的分支）；
// 3. 它**会减少**原来那个父节点的孩子数，也**会增加**根的孩子数——后者有契约上限
//    （`LIMITS.children`），是唯一需要挡的量。

export type DetachRejectionCode =
  /** 节点不存在（可能已经被删掉了）。 */
  | 'missing'
  /** 想断的是中心主题：它本来就没有父节点。 */
  | 'root-moved'
  /** 它已经挂在一级上了——"断关系"对它是一次无变化的动作。 */
  | 'already-branch'
  /** 中心主题下的一级分支数到契约上限了。 */
  | 'branch-cap';

export interface DetachPlanOk {
  ok: true;
  /** 改动后的新树（原树不动）。 */
  root: MindNode;
  /** 断开之前它挂在谁下面——界面那句"从「甲」下面断开"只有这里知道。 */
  formerParentId: string;
  /** 新位置：根的第几个孩子。 */
  index: number;
}

export interface DetachPlanRejected {
  ok: false;
  code: DetachRejectionCode;
  reason: string;
}

export type DetachPlan = DetachPlanOk | DetachPlanRejected;

/**
 * 拖到空白处松手：把 `nodeId` 从它父节点下面摘下来，挂成一级分支。
 *
 * **拒绝的两种情形必须是拒绝，不能是"重排"**：
 *
 *  - `already-branch`：它已经挂在中心主题下。此时唯一可能的结果是"挪到根的孩子列表
 *    末尾"——那是一次**悄悄的重排**，而用户做的手势是"拖到空白处"。他要是想调位次，
 *    拖到兄弟的上 / 下就有明确的落点预览；从"拖到空白"读不出"我要排到最后一位"。
 *  - `branch-cap`：契约的 `children ≤ 50` 是硬拒（后端 422），前端先拦一次。
 */
export function planDetach(root: MindNode, nodeId: string): DetachPlan {
  const node = findNode(root, nodeId);
  if (!node) {
    return { ok: false, code: 'missing', reason: '节点不存在（可能已经被删掉了）' };
  }
  if (nodeId === root.id) {
    return { ok: false, code: 'root-moved', reason: '中心主题没有父节点，无从断开' };
  }
  const parent = findParent(root, nodeId);
  if (!parent) {
    return { ok: false, code: 'missing', reason: '节点不存在（可能已经被删掉了）' };
  }
  if (parent.id === root.id) {
    return {
      ok: false,
      code: 'already-branch',
      reason: `「${shortLabel(node)}」已经是一级分支了，没有父子关系可断`,
    };
  }
  if (root.children.length >= LIMITS.children) {
    return {
      ok: false,
      code: 'branch-cap',
      reason: `中心主题下已经有 ${LIMITS.children} 个一级分支，到上限了`,
    };
  }

  const moved = detachToRoot(root, nodeId);
  return {
    ok: true,
    root: moved.root,
    formerParentId: moved.formerParentId,
    index: moved.index,
  };
}

/** 断开之后那句状态文案。**与拖动的四档措辞不同**，因为它改的是另一件事。 */
export function detachMessage(
  root: MindNode,
  nodeId: string,
  formerParentId: string,
  plan: DetachPlanOk,
): string {
  const node = findNode(root, nodeId);
  const parent = findNode(root, formerParentId);
  const who = node ? `「${shortLabel(node)}」` : '节点';
  const from = parent ? `「${shortLabel(parent)}」下面` : '原来的父节点下面';
  return `已把${who}从${from}断开，挂为一级分支（第 ${plan.index + 1} 位），可撤销。`;
}

/**
 * 拖着节点悬在空白处时，跟着光标的那句话。
 *
 * 与 `dropPendingLabel` 同一套"用关系措辞、不用方向措辞"的规矩。这里必须把**两端**
 * 都写出来（"从「甲」下面断开"），因为空白处没有任何视觉锚点告诉用户"会断掉哪条边"
 * ——那正是这一档最容易读成"它归位了"的地方。
 */
export function detachPendingLabel(nodeLabel: string, parentLabel: string): string {
  return `断开「${nodeLabel}」与「${parentLabel}」的关系 · 成为一级分支`;
}

// ---------------------------------------------------------------- 新增节点
//
// **双击空白**。位置由用户给（他点到哪就是哪），但坐标仍然只有布局一个来源，所以
// "在那儿"只能被翻译成一件事：**挂到谁下面、排第几**。距离最近的那个可见节点就是
// 用户心里的父（`canvas/layout/drop-zone.ts::nearestHitOf` 用与命中区同一套归一化
// 度量算出它）。
//
// 四档 zone 原样接收（与拖动同构），只有两处必须**降级**，因为新增一个节点不能顺带
// 重排已有结构：
//
//  - `parent`（我要当它的父）：一个尚未存在的新节点没法"收养"目标并把目标降到下一层
//    ——那是两次结构改动，而用户做的手势只是"在这里加一个"。所以落到目标**前面**
//    当兄弟（`sibling-before`），位置就在他点的那个方向、目标旁边。
//  - 目标就是根时的 `sibling-*`：根没有兄弟，落到根下面当一级分支（`child`）。

export interface InsertIntent {
  /** 离落点最近的那个节点（`zone` 的参照物）。 */
  targetId: string;
  zone: DropZone;
}

export type InsertRejectionCode = 'missing' | 'too-deep' | 'full';

export interface InsertPlanOk {
  ok: true;
  /** 新节点的父节点。 */
  parentId: string;
  index: number;
  /** 实际采用的落法；与传入的 `zone` 不同就说明降级过。 */
  zone: DropZone;
  /** 降级时那半句解释（给 `status` 拼在最前面）；没降级是 `null`。 */
  degraded: string | null;
}

export interface InsertPlanRejected {
  ok: false;
  code: InsertRejectionCode;
  reason: string;
}

export type InsertPlan = InsertPlanOk | InsertPlanRejected;

/** 某个节点在树上的层号，根是 1；找不到返回 `null`。 */
function depthOfNode(root: MindNode, id: string, depth = 1): number | null {
  if (root.id === id) return depth;
  for (const child of root.children) {
    const hit = depthOfNode(child, id, depth + 1);
    if (hit !== null) return hit;
  }
  return null;
}

/**
 * 双击落点 → 新节点挂在谁下面、排第几。**纯函数**，所以"降级"与"挡下"这两类判据
 * 能逐条钉住，不必在浏览器里双击。
 *
 * 判据顺序：先归一化 zone（降级是"换个落法"，不是失败），再查目标父节点的容量，
 * 最后查深度。深度放最后是因为它要的是**父节点**的层号，而父节点是哪一位取决于
 * 前面那一步。
 */
export function planInsert(root: MindNode, intent: InsertIntent): InsertPlan {
  const target = findNode(root, intent.targetId);
  if (!target) {
    return { ok: false, code: 'missing', reason: '落点附近没有可挂的节点' };
  }

  let zone = intent.zone;
  let degraded: string | null = null;

  if (zone === 'parent') {
    // 新节点收养不了目标；根更不能有父。
    zone = target.id === root.id ? 'child' : 'sibling-before';
    degraded = target.id === root.id
      ? '中心主题不能有父节点，改挂成一级分支'
      : '新节点只能插在「' + shortLabel(target) + '」前面当兄弟（不能把它变成自己的子节点）';
  } else if (zone !== 'child' && target.id === root.id) {
    zone = 'child';
    degraded = '中心主题没有兄弟，改挂成一级分支';
  }

  let parentId: string;
  let index: number;
  if (zone === 'child') {
    parentId = target.id;
    index = target.children.length;
  } else {
    // 走到这里 `target` 一定不是根（`parent` 那档已经降级，`sibling-*` 在根上已改成
    // `child`），所以父节点必然存在。仍显式判一次：`!` 断言在"降级表改了但这条没跟上"
    // 的那天会变成一个运行时崩。
    const parent = findParent(root, target.id);
    if (!parent) {
      parentId = target.id;
      index = target.children.length;
    } else {
      parentId = parent.id;
      index = parent.children.findIndex(child => child.id === target.id)
        + (zone === 'sibling-after' ? 1 : 0);
    }
  }

  const parent = findNode(root, parentId);
  if (!parent) {
    return { ok: false, code: 'missing', reason: '落点附近没有可挂的节点' };
  }
  if (parent.children.length >= LIMITS.children) {
    return {
      ok: false,
      code: 'full',
      reason: `「${shortLabel(parent)}」下已经有 ${LIMITS.children} 个子节点，到上限了`,
    };
  }
  const depth = (depthOfNode(root, parentId) ?? LIMITS.depth) + 1;
  if (depth > LIMITS.depth) {
    // 与 `planDrop` 的 `too-deep` 同一条规矩：说清墙在哪儿，也说清怎么绕过去。
    return {
      ok: false,
      code: 'too-deep',
      reason: `挂到「${shortLabel(parent)}」下就是第 ${depth} 层了，导图最多 ${LIMITS.depth} 层`
        + `——换一个浅一点的位置双击，或者先把这一支收短一点`,
    };
  }

  return { ok: true, parentId, index, zone, degraded };
}

/** 新增之后那句状态文案。四档措辞不同，理由同 `dropMessage`。 */
export function insertMessage(targetLabel: string, plan: InsertPlanOk): string {
  const at = `「${targetLabel}」`;
  const tail = '输入文字后按回车确认（可撤销）';
  const body = (() => {
    switch (plan.zone) {
      case 'child':
        return `已在${at}下面加了一个子节点`;
      case 'sibling-before':
        return `已在${at}前面加了一个兄弟节点`;
      case 'sibling-after':
        return `已在${at}后面加了一个兄弟节点`;
      default:
        return '已加一个节点';
    }
  })();
  return `${plan.degraded ? `${plan.degraded}；` : ''}${body}，${tail}。`;
}
