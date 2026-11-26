<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import MindMapCanvas from './canvas/MindMapCanvas.vue';
import type { Direction } from './canvas/layout/tidy';
import AgentPanel from './components/AgentPanel.vue';
import ChatDock from './components/ChatDock.vue';
import HistoryDrawer from './components/HistoryDrawer.vue';
import MessageFab from './components/MessageFab.vue';
import NodeToolbar from './components/NodeToolbar.vue';
import SettingsPanel from './components/SettingsPanel.vue';
import SourceContextDrawer from './components/SourceContextDrawer.vue';
import StructureCheck from './components/StructureCheck.vue';
import { SOURCE_LABELS } from './sources/input';
import { ApiError, clearAliases as clearAllAliasesRequest, deleteAlias as deleteAliasRequest, deleteLibrarySource, exportFilename, exportLabel, exportMindMap, getHealth, listAliases, listLibrary, readLibrarySource, type ExportFormat } from './api/client';
import type { AliasEntry, LibraryEntry, LibrarySource } from './api/types';
import type { SampleMap } from './dev/samples';
import type { DemoPreset } from './demo/presets';
import { displayBody, bodySource, useMindMapStore } from './stores/mindmap';
import { useChatStore } from './stores/chat';
import { useSourcesStore } from './stores/sources';
import { usePrefsStore } from './stores/prefs';
import { resolveRefs, type ResolvedRef } from './mindmap/refs';
import { agentDetailVisible } from './mindmap/run-status';
import { EDGE_KINDS, EDGE_KIND_LABELS, NODE_COLORS, NODE_COLOR_LABELS } from './mindmap/edits';
import type { DiagramContent, EdgeKind, RelationKind } from './api/types';
import { containerNoun, kindOf, topBranchesOf } from './mindmap/shape';

const store = useMindMapStore();
const chat = useChatStore();
const sources = useSourcesStore();

/**
 * 选中节点的引用，解析成"能直接画"的形状。
 *
 * 原文取自 sources store：本次会话里解析过的来源才能回查，从已保存导图打开时
 * readySources 是空的，解析结果里 source 为 null，抽屉会据此降级成"只看摘录"。
 */
const selectedRefs = computed(() =>
  resolveRefs(store.selectedRefs, sources.readySources, store.content?.source_usage ?? []),
);

/** 正在右侧抽屉里查看的引用；null 表示抽屉关着。 */
const activeRef = ref<ResolvedRef | null>(null);

/**
 * 全图引用覆盖率的一句话说明，只在"这个节点没有引用"时出现。
 *
 * 三档写法，各自对应一个真实形态：
 *   - 一个都没有 → 说清"这次生成没有可回查的材料"，而不是让用户去点空白；
 *   - 有部分 → 给出比例，让"没引用的节点"变成这张图的常态而不是一处异常；
 *   - 全都有 → 这一档不会进来（节点没有任何引用才算覆盖率说明），
 *     所以不写第 3 句。
 *
 * 最后那句"或者是你自己加的"是**诚实的边界**：`MindNode` 没有"手动/生成"标记，
 * 分不出这两种，所以不假装能分。
 */
const citationSummary = computed(() => {
  const { total, cited } = store.refCoverageStat;
  if (!total) return '';
  if (!cited) {
    return '这张图的节点都没有原文引用——生成时没有可回查的材料，或者这张图是手工画的。';
  }
  const percent = Math.round((cited / total) * 100);
  return `这张图 ${total} 个节点里有 ${cited} 个带原文引用（${percent}%）。没有引用的节点不代表内容有错：生成时模型没在材料里找到对应句子，或者这个节点是你自己加的。`;
});

const copied = ref('');

/**
 * 屏幕底部的短暂提示。
 *
 * 导出的结果原来只写进左侧来源面板的 `store.status` 小字里，位置在折叠区下方，
 * 失败时看上去就是"点了没反应"（用户实测：控制台 422，界面无提示）。成功和失败
 * 都要能一眼看到，所以单独开一条浮层，四秒后自动消失。
 *
 * `action` 是给"刚清掉的东西"用的撤销入口（「新对话」清材料那一句）：只写"已清空
 * 3 份材料"而不给回去的路，等于让用户确认一次损失再目送它消失。带动作的提示停
 * 8 秒——比纯通知久，因为他可能正在读那句"清掉了什么"。
 */
const notice = ref<{
  kind: 'ok' | 'error';
  text: string;
  action?: { label: string; run: () => void };
} | null>(null);
let noticeTimer: ReturnType<typeof setTimeout> | null = null;

/** 后端自报的版本号（线上是 tag，本机是 dev）；拉不到就是空串。 */
const appVersion = ref('');

/** 标题旁那个小徽章：tag 自带 `v` 前缀，别显示成 `vv0.4.7`；dev 就写 dev。 */
const versionLabel = computed(() => {
  const raw = appVersion.value.trim();
  if (!raw) return '';
  if (raw === 'dev') return 'dev';
  return /^v/i.test(raw) ? raw : `v${raw}`;
});

function flash(
  kind: 'ok' | 'error',
  text: string,
  action?: { label: string; run: () => void },
): void {
  if (noticeTimer) clearTimeout(noticeTimer);
  notice.value = { kind, text, action };
  const ms = action ? 8000 : kind === 'error' ? 6000 : 3000;
  noticeTimer = setTimeout(() => {
    notice.value = null;
    noticeTimer = null;
  }, ms);
}

// 开发期示例选择器：只在 dev 服务器下加载，生产构建里这段会被整体去掉。
const samples = ref<{ id: string; label: string }[]>([]);
let sampleMaps: SampleMap[] = [];

if (import.meta.env.DEV) {
  void import('./dev/samples').then(mod => {
    sampleMaps = mod.SAMPLE_MAPS;
    samples.value = mod.SAMPLE_MAPS.map(({ id, label }) => ({ id, label }));
    // 方便复现与截图：地址栏加 ?sample=down 就直接装那个示例。
    const wanted = new URLSearchParams(window.location.search).get('sample');
    const initial = sampleMaps.find(item => item.id === wanted);
    if (initial) store.preview(initial.content, initial.direction);
  });
}

function loadSample(id: string) {
  const found = sampleMaps.find(item => item.id === id);
  if (found) store.preview(found.content, found.direction);
}

const directions: { value: Direction; label: string }[] = [
  { value: 'right', label: '向右' },
  { value: 'left', label: '向左' },
  { value: 'down', label: '向下' },
  { value: 'both', label: '双向' },
];

const selected = computed(() => store.selectedNode);
const pathText = computed(() => store.breadcrumb.map(node => node.label.trim()).join(' / '));

/**
 * 「节点详情」分区的正文。
 *
 * 取值来自 `displayBody`（`detail → note → label`），与卡片、导出、复制全文同一个
 * 函数——四处各写一遍回退链，迟早出现"卡片里有正文、面板里没有"这种说不清的分歧。
 *
 * `bodyFrom` 决定那行小字怎么写。它存在的理由：旧图没有 `detail`，回退到 `note`
 * 之后界面上那段文字**同时也是「我的备注」输入框里的内容**，不说明来源的话，用户
 * 会以为生成说明了什么，其实那是他自己以前写的备注。
 */
const bodyText = computed(() => (selected.value ? displayBody(selected.value.node) : ''));
const bodyFrom = computed(() => (selected.value ? bodySource(selected.value.node) : 'label'));
const BODY_SOURCE_NOTE: Record<string, string> = {
  detail: '这次生成写的说明',
  note: '这个节点没有生成详情，下面是你以前写的备注',
  label: '这个节点没有正文，下面是标题全文',
};

/**
 * 正文编辑框的草稿。
 *
 * 和标题一样不逐键写回 content：每敲一个字都进撤销栈的话，撤销一次只退一个字符。
 * 选中变化时重新取一次，避免把上一个节点的正文带到另一个节点上。
 */
const noteTargetId = ref<string | null>(null);
const noteDraft = ref('');

watch(
  () => [selected.value?.node.id ?? null, selected.value?.node.note ?? ''] as const,
  ([id, note]) => {
    noteTargetId.value = id;
    noteDraft.value = note;
  },
  { immediate: true },
);

const noteLength = computed(() => Array.from(noteDraft.value).length);

function commitNoteDraft() {
  const id = noteTargetId.value;
  if (!id) return;
  store.commitNote(id, noteDraft.value);
  // 提交后以 store 里的值为准：空正文会被归一成 null，草稿也要跟着归一，
  // 否则框里留着空格、界面上却显示"没有正文"。
  noteDraft.value = store.selectedNode?.node.note ?? '';
}

function resetNoteDraft() {
  noteDraft.value = store.selectedNode?.node.note ?? '';
}

// ---- 附加关系（流程图的条件分支 / 汇合 / 回退）--------------------------------
//
// 最小可用入口，三条理由让它必须存在：
//   1. 模型只在 `flowchart` 生成时给边，用户手里一张 `tree` 图永远补不出流程；
//   2. 模型给的边可能是错的，而"删掉那条画错的回退线"没有别的入口；
//   3. 边不参与布局，所以增删它不会重排画布——这件事只有在界面上点一次才看得出。

/** 新边连到哪个节点（下拉框的值）。默认空：用户得自己挑，不替他猜。 */
const edgeTarget = ref('');
const edgeLabel = ref('');
const edgeKind = ref<EdgeKind>('branch');

/**
 * 候选目标：当前内容里除选中节点之外的所有节点。自环在契约层就是硬拒的，列出来也没用。
 *
 * 读 `selectableNodes` 而不是 `flatNodes`：鱼骨与流程的画布上多一个**不在 content 里**
 * 的合成根（鱼头 / 流程起点），它是一个纯渲染产物，既不能当端点也不该出现在下拉框里。
 */
const edgeTargets = computed(() => {
  const current = selected.value?.node.id;
  return store.selectableNodes
    .filter(entry => entry.node.id !== current)
    .map(entry => ({ id: entry.node.id, label: entry.node.label.trim(), depth: entry.depth }));
});

/**
 * 边端点的显示名：`id → 标题`。删边/列边都要它，用户认标题不认 id。
 *
 * 走 store 的 `edgeNameOf` 而不是在这里自己建表：流程图的边端点存的是**阶段 id**
 * （另一个命名空间），自己按节点建的表会把它显示成一串 `s_xxx`。
 */
const edgeNodeName = computed(() => store.edgeNameOf);

/**
 * 选中节点相关的边，拼成"谁 → 谁（标签）"给列表用。
 *
 * **两层分开两份**（而不是拼成一份带标记的列表）：阶段之间与阶段内部是两个数组、
 * 两个命名空间（`FlowContent.edges` 的端点是阶段 id，`FlowStage.edges` 的端点是
 * 该阶段的节点 id），界面上也确实是两种东西——一个是这张图的走向，一个是某一步上
 * 的分叉。混在一列里用户读到"提交申请 → 付款"和"审不审 → 退回"会以为是同一类关系。
 */
function edgeRow(edge: { id: string; source: string; target: string; label?: string | null; kind?: string | null }) {
  return {
    id: edge.id,
    text: `${edgeNodeName.value(edge.source)} → ${edgeNodeName.value(edge.target)}`,
    label: edge.label ?? '',
    kind: edge.kind ?? 'flow',
  };
}

const selectedEdgeRows = computed(() => store.selectedEdges.map(edgeRow));

/** 阶段内部的边（判定节点的「是 / 否」与步骤之间的顺序跳转）。 */
const selectedInnerEdgeRows = computed(() => store.selectedInnerEdges.map(edgeRow));

/** 两条列表加起来才是"这个节点有几条附加关系"。 */
const selectedEdgeTotal = computed(
  () => selectedEdgeRows.value.length + selectedInnerEdgeRows.value.length,
);

/**
 * 六种关系的中文短标签。与 `canvas/MindRelation.vue` 里那张表**必须一致**：
 * 画布上的虚线写「依赖」，侧栏里也必须是「依赖」——两处各写一套的话，用户会以为
 * 它们是两个不同的字段。
 */
const RELATION_KIND_LABELS: Record<RelationKind, string> = {
  depends_on: '依赖',
  causes: '因果',
  conflicts: '冲突',
  part_of: '属于',
  refines: '细化',
  references: '引用',
};

/**
 * 选中节点参与的关系，拼成"谁 → 谁（kind）+ why"给列表用。
 *
 * `why` 一起列出来是这个区块与「附加关系」最大的差别：附加边只需要一句"哪到哪"，
 * 而关系线的价值全在理由上——没有 `why` 的那一条，读者看不出凭什么把它们连起来。
 * 生成侧已经把没有理由的边丢掉了，所以这里列出来的每一条都有话可说。
 */
const selectedRelationRows = computed(() => store.selectedRelations.map((relation) => {
  const kind = relation.kind ?? 'references';
  return {
    id: relation.id,
    text: `${edgeNodeName.value(relation.source)} → ${edgeNodeName.value(relation.target)}`,
    kind,
    label: RELATION_KIND_LABELS[kind],
    why: (relation.why ?? '').trim(),
  };
}));

/** 只要这张图有跨分支关系就摆出这个区块：没有的话它只是一个占位。 */
const showRelationSection = computed(() => store.contentRelations.length > 0);

function disconnectRelation(relationId: string) {
  store.deleteRelation(relationId);
}

const edgeHint = computed(() => {
  if (!selected.value) return '';
  if (!store.content) return '先生成或打开一张导图。';
  if (store.busy) return '正在生成，完成之后才能加关系。';
  // 上限**两类各算 200**（判据与 store 的 `connectNodes` 一致）：阶段之间的边与
  // 某个阶段内部的边是两份独立的账，合计起来算的话，一条吵闹的阶段会先把主轴的
  // 额度吃光，而两者在画布上的规模本来就不成比例。
  if (store.contentEdges.length >= 200) return '阶段之间的附加关系已到上限（200 条），请先删掉一些。';
  if (store.contentInnerEdges.length >= 200) return '阶段内部的附加关系已到上限（200 条），请先删掉一些。';
  if (!edgeTarget.value) return '选一个终点节点，再点「连一条关系」。';
  return '关系只画连线，不会改变节点位置。';
});

/**
 * 「附加关系」区块的可见性。
 *
 * 它此前**一直摆着**：这个区块只对流程图有意义（思维导图与鱼骨图的边为空是正常
 * 的，不是"这个节点没有关系"），但代码里没有任何判断依据，只能一直显示。加在
 * `MindMapContent.diagram_type` 上之后，这里才有据可依。
 *
 * 两个条件缺一不可：
 *
 *   1. 图型是流程图：模型会给边，而用户要能补错的、删画歪的；
 *   2. **或者手里已经有边**。`diagram_type` 是后加的字段，库里那些旧图缺省是
 *      `tree`，但用户可能早就手工加过边——只按图型判会让那些边再也删不掉
 *      （列表与删除按钮都在这个区块里）。
 *
 * `?? 'tree'` 不能省：字段缺失（旧图、上一个版本保存的草稿）时读成 `undefined`，
 * 直接比较会让 `=== 'flowchart'` 之外的一切都落到"没有边就隐藏"这一支，看起来
 * 恰好也对——但它把"字段缺失"与"明确是 tree"混成了一件事，而将来 `fishbone`
 * 也要有专属区块时，这个区别就是要害。
 */
const showEdgeSection = computed(
  () => (store.content?.diagram_type ?? 'tree') === 'flowchart'
    || store.contentEdges.length > 0
    || store.contentInnerEdges.length > 0,
);

/**
 * 保存列表里那一行的规模描述：「6 个一级分支」/「6 根骨」/「4 个阶段」。
 *
 * 三种形状的顶层容器是同一个概念（画布上的一块区域），但中文里各有各的量词。
 * 在这之前这里写的是 `content.root.children.length`——一张鱼骨图打开列表就会炸。
 */
function mapShapeLine(content: DiagramContent): string {
  // 树的"一级分支"是 `root.children`（中心主题是标题，不算一块内容），鱼骨/流程是
  // 容器本身——两边的口径收在 `shape.topBranchesOf` 一处。
  const count = topBranchesOf(content).length;
  return `${count} 个${containerNoun(kindOf(content))}`;
}

function connectEdge() {
  const source = selected.value?.node.id;
  const target = edgeTarget.value;
  if (!source || !target) return;
  if (store.connectNodes(source, target, edgeLabel.value, edgeKind.value)) {
    edgeLabel.value = '';
    edgeTarget.value = '';
  }
}

function disconnectEdge(edgeId: string) {
  store.deleteEdge(edgeId);
}

// 换选中节点时清掉上一轮的目标：留着它会让"给甲连到乙"在切到丙之后变成
// "给丙连到乙"，而用户以为自己点的还是甲。
watch(() => selected.value?.node.id ?? null, () => {
  edgeTarget.value = '';
  edgeLabel.value = '';
});

/**
 * 「展开这一支」按钮的状态。
 *
 * 三件事一起决定它能不能点，缺一条都会让用户白花一次模型调用：
 *   - `expandable === 'no'`：后端/模型已经说过到底了，按钮禁用并**说明原因**
 *     （灰着不解释，用户只会以为功能坏了）；
 *   - 正在展开：转成「展开中…」，同时禁用——重复点击会发两次请求，而第二次
 *     会被 `runGeneration` 的闸门静默丢掉，看起来像"点了没反应"；
 *   - 正在生成 / 编辑：互斥。两轮 agent 运行重叠时后到的那个会清掉前一个的预览，
 *     用户看到的是"之前的进度消失了"。
 *
 * `unknown` **不禁用**：那正是"旧图，或者上次被额度截断"的情况，按钮可点才有
 * 意义——把不确定当成"到头了"，唯一能继续长的地方就灰掉了。旧图没有这个字段
 * （`undefined`）时与 `unknown` 同义：它从来没被问过，而不是被问过说"没有"。
 */
const expandState = computed(() => {
  const node = selected.value?.node;
  if (!node) return { disabled: true, label: '展开这一支', hint: '' };
  const done = node.expandable === 'no';
  const busyElsewhere = store.agentRunning || store.busy;
  let hint = '让模型给这个节点补 2–4 个子节点；结果直接落画布，标成未保存。';
  if (done) {
    hint = '已经到底了：模型说过这个节点没有更细的内容可展开。';
  } else if (store.expanding) {
    hint = '正在展开这一支，新节点会边生成边长在画布上。';
  } else if (store.generating) {
    hint = '正在生成，先等这一次跑完再展开。';
  } else if (node.expandable !== 'yes') {
    hint = '不确定还能不能再拆（旧图、或上次是被额度截断的）；可以试一次。';
  }
  return {
    disabled: done || busyElsewhere,
    label: store.expanding ? '展开中…' : '展开这一支',
    hint,
  };
});

/**
 * 「展开这一支」——**带材料**的唯一起点。
 *
 * 两处按钮（抽屉里那个、节点工具条上那个）都走这里，不是各自调 `store.expandSelected()`：
 * 工具条原先自己调的版本没传 `sources`，于是"给旧图新上传一份材料、再点节点上的展开"
 * 时，请求里只有节点 id，后端只能按 `source_refs` 回查图里的旧引用——新材料这一次
 * 完全没用上，而界面上材料就在那儿摆着。两条入口各写一遍 = 漏一半，所以只留这一条。
 *
 * 材料正文只在本次会话内存里；重新打开旧图时它是空的，后端会按节点的
 * `source_refs` 从知识库把来源找回来，所以这里照传、传空也不拦。
 */
function expandBranch() {
  void store.expandSelected(sources.readySources);
}

/**
 * 说一句话的地方只有一处，但它已经不在画布右上角了——`ChatDock.vue`（画布底部那条
 * 对话框）承担 `create` 与 `edit` 两个意图，文案随状态变，选区该不该带由**看得见的
 * 徽标**决定。这个组件只负责外壳：顶栏、抽屉、悬浮球、模态与文件。
 *
 * 「展开这一支」不并入对话框：那是快捷动作（节点工具条上一枚按钮），不是文字输入。
 *
 * 后端 `edit` 意图给的是四个细粒度工具 + 批量 `mindmap_edit`，`create` 只有分片
 * 生成；两条路的改动都在 `done` 那一刻直接落画布（`commitGenerated`），没有确认步骤。
 */
const prefs = usePrefsStore();


/** 详情面板的「样式」两个控件：颜色是下拉（只列调色板），图标是一个输入框。 */
function changeColor(event: Event) {
  const id = selected.value?.node.id;
  if (!id) return;
  const value = (event.target as HTMLSelectElement).value;
  store.setNodeVisualById(id, { color: value ? (value as (typeof NODE_COLORS)[number]) : null });
}

function changeIcon(event: Event) {
  const id = selected.value?.node.id;
  if (!id) return;
  const value = (event.target as HTMLInputElement).value;
  store.setNodeVisualById(id, { icon: value });
  // 以 store 归一后的值为准（空串→清空、超长→截断），否则框里留着空格，
  // 界面上却显示"没有图标"——与备注框同一条处理。
  (event.target as HTMLInputElement).value = store.selectedNode?.node.icon ?? '';
}

/** 标题栏右侧的保存状态：说清楚"有没有没存的东西"，而不是只显示一个保存按钮。 */
const saveHint = computed(() => {
  if (store.saving) return '保存中…';
  if (!store.content) return '';
  if (store.dirty) return '有未保存的修改';
  return store.savedAt ? '已保存' : '';
});

function saveNow() {
  // 正在输入的标题或节点还没提交，文字只在输入框里。直接保存存的是旧内容，
  // 界面却显示"已保存"，刷新后那段输入就没了。先让它失焦提交一次——
  // 标题走 @change、节点走 @blur，两者都会在 blur 时把值落到 content 上。
  const active = document.activeElement;
  if (active instanceof HTMLElement && active !== document.body) {
    active.blur();
  }
  void store.save();
}

/** 打开/新建都会丢掉当前这张图上的未保存修改，先问一句。 */
function confirmDiscard(action: string): boolean {
  if (!store.dirty) return true;
  return window.confirm(`当前有未保存的修改，${action}会丢掉这些修改，继续？`);
}

/**
 * 演示用的预置导图：**只在用户点按钮时加载**，模型失败时不会自动顶上。
 *
 * 内容单独成块（动态 import），主包不因为演示数据变大。约定与理由写在
 * `frontend/src/demo/presets.ts` 的文件头。
 */
const demoOpen = ref(false);
const demoPresets = ref<DemoPreset[]>([]);
const demoBusy = ref(false);

async function openDemoPresets(): Promise<void> {
  demoOpen.value = true;
  if (demoPresets.value.length || demoBusy.value) return;
  demoBusy.value = true;
  try {
    const mod = await import('./demo/presets');
    demoPresets.value = mod.DEMO_PRESETS;
  } catch {
    store.status = '预置导图没能加载，刷新页面再试一次。';
    demoOpen.value = false;
  } finally {
    demoBusy.value = false;
  }
}

function loadDemoPreset(preset: DemoPreset): void {
  if (!confirmDiscard(`载入「${preset.label}」`)) return;
  store.loadPreset(preset);
  demoOpen.value = false;
  flash('ok', `已载入预置导图：${preset.label}`);
}

// ---- 知识库 ----
/**
 * 库里有什么材料、每份被几张已保存导图引用、点开看正文预览、删掉传错的那份。
 *
 * 不提供"从这里选材料去生成"：那个开关是 `MINDMAP_LIBRARY_SEARCH`，与本面板无关
 * ——线上那个开关关着，这个入口照样能用。材料是在**生成时**入库的，所以刚上传
 * 还没生成过的文件不会出现在这里。
 */
const libraryOpen = ref(false);
const libraryEntries = ref<LibraryEntry[]>([]);
const libraryTotal = ref(0);
const libraryBusy = ref(false);
/**
 * 列表读不出来，和"某一份正文读不出来"是两件事。
 *
 * 共用一个 error 时，预览失败会把整个抽屉顶成一句错误文案——列表没了，
 * 也退不回去，只能关掉重开。
 */
const libraryListError = ref('');
/** 当前正在看正文的那一份；为 null 表示不在正文视图。 */
const libraryPreview = ref<LibrarySource | null>(null);
/** 正在打开（或打开失败停在原地）的那一份；非 null 时抽屉显示预览壳。 */
const libraryOpening = ref<LibraryEntry | null>(null);
const libraryPreviewBusy = ref(false);
const libraryPreviewError = ref('');
/** 只让最后点开的那一份落地：连续点两份时，慢的响应不能盖掉后点的。 */
let libraryPreviewToken = 0;
/**
 * 抽屉声明了 `aria-modal`，就得按模态对待：打开时焦点移进去，Tab 圈在里面，
 * 关闭时还给原来那个按钮——否则键盘用户能 Tab 到背后的画布上。
 */
const libraryPanel = ref<HTMLElement | null>(null);
const libraryCloseButton = ref<HTMLButtonElement | null>(null);
let libraryOpener: HTMLElement | null = null;

async function openLibrary(): Promise<void> {
  // 记下是谁打开的：关闭时焦点要还回去。
  libraryOpener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  libraryOpen.value = true;
  libraryPreview.value = null;
  libraryOpening.value = null;
  libraryPreviewError.value = '';
  libraryDeleteNotice.value = '';
  libraryDeleteError.value = '';
  // 别名折叠区每次重开都回到收起状态、清掉上一次的提示：抽屉关掉之后那些话已经
  // 不属于任何一次操作了，留在界面上只会让人以为刚发生了什么。
  aliasesOpen.value = false;
  aliasesError.value = '';
  aliasNotice.value = '';
  await loadLibrary();
  await nextTick();
  libraryCloseButton.value?.focus();
}

async function loadLibrary(): Promise<void> {
  libraryBusy.value = true;
  libraryListError.value = '';
  try {
    const payload = await listLibrary();
    libraryEntries.value = payload.items;
    libraryTotal.value = payload.total;
  } catch (error) {
    libraryEntries.value = [];
    libraryTotal.value = 0;
    libraryListError.value = `知识库没能读取：${(error as Error).message}`;
  } finally {
    libraryBusy.value = false;
  }
}

async function openLibrarySource(entry: LibraryEntry): Promise<void> {
  const token = ++libraryPreviewToken;
  libraryOpening.value = entry;
  libraryPreviewError.value = '';
  libraryPreviewBusy.value = true;
  try {
    const source = await readLibrarySource(entry.source_id);
    if (token !== libraryPreviewToken) return;
    libraryPreview.value = source;
  } catch (error) {
    if (token !== libraryPreviewToken) return;
    libraryPreviewError.value = `正文没能读取：${(error as Error).message}`;
  } finally {
    if (token === libraryPreviewToken) libraryPreviewBusy.value = false;
  }
}

function backToLibrary(): void {
  // 让在途预览失效：回到列表之后，迟到的正文不该自己弹出来
  libraryPreviewToken += 1;
  libraryPreview.value = null;
  libraryOpening.value = null;
  libraryPreviewError.value = '';
  libraryPreviewBusy.value = false;
}

function closeLibrary(): void {
  libraryOpen.value = false;
  backToLibrary();
  // 焦点还给打开它的按钮；节点可能已经不在 DOM 里（列表刷新过），所以判一下。
  const opener = libraryOpener;
  libraryOpener = null;
  if (opener && opener.isConnected) opener.focus();
}

/** 把 Tab 圈在抽屉里。返回 true 表示这次按键已被接住。 */
function trapLibraryFocus(event: KeyboardEvent): boolean {
  const panel = libraryPanel.value;
  if (!panel) return false;
  const focusable = Array.from(panel.querySelectorAll<HTMLElement>(
    'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
  )).filter(element => !element.hasAttribute('disabled'));
  if (focusable.length === 0) return false;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  const inside = active instanceof HTMLElement && panel.contains(active);
  if (event.shiftKey && (!inside || active === first)) {
    event.preventDefault();
    last.focus();
    return true;
  }
  if (!event.shiftKey && (!inside || active === last)) {
    event.preventDefault();
    first.focus();
    return true;
  }
  return false;
}

/** 材料被几张已保存导图引用过；0 表示存了但从没进过任何一张图。 */
function citationLabel(count: number): string {
  return count > 0 ? `被 ${count} 张导图引用` : '还没有导图引用它';
}

function librarySize(entry: LibraryEntry): string {
  return `${entry.chars} 字 · ${entry.chunks} 段`;
}

// ---- 删除材料（`library.forget` 的产品入口） ----

/** 正在删的那一份；空串表示没有在删。按钮据此禁用，避免连点两次。 */
const libraryDeleting = ref('');
const libraryDeleteNotice = ref('');
const libraryDeleteError = ref('');

/**
 * 删掉一份材料。
 *
 * `cited_by > 0` 时先问一次：正文是导图证据的**最终依据**，删掉之后那些节点的引用
 * 只能显示「来源原文不可用」，而且不可逆——正文没了就再也没法回填。列表里本来就有
 * 这个数，所以确认框里的 N 不必再请求一次才知道。
 */
async function removeLibrarySource(entry: LibraryEntry): Promise<void> {
  if (entry.cited_by > 0 && !window.confirm(
    `「${entry.filename || '未命名材料'}」被 ${entry.cited_by} 张导图引用。\n`
    + '删掉之后那些节点会显示「来源原文不可用」，且无法恢复。确定删除？',
  )) return;
  await runLibraryDelete(entry, entry.cited_by > 0);
}

async function runLibraryDelete(entry: LibraryEntry, force: boolean): Promise<void> {
  libraryDeleting.value = entry.source_id;
  libraryDeleteError.value = '';
  libraryDeleteNotice.value = '';
  try {
    const result = await deleteLibrarySource(entry.source_id, force);
    // 正在预览的就是它：退回列表，否则会停在一份已经不存在的材料上。
    if (libraryOpening.value?.source_id === entry.source_id) backToLibrary();
    libraryDeleteNotice.value = result.cited_by > 0
      ? `已删除「${entry.filename || '未命名材料'}」，原来有 ${result.cited_by} 张导图引用它——那些节点的引用现在显示「来源原文不可用」。`
      : `已删除「${entry.filename || '未命名材料'}」。`;
    await loadLibrary();
  } catch (error) {
    const failure = error as ApiError;
    if (failure.code === 'source_cited') {
      // 列表里的计数可能刚好被另一张新图超过（后端判的是这一刻的计数）。这时不让
      // 用户对着"删除失败"发愣：把新的代价说清，再问一次。
      if (window.confirm('这份材料刚被更多导图引用。删掉之后那些引用会失效，且无法恢复。仍然删除？')) {
        await runLibraryDelete(entry, true);
      }
      return;
    }
    libraryDeleteError.value = `删除没能完成：${failure.message}`;
  } finally {
    libraryDeleting.value = '';
  }
}

// ---- 实体别名（模型猜出来的"这两个名字是同一个东西"） ----

/**
 * 别名清单。**这是"模型把实体认错了、能不能改回来"唯一的界面入口**。
 *
 * 别名不参与证据：删掉一条只让这两个名字重新变成两个实体，挂在它们身上的证据边
 * 一条不动。所以这里只有"删掉错的那条"，没有"合并两个实体"——合并要跨图搬引用，
 * 是另一件事。
 */
const aliasesOpen = ref(false);
const aliasEntries = ref<AliasEntry[]>([]);
const aliasConfigured = ref(true);
const aliasesBusy = ref(false);
const aliasesError = ref('');
const aliasNotice = ref('');
/** 正在删的那一条，键是 `别名→主名`。 */
const aliasDeleting = ref('');

function aliasKey(edge: AliasEntry): string {
  return `${edge.alias}→${edge.canonical}`;
}

/** 落库时的依据。凭子串进去的那批最该复查，所以把依据直接写在行上。 */
function aliasReasonLabel(edge: AliasEntry): string {
  const basis = edge.source === 'substring' ? '构词关系' : edge.source === 'embedding' ? '向量相似' : (edge.source || '未知依据');
  return edge.similarity > 0 ? `${basis} ${Math.round(edge.similarity * 100) / 100}` : basis;
}

async function toggleAliases(): Promise<void> {
  aliasesOpen.value = !aliasesOpen.value;
  if (aliasesOpen.value) await loadAliases();
}

async function loadAliases(): Promise<void> {
  aliasesBusy.value = true;
  aliasesError.value = '';
  try {
    const payload = await listAliases();
    aliasEntries.value = payload.items;
    aliasConfigured.value = payload.configured;
  } catch (error) {
    aliasEntries.value = [];
    aliasesError.value = `别名清单没能读取：${(error as Error).message}`;
  } finally {
    aliasesBusy.value = false;
  }
}

async function removeAlias(edge: AliasEntry): Promise<void> {
  aliasDeleting.value = aliasKey(edge);
  aliasesError.value = '';
  aliasNotice.value = '';
  try {
    await deleteAliasRequest(edge.alias, edge.canonical);
    aliasNotice.value = `已删掉「${edge.alias} → ${edge.canonical}」：这两个名字重新是两个实体，证据边没动。`;
    await loadAliases();
  } catch (error) {
    aliasesError.value = `这条别名没能删除：${(error as Error).message}`;
  } finally {
    aliasDeleting.value = '';
  }
}

async function clearAliasTable(): Promise<void> {
  if (!window.confirm('清空全部别名？实体与证据边不受影响，但合并结果会全部消失，只能重跑实体消解恢复（重跑会按同一批阈值重新猜一遍）。')) return;
  aliasesBusy.value = true;
  aliasesError.value = '';
  aliasNotice.value = '';
  try {
    const result = await clearAllAliasesRequest();
    aliasNotice.value = `已清空 ${result.deleted} 条别名。`;
    await loadAliases();
  } catch (error) {
    aliasesError.value = `别名没能清空：${(error as Error).message}`;
  } finally {
    aliasesBusy.value = false;
  }
}

function formatStamp(value: string): string {
  const when = new Date(value);
  if (Number.isNaN(when.getTime())) return '—';
  return when.toLocaleString('zh-CN', { hour12: false });
}

const exporting = ref(false);
// 必须由 Vue 管住选中值：把占位项写成 disabled 时，浏览器会把第一个可用项
// （PlantUML）当成初始选中，用户第一次点它就等于"值没变"，change 不触发，
// 表现为点了没反应。
const exportChoice = ref<ExportFormat | ''>('');

/**
 * 浏览器端落盘：四种导出都是纯文本（JSON 也是），不需要后端再跑一趟。
 *
 * 统一用 `text/plain` 而不是给 JSON 单独一个 `application/json`：浏览器只按
 * 扩展名决定保存的文件名与打开方式，MIME 在这里不影响写盘结果，而多一个分支
 * 就多一处"两种格式走了不同代码路径"的机会。
 */
function saveTextFile(filename: string, text: string): void {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

async function onExport(): Promise<void> {
  const format = exportChoice.value;
  // 复位，否则连着导同一种格式时 change 不会再次触发
  exportChoice.value = '';
  const content = store.content;
  if (!content || !format) return;

  exporting.value = true;
  try {
    const text = await exportMindMap(format, content);
    saveTextFile(exportFilename(format, content.title), text);
    store.status = `已导出 ${exportLabel(format)}（${content.title}）`;
    flash('ok', store.status);
  } catch (error) {
    const message = error instanceof ApiError ? error.message : '导出失败，请重试。';
    store.status = message;
    flash('error', `导出失败：${message}`);
  } finally {
    exporting.value = false;
  }
}

function newMap() {
  if (!confirmDiscard('新建导图')) return;
  store.newMap();
}

async function openSaved(id: string) {
  if (!confirmDiscard('打开另一张导图')) return;
  await store.openMap(id);
}

/**
 * **删除一张已保存的导图**。列表里点「删除」只是进入确认态，真正发请求的是这里。
 *
 * 为什么不用 `window.confirm`（`confirmDiscard` 那一套）：这个动作不可恢复，而
 * 系统模态在 Electron / PWA 里样式不受控、在测试里要 stub、并且**说不出"会连什么
 * 一起删"**。两段式按钮能把理由写在按钮旁边，而且"点了第一次"这件事本身是可见的
 * 状态（材料列表里的「彻底删除」用的是同一套交互）。
 */
const pendingDeleteId = ref<string | null>(null);

async function deleteSaved(id: string) {
  const ok = await store.deleteMap(id);
  // 失败时保留确认态？不——`store.deleteMap` 已经把原因写进 `status`，再留着一个
  // 红色按钮等于催他再点一次同样的失败。
  if (ok) pendingDeleteId.value = null;
}

/** 这一行是不是"当前打开的那张图"：删除时会连它上面的未保存修改一起丢掉。 */
function isOpenMap(id: string): boolean {
  return store.mapId === id;
}

function formatTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString('zh-CN', { hour12: false });
}


/** Ctrl/Cmd+S 保存。画布那边的键盘处理也会收到事件，但 S 不在它的分支里，不冲突。 */
function onKeydown(event: KeyboardEvent) {
  // 知识库抽屉是模态的：Esc 先关它，Tab 也不能跑到背后的画布上。
  if (libraryOpen.value) {
    if (event.key === 'Escape') {
      event.preventDefault();
      closeLibrary();
      return;
    }
    if (event.key === 'Tab' && trapLibraryFocus(event)) return;
  }
  // 模型设置是带遮罩的模态浮层：Esc 关它，跟知识库抽屉同一档（它上面不该再压别的东西）。
  if (settingsOpen.value && event.key === 'Escape') {
    event.preventDefault();
    settingsOpen.value = false;
    return;
  }
  // 偏好浮层盖在抽屉左上角，Esc 关它是这类浮层的默认预期（它不带遮罩，也没有确定按钮）。
  if (prefsOpen.value && event.key === 'Escape') {
    event.preventDefault();
    prefsOpen.value = false;
    return;
  }
  // 左抽屉（`☰`）是覆盖层，不是模态：Esc 收起它。
  if (historyOpen.value && event.key === 'Escape') {
    event.preventDefault();
    historyOpen.value = false;
    return;
  }
  // 右抽屉是覆盖层，不是模态：画布仍然可点，所以只见 Esc 这一条"退出"手势要接住。
  if (sideOpen.value && event.key === 'Escape') {
    event.preventDefault();
    closeSide();
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
    event.preventDefault();
    saveNow();
  }
}

/** 有未保存的修改时，关标签页/刷新要弹浏览器自己的确认框。 */
function onBeforeUnload(event: BeforeUnloadEvent) {
  // 除了脏标记，还要看有没有"输入到一半"的文字：标题草稿和节点草稿都不改
  // content，dirty 是 false，但那些字一样会丢。
  const hasDraft = store.editingId !== null || store.titleDraft !== null;
  if (!store.dirty && !hasDraft) return;
  event.preventDefault();
  event.returnValue = '';
}

onMounted(() => {
  window.addEventListener('keydown', onKeydown);
  window.addEventListener('beforeunload', onBeforeUnload);
  // 启动即拉偏好：生成面板的两个下拉要在用户动手之前就填好，否则第一次生成总是
  // "没带偏好"的那一次——而那正是最该被记住偏好的那一次。
  void prefs.load();
  void loadVersion();
});

/**
 * 后端版本号，显示在标题旁。
 *
 * 拉不到就留空：版本号是装潢，不该因为它让页面多一个错误提示——用户真正
 * 关心的是"域名上跑的是不是刚部署的那份"，而后端不可用时页面本来也做不了事。
 */
async function loadVersion() {
  try {
    // 有些测试替身会回一个不带 version 的对象，这里统一收敛成字符串：
    // 徽章是装潢，不能因为字段缺失把整个页面带崩。
    appVersion.value = String((await getHealth())?.version ?? '');
  } catch {
    appVersion.value = '';
  }
}

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown);
  window.removeEventListener('beforeunload', onBeforeUnload);
  // 计时器必须跟着组件走：它是**展示**用的，没有组件可展示时继续跑只是泄漏。
  if (noticeTimer) clearTimeout(noticeTimer);
});

/** 标题用 change 提交（失焦或回车时），不在每次按键上都记一步撤销。 */
function commitTitle(event: Event) {
  store.renameTitle((event.target as HTMLInputElement).value);
}

/**
 * 标题框走 `:value` + `@change`，打字期间 content 不会变。把草稿同步给 store，
 * 上传生成完成时才知道"这里还有没提交的输入"，否则标题会被静默换掉。
 */
function onTitleInput(event: Event) {
  store.setTitleDraft((event.target as HTMLInputElement).value);
}

async function copyBody() {
  const node = selected.value?.node;
  if (!node) return;
  try {
    await navigator.clipboard.writeText(displayBody(node));
    copied.value = '已复制';
  } catch {
    // 非 HTTPS 或浏览器拒绝剪贴板权限时只能提示，不能假装成功。
    copied.value = '复制失败，请手动选择文字';
  }
  setTimeout(() => {
    copied.value = '';
  }, 2000);
}

// ---------------------------------------------------- 侧栏收窄与底部状态行
//
// 右栏原来是常驻 320px 的 flex 兄弟节点，它和左侧材料区一起把画布夹在中间，而画布
// 上还压着一个 400px 的生成框。可那块宽度上的四样东西使用频率完全不同——偏好一次
// 设置、节点详情按需、原文本来就是抽屉、模型协作只在生成那几十秒里才有意义。所以：
//
//   - 常驻宽度只留**一行**（画布底部）：进度 / 结果 / 异常；
//   - 偏好进顶栏齿轮的浮层；
//   - 节点详情与改动明细进**覆盖式**右抽屉（沿用原文抽屉那套 `fixed inset-y-0 right-0`）。
//
// **抽屉开合不改画布容器尺寸**：它是 `fixed` 覆盖层，不是参与 flex 布局的兄弟节点。
// 这一点是刻意的——画布变宽不会自动 `fitView`（适配只在初始化、节点数/方向变化时
// 按条件触发），所以"推挤式"的抽屉恰逢生成时，反而会在节点增长那一刻把视图拽一下。

/** 右抽屉开着没有。默认关：画布默认拿到全部剩余宽度。 */
const sideOpen = ref(false);

/**
 * 抽屉里「本次生成明细」那一块展开没有。
 *
 * 默认折叠：抽屉打开时用户多半是来看节点详情的，改动清单挡在上面就成了新的遮挡。
 * 状态行上的「明细」按钮是它的快捷入口——那里点一下，抽屉打开并且这一块展开。
 */
const agentDetailOpen = ref(false);

/** `☰` 打开的左抽屉（历史导图 + 原来的顶栏操作）。 */
const historyOpen = ref(false);

/**
 * 偏好浮层的开合。
 *
 * 浮层本身搬进了 `HistoryDrawer`（齿轮在那儿），这里只留一位状态：Esc 与"点外面关掉"
 * 由 `App` 统一管——键盘监听与 pointerdown 捕获都挂在 window/document 上，写在抽屉里
 * 会让它自己也有两份。
 */
const prefsOpen = ref(false);

/**
 * 模型设置浮层的开合。
 *
 * 它跟偏好浮层不是同一类东西：偏好是"这张图长什么样"，模型设置是"这台后端连哪个
 * 模型服务"。所以入口在顶栏（`⚙`）而不是抽屉里——换密钥常常是"现在生成失败了，
 * 我去看一下"，那时候顺手打开抽屉再找一层反而是绕路。
 *
 * 面板自己带遮罩并管自己的关闭，这里只留一位状态：`Esc` 与「点遮罩」都要能关，
 * 而键盘监听整栋房子只有 `App` 这一处（见 `onKeydown`）。
 */
const settingsOpen = ref(false);

function toggleSide(): void {
  sideOpen.value = !sideOpen.value;
}

function closeSide(): void {
  sideOpen.value = false;
}

/** 状态行上的「明细」：抽屉打开并直接展开明细，省掉两次点击。 */
function openAgentDetail(): void {
  agentDetailOpen.value = true;
  sideOpen.value = true;
}

function toggleHistory(): void {
  historyOpen.value = !historyOpen.value;
  if (!historyOpen.value) prefsOpen.value = false;
  // 打开就顺手拉一次列表：抽屉里的"历史导图"不该要求用户先点一次「打开」才出现。
  // 注意用 `loadMaps()` 而不是 `openList()`——后者会同时把选择模态弹出来，正好盖住抽屉。
  else void store.loadMaps();
}

/** 抽屉里那个「打开已保存的导图…」：打开原来的列表模态（选择更清楚，带版本与图型）。 */
function openHistoryList(): void {
  void store.openList();
}

/**
 * 偏好写失败要主动说一句。
 *
 * 面板收进抽屉之后，"保存失败"那一行不再常驻——不主动提示的话，用户点了个勾、
 * 勾自己弹回原样、然后什么都没有发生。「偏好建议与保存错误要有提醒」就是这条。
 */
watch(() => prefs.error, message => {
  if (message) flash('error', message);
});

/**
 * 点了节点上的引用角标：把右抽屉打开，露出这个节点的引用清单。
 *
 * 抽屉原本只在"点了抽屉开关"或"选中节点时已经在开着"的情况下可见。角标是
 * 主动动作（"我要看这个节点的依据"），把它接成"点开一个本来就藏着的东西"
 * 会让用户以为没反应——所以这一次必须自己把面板打开。
 *
 * 用计数器而不是监听选中：点画布上的节点只该换详情，不该把抽屉硬推出来。
 */
watch(() => store.refsTick, () => {
  sideOpen.value = true;
});

// ---- 新外壳的三件事：历史 / 新对话 / 消息流 ----------------------------------

/** 当前图的来历（原来顶栏那枚徽章，现在在抽屉的状态区里）。 */
const modeLabel = computed(() => {
  switch (store.mode) {
    case 'demo': return '演示模式（非 AI 生成）';
    case 'model': return '模型生成';
    case 'saved': return '已保存导图';
    case 'import': return '导入';
    default: return '未生成';
  }
});

const modeTone = computed(() => {
  switch (store.mode) {
    case 'demo': return 'bg-amber-100 text-amber-700';
    case 'model': return 'bg-emerald-100 text-emerald-700';
    case 'saved': return 'bg-slate-100 text-slate-600';
    case 'import': return 'bg-sky-100 text-sky-700';
    default: return 'bg-slate-100 text-slate-500';
  }
});

/** 底部对话框：叫回它、预填一句话（选中节点点「改」走这条）。 */
const dock = ref<{
  openComposer: (prefill?: string) => void;
  openMaterial: () => void;
  takeDraft: () => string;
} | null>(null);

function onNodeEdit(label: string): void {
  dock.value?.openComposer(`关于「${label}」…`);
}

/**
 * 悬浮球里的「继续说一句」：把对话框叫回来并关掉消息面板。
 *
 * 这是"生成完对话框淡出"之后的主要回程入口。没有它，用户想接着改一句就得先去画布
 * 上选个节点（那是"改这一支"），或者点 `+`（那会新开一轮、丢掉当前这张图）。
 */
function onComposeFromFab(): void {
  chat.closePanel();
  dock.value?.openComposer();
}

/**
 * 悬浮球里某条用户消息的「改这句」：把原句填回对话框。
 *
 * 这是自评里那条顾虑的落地：对话框淡出之后，用户想"改一个词重发"的成本变成
 * 一次点击——不用先翻历史再重打一遍。
 */
function onReuseFromFab(text: string): void {
  chat.closePanel();
  dock.value?.openComposer(text);
}

/**
 * 用户发出去一句话：立刻记进时间线。
 *
 * 记的是**发出去的那句**（`goal`），不是输入框里此刻的内容——对话框随后会收成进度条
 * 并清空，用户回头要看的正是他刚说的话。
 */
function onIntentSubmitted(goal: string): void {
  chat.said(goal);
}

/**
 * 一轮跑完：把模型的答复写进消息流。
 *
 * 它承担的正是"生成完对话框就淡出"之后的信息出口：结果好不好、改了几处、走的哪个
 * 模型、想了什么——全在这里，不用去翻抽屉。
 */
watch(
  () => store.agentRunning,
  (running, was) => {
    if (!was || running) return;
    const summary = store.generatedSummary;
    if (store.agentError) {
      chat.replied({
        text: `这次没成：${store.agentError}`,
        kind: 'error',
        model: store.runModel,
        servedModel: store.runServedModel,
        reasoning: store.lastReasoning || undefined,
      });
      return;
    }
    if (!summary) return;
    const partial = store.agentWarnings.find(item => item.kind === 'partial');
    const bits = [`已落到画布：${summary.nodes} 个节点`];
    if (summary.changes != null) bits.push(`改了 ${summary.changes} 处`);
    if (partial) bits.push(`（部分完成：${partial.message}）`);
    chat.replied({
      text: bits.join(' · '),
      kind: partial ? 'partial' : 'done',
      model: store.runModel,
      servedModel: store.runServedModel,
      reasoning: store.lastReasoning || undefined,
      nodes: summary.nodes,
      changes: summary.changes,
    });
  },
);

/**
 * `+`：新的一轮。
 *
 * 四件事同时发生，缺一件都会让"新对话"看起来没生效：清空画布（`newMap`）、清空
 * 时间线、把材料与草稿一起清掉、把对话框叫回来放在输入位置。
 *
 * **材料为什么要清**：它和主题句一样属于"这一轮的工作台"。留着它的后果不是"多带
 * 几份材料"，而是下一次生成会拿上一轮的材料当依据——用户点的是"新对话"，画布空着、
 * 时间线空着，只有材料在暗处延续，他没有任何办法看出来。
 *
 * 但材料可能是刚传完、刚解析好几份文件，所以清之前留一句**带撤销**的提示：
 * 只写"已清空 3 份材料"而不给回去的路，等于让用户确认一次损失再目送它消失。
 */
function newConversation(): void {
  if (!confirmDiscard('开一个新对话')) return;
  const materials = sources.items.length;
  const hasMaterialDraft = !!sources.draftText.trim();
  // 先取走输入框里的那句话：`clear()` 之前要先知道它有没有内容，撤销时要原样放回。
  const typed = dock.value?.takeDraft() ?? '';
  // 材料与粘贴框正文是一份整体（含 `files`，失败条目靠它才能重试），一次快照。
  const stash = materials || hasMaterialDraft ? sources.snapshot() : null;

  sources.clear();
  sources.draftText = '';
  store.newMap();
  chat.clear();
  historyOpen.value = false;
  dock.value?.openComposer();

  const what: string[] = [];
  if (materials) what.push(`${materials} 份材料`);
  if (hasMaterialDraft) what.push('粘贴框里没添加的正文');
  if (typed.trim()) what.push('没发出去的那句话');
  if (!what.length) return;
  flash('ok', `已清空 ${what.join('、')}`, {
    label: '撤销',
    run: () => {
      if (stash) sources.restore(stash);
      if (typed) dock.value?.openComposer(typed);
    },
  });
}

/** 抽屉里选一个导出格式：先把值写回，再走原来那条导出。 */
function onExportFromDrawer(format: ExportFormat): void {
  exportChoice.value = format;
  void onExport();
}

const agentHasDetail = computed(() => agentDetailVisible(store));
</script>

<template>
  <div class="flex h-full w-full flex-col bg-slate-50 text-slate-900">
    <!--
      瘦顶栏：**只有三件东西**。左边 `☰` 开历史与全部操作，中间是当前图名（就地改名），
      右边 `+` 开新的一轮。

      原来那一排（保存 / 演示 / 知识库 / 打开 / 新建 / 导入 / 导出 / 偏好 / 节点详情 /
      版本号 / 来历徽章）一件没少，全搬进了 `HistoryDrawer`——每一件都还能找到，只是
      不再天天占着一条横边。顶栏的高度因此从 ~44px 降到 48px 以内，画布多出一整条宽。
    -->
    <header class="flex h-12 shrink-0 items-center justify-between gap-2 border-b border-slate-200 bg-white px-3">
      <div class="flex min-w-0 flex-1 items-center gap-2">
        <button
          type="button"
          data-test="history-toggle"
          class="shrink-0 rounded-md border border-slate-200 px-2 py-1 text-slate-600 hover:bg-slate-50"
          :aria-expanded="historyOpen"
          aria-controls="history-drawer"
          aria-label="历史导图与更多操作"
          title="历史导图与更多操作"
          @click="toggleHistory"
        >
          <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
            <path d="M4 6h16M4 12h16M4 18h16" stroke-linecap="round" />
          </svg>
        </button>
        <!-- 标题就是那个"当前图名"，点进去直接改名（#title 一直是同一个输入框）。 -->
        <input
          id="title"
          :value="store.content?.title ?? ''"
          :placeholder="store.content ? '' : '新对话'"
          :disabled="!store.content"
          class="min-w-0 flex-1 truncate rounded border border-transparent bg-transparent px-1 py-0.5 text-sm font-medium text-slate-800 outline-none hover:border-slate-200 focus:border-slate-400 disabled:text-slate-400"
          aria-label="导图标题"
          @input="onTitleInput"
          @change="commitTitle"
          @blur="store.setTitleDraft(null)"
        />
      </div>
      <!--
        模型设置入口：一个齿轮。
        图标不带文字，顶栏的可见文字仍是"☰ / 图名 / +"——顶栏不能被一格格按钮撑长
        这个取舍没变，见 `frontend/tests/shell-actions.spec.ts` 里按 `data-test` 数
        "顶栏有哪些动作"的那条用例。
      -->
      <button
        type="button"
        data-test="open-settings"
        class="shrink-0 rounded-md border border-slate-200 px-2 py-1 text-slate-600 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-slate-400"
        :aria-expanded="settingsOpen"
        aria-haspopup="dialog"
        aria-label="模型设置"
        title="模型设置（服务地址 / 密钥 / 模型名，改完立即生效）"
        @click="settingsOpen = !settingsOpen"
      >
        <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.11a1.7 1.7 0 0 0-1.11-1.55 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.11a1.7 1.7 0 0 0 1.55-1.11 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34H9a1.7 1.7 0 0 0 1-1.55V3a2 2 0 1 1 4 0v.11a1.7 1.7 0 0 0 1 1.55 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87V9a1.7 1.7 0 0 0 1.55 1H21a2 2 0 1 1 0 4h-.11a1.7 1.7 0 0 0-1.49 1z" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </button>
      <button
        type="button"
        data-test="new-run"
        class="shrink-0 rounded-md bg-slate-900 px-2.5 py-1 text-xs text-white hover:bg-slate-800"
        aria-label="新对话"
        title="新对话（清空画布，开新的一轮）"
        @click="newConversation"
      >+</button>
    </header>

    <main class="relative flex min-h-0 flex-1">

      <section class="relative min-w-0 flex-1">
        <div class="absolute left-4 top-4 z-10 flex gap-1 rounded-md border border-slate-200 bg-white p-1 shadow-sm">
          <button
            v-for="item in directions"
            :key="item.value"
            type="button"
            class="rounded px-2 py-1 text-xs"
            :class="store.direction === item.value ? 'bg-slate-900 text-white' : 'text-slate-600 hover:bg-slate-100'"
            @click="store.setDirection(item.value)"
          >
            {{ item.label }}
          </button>
          <span class="mx-1 w-px bg-slate-200" />
          <button
            type="button"
            class="rounded px-2 py-1 text-xs text-slate-600 hover:bg-slate-100 disabled:opacity-40 disabled:hover:bg-transparent"
            :disabled="!store.canUndo"
            title="撤销（Ctrl+Z）"
            @click="store.undo()"
          >
            撤销
          </button>
          <button
            type="button"
            class="rounded px-2 py-1 text-xs text-slate-600 hover:bg-slate-100 disabled:opacity-40 disabled:hover:bg-transparent"
            :disabled="!store.canRedo"
            title="重做（Ctrl+Shift+Z）"
            @click="store.redo()"
          >
            重做
          </button>
        </div>
        <MindMapCanvas />

        <!--
          选中节点旁浮出的工具条（`[改][展开][删][色]`）。它读画布 DOM 定位，
          不改动一个像素的渲染——"点节点要求 AI 修改"这条需求的落点就在这里。
        -->
        <NodeToolbar @edit="onNodeEdit" @expand="expandBranch" />

        <!--
          底部对话框。**生成中**收成一条细进度、**生成完**淡出，只剩画布 + 悬浮球；
          失败 / 有草稿 / 画布还空着时留着（判据在 `ChatDock.shouldKeep`）。
        -->
        <ChatDock ref="dock" @detail="openAgentDetail" @submit="onIntentSubmitted" />

        <!-- 右下角悬浮球：未读角标 + 消息时间线（生成完对话框消失之后的信息出口）。
             `@compose` 把对话框叫回来——那是淡出之后"再说一句"的入口。 -->
        <MessageFab @compose="onComposeFromFab" @reuse="onReuseFromFab" />
      </section>

      <!--
        `☰` 打开的左抽屉：历史导图 + 原来顶栏那一排按钮。
        与右抽屉同一个套路——`absolute` 相对 `<main>`，不盖住顶栏（那个开关必须一直点得到）。
      -->
      <HistoryDrawer
        :open="historyOpen"
        :confirm-discard="confirmDiscard"
        :exporting="exporting"
        :export-choice="exportChoice"
        :save-hint="saveHint"
        :version-label="versionLabel"
        :mode-label="modeLabel"
        :mode-tone="modeTone"
        :node-count="store.nodeCount"
        :side-open="sideOpen"
        :samples="samples"
        @close="historyOpen = false"
        @save="saveNow"
        @new-map="newMap"
        @open-list="openHistoryList"
        @open-map="openSaved"
        @demo="openDemoPresets"
        @library="openLibrary"
        @export="onExportFromDrawer"
        @update-export="value => exportChoice = value"
        @load-sample="loadSample"
        @toggle-side="toggleSide"
        @prefs-open="value => prefsOpen = value"
      />

      <!--
        右抽屉：**覆盖式**，不参与主区的 flex 布局。

        它取代了原来那个常驻 320px 的右栏。四块内容没有变少，只是改成了"要看的时候
        才占屏幕"，而且占的是盖在画布上的那一层——**画布宽度一点都没变**。

        刻意不设 `role="dialog"` / `aria-modal`：它不是模态。画布仍然可点、可拖、可以
        继续选节点（选完这里的详情跟着变），这正是"覆盖而不是推挤"的好处，也是它与
        原文抽屉、知识库抽屉的区别——那两个是"看图之外的另一件事"。

        `absolute` 挂着而不是 `fixed`：它相对的是 `<main>`，于是**不盖住顶栏**——
        那个开关按钮必须始终点得到（"点开、再点收起"，不能被自己打开的东西挡住）。
      -->
      <aside
        v-show="sideOpen"
        id="side-panel"
        data-test="side-panel"
        aria-label="节点详情与本次改动"
        class="absolute inset-y-0 right-0 z-30 flex w-[360px] max-w-full flex-col border-l border-slate-200 bg-white shadow-2xl"
      >
        <div class="flex items-center justify-between border-b border-slate-200 px-4 py-2">
          <p class="text-xs font-semibold text-slate-700">节点详情与本次改动</p>
          <button
            type="button"
            data-test="side-close"
            class="text-xs text-slate-500 hover:text-slate-900"
            title="关闭（Esc）"
            @click="closeSide"
          >关闭</button>
        </div>

        <div class="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4">
          <!--
            本次生成的明细（工具进度、改动清单、诊断、预览、冲突裁决）。

            默认折叠：打开抽屉时多半是来看节点详情的，一份改动清单挡在上面就成了新的
            遮挡。状态行上的「明细」按钮会把它撑开——那里是"我关心这次改了什么"的入口。
          -->
          <details
            v-if="agentHasDetail"
            :open="agentDetailOpen"
            data-test="agent-detail"
            class="text-xs"
            @toggle="agentDetailOpen = ($event.target as HTMLDetailsElement).open"
          >
            <summary class="cursor-pointer rounded-md border border-slate-200 bg-slate-50 px-2 py-1 font-medium text-slate-600">
              本次生成明细
            </summary>
            <div class="mt-2"><AgentPanel /></div>
          </details>

          <!--
            结构检查（`/optimization/check` + `/suggest`）：诊断逐条 + 候选建议。

            **放在右抽屉里，不给它自己的浮层**。上一轮刚解决的是"诊断/建议不许挡住
            画布"：抽屉是覆盖式的（`absolute` 挂在 `<main>` 上，不参与主区 flex），
            它开合时画布宽度一个像素都没变，也不会触发一次自动 fitView。

            它排在上面那块「本次生成明细」之后：明细是"刚才那 25 秒发生了什么"的日志，
            结构检查是"这张图现在有什么问题"的结论——后者常常在生成之后很久才被点开，
            所以不该把它压在一份长日志的下方，也不该顶在节点详情前面。

            自己判可见性（没跑过、没有发现、没有错误时整块不渲染），这里不再套一层
            `v-if`——那会让"有没有东西可说"这件事在 App 与组件里各写一遍。
          -->
          <StructureCheck />

          <template v-if="selected">
          <p class="text-xs text-slate-400">{{ pathText }}</p>
          <!-- 标题：与画布上那颗节点是同一段文字（双击节点改的就是它） -->
          <h2 class="text-sm font-semibold">
            {{ selected.node.label.trim() }}
          </h2>

          <!--
            详情分区（只读）：生成产出的正文。
            `detail` 是模型写的，界面不提供编辑入口——要改内容就重新生成，
            或者写到下面的「我的备注」里。两个字段分开存，正是为了这两种意图
            不会互相覆盖。
          -->
          <section class="flex flex-col gap-1" aria-label="节点详情">
            <h3 class="text-xs font-medium text-slate-500">详情</h3>
            <p class="whitespace-pre-wrap break-words rounded-md bg-slate-50 px-2 py-1.5 text-sm leading-relaxed text-slate-700">{{ bodyText }}</p>
            <p class="text-xs text-slate-400">{{ BODY_SOURCE_NOTE[bodyFrom] }}</p>
          </section>

          <!--
            按需展开：初次只出 4 层，往下的内容由用户点哪个节点就展开哪个。
            走的是与生成同一条 `/api/agent/runs/stream`（intent=expand），
            结果同样直接落画布——版本检查与冲突裁决一步都没少。
          -->
          <section class="flex flex-col gap-1" aria-label="展开这一支">
            <button
              type="button"
              class="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-40 disabled:hover:bg-transparent"
              :disabled="expandState.disabled"
              @click="expandBranch"
            >
              {{ expandState.label }}
            </button>
            <p class="text-xs leading-relaxed text-slate-400">{{ expandState.hint }}</p>
          </section>

          <!--
            样式：语义色 + 图标。两个值都可选、缺省为空（"默认"）。
            颜色只给调色板里的键——不是取色器。理由与契约那边一致：任意 hex 会
            得到一堆脏色，而且换主题（暗色/亮色）时无法自动适配；键是语义，
            色值在 CSS 变量里（`styles.css`），换主题只改那一处。
            图标是**一个 emoji**：前端没有图标库，为一个纯装饰字段新增依赖不划算。
          -->
          <section class="flex flex-col gap-1" aria-label="节点样式">
            <h3 class="text-xs font-medium text-slate-500">样式</h3>
            <div class="flex items-center gap-2">
              <select
                aria-label="节点颜色"
                class="min-w-0 flex-1 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-xs text-slate-700"
                :value="selected.node.color ?? ''"
                @change="changeColor"
              >
                <option value="">默认（无色）</option>
                <option v-for="key in NODE_COLORS" :key="key" :value="key">
                  {{ NODE_COLOR_LABELS[key] }}
                </option>
              </select>
              <input
                aria-label="节点图标"
                class="w-16 rounded-md border border-slate-300 px-2 py-1.5 text-center text-xs text-slate-700"
                :value="selected.node.icon ?? ''"
                :maxlength="16"
                placeholder="🎯"
                title="一个 emoji；清空表示去掉图标"
                @change="changeIcon"
              />
            </div>
            <p class="text-[11px] leading-relaxed text-slate-400">
              颜色只收预设的 8 个语义色（换主题时自动适配）；图标是一个 emoji。生成时只有一级分支会带这两个值。
            </p>
            <!--
              继承规则必须写出来，否则这里会成为一处"看起来坏了"的地方：给一个二级
              节点选「默认（无色）」之后它**仍然是带色的**（继承上级），用户会以为
              选择没生效。说清"下级选默认 = 继承"比让那条下拉自己解释要省事。
            -->
            <p class="text-[11px] leading-relaxed text-slate-400">
              一级分支定了颜色，整支都跟着用这个色；下级选「默认」表示继承上级。
            </p>
          </section>

          <!-- 备注框不再用 `flex-1` 争抢高度：抽屉的滚动容器里没有"剩余高度"可分，
               那会让滚动区忽高忽低；固定 `min-h-24` + 可拖拽更可预测。 -->
          <label class="flex flex-col gap-1">
            <span class="text-xs text-slate-400">
              我的备注 {{ noteLength }} / 2000
              <span v-if="!(selected.node.note ?? '').trim()">· 现在是空的，不影响详情与导出</span>
            </span>
            <textarea
              v-model="noteDraft"
              aria-label="我的备注"
              class="min-h-24 flex-1 resize-y rounded-md border border-slate-200 px-2 py-1.5 text-sm leading-relaxed text-slate-700 focus:border-slate-400 focus:outline-none"
              maxlength="2000"
              placeholder="写你自己的备忘；这里的字不会被重新生成覆盖"
              @blur="commitNoteDraft"
              @keydown.esc="resetNoteDraft"
            />
          </label>

          <section v-if="selectedRefs.length" class="flex flex-col gap-2 border-t border-slate-200 pt-3">
            <h3 class="text-xs font-medium text-slate-500">来源引用（{{ selectedRefs.length }}）</h3>
            <ul class="flex flex-col gap-1.5">
              <li v-for="(item, index) in selectedRefs" :key="`${item.ref.source_id}-${index}`">
                <div class="rounded-md border border-slate-200 px-2 py-1.5 hover:border-slate-300 hover:bg-slate-50">
                  <!--
                    有原文地址的来源（外部检索的维基条目 / arXiv 论文）名字就是链接：
                    用户读完这句可以直接去核对出处，而不是只能在抽屉里看一段没有上下文的
                    摘录。没有地址时保持原来的样子（点开抽屉），不给一个点不动的假链接。
                  -->
                  <a
                    v-if="item.url"
                    :href="item.url"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="block truncate text-xs text-sky-700 underline decoration-sky-300 underline-offset-2 hover:text-sky-900"
                    :title="`在新窗口打开原文：${item.url}`"
                  >
                    {{ item.name }}<span v-if="item.where" class="text-slate-500"> · {{ item.where }}</span>
                  </a>
                  <button
                    v-else
                    type="button"
                    class="w-full text-left"
                    @click="activeRef = item"
                  >
                    <span class="block truncate text-xs text-slate-600">
                      {{ item.name }}<span v-if="item.where"> · {{ item.where }}</span>
                    </span>
                  </button>
                  <button
                    type="button"
                    class="mt-0.5 block w-full text-left"
                    @click="activeRef = item"
                  >
                    <span class="line-clamp-2 block text-xs leading-relaxed text-slate-500">
                      {{ item.quote || '（这条引用没有摘录）' }}
                    </span>
                  </button>
                </div>
              </li>
            </ul>
          </section>
          <!--
            **没有引用的节点：不许写成一次失败。**

            一次生成里总有节点拿不到引文（实测薄批 78–80%、厚批 86% 的覆盖率）。
            只写一句"这个节点没有来源引用"，用户看到的是一枚**洞**：是不是引用功能
            坏了、是不是这条点不开、是不是漏生成了一步。而这恰恰不是数据层能回答的
            问题——`MindNode` 契约里没有"这个节点是手动的还是生成的"，只有
            `label` / `detail` / `source_refs`，所以界面无法断言原因。

            能做的是**把口径摆出来**：告诉用户这张图本来就有多少节点没有引文。
            这样"这个节点没依据"读起来才是一张图的属性，而不是一个坏掉的功能。
          -->
          <section
            v-else
            class="flex flex-col gap-1 border-t border-slate-200 pt-3"
            data-test="ref-empty"
          >
            <p class="text-xs text-slate-500">这个节点没有原文引用。</p>
            <p v-if="citationSummary" class="text-xs leading-relaxed text-slate-400">
              {{ citationSummary }}
            </p>
          </section>

          <!--
            附加关系（流程图的条件分支 / 汇合 / 回退）。

            这一段是**唯一**的手工补边入口：模型只在 flowchart 生成时给边，
            而它给错的那些（比如画到了错的出口、或者本该是顺序却标成了回退）
            没有别的地方能改。删边同样在这里——画布上点线不做选中，因为节点
            已经有选中态了，两套选中语义混在一起会让「Delete 删什么」变得不可预测。

            **两层分开列**：`FlowContent.edges`（阶段之间，端点用阶段 id）与
            `FlowStage.edges`（阶段内部，端点用该阶段的节点 id）是两个数组、两个
            命名空间。合成一列的话，"审不审 → 退回（否）"与"提交申请 → 付款"长得
            一样，而它们在这张图里是两件不同的事：一个是主干走向，一个是某一步上的
            分叉。删除按钮两边都有——只列不删等于把用户引进一个死胡同。

            `v-if` 的依据是产物自己的 `diagram_type`（见 `showEdgeSection`）：
            对思维导图与鱼骨图来说，"没有附加关系"不是缺陷而是定义。
          -->
          <section
            v-if="showEdgeSection"
            class="flex flex-col gap-2 border-t border-slate-200 pt-3"
            aria-label="附加关系"
          >
            <h3 class="text-xs font-medium text-slate-500">
              附加关系（{{ selectedEdgeTotal }}）
            </h3>
            <!--
              两组分开列。第一组是**阶段之间**的跳转（端点存在阶段 id 上），第二组
              是**阶段内部**的判定出边（端点存在该阶段的节点 id 上）。合成一列的话，
              "审不审 → 退回（否）"与"提交申请 → 付款"长得一模一样，而它们在这张图里
              是两件不同的事：一个是主干的走向，一个是某一步上的分叉。
            -->
            <div v-if="selectedEdgeRows.length" class="flex flex-col gap-1">
              <p v-if="selectedInnerEdgeRows.length" class="text-[11px] text-slate-400">阶段之间</p>
              <ul class="flex flex-col gap-1">
                <li
                  v-for="row in selectedEdgeRows"
                  :key="row.id"
                  class="flex items-center gap-2 rounded-md border border-slate-200 px-2 py-1"
                  :class="row.kind === 'loop' ? 'border-amber-200 bg-amber-50' : ''"
                >
                  <span class="min-w-0 flex-1 truncate text-xs text-slate-700" :title="row.text">
                    {{ row.text }}
                    <span v-if="row.label" class="text-slate-500">（{{ row.label }}）</span>
                  </span>
                  <button
                    type="button"
                    class="shrink-0 rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-50"
                    :title="`删除这条关系（${row.id}）`"
                    @click="disconnectEdge(row.id)"
                  >
                    删除
                  </button>
                </li>
              </ul>
            </div>
            <div v-if="selectedInnerEdgeRows.length" class="flex flex-col gap-1">
              <p class="text-[11px] text-slate-400">阶段内部（判定出口）</p>
              <ul class="flex flex-col gap-1">
                <li
                  v-for="row in selectedInnerEdgeRows"
                  :key="row.id"
                  class="flex items-center gap-2 rounded-md border border-dashed border-slate-200 px-2 py-1"
                  :class="row.kind === 'loop' ? 'border-amber-200 bg-amber-50' : ''"
                >
                  <span class="min-w-0 flex-1 truncate text-xs text-slate-700" :title="row.text">
                    {{ row.text }}
                    <span v-if="row.label" class="text-slate-500">（{{ row.label }}）</span>
                  </span>
                  <button
                    type="button"
                    class="shrink-0 rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-50"
                    :title="`删除这条关系（${row.id}）`"
                    @click="disconnectEdge(row.id)"
                  >
                    删除
                  </button>
                </li>
              </ul>
            </div>
            <p v-if="!selectedEdgeTotal" class="text-xs text-slate-400">
              这个节点没有附加关系。流程图里的条件分支与回退线都记在这里。
            </p>
            <div class="flex items-center gap-1">
              <select
                v-model="edgeTarget"
                aria-label="关系终点"
                class="min-w-0 flex-1 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-xs text-slate-700"
              >
                <option value="">连到哪个节点…</option>
                <option v-for="item in edgeTargets" :key="item.id" :value="item.id">
                  {{ '·'.repeat(item.depth) }} {{ item.label }}
                </option>
              </select>
              <select
                v-model="edgeKind"
                aria-label="关系类型"
                class="w-24 shrink-0 rounded-md border border-slate-300 bg-white px-1 py-1.5 text-xs text-slate-700"
              >
                <option v-for="kind in EDGE_KINDS" :key="kind" :value="kind">
                  {{ EDGE_KIND_LABELS[kind] }}
                </option>
              </select>
            </div>
            <div class="flex items-center gap-1">
              <input
                v-model="edgeLabel"
                aria-label="关系说明"
                class="min-w-0 flex-1 rounded-md border border-slate-300 px-2 py-1.5 text-xs text-slate-700"
                :maxlength="40"
                placeholder="边上的文字：是 / 否 / 驳回"
              />
              <button
                type="button"
                class="shrink-0 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-40"
                :disabled="!edgeTarget || store.busy"
                @click="connectEdge"
              >
                连一条关系
              </button>
            </div>
            <p class="text-[11px] leading-relaxed text-slate-400">{{ edgeHint }}</p>
          </section>
          <!--
            跨分支关系（`ContentBase.relations`）。与上面「附加关系」**分成两块**，
            理由与那里两层边分开列同源：这是两种不同的东西——附加边是这张图的主干
            走向，关系线是"树表达不了的那部分语义"（依赖 / 因果 / 冲突 / 属于）。

            这一块同时承担一个诊断职责：`why` 一起列出来，读的人不必回到材料就能判断
            这条线对不对。没有它，虚线在画布上只是一根看不出所以然的弧。
          -->
          <section
            v-if="showRelationSection"
            class="flex flex-col gap-2 border-t border-slate-200 pt-3"
            aria-label="跨分支关系"
          >
            <h3 class="text-xs font-medium text-slate-500">
              跨分支关系（{{ selectedRelationRows.length }} / 全图 {{ store.contentRelations.length }}）
            </h3>
            <ul v-if="selectedRelationRows.length" class="flex flex-col gap-1">
              <li
                v-for="row in selectedRelationRows"
                :key="row.id"
                class="flex flex-col gap-0.5 rounded-md border border-dashed border-slate-300 px-2 py-1"
              >
                <div class="flex items-center gap-2">
                  <span
                    class="shrink-0 rounded border border-slate-300 px-1 text-[10px] text-slate-600"
                    :title="row.kind"
                  >
                    {{ row.label }}
                  </span>
                  <span class="min-w-0 flex-1 truncate text-xs text-slate-700" :title="row.text">
                    {{ row.text }}
                  </span>
                  <button
                    type="button"
                    class="shrink-0 rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-50"
                    :title="`删除这条跨分支关系（${row.id}）`"
                    @click="disconnectRelation(row.id)"
                  >
                    删除
                  </button>
                </div>
                <p v-if="row.why" class="text-[11px] leading-relaxed text-slate-500">
                  {{ row.why }}
                </p>
              </li>
            </ul>
            <p v-else class="text-xs text-slate-400">
              这个节点没有跨分支关系。关系线只在**不同一级分支**之间连——同一父节点下的
              关系由树本身表达。
            </p>
          </section>
          <div class="grid grid-cols-2 gap-2">
            <button
              type="button"
              class="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50"
              @click="store.beginEdit(selected.node.id)"
            >
              改文字（F2）
            </button>
            <button
              type="button"
              class="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50"
              @click="store.addChildTo(selected.node.id)"
            >
              加子节点（Tab）
            </button>
            <button
              type="button"
              class="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50"
              @click="store.addSiblingTo(selected.node.id)"
            >
              加兄弟（Enter）
            </button>
            <button
              type="button"
              class="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-40"
              :disabled="store.isContainerRootId(selected.node.id)"
              @click="store.deleteNode(selected.node.id)"
            >
              删除（Delete）
            </button>
          </div>
          <div class="flex items-center gap-2">
            <button
              type="button"
              class="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700"
              @click="copyBody"
            >
              复制全文
            </button>
            <span class="text-xs text-slate-400">{{ copied }}</span>
          </div>
        </template>
        <template v-else>
          <h2 class="text-sm font-semibold">节点详情</h2>
          <p class="text-xs leading-relaxed text-slate-500">
            点击画布上的任意节点查看完整内容与层级；双击节点可以直接改文字。
          </p>
        </template>
        <!-- 本图来源：整图口径的来源清单，与各节点自己的引用互为对照。
             它留在抽屉最底部——它属于"这张图整体"的信息，与节点详情不同层级。 -->
        <details v-if="store.content?.source_usage?.length" class="border-t border-slate-200 pt-3 text-xs text-slate-600" open>
          <summary class="cursor-pointer font-medium">本图来源（{{ store.content.source_usage.length }}）</summary>
          <ul class="mt-2 flex flex-col gap-2">
            <li v-for="(source, index) in store.content.source_usage" :key="source.source_id" class="break-words">
              {{ SOURCE_LABELS[source.kind] }} · {{ source.filename || `来源 ${index + 1}` }} · {{ source.char_count }} 字
              <span
                v-if="store.refIndex.counts[source.source_id]"
                class="text-slate-400"
              >· 被 {{ store.refIndex.counts[source.source_id] }} 个节点引用</span>
              <span v-else class="text-amber-700">· 没有节点引用</span>
            </li>
          </ul>
          <p class="mt-2 leading-relaxed text-slate-400">生成时用到的来源；具体出处见各节点引用，原文未随导图保存。</p>
        </details>
        </div>
      </aside>
    </main>

    <!--
      模型设置浮层（顶栏 `⚙` 打开）。挂在 `<main>` 之外、`fixed` 定位：它不属于画布，
      也不该被任何 `absolute` 的抽屉层级压住。自己带遮罩，开合不影响主区布局。
    -->
    <SettingsPanel v-if="settingsOpen" @close="settingsOpen = false" />

    <!-- 导出等一次性操作的结果浮层；错误停 6 秒，成功 3 秒，带撤销的那条 8 秒。 -->
    <div
      v-if="notice"
      data-test="notice"
      class="fixed bottom-6 left-1/2 z-50 flex max-w-[90vw] -translate-x-1/2 items-center gap-3 rounded-md px-4 py-2 text-sm shadow-lg"
      :class="notice.kind === 'error' ? 'bg-rose-600 text-white' : 'bg-slate-900 text-white'"
      :role="notice.kind === 'error' ? 'alert' : 'status'"
      aria-live="polite"
    >
      <span class="min-w-0">{{ notice.text }}</span>
      <button
        v-if="notice.action"
        type="button"
        data-test="notice-action"
        class="shrink-0 rounded border border-white/40 px-2 py-0.5 text-xs hover:bg-white/10"
        @click="notice.action?.run(); notice = null"
      >{{ notice.action.label }}</button>
    </div>

    <!--
      引用原文抽屉：点引用 → 看那句 → 看上下文（定位到 `start/end` 并高亮）。
      组件自己决定正文从哪来（本次会话解析过的 `Source.text` 优先，否则按
      `source_id` 从知识库取整份），所以这里只负责"打开哪一条"与"关掉"。
    -->
    <SourceContextDrawer
      v-if="activeRef"
      :item="activeRef"
      @close="activeRef = null"
    />

    <!-- 遮罩：点空白处也算关闭。抽屉本身 z-40，盖在它上面 -->
    <div
      v-if="libraryOpen"
      class="fixed inset-0 z-30 bg-slate-900/20"
      aria-hidden="true"
      @click="closeLibrary()"
    />

    <aside
      v-if="libraryOpen"
      ref="libraryPanel"
      class="fixed inset-y-0 right-0 z-40 flex w-[480px] max-w-full flex-col border-l border-slate-200 bg-white shadow-2xl"
      role="dialog"
      aria-modal="true"
      aria-labelledby="library-title"
    >
      <div class="flex items-center justify-between border-b border-slate-100 px-4 py-3">
        <h2 id="library-title" class="text-sm font-semibold">知识库</h2>
        <button
          ref="libraryCloseButton"
          type="button"
          class="text-xs text-slate-500 hover:text-slate-900"
          @click="closeLibrary()"
        >
          关闭
        </button>
      </div>

      <div class="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        <template v-if="libraryPreview">
          <button type="button" class="text-xs text-slate-500 hover:text-slate-900" @click="backToLibrary()">
            ← 回到列表
          </button>
          <h3 class="mt-2 break-words text-sm font-medium text-slate-800">
            {{ libraryPreview.filename || '未命名材料' }}
          </h3>
          <p class="mt-1 text-xs text-slate-400">
            {{ SOURCE_LABELS[libraryPreview.kind] }} · 共 {{ libraryPreview.total_chars }} 字
          </p>
          <pre class="mt-3 whitespace-pre-wrap break-words font-sans text-xs leading-relaxed text-slate-700">{{ libraryPreview.text }}</pre>
          <p
            v-if="libraryPreview.truncated"
            class="mt-3 rounded-md bg-amber-50 p-3 text-xs leading-relaxed text-amber-700"
          >
            这里只显示前 4000 字，全文共 {{ libraryPreview.total_chars }} 字。
          </p>
        </template>

        <!-- 打开中 / 打开失败：停在预览壳里，不把列表顶掉，也不逼用户关掉重开 -->
        <template v-else-if="libraryOpening">
          <button type="button" class="text-xs text-slate-500 hover:text-slate-900" @click="backToLibrary()">
            ← 回到列表
          </button>
          <h3 class="mt-2 break-words text-sm font-medium text-slate-800">
            {{ libraryOpening.filename || '未命名材料' }}
          </h3>
          <p class="mt-1 text-xs text-slate-400">
            {{ SOURCE_LABELS[libraryOpening.kind] }} · {{ librarySize(libraryOpening) }}
          </p>
          <p v-if="libraryPreviewBusy" class="mt-3 text-xs text-slate-400">正在读取正文…</p>
          <div
            v-else-if="libraryPreviewError"
            class="mt-3 rounded-md bg-rose-50 p-3 text-xs leading-relaxed text-rose-700"
          >
            <p>{{ libraryPreviewError }}</p>
            <button
              type="button"
              class="mt-2 underline decoration-rose-300 hover:text-rose-900"
              @click="openLibrarySource(libraryOpening)"
            >
              再试一次
            </button>
          </div>
        </template>

        <template v-else>
          <p v-if="!libraryBusy && !libraryListError" class="text-xs leading-relaxed text-slate-500">
            生成时用到的材料会被收进这里，供以后复用。库里现在有
            <strong class="text-slate-700">{{ libraryTotal }}</strong> 份。
            <!-- 库里超过一页时必须说明"只显示了一部分"，否则用户会以为材料丢了 -->
            <span v-if="libraryTotal > libraryEntries.length">
              这里只显示最近 <strong class="text-slate-700">{{ libraryEntries.length }}</strong> 份。
            </span>
          </p>
          <p v-if="libraryBusy" class="mt-3 text-xs text-slate-400">正在读取…</p>
          <p v-else-if="libraryListError" class="mt-3 rounded-md bg-rose-50 p-3 text-xs leading-relaxed text-rose-700">
            {{ libraryListError }}
          </p>
          <p v-else-if="libraryEntries.length === 0" class="mt-3 text-xs leading-relaxed text-slate-400">
            还是空的。带着材料生成一次导图，用到的材料就会出现在这里。
          </p>
          <ul v-else class="mt-3 divide-y divide-slate-100">
            <li v-for="entry in libraryEntries" :key="entry.source_id" class="flex items-start gap-2 py-2">
              <button
                type="button"
                class="flex min-w-0 flex-1 items-start justify-between gap-3 text-left hover:bg-slate-50"
                @click="openLibrarySource(entry)"
              >
                <span class="min-w-0">
                  <span class="block break-words text-sm text-slate-800">{{ entry.filename || '未命名材料' }}</span>
                  <span
                    class="mt-0.5 block text-xs text-slate-400"
                    :title="`最近使用：${formatStamp(entry.last_used_at)}；收录：${formatStamp(entry.created_at)}`"
                  >
                    {{ SOURCE_LABELS[entry.kind] }} · {{ librarySize(entry) }} · 最近使用 {{ formatStamp(entry.last_used_at) }}
                  </span>
                </span>
                <span
                  class="shrink-0 rounded px-1.5 py-0.5 text-[11px]"
                  :class="entry.cited_by > 0 ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500'"
                >
                  {{ citationLabel(entry.cited_by) }}
                </span>
              </button>
              <!--
                删除单独一个按钮：它是"传错了材料"唯一的出口。被引用时先弹一次确认
                （正文是证据的最终依据，删掉不可逆），未引用则直接删。
              -->
              <button
                type="button"
                class="mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[11px] text-rose-600 hover:bg-rose-50 hover:text-rose-800 disabled:opacity-50"
                :disabled="libraryDeleting === entry.source_id"
                :title="entry.cited_by > 0 ? `被 ${entry.cited_by} 张导图引用：删除前会让你确认` : '这份材料还没有被任何导图引用'"
                @click="removeLibrarySource(entry)"
              >
                {{ libraryDeleting === entry.source_id ? '删除中…' : '删除' }}
              </button>
            </li>
          </ul>
          <p v-if="libraryDeleteNotice" class="mt-3 rounded-md bg-emerald-50 p-3 text-xs leading-relaxed text-emerald-700">
            {{ libraryDeleteNotice }}
          </p>
          <p v-if="libraryDeleteError" class="mt-3 rounded-md bg-rose-50 p-3 text-xs leading-relaxed text-rose-700">
            {{ libraryDeleteError }}
          </p>
          <p v-if="!libraryBusy && !libraryListError" class="mt-4 text-xs leading-relaxed text-slate-400">
            材料正文随库保存；上一步生成时没用到的文件不会出现在这里。
          </p>

          <!--
            实体别名：模型推断出来的"这两个名字是同一个东西"。猜错之后没有界面能改，
            所以这一节的意义不在"便利"，在于它是唯一的修正入口。只做删除，不做合并
            （合并要跨图搬引用，是另一件事）。
          -->
          <section class="mt-4 border-t border-slate-100 pt-3">
            <div class="flex items-center justify-between gap-3">
              <h3 class="text-xs font-semibold text-slate-700">实体别名</h3>
              <button type="button" class="text-xs text-slate-500 hover:text-slate-900" @click="toggleAliases()">
                {{ aliasesOpen ? '收起' : '查看' }}
              </button>
            </div>
            <p class="mt-1 text-xs leading-relaxed text-slate-400">
              生成时模型会猜「这两个名字指同一个东西」，猜完记成别名（左 → 右是"别名 → 主名"）。猜错了删掉那一条：实体与证据边都不受影响。
            </p>
            <template v-if="aliasesOpen">
              <p v-if="aliasesBusy && aliasEntries.length === 0" class="mt-2 text-xs text-slate-400">正在读取…</p>
              <p v-else-if="aliasesError" class="mt-2 rounded-md bg-rose-50 p-3 text-xs leading-relaxed text-rose-700">
                {{ aliasesError }}
              </p>
              <p v-else-if="!aliasConfigured" class="mt-2 text-xs leading-relaxed text-slate-400">
                图存储没有启用（缺 MINDMAP_NEO4J_PASSWORD），这一层暂时不可用。
              </p>
              <p v-else-if="aliasEntries.length === 0" class="mt-2 text-xs leading-relaxed text-slate-400">
                还没有别名。实体消解跑过一轮之后，合并结果会出现在这里。
              </p>
              <template v-else>
                <ul class="mt-2 divide-y divide-slate-100">
                  <li
                    v-for="edge in aliasEntries"
                    :key="aliasKey(edge)"
                    class="flex items-center justify-between gap-3 py-1.5"
                  >
                    <span class="min-w-0 break-words text-xs text-slate-700">
                      {{ edge.alias }} → {{ edge.canonical }}
                      <span class="text-slate-400">（{{ aliasReasonLabel(edge) }}）</span>
                    </span>
                    <button
                      type="button"
                      class="shrink-0 rounded px-1.5 py-0.5 text-[11px] text-rose-600 hover:bg-rose-50 hover:text-rose-800 disabled:opacity-50"
                      :disabled="aliasDeleting === aliasKey(edge)"
                      @click="removeAlias(edge)"
                    >
                      {{ aliasDeleting === aliasKey(edge) ? '删除中…' : '删除' }}
                    </button>
                  </li>
                </ul>
                <p v-if="aliasNotice" class="mt-2 text-xs leading-relaxed text-emerald-700">{{ aliasNotice }}</p>
                <button
                  type="button"
                  class="mt-2 text-xs text-slate-500 underline decoration-slate-300 hover:text-slate-900 disabled:opacity-50"
                  :disabled="aliasesBusy"
                  @click="clearAliasTable()"
                >
                  清空全部别名
                </button>
              </template>
            </template>
          </section>
        </template>
      </div>
    </aside>

    <div
      v-if="demoOpen"
      class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="demo-title"
    >
      <div class="flex max-h-[70vh] w-full max-w-lg flex-col rounded-lg border border-slate-200 bg-white p-5 shadow-xl">
        <div class="flex items-center justify-between">
          <h2 id="demo-title" class="text-sm font-semibold">预置演示导图</h2>
          <button type="button" class="text-xs text-slate-500 hover:text-slate-900" @click="demoOpen = false">
            关闭
          </button>
        </div>
        <p class="mt-2 text-xs leading-relaxed text-slate-500">
          这些图随页面一起打包，不经过模型，用来在演示时快速摆出一张完整的图。
          它们不会自动顶替模型失败的提示——要哪张，自己点。
        </p>
        <p v-if="demoBusy" class="mt-3 text-xs text-slate-400">正在加载…</p>
        <ul v-else class="mt-3 flex-1 divide-y divide-slate-100 overflow-y-auto">
          <li
            v-for="preset in demoPresets"
            :key="preset.id"
            class="flex items-start justify-between gap-3 py-2"
          >
            <div class="min-w-0">
              <p class="text-sm text-slate-800">{{ preset.label }}</p>
              <p class="text-xs leading-relaxed text-slate-400">{{ preset.description }}</p>
            </div>
            <button
              type="button"
              class="shrink-0 rounded-md border border-slate-300 px-3 py-1 text-xs text-slate-700 hover:bg-slate-50"
              @click="loadDemoPreset(preset)"
            >
              载入
            </button>
          </li>
        </ul>
      </div>
    </div>

    <div
      v-if="store.listOpen"
      class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="list-title"
    >
      <div class="flex max-h-[70vh] w-full max-w-lg flex-col rounded-lg border border-slate-200 bg-white p-5 shadow-xl">
        <div class="flex items-center justify-between">
          <h2 id="list-title" class="text-sm font-semibold">已保存的导图</h2>
          <button type="button" class="text-xs text-slate-500 hover:text-slate-900" @click="store.closeList()">
            关闭
          </button>
        </div>
        <p v-if="store.listBusy" class="mt-3 text-xs text-slate-400">正在读取…</p>
        <p v-else-if="store.maps.length === 0" class="mt-3 text-xs text-slate-400">
          还没有保存过任何导图。
        </p>
        <ul v-else class="mt-3 flex-1 divide-y divide-slate-100 overflow-y-auto">
          <li
            v-for="item in store.maps"
            :key="item.id"
            class="flex items-center justify-between gap-3 py-2"
          >
            <div class="min-w-0">
              <p class="truncate text-sm text-slate-800">{{ item.content.title }}</p>
              <p class="text-xs text-slate-400">
                版本 {{ item.version }} · {{ mapShapeLine(item.content) }} ·
                {{ formatTime(item.updated_at) }}
              </p>
              <!-- 确认态的行内说明：说清"会连什么一起删"，而不是只说"确定吗"。 -->
              <p
                v-if="pendingDeleteId === item.id"
                data-test="map-delete-hint"
                class="mt-1 text-xs leading-relaxed text-rose-700"
              >
                彻底删除「{{ item.content.title }}」，连同它的改动记录。不可恢复。
                <span v-if="isOpenMap(item.id) && store.dirty">
                  这张图上有未保存的修改，删掉也一起没了。
                </span>
              </p>
            </div>
            <div class="flex shrink-0 items-center gap-1">
              <template v-if="pendingDeleteId === item.id">
                <button
                  type="button"
                  data-test="map-delete-confirm"
                  class="rounded-md bg-rose-600 px-3 py-1 text-xs text-white hover:bg-rose-700 disabled:opacity-50"
                  :disabled="store.deleting === item.id"
                  @click="deleteSaved(item.id)"
                >
                  {{ store.deleting === item.id ? '删除中…' : '确认删除' }}
                </button>
                <button
                  type="button"
                  data-test="map-delete-cancel"
                  class="rounded-md px-2 py-1 text-xs text-slate-500 hover:bg-slate-50"
                  @click="pendingDeleteId = null"
                >
                  取消
                </button>
              </template>
              <template v-else>
                <button
                  type="button"
                  class="shrink-0 rounded-md border border-slate-300 px-3 py-1 text-xs text-slate-700 hover:bg-slate-50"
                  @click="openSaved(item.id)"
                >
                  打开
                </button>
                <button
                  type="button"
                  data-test="map-delete"
                  class="shrink-0 rounded-md px-2 py-1 text-xs text-slate-400 hover:text-rose-600"
                  :title="`彻底删除「${item.content.title}」（不可恢复）`"
                  :aria-label="`彻底删除 ${item.content.title}`"
                  @click="pendingDeleteId = item.id"
                >
                  删除
                </button>
              </template>
            </div>
          </li>
        </ul>
      </div>
    </div>

    <div
      v-if="store.conflict"
      class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="conflict-title"
    >
      <div class="w-full max-w-md rounded-lg border border-slate-200 bg-white p-5 shadow-xl">
        <h2 id="conflict-title" class="text-sm font-semibold">保存冲突</h2>
        <p class="mt-2 text-xs leading-relaxed text-slate-600">
          这张导图在别处被改过（{{ store.conflict.message }}）。你本地的修改还没有保存，选择怎么处理：
        </p>
        <div class="mt-4 flex flex-col gap-2">
          <button
            type="button"
            class="rounded-md border border-slate-300 px-3 py-2 text-left text-xs text-slate-700 hover:bg-slate-50"
            @click="store.reloadFromServer()"
          >
            放弃本地修改，加载服务端最新版本
          </button>
          <button
            type="button"
            class="rounded-md border border-slate-300 px-3 py-2 text-left text-xs text-slate-700 hover:bg-slate-50"
            @click="store.saveAsNew()"
          >
            保留本地修改，另存为新导图
          </button>
          <button
            type="button"
            class="rounded-md px-3 py-2 text-left text-xs text-slate-500 hover:bg-slate-50"
            @click="store.dismissConflict()"
          >
            取消，稍后再说
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
