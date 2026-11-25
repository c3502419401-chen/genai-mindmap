import { reactive } from 'vue';

import type { Size } from './overlay-layout';

/**
 * 两个浮层"在不在、多大"，工具条与详情卡交换这四件事。
 *
 * 它**不存位置**：位置只由 `overlay-layout.ts::planOverlays` 算，两个组件各自拿同一份
 * 输入去算，得到同一个答案。这里只放"对方在哪、占多大"——少了这一份，工具条就没法
 * 躲开一张它看不见的卡片（那正是"工具条压住 detail"这个 bug 的成因）。
 *
 * 为什么不做成 store 的字段：它们是纯界面的瞬态（鼠标在哪、选中谁），与
 * `content` / 撤销栈没有关系；放进 `stores/mindmap` 会让每次悬停都碰一次数据层。
 */
export const overlayState = reactive({
  /** 选中的节点（工具条跟着它）。 */
  toolbarId: null as string | null,
  /** 工具条自己的尺寸：量出来才知道要躲多远。 */
  toolbarSize: { width: 0, height: 0 } as Size,
  /** 正在悬停的节点（详情卡跟着它）。 */
  detailId: null as string | null,
  /** 详情卡的尺寸：宽固定（`DETAIL_WIDTH`），高随正文。 */
  detailSize: { width: 288, height: 0 } as Size,
  /**
   * 就地意图输入框自己的尺寸（`canvas/NodeIntentBox.vue` 量出来之后填）。
   *
   * 它自己也要躲别人，所以它必须先知道自己多大；而它**不广播**自己在哪儿——
   * 工具条与详情卡都不躲它（顺序见 `planOverlays`：意图框是最后摆的那个）。
   */
  intentSize: { width: 240, height: 0 } as Size,
});
