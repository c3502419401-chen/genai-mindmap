"""第三段的第四种证据形态：**每个节点在它自己那一章内检索**（`MINDMAP_DETAIL_RETRIEVAL`）。

这条路要成立的四个条件，这个文件逐条钉住——每一条失效时的样子都是"说明照常写出来，
只是内容飘到别的章上"，从产出上看不出来：

  1. **范围是章**：检索时喂给切块的来源必须**就是这一章的正文**（`_chapter_source`），
     不是别的章、不是整份材料；命中坐标平移回全文口径之后仍落在 `[章首, 章末)`，
     且 `quote == 原文[start:end]`（与 `SourceRef` 同一口径）；
  2. **query 是 label**：章内重名时才补父 label（不重名时拼接只是噪声）；
  3. **块跨节点去重**：同一章的 5 个节点命中的是同一批父块，渲染时只出现一次，
     节点只记编号——否则"按章限定"会被复制成五份整章；
  4. **开关关掉逐字节不变**：不检索、不新增 `data` 键、`_detail_ask` 的输出与加这条
     路之前一字不差。

再加两条降级：一条检索抛错只让**那一个**节点没有证据；一条都没命中时退回原来的
`sections`（那是**已有**的证据，换成空块池等于把这一段退化成凭知识写）。
"""
from __future__ import annotations

import asyncio

import pytest

from app.agent import tools as tools_module
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext
from app.agent.tools import (
    DETAIL_RETRIEVAL_MAX_CHARS,
    DETAIL_RETRIEVAL_TOP_K,
    DETAIL_ONLY_PROMPT,
    DETAIL_ONLY_RETRIEVED_RULES,
    _DetailBlock,
    _DetailEvidence,
    _chapter_slices,
    _detail_ask,
    _detail_evidence,
    _detail_evidence_section,
    _detail_queries,
    _fill_details,
    _node_chapters,
    _iter_paths,
)
from app.contracts.mindmap import MindMapContent, Node
from app.contracts.source import Source, SourceKind
from app.knowledge.service import EvidenceHit, search_exact_blocks

TOPIC = "检索增强"

MATERIAL = (
    "# 检索增强手记\n\n"
    "本文记录三个决定：切块、检索、引用。\n\n"
    "## 切块与粒度\n\n"
    "切块决定检索单元。定长切块便宜但会切断句子，递归切块按结构降级；块太大时命中里"
    "混着无关内容，块太小时一句话被拆成两半。目标长度 450 字、上限 700 字。\n\n"
    "## 混合检索\n\n"
    "词面检索擅长精确串，向量检索擅长近义改写。两路各自归一化之后用 RRF 融合名次，"
    "不做分数归一；融合之前先看去重，重叠过半的块只留一条。\n\n"
    "## 引用与校验\n\n"
    "引用必须逐字回到原文。定位不唯一时标待核，而不是猜一个坐标；找不到的引用直接丢，"
    "节点保留。没有出处的句子与编造在界面上长得一样，所以要能点回原文。\n"
)

DETAIL_TEXT = (
    "这一条说明写到八十个字以上，讲清它是什么、怎么做、有什么条件，"
    "这样第三段的产出才符合长度目标。"
)


def _source() -> Source:
    return Source(id="s-rag", kind=SourceKind.markdown, status="parsed",
                  filename="rag.md", text=MATERIAL)


def _tree() -> tuple[MindMapContent, list]:
    """三个一级分支（= 三章），每支两个要点。id 用 `s{章号}` 那一套。"""
    children = [
        Node(id=f"s{index}", label=f"{index} {title}", children=[
            Node(id=f"s{index}-1", label=f"{title}要点一"),
            Node(id=f"s{index}-2", label=f"{title}要点二"),
        ])
        for index, title in enumerate(("切块与粒度", "混合检索", "引用与校验"), start=1)
    ]
    content = MindMapContent(title=TOPIC, root=Node(id="root", label=TOPIC,
                                                   children=children))
    return content, _chapter_slices([_source()])


def _ctx() -> RunContext:
    source = _source()
    return RunContext(request=AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default", sources=[source],
        budget=RunBudget(max_model_calls=20, max_tool_calls=4, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=1),
    ), sources_by_id={source.id: source})


def _answers_ids(system: str, user: str) -> dict[str, str]:
    ids = [line.split("｜", 1)[0].split("=", 1)[1]
           for line in user.splitlines()
           if line[:1].isdigit() and "id=" in line and "｜" in line]
    return {node_id: DETAIL_TEXT for node_id in ids}


def _run_fill(*, chapters, source, monkeypatch, hits=None) -> tuple[MindMapContent, dict]:
    """跑一次第三段；`hits` 是替身检索的结果（缺省按 query 命中本章第一段）。"""
    seen: list[tuple[list[Source], str]] = []

    def fake_search(sources, query, top_k=3):
        seen.append((sources, query))
        if hits is not None:
            return hits(sources, query, top_k)
        body = sources[0].text
        return [EvidenceHit(source_id=sources[0].id, start=0, end=min(len(body), 120),
                            quote=body[:120], score=1.0, leaf_start=0,
                            leaf_end=min(len(body), 60))]

    async def fake_chat(ctx, system, user, **kwargs):
        return _answers_ids(system, user)

    content, _ = _tree()
    diagnostics: list = []
    monkeypatch.setattr(tools_module, "search_exact_blocks", fake_search)
    monkeypatch.setattr(tools_module, "_chat", fake_chat)
    filled, report = asyncio.run(_fill_details(
        content, ctx=_ctx(), topic=TOPIC, sections=(), external_rules="",
        diagnostics=diagnostics, chapters=chapters, source=source))
    return filled, report, seen


# ---------------------------------------------------------------- 1. 开关关掉不变


def test_switch_off_never_searches_and_adds_no_key(monkeypatch):
    """关着：一次检索都不发，`data.detail` 里没有 `evidence` 键。"""
    monkeypatch.delenv("MINDMAP_DETAIL_RETRIEVAL", raising=False)
    content, chapters = _tree()
    calls: list[int] = []

    async def fake_chat(ctx, system, user, **kwargs):
        return _answers_ids(system, user)

    def boom(*args, **kwargs):
        calls.append(1)
        raise AssertionError("开关关着时不该检索")

    monkeypatch.setattr(tools_module, "search_exact_blocks", boom)
    monkeypatch.setattr(tools_module, "_chat", fake_chat)
    filled, report = asyncio.run(_fill_details(
        content, ctx=_ctx(), topic=TOPIC, sections=["（整份材料）"], external_rules="",
        diagnostics=[], chapters=chapters, source=_source()))

    assert calls == []
    assert "evidence" not in report
    assert report["filled"] == report["requested"] == 10
    first = next(node for node, _ in _iter_paths(filled.root))
    assert first.detail == DETAIL_TEXT


def test_ask_is_byte_identical_when_no_evidence_is_given():
    """`_detail_ask` 不带 evidence 时与加这条路的参数之前一字不差。"""
    content, _ = _tree()
    targets = [(node, path) for node, path in _iter_paths(content.root)]
    legacy = _detail_ask(TOPIC, targets, ["（整份材料）"], full_text=True)
    assert _detail_ask(TOPIC, targets, ["（整份材料）"], full_text=True,
                       evidence="") == legacy
    assert "原文块" not in legacy


# ---------------------------------------------------------------- 2. 范围是章


def test_each_query_is_issued_against_that_chapter_only(monkeypatch):
    """喂给检索的来源**就是这一章的正文**：别的章一个字都进不去。"""
    monkeypatch.setenv("MINDMAP_DETAIL_RETRIEVAL", "1")
    source = _source()
    _content, chapters = _tree()
    _filled, report, seen = _run_fill(chapters=chapters, source=source,
                                      monkeypatch=monkeypatch)

    assert len(seen) == report["evidence"]["queries"] == 9   # 10 个节点，根不在内
    chapter_bodies = {source.text[c.start:c.end] for c in chapters}
    for sources, _query in seen:
        assert len(sources) == 1
        assert sources[0].id == source.id
        assert sources[0].text in chapter_bodies
    # 每一章都真被问过（不是全挤在一章上）
    assert len({sources[0].text for sources, _ in seen}) == len(chapters)


def test_blocks_stay_inside_their_chapter_and_quote_is_exact(monkeypatch):
    """块坐标平移回全文口径之后仍在本章内，且 `quote == 原文[start:end]`。"""
    monkeypatch.setenv("MINDMAP_DETAIL_RETRIEVAL", "1")
    source = _source()
    content, chapters = _tree()
    mapping = _node_chapters(content.root, chapters)
    evidence = asyncio.run(_detail_evidence(
        [(node, path) for node, path in _iter_paths(content.root) if node.id in mapping],
        mapping=mapping, source=source))
    assert evidence.blocks
    by_index = {chapter.index: chapter for chapter in chapters}
    for block in evidence.blocks:
        chapter = by_index[block.chapter_index]
        assert chapter.start <= block.start < block.end <= chapter.end
        assert block.quote == source.text[block.start:block.end]
        assert block.quote.strip()


def test_root_has_no_chapter(monkeypatch):
    """根节点不属于任何一章：它不进映射，也就不该被检索。"""
    content, chapters = _tree()
    mapping = _node_chapters(content.root, chapters)
    assert "root" not in mapping
    assert all(node_id != "root" for node_id, _ in
               _detail_queries([(node, path) for node, path in _iter_paths(content.root)],
                               mapping).items())


# ---------------------------------------------------------------- 3. query


def test_query_is_the_label_and_parent_only_when_repeated(monkeypatch):
    """不重名的 label 直接发；同一章里重名时补父 label（父与自己同名则不补）。"""
    _content, chapters = _tree()
    mapping = {f"s{index}": chapters[index - 1] for index in range(1, 4)}
    mapping.update({f"s{index}-1": chapters[index - 1] for index in range(1, 4)})
    mapping.update({f"s{index}-2": chapters[index - 1] for index in range(1, 4)})
    targets = [
        (Node(id="s1-1", label="实施要点"), ("1 切块与粒度",)),
        (Node(id="s1-2", label="实施要点"), ("1 切块与粒度",)),
        (Node(id="s2-1", label="融合方式"), ("2 混合检索",)),
        (Node(id="s3-1", label="融合方式"), ("3 引用与校验",)),
    ]
    questions = _detail_queries(targets, mapping)
    # 同章重名 → 父 + 自己；跨章重名不算重名（范围本来就不同）
    assert questions["s1-1"] == "1 切块与粒度 实施要点"
    assert questions["s1-2"] == "1 切块与粒度 实施要点"
    assert questions["s2-1"] == "融合方式"
    assert questions["s3-1"] == "融合方式"


# ---------------------------------------------------------------- 4. 块池


def test_blocks_are_shared_between_nodes_of_the_same_chapter(monkeypatch):
    """同一章的三个节点命中同一段原文：块池里只有一块，三个节点都指向它。"""
    monkeypatch.setenv("MINDMAP_DETAIL_RETRIEVAL", "1")
    content, chapters = _tree()
    mapping = _node_chapters(content.root, chapters)
    targets = [(node, path) for node, path in _iter_paths(content.root)
               if node.id in mapping]

    def one_hit(sources, query, top_k):
        return [EvidenceHit(source_id=sources[0].id, start=0,
                            end=min(len(sources[0].text), 100),
                            quote=sources[0].text[:100], score=1.0)]

    monkeypatch.setattr(tools_module, "search_exact_blocks", one_hit)
    evidence = asyncio.run(_detail_evidence(targets, mapping=mapping, source=_source()))
    assert len(evidence.blocks) == len(chapters) == 3      # 三章各一块
    assert all(len(indices) == 1 for indices in evidence.by_node.values())
    assert len(evidence.by_node) == len(targets)


def test_rendered_section_lists_a_block_once_with_pointers(monkeypatch):
    """渲染：块只出现一次，节点用编号指过去。"""
    block = _DetailBlock(chapter_index=2, chapter_title="混合检索", source_id="s-rag",
                         start=10, end=20, quote="词面检索擅长精确串。", score=1.0)
    other = _DetailBlock(chapter_index=3, chapter_title="引用与校验", source_id="s-rag",
                         start=40, end=50, quote="引用必须逐字回到原文。", score=1.0)
    evidence = _DetailEvidence(blocks=[block, other],
                               by_node={"s2-1": [0], "s2-2": [0], "s3-1": [1]})
    targets = [
        (Node(id="s2-1", label="词面"), ()),
        (Node(id="s2-2", label="向量"), ()),
        (Node(id="s3-1", label="逐字"), ()),
    ]
    section = _detail_evidence_section(targets, evidence)
    assert section.count("词面检索擅长精确串。") == 1
    assert section.count("引用必须逐字回到原文。") == 1
    assert "id=s2-1 → 块 1" in section
    assert "id=s2-2 → 块 1" in section
    assert "id=s3-1 → 块 2" in section
    assert "[块 1｜第 2 章「混合检索」]" in section


def test_a_half_only_renders_its_own_blocks(monkeypatch):
    """两半各渲染自己节点用到的块：另一半的章不进这一半的 prompt。"""
    left = _DetailBlock(chapter_index=1, chapter_title="切块", source_id="s-rag",
                        start=0, end=5, quote="左块正文", score=1.0)
    right = _DetailBlock(chapter_index=2, chapter_title="检索", source_id="s-rag",
                         start=5, end=10, quote="右块正文", score=1.0)
    evidence = _DetailEvidence(blocks=[left, right],
                               by_node={"s1-1": [0], "s2-1": [1]})
    section = _detail_evidence_section([(Node(id="s1-1", label="甲"), ())], evidence)
    assert "左块正文" in section
    assert "右块正文" not in section
    assert "id=s2-1" not in section


def test_blocks_are_capped_by_score_not_by_document_order(monkeypatch):
    """超出字数上限时按**分数**留块——按文档序截断会系统性饿死后面几章。"""
    from app.agent.tools import _budget_blocks

    # 两块放得下、三块放不下：上限 16,000 时 size 取 6,400（2 × 6,400 ≤ 16,000 < 3 × 6,400）
    size = DETAIL_RETRIEVAL_MAX_CHARS // 2 - 1_600
    blocks = [
        _DetailBlock(chapter_index=1, chapter_title="第一章", source_id="s", start=0,
                     end=size, quote="甲" * size, score=0.1),
        _DetailBlock(chapter_index=2, chapter_title="第二章", source_id="s", start=size,
                     end=size * 2, quote="乙" * size, score=0.9),
        _DetailBlock(chapter_index=3, chapter_title="第三章", source_id="s", start=size * 2,
                     end=size * 3, quote="丙" * size, score=0.5),
    ]
    evidence = _DetailEvidence(blocks=blocks,
                               by_node={"a": [0], "b": [1], "c": [2]})
    _budget_blocks(evidence)
    kept = [block.chapter_index for block in evidence.blocks]
    assert sum(len(block.quote) for block in evidence.blocks) <= DETAIL_RETRIEVAL_MAX_CHARS
    # 最高分那两块留下，最低分那块被砍；留下的仍按文档序排
    assert kept == [2, 3]
    assert set(evidence.by_node) == {"b", "c"}
    assert evidence.by_node["b"] == [0] and evidence.by_node["c"] == [1]


# ---------------------------------------------------------------- 5. 降级


def test_a_failed_search_only_costs_that_one_node(monkeypatch):
    """一条检索抛错：那一个节点没有证据，其余节点照常，整段说明全部写回。"""
    monkeypatch.setenv("MINDMAP_DETAIL_RETRIEVAL", "1")
    content, chapters = _tree()
    mapping = _node_chapters(content.root, chapters)
    targets = [(node, path) for node, path in _iter_paths(content.root)
               if node.id in mapping]

    def flaky(sources, query, top_k):
        if query.endswith("要点一") and "切块" in query:
            raise RuntimeError("向量服务 500")
        return [EvidenceHit(source_id=sources[0].id, start=0, end=50,
                            quote=sources[0].text[:50], score=1.0)]

    monkeypatch.setattr(tools_module, "search_exact_blocks", flaky)
    evidence = asyncio.run(_detail_evidence(targets, mapping=mapping, source=_source()))
    assert evidence.seconds >= 0
    assert len(evidence.by_node) == len(targets) - 1
    assert "s1-1" not in evidence.by_node


def test_falling_back_to_the_old_sections_when_nothing_matched(monkeypatch):
    """一条都没命中：退回原来的 `sections` 并在账里写 `fallback`。

    "检索没成"与"材料里没有"是两件事：前者把**已有**的证据丢掉，等于把这一段退化成
    凭知识写，而产出上看不出区别。
    """
    monkeypatch.setenv("MINDMAP_DETAIL_RETRIEVAL", "1")
    sources: list[str] = []

    async def fake_chat(ctx, system, user, **kwargs):
        sources.append(system + user)
        return _answers_ids(system, user)

    content, chapters = _tree()
    monkeypatch.setattr(tools_module, "search_exact_blocks",
                        lambda *args, **kwargs: [])
    monkeypatch.setattr(tools_module, "_chat", fake_chat)
    filled, report = asyncio.run(_fill_details(
        content, ctx=_ctx(), topic=TOPIC, sections=["（整份材料的原文）"],
        external_rules="", diagnostics=[], chapters=chapters, source=_source()))
    assert report["evidence"]["fallback"] == "no_hits"
    assert report["evidence"]["blocks"] == 0
    assert report["filled"] == report["requested"]
    # 退回原来那条：整份材料仍在 prompt 里，且 system 用的是全塞/按支那份而不是新那份
    assert any("（整份材料的原文）" in text for text in sources)
    assert not any(DETAIL_ONLY_RETRIEVED_RULES in text for text in sources)


def test_switch_on_replaces_sections_and_uses_the_new_rules(monkeypatch):
    """命中之后：整支/整份那份**不再进 prompt**，system 换成按节点那份规则。"""
    monkeypatch.setenv("MINDMAP_DETAIL_RETRIEVAL", "1")
    prompts: list[str] = []

    async def fake_chat(ctx, system, user, **kwargs):
        prompts.append(system + user)
        return _answers_ids(system, user)

    content, chapters = _tree()
    monkeypatch.setattr(tools_module, "search_exact_blocks",
                        lambda sources, query, top_k: [EvidenceHit(
                            source_id=sources[0].id, start=0,
                            end=min(len(sources[0].text), 80),
                            quote=sources[0].text[:80], score=1.0)])
    monkeypatch.setattr(tools_module, "_chat", fake_chat)
    _filled, report = asyncio.run(_fill_details(
        content, ctx=_ctx(), topic=TOPIC, sections=["（整份材料的原文）"],
        external_rules="", diagnostics=[], chapters=chapters, source=_source()))
    assert report["evidence"]["fallback"] == ""
    assert report["evidence"]["blocks"] == 3
    assert prompts and all("（整份材料的原文）" not in text for text in prompts)
    assert all(DETAIL_ONLY_RETRIEVED_RULES in text for text in prompts)
    # system 仍以 `DETAIL_ONLY_PROMPT` 开头（分类靠前缀，`measure_section_slice` 用它）
    assert all(text.startswith(DETAIL_ONLY_PROMPT) for text in prompts)


# ---------------------------------------------------------------- 6. 检索入口


def test_search_exact_blocks_is_parent_granularity_without_rerank(monkeypatch):
    """`search_exact_blocks`：父块粒度、**不重排**、空 query 直接空。"""
    from app.knowledge import rerank as rerank_module

    def boom(*args, **kwargs):
        raise AssertionError("这条路不该重排（rerank.py 里点名了这类字面检索）")

    monkeypatch.setattr(rerank_module, "rerank", boom)
    source = _source()
    hits = search_exact_blocks([source], "切块决定检索单元", 2)
    assert hits and len(hits) <= 2
    assert all(hit.leaf_start is not None for hit in hits)
    assert all(source.text[hit.start:hit.end] == hit.quote for hit in hits)
    assert search_exact_blocks([source], "   ", 2) == []
    assert search_exact_blocks([source], "切块", 0) == []


def test_top_k_is_three_so_a_node_sees_a_few_paragraphs():
    assert DETAIL_RETRIEVAL_TOP_K == 3


@pytest.mark.parametrize("value", ["0", "off", "false", "no", ""])
def test_falsey_switch_values_keep_the_old_path(monkeypatch, value):
    monkeypatch.setenv("MINDMAP_DETAIL_RETRIEVAL", value)
    assert tools_module.detail_retrieval_enabled() is False


@pytest.mark.parametrize("value", ["1", "on", "true", "yes"])
def test_truthy_switch_values_enable_the_new_path(monkeypatch, value):
    monkeypatch.setenv("MINDMAP_DETAIL_RETRIEVAL", value)
    assert tools_module.detail_retrieval_enabled() is True
