"""知识检索（F12 / F13 / F14）。

位置：agent 循环里由 `rag_query` 工具按需调用（`app/agent/tools.py::_rag()`），
用检索结果给模型补上下文，不进导图契约。
状态：**未实现**。当前返回空结果，接口存在只为固定契约与前端联调顺序。

实现路线（按成本从低到高）：
  1. PostgreSQL 全文检索（tsvector）—— 不需要新组件
  2. pgvector + 嵌入模型 —— 需要 embedding 调用与向量列
  3. Milvus / Pinecone —— 仅在明确要求时再考虑

注意：向量检索解决的是"找到相关片段"，不是"判断内容正确"。
F13 的验收不能被等同于事实验证。
"""
from __future__ import annotations

import re
from collections.abc import Iterator

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.source import Source


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
    """

    model_config = ConfigDict(extra="forbid")
    source_id: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    quote: str
    score: float


# ---------------------------------------------------------------- 切块参数
#
# 词面检索只关心"哪一段包含查询里的字组"，所以切块的目标是让每块读起来是
# 完整的句子群，而不是精确控制长度：命中后回给模型的是整块原文，块太大就
# 开始跑题，块太小又缺上下文。
# 目标长度，标的是"一块读起来多长合适"这个设计意图。注意它不参与本轮切点
# 计算：贪心"同级取最靠后"等价于"在窗口里尽量取大"，实际块长贴着 MAX。
# 留成常量而不是删掉，是因为它同时是重叠比例和将来向量检索的调参参照值——
# 参数表里少了它，后来人只能从 MAX 反推。
CHUNK_TARGET_CHARS = 450
CHUNK_MAX_CHARS = 700      # 硬上限：再大，命中回给模型的原文会稀释查询
CHUNK_MIN_CHARS = 150      # 低于它向后合并：一句话单独成块不值一个 top_k
# 相邻块重叠 60 码点。词面检索其实不需要它——下一步要换向量检索，跨块的
# 因果句和"定义—被定义"一旦被切断，语义检索就永远找不回来；届时补重叠要
# 重切全部材料并重建索引，代价远大于现在留 60。
CHUNK_OVERLAP_CHARS = 60
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


def _bigrams(text: str) -> set[str]:
    """字符二元组。

    中文没有空格可分词，按单字切又太碎（重合率虚高，几乎什么都能命中），
    二元组是这两者之间最省事的折中：不引入分词依赖，区分度也够用。
    """
    chars = list(text)
    if len(chars) < 2:
        return set(chars)
    return {chars[index] + chars[index + 1] for index in range(len(chars) - 1)}


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
        (_NEWLINE_RE, False),
        (_SENTENCE_END_RE, False),
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
    """下一块的起点：从 end - OVERLAP 向后吸附到最近的句首，最多走 40 码点。

    不用 `end - OVERLAP` 直接回退，是因为那样新的块会从半句话中间开始；
    吸附到句首之后，每块的起点都落在合法边界上，重叠量 ≤ OVERLAP。
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

    只动 start/end，不重排文本。贪心切点产出的块长天然 ≥ MIN，所以这条规则
    在当前参数下基本是防御性的：参数被调成 MIN > MAX、或将来把重叠调大时
    才会真正触发。
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
        if merged[-1][1] - merged[-2][0] <= CHUNK_MAX_CHARS:
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


def _near_duplicate(left: EvidenceHit, right: EvidenceHit) -> bool:
    """同一来源里，两条命中区间重叠超过较短者 50% 就算近重复。

    相邻块有 60 码点重叠，同一句话常能同时命中前后两块；不去重的话
    top_k=5 会被同一条证据的副本占掉两格，真正不同的证据反而挤不进来。
    跨来源的区间是不同文档的坐标，不能拿来比。
    """
    if left.source_id != right.source_id:
        return False
    overlap = min(left.end, right.end) - max(left.start, right.start)
    if overlap <= 0:
        return False
    return overlap * 2 > min(left.end - left.start, right.end - right.start)


def search_sources(sources: list[Source], query: str, top_k: int = 5) -> list[EvidenceHit]:
    """在**本次请求带来的来源**里找相关片段。

    只查参数给的这些，不碰任何持久化索引：agent 一次运行能看到的证据范围必须
    与它实际拿到的来源严格一致，否则它引用的东西在界面上根本点不到。

    这是词面检索而不是语义检索，`score` 只表示字面相关程度。命中不证明结论
    正确，上层不能拿它当事实校验——这也是它与 `search()` 那个检索桩的区别：
    那个将来接向量库，这个永远只做"在给定文本里定位"。
    """
    question = _bigrams(query.strip())
    if not question:
        return []

    hits: list[EvidenceHit] = []
    for source in sources:
        # 不能先 strip 再切块：偏移会整体前移，调用方拿它去切 Source.text
        # 得到的不是 quote 那段文字。空判断用 strip，切块用原文。
        text = source.text or ""
        if not text.strip():
            continue
        for start, end, chunk in _chunks(text):
            overlap = question & _bigrams(chunk)
            if not overlap:
                continue
            hits.append(EvidenceHit(
                source_id=source.id,
                start=start,
                end=end,
                quote=chunk,
                score=round(len(overlap) / len(question), 4),
            ))

    # 同分时短的块优先：整块命中率相同的情况下，短块的信息密度更高
    hits.sort(key=lambda hit: (-hit.score, hit.end - hit.start))

    # 排序之后再去重：重叠块会把同一段原文重复推进 top_k。签名、打分公式和
    # 排序键都不动，只在截断前多这一道筛。
    picked: list[EvidenceHit] = []
    for hit in hits:
        if any(_near_duplicate(hit, kept) for kept in picked):
            continue
        picked.append(hit)
        if len(picked) >= top_k:
            break
    return picked
