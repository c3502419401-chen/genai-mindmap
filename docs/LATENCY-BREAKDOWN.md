# 一次生成到底慢在哪：分段实测与开关建议

> 实测机器：本机（后端 `run-backend.bat` 那套凭据与代理）、2026-11-26。
> 复算脚本：`backend/scripts/measure_latency_breakdown.py`（进程内直调工具，不占游客配额，
> 但**会真花模型额度**）；端到端那条用 `backend/scripts/_probe_latency.py` 走 HTTP + SSE。
>
> 这一份回答三个问题：**那 4 秒外部检索占多少**、**按章切跑了几次**、**17.5 秒入图还在不在**，
> 以及"哪几个开关的组合是耗时最省而质量不掉"。

## 一、结论先给（三个能直接执行的判断）

1. **大头不是检索，是输出量。** 端到端 139.5 秒里，模型往返（结构 + 说明 + 节点流式）
   占 **110 秒**；外部检索 14.8 秒（冷）、后续同主题 **0.03 秒**（`external_cache` 命中）。
2. **一次 create 有 6.8 秒是纯白烧**（已修）：库里非空 + 请求不带材料时，注册表把
   `rag_query` 给了 `create`，而 system 里那句"先调用 rag_query 检索"让模型在外层先查
   两次（3.52s + 3.32s），查到的片段**进不了生成工具的任何参数**——工具内部自己会按支/
   按章检索。修法见 `tools.build_registry` 与 `service._RAG_INTENT_RULES["create"]`。
3. **最该动的旋钮是 `MINDMAP_NODE_SCALE`。** 同一份代码、同一份材料：开 = 205 节点 /
   72.6 秒 / 说明均长 75 字；关 = 115 节点 / 41.0 秒 / 说明均长 **89 字**。
   耗时 **-43%**，而每段说明反而更长（节点少，说明预算更集中）。

## 二、端到端 139.5 秒的账（真跑一次，SSE 帧时间线）

主题「碳中和的主要方面」，生产开关组合（见第四节），库里有 86 份来源，外部检索**冷**。

| 段 | 秒 | 说明 |
|---|---|---|
| 门禁 / 配额 / 建运行 | 0.15 | 含游客 cookie 与免费次数检查 |
| 外层 agent 两次 `rag_query` | **6.8** | 3.52 + 3.32，**纯白烧**（见第一节第 2 条） |
| 外部检索（`external_search` stage 开始→结束） | **14.8** | 补 8 份；`external_cache` 冷启动 |
| 骨架 + 证据 + 结构（到第一个 `node_grow`） | 41.5 | 骨架 1 次 + 结构 1 次（含入图/检索） |
| 节点流式生长（`node_grow` × 113） | 39.8 | 模型一次写完 160 个节点的输出量 |
| 说明（`detail` stage） | **29.2** | 两半并发，墙钟取大的那一半 |
| 收尾（快照 / 校验 / 落库） | 3.4 | |
| **合计** | **139.5** | 工具自身 `mindmap_generate` 报 125.2 秒 |

读数来自 `tool_end` 的 `result_summary`：`data.calls = {skeleton, structure, detail}`、
`data.retrieval.seconds`、`data.external.seconds`、`data.detail.halves`。
这几个字段本来就在，只是从来没有一行汇总——所以"为什么慢"以前只能靠猜。

## 三、工具级三组对比（同一脚本、同一主题、外部检索缓存命中）

脚本直调 `mindmap_generate`，所以**不含**外层 agent 的 6.8 秒与 SSE 开销。

| 组 | 总 | 骨架 | 结构 | 说明 | 节点 | 层 | 说明均长 | 秒/节点 |
|---|---|---|---|---|---|---|---|---|
| production（全开） | 72.6 | 1.34 | **45.04** | 25.31 | 205 | 5 | 75.0 | 0.354 |
| `--no-scale`（关 NODE_SCALE） | **41.0** | 0.94 | 22.30 | 17.55 | 115 | 4 | **89.2** | 0.357 |
| `--no-graph`（关入图） | 51.2 | 1.69 | 27.97 | 21.28 | 135 | 4 | — | 0.379 |

**三组之间不要横向硬比"开关省几秒"**：模型每次给的分支数与节点数本来就会变，而
"秒/节点"在 0.354–0.379 之间高度一致——**耗时 ≈ 节点数 × 0.35 秒**。所以任何"关掉某个
开关省 N 秒"的结论，都要落到"它让节点/调用少了几次"上才成立。

结构那一次（45 秒）是单次调用的大头，它的墙钟由**输出量**决定（全树 label + 引用），
不是由输入决定：把材料从 8 份换成 1 份、节点数不变时它不会明显变快。

## 四、你问的那四个数

**① 那 4 秒外部检索占多少？关掉差多少？**
冷启动 **14.8 秒**（8 份材料、含 DeepSeek 搜索那一路），占端到端 139.5 秒的 10.6%。
同一个主题第二次生成走 `external_cache`，实测 **0.035 秒**——所以"4 秒"只在**第一次
问这个主题**时存在，重复主题接近免费。关掉它省的是这 15 秒，代价是整棵树没有外部依据
（`GENERATION_FROM_EXTERNAL` 那条诊断消失），质量掉得比时间省得多。

**② SECTION_SLICE 在 6 章材料上跑了几次、并发度多少？**
这批外部材料切不出章（诊断 `SECTION_SLICE_UNAVAILABLE`），这一次是 **0 次额外调用、
0 开销**。它只在下载/上传的材料**自带目录**时生效，那时结构那一段从 1 次变成「每章一次」，
并发度与详情见 `docs/SECTION-SLICE.md`。换句话说：本条开关的代价不是常量，而是
"这份材料有没有目录"的函数。

**③ GRAPH_INGEST 的 17.5 秒现在还在吗？**
在，但**只在材料第一次入图时**。本次三组的诊断都是 `GRAPH_INGEST_DONE` +
`GRAPH_OUTLINE_USED`（入图那一步的出口有两种：这批材料已经抽过 → "零成本跳过"；
首次 → 真抽取并报"耗时 X 秒"），而 `GRAPH_INGEST_DONE` 的总账从总耗时里反推只有
亚秒级——说明这次走的是跳过那一条。历史首次抽取实测 **+17.5 秒**
（`run-backend.bat` 第 23 行的口径）。它买的东西是**骨架不再用模板词**：没有图谱时
一级分支是 8 次采样里的通用框架（`docs/GRAPH-OUTLINE.md` §5）。所以它是"一次性成本 +
永久收益"，重复生成不该关。

**④ 哪一段是真正的大头，砍它值不值？**
结构（一次调用，45 秒）+ 说明（两半并发，25 秒）= 七成。两者都由**节点数 × 每节点字数**
决定。砍节点数立刻见效且**说明更饱满**（75 → 89 字）；砍说明长度省时间但直接掉质量。

## 五、开关建议

| 开关 | 实测代价 | 建议 |
|---|---|---|
| `MINDMAP_NODE_SCALE=1` | 节点 115→205，耗时 41→72.6（**+77%**），说明均长 89→75，且诊断出 `TREE_OVER_READABILITY`（205 > 80） | **要快就关**。关掉后图更小、说明更长、耗时 -43%。它买的是"覆盖更广"，卖的是"读不动 + 等更久" |
| `MINDMAP_OPTIMIZATION_CROSS_MAP=1` | 收尾附加，不改进这次产物 | **关**（纯省） |
| `MINDMAP_EXTERNAL_KNOWLEDGE=1` | 冷 14.8s / 热 0.03s | 保留。没材料时它是唯一依据；重复主题靠缓存免费 |
| `MINDMAP_EXTERNAL_DEEPSEEK_SEARCH=1` | 第 6 源，8 份里的 6–7 份来自它（联网最深、最慢） | 想再省 5–8 秒时关它，保留另外 5 源 |
| `MINDMAP_GRAPH_INGEST` + `GRAPH_EXTRACTION` | 首次 +17.5s，之后 ≈ 0 | 保留（它决定骨架是不是模板词） |
| `MINDMAP_SECTION_SLICE=1` | 有目录的材料：结构从 1 次 → N 次 | 只对"自带目录的长材料"开；碎片化外部材料上它是 0 开销也 0 收益 |
| `MINDMAP_DETAIL_RETRIEVAL=1` | 全塞时 0；逐节点检索时每节点一次本地检索 | 保留（它是"每条说明都有出处"的前提） |
| `MINDMAP_DETAIL_LENGTH=free` / `DETAIL_SCALE=1` | 说明段输出量 = 全部节点 × 60–150 字 | 要快：给它一个上限（80 字量级），说明段近乎减半 |
| `MINDMAP_VERIFY_AFTER_GENERATE` | 已关 | 保持关 |

**"耗时最省而质量不掉"的组合**（按收益排序）：

```
关 MINDMAP_NODE_SCALE        -31.6s（-43%），说明均长 +14 字
关 MINDMAP_OPTIMIZATION_CROSS_MAP  若干秒，产物不变
关 MINDMAP_EXTERNAL_DEEPSEEK_SEARCH  -5~8s（仅冷启动那一次）
保留 EXTERNAL / GRAPH_INGEST / DETAIL_RETRIEVAL / SECTION_SLICE
```

代码层这一次已经做掉的一刀：**create 不再把 `rag_query` 给模型**（-6.8 秒，零质量代价，
理由见第一节第 2 条）。

## 六、改图与展开这两条路（同一套口径）

| 动作 | 实测 | 检索 |
|---|---|---|
| `edit`「把『工业过程减排』这个标题改短一点」 | **4.2s** | 无检索（1 次模型往返） |
| `edit`「把这一支按材料补充一下，补两个子节点」 | **15.1s** | `rag_query` **7.3s**（合法：用户点名要材料） |
| `expand`（节点没有 `source_refs` 的小图） | **7.1s** | 无检索；`node_expand` 内部 2.3s（一次模型调用） |

所以"改节点也去查资料"不是一个恒定行为：**同一份代码，模型查不查取决于那句话**。
这正是这一轮把指令写清的理由（`service._RAG_INTENT_RULES` 的 `edit` / `expand` 两条）：
默认不要检索，"按材料补充 / 核对原文"才查——一刀切禁止会挡掉后者的合法用法。

另外 `expand` 的代价与图有关：节点带 `source_refs` 时，`_expand` 会从库回查原文并按
这一支的标题链检索一次（`search_parent`，含远程 rerank），几秒量级；没有出处的图不付这笔。

## 七、怎么复算

```
cd backend
python scripts/measure_latency_breakdown.py --label production --out data/_latency_production.json
python scripts/measure_latency_breakdown.py --label no-scale --no-scale
python scripts/measure_latency_breakdown.py --label no-graph --no-graph
python scripts/_probe_latency.py create "碳中和的主要方面"     # 端到端（含外层 agent 与 SSE）
```

`measure_latency_breakdown.py` 缺省复刻 `run-backend.bat` 的开关（含 Neo4j 凭据，
否则 `GRAPH_INGEST` 与 `=0` 完全一样，量出来的数是假的）。
