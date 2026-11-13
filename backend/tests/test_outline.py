"""材料目录：`heading_outline` 的提取规则，以及它怎么进骨架。

分两层，两层都要盯：

  1. **提取**（纯函数）：目录从哪种标题里来、哪一层算一级、什么算假阳性、
     页眉重复怎么去、超限怎么裁。这层不碰模型也不碰检索。
  2. **接线**：目录非空**且**覆盖率 ≥60% 才拼进骨架的 user 消息；不满足时一个字
     都不加（与加这个功能之前逐字相同）。这条闸是这次改动的全部风险所在——
     半份目录拼进去，模型会把目录当全文，漏掉的章节再也没人问。

`full` 形态不拼目录（正文整份已在 prompt 里，目录是同一份内容的第二份拷贝），
这里也钉一条：`slicing` 为假时目录一次都不算。

数值断言都写成字面量：`coverage` 是原文码点数，跟着夹具走。夹具里的正文用固定
长度的填充句，改夹具就得改数字——那正是"这个数是被算出来的"该有的样子。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    OUTLINE_HEADER,
    OUTLINE_MIN_COVERAGE,
    PLAN_PROMPT,
    _plan_ask,
    _skeleton_outline,
    _skeleton_titles,
    build_registry,
)
from app.contracts.source import Source, SourceKind
from app.knowledge.service import heading_outline, outline_coverage

FILLER = "这一段是正文内容，用来把章节的长度撑开，本身不参与任何断言。" * 3


def _source(text: str, *, source_id: str = "s1") -> Source:
    return Source(id=source_id, kind=SourceKind.document, status="parsed",
                  filename="材料.md", text=text)


def _titles(entries) -> list[str]:
    return [entry.title for entry in entries]


# ---------------------------------------------------------------- 提取：层级选择


def test_markdown_material_uses_hash_level():
    """有两个以上 `#` 时一级就是 `#`：`##` 是它的小节，不混进目录。"""
    text = (
        "# 引言\n\n" + FILLER + "\n\n"
        "## 背景\n\n小节不算独立一章\n\n"
        "# 方法\n\n" + FILLER + "\n"
    )
    entries = heading_outline([_source(text)])
    assert _titles(entries) == ["引言", "方法"]


def test_single_hash_title_falls_back_to_next_level():
    """只有一条 `#` 时它是**文档标题**，一级下探到 `##`。

    这是本实现与"markdown 一律取 `#`"唯一的差别，理由是材料本身：
    `docs/RAG-DECISIONS.md`（以及本仓库 10 份实测材料里的 9 份）是 1 个 `#`
    + 10 个 `##` 的形状。只认 `#` 会得到"一条目录覆盖 100%"，那不是目录是书名。
    """
    text = (
        "# 材料整理手册\n\n"
        "## 预算审批\n\n" + FILLER + "\n\n"
        "## 绩效考核\n\n" + FILLER + "\n"
    )
    entries = heading_outline([_source(text)])
    assert _titles(entries) == ["预算审批", "绩效考核"]
    assert "材料整理手册" not in _titles(entries)


def test_plain_text_material_uses_the_most_frequent_level():
    """没有 `#` 的材料退回编号标题，取**出现次数最多**的那一层。

    `一、` 与 `（一）` 混排时只取 `一、`：后者是小节。按频次而不是"先出现的那个"
    选，结论才不会取决于材料里哪一级先露面。
    """
    text = (
        "一、实验目的\n\n" + FILLER + "\n\n"
        "（一）子目的\n\n小节内容\n\n"
        "二、实验环境\n\n" + FILLER + "\n\n"
        "三、实验步骤\n\n" + FILLER + "\n"
    )
    entries = heading_outline([_source(text)])
    assert _titles(entries) == ["实验目的", "实验环境", "实验步骤"]


def test_numbering_is_stripped_from_titles():
    """分隔符编号属于目录本身，不属于标题：调用方会重新编号。

    留着原文编号会渲染成 `1. 一、实验目的`。
    """
    text = "1. 引言\n\n" + FILLER + "\n\n2. 方法\n\n" + FILLER + "\n"
    assert _titles(heading_outline([_source(text)])) == ["引言", "方法"]


def test_markdown_marker_is_stripped_but_a_bare_number_is_kept():
    """`# 一、实验目的` → `实验目的`；`# 3 种检索形态` 一字不改。

    后者那个 `3 ` 后面没有分隔符，它可能是标题的一部分——剥掉就成了"种检索形态"。
    判据是"分隔符编号"（`一、`/`1.`/`（一）`/`第N章`），不是"以数字开头"。
    """
    text = (
        "# 一、实验目的\n\n" + FILLER + "\n\n"
        "# 二、实验任务\n\n" + FILLER + "\n\n"
        "# 3 种检索形态\n\n" + FILLER + "\n"
    )
    assert _titles(heading_outline([_source(text)])) == ["实验目的", "实验任务", "3 种检索形态"]


def test_long_title_is_dropped_in_plain_text_mode():
    """非 markdown 候选超过 40 字直接丢：那是散文句，不是标题。

    markdown 标题反过来是**截断**（它确实是标题），两条路差别就在这里。
    """
    long_line = "一、" + "这是一个非常长的列表项它其实是正文里的一句话而不是章节标题" * 2
    text = (
        long_line + "\n\n正文\n\n"
        "二、实验环境\n\n" + FILLER + "\n\n"
        "三、实验步骤\n\n" + FILLER + "\n"
    )
    entries = heading_outline([_source(text)])
    assert _titles(entries) == ["实验环境", "实验步骤"]


def test_markdown_long_title_is_truncated_not_dropped():
    text = (
        "# " + "很长的章节名" * 12 + "\n\n" + FILLER + "\n\n"
        "# 方法\n\n" + FILLER + "\n"
    )
    entries = heading_outline([_source(text)])
    assert len(entries) == 2
    assert len(entries[0].title) == 40
    assert entries[0].title == ("很长的章节名" * 12)[:40]


def test_prose_like_line_is_filtered_out():
    """`**` / `|` / 冒号后长分句 三条假阳性特征，一条都不许进目录。"""
    text = (
        "一、判断器\n\n" + FILLER + "\n\n"
        "二、实验环境\n\n" + FILLER + "\n\n"
        "三、实验步骤\n\n" + FILLER + "\n"
    )
    # 这行在 `_HEADING_RE` 眼里是标题（`1.` 开头），但它是加粗的列表项
    bad = "1. **判断器不是噪声源，是系统性失效**。同一 query 跑 3 次结论就变\n\n"
    entries = heading_outline([_source(text + bad)])
    assert "**" not in "".join(_titles(entries))
    assert _titles(entries) == ["判断器", "实验环境", "实验步骤"]


def test_list_item_without_blank_line_after_is_filtered_out():
    """列表项后面紧跟内容，标题后面空一行——用这一点挡掉连排的编号列表。"""
    ran = "1. 第一条步骤\n2. 第二条步骤\n3. 第三条步骤\n"
    text = ran + "\n" + "一、实验环境\n\n" + FILLER + "\n\n" + "二、实验步骤\n\n" + FILLER + "\n"
    entries = heading_outline([_source(text)])
    assert _titles(entries) == ["实验环境", "实验步骤"]


def test_repeated_header_is_deduplicated_keeping_first_position():
    """PDF 页眉会把同一行印在每页顶上：按文本去重，留第一次出现的位置。"""
    text = (
        "实验报告\n\n"
        "一、实验目的\n\n" + FILLER + "\n\n"
        "实验报告\n\n"
        "二、实验环境\n\n" + FILLER + "\n"
    )
    entries = heading_outline([_source(text)])
    assert _titles(entries) == ["实验目的", "实验环境"]


def test_bare_digit_line_is_not_a_heading():
    """`1.5 倍` 这类行首数字不是编号：小数不是两级标题。"""
    text = (
        "一、实验环境\n\n" + FILLER + "\n\n"
        "1.5 倍\n\n正文\n\n"
        "二、实验步骤\n\n" + FILLER + "\n"
    )
    entries = heading_outline([_source(text)])
    assert _titles(entries) == ["实验环境", "实验步骤"]


def test_no_heading_returns_empty_list():
    """没有可用目录 = 空列表，调用方据此回退到"自行归纳"。"""
    assert heading_outline([_source("整篇都是正文，没有任何标题行。" * 5)]) == []
    assert heading_outline([]) == []
    assert heading_outline([_source("   ")]) == []


def test_single_entry_is_not_an_outline():
    """一条标题的"目录"覆盖全文，对骨架是误导——按没有目录处理。"""
    assert heading_outline([_source("# 唯一的标题\n\n" + FILLER)]) == []


def test_multi_source_merges_in_order():
    first = _source("## 预算审批\n\n" + FILLER + "\n", source_id="a")
    second = _source("## 绩效考核\n\n" + FILLER + "\n", source_id="b")
    entries = heading_outline([first, second])
    # 每份材料各自出目录（各自 ≥2 条）→ 单条的不算，合并后为空
    assert entries == []

    first = _source("## 预算审批\n\n" + FILLER + "\n\n## 预算执行\n\n" + FILLER + "\n",
                    source_id="a")
    second = _source("## 绩效考核\n\n" + FILLER + "\n\n## 绩效改进\n\n" + FILLER + "\n",
                     source_id="b")
    assert _titles(heading_outline([first, second])) == [
        "预算审批", "预算执行", "绩效考核", "绩效改进"]


def test_bold_paragraphs_carry_the_structure_of_a_real_docx():
    """docx 的真形状：Docling 只从标题样式推出一个 `## 实验4 …`，正文里的章节名
    是**整段加粗**（`**一、实验须知**`）。

    这份夹具照抄桌面上那份真实实验报告的结构（`scripts/measure_outline_skeleton.py
    --docx` 跑出来就是这四条）：一条 ATX 标题 + 若干编号加粗段 + 一堆加粗的
    表头/正文行。只有编号形状的加粗行算章节。
    """
    text = (
        "## 实验4 图形数据结构实现及其应用\n\n"
        "**班别：2025级工业软件 4 班    学号：2025404100403    姓名：陈嘉涛**\n\n"
        "**成绩：\\_\\_\\_\\_\\_**\n\n"
        "**一、实验须知**\n\n" + FILLER + "\n\n"
        "**（2）提交截止时间：以平台上设置的截止时间为准。**\n\n"
        "**二、实验目的**\n\n" + FILLER + "\n\n"
        "**三、实验内容**\n\n" + FILLER + "\n\n"
        "**请填写以下内容：**\n\n" + FILLER + "\n\n"
        "**四、实验总结**\n\n" + FILLER + "\n"
    )
    entries = heading_outline([_source(text)])
    assert _titles(entries) == ["实验须知", "实验目的", "实验内容", "实验总结"]
    # 加粗的正文行/表头不是章节：它们的文字不以编号开头
    assert "班别" not in "".join(_titles(entries))
    assert "请填写以下内容" not in "".join(_titles(entries))


def test_single_atx_title_alone_is_still_not_an_outline():
    """只有一条 `#`、又没有编号加粗段时，目录为空（不是"取那一条当目录"）。"""
    assert heading_outline([_source("## 实验4 报告\n\n" + FILLER)]) == []


# ---------------------------------------------------------------- 提取：coverage


def test_coverage_is_distance_to_next_heading_of_the_same_level():
    """`coverage` = 本条标题到**下一个同级标题**之间的字数，末条到文件结束。"""
    text = "# 甲\n\nabc\n\n# 乙\n\ndefgh\n"
    entries = heading_outline([_source(text)])
    assert _titles(entries) == ["甲", "乙"]
    # 甲的起点 0，乙的起点是 "# 乙" 的位置
    second = text.index("# 乙")
    assert entries[0].start == 0
    assert entries[0].coverage == second
    assert entries[1].coverage == len(text) - second


def test_outline_coverage_is_a_char_weighted_ratio():
    text = "## 甲\n\n" + FILLER + "\n\n## 乙\n\n" + FILLER + "\n"
    source = _source(text)
    entries = heading_outline([source])
    expected = sum(entry.coverage for entry in entries) / len(text)
    assert outline_coverage([source], entries) == expected
    assert 0.99 < expected <= 1.0


def test_outline_coverage_of_empty_catalogue_is_zero():
    source = _source("没有标题的正文" * 20)
    assert outline_coverage([source]) == 0.0
    assert outline_coverage([]) == 0.0


def test_outline_coverage_weights_sources_by_char_count():
    """多份材料按字数加权：便签不该与手册各占一半权重。"""
    big = _source("## 甲\n\n" + FILLER * 5 + "\n\n## 乙\n\n" + FILLER * 5 + "\n",
                  source_id="big")
    small = _source("没有目录的便签。", source_id="small")
    entries = heading_outline([big, small])
    total = len(big.text) + len(small.text)
    assert outline_coverage([big, small], entries) == pytest.approx(
        sum(entry.coverage for entry in entries) / total)
    # 便签没有目录，但它那句正文照样进分母 → 覆盖率低于 100%
    assert outline_coverage([big, small], entries) < 1.0


# ---------------------------------------------------------------- 提取：条数上限


def test_overflow_keeps_the_largest_sections_in_document_order():
    """超限按 coverage 降序留下大章，交出去时仍按原文顺序。"""
    sizes = (1, 9, 2, 8, 3)          # 第 2 与第 4 章最大
    parts = [f"## 第{index + 1}章\n\n" + "字" * (size * 20) + "\n"
             for index, size in enumerate(sizes)]
    text = "\n".join(parts)
    entries = heading_outline([_source(text)], max_items=2)
    assert _titles(entries) == ["第2章", "第4章"]        # 顺序是原文顺序
    assert [entry.start for entry in entries] == sorted(entry.start for entry in entries)


# ---------------------------------------------------------------- 接线：骨架


class _Plan:
    """骨架的模型替身：记下每一次 user 消息，返回三条分支。"""

    def __init__(self) -> None:
        self.systems: list[str] = []
        self.users: list[str] = []

    async def __call__(self, system: str, user: str, *, max_tokens=None, on_delta=None):
        self.systems.append(system)
        self.users.append(user)
        if system.startswith(PLAN_PROMPT):
            return {"branches": ["预算审批", "绩效考核", "预算执行分析"]}
        return {"children": [{"label": "要点一", "expandable": False},
                             {"label": "要点二", "expandable": False}]}

    @property
    def plan_user(self) -> str:
        return next(user for system, user in zip(self.systems, self.users)
                    if system.startswith(PLAN_PROMPT))


def _run_deep(sources: list[Source], *, mode: str = "rag"):
    request = AgentRunRequest(
        goal="整理管理办法", intent="create", sources=sources,
        budget=RunBudget(max_model_calls=20, max_tool_calls=8, max_output_tokens=32768),
    )
    ctx = RunContext(request=request, candidate=None,
                     sources_by_id={source.id: source for source in sources})
    model = _Plan()
    registry = build_registry("create", has_sources=True)
    with patch("app.agent.tools.chat_async", new=model), \
            patch("app.agent.tools.source_mode", new=lambda: mode):
        result = asyncio.run(registry.execute(
            ToolCall(call_id="c-deep", name="mindmap_generate_deep",
                     arguments={"topic": "管理办法", "branches": 3,
                                "points_per_branch": 2}),
            ctx,
        ))
    return result, model


HIGH_COVERAGE = (
    "# 管理办法\n\n"
    "## 预算审批\n\n" + FILLER + "\n\n"
    "## 绩效考核\n\n" + FILLER + "\n\n"
    "## 预算执行分析\n\n" + FILLER + "\n"
)
# 前 75% 是一段没有任何标题的正文，目录只管住最后那一小块 → 覆盖率远低于 60%
LOW_COVERAGE = (
    "这一大段是没有章节的流水账。" * 60 + "\n\n"
    "## 预算审批\n\n" + FILLER + "\n\n"
    "## 绩效考核\n\n" + FILLER + "\n"
)


def test_outline_reaches_the_skeleton_prompt_when_coverage_is_high():
    sources = [_source(HIGH_COVERAGE)]
    entries = heading_outline(sources)
    assert outline_coverage(sources, entries) >= OUTLINE_MIN_COVERAGE

    result, model = _run_deep(sources)
    assert result.ok, result.content
    user = model.plan_user
    assert OUTLINE_HEADER in user
    # 章节名逐字来自原文
    for entry in entries:
        assert entry.title in user
        assert entry.title in sources[0].text
    assert "1. 预算审批" in user
    assert "约占" in user


def test_outline_is_absent_when_coverage_is_low():
    sources = [_source(LOW_COVERAGE)]
    entries = heading_outline(sources)
    # 目录本身是有的（两条标题），只覆盖一小块 —— 这正是必须**不拼**的那种
    assert entries
    assert outline_coverage(sources, entries) < OUTLINE_MIN_COVERAGE

    result, model = _run_deep(sources)
    assert result.ok, result.content
    assert OUTLINE_HEADER not in model.plan_user


def test_outline_is_absent_when_material_has_no_outline():
    sources = [_source("整篇都是正文。" * 200)]
    assert heading_outline(sources) == []
    result, model = _run_deep(sources)
    assert result.ok, result.content
    assert OUTLINE_HEADER not in model.plan_user
    # 与没有目录时逐字相同：主题 + 条数要求
    assert model.plan_user.startswith("主题：管理办法\n根据内容实际包含的方面")


def test_full_mode_never_builds_an_outline():
    """`full` 下正文整份在 prompt 里，目录是同一份内容的第二份拷贝——不拼。"""
    sources = [_source(HIGH_COVERAGE)]
    with patch("app.agent.tools._skeleton_outline") as spy:
        result, model = _run_deep(sources, mode="full")
    assert result.ok, result.content
    assert spy.call_count == 0
    assert OUTLINE_HEADER not in model.plan_user


def test_plan_ask_without_outline_is_unchanged():
    """没有目录时 user 消息与加这个功能之前逐字相同（回归口径）。"""
    assert _plan_ask("管理办法", 3) == (
        "主题：管理办法\n"
        "根据内容实际包含的方面来定一级分支数量，最多 3 条。\n"
        "材料层次多就用足、内容单薄就少给，不要为了凑数把不同的事塞进同一支。\n"
        "每条不超过 20 字，互不重叠，按逻辑顺序排列。"
    )


def test_plan_ask_puts_outline_between_topic_and_count():
    """目录在主题之后、条数要求之前：先看结构，再数条数。"""
    ask = _plan_ask("管理办法", 3, outline=OUTLINE_HEADER + "\n1. 预算审批（约占 40%）")
    assert ask.index("主题：管理办法") < ask.index(OUTLINE_HEADER)
    assert ask.index(OUTLINE_HEADER) < ask.index("最多 3 条")


def test_skeleton_outline_reports_whole_material_shares():
    """百分比的分母是全文：三条各占三分之一，加起来正好 100% 上下。"""
    block = _skeleton_outline([_source(HIGH_COVERAGE)])
    shares = [int(line.rsplit("约占 ", 1)[1].rstrip("）%")) for line in block.splitlines()[1:]]
    assert sum(shares) >= 95


def test_skeleton_contract_is_still_a_branch_list():
    """骨架的输出契约不变：`_skeleton_titles` 只认 `{"branches": [str]}`。"""
    assert _skeleton_titles({"branches": ["甲", "乙"]}) == ["甲", "乙"]
    assert _skeleton_titles({"branches": [{"label": "甲"}]}) == []
    assert _skeleton_titles(["甲"]) == []
    # 目录进 prompt 之后，模型仍被要求只回这个形状
    assert '{"branches":["分支一","分支二"]}' in PLAN_PROMPT
    assert "优先采用它们作为一级分支" in PLAN_PROMPT
    assert json.dumps({"branches": ["甲"]}, ensure_ascii=False) == '{"branches": ["甲"]}'
