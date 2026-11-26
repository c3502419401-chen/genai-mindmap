import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

import thinkingOptions from '../api/thinking-options.json';
import diagramLabels from '../api/diagram-labels.json';
import type { DiagramType, DiagramTypeChoice } from '../api/types';
import { DIAGRAM_PREF_KEY, THINKING_PREF_KEY } from './prefs';

/**
 * 「这次新建要生成成什么样」的两个选项：拆解方式与图型。
 *
 * 它们从 `SourceInputs` 里搬到了这个 store，理由只有一条：**下拉与消费它的按钮
 * 不再在同一个组件里**。材料区只剩下材料，按钮在画布右上角（`App.vue` 的 #2），
 * 两个组件都要读同一份值，所以状态必须有单一归属，不能各存一份再靠 props 同步。
 *
 * 两份选项的来源**不一样**，这一点必须分开说，否则下一个人会以为它们同一种做法：
 *
 * · 拆解方式：值**和**中文名都在后端（`THINKING_MODELS`），这里只 import 生成物
 *   （`api/thinking-options.json`）；
 * · 图型：只有值域在后端（`DiagramKind` 的 Literal），中文名与 `hint` 是纯表现层
 *   的东西，后端没有对应字段可抄。但**值域也不再手写**：它走
 *   `openapi.json → schema.d.ts → api/types.ts::DiagramType → 这里的文案表`，
 *   这条链的最后一环是 `Record<DiagramType, …>`，少一个图型就是编译错误。
 *
 * 曾经两句都写成"写死在前端、两边不一致时后端会 422"。那句话**只对了半句**：
 * 422 挡得住拼错的值，挡不住"后端多了一种、前端这份没有"——那种情况前端不报错、
 * 用户也选不到，唯一的发现途径是有人去读后端那张表。两句都修过了：拆解方式整个
 * 改成生成物，图型让值域跟着生成物走、把"少一条"变成编译期红灯。细节见
 * `docs/WIRING-GAPS.md` 第十一节。
 */

/**
 * 拆解方式的全部候选：**值与中文名都不在这里定义**。
 *
 * 这是本文件里唯一一处"不该有字面量"的地方，成因与修法都值得留一句：
 *
 * 后端 `generation.service.THINKING_MODELS` 是这张表的唯一真源（提示词在那边，
 * 标签也在那边），而此前前端抄了一份同值同名的副本。于是后端加一种思考方式时，
 * 下拉不变、界面不报错、用户永远选不到——**静默不一致**，比功能缺失更难发现。
 *
 * 现在的通路是构建期的：`backend/scripts/export_thinking_options.py` 把后端那张表
 * 写成 `api/thinking-options.json`（提交、可 review），这里只 `import` 它。
 * 之所以不走接口：这个下拉就在生成按钮旁边，**首屏就要可用**，多一次往返意味着
 * 要么空着、要么再抄一份兜底（那就又变成两份定义源）。值与后端
 * `AgentRunRequest.thinking_model` 的 Literal 是否同步，由后端用例钉着；
 * 产物是否过期，由 `backend/tests/test_thinking_options.py` 钉着。
 */
export const THINKING_MODEL_OPTIONS: readonly { value: string; label: string }[] = thinkingOptions;

/**
 * 图型。`auto` = 不传字段（后端 `None`，让模型自己判断）——它是默认值，也是
 * "用户没说"这件事在界面上唯一诚实的呈现：不选，就由生成那一次决定。
 *
 * ## 三样东西，三个来源，一个都不能混
 *
 * · **值域**：`api/types.ts::DiagramType`，从生成物 `schema.d.ts` 推导，
 *   唯一真源是后端 `DiagramKind` 的 Literal；
 * · **中文名**：生成物 `api/diagram-labels.json`，唯一真源是后端
 *   `contracts/mindmap.py::DIAGRAM_LABELS`（导出文件头也在用它，所以它本来就得
 *   在后端）；
 * · **`hint`**：只在这里。后端没有对应字段，也不该有——`mindmap.py:154-157` 的
 *   注释已经把这条判过了："**只放中文名，不放说明**——说明属于界面"。
 *
 * 为什么 hint 不跟着名字一起进后端表：`hint` 说的是"选了它，界面上会发生什么"，
 * 只有写界面的人知道该说多少。搬到后端换不到"加图型零改动"——新图型的中文说明
 * 总得有人写，差别只是写哪个文件，不是要不要写。
 *
 * `hint` 是**必须**的：图型不是自解释的选项。用户选「流程图」时，他期待的是
 * "一级分支按阶段排、判断节点分是/否"，而不是"换个颜色的思维导图"；选「鱼骨图」
 * 时更得先知道一级分支固定是那六个类别——否则他会以为模型没听懂他的主题。
 *
 * ## "少写一条"必须红，两种红都在编译期
 *
 * `Record<DiagramType, …>` 要求键集**恰好等于**值域；`diagramLabels[value]` 要求
 * 生成物里**有**这个键。后端加一个图型、重跑 `npm run gen:api` 之后，少一项就是
 * `npm run type-check` 的编译错误——不是"某个断言没通过"，是"这个表缺一项"。
 */
const DIAGRAM_TYPE_COPY: Record<DiagramType, { hint: string }> = {
  tree: { hint: '一级分支按话题分解，不画附加连线。' },
  flowchart: { hint: '一级分支会按执行阶段排，判断节点带是/否分支' },
  fishbone: { hint: '一级分支固定为人/机/料/法/环/测' },
};

/** 值域（不含 `auto`）：`DIAGRAM_TYPE_COPY` 的键，顺序即下拉顺序。 */
const DIAGRAM_VALUES = Object.keys(DIAGRAM_TYPE_COPY) as DiagramType[];

/** 下拉里的全部候选：`auto` 打头，其余按 `DIAGRAM_TYPE_COPY` 的书写顺序。 */
export const DIAGRAM_TYPE_OPTIONS: { value: DiagramTypeChoice; label: string; hint: string }[] = [
  { value: 'auto', label: '自动判断', hint: '按你的描述判断图型；判断不出时按思维导图生成。' },
  ...DIAGRAM_VALUES.map(value => ({
    value,
    label: diagramLabels[value],
    hint: DIAGRAM_TYPE_COPY[value].hint,
  })),
];

function asChoice(value: string | undefined): DiagramType | null {
  return DIAGRAM_VALUES.find(item => item === value) ?? null;
}

export const useGenerationOptions = defineStore('generationOptions', () => {
  /** 拆解方式：`default` 与后端默认值一致。 */
  const thinkingModel = ref('default');
  const diagramType = ref<DiagramTypeChoice>('auto');

  /**
   * 用户**手动改过**没有。改过之后偏好不再覆盖它。
   *
   * 这条是"预填"与"偷偷填"的分界：偏好只决定**初始值**，一旦用户动过下拉，值就
   * 是他的，刷新偏好也不该把它顶回去。没有这一位的话，用户在面板里改一条偏好会
   * 顺手改掉他刚刚手动选的图型——而那个选择才是他此刻的意图。
   */
  const touched = ref({ thinking: false, diagram: false });

  /**
   * 当前值是不是**从偏好预填来的**。
   *
   * 界面据此标「流程图（来自你的偏好）」，请求层据此记 `prefs_applied`。
   * 它与 `touched` 不是互补的两种状态——一个值可以既没被用户动过、也不是偏好
   * 预填的（那就是 `auto` / `default`，谁都没说）。
   */
  const prefilled = ref({ thinking: false, diagram: false });

  const diagramHint = computed(
    () => DIAGRAM_TYPE_OPTIONS.find(item => item.value === diagramType.value)?.hint ?? '',
  );

  /** 图型下拉里那一项的显示名（`auto` 是「自动判断」）。 */
  const diagramLabel = computed(
    () => DIAGRAM_TYPE_OPTIONS.find(item => item.value === diagramType.value)?.label ?? '',
  );

  const thinkingLabel = computed(
    () => THINKING_MODEL_OPTIONS.find(item => item.value === thinkingModel.value)?.label
      ?? thinkingModel.value,
  );

  /**
   * 把偏好填成**初始值**。用户在界面上看得见、也改得掉——这是这一层的全部要害。
   *
   * 与"后端开流前把偏好塞进请求体"是两件事：那条路会让 `runs.request.diagram_type`
   * 记下"用户选了流程图"，而用户根本没选过。预填之后请求体里带的是用户能在下拉里
   * 看见的值，那一列的语义仍然是"这次请求实际是什么"。
   */
  function applyDefaults(defaults: Record<string, string>): void {
    if (!touched.value.diagram) {
      const choice = asChoice(defaults[DIAGRAM_PREF_KEY]);
      diagramType.value = choice ?? 'auto';
      prefilled.value.diagram = choice !== null;
    }
    const thinking = defaults[THINKING_PREF_KEY];
    if (!touched.value.thinking && thinking) {
      thinkingModel.value = thinking;
      prefilled.value.thinking = true;
    }
  }

  function chooseDiagram(value: DiagramTypeChoice): void {
    diagramType.value = value;
    touched.value.diagram = true;
    prefilled.value.diagram = false;
  }

  function chooseThinking(value: string): void {
    thinkingModel.value = value;
    touched.value.thinking = true;
    prefilled.value.thinking = false;
  }

  /**
   * 忘掉"用户改过"。在「以后都这样」把当前值写成偏好之后调一次。
   *
   * 不调的话，界面上会同时存在两个真相：偏好里写着流程图、下拉却停在用户上一次
   * 手动选的思维导图，而标着"来自你的偏好"的那行说明已经消失了。
   */
  function forget(): void {
    touched.value = { thinking: false, diagram: false };
    prefilled.value = { thinking: false, diagram: false };
  }

  /**
   * 这一次请求**实际带着哪些偏好**，形如 `["diagram_type.default=fishbone"]`。
   *
   * 只算"值来自偏好、且用户没动过"的那些。用户手动选过之后再选回同一个值时这里
   * 是空的——那是他自己选的，哪怕值碰巧一样。宁可少记一条，也不要把用户的选择
   * 说成系统的预填。
   */
  function appliedKeys(): string[] {
    const keys: string[] = [];
    if (prefilled.value.diagram && diagramType.value !== 'auto') {
      keys.push(`${DIAGRAM_PREF_KEY}=${diagramType.value}`);
    }
    if (prefilled.value.thinking && thinkingModel.value) {
      keys.push(`${THINKING_PREF_KEY}=${thinkingModel.value}`);
    }
    return keys;
  }

  return {
    thinkingModel,
    diagramType,
    diagramHint,
    diagramLabel,
    thinkingLabel,
    touched,
    prefilled,
    applyDefaults,
    chooseDiagram,
    chooseThinking,
    forget,
    appliedKeys,
  };
});
