import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

import { checkOptimization, repairOptimization, suggestOptimization } from '../api/client';
import type { DiagramContent, Source } from '../api/types';
import {
  diagnosticLabel,
  normalizeDiagnostics,
  normalizeSuggestions,
  structureCounts,
  type DiagnosticNote,
  type SuggestionNote,
} from '../mindmap/diagnostics';
import { repairPlan, repairPlanTotal } from '../mindmap/repair';
import { findIn, isTree } from '../mindmap/shape';
import { useMindMapStore } from './mindmap';

/**
 * 「应用」这条动作的分派表：`kind` → 具体做什么。
 *
 * **表外的一律没有按钮**（`APPLICABLE_KINDS` 是同一份清单的两面）。加一种能自动应用
 * 的建议类型时，改两处：这里加一项、`diagnostics.ts` 的白名单加一个 kind。
 *
 * 现在只有 `dedupe`：把那个重复的节点删掉。`merge` / `link` / `fill` / `drop` 后端
 * 还没给出"应用它到底改成什么"的定义（合并进哪个节点？补什么环节？），所以界面只
 * 显示。给它们摆按钮的结果是：点了要么没反应、要么替后端猜一个它没定义的操作。
 */
const APPLIERS: Record<string, (nodeId: string) => void> = {
  dedupe: nodeId => useMindMapStore().deleteNode(nodeId),
};

/**
 * 结构检查（`/optimization/check` 与 `/optimization/suggest`）的状态。
 *
 * ## 为什么它是独立的 store，而不是塞进 `useMindMapStore`
 *
 * 这两条函数描述的是"**画布内容的一份体检报告**"，与画布本身的生命周期不同：
 * 画布每次编辑都换一个 `content` 引用，而体检是**某一刻**的快照。分开之后那件事
 * 可以被如实说出来（`stale`），而塞进 mindmap store 的话，它迟早会被当成"实时的
 * 结论"来读——于是用户改了两笔，面板上还挂着"同级重名"，而那个重名已经被他改掉了。
 *
 * ## 两条路径，成本不同
 *
 *  - **自动**：生成落地之后跑一次 `check`，**不带 sources**。它只做结构三件套
 *    （单子节点 / 深度上限 / 同级重名），不落库、不调模型，请求体是那棵树。
 *  - **手动**：用户点「检查结构」时跑 `check` + `suggest`，**带上 sources**。带上之后
 *    后端会按原文核验每条引用（第三层），那条链是"点引用能跳回原文"的依据；
 *    代价是请求体里有材料全文，所以它不该在用户没点的时候发生。
 *
 * 自动路径刻意不跑 `suggest`：它现在还是纯规则（不花钱），但后端正在给它加模型层，
 * 那时"每次生成完自动跑"就变成了"每次生成完自动花一次模型调用"。界线画在"用户点了
 * 才发"这一侧，后端加成本时前端不用回来改。
 */
export const useStructureStore = defineStore('structure', () => {
  const map = useMindMapStore();

  const diagnostics = ref<DiagnosticNote[]>([]);
  const suggestions = ref<SuggestionNote[]>([]);

  /**
   * 用户明确说过"这条不用管"的键（`DiagnosticNote.key` / `SuggestionNote.key`）。
   *
   * **只在本会话内有效，不写库**：一次拒绝不需要留下任何记录（与偏好建议"点不用
   * 什么都不发生"同一条口径）。而它必须挡住"重新检查之后又冒出来"——后端的规则是
   * 纯函数，同一份内容重跑得到同一个 message，于是键也相同。
   */
  const dismissed = ref<Set<string>>(new Set());

  /** 正在检查。它只控制按钮的禁用态与一句"检查中"，不挡画布。 */
  const busy = ref(false);

  /** 上一次检查错在哪；空串表示没错。**不写进状态行**：它是抽屉里的一句提示。 */
  const error = ref('');

  /** 上一次检查跑完了没有。空结果与"没检查过"必须分开，见 `neverRun`。 */
  const ran = ref(false);

  /**
   * 这份报告是**哪一份内容**的体检结论（引用相等）。
   *
   * 它是 `stale` 的唯一判据：画布内容换过引用之后，报告说的就不是眼前这张图了。
   * 用引用而不是比较序列化：`applyContent` 每次整体替换 `content.value`，引用相等
   * 恰好等于"一个字都没变"（与 `generatedContent` / `editBase` 同一条口径）。
   */
  const checkedContent = ref<DiagramContent | null>(null);

  /** 这次报告里包不包含 `suggest`（手动路径才包含）。界面据此说清"应用"为什么不在。 */
  const withSuggestions = ref(false);

  /** 请求令牌：花名册外的响应（用户已经又点了一次 / 换了图）一律丢弃。 */
  let token = 0;

  /** 忽略过的条目从这里被滤掉，而不是从数组里删——重新检查之后它们还要被挡住。 */
  const visibleDiagnostics = computed(
    () => diagnostics.value.filter(note => !dismissed.value.has(note.key)),
  );
  /**
   * 一条建议指向的节点还在不在。
   *
   * 报告是**某一刻的快照**：用户可能已经手动删掉了那个节点。留一条指向"不存在的
   * 节点"的建议，点「应用」只会换来一句"已经不在图里了"——那它一开始就不该显示。
   *
   * 它顺手还挡掉了应用之后的一种复活：删掉节点之后再重跑，如果后端仍给出同一条
   * 建议（规则比想象中宽松、或者它指的是另一个同 id 的节点），这里会先把那条挡在
   * 外面——"点了应用它还在"是最让人不信任这个面板的形状。
   */
  function stillLive(note: SuggestionNote): boolean {
    if (!note.nodeId) return true;
    const content = map.content;
    if (!content) return true;
    return findIn(content, note.nodeId) !== null;
  }

  /**
   * `dedupe` 的「应用」= **删掉那个节点**，所以只对叶子放行。
   *
   * 这条限制是后端这一版加出 `NEAR_DUPLICATE_LABEL` 之后必须有的：那条诊断的文案是
   * "只多了「有」这 1 个字，请确认是否指的是同一件事"，它把这个判断**交给了人**，
   * 而它给出的建议 kind 同样是 `dedupe`。
   *
   * 于是"应用它"在两种情形下后果完全不同：
   *   · 完全重名的两个叶子 → 删掉多余的那个，零损失；
   *   · 近义的两个节点，其中一个下面还有 5 个节点 → 删掉就**丢了那一支**。而按钮上
   *     只写着「应用」，用户以为自己是在合并。
   *
   * 真正的合并（把子节点搬到保留的那个下面）需要人决定"并进哪一个、深度还对不对"，
   * 那是另一次编辑，不是一次点击。所以这里**只对叶子给按钮**，其余说清原因——
   * 少一个能点的按钮，好过一次静默丢内容。
   */
  function leafOnly(note: SuggestionNote): SuggestionNote {
    if (!note.applicable || !note.nodeId) return note;
    const content = map.content;
    if (!content) return note;
    const node = findIn(content, note.nodeId);
    if (!node || node.children.length === 0) return note;
    return {
      ...note,
      applicable: false,
      blockedReason: `这一支下面还有 ${node.children.length} 个节点：删掉会丢内容，`
        + '要合并请先把它下面的节点挪过去',
    };
  }

  /** 忽略过的、指向已不存在节点的、以及**有子节点不能一键删除**的，都在这里被标注/滤掉。 */
  const visibleSuggestions = computed(
    () => suggestions.value
      .filter(note => !dismissed.value.has(note.key))
      .filter(stillLive)
      .map(leafOnly),
  );

  /**
   * 状态行与按钮读的那三个数。
   *
   * 从**过滤之后**的列表算：用户点了忽略，状态行上的数字必须立刻跟着减——否则
   * "3 条建议"会一直挂着，点进去只剩两条，用户会以为界面在骗他。
   */
  const counts = computed(() => structureCounts(visibleDiagnostics.value, visibleSuggestions.value));

  /** 这件事有必要让用户看见吗（状态行/入口按钮据此存在）。 */
  const hasFindings = computed(() => counts.value.attention > 0 || counts.value.suggestions > 0);

  /**
   * 报告过期了：画布内容已经与体检那一刻不同。
   *
   * **不自动重跑**。自动重跑会在用户每改一个字时发一次请求，而编辑是连续动作；
   * 更重要的是，"过期"这件事本身是**该被说出来**的：用户改掉那个重名之后，面板上
   * 挂着"同级重名"而没有标记，他就会以为界面在说他没改过。
   */
  const stale = computed(
    () => checkedContent.value !== null && map.content !== checkedContent.value,
  );

  /** 从来没检查过。空列表 + `ran=false` 与"检查了，一条都没有"是两件事。 */
  const neverRun = computed(() => !ran.value);

  /**
   * 一次体检。
   *
   * `tree` 之外的内容直接返回 `'unsupported'`：后端 `optimization/service.py::check()`
   * 的签名只认 `MindMapContent`（它直接读 `content.root`），鱼骨与流程传过去会在服务端
   * 炸成 500。那不是前端能修的事（后端正在改），所以这里拦在发请求之前，并给出一句
   * 能读懂的话——比让用户看见一个 500 好。
   */
  async function run(options: { withSuggest?: boolean; sources?: Source[] } = {}): Promise<
    'ok' | 'unsupported' | 'empty' | 'failed'
  > {
    const content = map.content;
    if (!content) return 'empty';
    if (!isTree(content)) {
      diagnostics.value = [];
      suggestions.value = [];
      ran.value = true;
      checkedContent.value = content;
      withSuggestions.value = false;
      error.value = '结构检查现在只支持思维导图：后端的诊断只遍历树的根节点，鱼骨图与流程图还进不去。';
      return 'unsupported';
    }

    const mine = ++token;
    busy.value = true;
    error.value = '';
    const withSuggest = options.withSuggest === true;
    try {
      if (withSuggest) {
        const result = await suggestOptimization(content, options.sources);
        if (mine !== token) return 'ok';
        diagnostics.value = normalizeDiagnostics(result.diagnostics);
        suggestions.value = normalizeSuggestions(result.suggestions);
      } else {
        const result = await checkOptimization(content, options.sources);
        if (mine !== token) return 'ok';
        diagnostics.value = normalizeDiagnostics(result.diagnostics);
        // 自动路径不清建议：用户手动看过的那批候选还在，下一次生成落地时它们才作废
        // （见 `reset`）。这里清掉会让"刚看完的建议"被任意一次自动检查抹去。
      }
      if (mine !== token) return 'ok';
      withSuggestions.value = withSuggest || withSuggestions.value;
      ran.value = true;
      checkedContent.value = content;
      return 'ok';
    } catch (caught) {
      if (mine !== token) return 'ok';
      // 体检失败不改任何既有结论：它是一份附加信息，拿不到就当没拿到。
      error.value = caught instanceof Error ? caught.message : '结构检查没跑成，请重试。';
      return 'failed';
    } finally {
      if (mine === token) busy.value = false;
    }
  }

  /** 自动路径：生成落地之后跑一次免费的结构体检（不带 sources、不跑 suggest）。 */
  function checkAfterGenerate(): void {
    void run();
  }

  /** 手动路径：「检查结构」按钮。带上材料，因此第三层引用核验也会跑。 */
  function runFull(sources?: Source[]): void {
    void run({ withSuggest: true, sources });
  }

  /**
   * 一条建议的**应用动作**，按 `kind` 分派。
   *
   * 白名单在 `mindmap/diagnostics.ts::APPLICABLE_KINDS` 里——表外的一律到不了这里
   * （`note.applicable` 为假）。现在只有 `dedupe`：删掉那个重复的节点。
   *
   * 三条规矩：
   *  1. **绝不自动应用**。只有用户点「应用」才到这里——后端也没有自动应用接口。
   *  2. **走现有的编辑路径**（`map.deleteNode`）：它压撤销栈、清悬空边、把选中挪到
   *     父节点。绕过去自己改 `content` 就等于造了一条不能 Ctrl+Z 的修改。
   *  3. 应用之后**本地移除那条建议**，再顺手重跑一次体检：删掉之后后端会给出新的
   *     结论（可能还剩一条重名），而重跑失败也不影响"它已经不在了"这件事。
   */
  function applySuggestion(note: SuggestionNote): boolean {
    if (!note.applicable || !note.nodeId) return false;
    const content = map.content;
    if (!content) return false;
    const node = findIn(content, note.nodeId);
    // 节点已经不在了（用户先手动删了，或者这是一份过期报告）：真话说出来，而不是
    // 假装应用成功。
    if (!node) {
      map.status = '这条建议指向的节点已经不在图里了，重新检查一次。';
      return false;
    }
    if (map.isContainerRootId(note.nodeId)) {
      map.status = '这条建议指向的是中心主题 / 骨 / 阶段本身，不能删。';
      return false;
    }
    // 同一道闸门在动作这一层再判一次：按钮的显隐由 `visibleSuggestions` 决定，
    // 但那只是一层 UI；`applySuggestion` 是公开方法（测试、将来的快捷键都会直调），
    // 删掉一棵有内容的子树是不可撤销之外的损失（撤销也只救回"这一次会话"）。
    if (node.children.length > 0) {
      map.status = `「${node.label.trim()}」下面还有 ${node.children.length} 个节点，`
        + '要合并请先把它下面的内容挪过去。';
      return false;
    }

    const applier = APPLIERS[note.kind];
    if (!applier) return false;
    applier(note.nodeId);

    suggestions.value = suggestions.value.filter(item => item.key !== note.key);
    // 那条诊断（`DUPLICATE_LABEL`）说的是同一个节点，一起收掉：留着它就是在提醒
    // 一件已经做完的事。
    diagnostics.value = diagnostics.value.filter(item => item.nodeId !== note.nodeId);
    map.status = `已按建议删除重复节点「${node.label.trim()}」，可撤销。`;
    // 不带 sources 重跑：这是一次"看看还剩什么"的复核，没必要把材料全文再发一遍。
    void run({ withSuggest: true });
    return true;
  }

  /** 「忽略」：不写库，本会话内不再出现（见 `dismissed`）。 */
  function dismiss(key: string): void {
    const next = new Set(dismissed.value);
    next.add(key);
    dismissed.value = next;
  }

  // ---------------------------------------------------------------- 一键修复

  /** 正在修。它只挡按钮的重复点击，不挡画布——修复是一次往返，不该把编辑锁住。 */
  const repairing = ref(false);

  /**
   * 修完之后那份"改了什么"的清单。
   *
   * `content` 是**这份清单说的是哪一棵树**（引用相等），界面据此说清"你撤销过 / 又改过
   * 了，下面这些已经不是眼前这张图的样子"。用引用而不是比较序列化，与 `checkedContent`
   * 同一条口径：`applyContent` 每次整体替换 `content.value`，引用相等恰好等于"一个字
   * 都没变"。
   */
  const repairReport = ref<{
    content: DiagramContent;
    steps: number;
    summary: string;
    changes: { code: string; label: string; message: string }[];
    skipped: { code: string; label: string; reason: string }[];
  } | null>(null);

  /** 那份清单还准不准（`false` = 修完之后又撤销过、或者又改了）。 */
  const repairReportCurrent = computed(
    () => !!repairReport.value && repairReport.value.content === map.content,
  );

  /**
   * 能自动修的那几处，按码分组。
   *
   * 从 `visibleDiagnostics` 算（忽略过的不算），理由见 `mindmap/repair.ts::repairPlan`。
   * 它是**按钮出不出现**的判据——一份 `check` 就够，不必先跑一次 `suggest`。
   */
  const repairablePlan = computed(() => repairPlan(visibleDiagnostics.value));
  const repairableCount = computed(() => repairPlanTotal(repairablePlan.value));

  /**
   * 一键修到底（`POST /optimization/repair`），然后把修好的树**整体装进画布**。
   *
   * ## 与 `applySuggestion` 的分工
   *
   * `applySuggestion` 是"一条建议 → 一个节点的动作"（现在只有删叶子）。这里是"三条
   * 修法唯一的规则 → 整棵树跑到不动点"。后者只能整体替换，因为一次修复会删节点、搬
   * 子树、改标题——增量补丁描述不了它（后端 `RepairResponse` 的注释也是这么写的）。
   *
   * ## 落地必须走 `map.replaceContent`，不能直接改 `content`
   *
   * 那一行是这次接线唯一的难点：整体替换要进撤销栈，而且**只进一步**。绕过它手写
   * `content = next` 能改，但不可撤销、`dirty` 与 `clientRevision` 不更新、改动账本
   * 也记不上。粒度为什么是一条，见 `stores/mindmap.ts::replaceContent`。
   *
   * ## 在途期间图被改过 → 不落地
   *
   * 请求飞出去之后用户可能又改了两笔（画布在修的时候不锁）。拿回来的树是按**旧**那棵
   * 算的，装进去等于把用户手里的两笔静默丢掉。所以落地前比一次引用：变了就放弃这次
   * 结果并说清原因，而不是报"应用成功"。
   *
   * ## 落地之后
   *
   * 后端响应里带着**修完之后重跑的 `check()`**（`diagnostics`），直接拿来用，不再多打
   * 一次 `/check`：树是我们刚装进去的那一棵，重跑只可能得到同一份结论。建议列表不清空
   * ——指向已消失节点的那些会被 `stillLive` 滤掉，"还剩几条"仍然是真的。
   */
  async function applyRepair(): Promise<
    'ok' | 'none' | 'empty' | 'unsupported' | 'stale' | 'failed'
  > {
    if (repairing.value) return 'none';
    const content = map.content;
    if (!content) return 'empty';
    if (!isTree(content)) {
      error.value = '自动修复现在只支持思维导图：后端那三条规则读的是树的根节点。';
      return 'unsupported';
    }

    repairing.value = true;
    error.value = '';
    try {
      const result = await repairOptimization(content);

      // 在途期间画布被换掉了：这份结果属于旧那棵树。与 `commitGenerated` 的冲突判定
      // 同一条判据（引用相等），只是这里不保留两棵树——修复可以再点一次。
      if (map.content !== content) {
        error.value = '这张图在点了修复之后又改过，这次的结果没有应用。请重新检查再修。';
        return 'stale';
      }

      // 一处都没修：不动画布（连历史都不压），只把真相说出来。真图实测 41/58 张属于
      // 这一类，所以这句话不能写成"修复失败"。
      if (!result.steps) {
        diagnostics.value = normalizeDiagnostics(result.diagnostics);
        ran.value = true;
        checkedContent.value = content;
        map.status = result.skipped.length
          ? `${result.skipped.length} 处没能自动修（超出契约上限或诊断过期）。`
          : '没有可以自动修的地方。';
        return 'none';
      }

      if (!map.replaceContent(result.content)) return 'empty';

      diagnostics.value = normalizeDiagnostics(result.diagnostics);
      ran.value = true;
      // 报告说的就是眼前这棵树了：不更新的话面板上会挂一句"这张图在检查之后又改过"，
      // 而我们刚刚才核对过新树。
      checkedContent.value = map.content;
      repairReport.value = {
        content: map.content,
        steps: result.steps,
        summary: result.summary,
        changes: result.applied.map(item => ({
          code: item.code,
          label: diagnosticLabel(item.code),
          message: item.message,
        })),
        skipped: result.skipped.map(item => ({
          code: item.code,
          label: diagnosticLabel(item.code),
          reason: item.reason,
        })),
      };
      map.status = `已自动修复 ${result.steps} 处`
        + `${result.summary ? `：${result.summary}` : ''}，可撤销。`;
      return 'ok';
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : '自动修复没跑成，请重试。';
      return 'failed';
    } finally {
      repairing.value = false;
    }
  }

  /**
   * 报告作废（换了一张图、或者新一轮生成落地）。
   *
   * `dismissed` 也一起清：那些键属于上一张图 / 上一轮内容，留着只会在新图上偶然
   * 挡住一条恰好同键的诊断——而"我明明没忽略过这条"是最难查的一类问题。
   */
  function reset(): void {
    token += 1;
    diagnostics.value = [];
    suggestions.value = [];
    dismissed.value = new Set();
    error.value = '';
    ran.value = false;
    busy.value = false;
    withSuggestions.value = false;
    checkedContent.value = null;
    repairing.value = false;
    // 换图之后那份"刚修了什么"属于上一棵树：留着它就是在新图上说旧话。
    repairReport.value = null;
  }

  return {
    diagnostics,
    suggestions,
    visibleDiagnostics,
    visibleSuggestions,
    dismissed,
    busy,
    error,
    ran,
    neverRun,
    stale,
    withSuggestions,
    counts,
    hasFindings,
    // 「有 warn 或 error 吗」：抽屉里 info 折叠区的标题用它决定怎么说
    attentionCount: computed(() => counts.value.attention),
    run,
    checkAfterGenerate,
    runFull,
    applySuggestion,
    dismiss,
    // 一键修复：能修几处（`repairablePlan`）、正在修、修完的清单与它是否还准
    repairing,
    repairablePlan,
    repairableCount,
    repairReport,
    repairReportCurrent,
    applyRepair,
    reset,
  };
});
