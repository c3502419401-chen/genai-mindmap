import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { exportMindMap } from '../src/api/client';
import type { DiagramContent, MindMapContent } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';
import mergeJson from './fixtures/repair-merge.json';
import realJson from './fixtures/repair-real.json';

/**
 * 「整体替换进撤销栈」的语义：**一条**，不是 N 条。
 *
 * ## 这份用例为什么存在
 *
 * 修复（`/optimization/repair`）一次会改多处——重名合并要删节点，单子链要把两层压成
 * 一层（**改标题**）。前端能选的路只有三条，两条是错的：
 *
 *  - `content = next`：能改，但不可撤销、`dirty` 与 `clientRevision` 不更新；
 *  - `importMap(next)`：`resetHistory()`，撤销栈直接清零；
 *  - `replaceContent(next)`：走 `applyContent(next, true)`，压**一步**快照。
 *
 * 所以这里钉的不是"能修"，是粒度：**按一次 Ctrl+Z 就回到修复之前**，而不是回到
 * "第 1 处修复之前"。粒度错了的表现是"用户要按三次才回到原样"——它不报错，只让这个
 * 功能听起来坏了。
 *
 * 这里**只驱动 store**（不经过 HTTP、不经过面板）：那一层才是语义的所在地。接口与
 * 面板上的行为在 `repair-apply.spec.ts`。
 *
 * ## fixture 的来源
 *
 * `repair-real.json`：库里真图（`backend/data/mindmaps.db` 的 `0b82a740`「机器学习」，
 * 3 处单子链），`before` / `first` / `second` 由后端路由函数真跑产出，与线上响应逐字节
 * 相同。`repair-merge.json`：构造输入 + 真响应（真库里唯一有同级重名的图是 561 节点
 * 那张异常图，600KB，塞不进仓库），它一次修两处，正好用来验证"多处 = 一条撤销"。
 */
interface Fixture {
  before: MindMapContent;
  /** 只需要"修好的树 + 步数"：这份用例盯的是 store 的语义，不是响应体的全貌。 */
  first: { content: MindMapContent; steps: number };
  second: { content: MindMapContent; steps: number };
}

// JSON 导入会把字面量放宽（`"tree"` → `string`），所以在这一个边界上断言一次类型；
// 两个 fixture 都由后端的 `model_dump_json()` 产出，形状就是响应体本身。
const REAL = realJson as unknown as Fixture;
const MERGE = mergeJson as unknown as Fixture;

function clone<T>(value: T): T {
  return structuredClone(value);
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

interface Hits {
  /** 每次保存请求里的 `content`（序列化后的字符串，直接比字节）。 */
  saves: string[];
  /** 每次导出请求里的 `content`。 */
  exports: string[];
}

/** 保存与导出这两个端点的替身；顺手截下请求体，用来核对"带上去的是不是那棵树"。 */
function stubBackend(): Hits {
  const hits: Hits = { saves: [], exports: [] };
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    if (url.includes('/export/')) {
      hits.exports.push(JSON.stringify(body?.content));
      return new Response('exported', { status: 200 });
    }
    if (url.endsWith('/mindmaps') && init?.method === 'POST') {
      hits.saves.push(JSON.stringify(body?.content));
      return json({ id: 'm1', version: 1, updated_at: '2026-01-01T00:00:00Z' });
    }
    return json({});
  }));
  return hits;
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('一次整体替换 = 一条撤销', () => {
  it('真图 3 处单子链：按一次 Ctrl+Z 逐字节回到替换前，再按一次重做回到替换后', () => {
    const store = useMindMapStore();
    store.preview(clone(REAL.before), 'right');

    // 替换前的字节。整份快照栈存的就是这个字符串，所以"逐字节回到原样"可以真比。
    const beforeJson = JSON.stringify(store.content);
    expect(store.undoDepth).toBe(0);

    expect(store.replaceContent(clone(REAL.first.content))).toBe(true);

    // ★ 三处修复 → 还是**一步**。
    expect(store.undoDepth).toBe(1);
    expect(JSON.stringify(store.content)).toBe(JSON.stringify(REAL.first.content));
    // 换树之后的副作用：脏、版本递增（生成期间的冲突判定读它）。
    expect(store.dirty).toBe(true);
    expect(store.clientRevision).toBe(1);

    store.undo();
    expect(JSON.stringify(store.content)).toBe(beforeJson);
    expect(store.canRedo).toBe(true);

    store.redo();
    expect(JSON.stringify(store.content)).toBe(JSON.stringify(REAL.first.content));
    expect(store.undoDepth).toBe(1);
  });

  it('一次修了两处（重名合并 + 单子链）也是一条撤销，不是两条', () => {
    const store = useMindMapStore();
    store.preview(clone(MERGE.before), 'right');
    const beforeJson = JSON.stringify(store.content);

    store.replaceContent(clone(MERGE.first.content));

    expect(MERGE.first.steps).toBe(2);
    expect(store.undoDepth).toBe(1);
    store.undo();
    expect(JSON.stringify(store.content)).toBe(beforeJson);
  });

  it('空画布上不装东西：没有"替换前"可退，调用方据此能说清原因', () => {
    const store = useMindMapStore();
    expect(store.content).toBeNull();
    expect(store.replaceContent(clone(REAL.first.content))).toBe(false);
    expect(store.content).toBeNull();
    expect(store.undoDepth).toBe(0);
    expect(store.dirty).toBe(false);
  });
});

describe('改动账本：批次与 actor', () => {
  it('一批事件共享一个时刻，actor 记 user（整体替换没有调模型）', () => {
    const store = useMindMapStore();
    store.preview(clone(REAL.before), 'right');

    store.replaceContent(clone(REAL.first.content));

    const events = store.pendingEdits;
    // 3 处压链会同时改标题（update）、删掉被压掉的那一层（remove）、把孙节点搬上来（move）。
    expect(events.length).toBeGreaterThan(0);
    expect(new Set(events.map(event => event.at)).size).toBe(1);
    expect(events.every(event => event.actor === 'user')).toBe(true);
    expect(events.some(event => event.kind === 'remove')).toBe(true);
    expect(events.some(event => event.kind === 'update' && event.field === 'label')).toBe(true);
  });

  it('撤销本身不记账（一次被撤销的编辑不是用户的最终意图）', () => {
    const store = useMindMapStore();
    store.preview(clone(REAL.before), 'right');

    store.replaceContent(clone(REAL.first.content));
    const count = store.pendingEdits.length;
    store.undo();
    expect(store.pendingEdits.length).toBe(count);
  });
});

describe('保存 / 导出 / 重新导入', () => {
  it('保存与导出带上去的都是替换后的树，导入回来一字不差', async () => {
    const hits = stubBackend();
    const store = useMindMapStore();
    store.preview(clone(REAL.before), 'right');

    store.replaceContent(clone(REAL.first.content));
    const repairedJson = JSON.stringify(store.content);

    await expect(store.save()).resolves.toBe('saved');
    expect(hits.saves[0]).toBe(repairedJson);

    await exportMindMap('json', store.content!);
    expect(hits.exports[0]).toBe(repairedJson);

    // 重新导入：文件里是文本，装回画布必须还是同一棵树（`importMap` 会 resetHistory）。
    store.importMap(JSON.parse(repairedJson) as DiagramContent);
    expect(JSON.stringify(store.content)).toBe(repairedJson);
  });
});
