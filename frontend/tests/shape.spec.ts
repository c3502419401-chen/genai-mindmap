import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import type { FishboneContent, FlowContent, MindMapContent, MindNode } from '../src/api/types';
import { applyOps } from '../src/mindmap/ops';
import { OpError } from '../src/mindmap/ops';
import { renameNode, validateDraft } from '../src/mindmap/edits';
import {
  CANVAS_ROOT_ID,
  canvasTreeOf,
  containersOf,
  countNodesIn,
  findIn,
  isContainerRoot,
  kindOf,
  replaceContainer,
  rootsOf,
  topBranchesOf,
  viewOf,
} from '../src/mindmap/shape';
import { useMindMapStore } from '../src/stores/mindmap';
import { withDirection } from '../src/mindmap/direction';

/**
 * 三种形状的读侧适配与写回（阶段 3a）。
 *
 * 这组用例盯的是**"前端拿到另外两种结构会发生什么"**，所以刻意都从 store 的公开
 * 行为读结论（`nodes` / `nodeCount` / `content`），而不是只测 `shape.ts` 的纯函数：
 * 会炸的地方从来不是"函数算错了"，而是"某个消费点仍然假设导图是一棵树"。
 *
 * 树的逐像素不变性不在这里验——那由既有 700 多条用例钉着。
 */

/**
 * 把 store 里那份内容按指定形状读出来。
 *
 * store 的 `content` 声明类型仍是 `MindMapContent`（理由见 `stores/mindmap.ts` 里那段
 * 注释），所以这里要显式断言一次——这一步正是"声明说树、实际可能是鱼骨/流程"的
 * 代价，落在用例里比散在源码里好。
 */
const asShape = <T>(content: MindMapContent | null): T => content as unknown as T;

const node = (id: string, label: string, children: MindNode[] = []): MindNode => ({
  id,
  label,
  children,
});

const tree = (): MindMapContent => ({
  title: '树',
  root: node('root', '中心', [node('a', '甲', [node('a1', '甲一')]), node('b', '乙')]),
});

const fishbone = (): FishboneContent => ({
  title: '报销慢的原因分析',
  diagram_type: 'fishbone',
  problem: '为什么报销要 11 天',
  bones: [
    { category: '人', root: node('b_ren', '人', [node('c1', '审批的人不在')]), color: 'pink' },
    { category: '机', root: node('b_ji', '机') },
  ],
});

const flow = (): FlowContent => ({
  title: '报销流程',
  diagram_type: 'flowchart',
  stages: [
    { id: 's_1', root: node('n1', '提交申请', [node('n11', '填表')]) },
    { id: 's_2', root: node('n2', '形式审查') },
  ],
  edges: [{ id: 'e_1', source: 's_1', target: 's_2', label: '通过', kind: 'flow' }],
});

beforeEach(() => {
  setActivePinia(createPinia());
});

describe('shape：按形状判别与抹平', () => {
  it('三种形状各有各的顶层容器', () => {
    expect(kindOf(tree())).toBe('tree');
    expect(kindOf(fishbone())).toBe('fishbone');
    expect(kindOf(flow())).toBe('flowchart');

    expect(containersOf(tree()).map(item => item.id)).toEqual(['root']);
    expect(containersOf(fishbone()).map(item => item.id)).toEqual(['b_ren', 'b_ji']);
    expect(containersOf(flow()).map(item => item.id)).toEqual(['s_1', 's_2']);
  });

  it('`diagram_type` 标着 fishbone 但形状仍是树时，按树处理', () => {
    // 阶段 2 之前生成侧就是这么产出鱼骨图的，库里真有一批这样的记录。
    const legacy: MindMapContent = { ...tree(), diagram_type: 'fishbone' };
    expect(kindOf(legacy)).toBe('tree');
    expect(canvasTreeOf(legacy).synthetic).toBe(false);
  });

  it('viewOf 给出每个节点的容器归属与层号', () => {
    const view = viewOf(fishbone());
    expect(view.kind).toBe('fishbone');
    expect(view.nodes.map(item => `${item.containerId}:${item.node.id}:${item.depth}`)).toEqual([
      'b_ren:b_ren:1',
      'b_ren:c1:2',
      'b_ji:b_ji:1',
    ]);
  });

  it('画布树对树型**原样透传**（同一个对象），对另外两种拼合成树', () => {
    const content = tree();
    expect(canvasTreeOf(content).root).toBe(content.root);

    const fish = fishbone();
    const composed = canvasTreeOf(fish);
    expect(composed.synthetic).toBe(true);
    expect(composed.root.id).toBe(CANVAS_ROOT_ID);
    // 鱼头里那句是这张图的论点，不是文件名。
    expect(composed.root.label).toBe('为什么报销要 11 天');
    expect(composed.root.children.map(child => child.id)).toEqual(['b_ren', 'b_ji']);
    // 合成根只是拼出来的，原内容里没有它。
    expect(findIn(fish, CANVAS_ROOT_ID)).toBeNull();
  });

  it('replaceContainer 只改命中那一处，形状不变', () => {
    const fish = fishbone();
    // `fn` 拿到的是**容器自己的子树根**（不是被点名的那个节点）：调用方的写法是
    // `root => renameNode(root, id, ...)` 这类"吃根、返根"的纯函数。
    const next = replaceContainer(fish, 'c1', root => renameNode(root, 'c1', '改过的原因'));
    expect(kindOf(next)).toBe('fishbone');
    expect(findIn(next, 'c1')?.label).toBe('改过的原因');
    expect(rootsOf(next).map(root => root.id)).toEqual(['b_ren', 'b_ji']);
    // 未命中时原样返回同一个对象（调用方靠引用相等判"什么都没改"）。
    expect(replaceContainer(fish, 'nope', root => root)).toBe(fish);
  });

  it('`topBranchesOf` 对树数一级分支，对另外两种数容器', () => {
    // 这一条守的是界面文案（「N 个一级分支」）：树的最外层内容节点是 `root.children`，
    // 数容器的话一棵四支的导图会说"1 个一级分支"。
    expect(topBranchesOf(tree()).map(node => node.id)).toEqual(['a', 'b']);
    expect(topBranchesOf(fishbone()).map(node => node.id)).toEqual(['b_ren', 'b_ji']);
    expect(topBranchesOf(flow())).toHaveLength(2);
  });

  it('容器根不是可删的普通节点', () => {
    expect(isContainerRoot(fishbone(), 'b_ren')).toBe(true);
    expect(isContainerRoot(fishbone(), 'c1')).toBe(false);
    // 判据收的是**节点 id**：流程的阶段根是 `stage.root`，阶段 id（`s_1`）是另一个
    // 命名空间，它不在 `rootsOf()` 的返回值里。
    expect(isContainerRoot(flow(), 'n1')).toBe(true);
    expect(isContainerRoot(flow(), 's_1')).toBe(false);
  });
});

describe('画布：另外两种形状能画出来', () => {
  it('鱼骨图画出鱼头 + 全部骨与原因，鱼头不可选', () => {
    const store = useMindMapStore();
    store.preview(fishbone(), 'right');

    // 节点计数按 **content** 走：两根骨 + 一条原因 = 3。鱼头在画布上但不在 content 里，
    // 数进去会让顶栏比内容多一个。
    expect(store.nodeCount).toBe(3);
    expect(countNodesIn(store.content!)).toBe(3);
    expect(store.nodes).toHaveLength(4);
    expect(store.nodes.map(item => item.id).sort()).toEqual(['b_ji', 'b_ren', 'c1', CANVAS_ROOT_ID].sort());
    // 每个节点都有坐标：一个都没落在 (0,0) 的兜底值上。
    for (const item of store.nodes) {
      expect(store.layout!.positions.get(item.id)).toBeDefined();
    }
    const head = store.nodes.find(item => item.id === CANVAS_ROOT_ID)!;
    expect(head.selectable).toBe(false);
    expect(head.data?.label).toBe('为什么报销要 11 天');
    // 骨根是可选中的真实节点。
    expect(store.nodes.find(item => item.id === 'b_ren')!.selectable).toBe(true);
  });

  it('流程图按阶段分层画出来，阶段之间的边在线', () => {
    const store = useMindMapStore();
    store.preview(flow(), 'down');

    expect(store.nodes.map(item => item.id).sort()).toEqual(['n1', 'n11', 'n2', CANVAS_ROOT_ID].sort());
    // 层的先后：根的层号小于阶段，阶段的层号小于阶段内的步骤。
    const layers = (store.layout as unknown as { layers: Map<string, number> }).layers;
    expect(layers.get(CANVAS_ROOT_ID)!).toBeLessThan(layers.get('n1')!);
    expect(layers.get('n1')!).toBeLessThan(layers.get('n11')!);

    const attach = store.edges.filter(edge => edge.id.startsWith('attach:'));
    expect(attach.map(edge => edge.id)).toEqual(['attach:e_1']);
    // 内容的边端点存的是**阶段 id**，画布上换成阶段入口节点的 id（否则这条线连
    // 坐标都找不到，会被整条跳过）。
    expect(attach[0].source).toBe('n1');
    expect(attach[0].target).toBe('n2');
    // 内容那边一个字节都没变：侧栏与删除读的仍是阶段 id。
    expect(store.contentEdges.map(edge => `${edge.source}->${edge.target}`)).toEqual(['s_1->s_2']);
  });

  it('换朝向不会给鱼骨/流程塞一个 `layout` 字段（那会让保存 422）', () => {
    // 判据落在**纯函数**上而不是 `store.setDirection`：第三轮起 `setDirection` 对
    // 非树型直接拒绝（`direction` 只有树有，见 `tests/drag-round3.spec.ts`），但
    // `save()` 仍然会调 `withDirection(content, direction.value)` 归一——那一步要是
    // 给鱼骨补一个 `layout`，下一次保存就是 422，而用户只是点了一下朝向。
    const content = withDirection(fishbone(), 'left');
    expect('layout' in content).toBe(false);
    expect(content.theme?.direction).toBe('left');
  });
});

describe('编辑：另外两种形状能改、能撤销', () => {
  it('改鱼骨图里的一条原因，写回还是鱼骨图', () => {
    const store = useMindMapStore();
    store.preview(fishbone(), 'right');

    store.commitLabel('c1', '审批的人总在出差');
    expect(findIn(store.content!, 'c1')?.label).toBe('审批的人总在出差');
    expect(kindOf(store.content!)).toBe('fishbone');
    expect(asShape<FishboneContent>(store.content).bones[0].root.children[0].label).toBe('审批的人总在出差');

    store.undo();
    expect(findIn(store.content!, 'c1')?.label).toBe('审批的人不在');
  });

  it('给骨下加子节点：落在那一根骨里，别的骨一个字节都不动', () => {
    const store = useMindMapStore();
    store.preview(fishbone(), 'right');
    const otherBone = asShape<FishboneContent>(store.content).bones[1].root;

    store.addChildTo('b_ren');
    const bones = asShape<FishboneContent>(store.content).bones;
    expect(bones[0].root.children).toHaveLength(2);
    // 第二根骨还是原来那个对象（结构共享，不是整份重建）。
    expect(bones[1].root).toBe(otherBone);
    // 新节点被选中并进入编辑态，用户可以立刻打字。
    expect(store.editingId).toBe(bones[0].root.children[1].id);
  });

  it('删掉一条原因连带删掉它的子树，但容器根删不得', () => {
    const store = useMindMapStore();
    store.preview(fishbone(), 'right');

    store.select('b_ren');
    store.deleteNode('b_ren');
    expect(store.content!.title).toBe('报销慢的原因分析'); // 没被删
    expect(findIn(store.content!, 'b_ren')).not.toBeNull();
    expect(store.status).toContain('不能删除');

    store.deleteNode('c1');
    expect(findIn(store.content!, 'c1')).toBeNull();
    expect(asShape<FishboneContent>(store.content).bones).toHaveLength(2);
  });

  it('流程图的阶段内步骤可改，阶段 id 与阶段序列都不变', () => {
    const store = useMindMapStore();
    store.preview(flow(), 'down');

    store.commitLabel('n11', '填报销单');
    expect(findIn(store.content!, 'n11')?.label).toBe('填报销单');
    const content = asShape<FlowContent>(store.content);
    expect(content.stages.map(stage => stage.id)).toEqual(['s_1', 's_2']);
    expect(content.stages[0].root.children[0].label).toBe('填报销单');
  });

  it('选中阶段内的步骤时，能看到该阶段的跳转边（否则用户删不掉自己加的线）', () => {
    const store = useMindMapStore();
    store.preview(flow(), 'down');

    store.select('n11');
    expect(store.selectedEdges.map(edge => edge.id)).toEqual(['e_1']);
    // 端点显示名按形状回退：阶段 id → 阶段根节点的标题。
    expect(store.edgeNameOf('s_1')).toBe('提交申请');
  });
});

describe('校验与 op 批次', () => {
  it('流程图的边按**阶段 id** 校验端点，不是节点 id', () => {
    expect(validateDraft(flow())).toBeNull();
    const broken: FlowContent = {
      ...flow(),
      edges: [{ id: 'e_9', source: 'n1', target: 's_2' }],
    };
    expect(validateDraft(broken)).toContain('阶段');
  });

  it('落在同一根骨里的 op 批次能执行，形状不变', () => {
    const fish = fishbone();
    const result = applyOps(fish, {
      batch_id: 'b1',
      base_revision: 0,
      ops: [{ op: 'add', id: 'c2', parent_id: 'b_ren', index: 1, label: '票据不齐' }],
    });
    expect(kindOf(result.content)).toBe('fishbone');
    expect(findIn(result.content, 'c2')?.label).toBe('票据不齐');
    expect(asShape<FishboneContent>(result.content as MindMapContent).bones[0].root.children)
      .toHaveLength(2);
  });

  it('跨容器的 op 批次被明确拒绝（一批只能改一处）', () => {
    const fish = fishbone();
    expect(() => applyOps(fish, {
      batch_id: 'b2',
      base_revision: 0,
      ops: [
        { op: 'update', id: 'c1', changes: { label: '甲' } },
        { op: 'update', id: 'b_ji', changes: { label: '乙' } },
      ],
    })).toThrowError(OpError);

    try {
      applyOps(fish, {
        batch_id: 'b2',
        base_revision: 0,
        ops: [
          { op: 'update', id: 'c1', changes: { label: '甲' } },
          { op: 'update', id: 'b_ji', changes: { label: '乙' } },
        ],
      });
    } catch (error) {
      expect((error as OpError).code).toBe('cross_container');
    }
  });
});
