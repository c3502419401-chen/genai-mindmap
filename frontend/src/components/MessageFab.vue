<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';

import { useChatStore, type ChatMessage } from '../stores/chat';
import { hasModelInfo, modelTraceLine } from '../mindmap/run-status';

/**
 * 右下角那颗悬浮球 + 展开后的消息时间线。
 *
 * 它是"生成完对话框就消失"的补偿：对话框收走之后，用户还是要能回答三个问题——
 * 我刚才说的是什么、它回了什么、改了几处。**角标只数面板关着时新增的条数**，
 * 所以"生成完了"这件事不会被静默掉：画布上多了棵树，球上多一个 1。
 *
 * 时间线按时间排、不按轮次折叠：折叠之后每折只有一行，翻自己的原话要点三下。
 */
const chat = useChatStore();

const emit = defineEmits<{ compose: []; reuse: [text: string]; }>();

const listRef = ref<HTMLElement | null>(null);

const badge = computed(() => (chat.unread > 99 ? '99+' : String(chat.unread)));

function timeOf(at: number): string {
  const date = new Date(at);
  // 显示层也要兜底：`at` 缺失或脏数据时 `getHours()` 是 NaN，直接拼出来就是
  // `NaN:NaN` 摆在用户脸上。这一行回答的是"什么时候说的"，不知道就写不知道。
  // （写入那一侧已经修了默认时间，两条路都得挡住，只修一个另一条还会漏。）
  if (!Number.isFinite(date.getTime())) return '--:--';
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}

function toneOf(message: ChatMessage): string {
  if (message.role === 'user') return 'bg-slate-900 text-white';
  if (message.kind === 'error') return 'bg-rose-50 text-rose-900 ring-1 ring-rose-200';
  if (message.kind === 'partial') return 'bg-amber-50 text-amber-900 ring-1 ring-amber-200';
  if (message.role === 'system') return 'bg-slate-50 text-slate-500';
  return 'bg-white text-slate-700 ring-1 ring-slate-200';
}

/** 这条消息的模型名怎么说（请求名 → 服务名，缺的写"未知"）。 */
function traceOf(message: ChatMessage): string {
  return modelTraceLine({ requested: message.model, served: message.servedModel });
}

function hasTrace(message: ChatMessage): boolean {
  return hasModelInfo({ requested: message.model, served: message.servedModel });
}

/** 面板打开时滚到底：用户点开就是想看最新那条。 */
watch(() => [chat.open, chat.count] as const, async ([open]) => {
  if (!open) return;
  await nextTick();
  const el = listRef.value;
  if (el) el.scrollTop = el.scrollHeight;
});

function closeOnEscape(event: KeyboardEvent): void {
  if (event.key === 'Escape' && chat.open) chat.closePanel();
}

watch(() => chat.open, open => {
  if (open) window.addEventListener('keydown', closeOnEscape);
  else window.removeEventListener('keydown', closeOnEscape);
});

onBeforeUnmount(() => window.removeEventListener('keydown', closeOnEscape));
</script>

<template>
  <div class="pointer-events-none absolute bottom-4 right-4 z-40 flex flex-col items-end gap-2">
    <!--
      消息流：贴着球往左上方展开，不遮画布中心。

      **窄屏（<900px）改成整屏抽屉**：380px 的面板钉在右下角时，横向会正好切进底部
      对话框（1280 宽时盖掉 136px），纵向一相交就把提交按钮挡住——用户既看不全消息，
      也点不到要点的按钮。到了这个宽度还并排放两列本来就没地方，所以干脆整屏盖住：
      要么看消息，要么用对话框，用那个悬浮球切换。宽屏那边由 `ChatDock` 让位并排。
    -->
    <section
      v-show="chat.open"
      data-test="message-panel"
      aria-label="消息记录"
      class="pointer-events-auto flex h-[min(60vh,520px)] w-[min(92vw,380px)] flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl max-[899px]:fixed max-[899px]:inset-x-0 max-[899px]:bottom-0 max-[899px]:h-[min(86vh,680px)] max-[899px]:w-full max-[899px]:rounded-b-none max-[899px]:rounded-t-2xl max-[899px]:border-x-0 max-[899px]:border-b-0"
    >
      <header class="flex items-center justify-between border-b border-slate-100 px-3 py-2">
        <p class="text-xs font-semibold text-slate-700">消息记录</p>
        <div class="flex items-center gap-2">
          <!-- 对话框淡出之后，"再说一句"的入口就在这里。没有它，用户想接着改一句
               只能先去画布上选个节点——那是"改这一支"，不是"我刚才那句还不够"。 -->
          <button
            type="button"
            data-test="message-compose"
            class="rounded-md border border-slate-300 px-2 py-0.5 text-[11px] text-slate-700 hover:bg-slate-50"
            title="把底部对话框叫回来"
            @click="emit('compose')"
          >继续说一句</button>
          <button
            type="button"
            data-test="message-close"
            class="text-[11px] text-slate-500 hover:text-slate-900"
            @click="chat.closePanel()"
          >关闭</button>
        </div>
      </header>
      <div ref="listRef" class="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-3 py-2">
        <p v-if="!chat.messages.length" class="text-xs leading-relaxed text-slate-400">
          还没有消息。写一个主题生成之后，对话会按时间记在这里。
        </p>
        <article
          v-for="message in chat.messages"
          :key="message.id"
          data-test="message-item"
          class="flex flex-col gap-1"
        >
          <div class="flex items-center gap-1.5 text-[10px] text-slate-400">
            <span>{{ message.role === 'user' ? '你' : message.role === 'ai' ? '模型' : '界面' }}</span>
            <span>{{ timeOf(message.at) }}</span>
            <!--
              两条名字都写出来：请求的那个是我们发出去的意图，服务的那个是对面的回答。
              实测同一个地址会把 `deepseek-chat` 路由到 `deepseek-flash`——只写请求名，
              这件事在界面上永远看不出来。
            -->
            <span
              v-if="hasTrace(message)"
              class="rounded bg-slate-100 px-1 text-slate-500"
              data-test="message-model"
            >{{ traceOf(message) }}</span>
            <span v-else-if="message.role === 'ai'" class="text-slate-400">（模型名未上报）</span>
          </div>
          <p
            class="whitespace-pre-wrap break-words rounded-xl px-2.5 py-1.5 text-xs leading-relaxed"
            :class="toneOf(message)"
          >{{ message.text }}</p>
          <!-- 「改这句」：对话框淡出之后，想"改一个词重发"不该变成翻历史 + 重打。
               点它把原句连同对话框一起叫回来，用户只改他要改的那几个字。 -->
          <button
            v-if="message.role === 'user'"
            type="button"
            data-test="message-reuse"
            class="self-start text-[11px] text-slate-500 underline decoration-slate-300 underline-offset-2 hover:text-slate-900"
            title="把这句填回对话框，改完再发一次"
            @click="emit('reuse', message.text)"
          >改这句</button>
          <!-- 思考过程全文：`<details>` 折叠，不占时间线的垂直空间。 -->
          <details v-if="message.reasoning" class="rounded-lg bg-slate-50 px-2 py-1">
            <summary class="cursor-pointer text-[11px] text-slate-500">
              模型当时的思考（{{ message.reasoning.length }} 字）
            </summary>
            <p class="mt-1 max-h-60 overflow-y-auto whitespace-pre-wrap break-words text-[11px] leading-relaxed text-slate-600">
              {{ message.reasoning }}
            </p>
          </details>
        </article>
      </div>
    </section>

    <button
      type="button"
      data-test="message-fab"
      class="pointer-events-auto relative flex h-12 w-12 items-center justify-center rounded-full bg-slate-900 text-white shadow-xl transition hover:bg-slate-800"
      :aria-label="chat.open ? '收起消息记录' : `消息记录（${chat.unread} 条未读）`"
      :aria-expanded="chat.open"
      aria-controls="message-panel"
      @click="chat.toggle()"
    >
      <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
        <path d="M4 6h16M4 12h10M4 18h13" stroke-linecap="round" />
      </svg>
      <span
        v-if="chat.hasUnread"
        data-test="message-badge"
        class="absolute -right-1 -top-1 min-w-[18px] rounded-full bg-rose-500 px-1 text-center text-[10px] leading-[18px] text-white"
      >{{ badge }}</span>
    </button>
  </div>
</template>
