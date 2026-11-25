"""分数分布的形态：`top_k=3` 到底漏掉了什么。

这组用例**不依赖**按分数取数的实现（`keep_by_score` 一行都不碰），它量的是**改之前
就存在的形状**，所以它既是"要不要改"的证据，也是一条回归：如果哪天切块或 BM25 的
参数调整让分数分布变成"3 附近有断崖"，这几条会失败，那时该做的是重新量、而不是
改断言。

材料是合成的（不依赖 `backend/data/mindmaps.db`）：八段同主题、每段 ~540 字，段间
一个空行 → 8 个父块，每段都能被"预算审批流程"这一串字命中。真实材料上的同一组数字
在 `scripts/measure_topk_threshold.py`（五组真材料 × 23 个 query）：

    第 3→4 条的跌幅  中位 0.026（hybrid）/ 0.047（纯 BM25）
    第 4–10 条       131/140（94%）落在不同父块
"""
from __future__ import annotations

from app.contracts.source import Source, SourceKind
from app.knowledge.service import search_parent


def material() -> str:
    """八段同主题材料，段间一个空行 → 八个父块。

    刻意让每段措辞不同：字面完全相同的段落会被 BM25 打成同分，那证明不了
    "分数差不多"，只证明"分数相等"。
    """
    topics = (
        "预算审批流程的第一步是业务部门在系统里发起申请，填写金额、用途与预算科目。",
        "预算审批流程的第二步是部门负责人在线确认，确认的是这笔钱是否在本部门额度内。",
        "预算审批流程的第三步是财务经理核准，财务看的是科目余额与凭证是否齐备。",
        "预算审批流程的第四步是分管副总审批，超过一定金额时这一步不能跳过。",
        "预算审批流程的第五步是出纳复核并生成付款计划，复核的是收款账户与合同号。",
        "预算审批流程的第六步是总经理终审，终审只对超额与跨年度这两类申请生效。",
        "预算审批流程的第七步是归档，归档之后原始单据与系统记录必须一一对应。",
        "预算审批流程的第八步是月度复盘，复盘统计的是流程时长与驳回原因分布。",
    )
    filler = ("这一段还说明了同一件事的边界：流程之外的口头同意不作数，"
              "任何一步的退回都要写明原因，退回次数会进入下个月的统计。")
    return "\n\n".join(f"{topic}{filler * 9}" for topic in topics)


def source(text: str | None = None) -> Source:
    return Source(id="s1", kind=SourceKind.document, filename="预算管理制度.docx",
                  text=text if text is not None else material(), status="parsed")


def test_fourth_hit_is_within_five_percent_of_third():
    """`top_k=3` 砍掉的第 4 条，与第 3 条的分差在 15% 以内。

    这就是"固定取前 N 条会漏"的可复现证据：如果第 4 条只有第 3 条的 20%，阈值取数
    就没有意义；实测这份材料上是 **0.02%**（4.604 → 4.603）。
    """
    hits = search_parent([source()], "预算审批流程", 12)
    assert len(hits) >= 8, f"这份材料该命中 8 个父块，实际 {len(hits)}"
    third, fourth = hits[2].score, hits[3].score
    assert (third - fourth) / third < 0.15


def test_the_dropped_hits_are_different_parents_not_the_same_one():
    """第 4 条之后每条落在**不同父块**上：多给是信息，不是同一段换个开头。"""
    hits = search_parent([source()], "预算审批流程", 12)
    parents = [(item.source_id, item.start, item.end) for item in hits]
    assert len(set(parents)) == len(parents)


def test_top_three_covers_at_most_half_of_the_available_parents():
    """`top_k=3` 只拿到可用父块的一小半——"漏很多"这件事的直接度量。

    8 个父块里取 3 条 = 37.5%。真实材料上的同一条（`thin` 那种整份比 3 块还小的
    除外）也是这个量级：`pdf/Attention` 48 → 3 条（6%）。
    """
    available = search_parent([source()], "预算审批流程", 12)
    delivered = search_parent([source()], "预算审批流程", 3)
    assert len(delivered) == 3
    assert len(delivered) / len(available) <= 0.5
