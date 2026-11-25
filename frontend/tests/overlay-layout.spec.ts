import { describe, expect, it } from 'vitest';

import {
  DETAIL_WIDTH,
  OVERLAY_GAP,
  OVERLAY_MARGIN,
  overlapArea,
  planOverlays,
  rectOf,
  type Rect,
  type Spot,
} from '../src/canvas/overlay-layout';

/**
 * 工具条与详情卡的**统一避让**。
 *
 * 这两个浮层原先各摆各的：工具条贴节点上方，详情卡也从节点上方弹。实测点开
 * 「明确目标分数」时工具条正压在那段正文上；贴到画布上沿时它又被 clamp 回节点里。
 *
 * 现在位置只在这里算一次：先摆详情卡（被动的内容块），再摆工具条，而且工具条的
 * 候选顺序是**侧面 → 下方 → 上方**，并且要躲开详情卡已经占下的矩形。
 * 这一组用例全是算术，没有 DOM——DOM 那一侧由 `node-toolbar.spec` / `node-detail.spec` 钉。
 */

const AREA: Rect = { left: 0, top: 60, right: 1200, bottom: 800 };
const BAR = { width: 150, height: 36 };
const CARD = { width: DETAIL_WIDTH, height: 120 };
const NODE: Rect = { left: 500, top: 300, right: 680, bottom: 348 };

function rects(plan: { toolbar: Spot | null; detail: Spot | null }) {
  return {
    toolbar: plan.toolbar ? rectOf(plan.toolbar, BAR) : null,
    detail: plan.detail ? rectOf(plan.detail, CARD) : null,
  };
}

describe('两个浮层一起摆', () => {
  it('同一个节点：详情卡在上方、工具条在右侧，两块零重叠', () => {
    const plan = planOverlays({
      area: AREA,
      toolbar: { anchor: NODE, size: BAR },
      detail: { anchor: NODE, size: CARD },
    });
    expect(plan.detail?.placement).toBe('above');
    expect(plan.toolbar?.placement).toBe('right');

    const { toolbar, detail } = rects(plan);
    expect(overlapArea(toolbar!, detail!)).toBe(0);
    expect(overlapArea(toolbar!, NODE)).toBe(0);
    expect(overlapArea(detail!, NODE)).toBe(0);
    // 详情卡下边缘离节点上边正好一个缝。
    expect(detail!.bottom).toBe(NODE.top - OVERLAY_GAP);
    // 工具条左边缘离节点右边正好一个缝。
    expect(toolbar!.left).toBe(NODE.right + OVERLAY_GAP);
  });

  it('节点贴在画布上沿：工具条不再被夹回节点里，也仍然不压详情卡', () => {
    // 上边 60 起，节点顶在 62：上方放不下工具条（原来那版就是这样压到节点上的）。
    const node: Rect = { left: 500, top: 62, right: 680, bottom: 110 };
    const plan = planOverlays({
      area: AREA,
      toolbar: { anchor: node, size: BAR },
      detail: { anchor: node, size: CARD },
    });
    expect(plan.toolbar?.placement).toBe('right');
    const { toolbar, detail } = rects(plan);
    expect(overlapArea(toolbar!, node)).toBe(0);
    expect(overlapArea(toolbar!, detail!)).toBe(0);
    expect(toolbar!.top).toBeGreaterThanOrEqual(AREA.top + OVERLAY_MARGIN);
    // 详情卡只能往下翻（上方没有任何空间），仍然不压节点。
    expect(plan.detail?.placement).toBe('below');
    expect(overlapArea(detail!, node)).toBe(0);
  });

  it('右侧没地方：工具条让到下方或左侧，仍旧躲开详情卡', () => {
    const node: Rect = { left: 1000, top: 300, right: 1180, bottom: 348 };
    const plan = planOverlays({
      area: AREA,
      toolbar: { anchor: node, size: BAR },
      detail: { anchor: node, size: CARD },
    });
    // 右侧越界（1180 + 8 + 150 > 1192）；左侧那块又被详情卡占着（它自己也翻到了左边），
    // 于是落到下方——顺序是侧面 → 下方 → 上方，谁先放得下就是谁。
    expect(plan.toolbar?.placement).not.toBe('right');
    const { toolbar, detail } = rects(plan);
    expect(overlapArea(toolbar!, detail!)).toBe(0);
    expect(overlapArea(toolbar!, node)).toBe(0);
  });

  it('节点在右上角、四面都挤：工具条退到画布里，且挑重叠最少的位置', () => {
    const node: Rect = { left: 1020, top: 62, right: 1200, bottom: 110 };
    const plan = planOverlays({
      area: AREA,
      toolbar: { anchor: node, size: BAR },
      detail: { anchor: node, size: CARD },
    });
    const { toolbar } = rects(plan);
    expect(toolbar!.left).toBeGreaterThanOrEqual(AREA.left + OVERLAY_MARGIN);
    expect(toolbar!.right).toBeLessThanOrEqual(AREA.right - OVERLAY_MARGIN);
    expect(toolbar!.top).toBeGreaterThanOrEqual(AREA.top + OVERLAY_MARGIN);
    expect(toolbar!.bottom).toBeLessThanOrEqual(AREA.bottom - OVERLAY_MARGIN);
  });

  it('没有详情卡时，工具条只在节点附近（旧行为那条路仍然成立）', () => {
    const plan = planOverlays({
      area: AREA,
      toolbar: { anchor: NODE, size: BAR },
      detail: null,
    });
    expect(plan.detail).toBeNull();
    expect(plan.toolbar?.placement).toBe('right');
  });

  it('没有任何浮层时三块都是 null', () => {
    // 第三块是就地意图输入框（`canvas/NodeIntentBox.vue`）：它排在最后摆位，
    // 不传就是"这次没有它"，与另外两块同一个语义。
    expect(planOverlays({ area: AREA, toolbar: null, detail: null }))
      .toEqual({ toolbar: null, detail: null, intent: null });
  });
});

/**
 * 第三个浮层：点节点弹出来的就地输入框。
 *
 * 它的位置诉求与另外两块不同，所以候选顺序也不同（下方优先）：
 *  · 详情卡的诉求是"贴着节点、别挡视线"→ 上方优先；
 *  · 工具条可交互、宁可挪远 → 侧面优先；
 *  · 意图框是**用户刚点出来要用的东西**，而且它是"接着这个节点往下写一行字"的
 *    那个位置 → 下方优先，被挪开也必须完整可见（它要接键盘）。
 */
describe('就地输入框：下方优先，且躲开已经占位的两样', () => {
  const BOX = { width: 240, height: 72 };

  it('只有它一个时，贴在节点正下方', () => {
    const plan = planOverlays({ area: AREA, toolbar: null, detail: null, intent: { anchor: NODE, size: BOX } });
    expect(plan.intent?.placement).toBe('below');
    expect(plan.intent?.y).toBe(NODE.bottom + OVERLAY_GAP);
  });

  it('工具条已经占了右侧、详情卡占了上方：三者零重叠', () => {
    const plan = planOverlays({
      area: AREA,
      toolbar: { anchor: NODE, size: BAR },
      detail: { anchor: NODE, size: CARD },
      intent: { anchor: NODE, size: BOX },
    });
    expect(plan.intent).toBeTruthy();
    const box = rectOf(plan.intent!, BOX);
    expect(overlapArea(box, rectOf(plan.toolbar!, BAR))).toBe(0);
    expect(overlapArea(box, rectOf(plan.detail!, CARD))).toBe(0);
    expect(overlapArea(box, NODE)).toBe(0);
  });

  it('节点贴着画布下沿时翻到上方，不飘到画布外', () => {
    const LOW: Rect = { left: 500, top: 760, right: 680, bottom: 800 };
    const plan = planOverlays({ area: AREA, toolbar: null, detail: null, intent: { anchor: LOW, size: BOX } });
    expect(plan.intent).toBeTruthy();
    const box = rectOf(plan.intent!, BOX);
    expect(box.bottom).toBeLessThanOrEqual(AREA.bottom - OVERLAY_MARGIN);
    expect(box.top).toBeGreaterThanOrEqual(AREA.top + OVERLAY_MARGIN);
  });
});

describe('浮层尺寸跟着内容走', () => {
  it('详情卡越高，它越可能翻到下方（不是简单居中）', () => {
    // 节点离上沿只有 100px，卡片 300 高 → 上方放不下。
    const node: Rect = { left: 500, top: 160, right: 680, bottom: 208 };
    const tall = planOverlays({
      area: AREA,
      toolbar: null,
      detail: { anchor: node, size: { width: DETAIL_WIDTH, height: 300 } },
    });
    expect(tall.detail?.placement).toBe('below');
  });
});
