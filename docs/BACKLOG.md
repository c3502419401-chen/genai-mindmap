# 待办台账

> 机器判据在 `backend/scripts/check_doc_claims.py` 的 `check_backlog()` 与 `_backlog_rules()`，
> 用例在 `backend/tests/test_doc_claims.py`。设计说明在 `docs/DOC-CLAIMS.md` 第十节。
>
> **本文件是"还剩什么事没做"的唯一入口。** 其它文档里散落的"下一轮该做…""这是真缺口"
> 一律收敛到这里；判断的**理由**留在原文档（不会变），只有**状态**搬进来（会变）。

## 一、三条入表规则

1. **没有判据的条目不许进表。** 判据在代码里，状态才自动对得上；没判据的条目永远会漂。
2. **状态只有四态**：待做 / 已做 / 半做 / 不做。三态装不下"离线侧确认、真模型侧没跑"
   这一类——把它压成"已做"会让下一轮以为结案，压成"待做"会让已经验过的那半重做一遍。
3. **不做必须给触发条件**，而且必须可量。`docs/BRANCH-CAP.md` §5.2.2 那种"比例过 10%
   再回来"是标准形状；"以后看情况"不算。

## 二、判据的三种形态（与状态绑定，不是自由文本）

| 状态 | 判据是什么 | 机器怎么验 |
|---|---|---|
| 已做 | **它做完了的证明**：一条钉住该行为的用例，或一道会红的闸门 | 锚必须存在（`路径::用例名` 里 `def` 真的在；`python scripts/… --gate` 文件真的在） |
| 半做 | 同上，**只覆盖已验的那一半** | 同上 |
| 不做 | **它该被重新考虑的条件**：一个可量阈值，或一条钉住"不做"的用例 | 必须含阈值（数字 + 单位），或有存在的锚 |
| 待做 | **它做完了会长什么样**：先写锚，再派人做 | 锚可以**暂时不存在**——闸门把它列进"待做的判据现在不在"，可见，不静默 |

反过来的那条：**判据锚消失 vs 功能消失，是两种红，报的话不同**。用例被删掉时，
闸门说"这条账目失去判据"，不说"这件事没做"——见 `docs/DOC-CLAIMS.md` §10.3。

## 三、每条带一个 `probe`

`probe` 是"状态对不对"的机器判据，取值是一个注册在脚本里的探针名，或 `none`。

**语义只有一条，别给它加极性**：`probe` 为真 ⇔ 这件事**已经在代码里**。

- `probe` 非 `none` → 状态只能是 **已做** 或 **半做**（**"已做"不许没有机器判据**）；
  事实为假就红。并自动生成一条 `backlog:<N>` 规则：事实为真时禁本块说"待做/该做/
  下一轮"，为假时禁说"已做/已落地"。
- `probe: none` → 状态只能是 **待做** 或 **不做**（这两态的判据是可量阈值或"钉住不做"
  的用例，代码事实决定不了）。闸门不假装能判，把它单列在 `--facts` 输出里，可见，不静默。

这一栏是这台账本诚实的地方：12 条里 7 条有机判，5 条只有人判。**别把 7 条的可信度
读成 12 条的。**

## 四、台账

### 1. `branches` 工具参数从"上限"改成"已废弃"

- **状态**：已做
- **probe**：`branch_arg_deprecated`
- **判据**：`backend/tests/test_branch_arg_deprecated.py::test_both_models_describe_the_argument_as_inert` —— 一条用例同时钉三件事：两处 `description` 都以「已废弃」开头、字段仍在 schema 里、以及 `branches=3` 与 `branches=20` 的每一次模型调用输入逐字节相同
- **出处**：`docs/BRANCH-CAP.md` §5.2.1；提交 `c33c1bf`

### 2. label 的切点不落在半个 token 上

- **状态**：已做
- **probe**：`clip_label_retreats`
- **判据**：`backend/tests/test_node_detail.py::test_clip_label_output_never_ends_inside_a_token` —— 对一批样本逐条断言输出末尾不落在标识符中间（`#21` 不是 `#218`、`21` 不是 `21ms`）
- **出处**：`docs/LABEL-CLIP-VERDICT.md` §四；提交 `4b5dccb`

### 3. `origin` 字段落地（三态取值 + 九处写入口）

- **状态**：已做
- **probe**：`node_origin_field`
- **判据**：`backend/tests/test_node_origin.py::test_shape_output_stamps_every_node_generated` —— 生成链路的每一种图型出口都盖上 `generated`；另有十一处写入口各自的用例与"导入第三方格式落 `unknown`"那条
- **出处**：`docs/NODE-ORIGIN.md` §六；提交 `181af48`

### 4. 实体消解的产品入口（候选预览 → 确认合并）

- **状态**：已做
- **probe**：`resolve_has_route`
- **判据**：`backend/tests/test_alias_preview.py::test_preview_shows_real_quotes_and_applies_only_selected` —— 预览走真实引用、只应用勾选的候选
- **出处**：`docs/WIRING-GAPS.md` §13.3 第 1 条；提交 `3c12933` / `98a5309`
- **凭什么**：`§13.3` 至今仍把它记成"下一轮第一顺位"，而它已经做完了（`3c12933` / `98a5309`）——那处旧账还写在 `WIRING-GAPS.md` 里。这是"待办快照会漂"最便宜的一个证据，也正是"只搬状态、不搬理由"的理由：原文档留理由，本表只留状态。 <!-- claims-ok: backlog:4 历史留档：§13.3 那句"第一顺位"是 3c12933 之前的旧判断 -->

### 5. 零引用棘轮的五条 `todo` 清零

- **状态**：已做
- **probe**：`unwired_no_todo`
- **判据**：`python scripts/acc3/find_unwired.py --gate` —— 读数 `522 / 65 / 8 / 0`（顶层公开函数 / 零引用 / 疑似 / 待判），基线的五条：三条删掉、两条改 `accepted`
- **出处**：`docs/WIRING-GAPS.md` §十二、§十三

### 6. 薄材料的真模型侧复跑

- **状态**：半做
- **probe**：`thin_material_offline`
- **判据**：`backend/tests/test_few_branches.py::test_two_branches_with_material_is_retried_with_evidence` —— 覆盖"离线那一半"：上下游判据与三种结局的代码路径；真模型那一半没有判据，因为本机没有 `MINDMAP_LLM_API_KEY`
- **触发条件**：配一个 `MINDMAP_LLM_API_KEY` 后跑 `python scripts/measure_acceptance.py`，再给 `docs/THIN-MATERIAL-BRANCHES.md` 加一节"真模型侧"
- **出处**：`docs/THIN-MATERIAL-BRANCHES.md` §四
- **凭什么**：目录侧 20 条真材料读到 6–7 块、覆盖率 0.96、`OUTLINE_FEW_BLOCKS_NOTE` 触发 0 次；骨架侧三种结局与三条用例一一对应。**未验的是模型面对这 20 条材料会不会只给两条。**

### 7. 纯主题（零材料）请求两级分组

- **状态**：不做
- **probe**：`none`
- **判据**：阈值 —— `runs` 表里一级分支 >10 的比例过 10% 再回来（现在 0.7%：150 次真实运行里 1 次；20 棵带材料的验收树无一超过 10）
- **触发条件**：上述比例过 10%，或有用户反馈"这张图太宽"。那时先看的是 `_cap_plan` 的 20 条上限与画布排布，不是往骨架提示词里加分组要求——分组要多吃一层深度
- **出处**：`docs/BRANCH-CAP.md` §5.2.2

### 8. label 的中文词内切 / 引用配平 / 整条是一个 token

- **状态**：不做
- **probe**：`none`
- **判据**：`backend/tests/test_node_detail.py::test_clip_label_does_not_pretend_to_know_chinese_word_boundaries` —— 把"不去猜中文词边界"写成用例：谁哪天引一张词表进来，这里会红，而不是静默改坏模型新造的术语
- **触发条件**：出现一张能判新术语的词表（不是通用词典），或某个残句有了"读错意思"级别的投诉
- **出处**：`docs/LABEL-CLIP-VERDICT.md` §六 第 1–3 条

### 9. 跨支重复 label 17 处

- **状态**：半做
- **probe**：`cross_reference_wired`
- **判据**：三条一起看，**都不再是"=0"**（理由见 `docs/CROSS-BRANCH-DUPLICATES.md` §五：
  17 处里 8 处 amber 删了会丢信息、3 处 grey 是误报）：
  1. **三档可复算**：`python scripts/measure_cross_branch_duplicates.py --triage` 出
     `red 6 / amber 8 / grey 3`（旧树），且 `python scripts/_structure_audit.py` 的 D 只数
     red（`D 跨支重复（red）≤1`）——首口径 **19/20 = 95%**，与 `--triage` 最后一行逐字一致；
     用例 `backend/tests/test_cross_branch_triage.py`（6 条）钉住三档与"同链不报、跨支仍报"。
  2. **每棵 red ≤1**：旧树 20 棵合计 red 6（全在 S02），重跑三次 S02 全为 **0**。
  3. **amber 有出口**：`agent/tools.py::CROSS_REFERENCE_RULES` 在**非 graph_mode 的结构
     那一段**（system 末尾，紧挨输出形状）追加，上限 8 条；真实生成时 `relations` 非空。
     现状：三次采样 `relations` 8/8/8（基线 0），但"一支写节点、另一支只写关系"**没有**
     发生——8 条线的两端都还在（见 §八）。
  4. **缝有探针**：~~不另立近义判据~~ → **已落地**：`CROSS_BRANCH_UNLINKED`
     （`backend/app/optimization/service.py`）判"跨支同核（剥序号与数量后同一句话）却没有
     任何 `relations` 指认"。读数：r1/r2/r3 = **1 / 2 / 0**，而同一批 red = **0 / 0 / 0**
     ——探针在 red 归零之后还有话说。**不用相似度阈值**：2-gram Dice ≥0.5 在 28 棵树上
     假阳性 210 对 / 真命中 22 对。用例 `test_optimization_rules.py` 的 §3.5（6 条）。
     判定与实测见 `docs/PROMPT-IMPERATIVES.md` §二、§九。
- **触发条件**：第 3 条要么由一条更硬的措辞补上（本轮把"另一支不写节点"改成"先比对你自己
  写的两个标题：去掉序号与数量后同一句话就并成一处"），要么承认"用关系标注同义"就是这条
  路的极限；**`CROSS_REFERENCE_MAX = 8` 要不要动**，等"8 条够不够"的 3 次采样——r2 的 2 处
  未指认是配额挤掉的，成因见 §九.4
- **出处**：`docs/CROSS-BRANCH-DUPLICATES.md` §4.3（落地与核查四问+第五问）、§5.3（四条）、
  §八（三次采样）

### 10. `docs/` 的分级（哪份是入口、哪份是存档、哪份只给 AI 看）

- **状态**：待做
- **probe**：`none`
- **判据**：`python scripts/check_doc_map.py --gate` —— **这个脚本现在不存在**，所以它是"先写判据再派人做"那一档：每份 md 头顶一行 `tier:`（`entry` / `reference` / `archive` / `ai-only`），且从 `README.md` 出发 2 跳内能到达全部 `entry`
- **触发条件**：`docs/` 下 md 数过 130（现值现算：`git ls-files 'docs/*.md' | wc -l`），或者下一个人第二次问"该看哪份"
- **出处**：本文件 §五；理由见 `docs/DOC-CLAIMS.md` §10.5

### 11. `style_profile.branches` 里的 3 与 12 是不是度量脚本的产物

- **状态**：待做
- **probe**：`none`
- **判据**：`python scripts/measure_branch_budget.py --help` 存在的复算口径是主库那条单行命令（`docs/BRANCH-CAP.md` §4.2）；切法写在一句话里：按 `runs.request` 的 `sources` 长度与 `goal` 字面把 3 与 12 那两格归因，`_dedupe` 之后它们应当归零或带出处
- **出处**：`docs/BRANCH-CAP.md` §6 第 3 条

### 12. 文档里会漂的数（用例数 / 文件数 / 端点数 / 开关数 / md 份数）

- **状态**：半做
- **probe**：`doc_drift_guard`
- **判据**：`backend/tests/test_doc_claims.py::test_a_bare_count_in_a_state_document_is_red` —— 闸门的 D 族（`backend/scripts/check_doc_claims.py::_drift_rules`）把这几类写成**形状级禁句**：数字 + 单位出现在「断言现状」的文档里就红。这一族**不立"用例数应当是几"**——那要在每次 `--gate` 里 `collect` 一遍（十几秒），而读数下一秒就过期；它立的是形状：这类数不许写，要写就写"怎么现取"
- **触发条件**：`DRIFT_SCOPE` 之外**断言现状**的还剩 3 份（`TEAM-GUIDE.md`、`docs/PROJECT-STATUS.md`、`docs/USER-PATH-TESTS.md`）；这 3 份里再漂出一次，或者这样的文件涨到 5 份，就把名字加进 `DRIFT_SCOPE`（一行一个，闸门立刻开始判）
- **出处**：`docs/FEATURES_AND_OWNERS.md` 的 F23 ——
  <!-- claims-ok: drift:use_cases 出处引的是三处过期值的原话，不是本文件在断言规模 --><!-- claims-ok: drift:test_files 同一句引文里的文件数 -->
  那行原来写着"后端 2113 个用例 / 85 个测试文件，前端 900 个 / 55 个 spec"，被 `docs/PLAN-REWORDING.md` 与 `docs/SCOPE.md` 各抄一份；三个数早过期了，三处都还写着"本次实测"
- **凭什么**：这种漂**没有一条断言能发现**，因为每句话单独看都曾经是真的，过期时也没有错字——错的是它还写着"现在"。判据只有一句：**这个数会不会因为别人正常提交而变。** 会变的（用例数、文件数、端点数、开关数、md 份数）→ 引用式，永不写具体值；不会变的（契约上限 50 步撤销、`FULL_TEXT_MATERIAL_CHARS = 50_000`、压测 P50）→ 允许写，那些自有前三族与用例管着。这一轮已按这条把 `README.md` 与另外四份里的裸数字全部换成"现跑"。**半做的那一半**：闸门只判这 5 份，scope 外还有 3 份在写旧值（见触发条件）

## 五、这台账本与另外那一百多份 md 是什么关系

（份数不写在这里：`git ls-files '*.md' | wc -l` 现算。这句原本写着两个数，两个数**都已漂**——第 12 条就是从那两个数来的。）

**它吸收"状态"，不吸收"理由"。** 每条只留一句状态、一句判据、一句出处；为什么这么判、
量到了什么、放弃了哪条替代路线——全在原文档里，不搬。搬了就会得到一份会腐烂的摘要，
而摘要腐烂得比原文快。

**所以它不是"第二十四份会腐烂的文档"，它是一份只有 12 行会变、且每行都有机器判据的
索引。** 它的腐烂方式是两种，都已经被闸门挡住：状态写错（`check_backlog` 红）、
判据锚没了（红）。原文档的腐烂不会被它挡住——那仍然是 `--gate` 那批手写事实在管的事。

**但如果只做这一步，读者仍然不知道那一百多份该看哪份。** 那个问题的答案是第 10 条：
分级该建，而且必须是机器可判的分级（`tier:` 字段 + 入口可达性），不是又一份手写目录——
手写目录是这个仓库里最先腐烂的东西（`TODO.md` 与 `NEXT-PLAN.md` 都已经过期）。本轮
不建分级的原因只有一个：它要改每一份 md 的开头，属另一件事；但**它已经进了表，带着
判据和触发条件**，不是一句"以后再说"。

## 六、一条被自己的闸门拦下的账目（它不在表里）

`docs/LABEL-CLIP-VERDICT.md` §六 第 4 条「已落库的残句回溯治」**没有进表**，尽管它是
一件真事。原因不是忘了，是它过不了第一条入表规则：

> 要治它先得有"从 `detail` 反推标题"的路，而那条路本身没有判据——`runs` 不存 raw，
> 原文已不可复原。所以它连一个可量阈值都立不住。

**这正是"没有判据的条目永远会漂"那一类**：它没有状态可言（做不了、也不该记成不做），
留在原文里当一句诚实的自评是对的，进台账只会变成一行没人能验的字。闸门第一次跑起来
就是它先红的——这条记在这里，是为了让"入表规则"有一份活的证据，而不是一句口号。

## 七、本轮读数

- 12 条：已做 5、半做 2、不做 2、待做 3
- 有机判（`probe` 非 `none`）**7 条**：1、2、3、4、5、6、12
- 只有人判（`probe: none`）**5 条**：7、8、9、10、11 —— 它们在 `--facts` 输出里单列
- 判据锚在本仓 HEAD 上**全部存在**的：11 条；唯一不存在的是第 10 条的
  `scripts/check_doc_map.py`，那是"待做"态的规范形状（先写判据，再派人做）
