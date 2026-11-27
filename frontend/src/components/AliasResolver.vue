<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue';

import {
  applyAliasPreview, cancelAliasPreview, deleteAlias, readAliasPreview, startAliasPreview,
} from '../api/client';
import type { AliasCandidate, AliasPair, AliasPreview } from '../api/types';

const emit = defineEmits<{ changed: [] }>();
const preview = ref<AliasPreview | null>(null);
const selected = ref<string[]>([]);
const starting = ref(false);
const applying = ref(false);
const error = ref('');
const notice = ref('');
const created = ref<AliasPair[]>([]);
const undoing = ref('');
const selectable = computed(() => preview.value?.items.filter((item) => item.selectable) ?? []);
const chosen = computed(() => preview.value?.items.filter((item) => selected.value.includes(item.id)) ?? []);
let timer: ReturnType<typeof setTimeout> | undefined;
let generation = 0;
let disposed = false;

function stopPolling(): void {
  if (timer !== undefined) clearTimeout(timer);
  timer = undefined;
}

async function poll(jobId: string, token: number): Promise<void> {
  try {
    const next = await readAliasPreview(jobId);
    if (token !== generation || disposed) return;
    preview.value = next;
    if (next.status === 'running') {
      timer = setTimeout(() => void poll(jobId, token), 1200);
    } else if (next.status === 'failed') {
      error.value = next.error || '查找失败，请重试。';
    }
  } catch (err) {
    if (token === generation && !disposed) error.value = `进度读取失败：${(err as Error).message}`;
  }
}

async function start(): Promise<void> {
  const token = ++generation;
  stopPolling();
  starting.value = true;
  error.value = '';
  notice.value = '';
  selected.value = [];
  created.value = [];
  preview.value = null;
  try {
    const next = await startAliasPreview();
    if (token !== generation || disposed) {
      await cancelAliasPreview(next.job_id).catch(() => undefined);
      return;
    }
    preview.value = next;
    await poll(next.job_id, token);
  } catch (err) {
    if (token === generation && !disposed) error.value = `无法开始查找：${(err as Error).message}`;
  } finally {
    if (token === generation && !disposed) starting.value = false;
  }
}

async function cancel(): Promise<void> {
  const job = preview.value;
  if (!job) return;
  ++generation;
  stopPolling();
  try {
    preview.value = await cancelAliasPreview(job.job_id);
    selected.value = [];
    error.value = '';
  } catch (err) {
    error.value = `取消失败：${(err as Error).message}`;
  }
}

function selectAll(): void {
  selected.value = selectable.value.map((item) => item.id);
}

function basis(item: AliasCandidate): string {
  if (item.reason === 'substring') return `构词比例 ${item.similarity.toFixed(2)}`;
  if (item.reason === 'embedding') return `向量余弦 ${item.similarity.toFixed(2)}`;
  return `判定强度 ${item.similarity.toFixed(2)}`;
}

async function apply(): Promise<void> {
  const job = preview.value;
  if (!job || !chosen.value.length || applying.value) return;
  const names = chosen.value.slice(0, 5).map((item) => `「${item.alias} → ${item.canonical}」`).join('、');
  const remaining = chosen.value.length > 5 ? `等 ${chosen.value.length} 对` : `共 ${chosen.value.length} 对`;
  if (!window.confirm(`确认添加${remaining}别名：${names}？查询路径会随之变化。原始材料和证据边不变，可逐对撤回。`)) return;
  applying.value = true;
  error.value = '';
  try {
    const result = await applyAliasPreview({ job_id: job.job_id, candidate_ids: [...selected.value] });
    created.value = result.created;
    notice.value = `已新增 ${result.relationships_created} 条别名关系，${result.skipped} 对未写入。`;
    preview.value = { ...job, status: 'applied', stage: '已应用' };
    selected.value = [];
    emit('changed');
  } catch (err) {
    error.value = `应用未完成：${(err as Error).message}。请检查别名清单后再重新预览。`;
    preview.value = null;
    emit('changed');
  } finally {
    applying.value = false;
  }
}

async function undo(pair: AliasPair): Promise<void> {
  const key = `${pair.alias}\u0000${pair.canonical}`;
  undoing.value = key;
  error.value = '';
  try {
    await deleteAlias(pair.alias, pair.canonical);
    created.value = created.value.filter((item) => item.alias !== pair.alias || item.canonical !== pair.canonical);
    notice.value = `已撤回「${pair.alias} → ${pair.canonical}」；材料和证据边未改动。`;
    emit('changed');
  } catch (err) {
    error.value = `撤回失败：${(err as Error).message}。请检查别名清单。`;
  } finally {
    undoing.value = '';
  }
}

onBeforeUnmount(() => {
  disposed = true;
  ++generation;
  stopPolling();
  if (preview.value && ['running', 'done', 'failed'].includes(preview.value.status)) {
    void cancelAliasPreview(preview.value.job_id).catch(() => undefined);
  }
});
</script>

<template>
  <div class="mt-3 space-y-2 text-xs text-slate-700">
    <p class="leading-relaxed text-slate-500">
      查找会连接图谱并使用向量服务额度，可能需要几十秒；最多比较 500 个实体，其中为最近入图的实体保留席位，结果需要逐对核对后才会写入。
    </p>
    <button type="button" class="rounded border border-slate-300 px-2 py-1 hover:bg-slate-50 disabled:opacity-50"
      :disabled="starting || applying || preview?.status === 'running'" @click="start">
      {{ starting ? '正在启动…' : '找出可合并的实体' }}
    </button>
    <div v-if="preview?.status === 'running'" role="status" class="space-x-2">
      <span>{{ preview.stage }}…</span>
      <button type="button" class="underline" @click="cancel">取消查找</button>
      <p class="mt-1 text-slate-400">已发出的向量请求可能仍会完成；结果不会被应用。</p>
    </div>
    <p v-if="preview?.status === 'cancelled'" role="status">已取消查找，未应用任何候选。</p>
    <p v-if="error" role="alert" class="rounded bg-rose-50 p-2 text-rose-700">{{ error }}</p>
    <p v-for="warning in preview?.warnings ?? []" :key="warning" role="status"
      class="rounded bg-amber-50 p-2 text-amber-800">{{ warning }}</p>
    <p v-if="notice" role="status" class="rounded bg-emerald-50 p-2 text-emerald-700">{{ notice }}</p>

    <div v-if="preview?.status === 'done'" class="space-y-2">
      <p>找到 {{ preview.items.length }} 对建议；请选择要添加的别名。</p>
      <p class="leading-relaxed text-slate-500">
        每一对都经过直接比较，但仍是算法推测；摘录仅说明名字出自材料，不能证明两者为同一实体。
        没有图谱出处时最多回查最近 100 份材料的前 5 万字；找不到不等于材料中不存在。
      </p>
      <div v-if="preview.items.length" class="flex gap-3">
        <button type="button" class="underline" :disabled="!selectable.length" @click="selectAll">全选有出处的候选</button>
        <button type="button" class="underline" @click="selected = []">清空选择</button>
        <button type="button" class="underline" @click="cancel">丢弃预览</button>
      </div>
      <ul class="space-y-2">
        <li v-for="item in preview.items" :key="item.id" class="rounded border border-slate-200 p-2">
          <label class="flex items-start gap-2">
            <input v-model="selected" type="checkbox" :value="item.id" :disabled="!item.selectable || applying">
            <span class="min-w-0 break-words"><strong>{{ item.alias }} → {{ item.canonical }}</strong>（{{ basis(item) }}）</span>
          </label>
          <p v-if="item.notice" role="status"
            class="mt-1 break-words rounded bg-amber-50 px-2 py-1 leading-relaxed text-amber-800">
            ⚠ {{ item.notice }}：两个名字可能指不同的事物，合并后它们会共用一条查询路径。
          </p>
          <div v-for="side in [{ name: item.alias, evidence: item.alias_evidence }, { name: item.canonical, evidence: item.canonical_evidence }]"
            :key="side.name" class="mt-2 break-words leading-relaxed">
            <p v-if="!side.evidence.length" class="text-amber-700">「{{ side.name }}」的原文出处不可核验，不能选择这一对。</p>
            <p v-for="e in side.evidence" :key="`${e.source_id}:${e.start}:${e.end}`" class="text-slate-600">
              「{{ side.name }}」在《{{ e.filename }}》的第 {{ e.start + 1 }}–{{ e.end }} 字：{{ e.quote }}
              <span class="text-slate-400">（{{ e.kind === 'graph_edge' ? '图谱关系出处' : '知识库原文匹配，未记录图谱出处' }}）</span>
            </p>
          </div>
        </li>
      </ul>
      <button v-if="preview.items.length" type="button"
        class="rounded bg-slate-800 px-3 py-1.5 text-white disabled:opacity-40"
        :disabled="!selected.length || applying" @click="apply">
        {{ applying ? '正在应用…' : `确认添加已选 ${selected.length} 对` }}
      </button>
    </div>
    <p v-if="created.length" class="leading-relaxed text-slate-500">
      撤回会删除这一对名称当前的全部别名边（包括此后新加的同名边），不会删除材料或证据边。
    </p>
    <ul v-if="created.length" class="space-y-1">
      <li v-for="pair in created" :key="`${pair.alias}→${pair.canonical}`" class="flex items-center justify-between gap-2">
        <span class="min-w-0 break-words">{{ pair.alias }} → {{ pair.canonical }}</span>
        <button type="button" class="shrink-0 text-rose-700 underline"
          :disabled="!!undoing" @click="undo(pair)">撤回这一对</button>
      </li>
    </ul>
  </div>
</template>
