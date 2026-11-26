# 三处"代码在、用例绿、产品里没接上"——根因、修法与实跑读数

> 来源：`docs/ACCEPTANCE-EVIDENCE.md` 的 B1 / B3 / B4。那一轮只报不改（"未改代码，逐条报"），
> 这一轮把三条修掉，并把"同类还有多少"扫了一遍。
>
> 基线：`main`，从 `94e30ed` 起改。后端 `run-backend.bat`（`MINDMAP_LLM_MODEL=deepseek-chat`，
> 8000），前端 5173，本地库 `backend/data/mindmaps.db`。真跑脚本在 `backend/scripts/acc3/`。
>
> 修前的数字（那一轮的读数，保留在这里做对照）：后端 **3221 通过 / 37 跳过**，
> 前端 **82 文件 / 1360 用例**；修后后端 **3242 通过 / 37 跳过**（+21 条新用例），前端不变。

---

## 零、三条一句话

| 缺口 | 是刻意留的吗 | 修法 | 修完的实跑读数 |
|---|---|---|---|
| `sources` 的 id 被换掉 | **不是**——换 id 是设计，只给 `mapping` 是遗漏 | SSE `sources` 帧补上**最终清单**（`SourceIdentity`，不带正文），`mapping` 保留 | 真 SSE：`sources` 帧的 1 个 id 与 `done` 帧 `content.source_usage` 里的那个 id 一致 |
| `/api/knowledge/search` 是桩 | **不是刻意**，是"没来得及"（`TODO(F12/F13)` 从脚手架那天就在） | 走生成链路同一套：`_library_sources` 选候选 → `search_parent` 打分 → `touch` 记账 | 真库 4913 条向量：5 条命中、`method=hybrid`、**1.1–1.7 s**（首次冷缓存 9.7 s） |
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
| `/knowledge/search` | 只有 `TODO(F12/F13)` 与 `implemented=false`，**没有一句"为什么先不接"** | **纯遗漏**（脚手架遗留） |
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
