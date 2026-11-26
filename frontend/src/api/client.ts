import type {
  AliasClearance,
  AliasDeletion,
  AliasListing,
  AgentEvent,
  AgentRunRequest,
  AuthUser,
  ChangePasswordRequest,
  CheckResponse,
  CreateTextSourceResponse,
  Credentials,
  Diagnostic,
  EditEventPayload,
  EditEventRow,
  EditHealth,
  EditSummary,
  ImportResult,
  LibraryDeletion,
  LibraryList,
  LibrarySource,
  MapRecord,
  MindMapContent,
  DiagramContent,
  ModelSettings,
  Pref,
  PrefSuggestion,
  RunDetail,
  RunSummary,
  SaveModelSettingsRequest,
  SaveModelSettingsResponse,
  SavedPref,
  Source,
  SuggestResponse,
  TestModelSettingsRequest,
  TestModelSettingsResponse,
  WhoAmI,
} from './types';
import type { EditEvent } from '../mindmap/edit-log';

/** 后端错误体现在只返回 detail；契约 v2 会带 code/request_id，这里先兼容两种。 */
interface ErrorBody {
  code?: string;
  message?: string;
  detail?: unknown;
  request_id?: string;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

const BASE = import.meta.env.VITE_API_BASE ?? '/api';

function errorMessage(status: number, body: ErrorBody): string {
  if (typeof body.message === 'string' && body.message) return body.message;
  if (typeof body.detail === 'string' && body.detail) return body.detail;
  return `请求失败（HTTP ${status}）`;
}

/**
 * 后端错误体 → ApiError。JSON 解析失败时退化成空对象，保底给出 HTTP 状态码。
 * JSON 请求与 multipart 上传共用这一处，免得两边文案各自漂移。
 */
async function toApiError(response: Response): Promise<ApiError> {
  const body = (await response.json().catch(() => ({}))) as ErrorBody;
  return new ApiError(
    response.status,
    body.code ?? `http_${response.status}`,
    errorMessage(response.status, body),
    body.request_id,
  );
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      // 会话是 HttpOnly cookie，必须让浏览器带上它。`same-origin` 是缺省值，够用
      // 于"前端与后端同一个源"（生产就这样，开发时 Vite 代理 `/api` 也是）；但
      // `VITE_API_BASE` 指向另一个端口时它就不带了——而那个场景下同源策略放行的
      // 正是我们自己（见 `app/main.py` 的 CORS `allow_credentials=True`）。
      // 写 `include` 两个场景都对，且它让"这个请求要凭证"这件事在代码里可见。
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', ...init?.headers },
    });
  } catch {
    throw new ApiError(0, 'network_error', '无法连接后端，请确认服务已启动。');
  }

  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

// ---- 基础服务 ----

/**
 * 后端自己报的版本。线上由 `deploy/install.sh` 写进 `MINDMAP_VERSION`，
 * 本机跑源码时是 `dev`——页面标题旁显示它，用来确认域名上跑的是哪一次部署。
 */
export interface Health {
  status: string;
  version: string;
}

export function getHealth(): Promise<Health> {
  return request<Health>('/health');
}

// ---- 保存与打开 ----

/** 已保存的导图列表（不含筛选，按更新时间倒序由后端决定）。 */
export function listMindMaps(): Promise<MapRecord[]> {
  return request<MapRecord[]>('/mindmaps');
}

export function getMindMap(id: string): Promise<MapRecord> {
  return request<MapRecord>(`/mindmaps/${encodeURIComponent(id)}`);
}

/**
 * **删掉一张导图。不可恢复**（连带它的改动账本）。
 *
 * 204 没有 body，`request` 对 204 返回 `undefined`，所以返回类型是 `void`——给一个
 * `{deleted: true}` 只会让调用方多一种"成功但字段为假"的可能，而删掉的东西没有
 * 可回报的形状。
 *
 * 界面**必须先二次确认**（`HistoryDrawer` 与 `App` 的列表都是两段式）：这个仓库里
 * 唯一其它"彻底删除"是知识库的材料，那次在 409 上还能被引用计数拦一下；删导图没有
 * 任何东西会拦它。
 */
export function deleteMindMap(id: string): Promise<void> {
  return request<void>(`/mindmaps/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

/** 编辑日志的事件 → 请求体里的形状（前端的 camelCase 只活在内存里）。 */
function toEventPayload(event: EditEvent): EditEventPayload {
  return {
    actor: event.actor,
    kind: event.kind,
    at: event.at,
    ...(event.nodeId !== undefined ? { node_id: event.nodeId } : {}),
    ...(event.field !== undefined ? { field: event.field } : {}),
    ...(event.before !== undefined ? { before: event.before } : {}),
    ...(event.after !== undefined ? { after: event.after } : {}),
    ...(event.depth !== undefined ? { depth: event.depth } : {}),
    ...(event.subtreeSize !== undefined ? { subtree_size: event.subtreeSize } : {}),
    ...(event.nodeCount !== undefined ? { node_count: event.nodeCount } : {}),
    ...(event.runId !== undefined ? { run_id: event.runId } : {}),
  };
}

/**
 * 保存请求体。
 *
 * 空事件数组**不发字段**：与 `sources` / `prefs_applied` 同一条约定——"这次一条
 * 都没记"与"这个字段不存在"对读的人来说是同一件事，而少发一个字段让一次没有改动的
 * 保存与上一版逐字节相同。
 *
 * `truncated` 是"这一次没带上多少条"（超出 `EDIT_EVENT_LIMIT` 的部分留在前端队列里
 * 等下次），只在真的发生时才发：后端据此记一条 warning，不落表。
 */
function saveBody(
  content: DiagramContent,
  events: readonly EditEvent[],
  truncated: number,
): { content: DiagramContent; events?: EditEventPayload[]; truncated?: number } {
  const body: { content: DiagramContent; events?: EditEventPayload[]; truncated?: number } = {
    content,
  };
  if (events.length) body.events = events.map(toEventPayload);
  if (truncated > 0) body.truncated = truncated;
  return body;
}

/** 第一次保存：创建一条新记录，版本由服务端给（1）。 */
export function createMindMap(
  content: DiagramContent,
  events: readonly EditEvent[] = [],
  truncated = 0,
): Promise<MapRecord> {
  return request<MapRecord>('/mindmaps', {
    method: 'POST',
    body: JSON.stringify(saveBody(content, events, truncated)),
  });
}

/**
 * 覆盖保存已有导图。`version` 是"我打开时看到的版本"：
 * 服务端发现不是最新就返回 409，不会用旧内容盖掉别人的修改。
 *
 * `events` 与内容同一次请求提交，理由见 `stores/mindmap.ts::save()`：
 * 第一次保存前没有 `map_id`，独立端点根本发不出去。
 */
export function updateMindMap(
  id: string,
  content: DiagramContent,
  version: number,
  events: readonly EditEvent[] = [],
  truncated = 0,
): Promise<MapRecord> {
  return request<MapRecord>(`/mindmaps/${encodeURIComponent(id)}`, {
    method: 'PUT',
    body: JSON.stringify({ ...saveBody(content, events, truncated), version }),
  });
}

// ---- 编辑日志（"用户最常改什么"）----

/**
 * 这张图的改动统计。
 *
 * 这是这一版真正的交付物：`label_shrunk` 回答"标题是不是被普遍改短"，
 * `removed_depth` 回答"删掉的集中在第几层"——两个都直接指向生成侧该改什么。
 */
export function getEditSummary(id: string): Promise<EditSummary> {
  return request<EditSummary>(`/mindmaps/${encodeURIComponent(id)}/edits/summary`);
}

/**
 * 最近的改动明细，新的在前。
 *
 * 统计给的是分布，明细回答"那 42 条具体是什么"——没有它，一个反常的平均值
 * 只能靠连数据库才查得下去。
 */
export function listEditEvents(id: string, limit = 50): Promise<EditEventRow[]> {
  return request<EditEventRow[]>(
    `/mindmaps/${encodeURIComponent(id)}/edits?limit=${encodeURIComponent(limit)}`,
  );
}

/**
 * 跨图的链路健康检查：昨日编辑过的图数 / 昨日保存的图数。
 *
 * 单张图的 `summary` 只能回答"这张图记了什么"，回答不了"这张表还在工作吗"——
 * 一条事件都收不到时，每张图的 `total` 都是 0，而 0 看起来和"没人改过"一模一样。
 * **"保存了却一条事件都没有"在正常情况下不该发生**（每次生成落画布都记一条
 * `generate`），所以这个比值是断链的可见证据。
 */
export function getEditHealth(): Promise<EditHealth> {
  return request<EditHealth>('/mindmaps/edits/health');
}

// ---- agent 流式运行 ----

/**
 * 通用 SSE 读取：POST 一个 JSON，按帧回调。
 *
 * 不用 EventSource 是因为它只能发 GET，而输入文本可能很长。这里用 fetch 的
 * ReadableStream 自己切帧：SSE 的帧以空行分隔，每帧取第一条 `data: ` 行。
 *
 * 单独抽出来是为了让帧解析只有一份：`[DONE]` 终止帧、分片边界、单帧解析失败
 * 跳过，这些细节各写一遍就会各自漂移，出现"同样的网络条件下只有一条流偶发丢帧"。
 */
async function postSse<T>(
  url: string,
  body: unknown,
  onEvent: (event: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
      // 与 `request()` 同一个理由：会话 cookie 不带上，生成请求就是"未登录"的。
      credentials: 'include',
    });
  } catch (error) {
    if ((error as Error).name === 'AbortError') return;
    throw new ApiError(0, 'network_error', '无法连接后端，请确认服务已启动。');
  }

  if (!response.ok || !response.body) {
    const errorBody = (await response.json().catch(() => ({}))) as ErrorBody;
    throw new ApiError(response.status, errorBody.code ?? `http_${response.status}`,
      errorMessage(response.status, errorBody), errorBody.request_id);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const flush = (frame: string) => {
    const line = frame.split('\n').find(part => part.startsWith('data: '));
    if (!line) return;
    const data = line.slice(6).trim();
    // 终止帧沿用 OpenAI 的 [DONE] 约定，不是 JSON，遇到即表示流正常结束。
    if (data === '[DONE]') return;
    try {
      onEvent(JSON.parse(data) as T);
    } catch {
      // 单帧解析失败不该打断整条流，跳过即可。
    }
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut = buffer.indexOf('\n\n');
      while (cut >= 0) {
        flush(buffer.slice(0, cut));
        buffer = buffer.slice(cut + 2);
        cut = buffer.indexOf('\n\n');
      }
    }
    if (buffer.trim()) flush(buffer);
  } catch (error) {
    // 主动取消（点「取消」）不是错误；其余中断要让调用方知道流没走完，
    // 否则界面会停在「正在生成」上，看起来像卡死。
    if ((error as Error).name === 'AbortError') return;
    throw new ApiError(0, 'stream_interrupted', '连接中断，请重试。');
  }
}

export function runAgentStream(
  body: AgentRunRequest,
  onEvent: (event: AgentEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return postSse<AgentEvent>(`${BASE}/agent/runs/stream`, body, onEvent, signal);
}

// ---- 运行台账（run 的身份与草稿）----

/**
 * 上次跑完、但结果还没被保存的运行。
 *
 * 默认取后端给的"有结果且没保存过"那一批，其中**包含"被取消但已经产出结果"的
 * 运行**：那正是"关掉页面那 40 秒别白花"要捡回来的东西。只查 `done` 的话，
 * 结果会被写进库、然后再也没人读过——表就成了一行字。
 */
export function listRuns(status?: RunSummary['status']): Promise<RunSummary[]> {
  const query = new URLSearchParams({ unapplied: 'true' });
  if (status) query.set('status', status);
  return request<RunSummary[]>(`/agent/runs?${query.toString()}`);
}

/** 单条详情，带结果正文。只有点「载入」时才值得走这一趟。 */
export function getRun(runId: string): Promise<RunDetail> {
  return request<RunDetail>(`/agent/runs/${encodeURIComponent(runId)}`);
}

/**
 * 这次生成的结果**被保存进库了**。
 *
 * 它不是"用户点了应用"（那个动作已经不存在了，生成结果直接落画布）：删掉候选层
 * 之后，`applied_at` 回答的是"这份结果后来有没有存下来"，也正是"下次进页面还要
 * 不要提醒"的判据。幂等，只在本来为空时打第一个时间戳，所以前端可以放心重试。
 */
export function markRunSaved(runId: string): Promise<void> {
  return request<void>(`/agent/runs/${encodeURIComponent(runId)}/applied`, {
    method: 'POST',
  });
}

// ---- 结构诊断与建议（两个都只读）----

/**
 * `POST /optimization/*` 的请求体。
 *
 * `sources` 为空时**不发这个字段**：与保存请求同一条约定——"这次没带材料"与"这个
 * 字段不存在"对读的人来说是同一件事，而少发一个字段让一次不带材料的体检与上一版
 * 逐字节相同。后端两个端点共用 `CheckRequest`，`sources=None` 就是"不做引用核验"。
 */
function optimizationBody(
  content: DiagramContent,
  sources?: Source[],
): { content: DiagramContent; sources?: Source[]; link?: boolean } {
  const body: { content: DiagramContent; sources?: Source[]; link?: boolean } = { content };
  if (sources && sources.length) body.sources = sources;
  return body;
}/**
 * 只读诊断：单子节点 / 深度上限 / 同级重名，给了 `sources` 时还会按原文核验引用。
 *
 * **它不改任何东西**（后端 docstring：“只读诊断”）：不落库、不写回树、不调模型。
 * 所以"生成完成之后自动跑一次"是安全的，也可以放心重跑——重跑一百次与一次等价。
 *
 * 形状做了一次归一（`Array.isArray`）：后端换版本、或者测试替身回了个空对象时，
 * 界面拿到的是"零条诊断"，而不是一个 undefined 崩在半路。诊断是附加信息，
 * 拿不到就当没有——它不该把生成结果的成功态弄成失败态。
 */
export async function checkOptimization(
  content: DiagramContent,
  sources?: Source[],
): Promise<CheckResponse> {
  const body = await request<Partial<CheckResponse>>('/optimization/check', {
    method: 'POST',
    body: JSON.stringify(optimizationBody(content, sources)),
  });
  return { diagnostics: Array.isArray(body?.diagnostics) ? body.diagnostics : [] };
}

/**
 * 候选修改（**用户确认后才应用**；后端目前没有自动应用接口）。
 *
 * 与 `checkOptimization` 分成两个函数，不是把两者合并成一次往返：`check` 是免费的
 * 只读诊断，`suggest` 现在有两处会花钱——模型层（近义判定）与 link 层（"这个节点
 * 该挂在谁下面"）。分开之后"免费的自动体检"与"点了才发的建议"各走各的路径。
 *
 * `options.link` 明确传 `false` 时这一层一次调用都不发（后端 `link` 字段）；
 * 不传 = 由后端按部署开关决定（缺省开，见 `optimization/router.py` 的注释）。
 * 这里**不主动传 true**：默认值只有一处（服务端），前端再写一份就会在两边漂。
 */
export async function suggestOptimization(
  content: DiagramContent,
  sources?: Source[],
  options: { link?: boolean } = {},
): Promise<SuggestResponse> {
  const payload = optimizationBody(content, sources);
  if (options.link !== undefined) payload.link = options.link;
  const body = await request<Partial<SuggestResponse>>('/optimization/suggest', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  return {
    suggestions: Array.isArray(body?.suggestions) ? body.suggestions : [],
    diagnostics: Array.isArray(body?.diagnostics) ? body.diagnostics : [],
    // `null` 与"没这个字段"都表示这次没跑 link 那一层，界面据此不加那一段话。
    link: body?.link ?? null,
  };
}

// ---- 自动修复（三条"修法唯一"的规则）----

/**
 * 一条修复动作。`kind` 是 `merge_duplicate` / `collapse_chain`（后端
 * `RepairKind`），`code` 是它对应的诊断码——一次合并可能同时被两个码解释，
 * 所以两者分开（与后端同一条口径）。
 */
export interface RepairStep {
  code: string;
  kind: string;
  node_id: string;
  /** 一句人话，界面逐条显示"改了什么"。后端保证它写清了新标题是什么。 */
  message: string;
  /**
   * 反操作（`RepairUndo`：槽位 + 字段快照 + 重定向记录）。
   *
   * **前端不读它**。撤销走 store 的快照栈（一步回到修复之前），不逐条重放
   * `RepairUndo`——理由写在 `stores/mindmap.ts::replaceContent`。所以这里故意
   * 声明成 `unknown` 而不是把后端那个结构抄一遍：抄一份没人读的类型，只会在后端
   * 改字段时给出假的"我懂它"。
   */
  undo?: unknown;
}

/** 看到了但没修的那一条（超出契约上限、或诊断在处理途中过期）。 */
export interface RepairSkip {
  code: string;
  node_id?: string | null;
  reason: string;
}

export interface RepairResult {
  /** 修好的树。**整体替换**，不是补丁：修复会删节点、改标题、搬子树。 */
  content: MindMapContent;
  applied: RepairStep[];
  skipped: RepairSkip[];
  /** 修完之后重跑的 `check()`：界面据此说"还剩几条"。 */
  diagnostics: Diagnostic[];
  steps: number;
  /** 一句人话（无修复时是空串），例如"合并了 3 处同级重名"。 */
  summary: string;
}

/**
 * 把树修到不动点（`POST /optimization/repair`）。
 *
 * ## 它不进 openapi 快照，但接口是通的
 *
 * 后端那条路由挂着 `include_in_schema=False`（理由见 `optimization/router.py`：
 * 这一轮 `openapi.json` 与 `schema.d.ts` 由另一条线在改，不能重导）。那一个参数
 * **只影响文档与生成的类型**，不影响路由本身——所以这里手写上面那几个类型，
 * 请求照常打到 `POST /api/optimization/repair`。等快照能重导的那一轮，把这段换成
 * 生成的类型，`include_in_schema` 一起删掉（两步都要做）。
 *
 * ## 为什么不带 `codes`
 *
 * 后端缺省就是三条全修。规则之间是有交互的（合并会改同名集合、压链会改 label，
 * 所以它两段交替到不动点），分三次点会得到"每点一次都还没修完"。要细分的时候
 * 让后端定义清楚"只修合并"是什么意思，而不是让前端凭 `codes` 参数猜。
 *
 * ## 失败形状
 *
 * 诊断类响应（check/suggest）拿不到时降级成"零条"；这个**不能降级**：返回体里没有
 * `content` 就等于"没有可落地的树"，静默返回一棵原样的树会让用户以为修过了。
 */
export async function repairOptimization(content: DiagramContent): Promise<RepairResult> {
  const body = await request<Partial<RepairResult>>('/optimization/repair', {
    method: 'POST',
    body: JSON.stringify({ content }),
  });
  if (!body?.content) {
    throw new ApiError(200, 'empty_repair', '后端没有返回修复结果，这次修复没有落地。');
  }
  const applied = Array.isArray(body.applied) ? body.applied : [];
  return {
    content: body.content as MindMapContent,
    applied,
    skipped: Array.isArray(body.skipped) ? body.skipped : [],
    diagnostics: Array.isArray(body.diagnostics) ? body.diagnostics : [],
    // 后端给了就信它，没给就按 `applied` 数：这条数字界面要显示，不能是 NaN。
    steps: typeof body.steps === 'number' ? body.steps : applied.length,
    summary: typeof body.summary === 'string' ? body.summary : '',
  };
}

// ---- 文档上传 ----

/**
 * 上传文档，由后端解析成文本来源。
 *
 * 不走 request()：它固定带 `Content-Type: application/json`，而 multipart 的
 * boundary 必须由浏览器自己生成。手动设头的结果是后端拆不出 file 字段，
 * 所以这里只给 method 和 body，让 fetch 自己填类型。
 */
export async function uploadDocument(file: File, signal?: AbortSignal): Promise<Source> {
  const form = new FormData();
  form.append('file', file);

  let response: Response;
  try {
    response = await fetch(`${BASE}/ingestion/files`, {
      method: 'POST', body: form, signal, credentials: 'include',
    });
  } catch {
    throw new ApiError(0, 'network_error', '无法连接后端，请确认服务已启动。');
  }

  if (!response.ok) throw await toApiError(response);

  const body = (await response.json().catch(() => null)) as CreateTextSourceResponse | null;
  if (!body?.source?.id) {
    // 契约里解析失败一定走 422；拿到空壳说明后端出了问题，别把 undefined 传给下一步。
    throw new ApiError(response.status, 'empty_source', '后端没有返回解析结果，请重试。');
  }
  return body.source;
}

/**
 * 按 URL 读一篇网页 → 一份材料（`POST /api/knowledge/fetch`）。
 *
 * 与 `uploadDocument` 共用同一套错误处理：后端已经把三种失败分成了三种状态码
 * （409 没打开 / 503 没配 key / 502 抓不到），`ApiError.message` 里就是给人看的那句话，
 * 调用方**直接显示**，不要再拼一层"抓取失败"——那会把"去把开关打开"盖成一句废话。
 */
export async function fetchUrlSource(url: string, signal?: AbortSignal): Promise<Source> {
  const body = await request<CreateTextSourceResponse>('/knowledge/fetch', {
    method: 'POST',
    body: JSON.stringify({ url }),
    signal,
  });
  if (!body?.source?.id) {
    throw new ApiError(0, 'empty_source', '后端没有返回抓取结果，请重试。');
  }
  return body.source;
}

// ---- 用户偏好 ----

/**
 * 全部偏好，带每条的"上次生效"。
 *
 * 启动时调一次，结果填进 `usePrefs` 与 `useGenerationOptions`：偏好面板要列它，
 * 生成面板的下拉要拿它当预填值。**两处读同一份**——各拉一次会先制造"面板改完、
 * 下拉没变"的空窗。
 */
export function listPrefs(): Promise<Pref[]> {
  return request<Pref[]>('/prefs');
}

/**
 * 写一条。`changed=false` 表示这次提交没有改变任何东西（同值）。
 *
 * `source` 只有用户在建议里点"设为默认"时才传 `suggestion_accepted`：两种来源在
 * 面板上显示不同的标签，而那个区别只有写入的这一刻知道。
 */
export function setPref(
  key: string,
  value: string,
  source: 'user_explicit' | 'suggestion_accepted' = 'user_explicit',
): Promise<SavedPref> {
  return request<SavedPref>(`/prefs/${encodeURIComponent(key)}`, {
    method: 'PUT',
    body: JSON.stringify({ value, source }),
  });
}

/** 删一条。**硬删**：没有软删，也就不存在"删了但还能翻出来"这种界面谎言。 */
export function deletePref(key: string): Promise<void> {
  return request<void>(`/prefs/${encodeURIComponent(key)}`, { method: 'DELETE' });
}

/**
 * 候选建议，**只读不落库**。
 *
 * 用户点"设为默认"才会走 `setPref`；点了"不用"则什么都不发生——一次拒绝不需要
 * 留下任何记录。
 */
export function listPrefSuggestions(): Promise<PrefSuggestion[]> {
  return request<PrefSuggestion[]>('/prefs/suggestions');
}

// ---- 模型设置 ----

/**
 * 当前生效的模型配置与每一项的来源。
 *
 * **响应里没有明文密钥**，只有 `api_key_masked`（`…abcd`）。面板打开时调一次，
 * 之后每次保存都拿响应就地更新，不重拉——重拉会多一次"刚存的值还没刷新到界面"
 * 的窗口。
 */
export function getModelSettings(): Promise<ModelSettings> {
  return request<ModelSettings>('/settings/model');
}

/**
 * 保存。**改完立即生效，不用重启后端**：所有模型出口每次调用都重新解析配置。
 *
 * 空串的语义按字段不同（见 `SaveModelSettingsRequest`）：`api_key` 空串 = 不改，
 * `base_url` / `model` 空串 = 回落环境变量。
 */
export function saveModelSettings(
  body: SaveModelSettingsRequest,
): Promise<SaveModelSettingsResponse> {
  return request<SaveModelSettingsResponse>('/settings/model', {
    method: 'PUT',
    body: JSON.stringify(body),
  });
}

/**
 * 发一次最小 chat 请求（`max_tokens=1`）验证地址 / 密钥 / 模型名。
 *
 * `ok=false` 时**不是异常**：它是"这次连不上"的结论，HTTP 状态仍是 200，调用方
 * 把 `message` 直接显示在面板里。只有三项都没配才会抛 `ApiError`（400）。
 */
export function testModelSettings(
  body: TestModelSettingsRequest = {},
): Promise<TestModelSettingsResponse> {
  return request<TestModelSettingsResponse>('/settings/model/test', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ---- 账号与会话 ----
//
// 这一组**没有 token 参数，也没有 token 返回值**：会话是 HttpOnly cookie，浏览器
// 自己带、自己存。代价是每个请求都得带凭证（`request()` 里已经 `credentials:
// 'same-origin'`），收益是 XSS 偷不到它——JS 连读都读不到。
//
// 这一批只把调用面搭起来，界面还没有登录入口（见 `app/auth/__init__.py`：门禁按
// 接口逐个打开，先有身份层再上门）。

/**
 * 当前是谁。**永远 200**，未登录时 `kind` 是 `'guest'` 或 `'anonymous'`。
 *
 * 它会顺手发游客 cookie（全站唯一发它的地方），所以**界面启动时该调一次**——
 * 不调的话这台浏览器就没有编号，免费额度那一批会把它当成"没来过"。
 */
export function whoAmI(): Promise<WhoAmI> {
  return request<WhoAmI>('/auth/me');
}

/** 注册。成功即登录（201 + 会话 cookie），不需要再走一次登录。 */
export function register(body: Credentials): Promise<AuthUser> {
  return request<AuthUser>('/auth/register', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

/** 登录。账号或密码不对统一 401 `auth_bad_credentials`（不区分是哪一项错）。 */
export function login(body: Credentials): Promise<AuthUser> {
  return request<AuthUser>('/auth/login', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

/** 登出。幂等：本来就没登录也是 204。 */
export function logout(): Promise<void> {
  return request<void>('/auth/logout', { method: 'POST' });
}

/** 改密码。需要已登录 + 旧密码；改完**所有设备都要重新登录**（会话全撤销）。 */
export function changePassword(body: ChangePasswordRequest): Promise<void> {
  return request<void>('/auth/password', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ---- 知识库 ----

/**
 * 库里的材料列表。**不受 `MINDMAP_LIBRARY_SEARCH` 影响**：那个开关管的是
 * "生成时要不要从库里捞材料"，这里只是把库里有什么显示出来。
 */
export function listLibrary(limit = 50): Promise<LibraryList> {
  return request<LibraryList>(`/knowledge/library?limit=${limit}`);
}

/**
 * 单份材料的正文。
 *
 * 不传 `limit` 时后端回前 4000 字（预览口径，知识库面板用的就是它）。
 * 传 `limit` 可以要更多，上限由后端 schema 挡（超出是 422，不是静默夹断）——
 * "点引用跳原文"必须能拿到引用位置之前的正文，而 `SourceRef.start/end` 是
 * 全文码点偏移，只给前 4000 字的话引用落在第 20000 字处就什么都看不见。
 */
export function readLibrarySource(id: string, limit?: number): Promise<LibrarySource> {
  const query = limit == null ? '' : `?limit=${Math.floor(limit)}`;
  return request<LibrarySource>(`/knowledge/library/${encodeURIComponent(id)}${query}`);
}

/**
 * 删掉库里的一份材料。**被导图引用时会先被拦一次**（409 `source_cited`，`detail.cited_by`
 * 是被几张图引用）：那几张图不会坏，但引用这些材料的节点打开时是"来源原文不可用"，
 * 且正文没了就再也没法回填。确认之后带 `force=true` 再调一次才真删。
 *
 * 409 不是错误分支里最难写的那种——它带着可执行的下一步（"带 force 再来一次"），
 * 所以界面要能读到 `detail.cited_by` 并把代价写清楚，而不是显示一句"删除失败"。
 */
export function deleteLibrarySource(id: string, force = false): Promise<LibraryDeletion> {
  const query = force ? '?force=true' : '';
  return request<LibraryDeletion>(
    `/knowledge/library/${encodeURIComponent(id)}${query}`,
    { method: 'DELETE' },
  );
}

/**
 * 库里现存的实体别名（`alias → canonical`）。`configured=false` 是"图存储没开"，
 * 与"没有别名"必须分开显示：前者要去配 Neo4j，后者要去跑实体消解。
 */
export function listAliases(limit = 500): Promise<AliasListing> {
  return request<AliasListing>(`/knowledge/aliases?limit=${Math.floor(limit)}`);
}

/**
 * 删掉一条别名。`canonical` 只用于"同一别名指向两个主名"的情形（同名不同类型的
 * 节点），给上它只删这一对。
 *
 * 别名不参与证据：删掉之后这两个名字重新是两个实体，挂在它们身上的证据边一条不动。
 * 所以"删错别名"是可逆的语义修正，不需要备份——但**不要用重跑实体消解来改**，
 * 那会按同一批阈值把它重新猜回来。
 */
export function deleteAlias(alias: string, canonical?: string): Promise<AliasDeletion> {
  const query = canonical ? `?canonical=${encodeURIComponent(canonical)}` : '';
  return request<AliasDeletion>(
    `/knowledge/aliases/${encodeURIComponent(alias)}${query}`,
    { method: 'DELETE' },
  );
}

/** 清空全部别名（"这一批合并全错了"的退路，别拿它当常规修正手段）。 */
export function clearAliases(): Promise<AliasClearance> {
  return request<AliasClearance>('/knowledge/aliases', { method: 'DELETE' });
}

// ---- 导出 ----

/**
 * 四种导出都是纯文本响应，不能走 request()——它固定按 JSON 解析。
 *
 * `json` 是**唯一无损**的一种：另外三种都只有一个文字槽位（正文优先），
 * `note` / `detail` 的区分、颜色、图标、图型、附加边往返一次就退化了。
 * 它也是导入的保底——"导出改一改再导入"只有这条路能保证一条都不丢。
 */
export type ExportFormat = 'puml' | 'markdown' | 'opml' | 'json';

const EXPORT_META: Record<ExportFormat, { label: string; extension: string }> = {
  puml: { label: 'PlantUML', extension: 'puml' },
  markdown: { label: 'Markdown', extension: 'md' },
  opml: { label: 'OPML', extension: 'opml' },
  json: { label: 'JSON', extension: 'json' },
};

export function exportLabel(format: ExportFormat): string {
  return EXPORT_META[format].label;
}

/** 文件名用导图标题，但要去掉路径分隔符等非法字符——标题是模型生成的自由文本。 */
export function exportFilename(format: ExportFormat, title: string): string {
  const safe = title.replace(/[\\/:*?"<>|\r\n]/g, '_').trim() || '导图';
  return `${safe}.${EXPORT_META[format].extension}`;
}

export async function exportMindMap(
  format: ExportFormat,
  content: DiagramContent,
): Promise<string> {
  let response: Response;
  try {
    response = await fetch(`${BASE}/export/${format}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // 后端四种导出统一取 `note or label`（JSON 除外，它全字段照抄），这里直接发
      // 原样内容：前端不再自己做"把正文折进标题"的拼接，避免同一份内容两端拼出两种结果。
      body: JSON.stringify({ content }),
      credentials: 'include',
    });
  } catch {
    throw new ApiError(0, 'network_error', '无法连接后端，请确认服务已启动。');
  }

  if (!response.ok) throw await toApiError(response);
  return response.text();
}

// ---- 导入 ----

/**
 * 上传一个已有的思维导图文件，由后端识别格式并解析成一份 `DiagramContent`。
 *
 * 与 `uploadDocument` 同一个形状（multipart 不能走 `request()`：它固定带
 * `Content-Type: application/json`，而 multipart 的 boundary 必须由浏览器生成）。
 *
 * **后端只解析不落库**：拿到 `content` 之后由 store 装进画布，形态是"有未保存
 * 修改的新导图"，用户点保存才写库。这样导入失败不会污染已有数据。
 */
export async function importMindMap(
  file: File,
  signal?: AbortSignal,
): Promise<ImportResult> {
  const form = new FormData();
  // 字段名必须是 `file`：后端签名是 `file: UploadFile = File(...)`。
  form.append('file', file);

  let response: Response;
  try {
    response = await fetch(`${BASE}/mindmaps/import`, {
      method: 'POST', body: form, signal, credentials: 'include',
    });
  } catch {
    throw new ApiError(0, 'network_error', '无法连接后端，请确认服务已启动。');
  }

  if (!response.ok) throw await toApiError(response);

  const body = (await response.json().catch(() => null)) as ImportResult | null;
  if (!body?.content) {
    // 契约里失败一定走 400；拿到空壳说明后端出了问题，别把 undefined 塞进画布。
    // 这里判的是"有没有 content"，不是"有没有 content.root"：导入的产物可能是
    // 鱼骨图或流程图，那两个顶层结构上没有 `root`。
    throw new ApiError(response.status, 'empty_import', '后端没有返回解析结果，请重试。');
  }
  return body;
}
