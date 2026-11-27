import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { DiagramContent, MindMapContent } from '../src/api/types';
import { diffEdits, type EditEvent } from '../src/mindmap/edit-log';
import { useMindMapStore } from '../src/stores/mindmap';
import realJson from './fixtures/repair-real.json';

/**
 * 账本里"一键修复"与"用户自己改"是两件事：**`kind='repair'`**。
 *
 * ## 这份用例为什么存在
 *
 * 一键修复把整棵树替换掉，diff 出来的是 `remove` / `add` / `move` / `update(label)`
 * 那批动作。整批事件的 `actor` 是 `user`（扳机是用户按的，`EditActor` 里 `agent`
 * 的语义是"模型算的"，而规则修复一次模型调用都没有），于是用户回看"我改过什么"
 * 会看到一堆自己没做过的改动。
 *
 * 分开的维度是 `kind` 而不是第三种 `actor`：`actor` 回答"谁有能动性"，`kind`
 * 回答"这是一次什么动作"。这套分工 `generate` 早就用过（`actor='agent'` 但靠 kind
 * 与"生成带来的那批节点改动"分开）。理由与代价见 `docs/OPTIMIZATION-REPAIR.md` §7。
 *
 * ## 钉的三件事
 *
 *  1. `DiffOptions.kind` 覆盖**且只覆盖** `kind` 一个字段（动作粒度仍在事件行上）；
 *  2. 整条链路到 HTTP：保存请求体里那批事件的 `kind` 就是 `repair`（契约里已有这个值，
 *     否则后端 `extra="forbid"` 之外还会多一条 422 —— `EditKind` 是 `Literal`）；
 *  3. 反向：手工编辑一条标题仍然是 `update`。这条防的是"覆盖写宽了"——把整批默认值
 *     做成全局默认，用户手改的那一条也会被染成 `repair`。
 */
interface Fixture {
  before: MindMapContent;
  first: { content: MindMapContent; steps: number };
  second: { content: MindMapContent; steps: number };
}

const REAL = realJson as unknown as Fixture;
const AT = '2026-11-27T09:00:00.000Z';

function clone<T>(value: T): T {
  return structuredClone(value);
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

/** 保存端点的替身，顺手截下请求体里的 `events`（那才是这份用例要看的字节）。 */
function stubBackend(): { events: EditEventPayloadLike[][] } {
  const hits: { events: EditEventPayloadLike[][] } = { events: [] };
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    if (url.endsWith('/mindmaps') && init?.method === 'POST') {
      hits.events.push(body?.events ?? []);
      return json({ id: 'm1', version: 1, updated_at: '2026-01-01T00:00:00Z' });
    }
    return json({});
  }));
  return hits;
}

interface EditEventPayloadLike {
  actor: string;
  kind: string;
  field?: string;
  subtree_size?: number;
}

/** 除 `kind` 之外的字段（用来证明覆盖只动了一个字段）。 */
function withoutKind(event: EditEvent): Omit<EditEvent, 'kind'> {
  const { kind: _kind, ...rest } = event;
  return rest;
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('DiffOptions.kind：只换动作名，不动别的', () => {
  it('给了 kind 就是整批 repair；不给就保留 diff 自己判出来的动作名', () => {
    const before = REAL.before as unknown as DiagramContent;
    const after = REAL.first.content as unknown as DiagramContent;

    const plain = diffEdits(before, after, { actor: 'user', at: AT });
    const asRepair = diffEdits(before, after, { actor: 'user', at: AT, kind: 'repair' });

    // 一批事件，条数一模一样——覆盖不新增也不合并事件。
    expect(asRepair.length).toBe(plain.length);
    expect(asRepair.length).toBeGreaterThan(0);
    // 不加 kind 时动作名不止一种（3 处压链会同时改文字、删掉被压掉的那一层、搬子树）。
    expect(new Set(plain.map(event => event.kind)).size).toBeGreaterThan(1);
    // 加了之后整批只有一个动作名。
    expect(asRepair.every(event => event.kind === 'repair')).toBe(true);
    // ★ 除 kind 之外逐字段相同：`field` / `before` / `after` / `depth` / `subtreeSize`
    //   一个都没被抹掉。动作粒度没丢，只是不能再按 kind 分桶读。
    expect(asRepair.map(withoutKind)).toEqual(plain.map(withoutKind));
    // actor 与时刻也不受影响。
    expect(asRepair.every(event => event.actor === 'user' && event.at === AT)).toBe(true);
  });
});

describe('账本链路：修复记 repair，手工改仍是 update', () => {
  it('保存请求体里那批事件的 kind 全是 repair、actor 全是 user', async () => {
    const hits = stubBackend();
    const store = useMindMapStore();
    store.preview(clone(REAL.before), 'right');

    store.replaceContent(clone(REAL.first.content));
    await expect(store.save()).resolves.toBe('saved');

    const sent = hits.events[0];
    expect(sent.length).toBeGreaterThan(0);
    expect(sent.every(event => event.kind === 'repair')).toBe(true);
    expect(sent.every(event => event.actor === 'user')).toBe(true);
    // 那条改文字的还在（它正是 `label_shrunk` 要排除的样本）。
    expect(sent.some(event => event.field === 'label')).toBe(true);
  });

  it('修完之后手工改一个标题，那条仍然是 update（覆盖没有变成全局默认）', () => {
    const store = useMindMapStore();
    store.preview(clone(REAL.before), 'right');
    store.replaceContent(clone(REAL.first.content));

    const repaired = store.pendingEdits.length;
    const rootId = (store.content as MindMapContent).root.id;
    store.commitLabel(rootId, '手工改过的中心主题');

    const manual = store.pendingEdits.slice(repaired);
    expect(manual.length).toBe(1);
    expect(manual[0].kind).toBe('update');
    expect(manual[0].field).toBe('label');
    expect(manual[0].actor).toBe('user');
  });
});
