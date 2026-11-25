import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import type { MindMapContent, MindNode } from '../src/api/types';
import {
  DROP_PAD_X,
  DROP_PAD_Y,
  dropHitOf,
  dropSideOf,
  dropZoneOf,
  outwardOf,
} from '../src/canvas/layout/drop-zone';
import { layoutTidy, NODE_SIZE } from '../src/canvas/layout/tidy';
import {
  dropMessage,
  dropPendingLabel,
  planDrop,
  subtreeHeight,
} from '../src/mindmap/drop-plan';
import { findParent, moveNode, reparentAbove, subtreeIds } from '../src/mindmap/edits';
import { useMindMapStore } from '../src/stores/mindmap';

/** 根 + 三支，其中「甲」有两个孩子。所有用例共用这一棵。 */
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
  return { title: '拖动用例', root: tree() };
}

/** 节点盒子：左上角在原点、尺寸与布局共读同一个常量。 */
const BOX = { x: 0, y: 0, width: NODE_SIZE.width, height: NODE_SIZE.height };

beforeEach(() => {
  setActivePinia(createPinia());
});

// ---------------------------------------------------------------- 一、几何

describe('落点几何：指针在节点的哪一侧', () => {
  it('正中心判水平轴，落到右侧（= 子）', () => {
    // 平局规则写死在 `dropHitOf` 里：`|dx| >= |dy|` 且 `dx = 0` → right。
    expect(dropSideOf({ x: 90, y: 24 }, BOX)).toBe('right');
  });

  it('左右两侧各自的中间带判 left / right', () => {
    expect(dropSideOf({ x: 10, y: 24 }, BOX)).toBe('left');
    expect(dropSideOf({ x: 170, y: 24 }, BOX)).toBe('right');
  });

  it('贴着上边缘 / 下边缘的中间带判 top / bottom', () => {
    // 上边缘 `y = 0` 处 `dy = -0.5`；水平偏离 30px 时 `dx = 0.27`，仍属上下。
    expect(dropSideOf({ x: 90, y: 0 }, BOX)).toBe('top');
    expect(dropSideOf({ x: 120, y: 4 }, BOX)).toBe('top');
    expect(dropSideOf({ x: 90, y: 48 }, BOX)).toBe('bottom');
  });

  it('越出命中区就是没命中，而不是"最近的边"', () => {
    // 上下外扩 `DROP_PAD_Y`、左右外扩 `DROP_PAD_X`，各差一像素都算出去。
    expect(dropSideOf({ x: 90, y: -DROP_PAD_Y - 1 }, BOX)).toBeNull();
    expect(dropSideOf({ x: 90, y: NODE_SIZE.height + DROP_PAD_Y + 1 }, BOX)).toBeNull();
    expect(dropSideOf({ x: -DROP_PAD_X - 1, y: 24 }, BOX)).toBeNull();
    expect(dropSideOf({ x: NODE_SIZE.width + DROP_PAD_X + 1, y: 24 }, BOX)).toBeNull();
  });

  it('角落判水平轴——这是"归一化"与"最近边"给出不同答案的地方', () => {
    // 距上边缘只有 6px、距左边缘 20px。按"最近边"会判 top（更贴上面那条边），
    // 按归一化判 left（水平偏离 70/110 > 垂直偏离 18/48）。`dropHitOf` 的注释里
    // 记了为什么取后者：另一条路会让"放到上面"这类手势整体做不出来。
    expect(dropSideOf({ x: 20, y: 6 }, BOX)).toBe('left');
  });

  it('nearness 是"离中心多远"的归一化值，用于重叠时的仲裁', () => {
    const center = dropHitOf({ x: 90, y: 24 }, BOX);
    const edge = dropHitOf({ x: 90, y: 0 }, BOX);
    expect(center?.nearness).toBe(0);
    expect(edge?.nearness).toBeCloseTo(0.5, 5);
    if (!center || !edge) throw new Error('应当命中');
    expect(edge.nearness).toBeGreaterThan(center.nearness);
  });
});

describe('落点几何：屏幕方位 → 结构关系（随朝向翻转）', () => {
  it('向右布局：左父、右子、上前兄弟、下后兄弟', () => {
    expect(dropZoneOf('left', { direction: 'right' })).toBe('parent');
    expect(dropZoneOf('right', { direction: 'right' })).toBe('child');
    expect(dropZoneOf('top', { direction: 'right' })).toBe('sibling-before');
    expect(dropZoneOf('bottom', { direction: 'right' })).toBe('sibling-after');
  });

  it('向左布局是镜像：左子、右父', () => {
    // `direction: 'left'` 时子节点画在父的左边，所以"拖到左边"= 更深一层。
    // 还按"左=父"翻译的话，用户会得到一个与画面完全相反的结果。
    expect(dropZoneOf('left', { direction: 'left' })).toBe('child');
    expect(dropZoneOf('right', { direction: 'left' })).toBe('parent');
    expect(dropZoneOf('top', { direction: 'left' })).toBe('sibling-before');
  });

  it('向下布局：层轴换成 y，兄弟沿 x', () => {
    expect(dropZoneOf('top', { direction: 'down' })).toBe('parent');
    expect(dropZoneOf('bottom', { direction: 'down' })).toBe('child');
    expect(dropZoneOf('left', { direction: 'down' })).toBe('sibling-before');
    expect(dropZoneOf('right', { direction: 'down' })).toBe('sibling-after');
  });

  it('双向布局看目标在根的哪一侧：远离根的一侧是子', () => {
    // 目标在根的右边 → 更右是更深。
    expect(dropZoneOf('right', { direction: 'both', outward: 1 })).toBe('child');
    expect(dropZoneOf('left', { direction: 'both', outward: 1 })).toBe('parent');
    // 目标在根的左边 → 反过来。
    expect(dropZoneOf('left', { direction: 'both', outward: -1 })).toBe('child');
    expect(dropZoneOf('right', { direction: 'both', outward: -1 })).toBe('parent');
    // 目标就是根：两侧都长着子节点，两个方向都是"子"。
    expect(dropZoneOf('left', { direction: 'both', outward: 0 })).toBe('child');
    expect(dropZoneOf('right', { direction: 'both', outward: 0 })).toBe('child');
  });

  it('outwardOf 认得出目标在根的哪边', () => {
    expect(outwardOf(400, 0)).toBe(1);
    expect(outwardOf(-400, 0)).toBe(-1);
    expect(outwardOf(0, 0)).toBe(0);
  });
});

// ---------------------------------------------------------------- 二、结构

describe('落点合法性：四档合法 + 六种拒绝', () => {
  it('拖到右边 = 成为目标的子节点（挂在末尾）', () => {
    const plan = planDrop(tree(), { draggedId: 'b', targetId: 'a', zone: 'child' });
    expect(plan.ok).toBe(true);
    if (!plan.ok) return;
    expect(plan.parentId).toBe('a');
    expect(findParent(plan.root, 'b')?.id).toBe('a');
    // 「甲」原来的两个孩子都还在，新的排在最后。
    const kids = plan.root.children[0].children.map(child => child.id);
    expect(kids).toEqual(['a1', 'a2', 'b']);
  });

  it('拖到左边 = 成为目标的父节点，且顶替它原来的位次', () => {
    const plan = planDrop(tree(), { draggedId: 'c', targetId: 'a', zone: 'parent' });
    expect(plan.ok).toBe(true);
    if (!plan.ok) return;
    // 「丙」变成「甲」的父；「甲」的父仍是根，「丙」插在根的第一个位置（甲原来的位次）。
    const roots = plan.root.children.map(child => child.id);
    expect(roots).toEqual(['c', 'b']);
    const target = plan.root.children[0];
    expect(target.id).toBe('c');
    expect(target.children.map(child => child.id)).toEqual(['a']);
    // 「甲」自己的两个子节点一个都没被改坏。
    expect(target.children[0].children.map(child => child.id)).toEqual(['a1', 'a2']);
  });

  it('拖到上面 / 下面是兄弟：插到目标前 / 后一位', () => {
    const before = planDrop(tree(), { draggedId: 'c', targetId: 'b', zone: 'sibling-before' });
    const after = planDrop(tree(), { draggedId: 'a', targetId: 'b', zone: 'sibling-after' });
    expect(before.ok && before.root.children.map(child => child.id)).toEqual(['a', 'c', 'b']);
    // 「甲」整支搬走，它的三代后代一起跟过去。
    expect(after.ok && after.root.children.map(child => child.id)).toEqual(['b', 'a', 'c']);
  });

  it('拖到自己身上：拒绝', () => {
    const plan = planDrop(tree(), { draggedId: 'a', targetId: 'a', zone: 'child' });
    expect(plan.ok).toBe(false);
    if (plan.ok) return;
    expect(plan.code).toBe('self');
  });

  it('拖到自己的后代身上：拒绝（唯一会成环的一种）', () => {
    for (const target of ['a1', 'a2', 'a21']) {
      const plan = planDrop(tree(), { draggedId: 'a', targetId: target, zone: 'child' });
      expect(plan.ok).toBe(false);
      if (plan.ok) continue;
      expect(plan.code).toBe('descendant');
      expect(plan.reason).toContain('环');
    }
  });

  it('拖到自己的父身上：三档各有各的正确结果，都不是"非法"', () => {
    // 子区 → 已经在末尾的孩子再拖到父的右边，是无变化；不在末尾则是"挪到末尾"，
    // 那是一次真实的位次变化（`zone='child'` 定义落点是 children 的末尾）。
    expect((planDrop(tree(), { draggedId: 'c', targetId: 'root', zone: 'child' }) as { code: string }).code)
      .toBe('no-change');
    const toTail = planDrop(tree(), { draggedId: 'b', targetId: 'root', zone: 'child' });
    expect(toTail.ok).toBe(true);
    if (toTail.ok) expect(toTail.root.children.map(child => child.id)).toEqual(['a', 'c', 'b']);
    // 兄弟区 → 合法的"提升一级"（变成父的兄弟）。
    const up = planDrop(tree(), { draggedId: 'b', targetId: 'a', zone: 'sibling-before' });
    expect(up.ok).toBe(true);
    // 父区 → 合法的"把父顶下去"：目标变成自己的子，自己顶替它的位次。
    const down = planDrop(tree(), { draggedId: 'a1', targetId: 'a', zone: 'parent' });
    expect(down.ok).toBe(true);
    if (!down.ok) return;
    expect(down.root.children[0].id).toBe('a1');
    expect(down.root.children[0].children.map(child => child.id)).toEqual(['a']);
    // 「甲」剩下的那个孩子（甲二 + 甲二一）跟着它一起变成「甲一」的后代，不会丢掉：
    // 甲一、甲、甲二、甲二一 = 4 个。
    expect(subtreeIds(down.root.children[0]).size).toBe(4);
    expect(down.root.children[0].children[0].children.map(child => child.id)).toEqual(['a2']);
  });

  it('根节点不能被拖走，也不能变成别人的子节点或兄弟', () => {
    expect((planDrop(tree(), { draggedId: 'root', targetId: 'a', zone: 'child' }) as { code: string }).code)
      .toBe('root-moved');
    expect((planDrop(tree(), { draggedId: 'a', targetId: 'root', zone: 'parent' }) as { code: string }).code)
      .toBe('root-as-child');
    expect((planDrop(tree(), { draggedId: 'a', targetId: 'root', zone: 'sibling-after' }) as { code: string }).code)
      .toBe('root-as-sibling');
  });

  it('拖到根的右边是合法的：那是"挂成一级分支"', () => {
    const plan = planDrop(tree(), { draggedId: 'a21', targetId: 'root', zone: 'child' });
    expect(plan.ok).toBe(true);
    if (!plan.ok) return;
    expect(plan.root.children.map(child => child.id)).toEqual(['a', 'b', 'c', 'a21']);
  });

  it('已经排在那个位置时不做改动（否则会白记一步撤销）', () => {
    const plan = planDrop(tree(), { draggedId: 'b', targetId: 'a', zone: 'sibling-after' });
    expect(plan.ok).toBe(false);
    if (plan.ok) return;
    expect(plan.code).toBe('no-change');
  });

  it('超过 8 层就拒绝，且报的是拖完之后实际有几层', () => {
    // 主链正好 8 层（r → d1 … d7），旁边一支 `x`（2 层）挂到最深处就成了 9 层。
    const deep: MindNode = { id: 'r', label: '根', children: [] };
    let cursor = deep;
    for (let i = 1; i <= 7; i += 1) {
      const node: MindNode = { id: `d${i}`, label: `第${i}层`, children: [] };
      cursor.children.push(node);
      cursor = node;
    }
    deep.children.push({ id: 'x', label: '旁支', children: [{ id: 'x1', label: '旁支一', children: [] }] });

    // 拖 `x` 到最深的 `d7`（第 8 层）右边 = 成为它的子节点 → `x` 落在第 9 层，
    // 它的孩子 `x1` 在第 10 层。
    const tooDeep = planDrop(deep, { draggedId: 'x', targetId: 'd7', zone: 'child' });
    expect(tooDeep.ok).toBe(false);
    if (tooDeep.ok) return;
    expect(tooDeep.code).toBe('too-deep');
    // 报的是**拖完之后整棵树的深度**（10），不是"被拖节点的层号"（9）：用户要看懂
    // 的是"这一支太长了"，而"第 9 层"听起来只差一层。
    expect(tooDeep.reason).toContain('10 层');
    expect(tooDeep.reason).toContain('8 层');

    // 同一支挂到第 6 层下面（`d5`）：x 在第 7 层、x1 在第 8 层，正好到上限，必须放行。
    const ok = planDrop(deep, { draggedId: 'x', targetId: 'd5', zone: 'child' });
    expect(ok.ok).toBe(true);
  });

  it('subtreeHeight 数的是层数而不是节点数', () => {
    expect(subtreeHeight(tree())).toBe(4);
    expect(subtreeHeight(tree().children[0])).toBe(3);
  });
});

// ---------------------------------------------------------------- 三、纯函数改写

describe('moveNode / reparentAbove：不可变、先摘后插', () => {
  it('moveNode 不碰原树', () => {
    const before = tree();
    const frozen = JSON.stringify(before);
    moveNode(before, 'b', 'a', 0);
    expect(JSON.stringify(before)).toBe(frozen);
  });

  it('同层往上挪一位不会变成"原地不动"（先摘后插）', () => {
    // children = [a, b, c]，把 c 插到下标 0 → [c, a, b]。
    expect(moveNode(tree(), 'c', 'root', 0).children.map(child => child.id))
      .toEqual(['c', 'a', 'b']);
    // 把 a 插到下标 1：摘掉 a 之后是 [b, c]，插到 1 → [b, a, c]。
    expect(moveNode(tree(), 'a', 'root', 1).children.map(child => child.id))
      .toEqual(['b', 'a', 'c']);
  });

  it('下标超界时夹到末尾，不抛错', () => {
    expect(moveNode(tree(), 'b', 'a', 99).children[0].children.map(child => child.id))
      .toEqual(['a1', 'a2', 'b']);
  });

  it('把自己挂进自己的子树会抛错（这里是最后一道网，前面 planDrop 已经拦了）', () => {
    expect(() => moveNode(tree(), 'a', 'a2', 0)).toThrow();
  });

  it('reparentAbove 让目标顶替被挂者的位次', () => {
    const next = reparentAbove(tree(), 'b', 'a');
    expect(next.children.map(child => child.id)).toEqual(['b', 'c']);
    expect(next.children[0].children.map(child => child.id)).toEqual(['a']);
  });
});

// ---------------------------------------------------------------- 四、写回 content

describe('store.moveNodeTo：一次拖动 = 一次可撤销的内容改动', () => {
  function storeWithTree() {
    const store = useMindMapStore();
    store.preview(content(), 'right');
    return store;
  }

  it('改 content、置脏、递增 clientRevision', () => {
    const store = storeWithTree();
    const revision = store.clientRevision;
    expect(store.dirty).toBe(false);

    const result = store.moveNodeTo({ draggedId: 'b', targetId: 'a', zone: 'child' });
    expect(result.ok).toBe(true);
    expect(store.dirty).toBe(true);
    expect(store.clientRevision).toBe(revision + 1);
    expect(store.content?.root.children.map(child => child.id)).toEqual(['a', 'c']);
    expect(store.content?.root.children[0].children.map(child => child.id)).toEqual(['a1', 'a2', 'b']);
  });

  it('一步撤销就能退回原样', () => {
    const store = storeWithTree();
    const before = JSON.stringify(store.content);

    store.moveNodeTo({ draggedId: 'b', targetId: 'a', zone: 'child' });
    expect(store.canUndo).toBe(true);

    store.undo();
    expect(JSON.stringify(store.content)).toBe(before);
  });

  it('进的是账本的 move 事件，不是 update（关系变化有自己的 kind）', () => {
    const store = storeWithTree();
    store.moveNodeTo({ draggedId: 'b', targetId: 'a', zone: 'child' });

    const moves = store.pendingEdits.filter(event => event.kind === 'move' && event.nodeId === 'b');
    // 一条父变化（b 换到 a 下面）+ 一条位次变化（c 从第 3 升到第 2）——
    // 两条都是 `move`，`by_kind` 能统计到，`by_field` 里不会出现它们。
    expect(moves.length).toBeGreaterThanOrEqual(1);
    const reparent = moves.find(event => event.before === '根主题');
    expect(reparent?.after).toBe('甲');
  });

  it('非法落点不写 content、不记历史、也不静默', () => {
    const store = storeWithTree();
    const before = JSON.stringify(store.content);
    const depth = store.undoDepth;

    const result = store.moveNodeTo({ draggedId: 'a', targetId: 'a2', zone: 'child' });
    expect(result.ok).toBe(false);
    expect(result.code).toBe('descendant');
    expect(JSON.stringify(store.content)).toBe(before);
    expect(store.undoDepth).toBe(depth);
    // 拒绝理由必须落到状态栏：拖了一下什么都没发生，与"弹一句为什么"，是两种体验。
    expect(store.status).toContain('环');
  });

  it('非树型不接拖动（本轮只做了树）', () => {
    const store = useMindMapStore();
    store.preview({
      title: '鱼骨',
      diagram_type: 'fishbone',
      bones: [{ id: 'b1', label: '人', root: { id: 'r1', label: '人', children: [] } }],
    } as unknown as MindMapContent, 'right');

    const result = store.moveNodeTo({ draggedId: 'r1', targetId: 'r1', zone: 'child' });
    expect(result.ok).toBe(false);
    expect(result.code).toBe('not-tree');
  });
});

// ---------------------------------------------------------------- 五、子节点跟随

describe('子节点跟随：拖完落点是"新树重排的结果"，不是"旧位置 + 位移"', () => {
  it('被拖节点的所有后代都落在 layoutTidy(新树) 给的位置上', () => {
    const store = useMindMapStore();
    store.preview(content(), 'right');

    // 拖动之前，记下「甲」这一支每一个节点的坐标。
    const draggedSubtree = ['a', 'a1', 'a2', 'a21'];
    const before = new Map(
      store.nodes.filter(node => draggedSubtree.includes(node.id)).map(node => [node.id, { ...node.position }]),
    );

    store.moveNodeTo({ draggedId: 'a', targetId: 'c', zone: 'child' });
    const next = store.content!;
    expect(findParent(next.root, 'a')?.id).toBe('c');

    // 判据照抄上一轮验证布局的那一条：逐节点比对 `store.nodes` 的坐标与
    // `layoutTidy(新树)` 给的中心点（减去半个节点 = 左上角）。
    const geometry = layoutTidy(next.root, { direction: 'right' });
    for (const id of draggedSubtree) {
      const rendered = store.nodes.find(node => node.id === id);
      const center = geometry.positions.get(id);
      expect(rendered, id).toBeTruthy();
      expect(center, id).toBeTruthy();
      expect(rendered!.position.x, `${id}.x`).toBeCloseTo(center!.x - NODE_SIZE.width / 2, 5);
      expect(rendered!.position.y, `${id}.y`).toBeCloseTo(center!.y - NODE_SIZE.height / 2, 5);
    }

    // 而且**确实动了**：整支 4 个节点没有一个是原坐标。"位移 + 旧位置"那种实现
    // 会让父子相对关系仍然旧——它与"重排"的差别只有这一句能钉出来。
    for (const id of draggedSubtree) {
      const was = before.get(id)!;
      const now = store.nodes.find(node => node.id === id)!.position;
      expect(now.x === was.x && now.y === was.y, `${id} 应当换位置`).toBe(false);
    }
  });

  it('子节点数量与内容一致：重排不会丢节点', () => {
    const store = useMindMapStore();
    store.preview(content(), 'right');
    const total = store.nodeCount;

    store.moveNodeTo({ draggedId: 'a2', targetId: 'b', zone: 'child' });
    expect(store.nodeCount).toBe(total);
    // 「甲二」带着它的孩子「甲二一」一起搬到「乙」下面。
    const b = store.content!.root.children.find(child => child.id === 'b')!;
    expect(b.children.map(child => child.id)).toEqual(['a2']);
    expect(b.children[0].children.map(child => child.id)).toEqual(['a21']);
  });
});

// ---------------------------------------------------------------- 六、文案

describe('落点文案：拖的时候和松手之后说的是同一件事', () => {
  it('四档的将来式互不相同', () => {
    const says = (['child', 'parent', 'sibling-before', 'sibling-after'] as const)
      .map(zone => dropPendingLabel('甲', zone));
    expect(new Set(says).size).toBe(4);
    expect(says[0]).toContain('子节点');
    expect(says[1]).toContain('父节点');
    expect(says[2]).toContain('前面');
    expect(says[3]).toContain('后面');
  });

  it('松手之后的过去式也按档区分，并带上"可撤销"', () => {
    const root = tree();
    const intent = { draggedId: 'b', targetId: 'a', zone: 'child' as const };
    const plan = planDrop(root, intent);
    expect(plan.ok).toBe(true);
    if (!plan.ok) return;
    const message = dropMessage(root, intent, plan);
    expect(message).toContain('子节点');
    expect(message).toContain('可撤销');
  });
});
