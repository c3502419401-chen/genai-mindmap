import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import type { MindMapContent, MindNode } from '../src/api/types';
import { layoutTidy, NODE_SIZE } from '../src/canvas/layout/tidy';
import {
  detachMessage,
  detachPendingLabel,
  insertMessage,
  planDetach,
  planInsert,
} from '../src/mindmap/drop-plan';
import {
  addChildAt,
  detachToRoot,
  findNode,
  LIMITS,
  validateDraft,
} from '../src/mindmap/edits';
import { useMindMapStore } from '../src/stores/mindmap';
import { insertIntentAt } from '../src/canvas/layout/insert-zone';
import { DROP_PAD_X, DROP_PAD_Y } from '../src/canvas/layout/drop-zone';

/**
 * 第二轮的三个动作：**断关系 / 删除 / 新增节点**。
 *
 * 三件事各自最怕的东西不一样，所以用例分组也按它们分：
 *
 *  - **断关系**怕"以为是断开、实际是悄悄重排"（一级分支被挪到末尾）；
 *  - **删除**怕"以为只删了一个、实际没了一整支而没人说"；
 *  - **新增**怕"落点读不出来"（在哪儿双击就长在哪儿？布局给的坐标又是什么？）。
 *
 * 共同的判据只有一条：这三件都是**内容改动**，所以它们必须进撤销栈、置脏、记账，
 * 而且每一步 `undo()` 必须把树逐节点还原。
 */

/** 根 + 三支，其中「甲」有两个孩子（「甲二」再带一个）。与 `node-drag.spec.ts` 同一棵。 */
function tree(): MindNode {
  return {
    id: 'root',
    label: '根主题',
    children: [
      {
        id: 'a',
        label: '甲',
        children: [
          { id: 'a1', label: '甲一', children: [] },
          {
            id: 'a2',
            label: '甲二',
            children: [{ id: 'a21', label: '甲二一', children: [] }],
          },
        ],
      },
      { id: 'b', label: '乙', children: [] },
      { id: 'c', label: '丙', children: [] },
    ],
  };
}

function content(): MindMapContent {
  return { title: '第二轮用例', root: tree() };
}

/** 一棵 8 层的直链：r → d1 … d7。用来量"最深几层"。 */
function deepChain(): MindNode {
  const root: MindNode = { id: 'r', label: '根', children: [] };
  let cursor = root;
  for (let i = 1; i <= 7; i += 1) {
    const node: MindNode = { id: `d${i}`, label: `第${i}层`, children: [] };
    cursor.children.push(node);
    cursor = node;
  }
  return root;
}

function ids(root: MindNode, out: string[] = []): string[] {
  out.push(root.id);
  for (const child of root.children) ids(child, out);
  return out;
}

beforeEach(() => {
  setActivePinia(createPinia());
});

// ================================================================ 一、断关系

describe('断关系：拖到空白处松手，把节点从父节点下面摘下来', () => {
  it('挂到根的孩子列表末尾，成为一级分支', () => {
    const before = tree();
    const plan = planDetach(before, 'a2');
    expect(plan.ok).toBe(true);
    if (!plan.ok) return;

    // 一级分支从 3 条变 4 条，新的一条在**末尾**：它在根下面本来就没有"原来的位次"。
    expect(plan.root.children.map(child => child.id)).toEqual(['a', 'b', 'c', 'a2']);
    expect(plan.index).toBe(3);
    // 原来那个父节点少了一个孩子。
    expect(findNode(plan.root, 'a')!.children.map(child => child.id)).toEqual(['a1']);
    // 整支跟着走：子孙一个都不许丢。
    expect(findNode(plan.root, 'a2')!.children.map(child => child.id)).toEqual(['a21']);
    expect(plan.formerParentId).toBe('a');
  });

  it('不碰原树（不可变）', () => {
    const before = tree();
    const frozen = JSON.stringify(before);
    planDetach(before, 'a2');
    expect(JSON.stringify(before)).toBe(frozen);
  });

  it('已经是一级分支时拒绝，而不是"悄悄把它挪到末尾"', () => {
    // 这一条是这一档最关键的判据。`c` 就在根下面、但不在末尾——如果实现成"重排到
    // 末尾"，用户拖到空白处会看到一次没有任何预览、也没人要过的重排。手势里读不出
    // "我要排到最后一位"，所以只能拒绝。
    const plan = planDetach(tree(), 'c');
    expect(plan.ok).toBe(false);
    if (plan.ok) return;
    expect(plan.code).toBe('already-branch');
    expect(plan.reason).toContain('一级分支');
  });

  it('中心主题拒绝：它本来就没有父节点', () => {
    const plan = planDetach(tree(), 'root');
    expect(plan.ok).toBe(false);
    if (plan.ok) return;
    expect(plan.code).toBe('root-moved');
  });

  it('节点不存在时拒绝（预览里的派生 id 会走到这里）', () => {
    const plan = planDetach(tree(), 'preview-0-1');
    expect(plan.ok).toBe(false);
    if (plan.ok) return;
    expect(plan.code).toBe('missing');
  });

  it('断开只让树变浅，所以没有"太深"这一档拒绝', () => {
    // 8 层直链里的第 5 层（身后还有 3 层）。断开之后它落在第 2 层，整树最深 4 层。
    const deep = deepChain();
    const plan = planDetach(deep, 'd5');
    expect(plan.ok).toBe(true);
    if (!plan.ok) return;
    const depthOf = (node: MindNode): number => (
      1 + Math.max(0, ...node.children.map(depthOf))
    );
    expect(depthOf(plan.root)).toBeLessThan(8);
    expect(plan.root.children[plan.root.children.length - 1].id).toBe('d5');
  });

  it('断开之后一级分支数会变，但不触发任何校验（MIN_BRANCHES 是生成侧的门槛）', () => {
    // 判据的出处：`docs/AGENT_PLAN.md` 的 0b 行——`MindMapContent.validate_tree()`
    // **不加**这条质量门槛，"旧图与人工删支仍合法"；`MIN_BRANCHES=3` 只在
    // `generation/service.py::validate_generated_content(enforce_min_branches=True)`
    // 这条**模型整图生成**的路上生效。所以人工断关系既不查下限、也不查上限（除契约
    // 的 `children ≤ 50`）。
    const plan = planDetach(tree(), 'a2');
    expect(plan.ok).toBe(true);
    if (!plan.ok) return;
    expect(plan.root.children.length).toBe(4);
    expect(validateDraft({ ...content(), root: plan.root })).toBeNull();

    // 反过来：断到只剩 1 条一级分支也得存得下去（用户有权把图整理成一条链）。
    const single = planDetach(
      { id: 'root', label: '根', children: [{ id: 'p', label: '父', children: [{ id: 'k', label: '子', children: [] }] }] },
      'k',
    );
    expect(single.ok).toBe(true);
    if (!single.ok) return;
    expect(validateDraft({ title: '一条链', root: single.root })).toBeNull();
  });

  it('文案说清"从谁下面断开"，因为空白处没有视觉锚点', () => {
    const plan = planDetach(tree(), 'a2');
    if (!plan.ok) throw new Error('应当允许断开');
    const message = detachMessage(tree(), 'a2', plan.formerParentId, plan);
    expect(message).toContain('甲二');
    expect(message).toContain('甲');
    expect(message).toContain('一级分支');
    expect(message).toContain('可撤销');

    expect(detachPendingLabel('甲二', '甲')).toContain('断开');
    expect(detachPendingLabel('甲二', '甲')).toContain('一级分支');
  });

  it('detachToRoot 的两个前置条件都抛错（调用方判据出洞的最后一道网）', () => {
    expect(() => detachToRoot(tree(), 'root')).toThrow();
    expect(() => detachToRoot(tree(), 'nope')).toThrow();
  });
});

describe('store.detachNode：一次断开 = 一次可撤销的内容改动', () => {
  function storeWithTree() {
    const store = useMindMapStore();
    store.preview(content(), 'right');
    return store;
  }

  it('改 content、置脏、递增 clientRevision，并选中被断开的那个节点', () => {
    const store = storeWithTree();
    const revision = store.clientRevision;
    expect(store.dirty).toBe(false);

    const result = store.detachNode('a2');
    expect(result.ok).toBe(true);
    expect(store.dirty).toBe(true);
    expect(store.clientRevision).toBe(revision + 1);
    expect(store.content!.root.children.map(child => child.id)).toEqual(['a', 'b', 'c', 'a2']);
    expect(store.selectedId).toBe('a2');
    expect(result.detachedFrom).toBe('a');
  });

  it('一步撤销就把整支原样还回去', () => {
    const store = storeWithTree();
    const before = JSON.stringify(store.content);
    store.detachNode('a2');
    expect(store.canUndo).toBe(true);
    store.undo();
    expect(JSON.stringify(store.content)).toBe(before);
  });

  it('落点是"新树重排的结果"，不是手拖的位置（Node.pos 仍然没有写入口径）', () => {
    const store = storeWithTree();
    store.detachNode('a2');
    const next = store.content!;
    const geometry = layoutTidy(next.root, { direction: 'right' });
    for (const id of ['a2', 'a21']) {
      const rendered = store.nodes.find(node => node.id === id)!;
      const center = geometry.positions.get(id)!;
      expect(rendered.position.x, `${id}.x`).toBeCloseTo(center.x - NODE_SIZE.width / 2, 5);
      expect(rendered.position.y, `${id}.y`).toBeCloseTo(center.y - NODE_SIZE.height / 2, 5);
    }
    expect(findNode(next.root, 'a2')!.pos).toBeUndefined();
  });

  it('非法（已经是一级分支）不写 content、不记历史，但状态栏要说原因', () => {
    const store = storeWithTree();
    const before = JSON.stringify(store.content);
    const depth = store.undoDepth;

    const result = store.detachNode('b');
    expect(result.ok).toBe(false);
    expect(result.code).toBe('already-branch');
    expect(JSON.stringify(store.content)).toBe(before);
    expect(store.undoDepth).toBe(depth);
    expect(store.status).toContain('一级分支');
  });

  it('账本里是 move（父变了），不是 update', () => {
    const store = storeWithTree();
    store.detachNode('a2');
    const moves = store.pendingEdits.filter(event => event.kind === 'move' && event.nodeId === 'a2');
    expect(moves.length).toBeGreaterThanOrEqual(1);
    const reparent = moves.find(event => event.before === '甲');
    expect(reparent?.after).toBe('根主题');
  });

  it('previewDetach 给出的矩形就是落地后的真实位置', () => {
    const store = storeWithTree();
    const preview = store.previewDetach('a2');
    expect(preview.ok).toBe(true);
    const after = store.detachNode('a2');
    expect(after.ok).toBe(true);
    const rendered = store.nodes.find(node => node.id === 'a2')!;
    // 亚像素容差：预演与落地走同一个 `layoutTidy`，所以两者应当逐像素相同。
    expect(preview.rect!.x).toBeCloseTo(rendered.position.x, 5);
    expect(preview.rect!.y).toBeCloseTo(rendered.position.y, 5);
  });
});

// ================================================================ 二、删除

describe('删除节点：整支一起走，但一步撤销能原样回来', () => {
  function storeWithTree() {
    const store = useMindMapStore();
    store.preview(content(), 'right');
    return store;
  }

  it('删父节点 = 连它的全部子孙一起删（这是画面上看到的那件事）', () => {
    // 三档候选（一起删 / 子节点上提到父 / 只许删叶子）里选第一档，判据：
    //   - 画布上「甲」是**一个**盒子，它的子节点缩在里面；用户点"删"时心里删的就是
    //     那一整块。上提会让"删一个节点"变成"20 个节点同时换了父"，而他没点过那个；
    //   - "只许删叶子"把删除变成一件要先自底向上扫一遍的活，而这是一棵会长的树；
    //   - "整支一起删"的代价（丢内容）由两步兜住：状态栏明说"已删除该节点及其子节点"，
    //     并且一步撤销原样还回来。
    const store = storeWithTree();
    expect(store.nodeCount).toBe(7);

    store.deleteNode('a');

    expect(store.content!.root.children.map(child => child.id)).toEqual(['b', 'c']);
    for (const gone of ['a', 'a1', 'a2', 'a21']) {
      expect(findNode(store.content!.root, gone), gone).toBeNull();
    }
    expect(store.nodeCount).toBe(3);
    // "悄悄丢数据"与"明说丢了"的差别就在这一句。
    expect(store.status).toContain('及其子节点');
    expect(store.status).toContain('可撤销');
  });

  it('一步撤销把整支按原顺序还回来（数据没有真的丢）', () => {
    const store = storeWithTree();
    const before = JSON.stringify(store.content);
    const beforeIds = ids(store.content!.root);

    store.deleteNode('a');
    expect(store.canUndo).toBe(true);
    store.undo();

    expect(JSON.stringify(store.content)).toBe(before);
    expect(ids(store.content!.root)).toEqual(beforeIds);
  });

  it('删叶子只少一个节点', () => {
    const store = storeWithTree();
    store.deleteNode('a21');
    expect(store.nodeCount).toBe(6);
    expect(findNode(store.content!.root, 'a2')!.children).toEqual([]);
  });

  it('连带删边与关系，否则下一次保存 422', () => {
    const store = storeWithTree();
    // 造两条悬空源：一条附加边（a → a21）与一条跨节点关系（a ↔ b）。两者都挂在
    // 即将被删的那一支上。
    store.connectNodes('a', 'a21');
    store.connectNodes('a', 'b');
    expect(store.contentEdges.length).toBeGreaterThan(0);

    store.deleteNode('a');

    const dangling = store.contentEdges.filter(
      edge => edge.source === 'a' || edge.target === 'a'
        || edge.source === 'a21' || edge.target === 'a21',
    );
    expect(dangling).toEqual([]);
    // 校验器是最终的判据：保存前 `validateDraft` 必须过（它逐条对齐后端契约）。
    expect(validateDraft(store.content!)).toBeNull();
  });

  it('中心主题删不得，content 一个字节都不动', () => {
    const store = storeWithTree();
    const before = JSON.stringify(store.content);
    const depth = store.undoDepth;

    store.deleteNode('root');

    expect(JSON.stringify(store.content)).toBe(before);
    expect(store.undoDepth).toBe(depth);
    expect(store.status).toContain('根节点不能删除');
  });

  it('删除进撤销栈，而且账本记的是 remove + 整支大小', () => {
    const store = storeWithTree();
    const depth = store.undoDepth;
    store.deleteNode('a2');

    expect(store.undoDepth).toBe(depth + 1);
    const removed = store.pendingEdits.filter(event => event.kind === 'remove');
    expect(removed.map(event => event.nodeId)).toEqual(['a2']);
    // 子树大小是这一条事件的核心：删一个叶子和删一整支在统计里必须分得开。
    expect(removed[0].subtreeSize).toBe(2);
  });

  it('删一个不存在的 id 不动账本（预览的派生 id 会走到这里）', () => {
    const store = storeWithTree();
    const depth = store.undoDepth;
    const revision = store.clientRevision;

    store.deleteNode('preview-0-1');

    expect(store.undoDepth).toBe(depth);
    expect(store.clientRevision).toBe(revision);
  });
});

// ================================================================ 三、新增节点

describe('planInsert：双击落点 → 挂到谁下面、排第几', () => {
  it('落在某个节点上（child）→ 挂成它的最后一个孩子', () => {
    const plan = planInsert(tree(), { targetId: 'a', zone: 'child' });
    expect(plan.ok).toBe(true);
    if (!plan.ok) return;
    expect(plan.parentId).toBe('a');
    expect(plan.index).toBe(2);
    expect(plan.degraded).toBeNull();
  });

  it('落在节点上边 → 插成它前面的兄弟', () => {
    const plan = planInsert(tree(), { targetId: 'b', zone: 'sibling-before' });
    if (!plan.ok) throw new Error('应当允许');
    expect(plan.parentId).toBe('root');
    expect(plan.index).toBe(1);
    expect(plan.zone).toBe('sibling-before');
  });

  it('落在节点下边 → 插成它后面的兄弟', () => {
    const plan = planInsert(tree(), { targetId: 'b', zone: 'sibling-after' });
    if (!plan.ok) throw new Error('应当允许');
    expect(plan.parentId).toBe('root');
    expect(plan.index).toBe(2);
  });

  it('"我要当它的父"降级成"排在它前面当兄弟"，并且说明理由', () => {
    // 一个**尚未存在**的节点没法收养 target 并把它降一层——那是两次结构改动，而用户
    // 做的手势只是"在这里加一个"。所以降级，而且降级必须说出来。
    const plan = planInsert(tree(), { targetId: 'a', zone: 'parent' });
    if (!plan.ok) throw new Error('应当允许（降级）');
    expect(plan.zone).toBe('sibling-before');
    expect(plan.parentId).toBe('root');
    expect(plan.index).toBe(0);
    expect(plan.degraded).toContain('兄弟');
  });

  it('目标就是根时：`parent` → 一级分支，`sibling-*` → 一级分支', () => {
    const asChild = planInsert(tree(), { targetId: 'root', zone: 'parent' });
    if (!asChild.ok) throw new Error('应当允许');
    expect(asChild.zone).toBe('child');
    expect(asChild.parentId).toBe('root');
    expect(asChild.degraded).toContain('一级分支');

    const asSibling = planInsert(tree(), { targetId: 'root', zone: 'sibling-after' });
    if (!asSibling.ok) throw new Error('应当允许');
    expect(asSibling.zone).toBe('child');
  });

  it('第 8 层下面加第 9 层：拒绝，并报它会是第几层', () => {
    const plan = planInsert(deepChain(), { targetId: 'd7', zone: 'child' });
    expect(plan.ok).toBe(false);
    if (plan.ok) return;
    expect(plan.code).toBe('too-deep');
    expect(plan.reason).toContain('9 层');
    expect(plan.reason).toContain('8 层');

    // 第 7 层下面加正好到 8：放行。
    expect(planInsert(deepChain(), { targetId: 'd6', zone: 'child' }).ok).toBe(true);
  });

  it('父节点孩子满了就拒绝（契约硬上限）', () => {
    const full: MindNode = {
      id: 'root',
      label: '根',
      children: Array.from({ length: LIMITS.children }, (_, index) => ({
        id: `k${index}`,
        label: `孩子${index}`,
        children: [],
      })),
    };
    const plan = planInsert(full, { targetId: 'root', zone: 'child' });
    expect(plan.ok).toBe(false);
    if (plan.ok) return;
    expect(plan.code).toBe('full');
    expect(plan.reason).toContain(String(LIMITS.children));
  });

  it('目标不存在时拒绝', () => {
    const plan = planInsert(tree(), { targetId: 'preview-0-1', zone: 'child' });
    expect(plan.ok).toBe(false);
    if (plan.ok) return;
    expect(plan.code).toBe('missing');
  });

  it('四档措辞互不相同（用户要能从状态栏确认自己刚才做的是哪一种）', () => {
    const child = planInsert(tree(), { targetId: 'a', zone: 'child' });
    const before = planInsert(tree(), { targetId: 'b', zone: 'sibling-before' });
    const after = planInsert(tree(), { targetId: 'b', zone: 'sibling-after' });
    if (!child.ok || !before.ok || !after.ok) throw new Error('应当都允许');
    const texts = [
      insertMessage('甲', child),
      insertMessage('乙', before),
      insertMessage('乙', after),
    ];
    expect(new Set(texts).size).toBe(3);
    expect(texts[0]).toContain('子节点');
    expect(texts[1]).toContain('前面');
    expect(texts[2]).toContain('后面');
  });
});

describe('addChildAt：带下标的插入', () => {
  it('插在指定位置，不碰后面的兄弟', () => {
    const { root, node } = addChildAt(tree(), 'root', 1, '新的');
    expect(root.children.map(child => child.id)).toEqual(['a', node.id, 'b', 'c']);
    expect(root.children[1].label).toBe('新的');
  });

  it('下标越界时夹到末尾（最坏是一个合法结果，不是崩）', () => {
    const { root } = addChildAt(tree(), 'root', 99, '新的');
    expect(root.children).toHaveLength(4);
    expect(root.children[root.children.length - 1].label).toBe('新的');
    const negative = addChildAt(tree(), 'root', -5, '新的');
    expect(negative.root.children[0].label).toBe('新的');
  });

  it('父节点不存在时抛错（调用方的判据出了洞）', () => {
    expect(() => addChildAt(tree(), 'nope', 0)).toThrow();
  });
});

describe('store.addNodeAt：双击空白 = 在布局给的位置长一个节点', () => {
  function storeWithTree() {
    const store = useMindMapStore();
    store.preview(content(), 'right');
    return store;
  }

  it('新节点的初始形状：label 是「新节点」、无正文、无引用', () => {
    const store = storeWithTree();
    const result = store.addNodeAt({ targetId: 'a', zone: 'child' });
    expect(result.ok).toBe(true);

    const added = findNode(store.content!.root, store.selectedId!)!;
    // 空串会让保存直接 422（契约 `Node.label` 非空），而"新节点"这三个字在编辑框里
    // 是全选的——用户按下去打第一个字就覆盖掉它。
    expect(added.label).toBe('新节点');
    expect(added.children).toEqual([]);
    expect(added.detail ?? null).toBeNull();
    expect(added.note ?? null).toBeNull();
    expect(added.source_refs ?? []).toEqual([]);
  });

  it('无引用是合法的：`validateDraft` 不拦，`verify_source_refs` 只报提示不删节点', () => {
    const store = storeWithTree();
    store.addNodeAt({ targetId: 'a', zone: 'child' });
    // 收尾校验那一侧的依据写在 `backend/app/optimization/source_refs.py`：没有引用的
    // 节点只产出一条 `UNSOURCED_NODE` warn（"不代表节点内容错误"），节点留着。
    // 前端这一层能钉的是：契约校验不会因为它没有引用而拒绝保存。
    expect(validateDraft(store.content!)).toBeNull();
  });

  it('位置由 planInsert 决定，坐标由布局决定（不是双击的那个像素）', () => {
    const store = storeWithTree();
    store.addNodeAt({ targetId: 'b', zone: 'sibling-after' });
    const next = store.content!;
    const addedId = store.selectedId!;

    // 结构：落在「乙」后面。
    expect(next.root.children.map(child => child.id))
      .toEqual(['a', 'b', addedId, 'c']);
    // 坐标：与 `layoutTidy(新树)` 逐像素相同。
    const geometry = layoutTidy(next.root, { direction: 'right' });
    const rendered = store.nodes.find(node => node.id === addedId)!;
    const center = geometry.positions.get(addedId)!;
    expect(rendered.position.x).toBeCloseTo(center.x - NODE_SIZE.width / 2, 5);
    expect(rendered.position.y).toBeCloseTo(center.y - NODE_SIZE.height / 2, 5);
    expect(findNode(next.root, addedId)!.pos).toBeUndefined();
  });

  it('加完立刻进入编辑态并选中它（否则用户看不到新节点已经在那儿）', () => {
    const store = storeWithTree();
    store.addNodeAt({ targetId: 'a', zone: 'child' });
    expect(store.editingId).toBe(store.selectedId);
    expect(store.status).toContain('子节点');
  });

  it('一步撤销就退回原样', () => {
    const store = storeWithTree();
    const before = JSON.stringify(store.content);
    store.addNodeAt({ targetId: 'a', zone: 'child' });
    expect(store.canUndo).toBe(true);
    store.undo();
    expect(JSON.stringify(store.content)).toBe(before);
  });

  it('拒绝时不写 content、不记历史，理由进状态栏', () => {
    const store = storeWithTree();
    const before = JSON.stringify(store.content);
    const depth = store.undoDepth;

    const result = store.addNodeAt({ targetId: 'preview-0-1', zone: 'child' });

    expect(result.ok).toBe(false);
    expect(JSON.stringify(store.content)).toBe(before);
    expect(store.undoDepth).toBe(depth);
    expect(store.status).toBeTruthy();
  });

  it('账本记的是 add，并把父节点 id 带上', () => {
    const store = storeWithTree();
    store.addNodeAt({ targetId: 'a', zone: 'child' });
    const added = store.pendingEdits.filter(event => event.kind === 'add');
    expect(added).toHaveLength(1);
    expect(added[0].nodeId).toBe(store.selectedId);
    expect(added[0].parentId).toBe('a');
    // actor 是分水岭：手动加的节点与模型加的节点在账本里本来就能分开。
    expect(added[0].actor).toBe('user');
  });

  it('空画布上加不了（没有根就没有"谁的孩子"）', () => {
    const store = useMindMapStore();
    const result = store.addNodeAt({ targetId: 'root', zone: 'child' });
    expect(result.ok).toBe(false);
    expect(store.content).toBeNull();
  });
});

// ================================================================ 四、双击落点的几何

describe('insertIntentAt：双击落哪儿 → 挂到谁下面、算哪种关系', () => {
  /** 一个 180×48 的盒子，左上角在 (0,0)。命中区是它的四边各外扩 PAD 之后的椭圆。 */
  const BOX = { id: 'n', rect: { x: 0, y: 0, width: NODE_SIZE.width, height: NODE_SIZE.height } };

  it('落在命中区里：右侧判子节点', () => {
    const intent = insertIntentAt({ x: 150, y: 24 }, [BOX], { direction: 'right' });
    expect(intent).toEqual({ targetId: 'n', zone: 'child' });
  });

  it('落在命中区里：上侧判前兄弟（与拖动共用同一张方位表）', () => {
    // y = -20 时 dy = -44/48 = -0.92，仍在命中区（|dy| < 1）；再往外就出区了。
    const intent = insertIntentAt({ x: 90, y: -20 }, [BOX], { direction: 'right' });
    expect(intent).toEqual({ targetId: 'n', zone: 'sibling-before' });
  });

  it('真的在空白处：固定挂成最近那个节点的子节点，不按方位猜', () => {
    const intent = insertIntentAt({ x: 400, y: 400 }, [BOX], { direction: 'right' });
    expect(intent).toEqual({ targetId: 'n', zone: 'child' });
  });

  it('"最近的"用归一化度量，不是欧氏距离——两者在这里给出不同答案', () => {
    // A 在正上方（中心 (90, 24)），B 在右下方（中心 (590, 124)）。指针在 (100, 300)。
    //
    //   欧氏：到 A ≈ 276，到 B ≈ 522 → 选 A（"正上方那个"）；
    //   归一化：A 的 dy = 276/48 = 5.75；B 的 dx = 490/110 = 4.45 → 选 B。
    //
    // 取后者：思维导图里一个兄弟间距是 68px、一层是 236px，所以"4.45 个盒宽"比
    // "5.75 个盒高"更近——按层数与位次数的是同一件事。纯像素距离会让水平方向重
    // 近四倍（盒子是 180×48），于是右上角双击会挂到右边很远的节点下面。
    const a = { id: 'a', rect: { x: 0, y: 0, width: NODE_SIZE.width, height: NODE_SIZE.height } };
    const b = { id: 'b', rect: { x: 500, y: 100, width: NODE_SIZE.width, height: NODE_SIZE.height } };
    expect(insertIntentAt({ x: 100, y: 300 }, [a, b], { direction: 'right' }))
      .toEqual({ targetId: 'b', zone: 'child' });
  });

  it('画布上没有可挂的节点时返回 null（空画布双击不动作）', () => {
    expect(insertIntentAt({ x: 10, y: 10 }, [], { direction: 'right' })).toBeNull();
  });

  it('双向布局里按"目标在根的哪一侧"翻译左右两侧', () => {
    const root = { id: 'r', rect: { x: 0, y: 0, width: NODE_SIZE.width, height: NODE_SIZE.height } };
    const right = { id: 'r1', rect: { x: 300, y: -100, width: NODE_SIZE.width, height: NODE_SIZE.height } };
    // 指针在 r1 的右侧 → 更远 = 更深 = 子节点。
    expect(insertIntentAt({ x: 480, y: -76 }, [root, right], { direction: 'both', rootId: 'r' }))
      .toEqual({ targetId: 'r1', zone: 'child' });

    const left = { id: 'l1', rect: { x: -300, y: -100, width: NODE_SIZE.width, height: NODE_SIZE.height } };
    // 指针在 l1 的左侧 → 同样是"更远"，也是子节点（两侧都是层）。
    expect(insertIntentAt({ x: -480, y: -76 }, [root, left], { direction: 'both', rootId: 'r' }))
      .toEqual({ targetId: 'l1', zone: 'child' });
  });

  it('命中区的边界与拖动那套 PAD 完全一致', () => {
    // 边界内侧一像素：命中，按方位判（上 → 前兄弟）。
    expect(insertIntentAt({ x: 90, y: -DROP_PAD_Y + 1 }, [BOX], { direction: 'right' })?.zone)
      .toBe('sibling-before');
    // 边界外侧一像素：不命中，落到"最近节点"那条路，那里 zone 恒为 child——方位在
    // 空白处不参与判断。两个手势共用同一份 PAD，所以这条边界不存在第二种说法。
    expect(insertIntentAt({ x: 90, y: -DROP_PAD_Y - 1 }, [BOX], { direction: 'right' })?.zone)
      .toBe('child');
    // 水平方向同理：外扩 PAD_X 之内算命中（右 → 子节点）。
    expect(insertIntentAt({ x: NODE_SIZE.width + DROP_PAD_X - 1, y: 24 }, [BOX], { direction: 'right' })?.zone)
      .toBe('child');
  });
});
