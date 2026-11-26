/**
 * 引用回原文时的**窗口**：显示哪一段、上下各留多少、还能不能再往外读。
 *
 * 为什么单独一层而不是让组件直接调 `windowOf`：窗口的三件事都需要边界，
 * 而边界错了的表现都很安静——
 *
 *   1. `pad` 之和不能覆盖全文。37973 字的材料按 600 字窗口读，第一次点到
 *      第 30000 字处就该只显示那一段，而不是把整份材料摊进 480px 的抽屉
 *      （那等于没定位，用户还得自己找）。
 *   2. 窗口已经贴到全文两端时，不该再给"再读上文/下文"——点了没反应比没有按钮更糟。
 *   3. 窗口可能被**截断的正文**卡住：正文超过后端单次上限时拿到的是前 N 字，
 *      引用落在后面就必须说清楚，不能让界面显示一段"看起来是空的空间"。
 *
 * 全部是纯函数，位置与字数都按**码点**算（与后端 `SourceRef.start/end` 同一坐标系）。
 */
import { windowOf } from './refs';

/** 默认前后各留多少字。480px 宽、13px 行高下大约一屏半，读得出一句话的语境。 */
export const CONTEXT_PAD = 600;

/** 点一次"再读上文/下文"往外扩多少。一次给太少要点很多下，太多又回到"摊开全文"。 */
export const CONTEXT_STEP = 1800;

export interface ContextSlice {
  /** 命中之前的上下文（窗口左边界被截断时带前导 `…`）。 */
  before: string;
  /** 命中本身，界面用 `<mark>` 包住它。 */
  hit: string;
  /** 命中之后的上下文。 */
  after: string;
  /** 窗口在全文里的码点区间 `[from, to)`。 */
  from: number;
  to: number;
  /** 上文还有没显示的内容。 */
  moreBefore: boolean;
  /** 下文还有没显示的内容。 */
  moreAfter: boolean;
}

/**
 * 切出窗口。
 *
 * `total` 传**原文的完整长度**而不是手里这份字符串的长度：正文可能被后端上限
 * 截断，两者不相等，这时"下面还有"必须按完整长度判——按手里的长度判会得出
 * "已经到底了"，而其实只是没载入。
 */
export function contextSlice(
  text: string,
  start: number,
  end: number,
  total: number = Array.from(text).length,
  pad: number = CONTEXT_PAD,
): ContextSlice {
  const chars = Array.from(text);
  const length = chars.length;
  const from = Math.max(0, start - pad);
  const to = Math.min(length, end + pad);
  const { before, hit, after } = windowOf(text, start, end, pad);
  return {
    before,
    hit,
    after,
    from,
    to,
    // 贴住 0 才算"没有上文"。下文按**完整长度**判：正文可能只载入了一部分，
    // 那时"下面还有"是事实，只是载不进来——那句话由 `beyondLoaded` 的调用方说。
    moreBefore: from > 0,
    moreAfter: to < total,
  };
}

/**
 * 往外扩一档后的 `pad`（不是窗口区间，改 `pad` 比改区间更省事：`windowOf`
 * 已经按 `pad` 算好了省略号与边界）。
 *
 * `pad` 是两侧对称的口径，所以"往某一侧扩"实际上是把 `pad` 抬到目标侧需要
 * 的字数为止：`need` 是那一侧离全文边界还有多少字，够不着一档就一次给完，
 * 够得着就只加一档。返回原值表示那一侧已经全在窗口里（不该再给按钮）。
 */
export function growPad(
  pad: number,
  direction: 'before' | 'after',
  start: number,
  end: number,
  total: number,
): number {
  const need = direction === 'before' ? start : Math.max(0, total - end);
  if (need <= pad) return pad;
  return Math.min(need, pad + CONTEXT_STEP);
}

/**
 * 多次命中时的轮换。`count` 是命中总数，`index` 是当前位置。
 *
 * 越界按环形处理：命中列表是**同一句引文**在各处的出现，从最后一条再往前
 * 回到第一条是连续的读法，不是错误。
 */
export function cycleIndex(index: number, delta: number, count: number): number {
  if (count <= 1) return 0;
  return ((index + delta) % count + count) % count;
}

/**
 * 正文被截断时，引用位置是否已经超出**手里这份**正文。
 *
 * 这条判据只服务一句话：「这份材料太长，只载入了前 N 字，这条引用在第 M 字，
 * 看不到」。没有它，界面会在一个合法但空的位置上显示一个空窗口。
 */
export function beyondLoaded(
  start: number,
  loadedLength: number,
  total: number,
): boolean {
  return total > loadedLength && start >= loadedLength;
}
