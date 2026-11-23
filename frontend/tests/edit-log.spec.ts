import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MapRecord, MindMapContent, MindNode } from '../src/api/types';
import { diffEdits, EDIT_EVENT_LIMIT } from '../src/mindmap/edit-log';
import { useMindMapStore } from '../src/stores/mindmap';

const AT = '2026-11-17T09:31:02.123Z';

function node(id: string, label: string, children: MindNode[] = []): MindNode {
  return { id, label, children };
}

function content(root: MindNode, extra: Partial<MindMapContent> = {}): MindMapContent {
  return { title: '示例', root, ...extra };
}

/** 两层的小树：root → a(→ b)。改动都从这棵树上做，事件数一眼数得清。 */
function tree(): MindMapContent {
  return content(node('root', '主题', [node('a', '甲', [node('b', '乙')])]));
}

// ---------------------------------------------------------------- 纯函数

describe('diffEdits：改了什么', () => {
  it('改标题是一条 label 事件，带层数', () => {
    const events = diffEdits(tree(), content(node('root', '主题', [node('a', '新标题', [node('b', '乙')])])), {
      actor: 'user', at: AT,
    });

    expect(events).toEqual([{
      actor: 'user', at: AT, kind: 'update', field: 'label',
      nodeId: 'a', before: '甲', after: '新标题', depth: 2,
    }]);
  });

  it('改颜色是 style 事件，不是 update', () => {
    const after = content(node('root', '主题', [
      { ...node('a', '甲', [node('b', '乙')]), color: 'red' },
    ]));

    expect(diffEdits(tree(), after, { actor: 'user', at: AT })).toEqual([{
      actor: 'user', at: AT, kind: 'style', field: 'color',
      nodeId: 'a', before: '无', after: 'red', depth: 2,
    }]);
  });

  it('加一个节点是一条 add，带父节点 id', () => {
    const after = content(node('root', '主题', [node('a', '甲', [node('b', '乙'), node('c', '丙')])]));

    expect(diffEdits(tree(), after, { actor: 'user', at: AT })).toEqual([{
      actor: 'user', at: AT, kind: 'add', nodeId: 'c', after: '丙', depth: 3,
      parentId: 'a',
    }]);
  });

  it('一加就加在最外层的 add，父节点是中心主题', () => {
    const after = content(node('root', '主题', [node('a', '甲', [node('b', '乙')]), node('d', '丁')]));

    const [event] = diffEdits(tree(), after, { actor: 'user', at: AT });
    expect(event).toMatchObject({ kind: 'add', nodeId: 'd', depth: 2, parentId: 'root' });
  });

  it('父节点 id 记的是 id，不是标题：同名兄弟也不会串', () => {
    // 标题会重名（"风险"在两支下各有一个），用标题当父节点标识就会把两条不同的
    // 挂载关系读成同一条。`parentId` 存在的理由就是这个。
    const before = content(node('root', '主题', [node('x', '风险'), node('y', '风险')]));
    const root = node('root', '主题', [
      node('x', '风险'),
      node('y', '风险', [node('c', '新加')]),
    ]);

    const [event] = diffEdits(before, content(root), { actor: 'user', at: AT });
    expect(event).toMatchObject({ kind: 'add', nodeId: 'c', parentId: 'y' });
  });

  it('非 add 的事件不带 parentId（`undefined` 与 `null` 是两件事）', () => {
    const events = diffEdits(
      tree(),
      content(node('root', '主题', [node('a', '新标题', [node('b', '乙')])])),
      { actor: 'user', at: AT },
    );

    expect(events[0]).not.toHaveProperty('parentId');
  });

  it('删一整支：只有一条 remove，带着整支的节点数', () => {
    const before = content(node('root', '主题', [node('a', '甲', [node('b', '乙', [node('c', '丙')])])]));

    const events = diffEdits(before, content(node('root', '主题', [])), { actor: 'user', at: AT });

    // 每个后代各记一条会让 removed_depth 在 2…5 层各留几条，读起来像"每层都在被删"，
    // 而真实动作只有一个：删掉了一支。
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      kind: 'remove', nodeId: 'a', depth: 2, subtreeSize: 3,
    });
    expect(events[0].before).toBe('甲');
    expect(events[0].after).toBeUndefined();
  });

  it('删一个叶节点：subtreeSize 是 1（零散删除）', () => {
    const before = content(node('root', '主题', [node('a', '甲'), node('b', '乙')]));
    const after = content(node('root', '主题', [node('b', '乙')]));

    expect(diffEdits(before, after, { actor: 'user', at: AT })).toEqual([{
      actor: 'user', at: AT, kind: 'remove', nodeId: 'a', before: '甲', depth: 2, subtreeSize: 1,
    }]);
  });

  it('删两支：每支各一条，各自数各自的节点', () => {
    const before = content(node('root', '主题', [
      node('a', '甲', [node('a1', '甲1')]),
      node('b', '乙', [node('b1', '乙1'), node('b2', '乙2', [node('b21', '乙21')])]),
    ]));

    const events = diffEdits(before, content(node('root', '主题', [])), { actor: 'user', at: AT });

    expect(events.map(event => [event.nodeId, event.subtreeSize, event.depth])).toEqual([
      ['a', 2, 2],
      ['b', 4, 2],
    ]);
  });

  it('删节点但子节点被改派活下来：subtreeSize 只数真的消失的那个', () => {
    // 直接用"旧子树的节点数"会把活下来的 a1 也算进去，于是"删了 1 个"被读成"删了 2 个"。
    const before = content(node('root', '主题', [node('a', '甲', [node('a1', '甲1')])]));
    const after = content(node('root', '主题', [node('a1', '甲1')]));

    const events = diffEdits(before, after, { actor: 'user', at: AT });

    expect(events.map(event => [event.kind, event.nodeId, event.subtreeSize ?? null])).toEqual([
      ['move', 'a1', null],
      ['remove', 'a', 1],
    ]);
  });

  it('换父是 move，前后是两边的父标题', () => {
    const before = content(node('root', '主题', [node('a', '甲'), node('x', 'X')]));
    const after = content(node('root', '主题', [node('x', 'X', [node('a', '甲')])]));

    const moves = diffEdits(before, after, { actor: 'user', at: AT })
      .filter(event => event.kind === 'move');

    expect(moves).toEqual([{
      actor: 'user', at: AT, kind: 'move', nodeId: 'a',
      before: '主题', after: 'X', depth: 3,
    }]);
  });

  it('同父换序也算 move（这一层成员没变）', () => {
    const before = content(node('root', '主题', [node('a', '甲'), node('b', '乙')]));
    const after = content(node('root', '主题', [node('b', '乙'), node('a', '甲')]));

    const moves = diffEdits(before, after, { actor: 'user', at: AT });

    // 遍历顺序跟着新树走（新树里 b 在前），所以 b 的事件先出现。
    expect(moves.map(event => [event.nodeId, event.before, event.after])).toEqual([
      ['b', '第2位', '第1位'],
      ['a', '第1位', '第2位'],
    ]);
  });

  it('插入一个兄弟不会给其它兄弟记 move', () => {
    // 这是"顺序变化算不算改动"那条判断的验收点：按 index 直接比，插一个节点会让
    // 后面所有兄弟都 +1，1 次操作变成 N 条 move，`by_kind` 立刻失真。
    const before = content(node('root', '主题', [node('a', '甲'), node('b', '乙')]));
    const after = content(node('root', '主题', [node('a', '甲'), node('c', '丙'), node('b', '乙')]));

    const events = diffEdits(before, after, { actor: 'user', at: AT });

    expect(events.map(event => event.kind)).toEqual(['add']);
    expect(events[0].nodeId).toBe('c');
  });

  it('加一条附加边是一条 edge 事件', () => {
    const after = content(node('root', '主题', [node('a', '甲', [node('b', '乙')])]), {
      edges: [{ id: 'e_1', source: 'b', target: 'root', label: '是' }],
    });

    expect(diffEdits(tree(), after, { actor: 'user', at: AT })).toEqual([{
      actor: 'user', at: AT, kind: 'edge', nodeId: 'b', after: '乙 → 主题（是）',
    }]);
  });

  it('一次操作改了两个字段就是两条事件', () => {
    // 合并成一条会让 by_field 把一次改动算进两个桶，`sum(by_field)` 就对不上
    // `by_kind.update + by_kind.style`——统计一旦不能自校验，读的人得先怀疑数字。
    const after = content(node('root', '主题', [
      { ...node('a', '新标题', [node('b', '乙')]), color: 'amber' },
    ]));

    const events = diffEdits(tree(), after, { actor: 'user', at: AT });

    expect(events.map(event => `${event.kind}:${event.field}`)).toEqual([
      'update:label', 'style:color',
    ]);
    expect(new Set(events.map(event => event.at)).size).toBe(1);
  });

  it('detail 只存长度，不存全文', () => {
    const before = content(node('root', '主题', [{
      ...node('a', '甲'), detail: '长'.repeat(1800),
    }]));
    const after = content(node('root', '主题', [{
      ...node('a', '甲'), detail: '短'.repeat(700),
    }]));

    const events = diffEdits(before, after, { actor: 'user', at: AT });

    expect(events).toHaveLength(1);
    expect(events[0].before).toBe('1800字');
    expect(events[0].after).toBe('700字');
    // 2000 字的正文进日志会把这张表撑成第二个 content，长度本身就是情报。
    expect(events[0].before!.length).toBeLessThan(10);
  });

  it('导图标题的改动记成 field=title，不混进 label', () => {
    const after = content(node('root', '主题', [node('a', '甲', [node('b', '乙')])]), {
      title: '新标题',
    });

    expect(diffEdits(tree(), after, { actor: 'user', at: AT })).toEqual([{
      actor: 'user', at: AT, kind: 'update', field: 'title', before: '示例', after: '新标题',
    }]);
  });

  it('空画布 → 一整棵树不产生事件', () => {
    // create 意图整树落地：整棵树都是"新增"，那不是 diff，是把整棵树重念一遍。
    expect(diffEdits(null, tree(), { actor: 'agent', at: AT })).toEqual([]);
  });

  it('runId 只在给出时出现', () => {
    const after = content(node('root', '主题', [node('a', '新标题', [node('b', '乙')])]));
    const events = diffEdits(tree(), after, { actor: 'agent', at: AT, runId: 'run-1' });
    expect(events[0].runId).toBe('run-1');
    expect(diffEdits(tree(), after, { actor: 'user', at: AT })[0]).not.toHaveProperty('runId');
  });
});

// ---------------------------------------------------------------- store 集成

interface Call {
  url: string;
  method: string;
  body: Record<string, unknown>;
}

const RECORD: MapRecord = {
  id: 'm1', version: 2, content: tree(), updated_at: '2026-11-17T09:31:02.123Z',
};

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

let calls: Call[] = [];

function stubFetch(handler: (call: Call) => Response | Promise<Response>) {
  calls = [];
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const call: Call = {
      url: String(input),
      method: init?.method ?? 'GET',
      body: init?.body ? JSON.parse(String(init.body)) : {},
    };
    calls.push(call);
    return handler(call);
  }));
}

/** 只发 `done` 帧再 `[DONE]`：够把候选放进 store，不用真跑一轮 agent。 */
function streamWithDone(next: MindMapContent): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(
        `data: ${JSON.stringify({ type: 'done', seq: 1, content: next, mode: 'model' })}\n\n`,
      ));
      controller.enqueue(encoder.encode('data: [DONE]\n\n'));
      controller.close();
    },
  });
  return { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('store：事件攒到保存时提交', () => {
  it('手工编辑记成 user', () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');

    store.commitLabel('a', '新标题');

    expect(store.pendingEdits).toHaveLength(1);
    expect(store.pendingEdits[0]).toMatchObject({
      actor: 'user', kind: 'update', field: 'label', nodeId: 'a',
      before: '甲', after: '新标题',
    });
  });

  it('生成落地带来的改动记成 agent，并带上那次 run 的 id', async () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');
    stubFetch(() => streamWithDone(content(node('root', '主题', [node('a', '改过的甲', [node('b', '乙')])]))));

    await store.runGeneration('把甲改个名', 'edit');

    const changes = store.pendingEdits.filter(event => event.kind !== 'generate');
    expect(changes.length).toBeGreaterThan(0);
    expect(changes.every(event => event.actor === 'agent')).toBe(true);
    expect(changes[0].runId).toMatch(/^run-/);
    expect(changes[0].after).toBe('改过的甲');
  });

  it('create 意图落地：一条 generate，nodeCount 是整棵树的节点数', async () => {
    // 整棵树不是 diff，但"这次生成"必须留痕——否则"一字未改"与"从没用过"
    // 在表里是同一件事，而缺的正是那个分母。
    const store = useMindMapStore();
    stubFetch(() => streamWithDone(tree()));

    await store.runGeneration('做一张图', 'create');

    expect(store.pendingEdits).toHaveLength(1);
    expect(store.pendingEdits[0]).toMatchObject({
      actor: 'agent', kind: 'generate', nodeCount: 3,
    });
    expect(store.pendingEdits[0].nodeId).toBeUndefined();
    expect(store.pendingEdits[0].field).toBeUndefined();
    expect(store.pendingEdits[0].runId).toMatch(/^run-/);
  });

  it('expand 意图也记一条 generate，nodeCount 是落地后整棵树的节点数', async () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');
    store.select('a');
    const grown = content(node('root', '主题', [node('a', '甲', [node('b', '乙'), node('a1', '新增')])]));
    stubFetch(() => streamWithDone(grown));

    await store.runGeneration('展开甲', 'expand');

    const generates = store.pendingEdits.filter(event => event.kind === 'generate');
    expect(generates).toHaveLength(1);
    expect(generates[0].actor).toBe('agent');
    // 分母数的是"这次落进画布的那棵树有几个节点"，不是"新加了几个"——
    // 后者只有 add 事件知道，而分母要跨 create / expand / edit 三种意图可比。
    expect(generates[0].nodeCount).toBe(4);
    expect(store.pendingEdits).toHaveLength(2);
    expect(store.pendingEdits[0]).toMatchObject({ actor: 'agent', kind: 'add' });
  });

  it('edit 意图什么都没改：别的没有，但 generate 仍然记一条', async () => {
    // 模型交回来的树一模一样。这也是"用户接受了生成结果"的一次真实观测，
    // 跳过它等于把正样本删掉——后端那条 `untouched_runs` 就是数它的。
    const store = useMindMapStore();
    store.preview(tree(), 'right');
    stubFetch(() => streamWithDone(tree()));

    await store.runGeneration('什么也别改', 'edit');

    expect(store.pendingEdits).toHaveLength(1);
    expect(store.pendingEdits[0]).toMatchObject({
      actor: 'agent', kind: 'generate', nodeCount: 3,
    });
  });

  it('generate 与它带来的改动共享同一个 at', async () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');
    stubFetch(() => streamWithDone(content(node('root', '主题', [node('a', '改过的甲', [node('b', '乙')])]))));

    await store.runGeneration('把甲改个名', 'edit');

    // 同一个 at 是"这次操作产生了哪几条事件"的唯一分组依据（GROUP BY at）。
    expect(new Set(store.pendingEdits.map(event => event.at)).size).toBe(1);
  });

  it('删整支只写一条 remove，subtreeSize 是整支的节点数', () => {
    const store = useMindMapStore();
    store.preview(content(node('root', '主题', [
      node('a', '甲', [node('b', '乙', [node('c', '丙')])]),
    ])), 'right');

    store.deleteNode('a');

    expect(store.pendingEdits).toHaveLength(1);
    expect(store.pendingEdits[0]).toMatchObject({
      actor: 'user', kind: 'remove', nodeId: 'a', depth: 2, subtreeSize: 3,
    });
  });

  it('删一个叶节点：subtreeSize 是 1', () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');

    store.deleteNode('b');

    expect(store.pendingEdits).toHaveLength(1);
    expect(store.pendingEdits[0]).toMatchObject({ kind: 'remove', nodeId: 'b', subtreeSize: 1 });
  });

  it('保存成功后清空', async () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');
    store.commitLabel('a', '新标题');
    stubFetch(() => jsonResponse(RECORD));

    expect(await store.save()).toBe('saved');

    expect(store.pendingEdits).toEqual([]);
    const post = calls.find(call => call.method === 'POST')!;
    // 事件跟内容在同一次请求里：第一次保存前没有 map_id，独立端点根本发不出去。
    expect((post.body.events as unknown[]).length).toBe(1);
    expect(post.body.truncated).toBeUndefined();
  });

  it('保存失败不清空（否则这批事件就丢了）', async () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');
    store.commitLabel('a', '新标题');
    stubFetch(() => jsonResponse({ message: '炸了' }, 500));

    expect(await store.save()).toBe('failed');
    expect(store.pendingEdits).toHaveLength(1);

    // 再存一次就能把它们补上。
    stubFetch(() => jsonResponse(RECORD));
    expect(await store.save()).toBe('saved');
    expect(store.pendingEdits).toEqual([]);
  });

  it('保存期间的新改动留给下一次，不被一起清掉', async () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');
    store.commitLabel('a', '第一次改动');

    let release: () => void = () => {};
    const gate = new Promise<void>(resolve => { release = resolve; });
    stubFetch(async () => {
      await gate;
      return jsonResponse(RECORD);
    });

    const saving = store.save();
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    store.commitLabel('a', '保存期间的改动');
    release();
    expect(await saving).toBe('saved');

    expect(store.pendingEdits).toHaveLength(1);
    expect(store.pendingEdits[0].after).toBe('保存期间的改动');
  });

  it('单次保存最多 500 条，剩下的留到下一次', async () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');
    // 直接填队列：这一条验的是 `save()` 的分批，不是"怎么攒出 521 条"。真改 521 次
    // 会让每次改动都把整棵树序列化进撤销栈，满套件并发跑时会被挤到超时——那个耗时
    // 与被测行为无关。怎么攒出这些事件由上面那些用例各自钉住。
    const total = EDIT_EVENT_LIMIT + 21;
    store.pendingEdits = Array.from({ length: total }, (_, index) => ({
      actor: 'user' as const,
      kind: 'update' as const,
      field: 'label' as const,
      nodeId: `n${index}`,
      before: '甲',
      after: '乙',
      depth: 2,
      at: '2026-11-17T09:31:02.123Z',
    }));

    stubFetch(() => jsonResponse(RECORD));
    await store.save();

    const post = calls.find(call => call.method === 'POST')!;
    expect((post.body.events as unknown[]).length).toBe(EDIT_EVENT_LIMIT);
    // 超出的 21 条不是被丢掉，而是留在队列里等下一次保存——截断是分批，不是丢弃。
    // 它们也不会进表：`total` 必须是真实改动数，插一条"我截断了"的假事件会让每个
    // 统计口径都变脏。
    expect(post.body.truncated).toBe(21);
    expect(store.pendingEdits).toHaveLength(21);

    await store.save();
    expect((calls[calls.length - 1].body.events as unknown[]).length).toBe(21);
    expect(store.pendingEdits).toHaveLength(0);
  });

  it('subtree_size 以 snake_case 进请求体', async () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');
    store.deleteNode('a');
    stubFetch(() => jsonResponse(RECORD));

    await store.save();

    const post = calls.find(call => call.method === 'POST')!;
    expect((post.body.events as Record<string, unknown>[])[0]).toMatchObject({
      kind: 'remove', node_id: 'a', subtree_size: 2,
    });
  });

  it('generate 的 node_count 以 snake_case 进请求体', async () => {
    const store = useMindMapStore();
    stubFetch(() => streamWithDone(tree()));
    await store.runGeneration('做一张图', 'create');

    stubFetch(() => jsonResponse(RECORD));
    await store.save();

    const post = calls.find(call => call.method === 'POST')!;
    // 后端 `EditEventIn` 是 `extra="forbid"`：字段名漂移的表现是保存直接 422，
    // 所以这一条只能靠"请求体里真的叫 node_count"来钉。
    expect((post.body.events as Record<string, unknown>[])[0]).toMatchObject({
      actor: 'agent', kind: 'generate', node_count: 3,
    });
  });

  it('保存成功之后上报"这份结果被存了"', async () => {
    const store = useMindMapStore();
    stubFetch(() => streamWithDone(tree()));
    await store.runGeneration('做一张图', 'create');
    const runId = store.pendingEdits[0].runId!;

    const urls: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      urls.push(String(input));
      return jsonResponse(RECORD);
    }));
    await store.save();

    // `applied_at` 现在的语义是"被保存下来了"：它是"下次进页面还要不要提醒"
    // 的唯一判据（`runs.pending`），所以必须由保存成功这条路打点。
    expect(urls).toContain(`/api/agent/runs/${runId}/applied`);
  });

  it('撤销与重做不产生事件', () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');
    store.commitLabel('a', '新标题');
    const before = store.pendingEdits.length;

    store.undo();
    store.redo();

    // 一次被撤销的编辑不是用户的最终意图：同时记"18→9"和"9→18"会让平均值
    // 被拉回中性、次数却翻倍，比不记更糟。
    expect(store.pendingEdits).toHaveLength(before);
  });

  it('换图把上一张图的事件作废，不会串到新记录上', () => {    const store = useMindMapStore();
    store.preview(tree(), 'right');
    store.commitLabel('a', '新标题');
    expect(store.pendingEdits).toHaveLength(1);

    store.preview(tree(), 'right');

    expect(store.pendingEdits).toEqual([]);
  });

  it('朝向变化不产生事件', () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');

    store.setDirection('left');

    // 朝向写在 content 里，但它回答不了"生成侧该改什么"，记下来只是噪音。
    expect(store.pendingEdits).toEqual([]);
  });
});
