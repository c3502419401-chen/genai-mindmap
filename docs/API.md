# API 与数据契约

前后端以 `/api` 为前缀。FastAPI 启动后的 `/docs` 展示当前路由；本页说明每条路由是否真正实现。当前无身份认证，仅限本机开发。

**错误体已统一**：所有错误响应都是 `{code, message, detail, request_id}`（实现见 `backend/app/core/errors.py`）。`message` 是中文可读文案，`detail` 保留原始细节（校验错误为数组），`request_id` 优先沿用请求头 `X-Request-ID`、没有则生成，并同时通过响应头回传。前端 `src/api/client.ts` 的 `ApiError` 读 `message`，缺失时回退到 `detail`。

**前端消费情况**（截至 2026-11-27）：`POST /api/agent/runs/stream`（`runAgentStream`，生成与编辑的**唯一**入口）、`POST /api/ingestion/files`（`uploadDocument`）、`POST /api/export/{puml,markdown,opml,json}`、`POST /api/mindmaps/import`、四个 `/api/mindmaps*` 端点（另有两条改动日志读路径 `GET .../edits/summary` 与 `GET .../edits`，界面暂无入口，供统计与排查）、`GET /api/prefs` 系列（顶栏「我的偏好」）、`GET /api/knowledge/library` 与 `GET /api/knowledge/library/{source_id}`（`listLibrary` / `readLibrarySource`）、**`DELETE /api/knowledge/library/{source_id}`（`deleteLibrarySource`，材料列表的删除按钮）与 `GET/DELETE /api/knowledge/aliases*`（`listAliases` / `deleteAlias` / `clearAliases`，抽屉里「实体别名」折叠区）——都在顶栏「知识库」抽屉里**。画布与编辑全部在 `src/canvas/`、`src/mindmap/{edits,ops}.ts` 里完成，只把整棵树发给后端。节点引用（`node.source_refs`）已能展示：角标显示条数，详情面板列来源与位置，抽屉里看原文并高亮命中句，实现见 `src/mindmap/refs.ts`。

**尚未封装**：`POST /api/ingestion/text`、`GET /api/ingestion/formats`、`POST /api/optimization/check`、`POST /api/optimization/suggest`、`POST /api/knowledge/search`、`POST /api/agent/tasks`。其中前两个是小事，中间两个是"结构诊断还没进界面"，后两个仍是桩。

## 导图格式 v2（当前；v1 请求体仍然接受）

```json
{
  "schema_version": 2,
  "title": "人工智能",
  "layout": "tree",
  "theme": {"preset": "default", "font": "system", "direction": "right"},
  "root": {
    "id": "root",
    "label": "人工智能",
    "note": "可选的正文，画布上只显示短标题",
    "children": [
      {"id": "node-1", "label": "机器学习", "children": []}
    ]
  },
  "source_usage": [
    {"source_id": "source-1", "kind": "text", "filename": null, "char_count": 42}
  ]
}
```

每个节点需要非空 `id`、非空 `label`；省略 `children` 时默认为空。节点还可以带可选的 `source_refs`（**属于 v1，不是 v2 预告**）：

```json
"source_refs": [{"source_id": "source-1", "quote": "原文里的逐字片段", "start": 12, "end": 25, "page": 3}]
```

`quote` 必须能在本次请求提供的原文里逐字匹配，`source_id` 必须出现在 `source_usage` 里；不合规的引用在生成/保存时被剔除，原因通过 `diagnostics`（带 `node_id`）返回，节点本身保留。规则见 [节点来源引用](SOURCE-REFERENCES.md)。

整棵树 ID 唯一，最多 1000 节点、8 层、每个节点最多 50 个子节点，每节点最多 20 条引用。标题和节点文字长度不超过 200。

**v2 已落地**（2026-11-10，`backend/app/contracts/mindmap.py`）：在 v1 之上新增 `schema_version`、`layout`（`tree` / `vertical` / `logic` / `timeline` / `fishbone` / `manual`）、`theme`（预设、强调色、字体、`direction`）、`relations[]`，以及节点的 `note` / `collapsed` / `style` / `pos`。字段定义与校验规则见 [CONTRACT_V2](CONTRACT_V2.md)。

兼容策略是"**读时补默认值**"：没有 `schema_version` 的 v1 请求体与库里的 v1 记录都直接可用（缺的字段填默认值），保存后写成 v2；不需要跑迁移脚本，也不需要停机。反过来不成立——把 v2 字段发给旧版后端仍会 422（`extra="forbid"`），所以升级顺序必须是后端先上、前端后上。

前端从 2026-11-10 起按 v2 发送（`frontend/src/api/types.ts` 已补字段），保存 / 打开 / 导出都带 `note` 与 `layout` / `theme`；`frontend/src/api/client.ts` 里原有的两个裁剪函数 `toContractV1()` / `toExportV1()` 已删除。三种导出取节点正文（`note`），没有正文时回落标题。

`source_usage` 是可选的**整张导图来源摘要**：agent 的 `mindmap_generate_deep` 在合并候选树时按实际输入重建（`backend/app/agent/tools.py::_generate_deep`），保存导图时一并保存；旧数据省略时默认为空列表。它只记录来源 ID、类型、文件名和使用的字符数，不保存原文，也没有节点到来源片段的对应关系，不能作为节点级引用或事实验证。没有来源的生成该列表为空；后续完整追溯还需要来源存储、分段定位与节点关联。

## 已实现

| 方法和路径 | 请求 | 成功响应 | 失败情况 |
|---|---|---|---|
| `GET /api/health` | 无 | `{"status":"ok","version":"..."}` | — |
| `POST /api/ingestion/text` | `{"text":"内容","kind":"text 或 markdown"}` | `{"source": 来源}` | 空文本或其他来源类型 422 |
| `POST /api/optimization/check` | `{"content":导图}` | 结构诊断列表 | 结构无效 422 |
| `POST /api/optimization/suggest` | `{"content":导图}` | 诊断与候选建议 | 结构无效 422；建议需用户判断 |
| `POST /api/export/puml` | `{"content":导图}` | PlantUML 文本；根节点来自 `root.label` | 结构无效 422 |
| `POST /api/export/markdown` | `{"content":导图}` | Markdown 大纲：标题 + 缩进列表 | 结构无效 422 |
| `POST /api/export/opml` | `{"content":导图}` | OPML 2.0，可导入 XMind | 结构无效 422 |
| `POST /api/ingestion/files` | multipart，字段名 `file` | `{"source": 来源}`，`source.text` 是解析出的正文 | 扩展名不在白名单 422；文档/图片超 10MB 或音频超 20MB 413；扫描件无文本层、音频无人声、图片视觉不可用、缺语音凭证 422 |
| `GET /api/ingestion/formats` | 无 | `{"document":[…],"audio":[".wav"],"image":[".png",…],"maxUploadBytes":…,"maxAudioBytes":…,"maxImageBytes":…}` | — |
| `GET /api/mindmaps` | 无 | 导图记录数组 | — |
| `POST /api/mindmaps` | `{"content":导图,"events":[改动事件…],"truncated":0}`（后两个可省略） | 201，导图记录 | 结构无效 422；事件形状不对（字段名漂移、未知 kind/field、时间戳以外的多余键）422 |
| `GET /api/mindmaps/{map_id}` | 无 | 导图记录 | 不存在 404 |
| `PUT /api/mindmaps/{map_id}` | `{"version":1,"content":导图,"events":[…]}` | 更新后的记录，版本加 1 | 不存在 404；版本过期 409（此时**事件一条都不写**）；结构无效 422 |
| `GET /api/mindmaps/{map_id}/edits/summary` | 无 | 改动分布：`total` / `last_event_at` / `by_actor` / `by_kind` / `by_field` / `label_shrunk` / `removed_depth` / `removed_subtree`（见下） | 不存在 404（不返回全零报告——"这张图没人改过"是另一件事） |
| `GET /api/mindmaps/edits/health` | 无 | 跨图死表探测器：`window_start` / `window_end` / `edited_maps` / `saved_maps` / `chain_suspect` / `detail` | — |
| `GET /api/mindmaps/{map_id}/edits` | 查询参数 `limit`（1–500，默认 50） | 改动明细，新的在前：`id` / `map_id` / `run_id` / `actor` / `kind` / `node_id` / `field` / `before` / `after` / `depth` / `subtree_size` / `change_count` / `at` / `recorded_at` | 不存在 404 |

**改动日志（"用户最常改什么"）只记录、不总结。** 表是 `edit_events`（在 `repository.connect()` 里与其余表同一时刻建好）。写入口只有一个：`POST`/`PUT /api/mindmaps` 的 `events` 字段——**并进保存，不另开端点**，因为第一次保存前没有 `map_id`，一个 `POST /mindmaps/{id}/edits` 要么发不出去，要么必须 create 之后补发，那就会多出"内容存了、事件没存"的半截状态，而统计从此只看见部分改动。所以：内容与事件同一次请求、同一次成败；409 冲突时两者一起拒。

一条事件的形状（`before` / `after` 是**摘要**，`detail` 只存 `"1800字"` 这种长度）：

```json
{"actor":"user","kind":"update","field":"label","node_id":"d1a",
 "before":"缺失值、异常值与重复样本的清理顺序","after":"缺失值清洗","depth":3,
 "subtree_size":null,"change_count":null,
 "run_id":null,"at":"2026-11-17T09:30:01.250Z"}
```

`kind` ∈ `add` / `remove` / `update` / `move` / `edge` / `style` / `generate`（历史行里还有 `accept`）；`field` **只在 `update` / `style` 上有值**（`label` / `title` / `detail` / `note` / `color` / `icon`）。这条规则让统计能自校验：`sum(by_field) == by_kind.update + by_kind.style`，`sum(by_actor) == total`。对不上就说明有写入路径绕过了契约。

`generate` 是**一次"生成落到画布"**（`create` / `expand` / `edit` 三条路都算），不是改动：`node_id` / `field` / `depth` 全空，只有 `node_count`（这次落进画布的那棵树有几个节点，**在生成完成那一刻定死**）与 `run_id`，`actor` 固定 `agent`。它**每次都记**，哪怕一处都没改——没有它，"生成完一字不改直接保存"在表里是 0 行，与"用户从没用过"不可区分，缺的不是样本而是分母。它的前身是 `accept`（候选层还在时"用户点了应用"，`actor='user'` + `change_count`）；候选层删掉之后新数据一律写 `generate`，老行原样保留、`by_kind` 照样读得到。

`depth` 是节点层数（**根是 1**），`remove` 事件填删除前所在层。`subtree_size` 是这条 `remove` **一共删掉几个节点**（含自己，数的是真的消失的那些）：删一个叶节点是 1，删一整支 12 个节点是**一条**事件、`subtree_size=12`。加列之前的老数据这一位是 `null`（当时没记，不做回填）。`at` 由前端给（事件发生的时刻，非法时退回入库时刻），`recorded_at` 是服务端写入时刻——分开才诊断得出时钟漂移。单次保存最多 500 条，超出的部分留在前端队列里**等下一次**（分批，不是丢弃），只把条数上报成一条服务端 warning。

`label_shrunk` 是 `field='label'`、**`actor='user'`** 且 `length(after) < length(before)` 的条数与两个平均值（码点口径，`count=0` 时平均值为 `null` 而不是 0——0 会被读成"平均改成 0 字"）。过滤 actor 是因为 `agent` 那批是"用户点了应用、模型又改了一遍"，性质不同：实测 B 图只有 3 条 user 的缩短，不过滤会读成 4 条。

`removed_depth` 是删掉的节点按层分布，**只含零散删除**（`subtree_size = 1`）；`subtree_size` 为空的老数据**不进**这个分布，否则"每层都在被删"那次污染还在。`removed_subtree` 是删除规模直方图：`{"size_1": 5, "size_12": 1, "size_unknown": 2}`，`size_unknown` 就是那些老数据。

这几个数直接对应生成侧该改什么：标题普遍被缩短 → 16 字上限可能还不够短；零散删掉的集中在第 4 层 → 初次深度太深；成规模删掉的是整支 → 那一支根本不该生成。**它们只说发生了什么，不说为什么**（见 `docs/EDIT-LOG.md` 的局限一节）。

`last_event_at` 是这张图最近一条事件的**入库**时刻（`recorded_at`，不是前端给的 `at`；空表是 `null` 而不是空串）。`GET /mindmaps/edits/health` 是跨图的死表探测器：**"保存了但没有任何事件"在正常情况下不该发生**（每次生成落地都记一条 `generate`，连一处没改也记），所以"昨日保存 > 0 且昨日编辑 = 0"判定 `chain_suspect`——前端在发、后端在丢的那种静默故障，40 分钟里没有任何告警，就靠这一条捞。
| `POST /api/agent/runs/stream` | `{"run_id":"…","goal":"…","intent":"create / expand / edit","thinking_model":"default / first-principles / feynman / systematic / critical","snapshot":导图或省略,"selected_ids":[…],"sources":[…],"prefs_applied":["diagram_type.default=fishbone"],"deep_thinking":true / 省略,"budget":{…}}` | SSE v2：每帧 `data: <json>`，信封 `{seq, type, ...}`（`backend/app/agent/contracts.py::_EventBase` 只有 `seq`），`type` ∈ `stage` / `sources` / `tool_start` / `tool_end` / `ops` / `snapshot` / `node_grow` / `heartbeat` / `done` / `error`；**有材料时**响应体最前面还有一行 **SSE 注释帧** `: 正在把材料收进知识库`（入库在首帧之前，先用它把首字节送出去；不是数据帧、不占 `seq`；没有材料时整段跳过，所以不带材料的请求帧与加这段之前逐字节相同）。`done` 带 `mode`（`demo` / `model`，序列化用 `exclude_none=True`），`content` 是这次生成的整棵树（前端拿到就直接落画布并标成未保存，没有"候选待确认"这一步）；`done` 还可能带 `text`（收尾原话，**只在没产出导图时**填，截断 4000 字）、`reasoning`（思考过程，**成功时也填**，上限 20000 字）、`model`（这一轮真正发出去的模型名，缺字段＝没说）；模型思考期间有 `heartbeat` 帧（`{seq, type:"heartbeat", waited_ms}`，每 10 秒一帧，只保活）；`sources` 帧**只在材料真的进了知识库时出现**（旧 id → 库里的持久 id） | 目标为空 422；来源合计超出模型上下文 422；同一个 `run_id` 正在跑 409（`run_in_progress`）；**公共免费额度用完 429（`free_quota_exhausted`，带 `Retry-After` = 到明天零点的秒数；此时不开流、不建 `runs` 行、不发任何模型调用）**；预算用尽或模型失败以 `error` 事件下发 |
| `GET /api/agent/runs` | 查询参数 `status`（`done` / `cancelled` / `failed` / `running`，可省略）、`unapplied`（默认 `true`）、`limit`（1–100，默认 20） | 运行台账数组（`run_id` / `status` / `intent` / `goal` / 时间戳），**不含结果正文**。`status` 省略时取 `done` + `cancelled` 中"有结果且没被保存过"的 | — |
| `GET /api/agent/runs/{run_id}` | 无 | 同上再加 `candidate`（整棵树；字段名沿用，语义是"这次生成产出的树"）、`failure`、`mode` | 不存在 404 |
| `POST /api/agent/runs/{run_id}/applied` | 无 | 204；幂等（只打第一个时间戳）。语义是**这份结果被保存进库了**（前端在保存成功后调用），它决定下次进页面还要不要提醒 | 不存在 404 |
| `GET /api/prefs` | 无 | 全部偏好：`key` / `value` / `scope` / `source` / 两个时间戳，以及 `last_applied_at`（来自 `runs.request.prefs_applied`，为空 = 从未生效） | — |
| `GET /api/prefs/{pref_key}` | 无 | 单条偏好，形状同上 | 不存在 404（`pref_not_found`）。**这条端点原先缺失**，于是 `GET /api/prefs/style` 这类请求会命中 `/{pref_key}` 却被判 405「方法不允许」——字面意思是"资源存在但方法不行"，与实情（根本没这条偏好）正好相反 |
| `PUT /api/prefs/{pref_key}` | `{"value":"…","source":"user_explicit / suggestion_accepted","source_run_id":null}` | `{"pref":…,"changed":true/false}`；同值重复提交 `changed=false` 且不刷新 `updated_at` | 键非法 / 枚举外 / 备注超 8 条或 500 字 400（`pref_key_invalid` / `pref_value_invalid` / `pref_limit_exceeded`） |
| `DELETE /api/prefs/{pref_key}` | 无 | 204，硬删（没有软删） | 不存在 404（与"删了但还在"必须分得开） |
| `GET /api/prefs/suggestions` | 无 | 候选建议数组（`key` / `value` / `label` / `count` / `message`）。**只读不落库**：最近 3 次有效选择都是同一个值、且与当前偏好不同才提 | — |
| `GET /api/knowledge/library` | 可选 `limit`（默认 50，范围 1–200；越界 422） | `{"items":[…],"total":N}`；每条含 `source_id` / `filename` / `kind` / `chars` / `chunks` / `created_at` / `last_used_at` / `cited_by`（被几张**已保存导图**引用，`source_usage` 与节点 `source_refs` 都算，同图去重；该计数带 30 秒缓存，缓存键是导图表指纹，**存完/改完导图再列库会立刻反映**） | 库读不出来时返回空列表；导图表里有读不出的行时该行跳过、`cited_by` 按 0 计，都不返回 500 |
| `GET /api/knowledge/library/{source_id}` | 无 | `{"source_id","filename","kind","text","truncated","total_chars"}`，正文最多前 4000 字 | 库里没有这份 404（`code=not_found`）；库本身读不出来 503（`code=library_unavailable`，稍后重试） |
| `DELETE /api/knowledge/library/{source_id}` | 可选 `force`（默认 false） | `{"source_id","deleted","cited_by"}`；`cited_by` 是**删除前**引用它的已保存导图数。删掉之后那些图**不受影响**（`source_refs` 是值对象、无外键），只是引用查不到原文——`SOURCE_TEXT_UNAVAILABLE` | 不存在 404（`not_found`）；**还被导图引用且没给 `force` 时 409（`source_cited`，`detail.cited_by` 是被几张图引用）**——正文是证据的最终依据，删掉不可逆，所以后果要先确认；库读不出来 503 |
| `GET /api/knowledge/aliases` | 可选 `limit`（默认 500，1–2000） | `{"items":[{alias, canonical, source, similarity, created_at}],"total","configured"}`；`source` 是落库时的判定依据（`substring` 构词关系 / `embedding` 余弦兜底）。`configured=false` 表示图存储没开（缺 `MINDMAP_NEO4J_PASSWORD`）——**必须与"没有别名"分开**：一个是去配 Neo4j，一个是去跑实体消解 | 图存储没开时 200 + 空列表（知识库面板不该因此打不开） |
| `DELETE /api/knowledge/aliases/{alias}` | 可选 `canonical`（同一别名指向两个主名时只删这一对） | `{"alias","deleted"}`；别名不参与证据，删掉只让这两个名字重新是两个实体，`REL` 一条不动 | 没有这条别名 404（`not_found`）；图存储没开或不可用 503（`graph_unavailable`——**不是 404**，那会被读成"这条别名已经不在"） |
| `DELETE /api/knowledge/aliases` | 无 | `{"deleted"}`，清空全部别名边 | 图存储没开或不可用 503（`graph_unavailable`） |
| `GET /api/settings/model` | 无 | `ModelSettings` 14 字段：`base_url` / `base_url_source` / `api_key_masked` / `api_key_source` / `api_key_set` / `model` / `model_source` / `deep_model` / `deep_model_source` / `deep_model_effective` / `llm_mode` / `configured` / `updated_at` / `can_edit_instance` / `instance`。`*_source` 是逐字段的来源（`saved` / `instance` / `public` / `free` / `env` / `unbound` / `unset`），**密钥只回打码值**（≤8 字符只给 `…`，否则 `…` + 末 4 位）。`api_key_source: "public"` = 用的是部署方在 `MINDMAP_FREE_TIER_API_KEY` 里配的**公共免费密钥**（唯一一档有次数上限的来源，每 IP 每天 3 次，见 `docs/FREE-MODELS.md` 第 8 节）；它能生效的前提是**地址也来自部署方**（`base_url_source` 是 `instance` 或 `env`）——调用方自己写的地址永远领不到这把钥匙。`model_source: "free"` = 这次生成走的是部署方那台端点当前标 `:free` 的免费模型（名单运行时现问，见 `docs/FREE-MODELS.md`）；免费模型也**要密钥**，只是那次调用不收费，所以此时 `api_key_source` 仍是 `instance` / `env` / `public`。`deep_model_source: "free"` 同理（免费档不换深度模型——换了就是用部署方的钱）。`api_key_source: "unbound"` = 库里那把密钥仍在、但它是为**另一个主机**保存的，所以现在不交出（此时 `api_key_masked` 是空串、`api_key_set` 是 false、`configured` 也是 false）。`api_key_source: "instance"` = 你自己那行没有、借用了本站共享配置。`can_edit_instance` 只有**部署方**（这台机器上第一个注册的账号）为真；`instance` 也只有他拿得到，内容是共享那一行**存了什么**（不是回落到环境变量后的生效值），非部署方为 `null` | — |
| `PUT /api/settings/model` | `{"base_url?","api_key?","model?","deep_model?","clear_api_key":false,"scope?":"user"\|"instance"}` | `ModelSettings` + `changed: bool` + `notice: str \| null`。`api_key` 传空或省略 = **不改**；要清必须 `clear_api_key: true`；`base_url`/`model`/`deep_model` 的空串 = 清掉覆盖、回落环境变量。`scope: "instance"` 写的是**本站共享配置**那一行（游客免费额度用的就是它），只有部署方能写。`notice` 只在**地址真的变了**（规范化之后）且原来有地址时非空，是一句"上次测试的结论对新地址不成立"——只提醒不阻拦，因为"先填地址、等供应商开白名单"是合法用法。**改地址会让已存密钥变成 `unbound`**（绑定只认主机名，换路径/端口不算），要恢复得重新粘贴一次密钥 | `settings_incomplete` 400（缺必填）/ `settings_base_url_invalid` 400（不是 http(s) 或不是 OpenAI 兼容）/ `settings_clear_conflict` 400（同时给 `api_key` 与 `clear_api_key`）/ `settings_api_key_masked` 400（`api_key` 传的是响应里那个打码值 `…abcd`，不是密钥本身）/ `settings_forbidden` 403（非部署方提交 `scope:"instance"`） |
| `POST /api/settings/model/test` | `{"base_url?","api_key?","model?"}`（空字段回落当前生效值） | 200 一律返回 `{"ok","message","model","status","latency_ms"}`——发一次 `max_tokens=1` 的最小请求；**失败也是结论不是 5xx**，上游错误体里的密钥会被替换成 `…` 再截 200 字 | `settings_incomplete` 400（三项不齐）/ `settings_key_unbound` 400（地址换过了、库里那把密钥属于旧地址，要重贴；**不发请求**）/ `settings_api_key_masked` 400（`api_key` 传的是打码值，不发请求就拒）/ `settings_base_url_refused` 400（地址指向本机、内网、云元数据或保留地址，**一个字节都不发**；本机/内网要连得设 `MINDMAP_SETTINGS_ALLOW_PRIVATE=1`，**云元数据地址这个开关也打不开**） |
| `GET /api/auth/me` | 无 | `{"user":{…}\|null,"guest_id":str\|null,"kind":"user"\|"guest"\|"anonymous"}`，**永远 200** | — |
| `POST /api/auth/register` | `{"username","password"}` | 201 + `{"id","username","created_at"}`，**直接登录**（同时下发会话 cookie） | 账号名不合规 `auth_username_invalid` 400；密码不足 8 位 `auth_password_too_short` 400；超 200 位 `auth_password_too_long` 400；重名 `auth_username_taken` 409（**不区分大小写**） |
| `POST /api/auth/login` | `{"username","password"}` | `{"id","username","created_at"}` + 会话 cookie | `auth_bad_credentials` 401——**不区分"没这个人"与"密码错"**，那是账号枚举信道；`auth_rate_limited` 429 + `Retry-After: <秒>`——同 IP 或同账号在 15 分钟窗口内已失败 20 次 |
| `POST /api/auth/logout` | 无 | 204，**幂等**（本来就没登录也是 204）；撤销这一次会话并清 cookie | — |
| `POST /api/auth/password` | `{"current_password","new_password"}` | 204，清 cookie。需要**已登录 + 旧密码**；改完撤销这个人的**全部**会话 | 未登录 `auth_required` 401；旧密码不对 `auth_bad_credentials` 401；新密码长度不合规同上 |

**门禁：`/api/` 下的请求必须带一个身份，匿名的直接 401。** 判据是"有身份"而不是"登录过"——带会话 cookie（`mm_session`）或者游客 cookie（`mm_guest`）都算，所以**新访客打开页面就能用**，每天还有 3 次免费生成。白名单只有两处：`GET /api/health`（部署的健康检查与容器健康探针）与 `/api/auth/*`（否则没人能注册或登录）；`OPTIONS` 与所有非 `/api/` 路径（前端静态资源）也放行。实现在 `backend/app/auth/gate.py`，一张显式白名单而不是给 32 条业务路径各挂一个依赖——**漏一条就是一条匿名口子，而"漏了一条"在评审里极难看出来**。

401 的响应体和其他错误同一个形状，`code` 是 `auth_required`，`message` 会说"刷新页面即可（会自动分配一个游客身份，每天有 3 次免费生成），或者注册账号"，`detail.path` 带你请求的那个路径，并且**带 `X-Request-ID`**（沿用请求头里的那个，没有就新生成一个 8 位）。前端拿不到身份时不要假装成功：这一档在界面上的处理是"刷新页面"，不是"重试"——原样重发还是 401。

**数据隔离：读共享，写限主。** 导图的 `owner` 是创建时的身份（`user:<id>` 或 `guest:<cookie id>`）：任何人都能读别人保存的图（团队协作要能看），但 `PUT` 只有创建者能改，别人拿到 403 `map_forbidden`（**不是 404**——他看得见这张图，假装不存在只会让他以为自己的图丢了）；正文会告诉他用「另存为」新建一份。门禁打开之前存下的老行 `owner` 是空串，语义是"无主"，**只有部署方（第一个注册的账号）能改**，其余人 403。游客创建的图归他那台浏览器，清了 cookie 就找不回来——这是"游客"这个词本身的含义，不是缺陷。

`runs` 的可见性口径**刻意与导图不同**：只本人可见。导图是作品、读共享；`runs` 是过程记录（还没保存的树、模型的思考、失败原因），属于发起者。别人的 `run_id` 读 `GET /runs/{id}` 返 404（不是 403：`run_id` 是随机串，"存在但不属于你"本身就是不该给出的信息），`pending` 永远按 owner 过滤，`POST /runs/{id}/applied` 也只有本人能打——否则知道别人 `run_id` 的人能替他标掉这一位，那条记录再也不会出现在他的「上次的结果」里，一条安静的数据丢失。

**流入口这条路也是按 owner 的**：`POST /agent/runs/stream` 带别人的 `run_id` 时，既不会重放他的树、也不会把他的行重开成自己的，统一 404 `run_not_found`。`runs.begin()` 的重开 `UPDATE` 带着 `AND owner = ?`——没有这个条件，任何一个知道别人 `run_id` 的人都能把那个 `failed` 行"接着跑"成自己的，`UPDATE ... SET owner = ?` 会把行改归他，原主人之后连 `GET /runs/{id}` 都是 404。审查时实测过这条（`tests/test_isolation.py` 里两条 stream 用例钉住它）。

**五张表各不相同的口径，一张表看完**：

| 数据 | 谁读 | 谁写 | 说明 |
| --- | --- | --- | --- |
| `mindmaps`（保存的图） | 所有人 | 只有创建者 | 读共享写限主；别人 `PUT` 403 `map_forbidden` |
| `runs`（未保存的树 / 运行记录） | 只有本人 | 只有本人 | 别人 404（含 stream 入口，不只是 detail） |
| `knowledge`（材料库） | 所有人 | 所有人 | **刻意共享**：单用户单库是部署假设，多账号 = 共用一个库（`docs/DEPLOY.md` 有原文） |
| `memory_items`（推断出的记忆） | 只有本人 | 只有本人 | 存的是"关于这个人的推断"，跨账号读等于共享人格画像 |
| `user_prefs` / `style_profile`（偏好与风格画像） | 只有本人 | 只有本人 | `note.*` 是唯一进 prompt 的自由文本——不能让别人往里写 |

`memory_items`、`user_prefs`、`style_profile` 上**只有**"共享是有意的"那两张（导图读、材料库）之外的口径：其余按 owner 隔离。三者都是 PR#27 新加的列（老库迁移到 `owner = ''`，语义是"无主"，只有部署方能改），迁移在 `repository.connect()` 里两段式跑，幂等且可从半成品状态续上。

**为什么门禁在中间件而不是路由依赖**：`IdentityMiddleware` 先把身份放进 ContextVar，`GateMiddleware` 只读。两者注册顺序不能反——Starlette 的 `add_middleware` 是 `insert(0)`，后注册的在外层，反了就是"门禁跑在身份解析之前"，`current_identity()` 恒匿名、**整站 401**，而现象看起来像部署坏了或者 cookie 没生效。`tests/test_gate.py::test_the_gate_wraps_the_identity_layer` 钉住这个顺序。

**前端启动顺序也是硬要求**：进页面先 `GET /api/auth/me`（拿游客 cookie）**再挂载 `App`**。反过来（先挂载）会让挂载瞬间发出的那批请求（偏好、知识库、可恢复的运行）赶在 cookie 之前到后端，一起 401——现象是"页面一打开就报错，刷新一下又好了"。见 `frontend/src/main.ts`。

**会话是 HttpOnly cookie，不是 JWT，也不出现在任何响应体里。** cookie 名 `mm_session`，属性 `Path=/; HttpOnly; SameSite=Strict`（走 https 才加 `Secure`，本机 http 不加——否则部分浏览器直接不收）。库里存的是 `sha256(token)`：库文件泄漏时拿到的是哈希，不是一串能直接用的 cookie。**可撤销**是这次整改的硬要求，无状态 token 只能等它自己过期，所以没用 JWT（详见 `backend/app/auth/store.py` 的文件头）。

**游客 cookie（`mm_guest`）只由 `GET /api/auth/me` 下发**，`Max-Age` 一年。它的语义是"这台浏览器"而不是"这次登录"，免费额度那一批会按它计数；放在别的接口上（每次生成、每次保存）会让"我到底发过几个游客 id"变成无法回答的问题。

**游客 cookie 的值是签名过的**（`<32 位 id>.<16 位 hmac>`，见 `auth/store.py` 的 `sign_guest` / `read_guest`）。没有签名时它只是一段格式合法的十六进制串——改一下就能变成别人的身份（审查时实测：拿别人的 id 能读到他的草稿、待处理列表、他那次运行的树）。签名密钥从库文件路径派生，**不引入新环境变量**：多一个 env 就多一种"忘了设 / 两台不一致"的故障形态，而这两种故障都表现为"游客数据全丢"。伪造或形状不对的 cookie **等价于没有 cookie**（`/me` 补发一个新的），不是 401——游客是合法状态，一份坏 cookie 不该让页面打不开。签名不防"cookie 被真偷走"，那由 `HttpOnly` + `SameSite=Strict` + 只走 https 管。

**口令用标准库 `hashlib.scrypt`**（`n=2^14, r=8, p=1`，实测一次 36 毫秒 / 16 MB），不引 `passlib` / `bcrypt` / `python-jose`——这台机器是 2C2G，少一个依赖少一份部署风险。哈希串是 `scrypt$n$r$p$<salt_hex>$<hash_hex>`，**参数写进串里**，所以将来调参不会让老密码全部失效；校验时按串里那组参数重算，并对 `n/r/p` 设上限（一行被改成 `n=2^30` 的记录会让进程申请几百 GB，那是拒绝服务）。

**没有"找回密码"**：仓库里没有 SMTP，发不出任何邮件；假装有这个流程（"我们会给你发一封邮件"）比没有它更糟。真要做，入口是部署方直接改库。

**登录失败是限速的，而且限速在哈希之前。** 两个桶 `ip:<地址>` 与 `acct:<小写账号名>` 各记一份失败，判据取严——只按账号限，撞库（一个口令试很多账号）绕过；只按 IP 限，NAT 出口后面的人互相拖累。规则是 15 分钟滑动窗口内前 20 次失败正常回 401，之后每次都要等 5 秒（429 + `Retry-After`），等够了放行一次，也就是封顶 12 次/分钟；窗口滑出后重新拿到 20 次，"昨晚忘了密码"不会欠到第二天。**`store.authenticate()`（那次 36 毫秒的 scrypt）在限速判据之后才被调用**——顺序反过来的话，被拒的请求照样烧算力，限速本身成了放大器，用例 `test_a_throttled_login_does_not_touch_the_hash` 钉的就是这一点。成功登录只清账号桶、**不清 IP 桶**：清 IP 桶会让"持有一个有效账号的人"能用它给自己重置后继续撞别人，而那正好是最需要防的形状；IP 桶只随窗口自然衰减。限速表读写异常时**放开**（记 `logger.error` 并当作没有记录）——关掉保护比把所有人关在门外好。

**密钥与地址是绑定的（`api_key_host`）。** P0-1 的第一层是"发请求之前挡一道"（`probe._refuse`），第二层在这里：库里每一把密钥记着**它被保存时那个主机名**，`store.resolve()` 只在当前生效地址的主机名与锚点一致时才交出明文。这条挡的是第一层挡不住的形状——攻击者先在自己的面板里把地址改成 `https://attacker.example`（这一步本来没有任何密钥出门），下一次正常生成就会带着已存密钥去访问那个地址。锚点只记主机名不记端口与路径：同一台机器 `/v1` → `/v2` 不该逼人重填，换主机才意味着"换了一个会收到这把密钥的对方"。锚点是**落库**的而不是每次读时算——算的话会跟着新地址一起漂，绑定等于不存在。老库升级时该列补成空串，此时按那一行自己的 `base_url` 兜底，不会把所有已存配置一次性打成"未配置"。

**恢复动作是"重新粘贴一次密钥"，判据是"这次提交了密钥"而不是"提交的与库里那把不同"。** 用户换地址时多半没换供应商，粘回来的就是同一把；若拿"值变了"当判据，最正常的恢复姿势会**静默失败**：界面说"重新粘贴一次即可"，粘完仍是 `unbound`，而用户唯一能想到的解释是"这个功能坏了"。提交密钥就是一次明确的"我认可这个地址"。攻击者这边不受影响——他改地址只能不带密钥，带密钥得先有密钥，而打码值那一道（`settings_api_key_masked`）已经挡住了。

**模型配置按人分开存，不是全站一份（BYOK）。** 表里的主键从"单行 `CHECK (id = 1)`"换成了 `owner`，取值三种：`''`（本站共享配置，游客免费额度用的就是它）、`user:<用户 id>`、`guest:<游客 id>`。**游客也能存自己的密钥**——他不想填而已，不是不许。

回落链是 **当前身份自己那一行 → 本站共享那一行 → 环境变量 → 空**，逐字段独立判断。中间那一档必须有：否则一个刚注册、还没填密钥的人一条路都走不了，连"本站是不是配好了"都读不到。反过来，它只在自己那一行**缺这个字段**时才生效，不然 BYOK 会被公共配置永久盖住。

`api_key_source` 因此多了一个值 `instance`：那一项是从共享配置借来的。它和 `saved` 必须分开——看到 `saved` 时改自己那行是改自己的；看到 `instance` 时改自己那行只是**覆盖**，清掉就回公共配置，两件事混成一个来源值会让"清除"变成不敢点的按钮。

**部署方就是这台机器上第一个注册的账号**，不设 `MINDMAP_ADMIN` 之类环境变量（多一个"忘了设 / 设错了"的故障形态）。只有他能写共享那一行（`scope: "instance"`，非部署方 403 `settings_forbidden`），也只有他能看见它（`instance` 字段，别人是 `null`——他们改不了，那是一片点不动的表单；而且 `base_url` 是部署方用的哪家供应商，不该是所有人可见信息）。判定用 `created_at` 排序、`id` 兜底，两人同毫秒注册时"谁是部署方"不摇摆；他登出也不影响判定。

**老库迁移是两段式、可续、幂等**：`RENAME` 老表 → 立刻建新表 → `INSERT OR REPLACE ... SELECT` 把那一行搬成 `owner = ''` → `DROP` 老表。判据是"有 `id` 列且没有 `owner` 列"（老表独有的形状；用 `owner` 判会在迁移到一半时把半成品当已完成）。每一步都可重入：进程被杀后下次看到新表与老表并存就再搬一次，老表已删就什么都不做。老行补 `api_key_host` 是空串，按那一行自己的 `base_url` 兜底，不会在升级后突然把已存配置全打成"未配置"。


**流式接口只有一套帧格式**，就是 `/api/agent/runs/stream` 的这套；旧的 `/api/generations/stream`（`stage` / `skeleton` / `node` / `done` / `error` + 末尾 `data: [DONE]`）已连路由一起删除。`backend/app/generation/stream_partial.py` 现在只剩增量解析器（`parse_partial` / `collect_new_nodes`），供 agent 的 `node_grow` 预览调用，不再有帧拼装。

**`run_id` 让一次运行有身份（新增）。** 以前请求里没有 run id，服务端没有能力识别"同一次运行又投递了一遍"，于是换标签页、刷新重发、客户端重试都会新开一次真跑——两倍的钱、两份结果，两个标签页各存一次就是两张图（`create_map` 每次 `uuid4()`）。现在：

- 带同一个 `run_id` 且该行 `status=running` → 409 `run_in_progress`（同一个 run 并发跑两次会互相打架）。
- 带同一个 `run_id` 且该行 `done` → 直接把已有结果按原样的帧重放（`snapshot` → `done`，`mode` 照原值），**不再调模型**。
- 带同一个 `run_id` 且该行 `cancelled` / `failed` → 允许重跑，复用同一个 id（那是用户主动重试，不是重复投递）。重跑期间不清空上一轮的草稿。
- 不带 `run_id`（老客户端）→ 服务端补一个，**响应体逐字节不变**，只是额外落一行台账。

`done` 之后这行冻结：一条迟到的取消不许把已经交付的结果改写成 `cancelled`。SSE 因此从"run 本身"降级成"这一行的一次观察窗口"，帧格式一个字节都没改。

**运行结束（含被取消）时草稿落库。** 这是"关掉页面那 40 秒白花"的解：abort 会让 uvicorn 关掉生成器、`aclosing` 触发 `task.cancel()`，工具里的模型请求被取消且已经花掉的调用不回滚——原先 `ctx.candidate` 只活内存，用户回来什么都看不到。现在 `_frames` 的 `finally` 里写一次（正常 → `done`，异常 → `failed` + `failure`，什么都没发出去就断了 → `cancelled`，**只要树非空就写**）。前端进页面时问一次 `GET /api/agent/runs`，有残留就在侧栏提示一句（不是弹窗）：点「查看」把那份结果直接装进画布（与生成落地走同一条 `commitGenerated`），点「丢弃」只在本会话内静音——`dismissed_at` 那一列已随候选层删除（`acfdb5b`），没有"丢弃"这个写库动作了。
信封只有 `{seq, type, ...}`：`seq` 由服务端补齐，没有 `v` / `runId` / revision 字段（轮次与树的版本在各事件自己的 payload 里）。产物是 `ops` 操作批次或整树 `snapshot` 而不是"增量节点上屏"；`node_grow` 只作深层生成的预览帧、收到 `snapshot` 即作废，前端把它单独挂在 `agentPreview` 上（不混进 `draft`），`done` 一到才整树落画布——没有"用户点接受"这一步。`MINDMAP_LLM_MODE=demo` 也作用于 agent：`run()` 入口直接发 `stage` / `snapshot` / `done` 三帧（`done.mode=demo`），不调用模型，因此演示模式下没有 `tool_start` / `ops` / `node_grow` 帧。协议见 `docs/AGENT_STREAM_PROTOCOL.md`，设计背景见 `docs/AGENT_PLAN.md`。

记录形如 `{"id":"...","version":1,"content":{...},"updated_at":"...Z"}`。生成接口不会自动保存。`demo` 为演示模式，不是 AI；`model` 表示请求已成功经模型服务处理，不代表内容必然正确。`/api/agent/tasks` 仍返回固定阶段列表，那是桩，不反映真实进度。

**文档解析按格式走不同链路**（详见 [部署文档](DEPLOY.md)）。`.pdf` 逐页渲染后交模型视觉识别（demo 模式回落文本层，扫描件则 422），处理到第几页由上下文预算算出（`plan_page_count`，扫描页按 379 字/页折算），另有 300 页的转换护栏；`.docx`/`.pptx`/`.xlsx`/`.html`/`.md` 走 Docling 读结构化格式，不加载版面模型、离线可用；`.doc`/`.ppt` 要先经 LibreOffice 转 PDF 再视觉识别，没装 LibreOffice 时 422 并附安装命令；`.xls` 用 `xlrd` 直读单元格；`.png`/`.jpg`/`.webp`/`.gif`/`.bmp` 复用多模态调用；`.wav` 走百炼 Qwen-Audio-ASR（≤300 秒，更长自动切片）。解析失败或拿不到正文时返回 422，不返回空内容。

`source` 是 `/api/ingestion/text` 返回的对象，也可按 `/docs` 中的 Source 结构提交；每次 agent 运行最多 50 个来源，必须均为非空且已解析，ID 不可重复。当前超长会报错，不会静默截断或丢弃来源。

**材料什么时候进知识库，以及 id 为什么会变**：上传只解析、不入库；**生成时**才把这次用到的材料收进知识库（`backend/app/knowledge/remember.py`）。入库后持久 id 与解析时给的临时 id 不同，所以这批 id 的映射会作为 `sources` 帧在事件流最前面下发一趟，前端收到后把本地条目就地改名（`sources.remap`）。之后保存的导图、节点引用、抽屉里取原文用的都是持久 id，重开导图也能对上。整批入库共用一个连接一个事务，并带总预算（`REMEMBER_BUDGET_SECONDS`，5 秒）：超预算或单份失败的材料保留临时 id 继续参与生成——入库是旁路，不能拖住第一帧。开关是 `MINDMAP_LIBRARY_REMEMBER`（只有 `"0"` 表示关），与 `MINDMAP_LIBRARY_SEARCH`（生成时要不要从库里捞材料）是两件事，可以分别关。

**读入口只读，写入口只有两条**：`GET /api/knowledge/library*` 只看库、不改库（也不刷 `last_used_at`——"看了一眼列表"不等于"用到了这份材料"，那个字段是 `prune` 保留与否的依据），线上把 `MINDMAP_LIBRARY_SEARCH` 置 0 时照常可用。写入口是 `DELETE /api/knowledge/library/{id}`（删材料）与 `DELETE /api/knowledge/aliases*`（改合并结果），两条都**不受任何检索开关影响**：那是"改我自己的库"，不是"生成时要不要查库"。

**删材料为什么不是软删**：材料正文是导图证据的最终依据。软删（标记删除 + 清正文 + 留 `source_refs`）买的只有一句更好的诊断措辞，代价是一次 schema 迁移、一遍读路径过滤、外加"重新上传同一份 `content_hash` 撞上墓碑"的复活规则——而 `SOURCE_TEXT_UNAVAILABLE` 已经把"删了但引用还在"表达清楚了。所以：未引用直接硬删；被引用先回 409 把代价说清（`detail.cited_by`），确认后 `force=true` 真删，导图里的 `source_refs` 原样保留（值对象、无外键），那些节点的引用变成"来源原文不可用"。

**别名为什么可以删**：`ALIAS_OF` 不是证据，它不参与游走（`CYPHER_WALK` 只走 `:REL`），也不带 `source_id` / `chunk_start`。删掉一条别名只让两个名字重新变成两个实体，挂在它们身上的证据边一条不动——所以这是可逆的语义修正，不需要备份。**但不要用重跑实体消解来"修正"猜错**：它按同一批阈值重新算，删掉的那条会一起回来。

## 已注册但尚未实现

| 路由 | 当前行为 |
|---|---|
| `POST /api/knowledge/search` | 返回空列表与 `implemented: false`，没有接入检索。 |
| `POST /api/agent/tasks` | 返回固定阶段与 `implemented: false`，不会生成导图；真正能跑的是 `/api/agent/runs/stream`。 |

## 功能接口规划（均未实现）

| 模块 | 候选接口/契约 | 目的 |
|---|---|---|
| 多模态文件 | 已落地 `POST /api/ingestion/files`（文档 + 图片 + WAV）；来源查询接口待确定 | 上传或解析 PDF、Word、PPT、图片、WAV 音频已可用；音频已有逐句时间戳，但节点引用还不能指回"第几秒"，页码级定位也缺。 |
| 生成任务 | `POST /api/generation-tasks`、`GET /api/generation-tasks/{id}`、`GET .../{id}/events` | 多来源输入、持久任务状态与进度；输出仍转换为统一导图结构。当前只有 agent 这一条 SSE 流，没有持久任务。 |
| 优化 | 已有 `/api/optimization/suggest`，持久化和应用接口待确定 | 未来补齐矛盾、缺失、重组、跨节点关系候选及依据，不直接覆盖。 |
| 知识库 | 已落地 `GET /api/knowledge/library` 与 `.../library/{id}`（读），`DELETE .../library/{id}`（删材料）与 `GET/DELETE /api/knowledge/aliases*`（实体别名的清单与修正） | 材料列表、被引用次数、正文预览、删材料可用；别名可列可删（删掉的是"模型把两个名字认成一个"的错判）。**产生别名的入口还没有**（`graph_resolve.resolve_entities` 是运维 API，产品不调）——所以"跨材料同名不同写法合不起来"这一档仍然存在；检索（`POST /api/knowledge/search`）仍是桩。 |
| AI 代理 | `POST /api/agent/runs/stream`、`GET /api/agent/runs` 系列已实现；`POST /api/agent/tasks` 仍是桩 | 工具循环、生成即落画布、"运行结束后落草稿"（`runs` 表，SQLite）与用户偏好（`user_prefs`）已可用；任务化长跑、`runs` 的跨进程/跨机存储（现为本地 SQLite，多 worker 共享同一文件）与多角色仍待设计。 |
| 协作 | 房间授权接口 + Yjs WebSocket | 房间鉴权、实时同步、在线状态、恢复；最终协议由协作负责人完成设计。 |
| 更多导出 | `/api/export/puml`、`/markdown`、`/opml` 均已实现；图像导出待开发 | PNG/SVG 渲染要额外依赖，优先用浏览器端截图或交给 PlantUML 服务。 |

未来接口的请求、响应、权限、错误和任务生命周期需在实现前由 RPeGio 与陈嘉涛共同评审，不能将候选路径视为已实现承诺。
