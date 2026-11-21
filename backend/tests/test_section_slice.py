"""按章切（`MINDMAP_SECTION_SLICE=1`）：**每支只给它自己那一章的原文**。

这条路与既有三条路的差别只有一处，这个文件盯的就是那一处：

  1. **切章是确定的**：`_outline_lines` 只收一个级别，所以 `[start, start+coverage)`
     切出来的章互不相交；第 1 章吃掉文件导语（否则全文永远差那 1%）。多份材料 /
     单章材料 / 覆盖率不足一律返回空，调用方据此**静默退回现状**；
  2. **每章一次调用，输入只有这一章**：兄弟支的原文不在它的 prompt 里——这是
     "写不出跨支重复"的机制前提，所以用例直接断言 prompt 里不含别章的正文；
  3. **组装是确定的**：id 由代码给（模型写 id 会撞号）、节点预算平分（自由写然后裁
     会让尾部整支消失）、深度夹在 `SECTION_SLICE_DEPTH`；
  4. **引用落在本章**：`start/end` 由服务端在本章范围内数出来；跨章的 quote 直接丢。
     这条是"每章覆盖率"可测的前提；
  5. **开关关掉逐字节不变**：`data` 没有 `section_slice` 键、没有新诊断、骨架的
     ask 与结构那一次调用与原来完全一样（`test_edges.TestTreeIsByteIdentical` 是
     更严的那一道，这里补一条更快的）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    DETAIL_ONLY_PROMPT,
    INITIAL_MAX_NODES,
    PLAN_PROMPT,
    SECTION_SLICE_DEPTH,
    SECTION_SLICE_MAX_CHARS,
    SECTION_SLICE_PROMPT,
    _SkeletonBranch,
    _SkeletonPlan,
    _align_chapters,
    _chapter_slices,
    _locate_in_chapter,
    _slice_budget,
    _slice_nodes,
    _slice_refs,
    build_registry,
)
from app.contracts.source import Source, SourceKind

TOPIC = "检索增强生成"

# 12 章，每章正文**互不相同**：要量"跨支重复是不是被机制消掉了"，材料本身就不能
# 自带重复（`measure_two_tier.py` 的注释里踩过这个坑：材料自带重复时量出来 58）。
CHAPTERS: dict[str, str] = {
    "背景与动机": "大模型的知识停在训练截止那天，检索把外部资料带进上下文。"
                  "这条路的收益取决于召回质量，而不是提示词写得多漂亮。",
    "切块与粒度": "切块决定检索单元。定长切块便宜但会切断句子，递归切块按结构降级。"
                  "块太大时命中里混着无关内容，块太小时一句话被拆成两半。",
    "向量与索引": "向量把语义压成可比的距离，索引让近邻查找不必扫全库。"
                  "维度不是越高越好，召回率与延迟要一起看。",
    "混合检索": "词面检索擅长精确串，向量检索擅长近义改写，两者合并要定权重。"
                "融合之前先各自归一化，否则分数量纲差一个数量级。",
    "重排序": "粗排取回二十条，精排用交叉编码器逐对打分。"
              "精排贵在每次请求都要跑一遍模型，所以只对前若干条做。",
    "上下文拼装": "命中要按来源分组、带出处地拼进 prompt，否则模型会把两段话缝在一起。"
                  "位置也影响注意力，最重要的片段不要埋在中间。",
    "引用与校验": "模型写的引用必须逐字回到原文，定位不唯一时标待核而不是猜一个坐标。"
                  "没有出处的句子与编造在界面上长得一样，所以要能点回原文。",
    "评估口径": "覆盖率、忠实度、答案正确率是三个不同的数，混着报会掩盖真正的短板。"
                "人工评估成本高，抽样要按分支分层。",
    "成本与延迟": "输入 token 是这条路的主要成本，全文进 prompt 比按支检索贵一个数量级。"
                  "缓存命中与并发上限决定墙钟。",
    "失败模式": "召回为空、召回跑偏、引用编造、跨支重复是四种不同的失败。"
                "前两种要改检索，后两种要改生成的结构。",
    "工程落地": "离线建库、在线查询、增量更新三条链路要分开，重建索引不能阻塞查询。"
                "版本要能回滚，否则一次坏数据会污染全部结果。",
    "未解问题": "长文档的结构化检索还没有统一做法，多跳问答的中间步骤难以验证。"
                "评估集与真实流量之间的分布差是长期问题。",
}
LEAD = "本文记录检索增强生成在工程上的十二个决定，按从材料到答案的顺序排列。"


def _material() -> Source:
    body = [f"# 检索增强生成手记\n\n{LEAD}\n"]
    for index, (title, text) in enumerate(CHAPTERS.items(), start=1):
        # 每章正文**互不相同**，`###` 子节也不复读正文：材料自带重复时，"跨支重复"
        # 这个指标量到的就是材料本身，不是生成机制（`measure_two_tier.py` 踩过）。
        body.append(
            f"## {index} {title}\n\n{text}\n\n"
            f"### {title}的取舍\n\n做{title}这件事时，先定口径再谈方案。\n")
    return Source(id="s-rag", kind=SourceKind.markdown, status="parsed",
                  filename="rag.md", text="\n".join(body))


def _request(sources: list[Source], *, calls: int = 40) -> AgentRunRequest:
    return AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default", sources=sources,
        budget=RunBudget(max_model_calls=calls, max_tool_calls=8, max_seconds=120.0,
                         max_output_tokens=8192, max_repairs=1),
    )


def _ctx(sources: list[Source], *, calls: int = 40) -> RunContext:
    return RunContext(request=_request(sources, calls=calls),
                     sources_by_id={source.id: source for source in sources})


def _codes(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


def _walk(node: dict):
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


DETAIL_TEXT = (
    "这一条说明写到八十个字以上，讲清它是什么、怎么做、有什么条件与数字，"
    "这样第三段的产出才符合长度目标。"
)


def _skeleton(count: int = len(CHAPTERS)) -> dict:
    return {"branches": [{"label": f"{index} {title}"}
                         for index, title in enumerate(CHAPTERS, start=1)][:count]}


def _chat(*, skeleton: dict | None = None, subtree: dict | None = None,
          seen: list[tuple[str, str]] | None = None, detail_systems: list[str] | None = None):
    """模型替身：骨架 / **每章一次的子树** / 第三段，按 system 前缀分流。"""
    skeleton = _skeleton() if skeleton is None else skeleton

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if seen is not None:
            seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return skeleton
        if system.startswith(SECTION_SLICE_PROMPT):
            return subtree if subtree is not None else {
                "children": [
                    {"label": "要点甲", "children": [{"label": "细节一"}]},
                    {"label": "要点乙"},
                ]
            }
        if system.startswith(DETAIL_ONLY_PROMPT):
            if detail_systems is not None:
                detail_systems.append(system)
            # 第三段按 id 对号：这里的 id 只有运行时才知道，所以按 user 里出现的
            # `- id: …` 逐条回（`_detail_ask` 的清单就是那个形状）。
            ids = [line.split(":", 1)[0].strip("- ").strip()
                   for line in user.splitlines() if line.strip().startswith("- ")]
            return {node_id: DETAIL_TEXT for node_id in ids if node_id}
        # 整树那条路的结构响应：照骨架写成 root → 分支 → 要点（有效的树）
        return {"title": TOPIC, "root": {"id": "root", "label": TOPIC, "children": [
            {"id": f"b{index}", "label": branch["label"],
             "children": [{"id": f"p{index}", "label": f"{branch['label']}要点"}]}
            for index, branch in enumerate(skeleton.get("branches") or [])
        ]}}
    return chat


def _run(chat, *, channels: bool = False, **kwargs):
    source = kwargs.pop("source", None) or _material()
    ctx = _ctx([source] if source is not None else [], **kwargs)
    registry = build_registry("create", has_sources=True, has_library=False)
    with patch("app.agent.tools.chat_async", new=chat):
        return asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate",
                     arguments={"topic": TOPIC, "branches": 5}), ctx))


# ---------------------------------------------------------------- 1. 切章


def test_chapters_cover_the_whole_material_including_the_lead_in():
    """12 章、互不相交、**覆盖 100%**：导语并进第 1 章。

    导语（`LEAD`）在第一个标题之前，`coverage` 从标题算起——不并进第 1 章的话，
    全文永远差这一小段，而这段常常写着"本文的结论是…"。
    """
    source = _material()
    chapters = _chapter_slices([source])
    assert len(chapters) == len(CHAPTERS) == 12
    assert sum(chapter.chars for chapter in chapters) == len(source.text)
    assert chapters[0].start == 0
    assert LEAD in chapters[0].prompt_text
    # 相邻两章首尾相接，没有重叠也没有空隙
    for left, right in zip(chapters, chapters[1:]):
        assert left.end == right.start
    assert chapters[-1].end == len(source.text)


def test_chapters_refuse_every_shape_that_cannot_be_sliced():
    """多份材料 / 一章 / 覆盖率不足都返回空（调用方据此退回现状，不报错）。"""
    one = _material()
    assert _chapter_slices([one, one.model_copy(update={"id": "s-other"})]) == []
    single = Source(id="s-one", kind=SourceKind.markdown, status="parsed",
                    filename="one.md", text="## 只有一章\n\n正文。")
    assert _chapter_slices([single]) == []
    # 目录覆盖率不足 60%：正文在前、标题挤在文末，Σcoverage 只占全文一小截
    sparse = Source(id="s-sparse", kind=SourceKind.markdown, status="parsed",
                    filename="sparse.md",
                    text="没有标题的正文。" * 200 + "\n## 甲\n\n短。\n\n## 乙\n\n短。\n")
    assert _chapter_slices([sparse]) == []


def test_outline_levels_are_one_level_so_chapters_do_not_overlap():
    """这条钉的是**用户提的那个隐患**：如果一级二级都收，同一段原文会被两支拿到。

    `_outline_lines` 取"井号最少且至少 `OUTLINE_MIN_ITEMS` 条"的那一级，所以
    `#`×1 + `##`×12 + `###`×12 里只有 `##` 被收——`###` 落在它所属那一章的区间
    **内部**，不会被单独切成一支。
    """
    source = _material()
    chapters = _chapter_slices([source])
    assert any("###" in chapter.prompt_text for chapter in chapters), \
        "每章应当含它自己的 ### 子节"
    starts = [chapter.start for chapter in chapters]
    assert starts == sorted(set(starts))


def test_chapter_prompt_text_is_truncated_at_the_documented_limit():
    """超过上限的章只给前缀，并且如实告诉模型"没给到的不要补"。"""
    long_text = "## 甲\n\n" + "材料。" * (SECTION_SLICE_MAX_CHARS // 2) + \
                "\n\n## 乙\n\n" + "正文。" * 10
    source = Source(id="s-long", kind=SourceKind.markdown, status="parsed",
                    filename="long.md", text=long_text)
    chapters = _chapter_slices([source])
    assert len(chapters) == 2
    assert chapters[0].truncated is True
    assert len(chapters[0].prompt_text) == SECTION_SLICE_MAX_CHARS
    assert chapters[1].truncated is False


def test_per_branch_budget_splits_the_initial_limit():
    """平分：80 - 1 根 - 12 支 = 67，67 // 12 = 5 个后代/支（预算 6 含分支自己）。"""
    assert _slice_budget(12) == 5
    assert _slice_budget(1) == INITIAL_MAX_NODES - 2     # 1 根 + 1 支之后全给这一支
    # 章多到平分不够时守住下限，交给 `_clip_tree` 收尾
    assert _slice_budget(40) == 3


# ---------------------------------------------------------------- 2. 组装


def test_slice_nodes_gives_ids_and_enforces_budget_and_depth():
    """id 由代码给（模型写 id 会撞号）、预算按前序丢、深度夹在 `SECTION_SLICE_DEPTH`。"""
    raw = {"children": [
        {"label": "甲", "detail": "不该被收下",
         "children": [{"label": "甲一", "children": [{"label": "甲一一"}]}]},
        {"label": "乙"}, {"label": "丙"}, {"label": "丁"}, {"label": "戊"},
    ]}
    nodes = _slice_nodes(raw, prefix="s3", budget=4, depth=2)
    # 预算 4 = 分支自己 + 3 个后代；甲的**两个**后代先吃掉份额，顶层只剩甲与乙
    assert [node["id"] for node in nodes] == ["s3-1", "s3-2"]
    assert "detail" not in nodes[0], "这一轮不写 detail，模型写了也丢掉"
    assert nodes[0]["children"][0]["id"] == "s3-1-1"
    # 第 3 层（甲一一）超出 depth=2：不出现
    assert nodes[0]["children"][0]["children"] == []


def test_slice_nodes_accepts_all_three_shapes():
    """`{"children":…}` / `{"points":…}` / 裸数组都收：形状不对就整支判废太贵。"""
    assert _slice_nodes({"points": ["甲"]}, prefix="a", budget=5, depth=2)[0]["label"] == "甲"
    assert _slice_nodes(["乙"], prefix="a", budget=5, depth=2)[0]["label"] == "乙"
    assert _slice_nodes("不是结构", prefix="a", budget=5, depth=2) == []


def test_align_chapters_matches_by_title_then_by_position():
    """名字对上就按名字；对不上按序号；缺的章补出来（不能让某一章没有支）。"""
    chapters = _chapter_slices([_material()])
    # 逆序给：名字能对上，就该按名字对，而不是按位置错配
    reversed_plan = _SkeletonPlan(flat=tuple(
        _SkeletonBranch(chapter.title) for chapter in reversed(chapters)))
    aligned = _align_chapters(reversed_plan, chapters)
    assert [branch.label for _, branch in aligned] == [chapter.title for chapter in chapters]
    # 只给 3 支：后 9 章补成 None，但仍然各有一个位置
    partial = _SkeletonPlan(flat=tuple(
        _SkeletonBranch(chapter.title) for chapter in chapters[:3]))
    aligned = _align_chapters(partial, chapters)
    assert len(aligned) == len(chapters)
    assert [branch is None for _, branch in aligned] == [False] * 3 + [True] * 9


# ---------------------------------------------------------------- 3. 引用


def test_locate_in_chapter_is_scoped_to_this_chapter():
    """同一句话在全文里出现多次、在本章里只有一次时，仍然能正确定位。"""
    text = "甲段。共同的一句话在这里。\n乙段。共同的一句话在这里。\n"
    window = (0, text.index("乙段"))
    assert _locate_in_chapter(text, "共同的一句话在这里。", window) == \
        ("unique", (3, 3 + len("共同的一句话在这里。")))
    # 本章内没有这句（只在下一章）→ missing
    assert _locate_in_chapter(text, "乙段。", window) == ("missing", None)
    # 本章内出现两次 → ambiguous，不猜
    twice = "同一句。同一句。"
    assert _locate_in_chapter(twice, "同一句。", (0, len(twice)))[0] == "ambiguous"


def test_slice_refs_keeps_only_quotes_from_this_chapter():
    """跨章引用被丢掉、本章引用补上绝对坐标、`source_id` 强制成这一章的那一份。"""
    source = _material()
    chapters = _chapter_slices([source])
    chapter = chapters[1]
    inside = "切块决定检索单元"
    assert inside in chapter.prompt_text
    nodes = [{
        "label": "甲",
        "source_refs": [
            {"source_id": "别处", "quote": inside},               # 本章有 → 收下并补偏移
            {"source_id": source.id, "quote": "这一句哪个章都没有"},  # 本章没有 → 丢
            {"source_id": source.id, "quote": ""},                 # 空 → 丢
        ],
    }]
    report = {"refs_located": 0, "refs_ambiguous": 0, "refs_dropped": 0}
    _slice_refs(nodes, chapter, source.text, report)
    kept = nodes[0]["source_refs"]
    assert len(kept) == 1
    assert kept[0]["source_id"] == source.id
    assert chapter.start <= kept[0]["start"] < kept[0]["end"] <= chapter.end
    assert source.text[kept[0]["start"]:kept[0]["end"]] == inside
    assert report["refs_located"] == 1
    assert report["refs_dropped"] == 2


# ---------------------------------------------------------------- 4. 端到端


def test_switch_off_keeps_the_old_shape(monkeypatch):
    """开关关掉：没有 `section_slice` 键、没有新诊断、结构那一次仍然是一次调用。"""
    monkeypatch.delenv("MINDMAP_SECTION_SLICE", raising=False)
    seen: list[tuple[str, str]] = []
    result = _run(_chat(seen=seen))
    assert result.ok, result.content
    assert "section_slice" not in result.data
    assert "SECTION_SLICE_UNAVAILABLE" not in _codes(result)
    # 骨架 1 + 结构 1 + 说明（1 或 2 半）
    assert len(seen) <= 4


def test_switch_on_calls_once_per_chapter_and_assembles_one_branch_each(monkeypatch):
    """开关打开：**每章一次调用**，落树后一级分支数就是章数，每支都有要点。"""
    monkeypatch.setenv("MINDMAP_SECTION_SLICE", "1")
    seen: list[tuple[str, str]] = []
    result = _run(_chat(seen=seen))
    assert result.ok, result.content
    report = result.data["section_slice"]
    assert report["chapters"] == 12
    assert report["calls"] == 12
    assert report["failed_chapters"] == []
    assert result.data["planned_branches"] == 12
    # 骨架 1 + 每章 12 + 说明 1–2
    assert len(seen) >= 13
    # 结构那段 user 里**只有本章原文**：别的章的正文一个字都不该出现
    structure_users = [user for system, user in seen
                       if system.startswith(SECTION_SLICE_PROMPT)]
    assert len(structure_users) == 12
    for user in structure_users:
        others = [text for text in CHAPTERS.values() if text not in user]
        assert len(others) >= len(CHAPTERS) - 2, "一份 prompt 里不该出现别章的正文"


def test_switch_on_keeps_the_third_stage_system_byte_identical(monkeypatch):
    """第三段（只写 detail）的 system 与开关关掉时**逐字节相同**：它没被前面的 N 次影响。"""
    monkeypatch.delenv("MINDMAP_SECTION_SLICE", raising=False)
    off_systems: list[str] = []
    _run(_chat(detail_systems=off_systems))
    monkeypatch.setenv("MINDMAP_SECTION_SLICE", "1")
    on_systems: list[str] = []
    _run(_chat(detail_systems=on_systems))
    assert off_systems and on_systems
    assert set(off_systems) == set(on_systems)


def test_switch_on_degrades_with_a_diagnostic_when_material_cannot_be_sliced(monkeypatch):
    """两份材料时切成不了：**退回现状**并说清为什么（不说就是"假的 A/B"）。"""
    monkeypatch.setenv("MINDMAP_SECTION_SLICE", "1")
    source = _material()
    twin = source.model_copy(update={"id": "s-twin", "filename": "twin.md"})
    ctx = _ctx([source, twin])
    registry = build_registry("create", has_sources=True, has_library=False)
    with patch("app.agent.tools.chat_async", new=_chat()):
        result = asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate",
                     arguments={"topic": TOPIC, "branches": 5}), ctx))
    assert result.ok, result.content
    assert "SECTION_SLICE_UNAVAILABLE" in _codes(result)
    assert "section_slice" not in result.data


def test_switch_on_reports_budget_shortage_instead_of_failing(monkeypatch):
    """额度不够时：能写几章写几章，其余的记一条 `SECTION_SLICE_NO_BUDGET`，树照样交付。"""
    monkeypatch.setenv("MINDMAP_SECTION_SLICE", "1")
    source = _material()
    # 骨架 1 + 前 4 章 + 说明 1 = 6；给 6 份额度，后 8 章没有额度
    ctx = _ctx([source], calls=6)
    registry = build_registry("create", has_sources=True, has_library=False)
    with patch("app.agent.tools.chat_async", new=_chat()):
        result = asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate",
                     arguments={"topic": TOPIC, "branches": 5}), ctx))
    assert result.ok, result.content
    report = result.data["section_slice"]
    assert report["skipped_chapters"] > 0
    assert "SECTION_SLICE_NO_BUDGET" in _codes(result)


def test_switch_on_records_failed_chapters_without_dropping_the_tree(monkeypatch):
    """某一章的调用抛错：那一支空着，别的章与整棵树照常交付。"""
    monkeypatch.setenv("MINDMAP_SECTION_SLICE", "1")
    from app.generation.service import GenerationError

    calls = {"n": 0}

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if system.startswith(PLAN_PROMPT):
            return _skeleton()
        if system.startswith(SECTION_SLICE_PROMPT):
            calls["n"] += 1
            if "第 7 章" in user:
                raise GenerationError("模型 500")
            return {"children": [{"label": "要点"}]}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {}
        return {}

    result = _run(chat)
    assert result.ok, result.content
    assert calls["n"] == 12
    assert result.data["section_slice"]["failed_chapters"] == [7]
    assert "SECTION_SLICE_CALL_FAILED" in _codes(result)
    assert result.data["planned_branches"] == 12


def test_switch_on_survives_a_non_generation_error_in_one_chapter(monkeypatch):
    """某一章抛的不是 `GenerationError`：另外 11 章的结果必须落地。

    这条路把一次调用换成 N 次，任何一次抛个别的异常（超时包装、解析器意外）都会被
    `gather` 原样抛出去，把已经付过钱的产出一起丢掉——所以兜的是 `Exception`。
    """
    monkeypatch.setenv("MINDMAP_SECTION_SLICE", "1")

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if system.startswith(PLAN_PROMPT):
            return _skeleton()
        if system.startswith(SECTION_SLICE_PROMPT):
            if "第 3 章" in user:
                raise ValueError("解析器意外")
            return {"children": [{"label": "要点"}]}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {}
        return {}

    result = _run(chat)
    assert result.ok, result.content
    assert result.data["section_slice"]["failed_chapters"] == [3]
    assert "ValueError" in result.data["diagnostics"][0]["message"]
    assert result.data["planned_branches"] == 12


def test_switch_on_says_so_when_the_outline_is_trimmed(monkeypatch):
    """目录被 `OUTLINE_MAX_ITEMS` 裁掉几章时：切出来的章仍正确，但要说清有一截没归属。

    不说的话，那几章的覆盖率恒为 0，读起来像"模型没写"。
    """
    monkeypatch.setenv("MINDMAP_SECTION_SLICE", "1")
    body = ["# 四十章"]
    for index in range(1, 41):
        # 章越长越靠前（`_trim_outline` 按 coverage 降序保留前 30 条）
        body.append(f"## 章{index}\n\n" + "正文。" * (60 - index))
    source = Source(id="s-40", kind=SourceKind.markdown, status="parsed",
                    filename="forty.md", text="\n".join(body))
    chapters = _chapter_slices([source])
    assert 2 <= len(chapters) <= 30, "切出来的章数不该超过目录上限"
    assert sum(chapter.chars for chapter in chapters) < len(source.text)

    result = _run(_chat(), source=source)
    assert result.ok, result.content
    assert "SECTION_SLICE_PARTIAL_COVERAGE" in _codes(result)


@pytest.mark.parametrize("depth", [SECTION_SLICE_DEPTH])
def test_depth_constant_is_two_so_a_chapter_gets_two_levels(depth):
    """`INITIAL_MAX_DEPTH=3`：root → 分支 → 两层。新路不分组，所以这一笔账是干净的。"""
    assert depth == 2
