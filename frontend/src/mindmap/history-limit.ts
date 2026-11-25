/**
 * 撤销栈的**淘汰判据**，抽成纯函数。
 *
 * 为什么要单独一个文件：这条规则原先只有一处实现，藏在 store 的 `recordPast()`
 * 里，且与两个 `ref`（`past` / `pastBytes`）缠在一起，于是它只有一条测法——
 * "造一棵足够大的树、加够多次、看栈会不会涨"。那条测法有两个问题：
 *
 *  1. **它测的是链路，不是规则**。规则本身只有一句话：累计字节超过上限就从最旧
 *     开始丢、丢到不超为止，且至少留一份（一份自身就超限时清空栈等于撤销功能
 *     直接失效，那比多占这 10MB 更糟）。走完整链路要同时满足"快照够大""步数够多"
 *     才**有可能**触发字节上限——而在当前契约下（`LIMITS.nodes = 1000`、
 *     `LIMITS.detail = 2000`）单份快照最大约 6.6MB，若树不够大，**步数上限 50 先
 *     命中，字节上限一次都不会生效**，那条断言于是恒真（详见
 *     `docs/TEST-COST-3-CASES.md` §6）。
 *  2. **它把"规则对不对"和"接线通不通"混成一条**。规则用纯函数测（毫秒级），
 *     "`recordPast()` 真的调用了它"另用一条刻意造出的大快照测——两条各管一件事。
 *
 * 这里刻意不 import store 里的常量：纯函数收 limits 参数，测试可以给任意组合
 * （"步数先命中""字节先命中""两者同时命中"三种边界都能摆出来）。
 */

/** 两个上限。`count` 是步数，`bytes` 是累计字节。 */
export interface HistoryLimits {
  count: number;
  bytes: number;
}

/**
 * 从**最旧**开始要淘汰几份。
 *
 * `entryBytes` 按"最旧 → 最新"排（与 `past` 数组同序）。返回值是"该 `shift()`
 * 几次"，0 表示一份都不淘汰。
 *
 * 三条边界与 store 里的 `while` 逐字对应：
 *  - `kept > 1`：至少留一份。一份快照自身就超过 `bytes` 时（极端大的树）清空栈会
 *    让撤销直接失效，那比占着 10MB 更糟——所以这份"超限"是被允许的。
 *  - `kept > limits.count`：步数超了。
 *  - `bytes > limits.bytes`：累计字节超了。
 *
 * 两者是**或**关系：任何一个超了就开始丢，直到两个都不超（或只剩一份）。
 */
export function evictCount(entryBytes: readonly number[], limits: HistoryLimits): number {
  let kept = entryBytes.length;
  let bytes = entryBytes.reduce((sum, size) => sum + size, 0);
  let drop = 0;
  while (kept > 1 && (kept > limits.count || bytes > limits.bytes)) {
    bytes -= entryBytes[drop];
    drop += 1;
    kept -= 1;
  }
  return drop;
}
