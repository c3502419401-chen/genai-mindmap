import { beforeAll, describe, expect, it } from 'vitest';

import type { MindNode } from '../src/api/types';
import { layoutDiagram } from '../src/canvas/layout';
import {
  boxesOf,
  boxesOfNodes,
  branchWidths,
  chooseViewMode,
  measureLayout,
  overlappingPairs,
  parentIndex,
  visibleIds,
  type Box,
} from '../src/canvas/clarity';
import {
  MIN_READABLE_SCALE,
  NODE_FONT_PX,
  contentBounds,
  fitAllScale,
} from '../src/canvas/viewport';

/**
 * 「现在的布局在什么情况下不清晰」——**用真图量**，以及判据本身的用例。
 *
 * ## 两层，各管一件事
 *
 *  - 第一层（`describe('判据')`）：合成图，钉住五个指标与选档判据的**定义**。
 *    这些是本轮要长期成立的东西，不依赖任何样本文件。
 *  - 第二层（`describe('真图实测')`）：读 `backend/scripts/measure_layout_clarity.py`
 *    导出的样本，按三套几何各算一遍，把表打到 stdout。样本是临时产物（`_` 前缀，
 *    不进提交），没有样本时这一层**跳过**而不是失败——CI 上不该因为缺一份本地数据
 *    就红。
 *
 * ## 为什么几何必须在**这份**用例里算
 *
 * `layoutDiagram` 是线上唯一的分派入口（`stores/mindmap.ts::layout` 调它）。测量若
 * 自己写一遍坐标算术，量出来的就是那个重写版的行为，而线上是另一个——这正是
 * "测出来没问题、用户看到糊成一团"的来源。所以这里一行几何都不自己算。
 *
 * `layoutDiagram` 不许被改动，这条用例是**读者**不是守护者：三套几何一个字节都没
 * 碰（`TestTreeIsByteIdentical` 那一类用例继续钉着逐像素）。
 */

/** 线上画布尺寸：与 `tests/canvas-viewport.spec.ts` 同一个数（1600 宽的画布）。 */
const VIEWPORT = { width: 1600, height: 852 };

interface Sample {
  key: string;
  source: string;
  title: string;
  diagram_type: 'tree' | 'fishbone' | 'flowchart';
  root: MindNode;
  edges: Array<{ id: string; source: string; target: string }>;
  nodes: number;
  depth: number;
  branches: number[];
  relations: number;
}

/**
 * 样本用 `import.meta.glob` 读，不用 `node:fs`。
 *
 * 这不是绕：本包 tsconfig 的 `types` 只有 `vite/client`（浏览器侧视图），为一份测试
 * 数据引入 `@types/node` 不划算（`tests/ops-contract.spec.ts` 早就为同一件事这么决定
 * 过）。`glob` 匹配不到文件时返回空对象——样本不存在就跳过，正是想要的。
 */
const SAMPLE_MODULES = import.meta.glob<{ default: { samples: Sample[] } }>(
  '../../backend/scripts/_layout_clarity_sample.json',
);

let samples: Sample[] = [];

beforeAll(async () => {
  const loaders = Object.values(SAMPLE_MODULES);
  if (!loaders.length) return;
  const module = await loaders[0]();
  samples = module.default?.samples ?? [];
});

function box(id: string, x: number, y: number, width = 180, height = 48): Box {
  return { id, x, y, width, height };
}

function leaf(id: string, label = id): MindNode {
  return { id, label, children: [] };
}

describe('判据：五个指标的定义', () => {
  it('① 重叠是严格相交，相切不算（紧凑排布不该被算成重叠）', () => {
    // tidy 的兄弟间隙是 20：两个 180 宽的节点横向隔 20，边缘不相接。
    expect(overlappingPairs([box('a', 0, 0), box('b', 200, 0)])).toEqual([]);
    // 边界刚好相接（x = 180）：
    expect(overlappingPairs([box('a', 0, 0), box('b', 180, 0)])).toEqual([]);
    // 压过去 1 像素：
    expect(overlappingPairs([box('a', 0, 0), box('b', 179, 0)])).toEqual([['a', 'b']]);
  });

  it('② 越界看的是矩形相交：露出一点的节点算"看得见"', () => {
    const boxes = [box('in', 0, 0), box('edge', 1590, 0), box('out', 5000, 0)];
    // 视口 1600 宽、缩放 1、偏移 0 → 画布坐标 [0, 1600] 可见。
    const plan = { x: 0, y: 0, zoom: 1 };
    expect(visibleIds(boxes, VIEWPORT, plan)).toEqual(['in', 'edge']);
  });

  it('③ 层距是从布局结果反推的：父子中心在层级轴上的最小投影距离', () => {
    const root: MindNode = {
      id: 'r', label: '根', children: [
        { id: 'a', label: 'A', children: [leaf('a1'), leaf('a2')] },
        leaf('b'),
      ],
    };
    const layout = layoutDiagram({ root, diagramType: 'tree' });
    const metrics = measureLayout({ root, layout, viewport: VIEWPORT });
    // tidy：层距 = 节点宽 180 + depthGap 56 = 236，所有父子边都恰好是这个距离。
    // **不能用欧氏距离**：子节点被居中摆开，最近的一对父子是 238.44 而不是 236。
    expect(metrics.axis).toBe('x');
    expect(metrics.nominalGap).toBeCloseTo(236, 6);
    expect(metrics.longEdges).toBe(0);
    expect(metrics.depth).toBe(3);
  });

  it('④ 层宽按内容层数（根 = 1），与几何坐标无关', () => {
    const root: MindNode = {
      id: 'r', label: '根', children: [
        { id: 'a', label: 'A', children: [leaf('a1'), leaf('a2'), leaf('a3')] },
        leaf('b'),
      ],
    };
    const layout = layoutDiagram({ root, diagramType: 'tree' });
    const metrics = measureLayout({ root, layout, viewport: VIEWPORT });
    expect(metrics.layers).toEqual([1, 2, 3]);
    expect(metrics.widestLayer).toBe(3);
  });

  it('⑤ 分支宽度是每条一级分支的子节点数', () => {
    const root: MindNode = {
      id: 'r', label: '根', children: [
        { id: 'a', label: 'A', children: [leaf('a1'), leaf('a2')] },
        { id: 'b', label: 'B', children: [leaf('b1')] },
        leaf('c'),
      ],
    };
    expect(branchWidths(root)).toEqual([2, 1, 0]);
  });

  it('外接框：positions 是中心点，尺寸取 layout.sizes（流程图判定节点 220×110）', () => {
    const root: MindNode = { id: 'r', label: '根', children: [leaf('a'), leaf('b')] };
    const layout = layoutDiagram({ root, diagramType: 'tree' });
    const boxes = boxesOf(layout);
    expect(boxes.length).toBe(3);
    // 树布局没有 `sizes`，全部回落 NODE_SIZE。
    for (const item of boxes) {
      expect(item.width).toBe(180);
      expect(item.height).toBe(48);
    }
    // 逐框拼出来的外接框必须等于布局自己报的 bounds（两处不能分叉）。
    const bounds = contentBounds(boxes.map(item => ({
      position: { x: item.x, y: item.y },
      data: { size: { width: item.width, height: item.height } },
    })))!;
    expect(bounds.width).toBeCloseTo(layout.bounds.width, 6);
    expect(bounds.height).toBeCloseTo(layout.bounds.height, 6);
  });

  it('画布上的节点（左上角 + data.size）与布局结果走同一个盒子口径', () => {
    const boxes = boxesOfNodes([
      { position: { x: 10, y: 20 } },
      { position: { x: 300, y: 20 }, data: { size: { width: 220, height: 110 } } },
    ]);
    expect(boxes[0]).toMatchObject({ x: 10, y: 20, width: 180, height: 48 });
    // 判定节点的菱形比 NODE_SIZE 大，画布这一路必须读它自己的尺寸。
    expect(boxes[1]).toMatchObject({ x: 300, y: 20, width: 220, height: 110 });
  });

  it('父子索引与深度索引覆盖整棵树', () => {
    const root: MindNode = {
      id: 'r', label: '根', children: [
        { id: 'a', label: 'A', children: [leaf('a1')] },
      ],
    };
    expect([...parentIndex(root)]).toEqual([['a', 'r'], ['a1', 'a']]);
  });
});

describe('判据：生成后该用哪一档视图', () => {
  it('小图：整张能装下且字号够大 → fit-all（看全不吃亏）', () => {
    const boxes = [box('r', 0, 0), box('a', 236, -24), box('b', 236, 24)];
    const choice = chooseViewMode({ boxes, viewport: VIEWPORT });
    expect(choice.mode).toBe('fit-all');
    expect(choice.offscreen).toBe(0);
    expect(choice.fontPx).toBeGreaterThanOrEqual(NODE_FONT_PX * MIN_READABLE_SCALE);
  });

  it('竖长型的图：横向装得下也不是 fit-all —— 瓶颈在高度', () => {
    // `tests/canvas-viewport.spec.ts` 里那棵 42 节点的树的真实外接框：
    // 宽 909（< 1334，横向装得下），高 1952。
    const bounds = { x: -90, y: -844, width: 909, height: 1952 };
    const boxes = [box('a', bounds.x, bounds.y), box('b', bounds.x + 729, bounds.y + 1904)];
    const choice = chooseViewMode({ boxes, viewport: VIEWPORT, bounds });
    expect(choice.mode).toBe('fit-width');
    expect(choice.fontPx).toBeCloseTo(NODE_FONT_PX * MIN_READABLE_SCALE, 6);
    // 字面判据「节点数 × 每节点宽 > 画布宽 × 可读比例」在这张图上会判成 fit-all，
    // 而它横向装得下 —— 这就是判据落在 `fitAllScale` 上的理由。
    expect(fitAllScale(bounds, VIEWPORT)).toBeLessThan(MIN_READABLE_SCALE);
    expect(bounds.width * MIN_READABLE_SCALE).toBeLessThan(VIEWPORT.width);
  });

  it('空画布 → fit-width（不制造一个"全图"的假结论）', () => {
    expect(chooseViewMode({ boxes: [], viewport: VIEWPORT }).mode).toBe('fit-width');
  });

  it('装不下时给出"还有几个节点在视野外"，且那个数是真的', () => {
    const boxes: Box[] = [];
    for (let index = 0; index < 40; index += 1) {
      boxes.push(box(`n${index}`, 0, index * 100));
    }
    const choice = chooseViewMode({ boxes, viewport: VIEWPORT });
    expect(choice.mode).toBe('fit-width');
    expect(choice.offscreen).toBeGreaterThan(0);
    expect(choice.offscreen).toBeLessThan(boxes.length);
    // 一屏高度 852 / 0.857 = 994px，节点行距 100 → 看得见 10 行左右。
    const visible = boxes.length - choice.offscreen;
    expect(visible).toBeGreaterThanOrEqual(9);
    expect(visible).toBeLessThanOrEqual(12);
  });
});

/** 一张图在三套几何下的两档缩放与五个指标。 */
interface GeometryRow {
  key: string;
  title: string;
  diagram_type: string;
  nodes: number;
  depth: number;
  geometry: 'tidy' | 'fishbone' | 'flow';
  overlapPairs: number;
  longEdges: number;
  widestLayer: number;
  widestBranch: number;
  boundsWidth: number;
  boundsHeight: number;
  fitAllZoom: number;
  readableAtFitAll: boolean;
  /** `fit-all` 那一档仍被裁到视口外的节点数（夹到 `MIN_ZOOM = 0.2` 都装不下时非零）。 */
  offscreenAtFitAll: number;
  chosen: 'fit-width' | 'fit-all';
  chosenOffscreen: number;
  fontPx: number;
}

function rowOf(sample: Sample, geometry: 'tidy' | 'fishbone' | 'flow'): GeometryRow {
  const layout = layoutDiagram({
    root: sample.root,
    edges: sample.edges,
    diagramType: geometry === 'tidy' ? 'tree' : geometry === 'fishbone' ? 'fishbone' : 'flowchart',
  });
  const metrics = measureLayout({ root: sample.root, layout, viewport: VIEWPORT });
  const boxes = boxesOf(layout);
  const choice = chooseViewMode({ boxes, viewport: VIEWPORT });
  return {
    key: sample.key,
    title: sample.title,
    diagram_type: sample.diagram_type,
    nodes: metrics.nodes,
    depth: metrics.depth,
    geometry,
    overlapPairs: metrics.overlapPairs,
    longEdges: metrics.longEdges,
    widestLayer: metrics.widestLayer,
    widestBranch: Math.max(0, ...metrics.branchWidths),
    boundsWidth: Math.round(metrics.bounds.width),
    boundsHeight: Math.round(metrics.bounds.height),
    fitAllZoom: Number(metrics.fitAllZoom.toFixed(4)),
    readableAtFitAll: metrics.readableAtFitAll,
    offscreenAtFitAll: metrics.offscreen,
    chosen: choice.mode,
    chosenOffscreen: choice.offscreen,
    fontPx: Number(choice.fontPx.toFixed(2)),
  };
}

function median(values: number[]): number {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)];
}

describe('真图实测', () => {
  it('把三套几何的分布写成表（含每档的"有问题"比例）', ctx => {
    if (!samples.length) {
      ctx.skip();
      return;
    }
    const rows: GeometryRow[] = [];
    for (const sample of samples) {
      for (const geometry of ['tidy', 'fishbone', 'flow'] as const) {
        rows.push(rowOf(sample, geometry));
      }
    }
    const geometryRows = (geometry: 'tidy' | 'fishbone' | 'flow') =>
      rows.filter(row => row.geometry === geometry);
    const count = (group: GeometryRow[], predicate: (row: GeometryRow) => boolean) =>
      group.filter(predicate).length;

    const summary = (['tidy', 'fishbone', 'flow'] as const).map(geometry => {
      const group = geometryRows(geometry);
      return {
        geometry,
        maps: group.length,
        overlapMaps: count(group, row => row.overlapPairs > 0),
        overlapPairs: group.reduce((sum, row) => sum + row.overlapPairs, 0),
        clippedMaps: count(group, row => row.offscreenAtFitAll > 0),
        clippedNodes: group.reduce((sum, row) => sum + row.offscreenAtFitAll, 0),
        longEdgeMaps: count(group, row => row.longEdges > 0),
        widestLayer: Math.max(...group.map(row => row.widestLayer)),
        medianWidestLayer: median(group.map(row => row.widestLayer)),
        widestBranch: Math.max(...group.map(row => row.widestBranch)),
        medianWidestBranch: median(group.map(row => row.widestBranch)),
        unreadableAtFitAll: count(group, row => !row.readableAtFitAll),
        chosenFitWidth: count(group, row => row.chosen === 'fit-width'),
        medianFontPxAtChosen: Number(median(group.map(row => row.fontPx)).toFixed(2)),
      };
    });

    const sized = geometryRows('tidy');
    const buckets = [
      { label: '<40', test: (row: GeometryRow) => row.nodes < 40 },
      { label: '40–79', test: (row: GeometryRow) => row.nodes >= 40 && row.nodes < 80 },
      { label: '≥80', test: (row: GeometryRow) => row.nodes >= 80 },
    ].map(bucket => {
      const group = sized.filter(bucket.test);
      return {
        bucket: bucket.label,
        maps: group.length,
        unreadable: count(group, row => !row.readableAtFitAll),
        chosenFitWidth: count(group, row => row.chosen === 'fit-width'),
        medianFontPx: Number(median(group.map(row => row.fontPx)).toFixed(2)),
      };
    });

    // "真实场景"= 每张图按**它自己的** `diagram_type` 排。三套几何全跑是**对照实验**
    // （把树内容塞进鱼骨/流程几何），两者要分开看：前者是用户看到的，后者才回答
    // "问题在内容还是在几何"。
    const nativeOf: Record<string, 'tidy' | 'fishbone' | 'flow'> = {
      tree: 'tidy', fishbone: 'fishbone', flowchart: 'flow',
    };
    const native = rows.filter(row => nativeOf[row.diagram_type] === row.geometry);

    console.log([
      ['几何', '图', '重叠', '越界(fit-all)', '超长边', '层宽中位/最大', '分支宽中位/最大',
        'fit-all 不可读', '选 fit-width'].join('\t'),
      ...summary.map(item => [
        item.geometry,
        item.maps,
        `${item.overlapMaps} 张 / ${item.overlapPairs} 对`,
        `${item.clippedMaps} 张 / ${item.clippedNodes} 个`,
        `${item.longEdgeMaps} 张`,
        `${item.medianWidestLayer} / ${item.widestLayer}`,
        `${item.medianWidestBranch} / ${item.widestBranch}`,
        `${item.unreadableAtFitAll} 张`,
        `${item.chosenFitWidth} 张`,
      ].join('\t')),
    ].join('\n'));
    console.log('\n按规模（tidy 几何）：');
    for (const bucket of buckets) {
      console.log(`  ${bucket.bucket} 节点：${bucket.maps} 张，fit-all 不可读 ${bucket.unreadable} 张，`
        + `自动选 fit-width ${bucket.chosenFitWidth} 张，字号中位 ${bucket.medianFontPx}px`);
    }
    console.log(`\n真实图型（native）：${native.length} 张，重叠 `
      + `${count(native, row => row.overlapPairs > 0)} 张，超大 ` 
      + `${count(native, row => row.longEdges > 0)} 张，fit-all 不可读 `
      + `${count(native, row => !row.readableAtFitAll)} 张`);

    const worst = [...sized].sort((a, b) => b.nodes - a.nodes).slice(0, 5);
    console.log('\n最大的 5 张（tidy）：');
    for (const row of worst) {
      console.log(`  ${row.nodes} 节点  ${row.boundsWidth}×${row.boundsHeight}`
        + `  fit-all ${row.fitAllZoom}（字号 ${(NODE_FONT_PX * row.fitAllZoom).toFixed(2)}px）`
        + `  层宽 ${row.widestLayer}  ${row.title.slice(0, 20)}`);
    }

    expect(rows.length).toBe(samples.length * 3);
    expect(rows.every(row => Number.isFinite(row.fitAllZoom))).toBe(true);
  });

  it('那张 56 节点的图：自动切到 fit-width，且"视野外"的数是真的', ctx => {
    if (!samples.length) {
      ctx.skip();
      return;
    }
    const rows = samples.map(sample => rowOf(sample, 'tidy')).filter(row => row.nodes === 56);
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      console.log(`56 节点：${row.title.slice(0, 18)}  ${row.boundsWidth}×${row.boundsHeight}`
        + `  fit-all ${row.fitAllZoom} → 字号 ${(NODE_FONT_PX * row.fitAllZoom).toFixed(2)}px`
        + `；选 ${row.chosen}，字号 ${row.fontPx}px，视野外 ${row.chosenOffscreen} / ${row.nodes}`);
      expect(row.chosen).toBe('fit-width');
      expect(row.readableAtFitAll).toBe(false);
      expect(NODE_FONT_PX * row.fitAllZoom).toBeLessThan(NODE_FONT_PX * MIN_READABLE_SCALE);
      expect(row.fontPx).toBeCloseTo(NODE_FONT_PX * MIN_READABLE_SCALE, 2);
      expect(row.chosenOffscreen).toBeGreaterThan(0);
      expect(row.chosenOffscreen).toBeLessThan(row.nodes);
    }
  });
});
