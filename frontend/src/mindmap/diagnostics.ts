import type { Diagnostic, Suggestion } from '../api/types';

/**
 * 结构诊断与建议的**表现层判据**：只有这一份。
 *
 * 三条铁律，每一条都对着一个具体的坏结果：
 *
 *  1. **按 `code` / `kind` 通用渲染，不硬编码**。后端正在持续加新的诊断码与建议类型
 *     （近义冗余、缺失环节…），前端不能在每加一个码的时候跟着改一次。所以映射表只
 *     负责"已知的码给一句人话"，`?? code` 兜住全部未知情况——后端加码时界面**不改也能
 *     显示**，显示的是码本身而不是空白。
 *  2. **判据留在纯函数里，组件只渲染**。同一件事（"这条该不该进状态行""这条能不能
 *     应用"）在状态行、抽屉、按钮三处各写一遍必然分叉——那正是"一边说有 3 条、另一边
 *     只有 1 条"的来源。
 *  3. **info 与 warn 不是同一档**。`SINGLE_CHILD` 在一棵正常的树上是结构常态（任何
 *     链式子结构都命中），把它和"同级重名"摆在一起，用户学会的是无视整块面板。
 *     所以两者在这里就被分开，状态行只认 `attention`（见 `structureHint`）。
 */

/** 归一到契约里的三档；认不出来的 level 按 `info` 处理（不是不显示）。 */
export type DiagnosticLevel = 'info' | 'warn' | 'error';

/** 颜色语义。组件把它映射成 tailwind class —— 色值不在这里，与 `NODE_COLORS` 同一条理由。 */
export type Tone = 'slate' | 'amber' | 'rose';

const LEVELS = ['info', 'warn', 'error'] as const;

export function diagnosticLevelOf(level: string): DiagnosticLevel {
  return (LEVELS as readonly string[]).includes(level) ? (level as DiagnosticLevel) : 'info';
}

/**
 * 级别 → 颜色。
 *
 * `warn` 是琥珀、`error` 是玫红、其余（含 info 与**未知级别**）是中性灰。未知值走灰色
 * 而不是红色：红是"出问题了"的语气，而一个前端不认识的级别恰恰说明不了这件事。
 */
export function diagnosticTone(level: DiagnosticLevel): Tone {
  if (level === 'warn') return 'amber';
  if (level === 'error') return 'rose';
  return 'slate';
}

/**
 * 诊断码 → 一句人话。
 *
 * **只登记已知码**。表里没有的一律显示码本身（`diagnosticLabel`），这是这个功能能
 * 扛住后端演进的全部机制：另一个 agent 加 `NEAR_DUPLICATE_BRANCH` 时，界面不需要
 * 改一行，用户看到的是 `NEAR_DUPLICATE_BRANCH` 加后端给的那句中文 message。
 *
 * 所以这里**不是**一张必须补齐的表：空缺是正常状态，不是待办。
 */
export const DIAGNOSTIC_LABELS: Record<string, string> = {
  // 结构（optimization/service.py::check）
  SINGLE_CHILD: '单子节点',
  DEPTH_LIMIT: '到了深度上限',
  DUPLICATE_LABEL: '同级重名',
  // F15 那一轮补的三种形态（后端 `_shape_of` 一族）。它们与上面三条互斥地覆盖
  // "这一支长得不对"的几种情况，**不替换**上面的码。
  HEADING_ONLY_BRANCH: '只有标题',
  SHALLOW_BRANCH: '这一支太浅',
  NEAR_DUPLICATE_LABEL: '同级近义',
  MISSING_DETAIL: '缺少说明',
  // 引用核验（optimization/source_refs.py）
  EVIDENCE_UNUSED: '引文没被用上',
  UNSOURCED_NODE: '没有可核验的引用',
  SOURCE_TEXT_UNAVAILABLE: '来源原文不可用',
  SOURCE_RANGE_OUT_OF_BOUNDS: '引用区间越界',
  EMPTY_SOURCE_EXCERPT: '引用区间是空白',
  UNVERIFIED_SOURCE_REF: '引用无法核验',
  SOURCE_QUOTE_MISMATCH: '引文对不上原文',
  SOURCE_QUOTE_AMBIGUOUS: '引文多处命中',
  SOURCE_QUOTE_OFFSET_FIXED: '引文偏移已补正',
  INVALID_SOURCE_PAGE: '页码越界',
  SOURCE_PAGE_UNVERIFIED: '页码未核验',
  SOURCE_REF_OUTSIDE_EVIDENCE: '引文落在命中范围外',
  SOURCE_REF_CONTRADICTED: '引文与节点语义相反',
  SOURCE_REF_NLI: '引文语义判定',
};

/** 一条诊断的标题：已知码给人话，未知码给码本身。空码兜底成「未知诊断」。 */
export function diagnosticLabel(code: string): string {
  const key = (code ?? '').trim();
  if (!key) return '未知诊断';
  return DIAGNOSTIC_LABELS[key] ?? key;
}

/**
 * 建议类型 → 中文名。
 *
 * 与 `Diagnostic` 同一条：只登记**已经能应用**的那一种。其余 kind 会显示原 kind，
 * 而界面对它们不提供「应用」——因为后端还没给对应的操作定义，摆一个点了不知道会
 * 发生什么的按钮比不摆更糟。
 */
export const SUGGESTION_KIND_LABELS: Record<string, string> = {
  dedupe: '合并重复',
  merge: '层级归并',
  link: '补关系',
  fill: '补环节',
  drop: '剔除无依据',
  expand: '再展开一层',
  add_detail: '补说明',
  cross_map: '同类图参照',
};

/**
 * 有"动作入口"、但那个入口**不在这里**的 kind → 告诉用户去哪儿。
 *
 * `expand` / `add_detail` 不是没有定义，而是它们的动作是**一次模型调用**：几十秒、
 * 有成本、结果不确定。把它塞进「应用」按钮，等于把"点一下"变成"发一次模型请求"，
 * 而按钮上看不出这个代价。它们真正的入口已经存在——选中节点后工具条上的「展开」。
 *
 * 所以这里不给按钮，但也不说"后端还没定义"（那是假话，后端已经定义了）：说清
 * **这件事该怎么做**。
 */
export const MANUAL_KINDS: Record<string, string> = {
  expand: '这一类要按需展开：选中那个节点，点工具条上的「展开」',
  add_detail: '这一类要走 AI 生成：选中节点后用「改这个节点」让它补说明',
  cross_map: '依据来自你库里别的图：点开那张图，看它是怎么展开这一节的',
  // `link` 的动作是**改层级**（把这个节点挂到另一个父下面）。它现在只显示、
  // 不给按钮：撤销只救回这一次会话，而"应用它到底改成什么"还没有样本可以定义
  // （`optimization/link.py` 的 docstring 写过这一条）。所以这里不说"后端没定义"
  // ——它定义了候选，缺的是触发它的那一按；说清手动怎么改。
  link: '这一类要手动挪：选中节点拖动，或者剪切后粘到候选父下面',
};

export function suggestionKindLabel(kind: string): string {
  const key = (kind ?? '').trim();
  if (!key) return '未知建议';
  return SUGGESTION_KIND_LABELS[key] ?? key;
}

/**
 * 哪些 `kind` 有真正的应用动作。
 *
 * **这是一张白名单，不是一个 switch**。`applySuggestion` 里按这张表分派，表外的一律
 * 只显示。后端加一种能自动应用的建议类型时，改这一处 + `applySuggestion` 一处即可；
 * 而加"只能看"的类型（多半是模型给的那些）连这里都不用碰。
 */
export const APPLICABLE_KINDS: readonly string[] = ['dedupe'];

/** 界面能直接渲染的一条诊断。归一（level / 文案 / 键）都在 `normalizeDiagnostics` 里做完。 */
export interface DiagnosticNote {
  /**
   * 稳定键：**忽略**与 `v-for` 都用它。
   *
   * 不用 `node_id`：同一个节点上可以同时有两条诊断（重名 + 深度）。也不用后端的
   * `Suggestion.id`：那是每次请求现生成的 uuid，两次请求之间对不上，"忽略过"会失效。
   */
  key: string;
  level: DiagnosticLevel;
  tone: Tone;
  /** 原码。界面在标题旁显示它——未知码时它**就是**用户能看到的全部信息。 */
  code: string;
  /** 码的人话（未知码 = 码本身）。 */
  label: string;
  message: string;
  evidence: string;
  nodeId: string | null;
}

/** 界面能直接渲染的一条建议。 */
export interface SuggestionNote {
  key: string;
  kind: string;
  kindLabel: string;
  message: string;
  evidence: string;
  nodeId: string | null;
  /**
   * 有没有对应的应用动作。`false` 时界面**不渲染按钮**，只显示一句原因
   * （`blockedReason`）——灰着的按钮会让人一直去试它。
   */
  applicable: boolean;
  blockedReason: string;
}

/**
 * 一条诊断/建议的稳定键。
 *
 * 三者拼起来：`kind|node_id|message`。后端的 `message` 对同一输入是确定性的（规则是
 * 纯函数），所以同一份内容重跑两次得到同一个键——"忽略"才能跨过一次重新检查。
 * 换掉内容之后那句 message 变了，它也就该重新出现，这正是想要的语义。
 */
function stableKey(kind: string, nodeId: string | null, message: string): string {
  return `${kind}|${nodeId ?? ''}|${message.trim()}`;
}

export function normalizeDiagnostics(list: readonly Diagnostic[] | undefined | null): DiagnosticNote[] {
  if (!Array.isArray(list)) return [];
  return list.map(item => {
    const level = diagnosticLevelOf(item.level ?? '');
    const code = (item.code ?? '').trim();
    const nodeId = item.node_id ?? null;
    const message = (item.message ?? '').trim();
    return {
      key: stableKey(`diagnostic:${code}`, nodeId, message),
      level,
      tone: diagnosticTone(level),
      code,
      label: diagnosticLabel(code),
      message: message || diagnosticLabel(code),
      evidence: (item.evidence ?? '').trim(),
      nodeId,
    };
  });
}

/**
 * 把后端建议翻成界面条目。
 *
 * `applicable` 只看两件事：这个 kind 在白名单里，以及它点名了一个节点（没有节点的
 * `dedupe` 没法执行——删谁？）。
 *
 * 不可应用时分三档说原因，因为它们对用户的意思完全不同：
 *  - 有动作入口但在别处（`expand` / `add_detail`）→ 说清怎么去做；
 *  - 后端还没定义这个动作（`merge` / `link` / `fill` / `drop`）→ 说清它只是显示；
 *  - 点不出对象（没有 `node_id`）→ 说清缺了什么。
 * 把三种合成一句"暂不支持"，前两种就被混成了第三种，而它们要做的事完全不同。
 */
export function normalizeSuggestions(list: readonly Suggestion[] | undefined | null): SuggestionNote[] {
  if (!Array.isArray(list)) return [];
  return list.map(item => {
    const kind = (item.kind ?? '').trim();
    const nodeId = item.node_id ?? null;
    const message = (item.message ?? '').trim();
    const known = APPLICABLE_KINDS.includes(kind);
    return {
      key: stableKey(`suggestion:${kind}`, nodeId, message),
      kind,
      kindLabel: suggestionKindLabel(kind),
      message: message || suggestionKindLabel(kind),
      evidence: (item.evidence ?? '').trim(),
      nodeId,
      applicable: known && !!nodeId,
      blockedReason: known && !nodeId
        ? '这条建议没有指明节点，没法自动改'
        : MANUAL_KINDS[kind] ?? '这类建议还没有对应的应用动作，先只显示',
    };
  });
}

/** 一次结构检查的规模。三个数各自对应一种界面处置，见 `structureHint`。 */
export interface StructureCounts {
  /** `warn` + `error`：真需要看一眼的，进状态行。 */
  attention: number;
  /** 可以一键应用的候选修改，也进状态行（"有 3 条建议"）。 */
  suggestions: number;
  /** `info`：结构常态。**不进状态行**，抽屉里默认折叠。 */
  hints: number;
}

export function countDiagnostics(notes: readonly DiagnosticNote[]): StructureCounts['attention'] {
  return notes.filter(note => note.level !== 'info').length;
}

export function structureCounts(
  diagnostics: readonly DiagnosticNote[],
  suggestions: readonly SuggestionNote[],
): StructureCounts {
  return {
    attention: countDiagnostics(diagnostics),
    suggestions: suggestions.length,
    hints: diagnostics.filter(note => note.level === 'info').length,
  };
}

/**
 * 状态行上的那半句；没什么可说时是**空串**（调用方据此不加这一段）。
 *
 * 三种组合分开说，因为它们对用户的意思不一样：`建议`是可以一键改的候选，`要注意`
 * 是看不太懂但可能有问题的东西。把它们合成一个数会让"3"这个数字在两个时刻表示
 * 两件不同的事——而状态行是常驻的，用户会记住这个数字。
 *
 * **`hints` 什么都不产生**。这是刻意的，理由见文件头第 3 条：info 在一棵正常的树上
 * 恒有若干条，让它进状态行，等于给这条常驻的界面挂一个永远不为零的噪音计数器。
 */
export function structureHint(counts: StructureCounts): string {
  const parts: string[] = [];
  if (counts.suggestions > 0) parts.push(`${counts.suggestions} 条建议`);
  if (counts.attention > 0) parts.push(`${counts.attention} 处要注意`);
  return parts.join('、');
}

/**
 * 抽屉里那句总结。**诊断与建议分开数**，并且把折叠起来的 info 单独说出来——
 * "还有 12 条提示"必须让人知道它们是提示而不是问题，否则收起这件事就变成了隐瞒。
 */
export function structureSummaryLine(counts: StructureCounts): string {
  if (!counts.suggestions && !counts.attention && !counts.hints) return '没有发现问题。';
  const parts: string[] = [];
  if (counts.attention) parts.push(`${counts.attention} 处要注意`);
  if (counts.suggestions) parts.push(`${counts.suggestions} 条建议`);
  if (counts.hints) parts.push(`${counts.hints} 条提示`);
  return parts.join(' · ');
}
