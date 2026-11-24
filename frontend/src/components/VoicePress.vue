<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue';

import { RecordingError, startRecording, type ActiveRecording } from '../audio/recorder';

/**
 * 「按住说话」：按下开始录、松手结束并把它加进材料。
 *
 * 录音能力本身是**复用**的——`audio/recorder.ts` 的 `startRecording()` 吐出一个
 * 16kHz WAV 的 `File`，与"用户选了文件"完全一样，后面走同一条上传解析链路。这里
 * 只把交互从"点一下开始、再点一下停止"换成按住，因为那是聊天框里人人都懂的手势。
 *
 * 三点必须做到，否则这个按钮会变成"看起来能按、按了没反应"：
 *   1. **指针移出按钮再松手也要停**：`pointerup` 挂在 window 上，不挂在按钮上；
 *   2. **键盘也能按住**：Space/Enter 的 keydown 开始、keyup 结束（读屏用户没有指针）；
 *   3. **组件卸载时取消录音**：否则麦克风指示灯一直亮着。
 *
 * 还有第四点，是实测出来的：**权限还没回来就松手**。`startRecording()` 会等
 * `getUserMedia` 的授权框，那一步可能几百毫秒到几秒。这段时间里用户松手，旧写法
 * 的 `finish()` 看到 `recording === null` 直接 return，等授权回来之后组件照常进入
 * `recording` 态——**用户明明松手了，麦克风还在录**，而且只有再按一次才能停。
 *
 * 硬上限 600 秒并非自创：`RecordButton` 与后端 `MAX_AUDIO_BYTES`（20MB）同一条口径，
 * 到这个长度自动停止，免得录完才发现传不上去。
 */
const props = defineProps<{ disabled?: boolean }>();

const emit = defineEmits<{
  recorded: [file: File];
  failed: [message: string];
  active: [value: boolean];
}>();

const HINT_SECONDS = 60;
const HARD_LIMIT_SECONDS = 600;

const state = ref<'idle' | 'starting' | 'recording' | 'finishing'>('idle');
const seconds = ref(0);
const error = ref('');

let recording: ActiveRecording | null = null;
let ticker: number | undefined;
let disposed = false;
/**
 * 用户此刻是否还按着（指针或键盘）。
 *
 * 它解决的正是"授权期间松手"：`begin()` 拿到麦克风之后要再看一眼这个值，
 * 为假就取消，而不是进入录音态。
 */
let pressed = false;
/**
 * 上一次通过 `active` 报给外面的值。去重之后，卸载时无条件调一次 `setActive(false)`
 * 就不会多出一串无意义的帧（`sources.recording` 是生成按钮的闸门）。
 */
let announced = false;

function setActive(value: boolean): void {
  if (announced === value) return;
  announced = value;
  emit('active', value);
}

function format(total: number): string {
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
}

function stopTicker(): void {
  if (ticker !== undefined) {
    window.clearInterval(ticker);
    ticker = undefined;
  }
}

function detach(): void {
  window.removeEventListener('pointerup', onPointerUp);
  window.removeEventListener('pointercancel', onPointerUp);
  window.removeEventListener('keyup', onKeyUp);
}

async function finish(): Promise<void> {
  // 先摘监听器：这一次"松手"已经发生，后面不该再有第二次回调。
  detach();
  // 授权还没回来（`recording` 仍是 null）：不做任何事，交给 `begin()` 的
  // `pressed` 判断收尾。以前这里直接 return，于是松手被静默丢弃。
  if (!recording) return;
  stopTicker();
  state.value = 'finishing';
  const current = recording;
  recording = null;
  try {
    const file = await current.stop();
    state.value = 'idle';
    seconds.value = 0;
    emit('recorded', file);
  } catch (err) {
    state.value = 'idle';
    error.value = err instanceof RecordingError ? err.message : '录音处理失败';
    emit('failed', error.value);
  } finally {
    setActive(false);
  }
}

async function begin(): Promise<void> {
  if (props.disabled || state.value !== 'idle') return;
  error.value = '';
  state.value = 'starting';
  setActive(true);
  try {
    const started = await startRecording();
    // 授权期间松手了（或者组件已经被卸载）：**不进入录音态**，直接丢弃这一段。
    // 这是"按住说话"最容易错的一处：松手那一刻麦克风还没开，等到它开的时候
    // 用户的手早就不在按钮上了。
    if (disposed || !pressed) {
      started.cancel();
      setActive(false);
      state.value = 'idle';
      return;
    }
    recording = started;
    state.value = 'recording';
    seconds.value = 0;
    // 500ms 轮询而不是每秒 +1：标签页切后台时 setInterval 会被节流，
    // 累加出来的时间会偏小，而 `elapsedSeconds()` 读的是真实时钟。
    ticker = window.setInterval(() => {
      seconds.value = Math.floor(recording?.elapsedSeconds() ?? 0);
      if (seconds.value >= HARD_LIMIT_SECONDS) void finish();
    }, 500);
  } catch (err) {
    state.value = 'idle';
    error.value = err instanceof RecordingError ? err.message : '录音启动失败';
    emit('failed', error.value);
    setActive(false);
  }
}

function onPointerDown(event: PointerEvent): void {
  if (props.disabled) return;
  event.preventDefault();
  pressed = true;
  // 先把监听器挂上再启动麦克风：授权那一步是异步的，中间任何一个同步异常都不该
  // 留下"按下了但没人听松手"的状态。
  window.addEventListener('pointerup', onPointerUp);
  window.addEventListener('pointercancel', onPointerUp);
  void begin();
}

function onPointerUp(): void {
  pressed = false;
  void finish();
}

function onKeyDown(event: KeyboardEvent): void {
  if (event.key !== ' ' && event.key !== 'Enter') return;
  if (event.repeat) return;
  event.preventDefault();
  pressed = true;
  window.addEventListener('keyup', onKeyUp);
  void begin();
}

function onKeyUp(event: KeyboardEvent): void {
  if (event.key !== ' ' && event.key !== 'Enter') return;
  pressed = false;
  void finish();
}

onBeforeUnmount(() => {
  disposed = true;
  pressed = false;
  detach();
  stopTicker();
  recording?.cancel();
  recording = null;
  state.value = 'idle';
  // **必须复位全局录音标记**：切回键盘输入（`v-if="voiceMode"`）会把本组件整个
  // 卸载，而 `sources.recording` 是生成按钮的闸门之一。漏掉这一句，用户切一次
  // 键盘就再也生成不了，界面上只说"正在录音"。
  setActive(false);
});
</script>

<template>
  <div class="flex flex-col gap-1">
    <button
      type="button"
      data-test="press-to-talk"
      class="select-none rounded-lg border px-3 py-3 text-sm transition"
      :class="state === 'recording'
        ? 'border-rose-300 bg-rose-50 text-rose-700'
        : 'border-slate-300 text-slate-600 hover:border-slate-400 hover:bg-slate-50'"
      :disabled="props.disabled"
      :aria-busy="state === 'starting' || state === 'finishing'"
      :aria-pressed="state === 'recording'"
      @pointerdown="onPointerDown"
      @keydown="onKeyDown"
      @contextmenu.prevent
    >
      <template v-if="state === 'starting'">正在启动麦克风…</template>
      <template v-else-if="state === 'finishing'">正在整理音频…</template>
      <template v-else-if="state === 'recording'">松开发送 · {{ format(seconds) }}</template>
      <template v-else>按住说话</template>
    </button>
    <p v-if="state === 'recording' && seconds >= HINT_SECONDS" class="text-[11px] text-amber-700">
      已超过 60 秒，将分段识别，转写会慢一些
    </p>
    <p v-if="state === 'recording'" class="text-[11px] text-slate-400">
      录音在本地进行，松手后才上传转写。
    </p>
    <p v-if="error" class="text-[11px] leading-relaxed text-rose-600" role="alert">{{ error }}</p>
  </div>
</template>
