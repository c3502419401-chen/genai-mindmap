"""跨支重复的**唯一判据**：逐字同名（`CROSS_BRANCH_DUPLICATE`）。

位置的理由是这条判据曾经分居两处，而分居的表现是**同一张图，生成时能发现的问题，
用户事后检查看不见**：

    agent/tools.py     _cross_branch_duplicates    生成链路（`_generate` 收尾追加一条）
    optimization/      （没有）                     /check 与 /suggest 走这里，看不见

「检查与建议」是用户唯一能主动点的自查入口，所以判据的归属地是这里，
`agent/tools.py::_cross_branch_duplicates` 现在是本模块的**薄委托**（保留这个名字
是因为十几个测量脚本与用例按 `from app.agent.tools import _cross_branch_duplicates`
取它，改调用点等于把一份判据的地址抄到十几处）。

======================================================================
两条码的分工，与它们共用的四条边界
======================================================================
「同一件事被写成两处」有两种形态，判据不同、读者不同：

  · **逐字同名**（只差空白）→ `CROSS_BRANCH_DUPLICATE`（本模块）。它说的是
    "这句话在图里出现了两遍"。
  · **近义改写**（「概述 4 学时」/「第一部分 概述」）→ `CROSS_BRANCH_UNLINKED`
    （`service.py`）。它说的是"这两处你打算怎么处理，图上读不出来"。

四条边界**两侧都要守**，所以它们写在这里一次，而不是各写一份：

  1. **同父不算跨支**：同级的重复是 `DUPLICATE_LABEL` / `NEAR_DUPLICATE_LABEL`
     的地盘，两条判据落在同一对上是重复报。
  2. **同一条祖先链不算跨支**：父子同名是"父节点既当条目又当分组"（S07 的
     「力矩与转动定律」），要改的是层级，不是加一条关系。
  3. **逐字档让给 `CROSS_BRANCH_DUPLICATE`**：`CROSS_BRANCH_UNLINKED` 必须真的
     只管"被改写成近义"的那一档，否则两条码说同一件事。
  4. **剥空退回原文**（`_core_label`，在 `service.py`）：整串都是"序号 + 数量"的
     标题剥完什么都不剩，而空串与空串相等——那会让「第二章 8 学时」与「第五章
     6 学时」被判成同一件事，恰好是相似度判据栽的那个坑。

======================================================================
两个"逐字"口径，以及它们之间那一档谁都报的缝
======================================================================
边界 3 说"逐字档让给 DUPLICATE"，而**"逐字"在两侧是两个宽度**，这是刻意的：

    verbatim_key(label)    只折空白               ← DUPLICATE 用它判"是不是同一个说法"
    normalize_label(label) NFKC + 大小写 + 标点符号全删 ← UNLINKED 用它判"该不该让"

窄的在前、宽的在后，于是有一条缝：**只差标点或全半角的两处**（「概述：方法」与
「概述方法」），DUPLICATE 嫌它不够逐字、UNLINKED 认定它属于逐字档——两条码都不报。

这条缝是**选出来的**，不是漏的。填它的办法只有两种，都比缝更糟：让 DUPLICATE 也
做标点归一，等于在一条"宁可漏、不误报"的判据上放低门槛（它的价值全在"值得看一眼"，
误报会把整条信号变成噪声）；让 UNLINKED 收窄到只折空白，等于让它对"只差一个冒号"
的两处喊"你打算怎么处理"——那是半条误报，而它报出来的东西本来是给"被改写成近义"
这一档准备的。所以这一档**静默**，两个口径都写在上面两行签名里，读的人知道界在
哪里。

======================================================================
为什么判据是 label 级、且不做语义归一
======================================================================
**不要求 `detail` 也相同**：库里那三张 `机器学习的主要方向`（56/57 节点）各有 3 个
label 跨支重复、共 6 个重复节点，而它们的 detail **各不相同**——按"连 detail 也要
相同"判，这条诊断对真实缺陷完全瞎掉（量出来 0）。同一个概念挂在两条一级分支下，
无论说明怎么写，用户都会看到两块内容在讲同一件事；而"交叉指认"（两支都要提到同一
个概念）的正确写法是指向已有节点，不是复制一份。

**不做语义归一**：`无监督学习` 与 `非监督学习`、`监督学习` 与 `有监督学习` 不算
重复（不确定就别报）。宁可漏，不误报——这条诊断不删任何节点，只把"哪几句写了两遍"
摆出来，误报会让整条信号变成噪声。

**只报不删**：留哪一处是用户的决定。
"""
from __future__ import annotations

import re
import unicodedata

from app.contracts.diagnostic import Diagnostic, DiagnosticLevel
from app.contracts.mindmap import Node


def verbatim_key(text: str) -> str:
    """逐字档的归一：**只折空白**。`CROSS_BRANCH_DUPLICATE` 的判据就是这个。

    不做标点归一、不做全半角、不做大小写——理由见模块 docstring 最后两段：
    这条判据是"宁可漏、不误报"的那一半。
    """
    return re.sub(r"\s+", "", text or "")


def normalize_label(text: str) -> str:
    """归一化到"只留文字"：全半角、大小写、空白、标点符号全部抹平。

    NFKC 把全角折成半角（`Ａ`→`A`、`（`→`(`），`casefold` 处理拉丁文大小写，
    `P*`（标点）与 `S*`（符号）按**类别**整类删——枚举字符必然漏（半角括号与
    全角方括号在 NFKC 之后也不同形）。

    它是**宽口径**那一侧：近义判据（`service.py` 的 `_near_reason`）、核心词
    （`_core_label`）、以及 `CROSS_BRANCH_UNLINKED` 的"逐字档让位"都用它。
    与 `verbatim_key` 的关系（窄 ⊂ 宽）见模块 docstring。
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        char for char in folded
        if not char.isspace() and not unicodedata.category(char).startswith(("P", "S"))
    )


def cross_branch_duplicates(root: Node) -> list[tuple[str, list[str]]]:
    """跨分支重复的节点：`(label, [出现在哪些父节点下])`。

    这是分片那条路的**结构性缺陷**，也是一次生成要说清的那条收益：分片每支展开时
    只看得到自己那条标题链，兄弟支写了什么它不知道（`PLAN_PROMPT` 里"彼此覆盖的
    范围不重叠"模型并不听），于是同一个说法会挂在两条分支下。一次生成时全部骨架在
    同一份响应里，模型至少有可能看住；这里数的是"它看住没有"。

    判据与两条边界（同父不算、同一条祖先链不算）见模块 docstring。
    """
    # 每条链带两份：`ids` 判"在不在同一条祖先链上"，`labels` 输出给人看。
    #
    # **判链必须用 id**：同名节点不止一种含义（父子同名、兄弟同名），而 label 前缀
    # 相同不代表同一条链——两支下各有一处「第 3 章」时，按 label 判会把它们当成同链
    # 放过，那正是这条判据要报的东西。第一版实现比的是**父元组**（`place[:-1]`），
    # 于是父子同名被判成跨支（S07「力矩与转动定律」：父与子同名，两处的祖先链是
    # 前缀关系），docstring 承诺的"同一条祖先链上的两处不算跨支"没做到。
    found: dict[str, list[tuple[tuple[str, ...], tuple[str, ...]]]] = {}
    stack: list[tuple[Node, tuple[str, ...], tuple[str, ...]]] = [
        (child, (), ()) for child in root.children
    ]
    while stack:
        node, ids, labels = stack.pop()
        key = verbatim_key(node.label)
        if key:
            found.setdefault(key, []).append((ids, (*labels, node.label)))
        for child in node.children:
            stack.append((child, (*ids, node.id), (*labels, node.label)))
    duplicates: list[tuple[str, list[str]]] = []
    for label, places in found.items():
        if len(places) < 2:
            continue
        # 同一条祖先链上的两处不算跨支（父子同一句话是另一类问题，`check()` 管）：
        # "同一条链"= 全部落在那条最长的链上。判据用 id 元组的前缀关系，见上面那段。
        #
        # **同父兄弟也在这一句里被挡掉**：两处的 `ids` 完全相同，于是它们都等于最长
        # 那条，前缀关系成立——不需要另写一个 `parent is parent` 判断。
        longest = max((ids for ids, _ in places), key=len)
        if all(longest[: len(ids)] == ids for ids, _ in places):
            continue
        duplicates.append((label, sorted(
            "/".join(labels[:-1]) or "根" for _, labels in places)))
    return duplicates


def duplicate_diagnostics(root: Node) -> list[Diagnostic]:
    """把 `cross_branch_duplicates` 的结果说成一条汇总 warn。

    汇总一条而不是逐对报：库里那张图是 3 个说法各写 2 遍 = 6 个节点，逐对报会把
    面板刷满（与 `MISSING_DETAIL` 同一条取舍）。

    **message 逐字冻结**：它原来写在 `agent/tools.py::_generate` 里，有诊断码就有
    下游（`AgentPanel` 渲染、`data["diagnostics"]`、用例断言），搬家时这段文案跟着
    搬，一个字都不改。
    """
    duplicates = cross_branch_duplicates(root)
    if not duplicates:
        return []
    return [Diagnostic(
        level=DiagnosticLevel.warn, code="CROSS_BRANCH_DUPLICATE",
        message=(
            f"{sum(len(places) for _, places in duplicates)} 个节点跨分支重复"
            f"（{len(duplicates)} 个说法各写了 "
            f"{'/'.join(str(len(places)) for _, places in duplicates[:3])} 遍）："
            + "；".join(
                f"「{label}」（{'、'.join(places)}）" for label, places in duplicates[:5]
            )
            + ("…" if len(duplicates) > 5 else "")
            + "。重复的节点没有被删——要保留哪一处由你决定。"
        ),
    )]
