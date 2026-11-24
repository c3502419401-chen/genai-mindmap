<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';

import {
  BOOLEAN_PREF_KEYS,
  DIAGRAM_PREF_KEY,
  EXTERNAL_SEARCH_PREF_KEY,
  ICON_PREF_KEY,
  MAX_NOTES,
  NOTE_PREFIX,
  nextNoteKey,
  prefDate,
  prefSourceLabel,
  prefValueLabel,
  THINKING_PREF_KEY,
  usePrefsStore,
} from '../stores/prefs';

/**
 * 偏好面板：把"系统记住了什么"摆到明面上。
 *
 * 这个组件存在的理由不是好看，是调研报告里那条最贵的判断：**如果一条记忆写进去但
 * 从不被读，那它就只是"心里记着"**——用户看不到效果，也改不了，于是他会以为系统
 * 根本没记住。所以三件事一个都不能少：列出来、能改、能删。
 *
 * 第四条是这里最特别的一格：每条显示「上次生效：日期」或「尚未生效」，数据来自
 * `runs.request.prefs_applied`。它是这整张表的**死表探测器**——一条偏好写进去之后
 * 从来没有被任何一次运行报告过，面板上就写着"尚未生效"，不用翻日志就知道读路径
 * 没接上。如果面板上每一条都是"尚未生效"，那这个功能当前的价值就是零。
 *
 * 建议走"候选"而不是自动写入：同一主题两分钟内出现过两次相反的图型要求，任何自动
 * 总结都会在那里写出两条互相矛盾的事实。所以建议只显示一句话加两个按钮，点"不用"
 * 什么都不发生。
 */

const prefs = usePrefsStore();

/** 键的中文名。只给 A/B 类起名：备注是用户自己写的，翻译它没有意义。 */
const PREF_KEY_LABELS: Record<string, string> = {
  [DIAGRAM_PREF_KEY]: '默认图型',
  [THINKING_PREF_KEY]: '默认拆解方式',
  [ICON_PREF_KEY]: '节点图标',
  [EXTERNAL_SEARCH_PREF_KEY]: '联网检索',
};

/** 正在编辑的键；空串＝没有在编辑。 */
const editing = ref('');
const draft = ref('');
/** 新增备注的草稿。 */
const noteDraft = ref('');
const open = ref(true);

const notes = computed(() => prefs.notes);
const canAddNote = computed(() => notes.value.length < MAX_NOTES);

function keyLabel(key: string): string {
  if (key.startsWith(NOTE_PREFIX)) return '内容风格备注';
  return PREF_KEY_LABELS[key] ?? key;
}

/** 值域是 `true` / `false` 的偏好：面板画勾选框，不让人手打这两个单词。 */
function isBooleanKey(key: string): boolean {
  return BOOLEAN_PREF_KEYS.includes(key);
}

/**
 * 勾选框 = 写一条偏好。写不进去（接口挂了）时把勾拨回原样：`:checked` 绑的是
 * `item.value`，store 没变 Vue 就不会重渲染这个 prop，不手动复原的话界面上会留下
 * 一个"看起来已经改了、其实没改"的状态，而 `prefs.error` 那一行未必就在手边。
 */
async function toggleBoolean(key: string, current: string, event: Event): Promise<void> {
  if (await prefs.save(key, current === 'true' ? 'false' : 'true')) return;
  (event.target as HTMLInputElement).checked = current === 'true';
}

function startEdit(key: string, value: string): void {
  editing.value = key;
  draft.value = value;
}

function cancelEdit(): void {
  editing.value = '';
  draft.value = '';
}

async function commitEdit(key: string): Promise<void> {
  const text = draft.value.trim();
  if (!text) return;
  if (await prefs.save(key, text)) cancelEdit();
}

async function addNote(): Promise<void> {
  const text = noteDraft.value.trim();
  if (!text) return;
  const key = nextNoteKey(prefs.items.map(item => item.key));
  if (await prefs.save(key, text)) noteDraft.value = '';
}

async function acceptSuggestion(item: (typeof prefs.suggestions)[number]): Promise<void> {
  await prefs.accept(item);
}

onMounted(() => {
  // **不在这里拉数据**：`App` 挂载时已经 `prefs.load()` 过一次，面板与生成面板的
  // 两个下拉读的是同一份 store。两处各拉一次会制造一个真实的坏窗口——第二次请求
  // 把 `loading` 置真，"还没有任何偏好"那句提示先消失再出现，界面看起来像闪了一下。
  // 面板的"刷新"按钮是唯一的重读入口。
  open.value = true;
});
</script>

<template>
  <section
    aria-label="我的偏好"
    data-test="prefs-panel"
    class="rounded-md border border-slate-200 bg-white p-2 text-xs"
  >
    <div class="flex items-center justify-between">
      <button
        type="button"
        class="flex items-center gap-1 font-semibold text-slate-700"
        :aria-expanded="open"
        @click="open = !open"
      >
        我的偏好
        <span class="text-slate-400">（{{ prefs.items.length }}）</span>
        <span class="text-slate-400">{{ open ? '收起' : '展开' }}</span>
      </button>
      <button
        type="button"
        class="text-slate-500 hover:text-slate-900 disabled:opacity-50"
        :disabled="prefs.loading"
        @click="prefs.load()"
      >刷新</button>
    </div>

    <template v-if="open">
      <p class="mt-1 leading-relaxed text-slate-400">
        记住你的习惯，下次新建时预填进下拉，你仍可以当场改掉。
      </p>

      <!-- 建议：候选，不是事实。点「不用」什么都不写。 -->
      <div
        v-for="item in prefs.suggestions"
        :key="`${item.key}=${item.value}`"
        data-test="pref-suggestion"
        class="mt-2 rounded border border-sky-200 bg-sky-50 p-2"
      >
        <p class="leading-relaxed text-slate-700">{{ item.message }}</p>
        <div class="mt-1 flex gap-2">
          <button
            type="button"
            class="rounded border border-sky-300 bg-white px-2 py-0.5 text-sky-800 hover:bg-sky-100 disabled:opacity-50"
            :disabled="prefs.saving === item.key"
            @click="acceptSuggestion(item)"
          >设为默认</button>
          <button
            type="button"
            class="rounded border border-slate-300 px-2 py-0.5 text-slate-600 hover:bg-slate-50"
            @click="prefs.dismiss(item)"
          >不用</button>
        </div>
      </div>

      <p v-if="prefs.error" role="alert" class="mt-2 leading-relaxed text-rose-700">{{ prefs.error }}</p>

      <template v-if="!prefs.items.length">
        <!-- 加载中与"真的没有"共用同一块，只换一句话：用 `loading` 把整块藏起来的
             话，重新拉一次数据会让提示闪一下——用户看到的是"我的偏好刚才还在"。
             数据本身由 `App` 挂载时拉过一次，面板只负责渲染（它没有第二个数据源）。 -->
        <p v-if="!prefs.loading" data-test="prefs-empty" class="mt-2 leading-relaxed text-slate-400">
          还没有任何偏好。生成时勾「以后都这样」，或者在这里加一条备注。
        </p>
        <p v-else class="mt-2 leading-relaxed text-slate-400">正在读取偏好…</p>
      </template>

      <ul v-else class="mt-2 flex flex-col gap-2">
        <li
          v-for="item in prefs.items"
          :key="item.key"
          data-test="pref-item"
          class="rounded border border-slate-200 p-2"
        >
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0 flex-1">
              <p class="font-medium text-slate-700">
                {{ keyLabel(item.key) }}
              </p>
              <template v-if="editing === item.key">
                <textarea
                  v-model="draft"
                  rows="2"
                  :data-test="`pref-edit-${item.key}`"
                  class="mt-1 w-full resize-y rounded border border-slate-300 px-1 py-0.5 text-xs outline-none focus:border-slate-500"
                />
                <div class="mt-1 flex gap-2">
                  <button
                    type="button"
                    class="rounded border border-slate-300 px-2 py-0.5 text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                    :data-test="`pref-save-${item.key}`"
                    :disabled="prefs.saving === item.key"
                    @click="commitEdit(item.key)"
                  >保存</button>
                  <button
                    type="button"
                    class="text-slate-500 hover:text-slate-900"
                    @click="cancelEdit"
                  >取消</button>
                </div>
              </template>
              <p v-else class="mt-0.5 break-words text-slate-900">
                {{ prefValueLabel(item.key, item.value) }}
              </p>
              <!--
                布尔型偏好用一个勾选框，而不是让它和备注共用那个文本框：一个开关
                用文本框编辑，用户只能靠"上次生效"去猜自己有没有打错 `fasle`。
                打错时后端会 400（值域只认 true/false），但那已经是一次失败。
              -->
              <label
                v-if="isBooleanKey(item.key) && editing !== item.key"
                class="mt-1 flex w-fit items-center gap-1 text-slate-600"
              >
                <input
                  type="checkbox"
                  :checked="item.value === 'true'"
                  :disabled="prefs.saving === item.key"
                  :data-test="`pref-toggle-${item.key}`"
                  @change="toggleBoolean(item.key, item.value, $event)"
                />
                {{ item.value === 'true' ? '已开启' : '已关闭' }}
              </label>
              <p class="mt-0.5 text-slate-400">
                {{ prefSourceLabel(item.source) }}
                <!--
                  「上次生效」是这一整块里最重要的一行：它是这条偏好到底有没有在工作的
                  唯一可见证据。日期取自 `runs.request.prefs_applied`，所以一条从没被任何
                  一次运行报告过的偏好会永远显示「尚未生效」——那就是死表的形状。
                -->
                ·
                <span v-if="item.last_applied_at" :data-test="`pref-applied-${item.key}`">
                  上次生效：{{ prefDate(item.last_applied_at) }}
                </span>
                <span v-else :data-test="`pref-never-${item.key}`" class="text-amber-700">
                  尚未生效
                </span>
              </p>
            </div>
            <div class="flex shrink-0 flex-col items-end gap-1">
              <!-- 布尔型没有"改"：值域只有两个，勾选框就是全部编辑面。 -->
              <button
                v-if="!isBooleanKey(item.key)"
                type="button"
                class="text-slate-500 hover:text-slate-900"
                :data-test="`pref-edit-btn-${item.key}`"
                @click="startEdit(item.key, item.value)"
              >改</button>
              <button
                type="button"
                class="text-slate-400 hover:text-rose-600 disabled:opacity-50"
                :aria-label="`删除 ${keyLabel(item.key)}`"
                :data-test="`pref-del-${item.key}`"
                :disabled="prefs.saving === item.key"
                @click="prefs.remove(item.key)"
              >删除</button>
            </div>
          </div>
        </li>
      </ul>

      <!-- 加一条备注：唯一会进提示词的一类。上限在界面上先说清楚，而不是等提交被拒。 -->
      <div class="mt-2 border-t border-slate-200 pt-2">
        <label :for="'note-draft'" class="flex items-center justify-between text-slate-600">
          <span>加一条内容风格备注</span>
          <span class="text-slate-400">{{ notes.length }}/{{ MAX_NOTES }}</span>
        </label>
        <textarea
          id="note-draft"
          v-model="noteDraft"
          rows="2"
          :disabled="!canAddNote"
          data-test="pref-note-draft"
          class="mt-1 w-full resize-y rounded border border-slate-300 px-1 py-0.5 text-xs outline-none focus:border-slate-500 disabled:opacity-50"
          placeholder="例如：喜欢短标题，不要行业黑话"
        />
        <button
          type="button"
          class="mt-1 rounded border border-slate-300 px-2 py-0.5 text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          :disabled="!canAddNote || !noteDraft.trim() || prefs.saving !== ''"
          data-test="pref-note-add"
          @click="addNote"
        >添加备注</button>
        <p class="mt-1 leading-relaxed text-slate-400">
          <template v-if="canAddNote">
            最多 8 条、合计 500 字。
          </template>
          <template v-else>已经 8 条了，要加新的请先删一条。</template>
        </p>
      </div>
    </template>
  </section>
</template>
