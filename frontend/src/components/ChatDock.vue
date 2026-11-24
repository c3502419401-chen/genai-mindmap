<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import SourceInputs from './SourceInputs.vue';
import VoicePress from './VoicePress.vue';
import { mergedCharCount, SHORT_MATERIAL_CHARS, materialTitle, SOURCE_LABELS } from '../sources/input';
import { INITIAL_STATUS, useMindMapStore } from '../stores/mindmap';
import { useChatStore } from '../stores/chat';
import { useGenerationOptions, DIAGRAM_TYPE_OPTIONS, THINKING_MODEL_OPTIONS } from '../stores/options';
import { usePrefsStore, DIAGRAM_PREF_KEY, EXTERNAL_SEARCH_PREF_KEY, THINKING_PREF_KEY } from '../stores/prefs';
import { useSourcesStore } from '../stores/sources';
import { useStructureStore } from '../stores/structure';
import type { DiagramTypeChoice } from '../api/types';
import { agentDetailVisible, diagnosticCount, diagnosticsHint } from '../mindmap/run-status';
import { structureHint } from '../mindmap/diagnostics';

/**
 * 底部对话框：**唯一**说一句话的地方。
 *
 * 它接管了原来"画布右上角那个框"的全部职责（输入、拆解方式、图型、联网检索、提交），
 * 并且把状态机显式化了——这是这一版的核心交互：
 *
 *   空闲      完整对话框（缩略图 + 输入 + 按钮行）
 *   生成中    收成一条细进度（"正在生成 · 已定好框架 · 12 秒" + 取消）
 *   生成完    淡出，只剩画布 + 悬浮球（结果进消息流）
 *   选中节点  节点旁浮出小工具条；点「改」把对话框叫回来并预填
 *   语音      输入框换成「按住说话」，右侧图标变 ⌨
 *
 * **为什么"生成完就淡出"要留三条刹车**（`shouldKeep`）：输入框里还有没发出去的字、
 * 这次失败了、或者画布上根本还没有图——这三种时候把对话框收走，用户下一步就不知道
 * 该点哪儿了。前两种是"他要接着改"，第三种是"他还什么都没做"。只有"成功且有图"
 * 那一种才真的只剩画布。
 */

const store = useMindMapStore();
const sources = useSourcesStore();
const options = useGenerationOptions();
const prefs = usePrefsStore();
const chat = useChatStore();
const structure = useStructureStore();

const emit = defineEmits<{ detail: []; submit: [goal: string] }>();

/**
 * 静默多久之后把阶段文案换成「模型正在思考」。
 *
 * 后端只在静默达到 10 秒才发心跳，所以 `waitedMs >= 10` 基本等价于"已经静默 10 秒"。
 * 阈值再写一遍是为了前端自己也站得住：万一别处的实现改成更密的保活帧，这里不会
 * 1 秒就把阶段文案顶掉。
 */
const SILENT_HINT_SECONDS = 10;

/** 生成成功之后停多久再淡出。2 秒够看清"已落到画布"，又不至于一直挡着画布底部。 */
const FADE_DELAY_MS = 2000;

type IntentState = 'create' | 'edit-all' | 'edit-selected';

// ---------------------------------------------------------------- 输入

const intentText = ref('');
const stickyDiagram = ref(false);
const stickyThinking = ref(false);

/**
 * 偏好 → 预填。`prefs.defaults` 一变就重新填（面板里改一条，下拉跟着变）。
 *
 * `applyDefaults` 内部只填"用户没手动改过"的那两个——用户刚选过的值是他的意图，
 * 不能被一条偏好顶回去。`prefs.load()` 在 `App.onMounted` 里发起，这里跟着它的结果填。
 */
watch(() => prefs.defaults, defaults => options.applyDefaults(defaults),
  { immediate: true, deep: true });

/**
 * 「以后都这样」写进偏好。
 *
 * 必须在**发请求之前**完成：这一次请求的 `prefs_applied` 要能记上它，否则用户刚勾完
 * 就看到面板上写着"尚未生效"。
 */
async function syncStickyPrefs(): Promise<void> {
  const writes: Promise<boolean>[] = [];
  if (stickyThinking.value) {
    writes.push(prefs.save(THINKING_PREF_KEY, options.thinkingModel));
  }
  if (stickyDiagram.value && options.diagramType !== 'auto') {
    writes.push(prefs.save(DIAGRAM_PREF_KEY, options.diagramType));
  }
  if (!writes.length) return;
  await Promise.all(writes);
  stickyThinking.value = false;
  stickyDiagram.value = false;
  // `forget()` + `applyDefaults()` 这两步不能省：不调的话，用户手动选了流程图、勾了
  // "以后都这样"，而 `prefilled` 还是 false——这次生成的 `prefs_applied` 就是空的，
  // 面板上那条偏好显示"尚未生效"，明明它刚刚生效过。顺序是"先写、再让选项回到
  // 跟随偏好、最后算 appliedKeys"。
  //
  // 只在**真的写了**的时候做：没有勾选时这一步会清掉 `touched`，把用户刚刚手选的
  // 图型顶回偏好值——"用户显式选了之后偏好不再覆盖"那条性质就是这么坏的。
  options.forget();
  options.applyDefaults(prefs.defaults);
}

const intentState = computed<IntentState>(() => {
  if (!store.content) return 'create';
  return store.selectedIds.length ? 'edit-selected' : 'edit-all';
});

/**
 * 选区大小。只认 `content` 里真实存在的节点：`selectedIds` 可能残留着上一次的
 * 选中，而那时树已经换了一张——徽标说"已选 3 个"、请求里却是空，用户没法察觉。
 */
const selectionCount = computed(() => (store.content ? store.selectedIds.length : 0));

/** 材料那边的闸门（解析中、有失败项、超长）。没有材料时它是空串——材料是可选的。 */
const materialProblem = computed(() => sources.materialProblem);

/** 只给材料、不给主题时的备选主题。判据在 `materialTitle` 里，这里只取结果。 */
const materialTopicChoice = computed(() => materialTitle(sources.readySources));

const runBlocked = computed(() => store.busy || store.agentRunning);
const typed = computed(() => intentText.value.trim());

/**
 * 这次点下去会发什么；空串 = 还发不出去（按钮禁用，原因写在下面那行提示里）。
 *
 * 「只给材料」的那条路在这里落地：`mindmap_generate_deep` 只需要一个 topic 字符串，
 * 而模型看不见文件名，所以标题必须**写进 goal** 才真的算"用了它当主题"。
 */
const submitGoal = computed(() => {
  if (typed.value) return typed.value;
  if (intentState.value !== 'create') return '';
  const topic = materialTopicChoice.value.topic;
  return topic ? `用材料标题「${topic}」当主题，把材料整理成导图` : '';
});

const canSubmit = computed(() => {
  if (runBlocked.value || !submitGoal.value) return false;
  // 材料没准备好时**任何**一处入口都不发：编辑请求照样带 `sources`，正在解析或解析
  // 失败的那几份会被请求体静默漏掉（用户看到的是"改了，但新材料没算进去"）。
  // 这条闸门以前只挂在新建那条路上，改图那条提前 return true 把它跳过去了。
  if (materialProblem.value || sources.recording) return false;
  return true;
});

/** 材料够长到"这次一定不走外部检索"没有。够长时那个勾置灰。 */
const materialLongEnough = computed(
  () => mergedCharCount(sources.readySources) >= SHORT_MATERIAL_CHARS,
);

/** 这个开关这次真的会起作用：新建、且材料不够长。 */
const externalSearchApplies = computed(
  () => intentState.value === 'create' && !materialLongEnough.value,
);

/**
 * 联网检索这一次会怎么走：**判据与文案都只有这一份**。
 *
 * 这件事原先在两处各写一遍——勾选框旁的提示，与提交行下面那句"没传材料，会先去
 * 外部搜资料"。开关关掉之后，前者说"不会联网"、后者仍然说"会去搜"，两行同时挂在
 * 一个面板上互相打脸，而后端（`tools.external_knowledge_off_reason`）只认开关：
 * 错的是后者。
 *
 * 所以判据只留这里：三态（关闭 / 材料太短 / 材料充足）外加"这次是改图"那一态，
 * 两个渲染点都读同一句 `line`。
 */
type SearchState = {
  kind: 'edit' | 'off' | 'short' | 'enough';
  line: string;
};

const externalSearchState = computed<SearchState>(() => {
  // edit / expand 这条路不生成骨架、也不发检索请求：灰着就得说明原因，
  // 否则用户会以为"勾了没反应"是坏了。
  if (intentState.value !== 'create') {
    return { kind: 'edit', line: '这次是改图，不走联网检索' };
  }
  if (materialLongEnough.value) {
    // 置灰的原因必须写出来。灰着不解释，用户只会以为功能坏了。
    // 这一态排在开关之前：材料够长时开关本来就不起作用，说"已关闭"会让人
    // 以为关掉它是这次不联网的原因。
    return { kind: 'enough', line: '这次带材料，不走联网检索' };
  }
  if (!prefs.externalSearchEnabled) {
    return { kind: 'off', line: '已关闭：直接按主题生成，节点上不会有引用' };
  }
  return { kind: 'short', line: '会先去外部搜一轮，命中的资料会变成能点回原文的引用（约 4 秒）' };
});

const externalSearchHint = computed(() => externalSearchState.value.line);

/**
 * 勾选即落库。写失败时把勾**拨回去**：`:checked` 绑的是 store，而 store 没变就等于
 * 绑定的值没变、Vue 不会重渲染这个 prop——不手动复原的话，界面上显示"已关闭"、
 * 后端还是"开"，下一次生成照样联网。
 */
async function toggleExternalSearch(next: boolean, event: Event): Promise<void> {
  if (await prefs.save(EXTERNAL_SEARCH_PREF_KEY, next ? 'true' : 'false')) return;
  (event.target as HTMLInputElement).checked = prefs.externalSearchEnabled;
}

/** 这一次运行**实际带着哪些偏好**（与 `stores/options.ts` 同一口径，多一条联网检索）。 */
function appliedKeys(): string[] {
  const keys = options.appliedKeys();
  if (externalSearchApplies.value && prefs.value(EXTERNAL_SEARCH_PREF_KEY) !== null) {
    keys.push(`${EXTERNAL_SEARCH_PREF_KEY}=${prefs.externalSearchEnabled ? 'true' : 'false'}`);
  }
  return keys;
}

const wantedIntent = computed<'create' | 'edit'>(
  () => (intentState.value === 'create' ? 'create' : 'edit'),
);

const intentButton = computed(() => {
  // 只认**这一个入口**发起的那一轮：展开在跑的时候，「改这张图」不该显示「改中…」。
  const busyHere = store.agentRunning && store.runIntent === wantedIntent.value;
  if (busyHere) return wantedIntent.value === 'create' ? '生成中…' : '改中…';
  if (intentState.value === 'create') return '生成导图';
  if (intentState.value === 'edit-all') return '改这张图';
  return selectionCount.value === 1 ? '改这个节点' : `改这 ${selectionCount.value} 个节点`;
});

const intentPlaceholder = computed(() => {
  if (intentState.value === 'create') return '发消息或写一个主题…';
  if (intentState.value === 'edit-all') return '说说你想怎么改';
  return selectionCount.value === 1 ? '改选中的这个节点…' : `改选中的这 ${selectionCount.value} 个节点…`;
});

const intentLabel = computed(() => (intentState.value === 'create' ? '主题或要求' : '怎么改'));

const selectionLabel = computed(
  () => (selectionCount.value > 0 ? `已选 ${selectionCount.value} 个节点` : ''),
);

const intentHint = computed(() => {
  if (runBlocked.value) {
    return store.agentRunning ? '正在跑，等这一次结束。' : '正在生成，等这一次跑完再改。';
  }
  // 材料那道闸门对**改图**同样成立（见 `canSubmit`）。原因必须写在同一条提示里：
  // 否则改图时按钮灰着、下面这句还在说"先在画布上选中节点"，用户找不到为什么。
  if (sources.recording) return '正在录音：先松开「按住说话」，再提交。';
  if (materialProblem.value) return materialProblem.value;
  if (pastedMaterial.value && !typed.value) {
    return '正文已经当材料了：写一句要它怎么整理，不写就用材料标题当中心主题。';
  }
  if (intentState.value === 'create') {
    if (typed.value) {
      // 「会不会联网」与勾选框旁那句读的是**同一份判据**（`externalSearchState`）。
      // 这里曾自己写了一句"没传材料，会先去外部搜资料"，开关关掉之后它还在说会搜，
      // 与旁边那行正好相反；两份文案各自演化过一次就足够了。
      return sources.items.length
        ? `会带上这里附的材料一起生成。${externalSearchHint.value}`
        : externalSearchHint.value;
    }
    if (materialTopicChoice.value.topic) {
      return `没写主题：用材料标题「${materialTopicChoice.value.topic}」当中心主题。`;
    }
    return materialTopicChoice.value.reason
      ? `${materialTopicChoice.value.reason}——写一个主题，或者换一份标题像样的材料。`
      : '写一个主题，或者点「+」添加材料。';
  }
  if (intentState.value === 'edit-all') {
    return '先在画布上选中节点，或直接描述要改哪里（按住 Ctrl 点节点可以多选）。';
  }
  return selectionCount.value === 1
    ? '优先改这个节点及其子分支；别处要动也能动，清单里会标出来。'
    : `优先改这 ${selectionCount.value} 个节点及其子分支；别处要动也能动，清单里会标出来。`;
});

const intentHintWarn = computed(() => !!materialProblem.value);

/** 状态行只在"有一件事要讲"时出现。 */
const showStatus = computed(() => store.status !== INITIAL_STATUS);

/**
 * 「深度思考」：**这一次**走不走推理模型。
 *
 * 刻意不做成偏好：用户会在"随便出一张"和"这段逻辑要拆准"之间来回切，写进库里就把
 * 一次性的选择变成了永久状态。所以它只是组件里的一个开关，跟着这一次请求发出去
 * （`deep_thinking`），用完不复位——同一轮里连点两次生成时，第二句通常还是同一类任务。
 */
const deepThinking = ref(false);

/** 材料面板（粘贴 / 文件 / 录音 / 来源列表）开着没有。 */
const materialOpen = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);

async function submitIntent() {
  if (!canSubmit.value) return;
  const goal = submitGoal.value;
  if (!goal) return;
  // 选区随状态**显式**带上：徽标说「已选 3 个」时，请求里就正好是这三个。
  const selection = intentState.value === 'create' ? [] : [...store.selectedIds];
  const intent = intentState.value === 'create' ? 'create' : 'edit';
  const wanted = goal;
  intentText.value = '';
  // 主动收走焦点：组合输入与焦点都算"他正在写"的刹车，而提交之后他不再在写。
  // 不显式失焦的话，某些浏览器对 `disabled` 的 textarea 不发 blur，`focused` 会一直
  // 为真——那一轮成功之后对话框就再也不淡出了。
  (document.activeElement as HTMLElement | null)?.blur?.();
  // 这句话发出去之后，"用户要求留着"这件事就结束了：输入框已经空出来，
  // 接下来该按状态判（生成中就收起、成功就淡出）。
  pinned.value = false;
  // 那句"这段正文已放进材料"的提示随着这一轮发出去就过期了：材料进了请求体，
  // 再摆一个"放回输入框"只会把用户已提交的东西又抄回框里。
  pastedMaterial.value = null;
  // 发出去的那一刻就把这句话记进时间线：对话框随后会收成进度条，"我说了什么"
  // 不能只活在输入框里（那个框马上要清空）。
  emit('submit', wanted);
  await syncStickyPrefs();
  await store.runGeneration(
    goal, intent, sources.readySources,
    options.thinkingModel, options.diagramType,
    { selection, prefsApplied: appliedKeys(), deepThinking: deepThinking.value },
  );
}

/**
 * Enter 提交、Shift+Enter 换行。
 *
 * 输入法候选框里的回车**不是**提交：中文主题几乎都要经过那一步，而老写法
 * （`@keydown.enter.prevent`）会在组词过程中就把半截主题发出去。
 */
function onIntentKeydown(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.metaKey) return;
  event.preventDefault();
  void submitIntent();
}

// ---------------------------------------------------------------- 粘贴 = 材料
//
// 「粘贴正文」以前只有一条路：点「+」→ 面板展开 → 点里面那个粘贴框 → Ctrl+V →
// 点「添加文本到来源」→ 收起面板，五步，而且藏在"上传附件"的图标后面。入口分类错了：
// 「+」是**传文件**，粘贴是**另一种输入**，它不是附件的子类。
//
// 处置：长正文直接由那个"唯一说一句话的地方"接住。粘进来超过 `PASTE_AS_MATERIAL_CHARS`
// 的，就地变成一份材料，不进主题框；同时给一条能一键放回去的提示——自动分流必须是
// 可反悔的，否则它就从"少点四步"变成"抢走了我刚粘的东西"。

/** 粘到主题框里的正文多长就不再当"主题"。 */
const PASTE_AS_MATERIAL_CHARS = 120;

/**
 * 整段只有一个 http(s) 链接时的形状。
 *
 * 不允许任何空白：主题框里的粘贴是**整段**内容，含空格的多半是"标题 + 链接"或一整句
 * 话，那些该按原来的规则走。尾部标点是常见的复制残留（`…/a。`），剥掉再判一次。
 */
const URL_ONLY = /^https?:\/\/[^\s<>"']+$/i;

/** 这段粘贴是一个链接就返回它（已去首尾空白与尾部标点），否则 null。 */
function pastedLink(text: string): string | null {
  const trimmed = text.trim().replace(/[，。、；：）】」』]+$/u, '');
  if (!trimmed || /\s/.test(trimmed)) return null;
  return URL_ONLY.test(trimmed) ? trimmed : null;
}

/** 刚被自动分流的那一段正文 / 那一个链接（记着它落在哪一份材料里，才能撤回）。 */
const pastedMaterial = ref<{
  key: string; text: string; chars: number; link: boolean;
} | null>(null);

function onIntentPaste(event: ClipboardEvent): void {
  const text = event.clipboardData?.getData('text/plain') ?? '';
  // **链接优先于"长正文"**，理由是三件事叠在一起：
  //   1. 把 URL 当**主题**去生成一棵树是这份输入最坏的归宿——树会长成一棵以网址为
  //      中心的图，用户要的那篇内容一个字都没进来；
  //   2. 而 URL 通常不到 120 字，正好落进"当主题"那一支；
  //   3. 带一长串查询参数的 URL 会落进"当材料"那一支，那同样是一份没有内容的材料
  //      （正文就是一串百分号编码）。
  // 所以判据放在最前面、且与长度无关。读不到正文时**回退**到原来的分流，不吞内容。
  const link = pastedLink(text);
  if (link) {
    event.preventDefault();
    void attachLink(link);
    return;
  }
  const chars = text.trim().length;
  if (chars < PASTE_AS_MATERIAL_CHARS) return;   // 短的就是主题，照常粘进输入框
  event.preventDefault();
  const key = sources.addPastedText(text);
  // 加不进去（超上限、合并后超长）时 store 里已经有错误提示；正文没丢，仍在剪贴板。
  if (!key) return;
  pastedMaterial.value = { key, text, chars, link: false };
}

/**
 * 把一个链接读成材料，成功之后给一条可撤回的提示。
 *
 * **失败不弹提示条**：那一行材料自己的 `error` 里已经是后端给的原因（没打开 / 没配
 * key / 抓不到），再在输入框下面重复一句会让用户以为发生了两件事。同时这个链接
 * **不回填输入框**——它读不出来，放回去也还是读不出来；用户该做的是换一个链接或者
 * 直接贴正文，而那正是错误提示在那一行上说的话。
 */
async function attachLink(url: string): Promise<void> {
  const key = await sources.addFetchedUrl(url);
  if (!key) return;
  const item = sources.items.find(entry => entry.key === key);
  if (!item || item.state !== 'ready') return;
  pastedMaterial.value = { key, text: url, chars: url.length, link: true };
}

/** 把这段正文放回输入框，并撤掉刚建的那份材料。 */
function undoPastedMaterial(): void {
  const pasted = pastedMaterial.value;
  if (!pasted) return;
  sources.remove(pasted.key);
  intentText.value = pasted.text;
  pastedMaterial.value = null;
}

// 材料在别处被移除（缩略图上的 ✕、「清空来源」）时提示跟着走：它指的是**那一行**。
watch(() => sources.items.map(item => item.key), keys => {
  if (pastedMaterial.value && !keys.includes(pastedMaterial.value.key)) pastedMaterial.value = null;
});

// ---------------------------------------------------------------- 语音

const voiceMode = ref(false);

function toggleVoice(): void {
  voiceMode.value = !voiceMode.value;
  if (voiceMode.value) {
    // 切进语音就先把输入框的焦点交出去：移动端键盘会顶起半个屏幕，
    // 而「按住说话」期间根本不需要它。
    (document.activeElement as HTMLElement | null)?.blur?.();
  }
}

function onRecorded(file: File): void {
  void sources.addFiles([file]);
}

// ---------------------------------------------------------------- 状态行

type StatusKind = 'running' | 'error' | 'warn' | 'ok' | 'conflict';

const agentPartial = computed(
  () => store.agentWarnings.find(item => item.kind === 'partial') ?? null,
);

/**
 * 这次有几条诊断。
 *
 * 判据与抽屉里那份明细共用（`run-status.ts`）：状态行说"有 2 条诊断"、按钮上写着
 * "2 条诊断"，点开必须正好是那两条。两处各数一次的话，某一天它们会开始不一样。
 */
const diagnostics = computed(() => diagnosticCount(store.agentWarnings));

/**
 * 结构检查（`/optimization/check`）的那半句结论。
 *
 * 它与上面那个 `diagnostics` **不是同一个东西**：那个来自生成链上工具回的
 * `result_summary.diagnostics`（说的是"这次产出缺了什么"），这个来自只读的
 * `/optimization/check`（说的是"这棵树现在有什么结构问题"）。两个来源各自演化，
 * 合并成一个会立刻出现"这句话到底在说哪一次"的问题。
 */
const structureLine = computed(() => structureHint(structure.counts));

/**
 * 结构检查那边**需要人动手**的条数：warn / error 诊断 + 候选建议。
 *
 * **不含 info**。这一条同时决定两件事：状态行上显不显示数字，以及对话框会不会淡出。
 * 让 info 参与任何一件，"生成完只剩下画布"这条性质就当场失效——`SINGLE_CHILD` 在
 * 一棵正常的树上恒有十几条（任何链式子结构都命中），对话框将永远淡不出去。
 * 判据在 `mindmap/diagnostics.ts::structureHint`，这里只是取那个数。
 */
const structureAlert = computed(
  () => structure.counts.attention + structure.counts.suggestions,
);

/**
 * 状态行上那个按钮的文字。
 *
 * 优先说生成链自己的诊断（那是"这次生成有问题"），其次说结构检查的条数，都没有时
 * 退回「明细」——按钮本身仍然要在场，抽屉里还有改动清单与生成中的预览。
 */
const detailLabel = computed(() => {
  if (diagnostics.value > 0) return `${diagnostics.value} 条诊断`;
  if (structure.counts.suggestions > 0) return `${structure.counts.suggestions} 条建议`;
  if (structure.counts.attention > 0) return `${structure.counts.attention} 处要注意`;
  return '明细';
});

const detailTitle = computed(() => {
  if (diagnostics.value > 0) return `打开右侧抽屉：${diagnostics.value} 条诊断逐条列出`;
  if (structureAlert.value > 0) {
    return '打开右侧抽屉：结构检查的建议与诊断逐条列出';
  }
  return '打开右侧抽屉：改动清单、结构检查、生成中的预览';
});

/**
 * 生成落地 → **自动跑一次只读结构检查**。
 *
 * 为什么自动：它零成本（后端 docstring：“只读诊断”，不落库、不改树、不调模型），
 * 而"检查"一旦变成一件用户要记得去做的事，就等于没做。结果只并进状态行的一句话，
 * 点进去才看详情——不新增任何常驻界面，也不挡画布。
 *
 * 它**只跑 check，不跑 suggest**：suggest 现在是纯规则，但后端正在给它加模型层，
 * 那时"每次生成完自动跑"就变成"每次生成完自动花一次模型调用"。界线画在"用户点了
 * 才发"，后端加成本时前端不用改。
 *
 * 新一轮开始（`generatedSummary` 被置回 null——`runGeneration` 开头、换图、撤销生成
 * 都走这一步）时整份报告作废：它说的还是上一棵树。
 */
watch(() => store.generatedSummary, (now, before) => {
  if (now && !before) {
    structure.checkAfterGenerate();
    return;
  }
  if (!now && before) structure.reset();
});

/**
 * 「检查结构」按钮：手动跑一次 `check` + `suggest`，**带上材料**。
 *
 * 只有这条路径带 sources：后端拿到原文才会跑第三层引用核验（"点引用跳回原文"的依据
 * 就是它），而材料全文可能上万字——用户没点的时候不该发这么大的请求体。自动那次
 * 不带，只做结构三件套。
 */
function runStructureCheck(): void {
  structure.runFull(sources.readySources);
  // 结果在右抽屉里，所以顺手把它打开：点了按钮却什么都不出现，用户只会以为坏了。
  // 抽屉是覆盖式浮层，开合不改画布宽度（与 `emit('detail')` 走同一个出口）。
  emit('detail');
}

/**
 * 输入法组合中 / 焦点在主题框里。
 *
 * **为什么需要这两个**：`v-model` 在组合期间**不更新**——中文用户打「机器学习」四个字
 * 时，`intentText` 一直是空的，直到上屏那一刻才变成四个字。淡出计时器只看 `typed`，
 * 于是"他正在打字"与"他什么都没写"在代码里长得一模一样：2 秒到点，对话框连同他
 * 正在打的字一起淡出，上屏之后又淡回来——一次肉眼可见的闪烁，而且窗口期里他
 * 根本看不到自己打的东西。
 *
 * 焦点单独也要认：组合结束到松手之间还有一个空窗（`compositionend` 先到、字的
 * `input` 事件后到），只盯 `composing` 会在那一瞬间漏掉。
 */
const composing = ref(false);
const focused = ref(false);

/**
 * 这一行现在在说什么。**优先级从上到下**，同一时刻只说一件。
 *
 * 冲突排第一：它要用户在两个都会丢东西的选择里做决定，压住它就是丢数据。
 * 运行中排第二：进度要压过上一次的结果。异常排第三：失败不能因为"上次成功过"就不显示。
 *
 * 「上次没接住的结果」（`resumableRun`）**不在这张表里**：它是一份独立的待办，
 * 不是"此刻的状态"。塞进来就排在 `ok` 后面——刚生成成功那一次，上一次没保存的结果
 * 被这条状态行遮住、跟着对话框一起淡出，用户再没机会点「载入」。
 */
const statusKind = computed<StatusKind | null>(() => {
  if (store.conflictDraft) return 'conflict';
  if (store.busy || store.agentRunning) return 'running';
  if (store.agentError) return 'error';
  if (agentPartial.value) return 'warn';
  if (store.generatedSummary) return 'ok';
  return null;
});

/**
 * 上次那次运行的结果还等着处理：**独立一条**，与状态行并存。
 *
 * 两个"不显示"各有理由：
 *   - 运行中：那会儿进度才是他要看的，而这条提示不会因为这一轮结束而消失；
 *   - **就是刚刚这一次**（`run_id === store.pendingRunId`）：那棵树已经落到画布上、
 *     用户正看着它。此时再说一句"上次生成完的导图还没保存"，说的是他自己刚做的事，
 *     既没有信息量，也会在成功态下多出一条永不消失的提示。服务端确实会把这一次也
 *     列进 `pending()`（它只看到"有一条没保存的结果"），所以过滤只能放在这一层。
 */
const resumable = computed(() => {
  if (statusKind.value === 'running') return null;
  const run = store.resumableRun;
  if (!run) return null;
  if (run.run_id === store.pendingRunId) return null;
  return run;
});

/** 这条提示的那句话。文案与"被中断"分开——"完成了没保存"与"中途断了"不是一回事。 */
const resumableText = computed(() => {
  const run = resumable.value;
  if (!run) return '';
  const date = new Date(run.updated_at);
  const clock = Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
  const when = clock ? `（${clock}）` : '';
  // 第三种形态：跑完了但它只交出半棵树（`partial`）。说成"生成完的"是撒谎——用户
  // 载入之后才发现某几支是空的，那时画布已经是他的作品了。`partial === true` 才
  // 算数：缺字段是"这条记录没记过"，不是"确定完整"。
  //
  // 检查点（`checkpointed_at`）单独一档、排在 `partial` 前面：它交出来的**不是**
  // 半棵树，是完整结构 + 空的说明。说成"只完成了一部分"会让用户以为要重跑，而实际
  // 上他载入之后直接改就是一棵能用的树——两者下一步完全不同。
  // `node_count` 用 `!= null` 判而不是真值判：0 个节点是**合法**的（检查点落在裁剪
  // 之后、恰好一棵空树），`run.node_count ?` 会把它悄悄吞成"没存下节点"。
  const count = run.node_count != null ? `，已保存 ${run.node_count} 个节点` : '';
  const what = run.checkpointed_at
    ? `上次${when}生成在写说明之前断了，结构还在${count}`
    : run.status === 'cancelled'
      ? `上次${when}那次生成被中断了，已经生成的部分还留着`
      : run.partial === true
        ? `上次${when}生成只完成了一部分，没长完的树还留着`
        : `上次${when}生成完的导图还没保存`;
  return run.goal ? `${what}：「${run.goal}」` : `${what}。`;
});

const runningVerb = computed(() => {
  if (store.runIntent === 'expand') return '正在展开这一支';
  if (store.runIntent === 'edit') return '正在改这张图';
  return '正在生成';
});

/**
 * 已经跑了多少秒。**只用于状态行**，不写进 store：它是纯展示计时器，放进 store
 * 会让每一下心跳都触发一轮与数据无关的重算。
 */
const elapsedSec = ref(0);
let elapsedTimer: ReturnType<typeof setInterval> | null = null;

function stopElapsed(): void {
  if (elapsedTimer) clearInterval(elapsedTimer);
  elapsedTimer = null;
  elapsedSec.value = 0;
}

watch(
  () => store.busy || store.agentRunning,
  running => {
    if (!running) {
      stopElapsed();
      return;
    }
    if (elapsedTimer) return;
    elapsedSec.value = 0;
    elapsedTimer = setInterval(() => { elapsedSec.value += 1; }, 1000);
  },
  { immediate: true },
);

const statusText = computed(() => {
  switch (statusKind.value) {
    case 'conflict':
      return '生成期间你改过这张图：两棵树都还在，选一个';
    case 'running': {
      const parts = [runningVerb.value];
      if (store.waitedMs >= SILENT_HINT_SECONDS) {
        parts.push(`模型正在思考，已等 ${store.waitedMs} 秒`);
      } else {
        if (store.stageText) parts.push(store.stageText);
        if (elapsedSec.value > 0) parts.push(`${elapsedSec.value} 秒`);
      }
      return parts.join(' · ');
    }
    case 'error':
      return `生成失败：${store.agentError}`;
    case 'warn':
      return `部分完成：${agentPartial.value?.message ?? '有几支没能生成'}`;
    case 'ok': {
      const summary = store.generatedSummary;
      if (!summary) return '';
      const nodes = summary.nodes ? `${summary.nodes} 个节点` : '';
      const changes = summary.changes == null ? '' : `，改了 ${summary.changes} 处`;
      // 两处结论共用**一个**提示位：状态行只有一行，两段同时挂上去就是一句读不完的
      // 话。生成链自己的诊断优先（"这次产出不完整"更紧急），没有它才轮到结构检查。
      const hint = diagnosticsHint(diagnostics.value) || structureLine.value;
      const tail = hint ? ` · ${hint}` : '';
      return `已落到画布${nodes ? `（${nodes}${changes}）` : changes}${tail} · 还没保存`;
    }
    default:
      return '';
  }
});

interface StatusAction { key: string; label: string; test: string; run: () => void }

const statusActions = computed<StatusAction[]>(() => {
  switch (statusKind.value) {
    case 'conflict':
      return [
        { key: 'use', label: '用模型的结果', test: 'run-status-use',
          run: () => store.resolveConflict('generated') },
        { key: 'keep', label: '保留我的', test: 'run-status-keep',
          run: () => store.resolveConflict('mine') },
      ];
    case 'running':
      return [
        { key: 'cancel', label: '取消', test: 'run-status-cancel', run: () => store.cancel() },
      ];
    case 'ok':
      return store.canUndoGenerate
        ? [{ key: 'undo', label: '回到生成之前', test: 'run-status-undo',
            run: () => store.undoGenerate() }]
        : [];
    default:
      return [];
  }
});

/**
 * 「上次没接住的结果」那几个动作：捡回来，或者这次会话里不要再问。
 *
 * 检查点上多一个「补齐说明」：那种结果的结构是完整的、缺的只是节点说明，而补说明
 * 是一条独立的、更便宜的往返（只发写说明那几次调用，不重跑骨架与结构）。没有这个
 * 按钮，用户唯一的补救办法是重跑一次生成——既换出一棵可能不一样的树，也多花三倍
 * 的调用。
 *
 * **判据是"这行还能被重开"，不是 `checkpointed_at`**：那个时间戳只在收尾时交出
 * 候选的那一刻被清（`runs.finish`），而补说明失败那一轮照样会交出 edit 的快照，
 * 于是失败一次之后入口就永久消失了——用户明明还缺说明，按钮却没了，文案也退化成
 * "生成完的导图还没保存"。能重开（cancelled / failed）才是"这次点下去真的会跑"的
 * 判据：`runs.begin()` 对这两个状态是重开同一行，对 `done` 是幂等重放（点了也不会
 * 补，只会把旧结果再放一遍）。何况树上没有可补的节点时，工具本身零调用返回
 * `nothing_to_fill`，多摆一个按钮不花任何额度。
 */
const resumableActions = computed<StatusAction[]>(() => {
  const actions: StatusAction[] = [
    { key: 'load', label: '载入', test: 'run-status-load', run: () => void store.resumeRun() },
  ];
  const status = resumable.value?.status;
  if (status === 'cancelled' || status === 'failed') {
    actions.push({
      key: 'fill',
      label: '补齐说明',
      test: 'run-status-fill',
      // 先载入再补：补说明要的是"画布上是这棵树"，而后端那条路要求带 `snapshot`。
      // 载入失败（换过图 / 服务端没了）就不该继续发补写请求——那会在另一张图上跑。
      run: () => void fillDetailsAfterLoad(),
    });
  }
  actions.push({ key: 'skip', label: '不用了', test: 'run-status-skip', run: () => store.ignoreResumable() });
  return actions;
});

/**
 * 「补齐说明」：载入检查点，再让模型只补 `detail`。
 *
 * 两步而不是一步：补说明那条路的输入是**画布上的树**（后端按 `snapshot` 走
 * `edit`），所以要先把检查点装进画布。`resumeRun()` 返回布尔——换图、服务端没
 * 内容这两种情况它自己已经提示过，这里不该在同一件事上再叠一句。
 *
 * `run_id` 必须在载入**之前**取：载入成功会清 `resumableRun`，之后再读只会拿到
 * null，于是补说明会悄悄新开一个身份，台账上同一次生成变成两次运行。
 */
async function fillDetailsAfterLoad(): Promise<void> {
  const runId = resumable.value?.run_id;
  const loaded = await store.resumeRun();
  if (!loaded) return;
  await store.fillMissingDetails(runId);
}

const statusTone = computed(() => {
  switch (statusKind.value) {
    case 'error':
      return 'border-rose-300 bg-rose-50 text-rose-900 ring-1 ring-rose-200';
    case 'warn':
    case 'conflict':
      return 'border-amber-300 bg-amber-50 text-amber-900 ring-1 ring-amber-200';
    case 'ok':
      return 'border-emerald-200 bg-white text-slate-700';
    default:
      return 'border-slate-200 bg-white text-slate-700';
  }
});

const statusDot = computed(() => {
  switch (statusKind.value) {
    case 'running': return 'bg-sky-500';
    case 'error': return 'bg-rose-500';
    case 'warn':
    case 'conflict': return 'bg-amber-500';
    case 'ok': return 'bg-emerald-500';
    default: return 'bg-sky-400';
  }
});

const showDetailAction = computed(
  () => statusKind.value !== null,
);

/** 抽屉里有没有东西可展开（判据与 `AgentPanel` 共用）。 */
const agentHasDetail = computed(() => agentDetailVisible(store));

// ---------------------------------------------------------------- 状态机

/**
 * 强制把对话框叫回来（选中节点点「改」、点「+」、点悬浮球里的一条）。
 *
 * 它只压住"淡出"这一件事，不改生成中的形态：正在跑的时候点「改」仍然只看到进度条，
 * 因为那会儿改不了任何东西。
 */
const pinned = ref(false);
const dismissed = ref(false);
let dismissTimer: ReturnType<typeof setTimeout> | null = null;

function clearDismiss(): void {
  if (dismissTimer) clearTimeout(dismissTimer);
  dismissTimer = null;
}

/**
 * 生成成功后"只剩画布"这一件事，什么时候该让位。
 *
 * 刹车（缺一条都会让用户下一步不知道点哪儿）：
 *   1. 画布上还没有图——他从没成功过，收走对话框等于收走了唯一的入口；
 *   2. 输入框里还有没发出去的字，或者**正在用输入法打**（组合中 `v-model` 是空的）、
 *      或者焦点还在框里——都是"他正在写"，收走就丢了；
 *   3. 材料粘贴框里还有没点「添加」的正文——同上，而且它住在一个会因为淡出而
 *      看不到的面板里（草稿本身在 store 里，不丢；但看不见 = 以为丢了）；
 *   4. 这一次失败了 / 部分完成 / 有冲突——这几种都在等他做决定。
 *
 * **「上次没保存的结果」不在这张表里**，理由与它独立成行是同一条：它只在
 * `statusKind === 'ok'` 时才可能多挡住一次淡出，而那恰好是"刚生成成功"——用户
 * 正要看的是新结果，"上次"这件事此刻并不需要他做决定。实测把它当刹车之后，
 * 只要历史里有一条没保存的结果，**每一次生成完的对话框都再也不淡出**，产品那条
 * "生成完只剩画布"直接失效。它靠独立那一行 + 悬浮球的回程入口保持可达。
 */
const shouldKeep = computed(() => {
  if (pinned.value) return true;
  if (!store.content) return true;
  if (typed.value) return true;
  if (composing.value || focused.value) return true;
  if (sources.draftText.trim()) return true;
  const kind = statusKind.value;
  if (kind && kind !== 'ok' && kind !== 'running') return true;
  if (kind === 'running') return true;
  // 有诊断＝**该看的东西还没看**。它是第 4 条刹车（失败 / 部分完成 / 冲突）的延伸：
  // 结果是成功了，但有几条"某支没展开 / 有重复节点"等着人处理，两秒后连入口一起
  // 淡掉就是把它变成一句谁也找不到的提示（实测：用户找不到"诊断"在哪）。
  if (diagnostics.value > 0) return true;
  // 结构检查的 warn / 建议同理，但**只认需要人动手的那一档**：info 不刹车。
  // 让它刹车的话，任何一张正常的树上都有十几条 `SINGLE_CHILD`，对话框将永远淡不出去
  // ——那正是"生成完只剩画布"这条产品性质的反面。
  if (structureAlert.value > 0) return true;
  return false;
});

/**
 * 这一刻"成功且有图、没有任何东西正在被写"——只有这种才安排淡出。
 *
 * 单独成 computed 而不是让 watch 里去重算 `shouldKeep`：`shouldKeep` 含 `pinned`，
 * 而定时器到点时要把 `pinned` 松开（见下面）。两件事混在一个布尔里，淡出要么永远
 * 等不到（pinned 被当成刹车），要么把"用户刚点开输入框"也淡出去。
 */
const canFade = computed(() => statusKind.value === 'ok'
  && !!store.content
  && !typed.value
  && !composing.value
  && !focused.value
  && !sources.draftText.trim()
  // 有诊断就不安排淡出（同 `shouldKeep` 最后那条）：这一轮的结论里还有一笔没算完。
  && diagnostics.value === 0
  // 结构检查同理，且同样**只认 warn / 建议**（见 `structureAlert`）。
  && structureAlert.value === 0);

watch(
  () => [canFade.value, store.nodeCount] as const,
  ([fadeable]) => {
    clearDismiss();
    dismissed.value = false;
    if (!fadeable) return;
    dismissTimer = setTimeout(() => {
      // 定时器到点时把 `pinned` 一并松开：用户点过「改」把对话框叫回来之后，
      // 那一轮结束后它仍然该按状态判（否则它再也淡不出去）。
      pinned.value = false;
      dismissed.value = true;
    }, FADE_DELAY_MS);
  },
  { immediate: true },
);

const running = computed(() => statusKind.value === 'running');
/** 生成中：整块收成一条细进度。 */
const compact = computed(() => running.value);
/**
 * 生成完淡出：**opacity 归零而不是 v-if**，DOM 留着——「继续说一句」要能把它原样
 * 叫回来，`v-if` 会把材料面板、输入框状态一起扔掉。
 *
 * 但"留着"不等于"还能操作"：淡出时整块要 `inert`。只给 `opacity-0` +
 * `pointer-events-none` + `aria-hidden` 挡得住鼠标和读屏，挡不住 Tab——焦点会走进
 * 一个用户根本看不见的对话框，接着再按 Enter 就是对着空气提交。
 */
const faded = computed(() => dismissed.value && !shouldKeep.value && !compact.value);

function openComposer(prefill?: string): void {
  pinned.value = true;
  dismissed.value = false;
  clearDismiss();
  if (prefill !== undefined) intentText.value = prefill;
  void nextTick(() => {
    const el = document.getElementById('intent') as HTMLTextAreaElement | null;
    el?.focus();
  });
}

/**
 * 取走"还没发出去的那句话"并清空输入框；返回它（空串表示本来就空）。
 *
 * 「新对话」用它做两件事：把草稿一起清掉（否则点了 + 之后输入框里还留着上一轮的
 * 半句话，用户会以为新对话没生效），以及把这段文字交给撤销按钮——清掉的东西必须
 * 能原样放回来。
 */
function takeDraft(): string {
  const draft = intentText.value;
  intentText.value = '';
  return draft;
}

function closeMaterial(): void {
  materialOpen.value = false;
}

function openMaterial(): void {
  materialOpen.value = true;
}

function pickFiles(): void {
  // 「+」既开材料面板又直接弹文件选择框：它是截图里那个加号，用户点它是想传东西。
  materialOpen.value = true;
  void nextTick(() => fileInput.value?.click());
}

/** `+` 弹出来的文件选择：与材料面板里的入口走同一条上传链路。 */
async function onPickMaterials(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const selected = Array.from(input.files ?? []);
  // 再选同一个文件也要触发 change。
  input.value = '';
  await sources.addFiles(selected);
}

onBeforeUnmount(() => {
  stopElapsed();
  clearDismiss();
  window.removeEventListener('beforeunload', guardUnload);
});

/**
 * 关页面提醒：材料没入库、粘贴框里还有没点「添加」的正文、或者正在录音。
 *
 * **挂在 `ChatDock` 而不是 `SourceInputs`**：后者在生成中会被 `v-if` 整个卸载
 * （对话框收成一条细进度），那一刻起提醒就没了——而"草稿还在、关掉就丢"这件事
 * 恰恰发生在用户最可能离开的时候。`ChatDock` 只淡出不卸载，是这个提醒唯一
 * 站得住的挂载点。
 */
function guardUnload(event: BeforeUnloadEvent): void {
  if (!sources.items.length && !sources.draftText.trim() && !sources.recording) return;
  event.preventDefault();
  event.returnValue = '';
}

onMounted(() => window.addEventListener('beforeunload', guardUnload));

defineExpose({
  openComposer, openMaterial, closeMaterial, focusInput: () => openComposer(), takeDraft,
});
</script>

<template>
  <!--
    `pr` 的两档：消息面板停在右下角时，这里让出它的宽度（380 + 24 的间隙），
    对话框整块左移居中——两者从"横向切进去 136px"变成并排。
    窄屏（<900px）那边面板自己改成整屏抽屉（`MessageFab.vue`），对话框不再让位。
  -->
  <div
    class="pointer-events-none absolute inset-x-0 bottom-0 z-30 flex justify-center pb-4 pl-4 transition-all duration-300"
    :class="chat.open ? 'pr-4 min-[900px]:pr-[404px]' : 'pr-4'"
  >
    <div
      data-test="chat-dock"
      :data-faded="faded ? 'true' : 'false'"
      class="pointer-events-auto w-full max-w-[760px] transition-all duration-300"
      :class="faded ? 'pointer-events-none translate-y-2 opacity-0' : 'translate-y-0 opacity-100'"
      :aria-hidden="faded ? 'true' : undefined"
      :inert="faded ? true : undefined"
    >
      <!-- 生成中：一条细进度。它取代了整个对话框（不是叠在上面）——两处都说"正在生成"
           只会互相削弱，而画布此刻正是用户想看的东西。 -->
      <div
        v-if="compact"
        data-test="run-status"
        class="flex items-center gap-2 rounded-full border border-slate-200 bg-white px-4 py-2 text-xs shadow-lg"
        aria-live="polite"
      >
        <span class="h-2 w-2 shrink-0 animate-pulse rounded-full bg-sky-500" aria-hidden="true" />
        <span class="min-w-0 flex-1 truncate font-medium text-slate-700" data-test="run-status-text">
          {{ statusText }}
        </span>
        <button
          v-for="action in statusActions"
          :key="action.key"
          type="button"
          :data-test="action.test"
          class="shrink-0 rounded-full border border-slate-300 bg-white px-3 py-0.5 text-xs hover:bg-slate-50"
          @click="action.run()"
        >{{ action.label }}</button>
      </div>

      <!-- 空闲 / 失败 / 有草稿：完整对话框。 -->
      <div
        v-else
        class="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-xl shadow-slate-900/5"
      >
        <!-- 材料缩略图区：左栏那个 320px 常驻区块的内容全在这里（横向），画布因此
             回到全宽。每份材料一个缩略块 + ✕；状态（解析中/失败）就地显示。

             `flex-nowrap` + `overflow-x-auto`：材料多了就**横向滚**，不换行也不撑宽
             ——换行会把对话框一层层顶高，直到把画布盖住。`min-w-0` 是那半个坑：
             这一行是 flex 子项，默认 `min-width:auto` 会按内容的最小宽度顶回去，
             溢出容器就白设了。

             「管理材料」放在滚动区**外面**（不是当成最后一个缩略块）：它是个常驻
             入口，材料一多就被推出视野、得先滚到底才点得到，等于没有。
        -->
        <div
          v-if="sources.items.length"
          class="flex items-center gap-2 border-b border-slate-100 pr-3"
        >
          <div
            data-test="thumbs"
            class="flex min-w-0 flex-1 flex-nowrap gap-2 overflow-x-auto overflow-y-hidden px-3 py-2"
          >
            <div
              v-for="item in sources.items"
              :key="item.key"
              class="group relative flex w-[132px] shrink-0 flex-col gap-1 rounded-lg border border-slate-200 bg-slate-50 px-2 py-1.5"
              :title="item.label"
            >
              <button
                type="button"
                class="absolute -right-1.5 -top-1.5 z-10 flex h-5 w-5 items-center justify-center rounded-full border border-slate-300 bg-white text-[11px] text-slate-500 hover:text-rose-600 disabled:opacity-50"
                :aria-label="`移除 ${item.label}`"
                :disabled="store.busy"
                @click="sources.remove(item.key)"
              >✕</button>
              <span class="truncate text-[11px] font-medium text-slate-700">{{ item.label }}</span>
              <span class="text-[10px] text-slate-500">
                {{ SOURCE_LABELS[item.kind] }}
                <template v-if="item.state === 'ready' && item.source">
                  · {{ Array.from(item.source.text?.trim() ?? '').length }} 字
                </template>
              </span>
              <span v-if="item.state === 'queued'" class="text-[10px] text-slate-500">等待解析…</span>
              <span v-else-if="item.state === 'parsing'" class="text-[10px] text-slate-500" aria-live="polite">
                正在解析…{{ item.seconds }} 秒
              </span>
              <span v-else-if="item.state === 'failed'" class="text-[10px] leading-tight text-rose-700" role="alert">
                {{ item.error }}
              </span>
              <button
                v-if="item.state === 'failed'"
                type="button"
                class="text-left text-[10px] text-slate-600 underline disabled:opacity-50"
                :disabled="store.busy"
                @click="sources.retry(item.key)"
              >重试</button>
            </div>
          </div>
          <button
            type="button"
            class="shrink-0 rounded-lg border border-dashed border-slate-300 px-2 text-[11px] text-slate-500 hover:bg-slate-50"
            @click="openMaterial"
          >管理材料</button>
        </div>

        <!--
          `data-test="intent-box"` 保留在这个容器上：一批用例按它定位提交按钮
          （`[data-test="intent-box"] button` 取文档顺序第一个）。所以提交按钮是这个
          容器里的**第一个** button，视觉位置靠 `order-last` 排到最右。
        -->
        <div data-test="intent-box" role="group" aria-label="生成与改图" class="flex flex-col gap-1.5 p-2.5">
          <div class="flex items-center justify-between gap-2">
            <label for="intent" class="text-xs font-medium text-slate-600">{{ intentLabel }}</label>
            <span
              v-if="selectionLabel"
              data-test="edit-selection"
              class="shrink-0 rounded bg-sky-50 px-1 py-0.5 text-[11px] text-sky-700"
            >{{ selectionLabel }}</span>
          </div>

          <!-- 语音态：输入框换成「按住说话」。右侧那个 ⌨ 把键盘输入换回来。 -->
          <VoicePress
            v-if="voiceMode"
            :disabled="runBlocked"
            @recorded="onRecorded"
            @active="sources.recording = $event"
            @failed="() => {}"
          />

          <textarea
            v-if="!voiceMode"
            id="intent"
            v-model="intentText"
            rows="2"
            aria-label="主题或修改指令"
            class="resize-none rounded border border-slate-300 px-2 py-1 text-xs outline-none focus:border-slate-500 disabled:opacity-50"
            :disabled="runBlocked"
            :placeholder="intentPlaceholder"
            @keydown="onIntentKeydown"
            @paste="onIntentPaste"
            @compositionstart="composing = true"
            @compositionend="composing = false"
            @focus="focused = true"
            @blur="focused = false"
          />

          <!-- 自动分流是可反悔的：这段正文去哪了、怎么放回去，就在原地写着。 -->
          <div
            v-if="pastedMaterial"
            data-test="paste-material"
            class="flex items-center gap-2 rounded border border-sky-200 bg-sky-50 px-2 py-1 text-[11px] text-sky-900"
          >
            <span class="min-w-0 flex-1">
              <template v-if="pastedMaterial.link">
                这个链接已经读成材料，主题框里说要它做什么。
              </template>
              <template v-else>
                这段正文（{{ pastedMaterial.chars }} 字）已经放进材料，主题框里说要它做什么。
              </template>
            </span>
            <button
              type="button"
              data-test="paste-material-undo"
              class="shrink-0 underline decoration-sky-400 underline-offset-2 hover:text-sky-700"
              @click="undoPastedMaterial"
            >放回输入框</button>
          </div>

          <!--
            拆解方式与图型只在**新建**那一次生效：后端的编辑路径既不消费
            `thinking_model`，也不生成骨架。摆一个选了不起作用的控件比不摆更糟。
          -->
          <details
            v-if="intentState === 'create'"
            class="rounded border border-slate-100 bg-slate-50/60 px-2 py-1"
          >
            <summary class="cursor-pointer text-[11px] text-slate-500">
              拆解方式与图型<span v-if="options.prefilled.thinking || options.prefilled.diagram" class="text-sky-700">（含偏好预填）</span>
            </summary>
            <div class="mt-1 flex flex-col gap-1">
              <div class="flex items-center gap-1.5 text-[11px] text-slate-500">
                <label for="thinking-model" class="w-14 shrink-0">拆解方式</label>
                <select
                  id="thinking-model"
                  :value="options.thinkingModel"
                  :disabled="runBlocked"
                  title="按这种方式拆解主题"
                  class="min-w-0 flex-1 rounded border border-slate-300 px-1 py-1 text-[11px] outline-none focus:border-slate-500 disabled:opacity-50"
                  @change="options.chooseThinking(($event.target as HTMLSelectElement).value)"
                >
                  <option v-for="item in THINKING_MODEL_OPTIONS" :key="item.value" :value="item.value">{{ item.label }}</option>
                </select>
              </div>
              <div class="flex items-center justify-between gap-2 text-[11px]">
                <p class="leading-tight text-slate-400">
                  按这种方式拆解主题
                  <span v-if="options.prefilled.thinking" class="text-sky-700">（来自偏好：{{ options.thinkingLabel }}）</span>
                </p>
                <label class="flex shrink-0 items-center gap-1 text-slate-500">
                  <input
                    v-model="stickyThinking"
                    type="checkbox"
                    :disabled="runBlocked"
                    data-test="sticky-thinking"
                  />
                  以后都这样
                </label>
              </div>
              <div class="flex items-center gap-1.5 text-[11px] text-slate-500">
                <label for="diagram-type" class="w-14 shrink-0">图型</label>
                <select
                  id="diagram-type"
                  :value="options.diagramType"
                  :disabled="runBlocked"
                  title="这张图要画成什么形状"
                  class="min-w-0 flex-1 rounded border border-slate-300 px-1 py-1 text-[11px] outline-none focus:border-slate-500 disabled:opacity-50"
                  @change="options.chooseDiagram(($event.target as HTMLSelectElement).value as DiagramTypeChoice)"
                >
                  <option v-for="item in DIAGRAM_TYPE_OPTIONS" :key="item.value" :value="item.value">{{ item.label }}</option>
                </select>
              </div>
              <div class="flex items-center justify-between gap-2 text-[11px]">
                <p class="leading-tight text-slate-400">
                  {{ options.diagramHint }}
                  <span v-if="options.prefilled.diagram" class="text-sky-700">（来自偏好：{{ options.diagramLabel }}）</span>
                </p>
                <label class="flex shrink-0 items-center gap-1 text-slate-500">
                  <input
                    v-model="stickyDiagram"
                    type="checkbox"
                    :disabled="runBlocked"
                    data-test="sticky-diagram"
                  />
                  以后都这样
                </label>
              </div>
            </div>
          </details>

          <!-- 按钮行。视觉顺序靠 `order`，DOM 顺序靠"提交在最前"——后者是测试契约。 -->
          <div class="flex items-center gap-1.5 text-[11px]">
            <button
              type="button"
              class="order-last flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-slate-900 text-white before:content-['↑'] disabled:opacity-30"
              :disabled="!canSubmit"
              :aria-label="intentButton"
              :title="intentButton"
              @click="submitIntent"
            >
              <!-- 箭头用 CSS 伪元素画：`text()` 里只剩按钮语义（"生成导图 / 改这张图"），
                   读屏不会读成一个孤零零的箭头，用例也不必为它改断言。 -->
              <span class="sr-only">{{ intentButton }}</span>
            </button>
            <!-- 「深度思考」：这一次的开关（不是设置）。 -->
            <button
              type="button"
              data-test="deep-thinking"
              class="order-1 shrink-0 rounded-full border px-2 py-0.5"
              :class="deepThinking
                ? 'border-sky-300 bg-sky-50 text-sky-700'
                : 'border-slate-200 text-slate-500 hover:bg-slate-50'"
              :aria-pressed="deepThinking"
              :disabled="runBlocked"
              title="这一次走推理模型：先想一遍再动手，慢一些，拆解更稳。检索仍走普通模型。"
              @click="deepThinking = !deepThinking"
            >深度思考</button>
            <!-- 「智能搜索」：原来那个联网检索开关。它写的是偏好，所以勾一下就落库。 -->
            <label
              class="order-2 shrink-0 cursor-pointer rounded-full border px-2 py-0.5"
              :class="prefs.externalSearchEnabled && externalSearchApplies
                ? 'border-sky-300 bg-sky-50 text-sky-700'
                : externalSearchApplies
                  ? 'border-slate-200 text-slate-500 hover:bg-slate-50'
                  : 'border-slate-200 text-slate-400'"
              :title="externalSearchHint"
            >
              <input
                type="checkbox"
                class="sr-only"
                :checked="prefs.externalSearchEnabled"
                :disabled="runBlocked || !externalSearchApplies || prefs.saving === EXTERNAL_SEARCH_PREF_KEY"
                data-test="external-search"
                @change="toggleExternalSearch(($event.target as HTMLInputElement).checked, $event)"
              />
              智能搜索
            </label>
            <!--
              「检查结构」：手动跑一次 `/optimization/check` + `/suggest`。

              自动那次（生成落地之后）只跑 check、不带材料；这里**带上材料**，所以后端
              会连第三层引用核验一起做——那是"点引用能跳回原文"的依据。

              **只在画布上有图时出现**：没有图就没有检查对象，摆一个点了没反应的按钮
              比不摆更糟。数字只在有 warn / 建议时出现（info 那一档不进这里，理由见
              `structureAlert`）。
            -->
            <button
              v-if="store.content"
              type="button"
              data-test="structure-check-run"
              class="order-3 shrink-0 rounded-full border px-2 py-0.5"
              :class="structureAlert > 0
                ? 'border-amber-300 bg-amber-50 text-amber-800'
                : 'border-slate-200 text-slate-500 hover:bg-slate-50'"
              :disabled="runBlocked || structure.busy"
              :title="structureAlert > 0
                ? `这张图有 ${structureAlert} 处值得看一眼；打开右侧逐条看`
                : '检查这张图的结构：单子节点、深度上限、同级重名、引用能否核验。只读，不改任何东西。'"
              @click="runStructureCheck"
            >{{ structure.busy ? '检查中…' : structureAlert > 0 ? `检查 · ${structureAlert}` : '检查' }}</button>
            <p class="order-4 min-w-0 flex-1 truncate text-right leading-tight text-slate-400" data-test="external-search-hint">
              {{ externalSearchHint }}
            </p>
            <button
              type="button"
              data-test="attach"
              class="order-5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-slate-200 text-slate-600 hover:bg-slate-50 disabled:opacity-40"
              :disabled="runBlocked"
              aria-label="添加材料"
              title="添加材料（文档 / 图片 / 文本 / 录音）"
              @click="pickFiles"
            >+</button>
            <button
              type="button"
              data-test="voice-toggle"
              class="order-6 flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-slate-200 text-slate-600 hover:bg-slate-50 disabled:opacity-40"
              :class="voiceMode ? 'border-slate-900 bg-slate-100 text-slate-900' : ''"
              :disabled="runBlocked"
              :aria-pressed="voiceMode"
              :aria-label="voiceMode ? '切回键盘输入' : '语音输入'"
              :title="voiceMode ? '切回键盘输入' : '语音输入（按住说话）'"
              @click="toggleVoice"
            >{{ voiceMode ? '⌨' : '🎤' }}</button>
          </div>

          <p
            data-test="intent-hint"
            class="text-[11px] leading-tight"
            :class="intentHintWarn ? 'text-amber-700' : 'text-slate-400'"
          >{{ intentHint }}</p>

          <!-- 状态行：失败 / 部分完成 / 冲突 / 成功。运行中的那一版在上面的细进度条里。 -->
          <div
            v-if="statusKind"
            data-test="run-status"
            aria-live="polite"
            class="flex items-center gap-2 rounded-lg border px-2 py-1.5 text-xs"
            :class="statusTone"
          >
            <span class="h-2 w-2 shrink-0 rounded-full" :class="statusDot" aria-hidden="true" />
            <span class="min-w-0 flex-1 truncate font-medium" :title="statusText" data-test="run-status-text">
              {{ statusText }}
            </span>
            <button
              v-for="action in statusActions"
              :key="action.key"
              type="button"
              :data-test="action.test"
              class="shrink-0 rounded-md border border-slate-300 bg-white px-2 py-0.5 text-xs hover:bg-slate-50"
              @click="action.run()"
            >{{ action.label }}</button>
            <button
              v-if="showDetailAction && agentHasDetail"
              type="button"
              data-test="run-status-detail"
              class="shrink-0 rounded-md px-2 py-0.5 text-xs underline decoration-dotted underline-offset-2 hover:bg-white/60"
              :title="detailTitle"
              @click="emit('detail')"
            >{{ detailLabel }}</button>
          </div>

          <!--
            「上次没接住的结果」：**独立一条**，与上面那行状态并存。

            它以前是 `statusKind` 里的 `info`，排在 `ok` 后面。于是"刚生成成功、
            上一次的结果还没保存"这种最普通的情形下，它被状态行遮住，随后跟着对话框
            一起淡出——`runs` 表里那份结果从"该被捡回来"变成"没人知道它在"。
            它是一件**等用户做决定**的待办（载入 / 不用了），不是"此刻的状态"，
            所以不能被抢位。但它**不**参与淡出判定：那是实测之后放弃的（见
            `shouldKeep` 的注释）——它在唯一会生效的场景下正好挡错了人。
          -->
          <div
            v-if="resumable"
            data-test="resumable-notice"
            aria-label="上次的生成结果"
            aria-live="polite"
            class="flex items-center gap-2 rounded-lg border border-sky-200 bg-sky-50 px-2 py-1.5 text-xs text-sky-900"
          >
            <span class="h-2 w-2 shrink-0 rounded-full bg-sky-400" aria-hidden="true" />
            <span class="min-w-0 flex-1 truncate font-medium" :title="resumableText">
              {{ resumableText }}
            </span>
            <button
              v-for="action in resumableActions"
              :key="action.key"
              type="button"
              :data-test="action.test"
              class="shrink-0 rounded-md border border-sky-300 bg-white px-2 py-0.5 text-xs hover:bg-sky-50"
              @click="action.run()"
            >{{ action.label }}</button>
          </div>

          <!-- `role="status"` 只该有一个来源：这一行。生成中不渲染（细进度条已经占位）。 -->
          <p
            v-if="showStatus && statusKind !== 'running'"
            role="status"
            class="text-[11px] leading-tight text-slate-500"
          >{{ store.status }}</p>
        </div>

        <!--
          材料面板：粘贴正文、上传文件、录音、来源列表与引用计数。

          它**在 `intent-box` 之外**：面板里的说明会重复输入框提示里的"写一个主题"，
          混在同一个分组里会让"这个框里有几句话在说同一件事"变成一件要数一数才知道的事。
          默认收起——它是"准备材料"这一步，不是每一轮都要看的东西。

          `max-h` + `overflow-y-auto`：这份列表是**无上限**的（最多 30 份，每份还可能
          展开正文与引用明细）。不给高度上限时它一路往下长，把画布连同视口一起顶出去
          ——面板本身成了整页最高的一层，用户连对话框的提交按钮都看不到了。
        -->
        <div
          v-show="materialOpen"
          data-test="material-panel"
          class="max-h-[min(60vh,520px)] overflow-y-auto overscroll-contain border-t border-slate-100 bg-slate-50/60 px-2.5 py-2"
        >
          <div class="mb-1 flex items-center justify-between">
            <p class="text-[11px] font-medium text-slate-600">材料（可选）</p>
            <button type="button" class="text-[11px] text-slate-500 hover:text-slate-900" @click="closeMaterial">收起</button>
          </div>
          <SourceInputs />
          <!-- 「+」弹出的那个文件选择框。全页只有这一个 `input[type=file]` 的材料入口：
               再放一个（比如 import）会让"选文件"的定位二义。 -->
          <input ref="fileInput" type="file" multiple class="hidden" aria-hidden="true" tabindex="-1" @change="onPickMaterials" />
        </div>
      </div>
    </div>
  </div>
</template>
