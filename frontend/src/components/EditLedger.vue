<script setup lang="ts">
import { computed, ref, watch } from 'vue';

import { useLedgerStore } from '../stores/ledger';
import { useMindMapStore } from '../stores/mindmap';

/**
 * 账本：**这张图改过什么**。
 *
 * ## 为什么是"一整块的列表面板"，而不是塞进节点详情
 *
 * 三个候选里选了这个（"这张图的改动记录"独立一块），理由是数据形状：后端三条读路径
 * 全部以 `map_id` 为键（`/edits`、`/edits/summary`），`summary` 里每一个数字
 * （`label_shrunk` / `removed_depth` / `untouched_runs`）都是**整图口径**。挂到节点
 * 详情里之后，这些数字要么全部用不上（只剩 `by_kind` 的一个角），要么得在界面上按
 * 节点重新聚合一遍——而后端已经明确说过"这张图"是它回答问题的粒度。
 *
 * 更要紧的一条：**账本答不了"这个节点为什么变成这样"。** 它记的是 diff
 * （`before` / `after` / `depth`），没有记理由。把半截答案摆进节点详情，会让人以为
 * 那里能归因，而"为什么"是 `StructureCheck`（诊断）在答的事。
 *
 * 那个问题的**另一半**（"这个节点被改过几次、改成了什么"）确实该有——所以这里给了
 * 一个「只看选中节点」的筛选：形态仍是整图列表（数据形状不变），选中节点时多一个
 * 开关。不是两个面板，是同一个面板的两种取景。
 *
 * ## 住在右抽屉里，和「本次生成明细」并排
 *
 * 右抽屉的定位就是"这张图的元信息 + 选中节点"。账本属于前者。它默认收起——打开抽屉
 * 的人多半是来看节点详情的，一份改动清单挡在上面就是新的遮挡（与「本次生成明细」的
 * 判断同一条）。收起时**一个请求都不发**：这是账本和那两块的区别，它的数据要问后端。
 *
 * ## 收藏之后不再假装是最新的
 *
 * 账本读的是**已落库**的事件。用户改完没保存，那些改动不在账本里——所以有未保存
 * 改动时标题下面明写一句。少这一句，"我刚改的怎么没出现"就变成一次不可解释的空白。
 */
const store = useLedgerStore();
const mindmap = useMindMapStore();

const open = ref(false);
/** 展开明细的那几行（键是 `at#行号`，见下方 `rowKey`）。 */
const expanded = ref<string[]>([]);
/** 「只看选中节点」。选中节点被清空时这个开关自动失效（`visible` 里判）。 */
const onlySelected = ref(false);

const selectedId = computed(() => mindmap.selectedId);
const mapId = computed(() => mindmap.mapId);

async function toggleOpen(): Promise<void> {
  open.value = !open.value;
  if (open.value) await store.load(mapId.value);
}

function reload(): void {
  void store.load(mapId.value);
}

/**
 * 换图了：账本里那份数据不再属于任何东西，清掉。
 *
 * 只有**展开着**的时候才顺手重读——收起的账本不该因为换图而发三个请求，那等于把
 * "收起时不发请求"这条判断绕开了。
 */
watch(mapId, id => {
  if (id === store.mapId) return;
  if (open.value && id) {
    void store.load(id);
    return;
  }
  store.reset();
});

/**
 * 行键：`at` + 行号。
 *
 * 不用 `at` 一个字段：同一批手工编辑共享同一个 `at`（一次操作取一次时刻），那时
 * 两行的 `at` 完全一样，拿它当 key 会让展开状态在两行之间乱跳。加行号即可——列表
 * 每次刷新都是整体替换，行号不会跨刷新复用。
 */
function rowKey(at: string, index: number): string {
  return `${at}#${index}`;
}

function expandedRow(at: string, index: number): boolean {
  return expanded.value.includes(rowKey(at, index));
}

function toggleRow(at: string, index: number): void {
  const key = rowKey(at, index);
  expanded.value = expanded.value.includes(key)
    ? expanded.value.filter(item => item !== key)
    : [...expanded.value, key];
}

/**
 * 时刻显示成 `MM-DD HH:mm`。
 *
 * 不带年份：这张图的改动通常集中在最近几天，而 360px 宽的抽屉里多五列字符换不回
 * 什么。真需要年份时把鼠标放上去（`title` 里是完整值）——那是少数情况，不该为它让
 * 每一行都长一截。
 */
function stampOf(at: string): string {
  const date = new Date(at);
  if (Number.isNaN(date.getTime())) return at;
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

const lines = computed(() => store.lines);

const visible = computed(() => {
  const id = selectedId.value;
  if (!onlySelected.value || !id) return lines.value;
  return lines.value.filter(line => line.nodeIds.includes(id));
});

/** 明细要不要给展开按钮：只有折叠过的行（一键修复）才有多行子项。 */
function hasChildren(count: number): boolean {
  return count > 1;
}
</script>

<template>
  <section class="text-xs" aria-label="改动记录">
    <button
      type="button"
      data-test="edit-ledger-toggle"
      class="flex w-full items-start justify-between gap-2 rounded-md border border-slate-200 bg-slate-50 px-2 py-1 text-left"
      :aria-expanded="open"
      @click="toggleOpen"
    >
      <span class="min-w-0 flex-1">
        <span class="font-medium text-slate-600">改动记录</span>
        <!--
          摘要单独一行并截断。放在标题右边那一行里会长到两行——"模型给了 47 个节点 ·
          图上 6 处改动 · 1 / 2 次生成一字未改"比标题本身长三倍，挤在一起时标题反而
          被压成一个尾巴。全文在 `title` 里，鼠标放上去能读全。
        -->
        <span
          v-if="store.headline"
          data-test="edit-ledger-headline"
          class="mt-0.5 block truncate text-[11px] text-slate-400"
          :title="store.headline"
        >· {{ store.headline }}</span>
      </span>
      <span class="shrink-0 text-[11px] text-slate-400">{{ open ? '收起' : '展开' }}</span>
    </button>

    <div v-show="open" class="mt-2 flex flex-col gap-2">
      <!-- 还没保存过的图：没有 map_id，事件无从归属。说清"从哪一刻开始记"。 -->
      <p v-if="!mapId" data-test="edit-ledger-unsaved" class="leading-relaxed text-slate-500">
        这张图还没保存过。改动记录从第一次保存开始记——之前改的不会补记。
      </p>

      <template v-else>
        <p v-if="store.busy" data-test="edit-ledger-busy" class="text-slate-400">正在读取…</p>

        <p v-else-if="store.error" data-test="edit-ledger-error" class="leading-relaxed text-rose-600">
          读不到改动记录：{{ store.error }}
        </p>

        <template v-else>
          <!-- 备份这段是运维读数，正常时不出现（判据在 `healthWarning`）。 -->
          <p
            v-if="store.warning"
            data-test="edit-ledger-warning"
            class="rounded border border-amber-200 bg-amber-50 px-2 py-1 leading-relaxed text-amber-800"
          >{{ store.warning }}</p>

          <p
            v-if="mindmap.dirty"
            data-test="edit-ledger-dirty"
            class="leading-relaxed text-amber-700"
          >还有未保存的改动。保存之后它们才会出现在这里。</p>

          <!-- 摘要下面的几条读数：只有读得出方向时才各占一行。 -->
          <ul v-if="store.notes.length" class="flex flex-col gap-0.5 text-[11px] text-slate-500">
            <li v-for="note in store.notes" :key="note" data-test="edit-ledger-note">· {{ note }}</li>
          </ul>

          <label
            v-if="selectedId"
            class="flex items-center gap-1 text-[11px] text-slate-500"
            :title="'只看当前选中节点的改动'"
          >
            <input
              v-model="onlySelected"
              data-test="edit-ledger-only-selected"
              type="checkbox"
              class="h-3 w-3"
            >
            只看选中节点
          </label>

          <p v-if="!lines.length" data-test="edit-ledger-empty" class="leading-relaxed text-slate-500">
            这张图还没有任何改动记录。导入的图一条都没有——账本只记生成之后发生的事。
          </p>

          <p
            v-else-if="!visible.length"
            data-test="edit-ledger-filter-empty"
            class="leading-relaxed text-slate-500"
          >
            这个节点在最近这些记录里没有改动（账本只读最近 {{ lines.length }} 行，更早的没显示）。
          </p>

          <ol v-else class="flex flex-col divide-y divide-slate-100">
            <li
              v-for="(line, index) in visible"
              :key="rowKey(line.at, index)"
              data-test="edit-ledger-row"
              class="flex flex-col gap-0.5 py-1"
            >
              <div class="flex items-start gap-2">
                <span
                  class="shrink-0 font-mono text-[10px] leading-4 text-slate-400"
                  :title="line.at"
                >{{ stampOf(line.at) }}</span>
                <span class="min-w-0 flex-1 break-words leading-relaxed" :class="{
                  'text-slate-700': line.tone === 'user',
                  'text-sky-700': line.tone === 'agent',
                  'text-amber-800': line.tone === 'rule',
                }">{{ line.text }}</span>
                <button
                  v-if="hasChildren(line.children.length)"
                  type="button"
                  data-test="edit-ledger-expand"
                  class="shrink-0 text-[10px] text-slate-500 hover:text-slate-900"
                  :aria-expanded="expandedRow(line.at, index)"
                  @click="toggleRow(line.at, index)"
                >{{ expandedRow(line.at, index) ? '收起' : `${line.children.length} 条明细` }}</button>
              </div>

              <!-- 折叠摘要：一句话说清这批到底做了什么，展开之前就能读。 -->
              <p
                v-if="line.digest"
                data-test="edit-ledger-digest"
                class="pl-14 text-[11px] leading-relaxed text-slate-500"
              >{{ line.digest }}</p>

              <ul
                v-if="expandedRow(line.at, index) && line.children.length"
                data-test="edit-ledger-children"
                class="ml-14 flex flex-col gap-0.5 border-l border-slate-200 pl-2"
              >
                <li
                  v-for="(child, childIndex) in line.children"
                  :key="`${childIndex}`"
                  data-test="edit-ledger-child"
                  class="leading-relaxed text-[11px] text-amber-900"
                >{{ child.text }}</li>
              </ul>
            </li>
          </ol>

          <div class="flex items-center justify-between">
            <button
              type="button"
              data-test="edit-ledger-refresh"
              class="text-[11px] text-slate-500 hover:text-slate-900"
              :disabled="store.busy"
              @click="reload"
            >刷新</button>
            <span class="text-right text-[11px] text-slate-400">
              共 {{ store.summary?.total ?? 0 }} 条事件 · 这里 {{ lines.length }} 行
              <!-- 只读了最近一段时必须说出来：一次修复就能吃掉几十条事件，读到的
                   200 条可能只是整本账的一角，而"这里 6 行"会被读成"一共只有 6 行"。 -->
              <template v-if="store.events.length < (store.summary?.total ?? 0)">
                （只读了最近 {{ store.events.length }} 条）
              </template>
            </span>
          </div>
        </template>
      </template>
    </div>
  </section>
</template>
