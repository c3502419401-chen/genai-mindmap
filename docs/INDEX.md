# 文档索引：人读前两节，机器读第三节

这个仓库的文档不是"写完就归档"的，它们**在高速协作中被持续修订**（最近七天里动过的
占三分之二）。所以真正的问题不是"有几份"，而是**"我现在这个疑问该看哪一份"**。

本文件按读者切三刀：

| 节 | 给谁 | 是什么 | 会不会腐烂 |
|---|---|---|---|
| 一、从哪儿开始 | 人 | 推荐路径，十来份 | 几乎不会：它只写"去哪"，不列清单 |
| 二、按问题查 | 人 | 真实的疑问 → 该读的那份 | 这份最容易被替换成"更常被问的问题"，改它比改清单便宜 |
| 三、全量登记 | 机器 | 现算生成，`check_doc_map.py --write` 维护 | 不会：生成物 + `--gate` 钉着 |

第三节**不要手改路径与题目**（那是现算的），只改两列：`档`（`entry` / `reference` /
`archive` / `ai-only`）与 `archive` 那行的 `替代物`。档的定义与"为什么不给每个文件加
`tier:` 文件头"，写在 `backend/scripts/check_doc_map.py` 的 docstring 里。

`entry` 的四条承诺由闸门判：不是空的、从 `README.md` 出发两跳内可达、不含 `ai-only`
的文档、每份都真的存在。

---

## 一、从哪儿开始

| 你要干什么 | 读哪份 | 为什么是它 |
|---|---|---|
| 第一次进这个仓库，想把它跑起来 | [README.md](../README.md) | 60 秒跑起来、三道闸的命令、功能状态表、目录导览 |
| 想知道"它现在到底能做到什么" | [PROJECT-STATUS.md](PROJECT-STATUS.md) | 现状总览，**比任何人的记忆都权威**；`START_HERE.md` 是它的易错点版 |
| 想知道一次生成从头到尾发生了什么 | [FLOW.md](FLOW.md) | 端到端流程，标着每一段的代码位置 |
| 想知道某个功能实现了没有 | [FEATURES_AND_OWNERS.md](FEATURES_AND_OWNERS.md) | 逐功能的实现日期、能力与端点，其余文档引它为准 |
| 想知道还剩什么事没做 | [BACKLOG.md](BACKLOG.md) | 台账：每条一句状态、一句机器判据、一句出处 |
| 新人上手与分工 | [TEAM-GUIDE.md](../TEAM-GUIDE.md) | 目录树、A/B 分工、两条协作纪律 |
| 要动几个人共用的文件 | [FILE-OWNERSHIP.md](FILE-OWNERSHIP.md) | 同一批文件别两个人同时动；2026-11-07 那次冲突就出在这里 |
| 要部署上线 | [DEPLOY.md](DEPLOY.md) | systemd + nginx 一条命令装完，含这台机器的已知坑 |
| 要教一个用户怎么用 | [USER-GUIDE.md](USER-GUIDE.md) | 用户视角：从装好到做出一张有用的图 |

## 二、按问题查

这一节来自**真实撞过的三次"该看哪份"**（找"记忆是给谁的"、找"鱼骨为什么不该体检"的
原始判断、想知道接口缺口那 12 条从哪读起），加上几类反复被问的。答案不止一份时，
**第一份是结论，后面是判据与实测**。

| 你要查的是 | 看哪份 | 一句话 |
|---|---|---|
| 记忆是给谁的、怎么实现、该不该做 | [RESEARCH-AGENT-MEMORY.md](RESEARCH-AGENT-MEMORY.md) → [MEMORY-PROPOSITION.md](MEMORY-PROPOSITION.md) → [EDIT-NO-RETRIEVAL.md](EDIT-NO-RETRIEVAL.md) | 先是 LightMem 调研的取舍，再是"观察→命题"的两级压缩，最后是"改图时该查不该查"的实测 |
| 鱼骨图为什么不该进结构体检 | [DIAGNOSTIC-TWO-SIDES.md](DIAGNOSTIC-TWO-SIDES.md) → [FISHBONE-CATEGORIES.md](FISHBONE-CATEGORIES.md) → [CHECK-INTERFACE-GAP.md](CHECK-INTERFACE-GAP.md) | 判据分两侧（生成时能发现 / 事后检查看得见），鱼骨类别可定而不可退化的理由，以及那 12 条缺口卡在哪一层 |
| "代码在、用例绿、产品里没接上"这一类的全部形状 | [WIRING-GAPS.md](WIRING-GAPS.md) → [CHECK-INTERFACE-GAP.md](CHECK-INTERFACE-GAP.md) → [REACHABILITY.md](REACHABILITY.md) | 三处真缺口 → 12 条接口缺口 → 组件从入口到不了的那一档 |
| 某份文档说的话还算不算数 | [DOC-CLAIMS.md](DOC-CLAIMS.md)，以及被怀疑那份的**文件头订正块**（如 [ONE-PATH.md](ONE-PATH.md)） | 凡"已删除 / 仍是桩 / 默认关"三类断言，先跑 `python scripts/check_doc_claims.py --facts` |
| 某个开关的默认值、有几条路由、几个用例 | 现跑，别读文档 | `check_doc_claims.py --facts` 自己会数；会漂的数一律不写进文档（[BACKLOG.md](BACKLOG.md) 第 12 条） |
| 某个决策当时为什么这么定 | [PLAN.md](PLAN.md) 的 Q1–Q36 → [HANDOFF.md](HANDOFF.md) | 里程碑与验收门里带着决策记录；跨人确认过的在交接文档 |
| 生成链路为什么切四步、为什么删了又恢复整树 | [ONE-CALL.md](ONE-CALL.md) → [ONE-PATH.md](ONE-PATH.md) | 当前主路径的实测与红线判定，以及那次删除的决策与订正 |
| 哪份属于哪一档、为什么 | 本文件第三节 + `check_doc_map.py` 的 docstring | 四档的定义与"不设 `dead`"的理由都在那个 docstring 里 |
| 要给 AI / agent 立规矩 | [AGENTS.md](../AGENTS.md)、[PROMPT-IMPERATIVES.md](PROMPT-IMPERATIVES.md)、`.agents/skills/*/SKILL.md` | 仓库级规范、提示词里的祈使句、三个可复用的改代码套路 |

---

## 三、全量登记

<!-- doc-map:begin -->

| 档 | 路径 | 题目 | archive 的替代物 |
|---|---|---|---|
| ai-only | `.agents/skills/add-api-endpoint/SKILL.md` | 新增一个后端接口 | — |
| ai-only | `.agents/skills/add-retrieval-stage/SKILL.md` | 往检索链加一个阶段 | — |
| ai-only | `.agents/skills/change-map-schema/SKILL.md` | 改导图 JSON 结构 | — |
| ai-only | `.github/pull_request_template.md` | 这段代码做什么 | — |
| ai-only | `AGENTS.md` | AGENTS.md | — |
| entry | `README.md` | 基于生成式 AI 的思维导图系统 | — |
| entry | `START_HERE.md` | 起步说明：这是一套已上线的系统，不是开发骨架 | — |
| entry | `TEAM-GUIDE.md` | 给组员的阅读说明 | — |
| reference | `docs/ACCEPTANCE-EVIDENCE.md` | 未验收项的实跑证据（甲/乙/丙三组） | — |
| reference | `docs/ACCEPTANCE-METRICS.md` | 三条量化指标的实测：口径、样本、分数、与计划书的差距 | — |
| reference | `docs/ACCEPTANCE-REPORT.md` | 三条量化指标验收报告（定稿） | — |
| reference | `docs/ACCEPTANCE-WALKTHROUGH-2026-11-28.md` | 成品验收：用真浏览器从头走一遍（2026-11-28） | — |
| reference | `docs/AGENT-DEMO.md` | 开启 agent 与现场演示指南 | — |
| reference | `docs/AGENT_PLAN.md` | Agent 化改造实施计划 | — |
| reference | `docs/AGENT_STREAM_PROTOCOL.md` | agent 生成流的节点级事件 | — |
| reference | `docs/API.md` | API 与数据契约 | — |
| reference | `docs/ARCHITECTURE.md` | 系统架构设计 | — |
| reference | `docs/ARCHITECTURE_ADDENDUM.md` | 架构增量附录 | — |
| entry | `docs/BACKLOG.md` | 待办台账 | — |
| reference | `docs/BLIND-SCORING.md` | 四维盲评：修复之后重打（20 条 × 4 维） | — |
| reference | `docs/BRANCH-CAP.md` | 一级分支条数：不设限的来龙去脉，与"按偏好"那套的设计留档 | — |
| reference | `docs/BRANCH-DIGEST.md` | 一级分支的概括角标：这一支还有多少、讲的是什么 | — |
| reference | `docs/CHECK-INTERFACE-GAP.md` | 结构体检只收树型：那 12 条鱼骨 / 流程诊断卡在哪一层 | — |
| reference | `docs/CONTRACT_V2.md` | MindMapContent v2 契约 | — |
| reference | `docs/CROSS-BRANCH-DUPLICATES.md` | 跨支重复 17 处：逐条分类、D 判据的意图、该改哪一头 | — |
| reference | `docs/DEEPSEEK-TOOL-APIS.md` | DeepSeek 工具 API 五件套：是什么，以及我们哪些已经有了 | — |
| entry | `docs/DEPLOY.md` | 部署与依赖 | — |
| reference | `docs/DETAIL-RETRIEVAL.md` | 第三段（写说明）的证据：每个节点在自己那一章内检索 | — |
| reference | `docs/DETAIL-SCALE.md` | 每段说明写多长：字数要不要随材料量伸缩 | — |
| reference | `docs/DETAIL-SENTENCES.md` | 说明那一轮的句子数下限：放开它会不会变差 | — |
| reference | `docs/DIAGNOSTIC-TWO-SIDES.md` | 两套判据：生成时能发现、事后检查看不见 —— 该搬哪些，分界线在哪 | — |
| ai-only | `docs/DOC-CLAIMS.md` | 文档断言与代码一致：一张双向的「事实 → 反句」表 | — |
| reference | `docs/DOCKER.md` | 单机容器化部署（Docker / docker compose） | — |
| reference | `docs/DRAG-ROUND3.md` | 拖动第三轮：就地输入意图、两类图的拖动语义、以及"旋转"到底是什么 | — |
| reference | `docs/EDIT-LOG.md` | 改动日志：先让"用户最常改什么"变成事实 | — |
| reference | `docs/EDIT-NO-RETRIEVAL.md` | 改图该查时查不查：5 组说法的实测与裁定 | — |
| reference | `docs/ENTITY-RESOLVE.md` | 实体消解入口设计 | — |
| reference | `docs/EXTERNAL-RELEVANCE.md` | 外部检索的词面相关性门：挡住了跑题，挡不住同形异义 | — |
| reference | `docs/EXTERNAL-SELECTION.md` | 外部检索的取数策略：从"每源保底"改成"按相关度排 + 去重" | — |
| entry | `docs/FEATURES_AND_OWNERS.md` | 项目书功能追踪与 RPeGio / 陈嘉涛分工 | — |
| entry | `docs/FILE-OWNERSHIP.md` | 文件占用约定（谁在动哪些文件） | — |
| reference | `docs/FISHBONE-CATEGORIES.md` | 鱼骨图的类别：为什么可以按主题定，以及凭什么不退化 | — |
| entry | `docs/FLOW.md` | 端到端流程 | — |
| archive | `docs/FOLD-RETIRED.md` | 折叠退休：为什么"折而不裁"最后还是把内容拿走了 | [VIEW-COLLAPSE.md](VIEW-COLLAPSE.md) |
| reference | `docs/FREE-MODELS.md` | 免费的模型档：为什么是动态的，以及实测到的东西 | — |
| reference | `docs/FULL-CONTEXT.md` | 全塞还是按支检索：一次实测与一条被改掉的提示词 | — |
| reference | `docs/GENERATE-REPAIR-SELFCHECK.md` | 生成收尾的两件便宜事：确定性修复 + 结构自检 | — |
| reference | `docs/GENERATION-PIPELINE-NOTES.md` | 生成链路的边界与流式：一轮修复记录 | — |
| reference | `docs/GENERATION-VERIFY.md` | 生成之后的核对：一次搜索能核对到什么程度 | — |
| reference | `docs/GRAPH-OUTLINE.md` | 第三档骨架：目录不可用时，用图谱定骨架 | — |
| reference | `docs/GRAPH-SCALE.md` | 图该多大：节点数要不要随材料量伸缩 | — |
| reference | `docs/GRAPH-TWO-PASS.md` | 图谱两段式：用知识图谱定节点，而不是章节骨架 | — |
| reference | `docs/HANDOFF.md` | 交接文档（需要对方知悉或确认的决策） | — |
| reference | `docs/IMPORT.md` | 导入：把别人的导图变成我们的三种图型之一 | — |
| entry | `docs/INDEX.md` | 文档索引：人读前两节，机器读第三节 | — |
| reference | `docs/INPUT-EXTRACT.md` | 输入先读成「意图 + 主题」：那一段话不该直接进 主题： 那一行 | — |
| reference | `docs/LABEL-CLIP-VERDICT.md` | label 的 16 字硬切：三处判定摆在一起之后 | — |
| reference | `docs/LABEL-JOIN-ADMISSION.md` | 压链拼标题的准入条件：「放得下」不是「读得懂」 | — |
| reference | `docs/LATENCY-BREAKDOWN.md` | 一次生成到底慢在哪：分段实测与开关建议 | — |
| reference | `docs/LAYOUT-CLARITY.md` | 布局清晰度：真图上的问题分布，以及"该用哪一档" | — |
| reference | `docs/LEGACY-FORMAT-COST.md` | 旧版 Office 格式（.doc / .ppt / .xls）的解析代价实测 | — |
| reference | `docs/LIBRARY-IN-GENERATE.md` | 库参与生成（MINDMAP_LIBRARY_IN_GENERATE） | — |
| reference | `docs/LIBRARY-MEASUREMENTS.md` | 知识库：落库与「只靠库检索」实测 | — |
| reference | `docs/LINK-SUGGESTIONS.md` | SuggestionKind.link 第一版：新加的节点该挂在谁下面 | — |
| reference | `docs/LLM-OUTLINE-ACCURACY.md` | PDF 纯文本交给 LLM 分层：准确率实测 | — |
| reference | `docs/MEMORY-PROPOSITION.md` | 观察 → 命题：两级压缩（只做"记"，不做"学"） | — |
| reference | `docs/MIXED-SKELETON.md` | 混合材料的骨架：有章节的走章节、没章节的走图谱 | — |
| reference | `docs/NEO4J-LOCAL.md` | 这台机器上的 Neo4j：怎么起、怎么确认活着 | — |
| reference | `docs/NODE-DRAG.md` | 拖动改结构：坐标仍然只有布局一个来源，拖动只表达"关系" | — |
| reference | `docs/NODE-EDIT.md` | 断关系 / 删除 / 新增节点：三个动作各自的判据 | — |
| reference | `docs/NODE-ORIGIN.md` | origin：补不补、补成什么 | — |
| reference | `docs/OFF-TOPIC-AND-FEW-BRANCHES.md` | 两处"生成出来的东西真的不对"：会议纪要跑题、2 条一级分支被判死 | — |
| reference | `docs/ONE-CALL.md` | 回到一次生成：骨架 + 每支检索 + 一次写完（→ 后接"只写说明"一段，再拆两半并发） | — |
| archive | `docs/ONE-PATH.md` | 只剩一条生成路：删掉 full 形态与整树生成 | [ONE-CALL.md](ONE-CALL.md) |
| reference | `docs/OPTIMIZATION-CROSS-MAP.md` | 跨图参照：节点向量索引实测，以及为什么没装 Milvus | — |
| reference | `docs/OPTIMIZATION-REPAIR.md` | 自动修复：只做"修法唯一"的那三条 | — |
| reference | `docs/OPTIONAL-DEPS.md` | 可选依赖：哪个脚本缺什么就跑不起来 | — |
| reference | `docs/PDF-PARSER-SELECTION.md` | PDF 解析方案选型报告 | — |
| reference | `docs/PLAN-REWORDING.md` | 计划书纠错：可直接贴进申报书的替换句 | — |
| reference | `docs/PLAN-VS-IMPLEMENTATION.md` | 计划书与实现的逐项核对 | — |
| reference | `docs/PLAN.md` | 项目执行计划 v1.4 | — |
| reference | `docs/PROFILE-ISOLATION.md` | 画像方向按导图隔离 | — |
| entry | `docs/PROJECT-STATUS.md` | 项目现状总览 | — |
| ai-only | `docs/PROMPT-IMPERATIVES.md` | 提示词里那些祈使句：三问核查、10 条判定、以及一条落地的判据 | — |
| reference | `docs/PROPOSAL-REPLACEMENTS.md` | 可直接贴进申报书的替换文字（一页版） | — |
| reference | `docs/PROPOSAL.md` | 项目申报书原文（存档） | — |
| reference | `docs/RAG-DECISIONS.md` | RAG 与召回：一轮评估记录与取舍 | — |
| reference | `docs/REACHABILITY.md` | 第四档：函数被调用了，但那个组件从入口到不了 | — |
| reference | `docs/RESEARCH-AGENT-MEMORY.md` | LightMem 调研：agent 记忆该怎么实现，我们该不该做 | — |
| reference | `docs/RESEARCH-ASR-LOCAL.md` | 语音识别（ASR）能否本地推理 —— 调研与 PoC 报告 | — |
| reference | `docs/RESEARCH-CODE-MODE-AND-ALWAYS-ON.md` | 技术调研：Code Mode 与 always-on agent 对我们意味着什么 | — |
| reference | `docs/RETRIEVAL-EVAL.md` | 检索评估基线（recall@k / MRR / nDCG / coverage） | — |
| reference | `docs/RETRIEVAL-KEEP.md` | 按分数取数：top_k 从"条数"变成"下界" | — |
| reference | `docs/SCOPE.md` | 本期承诺边界：已交付 / 本期不交付 / 长期不做 | — |
| ai-only | `docs/SCRIPT-HYGIENE.md` | 探针脚本与它周围那圈"该查没人查"的规矩 | — |
| reference | `docs/SECTION-SLICE.md` | 按章切：每支只给它自己那一章的原文 | — |
| reference | `docs/SOURCE-REFERENCES.md` | 节点来源引用 | — |
| reference | `docs/SUBAGENT-VS-SELFCHECK.md` | 每支一个子代理 vs 一次写完 + 自检：可行性验证 | — |
| reference | `docs/TEST-COST-136-NODE.md` | 136 节点那两条用例为什么慢：成本分解、判定与"该给多少超时" | — |
| reference | `docs/TEST-COST-3-CASES.md` | 三条裸奔在 5 秒线上的用例：慢在哪、判什么、修成什么 | — |
| reference | `docs/TEST-LOAD-SENSITIVITY-TABLE.md` | 负载敏感性实测表（脚本生成，勿手改） | — |
| reference | `docs/TEST-LOAD-SENSITIVITY.md` | 负载敏感用例的系统性治理：实测表、可算判据、以及该改的是验证口径 | — |
| reference | `docs/THIN-MATERIAL-BRANCHES.md` | 薄材料上骨架只给两条：现在怎么处置、复跑到了什么 | — |
| reference | `docs/TWO-ARCH-COMPARISON.md` | 同条件对比：整树一次生成 vs 逐支分片 | — |
| reference | `docs/USER-GUIDE-DELTA-2026-11-28.md` | 用户手册 × 实际界面：差异清单（2026-11-28） | — |
| entry | `docs/USER-GUIDE.md` | 用户手册：从装好到做出一张有用的图 | — |
| reference | `docs/USER-PATH-TESTS.md` | 用户路径测试：让"用户走不到"开始有红灯形态 | — |
| reference | `docs/VIEW-COLLAPSE.md` | 视图层收起：数据完整，这一屏只画得下这么多 | — |
| archive | `docs/W1_DAY1.md` | W1 开工清单（11/3 – 11/8） | [PLAN.md](PLAN.md) |
| reference | `docs/WIRING-GAPS.md` | 三处"代码在、用例绿、产品里没接上"——根因、修法与实跑读数 | — |
| archive | `docs/archive/NEXT-PLAN.md` | 下一步计划书 | [PROJECT-STATUS.md](PROJECT-STATUS.md) |
| reference | `docs/research/AGENT_MINDMAP_RESEARCH.md` | Agent 做思维导图：外部调研汇总 | — |
| archive | `docs/research/readmes/Fu-Jie__openwebui-extensions.md` | OpenWebUI Extensions | — |
| archive | `docs/research/readmes/NJUxlj__Travel-Agent-based-on-Qwen2-RLHF.md` | 基于Qwen2.5+LoRA微调+RLHF+RAG的旅游路径规划智能体 | — |
| archive | `docs/research/readmes/OpenSourceAGI__qwksearch-research-agent.md` | 🧠💻 Reimagine the Internet as Self-Organizing Mind Map | — |
| archive | `docs/research/readmes/Promptly-Technologies-LLC__mcp-structured-thinking.md` | Structured Thinking MCP Server | — |
| archive | `docs/research/readmes/XimilalaXiang__DeLive.md` | Demo | 14 ASR Providers \| Local-First AI Review Workspace | — |
| archive | `docs/research/readmes/YuChenSSR__mindmap-mcp-server.md` | Mindmap MCP Server | — |
| archive | `docs/research/readmes/cheshireyang__mindmap-mcts-skill.md` | MindMap-MCTS | — |
| archive | `docs/research/readmes/format81__TI-Mindmap-GPT.md` | 🚀 TI Mindmap GPT vs TI Mindmap Hub | — |
| archive | `docs/research/readmes/helixnow__deep-student.md` | DeepStudent | English | — |
| archive | `docs/research/readmes/jinzcdev__markmap-mcp-server.md` | Markmap MCP Server | — |
| archive | `docs/research/readmes/jmanhype__MindMapper.md` | MindMapper | — |
| archive | `docs/research/readmes/kongkongyo__Ai-Markmap.md` | ✨ 主要特性 | <a href="./README_EN.md">English</a> | — |
| archive | `docs/research/readmes/mindolph__Mindolph.md` | Mindolph | — |
| archive | `docs/research/readmes/satellitecomponent__Neurite.md` | 🌐 neurite.network 🌐 | — |
| archive | `docs/research/readmes/siyuan-note__siyuan.md` | Table of Contents | <a href="README.zh-CN.md">中文</a> | — |
| archive | `docs/research/readmes/vcmf__dim0.md` | Features | — |
| archive | `docs/research/readmes/xianjianlf2__MindGeniusAI.md` | MindGenius AI | — |
| archive | `docs/research/readmes/xiehuan123__dsh-deepread.md` | 📖 DeepRead — Make AI reading traceable | — |
| reference | `frontend/tests/README.md` | 写用例时的三条规矩（负载这块） | — |

<!-- doc-map:end -->
