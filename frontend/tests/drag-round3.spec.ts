import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import type { FishboneContent, FlowContent, MindNode } from '../src/api/types';
import { layoutFishbone } from '../src/canvas/layout/fishbone';
import { layoutFlow } from '../src/canvas/layout/flow';
import { NODE_SIZE } from '../src/canvas/layout/tidy';
import { moveScopeProblem } from '../src/mindmap/move-scope';
import { canvasTreeOf } from '../src/mindmap/shape';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * 拖动第三轮的两件结构判断：
 *
 *  1. **流程图 / 鱼骨图适配拖动**——判据不是"支不支持"，而是"改到哪一层为止"：
 *     容器内（阶段的步骤、骨上的原因）放开，跨容器明确拒绝。判据在
 *     `mindmap/move-scope.ts`，这里把它连同落点几何一起钉住；
 *  2. **旋转**——`direction` 只有树有。这一组用例钉的是"非树型换了朝向不会静默
 *     改内容"（那是上一版真有的一个 bug：点了「向左」，内容被改脏、画布不动）。
 */

// ---------------------------------------------------------------- fixtures

function tree(): MindNode {
  return {
    id: 'root',
    label: '根主题',
    children: [
      { id: 'a', label: '甲', children: [{ id: 'a1', label: '甲一', children: [] }] },
      { id: 'b', label: '乙', children: [] },
    ],
  };
}

/**
 * 鱼骨：两根骨，第一根有两个原因。
 *
 * 两根骨就够——这一组用例要回答的是"同一根骨内 vs 跨骨"这一条判据，而骨的数量
 * 只影响布局坐标，不影响判据。
 */
function fish(): FishboneContent {
  return {
    title: '报销慢',
    diagram_type: 'fishbone',
    problem: '为什么报销要 11 天',
    bones: [
      {
        category: '人',
        root: {
          id: 'p', label: '人', children: [
            { id: 'p1', label: '人手不足', children: [] },
            { id: 'p2', label: '审批人多', children: [] },
          ],
        },
      },
      {
        category: '机',
        root: { id: 'm', label: '机', children: [{ id: 'm1', label: '系统慢', children: [] }] },
      },
    ],
  };
}

/** 流程图：两个阶段，第一阶段里有两个步骤。 */
function flow(): FlowContent {
  return {
    title: '报销流程',
    diagram_type: 'flowchart',
    stages: [
      {
        id: 's1',
        root: {
          id: 'f0', label: '提交申请', children: [
            { id: 'f1', label: '填单', children: [] },
            { id: 'f2', label: '附发票', children: [] },
          ],
        },
      },
      {
        id: 's2',
        root: { id: 'g0', label: '形式审查', children: [{ id: 'g1', label: '核对金额', children: [] }] },
      },
    ],
    edges: [],
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
});

// ---------------------------------------------------------------- 一、作用域判据

describe('拖动作用域：容器内放开、跨容器拒绝', () => {
  it('树只有一块区域：这一层恒为放行（树的拖动行为一个字节都不变）', () => {
    const content = { title: 't', root: tree() };
    expect(moveScopeProblem(content, 'a1', 'b')).toBeNull();
    expect(moveScopeProblem(content, 'a', 'b')).toBeNull();
  });

  it('流程图：同一个阶段里的两个步骤可以互相拖', () => {
    expect(moveScopeProblem(flow(), 'f1', 'f2')).toBeNull();
  });

  it('流程图：跨阶段拒，理由说的是"判定出边会悬空"', () => {
    const reason = moveScopeProblem(flow(), 'f1', 'g1');
    expect(reason).toBeTruthy();
    expect(reason).toContain('阶段');
    expect(reason).toContain('悬空');
  });

  it('流程图：阶段入口（容器根）不能当落点，也不能被拖走', () => {
    // 拿它当落点 = 想把它塞进另一个阶段，那是换归属。
    expect(moveScopeProblem(flow(), 'f1', 'g0')).toContain('不能当落点');
    // 拖它本身 = 换阶段次序，那是流程语义。
    expect(moveScopeProblem(flow(), 'g0', 'f1')).toContain('不能移动');
  });

  it('鱼骨：同一根骨上的原因可以互相拖', () => {
    expect(moveScopeProblem(fish(), 'p1', 'p2')).toBeNull();
  });

  it('鱼骨：跨骨拒，理由说的是"类别是这张图的骨架"', () => {
    const reason = moveScopeProblem(fish(), 'p1', 'm1');
    expect(reason).toBeTruthy();
    expect(reason).toContain('骨');
    expect(reason).toContain('骨架');
  });

  it('节点不存在时放行给 `planDrop` 去说（它那句文案更准：可能刚被删掉）', () => {
    expect(moveScopeProblem(flow(), 'gone', 'g1')).toBeNull();
  });
});

// ---------------------------------------------------------------- 二、写回

describe('流程图的拖动：容器内重排真的落进 content', () => {
  it('同阶段内换位次：content 变了、可撤销、落下的是新结构的坐标', () => {
    const store = useMindMapStore();
    store.preview(flow(), 'right');

    const result = store.moveNodeTo({ draggedId: 'f2', targetId: 'f1', zone: 'sibling-before' });
    expect(result.ok).toBe(true);
    expect(store.undoDepth).toBe(1);

    // 内容：`f2` 排到 `f1` 前面，而且**还在同一个阶段里**（跨阶段是被拒的那一档）。
    const stage = (store.content as unknown as FlowContent).stages[0];
    expect(stage.root.children.map(child => child.id)).toEqual(['f2', 'f1']);

    // 坐标：落在 `layoutFlow(新图)` 给的位置上，不是"旧位置 + 位移"。
    const next = store.content as unknown as FlowContent;
    const geometry = layoutFlow(canvasTreeOf(next).root, []);
    for (const id of ['f1', 'f2']) {
      const rendered = store.nodes.find(node => node.id === id)!;
      const center = geometry.positions.get(id)!;
      expect(rendered.position.x, `${id}.x`).toBeCloseTo(center.x - NODE_SIZE.width / 2, 5);
      expect(rendered.position.y, `${id}.y`).toBeCloseTo(center.y - NODE_SIZE.height / 2, 5);
    }
  });

  it('跨阶段：不写 content、不记历史，状态行说清为什么', () => {
    const store = useMindMapStore();
    store.preview(flow(), 'right');
    const before = JSON.stringify(store.content);

    const result = store.moveNodeTo({ draggedId: 'f1', targetId: 'g1', zone: 'child' });
    expect(result.ok).toBe(false);
    expect(result.code).toBe('cross-container');
    expect(JSON.stringify(store.content)).toBe(before);
    expect(store.undoDepth).toBe(0);
    expect(store.status).toContain('悬空');
  });
});

describe('鱼骨图的拖动：同骨内重排 / 跨骨拒绝', () => {
  it('同骨内换位次真的落下去', () => {
    const store = useMindMapStore();
    store.preview(fish(), 'right');

    const result = store.moveNodeTo({ draggedId: 'p2', targetId: 'p1', zone: 'sibling-before' });
    expect(result.ok).toBe(true);
    const bone = (store.content as unknown as FishboneContent).bones[0];
    expect(bone.root.children.map(child => child.id)).toEqual(['p2', 'p1']);
  });

  it('跨骨：拒，且 content 一个字没动', () => {
    const store = useMindMapStore();
    store.preview(fish(), 'right');
    const before = JSON.stringify(store.content);

    const result = store.moveNodeTo({ draggedId: 'p1', targetId: 'm1', zone: 'child' });
    expect(result.ok).toBe(false);
    expect(result.code).toBe('cross-container');
    expect(JSON.stringify(store.content)).toBe(before);
    expect(store.status).toContain('骨架');
  });

  it('容器根拖不动（与 `draggable` 那道 gate 同一条判据的兜底）', () => {
    const store = useMindMapStore();
    store.preview(fish(), 'right');

    const result = store.moveNodeTo({ draggedId: 'p', targetId: 'p1', zone: 'child' });
    expect(result.ok).toBe(false);
    expect(result.code).toBe('cross-container');
  });
});

// ---------------------------------------------------------------- 三、落点几何

describe('幽灵框的落点用**这张图自己的**布局算', () => {
  it('鱼骨：预演位置等于 layoutFishbone 给的坐标，而不是树布局的', () => {
    const store = useMindMapStore();
    store.preview(fish(), 'right');

    const preview = store.previewMove({ draggedId: 'p2', targetId: 'p1', zone: 'sibling-before' });
    expect(preview.ok).toBe(true);

    // 在**假设的新树**上算一次，与预演必须逐像素相等（那个"画在哪儿就落在哪儿"
    // 的承诺）。用 `layoutFishbone` 而不是 tidy：这张图是鱼骨，落点只有鱼骨的
    // 几何说得准——上一轮这里硬编码 `layoutTidy`，幽灵框会落在主骨上方几十像素的
    // 空气里。
    const base = fish();
    const next: FishboneContent = {
      ...base,
      bones: [
        {
          ...base.bones[0],
          root: {
            ...base.bones[0].root,
            children: [
              { id: 'p2', label: '审批人多', children: [] },
              { id: 'p1', label: '人手不足', children: [] },
            ],
          },
        },
        base.bones[1],
      ],
    };
    const expected = layoutFishbone(canvasTreeOf(next).root).positions.get('p2')!;
    expect(preview.rect!.x).toBeCloseTo(expected.x - NODE_SIZE.width / 2, 5);
    expect(preview.rect!.y).toBeCloseTo(expected.y - NODE_SIZE.height / 2, 5);
    // 反证：拖动前后这个节点的 y 是**不同**的（不然这条用例证明不了用的是哪个函数）。
    const wrong = layoutFishbone(canvasTreeOf(fish()).root).positions.get('p2')!;
    expect(Math.abs(expected.y - wrong.y)).toBeGreaterThan(1);
  });

  it('判定节点（菱形）的幽灵框按它自己的外接框画，不是 180×48', () => {
    const store = useMindMapStore();
    const content = flow();
    // 把**被拖的那一个**标成判定：`layoutFlow` 会给它 220×110，而幽灵框必须与
    // 真实落下的那个框同尺寸——小一圈的幽灵框读起来像"差一点没对准"。
    content.stages[0].root.children[1].role = 'decision';
    store.preview(content, 'right');

    const preview = store.previewMove({ draggedId: 'f2', targetId: 'f1', zone: 'sibling-before' });
    expect(preview.ok).toBe(true);
    expect(preview.rect!.width).toBe(220);
    expect(preview.rect!.height).toBe(110);
  });
});

// ---------------------------------------------------------------- 四、旋转（direction）
//
// 「旋转」这一轮的结论是**选 A（整棵树的朝向）**，而 A 早就在了（左上角四个按钮 +
// `store.setDirection`）。本组用例钉的是它的边界：`direction` 只有树有，鱼骨与流程
// 的排布是定死的——对它们点朝向不能**静默改内容**（那会凭空多一步撤销 + 一个未保存
// 标记，而画布一个像素都不动）。

describe('旋转：朝向只对树有效，对鱼骨/流程图是"说一句"而不是"偷偷改内容"', () => {
  it('树：换朝向是一次可撤销的内容改动', () => {
    const store = useMindMapStore();
    store.preview({ title: 't', root: tree() }, 'right');
    expect(store.direction).toBe('right');

    store.setDirection('down');
    expect(store.direction).toBe('down');
    expect(store.undoDepth).toBe(1);

    store.undo();
    expect(store.direction).toBe('right');
  });

  it('鱼骨：点朝向不写 content、不进撤销栈，只给一句话', () => {
    const store = useMindMapStore();
    store.preview(fish(), 'right');
    const before = JSON.stringify(store.content);

    store.setDirection('left');
    expect(JSON.stringify(store.content)).toBe(before);
    expect(store.undoDepth).toBe(0);
    expect(store.status).toContain('固定的');
    // 画布的朝向读的仍是内容里的派生值，一个像素都没动。
    expect(store.direction).toBe('right');
  });

  it('流程图：同上（它的分层永远是上下，与用户选什么无关）', () => {
    const store = useMindMapStore();
    store.preview(flow(), 'right');
    const before = JSON.stringify(store.content);

    store.setDirection('down');
    expect(JSON.stringify(store.content)).toBe(before);
    expect(store.undoDepth).toBe(0);
    expect(store.status).toContain('固定的');
  });

  it('拖动的方位换算用**布局自己报的**朝向：鱼骨恒为 right，与那张图上的 theme 无关', () => {
    const store = useMindMapStore();
    // 库里可能有一张 `theme.direction: 'both'` 的鱼骨（历史数据，或用户在别的版本里
    // 点过）。画布的方位换算必须看**布局报的**那一个，不看这个字段。
    store.preview(fish(), 'both');

    expect(store.direction).toBe('both');
    // 布局自己报的朝向才是拖动的换算依据（`MindMapCanvas.zoneDirection`）。
    expect(store.layout?.direction).toBe('right');
  });
});
