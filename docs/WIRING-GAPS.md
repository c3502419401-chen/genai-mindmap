# 三处"代码在、用例绿、产品里没接上"——根因、修法与实跑读数

> 来源：`docs/ACCEPTANCE-EVIDENCE.md` 的 B1 / B3 / B4。那一轮只报不改（"未改代码，逐条报"），
> 这一轮把三条修掉，并把"同类还有多少"扫了一遍。
>
> 基线：`main`，从 `94e30ed` 起改。后端 `run-backend.bat`（`MINDMAP_LLM_MODEL=deepseek-chat`，
> 8000），前端 5173，本地库 `backend/data/mindmaps.db`。真跑脚本在 `backend/scripts/acc3/`。
>
> 修前的数字（那一轮的读数，保留在这里做对照）：后端 **3221 通过 / 37 跳过**，
> 前端 **82 文件 / 1360 用例**；前端数字修前修后相同（这一轮没动前端行为，只多显示一句话）。
>
> 后端全量在**本次范围内恒为 +26 条**（`test_wiring_gaps_baseline.py` 5 条 + 三处缺口
> 各自的详测 21 条）。但同一个工作区里另一条线正在增删 `tests/test_few_branches.py`
> （未跟踪），所以跑出来的总数会在 **3255–3256** 之间漂——那个数不是本次的成果，
> 读的时候看增量、不要看总数。

---

## 零、三条一句话

| 缺口 | 是刻意留的吗 | 修法 | 修完的实跑读数 |
|---|---|---|---|
| `sources` 的 id 被换掉 | **不是**——换 id 是设计，只给 `mapping` 是遗漏 | SSE `sources` 帧补上**最终清单**（`SourceIdentity`，不带正文），`mapping` 保留 | 真 SSE：`sources` 帧的 1 个 id 与 `done` 帧 `content.source_usage` 里的那个 id 一致 |
| `/api/knowledge/search` 是桩 | **不是刻意**，是"没来得及"（`TODO(F12/F13)` 从脚手架那天就在） | 走生成链路同一套：`_library_sources` 选候选 → `search_parent` 打分 → `touch` 记账 | 真库 4913 条向量：5 条命中、`method=hybrid`、**1.1–1.7 s**（首次冷缓存 9.7 s） | <!-- claims-ok: knowledge_search_implemented 缺口名 + 那一轮的修法与实跑读数 -->
| `link.suggest` 零调用 | **一半是刻意**：不给"应用"按钮是刻意的（改层级不可逆）；"连出现都没有"是遗漏 | 接进 `/optimization/suggest`，产出 `kind="link"` + `SuggestResponse.link` 状态 | 真图（80 节点、无缺陷）：**2.9 s → `already_best`**；人造错挂树：**1.97 s → 1 条 link** |

---

## 一、`sources` 的 id 在生成侧被换掉（B3）

### 根因：谁换的、为什么、理由还成立吗

换 id 的是 `app/knowledge/remember.py::remember_sources()`，它排在 SSE 开流**之前**
（`agent/router.py`）：

```python
remembered = await run_in_threadpool(remember_sources, body.sources)
if remembered.mapping:
    yield encode_event(SourcesEvent(seq=seq, mapping=remembered.mapping))
    body = body.model_copy(update={"sources": remembered.sources})
```

`remember_sources` 把材料交给 `library.ingest_many`，库用 `uuid4()` 生成**持久 id**
（正文 sha256 只判重、不充当 id），于是 `Source.id` 从 `2fa76b62…` 变成 `8007363c…`，
导图里 `source_refs.source_id` 记的是新的那个。

**理由仍然成立，不能靠"不换"修**：导图要落库、要被重新打开、要能"点引用跳回原文"、
要被"这份材料被几张图引用"统计——这些路径全都按 `source_refs.source_id` 去
`library.load()` 查。请求内 `uuid4` 活不过这一次请求，留着它等于让引用在保存那一刻
起就全部失效。所以"换"是对的，错的是**换完之后只把差集交出去**。

### 只给 `mapping` 为什么不等于把答案给了

`mapping`（旧 → 新）是**换算**，不是**清单**。用来换算的前提是"调用方手里有一份
按旧 id 索引的完整材料表，并且知道要来做这个换算"：

- 前端两份都有：`stores/sources.ts::remap()` 照着 `mapping` 就地改 —— **所以前端一直是对的**；
- 脚本 / 移动端 / 第三方集成**从来不知道有这一步**：它们拿上传应答里的旧 id 去
  `/optimization/check`，得到一条不落的 `SOURCE_TEXT_UNAVAILABLE`（实测 33/37 条），
  看起来像"引用核验坏了"。

要判断这是"补救"还是"设计"，一句话就够：**如果补丁只对一个客户端有效，那它是补丁。**
`matches` 之外的调用方拿不到任何东西，所以它从来不是设计。

### 修法

`SourcesEvent` 加一个字段（`contracts/source.py::SourceIdentity`）：

```python
class SourcesEvent(_EventBase):
    type: Literal["sources"] = "sources"
    mapping: dict[str, str] = Field(default_factory=dict)     # 旧 → 新（保留）
    sources: list[SourceIdentity] = Field(default_factory=list)  # 最终清单（新增）
```

三条边界都写进了 docstring：

1. **不带正文**。50 份 × 4000 字塞进一个 SSE 帧既撑大帧，也把材料原文复制进事件流；
   正文只有调用方自己有（它上传的），事件只需要给身份（`id` / `kind` / `filename` /
   `status` / `chars`）。
2. **清单是"全部材料"**，含没换 id 的那些、含入库失败按原 id 继续的那些（那时
   `mapping` 为空、这一帧根本不发，既有"帧序列逐字节不变"的不变量没破）。
3. `mapping` **保留**：删了会让前端白改一遍，而"前端不需要再算差集"不是必须同时达成的目标。
   **前端没有因此坏**——`remap()` 照旧，`npm test` 1360 条全绿。

另外在 `CheckRequest.sources` 的 schema description 里把前置写清（`id` 必须是生成侧
持久 id，换算在 SSE `sources` 帧里），这就是 B3 建议的"把前置写进契约"。

### 纯 API 客户端现在能不能拿到一致的 id

**能**，两条路都通（`tests/test_source_id_contract.py` 各钉一条）：

- 按 `mapping` 把自己手里的材料改 id（等价于前端 `remap()`）；
- 或者直接按 `sources` 的顺序对（与请求同序，`chars` 可核）。

真 SSE 读数（`scripts/acc3/probe_gaps.py --sse`，一次真生成）：

```
48.11 s · 106 帧 · 95 节点
sources 帧的 id：      ['e3889c15']
done 帧 source_usage： ['e3889c15', '6964824e', '8e7e0c9a', '7ab96c85', '2a4185ef',
                        '1e56f73e', 'e455454b', '80f8b81e', '33f79608']
uploaded_ids_are_final: true
external_in_usage: 8
```

上传那份材料的 id 在两端逐字相同。另外 8 份是**生成途中**外部检索入库的
（`_external_sources` 自己 `library.ingest`），它们本来就在库里、本来就是持久 id，
身份从 `content.source_usage` 读（那里带 `filename` / `url`）——这条边界写进了脚本注释：
断言方向是"帧里的 id ⊆ 最终 content 用的 id"，不是反过来。

---

<!-- claims-ok: knowledge_search_implemented 章节标题是缺口名，正文是修复记录 -->
## 二、`/api/knowledge/search` 是桩（B4）

### 为什么是桩：不是刻意，是"没来得及"，但有历史形状

`git log -S "implemented=False" -- backend/app/knowledge` 只有一个提交：
`c4ff5e1 feat: add mind map application scaffold`——**脚手架那天的第一个桩**，
连 `search()` 里的 `TODO(F12/F13)` 也是那天写的。没有任何一处注释或提交信息给出
"故意留桩"的理由（对比 `optimization/router.py::repair_map` 的 `include_in_schema=False`：
那一条把"为什么先不导出"写得清清楚楚）。

反过来，`chunk_vectors` 里 4913 条 × 1024 维、`search_sources` / `search_parent`
被生成链路用了几个月、`scripts/measure_*.py` 一批度量都在跑——所以这不是"能力没做"，
是"能力做完了，出口忘了接"。用例覆盖 service 层、没一条走路由，于是绿与不可用同时成立。

权限/多租户不是理由：库表没有 owner 列（`library_sources` 只有 `content_hash` /
`filename` / `kind` / `text` / `metadata` / 时间），检索也不按身份过滤。要加租户是另一件事，
与"这个路由要不要接"无关。

### 接上之后是什么行为

`search()` 现在走**生成链路的同一套**，不另写一条：

1. `_library_sources` 选候选（最近 100 份 → 查询词字面覆盖率粗筛 → 至多 `LIBRARY_PICK=5` 份）；
2. `search_parent` 打分（子块匹配、父块返回；`MINDMAP_PARENT_RETRIEVAL=0` 时它自己转发
   到 `search_sources`，粒度退回单级块——所以两条粒度都在同一条路上）；
3. `touch` 记一次"被用到"（`prune` 的保留依据）。

`KnowledgeHit` 从 `{source_id?, snippet, score}` 扩成"来源身份 + 原文区间 + 片段"：
`source_id`（持久 id，必填）、`filename`、`kind`、`start`/`end`（码点偏移，与 `SourceRef`
同口径）、`snippet`、`score` + 两路原始分 + `method` + `rerank_score`。

`implemented` 改成恒 `true`，语义收窄成"这条路接上了没有"——**空结果有两种原因**
（库里没命中 / 库是空的），它们混在一个字段里时，"库是空的"会被读成"功能没做"，
这正是它此前被这么用的原因。

实跑（真库，4913 条向量）：

```
POST /api/knowledge/search {"query":"缓存命中率对查询延迟的影响","top_k":5}
第 1 次  200 · 9707.5 ms · implemented=true · hits=5 · 首条 method=hybrid
第 2 次  200 · 1699.7 ms · hits=5
第 3 次  200 · 1130.0 ms · hits=5
```

**首次 9.7 秒要单独说清**：向量缓存是冷的（`vector_cache` 按块文本哈希缓存，第一次要
把候选材料的块全部 embed 一遍）。第二次起 1.1–1.7 s，是稳态。9.7 s 不是"每次都要"，
但它是**第一个用户第一次点击**要等的，写在这里而不是藏进平均里。

---

## 三、`link.suggest` 零调用（B1）

### 为什么没接：两层原因，只有一层是刻意的

- **刻意的那层**：`link.py` 的 docstring 明写"界面上它只有忽略、没有应用"。理由站得住——
  改层级不可逆（撤销只救回这一次会话），而"点一下应用到底改成什么"没有样本可以定义。
  前端 `APPLIERS` 与 `APPLICABLE_KINDS` 里没有 `link`，是这条决定的落地。
- **遗漏的那层**：**连"出现在界面上"都没有发生**。产品代码里一个调用者都没有，
  于是 `/suggest` 永远只产 expand / dedupe / add_detail。这不是"产品决定先不给按钮"，
  这是"产品还没决定要不要给这条建议"——两者在文档里被写成了前者。

「接了会有问题」的那半也确实存在，两条，都在接线时解决了：

1. **冷启动可能返回 0 条**（用户看到空）。真跑了才看清它分两种：`already_best`
   （问过，模型认为现在挂得对）与 `no_targets`（没有可问的节点）。两者在界面上都表现为
   "一条建议都没有"，所以响应里加了 `SuggestResponse.link`（`status` / `targets` /
   `calls` / `dropped` / `detail`），界面在建议列表底下显示一句
   （`StructureCheck.vue` 的 `data-test="link-note"`）。
2. **成本**。`link.suggest` 一个目标一次模型调用，还要扫跨图历史与材料库。第一次接上时
   一次 `/suggest` **跑了 20906 ms**（2 个目标 + 两处库扫描）——那不是一个按钮该有的延迟。
   三条成本闸，每条都有依据：
   - `MAX_LINK_TARGETS = 1`：只问最有价值的那一个；
   - `LINK_CONTEXT_ENV`（`MINDMAP_OPTIMIZATION_LINK_CONTEXT`）**默认关**：不查跨图历史与
     材料原文。依据是 `docs/LINK-SUGGESTIONS.md` 的消融——把这两样全剥掉，藏节点再猜的
     准确率 14/16 → 13/16，**在噪声里**；而它们各自要扫库 + 一次 embedding，是这一层最贵
     的两段。要复现论文那套完整输入时置 1；
   - 目标选择：`SINGLE_CHILD` 的落点**往下挪一层**（诊断说的是"这个父节点只有一个孩子"，
     而 link 问的是**那个孩子**该挂哪；拿父节点去问，回答"就该在这儿"是必然的）。
     没有形态诊断时兜底取一级分支下第一个叶子——真实生成的图多半 0 条诊断，只认诊断的话
     这一层在正常图上永远不出现。

还有一条降噪规则：**首选父 == 当前父就丢弃**（`parent_choices` 把当前父排在候选表第一位，
所以"首选父是当前父"就是模型在说"不用动"）。不丢的话，一张本来没问题的图会收到几条
"可以挂到它现在待的地方"，用户点一次就不信这个面板了。

### 接上之后：冷启动返回几条

真读数（`scripts/acc3/probe_gaps.py`）：

| 输入 | 耗时 | `kinds` | `link` |
|---|---|---|---|
| 库里最新的真图（80 节点，一条形态诊断都没有） | 2901 ms / 1853 ms | `{}` | `already_best`，`calls=1`，`dropped=1` |
| 人造错挂树（11 节点，「数据出境合规」挂在「市场定位」下） | 1971 ms | `{expand:1, link:1}` | `ok`，`calls=1`，`dropped=0` |

错挂树那条的产出（原文）：

```
「数据出境合规」现在挂在「市场定位」下面，也可以挂到「风险控制」：
数据出境合规是产品上市前必须评估的合规风险项，与供应商资质审查、供应链中断预案同属风险控制这一支

what = 产品数据出境需满足的合规要求与限制
line = 风险控制——与供应商资质审查、供应链中断预案同属合规与风险议题
candidates = [risk（风险控制）, root（挂在一级评审根下与成本、风险、市场并列）]
```

这就是计划书 3.3.2 举的那类建议（"分支 A 与分支 B 存在因果关系，建议添加连接线"）的
第一版落地：它给的是"该挂哪一支"而不是"连一条线"，但判据是同一条——**这个节点与
哪条论证线同源**。`kind` 仍是 `link`（契约里的值域没变）。

**所以"冷启动返回几条"的准确答案是：干净图 0 条（`already_best`，模型认为都对），
有错挂的图 1 条。** 0 条不是失败——它是"问过了"的证据，`link.status` 会把这句话说出来。

---

## 四、自评：哪一处是"当初刻意留、现在该接"，哪一处是纯遗漏

判据只看 git 历史与注释里**有没有写出理由**：

| 缺口 | 有理由吗 | 判定 |
|---|---|---|
| `sources` id | 换 id 有理由（跨会话可解析），**只给 `mapping` 没有理由** | 设计对、出口缺一半 → **疏忽** |
| `/knowledge/search` | 只有 `TODO(F12/F13)` 与 `implemented=false`，**没有一句"为什么先不接"** | **纯遗漏**（脚手架遗留） | <!-- claims-ok: knowledge_search_implemented 回顾表格：讲的是当初为什么是桩 -->
| `link.suggest` | 有理由，而且写得很清楚——但理由覆盖的是"不给应用按钮"，**覆盖不到"连出现都没有"** | **刻意留门 + 遗漏接线**：该接的是"出现"，不该接的是"一键应用" |

一条反例可以校准判据：`optimization/router.py::repair_map` 挂着
`include_in_schema=False`，旁边写着"这一轮 `openapi.json` 与 `schema.d.ts` 由另一条线在改，
不能重导，所以先让接口存在、快照不动"，并列出"等快照能重导的那一轮要做三步"。
**同一个人、同一个仓库，会写理由。** 没写理由的那几处，就是没想过——而不是想过之后的克制。

---

## 五、更重要的是：这个仓库里还有多少同类

三处的共同点是"用例测了函数、没测接线"，所以需要一条**静态**判据。
脚本：`backend/scripts/acc3/find_unwired.py`。

```bash
cd backend
python scripts/acc3/find_unwired.py            # 汇总 + 全部零引用清单
python scripts/acc3/find_unwired.py --suspects # 只列真正可疑的
```

判据（三条，各自都有误差，写在脚本 docstring 里）：模块顶层、名字不以 `_` 开头的 `def`；
"引用"= 在 `backend/app/**/*.py` 里除定义行外 `\b函数名\b` 出现的次数；自动排除三类
有理由的（`route` 由框架调、`decorated` 由 pydantic 调、`exported` 在 `__all__` 里）。

实跑读数（本次改动之后）：

```
顶层公开函数：515
  零产品引用（含路由/装饰器/__all__）：39
    其中标记为 route：33
  真正可疑（零产品引用且无任何豁免标记）：6
```

6 条逐条判：

| 位置 | 名字 | tests | 判定 |
|---|---|---|---|
| `agent/tools.py:9043` | `reset_graph_notices` | 12 | 测试隔离专用（docstring 明写） |
| `generation/service.py:204` | `reset_learned_max_tokens` | 7 | 测试隔离专用（docstring 明写"只给测试用"） |
| `agent/tools.py:4916` | `is_detail_only_prompt` | 10 | 测试与测量脚本判定"这是哪一轮"用 |
| `optimization/verify.py:276` | `quote_in` | 2 | 内部工具，只被用例用（**可删或私有化**，不是缺口） |
| `generation/service.py:539` | `thinking_options` | 2 | **同类缺口（第 5 处）** |
| `generation/service.py:1623` | `diagnose_density` | 5 | **同类缺口（第 6 处）** |

两处新发现，都不在本轮的改动边界里，所以只报不改：

1. **`thinking_options()` 零引用**——它的 docstring 自己写着"前端下拉的候选"，
   而**没有任何路由暴露它**：openapi 的 30 条路径里没有它。前端因此自己抄了一份
   `stores/options.ts::THINKING_MODEL_OPTIONS`。后果不是"功能没了"（下拉在），是
   **两份定义源**：后端往 `THINKING_MODELS` 加一个思考方式，前端下拉不会变，
   而两边都不会报错。修法是加一条 `GET /api/generation/thinking-options`（要重导快照）
   并让前端改成请求它——**比前三条更值得做，因为它防的是"静默不一致"**。
2. **`diagnose_density()` 零引用**——它产出的 `SPARSE_BRANCH`（"这一支只有 1 个要点"）
   是一个**从来不存在的诊断码**：`check()` 的六层规则里没有它，前端 `DIAGNOSTIC_LABELS`
   里也没有。它要么接进 `check()`（那要先决定它和 `SHALLOW_BRANCH` 的关系），
   要么删掉——现在这样是最坏的一种：读代码以为有这条诊断，跑起来永远拿不到。

判据的边界（必须说清，否则这套数字会被当成"只有 6 条"）：这是**字面**判据，
`\bname\b` 会把 docstring / 日志里的同名字符串算成引用，所以它给出的是**下界**；
动态调用（`getattr`、注册表）会被误判成零引用，那是**误报**方向。
因此它适合用来"扫一遍给出候选清单"，不适合直接做成 CI 闸门。

> **第二轮修正**：上面这段边界当时低估了误差的**方向**。判据 v2（语法树而不是字面）
> 实测把"真正可疑"从 6 条抬到 11 条，多出来的 6 条全是**名字只出现在注释里**、
> 所以 v1 从来看不见的（`graph_store.add_edge` 被注释提到 24 次、被调用 0 次）。
> 量具、闸门与"为什么只能做成棘轮"见第九节。

---

## 六、边界与残留风险

- 改动的产品文件：`contracts/source.py`、`contracts/diagnostic.py`、`agent/contracts.py`、
  `agent/router.py`、`knowledge/service.py`、`knowledge/router.py`、
  `optimization/service.py`、`optimization/router.py`；
  前端 `api/types.ts`、`api/client.ts`、`stores/structure.ts`、
  `components/StructureCheck.vue`、`mindmap/diagnostics.ts`。
  **没碰** `frontend/src/canvas/*`、`mindmaps/repository.py`。
- 契约：`backend/openapi.json` 重导（`SourcesEvent` 不进快照——SSE 路由不声明响应模型），
  前端 `schema.d.ts` 用 `npm run gen:api` 重生成；`tests/test_openapi_snapshot.py` 绿。
- **`/knowledge/search` 首次 9.7 s**（向量缓存冷）。没做"预热"也没有偷改口径：
  它是缓存机制的正常代价，稳态 1.1–1.7 s。
- **`/suggest` 现在多了一次模型往返**（默认开、1 个目标、约 2–3 s）。`link: false`
  可以按请求关掉，`MINDMAP_OPTIMIZATION_LINK=0` 可以按部署关掉。
  自动路径（生成落地后那次 `check`）**不跑** link，所以"每次生成都花钱"没有发生。
- 生成的这次真跑（48 s、95 节点）写了库：`runs` / `library_chunks` / `chunk_vectors`
  各多几行，`mindmaps` 未新增（没保存）。
- 顺手清掉 4 条 `ruff` 告警（都在 `scripts/`，是上一轮证据脚本留下的未用变量 /
  未用 import）：现在 `python -m ruff check .` 全绿。

---

# 第二轮：第五、第六处缺口（`thinking_options` / `diagnose_density`）

> 起点 `main` = `8d2f098`。这一轮只动两件事，都是为了把"两份定义源"和"一个没有产出的
> 诊断码"这两类**静默不一致**变成红灯。第七节是 `thinking_options`，第八节是
> `SPARSE_BRANCH`（结论是**码留着、函数删掉**），第九节把第五节那个"扫零引用"
> 判据的量具重新校了一遍——那里有一个必须先说清的数：**旧判据漏报 6/11**。

## 七、`thinking_options()` 零引用：两张表现在一份都不差，差的是"没有任何一处会红"

### 7.1 现状：四份手写副本 + 一份生成副本，逐项**全部对得上**

| 副本 | 位置 | 内容 | 谁保证它不变 |
|---|---|---|---|
| 真源 | `app/generation/service.py::THINKING_MODELS` | 5 个键 + 5 个标签 + 5 段提示词 | — |
| 手写② | `app/agent/contracts.py::AgentRunRequest.thinking_model` 的 `Literal` | 5 个键 | `test_thinking_options.py`（本轮加） |
| 手写③ | `backend/tests/test_generation.py:459` | 5 个标签 | 就是它自己（断言写死） |
| 手写④ | `frontend/src/stores/options.ts:19`（**本轮删**） | 5 个键 + 5 个标签 | 没有 |
| 手写⑤ | `frontend/tests/diagram-type.spec.ts:136`（**本轮删**） | 5 个键 + 5 个标签 | 没有 |
| 生成 | `frontend/src/api/schema.d.ts:971` 的 union | 5 个键 | `test_openapi_snapshot.py` + `npm run gen:api` |

逐项比对（键 / 标签）：`default/通用`、`first-principles/第一性原理`、`feynman/费曼`、
`systematic/5W1H`、`critical/批判性`——**五个全对得上，顺序也一样，零处不一致**。

所以这一处**不是"修一次对齐"**。它现在的状态是"恰好一致"：后端往 `THINKING_MODELS`
加一种思考方式，界面上什么都不会发生，也没有任何一条用例会红。②③④⑤ 四份手写副本里，
只有 ② 的**值域**会在拼错时被后端 422 挡住（那是运行期、点了才知道），"少一种"谁都不管。

### 7.2 路线：**不加接口**，改成"后端表 → 生成物 → 前端 import"

判据只有一条：**这个下拉必须在首屏可用**。它在 `ChatDock.vue` 里、生成按钮旁边，用户
点开就要能选。这条判据把"运行时请求"整条路否掉了：

1. 走 `GET /api/generation/thinking-options`，要么下拉在响应到达之前**是空的**，要么前端
   再抄一份兜底副本——后者就是现在这个病的动态版本，定义源从两份变成三份；
2. 值域本来就**已经在契约里**（`AgentRunRequest.thinking_model` 是 `Literal`，
   `schema.d.ts` 里已经是同一条 union）。再开一个接口，等于把同一件事用第二种形状
   说第二遍；
3. 仓库里没有"运行时发现全集"的客户端：`scripts/**` 全是 `import` 后端模块，第三方
   集成也不在下拉这件事上。现在开这个接口是投机。

改判条件写在前面：**如果哪天后端独立升级、前端静态托管**（旧包 + 新后端），运行期
发现才有价值——那时旧包连新值都发不出来，只有接口能救。当前仓库不是这个部署形状。

于是链路变成单向的一条：

```
app/generation/service.py::THINKING_MODELS          ← 唯一的那一份（键/标签/提示词）
    ↓ scripts/export_thinking_options.py（手跑，像 export_openapi.py）
frontend/src/api/thinking-options.json              ← 提交、可 review 的生成物
    ↓ import（构建期，零往返）
frontend/src/stores/options.ts::THINKING_MODEL_OPTIONS
    ↓ v-for
ChatDock.vue #thinking-model
```

### 7.3 "以后不一致会红"靠三件事

| 用例 | 拦的失败 |
|---|---|
| `test_table_and_contract_literal_agree` | 后端内部：往表里加了键、忘了改 `Literal` → 用户选了就 422 |
| `test_exported_snapshot_is_up_to_date` / `test_export_has_one_entry_per_thinking_model` | 后端 → 前端：改了表没重跑导出脚本 → 生成物过期 |
| `test_frontend_store_holds_no_copy_of_the_table` | 前端内部：有人把候选表**抄回去**（那正是本轮修掉的形状） |
| `test_default_is_first` | 顺序契约：`default` 排第一，否则"不选就生成"的默认值与界面显示的不是同一个 |
| `diagram-type.spec.ts` 那条（改断言，不删） | 组件把整张表**一条不漏、按顺序**渲染出来（期望值从生成物读，不再写死） |

### 7.4 ★ 造一次"后端多一种思考方式"：三段落红读数

把 `socratic / 苏格拉底` 加进 `THINKING_MODELS`，什么都不重跑：

```
$ python -m pytest -q tests/test_thinking_options.py
3 failed, 2 passed
FAILED test_table_and_contract_literal_agree          ← Literal 没跟上
FAILED test_exported_snapshot_is_up_to_date           ← 生成物过期
FAILED test_export_has_one_entry_per_thinking_model   ← 生成物少一条
```

只重跑导出脚本（假装"忘了改契约"）：

```
$ python scripts/export_thinking_options.py && python -m pytest -q tests/test_thinking_options.py
1 failed, 4 passed
FAILED test_table_and_contract_literal_agree          ← 仍然红：这个值发出去就是 422
```

补齐 `Literal` + 重导快照 + 重生成类型：

```
$ python scripts/export_openapi.py && (cd ../frontend && npm run gen:api) && npm test -- --run tests/diagram-type.spec.ts
backend/openapi.json        | 3 ++-      （只多一个字面量成员）
frontend/src/api/schema.d.ts| 2 +-
14 passed                                ← 前端**一行源码都没改**，下拉从 5 项变 6 项
$ node -e "require('./frontend/src/api/thinking-options.json').length"
6
```

**这就是"单一源"的验收标准**：后端加一项 → 先是三处红（改的人知道必须动），补完
契约后前端自动跟上（不用再改第二处）。探针已完整回滚：`openapi.json`、`schema.d.ts`、
`contracts.py` 三份与 HEAD 逐字节相同。

## 八、`diagnose_density()` 零引用：先纠正前提，再判 b

### 8.1 前提要纠正：`SPARSE_BRANCH` **不是**"从来不存在的诊断码"

第五节写的是"它产出的 `SPARSE_BRANCH` 是一个从来不存在的诊断码"。**这一句只对了
一半**，而这半个错正是它被拖了两轮的原因：

- 码是**活**的：`app/agent/tools.py::_generate_deep` 的分片路径在
  `sparse, overfull = _audit_branches(content, need)` 之后逐支产出 `SPARSE_BRANCH`
  （`tools.py:13193` 取值、`13184` 产出），两条用例钉着它**出现**与**不该出现**
  （`tests/test_agent_tools.py:504` `assertIn`、`:388` `assertNotIn`）。
- 死的是 `generation/service.py::diagnose_density()`：零调用方，`check()` 里没有，
  前端 `DIAGNOSTIC_LABELS` 里也没有。

也就是说，缺口不是"码没接上"，而是**同一件事有两份实现，其中一份没人用**。

### 8.2 三条候选逐条过

| 候选 | 判 | 依据 |
|---|---|---|
| **a 接进 `check()` 的规则层** | 不接 | `check()` 是**已落库的图**的事后体检，判据必须与生成过程无关、且是相对的（`SHALLOW_BRANCH` = "比同级浅"）。密度是**生成期**的交付判据（"要求 3 个要点、拿到 1 个"）。搬进事后体检，会在每一张导入图、每一张手改图上误报——那些图从来没人"要求过要点数"。 |
| **a′ 加前端 `DIAGNOSTIC_LABELS`** | 不加（加了没用） | 生成链上的诊断在 `AgentPanel.vue:513-517` **只渲染 `message`**（后端给的整句中文），码根本不上屏；把码当标题显示的是另一个面板（`structure.ts` → `StructureCheck.vue`，那里的码来自 `check()`/`repair`）。所以"给生成码补标签"是装饰，不是接线。 |
| **c 改用途（数值给视图层当"该收起了"判据）** | 不接 | 视图层已经有三档判据（`structureCounts` 的 `attention` / `suggestions` / `hints`，见 `mindmap/diagnostics.ts`），密度数值没有消费方；硬塞进去会让"该收起了"这件事有两个来源。 |
| **b 删掉** | **✔** | 见 8.3 |

### 8.3 b 的依据：它是同一件事的**第二份、更弱**的实现

不是"没人要"，而是"要的东西已经有了，这一份更差"：

1. **阈值**：写死 `minimum=2`；分片路径的 `need` 是从本次配置算出来的。
2. **说不出为什么**：`tools.py` 那条会带一句尾注——"已补调一次模型，仍不足，按现状接受"
   或"模型调用额度不足，未能补调"；`diagnose_density` 只有一句"少于建议的 2 个"。
3. **会重复告警**：它把"因额度没展开"和"展开失败"的分支也算成稀疏；`tools.py` 那边
   用 `skipped_set` / `failed_set` 排掉了（那里有一条注释专门解释这件事）。
4. **口径不可调**：`minimum` 参数没有任何调用方传过，等于第二套阈值。

判据回放（"用户看到它会不会采取行动"）：**会**——他能做的是手动展开那一支。而他要
做这个动作所需的信息（哪一支、差几个、为什么不足、是不是已经补调过）**全部在
`message` 里**，函数对这个动作没有贡献。所以：**码留，函数删**。

删掉的东西：`diagnose_density()`（17 行）、`tests/test_generation.py` 的
`DensityDiagnosticTest`（3 条用例）、`test_generation.py` 里那一行 import。

补上的守卫：`tests/test_diagnostic_codes.py` 两条——`SPARSE_BRANCH` 全仓**只有一个
产出点**（且它是 `agent/tools.py`，不是 `generation/service.py`）。哪天有人再养一份，
红的是这条，而不是等用户在同一块面板上读到两句不一样的话。

## 九、第五节那个判据的漏报率：**6/11**，而且量具本身要换

### 9.1 旧判据（字面）与新判据（语法树）的实测对照

`scripts/acc3/find_unwired.py` 换掉了量具：v1 数 `\bname\b` 在文本里出现几次，v2 数
**语法树里真正被引用的节点**（`ast.Name`(Load) / `ast.Attribute` / `ast.alias`）。
同一棵树、同一批 515 个顶层公开函数：

| | v1（字面） | v2（ast） |
|---|---|---|
| 零引用（含路由/装饰器/`__all__`） | 39（其中 route 33） | 66（route 37 / exported 18） |
| 真正可疑 | **6** | **11** |
| 其中 v1 **看不见**的 | — | **6** |
| 全跑一次耗时 | **约 1.6 分钟**（每个名字把三棵树重读一遍：107 个文件 × 515 名字） | **2 秒**（一遍建索引，之后查表） |

（route 从 33 变 37 是量具差异：ast 也把 `X.handler` 这类属性引用算进引用，于是更多
路由落进"零引用"那一档。两者都整类豁免，不影响"真正可疑"。）

v1 看不见的那 6 条（它们 `text>0`、`refs=0`——名字**只出现在注释或字符串里**）：

| 位置 | 名字 | 只在注释里被提到的次数 |
|---|---|---|
| `knowledge/graph_store.py:787` | `add_edge` | 24 |
| `knowledge/graph_store.py:670` | `close_driver` | 2 |
| `knowledge/graph_store.py:1125` | `clear_aliases` | 1 |
| `knowledge/graph_resolve.py:609` | `resolve_entities` | 1 |
| `knowledge/library.py:443` | `forget` | 1 |
| `optimization/cross_map.py:215` | `prime_history` | 1 |

**这个方向的漏报在这个仓库特别致命**：注释与 docstring 里写函数名是这里的书写习惯
（"`verify_source_refs` 做过了"、"`close_driver()` 会重置它"），于是**越是被文档认真
描述过的函数，越容易被字面判据放过**。`add_edge` 24 次提及、0 次调用，是最干净的一例。

用 `ast` 逐个复核（只数 `Call` 节点）确认这 11 条**都真的没有被调用过**，所以 v2 在
"零引用"这个方向上没有假阳性；假阴性（同名局部变量被算成引用）理论上存在，实际这 11
条一条都没被它误伤。

### 9.2 但"零引用"仍然不等于"缺口"：精确率 5/11

11 条逐条判（写进 `scripts/acc3/unwired_baseline.json`，每条带理由与凭据）：

- **6 条有正当理由**：`reset_graph_notices` / `reset_learned_max_tokens`（测试隔离专用）、
  `is_detail_only_prompt`（用例判"这是哪一轮"）、`quote_in`（内部工具，只被 verify 的
  用例调）、`prime_history`（只在 `scripts/measure_cross_map.py` 里）、
  `thinking_options`（被导出脚本消费，产物由用例守）。
- **5 条待判（真候选，不在这一轮的边界里）**：`graph_store.add_edge` /
  `close_driver` / `clear_aliases`、`graph_resolve.resolve_entities`、`library.forget`。
  共同形状是"写接口 + 只有用例调"，与第五节那三处同源。下一轮该拿它们开刀。

所以三层判据的精度是：**"零引用"→ 11/11**（这 11 条真的没被调用过，逐条用 `Call`
节点复核）；**"缺口候选"→ 5/11 ≈ 45%**（还没逐条判完，是上限）；**"这一轮必须修"→ 0**
——两条真缺口一条已经修掉（`thinking_options`，它现在被导出脚本消费，落在上面那 6 条
"正当理由"里）、一条已经删掉（`diagnose_density`，所以它压根不在 v2 的 11 条里）。

这也是第五节那个 "39 → 6" 的真实含义：**6 条里只有 2 条是真缺口**（`thinking_options`、
`diagnose_density`），另外 4 条是"只在测试/脚本里用"的合理存在。v1 对真缺口的召回是
**5/11 ≈ 45%**：它给出 6 条，其中 5 条与 v2 重合，漏掉 6 条只被注释提到的。

### 9.3 下一轮该不该进 CI：该，但只能是**棘轮**，且要加一条反向检查

`--gate` 已经实现，读 `scripts/acc3/unwired_baseline.json`：

1. **新增即红**：任何不在基线里的零引用公开函数 → 退出码 1。
   （实测：往 `service.py` 加一个无人调用的公开函数，闸门立刻退 1 并指名 `文件:行 名字`。）
2. **理由必须有凭据**：每个基线条目写着 `evidence`（脚本 / 用例 / 生成物的仓库相对路径）。
   凭据文件被删掉而条目还在 → **红**。这一条防的是"理由消失、结论留下"，也就是第五节
   那个反例（`include_in_schema=False` 旁边那段"这一轮为什么先不导出"）消失之后没人发现。
3. **修好了要销账**：条目不再是零引用 → 打印提示（不红）。
4. `status: todo` 的条目每次都会被打出来，数量就是"还没判的账"。

**不做绝对闸门**（"零引用必须为 0"）：那等于逼人往豁免名单里塞东西，然后忘掉它。

**不做"契约-客户端对账"当闸门**：我试了两种最自然的字符串做法——`openapi.json` 的
38 条 path 与 `frontend/src/api/client.ts` 对账，一种报"18 条没有客户端"、另一种（把
`{id}`/`${id}` 归一后）报"36 条没有客户端"，**两次都不对**：路径是拼出来的
（`${BASE}/mindmaps/${id}`），字符串层面量不准。这条要成立得做模板串归一的静态分析，
不属于这一轮。它恰恰也是一个真实的洞：33 条路由被 v1 当作"框架调用"整类豁免，而
"这条路由有没有前端在调"从来没人问——本轮我拒绝再加一条路由，理由之一就是它。

**该进 CI 的是这三件事**：`find_unwired.py --gate`（棘轮）、`test_openapi_snapshot.py`
（已有）、`test_thinking_options.py`（本轮加的"生成物必须新鲜"）。都是"红得起来、且
红得有话说"的那种。

## 十、这一轮的边界与残留风险

- 改动的产品文件：`app/generation/service.py`（删一个函数）、
  `frontend/src/stores/options.ts`（改成 import 生成物）。
  新增：`scripts/export_thinking_options.py`、`frontend/src/api/thinking-options.json`
  （生成物）、`tests/test_thinking_options.py`、`tests/test_diagnostic_codes.py`、
  `scripts/acc3/unwired_baseline.json`；改写：`scripts/acc3/find_unwired.py`（判据 v2 +
  闸门）。
  **没碰** `app/agent/tools.py`、`app/knowledge/service.py`、`frontend/src/canvas/*`。
- **契约零变化**：`openapi.json` 与 `schema.d.ts` 与 HEAD 逐字节相同（7.4 的探针加过
  一个字面量成员，已完整回滚）。
- 用例读数：后端 **3259 通过 / 37 跳过**；前端 **82 文件 / 1360 用例**。
  相对 `8d2f098` 的**净增量 +4**（新增 5 + 2，删掉 3），不是 3259 与旧文档里那个
  3247 的差——那个差里还有另一条线的东西（`tests/test_few_branches.py` 等）。
- **同一条病还有一个没修的**：`DIAGRAM_TYPE_OPTIONS`。它的值域在后端
  （`AgentRunRequest.diagram_type` 的 `Literal`），标签与 `hint` 是纯表现层的，而前端
  `api/types.ts::DiagramType` 是**手写**的，与 `schema.d.ts` 的 union 之间没有任何用例
  连着。加一个图型（比如"时间轴"）同样不会红。修法与本轮同一条（值域走生成物），
  但 `hint` 得先决定放在哪一侧，所以没顺手改。
- `scripts/acc3/unwired_baseline.json` 里 5 条 `todo` 是**这一轮不判**的账，
  不是"判定它们可以零引用"。

---

# 第三轮：`DIAGRAM_TYPE_OPTIONS` 与"棘轮进 CI"

> 起点 `main` = `3dc00b2`。这一轮只做两件事，都是上面留下的账：第十节最后那条
> "同一条病还有一个没修的"，以及 9.3 里"该进 CI"却一直没进的那条。第三节是这份账本
> 的自评。三件都不改生成逻辑，`app/agent/tools.py`、`generation/service.py`、
> `optimization/repair.py` 一行没碰。

## 十一、图型：三样东西，三个源，六处红灯

### 11.1 修前：两侧**恰好一致**，而这正是最坏的状态

| 东西 | 修前的位置 | 谁保证它跟着后端变 |
|---|---|---|
| 值域 | `api/types.ts:663` 手写 `'tree' \| 'flowchart' \| 'fishbone'` | **没有人** |
| 中文名 | `stores/options.ts` 手写三个 label | **没有人** |
| `hint` | `stores/options.ts` 手写三条 | 不适用（它没有后端对应物） |
| 生成物 | `schema.d.ts:973` 的 `("tree" \| "flowchart" \| "fishbone") \| null` | `test_openapi_snapshot.py` + `npm run gen:api` |

三份字面量**逐字相同、顺序相同**，所以没有任何一条用例会红——它们比的是各自那份副本。
第七节那段话在这里逐字适用："它现在的状态是'恰好一致'"。加一个图型（"时间轴"）
同样什么都不会发生。

### 11.2 `hint` 放哪侧：**留界面**——依据不是偏好，是后端自己写下的边界

`contracts/mindmap.py:154-157` 那段注释已经把这件事判过了：

> 图型的中文名。导出文件头（`export.service.to_puml`）要用它……**只放中文名，不放
> 说明**——说明属于界面（`SourceInputs.vue` 里那四个 hint）。

（顺带一句文档漂移：那四个 hint 早就从 `SourceInputs.vue` 搬到了
`stores/options.ts`，注释没跟上。不影响行为，没改。）

所以分开判的两件事：

- **中文名往后端走**。它不只属于界面——`export.service` 把它写进 PlantUML 文件头，
  后端本来就**必须**有。前端再抄一份就是第十节那条病的原样：抄的那份不会跟着变。
  这一轮把它接上生成物（`api/diagram-labels.json`）。
- **`hint` 留在界面**。它说的是"选了它，界面上会发生什么"，后端没有对应字段、也没有
  任何生成逻辑读它。而搬过去**换不到"加图型零改动"**：新图型的中文说明总得有人写，
  差别只是写哪个文件，不是要不要写。写在后端还要过一遍 `export_openapi.py` 进
  `schema.d.ts`——那份文件现在是纯技术文档。

一句话收口：**值是契约，名是两处都要用的产物，说明是界面。**

### 11.3 链路：两条生成路，一条手工

```
app/contracts/mindmap.py::DiagramKind          ← 值域的唯一真源
    ↓ scripts/export_openapi.py（提交的快照，test_openapi_snapshot.py 守卫）
backend/openapi.json
    ↓ npm run gen:api（CI 里重生成后 git diff --exit-code）
frontend/src/api/schema.d.ts
    ↓ NonNullable<components['schemas']['AgentRunRequest']['diagram_type']>
api/types.ts::DiagramType                       ← 不再是手写并集

app/contracts/mindmap.py::DIAGRAM_LABELS        ← 中文名的唯一真源
    ↓ scripts/export_diagram_labels.py（手跑，像 export_thinking_options.py）
frontend/src/api/diagram-labels.json            ← 提交、可 review 的生成物
    ↓ import
stores/options.ts::DIAGRAM_TYPE_OPTIONS         ← 中文名不含任何字面量
    ↓
ChatDock.vue #diagram-type

stores/options.ts::DIAGRAM_TYPE_COPY            ← hint 的唯一真源（只在这里）
```

值域**没有再导一份 JSON**。理由：它已经有一条比 JSON 更早生效的路（`gen:api`
出类型、`vue-tsc` 直接用），再导一份等于同一件事有两条真源，改了一处忘了另一处时，
红的是"两个生成物不一致"而不是"契约改了"。中文名不一样——它是普通 dict，不是
pydantic 字段，`app.openapi()` 里根本没有它，唯一出口就是导出脚本。

"少写一条"靠**类型**红，不靠断言：

```ts
const DIAGRAM_TYPE_COPY: Record<DiagramType, { hint: string }> = { … };
//                        ^ 键集必须恰好等于值域
...DIAGRAM_VALUES.map(value => ({ value, label: diagramLabels[value], … }));
//                                        ^ 生成物里必须有这个键
```

### 11.4 ★ 造一次"后端多一个图型"（`timeline / 时间轴`）：六处红灯

**只改 `mindmap.py`，什么都不重跑**——最常见的人为错误：

```
$ python -m pytest -q tests/test_diagram_options.py::test_schema_union_matches_diagram_kind
E  AssertionError: frontend/src/api/schema.d.ts 的 diagram_type union 与后端 DiagramKind
   不同步。往 `contracts/mindmap.py` 加了一种图型，却只改了一侧。重新生成：
     cd backend && python scripts/export_diagram_labels.py
     cd backend && python scripts/export_openapi.py
     cd frontend && npm run gen:api
FAILED …::test_schema_union_matches_diagram_kind
```

**补跑 `export_openapi.py` + `gen:api`，"忘了"重跑 `export_diagram_labels.py`**：

```
$ npx vue-tsc --noEmit
src/stores/options.ts(78,7): error TS2741: Property 'timeline' is missing in type
  '{ tree: { hint: string; }; flowchart: { hint: string; }; fishbone: { hint: string; }; }'
  but required in type 'Record<DiagramType, { hint: string; }>'.
src/stores/options.ts(92,12): error TS7053: Element implicitly has an 'any' type because
  expression of type 'DiagramType' can't be used to index type
  '{ tree: string; flowchart: string; fishbone: string; }'.
[exit code: 2]

$ npx vitest run tests/diagram-options-source.spec.ts
× 生成物的请求侧与产物侧同宽（同一次导出，不该只有一侧变）
  AssertionError: expected [ 'tree', 'flowchart', …(2) ] to deeply equal [ 'tree', 'flowchart', 'fishbone' ]
× 每一项都在生成物的 union 里，并集也覆盖全部（少一条/多一条都红）
  AssertionError: expected [ 'tree', 'flowchart', 'fishbone' ] to deeply equal [ 'tree', 'flowchart', …(2) ]
Tests  2 failed | 3 passed (5)

$ python -m pytest -q tests/test_diagram_options.py
FAILED test_exported_snapshot_is_up_to_date
  AssertionError: diagram-labels.json 过期了。重新生成：…
FAILED test_export_has_one_entry_per_diagram_kind
  assert {'tree': '思维导…hbone': '鱼骨图'} == {'tree': '思维导…eline': '时间轴'}
2 failed, 2 passed
```

**六处红，两种性质**：`type-check` 那两条是**编译期**（"这个表缺一项"），比用例早、
也比用例硬——`npm test` 走 esbuild、不做类型检查，所以真正拦住它的是 `type-check`；
pytest 那两条是生成物过期。四条新用例的完整清单在
`tests/test_diagram_options.py`（4 条，跨语言对账）与
`tests/diagram-options-source.spec.ts`（5 条，前端内部）。

探针已完整回滚：`contracts/mindmap.py`、`openapi.json`、`schema.d.ts` 三份与 HEAD
逐字节相同（`git diff --stat` 里只剩本轮真正的两处改动）。

### 11.5 残留与销账

**已销（本轮）· `mindmap/shape.ts::shapeLabel`**：这里曾有一份三值映射
（鱼骨图 / 流程图 / 思维导图），注释自认"与后端 `DIAGRAM_LABELS` **同一份字面量**"。
当时留着的理由是"它的入参是 `ShapeKind`（走 `kindOf()` 的形状判别），不是 `diagram_type`
字段"——**那条理由仍然成立，但它只证明"判据不能改成读字段"，不证明"文案必须手抄"。**
中文名的单一源是生成物 `api/diagram-labels.json`（`stores/options.ts` 已经在读它），
而 `ShapeKind` 与它的键都出自后端 `DiagramKind`，所以 `diagramLabels[kind]` 是**同一个
键**，不是两次命名撞车。改法：import 生成物，函数体只剩一行索引。

失效形态是**用户可见**且静默的：图形类型下拉读生成物，导入结果面板那句"已导入一张 ___"
读 `shapeLabel()`。改一个中文名（后端 → `export_diagram_labels.py` → json），下拉跟着变、
面板不变；而 json 与后端有 `test_diagram_options.py` 对账、`options.ts` 侧有
`diagram-options-source.spec.ts` —— **两边全绿**。

守卫（两侧各一条，都验过红）：

- `backend/tests/test_shared_constants.py::test_shape_label_reads_the_generated_labels`：
  读 `shape.ts` 文本，断言 ① import 了生成物 ② 函数体里出现 `diagramLabels[kind]`
  ③ 函数体不含那三个中文字面量。用"只取函数体一段文本"而不是扫全文，因为全文注释必然
  在解释这三样东西，正则会被自己的说明绊倒——与同文件 `_literal_assignments` 用 `ast`
  躲开注释是同一个理由，只是 TypeScript 侧没有现成的解析器（所以配平花括号，并在
  docstring 里写明这条假设什么时候会失效）。
- `frontend/tests/diagram-options-source.spec.ts`：断言 `shapeLabel(kind)` 等于下拉里
  那一项的 `label`（**两个消费点相互相等**——比"都等于 json"更贴近用户看到的东西），
  以及源码里不再有那三个带引号的字面量。

验红实录（把 json 里 fishbone 改成"因果图"、`shape.ts` 退回手抄，两个文件都由明确
文件名备份/还原）：`AssertionError: expected '鱼骨图' to be '因果图'`。这就是"同一个
界面两处叫法不同"的字面形态。

顺带一个改善：原来那个穷尽的 `return '思维导图'` 在加第四种图型时会**静默返回错名字**；
改成 `diagramLabels[kind]` 之后，那是一句 `npm run type-check` 错误（生成物里还没那个键），
比用例早也比用例硬。

**残留 · `contracts/mindmap.py:156` 的注释漂移**：它说 hint 在 `SourceInputs.vue`，实际
早在 `stores/options.ts`。

## 十二、棘轮进 CI

### 12.1 接线

`.github/workflows/ci.yml` 的后端 job，`Test` 之后加一步：

```yaml
      - name: 零引用函数棘轮
        run: python scripts/acc3/find_unwired.py --gate
```

**依赖：零额外。** 脚本只 import 标准库（`ast` / `json` / `pathlib` / `re` /
`collections` / `dataclasses` / `argparse` / `sys`），`Install` 那步装的
requirements + pytest 已经够；它**不** import `app` 里任何模块，所以也不需要
`requirements.txt` 里那些东西真能跑起来。

### 12.2 确定性：先在干净 worktree 里对过

判据是**三棵源码树的文本**（`app/`、`tests/`、`scripts/`），不读运行中的服务、不读
数据库、不读 git 状态或工作区差异；所有路径以 `__file__` 为锚
（`BACKEND_DIR = Path(__file__).resolve().parents[2]`，`BASELINE` 相对脚本目录）。
唯一一处跨目录读的是 `evidence` 的存在性检查，锚在 `BACKEND_DIR.parent`（仓库根）。

实测：`git worktree add` 一个 detached HEAD=`3dc00b2` 的干净树，在那里跑
`--gate`，四个读数与主工作区**逐行相同**（515 / 66 / 11 / 5），退出码 0。

### 12.3 它红了会看到什么（干净 worktree 里演的两场）

**新增一个零引用公开函数**（往 `app/` 加一个只有 docstring 的模块）：

```
$ python scripts/acc3/find_unwired.py --gate
待判（5 条，它们是往年遗留、不在任何一轮的边界里）：
  · close_driver：只在测试里被调（conftest 与几个图用例）……
  · …（另 4 条）

闸门：不通过
  · 新增零引用公开函数：app/_probe_unwired.py:4 probe_orphan_function

每条新增都要么接上，要么在 backend/scripts/acc3/unwired_baseline.json 里写清为什么
它可以零引用。
[exit code: 1]
```

**理由消失、结论留下**（删掉 `quote_in` 那条的 evidence 文件）：

```
闸门：不通过
  · 基线条目 quote_in 的豁免理由已经不在了：backend/tests/test_verify_states.py
[exit code: 1]
```

两种红的形状不同、话也不同：前者说"你新增了一条"，后者说"你删掉了一个理由"。

### 12.4 ★ 五条 `todo` 逐条判：下一轮该不该动、动的话是什么活

| 条目 | 下一轮 | 具体是什么活 |
|---|---|---|
| `graph_store.close_driver` | **该**（十分钟） | **删半句 docstring，不接退出钩子**。产品进程由 uvicorn 管，驱动随进程消失；为"让人手动调一个函数"加 shutdown 钩子，是把测试便利包装成生产必要。把"供测试与进程退出调用"改成"测试隔离专用"，`status` 转 `accepted`（与 `reset_graph_notices` 同类）。先确认它只是 `driver.close()` 的薄包装、没有 flush 语义。 |
| `graph_store.add_edge` | **该**（判完大概率删） | 它被注释提到 **24 次、调用 0 次**——文档在描述一个不存在的调用。两条路选一条：把那些注释改指向 `add_edges_bulk`，或把它做成 `add_edges_bulk([x])` 的薄封装并让注释指向它。风险低（`knowledge/` 下的模块级函数，不是路由）。 |
| `graph_store.clear_aliases` | **该**——五条里最像**真缺口**的一条 | 别名是模型解析实体时写进图库的**推断产物**，模型会猜错，而猜错之后**没有任何界面或接口能改**。这不是"没人用"，是"没人能做"。要么给 `DELETE /api/knowledge/aliases/{name}`，要么落库时就不写推断别名（那要改生成链）。先量：`graph_resolve` 一次跑写多少别名、错一次影响几张图。 |
| `graph_resolve.resolve_entities` | **不该"接上"**（降级可见性） | 形状是"公开的单条入口，产品只用内部批量入口"——这是**可见性标错了**，不是接线缺口。改成 `_resolve_entity` 私有、用例改调批量入口，或确认它对第三方是 API 的一部分、写进 `__all__`。与基线里 `quote_in` 那条同一个判法（上一轮已判"不是接线缺口"）。 |
| `library.forget` | **该**——与 `clear_aliases` 并列的真缺口 | 产品里没有"删掉这份材料"的入口，而 `prune` 有自己的回收路径。活是一条完整链路：repository 删除 → `source_refs` 悬空处理 → 材料列表的删除按钮。**设计前置**：删掉一份被 3 张图引用的材料，那 3 张图的 `source_refs` 怎么办？现在有 `SOURCE_TEXT_UNAVAILABLE` 这条诊断，"删了但引用还在"是**有表达方式**的——这条判完才知道是一小时的活还是一天的活。 |

**为什么一直没人动？分两半，比例是 3:2。**

- `close_driver` / `resolve_entities` / `add_edge`：**不影响用户**。产品行为完全正确，
  它们是内部可见性标错与文档漂移。没人动不是不知道——是**知道但不值得**。
- `clear_aliases` / `library.forget`：**没人知道它们存在**。它们的症状只有用户撞上
  之后才会被想起（"模型把这个实体认错了，我能不能改回来"／"这份材料传错了，我能不能
  删"），而**产品里没有那个按钮**——于是用户不会去搜"为什么删不掉"，他只会认为
  "这个产品不支持"。**没有抱怨的功能缺失，比有抱怨的更难被发现。**

**所以棘轮的价值要重新定位。** 它不主要在"以后不新增"——新增本来就会在 PR 里被
看见。它的价值是**让存量可见**：上一轮之前，这 5 条躺在 `graph_store.py` /
`library.py` 里，**没有人有一份清单**；现在每次 push 都会把"这些是还没判的账"连同
理由重新说一遍。

**一个每次都说一遍、说了不改也不会红的清单，恰好是这类账最合适的载体**：它不逼人往
豁免名单里塞东西，但它让"我们欠着 5 件事"这件事无法被忘记。这也解释了它为什么必须
是棘轮——绝对闸门会在这 5 条上立刻红，而其中 3 条的正确处置是"降级可见性或删半句
注释"，和两条真缺口混在同一个红灯里，红几次之后就没人看了。

## 十三、五条 `todo` 的处置（已落地）

读数变化：`515 / 66 / 11 / 5` → **`522 / 65 / 8 / 0`**（顶层公开函数 / 零引用 / 可疑 /
待判）。新增的 7 个公开函数全部被产品接线或由路由装饰器豁免，所以「真正可疑」从 11 掉
到 8，`todo` 清零，`--gate` 仍然通过。基线里那五条：三条删掉（它们已经不在零引用清单里
了），两条改 `accepted`。

| 条目 | 处置 | 落在哪儿 |
|---|---|---|
| `graph_store.close_driver` | 删掉"进程退出也调它"那半句（函数 docstring + 模块 docstring「驱动是模块级惰性单例」那段）；**不接退出钩子**。`status=accepted`，与 `reset_graph_notices` 同类 | `app/knowledge/graph_store.py` |
| `graph_store.add_edge` | 改成 `add_edges_bulk([EdgeInput(...)]) > 0` 的薄封装，**删掉独立的 `CYPHER_ADD_EDGE`**；模块 docstring 里三处提及改成"两者共用一份校验"。`status=accepted` | 同上 |
| `graph_resolve.resolve_entities` | 写进新的 `__all__`（`resolve_entities` / `find_merge_candidates` / `MergeCandidate`），docstring 写清"产品没有触发点，这是设计" | `app/knowledge/graph_resolve.py` |
| `graph_store.clear_aliases` | 接线成 `DELETE /api/knowledge/aliases`；同一族加了 `GET /api/knowledge/aliases`（清单）与 `DELETE /api/knowledge/aliases/{alias}`（删一条，`graph_store.delete_alias` 是新的）。前端在知识库抽屉里加「实体别名」折叠区 | `app/knowledge/graph_store.py`、`app/knowledge/router.py`、`frontend/src/App.vue` |
| `library.forget` | 接线成 `DELETE /api/knowledge/library/{source_id}`（`shelf.delete_entry`），被引用时先回 409、`?force=true` 才真删；前端列表项加删除按钮 | `app/knowledge/{library,shelf,router}.py`、`frontend/src/App.vue` |

### 13.1 两处 §12.4 的判断与代码不符（照原样做会做错）

**一、`add_edge`「被注释提到 24 次」——那 24 次里 19 次不是它。**

`find_unwired.py` 的 `text` 列数的是 `\badd_edge\b` 在 `app/` 下的字面出现次数（减掉
定义行），确实是 24。但逐处读一遍：`agent/ops.py` 的 `Literal["add_edge"]`、
`_EDGE_ID_OPS`、`tools.py` 的 `ToolSpec(name="add_edge")`、`15461`/`16506`/`16535` 那几处
"边 op 分流"的注释——它们说的是**生成层的边 op 名**，与 `graph_store.add_edge` 是两个
东西。真正指这个函数的只有 6 处（`graph_store.py` 的模块 docstring 3 处 + `EdgeInput` /
`_prepare_edge` / `add_edges_bulk` 的 docstring，`graph_ingest.py:109` 1 处）。

所以 §12.4 给的两条路里，"把那些注释改指向 `add_edges_bulk`"在 19 处上是**改错**：改完
之后 `ops.py` 与 `tools.py` 的注释会指向一个与 op 语义无关的函数。第二条路（做成薄封装）
是对的，而且顺带把那条独立的 `CYPHER_ADD_EDGE` 删掉了——现在"单条"与"批量"只有一份
`_prepare_edge`、一份 `MERGE`，"单条写得进去、批量写不进去"在结构上不再可能。

**二、`resolve_entities`「产品路径走 `graph_resolve` 内部的批量入口」——不成立。**

`find_merge_candidates` 的引用者只有 `resolve_entities`（`graph_resolve.py:632`），而
`resolve_entities` 的调用者只有用例。也就是说产品里**没有任何一层**走过别名生成：不是
"公开单条入口、内部用批量"，而是整条编排没有触发点。§12.4 把"不该接上"这个结论判对了
（理由另外成立：它要 Neo4j + embedding 额度，`dry_run=False` 还是不可逆写），但归因写
错了。处置按"可见性"做：在 `__all__` 里声明它是外部/运维入口，docstring 写清产品不调用。

### 13.2 `forget` 的 a/b/c：选 **a（硬删）+ 引用前置确认**

§12.4 的设计前置是"删掉被 3 张图引用的材料，那 3 张图怎么办"。三个选项逐一过：

- **b 软删**（标记删除、清正文、留 `source_refs`）：它买的只有一句更好的诊断措辞（"这份
  材料被删了"而不是"来源不存在"）。代价是一次 schema 迁移、一遍读路径过滤、外加"重新
  上传同一份 `content_hash` 撞上墓碑"的复活规则——而 `SOURCE_TEXT_UNAVAILABLE` 已经把
  "删了但引用还在"表达清楚了。为措辞加一条链路，不划算。
- **c 拒删**（被引用就不许删）：在没有"解除引用"入口的世界里等于"永远删不掉被引用的
  材料"，是死路；用户撞上的形态会是"我要删它，它说先解除引用，而解除引用的按钮不存在"。
- **a 硬删**：与"删除"的语义一致、可逆性由用户承担、零迁移。缺的那一块是"用户不知道
  后果"——所以补的不是软删，是**把代价提前说清**：未引用直接删；有引用先回 409
  （`detail.cited_by`），确认框里写明"那 N 张图的引用会显示「来源原文不可用」，且无法
  恢复"，确认后才 `?force=true`。

**数据完整性 vs 用户预期，这里用户预期赢**，依据是删除动机的分布：绝大多数删除是"传错
了 / 重复了 / 不该传"，这类材料通常 `cited_by == 0`（直接删）；真有引用的那一批，用户
已经在确认框里看到了代价，而且**请求删除是显式意志**。硬删之后导图不坏：`source_refs`
是值对象、没有外键，节点与边一条不少，只是那条引用查不到原文——`tests/test_knowledge_delete.py`
用一张真存的图钉着这一点（`force` 删完，图里 `source_refs` 仍在，`GET` 原文 404）。

### 13.3 用同一把尺子重新判：还剩几条"用户撞上才发现"

§12.4 的尺子是"3 条不影响用户、2 条没人知道存在"。销账之后重新量，**还剩两条，而且
第一条比原来那五条都重**（第二条在下文就地销账，见该条末尾的「事后订正」）：

1. **实体消解没有产品入口——"合并"这个能力在产品里等于不存在。** 这一轮把"删错别名"
   补上了，但别名**只由 `resolve_entities` 产生**，而它没有路由、没有脚本、没有自动
   触发。现在的状态是：删除入口有、清单入口有、产生入口没有。用户撞上的形态不是"我改
   不了别名"，而是 **"跨材料同名不同写法永远合不起来"**——`neighbors("市场监管总局")`
   返回空，与"这两份材料确实无关"逐字节相同。这是本文件第一节到第十一节反复在说的同一
   类哑失败，只是换到了合并这一层。做它比"接线"重：一次要 Neo4j + embedding 额度、要
   长任务的进度反馈、还要一个"候选预览 → 确认"的两步界面。**下一轮第一顺位。**

   **订正：这一条已经落地。** `3c12933` / `98a5309` 把候选预览、确认合并、长任务进度与
   界面入口一起接上了（`app/knowledge/router.py` 调 `graph_resolve.resolve_entities`，
   用例 `backend/tests/test_alias_preview.py`）。状态与判据在 `docs/BACKLOG.md` 第 4 条；
   本文件只留"当初为什么判它重"的理由，不再维护状态。
2. **"上传了但没生成过"的材料不在库里，删不掉。** 入库发生在生成时，所以用户"刚传错
   一份材料、还没生成"时，它既不在知识库列表里，也没有删除入口——而这类材料恰好是删除
   动机最强的一类。修法要么改入库时机（上传即入库，但那时还没有 `source_id` 契约），
   要么给"本次会话的来源"一个清空入口。它比第 1 条轻，但症状一样：**没有按钮，于是用户
   认为"这个产品不支持"。**

   **事后订正：这半句"没有删除入口"是错的，本条已在同一轮里销账。**

   `SourceInputs.vue` 里每行一直有「移除」、标题栏一直有「清空来源」
   （`stores/sources.ts` 的 `remove` / `clear`），知识库抽屉也有删除。所以"给会话来源
   一个清空入口"这条路不成立——它已经在了。

   真正缺的是**语义**，不是按钮：「移除」只把材料从这一轮生成里拿掉，库里那份不动。
   于是"传错一份、生成过一次"之后，用户在材料列表里点掉它，"我的材料"里消失的只有这
   一行——知识库里那份还占着 200 份上限的一个位置，而且因为 `content_hash` 是唯一键，
   下次传同一份文件会被**静默复用**（连上一次的标签一起，见
   `knowledge/library.py` 的「复用已有记录时只更新 `last_used_at`」）。

   处置：`stores/sources.ts` 增加 `inLibrary(item)`（判据 `source.id !== item.key`
   ——`remap` 只把入库成功的临时 id 换成持久 id，行身份 `key` 从不动）与
   `forget(key)`（库里有就调既有的 `DELETE /api/knowledge/library/{id}`，再移出会话；
   库里没有就只移出，**不发请求**——对一份从未入库的材料发 DELETE 只会拿到 404，然后把
   "它早就不在"显示成"删除失败"）。界面上「移除」的语义不变，另给一个「删除」并写明
   差别；两份内容相同的材料共享一个持久 id 时，删一份会让另一份退回"未入库"。

   `改入库时机` 那条仍然否决，理由与 `knowledge/remember.py` 的 docstring 相同：上传
   那一刻没有"用户要不要用它"的信息，而 `prune` 的保留依据是 `last_used_at`、上限是
   硬性的 200 份。

另有两处**不算缺口**、记一笔：`library.prune`（"不自动调用"，库会一直涨——不过现在有
手动删除，最坏情况可控）；`chunk_vectors` 删材料后不清（按 `content_hash` 缓存，重传
命中是收益，膨胀不可见）。

---

# 第五轮：可达性——"代码全在、链子全通、就是没有入口"的最后一层

这一轮**没有改产品代码**，只补了一道查法。它的读数、逐条判定、闸门的验红验绿实录、
以及"它答不了什么"的自评，都在 `docs/REACHABILITY.md`；这里只留它与前面几轮的关系。

## 十四、前面几轮缺的那一格

```
后端函数零引用                     → scripts/acc3/find_unwired.py（§12）
client.ts 函数零调用               → frontend/scripts/find_unwired.mjs
后端有路由、client 一行都没有       → frontend/scripts/check_route_coverage.mjs
★ 函数被调用了，但那个组件从入口到不了 → frontend/scripts/check_reachability.mjs
```

前四轮撞出来的缺口（本文件 §零 与 §七、§八 那五处）全是"链子没接"：路由写了没人调、
函数写了没人用、文档在说一个不存在的调用。它们有一个共同前提——**链子的起点是接得上
的**。这一档问的是那个前提本身：

> 某个 `.vue` 里写着完整界面（按钮、事件、调 client 函数），而它没有被任何可达组件
> import。用户永远看不到。而第二道闸会因为"那个 client 函数被调用了"而全绿。

## 十五、读数

`src/**` 69 个节点（`.vue` 23 / `.ts` 46），可达 66，不可达 3——**全部是 c 档**（镜像
类型 `api/types.ts`、生成的 `api/schema.d.ts`、编译期断言 `api/contract-check.ts`）。
`a`（完整界面没人 import）**0 条**，`d`（只在 tests 里被 import）0 条，`e`（死代码）
0 条。23 个 `.vue` 全部可达，入边可逐条审计（`--edges`）。

所以这一轮**没有发现新的缺口**。这是结果，不是"没查"：读数的形状（69 / 66 / 3）就是
下次复核的依据，而 `a` 档的判据（`.vue` + 事件绑定/`data-test`/`emit`/`props`/调 client）
在干净 worktree 里用一个新造的完整界面组件验过会红。

## 十六、它答不了什么——以及为什么这件事该说清

它算的是 **import 图**，不是**渲染条件**。`AliasResolver` 被 `v-if="aliasConfigured"`
包着（部署没配 Neo4j 时它永远不渲染），`StructureCheck` 自己判可见性，`AgentPanel` 在
`<details>` 里——这三种在可达性图上都是"可达"。

本仓库的渲染条件 335 处，其中**字面常量 0 处**，绝大多数绑在运行时状态上（`busy` /
`error` / `length` / `generating`）。所以"查 `v-if` 恒假"这一档补出来是空的；"开关状态"
那一半只有"能力开关"这一小类可补，而它恰好是"看不见是设计"的那一类（假的分支有降级
文案）。细则与三档判断在 `docs/REACHABILITY.md` §6.2。

八道闸全绿能说的一句话是：**从后端函数 → 路由 → client → 组件 → 入口，这条链上没有
任何一环是"零引用"或"import 不到"的。** 它不能说"这个功能用户看得到"。缺的那一半不是
静态判据能补的——把前者的绿灯当成后者的证明，就是把它当成了它不是的东西（§12.4 说
棘轮的价值在"让存量可见"，这一档同理）。


