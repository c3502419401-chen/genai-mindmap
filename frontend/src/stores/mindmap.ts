import type { Edge as FlowEdge, Node as FlowNode } from '@vue-flow/core';
import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

import { ApiError, createMindMap, getMindMap, getRun, listMindMaps, listRuns, markRunSaved, runAgentStream, updateMindMap } from '../api/client';
import type { AgentEvent, AgentRunRequest, DiagramContent, DiagramTypeChoice, EdgeKind, MapRecord, MindEdge, MindMapContent, MindNode, MindRelation, NodeColor, OpBatch, RunSummary, Source, SourceRef } from '../api/types';
import { directionOf, withDirection } from '../mindmap/direction';
import {
  dropMessage,
  planDrop,
  type DropIntent,
  type DropRejectionCode,
} from '../mindmap/drop-plan';
import { diffEdits, EDIT_EVENT_LIMIT, generateEvent, type EditActor, type EditEvent } from '../mindmap/edit-log';
import { CONNECTION_LOST, guideForFailure, INVALID_SOURCES, NO_CONTENT, type FailureGuide } from '../mindmap/failures';
import { applyOps } from '../mindmap/ops';
import { buildRefIndex } from '../mindmap/refs';
import { diagnosticCount, diagnosticsHint } from '../mindmap/run-status';
import {
  canvasEdgesOf,
  canvasStageEdgesOf,
  canvasTreeOf,
  containerNoun,
  containerOfNode,
  countNodesIn,
  depthIn,
  edgeEndpointLabel,
  findIn,
  findParentIn,
  iterNodesIn,
  isContainerRoot,
  isFlow,
  isTree,
  kindOf,
  nodeShapeOf,
  pathToIn,
  replaceContainer,
  rootsOf,
  stageInnerEdgesOf,
  stageOfNode,
  type NodeRole,
  type NodeShape,
} from '../mindmap/shape';
import { sourceInputError } from '../sources/input';
import { useSourcesStore } from './sources';
import { childSide, layoutTidy, NODE_SIZE, type ChildSide, type Direction } from '../canvas/layout/tidy';
import { diagramTypeOf, layoutDiagram, type DiagramLayout } from '../canvas/layout';
import type { NodeSize } from '../canvas/layout/flow';
import {
  MINDMAP_BRANCH_BADGE_MIN_HIDDEN,
  MINDMAP_VISIBLE_NODE_LIMIT,
  foldActionOf,
  viewTree,
  type FoldInfo,
  type FoldOpenReason,
} from '../canvas/view-collapse';
import {
  addChild,
  addEdge as addEdgeToContent,
  addSibling,
  charLength,
  dropDanglingEdges,
  edgesOf,
  findNode,
  LIMITS,
  removeEdge as removeEdgeFromContent,
  removeRelation as removeRelationFromContent,
  removeNode,
  renameNode,
  setNodeNote,
  setNodeVisual,
  subtreeIds,
  validateDraft,
} from '../mindmap/edits';

/** 画布节点上最多显示的字数（按码点算，避免把 emoji 切成半个）。 */
export const TRUNCATE_CHARS = 24;

/**
 * 拖动落点的预演结果：松手之后它会落在哪儿、这一步合不合法。
 *
 * `rect` 是**松手之后的真实位置**（flow 坐标，节点左上角），不是"光标当前位置"。
 * 两者的区别就是这一轮的全部设计：坐标仍然只有一个来源（布局），拖动表达的是
 * **结构**；幽灵框把这个结构的结果提前画出来，于是"手拖的位置"从来不需要被保存，
 * `Node.pos` 也一直不需要被写（见 `docs/NODE-DRAG.md` §1）。
 */
export interface MovePreview {
  ok: boolean;
  /** 拒绝原因分类。`not-tree` 是这张图型本轮还不支持拖动改结构。 */
  code?: DropRejectionCode | 'not-tree';
  /** 直接给用户看的那句话（非法时画布把它画在光标旁）。 */
  reason?: string;
  /** 落点父节点 id（"它会挂到谁下面"）。 */
  parentId?: string;
  index?: number;
  /** 落点左上角 + 尺寸，画布据此画那个虚线幽灵框。 */
  rect?: { x: number; y: number; width: number; height: number };
}

/** 撤销栈上限。整树快照一步几 KB，50 步封顶，内存与手感都够用。 */
export const HISTORY_LIMIT = 50;/**
 * 撤销栈的总字节上限。
 *
 * 只有步数上限时，节点上限从 300 提到 1000 之后会失真：一份快照就能到几十 KB，
 * 50 步最坏能压到几十 MB。按字节封顶比调小步数更贴近真实内存占用，也不用牺牲
 * "深树也能退很多步"。
 */
export const HISTORY_BYTES_LIMIT = 10 * 1024 * 1024;

/** 保存的结果分类：界面据此决定提示什么，而不是把所有失败都说成"保存失败"。 */
export type SaveResult = 'saved' | 'conflict' | 'invalid' | 'empty' | 'busy' | 'failed';

/** 节点上显示的短标题：按码点截断，超出加省略号。
 *
 * 生成侧现在把 `label` 限制在 16 字以内（契约 `GENERATED_LABEL_MAX_CHARS`），所以
 * 这个截断**对新图不再生效**——它是给旧图留的兜底：库里那些 20–30 字的长标题必须
 * 照常画得下，不能被"我们改好了生成侧"这件事连累。
 */
export function displayTitle(node: MindNode): string {
  const chars = Array.from(node.label.trim());
  return chars.length > TRUNCATE_CHARS ? `${chars.slice(0, TRUNCATE_CHARS).join('')}…` : chars.join('');
}

/**
 * 详情面板 / 悬停卡片里的正文：`detail → note → label`。
 *
 * 三段的语义完全不同，所以回退顺序不能调：
 *   - `detail`：这次生成产出的说明（模型写的，2–3 句）；
 *   - `note`：用户自己写的备注（生成侧永远不碰它）；
 *   - `label`：都没有时的兜底——旧图（v1/v2 早期）只有标题，那时"完整内容"
 *     就是标题本身。
 *
 * 回退顺序写反（比如 `note` 优先）的后果是：用户给一个已有详情的节点随手写了句
 * 备注，画布与导出就只剩那句备注了——而 detail 还在数据里，只是没人显示。
 */
export function displayBody(node: MindNode): string {
  const detail = node.detail?.trim();
  if (detail) return detail;
  const note = node.note?.trim();
  return note && note.length > 0 ? note : node.label.trim();
}

/** 正文本当前来自哪一段。详情面板据此给出"这一段是什么"的说明。 */
export type BodySource = 'detail' | 'note' | 'label';

export function bodySource(node: MindNode): BodySource {
  if (node.detail?.trim()) return 'detail';
  if (node.note?.trim()) return 'note';
  return 'label';
}

/**
 * 一次生成相对生成前那条画布的一条改动。
 *
 * 存在的理由：生成**直接落画布**（没有候选确认这一步了），于是"刚才那 25 秒动了
 * 什么"变成了必须补上的一件事——画布整棵树换了一遍，用户认得的那张图还在，但
 * 哪几支被改了、哪几个节点没了，光看画布看不出来。没有这份清单，用户只能靠在
 * 撤销栈里试一步来猜。
 */
export interface CandidateChange {
  /**
   * 改动种类。前四种是**节点**：`added` / `removed` / `updated` / `moved`；
   * 后两种是**附加边**（`edge_added` / `edge_removed`）。
   *
   * 边单独两种而不是复用 `added` / `removed`：`id` 的语义不同（边 id vs 节点 id），
   * 而清单上要能一眼分清"这张图多了几个节点"与"多了几条连线"——前者是要看内容
   * 的地方，后者是要看流程走向的地方。混在一起时，一条"新增关系"会被读成
   * "新增节点"，用户点它跳不到任何地方。
   */
  kind: 'added' | 'removed' | 'updated' | 'moved' | 'edge_added' | 'edge_removed';
  /** 节点 id（前四种）或边 id（后两种）。 */
  id: string;
  /** 显示用标题：更新/移动取**新**标题，删除取原标题。边则显示「A → B（是）」。 */
  label: string;
  /** 一句话说明改了什么（"标题 → 新标题"、"颜色 blue→red"、"移到「甲」下"）。 */
  detail: string;
  /**
   * 这条改动**碰到了哪几个节点**（节点改动是一个，边改动是两个端点）。
   *
   * 它只服务一件事：判断这条改动落没落在用户当时的选区里。用 `id` 判不行——
   * 边的 `id` 是边 id，与节点 id 是两套命名空间；`added` 的 `id` 是一个新 id，
   * 在基线树里根本查不到，而它确实长在用户选中的那棵子树下面。
   */
  nodes: string[];
}

/**
 * 带上「在不在选区里」的改动条目（`store.generatedChanges` 的返回值）。
 *
 * 为什么不把 `inSelection` 直接塞进 `CandidateChange`：那两个纯函数
 * （`diffCandidate` / `diffEdges`）回答的是"两棵树哪里不一样"，它们**看不到**
 * 选区——把布尔字段放进去就必须先填一个假值，而"假值"与"算出来是 false"
 * 在类型上分不开。用一个派生类型把两件事分开：谁需要范围，谁再算一层。
 */
export interface ScopedChange extends CandidateChange {
  /**
   * 是否落在「这次生成」带上去的选区里（见 `store.generationSelection`）。
   *
   * 没有选区时恒为 `false`：那时"范围内/外"不成立，界面也不显示这个标记
   * （`generationSelection` 为空判断）。刻意用布尔而不是三态 `null`——清单每一行
   * 都要渲染，三态会让每个读它的地方都写一遍 `=== false` 的分支。
   */
  inSelection: boolean;
}

/**
 * `MindNode` 还是 `MindNode[]`：这两种都表示"一批并列的子树"。
 *
 * 单独抽出来是因为 `diffCandidate` / `selectionScope` 要对**两份**输入各做一次归一，
 * 写在两个函数里就是两行一样的 `Array.isArray`——而它们迟早会在"传进来是不是数组"
 * 这件事上分叉（一个允许 `null`、另一个忘了）。
 */
function asRoots(value: MindNode | readonly MindNode[]): readonly MindNode[] {
  return Array.isArray(value) ? value : [value as MindNode];
}

/**
 * 多棵**并列**子树的索引（鱼骨的六根骨、流程的各阶段）。
 *
 * 树只有一棵，`indexForest([root])` 与原来的 `indexTree(root)` 逐格相同（每棵子树
 * 的根都是 `parentId: null`），所以树的 diff 结果一条都不该变。
 */
function indexForest(
  roots: readonly MindNode[],
): Map<string, { node: MindNode; parentId: string | null }> {
  const out = new Map<string, { node: MindNode; parentId: string | null }>();
  const walk = (node: MindNode, parentId: string | null) => {
    out.set(node.id, { node, parentId });
    for (const child of node.children) walk(child, node.id);
  };
  for (const root of roots) walk(root, null);
  return out;
}

/** 一个节点上"人看得懂的变化"。没有变化返回空数组。 */
function nodeFieldChanges(before: MindNode, after: MindNode): string[] {
  const parts: string[] = [];
  if (before.label.trim() !== after.label.trim()) {
    parts.push(`标题「${before.label.trim()}」→「${after.label.trim()}」`);
  }
  if ((before.detail ?? '').trim() !== (after.detail ?? '').trim()) {
    parts.push(after.detail?.trim() ? '改了详情' : '清空了详情');
  }
  if ((before.color ?? null) !== (after.color ?? null)) {
    parts.push(`颜色 ${before.color ?? '无'} → ${after.color ?? '无'}`);
  }
  if ((before.icon ?? null) !== (after.icon ?? null)) {
    parts.push(`图标 ${before.icon ?? '无'} → ${after.icon ?? '无'}`);
  }
  // `note` 归用户，生成侧碰不到它；这里仍然比一次，是为了让"某条路径改到了用户
  // 备注"这种 bug 在界面上直接看得见，而不是等着用户发现自己的字变了。
  if ((before.note ?? '') !== (after.note ?? '')) {
    parts.push('改了「我的备注」');
  }
  if ((before.source_refs?.length ?? 0) !== (after.source_refs?.length ?? 0)) {
    parts.push(`引用 ${before.source_refs?.length ?? 0} → ${after.source_refs?.length ?? 0} 条`);
  }
  return parts;
}

/**
 * 两棵树之间的差异；**没有可比基线时返回 `null`**（画布本来是空的）。
 *
 * 为什么用"两份树对比"而不是"记录收到的 op 批次"：
 *
 *  - op 帧可能丢、可能乱序（`applyOpsToDraft` 会主动丢弃对不上的批次），按 op
 *    记账得到的是"我以为改了什么"，而对比得到的是"树到底哪里不一样"；
 *  - `resumeRun` 从服务端捡回结果时一条 op 帧都没有，那时只有对比能给出差异；
 *  - 对比不依赖协议：后端将来加一种下发方式，这里一行都不用改。
 *
 * 没有基线返回 `null` 而不是"全部节点都是新增"：一张 80 节点的图会得到 80 条
 * "新增"，那不是 diff，是把整棵树重念一遍。
 */
export function diffCandidate(
  base: MindNode | readonly MindNode[] | null,
  next: MindNode | readonly MindNode[],
): CandidateChange[] | null {
  if (!base) return null;
  const before = indexForest(asRoots(base));
  const after = indexForest(asRoots(next));
  const changes: CandidateChange[] = [];

  for (const [id, entry] of after) {
    const old = before.get(id);
    if (!old) {
      changes.push({
        kind: 'added', id, label: entry.node.label.trim(), detail: '新增节点', nodes: [id],
      });
      continue;
    }
    const fields = nodeFieldChanges(old.node, entry.node);
    if (fields.length) {
      changes.push({
        kind: 'updated', id, label: entry.node.label.trim(), detail: fields.join('；'), nodes: [id],
      });
    }
    if (old.parentId !== entry.parentId) {
      const target = entry.parentId ? after.get(entry.parentId)?.node.label.trim() ?? entry.parentId : '根';
      // 移动碰到两个节点：被移动的那个，以及它的新父节点。只记前者的话，
      // "把这一支挂到你选中的节点下面"会被标成范围外——而它正是选区边缘的改动。
      changes.push({
        kind: 'moved', id, label: entry.node.label.trim(), detail: `移到「${target}」下`,
        nodes: entry.parentId ? [id, entry.parentId] : [id],
      });
    }
  }
  for (const [id, entry] of before) {
    if (!after.has(id)) {
      changes.push({
        kind: 'removed', id, label: entry.node.label.trim(), detail: '删除节点（含子树）', nodes: [id],
      });
    }
  }
  return changes;
}

/**
 * 两份**边集合**之间的差异（附加关系：条件分支 / 汇合 / 回退）。
 *
 * 与 `diffCandidate`（对比两份树）并列而不是合并进去：树对比走的是"前序遍历 +
 * 父 id"，边对比走的是"按 id 对齐的集合差"，两种算法的输入形状不同，硬塞进一个
 * 函数只会让两边都长出分支。调用方（`commitGenerated`）把两份结果拼起来。
 *
 * `nameOf` 把节点 id 换成标题：清单上写「形式审查 → 付款（是）」比写两个 32 位
 * hex 有用得多，而边本身只有 id。
 *
 * `kind` 不同的同 id 边按"改过"上报（`edge_added`），不新增第三种 kind：画布上
 * 它就是同一条线换了画法，用户要看的也是"这条线变了"。
 */
export function diffEdges(
  base: readonly MindEdge[],
  next: readonly MindEdge[],
  nameOf: (id: string) => string,
): CandidateChange[] {
  const describe = (edge: MindEdge): string => {
    const text = `${nameOf(edge.source)} → ${nameOf(edge.target)}`;
    return edge.label ? `${text}（${edge.label}）` : text;
  };
  const before = new Map(base.map(edge => [edge.id, edge]));
  const changes: CandidateChange[] = [];

  for (const edge of next) {
    const old = before.get(edge.id);
    if (!old) {
      changes.push({
        kind: 'edge_added', id: edge.id, label: describe(edge),
        detail: edge.kind === 'loop' ? '新增回退线' : edge.kind === 'branch' ? '新增条件分支' : '新增连线',
        nodes: [edge.source, edge.target],
      });
      continue;
    }
    if ((old.label ?? null) !== (edge.label ?? null) || (old.kind ?? null) !== (edge.kind ?? null)) {
      changes.push({
        kind: 'edge_added', id: edge.id, label: describe(edge),
        detail: `关系说明改为「${edge.label ?? edge.kind ?? '无'}」`,
        nodes: [edge.source, edge.target],
      });
    }
  }
  for (const [id, edge] of before) {
    if (!next.some(item => item.id === id)) {
      changes.push({
        kind: 'edge_removed', id, label: describe(edge), detail: '删除了一条附加关系',
        nodes: [edge.source, edge.target],
      });
    }
  }
  return changes;
}

/**
 * 选区覆盖到的节点 id 集合：用户选中的每个节点在**两棵树**里的整棵子树。
 *
 * 两棵都要算，各自补上另一种算法看不见的那一半：
 *
 *  - **生成后的树**：在选中节点下面新增的节点 id 在基线里根本不存在，只在基线算
 *    的话，最典型的"选一支、让它展开出三个子节点"会整片被标成范围外；
 *  - **基线树**：被删掉的节点在生成后的树里已经不存在，只在生成后算的话，"把选中
 *    的这一支删掉"本身会被标成范围外——那正是用户圈它的原因。
 *
 * 空选区返回空集（`withinScope` 据此一律判 false，界面也不显示这个标记）。
 */
function selectionScope(
  before: MindNode | readonly MindNode[] | null,
  next: MindNode | readonly MindNode[],
  ids: readonly string[],
): Set<string> {
  const scope = new Set<string>();
  if (ids.length === 0) return scope;
  for (const group of [before, next]) {
    if (!group) continue;
    for (const root of asRoots(group)) {
      for (const id of ids) {
        const hit = findNode(root, id);
        if (!hit) continue;
        for (const owned of subtreeIds(hit)) scope.add(owned);
      }
    }
  }
  return scope;
}

/**
 * 一条改动算不算"在选中的范围内"：它碰到的**每个**节点都在选区子树里。
 *
 * 用"每个"而不是"任意一个"：一条从选中节点连到别处的边，改动确实有一半在选区
 * 里，但用户需要知道的是"它伸出去了"——按"任意"判就会把这种跨界的改动混进
 * 范围内那一堆，而那正是这次要给用户看见的东西。
 */
function withinScope(change: CandidateChange, scope: Set<string>): boolean {
  if (scope.size === 0 || change.nodes.length === 0) return false;
  return change.nodes.every(id => scope.has(id));
}

/**
 * agent 工具名 → 用户读得懂的说法。
 *
 * 映射只此一份：面板（要带耗时）与状态行（不带）都从这里取。两处各写一份的话，
 * 同一件事迟早出现两种措辞，用户在两个位置读到不同的词。
 */
const AGENT_TOOL_LABELS: Record<string, { running: string; done: string; name: string }> = {
  // `name` 是这个动作的短名（"生成导图"），只在没有进度可言的那一行用——
  // 工具失败时那行写的是「生成导图 未成功」，不能把 `mindmap_generate` 摆给用户。
  mindmap_generate: { running: '正在定框架', done: '已定好框架', name: '生成导图' },
  mindmap_generate_deep: { running: '正在分部生成', done: '已分部生成', name: '生成导图' },
  node_expand: { running: '正在展开分支', done: '已展开分支', name: '展开节点' },
  rag_query: { running: '正在查资料', done: '已查好资料', name: '查资料' },
  mindmap_edit: { running: '正在改结构', done: '已改好结构', name: '修改导图' },
  mindmap_fill_details: { running: '正在补写说明', done: '已补写说明', name: '补写说明' },
  audit_tree: { running: '正在检查结构', done: '已检查结构', name: '检查结构' },
  // 四个细粒度工具。`edit` 意图下模型可能连调几次（一次改一处），所以文案要能
  // 让人看出"它在逐个改"，而不是以为同一件事被重复执行了。
  add_node: { running: '正在加节点', done: '已加节点', name: '加节点' },
  update_node: { running: '正在改节点', done: '已改节点', name: '改节点' },
  remove_node: { running: '正在删节点', done: '已删节点', name: '删节点' },
  move_node: { running: '正在移动节点', done: '已移动节点', name: '移动节点' },
  // 两条边工具。文案要让人看出"它在连线"，而不是"它在改结构"——连线不改层级，
  // 用户盯着画布时看到的是"多了一个箭头"，不是"某个节点换了位置"。
  add_edge: { running: '正在连关系', done: '已连关系', name: '加连线' },
  remove_edge: { running: '正在删关系', done: '已删关系', name: '删连线' },
};

/**
 * 工具进度的人话文案。
 *
 * 未知工具名原样返回，不硬翻：模型新接一个工具时，界面出现英文也比出现一个
 * 编出来的中文词强——至少排查时能直接对上后端日志。
 */
export function agentToolLabel(name: string, phase: 'running' | 'done'): string {
  const pair = AGENT_TOOL_LABELS[name];
  if (!pair) return name;
  return phase === 'running' ? pair.running : pair.done;
}

/**
 * 工具的动作短名（"生成导图"），给「…未成功」这类没有进度可言的行用。
 *
 * 与 `agentToolLabel` 同一张表：失败时也不该让 `mindmap_generate` 这种函数名
 * 出现在用户面前。未知工具名仍原样返回，理由同上——不硬编一个中文词。
 */
export function agentToolName(name: string): string {
  return AGENT_TOOL_LABELS[name]?.name ?? name;
}

/**
 * 一次 agent 运行里后端报出来的告警。
 *
 * 分片生成可能返回 `ok=true` 但结果不完整：`result_summary.partial` 为真，
 * 或带一串 `diagnostics`（BRANCH_FAILED / BRANCH_SKIPPED_BUDGET / DUPLICATE_POINT…）。
 * 只看 ok 的话，「3 个分支失败」和「全部成功」在界面上长得一模一样，
 * 用户点了接受才发现树缺一块。两种信号都收进同一个列表。
 */
export interface AgentWarning {
  /** partial=分片没跑完；diagnostic=后端给的某条具体诊断 */
  kind: 'partial' | 'diagnostic';
  /** 诊断码（如 BRANCH_FAILED）；partial 项固定为 PARTIAL */
  code: string;
  message: string;
}

/** 告警上限：面板只显示前几条并折叠，这里防的是后端一路刷屏。 */
const AGENT_WARNING_LIMIT = 20;

/**
 * 预览树里一个节点。
 *
 * 没有 id：`node_grow` 那一侧的事件里本来就没有 id（展开调用的响应只有 label
 * 和 expandable），位置全靠路径。为了渲染顺手编一个假 id 没有意义——它一旦
 * 混进 content 就会变成一个后端不认识的节点。
 */
export interface PreviewNode {
  label: string;
  /** 模型写出的可展开性；null 表示还没写到，界面不显示任何标记。 */
  expandable: boolean | null;
  children: PreviewNode[];
}

/**
 * 预览节点 id 的前缀。
 *
 * 预览要进布局就得有 id（`layoutTidy` 用它做 memo 的键，扁平化用它建父子映射），
 * 但 `node_grow` 帧里根本没有 id。前缀把派生 id 和真实 id 分开：真实 id 由模型或
 * 后端给出，将来两者若出现在同一份数据里，靠前缀就能一眼认出哪些不可编辑。
 */
export const PREVIEW_ID_PREFIX = 'preview-';

/**
 * 预览节点的派生 id：根是 `preview-root`，其余按「从根出发的子节点索引链」拼。
 *
 * 不用递增计数器：帧是增量到达的，计数器会随插入顺序变化，同一棵树两次渲染就
 * 会拿到不同的 id，布局缓存与 Vue 的 key 一起抖（节点闪一下、视图跳一下）。路径只
 * 取决于节点在树里的位置，同一棵树永远得到同一套 id。
 */
export function previewNodeId(path: readonly number[]): string {
  return path.length === 0 ? `${PREVIEW_ID_PREFIX}root` : `${PREVIEW_ID_PREFIX}${path.join('-')}`;
}

/**
 * 预览树 → 布局能直接吃的 `MindNode` 树，顺手把可展开性收进 `expandable`。
 *
 * `note` / `source_refs` 一律不填：预览帧里没有正文与引用，编一份假的会让悬停卡片
 * 和详情面板显示不存在的出处。两者都是契约里的可选字段，缺省时 `displayBody()`
 * 回退到 `label`——预览节点显示的永远是它自己的文字。
 */
function previewSubtree(
  node: PreviewNode,
  path: number[],
  expandable: Map<string, boolean | null>,
): MindNode {
  const id = previewNodeId(path);
  expandable.set(id, node.expandable);
  return {
    id,
    label: node.label,
    children: node.children.map((child, index) =>
      previewSubtree(child, [...path, index], expandable)),
  };
}

/**
 * 等待父节点的帧最多攒几条。
 *
 * 正常运行时并发展开的 slot 只有个位数；攒到几百条只可能是父节点的帧一直没来
 * （丢帧或后端换了语义），这时继续攒只是把内存和重放成本一起拖长。
 */
const PREVIEW_PENDING_LIMIT = 200;

/**
 * 从 `tool_end` 的 `result_summary` 里摘出"这次结果不完整"的告警。
 *
 * `result_summary` 是后端 `ToolResult.data` 原样透传，键形态不走类型契约
 * （`diagnostics` 在 types.ts 里就是 unknown[]），所以这里逐字段收窄，
 * 不假设形状——模型工具换一版数据结构时，面板最多少显示几条，不会崩。
 */
function agentWarningsFrom(summary?: Record<string, unknown>): AgentWarning[] {
  if (!summary) return [];
  const out: AgentWarning[] = [];
  if (summary.partial === true) {
    out.push({
      kind: 'partial',
      code: 'PARTIAL',
      message: '有分支没能生成完整，结果可能缺内容。',
    });
  }
  const raw = summary.diagnostics;
  if (Array.isArray(raw)) {
    for (const item of raw) {
      if (!item || typeof item !== 'object') continue;
      const entry = item as Record<string, unknown>;
      const message = typeof entry.message === 'string' ? entry.message.trim() : '';
      if (!message) continue;
      out.push({
        kind: 'diagnostic',
        code: typeof entry.code === 'string' ? entry.code : '',
        message,
      });
    }
  }
  return out;
}

export interface MindNodeData {
  /** 截断后的短标题，画在节点框里 */
  label: string;
  /**
   * 悬停卡片里的正文：`detail → note → label`（见 `displayBody`）。
   *
   * 画布上**只画 `label`**，正文不再往节点框里挤——这正是"画布上短、点开看长文"
   * 的那条分界。`body` 只服务悬停卡片（用户没点开时的快速预览）。
   */
  body: string;
  /** 完整 label（不截断），供 title 属性与无障碍读屏使用 */
  fullLabel: string;
  /** 正文来自哪一段（detail / note / label）。卡片据此决定要不要标"我的备注"。 */
  bodySource?: BodySource;
  depth: number;
  /** 需要渲染的源句柄：子节点分布在哪几侧就要哪几个（双向布局的根两侧都有） */
  out: ChildSide[];
  /** 需要渲染的目标句柄：连线从父节点进来时落在哪一侧 */
  in: ChildSide[];
  /** 这个节点有几条来源引用；画布角标用，0 或缺省都不显示角标 */
  refs?: number;
  /**
   * 这一帧画的是还没进画布的树：`preview` 是生成中（节点一个一个长出来）。
   * 缺省表示它就在 `content` 里，也就是用户的作品——生成结果落地之后也在这一档，
   * 因为它**已经是**画布上的内容了（只是还没保存）。画布据此弱化样式。
   */
  draft?: 'preview';
  /** 预览里模型明确标了「不用再展开」的节点，再弱一档；`null` 与 `true` 都不额外标记。 */
  previewLeaf?: boolean;
  /**
   * 语义色键（`blue` / `green` / …）。渲染层把它映射到 CSS 变量，色值不在数据里。
   * 缺省 `undefined` 表示"没有语义色"，节点保持原来的中性样式。
   */
  color?: NodeColor;
  /** 图标（目前是 emoji）。画在 label 前面。 */
  icon?: string;
  /**
   * 这个节点在图里扮演什么（`role` 契约字段）。**只有流程图会把它带到这里**：
   * 树与鱼骨上它是 `undefined`，渲染路径与加这个字段之前完全相同。
   */
  role?: NodeRole;
  /**
   * 画布形状。`undefined` / `rect` 走原来那条矩形渲染路径；`diamond` 是判定节点
   * （见 `canvas/layout/flow.ts` 的 `DECISION_SHAPE_NOTE`），`pill` 是起点/终点。
   *
   * 形状由**布局结果**决定（`layout.sizes` 存在才可能有非 `rect`），不是直接读
   * `role`：一张 `diagram_type=flowchart` 但形状其实是树的历史记录里，也可能有节点
   * 带着 `role`，那时布局走的是树——形状必须跟着**实际跑的布局**，否则菱形会画在
   * 一套按矩形排的坐标上。两边各判一次是这类错位的标准来源。
   */
  shape?: NodeShape;
  /**
   * 外接框（px）。缺省 `NODE_SIZE`。判定节点更大（`flow.DECISION_SIZE`）。
   *
   * 渲染层用它设 DOM 尺寸，`position` 用它算左上角——同一个数只有一个来源，
   * 否则节点会被画偏半个差值，而连线仍按布局坐标画。
   */
  size?: NodeSize;
  /**
   * 这个节点在**这次生成的改动清单**里被点名（新增除外——新增的节点在画布上还没
   * 有位置）。
   *
   * 只对 `edit` / `expand` 意图有意义：那两个意图是"在当前树上改几处"，落地后画布
   * 与生成前几乎一模一样，不给一个视觉标记，用户只能靠撤销栈试一步才知道哪几处
   * 被动过。清单在 `commitGenerated` 里冻结一次（`generatedChanges`）。
   */
  changed?: boolean;
  /**
   * 视图层收起：这一屏**还有几个直接子节点没画出来**。
   *
   * 与 `expandable` 不是一回事，别混：`expandable` 说的是"模型还能不能再长"
   * （三态，跟着数据走），这个数说的是"这一屏少画了几个"（跟着视图走）。
   * 一个节点可以 `expandable: 'no'`（内容到头了）却仍然有 18 个子节点被截在预算外。
   *
   * `undefined` = 这一屏没藏它的任何直接子节点（画布上绝大多数节点是这样），
   * 渲染层据此不画 `+N` 角标。
   */
  hiddenChildren?: number;
  /** 与 `hiddenChildren` 同一次截断：整棵子树里还有多少节点没画出来。给 `title` 与读屏用。 */
  hiddenSubtree?: number;
  /**
   * 这个节点的子节点正显示着，而且是**用户要求**显示着的（点开过，或点了「全部展开」）。
   *
   * 渲染层据此给一枚「−」角标：那是"再点一下叠回去"的入口。与 `hiddenChildren` 互斥
   * ——一个节点的子节点要是被截断过，它就不可能"已经全显示"。
   */
  foldOpen?: boolean;
  /**
   * 这一屏上这个节点**正显示着几个子节点**（视图树里的 `children.length`）。
   *
   * 只服务一件小事：渲染层判"有没有东西可以叠回去"——0 就是叶子，`−` 角标不该出现。
   * 不从 `hiddenChildren` 反推：两者都为空时，"叶子"与"孩子全靠预算显示着"是两种
   * 不同的状态，反推会把它们混成一种。
   */
  visibleChildren?: number;
  /**
   * **一级分支**这一支在讲什么（8–12 字，生成时调的模型）。
   *
   * 只在一级分支（`depth === 1`）上有值，其余节点恒为 `undefined`。与 `label` 并列
   * 显示在节点上方那枚角标里，说的是"这一支的范围"，而 `label` 说的是"这一支叫什么"
   * ——两者重合时这枚角标就是噪声，那正是 `branchHidden` 与它必须同时成立的原因。
   */
  branchDigest?: string;
  /**
   * **一级分支**整支还有几个节点没画（含更深处）。
   *
   * 与 `hiddenSubtree` 的区别是覆盖面：那个只在"这个父节点自己有直接孩子没画"时
   * 存在（`+N` 的判据），这个是"这一支下面任何一处有节点没画"就存在。角标说的是
   * "这一支还有 21 个"，那 21 个可以全在三四层深处、中间没有一处断口。
   */
  branchHidden?: number;
}

export interface FlatNode {
  node: MindNode;
  depth: number;
  parentId: string | null;
}

/**
 * `runGeneration` 的可选参数。
 *
 * 只有一个字段也走对象，不再加第六个位置参数：那个签名已经有五个位置参数了，
 * 第六个数组读代码时得数一数才知道对应哪个。
 */
export interface RunOptions {
  /**
   * 这次运行**显式**指定的选区，覆盖 store 里当前的选中。
   *
   * 存在的理由只有一条：**看得见的才发**。画布右上角那个框旁边挂着「已选 N 个
   * 节点」徽标，它按徽标上的数把选区带上；而将来任何不显示选区的入口（快捷动作、
   * 测试直调）必须显式传 `[]`——否则它会静默带着用户在画布上圈的节点去"重新生成"，
   * 而用户看不到这件事正在发生。
   */
  selection?: string[];
  /**
   * 这一次运行**实际带着哪些偏好**（`["diagram_type.default=fishbone"]`）。
   *
   * 只由界面那层填：只有它知道"下拉里这个值是偏好预填的、用户没动过"。`store`
   * 不自己算——它看不出一个值和它的来源，把推断写在这里就会和界面各算一份。
   *
   * 落进 `runs.request.prefs_applied`，是偏好面板上"上次生效"的唯一数据源。
   */
  prefsApplied?: string[];
  /**
   * 「这一次」走不走推理模型（镜像后端 `AgentRunRequest.deep_thinking`）。
   *
   * 与 `prefsApplied` 一样只由界面那层填：它是**这一次的开关**，不是设置——用户
   * 会在"随便出一张"和"这段逻辑要拆准"之间来回切，写死进 store 就变成了一个没有
   * 出口的全局状态。缺省 `false`，发出去的请求与加它之前逐字节相同。
   */
  deepThinking?: boolean;
  /**
   * 沿用哪一次运行的 `run_id`。
   *
   * 只有一个调用方需要它：把一份检查点补完说明。那次生成在写说明之前断了，服务端
   * `runs` 里那一行的状态是 `cancelled` / `failed`；`begin()` 对这两种状态是**重开**
   * 同一行（只清 `partial` 标记，不动 `candidate`），于是补完的树会覆盖同一行，
   * 台账上这是"同一次生成的下半场"而不是凭空多出来的一次。
   *
   * 换一个新 id 的代价不是多一行记录：`resumeRun()` 捡回来的东西会被记成两次运行，
   * 而用户看到的明明是同一张图。缺省 `undefined` = 老行为（每次点击新开一个身份）。
   */
  runId?: string;
}

/**
 * 上一次真正发出去的请求，留着给「重试」用。
 *
 * 只活在内存里：刷新页面之后没什么可重试的——服务端 `runs` 表那条路走的是
 * 「上次跑完但没保存」的结果载入（`probeResumableRun`），不是重发请求。两者
 * 解决的也不是同一件事：这里要的是"把刚才那句话再跑一遍"。
 *
 * 存的是**规整后的参数**而不是原始的 `arguments`：`runGeneration` 有五个位置参数
 * 加一个对象，原样转发会在加了默认值之后悄悄改变行为。
 */
export interface RunAttempt {
  goal: string;
  intent: 'create' | 'expand' | 'edit';
  sources?: Source[];
  thinkingModel: string;
  diagramType: DiagramTypeChoice;
  options: RunOptions;
}

/**
 * 单次请求最多带几个选中 id。
 *
 * **与后端 `AgentRunRequest.selected_ids` 的 `max_length=50` 同一个数**，两处一个
 * 值：前端先截断，用户就不用为"按着 Ctrl 点了一整片"收一条 422。改这里时后端
 * 那个字段要一起看，反过来也一样。
 */
const MAX_REQUEST_SELECTION = 50;

/**
 * 画布节点。Vue Flow 的 `Node` 输入类型里没有 `selected`（它把选中当内部状态），
 * 但 `:nodes` 变化时 applyChanges 会把 `selected` 同步进去，所以这里补上这个字段的类型。
 */
export type MindFlowNode = FlowNode<MindNodeData> & { selected: boolean };

/**
 * 画布当前渲染的是哪棵树。
 *
 * 两级：`content`（用户的作品，生成结果落地之后也在这里）→ 生长中的 `agentPreview`。
 * 保存、导出、撤销、编辑全都只认 `content`——渲染源只决定"画什么"。
 *
 * **没有"候选"这一级了**：生成完成就把树装进 `content` 并标成未保存，不再有
 * "看得见但不算你的"这个中间态。代价与补偿见 `commitGenerated`。
 */
export type RenderKind = 'content' | 'preview';

interface RenderSource {
  root: MindNode;
  /**
   * 布局用的合成根 id（见 `shape.canvasTreeOf`）。树型恒为 `null`。
   *
   * 这个节点**不在 content 里**：它只在画布上代表"鱼头 / 流程起点"。所以它不可选中
   * （选中会让详情面板对着一个 `findIn()` 找不到的 id 说话），也不参与层号、
   * 节点计数与编辑。
   */
  syntheticId: string | null;
  kind: RenderKind;
  /** 预览节点的可展开性（派生 id → expandable）；非预览时是空表。 */
  expandable: Map<string, boolean | null>;
  /**
   * 附加边。**只画不参与布局**：节点坐标仍然由 `layoutTidy(root)` 决定。
   *
   * 这是"树 + 边"能成立的前提之一——边要是参与布局，用户每连一条线，整张画布
   * 都会重排，他刚记住的位置就没了。预览（生成中）没有边，是空数组。
   *
   * 流程图上这里装的是**两路**（阶段之间 + 阶段内部，见 `shape.canvasEdgesOf`）：
   * 两路都要画出来，否则契约里已经有的判定出边在画面上一条都看不见。
   */
  edges: MindEdge[];
  /**
   * **喂给布局**的那一路边：只有阶段**之间**的（缺省 = `edges`）。
   *
   * 与 `edges` 分成两个字段，而不是让布局函数自己过滤：`layoutFlow` 分不出这两
   * 层——阶段内边的端点也是节点 id，`order.has()` 一样为 true，它会老老实实把
   * 一条「是 / 否」出边当成"下一步"排进 dagre，把目标步骤推到下一层。那不叫
   * "dagre 该做的"，那是布局在吃它不该吃的东西（`Edge` 契约写明边不参与布局）。
   *
   * 树与鱼骨没有这个区别（`canvasStageEdgesOf` 与 `canvasEdgesOf` 返回同一个
   * 数组），所以那两种形状下这两个字段相等，布局输入一个字节都没变。
   */
  layoutEdges?: MindEdge[];
  /**
   * 视图层收起：被截断的节点 id → 它缺了什么。
   *
   * 只对树型有值（鱼骨与流程本来就是"每根骨 / 每个阶段几个节点"，收起没有收益，
   * 而且它们的画布树是合成出来的，数量口径会把用户没生成过的东西算进去）。
   * 空表 / `undefined` 都表示"这一屏一个节点都没截断"，渲染层不画任何角标。
   */
  folded?: ReadonlyMap<string, FoldInfo>;
  /** 子节点正显示着的节点 id → 为什么显示（渲染层据此决定给不给"叠回去"的角标）。 */
  foldOpen?: ReadonlyMap<string, FoldOpenReason>;
  /**
   * **这一支还有几个节点没画**：节点 id → 整棵子树里没画出来的节点数。
   *
   * 与 `folded` 的区别见 `view-collapse.ts::ViewTreeResult.unshown`：`folded` 只说
   * "这个父节点自己有直接孩子没画"，这一份连"孩子都画了、但孙子被截"的支也算进来。
   * 一级分支上的概括角标要的是后者——它说"这一支还有 21 个"，而中间可能没有一处断口。
   */
  unshown?: ReadonlyMap<string, number>;
  /**
   * 一级分支 id → **这一支在讲什么**（8–12 字，生成时调的模型，落库在
   * `content.branch_digests`）。
   *
   * 原样透传 `content` 里那份 `Record`（不转 Map）：渲染层每个节点只查一次
   * `digests?.[id]`，而转换要在每次内容变化时重建一份 Map —— 为一次取值建一张表
   * 不划算。键可能指向已经不存在的节点（契约层不校验），取值必须容忍 `undefined`。
   */
  digests?: Readonly<Record<string, string>>;
}

/** 共用的空表：非预览来源不需要这份映射，别每次重算都新建一个。 */
const NO_EXPANDABLE = new Map<string, boolean | null>();

/**
 * 两个共用空表，给"这张图一个节点都没被截断"的那条路用。
 *
 * 必须是常量而不是每次 `new Map()`：`nodes` 这个 computed 每次重排都会逐节点跑一遍，
 * 在循环里新建空表等于让每个节点都产出一份垃圾。
 */
const NO_FOLDS: ReadonlyMap<string, FoldInfo> = new Map<string, FoldInfo>();
const NO_FOLD_OPEN: ReadonlyMap<string, FoldOpenReason> = new Map<string, FoldOpenReason>();

/**
 * 某个阶段**自己**的附加边条数（`stages[i].edges.length`）。
 *
 * 上限判据必须落在这个数上，而不是 `stageInnerEdgesOf()` 的摊平结果：后者是全部
 * 阶段的合计，用它判 200 等于"所有阶段共享一份额度"，与后端"每个数组各判 200"
 * 的口径正好相反（`ops._apply_add_flow_edge` 的 `len(sibling)`、契约
 * `FlowContent.validate_flow` 逐阶段调的 `_validate_edges`）。
 *
 * `stageId` 为 `null`（不是流程图）时返回 0：调用方只在"两端点同属一个阶段"时用它，
 * 那时 `stageId` 必定非空，这里只是把类型收窄成一个可判的空值。
 */
function innerEdgeCount(content: DiagramContent, stageId: string | null): number {
  if (!stageId || !isFlow(content)) return 0;
  return content.stages.find(stage => stage.id === stageId)?.edges?.length ?? 0;
}

function flatten(root: MindNode): FlatNode[] {
  const out: FlatNode[] = [];
  const walk = (node: MindNode, depth: number, parentId: string | null) => {
    out.push({ node, depth, parentId });
    for (const child of node.children) walk(child, depth + 1, node.id);
  };
  walk(root, 0, null);
  return out;
}

/**
 * 还没跑过任何一轮时的状态行。
 *
 * 导出成常量是为了让界面能判断"这条状态是不是还没有内容"：画布右上角那个框在
 * 初始状态下已经有一行提示了，再摆一句"写一个主题后生成"就是在同一个框里说两遍
 * 同一件事。
 */
export const INITIAL_STATUS = '写一个主题后生成，节点会边生成边出现。';

export const useMindMapStore = defineStore('mindmap', () => {
  // 只为了在收到 `sources` 帧时把来源 id 换成持久 id（见 runGeneration）。
  // 依赖方向是单向的：sources 不认识 mindmap，反过来才会。
  const sourcesStore = useSourcesStore();
  /**
   * 画布上那份内容。
   *
   * **声明类型是 `MindMapContent`，运行时可能装着鱼骨图或流程图。** 这不是笔误：
   * 全仓几十处既有读点（以及 700 多条用例）全都按树读 `content.root`，把声明换成
   * 联合类型会让"树的这条路径"在一夜之间多出上百处 narrowing 噪音，而真正需要按
   * 形状分派的地方（渲染、遍历、定位、写回、校验）已经全部收进 `mindmap/shape.ts`
   * 了。两件事因此定死：
   *
   *  1. 装内容一律走 `slot()`（唯一一处有意的类型断言，见它的注释）；
   *  2. **新代码不要再直接摸 `content.root`**——判形状用 `kindOf()`，取值用
   *     `findIn()` / `rootsOf()` / `containerOfNode()`，布局用 `canvasTreeOf()`。
   */
  const content = ref<MindMapContent | null>(null);

  /**
   * 把任意一种形状装进 `content`。
   *
   * 这是全仓**唯一**一处"类型说树、实际可能是别的形状"的断言，收在一个函数里是为了
   * 让它可数、可查：任何一次新的内容赋值出现在别处，都该被当成"绕过了形状分派"来
   * 审。运行时不做任何转换——三种形状原样装进去，`shape.ts` 按形状自己认得出来。
   */
  function slot(next: DiagramContent | null): MindMapContent | null {
    return next as MindMapContent | null;
  }
  /**
   * 这张图是怎么来的。顶栏那一小块徽标按它换文案。
   *
   * `import` 是新增的一档：导入进来的图既不是示例（`demo`），也不是模型生成的
   * （`model`），更不是"已保存导图"（`saved`，那个意味着从库里打开、有 mapId）。
   * 它**没有 mapId**、`dirty` 为真——第一次保存会创建一条新记录。混进任何一档
   * 都会在界面上说错话：「已保存导图」会让人以为导入的内容已经在库里了。
   */
  const mode = ref<'demo' | 'model' | 'saved' | 'import' | null>(null);
  const status = ref(INITIAL_STATUS);
  const stageText = ref('');
  const waitedMs = ref(0);
  const busy = ref(false);
  const selectedId = ref<string | null>(null);
  /**
   * 追加选区（主选区之外的那几个）。
   *
   * 为什么要主 + 追加，而不是一个 `string[]`：详情面板、面包屑、Tab/Enter/Delete
   * 快捷键、`expand` 请求全都只认**一个**节点，它们区分不了"哪个是我正在看的那
   * 一个"。把主选区继续放在 `selectedId` 上，那些路径一行都不用改；多出来的这层
   * 只服务一件事——「改这张图」那句话是对谁说的（`selectedIds`）。
   *
   * 必须是列表而不是单个"上一个选中"：用户圈三个节点说「这几个合并成一个」时，
   * `selected_ids` 要发三条，模型才不用从整棵树里猜是哪三个。
   */
  const extraSelectedIds = ref<string[]>([]);
  /**
   * 朝向是 content 的派生值（layout + theme.direction），不单独存一份：
   * 否则保存后再打开，画布上的朝向与库里的记录会各说各话。
   */
  const direction = computed<Direction>(() => directionOf(content.value));
  /** 用户手动平移/缩放后不再自动 fitView，否则每次节点增长都会把视图拽回去。 */
  const viewAdjusted = ref(false);

  /**
   * 画布上**最多画几个节点**（`0` / 负数 = 不限制，见 `canvas/view-collapse.ts`）。
   *
   * **这是视图状态，不是内容。** 它不进 `content`、不进撤销栈、不进导出文件：
   * 同一个文件在 13 寸屏与 4K 上、在"只想看目录"与"要逐条核对"时，合适的节点数不同，
   * 那是"这一屏怎么看"，不是作品属性（理由见 `docs/VIEW-COLLAPSE.md` §三）。
   *
   * 初值取常量而不是 `Infinity`：换图那一瞬间画布还没量出尺寸，先用产品定的那一档
   * 画出来。**它不随视口变化**——上限是认知预算，同一张图在任何屏幕上显示同一批节点；
   * 字号由 `clearity.chooseViewMode` 那两档管，两件事分开。
   */
  const visibleNodeLimit = ref<number>(MINDMAP_VISIBLE_NODE_LIMIT);

  /**
   * 用户**点开过**的节点（无视预算，自己的全部子节点一律显示）。
   *
   * 用不可变集合（每次重建一份）而不是就地 `add`：这个集合的读者是 `renderSource`
   * 那个 computed，就地改一个 `Set` 不会触发重算，而画布上的表现就是"点了没反应"。
   */
  const viewExpandedNodes = ref<ReadonlySet<string>>(new Set<string>());

  /**
   * 用户**收起过**的节点（一个子节点都不画，哪怕预算还够）。
   *
   * 与 `viewExpandedNodes` 分两个集合而不是一个三态 map：两边各自"加一个 / 删一个"，
   * 合并成一个 map 的话，每次动作都要读旧值再写新值，多一处能写错的地方。
   * 两条集合由 `expandNodeInView` / `collapseNodeInView` 互相清掉，所以同一个 id
   * 永远只会出现在一边。
   */
  const viewCollapsedNodes = ref<ReadonlySet<string>>(new Set<string>());

  /**
   * 用户点了「全部展开」：这一屏就是整棵树。
   *
   * 用**一个布尔**而不是把每个 id 都塞进 `viewExpandedNodes`：那样"全部展开"会变成
   * 一百多条必须逐个撤销的状态，用户想再点一支收起来时，还得先判断"它是不是被全部
   * 展开塞进去的"。布尔 + 逐节点的 `collapsed` 让「全部展开之后逐个叠回去」只是
   * 一次 `collapsed.add`。
   */
  const viewExpandAll = ref(false);

  /** 正在内联编辑的节点 id；null 表示没有编辑框打开。 */
  const editingId = ref<string | null>(null);

  /**
   * 标题输入框里还没提交的文字。
   *
   * 标题走 `:value` + `@change`，用户打字时 DOM 的 value 变了但 content 没变，
   * 也碰不到 editingId。没有这个草稿，上传生成完成后标题会被直接换掉，
   * 那半截输入一声不响就没了。
   */
  const titleDraft = ref<string | null>(null);

  /** 保存相关状态：mapId 为空表示这份导图还没存过。 */
  const mapId = ref<string | null>(null);
  const version = ref(0);
  const savedAt = ref<string | null>(null);
  const dirty = ref(false);

  /** 撤销/重做存整树 JSON 快照：比命令模式少一半代码。节点上限 1000、每个节点还带正文，一份快照能到几十到几百 KB，只按 50 步计会涨到十几 MB，所以除了步数还按字节截（10MB）。 */
  const past = ref<string[]>([]);
  const future = ref<string[]>([]);
  /** 撤销栈已占的字节数。维护这个数，免得每次压栈都把整栈重新编码一遍。 */
  const pastBytes = ref(0);

  /**
   * 还没提交的改动（算法与取舍见 `mindmap/edit-log.ts`）。
   *
   * 攒到保存时一起发（`save()`），**保存成功才清**：失败就清掉的话，那批事件永远
   * 丢了，而用户以为已经记下。不保存就关页面 → 事件丢失，这是可接受的——那次编辑
   * 本来也没保存，日志不该比内容更"诚实"。
   */
  const pendingEdits = ref<EditEvent[]>([]);

  /**
   * 改动**之前**那棵树，以及这次改动该记到谁头上。
   *
   * `pushHistory()` 设置、`collectEdits()` 消费并清空。抄引用而不是抄序列化：
   * `applyContent` 每次都是整体替换 `content.value`，旧树不会被后续修改带跑。
   *
   * `content` 允许为 `null`：画布本来是空的时那一步没有"改动之前那棵树"，但差分
   * 本身会返回空数组（整棵树都是新的，不是 diff）——那条判断在 `diffEdits` 里。
   */
  let editBase: {
    content: DiagramContent | null;
    actor: EditActor;
    runId?: string;
  } | null = null;

  const saving = ref(false);

  /**
   * 每次已提交的编辑都递增。生成落地时靠它判断"这棵树是不是基于一棵已经被改过的
   * 画布算出来的"——用 revision 比比较整树引用更便宜，也比比 dirty 更准
   * （dirty 只说明"有改动"，不说明"改动发生在生成开始之后"）。
   */
  const clientRevision = ref(0);

  /**
   * 生成过程中的**工作副本**：op 批次与 `snapshot` 帧都拼在这上面。
   *
   * 它**不是** content，而且生命周期只有一轮运行：
   *
   *  - 生成期间不能直接改 `content`：`ops` 是一次几处的增量（`edit` 意图下模型可能
   *    连调十几次），每条都落画布就等于把 25 秒的中间态全摊给用户看，撤销栈也会被
   *    切成十几步半成品；
   *  - 但它也**不是**用户看到的那棵树：`done` 帧一到就整树装进 `content`
   *    （`commitGenerated`），中间没有任何确认动作。
   *
   * 所以它的对错只影响"这一轮生成有没有拼对"，不影响"用户的作品是什么"——那件事
   * 到 `commitGenerated` 那一刻才发生，且可撤销。
   */
  const draft = ref<DiagramContent | null>(null);

  /**
   * 生成开始那一刻的画布引用。**只在一轮运行里有意义**，用于两件事：
   *
   *  1. `generatedChanges`：告诉用户"这次生成改了什么"，基线必须是生成前那棵树；
   *  2. 版本冲突判定：完成时 `clientRevision` 变过，说明期间画布被改过（正常路径上
   *     不可能——`busy` 把所有编辑入口都拦住了，但 `undo` / 将来新加的入口未必），
   *     那时**不覆盖**，把两棵树都留着让用户选。
   *
   * 抄引用而不是深拷贝：`applyContent` 每次整体替换 `content.value`，旧树不会被
   * 后续修改带跑（与 `editBase` 同一条理由）。
   */
  let generationBase: DiagramContent | null = null;
  /** 生成开始时的 `clientRevision`，完成时用来判"期间画布被改过"。 */
  let generationBaseRevision = 0;

  /**
   * 生成开始时所处的导图会话号（见 `mapSession`）。
   *
   * `clientRevision` 只在编辑时递增，切图（loadMap / newMap / preview）不碰它。
   * 只看 revision 的话："生成 A → 打开另一张图 B → 完成"会把 A 的内容装进 B，
   * 还沿用 B 的 mapId 与 version，下次保存就把 A 写进 B 的记录。
   */
  let generationSession = 0;

  /**
   * 这一轮生成是在哪种意图下跑出来的。
   *
   * 落画布时要按它区分两件事：`create` 是一份新作品，不能继承上一张图的
   * `mapId` / `version` / 历史；`expand` / `edit` 改的就是当前这张图，身份必须留着。
   */
  let generationIntent: 'create' | 'expand' | 'edit' = 'create';

  /**
   * 正在跑的这一轮是哪种意图（`agentRunning` 为假时只表示"上一次是什么"）。
   *
   * 与 `generationIntent` 分开不是冗余：那个是结果的归属（落画布时用），这个是
   * **这一轮在做什么**（按钮文字用）。两者在生成完成之后会分叉——树已经进画布、
   * 运行却还在收尾，那时按钮该说的是"展开中…"，而结果的归属早就不变了。
   */
  const runIntent = ref<'create' | 'expand' | 'edit'>('create');

  /**
   * 这一轮「改这张图」（`edit`）带上去的 `selected_ids`，原样快照。
   *
   * 为什么要存一份而不是直接读 `selectedIds`：生成的整个过程中用户随时会改选中
   * （点一下清单里的一行就会跳转并选中）。读实时选区的话，diff 上那句"在不在你
   * 选中的范围内"会随着用户点来点去而变，而那说的其实是**另一个时刻**的选区。
   *
   * 只有 `edit` 会写它：`expand` 的选区是"要展开的那一个节点"，新节点按构造就
   * 落在它下面，"范围内/外"在那条路上没有信息量。
   */
  const generationSelection = ref<string[]>([]);

  /**
   * 生成中的工作副本当前走到第几版 op（见 `draft`）。
   *
   * 每批 op 的 `base_revision` 是后端在**它那棵树**上算的序号（0 起，一批加一），
   * 这里记的是"我这份副本已经应用到这里"。两者对不上就说明中间漏了一批
   * （丢帧、乱序），在错位的树上继续套用比不套用更糟。
   *
   * 只放在 store 内部，不进返回对象：界面不需要读它，它只是消费端的一致性账本。
   */
  let draftRevision = 0;

  /**
   * 工作副本整棵作废时，把 op 版本账本一起归零。
   *
   * 零是后端每一轮的新起点（`RunContext.revision` 从 0 起算）。漏掉这一步，
   * 下一轮的第一批 op 会因为 `base_revision=0` 对不上旧账本而被整批丢掉，
   * 表现出来就是"改了半天画布一点没动"。
   */
  function resetDraftOps() {
    draftRevision = 0;
  }

  /** 给面板显示的工具进度与阶段。 */
  const agentEvents = ref<AgentEvent[]>([]);
  const agentRunning = ref(false);
  const agentError = ref('');

  /**
   * 失败分类用的两个字段，与 `agentError` 的文案分开存。
   *
   * `agentError` 只够"说清发生了什么"，说不出"接下来该怎么办"：同一个 `message`
   * 可能来自重试有意义的失败，也可能来自重试一百次都一样的配置缺失。判据在
   * `mindmap/failures.ts`，这两个字段是它的输入。
   *
   * 为什么不用 `agentEvents` 里那帧 `error`：面板拿的是**最后一次运行**的失败，
   * 而事件数组还留着前几轮的帧；从这里取不会串。
   */
  const agentErrorCode = ref('');
  const agentErrorLimit = ref('');

  /**
   * 上一次真正发出去的请求，只给「重试」用（内存态，刷新即失效）。
   *
   * 记在 store 而不是组件里：将来不止一个入口会用到重试（面板、状态行、快捷键），
   * 而组件卸载（抽屉收起来）不该让"刚才那次"跟着消失。
   */
  const lastAttempt = ref<RunAttempt | null>(null);

  /**
   * 上次跑完（或被中断）、但**结果还没被保存**的那次运行。
   *
   * 这是"run 身份"落到界面上的唯一痕迹。此前 run 只活在一个 HTTP 连接的寿命里：
   * 关掉页面，已经生成出来的结果跟着连接一起没了，用户回来什么都看不到。现在
   * 服务端把它落进 `runs` 表，进页面时问一句"有没有我还没接住的结果"。
   *
   * 只是**一条提示**，不是弹窗：用户可能正在做别的事，打断他比让他知道这件事更烦。
   * 它提醒的是一份**已经进了画布、但没保存**的改动：关掉页面丢的是那份未保存的
   * 结果，不是"一份还没被采纳的提案"——生成落地那一刻它就是用户的了。
   */
  const resumableRun = ref<RunSummary | null>(null);

  /**
   * 这次运行**真正发出去的模型名**与模型的思考过程（`done` 帧带来的两个字段）。
   *
   * 为什么留在 store 而不是只写进消息流：它们要能解释"这次为什么不一样"。
   * `runModel` 是勾了「深度思考」之后**唯一**能证明它真的换了模型的证据（配置里
   * 没给 `MINDMAP_LLM_MODEL_DEEP` 时后端会退回原模型，界面上必须看得见）；
   * `lastReasoning` 是折叠面板的数据源，成功时也有内容。
   *
   * 两者都随每次运行清空：上一轮的思考过程挂在这一次的结果上是纯粹的误读。
   */
  const runModel = ref<string | null>(null);
  /**
   * 服务端在响应里回报的模型名（`done` 帧的 `served_model`）。
   *
   * 与 `runModel` **不是**一个东西：`runModel` 是这次请求写出去的名字（我们的意图），
   * 这个是对方说它实际跑的那个。实测请求写 `deepseek-chat`、响应回 `deepseek-flash`
   * （两个名字是同一模型的两个别名，区别只在思不思考）。只显示请求名时，
   * "服务端把请求路由到了别处"这件事在界面上根本看不出来。
   */
  const runServedModel = ref<string | null>(null);
  const lastReasoning = ref('');

  /**
   * 这一次会话里已经明确表示"不要再问"了。
   *
   * **只在本会话内静音，不写库**：`dismissed_at` 那一列已经删掉（没有"丢弃候选"
   * 这个动作了），而后端每次进页面仍会返回这条——那是刻意的，因为它说的是事实：
   * 你上次生成完没保存。下次开新会话再问一次不算骚扰。
   */
  let resumableSettled = false;

  /**
   * 这次生成落进画布的那个 run，等保存成功后上报 `applied_at`。
   *
   * 从前它是"候选属于哪一次运行，应用/丢弃时上报"。现在只有一半留着：保存成功
   * 才写时间戳，因为 `applied_at` 的语义已经变成"这份结果被存下来了"——它正是
   * "下次进页面还要不要提醒"的判据。
   *
   * 用 ref 而不是普通变量：界面要读它（`ChatDock` 据此判断"上次没保存的那次"
   * 是不是**刚刚**这一次）。普通变量没有响应性，那个判断会停在上一次的值上。
   */
  const pendingSaveRunId = ref<string | null>(null);

  /** 每次点击生成一个新的 run 身份。服务端只在这个 id 相同时才做幂等。 */
  function newRunId(): string {
    // 不用 crypto.randomUUID：它在非安全上下文（http://192.168.x.x 打开的开发页）
    // 里是 undefined，而那正是最需要它的时候。时间戳 + 随机后缀的碰撞概率对
    // "一次会话里的几十次运行"绰绰有余。
    return `run-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  }

  /**
   * 问一次服务端：有没有"跑完了但我还没接住"的结果。
   *
   * 失败一声不响：进页面时后端没起来、或者 `runs` 表还是空的，都不该弹一句
   * "查询失败"——用户此刻什么也没要求。
   */
  async function probeResumableRun(): Promise<void> {
    if (resumableSettled) return;
    try {
      const pending = await listRuns();
      resumableRun.value = pending.length > 0 ? pending[0] : null;
    } catch {
      resumableRun.value = null;
    }
  }

  /**
   * 把上次那次生成的结果**直接装进画布**（并标成未保存）。
   *
   * 没有"确认"这一步：用户点「载入」就是明确要它，再让他点一次「应用」只是把
   * 已经表达过的意图问第二遍。但**仍然走 `commitGenerated`**：撤销栈、`generate`
   * 事件、身份重置、冲突判定一步都不少——不是另开一条"恢复"的旁路。
   */
  async function resumeRun(): Promise<boolean> {
    const target = resumableRun.value;
    if (!target) return false;
    // 进这一步之前的画布与版本：下面那趟请求在途时用户可能已经改了画布、或切了图，
    // 两种都得判（判据与生成落地完全同一套，见 `commitGenerated`）。
    const session = mapSession;
    const baseRevision = clientRevision.value;
    const base = content.value;
    try {
      const detail = await getRun(target.run_id);
      if (detail.candidate) {
        if (session !== mapSession) {
          // 等网络这段时间用户切了图：这份结果属于上一张，装上去就是把 A 写进 B。
          resumableRun.value = null;
          status.value = '载入期间已经换了一张图，上次的结果没有装进来。';
          return false;
        }
        generationBase = base;
        generationBaseRevision = baseRevision;
        generationSession = session;
        generationIntent = detail.intent;
      }
      if (!detail.candidate) {
        resumableRun.value = null;
        status.value = '上次的生成没有留下可用的内容。';
        return false;
      }
      agentEvents.value = [];
      agentWarnings.value = [];
      agentError.value = '';
      // 载入成功即"这次没有失败"：不跟着清的话，卡片会挂着一份几分钟前那次的
      // 解释，而用户刚刚明明把结果接住了。
      agentErrorCode.value = '';
      agentErrorLimit.value = '';
      // 半棵树被捡回来时也得说清它是半棵树。实时流那条路在 `done` 帧上写了告警，
      // 载入这条路走的是台账接口——不在这里补一笔，同一份内容"刚生成时是半棵、
      // 隔天载入就成了完整交付"，而 `finishStatus` 正是照告警决定状态行怎么写。
      if (detail.partial === true) {
        recordWarnings([
          {
            kind: 'partial',
            code: 'PARTIAL',
            message: detail.partial_reason || '有分支没能生成完整，结果可能缺内容。',
          },
        ]);
      }
      // 检查点也要说清。它**不是**半棵树（结构是完整的），缺的是每个节点的说明——
      // 不补这一笔，用户载入后会以为"模型这次什么都没写"，然后重跑一遍，
      // 而他手上那棵树其实已经能用了。用 `diagnostic` 而不是新增一种 `kind`：
      // 告警区的两种样式已经够用，"这是一条附注"正是它的位置。
      if (detail.checkpointed_at) {
        recordWarnings([
          {
            kind: 'diagnostic',
            code: 'CHECKPOINT',
            message: '这份结果是在写说明之前保住的结构，节点说明还没生成；结构可以照常用。',
          },
        ]);
      }
      clearAgentPreview();
      resetDraftOps();
      // 来历照原样带过来：占位树恢复成"模型生成"就是界面替它撒谎。
      runIntent.value = detail.intent;
      // 捡回来的结果不知道当时圈了谁（`runs` 表存的是请求摘要，没有回读这条路），
      // 所以范围标记一律按"没有选区"处理：宁可不说"范围内/外"，也不能拿此刻的
      // 选区去标一份几分钟前产出的改动——那会把用户引到错的地方去核对。
      generationSelection.value = [];
      resumableRun.value = null;
      commitGenerated(detail.candidate, {
        runId: detail.run_id,
        intent: detail.intent,
        mode: detail.mode ?? null,
        // 载入时基线是**此刻**的画布：用户看到的"这次改了什么"必须是相对他现在
        // 这张图算的，而不是相对那次运行开始时的某个已不存在的状态。
        base,
      });
      return true;
    } catch (error) {
      resumableRun.value = null;
      status.value = error instanceof ApiError ? error.message : '读取上次的结果失败，请重试。';
      return false;
    }
  }

  /**
   * 把刚载入的这份检查点**补完说明**：同一棵树上再跑一轮，只补 `detail`。
   *
   * 三个决定：
   *
   *  1. **沿用那条 `run_id`**。服务端那一行还在（状态是 `cancelled` / `failed`），
   *     `runs.begin()` 对这两种状态是"重开同一行"，只清半棵树标记、不动 `candidate`。
   *     补完的树落回同一行，台账上这是同一次生成的下半场；换新 id 会让一次生成变成
   *     两次运行，而用户看到的明明是同一张图。那一行若还在 `running`（进程没死、
   *     只是前端断过线），服务端会 409 —— 那也正是对的：真正在跑的那一轮不该被
   *     第二个请求顶掉。
   *  2. **走 `edit` 意图**，不新增意图。后端 `mindmap_fill_details` 只注册给 `edit`，
   *     而 `edit` 本来就要求带 `snapshot`——补说明要的正是"在现有这棵树上改"。
   *  3. **`goal` 是一句人话**：写说明那条路不吃用户的自由指令（`_detail_ask` 自己
   *     按节点清单发问），模型看到的是工具描述与节点清单，不是这句话。它唯一的去处
   *     是台账的 `goal`，而界面会把它念出来（`ChatDock` 的"上次…：「goal」"），
   *     所以得写成一句像标题的话，不能写成对模型下的命令。
   *
   * `runId` 由调用方显式给：它是**载入之前**从那行记录上读到的值，而载入成功会把
   * `resumableRun` 清空——在这里回读只会拿到 null，然后悄悄新开一个身份。
   */
  async function fillMissingDetails(runId?: string): Promise<void> {
    if (busy.value || agentRunning.value) return;
    if (!content.value || !isTree(content.value)) {
      status.value = '只有思维导图能单独补说明。';
      return;
    }
    await runGeneration(
      '补齐这张图的节点说明',
      'edit',
      undefined,
      'default',
      'auto',
      // 选区显式给空：补说明是"整棵树按长度数一遍"，不是"改我圈的这几个"，
      // 静默带上画布上圈的节点会让解释变成"只在范围内补"，而实际不是。
      { selection: [], runId },
    );
  }

  /** 本会话内不再提这件事。**不写库**——理由见 `resumableSettled`。 */
  function ignoreResumable(): void {
    resumableRun.value = null;
    resumableSettled = true;
    status.value = '不再提示上次那次生成。它的结果还在服务端，刷新页面可以再看到。';
  }

  /**
   * 上报"这份结果被保存了"。失败不提示：写台账失败不该让用户以为他没保存成功
   * ——那是两件事。
   */
  function reportRunSaved(): void {
    const id = pendingSaveRunId.value;
    pendingSaveRunId.value = null;
    if (!id) return;
    void markRunSaved(id).catch(() => {});
  }

  /**
   * 本轮运行里"结果不完整"的证据：部分完成标记 + 每条诊断。
   *
   * 面板在结果摘要上方按琥珀色列出，`status` 也会因此改成「部分分支未完成」。
   * 只认 `ok` 的时候，这三条信息一条都没地方落。
   */
  const agentWarnings = ref<AgentWarning[]>([]);

  /**
   * 生成过程中正在长出来的预览树。
   *
   * 与 `draft` 分开存，不共用一份状态：预览没有经过整树校验，也不保证和最终结果
   * 一致（某一路失败被裁掉、去重丢掉重复项，都会让预览多出几个节点）。一旦让它
   * 顶替 `draft`，`done` 就会把半成品直接装进画布——候选层删掉（`acfdb5b`）之后
   * 落地不再有"用户点一下"这个复核点，混进去就再也拦不住。
   */
  const agentPreview = ref<PreviewNode | null>(null);

  /**
   * 已经收下的 `(父路径, label)`。
   *
   * 协议说同一组合只发一次，但那是在一条完整的连接上；重连重放、后端补发都会
   * 打破它，而界面上多出两个同名兄弟是看得见的错。
   */
  const previewSeen = new Set<string>();

  /** 父节点还没到的帧，按父路径攒着等它出现。 */
  const previewPending = new Map<string, PreviewNode[]>();

  /** 被真正丢掉的预览帧数。界面不显示，排障时用它回答"少的那几个节点去哪了"。 */
  const agentPreviewDropped = ref(0);

  /**
   * 预览树的"顶部是谁"。
   *
   * 生成时预览从中心主题开始长，顶部就是根，面板显示它的 label 即可。展开时预览是
   * 一条**祖先链**（见 `seedExpandPreview`）：顶部仍然是根，但用户关心的是"正在展开
   * 哪个节点"，把根的名字摆在标题行会让他以为整张图在重生成。所以展开时把它设成
   * 目标节点的标题，面板优先显示它。
   */
  const agentPreviewScope = ref<string | null>(null);

  /**
   * 预览整棵作废。
   *
   * 新一轮开始、收到 snapshot / done / error、换图，都要走这里：上一轮的残留
   * 一旦留到下一轮，用户看到的就是两棵树拼在一起的幻影。
   */
  function clearAgentPreview() {
    agentPreview.value = null;
    agentPreviewScope.value = null;
    previewSeen.clear();
    previewPending.clear();
    agentPreviewDropped.value = 0;
  }

  /** 不可能合法的帧直接丢掉，但别静默：留一条调试线索，否则少节点时无从下手。 */
  function dropPreviewFrame(reason: string) {
    agentPreviewDropped.value += 1;
    previewDebug(`[agent] 丢弃 node_grow 帧：${reason}`);
  }

  /**
   * 预览帧的调试线索只在开发/测试里输出。
   *
   * 它回答的是"少的那几个节点去哪了"，属于排障信息；用户不该在控制台看到这些。
   */
  function previewDebug(message: string) {
    if (import.meta.env.DEV) console.debug(message);
  }

  /** 路径 → 键。索引只可能是非负整数，用 '.' 连接不会撞。 */
  function previewPathKey(path: number[]): string {
    return path.join('.');
  }

  /**
   * 沿 path 找父节点；找不到返回 null。
   *
   * 返回 null 不代表这一帧有问题：索引超出当前子节点数量，更常见的原因是中间
   * 某个父节点的帧还没到（丢帧、重连后只接上了后半段）。调用方该把它攒起来。
   */
  function findPreviewParent(path: number[]): PreviewNode | null {
    let node: PreviewNode | null = agentPreview.value;
    for (const index of path) {
      if (!node) return null;
      const child: PreviewNode | undefined = node.children[index];
      if (!child) return null;
      node = child;
    }
    return node;
  }

  /**
   * 把帧挂到预览树上；父节点还不在就返回 false。
   *
   * 新节点自己也可能正是别人在等的父节点，所以挂上之后立刻放一批缓存出来。
   */
  function mountPreviewNode(path: number[], node: PreviewNode): boolean {
    const parent = findPreviewParent(path);
    if (!parent) return false;
    parent.children.push(node);
    // 自己的完整路径 = 父路径 + 自己在末尾的下标：追加到末尾，下标就是新的长度 - 1。
    flushPreviewPending([...path, parent.children.length - 1]);
    return true;
  }

  /** 收下还挂不上的帧，等它的父节点出现。 */
  function queuePreviewNode(path: number[], node: PreviewNode) {
    const key = previewPathKey(path);
    const queued = previewPending.get(key) ?? [];
    if (queued.length >= PREVIEW_PENDING_LIMIT) {
      dropPreviewFrame(`父路径 ${key} 下待挂帧超过 ${PREVIEW_PENDING_LIMIT} 条`);
      return;
    }
    queued.push(node);
    previewPending.set(key, queued);
    // 协议保证的顺利路径上永远不会走到这里，所以只留调试信息，不打扰用户。
    previewDebug(`[agent] node_grow 父节点还没到，暂存：path=${key} label=${node.label}`);
  }

  /**
   * 某个节点挂上之后，把它名下等着挂的帧放出来。
   *
   * 先整批取走再挂：挂的过程中会继续 flush 同一批键，边遍历边删会漏掉后半截。
   */
  function flushPreviewPending(path: number[]) {
    const key = previewPathKey(path);
    const queued = previewPending.get(key);
    if (!queued) return;
    previewPending.delete(key);
    for (const node of queued) {
      // 仍然挂不上说明它等的其实是更深的父节点，放回去继续等。
      if (!mountPreviewNode(path, node)) queuePreviewNode(path, node);
    }
  }

  /**
   * 消费一帧 `node_grow`。
   *
   * 顺序上后端保证父先于子，这里仍然按"可能乱序"处理：预览是纯展示，宁可多花
   * 一点缓存的代价，也不能因为一次丢帧就让整支子树静默消失。
   */
  function growPreviewNode(path: number[] | null, label: string, expandable: boolean | null) {
    const text = label.trim();
    if (!text) {
      dropPreviewFrame('label 为空');
      return;
    }
    if (charLength(text) > LIMITS.label) {
      dropPreviewFrame(`label 超过 ${LIMITS.label} 字`);
      return;
    }
    // 根帧的 `path` 是 null，不走索引校验；只有真正带路径的帧才需要。
    if (path !== null) {
      // 负索引、小数、超深路径都不是"父节点还没到"，是数据本身坏了。
      if (!path.every(index => Number.isInteger(index) && index >= 0)) {
        dropPreviewFrame(`path 不是有效的索引链：${path.join(',')}`);
        return;
      }
      if (path.length >= LIMITS.depth) {
        dropPreviewFrame(`path 深过 ${LIMITS.depth} 层：${path.join(',')}`);
        return;
      }
    }

    // 根用单独的键前缀：根没有路径，`[]` 已经被"挂在根上的孩子"占着了。
    const key = `${path === null ? 'root' : previewPathKey(path)}|${text}`;
    if (previewSeen.has(key)) return;
    if (previewSeen.size >= LIMITS.nodes) {
      dropPreviewFrame(`预览节点数超过 ${LIMITS.nodes}`);
      return;
    }
    // 收下就记，重放过来的同一帧到此为止；挂不上时它已经进了缓存，也不该再收一次。
    previewSeen.add(key);

    const node: PreviewNode = { label: text, expandable, children: [] };

    if (path === null) {
      // 根已经立起来又来了第二帧：只在标签不同时才算数据出错。同一个标签的重复
      // 帧上面已经被 previewSeen 挡掉，走到这里就说明后端给了两个不同的根。
      if (agentPreview.value) {
        dropPreviewFrame(`重复的根帧：${text}`);
        return;
      }
      agentPreview.value = node;
      // 根一到，之前攒着等它的分支（`[]`）立刻可以挂了。
      flushPreviewPending([]);
      return;
    }

    if (!agentPreview.value) {
      // 根还没到：带路径的帧全是孤儿，攒着等根。它们不会被误当成根——那是根
      // 帧丢失时唯一能察觉到的信号。
      queuePreviewNode(path, node);
      return;
    }
    if (!mountPreviewNode(path, node)) queuePreviewNode(path, node);
  }

  /**
   * 节点 id → 从根出发的子索引链（`[]` 是根自己）；找不到返回 null。
   *
   * 与后端 `tools._index_path` 同一口径：`node_grow` 的 `path` 指的是**父节点**的
   * 索引链，而展开时父节点就是被展开的那个节点，所以两边必须用同一套编号，
   * 否则预览会把新节点挂到别的分支下面。
   */
  function indexPathOf(root: MindNode, id: string): number[] | null {
    if (root.id === id) return [];
    const walk = (node: MindNode, path: number[]): number[] | null => {
      for (let index = 0; index < node.children.length; index += 1) {
        const child = node.children[index];
        if (child.id === id) return [...path, index];
        const found = walk(child, [...path, index]);
        if (found) return found;
      }
      return null;
    };
    return walk(root, []);
  }

  /**
   * 展开前把预览的**祖先链**搭起来，让 `node_grow` 帧能挂到被展开的那个节点下。
   *
   * 不搭这条链会怎样：后端的 `path` 是 `[0, 0]`（第三层那个叶子），而预览树此时是
   * 空的，`findPreviewParent` 找不到父节点，帧会被攒进 `previewPending` 里永远不
   * 出现——界面上就是"点了展开，什么预览都没有"。
   *
   * 只搭链上的节点（不是整棵树）：预览面板显示的是"正在展开 风险"，而不是把用户
   * 已有的整张图复制一遍；链上每一层只有一个孩子，正是路径的形状。
   *
   * 返回是否搭成功。拿不到路径（节点不在 content 里、比如正在被删）时返回 false，
   * 这一次展开就没有预览——那比画一条错的链好。
   */
  function seedExpandPreview(nodeId: string): boolean {
    const current = content.value;
    if (!current) return false;
    // 索引链是"根 → 第 i 个子节点"的坐标语言，只有树有。鱼骨与流程的顶层容器
    // 不是节点，那条链落不到任何位置上——宁可没有预览，也不给一个错位置
    // （与后端 `shapes.index_path` 对非树返回 `None` 同一条口径）。
    if (!isTree(current)) return false;
    const root = current.root;
    const path = indexPathOf(root, nodeId);
    if (!path) return false;

    const labels = [root.label.trim()];
    let cursor = root;
    for (const index of path) {
      cursor = cursor.children[index];
      labels.push(cursor.label.trim());
    }

    const build = (depth: number): PreviewNode => ({
      label: labels[depth],
      expandable: null,
      // 链上只往下走一格；最后一格就是目标节点，它的 children 留给真实的帧。
      children: depth + 1 < labels.length ? [build(depth + 1)] : [],
    });

    agentPreview.value = build(0);
    agentPreviewScope.value = labels[labels.length - 1];
    return true;
  }

  /** 记下这一帧的告警：同 kind + code + message 只留一条，两个工具报同一件事时不重复刷屏。 */
  function recordWarnings(next: AgentWarning[]) {
    if (!next.length) return;
    const seen = new Set(agentWarnings.value.map(item => `${item.kind}|${item.code}|${item.message}`));
    const merged = [...agentWarnings.value];
    for (const item of next) {
      const key = `${item.kind}|${item.code}|${item.message}`;
      if (seen.has(key)) continue;
      seen.add(key);
      merged.push(item);
    }
    agentWarnings.value = merged.slice(0, AGENT_WARNING_LIMIT);
  }

  /**
   * 「生成完成」那句的完整文案。
   *
   * 结果不完整时必须和全部成功区分开：只写「已落到画布」，用户改到一半才发现某几支
   * 是空的，而那时画布已经是他的作品了。partial 与"只有诊断"分开说——「分支未完成」
   * 这种话放在一棵只是有重复节点待合并的树上并不成立。
   *
   * **诊断要带数量**：原来只写「有诊断提示」，而界面上没有任何地方写着"诊断"两个字，
   * 用户不知道该去哪儿看（实测：生成完在界面上找了半天）。`有 2 条诊断` 是能对上的；
   * 数量与状态行上的入口按钮同一个来源（`diagnosticCount`）。
   */
  function finishStatus(base: string): string {
    const diagnostics = diagnosticCount(agentWarnings.value);
    const tail = diagnostics > 0 ? `，${diagnosticsHint(diagnostics)}` : '';
    if (agentWarnings.value.some(item => item.kind === 'partial')) {
      return `${base}（部分分支未完成${tail}）。可以直接改，或撤销回生成前。`;
    }
    if (diagnostics > 0) {
      return `${base}（${diagnosticsHint(diagnostics)}）。可以直接改，或撤销回生成前。`;
    }
    return `${base}，记得保存。`;
  }

  /** 保存撞上 409 时置位，由界面弹窗让用户选"重新加载 / 另存为新导图 / 取消"。 */
  const conflict = ref<{ message: string } | null>(null);
  /** 打开列表用的缓存。 */
  const maps = ref<MapRecord[]>([]);
  const listOpen = ref(false);
  const listBusy = ref(false);

  let controller: AbortController | null = null;

  /**
   * 画布该画哪棵树。
   *
   * 两级优先级，各有理由：
   * 1. `content` 最优先。用户的作品（含刚生成落地的那棵树）永远是画布上那一个。
   * 2. `agentPreview` 垫底。只有画布还是空的时候它才露面：节点一个一个到达，画布
   *    跟着长——这是"生成中"唯一的视觉反馈。一旦生成完成、`content` 有了值，
   *    预览就再也盖不住任何东西。
   *
   * **没有"候选"这一级了。** 从前 `candidate` 排在中间，让用户在点「应用」之前
   * 先看见结果；现在生成完成即落地（`commitGenerated`），看得见的就是他的。
   */
  const renderSource = computed<RenderSource | null>(() => {
    if (content.value) {
      // 树型拿到的是 `content.value.root` **这一个对象**（`canvasTreeOf` 里原样透传），
      // 所以树布局的入参与返回值一个字节都没变；鱼骨/流程拿到的是一棵合成树
      // （虚拟根 + 每个容器作为一级子节点），布局函数那边一行都不用改。
      const tree = canvasTreeOf(content.value);
      // **视图层收起在这里落地**（见 `canvas/view-collapse.ts`）。
      //
      // 为什么必须在这个位置、而不是渲染层：`nodes`、`edges` 都从 `layout` 这一份
      // 结果里读坐标，而 `layout` 吃的是 `source.root`。只过滤 `flatNodes` 的话，
      // 被截断的节点仍然参与 `layoutTidy` 的占位——它们照样撑开画布、照样把字号
      // 挤到 5px，只是看不见。那样"收起"连一个像素的可读性都没换回来。
      //
      // 只在树型生效：鱼骨与流程的画布树是合成出来的（虚拟根 + 每个容器一个子），
      // 它们本来就是"一层容器 + 一层节点"的形状，收起没有收益；更要紧的是数量口径
      // 要数"内容里真实的节点"，而合成根不在内容里，数出来的上限会凭空虚高一个。
      const foldedView = diagramTypeOf(content.value) === 'tree'
        ? viewTree(tree.root, {
          limit: visibleNodeLimit.value,
          expanded: viewExpandedNodes.value,
          collapsed: viewCollapsedNodes.value,
          expandAll: viewExpandAll.value,
        })
        : null;
      return {
        root: foldedView ? foldedView.root : tree.root,
        syntheticId: tree.synthetic ? tree.root.id : null,
        kind: 'content',
        expandable: NO_EXPANDABLE,
        // 画布用的边：流程图的阶段跳转在这里换成阶段入口节点的 id（见 `canvasEdgesOf`），
        // 阶段内的边原样追加在后面。侧栏与删除仍然读内容数组，那是内容真相。
        //
        // 附加边的端点有可能正落在被截断的子树上（边的两端是内容 id，与视图无关）。
        // 那时 `attachEdges` / `relationEdges` 找不到坐标就跳过——已有一条兜底，
        // 不需要在这里过滤：过滤会让"这条线去哪了"变成一个查不出来的问题。
        edges: canvasEdgesOf(content.value),
        // 布局只吃阶段**之间**那一路（见 `RenderSource.layoutEdges`）：阶段内的判定
        // 出边要是进了 dagre，整张流程图的分层就会为了两条"是 / 否"变形。
        layoutEdges: canvasStageEdgesOf(content.value),
        ...(foldedView && foldedView.folded.size ? { folded: foldedView.folded } : {}),
        ...(foldedView && foldedView.open.size ? { foldOpen: foldedView.open } : {}),
        ...(foldedView && foldedView.unshown.size ? { unshown: foldedView.unshown } : {}),
        // 一级分支的概括：**只读内容里那一份**（生成时调模型写的，落库、随图走）。
        // 只在树型上取，与 `foldedView` 同一条判据：鱼骨/流程的产物里根本没有这个
        // 字段，而它们的"一级"是固定六类 / 阶段序列，概括不出新东西。
        //
        // 空表不挂：`undefined` 就是"这些分支没有概括"，渲染层据此退回只显示数字
        // ——与加这个字段之前的行为一致（旧图、开关关着时生成的图都是这一档）。
        ...(tree.root && foldedView && content.value.branch_digests
          && Object.keys(content.value.branch_digests).length
          ? { digests: content.value.branch_digests }
          : {}),
      };
    }
    if (agentPreview.value) {
      const expandable = new Map<string, boolean | null>();
      const root = previewSubtree(agentPreview.value, [], expandable);
      return { root, syntheticId: null, kind: 'preview', expandable, edges: [] };
    }
    return null;
  });

  /**
   * 画布几何。**图型在这里分派**，树 / 流程图 / 鱼骨图各走各的（见
   * `canvas/layout/index.ts` 的 `layoutDiagram`）。
   *
   * 分派放在 store 而不是画布组件：`nodes` 与 `edges` 都从这一份结果里读坐标，
   * 而连线的手柄是按坐标反推的——两处各算一次，节点与连线一定会分家。
   *
   * 树图型仍然逐像素等于 `layoutTidy(root, { direction })`：默认值就是它。
   */
  const layout = computed<DiagramLayout | null>(() => {
    const source = renderSource.value;
    if (!source) return null;
    return layoutDiagram({
      root: source.root,
      // 布局吃 `layoutEdges`（流程图里只有阶段间边），画布吃 `edges`。两者在树与
      // 鱼骨上同一个数组，所以那两种形状的入参与改动前逐字节相同。
      edges: source.layoutEdges ?? source.edges,
      // 生成中的预览没有 content，也就还没有图型：那一刻按树排，节点一个一个长出来
      // 的样子与旧行为一致。等 `done` 把结果装进 content，图型才第一次生效。
      diagramType: diagramTypeOf(content.value),
      direction: direction.value,
    });
  });

  const flatNodes = computed<FlatNode[]>(() => {
    const source = renderSource.value;
    return source ? flatten(source.root) : [];
  });

  /**
   * **未截断**的画布树根（树型时就是 `content.root` 这一个对象）。
   *
   * 只服务"测量"这一类读者：读数用例要按**完整的树**算 `fit-all` 字号、重叠与视野外
   * 节点数，从已经收起的画布上算只会得到"收得刚刚好"的结论。
   *
   * 与 `renderSource.root` 分成两个出口，而不是让调用方去判 `kind`：画布那边手上没有
   * content，它拿到的只有渲染结果。让 store 把"完整的那棵树"直接交出去，调用方就不需要
   * 知道 `canvasTreeOf` 怎么合成鱼骨与流程的虚拟根。
   */
  const canvasRoot = computed<MindNode | null>(() => {
    if (!content.value) return null;
    return canvasTreeOf(content.value).root;
  });

  /**
   * 附加边在画布上的几何信息：用哪两个句柄。
   *
   * 附件的两端往往**不是父子**（汇合、回退跨越两个 subtree），所以不能像父子线
   * 那样只按 depth 猜方向，只能看两者的实际坐标：`childSide(from, to, direction)`
   * 给出的就是"从 from 看 to 在哪一侧"，接线端点即可。
   *
   * 端点找不到（不该发生：契约层硬拒 dangling，这里只是兜底）就跳过这条边，
   * 而不是画一条 `source: undefined` 的线——Vue Flow 会因此抛错，整张画布白屏。
   *
   * 流程图里被 dagre 判成**回退**的边（目标层号 ≤ 源层号）单独处理：按坐标算出的
   * 侧边会是 `bottom`，于是回退线从源节点底部出去、扎进目标节点顶部——一条笔直
   * 向上穿过整张图的线，正好压在主干上。它们一律改走**左侧**（出左、进左），
   * 绕在图的边上。这也是 `backEdges` 唯一的用处：判定归布局，接线归这里。
   */
  /**
   * **阶段内部**边的 id 集合：这些边在画布上画成细线（`MindEdge.vue` 的 `inner`）。
   *
   * 判据只能从内容取，不能从 `renderSource.edges` 反推：后者已经把阶段间的端点
   * 换成了节点 id，两路混在一起之后，"这条线是主轴还是判定出边"在数组里再也看不
   * 出来。用 id 对齐是唯一可靠的对应关系（边 id 在两张表里都唯一，契约层硬保证）。
   */
  const innerEdgeIds = computed<Set<string>>(() => {
    const current = content.value;
    return new Set(current ? stageInnerEdgesOf(current).map(edge => edge.id) : []);
  });

  const attachEdges = computed(() => {
    const current = layout.value;
    const source = renderSource.value;
    if (!current || !source) return [];

    const out: Array<{
      edge: MindEdge; side: ChildSide; targetSide: ChildSide; inner: boolean;
    }> = [];
    for (const edge of source.edges) {
      const from = current.positions.get(edge.source);
      const to = current.positions.get(edge.target);
      if (!from || !to) continue;
      const inner = innerEdgeIds.value.has(edge.id);
      if (current.backEdges?.has(edge.id)) {
        out.push({ edge, side: 'left', targetSide: 'left', inner });
        continue;
      }
      const side = childSide(from, to, current.direction);
      // 与父子线同一套端点约定（见 `edges` 里 targetHandle 那一行）：从哪一侧出去，
      // 就从对面那侧进来。
      const targetSide: ChildSide = side === 'left' ? 'right' : side === 'right' ? 'left' : 'top';
      out.push({ edge, side, targetSide, inner });
    }
    return out;
  });

  /**
   * 跨节点关系线（`ContentBase.relations`）的几何：端点落在哪两个节点、走哪两个句柄。
   *
   * 与 `attachEdges` 分开而不是合并成一个数组：两者的画法、数据形状、删除入口都不同
   * （关系线读 `kind` / `why`，附加边读 `kind` / `label` / `back` / `inner`）。合起来
   * 会让 `edges` 那个 computed 里出现一堆"这条到底是哪种"的判别。
   *
   * 端点找不到就跳过（契约层硬拒 dangling，这里只是兜底）：Vue Flow 拿到
   * `source: undefined` 会抛错，整张画布白屏。
   */
  const relationEdges = computed(() => {
    const current = layout.value;
    const source = content.value;
    if (!current || !source) return [];

    const out: Array<{
      relation: MindRelation; side: ChildSide; targetSide: ChildSide;
    }> = [];
    for (const relation of source.relations ?? []) {
      const from = current.positions.get(relation.source);
      const to = current.positions.get(relation.target);
      if (!from || !to) continue;
      const side = childSide(from, to, current.direction);
      const targetSide: ChildSide = side === 'left' ? 'right' : side === 'right' ? 'left' : 'top';
      out.push({ relation, side, targetSide });
    }
    return out;
  });

  /**
   * 每个节点要为附加边多开哪几个句柄。
   *
   * `MindNode.vue` 只渲染 `data.in` / `data.out` 里列出的句柄，而那两个数组原本只
   * 按**父子线**算。一条从叶子指向远处的回退线，源节点根本没有子节点、也就没有
   * 任何 `out-*` 句柄——Vue Flow 找不到句柄时线会从头节点中心乱连（或干脆不画），
   * 而报错只出现在浏览器控制台。
   */
  const edgeSideIndex = computed(() => {
    const index = new Map<string, { out: ChildSide[]; in: ChildSide[] }>();
    const ensure = (id: string) => {
      const hit = index.get(id);
      if (hit) return hit;
      const fresh = { out: [] as ChildSide[], in: [] as ChildSide[] };
      index.set(id, fresh);
      return fresh;
    };
    for (const { edge, side, targetSide } of attachEdges.value) {
      const from = ensure(edge.source);
      if (!from.out.includes(side)) from.out.push(side);
      const to = ensure(edge.target);
      if (!to.in.includes(targetSide)) to.in.push(targetSide);
    }
    // 跨节点关系线与附加边走**同一套端点约定**：一条关系线的两端也可能是两个叶子，
    // 不为它们开句柄，Vue Flow 同样会把线从节点中心乱连。这一段是"关系能画出来"
    // 的必要条件，不是优化。
    for (const { relation, side, targetSide } of relationEdges.value) {
      const from = ensure(relation.source);
      if (!from.out.includes(side)) from.out.push(side);
      const to = ensure(relation.target);
      if (!to.in.includes(targetSide)) to.in.push(targetSide);
    }
    return index;
  });

  const nodes = computed<MindFlowNode[]>(() => {
    const current = layout.value;
    const source = renderSource.value;
    if (!current || !source) return [];
    // 只有 `content` 可编辑：预览也能画出来，但点不动、改不了。
    const editable = source.kind === 'content';

    /**
     * 这张图**能不能拖动改结构**。
     *
     * 三条判据，每条都对应一种"拖了会出事"的具体情形：
     *
     *  1. `editable`：生成中的预览树不在 `content` 里，拖它没有可以写回的地方；
     *  2. **只认树型**：流程图与鱼骨图的画布树是**合成**出来的（虚拟根 + 每个容器
     *     一个子，见 `shape.canvasTreeOf`），拖一个节点要改的是"它属于哪个阶段 /
     *     哪根骨"这件事，语义与树完全不同。第一轮只做树，其余留给后面单独适配
     *     —— 让它们也能拖、但拖出个说不清的结果，比不让拖更糟；
     *  3. **根不拖**：`planDrop` 会拒（中心主题不能被挪走），而"让用户先把根拖到
     *     半空、松手才被告知不行"是没必要的一次失败。
     */
    const treeDraggable = editable && diagramTypeOf(content.value) === 'tree';

    const parentOf = new Map<string, string | null>();
    for (const { node, parentId } of flatNodes.value) parentOf.set(node.id, parentId);

    /**
     * 颜色继承：沿父链取**最近的有色祖先**。
     *
     * 一级分支一个颜色、它的所有后代用同一个色——这是思维导图"按分支分组"的
     * 标准做法。每个节点各一色会像调色板测试：颜色不再指示"这属于哪一支"。
     *
     * 为什么在**渲染层**继承，而不是生成时给每个节点都填上色：
     *   1. 旧图立刻有颜色。生成时填只对那一次生成有效，库里已有的树仍然是黑的；
     *   2. 用户手工改一级分支的颜色时，整支跟着变——他改的是"这一支的分组色"，
     *      而生成时填下去的是快照，改完只有那一个节点变，视觉上像改坏了；
     *   3. 落进库里的数据仍然是"一级分支才有 color"（与后端
     *      `PLAN_PROMPT` / `_slot_from` 的边界一致），导出、保存都不多出噪音。
     *
     * 一次前向遍历算完：`flatNodes` 是父在子前的 DFS 序，所以取父节点的结论时
     * 它一定已经算好了。逐节点沿父链往上走是 O(深度)，这条路是 O(节点数)，而且
     * 这段代码每次 `flatNodes` 变化都会重跑（画布重排的同一条路）。
     */
    const inheritedColor = new Map<string, NodeColor | undefined>();
    for (const { node, parentId } of flatNodes.value) {
      const own = node.color ?? undefined;
      inheritedColor.set(
        node.id,
        own ?? (parentId ? inheritedColor.get(parentId) : undefined),
      );
    }

    return flatNodes.value.map(({ node, depth }) => {
      const point = current.positions.get(node.id) ?? { x: 0, y: 0 };
      // 这个节点在画布上缺了什么（还有几个直接子节点没画）。见 `canvas/view-collapse.ts`：
      // `folded` / `foldOpen` 只收树型，鱼骨/流程恒为空表。
      const fold = source.folded?.get(node.id);
      /**
       * **一级分支的概括角标**：「还有 21 个 · 训练与并行」。
       *
       * 为什么只在一级分支上（`depth === 1`）：一枚 `+N` 只说得出"这里还有 3 个
       * 直接孩子"，说不出"这一支一共还有 21 个、说的是什么"。后者只有按支统计才
       * 答得出来，而一级分支只有几条、位置固定——给每个父节点都挂一枚概括，画布
       * 会被字糊满（`docs/VIEW-COLLAPSE.md` §七 的结论）。
       *
       * 两半各有一个来源，缺任何一半都不画：
       *   - `branchHidden` 是**渲染层自己数的**（`viewTree` 的 `unshown`，免费）；
       *   - `branchDigest` 是**生成时调模型写的**（`content.branch_digests`）。
       *
       * 没有概括就不画，是刻意的：那时这枚角标只剩一个数字，而"这一支还有 N 个"
       * 与 `+N`、顶部「显示 40 / 136」说的是同一件事——一枚只会重复别人的角标不值得
       * 占位置。
       */
      const branchHidden = depth === 1 ? source.unshown?.get(node.id) : undefined;
      const branchDigest = (branchHidden ?? 0) >= MINDMAP_BRANCH_BADGE_MIN_HIDDEN
        ? source.digests?.[node.id]
        : undefined;
      /**
       * 这个节点的子节点正显示着、而且是**用户要求**显示着的（点开过 / 全部展开）——
       * 渲染层据此给一枚常驻的「−」：那是"再点一下叠回去"的入口。
       *
       * 判据与单击节点那条路**同源**（`foldActionOf`）：两处各判一次，就会在边界上
       * 分家（角标说能收起、点节点却只选中），而那种不一致用户一眼就能看见。
       */
      const foldAction = foldActionOf(node.id, {
        folded: source.folded ?? NO_FOLDS,
        open: source.foldOpen ?? NO_FOLD_OPEN,
      });
      const foldOpen = foldAction === 'collapse';
      // 合成根（鱼头 / 流程起点）只画不选：它不在 content 里，选中它会让详情面板、
      // 面包屑、`expand` 请求一起对着一个 `findIn()` 找不到的 id 说话。
      const inContent = node.id !== source.syntheticId;

      /**
       * 外接框与形状。
       *
       * `current.sizes` **只有流程图会填**（`layoutFlow` 的 `sizes` 出参），树与鱼骨
       * 没有这个字段。于是这两个派生量本身就是"这张图是不是按流程排的"的判据：
       * 树与鱼骨恒定 `NODE_SIZE` + `shape: undefined`，渲染路径一个像素都不变——
       * 哪怕某个历史节点上带着 `role: 'decision'`。这条 gate 是"打开旧图不许变样"
       * 的执行点，别把它换成直接读 `node.role`。
       */
      const size = current.sizes?.get(node.id) ?? NODE_SIZE;
      const shape = current.sizes ? nodeShapeOf(node.role) : undefined;

      // 子节点在哪几侧，就在哪几侧留出口（双向布局里根的两边都有）。
      const outSides: ChildSide[] = [];
      for (const child of node.children) {
        const childPoint = current.positions.get(child.id);
        if (!childPoint) continue;
        // 侧边按**这份布局自己报的朝向**算，而不是 `theme.direction`：流程图的分层
        // 永远是上下（dagre TB），鱼骨的骨线永远是左右，两者都与"用户把树摆成
        // 哪个方向"无关。树布局里这两个值恒等，所以对树没有任何影响。
        const side = childSide(point, childPoint, current.direction);
        if (!outSides.includes(side)) outSides.push(side);
      }

      // 入口在父节点到自己的连线的对面：父在左边就从左边进来。
      const inSides: ChildSide[] = [];
      const parentPoint = current.positions.get(parentOf.get(node.id) ?? '');
      if (parentPoint) {
        const side = childSide(parentPoint, point, current.direction);
        inSides.push(side === 'left' ? 'right' : side === 'right' ? 'left' : 'top');
      }

      // 附加边也要句柄（理由见 `edgeSideIndex`）。并进来而不是替换：一个节点
      // 可以同时有父子线和回退线，两侧的句柄都要在。
      const extra = edgeSideIndex.value.get(node.id);
      if (extra) {
        for (const side of extra.out) if (!outSides.includes(side)) outSides.push(side);
        for (const side of extra.in) if (!inSides.includes(side)) inSides.push(side);
      }

      return {
        id: node.id,
        type: 'mind',
        // 拖动**只改结构**，不改坐标：松手之后 `content` 换了，`layout` 重算，
        // 这个节点的 `position` 就落到新结构给的位置上。手拖的那个位置从来不进
        // `content`（`Node.pos` 至今没有渲染层的读者，这一轮也不给它加）。
        // 三个 gate 的理由见上面 `treeDraggable` 那一段。
        draggable: treeDraggable && inContent && depth > 0,
        connectable: false,
        // 预览不可选：选中一个只在"提案"里存在的 id，会让详情面板对着
        // 空内容说话，expand 请求也会把后端不认识的 id 发回去。
        selectable: editable && inContent,
        // 选中态由 store 说了算。只在点击时靠 Vue Flow 自己切换的话，代码里改选中
        // （加节点、删节点后跳到父节点）画布上看不出来，用户会以为还停在原来那个节点上。
        // 不可编辑时一律不显示选中：残留的 selectedId 可能正好撞上预览里的同名 id。
        //
        // 多个节点一起高亮而不是只高亮主选区：「改这张图」吃的是整个选区，
        // 只亮一个的话，用户圈完三个节点看不出另外两个算不算数。
        selected: editable && inContent && selectedIds.value.includes(node.id),
        position: {
          x: point.x - size.width / 2,
          y: point.y - size.height / 2,
        },
        data: {
          label: displayTitle(node),
          body: displayBody(node),
          fullLabel: node.label,
          bodySource: bodySource(node),
          depth,
          out: outSides,
          in: inSides,
          refs: node.source_refs?.length ?? 0,
          draft: source.kind === 'content' ? undefined : source.kind,
          previewLeaf: source.kind === 'preview' && source.expandable.get(node.id) === false,
          // 视觉元素原样带过去：只有 `content` 那棵树才画（预览用弱化样式，
          // 它的颜色会让"这是还在长的东西"这件事变得模糊）。
          // 颜色取**继承**的结果：自身没有就沿用最近的有色祖先（见上面那段）。
          color: editable ? inheritedColor.get(node.id) : undefined,
          icon: editable ? node.icon ?? undefined : undefined,
          // 高亮只在"画布上这棵树 + 有一份生成改动清单"时成立，所以它天然只对 content 生效。
          changed: changedNodeIds.value.has(node.id),
          // 形状与尺寸只在流程图上出现（见上面 `size` / `shape` 那段的 gate）。
          role: shape === undefined ? undefined : node.role ?? undefined,
          shape,
          size: shape === undefined ? undefined : size,
          // 视图层收起：这一屏少画了它几个直接子节点，照实说。`undefined` 表示
          // "一个都没少画"，渲染层据此连角标都不画（绝大多数节点是这一档）。
          hiddenChildren: fold?.hidden,
          hiddenSubtree: fold?.closedSubtree,
          // 一级分支的概括角标（见上面 `branchDigest` 那一段）。两个都可能是
          // `undefined`：绝大多数节点是这一档，渲染层连角标都不画。
          branchDigest,
          branchHidden,
          // 画布上这个节点**正显示着几个子节点**。渲染层拿它判"有没有东西可以叠回去"
          // ——0 就是叶子，「−」角标根本不该出现。
          visibleChildren: node.children.length,
          foldOpen,
        },
      } satisfies MindFlowNode;
    });
  });

  const edges = computed<FlowEdge[]>(() => {
    const current = layout.value;
    if (!current) return [];

    const out: FlowEdge[] = [];
    for (const { node, parentId } of flatNodes.value) {
      if (!parentId) continue;
      const parentPoint = current.positions.get(parentId);
      const childPoint = current.positions.get(node.id);
      if (!parentPoint || !childPoint) continue;

      const id = `${parentId}->${node.id}`;
      // 鱼骨图的骨线不是两点直线：主骨段 + 60° 斜骨，或者一段十几像素的水平短线。
      // 折线由布局算好（`layout/fishbone.ts` 的 `links`），这里只交给对应的渲染器。
      // 句柄仍按约定给：`out-left` / `in-right` 在鱼骨里恒成立（子永远在父的左侧），
      // 这些句柄也确实由下面 `nodes` 那段按几何算出来了——给错 id 的话 Vue Flow
      // 找不到句柄，线会从头节点中心乱连。
      const bone = current.fishbone?.links.get(id);
      if (bone) {
        // 主骨末端的箭头只挂给**布局指定的那一条**骨线（锚点最靠右的那根，它的水平段
        // 就是主骨最右边那一截）。主骨那一小段被六条骨线各画了一遍，六份箭头在
        // DOM 里是六份、在画面上是一份——布局那边只登记一条，这里就照着给。
        const arrow = current.fishbone?.spineArrow;
        out.push({
          id,
          source: parentId,
          sourceHandle: 'out-left',
          target: node.id,
          targetHandle: 'in-right',
          type: 'fishbone',
          data: arrow && arrow.link === id ? { points: bone, arrow: arrow.points } : { points: bone },
        });
        continue;
      }

      const side = childSide(parentPoint, childPoint, current.direction);
      out.push({
        id,
        source: parentId,
        sourceHandle: `out-${side}`,
        target: node.id,
        targetHandle: `in-${side === 'left' ? 'right' : side === 'right' ? 'left' : 'top'}`,
        type: 'smoothstep',
      });
    }

    // 附加边（汇合 / 回退 / 条件跳转）。用自定义类型 `mindEdge`：它要画标签、
    // 按 kind 换颜色/线型，而内置的 smoothstep 做不到（见 `canvas/MindEdge.vue`）。
    //
    // 与父子线不同的两点，都在这里定死：
    //   - 用 `data` 传 label/kind，而不是 Vue Flow 的 `label` 字段：那个会被渲染成
    //     一个默认标签块，样式没法跟着 kind 变。
    //   - id 前缀 `attach:`，避免与 `${parentId}->${node.id}` 这种父子线 id 撞车
    //     （两个节点之间可以同时存在父子线与一条回退线，id 撞了只会剩一条）。
    for (const { edge, side, targetSide, inner } of attachEdges.value) {
      out.push({
        id: `attach:${edge.id}`,
        source: edge.source,
        sourceHandle: `out-${side}`,
        target: edge.target,
        targetHandle: `in-${targetSide}`,
        type: 'mindEdge',
        data: {
          label: edge.label ?? undefined,
          kind: edge.kind ?? 'flow',
          edgeId: edge.id,
          // 布局判出来的回退（目标层号 ≤ 源层号）。它是**几何事实**，与 `kind`
          // 不冲突：`kind` 说这条线是什么关系（是/否/回退），`back` 说它在这张
          // 图的层级里是不是在往回走。只有流程图会给它，树图型下恒为 undefined。
          //
          // 阶段内的边永远不会进这里：它们不参与分层，也就没有"回退"这个判据
          // （`layoutFlow` 从未见过它们，`backEdges` 里不可能有它们的 id）。
          back: current.backEdges?.has(edge.id) || undefined,
          // 阶段内部的边（判定节点的「是 / 否」）。画成细线，否则一条"否"看起来
          // 与阶段之间的主轴跳转一模一样，读图的人分不出哪条是主干。
          inner: inner || undefined,
        },
      });
    }

    // 跨节点关系线（`relations`）。第三类线，用**第三种**渲染器：它的两个端点没有
    // 共同的父节点，走 `mindEdge` 那套贴边的折线会从中间节点上拐直角，看起来像
    // "穿过"了某个节点（见 `canvas/MindRelation.vue` 的注释）。
    //
    // id 前缀 `relation:`：同一个节点对上可以同时存在父子线（`A->B`）、附加边
    // （`attach:…`）与关系线，三条的 id 必须分得开。
    for (const { relation, side, targetSide } of relationEdges.value) {
      out.push({
        id: `relation:${relation.id}`,
        source: relation.source,
        sourceHandle: `out-${side}`,
        target: relation.target,
        targetHandle: `in-${targetSide}`,
        type: 'mindRelation',
        data: {
          // 渲染只读 `kind`：后端 `_sync_kind` 保证它有值，`type` 是旧名的回声。
          kind: relation.kind ?? 'references',
          why: relation.why ?? undefined,
          relationId: relation.id,
        },
      });
    }
    return out;
  });

  /**
   * 画布上有几个节点（界面上那个「N 个节点」）。
   *
   * 用 content 自己的口径（`countNodesIn`）而不是 `flatNodes.length`：鱼骨与流程的
   * 布局树上多一个**不在 content 里**的合成根，拿扁平表计数会让顶栏说"23 个节点"
   * 而实际内容是 22 个。树型两者相等，一个数都不会变。
   *
   * 画布本来为空时 `content` 是 null，这时退回扁平表——生成中的预览树还是照旧数
   * 它自己（那正是"节点一个一个长出来"的反馈）。
   */
  const nodeCount = computed(() => (
    content.value ? countNodesIn(content.value) : flatNodes.value.length
  ));

  /**
   * 这一屏被**收起**了多少个节点（内容总数 − 画布上真画出来的数）。
   *
   * 界面必须说出这个数：默认收起之后，屏幕上只有 7 个节点，而顶栏写着「136 个节点」。
   * 两个数都对，但不解释一句，用户会以为其中一个错了。只数祖先、不数"看得见但没摊开
   * 的子节点"——那正是差值，也就是被藏起来的那些。
   *
   * 鱼骨与流程不收起，这个值恒为 0（合成根使得差值可能是 -1，`Math.max` 兜住）。
   */
  const hiddenNodeCount = computed(() => Math.max(0, nodeCount.value - flatNodes.value.length));

  /**
   * 详情面板要显示的节点。
   *
   * 必须落在 `content` 里：画布上可能正画着预览（`flatNodes` 跟着渲染源走），
   * 而面板上的「改文字 / 加子节点 / 删除」全都写 `content`。没有 `content` 就没有
   * 可编辑的节点，否则面板会摆出一排点了没反应的按钮。
   */
  /**
   * 这一轮"用户圈住的节点"全集（主选区在前）。
   *
   * 顺序有意义：第一个就是详情面板/展开按钮对着的那一个，所以调用方拿
   * `selectedIds[0]` 与 `selectedId` 是同一件事。
   *
   * 这里按 `content` 过滤一次，而不是指望每条删节点的路径都记得清追加选区：
   * 删掉的节点留在选区里，下次「改这张图」就会把后端根本不认识的 id 发过去
   * （后端会静默跳过，但那份提示词里就少了一条、而用户以为自己选了三个）。
   */
  const selectedIds = computed<string[]>(() => {
    const all = content.value;
    if (!all) return [];
    const primary = selectedId.value;
    const extra = extraSelectedIds.value.filter(
      id => id !== primary && findIn(all, id) !== null,
    );
    return primary ? [primary, ...extra] : extra;
  });

  const selectedNode = computed<FlatNode | null>(() => {
    if (!content.value) return null;
    return flatNodes.value.find(entry => entry.node.id === selectedId.value) ?? null;
  });

  /** 选中节点的引用。空节点也有自己的空数组，详情面板不必再判空。 */
  const selectedRefs = computed<SourceRef[]>(() => selectedNode.value?.node.source_refs ?? []);

  /**
   * 已应用内容里的**全部**附加边。
   *
   * 只读 `content` 而不是 `renderSource`：侧栏的「附加关系」区块要能删边、要能
   * 显示"这条线的端点是谁"，而预览里的边还不是任何人的作品（它是生成中的临时树）。
   * 画布那条路（`edges` computed）才跟着 `renderSource` 走。
   */
  const contentEdges = computed<MindEdge[]>(() => edgesOf(content.value));

  /**
   * 已应用内容里的**阶段内部**边（`stages[i].edges`）。
   *
   * 与 `contentEdges` 分成两个 computed 而不是并成一个：它们是**两个数组、两个
   * 命名空间**（见契约 `FlowStage`）。并起来之后侧栏只能给出一串 id，而"删除"
   * 必须知道该删哪一层——那正是 `edgesOfContent` 的语义不能动的原因。
   *
   * 树与鱼骨恒为空数组。
   */
  const contentInnerEdges = computed<MindEdge[]>(() => (
    content.value ? stageInnerEdgesOf(content.value) : []
  ));

  /**
   * 与当前选中节点相关的边（源或目标命中它）。
   *
   * 侧栏只用这一份而不是全量：一张流程图可能有几十条线，全列出来用户得自己在
   * 里面找"这条跟选中的节点有关系吗"。而"改这条线 / 删这条线"的操作入口，本来看
   * 的就是选中节点周围那几条。
   *
   * 流程图里两个命名空间都要认：`FlowContent.edges` 的端点是**阶段 id**，而画布上
   * 选中的是阶段里的某个**步骤**。只按节点 id 比的话，一张流程图的每条边都不在
   * 列表里——用户加完线之后再也删不掉它。
   */
  const selectedEdges = computed<MindEdge[]>(() => {
    const id = selectedId.value;
    if (!id || !content.value) return [];
    const stage = stageOfNode(content.value, id);
    return contentEdges.value.filter(edge => (
      edge.source === id || edge.target === id
      || (stage !== null && (edge.source === stage || edge.target === stage))
    ));
  });

  /**
   * 已应用内容里的**全部**跨节点关系线。
   *
   * 三种形状的顶层都有这个字段（契约 `ContentBase`），所以不需要像边那样按图型分派。
   */
  const contentRelations = computed<MindRelation[]>(() => (
    content.value?.relations ?? []
  ));

  /**
   * 与选中节点相关的关系线（源或目标命中它）。详情面板用它列"这个节点参与的关系"。
   *
   * 判据只有节点 id，没有 `selectedEdges` 那一层"阶段放宽"：关系线的端点永远
   * 是节点 id（见契约 `_validate_relations`），不存在第二种命名空间。
   */
  const selectedRelations = computed<MindRelation[]>(() => {
    const id = selectedId.value;
    if (!id) return [];
    return contentRelations.value.filter(relation => (
      relation.source === id || relation.target === id
    ));
  });

  /**
   * 与选中节点相关的**阶段内**边。
   *
   * 判据只按**节点 id**（不做"该阶段全都列"那一层放宽）：阶段间的边属于**阶段**，
   * 所以选中阶段里任何一步都该看见它；阶段内的边属于**具体两个步骤**，把一整个
   * 阶段的判定出边都铺给每一步看，只会让侧栏在一个 8 步阶段里塞进十几条与当前
   * 节点无关的线。选中的是阶段入口节点时它天然命中（那个节点就是边的端点）。
   */
  const selectedInnerEdges = computed<MindEdge[]>(() => {
    const id = selectedId.value;
    if (!id) return [];
    return contentInnerEdges.value.filter(edge => edge.source === id || edge.target === id);
  });

  /**
   * 边端点 id → 显示名（节点标题；流程的边给阶段根节点的标题）。
   *
   * 两个命名空间混在同一份列表里显示，所以查找要按形状回退——只查节点的话，
   * 流程图每一条边都会显示成 `s_xxx → s_yyy`。
   */
  const edgeNameOf = computed(() => {
    const current = content.value;
    return (id: string) => (current ? edgeEndpointLabel(current, id) : id);
  });

  /**
   * 来源的反向索引。
   *
   * 只从 `content` 算：画布上可能正画着预览节点，把它们算进引用统计会让
   * 左侧说"被 5 个节点引用"、而其中 3 个根本不在导图里。
   */
  const refIndex = computed(() => buildRefIndex(content.value ? rootsOf(content.value) : null));

  /**
   * **这次生成改了什么**：生成落地那一刻算好的一份静态快照；没有基线时为 `null`。
   *
   * 为什么是 `ref` 而不是 `computed`：`computed` 要两份实时值（基线树 + 当前树），
   * 而基线在落地之后就没人持有了——`content` 已经被换成新树，再拿它算 diff 只会得到
   * 一份空清单。落地瞬间算一次并冻结下来的语义恰好就是用户要的那句话："刚才那 25
   * 秒动了什么"。之后他改画布，这份记录**不该**跟着变——它记的是生成，不是编辑。
   *
   * `null` 与空数组要分开：前者是"没有可比基线"（画布本来是空的，整棵树都是新的），
   * 后者是"算出来了，一处都没改"。前者不该显示清单，后者要显示"这次什么都没改"。
   */
  const generatedChanges = ref<ScopedChange[] | null>(null);

  /**
   * 这次生成落进画布的规模：`nodes` 是节点数，`changes` 是改动条数（`null` = 无基线）。
   *
   * 面板读它说清"已落到画布（42 个节点，改了 6 处）"。前端这份只给人看；进统计的
   * 那份 `node_count` 在同一时刻由 `generate` 事件带上去，两者必须是同一个数——所以
   * 它们在同一次调用里算（`commitGenerated`）。
   */
  const generatedSummary = ref<{ nodes: number; changes: number | null } | null>(null);

  /**
   * 版本冲突时保留的第二棵树（连同它落地所需的元数据）。
   *
   * 正常路径上永远是 `null`——生成期间 `busy` 把所有编辑入口都拦住了，画布不会变。
   * 但它不是不可能：`undo` / `redo` 走的是 `restoreSnapshot`，绕过 `applyContent`；
   * 将来新加的编辑入口也未必记得判 `busy`。所以完成时仍然比对 `clientRevision`，
   * 变过就**不覆盖**，把两棵树都摆出来让用户选。
   *
   * 这是"删掉候选层"之后唯一保留的裁决界面，而它的性质与候选不同：候选挡在每一次
   * 生成前面，它只在数据要丢的时候出现。
   */
  const conflictDraft = ref<{ next: DiagramContent; meta: GenerateMeta } | null>(null);

  /**
   * 最近一次落到画布的那棵树的**引用**（不是副本）。
   *
   * 只服务一个判断：`undoGenerate()` 能不能一步回到生成之前。用引用相等比比较整树
   * 便宜，而且正好是那个语义——画布上还是生成的结果本身，说明撤销栈顶就是生成前
   * 那棵树；用户改过之后引用变了，那时"撤销一步"只是退掉他自己的一次编辑。
   */
  let generatedContent: DiagramContent | null = null;

  /**
   * `generatedChanges` 里落在「这次生成带上去的选区」中的那些。
   *
   * 看的是**请求时**的选区（`generationSelection`），不是此刻的选区：用户点一下清单
   * 里的一行就会改选中，读实时选区的话这份标记会自己漂走。
   */
  const changedNodeIds = computed<Set<string>>(() => {
    const diff = generatedChanges.value;
    if (!diff) return new Set<string>();
    const nodeKinds = new Set<CandidateChange['kind']>(['removed', 'updated', 'moved']);
    return new Set(diff.filter(item => nodeKinds.has(item.kind)).map(item => item.id));
  });

  /**
   * "把某个节点挪到视野中心"的触发计数。
   *
   * 用计数器而不是监听 `selectedId`：在画布上手动点节点时也会改选中，若监听选中，
   * 视图会把人刚拖到的位置又拽回中心。只有从引用列表主动跳转时才加一。
   */
  const focusTick = ref(0);

  /** 从容器根到当前节点的路径，详情面板用来标明"这句话出自哪一支"。 */
  const breadcrumb = computed<MindNode[]>(() => {
    const target = selectedId.value;
    if (!content.value || !target) return [];
    // 链首是**它所在那棵子树的根**：鱼骨图里一个原因的祖先链里不该出现另一根骨
    // 的内容（与后端 `shapes.path_to` 同一条口径）。
    return pathToIn(content.value, target) ?? [];
  });

  const canUndo = computed(() => past.value.length > 0);
  const canRedo = computed(() => future.value.length > 0);

  function snapshot(): string | null {
    return content.value ? JSON.stringify(content.value) : null;
  }
  function snapshotBytes(snap: string): number {
    return new TextEncoder().encode(snap).length;
  }

  /**
   * 压一步撤销，并按字节与步数两个上限淘汰最旧的。
   *
   * 至少保留一步：一份快照自身就超过上限时（极端大的树）清空栈会让撤销直接失效，
   * 那比占着这 10MB 更糟。
   */
  function recordPast(snap: string) {
    past.value.push(snap);
    pastBytes.value += snapshotBytes(snap);
    while (past.value.length > 1 && (past.value.length > HISTORY_LIMIT || pastBytes.value > HISTORY_BYTES_LIMIT)) {
      const dropped = past.value.shift()!;
      pastBytes.value -= snapshotBytes(dropped);
    }
  }

  /**
   * 改动前调用：记一步历史，并让重做栈失效（树从这里长出新的分支了）。
   *
   * `actor` 决定这次改动记到谁头上：默认 `user`（手工编辑，绝大多数调用点），
   * `agent` 只有 `commitGenerated()` 传（用户没动手，改动是模型算的）。
   * 区分只能靠参数——`pushHistory()` 返回之后 `content.value` 已经是新树，那时候
   * 再想看出"谁改的"是不可能的。
   */
  function pushHistory(actor: EditActor = 'user', runId?: string) {
    const current = content.value;
    if (!current) return;
    recordPast(JSON.stringify(current));
    future.value = [];
    editBase = { content: current, actor, ...(runId ? { runId } : {}) };
  }

  /** 还能撤销几步、占了多少字节：界面与测试都靠它确认字节上限真的生效。 */
  const undoDepth = computed(() => past.value.length);
  const undoBytes = computed(() => pastBytes.value);

  /**
   * 撤销栈与改动账本一起归零。
   *
   * 两个都清是因为它们的适用前提是同一个：**这是同一棵树的历史**。换图（打开 /
   * 新建 / 示例 / 导入）之后，上一张图那些没提交的事件属于另一条记录，留着它们，
   * 下一次保存会把它们写到当前这张图的 `map_id` 上——统计一旦串图，就再也分不开
   * "这张图被改得多"和"这个人改得多"。
   *
   * `commitGenerated()` 的 `create` 分支也走它：那是一份新作品，上一张图（如果
   * 有）的账本同样到此为止。
   */
  function resetHistory() {
    past.value = [];
    future.value = [];
    pastBytes.value = 0;
    pendingEdits.value = [];
    editBase = null;
    // 换图/新建/新作品：视图的展开程度一起回到出厂设置。它不是历史的一部分，但它
    // 与"这一屏画的是哪棵树"绑得比与历史更紧——上一棵树的手动展开照搬到新树上，
    // 表现出来是"打开另一张图，它莫名其妙多摊开几层"。
    resetViewCollapse();
  }

  /**
   * 改动**已经落到 `content` 之后**调用：算出这次改了什么，攒进待提交队列。
   *
   * 与 `pushHistory()` 分成两步，是因为顺序不能换：diff 要"改之前"和"改之后"两份树，
   * 而 `pushHistory()` 执行时 `content.value` 还是旧值。
   *
  /**
   * 一次提交内所有事件共享同一个 `at`：它是"这次操作产生了几条事件"的唯一分组依据
   * （一个字段一条事件，见 `edit-log.ts` 文件头）。
   *
   * **应用/接受候选那条 `accept` 已经不在这里了**：候选层删掉之后没有"接受"这个
   * 动作，分母改由 `kind='generate'` 承担，而它由 `commitGenerated()` 直接构造
   * （那条路要 `node_count`，不属于"用户改了什么"这条差分）。
   *
   * 不在这里截断：截断发生在提交那一刻（`save()`），这样"超了多少条"能如实上报；
   * 在这里截掉的话，超出的部分连数都不知道。
   */
  function collectEdits(after: DiagramContent | null, at = new Date().toISOString()) {
    const base = editBase;
    editBase = null;
    if (!base) return;
    const events = diffEdits(base.content, after, {
      actor: base.actor,
      at,
      ...(base.runId ? { runId: base.runId } : {}),
    });
    if (!events.length) return;
    pendingEdits.value = [...pendingEdits.value, ...events];
  }

  /**
   * create 意图落画布后的那一步撤销。
   *
   * 与 `pushHistory()` 只差一处：画布本来是空白时也要留下一步（`'null'` 就是
   * JSON 里的空画布），否则用户点了生成就再也退不回生成之前的样子——而那正是
   * 删掉候选层之后唯一的后悔药。
   *
   * 不设 `editBase`：整棵树不是 diff，`diffEdits(null, ...)` 本来就返回空数组；
   * 那次生成由一条 `generate` 事件记账（见 `commitGenerated`）。
   */
  function pushHistoryOrEmpty() {
    recordPast(snapshot() ?? 'null');
    // 树从这里长出新的分支了，重做栈必须失效。
    future.value = [];
  }

  /** 选中的节点可能已被删掉：兜底清空，免得详情面板指向不存在的节点。 */
  function ensureSelection() {
    const target = selectedId.value;
    if (target && content.value && !findIn(content.value, target)) {
      setPrimary(null);
    }
  }

  /**
   * 把一步历史快照装回画布。
   *
   * 快照可能是字符串 `'null'`——生成落进一张空画布的时候，`pushHistoryOrEmpty()`
   * 压的就是它。`JSON.parse('null')` 得到 null，正好是 `MindMapContent | null`
   * 的另一半；但选中与编辑态还指着已经不存在的旧节点，必须一起清掉，
   * 否则详情面板和编辑框会继续对着空白画布说话。
   */
  function restoreSnapshot(snap: string) {
    const parsed = slot(JSON.parse(snap) as DiagramContent | null);
    content.value = parsed;
    if (!parsed) {
      setPrimary(null);
      editingId.value = null;
    }
  }

  function applyContent(
    next: DiagramContent,
    history: boolean,
    actor: EditActor = 'user',
  ): boolean {
    // **生成期间不再禁止编辑。** 从前锁画布是为了"候选落地"那一步：它要用户点一下，
    // 而那一刻必须比对版本。现在落地是自动的，锁住反而让"这 25 秒我顺手改个标题"
    // 变成不可能——而那正是最自然的事。
    //
    // 代价由 `commitGenerated` 的冲突判定兜住：期间改过（`clientRevision` 变了）
    // 就**不覆盖**，两棵树都留着让用户选。这里一个字都不用判。
    if (history) pushHistory(actor);
    content.value = slot(next);
    dirty.value = true;
    clientRevision.value += 1;
    // 改动已经落地，这时候才比得出差：`pushHistory()` 抄下的那棵树 vs `next`。
    // `history=false` 时 `editBase` 是空的，这里自动什么都不记——"不记历史"与
    // "不记日志"是同一件事，没有哪条调用路径会只要其中之一。
    collectEdits(next);
    return true;
  }

  /**
   * 节点深度，1 起算（与契约的"最多 8 层"同一口径）。
   *
   * 直接读 `shape.depthIn` 而不是扁平表：鱼骨与流程的布局树上多一层合成根，拿
   * `flatNodes` 的 depth 数出来的层号会整体偏 1（一根骨会被算成第 2 层），于是
   * "最多 8 层"在鱼骨图上前 7 层就开始拦。
   */
  function depthOfId(id: string): number | null {
    if (content.value) return depthIn(content.value, id);
    const hit = flatNodes.value.find(entry => entry.node.id === id);
    return hit ? hit.depth + 1 : null;
  }

  function beginEdit(id: string) {
    if (!content.value || !findIn(content.value, id)) return;
    setPrimary(id);
    // 与 `select` 同一条：编辑框长在一个看不见的节点上是纯粹的 bug。
    revealInView(id);
    editingId.value = id;
  }

  function endEdit() {
    editingId.value = null;
  }

  function commitLabel(id: string, label: string) {
    if (!content.value) return;
    const node = findIn(content.value, id);
    if (!node) return;

    const next = label.trim();
    if (next === node.label.trim()) {
      // 只收拾自己的编辑态：连按 Tab 时上一个输入框失焦会再提交一次，
      // 那时正在编辑的其实是新节点，这里不能把新节点的编辑态一起关掉。
      if (editingId.value === id) editingId.value = null;
      return;
    }
    if (!next) {
      status.value = '节点标题不能为空';
      return;
    }
    if (Array.from(next).length > LIMITS.label) {
      status.value = `节点标题最多 ${LIMITS.label} 个字`;
      return;
    }

    // 写回按"定位到它所在的那棵子树、把子树当根改、再放回原位"（镜像后端
    // `shapes.replace_node`）：`edits.ts` 那套纯函数因此一行都不用重写，鱼骨与流程
    // 走的还是同一份实现。
    const patched = replaceContainer(content.value, id, root => renameNode(root, id, next));
    if (!applyContent(patched, true)) return;
    if (editingId.value === id) editingId.value = null;
    status.value = '已修改，记得保存。';
  }

  /**
   * 提交节点正文（契约 v2 的 `note`）。
   *
   * 和改标题一样：内容没变就只收拾编辑态，不记历史；空正文是合法输入——
   * 它表示"这个节点只保留短标题"，所以清空必须能存下去（不能当成"非法"）。
   */
  function commitNote(id: string, note: string | null) {
    if (!content.value) return;
    const node = findIn(content.value, id);
    if (!node) return;

    const next = note?.trim() ?? '';
    const before = node.note?.trim() ?? '';
    if (next === before) return;
    if (charLength(next) > LIMITS.note) {
      status.value = `节点正文最多 ${LIMITS.note} 个字`;
      return;
    }

    const patched = replaceContainer(content.value, id, root => setNodeNote(root, id, next));
    if (!applyContent(patched, true)) return;
    status.value = next ? '已修改正文，记得保存。' : '已清空正文，只保留标题，记得保存。';
  }

  /**
   * 改节点的语义色与图标（详情面板上的两个控件）。
   *
   * 与改标题、改备注同一套语义：内容没变就什么都不做（不记撤销、不置脏），
   * 变了才是**一次可撤销的内容修改**——换颜色凭什么不能 Ctrl+Z 退回去。
   *
   * 传 `null` 是显式清除（选「默认」就是清掉颜色）；要"不动"就别传这个字段。
   * 图标按码点截到契约上限（`LIMITS.icon`），空串归一成 `null`：一个只含空格的
   * 图标在画布上什么都不显示，却会让"有没有图标"这件事变成两个分支。
   */
  function setNodeVisualById(
    id: string,
    patch: { color?: NodeColor | null; icon?: string | null },
  ): void {
    if (!content.value) return;
    const node = findIn(content.value, id);
    if (!node) return;

    const nextColor = patch.color === undefined ? node.color ?? null : patch.color;
    const nextIcon = patch.icon === undefined
      ? node.icon ?? null
      : (() => {
        const text = (patch.icon ?? '').trim();
        return text ? Array.from(text).slice(0, LIMITS.icon).join('') : null;
      })();

    if ((node.color ?? null) === nextColor && (node.icon ?? null) === nextIcon) return;

    const patched = replaceContainer(
      content.value, id, root => setNodeVisual(root, id, nextColor, nextIcon),
    );
    if (!applyContent(patched, true)) return;
    status.value = '已修改样式，记得保存。';
  }

  function renameTitle(title: string) {
    if (!content.value) return;
    const next = title.trim();
    // 提交后草稿就作废：已经落到 content 里了，再留着会让「等待期间有未提交
    // 输入」的判断一直为真。
    titleDraft.value = null;
    if (next === content.value.title) return;
    if (!next) {
      status.value = '标题不能为空';
      return;
    }
    if (Array.from(next).length > LIMITS.title) {
      status.value = `标题最多 ${LIMITS.title} 个字`;
      return;
    }
    if (!applyContent({ ...content.value, title: next }, true)) return;
  }

  /** 标题框每次按键都同步草稿，只记"有未提交的输入"，不改 content。 */
  function setTitleDraft(value: string | null) {
    titleDraft.value = value;
  }

  function addChildTo(parentId: string) {
    if (!content.value) return;
    if (countNodesIn(content.value) >= LIMITS.nodes) {
      status.value = `导图最多 ${LIMITS.nodes} 个节点`;
      return;
    }
    const parent = findIn(content.value, parentId);
    if (!parent) return;
    if (parent.children.length >= LIMITS.children) {
      status.value = `一个节点最多 ${LIMITS.children} 个子节点`;
      return;
    }
    if ((depthOfId(parentId) ?? 1) + 1 > LIMITS.depth) {
      status.value = `导图最多 ${LIMITS.depth} 层`;
      return;
    }

    // 加到"某根骨 / 某个阶段"下与加到树节点下是同一件事：容器内的子树形状不变。
    // 所以这里只换一个入口——拿容器自己的子树根去 `addChild`，再原位写回。
    const container = containerOfNode(content.value, parentId);
    if (!container) return;
    const { root, node } = addChild(container.root, parentId);
    const patched = replaceContainer(content.value, parentId, () => root);
    if (!applyContent(patched, true)) return;
    setPrimary(node.id);
    // 刚加的子节点必须看得见：默认只展开一层，而新节点通常加在第 2 层以下。
    // 不加这一句，按 Tab 之后画面毫无变化，用户会以为按键没生效、再按一次。
    revealInView(node.id);
    editingId.value = node.id;
    status.value = '已加子节点，输入文字后按回车确认。';
  }

  /**
   * 编辑新节点时按 Tab：先提交文字，再回到它的父节点下加一个兄弟。
   * 若只看 selectedId，新节点一被选中，Tab 就变成"给子节点加子节点"——连按会一路下钻；
   * 用户预期是同一个父节点下排出一串兄弟，所以这里固定回到父节点。
   */
  function commitAndAddChild(label: string) {
    const id = editingId.value;
    if (!id) return;
    const parent = content.value ? findParentIn(content.value, id) : null;
    commitLabel(id, label);
    // 提交被拦下（空标题、超长）时编辑态还在，别在错误的基础上接着加节点。
    if (editingId.value === id) return;
    addChildTo(parent?.id ?? selectedId.value ?? id);
  }

  function addSiblingTo(id: string) {
    if (!content.value) return;
    if (countNodesIn(content.value) >= LIMITS.nodes) {
      status.value = `导图最多 ${LIMITS.nodes} 个节点`;
      return;
    }
    const parent = findParentIn(content.value, id);
    // 容器根（骨 / 阶段 / 中心主题）没有兄弟，退化成一个子节点；这时要按"加子节点"
    // 的规则校验层数与个数。
    if (!parent) {
      addChildTo(id);
      return;
    }
    if (parent.children.length >= LIMITS.children) {
      status.value = `一个节点最多 ${LIMITS.children} 个子节点`;
      return;
    }
    if ((depthOfId(parent.id) ?? 1) + 1 > LIMITS.depth) {
      status.value = `导图最多 ${LIMITS.depth} 层`;
      return;
    }

    const container = containerOfNode(content.value, id);
    if (!container) return;
    const { root, node } = addSibling(container.root, id);
    const patched = replaceContainer(content.value, id, () => root);
    if (!applyContent(patched, true)) return;
    setPrimary(node.id);
    // 同 `addChildTo`：新兄弟必须看得见（它可能长在被收起的那一层上）。
    revealInView(node.id);
    editingId.value = node.id;
    status.value = '已加兄弟节点，输入文字后按回车确认。';
  }

  function deleteNode(id: string) {
    if (!content.value) return;
    // 容器根删不得：树的中心主题删了就没这张图了，一根骨/一个阶段删了就不是鱼骨图/
    // 流程图了。判据走形状（`isContainerRoot`），不看 `diagram_type` 字段。
    if (isContainerRoot(content.value, id)) {
      status.value = isTree(content.value)
        ? '根节点不能删除'
        : `${containerNoun(kindOf(content.value))}本身不能删除；它下面挂的内容可以删。`;
      return;
    }

    // id 不在内容里（预览的派生 id 会走到这里）就直接退出。
    // 交给 removeNode 的话它只会原样返回整棵树，而 applyContent 照样记一步历史、
    // 递增 clientRevision——用户什么都没删，撤销栈与生成冲突判定的账本却被改脏了。
    const doomedNode = findIn(content.value, id);
    if (!doomedNode) return;

    const parent = findParentIn(content.value, id);
    // 连带删边：命中**这棵子树**任意节点的边全部消失，与后端 `_apply_remove` 同一
    // 语义。只清被点名的那一个节点的话，子孙身上的边会悬空，而 dangling 是契约层
    // 的硬拒——下一次保存就 422，用户却只是"删了一个节点"。
    //
    // 两张边表都要清，理由见 `dropDanglingEdges`：流程图的阶段间边端点是阶段 id
    // （删步骤碰不到），而阶段内边端点是**节点 id**——那一层以前没有读者，所以
    // "流程删节点不用管边"这条旧结论在这里不再成立。
    const doomed = subtreeIds(doomedNode);
    const removed = replaceContainer(content.value, id, root => removeNode(root, id));
    const nextContent = dropDanglingEdges(removed, doomed);
    if (!applyContent(nextContent, true)) return;
    setPrimary(parent?.id ?? null);
    editingId.value = null;
    status.value = '已删除该节点及其子节点，可撤销。';
  }

  /**
   * 拖动过程中的**预演**：这一落会改出什么，以及它落在哪个像素上。
   *
   * 为什么要有它——这是这一轮最核心的一个决定。布局是**自动算的**（`layoutDiagram`
   * 从 content 推所有坐标），拖动是**手动的**；两者只能有一个赢。松手之后节点落在
   * **布局给的位置**（理由见 `docs/NODE-DRAG.md` §1：坐标的唯一来源不能有两个），
   * 代价是"拖过去、弹回来"的观感。
   *
   * 消掉这个代价的办法不是留住手动坐标，而是**在松手之前就把结果画出来**：
   * 这里算出的 `rect` 就是松手之后的真实位置（同一个 `layoutTidy`、同一棵假设树），
   * 于是"弹回"变成"吸附"——用户看见幽灵框在哪里，节点就落到哪里。
   *
   * 它同时是非法情形的第二道出口：`ok=false` 时画布不画幽灵框、改画一句原因，
   * 于是**禁止发生在松手之前**，而不是松手之后弹一句"操作失败"。
   */
  function previewMove(intent: DropIntent): MovePreview {
    const current = content.value;
    if (!current || !isTree(current)) {
      return { ok: false, code: 'not-tree', reason: '这张图还不能拖动改结构（本轮只做了树型）' };
    }
    const plan = planDrop(current.root, intent);
    if (!plan.ok) return { ok: false, code: plan.code, reason: plan.reason };

    // 落点用**与真正落地时同一个**布局函数算：两处各算一次的话，幽灵框与松手后的
    // 位置会差一个常量，而那种偏差看起来像"差一点没对准"，用户会反复微调。
    const geometry = layoutTidy(plan.root, { direction: direction.value });
    const center = geometry.positions.get(intent.draggedId);
    if (!center) return { ok: false, code: 'missing', reason: '算不出落点' };

    return {
      ok: true,
      parentId: plan.parentId,
      index: plan.index,
      rect: {
        x: center.x - NODE_SIZE.width / 2,
        y: center.y - NODE_SIZE.height / 2,
        ...NODE_SIZE,
      },
    };
  }

  /**
   * 松手：把这次拖动落到 `content` 上。
   *
   * 它是一次**内容改动**，所以 `applyContent(patched, true)` 一步就把四件事办全：
   * 压一步撤销（`user` 名下的 `move` 事件）、置脏、递增 `clientRevision`、
   * 交给 `diffEdits` 记账。拖动**不是**视图操作——这与收起/展开那条线泾渭分明。
   *
   * 非法情形不写 `content`，但**也不静默**：`status` 说清为什么。用户拖了一下
   * 什么都没发生，与拖了一下弹出一句原因，是两种体验；而这条路径上"什么都没发生"
   * 的成因有六种。
   */
  function moveNodeTo(intent: DropIntent): MovePreview {
    const current = content.value;
    if (!current || !isTree(current)) {
      return { ok: false, code: 'not-tree', reason: '这张图还不能拖动改结构（本轮只做了树型）' };
    }

    const plan = planDrop(current.root, intent);
    if (!plan.ok) {
      status.value = plan.reason;
      return { ok: false, code: plan.code, reason: plan.reason };
    }

    if (!applyContent({ ...current, root: plan.root }, true)) {
      return { ok: false, code: 'missing', reason: '这次没能改动内容' };
    }

    // 落点可能正藏在某个收起来的折叠点后面（结构变了，可见集合也跟着变）。
    // 不补这一句，用户会看到"拖完之后节点不见了"。
    revealInView(intent.draggedId);
    setPrimary(intent.draggedId);
    status.value = dropMessage(current.root, intent, plan);
    return { ok: true, parentId: plan.parentId, index: plan.index };
  }

  /**
   * 在当前树里连一条**附加关系**（侧栏「连一条关系」的入口）。
   *
   * 三件事与 `addEdge`（`edits.ts` 的纯函数）分工，这里只做"界面语义"那部分：
   *   - **节点必须存在**：面板上的下拉框只会列出真实节点，但 store 是公开 API，
   *     别处（测试、将来的快捷键）传进来的 id 也要走同一条判据；
   *   - **自环与重复在 `addEdge` 里抛错**，这里转成 `status` 文案而不是异常：
   *     用户点错一次不该让整个组件树炸掉；
   *   - **记一步撤销**：连线是内容修改，凭什么不能 Ctrl+Z。
   */
  function connectNodes(
    source: string, target: string, label?: string | null, kind?: EdgeKind | null,
  ): boolean {
    if (!content.value) return false;
    if (!findIn(content.value, source) || !findIn(content.value, target)) {
      status.value = '端点节点不存在';
      return false;
    }
    // 上限**按每个数组各自算 200**（不是合计，也不是"两类各算"）。判据与契约逐条
    // 对应：`_validate_edges` 是**按数组各自**调用的（`FlowStage.edges` 逐阶段一次），
    // 后端 `ops._apply_add_flow_edge` 也只看 `len(sibling)`——它数的是"这条边要落进的
    // 那个数组"，而不是全部阶段的内部边之和。
    //
    // 拿 `contentInnerEdges.length`（全部阶段合计数）当判据是错的：两个阶段各有 100
    // 条时它等于 200，于是前端把一条本该允许的边拦下来，而同样的动作走后端
    // （`apply_flow_edge_ops`）能成功——用户看到的是"这个阶段内部最多 200 条"，
    // 但这个阶段其实只有 100 条。
    const sourceStage = stageOfNode(content.value, source);
    const sameStage = sourceStage !== null && sourceStage === stageOfNode(content.value, target);
    const used = sameStage
      ? innerEdgeCount(content.value, sourceStage)
      : contentEdges.value.length;
    if (used >= LIMITS.edges) {
      status.value = sameStage
        ? `这个阶段内部最多 ${LIMITS.edges} 条附加关系，请先删掉一些`
        : `最多 ${LIMITS.edges} 条附加关系，请先删掉一些`;
      return false;
    }
    let next: DiagramContent;
    try {
      next = addEdgeToContent(content.value, source, target, label, kind).content;
    } catch (error) {
      status.value = error instanceof Error ? error.message : '这条关系加不上';
      return false;
    }
    if (!applyContent(next, true)) return false;
    status.value = sameStage
      ? '已加一条阶段内部的附加关系，记得保存。'
      : '已加一条附加关系，记得保存。';
    return true;
  }

  /**
   * 删一条附加关系。删线不动结构，所以不检查节点数/层数，也不需要重算布局。
   *
   * **两个数组都查**（`removeEdge` 里判层级）：阶段内的边只查 `content.edges` 的话，
   * 侧栏列得出来却删不掉——按钮点了没反应。
   */
  function deleteEdge(edgeId: string): boolean {
    if (!content.value) return false;
    const known = contentEdges.value.some(edge => edge.id === edgeId)
      || contentInnerEdges.value.some(edge => edge.id === edgeId);
    if (!known) return false;
    if (!applyContent(removeEdgeFromContent(content.value, edgeId), true)) return false;
    status.value = '已删除这条附加关系，可撤销。';
    return true;
  }

  /**
   * 删一条跨节点关系线。
   *
   * 与 `deleteEdge` 分开的理由见 `edits.removeRelation`：两套字段、两套 id 命名空间。
   * 这里不做"未知 id 直接 false"之外的校验——关系线不参与布局，删它不改变任何节点
   * 的坐标，所以不需要重排。
   */
  function deleteRelation(relationId: string): boolean {
    const current = content.value;
    if (!current) return false;
    if (!(current.relations ?? []).some(relation => relation.id === relationId)) return false;
    if (!applyContent(removeRelationFromContent(current, relationId), true)) return false;
    status.value = '已删除这条关系线，可撤销。';
    return true;
  }

  function undo() {
    const snap = past.value.pop();
    if (!snap) return;
    pastBytes.value -= snapshotBytes(snap);
    // 当前是空画布时也要压一步 `'null'`，否则「空 → 生成 → 撤销 → 重做 → 撤销」
    // 的最后一步会丢掉：重做那一次不记历史，再撤销就没得可撤，回不到空白。
    future.value.push(snapshot() ?? 'null');
    restoreSnapshot(snap);
    dirty.value = true;
    // 撤销改的就是画布本身，必须让"这份结果基于哪个版本算出来"的判断看见这一步。
    // 不递增的话，生成期间用户撤销一步、生成结果照样把画布整棵盖掉——那正是
    // `commitGenerated` 里那条 `clientRevision` 判据要拦下的情形。
    clientRevision.value += 1;
    ensureSelection();
    editingId.value = null;
    status.value = '已撤销。';
  }

  function redo() {
    const snap = future.value.pop();
    if (!snap) return;
    // 与 undo 对称：当前是空画布时把空画布压进撤销栈，这样重做之后仍能撤回空白。
    recordPast(snapshot() ?? 'null');
    restoreSnapshot(snap);
    dirty.value = true;
    // 同 undo：重做同样把画布换成了另一棵树，生成结果再不成立。
    clientRevision.value += 1;
    ensureSelection();
    editingId.value = null;
    status.value = '已重做。';
  }

  /** 打开一张已保存的导图：撤销栈、选中、脏标记全部重置到"刚读出来"的状态。 */
  /**
   * 当前页面"正在看哪张图"的会话号。每次 loadMap / newMap 递增。
   *
   * 保存请求在途时用户可能切到另一张导图，那份响应回来时不能再拿它改当前
   * 页面的身份：否则 mapId 会被改回上一张，而画布上显示的还是新的那张，
   * 之后保存就会把 B 的内容写进 A。用会话号比比较 mapId 更严格——它也覆盖
   * 「保存中点了新建」（mapId 前后都是 null）这种情形。
   */
  let mapSession = 0;

  function loadMap(next: DiagramContent, record: { id: string; version: number; updated_at: string }) {
    cancel();
    mapSession += 1;
    // 换了一张图，上一轮生成的工作副本与"改了什么"没有任何意义：留着它，一次
    // 误操作就会把那张图的内容装进这张图。
    draft.value = null;
    resetDraftOps();
    generatedChanges.value = null;
    generatedSummary.value = null;
    conflictDraft.value = null;
    saving.value = false;
    titleDraft.value = null;
    content.value = slot(next);
    mapId.value = record.id;
    version.value = record.version;
    savedAt.value = record.updated_at;
    mode.value = 'saved';
    dirty.value = false;
    setPrimary(null);
    editingId.value = null;
    viewAdjusted.value = false;
    resetHistory();
    status.value = `已打开导图，版本 ${record.version}。`;
  }

  function newMap() {
    cancel();
    mapSession += 1;
    // 同上：新建之后旧的工作副本不再属于画布上的任何一张图。
    draft.value = null;
    resetDraftOps();
    generatedChanges.value = null;
    generatedSummary.value = null;
    conflictDraft.value = null;
    saving.value = false;
    titleDraft.value = null;
    content.value = null;
    mapId.value = null;
    version.value = 0;
    savedAt.value = null;
    mode.value = null;
    dirty.value = false;
    setPrimary(null);
    editingId.value = null;
    resetHistory();
    status.value = '新建导图：输入主题生成，或直接在画布上手动编辑。';
  }

  function markSaved(record: { id: string; version: number; updated_at: string }) {
    mapId.value = record.id;
    version.value = record.version;
    savedAt.value = record.updated_at;
    dirty.value = false;
    mode.value = 'saved';
    status.value = `已保存，版本 ${record.version}。`;
  }

  /**
   * 保存。返回结果让界面分流提示：正常、内容不合法、版本冲突各走各的。
   * 失败时保留 dirty —— 一次网络抖动不能让用户以为已经存上了。
   */
  async function save(): Promise<SaveResult> {
    // 生成中途保存必然出脏数据：`dirty` 还是 false、`mapId` 还可能是 null，
    // POST 出去就是一条半截记录，`done` 到达后再保存又多出第二条。挡在这里，
    // 而不是留给后端去猜哪条该丢。
    if (busy.value || agentRunning.value) {
      status.value = '正在生成导图，完成之后才能保存。';
      return 'busy';
    }
    const current = content.value;
    if (!current) {
      status.value = '没有可保存的导图。';
      return 'empty';
    }
    // 朝向存在 content 里，但生成与示例拿到的 content 可能还带着后端默认值：
    // 保存前按当前视图归一，否则会出现"看着是向左、存下去是向右"。
    const payload = withDirection(current, direction.value);
    const invalid = validateDraft(payload);
    if (invalid) {
      status.value = invalid;
      return 'invalid';
    }
    if (saving.value) return 'busy';

    saving.value = true;
    status.value = '正在保存…';
    const session = mapSession;
    // 事件跟着内容一起提交，不另开端点：**第一次保存前根本没有 `map_id`**，
    // 一个 `POST /mindmaps/{id}/edits` 要么发不出去，要么必须先 create 再补发——
    // 那就多出"内容存了、事件没存"这种半截状态，而统计从此只看见部分改动。
    //
    // 超限的部分**不是被丢掉**，而是留在 `pendingEdits` 里等下一次保存——截断是
    // 分批，不是丢弃。`truncated` 只报"这次没带上多少条"，后端记一条 warning；
    // **不在表里插一条假事件**：`total` 必须是真实改动数，运维事件混进行为事件会
    // 让每一个统计口径都脏掉。
    const batch = pendingEdits.value.slice(0, EDIT_EVENT_LIMIT);
    const truncated = pendingEdits.value.length - batch.length;
    try {
      const record = mapId.value
        ? await updateMindMap(mapId.value, payload, version.value, batch, truncated)
        : await createMindMap(payload, batch, truncated);

      // 请求在途时用户可能已经切到别的导图（或点了新建）。这时响应属于
      // 上一张图，不能拿它改当前页面的 mapId —— 改了之后画布显示的是 B、
      // 身份却是 A，再点保存就会把 B 的内容写进 A。
      if (session !== mapSession) {
        status.value = `上一张导图已保存（版本 ${record.version}），当前导图未受影响。`;
        return 'saved';
      }

      // 内容与事件在同一次请求里落库，所以"保存成功"就是"这批事件收下了"。
      // 用 `slice(batch.length)` 而不是清空，两种情形都靠它：
      //  - 请求在途时用户又改了几处 —— 那几条还没上过网，必须留给下一次；
      //  - 这批超过 500 条被截 —— 剩下的同样留在队首之后，下一次继续发。
      if (batch.length) pendingEdits.value = pendingEdits.value.slice(batch.length);

      // 请求在途时用户可能还在编辑。applyContent() 是整体替换 content.value，
      // 所以比一次引用就知道内容有没有变过。变了必须把脏标记恢复回来——
      // markSaved() 无条件把它清成 false，那样后改的内容会被误标成"已保存"，
      // 而请求体里发出去的只是点保存那一刻的树，刷新即丢。
      const changedDuringSave = content.value !== current;
      markSaved(record);
      // 内容与事件都落库了 → 这次生成的结果算"存下来了"，服务端那条台账到此为止
      // （下次进页面不再问"要不要捡回上次的结果"）。
      reportRunSaved();
      if (changedDuringSave) {
        dirty.value = true;
        status.value = `已保存版本 ${record.version}，但保存期间又有修改，请再次保存。`;
      }
      return 'saved';
    } catch (error) {
      if (session !== mapSession) {
        return 'saved';
      }
      if (error instanceof ApiError && error.status === 409) {
        conflict.value = { message: error.message };
        status.value = '这张导图已被别人改过，本地修改还没保存。';
        return 'conflict';
      }
      // 403 只可能来自一件事：这张图不是你创建的（门禁有身份就能过，所以不是登录问题）。
      // 后端那句话已经说了下一步（另存为新建一份），这里原样透传——自己再编一句只会
      // 与后端口径漂移，而"为什么存不了"这件事只有一个正确答案。
      if (error instanceof ApiError && error.status === 403) {
        status.value = error.message;
        return 'failed';
      }
      // 401 = 身份过期（会话 30 天到期、换了浏览器、清了 Cookie）。保存是**唯一**
      // 一个用户会反复点的动作，所以这一档必须说清"刷新页面"而不是"重试"：
      // 原样重发还是会 401，而刷新一下就会领到新的游客身份。
      if (error instanceof ApiError && error.status === 401) {
        status.value = '这一页的身份过期了：刷新页面后重新打开这张图再保存。';
        return 'failed';
      }
      status.value = error instanceof ApiError ? error.message : '保存失败，请重试。';
      return 'failed';
    } finally {
      // 切图后新页面的保存状态不能被打扫：只清理自己这一轮。
      if (session === mapSession) saving.value = false;
    }
  }

  /** 409 的选项一：放弃本地修改，换成服务端最新版本（撤销栈一并清空）。 */
  async function reloadFromServer(): Promise<void> {
    const id = mapId.value;
    if (!id) return;
    const session = mapSession;
    try {
      const record = await getMindMap(id);
      // 等待期间用户可能已经新建或切到别的图，这份响应不能再盖上去。
      if (session !== mapSession) return;
      loadMap(record.content, record);
      conflict.value = null;
    } catch (error) {
      status.value = error instanceof ApiError ? error.message : '读取服务端版本失败。';
    }
  }

  /** 409 的选项二：把当前内容存成一条新记录，两边的修改都保留。 */
  async function saveAsNew(): Promise<void> {
    const current = content.value;
    if (!current) return;
    const session = mapSession;
    // 另存为同样把事件带上：新记录的内容里就含着这批改动，事件挂在旧 `map_id`
    // 上就成了"改动在 A，内容在 B"。
    const batch = pendingEdits.value.slice(0, EDIT_EVENT_LIMIT);
    const truncated = pendingEdits.value.length - batch.length;
    try {
      const record = await createMindMap(current, batch, truncated);
      if (session !== mapSession) {
        return;
      }
      if (batch.length) pendingEdits.value = pendingEdits.value.slice(batch.length);
      const changedDuringSave = content.value !== current;
      markSaved(record);
      conflict.value = null;
      reportRunSaved();
      if (changedDuringSave) {
        dirty.value = true;
        status.value = `已另存为新导图（版本 ${record.version}），但保存期间又有修改，请再次保存。`;
      } else {
        status.value = `已另存为新导图，版本 ${record.version}。`;
      }
    } catch (error) {
      status.value = error instanceof ApiError ? error.message : '另存失败，请重试。';
    }
  }

  /** 409 的选项三：先不处理，本地修改继续留在页面上。 */
  function dismissConflict() {
    conflict.value = null;
    status.value = '已取消保存，本地修改还在，可以再试一次。';
  }

  /**
   * 只把"已保存的导图"列表拉回来，**不打开**那个模态。
   *
   * `☰` 抽屉里要直接列出历史，而 `openList()` 会同时把选择模态弹出来（它盖住整个
   * 界面，抽屉就被压在下面点不到了）。两件事拆开：抽屉用这个，`打开…` 与老入口用
   * `openList()`。
   */
  async function loadMaps(): Promise<void> {
    listBusy.value = true;
    try {
      maps.value = await listMindMaps();
    } catch (error) {
      maps.value = [];
      status.value = error instanceof ApiError ? error.message : '读取导图列表失败。';
    } finally {
      listBusy.value = false;
    }
  }

  async function openList() {
    listOpen.value = true;
    listBusy.value = true;
    try {
      maps.value = await listMindMaps();
    } catch (error) {
      maps.value = [];
      status.value = error instanceof ApiError ? error.message : '读取导图列表失败。';
    } finally {
      listBusy.value = false;
    }
  }

  function closeList() {
    listOpen.value = false;
  }

  async function openMap(id: string) {
    const session = mapSession;
    listBusy.value = true;
    try {
      const record = await getMindMap(id);
      // 点开之后等网络这段时间，用户可能已经关掉列表并新建了一张图。
      // 直接 loadMap 会把那张新画布替换成刚读回来的旧记录。
      if (session !== mapSession) return;
      loadMap(record.content, record);
      listOpen.value = false;
      conflict.value = null;
    } catch (error) {
      status.value = error instanceof ApiError ? error.message : '打开导图失败。';
    } finally {
      listBusy.value = false;
    }
  }

  /**
   * 换主选区，并丢掉追加选区。
   *
   * 所有"跳到某个节点去干活"的路径都走它（点选、加子节点、加兄弟、删完跳父节点、
   * 开始内联编辑、换图）：那些动作的语义是"现在只对着这一个节点"，留着上一批
   * 追加选区，下一次「改这张图」会带上一串用户早就忘了自己选过的 id。
   */
  function setPrimary(id: string | null) {
    extraSelectedIds.value = [];
    selectedId.value = id;
  }

  /**
   * 选中一个节点（清掉多选）。
   *
   * 只接受 `content` 里真实存在的节点。预览的派生 id 还没进画布：
   * 选中它们会让右侧详情面板、面包屑，以及 `expand` 请求里的 `selected_ids` 一起
   * 对着一个不存在的节点说话。画布那边同样给这些节点设了 `selectable: false`，
   * 这里是不依赖渲染层的最后一道。
   */
  function select(id: string | null) {
    if (id !== null && !(content.value && findIn(content.value, id))) return;
    setPrimary(id);
    // 选中即可见：从侧栏引用列表、从「改这张图」的改动清单点过来的节点，可能正藏在
    // 某个折叠点后面。详情面板已经换成了它，画布上却没有任何东西亮起来——那种
    // "我选了一个看不见的节点"的状态不该存在（见 `revealInView`）。
    if (id !== null) revealInView(id);
  }

  /**
   * 在画布上按住 Ctrl / Cmd / Shift 点一个节点：加进选区或从选区里拿掉。
   *
   * 主选区被取消时把第一个追加选区提上来（详情面板不能停在"没选中但有选区"
   * 那种状态：`selectedIds` 非空而 `selectedNode` 为 null，界面上每一个按钮都
   * 不知道自己该对谁说话）。
   *
   * 用例与 `select` 同一条判据：只有 `content` 里真实存在的节点才进得来。
   */
  function toggleSelect(id: string): void {
    if (!content.value || !findIn(content.value, id)) return;
    const rest = extraSelectedIds.value.filter(item => item !== id);
    if (selectedId.value === id) {
      const [promoted, ...remaining] = rest;
      selectedId.value = promoted ?? null;
      extraSelectedIds.value = remaining;
      return;
    }
    if (rest.length !== extraSelectedIds.value.length) {
      extraSelectedIds.value = rest;
      return;
    }
    if (selectedId.value === null) {
      selectedId.value = id;
      return;
    }
    extraSelectedIds.value = [...rest, id];
  }

  /** 清空整个选区：画布上点空白处走它。 */
  function clearSelection(): void {
    setPrimary(null);
  }

  /**
   * 从引用列表跳到节点：选中它，并让画布把它移到视野中心。
   *
   * 与 `select()` 分开的原因见 `focusTick`：跳转是"我要看这个"，点选是"我在看这个"。
   */
  function locate(id: string) {
    select(id);
    if (selectedId.value !== id) return;
    focusTick.value += 1;
  }

  /**
   * 画布上点了一个折叠点的「+N」：把它没画出来的子节点放出来。
   *
   * 只动视图（`viewExpandedNodes`），不动 `content`——所以它不进撤销栈、不置脏。
   * 想退回去要点画布顶部的「收起到上限」、或者再点一下这个节点，而不是 Ctrl+Z：
   * 那一条撤销的是内容，用内容撤销去退视图，用户会发现"撤销一步"把他刚改的字也吃了。
   */
  function expandNodeInView(id: string): void {
    // 两个集合互斥（见 `viewCollapsedNodes`）：展开一个被收起过的节点，它必须真的出现，
    // 所以两边一起改，而不是只加一边。
    if (!viewExpandedNodes.value.has(id) || viewCollapsedNodes.value.has(id)) {
      const next = new Set(viewExpandedNodes.value);
      next.add(id);
      viewExpandedNodes.value = next;
    }
    if (viewCollapsedNodes.value.has(id)) {
      const rest = new Set(viewCollapsedNodes.value);
      rest.delete(id);
      viewCollapsedNodes.value = rest;
    }
  }

  /**
   * 把某个节点的子节点叠回去（**保持界面清爽**的那一半）。
   *
   * 两条完全不同的回程，按"这些子节点当初是被谁放出来的"分：
   *
   * - **用户点开的**（`viewExpandedNodes` 里有它）→ 撤销这次展开，回到预算那一档。
   *   "点开 A 冒出 3 个，再点 A 收回去"必须回到点之前那一屏，而不是比点之前更少
   *   ——否则用户点两下就丢了本来就看得见的东西。
   * - **预算给的 / 全部展开给的** → 真的把它藏起来（进 `collapsed`）。这时可见数**会**
   *   变少，那正是"保持界面清爽"要的效果：收起一支，别的地方不会莫名其妙冒出节点
   *   （预算是一个固定前缀，不是边走边扣的余额，见 `viewTree` 第一步）。
   */
  function collapseNodeInView(id: string): void {
    const undoExpand = !viewExpandAll.value && viewExpandedNodes.value.has(id);
    if (undoExpand) {
      const rest = new Set(viewExpandedNodes.value);
      rest.delete(id);
      viewExpandedNodes.value = rest;
      return;
    }
    if (!viewCollapsedNodes.value.has(id)) {
      const next = new Set(viewCollapsedNodes.value);
      next.add(id);
      viewCollapsedNodes.value = next;
    }
    if (viewExpandedNodes.value.has(id)) {
      const rest = new Set(viewExpandedNodes.value);
      rest.delete(id);
      viewExpandedNodes.value = rest;
    }
  }

  /**
   * **单击一个节点**：按它此刻的状态决定"展开 / 收起 / 什么都不做"。
   *
   * 判据收敛在 `foldActionOf` 一处（与渲染层给不给「−」角标是同一处），所以不会出现
   * "角标说能收起、点节点却只选中"这种两条路分家。
   *
   * `none` 那一档是刻意的：**预算本来就够**的节点看起来没有任何"更多"，用户点它是
   * 为了看它、选它（右侧详情区块）；让它的子节点突然塌掉是纯粹的伤害。想收这种节点
   * 有两条明确的路——它头上的「−」（悬停/选中时出现）与顶部的「收起到上限」。
   */
  function toggleFoldInView(id: string): void {
    const current = content.value;
    if (!current || !isTree(current)) return;
    const node = findIn(current, id);
    if (!node || !node.children.length) return;
    const view = {
      folded: renderSource.value?.folded ?? NO_FOLDS,
      open: renderSource.value?.foldOpen ?? NO_FOLD_OPEN,
    };
    const action = foldActionOf(id, view);
    if (action === 'expand') expandNodeInView(id);
    else if (action === 'collapse') collapseNodeInView(id);
  }

  /**
   * **展开一整支**：一级分支上那枚概括角标点下去走这里。
   *
   * 为什么不是 `expandNodeInView(id)`（= `+N` 那一层）：角标上写的是**整支**的
   * 数字（「还有 21 个」），点下去就该冒出 21 个。写成"只放一层"会得到一个比 `+N`
   * 更没用的角标——预算铺满时一级分支的孩子通常**已经全在画布上**，再放一次等于
   * 什么都没发生（`view-collapse.ts` 的 BFS 前三层就是这样）。**数字决定粒度**，
   * 这条规则 `+N` 与它共用：`+N` 写的是这一处缺几个，所以只放一层。
   *
   * 做法是把这一支里**每一个有孩子的节点**都放进 `expanded`（"用户要求看它"那一档），
   * 同时清掉这一支内部的收起标记。视图层不用新增概念——`expanded` 的语义本来就是
   * "无视预算，自己的全部子节点一律显示"，逐节点交上去就是整支摊开。
   *
   * 代价是这一支里每个有孩子的节点都会挂一枚「−」（叶子不挂，`canCollapse` 判着）：
   * 一支 22 个节点里通常只有 4 个内部节点，而用户刚刚明确要求了"看这一支"。回程有
   * 顶部「收起到上限」与逐枚「−」两条。
   */
  function expandBranchInView(id: string): void {
    const current = content.value;
    if (!current || !isTree(current)) return;
    const branch = findIn(current, id);
    if (!branch || !branch.children.length) return;
    const nextExpanded = new Set(viewExpandedNodes.value);
    const nextCollapsed = new Set(viewCollapsedNodes.value);
    const walk = (node: MindNode): void => {
      nextCollapsed.delete(node.id);
      if (node.children.length) nextExpanded.add(node.id);
      for (const child of node.children) walk(child);
    };
    walk(branch);
    viewExpandedNodes.value = nextExpanded;
    viewCollapsedNodes.value = nextCollapsed;
  }

  /**
   * 「全部展开」：一个节点都不收起。
   *
   * 这是收起的**逃生舱**，也是"这棵树到底有多大"的唯一诚实答案。两个集合一起清空：
   * 用户说的是"我全都要看"，那他刚才逐支收起的那几处到这个时刻就该作废。
   */
  function expandAllInView(): void {
    viewExpandAll.value = true;
    viewExpandedNodes.value = new Set<string>();
    viewCollapsedNodes.value = new Set<string>();
  }

  /**
   * 「收起到上限」：回到上限以内，并清掉所有手动点开/收起。
   *
   * 这是**回程路**：全展开之后（字小到看不清）必须有一条一次到位的退路，
   * 否则「全部展开」就是一次性的、不可撤销的破坏。
   */
  function collapseViewToCap(): void {
    viewExpandAll.value = false;
    viewExpandedNodes.value = new Set<string>();
    viewCollapsedNodes.value = new Set<string>();
  }

  /**
   * 换了一张图：展开程度回到出厂设置。
   *
   * 三个视图状态必须清——那些 id 属于上一棵树。留着不会立刻出错（新树里没有那些
   * id），但它是每次换图都会长大一点的内存泄漏，而且下一次换到一棵**恰好复用同 id**
   * 的树时会莫名多露出几处。
   *
   * **上限本身不清**：它是"这个人一次想看几个节点"的偏好，与哪棵树无关。
   */
  function resetViewCollapse(): void {
    viewExpandAll.value = false;
    viewExpandedNodes.value = new Set<string>();
    viewCollapsedNodes.value = new Set<string>();
  }

  /**
   * "选中即可见"：把某个节点的祖先链放出来，直到它在画布上有出口。
   *
   * 为什么必须有这一条：收起之后，从引用列表跳到第 4 层的某个节点、或者删掉一个节点
   * 之后跳回它的父节点，目标完全可能正藏在某个折叠点后面。那时画布上什么都不变、
   * 详情面板却已经换成了那条内容——用户以为自己点错了。视图不许让"正在编辑一个
   * 看不见的节点"这种状态存在。
   *
   * 判据从"层号"换成了**"它在不在画布上"**（`flatNodes` 就是这一屏画出来的那些）：
   * 数量口径下没有"层号 ≤ N"这种可算的边界，唯一可靠的问法就是问画布。
   *
   * 只对树型生效：鱼骨与流程不收起，往里塞 id 只会让换图时的清理多几个条目。
   */
  function revealInView(id: string): void {
    const current = content.value;
    if (!current || !isTree(current)) return;
    // 已经在画布上：什么都不用做。少了这一句，每次点一个可见的深层节点都会把它的
    // 整条祖先链撑成全展开——点一下，画布上突然多出几十个节点。
    if (flatNodes.value.some(entry => entry.node.id === id)) return;
    const path = pathToIn(current, id);
    if (!path || path.length < 2) return;
    let next: Set<string> | null = null;
    let rest: Set<string> | null = null;
    for (const ancestor of path.slice(0, -1)) {
      if (!ancestor.children.length) continue;
      if (viewCollapsedNodes.value.has(ancestor.id)) {
        if (!rest) rest = new Set(viewCollapsedNodes.value);
        rest.delete(ancestor.id);
      }
      if (viewExpandedNodes.value.has(ancestor.id)) continue;
      if (!next) next = new Set(viewExpandedNodes.value);
      next.add(ancestor.id);
    }
    if (rest) viewCollapsedNodes.value = rest;
    if (next) viewExpandedNodes.value = next;
  }

  /**
   * 换朝向。朝向写在 content 里，所以这是一次可撤销的内容修改：
   * 换了方向之后按 Ctrl+Z 能退回原布局，和改文字是同一套语义。
   */
  function setDirection(next: Direction) {
    if (!content.value) return;
    if (direction.value === next) return;
    if (!applyContent(withDirection(content.value, next), true)) return;
    // 换方向后整体重排，视图必须重新适配，否则可能停在空白处。
    viewAdjusted.value = false;
  }

  /**
   * 不经过网络直接装载一份导图，开发期的示例选择器用它预览布局。
   * 走 cancel() 先掐掉在途请求，避免旧流回来后把示例顶掉。
   */
  function preview(next: DiagramContent, dir: Direction) {
    cancel();
    // 换了一张图，会话必须跟着换：否则上一张图在途的保存响应回来时，
    // 会因为会话号没变而把它的 id 写进 mapId——画布上是新图、身份却是旧图，
    // 下次保存就把新内容 PUT 到旧记录上。
    mapSession += 1;
    // 换图即作废这一轮的工作副本与生成记录（preview 同样会换掉整棵树）。
    draft.value = null;
    resetDraftOps();
    generatedChanges.value = null;
    generatedSummary.value = null;
    conflictDraft.value = null;
    titleDraft.value = null;
    // 上一张图在途的保存到这里就作废了：它的 finally 因为会话不匹配不会
    // 清理 saving，这里必须自己复位，否则新图永远卡在"保存中"，点保存
    // 只会返回 busy。loadMap / newMap / generate 都是同一套处理。
    saving.value = false;
    content.value = slot(withDirection(next, dir));
    mode.value = 'demo';
    setPrimary(null);
    editingId.value = null;
    viewAdjusted.value = false;
    // 示例只是用来看布局的，不算用户的作品，别让它挂上"未保存"的包袱。
    mapId.value = null;
    version.value = 0;
    savedAt.value = null;
    dirty.value = false;
    resetHistory();
    status.value = '这是本地示例数据，用来检查布局与截断，不是模型生成的。';
  }

  /**
   * 装载一份**导入**的导图。
   *
   * 与 `preview` 的三处差别，每一处都是刻意的：
   *
   *  1. `dirty = true`。导入的是用户的作品，他必须被提醒"还没保存"。示例数据是
   *     拿来检查布局的，不算作品，所以那边是 false。
   *  2. `mode = 'import'`。徽标要说清这张图的来历；混进 `saved` 会让人以为它已经
   *     在库里了，混进 `demo` 会让人以为这是假数据（他会不敢保存）。
   *  3. `mapId = null`。后端导入**只解析不落库**，所以第一次保存走 `createMindMap`
   *     建一条新记录，而不是 PUT 到某张既有图上。
   *
   * 朝向不经过 `withDirection`：文件自带的 `theme.direction` / `layout` 就是用户
   * 原来看到的样子，`direction` 是 content 的派生值（`directionOf`），设好 content
   * 之后画布自然按它排。强行套一个默认朝向会把一份"向左"的图导成向右。
   *
   * `resetHistory()`：导入不是一个可以撤销的编辑步骤——撤销回到导入前就意味着
   * "把画布清空"，而那不是用户点撤销时想要的东西。
   */
  function importMap(next: DiagramContent) {
    cancel();
    // 换图必须换会话号，否则上一张图在途的保存响应回来时会把它的 id 写进 mapId，
    // 而画布上是这张新导入的图——下次保存就把导入的内容 PUT 到旧记录上。
    mapSession += 1;
    draft.value = null;
    resetDraftOps();
    generatedChanges.value = null;
    generatedSummary.value = null;
    conflictDraft.value = null;
    titleDraft.value = null;
    // 与 preview 同一条：上一张图在途的保存作废，saving 必须自己复位，否则新图
    // 永远卡在"保存中"。
    saving.value = false;
    content.value = slot(next);
    mode.value = 'import';
    setPrimary(null);
    editingId.value = null;
    viewAdjusted.value = false;
    mapId.value = null;
    version.value = 0;
    savedAt.value = null;
    dirty.value = true;
    resetHistory();
    status.value = `已导入「${next.title}」，还没有保存。`;
  }

  /**
   * 载入一张预置的演示导图（内容见 `frontend/src/demo/presets.ts`）。
   *
   * 复用 `preview` 的"换图即换会话"处理：不做的话，上一张图在途的保存响应回来时
   * 会把旧 id 写进 `mapId`，画布是新图、身份却是旧图。`mapId` 保持为空，所以第一次
   * 保存会新建记录，不会覆盖别人的图。
   *
   * 只改一句状态文案：预置图是内容完整的演示材料，不是"用来看布局的示例"。
   */
  function loadPreset(preset: {
    label: string;
    content: DiagramContent;
    direction: Direction;
  }) {
    preview(preset.content, preset.direction);
    status.value = `已载入预置演示导图「${preset.label}」：可以直接改，也可以保存成一张新导图。`;
  }

  /**
   * 掐掉在途的 agent 运行。
   *
   * 取消就是**真正的"不要了"**：这一轮的结果从未进过画布（`draft` 只是工作副本，
   * `commitGenerated` 才写 `content`），所以不需要回滚，清掉属于这一轮的中间态就够了。
   * 这也是删掉候选层之后仍然保留的那半个保护——生成 25 秒里发现方向不对，点取消，
   * 画布一点没变。
   * 换图场景（loadMap / newMap / preview）也走它，把旧流掐死。
   */  function cancel() {
    const active = controller;
    controller = null;
    active?.abort();
    busy.value = false;
    agentRunning.value = false;
    // 这一轮就此作废：告警跟着一起清，否则取消之后面板还挂着"部分分支未完成"。
    agentWarnings.value = [];
    // 取消/换图同理：长到一半的预览属于已经结束的那一次运行。
    clearAgentPreview();
    // 工作副本同理：它属于被取消的那一轮，留着只会在下一轮开跑前短暂地
    // 影响一次 ops 批次的基树判断。
    draft.value = null;
    resetDraftOps();
    stageText.value = '';
    // 这一轮已经结束，"等了多久"不再有意义：不清的话，静默超 10s 后点取消，
    // 界面会一边写"已取消。"、一边挂着"模型正在思考，已等 20 秒"。
    waitedMs.value = 0;
    status.value = '已取消。';
  }

  // ---- agent 运行与生成落地 ----

  /**
   * 把一批 op 应用到工作副本上，返回是否应用成功。
   *
   * 只动 `draft`，绝不写 `content`：`ops` 是一次几处的增量，`edit` 意图下模型可能
   * 连调十几次，每条都落画布等于把 25 秒的中间态全摊给用户、撤销栈切成十几步半成品。
   * 整树只在 `done` 那一刻进画布（`commitGenerated`），那一次是原子的、可撤销的。
   *
   * 失败与乱序都**静默丢弃**：`done` 帧会带完整树覆盖工作副本，少应用一批不会丢
   * 数据；而在这种时候弹一句"修改没能应用"，用户只会以为最终结果不可信。
   */
  function applyOpsToDraft(batch: OpBatch): boolean {
    // edit / expand 这两条路上后端不发 snapshot（工作副本从请求带过去的树起步），
    // 所以副本要由画布那份树兜底——`applyOps` 自己会克隆，不会改到 content。
    // 不兜底的话第一批 op 就没有落点，"打开旧图 → 让 AI 改"依旧一点反应都没有。
    const base = draft.value ?? (generationIntent === 'create' ? null : content.value);
    if (!base) return false;
    // 对不上就是中间漏了一批：在错位的树上继续套用只会套出错位的结构。
    if (batch.base_revision !== draftRevision) {
      previewDebug(`[agent] 丢弃 ops 批次：base_revision=${batch.base_revision}，当前 ${draftRevision}`);
      return false;
    }
    try {
      draft.value = applyOps(base, batch).content;
    } catch (error) {
      previewDebug(`[agent] 丢弃 ops 批次：${error instanceof Error ? error.message : String(error)}`);
      return false;
    }
    // 只有真正落进副本才算这一版完成，失败那批不能推进账本。
    draftRevision = batch.base_revision + 1;
    return true;
  }

  async function runGeneration(
    goal: string,
    intent: 'create' | 'expand' | 'edit' = 'create',
    sources?: Source[],
    thinkingModel: string = 'default',
    diagramType: DiagramTypeChoice = 'auto',
    options: RunOptions = {},
  ): Promise<void> {
    if (busy.value || agentRunning.value) return;
    // 来源合法性在这里再判一次。组件里已有同样的闸门，但 `runGeneration` 是唯一
    // 入口——绕过组件直调（测试，或将来加的快捷入口）时那一层就不在了。判一次
    // 只省一次 422 往返，权威仍是后端契约校验。
    if (sources && sources.length > 0) {
      const problem = sourceInputError(sources);
      if (problem) {
        // 材料没解析好不是"运行失败"，它的下一步是改输入而不是重试——原样再发
        // 一遍，后端照样会把这几份剔掉。所以用单独的分类码。
        agentError.value = problem;
        agentErrorCode.value = INVALID_SOURCES;
        agentErrorLimit.value = '';
        status.value = problem;
        return;
      }
    }
    const prompt = goal.trim();
    if (!prompt) {
      // 这里最容易踩的坑：用户把主题写进了「导图标题」（那个框只改名字、
      // 不触发生成），然后点生成，界面毫无动静。所以提示必须指出是哪个框，
      // 同时清掉上一次运行的进度——不清的话面板还挂着上一轮的失败卡片，
      // 看起来像"这次又失败了"。
      agentEvents.value = [];
      agentError.value = '';
      agentErrorCode.value = '';
      agentErrorLimit.value = '';
      agentWarnings.value = [];
      clearAgentPreview();
      status.value = '请在画布右上角的输入框里写下主题或要改什么；「导图标题」那个框只改名字，不会重新生成。';
      return;
    }
    if (intent !== 'create' && !content.value) {
      status.value = '当前还没有导图，无法在此基础上修改。';
      return;
    }
    // 鱼骨与流程还不能交给模型改：后端 `AgentRunRequest.snapshot` 只收树形状
    // （`MindMapContent`，`extra="forbid"` 会把 `bones` / `stages` 拒成 422）。
    // 拦在这里给一句能读懂的话，好过让用户等一轮之后收到一条后端校验错误。
    if (intent !== 'create' && content.value && !isTree(content.value)) {
      status.value = kindOf(content.value) === 'fishbone'
        ? '鱼骨图还不能让模型改：后端目前只接受树形状的快照，这一步等它放宽之后才能用。'
        : '流程图还不能让模型改：后端目前只接受树形状的快照，这一步等它放宽之后才能用。';
      return;
    }

    agentError.value = '';
    // 上一轮失败分类的理由跟着一起清：留着的话新一轮刚开跑，失败卡片就已经在
    // 按上一轮的 code 解释了——而这一轮可能什么都还没发生。
    agentErrorCode.value = '';
    agentErrorLimit.value = '';
    // 记住这一次的完整参数，「重试」要原样再发一遍。放在清理之后、发请求之前：
    // 拦在闸门外的请求（没写主题、非树快照）不算"发出去过"，不该变成可重试的对象。
    lastAttempt.value = {
      goal: prompt,
      intent,
      sources: sources && sources.length > 0 ? [...sources] : undefined,
      thinkingModel,
      diagramType,
      // `options` 要连里面的两个数组一起拷：直接存调用方那个对象，等于把"上一次重试
      // 要用的参数"挂在界面的 ref 上——下一次它改 `selection`（比如用户又圈了几个
      // 节点），这条记录会跟着变，重试就悄悄换成了另一批选区。`sources` 上面已经
      // 这么拷贝了，这两个没有理由例外。
      options: {
        ...options,
        selection: options.selection ? [...options.selection] : undefined,
        prefsApplied: options.prefsApplied ? [...options.prefsApplied] : undefined,
      },
    };
    agentEvents.value = [];
    // 上一轮的告警不能带到这一轮：面板里挂着旧告警，用户会以为这次又缺分支。
    agentWarnings.value = [];
    draft.value = null;
    // 新一轮的 op 从第 0 版重新开始，上一轮的账本留着只会把首批 op 判成乱序。
    resetDraftOps();
    // 上一轮长到一半的预览同样不能留：它的节点属于上一次运行，混进这一轮的
    // 面板里，用户会以为新任务已经长出了这些分支。
    clearAgentPreview();
    // 上一轮"改了什么"与上一轮的冲突裁决到此为止：新一轮有自己的基线。
    generatedChanges.value = null;
    generatedSummary.value = null;
    conflictDraft.value = null;
    // 记下这一轮所依据的画布与所在的图：完成时用它算"改了什么"、判版本冲突、判换了图。
    // 意图也要记：落地时得知道这份内容算「新作品」还是「改现有这张图」。
    generationBase = content.value;
    generationBaseRevision = clientRevision.value;
    generationSession = mapSession;
    generationIntent = intent;
    // 选区按意图取：`expand` 只展开**一个**节点（后端开流前会 422 掉多选的请求），
    // `edit` 吃整个选区（用户圈了三个节点说「这几个合并成一个」，模型得知道是
    // 哪三个，否则只能从整棵树里猜）。`create` 没有选区。
    //
    // `options.selection` 是**显式覆盖**：看不见选区的入口（左侧「生成」按钮，
    // 那里没有「已选 N 个」徽标）必须传空数组，否则它会偷偷带着用户在画布上
    // 圈的东西去"重新生成"——用户看不到这件正在发生的事。
    const wantedSelection = intent === 'expand'
      ? (selectedId.value ? [selectedId.value] : [])
      : intent === 'edit' ? (options.selection ?? [...selectedIds.value]) : [];
    // 后端 `AgentRunRequest.selected_ids` 的 `max_length` 是 50：超了会被开流前的
    // 校验直接 422，而用户只是"按着 Ctrl 把一整片都点了一遍"。截断比报错好——
    // 多出来的那几个在提示词里本来就只是上下文，前 50 个足够说明他圈的是哪一片。
    const selectionIds = wantedSelection.slice(0, MAX_REQUEST_SELECTION);
    // diff 的「范围内/外」按**请求时**的选区判，之后用户点来点去不会改写这份记录。
    // 只对 `edit` 记：`expand` 的选区是"要展开的那一个节点"，新节点按构造就落在
    // 它下面，每一行都标「范围内」只是噪音；`create` 没有选区。
    generationSelection.value = intent === 'edit' ? selectionIds : [];    runIntent.value = intent;
    // 展开的预览要有一条祖先链兜着（`node_grow` 的 path 是父节点的索引链）。
    // 生成不用：它的根帧自带 path=null，预览从中心主题开始长。
    if (intent === 'expand' && selectedId.value) {
      seedExpandPreview(selectedId.value);
    }

    const ctrl = new AbortController();
    controller = ctrl;
    agentRunning.value = true;
    busy.value = true;
    stageText.value = '正在连接';
    status.value = '正在生成…';
    waitedMs.value = 0;
    // 上一轮的模型名与思考过程在**这一次开始的那一刻**就作废：它们描述的是另一个
    // 运行。留到这一次的 done 帧才清，中间那段时间面板会挂着一个已经不对的模型名。
    runModel.value = null;
    runServedModel.value = null;
    lastReasoning.value = '';

    // 流只在 done / error 帧上才算有了明确结局；两者都没到就 EOF，说明连接被掐断了，
    // 不能当成正常结束——否则面板会停在「正在与模型协作…」，工具行永远显示「执行中…」。
    let settled = false;
    // `done` 帧带来的来历（`demo` / `model`）。它只在**这次运行**里有意义，
    // 所以是局部变量而不是 store 状态：落地那一步把结果写进 `content.mode`，
    // 之后所有读者看的都是 `mode`（用户作品的来历），不是一个易失的中间值。
    let doneMode: 'demo' | 'model' | null = null;

    try {
      // 每次点击一个新的身份：幂等只在"带同一个 run_id"时生效，而"点了两次生成"
      // 本来就是两次运行（按钮禁用与 busy 闸门管那个）。真正需要同一个 id 的场景
      // 是刷新重发、换标签页、客户端重试——那时服务端能认出这是同一次运行。
      // 唯一例外是"把检查点补完说明"：那条路显式给 `options.runId`，因为服务端
      // 那一行还在（`cancelled` / `failed`），补完的树该覆盖同一行而不是多记一次。
      const runId = options.runId ?? newRunId();
      const body: AgentRunRequest = {
        goal: prompt,
        intent,
        run_id: runId,
        // 始终显式带上：即使宿主页面没给值也发 default，而不是省略字段。
        // 省略会把「前端没传」和「后端默认」两个概念混在一起，排查请求体时看不出
        // 用户到底选了哪套拆解方式；值本身与后端默认一致，传了不影响结果。
        thinking_model: thinkingModel || 'default',
        // create 不能带 snapshot（后端开流前会 422）；另外两种必须带
        // 后端的 `AgentRunRequest.snapshot` 目前仍声明为 `MindMapContent | None`
        // （`extra="forbid"`），鱼骨/流程原样发过去会被 422。这里只发树的形状：
        // 非树内容在 `runGeneration` 开头就被拦下并给出一句人话，不会走到这里。
        snapshot: intent === 'create' ? null : (content.value && isTree(content.value) ? content.value : null),
        // 选中节点是**上下文提示**，不是作用域限制：后端会把它拼进 system
        // （`service.selection_text`），说清"优先改这些节点及其子树，意图明显
        // 不限于它们时也允许改别处，但要在回复里说明"。所以这里是"我圈了谁"，
        // 不是"只许改谁"——「整张图都加上时间维度」这类请求仍然照常发得出去。
        selected_ids: selectionIds,
      };
      // 来源原样送去，一个字都不截：材料是"依据"，goal 是"要做什么"。
      // 把正文拼进 goal 会同时踩两个坑——被截断的材料丢掉大半，且模型分不清
      // 目标与素材。空数组不发：旧后端对多出来的字段是 extra="forbid"。
      if (sources && sources.length > 0) body.sources = sources;
      // 图型只在用户真的选了、且这次是新建时发出去。两条边界各有理由：
      //   - `auto`（= 没选）**省略字段**，不发假值：后端 `None` 的语义就是"让模型
      //     自己判断"，省略与它同一件事，而 `"auto"` 这种字符串会被 Literal 挡成
      //     422。省略还多换来一条性质：没选图型的请求与老前端逐字节相同。
      //   - `expand` / `edit` 不生成骨架，后端的编辑工具集里连生成工具都没有。
      //     那两个意图带图型只会让"图型只在新建时生效"这条边界变成一句空话。
      if (intent === 'create' && diagramType !== 'auto') body.diagram_type = diagramType;
      // 空数组不发：老后端对多出来的字段是 `extra="forbid"`，而"这次一条偏好都没用上"
      // 与"这个字段不存在"对读的人来说是同一件事。
      if (options.prefsApplied?.length) body.prefs_applied = [...options.prefsApplied];
      // 「深度思考」只在**勾上**时才发字段：`false` 与"没有这个字段"对后端是同一件
      // 事，而省略让一次普通生成与加这个开关之前的请求逐字节相同。
      if (options.deepThinking) body.deep_thinking = true;

      await runAgentStream(body, event => {
        // 被取消或被新请求接管的旧流：一帧都不许再改界面状态，
        // 否则旧回调会把新请求刚设好的 stageText / status / 预览覆盖掉。
        if (controller !== ctrl) return;
        // 心跳不进 `agentEvents`：它只驱动状态行的"已等 N 秒"，面板里没有任何地方
        // 读它。而每帧都整数组复制是 O(n²)——长静默会累积几十帧，没必要为纯保活
        // 的帧付这个成本。
        if (event.type === 'heartbeat') {
          waitedMs.value = Math.round(event.waited_ms / 1000);
          return;
        }
        agentEvents.value = [...agentEvents.value, event];
        // 真进度帧到了，静默就结束了：等待计时必须跟着归零，否则「已等 25 秒」
        // 会一直挂在状态行上，看起来像卡住了——而那会儿节点已经在长。
        waitedMs.value = 0;
        switch (event.type) {
          case 'stage':
            stageText.value = event.detail || event.stage;
            break;
          case 'sources':
            // 后端刚把材料写进知识库，临时 id 已被换成持久 id。不换的话这张图里
            // 的 `source_refs.source_id` 重新打开后指不到原文——引用计数、点开看
            // 原文、会话内反向索引全部失效，而且是静默失效。
            sourcesStore.remap(event.mapping);
            break;
          case 'tool_start':
            // 状态行显示的也是人话：用户读不到 `mindmap_generate` 这种函数名。
            stageText.value = agentToolLabel(event.name, 'running');
            break;
          case 'tool_end': {
            stageText.value = event.ok
              ? agentToolLabel(event.name, 'done')
              : `${agentToolName(event.name)} 未成功`;
            // 后端分片生成会以 ok=true 报"结果不完整"：partial 与 diagnostics 都在
            // result_summary 里。不在这里接下来，面板与状态行都看不见它。
            const warnings = agentWarningsFrom(event.result_summary);
            if (warnings.length) {
              recordWarnings(warnings);
              // snapshot 可能已经先到了（与工具帧的先后不固定）。这时状态行还写着
              // "正在收尾"，得把"结果不完整"带上，不能让两句文案在同一个面板上打架。
              if (draft.value && event.ok) status.value = finishStatus('模型给出了结果');
            }
            break;
          }
          case 'node_grow':
            // 只进预览树。真正的结果在 snapshot / done 的 `content` 里：预览没经过
            // 整树校验，某一路失败被裁掉、去重丢掉重复项，都会让它与最终结果不一致。
            growPreviewNode(event.path, event.label, event.expandable);
            break;
          case 'ops':
            // `edit` 意图的改动**只**以 op 批次下发（没有整树快照）。不接这一帧，
            // "打开旧图 → 让 AI 改 → 界面没反应"就是必然结果。
            if (applyOpsToDraft(event.batch)) {
              status.value = `已应用 ${event.batch.ops.length} 条修改，生成完会一起落到画布。`;
            }
            break;
          case 'snapshot':
            // 后端整树校验通过后先推一帧 snapshot：`done` 之前先把结果记下来。
            // 这一帧**不落地**——`done` 通常紧跟，两次落地会留下两步撤销、两条
            // `generate` 事件，而用户只按了一次生成。
            //
            // **也不清预览**：画布此刻还是空的（`content` 为空才看得见预览），
            // 清掉它就会空一帧——用户正盯着一棵长到一半的树，突然变成空态占位，
            // 然后 `done` 又把整棵树塞进来。从前这一帧由候选层接住，候选层删掉
            // 之后，接住它的是"预览再撑到 done"。
            draft.value = event.content;
            status.value = '正在收尾…';
            break;
          case 'done':
            settled = true;
            // 这棵树的来历只有 done 帧说得准，落在这里捕获成 `doneMode`。
            // 后端 `exclude_none=True`，所以正常帧一定带值；缺字段只说明对面是
            // 没填 mode 的旧版本，记作"没说"，不能替它认成模型产出。
            doneMode = event.mode ?? null;
            // 模型名与思考过程：成功时也留（`done` 帧现在两条都给）。
            // `model` 缺字段 = 对面没说（演示模式 / 旧后端 / 测试里的假 provider），
            // 不替它认成 `deepseek-chat`——界面据此只说"模型没说"，不说谎。
            runModel.value = event.model ?? null;
            // 服务端回报的那个名字。缺字段＝服务端没说（演示模式 / 旧后端 / 本地假件），
            // 界面据此写"未知"，不拿请求名冒充答案——那正是这个字段要拆开的原因。
            runServedModel.value = event.served_model ?? null;
            lastReasoning.value = event.reasoning ?? '';
            // 半棵树也是交付：后端在预算耗尽、工具超时、连续失败、空转收尾时不发
            // `error` 了，而是交出手上那棵树，用 `partial` 标明它没长完。**必须收进
            // 告警**——否则用户看到一棵树落到画布上，会当它是模型认为的完整答案。
            //
            // 判断用 `=== true`：缺字段是"没说"（正常收尾、加字段之前的老帧），
            // 不是"完整"；拿 `event.partial` 直接当真会把这批老帧全判成不完整。
            if (event.partial === true) {
              recordWarnings([
                {
                  kind: 'partial',
                  code: 'PARTIAL',
                  // 后端给的原因比这里的兜底文案具体得多（"模型调用次数已用完"
                  // 和"这次运行超过了时间上限"对用户是两件不同的事）。
                  message: event.partial_reason || '有分支没能生成完整，结果可能缺内容。',
                },
              ]);
            }
            // 终态已到，预览再留着只会让人以为还有节点在路上。
            clearAgentPreview();
            if (event.content) draft.value = event.content;
            if (draft.value) {
              // **生成结果直接进画布**：没有确认这一步了。撤销栈、`generate` 事件、
              // 身份重置、冲突判定全在 `commitGenerated` 里，一次做完。
              commitGenerated(draft.value, {
                runId,
                intent,
                mode: doneMode,
                base: generationBase,
              });
            } else {
              // 这一轮有终态帧但没有树：是一类**失败**，只是后端走了 done。
              // 不给分类码的话，面板只会显示一句干巴巴的"没有产出导图"，
              // 而用户此时最想知道的是"再点一次有没有用"。
              agentError.value = '模型这次没有产出导图。';
              agentErrorCode.value = NO_CONTENT;
              agentErrorLimit.value = '';
              status.value = '模型这次没有产出导图。';
            }
            break;
          case 'error':
            settled = true;
            // 失败时预览同样作废：它可能正好停在半截，留着会被当成"这次的结果"。
            clearAgentPreview();
            // 文案与分类分开存：`message` 说"发生了什么"，code/limit 让面板翻出
            // "接下来该怎么办"（见 `mindmap/failures.ts`）。只存 message 的话，
            // 界面除了把后端原话再念一遍什么也做不了。
            agentError.value = event.message;
            agentErrorCode.value = event.code;
            agentErrorLimit.value = event.limit ?? '';
            status.value = event.message;
            break;
        }
      }, ctrl.signal);

      // 已经被后来的请求接管：这个旧请求无论成功、失败还是被取消，都不许再改状态。
      if (controller !== ctrl) return;
      if (!settled && !ctrl.signal.aborted) {
        // 流正常结束但没有终态帧：中途断了，不能假装成功。
        agentError.value = '连接中断，结果可能不完整，请重试。';
        agentErrorCode.value = CONNECTION_LOST;
        agentErrorLimit.value = '';
        status.value = '连接中断，结果可能不完整，请重试。';
      }
    } catch (error) {
      if (controller !== ctrl) return;
      const message = error instanceof ApiError ? error.message : '生成失败，请重试。';
      agentError.value = message;
      // 422 这类"请求本身就不合法"不该显示「重试」——原样再发一遍还是同一个 422。
      // 用它自己的错误码，面板据此给出"改输入"而不是"再来一次"。
      agentErrorCode.value = error instanceof ApiError ? error.code || '' : '';
      agentErrorLimit.value = '';
      status.value = message;
    } finally {
      // 取消后立刻重新运行、或换图后新请求已接管时，旧请求的收尾不能清掉新请求的状态。
      if (controller === ctrl) {
        controller = null;
        agentRunning.value = false;
        busy.value = false;
        stageText.value = '';
        // 断流/异常退出时同理：这一轮已经结束，别再留着"模型正在思考"。
        waitedMs.value = 0;
      }
    }
  }

  /**
   * 失败卡片要说的话：把后端那句"发生了什么"翻成"接下来该怎么办"。
   *
   * 判据在 `mindmap/failures.ts`（纯函数，逐条有用例），这里只负责把手上这三个
   * 分散的字段（`agentError` / `agentErrorCode` / `agentErrorLimit`）与"能不能
   * 重试"拼成它的输入。没有失败时返回 null，面板据此整块不渲染。
   */
  const failureGuide = computed<FailureGuide | null>(() => {
    if (!agentError.value) return null;
    return guideForFailure({
      code: agentErrorCode.value,
      limit: agentErrorLimit.value,
      message: agentError.value,
      // 有 lastAttempt 才谈得上重试：被闸门拦下的请求（没写主题、快照不是树）
      // 从没发出去过，给「重试」只会让用户点一次、什么都不会发生。
      canRetry: lastAttempt.value !== null,
      running: agentRunning.value || busy.value,
    });
  });

  /**
   * 把上一次请求原样再发一遍。
   *
   * **新开一个 run 身份**：重试是一次新的运行，不是"把上一次那个 run 接着跑"。
   * 沿用旧 id 会被后端按幂等判成"同一个 run 已经 done"，直接把旧结果重放回来——
   * 用户点「重试」却看到同一份失败，会以为按钮坏了。
   *
   * 这里必须把 `options.runId` 摘掉：它的语义是"续着那一行跑"（只有「补齐说明」
   * 会传，见 `fillMissingDetails`），而重试恰恰要**换一行**。`lastAttempt` 是浅拷贝
   * 进来的，不摘就会连着传回去，于是重试把上一行重开一遍——`runs.begin()` 顺手
   * 清掉 `checkpointed_at`，补救入口就此消失。
   */
  async function retryLastRun(): Promise<void> {
    const attempt = lastAttempt.value;
    if (!attempt) return;
    if (agentRunning.value || busy.value) return;
    const { runId: _continuation, ...options } = attempt.options;
    await runGeneration(
      attempt.goal,
      attempt.intent,
      attempt.sources,
      attempt.thinkingModel,
      attempt.diagramType,
      options,
    );
  }

  /** 这一轮是不是"展开某个选中节点"。按钮文字与互斥判定都读它。 */
  const expanding = computed(() => agentRunning.value && runIntent.value === 'expand');

  /**
   * 这一轮是不是"在生成"（新建 / 编辑也算），用来与展开互斥。
   *
   * 展开与生成互斥不是保守，是必要的：`runGeneration` 一进来就 `clearAgentPreview`
   * 并换掉 `controller`，两轮流重叠时后到的那个会把前一个刚长出来的预览一起
   * 作废——用户看到的是"点了一下，之前的进度消失了"。
   */
  const generating = computed(() => agentRunning.value && runIntent.value !== 'expand');

  /**
   * 展开当前选中的节点：走同一个 `/agent/runs/stream`，只是意图换成 `expand`。
   *
   * 与生成的差别只有三处，其余全部复用（op 帧、版本检查、落地与冲突裁决都还是
   * 那一条路）：
   *
   *  1. `goal` 写的是"展开哪个节点"，而不是用户的主题；
   *  2. `intent="expand"` + `selected_ids=[node_id]`（`runGeneration` 里按意图补的）；
   *  3. `snapshot` 是**当前整棵树**——后端要从它上面挂子节点，而它也可能有一堆
   *     没保存的编辑，重读数据库会把那些编辑丢掉。
   *
   * `sources` 允许为空：用户重新打开一张旧导图时前端手里没有正文，后端会按节点上
   * 的 `source_refs` 从知识库里把材料找回来（`tools._expand_sources`）。这里不因为
   * "没带材料"就拦下按钮——那正是这条链要打通的那个场景。
   *
   * `expandable === 'no'` 时不发请求：后端也会判（`too_deep` / 模型说没有更多），
   * 但让用户先点一次再看一句"到底了"是白花一次调用。
   */
  async function expandSelected(sources?: Source[], count = 3): Promise<void> {
    if (busy.value || agentRunning.value) return;
    const node = selectedNode.value?.node;
    if (!node) {
      status.value = '先在画布上选中一个节点，再展开它。';
      return;
    }
    if (node.expandable === 'no') {
      status.value = `「${node.label.trim()}」已经到底了，没有更细的内容可展开。`;
      return;
    }
    const label = node.label.trim();
    // `goal` 是外层模型看到的那一句话（`_expand` 自己也拿得到选中节点与路径，
    // 所以这里不需要把路径拼进去——拼了就有两处口径）。
    const goal = `展开节点「${label}」，补充 ${count} 个子节点。`;
    await runGeneration(goal, 'expand', sources);
  }

  /**
   * 一次生成本次落地的元数据。`commitGenerated` 与冲突裁决共用它。
   */
  interface GenerateMeta {
    runId: string;
    intent: 'create' | 'expand' | 'edit';
    /** `done` 帧的来历；`null` = 对面没填，落地时退回 `model`。 */
    mode: 'demo' | 'model' | null;
    /** 生成开始那一刻的画布，用来算"这次改了什么"、并进改动事件。 */
    base: DiagramContent | null;
  }

  /**
   * **生成结果落到画布**。这是整条 agent 路径的终点，也是删掉候选层之后唯一的入口。
   *
   * 它做六件事，一次做完：
   *
   *  1. 整树校验——模型产出是外部数据，长度/引用/节点数都可能越界。拦在这里，
   *     脏数据就不会先进画布、直到保存时才被后端 422 拒绝（那时用户已经改了半天）；
   *  2. 判冲突——生成期间画布变过就**不覆盖**，把两棵树留着让用户选（`resolveConflict`）；
   *  3. 压撤销栈——生成前那棵树进 `past`，于是"生成差了"可以一步退回去；
   *  4. 记一条 `kind='generate'`（带 `node_count`，**在生成完成这一刻定死**）；
   *  5. 把身份按意图处理——`create` 是一份新作品，不能继承上一张图的 `mapId`；
   *  6. 算一次"这次改了什么"，冻结成 `generatedChanges`。
   *
   * 返回分类而不是布尔：调用方（面板/测试）要能区分"落地了""等用户裁决""数据不合法"。
   */
  function commitGenerated(next: DiagramContent, meta: GenerateMeta, force = false): 'applied' | 'conflict' | 'invalid' | 'stale' {
    // 换了图：`clientRevision` 判不出这件事（切图不递增），只有会话号能。
    // 装上去等于把上一张图的内容写进当前记录，还顶着它的 mapId。
    if (generationSession !== mapSession) {
      draft.value = null;
      resetDraftOps();
      conflictDraft.value = null;
      status.value = '这次生成是在另一张导图上跑的，结果已丢弃，请重新生成。';
      return 'stale';
    }
    // 生成期间画布被改过（`undo` 或手工编辑）。**不覆盖**：用户那 25 秒里的编辑
    // 不能被一次生成静默抹掉——两棵树都留着，让用户选（`resolveConflict`）。
    if (!force && clientRevision.value !== generationBaseRevision) {
      conflictDraft.value = { next, meta };
      status.value = '生成期间你改过这张图。要保留哪个？';
      return 'conflict';
    }
    if (editingId.value !== null || titleDraft.value !== null) {
      status.value = '请先提交或取消正在编辑的内容，再让模型改这张图。';
      // 手打了一半的标题不能丢：同样走裁决，用户还能选"保留我的"。
      conflictDraft.value = { next, meta };
      return 'conflict';
    }
    // 装进画布之前整树校验一次（理由见函数头第 1 条）。
    const invalid = validateDraft(next);
    if (invalid) {
      draft.value = null;
      resetDraftOps();
      conflictDraft.value = null;
      status.value = `生成结果不合法，没有落进画布：${invalid}`;
      return 'invalid';
    }

    // 身份：create 是一份新作品，绝不能继承上一张图的身份——否则落地之后点保存会走
    // PUT，把刚打开的那条旧记录整条覆盖掉。
    if (meta.intent === 'create') {
      mapId.value = null;
      version.value = 0;
      savedAt.value = null;
      // 上一张图在途的保存响应回来时不能再把它的 id 写回 mapId，所以会话跟着换；
      // saving 也自己复位——那一轮请求的 finally 会因为会话不匹配跳过清理。
      mapSession += 1;
      saving.value = false;
      resetHistory();
      // 新作品不接上一张图的历史：撤销栈里只留「生成之前」这一步（画布本来是空的
      // 就压 `'null'`，照样退得回去）。
      pushHistoryOrEmpty();
    } else {
      // 改的是现有这张图：身份、版本、历史都沿用，撤销回到生成前的那棵树。
      //
      // `actor` 必须是 `agent`：这条路上用户没动手，改动是模型算出来的。
      // `runId` 带上——统计里"这次生成被改了几处"只能靠它把改动事件与
      // `generate` 那条对上，而它过了 `reportRunSaved()` 就没了。
      pushHistory('agent', meta.runId);
    }
    content.value = slot(next);
    // 改动与"这次生成"共享同一个 `at`：它是"一次操作产生了几条事件"的唯一分组
    // 依据（`GROUP BY at`），两次取时刻会让同一次生成被读成两次操作。
    const at = new Date().toISOString();
    // 改动已经落地，这时候才比得出差：`pushHistory()` 抄下的那棵树 vs `next`。
    collectEdits(next, at);
    // 生成这条记录紧跟在它带来的那批改动之后：读起来像"改了这些…这是这次生成的规模"。
    recordGeneration(next, meta.runId, at);

    // 「这次改了什么」——**落地瞬间冻结**。基线是生成开始那一刻的画布，不是此刻的
    // （此刻就是 next，比出来永远是空）。
    if (meta.base) {
      // 两批并列子树（鱼骨的骨、流程的阶段）都要走同一套差：把 `rootsOf` 交进去，
      // 树的 `[content.root]` 与它逐格等价，所以树的这份清单一个字都不会变。
      const baseRoots = rootsOf(meta.base);
      const nextRoots = rootsOf(next);
      const nodeChanges = diffCandidate(baseRoots, nextRoots) ?? [];
      const names = new Map<string, string>();
      for (const root of nextRoots) {
        for (const entry of flatten(root)) {
          names.set(entry.node.id, displayTitle(entry.node));
        }
      }
      const nameOf = (id: string) => names.get(id) ?? id;
      const changes: CandidateChange[] = [
        ...nodeChanges,
        ...diffEdges(edgesOf(meta.base), edgesOf(next), nameOf),
      ];
      const scope = selectionScope(baseRoots, nextRoots, generationSelection.value);
      generatedChanges.value = changes.map(item => ({ ...item, inSelection: withinScope(item, scope) }));
      generatedSummary.value = { nodes: countNodesIn(next), changes: changes.length };
    } else {
      // 画布本来是空的：整棵树都是新的，那不是 diff，是把整棵树重念一遍。
      generatedChanges.value = null;
      generatedSummary.value = { nodes: countNodesIn(next), changes: null };
    }
    generatedContent = next;

    draft.value = null;
    // 结果已经进了 content，下一轮从第 0 版重来。
    resetDraftOps();
    conflictDraft.value = null;
    // 来源标记跟着 done 帧走，不再一律写成「模型」：演示模式产出的是一棵占位树，
    // 给它贴"模型生成"的标签就是让界面替它撒谎。没说（旧后端）才退回 model。
    mode.value = meta.mode ?? 'model';
    dirty.value = true;
    clientRevision.value += 1;
    editingId.value = null;
    // 告警**不清**：它说的是"这棵树缺了什么"，而树已经在画布上了——用户正需要知道
    // 这件事。清掉就只剩一句"已落到画布"，而某几支是空的。
    ensureSelection();
    status.value = finishStatus(mode.value === 'demo'
      ? '已落到画布（演示导图，固定结构，不是 AI 生成）'
      : '模型的结果已落到画布');
    // 等保存成功再上报 `applied_at`：它的语义是"这份结果被存下来了"，正是
    // "下次进页面还要不要提醒"的判据（见 `runs.pending`）。
    pendingSaveRunId.value = meta.runId;
    return 'applied';
  }

  /**
   * 生成那条事件：`kind='generate'` + `node_count` + `run_id`。
   *
   * **`node_count` 在这里定死**，一小时后的保存不再重算：用户改完之后树上的节点数
   * 已经变了，那时数出来的是"改后的规模"，而分母要的是"模型交给你多少东西"。
   *
   * `at` 由调用方给（与同一批改动共享同一个值）：`GROUP BY at` 是"这次操作产生了
   * 哪几条事件"的唯一分组依据，两次 `new Date()` 会在慢机器上差出几毫秒，把一次
   * 生成拆成两次操作。
   */
  function recordGeneration(after: DiagramContent, runId: string, at: string) {
    pendingEdits.value = [
      ...pendingEdits.value,
      generateEvent(countNodesIn(after), { at, runId }),
    ];
  }

  /**
   * 冲突裁决：生成期间画布被改过，用户挑一棵。
   *
   * `generated` = 用模型的结果（他中途的编辑会被这次生成覆盖，但**仍可撤销**：
   * 落地前压的那一步是"生成开始时的画布"，撤销能退回生成前，再撤销一步才到他的
   * 中途编辑）；`mine` = 保留画布，生成结果丢掉。
   *
   * 这是删掉候选层之后唯一留下的"两棵树"界面，而它的性质与候选不同：候选挡在
   * **每一次**生成前面，它只在数据要丢的时候出现。
   */
  function resolveConflict(choice: 'generated' | 'mine'): 'applied' | 'kept' | 'invalid' {
    const pending = conflictDraft.value;
    if (!pending) return 'kept';
    if (choice === 'mine') {
      conflictDraft.value = null;
      draft.value = null;
      resetDraftOps();
      status.value = '已保留你的改动，这次生成的结果没有落到画布。';
      return 'kept';
    }
    conflictDraft.value = null;
    // `force`：用户已经明确选了"用生成的"，那条 revision 判据不该再拦一次
    // （它刚刚正是拦下这次落地的原因）。
    const result = commitGenerated(pending.next, pending.meta, true);
    return result === 'applied' ? 'applied' : 'invalid';
  }

  /**
   * 「回到生成之前」：一步退回上一次生成开始时的画布。
   *
   * 只在**画布还是生成结果本身**时可用（引用相等）。生成之后又改过就不提供了——
   * 那时撤销一步只是退掉他自己的一次编辑，把它说成"回到生成前"是骗人；界面会
   * 让他用普通撤销逐步退回。
   */
  function undoGenerate(): boolean {
    if (content.value === null || content.value !== generatedContent) return false;
    if (!canUndo.value) return false;
    undo();
    generatedChanges.value = null;
    generatedSummary.value = null;
    generatedContent = null;
    status.value = '已回到生成之前的画布。';
    return true;
  }

  /**
   * 「回到生成之前」现在可用吗。
   *
   * 判据只有一条：**画布上还是生成结果本身**（引用相等）。用户生成之后又改过，
   * 这个快捷动作就该消失——那时"撤销一步"退掉的是他自己那次编辑，把它标成
   * "回到生成前"是骗人。面板据此决定显示不显示那个按钮。
   */
  const canUndoGenerate = computed(() => content.value !== null && content.value === generatedContent);

  /**
   * 画布上**能选中**的那些节点：扁平表去掉合成根（鱼头 / 流程起点）。
   *
   * 界面上有两处按节点列表工作（附加关系的下拉框、边端点的显示名），它们要的是
   * "content 里真实存在的节点"。合成根不在 content 里，选中它 / 给它连线都会得到
   * 一个 `findIn()` 找不到的 id，所以这里先摘掉；树型没有合成根，这张表与
   * `flatNodes` 逐项相同。
   */
  const selectableNodes = computed<FlatNode[]>(() => {
    const current = content.value;
    // 没有 content 时画布上是预览：那棵树本来就全是节点（画布那边已设 `selectable: false`），
    // 这里原样返回，界面不必再分一次叉。
    if (!current) return flatNodes.value;
    // 先建 id 集合再过滤（O(n)）：逐个 `findIn` 是 O(n²)，1000 节点的图每次重排
    // 都要走一遍百万级的遍历。
    const known = new Set<string>();
    for (const node of iterNodesIn(current)) known.add(node.id);
    return flatNodes.value.filter(entry => known.has(entry.node.id));
  });

  /**
   * 这个节点是不是**某块区域的根**（中心主题 / 一根骨 / 一个阶段）。
   *
   * 界面上唯一需要它的地方是"删除"按钮的禁用态：删一根骨就不是鱼骨图了，而那件事
   * 会在点的瞬间被 `deleteNode` 拒掉——按钮灰着比点一次再看一句提示便宜。
   */
  function isContainerRootId(id: string): boolean {
    return content.value ? isContainerRoot(content.value, id) : false;
  }

  return {
    content,
    mode,
    status,
    stageText,
    waitedMs,
    busy,
    selectedId,
    // 多选：主选区（`selectedId`）+ 追加选区 → `selectedIds`。界面上的「已选 N 个」
    // 与请求里的 `selected_ids` 都读后者。
    selectedIds,
    toggleSelect,
    clearSelection,
    direction,
    viewAdjusted,
    // 视图层收起：这一屏最多画几个节点、哪些被点开/收起过、这一屏少画了多少个节点。
    // 全都不进 content（见 `docs/VIEW-COLLAPSE.md` §三）。
    visibleNodeLimit,
    viewExpandedNodes,
    viewCollapsedNodes,
    viewExpandAll,
    hiddenNodeCount,
    canvasRoot,
    expandNodeInView,
    collapseNodeInView,
    toggleFoldInView,
    expandBranchInView,
    expandAllInView,
    collapseViewToCap,
    revealInView,
    editingId,
    titleDraft,
    setTitleDraft,
    mapId,
    version,
    savedAt,
    dirty,
    layout,
    flatNodes,
    // 可选中/可连线的节点（去掉合成根）：界面按节点列表工作的地方读它
    selectableNodes,
    isContainerRootId,
    nodes,
    edges,
    nodeCount,
    // 附加关系：当前树上的全部边 + 与"选中节点"相关的那几条（侧栏用）。
    // 两层分开返回：侧栏分组显示、删除按 id 判层级，都靠"谁属于哪一层"这件事。
    contentEdges,
    contentInnerEdges,
    selectedEdges,
    selectedInnerEdges,
    // 边端点 id → 显示名：流程的边连的是阶段 id，只查节点会显示成一串 `s_xxx`
    edgeNameOf,
    connectNodes,
    deleteEdge,
    // 跨节点关系线：**画布上画出来**（`edges` 里的 `relation:` 通道）与**详情里能看见**
    // （`selectedRelations`）是同一个能力的两半——只画不列，用户看到一根虚线却找不到
    // 它是从哪来的；只列不画，那就还是改动之前那副"界面不渲染 relations"的样子。
    contentRelations,
    selectedRelations,
    deleteRelation,
    selectedNode,
    selectedRefs,
    refIndex,
    // 这次生成改了什么：落地瞬间冻结的一份静态快照（`create` 意图无基线时为 null）
    generatedChanges,
    // 这次落地的规模与改动条数：面板用它说"已落到画布（42 个节点，改了 6 处）"
    generatedSummary,
    // 这一轮请求带上去的选区：diff 的「范围内/外」按它判（不是按此刻的选区）
    generationSelection,
    // 版本冲突时保留的第二棵树：非空 = 正在等用户裁决
    conflictDraft,
    resolveConflict,
    undoGenerate,
    canUndoGenerate,
    changedNodeIds,
    focusTick,
    breadcrumb,
    canUndo,
    canRedo,
    undoDepth,
    undoBytes,
    // 还没提交的改动：保存时随内容一起落库，保存成功才清（见 `mindmap/edit-log.ts`）
    pendingEdits,
    saving,
    conflict,
    maps,
    listOpen,
    listBusy,
    cancel,
    preview,
    importMap,
    loadPreset,
    select,
    locate,
    setDirection,
    beginEdit,
    endEdit,
    commitLabel,
    commitNote,
    setNodeVisualById,
    renameTitle,
    addChildTo,
    addSiblingTo,
    commitAndAddChild,
    deleteNode,
    // 拖动改结构：`previewMove` 是拖动中的预演（不写内容），`moveNodeTo` 是松手
    // 那一下（一次可撤销的内容改动）。两者共用 `planDrop`，所以"看见的落点"与
    // "真正落下的位置"在结构上是同一份推导。
    previewMove,
    moveNodeTo,
    undo,
    redo,
    loadMap,
    newMap,
    markSaved,
    save,
    reloadFromServer,
    saveAsNew,
    dismissConflict,
    openList,
    // 只拉列表不弹模态（`☰` 抽屉的历史区用它）
    loadMaps,
    closeList,
    openMap,
    // agent 路径
    clientRevision,
    agentPreview,
    agentPreviewScope,
    agentPreviewDropped,
    agentEvents,
    agentWarnings,
    agentRunning,
    agentError,
    // 失败卡片：分类字段 + 翻译好的"下一步"，以及「重试」（原样重发上一次请求）
    agentErrorCode,
    agentErrorLimit,
    failureGuide,
    lastAttempt,
    retryLastRun,
    runIntent,
    // 这一次真正发出去的模型名 + 模型的思考过程（`done` 帧的两条）
    runModel,
    // 服务端回报的模型名（同一条帧的另一个字段）：与请求名分开，才能看出路由。
    runServedModel,
    lastReasoning,
    expanding,
    generating,
    runGeneration,
    expandSelected,
    // 上次运行留下的结果（run 身份）
    resumableRun,
    probeResumableRun,
    resumeRun,
    fillMissingDetails,
    ignoreResumable,
    // 刚落进画布、还没保存的那个 run：界面据此把"上次没保存"与"刚刚这一次"分开
    pendingRunId: pendingSaveRunId,
  };
});
