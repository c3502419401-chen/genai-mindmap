"""切块（`knowledge.service._chunks`）与来源内检索的行为约束。

这些断言全部围绕一件事：块必须是**原文的连续子串**。偏移一旦错位，前端拿
`text[start:end]` 做高亮就会标到别的字上，而这种错在界面上看起来"只是差了
几个字"，很难在人工验收里发现，所以只能用测试钉死。
"""
import re

import pytest

from app.contracts.source import Source, SourceKind
from app.knowledge.service import (
    CHUNK_MAX_CHARS,
    CHUNK_MIN_CHARS,
    CHUNK_OVERLAP_CHARS,
    CHUNK_TARGET_CHARS,
    EvidenceHit,
    _bm25_scores,
    _chunk_spans,
    _chunks,
    _merge_short,
    _near_duplicate,
    search_sources,
)


@pytest.fixture(autouse=True)
def _lexical_only(monkeypatch):
    """这些断言全部关于切块与**词面**排序，不该被向量路影响。

    凭证与开关的清理统一在 `conftest.py` 的全局 fixture 里（那里清的是
    `MINDMAP_EMBEDDING_API_KEY` 与回退的 `DASHSCOPE_API_KEY` 两个，只清后者
    正是这个文件上一版出问题的地方）。这里再**显式**关一次向量路，是为了把
    "本文件依赖纯 BM25"写死：哪天 conftest 的默认值变了，这几条会立刻红，
    而不是悄悄换一种分数口径。融合逻辑由 `test_hybrid_search.py` 用 mock 覆盖。
    """
    monkeypatch.setenv("MINDMAP_HYBRID_SEARCH", "0")

PARAGRAPH = (
    "今天上午，我去“秘密基地”了。那里堆着三箱旧书（详见附录 A），"
    "书脊上全是灰。……翻到第七本时，我发现了那张折起来的纸。\n"
)


def _document(repeat: int = 20) -> str:
    """带章节标题、段落、引号、括号、省略号的中文长文。"""
    parts = ["# 第一章 总则\n\n"]
    for index in range(repeat):
        parts.append(f"## 小节{index}\n\n{PARAGRAPH}")
    return "".join(parts)


def _sentences(repeat: int = 30) -> str:
    """只有句号的连续文本，用来单独检验"块尾落在句末标点"。"""
    return "山里的雾散得很慢。石阶上还留着昨夜的雨水。他没有回头。" * repeat


def _boundaries(text: str) -> set[int]:
    edges: set[int] = set()
    for start, end, _ in _chunks(text):
        edges.add(start)
        edges.add(end)
    return edges


def test_offsets_match_source_text():
    """任何块的 text[start:end] 都必须逐字等于 quote。"""
    text = _document()
    chunks = list(_chunks(text))
    assert len(chunks) >= 3
    for start, end, quote in chunks:
        assert text[start:end] == quote
        assert end > start


def test_every_char_is_covered():
    """按字符位置打覆盖标记：原文每一位都至少落在一个块里。"""
    text = _document()
    chunks = list(_chunks(text))
    covered = bytearray(len(text))
    for start, end, _ in chunks:
        covered[start:end] = b"\x01" * (end - start)
    assert all(covered)
    assert chunks[0][0] == 0
    assert chunks[-1][1] == len(text)
    # 相邻块只能是相接或重叠，中间不能有空洞
    for (_, prev_end, _), (next_start, _, _) in zip(chunks, chunks[1:]):
        assert next_start <= prev_end


def test_chunk_lengths_stay_within_bounds():
    assert CHUNK_MIN_CHARS < CHUNK_TARGET_CHARS < CHUNK_MAX_CHARS
    chunks = list(_chunks(_document()))
    for _, end, quote in chunks[:-1]:
        assert CHUNK_MIN_CHARS <= len(quote) <= CHUNK_MAX_CHARS
    # 末块允许略超上限：切块前提是"剩余 > MAX"，701 字会切出 1 字末块，
    # 宁可让末块多出几百字符，也不要留一个没有任何信息的碎块。
    assert 0 < len(chunks[-1][2]) <= CHUNK_MAX_CHARS + CHUNK_MIN_CHARS


def test_tiny_tail_is_absorbed():
    """`"字"*701` 只切出 1 字末块 —— 那块必须并回去，否则它会独占一个 top_k。"""
    assert _chunk_spans("字" * 701) == [(0, 701)]


def test_chunks_end_on_sentence_boundary():
    """块尾必须把句子收完整（不 strip，所以收在标点上而不是标点后）。"""
    text = _sentences()
    chunks = list(_chunks(text))
    assert len(chunks) >= 2
    for _, end, _ in chunks[:-1]:
        assert text[end - 1] in "。！？；…\n"


def _paired_spans(text: str, opener: str, closer: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in re.finditer(
        f"{re.escape(opener)}[^{re.escape(closer)}]*{re.escape(closer)}", text)]


def test_quotes_never_split():
    """没有任何块边界落在“”之间，包括引号横跨整个搜索窗口的情形。"""
    for text in (_document(), "雾" * 650 + "“" + "句号。这里。" * 40 + "”" + "收尾。" * 20):
        edges = _boundaries(text)
        spans = _paired_spans(text, "“", "”")
        assert spans, "语料里必须有引号，否则这条断言没有意义"
        for start, end in spans:
            inside = set(range(start + 1, end))
            assert not edges & inside, (text[:40], start, end, edges & inside)


def test_brackets_never_split():
    """（详见附录 A）这种括注不被切开，且整段括注落在同一块里。"""
    open_at = 695
    text = "字" * open_at + "（详见附录 A）" + "正文。" * 100
    bracket_end = text.index("）") + 1
    edges = _boundaries(text)
    assert not edges & set(range(open_at + 1, bracket_end))
    chunks = list(_chunks(text))
    assert any(start <= open_at and end >= bracket_end for start, end, _ in chunks)


def test_punctuation_free_run_is_hard_cut():
    """2000 字无标点也必须切进上限内，且不丢任何字。"""
    text = "字" * 2000
    chunks = list(_chunks(text))
    assert len(chunks) >= 3
    for start, end, quote in chunks:
        assert 0 < end - start <= CHUNK_MAX_CHARS
        assert text[start:end] == quote
    assert chunks[0][0] == 0 and chunks[-1][1] == len(text)
    covered = bytearray(len(text))
    for start, end, _ in chunks:
        covered[start:end] = b"\x01" * (end - start)
    assert all(covered)


def test_short_neighbour_is_merged():
    """80 字 + 400 字并成一块；合并后超上限时不许硬并。"""
    text = "字" * 80 + "字" * 400
    chunks = list(_chunks(text))
    assert [(start, end) for start, end, _ in chunks] == [(0, 480)]
    assert _merge_short([(0, 80), (80, 480)]) == [(0, 480)]
    # 末块合并的上限放宽到 MAX+MIN；900 已越过它，不许硬并。
    assert _merge_short([(0, 700), (700, 900)]) == [(0, 700), (700, 900)]


def test_markdown_heading_starts_chunk():
    """切点落在 ## 之前：标题跟着它下面的正文，而不是孤立在上一块末尾。"""
    text = "正文。" * 300 + "\n## 小节\n\n" + "正文。" * 300
    heading_at = text.index("## 小节")
    chunks = list(_chunks(text))
    assert heading_at in {end for _, end, _ in chunks}
    assert text[heading_at - 1] == "\n"
    assert any("## 小节\n\n正文。" in quote for _, _, quote in chunks)


def test_ideographic_space_lines():
    """全角空格开头的行不产生空块，也不把偏移带歪。"""
    text = "第一段正文。\n\u3000\u3000缩进的第二段。\n\u3000\n第三段。\n" * 40
    chunks = list(_chunks(text))
    assert len(chunks) >= 2
    for start, end, quote in chunks:
        assert quote and not quote.isspace()
        assert text[start:end] == quote
    assert any("\u3000\u3000缩进" in quote for _, _, quote in chunks)


def test_empty_and_tiny_text():
    """空文本与极短文本不崩，各返回单块。"""
    assert list(_chunks("")) == [(0, 0, "")]
    assert list(_chunks("短")) == [(0, 1, "短")]
    tiny = "字" * (CHUNK_MIN_CHARS - 1)
    assert list(_chunks(tiny)) == [(0, len(tiny), tiny)]


def test_overlap_lands_on_sentence_start():
    """相邻块的重叠量不超过设定值，且新块的起点落在句首/行首。"""
    text = _sentences()
    chunks = list(_chunks(text))
    for (_, prev_end, _), (next_start, _, _) in zip(chunks, chunks[1:]):
        gap = prev_end - next_start
        assert 0 <= gap <= CHUNK_OVERLAP_CHARS
        assert text[next_start - 1] in "。！？；…\n"


def test_ascii_words_and_urls_never_split():
    """ASCII 单词、数字串、URL 内部不切。"""
    url = "https://example.com/docs/v2.0.1/release-notes"
    text = (
        "Our retriever does not normalize case or strip markup. " * 20
        + f"见 {url} 与版本 v2.0.1 的说明。" + "补充说明。" * 40
    )
    chunks = list(_chunks(text))
    edges = {start for start, _, _ in chunks} | {end for _, end, _ in chunks}
    for edge in edges:
        if 0 < edge < len(text):
            before, after = text[edge - 1], text[edge]
            assert not (before.isascii() and before.isalnum()
                        and after.isascii() and after.isalnum()), text[edge - 8:edge + 8]
    assert any(url in quote for _, _, quote in chunks)


def test_near_duplicate_filter():
    """重叠超过较短者一半的命中算重复；跨来源不算，轻微重叠也不算。"""
    base = EvidenceHit(source_id="s", start=0, end=100, score=0.5, quote="")
    heavy = EvidenceHit(source_id="s", start=20, end=120, score=0.5, quote="")
    light = EvidenceHit(source_id="s", start=80, end=180, score=0.5, quote="")
    other = EvidenceHit(source_id="t", start=0, end=100, score=0.5, quote="")
    assert _near_duplicate(base, heavy)
    assert not _near_duplicate(base, light)
    assert not _near_duplicate(base, other)


def test_search_results_carry_no_near_duplicates():
    """检索结果里不会出现两条区间几乎重合的证据，且 top_k 仍然生效。"""
    source = Source(kind=SourceKind.text, status="parsed", text=_document())
    hits = search_sources([source], "秘密基地的旧书", top_k=5)
    assert hits
    for index, left in enumerate(hits):
        for right in hits[index + 1:]:
            assert not _near_duplicate(left, right)
    assert len(search_sources([source], "秘密基地的旧书", top_k=1)) == 1


def test_score_is_bm25():
    """分数是 BM25 分，可按同一次检索的块集逐块复算。

    旧断言核对的是 `bigram 交集 / 查询词数`；换成 BM25 之后这个公式不再成立，
    但"分数必须能从块本身复算出来"这条要求没变——分数一旦不可复算，排序就
    只能靠观察结果倒推，回归时无从判断哪一步变了。
    """
    source = Source(
        kind=SourceKind.text, status="parsed", text="会议决定十月发布二点零版本。")
    hits = search_sources([source], "十月发布")
    assert hits
    quotes = [quote for _, _, quote in _chunks(source.text or "")]
    expected = _bm25_scores("十月发布", quotes)
    for hit in hits:
        assert hit.method == "lexical"          # 没配凭证时只走词面路
        assert hit.score_vector is None
        assert hit.score == round(hit.score_bm25, 6)
        assert hit.score == round(expected[quotes.index(hit.quote)], 6)
    assert source.id == hits[0].source_id
    assert source.text[hits[0].start:hits[0].end] == hits[0].quote


def test_spans_are_ordered_and_non_empty():
    """区间列表本身自洽：严格递增的起点、递增的终点。"""
    spans = _chunk_spans(_document())
    assert spans[0][0] == 0
    for (_, prev_end), (next_start, next_end) in zip(spans, spans[1:]):
        assert next_start <= prev_end
        assert next_end > next_start
