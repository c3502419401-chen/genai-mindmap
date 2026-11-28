# 两套判据：生成时能发现、事后检查看不见 —— 该搬哪些，分界线在哪

位置：`backend/app/optimization/`（事后检查）、`backend/app/agent/tools.py`（生成收尾）。
这一份回答一个问题，不是为了记时间线：

> **"生成时"与"事后"两套判据，用户最终该在哪一处看到问题？**

起因是 `CROSS_BRANCH_DUPLICATE`（跨支逐字同名）：它只在生成链路里报，而用户点
「检查与建议」（`/optimization/suggest` → `check()`）时**一条都看不见**。同一张图，
同一个缺陷，两种时刻两种答案——而那个按钮是用户唯一能主动点的自查入口。

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
本轮修的就是它。

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

---

## 二、逐个查：现在的生成侧判据都有谁、事后有没有对应

只列**生成链路产出**的诊断码。`check()` 已有的七条形码判据（`SINGLE_CHILD` /
`DEPTH_LIMIT` / `DUPLICATE_LABEL` / `NEAR_DUPLICATE_LABEL` / `SHALLOW_BRANCH` /
`HEADING_ONLY_BRANCH` / `MISSING_DETAIL`）两边都有，不在表里。

| 判据 | 生成时能发现 | 事后（`/check`、`/suggest`）能发现 | 该不该搬 |
|---|---|---|---|
| `CROSS_BRANCH_DUPLICATE`（跨支逐字同名） | 有（`_generate` 判在修复**之前**、`_generate_deep` 判在修复之后） | **本轮搬进 `check()`** | **已搬**。输入就是那棵树，三条闸全过 |
| `CROSS_BRANCH_UNLINKED`（跨支同核未指认） | 有（`check()` 里，两条生成路都走它） | 有 | 已在，不需要动 |
| `SOURCE_QUOTE_*` / `SOURCE_QUOTE_AMBIGUOUS` / `UNVERIFIED_SOURCE_REF` / `UNSOURCED_NODE` / `EVIDENCE_UNUSED` 一族 | 有（收尾的 `verify_source_refs`） | **有**：`/suggest` 带 `sources` 就跑第三层，前端「检查与建议」按钮传的是 `sources.readySources`（`StructureCheck.vue` → `structure.ts::runFull`） | 已在。**前提是用户给了材料**——没给就静默不判，这是对的（没材料时"引用核验"没有意义） |
| `SELF_CHECK_ISSUES` 四类（跨支重复 / 语义重复 / 内容跑题 / 层级异常） | 有（`MINDMAP_SELF_CHECK`，默认关，+1 次模型调用） | 无 | **不该搬**。闸 1：它是模型判的。`docs/GENERATE-REPAIR-SELFCHECK.md` 还给了第二个理由——`SuggestionKind` 是**动作**名词，这份清单是**判断**，硬映射会让用户在"建议"面板点一个没有动作的条目 |
| `BRANCHES_BELOW_MIN`（一级分支少于下限） | 有（骨架那一步，重问一次之后仍不足时报） | 无 | **可搬但不值**。闸 2 过（`len(root.children) < MIN_BRANCHES` 是纯数），闸 3 **不过**：用户一眼就数得出几条分支。而它真正说的话是"模型**两次**都只给了 2 条"——那是过程事实，产物上没有痕迹。搬只会剩下一句"结构偏窄" |
| `GRAPH_OUTLINE_UNAVAILABLE`（图谱大纲拿不到） | 有（四种原因各一条，`code` 只有一个） | 无 | **搬不动**。闸 2 不过：它说的是**输入状态**（几份材料、有没有章节目录、入图闸有没有过），产物上没有任何痕迹。事后判它只能猜 |
| `STRUCTURE_AUTO_REPAIRED`（收尾修了几处） | 有（info，带每条的处数） | 无 | 不搬：它是**过程读数**（"这次顺手修了什么"），事后那棵树已经没有那个状态了 |
| `STRUCTURE_FEW_BRANCHES_AFTER_REPAIR` | 有（修复把分支并到少于下限） | 可判但无对应 | 可搬，闸 3 与 `BRANCHES_BELOW_MIN` 同一条理由（一眼可见）。**这一条值得单列**：它说的是"修复自己造出的问题"，而修复是 `/repair` 也在做的事——`/repair` 的 `skipped` 已经报了它没修的，所以缺口的实际伤害小 |
| `LABEL_OVERLONG_IN_DELIVERY`（交付的树上有标题会被截成半句） | 有（修完之后再量一遍） | 无 | **可搬**：闸 1 过（`repair.overlong_labels` 是纯函数）、闸 2 过（就量 label 字数）、闸 3 过（画布上 `line-clamp-2` 截成半句，用户看得出被截但看不出"截掉的那半句本可以不断"）。**本轮没搬**：它要动 `repair.py` 的公开面，而这一轮的边界只批了跨支那一条 |
| `LABEL_CLIPPED_TO_GENERATED` | 有（模型写了超过 16 字的标题，已截短） | 无 | 不搬：截断**已经发生了**，事后那棵树上没有长标题可判 |
| `SOURCE_REF_NLI` / `SOURCE_REF_CONTRADICTED`（语义判定） | 有（`annotate_nli`，默认开） | 无 | 不该搬：闸 1 不过（onnx 推理，几十毫秒/条 × 上限 100 条 = 最多 4.4 秒）。`/suggest` 是个按钮，不是批处理 |

**真实的"生成时能发现、事后看不见"的判据，摊开来是 6 条**（`SELF_CHECK_ISSUES` 一族
按 1 条算），而其中只有 1 条（本轮这条）三条闸全过。这一轮搬的就是那一条。

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
的判据，用户在两处都看得到（而且必须是同一条实现，见下一节）；剩下的只在生成完的那
一刻看得到，那就够了——那一刻他本来就在看这张图。

---

## 四、这一轮落了什么

`CROSS_BRANCH_DUPLICATE` 的判据原先只活在 `agent/tools.py::_cross_branch_duplicates`。
现在它在 `backend/app/optimization/cross_branch.py`，`agent/tools.py` 那一侧是
**同一个函数对象的 import 别名**：

    from app.optimization.cross_branch import cross_branch_duplicates as _cross_branch_duplicates

`is` 断言钉在 `test_cross_branch_judgement_has_exactly_one_implementation`——两份实现
可以凑出"结果一致"，凑不出"同一个对象"。

三条随之而来的边界（都在 `cross_branch.py` 的模块 docstring 里一次写完）：

1. **同父不算**、2. **同一条祖先链不算**、3. **逐字档让给 `CROSS_BRANCH_DUPLICATE`**、
4. **剥空退回原文**：四条边界两条码都要守，所以写在一处、两边指过去。
5. 顺带写清一个此前没人说过的**缝**：两条码的"逐字"是两个宽度（一个只折空白、一个连
   标点一起抹平），中间那一档——只差标点或全半角的两处——**谁都报**。这是选出来的
   静默：填它要么放低 `CROSS_BRANCH_DUPLICATE` 的门槛（它的价值全在"值得看一眼"），
   要么让 `CROSS_BRANCH_UNLINKED` 对"只差一个冒号"喊话（半条误报）。

**生成链路的行为**：整树那条路的诊断逐字不变（仍用**修复之前**取好的重复清单，
`test_cross_branch_duplicates_are_judged_before_the_repair` 钉着），做法是把它从
`check()` 的产出里按码剔掉、再追加修前那条。**分片那条路新增一条**——它原先一次都不
报，而跨支重复正是分片的结构性缺陷，这条诊断它比整树更需要
（`test_the_deep_path_reports_cross_branch_duplicates_too`）。

**第二个出口**：`NOT_SUGGESTED` 与 `repair.MANUAL_ONLY` 各加一条。跨父合并要人定
留哪一处——`dedupe` 只处理**同一个父节点下**的一对，而跨支的两处挂在两个父下面，删
哪一处会改变哪条分支的内容量，那是编辑决定不是去重。

**实测（`/suggest` 这条路，`check()` 直接跑）**：

    S02  「18 个节点跨分支重复（9 个说法各写了 2/2/2 遍）」……
    S14  「4 个节点跨分支重复（2 个说法各写了 2/2 遍）」……

S02 那 9 个说法是 `docs/CROSS-BRANCH-DUPLICATES.md` §一 那 17 处里最大的一份
（9 个说法 = 18 个节点，与那里的"节点数 34 = 17×2"同口径）。
