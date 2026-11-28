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

**语义只有一条，别给它加极性**：`probe` 读出 `present` ⇔ 这件事**已经在代码里**。

**读数有三档，不是真假两档**（为什么必须是三档：`docs/DOC-CLAIMS.md` §10.8）：

| 读数 | 怎么判出来的 | 含义 | 状态该是 |
|---|---|---|---|
| `present` | 原料在，且事实成立 | 这件事在代码里 | 已做 / 半做 |
| `absent` | 原料在，但事实不成立 | 这件事不在代码里 | 待做 |
| `missing` | **原料读不到**（目标符号改名、文件丢了、基线读不出来） | **判据失效**，不是"事实为假" | 不推定 |

前两档说的是**代码**，第三档说的是**探针自己**。二值读数把它们压成一个 `False`，
于是"`_clip_index` 被改名了"会被报成"状态改错，或代码被翻回去了"——那是误诊，而且
会让人去翻 git、去重做已经做完的活。所以 `missing` 单独红，文案带【判据失效】，**且
不许用前两档那两句**。每条 probe 的原料登记在脚本的 `PROBE_MATERIAL` 里，漏登记会红。

- `probe` 非 `none` → 状态只能是 **已做** 或 **半做**（**"已做"不许没有机器判据**）；
   读 `absent` 就红。并自动生成一条 `backlog:<N>` 规则：`present` 时禁本块说"待做/该做/
   下一轮"，`absent` 时禁说"已做/已落地"。**`missing` 时不生成这条规则**——没有读数
   就不该武装任何一侧禁句。
- `probe: none` → 状态只能是 **待做** 或 **不做**（这两态的判据是可量阈值或"钉住不做"
  的用例，代码事实决定不了）。闸门不假装能判，把它单列在 `--facts` 输出里，可见，不静默。

这一栏是这台账本诚实的地方：**别把有机器判据那部分的可信度读成整本的可信度。**
条数与分布不写在这里——它随条目增删变，而这一行**没有**机器判据，写一次就要手抄一次
（§七 就是它已经漂过的证据）。分布由闸门每次现取并打印：`--facts` / `--gate` 都打一行
「状态分布」，还打一行「probe 非 none 几条、三档各几条」。**不落盘**——落盘就是第二份
会漂的汇总，理由见 §七。

### 三之二、半做的另一半：`blocker_probe`（极性相反）

`probe` 真 ⇔ 这件事**已经在代码里**。但"半做"的含义是**另一半卡在某个具体的东西上**，
而 `probe` 一个字都不说那是什么。于是台账允许了一句自述——第 6 条把卡点写成"缺
`MINDMAP_LLM_API_KEY`"——而那句话**是错的**：key 一直在 `run-backend.bat` 第 5 行
（`.env` 里没有，bat 里有），条目因此在错误前提上停了两轮。

所以半做必须带一个 `blocker_probe`，它读的是**"卡点还在不在"**，语义与前一条相反：

| 栏 | 真表示 | 谁必须有 |
|---|---|---|
| `probe` | 事已在代码里 | 已做 / 半做 |
| `blocker_probe` | **卡点还在**（假 = 卡点消失，可以转已做） | 半做必填；待做可给；已做/不做不许给 |

判据只有一句：**"卡点在不在"是代码事实，"活干完没有"要人跑。** 前者可机器判，后者不可——
所以读数翻假时闸门只说「可以转『已做』，但要先真跑一遍」，**不自动转**。自动转就是把
"卡点消失"当成"活干完"，那是同一个病换个方向。

两种可判形态，选哪种由卡点的性质定：

- **代码事实型**（第 11 条）：卡点是"某个机制一行都还没写" → 读代码里有没有它。
- **时序型**（第 9 条）：卡点是"某次测量要按新代码重跑" → 读**产物与补丁的提交时间先后**。
  这里用提交时间，**不用文件 mtime**：干净 checkout 上 mtime 是 checkout 那一刻，
  "产物比补丁新"会永远为真——假绿比没有判据更坏。CI 的 `fetch-depth: 0` 就是为这类判据配的，
  所以这一读数在本地与 CI 上一致。

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

- **状态**：已做
- **probe**：`thin_material_offline`
- **判据**：`backend/tests/test_few_branches.py::test_two_branches_with_material_is_retried_with_evidence`（三种结局的代码路径）+ `docs/THIN-MATERIAL-BRANCHES.md` §4.4 那张表（真模型侧三次采样都走到"交树 + `BRANCHES_BELOW_MIN`"，`ok=True`）
- **出处**：`docs/THIN-MATERIAL-BRANCHES.md` §三、§四
- **凭什么**：`MINDMAP_LLM_API_KEY` 一直在 `run-backend.bat` 第 5 行（`.env` 里没有，bat 里有——这是本条卡住两轮的全部原因，不是缺凭证）。补跑读数：20 条验收材料一级分支 4–7、`planned_branches` 与最终条数逐条相等、降级 0 次；最该触发的"两块标题 + 369 字"在生产配置上 3/3 给 2 条并降级交付（`ok=True`），在干净环境上 3/3 给 4 条。**"只给 2 条"这个形态没有消失，"它被判死"消失了**——所以结案判的是"真模型侧也观察到同一处置"，不是"修复确认"（理由见 §4.5）。未定位：2 条与 4 条之间那个稳定差异由什么开关/库内容决定。

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
- **blocker_probe**：`cross_reference_quota_sampling_stale` —— 卡的不是"没做完"，是**「8 条够不够」还按旧代码量**：`backend/app/agent/tools.py` 的最后提交晚于 `docs/CROSS-BRANCH-DUPLICATES.md`（§八 那三次采样的记录），所以那批读数是旧规则下的。探针读的就是这两个提交时间的先后：真 = 卡点仍在，假 = 可以重判配额并结案（读到假时闸门只说"可以转已做"，不自动转）
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
  5. **判据不分居**：~~逐字那档只在生成链路~~ → **已落地**：跨支逐字同名的判据只有
     一份（`backend/app/optimization/cross_branch.py::cross_branch_duplicates`），
     `agent/tools.py::_cross_branch_duplicates` 是同一个函数对象的 import 别名。
     两条锚：`backend/tests/test_optimization_rules.py::test_cross_branch_judgement_has_exactly_one_implementation`
     （`is` 断言）与
     `backend/tests/test_optimization_rules.py::test_verbatim_cross_branch_names_are_visible_from_check`
     （点「检查与建议」现在报得出来；S02 一次 18 个节点 / 9 个说法）。
     哪些生成侧判据该搬、哪些不该，见 `docs/DIAGNOSTIC-TWO-SIDES.md`。
- **触发条件**：第 3 条要么由一条更硬的措辞补上（本轮把"另一支不写节点"改成"先比对你自己
  写的两个标题：去掉序号与数量后同一句话就并成一处"），要么承认"用关系标注同义"就是这条
  路的极限；**`CROSS_REFERENCE_MAX = 8` 要不要动**，等"8 条够不够"的 3 次采样——r2 的 2 处
  未指认是配额挤掉的，成因见 §九.4
- **出处**：`docs/CROSS-BRANCH-DUPLICATES.md` §4.3（落地与核查四问+第五问）、§5.3（四条）、
  §八（三次采样）

### 10. `docs/` 的分级（哪份是入口、哪份是存档、哪份只给 AI 看）

- **状态**：已做
- **probe**：`doc_map_gate`
- **判据**：`python scripts/check_doc_map.py --gate`，八条判据写在脚本 docstring 里。分级是四档——`entry`（人从这里开始读，十来份，承诺从 `README.md` 出发两跳内可达）/ `reference`（按需查阅，绝大多数）/ `archive`（内容已失效的留档，必须写清替代物）/ `ai-only`（给 AI 的规范与判据，不进人的入口）。**不设 `dead`**：它不是档位而是待办动作（该删或该改），一个标签挂上去就永远挂着——本条自己从 2026-11-10 挂到 11-28 就是活证据。档位与替代物写在 `docs/INDEX.md` 第三节，路径与题目由 `--write` 现算生成，所以**没有给每一份文件加 `tier:` 文件头**（理由见脚本 docstring 第二节：档位是"相对入口的位置"，不是文件自身的属性，写进文件头等于把变动面多放大一倍）。
- **触发条件**：原文写的是"`docs/` 下 md 数过 130，或者第二个人问『该看哪份』"。实际触发的是第二条，而问的人是同一个：一天之内三次在几十份文档里翻原始判断（"记忆是给谁的"、"鱼骨为什么不该体检"、接口缺口那一条从哪读起），而两份本该当前门的入口**都过期了**。所以"第二个人"应当读成"**第二个人，或同一个人第三次**"——按人头立阈值等于假设读者不会忘事。
- **出处**：`docs/DOC-CLAIMS.md` §10.5（原先写着"不做，因为要改一批文件的开头"）；`docs/INDEX.md`；`backend/scripts/check_doc_map.py` 的 docstring；本文件 §五

### 11. `style_profile.branches` 里的 3 与 12 是不是度量脚本的产物

- **状态**：待做
- **probe**：`none`
- **blocker_probe**：`style_branch_attribution_absent` —— 卡的不是"没人看"，是**归因这件事一行代码都没有**：`scripts/measure_branch_budget.py` 里没有任何 `style_profile` 切分（它量的是预算与分支数，不读画像），而 `app/prefs/style.py` 的 `branches` 仍是 `_number("branches", …)` 单值汇总——没有按 `runs.request` 的 `sources` 长度分桶，所以那两格无从归因。真 = 卡点仍在，假 = 归因写出来了，可以拿生产 `runs` 表真跑
- **判据**：`python scripts/measure_branch_budget.py --help` 存在的复算口径是主库那条单行命令（`docs/BRANCH-CAP.md` §4.2）；切法写在一句话里：按 `runs.request` 的 `sources` 长度与 `goal` 字面把 3 与 12 那两格归因，`_dedupe` 之后它们应当归零或带出处
- **出处**：`docs/BRANCH-CAP.md` §6 第 3 条

### 12. 文档里会漂的数（用例数 / 文件数 / 端点数 / 开关数 / md 份数）

- **状态**：已做
- **probe**：`doc_drift_guard`
- **判据**：`backend/tests/test_doc_claims.py::test_a_bare_count_in_a_state_document_is_red` —— 闸门的 D 族（`backend/scripts/check_doc_claims.py::_drift_rules`）把这几类写成**形状级禁句**：数字 + 单位出现在「断言现状」的文档里就红。这一族**不立"用例数应当是几"**——那要在每次 `--gate` 里 `collect` 一遍（十几秒），而读数下一秒就过期；它立的是形状：这类数不许写，要写就写"怎么现取"
- **触发条件**：~~`DRIFT_SCOPE` 之外断言现状的还剩 3 份~~ → **已清（2026-11-28）**：那 3 份逐处改成引用式，名字已加进 `DRIFT_SCOPE`。同日又扩了一次：`DRIFT_CANDIDATES` 里状态 `todo` 的那两处也改成了引用式并进 scope——**"可见但不红"这句话本身就是该消灭的那种话**。份数不写在这里：现取 `len(DRIFT_SCOPE)`。下一条触发条件换成**形状缺口**：D 族只认"数字 + 中文单位"，`2123 passed / 55 files` 这种英文单位它看不见——那类要新增一行 `DRIFT_CLASSES`，判据不变（会因别人正常提交而变就不许写） <!-- claims-ok: drift:use_cases 引文：这两对反引号里写的是**被改掉的原话**（`docs/EXTERNAL-RELEVANCE.md` 的「29 条用例」与 `docs/SCRIPT-HYGIENE.md` 的「128 份」），不是本文件在断言规模 --><!-- claims-ok: drift:doc_count 同上：这里引的是那两处的原话，改完之后原话已不在原文件里 -->
- **出处**：`docs/FEATURES_AND_OWNERS.md` 的 F23 ——
  <!-- claims-ok: drift:use_cases 出处引的是三处过期值的原话，不是本文件在断言规模 --><!-- claims-ok: drift:test_files 同一句引文里的文件数 -->
  那行原来写着"后端 2113 个用例 / 85 个测试文件，前端 900 个 / 55 个 spec"，被 `docs/PLAN-REWORDING.md` 与 `docs/SCOPE.md` 各抄一份；三个数早过期了，三处都还写着"本次实测"
- **凭什么**：这种漂**没有一条断言能发现**，因为每句话单独看都曾经是真的，过期时也没有错字——错的是它还写着"现在"。判据只有一句：**这个数会不会因为别人正常提交而变。** 会变的（用例数、文件数、端点数、开关数、md 份数）→ 引用式，永不写具体值；不会变的（契约上限 50 步撤销、`FULL_TEXT_MATERIAL_CHARS = 50_000`、压测 P50）→ 允许写，那些自有前三族与用例管着。这一轮已按这条把 `README.md` 与另外四份里的裸数字全部换成"现跑"。**下一半也清了**：2026-11-28 把 scope 外那 3 份（`TEAM-GUIDE.md` / `docs/PROJECT-STATUS.md` / `docs/USER-PATH-TESTS.md`）逐处改成引用式，并把名字加进 `DRIFT_SCOPE`（份数现取 `len(DRIFT_SCOPE)`）。**扩 scope 不误报，但会漏**：三份里确实有历史留档段落（`USER-PATH-TESTS.md` 第一节那批读数、`PROJECT-STATUS.md` 的"这一周的实测数字"），闸门对它们零命中——因为历史语境写的是过去时；而三份新文件第一次进 scope 就红了 7 行（`TEAM-GUIDE` 5 行、`USER-PATH-TESTS` 2 行），说明它们**当时就在漂**，不用等"再漂一次或涨到 5 份"这个触发条件。英文单位那类（`2123 passed / 55 files`）闸门至今看不见，见触发条件

## 五、这台账本与另外那一百多份 md 是什么关系

（份数不写在这里：`git ls-files '*.md' | wc -l` 现算。这句原本写着两个数，两个数**都已漂**——第 12 条就是从那两个数来的。）

**它吸收"状态"，不吸收"理由"。** 每条只留一句状态、一句判据、一句出处；为什么这么判、
量到了什么、放弃了哪条替代路线——全在原文档里，不搬。搬了就会得到一份会腐烂的摘要，
而摘要腐烂得比原文快。

**所以它不是"第二十四份会腐烂的文档"，它是一份只有 12 行会变、且每行都有机器判据的
索引。** 它的腐烂方式是两种，都已经被闸门挡住：状态写错（`check_backlog` 红）、
判据锚没了（红）。原文档的腐烂不会被它挡住——那仍然是 `--gate` 那批手写事实在管的事。

**但如果只做这一步，读者仍然不知道那一百多份该看哪份。** 那个问题的答案是第 10 条，
而它已经落地：分级在 `docs/INDEX.md`，四档（`entry` / `reference` / `archive` / `ai-only`），
第三节由 `check_doc_map.py --write` 从 `git ls-files` 现算生成、`--gate` 判八条。它**没有**
给每一份文件加 `tier:` 文件头——上一轮以为那是必须的，于是把成本判成"要改一批文件的开头，
属另一件事"，判得没错，但改错了地方：档位是"相对入口的位置"，属于索引，不属于文件头。
同一个判断顺手把"手写目录最先腐烂"也解决了：`docs/INDEX.md` 只剩人读的两节是手写的
（推荐路径，不列清单，所以不会漏项），清单那半是生成物，像 `openapi.json` 一样被 CI 钉着。

手写目录腐烂这件事本轮有两个活样本，都处理了：`docs/TODO.md` 的名字与内容不符（里面装的
是文件占用，待办在本文），改名 `docs/FILE-OWNERSHIP.md` 并删掉已结案的排期两节；
`NEXT-PLAN.md` 整篇过期却待在仓库根目录那个最强入口位上，移入 `docs/archive/`。

## 六、一条被自己的闸门拦下的账目（它不在表里）

`docs/LABEL-CLIP-VERDICT.md` §六 第 4 条「已落库的残句回溯治」**没有进表**，尽管它是
一件真事。原因不是忘了，是它过不了第一条入表规则：

> 要治它先得有"从 `detail` 反推标题"的路，而那条路本身没有判据——`runs` 不存 raw，
> 原文已不可复原。所以它连一个可量阈值都立不住。

**这正是"没有判据的条目永远会漂"那一类**：它没有状态可言（做不了、也不该记成不做），
留在原文里当一句诚实的自评是对的，进台账只会变成一行没人能验的字。闸门第一次跑起来
就是它先红的——这条记在这里，是为了让"入表规则"有一份活的证据，而不是一句口号。

## 七、这一节不存汇总数字（以及为什么）

**上一版写着两行手抄汇总**：状态分布一行、"有机判"一行。两行都过期了，而**没有任何闸门判
它们**——`check_backlog` 判的是每一条的"状态 ↔ probe ↔ 判据锚 ↔ 卡点"，判不到汇总。
处理方式是删掉而不是再抄一遍：逐条状态就在上面，汇总每重抄一次就再漂一次。

**删掉之后还剩一个问题：读者怎么知道分布？** 答案是让闸门现取并打印，`--facts` 与
`--gate` 都打同样三块：

- **状态分布**：四态各几条、合计几条。
- **机器判据**：`probe` 非 `none` 几条、读数真/假各几条、只有人判几条（并列出编号）。
- **漂移候选枚举**：`DRIFT_SCOPE` 内几处红 / 外几处、几份 md、登记几条（`accepted` /
  `todo` 各几条）、新增几条。

**闸门自己的输出：只打印，不落盘。** 判据是一条——**落盘就等于在文档里再造一份会漂的
汇总**：它随条目增删变，而落盘那份没有判据，正是这一节删掉的两行。只打印的代价是"读者
看不到"，但那可解（命令写在这，跑一次就有）；落盘的代价是**它会绿着过期**，那不可解。
`docs/INDEX.md` 的归档日期由 `check_doc_map.py` 现算并打印、不落盘，走的是同一条路：
能现算的交给命令，人只写不会变的那部分。
