/** 后端契约的 TypeScript 镜像。字段以 backend/app/contracts/mindmap.py 为准。
 *
 *  2026-11-10 起后端是契约 v2：本文件声明了 v2 的字段，但界面只读写
 *  `note` / `layout` / `theme.direction`，其余（`relations` / `style` / `pos` /
 *  `collapsed`）只做透传保存。
 *
 *  `schema.d.ts` 已由 `openapi-typescript` 从 `backend/openapi.json` 生成，
 *  但本文件**决定保留**：手写镜像读起来比生成的类型省事，两者靠
 *  `contract-check.ts` 的编译期断言保持一致（见 `docs/CONTRACT_V2.md`）。 */
import type { components } from './schema';

export interface MindNode {
  id: string;
  /** 一句话标题（生成侧 ≤16 字）。画布上只画它；超长的旧图仍按 24 字截断显示。 */
  label: string;
  children: MindNode[];
  /**
   * 展开正文（契约 v2 字段，v1 后端不返回）。
   *
   * 语义：`label` 是短标题、`note` 承载正文；渲染时用 `note ?? label` 回退，
   * 因此 v1 阶段画布/详情面板显示的仍是 `label` 全文，v2 落地后自动切换。
   * 必须与契约 v2 同批上线：v1 的 `Node` 是 `extra="forbid"`，提前出现会 422。
   */
  note?: string | null;
  /**
   * 生成产出的说明（契约 v2 字段，上限 2000 字）。
   *
   * 与 `note` **不是同一个东西**：`detail` 是模型写的（整树生成、分片展开都会填），
   * `note` 是用户自己写的备注。两者分区展示，界面只让用户改后者——生成侧永远
   * 碰不到 `note`，否则一次重新生成就把用户敲的字整段盖掉。
   *
   * 渲染回退链：`detail → note → label`。旧图没有 `detail`，因此行为与今天一致。
   */
  detail?: string | null;
  /**
   * 这一支还能不能再展开（契约字段，三态）。
   *
   * **给界面画按钮用的状态，不是给模型看的信号**（模型那一侧的对应物是展开响应里
   * 的布尔 `expandable`，两回事）。判定表在 `backend/app/contracts/mindmap.py`：
   *
   *  - `yes`：还能展开——有子节点但没长完，或者本身就是叶子；
   *  - `no`：已经到底——模型明确说没有更细的内容，或已经到第 8 层；
   *  - `unknown`：不确定——**被额度截断的不算 `no`**，旧图没有这个字段时也是它。
   *
   * 缺省按 `unknown` 处理：旧图从来没被问过这件事，把它当成"还能展开"会让界面
   * 替它许下一个兑现不了的承诺；当成"到头了"则会把唯一能继续的入口灰掉。
   */
  expandable?: NodeExpandable;
  /**
   * 节点级引用，镜像后端 `Node.source_refs`（v1 契约里**已经存在**，不是 v2 预告）。
   *
   * 之前前端缺这个字段，`client.ts::toContractV1()` 只保留 id/label/children，
   * 于是引用在保存时被整批删掉——刷新后永远看不到引用，跟渲染层无关。
   * 字符区间以 `Source.text` 为准，用 Unicode 码点计（不是 UTF-16 下标）。
   */
  source_refs?: SourceRef[];
  /**
   * 契约 v2 的视图字段：折叠状态、样式、手动坐标。
   *
   * 界面都还不渲染，但必须声明成可选字段并原样透传保存——否则用户点一次保存，
   * 后端下发的这些字段就被序列化丢掉了（`note` 在 v1 阶段已经踩过同一个坑）。
   */
  collapsed?: boolean;
  /**
   * 这个节点在图里扮演什么：普通步骤 / 判定 / 起点 / 终点 / 输入输出 / 存储。
   *
   * 与 `MindEdge.kind` 是同一条分工的两半——**边**有语义类型（`flow`/`branch`/`loop`），
   * 节点侧的对偶就是这个字段。流程图里判定节点要画菱形，否则「是/否」两个出口
   * 从哪儿分出来读不出来。
   *
   * 刻意**不复用** `NodeStyle.shape`：那是导入侧的"用户画的那个形状"，逐像素还原
   * 用；`color` 与 `style.color` 的分工已经把这条线划过一次（语义不能塞进样式）。
   * 也刻意**不靠出边数推断**：用户编辑到一半会闪变。
   *
   * 缺省 `null` = 普通步骤，与加这个字段之前的渲染逐像素相同。
   */
  role?: 'step' | 'decision' | 'start' | 'end' | 'data' | 'database' | null;
  style?: NodeStyle | null;
  pos?: NodePosition | null;
  /**
   * 语义色（调色板键）。**可选、缺省 `null`**，所以旧图不需要迁移。
   *
   * 只接受 `NODE_COLORS` 里的键，不是任意 hex：模型自由发挥的 hex 会得到一堆
   * 脏色，而且换主题（暗色/亮色）时无法自动适配。渲染层把它映射到 CSS 变量
   * （`--mind-color-<key>`，见 `styles.css`），色值不在数据里。
   *
   * 与 `style.color`（`#RRGGBB`，透传保存、界面不渲染）**不是同一个东西**，
   * 两者同时存在时以这个为准——语义色是渲染意图，hex 是历史值。
   */
  color?: NodeColor | null;
  /**
   * 图标。目前是 emoji（零依赖、跨平台、`<span>` 直接渲染）。
   *
   * 契约上限 16 码点（组合 emoji 也要放得下）。只有一级分支会被生成侧要求带它：
   * 一棵 80 节点的树如果每个节点都有图标，图标就不再指示任何东西。
   */
  icon?: string | null;
}

/**
 * 节点语义色。与后端 `mindmap.NODE_COLORS` 一一对应。
 *
 * 用联合类型而不是 `string`：拼错一个键在编译期就红，而不是等到画出来是灰的
 * 才有人发现。运行时那份清单在 `mindmap/edits.ts::NODE_COLORS`（与 `LIMITS`
 * 同一个位置、同一条理由：数字与枚举值得两侧共读）。
 */
export type NodeColor = 'blue' | 'green' | 'amber' | 'red' | 'purple' | 'teal' | 'pink' | 'gray';

/**
 * 节点还能不能再展开。与后端 `Node.expandable` 的取值一一对应。
 *
 * 三态而不是布尔：`unknown` 必须与 `no` 分开。被额度截断的节点若记成 `no`，
 * 界面上按钮灰着、说明"已经到底了"——用户被一句假话挡住了唯一能继续的入口。
 */
export type NodeExpandable = 'yes' | 'no' | 'unknown';

/** 节点引用。与 backend/app/contracts/source.py 的 `SourceRef` 对应。 */
export interface SourceRef {
  source_id: string;
  /** 页码，1 起；非分页来源为 null。 */
  page?: number | null;
  /** 字符区间 [start, end)，按码点计；两项必须同时给出。 */
  start?: number | null;
  end?: number | null;
  /** 逐字摘录的原文，≤300 字。 */
  quote?: string | null;
}

// ---- 导图操作契约（与 backend/app/contracts/ops.py 逐字段对应）----
//
// 放在 types.ts 而不是 mindmap/ops.ts：agent 事件流要引用 OpBatch，而
// ops.ts 已经反过来依赖本文件，再让本文件依赖它就成了循环。契约归契约，
// 执行器归执行器。字段一律 snake_case，这些结构会原样跨端传输。

/** 挂一个新节点。`id` 由服务端规范化时分配，模型草案里用 `$tmp1` 这类临时名。 */
export interface AddOp {
  op: 'add';
  id: string;
  parent_id: string;
  index: number;
  label: string;
  source_refs?: SourceRef[];
  /** 生成产出的说明（上限 2000 字）。与 `note` 分开：后者永远是用户写的。 */
  detail?: string | null;
  /** 语义色，只收调色板键（`NodeColor`）；不认识的键在执行器里被拒。 */
  color?: NodeColor | null;
  /** 图标（一个 emoji，≤16 码点）。 */
  icon?: string | null;
}

/**
 * 改节点。
 *
 * `label`、`detail`、`color`、`icon` 与 `source_refs` 缺省（undefined）表示
 * "这一项不动"，与"清空"区分开：`source_refs: []` 是显式清空，`detail: ''` /
 * `color: ''` 是清空该项。这个区别丢不得。
 */
export interface UpdateOp {
  op: 'update';
  id: string;
  changes: {
    label?: string;
    source_refs?: SourceRef[];
    detail?: string | null;
    color?: NodeColor | '';
    icon?: string | null;
  };
}

/** 删节点及整棵子树。根节点不可删。 */
export interface RemoveOp {
  op: 'remove';
  id: string;
}

/** 改变拓扑：把节点连同子树挂到新父节点下。唯一可能造环的 op。 */
export interface MoveOp {
  op: 'move';
  id: string;
  new_parent_id: string;
  index: number;
}

/**
 * 连一条附加边（`MindMapContent.edges`）。
 *
 * `id` 是**边 id**，不是节点 id：它只要求在边集合里唯一。服务端在规范化阶段
 * 分配（`e_` 前缀），模型草案里用 `$tmp1` 这类临时名。
 *
 * `source` / `target` 必须是已有节点，且**允许成环**（A→B 之后 B→A 就是回退线）。
 */
export interface AddEdgeOp {
  op: 'add_edge';
  id: string;
  source: string;
  target: string;
  /** 边上的文字（≤40 字）。`null` 表示没有文字。 */
  label?: string | null;
  kind?: EdgeKind | null;
}

/** 删一条附加边。不动节点：删线之后树一个字节都不变。 */
export interface RemoveEdgeOp {
  op: 'remove_edge';
  id: string;
}

export type MindMapOp = AddOp | UpdateOp | RemoveOp | MoveOp | AddEdgeOp | RemoveEdgeOp;

/** 整批原子生效。 */
export interface OpBatch {
  batch_id: string;
  base_revision: number;
  ops: MindMapOp[];
}

export interface MindMapContent {
  title: string;
  root: MindNode;
  /** 整图来源摘要；旧导图可以省略，不代表节点级引用。 */
  source_usage?: SourceUsage[];
  /** 契约 v2：内容格式版本。旧记录没有该字段，按 v2 默认值理解。 */
  schema_version?: 2;
  /**
   * 契约 v2：树的排布方式。界面目前只认 `tree` 与 `vertical`；
   * `logic` / `timeline` / `fishbone` / `manual` 原样透传保存，不渲染。
   */
  layout?: LayoutKind;
  /** 契约 v2：主题。界面只读写 `direction`，其余字段原样透传。 */
  theme?: MindMapTheme;
  /** 契约 v2：节点之间的非层级关系。界面不渲染，原样透传保存。 */
  relations?: MindRelation[];
  /**
   * 跨父子节点的**附加边**：流程图里的条件跳转、汇合、回退。
   *
   * 树仍然是骨架（布局、展开、限额全部由它决定），这个字段只补上树表达不了的
   * 那一类关系——**主干顺序 = 父链**，**条件分支 = 子节点**，**汇合与回退 = 边**。
   *
   * 三个必须记住的性质：
   *
   *  - **不参与布局**。节点坐标仍由 `layoutTidy(root)` 算，边只画连线；否则每加
   *    一条线整个画布都会重排，用户刚记住的位置就没了。
   *  - **允许环**（A→B→A 就是回退线），也**允许跨子树**（那正是汇合）。
   *  - **可选、缺省空**：旧图没有这个字段时按空数组处理，全部节点与行为不变。
   */
  edges?: MindEdge[];
  /**
   * 这张图**是什么图型**（镜像后端 `MindMapContent.diagram_type`）。
   *
   * 与 `layout` 不是一回事：`layout` 描述排布，图型描述"这张图按什么读"——流程图的
   * 条件分支与回退只对 `flowchart` 有意义，鱼骨图的一级分支固定是人/机/料/法/环/测。
   *
   * **可选、缺省按 `tree` 处理**：旧图（与后端加这个字段之前生成的所有记录）没有
   * 这个键，而它们全部由思维导图那条提示词生成。所以判断时要写 `?? 'tree'`，别写
   * `=== 'tree'`——后者会把旧图判成"未知图型"。
   *
   * 唯一用法是「附加关系」区块的可见性（`App.vue`）：那个区块只对流程图有意义，
   * 而在此之前代码里没有任何判断依据，只能一直摆着。
   */
  diagram_type?: DiagramType;
  /**
   * 一级分支**这一支讲什么**：`{一级分支节点 id: 8–12 字概括}`。
   *
   * 唯一读者是一级分支节点上那枚概括角标（`canvas/MindNode.vue`，
   * `data.branchDigest`）。数字（这一支还有几个节点没画）是渲染层自己数的，
   * 这里只装"要调模型才说得出来"的那一半。
   *
   * 三个必读性质：
   *
   *  - **可选、缺省空**：旧图与开关关着时生成的图都没有这个键（后端那条字段
   *    带 `exclude_if`，空表根本不序列化）。读到 `undefined` 就是"这一支没有概括"，
   *    角标退回只显示数字——与加这个字段之前的行为一致。
   *  - **可能装着一个已经不存在的 id**：契约层不校验键（理由见后端
   *    `MindMapContent.branch_digests`）。所以取值必须容忍 `undefined`，不能
   *    用 `!` 断言，也不能拿它的键去遍历树。
   *  - **不是标题**：它与 `label` 说的是同一件事的另一种说法，两个都画在同一屏上
   *    时，"看哪个"是一件要用判据决定的事（见 `docs/VIEW-COLLAPSE.md` §七）。
   */
  branch_digests?: Record<string, string>;
}

// ---- 另外两种图型的顶层结构（阶段 2 起后端真的会产出它们）----
//
// 三种内容的判别**靠形状，不靠 `diagram_type`**（镜像后端 `DiagramContent` 的注释）：
// 树必有 `root`、鱼骨必有 `bones` + `problem`、流程必有 `stages`，三者天然互斥；
// 而 `diagram_type` 在库里有一批"标着 fishbone、形状仍是树"的历史记录，拿它分派会
// 让那些图走进不存在的分支。判别与分派都在 `mindmap/shape.ts` 里，别处不要自己写。

/**
 * 鱼骨图上的一根骨：一个类别 + 一棵子树。与后端 `FishboneBone` 逐字段对应。
 *
 * `root.children` 就是"这根骨下挂的原因"——套一层 `causes: list[Node]` 只会让
 * "这条原因下面还能不能再展开"换个地方回答。
 */
export interface FishboneBone {
  /** 主骨上那一行类别（人 / 机 / 料 / 法 / 环 / 测）。 */
  category: string;
  /** 骨自己的子树根。**原因挂在它的 `children` 上**，不挂在别处。 */
  root: MindNode;
  /** 骨的配色，独立于第一个原因（那是图的骨架，不是内容节点）。 */
  color?: NodeColor | null;
  icon?: string | null;
}

/**
 * 鱼骨图。与后端 `FishboneContent` 逐字段对应。
 *
 * **没有 `layout`**：鱼的排布是几何定死的（主骨横贯、骨按类别固定上下交替），
 * 让用户选"横向还是纵向"只会得到一张画歪的鱼。所以这个类型上也不能有 `layout`
 * ——`extra="forbid"` 会让多出来的那个键变成保存时的 422。
 */
export interface FishboneContent {
  schema_version?: 2;
  title: string;
  diagram_type: 'fishbone';
  /** 主骨上那一句（"为什么报销要 11 天"）。与 `title`（文件名）是两件事。 */
  problem: string;
  bones: FishboneBone[];
  theme?: MindMapTheme;
  relations?: MindRelation[];
  source_usage?: SourceUsage[];
}

/**
 * 流程里的一个阶段：一个 id + 一棵步骤树 + **阶段内部**的附加边。
 * 与后端 `FlowStage` 逐字段对应。
 *
 * `id` 与节点 id 是**两个命名空间**：阶段 id 只被 `FlowContent.edges` 引用，节点 id
 * 只被 `relations` 与阶段内部的树引用。合并之后"这条边连的是阶段还是步骤"只能靠
 * 形状猜，而两者在画布上画出来的东西完全不同。
 *
 * ## 两处 `edges`、两个命名空间（这个结构最容易串的地方）
 *
 *  - `FlowContent.edges` —— **阶段之间**的跳转，端点是**阶段 id**（`s_…`）。画出来
 *    是画布上的**主轴**，也参与 dagre 分层。
 *  - `FlowStage.edges`（下面这个字段）—— **这个阶段内部**的边：判定节点的「是 / 否」
 *    两条出边、以及步骤之间的顺序跳转。端点是**该阶段自己的节点 id**。画出来是
 *    阶段内部的一条**细线**，**不参与布局**。
 *
 * 合成一个数组就得靠前缀区分命名空间，而节点 id 允许任意字符——`node:x` 这种
 * 节点 id 与"前缀表示法"根本分不开。靠位置隔离则只要"每条边的两个端点都在这个
 * 集合里"，一次查表，还天然排除了"阶段内的边连到另一个阶段"这种错。
 */
export interface FlowStage {
  /** 阶段 id（后端规范化成 `s_` 前缀）。 */
  id: string;
  /** 阶段内部的步骤树根。`root.children` 就是这个阶段的第一步、第二步…… */
  root: MindNode;
  /**
   * 阶段内部的边（端点必须是**这个阶段自己的**节点 id）。可选、缺省空：
   * 旧记录没有这个键，按空数组处理——它们在加这个字段之前画出来正好也是那样。
   */
  edges?: MindEdge[];
}

/** 流程图。与后端 `FlowContent` 逐字段对应。 */
export interface FlowContent {
  schema_version?: 2;
  title: string;
  diagram_type: 'flowchart';
  /** 顺序就是执行先后：流程图的"先后"是数组下标，不需要另一个 `order` 字段。 */
  stages: FlowStage[];
  /** **端点是阶段 id**，不是节点 id（与 `MindMapContent.edges` 刚好相反）。 */
  edges?: MindEdge[];
  theme?: MindMapTheme;
  relations?: MindRelation[];
  source_usage?: SourceUsage[];
}

/**
 * 一份内容可能是三种形状之一。保存 / 读取 / 生成产物共用它。
 *
 * 写成联合而不是"一个宽松接口 + 一堆可选字段"：可选字段的版本里，`bones` 与
 * `stages` 同时存在是**合法**的，而那样的图在契约层是 422——类型层面就不该允许
 * 造出来。
 */
export type DiagramContent = MindMapContent | FishboneContent | FlowContent;

/**
 * 边的语义类型。与后端 `mindmap.EDGE_KINDS` 一一对应。
 *
 * 三个值各对应一种画法（`canvas/MindEdge.vue`）：`flow` 实线箭头、`branch` 换色、
 * `loop` 虚线。所以它是联合类型而不是 `string`——拼错一个值在编译期就红，而不是
 * 等到画出来是一条"看起来像普通箭头"的线才有人发现。
 */
export type EdgeKind = 'flow' | 'branch' | 'loop';

/** 与后端 `app/contracts/mindmap.py::Edge` 一致的附加边。 */
export interface MindEdge {
  id: string;
  /** 起点节点 id（必须在树里）。 */
  source: string;
  /** 终点节点 id（必须在树里）。 */
  target: string;
  /** 边上的文字（≤40 字）："是" / "否" / "通过" / "驳回"。 */
  label?: string | null;
  /** 语义类型；`undefined` / `null` 按 `flow` 画。 */
  kind?: EdgeKind | null;
}

/** 与 `backend/app/contracts/mindmap.py::LayoutKind` 一致。 */
export type LayoutKind = 'tree' | 'vertical' | 'logic' | 'timeline' | 'fishbone' | 'manual';

/** 与后端 `Theme` 一致。`direction` 只描述横向布局朝哪边。 */
export interface MindMapTheme {
  preset?: 'default' | 'slate' | 'forest' | 'sunset' | 'mono';
  accent?: string | null;
  font?: 'system' | 'serif' | 'mono';
  direction?: 'right' | 'left' | 'both';
}

/** 与后端 `NodeStyle` 一致。界面不渲染，原样透传。 */
export interface NodeStyle {
  color?: string | null;
  icon?: string | null;
  bold?: boolean;
  italic?: boolean;
  shape?: 'rounded' | 'rect' | 'pill';
}

/** 与后端 `Position` 一致。只在 `layout === 'manual'` 时有效。 */
export interface NodePosition {
  x: number;
  y: number;
}

/**
 * 跨节点关系的语义种类。与后端 `RelationKind` 一致（六个值）。
 *
 * 为什么单独起一套而不复用 `RelationType` 的四个旧值：`part_of` / `refines` 在旧集合
 * 里根本无处安放，而混成一个联合类型会让"这个值是给老数据用的、那个是新生成用的"
 * 永远分不清。渲染一律读 `kind`。
 */
export type RelationKind =
  | 'depends_on'
  | 'causes'
  | 'conflicts'
  | 'part_of'
  | 'refines'
  | 'references';

/**
 * 与后端 `Relation` 一致。**画布上画成虚线**（`canvas/MindRelation.vue`）。
 *
 * 两个语义字段并存，分工是死的：
 *  - `kind` 是规范名，六个值，后端 `_sync_kind` 保证它一定有值；
 *  - `type` 是旧名，四个值，只为读回存量数据存在——**不要**用它做判断。
 *
 * `why` 是这条边成立的理由（一句话）。生成侧把没有 `why` 的边直接丢掉，所以画布上
 * 每一根虚线都答得上来"为什么这两个节点有关"。
 */
export interface MindRelation {
  id: string;
  source: string;
  target: string;
  kind?: RelationKind | null;
  type?: 'cause' | 'ref' | 'conflict' | 'depends' | null;
  label?: string | null;
  why?: string | null;
  evidence?: string | null;
  provenance?: 'user' | 'ai' | 'import';
}

/** 已保存的导图记录。`version` 是乐观锁的版本号，保存时要原样带回去。 */
export interface MapRecord {
  id: string;
  version: number;
  /**
   * 三种形状之一（树 / 鱼骨 / 流程）。
   *
   * 此前这里是 `MindMapContent`：阶段 2 之后库里真的会有另外两种，读回来直接
   * `content.root` 会让"打开一张鱼骨图"炸在渲染层。判别与分派统一走
   * `mindmap/shape.ts`，不要在这里自己判。
   */
  content: DiagramContent;
  updated_at: string;
}

// ---- 导入（`POST /api/mindmaps/import`）----

/**
 * 一条导入告警。**只有 code 与 message**（后端 `ImportWarningPayload`）。
 *
 * 定位信息（第几行、哪个节点）写在 message 正文里，不占单独字段：多一个字段就
 * 多一次两端契约对齐，而"第 12 行"读在句子里并不更差。
 *
 * `code` 是稳定的（`truncated_nodes` / `source_refs_cleared` / `edges_dropped`…），
 * 界面按它分组或加图标；`message` 是给人看的中文。**不要按 message 做判断**——
 * 文案会改，码不会。
 */
export interface ImportWarning {
  code: string;
  message: string;
}

/**
 * 导入的响应体。`source_format` 是识别出来的格式（`json` / `markdown` / `opml` /
 * `xmind` / `freemind` / `puml`），界面用它说"按 XMind 读进来了"。
 *
 * **只解析不落库**：`content` 装进画布后是"有未保存修改"的状态，落库仍走保存。
 */
export interface ImportResult {
  content: DiagramContent;
  warnings: ImportWarning[];
  source_format: string;
}

// ---- 来源（文档解析）----

/**
 * 来源模态，镜像后端 SourceKind。
 *
 * `web` / `paper` 是**外部检索**抓回来的（维基百科条目、arXiv 论文），不是用户
 * 上传的材料。分开不是为了好看：两者的可信度不同，界面上要能区分"我的材料"与
 * "外面抓的资料"（引用只证明这句话出自那个来源，不证明来源本身权威）。
 */
export type SourceKind = 'text' | 'markdown' | 'document' | 'image' | 'audio' | 'web' | 'paper';

export interface SourceUsage {
  source_id: string;
  kind: SourceKind;
  filename: string | null;
  char_count: number;
  /**
   * 原文地址（外部检索的来源才有）。
   *
   * 放在摘要里而不是只放在 `Source.metadata`：保存的导图**不带** `Source`，
   * 重新打开时手里只有这份摘要——只把它留在 `Source` 上，等于"这一轮能看到链接，
   * 下次打开就没了"。
   */
  url?: string | null;
}

/** 来源定位，供节点溯源。 */
export interface SourceSpan {
  /** 整份文档共几页（PDF 才有），不是"解析了几页"。 */
  page: number | null;
  start: number | null;
  end: number | null;
}

/**
 * 解析后的统一来源。字段与 backend/app/contracts/source.py 的 `Source` 一一对应：
 * 后端是 `extra="forbid"`，多一个字段就 422，所以这里宁可保守也不要自作主张加字段。
 */
export interface Source {
  id: string;
  kind: SourceKind;
  filename: string | null;
  /** 抽取出的正文；解析失败时为 null。 */
  text: string | null;
  span: SourceSpan | null;
  status: 'pending' | 'parsed' | 'failed';
  error: string | null;
  /** 实际得到内容的页数：51 页的 PDF 只处理前 50 页时这里是 50，span.page 才是 51。 */
  parsed_pages: number | null;
  /** 是否因后端页数上限被截断（后半截根本没处理）。 */
  truncated: boolean;
  /**
   * 处理过但彻底没拿到内容的页码（1 起）。
   *
   * 与 truncated 是两回事：那个是"没处理到"，这个是"处理了但中间有洞"。
   * 合并成一个布尔值会给出"共 3 页，仅处理前 3 页"这种自相矛盾的提示。
   */
  failed_pages: number[];
  /** 音频时长（秒）；兼容未返回此字段的旧响应。 */
  duration_seconds?: number | null;
  /**
   * 音频的句级时间戳（音频来源才有）。
   *
   * 加这个字段是因为 `backend/app/contracts/source.py::Source` 已经下发它，而镜像里
   * 缺字段时，「把来源原样送回后端」会把时间戳整段丢掉——需要按句回指原文的功能
   * 就再也用不上它。时间是从**原始录音**开头算的毫秒偏移，不是相对切片。
   */
  sentences?: AudioSentence[];
  /**
   * 正文指纹（去重与"同一份材料是否已入库"的依据）。
   *
   * 与下面两个字段同一条教训：后端的 `Source` 已经下发它们，镜像缺字段时
   * `contract-check.ts` 的 `SameKeys` 会红（键集合必须一致），而且「把来源原样
   * 送回后端」会把这些值丢掉。基线里这三个字段就是漏的，`npm run type-check`
   * 因此长期失败——一个常年红的检查等于没有检查，所以顺手补齐。
   */
  content_hash?: string | null;
  /** 描述性元数据：检索前过滤用（作者、时间、分类、标签）。 */
  metadata?: SourceMetadata;
}

/**
 * 来源的描述性元数据，镜像后端 `SourceMetadata`。
 *
 * 两类来源不同：`author` / `created_at` / `modified_at` 由 ingestion 层从文件
 * 自带的元数据里读（确定性、零成本）；`category` / `tags` 只由调用方给。
 * 时间一律是 ISO8601 UTC 秒级字符串，与 `MapRecord.updated_at` 同口径。
 */
export interface SourceMetadata {
  author?: string | null;
  created_at?: string | null;
  modified_at?: string | null;
  category?: string | null;
  tags?: string[];
  /**
   * 原文地址（外部检索专用）。后端只放行 http/https——`javascript:` 之类的 scheme
   * 在这里是可执行的，所以校验在契约侧就做了，前端可以放心塞进 `<a href>`。
   */
  url?: string | null;
  /** 抓取时的原始字符数（外部检索专用）：正文按 8000 字入库截断，这个数是截断前的。 */
  raw_chars?: number | null;
}

/** 音频里的一句话及其在原始录音中的位置。 */
export interface AudioSentence {
  text: string;
  begin_ms: number;
  end_ms: number;
}

export interface CreateTextSourceResponse {
  source: Source;
}

// ---- 知识库 ----

/**
 * 知识库里的一份材料。字段与 `backend/app/knowledge/shelf.py` 的 `LibraryEntry` 对应。
 *
 * `cited_by` 是"被几张**已保存导图**引用"：一张图的 `source_usage` 与它节点的
 * `source_refs` 都算，同图内去重。它回答的是"这份材料我到底用没用过"。
 */
export interface LibraryEntry {
  source_id: string;
  filename?: string | null;
  kind: SourceKind;
  chars: number;
  chunks: number;
  created_at: string;
  last_used_at: string;
  cited_by: number;
}

/** 列表页：`total` 是库里的总数，不是 `items.length`。 */
export interface LibraryList {
  items: LibraryEntry[];
  total: number;
}

/** 单份材料的正文预览；`truncated` 为真时界面必须说明"只是前 4000 字"。 */
export interface LibrarySource {
  source_id: string;
  filename?: string | null;
  kind: SourceKind;
  text: string;
  truncated: boolean;
  total_chars: number;
}

// ---- agent 运行 ----

/**
 * 图型。**值域不在这里定义**——它从生成物 `schema.d.ts` 推导。
 *
 * `tree` 思维导图 / `flowchart` 流程图 / `fishbone` 鱼骨图。三者共用同一条生成
 * 流水线，差别只在骨架那一次要哪份提示词、要不要解析附加边。
 *
 * 此前这里是手写的 `'tree' | 'flowchart' | 'fishbone'`，与 `schema.d.ts` 里那条
 * union 之间**没有任何东西连着**：后端往 `contracts/mindmap.py::DiagramKind` 加一个
 * 图型，这条手写 union 不会变、前端不报错、用户永远选不到——正是
 * `docs/WIRING-GAPS.md` 记的那条残留。现在值域的唯一真源是
 * `backend/openapi.json`（`DiagramKind` 的 Literal），链子是：
 *
 *     app/contracts/mindmap.py::DiagramKind
 *         ↓ scripts/export_openapi.py（提交的快照，test_openapi_snapshot.py 守卫）
 *     frontend/src/api/schema.d.ts     ← npm run gen:api 生成
 *         ↓ NonNullable<…>
 *     api/types.ts::DiagramType
 *         ↓ Record<DiagramType, …>
 *     stores/options.ts::DIAGRAM_TYPE_COPY
 *
 * 后端加一个图型时，`schema.d.ts` 的 union 变宽 → 这里的类型跟着变 →
 * `stores/options.ts` 的文案表少一条 → **`npm run type-check` 直接红**。红的时机
 * 比用例早、也比用例硬：不是"某个断言没通过"，而是"这个类型没有那一项"。
 *
 * `NonNullable` 同时去掉 `| null`（后端 `DiagramKind | None`，None = 让模型判断）
 * 与字段可选带来的 `| undefined`。两者都不是**图型**，不该进这个类型。
 */
export type DiagramType = NonNullable<
  components['schemas']['AgentRunRequest']['diagram_type']
>;

/**
 * 界面上下拉里可能出现的那四个值。
 *
 * `auto` **不是**发给后端的值：它表示"我不指手画脚"，请求体里干脆不带这个字段
 * （后端 `None` = 让模型自己判断）。省略字段与老前端逐字节相同，而发一个假值
 * 会让后端多一种要理解的状态。
 */
export type DiagramTypeChoice = 'auto' | DiagramType;

/**
 * 一次 agent 运行。字段与 backend/app/agent/contracts.py 的 `AgentRunRequest` 对应。
 *
 * `create` 不能带 snapshot、`expand`/`edit` 必须带——那条约束在后端开流前校验，
 * 这里不重复，因为报错文案由后端统一给。
 */
export interface AgentRunRequest {
  goal: string;
  intent: 'create' | 'expand' | 'edit';
  /**
   * 这次运行的**身份**（镜像后端 `AgentRunRequest.run_id`）。
   *
   * 由前端自己生成、每次点击生成一个新的：服务端发的话，客户端要先请求一次才拿得到
   * id，而那一次请求本身就成了"第一次运行"——刷新重发时照样没有 id 可用。
   *
   * 幂等只在"带同一个 run_id"时生效。所以它的价值不是防"重复点击"（那是按钮禁用
   * 与 `busy` 闸门的事），而是让服务端能认出"这是同一次运行又投递了一遍"：
   * 正在跑 → 409 而不是再花一份钱；已经跑完 → 直接把上次产出的那棵树重放回来。
   */
  run_id?: string | null;
  /**
   * 拆解方式：同一句主题换一套步骤，长出来的树结构就不同。
   *
   * 取值与后端 `generation.service.THINKING_MODELS` 的键一一对应；后端是
   * Literal，拼错会被 422 拒（不是静默退化成 default），所以这里保持宽松的
   * string，由下拉框保证只在五个值里出现。缺省即 `default`。
   */
  thinking_model?: string;
  /**
   * 图型：用户在图型下拉里选的那一个。**省略 = 让模型自己判断**。
   *
   * 这个字段是"说了流程图就真的出流程图"的唯一保证：模型看不见界面，而工具参数
   * `diagram_type` 它有默认值可走——用户说"画个流程图"，它把这句话当主题消化掉，
   * 然后照默认的 `tree` 生成。提到请求层之后，图型不再依赖模型的记性。
   *
   * 只有 `create` 会用它：`expand` / `edit` 不生成骨架，图型对它们没有意义。
   */
  diagram_type?: DiagramType | null;
  /**
   * 这一次运行**实际带着哪些偏好**，形如 `["diagram_type.default=fishbone"]`。
   *
   * 由前端标（镜像后端 `AgentRunRequest.prefs_applied`），因为只有这里知道
   * "下拉里这个值是偏好预填的、用户没动过"。后端**不会**把这个值塞进
   * `diagram_type`——那会让"用户没说"与"用户选了"再也分不开。
   *
   * 它落进 `runs.request.prefs_applied`，是偏好面板上"上次生效"的唯一数据源。
   * 没有它，一条偏好写进去之后没有任何办法证明它影响过任何一次生成。
   */
  prefs_applied?: string[];
  snapshot?: MindMapContent | null;
  selected_ids?: string[];
  /**
   * 本次请求附带的来源，镜像后端 `AgentRunRequest.sources`（max_length=50）。
   *
   * 材料正文走这里，不拼进 goal：拼进去会被当成"用户的主题"，
   * 而模型需要的是带 source_id 的结构化数据——否则它写不出合法的引用。
   */
  sources?: Source[];
  /**
   * 「这一次」走不走推理模型（镜像后端 `AgentRunRequest.deep_thinking`）。
   *
   * 它是这**一次**的开关，不是设置：界面上那枚「深度思考」按钮点一下就变一次，
   * 不写偏好、不进 `prefs_applied`。缺省不发这个字段，与加它之前逐字节相同。
   */
  deep_thinking?: boolean;
}

/**
 * 运行台账里的一行（镜像后端 `agent.router.RunSummary`）。
 *
 * **不带结果正文**：那是一整棵树，20 条一起下发只为显示一句"上次生成还没保存"。
 * 真要那份内容时点「查看」，走 `GET /api/agent/runs/{run_id}`。
 *
 * 这一行是"运行身份"落到界面上的唯一痕迹：此前 run 只活在一个 HTTP 连接的
 * 寿命里，页面一关，已经跑出来的结果就跟着连接一起没了。
 */
export interface RunSummary {
  run_id: string;
  status: 'running' | 'done' | 'cancelled' | 'failed';
  /** 原样回填：`create` 的结果落地时不能继承当前图的身份，`edit`/`expand` 要。 */
  intent: 'create' | 'expand' | 'edit';
  goal: string;
  created_at: string;
  updated_at: string;
  /** 这份结果后来被保存了。空值 = 生成过、还没存下来，下次进页面会提醒一次。 */
  applied_at?: string | null;
  /**
   * 这份结果是"跑到一半停下"交出来的半棵树。
   *
   * 判断一律写 `partial === true`：字段缺省 / `null` 表示**这条记录没记过**（老库、
   * 或那一轮根本没走交付标记），不是"确定完整"。列表也带它，否则点「载入」时
   * 半棵树与完整生成长得一模一样。
   */
  partial?: boolean | null;
  /**
   * 这份结果不是收尾交的，而是跑到一半截下来的检查点。
   *
   * 有值 = **结构长全了、说明还没写**（`detail` 是空的）。它与 `partial` 是两件事：
   * `partial` 说"树没长完"，这一条说"树长全了、描述没来得及写"。下一步也不同——
   * 前者要补内容，后者只是缺描述，直接载入接着改就行。
   */
  checkpointed_at?: string | null;
  /** 检查点那份里有多少个节点。只为一句"已保存 N 个节点"，不值得再拉整棵树。 */
  node_count?: number | null;
}

/** 单条运行的详情（镜像后端 `RunDetail`）：多出结果正文、失败原因与来历。 */
export interface RunDetail extends RunSummary {
  candidate?: DiagramContent | null;
  failure?: { code?: string; message?: string } | null;
  /** `demo` 是占位树、`model` 是模型产出。重放时按它标来源，不能一律写成模型。 */
  mode?: 'demo' | 'model' | null;
  /** 半棵树的来历：哪条闸把这一轮停下的那一句解释，与实时流上的 `partial_reason` 同源。 */
  partial_reason?: string | null;
}

/**
 * agent 事件流。`seq` 从 1 连续递增，前端靠它判断丢帧与乱序。
 *
 * 生成只剩这一条通道，所以这就是唯一的帧形状；状态机完整地待在 store 的
 * `runGeneration` 里，一个 switch 能从头读到尾。
 */
export type AgentEvent =
  /**
   * 一句"现在在干什么"。
   *
   * `stage` 是**稳定标识**（`external_search`），不是文案——界面只显示 `detail`，
   * `stage` 留给"按阶段换样式/抑制"与用例断言，所以它可以随时被后端加值，但已有
   * 的值不会改。
   *
   * `count` 只在"这件事有数量结果"的帧上出现（外部检索的结束帧带命中条数）。
   * 缺席＝还没结束，`0` ＝结束了但一条没找到，两者必须分得开。
   */
  | { type: 'stage'; seq: number; stage: string; detail?: string; count?: number }
  /**
   * 材料入库后的 id 对照表：旧 id → 库里的持久 id。
   *
   * 解析接口给的是本次请求内有效的 uuid，而知识库认自己的持久 id；导图里记的
   * `source_refs.source_id` 必须用后者，否则重新打开后引用指不到原文。收到这一帧
   * 就把手上的来源条目就地换掉（`item.source.id` 与 `item.id` 一起换，后者是
   * 会话内反向索引的键）。没有 id 变化时这一帧不出现。
   */
  | { type: 'sources'; seq: number; mapping: Record<string, string> }
  | { type: 'tool_start'; seq: number; call_id: string; name: string; args_summary?: Record<string, unknown> }
  | {
      type: 'tool_end';
      seq: number;
      call_id: string;
      name: string;
      ok: boolean;
      result_summary?: Record<string, unknown>;
      latency_ms?: number;
    }
  | { type: 'ops'; seq: number; call_id: string; batch: OpBatch }
  /**
   * 节点级预览帧：深层生成的某一次展开调用产出了一个子节点。
   *
   * 与普通生成的 `node` 事件不是一回事：那条帧自带节点 id，父子关系在 JSON 里；
   * 这里的展开调用响应里**根本没有 id**，父节点是谁只有工具层知道，所以位置靠
   * `path` 描述。它也只是预览，可采纳的内容仍以 snapshot / done 的 `content` 为准。
   */
  | {
      type: 'node_grow';
      seq: number;
      /** 从根出发的子节点索引链，指向**父节点**；`[]` 表示挂在根上，`null` 表示这一帧就是根。 */
      path: number[] | null;
      label: string;
      /** 模型写出来的可展开性；`null` 表示还没写到，与 `false` 不是一回事。 */
      expandable: boolean | null;
    }
  | { type: 'snapshot'; seq: number; content: DiagramContent }
  /**
   * 长静默期间的保活帧。
   *
   * 线上实测单轮静默到过 25 秒，那段时间事件流里一个字节都没有，中间链路的空闲
   * 超时会掐断连接。它不带业务进度：真实进度看 `stage` / `tool_start` / `node_grow`。
   */
  | { type: 'heartbeat'; seq: number; waited_ms: number }
  | {
      type: 'done';
      seq: number;
      content?: DiagramContent | null;
      usage?: Record<string, unknown>;
      /**
       * 这棵树是演示规则拼出来的还是模型产出的。
       *
       * 后端按 `exclude_none=True` 编码，正常帧一定带值；字段缺失只说明对方
       * 是没填 mode 的旧版本——那是"没说"，不是"模型"。
       */
      mode?: 'demo' | 'model' | null;
      /**
       * 收尾这一轮模型说的话，**只在没产出导图时**由后端填（已截断）。
       *
       * 线上出现过"模型只写字、不调工具"，那时只有这一帧能说明原因；页面上
       * 直接显示，排障不用登服务器翻日志。
       */
      text?: string;
      /**
       * 模型这一轮的思考过程（`reasoning_content`）。
       *
       * 与 `text` 不同，**成功时也填**：走推理模型时它正是用户勾「深度思考」要看
       * 的东西，界面用 `<details>` 折叠展示全文。上限见后端 `REASONING_BYTE_LIMIT`
       * （编码后的 64 KiB，汉字约 21800 字；实测的 3000–8000 字远够，等于不截）。
       */
      reasoning?: string;
      /**
       * 这一轮真正发出去的模型名（`deepseek-chat` / `deepseek-reasoner` / 配置值）。
       *
       * 缺字段＝没说（演示模式、按脚本回放的假 provider），不是"用了 chat"。
       * 界面据此在消息流里写清"这次走了哪个模型"——勾了「深度思考」而配置没给对
       * 名字时，这是唯一看得见的地方。
       */
      model?: string | null;
      /**
       * 服务端在响应体 `model` 里回报的名字——**它实际跑的是哪个**。
       *
       * 与上面那个 `model` 是两件事：那是我们发出去的意图，这是对面的回答。
       * 实测请求写 `deepseek-chat`、响应回 `deepseek-flash`（同一模型的两个别名，
       * 区别只在思不思考）。缺字段＝服务端没说，界面写"未知"。
       */
      served_model?: string | null;
      /**
       * 交出来的这棵树**没长完**：预算耗尽、工具超时、连续失败、空转收尾。
       *
       * 从前这些收尾一律发 `error`，前端清掉预览、画布也不动，用户跑了三分钟什么
       * 都没拿到。现在改成"有树就交树"，但必须让用户知道它不是一次完整生成——
       * 否则半棵树会被当成模型认为的完整答案。
       *
       * 缺字段＝没说（正常收尾、以及加这个字段之前的老帧），与 `false`（明确完整）
       * 不是一回事，所以判的时候用 `=== true`。
       */
      partial?: boolean | null;
      /**
       * 为什么没长完，一句人话（与 `error.message` 同源，可直接显示）。
       *
       * 与 `partial` 成对出现：只给布尔值的话，界面只能说"不完整"却说不出原因，
       * 而"额度用完"与"工具卡死"对用户意味着完全不同的下一步。
       */
      partial_reason?: string | null;
    }
  | { type: 'error'; seq: number; code: string; message: string; limit?: string };

// ---- 用户偏好 ----

/**
 * 一条偏好（镜像后端 `prefs.service.Pref`）。
 *
 * 三层记忆里唯一缺的那一层——**跨请求、关于这个人的稳定事实**。会话那层是
 * `runs` 里存的那棵树，工作区那层是 `MindMapContent` 本身。
 */
export interface Pref {
  /** 键即主键：同一件事只能有一条。`diagram_type.default` / `note.1` …… */
  key: string;
  value: string;
  scope: string;
  /** `user_explicit` 是用户自己设的，`suggestion_accepted` 是点了建议才写的。 */
  source: 'user_explicit' | 'suggestion_accepted';
  source_run_id?: string | null;
  created_at: string;
  updated_at: string;
  /**
   * 上一次**真的生效**是什么时候（来自 `runs.request.prefs_applied`）。
   *
   * 这一位是这张表的死表探测器：`null` 表示"这条偏好从没影响过任何一次生成"。
   * 一条全都为 `null` 的面板，就是"写进去但没人读"的样子——不用翻日志。
   */
  last_applied_at?: string | null;
}

/** `PUT /prefs/{key}` 的响应：写入后的行，以及这次有没有真的改变。 */
export interface SavedPref {
  pref: Pref;
  /** 同值重复提交是 `false`，且 `updated_at` 不刷新——不制造"我刚设过"的假象。 */
  changed: boolean;
}

/**
 * 一条**候选**建议（镜像后端 `prefs.service.Suggestion`）。
 *
 * 它不是偏好，只是一个判断："最近 3 次都是同一个值"。落库必须经过用户点一次，
 * 因为实测同一主题两分钟内有过两次相反的图型要求——自动总结会在那里写出两条
 * 互相矛盾的事实。
 */
export interface PrefSuggestion {
  key: string;
  value: string;
  /** 值的中文名（"流程图"），供界面直接显示。 */
  label: string;
  count: number;
  message: string;
}

// ---- 模型设置（`/api/settings/model`）----
//
// 镜像 `backend/app/settings/router.py`。这一组类型里没有**一个**明文字段——
// 密钥只有 `api_key_masked`（`…abcd`）。这不是风格问题：`GET` 的响应会出现在
// devtools 的 Network 面板里，也会被用户截图发到群里。

/**
 * 当前生效的模型配置（镜像后端 `ModelSettings`）。
 *
 * `*_source` 是 `saved` / `env` / `unset` 三个值之一，界面据此标注每一项
 * 「面板里设的」「来自环境变量」。**不返回明文密钥**是这张契约的硬约束。
 */
export interface ModelSettings {
  /** OpenAI 兼容端点，已去掉末尾斜杠（否则拼出来是 `//chat/completions`）。 */
  base_url: string;
  base_url_source: string;
  /** 打码后的密钥：`…abcd`（只露末 4 位）；没配时是空串。**永远不是明文**。 */
  api_key_masked: string;
  api_key_source: string;
  /** 有没有密钥。界面用它决定"清除密钥"按钮可不可点。 */
  api_key_set: boolean;
  model: string;
  model_source: string;
  /** 显式配的「深度思考」模型名；空串 = 没配，走 `_model_for` 的启发式。 */
  deep_model: string;
  deep_model_source: string;
  /** 勾了深度思考时**实际会发出去**的名字（可能来自启发式，如 `deepseek-reasoner`）。 */
  deep_model_effective: string;
  /** `MINDMAP_LLM_MODE` 的原值。`demo` 表示"配好了也不会调模型"，**只读**。 */
  llm_mode: string;
  /** 地址 / 密钥 / 模型名三项齐了没有。判据与后端 `_config()` 一致。 */
  configured: boolean;
  /**
   * 当前这个人能不能改**全站共享**的那份配置（`scope: "instance"`）。
   *
   * 真值只属于部署方——这台机器上第一个注册的账号。界面上"本站共享配置"那一块
   * 据此决定显不显示：藏起来不是为了保密，是因为给所有人显示一个点了就 403 的
   * 表单项，比看不见更糟。
   */
  can_edit_instance: boolean;
  /**
   * 全站共享那一行（`owner = ''`）的**原始内容**。只有部署方拿得到，别人是 `null`。
   *
   * 与上面那几个字段的区别：那些是"我这次生成会用哪份配置"（会一路回落到环境
   * 变量），这里是"共享那一行存了什么"。部署方把它清空之后，`base_url` 会回落成
   * 环境变量里的值，而 `instance.base_url` 如实变成空串。
   */
  instance: InstanceSettings | null;
  /** 库里那一行最后保存的时刻；没保存过是 `null`。 */
  updated_at?: string | null;
}

/**
 * 全站共享那一行（`owner = ''`）的样子。只有部署方能看到。
 *
 * 游客、以及还没填自己密钥的账号，生成时用的就是这一份——所以面板有必要让部署方
 * 看到它现在配的是什么，否则"游客说用不了"时无从对照。
 */
export interface InstanceSettings {
  base_url: string;
  /** 打码后的密钥：`…abcd`；没配时是空串。**永远不是明文**。 */
  api_key_masked: string;
  api_key_set: boolean;
  model: string;
  deep_model: string;
  /** 三项齐了没有。不齐时借它兜底的人依旧没有可用配置。 */
  configured: boolean;
  updated_at?: string | null;
}

/** `PUT /settings/model` 的请求体。**每个字段的"空"含义不同**。 */
export interface SaveModelSettingsRequest {
  /** 省略 = 不改；空串 = 清掉覆盖、回落环境变量。 */
  base_url?: string | null;
  /**
   * 省略或**空串** = 不改。界面上那个框显示打码值，用户不动它时提交的就是空串。
   * 要清空必须 `clear_api_key: true`——把空串当"清空"，等于每次改模型名就抹一次密钥。
   */
  api_key?: string | null;
  /** 省略 = 不改；空串 = 清掉覆盖、回落环境变量。 */
  model?: string | null;
  deep_model?: string | null;
  /** 显式清除已保存的密钥。与同时提交 `api_key` 冲突（400 `settings_clear_conflict`）。 */
  clear_api_key: boolean;
  /**
   * 写到哪一行：`user`（缺省）= 你自己那行；`instance` = 全站共享那行。
   *
   * `instance` 只有部署方可以，别人 403 `settings_forbidden`——权限判定在后端，
   * 这里只是把两个作用域区分开。缺省值刻意是 `user`：老客户端（与不传这个字段的
   * 调用）行为与改造前一致。
   */
  scope?: 'user' | 'instance';
}

/** `PUT` 的响应：写入后的生效配置 + 这次有没有真的改变（同值重复保存是 `false`）。 */
export interface SaveModelSettingsResponse extends ModelSettings {
  changed: boolean;
  /**
   * 需要提醒用户的一句话，通常是"地址改了，之前那次连接成功的结论不再成立"。
   *
   * 空值 = 没什么要说的。**只提醒不阻止**：合法用法里有"先填地址、等供应商开白名单"，
   * 强制当场测通会把这条路堵死。
   */
  notice?: string | null;
}

/** `POST /settings/model/test` 的请求体：可选的临时覆盖，空字段 = 用当前生效值。 */
export interface TestModelSettingsRequest {
  base_url?: string | null;
  api_key?: string | null;
  model?: string | null;
}

/**
 * 一次测试连接的结论。`ok=false` 也是 **200**——它是结论，不是 HTTP 失败。
 *
 * 真正的业务错误（三项都没配）才走 400：那时连测都没法测。
 */
export interface TestModelSettingsResponse {
  ok: boolean;
  /** 可直接显示的一句中文，例如「密钥被拒绝（401）」或「连接正常（320 毫秒）」。 */
  message: string;
  /** 这次真正拿去测的模型名。 */
  model: string;
  /** 对端 HTTP 状态码；超时/连不上时是 `null`。 */
  status?: number | null;
  latency_ms: number;
}

// ---- 账号与会话（`/api/auth/*`）----
//
// 会话本身是 **HttpOnly cookie**，所以这里的类型里没有 token 字段——浏览器替我们
// 保管，JS 拿不到（那正是它防 XSS 的方式）。前端只需要知道"现在是谁"。
//
// `kind` 由后端算而不是前端拼：`user ? 'user' : guest_id ? 'guest' : 'anonymous'`
// 三个分支写在界面里，迟早会在某处漏掉一个，而漏掉的表现是"游客被当成已登录"。

/** 对外的账号形状。**不含任何口令相关字段**。 */
export interface AuthUser {
  id: string;
  username: string;
  created_at: string;
}

/** `GET /api/auth/me` 的响应：当前是谁。未登录时 `user` 为 `null`，**永远是 200**。 */
export interface WhoAmI {
  user?: AuthUser | null;
  /** 这台浏览器的游客编号。已登录时可能为 `null`（登录请求不一定带游客 cookie）。 */
  guest_id?: string | null;
  kind: 'user' | 'guest' | 'anonymous';
}

/** 注册与登录共用的请求体。 */
export interface Credentials {
  username: string;
  password: string;
}

/** 改密码：需要旧密码，并且需要已经登录。 */
export interface ChangePasswordRequest {
  current_password: string;
  new_password: string;
}

// ---- 编辑日志（统计"用户最常改什么"）----
//
// 这几个类型是**日志载荷**，不是导图契约：它们的形状由 `POST/PUT /mindmaps` 的
// 请求体决定，后端 `EditEventIn` 带 `extra="forbid"`。所以字段名漂移的表现是
// **保存直接 422**，不是字段被静默丢掉——那正是 `api/contract-check.ts` 存在的
// 理由（它防的是静默丢字段），所以这里刻意不把它加进那份检查：多一道编译期镜像
// 换不来安全，只会让加一个统计字段要改三个文件。

/** 一条改动。`at` 与 `depth` 的语义见 `src/mindmap/edit-log.ts`。 */
export interface EditEventPayload {
  actor: 'user' | 'agent';
  /**
   * `generate` 是"一次生成落到画布"（新数据）；`accept` 是它的前身（候选层还在时
   * "用户点了应用"），只在历史行里出现。两字样都在，是因为库里既有老行也有新行。
   */
  kind: 'add' | 'remove' | 'update' | 'move' | 'edge' | 'style' | 'accept' | 'generate';
  node_id?: string;
  field?: 'label' | 'title' | 'detail' | 'note' | 'color' | 'icon';
  /** 摘要，不是内容（`detail`/`note` 只存 `"1800字"` 这种长度）。 */
  before?: string;
  after?: string;
  depth?: number;
  /**
   * 这条 `remove` 一共删掉几个节点（含自己）。`1` = 零散删除，`12` = 整支一起删。
   *
   * 只有 `kind='remove'` 有值；`subtree_size` 为空的老数据是"当时没记"，统计里
   * 单列成 `removed_subtree.size_unknown`，不会被当成 `1` 混进 `removed_depth`。
   */
  subtree_size?: number;
  /** 历史列（`kind='accept'` 才有）。新数据不写它：分母改由 `node_count` 承担。 */
  change_count?: number;
  /**
   * 这次生成落进画布的那棵树有几个节点。只有 `kind='generate'` 有值。
   *
   * **在生成完成那一刻算好**：保存时才数会数到用户改之后的树，分母永久偏高。
   */
  node_count?: number;
  run_id?: string;
  at: string;
}

/** 落库后的一行：载荷 + 自增 id + 入库时刻（`GET /mindmaps/{id}/edits`）。 */
export interface EditEventRow extends EditEventPayload {
  id: number;
  /**
   * 服务端写入时刻。与 `at` 分开是为了能诊断"事件说它 09:31 发生、
   * 实际 09:35 才入库"——前端的时钟不可信，两者相等才是常态。
   */
  recorded_at: string;
  map_id: string;
}

/**
 * 统计结果（`GET /mindmaps/{id}/edits/summary`）。
 *
 * 两条恒等式让这份数字可以先自校验再读结论：
 *  - `sum(by_actor) === total`；
 *  - `sum(by_field) === by_kind.update + by_kind.style`（只有这两种事件带 field）。
 */
export interface EditSummary {
  total: number;
  /** 最近一条事件的**入库**时刻；空表是 `null`（不是空串）。死表探测器的读数。 */
  last_event_at: string | null;
  by_actor: Record<string, number>;
  by_kind: Record<string, number>;
  by_field: Record<string, number>;
  /** 变短的标题改动，**只算 `actor='user'`**。`count` 为 0 时两个平均值为 `null`。 */
  label_shrunk: { count: number; avg_before: number | null; avg_after: number | null };
  /**
   * 删掉的节点按层分布，键是层数（根=1）的字符串形式。
   *
   * **只统计零散删除**（`subtree_size = 1`）：一条 remove 现在代表一整棵被删的
   * 子树，根所在的层不等于"这一层的内容被嫌弃"。
   */
  removed_depth: Record<string, number>;
  /**
   * 删除规模直方图：`{size_1: 5, size_12: 1, size_unknown: 2}`。
   *
   * `size_unknown` 是加列之前的老数据（`subtree_size` 为 NULL），它们**不进**
   * `removed_depth`——否则那次"每层都在被删"的污染还在。
   */
  removed_subtree: Record<string, number>;
}

// ---- 结构诊断与建议（`POST /api/optimization/check` / `suggest`）----
//
// 镜像 `backend/app/contracts/diagnostic.py`。两个端点都**只读**：不落库、不改任何
// 东西（后端 docstring 的原话是"只读诊断""用户确认后才能应用"）。所以"生成完成后
// 自动跑一次 check"是零成本的——它既不会改用户的作品，也不会有模型调用。
//
// `code` / `kind` / `level` 一律声明成 `string`，**不是**字面量联合。这是刻意的：
// 后端正在持续加新的诊断码（近义冗余、缺失环节…），写成 `"SINGLE_CHILD" | …` 之后
// 每来一个新码前端都要跟着改类型；而界面要的只是"把不认识的东西原样显示出来"。
// 人话映射在 `mindmap/diagnostics.ts`，认不出来的码就显示码本身。

/** 一条诊断。`level` 目前是 info / warn / error；未知值按 info 画。 */
export interface Diagnostic {
  level: string;
  code: string;
  /** 指向哪个节点；`null` 表示这条诊断不针对单个节点（如整图层面的问题）。 */
  node_id?: string | null;
  message: string;
  /** 原文片段或相似节点 id。界面原样展示，不解释。 */
  evidence?: string | null;
}

/**
 * 一条**候选**修改。必须由用户确认才应用——后端没有自动应用接口。
 *
 * `kind` 决定"应用"这个动作到底做什么（`structure.ts::applySuggestion`）。只有
 * `dedupe` 有定义（删掉那个重复节点）；其余 kind 后端还没给出对应的操作，界面
 * 只显示不给按钮——摆一个点了不知道会发生什么的按钮比不摆更糟。
 */
export interface Suggestion {
  /** 每次请求都会变（后端 `uuid4` 默认值）。**不要**拿它当"忽略过"的键。 */
  id?: string;
  kind: string;
  node_id?: string | null;
  message: string;
  evidence?: string | null;
  /** 规则建议没有经过概率校准；`null` = 未评估，与"置信 0"不是一回事。 */
  confidence?: number | null;
}

export interface CheckResponse {
  diagnostics: Diagnostic[];
}

/**
 * `kind="link"` 那一层这一趟的结果（`POST /optimization/suggest` 的 `link`）。
 *
 * 为什么它不是可选的"附加信息"：link 是唯一**可能一条都不给**的建议类型——
 * 不花钱的规则层几乎总有三五条，而 link 要先挑目标、再逐目标调一次模型，任何一环
 * 不成立结果就是空列表。空列表与"这个功能没接上"在界面上长得一模一样，所以原因
 * 单独带出来给用户看。
 *
 * `status` 的取值：`ok`（至少一条）/ `already_best`（问过，模型认为现在挂得对）/
 * `no_targets`（没有可问的节点）/ `disabled`（部署方关了）/ `no_choices` /
 * `no_candidates` / `model_failed` / `failed`。
 */
export interface LinkStatus {
  status: string;
  /** 被问过"该挂哪儿"的节点 id，按实际调用顺序。 */
  targets: string[];
  /** 真的发出去了几次模型调用。0 = 这一层没花钱。 */
  calls: number;
  /** 模型说"现在这样最好"因此没有产出建议的条数。 */
  dropped: number;
  /** 一句给用户看的话（空串表示不必说）。 */
  detail: string;
}

export interface SuggestResponse {
  suggestions: Suggestion[];
  diagnostics: Diagnostic[];
  /** `null` 表示这次请求**没跑** link 那一层（`link: false` 或自动路径）。 */
  link?: LinkStatus | null;
}

/**
 * 跨图的链路健康检查（`GET /mindmaps/edits/health`）。
 *
 * 判据只有一句：**"保存了但没有任何事件"在正常情况下不该发生**——每次生成落画布都记
 * 一条 `generate`，连一字未改也记。所以"昨日有保存、昨日零事件"只能是链路断了
 * （前端在发、后端在丢）。这正是那张表能静默坏上 40 分钟的原因：它没有死表探测器。
 */
export interface EditHealth {
  /** 统计窗口：UTC 昨日整天（`[window_start, window_end)`，与库里时间格式一致）。 */
  window_start: string;
  window_end: string;
  /** 昨日有编辑事件的图数（`COUNT(DISTINCT map_id)`）。 */
  edited_maps: number;
  /** 昨日保存过的图数。`mindmaps.updated_at` 只留最后一次保存，所以这是下界。 */
  saved_maps: number;
  /** `saved_maps > 0 && edited_maps === 0`：日志链路可能是断的。 */
  chain_suspect: boolean;
  /** 一句话结论，界面直接显示。 */
  detail: string;
}
