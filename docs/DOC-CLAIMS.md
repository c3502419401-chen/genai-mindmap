# 文档断言与代码一致：一张双向的「事实 → 反句」表

> 机器在 `backend/scripts/check_doc_claims.py`，用例在 `backend/tests/test_doc_claims.py`，
> CI 上是 backend job 的第三步。本文只讲**怎么用**与**为什么这么设计**。
>
> 本文件自己被排除在扫描之外（`SELF_DOC`）：它整篇在引用那些错句当例子，扫它等于自指。

## 一、它防的是什么

一天之内，同一类错误撞了五次，五个都是**文档说错了代码现在做的事**：

| # | 文档说 | 代码实际 |
|---|---|---|
| ① | `START_HERE`：`mindmap_generate` 已删除、只剩分片 | `_ALLOWED_BY_INTENT = {"create": ("mindmap_generate",)}`（活的那条） |
| ② | `knowledge/search` 仍是桩（四处） | `implemented=True`，混合检索在跑 |
| ③ | 没有登录/权限、接口裸奔（两处） | `auth` 全套 + `GateMiddleware` + 401 都在 |
| ④ | 开发骨架 / 最小演示闭环（三处） | 已上线，`deploy/` 里 unit + nginx + install.sh 齐全 |
| ⑤ | 画布只有一套紧凑树布局 | `Direction` 有四种朝向 |

这些句子**不会让任何用例变红**：用例钉的是代码，错的是关于代码的话。人工清一遍能清净，
下一轮又会漂——所以闸门要判的不是"文档里有没有某句话"，而是"**文档说的是不是代码现在做的事**"。

## 二、表怎么加一行

一行就是一个三元组：

```python
(事实探针, 事实为真时全仓禁止的句子正则, 事实为假时禁止的句子正则)
```

**双向是关键。** 反句只在事实为真时生效。`create` 真注册整树生成时，禁的是"只剩分片"；
哪天把它换成分片，禁的立刻变成"`create` 只有 `mindmap_generate`"。这不是"某句话不许写"，
而是"一句话与代码现在做的事不一致"。

在 `RULES` 里加一条 `Rule`，四个字段必填：

```python
Rule(
    name="knowledge_search_implemented",          # 事实名：豁免注释里写的就是它
    evidence="backend/tests/test_knowledge_search_route.py::"
             "test_unrelated_query_returns_empty_but_still_implemented",
    kind="contract",                              # contract 必须给用例名；observation 只给文件
    holds=_stub("SearchResponse"),                # 读事实（返回 bool）
    expect=lambda facts: f"SearchResponse.implemented = {(facts.stubs or {}).get('SearchResponse')}",
    when_true=(...),                              # 事实为真时禁止的句子
    when_false=(...),                             # 事实为假时禁止的句子
)
```

三条自查，缺一条这道闸门就会变成装饰：

1. **`holds` 必须读代码，不能写字面量。** 写 `lambda facts: True` 就是硬编码。
   `tests/test_doc_claims.py::test_the_probe_reads_the_file_not_a_constant` 专门防这个：
   它给探针喂两份假代码树，要求读出两个不同的值。
2. **`when_true` / `when_false` 只禁一侧**（开关一族）。A 族两侧都写是刻意的——那是双向。
3. **换个方向也得有样例。** 加了规则就去 `test_doc_claims.py` 的 `SAMPLES` 里补一句；
   没补的话 `test_every_rule_has_a_sample_and_its_own_side_is_armed` 会红。

### 开关默认值：加一行 = 什么都不用写

`env_default:<变量名>` 这一族是**自动生成**的：`ast` 找出 `X_ENV = "MINDMAP_..."` 加零参
`*_enabled()`，在**清空所有 `MINDMAP_*` 与凭证**的子进程里真的调一次，读出"代码现在的默认值"。
文档里凡在这个变量名附近说"默认开/默认关"，一律与它比对——**全量**，不是抽验两项。

上一轮的人工核法是"抽验两项，其余没核"。这一族一次把 49 个开关全钉住，而且**不需要为它们
写任何句子**：变量名就是锚点，代码翻一次，禁句跟着翻。

## 三、五类探针，和"门禁到底算不算真生效"

| 探针 | 读什么 | 为什么不能更简单 |
|---|---|---|
| ① `_ALLOWED_BY_INTENT` | `ast` 读字面量 | — |
| ② `build_registry(intent).names()` | 受控子进程真的调一次 | 只看 ① 会把"表里写着、`_TOOL_SPECS` 里没有"判成真；①② 必须一致 |
| ③ 桩端点 | `X(..., implemented=<bool>)` + `openapi.json` 的 path 对表 | 只扫 `implemented=False` 会把同名普通字段也算进来 |
| ④ 开关默认值 | 清空环境后调 `*_enabled()` | 从注释里抄"默认关"就是人写事实，闸门立刻失效 |
| ⑤ 门禁 | `main.py` 有 `add_middleware(GateMiddleware)` **且** `gate.py` 的 `__call__` 里有 401/403 分支 | 见下 |

**第 ⑤ 条为什么是静态的。** 候选三条：a 查 `add_middleware(GateMiddleware)`；b 查 `auth_router`
有没有 `include_router`；c 跑 `TestClient` 打受保护端点断言 401。

- b 不足：路由挂上不等于有门禁（`auth/router.py` 自己写着"这一批不拦任何请求"）。
- c 最真，但它把闸门从"读文本"变成"跑应用"：`TestClient` 会触发 startup，结论开始依赖
  外部 Neo4j、模型服务、磁盘上的库文件。这道闸门要在 CI 的干净 checkout 上与本地逐字节
  一致，不能有这个依赖。
- 行为层已经有运行时用例钉着：
  `tests/test_auth.py::test_the_gate_is_now_closed_for_anonymous_callers` 真的打了受保护端点。
  分工是**行为归用例，接线归这里**——而这条事实的 `evidence` 就写着那个用例名，用例被删掉
  闸门会红。

所以 ⑤ **是纯静态的**。唯一"跑代码"的是 ② ④ 的受控子进程：它 `import app.*` 但不连库
（`has_library=False`）、不发网络请求，环境按 `tests/conftest.py` 的 `CREDENTIALS` / `BEHAVIOUR`
清空（脚本**读** conftest 的清单，不复制——测试侧的隔离只有一个权威）。

## 四、豁免怎么用

引用旧说法的地方（`ONE-PATH.md` 的订正块、`PLAN-REWORDING.md` 的「原文」块、
`NODE-*` / `USER-GUIDE` 里"旧版写的是…"的段落），在**同一行或上一行**写：

```markdown
<!-- claims-ok: create_registers_generate 历史留档：这一块引的是 11-16 的旧说法 -->
```

三条硬规矩，都是刻意的：

- **必须带事实名**，`*` / `all` 不接受 —— 它不是通配，是"这一句为什么可以是旧说法"。
- **事实名必须真实存在**：写错名字会红（豁免一个不存在的事实 = 豁免了一条永远不会
  被检查的句子）。
- **理由不能为空**。

这三条抄的是 `unwired_baseline.json` 的教训：那里每条基线都写理由与 `evidence`，
证据文件被删也红——否则豁免会烂成一张"别查我"的白名单。

表格行（以 `|` 开头）要把注释放**行尾**：另起一行会打断 markdown 表格。

另外两种放行是**语境**判的，不需要注释，但会在输出里单独列出（可见，不静默）：

- **否定语境**（`不是` / `并非` / `不再` / `别写`…，命中点前 12 字内）：这个仓库的文档里
  有一大批**纠错句**——"这是一套已上线的系统，**不是**开发骨架"。它与禁句逐字相同而
  意思相反，判红就等于把对的话判成错的。
- **历史语境**（`曾经` / `旧版` / `已删`…，命中点前 40 字内）：只给开关默认值一族用。
  那一族在文档里大量以"它曾经以 `X`（默认关）的形式挂在代码里，现已删除"出现。

## 五、怎么跑

```powershell
cd backend
python scripts/check_doc_claims.py            # 每条事实的现值 + 全部命中
python scripts/check_doc_claims.py --gate     # 闸门：命中 / 证据缺失 / 探针失败 → 退出码 1
python scripts/check_doc_claims.py --facts    # 只打印事实现值（文档里那句"凭什么"）
```

命中行的形状是 `文件:行号:命中句:期望事实`：

```
· [冲突] docs/ONE-PATH.md:12: > 所以下面"新建导图只剩分片这一条路"…
    事实 create_registers_generate = create → ['mindmap_generate'], …
    凭什么：backend/tests/test_one_path.py::test_create_intent_offers_only_the_single_call_generator
```

`[冲突]` 与 `[文档过期]` 是两种不同的东西，见下一节。

## 六、边界：它假设"代码对、文档漂"

反过来（文档对、代码是 bug）时，这道闸门会要求改文档去迎合 bug —— 那是它最危险的失效
方式，也可能让一个 bug 被固化成契约。两条防线，都是机器判据而不是纪律：

**① 每条事实带 `kind` 与 `evidence`，矛盾时先看证据。**

- `contract`：必须给出**钉住这条事实的运行时用例**（`路径::用例名`）。闸门会用 `ast`
  校验那个 `def` 真的存在；用例被删掉就红。报错文案是**冲突**——
  「代码才是 bug 就改代码；契约本身变了就先改用例与 evidence，再改文档」。
- `observation`：没有用例背书（部署件、画布朝向、开关默认值），矛盾时才默认改文档。

这就是"文档说默认关、代码默认却开了，而那是个 bug"那一侧的处理方式：那条事实的
`kind=observation`，闸门只会说"两处不一致"，而改哪边由人看证据定——但**证据列在那里**，
不是"我们会小心"。

**② 探针本身可证伪。** `tests/test_doc_claims.py` 把每个探针喂一个假的代码树，断言
"代码变了，探针读出来的值跟着变"（`test_the_probe_reads_the_file_not_a_constant`、
`test_the_direction_probe_counts_what_is_in_the_file`、`test_deploy_probe_follows_the_files`）。
缺了这一层，探针可以硬编码 `True` 而永远绿，闸门就退化成装饰。

用户提的两条里，**两条都要**：① 决定"矛盾时谁说了算"，② 保证"读的确实是代码"。
少了 ① 会固化成 bug，少了 ② 会固化成空气。

## 七、这一轮验证过什么

- **验红（端到端，真实仓库）**：往 `START_HERE.md` / `USER-GUIDE.md` 临时注入四句错的，
  四条分别红在对应的事实上（`create_registers_generate` / `four_directions` /
  `env_default:MINDMAP_PARENT_RETRIEVAL` / `knowledge_search_implemented`），退出码 1。
- **代码翻回去自动换禁句**：把 `_ALLOWED_BY_INTENT["create"]` 改成只剩分片，
  闸门不再禁"只剩分片"，转而抓住了 `docs/FLOW.md`「分片不注册到 `create`」、
  `docs/INPUT-EXTRACT.md`「`create` 意图不注册它」、`docs/PLAN-VS-IMPLEMENTATION.md`
  「`create` 只有 `mindmap_generate`」。**这是这套设计的核心，已验。**
- **每条规则各有一条断言**：`test_doc_claims.py` 参数化跑全部规则（8 条手写样例 +
  49 条开关自动生成），每条都断言"它自己那侧的样例句真的会被抓到"。

## 八、它顺手抓到的东西

落地当天 `--gate` 报出 20 处命中，其中 **8 处是真漂移**（不是"引用旧说法"，是现在时
的错话），已按最小改动修正措辞：

| 文件 | 原来 | 现在 |
|---|---|---|
| `docs/API.md` | `knowledge/search` "返回空列表与 `implemented: false`" | 混合检索，`implemented: true` |
| `docs/API.md` | "当前无身份认证，仅限本机开发" | 身份层已落地（游客 / 登录 + 门禁，匿名 401） |
| `docs/PROJECT-STATUS.md` | "`POST /api/knowledge/search` 仍是桩" | 已接（2026-11） |
| `docs/ARCHITECTURE.md` | "没有用户认证、权限隔离…" | 没有多租户权限隔离与生产日志策略（认证/上传已落地） |
| `docs/RAG-DECISIONS.md` | 父子切块"默认关、未接入" | 默认开、已接入（`MINDMAP_PARENT_RETRIEVAL` 缺省 `1`） |
| `docs/USER-GUIDE.md` | "画布仍然只有一套紧凑树布局" | 一套算法，朝向四种（右 / 左 / 下 / 双向） |
| `NEXT-PLAN.md` | "2 条桩" | 1 条（只有 `agent/tasks`） |

另外 12 处是**关于过去的正确陈述**（订正块、验收留档、修复报告的标题），
加 `<!-- claims-ok: … -->` 而不是改内容。

### 改文档还有一道连带责任

`docs/` 里有四份材料被 `scripts/eval_cases_manual.py` 的 `MANUAL_SOURCE_HASHES` 钉着：
**已标注章节正文的 sha256**（口径见 `evaluate_retrieval.annotated_hashes`）。改到标注过的
章节，`tests/test_retrieval_eval_manual.py` 会当场报错，要求"重读那一章、复核区间与问法、
再把新指纹写回"——它不静默跳过，因为静默跳过会让"标注失效"看起来像"指标掉了"。

这一轮改到三章（`AGENT_PLAN.md` 的「兼容、测试逐文件迁移与数据库」、`RAG-DECISIONS.md`
的「3 评估后决定不做的」、`ARCHITECTURE.md` 的「安全与部署边界」）。核过的结论：受影响
case 的问法与 `relevant` 锚点都不在改动的句子上（`ARCHITECTURE.md` 那条 `[passage]` 的
`focus` 在章节另一处；`RAG-DECISIONS.md` 那条 `[chapter]` 的答案是"出示顾虑"那一段，
未动），所以只写回新指纹，区间与问法不动。

**所以改文档之后要跑全量，不只是跑这道闸门**：闸门管"话与代码一致"，指纹管"人读过的那段
还读得懂"。

## 九、不打算做的事

- **不做"文档里不许出现某个词"的词表闸门。** 那会把"引用旧说法来纠正它"也判红，
  而这个仓库最值钱的几句文档恰恰是纠错句。
- **不读运行时、不连库、不打网络。** 需要这些的判据留给定 `pytest` 的用例。
- **不追求完备。** 反句表只禁"人想到要禁的说法"；新增一类漂移要手动加一行。
  这不是 bug，是这张表的定义。

## 十、台账：从待办快照到带判据的台账

台账在 `docs/BACKLOG.md`（11 条），机器在 `check_doc_claims.py` 的 `parse_backlog` /
`check_backlog` / `_backlog_rules`，用例在 `test_doc_claims.py` 末尾那一组。

### 10.1 为什么要给它上判据

同一份账本，三种内容的可信度完全不同：

| 内容 | 可信度 | 实测 |
|---|---|---|
| 问题诊断 | **可信** | 五条描述逐条对得上代码与历史，连最刁的那条在写下时也是真的 |
| "该做什么" | **低** | 五条处方里一条被实测否掉——它建立在"模型会给你 15 条"这个未验证的推测上，一量落在 0.7% |
| "做没做" | **不可信** | 三条已完成还挂在待办上 |

诊断对得上，是因为它**写在代码旁边**（有行号、有读数）；"做没做"会漂，是因为它**只有一句话**。
结论很直接：**判据在代码里，状态就自动对得上；没判据的条目永远会漂。** 所以入表规则只有
一条硬的——**没有判据的条目不许进表**。这句话第一次跑就咬到了我自己：`LABEL-CLIP-VERDICT`
§六 第 4 条「已落库的残句回溯治」被判据检查拦住（它连一个可量阈值都立不住），于是它留在
原文档，没进表，理由写在 `BACKLOG.md` §六。

### 10.2 四态，和与态绑定的三种判据

三态（待做 / 已做 / 不做）装不下「离线侧确认、真模型侧没跑」：压成"已做"会让下一轮以为
结案，压成"待做"会让已经验过的那半重做一遍。所以有第四态 **半做**。

判据不是自由文本，**形态由状态决定**：

| 状态 | 判据是什么 | 机器怎么验 |
|---|---|---|
| 已做 | 它做完了的**证明** | 锚必须现在就在（`def` 真的在 / 脚本真的在） |
| 半做 | 同上，只覆盖已验的那一半 | 同上，且**必须带触发条件**（另一半卡在哪） |
| 不做 | 它该被**重新考虑的条件** | 可量阈值（数字 + 单位），或一条钉住"不做"的用例 |
| 待做 | 它做完了**会长什么样** | 锚可以暂时不存在——先写判据，再派人做 |

「不做」必须带触发条件是照 `BRANCH-CAP` §5.2.2 的形状抄的：那里判"不改"时给的是
"`runs` 里一级分支 >10 的比例过 10% 再回来（现在 0.7%，样本 150 次）"，不是"以后看情况"。

### 10.3 那个循环问题：探针读代码，锚只看证据

看起来是自指：状态写在 md 里，而"状态对不对"要读代码。拆开就不自指——**三件事分开判**：

1. **状态 ↔ 事实**：`probe` 只读**代码事实**（`description` 的文本、`Node` 有没有 `origin`、
   `clip_label` 调不调 `_clip_index`、`router` 有没有调 `resolve_entities`、基线里还有没有
   `status=todo`）。它不读 md，所以不存在自指。
2. **判据在不在**：`_anchor_candidates` 只验锚存在性——那个 `test_` 函数还在不在、那个脚本
   还在不在。
3. **"这一行属于哪一条"**：由块边界定（`parse_backlog` 切块，`Rule.scope` + `Rule.block`
   把它交回 `scan`），不由正则猜。误报的空间在这里被结构性消掉——否则十二个块里的"待做"
   会互相命中。

**两种红，话不同**，这是这一节最要紧的地方：

```
[文档过期] docs/BACKLOG.md:48: - **状态**：待做        ← 块内禁句：事实为真时禁"待做"
[台账] 第 1 条 写待做，但探针 branch_arg_deprecated 读出来是**真**——状态写错了
[台账] 第 2 条 是已做，但判据锚一个都不在：…（用例被删或脚本改名？这是「失去判据」，不是「没做」）
```

第二句和第三句不能合并：**用例被删掉 ≠ 功能没做**。合并了，下一轮就会把已经做完的活
重做一遍——这正好是这台账本要治的那个病。

至于"用例被删"的先例：`git log --diff-filter=D -- backend/tests/` 里四条，全部是**功能删掉、
用例跟着删**（`test_fold_tree.py` / `test_generate_evidence.py` / `test_source_mode.py` /
`test_stream.py`）；反向的"用例删了、功能还在"一次也没有过。所以这条防线是为将来设的，
不是凭空加的——而且它一旦触发，`unwired_baseline.json` 的教训（理由消失、结论留下）说明
这里必须红。

`probe: none` 的五条（7–11）**不假装能判**：它们的状态由验收数据、跨人决策、还没写的闸门
决定。`--facts` 把它们单列，可见，不静默。**别把 6 条的可信度读成 11 条的。**

### 10.4 这一轮验过什么（三次端到端，都是真实仓库）

| 验什么 | 怎么动 | 读数 |
|---|---|---|
| `--gate` 通过 | — | 54 条事实（48 + 6 条台账），退出码 0 |
| **验红：状态改错** | 把第 1 条的"已做"改成"待做" | 两条红同时出现：`BACKLOG.md:48` 块内禁句命中 + `[台账] 写待做但探针为真` |
| **验红：代码翻回去** | 把 `branches` 的 `description` 改回"一级分支数量上限" | 探针翻成 `False`，`backlog:1` 立刻改禁「已做」族，抓到 `BACKLOG.md:48` 的状态行 |

第二次验红是这套设计的核心：**禁句随代码翻，不是随人记得翻。** 两次实验之后代码都改回
（`git status` 里 `tools.py` 无残留），并且那次翻转被写成了用例
`test_the_backlog_rule_flips_the_forbidden_sentence`——不靠人记得再做一遍。

### 10.5 它与另外 118 份 md 是什么关系

`git ls-files '*.md'` 写下时是 119 份（`docs/` 下 109）。台账再加一份，读者的第一个问题是
"该看哪份"。

**判断一：`BACKLOG.md` 是唯一入口，但不是唯一内容。** 每条只留状态、判据、出处；为什么
这么判、量到了什么、放弃了哪条替代路线，全留在原文档。搬过来就会得到一份会腐烂的摘要，
而摘要烂得比原文快。所以它不是"第二十四份会腐烂的文档"，是**一份只有 11 行会变、每行
都有机器判据的索引**。

**判断二：分级该建，而且必须是机器可判的分级。** 手写目录是这个仓库里最先腐烂的东西
（`docs/TODO.md` 与 `NEXT-PLAN.md` 都已经过期，而它们本该是入口）。可判的分级是三件事：
每份 md 头顶一行 `tier:`（`entry` / `reference` / `archive` / `ai-only`）、`entry` 那几份
从 `README.md` 出发 2 跳内可达、`ai-only`（给 agent 读的判据说明，如本文件）不进人的入口。
本轮不做的原因只有一个：它要改 109 份文件的开头，属另一件事。**但它已经作为第 10 条进表，
带着判据（`scripts/check_doc_map.py --gate`）和触发条件（md 数过 130 或第二个人问"该看哪份"）
——不是一句"以后再说"。**

这就是"带判据的台账"与"待办快照"的差别：快照会说"文档地图该建了"，台账会说"它建成的样子
是这样一个脚本、触发条件是这条阈值"。
