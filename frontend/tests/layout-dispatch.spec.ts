import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import type { DiagramType, MindEdge, MindMapContent, MindNode } from '../src/api/types';
import { diagramTypeOf, layoutDiagram } from '../src/canvas/layout';
import { layoutFishbone } from '../src/canvas/layout/fishbone';
import { layoutFlow } from '../src/canvas/layout/flow';
import { layoutTidy, NODE_SIZE, type Direction } from '../src/canvas/layout/tidy';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * 图型 → 布局的分派，以及"树布局一个像素都不许动"。
 *
 * 这一组用例守的是三条边界：
 *  1. `tree` 分支与直接调 `layoutTidy` **逐值相等**（不是"看起来差不多"）；
 *  2. `fishbone` / `flowchart` 真的换了算法（坐标等于各自的布局函数）；
 *  3. 连线跟着换：鱼骨用折线渲染器、流程图的回退边绕左侧、树的连线保持原样。
 */

const leaf = (id: string, label: string): MindNode => ({ id, label, children: [] });

/** 一棵有深度的树：四个层级 + 三个一级分支。 */
function tree(): MindNode {
  return {
    id: 'root',
    label: '根',
    children: [
      {
        id: 'a', label: '甲',
        children: [leaf('a1', '甲一'), leaf('a2', '甲二')],
      },
      { id: 'b', label: '乙', children: [leaf('b1', '乙一')] },
      { id: 'c', label: '丙', children: [] },
    ],
  };
}

/** 六类鱼骨。 */
function fishboneTree(): MindNode {
  return {
    id: 'root',
    label: '交付延期',
    children: ['人', '机', '料', '法', '环', '测'].map(name => ({
      id: name,
      label: name,
      children: [leaf(`${name}1`, `${name}原因一`), leaf(`${name}2`, `${name}原因二`)],
    })),
  };
}

/** 三阶段流程 + 条件分支 + 驳回（与后端 `FLOW_PLAN_PROMPT` 的示例同构）。 */
function flowContent(): MindMapContent {
  return {
    title: '报销流程',
    diagram_type: 'flowchart',
    root: {
      id: 'root',
      label: '报销',
      children: [leaf('a', '提交申请'), leaf('b', '形式审查'), leaf('c', '付款')],
    },
    edges: [
      { id: 'e0', source: 'root', target: 'a', kind: 'flow' },
      { id: 'e1', source: 'b', target: 'c', label: '是', kind: 'branch' },
      { id: 'e2', source: 'b', target: 'a', label: '驳回', kind: 'loop' },
    ],
  };
}

const DIRECTIONS: Direction[] = ['right', 'left', 'down', 'both'];

beforeEach(() => {
  setActivePinia(createPinia());
});

describe('分派：树布局逐像素不变', () => {
  it.each(DIRECTIONS)('tree + %s 与 layoutTidy 完全一致', direction => {
    const root = tree();
    const dispatched = layoutDiagram({ root, diagramType: 'tree', direction });
    const direct = layoutTidy(root, { direction });

    expect([...dispatched.positions]).toEqual([...direct.positions]);
    expect(dispatched.bounds).toEqual(direct.bounds);
    expect(dispatched.direction).toBe(direct.direction);
    // 树分支不多带任何字段：多一个 `backEdges: undefined` 都算契约变化
    expect(dispatched.backEdges).toBeUndefined();
    expect(dispatched.fishbone).toBeUndefined();
  });

  it('不传图型（旧图）走的也是树布局', () => {
    const root = tree();
    expect([...layoutDiagram({ root, direction: 'right' }).positions])
      .toEqual([...layoutTidy(root, { direction: 'right' }).positions]);
  });

  it('diagramTypeOf：字段缺失 / null 都读成 tree', () => {
    expect(diagramTypeOf(null)).toBe('tree');
    expect(diagramTypeOf(undefined)).toBe('tree');
    expect(diagramTypeOf({})).toBe('tree');
    expect(diagramTypeOf({ diagram_type: 'fishbone' })).toBe('fishbone');
  });
});

describe('分派：换成各自的算法', () => {
  it('fishbone 用的是鱼骨布局', () => {
    const root = fishboneTree();
    const dispatched = layoutDiagram({ root, diagramType: 'fishbone' });
    const direct = layoutFishbone(root);
    expect([...dispatched.positions]).toEqual([...direct.positions]);
    expect([...dispatched.fishbone!.links]).toEqual([...direct.fishbone.links]);
  });

  it('flowchart 用的是 dagre 分层，且吃附加边', () => {
    const content = flowContent();
    const dispatched = layoutDiagram({
      root: content.root,
      edges: content.edges,
      diagramType: 'flowchart',
    });
    const direct = layoutFlow(content.root, content.edges);
    expect([...dispatched.positions]).toEqual([...direct.positions]);
    expect([...dispatched.backEdges!]).toEqual(['e2']);
    // 同一棵树用树布局会得到完全不同的坐标：分派真的换了算法
    const tidy = layoutTidy(content.root, { direction: 'right' });
    expect(dispatched.positions.get('c')).not.toEqual(tidy.positions.get('c'));
  });
});

describe('store：画布按 content.diagram_type 选布局', () => {
  /**
   * 装载一份内容并**全展开**。
   *
   * 这个文件钉的是"图型 → 布局函数"的分派：断言的是 `store.nodes` 与某个布局函数的
   * 坐标逐个相等。培养皿里不能有别的变量——视图层的默认收起会少画几层（见
   * `canvas/view-collapse.ts`），那与布局分派对错无关，所以这里显式关掉它。
   * 鱼骨与流程本来就不收起，这一句对它们没有影响。
   */
  function mount(content: MindMapContent) {
    const store = useMindMapStore();
    store.preview(content, 'right');
    store.expandAllInView();
    return store;
  }

  it('没有 diagram_type 的旧图仍是树布局（位置与 layoutTidy 一致）', () => {
    const content: MindMapContent = { title: '旧图', root: tree() };
    const store = mount(content);
    const expected = layoutTidy(content.root, { direction: 'right' });
    for (const [id, point] of expected.positions) {
      const node = store.nodes.find(item => item.id === id)!;
      expect(node.position).toEqual({
        x: point.x - NODE_SIZE.width / 2,
        y: point.y - NODE_SIZE.height / 2,
      });
    }
  });

  it.each<DiagramType>(['fishbone', 'flowchart'])('%s 的节点坐标来自对应布局', type => {
    const content: MindMapContent = {
      title: '图',
      diagram_type: type,
      root: type === 'fishbone' ? fishboneTree() : flowContent().root,
      edges: type === 'flowchart' ? flowContent().edges : [],
    };
    const store = mount(content);
    const expected = layoutDiagram({
      root: content.root,
      edges: content.edges,
      diagramType: type,
    });
    expect(store.nodes).toHaveLength(expected.positions.size);
    for (const [id, point] of expected.positions) {
      const node = store.nodes.find(item => item.id === id)!;
      expect(node.position.x).toBeCloseTo(point.x - NODE_SIZE.width / 2, 9);
      expect(node.position.y).toBeCloseTo(point.y - NODE_SIZE.height / 2, 9);
    }
  });

  it('鱼骨图的父子连线交给 fishbone 渲染器，并带上折线', () => {
    const store = mount({ title: 't', diagram_type: 'fishbone', root: fishboneTree() });
    const edges = store.edges.filter(edge => edge.type === 'fishbone');
    expect(edges.length).toBeGreaterThan(0);
    for (const edge of edges) {
      const points = (edge.data as { points: Array<{ x: number; y: number }> }).points;
      expect(points.length).toBeGreaterThanOrEqual(2);
    }
    // 骨线的句柄：出左、进右（子永远在父的左侧），且这些句柄确实被渲染出来了
    const bone = store.edges.find(edge => edge.id === 'root->人')!;
    expect(bone.sourceHandle).toBe('out-left');
    expect(bone.targetHandle).toBe('in-right');
    const rootNode = store.nodes.find(item => item.id === 'root')!;
    expect((rootNode.data as { out: string[] }).out).toContain('left');
    const renNode = store.nodes.find(item => item.id === '人')!;
    expect((renNode.data as { in: string[] }).in).toContain('right');
  });

  it('主骨末端的箭头只挂在布局指定的那一条骨线上', () => {
    const store = mount({ title: 't', diagram_type: 'fishbone', root: fishboneTree() });
    const carriers = store.edges.filter(edge => (edge.data as { arrow?: unknown }).arrow !== undefined);
    // 主骨那一小段被六条骨线各画了一遍；箭头只登记一条，画布上就只有一份
    expect(carriers).toHaveLength(1);
    expect(carriers[0]!.id).toBe('root->人');
    expect((carriers[0]!.data as { arrow?: Array<{ x: number; y: number }> }).arrow![0])
      .toEqual({ x: -NODE_SIZE.width / 2, y: 0 });
    // 树图型一根都没有
    const treeStore = mount({ title: '树', root: tree() });
    expect(treeStore.edges.some(edge => (edge.data as { arrow?: unknown } | undefined)?.arrow !== undefined))
      .toBe(false);
  });

  it('流程图里回退边绕左侧走、并打上 back 标记', () => {
    const store = mount(flowContent());
    const back = store.edges.find(edge => edge.id === 'attach:e2')!;
    expect(back.sourceHandle).toBe('out-left');
    expect(back.targetHandle).toBe('in-left');
    expect((back.data as { back?: boolean }).back).toBe(true);

    // 前向的条件分支按几何算侧边（阶段是上下排的，所以走 bottom → top）
    const branch = store.edges.find(edge => edge.id === 'attach:e1')!;
    expect(branch.sourceHandle).toBe('out-bottom');
    expect(branch.targetHandle).toBe('in-top');
    expect((branch.data as { back?: boolean }).back).toBeUndefined();
    // kind 照旧带给渲染层："是"要能画成条件分支
    expect((branch.data as { kind?: string }).kind).toBe('branch');
  });

  it('树图型的连线一根都没变：父子线 smoothstep、附加边 mindEdge', () => {
    const content: MindMapContent = {
      title: '树',
      root: tree(),
      edges: [{ id: 'e1', source: 'a', target: 'c', label: '关联', kind: 'flow' }],
    };
    const store = mount(content);
    const treeEdges = store.edges.filter(edge => edge.type === 'smoothstep');
    expect(treeEdges.length).toBeGreaterThan(0);
    expect(store.edges.some(edge => edge.type === 'fishbone')).toBe(false);
    const attach = store.edges.find(edge => edge.id === 'attach:e1')!;
    expect(attach.type).toBe('mindEdge');
    expect((attach.data as { back?: boolean }).back).toBeUndefined();
  });

  it('没有内容时不算布局（预览还没长出来）', () => {
    const store = useMindMapStore();
    expect(store.layout).toBeNull();
    expect(store.nodes).toEqual([]);
  });
});

describe('附加边不参与分层（树的语义保持不变）', () => {
  it('给树加一条边，节点坐标一个都不动', () => {
    const store = useMindMapStore();
    const content: MindMapContent = { title: '树', root: tree() };
    store.preview(content, 'right');
    const before = store.nodes.map(item => `${item.id}:${item.position.x},${item.position.y}`);

    expect(store.connectNodes('a', 'c', '关联')).toBe(true);
    const after = store.nodes.map(item => `${item.id}:${item.position.x},${item.position.y}`);
    expect(after).toEqual(before);
  });

  it('流程图相反：加一条前向分支会重排（这是分层的意义）', () => {
    const store = useMindMapStore();
    // 只留驳回边：此时三个阶段都在同一层（都挂在 root 下）。
    const content = flowContent();
    store.preview({ ...content, edges: [content.edges![0]!] }, 'right');
    const before = store.nodes.map(item => item.position.y);

    // b → c（是）要求 c 至少比 b 低一层 → c 会落下去。
    expect(store.connectNodes('b', 'c', '是', 'branch')).toBe(true);
    const after = store.nodes.map(item => item.position.y);
    expect(after).not.toEqual(before);
    expect(store.nodes.find(item => item.id === 'c')!.position.y)
      .toBeGreaterThan(store.nodes.find(item => item.id === 'b')!.position.y);
  });

  it('加一条回退边不重排：分层不看它（它只是画出来的那条虚线）', () => {
    const store = useMindMapStore();
    store.preview({ ...flowContent(), edges: [] }, 'right');
    const before = store.nodes.map(item => `${item.id}:${item.position.x},${item.position.y}`);

    expect(store.connectNodes('c', 'a', '驳回', 'loop')).toBe(true);
    const after = store.nodes.map(item => `${item.id}:${item.position.x},${item.position.y}`);
    expect(after).toEqual(before);
    // 但它仍然被画出来，并且被标成回退
    const back = store.edges.find(edge => edge.id.startsWith('attach:'))!;
    expect((back.data as { back?: boolean }).back).toBe(true);
    expect(back.sourceHandle).toBe('out-left');
  });
});

describe('鱼骨 / 流程的几何与渲染契约', () => {
  it('Edges 的 data 里没有多余字段（契约不动）：树图型下只有 label/kind/edgeId', () => {
    const store = useMindMapStore();
    const content: MindMapContent = {
      title: '树',
      root: tree(),
      edges: [{ id: 'e1', source: 'a', target: 'c', label: '关联', kind: 'flow' } as MindEdge],
    };
    store.preview(content, 'right');
    const attach = store.edges.find(edge => edge.id === 'attach:e1')!;
    // `back` 只在该边被判成回退时出现；树图型下恒为 undefined（键存在但值是 undefined，
    // 与"没有这个字段"在 Vue Flow 里等价，这里只钉"不会误标 true"）。
    expect((attach.data as { back?: boolean }).back).toBeFalsy();
  });
});
