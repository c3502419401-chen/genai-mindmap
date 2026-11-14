"""第三层：引用与本次提供的原文匹配，不将文本匹配宣称为事实核验。"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace

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


@dataclass(frozen=True)
class _Verdict:
    """一条引用的判定结果：**有没有问题**与**留不留**是两件事。

    上一版的 `_reference_problem` 把两者绑死（返回 code 就等于剔除），于是"提示"
    一定带着删除的副作用。放宽之后必须拆开：

      - 落在父块内、命中子块外 → warn，`drop=False`（模型引的是材料里的某段，
        只是不在这轮命中的那 200 字里，而它看到的是 450–700 字的父块）；
      - 引文在可见范围内多处出现 → warn，`drop=False`（判不了对错，交给人核）；
      - 逐字对不上且定位不到 → `drop=True`（这条真的是编的）。

    `fix` 是服务端补正后的 `(start, end)`：只在可写路径（`remove_invalid=True`）
    写回模型。`check_evidence=False` 表示这条引用不要再按偏移判"落在命中的子块里吗"
    ——它的偏移本来就是猜的，用错误坐标去判只会产出噪声。
    """

    code: str | None = None
    message: str = ""
    level: DiagnosticLevel = DiagnosticLevel.warn
    drop: bool = False
    fix: tuple[int, int] | None = None
    check_evidence: bool = True


_OK = _Verdict()


def _evidence_ranges(
    text: str, evidence: Sequence[EvidenceSpan], source_id: str, start: int, end: int,
) -> list[tuple[int, int]]:
    """这条引用**这次可见**的范围，按"模型手上实际有什么"排序。

    模型在父块返回模式下看到的是 450–700 字的父块，在 `full` 形态下看到的是全文。
    所以定位引文时先查覆盖它原区间的父块，查不到再退回全文：

      - 先父块：模型就是从那里抄的，落在父块内的定位比"全文里唯一"更贴近事实；
      - 再全文：`full` 形态、没有证据、或者引用跨出了父块时，可见范围就是全文。
        少这一段，`full` 形态下"偏移写错但引文逐字正确"的引用会被判成编造。

    没有任何父块覆盖它时（引用落在本次命中的范围之外），只剩全文这一个范围——
    "落在父块外"由 `_outside_evidence` 单独判，与定位是两件事。
    """
    covering = [
        (span.start, span.end) for span in evidence
        if span.source_id == source_id and span.start < end and span.end > start
    ]
    covering.append((0, len(text)))
    return covering


def _quote_occurrences(text: str, quote: str, window: tuple[int, int]) -> list[int]:
    """`quote` 在 `window` 里出现的所有起点（含重叠）。

    重叠也算一处：`aa` 在 `aaa` 里出现两次，这时"唯一定位"本来就不成立，
    少算一次会把一条有歧义的引用误判成补正成功。
    """
    low, high = window
    found: list[int] = []
    position = text.find(quote, low)
    while 0 <= position and position + len(quote) <= high:
        found.append(position)
        position = text.find(quote, position + 1)
    return found


def _locate_quote(
    text: str, quote: str, evidence: Sequence[EvidenceSpan] | None,
    source_id: str, start: int | None, end: int | None,
) -> tuple[str, tuple[int, int] | None]:
    """在可见范围内定位引文：返回 `("unique", (start, end))` / `("ambiguous", None)` /
    `("missing", None)`。

    **这是放宽之后补上的那一半**：上一版只要引文与给定区间不一致就判
    `SOURCE_QUOTE_MISMATCH` 整条剔掉，而模型本来就被告知"只截一段连续的、别自己算
    偏移"——它照做之后，只给 quote 的引用永远只能靠"逐字在全文里"，落在父块的哪一段
    查不出来。现在服务端自己定位：唯一命中就把偏移补对，多处命中就标"待核"，
    真的找不到才认定是编的。
    """
    windows = ([(0, len(text))] if evidence is None or start is None or end is None
               else _evidence_ranges(text, evidence, source_id, start, end))
    ambiguous = False
    for window in windows:
        positions = _quote_occurrences(text, quote, window)
        if len(positions) == 1:
            found = positions[0]
            return "unique", (found, found + len(quote))
        if len(positions) > 1:
            ambiguous = True
    return ("ambiguous", None) if ambiguous else ("missing", None)


def _page_verdict(ref: SourceRef, source: Source) -> _Verdict | None:
    """页码只在**明确不可能**时才判死：`SourceSpan.page` 是总页数，不是页与文本的映射。"""
    if ref.page is None:
        return None
    total_pages = source.span.page if source.span else None
    if (total_pages is not None and ref.page > total_pages) or ref.page in source.failed_pages:
        return _Verdict(
            "INVALID_SOURCE_PAGE", "引用页码超出来源页数或指向解析失败的页面", drop=True)
    return None


def _check_reference(
    ref: SourceRef, source: Source | None, *,
    evidence: Sequence[EvidenceSpan] | None = None,
) -> _Verdict:
    """一条引用的判定：通过 / 保留并提示 / 剔除，三种结果分开表达。

    上一版这个函数返回 `(code, message)` 或者 `None`，而调用方把"有 code"直接当成
    "剔除"——于是所有提示都带着删除的副作用。放宽之后两者必须解耦：落在父块外的
    引用、定位不唯一而"待核"的引用都要**留在树上**，只是各带一条诊断。
    """
    if source is None or source.status != "parsed" or not (source.text or "").strip():
        return _Verdict(
            "SOURCE_TEXT_UNAVAILABLE", "未提供已解析的来源原文，无法核验引用", drop=True)
    text = source.text or ""
    quote = ref.quote

    if ref.start is not None and ref.end is not None:
        if ref.end > len(text):
            return _Verdict(
                "SOURCE_RANGE_OUT_OF_BOUNDS", "引用字符区间超出来源原文", drop=True)
        excerpt = text[ref.start:ref.end]
        if not excerpt.strip():
            return _Verdict("EMPTY_SOURCE_EXCERPT", "引用字符区间只有空白", drop=True)
        if quote is None:
            # 放行口之一（本轮堵住）：区间合法、引文为空时旧实现直接通过。那等于
            # 只核对了一对坐标——谁都能给任意区间"标记"一段自己从没读过的原文，
            # 而界面上它会显示成一条有出处的引用。
            return _Verdict(
                "UNVERIFIED_SOURCE_REF",
                "引用只有字符区间、没有引文，无法核验它指的是原文的哪一段",
                drop=True,
            )
        if quote == excerpt:
            return _page_verdict(ref, source) or _OK
        return _relocate(ref, source, text, evidence)

    if quote is None:
        # 放行口之二：既无区间也无引文。旧实现这条 else 分支本来就拦住（返回
        # `UNVERIFIED_SOURCE_REF` 并剔除），这里保留同样的判定，只把文案说清。
        return _Verdict(
            "UNVERIFIED_SOURCE_REF",
            "引用既没有引文也没有字符区间，无法核验原文依据",
            drop=True,
        )
    if quote not in text:
        return _Verdict(
            "SOURCE_QUOTE_MISMATCH", "在指定来源原文中找不到该引文", drop=True)
    # 只有 quote、没有偏移：逐字匹配本来就算通过，但服务端仍然把偏移补上——
    # 界面上"点引用跳原文"要坐标，`annotate_nli` 后面也要用它。定位不唯一时
    # 只标一条 warn、不补：猜一个坐标比没有坐标更糟。
    outcome, fix = _locate_quote(text, quote, evidence, ref.source_id, None, None)
    page = _page_verdict(ref, source)
    if page is not None:
        return page
    if outcome == "ambiguous":
        return _Verdict(
            "SOURCE_QUOTE_AMBIGUOUS",
            "引文在可见范围内出现多次，无法确定它指的是哪一处，已保留并标为待核",
            check_evidence=False,
        )
    if outcome == "unique" and fix is not None:
        # 只给引文时补上偏移是**补齐元数据**，不是修错，所以不产诊断：整树生成时
        # 每个节点都会有一条这样的引用，逐条报 info 只会把真正要看的东西淹掉。
        return _Verdict(fix=fix)
    return _OK


def _relocate(
    ref: SourceRef, source: Source, text: str,
    evidence: Sequence[EvidenceSpan] | None,
) -> _Verdict:
    """引文与给定区间不一致：由服务端重新定位，而不是直接判死。

    三种结果对应三种处置，缺任何一种都会退化成上一版的"一律剔除"：

      - 唯一定位 → 补正偏移（info `SOURCE_QUOTE_OFFSET_FIXED`），引用保留。
        写回只在 `remove_invalid=True` 时发生：`check()` 那条只读体检路径不能因为
        "顺手修一下"就改了调用方的树。
      - 多处出现 → 保留并标"待核"（warn `SOURCE_QUOTE_AMBIGUOUS`）。判不了对错时
        删掉是替用户做了决定，而这条引用**确实**逐字存在于原文里。
      - 定位不到 → 剔除（`SOURCE_QUOTE_MISMATCH`）。这才是真编的那一种。
    """
    outcome, fix = _locate_quote(
        text, ref.quote, evidence, ref.source_id, ref.start, ref.end)
    if outcome == "unique" and fix is not None:
        page = _page_verdict(ref, source)
        if page is not None:
            return page
        return _Verdict(
            "SOURCE_QUOTE_OFFSET_FIXED",
            f"引文与给定区间不一致，已按原文定位补正为 [{fix[0]}, {fix[1]})",
            level=DiagnosticLevel.info, fix=fix, check_evidence=False,
        )
    if outcome == "ambiguous":
        return _Verdict(
            "SOURCE_QUOTE_AMBIGUOUS",
            "引文在可见范围内出现多次，无法确定它指的是哪一处，已保留并标为待核",
            check_evidence=False,
        )
    return _Verdict(
        "SOURCE_QUOTE_MISMATCH",
        "引文与指定字符区间的原文不一致，且在可核验的原文里定位不到这句",
        drop=True,
    )


def _reference_problem(ref: SourceRef, source: Source | None) -> tuple[str, str] | None:
    """`_check_reference` 的**旧二元视图**：只回答"这条引用该不该剔除"。

    保留它是给两个测量脚本（`scripts/measure_branch_expansion.py`、
    `scripts/measure_deep_slicing.py`）用的：它们要在不接生成链的情况下数"这条引用
    站不站得住"。新代码不要用它——它丢掉了"保留并提示"这一档，而那正是这一轮
    放宽出来的第三种结果（父块越界、多处待核）。
    """
    verdict = _check_reference(ref, source)
    if verdict.drop:
        return verdict.code or "SOURCE_QUOTE_MISMATCH", verdict.message
    return None


def _outside_evidence(ref: SourceRef, evidence: Sequence[EvidenceSpan]) -> bool:
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

    **它现在只产出诊断，不剔除**（三轮实测的教训见 `verify_source_refs` 的
    docstring）：模型写的是自己的话、引的是材料里的某段，它看到的是父块而不是
    命中的那 200 字，按子块卡等于要求它做一个它没有信息的判断。
    """
    if ref.start is None or ref.end is None:
        return False
    covering = [
        span for span in evidence
        if span.source_id == ref.source_id
        and span.leaf_start is not None and span.leaf_end is not None
        # 引用的 [start, end) 与命中的父块区间有交集
        and ref.end > span.start and ref.start < span.end
    ]
    if not covering:
        return False
    return not any(ref.end > span.leaf_start and ref.start < span.leaf_end
                   for span in covering)


def verify_source_refs(content: MindMapContent, sources: list[Source], *,
                       remove_invalid: bool = False,
                       evidence: Sequence[EvidenceSpan] | None = None,
                       ) -> list[Diagnostic]:
    """只检查指定来源；生成时可剔除无效引用，诊断接口始终只读。

    没有原文时不能凭 source_usage 推断已验证。未找到依据的节点只给出提示，
    不删除节点，也不据此生成自动删除建议。

    `evidence` 是**可选**的两级命中区间，用于父块返回模式（`search_parent`）。
    给了它之后有两条相关行为：

      1. 引文与偏移对不上时**先在可见父块内定位**（`_locate_quote`），唯一定位就
         补正偏移、多处出现就标「待核」，两者都**保留**引用；
      2. 引用落在命中的父块里、但不在命中的子块范围内 → warn
         `SOURCE_REF_OUTSIDE_EVIDENCE`，**保留**（判定规则见 `_outside_evidence`）。

    第 2 条这一轮从"剔除"降级为"提示"：模型现在写的是自己的话、引的是材料里的某段，
    它看到的是 450–700 字的父块，要求引用精确落在命中的那 200 字里过于苛刻——实测
    它会把合法引用成批剔掉，界面上表现为"这一支没有证据"。子块约束保留下来是为了
    **可观测**（谁引了父块里与查询无关的部分，诊断里看得见），不是为了删。

    不给 `evidence` 时只有逐字校验与定位补正，没有父块约束——`full` 形态和
    `optimization/service.py::check()` 那条只读体检走的就是这条路。

    唯一接线点是生成链：`tools._rag` 把每次检索的命中区间累积进
    `RunContext.evidence`，`tools._generate` 再交给
    `generation.service.validate_generated_content(..., evidence=...)`，由它转到这里。
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
            verdict = _check_reference(ref, by_id.get(ref.source_id), evidence=evidence)
            if verdict.fix is not None:
                if remove_invalid:
                    # 只在可写路径写回：`check()` 那条只读体检不能因为"顺手修一下"
                    # 就改了调用方传进来的树（`test_bad_evidence_is_reported_without_editing_the_map`
                    # 钉着这条契约）。
                    ref.start, ref.end = verdict.fix
                else:
                    # 只读路径不改树 → "已补正"这句话是假的。能补正就当它通过，
                    # 一条诊断都不报：把没实施的改动写进诊断，比没有诊断更糟。
                    verdict = replace(verdict, code=None, message="")
            if verdict.code is not None:
                diagnostics.append(Diagnostic(
                    level=verdict.level, code=verdict.code, node_id=node.id,
                    message=f"来源 {ref.source_id}：{verdict.message}", evidence=ref.quote,
                ))
            if verdict.drop:
                continue
            # 逐字校验通过之后才问"落在命中的那段里吗"：顺序反过来会对一条本来就
            # 对不上原文的引用再报一次位置问题，两条诊断里只有前一条有意义。
            # 补正过 / 待核的引用跳过这一层：它们的偏移是模型猜的，用错误坐标去判
            # 只会产出"落在父块外"的噪声。
            if (evidence is not None and verdict.check_evidence
                    and _outside_evidence(ref, evidence)):
                diagnostics.append(Diagnostic(
                    level=DiagnosticLevel.warn, code="SOURCE_REF_OUTSIDE_EVIDENCE",
                    node_id=node.id,
                    message=(f"来源 {ref.source_id}：引文确实在原文里，但落在父块中"
                             "与本次查询无关的部分；已保留，可人工核对"),
                    evidence=ref.quote,
                ))
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
