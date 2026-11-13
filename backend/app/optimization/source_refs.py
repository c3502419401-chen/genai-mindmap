"""第三层：引用与本次提供的原文匹配，不将文本匹配宣称为事实核验。"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.contracts.diagnostic import Diagnostic, DiagnosticLevel
from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source, SourceRef

logger = logging.getLogger(__name__)

# 单次生成最多对多少条引用跑 NLI。一次推理 15–44 ms，不设上限时 1000 节点
# 每条引用都跑一遍能拖到几十秒，而用户只是点了「生成导图」。超出预算的
# 引用静默跳过——它们是可选的附加评分，不是功能的必要条件。
MAX_NLI_CHECKS = 100


@dataclass(frozen=True)
class EvidenceSpan:
    """一条检索命中的两级区间：返回给模型的父块 + 真正匹配上的子块。

    单独一个小类型而不是直接收 `EvidenceHit`：这一层是**后处理**，不该为了两个
    整数去 import 整个检索模块（`app.knowledge.service` 有 60KB，还牵着 embedding
    与向量缓存）。接入时一行转换：

        [EvidenceSpan(h.source_id, h.start, h.end, h.leaf_start, h.leaf_end)
         for h in hits]

    `leaf_start/leaf_end` 允许缺省：`search_sources` 的命中没有子块这一级，
    那时校验退化成现在这样（只用 `start/end` 收范围、用 `quote` 逐字比对）。
    """

    source_id: str
    start: int
    end: int
    leaf_start: int | None = None
    leaf_end: int | None = None


def _evidence_problem(
    ref: SourceRef, evidence: Sequence[EvidenceSpan],
) -> tuple[str, str] | None:
    """引用是否落进了"父块里与查询无关的那部分"。

    为什么需要这一层：父块返回模式下 `quote` 是 450–700 字的上下文，命中的
    只有其中 200 字。模型从父块里挑一句与查询无关的话当引用，`quote` 逐字比对
    照样通过——校验就从"证据核验"退化成"格式检查"。这条约束问的是更细的问题：
    **你引的这句，在命中的那段里吗**。

    三条判定规则，都是为了让误伤最小：

    1. 引用没有字符区间（只有 `quote`）时不施加约束：这类引用由逐字比对管，
       本来就没有可比对的区间。
    2. 引用**完全落在所有命中之外**时不施加约束。材料整份在 prompt 里，模型
       引用没被这次检索命中的段落是合法且常见的，那种情况由逐字校验负责。
    3. 引用与多条命中都重叠时，只要**有一条**命中的子块区间与它有交集就通过。
       不同父块之间会重叠（`CHUNK_OVERLAP_CHARS`），一条引用同时落进两个父块
       是正常形态，不能因为其中一个的命中区不在交集里就判失败。
    """
    if ref.start is None or ref.end is None:
        return None

    covering = [
        span for span in evidence
        if span.source_id == ref.source_id
        and span.leaf_start is not None and span.leaf_end is not None
        # 引用的 [start, end) 与命中的父块区间有交集
        and ref.end > span.start and ref.start < span.end
    ]
    if not covering:
        return None
    if any(ref.end > span.leaf_start and ref.start < span.leaf_end
           for span in covering):
        return None
    return (
        "SOURCE_REF_OUTSIDE_EVIDENCE",
        "引用区间与本次命中的证据子块没有交集：引文确实在原文里，但落在父块中"
        "与查询无关的部分",
    )


def _reference_problem(ref: SourceRef, source: Source | None) -> tuple[str, str] | None:
    if source is None or source.status != "parsed" or not (source.text or "").strip():
        return "SOURCE_TEXT_UNAVAILABLE", "未提供已解析的来源原文，无法核验引用"
    text = source.text or ""
    if ref.start is not None and ref.end is not None:
        if ref.end > len(text):
            return "SOURCE_RANGE_OUT_OF_BOUNDS", "引用字符区间超出来源原文"
        excerpt = text[ref.start:ref.end]
        if not excerpt.strip():
            return "EMPTY_SOURCE_EXCERPT", "引用字符区间只有空白"
        if ref.quote is not None and ref.quote != excerpt:
            return "SOURCE_QUOTE_MISMATCH", "引文与指定字符区间的原文不一致"
    elif ref.quote is not None:
        if ref.quote not in text:
            return "SOURCE_QUOTE_MISMATCH", "在指定来源原文中找不到该引文"
    else:
        return "UNVERIFIED_SOURCE_REF", "引用没有引文或字符区间，无法核验原文依据"

    # SourceSpan.page 记录总页数，不是页与文本的映射；只能排除明确不可能的页码。
    if ref.page is not None:
        total_pages = source.span.page if source.span else None
        if (total_pages is not None and ref.page > total_pages) or ref.page in source.failed_pages:
            return "INVALID_SOURCE_PAGE", "引用页码超出来源页数或指向解析失败的页面"
    return None


def verify_source_refs(content: MindMapContent, sources: list[Source], *,
                       remove_invalid: bool = False,
                       evidence: Sequence[EvidenceSpan] | None = None,
                       ) -> list[Diagnostic]:
    """只检查指定来源；生成时可剔除无效引用，诊断接口始终只读。

    没有原文时不能凭 source_usage 推断已验证。未找到依据的节点只给出提示，
    不删除节点，也不据此生成自动删除建议。

    `evidence` 是**可选**的两级命中区间，用于父块返回模式（`search_parent`）：
    给了它，落在命中父块里、但与子块区间没有交集的引用会被判为
    `SOURCE_REF_OUTSIDE_EVIDENCE`（判定规则见 `_evidence_problem`）。不给时
    逐字节等同于加这个参数之前的行为。

    唯一接线点是生成链：`tools._rag` 把每次检索的命中区间累积进
    `RunContext.evidence`，`tools._generate` 再交给
    `generation.service.validate_generated_content(..., evidence=...)`，由它转到这里。
    `optimization/service.py::check()` 那条诊断路径**不传**——它是只读体检，
    没有"本次运行"这个概念；`full` 形态也不传，那里的模型看得见全文。
    """
    by_id = {source.id: source for source in sources}
    if len(by_id) != len(sources):
        raise ValueError("核验来源 ID 不能重复")
    diagnostics: list[Diagnostic] = []
    stack = [content.root]
    while stack:
        node = stack.pop()
        verified: list[SourceRef] = []
        for ref in node.source_refs:
            problem = _reference_problem(ref, by_id.get(ref.source_id))
            if problem is not None:
                code, message = problem
                diagnostics.append(Diagnostic(
                    level=DiagnosticLevel.warn, code=code, node_id=node.id,
                    message=f"来源 {ref.source_id}：{message}", evidence=ref.quote,
                ))
                continue
            # 逐字校验通过之后才问"落在命中的那段里吗"：顺序反过来会对一条
            # 本来就对不上原文的引用再报一次位置问题，两条诊断里只有前一条有意义。
            if evidence is not None:
                off_evidence = _evidence_problem(ref, evidence)
                if off_evidence is not None:
                    code, message = off_evidence
                    diagnostics.append(Diagnostic(
                        level=DiagnosticLevel.warn, code=code, node_id=node.id,
                        message=f"来源 {ref.source_id}：{message}", evidence=ref.quote,
                    ))
                    continue
            verified.append(ref)
            if ref.page is not None:
                diagnostics.append(Diagnostic(
                    level=DiagnosticLevel.info, code="SOURCE_PAGE_UNVERIFIED", node_id=node.id,
                    message=f"来源 {ref.source_id} 的引文已匹配，但当前无逐页文本映射，页码尚未核验",
                    evidence=ref.quote,
                ))
        if remove_invalid:
            node.source_refs = verified
        if not verified:
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn, code="UNSOURCED_NODE", node_id=node.id,
                message=f"节点「{node.label}」没有可核验的原文引用；不代表节点内容错误",
            ))
        stack.extend(reversed(node.children))
    return diagnostics


def annotate_nli(content: MindMapContent, sources: list[Source]) -> list[Diagnostic]:
    """可选层：对**已通过前两层**的引用跑语义蕴含打分。

    前两层是机械判据（字段合法、quote 逐字出现在原文里），它们管不住
    「引文逐字对得上、但节点论断与原文语义相反」这一类。这一层补的正是它。

    **只产出诊断，不改任何 source_ref。** 引用存不存在由前两层决定；这一层
    只回答"一条已存在的引用在语义上站不站得住"。判定相反时引用留着、同时
    给一条 contradiction 警告，两边都保留由人判断——概率模型不该推翻确定性判据。

    没有 quote 也没有字符区间的引用直接跳过：拿整篇原文去比一个节点标签，
    NLI 只会给 neutral，没有信息量还白花时间。

    模块未启用时立刻返回空列表，连 import 都不发生。
    """
    from app.optimization import nli

    if not nli.available():
        return []

    by_id = {source.id: source for source in sources}
    diagnostics: list[Diagnostic] = []
    checked = 0
    skipped = 0
    stack = [content.root]
    while stack:
        node = stack.pop()
        for ref in node.source_refs:
            evidence = ref.quote
            if not evidence and ref.start is not None and ref.end is not None:
                source = by_id.get(ref.source_id)
                if source is not None:
                    evidence = (source.text or "")[ref.start:ref.end]
            if not (evidence or "").strip():
                continue

            if checked >= MAX_NLI_CHECKS:
                skipped += 1
                continue
            checked += 1

            result = nli.judge(node.label, evidence)
            if not result:
                continue

            if result["label"] == "contradiction":
                diagnostics.append(Diagnostic(
                    level=DiagnosticLevel.warn, code="SOURCE_REF_CONTRADICTED",
                    node_id=node.id,
                    message=(f"节点「{node.label}」与来源 {ref.source_id} 的引文"
                             f"语义相反（置信 {result['score']:.2f}）；"
                             f"引用本身逐字匹配，问题出在语义方向"),
                    evidence=ref.quote,
                ))
            else:
                diagnostics.append(Diagnostic(
                    level=DiagnosticLevel.info, code="SOURCE_REF_NLI",
                    node_id=node.id,
                    message=(f"来源 {ref.source_id} 引文语义判定："
                             f"{result['label']}（{result['score']:.2f}）"),
                    evidence=ref.quote,
                ))
        stack.extend(reversed(node.children))

    if skipped:
        logger.info("NLI 评估达到上限 %d 条，另有 %d 条引用跳过", MAX_NLI_CHECKS, skipped)
    return diagnostics
