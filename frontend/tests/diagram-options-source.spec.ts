import { describe, expect, it } from 'vitest';

// 这三份是**文本**，不是模块：`schema.d.ts` 是 `npm run gen:api` 的产物，另两份是
// 源码。要断言的是"文本里有没有那串字面量"，所以按 `?raw` 读原文（vite 的
// `*?raw` ambient 声明由 `vite/client` 提供，无需 @types/node）。
import schemaSource from '../src/api/schema.d.ts?raw';
import typesSource from '../src/api/types.ts?raw';
import storeSource from '../src/stores/options.ts?raw';
import { DIAGRAM_TYPE_OPTIONS } from '../src/stores/options';

/**
 * 「图型」这三样东西的单一源：**值域、中文名、说明**各走各的路，但每一条都要红。
 *
 * 上一轮修的是「拆解方式」的同一条病（`docs/WIRING-GAPS.md` 第七节），当时明确留了
 * 这一处没修，原话是"值域仍靠手写的 `api/types.ts::DiagramType`，与 `schema.d.ts`
 * 的 union 之间没有任何用例连着；修法与这轮同一条，但要先决定 hint 放哪侧"。
 *
 * 这一轮判完了：**值域跟生成物走、中文名跟生成物走、`hint` 留界面**。判据不是偏好，
 * 是后端自己的注释——`contracts/mindmap.py:154-157` 写着"只放中文名，不放说明"。
 *
 * 四条用例分别钉住这条链上的四个接缝：
 *
 * 1. `api/types.ts` 里不许再出现手写的图型并集（那是这次修掉的形状，不该长回来）；
 * 2. 下拉里的每一项必须在 `schema.d.ts` 的 union 里，且顺序一致（后端加图型 →
 *    这里红，除非重跑 gen:api 并补上文案）；
 * 3. 中文名不许在 store 里重抄一份（唯一真源是后端 `DIAGRAM_LABELS` 的生成物）；
 * 4. `auto` 只活在界面里（它是"我不说"，不是一个图型）。
 *
 * 注意这三份文件里有两份是**生成的**：`schema.d.ts` 由 `npm run gen:api` 从
 * `backend/openapi.json` 生成，`diagram-labels.json` 由
 * `backend/scripts/export_diagram_labels.py` 生成。用例读它们的文本，不读类型——
 * 类型那半由 `npm run type-check` 管（比用例早，也比用例硬）。
 */

/**
 * 从 `schema.d.ts` 里抠出请求侧的图型 union。
 *
 * 只找 `AgentRunRequest` 里那一行 `diagram_type?: ("tree" | "flowchart" | ...) | null`：
 * 产物侧（`MindMapContent-Output`）另有一行同名同值的，取第一个（请求侧）就够——
 * 它是下拉真正要发出去的那个字段。
 */
function schemaDiagramTypes(): string[] {
  const match = schemaSource.match(/diagram_type\?:\s*\(([^)]*)\)\s*\|\s*null/);
  if (!match) throw new Error('schema.d.ts 里找不到 AgentRunRequest.diagram_type 的 union');
  return match[1].split('|').map(item => item.trim().replace(/^"|"$/g, ''));
}

describe('图型的值域来自生成物', () => {
  it('api/types.ts 不再手写那份并集', () => {
    // 手写并集长这样：`export type DiagramType = 'tree' | 'flowchart' | 'fishbone';`
    // 它不会跟着 schema.d.ts 变，所以后端加图型时它是**沉默**的那一环。
    expect(typesSource).not.toMatch(/export type DiagramType\s*=\s*['"]tree['"]/);
    // 它现在应该是从生成物推导的
    expect(typesSource).toMatch(/export type DiagramType = NonNullable</);
    expect(typesSource).toMatch(/\['AgentRunRequest'\]\['diagram_type'\]/);
  });

  it('生成物的请求侧与产物侧同宽（同一次导出，不该只有一侧变）', () => {
    const requestSide = schemaDiagramTypes();
    // 产物侧是必填、不带 null 的同一个 union。这次改动没动它，加个复核而已：
    // 若后端往请求侧加了一种、产物侧的 `MindMapContent.diagram_type` 却没跟着，
    // 这一条会红。
    const contentSide = schemaSource.match(/diagram_type: "tree" \| "flowchart" \| "fishbone"/);
    expect(contentSide, '产物侧的 diagram_type 已经不是三种图型了').not.toBeNull();
    expect(requestSide).toEqual(['tree', 'flowchart', 'fishbone']);
  });
});

describe('下拉与生成物对得上', () => {
  it('每一项都在生成物的 union 里，并集也覆盖全部（少一条/多一条都红）', () => {
    const union = schemaDiagramTypes();
    const offered = DIAGRAM_TYPE_OPTIONS.map(item => item.value).filter(value => value !== 'auto');

    // 顺序也一起断言：下拉的展示顺序是 UI 契约的一部分（`tree` 最常用，排第一）。
    // 这个顺序从 `DIAGRAM_TYPE_COPY` 的书写顺序来，与后端 `DIAGRAM_LABELS` 同序。
    expect(offered).toEqual(union);
  });

  it('每个图型都有中文名与说明，而且中文名不是在这里写的', () => {
    for (const option of DIAGRAM_TYPE_OPTIONS) {
      expect(option.label.trim(), `${option.value} 没有中文名`).not.toBe('');
      expect(option.hint.trim(), `${option.value} 没有说明`).not.toBe('');
    }
    // 中文名的唯一真源是 `diagram-labels.json`（后端 `DIAGRAM_LABELS` 的生成物）。
    // 这里防的是"有人把副本抄回来"——那正是这次修掉的形状。
    // 只查 `label: '…'` 这种写法：`auto` 的 hint 里可以有"思维导图"四个字
    // （它在解释回退行为），那不是副本。
    for (const label of ['思维导图', '流程图', '鱼骨图']) {
      expect(storeSource).not.toMatch(new RegExp(`label:\\s*['"]${label}['"]`));
    }
    // 它 import 了生成物
    expect(storeSource).toMatch(/from '\.\.\/api\/diagram-labels\.json'/);
  });

  it('「自动判断」只活在界面里：它不在 union 里，也不发给后端', () => {
    expect(DIAGRAM_TYPE_OPTIONS[0].value).toBe('auto');
    expect(schemaDiagramTypes()).not.toContain('auto');
    // 后端 `diagram_type` 的类型是 `DiagramKind | None`，None 就是"用户没说"。
    // 发一个字符串 'auto' 出去等于给后端多一种要理解的状态。
    expect(storeSource).not.toMatch(/diagram_type.*'auto'/);
  });
});
