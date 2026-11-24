<script setup lang="ts">
import { computed, ref } from 'vue';

import {
  structureSummaryLine,
  type SuggestionNote,
  type Tone,
} from '../mindmap/diagnostics';
import { useMindMapStore } from '../stores/mindmap';
import { useSourcesStore } from '../stores/sources';
import { useStructureStore } from '../stores/structure';

/**
 * 结构检查面板：诊断逐条 + 候选建议（带「应用 / 忽略」）。
 *
 * ## 它住在右抽屉里，不是一个新浮层
 *
 * 上一轮刚解决掉的是"诊断/建议不许挡住画布"。右抽屉是**覆盖式**的（`absolute` 挂在
 * `<main>` 上，不参与主区 flex 布局），于是它出现与消失时画布宽度一点都没变，也不会
 * 触发一次自动 fitView。再造一个居中浮层或者一条常驻横幅，等于把刚拆掉的东西装回去。
 *
 * ## 渲染规则：按 `code` / `kind` 通用，不硬编码
 *
 * 界面对后端返回的每条诊断只做三件事：取人话标题（`diagnosticLabel`，认不出来就显示
 * 码本身）、取颜色（`diagnosticTone`）、显示 `message`。**没有任何 `if (code === …)`**
 * ——后端加码时这里一行都不用改，用户看到的是 `NEAR_DUPLICATE_BRANCH` 加后端那句
 * 中文。这是这个面板能跟上后端演进的全部机制。
 *
 * ## info 默认折叠
 *
 * `SINGLE_CHILD` 在一棵正常的树上是结构常态（任何链式子结构都命中），一张 43 节点的
 * 图能有十几条。把它们和"同级重名"并排铺开，用户学会的是无视整块面板——那这个功能
 * 就废了。所以：`warn` / `error` 默认展开，info 折进一个写着"不影响使用"的开关里，
 * 状态行上一个数字都不给它（判据在 `structureHint`）。
 *
 * ## 原码默认收起，但随时展开得开
 *
 * 诊断码（`DUPLICATE_LABEL`）是内部标识：已知码已经有中文标题，把它留在卡片正文里
 * 是给写代码的人看的。但它又是排查时唯一能对上后端日志的东西，所以**不删**：每条
 * 卡片收一个「查看原码」，面板底部再给一个总开关一次全露出来。未知码不受影响——
 * 它的标题就是原码本身，藏不掉。
 */
const store = useMindMapStore();
const sources = useSourcesStore();
const check = useStructureStore();

/** info 那一段展开没有。默认收起——理由见文件头。 */
const hintsOpen = ref(false);

/**
 * 一次默认铺开几条。
 *
 * 数字来自实测，不是拍的：库里那张 561 节点的「机器学习的主要方向」跑一次 check 拿到
 * **45 条** `DUPLICATE_LABEL`；跑 suggest 就是 45 条几乎一样的「合并重复」建议。
 * 一次全铺开，抽屉里除了这 45 行什么都看不见，而它们要做的事其实只有一件。
 *
 * 所以两段各有一个上限 + 一条「展开全部 N 条」的出路（`hiddenAttention` /
 * `hiddenSuggestions`）。**上限只是折叠，不是丢弃**：剩下的永远点得到。
 */
const ATTENTION_LIMIT = 6;
const SUGGESTION_LIMIT = 5;

const attentionOpen = ref(false);
const suggestionsOpen = ref(false);

/**
 * 原码显示策略：默认藏起来，能按条展开，也能一下全展开。
 *
 * 已知码有中文标题（`diagnosticLabel`），`DUPLICATE_LABEL` 这种内部码留在卡片正文里
 * 是给写代码的人看的；但它又是排查时唯一能对上后端日志的东西，所以不能删——收进
 * 「查看原码」，再给一个面板级的总开关（排障时一次全露出来，不用逐条点）。
 *
 * 未知码不受影响：它的标题**就是**原码（见 `diagnosticLabel`），藏不掉也不该藏。
 */
const showCodes = ref(false);
const openedCodes = ref<string[]>([]);

function codeVisible(key: string): boolean {
  return showCodes.value || openedCodes.value.includes(key);
}

function toggleCode(key: string): void {
  openedCodes.value = openedCodes.value.includes(key)
    ? openedCodes.value.filter(item => item !== key)
    : [...openedCodes.value, key];
}

const attention = computed(() => check.visibleDiagnostics.filter(item => item.level !== 'info'));
const hints = computed(() => check.visibleDiagnostics.filter(item => item.level === 'info'));

const shownAttention = computed(
  () => (attentionOpen.value ? attention.value : attention.value.slice(0, ATTENTION_LIMIT)),
);
const hiddenAttention = computed(() => attention.value.length - shownAttention.value.length);

const shownSuggestions = computed(
  () => (suggestionsOpen.value
    ? check.visibleSuggestions
    : check.visibleSuggestions.slice(0, SUGGESTION_LIMIT)),
);
const hiddenSuggestions = computed(
  () => check.visibleSuggestions.length - shownSuggestions.value.length,
);

/** 颜色 → class。色值只在这里，判据全在 `mindmap/diagnostics.ts`。 */
const TONE_CLASS: Record<Tone, string> = {
  slate: 'bg-slate-100 text-slate-600',
  amber: 'bg-amber-100 text-amber-800',
  rose: 'bg-rose-100 text-rose-700',
};

const TONE_BORDER: Record<Tone, string> = {
  slate: 'border-slate-200 bg-white',
  amber: 'border-amber-200 bg-amber-50/60',
  rose: 'border-rose-200 bg-rose-50/60',
};

/**
 * 标题行那句摘要。**没有发现时是空串**：正文那一行已经在说「没有发现问题。」，
 * 标题上再说一遍就成了同一个面板里说两次同一句话。
 */
const summary = computed(() => {
  const counts = check.counts;
  if (!counts.attention && !counts.suggestions && !counts.hints) return '';
  return structureSummaryLine(counts);
});

/**
 * 按钮文案。
 *
 * 「检查结构」是**唯一**会带材料全文去后端的那条路（第三层引用核验），所以它不是
 * 自动的：材料可能上万字，用户没点的时候不该发。
 */
const runLabel = computed(() => {
  if (check.busy) return '正在检查…';
  if (check.neverRun) return '检查结构';
  if (check.stale) return '图改过了，重新检查';
  return '重新检查';
});

/** 一条建议点了「应用」之后，界面要能指出它做了什么——文案来自 store 的 status。 */
function apply(note: SuggestionNote): void {
  check.applySuggestion(note);
}

function dismiss(key: string): void {
  check.dismiss(key);
}

/** 有没有任何可显示的东西。空面板不渲染——抽屉里多一块"什么都没有"只是噪音。 */
const visible = computed(
  () => check.busy || check.neverRun || check.ran || !!check.error || attention.value.length > 0
    || hints.value.length > 0 || check.visibleSuggestions.length > 0,
);
</script>

<template>
  <section
    v-if="visible"
    data-test="structure-check"
    aria-label="结构检查"
    class="flex flex-col gap-2 text-xs"
  >
    <div class="flex items-center justify-between gap-2">
      <p class="font-medium text-slate-600">
        结构检查
        <span v-if="check.ran && summary" data-test="structure-summary" class="font-normal text-slate-400">
          · {{ summary }}
        </span>
      </p>
      <button
        type="button"
        data-test="structure-run"
        class="shrink-0 rounded-md border border-slate-300 px-2 py-0.5 text-[11px] text-slate-700 hover:bg-slate-50 disabled:opacity-40"
        :disabled="check.busy || !store.content"
        @click="check.runFull(sources.readySources)"
      >{{ runLabel }}</button>
    </div>

    <p v-if="check.neverRun" data-test="structure-empty" class="leading-relaxed text-slate-400">
      还没检查过。点「检查结构」跑一次：只读，不改任何东西。
    </p>
    <p v-else-if="check.ran && !attention.length && !hints.length && !check.visibleSuggestions.length"
      data-test="structure-clean" class="leading-relaxed text-slate-400">
      没有发现问题。
    </p>

    <!-- 过期：报告说的已经不是眼前这张图。说出来比悄悄重跑好——用户改掉了那个
         重名之后，看到一句"同级重名"而没有这个标记，他会以为界面在说他没改过。 -->
    <p
      v-if="check.stale && !check.busy"
      data-test="structure-stale"
      class="rounded border border-slate-200 bg-slate-50 px-2 py-1 text-[11px] leading-relaxed text-slate-500"
    >
      这张图在检查之后又改过，下面的结论可能不准了。
    </p>

    <p v-if="check.error" data-test="structure-error" role="alert"
      class="rounded border border-amber-200 bg-amber-50 px-2 py-1 text-[11px] leading-relaxed text-amber-800">
      {{ check.error }}
    </p>

    <!-- 建议：**只有点了「应用」才改图**，而且走的是 store 的编辑路径（可撤销）。 -->
    <ul v-if="shownSuggestions.length" class="flex flex-col gap-1.5">
      <li
        v-for="note in shownSuggestions"
        :key="note.key"
        data-test="suggestion-item"
        class="rounded border border-sky-200 bg-sky-50/60 px-2 py-1.5"
      >
        <p class="flex items-center gap-1.5">
          <!-- 徽标写 kind 的人话；未知 kind 显示原 kind（同诊断码的规则）。 -->
          <span
            data-test="suggestion-kind"
            class="rounded bg-sky-100 px-1 text-[10px] text-sky-700"
          >{{ note.kindLabel }}</span>
          <span class="min-w-0 flex-1 break-words leading-relaxed text-slate-700">{{ note.message }}</span>
        </p>
        <p v-if="note.evidence" class="mt-0.5 break-words text-[11px] text-slate-500">
          依据：{{ note.evidence }}
        </p>
        <div class="mt-1 flex items-center gap-2">
          <button
            v-if="note.applicable"
            type="button"
            data-test="suggestion-apply"
            class="rounded-md bg-slate-900 px-2 py-0.5 text-[11px] text-white disabled:opacity-40"
            :disabled="check.busy"
            title="按这条建议改图；改完可以 Ctrl+Z 撤销"
            @click="apply(note)"
          >应用</button>
          <button
            type="button"
            data-test="suggestion-dismiss"
            class="rounded-md border border-slate-300 bg-white px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-50"
            title="这次会话里不再显示这条"
            @click="dismiss(note.key)"
          >忽略</button>
          <!-- 没有应用动作时**不给灰按钮**：灰着会让人一直去试它。给一句原因。 -->
          <span v-if="!note.applicable" data-test="suggestion-blocked" class="text-[11px] text-slate-400">
            {{ note.blockedReason }}
          </span>
        </div>
      </li>
    </ul>

    <!-- 折叠之后必须留一条**能走到全部**的路：只写"还有 N 条"等于把后面那几条锁起来。 -->
    <button
      v-if="hiddenSuggestions > 0 || suggestionsOpen"
      type="button"
      data-test="suggestion-expand"
      class="self-start text-[11px] text-slate-500 underline decoration-dotted underline-offset-2 hover:text-slate-900"
      :aria-expanded="suggestionsOpen"
      @click="suggestionsOpen = !suggestionsOpen"
    >{{ suggestionsOpen ? '收起建议' : `展开全部 ${check.visibleSuggestions.length} 条建议（还有 ${hiddenSuggestions} 条）` }}</button>

    <!-- 诊断：warn / error 直接铺开（默认上限 6 条，其余给一条出路）。 -->
    <ul v-if="shownAttention.length" data-test="diagnostic-list" class="flex flex-col gap-1">
      <li
        v-for="note in shownAttention"
        :key="note.key"
        data-test="diagnostic-item"
        class="rounded border px-2 py-1.5"
        :class="TONE_BORDER[note.tone]"
      >
        <p class="flex items-center gap-1.5">
          <span class="rounded px-1 text-[10px]" :class="TONE_CLASS[note.tone]">{{ note.label }}</span>
          <span class="min-w-0 flex-1 break-words leading-relaxed text-slate-700">{{ note.message }}</span>
          <button
            type="button"
            data-test="diagnostic-dismiss"
            class="shrink-0 text-[11px] text-slate-400 hover:text-slate-700"
            title="这次会话里不再显示这条"
            @click="dismiss(note.key)"
          >忽略</button>
        </p>
        <!--
          原码默认收起：已知码已经有中文标题，`DUPLICATE_LABEL` 这种内部码留在正文里
          是给写代码的人看的。但它又是排查时唯一能对上后端日志的东西，所以不能删——
          收进「查看原码」，并用面板级的总开关（`showCodes`）一次全露出来。
          未知码不受影响：它的标题**就是**原码（`diagnosticLabel` 的 `?? code`）。
        -->
        <p class="mt-0.5 flex flex-wrap items-baseline gap-x-1 break-words text-[10px] text-slate-400">
          <code v-if="codeVisible(note.key)" data-test="diagnostic-code">{{ note.code || '（无码）' }}</code>
          <!-- 单条开关在总开关打开时就不出现了：那时"收起"归总开关管，两条路互不打架。 -->
          <button
            v-if="!showCodes"
            type="button"
            data-test="diagnostic-code-toggle"
            class="text-slate-400 underline decoration-dotted underline-offset-2 hover:text-slate-700"
            :title="`原码：${note.code || '（无码）'}`"
            :aria-expanded="codeVisible(note.key)"
            @click="toggleCode(note.key)"
          >{{ codeVisible(note.key) ? '收起原码' : '查看原码' }}</button>
          <span v-if="note.evidence">依据：{{ note.evidence }}</span>
        </p>
      </li>
    </ul>

    <button
      v-if="hiddenAttention > 0 || attentionOpen"
      type="button"
      data-test="diagnostic-expand"
      class="self-start text-[11px] text-slate-500 underline decoration-dotted underline-offset-2 hover:text-slate-900"
      :aria-expanded="attentionOpen"
      @click="attentionOpen = !attentionOpen"
    >{{ attentionOpen ? '收起诊断' : `展开全部 ${attention.length} 条诊断（还有 ${hiddenAttention} 条）` }}</button>

    <!--
      info：结构常态，默认收起。
      「不影响使用」这句不是安慰——它说清了这一堆为什么被折起来，否则收起这件事
      看起来就是"还有东西没给我看"。
    -->
    <div v-if="hints.length" data-test="diagnostic-hints">
      <button
        type="button"
        data-test="diagnostic-hints-toggle"
        class="text-[11px] text-slate-500 underline decoration-dotted underline-offset-2 hover:text-slate-900"
        :aria-expanded="hintsOpen"
        @click="hintsOpen = !hintsOpen"
      >{{ hintsOpen ? '收起提示' : `展开 ${hints.length} 条提示（结构常态，不影响使用）` }}</button>
      <ul v-if="hintsOpen" class="mt-1 flex flex-col gap-1">
        <li
          v-for="note in hints"
          :key="note.key"
          data-test="diagnostic-hint-item"
          class="rounded border border-slate-200 bg-white px-2 py-1 text-[11px] leading-relaxed text-slate-600"
        >
          <span class="mr-1 rounded bg-slate-100 px-1 text-[10px] text-slate-500">{{ note.label }}</span>
          <span class="break-words">{{ note.message }}</span>
          <code
            v-if="codeVisible(note.key)"
            data-test="diagnostic-hint-code"
            class="ml-1 text-[10px] text-slate-400"
          >{{ note.code || '（无码）' }}</code>
          <button
            v-else-if="!showCodes"
            type="button"
            data-test="diagnostic-hint-code-toggle"
            class="ml-1 text-[10px] text-slate-400 underline decoration-dotted underline-offset-2 hover:text-slate-700"
            :title="`原码：${note.code || '（无码）'}`"
            @click="toggleCode(note.key)"
          >查看原码</button>
        </li>
      </ul>
    </div>

    <!--
      原码总开关：排查时一次全露出来，不用逐条点。放在最底下，正常使用时它不挡路；
      没有诊断时整块不渲染。
    -->
    <button
      v-if="attention.length || hints.length"
      type="button"
      data-test="diagnostic-code-all"
      class="self-start text-[11px] text-slate-400 underline decoration-dotted underline-offset-2 hover:text-slate-700"
      :aria-pressed="showCodes"
      @click="showCodes = !showCodes"
    >{{ showCodes ? '隐藏原码' : '显示原码' }}</button>
  </section>
</template>
