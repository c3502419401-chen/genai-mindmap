import type { Direction } from './tidy';
import type { DropPointer, DropRect, DropSide } from './drop-zone';
import { dropHitOf, dropZoneOf, nearestHitOf, outwardOf } from './drop-zone';
import type { InsertIntent } from '../../mindmap/drop-plan';

/**
 * 「双击空白处加节点」这一步的几何：指针落哪儿 → 挂到谁下面、算哪种关系。
 *
 * 单独一个文件而不是写在 `MindMapCanvas.vue` 里，理由与 `drop-zone.ts` 自己那一条
 * 相同：**它是纯几何，可以被逐条钉住**。写进组件的后果是这一层只能靠"挂载整个 App、
 * 在 jsdom 里伪造视口、再双击一个坐标"来验证——而 jsdom 里没有布局，那种用例测的
 * 是"桩子对不对"而不是"判据对不对"。
 *
 * 判据分两档，**顺序不能换**：
 *
 *  1. **落在某处命中区里**（节点盒子外扩 `DROP_PAD_X/Y` 的那一圈）：按方位翻译，与
 *     拖动共用同一张表（`dropZoneOf`）。于是"在节点右边双击"与"往节点右边拖"得到
 *     同一个结果——用户不必记两套规则。
 *  2. **一个命中区都没进**（真正的空白）：取**归一化距离最近**的那个节点，固定算
 *     `child`。这一档刻意不读方位：空白可以落在某个节点的任何一侧很远处，按方位会
 *     读出"我要当它的父"这种没人期待的结果。
 *
 * 为什么"最近的"用 `nearestHitOf` 而不是欧氏距离：那个函数与命中区共用同一套归一化
 * 度量（除以半宽 + 外扩），两个轴在"几个盒子的尺度"上同权。盒子是 180×48，纯像素
 * 距离会让水平方向重近四倍——右上角双击会挂到右边很远的节点下面，而不是正上方那个。
 */
export interface InsertBox {
  id: string;
  rect: DropRect;
}

export interface InsertContext {
  /** 树布局朝向（`dropZoneOf` 要用）。 */
  direction: Direction;
  /**
   * 中心主题的 id。给 `both` 朝向判"目标在根的哪一侧"用；其余朝向不读它。
   *
   * 可不给（画布在最外层算不出来时），代价只是 `both` 上左右两档都算成"子"——
   * 与 `store.direction !== 'both'` 那条早退是同一个退化，不会判错以外的东西。
   */
  rootId?: string | null;
}

function centerOf(rect: DropRect): { x: number; y: number } {
  return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
}

export function insertIntentAt(
  pointer: DropPointer,
  boxes: readonly InsertBox[],
  context: InsertContext,
): InsertIntent | null {
  if (boxes.length === 0) return null;

  let best: { box: InsertBox; side: DropSide; nearness: number } | null = null;
  for (const box of boxes) {
    const hit = dropHitOf(pointer, box.rect);
    if (!hit) continue;
    if (!best || hit.nearness < best.nearness) {
      best = { box, side: hit.side, nearness: hit.nearness };
    }
  }

  if (best) {
    const rootBox = context.rootId ? boxes.find(box => box.id === context.rootId) : undefined;
    // 与画布那边的 `outwardFor` 同一件事：只有 `both` 读它，其余朝向给 0 就等于没有。
    const outward = rootBox
      ? outwardOf(centerOf(best.box.rect).x, centerOf(rootBox.rect).x)
      : 0;
    return {
      targetId: best.box.id,
      zone: dropZoneOf(best.side, { direction: context.direction, outward }),
    };
  }

  const near = nearestHitOf(pointer, boxes);
  if (!near) return null;
  return { targetId: near.id, zone: 'child' };
}
