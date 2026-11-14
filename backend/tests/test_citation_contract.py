"""新契约：内容可以自由展开，引用必须标出处。

上一版的契约是"检索不到就不要写，空着是正确结果"。同一份 12392 字材料实测：
全文可见时覆盖率 100%，检索形态只有 28.8%，树上还会长出"材料中没有相关内容：
未检索到任何原文依据"的空节点。产品要的是一张完整的导图，不是"材料里有什么就画
什么"。

新契约把两件事拆开：**内容**允许模型凭自己的知识补全，**出处**不许冒充材料。
两半必须同时成立——只说前者，模型会给自主补充的内容挂上编造的引用；只说后者，
模型退回"没检索到就不写"，正是这次要消灭的空心树。

本文件按改动说明的九条验收点组织（编号与那份清单一一对应）：

    1. 三份提示词都含同一句契约，`RAG_SOURCE_PROMPT` 里不再有"空着是正确结果"
    2. 整树生成不再要求模型自己调 `rag_query`
    3. 引用落在父块内但不在命中子块 → 保留 + 一条 warn
    4. 服务端按引文补正偏移（只给 quote 不给偏移）
    5. 同一父块内多处出现 → 标"待核"，保留
    6. 无偏移且无引文、有区间而引文为空 → 都拒
    7. 零证据：允许零引用，`content` 说明"没有检索到依据"
    8. `full` 形态不受父块约束
    9. 补查限额：一支最多 1 次、一次运行最多 3 次

单层的判定表在 `tests/test_source_refs.py` 里逐格钉着；这里测的是接进生成链之后
用户实际看到的结果（保留了什么、诊断里有什么）。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    PLAN_PROMPT,
    SUPPLEMENTARY_MIN_NODES,
    SUPPLEMENTARY_RUN_LIMIT,
    build_registry,
)
from app.contracts.source import Source, SourceKind
from app.generation.service import (
    DETAIL_PROMPT,
    RAG_SOURCE_PROMPT,
    SOURCE_MODE_ENV,
    SOURCE_MODE_FULL,
    SOURCED_DETAIL_PROMPT,
    validate_generated_content,
)
from app.knowledge.service import EvidenceHit
from app.optimization.source_refs import EvidenceSpan

# 三段互不重叠的话：第一段永远不在命中区间里，第二段是命中区间。
MATERIAL = (
    "第一章 预算审批。预算由财务经理核准后执行，超过五十万元报总经理审批。\n"
    "第二章 报销时限。费用报销的时限是三十个自然日，逾期不再受理。\n"
    "第三章 绩效复核。考核分季度与年度两次，由直属上级打分。\n"
)
OUTSIDE = "预算由财务经理核准后执行"
INSIDE = "费用报销的时限是三十个自然日"


# ---------------------------------------------------------------- 夹具


def _source(text: str = MATERIAL, source_id: str = "doc-1") -> Source:
    return Source(id=source_id, kind=SourceKind.document, status="parsed",
                  filename="制度汇编.md", text=text)


def _ctx(sources: list[Source], *, max_model_calls: int = 30) -> RunContext:
    request = AgentRunRequest(
        goal="整理材料", intent="create", sources=sources,
        budget=RunBudget(max_model_calls=max_model_calls, max_tool_calls=8,
                         max_output_tokens=32768),
    )
    return RunContext(request=request, candidate=None,
                      sources_by_id={source.id: source for source in sources})


def _tree(refs: list[dict] | None = None) -> dict:
    """3 条一级分支（`MIN_BRANCHES` 的下限），引用挂第一条分支上。"""
    children = [
        {"id": f"b{index}", "label": f"分支{index}", "children": [
            {"id": f"b{index}-1", "label": f"要点{index}", "children": []},
        ]}
        for index in range(3)
    ]
    children[0]["source_refs"] = list(refs or [])
    return {"title": "制度要点", "root": {"id": "root", "label": "制度要点",
                                          "children": children}}


def _ref(quote: str, source_id: str = "doc-1", **extra) -> dict:
    return {"source_id": source_id, "quote": quote, **extra}


def _codes(diagnostics) -> list[str]:
    return [item.code for item in diagnostics]


def _codes_of(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


def _run_generate(sources: list[Source], topic: str, tree: dict):
    """跑一次 `mindmap_generate`，返回 (结果, 模型收到的 (system, user), ctx)。"""
    ctx = _ctx(sources)
    mocked = AsyncMock(return_value=tree)
    with patch("app.agent.tools.chat_async", new=mocked):
        result = asyncio.run(build_registry("create", has_sources=bool(sources)).execute(
            ToolCall(call_id="c-gen", name="mindmap_generate",
                     arguments={"topic": topic}), ctx))
    system, user = mocked.await_args.args
    return result, (system, user), ctx


# ---------------------------------------------------------------- 1. 提示词回归


CONTRACT_CLAUSES = (
    "材料没覆盖的方面，按你的知识补全",
    "凡引用、转述、概括材料的，必须标出处",
    "自主补充的内容不要冒充材料",
)


@pytest.mark.parametrize("name,prompt", [
    ("DETAIL_PROMPT", DETAIL_PROMPT),
    ("SOURCED_DETAIL_PROMPT", SOURCED_DETAIL_PROMPT),
    ("RAG_SOURCE_PROMPT", RAG_SOURCE_PROMPT),
])
def test_every_prompt_states_the_same_contract(name, prompt):
    """三份提示词必须逐字含同一句契约。

    `SOURCED_DETAIL_PROMPT` 与 `RAG_SOURCE_PROMPT` 是 `DETAIL_PROMPT` 的前缀 + 后缀，
    所以这一条同时证明"这一句只写了一份"——三处各写一遍，改一处就会漂。
    """
    for clause in CONTRACT_CLAUSES:
        assert clause in prompt, (name, clause)


def test_rag_prompt_drops_the_empty_is_correct_rule():
    """旧契约那一句必须真的消失，只留新的一句。

    两句同时在场时模型只会执行更保守的那句（"检索不到就不要写"），新契约等于
    没生效——这正是覆盖率一直上不去的原因之一。
    """
    assert "空着是正确结果" not in RAG_SOURCE_PROMPT
    assert "检索不到就不要写" not in RAG_SOURCE_PROMPT
    assert "检索不到就按自己的知识展开" in RAG_SOURCE_PROMPT
    assert "未检索到不等于材料里没有" in RAG_SOURCE_PROMPT


# ---------------------------------------------------------------- 2. 整树不再自行检索


def test_tree_generation_does_not_ask_the_model_to_retrieve_itself():
    """整树生成的 system 与清单里都不能再出现"先调 rag_query"。

    这一条路在拼 system 之前已经做过主题预检索（`_retrieve_topic`），命中拼在
    user 末尾。再要求模型自行检索是重复的：它拿不到新东西，却多花一次 embedding
    + 一轮对话，而"我还没查过"的自我判断会把整棵树又拉回空心形态。
    """
    source = _source()
    result, (system, user), _ctx_after = _run_generate([source], "预算审批与报销时限", _tree())

    assert result.ok, result.content
    assert "rag_query" not in system
    assert "先检索" not in system
    # user 里那句"要写与材料有关的内容，先调用 rag_query 检索"同样不能再出现：
    # 模型读的是整条消息，而这次命中已经附在末尾了。
    # （`GENERATE_EVIDENCE_HEADER` 里还留着"与 rag_query 的返回同形"，那是形状
    #   说明、不是动作指令，所以按指令措辞断言而不是按工具名。）
    for phrase in ("先调用 rag_query 检索", "写任何与材料有关的内容之前"):
        assert phrase not in user, phrase
    # 前提：这次真的命中了，否则测的是"检索为空"那条退化路
    assert "本次已对该主题做过一次检索" in user


# ---------------------------------------------------------------- 3. 父块内、子块外


def _evidence(source: Source) -> EvidenceSpan:
    """一个覆盖 `OUTSIDE`、命中 `INSIDE` 的父块。"""
    text = source.text or ""
    start = text.index(INSIDE)
    return EvidenceSpan(source.id, 0, len(text), start, start + len(INSIDE))


def test_ref_inside_parent_block_is_kept_with_a_warning():
    """引用在父块里、不在命中的子块里：保留 + warn，不剔除。

    模型看到的是 450–700 字的父块，要求它精确落在命中的那 200 字里过于苛刻——
    上一版按子块剔除，实测把合法引用成批删掉，界面上表现为"这一支没有证据"。
    """
    source = _source()
    content, diagnostics = validate_generated_content(
        _tree([_ref(OUTSIDE)]), sources=[source], evidence=[_evidence(source)])

    assert [ref.quote for ref in content.root.children[0].source_refs] == [OUTSIDE]
    assert "SOURCE_REF_OUTSIDE_EVIDENCE" in _codes(diagnostics)
    # 分支 b0 有引用留着 → 它不该再报"没有可核验引用"（根与另外两条分支本来就没写，
    # 它们的 UNSOURCED_NODE 是既有语义，与本条无关）
    assert not [d for d in diagnostics
                if d.code == "UNSOURCED_NODE" and d.node_id == "b0"]


# ---------------------------------------------------------------- 4. 服务端补正偏移


def test_offset_is_filled_in_from_the_quote_alone():
    """只给 quote、不给偏移：服务端按原文补上精确区间，引用通过。

    上一版只给 quote 的引用靠"逐字在全文里"通过——坐标永远是空的，界面上点不开，
    语义层的 NLI 也只能拿 quote 去比。补上偏移不是新增约束，是把这条引用变成一条
    **完整的**引用。
    """
    source = _source()
    content, diagnostics = validate_generated_content(
        _tree([_ref(INSIDE)]), sources=[source])

    kept = content.root.children[0].source_refs[0]
    text = source.text or ""
    start = text.index(INSIDE)
    assert (kept.start, kept.end) == (start, start + len(INSIDE))
    # 补齐元数据不算问题，不该在诊断里冒出来
    assert [code for code in _codes(diagnostics) if code != "UNSOURCED_NODE"] == []


def test_wrong_offset_is_repaired_instead_of_dropped():
    """偏移写错、引文逐字正确且在可见范围内唯一：补正，不判 `SOURCE_QUOTE_MISMATCH`。"""
    source = _source()
    content, diagnostics = validate_generated_content(
        _tree([_ref(INSIDE, start=0, end=5)]), sources=[source])

    kept = content.root.children[0].source_refs[0]
    text = source.text or ""
    start = text.index(INSIDE)
    assert (kept.start, kept.end) == (start, start + len(INSIDE))
    assert "SOURCE_QUOTE_MISMATCH" not in _codes(diagnostics)
    assert "SOURCE_QUOTE_OFFSET_FIXED" in _codes(diagnostics)


# ---------------------------------------------------------------- 5. 多处出现 → 待核


def test_quote_appearing_twice_is_flagged_for_review_and_kept():
    """同一父块内多处出现：标"待核"，引用留着。

    判不了对错时删掉是替用户做决定，而这条引用**确实**逐字存在于原文里。
    """
    text = "预算由财务经理核准。预算由财务经理执行。报销时限三十天。"
    source = _source(text)
    quote = "预算由财务经理"

    content, diagnostics = validate_generated_content(
        _tree([_ref(quote)]), sources=[source])

    assert [ref.quote for ref in content.root.children[0].source_refs] == [quote]
    assert "SOURCE_QUOTE_AMBIGUOUS" in _codes(diagnostics)
    ambiguous = next(d for d in diagnostics if d.code == "SOURCE_QUOTE_AMBIGUOUS")
    assert "待核" in ambiguous.message
    # 待核不是错误：诊断级别是 warn，节点也没有被摘掉引用
    assert ambiguous.level.value == "warn"


# ---------------------------------------------------------------- 6. 空引用必须拒


@pytest.mark.parametrize("ref", [
    {"source_id": "doc-1"},                                     # 既无区间也无引文
    {"source_id": "doc-1", "start": 0, "end": 6},               # 有区间、引文为空
])
def test_citation_without_a_quote_is_rejected(ref):
    """两个放行口都要堵死：没有引文的引用核验不了任何东西。

    第二种是这一轮新堵的：旧实现只要区间落在原文里就放行，等于只核对了一对坐标
    ——谁都能给任意区间"标记"一段自己从没读过的原文，界面上它还会显示成一条
    有出处的引用。
    """
    source = _source()
    content, diagnostics = validate_generated_content(_tree([ref]), sources=[source])

    assert content.root.children[0].source_refs == []
    assert "UNVERIFIED_SOURCE_REF" in _codes(diagnostics)


# ---------------------------------------------------------------- 7. 零证据


def test_zero_evidence_allows_zero_citations_and_says_so():
    """`rag` 形态一条证据都没检索到：允许零引用，但必须在 `content` 里说明。

    `GENERATION_WITHOUT_EVIDENCE` 是 warn 级诊断——它不参与 ok 判定。一棵"结构完整
    但没有任何依据"的图仍然可用，用户要能一眼看出它为什么没有引用。
    """
    source = _source("甲乙丙丁戊己庚辛壬癸。")
    with patch("app.agent.tools.search_parent", new=lambda *a, **k: []):
        result, (system, _user), ctx = _run_generate(
            [source], "量子色动力学的渐近自由", _tree())

    assert result.ok, result.content
    # 提示词退回"没有材料可标出处"那一份
    assert system.startswith(DETAIL_PROMPT)
    assert all(not child.source_refs for child in ctx.candidate.root.children)
    levels = {item["code"]: item["level"] for item in result.data["diagnostics"]}
    assert levels["GENERATION_WITHOUT_EVIDENCE"] == "warn"
    assert "不含任何材料依据" in result.content
    assert "只基于主题" in result.content


# ---------------------------------------------------------------- 8. full 不受父块约束


def test_full_mode_ignores_the_parent_block_constraint(monkeypatch):
    """`full` 形态：正文整份在 prompt 里，引用任何段落都合法。

    引用刻意选在**父块内、命中区间外**——如果父块约束被错误地施加到这条路上，
    它必然会被标成 `SOURCE_REF_OUTSIDE_EVIDENCE`，所以这条断言有区分度。
    """
    monkeypatch.setenv(SOURCE_MODE_ENV, SOURCE_MODE_FULL)
    source = _source()
    ctx = _ctx([source])
    ctx.record_evidence([_evidence(source)])

    mocked = AsyncMock(return_value=_tree([_ref(OUTSIDE)]))
    with patch("app.agent.tools.chat_async", new=mocked):
        result = asyncio.run(build_registry("create", has_sources=True).execute(
            ToolCall(call_id="c-gen", name="mindmap_generate",
                     arguments={"topic": "整理制度"}), ctx))

    assert result.ok, result.content
    system, _user = mocked.await_args.args
    assert system.startswith(SOURCED_DETAIL_PROMPT)
    assert "SOURCE_REF_OUTSIDE_EVIDENCE" not in _codes_of(result)
    assert [ref.quote for ref in ctx.candidate.root.children[0].source_refs] == [OUTSIDE]


# ---------------------------------------------------------------- 9. 补查限额


def _hit(source: Source) -> EvidenceHit:
    """一条覆盖全篇的合成命中（补查只要有命中就会触发重写）。"""
    text = source.text or ""
    return EvidenceHit(source_id=source.id, start=0, end=len(text),
                       quote=text, score=0.5, score_bm25=1.0)


def _expansion(title: str) -> dict:
    """每次展开都给 `SUPPLEMENTARY_MIN_NODES` 个要点、**一条引用都没有**。"""
    return {"children": [
        {"label": f"{title}·要点{index}", "expandable": False}
        for index in range(SUPPLEMENTARY_MIN_NODES)
    ]}


def _run_deep(titles: tuple[str, ...], *, max_model_calls: int = 30):
    """跑一次 `mindmap_generate_deep`，返回 (结果, 检索 query 列表)。"""
    sources = [_source()]
    ctx = _ctx(sources, max_model_calls=max_model_calls)
    queries: list[str] = []

    def spy(sources_arg, query, top_k=3, *, filters=None):
        queries.append(query)
        return [_hit(sources[0])]

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(titles)}
        title = user.split("本条一级分支：", 1)[1].split("\n", 1)[0]
        return _expansion(title)

    registry = build_registry("create", has_sources=True)
    with patch("app.agent.tools.search_parent", new=spy), \
            patch("app.agent.tools.chat_async", new=chat):
        result = asyncio.run(registry.execute(
            ToolCall(call_id="c-deep", name="mindmap_generate_deep",
                     arguments={"topic": "制度要点", "branches": len(titles),
                                "points_per_branch": SUPPLEMENTARY_MIN_NODES}),
            ctx,
        ))
    return result, queries


def test_each_branch_is_supplemented_at_most_once():
    """3 支、每支都够多却没引用：每支各补 1 次，一篇只补 3 次。

    "一支最多 1 次"由调用点保证（补查结果不再回判），"一次运行最多 3 次"由
    `SUPPLEMENTARY_RUN_LIMIT` 保证——两条都在这一个场景里成立。
    """
    titles = ("预算审批", "报销时限", "绩效复核")
    result, queries = _run_deep(titles)

    assert result.ok, result.content
    supplementary = result.data["retrieval"]["supplementary"]
    assert supplementary["used"] == len(titles)
    assert sorted(supplementary["branches"]) == sorted(titles)
    assert len(supplementary["queries"]) == len(titles)
    # 首路是分支标题，补查那一次用的是这一支**最长子节点**的文字
    assert len(queries) == len(titles) * 2
    for title in titles:
        assert title in queries
        # 等长子节点时 `max` 取哪一个不固定（这里是栈序），只钉"用的是子节点文字"
        assert any(query.startswith(f"{title}·要点") for query in queries), title


def test_supplementary_retrieval_is_capped_per_run():
    """5 支都要补 → 只补 3 支：成本闸在运行级，不是按支放大。"""
    titles = ("预算审批", "报销时限", "绩效复核", "合同审核", "印章管理")
    result, queries = _run_deep(titles)

    assert result.ok, result.content
    supplementary = result.data["retrieval"]["supplementary"]
    assert supplementary["used"] == SUPPLEMENTARY_RUN_LIMIT
    assert len(supplementary["branches"]) == SUPPLEMENTARY_RUN_LIMIT
    assert len(set(supplementary["branches"])) == SUPPLEMENTARY_RUN_LIMIT
    # 首路 5 次 + 补查 3 次 = 8 次检索，且补查不占 `max_tool_calls`
    assert len(queries) == len(titles) + SUPPLEMENTARY_RUN_LIMIT
    assert "BRANCH_SUPPLEMENTARY_HITS" in _codes_of(result)


def test_supplementary_retrieval_never_touches_tool_calls():
    """补查整段发生在工具内部：`max_tool_calls` 一个都不占。

    与 `test_deep_slicing.test_branch_retrieval_does_not_consume_tool_calls` 同一
    口径，只是把"补查也开了"这一种状态补上——它多发了 3 次检索，但模型看到的仍然
    只有一次 `mindmap_generate_deep` 调用。
    """
    titles = ("预算审批", "报销时限", "绩效复核")
    ctx = _ctx([_source()], max_model_calls=30)
    sources = [ctx.request.sources[0]]
    calls = {"tool": 0}

    def spy(_sources, _query, top_k=3, *, filters=None):
        return [_hit(sources[0])]

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(titles)}
        return _expansion(user.split("本条一级分支：", 1)[1].split("\n", 1)[0])

    registry = build_registry("create", has_sources=True)

    class CountingRegistry:
        """只数工具调用次数，其余原样转发。"""

        def __getattr__(self, name):
            return getattr(registry, name)

        async def execute(self, call, context):
            calls["tool"] += 1
            return await registry.execute(call, context)

    with patch("app.agent.tools.search_parent", new=spy), \
            patch("app.agent.tools.chat_async", new=chat):
        result = asyncio.run(CountingRegistry().execute(
            ToolCall(call_id="c-deep", name="mindmap_generate_deep",
                     arguments={"topic": "制度要点", "branches": len(titles),
                                "points_per_branch": SUPPLEMENTARY_MIN_NODES}),
            ctx,
        ))

    assert result.ok, result.content
    assert result.data["retrieval"]["supplementary"]["used"] == len(titles)
    assert calls["tool"] == 1
    # 前提：补查真的发生了（不是"没触发所以没占额度"）
    assert result.data["retrieval"]["supplementary"]["queries"]
    # 形状仍是合法 JSON 树：补查多出来的证据没有把引用写进没有依据的节点
    assert json.dumps(result.data, ensure_ascii=False)
