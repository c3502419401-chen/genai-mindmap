"""检索命中 → 引用校验的那条线：累积、去重、限量、接线、默认值。

为什么单独一个文件：这组用例钉的是**跨模块的接缝**，不是任何一侧的内部逻辑。
`source_refs.py` 的三条判定在 `tests/test_source_refs.py` 里已经钉住，`search_parent`
的两级切块在 `tests/test_parent_retrieval.py` 里已经钉住；缺的正是中间那一段——
hits 怎么从 `rag_query` 的返回值活到生成收尾的 `verify_source_refs`。

这条线有四段，任何一段断掉都会让"父块模式下的引用校验"退化成格式检查，而
退化**没有任何运行时信号**（诊断照常产出，只是永远不出现 `SOURCE_REF_OUTSIDE_EVIDENCE`）：

    tools._rag → RunContext.record_evidence → tools._generate
              → validate_generated_content(evidence=...) → verify_source_refs

所以每一段都单独断一次：去重与上限在 `record_evidence` 上直接测，"累积到哪"在
`_rag` 上测，"最后有没有交出去"在 `_generate` 上测，"交给谁"在
`validate_generated_content` 上测，"模型抄什么才能通过"用一次真的多轮循环测。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any
from unittest.mock import AsyncMock, patch

from app.agent import tools as agent_tools
from app.agent.contracts import (
    MAX_EVIDENCE_SPANS,
    AgentRunRequest,
    Completion,
    RunBudget,
    RunContext,
    ToolCall,
)
from app.agent.service import run
from app.agent.tools import build_registry
from app.contracts.mindmap import MindMapContent, Node
from app.contracts.source import Source, SourceKind, SourceUsage
from app.generation.service import SOURCE_MODE_ENV, validate_generated_content
from app.knowledge.service import (
    PARENT_RETRIEVAL_ENV,
    _PARENT_RETRIEVAL_TRUTHY,
    _chunk_tree,
    _parent_retrieval_enabled,
    EvidenceHit,
    search_parent,
    search_sources,
)
from app.optimization.source_refs import EvidenceSpan, verify_source_refs

# 与 `SourceRef.quote` 的 max_length 同口径：超了就没法整段照抄。
QUOTE_LIMIT = 300

# 八个小节，各自一个主题、互不共享关键词。真实材料的切点落在标题与空行上，
# 用 "字" * N 这种合成文本测不到有些边界（重叠量、末块长度都由分隔符决定）。
SECTIONS = (
    ("预算审批的层级",
     "预算审批由部门负责人签核，超过额度的部分必须上报总经理复审，"
     "财务只在核准后执行付款，权责边界写在制度附件里。"),
    ("报销时限",
     "费用报销的时限是三十个自然日，逾期不再受理，"
     "票据缺失的申请一律退回并通知经办人补齐材料。"),
    ("采购合规",
     "采购合同必须经过法务审核，金额超过五十万元另行招标，"
     "供应商资质与授权链条要逐项核对。"),
    ("会议纪要",
     "会议纪要由记录人当日整理，决议事项逐条落到责任人，"
     "下次例会先复核上一轮的落实情况。"),
    ("档案归集",
     "档案归集按年度与部门两级分类，电子件与纸质件分开编号，"
     "借阅要留下登记与归还日期。"),
    ("培训计划",
     "培训计划每季度滚动调整，讲师与场地提前两周确认，"
     "缺席超过三次的学员要重新报名。"),
    ("设备维护",
     "设备维护按周巡检、按月保养，异常记录当天上报，"
     "停机超过四小时启动备机流程。"),
    ("数据备份",
     "数据备份每日增量、每周全量，异地副本保留三十天，"
     "恢复演练每半年做一次并记录耗时。"),
)

QUERY = "报销时限三十个自然日"


def _material(repeat: int = 3) -> str:
    """约 1600 字：4 个父块、10 个左右子块，每个父块里有 2–3 个子块。"""
    parts = ["# 制度汇编\n\n"]
    for round_index in range(repeat):
        for title, body in SECTIONS:
            parts.append(f"## {round_index + 1}.{title}\n\n{body}\n\n")
    return "".join(parts)


def _source(text: str | None = None) -> Source:
    return Source(kind=SourceKind.text, status="parsed", text=text or _material())


def _request(sources: list[Source], *, intent: str = "create") -> AgentRunRequest:
    return AgentRunRequest(
        goal="整理材料", intent=intent, thinking_model="default",
        sources=sources,
        budget=RunBudget(max_model_calls=10, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=4096, max_repairs=1),
    )


def _ctx(sources: list[Source], *, intent: str = "expand") -> RunContext:
    return RunContext(request=_request(sources, intent=intent), candidate=None,
                      sources_by_id={source.id: source for source in sources})


def _call(name: str, call_id: str, **arguments: Any) -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments=arguments)


def _exec_rag(ctx: RunContext, **arguments: Any):
    registry = build_registry("expand", has_sources=True)
    return asyncio.run(registry.execute(
        _call("rag_query", "c-rag", question=QUERY, **arguments), ctx))


def _scene(monkeypatch):
    """一次真实的两级检索，返回 `(source, hit, 命中段文本, 父块内无关段文本)`。

    偏移全部从命中自己身上算，不写死：切块参数一改，这组用例仍对着同一件事
    ——"引用落在命中的子块里"与"落在父块里但不在子块里"。
    """
    monkeypatch.delenv(PARENT_RETRIEVAL_ENV, raising=False)
    source = _source()
    text = source.text or ""
    hits = search_parent([source], QUERY, top_k=5)
    assert hits, "这组用例的前提是一次有命中的检索"
    # 挑一条：父块起点之前还留着正文（用来构造"父块内、命中区之外"的引用），
    # 且命中段短到能整段抄进 `SourceRef.quote`。
    hit = next(h for h in hits
               if h.leaf_start is not None and h.leaf_end is not None
               and h.leaf_start - h.start >= 12
               and h.leaf_end - h.leaf_start <= QUOTE_LIMIT)
    leaf = text[hit.leaf_start:hit.leaf_end]
    outside_quote = text[hit.start:hit.start + 12]
    assert outside_quote.strip()
    # 前提断言：这段确实与命中区没有交集（否则"越界引用"根本没越界）
    assert not (hit.start + 12 > hit.leaf_start and hit.start < hit.leaf_end)
    return source, hit, leaf, outside_quote


# ---------------------------------------------------------------- 一、累积通道


def test_record_evidence_dedupes_the_same_batch():
    """同一批 hits 累两次只留一份（重试同一次检索时不该重复堆）。"""
    ctx = _ctx([_source()])
    batch = [EvidenceSpan("s1", 0, 500, 100, 300), EvidenceSpan("s1", 400, 900, 700, 900)]

    ctx.record_evidence(batch)
    ctx.record_evidence(batch)

    assert ctx.evidence == batch


def test_record_evidence_keeps_same_parent_with_different_leaves():
    """同一父块、不同子块的两条都保留。

    这条是去重键比"父块三元组"多两位的全部理由：两次不同问法命中同一父块的两个
    不同子块时，引用落在**后一个**子块里是合法的。只按父块区间去重会把后一条吞掉，
    那条引用随后会被判成"落在父块里与查询无关的部分"——一次去重换一次系统性误伤。
    """
    ctx = _ctx([_source()])
    ctx.record_evidence([EvidenceSpan("s1", 0, 600, 0, 200)])
    ctx.record_evidence([EvidenceSpan("s1", 0, 600, 300, 500)])

    assert [(span.leaf_start, span.leaf_end) for span in ctx.evidence] == [(0, 200), (300, 500)]


def test_record_evidence_drops_oldest_beyond_cap(caplog):
    """上限生效：造 250 条，留最新的 200 条（丢最旧的，因为模型先检索后生成）。"""
    ctx = _ctx([_source()])
    with caplog.at_level(logging.DEBUG, logger="app.agent.contracts"):
        ctx.record_evidence(EvidenceSpan("s1", index, index + 10)
                            for index in range(250))

    assert len(ctx.evidence) == MAX_EVIDENCE_SPANS == 200
    # 留下的必须是**后** 200 条：新证据比旧证据有用，反过来等于长循环里后面
    # 所有引用都失去校验依据。
    assert (ctx.evidence[0].start, ctx.evidence[-1].start) == (50, 249)
    # 逐出要留痕：容量策略生效时静默丢弃，排查"某条引用为什么没被校验"就只能猜。
    assert "丢弃最旧的 50 条" in caplog.text


def test_rag_accumulates_hits_into_the_run_context(monkeypatch):
    """`_rag` 每检索一次就把这批命中并进 `ctx.evidence`，字段一一对应。"""
    source, hit, _leaf, _outside = _scene(monkeypatch)
    ctx = _ctx([source])

    result = _exec_rag(ctx)

    assert result.ok, result.content
    hits = result.data["hits"]
    assert hits and ctx.evidence
    assert [(span.source_id, span.start, span.end, span.leaf_start, span.leaf_end)
            for span in ctx.evidence] == [
        (item["source_id"], item["start"], item["end"], item["leaf_start"], item["leaf_end"])
        for item in hits
    ]
    # 与 `_scene()` 那次独立检索一致：确定性由纯 BM25 + 同一材料保证
    assert (hit.start, hit.end) in {(item["start"], item["end"]) for item in hits}


def test_rag_hands_the_model_a_citable_leaf_span(monkeypatch):
    """回给模型的 JSON 里带 `evidence_*`，且它逐字对应原文的命中段。

    没有这一组，父块模式下模型拿到的 `quote` 是 450–700 字（超过引用字段 300 字的
    上限），只能截段、且截完没有偏移可抄——带偏移的引用一条也写不出来，
    `EvidenceSpan` 就永远没有被校验的对象。
    """
    source, hit, leaf, _outside = _scene(monkeypatch)
    result = _exec_rag(_ctx([source]))

    assert result.ok, result.content
    payload = {item["start"]: item for item in json.loads(result.content)}
    entry = payload[hit.start]
    assert entry["evidence_start"] == hit.leaf_start
    assert entry["evidence_end"] == hit.leaf_end
    assert entry["evidence_quote"] == leaf
    assert len(entry["evidence_quote"]) <= QUOTE_LIMIT
    # `data["hits"]` 保持 `EvidenceHit` 的字段形状：事件流与既有断言都按它走。
    assert "evidence_quote" not in result.data["hits"][0]


def test_citable_hits_omits_span_when_leaf_exceeds_quote_limit():
    """子块超过 300 字时不给 `evidence_*`：给了偏移就只能配一段不符的 quote。

    这条是**防御性**的，不是正常路径：切块那边已经把 `LEAF_MAX_CHARS` 压到 300
    且末块合并也不再放宽（`test_leaf_never_exceeds_the_quote_contract`），所以
    `_chunk_tree` 产不出超过 300 的子块。这里用手工构造的 `EvidenceHit` 守的是
    另外两件事：参数被改回去，以及库里存着旧参数切出来的块。触发时的行为仍是
    "宁可没有偏移、也不给一段对不上的 quote"——截断后 `quote != text[start:end]`，
    逐字校验必挂，那比"没有偏移"更糟。
    """
    for length, expect in ((300, True), (301, False), (320, False)):
        text = "字" * length
        source = _source(text)
        hit = EvidenceHit(source_id=source.id, start=0, end=length, quote=text,
                          score=0.1, leaf_start=0, leaf_end=length)
        entry = agent_tools._citable_hits([hit], [source])[0]
        assert ("evidence_quote" in entry) is expect, length
        assert entry["quote"] == text


def test_every_leaf_hit_is_citable_after_the_cap_matches_the_quote_limit():
    """切块参数与引用上限对齐之后，父块模式下**没有一条命中**是"不可抄"的。

    这是这次改动要堵的那个缝，所以正面钉一次：只要有一条子块超过 `SourceRef.quote`
    的 300 上限，那条命中就给不出 `evidence_*`，模型退回"只抄 quote、不写偏移"，
    而 `_evidence_problem` 对没有区间的引用不施加约束——"引的这句在命中的那段里吗"
    对这批命中整个失效。缝原来只有二十几个字（上限 320，实测最长子块 370），但它
    正是这条防护要挡的东西。

    用真实材料的真实切块，不手工构造 hit：要证明的是**切块参数与契约对齐**，
    手工构造只能证明 `_citable_hits` 自己会截。材料连跑 `repeat=1..6` 六档
    （452–2672 字），覆盖"父块比子块上限短""未切满""已切满"三种形状，实测出
    66 条命中、最长子块 289 字——正好贴在上限（300）的下方，是这条断言最该压住的
    那个区间。
    """
    broken: list[tuple[int, int]] = []
    total = 0
    for repeat in (1, 2, 3, 4, 5, 6):
        source = _source(_material(repeat))
        text = source.text or ""
        tree = _chunk_tree(text)
        leaves = [node for node in tree if node.level == 1]
        assert leaves
        hits = [
            EvidenceHit(source_id=source.id,
                        start=tree[node.parent].start, end=tree[node.parent].end,
                        quote=text[tree[node.parent].start:tree[node.parent].end],
                        score=0.5, leaf_start=node.start, leaf_end=node.end)
            for node in leaves
        ]
        payload = agent_tools._citable_hits(hits, [source])
        assert len(payload) == len(hits)
        for entry in payload:
            total += 1
            if "evidence_quote" not in entry:
                broken.append((repeat, entry["leaf_end"] - entry["leaf_start"]))
                continue
            # 给了偏移就必须是一份逐字成立的引文，且不超契约上限
            assert entry["evidence_quote"] \
                == text[entry["evidence_start"]:entry["evidence_end"]]
            assert len(entry["evidence_quote"]) <= QUOTE_LIMIT

    assert total >= 40, f"样本太少（{total} 条命中），这条断言没有说服力"
    assert broken == [], f"{len(broken)}/{total} 条命中给不出 evidence_*：{broken}"


def test_leaf_at_exactly_the_quote_limit_passes_verbatim_verification():
    """子块恰好 300 字：`quote` 顶到 `SourceRef` 上限，逐字校验仍然通过。

    `_citable_hits` 的判断是 `<=` 而不是 `<`。差一个等号，最长的那批子块会被当成
    "超限"白丢证据——而它们正是最该给偏移的那批（300 字的上下文里挑错句的空间最大）。
    """
    text = "字" * QUOTE_LIMIT
    source = _source(text)
    hit = EvidenceHit(source_id=source.id, start=0, end=QUOTE_LIMIT, quote=text,
                      score=1.0, leaf_start=0, leaf_end=QUOTE_LIMIT)
    entry = agent_tools._citable_hits([hit], [source])[0]
    assert entry["evidence_start"] == 0
    assert entry["evidence_end"] == QUOTE_LIMIT
    assert len(entry["evidence_quote"]) == QUOTE_LIMIT == len(text)

    # 契约本身也要收下它：`SourceRef.quote` 的 max_length 就是 300
    ref = {"source_id": source.id, "start": entry["evidence_start"],
           "end": entry["evidence_end"], "quote": entry["evidence_quote"]}
    content = MindMapContent(
        title="t", root=Node(id="r", label="t", children=[
            Node(id="n", label="恰好到上限", source_refs=[ref])]),
        source_usage=[SourceUsage(source_id=source.id, kind=source.kind,
                                  filename=None, char_count=len(text))],
    )
    reports = verify_source_refs(
        content, [source], remove_invalid=True,
        evidence=[EvidenceSpan(source.id, 0, QUOTE_LIMIT, 0, QUOTE_LIMIT)])
    codes = [item.code for item in reports
             if item.code not in ("UNSOURCED_NODE", "SOURCE_PAGE_UNVERIFIED")]
    assert codes == [], codes
    assert content.root.children[0].source_refs[0].quote == text


def test_verify_sources_matches_the_verifier_on_source_status():
    """`_verify_sources` 与 `_reference_problem` 的来源判据必须一致。

    校验器拒绝 `status != "parsed"` 的来源（判 `SOURCE_TEXT_UNAVAILABLE`），所以
    "该认的来源"里也不能收它们。两者口径不同时出现的失败是**静默的**：一份非
    `parsed` 的来源被收进来，模型照着它的正文写引用、逐字全对，收尾时却全被判
    `SOURCE_TEXT_UNAVAILABLE` 剔除——用户看不出为什么，日志里也没有一条。

    **这条不是"将来引入 `partial` 才会触发"**：`Source.status` 目前是
    `Literal["pending", "parsed", "failed"]`，没有 `partial`，而 `pending` 正是
    **默认值**（`status: Literal[...] = "pending"`）。前端上传时省略 `status`
    字段，拿到就是一份带正文的 `pending` 来源。`failed` 同理（重解析失败但正文
    还在）。所以这条现在就有真实触发面。

    将来真加了 `partial`，这个判据（`!= "parsed"`）自动覆盖它，不用再改一次——
    这正是"两边都用同一个条件"而不是"两边各写一份白名单"的理由。
    """
    parsed = _source("正常材料的一段正文。")
    pending = Source(kind=SourceKind.text, status="pending",
                     text="上传时省略了 status：默认值，正文齐全。")
    failed = Source(kind=SourceKind.text, status="failed",
                    text="重解析失败，但上一轮成功的正文还在。")
    blank = _source("   \n  ")
    ctx = _ctx([parsed, pending, failed, blank])

    keep = agent_tools._verify_sources(ctx)

    assert keep == [parsed]


def test_verify_sources_drops_non_parsed_library_sources_too():
    """库来源同样受这条判据管：口径对齐不该只对齐请求内那一半。

    库来源比请求内来源更容易撞上这条——库里存的是历次上传的 PDF 与扫描件，
    `pending` / `failed` 的残留只会在那边积累。
    """
    request_source = _source("请求内材料。")
    parsed = _source("库里的正常材料。")
    pending = Source(kind=SourceKind.text, status="pending", text="库里的待解析材料。")
    ctx = _ctx([request_source])
    ctx.record_library_sources([parsed, pending])

    keep = agent_tools._verify_sources(ctx)

    # 顺序固定"请求的在前、库的在后"
    assert [source.id for source in keep] == [request_source.id, parsed.id]


# ---------------------------------------------------------------- 二、校验接线


def _tree(ref: dict[str, Any]) -> dict[str, Any]:
    """一条一级分支带引用、另外两条不带：3 条分支是 `MIN_BRANCHES` 的下限。"""
    return {
        "title": "证据核验",
        "root": {"id": "root", "label": "根", "children": [
            {"id": "a", "label": "甲", "children": [], "source_refs": [ref]},
            {"id": "b", "label": "乙", "children": []},
            {"id": "c", "label": "丙", "children": []},
        ]},
    }


def test_generate_flags_ref_inside_parent_but_outside_leaf(monkeypatch):
    """生成收尾把 `ctx.evidence` 交出去：越界引用被**标记**，但不再剔除。

    这是整条改动的核心用例：引用**逐字**在原文里（`text[start:end] == quote`），
    只是落在父块中与这次查询无关的那一半。上一版把它剔掉，实测会成批删掉合法引用
    （模型看到的是 450–700 字的父块，不是命中的那 200 字）。现在留一条 warn：
    行为可观测，但不由代码替用户做删改决定。
    """
    source, hit, _leaf, outside = _scene(monkeypatch)
    reference = {
        "source_id": source.id, "start": hit.start,
        "end": hit.start + len(outside), "quote": outside,
    }
    ctx = _ctx([source], intent="create")
    ctx.record_evidence([EvidenceSpan(source.id, hit.start, hit.end,
                                      hit.leaf_start, hit.leaf_end)])

    with patch("app.agent.tools.chat_async", AsyncMock(return_value=_tree(reference))):
        result = asyncio.run(build_registry("create").execute(
            _call("mindmap_generate", "c-gen", topic="整理这些材料"), ctx))

    assert result.ok, result.content
    codes = [item["code"] for item in result.data["diagnostics"]]
    assert "SOURCE_REF_OUTSIDE_EVIDENCE" in codes
    # 分支 a 的引用留着 → 它不该再报"没有可核验引用"（b/c 两条分支本来就没写引用，
    # 它们的 `UNSOURCED_NODE` 是既有语义，与本条无关）
    assert not [item for item in result.data["diagnostics"]
                if item["code"] == "UNSOURCED_NODE" and item["node_id"] == "a"]
    assert [ref.quote for ref in ctx.candidate.root.children[0].source_refs] == [outside]


def test_generate_keeps_ref_inside_leaf_span(monkeypatch):
    """引用落在命中的子块里：一条新码都不出现，引用留着。"""
    source, hit, leaf, _outside = _scene(monkeypatch)
    reference = {
        "source_id": source.id, "start": hit.leaf_start,
        "end": hit.leaf_end, "quote": leaf,
    }
    ctx = _ctx([source], intent="create")

    with patch("app.agent.tools.chat_async", AsyncMock(return_value=_tree(reference))):
        result = asyncio.run(build_registry("create").execute(
            _call("mindmap_generate", "c-gen", topic="整理这些材料"), ctx))

    assert result.ok, result.content
    codes = [item["code"] for item in result.data["diagnostics"]]
    assert "SOURCE_REF_OUTSIDE_EVIDENCE" not in codes
    assert ctx.candidate.root.children[0].source_refs[0].quote == leaf


def test_empty_evidence_matches_no_evidence():
    """没有证据时与改动前逐字节一致：诊断码与树内容都不差一个字符。"""
    source = _source()
    text = source.text or ""
    reference = {"source_id": source.id, "start": 0, "end": 12, "quote": text[:12]}
    assert reference["quote"].strip()

    plain, plain_diagnostics = validate_generated_content(_tree(reference), sources=[source])
    empty, empty_diagnostics = validate_generated_content(
        _tree(reference), sources=[source], evidence=[])

    assert plain.model_dump() == empty.model_dump()
    assert [item.model_dump() for item in plain_diagnostics] \
        == [item.model_dump() for item in empty_diagnostics]
    # 前提：同一条引用在给了父块证据、且它落在命中区之外**会**被判越界，
    # 否则"空证据等价无证据"是空断言。
    span = EvidenceSpan(source.id, 0, 400, 200, 400)
    flagged = verify_source_refs(plain, [source], evidence=[span])
    assert "SOURCE_REF_OUTSIDE_EVIDENCE" in {item.code for item in flagged}


def _out_of_leaf_ref(source: Source, hit) -> dict[str, Any]:
    """父块内、命中区之外的一段引用；偏移与引文都由原文算出来，逐字成立。"""
    text = source.text or ""
    outside = text[hit.start:hit.start + 12]
    return {"source_id": source.id, "start": hit.start,
            "end": hit.start + len(outside), "quote": outside}


def test_full_mode_generation_ignores_evidence(monkeypatch):
    """`full` 形态不施加证据约束：其中合法引用不该因为"没被这次检索命中"被剔掉。

    这是"形态"与"粒度"两件事的分界：`full` 下材料整份在 prompt 里，模型引用一段
    检索没命中的原文是合法且常见的。不门控的话 `full` + 父块开会把这类引用判成
    `SOURCE_REF_OUTSIDE_EVIDENCE` 并剔除（`remove_invalid=True`），用户看不到原因。
    """
    monkeypatch.setenv(SOURCE_MODE_ENV, "full")
    source, hit, _leaf, _outside = _scene(monkeypatch)
    ctx = _ctx([source], intent="create")
    ctx.record_evidence([EvidenceSpan(source.id, hit.start, hit.end,
                                      hit.leaf_start, hit.leaf_end)])

    with patch("app.agent.tools.chat_async",
               AsyncMock(return_value=_tree(_out_of_leaf_ref(source, hit)))):
        result = asyncio.run(build_registry("create").execute(
            _call("mindmap_generate", "c-gen", topic="整理这些材料"), ctx))

    assert result.ok, result.content
    codes = {item["code"] for item in result.data["diagnostics"]}
    assert "SOURCE_REF_OUTSIDE_EVIDENCE" not in codes
    assert len(ctx.candidate.root.children[0].source_refs) == 1


def test_switch_off_evidence_never_flags(monkeypatch):
    """父块关着时，累积到的区间没有叶子坐标，校验不该因此判任何引用越界。"""
    monkeypatch.setenv(PARENT_RETRIEVAL_ENV, "0")
    source = _source()
    ctx = _ctx([source], intent="create")
    rag = _exec_rag(ctx)
    assert rag.ok and rag.data["hits"]
    # 前提：这条路上每条命中的 leaf 都是 None（父块关 = 单级切块）
    assert all(item["leaf_start"] is None for item in rag.data["hits"])
    assert ctx.evidence

    text = source.text or ""
    reference = {"source_id": source.id, "start": 0, "end": 12, "quote": text[:12]}
    with patch("app.agent.tools.chat_async", AsyncMock(return_value=_tree(reference))):
        result = asyncio.run(build_registry("create").execute(
            _call("mindmap_generate", "c-gen", topic="整理这些材料"), ctx))

    assert result.ok, result.content
    codes = {item["code"] for item in result.data["diagnostics"]}
    assert "SOURCE_REF_OUTSIDE_EVIDENCE" not in codes


# ---------------------------------------------------------------- 三、端到端


class _ScriptedProvider:
    """按脚本返回工具调用；与 `tests/test_agent_loop.py` 的同名实现同义。"""

    def __init__(self, script: list[Completion]) -> None:
        self._script = list(script)
        self.calls = 0

    async def complete_tools(self, messages, tools, *, budget):
        self.calls += 1
        if not self._script:
            raise AssertionError("脚本已用完：循环比预期多调了一次模型")
        return self._script.pop(0)


def _completion(call: ToolCall | None = None) -> Completion:
    message: dict[str, Any] = {"role": "assistant", "content": "好的"}
    calls = [call] if call else []
    if call:
        message["tool_calls"] = [{
            "id": call.call_id, "type": "function",
            "function": {"name": call.name,
                         "arguments": json.dumps(call.arguments, ensure_ascii=False)},
        }]
    return Completion(message=message, tool_calls=calls,
                      finish_reason="stop" if call is None else "tool_calls",
                      usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})


def _drain(request: AgentRunRequest, provider) -> list:
    async def collect():
        return [event async for event in run(request, provider=provider)]

    return asyncio.run(collect())


def test_end_to_end_rag_mode_cites_evidence_and_flags_the_rest(monkeypatch):
    """`SOURCE_MODE=rag` + 父块默认开：抄 `evidence_*` 通过，抄父块其它位置被标记。

    全程走真的 agent 循环：模型先 `rag_query`（工具自己累积证据），再
    `mindmap_generate`（工具收尾交给校验）。中间没有手工塞进去的 `EvidenceSpan`
    ——喂给模型的树是按"模型会从检索结果里抄"的形态写死的。
    """
    monkeypatch.setenv(SOURCE_MODE_ENV, "rag")
    # 默认开的验证点之一：删掉 conftest 写的 "0"，让默认值生效
    monkeypatch.delenv(PARENT_RETRIEVAL_ENV, raising=False)

    source, hit, leaf, outside = _scene(monkeypatch)
    text = source.text or ""
    assert text[hit.start:hit.start + len(outside)] == outside

    good = {"source_id": source.id, "start": hit.leaf_start,
            "end": hit.leaf_end, "quote": leaf}
    bad = {"source_id": source.id, "start": hit.start,
           "end": hit.start + len(outside), "quote": outside}

    def build(ref: dict[str, Any]):
        tree = _tree(ref)
        provider = _ScriptedProvider([
            _completion(_call("rag_query", "c-rag", question=QUERY)),
            _completion(_call("mindmap_generate", "c-gen", topic="整理这些材料")),
            _completion(),
        ])
        with patch("app.agent.tools.chat_async", AsyncMock(return_value=tree)):
            events = _drain(_request([source]), provider)
        ends = [event for event in events if event.type == "tool_end"]
        rag_end = next(event for event in ends if event.name == "rag_query")
        gen_end = next(event for event in ends if event.name == "mindmap_generate")
        done = next(event for event in events if event.type == "done")
        return rag_end, gen_end, done

    rag_end, gen_end_good, done_good = build(good)
    # 前提：检索真的回了父块（否则这组用例测的是单级切块）
    first = rag_end.result_summary["hits"][0]
    assert first["leaf_start"] is not None and first["leaf_end"] is not None
    assert first["end"] - first["start"] > first["leaf_end"] - first["leaf_start"]
    assert "SOURCE_REF_OUTSIDE_EVIDENCE" not in {
        item["code"] for item in gen_end_good.result_summary["diagnostics"]}
    assert done_good.content.root.children[0].source_refs[0].quote == leaf

    _rag_end, gen_end_bad, done_bad = build(bad)
    assert "SOURCE_REF_OUTSIDE_EVIDENCE" in {
        item["code"] for item in gen_end_bad.result_summary["diagnostics"]}
    # 父块里、命中区之外：**保留**并标记。剔除这条口子在本轮关掉了——模型引的这段
    # 逐字在原文里，只是不在这轮命中的那 200 字内。
    assert [ref.quote for ref in done_bad.content.root.children[0].source_refs] == [outside]


# ---------------------------------------------------------------- 四、默认值


def test_rag_default_switch_returns_parent_blocks(monkeypatch):
    """不设变量时 `_rag` 拿到的就是父块：`leaf_*` 有值、父块比命中段大。"""
    monkeypatch.delenv(PARENT_RETRIEVAL_ENV, raising=False)
    source = _source()
    result = _exec_rag(_ctx([source]))

    assert result.ok, result.content
    hits = result.data["hits"]
    assert hits
    assert all(item["leaf_start"] is not None and item["leaf_end"] is not None
               for item in hits)
    assert all(item["end"] - item["start"] > item["leaf_end"] - item["leaf_start"]
               for item in hits)


def test_rag_switch_off_matches_single_level_search(monkeypatch):
    """`MINDMAP_PARENT_RETRIEVAL=0` 时与单级切块逐字段相同（回归闸）。"""
    monkeypatch.setenv(PARENT_RETRIEVAL_ENV, "0")
    source = _source()
    result = _exec_rag(_ctx([source]))

    assert result.ok, result.content
    expected = [hit.model_dump() for hit in search_sources([source], QUERY, 5)]
    assert result.data["hits"] == expected
    assert all(item["leaf_start"] is None and item["leaf_end"] is None
               for item in expected)


def test_full_mode_also_uses_parent_retrieval(monkeypatch):
    """`full` 形态下父块召回同样开：这个开关只影响检索，与 prompt 形态无关。

    确认有没有隐含耦合：`_rag` 里没有任何一处读 `source_mode()`——`full` 下模型
    看得见全文、引用任何段落都合法，所以"要不要施加证据约束"由形态决定（那在
    `_generate` 的 `sources`／`ctx.evidence` 上），"检索回什么粒度"不由形态决定。
    两边各管各的，这里钉住的是后一半。
    """
    monkeypatch.setenv(SOURCE_MODE_ENV, "full")
    monkeypatch.delenv(PARENT_RETRIEVAL_ENV, raising=False)
    source = _source()
    result = _exec_rag(_ctx([source]))

    assert result.ok, result.content
    assert result.data["hits"]
    assert all(item["leaf_start"] is not None for item in result.data["hits"])


def test_default_switch_value_is_read_at_call_time():
    """默认值本身：`os.getenv(..., "1")` 的落点是真值集合里的 `"1"`。

    这一条不 monkeypatch 环境变量（conftest 会写 "0"），所以它断言两件事：
    默认值在真值集合内，且"变量存在且为 0"时判定为关。行为断言在上面几条里
    ——它们是"读环境变量那一刻"的证据。
    """
    assert "1" in _PARENT_RETRIEVAL_TRUTHY
    # conftest 把变量写成 "0"：这时必须是关，否则上面那条"=0 逐字段相同"的
    # 回归闸就形同虚设。
    assert os.environ[PARENT_RETRIEVAL_ENV] == "0"
    assert _parent_retrieval_enabled() is False
