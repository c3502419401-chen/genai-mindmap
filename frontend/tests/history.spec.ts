import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import type { MindMapContent, MindNode } from '../src/api/types';
import { LIMITS } from '../src/mindmap/edits';
import { evictCount } from '../src/mindmap/history-limit';
import { HISTORY_BYTES_LIMIT, HISTORY_LIMIT, useMindMapStore } from '../src/stores/mindmap';

/** 造一棵够大的树：节点上限提到 1000 之后，单份快照本来就不小。 */
function bigTree(branches: number): MindNode {
  const label = '节点文字'.repeat(20); // 80 字，接近 label 上限的一半
  return {
    id: 'root',
    label: '根节点',
    children: Array.from({ length: branches }, (_, i) => ({
      id: `n${i}`,
      label: `${label}${i}`,
      children: [],
    })),
  };
}

function bigContent(branches: number): MindMapContent {
  return { title: '大图', root: bigTree(branches) };
}

/**
 * 造一棵**字节大、节点少**的树：每个节点灌满 `detail`（契约上限 2000 字）。
 *
 * 为什么不用"1000 个节点"来把字节灌上去：这一组要证明的是"累计字节超上限时淘汰
 * 最旧的"，不是"1000 个节点能存下"。而 1000 个普通节点的单份快照约 6.6MB——
 * 在 `HISTORY_LIMIT = 50` 的步数上限之前，**字节上限一次都命中不了**（那正是这条
 * 用例从前恒真的原因，见 `docs/TEST-COST-3-CASES.md` §6）。
 *
 * 灌 `detail` 就不一样：一个满 detail 节点贡献 2000 字 ≈ 6000 字节，60 个节点的
 * 单份快照约 364KB，**第 29 步就把 10MB 顶满**——比步数上限早得多，于是这条规则
 * 才真的被执行。挂载成本几乎为零（它只是数据，不渲染）。
 */
function fatTree(branches: number): MindNode {
  const detail = '详情'.repeat(LIMITS.detail / 2); // 2000 字
  return {
    id: 'root',
    label: '根节点',
    detail,
    children: Array.from({ length: branches }, (_, i) => ({
      id: `n${i}`,
      label: `分支${i}`,
      detail,
      children: [],
    })),
  };
}

function fatContent(branches: number): MindMapContent {
  return { title: '大图', root: fatTree(branches) };
}

beforeEach(() => {
  setActivePinia(createPinia());
});

/**
 * 这一组为什么要显式上限：**判 A（规模型），不是掩盖慢**。
 *
 * 先排掉最大的误判——**它不是渲染型**：本文件一行 `mount` 都没有。慢在"真实编辑
 * 次数 × 每次约 3ms 的生产代码成本"，插桩读数（`docs/TEST-COST-3-CASES.md` §1，
 * 对象是改造前那条 300 节点 / 80 步的负载）：
 *
 *   80 次 `commitLabel` 合计 281.8ms，p50 = 2.97ms/次。分解：
 *   `JSON.stringify(响应式 content)` 0.94ms（= `toRaw` 的 11.6×，Vue 的 Proxy 遍历）
 *   + 深拷贝子树 1.19ms + 全树 diff 0.63ms + `TextEncoder` 编码 0.12ms ≈ 2.88ms
 *   （占 97%）。那 3ms 是生产代码每改一个节点都要付的钱，不是"同一件事算了两遍"。
 *
 * 规模不许降：节点数与步数是这两条用例的前提（第一条要"步数顶到 50"、第二条要
 * "字节顶到 10MB"），降规模等于抽掉断言的前提。
 *
 * 上限的来历——本地单跑 **128ms**（改造后的新负载，见下面那条用例）、本地加压
 * （两份全量 vitest 同时跑）**最坏 146ms**。但同一条用例在**旧负载**（300 节点 /
 * 80 步）下加压见过 **6606ms**（相对单跑 281ms 放大约 24×）——这台机器上抖动最大
 * 的一次。按同一个放大上界推新负载 ≈ 128 × 24 = 3.1s，CI 估 ≈ 3.1 × 1.3–2 =
 * **4.0–6.2s**，所以给 **8 秒** = CI 估的 1.3–2×。
 *
 * （加压读数抖动极大：同一次加压的两份进程里，旧负载跑过 365ms 也跑过 6606ms。
 * 见 `docs/TEST-COST-3-CASES.md` §0——上限必须按见过的最坏值定，不能按单次读数。）
 */
describe('撤销栈的字节上限', { timeout: 8_000 }, () => {
  it('步数上限仍然生效', () => {
    const store = useMindMapStore();
    store.preview(bigContent(3), 'right');

    for (let i = 0; i < HISTORY_LIMIT + 10; i += 1) {
      store.commitLabel('n0', `第 ${i} 次改动`);
    }

    expect(store.undoDepth).toBe(HISTORY_LIMIT);
  });

  /**
   * 字节上限**先于**步数上限命中：40 步（远不到 50）就被压到 28 份左右。
   *
   * 从前这一条是恒真的：300 节点 / 70.5KB 的快照，50 步才 3525KB ≪ 10MB，
   * 于是"丢掉最旧的"永远是**步数**上限干的，字节那半边一次都没执行过——
   * `undoBytes <= HISTORY_BYTES_LIMIT` 写不写都成立。
   *
   * 现在用"少节点 × 满 detail"把单份撑到 364KB：29 步累计就超 10MB，
   * 而 40 < `HISTORY_LIMIT`，所以下面那条 `undoDepth < 40` 只可能由字节解释。
   */
  it('字节超上限时丢最旧的，栈不会一直涨', () => {
    const store = useMindMapStore();
    // 60 个满 detail 节点：单份快照约 364KB，10MB 在第 29 步被顶满
    store.preview(fatContent(60), 'right');
    expect(store.undoBytes).toBe(0);

    const steps = 40;
    // 前提：步数远未到上限，否则"谁淘汰的"又说不清了
    expect(steps).toBeLessThan(HISTORY_LIMIT);
    for (let i = 0; i < steps; i += 1) {
      store.commitLabel(`n${i % 60}`, `第 ${i} 次改动`);
    }

    // 步数没到线，栈却短了：这是字节上限干的
    expect(store.undoDepth).toBeLessThan(steps);
    expect(store.undoDepth).toBeGreaterThan(0);
    expect(store.undoBytes).toBeLessThanOrEqual(HISTORY_BYTES_LIMIT);
    // 淘汰后仍贴着上限（不是被清空）：证明它"丢到不超为止"而不是"一超就清"
    expect(store.undoBytes).toBeGreaterThan(HISTORY_BYTES_LIMIT / 2);
    expect(store.canUndo).toBe(true);
  });

  it('撤销会同步减掉字节，不会越用越大', () => {
    const store = useMindMapStore();
    store.preview(bigContent(50), 'right');

    for (let i = 0; i < 5; i += 1) {
      store.commitLabel(`n${i}`, `第 ${i} 次改动`);
    }
    const before = store.undoBytes;
    expect(before).toBeGreaterThan(0);

    store.undo();
    expect(store.undoBytes).toBeLessThan(before);
    // 重做把快照压回撤销栈，字节又回来
    store.redo();
    expect(store.undoBytes).toBe(before);
  });

  it('清空历史时字节归零', () => {
    const store = useMindMapStore();
    store.preview(bigContent(10), 'right');
    store.commitLabel('n0', '改一下');
    expect(store.undoBytes).toBeGreaterThan(0);

    store.preview(bigContent(10), 'right');
    expect(store.undoBytes).toBe(0);
    expect(store.undoDepth).toBe(0);
  });
});

/**
 * 淘汰判据本身：纯函数，毫秒级。
 *
 * 与上面那一组分工——**这里测规则，上面测接线**。规则用纯函数测能给任意边界
 * （步数先命中 / 字节先命中 / 两者同时 / 一份自己就超限），而走链路只能摆出
 * "恰好被这一次数据量顶到"的那一种。上面那条大快照用例的职责就只剩一句话：
 * "`recordPast()` 真的调用了这个判据"。
 */
describe('撤销栈的淘汰判据（evictCount，纯函数）', () => {
  it('两个上限都没到：一份都不淘汰', () => {
    expect(evictCount([100, 100, 100], { count: 50, bytes: 1000 })).toBe(0);
  });

  it('正好等于上限：不淘汰（判据是「超过」不是「达到」）', () => {
    // 4 份 × 250 = 1000 = bytes 上限；4 = count 上限
    expect(evictCount([250, 250, 250, 250], { count: 4, bytes: 1000 })).toBe(0);
  });

  it('字节刚超一点：只丢最旧的一份', () => {
    expect(evictCount([250, 250, 250, 250, 1], { count: 4, bytes: 1000 })).toBe(1);
  });

  it('字节远超上限：丢到不超为止', () => {
    // 1800 → 丢一份 600 → 1200 仍超 → 再丢 → 600 ≤ 1000
    expect(evictCount([600, 600, 600], { count: 50, bytes: 1000 })).toBe(2);
  });

  it('步数先命中：按步数丢，字节还富余也照丢', () => {
    expect(evictCount([1, 1, 1, 1, 1], { count: 3, bytes: 1e9 })).toBe(2);
  });

  it('一份自己就超上限：至少留一份（清空等于撤销功能失效）', () => {
    expect(evictCount([5000], { count: 50, bytes: 1000 })).toBe(0);
    // 有两份时只丢到剩一份，剩下的那份仍然超限，但必须留着
    expect(evictCount([5000, 1], { count: 50, bytes: 1000 })).toBe(1);
    expect(evictCount([5000, 5000], { count: 50, bytes: 1000 })).toBe(1);
  });

  it('空栈什么都不做', () => {
    expect(evictCount([], { count: 50, bytes: 10 })).toBe(0);
  });

  it('按生产上限摆一遍：40 份 × 364KB 会丢掉 12 份', () => {
    const oneSnapshot = 364 * 1024;
    const kept = evictCount(Array.from({ length: 40 }, () => oneSnapshot), {
      count: HISTORY_LIMIT,
      bytes: HISTORY_BYTES_LIMIT,
    });
    // 10MB / 364KB ≈ 28.7 → 留 28 份 → 丢 12 份
    expect(kept).toBe(12);
  });
});
