import type { DiagramContent } from '../api/types';
import {
  containerNoun,
  findIn,
  isContainerRoot,
  kindOf,
  isTree,
  containerOfNode,
  type ShapeKind,
} from './shape';

/**
 * 拖动改结构的**作用域闸门**：这条手势改的是"树里的关系"，还是"容器之间的关系"。
 *
 * ## 为什么需要它（流程图 / 鱼骨图适配的那道门槛）
 *
 * 树只有一块区域，任何落点都在同一棵树里，`planDrop` 一条判据就够。另外两种图型
 * 不一样：它们的顶层是**并列的容器**（流程的 `stages[i]`、鱼骨的 `bones[i]`），
 * 而画布上喂给布局的是一棵**合成树**（虚拟根 + 每个容器一个一级子节点）。
 * 于是同一个落点手势有两种完全不同的含义：
 *
 * | 落点 | 含义 | 该怎么做 |
 * | --- | --- | --- |
 * | 同一个容器内 | 这一阶段的步骤换个次序 / 换个父步骤；这根骨上的原因换排位 | **允许**，就是树内的那次 `planDrop` |
 * | 另一个容器 | 这一步改属于**哪个阶段** / 这条原因改属于**哪根骨** | **拒绝**（见下） |
 * | 容器根那一层（虚拟根下） | 换阶段次序 / 换骨的次序 | **拒绝**（见下） |
 *
 * ## 跨容器为什么不做
 *
 * 三条，按硬度排：
 *
 * 1. **流程图的阶段内部边会悬空。** `FlowStage.edges` 的端点是**节点 id**（与
 *    `FlowContent.edges` 的阶段 id 是两个命名空间），契约层硬拒 dangling。把一个
 *    步骤拖进另一个阶段，它身上那几条「是 / 否」出边就留在了原阶段里——端点已不在
 *    那个阶段，下一次保存 422。而用户只是拖了一个框。
 *    （同一条理由的镜像在 `dropDanglingEdges`：删节点时连带清边，就是为了不留下
 *    这种边。拖动没有"删"这一步，所以只能从源头挡。）
 * 2. **阶段次序是流程语义，不是排版。** `layoutFlow` 的阶段先后来自前序位次
 *    （`order`），而那个顺序是生成时按材料定的执行顺序。拖动改它，等于改**流程
 *    本身**——那不是"整理一下画布"能换来的东西。鱼骨的六类同理：类别是这张图的
 *    骨架（`FishboneBone.category`），"把这个原因归到人/机/料哪一类"是要看内容的
 *    判断，不该由一次鼠标轨迹猜出来。
 * 3. **手势里读不出意图。** 把一条原因拖到另一根骨旁边，可能想说"换个归类"，也
 *    可能只是"这根骨太挤，我想把它挪远一点"。树里两种解释都通向同一个结构改动，
 *    这里不是——猜错的代价是一次静默的语义修改。
 *
 * 所以本轮的结论是**部分支持**：容器内的重排（换位次、换父步骤）放开，跨容器明确
 * 拒绝并给出原因。`docs/DRAG-ROUND3.md` §二 记了这套判断的完整论证。
 *
 * ## 为什么是"目标节点的容器"而不是"落点父节点的容器"
 *
 * `planDrop` 的四档里，落点父要么是目标自己（`child`），要么是目标的父（其余三档）。
 * 两者都在目标的容器里——**除非目标是容器根**（它的父在合成树上，是那个不在
 * content 里的虚拟根）。所以"目标在哪个容器"这一个问题就够，再加上"目标不能是容器根"
 * 那一条特例。多算一次落点父只会让两个判据有分叉的机会。
 */
export function moveScopeProblem(
  content: DiagramContent,
  draggedId: string,
  targetId: string,
): string | null {
  const kind = kindOf(content);
  // 树只有一块区域：这一层对树恒为放行，树的行为一个字节都不变。
  if (isTree(content)) return null;

  const dragged = findIn(content, draggedId);
  const target = findIn(content, targetId);
  // 节点不存在交给 `planDrop` 的 `missing` 去说（那一步的文案更准：可能刚被删掉）。
  if (!dragged || !target) return null;

  if (isContainerRoot(content, draggedId)) {
    return `${containerNoun(kind)}本身不能移动；它下面挂的内容可以挪。`;
  }
  if (isContainerRoot(content, targetId)) {
    return `${containerNoun(kind)}本身不能当落点：把节点拖到另一${containerNoun(kind)}上等于换归属，`
      + '那要按内容判断，不能由一次拖动决定。';
  }

  const from = containerOfNode(content, draggedId);
  const to = containerOfNode(content, targetId);
  if (!from || !to) return null;
  if (from.id === to.id) return null;

  return crossContainerReason(kind, from.label, to.label);
}

/** 跨容器那句原因。三种图型各一套措辞——同一句话在流程图上说不通。 */
function crossContainerReason(kind: ShapeKind, fromLabel: string, toLabel: string): string {
  const at = `「${toLabel}」`;
  if (kind === 'flowchart') {
    return `「${fromLabel}」里的步骤不能直接拖进${at}：这一步身上那些「是 / 否」判定出边`
      + '会留原阶段里、成为悬空边。要改归属，先在目标阶段里加一个步骤，再把原来的删掉。';
  }
  if (kind === 'fishbone') {
    return `「${fromLabel}」上的原因不能直接拖到${at}：鱼的类别是这张图的骨架，`
      + '换归类要看内容判断。要改归属，请让模型改，或删掉它再在目标那一支加一个。';
  }
  return `不能把它拖到${at}下面。`;
}
