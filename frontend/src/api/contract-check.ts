/**
 * 契约漂移的编译期防线。
 *
 * `./schema.d.ts` 由 `npm run gen:api` 从 `backend/openapi.json` 生成，那份快照又是
 * `backend/scripts/export_openapi.py` 从 `app.openapi()` 导出的。两条独立的检查守
 * 同一件事：
 *
 * - 本文件：镜像类型（`./types.ts`）与生成结果对得上，`vue-tsc` 期间就红；
 * - CI：重新生成 `schema.d.ts` 后 `git diff --exit-code`，快照过期也红。
 *
 * 断言分两类：
 *
 * 1. `SameKeys`——键集合必须一样。后端加了字段（比如 `Node.highlight`）而镜像没跟，
 *    或者镜像自造了后端不认识的字段，都会在这里编译失败。这一条是重点：历史上
 *    `source_refs` 与 `note` 都因为"镜像没声明"，在保存时被整批丢掉。
 * 2. `SameShape`——把可空性抹平之后，两边的字段类型要能互相赋值。`label` 从字符串
 *    改成对象、`children` 从数组改成 map，都会在这里炸；而"必填/可选"的差异不算，
 *    因为 pydantic 给 `default_factory` 字段（`children`、`failed_pages`）在 OpenAPI
 *    里标的是可选，实际响应里却总是有——镜像那边写成必填才是对的。
 *
 * 故意**不做**"镜像 → 后端 Input"的断言：后端的 Input 模式把带默认值的标量字段
 * （`collapsed`、`theme.preset`、`style.bold`）标成必填，可实际请求体不写这些字段
 * 也能通过校验（`extra="forbid"` 只拒绝多余字段，不要求缺省字段）。那条断言会一直
 * 红，红久了就没人看它了——一个常年红的检查等于没有检查。
 *
 * 本文件没有运行时副作用：`checks` 只被类型系统读取，构建时会被摇掉；它也不需要
 * 被任何模块 import，只要在 `tsconfig.json` 的 `include` 里就会被检查。
 *
 * 注意 `MindMapOp` / `OpBatch` 不在 OpenAPI 里（它们走 agent 的 SSE 事件，不是 HTTP
 * 请求体或响应体），那两个类型靠 `backend/tests/fixtures/mindmap_ops.json` 两端共用
 * 一份用例来对齐，见 `frontend/src/mindmap/ops.ts`。
 */
import type { components } from './schema';
import type {
  AliasClearance,
  AliasDeletion,
  AliasEntry,
  AliasListing,
  AuthUser,
  ChangePasswordRequest,
  Credentials,
  ImportResult,
  ImportWarning,
  LibraryDeletion,
  LibraryEntry,
  LibraryList,
  LibrarySource,
  MapRecord,
  MindEdge,
  MindMapContent,
  MindMapTheme,
  MindNode,
  MindRelation,
  NodePosition,
  NodeStyle,
  Pref,
  PrefSuggestion,
  ModelSettings,
  InstanceSettings,
  SaveModelSettingsRequest,
  SaveModelSettingsResponse,
  SavedPref,
  Source,
  SourceRef,
  SourceSpan,
  SourceUsage,
  TestModelSettingsRequest,
  TestModelSettingsResponse,
  WhoAmI,
} from './types';

type Schemas = components['schemas'];

/** 两个类型的键集合完全相同才通过。 */
type SameKeys<A, B> = [keyof A] extends [keyof B] ? ([keyof B] extends [keyof A] ? true : false) : false;

/** 把每一层字段都放宽成可选，只看"有哪些字段、字段是什么形状"。 */
type Deep<T> = T extends (infer Item)[]
  ? Deep<Item>[]
  : T extends object
    ? { [Key in keyof T]?: Deep<T[Key]> }
    : T;

/** 抹平可空性之后，两个形状能互相赋值才通过。 */
type SameShape<A, B> = Deep<A> extends Deep<B> ? (Deep<B> extends Deep<A> ? true : false) : false;

export const checks = {
  // ---- 键集合 ----
  contentKeys: true satisfies SameKeys<MindMapContent, Schemas['MindMapContent-Output']>,
  nodeKeys: true satisfies SameKeys<MindNode, Schemas['Node-Output']>,
  themeKeys: true satisfies SameKeys<MindMapTheme, Schemas['Theme']>,
  styleKeys: true satisfies SameKeys<NodeStyle, Schemas['NodeStyle']>,
  positionKeys: true satisfies SameKeys<NodePosition, Schemas['Position']>,
  relationKeys: true satisfies SameKeys<MindRelation, Schemas['Relation']>,
  edgeKeys: true satisfies SameKeys<MindEdge, Schemas['Edge']>,
  sourceKeys: true satisfies SameKeys<Source, Schemas['Source']>,
  sourceRefKeys: true satisfies SameKeys<SourceRef, Schemas['SourceRef']>,
  sourceSpanKeys: true satisfies SameKeys<SourceSpan, Schemas['SourceSpan']>,
  sourceUsageKeys: true satisfies SameKeys<SourceUsage, Schemas['SourceUsage']>,
  recordKeys: true satisfies SameKeys<MapRecord, Schemas['MapRecord']>,
  // 导入：`warnings` 里只有 code/message，多一个字段就要在这里红——那条契约是
  // "定位信息写进 message"，加字段等于换一种约定。
  importKeys: true satisfies SameKeys<ImportResult, Schemas['ImportResponse']>,
  importWarningKeys: true satisfies SameKeys<ImportWarning, Schemas['ImportWarningPayload']>,
  // 偏好：键集合必须与后端一致。`last_applied_at` 尤其不能漏——它是"这条偏好
  // 到底有没有在工作"的唯一可见证据，漏掉它的镜像会让面板永远显示"尚未生效"。
  prefKeys: true satisfies SameKeys<Pref, Schemas['Pref']>,
  savedPrefKeys: true satisfies SameKeys<SavedPref, Schemas['Saved']>,
  prefSuggestionKeys: true satisfies
    SameKeys<PrefSuggestion, Schemas['app__prefs__service__Suggestion']>,
  libraryEntryKeys: true satisfies SameKeys<LibraryEntry, Schemas['LibraryEntry']>,
  libraryListKeys: true satisfies SameKeys<LibraryList, Schemas['LibraryList']>,
  librarySourceKeys: true satisfies SameKeys<LibrarySource, Schemas['LibrarySource']>,
  // 删除的回执：`cited_by` 不能漏。它是"删了之后有几张图受影响"的唯一来源，
  // 漏掉它界面就只能说一句"删掉了"，而用户下次打开那张图才会发现证据没了。
  libraryDeletionKeys: true satisfies
    SameKeys<LibraryDeletion, Schemas['LibraryDeletion']>,
  aliasEntryKeys: true satisfies SameKeys<AliasEntry, Schemas['AliasEntry']>,
  aliasListingKeys: true satisfies SameKeys<AliasListing, Schemas['AliasListing']>,
  aliasDeletionKeys: true satisfies SameKeys<AliasDeletion, Schemas['AliasDeletion']>,
  aliasClearanceKeys: true satisfies SameKeys<AliasClearance, Schemas['AliasClearance']>,
  // 模型设置：这五条是密钥保护契约的编译期一半。`ModelSettings` 里若出现任何
  // 明文字段（`api_key` 而不是 `api_key_masked`），这里会红——比 code review 可靠，
  // 因为加字段的人往往只是想"顺手把值也带出来"。
  modelSettingsKeys: true satisfies SameKeys<ModelSettings, Schemas['ModelSettings']>,
  // 共享配置那一行：同样不许带明文密钥——它是"别人也在用"的那份配置，泄露面比
  // 自己的更大。
  instanceSettingsKeys: true satisfies
    SameKeys<InstanceSettings, Schemas['InstanceSettings']>,
  saveModelSettingsRequestKeys: true satisfies
    SameKeys<SaveModelSettingsRequest, Schemas['SaveModelSettingsRequest']>,
  saveModelSettingsResponseKeys: true satisfies
    SameKeys<SaveModelSettingsResponse, Schemas['SaveModelSettingsResponse']>,
  testModelSettingsRequestKeys: true satisfies
    SameKeys<TestModelSettingsRequest, Schemas['TestModelSettingsRequest']>,
  testModelSettingsResponseKeys: true satisfies
    SameKeys<TestModelSettingsResponse, Schemas['TestModelSettingsResponse']>,

  // 账号：这三条守的是"`AuthUser` 里不许出现口令相关字段"。后端 `UserView` 刻意
  // 不含 `password_hash`，镜像若为了显示方便顺手加一个（或加个 `token`，因为
  // "cookie 看不见所以想自己留一份"），这里会红。会话走 HttpOnly cookie 就是为了
  // 让 JS 拿不到它，多一个 token 字段等于把那道防线绕开。
  authUserKeys: true satisfies SameKeys<AuthUser, Schemas['UserView']>,
  whoAmIKeys: true satisfies SameKeys<WhoAmI, Schemas['WhoAmI']>,
  credentialsKeys: true satisfies SameKeys<Credentials, Schemas['Credentials']>,
  changePasswordKeys: true satisfies
    SameKeys<ChangePasswordRequest, Schemas['ChangePasswordRequest']>,

  // ---- 字段形状 ----
  contentShape: true satisfies SameShape<MindMapContent, Schemas['MindMapContent-Output']>,
  nodeShape: true satisfies SameShape<MindNode, Schemas['Node-Output']>,
  themeShape: true satisfies SameShape<MindMapTheme, Schemas['Theme']>,
  styleShape: true satisfies SameShape<NodeStyle, Schemas['NodeStyle']>,
  positionShape: true satisfies SameShape<NodePosition, Schemas['Position']>,
  relationShape: true satisfies SameShape<MindRelation, Schemas['Relation']>,
  edgeShape: true satisfies SameShape<MindEdge, Schemas['Edge']>,
  sourceShape: true satisfies SameShape<Source, Schemas['Source']>,
  sourceRefShape: true satisfies SameShape<SourceRef, Schemas['SourceRef']>,
  sourceSpanShape: true satisfies SameShape<SourceSpan, Schemas['SourceSpan']>,
  sourceUsageShape: true satisfies SameShape<SourceUsage, Schemas['SourceUsage']>,
  recordShape: true satisfies SameShape<MapRecord, Schemas['MapRecord']>,
  importShape: true satisfies SameShape<ImportResult, Schemas['ImportResponse']>,
  importWarningShape: true satisfies
    SameShape<ImportWarning, Schemas['ImportWarningPayload']>,
  prefShape: true satisfies SameShape<Pref, Schemas['Pref']>,
  savedPrefShape: true satisfies SameShape<SavedPref, Schemas['Saved']>,
  // `source` 那条是靠契约对齐的：后端把它写成 `Literal` 而不是 `str`，所以 OpenAPI
  // 里是枚举、镜像里是同两个字面量。写成 `str` 的话这条形状断言会红——那是对的，
  // 因为"界面上能显示哪两种来源"这件事本来就不该只在镜像里知道。
  prefSuggestionShape: true satisfies
    SameShape<PrefSuggestion, Schemas['app__prefs__service__Suggestion']>,
  libraryEntryShape: true satisfies SameShape<LibraryEntry, Schemas['LibraryEntry']>,
  libraryListShape: true satisfies SameShape<LibraryList, Schemas['LibraryList']>,
  librarySourceShape: true satisfies SameShape<LibrarySource, Schemas['LibrarySource']>,
  libraryDeletionShape: true satisfies
    SameShape<LibraryDeletion, Schemas['LibraryDeletion']>,
  aliasEntryShape: true satisfies SameShape<AliasEntry, Schemas['AliasEntry']>,
  aliasListingShape: true satisfies SameShape<AliasListing, Schemas['AliasListing']>,
  aliasDeletionShape: true satisfies SameShape<AliasDeletion, Schemas['AliasDeletion']>,
  aliasClearanceShape: true satisfies SameShape<AliasClearance, Schemas['AliasClearance']>,
  modelSettingsShape: true satisfies SameShape<ModelSettings, Schemas['ModelSettings']>,
  instanceSettingsShape: true satisfies
    SameShape<InstanceSettings, Schemas['InstanceSettings']>,
  saveModelSettingsResponseShape: true satisfies
    SameShape<SaveModelSettingsResponse, Schemas['SaveModelSettingsResponse']>,
  testModelSettingsResponseShape: true satisfies
    SameShape<TestModelSettingsResponse, Schemas['TestModelSettingsResponse']>,
  authUserShape: true satisfies SameShape<AuthUser, Schemas['UserView']>,
  whoAmIShape: true satisfies SameShape<WhoAmI, Schemas['WhoAmI']>,
};
