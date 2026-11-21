<script setup lang="ts">
import { computed, ref } from 'vue';

import { agentDetailVisible, runResultLine } from '../mindmap/run-status';
import { agentToolLabel, useMindMapStore, type PreviewNode, type ScopedChange } from '../stores/mindmap';

/**
 * agent 运行面板：工具进度、生成中的预览、生成落地之后"改了什么"、以及冲突裁决。
 *
 * **没有「应用」这一步了**：生成结果直接进画布并标成未保存（见 `store.commitGenerated`）。
 * 于是这个面板的职责从"让你确认一份提案"变成了"告诉你刚才发生了什么、以及怎么退回去"：
 * 生成中显示在长什么，生成完显示改了几处，想反悔就有「回到生成前」。
 */
const store = useMindMapStore();

/**
 * 可见性判据在 `mindmap/run-status.ts` 里：画布底部那条状态行要判断"有没有明细可看"，
 * 判断依据必须与这里一致（一边说有、另一边打开是空的，是界面坏了的形状）。
 */
const visible = computed(() => agentDetailVisible(store));

/** 毫秒 → 「4.3 秒」。把 4341ms 直接摆给用户看是工程口吻。 */
function seconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)} 秒`;
}

/**
 * 一行进度的文案：`{人话} · {耗时}`，进行中不带耗时。
 *
 * 工具名到人话的映射在 store 里（状态行也要用），这里只负责拼耗时；
 * 失败时保留工具名——那种时候用户和排查都需要能对上后端日志。
 */
function rowText(name: string, state: 'running' | 'done', ok: boolean, ms: number): string {
  if (state === 'running') return agentToolLabel(name, 'running');
  return ok ? `${agentToolLabel(name, 'done')} · ${seconds(ms)}` : `${name} 未成功`;
}

function failureReason(summary?: Record<string, unknown>): string {
  // 工具的摘要结构不同，只展示可读字段，避免把诊断对象或缺失值直接插进界面。
  const parts = [summary?.code, summary?.message]
    .filter((value): value is string => typeof value === 'string' && value.trim().length > 0);
  return parts.join('：') || '工具未返回具体失败原因。';
}

/** 只把工具调用摘出来显示；stage 文案已经有单独的状态行了。 */
interface ToolRow {
  key: string;
  state: 'running' | 'done';
  ok: boolean;
  /** 已经翻成人话的整行文案，模板不再拼字符串 */
  text: string;
  detail: string;
}

const calls = computed<ToolRow[]>(() => {
  // 按 call_id 归并：一次调用会先收到 tool_start 再收到 tool_end，
  // 直接映射成两行的话，界面上会出现"同一次调用既是执行中又已完成"。
  const order: string[] = [];
  const byId = new Map<string, ToolRow>();
  for (const event of store.agentEvents) {
    if (event.type === 'tool_start') {
      if (!byId.has(event.call_id)) order.push(event.call_id);
      byId.set(event.call_id, {
        key: event.call_id,
        state: 'running',
        ok: true,
        text: rowText(event.name, 'running', true, 0),
        detail: '',
      });
    } else if (event.type === 'tool_end') {
      if (!byId.has(event.call_id)) order.push(event.call_id);
      byId.set(event.call_id, {
        key: event.call_id,
        state: 'done',
        ok: event.ok,
        text: rowText(event.name, 'done', event.ok, event.latency_ms ?? 0),
        detail: event.ok ? '' : failureReason(event.result_summary),
      });
    }
  }
  return order.map(id => byId.get(id)!);
});

/**
 * 外部检索那一行：取**最后一条** `external_search` 阶段帧。
 *
 * 为什么单独拎出来而不是只靠状态栏：检索时长为 4–6 秒（最坏 20 秒），这段时间
 * 里工具行写着"正在生成导图"、画布不动，用户看不出后台在联网。开始帧与结束帧
 * 共用同一个 `stage`，所以"取最后一条"天然实现了"检索结束后那句话变成已找到 N 条"。
 *
 * `detail` 是后端给的人话，这里不再翻译：同一句话在状态栏与面板里显示成两种说法，
 * 用户会以为是两件事。
 */
const searchNote = computed(() => {
  for (let index = store.agentEvents.length - 1; index >= 0; index -= 1) {
    const event = store.agentEvents[index];
    if (event.type === 'stage' && event.stage === 'external_search') return event;
  }
  return null;
});

/** 生长中的预览里的一行。层级摊平成缩进，模板里就不用递归组件。 */
interface PreviewRow {
  key: string;
  label: string;
  depth: number;
  /** false 才做标记：null 是「模型还没写到」，画出来会和「明确不展开」混作一谈。 */
  expandable: boolean | null;
}

/** 预览最多列几行：生成中它还会继续长，列到几十行就不再是"先看一眼"了。 */
const PREVIEW_LIMIT = 24;

function countPreview(node: PreviewNode): number {
  return 1 + node.children.reduce((sum, child) => sum + countPreview(child), 0);
}

const previewRows = computed<PreviewRow[]>(() => {
  const root = store.agentPreview;
  if (!root) return [];
  const rows: PreviewRow[] = [];
  const walk = (node: PreviewNode, depth: number, key: string) => {
    if (rows.length >= PREVIEW_LIMIT) return;
    rows.push({ key, label: node.label, depth, expandable: node.expandable });
    node.children.forEach((child, index) => walk(child, depth + 1, `${key}.${index}`));
  };
  walk(root, 0, '0');
  return rows;
});

/** 根节点单独摆在标题行，列表从它的孩子开始。 */
const previewBranchRows = computed(() => previewRows.value.filter(row => row.depth > 0));
const previewTotal = computed(() => (store.agentPreview ? countPreview(store.agentPreview) : 0));
const hiddenPreview = computed(() => previewTotal.value - previewRows.value.length);

/**
 * 预览折叠区的标题：展开与生成是两件事，标题也该分开说。
 *
 * `agentPreviewScope` 只在展开时被设置（那是"正在展开哪个节点"），所以它同时是
 * "这一轮是不是展开"的判据——不必再读一次 `runIntent`。
 */
const previewTitle = computed(() => {
  const expand = store.agentPreviewScope !== null;
  if (store.agentRunning) return expand ? '展开中（预览）' : '生成中（预览）';
  return expand ? '上次展开中断时的预览' : '上次生成中断时的预览';
});

/**
 * 「已完成」那一行说的话。
 *
 * 三个信息缺一不可：`nodes` 是这次落进画布的规模，`changes` 是改了几处（`null` 表示
 * 画布本来是空的，整棵树都是新的——那时说"改了 0 处"是误导），外加"还没保存"。
 * 文案本体在 `runResultLine`：状态行上那句必须是同一句。
 */
const resultLine = computed(() => (store.generatedSummary ? runResultLine(store.generatedSummary) : ''));

/**
 * 这次生成改了什么。
 *
 * `null` 表示"没有可比基线"（画布本来是空的），空数组表示"算出来了，一处都没改"
 * ——两者必须分开：前者不该显示清单，后者要显示"这次什么都没改"。它是**落地那一刻
 * 冻结**的快照（`store.generatedChanges`），用户之后改画布不会改写它。
 */
const diff = computed<ScopedChange[] | null>(() => store.generatedChanges);

/**
 * 这次生成是不是带着选区发出去的。
 *
 * 为空表示"没圈任何节点"（或结果是从服务端捡回来的，见 `resumeRun`）——那时
 * 「范围内/外」不成立，标记整片不显示。没选区却显示一个"范围外"，是纯粹的噪音，
 * 还会让人以为模型越权了。
 */
const showScope = computed(() => store.generationSelection.length > 0);

/** 改动清单最多列几条：再往下列就不是"看一眼改了什么"，而是让用户读变更日志。 */
const DIFF_LIMIT = 8;

/**
 * 范围外的改动排前面。
 *
 * 清单只列 8 条，其余进"……还有 N 处"。用户最需要看见的恰恰是那条越界的改动
 * （"我只圈了三个节点，它怎么动了这一支？"），而它按原始顺序很可能正好被截断
 * 在那个省略号里。
 */
const shownDiff = computed(() => {
  const all = diff.value ?? [];
  if (!showScope.value) return all.slice(0, DIFF_LIMIT);
  return [
    ...all.filter(item => !item.inSelection),
    ...all.filter(item => item.inSelection),
  ].slice(0, DIFF_LIMIT);
});

const hiddenDiff = computed(() => Math.max(0, (diff.value?.length ?? 0) - shownDiff.value.length));

/** 有多少处落在当时那个选区里。 */
const inScopeCount = computed(() => (diff.value ?? []).filter(item => item.inSelection).length);

/** 有多少处改到了选区之外——**不是错误**，是模型扩权了，用户要能一眼看见。 */
const outScopeCount = computed(() => (diff.value?.length ?? 0) - inScopeCount.value);

/** 清单下面那句范围小结。没选区时是空串（整段 `v-if` 一起不渲染）。 */
const scopeSummary = computed(() => {
  if (!showScope.value) return '';
  const total = diff.value?.length ?? 0;
  const head = `带着「已选 ${store.generationSelection.length} 个节点」发起：`
    + `${total} 处改动里 ${inScopeCount.value} 处在选中范围内`;
  return outScopeCount.value > 0
    ? `${head}，${outScopeCount.value} 处改到了范围外——那是模型判断必须一起动的地方，看一眼对不对。`
    : `${head}，没有改到范围外。`;
});

/** 六种改动的中文标签。用徽标而不是句子：一屏能扫完"哪几处被动了"。 */
const DIFF_LABELS: Record<ScopedChange['kind'], string> = {
  added: '新增',
  removed: '删除',
  updated: '修改',
  moved: '移动',
  // 边单独两个徽标：连线不改层级，"新增"与"新增关系"混在一起时，用户分不清
  // 清单上这一条是要去看节点内容还是去看流程走向。
  edge_added: '连关系',
  edge_removed: '断关系',
};

/** 颜色也在这里区分一次：删除用玫红，新增用翠绿，其余中性。 */
const DIFF_CLASS: Record<ScopedChange['kind'], string> = {
  added: 'bg-emerald-100 text-emerald-700',
  removed: 'bg-rose-100 text-rose-700',
  updated: 'bg-amber-100 text-amber-800',
  moved: 'bg-sky-100 text-sky-700',
  edge_added: 'bg-indigo-100 text-indigo-700',
  edge_removed: 'bg-orange-100 text-orange-700',
};

/**
 * 「在不在你圈的范围里」两枚标记的样式。
 *
 * 刻意不用实心色块（六种改动已经各占一色）：范围是**第二维**信息，与"改了什么"
 * 叠在同一行。范围外只用淡红描边：那是"看一眼"的语气，不是"出错了"——模型扩权
 * 很可能是对的（用户说「整张图都加上时间维度」时，它本来就该改全图）。
 */
const IN_SCOPE_CLASS = 'bg-slate-100 text-slate-600';
const OUT_SCOPE_CLASS = 'bg-rose-50 text-rose-600 ring-1 ring-rose-200';

/**
 * 点一条改动就跳到那个节点。
 *
 * 删除的节点已经不在画布上了，`locate` 会静默不选中它——这是刻意的：为了跳转方便
 * 就往 `content` 里塞一个已删除的节点，等于让"画布上有什么"和"用户的作品是什么"
 * 分叉。
 */
function jumpTo(id: string) {
  store.locate(id);
}

/**
 * 收尾那一帧（最后一个 `done`）带回来的模型原话与思考过程。
 *
 * 取最后一个 `done`：一轮里可能有多帧，只有最后一帧是"这次结束的样子"。
 * 这里**只负责取**，怎么解释由下面两个判据决定。
 */
interface RunTail {
  text: string;
  reasoning: string;
  /** 这一帧自己带了导图内容没有。 */
  hasContent: boolean;
}

const runTail = computed<RunTail | null>(() => {
  const events = store.agentEvents;
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i];
    if (event.type !== 'done') continue;
    return {
      text: event.text ?? '',
      reasoning: event.reasoning ?? '',
      hasContent: !!event.content,
    };
  }
  return null;
});

/** 这一轮真的产出导图了吗。 */
const produced = computed(() => runTail.value?.hasContent === true
  || !!store.generatedSummary
  // 生成中的预览帧本身也是产出：模型已经把那棵树写出来了，只是还在收尾。
  || store.agentEvents.some(event => event.type === 'snapshot' || event.type === 'ops'));

/**
 * 「模型只回了文字」那条提示的判据。
 *
 * **三个条件缺一不可**：没产出、没有任何工具调用记录、而且它确实说了/想了点什么。
 * 只按"有思考内容"判会误伤最深的一种成功：勾了「深度思考」之后，收尾帧成功时
 * 也带 `reasoning`，导图明明已经落到画布上了，面板却宣布"没有调用工具"——
 * 用户会以为这次白跑了。
 *
 * 有工具调用记录却仍没产出时同样不显示这句：那时它是"调了但没成"，失败原因在
 * 上面那几行工具进度里，说"没调用工具"是把用户引到错的方向。
 */
const textOnlyNote = computed<RunTail | null>(() => {
  const tail = runTail.value;
  if (!tail || produced.value || calls.value.length) return null;
  if (!tail.text && !tail.reasoning) return null;
  return tail;
});

/**
 * 结果不完整的证据：partial 标记单独摆在标题行，诊断逐条列 message。
 *
 * 后端在 `ok=true` 的同时给出这些信息，界面只认 ok 的话，「3 个分支失败」
 * 和「全部成功」长得一模一样——而树已经在画布上了，用户改到一半才发现某几支是空的。
 */
const partialWarning = computed(
  () => store.agentWarnings.find(item => item.kind === 'partial') ?? null,
);
const diagnostics = computed(() => store.agentWarnings.filter(item => item.kind === 'diagnostic'));

/** 最多列三条诊断：再往下列就不是提个醒，而是让用户读长文了。 */
const DIAGNOSTIC_LIMIT = 3;

/**
 * 「展开全部」。
 *
 * 前三条足够让人判断"要不要现在处理"，但**不是**全部真相：4 条以上时原来只写
 * 「还有 N 条」，没有展开入口——用户知道有东西却看不到（实测：状态行说"有诊断提示"，
 * 界面上找不到那 N 条在哪）。折叠的默认值不变，缺的只是那条出路。
 */
const allDiagnosticsOpen = ref(false);
const shownDiagnostics = computed(() => (
  allDiagnosticsOpen.value ? diagnostics.value : diagnostics.value.slice(0, DIAGNOSTIC_LIMIT)
));
const hiddenDiagnostics = computed(() => diagnostics.value.length - shownDiagnostics.value.length);
const canExpandDiagnostics = computed(() => diagnostics.value.length > DIAGNOSTIC_LIMIT);

/**
 * 失败卡片：标题 + 下一步 + 按钮（判据在 `mindmap/failures.ts`）。
 *
 * 这里不再直接把后端那句原话当文案。那句话是写给写代码的人看的——「这次运行
 * 超过了时间上限」读完既不知道要不要重试，也不知道该改什么。原话仍然留在卡片
 * 里（折在 `<details>` 后面），因为排障时它是唯一的线索，但它不该是用户读到的
 * 第一句话。
 */
const guide = computed(() => store.failureGuide);

/** `agentRunning` 期间按钮要禁用：连点两次会开两轮生成（互相作废）。 */
function actionDisabled(action: { disabledReason?: string }): boolean {
  return !!action.disabledReason;
}

function runAction(kind: 'retry' | 'copy'): void {
  if (kind === 'retry') {
    void store.retryLastRun();
    return;
  }
  // 复制失败时**不弹任何提示**：能点这个按钮的人正在排障，剪贴板被浏览器拒绝
  // （非 HTTPS、无用户手势以外的原因）他也只好手工选中——再多一条错误提示只是噪音。
  void navigator.clipboard?.writeText(rawFailure.value).catch(() => {});
}

/**
 * 复制走的那段文字：后端原话 + 分类码。
 *
 * 只复制原话不够——"模型调用次数已用完"这七个字在日志里搜不到，`code` 与
 * `limit` 才是能对上后端记录的键。
 */
const rawFailure = computed(() => {
  const parts = [store.agentError];
  if (store.agentErrorCode) parts.push(`code=${store.agentErrorCode}`);
  if (store.agentErrorLimit) parts.push(`limit=${store.agentErrorLimit}`);
  return parts.join(' · ');
});

/**
 * 冲突裁决：`generated` = 用模型的结果，`mine` = 保留画布。
 *
 * 这条分支正常永远不出现（生成期间画布被 `busy` 锁着），它是数据要丢时的兜底。
 */
function resolve(choice: 'generated' | 'mine') {
  store.resolveConflict(choice);
}

function undoGenerate() {
  store.undoGenerate();
}

function cancel() {
  store.cancel();
}
</script>

<template>
  <section
    v-if="visible"
    class="rounded-md border border-slate-200 bg-slate-50 p-2 text-xs"
    aria-label="模型协作进度"
  >
    <div class="flex items-center justify-between">
      <h3 class="font-medium text-slate-700">
        {{ store.agentRunning ? '正在生成' : '模型协作' }}
      </h3>
      <button
        v-if="store.agentRunning"
        type="button"
        class="text-slate-500 hover:text-slate-900"
        @click="cancel"
      >取消</button>
    </div>

    <p v-if="searchNote" class="mt-1 text-[11px] leading-relaxed text-slate-500">
      {{ searchNote.detail }}
    </p>

    <ol v-if="calls.length" class="mt-2 flex flex-col gap-1">
      <li
        v-for="call in calls"
        :key="call.key"
        class="rounded bg-white px-2 py-1"
      >
        <span :class="call.state === 'running' ? 'text-slate-500' : call.ok ? 'text-emerald-700' : 'text-rose-700'">
          {{ call.text }}
        </span>
        <p v-if="call.detail" class="mt-0.5 break-words text-[11px] leading-relaxed text-rose-700">
          {{ call.detail }}
        </p>
      </li>
    </ol>

    <!-- 失败卡片：说清发生了什么、下一步能做什么。
         从前这里就是后端那句原话，没有标题、没有下一步、也没有按钮——用户读完
         既不知道要不要重试，也不知道该改什么。原话折在下面给排障的人看。 -->
    <div
      v-if="guide"
      role="alert"
      class="mt-2 rounded border border-rose-200 bg-rose-50 px-2 py-1.5 leading-relaxed text-rose-900"
    >
      <p class="font-medium">{{ guide.title }}</p>
      <p class="mt-1 break-words text-[11px]">{{ guide.next }}</p>
      <div class="mt-1.5 flex flex-wrap items-center gap-1.5">
        <button
          v-for="action in guide.actions"
          :key="action.kind"
          type="button"
          class="rounded border px-2 py-0.5 text-[11px] disabled:cursor-not-allowed disabled:opacity-50"
          :class="action.kind === 'retry'
            ? 'border-rose-300 bg-white text-rose-800 hover:bg-rose-100'
            : 'border-slate-300 bg-white text-slate-600 hover:bg-slate-100'"
          :disabled="actionDisabled(action)"
          :data-test="`failure-${action.kind}`"
          @click="runAction(action.kind)"
        >{{ action.label }}</button>
      </div>
      <!-- 禁用原因必须写出来。只把按钮置灰等于沉默：用户看着一个点不动的「重试」，
           只能猜是自己点错了。被禁用的按钮在多数浏览器里连 title 悬停提示都不弹。 -->
      <p
        v-for="action in guide.actions.filter(item => item.disabledReason)"
        :key="`why-${action.kind}`"
        class="mt-1 text-[11px] text-rose-700"
        :data-test="`failure-why-${action.kind}`"
      >{{ action.disabledReason }}</p>
      <!-- 原话与分类码：排障时唯一的线索，但不该是用户读到的第一句话。
           开关只交给 `<details>` 自己管：`:open` 绑一个 ref、`@toggle` 再写回这个
           ref，等于两个源同时决定它开不开——点开一次会触发两轮更新，收起时浏览器
           与状态还可能各说各话。默认收起本来就是我们要的初始态，不需要 ref 代劳。 -->
      <details class="mt-1">
        <summary class="cursor-pointer text-[11px] text-slate-500">后端原话</summary>
        <p class="mt-1 whitespace-pre-wrap break-words text-[11px] text-slate-600">{{ rawFailure }}</p>
      </details>
    </div>

    <!--
      模型的思考过程：**与有没有产出无关**，独立成块。
      走推理模型时它就是用户勾「深度思考」要看的东西，成功时反而藏起来等于那个开关
      白勾了；而无产出时它是排障的唯一现场（日志未必捞得到）。
    -->
    <details
      v-if="runTail?.reasoning"
      data-test="run-reasoning"
      class="mt-2 rounded border border-slate-200 bg-white px-2 py-1.5"
    >
      <summary class="cursor-pointer text-[11px] text-slate-500">模型当时在想什么</summary>
      <p class="mt-1 max-h-64 overflow-y-auto whitespace-pre-wrap break-words text-[11px] leading-relaxed text-slate-600">
        {{ runTail.reasoning }}
      </p>
    </details>

    <!-- 模型只回了文字、没调工具、也没产出导图：把它的原话摆出来。线上排障时日志
         未必捞得到，这里是唯一能看到"它到底说了什么"的地方。 -->
    <div
      v-if="textOnlyNote"
      role="status"
      class="mt-2 rounded border border-amber-200 bg-amber-50 px-2 py-1.5 leading-relaxed text-amber-900"
    >
      <p class="font-medium">模型这次只回了文字，没有调用工具</p>
      <p v-if="textOnlyNote.text" class="mt-1 whitespace-pre-wrap break-words text-[11px]">
        {{ textOnlyNote.text }}
      </p>
      <p v-else class="mt-1 text-[11px]">（回复里没有正文）</p>
    </div>

    <!-- 部分完成与诊断：放在结果上方，用户先看到"这棵树不完整"，再去改。 -->
    <div
      v-if="store.agentWarnings.length"
      role="status"
      class="mt-2 rounded border border-amber-200 bg-amber-50 px-2 py-1.5 leading-relaxed text-amber-800"
    >
      <p v-if="partialWarning" class="font-medium">
        部分完成<span class="font-normal">：{{ partialWarning.message }}</span>
      </p>
      <p v-else class="font-medium">
        这次生成有 {{ diagnostics.length }} 条诊断提示
      </p>
      <ul v-if="shownDiagnostics.length" class="mt-1 flex flex-col gap-0.5 text-[11px]">
        <li v-for="item in shownDiagnostics" :key="`${item.code}|${item.message}`" class="break-words">
          {{ item.message }}
        </li>
      </ul>
      <!-- 折叠之后必须留一条**能走到全部**的路：只写"还有 N 条"等于把后几条锁起来。 -->
      <button
        v-if="canExpandDiagnostics"
        type="button"
        data-test="diagnostics-expand"
        class="mt-1 text-[11px] underline decoration-dotted underline-offset-2 hover:text-amber-900"
        :aria-expanded="allDiagnosticsOpen"
        @click="allDiagnosticsOpen = !allDiagnosticsOpen"
      >{{ allDiagnosticsOpen ? '收起' : `展开全部 ${diagnostics.length} 条（还有 ${hiddenDiagnostics} 条）` }}</button>
    </div>

    <!-- 生成中的预览：节点边跑边上屏，用户先看清树在往哪个方向长。
         它和最终结果可能不一致（分片失败会被裁掉），那一句就写在块尾。 -->
    <details
      v-if="store.agentPreview"
      open
      class="mt-2 rounded bg-white px-2 py-1"
    >
      <summary class="cursor-pointer font-medium text-slate-700">
        {{ previewTitle }}
      </summary>
      <!--
        展开时预览的顶部是「正在展开哪个节点」，不是中心主题：链上那几层祖先只是
        为了让 `node_grow` 的索引链有落点（`seedExpandPreview`），把根的标题摆在
        这里会让用户以为整张图在重生成。
      -->
      <p class="mt-1 break-words font-medium text-slate-700">
        {{ store.agentPreviewScope ? `正在展开：${store.agentPreviewScope}` : store.agentPreview.label }}
      </p>
      <ul
        v-if="previewBranchRows.length"
        class="mt-1 flex flex-col gap-0.5 text-[11px] leading-relaxed text-slate-600"
      >
        <li
          v-for="row in previewBranchRows"
          :key="row.key"
          class="break-words"
          :style="{ paddingLeft: `${(row.depth - 1) * 12}px` }"
        >
          <!-- 只有模型明确写了「不用再拆」才加这个点；null 与 true 都不加。 -->
          <span
            v-if="row.expandable === false"
            class="mr-1 inline-block h-1 w-1 rounded-full bg-slate-300 align-middle"
            aria-hidden="true"
          />
          <span :class="row.expandable === false ? 'text-slate-400' : ''">{{ row.label }}</span>
        </li>
      </ul>
      <p v-if="hiddenPreview > 0" class="mt-1 text-[11px] text-slate-400">
        ……还有 {{ hiddenPreview }} 个节点
      </p>
      <p class="mt-1 text-[11px] text-slate-400">这是生成过程中的预览，以落进画布的结果为准。</p>
    </details>

    <!-- 冲突裁决：生成期间画布变了（`undo` 之类绕过 busy 闸门的入口），两棵树都留着。 -->
    <div
      v-if="store.conflictDraft"
      role="alertdialog"
      data-test="generate-conflict"
      class="mt-2 rounded border border-amber-300 bg-amber-50 px-2 py-1.5 leading-relaxed text-amber-900"
    >
      <p class="font-medium">生成期间你改过这张图</p>
      <p class="mt-1 text-[11px]">两棵树都还在。用模型的结果会覆盖你的改动（覆盖之后仍可撤销），保留你的就丢掉这次生成。</p>
      <div class="mt-2 flex gap-2">
        <button
          type="button"
          class="rounded-md bg-slate-900 px-3 py-1.5 text-white"
          @click="resolve('generated')"
        >用模型的结果</button>
        <button
          type="button"
          class="rounded-md border border-slate-300 px-3 py-1.5 text-slate-700"
          @click="resolve('mine')"
        >保留我的</button>
      </div>
    </div>

    <!--
      生成结果已经落到画布（没有「应用」这一步了）。这里只说三件事：落了多大、
      改了几处、以及怎么退回去。原因：画布整棵树换了一遍，不做说明的话用户只知道
      "我的图变了"，却不知道变在哪、能不能反悔。
    -->
    <div
      v-if="store.generatedSummary && !store.conflictDraft"
      role="status"
      data-test="generate-done"
      class="mt-2 flex flex-col gap-1 rounded bg-white px-2 py-1.5"
    >
      <p class="font-medium text-slate-700">{{ resultLine }}</p>
      <p class="text-[11px] text-slate-500">还没保存。可以直接改，也可以退回生成之前。</p>
      <button
        v-if="store.canUndoGenerate"
        type="button"
        data-test="undo-generate"
        class="self-start rounded-md border border-slate-300 px-3 py-1.5 text-slate-700"
        @click="undoGenerate"
      >回到生成之前</button>
    </div>

    <!--
      这次生成改了什么。清单是**对比两份树算出来的**（`stores/mindmap.diffCandidate`），
      不是按收到的 op 帧记账：op 帧可能丢、也可能被主动丢弃，而"树到底哪里不一样"
      永远算得出来。
    -->
    <details
      v-if="diff"
      open
      class="mt-2 rounded bg-white px-2 py-1"
      aria-label="这次改动"
    >
      <summary class="cursor-pointer font-medium text-slate-700">
        这次改了什么（{{ diff.length }} 处）
      </summary>
      <p v-if="!diff.length" class="mt-1 text-[11px] text-slate-400">
        和生成之前一模一样，模型一处都没改。
      </p>
      <ul v-else class="mt-1 flex flex-col gap-1 text-[11px] leading-relaxed">
        <li v-for="item in shownDiff" :key="`${item.kind}:${item.id}`" class="break-words">
          <span class="mr-1 rounded px-1" :class="DIFF_CLASS[item.kind]">{{ DIFF_LABELS[item.kind] }}</span>
          <!--
            范围标记：这处改动落在「这次带上去的选区」里，还是跑到了范围外。
            只有真的带过选区才显示（`showScope`）——没圈节点时谈"范围外"没有意义。
          -->
          <span
            v-if="showScope"
            data-test="diff-scope-mark"
            class="mr-1 rounded px-1"
            :class="item.inSelection ? IN_SCOPE_CLASS : OUT_SCOPE_CLASS"
            :title="item.inSelection
              ? '这处改动落在你选中的节点及其子树里'
              : '这处改动落在你选中的范围之外：模型判断必须一起动，或者它判断错了'"
          >{{ item.inSelection ? '范围内' : '范围外' }}</span>
          <button
            type="button"
            class="text-left text-slate-700 underline decoration-slate-300 hover:text-slate-900"
            :title="`在画布上定位「${item.label}」`"
            @click="jumpTo(item.id)"
          >{{ item.label }}</button>
          <span class="ml-1 text-slate-400">{{ item.detail }}</span>
        </li>
      </ul>
      <p v-if="hiddenDiff > 0" class="mt-1 text-[11px] text-slate-400">……还有 {{ hiddenDiff }} 处</p>
      <p v-if="showScope" data-test="diff-scope-summary" class="mt-1 text-[11px] text-slate-500">
        {{ scopeSummary }}
      </p>
    </details>
  </section>
</template>
