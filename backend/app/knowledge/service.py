"""知识检索（F12 / F13 / F14）。

位置：agent 循环里由 `rag_query` 工具按需调用（`app/agent/tools.py::_rag()`），
用检索结果给模型补上下文，不进导图契约。

两条线，互不影响：
  - `search()` / `KnowledgeHit`：面向向量库的检索桩，仍然返回空列表（未接）
  - `search_sources()` / `EvidenceHit`：只在本**次请求带来的来源**里定位片段，
    这是 `rag_query` 真正走的路，也是本文件的主体

`search_sources` 是混合检索：
  1. BM25 词面路（中文 bigram + 英文词），在本次切出的全部块上算 IDF
  2. 向量路（DashScope text-embedding-v3，按块文本哈希缓存）
  3. RRF 融合两路的**名次**，不做分数归一化

降级是常态而不是异常：没有 `DASHSCOPE_API_KEY`、接口超时、向量全零，都会
安静地退成单路 BM25，结果照样返回、`method` 标明走的是哪条路。

注意：向量检索解决的是"找到相关片段"，不是"判断内容正确"。
F13 的验收不能被等同于事实验证。
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
from collections.abc import Iterator
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.source import Source
from app.knowledge import vector_cache
from app.knowledge.embedding import (
    EmbeddingError,
    EmbeddingNotConfigured,
    configured as embedding_configured,
    embed_texts,
    model_name as embedding_model,
)

logger = logging.getLogger(__name__)


class KnowledgeHit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str | None = None
    snippet: str = ""
    score: float = 0.0


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)


class SearchResponse(BaseModel):
    hits: list[KnowledgeHit] = Field(default_factory=list)
    implemented: bool = False


def search(query: str, top_k: int = 5) -> list[KnowledgeHit]:
    """检索知识库。

    TODO(F12/F13)：
      - 建索引：文档 → 分块 → 嵌入 → 存向量列
      - 检索：query → 嵌入 → 相似度排序 → 返回片段 + 来源定位
    现在返回空列表，调用方能正常跑完流程，但不会得到任何增强。
    """
    _ = (query, top_k)
    return []


# ---------------------------------------------------------------- 本次来源内检索


class EvidenceHit(BaseModel):
    """检索到的一段原文。

    `start` / `end` 是 **Unicode 码点**偏移，与 `SourceRef` 同口径。用 UTF-16 下标
    会在中文和 emoji 上错位，前端拿它 slice 出来的就是另一段文字。

    三个新字段都带默认值，`model_dump()` 只多出键、不改既有键的形状——`_rag`
    把它整个塞进 JSON 回给模型，多两个数字和一个小写字符串对模型无害。
    """

    model_config = ConfigDict(extra="forbid")
    source_id: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    quote: str
    # 融合后的最终分。混合检索时是 RRF 分，单路时是 BM25 分（或余弦）。
    # **只在同一次检索内可比**：IDF 与名次都依赖这次请求切出的块集合。
    score: float
    # 两路的原始分，供调试与将来调权。没走那一路时是 None，不是 0——0 是
    # "走了但得 0 分"，None 是"根本没参与"，两者对排查的含义完全不同。
    score_bm25: float | None = None
    score_vector: float | None = None
    # 这条结果实际由哪条路支撑。`_rag` 的 data["method"] 是请求级的旧字段，
    # 逐条标注才能看出"这条是纯语义捞回来的、字面一个词都不沾"。
    method: Literal["lexical", "hybrid"] = "lexical"


# ---------------------------------------------------------------- 切块参数
#
# 词面检索只关心"哪一段包含查询里的字组"，所以切块的目标是让每块读起来是
# 完整的句子群，而不是精确控制长度：命中后回给模型的是整块原文，块太大就
# 开始跑题，块太小又缺上下文。
# 目标长度：正常情况下块长的落点。`_pick_cut` 先在 [TARGET, MAX] 里取最靠前的
# 边界，只有 TARGET 之后整段都切不动（一个三百字的长句），才退回 [MIN, TARGET)
# 取最靠后的。所以实测块长围绕 TARGET 波动，而不是贴着 MAX。
CHUNK_TARGET_CHARS = 450
CHUNK_MAX_CHARS = 700      # 硬上限：再大，命中回给模型的原文会稀释查询
CHUNK_MIN_CHARS = 150      # 低于它向后合并：一句话单独成块不值一个 top_k
# 相邻块重叠 100 码点（中文约 2–3 句、英文约 0.9 句）。词面检索其实不需要它——
# 下一步要换向量检索，跨块的因果句和"定义—被定义"一旦被切断，语义检索就永远
# 找不回来；届时补重叠要重切全部材料并重建索引，代价远大于现在留。
# 60 在英文 PDF 上实测只有 0.37 句、且 45% 的相邻对重叠为零（切点常落在行尾，
# 吸附窗口够不到上一句），对向量检索等于没留。100 是折中：覆盖一个英文句，
# 又不至于把块数推高太多。
CHUNK_OVERLAP_CHARS = 100
# 重叠起点向后再吸附到句首时允许走的最大码点数。超过它还没找到句首就用
# 定长回退位置：宁可起点落在句中，也不要把重叠吃掉大半。
# 取值按"英文一个句子有多长"定：中文句子 20–30 字，40 够；英文常见 50–60 字符，
# 窗口太小时吸附经常失败、回退位置落在半句中间——那正是这段代码要避免的。
# 吸附窗口放大只会让重叠变小（上限仍是 end-1），起点却总能落在句首。
_OVERLAP_SNAP_CHARS = 100

# 分隔符优先级：从高到低，命中后切点落在匹配的**末尾之后**（标题行除外）。
# 结构标题行首——切点是标题行的起点，标题要跟下面的正文同块，否则块首是个
# 孤立标题、正文丢了所属章节。
_HEADING_RE = re.compile(
    r"^(?:"
    r"#{1,6}\s"                              # Markdown：# / ## 小节
    r"|第[一二三四五六七八九十百]+[章节条]"    # 第3章 / 第一节 / 第十二条
    r"|[一二三四五六七八九十]+、"             # 一、二、
    r"|（[一二三四五六七八九十]+）"           # （一）（二）
    r"|\d+(?:\.\d+)*[\s、.]"                 # 1. / 2、 / 3.1
    r")",
    re.MULTILINE,
)
# 空行——段落边界。\s 覆盖全角空格 \u3000
_BLANK_LINE_RE = re.compile(r"\n\s*\n")
# 单个换行——列表项、表格行、不空行分段的文档
_NEWLINE_RE = re.compile(r"\n")
# 句末标点，可带紧跟的闭引号/闭括号（。”）这种连续闭符号一起收，免得下一块
# 以孤零零的 "）" 开头）；连续的……由一个 + 号吃成一个匹配
_SENTENCE_END_RE = re.compile(
    r"[。！？；…]+[”’」』）】》]*"
    # 英文句末。两个限定缺一不可：`.` 后面必须跟空白（否则会把 v2.0.1、文件名、
    # 小数切开），`.` 前面不能是数字（否则 2.0 这种版本号/小数照样会被切）。
    # 缩写（Mr.、e.g.）仍会被当成句末——中文材料里罕见，不值得为它加词表。
    r"|(?<![0-9])[.!?]\s+"
)
# 次级标点——冒号后常是列举，逗号顿号后是并列项
_SUBCLAUSE_END_RE = re.compile(r"[：，、]")
# 重叠起点的句首吸附：句末标点之后、或换行之后，且后面不是空白
_SENTENCE_START_RE = re.compile(
    r"(?:[。！？；…]+[”’」』）】》]*|\n)(?=\S)"
    r"|(?<![0-9])[.!?]\s+(?=\S)"
)

# 成对符号。不切在它们内部，否则两块各带半截引号。
_OPEN_TO_CLOSE = {
    "“": "”", "‘": "’", "「": "」", "『": "』",
    "（": "）", "【": "】", "《": "》", "〈": "〉",
    "(": ")", "[": "]",
}
_CLOSE_TO_OPEN = {close: opener for opener, close in _OPEN_TO_CLOSE.items()}

# 数字串 / ASCII 单词 / URL。URL 分支必须排最前，否则 http 会先被普通单词
# 分支吃掉，域名部分就落到保护范围之外了。
_TOKEN_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9+.\-]*://\S+"
    r"|www\.[A-Za-z0-9.\-]+\S*"
    r"|[A-Za-z0-9]+(?:[._%+\-:/][A-Za-z0-9]+)*"
)


def _unsafe_boundaries(text: str) -> bytearray:
    """标出不能切的位置：`结果[i]` 表示"切在 text[i-1] 与 text[i] 之间"不安全。

    两类不安全：成对符号内部（“秘密基地” 从中间切开，两块各带半截引号）、
    URL / 数字串 / ASCII 单词内部（v2.0.1 切成 v2.0 和 .1）。
    用差分数组打区间标记：成对符号可能跨越很长，逐位置写会退化成 O(n²)。
    """
    length = len(text)
    delta = [0] * (length + 2)
    opened: dict[str, list[int]] = {}
    for index, char in enumerate(text):
        if char in _OPEN_TO_CLOSE:
            opened.setdefault(char, []).append(index)
            continue
        opener = _CLOSE_TO_OPEN.get(char)
        if opener is None:
            continue
        positions = opened.get(opener)
        if not positions:
            continue                      # 孤立闭符号：没成对，不设限
        open_index = positions.pop()
        delta[open_index + 1] += 1
        delta[index + 1] -= 1
    for match in _TOKEN_RE.finditer(text):
        if match.end() - match.start() > 1:
            delta[match.start() + 1] += 1
            delta[match.end()] -= 1
    mask = bytearray(length + 1)
    depth = 0
    for index in range(length + 1):
        depth += delta[index]
        if depth > 0:
            mask[index] = 1
    return mask


def _scan_cut(
    text: str, pos: int, low: int, high: int, mask: bytearray, *, prefer: str,
) -> int:
    """在 [low, high] 内按分隔符优先级找切点。

    `prefer="first"` 取同级最靠前的（块长贴近 TARGET），`"last"` 取最靠后的
    （块长贴近窗口上沿）。优先级仍高于位置偏好：高一级的边界永远赢。
    """
    for pattern, at_start in (
        (_HEADING_RE, True),
        (_BLANK_LINE_RE, False),
        # 句末标点排在单换行**之前**。PDF 提取出来的文本几乎每行都以 \n 结尾，
        # 而这个 \n 是版面折行、不是句子边界：若优先按行切，块尾只有两成落在
        # 句末（实测 arXiv 1706.03762：60/80 次切点来自单换行，块尾句末率 20%）。
        # 只有真正成对的「空行」才是段落边界，那一级留在上面。
        # 代价：英文单字母缩写（A. Smith）会被当成句末，实测误切 0.4%。
        (_SENTENCE_END_RE, False),
        (_NEWLINE_RE, False),
        (_SUBCLAUSE_END_RE, False),
    ):
        cut = 0
        # 从 low-1 起搜：正好跨在 low 上的空行 "\n\n" 也要能命中
        for match in pattern.finditer(text, max(pos, low - 1), high):
            candidate = match.start() if at_start else match.end()
            if low <= candidate <= high and candidate > pos and not mask[candidate]:
                if prefer == "first":
                    return candidate
                cut = candidate        # 匹配按位置递增，最后命中的即最靠后的
        if cut:
            return cut
    return 0


def _pick_cut(text: str, pos: int, mask: bytearray) -> int:
    """挑切点：先在 [TARGET, MAX] 里取最靠前的，退一步在 [MIN, TARGET) 里取最靠后的。

    两个窗口的顺序就是"块长优先"的意思：目标窗口里有边界时用最靠前的那个，
    块长落在 TARGET 附近；只有 TARGET 之后整段都没有可切之处（一个 300 字的
    长句），才允许在 TARGET 之前收尾，把块压到 MIN 与 TARGET 之间。
    反过来先找"最靠后"会让每块都贴着 MAX——那样 TARGET 就是一句空话。
    """
    target = min(pos + CHUNK_TARGET_CHARS, len(text))
    high = min(pos + CHUNK_MAX_CHARS, len(text))
    low = pos + CHUNK_MIN_CHARS

    cut = _scan_cut(text, pos, target, high, mask, prefer="first")
    if cut:
        return cut
    cut = _scan_cut(text, pos, low, max(low, target - 1), mask, prefer="last")
    if cut:
        return cut
    return _hard_cut(low, high, mask)


def _hard_cut(low: int, high: int, mask: bytearray) -> int:
    """兜底硬切：切在 high；正落在 URL / 数字串 / 成对符号内部就往前挪。

    一直退到窗口下限 low 为止：安全规则的优先级高于块长，而退到 low 时块长
    仍等于 MIN，没有破坏长度约束。整窗都在同一对符号或同一个巨型 token 里时
    照切 high——这时没有安全位置可选，覆盖全文和"每块不超过 MAX"必须保住。
    """
    for candidate in range(high, low - 1, -1):
        if not mask[candidate]:
            return candidate
    return high


def _overlap_start(text: str, start: int, end: int, mask: bytearray) -> int:
    """下一块的起点：从 end - OVERLAP 向后吸附到最近的句首。

    最多前进 `_OVERLAP_SNAP_CHARS` 个码点；超过它还没找到句首，就直接不重叠
    （见下面 return end 那段）。
    """
    base = max(start + 1, end - CHUNK_OVERLAP_CHARS)
    limit = min(end - 1, base + _OVERLAP_SNAP_CHARS)
    # endpos 放宽两个码点：句首吸附要看见 limit 之后那个字符是不是空白
    for match in _SENTENCE_START_RE.finditer(text, base - 1, limit + 2):
        candidate = match.end()
        if base <= candidate <= limit and not mask[candidate]:
            return candidate
    # 没有句首可吸附：直接不重叠，起点就用 end。
    # 不退回 `base` 或"最近的安全位置"——那会落在半句中间（英文里尤其常见：
    # end 本身就是句首时，候选句首正好等于 end、被 limit 排除在外，回退就把
    # 起点丢到 `3.2.1` 这种数字串中间）。块首是半句话，检索命中后回给模型的
    # 原文读起来就是断的，比少重叠 60 个字糟得多。
    return end


def _too_short(span: list[int]) -> bool:
    return span[1] - span[0] < CHUNK_MIN_CHARS


def _merge_short(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """自左向右把过短的块并进后一块；末块过短则并进前一块。

    只动 start/end，不重排文本。贪心切点产出的块长天然 ≥ MIN，所以向左合并
    这条在当前参数下基本是防御性的。真正会触发的是**末块**：切块循环的前提是
    "剩余 > MAX"，于是 `"字"*701` 会切出 `(0,700) + (700,701)`，末块只剩一个字。
    那种块占掉一个 top_k 位置却什么信息都没有，所以末块合并允许结果略超 MAX
    （上限放宽到 MAX+MIN）：宁可最后一块多出几百字符，也不要留一个碎块。
    """
    merged: list[list[int]] = []
    for start, end in spans:
        if (
            merged
            and _too_short(merged[-1])
            and end - merged[-1][0] <= CHUNK_MAX_CHARS
        ):
            merged[-1][1] = end
            continue
        merged.append([start, end])
    if len(merged) > 1 and _too_short(merged[-1]):
        if merged[-1][1] - merged[-2][0] <= CHUNK_MAX_CHARS + CHUNK_MIN_CHARS:
            merged[-2][1] = merged[-1][1]
            merged.pop()
    return [(span[0], span[1]) for span in merged]


def _chunk_spans(text: str) -> list[tuple[int, int]]:
    """按语义边界切出的 [start, end) 区间：顺序排列、可重叠、覆盖全文。

    覆盖的判据是 `首块.start == 0`、`末块.end == len(text)`、相邻块
    `下一块.start <= 上一块.end`——有重叠时不再是首尾严格相接。
    """
    length = len(text)
    if length == 0:
        return [(0, 0)]
    mask = _unsafe_boundaries(text)
    spans: list[tuple[int, int]] = []
    start = 0
    while length - start > CHUNK_MAX_CHARS:
        end = _pick_cut(text, start, mask)
        spans.append((start, end))
        start = _overlap_start(text, start, end, mask)
    spans.append((start, length))
    return _merge_short(spans)


def _chunks(text: str) -> Iterator[tuple[int, int, str]]:
    """切块，产出 (start, end, 文本)。

    偏移是**原文的码点下标**，`text[start:end]` 逐字等于 quote：调用方拿它
    去切 `Source.text` 做界面高亮，偏一位就标错字。所以这里既不 strip、也不
    改写任何字符，块就是原文的连续子串。
    """
    for start, end in _chunk_spans(text):
        yield start, end, text[start:end]


# ---------------------------------------------------------------- 混合检索参数

# `MINDMAP_HYBRID_SEARCH=0` 关掉向量路，只走 BM25。默认开启：缺凭证时它自己
# 就退成单路了，没有理由再让用户多配一个开关才能用。
HYBRID_ENV = "MINDMAP_HYBRID_SEARCH"

# RRF 的平滑常数。取 60 是原论文的经验值：它压住"名次 1 和名次 2 的差距"，
# 让两路都有名次的块靠"两票"胜出，而不是靠某一路的第一名通吃。
RRF_K = 60

# 两路各取 top_k * 3 个候选再融合。放大 3 倍是为了接住"只在一路靠前"的结果：
# 某块 BM25 排第 12、向量排第 2，只取 top_k 的话它根本进不了候选集。
CANDIDATE_FACTOR = 3

# 查询短于这个长度就不走向量路。两个字符的查询在语义上没有稳定方向，
# 却要为一个大概率噪声的向量付一次 HTTP 往返。
MIN_VECTOR_QUERY_CHARS = 2

# 与 FTS5 的默认参数一致，方便将来拿 SQLite 的 bm25() 交叉验算。
BM25_K1 = 1.2
BM25_B = 0.75

_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def _bigram_terms(segment: str) -> list[str]:
    """中文段的二元组，**保留重复与顺序**。取代了原来的集合版 `_bigrams`。

    中文没有空格可分词，按单字切又太碎（重合率虚高，几乎什么都能命中），
    二元组是两者之间最省事的折中：不引入分词依赖，区分度也够用。

    换成列表而不是继续用 set，是因为 BM25 要算词频——set 会把同一段里的两次
    出现压成一次，长块就白长了。空白先剔除：`\\n\\n` 这种二元组分不出任何
    信息，却会实打实抬高块长、让长度归一化把有内容的块压低。
    """
    chars = [char for char in segment if not char.isspace()]
    if len(chars) < 2:
        return chars
    return [chars[index] + chars[index + 1] for index in range(len(chars) - 1)]


def _token_list(text: str) -> list[str]:
    """BM25 的 token 序列：中文 bigram + 英文/数字词（小写）。

    ASCII 词整体成一个 token，不按 bigram 切：`retriever` 切出来是
    re/et/tr/ri… 一串碎片，彼此竞争 IDF，还会和真词的二元组混在同一个词表里。
    按段切开再分别处理，也顺手避免了跨字符集边界的 `r中` 这种 token。
    """
    tokens: list[str] = []
    cursor = 0
    for match in _ASCII_WORD_RE.finditer(text):
        tokens.extend(_bigram_terms(text[cursor:match.start()]))
        tokens.append(match.group(0).lower())
        cursor = match.end()
    tokens.extend(_bigram_terms(text[cursor:]))
    return tokens


def _hybrid_enabled() -> bool:
    return os.getenv(HYBRID_ENV, "1").strip().lower() not in {"0", "off", "false", "no"}


def _bm25_scores(query: str, quotes: list[str]) -> list[float]:
    """对本次请求切出的所有块算 BM25。

    替换掉原来的 `交集 / 查询词数`：那个公式没有 IDF、也没有长度归一化，
    结果是"常见字组虚高、长块吃亏"。BM25 两者都有：

        idf(t) = log(1 + (N - df(t) + 0.5) / (df(t) + 0.5))
        score  = Σ idf(t) · tf(t,d)·(k1+1) / (tf(t,d) + k1·(1 - b + b·|d|/avgdl))

    `N` 是**本次请求**切出的块数，不是全局语料。没有全局索引，就不假装有：
    df 在这个集合上算，N 也是这个集合的大小。代价是分数只在本次检索内可比
    （见 `search_sources` 的 docstring），收益是永远不需要重建统计。

    `+1` 在 log 里面，所以 idf 恒为正——`N=1, df=1` 时也不是 0，单块文档
    照样能排序。
    """
    total = len(quotes)
    if total == 0:
        return []
    # 去重但保留首次出现顺序：查询里重复的字只会让同一个 term 加两次分
    terms = list(dict.fromkeys(_token_list(query)))
    if not terms:
        return [0.0] * total

    documents = [_token_list(quote) for quote in quotes]
    average = sum(len(document) for document in documents) / total or 1.0

    counts: list[dict[str, int]] = []
    # 只为查询词统计 df：块里其余 token 永远不进打分公式，逐项统计纯属浪费
    document_frequency = dict.fromkeys(terms, 0)
    for document in documents:
        counter: dict[str, int] = {}
        for token in document:
            counter[token] = counter.get(token, 0) + 1
        counts.append(counter)
        for term in terms:
            if term in counter:
                document_frequency[term] += 1

    scores: list[float] = []
    for counter, document in zip(counts, documents):
        length = len(document)
        score = 0.0
        for term in terms:
            frequency = counter.get(term, 0)
            if not frequency:
                continue
            df = document_frequency[term]
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            score += idf * frequency * (BM25_K1 + 1) / (
                frequency + BM25_K1 * (1 - BM25_B + BM25_B * length / average))
        scores.append(score)
    return scores


def _bm25_ranking(scores: list[float], lengths: list[int]) -> dict[int, int]:
    """名次（1 起），只收正分的块；同分时短块优先。"""
    order = sorted((index for index, score in enumerate(scores) if score > 0),
                   key=lambda index: (-scores[index], lengths[index]))
    return {index: rank for rank, index in enumerate(order, 1)}


def _vector_ranking(scores: dict[int, float], lengths: list[int]) -> dict[int, int]:
    order = sorted(scores, key=lambda index: (-scores[index], lengths[index]))
    return {index: rank for rank, index in enumerate(order, 1)}


def _rrf_value(index: int, bm25: dict[int, int], vector: dict[int, int]) -> float:
    """RRF：只累加两路名次的倒数，两路的分数口径不参与。

    BM25 无上界、余弦在 [-1,1]，加权求和必须先归一化，而归一化要假设两种
    分数的分布可比——那个假设在 3 个块和 300 个块的请求里都不成立。名次是
    唯一在两种量纲下都同义的量，所以融合的输入只取名次。
    """
    total = 0.0
    if index in bm25:
        total += 1.0 / (RRF_K + bm25[index])
    if index in vector:
        total += 1.0 / (RRF_K + vector[index])
    return total


def _vector_scores(quotes: list[str], query: str) -> tuple[dict[int, float], str]:
    """算每块与查询的余弦相似度；返回 ({块下标: 相似度}, 不可用原因)。

    向量已由 `embed_texts` 做过 L2 归一化，所以余弦就是点积。缓存按块文本
    哈希走，命中的块不再花额度——同一份材料在一次会话里被 `rag_query` 反复
    检索，是这条路上最常见的重复。

    原因分四种，因为后续处理不同：`unconfigured`（缺 key，或 openai 协议下
    缺 base）是预期状态，不记日志；其余三种各留一条，否则语义路为什么没起来
    就只能靠猜。
    """
    if not embedding_configured():
        return {}, "unconfigured"

    started = time.perf_counter()
    model = embedding_model()
    hashes = [vector_cache.text_hash(quote) for quote in quotes]
    query_hash = vector_cache.text_hash(query)

    cached = vector_cache.get_vectors([*hashes, query_hash], model)
    misses = [(hash_value, quote) for hash_value, quote in zip(hashes, quotes)
              if hash_value not in cached]
    needs_query = query_hash not in cached
    # 查询与缺失的块合成一次批量：少一次往返，也少一次重试的退避等待
    pending = [quote for _, quote in misses] + ([query] if needs_query else [])
    if pending:
        try:
            vectors = embed_texts(pending)
        except EmbeddingNotConfigured:
            # 预检与调用之间凭证被清掉。并发下真会发生，不算故障。
            return {}, "unconfigured"
        except EmbeddingError as exc:
            logger.warning("向量检索降级为纯 BM25：%s",
                           f"{type(exc).__name__}：{exc}"[:160])
            return {}, "error"
        fresh: dict[str, list[float]] = {}
        for (hash_value, _), vector in zip(misses, vectors):
            if vector:
                cached[hash_value] = vector
                fresh[hash_value] = vector
        if needs_query:
            query_vector = vectors[len(misses)] if len(vectors) > len(misses) else None
            if query_vector:
                cached[query_hash] = query_vector
                fresh[query_hash] = query_vector
        if fresh:
            vector_cache.put_vectors(list(fresh.items()), model)

    query_vector = cached.get(query_hash)
    if not query_vector:
        logger.warning("向量检索降级为纯 BM25：查询向量缺失")
        return {}, "query_failed"

    similarity: dict[int, float] = {}
    for index, hash_value in enumerate(hashes):
        vector = cached.get(hash_value)
        if not vector or len(vector) != len(query_vector):
            # 维度对不上（换了模型但缓存没清）就跳过，绝不截断对齐
            continue
        value = sum(left * right for left, right in zip(vector, query_vector))
        # 余弦 ≤ 0 表示正交或反向：字面无关、语义也无关，不该占一个名次
        if value > 0:
            similarity[index] = value

    # 只记条数与耗时。块文本是材料原文，一律不进日志。
    logger.info("向量路：块 %d / 有向量 %d / 缓存命中 %d / 耗时 %.0fms",
                len(quotes), len(similarity), len(hashes) + 1 - len(pending),
                (time.perf_counter() - started) * 1000)
    return (similarity, "") if similarity else ({}, "no_signal")


def _near_duplicate(left: EvidenceHit, right: EvidenceHit) -> bool:
    """同一来源里，两条命中区间重叠超过较短者 50% 就算近重复。

    相邻块有 `CHUNK_OVERLAP_CHARS` 码点重叠，同一句话常能同时命中前后两块；
    不去重的话 top_k=5 会被同一条证据的副本占掉两格，真正不同的证据反而挤不进来。
    跨来源的区间是不同文档的坐标，不能拿来比。
    """
    if left.source_id != right.source_id:
        return False
    overlap = min(left.end, right.end) - max(left.start, right.start)
    if overlap <= 0:
        return False
    return overlap * 2 > min(left.end - left.start, right.end - right.start)


def _pick_top(hits: list[EvidenceHit], top_k: int) -> list[EvidenceHit]:
    """排序 → 去掉近似重复 → 截断 top_k。

    抽成独立函数是为了能单独测：切块器的重叠只来自"重叠起点吸附到句首"，
    吸附不到时重叠为 0，所以真实语料里很少造出超过较短块一半的重叠——端到端
    断言"结果无近重复"永远为真，这条分支等于没被测过。用合成的命中直接打这个
    函数才能验证它真的会筛。
    """
    # 同分时短的块优先：整块命中率相同的情况下，短块的信息密度更高。
    # RRF 下这条仍然必要——两路名次互换的组合（1+2 与 2+1）分数完全相同。
    ordered = sorted(hits, key=lambda hit: (-hit.score, hit.end - hit.start))
    picked: list[EvidenceHit] = []
    for hit in ordered:
        if any(_near_duplicate(hit, kept) for kept in picked):
            continue
        picked.append(hit)
        if len(picked) >= top_k:
            break
    return picked


def search_sources(sources: list[Source], query: str, top_k: int = 5) -> list[EvidenceHit]:
    """在**本次请求带来的来源**里找相关片段（BM25 + 向量，RRF 融合）。

    只查参数给的这些，不碰任何持久化索引：agent 一次运行能看到的证据范围必须
    与它实际拿到的来源严格一致，否则它引用的东西在界面上根本点不到。向量缓存
    不破例——那只是把同一段文本的向量复用，候选集仍然只来自 `sources`。

    `score` 的口径：
      - 混合路：RRF 分（两路 `1/(60+名次)` 相加），通常落在 0.01–0.04
      - 单路：BM25 分，无上界
      - **只在同一次检索内可比**。IDF 的分母 df 是在这次请求切出的块上算的，
        换一批材料、甚至换一次切块参数，同一个块的绝对值都会变。跨请求比分、
        拿分数当阈值，都是错的。需要跨请求比较时看 `method` 与
        `score_bm25`/`score_vector`，别用 `score`。

    `method` 逐条标明这条路：`hybrid` 表示向量路真的贡献了候选，`lexical`
    表示只有 BM25。命中不证明结论正确，上层不能拿它当事实校验——这也是它与
    `search()` 那个检索桩的区别：那个将来接向量库，这个永远只做"在给定文本里定位"。
    """
    question = query.strip()
    if not _token_list(question):
        return []

    collected: list[tuple[str, int, int, str]] = []
    for source in sources:
        # 不能先 strip 再切块：偏移会整体前移，调用方拿它去切 Source.text
        # 得到的不是 quote 那段文字。空判断用 strip，切块用原文。
        text = source.text or ""
        if not text.strip():
            continue
        for start, end, quote in _chunks(text):
            collected.append((source.id, start, end, quote))
    if not collected:
        return []

    quotes = [item[3] for item in collected]
    lengths = [item[2] - item[1] for item in collected]
    lexical = _bm25_scores(question, quotes)
    bm25_rank = _bm25_ranking(lexical, lengths)

    similarity: dict[int, float] = {}
    if _hybrid_enabled() and len(question) >= MIN_VECTOR_QUERY_CHARS:
        similarity, reason = _vector_scores(quotes, question)
        if reason and reason != "unconfigured":
            logger.info("检索走单路 BM25（%s）", reason)

    if similarity:
        pool = max(top_k * CANDIDATE_FACTOR, top_k)
        # 两路各留前 pool 名再融合：只在一路靠前的结果不会被另一路的名次拖掉。
        # 单路时**不**截断——那条路要保住改动前"所有命中块一起排序"的行为。
        top_bm25 = {index: rank for index, rank in bm25_rank.items() if rank <= pool}
        top_vector = {index: rank
                      for index, rank in _vector_ranking(similarity, lengths).items()
                      if rank <= pool}
        fused = {index: _rrf_value(index, top_bm25, top_vector)
                 for index in set(top_bm25) | set(top_vector)}
        method: Literal["lexical", "hybrid"] = "hybrid"
    else:
        fused = {index: lexical[index] for index in bm25_rank}
        method = "lexical"

    hits = [
        EvidenceHit(
            source_id=collected[index][0],
            start=collected[index][1],
            end=collected[index][2],
            quote=collected[index][3],
            score=round(score, 6),
            # 只走向量路的块 bm25 分是 0 而不是 None：它确实参与了 BM25 打分，
            # 只是没命中任何查询词。None 留给"这条根本没走那一路"。
            score_bm25=round(lexical[index], 6),
            score_vector=round(similarity[index], 6) if index in similarity else None,
            method=method,
        )
        for index, score in fused.items()
    ]

    # 排序之后再去重：重叠块会把同一段原文重复推进 top_k，真正不同的证据
    # 反而挤不进来。只在截断前多这一道筛，不改前面任何一步。
    return _pick_top(hits, top_k)
