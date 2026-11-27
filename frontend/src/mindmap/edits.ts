import type { DiagramContent, EdgeKind, MindEdge, MindNode, NodeColor } from '../api/types';
import {
  containersOf,
  edgesOfContent,
  isFishbone,
  isFlow,
  iterNodesIn,
  rootsOf,
  stageInnerEdgesOf,
  stageOfNode,
  walkNodes,
} from './shape';

/**
 * 与服务端 Pydantic 约束逐条对应（`backend/app/contracts/mindmap.py`）：
 * 节点 id ≤100、label 1–200、children ≤50、总数 ≤1000、深度 ≤8、title 1–200。
 * 前端先拦一次，省掉一次必然失败的请求；服务端仍是最终裁判（422）。
 */
export const LIMITS = {
  nodes: 1000,
  depth: 8,
  children: 50,
  label: 200,
  note: 2000,
  /** 生成产出的说明（契约 `Node.detail`）。与 `note` 同容量、不同语义。 */
  detail: 2000,
  title: 200,
  /** 节点图标（契约 `MAX_NODE_ICON_CHARS`）。emoji 组合字符也要放得下。 */
  icon: 16,
  /**
   * 附加边条数（契约 `MAX_EDGES`）。
   *
   * 与上面几个不同的地方：**它是"建议上限"而不是硬边界**。后端超过 200 只报
   * `EDGE_LIMIT`（warn），图照常能存；这里用它做"还能不能再加一条"的判据，因为
   * 界面上的「连一条边」是用户主动的动作，第 201 条时明确拒绝比默默画上去好。
   */
  edges: 200,
  /** 边上的文字（契约 `MAX_EDGE_LABEL_CHARS`）。 */
  edgeLabel: 40,
} as const;

/**
 * 边的语义类型**运行时清单**，与后端 `mindmap.EDGE_KINDS` 一一对应。
 *
 * 与 `NODE_COLORS` 同一个位置、同一条理由：下拉框按它列选项，渲染层按它挑画法。
 * 写死在两个组件里的话，加一种类型就要改两处，而漏掉的那处不会报错——只是那种
 * 边永远选不到、也画不出区别。
 */
export const EDGE_KINDS = ['flow', 'branch', 'loop'] as const satisfies readonly EdgeKind[];

/** `kind` → 下拉框里的中文名。「顺序」是默认值，所以放在第一。 */
export const EDGE_KIND_LABELS: Record<EdgeKind, string> = {
  flow: '顺序（实线）',
  branch: '条件分支（换色）',
  loop: '回退（虚线）',
};

/**
 * 节点语义色的**运行时清单**，与后端 `mindmap.NODE_COLORS` 一一对应。
 *
 * 放在这里而不是 `api/types.ts`：那个文件是纯类型镜像，而这里是"契约里的数字与
 * 枚举值"的镜像（`LIMITS` 就在同一个位置、同一条理由）。调色板要**两处共读**：
 * 渲染层按它挑 CSS 变量，详情面板的下拉框按它列选项。写死在组件里，加一个颜色
 * 就要改两个文件，而漏掉的那个不会报错——只是那个颜色永远选不到。
 *
 * 顺序就是下拉框里的顺序：先主色，再语义（正向/注意/风险），最后是中性。
 */
export const NODE_COLORS = [
  'blue', 'green', 'amber', 'red', 'purple', 'teal', 'pink', 'gray',
] as const satisfies readonly NodeColor[];

/** 调色板键 → 下拉框里给人看的中文名。键本身是渲染用的标识，不该直接上屏。 */
export const NODE_COLOR_LABELS: Record<NodeColor, string> = {
  blue: '蓝（主色）',
  green: '绿（结论）',
  amber: '琥珀（注意）',
  red: '红（风险）',
  purple: '紫（并列主题）',
  teal: '青（过程）',
  pink: '粉（人/组织）',
  gray: '灰（背景）',
};

/**
 * 一个值是不是调色板键。
 *
 * 执行器用它拒绝 `color: '#2563eb'` 这种 op——**两端必须同一口径**：后端
 * `ops._palette_color` 会抛 ValidationError，前端放过去的话，模型看到的反馈
 * 取决于调用的是哪一端。
 */
export function isNodeColor(value: unknown): value is NodeColor {
  return typeof value === 'string' && (NODE_COLORS as readonly string[]).includes(value);
}

/** 整棵树里有没有节点带视觉元素。用于"要不要提示旧图没有颜色"这类判断。 */
export function hasVisuals(root: MindNode): boolean {
  if (root.color || root.icon) return true;
  return root.children.some(hasVisuals);
}

// ---- 附加边（流程图的条件跳转 / 汇合 / 回退）----
//
// 三份小工具：找节点、增删边、校验。都放在这里而不是 `ops.ts`，理由是
// `stores/mindmap.ts`（用户手点「连一条边」）与 `ops.ts`（模型发来的 op 批次）
// 都要用同一套语义；两处各写一遍，"用户点的时候能连、模型连的时候被拒"只是时间问题。

/**
 * 边集合里所有边（缺省空数组）。旧图没有这个字段，所有读取点都走它。
 *
 * 三种内容里**只有鱼骨没有 `edges`**（它的顶层结构里只有 `relations`），判定收在
 * `shape.edgesOfContent` 一处：传进来是鱼骨就是没有边，返回空数组——画布少画一条
 * 线好过让每个调用点各判一次形状。
 */
export function edgesOf(content: DiagramContent | null | undefined): MindEdge[] {
  return content ? edgesOfContent(content) : [];
}

/** 按 id 找边；找不到返回 `null`。 */
export function findEdge(edges: readonly MindEdge[], id: string): MindEdge | null {
  return edges.find(edge => edge.id === id) ?? null;
}

/** 新边 id。前缀 `e_` 与后端 `new_edge_id` 一致，日志里一眼分得清线与节点。 */
export function newEdgeId(): string {
  return `e_${newId().slice(2)}`;
}

/**
 * 边 id 是**两个数组共用**的一个命名空间（契约 `FlowContent.validate_flow` 会跨
 * `content.edges` 与全部 `stages[i].edges` 查重名）。所以生成 id 时要拿两边已有的
 * id 一起避开，否则随机撞上之后表现是"另一条边永远画不出来"。
 */
function freshEdgeId(content: DiagramContent): string {
  const taken = new Set([
    ...edgesOf(content).map(edge => edge.id),
    ...stageInnerEdgesOf(content).map(edge => edge.id),
  ]);
  for (let attempt = 0; attempt < 16; attempt += 1) {
    const candidate = newEdgeId();
    if (!taken.has(candidate)) return candidate;
  }
  return newEdgeId();
}

/**
 * 加一条边。返回新内容与这条边；**不动原对象**。
 *
 * 与 `addChild` 同一套约定：纯函数、结构共享、调用方拿到新内容自己决定何时提交。
 * 两条会立刻被服务端拒的形态在这里先拦：
 *  - 自环（画不出来）；
 *  - 完全相同的 `(source, target, label)`（后端去重，这里直接不允许创建，
 *    免得用户点两次"添加"后看到一条线却没发现自己点了两下）。
 *
 * `label` 不同则允许：判断节点的"是"与"否"经常指向同一个目标。
 *
 * ## 三种形状各连的是什么（这里唯一需要判形状的地方）
 *
 *  - 树：端点是**节点 id**，与入参一致；
 *  - 流程：**两个数组、两个命名空间**（契约 `FlowStage` 的注释）。两个端点在
 *    **不同**阶段 → 写 `content.edges`，端点换成各自的**阶段 id**；两个端点在
 *    **同一个**阶段 → 写那个阶段的 `stage.edges`，端点保持**节点 id**。后者正是
 *    判定节点的「是 / 否」两条出边——此前这里明确拒绝（"同一个阶段里的步骤之间
 *    暂不支持连关系"），于是模型标出的判定节点在画布上没有出口，而用户也补不上；
 *  - 鱼骨：顶层结构里根本没有 `edges`，加不了。说清楚比静默丢弃好。
 */
export function addEdge(
  content: DiagramContent,
  source: string,
  target: string,
  label?: string | null,
  kind?: EdgeKind | null,
): { content: DiagramContent; edge: MindEdge } {
  if (source === target) throw new Error('起点与终点不能是同一个节点');
  if (isFishbone(content)) {
    throw new Error('鱼骨图暂不支持附加关系：它的一级结构是类别，跨骨的关系还没有语义');
  }

  const text = (label ?? '').trim();
  const normalized = text ? text.slice(0, LIMITS.edgeLabel) : null;

  if (isFlow(content)) {
    const sourceStage = stageOfNode(content, source);
    const targetStage = stageOfNode(content, target);
    if (!sourceStage || !targetStage) throw new Error('端点节点不存在');
    const duplicated = (items: readonly MindEdge[], from: string, to: string) => items.some(
      edge => edge.source === from && edge.target === to && (edge.label ?? null) === normalized,
    );

    if (sourceStage !== targetStage) {
      const edges = edgesOf(content);
      if (duplicated(edges, sourceStage, targetStage)) throw new Error('这条关系已经存在');
      const edge: MindEdge = {
        id: freshEdgeId(content),
        source: sourceStage,
        target: targetStage,
        label: normalized,
        kind: kind ?? null,
      };
      return { content: { ...content, edges: [...edges, edge] }, edge };
    }

    // 同一个阶段内部：端点就是两个节点的 id，落进这个阶段自己的数组。
    const stage = content.stages.find(item => item.id === sourceStage)!;
    const inner = stage.edges ?? [];
    if (duplicated(inner, source, target)) throw new Error('这条关系已经存在');
    const edge: MindEdge = {
      id: freshEdgeId(content),
      source,
      target,
      label: normalized,
      kind: kind ?? null,
    };
    return {
      content: {
        ...content,
        stages: content.stages.map(item => (
          item.id === sourceStage ? { ...item, edges: [...inner, edge] } : item
        )),
      },
      edge,
    };
  }

  if (!findNode(content.root, source)) throw new Error('起点节点不存在');
  if (!findNode(content.root, target)) throw new Error('终点节点不存在');

  const edges = edgesOf(content);
  if (edges.some(edge => edge.source === source && edge.target === target
    && (edge.label ?? null) === normalized)) {
    throw new Error('这条关系已经存在');
  }

  const edge: MindEdge = {
    id: freshEdgeId(content),
    source,
    target,
    label: normalized,
    kind: kind ?? null,
  };
  return { content: { ...content, edges: [...edges, edge] }, edge };
}

/**
 * 删一条边。只动装着它的那个数组，树与另一个数组一个字节都不变。
 *
 * **按 id 查两个数组**：阶段内的边活在 `stages[i].edges` 里，只查 `content.edges`
 * 的话，侧栏列出来（`stageInnerEdgesOf` 读得到）却删不掉——按钮点了没反应，而
 * 用户只会以为界面坏了。
 */
export function removeEdge(content: DiagramContent, edgeId: string): DiagramContent {
  if (isFlow(content)) {
    const holder = content.stages.find(stage => (stage.edges ?? []).some(edge => edge.id === edgeId));
    if (holder) {
      // 只重写持有它的那一个阶段：其余阶段保持原对象（结构共享），
      // 撤销栈与 diff 都靠"没变的还是同一个引用"这件事省事。
      return {
        ...content,
        stages: content.stages.map(stage => (
          stage === holder
            ? { ...stage, edges: (stage.edges ?? []).filter(edge => edge.id !== edgeId) }
            : stage
        )),
      };
    }
  }
  return { ...content, edges: edgesOf(content).filter(edge => edge.id !== edgeId) };
}

/**
 * 删一条**跨节点关系线**。只动 `relations`，树与两个边数组一个字节都不变。
 *
 * 为什么单独一个函数而不是让 `removeEdge` 顺着查：`edges` 与 `relations` 是两套字段
 * （同一条 dangling 判据，见 `dropDanglingEdges` 的注释），而 id 也是两套命名空间。
 * 混在一个函数里，调用方就得先判断"这个 id 是哪一类的"——那件事在界面上没有意义
 * （用户在详情面板里点的就是"删掉这条关系"）。
 */
export function removeRelation(content: DiagramContent, relationId: string): DiagramContent {
  const relations = content.relations ?? [];
  const kept = relations.filter(relation => relation.id !== relationId);
  if (kept.length === relations.length) return content;
  return { ...content, relations: kept } as DiagramContent;
}

/**
 * 删掉一批节点之后，把端点已经不存在的边**与关系**一起清掉。三张表都清。
 *
 * 与后端 `ops::_apply_remove` 同一语义（"删节点连带删边/关系"）：留着悬空项，下一次
 * 保存就是 422，而报错指向某条边或某条关系，与用户刚才做的事隔得很远。
 *
 * 三种形状各自的"端点是节点 id"这一层不同：
 *  - 树：`content.edges` 的端点就是节点 id；
 *  - 流程：`content.edges` 的端点是**阶段 id**（删一个步骤碰不到它），而
 *    `stages[i].edges` 的端点才是**该阶段的节点 id**——这一层以前没有读者，所以
 *    "流程删节点不用管边"曾经是成立的；现在它不成立了；
 *  - 鱼骨：没有边。
 *
 * 但 `relations`（跨节点关系线）**三种形状的顶层都有**（契约 `ContentBase`），端点
 * 永远是节点 id，与形状无关。此前这里只清 `edges`：删一个被关系引用的节点之后，
 * 保存会被契约 `_validate_relations` 硬拒（"关系 X 的起点节点 Y 不在导图中"），
 * 而界面根本不渲染 `relations`——用户既看不到那条残留的关系，也没法删掉它，
 * 只能看着保存 422。所以"删节点连带删"的范围必须包含它。
 */
export function dropDanglingEdges(content: DiagramContent, doomed: Set<string>): DiagramContent {
  const alive = (edge: { source: string; target: string }) => (
    !doomed.has(edge.source) && !doomed.has(edge.target)
  );

  let next: DiagramContent = content;
  if (isFlow(content)) {
    let hit = false;
    const stages = content.stages.map(stage => {
      const inner = stage.edges ?? [];
      const kept = inner.filter(alive);
      if (kept.length === inner.length) return stage;
      hit = true;
      return { ...stage, edges: kept };
    });
    // 一条都没删到时返回**同一个对象**：调用方靠引用相等判断"这次什么都没变"，
    // 无谓的复制会让"删节点却没删掉边"看起来像改过。
    if (hit) next = { ...content, stages };
  } else if (!isFishbone(content)) {
    const edges = edgesOf(content);
    const kept = edges.filter(alive);
    if (kept.length !== edges.length) next = { ...content, edges: kept };
  }
  // 鱼骨落到这里时 `next` 还是原内容：它的顶层本来就没有 `edges`。

  const relations = content.relations ?? [];
  const keptRelations = relations.filter(alive);
  if (keptRelations.length === relations.length) return next;
  // `relations` 落在契约的 `ContentBase` 上，三种形状都有这个键，所以这一句对三者
  // 都成立；`as DiagramContent` 只是让联合类型认得出这条**共有**的键。
  return { ...next, relations: keptRelations } as DiagramContent;
}

/**
 * 按**现存节点**清掉悬空的跨节点引用：`relations` 与**阶段内边**。
 *
 * 与 `dropDanglingEdges` 是同一个判据的两个入口，区别只在"什么时候知道哪些节点没了"：
 *
 *  - `dropDanglingEdges(content, doomed)`：删之前就算好了 `doomed`（界面删节点、
 *    撤销快照那条路）；
 *  - `pruneDanglingRefs(content)`：已经改完，只知道现在还剩谁（`ops.applyOps` 的
 *    鱼骨/流程分支——op 批次在一棵临时 `MindMapContent` 里跑，而 `relations` 与
 *    `stages[i].edges` 都**不在那棵树里**）。
 *
 * 两件事必须一起做，因为它们在同一个位置缺读者：
 *  - 阶段内边的端点是**该阶段自己的**节点 id，删掉一个步骤之后它们就悬空了。这条
 *    与后端 `tools._prune_stage_edges` 同口径（判据都是"端点是否还在"）；
 *  - `relations` 落在契约 `ContentBase` 上，三种形状都有，端点永远是节点 id。与
 *    后端 `tools._prune_dangling_relations` 同口径。
 *
 * 没有可丢的时候返回**同一个对象**：调用方靠引用相等判断"这次什么都没变"。
 */
export function pruneDanglingRefs(content: DiagramContent): DiagramContent {
  let next = content;

  if (isFlow(next)) {
    let hit = false;
    const stages = next.stages.map(stage => {
      const alive = new Set<string>();
      for (const node of walkNodes(stage.root)) alive.add(node.id);
      const inner = stage.edges ?? [];
      const kept = inner.filter(edge => alive.has(edge.source) && alive.has(edge.target));
      if (kept.length === inner.length) return stage;
      hit = true;
      return { ...stage, edges: kept };
    });
    if (hit) next = { ...next, stages };
  }

  const relations = next.relations ?? [];
  if (relations.length === 0) return next;
  const alive = new Set<string>();
  for (const node of iterNodesIn(next)) alive.add(node.id);
  const kept = relations.filter(
    relation => alive.has(relation.source) && alive.has(relation.target),
  );
  if (kept.length === relations.length) return next;
  return { ...next, relations: kept } as DiagramContent;
}

/**
 * AI 生成时标题的软上限（契约 `GENERATED_LABEL_MAX_CHARS`）。
 *
 * 与 `LIMITS.label`（200，契约硬上限、旧图读取口径）是两件事：生成侧要求 16 字，
 * 但旧图里那句 20–30 字的长标题必须照常读、照常存。前端用它只做**提示**（"这个
 * 标题有点长，画布上会折行"），不做校验——把 16 写进校验会让用户改不动旧节点。
 *
 * ## 这个 16 是跨语言副本，由后端用例对账
 *
 * 后端是 Python、这里是 TypeScript，没有 import 关系，所以这一行**必须**有人钉住：
 * `backend/tests/test_shared_constants.py::test_frontend_generated_label_hint` 读本文件、
 * 正则取出这个字面量，断言它等于 `GENERATED_LABEL_MAX_CHARS`。改部署侧那个数而
 * 忘了这里，红的是后端测试。
 *
 * 为什么不走 `thinking-options.json` 那条生成物管线（后端脚本 → json → 前端 import）：
 * 那条路是为"首屏就要用的**列表**"建的，产物是一个文件加一次导出、一次守卫用例。
 * 这里只有一个标量、只喂一句提示文案，为它复制一整条管线是拿结构换一个数字。
 * 值真变成"界面要靠它算"的那天，再升级成生成物不迟。
 *
 * ## 前端当前**零消费者**，这件事要说清楚
 *
 * 全仓搜这个名字只有两处：这一行定义，和后端那条用例的正则。上面写的"前端用它只做
 * 提示"是**位置已经留好、提示本身还没接线**——不写清这一点，几轮之后会有人读成
 * "界面上已经在提示标题太长了"，那是注释在描述一个不存在的功能。
 *
 * 为什么不删：删掉等于让 16 在前端彻底隐形。将来真要做那句提示，最顺手的写法就是就地
 * 写一个 16——那一刻没有任何红灯照得到它，而 `mindmap/shape.ts::shapeLabel` 栽的正是
 * 同一个坑（手抄一份、注释承诺同步、从来没人钉）。留着它：位置对、值有对账、来历写在
 * 这里，接线那天只需把比较用上。
 *
 * 顺带一个常被误会的点：前端零引用**不会**进 `scripts/acc3/find_unwired.py` 的清单。
 * 那个判据只扫 `backend/app/` 下的 Python 文件（`*.py`），与 TypeScript 无关。所以留它
 * 的成本是这一行注释，不是闸门红灯。
 */
export const GENERATED_LABEL_HINT = 16;

/** 复制一棵树：编辑操作都在副本上做，撤销栈里的快照才不会被后续修改带跑。 */
export function cloneTree<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

/** 生成新节点 id。加前缀是为了和模型返回的 id 区分开，避免撞车。 */
export function newId(): string {
  const random =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID().replace(/-/g, '').slice(0, 8)
      : Math.random().toString(16).slice(2, 10);
  return `n_${random}`;
}

export function findNode(root: MindNode, id: string): MindNode | null {
  if (root.id === id) return root;
  for (const child of root.children) {
    const hit = findNode(child, id);
    if (hit) return hit;
  }
  return null;
}

/** 父节点；根节点返回 null。 */
export function findParent(root: MindNode, id: string): MindNode | null {
  for (const child of root.children) {
    if (child.id === id) return root;
    const hit = findParent(child, id);
    if (hit) return hit;
  }
  return null;
}

export function countNodes(root: MindNode): number {
  return 1 + root.children.reduce((sum, child) => sum + countNodes(child), 0);
}

export function depthOf(root: MindNode): number {
  if (root.children.length === 0) return 1;
  return 1 + Math.max(...root.children.map(depthOf));
}

function replaceChildren(
  node: MindNode,
  parentId: string,
  mapper: (children: MindNode[]) => MindNode[],
): MindNode {
  if (node.id === parentId) return { ...node, children: mapper(node.children) };
  return {
    ...node,
    children: node.children.map(child => replaceChildren(child, parentId, mapper)),
  };
}

/** 改节点文字。返回新的根，原树不动。 */
export function renameNode(root: MindNode, id: string, label: string): MindNode {
  if (root.id === id) return { ...root, label };
  return {
    ...root,
    children: root.children.map(child => renameNode(child, id, label)),
  };
}

/**
 * 改节点正文（契约 v2 的 `note`）。返回新的根，原树不动。
 *
 * 纯空白等价于"没有正文"：后端把纯空白视为 `None`，这里先归一，
 * 免得界面上出现一个 `note: '   '`、保存后又被服务端悄悄改成 null。
 */
export function setNodeNote(root: MindNode, id: string, note: string | null): MindNode {
  if (root.id === id) {
    const trimmed = note?.trim();
    return { ...root, note: trimmed ? trimmed : null };
  }
  return {
    ...root,
    children: root.children.map(child => setNodeNote(child, id, note)),
  };
}

/**
 * 改节点的视觉元素（`color` / `icon`）。返回新的根，原树不动。
 *
 * 与 `setNodeNote` 分开而不是合成一个"改任意字段"的通用函数：通用版本会诱使
 * 调用方把 `label`、`note`、`source_refs` 也顺手塞进来，而那三条各自有更严格的
 * 前置条件（非空、2000 字、引用必须落在 source_usage 里）。窄函数让"哪条路径
 * 能改什么"在签名上就看得出来。
 *
 * `null` 是**显式清除**（与 `undefined` 不同）：界面上选「默认」就是清掉颜色。
 */
export function setNodeVisual(
  root: MindNode,
  id: string,
  color: NodeColor | null,
  icon: string | null,
): MindNode {
  if (root.id === id) {
    const trimmed = (icon ?? '').trim();
    return { ...root, color, icon: trimmed ? trimmed : null };
  }
  return {
    ...root,
    children: root.children.map(child => setNodeVisual(child, id, color, icon)),
  };
}

/**
 * 在 `parentId` 的第 `index` 个位置插一个子节点。返回新树与新节点，方便界面立刻
 * 选中它并进入编辑。
 *
 * `index` 越界时夹到 `[0, children.length]`（与 `moveNode` 同一口径）：越界说明调用方
 * 的算术出了洞，而"夹一下"与"抛错"之间——前者最坏是插在末尾（一个合法结果），后者会
 * 让一个按键把整个组件树炸掉。
 *
 * 下标不是可选参数：`addChild` 的"追加到末尾"是一条**语义**（"又一个孩子"），而
 * 空白处双击要的是"插在这个位置"（见 `drop-plan.ts::planInsert`）。两者合成一个
 * 函数会让调用方每次都猜"我该传什么"，而"末尾"在插入语境里恰好是最不容易出错的那个
 * 默认值——错的那个方向会被当成对的。
 *
 * **新节点一律 `origin: 'manual'`**：它是人在这里按出来的。这一条只在创建处写一次，
 * 三种入口（Tab 加子节点 / Enter 加兄弟 / 空白处双击插入）全部经过这里或
 * `addSibling`，所以"手动加一个节点却没标来历"这件事在这里是不可表达的——而不是
 * 靠每个调用方记得加一行。`origin` 的相反面（生成产出）由后端在它自己的出口写，
 * 前端不猜。
 */
export function addChildAt(
  root: MindNode,
  parentId: string,
  index: number,
  label = '新节点',
): { root: MindNode; node: MindNode } {
  if (!findNode(root, parentId)) throw new Error('父节点不存在');

  const node: MindNode = { id: newId(), label, children: [], origin: 'manual' };
  return {
    root: replaceChildren(root, parentId, children => {
      const at = Math.max(0, Math.min(index, children.length));
      return [...children.slice(0, at), node, ...children.slice(at)];
    }),
    node,
  };
}

/** 加子节点。返回新树与新节点，方便界面立刻选中它并进入编辑。 */
export function addChild(
  root: MindNode,
  parentId: string,
  label = '新节点',
): { root: MindNode; node: MindNode } {
  const parent = findNode(root, parentId);
  if (!parent) throw new Error('父节点不存在');
  return addChildAt(root, parentId, parent.children.length, label);
}

/**
 * 加兄弟：插在当前节点后面，位置符合直觉（紧接着自己）。
 * 根节点没有兄弟，退化成加子节点。
 */
export function addSibling(
  root: MindNode,
  id: string,
  label = '新节点',
): { root: MindNode; node: MindNode } {
  const parent = findParent(root, id);
  if (!parent) return addChild(root, root.id, label);

  const node: MindNode = { id: newId(), label, children: [], origin: 'manual' };
  return {
    root: replaceChildren(root, parent.id, children => {
      const at = children.findIndex(child => child.id === id);
      return [...children.slice(0, at + 1), node, ...children.slice(at + 1)];
    }),
    node,
  };
}

/** 删除节点及其子树。根节点不能删（删了导图就没了）。 */
export function removeNode(root: MindNode, id: string): MindNode {
  if (root.id === id) throw new Error('根节点不能删除');
  return {
    ...root,
    children: root.children
      .filter(child => child.id !== id)
      .map(child => removeNode(child, id)),
  };
}

/**
 * 节点及其全部子孙的 id。
 *
 * 「删节点要连带删边」（`store.deleteNode`、`ops.applyOps` 的 `remove`）都要这份
 * 集合。写成两处的话，漏掉子孙那一处的后果是可复现的：删掉一棵子树之后，子节点
 * 身上的边仍然留在 `content.edges` 里，**下一次保存 422**，而报错指向某条边，
 * 与用户刚才做的事隔了很远。
 */
export function subtreeIds(node: MindNode): Set<string> {
  const ids = new Set<string>();
  const stack = [node];
  while (stack.length > 0) {
    const current = stack.pop()!;
    ids.add(current.id);
    stack.push(...current.children);
  }
  return ids;
}

/**
 * 把一个节点（连同它整支）挪到别处：成为 `parentId` 的第 `index` 个孩子。
 * 返回新的根，原树不动。
 *
 * **先摘后插**，顺序不能换：同层移动时，先插会让"自己原来占的那一格"留在兄弟
 * 数组里参与下标计算，于是落到第 `index + 1` 位——每一次"往上挪一位"都会变成
 * "原地不动"。摘掉之后剩下的那个数组，才是 `index` 说的坐标系。
 *
 * 三个前置条件都抛错而不是静默返回原树：它们全部是**调用方的判据出了洞**才会
 * 触发的分支（自环、挂进自己的子树、目标不存在），静默返回会让"拖了没反应"
 * 变成一条查不出来的路径。
 */
export function moveNode(
  root: MindNode,
  id: string,
  parentId: string,
  index: number,
): MindNode {
  if (id === root.id) throw new Error('根节点不能移动');
  const node = findNode(root, id);
  if (!node) throw new Error('要移动的节点不存在');

  const detached = removeNode(root, id);
  // 摘完之后还找不到父节点，说明父节点就在**被摘走的那一支**里——那是最直接的
  // 环（自己挂到自己下面），也是唯一一种摘完就消失的情形。
  if (!findNode(detached, parentId)) throw new Error('不能把它挂到自己的子树里');

  return replaceChildren(detached, parentId, children => {
    const at = Math.max(0, Math.min(index, children.length));
    return [...children.slice(0, at), node, ...children.slice(at)];
  });
}

/**
 * 「我要当 `targetId` 的父」：目标那一支挂到自己的 children 末尾，自己顶替目标
 * 在它父节点下的位次。返回新的根，原树不动。
 *
 * 这个动作看着像"两步移动"，但它与 `moveNode` 是**同一类不可变改写**、不是两次
 * 独立操作：中间态（目标被摘下、自己还没插进去）永远不会被谁看到，所以撤销栈里
 * 它只是**一步**，而用户按 Ctrl+Z 退回去的也应该是整件事。
 *
 * 位次取「目标原来的那一格」而不是"末尾"：拖到目标的左边时，画面上期望的是
 * 「我在它刚才的位置，它被我挤到右边一层」。落到末尾会让整支兄弟的顺序莫名变化
 * ——用户改的是一处父子关系，不该顺带重排它旁边那些不相干的节点。
 */
export function reparentAbove(
  root: MindNode,
  draggedId: string,
  targetId: string,
): MindNode {
  if (draggedId === root.id) throw new Error('根节点不能移动');
  if (targetId === root.id) throw new Error('根节点不能成为别人的子节点');
  const dragged = findNode(root, draggedId);
  if (!dragged) throw new Error('要移动的节点不存在');

  // 先摘自己，再找目标：目标可能挂在自己的祖先链上（"把父顶下去当自己的子"），
  // 摘完之后它的父链才是松手之后的样子。
  const detached = removeNode(root, draggedId);
  const target = findNode(detached, targetId);
  if (!target) throw new Error('不能把上级挂到自己的子树里');
  const parent = findParent(detached, targetId);
  if (!parent) throw new Error('根节点不能成为别人的子节点');

  const at = parent.children.findIndex(child => child.id === targetId);
  const withoutTarget = removeNode(detached, targetId);
  const adopted: MindNode = { ...dragged, children: [...dragged.children, target] };
  return replaceChildren(withoutTarget, parent.id, children => [
    ...children.slice(0, at), adopted, ...children.slice(at),
  ]);
}

/**
 * 「断开与父节点的关系」：把这一支摘下来，挂到**根的最后**，成为一级分支。
 *
 * 为什么"断关系"在树上只能是这一个结果（`docs/NODE-EDIT.md` §一）：content 是一棵树，
 * 树里没有"无父节点"这个概念——除了根。想让一个节点不再属于原来那一支，唯一的合法
 * 去处就是根。所以"断成孤儿、单独悬在画布上"要么引入第二个概念（游离节点集合）+ 三套
 * 形状各写一遍遍历，要么就只能借"根的孩子"这个位置来承载。
 *
 * 挂在**末尾**而不是原来父节点的位次上：它在根下面本来就没有"原来的位次"。而末尾是
 * 最不容易被误读的一格——新出现的一级分支出现在这一串的最下面，与"新加的东西排在后面"
 * 这条既有直觉一致（`addChild` 也是末尾）。
 *
 * 与 `moveNode(root, id, root.id, n)` 的差别只有一处：这里**不重排**兄弟，也不碰
 * 任何别的节点；它只做"摘下来 + 挂到根末尾"，返回 `formerParentId` 是因为界面要说出
 * "从「甲」下面断开"这句话，而那句话的输入只有这里知道。
 */
export function detachToRoot(
  root: MindNode,
  id: string,
): { root: MindNode; formerParentId: string; index: number } {
  if (id === root.id) throw new Error('根节点不能断开');
  const parent = findParent(root, id);
  const node = findNode(root, id);
  if (!parent || !node) throw new Error('要断开的节点不存在');

  const detached = removeNode(root, id);
  const index = detached.children.length;
  return {
    root: { ...detached, children: [...detached.children, node] },
    formerParentId: parent.id,
    index,
  };
}

/** 字数按码点算，和 Pydantic 的 len() 以及画布上的截断保持一致。 */
export function charLength(text: string): number {
  return Array.from(text).length;
}

function shortLabel(label: string): string {
  const chars = Array.from(label.trim());
  return chars.length > 12 ? `${chars.slice(0, 12).join('')}…` : chars.join('');
}

/**
 * 提交前校验整份内容，返回第一条错误文案；没问题返回 null。
 * 文案尽量与服务端一致，避免同一条规则出现两种说法。
 *
 * **三种形状共用这一份**，判据逐条对齐后端：`_scan_tree` 的 `seen` 集合在多棵并列
 * 子树之间**共用**（节点 id 必须全局唯一、节点总数按各容器合计算），而层号按每棵
 * 子树**各自**从 1 数；边的端点命名空间随形状走——树是节点 id，流程是**阶段 id**，
 * 鱼骨没有边。分开写三份的话，某一天"树拦了、鱼骨没拦"的分叉只会表现为一次 422。
 */
export function validateDraft(content: DiagramContent): string | null {
  const title = content.title.trim();
  if (!title) return '标题不能为空';
  if (charLength(title) > LIMITS.title) return `标题最多 ${LIMITS.title} 个字`;

  if (isFishbone(content)) {
    const categories = new Set<string>();
    for (const bone of content.bones) {
      if (!bone.category.trim()) return '骨的类别不能为空';
      if (categories.has(bone.category)) return `骨的类别不能重复（${bone.category}）`;
      categories.add(bone.category);
    }
  }
  if (isFlow(content)) {
    if (content.stages.length === 0) return '流程图至少要有一个阶段';
    const stageIds = new Set<string>();
    for (const stage of content.stages) {
      if (!stage.id) return '阶段 ID 不能为空';
      if (stageIds.has(stage.id)) return `阶段 ID 必须唯一（重复的是 ${stage.id}）`;
      stageIds.add(stage.id);
    }
  }

  const seen = new Set<string>();
  for (const root of rootsOf(content)) {
    const stack: Array<{ node: MindNode; depth: number }> = [{ node: root, depth: 1 }];

    while (stack.length > 0) {
      const { node, depth } = stack.pop()!;

      if (seen.has(node.id)) return '节点 ID 必须唯一';
      seen.add(node.id);
      if (seen.size > LIMITS.nodes) return `导图最多 ${LIMITS.nodes} 个节点`;
      if (depth > LIMITS.depth) return `导图最多 ${LIMITS.depth} 层`;

      const label = node.label.trim();
      if (!label) return '节点标题不能为空';
      if (charLength(label) > LIMITS.label) {
        return `节点「${shortLabel(label)}」超过 ${LIMITS.label} 个字`;
      }
      if (node.note && charLength(node.note.trim()) > LIMITS.note) {
        return `节点「${shortLabel(label)}」的正文超过 ${LIMITS.note} 个字`;
      }
      if (node.detail && charLength(node.detail.trim()) > LIMITS.detail) {
        return `节点「${shortLabel(label)}」的详情超过 ${LIMITS.detail} 个字`;
      }
      // 视觉元素：与后端 `Node.color` 的 Literal / `Node.icon` 的 max_length 对齐。
      // 不在这里拦的话，一个非法颜色要到保存时才 422，而那时用户已经在图上改了
      // 半天——他看到的是一句"内容不合法"，指不到是哪个节点。
      if (node.color != null && !isNodeColor(node.color)) {
        return `节点「${shortLabel(label)}」的颜色不在调色板里（${NODE_COLORS.join('/')}）`;
      }
      if (node.icon != null && (!node.icon.trim() || charLength(node.icon) > LIMITS.icon)) {
        return `节点「${shortLabel(label)}」的图标应为 1–${LIMITS.icon} 个字符`;
      }
      if (node.children.length > LIMITS.children) {
        return `节点「${shortLabel(label)}」最多 ${LIMITS.children} 个子节点`;
      }

      for (const child of node.children) stack.push({ node: child, depth: depth + 1 });
    }
  }

  // 跨节点关系（`relations`）与附加边是**两套字段、同一条 dangling 判据**：契约
  // `_validate_relations` 对三种形状都硬拒指向不存在节点的关系，而"删节点"正是造出
  // 这种关系的那双手。`dropDanglingEdges` 已经在删的时候清掉了，这里再拦一次是为了
  // 另一半来源——库里读出来的旧图 / 导入进来的图本来就带悬空关系（界面不渲染
  // `relations`，用户既看不到也删不掉），那时保存 422 且指不到任何可操作的东西。
  //
  // 只查"端点存在"这一条：id 唯一、自环、同三元组重复在界面上造不出来（`addEdge`
  // 与导入侧各自拦过），重复写一遍只会让同一条规则有两处说法。
  for (const relation of content.relations ?? []) {
    if (!seen.has(relation.source) || !seen.has(relation.target)) {
      const missing = !seen.has(relation.source) ? relation.source : relation.target;
      return `关系 ${relation.id} 指向不存在的节点 ${missing}`;
    }
  }

  // 附加边：只拦**后端会硬拒**的那两种（dangling 与自环），与契约
  // `MindMapContent.validate_tree` / `FlowContent.validate_flow` 逐条对齐。
  //
  // 条数（>200）与环长（>12 节点）**不在这里拦**：后端把它们报成 warn 而不是
  // 拒绝，前端拦下来的话，一张从库里读出来的、有 250 条边的图就再也存不回去了
  // ——用户什么都没改，图却存不上。校验只该拦"服务端一定会拒"的东西。
  //
  // 端点按**形状自己的命名空间**查：树的边连节点，流程的边连阶段。用节点 id 去查
  // 一张流程图的边，每一条都会变成"指向不存在的节点"，而那份图其实好得很。
  const edgeSpace = isFlow(content)
    ? new Set(containersOf(content).map(container => container.id))
    : seen;
  const edgeNoun = isFlow(content) ? '阶段' : '节点';
  const edgeIds = new Set<string>();
  for (const edge of edgesOf(content)) {
    if (edgeIds.has(edge.id)) return `边 ID 必须唯一（重复的是 ${edge.id}）`;
    edgeIds.add(edge.id);
    if (!edgeSpace.has(edge.source) || !edgeSpace.has(edge.target)) {
      const missing = !edgeSpace.has(edge.source) ? edge.source : edge.target;
      return `附加关系 ${edge.id} 指向不存在的${edgeNoun} ${missing}`;
    }
    if (edge.source === edge.target) return `附加关系 ${edge.id} 的起点与终点不能是同一个${edgeNoun}`;
    if (charLength(edge.label ?? '') > LIMITS.edgeLabel) {
      return `附加关系 ${edge.id} 的文字超过 ${LIMITS.edgeLabel} 个字`;
    }
  }

  // 阶段**内部**的边是另一个命名空间（端点是该阶段自己的节点 id），判据一模一样、
  // 集合不同——与后端 `FlowContent.validate_flow` 逐条对应：先查逐阶段端点，再查
  // **两个数组之间**的边 id 唯一性（`edgeIds` 是共用的那一份）。
  //
  // 不在这里拦的话，一张"阶段内边连到了另一个阶段的节点"的图会一路存到后端才
  // 422，而报错只说"边 X 的终点节点 Y 不在阶段 s_2 里"，用户手上没有任何线索。
  if (isFlow(content)) {
    for (const stage of content.stages) {
      const own = new Set<string>();
      for (const node of walkNodes(stage.root)) own.add(node.id);
      for (const edge of stage.edges ?? []) {
        if (edgeIds.has(edge.id)) return `边 ID 必须唯一（重复的是 ${edge.id}）`;
        edgeIds.add(edge.id);
        if (!own.has(edge.source) || !own.has(edge.target)) {
          const missing = !own.has(edge.source) ? edge.source : edge.target;
          return `附加关系 ${edge.id} 指向不存在的节点 ${missing}（不在阶段 ${stage.id} 里）`;
        }
        if (edge.source === edge.target) {
          return `附加关系 ${edge.id} 的起点与终点不能是同一个节点`;
        }
        if (charLength(edge.label ?? '') > LIMITS.edgeLabel) {
          return `附加关系 ${edge.id} 的文字超过 ${LIMITS.edgeLabel} 个字`;
        }
      }
    }
  }

  return null;
}
