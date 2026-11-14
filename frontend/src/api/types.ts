/** 后端契约的 TypeScript 镜像。字段以 backend/app/contracts/mindmap.py 为准。
 *
 *  2026-11-10 起后端是契约 v2：本文件声明了 v2 的字段，但界面只读写
 *  `note` / `layout` / `theme.direction`，其余（`relations` / `style` / `pos` /
 *  `collapsed`）只做透传保存。
 *
 *  `schema.d.ts` 已由 `openapi-typescript` 从 `backend/openapi.json` 生成，
 *  但本文件**决定保留**：手写镜像读起来比生成的类型省事，两者靠
 *  `contract-check.ts` 的编译期断言保持一致（见 `docs/CONTRACT_V2.md`）。 */
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
  style?: NodeStyle | null;
  pos?: NodePosition | null;
}

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
}

/**
 * 改节点。
 *
 * `label`、`detail` 与 `source_refs` 缺省（undefined）表示"这一项不动"，与"清空"
 * 区分开：`source_refs: []` 是显式清空。这个区别丢不得。
 */
export interface UpdateOp {
  op: 'update';
  id: string;
  changes: { label?: string; source_refs?: SourceRef[]; detail?: string | null };
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

export type MindMapOp = AddOp | UpdateOp | RemoveOp | MoveOp;

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

/** 与后端 `Relation` 一致。界面不渲染，原样透传。 */
export interface MindRelation {
  id: string;
  source: string;
  target: string;
  type: 'cause' | 'ref' | 'conflict' | 'depends';
  label?: string | null;
  evidence?: string | null;
  provenance?: 'user' | 'ai' | 'import';
}

/** 已保存的导图记录。`version` 是乐观锁的版本号，保存时要原样带回去。 */
export interface MapRecord {
  id: string;
  version: number;
  content: MindMapContent;
  updated_at: string;
}

// ---- 来源（文档解析）----

/** 来源模态，镜像后端 SourceKind。 */
export type SourceKind = 'text' | 'markdown' | 'document' | 'image' | 'audio';

export interface SourceUsage {
  source_id: string;
  kind: SourceKind;
  filename: string | null;
  char_count: number;
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

// ---- agent 运行 ----

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
   * 拆解方式：同一句主题换一套步骤，长出来的树结构就不同。
   *
   * 取值与后端 `generation.service.THINKING_MODELS` 的键一一对应；后端是
   * Literal，拼错会被 422 拒（不是静默退化成 default），所以这里保持宽松的
   * string，由下拉框保证只在五个值里出现。缺省即 `default`。
   */
  thinking_model?: string;
  snapshot?: MindMapContent | null;
  selected_ids?: string[];
  /**
   * 本次请求附带的来源，镜像后端 `AgentRunRequest.sources`（max_length=50）。
   *
   * 材料正文走这里，不拼进 goal：拼进去会被当成"用户的主题"，
   * 而模型需要的是带 source_id 的结构化数据——否则它写不出合法的引用。
   */
  sources?: Source[];
}

/**
 * agent 事件流。`seq` 从 1 连续递增，前端靠它判断丢帧与乱序。
 *
 * 生成只剩这一条通道，所以这就是唯一的帧形状；状态机完整地待在 store 的
 * `runGeneration` 里，一个 switch 能从头读到尾。
 */
export type AgentEvent =
  | { type: 'stage'; seq: number; stage: string; detail?: string }
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
  | { type: 'snapshot'; seq: number; content: MindMapContent }
  | {
      type: 'done';
      seq: number;
      content?: MindMapContent | null;
      usage?: Record<string, unknown>;
      /**
       * 这棵树是演示规则拼出来的还是模型产出的。
       *
       * 后端按 `exclude_none=True` 编码，正常帧一定带值；字段缺失只说明对方
       * 是没填 mode 的旧版本——那是"没说"，不是"模型"。
       */
      mode?: 'demo' | 'model' | null;
    }
  | { type: 'error'; seq: number; code: string; message: string; limit?: string };
