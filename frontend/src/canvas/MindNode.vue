<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core';
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';

import type { ChildSide } from '../canvas/layout/tidy';
import { NODE_SIZE } from '../canvas/layout/tidy';
import {
  DETAIL_WIDTH,
  canvasArea,
  nodeRect,
  planOverlays,
  sizeOf,
} from '../canvas/overlay-layout';
import { overlayState } from '../canvas/overlay-state';
import type { MindNodeData } from '../stores/mindmap';

const props = defineProps<{
  id: string;
  data: MindNodeData;
  selected?: boolean;
  editing?: boolean;
}>();

const emit = defineEmits<{
  commit: [label: string];
  cancel: [];
  /** Tab：提交当前文字，并请外面在同一个父节点下再加一个子节点。 */
  next: [label: string];
  /**
   * 视图层收起：这个节点在画布上只露出自己，点一下把它的子节点放出来。
   *
   * 与 `next` / `commit` 分开是一件事两半：那两个**改内容**（进撤销栈、置脏），
   * 这一个是**改视图**（什么都不算）。合成一个 emit，外面就必须靠参数猜意图。
   */
  unfold: [];
}>();

const POSITIONS: Record<ChildSide, Position> = {
  left: Position.Left,
  right: Position.Right,
  top: Position.Top,
  bottom: Position.Bottom,
};

/** 文字被截断时才有必要浮现卡片；没截断也照样允许悬停查看（标题本身可能就是全文）。 */
const truncated = computed(() => props.data.label !== props.data.fullLabel);

/**
 * 卡片里是否还有节点上看不到的内容。
 *
 * 两种来源：标题被截断（旧图的长标题），或正文（`detail`）与标题不是同一段文字。
 * 生成侧的标题现在 ≤16 字，画布上不会被截断——那时这张卡片的全部价值就在于
 * "点开看长文"，所以 `truncated` 这一条不能单独当作提示的判据。
 */
const hasMore = computed(
  () => truncated.value || props.data.body !== props.data.fullLabel,
);

/**
 * 节点框的样式。
 *
 * 「档次」（tone）在这里算，几何（矩形 / 胶囊 / 菱形）在模板里分开：
 * 同一批颜色要服务三种形状，写两套只会有一处不一致。
 *
 * 三条真实分支原样保留——`content` 渲染出来必须和加预览之前逐像素一致。
 * 还在长的那棵树走一组草稿样式：
 * - 预览：虚线 + 更淡，一眼就是还在长；
 * - 预览里模型明确标了「不用再展开」的节点再淡一档。`null`（还没写到）不标记，
 *   否则「明确不展开」和「还没决定」在画面上会变成同一件事。
 */
type BoxTone = 'preview-leaf' | 'preview' | 'changed' | 'selected' | 'root' | 'default';

/**
 * 档次的**类名表**。矩形与胶囊直接用这一张；菱形把同一组决定喂给 CSS 变量
 * （`styles.css` 的 `.mind-diamond[data-diamond-tone]`），因为 `clip-path` 会把
 * `border` / `ring` / `box-shadow` 一起裁掉，那三样在菱形上一个都留不下。
 *
 * 类的顺序与取值与这个字段出现之前**逐字相同**：树与鱼骨画出来不能有任何变化。
 */
const TONE_CLASS: Record<BoxTone, string> = {
  'preview-leaf': 'border-dashed border-slate-200 bg-white text-slate-400 opacity-40',
  preview: 'border-dashed border-slate-300 bg-white text-slate-600 opacity-60',
  // 上次生成改动过的节点描一圈琥珀色：生成是**整棵树直接进画布**的，用户一睁眼
  // 画布就变了，不给标记的话他只能自己在整棵树里找哪里不同。新增的节点在这里
  // **标不出来**（它已经在 content 里、有位置），那部分差异由面板的改动清单负责。
  changed: 'border-amber-500 bg-white text-slate-700 ring-2 ring-amber-300',
  selected: 'border-slate-900 bg-white font-medium shadow-md',
  root: 'border-slate-900 bg-slate-900 font-medium text-white',
  default: 'border-slate-300 bg-white text-slate-700 hover:border-slate-400',
};

const tone = computed<BoxTone>(() => {
  if (props.data.draft === 'preview') {
    return props.data.previewLeaf ? 'preview-leaf' : 'preview';
  }
  if (props.data.changed) return 'changed';
  if (props.selected) return 'selected';
  if (props.data.depth === 0) return 'root';
  return 'default';
});

/**
 * 形状。`undefined` / `rect` 就是加这个字段之前那条矩形路径（`stores/mindmap`
 * 只在流程图里发 `shape`，树与鱼骨永远是 `undefined`）。
 */
const shape = computed(() => props.data.shape ?? 'rect');

/** 外接框：缺省与布局共读同一份常量，所以缺字段时不会画偏。 */
const size = computed(() => props.data.size ?? NODE_SIZE);

/**
 * 内联尺寸只给**非矩形**加。矩形的 180×48 一直由 `h-12 w-[180px]` 这两个类给，
 * 改成内联 style 会让树与鱼骨的节点 DOM 多出一个 `style` 属性——虽然像素一样，
 * 但"树一个像素都不能变"这条约束的证据应该是 diff 里看得出来，而不是推理出来。
 */
const boxStyle = computed(() => (
  shape.value === 'rect'
    ? {}
    : { width: `${size.value.width}px`, height: `${size.value.height}px` }
));

/**
 * 起点 / 终点：**胶囊**，不是菱形。
 *
 * 两者都是端点，不是分叉点——画成菱形会让读图的人以为"开始"也要选一条路走。
 * 再加一层对比：`start` 是实心深底（和根节点同一个语言：**入口是实的**），
 * `end` 是白底 + 双倍描边（**出口是收住的**）。区分靠这两条，不靠往文字里塞符号：
 * 符号会进 `title`、进读屏、进导出文本，为了一个纯视觉的差别污染三个地方不值得。
 *
 * 注意这里是**替换** `TONE_CLASS.default`，不是叠加：两套 `bg-*` / `border-*` 同时
 * 挂在一个元素上时，谁赢取决于 Tailwind 样式表里的顺序，而不是 `:class` 数组的顺序
 * ——叠加的写法当场就出过一次"白底白字，起点标题看不见"，截图里才看得出来。
 */
function pillToneClass(role: MindNodeData['role']): string {
  return role === 'start'
    ? 'border-slate-900 bg-slate-900 font-medium text-white'
    : 'border-2 border-slate-900 bg-white font-medium text-slate-800';
}

/** 几何 + 档次。矩形那两个类（`h-12 w-[180px]`）一个字都没动。 */
const cellClass = computed(() => {
  if (shape.value === 'diamond') {
    // 菱形不挂 border / 圆角 / 底色：描边与填充由三层 `clip-path` 画（见 styles.css）。
    // `font-medium` 仍然要保留——它是文字重量，不是框的画法。
    return ['mind-diamond flex items-center justify-center', tone.value === 'selected' || tone.value === 'root' ? 'font-medium' : ''];
  }
  if (shape.value === 'parallelogram' || shape.value === 'cylinder') {
    // 新两种形状同理不挂 `TONE_CLASS`：平行四边形被 `clip-path` 裁掉 border/ring，
    // 圆柱的框是 SVG 的 stroke——挂上去两边都会打架（白底与描边各画一套）。
    // 档次改由 `data-shape-tone` 交给 styles.css（与菱形 `data-diamond-tone` 同一个思路）。
    return [
      shape.value === 'parallelogram' ? 'mind-parallelogram' : 'mind-cylinder',
      'flex items-center justify-center text-center text-sm leading-tight',
      tone.value === 'selected' || tone.value === 'root' ? 'font-medium' : '',
    ];
  }
  const geometry = shape.value === 'pill' ? 'rounded-full px-4' : 'h-12 w-[180px] rounded-md px-3';
  const toneClass = shape.value === 'pill' && tone.value === 'default'
    ? pillToneClass(props.data.role)
    : TONE_CLASS[tone.value];
  return ['flex items-center justify-center border text-center text-sm leading-tight', geometry, toneClass];
});

/**
 * 圆柱的轮廓路径（`viewBox="0 0 100 100"`，随外接框非均匀拉伸）。
 *
 * 为什么是 SVG 而不是 `border-radius: 50% / 12%`：那条 CSS 技巧能画出"上下都是弧、
 * 两侧竖直"的**轮廓**，但画不出圆柱那件小事——**顶面椭圆的下半弧**。没有那道弧，
 * 看着就是一个压扁的药丸，读者不会把它读成"存数据的那个框"。用纯 CSS 补那道弧
 * 要再叠一个绝对定位的椭圆元素，它的曲率与轮廓不是同一条（描边加粗时走形），
 * 而 SVG 里两条弧就是两条 path，共用同一个 `ry`，永远不会错开。
 *
 * 椭圆半高 `e = 10`（viewBox 单位）：实际是 `68 × 10% = 6.8px`，椭圆总高 13.6px。
 * 那个数不是随手取的，见 `layout/flow.ts::DATABASE_SIZE` 里的可用高度算术。
 * 弧用三次贝塞尔近似（`k = 0.5523`），不用 `A` 命令：`A` 在非均匀缩放下各浏览器
 * 对椭圆变换的处理有出入，贝塞尔的两个控制点是死数，扩到任何尺寸都一样。
 */
const CYL_E = 10;
const CYL_K = 0.5523;
const cylRy = CYL_E;
/** 四分之一弧的控制点水平偏移：`k · rx`，`rx = 50`。 */
const cylCx = CYL_K * 50;
const CYL_BODY = [
  `M 0,${cylRy}`,
  // 顶面上弧：左点 → 顶点 → 右点
  `C 0,${cylRy - CYL_K * cylRy} ${50 - cylCx},0 50,0`,
  `C ${50 + cylCx},0 100,${cylRy - CYL_K * cylRy} 100,${cylRy}`,
  // 右侧竖直边 + 底面下弧
  `L 100,${100 - cylRy}`,
  `C 100,${100 - cylRy + CYL_K * cylRy} ${50 + cylCx},100 50,100`,
  `C ${50 - cylCx},100 0,${100 - cylRy + CYL_K * cylRy} 0,${100 - cylRy}`,
  'Z',
].join(' ');
/** 顶面椭圆的下半弧：圆柱的"盖沿"，缺了它只是个药丸。 */
const CYL_LID = [
  `M 0,${cylRy}`,
  `C 0,${cylRy + CYL_K * cylRy} ${50 - cylCx},${2 * cylRy} 50,${2 * cylRy}`,
  `C ${50 + cylCx},${2 * cylRy} 100,${cylRy + CYL_K * cylRy} 100,${cylRy}`,
].join(' ');

/**
 * 语义色：键挂在 `data-node-color` 上，**色值交给 CSS**（`styles.css` 里的
 * `--mind-color-*`）。
 *
 * 不在这里拼 `style="border-color:#2563eb"` 是有代价的：一旦写进节点样式，暗色
 * 主题下就是一堆刺眼的亮蓝，而那是已经存进库里的数据，改不回来。挂键之后，
 * 换主题只改 CSS，导图一个字都不用动。
 *
 * 根节点的底色是深色实底（`TONE_CLASS.root` 里的 `bg-slate-900`），CSS 那边只给它染
 * 左边一道；其余节点染边框 + 淡底。
 */
const colorAttrs = computed(() => {
  const color = props.data.color;
  if (!color) return {};
  return { 'data-node-color': color, 'data-node-depth': String(props.data.depth) };
});

const card = ref(false);
let showTimer: ReturnType<typeof setTimeout> | undefined;
let hideTimer: ReturnType<typeof setTimeout> | undefined;

/**
 * 悬停卡片的位置（视口坐标）。
 *
 * **卡片不在画布那层 DOM 里**：它被 `Teleport` 到 `body` 上，用 `position: fixed`
 * 摆位。原因只有一个，但足够致命——画布是靠一句 `transform: scale(...)` 缩放的，
 * 卡片如果还挂在节点内部，它就会跟着一起缩：默认档 `scale = 0.857` 时 12px 的正文
 * 还能读，切到「全图」（0.364）就是 5px，点开一张"详情"看到的是一片灰。
 *
 * 悬停卡片是**读长文**的地方，它的字号不该由"图有多挤"决定。
 *
 * **位置不由这里决定**：和工具条一起在 `overlay-layout.ts::planOverlays` 里摆。
 * 原先两者都贴节点上方，于是"选中一个节点"再"悬停它"时，工具条正好压在正文上。
 */
const cardAt = ref<{ x: number; y: number } | null>(null);
const rootEl = ref<HTMLElement | null>(null);
const cardRef = ref<HTMLElement | null>(null);

function placeCard(): void {
  const node = rootEl.value?.getBoundingClientRect();
  if (!node) return;
  const box = cardRef.value?.getBoundingClientRect();
  // 卡片自己多大要记进共享状态：工具条拿它避让（"一处算"的另一半输入）。
  if (box) overlayState.detailSize = { width: box.width, height: box.height };

  const toolbarNode = overlayState.toolbarId ? nodeRect(overlayState.toolbarId) : null;
  const plan = planOverlays({
    area: canvasArea(),
    detail: {
      anchor: { left: node.left, top: node.top, right: node.right, bottom: node.bottom },
      size: box ? sizeOf(box) : { width: DETAIL_WIDTH, height: 0 },
    },
    toolbar: toolbarNode ? { anchor: toolbarNode, size: overlayState.toolbarSize } : null,
  });
  if (plan.detail) cardAt.value = { x: plan.detail.x, y: plan.detail.y };
}

function scheduleCard() {
  clearTimeout(showTimer);
  clearTimeout(hideTimer);
  // 200ms 延迟：鼠标扫过画布时不该到处弹卡片。
  showTimer = setTimeout(() => {
    overlayState.detailId = props.id;
    card.value = true;
    // 先量一次（卡片的宽高要等它挂上去才知道），下一帧再按真实尺寸摆正。
    void nextTick(() => {
      placeCard();
      requestAnimationFrame(placeCard);
    });
  }, 200);
}

/**
 * 离开节点之后**晚一点**再收卡片。
 *
 * 卡片是 `Teleport` 出去的独立一层，与节点之间隔着 `OVERLAY_GAP` 那么一条缝：
 * 立刻收的话，鼠标朝卡片移过去的第一步就已经把卡片关掉了——而卡片里那段长正文
 * 是 `max-h-56 overflow-y-auto`，滚不动等于白给。这 120ms 就是穿过那条缝的时间，
 * 鼠标真的进了卡片（`@mouseenter="scheduleCard"`）就把这次收起取消。
 */
function hideCardSoon() {
  clearTimeout(hideTimer);
  hideTimer = setTimeout(hideCard, 120);
}

function hideCard() {
  clearTimeout(showTimer);
  clearTimeout(hideTimer);
  card.value = false;
  cardAt.value = null;
  if (overlayState.detailId === props.id) overlayState.detailId = null;
}

const draft = ref('');
const input = ref<HTMLInputElement | null>(null);
/** 防止"回车提交"之后紧跟的 blur 再提交一次（第二次会带着旧值覆盖结果）。 */
let settled = false;

/**
 * 用户继续打字就把锁解开。
 *
 * 提交不一定成功：store 那边会拒绝空标题和超长标题，那时编辑态仍然开着。
 * 如果锁一直不放，用户改成合法文字后按回车、Tab、Esc 都毫无反应，看起来
 * 像编辑器坏了。input 事件说明内容又变了，自然应该允许再提交一次。
 */
function onDraftInput() {
  settled = false;
}

/**
 * 新加的节点是刚插进画布的，下一帧才真正挂到 document 上；对还没挂上的元素调 focus()
 * 是空操作，用户就得再点一下才能打字。所以每帧试一次，直到焦点确实落进来。
 */
async function focusInput() {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await nextTick();
    const el = input.value;
    if (el && el.isConnected) {
      el.focus();
      el.select();
      if (document.activeElement === el) return;
    }
    await new Promise(resolve => requestAnimationFrame(() => resolve(null)));
  }
}

watch(
  () => props.editing,
  async editing => {
    if (!editing) {
      settled = false;
      return;
    }
    draft.value = props.data.fullLabel;
    settled = false;
    await focusInput();
  },
  { immediate: true },
);

function finish(commit: boolean) {
  if (settled) return;
  settled = true;
  if (commit) emit('commit', draft.value);
  else emit('cancel');
}

/** 和回车不同：Tab 提交完后还要接着加下一个兄弟，不能只把编辑框关掉。 */
function finishAndNext() {
  if (settled) return;
  settled = true;
  emit('next', draft.value);
}

onBeforeUnmount(() => {
  clearTimeout(showTimer);
  clearTimeout(hideTimer);
  if (overlayState.detailId === props.id) overlayState.detailId = null;
});
</script>

<template>
  <div
    ref="rootEl"
    class="relative"
    @mouseenter="scheduleCard"
    @mouseleave="hideCardSoon"
    @focusin="scheduleCard"
    @focusout="hideCard"
  >
    <Handle
      v-for="side in data.in"
      :id="`in-${side}`"
      :key="`in-${side}`"
      type="target"
      :position="POSITIONS[side]"
      class="!h-1.5 !w-1.5 !border-0 !bg-slate-300"
    />
    <Handle
      v-for="side in data.out"
      :id="`out-${side}`"
      :key="`out-${side}`"
      type="source"
      :position="POSITIONS[side]"
      class="!h-1.5 !w-1.5 !border-0 !bg-slate-300"
    />

    <div
      :class="cellClass"
      :style="boxStyle"
      :title="data.fullLabel"
      :data-node-color="colorAttrs['data-node-color']"
      :data-node-depth="colorAttrs['data-node-depth']"
      :data-node-shape="shape"
      :data-diamond-tone="shape === 'diamond' ? tone : undefined"
      :data-shape-tone="shape === 'parallelogram' || shape === 'cylinder' ? tone : undefined"
      tabindex="0"
    >
      <!--
        菱形只画在**背景层**：三层同形的 `clip-path` 叠出描边与填充（外环光晕、
        描边、填充），文字层完全独立、不裁切。旋转 45° 那条路要付的代价（标题、
        图标、引用角标、悬停卡片、内联输入框全部反向旋转）在这里一条都不存在。
        为什么不用 `border`：`clip-path` 会把边框、圆角、`ring`、`box-shadow` 一起
        裁掉——"画了描边却看不见"是最难查的一种。
      -->
      <template v-if="shape === 'diamond'">
        <span class="diamond-layer diamond-glow" aria-hidden="true" />
        <span class="diamond-layer diamond-stroke" aria-hidden="true" />
        <span class="diamond-layer diamond-fill" aria-hidden="true" />
      </template>

      <!--
        平行四边形与菱形**同一套三层结构**（原因也一样：`clip-path` 会裁掉 border /
        ring / box-shadow），差别只有那一条 `polygon` 的四个点。光晕仍然靠外扩同形状
        的一层画，所以悬停 / 改动 / 选中三态不需要各写一份结构。
      -->
      <template v-else-if="shape === 'parallelogram'">
        <span class="parallelogram-layer parallelogram-glow" aria-hidden="true" />
        <span class="parallelogram-layer parallelogram-stroke" aria-hidden="true" />
        <span class="parallelogram-layer parallelogram-fill" aria-hidden="true" />
      </template>

      <!--
        圆柱用 SVG 画：轮廓 + 顶面椭圆的下半弧。`preserveAspectRatio="none"` 让它
        跟着外接框拉伸（220×68），`vector-effect: non-scaling-stroke` 让描边宽度在
        非均匀缩放下仍然是 1.5px。描边、填充、光晕都由 CSS 变量驱动（见 styles.css
        的 `.mind-cylinder`），三态因此只改变量、不换结构。
      -->
      <svg
        v-else-if="shape === 'cylinder'"
        class="cylinder-svg"
        viewBox="0 0 100 100"
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        <path class="cylinder-body" :d="CYL_BODY" />
        <path class="cylinder-lid" :d="CYL_LID" />
      </svg>

      <input
        v-if="editing"
        ref="input"
        v-model="draft"
        class="w-full bg-transparent text-center outline-none"
        :maxlength="200"
        @keydown.enter.prevent="finish(true)"
        @keydown.esc.prevent="finish(false)"
        @keydown.tab.prevent="finishAndNext"
        @input="onDraftInput"
        @blur="finish(true)"
      />
      <span v-else class="line-clamp-2 break-words">
        <!--
          图标画在标题前面。emoji 不做成图标库的 id：零依赖、跨平台、`<span>`
          直接渲染，而图标库要为一个纯装饰字段新增一份依赖（`package.json` 里
          目前没有 `lucide-vue-next` 之类）。`aria-hidden` + 外面那层 `title`
          已经带上了完整标题，读屏读 emoji 的名字只是噪音。
        -->
        <span v-if="data.icon" class="mr-1" aria-hidden="true">{{ data.icon }}</span>{{ data.label }}
      </span>
    </div>

    <!--
      引用角标：只给"有引用"的节点画，没引用的节点保持干净。
      用 pointer-events-none 让它不挡节点点击（角标压在节点边缘上）。
    -->
    <div
      v-if="(data.refs ?? 0) > 0"
      class="pointer-events-none absolute -right-1.5 -top-1.5 flex h-5 min-w-5 items-center justify-center rounded-full border border-slate-300 bg-white px-1 text-[10px] font-medium leading-none text-slate-600 shadow-sm"
      :title="`${data.refs ?? 0} 处来源引用`"
    >
      {{ data.refs ?? 0 }}
    </div>

    <!--
      视图层收起：「还有 N 个」。

      **常驻，不等 hover**。收起之后屏幕上可能只有 7 个节点，而这个角标是"树其实有
      136 个节点"的唯一现场证据（另一处在右上角那行「显示 7 / 136」）。做成悬停才
      淡入，用户扫一眼画布就会得出"这棵树就这么大"——那正是折叠退休时要躲开的那件事
      （`docs/FOLD-RETIRED.md` §1）。

      数字是**直接子节点数**，不是整支：点一下当场冒出这么多，点完角标就消失。写 18
      而只冒出 3 个，这个数第二次就没人信了。整支的数字在 title 与 aria-label 里。

      `@click.stop`：不要让这一下冒泡成"点空白处清选区"；`@focusin.stop` 同理，否则
      键盘聚焦到它会把悬停卡片弹出来，盖住用户正要看的节点。
    -->
    <button
      v-if="data.foldedChildren"
      type="button"
      data-test="node-folded"
      class="absolute -bottom-2 right-0 flex h-5 items-center justify-center rounded-full border border-slate-400 bg-white px-1.5 text-[10px] font-medium leading-none text-slate-700 shadow-sm hover:border-slate-900 hover:bg-slate-900 hover:text-white"
      :title="`还有 ${data.foldedChildren} 个子节点（整支 ${data.foldedSubtree ?? data.foldedChildren} 个节点）`"
      :aria-label="`展开：还有 ${data.foldedChildren} 个子节点，整支共 ${data.foldedSubtree ?? data.foldedChildren} 个节点`"
      @click.stop="emit('unfold')"
      @focusin.stop
    >+{{ data.foldedChildren }}</button>

    <!--
      悬停卡片：`Teleport` 到 `body`、`position: fixed`。
      它必须是**画布缩放之外**的一层（理由见 `cardAt` 的注释）：卡片是读长文的地方，
      字号与尺寸不跟着 `transform: scale()` 走。位置按节点的视口外接框算，
      所以选中/悬停之后画布平移、缩放，它仍然贴在节点上方（`placeCard`）。
    -->
    <Teleport to="body">
      <div
        v-if="card && !editing && cardAt"
        ref="cardRef"
        data-test="node-detail-card"
        class="fixed z-30 w-72 rounded-md border border-slate-300 bg-white p-3 text-left shadow-lg"
        :style="{ left: `${cardAt.x}px`, top: `${cardAt.y}px` }"
        @mouseenter="scheduleCard"
        @mouseleave="hideCardSoon"
      >
        <p class="max-h-56 overflow-y-auto whitespace-pre-wrap break-words text-sm text-slate-700">
          {{ data.body }}
        </p>
        <p v-if="hasMore" class="mt-2 text-xs text-slate-400">
          <span v-if="data.bodySource === 'detail'">节点上只显示标题，这里是生成时写的详情</span>
          <span v-else-if="data.bodySource === 'note'">节点上只显示标题，这里是你的备注</span>
          <span v-else>节点上只显示前 24 个字，完整内容见此处或右侧详情面板</span>
        </p>
      </div>
    </Teleport>
  </div>
</template>
