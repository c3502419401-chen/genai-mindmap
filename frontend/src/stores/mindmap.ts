import type { Edge as FlowEdge, Node as FlowNode } from '@vue-flow/core';
import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

import { ApiError, createMindMap, getMindMap, listMindMaps, runAgentStream, updateMindMap } from '../api/client';
import type { AgentEvent, AgentRunRequest, MapRecord, MindMapContent, MindNode, OpBatch, Source, SourceRef } from '../api/types';
import { directionOf, withDirection } from '../mindmap/direction';
import { applyOps } from '../mindmap/ops';
import { buildRefIndex } from '../mindmap/refs';
import { sourceInputError } from '../sources/input';
import { layoutTidy, childSide, NODE_SIZE, type ChildSide, type Direction, type LayoutResult } from '../canvas/layout/tidy';
import {
  addChild,
  addSibling,
  charLength,
  countNodes,
  findNode,
  findParent,
  LIMITS,
  removeNode,
  renameNode,
  setNodeNote,
  validateDraft,
} from '../mindmap/edits';

/** 画布节点上最多显示的字数（按码点算，避免把 emoji 切成半个）。 */
export const TRUNCATE_CHARS = 24;

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

/** 正文当前来自哪一段。详情面板据此给出"这一段是什么"的说明。 */
export type BodySource = 'detail' | 'note' | 'label';

export function bodySource(node: MindNode): BodySource {
  if (node.detail?.trim()) return 'detail';
  if (node.note?.trim()) return 'note';
  return 'label';
}

/**
 * agent 工具名 → 用户读得懂的说法。
 *
 * 映射只此一份：面板（要带耗时）与状态行（不带）都从这里取。两处各写一份的话，
 * 同一件事迟早出现两种措辞，用户在两个位置读到不同的词。
 */
const AGENT_TOOL_LABELS: Record<string, { running: string; done: string }> = {
  mindmap_generate: { running: '正在定框架', done: '已定好框架' },
  node_expand: { running: '正在展开分支', done: '已展开分支' },
  rag_query: { running: '正在查资料', done: '已查好资料' },
  mindmap_edit: { running: '正在改结构', done: '已改好结构' },
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
      message: '有分支没能生成完整，候选可能缺内容。',
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
   * 这一帧画的是还没进画布的树：`preview` 是生成中，`candidate` 是已就绪但未应用。
   * 缺省表示它就在 `content` 里，也就是用户的作品。画布据此弱化样式。
   */
  draft?: 'preview' | 'candidate';
  /** 预览里模型明确标了「不用再展开」的节点，再弱一档；`null` 与 `true` 都不额外标记。 */
  previewLeaf?: boolean;
}

export interface FlatNode {
  node: MindNode;
  depth: number;
  parentId: string | null;
}

/**
 * 画布节点。Vue Flow 的 `Node` 输入类型里没有 `selected`（它把选中当内部状态），
 * 但 `:nodes` 变化时 applyChanges 会把 `selected` 同步进去，所以这里补上这个字段的类型。
 */
export type MindFlowNode = FlowNode<MindNodeData> & { selected: boolean };

/**
 * 画布当前渲染的是哪棵树。
 *
 * 三级优先级：已应用的 `content` → 已就绪但未应用的 `candidate` → 生长中的
 * `agentPreview`。前两级之外的一切（保存、导出、撤销、编辑、采纳）仍然只认
 * `content`——渲染源只决定"画什么"，不决定"什么是用户的作品"。
 */
export type RenderKind = 'content' | 'candidate' | 'preview';

interface RenderSource {
  root: MindNode;
  kind: RenderKind;
  /** 预览节点的可展开性（派生 id → expandable）；非预览时是空表。 */
  expandable: Map<string, boolean | null>;
}

/** 共用的空表：非预览来源不需要这份映射，别每次重算都新建一个。 */
const NO_EXPANDABLE = new Map<string, boolean | null>();

function flatten(root: MindNode): FlatNode[] {
  const out: FlatNode[] = [];
  const walk = (node: MindNode, depth: number, parentId: string | null) => {
    out.push({ node, depth, parentId });
    for (const child of node.children) walk(child, depth + 1, node.id);
  };
  walk(root, 0, null);
  return out;
}

export const useMindMapStore = defineStore('mindmap', () => {
  const content = ref<MindMapContent | null>(null);
  const mode = ref<'demo' | 'model' | 'saved' | null>(null);
  const status = ref('输入主题或文本后生成，节点会边生成边出现。');
  const stageText = ref('');
  const waitedMs = ref(0);
  const busy = ref(false);
  const selectedId = ref<string | null>(null);
  /**
   * 朝向是 content 的派生值（layout + theme.direction），不单独存一份：
   * 否则保存后再打开，画布上的朝向与库里的记录会各说各话。
   */
  const direction = computed<Direction>(() => directionOf(content.value));
  /** 用户手动平移/缩放后不再自动 fitView，否则每次节点增长都会把视图拽回去。 */
  const viewAdjusted = ref(false);

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

  const saving = ref(false);

  /**
   * 每次已提交的编辑都递增。agent 的接受动作靠它判断"候选是不是基于一棵已经
   * 被改过的树算出来的"——用 revision 比比较整树引用更便宜，也比比 dirty 更准
   * （dirty 只说明"有改动"，不说明"改动发生在候选生成之后"）。
   */
  const clientRevision = ref(0);

  /** agent 算出来的候选。它**不是** content：用户确认之前不写进画布。 */
  const candidate = ref<MindMapContent | null>(null);
  /** 候选产出时的 clientRevision，接受时用来判过时。 */
  let candidateBaseRevision = 0;

  /**
   * 候选产出时所在的导图会话号（见 `mapSession`）。
   *
   * `clientRevision` 只在编辑时递增，切图（loadMap / newMap / preview）不碰它。
   * 只看 revision 的话："生成候选 A → 打开另一张图 B → 点接受"会把 A 的内容
   * 装进 B，还沿用 B 的 mapId 与 version，下次保存就把 A 写进 B 的记录。
   */
  let candidateSession = 0;

  /**
   * 候选是在哪种意图下产出的。
   *
   * 接受时要按它区分两件事：`create` 是一份新作品，不能继承上一张图的
   * `mapId` / `version` / 历史；`expand` / `edit` 改的就是当前这张图，身份必须留着。
   */
  let candidateIntent: 'create' | 'expand' | 'edit' = 'create';

  /**
   * 候选树当前走到第几版 op。
   *
   * 每批 op 的 `base_revision` 是后端在**它那棵树**上算的序号（0 起，一批加一），
   * 这里记的是"我这份候选已经应用到这里"。两者对不上就说明中间漏了一批
   * （丢帧、乱序），在错位的树上继续套用比不套用更糟。
   *
   * 只放在 store 内部，不进返回对象：界面不需要读它，它只是消费端的一致性账本。
   */
  let candidateRevision = 0;

  /**
   * 候选整棵作废时，把 op 版本账本一起归零。
   *
   * 零是后端每一轮的新起点（`RunContext.revision` 从 0 起算）。漏掉这一步，
   * 下一轮的第一批 op 会因为 `base_revision=0` 对不上旧账本而被整批丢掉，
   * 表现出来就是"改了半天候选一点没动"。
   */
  function resetCandidateOps() {
    candidateRevision = 0;
  }

  /** 给面板显示的工具进度与阶段。 */
  const agentEvents = ref<AgentEvent[]>([]);
  const agentRunning = ref(false);
  const agentError = ref('');

  /**
   * 这一轮 agent 产出的树是演示还是模型给的，取自 `done` 帧的 `mode`。
   *
   * `null` 表示"没说"（旧后端省略了这个字段），接受时退回 `'model'`——那是
   * 这条通道一直以来的行为，不能因为多了个字段就让老后端的结果显示成未知。
   * 而一旦帧里明确写了 `demo`，界面就必须说明白：占位树不能顶着"模型生成"。
   */
  const agentMode = ref<'demo' | 'model' | null>(null);

  /**
   * 本轮运行里"结果不完整"的证据：部分完成标记 + 每条诊断。
   *
   * 面板在候选上方按琥珀色列出，`status` 也会因此改成「部分分支未完成」。
   * 只认 `ok` 的时候，这三条信息一条都没地方落。
   */
  const agentWarnings = ref<AgentWarning[]>([]);

  /**
   * 生成过程中正在长出来的预览树。
   *
   * 与 `candidate` 分开存，不共用一份状态：预览没有经过整树校验，也不保证和
   * 最终候选一致（某一路失败被裁掉、去重丢掉重复项，都会让预览多出几个节点）。
   * 一旦让它顶替 `candidate`，用户点「应用到画布」就会把半成品装进画布。
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
   * 预览整棵作废。
   *
   * 新一轮开始、收到 snapshot / done / error、换图，都要走这里：上一轮的残留
   * 一旦留到下一轮，用户看到的就是两棵树拼在一起的幻影。
   */
  function clearAgentPreview() {
    agentPreview.value = null;
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
   * 「候选已就绪」这句的完整文案。
   *
   * 结果不完整时必须和全部成功区分开：只写「候选已就绪」，用户点接受之后
   * 才发现某几支是空的。partial 与"只有诊断"分开说——「分支未完成」这种话
   * 放在一棵只是有重复节点待合并的树上并不成立。
   */
  function candidateStatus(base: string): string {
    if (agentWarnings.value.some(item => item.kind === 'partial')) {
      return `${base}（部分分支未完成），确认后才会应用到画布。`;
    }
    if (agentWarnings.value.length) return `${base}（有诊断提示），确认后才会应用到画布。`;
    return `${base}，确认后才会应用到画布。`;
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
   * 三级优先级各有理由：
   * 1. `content` 最优先。用户已经打开/生成了一张图时，让还没定稿的东西顶掉它，
   *    看起来就是"我的图被改了"——而且失败或取消之后才发现内容消失过。
   * 2. `candidate` 其次。`snapshot` 到达时预览会被整棵作废（那是必须的：不能再
   *    挂着一棵没校验的树），如果这一级不存在，画布会在"预览→候选"之间空一帧，
   *    退回到空态占位，用户还得点「应用」才看得见图。
   * 3. `agentPreview` 垫底。节点一个一个到达，画布跟着长。
   *
   * 注意候选只是"看得见"：`acceptCandidate()` 的校验一步都没少，候选照样要用户
   * 点「应用」才会写进 `content`。渲染与采纳是两件事。
   */
  const renderSource = computed<RenderSource | null>(() => {
    if (content.value) {
      return { root: content.value.root, kind: 'content', expandable: NO_EXPANDABLE };
    }
    if (candidate.value) {
      return { root: candidate.value.root, kind: 'candidate', expandable: NO_EXPANDABLE };
    }
    if (agentPreview.value) {
      const expandable = new Map<string, boolean | null>();
      const root = previewSubtree(agentPreview.value, [], expandable);
      return { root, kind: 'preview', expandable };
    }
    return null;
  });

  const layout = computed<LayoutResult | null>(() => {
    const source = renderSource.value;
    return source ? layoutTidy(source.root, { direction: direction.value }) : null;
  });

  const flatNodes = computed<FlatNode[]>(() => {
    const source = renderSource.value;
    return source ? flatten(source.root) : [];
  });

  const nodes = computed<MindFlowNode[]>(() => {
    const current = layout.value;
    const source = renderSource.value;
    if (!current || !source) return [];
    // 只有已应用的树才可编辑：候选与预览都能画出来，但点不动、改不了。
    const editable = source.kind === 'content';

    const parentOf = new Map<string, string | null>();
    for (const { node, parentId } of flatNodes.value) parentOf.set(node.id, parentId);

    return flatNodes.value.map(({ node, depth }) => {
      const point = current.positions.get(node.id) ?? { x: 0, y: 0 };

      // 子节点在哪几侧，就在哪几侧留出口（双向布局里根的两边都有）。
      const outSides: ChildSide[] = [];
      for (const child of node.children) {
        const childPoint = current.positions.get(child.id);
        if (!childPoint) continue;
        const side = childSide(point, childPoint, direction.value);
        if (!outSides.includes(side)) outSides.push(side);
      }

      // 入口在父节点到自己的连线的对面：父在左边就从左边进来。
      const inSides: ChildSide[] = [];
      const parentPoint = current.positions.get(parentOf.get(node.id) ?? '');
      if (parentPoint) {
        const side = childSide(parentPoint, point, direction.value);
        inSides.push(side === 'left' ? 'right' : side === 'right' ? 'left' : 'top');
      }

      return {
        id: node.id,
        type: 'mind',
        draggable: false,
        connectable: false,
        // 预览与候选都不可选：选中一个只在"提案"里存在的 id，会让详情面板对着
        // 空内容说话，expand 请求也会把后端不认识的 id 发回去。
        selectable: editable,
        // 选中态由 store 说了算。只在点击时靠 Vue Flow 自己切换的话，代码里改选中
        // （加节点、删节点后跳到父节点）画布上看不出来，用户会以为还停在原来那个节点上。
        // 不可编辑时一律不显示选中：残留的 selectedId 可能正好撞上候选里的同名 id。
        selected: editable && selectedId.value === node.id,
        position: {
          x: point.x - NODE_SIZE.width / 2,
          y: point.y - NODE_SIZE.height / 2,
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

      const side = childSide(parentPoint, childPoint, direction.value);
      out.push({
        id: `${parentId}->${node.id}`,
        source: parentId,
        sourceHandle: `out-${side}`,
        target: node.id,
        targetHandle: `in-${side === 'left' ? 'right' : side === 'right' ? 'left' : 'top'}`,
        type: 'smoothstep',
      });
    }
    return out;
  });

  const nodeCount = computed(() => flatNodes.value.length);

  /**
   * 详情面板要显示的节点。
   *
   * 必须落在 `content` 里：画布上可能正画着候选或预览（`flatNodes` 跟着渲染源走），
   * 而面板上的「改文字 / 加子节点 / 删除」全都写 `content`。没有已应用的树就没有
   * 可编辑的节点，否则面板会摆出一排点了没反应的按钮。
   */
  const selectedNode = computed<FlatNode | null>(() => {
    if (!content.value) return null;
    return flatNodes.value.find(entry => entry.node.id === selectedId.value) ?? null;
  });

  /** 选中节点的引用。空节点也有自己的空数组，详情面板不必再判空。 */
  const selectedRefs = computed<SourceRef[]>(() => selectedNode.value?.node.source_refs ?? []);

  /**
   * 来源的反向索引。
   *
   * 只从 `content` 算：画布上可能正画着候选或预览节点，把它们算进引用统计会让
   * 左侧说"被 5 个节点引用"、而其中 3 个根本不在导图里。
   */
  const refIndex = computed(() => buildRefIndex(content.value?.root ?? null));

  /**
   * "把某个节点挪到视野中心"的触发计数。
   *
   * 用计数器而不是监听 `selectedId`：在画布上手动点节点时也会改选中，若监听选中，
   * 视图会把人刚拖到的位置又拽回中心。只有从引用列表主动跳转时才加一。
   */
  const focusTick = ref(0);

  /** 从根到当前节点的路径，详情面板用来标明"这句话出自哪一支"。 */
  const breadcrumb = computed<MindNode[]>(() => {
    const target = selectedId.value;
    if (!content.value || !target) return [];
    const path: MindNode[] = [];
    const walk = (node: MindNode): boolean => {
      path.push(node);
      if (node.id === target) return true;
      for (const child of node.children) {
        if (walk(child)) return true;
      }
      path.pop();
      return false;
    };
    return walk(content.value.root) ? path : [];
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

  /** 改动前调用：记一步历史，并让重做栈失效（树从这里长出新的分支了）。 */
  function pushHistory() {
    const snap = snapshot();
    if (!snap) return;
    recordPast(snap);
    future.value = [];
  }

  /** 还能撤销几步、占了多少字节：界面与测试都靠它确认字节上限真的生效。 */
  const undoDepth = computed(() => past.value.length);
  const undoBytes = computed(() => pastBytes.value);

  function resetHistory() {
    past.value = [];
    future.value = [];
    pastBytes.value = 0;
  }

  /**
   * create 意图接受候选后的那一步撤销。
   *
   * 与 `pushHistory()` 只差一处：画布本来是空白时也要留下一步（`'null'` 就是
   * JSON 里的空画布），否则用户点了「应用到画布」就再也退不回接受之前的样子。
   */
  function pushHistoryOrEmpty() {
    recordPast(snapshot() ?? 'null');
    // 树从这里长出新的分支了，重做栈必须失效。
    future.value = [];
  }

  /** 选中的节点可能已被删掉：兜底清空，免得详情面板指向不存在的节点。 */
  function ensureSelection() {
    const target = selectedId.value;
    if (target && content.value && !findNode(content.value.root, target)) {
      selectedId.value = null;
    }
  }

  /**
   * 把一步历史快照装回画布。
   *
   * 快照可能是字符串 `'null'`——接受候选之前画布是空的时候，`pushHistoryOrEmpty()`
   * 压的就是它。`JSON.parse('null')` 得到 null，正好是 `MindMapContent | null`
   * 的另一半；但选中与编辑态还指着已经不存在的旧节点，必须一起清掉，
   * 否则详情面板和编辑框会继续对着空白画布说话。
   */
  function restoreSnapshot(snap: string) {
    const parsed = JSON.parse(snap) as MindMapContent | null;
    content.value = parsed;
    if (!parsed) {
      selectedId.value = null;
      editingId.value = null;
    }
  }

  function applyContent(next: MindMapContent, history: boolean): boolean {
    // 生成还在进行时禁止用户改动：done 帧会用完整结果整树替换，期间的编辑
    // 必然被覆盖；撤销栈里也会混进生成中间态，撤销起来只剩半成品。
    // 返回 false 让调用方提前退出，免得它接着把 status 改成「已修改」。
    if (busy.value) {
      status.value = '正在生成导图，完成之后才能编辑。';
      return false;
    }
    if (history) pushHistory();
    content.value = next;
    dirty.value = true;
    clientRevision.value += 1;
    return true;
  }

  /** 节点深度，1 起算（与契约的"最多 8 层"同一口径）。 */
  function depthOfId(id: string): number | null {
    const hit = flatNodes.value.find(entry => entry.node.id === id);
    return hit ? hit.depth + 1 : null;
  }

  function beginEdit(id: string) {
    if (!content.value || !findNode(content.value.root, id)) return;
    selectedId.value = id;
    editingId.value = id;
  }

  function endEdit() {
    editingId.value = null;
  }

  function commitLabel(id: string, label: string) {
    if (!content.value) return;
    const node = findNode(content.value.root, id);
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

    if (!applyContent({ ...content.value, root: renameNode(content.value.root, id, next) }, true)) return;
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
    const node = findNode(content.value.root, id);
    if (!node) return;

    const next = note?.trim() ?? '';
    const before = node.note?.trim() ?? '';
    if (next === before) return;
    if (charLength(next) > LIMITS.note) {
      status.value = `节点正文最多 ${LIMITS.note} 个字`;
      return;
    }

    if (!applyContent({ ...content.value, root: setNodeNote(content.value.root, id, next) }, true)) return;
    status.value = next ? '已修改正文，记得保存。' : '已清空正文，只保留标题，记得保存。';
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
    if (countNodes(content.value.root) >= LIMITS.nodes) {
      status.value = `导图最多 ${LIMITS.nodes} 个节点`;
      return;
    }
    const parent = findNode(content.value.root, parentId);
    if (!parent) return;
    if (parent.children.length >= LIMITS.children) {
      status.value = `一个节点最多 ${LIMITS.children} 个子节点`;
      return;
    }
    if ((depthOfId(parentId) ?? 1) + 1 > LIMITS.depth) {
      status.value = `导图最多 ${LIMITS.depth} 层`;
      return;
    }

    const { root, node } = addChild(content.value.root, parentId);
    if (!applyContent({ ...content.value, root }, true)) return;
    selectedId.value = node.id;
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
    const parent = content.value ? findParent(content.value.root, id) : null;
    commitLabel(id, label);
    // 提交被拦下（空标题、超长）时编辑态还在，别在错误的基础上接着加节点。
    if (editingId.value === id) return;
    addChildTo(parent?.id ?? selectedId.value ?? id);
  }

  function addSiblingTo(id: string) {
    if (!content.value) return;
    if (countNodes(content.value.root) >= LIMITS.nodes) {
      status.value = `导图最多 ${LIMITS.nodes} 个节点`;
      return;
    }
    const parent = findParent(content.value.root, id);
    // 根节点没有兄弟，退化成一个子节点；这时要按"加子节点"的规则校验层数与个数。
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

    const { root, node } = addSibling(content.value.root, id);
    if (!applyContent({ ...content.value, root }, true)) return;
    selectedId.value = node.id;
    editingId.value = node.id;
    status.value = '已加兄弟节点，输入文字后按回车确认。';
  }

  function deleteNode(id: string) {
    if (!content.value) return;
    if (content.value.root.id === id) {
      status.value = '根节点不能删除';
      return;
    }

    // id 不在树上（预览的派生 id、候选的 id 都会走到这里）就直接退出。
    // 交给 removeNode 的话它只会原样返回整棵树，而 applyContent 照样记一步历史、
    // 递增 clientRevision——用户什么都没删，撤销栈和候选的新鲜度却被改脏了。
    if (!findNode(content.value.root, id)) return;

    const parent = findParent(content.value.root, id);
    if (!applyContent({ ...content.value, root: removeNode(content.value.root, id) }, true)) return;
    selectedId.value = parent?.id ?? null;
    editingId.value = null;
    status.value = '已删除该节点及其子节点，可撤销。';
  }

  function undo() {
    if (busy.value) {
      status.value = '正在生成导图，完成之后才能编辑。';
      return;
    }
    const snap = past.value.pop();
    if (!snap) return;
    pastBytes.value -= snapshotBytes(snap);
    // 当前是空画布时也要压一步 `'null'`，否则「空 → 候选 → 撤销 → 重做 → 撤销」
    // 的最后一步会丢掉：重做那一次不记历史，再撤销就没得可撤，回不到空白。
    future.value.push(snapshot() ?? 'null');
    restoreSnapshot(snap);
    dirty.value = true;
    // 撤销改的就是画布本身，必须让「候选基于哪个版本算出来」的判断看见这一步。
    // 不递增的话，用户撤销一步再点「应用到画布」，撤销白做、候选照样落地。
    clientRevision.value += 1;
    ensureSelection();
    editingId.value = null;
    status.value = '已撤销。';
  }

  function redo() {
    if (busy.value) {
      status.value = '正在生成导图，完成之后才能编辑。';
      return;
    }
    const snap = future.value.pop();
    if (!snap) return;
    // 与 undo 对称：当前是空画布时把空画布压进撤销栈，这样重做之后仍能撤回空白。
    recordPast(snapshot() ?? 'null');
    restoreSnapshot(snap);
    dirty.value = true;
    // 同 undo：重做同样把画布换成了另一棵树，候选再不成立。
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

  function loadMap(next: MindMapContent, record: { id: string; version: number; updated_at: string }) {
    cancel();
    mapSession += 1;
    // 换了一张图，上一张图算出来的候选没有任何意义：留着它，一次误点就会把
    // 那张图的内容装进这张图。
    candidate.value = null;
    resetCandidateOps();
    saving.value = false;
    titleDraft.value = null;
    content.value = next;
    mapId.value = record.id;
    version.value = record.version;
    savedAt.value = record.updated_at;
    mode.value = 'saved';
    dirty.value = false;
    selectedId.value = null;
    editingId.value = null;
    viewAdjusted.value = false;
    resetHistory();
    status.value = `已打开导图，版本 ${record.version}。`;
  }

  function newMap() {
    cancel();
    mapSession += 1;
    // 同上：新建之后旧候选不再属于画布上的任何一张图。
    candidate.value = null;
    resetCandidateOps();
    saving.value = false;
    titleDraft.value = null;
    content.value = null;
    mapId.value = null;
    version.value = 0;
    savedAt.value = null;
    mode.value = null;
    dirty.value = false;
    selectedId.value = null;
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
    try {
      const record = mapId.value
        ? await updateMindMap(mapId.value, payload, version.value)
        : await createMindMap(payload);

      // 请求在途时用户可能已经切到别的导图（或点了新建）。这时响应属于
      // 上一张图，不能拿它改当前页面的 mapId —— 改了之后画布显示的是 B、
      // 身份却是 A，再点保存就会把 B 的内容写进 A。
      if (session !== mapSession) {
        status.value = `上一张导图已保存（版本 ${record.version}），当前导图未受影响。`;
        return 'saved';
      }

      // 请求在途时用户可能还在编辑。applyContent() 是整体替换 content.value，
      // 所以比一次引用就知道内容有没有变过。变了必须把脏标记恢复回来——
      // markSaved() 无条件把它清成 false，那样后改的内容会被误标成"已保存"，
      // 而请求体里发出去的只是点保存那一刻的树，刷新即丢。
      const changedDuringSave = content.value !== current;
      markSaved(record);
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
    try {
      const record = await createMindMap(current);
      if (session !== mapSession) {
        return;
      }
      const changedDuringSave = content.value !== current;
      markSaved(record);
      conflict.value = null;
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
   * 选中一个节点。
   *
   * 只接受 `content` 里真实存在的节点。预览的派生 id 和候选的 id 都还没进画布：
   * 选中它们会让右侧详情面板、面包屑，以及 `expand` 请求里的 `selected_ids` 一起
   * 对着一个不存在的节点说话。画布那边同样给这些节点设了 `selectable: false`，
   * 这里是不依赖渲染层的最后一道。
   */
  function select(id: string | null) {
    if (id !== null && !(content.value && findNode(content.value.root, id))) return;
    selectedId.value = id;
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
  function preview(next: MindMapContent, dir: Direction) {
    cancel();
    // 换了一张图，会话必须跟着换：否则上一张图在途的保存响应回来时，
    // 会因为会话号没变而把它的 id 写进 mapId——画布上是新图、身份却是旧图，
    // 下次保存就把新内容 PUT 到旧记录上。
    mapSession += 1;
    // 换图即作废旧候选（preview 同样会换掉整棵树）。
    candidate.value = null;
    resetCandidateOps();
    titleDraft.value = null;
    // 上一张图在途的保存到这里就作废了：它的 finally 因为会话不匹配不会
    // 清理 saving，这里必须自己复位，否则新图永远卡在"保存中"，点保存
    // 只会返回 busy。loadMap / newMap / generate 都是同一套处理。
    saving.value = false;
    content.value = withDirection(next, dir);
    mode.value = 'demo';
    selectedId.value = null;
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
   * 掐掉在途的 agent 运行。
   *
   * 取消只是停止这一轮：画布上的 `content` 从来没被这一轮碰过（候选落地要用户
   * 点「应用」），所以不需要回滚，清掉属于这一轮的中间态就够了。
   * 换图场景（loadMap / newMap / preview）也走它，把旧流掐死。
   */
  function cancel() {
    const active = controller;
    controller = null;
    active?.abort();
    busy.value = false;
    agentRunning.value = false;
    // 这一轮就此作废：告警跟着一起清，否则取消之后面板还挂着"部分分支未完成"。
    agentWarnings.value = [];
    // 取消/换图同理：长到一半的预览属于已经结束的那一次运行。
    clearAgentPreview();
    stageText.value = '';
    status.value = '已取消。';
  }

  // ---- agent 运行与候选接受 ----

  /**
   * 把一批 op 应用到候选上，返回是否应用成功。
   *
   * 只动 `candidate`，绝不写 `content`：采纳仍然要用户点「应用」。这是整条 agent
   * 路径的核心约束，不因为帧是增量的就默认落地。
   *
   * 失败与乱序都**静默丢弃**：`done` 帧会带完整树覆盖候选，少应用一批不会丢数据；
   * 而在这种时候弹一句"修改没能应用"，用户只会以为最终结果不可信。
   */
  function applyOpsToCandidate(batch: OpBatch): boolean {
    // edit / expand 这两条路上后端不发 snapshot（候选从请求带过去的树起步），
    // 所以候选要由画布那份树的副本兜底——`applyOps` 自己会克隆，不会改到 content。
    // 不兜底的话第一批 op 就没有落点，"打开旧图 → 让 AI 改"依旧一点反应都没有。
    const base = candidate.value ?? (candidateIntent === 'create' ? null : content.value);
    if (!base) return false;
    // 对不上就是中间漏了一批：在错位的树上继续套用只会套出错位的结构。
    if (batch.base_revision !== candidateRevision) {
      previewDebug(`[agent] 丢弃 ops 批次：base_revision=${batch.base_revision}，当前 ${candidateRevision}`);
      return false;
    }
    try {
      candidate.value = applyOps(base, batch).content;
    } catch (error) {
      previewDebug(`[agent] 丢弃 ops 批次：${error instanceof Error ? error.message : String(error)}`);
      return false;
    }
    // 只有真正落进候选才算这一版完成，失败那批不能推进账本。
    candidateRevision = batch.base_revision + 1;
    return true;
  }

  async function runGeneration(
    goal: string,
    intent: 'create' | 'expand' | 'edit' = 'create',
    sources?: Source[],
    thinkingModel: string = 'default',
  ): Promise<void> {
    if (busy.value || agentRunning.value) return;
    // 来源合法性在这里再判一次。组件里已有同样的闸门，但 `runGeneration` 是唯一
    // 入口——绕过组件直调（测试，或将来加的快捷入口）时那一层就不在了。判一次
    // 只省一次 422 往返，权威仍是后端契约校验。
    if (sources && sources.length > 0) {
      const problem = sourceInputError(sources);
      if (problem) {
        agentError.value = problem;
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
      agentWarnings.value = [];
      clearAgentPreview();
      status.value = '请在下方「输入主题或文本」里写下要生成的内容；上面的「导图标题」只改名字，不会重新生成。';
      return;
    }
    if (intent !== 'create' && !content.value) {
      status.value = '当前还没有导图，无法在此基础上修改。';
      return;
    }

    agentError.value = '';
    agentEvents.value = [];
    // 上一轮的告警不能带到这一轮：面板里挂着旧告警，用户会以为这次又缺分支。
    agentWarnings.value = [];
    // 同理，上一轮是演示还是模型，跟这一轮没关系；不清就会把上一轮的 demo
    // 标记挂到这一轮的候选上。
    agentMode.value = null;
    candidate.value = null;
    // 新一轮的 op 从第 0 版重新开始，上一轮的账本留着只会把首批 op 判成乱序。
    resetCandidateOps();
    // 上一轮长到一半的预览同样不能留：它的节点属于上一次运行，混进这一轮的
    // 面板里，用户会以为新任务已经长出了这些分支。
    clearAgentPreview();
    // 记下候选所依据的版本与所在的图：接受时用它判断期间有没有被改过、有没有换了图。
    // 意图也要记：接受时得知道这份内容算「新作品」还是「改现有这张图」。
    candidateBaseRevision = clientRevision.value;
    candidateSession = mapSession;
    candidateIntent = intent;

    const ctrl = new AbortController();
    controller = ctrl;
    agentRunning.value = true;
    busy.value = true;
    stageText.value = '正在连接';
    status.value = '正在与模型协作…';
    waitedMs.value = 0;

    // 流只在 done / error 帧上才算有了明确结局；两者都没到就 EOF，说明连接被掐断了，
    // 不能当成正常结束——否则面板会停在「正在与模型协作…」，工具行永远显示「执行中…」。
    let settled = false;

    try {
      const body: AgentRunRequest = {
        goal: prompt,
        intent,
        // 始终显式带上：即使宿主页面没给值也发 default，而不是省略字段。
        // 省略会把「前端没传」和「后端默认」两个概念混在一起，排查请求体时看不出
        // 用户到底选了哪套拆解方式；值本身与后端默认一致，传了不影响结果。
        thinking_model: thinkingModel || 'default',
        // create 不能带 snapshot（后端开流前会 422）；另外两种必须带
        snapshot: intent === 'create' ? null : content.value,
        selected_ids: intent === 'expand' && selectedId.value ? [selectedId.value] : [],
      };
      // 来源原样送去，一个字都不截：材料是"依据"，goal 是"要做什么"。
      // 把正文拼进 goal 会同时踩两个坑——被截断的材料丢掉大半，且模型分不清
      // 目标与素材。空数组不发：旧后端对多出来的字段是 extra="forbid"。
      if (sources && sources.length > 0) body.sources = sources;

      await runAgentStream(body, event => {
        // 被取消或被新请求接管的旧流：一帧都不许再改界面状态，
        // 否则旧回调会把新请求刚设好的 stageText / status / candidate 覆盖掉。
        if (controller !== ctrl) return;
        agentEvents.value = [...agentEvents.value, event];
        switch (event.type) {
          case 'stage':
            stageText.value = event.detail || event.stage;
            break;
          case 'tool_start':
            // 状态行显示的也是人话：用户读不到 `mindmap_generate` 这种函数名。
            stageText.value = agentToolLabel(event.name, 'running');
            break;
          case 'tool_end': {
            stageText.value = event.ok
              ? agentToolLabel(event.name, 'done')
              : `${event.name} 未成功`;
            // 后端分片生成会以 ok=true 报"结果不完整"：partial 与 diagnostics 都在
            // result_summary 里。不在这里接下来，面板与状态行都看不见它。
            const warnings = agentWarningsFrom(event.result_summary);
            if (warnings.length) {
              recordWarnings(warnings);
              // 候选可能已经先到了（snapshot 与工具帧的先后不固定）。这时状态行
              // 还写着"候选已就绪"，得跟着改，不能让两句文案在同一个面板上打架。
              if (candidate.value && event.ok) status.value = candidateStatus('候选已就绪');
            }
            break;
          }
          case 'node_grow':
            // 只进预览树。真正可采纳的内容在 snapshot / done 的 `content` 里，
            // 两者可能不一致——把预览当候选，等于把一棵没校验过的树交给用户应用。
            growPreviewNode(event.path, event.label, event.expandable);
            break;
          case 'ops':
            // `edit` 意图的改动**只**以 op 批次下发（没有整树快照）。不接这一帧，
            // "打开旧图 → 让 AI 改 → 界面没反应"就是必然结果。
            if (applyOpsToCandidate(event.batch)) {
              status.value = `已应用 ${event.batch.ops.length} 条修改，确认后才会应用到画布。`;
            }
            break;
          case 'snapshot':
            // 后端整树校验通过后先推一帧 snapshot：done 之前用户就该看得见候选长什么样。
            // 完整候选一到，预览就没有存在意义了，整棵作废（含还等着挂的缓存）。
            clearAgentPreview();
            // 只放候选、不碰 content——没经用户确认的内容不许进画布。
            candidate.value = event.content;
            status.value = candidateStatus('候选已就绪');
            break;
          case 'done':
            settled = true;
            // 这棵树的来历只有 done 帧说得准，落在这里等「接受」时再写进 mode。
            // 后端 `exclude_none=True`，所以正常帧一定带值；缺字段只说明对面是
            // 没填 mode 的旧版本，记作"没说"，不能替它认成模型产出。
            agentMode.value = event.mode ?? null;
            // 终态已到，预览再留着只会让人以为还有节点在路上。
            clearAgentPreview();
            if (event.content) {
              // 只放进候选，不动 content：用户确认之前画布保持原样
              candidate.value = event.content;
              status.value = candidateStatus(
                agentMode.value === 'demo' ? '演示导图已就绪（不是 AI 生成的）' : '模型给出了候选导图',
              );
            } else if (!candidate.value) {
              // 已经收到过 snapshot 的候选就留着：这一帧只是没重复带内容。
              status.value = '模型这次没有产出导图。';
            }
            break;
          case 'error':
            settled = true;
            // 失败时预览同样作废：它可能正好停在半截，留着会被当成"这次的结果"。
            clearAgentPreview();
            agentError.value = event.message;
            status.value = event.message;
            break;
        }
      }, ctrl.signal);

      // 已经被后来的请求接管：这个旧请求无论成功、失败还是被取消，都不许再改状态。
      if (controller !== ctrl) return;
      if (!settled && !ctrl.signal.aborted) {
        // 流正常结束但没有终态帧：中途断了，不能假装成功。
        agentError.value = '连接中断，结果可能不完整，请重试。';
        status.value = '连接中断，结果可能不完整，请重试。';
      }
    } catch (error) {
      if (controller !== ctrl) return;
      const message = error instanceof ApiError ? error.message : '运行失败，请重试。';
      agentError.value = message;
      status.value = message;
    } finally {
      // 取消后立刻重新运行、或换图后新请求已接管时，旧请求的收尾不能清掉新请求的状态。
      if (controller === ctrl) {
        controller = null;
        agentRunning.value = false;
        busy.value = false;
        stageText.value = '';
      }
    }
  }

  /**
   * 把候选应用到画布。
   *
   * 四道校验缺一不可：候选属于当前这张图、有候选、没有未提交的输入、树没被改过。
   * 少任何一条，用户手打了一半的标题、刚做的编辑，或另一张图的内容都会被候选
   * 静默覆盖——那正是这次改造要解决的问题，不能在新路径上重演。
   */
  function acceptCandidate(): 'applied' | 'stale' | 'invalid' {
    // 换图判在"有没有候选"之前：切图会把 candidate 清成 null，但 candidateSession
    // 还留着上一张图的号码。两者不等就说明这次候选属于另一张图，装上去等于把
    // 那张图的内容写进当前记录。
    if (candidateSession !== mapSession) {
      candidate.value = null;
      resetCandidateOps();
      status.value = '候选是在另一张导图上生成的，已失效，请重新运行。';
      return 'stale';
    }
    if (!candidate.value) return 'invalid';
    if (editingId.value !== null || titleDraft.value !== null) {
      status.value = '请先提交或取消正在编辑的内容。';
      return 'stale';
    }
    if (clientRevision.value !== candidateBaseRevision) {
      status.value = '候选生成之后这张图被改过，已失效，请重新运行。';
      return 'stale';
    }
    // 装进画布之前必须整树校验一次。候选是模型产出的外部数据，它的 label
    // 长度、引用区间、节点数都可能越界；不在这里拦，脏数据会先进画布、
    // 直到用户点保存才被后端 422 拒绝，而那时他已经在上面改了半天。
    const invalid = validateDraft(candidate.value);
    if (invalid) {
      status.value = `候选不合法，已丢弃：${invalid}`;
      candidate.value = null;
      resetCandidateOps();
      return 'invalid';
    }
    // 到这里候选可以落地了。先按意图处理"这份内容是谁"：
    // create 是一份新作品，绝不能继承上一张图的身份——否则接受之后点保存会走
    // PUT，把刚打开的那条旧记录整条覆盖掉。
    if (candidateIntent === 'create') {
      mapId.value = null;
      version.value = 0;
      savedAt.value = null;
      // 上一张图在途的保存响应回来时不能再把它的 id 写回 mapId（那是"新图挂旧身份"
      // 的另一条路径），所以会话跟着换；saving 也自己复位——那一轮请求的 finally
      // 会因为会话不匹配跳过清理，不复位就会永远卡在"保存中"。
      mapSession += 1;
      saving.value = false;
      resetHistory();
      // 新作品不接上一张图的历史：撤销栈里只留「接受之前」这一步。
      pushHistoryOrEmpty();
    } else {
      // 改的是现有这张图：身份、版本、历史都沿用，撤销回到改动前的那棵树。
      pushHistory();
    }
    content.value = candidate.value;
    candidate.value = null;
    // 候选落地之后账本归零：这份内容已经进了 content，下一轮从第 0 版重来。
    resetCandidateOps();
    // 来源标记跟着 done 帧走，不再一律写成「模型」：演示模式产出的是一棵占位树，
    // 给它贴"模型生成"的标签就是让界面替它撒谎。没说（旧后端）才退回 model。
    mode.value = agentMode.value ?? 'model';
    dirty.value = true;
    clientRevision.value += 1;
    editingId.value = null;
    // 告警说的是"这份候选缺了什么"，候选已落地就没有再挂着的理由；
    // 留着它，用户会以为画布上这棵树还有问题等着处理。
    agentWarnings.value = [];
    ensureSelection();
    status.value = mode.value === 'demo'
      ? '已应用演示导图（固定结构，不是 AI 生成），记得保存。'
      : '已应用模型的修改，记得保存。';
    return 'applied';
  }

  function discardCandidate(): void {
    const had = candidate.value !== null;
    candidate.value = null;
    resetCandidateOps();
    agentError.value = '';
    agentWarnings.value = [];
    status.value = had ? '已丢弃候选，画布未变。' : '没有待处理的候选。';
  }

  return {
    content,
    mode,
    status,
    stageText,
    waitedMs,
    busy,
    selectedId,
    direction,
    viewAdjusted,
    editingId,
    titleDraft,
    setTitleDraft,
    mapId,
    version,
    savedAt,
    dirty,
    layout,
    flatNodes,
    nodes,
    edges,
    nodeCount,
    selectedNode,
    selectedRefs,
    refIndex,
    focusTick,
    breadcrumb,
    canUndo,
    canRedo,
    undoDepth,
    undoBytes,
    saving,
    conflict,
    maps,
    listOpen,
    listBusy,
    cancel,
    preview,
    select,
    locate,
    setDirection,
    beginEdit,
    endEdit,
    commitLabel,
    commitNote,
    renameTitle,
    addChildTo,
    addSiblingTo,
    commitAndAddChild,
    deleteNode,
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
    closeList,
    openMap,
    // agent 路径
    clientRevision,
    candidate,
    agentPreview,
    agentPreviewDropped,
    agentEvents,
    agentWarnings,
    agentRunning,
    agentError,
    agentMode,
    runGeneration,
    acceptCandidate,
    discardCandidate,
  };
});
