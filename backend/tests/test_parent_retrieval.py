"""小块检索、大块召回（`search_parent`）：两级切块与"折父块"的行为约束。

三条主线，各自钉一类错误：

1. **树的结构**。父块覆盖全文、子块严格落在父块内、`level`/`parent` 自洽、
   `text[start:end]` 对父子两级都逐字成立。偏移一旦错位，界面高亮会标到别的字上，
   而那种错在人工验收里看起来"只是差了几个字"，只能靠断言钉住。
2. **折父块**。命中映射到父块、同一父块的多个子命中并成一条、分数取子块的较高者，
   并且两级坐标都留在结果里（`start/end` 是父块，`leaf_start/leaf_end` 是命中的
   子块）。这几步是把"匹配粒度"与"返回粒度"拆开的**全部**风险所在：映射错了会
   返回一段与命中无关的原文，不合并会让 `top_k` 被同一父块的多条子命中占满，
   丢了子块区间则引用校验退化成格式检查。
3. **开关关着时逐字段等价**。`f72c14c` 的旧 `service.py` 整份载进来对比——这一轮
   把 `_single_search` 拆成了 `_fuse` + 薄壳，那条对外行为必须逐字段不变，而这件事
   没有任何运行时信号，只有这组对比能钉住。

全部离线：`conftest.py` 已经清空凭证并把三个开关置 0；需要向量路的用例显式打开
`MINDMAP_HYBRID_SEARCH` 并替换 `embed_texts`，不会走到 `httpx` 的传输层。
"""
from __future__ import annotations

import hashlib
import importlib.util
import random
import subprocess
import sys
from pathlib import Path

import pytest

from app.contracts.source import Source, SourceKind
from app.knowledge import service
from app.knowledge.service import (
    CHUNK_MAX_CHARS,
    CHUNK_OVERLAP_CHARS,
    LEAF_MAX_CHARS,
    LEAF_MIN_CHARS,
    LEAF_TARGET_CHARS,
    PARENT_RETRIEVAL_ENV,
    EvidenceHit,
    _bm25_scores,
    _chunk_spans,
    _chunk_tree,
    _chunks,
    _fold_to_parents,
    _LeafCorpus,
    _near_duplicate,
    search_parent,
    search_sources,
)

# 写死，不跟随 HEAD：HEAD 之后 `service.py` 就被这一轮改过了（新增两级切块、
# 把 `_single_search` 拆成 `_fuse` + 薄壳），拿 HEAD 对比等于拿自己跟自己比，
# 永远是绿的。sha256 一并写死：只写 rev 的话，改掉常量就能悄悄换一个"基线"，
# 而"基线其实是新代码"这件事没有任何迹象。
BASELINE_REV = "f72c14c"
BASELINE_SHA256 = "40336f3ed4e22f8194081511bb3847fd372997d1441d3365f2c95ec650a04ab8"

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def parent_on(monkeypatch):
    """显式打开两级检索。`conftest.py` 的默认值是 0（与线上缺省一致）。"""
    monkeypatch.setenv(PARENT_RETRIEVAL_ENV, "1")


# ---------------------------------------------------------------- 测试材料


def _source(text: str) -> Source:
    return Source(kind=SourceKind.text, status="parsed", text=text)


# 十个小节，每节一个主题、约 60 字，互相不共享关键词。重复几轮就是一份有结构
# （Markdown 标题 = 一级切点）、有多块、每块几个子块的中文长文。
SECTIONS = (
    ("预算审批的层级",
     "预算审批由部门负责人签核，超过额度的部分必须上报总经理复审，"
     "财务只在核准后执行付款，权责边界写在制度附件里。"),
    ("报销时限",
     "费用报销的时限是三十个自然日，逾期不再受理，"
     "票据缺失的申请一律退回并通知经办人补充材料。"),
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
    ("安全巡检",
     "安全巡检覆盖消防通道与配电间，隐患整改单闭环后才允许销项，"
     "复检结果留档三年。"),
    ("客户回访",
     "客户回访按成交金额分级，季度内至少一次电话沟通，"
     "投诉工单要在四十八小时内响应。"),
    ("数据备份",
     "数据备份每日增量、每周全量，异地副本保留三十天，"
     "恢复演练每半年做一次并记录耗时。"),
)


def _material(repeat: int = 3) -> str:
    """约 1600 字：4 个父块、10 个子块，每个父块内有 2–3 个子块。

    刻意不用 `"字"*N` 这种合成文本：真实材料的切点落在标题与空行上，重叠量、
    末块长度、子块落点都由分隔符决定，而这些正是父块映射要面对的形态。
    """
    parts = ["# 制度汇编\n\n"]
    for round_index in range(repeat):
        for title, body in SECTIONS:
            parts.append(f"## {round_index + 1}.{title}\n\n{body}\n\n")
    return "".join(parts)


NEEDLE = "紫铜铃兰"
_FILLER = "本段交代背景，时间地点与参与人员一并记录在案，与检索目标无关。"


def _needle_material() -> str:
    """唯一词只落在一个子块里，且那个父块明显比子块长（> `LEAF_MAX_CHARS`）。

    段落插在第 2 节之前：再往后挪，唯一词就会落进两个父块的重叠区里（父块
    之间有 `CHUNK_OVERLAP_CHARS` 的重叠），"唯一命中"这个前提就没了。这条
    材料的位置是实测定下来的——把它当成"随便找根材料"会在改切块参数时变成
    一条莫名其妙的红灯，所以测试里另有一条前置断言把位置要求写死。
    """
    parts = ["# 巡检记录\n\n"]
    for index in range(9):
        if index == 2:
            parts.append(f"## 附录甲\n\n{_FILLER}样品的编号为{NEEDLE}，"
                         f"由后勤处封存待检。{_FILLER}\n\n")
        parts.append(f"## 第{index + 1}节\n\n{_FILLER * 3}\n\n")
    return "".join(parts)


def _parents(tree):
    return [node for node in tree if node.level == 0]


def _short_parent_material() -> str:
    """两个父块，其中第一个只有 150 字，且与第二个重叠 80 字（> 较短者一半）。

    构造依赖 `_pick_cut` 的两段窗口：前 450 字里没有分隔符，于是它退到
    `[MIN, TARGET)` 取最靠后的句末（index 149），父块 0 就只有 150 字；
    `_overlap_start` 再向后吸附到 index 69 那个句首，于是父块 1 从 70 起。
    两个关键字分别放在 0–150 与 70–770 两段里，让两个父块都命中。
    """
    head = "甲" * 20 + "乙丙" + "字" * 47 + "。"      # 句末在 69，父块 0 的起点候选在 70
    second = "字" * 79 + "。"                          # 句末在 149，父块 0 的切点落在 150
    tail = "字" * 451 + "丙丁" + "字" * 400            # 550–770 段无分隔符，父块 1 顶到 700
    return head + second + tail


def _leaves(tree):
    return [node for node in tree if node.level == 1]


# ---------------------------------------------------------------- 1 树结构


def test_tree_invariants():
    """父块覆盖全文；每个子块严格落在父块内；level/parent 自洽；切片逐字成立。"""
    for text in (_material(), _needle_material(), "短", "字" * 3200):
        tree = _chunk_tree(text)
        parents = _parents(tree)
        assert parents, "任何文本至少有一个父块"

        # 父块覆盖全文：与 `_chunk_spans` 的覆盖约束一致
        assert parents[0].start == 0
        assert parents[-1].end == len(text)
        for left, right in zip(parents, parents[1:]):
            assert left.end > left.start
            # 相接或重叠，中间不许有空洞
            assert right.start <= left.end
            assert right.end > right.start

        leaf_count = 0
        for index, node in enumerate(tree):
            assert node.end > node.start, "零宽区间不该出现在任何一个层级"
            assert text[node.start:node.end] is not None
            assert isinstance(text[node.start:node.end], str)
            if node.level == 0:
                assert node.parent is None
                continue
            assert node.level == 1
            leaf_count += 1
            # 父下标必须指向自己前面那个父块，且区间包含关系成立
            assert node.parent is not None and node.parent < index
            parent = tree[node.parent]
            assert parent.level == 0
            assert parent.parent is None
            assert parent.start <= node.start < node.end <= parent.end
        assert leaf_count >= len(parents), "每个父块至少切出一个子块"


def test_tree_spans_are_verbatim_slices():
    """`text[start:end]` 逐字成立，父子两级都不例外（不 strip、不改写）。"""
    text = _material()
    tree = _chunk_tree(text)
    # 父块切片必须逐字等于 `_chunks` 产出的 quote：两条路对同一段原文给出同一个
    # 字符串，父块才是"既有的块"，而不是另一个碰巧区间相同的概念。
    chunk_quotes = {start: quote for start, _, quote in _chunks(text)}
    for node in tree:
        quote = text[node.start:node.end]
        # 长度必须等于区间长度：任何 strip / 归一化都会让这两个数对不上，
        # 而那正是前端高亮错位的来源
        assert len(quote) == node.end - node.start
        assert quote
        if node.level == 0:
            assert quote == chunk_quotes[node.start]
            continue
        parent = tree[node.parent]
        assert quote in text[parent.start:parent.end]


def test_tree_parents_match_chunk_spans():
    """父块区间就是 `_chunk_spans` 的结果，一个字节都不多不少。"""
    for text in (_material(), _needle_material(), ""):
        assert [(node.start, node.end) for node in _parents(_chunk_tree(text))] \
            == _chunk_spans(text)


def test_tree_empty_and_short_text():
    """空文本只有零宽父块、没有子块；短文本的父块就是它自己的子块。"""
    empty = _chunk_tree("")
    assert [(node.start, node.end, node.level, node.parent) for node in empty] \
        == [(0, 0, 0, None)]

    tiny = _chunk_tree("短")
    assert [(node.start, node.end, node.level, node.parent) for node in tiny] \
        == [(0, 1, 0, None), (0, 1, 1, 0)]


def test_leaf_lengths_stay_within_bounds():
    """子块长度落在 [LEAF_MIN, LEAF_MAX]，三类例外各有明确来源。

    例外清单（写成断言而不是注释，改参数时会红）：
      - **父块本身不足上限**：子块就是父块，长度跟着父块走，可以短于 MIN；
      - **末块合并后超上限**：`_merge_short` 宁可末块多出一点，也不留碎块；
      - **无标点硬切**：整窗没有安全切点，切点被 `_hard_cut` 顶到上限。
    """
    assert LEAF_MIN_CHARS < LEAF_TARGET_CHARS < LEAF_MAX_CHARS
    assert LEAF_MAX_CHARS < CHUNK_MAX_CHARS, "子块必须明显短于父块，否则两级没有意义"

    for text in (_material(), _needle_material(), "字" * 3200):
        tree = _chunk_tree(text)
        # 下标必须用 `tree` 里的真实位置：父块与子块在同一个列表里交错，
        # 拿"过滤后的父块列表"的下标去对 `node.parent` 会整体错位。
        for parent_index, parent in enumerate(tree):
            if parent.level != 0:
                continue
            siblings = [node for node in _leaves(tree) if node.parent == parent_index]
            assert siblings, "父块必须有子块"
            parent_length = parent.end - parent.start
            if parent_length <= LEAF_MAX_CHARS:
                # 例外一：父块短，子块等于父块
                assert len(siblings) == 1
                assert siblings[0].end - siblings[0].start == parent_length
                continue
            for position, node in enumerate(siblings):
                length = node.end - node.start
                assert length <= LEAF_MAX_CHARS + LEAF_MIN_CHARS, "末块合并的上限"
                if length > LEAF_MAX_CHARS:
                    # 例外二：只有末块可以超上限（并进了前一块）
                    assert position == len(siblings) - 1
                if position < len(siblings) - 1:
                    # 非末块一律 >= MIN：贪心切点的下限就是 LEAF_MIN
                    assert length >= LEAF_MIN_CHARS
            # 子块首尾必须贴住父块：中间不许漏字
            assert siblings[0].start == parent.start
            assert siblings[-1].end == parent.end
            for left, right in zip(siblings, siblings[1:]):
                assert right.start <= left.end


def test_leaf_hard_cut_without_punctuation():
    """3200 字无标点：只走硬切，切点一律顶到上限，末块允许被合并。"""
    text = "字" * 3200
    leaves = _leaves(_chunk_tree(text))
    assert leaves
    lengths = [node.end - node.start for node in leaves]
    # 硬切没有安全点可选，`_hard_cut` 直接返回 high = pos + LEAF_MAX
    assert lengths[0] == LEAF_MAX_CHARS
    for length in lengths:
        assert LEAF_MAX_CHARS <= length <= LEAF_MAX_CHARS + LEAF_MIN_CHARS
    covered = bytearray(len(text))
    for node in leaves:
        covered[node.start:node.end] = b"\x01" * (node.end - node.start)
    assert all(covered)


def test_leaf_overlap_stays_inside_parent():
    """相邻子块可以重叠（沿用 `OVERLAP` 策略），但重叠后的起点仍在父块内。"""
    text = _material()
    tree = _chunk_tree(text)
    overlapped = 0
    for parent_index, parent in enumerate(tree):
        if parent.level != 0:
            continue
        siblings = [node for node in _leaves(tree) if node.parent == parent_index]
        for left, right in zip(siblings, siblings[1:]):
            assert right.start > left.start, "子块起点必须严格递增，否则切块会不推进"
            overlap = left.end - right.start
            assert overlap >= 0
            assert parent.start <= right.start, "重叠后的起点不能跑到父块外"
            assert right.end <= parent.end
            overlapped += 1 if overlap > 0 else 0
    assert overlapped, "真实材料上必须出现重叠，否则这条断言没有意义"


# ---------------------------------------------------------------- 2 命中的是父块


def test_hit_returns_parent_span(parent_on):
    """唯一命中的子块，返回的是它**父块**的区间与全文。"""
    text = _needle_material()
    tree = _chunk_tree(text)
    hit_leaves = [node for node in _leaves(tree) if NEEDLE in text[node.start:node.end]]
    # 材料设计的前置条件：唯一词只落在一个子块、一个父块里
    assert len(hit_leaves) == 1
    leaf = hit_leaves[0]
    parent = tree[leaf.parent]
    leaf_quote = text[leaf.start:leaf.end]
    assert parent.end - parent.start > LEAF_MAX_CHARS, "父块必须明显长于子块"

    hits = search_parent([_source(text)], NEEDLE, top_k=3)
    assert len(hits) == 1
    hit = hits[0]
    assert (hit.start, hit.end) == (parent.start, parent.end)
    assert hit.quote == text[parent.start:parent.end]
    assert len(hit.quote) > len(leaf_quote)
    assert leaf_quote in hit.quote
    assert hit.score == hit.score_bm25 > 0


def test_switch_on_changes_the_scoring_unit(monkeypatch):
    """开关不是摆设：同一个区间在开着/关着时分数不同，因为打分单元不同。

    父块与块级切块的**坐标空间本来就是同一套**（父块 = `_chunk_spans` 的块），
    所以"区间集合不一样"证明不了开关生效——它俩本来就该落在同一批边界上。真正的
    差别在打分单元：子块上的 BM25 用 N = 子块数、df 在子块集合上算，块级用
    N = 块数。同一段原文因此得到不同的分数，这正是 docstring 里"分数来自子块
    匹配、区间来自父块"那句的可观测后果。
    """
    text = _material()
    source = _source(text)
    parent_spans = {(node.start, node.end) for node in _parents(_chunk_tree(text))}

    monkeypatch.setenv(PARENT_RETRIEVAL_ENV, "0")
    off = {(hit.start, hit.end): hit for hit in search_sources([source], "预算审批", 3)}
    monkeypatch.setenv(PARENT_RETRIEVAL_ENV, "1")
    on = search_parent([source], "预算审批", 3)

    assert on
    assert {(hit.start, hit.end) for hit in on} <= parent_spans
    shared = [hit for hit in on if (hit.start, hit.end) in off]
    assert shared, "开关两侧至少要在同一个区间上有结果，否则这条对比没有意义"
    assert any(hit.score != off[(hit.start, hit.end)].score for hit in shared)


# ---------------------------------------------------------------- 3 同父合并


def test_fold_keeps_best_score_per_parent():
    """同一父块的多个子命中折成一条，分数取较高的那条（按分数，不按文本顺序）。

    合成数据而不是端到端构造：这里要验证的是**取舍规则**，不是切块落点。
    两条子命中的分数高低刻意与文本顺序相反（0.05 在后、0.03 在前），所以
    "取了第一条"的实现会立刻变红。
    """
    text = "甲" * 900
    corpus = _LeafCorpus(
        items=[
            ("s", 0, 200, text[0:200]),
            ("s", 200, 400, text[200:400]),
            ("s", 400, 500, text[400:500]),   # 父块 0 的末尾
            ("s", 500, 600, text[500:600]),
        ],
        parents=[(0, 500), (0, 500), (0, 500), (400, 800)],
        texts={"s": text},
    )
    ranked = [
        EvidenceHit(source_id="s", start=200, end=400, quote="x", score=0.05,
                    score_bm25=3.0, method="lexical"),
        EvidenceHit(source_id="s", start=0, end=200, quote="y", score=0.03,
                    score_bm25=1.0, method="lexical"),
        EvidenceHit(source_id="s", start=500, end=600, quote="z", score=0.02,
                    score_bm25=0.5, method="lexical"),
    ]

    folded = _fold_to_parents(ranked, corpus)
    assert [(hit.start, hit.end) for hit in folded] == [(0, 500), (400, 800)]
    # 分数留在子块：0.05 是第二条子命中的分，不是"父块的分数"
    assert folded[0].score == 0.05
    assert folded[0].score_bm25 == 3.0
    assert folded[1].score == 0.02
    assert folded[0].quote == text[0:500]
    assert folded[1].quote == text[400:800]


def test_same_parent_children_are_merged(parent_on):
    """端到端：命中同一父块的两个子块，结果里只出现一条，且分数是较高的那个。"""
    text = _material()
    tree = _chunk_tree(text)
    leaves = _leaves(tree)
    quotes = [text[node.start:node.end] for node in leaves]
    scores = _bm25_scores("预算审批", quotes)

    # 材料设计的前置条件：存在一个父块，它的两个以上子块都有正分
    grouped: dict[int, list[int]] = {}
    for index, node in enumerate(leaves):
        if scores[index] > 0:
            grouped.setdefault(node.parent, []).append(index)
    crowded = [parent for parent, indexes in grouped.items() if len(indexes) >= 2]
    assert crowded, "材料里必须有'同一父块多个子块命中'的情形"
    parent_index = crowded[0]
    best = max(scores[index] for index in grouped[parent_index])
    assert min(scores[index] for index in grouped[parent_index]) < best, \
        "两条子命中的分数必须不同，否则'取较高者'无从验证"

    parent = tree[parent_index]
    hits = search_parent([_source(text)], "预算审批", top_k=5)
    under = [hit for hit in hits if (hit.start, hit.end) == (parent.start, parent.end)]
    assert len(under) == 1, "同一父块只能占一条"
    assert under[0].score == round(best, 6)
    assert under[0].quote == text[parent.start:parent.end]
    # 整批结果里每个父块也各只出现一次
    spans = [(hit.start, hit.end) for hit in hits]
    assert len(spans) == len(set(spans))


# ---------------------------------------------------------------- 4 top_k 语义


def test_top_k_limits_distinct_parents(parent_on):
    """条数 <= top_k，且每条都落在不同的父块上。"""
    text = _material(repeat=4)
    tree = _chunk_tree(text)
    parent_spans = {(node.start, node.end) for node in _parents(tree)}

    total = len(search_parent([_source(text)], "预算审批", top_k=50))
    assert total >= 3, "这份材料上要能命中三个以上父块，否则截断测不出来"

    for top_k in (1, 2, 3):
        hits = search_parent([_source(text)], "预算审批", top_k=top_k)
        assert 0 < len(hits) <= top_k
        spans = [(hit.start, hit.end) for hit in hits]
        assert len(spans) == len(set(spans))
        assert set(spans) <= parent_spans


def test_empty_query_and_blank_sources(parent_on):
    """空查询、纯空白来源都返回 `[]`，不抛错——与 `search_sources` 同口径。"""
    assert search_parent([_source(_material())], "   ", top_k=3) == []
    assert search_parent([_source("   ")], "预算审批", top_k=3) == []
    assert search_parent([], "预算审批", top_k=3) == []


def test_hit_carries_leaf_span(parent_on):
    """命中同时给出两级区间：外层父块（读上下文），内层子块（匹配成立的范围）。

    `leaf_*` 是引用校验能不能从"格式检查"回到"证据核验"的唯一依据：只给父块
    时，模型从 500 字里随手引一句也能通过逐字校验。
    """
    text = _needle_material()
    tree = _chunk_tree(text)
    leaf = next(node for node in _leaves(tree) if NEEDLE in text[node.start:node.end])
    parent = tree[leaf.parent]

    hit = search_parent([_source(text)], NEEDLE, top_k=1)[0]
    assert (hit.start, hit.end) == (parent.start, parent.end)
    assert (hit.leaf_start, hit.leaf_end) == (leaf.start, leaf.end)
    assert NEEDLE in text[hit.leaf_start:hit.leaf_end]
    assert hit.leaf_end - hit.leaf_start < hit.end - hit.start
    # 内层区间是外层区间里的一段：偏移同源，能直接在同一份原文上切片
    assert hit.start <= hit.leaf_start < hit.leaf_end <= hit.end


def test_switch_off_keeps_leaf_fields_empty(monkeypatch):
    """开关关着时 `leaf_*` 必须是 None，而不是"与 start/end 相等的冗余字段"。

    这两个字段的语义是"匹配单元与返回单元不同"。关着时两者本来就是同一批块，
    填上去会让人以为走过两级检索；而 `model_dump()` 仍然逐键相同，只是多两个
    null——`_rag` 把它整个塞给模型，多两个 null 无害。
    """
    monkeypatch.setenv(PARENT_RETRIEVAL_ENV, "0")
    sources = _corpus()
    turned_off = search_parent(sources, "预算审批", 3)
    plain = search_sources(sources, "预算审批", 3)
    assert turned_off
    assert all(hit.leaf_start is None and hit.leaf_end is None for hit in turned_off)
    assert [hit.model_dump() for hit in turned_off] == [hit.model_dump() for hit in plain]


def test_short_parents_are_not_deduped(parent_on):
    """父块短于 `2×CHUNK_OVERLAP_CHARS` 时，两个**不同**父块都必须返回。

    这份材料里第一个父块只有 150 字（前面 450 字没有分隔符，`_pick_cut` 只能
    退到 `[MIN, TARGET)` 收尾），与第二个父块重叠 80 字 > 150 的一半。按重叠率
    去重（`_pick_ordered`）会把它们判成重复、静默丢掉一条；按父块区间去重
    （`_fold_to_parents`）不会。
    """
    text = _short_parent_material()
    parents = _parents(_chunk_tree(text))
    assert parents[0].end - parents[0].start < 2 * CHUNK_OVERLAP_CHARS

    def _hit(node):
        return EvidenceHit(source_id="s", start=node.start, end=node.end,
                           quote="", score=1.0)

    # 前提：这两个父块在重叠率判据下确实是"重复"的（否则这条用例测不到东西）
    assert _near_duplicate(_hit(parents[0]), _hit(parents[1]))

    hits = search_parent([_source(text)], "甲乙丙丁", top_k=5)
    assert {(hit.start, hit.end) for hit in hits} == {
        (parents[0].start, parents[0].end), (parents[1].start, parents[1].end)}


def test_filters_apply_before_chunking(parent_on):
    """`filters` 语义与 `search_sources` 一致：先筛来源、筛光返回空。"""
    from app.knowledge.service import SourceFilter

    kept = _source(_material())
    assert search_parent([kept], "预算审批", top_k=3,
                         filters=SourceFilter(author="张三")) == []
    assert search_parent([kept], "预算审批", top_k=3,
                         filters=SourceFilter()) == search_parent(
                             [kept], "预算审批", top_k=3)


# ---------------------------------------------------------------- 5 偏移正确性


def test_offsets_match_quote(parent_on):
    """随机抽 5 条：`text[start:end] == quote`，一条都不能错位。"""
    text = _material(repeat=4)
    source = _source(text)
    collected: list[EvidenceHit] = []
    for query in ("预算审批", "费用报销", "安全巡检", "数据备份"):
        collected.extend(search_parent([source], query, top_k=3))
    assert len(collected) >= 5, "多条查询合起来要够抽 5 条"

    for hit in random.Random(20261111).sample(collected, 5):
        assert text[hit.start:hit.end] == hit.quote
        assert hit.quote.strip()
        assert hit.end > hit.start


# ---------------------------------------------------------------- 6 开关关着等价


def _load_baseline_module(tmp_path):
    """把 `BASELINE_REV` 的 `service.py` 载成一个独立模块，并核对它的 sha256。"""
    try:
        raw = subprocess.run(
            ["git", "show", f"{BASELINE_REV}:backend/app/knowledge/service.py"],
            cwd=REPO_ROOT, capture_output=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:      # pragma: no cover
        pytest.skip(f"读不到 {BASELINE_REV} 的 service.py：{exc}")

    # rev 与内容各钉一道：只有 rev 时，改掉常量就能悄悄换基线；只有 hash 时，
    # 看不出"这份基线是哪一版"。
    assert hashlib.sha256(raw).hexdigest() == BASELINE_SHA256

    path = tmp_path / f"service_{BASELINE_REV}.py"
    path.write_bytes(raw)
    spec = importlib.util.spec_from_file_location(
        f"service_baseline_{BASELINE_REV}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _corpus() -> list[Source]:
    return [
        _source(_material()),
        _source(_needle_material()),
        _source("预算审批由部门负责人签核。"),
        _source("   "),
    ]


def _dump(hits) -> list[dict]:
    """`model_dump()` 并补齐基线没有的两个键。

    `f72c14c` 的 `EvidenceHit` 还没有 `leaf_start/leaf_end`，直接比字典会输在
    "键少了两个"上，而那与行为无关。补齐之后比的仍然是逐键逐值：当前实现若在
    关着时把 `leaf_*` 填成非 None，`==` 立刻会红。
    """
    payload = []
    for hit in hits:
        entry = hit.model_dump()
        entry.setdefault("leaf_start", None)
        entry.setdefault("leaf_end", None)
        payload.append(entry)
    return payload


def test_switch_off_matches_baseline_search_sources(tmp_path, monkeypatch):
    """开关关着时，结果与 `f72c14c` 的 `search_sources` 逐字段相同。

    对比的三方是：旧模块的 `search_sources`、当前 `search_sources`、
    当前 `search_parent`（开关=0）。少任何一方这段等价关系都不成立：
    只看后两者，`search_parent` 完全可以直接返回空列表而"与 `search_sources`
    相同"这条也测不出来（它比的是一致性，不是正确性）；少了当前
    `search_sources`，就看不出这一轮的 `_fuse` 重构有没有动过原行为。
    """
    baseline = _load_baseline_module(tmp_path)
    monkeypatch.setenv(PARENT_RETRIEVAL_ENV, "0")
    sources = _corpus()

    compared = 0
    for query in ("预算审批", "费用报销要什么材料", "紫铜铃兰"):
        for top_k in (1, 3, 5):
            expected = _dump(baseline.search_sources(sources, query, top_k))
            current = _dump(search_sources(sources, query, top_k))
            turned_off = _dump(search_parent(sources, query, top_k))
            assert current == expected, f"search_sources 变了：{query} / {top_k}"
            assert turned_off == expected, f"开关没生效：{query} / {top_k}"
            compared += 1
    assert compared == 9


def _fake_embed(texts):
    """4 维单位向量：按关键词给方向，单位向量下余弦就是点积，方便手算。"""
    vectors = []
    for text in texts:
        if "预算审批" in text:
            vectors.append([1.0, 0.0, 0.0, 0.0])
        elif "费用报销" in text or "报销" in text:
            vectors.append([0.0, 1.0, 0.0, 0.0])
        elif NEEDLE in text:
            vectors.append([0.0, 0.0, 1.0, 0.0])
        else:
            vectors.append([0.0, 0.0, 0.0, 1.0])
    return vectors


def test_switch_off_matches_baseline_with_vectors(tmp_path, monkeypatch):
    """带向量路时同样逐字段相同：退化不是"差不多"，是同一个函数。

    向量由替身给，`_vector_scores` 的缓存、融合、`method` 标注全都真的走一遍，
    只是不出网。
    """
    baseline = _load_baseline_module(tmp_path)
    monkeypatch.setenv(PARENT_RETRIEVAL_ENV, "0")
    monkeypatch.setenv("MINDMAP_HYBRID_SEARCH", "1")
    monkeypatch.setenv("MINDMAP_EMBEDDING_API_KEY", "sk-test")
    monkeypatch.setenv("MINDMAP_EMBEDDING_BASE_URL",
                       "https://maas.example.com/compatible-mode/v1")

    # 两个模块各自持有一个 `embed_texts` 名字，必须都换掉才是同一套向量
    monkeypatch.setattr(service, "embed_texts", _fake_embed)
    monkeypatch.setattr(baseline, "embed_texts", _fake_embed)

    sources = _corpus()
    for query in ("预算审批", "紫铜铃兰"):
        expected = _dump(baseline.search_sources(sources, query, 3))
        turned_off = _dump(search_parent(sources, query, 3))
        assert turned_off == expected
        assert any(hit["method"] == "hybrid" for hit in expected), "这组对比必须走过向量路"


def test_parent_retrieval_with_vectors(monkeypatch):
    """开关打开且向量路起来时，仍然返回父块；`method` 逐条标 hybrid。

    向量缓存按**子块文本**算（这是设计选择：折父块之后，父块文本从来不是打分
    单元），所以这里要确认换了打分单元之后混合路仍然自洽——`score_vector` 有值、
    区间还是父块。
    """
    monkeypatch.setenv(PARENT_RETRIEVAL_ENV, "1")
    monkeypatch.setenv("MINDMAP_HYBRID_SEARCH", "1")
    monkeypatch.setenv("MINDMAP_EMBEDDING_API_KEY", "sk-test")
    monkeypatch.setenv("MINDMAP_EMBEDDING_BASE_URL",
                       "https://maas.example.com/compatible-mode/v1")
    monkeypatch.setattr(service, "embed_texts", _fake_embed)

    text = _material()
    tree = _chunk_tree(text)
    parent_spans = {(node.start, node.end) for node in _parents(tree)}
    hits = search_parent([_source(text)], "预算审批", top_k=3)

    assert hits
    assert {hit.method for hit in hits} == {"hybrid"}
    assert {(hit.start, hit.end) for hit in hits} <= parent_spans
    assert all(hit.score_vector is not None for hit in hits)
    assert all(text[hit.start:hit.end] == hit.quote for hit in hits)
    # 同一父块只出现一次这条在混合路下同样成立
    spans = [(hit.start, hit.end) for hit in hits]
    assert len(spans) == len(set(spans))


def test_parent_switch_is_off_by_default(monkeypatch):
    """不设变量时是关的：`delenv` 之后 `search_parent` 仍等于 `search_sources`。"""
    monkeypatch.delenv(PARENT_RETRIEVAL_ENV, raising=False)
    sources = _corpus()
    assert [hit.model_dump() for hit in search_parent(sources, "预算审批", 3)] \
        == [hit.model_dump() for hit in search_sources(sources, "预算审批", 3)]
    # 只有显式真值才开：`0` / 空串 / 乱写都当关
    for value in ("0", "", "off", "False", "2"):
        monkeypatch.setenv(PARENT_RETRIEVAL_ENV, value)
        assert [hit.model_dump() for hit in search_parent(sources, "预算审批", 3)] \
            == [hit.model_dump() for hit in search_sources(sources, "预算审批", 3)]


# ---------------------------------------------------------------- 7 重叠与去重


def test_leaf_overlap_does_not_dedup_across_parents():
    """子块之间的重叠不能把**不同父块**的两条命中判成近重复。

    相邻父块本身重叠 `CHUNK_OVERLAP_CHARS`，它们的末/首子块在原文上也重叠。
    若在子块坐标上先跑一遍去重，下面这两条里较短的那个会被当成副本丢掉——
    模型少看到一整段上下文，而结果看起来完全正常（"就是少了一条"）。折父块
    这一步把坐标换成父块之后，两条在父块坐标上不重合，任何后续裁剪都不碰它们。
    """
    text = "甲" * 900
    # 父块 0 = [0, 500)，父块 1 = [400, 900)，重叠 100
    corpus = _LeafCorpus(
        items=[
            ("s", 300, 500, text[300:500]),
            ("s", 400, 550, text[400:550]),
            ("s", 550, 900, text[550:900]),
        ],
        parents=[(0, 500), (400, 900), (400, 900)],
        texts={"s": text},
    )
    left = EvidenceHit(source_id="s", start=300, end=500, quote="", score=0.04)
    right = EvidenceHit(source_id="s", start=400, end=550, quote="", score=0.05)

    # 前提：这两条在**子块坐标**下确实会被判成重复（否则这条用例测不到东西）
    assert _near_duplicate(left, right)
    # 按覆盖父块的交集算也是一样：子块坐标去重就是会误杀
    assert right.start < left.end and left.start < right.end

    folded = _fold_to_parents([right, left], corpus)
    assert [(hit.start, hit.end) for hit in folded] == [(400, 900), (0, 500)]
    # 折出来的两条在父块坐标上不重合，所以后续任何裁剪都不会误杀
    assert not _near_duplicate(folded[0], folded[1])
    assert [hit.leaf_start for hit in folded] == [400, 300]


def test_results_have_no_near_duplicates(parent_on):
    """端到端：返回的两两之间在父块坐标上不是近重复。"""
    text = _material(repeat=4) + _needle_material()
    source = _source(text)
    hits = search_parent([source], "预算审批 巡检 制度", top_k=5)
    assert hits
    for index, left in enumerate(hits):
        for right in hits[index + 1:]:
            assert not _near_duplicate(left, right)
