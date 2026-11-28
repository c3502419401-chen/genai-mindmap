"""画布上一个节点盒子装得下的字数，以及"哪些节点超了"。

## 为什么这段单独一个模块

同一个口径有两个调用方，而它们**不能互相 import**：`repair.py` 依赖
`service.check`（候选来自 `check()` 自己的诊断，"修完那条码消失"才是结构上成立的
而不是两份判定碰巧一致），所以 `service.py` 反过来 import `repair.py` 就是循环。

判据和它的数放在这里，`service.py` 与 `repair.py` 都 import **同一个对象**。两份
实现能凑出"结果一致"，凑不出"同一个函数"——`test_the_overlong_judgement_has_exactly
_one_implementation` 钉的是后者。

## 与 `GENERATED_LABEL_MAX_CHARS`（16）不是一件事

    GENERATED_LABEL_MAX_CHARS = 16   生成侧**硬切**：模型写超了就截到 16，截的那一刻
                                    报 `LABEL_CLIPPED_TO_GENERATED`（原长标题只有
                                    那一刻在手上，事后判不到）
    READABLE_LABEL_CHARS = 24        渲染侧**兜底**：画布 `line-clamp-2` 加
                                    `frontend/src/stores/mindmap.ts::TRUNCATE_CHARS`
                                    把超出的截成 `…`

16 < 24，所以正常生成的标题在画布上**不会被截**；超 24 的只可能来自压链拼接
（`repair._absorb` 用 ` / ` 把两层标题放进一个盒子）或用户手编。前一种正是修复
自己造出来的缺陷——修复要能被自己量到，所以这个函数同时是生成收尾与 `/check` 的
同一份判据。
"""
from __future__ import annotations

from typing import Iterator

from app.contracts.mindmap import MindMapContent, Node

#: **画布上一个节点盒子两行装得下的字数**，不是契约上限。
#:
#: 契约给的是 `MAX_NODE_LABEL_CHARS = 200`（`contracts/mindmap.py`），渲染层给的是
#: 这个数：`frontend/src/stores/mindmap.ts` 的 `TRUNCATE_CHARS = 24` 把超出的部分
#: 截成 `…`，`canvas/MindNode.vue` 的 `line-clamp-2` 只留两行。两处是同一个数字的
#: 两个侧面，所以这个口径取它——**不是**契约。
#:
#: 为什么不在契约里：契约说"什么算合法的图"，而这个数说的是"画布上装不装得下"。
#: 放契约里会让别处（模型输出、导入）也被判 24 字，那是另一件事。
#: `scripts/_structure_audit.py` 的 F 判据从这里导入同一个数（口径同源，不各写一份）。
READABLE_LABEL_CHARS = 24


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.children:
        yield from _walk(child)


def overlong_labels(content: MindMapContent, *,
                    limit: int = READABLE_LABEL_CHARS) -> list[tuple[str, str]]:
    """`(node_id, label)`：画布上会被截成半句的那些节点。

    ## 判据只有这一份，两个时刻都用它

    生成收尾（`agent/tools.py`）量的是**修复之后**那棵树，报
    `LABEL_OVERLONG_IN_DELIVERY`；`/check` 与 `/suggest` 量用户手里那棵树，是同一个
    码的第二个出口。原先只有生成侧那一段，于是用户点「检查与建议」时一条都看不见
    ——同一张图、同一个缺陷，两种时刻两种答案。

    为什么它值得进 `check()`（`docs/DIAGNOSTIC-TWO-SIDES.md` 的三条闸）：它是纯
    函数、只量 label 字数、而画布上 `line-clamp-2` 把超长的截成半句——用户看得出
    "标题被截了"，看不出"那半句本可以不断"。

    ## 事后判到什么程度

    判得到：哪些节点超了 24、它们的原文（`(id, label)` 两个都给出来）。
    判不到两件事，都不该在这里猜：

      1. **这段是压链拼的还是用户手写的**。`LABEL_JOINER`（` / `）就是中文标题里
         常用的那个斜杠，修复没有在产物上留标记——所以这里只测量、不给自动修的
         建议（`repair.MANUAL_ONLY` 有理由）。
      2. **生成侧 16 字那个切点落在半个词上没有**。原长标题在截断那一刻就没了，
         事后树上只剩切短的标题（`LABEL_CLIPPED_TO_GENERATED` 只在生成侧报，
         理由就是这个）。

    口径（`limit`）与渲染层同源：`TRUNCATE_CHARS` 超了就截成 `…`。首尾空白不计
    长度（`label.strip()`），与前端 `displayTitle` 的 `trim()` 同一口径。
    """
    return [(node.id, label) for node in _walk(content.root)
            if len(label := node.label.strip()) > limit]


__all__ = ["READABLE_LABEL_CHARS", "overlong_labels"]
