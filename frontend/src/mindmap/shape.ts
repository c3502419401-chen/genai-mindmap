import diagramLabels from '../api/diagram-labels.json';
import type {
  DiagramContent,
  FishboneContent,
  FlowContent,
  MindEdge,
  MindMapContent,
  MindNode,
  NodeColor,
} from '../api/types';

/**
 * 三种 `content` 结构的**读侧**适配层。这一层是 `backend/app/agent/shapes.py` 的镜像，
 * 不是重新发明：那边已经回答过"为什么不做整体转树"，这里是同一个结论的另一半。
 *
 * ## 为什么前端要这一层
 *
 * 契约层有三种顶层形状（`MindMapContent.root` / `FishboneContent.bones[i].root` /
 * `FlowContent.stages[i].root`），而界面上每一个消费点原先都写死了 `content.root`——
 * 渲染、遍历、查找、编辑、撤销、保存全部假设"导图是一棵树"。阶段 2 之后库里真的
 * 会有另外两种形状，那些点必须知道"节点藏在哪儿"。
 *
 * 两条路摆在面前，判据和后台那边一样是**可逆性需求**：
 *
 *  - **画布渲染必须转**（并且只转一次、单向）：布局函数（`layout/tidy.ts` /
 *    `layout/fishbone.ts` / `layout/flow.ts`）全部是"吃一棵树、读 `root.children`
 *    当最外层内容"，树的坐标还有几十条用例钉着逐像素的值。把它们改成吃容器数组，
 *    等于把三套几何各重写一遍，而"树不改一个像素"这条硬约束会立刻破掉。所以渲染
 *    走 `canvasTreeOf()`：树**原样返回同一个对象**（连包装都不加），鱼骨/流程拼一棵
 *    "虚拟根 + 每个容器作为一级子节点"的合成树喂给同一批布局函数。
 *  - **编辑不能转**：`bones[i].category` 是**类别**、`stages[i].id` 是**另一个命名
 *    空间**（`FlowStage` 的注释写明阶段 id 只被 `FlowContent.edges` 引用），这两件
 *    事在 `MindNode` 里没有任何字段能承载。要转成树再转回来，就得在树之外另存一份
 *    "谁是骨、谁是阶段"的映射——同一份信息两份表示，不同步的表现是"骨少了一根"
 *    这种静默错误。所以写操作也走"按 id 找到它所在的那棵子树、把子树当根改、再写回
 *    原位"（`replaceContainer`），与后端 `shapes.replace_node` 逐条对应。
 *
 * ## 一条必须记住的边界
 *
 * 这一层只保证"**能定位、能改、改完还是原来那种图**"。三套结构各自的语义（骨要不要
 * 重排、阶段之间要不要串线、能不能新增一根骨）是阶段 3 的事，这里一条都不做。
 */

/** 三套结构的短名。与后端 `shapes.StructureKind` 同值。 */
export type ShapeKind = 'tree' | 'fishbone' | 'flowchart';

/**
 * 合成根的 id：鱼骨的头、流程的起点。
 *
 * 它**不在 content 里**，只在画布上存在。取值要满足两件事：能当 `MindNode.id` 用
 * （契约上限 100 字，这个长度没问题），且不可能与真实 id 撞车——真实 id 由后端
 * 规范化成 uuid / `n_` 前缀，两侧都不可能出现这串带下划线的句子。
 */
export const CANVAS_ROOT_ID = '__canvas_root__';

/**
 * 画布上的一块区域：树的 `root` / 鱼骨的一根骨 / 流程的一个阶段。
 *
 * `id` 的命名空间跟着**这块区域自己**：树与鱼骨给节点 id（骨就是那个 `Node`，与后端
 * `_top_level_rows` 同口径），流程给**阶段 id**（`s_…`）——阶段 id 才是
 * `FlowContent.edges` 的端点，给节点 id 会让读它的人去连一条连不上的线。
 */
export interface ShapeContainer {
  id: string;
  /** 显示名。鱼骨取骨根节点的标题（画布上画的就是它），`category` 另存一份。 */
  label: string;
  /** 容器内部那棵子树。**改它 = 改这块区域的内容**。 */
  root: MindNode;
  /** 只有鱼骨有：`FishboneBone.category`。树与流程为 `null`。 */
  category: string | null;
  color: NodeColor | null;
  icon: string | null;
}

/** 扁平化之后的一个节点，带它属于哪块区域。 */
export interface ShapeNode {
  node: MindNode;
  /** 所属容器的 id（见 `ShapeContainer.id`）。 */
  containerId: string;
  /** 层号，**按容器自己的根从 1 数**（与契约 `_scan_tree` 同一条口径）。 */
  depth: number;
}

/** 一份内容被抹平形状之后的样子。字段名与任务书里的 `ShapeView` 一致。 */
export interface ShapeView {
  kind: ShapeKind;
  /** 顶层容器：树的 root / 鱼骨的 bones / 流程的 stages。树只有一个。 */
  containers: ShapeContainer[];
  /** 所有节点（含各容器的根），按画布上的阅读顺序。 */
  nodes: ShapeNode[];
  /** 跨容器/阶段的关系。鱼骨没有这个字段，恒为空数组。 */
  edges: MindEdge[];
}

// ---------------------------------------------------------------- 判别：按形状，不看字段

/**
 * 三个判别都是 `in` 检查，读的是**形状**而不是 `diagram_type`。
 *
 * 与后端 `shapes.kind_of` 同一条理由：库里存在"标着 fishbone、形状是树"的历史记录
 * （阶段 2 之前生成侧就是这么产出的）。拿 `diagram_type` 做分派会让那些图走进不存在
 * 的分支——最直接的表现是"打开旧图，画布变了"。
 */
export function isTree(content: DiagramContent): content is MindMapContent {
  return 'root' in content;
}

export function isFishbone(content: DiagramContent): content is FishboneContent {
  return 'bones' in content;
}

export function isFlow(content: DiagramContent): content is FlowContent {
  return 'stages' in content;
}

/** 节点在图里扮演什么。与 `MindNode.role` 同值（`null` = 普通步骤）。 */
export type NodeRole = NonNullable<MindNode['role']>;

/** 画布上这个节点该长什么样。缺省 `rect` 就是加形状之前的样子。 */
export type NodeShape = 'rect' | 'diamond' | 'pill' | 'parallelogram' | 'cylinder';

/**
 * `role` → 画布形状。**只有流程图会走到这里**（判据在 `stores/mindmap.ts`：布局
 * 结果里有没有 `sizes`）。树与鱼骨的节点即使带着 `role` 也一律 `rect`——那不是
 * 它们的语义，而且「打开旧图，画布变了」是这个项目里最不能犯的错。
 *
 * 判定画菱形是教科书约定：菱形本身就说明"这里有两条以上的出口"。`start` / `end`
 * 画成胶囊（大圆角），但不画菱形——它们是**端点**，不是分叉点，两者在流程图上
 * 共用一种形状会让读图的人以为起点也要选一条路走。
 *
 * `data` 是平行四边形、`database` 是圆柱：这两个是"数据流"那一族，与"干活"那一族
 * （矩形 / 菱形 / 胶囊）在轮廓上就能分开——斜边和圆柱的顶面弧线都是**一眼可辨**
 * 的方向性特征，不像"再圆一点的矩形"那种要靠比较才看得出来。
 */
export function nodeShapeOf(role: NodeRole | null | undefined): NodeShape {
  if (role === 'decision') return 'diamond';
  if (role === 'start' || role === 'end') return 'pill';
  if (role === 'data') return 'parallelogram';
  if (role === 'database') return 'cylinder';
  return 'rect';
}

export function kindOf(content: DiagramContent): ShapeKind {
  if (isFishbone(content)) return 'fishbone';
  if (isFlow(content)) return 'flowchart';
  return 'tree';
}

/** 这块区域在界面文案里叫什么。鱼骨的"骨"、流程的"阶段"、树的"一级分支"。 */
export function containerNoun(kind: ShapeKind): string {
  if (kind === 'fishbone') return '骨';
  if (kind === 'flowchart') return '阶段';
  return '一级分支';
}

/**
 * 这种形状在界面文案里叫什么图。
 *
 * **不手写**：值来自生成物 `api/diagram-labels.json`，与图形类型下拉
 * （`stores/options.ts`）用的是同一个文件。那份生成物由
 * `backend/scripts/export_diagram_labels.py` 从契约 `DIAGRAM_LABELS` 导出，
 * 后端 `tests/test_diagram_options.py` 钉住"生成物没过期"。此前这里是三个手写
 * 字面量，改一个中文名会让下拉跟着变、导入结果面板不变，而且两边都不红。
 *
 * 为什么这个函数仍然按形状取值（这条设计不变）：面板那句"已导入一张 ___"的文案
 * 只能来自形状判别（`kindOf()`），不能来自 `diagram_type` 字段——库里存在"标着
 * fishbone、形状是树"的历史记录，拿字段分派会让旧图走进不存在的分支。**判据依赖
 * 形状，取值不必依赖手抄**，两件事分开之后这个函数就是一次索引。
 *
 * 键集同源：`ShapeKind` 与生成物的键都出自后端 `DiagramKind` 那个 Literal，所以
 * `diagramLabels[kind]` 是同一个键，不是两次命名撞车。将来加第四种形状时，这里是
 * 一次 `npm run type-check` 错误（生成物里还没那个键），而不是"静默返回思维导图"
 * ——后者正是这份手抄副本最坏的失效形态。
 *
 * 守卫：`backend/tests/test_shared_constants.py::test_shape_label_reads_the_generated_labels`
 * 读本文件，断言函数体里索引生成物、且不含那三个中文字面量。
 */
export function shapeLabel(kind: ShapeKind): string {
  return diagramLabels[kind];
}

// ---------------------------------------------------------------- 读：容器、遍历、计数

/** 各子树自己的根，按画布上的阅读顺序（树一根、鱼骨六根、流程若干阶段）。 */
export function rootsOf(content: DiagramContent): MindNode[] {
  if (isFishbone(content)) return content.bones.map(bone => bone.root);
  if (isFlow(content)) return content.stages.map(stage => stage.root);
  return [content.root];
}

/**
 * 顶层容器列表。
 *
 * 树的 `label` 取中心主题的标题：画布上那一块就是它，界面上"有 6 个一级分支"
 * 之类的文案也按它数。
 */
export function containersOf(content: DiagramContent): ShapeContainer[] {
  if (isFishbone(content)) {
    return content.bones.map(bone => ({
      id: bone.root.id,
      label: bone.root.label.trim() || bone.category,
      root: bone.root,
      category: bone.category,
      color: bone.color ?? bone.root.color ?? null,
      icon: bone.icon ?? bone.root.icon ?? null,
    }));
  }
  if (isFlow(content)) {
    return content.stages.map(stage => ({
      id: stage.id,
      label: stage.root.label.trim(),
      root: stage.root,
      category: null,
      color: stage.root.color ?? null,
      icon: stage.root.icon ?? null,
    }));
  }
  return [{
    id: content.root.id,
    label: content.root.label.trim(),
    root: content.root,
    category: null,
    color: content.root.color ?? null,
    icon: content.root.icon ?? null,
  }];
}

/** 前序遍历一棵子树（父在子前）。 */
export function* walkNodes(root: MindNode): Generator<MindNode> {
  const stack: MindNode[] = [root];
  while (stack.length > 0) {
    const node = stack.pop()!;
    yield node;
    for (let index = node.children.length - 1; index >= 0; index -= 1) {
      stack.push(node.children[index]);
    }
  }
}

/** 遍历这份内容的**全部**节点，多棵并列子树按 `rootsOf()` 的顺序相接。 */
export function* iterNodesIn(content: DiagramContent): Generator<MindNode> {
  for (const root of rootsOf(content)) yield* walkNodes(root);
}

export function countNodesIn(content: DiagramContent): number {
  let total = 0;
  for (const _node of iterNodesIn(content)) total += 1;
  return total;
}

export function findIn(content: DiagramContent, nodeId: string): MindNode | null {
  for (const node of iterNodesIn(content)) {
    if (node.id === nodeId) return node;
  }
  return null;
}

/**
 * 按 id 找父节点。**容器根没有父节点**（返回 `null`），哪怕它在画布上挂在虚拟根 / 主骨
 * 上——那层挂载关系由 `FishboneBone` / `FlowStage` 表达，不是 `MindNode` 的父子关系。
 * 与后端 `shapes.find_parent` 同一条口径：把它当成父节点会让"移到某个容器根下面"
 * 变成一个不存在的操作。
 */
export function findParentIn(content: DiagramContent, nodeId: string): MindNode | null {
  for (const node of iterNodesIn(content)) {
    for (const child of node.children) {
      if (child.id === nodeId) return node;
    }
  }
  return null;
}

/** `容器根 → … → 目标节点` 的节点链；找不到返回 `null`。 */
export function pathToIn(content: DiagramContent, nodeId: string): MindNode[] | null {
  for (const root of rootsOf(content)) {
    const path = findPathWithin(root, nodeId);
    if (path) return path;
  }
  return null;
}

function findPathWithin(root: MindNode, nodeId: string): MindNode[] | null {
  const path: MindNode[] = [];
  const walk = (node: MindNode): boolean => {
    path.push(node);
    if (node.id === nodeId) return true;
    for (const child of node.children) {
      if (walk(child)) return true;
    }
    path.pop();
    return false;
  };
  return walk(root) ? path : null;
}

/** 层号：按它所在容器的根从 1 数。找不到返回 `null`。 */
export function depthIn(content: DiagramContent, nodeId: string): number | null {
  for (const container of containersOf(content)) {
    const path = findPathWithin(container.root, nodeId);
    if (path) return path.length;
  }
  return null;
}

/** 这个节点属于哪块区域。找不到返回 `null`。 */
export function containerOfNode(content: DiagramContent, nodeId: string): ShapeContainer | null {
  for (const container of containersOf(content)) {
    for (const node of walkNodes(container.root)) {
      if (node.id === nodeId) return container;
    }
  }
  return null;
}

/**
 * 这个节点是不是**某块区域的根**（树的中心主题、一根骨、一个阶段的入口）。
 *
 * 与后端 `tools._is_container_root` 同一个问题、同一个答案：它们删不得、也移不走。
 * 这里不做"是不是最后一个阶段"之类的三套语义判断，只回答结构问题。
 */
export function isContainerRoot(content: DiagramContent, nodeId: string): boolean {
  return rootsOf(content).some(root => root.id === nodeId);
}

/** 抹平之后的完整视图。 */
export function viewOf(content: DiagramContent): ShapeView {
  const containers = containersOf(content);
  const nodes: ShapeNode[] = [];
  for (const container of containers) {
    for (const node of walkNodes(container.root)) {
      const path = findPathWithin(container.root, node.id);
      nodes.push({ node, containerId: container.id, depth: path ? path.length : 1 });
    }
  }
  return {
    kind: kindOf(content),
    containers,
    nodes,
    edges: edgesOfContent(content),
  };
}

/**
 * 附加边，按"这份内容自己的边字段"读。
 *
 * 鱼骨的顶层结构里根本没有 `edges`（只有 `relations`）：给鱼骨加一条附加边在契约上
 * 无处安放，所以这里返回空数组而不是抛错——画布少画一条线好过整页白屏。
 */
export function edgesOfContent(content: DiagramContent): MindEdge[] {
  if ('edges' in content) return content.edges ?? [];
  return [];
}

/**
 * **阶段内部**的附加边：`stages[i].edges` 这一批。
 *
 * 端点是**该阶段自己的节点 id**（与 `FlowContent.edges` 的阶段 id 是两个命名
 * 空间，见 `types.ts::FlowStage`）。它此前一处也读不到——不是画歪，是没有代码
 * 在找它：所有读边的地方都走 `edgesOfContent()`，而那个函数读的是
 * `content.edges`（阶段之间那一层）。
 *
 * **不改 `edgesOfContent()` 的语义**：它是"内容真相"，侧栏删除要靠它知道该删
 * 哪一层数组（阶段内的边删的是 `stage.edges`，阶段间的删 `content.edges`）。
 * 所以这里另起一个函数，让"阶段内"这一层有名字、有调用点、可数。
 */
export function stageInnerEdgesOf(content: DiagramContent): MindEdge[] {
  if (!isFlow(content)) return [];
  return content.stages.flatMap(stage => stage.edges ?? []);
}

/**
 * **画布上用的阶段间边**：流程图的阶段跳转要把端点换成"阶段入口节点"的 id。
 *
 * `FlowContent.edges` 的端点是**阶段 id**（`s_…`），而画布上所有坐标、句柄、连线
 * 都按**节点 id** 索引。不换的话有两个后果，都不是"少画一条线"那么轻：
 *
 *  1. 这条线在整个画布链路上被跳过（`attachEdges` 拿不到端点坐标）；
 *  2. `layoutFlow` 的"边参与分层"对它失效——`order.has('s_1')` 恒为 false，
 *     于是一条"形式审查 → 付款"的依赖对排版没有任何影响，流程图分层退化成纯树形。
 *
 * 换取的是"画布视角"的端点，**不是**内容的真相：`content.edges` 一个字节都不动，
 * 侧栏（`contentEdges` / `selectedEdges` / 删除）读的仍是阶段 id。边界就在这里——
 * 画布那一条路读 `canvasEdgesOf()`，其余全部读 `edgesOfContent()`。
 *
 * ## 为什么这一路单独存在（而不是并进 `canvasEdgesOf`）
 *
 * 因为**布局**要的正是"只有阶段间"这一路：`layoutFlow` 用 dagre 按边分层，一条
 * 阶段内的「是 / 否」出边（`order.has(node.id)` 为 true）会把目标步骤强行推到下一
 * rank，与 `Edge` 契约里"边不参与布局"直接冲突——加了边之后流程图的分层会变形，
 * 而且看起来"像是 dagre 该做的"。所以两路各有各的名字：布局吃这一个，画布吃
 * `canvasEdgesOf`（= 这一路 + 阶段内那一路）。
 */
export function canvasStageEdgesOf(content: DiagramContent): MindEdge[] {
  const edges = edgesOfContent(content);
  if (!isFlow(content)) return edges;
  const entry = new Map(content.stages.map(stage => [stage.id, stage.root.id]));
  const out: MindEdge[] = [];
  for (const edge of edges) {
    const source = entry.get(edge.source);
    const target = entry.get(edge.target);
    // 端点不是已知阶段：契约层会硬拒这种边（dangling），这里只是兜底跳过。
    if (!source || !target) continue;
    out.push({ ...edge, source, target });
  }
  return out;
}

/**
 * **画布上要画的全部边**：阶段间（上面那一路）+ 阶段内（原样追加）。
 *
 * 阶段内边的端点**本来就是节点 id**，所以不做任何映射。这里必须**追加**而不是
 * 丢进上面那个 `entry` 映射里：那段循环对"端点不是已知阶段"会 `continue`，阶段内
 * 边的端点永远不是阶段 id——直接拼进去会被静默吞掉，而表现正好是"契约上有了、
 * 视觉上完全没有"。
 *
 * 没有阶段内边时返回的就是 `canvasStageEdgesOf()` 的那个数组**本身**（树型下
 * 更是 `content.edges` 原对象）：不复制、不重排，树的行为一个字节都不变。
 */
export function canvasEdgesOf(content: DiagramContent): MindEdge[] {
  const outer = canvasStageEdgesOf(content);
  const inner = stageInnerEdgesOf(content);
  return inner.length === 0 ? outer : [...outer, ...inner];
}

/** 阶段 id → 那个阶段的根节点（只有流程有）。 */
export function stageOfNode(content: DiagramContent, nodeId: string): string | null {
  if (!isFlow(content)) return null;
  for (const stage of content.stages) {
    for (const node of walkNodes(stage.root)) {
      if (node.id === nodeId) return stage.id;
    }
  }
  return null;
}

/**
 * 边端点的显示名：先当节点找，再当阶段找。
 *
 * 两个命名空间混在一个列表里显示（阶段 id 只出现在流程的边上），所以这里按"哪个
 * 找到就用哪个"回退。都找不到就退回 id —— 界面上写一串 `s_xxx` 比写"未知"更容易
 * 排查，它至少指得回原始数据。
 */
export function edgeEndpointLabel(content: DiagramContent, id: string): string {
  const node = findIn(content, id);
  if (node) return node.label.trim();
  if (isFlow(content)) {
    const stage = content.stages.find(item => item.id === id);
    if (stage) return stage.root.label.trim();
  }
  return id;
}

// ---------------------------------------------------------------- 画布：合成树

/**
 * 喂给布局函数的那棵树。
 *
 * **树型原样返回 `content.root` 这一个对象**（不包装、不复制）：`layoutTidy(root)` 的
 * 入参与返回值因此一个字节都没变，几十条钉着坐标的用例不受影响。这条性质是整个
 * 改动里最重要的约束，写成注释而不是靠记性。
 *
 * 鱼骨与流程拼一棵合成树：虚拟根 + 每个容器作为一级子节点。理由不是"省事"，是
 * 三个布局函数**本来就长成这个样子**——`layoutFishbone` 把 `root.children` 当六根骨、
 * 把 `root` 自己当鱼头，`layoutFlow` 把 `root.children` 当阶段、把 `root` 当图的起点
 * 参与 dagre 分层。改成吃 `containers` 意味着这两处几何各重写一遍（鱼头、起点、
 * 骨线端点全都要重新定义"根是谁"），而"树不改一个像素"这条约束挡在所有重写前面。
 * 合成一棵树什么都不用改，代价只是画布上多一个**不在 content 里**的节点。
 */
export function canvasTreeOf(content: DiagramContent): { root: MindNode; synthetic: boolean } {
  if (isTree(content)) return { root: content.root, synthetic: false };
  const label = isFishbone(content)
    // 鱼头里那句才是这张图的论点（"为什么报销要 11 天"），`title` 是文件名。
    ? (content.problem?.trim() || content.title)
    : content.title;
  return {
    root: {
      id: CANVAS_ROOT_ID,
      label,
      /**
       * 合成根**不带 `role`**：它是这张图的标题，不是流程的端点。
       *
       * 曾经给它标过 `start`，让流程图顶端长成一个"开始"胶囊——但后端
       * `_stamp_flow_roles` 还会把**第一个阶段的根**也标成 `start`（"提交申请"），
       * 于是画布上并排出现两个一模一样深色的开始胶囊，读者分不出哪一个是图题。
       * 形状只认内容里的 `role`：合成根不在 content 里，就不该借一个角色来驱动视觉。
       *
       * 它仍然走 `depth === 0` 那一档样式（深色实底），与树根、鱼头同一套语言：
       * 三个形状的顶端都是"这张图叫什么"。
       */
      children: rootsOf(content),
    },
    synthetic: true,
  };
}

// ---------------------------------------------------------------- 写：按 id 就地替换

/**
 * 把 `nodeId` 所在的那棵子树当根改一遍，再写回原位。形状不变。
 *
 * `fn` 收到的是**容器自己的子树根**（不是被点名的那个节点）：调用方因此可以复用
 * `edits.ts` 里那一整套"吃根、返根"的纯函数（`renameNode` / `addChild` /
 * `removeNode` …），一行都不用重写。
 *
 * **找不到原样返回同一个对象**（引用相等）：调用方靠它区分"什么都没改"，跟后端
 * `shapes.replace_node` 同一条规矩——无谓的复制会让"这一批 op 没命中"看起来像改过。
 */
export function replaceContainer(
  content: DiagramContent,
  nodeId: string,
  fn: (root: MindNode) => MindNode,
): DiagramContent {
  if (isFishbone(content)) {
    const index = content.bones.findIndex(bone => containsNode(bone.root, nodeId));
    if (index < 0) return content;
    return {
      ...content,
      bones: content.bones.map((bone, at) => (
        at === index ? { ...bone, root: fn(bone.root) } : bone
      )),
    };
  }
  if (isFlow(content)) {
    const index = content.stages.findIndex(stage => containsNode(stage.root, nodeId));
    if (index < 0) return content;
    return {
      ...content,
      stages: content.stages.map((stage, at) => (
        at === index ? { ...stage, root: fn(stage.root) } : stage
      )),
    };
  }
  if (!containsNode(content.root, nodeId)) return content;
  return { ...content, root: fn(content.root) };
}

function containsNode(root: MindNode, nodeId: string): boolean {
  for (const node of walkNodes(root)) {
    if (node.id === nodeId) return true;
  }
  return false;
}

/**
 * 按顺序把新的子树根写回容器（`rootsOf` 的顺序）。
 *
 * 长度对不上时原样返回：那说明调用方换了容器的个数（新增/删除一根骨、一个阶段），
 * 而那是阶段 3 的语义。宁可这一次改动不落，也不要静默地把某根骨的内容搬到另一根
 * 骨下面——那种错位事后没人看得出来。
 */
export function withRoots(content: DiagramContent, roots: readonly MindNode[]): DiagramContent {
  const current = rootsOf(content);
  if (current.length !== roots.length) return content;
  if (isFishbone(content)) {
    return {
      ...content,
      bones: content.bones.map((bone, index) => ({ ...bone, root: roots[index] })),
    };
  }
  if (isFlow(content)) {
    return {
      ...content,
      stages: content.stages.map((stage, index) => ({ ...stage, root: roots[index] })),
    };
  }
  return { ...content, root: roots[0] };
}

/** 画布最外那一层**内容节点**：树是一级分支，鱼骨是六根骨，流程是各阶段。
 *
 * 与 `containersOf()` 的差别只在树：树的最外层内容节点是 `root.children`（中心主题
 * 是标题，不是一块内容），而鱼骨/流程的 `rootsOf()` 本身就是最外层内容。界面文案
 * 「N 个一级分支」按这一层数——数容器的话，一棵四支的导图会说"1 个一级分支"。
 * 与后端 `shapes.top_branches` 同一口径。
 */
export function topBranchesOf(content: DiagramContent): MindNode[] {
  if (isTree(content)) return content.root.children;
  return rootsOf(content);
}
