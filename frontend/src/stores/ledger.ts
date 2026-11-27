import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

import { getEditHealth, getEditSummary, listEditEvents } from '../api/client';
import type { EditEventRow, EditHealth, EditSummary } from '../api/types';
import { NODE_COLOR_LABELS } from '../mindmap/edits';

/**
 * 账本：**这张图改过什么**——从 `edit_events` 表读，不是从内存里读。
 *
 * ## 它和右抽屉里那块「本次生成明细」不是一件事
 *
 * `AgentPanel` 显示的是"刚才那 25 秒发生了什么"，数据在 `chat` store 里，关掉页面就
 * 没了，且只有一次生成的范围。账本显示的是**这张图有史以来**的改动：跨会话、跨生成、
 * 含每一次手工编辑与每一次一键修复。两者唯一的共同点是"都长得像日志"。
 *
 * ## 三个读取函数（`getEditSummary` / `listEditEvents` / `getEditHealth`）在这里落地
 *
 * 它们原本是三条零引用：后端路由在、`api/client.ts` 的函数在、界面上没有任何入口。
 * client 自己的注释称 `getEditSummary` 是"这一版真正的交付物"，而用户点不到它——
 * 一个没人读的统计和没写过的统计一样，都只能靠连数据库才知道结论。
 *
 * 三条各自回答一件事，所以三条都要：
 *
 *   - `listEditEvents`：那 42 条具体是什么（明细，逐条人话）；
 *   - `getEditSummary`：这 42 条合起来说明什么（分布：标题被改短了没有、删掉的集中
 *     在第几层、几次生成一字未改）；
 *   - `getEditHealth`：**这张表还在收数吗**。它跨图、与当前打开哪张图无关，正常时
 *     一个字都不该显示——见 `healthWarning`。
 *
 * ## 一次读多少条：`LEDGER_LIMIT` 不是 `RECENT_DEFAULT`
 *
 * 后端缺省给 50 条。账本要的不是 50 条，而是"够折叠"。一次一键修复就能写进三四十条
 * 事件（整棵树按 diff 逐处记），50 条原始行折完之后可能只剩两行，而"这张图改过什么"
 * 读起来正是那三行里最没信息量的那两行。200 是后端上限（500）之内、一次请求还能算
 * 便宜的量级：真到 200 条时，折叠之后一屏也放得下。
 */
export const LEDGER_LIMIT = 200;

/**
 * 一行的颜色档。色值不在这里——`EditLedger.vue` 里映射成 class（与 `StructureCheck`
 * 把 `Tone` 映射成 class 同一套做法：判据与配色分开，配色只有一个地方）。
 */
export type LedgerTone = 'agent' | 'user' | 'rule';

/** 界面要显示的一行：**已经是人话**，模板里不再有任何 `if (kind === …)`。 */
export interface LedgerLine {
  /** 这一行的事件时刻（同一条聚合行取批内第一条的 `at`）。 */
  at: string;
  /** 完整的一句话，人称随 `actor` 变（"你" / "模型" / "规则"）。 */
  text: string;
  tone: LedgerTone;
  /** 这一行涉及的节点 id，供"只看选中节点"筛选与将来的跳转定位。 */
  nodeIds: string[];
  /** 折叠进来的明细（只有一键修复那一行有；普通行是空数组）。 */
  children: LedgerLine[];
  /** 折叠摘要（"删了 3 个节点 · 改了 7 处文字"）。普通行是空串。 */
  digest: string;
}

const FIELD_WORDS: Record<string, string> = {
  label: '标题',
  title: '图名',
  detail: '详情',
  note: '备注',
  color: '颜色',
  icon: '图标',
};

/**
 * 颜色值要翻成人话，否则一行日志会写成"你改了颜色：无 → amber"。
 *
 * `amber` 是调色板**键**（语义色，色值在 `styles.css`）。键给人读没有意义——界面上
 * 那个下拉早就写着"琥珀（注意）"（`mindmap/edits.ts::NODE_COLOR_LABELS`），账本没有
 * 理由退回去显示键名。认不出的值原样显示（老数据里可能是任意字符串）。
 */
const COLOR_WORDS: Record<string, string> = NODE_COLOR_LABELS;

/** 一个字段的值怎么念。只有颜色需要翻译，其余（字数摘要、emoji、标签）本来就是人话。 */
function fieldValue(field: string | undefined, value: string | undefined): string {
  const text = shown(value);
  if (field === 'color') return COLOR_WORDS[text] ?? text;
  return text;
}

/** 码点长度，与后端 `length()`（UTF-8 字符）同一口径。 */
function charLen(text: string): number {
  return Array.from(text).length;
}

/** `"第2位"` / `"第10位"`——重排事件用位次当 `before` / `after`，不是父节点标签。 */
function isRank(text: string | undefined): boolean {
  return !!text && /^第\d+位$/.test(text);
}

function shown(text: string | undefined, fallback = '无'): string {
  const trimmed = (text ?? '').trim();
  return trimmed || fallback;
}

/**
 * 一句人话里的引号取短一点。
 *
 * `label` / `title` 在库里存的是**全文**（≤200 字，统计要算长度），一行日志把它整段
 * 念出来会把列表撑成三段。40 与 `edit-log.ts::SUMMARY_LABEL_CHARS` 同一档：那边截的
 * 是写进库的摘要，这边截的是读出来给人看的一行——两者口径一致，才不会出现"库里写的
 * 是 40 字、界面显示 200 字"这种对不上的观感。
 */
const QUOTE_CHARS = 40;

function quote(text: string | undefined): string {
  const chars = Array.from((text ?? '').trim());
  if (!chars.length) return '空';
  return chars.length > QUOTE_CHARS ? `${chars.slice(0, QUOTE_CHARS).join('')}…` : chars.join('');
}

function updateWords(row: EditEventRow, who: string): string {
  const field = row.field ?? 'label';
  if (field === 'label' || field === 'title') {
    const name = FIELD_WORDS[field];
    const before = charLen(row.before ?? '');
    const after = charLen(row.after ?? '');
    // "改短了" / "改长了" / "改了" 三档，不是修辞：`label_shrunk` 统计的就是"改短"，
    // 界面若一律写"改了标题"，用户就看不出这条正是那个数字的来源。
    const verb = after < before ? '改短了' : after > before ? '改长了' : '改了';
    return `${who}${verb}${name}：「${quote(row.before)}」→「${quote(row.after)}」`;
  }
  return `${who}改了${FIELD_WORDS[field] ?? field}：` +
    `${fieldValue(row.field, row.before)} → ${fieldValue(row.field, row.after)}`;
}

/**
 * 一条普通事件的人话。
 *
 * 每一分支都只用**事件行上真的有的字段**（`contracts/edits.py::EditEventIn`）：`add`
 * 只有 `after`（标签摘要）、`move` 只有父标签或位次、`remove` 只有 `before` 与
 * `subtree_size`。所以文案不能说"把某节点移到了 X 下面"——事件里没有那个节点的名字，
 * 硬写就只能从 `node_id` 猜，而 id 不是给人读的。
 */
function singleWords(row: EditEventRow): string {
  const who = row.actor === 'agent' ? '模型' : '你';
  switch (row.kind) {
    case 'generate':
      return row.node_count
        ? `模型生成 · ${row.node_count} 个节点`
        : '模型生成（当次节点数没记）';
    case 'accept':
      return row.change_count
        ? `应用了一次候选 · ${row.change_count} 处改动`
        : '应用了一次候选';
    case 'add':
      return row.after ? `${who}加了一个节点「${quote(row.after)}」` : `${who}加了一个节点`;
    case 'remove':
      return row.subtree_size && row.subtree_size > 1
        ? `${who}删掉「${quote(row.before)}」整支 · 连它共 ${row.subtree_size} 个节点`
        : `${who}删掉「${quote(row.before)}」`;
    case 'update':
      return updateWords(row, who);
    case 'move':
      if (isRank(row.before) || isRank(row.after)) {
        return `${who}调了顺序：${shown(row.before)} → ${shown(row.after)}`;
      }
      // `move` 换父：`before` / `after` 存的是**父节点标签**（根是 `"根"`）。
      return `${who}换了父节点：${shown(row.before)} → ${shown(row.after)}`;
    case 'edge':
      if (row.before && row.after) return `${who}改了关系：${row.before} → ${row.after}`;
      if (row.after) return `${who}加了一条关系：${row.after}`;
      return `${who}删了一条关系：${shown(row.before)}`;
    case 'style':
      return `${who}改了${FIELD_WORDS[row.field ?? 'color'] ?? '样式'}：` +
        `${fieldValue(row.field, row.before)} → ${fieldValue(row.field, row.after)}`;
    case 'repair':
      // 单条 repair 只在"这批只有一条"时走到这里（`foldEvents` 仍会把它折成一行）。
      return '规则整理了一处';
    default:
      // 认不出来的 `kind` 原样显示：与 `diagnostics.ts` 对未知诊断码的做法一致——
      // 后端加一种动作时，界面不该整块变成空白，也不该假装自己认识它。
      return `${who}改了一处（${row.kind}）`;
  }
}

function toneOf(row: EditEventRow): LedgerTone {
  if (row.kind === 'repair') return 'rule';
  return row.actor === 'agent' ? 'agent' : 'user';
}

function lineOf(row: EditEventRow): LedgerLine {
  return {
    at: row.at,
    text: singleWords(row),
    tone: toneOf(row),
    nodeIds: row.node_id ? [row.node_id] : [],
    children: [],
    digest: '',
  };
}

/**
 * 一键修复那条明细怎么念。
 *
 * 难在这里：**这批事件的 `kind` 全被 `repair` 覆盖了**（`DiffOptions.kind`，见
 * `mindmap/edit-log.ts`），动作名丢了，只剩载荷。所以只能按载荷反推——而每一格都是
 * 判据，不是猜：
 *
 *   - `subtree_size` 有值 → 这条是"删掉了一棵子树"（只有 remove 会填它）；
 *   - 有 `field` → 改的是那个字段的文字/样式；
 *   - `before` 是 `"第N位"` → 重排；
 *   - 没有 `before` 而有 `after` → 新增（`add` 的 after 是标签摘要）。
 *
 * 判不出来就说"一处改动"并带上层号，不编一个动作名。
 */
function repairWords(row: EditEventRow): string {
  if (row.subtree_size) {
    return row.subtree_size > 1
      ? `删掉「${quote(row.before)}」整支 · 共 ${row.subtree_size} 个节点`
      : `删掉「${quote(row.before)}」`;
  }
  if (row.field) {
    return `改了${FIELD_WORDS[row.field] ?? row.field}：` +
      `${fieldValue(row.field, row.before)} → ${fieldValue(row.field, row.after)}`;
  }
  if (isRank(row.before) || isRank(row.after)) {
    return `调了顺序：${shown(row.before)} → ${shown(row.after)}`;
  }
  if (!row.before && row.after) return `加了节点「${quote(row.after)}」`;
  return `一处改动${row.depth ? `（第 ${row.depth} 层）` : ''}`;
}

function repairDigest(batch: readonly EditEventRow[]): string {
  let removed = 0;
  let removedBranches = 0;
  let relabeled = 0;
  let restyled = 0;
  let moved = 0;
  let added = 0;
  for (const row of batch) {
    if (row.subtree_size) {
      removed += row.subtree_size;
      if (row.subtree_size > 1) removedBranches += 1;
      continue;
    }
    if (row.field === 'color' || row.field === 'icon') {
      restyled += 1;
      continue;
    }
    if (row.field) {
      relabeled += 1;
      continue;
    }
    if (isRank(row.before) || isRank(row.after)) {
      moved += 1;
      continue;
    }
    if (!row.before && row.after) added += 1;
  }
  const parts: string[] = [];
  if (removed) {
    parts.push(removedBranches
      ? `删了 ${removed} 个节点（其中 ${removedBranches} 整支）`
      : `删了 ${removed} 个节点`);
  }
  if (relabeled) parts.push(`改了 ${relabeled} 处文字`);
  if (restyled) parts.push(`调了 ${restyled} 处样式`);
  if (moved) parts.push(`搬了 ${moved} 处`);
  if (added) parts.push(`新增 ${added} 个节点`);
  return parts.join(' · ');
}

function repairLine(batch: readonly EditEventRow[]): LedgerLine {
  return {
    at: batch[0].at,
    text: `规则整理 · ${batch.length} 处`,
    tone: 'rule',
    nodeIds: batch.flatMap(row => (row.node_id ? [row.node_id] : [])),
    children: batch.map(row => ({
      at: row.at,
      text: repairWords(row),
      tone: 'rule' as const,
      nodeIds: row.node_id ? [row.node_id] : [],
      children: [],
      digest: '',
    })),
    digest: repairDigest(batch),
  };
}

/**
 * 原始事件 → 可读的几行。**一键修复那批折成一条。**
 *
 * 分组键是 `at`：一次修复把整棵树换掉，批次内每一处改动共用同一个时刻（一次操作取
 * 一次 `new Date()`，见 `edit-log.ts::DiffOptions.at`）。列表已按 `at DESC, id ASC`
 * 排好（后端 `edits.recent` 的排序就是为这个），所以"同一批"必然是**相邻的一段**。
 *
 * 为什么必须折叠：一次修复可能写进三四十条事件。逐条列出来之后，同一张图上那 3 次
 * 手工编辑会被埋在第 4 条到第 40 条之间——用户问的是"这张图改过什么"，而被规则批量
 * 整理掉的 38 处细节是**一次动作**，它替用户回答了整件事。
 */
export function foldEvents(rows: readonly EditEventRow[]): LedgerLine[] {
  const out: LedgerLine[] = [];
  let index = 0;
  while (index < rows.length) {
    const row = rows[index];
    if (row.kind !== 'repair') {
      out.push(lineOf(row));
      index += 1;
      continue;
    }
    const batch: EditEventRow[] = [];
    while (index < rows.length && rows[index].kind === 'repair' && rows[index].at === row.at) {
      batch.push(rows[index]);
      index += 1;
    }
    out.push(repairLine(batch));
  }
  return out;
}

/**
 * 标题行那句摘要：**这张图的规模与改动量**。
 *
 * 三个数一起读才成立（后端 `summary` 的 docstring 把这条讲透了）：只给"每个节点被改
 * 0.8 次"看不出方向，加上"9 次一字未改、1 次改了 8 处"才知道处方完全不同。所以这里
 * 一定把 `generated_nodes` / `changed_events` / `untouched_runs` 三个都放进去，而不是
 * 挑一个当"关键指标"。
 */
export function headlineOf(summary: EditSummary): string {
  if (!summary.total) return '还没有改动记录';
  const parts: string[] = [];
  if (summary.generated_runs) {
    parts.push(summary.generated_nodes
      ? `模型给了 ${summary.generated_nodes} 个节点`
      : `模型生成过 ${summary.generated_runs} 次`);
  }
  parts.push(`图上 ${summary.changed_events} 处改动`);
  if (summary.generated_runs && summary.untouched_runs) {
    parts.push(`${summary.untouched_runs} / ${summary.generated_runs} 次生成一字未改`);
  }
  return parts.join(' · ');
}

/**
 * 摘要下面那几条读数：**只有真的读得出方向时才写**。
 *
 * 三条各对应一个"生成侧该改什么"的判断（后端 `summary` 的 docstring）：标题普遍被
 * 改短 → 生成侧 16 字上限可能还是太长；零散删除集中在第 3 层 → 初次生成挖得太深；
 * 有删除没记规模 → 那批是老数据，别拿它算比例。
 *
 * 空列表是常态（一张刚生成的图三条都不成立），所以模板里那一段整体不渲染——摆一行
 * "暂无"等于给一个不存在的结论留了位置。
 */
export function notesOf(summary: EditSummary): string[] {
  const notes: string[] = [];
  const shrunk = summary.label_shrunk;
  if (shrunk.count && shrunk.avg_before !== null && shrunk.avg_after !== null) {
    notes.push(
      `标题普遍被改短：平均 ${shrunk.avg_before.toFixed(1)} 字 → ` +
      `${shrunk.avg_after.toFixed(1)} 字（${shrunk.count} 次）`,
    );
  }
  const depths = Object.entries(summary.removed_depth ?? {});
  if (depths.length) {
    // 取"删得最多"的那一层：同分时取层号小的，读起来更稳定（不是随机顺序）。
    const [layer, count] = depths.reduce((best, item) =>
      (item[1] > best[1] || (item[1] === best[1] && Number(item[0]) < Number(best[0])) ? item : best));
    notes.push(`零散删掉的节点集中在第 ${layer} 层 · ${count} 个`);
  }
  const unknown = summary.removed_subtree?.size_unknown;
  if (unknown) notes.push(`${unknown} 条删除没记规模（加列之前的老数据）`);
  return notes;
}

/**
 * 链路坏掉时的一句话；正常时是空串。
 *
 * **正常状态一个字都不显示**，这是把一个运维视角的读数接进用户界面的唯一办法：
 * `edited_maps` / `saved_maps` 两个跨图数字对"这张图改过什么"没有任何帮助，摆在用户
 * 面前只会让他在两个和他无关的量之间找意义。而 `chain_suspect` 一旦为真，它就是
 * 用户唯一能看见的"账本可能没在记"——后端那张表曾经静默坏过 40 分钟，期间每张图的
 * `total` 都是 0，看起来和"没人改过"一模一样。所以异常必须出现在这里。
 */
export function healthWarning(health: EditHealth | null): string {
  if (!health?.chain_suspect) return '';
  return `账本可能没在记：昨天存过 ${health.saved_maps} 张图，` +
    `${health.edited_maps} 张收到改动记录。这条链断了时，改动不会报错，只是不出现。`;
}

export const useLedgerStore = defineStore('ledger', () => {
  /** 这份数据属于哪张图。与 `mindmap.mapId` 分开存：读的时候可能已经换了图。 */
  const mapId = ref<string | null>(null);
  const events = ref<EditEventRow[]>([]);
  const summary = ref<EditSummary | null>(null);
  const health = ref<EditHealth | null>(null);
  const busy = ref(false);
  const error = ref('');
  const loaded = ref(false);

  const lines = computed(() => foldEvents(events.value));
  const headline = computed(() => (summary.value ? headlineOf(summary.value) : ''));
  const notes = computed(() => (summary.value ? notesOf(summary.value) : []));
  const warning = computed(() => healthWarning(health.value));

  function reset(): void {
    mapId.value = null;
    events.value = [];
    summary.value = null;
    health.value = null;
    busy.value = false;
    error.value = '';
    loaded.value = false;
  }

  /**
   * 读这张图的账本。`id` 为空（还没保存过的新图）时清空并不发请求。
   *
   * 三个请求并行，但 `getEditHealth` **自己吞掉失败**：那条是跨图的运维读数，它坏了
   * 只意味着"这一次没有健康结论"，不该让单图的明细与统计一起变成一句错误——那正是
   * 把一条辅助读数接成主路径的典型代价。
   */
  async function load(id: string | null): Promise<void> {
    if (!id) {
      reset();
      return;
    }
    busy.value = true;
    error.value = '';
    try {
      const [list, stat, healthNow] = await Promise.all([
        listEditEvents(id, LEDGER_LIMIT),
        getEditSummary(id),
        getEditHealth().catch(() => null),
      ]);
      mapId.value = id;
      events.value = list;
      summary.value = stat;
      health.value = healthNow;
      loaded.value = true;
    } catch (err) {
      // 错误留在面板里而不是弹窗：账本是一块可以自己空着的区域，它读不到数据不该
      // 打断用户正在做的事（与 `structure` store 把失败写进 status 同一档）。
      error.value = err instanceof Error ? err.message : String(err);
      loaded.value = false;
    } finally {
      busy.value = false;
    }
  }

  return {
    mapId,
    events,
    summary,
    health,
    busy,
    error,
    loaded,
    lines,
    headline,
    notes,
    warning,
    load,
    reset,
  };
});
