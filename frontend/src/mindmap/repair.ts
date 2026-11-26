/**
 * 「可自动修复」这件事的纯函数：**哪些诊断能一键修、修之前怎么数给它看**。
 *
 * ## 为什么它不叫 APPLIERS
 *
 * `stores/structure.ts` 里那套 `APPLIERS` / `APPLICABLE_KINDS` 是**逐条建议**的分派
 * （`kind` → 对某一个节点的动作），现在只有 `dedupe`：删掉那个重复的叶子。`link` /
 * `fill` / `merge` 没有 applier，因为后端还没定义"应用它到底改成什么"。
 *
 * 修复不是那套里的第六个 applier，它换了一个维度：
 *
 *  - 输入是**诊断码**（`DUPLICATE_LABEL` / `SINGLE_CHILD` / `HEADING_ONLY_BRANCH`），
 *    不是建议的 `kind`；
 *  - 动作是**整棵树跑到不动点**，不是"对这一条做什么"；
 *  - 一次点击会改掉多处（`repair.py` 两段交替：先全部合并、再全部压链）。
 *
 * 硬塞进 `APPLIERS` 会要求"每一条建议都能独立应用"，而那正好是后端没有的东西
 * ——一条 `dedupe` 建议只说"这两个像"，没说"合并进哪一个"。所以这里单独一条路，
 * 也不动那张白名单。
 *
 * ## 为什么这三个码要在这里再列一遍
 *
 * 后端 `AUTO_REPAIRABLE_CODES` 是权威。前端这份是**镜像**，存在的理由是"按钮该不该
 * 出现"必须在发请求之前就知道（否则要为了显示一个按钮先打一次 `repair`）。
 * 镜像漂移的后果是可控的：多列一个码 → 按钮照常出现，后端 422 会在面板上说清
 * "不能自动修的规则：…"；少列一个码 → 那一条只是暂时没有入口，不产生错误状态。
 *
 * 判断依据是**诊断码**，不是诊断的 `level`：`SINGLE_CHILD` 是 `info`（结构常态），
 * 但它是三条可修规则之一，所以它必须能驱动按钮。`info` 默认折叠那条规矩管的是
 * "别把十几条常态铺满抽屉"，不管"能不能一键修好"。
 */
import { diagnosticLabel, type DiagnosticNote } from './diagnostics';

/** 后端 `optimization/repair.py::AUTO_REPAIRABLE_CODES` 的镜像（顺序也照抄）。 */
export const REPAIRABLE_CODES: readonly string[] = [
  'DUPLICATE_LABEL',
  'SINGLE_CHILD',
  'HEADING_ONLY_BRANCH',
];

/** 一组可修诊断：哪个码、它的中文名、有几条。 */
export interface RepairGroup {
  code: string;
  label: string;
  count: number;
}

/**
 * 按码分组数一遍。
 *
 * 用**过滤之后**的诊断（`visibleDiagnostics`）：用户点过「忽略」的那一条不该还挂在
 * "可自动修复 3 处"里——那条数字是给行动用的，点下去却只修了两处，比不给数字更糟。
 *
 * 顺序按 `REPAIRABLE_CODES` 而不是按出现顺序：同一个面板上重复出现的数字，位置
 * 不能随树的形状变。
 */
export function repairPlan(notes: readonly DiagnosticNote[]): RepairGroup[] {
  const counts = new Map<string, number>();
  for (const note of notes) {
    if (!REPAIRABLE_CODES.includes(note.code)) continue;
    counts.set(note.code, (counts.get(note.code) ?? 0) + 1);
  }
  return REPAIRABLE_CODES
    .filter(code => (counts.get(code) ?? 0) > 0)
    .map(code => ({ code, label: diagnosticLabel(code), count: counts.get(code) ?? 0 }));
}

/** 计划里一共几处。 */
export function repairPlanTotal(plan: readonly RepairGroup[]): number {
  return plan.reduce((total, group) => total + group.count, 0);
}

/** "同级重名 2 处、单子节点 3 处"。空计划是空串（调用方据此不渲染那一行）。 */
export function repairPlanLine(plan: readonly RepairGroup[]): string {
  return plan.map(group => `${group.label} ${group.count} 处`).join('、');
}

/**
 * 修完之后逐条说"改了什么"时，同一类动作不必把 504 条全部铺开。
 *
 * 与面板上诊断/建议那两处同一个处置（`ATTENTION_LIMIT` / `SUGGESTION_LIMIT`）：
 * 上限只是**折叠**，不是丢弃——剩下的永远点得到。数字来自真图：`机器学习的主要方向`
 * 那张 561 节点的图一次会产生 504 条合并。
 */
export const CHANGE_LIMIT = 8;
