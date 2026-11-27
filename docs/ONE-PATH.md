# 只剩一条生成路：删掉 `full` 形态与整树生成

> **⚠️ 本文写于 2026-11-16。引用前先读这段：结论有一半已被后来的提交推翻。**
>
> **被推翻的那一半**：`create` 上注册的生成工具**不是** `mindmap_generate_deep`（分片），
> 而是 `mindmap_generate`（整树一次成型）。2026-11-18 的 `bc3494e` 把"两条实现、一条注册"
> 定了下来——`_ALLOWED_BY_INTENT["create"] = ("mindmap_generate",)`，分片改挂在**非产品意图**
> `create_deep` 下（代码、规格、70 余个用例全部保留，服务"一次响应写不下 200+ 节点"的大图，
> `service.run` 永远不会把意图解成这个名字）。依据是 `runs` 表里 16 次真实运行 `sources` 全为 0
> ——分片的核心优势（材料覆盖 30.5% vs 整树 5.1%）需要材料才成立，而代价每次都要付。
> 取舍与实测见 `docs/ONE-CALL.md`，被 `backend/tests/test_one_path.py` 钉住。
<!-- claims-ok: create_registers_generate 订正块：明说这句只在 11-16 到 11-18 成立 -->
> 所以下面"新建导图只剩分片这一条路"**只在 2026-11-16 到 11-18 之间成立**；
> 现在的口径写在 `AGENTS.md` 第 49 行与 `TEAM-GUIDE.md` 第 4 节。
>
> **仍然成立的那一半**：`MINDMAP_SOURCE_MODE` 这个形态开关、`full` 的四份提示词、
> `sources_within_context` 预检、`_material_manifest` 全家确实都删了，`app/` 里没有任何一处
> 再 `getenv` 它（`test_source_mode_switch_is_gone` 钉着）。回来的整树生成**照旧不吃整份正文**，
> 它只吃检索命中（`_retrieve_branches`，每支各一次）。
>
> 本文其余部分（为什么删、代价、留下的东西、那处自我评估）作为当时的决策记录保留原样。

2026-11-16。这次改动**只做减法**：把 `MINDMAP_SOURCE_MODE=full`（正文整份拼进
prompt）与整树一次成型的 `mindmap_generate` 一起删除，`create` 意图从此只有一条
生成路——`rag` 语义的分片生成（`mindmap_generate_deep`）+ 按支检索。

`docs/RAG-DECISIONS.md` 里提到旧行为的文字**保持原样**：那份文档是检索评估套件的
语料，它被 case 标注到的那些章节的正文指纹登记在 `backend/scripts/eval_cases_manual.py`
的 `MANUAL_SOURCE_HASHES` 里，改动那些章节评估集就跟着变。决策记录因此放在这里。

## 删了什么

代码（`backend/app/`）：

| 位置 | 删掉的东西 |
|---|---|
| `generation/service.py` | 形态开关的四个常量（`SOURCE_MODE` 前缀的 `ENV` / `FULL` / `RAG` / `DEFAULT`）与形态判定函数 `source_mode`（含非法取值的告警去重集合）；有来源那一份整树提示词与它的思维限定句（`SOURCED_DETAIL…` / `RAG_SOURCE…` 两组） |
| `agent/tools.py` | `_generate()`（450 行）、主题预检索全家（`_retrieve_topic` / `_TopicEvidence` / `GENERATE_*`）、整树形态的输入渲染（`sources` 数组拼装 + 超预算二分裁剪，含 `SOURCE_TRUNCATED_HINT`）、材料清单渲染全家（`_material_manifest` / `_material_title` / `_material_type_label` / `_material_scale` / `MATERIAL_MANIFEST_*`）、`_GenerateGrowStream`、`GenerateArgs` |
| `agent/contracts.py` | `sources_within_context`（"来源正文合计装得进上下文"的预检——正文不进 prompt 之后它量的是一个不存在的对象）；`sources_not_too_many` 只剩"份数上限"一种文案 |
| `agent/service.py` | `_intent_rule` 不再判形态 |
| `build_registry` | `create` 只注册 `mindmap_generate_deep`；形态不再参与裁剪 |

文档：`SOURCE-REFERENCES.md` / `API.md` / `FLOW.md` / `FEATURES_AND_OWNERS.md` /
`GENERATION-PIPELINE-NOTES.md` / `README.md` / `START_HERE.md` / `TEAM-GUIDE.md` /
`AGENTS.md` 里对这两个东西的描述。

**两份文档刻意没改**：`docs/AGENT_PLAN.md` 与 `docs/ARCHITECTURE.md` 也是检索评估套件的
语料——它们被 case 标注到的章节正文指纹登记在 `backend/scripts/eval_cases_manual.py` 的
`MANUAL_SOURCE_HASHES` 里（与 `docs/RAG-DECISIONS.md`、`docs/PLAN.md` 一起，共四份）。
改到那些章节整套手写标注就失效，`tests/test_retrieval_eval_manual.py` 会直接报错。所以这两份
文档里仍然写着"`mindmap_generate` 走 `validate_generated_content`"这类旧描述——它们现在是
**语料**，不是文档；准确描述以本文与上表为准。

## 为什么删

**`full` 的唯一优势是覆盖率，而它正是分片要买的东西。** 同一份 14,120 字材料、
8 条一级分支、纯 BM25（`backend/scripts/measure_deep_slicing.py`）：

| 路 | 覆盖率 |
|---|---|
| `full` + 整树（正文全在 prompt） | **100%** |
| `rag` + 整树（一次 `top_k=5` 主题预检索） | 5.1% |
| `rag` + 分片（每支各检索一次） | **30.5%** |

`full` 拿到 100% 靠的是"整份正文进 prompt"。这条路同时带来三件长期成本：

1. **两套提示词、两套失败方式**。同一条 `create` 意图下，模型看到的是两份互不兼容
   的输入形状（`sources` JSON 数组 vs 材料清单），改一处必须同时改两处；历史上
   四次"只改了两三条路径"，单点测试全绿而链路是断的（见 `AGENTS.md`）。
2. **上下文是硬边界**。`full` 下"能不能接这份材料"取决于它装不装得进窗口，
   于是要一条 422 预检、一套"精简/分批"的文案，以及"25 万字材料被拒"这种与产品
   无关的失败面。
3. **一个只认两个值、实际只有一个值在用的开关**。`rag` 早在 `a84e763` 之前就是
   默认值，`full` 在生产配置里已经不再出现；留着它就是让下一个人以为还能切回去。

## 代价（这次是实质改动，不是重构）

- **覆盖率从 100% 降到 30.5%（`full` → 分片）**。这是有意的交易：`full` 的 100%
  只在"整份正文进 prompt"时成立，而那条路已经不存在；在"正文不进 prompt"这个
  前提下，同一条路上的分片比整树高六倍（30.5% vs 5.1%）。
- **整树收尾的两步诊断曾经不再跑在新建图上，现已接回分片收尾**：
  `optimization.check()`（单子节点 / 深度上限 / 同级重名，有来源时加第三层引用
  校验）与 `annotate_nli()`（引用与原文的语义蕴含打分）原先长在 `_generate` 的
  收尾里，随它一起消失过一段。现在两者都接在 `_generate_deep` 的
  `verify_source_refs` 之后（`tools.py` 的「结构体检」与「语义层」两段）：
  `check()` 无条件跑（单次 60–130 µs），`annotate_nli()` 走 `to_thread` 且要
  `MINDMAP_NLI_ENABLED` 为真（成本实测见 `docs/SOURCE-REFERENCES.md`）。
  分片路**比整树更需要** `SINGLE_CHILD`：某一支模型没展开时，树上留下的正是
  「一个父节点只有一个子节点」，而整树一次成型时模型总把每支要点数给满。
- **模型不再收到材料清单**（标题、字数、类型、"知识库中另有 N 份"）。那份清单是
  `_generate` 的输入渲染，分片路给模型的是骨架那一次的章节标题行与每支的检索命中。
  库来源也不再参与新建图的引用校验集合：分片的可检索来源只有请求内材料。
- **没有材料时不再有"一次调用出整棵树"的快路**。简单主题现在也走"骨架 + N 次展开"，
  模型调用次数从 1 次变成 `1 + N (+补查)`。实测本机（脚本化模型、纯 BM25）分片的
  墙钟在 0.1 秒量级，真实模型下一次初次生成是 20–30 次调用、十几秒——比整树慢，
  这是"每次只写一层"换来稳定性与覆盖的定价。
- **`scripts/measure_deep_slicing.py` 只剩一组**（原来 A/B/C 三组）。A/B 两组的
  数字留在本文档与 `docs/RAG-DECISIONS.md` 第 5 节；脚本仍然复现得出 30.5%。

## 留下的东西（逐项确认过）

分片生成的限额（初次 80 节点 / 4 层，扩展 1000 / 8 层）、骨架看材料目录
（`heading_outline` 覆盖率 ≥60% 才喂）、按支检索（每支各检索一次、只带自己的命中）、
限额补查（每支 1 次、每次运行 3 次）、**材料过短退化**（<300 字 → 视为无材料 →
外部检索 / 纯主题）、**外部知识检索**（默认开）与它的词面相关性门、引用逐字校验、
<!-- claims-ok: create_registers_generate 这句说 EVIDENCE_UNUSED 的产生点，不是生成路 -->
`Node.detail`、`expandable` 三态、runs 表、`EVIDENCE_UNUSED`（现在只剩分片与
`_expand` 两条路）、`DETAIL_PROMPT` / `CONTENT_FREEDOM_CONTRACT` /
`DETAIL_NO_SOURCE_RULES`（纯主题那条退路：没有可检索材料时拼在骨架与每次展开的
system 之后）。

后三段原来是 `_generate` 的入口行为（短材料退化、外部检索、无来源提示词），
这次**整段搬进了 `_generate_deep`**：唯一入口必须自己做完这些，否则"传了一份 8 字材料"
会在新路上变成"一份 8 字材料被当成语料"。

## 一处诚实的自我评估

**这次删除的代价可能大部分是"账面"的。** `full` 的 100% 覆盖率是真的，但它依赖
"整份正文进 prompt"这个已经被放弃的前提；在现行默认配置（`rag`）下，`full` 那段
代码从来没有被真正执行过——`a84e763` 之后 `rag` 就是默认值，而生产部署没有把它
翻回去。也就是说：**没有一个真实任务"在 full 下能做、现在做不了"**，因为 full 下
从来没有跑过真实任务；能举出来的只有"如果当初选了另一条路"的反事实。

真正沉没的能力是上面"代价"一节里那三条（结构诊断/NLI、材料清单、单次快路），
它们与 `full` 无关，是 `_generate` 这个**入口**带的，随入口一起消失。要拿回来，
应当单独做（例如在分片收尾也跑一次 `check()`），而不是把整树生成请回来。
