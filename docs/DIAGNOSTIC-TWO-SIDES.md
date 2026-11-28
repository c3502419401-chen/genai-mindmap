# 两套判据：生成时能发现、事后检查看不见 —— 该搬哪些，分界线在哪

位置：`backend/app/optimization/`（事后检查）、`backend/app/agent/tools.py`（生成收尾）。
这一份回答一个问题，不是为了记时间线：

> **"生成时"与"事后"两套判据，用户最终该在哪一处看到问题？**

起因是 `CROSS_BRANCH_DUPLICATE`（跨支逐字同名）：它只在生成链路里报，而用户点
「检查与建议」（`/optimization/suggest` → `check()`）时**一条都看不见**。同一张图，
同一个缺陷，两种时刻两种答案——而那个按钮是用户唯一能主动点的自查入口。

后续两轮各搬了一条：`CROSS_BRANCH_DUPLICATE`、`LABEL_OVERLONG_IN_DELIVERY`。搬完之后
这张表是什么状态、还剩多少没判，见 §五（全量普查）与 §六（结案状态）。

---

## 一、先纠正一个看起来对、其实会判错的二分

"事后那套是用户的（他能主动点），生成时那套是开发者的（只在日志与 data 里）"
——**前半句对，后半句不成立**。

生成收尾产出的诊断**不是日志**：它随 SSE 回传，进 `data["diagnostics"]`，
`AgentPanel` 会渲染给用户看。用户刚生成完的那一刻，看到的正是"生成时那套"。所以
按"谁看得到"分，两边都是给用户看的，这条线拉不开。

真正的差别只有一件事：**判据的输入还在不在。**

    输入是那棵树（+ 用户能提供的 sources）      → 任何时刻都在，两个时刻都该判
    输入是生成过程的状态（重试了几次、大纲
    拿不拿得到、模型自己说了什么）               → 只在那一刻存在，事后搬就是猜

`CROSS_BRANCH_DUPLICATE` 属于第一类，却只在生成侧跑——这是**归属错**，不是取舍。
那一轮修的就是它。

于是分界线是三条闸，而不是一句"该搬的都搬"：

### 闸 1：确定性（纯函数）

`check()` 必须是纯函数，同一个输入永远同样的诊断与顺序。理由写在
`optimization/service.py` 的模块 docstring 里，而且它是**被调用方式决定的**：
生成收尾调它、`/check` 调它、`/repair` 修完还要重跑一次。接上概率模型，同一个接口
两次调用就给出不同诊断，"还剩几条建议"也不再可复现。

→ **要模型才能判的，一律留在生成侧。** 这不是省事，是 `check()` 的性质。

### 闸 2：产物可判

判据的输入必须能从那棵树（加上调用方给的 `sources`）里读出来。读不出来的，硬搬到
`/check` 会变成猜：一条"猜出来的诊断"比没有诊断更糟，它占着同一个面板。

### 闸 3：看得见才算

搬过来的判据得说**用户从画布上看不出来**的事。一眼可见的东西（"这棵树只有两条
一级分支"）搬过来只是给面板加噪音——而面板的注意力是有限资源，`SINGLE_CHILD` 这种
结构常态已经被降成 `info` 就是为了不占那个位置。

### 闸 0（前置，不属于判据而是面）：这个接口收不收这种图

`/optimization/check` 与 `/suggest` **只收树型**（`optimization/router.py::_tree_content`，
非 `MindMapContent` 一律 422）。鱼骨的六根骨是并列类别、流程的阶段是独立命名空间，
"单子节点""分支深度对比"那些判据搬过去是噪声不是错误——所以那不是"泛化一下"，
是"这里不适用"。**鱼骨与流程的形态判据因此连进闸的资格都没有**（见 §五的第四类）。

---

## 二、逐个查：现在的生成侧判据都有谁、事后有没有对应

只列**生成链路产出**的诊断码里，判据落在产物上的那些。`check()` 已有的条码判据
（`SINGLE_CHILD` / `DEPTH_LIMIT` / `DUPLICATE_LABEL` / `NEAR_DUPLICATE_LABEL` /
`SHALLOW_BRANCH` / `HEADING_ONLY_BRANCH` / `MISSING_DETAIL`）两边都有，不在表里。

| 判据 | 生成时能发现 | 事后（`/check`、`/suggest`）能发现 | 判定 |
|---|---|---|---|
| `CROSS_BRANCH_DUPLICATE`（跨支逐字同名） | 有（整树判在修复**之前**、分片判在修复之后） | **已搬进 `check()`**（上一轮） | **已搬**。输入就是那棵树，三条闸全过 |
| `LABEL_OVERLONG_IN_DELIVERY`（交付的树上有标题会被截成半句） | 有（修完之后再量一遍） | **已搬进 `check()`**（本轮） | **已搬**。判据在 `optimization/label_length.py`，两侧同一个函数对象（`is` 断言）；只测量、不给建议，理由见下 |
| `CROSS_BRANCH_UNLINKED`（跨支同核未指认） | 有（`check()` 里，两条生成路都走它） | 有 | 已在，不需要动 |
| `SOURCE_QUOTE_*` / `SOURCE_QUOTE_AMBIGUOUS` / `UNVERIFIED_SOURCE_REF` / `UNSOURCED_NODE` / `EVIDENCE_UNUSED` 一族 | 有（收尾的 `verify_source_refs`） | **有**：`/suggest` 带 `sources` 就跑第三层，前端「检查与建议」按钮传的是 `sources.readySources` | 已在。**前提是用户给了材料**——没给就静默不判，这是对的 |
| `SELF_CHECK_ISSUES` 四类（跨支重复 / 语义重复 / 内容跑题 / 层级异常） | 有（`MINDMAP_SELF_CHECK`，默认关，+1 次模型调用） | 无 | **不该搬**。闸 1：它是模型判的。另一个理由见 `docs/GENERATE-REPAIR-SELFCHECK.md`——`SuggestionKind` 是**动作**名词，这份清单是**判断** |
| `BRANCHES_BELOW_MIN`（一级分支少于下限） | 有（骨架那一步，重问一次之后仍不足时报） | 无 | **可搬但不值**。闸 2 过，闸 3 **不过**：用户一眼数得出几条分支。而它真正说的话是"模型**两次**都只给了 2 条"——过程事实 |
| `TREE_OVER_READABILITY`（节点数超可读性参考值） | 有（info） | 无 | **不搬**，半接缝。节点数与分支数产物可判，但要报它得知道 `initial_nodes`（这次的口味配额，不在树上）；而"这棵树很大"用户一眼可见（闸 3） |
| `GRAPH_OUTLINE_UNAVAILABLE`（图谱大纲拿不到） | 有（四种原因各一条，`code` 只有一个） | 无 | **搬不动**。闸 2 不过：说的是**输入状态**，产物上没有任何痕迹 |
| `STRUCTURE_AUTO_REPAIRED`（收尾修了几处） | 有（info，带每条的处数） | 无 | 不搬：**过程读数**，事后那棵树已经没有那个状态了 |
| `STRUCTURE_FEW_BRANCHES_AFTER_REPAIR` | 有（修复把分支并到少于下限） | 可判但无对应 | 可搬但不值，闸 3 与 `BRANCHES_BELOW_MIN` 同一条理由。**这一条值得单列**：它说的是"修复自己造出的问题"，而 `/repair` 的 `skipped` 已经报了它没修的 |
| `LABEL_CLIPPED_TO_GENERATED` | 有（模型写了超过 16 字的标题，已截短） | 无 | 不搬：截断**已经发生了**，事后那棵树上没有长标题可判 |
| `UNKNOWN_SOURCE_REF` | 有（ref 指向不在本次输入里的 source，**已随判定移除**） | 无（事后由 `SOURCE_TEXT_UNAVAILABLE` 兜住） | 不搬：那条 ref 已被删掉，产物上没有痕迹；事后拿 `sources` 核，同一个现象换个码说出来 |
| `SOURCE_REF_NLI` / `SOURCE_REF_CONTRADICTED`（语义判定） | 有（`annotate_nli`，默认开） | 无 | 不该搬：闸 1 不过（onnx 推理，几十毫秒/条 × 上限 100 条 = 最多 4.4 秒）。`/suggest` 是个按钮，不是批处理 |

**§二这张表是"逐行判定过"的那一批：13 行、覆盖 18 个码**（引用类与 NLI 按族合并）。
其中 **2 行已搬**（前一轮跨支、本轮超长标题）、**2 行本来就两侧都有**（`CROSS_BRANCH_UNLINKED`、
引用那一族），其余 **9 行不搬**，每行都写着卡在哪条闸上。

★ 这 13 行**不是**"生成侧诊断码"的全量——全量是 72 条，见 §五。两处的差额是
"过程读数"与"接口面不覆盖"两类，按类整批判，不按条判。

---

## 三、分界线长什么样

一句话：

> **判据搬进 `/check`，当且仅当它的输入还在（那棵树 / 调用方给的 sources）、它是纯
> 函数、而且它说的是用户从画布上看不出来的事。三条缺一，它就留在生成侧——不是"以后
> 再搬"，是"搬过去会变成猜或者变成噪音"。**

三条闸各自挡掉的是**不同**的失效，所以不能合并成一条：

- 只留闸 1 → `BRANCHES_BELOW_MIN` 会被搬进来，面板多一条用户数一眼就知道的话。
- 只留闸 2 → `SELF_CHECK_ISSUES` 会被搬进来，`check()` 从此不确定。
- 只留闸 3 → `GRAPH_OUTLINE_UNAVAILABLE` 会被搬进来，判据开始猜输入。

而"用户在哪一处看到问题"是这三条闸的**结果**，不是一条独立的判据：产物可判 + 纯函数
的判据，用户在两处都看得到（而且必须是同一条实现，见 §四）；剩下的只在生成完的那
一刻看得到，那就够了——那一刻他本来就在看这张图。

---

## 四、两轮搬完之后，各自落了什么

### 4.1 跨支重复（上一轮）

`CROSS_BRANCH_DUPLICATE` 的判据原先只活在 `agent/tools.py::_cross_branch_duplicates`。
现在它在 `backend/app/optimization/cross_branch.py`，`agent/tools.py` 那一侧是
**同一个函数对象的 import 别名**：

    from app.optimization.cross_branch import cross_branch_duplicates as _cross_branch_duplicates

`is` 断言钉在 `test_cross_branch_judgement_has_exactly_one_implementation`。

三条随之而来的边界（都在 `cross_branch.py` 的模块 docstring 里一次写完）：
**同父不算**、**同一条祖先链不算**、**逐字档让给 `CROSS_BRANCH_DUPLICATE`**、
**剥空退回原文**；另有一条此前没人说过的缝：两条码的"逐字"是两个宽度（一个只折
空白、一个连标点一起抹平），中间那一档——只差标点或全半角的两处——**谁都报**。
那是选出来的静默。

**第二个出口**：`NOT_SUGGESTED` 与 `repair.MANUAL_ONLY` 各加一条。

### 4.2 标题超长（本轮）

判据原先在 `repair.py`，两侧都要用它，而 `repair.py` 依赖 `service.check`——反过来
import 就是循环。所以它单独一处：

    backend/app/optimization/label_length.py    READABLE_LABEL_CHARS + overlong_labels()

`service.py` 与 `repair.py` 都 import **同一个对象**，`repair.overlong_labels` 仍是
import 别名（公开面不变，`scripts/_structure_audit.py` 照旧从那里取数）。
`test_the_overlong_judgement_has_exactly_one_implementation` 用 `is` 钉住。

**生成侧不再自己量一遍**：`tools.py` 收尾那句 `check(content)` 会把它带出来。两处都
量会在同一次生成的诊断列表里出现两条一模一样的码——而 `test_a_delivered_long_label_
is_reported_after_the_repair` 的 `len(reported) == 1` 现在钉的是"没有两份产生处"。

三个数各管一段，搬的时候最容易拿错：

| 数 | 值 | 管什么 | 这条码用不用 |
|---|---|---|---|
| `MAX_NODE_LABEL_CHARS` | 200 | 契约上限（`Field` 硬拒，旧图必须能读） | 不用 |
| `GENERATED_LABEL_MAX_CHARS` | 16 | 生成侧**硬切**：模型写超了就截，截的那一刻报 `LABEL_CLIPPED_TO_GENERATED` | 不用。16 < 24，所以正常生成的标题在画布上**不会被截**；"切点落在半个词上"是那一刻的事，事后树上只剩切短的标题，判不到 |
| `READABLE_LABEL_CHARS` = 前端 `TRUNCATE_CHARS` | 24 | 画布 `line-clamp-2` 的截断口径 | **用它**。超 24 就是"用户看到半句标题" |

**只进诊断，不给建议**（`NOT_SUGGESTED` 与 `repair.MANUAL_ONLY` 各一条理由）：
事后分不出树上那个 `A / B` 是压链拼的还是用户手写的斜杠标题（`LABEL_JOINER` 就是
中文标题里常用的那个斜杠，产物上没有标记），两个修法（把后半句挪进 detail / 拆回
两层）都要改写用户自己的内容。所以它只负责说清"这几个标题会被截成半句，缩一下、
或者把后半句挪进说明"；而压链那一侧的正确修法早就在拼接**之前**做了（`_absorb`
第三档：拼不下先把外层标题降级进 detail）。

**实测**（`check()` 直接跑 `backend/data` 下 72 棵真树）：17 棵报出、23 个节点，
**全部是 ` / ` 拼接形状**；当前 `acc_trees` 一棵都没有——拼接口径改了之后，这条码是
**回归闸**而不是现存缺陷。

---

## 五、全量普查：72 条只在 `check()` 之外产生（可复算）

§二那张表只覆盖"判据落在产物上"的那批。**全量**用脚本查，不靠记忆：

    cd backend && python scripts/check_diagnostic_sides.py

它给两份互相独立的证据：

1. **静态**：`app/**/*.py` 里所有 `Diagnostic(code="X")` 字面量，按文件分区——
   `optimization/{service,cross_branch,source_refs}.py` 是 `check()` 这一侧，其余是别的路径。
2. **运行期**：`backend/data/acc_trees*` 下所有真树跑一遍 `check()`，收实际产出的码。

当前读数（本轮）：静态 87 个码 → 两侧都产生 3、只在 `check()` 一侧 12、**只在
`check()` 之外 72**；运行期在 72 棵真树上产出 5 个码（`CROSS_BRANCH_DUPLICATE` 27 次、
`CROSS_BRANCH_UNLINKED` 11 次、`LABEL_OVERLONG_IN_DELIVERY` 17 次、`SHALLOW_BRANCH`
6 次、`MISSING_DETAIL` 1 次）。

那 72 条落在四类里，**只有前两类需要判定，后两类不需要**：

| 类 | 有多少 | 判据的输入是什么 | 判定 |
|---|---|---|---|
| 过程读数（这次跑成什么样） | 大多数 | 生成过程的局部状态：重试次数、额度、哪一支抛错、模型返回了几条重复 | **不搬**，闸 2 不过——产物上没有痕迹 |
| 半接缝（产物可判、但阈值/参照是过程参数） | 1（`TREE_OVER_READABILITY`） | 节点数可判，`initial_nodes`（口味配额）不可判 | **不搬**，闸 3：树大一眼可见 |
| **接口面不覆盖**（鱼骨 / 流程） | 12（`FISHBONE_*` 5 + `FLOW_*` 7） | **判据不纯**：`_fishbone_shape_diagnostics` 读产物 + **环境变量**，`_stamp_flow_roles` 是**变换**（边改产物边报"我改了什么"），三条损失类读的是 `to_flow` 的返回值 | **搬不动**：`/check` 只收树型（§一 闸 0）；而更硬的限制是**产物里没有这个信息**——12 条里只有 2 条能从产物重算。逐条判定与实测见 `docs/CHECK-INTERFACE-GAP.md` |
| 已被自己移除（判完就删掉痕迹） | 1（`UNKNOWN_SOURCE_REF`） | 那条 ref 已在规范化时删掉 | 不搬；事后同一现象由 `SOURCE_TEXT_UNAVAILABLE` 说出来 |

**"怎么确认 9 条是全量？"——不能确认，只能复算。** 硬的说法是：全量是那 72 条（脚本
给，无需人记）；72 条里逐条读过输入来源的是 9 条，其余 63 条按上表整批归类。整批归类
的软处是"码名 + 所在函数 + 输入来源"这三点，不是逐行读——所以本轮抽检了 5 条代表：

    SPARSE_BRANCH      need 是每支要求点数（配额）+ "补调过一次"（过程）→ 判不到
    DUPLICATE_POINT    "模型返回了 N 条重复，已去重"——去重后树上已经没有重复 → 判不到
    SHALLOW_LEVEL      audit.missing 来自层级展开审计（过程）→ 判不到
    FLOW_EDGE_DROPPED  被丢的边已经不在树上（loss.dropped）→ 判不到
    TREE_OVER_READABILITY  节点数可判、阈值不可判 → 半接缝（已列上表）

抽检里唯一推翻"整批一样"的是 `TREE_OVER_READABILITY`：它是**产物可判但闸 3 不过**，
不是过程读数。所以整批归类时留了一句"抽到反例就单列"——它已经单列了。

**门禁**（可选接 CI）：

    python scripts/check_diagnostic_sides.py --require-documented

判据是"每个只在 `check()` 之外产生的码，名字都出现在本文档里"——也就是**必须有人
判过、或整批归类到了某一类**。新增一个生成侧诊断码而没写判定，这条会红。它比"我
查过了"多一样东西：可复算。

---

## 六、这张表现在的状态

- **已搬 2 条**：`CROSS_BRANCH_DUPLICATE`、`LABEL_OVERLONG_IN_DELIVERY`。
- **判过、不搬 9 条**（§二那 9 行）。
- **整批归类 63 条**：其中 12 条是接口面（`FISHBONE_*` 5 + `FLOW_*` 7，上一轮记成
  "13 条（6 + 7）"，合计错了，逐条判定见 `docs/CHECK-INTERFACE-GAP.md`），51 条是
  过程读数或输入状态。按类判，理由在 §五。
- **没有"可搬、值、但还没搬"的条目了。** 上一轮那句"下一轮最值得的一条"本轮已经
  搬完，而搬完之后它掉进了"已搬"，不是"值得但没做"。

结论：**这张表结案了**——不是"没有可搬的了"，而是"剩下的每一条都有一份写下来的
判定（按条或按类），且判定可复算"。它此后只该在两种情况被打开：

1. 有人**新增**一个诊断码（`--require-documented` 会先红一次，逼一次判定）；
2. 有人**改接口面**（`/check` 开始收鱼骨/流程时，那 12 条 `FISHBONE_*` / `FLOW_*`
   要按闸 1–3 重判一遍——其中 10 条的判据读的是"转换时的损失"与"代码做过的动作"，
   光放开接口救不出来，见 `docs/CHECK-INTERFACE-GAP.md` §三）。

---

## 附录：只在 `check()` 之外产生的 72 个码

`scripts/check_diagnostic_sides.py --require-documented` 认的是码名出现在本文件里，
所以下面逐个列出（分组只是给人读的归类，不是判定粒度）。

**分支 / 要点展开的过程读数**：`BRANCH_DIGEST_INCOMPLETE`、`BRANCH_DIGEST_SKIPPED`、
`BRANCH_FAILED`、`BRANCH_NO_EVIDENCE`、`BRANCH_RETRIEVAL_FAILED`、
`BRANCH_RETRIEVAL_FALLBACK`、`BRANCH_SKIPPED_BUDGET`、`BRANCH_SUPPLEMENTARY_EMPTY`、
`BRANCH_SUPPLEMENTARY_HITS`、`DETAIL_CALL_FAILED`、`DETAIL_ROUND_EMPTY`、
`DETAIL_ROUND_INCOMPLETE`、`DETAIL_SCALE_APPLIED`、`DETAIL_SENTENCES_OPEN`、
`DETAIL_SKIPPED_NO_BUDGET`、`DUPLICATE_POINT`、`EXPAND_FLAG_IGNORED`、`LEVEL_FAILED`、
`LEVEL_SKIPPED_BUDGET`、`NODE_BUDGET_CUT`、`NO_PROGRESS`、`OUTPUT_TRUNCATED_RECOVERED`、
`SHALLOW_LEVEL`、`SPARSE_BRANCH`

**骨架与形态整形**：`FISHBONE_CATEGORIES_INCOMPLETE`、`FISHBONE_CATEGORIES_UNFIXED`、
`FISHBONE_EXTRA_BRANCH`、`FISHBONE_PROBLEM_ECHOES_TITLE`、`FISHBONE_PROBLEM_MISSING`、
`FLOW_BRANCH_UNRESOLVED`、`FLOW_EDGE_DEMOTED`、`FLOW_EDGE_DROPPED`、
`FLOW_ROLE_END_DEFAULTED`、`FLOW_ROLE_END_MOVED_UP`、`FLOW_ROLE_END_OVERRIDES_DATA`、
`FLOW_ROLE_START_DEFAULTED`、`SKELETON_GROUPS_APPLIED`、`SKELETON_SIZE_OFF`、
`TREE_OVER_READABILITY`

**材料 / 检索 / 图谱 / 切片的输入状态**：`EVIDENCE_UNUSED`、`GENERATION_EXTERNAL_DISABLED`、
`GENERATION_FROM_EXTERNAL`、`GENERATION_VERIFIED`、`GENERATION_WITHOUT_EVIDENCE`、
`GENERATION_WITHOUT_EXTERNAL`、`GRAPH_INGEST_DONE`、`GRAPH_INGEST_FAILED`、
`GRAPH_OUTLINE_UNAVAILABLE`、`GRAPH_OUTLINE_USED`、`GRAPH_RELATION_DROPPED`、
`GRAPH_SKELETON_UNAVAILABLE`、`INPUT_EXTRACT_UNAVAILABLE`、`LIBRARY_SUPPLEMENT`、
`LIBRARY_SUPPLEMENT_SKIPPED`、`MATERIAL_TOO_SHORT`、`MATERIAL_UNUSED`、
`SECTION_SLICE_BRANCH_DROPPED`、`SECTION_SLICE_CALL_FAILED`、`SECTION_SLICE_NO_BUDGET`、
`SECTION_SLICE_PARTIAL_COVERAGE`、`SECTION_SLICE_REF_DROPPED`、`SECTION_SLICE_UNAVAILABLE`、
`SOURCE_NOT_PARSED`、`SOURCE_REFS_DROPPED`、`VERIFY_SKIPPED_NO_BUDGET`

**结构 / 自检 / 生成入口**：`BRANCHES_BELOW_MIN`、`LABEL_CLIPPED_TO_GENERATED`、
`SELF_CHECK_ISSUES`、`SELF_CHECK_SKIPPED`、`STRUCTURE_AUTO_REPAIRED`、
`STRUCTURE_FEW_BRANCHES_AFTER_REPAIR`、`UNKNOWN_SOURCE_REF`
