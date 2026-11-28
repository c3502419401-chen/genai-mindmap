"""结构诊断（F15）与候选建议（F16）。

位置：agent 的生成收尾（`agent/tools.py::_generate`）与 optimization 路由共用；
      `suggest()` 面向用户产出可确认的修改建议。

check() 检查单子节点、达到深度上限及同一父节点下的重复标签；
提供 sources 时还会核验引用原文。这不等于事实核验或语义蕴含判断。

重要：建议是候选，必须由用户确认后应用，不能自动覆盖手工编辑。

======================================================================
两条路不能混
======================================================================
    ops      模型改图   直接落画布     （已存在，`agent/ops.py`）
    suggest  建议       用户确认才应用  （本文件）

`check()` 的结果**永远不自动应用**。把"这棵树有问题"当成"这棵树该变成那样"，
用户会在保存时发现图莫名其妙变了，而且找不到是谁改的。

======================================================================
这一版的三层判据（成本从零往上）
======================================================================
1. **规则层**（零成本、确定性）：`check()` 的全部规则。同一个输入永远同样的
   输出，所以它敢接在生成收尾，也敢让 `/check` 反复调。
2. **候选层**（零成本）：`suggest()` 把诊断翻成"给用户看的动作"，
   `_SUGGESTION_OF_CODE` 是唯一的映射表。
3. **模型层**（**一次** LLM 调用，默认关）：只判"同级近义"，判据见
   `_model_near_pairs`。它是本功能里唯一的成本，所以有开关
   （`MINDMAP_OPTIMIZATION_LLM` 或 `suggest(use_model=...)`）、有规模下限
   （`MIN_LLM_SIBLINGS`）、失败一律返回空列表。

**模型层只在 `suggest()` 里跑，不进 `check()`**：`check()` 一旦接上概率模型，
同一个接口两次调用就会给出不同的诊断，生成收尾那个 `reported` 计数也不再可复现。
诊断是事实、建议是候选——概率模型只配产出后者。

======================================================================
故意不转建议的诊断
======================================================================
见 `NOT_SUGGESTED`（码 → 理由）与 `_REFERENCE_CODE_PREFIXES`。两条要点：

  · `DEPTH_LIMIT`——"已到 8 层"不是一个可执行的动作。契约本身禁止第 9 层
    （诊断用的深度上限就是契约的 `MAX_TREE_DEPTH`），所以既没有"再展开一层"
    可做，也没有"减一层"这种可定位的编辑——要减掉的是这一支的中间层，那是重写
    一支而不是改一个节点。它只作为 warn 诊断存在。
  · 引用类（`SOURCE_*` / `UNSOURCED_*` / `UNVERIFIED_*` / `INVALID_*` / `EMPTY_*`）
    ——`drop`（剔除无依据节点）是对内容真实性的判断：逐字命中只说明这句话出自
    那里，不说明那个来源可靠（F13 的口径），而 NLI 那一层实测里 24 条真实引用
    有 4 条被误判成相反（见 `optimization/nli.py` 的 `DEFAULT_ON_GENERATE`）。
    成批产出"删掉这些节点"的建议，代价远大于收益。它们只给诊断。

======================================================================
前置依赖：`relations` 在界面上根本不渲染
======================================================================
"建议添加连接线"这一类**因果关系**建议现在做不了，不是算法没写，是**做了用户也
看不见结果**。`frontend/src/mindmap/edits.ts` 的原话：

    `relations`（跨节点关系线）**三种形状的顶层都有**……用户既看不到那条残留的
    关系，也没法删掉它

`relations` 目前只有"删节点时被连带清理"（`_prune_dangling_relations`）这一个
读者。所以因果推断这一项的前置条件是**先把 `relations` 画出来**（F16 的界面
欠账），不是补一个检测函数。别看到这里缺一块就以为漏做了。

======================================================================
一个节点最多一条"形态"诊断
======================================================================
`SINGLE_CHILD` 与 `HEADING_ONLY_BRANCH` 的判据都落在"恰好一个子节点"上，所以
后者是前者的**替换**而不是追加（区别只在解释与动作：没展开 vs 只是个标题）。
`SHALLOW_BRANCH` 要求父节点至少两个子节点，与这两条互斥。
`tests/test_optimization_rules.py` 钉着这三件事。
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterator

import httpx

from app.contracts.diagnostic import (
    Diagnostic,
    DiagnosticLevel,
    LinkStatus,
    Suggestion,
    SuggestionKind,
)
from app.contracts.mindmap import MAX_TREE_DEPTH, MindMapContent, Node
from app.contracts.source import Source
from app.optimization.source_refs import verify_source_refs

logger = logging.getLogger(__name__)

# 层数上限**就是**契约那一个数，不另存一份。
#
# 这里曾经写着 `MAX_DEPTH = 8` 加一句"与契约保持一致"——那是注释承诺，不是机制：
# 把契约改成 9，这边仍会在第 8 层报"已到上限"，而图还能合法加一层。诊断说错了话
# 却没有任何东西会红。现在它是 `MAX_TREE_DEPTH` 的引用，契约一动它跟着动。
#
# 口径核对过，不是"看起来像"：`_iter_nodes` 的 `depth` 从 1 起（根是第 1 层），
# 契约的 `MAX_TREE_DEPTH` 也是**含根**的层数，所以"第 8 层是最后一层"两侧同义。
# 断言在 `tests/test_shared_constants.py`。

# ---------------------------------------------------------------- 规则层阈值
#
# 每个数都说清依据。这一层产出的是**提示**不是判决：判错的代价是用户多看一眼，
# 判漏的代价是"重复的两支一直留在图里"，所以宁可把低置信度那档降成 info，
# 也不要把阈值调到几乎不响——一个常年沉默的检查等于没有检查。

# 模糊比较的字符下限。2 个字之间编辑距离 1 意味着换掉一半（「定义」⇄「定理」、
# 「物理」⇄「数学」），那是两个概念；3 个字起才会出现「监督学习」⇄「有监督学习」
# 这种"同一个概念的两种写法"。
MIN_FUZZY_CHARS = 3

# 编辑距离上限。只用在**纯插删**这一种变形上（见 `_near_reason`）：中文里"同一个
# 概念的两种写法"最典型的形态就是加一个限定词（数据→**预**处理、监督学习→**有**
# 监督学习），一次插入；上限 2 允许加两个字的修饰语（「交叉验证」⇄「分层交叉验证」），
# 再多就不是修饰而是另一个概念了。
MAX_EDIT_DISTANCE = 2

# 归一化编辑距离上限 = 插入字数 / 较短标签字数。3 个字里加 2 个字（0.67）已经
# 换掉了大半语义，挡掉；5 个字加 2 个（0.4）放行。
MAX_EDIT_RATIO = 0.4

# 前缀关系的长度占比下限。「监督学习」/「监督学习与无监督学习」= 4/10；
# 0.4 挡掉"短标题是长标题前缀"的偶然命中（3/12 更像"分类名 + 具体做法"）。
MIN_PREFIX_RATIO = 0.4

# 反向 / 程度对照类的修饰语。插进标签的若是这一类字，两条标签描述的是**两个并列
# 方向**而不是同一个东西，不报。依据是全库 38 张真实生成图上的实测：纯编辑距离
# 判据一共命中 43 对，其中 40 对是平行/对立命名（「监督学习」/「无监督学习」3 张图、
# 「从重处罚的适用」/「从轻处罚的适用」、「分类任务选算法」/「回归任务选算法」、
# 「业务指标监控」/「技术指标监控」、「第一条分支」/「第二条分支」…），
# 真正近义的 **0 对**。合并掉「监督学习」与「无监督学习」是丢内容，不是去重。
CONTRASTING_MODIFIERS = frozenset("无非不未否反逆伪假轻重低高多少大小弱强")

# 「这一支还没展开」的对比门槛：同级里有一支**直接到底**，而最深的同级还有至少
# 3 层。为什么是 3 而不是 2：3 层的图里"要点有一两个孩子"会形成 1 对 2 的对比，
# 那是正常密度；1 对 3 意味着这一支比兄弟少了两个层级，那才是"分片展开时这一支
# 没跑成"的现场。
MIN_SHALLOW_SIBLING_HEIGHT = 3

# 「这一支可能只是个标题」只在**别的一级分支更深**时才报。对比才携带信息：
# 全图都是两层时报它等于替用户评价文风（导入的 Markdown 大纲大多长这样）。
# 参照高度与上一条同口径。
MIN_HEADING_REFERENCE_HEIGHT = 3

# 「这 N 个节点没有说明」的条数下限：一两个叶子缺说明是正常的编辑中间态，
# 从 3 个起才是"说明阶段没成批跑"。
MIN_MISSING_DETAIL = 3

# 汇总型诊断的 `evidence` 最多放几个 id。`Diagnostic.node_id` 只能装一个节点，
# 而"缺说明"是一条**汇总**观察：逐条报 20 个叶子会把前端诊断面板刷满
# （`DIAGNOSTIC_LIMIT` 只列前三条）。给前几个 id，既能点回去又不刷屏。
MAX_EVIDENCE_IDS = 8

# ---------------------------------------------------------------- 模型层配置

# 唯一的成本开关，缺省关：这一层要一次网络调用，而"没配模型"在设备上是常态
# （`MINDMAP_LLM_*` 三个变量缺一个就连不上）。
LLM_ENV = "MINDMAP_OPTIMIZATION_LLM"

# 参与比较的同级节点总数下限。低于它不跑模型：8 个节点最多十几对标签，人扫一眼
# 就完了，一次网络调用的延迟换不来信息量。
MIN_LLM_SIBLINGS = 8

# 发给模型的标签上限。按遍历顺序截断：大图上"人更需要机器帮看"这个直觉恰好会让
# 一次同步请求的成本失控。
MAX_LLM_LABELS = 120

# 模型一次最多采纳几对。它偶尔会把并列的近亲概念（监督学习/无监督学习）算成冗余；
# 封顶让最坏情况也只多出 10 条待确认建议。
MAX_LLM_PAIRS = 10

# 单次调用超时。这个数由**交互形态**决定，不是模型能力：`/suggest` 是用户点了
# 才发的同步请求，等十几秒已经比"没有这条建议"更糟。实测见 `LLM_TIMEOUT_MEASURED`。
LLM_TIMEOUT_SECONDS = 20.0

#: 实测记录（DeepSeek `deepseek-chat`，直连）：43 节点的「考研数学复习计划」
#: 一次 1.4–1.8 s，输出 ~40 token。超时值是它的十倍有余，只在网络抽风时兜底。
LLM_TIMEOUT_MEASURED = "1.4–1.8 s / 43 节点 1 次调用"

# 响应体上限，与仓库里其它模型出口同口径（跑飞的响应不该把内存读满）。
_MAX_RESPONSE_BYTES = 1_000_000

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}

# 引用类诊断的前缀。这类码只给诊断、不给建议（理由见模块 docstring）。
_REFERENCE_CODE_PREFIXES = (
    "SOURCE_", "UNSOURCED_", "UNVERIFIED_", "INVALID_", "EMPTY_",
)

# 诊断码 → 建议动作。**动作名词**（`dedupe` / `expand` / `add_detail`），不是
# 诊断码的复述：用户看到的是"点一下会发生什么"。
_SUGGESTION_OF_CODE: dict[str, SuggestionKind] = {
    "DUPLICATE_LABEL": SuggestionKind.dedupe,
    "NEAR_DUPLICATE_LABEL": SuggestionKind.dedupe,
    "SINGLE_CHILD": SuggestionKind.expand,
    "SHALLOW_BRANCH": SuggestionKind.expand,
    "HEADING_ONLY_BRANCH": SuggestionKind.merge,
    "MISSING_DETAIL": SuggestionKind.add_detail,
}

# 建议文案的**动作**部分。诊断说"哪里不对"，建议说"点了会怎样"。
_ACTION_OF_CODE: dict[str, str] = {
    "DUPLICATE_LABEL": "合并这两个同级节点，保留一个",
    "NEAR_DUPLICATE_LABEL": "确认是不是同一件事：是就合并，不是就把说法区分开",
    "SINGLE_CHILD": "把这一支再展开一层，或者并进相邻节点",
    "SHALLOW_BRANCH": "把这一支补到与其他分支相近的深度",
    "HEADING_ONLY_BRANCH": "这一支可能只是个标题：并进父节点，或者补出真正的内容",
    "MISSING_DETAIL": "给这些叶子节点各补一句说明",
}

# 故意不转建议的码 → 理由。有用例钉着"检查器能产出的每一个码，要么在
# `_SUGGESTION_OF_CODE` 里，要么在 `NOT_SUGGESTED` 里"，免得以后新增一条诊断
# 然后忘了决定它转不转。
NOT_SUGGESTED: dict[str, str] = {
    "DEPTH_LIMIT": (
        "「已到 8 层」不是一个可执行的动作：契约禁止第 9 层，所以没有"
        "「再展开一层」可做；而「减一层」要删的是整条中间层，那是重写一支，"
        "不是对某个节点的编辑"
    ),
    "CROSS_BRANCH_UNLINKED": (
        "两个动作都可能对，而选哪个要看**材料里这件事出现了几次**——那是这一层"
        "拿不到的信息（判据只看得见树上写了什么）。并成一处会丢一个视角（材料本身"
        "两处时），加一条关系则保留了那个视角但要求读者读一根虚线。"
        "交给人：诊断把「这一对没被指认」说出来就够了，"
        "`docs/CROSS-BRANCH-DUPLICATES.md` §一 有 a / b / c 三档的判法"
    ),
}


def _iter_nodes(node: Node, depth: int = 1) -> Iterator[tuple[Node, int]]:
    yield node, depth
    for child in node.children:
        yield from _iter_nodes(child, depth + 1)


def _subtree_height(node: Node) -> int:
    """以 `node` 为根的子树的层数（叶子 = 1）。"""
    return 1 + max((_subtree_height(child) for child in node.children), default=0)


def _has_body(node: Node) -> bool:
    """这个节点有没有正文。`detail` 是生成的、`note` 是用户自己写的，界面上分区
    展示，两者都算"有说明"——只认 `detail` 会把"用户已经补过备注"报成空白。"""
    return bool((node.detail or "").strip() or (node.note or "").strip())


def _normalize_label(text: str) -> str:
    """归一化到"只留文字"：全半角、大小写、空白、标点符号全部抹平。

    NFKC 把全角折成半角（`Ａ`→`A`、`（`→`(`），`casefold` 处理拉丁文大小写，
    `P*`（标点）与 `S*`（符号）按**类别**整类删——枚举字符必然漏（半角括号与
    全角方括号在 NFKC 之后也不同形）。
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        char for char in folded
        if not char.isspace() and not unicodedata.category(char).startswith(("P", "S"))
    )


def _inserted_text(shorter: str, longer: str) -> str | None:
    """`longer` 是否只是 `shorter` 中间插了几个字；是则返回插入的那一段。

    返回 None 表示不是"纯插入"关系（有替换、或者顺序变了）。这一判定比编辑距离
    严格得多，而严格正是这里的要点：替换型差异（重→轻、分类→回归、题→模）在真实
    图上几乎全是**平行命名**，把「从重处罚的适用」与「从轻处罚的适用」判成重复，
    用户点一下就会丢一条刑法情节。
    """
    if len(longer) <= len(shorter):
        return None
    head = 0
    while head < len(shorter) and shorter[head] == longer[head]:
        head += 1
    tail = 0
    while (tail < len(shorter) - head
           and shorter[-1 - tail] == longer[-1 - tail]):
        tail += 1
    if head + tail != len(shorter):
        return None          # 中间还有对不上的部分 → 不是纯插入
    return longer[head:len(longer) - tail]


def _near_reason(left: str, right: str) -> tuple[DiagnosticLevel, str] | None:
    """两条同级标签"近义"到什么程度；不近义返回 None。

    判据按可信度从高到低排，命中即返回：

      1. 归一化后相同（只差标点/空格/全半角）→ **warn**：字面已经一样，只剩排版；
      2. 一方是另一方的前缀 → **info**，「监督学习」/「监督学习与无监督学习」；
      3. 一方是另一方**插入**少量限定词的结果 → **info**，「数据处理」/「数据预处理」、
         「监督学习」/「有监督学习」。插入的词属于反向/程度对照类
         （`CONTRASTING_MODIFIERS`）时不报——那是两个并列方向。

    2、3 只给 info 而不是 warn，因为它们**也可能是上下位关系**（「解方程」/
    「解方程组的」）：字面包含是事实，"是不是同一件事"还得人看一眼。warn 留给
    "字面已经相同"这一档。

    **替换型差异一律不报**，这是全库实测的结论而不是保守：38 张真实生成图上，
    带替换的命中 40 对全是平行/对立命名，真正近义 0 对。换字型近义（评估/评价、
    整理/管理）留给模型层——规则层看不见"两个字是不是同义词"。

    调用方负责先排掉"逐字相同"的那一对：那是 `DUPLICATE_LABEL` 的地盘，
    同一个节点不能既报重复又报近义。
    """
    left_raw, right_raw = left.strip(), right.strip()
    if left_raw == right_raw:
        return None
    left_norm, right_norm = _normalize_label(left_raw), _normalize_label(right_raw)
    if left_norm and left_norm == right_norm:
        return DiagnosticLevel.warn, "去掉标点与空格后完全相同"

    shorter, longer = sorted((left_norm, right_norm), key=len)
    if len(shorter) < MIN_FUZZY_CHARS:
        return None
    if longer.startswith(shorter) and len(shorter) / len(longer) >= MIN_PREFIX_RATIO:
        return (DiagnosticLevel.info,
                f"文字上「{shorter}」是「{longer}」的前缀")

    inserted = _inserted_text(shorter, longer)
    if inserted is None:
        return None
    if len(inserted) > MAX_EDIT_DISTANCE:
        return None
    if len(inserted) / len(shorter) > MAX_EDIT_RATIO:
        return None
    if set(inserted) & CONTRASTING_MODIFIERS:
        return None
    return DiagnosticLevel.info, f"只多了「{inserted}」这 {len(inserted)} 个字"


def _shape_diagnostics(root: Node) -> list[Diagnostic]:
    """形态类：单子节点 / 一级分支只有一个叶子 / 到深度上限。

    **一个节点最多一条形态诊断**（有用例钉着），所以 `HEADING_ONLY_BRANCH` 是
    `SINGLE_CHILD` 的替换而不是追加：两者判据都落在"恰好一个子节点"上，差别只在
    解释——第 2 层（一级分支）那个唯一的子节点还是叶子时，"这更像个标题"比
    "这一支没展开"更贴切，动作也不同（归并 vs 展开）。

    并且它只在**其他一级分支更深**时才升级成 `HEADING_ONLY_BRANCH`：全图一样浅
    时这个形状是风格不是缺陷，留在 `SINGLE_CHILD`。
    """
    reference = max((_subtree_height(branch) for branch in root.children), default=1)
    diagnostics: list[Diagnostic] = []
    for node, depth in _iter_nodes(root):
        if len(node.children) == 1:
            only_child = node.children[0]
            is_heading = (depth == 2 and not only_child.children
                          and reference >= MIN_HEADING_REFERENCE_HEIGHT)
            if is_heading:
                diagnostics.append(Diagnostic(
                    level=DiagnosticLevel.info,
                    code="HEADING_ONLY_BRANCH",
                    node_id=node.id,
                    message=(f"一级分支「{node.label}」只有一个子节点「{only_child.label}」，"
                             f"而其他分支最深的还有 {reference} 层：这一支可能只是个标题"),
                    evidence=only_child.id,
                ))
            else:
                diagnostics.append(Diagnostic(
                    level=DiagnosticLevel.info,
                    code="SINGLE_CHILD",
                    node_id=node.id,
                    message=f"节点「{node.label}」只有一个子节点，可考虑展开或合并",
                ))
        if depth >= MAX_TREE_DEPTH:
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn,
                code="DEPTH_LIMIT",
                node_id=node.id,
                message=f"节点「{node.label}」已到 {MAX_TREE_DEPTH} 层上限",
            ))
    return diagnostics


def _duplicate_diagnostics(root: Node) -> list[Diagnostic]:
    """只比较同级节点，不同分支的相同文字可能含义不同。"""
    diagnostics: list[Diagnostic] = []
    for parent, _ in _iter_nodes(root):
        seen: dict[str, str] = {}
        for node in parent.children:
            key = node.label.strip()
            if key in seen:
                diagnostics.append(Diagnostic(
                    level=DiagnosticLevel.warn,
                    code="DUPLICATE_LABEL",
                    node_id=node.id,
                    message=f"同级节点「{key}」与 {seen[key]} 文字相同，请确认是否重复",
                    evidence=seen[key],
                ))
            else:
                seen[key] = node.id
    return diagnostics


def _near_duplicate_diagnostics(root: Node) -> list[Diagnostic]:
    """同级**近义**冗余（规则层，零成本）。

    报告在**后一个**节点上（`node_id` = 后者、`evidence` = 前者的 id），与
    `DUPLICATE_LABEL` 同口径：一条建议只需要一个"要动它"的落点。
    """
    diagnostics: list[Diagnostic] = []
    for parent, _ in _iter_nodes(root):
        children = parent.children
        for index, right in enumerate(children):
            for left in children[:index]:
                reason = _near_reason(left.label, right.label)
                if reason is None:
                    continue
                level, why = reason
                diagnostics.append(Diagnostic(
                    level=level,
                    code="NEAR_DUPLICATE_LABEL",
                    node_id=right.id,
                    message=(f"同级节点「{right.label.strip()}」与「{left.label.strip()}」"
                             f"{why}，请确认是否指的是同一件事"),
                    evidence=left.id,
                ))
    return diagnostics


def _shallow_branch_diagnostics(root: Node) -> list[Diagnostic]:
    """「这一支还没展开」：同级里一支直接到底，别的最少还有三层。

    与 `SINGLE_CHILD` 不重叠——这条要求父节点**至少两个**子节点，单子节点那条
    要求恰好一个，两者互斥。
    """
    diagnostics: list[Diagnostic] = []
    for node, _ in _iter_nodes(root):
        if len(node.children) < 2:
            continue
        heights = {child.id: _subtree_height(child) for child in node.children}
        deepest = max(heights.values())
        if deepest < MIN_SHALLOW_SIBLING_HEIGHT:
            continue
        for child in node.children:
            if heights[child.id] != 1:
                continue
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.info,
                code="SHALLOW_BRANCH",
                node_id=child.id,
                message=(f"「{child.label}」这一支还没展开：它是「{node.label}」下的叶子，"
                         f"而同级最深的一支往下还有 {deepest} 层"),
                evidence=node.label,
            ))
    return diagnostics


def _missing_detail_diagnostics(root: Node) -> list[Diagnostic]:
    """「这 N 个节点没有说明」——**汇总一条**，不逐节点报。

    判据是**部分覆盖**，"有叶子空着"不算：

      · 全空（叶子一个都没说明）不报。那是导入的图（Markdown/OPML 只有标题，
        带 `>` 引用块的才有 `detail`）或者"说明阶段根本没跑"的形态——界面上整张
        图都没有正文，一眼可见，能做的动作也不是逐节点补而是重跑说明阶段。
        对每张导入图都喊一次"没有说明"，这个面板就废了。
      · 全满不报。
      · 只有"写了但没写全"才报：漏掉的那几个是肉眼看不出来的。
    """
    leaves = [node for node, _ in _iter_nodes(root) if not node.children]
    empty = [node for node in leaves if not _has_body(node)]
    if len(empty) < MIN_MISSING_DETAIL or len(empty) == len(leaves):
        return []
    sample = "、".join(f"「{node.label}」" for node in empty[:3])
    return [Diagnostic(
        level=DiagnosticLevel.info,
        code="MISSING_DETAIL",
        node_id=None,
        message=f"这 {len(empty)} 个叶子节点没有说明（如 {sample}），读者看不出它们各自要做什么",
        evidence=" ".join(node.id for node in empty[:MAX_EVIDENCE_IDS]),
    )]


# ---------------------------------------------------------------- 跨支同核（未指认）
#
# 「同一件事被写成两处」有两种形态，判据不同、读者不同：
#
#   · **逐字同名**（只差空白）→ `CROSS_BRANCH_DUPLICATE`（`agent/tools.py` 的
#     `_cross_branch_duplicates`）。它说的是"这句话在图里出现了两遍"。
#   · **近义改写**（「概述 4 学时」/「第一部分 概述」/「概述4学时」）→ **本段**。
#     它说的是"这两处你打算怎么处理，图上读不出来"。
#
# 为什么非要第二条：三次真跑（`docs/CROSS-BRANCH-DUPLICATES.md` §八）里，加了交叉
# 指认规则之后 red 从 6 降到 0，而 red 归零**不是**因为第二处消失了——模型把 label
# 改写成近义（逐字判据当场失明），再补一条 `relations` 说"这两处是一回事"。于是
# "red = 0"与"仍有两份同一件事"同时成立，那是最容易被读成成功的一种假进步。这一条
# 判据就是那个空洞的探针，所以它**只报被改写过的那一档**：逐字那档已经有人报，
# 两条码不许落在同一对上（`test_cross_branch_duplicate_and_unlinked_do_not_overlap`）。
#
# 判据的形态是"剥掉序号前缀与量化尾缀后相等"，不是相似度阈值。阈值版（2-gram
# Dice ≥ 0.5）在库里 28 棵树上实测过：假阳性 210 对、真命中 22 对——「第二章 8
# 学时」与「第五章 6 学时」、「WebSocket」与「Socket 编程」都被判成同一件事。
# 剥前缀/尾缀这一版同一批树下沉到 8 对，且与人判逐条对得上（S02 8 处、S08 3 处、
# S17 1 处、S05 1 处，见 §一 的 17 处分类）。
#
# 为什么在**原始 label** 上剥而不是在 `_normalize_label` 之后：归一化会删掉所有标点
# （顿号、冒号、括号），而序号前缀全靠那些标点划界。「一、本次评审范围」剥完标点变成
# "一本次评审范围"，再想剥"一、"就只剩一个裸"一"字，那会把「一般原理」剥成「般原理」。
_CORE_PREFIX = re.compile(
    r"^\s*(?:"
    r"第\s*[一二三四五六七八九十百零〇0-9]+\s*(?:部分|章|编|节|讲|单元|模块|课)"
    r"|[一二三四五六七八九十]+\s*[、.．]"
    r"|M\s*[0-9]+\s*[:：.．]"
    r")\s*"
)

# 量化尾缀：**数字 + 单位**，可带括号、可连续多个（「各 8 学时」「（7 次实验）」）。
# 单位表只收"这件事的量"，不收"这件事是什么"：漏掉一个单位只是少配一对（判据是提示
# 不是判决），而多收一个字（比如把「的分配」也当尾缀）会把「总学时 40 的分配」剥成空。
_CORE_SUFFIX = re.compile(
    r"(?:\s*[（(]?\s*[0-9０-９]+(?:\.[0-9]+)?\s*"
    r"(?:学时|课时|小时|次|个|人|天|周|月|年|页|分|％|%|元|万元)"
    r"\s*[)）]?\s*)+$"
)

# core 的下限。**2**，不是 3：`MIN_FUZZY_CHARS` 那个 3 是编辑距离比较用的（2 个字之间
# 换一个字就换掉一半，那是两个概念），而这里剥完只剩 core，不再做模糊比较。压到 3 会
# 漏掉「概述」这种 2 字标题——S02 那 8 处里第一处就是它。
MIN_CORE_CHARS = 2


def _core_label(text: str) -> str:
    """剥掉序号前缀与量化尾缀之后的"核心词"。剥完再归一化（标点/空白/全半角）。

    **剥空了就退回归一化的原文**，不返回空串：整串都是"序号 + 数量"的标题（「第二章
    8 学时」）剥完什么都不剩，而空串与空串相等——那会让「第二章 8 学时」与「第五章
    6 学时」被判成同一件事，恰好是相似度判据栽的那个坑。退回原文之后它们各自是
    「第二章8学时」/「第五章6学时」，不同。
    """
    stripped = (text or "").strip()
    if not stripped:
        return ""
    prev = None
    while prev != stripped:
        prev = stripped
        stripped = _CORE_PREFIX.sub("", stripped)
        stripped = _CORE_SUFFIX.sub("", stripped)
    core = _normalize_label(stripped)
    return core or _normalize_label((text or "").strip())


def _declared_pairs(content: MindMapContent) -> set[frozenset[str]]:
    """`relations` 已经指认过的节点对（**方向无关**：指认就是指认）。"""
    pairs: set[frozenset[str]] = set()
    for relation in content.relations:
        if relation.source and relation.target:
            pairs.add(frozenset((relation.source, relation.target)))
    return pairs


def _cross_branch_unlinked_diagnostics(content: MindMapContent) -> list[Diagnostic]:
    """跨支"同一件事"但没有任何 `relations` 指认 → 一条汇总 warn。

    三条边界，每条都对应一种真实的误报：

      1. **同父不算跨支**：同级的重复是 `DUPLICATE_LABEL` / `NEAR_DUPLICATE_LABEL`
         的地盘，两条判据落在同一对上是重复报。
      2. **同一条祖先链不算跨支**：父子同名是"父节点既当条目又当分组"（S07 的
         「力矩与转动定律」），要改的是层级，不是加一条关系。
      3. **归一化后逐字相同的不报**：那一档归 `CROSS_BRANCH_DUPLICATE`。这一条是
         本判据存在的理由（只报"被改写成近义"的那一档），所以它必须真的只管那一档。

    聚合一条而不是逐对报：S02 那种材料上一次能出 8 对，逐对报会把整个面板淹掉
    （与 `MISSING_DETAIL` 同一条取舍）。
    """
    declared = _declared_pairs(content)
    buckets: dict[str, list[tuple[Node, Node | None, frozenset[str]]]] = {}
    order: list[str] = []

    def walk(node: Node, parent: Node | None, ancestors: frozenset[str]) -> None:
        core = _core_label(node.label)
        if len(core) >= MIN_CORE_CHARS:
            if core not in buckets:
                buckets[core] = []
                order.append(core)
            buckets[core].append((node, parent, ancestors))
        below = ancestors | {node.id}
        for child in node.children:
            walk(child, node, below)

    walk(content.root, None, frozenset())

    found: list[tuple[Node, Node]] = []
    seen: set[frozenset[str]] = set()
    for core in order:
        group = buckets[core]
        for index, (left, left_parent, left_anc) in enumerate(group):
            for right, right_parent, right_anc in group[index + 1:]:
                if left_parent is not None and left_parent is right_parent:
                    continue
                if right.id in left_anc or left.id in right_anc:
                    continue
                if (_normalize_label(left.label) == _normalize_label(right.label)):
                    continue          # 逐字同名：CROSS_BRANCH_DUPLICATE 的地盘
                pair = frozenset((left.id, right.id))
                if pair in declared or pair in seen:
                    continue
                seen.add(pair)
                found.append((left, right))

    if not found:
        return []
    shown = "；".join(
        f"「{left.label.strip()}」与「{right.label.strip()}」" for left, right in found[:5]
    )
    return [Diagnostic(
        level=DiagnosticLevel.warn,
        code="CROSS_BRANCH_UNLINKED",
        node_id=None,
        message=(
            f"有 {len(found)} 处同一件事写在了两条分支下，而图里没有一条关系指认它们"
            f"（{shown}{'…' if len(found) > 5 else ''}）：读者要自己判断这两处是不是"
            "一回事。要么并成一处，要么加一条 references 并在 why 里说清它们的关系"
        ),
        evidence=" ".join(
            node_id for left, right in found[:MAX_EVIDENCE_IDS] for node_id in (left.id, right.id)
        ),
    )]


def check(content: MindMapContent, sources: list[Source] | None = None) -> list[Diagnostic]:
    """结构诊断；只有显式提供原文时才执行引用校验。

    严格确定性：同一个输入永远同样的诊断与顺序。模型层不在这一层——`check()`
    会被生成收尾反复调用，也可能被前端缓存。
    """
    root = content.root
    diagnostics: list[Diagnostic] = []
    diagnostics.extend(_shape_diagnostics(root))
    diagnostics.extend(_duplicate_diagnostics(root))
    diagnostics.extend(_near_duplicate_diagnostics(root))
    diagnostics.extend(_shallow_branch_diagnostics(root))
    diagnostics.extend(_missing_detail_diagnostics(root))
    diagnostics.extend(_cross_branch_unlinked_diagnostics(content))
    if sources is not None:
        diagnostics.extend(verify_source_refs(content, sources))
    return diagnostics


def suggest(
    content: MindMapContent, *, use_model: bool | None = None,
    use_cross_map: bool | None = None, map_id: str | None = None,
) -> list[Suggestion]:
    """把诊断结果转成用户可确认的候选修改。

    `use_model`：`None` 走环境变量（`MINDMAP_OPTIMIZATION_LLM`，缺省关），
    `True` / `False` 显式开关（调用方与测试用）。关掉之后这一层**一次调用都不
    发生**，也不需要网络。

    `use_cross_map` / `map_id`：第四层判据（跨图参照，见
    `optimization/cross_map.py`）。同样是 `None` 走环境变量、缺省关；
    `map_id` 只用来把"这张图自己"从历史库里排除。

    建议不自动应用——见模块 docstring 的两条路。
    """
    suggestions: list[Suggestion] = []
    rule_pairs: set[frozenset[str]] = set()
    for diag in check(content):
        kind = _SUGGESTION_OF_CODE.get(diag.code)
        if kind is None:
            continue
        if diag.code == "NEAR_DUPLICATE_LABEL" and diag.evidence:
            rule_pairs.add(frozenset((diag.node_id or "", diag.evidence)))
        suggestions.append(Suggestion(
            kind=kind,
            node_id=diag.node_id,
            message=f"{_ACTION_OF_CODE[diag.code]}：{diag.message}",
            evidence=diag.evidence,
        ))

    if _should_use_model(content, use_model):
        suggestions.extend(_model_suggestions(content, rule_pairs))

    # 延迟导入：关闭时 `cross_map` 与它依赖的 numpy / 数据库都不进 import 图
    # （与模型层"关闭即零开销"同一条规矩）。
    from app.optimization import cross_map

    enabled = (use_cross_map if use_cross_map is not None else cross_map.enabled())
    if enabled:
        suggestions.extend(cross_map.suggestions(content, map_id=map_id, enabled=True))
    return suggestions


# ---------------------------------------------------------------- 模型层


def _llm_enabled() -> bool:
    return str(os.getenv(LLM_ENV, "")).strip().lower() in _TRUTHY


def _sibling_groups(
    root: Node, *, limit: int | None = None,
) -> list[list[tuple[str, str]]]:
    """参与模型比较的同级组，每组是 `(标签, 节点 id)`，顺序即发给模型的行号。

    标签与 id 一次取出、不再重走一遍树：`_model_suggestions` 要按行号把答案换回
    节点，而两次独立遍历只要有一处 `limit` 截断不同，行号就会错位——那种错位
    表现是"建议指向了别的分支"，看不出来是 bug。

    `limit` 按遍历顺序截到标签总量不超过它：`/suggest` 是同步请求，标签越多 token
    越多，而"这张图有 300 个节点"那种情况本来也该先分片体检。
    """
    groups: list[list[tuple[str, str]]] = []
    total = 0
    for node, _ in _iter_nodes(root):
        items: list[tuple[str, str]] = []
        for child in node.children:
            label = child.label.strip()
            if label and label not in {item[0] for item in items}:
                items.append((label, child.id))
        if len(items) < 2:
            continue
        if limit is not None:
            room = limit - total
            if room < 2:
                break
            items = items[:room]
        groups.append(items)
        total += len(items)
        if limit is not None and total >= limit:
            break
    return groups


def _sibling_label_groups(root: Node, *, limit: int | None = None) -> list[list[str]]:
    """`_sibling_groups` 的标签视图（测量脚本与"要不要跑模型"的判断用）。"""
    return [[label for label, _ in group]
            for group in _sibling_groups(root, limit=limit)]


def _should_use_model(content: MindMapContent, use_model: bool | None) -> bool:
    """这一次要不要跑模型。三个条件缺一不可，前一个是成本闸、后一个是规模闸。

      1. 开关（参数优先，其次环境变量，缺省关）；
      2. 有可比较的同级组——整棵树全是单子节点链时没有"同级"可言；
      3. 参与比较的同级节点总数 ≥ `MIN_LLM_SIBLINGS`。小图上人一眼扫完，
         一次网络调用的延迟换不来信息量。
    """
    if not (use_model if use_model is not None else _llm_enabled()):
        return False
    groups = _sibling_label_groups(content.root, limit=MAX_LLM_LABELS)
    return sum(len(group) for group in groups) >= MIN_LLM_SIBLINGS


def _model_near_pairs(groups: list[list[str]]) -> list[tuple[int, str, str, str]]:
    """一次 LLM 调用。返回 `(行号, 标签A, 标签B, 理由)`，行号从 0 起；失败返回空。

    为什么让模型做**确认**而不是读整棵树：规则层已经把"字面上像"的全抓了，留给
    模型的只剩"字面上不像、语义上是同一件事"那一小撮（正则化/规范化、降维/维数
    约简）。所以只喂同级标签，并要求它**按行**回答。

    按行（行号进、行号出）不是美学选择。第一版发的是嵌套 JSON、只要标签对，结果
    在两张真实图上模型的 11 对里 **11 对全部跨行**：它把"父标签 + 子标签"当成
    "说的是同一件事"（「按任务选模型族」/「按输出类型匹配模型族」正是父子），
    而那两条根本不是同级冗余。行号把"同一行内"变成它的输出约束，调用方按行号
    取节点，就不再需要事后猜"这两个标签是不是同级"。

    **绝不抛异常**（与 `optimization/nli.py` 同一条硬约束）：体检是附加信息，
    模型服务挂了不该让 `/suggest` 变成 500。配置缺失、网络错误、非 JSON 输出、
    响应过大，全部收敛成"这一层没有结论"。
    """
    if not groups:
        return []
    # 延迟导入：关闭时连 `generation` 那一坨都不进 import 图（与 nli.py 同一条
    # "关闭即零开销"的规矩）。配置口径也必须复用 `_config()`——两处各读一遍环境
    # 变量，迟早出现"生成能连、体检连不上"的分歧。
    from app.generation.service import GenerationError, _config, parse_model_json

    try:
        base, key, model = _config()
    except GenerationError:
        return []

    system = (
        "你是中文思维导图的结构审校员。用户按行给出若干组标签，每行一组，行号从 1 "
        "开始，行内是**同一个父节点下的所有子节点标签**，用 | 分隔。\n"
        "任务：只在**同一行内部**找**重复**的标签对——两个标签在读者看来指的是同一"
        "件事，只留一个不丢信息。判断标准是这一问：**把这两个合成一个，读者会丢掉"
        "一个独立的信息点吗？** 会丢 → 不是重复，不要报。\n"
        "不要报的情况（都比「重复」常见）：同一主题的两个不同方面（「定义与基本概念」"
        "与「理论基础」）、上下位、因果、并列的近亲概念（监督学习 / 无监督学习）、"
        "同一模板的两个实例（按题型分配 / 按模块分配）、一个是对另一个的概述。\n"
        "不同行之间永远不算：它们不在同一个父节点下，含义可以不同。\n"
        "只输出 JSON，不要解释：{\"pairs\": [{\"line\": 1, \"a\": \"标签原文\", "
        "\"b\": \"标签原文\", \"why\": \"合并之后为什么不会丢信息\"}]}；一对都没有就"
        "输出 {\"pairs\": []}。a、b 必须逐字出现在该行里。"
    )
    lines = [f"{index}: {' | '.join(group)}" for index, group in enumerate(groups, 1)]
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": "\n".join(lines)}],
        # 0 温：同一张图点两次检查该给同一批建议，否则用户会以为是随机的。
        "temperature": 0.0,
        "max_tokens": 512,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=LLM_TIMEOUT_SECONDS, follow_redirects=True) as client:
            with client.stream("POST", base + "/chat/completions",
                               content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                               headers=headers) as response:
                response.raise_for_status()
                buffer = bytearray()
                for chunk in response.iter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > _MAX_RESPONSE_BYTES:
                        return []
    except (httpx.HTTPError, OSError):
        return []

    try:
        text = json.loads(buffer)["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return []
    try:
        parsed = parse_model_json(text or "")
    except GenerationError:
        return []
    pairs = parsed.get("pairs") if isinstance(parsed, dict) else None
    if not isinstance(pairs, list):
        return []

    findings: list[tuple[int, str, str, str]] = []
    for item in pairs:
        if not isinstance(item, dict):
            continue
        line, left, right = item.get("line"), item.get("a"), item.get("b")
        if not isinstance(left, str) or not isinstance(right, str):
            continue
        left, right = left.strip(), right.strip()
        if not left or not right or left == right:
            continue
        # 行号可以是数字也可以是数字字符串（模型两种都写）。
        try:
            index = int(line) - 1
        except (TypeError, ValueError):
            # 没给行号的答案不采纳：行号是"这两个是同级"的唯一凭据。
            continue
        why = item.get("why")
        findings.append((index, left, right, why if isinstance(why, str) else ""))
        if len(findings) >= MAX_LLM_PAIRS:
            break
    return findings


def _group_nodes(content: MindMapContent, limit: int) -> dict[int, dict[str, str]]:
    """行号 → `{标签: 节点 id}`。与 `_sibling_groups` 同一份遍历结果。"""
    return {
        index: {label: node_id for label, node_id in group}
        for index, group in enumerate(_sibling_groups(content.root, limit=limit))
    }


def _model_suggestions(
    content: MindMapContent, rule_pairs: set[frozenset[str]],
) -> list[Suggestion]:
    """模型层 → `dedupe` 建议。

    四条过滤，缺一条就会被模型的越界答案污染：

      1. 行号必须落在发出去的行里，且两个标签**逐字属于**那一行（模型会答父标签
         或别的行，实测第一版 11/11 都是这种）；
      2. 规则层已经报过的那一对不重复报（同一个问题两条建议，用户点两次）；
      3. 同一个节点最多落一条建议——一次合并只能有一个落点。

    `confidence` 保持 None：模型给的是判定，不是校准过的概率（契约注释里那句
    "规则建议没有经过概率校准"同样适用）。
    """
    groups = _sibling_label_groups(content.root, limit=MAX_LLM_LABELS)
    findings = _model_near_pairs(groups)
    if not findings:
        return []

    members = _group_nodes(content, MAX_LLM_LABELS)
    suggestions: list[Suggestion] = []
    taken: set[str] = set()
    for index, left, right, why in findings:
        ids = members.get(index, {})
        if left not in ids or right not in ids:
            continue
        left_id, right_id = ids[left], ids[right]
        if left_id == right_id or frozenset((left_id, right_id)) in rule_pairs:
            continue
        if left_id in taken or right_id in taken:
            continue
        taken.update((left_id, right_id))
        reason = f"（模型判定：{why}）" if why else ""
        suggestions.append(Suggestion(
            kind=SuggestionKind.dedupe,
            node_id=right_id,
            message=(f"{_ACTION_OF_CODE['NEAR_DUPLICATE_LABEL']}："
                     f"同级节点「{right}」与「{left}」近义{reason}"),
            evidence=left_id,
        ))
    return suggestions


def _not_converted_reason(code: str) -> str | None:
    """这个诊断码为什么不转建议（`None` 表示它其实该转）。测量脚本用它解释缺口。"""
    if code in NOT_SUGGESTED:
        return NOT_SUGGESTED[code]
    if code.startswith(_REFERENCE_CODE_PREFIXES):
        return "引用类诊断：只说明文献是否可核验，动作是补证据而不是合并/展开"
    return None


# ---------------------------------------------------------------- link 层（补关系）
#
# 这一层把 `optimization/link.py` 接进产品。它在此之前**一个调用者都没有**：
# 算法、8 条用例、一个能跑通的度量脚本都在，而 `/suggest` 的产出里从来没有
# `kind="link"`——用户点「检查结构」得到的永远只有规则层的 expand/dedupe/add_detail。
# 这不是逻辑写错，是接线没做，所以没有任何用例会变红（用例测的是
# `link.suggest(...)`，不是"它会不会出现在用户面前"）。
#
# 三条设计约束，每条都对着一个具体坏结果：

LINK_TARGET_CODES = ("SINGLE_CHILD", "SHALLOW_BRANCH", "HEADING_ONLY_BRANCH")
"""哪几类诊断说明"这个节点的位置值得问一句"。

选这三类而不是全树逐节点：`link.suggest` **每个目标一次模型调用**（实测
1.4–1.8 s / 次），全树问一遍会把这个按钮变成分钟级操作。而这三类诊断本身说的
就是"这一支的挂法/展开有问题"——正好是"它该挂在谁下面"这个问题的适用面。
"""

MAX_LINK_TARGETS = 1
"""一次 `/suggest` 最多问几个节点。

**1**，不是 2 也不更多。实测（真后端、deepseek-chat、这张库里 80 节点的图）：
一个目标从挑候选到拿到候选约 10 秒，其中大半是那次模型往返。用户点「检查结构」
本来只等几毫秒（规则层是纯函数），把这一层加到上十秒已经接近"比没有这条建议更糟"；
两个目标就是 20 秒，实测过一次 `20906 ms`——那不是一个按钮该有的延迟。
所以要更多建议就多点几次（每次的目标会随诊断变化），而不是让一次点击等更久。
"""

LINK_CONTEXT_ENV = "MINDMAP_OPTIMIZATION_LINK_CONTEXT"
"""link 这一层要不要查"跨图历史"与"库里的材料原文"。

**默认关**，依据是实测而不是省事：`docs/LINK-SUGGESTIONS.md` 的消融把那两样输入
全剥掉之后，藏节点再猜的准确率从 14/16 掉到 13/16——**在噪声里**。而它们各自要
先扫库（`history_placements` 读全部导图 + 一次 embedding；`material_hits` 扫最近
20 份材料 + 一次检索），是这一层里最贵的两段。

所以默认只让模型看"这棵树本身"（真父的标题 + 它的子节点清单 + 分类常识，实测就是
撑住 88% 的那部分）。要复现论文那套完整输入时设 `=1`，读数是同一份文档。
"""


@dataclass(frozen=True)
class LinkRound:
    """link 那一层这一趟的结果（`SuggestResponse.link` 就是它）。"""

    suggestions: list[Suggestion] = field(default_factory=list)
    status: str = "ok"
    targets: list[str] = field(default_factory=list)
    calls: int = 0
    dropped: int = 0


def _link_on(on: bool | None) -> bool:
    """这一层跑不跑。

    **缺省开**，与环境变量 `MINDMAP_OPTIMIZATION_LINK` 的默认值（在
    `link.enabled()` 里是关）故意相反，理由是入口不同：那边的默认值管的是
    "没人要求也自动跑"（生成收尾那种场景，跑一次就是一次真金白银的调用）；
    而这里的唯一入口是用户点了「检查结构」——那一下已经表达了"帮我看这张图"。
    显式设成 `MINDMAP_OPTIMIZATION_LINK=0` 仍然能关掉它（部署方要省这笔钱时）。
    """
    if on is not None:
        return on
    from app.optimization import link

    raw = os.getenv(link.ENV)
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in _TRUTHY


def _link_context_on(on: bool | None) -> bool:
    """这一层要不要查跨图历史与材料原文（`LINK_CONTEXT_ENV`，默认关）。"""
    if on is not None:
        return on
    raw = os.getenv(LINK_CONTEXT_ENV)
    return bool(raw) and raw.strip().lower() in _TRUTHY


def _only_child_id(root: Node, node_id: str) -> str | None:
    """`node_id` 唯一的那个孩子。`SINGLE_CHILD` 的落点是父节点，而"该挂哪"问的是孩子。"""
    stack = [root]
    while stack:
        node = stack.pop()
        if node.id == node_id:
            return node.children[0].id if len(node.children) == 1 else None
        stack.extend(node.children)
    return None


def link_targets(
    content: MindMapContent, diagnostics: list[Diagnostic] | None = None,
    *, limit: int = MAX_LINK_TARGETS,
) -> list[str]:
    """这次该问哪几个节点"它该挂在谁下面"。

    两级取值，顺序就是优先级：

      1. **形态诊断指向的节点**。这些节点的挂法已经可疑，问一句最有价值。注意落点
         要往下挪一层：`SINGLE_CHILD` 说的是"这个父节点只有一个孩子"，而 link 问的
         是**那个孩子**该挂哪——拿父节点去问，回答"它就该在这儿"是必然的。
         `HEADING_ONLY_BRANCH` 的 `evidence` 本来就是那个子节点的 id，直接用。
      2. **一级分支下的第一个叶子**。这一级是兜底，而且不能省：真实生成的图
         多半**一条形态诊断都没有**（实测一棵 37 节点的干净树是 0 条），只靠第 1
         级的话"补关系"在正常图上永远不出现——那又回到了"产品里看不到"。

    根节点不进来：`link.parent_choices` 对根返回零个候选，问它必然 `no_choices`，
    白花一次调用。
    """
    report = check(content) if diagnostics is None else diagnostics
    ordered: list[str] = []
    for diag in report:
        if diag.code not in LINK_TARGET_CODES or not diag.node_id:
            continue
        picked_id = diag.evidence or _only_child_id(content.root, diag.node_id)
        if picked_id:
            ordered.append(picked_id)
    if not ordered:
        for branch in content.root.children:
            for node, _ in _iter_nodes(branch):
                if not node.children:
                    ordered.append(node.id)
                    break
    picked: list[str] = []
    seen: set[str] = set()
    for node_id in ordered:
        if node_id in seen or node_id == content.root.id:
            continue
        seen.add(node_id)
        picked.append(node_id)
        if len(picked) >= limit:
            break
    return picked


def _current_parent_id(root: Node, node_id: str) -> str | None:
    """`node_id` 现在挂在谁下面；不在树上（或它就是根）返回 None。"""
    stack = [root]
    while stack:
        node = stack.pop()
        if any(child.id == node_id for child in node.children):
            return node.id
        stack.extend(node.children)
    return None


def _first_candidate_parent(suggestion: Suggestion) -> str | None:
    """一条 link 建议里模型的**首选父**。

    `evidence` 装的是 `link.suggest` 打好的 JSON（见那边的 docstring：`Suggestion`
    是 `extra="forbid"`，加字段就要重导契约）。解不开就当"没有首选"，不猜。
    """
    raw = suggestion.evidence
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    first = candidates[0]
    if not isinstance(first, dict):
        return None
    parent_id = first.get("parent_id")
    return parent_id if isinstance(parent_id, str) and parent_id else None


_LINK_DETAIL: dict[str, str] = {
    "no_targets": "这张图没有可问的节点（还没有分点），补关系这一层没跑。",
    "disabled": "补关系这一层关掉了（MINDMAP_OPTIMIZATION_LINK=0）。",
    "already_best": "补关系这一层问过了：模型认为这几处现在挂得都对。",
    "no_choices": "目标节点只有一个可挂的位置，没有别的挂法可建议。",
    "no_candidates": "模型这次没给出可用的挂法候选。",
    "model_failed": "补关系这一层没能拿到模型结果，稍后再检查一次。",
    "failed": "补关系这一层出错了，其余建议不受影响。",
}


async def suggest_link(
    content: MindMapContent,
    *,
    map_id: str | None = None,
    targets: list[str] | None = None,
    chat=None,
    on: bool | None = None,
    context: bool | None = None,
) -> LinkRound:
    """对少数几个节点跑 `link.suggest`，把结果整成一层建议。

    **丢"模型说现在挺好"的那些**：`link.parent_choices` 把当前父排在候选表第一位，
    所以"首选父 == 当前父"就是模型在说"不用动"。不丢的话，一张本来没问题的图会
    收到两三条"可以挂到它现在待的地方"的建议——那种建议比没有更糟，用户点一次
    就会开始不信这个面板。

    失败一律降级：`link.suggest` 自己已经兜住模型异常（返回空报告），这里再兜一层
    是给"注入缝之外的意外"（历史库读炸、向量表坏了）留后路——建议是附加信息，
    判不出来不等于这张图坏了。
    """
    from app.optimization import link

    if not _link_on(on):
        return LinkRound(status="disabled")
    picked = list(targets) if targets is not None else link_targets(content)
    if not picked:
        return LinkRound(status="no_targets")

    suggestions: list[Suggestion] = []
    calls = 0
    dropped = 0
    reason = ""
    # 不查跨图历史与材料原文时，直接把两个注入缝传空列表：`link.suggest` 见到
    # 非 None 就**不**去扫库（那是这一层最贵的两段，见 `LINK_CONTEXT_ENV`）。
    blank = {} if _link_context_on(context) else {"history": [], "material": []}
    for node_id in picked:
        try:
            report = await link.suggest(content, node_id, map_id=map_id, chat=chat,
                                        on=True, **blank)
        except Exception:  # noqa: BLE001 — 见 docstring 最后一段
            logger.warning("补关系这一层在节点 %s 上失败", node_id, exc_info=True)
            reason = reason or "failed"
            continue
        calls += report.calls
        if not report.suggestions:
            reason = reason or (report.skipped or "no_candidates")
            continue
        current = _current_parent_id(content.root, node_id)
        best = _first_candidate_parent(report.suggestions[0])
        if current is not None and best == current:
            dropped += 1
            reason = reason or "already_best"
            continue
        suggestions.extend(report.suggestions)

    if suggestions:
        status = "ok"
    elif dropped:
        status = "already_best"
    else:
        status = reason or "no_candidates"
    return LinkRound(suggestions=suggestions, status=status, targets=picked,
                     calls=calls, dropped=dropped)


@dataclass(frozen=True)
class SuggestOutcome:
    """`/optimization/suggest` 的完整产出：建议 + link 那一层的状态。"""

    suggestions: list[Suggestion]
    link: LinkStatus | None


async def suggest_all(
    content: MindMapContent,
    *,
    use_model: bool | None = None,
    use_cross_map: bool | None = None,
    map_id: str | None = None,
    use_link: bool | None = None,
    link_chat=None,
    link_context: bool | None = None,
) -> SuggestOutcome:
    """`suggest()` + link 层。路由只调这一个。

    为什么不是把 `suggest()` 直接改成 `async`：它是**同步契约**，被生成收尾
    （`agent/tools.py`）与十几个测试直接调用；为一个要模型往返的层把它改成
    协程，等于让所有调用方跟着改一遍，而它们既不需要也不能等模型。所以同步的
    那一层原样保留，异步的这一层在外面加——`link` 是唯一需要 await 的部分。
    """
    base = suggest(content, use_model=use_model, use_cross_map=use_cross_map,
                   map_id=map_id)
    # 调用方明确说"这次不要"→ `link` 为 None（"没跑它"）；部署开关关掉时仍然报
    # `disabled`（"跑了，被闸住了"）。两者在界面上要说的话不同：前者不说，后者要
    # 告诉用户这一层被部署方关了。
    if use_link is False:
        return SuggestOutcome(suggestions=base, link=None)
    round_ = await suggest_link(content, map_id=map_id, chat=link_chat, on=use_link,
                                context=link_context)
    detail = "" if round_.status == "ok" else _LINK_DETAIL.get(
        round_.status, "补关系这一层这次没有产出。")
    status = LinkStatus(
        status=round_.status,
        targets=list(round_.targets),
        calls=round_.calls,
        dropped=round_.dropped,
        detail=detail,
    )
    return SuggestOutcome(suggestions=[*base, *round_.suggestions], link=status)
