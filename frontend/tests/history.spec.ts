import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import type { MindMapContent, MindNode } from '../src/api/types';
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

beforeEach(() => {
  setActivePinia(createPinia());
});

/**
 * 这一组为什么要显式上限：**判 A（规模型），不是掩盖慢**。
 *
 * 先排掉最大的误判——**它不是渲染型**：本文件一行 `mount` 都没有。慢在"80 次真实
 * 编辑 × 每次约 3ms 的生产代码成本"，插桩读数（`docs/TEST-COST-3-CASES.md` §1）：
 *
 *   快照 82.5KB（300 节点 × 240 字节 label）；80 次 `commitLabel` 合计 281.8ms，
 *   p50 = 2.97ms/次。分解：`JSON.stringify(响应式 content)` 0.94ms
 *   （= `toRaw` 的 11.6×，Vue 的 Proxy 遍历）+ 深拷贝子树 1.19ms
 *   + 全树 diff 0.63ms + `TextEncoder` 编码 0.12ms ≈ 2.88ms（占 97%）。
 *
 * 那 3ms 是**生产代码每改一个节点都要付的钱**（撤销栈要序列化、树要不可变更新、
 * 改动账本要 diff），不是"同一件事算了两遍"：唯一重复计算的是淘汰旧快照时再编码
 * 一次（0.12ms × 75 ≈ 8.8ms，3%），为它改生产代码不值当。
 *
 * 规模不许降：300 节点 / 80 步就是"几十步内把栈顶满"这个前提本身。
 *
 * 上限的来历——本地单跑 286ms、本地加压（两份全量 vitest 同时跑）**最坏 6606ms**、
 * CI 估 ≈ 加压 × 1.3–2 = 8.6–13.2s。给 15 秒 = 本地加压 2.3×、CI 估的 1.1–1.7×。
 * 不给 20 秒：这条的固有成本比 136 节点那两条低一个量级，20 秒会放过真卡死。
 * （加压读数抖动极大：同一次加压的两份进程里，这一条跑过 365ms 也跑过 6606ms。
 * 见 `docs/TEST-COST-3-CASES.md` §0——上限必须按见过的最坏值定。）
 */
describe('撤销栈的字节上限', { timeout: 15_000 }, () => {
  it('步数上限仍然生效', () => {
    const store = useMindMapStore();
    store.preview(bigContent(3), 'right');

    for (let i = 0; i < HISTORY_LIMIT + 10; i += 1) {
      store.commitLabel('n0', `第 ${i} 次改动`);
    }

    expect(store.undoDepth).toBe(HISTORY_LIMIT);
  });

  it('字节超上限时丢最旧的，栈不会一直涨', () => {
    const store = useMindMapStore();
    // 每个节点 80 字中文，快照约 20KB 量级；300 个分支足够在几十步内撑满 10MB
    store.preview(bigContent(300), 'right');
    expect(store.undoBytes).toBe(0);

    for (let i = 0; i < 80; i += 1) {
      store.commitLabel(`n${i % 300}`, `第 ${i} 次改动`);
    }

    expect(store.undoDepth).toBeLessThan(80);
    expect(store.undoBytes).toBeLessThanOrEqual(HISTORY_BYTES_LIMIT);
    // 仍然至少留得下一步
    expect(store.undoDepth).toBeGreaterThan(0);
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
