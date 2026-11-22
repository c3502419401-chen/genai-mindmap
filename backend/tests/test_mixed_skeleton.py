"""混合材料的骨架：有章节的走章节、没章节的走图谱，合并成一套。

## 这个文件盯的是什么

缺陷的判据不是"这条路上有没有目录"，而是**每一份材料有没有位置**。实测（库里 29 份
真实材料，`scripts/measure_mixed_skeleton.py --probe`）：

    只有 markdown（4 份 / 66,536 字）        30 条章节，覆盖 0.91
    只有 paper（20 份 / 6,658 字）            0 条
    markdown + paper 混（24 份 / 73,194 字）  30 条章节，覆盖 0.83 ← 那 20 份一条都没有
    web + paper 混（22 份 / 16,968 字）       16 条标题，覆盖 0.56 ← 过不了闸，一条都不拼

"覆盖 0.83"看上去还行，因为**分母是字数**。所以这里的用例全部按**份**判：

  0. 现状两条（只调用既有函数，修法落地前后都成立）：整批二选一、闸的粒度错了；
  1. 分组：逐份判"有没有可用章节"，两组都非空才走混骨架；
  2. 合并：章节超上限时**每份至少留一条**（旧路径按整批 coverage 挑，小材料会整批掉光）；
  3. 图谱这一档**只看无章节那几份的 scope**（有章节那几份的实体进来只会与章节重复）；
  4. 选择按**材料覆盖**而不是度数：度数最高的概念只覆盖 1 份时，该让位给覆盖 2 份的；
  5. 忠实度：材料原文里找不到的概念不进骨架（抽取层会吐出 `service.py` 这类噪声）；
  6. 去重：章节与图谱说同一件事时留章节那一条（它有偏移、能回原文）；
  7. 合并规则：章节在前、图谱在后；图谱条数进 `sections_expected`（两级骨架的判据）；
  8. 两条生成路（整树 / 分片）**同一批材料给出同一个骨架来源**（`AGENTS.md` 的要求）；
  9. 图不可用时那几份仍有一句话（"没有分支指向它们"），不许变成另一种静默；
 10. 开关关掉时逐字节退回改动之前。

第 10 条是这一批用例的地基：`MINDMAP_MIXED_SKELETON=0` 必须让第 1–9 条一条都不生效。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.agent import tools as tools_module
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    DETAIL_ONLY_PROMPT,
    GRAPH_OUTLINE_ENV,
    GRAPH_SUPPLEMENT_HEADER,
    MIXED_SKELETON_ENV,
    OUTLINE_HEADER,
    OUTLINE_MAX_ITEMS,
    OUTLINE_MIN_COVERAGE,
    PLAN_PROMPT,
    SKELETON_GROUP_THRESHOLD,
    _graph_supplement_attempt,
    _merge_chapter_entries,
    _mixed_diagnostic,
    _mixed_outline,
    _mixed_plain_rules,
    _mixed_plan_rules,
    _outline_entries,
    _outline_split,
    _pick_supplement,
    _supplement_candidates,
    build_registry,
)
from app.contracts.source import Source, SourceKind
from app.knowledge import graph_store
from app.knowledge.service import heading_outline, heading_outline_by_source, outline_coverage

TOPIC = "碳中和与海绵城市"
BRANCHES = ("海绵城市", "碳中和", "雨污分流")

# 有章节的材料：三章、覆盖率接近 100%，第一档直接接管。三章的标题刻意就是后面
# 那几个概念名——"章节与图谱说同一件事"那条用例靠它。
#
# 长度（约 1,000 字）与句子开头也是刻意的：
#   - 它要占整批的六成以上，否则旧路径那一道**整批**覆盖率闸（60%）会把它一起挡掉，
#     "整批二选一"与"开关关掉时退回改动之前"那两条用例量的就不是同一件事了；
#   - 重复的句子不能以章节名开头（`碳中和是…`）：切块器的 `_HEADING_RE` 会把那种行
#     当标题，目录被切成一条条 100 字的碎片，覆盖率掉到 0.6 以下。
_CHAPTER_PARA = (
    "海绵城市通过渗、滞、蓄、净、用、排六字方针控制雨水径流，示范区的年径流总量"
    "控制率按规划指标确定，透水铺装与下凹式绿地是最常见的两件工程措施。"
    "监测口径分两套：管网流量计看瞬时峰值，调蓄池水位看削峰效果。"
)
CHAPTERED = (
    "# 海绵城市与碳中和\n\n"
    "## 海绵城市\n\n" + _CHAPTER_PARA * 8 + "\n\n"
    "## 碳中和\n\n"
    "排放与吸收相抵之后的净零才是碳中和，排放由增转降的拐点是碳达峰。"
    "两者的时间顺序不能颠倒，达峰越晚，中和的坡度越陡。核算口径分三个范围，"
    "范围一与范围二有现成的排放因子，范围三要靠供应链自报。\n\n"
    "## 雨污分流\n\n"
    "把雨水与污水分成两套管网就是雨污分流，难点在既有管线的交错与错接，"
    "施工期还要保证污水不溢流。分流之后污水厂的进水浓度会上升，"
    "碳源不足时还要补加外碳源。\n"
)

# 无章节的材料：纯摘要、没有任何标题行（库里那 20 份 openalex / crossref 的形状）。
PLAIN_ONE = (
    "本文以武汉市汉阳四新示范区为例研究海绵城市建设试点中的海绵策略。"
    "示范区采用透水铺装与下凹式绿地，把年径流总量控制率作为主要指标。"
    "研究结论是海绵城市必须与雨污分流同步推进，否则调蓄能力会被合流制抵消。"
) * 3

PLAIN_TWO = (
    "高速铁路的节能改造是交通领域实现碳中和的重要路径。"
    "本文测算牵引能耗与再生制动回馈电量，指出碳中和与碳达峰的双目标下"
    "铁路电气化率的提升空间最大。结论是节能调度比更换车体更便宜。"
) * 3


def _source(text: str, *, source_id: str, kind: SourceKind = SourceKind.paper,
            filename: str = "material.md") -> Source:
    return Source(id=source_id, kind=kind, status="parsed", filename=filename,
                  text=text)


def _entity(name: str, degree: int = 3, kind: str | None = "概念"):
    return graph_store.GraphEntity(name=name, type=kind, degree=degree)


def _titles(entries) -> list[str]:
    return [entry.title for entry in entries]


def _fake_graph(monkeypatch, *, entities, count: int | None = None,
                configured: bool = True, available: bool = True,
                cooldown: float = 0.0) -> dict:
    """图存储替身。默认把 `MINDMAP_GRAPH_OUTLINE` 打开（conftest 把它置 0）。"""
    monkeypatch.setenv(GRAPH_OUTLINE_ENV, "1")
    calls: dict = {"scope": None, "count": 0}
    listed = list(entities)
    monkeypatch.setattr(graph_store, "configured", lambda: configured)
    monkeypatch.setattr(graph_store, "available", lambda: available)
    monkeypatch.setattr(graph_store, "cooldown_remaining", lambda: cooldown)
    monkeypatch.setattr(graph_store, "endpoint",
                        lambda: ("bolt://127.0.0.1:7687", "neo4j"))

    def fake_count(scope=None):
        calls["count"] += 1
        return len(listed) if count is None else count

    def fake_subgraph(*, entities=60, links=120, scope=None):
        calls["scope"] = list(scope or [])
        return listed, []

    monkeypatch.setattr(graph_store, "scoped_entity_count", fake_count)
    monkeypatch.setattr(graph_store, "subgraph", fake_subgraph)
    return calls


def _concept_lines(block: str) -> list[str]:
    """补充块里那几条概念（表头之后、连续编号的那几行）。"""
    lines: list[str] = []
    for line in block.splitlines()[1:]:
        if line[:1].isdigit() and "." in line[:4]:
            lines.append(line)
        elif lines:
            break
    return lines


# ---------------------------------------------------------------- 0. 现状（旧口径没变）


def test_the_batch_outline_gives_the_plain_material_no_place():
    """整批二选一：混进来那一份**一个字都没贡献**，结果与"只有那一份"逐条相同。

    这就是缺陷的判据——不是"分得不好"，是那几份根本不在骨架里。它不会被修法改掉
    （这一层的语义不变），所以它同时是"修法没有偷偷改掉旧口径"的证据。
    """
    titled = _source(CHAPTERED, source_id="s-titled", filename="方案.md",
                     kind=SourceKind.document)
    plain = _source(PLAIN_ONE, source_id="s-plain", filename="摘要.md")

    alone = _outline_entries([titled])
    mixed = _outline_entries([titled, plain])

    assert _titles(alone) == ["海绵城市", "碳中和", "雨污分流"]
    assert _titles(mixed) == _titles(alone)
    # 那一份自己一条目录都出不来（不是"标题不好"，是没有标题）
    assert heading_outline([plain]) == []
    # 整批覆盖率只按字数摊薄：混进那 297 字之后，这份目录的覆盖从 0.99 掉到 0.76
    assert outline_coverage([titled], alone) > 0.9
    assert outline_coverage([titled, plain], mixed) < 0.8


def test_the_coverage_gate_drops_titles_that_do_exist():
    """闸的粒度错了：标题真实存在（16 条的那种形状），只是被长材料摊薄。

    `web + paper 混`那一行就是它——16 条标题、整批覆盖 0.56 < 0.60，于是连那 16 条
    也一起不拼。判据"标题总共管住了多少字"在这里把**真实存在的结构**判成了"没有结构"。
    """
    titled = _source(CHAPTERED, source_id="s-titled", filename="方案.md",
                     kind=SourceKind.document)
    long_plain = _source("这一大段是没有章节的流水账。" * 200, source_id="s-long")

    entries = heading_outline([titled, long_plain])
    assert len(entries) == 3                      # 标题是有的
    assert outline_coverage([titled, long_plain], entries) < OUTLINE_MIN_COVERAGE
    assert _outline_entries([titled, long_plain]) == []   # 但一条都不拼
    # 单独看那一份，它完全够格——判据错在粒度，不在材料
    assert len(_outline_entries([titled])) == 3


# ---------------------------------------------------------------- 1. 分组


def test_a_mixed_batch_is_split_per_material():
    """逐份判：有章节的一边、没章节的一边；两组都非空才是"混合"。"""
    titled = _source(CHAPTERED, source_id="s-titled", filename="方案.md")
    plain = _source(PLAIN_ONE, source_id="s-plain", filename="摘要.md")

    split = _outline_split([titled, plain])
    assert [source.id for source in split.titled] == ["s-titled"]
    assert [source.id for source in split.plain] == ["s-plain"]
    assert split.mixed is True
    # 章节这一组自己出目录；无章节那组一条都不贡献（这正是要补的那一半）
    assert _titles(split.chapters) == ["海绵城市", "碳中和", "雨污分流"]


def test_a_single_group_is_not_mixed():
    """全有章节 / 全没有章节时不算混合——那两种批走原来那一档，一个字节都不改。"""
    titled = _source(CHAPTERED, source_id="s-titled")
    plain = _source(PLAIN_ONE, source_id="s-plain")

    assert _outline_split([titled]).mixed is False
    assert _outline_split([plain]).mixed is False
    # 调用方据此退回原来的骨架来源（第三档 / 章节）
    assert _mixed_outline([titled]) is None
    assert _mixed_outline([plain]) is None


def test_heading_outline_by_source_does_not_trim_across_materials():
    """`heading_outline` 是**整批**裁剪（一份可能被另一份挤光），逐份那份不裁。

    这是整个改动的数据入口：没有它，判据只能是"整批有没有目录"。
    """
    big = _source("\n".join(f"## 第{index}章\n\n正文。\n" for index in range(20)),
                  source_id="big")
    small = _source("\n".join(f"## 小{index}\n\n正文。\n" for index in range(20)),
                    source_id="small")

    groups = heading_outline_by_source([big, small])
    assert [len(group) for group in groups] == [20, 20]
    assert len(heading_outline([big, small], max_items=30)) == 30


def test_merge_keeps_one_entry_per_material_even_over_the_cap():
    """合并超上限时**每份至少留一条**：小材料不该被大材料整批挤掉。

    旧路径按 coverage 挑前 30 条，粒度是整批——一份 2 章的小材料在 30 章的大材料
    面前可以一条都不剩。这里把两份拼到 32 条，逼出裁剪。
    """
    big = _source("\n".join(f"## 大{index}\n\n" + "字" * (index + 2) + "\n"
                            for index in range(30)), source_id="big")
    small = _source("## 小甲\n\n短。\n\n## 小乙\n\n短。\n", source_id="small")
    merged = _merge_chapter_entries([
        (big, heading_outline([big])),
        (small, heading_outline([small])),
    ])

    assert len(merged) == OUTLINE_MAX_ITEMS
    assert {entry.title for entry in merged} & {"小甲", "小乙"}
    # 交出去按材料顺序：大材料那些在前，小材料那一条在后
    assert merged[-1].title.startswith("小")


# ---------------------------------------------------------------- 2. 图谱补充档


def test_supplement_only_asks_for_the_plain_materials(monkeypatch):
    """scope 只给无章节那几份：有章节那几份的实体进来，补出来的分支只会与章节重复。"""
    calls = _fake_graph(monkeypatch, entities=[_entity("下凹式绿地", 7)])
    titled = _source(CHAPTERED, source_id="s-titled")
    plain = _source(PLAIN_ONE, source_id="s-plain")
    supplement = _graph_supplement_attempt(
        [plain], chapter_titles=[entry.title for entry in heading_outline([titled])])

    assert calls["scope"] == ["s-plain"]
    assert supplement.block.startswith(GRAPH_SUPPLEMENT_HEADER)
    assert "下凹式绿地" in supplement.block
    assert (supplement.items, supplement.covered, supplement.plain) == (1, 1, 1)


def test_supplement_drops_concepts_the_material_never_says(monkeypatch):
    """忠实度过滤：原文里找不到的名字不进骨架（抽取层会吐出 `service.py` 这类噪声）。"""
    _fake_graph(monkeypatch, entities=[
        _entity("下凹式绿地", 9),
        _entity("service.py", 8),
        _entity("王小明", 7),
    ])
    plain = _source(PLAIN_ONE, source_id="s-plain")
    supplement = _graph_supplement_attempt([plain])

    assert "下凹式绿地" in supplement.block
    assert "service.py" not in supplement.block
    assert "王小明" not in supplement.block
    assert supplement.items == 1


def test_supplement_picks_by_material_coverage_not_by_degree():
    """选择判据是**材料覆盖**：度数低但多覆盖一份的概念，要顶掉度数高只覆盖一份的。

    这是本改动与 `subgraph`（按度数排序）之间唯一有意为之的偏差：判据是"有没有材料
    被指到"，不是"概念多热"。
    """
    first = _source("海绵城市与管网调蓄的关系是本文的重点。" * 12, source_id="s-a")
    second = _source("双碳目标下的铁路电气化率测算。" * 12, source_id="s-b")
    third = _source("双碳目标的核算口径与时间表。" * 12, source_id="s-c")
    rows = _supplement_candidates(
        [_entity("海绵城市", 10), _entity("双碳目标", 2)], [first, second, third])

    picked, uncovered = _pick_supplement(rows, ["s-a", "s-b", "s-c"], limit=1)

    assert [entity.name for entity, _hits in picked] == ["双碳目标"]
    # 只取一条时另一份没被覆盖——"残余要显式报出去"的那一半
    assert uncovered == {"s-a"}


def test_supplement_stops_when_every_material_is_covered(monkeypatch):
    """覆盖满了就停：同一件事的几种说法不该各占一条分支。"""
    _fake_graph(monkeypatch, entities=[
        _entity("碳中和", 9),
        _entity("碳达峰", 8),          # 与碳中和出现在同一份里
        _entity("下凹式绿地", 3),
    ])
    plain_one = _source(PLAIN_ONE, source_id="s-one")
    plain_two = _source(PLAIN_TWO, source_id="s-two")
    supplement = _graph_supplement_attempt([plain_one, plain_two])

    assert len(_concept_lines(supplement.block)) == 2   # 两份材料两条概念，不是三条
    assert supplement.covered == 2


def test_the_same_thing_from_two_columns_keeps_the_chapter():
    """章节与图谱说同一件事时只留章节那一条（它有偏移、能回到原文）。"""
    titled = _source(CHAPTERED, source_id="s-titled")
    plain = _source(PLAIN_ONE, source_id="s-plain")
    split = _outline_split([titled, plain])

    rows = _supplement_candidates(
        [_entity("碳中和", 9), _entity("下凹式绿地", 8)], split.plain)
    blocked = [entry.title for entry in split.chapters]
    kept = [row for row in rows
            if not any(tools_module._concept_key(row[0].name) in tools_module._concept_key(t)
                       or tools_module._concept_key(t) in tools_module._concept_key(row[0].name)
                       for t in blocked)]

    assert [entity.name for entity, _hits in kept] == ["下凹式绿地"]


# ---------------------------------------------------------------- 3. 接线：提示词


def _request(sources: list[Source]) -> AgentRunRequest:
    return AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default", sources=sources,
        budget=RunBudget(max_model_calls=20, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=1),
    )


def _ctx(sources: list[Source]) -> RunContext:
    return RunContext(request=_request(sources),
                      sources_by_id={source.id: source for source in sources})


def _tree_payload() -> dict:
    children = [
        {"id": f"b{index}", "label": label,
         "children": [{"id": f"b{index}-1", "label": f"{label}·一",
                       "detail": "", "source_refs": []}]}
        for index, label in enumerate(BRANCHES)
    ]
    return {"title": TOPIC,
            "root": {"id": "root", "label": TOPIC, "detail": "中心主题",
                     "children": children}}


def _deep_chat(seen: list[tuple[str, str]]):
    """分片那条路的模型替身：骨架回三条分支，其余回两个要点。"""
    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(BRANCHES)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {}
        return {"children": [{"label": "要点一", "expandable": False},
                            {"label": "要点二", "expandable": False}]}
    return chat


def _walk(node: dict):
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


def _tree_chat(seen: list[tuple[str, str]]):
    """整树那条路的模型替身：骨架回三条分支，结构回一棵合法树，说明按 id 给。"""
    tree = _tree_payload()

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(BRANCHES)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {node["id"]: "这句说明写到八十个字以上，讲清它是什么、怎么做、"
                                "有什么条件与数字，这样第三段的产出才符合长度目标。"
                    for node in _walk(tree["root"]) if node.get("id")}
        return tree
    return chat


def _run(deep: bool, sources: list[Source]) -> tuple[object, list[tuple[str, str]]]:
    """跑一次生成：`deep=True` 走分片，`False` 走整树。返回 (结果, 见过的 prompt)。"""
    ctx = _ctx(sources)
    seen: list[tuple[str, str]] = []
    if deep:
        registry = build_registry("create_deep", has_sources=True)
        call = ToolCall(call_id="c-deep", name="mindmap_generate_deep",
                        arguments={"topic": TOPIC, "branches": 3,
                                   "points_per_branch": 2})
        chat = _deep_chat(seen)
    else:
        registry = build_registry("create", has_sources=True, has_library=False)
        call = ToolCall(call_id="c-gen", name="mindmap_generate",
                        arguments={"topic": TOPIC, "branches": 3})
        chat = _tree_chat(seen)
    with patch("app.agent.tools.chat_async", new=chat):
        result = asyncio.run(registry.execute(call, ctx))
    return result, seen


def _plan(seen: list[tuple[str, str]]) -> tuple[str, str]:
    return next((system, user) for system, user in seen
                if system.startswith(PLAN_PROMPT))


def _mixed_sources() -> list[Source]:
    return [
        _source(CHAPTERED, source_id="s-titled", filename="方案.md",
                kind=SourceKind.document),
        _source(PLAIN_ONE, source_id="s-one", filename="汉阳四新.md"),
        _source(PLAIN_TWO, source_id="s-two", filename="高铁节能.md"),
    ]


def test_the_prompt_puts_chapters_before_the_graph(monkeypatch):
    """顺序：章节在前（作者划的结构）、图谱在后（这几份没有结构，别忘了它们）。"""
    _fake_graph(monkeypatch, entities=[_entity("下凹式绿地", 9)])
    result, seen = _run(deep=True, sources=_mixed_sources())

    assert result.ok, result.content
    system, user = _plan(seen)
    assert OUTLINE_HEADER in user and GRAPH_SUPPLEMENT_HEADER in user
    assert user.index(OUTLINE_HEADER) < user.index(GRAPH_SUPPLEMENT_HEADER)
    # 两份清单都在条数要求之前（顺序即权重）
    assert user.index(GRAPH_SUPPLEMENT_HEADER) < user.index("最多 3 条")
    assert _mixed_plan_rules(1) in system


def test_both_generation_paths_compose_the_same_skeleton(monkeypatch):
    """整树与分片对**同一批材料**必须给出同一个骨架来源。

    只改一条路会得到一个"两个入口不一致"的分叉，而那种分叉只在把分片重新注册回
    `create` 的那天才暴露（`AGENTS.md` 关于两条生成路的那条要求）。
    """
    _fake_graph(monkeypatch, entities=[_entity("下凹式绿地", 9)])
    sources = _mixed_sources()
    deep_result, deep_seen = _run(deep=True, sources=sources)
    tree_result, tree_seen = _run(deep=False, sources=sources)

    assert deep_result.ok, deep_result.content
    assert tree_result.ok, tree_result.content
    deep_system, deep_user = _plan(deep_seen)
    tree_system, tree_user = _plan(tree_seen)
    # user 逐字节相同（章节块 + 图谱补充块 + 清单 + 条数要求）
    assert deep_user == tree_user
    # system 只差那条"思考方式"（分片那条路把它拼在 system 尾部，不是本次改动引入的），
    # 骨架规则本身必须一致。
    assert _mixed_plan_rules(1) in deep_system and _mixed_plan_rules(1) in tree_system
    assert deep_system.startswith(tree_system.rstrip("\n"))
    codes = {item["code"] for item in tree_result.data["diagnostics"]}
    assert "GRAPH_OUTLINE_USED" in codes


def test_without_the_graph_the_plain_materials_still_get_a_rule(monkeypatch):
    """图不可用（没配密码）：那几份仍要有一句话，不许静默消失。

    降级本身是对的（不为一条注定失败的请求付一次连接超时），但"这几份没有分支指向
    它们"必须能从 diagnostics 里看出来——否则这个改动就是另一种静默。
    """
    _fake_graph(monkeypatch, entities=[], configured=False)
    result, seen = _run(deep=True, sources=_mixed_sources())

    assert result.ok, result.content
    system, user = _plan(seen)
    assert GRAPH_SUPPLEMENT_HEADER not in user
    assert _mixed_plain_rules(2) in system
    messages = [item for item in result.data["diagnostics"]
                if item["code"] == "GRAPH_OUTLINE_UNAVAILABLE"]
    assert messages and "2 份没有章节的材料没有分支指向它们" in messages[0]["message"]


def test_the_diagnostic_says_how_many_materials_got_a_concept(monkeypatch):
    """用了图谱那几条时要说清覆盖了几份：数字是这一档唯一的可检查凭证。"""
    _fake_graph(monkeypatch, entities=[
        _entity("下凹式绿地", 9), _entity("碳达峰", 8)])
    split = _outline_split(_mixed_sources())
    mixed = tools_module._MixedOutline(
        chapters=list(split.chapters),
        supplement=_graph_supplement_attempt(
            split.plain, chapter_titles=[entry.title for entry in split.chapters]),
        titled=len(split.titled), plain=len(split.plain))

    diagnostic = _mixed_diagnostic(mixed)
    assert diagnostic.code == "GRAPH_OUTLINE_USED"
    assert f"覆盖其中 {mixed.supplement.covered}/{mixed.plain} 份" in diagnostic.message


def test_the_plan_rule_asks_for_a_reachable_floor():
    """规则要**可满足**：8 个概念、请求 5–8 条分支时，"每条概念都要有分支"做不到。

    实测（`data/ab-mixed-small.json`）那句话会被整段无视；改成"至少 3 条落在这些概念
    上"才是这一批材料撑得住的量。
    """
    assert "至少 3 条" in _mixed_plan_rules(8)
    assert "至少 1 条" in _mixed_plan_rules(1)


def test_grouping_counts_the_graph_branches_too(monkeypatch):
    """总量：图谱那几条要进 `sections_expected`——它们真的会变成分支。

    3 章 + 8 概念 = 11 > 阈值（10）→ 请求两级骨架；只补出 1 条时是 4 条，保持扁平。
    """
    concepts = ["下凹式绿地", "透水铺装", "合流制", "调蓄能力",
                "牵引能耗", "再生制动", "电气化率", "节能调度"]
    plains = [
        _source(f"本文讨论{name}的工程做法与实测数字。" * 12,
                source_id=f"s-{index}", filename=f"{name}.md")
        for index, name in enumerate(concepts)
    ]
    sources = [
        _source(CHAPTERED, source_id="s-titled", filename="方案.md",
                kind=SourceKind.document),
        *plains,
    ]
    # 8 条概念各覆盖一份，加上 3 章 → 11 > 10
    assert len(plains) + 3 > SKELETON_GROUP_THRESHOLD
    _fake_graph(monkeypatch, entities=[
        _entity(name, 8 - index) for index, name in enumerate(concepts)])
    _result, seen = _run(deep=False, sources=sources)
    system, user = _plan(seen)
    assert len(_concept_lines(user.split(GRAPH_SUPPLEMENT_HEADER, 1)[1])) == 8
    assert "这一次请输出两级骨架" in system

    _fake_graph(monkeypatch, entities=[_entity("下凹式绿地", 9)])
    _result, seen = _run(deep=False, sources=sources)
    system, user = _plan(seen)
    assert len(_concept_lines(user.split(GRAPH_SUPPLEMENT_HEADER, 1)[1])) == 1
    assert "这一次请输出两级骨架" not in system


def test_switch_off_restores_the_previous_prompt(monkeypatch):
    """开关关掉 = 改动之前：只有章节、没有图谱块、没有混骨架规则。"""
    monkeypatch.setenv(MIXED_SKELETON_ENV, "0")
    _fake_graph(monkeypatch, entities=[_entity("下凹式绿地", 9)])
    sources = _mixed_sources()

    assert _mixed_outline(sources) is None
    result, seen = _run(deep=True, sources=sources)
    assert result.ok, result.content
    system, user = _plan(seen)
    assert OUTLINE_HEADER in user
    assert GRAPH_SUPPLEMENT_HEADER not in user
    assert "分成两类" not in system


def test_switch_is_on_by_default(monkeypatch):
    """默认开：它补的是"一部分材料被静默丢掉"，不是可选能力。"""
    monkeypatch.delenv(MIXED_SKELETON_ENV, raising=False)
    assert tools_module.mixed_skeleton_enabled() is True
    monkeypatch.setenv(MIXED_SKELETON_ENV, "off")
    assert tools_module.mixed_skeleton_enabled() is False
