<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue';

import type { SaveModelSettingsRequest, TestModelSettingsRequest } from '../api/types';
import {
  SETTING_SOURCE_UNBOUND,
  apiKeyPlaceholder,
  settingSourceLabel,
  useSettingsStore,
} from '../stores/settings';

/**
 * 模型设置面板：在页面里配好模型服务，**不碰环境变量、不重启**。
 *
 * 这个面板存在的理由：之前换一把密钥要 ssh 上服务器改 `.env` 再重启进程，而重启会
 * 打断正在跑的长图（一次生成可能几十秒）。有了它，改完立刻生效——后端每次调用都
 * 重新读一次配置，没有进程内缓存。
 *
 * 四件必须做对的事，每一件都对应一种"看起来能用、实际是错的"界面：
 *
 * 1. **密钥框留空 = 不改**。框里显示的是打码值（`…abcd`），用户不动它就提交空串；
 *    后端把空串当"不改"，要清空必须显式点「清除密钥」。反过来做（空串=清空）的话，
 *    每次改个模型名就会把密钥抹掉一次，而界面上只显示"已保存"。
 * 2. **只发改过的字段**。表单一打开就用"当前生效值"填好，那些值可能来自环境变量。
 *    把整张表原样 PUT 回去，等于把环境变量里的值搬进库里——之后改环境变量不再生效，
 *    而用户什么都没做。所以脏字段在这里算。
 * 3. **`demo` 模式要说出来**。`MINDMAP_LLM_MODE=demo` 时 agent 走占位内容、视觉
 *    提取直接拒绝：面板里填什么都不会调模型。不说的话用户会以为是密钥或地址填错了。
 * 4. **密钥永不明文回显**。只有打码值；用户新打的那一段只活在这个组件的局部状态里，
 *    保存成功就丢掉。
 *
 * 「测试连接」发的是 `max_tokens=1` 的一次 ping——验地址/密钥/模型名，花费大约一个
 * token。它也可以用**还没保存**的值，所以"填了先测、测通了再存"是通的。
 */

const settingsStore = useSettingsStore();

/** 面板是覆盖式浮层：`fixed` 挂在最上面，不参与主区 flex，开合不改变画布宽度。 */
const emit = defineEmits<{ close: [] }>();

/** 表单三个可编辑字段 + 深度模型。初值来自"当前生效值"（可能来自环境变量）。 */
const baseUrl = ref('');
const apiKey = ref('');
const model = ref('');
const deepModel = ref('');
/** 打开面板时的快照，用来算"改了哪几个字段"。 */
const initial = ref({ base_url: '', model: '', deep_model: '' });

const baseUrlInput = ref<HTMLInputElement | null>(null);

const view = computed(() => settingsStore.settings);

const canClearKey = computed(() => view.value?.api_key_set === true);

/**
 * 密钥还在库里，但它是给**旧地址**存的（后端判据在 `store._binding_of`）。
 *
 * 这时后端不交出这把密钥，`api_key_set` 是 `false`、占位符里也没有打码值。界面上
 * 如果只说"未配置"，用户会去供应商后台重开密钥。要在这里说清"重新粘一次就行"。
 */
const keyUnbound = computed(() => view.value?.api_key_source === SETTING_SOURCE_UNBOUND);

/**
 * 当前这个人能不能写**全站共享配置**（部署方 = 这台机器上第一个注册的账号）。
 *
 * 只有他能看到那一块。给所有人显示一个点了就 403 的按钮，比看不见更糟——用户会
 * 以为是网络问题。
 */
const canEditInstance = computed(() => view.value?.can_edit_instance === true);
/** 共享那一行现在配好没有。没配好时借它兜底的人（游客、新账号）根本生成不了。 */
const instanceConfigured = computed(() => view.value?.instance?.configured === true);

/** 有哪几个字段真被改过。空对象 = 什么都不用发。 */
const dirty = computed(() => {
  const body: SaveModelSettingsRequest = { clear_api_key: false };
  const start = initial.value;
  if (baseUrl.value.trim() !== start.base_url) body.base_url = baseUrl.value.trim();
  if (model.value.trim() !== start.model) body.model = model.value.trim();
  if (deepModel.value.trim() !== start.deep_model) body.deep_model = deepModel.value.trim();
  if (apiKey.value.trim()) body.api_key = apiKey.value.trim();
  return body;
});

/** 有没有实质改动。判据不含 `clear_api_key` 这个恒定键。 */
const changeCount = computed(
  () => ['base_url', 'api_key', 'model', 'deep_model'].filter((key) => key in dirty.value).length,
);

const nothingToSave = computed(() => changeCount.value === 0);

/** 三项齐了没有（用于保存按钮的提示文案，不用于禁用——用户可能只想先存地址）。 */
const incomplete = computed(() => view.value?.configured !== true);

function fillFrom(next: typeof view.value): void {
  baseUrl.value = next?.base_url ?? '';
  model.value = next?.model ?? '';
  deepModel.value = next?.deep_model ?? '';
  apiKey.value = '';
  initial.value = {
    base_url: baseUrl.value.trim(),
    model: model.value.trim(),
    deep_model: deepModel.value.trim(),
  };
}

onMounted(async () => {
  // 每次打开都重拉：别的标签页/别的浏览器可能刚改过，而这里显示的是"生效值"，
  // 不该是上一次打开时的快照。
  await settingsStore.load();
  fillFrom(settingsStore.settings);
  await nextTick();
  baseUrlInput.value?.focus();
});

/** 测试连接：**优先用表单里的值**（含还没保存的关键改动），空字段回落生效值。 */
async function runTest(): Promise<void> {
  const body: TestModelSettingsRequest = {};
  if (baseUrl.value.trim()) body.base_url = baseUrl.value.trim();
  if (apiKey.value.trim()) body.api_key = apiKey.value.trim();
  if (model.value.trim()) body.model = model.value.trim();
  await settingsStore.test(body);
}

async function runSave(): Promise<void> {
  const body = dirty.value;
  if (nothingToSave.value) {
    // 什么都没改：不发请求，也不假装保存过。
    return;
  }
  const saved = await settingsStore.save(body);
  if (saved) fillFrom(saved);
}

/** 显式清空已保存的密钥。走 `clear_api_key`，不是"提交空串"。 */
async function clearKey(): Promise<void> {
  const saved = await settingsStore.clearApiKey();
  if (saved) fillFrom(saved);
}

/**
 * 把上面填的这份存成**全站共享配置**（只有部署方看得到这个按钮）。
 *
 * 语义是整个覆盖，不是合并："共享配置 = 这一份"。所以四项都带上——`base_url` /
 * `model` / `deep_model` 用当前表单值（空串就是清掉），密钥只有用户真打了才带
 * （空串在后端是"不改"，所以没打字时共享那份的密钥原样留着）。
 *
 * 为什么不给所有人一个"保存到哪一行"的单选：绝大多数人只有自己那一份，多一个选择
 * 只会让他们犹豫；而写共享行是部署时才做一次的动作。
 */
async function saveAsInstance(): Promise<void> {
  const body: SaveModelSettingsRequest = {
    clear_api_key: false,
    scope: 'instance',
    base_url: baseUrl.value.trim(),
    model: model.value.trim(),
    deep_model: deepModel.value.trim(),
  };
  if (apiKey.value.trim()) body.api_key = apiKey.value.trim();
  const saved = await settingsStore.save(body);
  if (saved) fillFrom(saved);
}

/** `result.message` 的类型：`ok` 决定颜色，`status` 决定要不要补一句"HTTP 码"。 */
const resultTone = computed(() => {
  const result = settingsStore.result;
  if (!result) return '';
  return result.ok ? 'text-emerald-700' : 'text-rose-700';
});

/** 面板根节点，给焦点圈定用。 */
const panel = ref<HTMLElement | null>(null);

/**
 * 把 Tab 圈在面板里。
 *
 * `role="dialog" aria-modal="true"` 告诉读屏"这之外的东西不可达"，但键盘 Tab 不会
 * 读 aria：不接住的话焦点会跑到遮罩背后的画布与顶栏，用户看着面板还在、按键却已
 * 经操作别处了。与知识库抽屉（`App.vue` 的 `trapLibraryFocus`）同一套做法。
 */
function trapFocus(event: KeyboardEvent): void {
  if (event.key !== 'Tab') return;
  const root = panel.value;
  if (!root) return;
  const focusable = Array.from(root.querySelectorAll<HTMLElement>(
    'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
  )).filter(element => !element.hasAttribute('disabled'));
  if (focusable.length === 0) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  const inside = active instanceof HTMLElement && root.contains(active);
  if (event.shiftKey && (!inside || active === first)) {
    event.preventDefault();
    last.focus();
    return;
  }
  if (!event.shiftKey && (!inside || active === last)) {
    event.preventDefault();
    first.focus();
  }
}
</script>

<template>
  <!--
    遮罩 + 居中卡片。点遮罩关闭，`@click.stop` 挡住卡片内部的冒泡——
    少了那个 stop，点任何输入框都会把面板关掉。
  -->
  <div
    class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 p-4"
    data-test="settings-backdrop"
    @click.self="emit('close')"
  >
    <section
      ref="panel"
      role="dialog"
      aria-modal="true"
      aria-label="模型设置"
      data-test="settings-panel"
      class="max-h-full w-full max-w-lg overflow-auto rounded-lg border border-slate-200 bg-white p-4 text-sm shadow-xl"
      @click.stop
      @keydown="trapFocus"
    >
      <header class="mb-3 flex items-start justify-between gap-2">
        <div>
          <h2 class="text-base font-semibold text-slate-900">模型设置</h2>
          <p class="mt-0.5 text-xs text-slate-500">
            改完立即生效，不用重启后端。留空的字段表示"不改"。
          </p>
        </div>
        <button
          type="button"
          data-test="settings-close"
          class="rounded-md border border-slate-200 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
          aria-label="关闭模型设置"
          @click="emit('close')"
        >✕</button>
      </header>

      <!--
        demo 模式的提示必须比"未配置"更靠上：这两种情况在界面上长得一样（都会生成
        一张占位图），但原因完全不同，改的地方也不同。
      -->
      <p
        v-if="settingsStore.isDemoMode"
        data-test="settings-demo-hint"
        class="mb-3 rounded-md bg-amber-50 px-2 py-1.5 text-xs leading-relaxed text-amber-800"
      >
        当前后端是演示模式（<code>MINDMAP_LLM_MODE=demo</code>）：即使下面填好了，生成仍会用占位内容、
        图片提取也会被拒绝。要真正调用模型，需要把它设为 <code>model</code> 并重启一次后端。
      </p>

      <p v-if="settingsStore.error" data-test="settings-error" class="mb-3 rounded-md bg-rose-50 px-2 py-1.5 text-xs text-rose-700">
        {{ settingsStore.error }}
      </p>

      <!-- 保存成功、但有件事该说：通常是地址刚被改过，上次「测试连接」的结论
           对新地址不成立。用中性色而不是红色——保存本身是成功的。 -->
      <p
        v-if="settingsStore.notice"
        data-test="settings-notice"
        class="mb-3 rounded-md bg-sky-50 px-2 py-1.5 text-xs leading-relaxed text-sky-800"
      >
        {{ settingsStore.notice }}
      </p>

      <p v-if="settingsStore.loading" class="mb-3 text-xs text-slate-400">正在读取当前配置…</p>

      <div class="flex flex-col gap-3">
        <label class="flex flex-col gap-1">
          <span class="flex items-baseline justify-between gap-2">
            <span class="text-xs font-medium text-slate-600">服务地址（OpenAI 兼容）</span>
            <span class="text-[11px] text-slate-400">{{ settingSourceLabel(view?.base_url_source ?? 'unset') }}</span>
          </span>
          <input
            ref="baseUrlInput"
            v-model="baseUrl"
            type="url"
            inputmode="url"
            autocomplete="off"
            spellcheck="false"
            data-test="settings-base-url"
            placeholder="https://api.deepseek.com/v1"
            class="rounded-md border border-slate-300 px-2 py-1.5 text-xs text-slate-800 outline-none focus:border-slate-500"
          />
          <!-- 这句是给"填了另一家原生 API"的人看的：不做 provider 下拉就意味着用户
               可以填任意地址，而生成侧只会一种协议。不写这一句，非 OpenAI 兼容的
               服务会以"模型返回的不是合法的 Chat Completions 响应"的形式炸在生成
               阶段——那句话不指向地址这一行。 -->
          <span class="text-[11px] leading-relaxed text-slate-500" data-test="settings-base-url-hint">
            只支持 OpenAI 兼容格式：实际请求的是「地址 + <code class="text-slate-600">/chat/completions</code>」，
            所以填到 <code class="text-slate-600">/v1</code> 那一层即可。各家原生协议（Gemini、Anthropic Messages、
            部分国产厂商的自有 API）填进来会连不通。
          </span>
        </label>

        <label class="flex flex-col gap-1">
          <span class="flex items-baseline justify-between gap-2">
            <span class="text-xs font-medium text-slate-600">密钥</span>
            <span class="text-[11px] text-slate-400">{{ settingSourceLabel(view?.api_key_source ?? 'unset') }}</span>
          </span>
          <!--
            `type="password"`：即使打的是新密钥也不明文留在屏幕上。
            `autocomplete="off"`：否则浏览器的密码管理器会把它当成登录框去填。
          -->
          <input
            v-model="apiKey"
            type="password"
            autocomplete="off"
            spellcheck="false"
            data-test="settings-api-key"
            :placeholder="apiKeyPlaceholder(view)"
            class="rounded-md border border-slate-300 px-2 py-1.5 text-xs text-slate-800 outline-none focus:border-slate-500"
          />
          <span class="flex items-center justify-between gap-2">
            <span class="text-[11px] text-slate-400">留空＝不改动已保存的密钥。</span>
            <button
              v-if="canClearKey"
              type="button"
              data-test="settings-clear-key"
              class="rounded border border-rose-200 px-2 py-0.5 text-[11px] text-rose-700 hover:bg-rose-50 disabled:opacity-40"
              :disabled="settingsStore.saving"
              @click="clearKey"
            >清除已保存的密钥</button>
          </span>
          <!--
            密钥与地址是绑定的：库里那把只为**存它时的那个地址**保存。地址一改，
            那把密钥就不再交出——这时的正确动作是重新粘一次，而不是去供应商后台
            重开一把（库里那把一直是好的）。这句话必须说在这里，因为界面上别的
            地方只会显示"未配置"，那读起来像密钥丢了。
          -->
          <span
            v-if="keyUnbound"
            data-test="settings-key-unbound"
            class="rounded-md bg-amber-50 px-2 py-1.5 text-[11px] leading-relaxed text-amber-800"
          >
            你换过服务地址，而库里那把密钥是为旧地址保存的。确认新地址可信之后，把密钥重新粘贴一次即可。
          </span>
        </label>

        <label class="flex flex-col gap-1">
          <span class="flex items-baseline justify-between gap-2">
            <span class="text-xs font-medium text-slate-600">模型名</span>
            <span class="text-[11px] text-slate-400">{{ settingSourceLabel(view?.model_source ?? 'unset') }}</span>
          </span>
          <input
            v-model="model"
            type="text"
            autocomplete="off"
            spellcheck="false"
            data-test="settings-model"
            placeholder="deepseek-chat"
            class="rounded-md border border-slate-300 px-2 py-1.5 text-xs text-slate-800 outline-none focus:border-slate-500"
          />
        </label>

        <label class="flex flex-col gap-1">
          <span class="flex items-baseline justify-between gap-2">
            <span class="text-xs font-medium text-slate-600">深度思考用的模型名（可选）</span>
            <span class="text-[11px] text-slate-400">{{ settingSourceLabel(view?.deep_model_source ?? 'unset') }}</span>
          </span>
          <input
            v-model="deepModel"
            type="text"
            autocomplete="off"
            spellcheck="false"
            data-test="settings-deep-model"
            placeholder="留空＝自动（deepseek 端点用 deepseek-reasoner）"
            class="rounded-md border border-slate-300 px-2 py-1.5 text-xs text-slate-800 outline-none focus:border-slate-500"
          />
          <!--
            勾了「深度思考」时**实际会发出去**的名字。它可能就是自动选的那个，
            用户不看这一行会以为自己的设置没生效。
          -->
          <span v-if="view?.deep_model_effective" class="text-[11px] text-slate-400">
            实际会发：<code data-test="settings-deep-effective">{{ view.deep_model_effective }}</code>
          </span>
        </label>
      </div>

      <!--
        全站共享配置：只有部署方看得到。

        它是游客与"还没填自己密钥的人"生成时用的那一份。不显示的话，部署方看不到
        自己配的是什么，而"游客说用不了"时无从对照。
      -->
      <section
        v-if="canEditInstance"
        data-test="settings-instance"
        class="mt-4 rounded-md border border-slate-200 bg-slate-50/60 px-3 py-2.5"
      >
        <div class="flex items-baseline justify-between gap-2">
          <h3 class="text-xs font-medium text-slate-700">本站共享配置</h3>
          <span
            class="text-[11px]"
            :class="instanceConfigured ? 'text-slate-500' : 'text-amber-700'"
            data-test="settings-instance-state"
          >
            {{ instanceConfigured ? '已配置' : '还没配好' }}
          </span>
        </div>
        <p class="mt-1 text-[11px] leading-relaxed text-slate-500">
          游客、以及还没填自己密钥的账号，生成时用这一份。填好之后点下面那个按钮把它存成共享配置。
        </p>
        <dl class="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[11px] text-slate-600">
          <dt class="text-slate-400">地址</dt>
          <dd class="truncate" data-test="settings-instance-base-url">{{ view?.instance?.base_url || '（空）' }}</dd>
          <dt class="text-slate-400">密钥</dt>
          <dd data-test="settings-instance-key">{{ view?.instance?.api_key_set ? view?.instance?.api_key_masked : '（空）' }}</dd>
          <dt class="text-slate-400">模型</dt>
          <dd class="truncate">{{ view?.instance?.model || '（空）' }}</dd>
        </dl>
        <button
          type="button"
          data-test="settings-save-instance"
          class="mt-2 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-40"
          :disabled="settingsStore.saving"
          :title="'把上面填的这几项存成全站共享配置'"
          @click="saveAsInstance"
        >存为共享配置</button>
      </section>

      <div class="mt-4 flex flex-wrap items-center gap-2">
        <button
          type="button"
          data-test="settings-save"
          class="rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-800 disabled:opacity-40"
          :disabled="settingsStore.saving || nothingToSave"
          :title="nothingToSave ? '没有改动' : '保存并立即生效'"
          @click="runSave"
        >{{ settingsStore.saving ? '保存中…' : '保存' }}</button>
        <button
          type="button"
          data-test="settings-test"
          class="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-40"
          :disabled="settingsStore.testing"
          @click="runTest"
        >{{ settingsStore.testing ? '测试中…' : '测试连接' }}</button>
        <span v-if="incomplete" class="text-[11px] text-slate-400">三项填齐后才是"已配置"状态。</span>
      </div>

      <!-- 测试结论：成功绿、失败红。`status` 是**对端**的 HTTP 码，便于对照服务商文档。 -->
      <p
        v-if="settingsStore.result"
        data-test="settings-result"
        class="mt-3 rounded-md px-2 py-1.5 text-xs leading-relaxed"
        :class="[resultTone, settingsStore.result.ok ? 'bg-emerald-50' : 'bg-rose-50']"
      >
        {{ settingsStore.result.message }}
        <span v-if="settingsStore.result.status" class="text-[11px] opacity-70">（HTTP {{ settingsStore.result.status }}）</span>
      </p>

      <footer class="mt-4 border-t border-slate-100 pt-2 text-[11px] leading-relaxed text-slate-400">
        <p>
          这里存的值优先于环境变量（<code>MINDMAP_LLM_BASE_URL</code> / <code>MINDMAP_LLM_API_KEY</code> /
          <code>MINDMAP_LLM_MODEL</code> / <code>MINDMAP_LLM_MODEL_DEEP</code>）；把某一项清空就回到环境变量那套。
        </p>
        <p v-if="view?.updated_at" class="mt-1">上次保存：{{ view.updated_at }}</p>
      </footer>
    </section>
  </div>
</template>
