import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import type { FlowContent, MindMapContent, MindNode } from '../src/api/types';
import {
  DATA_SIZE,
  DATABASE_SIZE,
  DECISION_SHAPE_NOTE,
  DECISION_SIZE,
  flowNodeSize,
  layoutFlow,
} from '../src/canvas/layout/flow';
import { NODE_SIZE } from '../src/canvas/layout/tidy';
import { layoutDiagram } from '../src/canvas/layout';
import MindNodeComponent from '../src/canvas/MindNode.vue';
import { CANVAS_ROOT_ID, canvasStageEdgesOf, canvasTreeOf, nodeShapeOf } from '../src/mindmap/shape';
import { useMindMapStore, type MindNodeData } from '../src/stores/mindmap';

/**
 * 判定节点的菱形。
 *
 * 这一组用例钉三件事，每一件都对应一个"看起来能跑、其实错了"的失败模式：
 *
 *  1. **判定节点换了外接框，其他节点没换**（`DECISION_SIZE` 与 `NODE_SIZE` 的分工）。
 *     漏掉这条的表现是菱形被画在一个 180×48 的框里，标题当场被 `line-clamp-2` 裁掉。
 *  2. **树与鱼骨一个像素都不能变**——哪怕节点上带着 `role: 'decision'`。漏掉这条的
 *     表现是"打开一张旧图，版面变了"，而这个项目里最严重的回归就这一类。
 *  3. **句柄落在菱形的顶点上**（`clip-path` 的顶点 = 外接框各边中点 = Handle 位置）。
 *     这条是"方案 A/C 与方案 B 的根本差别"，钉住它可以防止将来有人改成旋转 45°。
 */

const leaf = (id: string, label: string, role?: MindNode['role']): MindNode => ({
  id,
  label,
  ...(role ? { role } : {}),
  children: [],
});

/** 一份最小的流程图内容：两个阶段，第一个阶段里有一个判定节点带两条出口。 */
function flowContent(): FlowContent {
  return {
    title: '报销流程',
    diagram_type: 'flowchart',
    stages: [
      {
        id: 's1',
        root: {
          id: 'a', label: '提交申请', role: 'start',
          children: [
            leaf('a1', '材料齐全吗', 'decision'),
            leaf('a2', '补材料'),
          ],
        },
        edges: [
          { id: 'e-yes', source: 'a1', target: 'a2', label: '否', kind: 'branch' },
        ],
      },
      { id: 's2', root: { id: 'b', label: '付款', role: 'end', children: [] } },
    ],
    edges: [{ id: 'stage', source: 's1', target: 's2', kind: 'flow' }],
  };
}

/**
 * 一张带"读库 / 写库 / 录入"的流程图：数据形状（平行四边形）+ 存储形状（圆柱）
 * 与普通步骤并排，用来验证三件事——形状、外接框、以及普通步骤没被带跑。
 */
function dataFlowContent(): FlowContent {
  return {
    title: '报销审批',
    diagram_type: 'flowchart',
    stages: [
      {
        id: 's1',
        root: {
          id: 'a', label: '提交申请', role: 'start',
          children: [
            // 标题故意用满 16 字的两行标题：斜边与椭圆要是算错了宽度，这里第一个裁。
            leaf('in', '录入报销单与发票金额共十六个字', 'data'),
            leaf('db', '写入审批台账并归档原始凭证', 'database'),
            leaf('plain', '核对预算余额'),
          ],
        },
      },
      { id: 's2', root: { id: 'b', label: '付款', role: 'end', children: [] } },
    ],
    edges: [{ id: 'stage', source: 's1', target: 's2', kind: 'flow' }],
  };
}

function treeContent(): MindMapContent {  return {
    title: '树',
    root: {
      id: 'r', label: '根',
      children: [
        // 故意在树上也写一个 decision：形状必须**不**生效（见 shape.nodeShapeOf 的
        // 注释——形状跟的是"实际跑的布局"，不是 role 字段本身）。
        leaf('d', '不该变菱形', 'decision'),
        leaf('x', '普通'),
      ],
    },
  };
}

describe('role → 外接框', () => {
  it('判定节点用 DECISION_SIZE，其余用 NODE_SIZE', () => {
    expect(flowNodeSize('decision')).toBe(DECISION_SIZE);
    const roles: Array<MindNode['role']> = ['step', 'start', 'end', null, undefined];
    for (const role of roles) {
      expect(flowNodeSize(role)).toBe(NODE_SIZE);
    }
  });

  it('data 比 NODE_SIZE 宽、database 比它高（两种新形状各有各的外接框）', () => {
    expect(flowNodeSize('data')).toBe(DATA_SIZE);
    expect(flowNodeSize('database')).toBe(DATABASE_SIZE);
    // 平行四边形：斜边吃掉左右各 20%，所以宽度必须比矩形大（算术见断言下一条）。
    expect(DATA_SIZE.width).toBeGreaterThan(NODE_SIZE.width);
    expect(DATA_SIZE.height).toBe(NODE_SIZE.height);
    // 圆柱：上下两个椭圆吃掉高度，所以它必须比矩形高。
    expect(DATABASE_SIZE.height).toBeGreaterThan(NODE_SIZE.height);
    expect(DATABASE_SIZE.width).toBeGreaterThan(NODE_SIZE.width);
  });

  it('平行四边形与圆柱的可用文字区都够放 16 字的两行标题', () => {
    const LINES = 2;
    const LINE_HEIGHT = 17.5;          // `leading-tight` 下的行高（14px 字号）
    const textHeight = LINES * LINE_HEIGHT;

    // 平行四边形：`polygon(20% 0, 100% 0, 80% 100%, 0 100%)` 下，居中文字块的
    // 可用宽度 = `W·(0.8 − 0.2·h/H)`（推导见 `flow.ts::DATA_SIZE`）。
    const paraWidth = (size: { width: number; height: number }) =>
      size.width * (0.8 - 0.2 * (textHeight / size.height));
    // 每行能放几个汉字（`text-sm` 下一个汉字约 14px）。16 字的两行标题 = 8 字/行。
    const perLine = (size: { width: number; height: number }) => paraWidth(size) / 14;
    expect(perLine(DATA_SIZE)).toBeGreaterThanOrEqual(10);
    // 同样两行字放在 180×48 的平行四边形里只剩 8.4 字/行：**刚好卡住、一点余量都
    // 没有**（中文换行不按字切，标点带避头尾，实际会裁）。这正是它要比 `NODE_SIZE`
    // 宽的理由，不是审美偏好。
    expect(perLine(NODE_SIZE)).toBeLessThan(9);

    // 圆柱：文字只能落在两个椭圆之间的矩形带里，可用高度 = `H·(1 − 0.4)`（椭圆
    // 半高取 H 的 10%，见 `styles.css::.mind-cylinder`）。
    const bandHeight = (size: { width: number; height: number }) => size.height * 0.6;
    expect(bandHeight(DATABASE_SIZE)).toBeGreaterThan(textHeight);
    // 反过来：220×48 的圆柱只剩 28.8px，装不下 35px 的两行。
    expect(bandHeight(NODE_SIZE)).toBeLessThan(textHeight);
  });

  it('NODE_SIZE 一个字都没动（树与鱼骨的布局常量）', () => {
    // 这条断言是"改菱形不许动树"的证据本身：树布局的入参就是这两个数。
    expect(NODE_SIZE.width).toBe(180);
    expect(NODE_SIZE.height).toBe(48);
  });

  it('菱形比矩形大：小框里放不下两行文字，所以这不是审美偏好', () => {
    // 外接框内接菱形在两行文字（≈35px）处的可用宽度。
    const usable = (size: { width: number; height: number }) =>
      size.width * (1 - 35 / size.height);
    expect(usable(DECISION_SIZE)).toBeGreaterThan(140);
    // 180×48 的菱形只剩不到 50px，五个汉字都放不下——这就是判定节点必须换框的原因。
    expect(usable(NODE_SIZE)).toBeLessThan(60);
  });
});

describe('layoutFlow：sizes 出参', () => {
  function flowTree() {
    const root: MindNode = {
      id: 'root', label: '报销',
      children: [
        {
          id: 'decide', label: '材料齐全吗', role: 'decision',
          children: [leaf('yes', '通过'), leaf('no', '退回')],
        },
      ],
    };
    return { root, edges: [
      { id: 'y', source: 'decide', target: 'yes', label: '是' as const },
      { id: 'n', source: 'decide', target: 'no', label: '否' as const },
    ] };
  }

  it('每个节点都报外接框：判定节点更大，其余是 NODE_SIZE', () => {
    const { root, edges } = flowTree();
    const result = layoutFlow(root, edges);
    expect([...result.sizes.keys()].sort()).toEqual(['decide', 'no', 'root', 'yes']);
    expect(result.sizes.get('decide')).toEqual(DECISION_SIZE);
    expect(result.sizes.get('root')).toEqual(NODE_SIZE);
    expect(result.sizes.get('yes')).toEqual(NODE_SIZE);
  });

  it('判定节点确实占更大的位置（dagre 用的是同一个框，不会压住邻居）', () => {
    const { root, edges } = flowTree();
    const result = layoutFlow(root, edges);
    const decide = result.positions.get('decide')!;
    const yes = result.positions.get('yes')!;
    const no = result.positions.get('no')!;
    // 下一层与判定节点的垂直间距至少是半个菱形高（不是半个矩形高）——菱形比矩形高
    // 62 像素，如果 dagre 还按 48 排，这条会算出一个明显偏小的值。
    expect(yes.y - decide.y).toBeGreaterThan(DECISION_SIZE.height / 2);
    // 两个出口横向分开的间距按各自的外接框算：两个 NODE_SIZE + `nodesep`。
    expect(Math.abs(yes.x - no.x)).toBeGreaterThanOrEqual(NODE_SIZE.width);
  });

  it('包围盒按各自的外接框算：菱形的尖角不会被 fitView 切掉', () => {
    const { root, edges } = flowTree();
    const result = layoutFlow(root, edges);
    for (const [id, point] of result.positions) {
      const size = result.sizes.get(id)!;
      expect(point.x - size.width / 2).toBeGreaterThanOrEqual(result.bounds.minX - 1e-9);
      expect(point.x + size.width / 2).toBeLessThanOrEqual(result.bounds.maxX + 1e-9);
      expect(point.y - size.height / 2).toBeGreaterThanOrEqual(result.bounds.minY - 1e-9);
      expect(point.y + size.height / 2).toBeLessThanOrEqual(result.bounds.maxY + 1e-9);
    }
  });

  it('没有任何 decision 时，坐标与尺寸和"没有青色菱形这回事"完全一致', () => {
    const root: MindNode = {
      id: 'r', label: '根',
      children: [leaf('a', '甲'), leaf('b', '乙'), leaf('c', '丙')],
    };
    const result = layoutFlow(root, []);
    for (const size of result.sizes.values()) expect(size).toEqual(NODE_SIZE);
    // 单节点的包围盒 = NODE_SIZE，与 `flow.spec.ts` 里那条旧用例同一个结论。
    const single = layoutFlow({ id: 'only', label: '唯一', children: [] }, []);
    expect(single.bounds.width).toBe(NODE_SIZE.width);
    expect(single.bounds.height).toBe(NODE_SIZE.height);
  });

  it('DECISION_SHAPE_NOTE 记的是实际状态，不是"保持矩形"', () => {
    expect(DECISION_SHAPE_NOTE).toBe('diamond-clip-path');
    expect(DECISION_SHAPE_NOTE).not.toBe('kept-rectangle');
  });
});

describe('nodeShapeOf：形状只认 role', () => {
  it('判定是菱形，起点/终点是胶囊，其余是矩形', () => {
    expect(nodeShapeOf('decision')).toBe('diamond');
    expect(nodeShapeOf('start')).toBe('pill');
    expect(nodeShapeOf('end')).toBe('pill');
    expect(nodeShapeOf('step')).toBe('rect');
    expect(nodeShapeOf(null)).toBe('rect');
    expect(nodeShapeOf(undefined)).toBe('rect');
  });

  it('data 是平行四边形、database 是圆柱（画布上的形状名与 role 一一对上）', () => {
    expect(nodeShapeOf('data')).toBe('parallelogram');
    expect(nodeShapeOf('database')).toBe('cylinder');
    // 数据形状不能退回矩形：那样"这一步在存数据"就完全看不出来了，而它们正是
    // 补这两个角色的全部理由。
    expect(nodeShapeOf('data')).not.toBe('rect');
    expect(nodeShapeOf('database')).not.toBe('rect');
  });

  it('起点/终点不是菱形：它们没有分叉，画成菱形会读成"这里要选一条路"', () => {
    expect(nodeShapeOf('start')).not.toBe('diamond');
    expect(nodeShapeOf('end')).not.toBe('diamond');
  });
});

describe('画布：流程图里 role 真的变成形状', () => {
  beforeEach(() => setActivePinia(createPinia()));

  it('流程图的判定节点带 diamond 形状与更大的外接框，位置按新框居中', () => {
    const content = flowContent();
    const store = useMindMapStore();
    store.preview(content, 'right');

    const byId = new Map(store.nodes.map(item => [item.id, item]));
    const decide = byId.get('a1')!;
    expect(decide.data!.shape).toBe('diamond');
    expect(decide.data!.role).toBe('decision');
    expect(decide.data!.size).toEqual(DECISION_SIZE);

    // 位置是**左上角**：中心 - 半个外接框。用旧的 NODE_SIZE 算会偏 20/31 像素，
    // 而连线仍按中心画——表现是"线从节点外面擦过去"。
    // 入参与 store 完全一致（合成树 + 阶段间边），否则比的是两次不同的布局。
    const layout = layoutDiagram({
      root: canvasTreeOf(content).root,
      edges: canvasStageEdgesOf(content),
      diagramType: 'flowchart',
    });
    const center = layout.positions.get('a1')!;
    expect(decide.position.x).toBeCloseTo(center.x - DECISION_SIZE.width / 2, 9);
    expect(decide.position.y).toBeCloseTo(center.y - DECISION_SIZE.height / 2, 9);
    // 同一个布局里普通节点仍然按 NODE_SIZE 居中。
    const plain = byId.get('a2')!;
    const plainCenter = layout.positions.get('a2')!;
    expect(plain.position.x).toBeCloseTo(plainCenter.x - NODE_SIZE.width / 2, 9);
    expect(plain.position.y).toBeCloseTo(plainCenter.y - NODE_SIZE.height / 2, 9);
  });

  it('start / end 是胶囊，普通步骤是矩形（都不是菱形）', () => {
    const store = useMindMapStore();
    store.preview(flowContent(), 'right');
    const byId = new Map(store.nodes.map(item => [item.id, item.data]));
    expect(byId.get('a')!.shape).toBe('pill');
    expect(byId.get('b')!.shape).toBe('pill');
    expect(byId.get('a2')!.shape).toBe('rect');
  });

  it('数据节点拿到平行四边形 / 圆柱与各自的外接框，位置按新框居中', () => {
    const content = dataFlowContent();
    const store = useMindMapStore();
    store.preview(content, 'right');
    const byId = new Map(store.nodes.map(item => [item.id, item]));
    expect(byId.get('in')!.data!.shape).toBe('parallelogram');
    expect(byId.get('in')!.data!.size).toEqual(DATA_SIZE);
    expect(byId.get('db')!.data!.shape).toBe('cylinder');
    expect(byId.get('db')!.data!.size).toEqual(DATABASE_SIZE);

    // 位置是左上角 = 中心 − 半个**自己那个**外接框。用 `NODE_SIZE` 算会偏
    // (220−180)/2 = 20px（横向）与 (68−48)/2 = 10px（纵向），而连线按中心画
    // ——表现是"线从节点外面擦过去"。
    const layout = layoutDiagram({
      root: canvasTreeOf(content).root,
      edges: canvasStageEdgesOf(content),
      diagramType: 'flowchart',
    });
    for (const [id, size] of [['in', DATA_SIZE], ['db', DATABASE_SIZE]] as const) {
      const center = layout.positions.get(id)!;
      expect(byId.get(id)!.position.x).toBeCloseTo(center.x - size.width / 2, 9);
      expect(byId.get(id)!.position.y).toBeCloseTo(center.y - size.height / 2, 9);
    }
    // 同一张图里的普通步骤仍然是 180×48。
    expect(byId.get('plain')!.data!.shape).toBe('rect');
    expect(byId.get('plain')!.data!.size).toEqual(NODE_SIZE);
  });

  it('合成根（图标题）不带 role：形状只认内容里的 role', () => {
    const tree = canvasTreeOf(flowContent());
    expect(tree.root.id).toBe(CANVAS_ROOT_ID);
    // 给它标 start 会让画布上出现两个一模一样的开始胶囊——后端 `_stamp_flow_roles`
    // 已经把第一个阶段的根标成 start 了（"提交申请"），那一个才是流程的入口。
    expect(tree.root.role ?? null).toBeNull();
    const fishbone = canvasTreeOf({
      title: '鱼骨', diagram_type: 'fishbone', problem: '为什么报销要 11 天',
      bones: [{ category: '人', root: leaf('n1', '甲') }],
    });
    expect(fishbone.root.role ?? null).toBeNull();
  });

  it('树里的节点即使带着 role="decision" 也不变形状、不变尺寸', () => {
    const store = useMindMapStore();
    store.preview(treeContent(), 'right');
    const decide = store.nodes.find(item => item.id === 'd')!;
    // 布局走的是树（`DiagramLayout.sizes` 不存在），所以形状字段根本不发。
    expect(decide.data!.shape).toBeUndefined();
    expect(decide.data!.size).toBeUndefined();
    expect(decide.position).toEqual({
      x: expect.any(Number),
      y: expect.any(Number),
    });
  });

  it('structure 是树、但 diagram_type 写着 flowchart（历史数据）时也不变', () => {
    const store = useMindMapStore();
    // 形状的判据是"实际跑的布局有没有 sizes"，不是 `diagram_type` 字段——
    // 库里存在"标着 flowchart、形状是树"的记录，它们必须走树布局那条路。
    store.preview(treeContent(), 'right');
    const decide = store.nodes.find(item => item.id === 'd')!;
    expect(decide.data!.shape).toBeUndefined();

    // 反过来：真正是流程结构的图，同名字段就该生效。
    const flow = useMindMapStore();
    setActivePinia(createPinia());
    flow.preview(flowContent(), 'right');
    expect(flow.nodes.find(item => item.id === 'a1')!.data!.shape).toBe('diamond');
  });
});

describe('MindNode：五种形状的 DOM', () => {
  function data(over: Partial<MindNodeData> = {}): MindNodeData {
    return {
      label: '材料齐全吗', body: '材料齐全吗', fullLabel: '材料齐全吗',
      depth: 1, out: [], in: [], ...over,
    };
  }

  function mountNode(over: Partial<MindNodeData> = {}) {
    return mount(MindNodeComponent, { props: { id: 'n', data: data(over) } });
  }

  it('菱形节点挂三层 clip-path 背景，文字仍在原来那个 span 里', () => {    const wrapper = mountNode({ shape: 'diamond', size: DECISION_SIZE, role: 'decision' });
    const cell = wrapper.find('[data-node-shape="diamond"]');
    expect(cell.exists()).toBe(true);
    expect(cell.findAll('.diamond-layer')).toHaveLength(3);
    expect(cell.find('.diamond-glow').exists()).toBe(true);
    expect(cell.find('.diamond-stroke').exists()).toBe(true);
    expect(cell.find('.diamond-fill').exists()).toBe(true);
    // 文字没有被搬进别的层：还是那个 `.line-clamp-2`，也还是这个框的直接子元素。
    const text = cell.find('.line-clamp-2');
    expect(text.exists()).toBe(true);
    expect(text.text()).toBe('材料齐全吗');
    expect((text.element as HTMLElement).parentElement).toBe(cell.element);
    // 外接框是菱形那一份。
    expect((cell.element as HTMLElement).style.width).toBe(`${DECISION_SIZE.width}px`);
    expect((cell.element as HTMLElement).style.height).toBe(`${DECISION_SIZE.height}px`);
  });

  it('矩形与胶囊不渲染菱形背景层（树的渲染路径一个元素都不多）', () => {
    const rect = mountNode();
    expect(rect.find('[data-node-shape="rect"]').exists()).toBe(true);
    expect(rect.findAll('.diamond-layer')).toHaveLength(0);
    // 原来那两个类还在：矩形节点的尺寸不靠内联 style。
    const cell = rect.find('[data-node-shape="rect"]');
    expect(cell.classes()).toContain('h-12');
    expect(cell.classes()).toContain('w-[180px]');
    expect(cell.classes()).toContain('rounded-md');
    expect((cell.element as HTMLElement).style.width).toBe('');

    const pill = mountNode({ shape: 'pill', role: 'start' });
    expect(pill.find('[data-node-shape="pill"]').classes()).toContain('rounded-full');
    expect(pill.findAll('.diamond-layer')).toHaveLength(0);
  });

  it('三态在菱形上都找得到（描边色/光晕由 data 属性决定，不是各写一套结构）', () => {
    const tones = [
      { over: {}, tone: 'default' },
      { over: { selected: true } as Partial<MindNodeData>, tone: 'selected' },
      { over: { changed: true }, tone: 'changed' },
      { over: { draft: 'preview' as const }, tone: 'preview' },
    ];
    for (const { over, tone } of tones) {
      const wrapper = mount(MindNodeComponent, {
        props: {
          id: 'n',
          data: data({ shape: 'diamond', size: DECISION_SIZE, ...over }),
          selected: Boolean((over as { selected?: boolean }).selected),
        },
      });
      const cell = wrapper.find('[data-node-shape="diamond"]');
      expect(cell.attributes('data-diamond-tone')).toBe(tone);
      // 三种状态都必须留着描边层——`clip-path` 会把 border/ring 裁掉，描边层是唯一的框。
      expect(cell.find('.diamond-stroke').exists()).toBe(true);
    }
  });

  it('起点/终点是胶囊，且与普通节点在视觉上分得开', () => {
    const start = mountNode({ shape: 'pill', role: 'start' });
    const end = mountNode({ shape: 'pill', role: 'end' });
    // 起点实心深底，终点白底粗描边——两句话说的是同一件事的两半：入口是实的、出口是收的。
    expect(start.find('[data-node-shape="pill"]').classes()).toContain('bg-slate-900');
    expect(end.find('[data-node-shape="pill"]').classes()).toContain('border-2');
    expect(end.find('[data-node-shape="pill"]').classes()).toContain('bg-white');
  });

  it('菱形的内联输入框与水印角标都还在（没被裁切层吃掉）', async () => {
    const wrapper = mount(MindNodeComponent, {
      props: {
        id: 'n',
        data: data({ shape: 'diamond', size: DECISION_SIZE, refs: 2 }),
        editing: true,
      },
    });
    const cell = wrapper.find('[data-node-shape="diamond"]');
    expect(cell.find('input').exists()).toBe(true);
    // 引用角标挂在**外层**容器上（`relative`），所以在菱形之外、不会被裁。
    expect(wrapper.find('[data-test="node-refs"]').exists()).toBe(true);
  });

  it('平行四边形：三层同形背景 + 自己的外接框 + 文字仍是框的直接子元素', () => {
    const wrapper = mountNode({ shape: 'parallelogram', size: DATA_SIZE, role: 'data' });
    const cell = wrapper.find('[data-node-shape="parallelogram"]');
    expect(cell.exists()).toBe(true);
    expect(cell.classes()).toContain('mind-parallelogram');
    // 与菱形同一套三层结构：`clip-path` 会把 border / ring / box-shadow 一起裁掉。
    expect(cell.findAll('.parallelogram-layer')).toHaveLength(3);
    expect(cell.find('.parallelogram-glow').exists()).toBe(true);
    expect(cell.find('.parallelogram-stroke').exists()).toBe(true);
    expect(cell.find('.parallelogram-fill').exists()).toBe(true);
    // 文字没被搬进别的层，也没挂 `TONE_CLASS` 的 border/bg（那些会被裁掉）。
    const text = cell.find('.line-clamp-2');
    expect((text.element as HTMLElement).parentElement).toBe(cell.element);
    expect(cell.classes()).not.toContain('border');
    expect(cell.classes()).not.toContain('bg-white');
    // 外接框是 DATA_SIZE。
    expect((cell.element as HTMLElement).style.width).toBe(`${DATA_SIZE.width}px`);
    expect((cell.element as HTMLElement).style.height).toBe(`${DATA_SIZE.height}px`);
  });

  it('圆柱：SVG 两条 path（轮廓 + 顶面下弧）+ 自己的外接框', () => {
    const wrapper = mountNode({ shape: 'cylinder', size: DATABASE_SIZE, role: 'database' });
    const cell = wrapper.find('[data-node-shape="cylinder"]');
    expect(cell.exists()).toBe(true);
    expect(cell.classes()).toContain('mind-cylinder');
    // 轮廓与盖沿是两条独立 path：只有轮廓时那是一个压扁的药丸，读不出"存储"。
    const svg = cell.find('svg.cylinder-svg');
    expect(svg.exists()).toBe(true);
    expect(svg.find('path.cylinder-body').exists()).toBe(true);
    expect(svg.find('path.cylinder-lid').exists()).toBe(true);
    // `preserveAspectRatio="none"` 是随外接框拉伸的前提。
    expect(svg.attributes('preserveAspectRatio')).toBe('none');
    // 两条 path 的 `d` 都不是空的（贝塞尔近似椭圆，不用 A 命令）。
    expect((svg.find('path.cylinder-body').element as SVGPathElement)
      .getAttribute('d')).toContain('C');
    const text = cell.find('.line-clamp-2');
    expect((text.element as HTMLElement).parentElement).toBe(cell.element);
    expect((cell.element as HTMLElement).style.height).toBe(`${DATABASE_SIZE.height}px`);
  });

  it('三态在平行四边形与圆柱上都找得到（改的是变量，不是结构）', () => {
    const tones = [
      { over: {}, tone: 'default' },
      { over: { selected: true } as Partial<MindNodeData>, tone: 'selected' },
      { over: { changed: true }, tone: 'changed' },
      { over: { draft: 'preview' as const }, tone: 'preview' },
    ];
    for (const shape of ['parallelogram', 'cylinder'] as const) {
      for (const { over, tone } of tones) {
        const wrapper = mount(MindNodeComponent, {
          props: {
            id: 'n',
            data: data({ shape, size: DATA_SIZE, ...over }),
            selected: Boolean((over as { selected?: boolean }).selected),
          },
        });
        const cell = wrapper.find(`[data-node-shape="${shape}"]`);
        expect(cell.attributes('data-shape-tone')).toBe(tone);
        // 描边层 / 轮廓 path 在每种 tone 下都必须存在——它们是唯一的框。
        const stroke = shape === 'parallelogram'
          ? cell.find('.parallelogram-stroke')
          : cell.find('path.cylinder-body');
        expect(stroke.exists()).toBe(true);
      }
    }
  });
});
