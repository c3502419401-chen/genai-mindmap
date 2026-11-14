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

import asyncio
import logging
import math
import os
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from app.contracts.source import Source, SourceMetadata, blank_to_none, clean_tags, to_utc_iso
from app.knowledge import library as knowledge_library
from app.knowledge import rerank as knowledge_rerank
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
    # 融合后的最终分。混合检索时是 RRF 分，单路时是 BM25 分（或余弦），
    # 多路时是各路 RRF 分累加（见 `search_multi`）。
    # **只在同一次检索内可比**：IDF 与名次都依赖这次请求切出的块集合。
    score: float
    # 两路的原始分，供调试与将来调权。没走那一路时是 None，不是 0——0 是
    # "走了但得 0 分"，None 是"根本没参与"，两者对排查的含义完全不同。
    score_bm25: float | None = None
    score_vector: float | None = None
    # 这条结果实际由哪条路支撑。`_rag` 的 data["method"] 是请求级的旧字段，
    # 逐条标注才能看出"这条是纯语义捞回来的、字面一个词都不沾"。
    method: Literal["lexical", "hybrid"] = "lexical"
    # 这条命中来自哪条**通道**：文本检索（`None`，默认）还是图游走（`"graph"`）。
    #
    # 为什么不复用 `method`：`method` 回答的是"哪条路支撑了这条"（词法 / 词法+向量），
    # 而图命中既不是词法也不是向量——它的 `score_bm25` / `score_vector` 都是 `None`，
    # `method` 恒为兼容值 `lexical`。把 `"graph"` 塞进 `method` 会让所有读
    # `method == "hybrid"` 的下游（统计、日志、测试）多一个它们没见过的取值，而
    # 语义上是两个正交的维度。
    #
    # 默认 `None` 而不是 `"text"`：**开关关着时不能留下任何痕迹**——加一个恒非空的
    # 新键会让"与旧版本逐字节相同"（`tests/test_rerank.py` /
    # `tests/test_parent_retrieval.py` 拿 git 里的旧模块比 `model_dump()`）当场失效，
    # 而那个契约正是"接线没接通时不许有任何副作用"的保证。这与 `leaf_start` /
    # `rerank_score` 当初相加时的处理一致：新字段只在真的走了那条路时才不是 None。
    origin: Literal["graph"] | None = None
    # 这次命中**真正匹配上**的子块区间。只有父块返回模式（`search_parent`，以及
    # `MINDMAP_PARENT_RETRIEVAL=1` 时的 `search_multi`）才有值：那里 `start/end`
    # 是父块、`leaf_start/leaf_end` 是子块。`search_sources` 恒为 None——它的
    # `start/end` 就是匹配单元，再记一遍是同一个数。
    #
    # 这两个字段不是给人看的，是给引用校验用的：`quote` 变成 500 字的父块之后，
    # 模型从父块里引一句与查询无关的话也照样通过逐字校验，`verify_source_refs`
    # 会从"证据核验"退化成"格式检查"。有了子块区间，校验才能问出那句
    # "这条引用落在命中的那 200 字里吗"（见 `optimization/source_refs.py`
    # 的 `EvidenceSpan`）。它们现在的去处是 `RunContext.evidence`：`tools._rag`
    # 每检索一次就把这一批转成 `EvidenceSpan` 并进去，生成收尾时统一交给
    # `verify_source_refs(..., evidence=...)`。
    leaf_start: int | None = None
    leaf_end: int | None = None
    # 重排分（0–1），**只有被 reranker 打过分的那几条有值**，其余一律 None。
    #
    # 为什么不覆盖 `score`（见 `knowledge/rerank.py` 的模块 docstring）：`score`
    # 是融合分（RRF / BM25 / 多路累加），而且它**本来就不单调**（`search_multi`
    # 与 `_merge_batches` 都有保底段）。把 rerank 的 [0,1] 概率灌进同一个字段，
    # 会让下游任何一次 `sorted(hits, key=score)` 把重排静默撤销。留成两个字段，
    # `is not None` 就是"这条被重排过"的唯一标记，误用的表现是"重排没生效"。
    #
    # 顺序与分数因此**不再单调**：重排后名次由 `rerank_score` 决定，`score` 仍是
    # 融合口径。要按名次读，看列表顺序；要比相关性，看 `rerank_score`。
    rerank_score: float | None = None


# ---------------------------------------------------------------- 检索前置过滤

# 只写日期不带时间（`2024-01-15`）。这类输入按"整天"解释：
# since 取当天零点、until 取当天最后一秒——不然 `until=2024-01-15` 会把
# `2024-01-15T09:00:00Z` 排除在外（字典序上它比纯日期长且更大），
# 用户说的"截至 1 月 15 日"显然是**包含**当天的。
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SourceFilter(BaseModel):
    """检索前的来源过滤条件。每一项都可以不给；不给就不参与筛选。

    语义写死在这里，调用方不必猜：

    1. `author`：去首尾空白 + 大小写折叠（`casefold`）后**精确匹配**。
       `"  Zhang San "` 命中 `"zhang san"`，但不做子串匹配——`"Zhang"` 不命中
       `"Zhang San"`。子串匹配会让"张三"命中"张三丰"，在只有 1–5 份材料的
       场景里一次误命中就等于没过滤。
    2. `since` / `until`：比较的是**文档时间**——先取 `created_at`，它为 None
       时退回 `modified_at`；两者都为 None 的来源被排除。
       **闭区间**：两端的等号都算命中（`since<=t<=until`）。
       `until` 只写日期时取到当天 `23:59:59Z`，见 `_DATE_ONLY_RE` 的注释。
       只用一个字段（而不是"两个字段任一命中"）是为了可预测：一份 2020 年创建、
       昨天又改过的文档，在 `since=2026-01-01` 下不该出现——用户问的是
       "这段时期的文档"，不是"这段时期碰过的文档"。
    3. `category`：去首尾空白后精确匹配，**大小写敏感**。分类是调用方自定的
       受控枚举，大小写折叠可能把两个本意不同的类别悄悄合并。
    4. `tags`：**全部命中（AND）**，大小写不敏感、去空白。选 AND 是因为过滤是
       收紧漏斗的动作，OR 只会让漏斗变宽、更容易放进不相关来源；真要"任一
       标签"就分两次请求，语义还更清楚。

    **元数据缺失怎么办**（最容易出错的地方）：某一项条件给了值，而来源在那一
    项上是 None → **排除**（宁缺毋滥）；某一项条件是 None → 这一项根本不参与
    筛选，缺元数据的来源照常通过。两条合起来的效果是"缺什么就筛不了什么"，
    而不是"缺就一律出局"。

    时间值接受纯日期（`2024-01-15`）或 ISO8601 时间戳（`2024-01-15T10:30:00Z`、
    `+08:00` 偏移都行），内部统一归一化成 UTC。解析不了直接抛校验错误、不静默
    忽略：调用方写了 `2024-13-01` 却拿到 2019 年的文档，比报错难查得多。
    """

    model_config = ConfigDict(extra="forbid")
    author: str | None = None
    since: str | None = None
    until: str | None = None
    category: str | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("author", "category")
    @classmethod
    def _blank_text_is_none(cls, value: str | None) -> str | None:
        # 空串与 None 同义。不折平的话 `author=""` 会去匹配"作者是空串"的来源
        # ——那个集合永远是空，等于把全部来源筛掉，而调用方本意是"不筛"。
        return blank_to_none(value)

    @field_validator("since", "until")
    @classmethod
    def _normalize_bound(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        normalized = to_utc_iso(text)
        if normalized is None:
            raise ValueError(f"{info.field_name} 不是可解析的时间：{value!r}")
        if _DATE_ONLY_RE.match(text):
            # 纯日期：since 落在当天 00:00:00，until 落在当天 23:59:59。
            # 上界这一步不能省——`until=2024-01-15` 的字面值比
            # `2024-01-15T09:00:00Z` 小，直接用会把当天的文档全部排除。
            return f"{text}T23:59:59Z" if info.field_name == "until" \
                else f"{text}T00:00:00Z"
        return normalized

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        # 与 `SourceMetadata` 共用同一份清洗：任何一边多做或漏做，
        # "预算" 与 " 预算 " 就比不上，过滤会静默失效。
        return clean_tags(value)


def _document_time(metadata: SourceMetadata) -> str | None:
    """来源的"文档时间"：创建时间优先，缺失时退回修改时间。"""
    return metadata.created_at or metadata.modified_at


def _matches(source: Source, filters: SourceFilter) -> bool:
    """单个来源是否通过全部条件。条件为 None 的项直接跳过（不参与筛选）。"""
    metadata = source.metadata

    if filters.author is not None:
        author = (metadata.author or "").strip()
        # 没有作者就过不了作者过滤：宁可少给，不给错的。
        if not author or author.casefold() != filters.author.casefold():
            return False

    if filters.since is not None or filters.until is not None:
        stamp = _document_time(metadata)
        # 同上：要按时间筛，却没有时间可筛的来源直接出局。
        if stamp is None:
            return False
        # 两端都是归一化后的 UTC 秒级字符串，字典序即时间序，不必再解析
        if filters.since is not None and stamp < filters.since:
            return False
        if filters.until is not None and stamp > filters.until:
            return False

    if filters.category is not None:
        if (metadata.category or "").strip() != filters.category:
            return False

    if filters.tags:
        owned = {tag.strip().casefold() for tag in metadata.tags if tag and tag.strip()}
        if not all(tag.casefold() in owned for tag in filters.tags):
            return False

    return True


def filter_sources(sources: list[Source], filters: SourceFilter) -> list[Source]:
    """按元数据裁剪来源列表。纯内存操作，不碰任何持久化状态。

    公开出来是给调用方判"是不是被筛光了"用的：`search_sources` 返回空列表有两种
    原因（过滤筛光 / 查询没命中），而这两种该给用户的话完全不同。调用方先自己
    过一遍这个函数就能区分，不必让 `search_sources` 改签名去回传统计。
    """
    return [source for source in sources if _matches(source, filters)]


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


def _pick_cut(
    text: str, pos: int, mask: bytearray, *,
    target_chars: int = CHUNK_TARGET_CHARS,
    max_chars: int = CHUNK_MAX_CHARS,
    min_chars: int = CHUNK_MIN_CHARS,
) -> int:
    """挑切点：先在 [TARGET, MAX] 里取最靠前的，退一步在 [MIN, TARGET) 里取最靠后的。

    两个窗口的顺序就是"块长优先"的意思：目标窗口里有边界时用最靠前的那个，
    块长落在 TARGET 附近；只有 TARGET 之后整段都没有可切之处（一个 300 字的
    长句），才允许在 TARGET 之前收尾，把块压到 MIN 与 TARGET 之间。
    反过来先找"最靠后"会让每块都贴着 MAX——那样 TARGET 就是一句空话。

    三个长度参数默认就是父块的那一组常量。之所以参数化而不是另写一份：子块要
    换的只是长度，分隔符优先级、安全回退、"先靠前再靠后"这两段窗口的顺序**一个
    都不能变**——复制一份就等于埋下两份会各自漂移的切点逻辑，而那种漂移除了
    回归用例没有任何信号（本文件 `_single_search` 的注释讲的是同一件事）。
    """
    target = min(pos + target_chars, len(text))
    high = min(pos + max_chars, len(text))
    low = pos + min_chars

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


def _too_short(span: list[int], minimum: int = CHUNK_MIN_CHARS) -> bool:
    return span[1] - span[0] < minimum


def _merge_short(
    spans: list[tuple[int, int]], *,
    minimum: int = CHUNK_MIN_CHARS,
    ceiling: int = CHUNK_MAX_CHARS,
    last_ceiling: int | None = None,
) -> list[tuple[int, int]]:
    """自左向右把过短的块并进后一块；末块过短则并进前一块。

    只动 start/end，不重排文本。贪心切点产出的块长天然 ≥ MIN，所以向左合并
    这条在当前参数下基本是防御性的。真正会触发的是**末块**：切块循环的前提是
    "剩余 > MAX"，于是 `"字"*701` 会切出 `(0,700) + (700,701)`，末块只剩一个字。
    那种块占掉一个 top_k 位置却什么信息都没有，所以末块合并允许结果略超 MAX
    （上限默认放宽到 MAX+MIN）：宁可最后一块多出几百字符，也不要留一个碎块。

    `minimum` / `ceiling` 参数化是为了子块复用同一套合并规则（见 `_leaf_spans`）：
    合并的**判据**（短了就并、并完不许超上限）与父块完全一样，只是阈值不同。

    `last_ceiling` 单独开一个口子，是因为"末块可以超上限"这条放宽**不能无脑复用**：
    子块的上限是引用字段的字数契约（见 `LEAF_MAX_CHARS`），超一个字那条命中就
    交不出 `evidence_*`。子块因此传 `last_ceiling=LEAF_MAX_CHARS`，放弃"末块多出
    一点"，宁愿留一个过短的末块——碎块的代价是匹配区分度低，超限的代价是整条
    证据区间约束对这批命中失效，两者的方向完全不同。缺省 `None` 时仍按
    `ceiling + minimum` 放宽，父块行为逐字节不变。
    """
    limit = ceiling + minimum if last_ceiling is None else last_ceiling
    merged: list[list[int]] = []
    for start, end in spans:
        if (
            merged
            and _too_short(merged[-1], minimum)
            and end - merged[-1][0] <= ceiling
        ):
            merged[-1][1] = end
            continue
        merged.append([start, end])
    if len(merged) > 1 and _too_short(merged[-1], minimum):
        if merged[-1][1] - merged[-2][0] <= limit:
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


# ---------------------------------------------------------------- 两级切块
#
# 单级切块把"匹配粒度"与"返回粒度"绑死在同一批块上：块大（450–700）时匹配带
# 噪声、长块在 BM25 的长度归一化里吃亏；块小（150–250）时匹配准，但回给模型的
# 上下文只有一句话，读起来没头没尾。
#
# 两级切块把这两件事拆开：**子块负责匹配**（短，词面与向量都不被无关内容稀释），
# **父块负责返回**（长，读得懂前因后果）。子块不是独立文本，它就是父块里的一段，
# 所以偏移仍落在原文上，`text[start:end]` 对父子两级都逐字成立。
#
# 父块参数直接用上面那组 `CHUNK_*`，一个都不改：`_chunk_spans` 是别的调用方与
# 测试的基础，它的块边界同时是父块的边界——两套边界一旦不同，"父块"就会变成
# 一个与 `search_sources` 返回的块无法对齐的概念，将来接落库时要多一份映射。
LEAF_TARGET_CHARS = 200    # 子块目标长度：一句话群，足够判定"讲的是这件事"
LEAF_MAX_CHARS = 300       # 子块硬上限：不得超过 `SourceRef.quote` 的 300（理由见下）
LEAF_MIN_CHARS = 100       # 低于它向后合并：一百字以下的子块匹配时区分度不足

# `LEAF_MAX_CHARS` 取 300 不只是"再长会混进两三个话题"：它是**引用证据链**的一段。
# `tools._citable_hits` 把命中的子块整段当 `evidence_quote` 回给模型，而
# `SourceRef.quote` 的上限就是 300。子块超过它时那条命中只能不给 `evidence_*`
# （截断后的 quote 与偏移对不上，逐字校验必挂），模型随即退回"只抄 quote、不写
# 偏移"——而 `source_refs._evidence_problem` 对没有区间的引用不施加约束，于是
# "引的这句在命中的那段里吗"这条判定对这批命中整个失效。模型可以从父块里挑任意
# 一句看着相关的话，逐字校验照样通过。缝原来只有二十几个字，但它正是这条防护要
# 挡的那件事，所以子块上限与引用上限取同一个数。
#
# 压到 300 之后，**末块合并也必须跟着压**（见 `_leaf_spans` 的 `last_ceiling`）：
# `_merge_short` 默认把末块上限放宽到 `ceiling + minimum`（父块是 700+150），子块
# 照抄这条规则会让末块并到 400 字——缝不但没堵上，还比原来更宽。


@dataclass(frozen=True)
class ChunkSpan:
    """两级切块树上的一个节点。

    `start` / `end` 是**原文码点**偏移，与 `EvidenceHit` / `SourceRef` 同口径，
    父子两级一视同仁：父块 `text[start:end]` 是完整上下文，子块 `text[start:end]`
    是父块里的一段，两者都能直接切片高亮。

    `parent` 存的是**本列表里的下标**而不是区间：同一段原文可能被切成重叠的两块，
    区间反查会有歧义，下标不会。`level=0`（父）的 `parent` 恒为 None。
    """

    start: int
    end: int
    level: int
    parent: int | None


def _leaf_spans(text: str, start: int, end: int, mask: bytearray) -> list[tuple[int, int]]:
    """把一个父块 `[start, end)` 内部切成子块，结果严格落在父块内。

    切点完全复用父块的机制（`_pick_cut` 的分隔符优先级、安全回退、`_overlap_start`
    的句首吸附），只换长度参数。重叠沿用 `CHUNK_OVERLAP_CHARS`（100）：子块只有
    200 上下，100 的重叠约占一半，看起来偏大，但它是**句子级**的重叠——吸附不
    到句首时直接不重叠，所以真实语料里远不是每一对都重叠一半。

    与父块唯一的机制差别是长度参数；合并规则也是同一套（`_merge_short`），但
    **末块上限不放宽**：`last_ceiling=LEAF_MAX_CHARS`。父块那边"宁可末块多出几百
    字符，也不留一个碎块"是对的（多出来的是可读的上下文），子块这边不成立——
    子块长度是引用字段的字数契约（见 `LEAF_MAX_CHARS` 那段注释），超一个字那条
    命中就交不出 `evidence_*`，模型退回无偏移引用，证据区间约束对整批命中失效。
    代价是偶尔留一个 60–90 字的末块：它照样能被 BM25/向量匹配到，只是区分度低
    一点；而超限的代价是一条防护静默失效。这个方向的取舍没有第二种选择。
    """
    if end - start <= LEAF_MAX_CHARS:
        # 父块本身还没到子块上限：它就是自己的子块。此时长度可能低于 LEAF_MIN
        # （末父块常见），这是"父块太短"造成的，不是切分失败——不拿 MIN 去反推，
        # 那会逼着切块器把短父块补长，等于凭空造原文。
        return [(start, end)]

    spans: list[tuple[int, int]] = []
    position = start
    while end - position > LEAF_MAX_CHARS:
        cut = _pick_cut(
            text, position, mask,
            target_chars=LEAF_TARGET_CHARS,
            max_chars=LEAF_MAX_CHARS,
            min_chars=LEAF_MIN_CHARS,
        )
        spans.append((position, cut))
        overlap = _overlap_start(text, position, cut, mask)
        # `_overlap_start` 保证返回值 ≥ position+1，这里的兜底是给"循环必须前进"
        # 这条不变量兜底：真进了死循环，代价是整次请求挂住，而不是慢一点。
        position = overlap if overlap > position else cut
    spans.append((position, end))
    return _merge_short(
        spans, minimum=LEAF_MIN_CHARS, ceiling=LEAF_MAX_CHARS,
        last_ceiling=LEAF_MAX_CHARS,
    )


def _chunk_tree(text: str) -> list[ChunkSpan]:
    """先按现有参数切出父块，再把每个父块内部切成子块。

    返回顺序就是原文顺序，每个父块后面紧跟它的子块——所以子块的 `parent` 下标
    `i` 满足 `i < 自己在列表里的下标`，一次正序遍历就能拿到父块区间。

    父块直接取自 `_chunk_spans`，不是"再写一遍同样的贪心"：`_chunk_spans` 的
    覆盖性（首块从 0 起、末块到 `len(text)`、相邻块相接或重叠）与全部既有的边界
    测试都由它保证，父块借这层就等于借到了那些约束。代价是 `_unsafe_boundaries`
    被算两遍（一次在里面、一次在这里给子块用）——它是 O(n) 的差分数组，五万字
    材料上是微秒级，不值得为省它去动 `_chunk_spans` 的签名。

    空文本返回单个零宽父块、没有子块：`start < end` 是子块的存在前提，零宽区间
    不产子块，也就不会被下游当成"命中了一个空片段"。
    """
    mask = _unsafe_boundaries(text)
    spans: list[ChunkSpan] = []
    for start, end in _chunk_spans(text):
        parent = len(spans)
        spans.append(ChunkSpan(start=start, end=end, level=0, parent=None))
        if end <= start:
            continue
        for leaf_start, leaf_end in _leaf_spans(text, start, end, mask):
            spans.append(ChunkSpan(
                start=leaf_start, end=leaf_end, level=1, parent=parent))
    return spans


# ---------------------------------------------------------------- 材料目录（只读）
#
# `heading_outline` 回答的是"**这份材料自己**把内容分成了哪几块"，给骨架调用
# （`agent/tools._generate_deep`）当结构线索用。它只读原文：不参与切块、不打分、
# 不进索引、不改任何既有边界——把目录从这份材料里去掉，检索结果逐字段相同。
#
# **为什么不靠块边界**：实测 10 份真实材料，父块的**起始行**是标题的只有 12%
# （`scripts/measure_outline_headings.py`）——父块是"450–700 字 + 句首吸附"切出来的，
# 一个块常从半句话开始、标题落在块中间或紧跟在前一块末尾。按父块起点取目录会漏掉
# 八成，所以这里逐行扫原文。
#
# **为什么只取一级**：骨架要回答的是"分成哪几块"。把二级小节也拼进去，十几二十条
# 细碎片段会把一级分支挤成小节罗列，也把真正的大章稀释掉。

OUTLINE_MAX_ITEMS = 30
"""目录条数上限。超了按 coverage 降序保留（留覆盖大的，丢细碎小节）。"""

OUTLINE_MAX_TITLE_CHARS = 40
"""单条标题的字数上限。markdown 标题超了**截断**（它确实是标题），
非 markdown 候选超了**丢弃**（那是散文句，见 `_looks_like_prose`）。"""

OUTLINE_MIN_ITEMS = 2
"""少于两条不叫目录。一条标题覆盖全文的"目录"对骨架是误导：模型会以为全文只有
那一章。这类材料按"没有可用目录"处理（返回空列表，调用方回退到自行归纳）。"""

OUTLINE_MIN_TITLE_CHARS = 2
"""候选标题的最短字数。一个字的"标题"是编号残片（`1.5 倍` 去掉编号后的 `倍`）。"""

OUTLINE_COLON_TAIL_CHARS = 20
"""冒号后的分句超过这么长就判为正文。见 `_looks_like_prose`。"""

# markdown 标题行：`#` 到 `######`，中间**必须有空格**（`#tag` 不是标题）；
# 行尾的闭合 `#`（`## 标题 ##`）是 ATX 语法的一部分，取标题时剥掉。
# 用 `[ \t]` 而不是 `\s`：`\s` 会让一行孤零零的 `#`（正文在下一行）也匹配上。
_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*)$", re.MULTILINE)
_MARKDOWN_CLOSING_RE = re.compile(r"[ \t]+#+[ \t]*$")

# 非 markdown 材料的编号形状。`_HEADING_RE` 只回答"这一行算不算标题"，级别由这里
# 判：先按字面前缀判，数字编号单独走 `_numbering_line`。
_NUMBERING_PREFIXES: tuple[tuple[str, int, re.Pattern[str]], ...] = (
    ("chapter", 1, re.compile(r"^第[一二三四五六七八九十百]+章")),
    ("section", 2, re.compile(r"^第[一二三四五六七八九十百]+节")),
    ("article", 1, re.compile(r"^第[一二三四五六七八九十百]+条")),
    ("cn1", 1, re.compile(r"^[一二三四五六七八九十]+、")),
    ("cn_sub", 2, re.compile(r"^（[一二三四五六七八九十]+）")),
    ("paren_sub", 2, re.compile(r"^\([一二三四五六七八九十]+\)")),
)
# `1.` / `1、` / `1.1` / `3.1.2` 后面紧跟正文：分隔符之后不能是数字（否则 `1.5 倍`
# 的 `1.5` 会被当成两级编号），余下部分就是标题。
_DECIMAL_RE = re.compile(r"^(\d+(?:\.\d+)*)([、.]|[ \t]+)(.*)$")
# 行首编号从标题里剥掉（markdown 与非 markdown 都剥）：调用方自己会重新编号
# （`1. 引言（约占 8%）`），留着原文编号会渲染成 `1. 一、实验目的`、`2. 1 已经落地的
# 两件事`。实测剥不剥对覆盖率没有差别（32.7% vs 30.8%，在噪声内），所以这一条只
# 按"渲染出来读不读得通"定。
#
# 剥的**只是分隔符编号**（`一、` / `1.` / `（一）` / `第N章`）——那是 `_HEADING_RE`
# 认的形状，天然只是前缀。裸的 `1 已经落地的两件事` 不剥：那里的 `1 ` 后面没有分隔符，
# 它可能是标题的一部分（`3 种检索形态，别混着谈` 剥掉就成了 `种检索形态`）。
# `(?=\D)` 是为了不把 `1.5 倍` 的 `1.5` 当编号剥掉——没有它，正则回溯会先试
# `\d+`="1" + `[、.]`="." 就收工，留下 `5 倍`。
_LEADING_MARKER_RE = re.compile(
    r"^(?:第[一二三四五六七八九十百]+[章节条][ \t]*|"
    r"[一二三四五六七八九十]+、[ \t]*|"
    r"[（(][一二三四五六七八九十]+[）)][ \t]*|"
    r"\d+(?:\.\d+)*[、.][ \t]*(?=\D))"
)
_COLON_TAIL_RE = re.compile(r"[：:][ \t]*(.+)$")
# **整个段落加粗**的一行（`**一、实验须知**`）。Docling 把"整段只有加粗文字"的
# 段落导出成这种形状，而 Word 里手工加粗的章节名正是这样——docx 的章节结构十有
# 八九落在这一条上。排除内部还有 `*` 的情况：`1. **判断器…**。同一 query…` 是
# "行首编号 + 段中加粗"，它内层不匹配（前后都有别的字），不会走到这里。
_BOLD_LINE_RE = re.compile(r"^[ \t]*\*\*([^*\n]+)\*\*[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class OutlineEntry:
    """材料目录里的一条：一个章节标题 + 它管到哪儿。

    `start` 是该标题在**原文**里的起始偏移（码点，与 `ChunkSpan` / `SourceRef`
    同一口径），`coverage` 是它到**下一个同级标题**之间的字数——两者合起来才是
    "这一章有多大"。只给标题的话，"引言" 与 "实测数据" 在模型眼里一样重，
    而它们该分到的分支显然不该一样重。

    `coverage` 只在**同一份材料内部**可比：它是原始码点数，跨材料比没有意义
    （多份材料时覆盖率要按各自字数加权，见 `outline_coverage`）。
    """

    title: str
    start: int
    coverage: int


def _line_bounds(text: str, offset: int) -> tuple[int, int]:
    """`offset` 所在行的 `[起, 止)`，止于换行符之前。

    `offset` 不在行首时结果仍是**整行**的边界：目录项的 `start` 要落在行首，
    调用方拿它切片高亮时不该出现半行。
    """
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    return start, len(text) if end < 0 else end


def _clean_markdown_title(body: str, max_title_chars: int) -> str:
    """markdown 标题的正文：去结尾闭合井号、剥分隔符编号、压空白、超长截断。

    截断而不是丢弃：`^#{1,6}` 行就是真标题（上一轮实测 P=100%），长是它自己的事，
    丢掉等于把这一章从目录里删掉。

    剥编号的理由见 `_LEADING_MARKER_RE`：中文材料的 `# 一、实验目的` 很常见，
    不剥会渲染成 `1. 一、实验目的`。
    """
    title = _MARKDOWN_CLOSING_RE.sub("", body)
    title = _LEADING_MARKER_RE.sub("", re.sub(r"\s+", " ", title).strip())
    return title[:max_title_chars]


def _numbering_line(line: str) -> tuple[str, str] | None:
    """非 markdown 材料的一行：`(级别 kind, 去编号后的名字)`；不是标题返回 None。

    两级判定，顺序不能换：

      1. `_HEADING_RE`（切块器**自己**用的标题行首规则）说"不是标题"就到此为止——
         目录与切块器看同一批行。模型拿到的目录若包含切块器不认的结构，
         "按章节切开的块"与"目录里的章"就对不上，检索命中会落在目录之外；
      2. 它说"是标题"之后再分级：字面前缀（`第一章`/`一、`/`（一）`）先判，
         数字编号走 `_DECIMAL_RE`。分级只决定"哪一层算一级"，不决定要不要它。
    """
    if not _HEADING_RE.match(line):
        return None
    for kind, _, pattern in _NUMBERING_PREFIXES:
        match = pattern.match(line)
        if match:
            return kind, line[match.end():].strip()
    match = _DECIMAL_RE.match(line)
    if match:
        marker, separator, rest = match.groups()
        # `1.5 倍`：`\d+(?:\.\d+)*` 把 `1.5` 整体吃掉，分隔符是它后面的空格。
        # 空格分隔且后面没有正文 = 不是标题。
        if separator.isspace() and not rest.strip():
            return None
        kind = "decimal" if "." in marker else "decimal1"
        return kind, rest.strip()
    return None


def _looks_like_prose(title: str, max_title_chars: int) -> bool:
    """这条候选长得像**正文句子 / 表格行 / 列表项**，而不是标题。

    三条都是实测出来的假阳性特征（`scripts/measure_outline_headings.py` 的报告段
    直接列出了误判行）：

      - 含 `**`：`1. **判断器不是噪声源，是系统性失效**。同一 query 跑 3 次…`
        是加粗的列表项；
      - 含 `|`：`| 4 | 单换行 | 列…` 是表格行；
      - 冒号后跟长分句（> `OUTLINE_COLON_TAIL_CHARS`）或带逗号句号：
        `1. 检索：把整份材料交给模型、让模型自己找` 是编号列表项。真标题的冒号
        后面是短语（`5 下一步：结构引导的召回`，7 字）。
    """
    if "**" in title or "|" in title:
        return True
    if len(title) > max_title_chars:
        return True
    tail = _COLON_TAIL_RE.search(title)
    if tail:
        clause = tail.group(1).strip()
        if len(clause) > OUTLINE_COLON_TAIL_CHARS:
            return True
        if any(char in clause for char in "。；，,;"):
            return True
    return False


def _followed_by_blank_line(text: str, line_end: int) -> bool:
    """这一行之后是空行或文件结束。

    列表项后面通常紧跟内容（下一行还是列表项），标题后面通常空一行再进正文。
    这是几条过滤里**最贵**的一条：对"一行标题 + 空行 + 正文"的版式（markdown、
    docx 导出都是这样）它几乎不误伤，对逐行紧排的扫描件会牺牲召回。宁可那种材料
    返回空目录，也好过把二十条列表项当章节塞给骨架。
    """
    if line_end >= len(text) - 1:
        return True
    rest_start = line_end + 1
    rest_end = text.find("\n", rest_start)
    rest_end = len(text) if rest_end < 0 else rest_end
    return not text[rest_start:rest_end].strip()


def _outline_lines(text: str, *, max_title_chars: int) -> list[tuple[int, str]]:
    """逐行扫出一级标题：`[(行首偏移, 标题)]`，按原文顺序。

    两条正交的规则：

      - **有 markdown 结构就只认 markdown**（`^#{1,6}[ \\t]`）。上一轮在 10 份真实
        材料上实测这样取 P=100% / R=100%（`#` 就是真值）。再混进来判 `一、`/`1.`
        只会引入假阳性（正文里的编号列表项），不会多出一个真章节。
      - **一级 = 最浅的、真把文档分开了的那一层**。markdown 取"井号最少且至少
        `OUTLINE_MIN_ITEMS` 条"的那一级：`#` 只有一条时它是**文档标题**而不是章节
        （本仓库 10 份材料 9 份是这个形状，`docs/RAG-DECISIONS.md` 就是 1 个 `#`
        + 10 个 `##`），只认 `#` 会得到一条覆盖全文的"目录"，那是书名不是目录。
        编号行取**出现次数最多**的层级（全是 `一、` 就取 `一、`；`一、` 与 `（一）`
        混排时前者更多，就只取前者——这就是"只取一级"）。

    **只有一条 markdown 标题时不算"有 markdown 结构"，继续往下走编号行那条路**：
    这是 docx 的常见形态——Docling 从标题样式推出一个 `## 实验4 …` 当文档标题，
    正文里的章节名却是**整个段落加粗**（`**一、实验须知**`），一个 `#` 都没有。
    在这里就返回空，等于让最常见的上传格式一个目录都拿不到。
    """
    markdown: list[tuple[int, str, int]] = []
    for match in _MARKDOWN_HEADING_RE.finditer(text):
        title = _clean_markdown_title(match.group(2), max_title_chars)
        if title:
            markdown.append((match.start(), title, len(match.group(1))))
    if markdown:
        counts = Counter(level for _, _, level in markdown)
        picked = next(
            (level for level in sorted(counts) if counts[level] >= OUTLINE_MIN_ITEMS),
            None,
        )
        if picked is not None:
            return [(start, title) for start, title, level in markdown if level == picked]

    numbered: list[tuple[int, str, str]] = []
    for match in _HEADING_RE.finditer(text):
        line_start, line_end = _line_bounds(text, match.start())
        line = text[line_start:line_end].strip()
        parsed = _numbering_line(line)
        if parsed is None:
            # `_HEADING_RE` 说它是标题，但分级说不出来（`2.5` 这类）。
            # 不猜级别：猜错会让整份目录落在错的那一层上。
            continue
        kind, rest = parsed
        title = re.sub(r"[ \t]+", " ", _LEADING_MARKER_RE.sub("", rest)).strip()
        if len(title) < OUTLINE_MIN_TITLE_CHARS:
            continue
        if _looks_like_prose(title, max_title_chars):
            continue
        if not _followed_by_blank_line(text, line_end):
            continue
        numbered.append((line_start, title[:max_title_chars], kind))
    # 整个段落加粗的编号行（`**一、实验须知**`）：`_HEADING_RE` 的 `^` 吃不到它
    # （行首是 `*`），而 docx 里的章节名十有八九是这个形状。
    # 判据与上一段**完全相同**（编号形状 + 四条过滤），只有"要不要先摘掉 `**`"这一
    # 步不同——所以它不是第二条规则，而是同一个规则的另一个入口。
    for match in _BOLD_LINE_RE.finditer(text):
        line_start, line_end = _line_bounds(text, match.start())
        parsed = _numbering_line(match.group(1))
        if parsed is None:
            continue
        kind, rest = parsed
        title = re.sub(r"[ \t]+", " ", _LEADING_MARKER_RE.sub("", rest)).strip()
        if len(title) < OUTLINE_MIN_TITLE_CHARS:
            continue
        if _looks_like_prose(title, max_title_chars):
            continue
        if not _followed_by_blank_line(text, line_end):
            continue
        numbered.append((line_start, title[:max_title_chars], kind))
    numbered.sort(key=lambda item: item[0])
    if not numbered:
        return []
    # 最高频的那一层当一级；票数相同时取更浅的那层（`一、` 优先于 `（一）`），
    # 让"混排材料"的结论可复现，而不是取决于材料里先出现谁。
    depth = {kind: level for kind, level, _ in _NUMBERING_PREFIXES}
    depth["decimal"] = 2
    depth["decimal1"] = 1
    counts = Counter(kind for _, _, kind in numbered)
    picked = min(counts, key=lambda kind: (-counts[kind], depth.get(kind, 2)))
    return [(start, title) for start, title, kind in numbered if kind == picked]


def _outline_entries(text: str, *, max_title_chars: int) -> list[OutlineEntry]:
    """单份材料的目录：算 coverage、按标题去重。**不做条数裁剪**（那是全局的事）。"""
    lines = _outline_lines(text, max_title_chars=max_title_chars)
    if len(lines) < OUTLINE_MIN_ITEMS:
        return []
    entries: list[OutlineEntry] = []
    for index, (start, title) in enumerate(lines):
        end = lines[index + 1][0] if index + 1 < len(lines) else len(text)
        entries.append(OutlineEntry(title=title, start=start, coverage=max(0, end - start)))
    # 去重按**标题文本**、保留第一次出现的位置：PDF 的页眉页脚与 docx 的页眉会把
    # 同一行印在每一页顶上，不过这一关目录里会有二十条同名项。
    # coverage 在去重**之前**就算好了（对着原始标题序列），所以留下来的那条记的是
    # "它第一次出现时管到哪儿"，不会被后面重复的那几行虚增。
    seen: set[str] = set()
    unique: list[OutlineEntry] = []
    for entry in entries:
        if entry.title in seen:
            continue
        seen.add(entry.title)
        unique.append(entry)
    return unique if len(unique) >= OUTLINE_MIN_ITEMS else []


def _trim_outline(entries: list[OutlineEntry], max_items: int) -> list[OutlineEntry]:
    """超限时按 coverage 降序保留，再按原文顺序返回。

    排序方向刻意相反：**挑**的时候看覆盖（留下"实测数据"这种大章、丢掉细碎小节），
    **交出去**的时候看原文顺序——目录一旦乱序，"1. 引言 2. 相关工作"就成了密码表，
    模型也没法顺着材料的结构写分支。
    """
    if len(entries) <= max_items:
        return entries
    kept = sorted(entries, key=lambda entry: (-entry.coverage, entry.start))[:max_items]
    return sorted(kept, key=lambda entry: entry.start)


def heading_outline(
    sources: list[Source], *,
    max_items: int = OUTLINE_MAX_ITEMS,
    max_title_chars: int = OUTLINE_MAX_TITLE_CHARS,
) -> list[OutlineEntry]:
    """材料自带的章节列表（只读，逐行扫原文）。**空列表 = 这份材料没有可用目录。**

    调用方拿它当骨架的"结构线索"，所以输出只有两类：能用的目录，或者空。
    没有"凑合能用"的中间态——半份目录（只覆盖 11%）比没有目录更糟：模型会把它
    当成全文的提纲，于是漏掉的九成内容它再也不会想到要问。

    上限、去重都在这一层收口：`max_items` 条以内、按原文顺序、标题互不重复，
    `coverage` 之和就是这份目录"管住了多少字"（见 `outline_coverage`）。
    多份材料时先各自出目录再一起裁剪：一份材料的结构不因另一份材料存在而变形。
    """
    entries: list[OutlineEntry] = []
    for source in sources:
        text = source.text or ""
        if not text.strip():
            continue                      # 空正文没有结构可言（`status` 只给界面看）
        entries.extend(_outline_entries(text, max_title_chars=max_title_chars))
    return _trim_outline(entries, max_items)


def outline_coverage(
    sources: list[Source], entries: list[OutlineEntry] | None = None,
) -> float:
    """目录覆盖了材料的百分之多少：`Σ coverage / Σ len(text)`（多份按字数加权）。

    分母是**原文总字数**而不是非空白字数：`coverage` 是两条标题之间的原始码点数
    （含空行与缩进），两边同一口径，比值才不会被空白灌水。多份材料时分子分母各自
    求和，等价于按字数加权——一份三万字的手册和一份两百字的便签不该各占一半权重。

    `entries` 传进来时用它：调用方拼进 prompt 的通常是**裁剪过**的那份目录，
    覆盖率必须跟着那份算，否则"覆盖率 95%"配一份只剩 8 条的目录，两边对不上。
    """
    total = sum(len(source.text or "") for source in sources)
    if total <= 0:
        return 0.0
    catalogue = heading_outline(sources) if entries is None else entries
    # 夹到 1.0 是防御：`heading_outline` 的条目互不重叠，理论上 sum ≤ total；
    # 但调用方可以自己拼一份 entries 进来（测试、将来的多材料合并），
    # 覆盖率出现 1.3 这种数会让阈值判断变得莫名其妙。
    return min(1.0, sum(entry.coverage for entry in catalogue) / total)


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


# `MINDMAP_PARENT_RETRIEVAL` 控制"子块匹配、父块召回"（`search_parent`）。
#
# **默认开**。原先默认关的理由（见 `docs/RAG-DECISIONS.md` 第 3 节）是两条前提：
# ①每次检索都把材料整份交给模型，父块只是"把模型已经读过的内容再贴一遍"；
# ②父块会让引用链失真——`score` 来自 200 字的子块，`quote` 却是 450–700 字的父块，
# 模型从父块里挑一句与查询无关的话也照样通过逐字校验。
#
# 两条前提现在都不成立了：
#   - `MINDMAP_SOURCE_MODE` 已默认 `rag`：正文不进 prompt，检索返回的块**就是**模型
#     能看到的全部上下文，粒度不够就真的读不懂。这条正是原注释说的"这个开关是为
#     '只给片段'的形态准备的"——那个形态已经是默认形态。
#   - ②有了对策：命中区间（`leaf_start`/`leaf_end`）会随 hits 一路累积到
#     `RunContext.evidence`，生成收尾时交给 `verify_source_refs(..., evidence=...)`，
#     落在父块里但不在命中子块里的引用会被标 `SOURCE_REF_OUTSIDE_EVIDENCE`。
#     它是 warn 级诊断、**不剔除引用**（要求模型精确落在命中的那 200 字里过于苛刻，
#     实测会成批删掉合法引用）；接线在 `tools._rag` → `RunContext.record_evidence`
#     → `_generate` → `validate_generated_content` 这条链上，**先接线、后翻默认值**
#     的顺序是刻意的：反过来会有一段时间是"父块模式 + 无防护"。
PARENT_RETRIEVAL_ENV = "MINDMAP_PARENT_RETRIEVAL"

# 判定方向与 `_hybrid_enabled` 相反这件事没变，但含义反过来了：那个默认开，所以
# "不在假值集合里"就是开；这个现在也默认开，却仍然**正列举**真值——不设变量时取
# 默认值 `"1"`，显式写 `0/off/false/no/空串` 一律关。保留正列举的理由是可读性：
# 谁改了默认值，判定逻辑不用跟着改，改错的表现也只剩"默认值写错了"一种。
_PARENT_RETRIEVAL_TRUTHY = {"1", "on", "true", "yes"}


def _parent_retrieval_enabled() -> bool:
    return os.getenv(PARENT_RETRIEVAL_ENV, "1").strip().lower() in _PARENT_RETRIEVAL_TRUTHY


# `MINDMAP_GRAPH_SEARCH` 控制"图游走并进检索"（`search_parent` 的两个出口）。
#
# **默认 0**：图值不值得进检索要由数字回答，不由设计回答。开关打开时一次检索会多
# 一次 `entity_degrees` + 至多 `graph_search.MAX_ENTITIES` 条变长路径查询，而图里的
# 实体精度（`graph_resolve` 的实测：合 7 / 漏 5 / 错 1）决定了这些多出来的证据里有
# 多少是真关联。没量过之前默认关，与 `MINDMAP_RERANK` 当初"先接线、后翻默认值"的
# 顺序一致。
GRAPH_SEARCH_ENV = "MINDMAP_GRAPH_SEARCH"

# 正列举真值，与 `_PARENT_RETRIEVAL_TRUTHY` 同一套写法：改默认值时判定逻辑不用动。
_GRAPH_SEARCH_TRUTHY = {"1", "on", "true", "yes"}


def _graph_search_enabled() -> bool:
    return os.getenv(GRAPH_SEARCH_ENV, "0").strip().lower() in _GRAPH_SEARCH_TRUTHY


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


def _pick_ordered(hits: list[EvidenceHit], top_k: int) -> list[EvidenceHit]:
    """按**给定顺序**去掉近似重复并截断 top_k，不再重排。

    与 `_pick_top` 的唯一差别就是不排序。多路融合需要一个"分段拼好的顺序"
    （保底席位在前、竞争席位在后），再交给 `_pick_top` 会被它按分数重排回去，
    分段等于没做。
    """
    picked: list[EvidenceHit] = []
    for hit in hits:
        if any(_near_duplicate(hit, kept) for kept in picked):
            continue
        picked.append(hit)
        if len(picked) >= top_k:
            break
    return picked


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
    return _pick_ordered(ordered, top_k)


def _collect_chunks(sources: list[Source]) -> list[tuple[str, int, int, str]]:
    """切一遍块，返回 `(source_id, start, end, quote)`。

    `search_sources`、`search_parent` 关着时的 `search_sources` 转发、以及
    `parent=False` 的 `search_multi` 共用这一层：多路检索若每路各切一遍，
    5 万字材料上就是 6 倍的切块开销——纯 CPU，却也是这条路上最贵的一段。
    `parent=True` 的那两条走 `_collect_leaves`（子块匹配 → 父块返回），
    粒度不同但"只切一次、多路共用"这条约束相同。
    空判断用 `strip`、切块用原文：先 strip 再切块会让偏移整体前移，调用方拿它
    去切 `Source.text` 得到的就不是 quote 那段文字了。
    """
    collected: list[tuple[str, int, int, str]] = []
    for source in sources:
        text = source.text or ""
        if not text.strip():
            continue
        for start, end, quote in _chunks(text):
            collected.append((source.id, start, end, quote))
    return collected


def _to_hit(
    index: int,
    score: float,
    collected: list[tuple[str, int, int, str]],
    bm25_scores: dict[int, float],
    vector_scores: dict[int, float],
    method: Literal["lexical", "hybrid"],
) -> EvidenceHit:
    """把「块下标 + 融合分」落成一条命中。

    `bm25_scores` / `vector_scores` 只放**候选块**的值：不在这两个字典里的块，
    对应字段是 None（"这条根本没走那一路"），而不是 0（"走了但得 0 分"）。
    单路调用时传进去的就是全量 BM25 分表，所以走 BM25 路但一个查询词都没命中
    的块仍然是 0.0——那是既有的、被回归用例钉住的形状。
    """
    source_id, start, end, quote = collected[index]
    return EvidenceHit(
        source_id=source_id,
        start=start,
        end=end,
        quote=quote,
        score=round(score, 6),
        score_bm25=round(bm25_scores[index], 6) if index in bm25_scores else None,
        score_vector=round(vector_scores[index], 6) if index in vector_scores else None,
        method=method,
    )


def _fuse(
    collected: list[tuple[str, int, int, str]], question: str, top_k: int,
) -> list[EvidenceHit]:
    """对一批块打分（BM25 + 向量，RRF 融合），按分数降序返回**未去重、未截断**的命中。

    这个函数不关心坐标是父块还是子块，它只认 `collected` 里的区间；排序键与
    `_pick_top` 完全一致（同分时短块优先）。

    拆出来是给 `search_parent` 用的：那条路要先把命中折到父块上，再在**父块
    坐标**上去重。如果沿用 `_single_search`（它在传入坐标上就去重了），相邻子块
    那 100 码点的重叠会让两个**不同父块**的命中被当成重复证据丢掉一条——去重的
    顺序在这里不是风格问题，是会少给证据。
    """
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

    # 只走向量路的块 bm25 分是 0 而不是 None：它确实参与了 BM25 打分，
    # 只是没命中任何查询词。None 留给"这条根本没走那一路"。
    bm25_values = {index: lexical[index] for index in fused}
    hits = [_to_hit(index, score, collected, bm25_values, similarity, method)
            for index, score in fused.items()]
    # 同分时短的块优先：整块命中率相同的情况下，短块的信息密度更高。
    # RRF 下这条仍然必要——两路名次互换的组合（1+2 与 2+1）分数完全相同。
    return sorted(hits, key=lambda hit: (-hit.score, hit.end - hit.start))


def _single_search(
    collected: list[tuple[str, int, int, str]], question: str, top_k: int,
) -> list[EvidenceHit]:
    """单路检索（BM25 + 向量，RRF 融合）——`search_sources` 的全部打分逻辑。

    抽成独立函数只为一件事：`search_multi` 在只有一路时必须与 `search_sources`
    **逐字段相同**。两份实现各写一遍排序/截断/去重，迟早会分叉，而那种分叉
    除了回归用例没有别的信号。

    打分融合在 `_fuse` 里，这里只剩"排序 → 去重 → 截断"（`_pick_ordered` 就是
    后两步，排序已在 `_fuse` 里做完）。与拆分之前逐字段相同，`tests/
    test_search_multi.py` 拿 `41dda8e` 的旧模块钉着这条。
    """
    # 排序之后再去重：重叠块会把同一段原文重复推进 top_k，真正不同的证据
    # 反而挤不进来。只在截断前多这一道筛，不改前面任何一步。
    return _pick_ordered(_fuse(collected, question, top_k), top_k)


# ---------------------------------------------------------------- 知识库来源
#
# `library=True` 让一次检索同时看两批来源：**本次请求带来的**（`sources` 参数）
# 与**库里存着的**（`knowledge/library.py`）。库那批不是"把整个库都切块"——那在
# 2C2G 上既慢又贵，而且大部分材料与这次查询无关。两级收窄，每一级都是硬上限：
#
#   1. 按 `last_used_at` 取最近 `LIBRARY_SCAN_LIMIT` 份（读正文，**不切块**）；
#   2. 按查询做一次**字面粗筛**（`_literal_coverage`），取前 `LIBRARY_PICK` 份，
#      只有这些才真正切块、算 BM25、可能调 embedding。
#
# 粗筛只用 `term in text`（C 层的子串搜索），不用 `_token_list`：后者是纯 Python
# 的 bigram 展开，100 份 × 三万字要一秒级，而粗筛只需要"哪几份更可能相关"这个
# 排序信号，不需要精确分值。**它不决定最终名次**——名次仍由 `_single_search` /
# `_fuse` 在真正切块之后给出，粗筛只决定谁进入那一步。
#
# 覆盖率全为 0 时照样取前 `LIBRARY_PICK` 份（`sorted` 稳定，退化成"最近使用的"）：
# 不特判。字面一个字都不沾恰恰是向量路该补的缺口（`docs/RAG-DECISIONS.md` 2.2 的
# 「谁能批钱」），在这里提前砍掉等于把那条路关掉一半。
LIBRARY_SCAN_LIMIT = 100
LIBRARY_PICK = 5


def _digest_of(source: Source) -> str | None:
    """来源的正文指纹；没有正文返回 `None`。

    优先用 `Source.content_hash`（`parse_bytes` 盖的那个），它缺席时按正文重算
    ——手工构造的 `Source`（测试、脚本、agent 内部拼的）都不带指纹，而"这批来源
    里有没有一份已经躺在库中"必须能判出来，否则同一份材料会以两个不同的
    `source_id` 各占一个席位，`top_k` 立刻少一条。
    """
    if source.content_hash:
        return source.content_hash
    text = source.text or ""
    return knowledge_library.content_hash(text) if text.strip() else None


def _literal_coverage(terms: list[str], text: str) -> float:
    """查询词在正文里的字面命中率（`命中词数 / 查询词数`）。

    只看"在不在"，不看次数与位置：粗筛要的是"这份材料值不值得切块"，而不是一个
    可以排序的名次。大小写不折叠——`_token_list` 已经把小写化做在查询侧，正文里
    的 `Budget` 与查询里的 `budget` 命中不上，代价是排序偏后，不是漏掉（最终名次由
    BM25 与向量给，那里两个路都能捞回来）。
    """
    if not text or not terms:
        return 0.0
    hits = sum(1 for term in terms if term in text)
    return hits / len(terms)


def _library_sources(
    question: str, filters: SourceFilter | None, exclude: list[Source],
) -> list[Source]:
    """从库里挑出这次检索要用的来源（含正文），最多 `LIBRARY_PICK` 份。

    顺序是"先筛后切块"的延伸：`filters` 作用在**元数据**上，被筛掉的来源连正文
    都不必进入切块那一步（`load` 出来的 `Source` 已经带着正文，但粗筛与切块都
    不为它发生）。`filter_sources` 只读 `source.metadata`，所以这一步与
    `search_sources` 里的过滤是同一个判断、同一套语义。

    库不可用（磁盘满、文件被锁、表被删）时返回空列表并记 warning：检索退化成
    "只看本次请求的材料"，与加知识库之前一样。这不是静默——日志里有一条，而且
    `method` 与 `source_id` 都会告诉调用方"这次没有库证据"。
    """
    try:
        pool = knowledge_library.recent(limit=LIBRARY_SCAN_LIMIT)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("知识库不可用，本次只检索请求内的来源：%s", type(exc).__name__)
        return []
    if not pool:
        return []

    # 本次请求里已经有的正文不必再从库里取一遍：同一份材料会以两个不同的
    # `source_id`（请求内 uuid4 / 持久 id）各占一个席位，而 `_near_duplicate`
    # 是按 `source_id` 比的，跨来源的重复它拦不住。
    present = {digest for source in exclude if (digest := _digest_of(source))}
    candidates = [item for item in pool
                  if item.content_hash and item.content_hash not in present]
    if filters is not None:
        candidates = filter_sources(candidates, filters)
    if not candidates:
        return []

    terms = list(dict.fromkeys(_token_list(question)))
    if not terms:
        return []
    ranked = sorted(candidates,
                    key=lambda item: -_literal_coverage(terms, item.text or ""))
    return ranked[:LIBRARY_PICK]


def _library_batch(
    question: str, top_k: int, filters: SourceFilter | None, *,
    exclude: list[Source], parent: bool,
) -> list[EvidenceHit]:
    """库那批来源上的检索结果。

    `parent=True` 时走 `_search_parent_core`，与本次请求那批**同一套粒度**：
    `library=True` 的 `search_parent` 两边都是"子块匹配、父块返回"，否则库证据的
    `quote` 会是 450–700 字而请求内的那是同一形状……不，反过来：如果库那批走单级
    块，两边返回的 `quote` 粒度不同，调用方按 `end - start` 判断信息密度时会得到
    两个互相矛盾的结论。粒度必须跟着入口走。
    """
    picked = _library_sources(question, filters, exclude)
    if not picked:
        return []
    knowledge_library.touch([source.id for source in picked])
    if parent:
        return _search_parent_core(picked, question, top_k, None)
    return _search_sources_core(picked, question, top_k, None)


def _merge_batches(
    base: list[EvidenceHit], extra: list[EvidenceHit], top_k: int,
) -> list[EvidenceHit]:
    """把「本次请求的命中」与「库的命中」按 RRF 名次融合，**本次保底占半数席位**。

    结构与 `search_multi` 的第 6 条约束同构，理由也是同一个：纯 RRF 累加会让后来
    的一批把先到的一批顶掉。这里更尖锐——库里的证据可能更多、更同质（同一份制度
    文件被切成几十块），而用户刚上传的那份材料才是这次请求的**对象**。所以
    `base` 的前 `reserve = ceil(top_k/2)` 条先占位，其余名额才由两批竞争。

    分数口径：**所有返回的 `score` 都是 RRF 分**（`1/(RRF_K + 名次)`），因为两批的
    原始分不可比——`search_sources` 的 BM25 分依赖各批自己切出来的块集合（N 与 df
    都不同），跨批比大小没有意义。`score_bm25` / `score_vector` 保留各自的原始值，
    它们本来就是"这条在它自己那批里怎么被打出来的"。

    ⚠ 排序是"保底段 + 竞争段"，而 `score` 是名次倒数，所以**分数不单调**：保底段
    最后一条的分可能低于竞争段第一条。与 `search_multi` 同一条告警——`score` 只用于
    同一次检索内比较大小，不要拿它反推名次。

    跨批去重按 `quote` 全等：两批的 `source_id` 体系不同（请求内 uuid4 / 持久 id），
    `_near_duplicate` 按 `source_id` 比、拦不住这种重复。`quote` 全等是"同一段正文
    被两边各切了一次"的可靠信号——切块算法只有一份，同一段原文切出来的区间也相同。
    """
    if not extra:
        # 库里没有可用证据（或库不可用）：原样返回，**连分数都不重算**。
        # `library=False` 与"库里捞不到东西"在输出上完全一致，这一点是有意的：
        # 用户不该因为"库里有别的材料"而看到同一批命中的分数变了。
        return base
    if not base:
        return [
            hit.model_copy(update={"score": round(1.0 / (RRF_K + rank), 6)})
            for rank, hit in enumerate(extra[:top_k], 1)
        ]

    reserve = min(max(1, (top_k + 1) // 2), len(base))
    kept = list(base[:reserve])
    seen_quotes = {hit.quote for hit in kept}

    contenders: list[tuple[float, EvidenceHit]] = []
    for rank, hit in enumerate(base[reserve:], reserve + 1):
        contenders.append((1.0 / (RRF_K + rank), hit))
    for rank, hit in enumerate(extra, 1):
        contenders.append((1.0 / (RRF_K + rank), hit))
    contenders.sort(key=lambda item: (-item[0], item[1].end - item[1].start))

    fused: list[EvidenceHit] = [
        hit.model_copy(update={"score": round(1.0 / (RRF_K + rank), 6)})
        for rank, hit in enumerate(kept, 1)
    ]
    for score, hit in contenders:
        if hit.quote in seen_quotes:
            continue
        seen_quotes.add(hit.quote)
        fused.append(hit.model_copy(update={"score": round(score, 6)}))
    # `_pick_ordered` 不再排序（顺序就是保底段在前），只做近重复筛与截断。
    return _pick_ordered(fused, top_k)


def _search_sources_core(
    sources: list[Source], question: str, top_k: int,
    filters: SourceFilter | None,
) -> list[EvidenceHit]:
    """`search_sources` 在**给定来源集合内**的全部打分逻辑（不碰库、不做跨批融合）。

    抽出来的唯一目的：`library=True` 要能用**同一个函数**分别处理两批来源。两批各
    写一份实现，结局一定是"库里那批少了一道去重"或者"过滤顺序反了"——那种差异在
    单测里看不出来，只在召回上看得出来。

    `library=False` 时 `search_sources` 就是本函数的转发，所以"默认行为逐字节不变"
    由结构保证（`tests/test_library.py` 另有一组拿 `038e5fe` 旧模块逐字段对比的
    用例把它钉住）。
    """
    if not _token_list(question):
        return []

    # 过滤必须在切块与向量化之前。放在这里还有一个原因：BM25 的 IDF 是在
    # "本次请求切出的块集合"上算的，被筛掉的来源连 N 和 df 都不该污染——
    # 否则同一份材料在"筛"与"不筛"两种请求里会得到不同的分数，
    # 而那个差异没有任何语义。向量侧同理：先筛再算，被筛掉的块连一次缓存
    # 写入都不会发生。注意这与"标签在入库期向量化之后产生"不冲突，见 docstring。
    if filters is not None:
        selected = filter_sources(sources, filters)
        if not selected:
            logger.info("过滤条件筛掉了全部来源（%d→0）", len(sources))
            return []
        if len(selected) != len(sources):
            logger.debug("检索前过滤：来源 %d→%d", len(sources), len(selected))
        sources = selected

    collected = _collect_chunks(sources)
    if not collected:
        return []
    # 打分与融合在 `_single_search` 里：`search_multi` 只有一路时也走它。
    # 两条路径共用同一份实现，"没有扩展"与"只多了一路纠错"才可能逐字段相同。
    return _single_search(collected, question, top_k)


def search_sources(sources: list[Source], query: str, top_k: int = 5, *,
                   filters: SourceFilter | None = None,
                   library: bool = False) -> list[EvidenceHit]:
    """在**本次请求带来的来源**里找相关片段（BM25 + 向量，RRF 融合）。

    默认（`library=False`）只查参数给的这些，不碰任何持久化索引：agent 一次运行
    能看到的证据范围必须与它实际拿到的来源严格一致，否则它引用的东西在界面上
    根本点不到。向量缓存不破例——那只是把同一段文本的向量复用，候选集仍然只来自
    `sources`。

    `library=True` 是这条规则的**显式例外**：候选集会加上知识库里相关的来源
    （`knowledge/library.py`）。例外之所以站得住，是因为库命中的 `source_id` 是
    持久 id，调用方拿它 `library.load()` 一定回查得到——"看得见就必须点得到"没有
    被打破，只是"点得到"的范围从本次请求扩到了库里。这也是它默认关着的理由：
    例外必须由调用方显式打开，而且那个调用方还得负责把库来源一并交给引用校验。

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

    `filters` 是**检索前置**的元数据过滤，语义见 `SourceFilter`。三条硬要求：
      - 先筛来源、再切块：被筛掉的来源不产生任何块，也就不会有 BM25 分和
        embedding 调用。反过来（先切块再逐块过滤）会白算一遍再扔掉，
        向量路还要为这些块真金白银地付费。
      - 筛完没有来源时返回 `[]` 并记一条 info 日志，**不抛错**。调用方
        （`_rag`）本来就把它当"没找到"处理，这是可接受的降级。
      - `filters=None` 时整段跳过，行为与加过滤之前逐字节一致。

    ⚠ **两个阶段的顺序不要读反**（将来做落库的人最容易在这里踩坑）：
      - **入库期**（预期的形态，本函数不参与）：切块 → 向量化 → 打标。
        业务分类若要用向量推断（聚类、与类别描述的相似度做 zero-shot），
        必须等向量算完才判得了，所以**标签是向量化之后的产物**。
      - **检索期**（本函数）：按标签筛来源 → 切块 → 检索。
        当前没有"入库"这一步，每次请求都是现切现算；先 embed 再筛，被筛掉
        那几份的向量就是纯浪费，而且它们的块会被写进 `vector_cache`
        （按文本哈希缓存，将来就是 `chunk_vectors` 里那些永远查不到的行）。
    两者不矛盾：标签在**入库期**产生、随 `Source` 一起带到每次请求；过滤在
    **检索期**执行。同一份标签，不同阶段各用一次。

    `library=True` 时**额外**检索知识库里与查询相关的来源（选法与上限见
    `LIBRARY_SCAN_LIMIT` / `_library_sources`），两批按 RRF 名次融合，
    **本次请求的命中保底占 `ceil(top_k/2)` 席**（`_merge_batches`）。三条要点：

      - 默认 `library=False` 时只是 `_search_sources_core` 的转发，行为与加这个
        参数之前**逐字节一致**（`tests/test_library.py` 拿 `038e5fe` 的旧模块钉着）；
      - 库命中的 `source_id` 是**持久 id**，调用方可拿它去 `library.load()` 回查；
        本次请求那批命中保留请求内的 `source_id`——那条 id 是 `RunContext`、
        `verify_source_refs` 与界面高亮共用的键，换成持久 id 会让同一份来源在
        引用链上出现两个身份；
      - 库里抽不到东西（空库 / 库不可用 / 被 `filters` 筛光 / 本次材料已覆盖）
        时输出与 `library=False` **完全相同**，连 `score` 都不重算——不该因为
        "库里有别的材料"而让同一批命中看起来变了。
    """
    question = query.strip()
    base = _search_sources_core(sources, question, top_k, filters)
    if not library:
        return base
    extra = _library_batch(question, top_k, filters, exclude=sources, parent=False)
    return _merge_batches(base, extra, top_k)


# ---------------------------------------------------------------- 小块检索、大块召回


@dataclass(frozen=True)
class _LeafCorpus:
    """一次两级切块的产物：喂给打分的子块 + 子块到父块区间的对照表。

    `items` 与 `parents` **按下标对齐**：`items[i]` 是第 i 个子块的
    `(source_id, start, end, quote)`（形状与 `_collect_chunks` 一致，好让同一个
    `_fuse` 直接吃），`parents[i]` 是它的父块区间。

    `texts` 留着原文是为了折父块时切片——`quote` 必须是 `text[父.start:父.end]`
    逐字，不能拿几个子块的文本拼出来：拼接会在重叠处重复、在无重叠处留下接缝，
    而 `quote` 的**唯一**用途就是"给模型读的连续上下文 + 给界面做高亮"。
    """

    items: list[tuple[str, int, int, str]]
    parents: list[tuple[int, int]]
    texts: dict[str, str]


def _collect_leaves(sources: list[Source]) -> _LeafCorpus:
    """切一遍两级树，只留下子块——父块的原样保留由 `_fold_to_parents` 负责。

    空判断与 `_collect_chunks` 一致（`strip` 判空、切块用原文），否则一份只有
    空白的来源会产出一个零宽父块，进而在打分时变成"一个什么都没匹配到的块"，
    抬高 N 却没有任何召回价值。
    """
    items: list[tuple[str, int, int, str]] = []
    parents: list[tuple[int, int]] = []
    texts: dict[str, str] = {}
    for source in sources:
        text = source.text or ""
        if not text.strip():
            continue
        texts[source.id] = text
        tree = _chunk_tree(text)
        for node in tree:
            # level=0 的父块不参与匹配：它存在的意义只有"被子块找到之后被返回"。
            if node.parent is None:
                continue
            parent = tree[node.parent]
            items.append((source.id, node.start, node.end, text[node.start:node.end]))
            parents.append((parent.start, parent.end))
    return _LeafCorpus(items=items, parents=parents, texts=texts)


def _fold_to_parents(ranked: list[EvidenceHit], corpus: _LeafCorpus) -> list[EvidenceHit]:
    """把子块命中折成父块命中：区间换父块、quote 取父块全文、分数留在子块。

    `ranked` 必须**按分数降序**（`_fuse` 的输出就是这样）：同一父块的第一条
    就是它分数最高的那条子块，所以"多个子块命中同一父块时只保留分数最高的"
    由遍历顺序保证，不需要比较、也不会被后面更低的分数覆盖。

    去重的键是**父块区间**而不是子块下标：一个父块通常有 2–4 个子块，按子块下标
    去重等于没去重（每个子块都是不同的下标），而"同一父块只留一条"正是这一步的
    目的。也**不能**指望后面 `_pick_ordered` 的近重复判定兜住——那一层按重叠率
    筛，父块很短时会把两个不同父块误判成重复，两种错法的方向正好相反。

    为什么必须合并：父块是 450–700 字，子块只有 200 上下，一个父块通常有 2–4
    个子块。不合并的话，`top_k=5` 会被同一个父块的三四条子命中占满，模型看到
    的五条"不同证据"其实是同一段上下文的三次摘录——这比块大导致的噪声更糟，
    因为它让"去重后的多样性"这个 top_k 的隐含承诺直接失效。

    分数的口径**不跟着区间变**：`score` / `score_bm25` / `score_vector` 都是
    子块匹配的结果，父块本身没有参与打分。父块是"返回窗口"，不是"评分单元"。

    `leaf_start` / `leaf_end` 记下真正命中的子块区间。它和 `start`/`end` 一起
    构成"证据的两级坐标"：外层是给模型读的上下文，内层是这次匹配成立的范围。
    引用校验要靠它区分"引用落在命中的那一段"与"引用落在父块里与查询无关的
    另一半"（详见 `optimization/source_refs.py`）。
    """
    position = {(item[0], item[1], item[2]): index
                for index, item in enumerate(corpus.items)}
    folded: list[EvidenceHit] = []
    seen: set[tuple[str, int, int]] = set()
    for hit in ranked:
        index = position[(hit.source_id, hit.start, hit.end)]
        start, end = corpus.parents[index]
        key = (hit.source_id, start, end)
        if key in seen:
            continue
        seen.add(key)
        folded.append(hit.model_copy(update={
            "start": start,
            "end": end,
            "quote": corpus.texts[hit.source_id][start:end],
            "leaf_start": hit.start,
            "leaf_end": hit.end,
        }))
    return folded


def _fold_multi_hits(
    hits: list[EvidenceHit],
    collected: list[tuple[str, int, int, str]],
    corpus: _LeafCorpus,
    primary: dict[int, float],
    want: int,
) -> list[EvidenceHit]:
    """多路子块命中 → 父块，并把**保底席位**在父块坐标上重算一次。

    只在 `_search_multi_core` 的子块模式下用（`parent=True`）。两条顺序约束，
    各对应一种能静默出错的做法：

      1. **先按融合分降序、再折**。`_fold_to_parents` 靠"第一条"保证同一父块留下
         分数最高的那个子块（那个前提写在它的 docstring 里）。先分段（保底段在
         前）再折，会让"被保底、但子块分更低"的那条顶掉同一父块里分更高的子块，
         于是 `leaf_start/leaf_end` 指向的不是最相关的那 200 字——引用校验会拿
         一段错区间去比，而它不会报错，只会给出错误的判断。
      2. **保底席位在折叠之后算**。席位是"回给调用方的一条证据"的席位，一条证据
         是**父块**：在子块坐标上保 `ceil(want/2)` 席，折完可能只落在两三个父块
         里，实际席位被折叠静默削掉——不变量看着还在，效果没了。

    所以保底的判据换成"原 query 那一路**在这个父块里**最好的那个子块分"
    （`parent_primary`）：子块级保底段原本要表达的也正是这件事——"这条是用户原话
    自己找到的，别被扩展路的票数顶掉"。

    `parent_primary` 取 max 而不是相加：同一父块里两个子块都被原 query 命中，
    说明的是"这个父块被命中了两次"，不是"它有两条独立证据"。相加会让子块多的
    长父块系统性压过子块少的短父块，而那正是这一轮要摆脱的长度偏差。

    返回长度 `<= want`、每条对应**不同父块**，顺序是"保底段 + 竞争段"（与
    `_search_multi_core` 的单级模式同一条口径）。与 `search_parent` 一样**不跑**
    `_pick_ordered` 的近重复筛：`_fold_to_parents` 已经保证父块互不相同，再按
    重叠率筛一遍只会把两个**不同**父块判成重复（父块短于 `2*CHUNK_OVERLAP_CHARS`
    时必然触发），净效果是静默少给一条证据。
    """
    if not hits:
        return []
    # 折叠的前提：按分数降序。排序键与 `_fuse` 完全一致（同分时短块优先）。
    ranked = sorted(hits, key=lambda hit: (-hit.score, hit.end - hit.start))
    folded = _fold_to_parents(ranked, corpus)
    # 原 query 一路都没命中（`primary` 为空）时没有"保底"可言：竞争段的顺序
    # 就是融合分的顺序，原样截断。
    if not folded or not primary:
        return folded[:want]

    index_of = {(item[0], item[1], item[2]): index
                for index, item in enumerate(collected)}
    parent_primary: dict[tuple[str, int, int], float] = {}
    for index, value in primary.items():
        source_id, start, end, _quote = collected[index]
        parent_start, parent_end = corpus.parents[index_of[(source_id, start, end)]]
        key = (source_id, parent_start, parent_end)
        if value > parent_primary.get(key, 0.0):
            parent_primary[key] = value

    # 席位按**候选池**（`want`）而不是 `top_k` 算，理由与原实现相同：扩池后若仍只
    # 保 `ceil(top_k/2)` 席，原始问题自己的第 2、3 名会被更多扩展路挤出去。
    # 上界取 `len(parent_primary)`：原 query 没碰过的父块不该占保底席位。
    reserve = min(max(1, (want + 1) // 2), len(folded), len(parent_primary))
    order = sorted(
        folded,
        key=lambda hit: (
            -parent_primary.get((hit.source_id, hit.start, hit.end), 0.0),
            -hit.score,
            hit.end - hit.start,
        ),
    )
    kept = order[:reserve]
    taken = {(hit.source_id, hit.start, hit.end) for hit in kept}
    rest = [hit for hit in folded
            if (hit.source_id, hit.start, hit.end) not in taken]
    return [*kept, *rest][:want]


# ---------------------------------------------------------------- 召回后的重排
#
# 扩池与重排是**两件事**，必须一起做才有意义：`top_k` 条候选上来就开始重排，
# 能做的只有换顺序；而 R@1 要涨，靠的是"把原本排第 6 的那条抬到第 1"。
#
# 扩池放在最外层而不是各 core 里，是为了让 `search_multi` 的"多路各自重排"变成
# "融合之后重排一次"——N 路各调一次 reranker 既贵 N 倍，也会让每一路都看不到
# 别的路捞回来的候选（而多路的价值恰恰在那些候选上）。


def _rerank_depth(top_k: int) -> int:
    """这一次检索的候选池大小：真会重排时扩到 `rerank.pool_size(top_k)`，否则 `top_k`。

    **两个条件缺一不可**：开关打开 **且** 凭证齐备。少了任何一个就只取 `top_k`。
    这不是省事的写法，而是"关着时逐字节一致"的构造保证：扩池会改变每路截断的
    `pool` 与 RRF 名次映射（见 `_fuse` / `_search_multi_core`），那是**分数口径**
    的变化，不只是"多了几条候选"。没配 key 时白扩池会让同一批命中的 `score`
    悄悄变一个数——那比不扩池糟得多。
    """
    if top_k > 0 and knowledge_rerank.enabled() and knowledge_rerank.configured():
        return knowledge_rerank.pool_size(top_k)
    return max(top_k, 0)


def _rerank_top(
    question: str, hits: list[EvidenceHit], top_k: int,
) -> list[EvidenceHit]:
    """候选池 → 重排 → 截断 `top_k`。开关关着（或没配 key）时等价于 `hits[:top_k]`。

    `hits` 已经是**融合与去重之后**的顺序（`_pick_ordered` / `_fold_to_parents`
    出来的），所以这里不需要再筛一次近重复——重排只换顺序，不动候选集合。
    """
    if _rerank_depth(top_k) <= top_k or not question:
        return hits[:top_k]
    return knowledge_rerank.rerank(question, hits, top_k)


def _search_parent_core(
    sources: list[Source], question: str, top_k: int,
    filters: SourceFilter | None, *, depth: int | None = None,
) -> list[EvidenceHit]:
    """`search_parent` 在**给定来源集合内**的全部逻辑：切两级树、子块匹配、折父块。

    不含开关判断（`search_parent` 自己那一步），也不做跨批融合。抽出来的理由与
    `_search_sources_core` 完全相同：`library=True` 时两批来源必须走同一份实现，
    否则"本次那批返回父块、库里那批返回单级块"这种粒度错位不会有任何报错，只会在
    调用方按 `end - start` 估信息密度时给出两个互相矛盾的结论。

    `depth` 是**返回条数**（候选池），缺省等于 `top_k`。只给重排用：`depth > top_k`
    时本函数返回一个还没截到 `top_k` 的池子，交给 `_rerank_top` 挑。两处随它一起
    放大——子块级的融合池（`depth * CANDIDATE_FACTOR`）与折父块后的截断——否则
    池子会被其中任何一处卡回 `top_k`，"扩池"等于没发生。
    """
    want = top_k if depth is None else depth
    if not _token_list(question):
        return []

    # 过滤必须在切块与向量化之前，理由与 `search_sources` 完全一致：被筛掉的
    # 来源不该污染 BM25 的 N/df，也不该产生 embedding 调用与缓存写入。
    if filters is not None:
        selected = filter_sources(sources, filters)
        if not selected:
            logger.info("过滤条件筛掉了全部来源（%d→0）", len(sources))
            return []
        if len(selected) != len(sources):
            logger.debug("检索前过滤：来源 %d→%d", len(sources), len(selected))
        sources = selected

    corpus = _collect_leaves(sources)
    if not corpus.items:
        return []

    # 候选池放大：子块级别多取一些，是因为折父块时同一父块的多条会并成一条。
    # 只取 top_k 条子块时，它们很可能全落在两三个父块里，折完凑不满 top_k。
    # 放大**不改**已在候选集里的分数（RRF 只吃名次），只是让更多子块有名字可排。
    ranked = _fuse(corpus.items, question, want * CANDIDATE_FACTOR)
    # 折父块 → 截断。折完就不再跑 `_pick_ordered` 的近重复筛：`_fold_to_parents`
    # 已经保证每条对应不同的父块，再按"重叠 > 较短者一半"筛一遍，只会在父块短于
    # 2×`CHUNK_OVERLAP_CHARS`（200 字）时把两个**不同**父块判成重复，净效果是
    # 静默少给一条证据。截断就是这里需要的全部裁剪。
    return _fold_to_parents(ranked, corpus)[:want]


def search_parent(sources: list[Source], query: str, top_k: int = 5, *,
                  filters: SourceFilter | None = None,
                  library: bool = False) -> list[EvidenceHit]:
    """**用子块匹配、把父块回给调用方**（small-to-big / parent-document retrieval）。

    与 `search_sources` 的差别只有粒度，不是另一套检索：

      - 匹配发生在**子块**上（`LEAF_*`，约 200 字）。子块短，查询词在块内的
        占比高、长度归一化的惩罚小，同一段原文不会因为有半页无关内容而掉名次；
        向量路的余弦也不会被跑题段落稀释。
      - 返回的是**父块**（`CHUNK_*`，450–700 字），`start`/`end`/`quote` 都取
        父块区间——命中之后模型读到的是完整的前因后果，而不是半句话。

    **分数来自子块匹配，区间来自父块**。`score` 是命中子块的 RRF 分（或单路
    BM25 分），`score_bm25` / `score_vector` 同理；父块区间只是返回口径。两者
    不要混着读：分数高说明"这段原文里有一小段精准命中"，不代表整个父块都相关。
    `score` 仍然是**只在本次检索内可比**（IDF 与名次都依赖这次请求切出的子块
    集合），跨请求比较看 `method` 与两路原始分。

    **`start/end/quote` 是父块，`leaf_start/leaf_end` 是命中的子块**。两级坐标
    一起给出，"这句话为什么被找出来"才可核验：只给父块时，从 500 字里随手引一句
    也能通过逐字校验，引用校验会退化成格式检查。给调用方的建议写在
    `optimization/source_refs.py::EvidenceSpan` 上。

    三条与 `search_sources` 对齐的约定：
      - `filters` 依旧是**检索前置**过滤（先筛来源再切块），语义见 `SourceFilter`；
        筛光时返回 `[]` 并记 info，不抛错。
      - 返回条数 `<= top_k`，且每条对应**不同的父块**。去重只做"同一父块并一条"
        （`_fold_to_parents`），**不做**基于重叠率的近重复筛：那一道在父块坐标上
        会把两个不同父块误判成重复（父块短于 `2×CHUNK_OVERLAP_CHARS` 即 200 字时
        必然触发），净效果是静默少给一条。
      - **不要把返回条数当成证据充分性**。父块合并是减法：子块/父块比实测 2.4–2.7，
        材料越同质（长合同、连续制度），`top_k` 条子块命中越可能只覆盖两三个父块，
        于是折完 `len(hits) < top_k`。这是合并的固有代价，不是"材料里没东西"——
        判断材料够不够，看 `score` 与 `score_bm25` 是否为正、以及命中的父块之间
        是否真的换了话题，不要看条数。

    开关 `MINDMAP_PARENT_RETRIEVAL` 默认 **1**（开）。关着时**直接返回
    `search_sources(...)` 的结果**——不是"照着它再实现一遍"，是同一个函数，
    所以逐字段相同由构造保证（`tests/test_parent_retrieval.py` 拿 `f72c14c`
    的旧模块钉着这条）。

    为什么从"默认关"翻成"默认开"：原注释说这个能力是为"只给片段"的形态准备的，
    那时返回的块就是模型能看到全部上下文，粒度不够就真的读不懂——而
    `MINDMAP_SOURCE_MODE` 已经默认 `rag`，那个形态就是现在的默认形态。另一半
    顾虑（父块里与查询无关的段落也能通过逐字校验）已经有对策：命中的子块区间随
    hits 累积进 `RunContext.evidence`，生成收尾时交给
    `verify_source_refs(..., evidence=...)` 标 `SOURCE_REF_OUTSIDE_EVIDENCE`
    （warn，不剔除）。也就是说，"默认开"这件事**依赖**接线先落地，两者不能反过来做。

    `library=True` 与 `search_sources` 的同名参数逐条对齐（选法、上限、排重、
    保底席位都由 `_library_batch` / `_merge_batches` 负责），唯一区别是库那批也走
    **同一套粒度**：子块匹配、父块返回。开关关着（`MINDMAP_PARENT_RETRIEVAL=0`）时
    本函数转发给 `search_sources`，`library` 一并透传——"关掉父块模式"只该改变
    粒度，不该顺带把知识库关掉。

    **重排（`MINDMAP_RERANK`，默认 1）**在这里接：`_rerank_depth(top_k)` 把候选池
    放宽到 `max(top_k*4, 10)` 条，`_rerank_top` 在**融合与去重之后、截断之前**
    把池子重排并截回 `top_k`。三条边界写在这里：

      - `library=True` 时仍然**只重排一次**：库那批与本地那批各自扩池，`_merge_batches`
        用 `depth` 而不是 `top_k` 做跨批融合的截断，重排只发生在融合之后
        （`_rerank_top(question, _merge_batches(base, extra, depth), top_k)`）。
      - `MINDMAP_PARENT_RETRIEVAL=0` 时本函数转发给 `search_sources`，**不重排**：
        那条路的入口是 `search_sources`，而重排只接在 `search_parent` 与
        `search_multi` 上。要重排又不要父块粒度，请在 `search_sources` 上接——现在
        没接，因为单级块的 `quote` 只有 200 字，重排的收益与它带来的多样性损失
        还没量过，不猜。
      - 重排只改顺序、附加 `rerank_score`，`start`/`end`/`quote`/`leaf_*`
        **一个都不动**（"只重排不改变证据"由 `rerank.rerank` 的 `model_copy` 保证）。

    **图游走（`MINDMAP_GRAPH_SEARCH`，默认 0）**接在本函数的两个出口上（父块模式开着
    与关着各一个）：文本路先跑完、重排完，`_with_graph` 再用 `graph_search.search_graph`
    捞一遍图证据并 `fuse_hits` 融合。四条边界：

      - 默认关；关着时 `_with_graph` **返回同一个列表对象**，结果与没接图逐字段相同。
      - 图路拿不到东西（没配 Neo4j / query 没有实体命中 / 没有路径）同样原样返回。
      - 图证据在重排**之后**进来，所以这一轮的 reranker 看不到它们（图命中没有可比的
        分数，塞进重排只会让它的 [0,1] 概率与名次混在一起）。要重排图证据是另一轮的事。
      - 融合后 `score` 一律变成 RRF 分（图命中没有可加的原始分），`origin="graph"`
        标出哪几条是图带来的——`method` 不动。
    """
    if not _parent_retrieval_enabled():
        hits = search_sources(sources, query, top_k, filters=filters, library=library)
        return _with_graph(hits, sources, query.strip(), top_k, filters)

    question = query.strip()
    depth = _rerank_depth(top_k)
    base = _search_parent_core(sources, question, top_k, filters, depth=depth)
    if not library:
        hits = _rerank_top(question, base, top_k)
    else:
        extra = _library_batch(question, depth, filters, exclude=sources, parent=True)
        hits = _rerank_top(question, _merge_batches(base, extra, depth), top_k)
    return _with_graph(hits, sources, question, top_k, filters)


# ---------------------------------------------------------------- 图游走接线


def _with_graph(
    text_hits: list[EvidenceHit],
    sources: list[Source],
    question: str,
    top_k: int,
    filters: SourceFilter | None,
) -> list[EvidenceHit]:
    """文本路的命中 → （开关打开且图可用时）与图路融合；否则**同一个列表原样返回**。

    三条"不做任何事"的分支刻意写成提前返回**同一个对象**（不是 `list(...)` 拷贝）：
    "开关关着 / 图没配 / 图里没实体命中"这三种情况在输出上必须与没接图时**逐字段
    相同**，连列表身份都相同最省事——回归用例可以拿 `is` 断言。

    `graph_store` 与 `graph_search` 都是**函数内延迟 import**：`service` 是这一层的
    入口，被 agent、脚本、几乎每个测试 import；为了一个默认关着的开关在 import 期
    把 `neo4j` 拖进依赖图，会让"没配图"的部署在 import 阶段就承担它不需要的成本。
    `library._chunk_rows` 对 `service._chunk_spans` 用的是同一手法，理由也相同（反向
    import 成环）。

    **异常一律降级**：Neo4j 抛错、库里读不到、区间校验失败，都只记一条 warning 然后
    返回文本结果。图是"补证据"的通道，它坏掉不该让整次检索失败——这与
    `graph_store` 读接口"查不到与库坏了同一个结果"的设计是同一条原则。
    """
    if not _graph_search_enabled() or not question:
        return text_hits
    from app.knowledge import graph_search, graph_store

    if not graph_store.configured():
        return text_hits
    try:
        graph_hits = graph_search.search_graph(
            sources, question, graph_search.graph_pool(top_k), filters=filters)
        if not graph_hits:
            return text_hits
        fused = graph_search.fuse_hits(text_hits, graph_hits, top_k)
        logger.debug("图检索：图证据 %d 条 → 融合后 %d 条", len(graph_hits), len(fused))
        return fused
    except (OSError, ValueError, sqlite3.Error) as exc:
        # `GraphStoreError` 是 `OSError` 的子类（见 `graph_store` 模块 docstring），
        # 所以这里不需要单独 import 它——写 `OSError` 还顺手接住了 `library.load`
        # 的磁盘错误。
        logger.warning("图检索降级为纯文本（%s）", type(exc).__name__)
        return text_hits


# ---------------------------------------------------------------- 多路并行检索

# 单次检索最多跑几路。2C2G + 5 秒预算：每多一路就多一次 embedding 往返，
# 6 路是"还等得起"的上限；块集合只有那么大，再多也不会凭空长出候选。
MAX_ROUTES = 6


def _route_queries(queries: list[str], hyde: str | None) -> list[tuple[str, bool]]:
    """把查询列表理成 `[(文本, 是否走 BM25)]`：去空白、去重保序、截断到 `MAX_ROUTES`。

    三条硬规则：

    1. **原 query 由调用方放在第一位，这里不补**。一个"自己会加一路"的函数会让
       调用方无法判断实际检索了几次，也会在开关开与关之间留下一个谁也说不清的
       差集。`queries` 清空就整体返回空——连 `hyde` 都不算一路：没有原问题在
       场，多路召回就失去了安全网（`docs/RAG-DECISIONS.md` 2.3：改写器会把
       「退款率怎么算」改成「订单量怎么算」，只有原句在场才拉得回来）。
    2. **`hyde` 是向量专用路**（`use_bm25=False`）。它是模型编的假答案，字面
       与材料不沾；拿它当关键词查，BM25 会被长文本里偶然撞上的常用 bigram
       喂出一堆正分噪声，白占名次。
    3. **截断时 `hyde` 让位**。真实问法（intents/subqueries）比一段假答案有
       价值，而 HyDE 本来就是几个方案里收益最低、延迟最高的那个
       （`docs/RAG-DECISIONS.md` 第 3 节：+2~8 点、+800ms）。
    """
    routes: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for raw in queries or []:
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text or text in seen or not _token_list(text):
            continue
        seen.add(text)
        routes.append((text, True))
    if not routes:
        return []
    if hyde:
        text = hyde.strip()
        if text and text not in seen:
            routes.append((text, False))
    if len(routes) > MAX_ROUTES:
        logger.debug("多路检索截断：%d→%d 路（hyde 在末尾，先让位）",
                     len(routes), MAX_ROUTES)
        routes = routes[:MAX_ROUTES]
    return routes


async def _route_vectors(
    quotes: list[str], queries: list[str],
) -> list[tuple[dict[int, float], str]]:
    """并行算每路查询与所有块的余弦，返回 `[(块下标→相似度, 不可用原因)]`。

    **进线程池的只有 HTTP**。`embed_texts` 是同步的（内部每次自建
    `httpx.Client`，没有共享状态），它的往返必须离开事件循环。缓存读写留在主
    线程是**工程选择，不是安全限制**：`vector_cache._open()` 每次新建连接、用完
    即关，整段操作在同一个线程内完成，放进线程池也不会出错。留在主线程的理由
    只有两条——它是本地毫秒级操作，调度开销大于收益；并发写会去争 SQLite 的
    写锁（`timeout=10` 兜底，不会崩，但也没有收益）。把线程池的职责限死在网络
    往返上，并发度才好推理：六路并行就是六个网络往返，没有隐藏的锁竞争。顺序：

        主线程查缓存 → 线程池并行 embed 缺失项 → 主线程写缓存 → 主线程算余弦

    并行的**实际路数受服务端配额影响**，不受这里控制：`embedding._post` 只对
    5xx 与超时重试，429 直接抛 `EmbeddingError`，那一路当场退成纯 BM25。配额
    紧张时六路里可能只有两三路真拿到向量——这是约束 6 说的可接受降级，不是故障。

    每条 query 单独一次调用、并发发起，而不是拼成一大批：多路的价值就在"同一
    时间窗内拿到几路的候选"，拼成一批等于把它们重新串行化。块文本只 embed
    一次（多路共享同一份块集合），否则 N 路各付一遍块向量的钱。

    失败按路退化：某一路的查询向量拿不到，只有那一路没有向量名次，其余路照常。
    """
    if not embedding_configured():
        return [({}, "unconfigured") for _ in queries]

    started = time.perf_counter()
    model = embedding_model()
    quote_hashes = [vector_cache.text_hash(quote) for quote in quotes]
    query_hashes = [vector_cache.text_hash(query) for query in queries]

    # 主线程：一次查缓存，覆盖块与所有 query（同一段文本只查一次）
    cached = vector_cache.get_vectors(
        list(dict.fromkeys([*quote_hashes, *query_hashes])), model)
    cached_hits = len(cached)

    pending: set[str] = set(cached)
    pending_queries: list[tuple[str, str]] = []
    for hash_value, text in zip(query_hashes, queries):
        if hash_value not in pending:
            pending.add(hash_value)
            pending_queries.append((hash_value, text))
    pending_chunks: list[tuple[str, str]] = []
    for hash_value, text in zip(quote_hashes, quotes):
        if hash_value not in pending:
            pending.add(hash_value)
            pending_chunks.append((hash_value, text))

    jobs = [("query", hash_value, asyncio.to_thread(embed_texts, [text]))
            for hash_value, text in pending_queries]
    if pending_chunks:
        jobs.append(("chunk", "", asyncio.to_thread(
            embed_texts, [text for _, text in pending_chunks])))

    fresh: dict[str, list[float]] = {}
    failed: set[str] = set()
    if jobs:
        results = await asyncio.gather(*(job for _, _, job in jobs),
                                       return_exceptions=True)
        for (kind, hash_value, _), result in zip(jobs, results):
            # 取消是我方被取消（客户端断开），不是 embedding 坏了：吞掉它会让
            # 上层以为任务正常跑完了。
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                if not isinstance(result, EmbeddingNotConfigured):
                    logger.warning("多路向量路降级（%s）：%s：%s",
                                   "query" if kind == "query" else "chunk",
                                   type(result).__name__, str(result)[:160])
                # 预检与调用之间凭证被清掉：并发下真会发生，不算故障，不记日志。
                if kind == "query":
                    failed.add(hash_value)
                continue
            vectors = result
            if kind == "query":
                vector = vectors[0] if vectors else None
                if vector:
                    fresh[hash_value] = vector
                else:
                    failed.add(hash_value)
                continue
            for (chunk_hash, _text), vector in zip(pending_chunks, vectors):
                if vector:
                    fresh[chunk_hash] = vector

    # 主线程：写缓存。写在这里而不是线程里，见本函数开头那条顺序。
    if fresh:
        cached.update(fresh)
        vector_cache.put_vectors(list(fresh.items()), model)

    similarity_routes: list[tuple[dict[int, float], str]] = []
    for query_hash in query_hashes:
        query_vector = cached.get(query_hash)
        if not query_vector:
            similarity_routes.append(
                ({}, "error" if query_hash in failed else "query_failed"))
            continue
        similarity: dict[int, float] = {}
        for index, hash_value in enumerate(quote_hashes):
            vector = cached.get(hash_value)
            if not vector or len(vector) != len(query_vector):
                # 维度对不上（换了模型但缓存没清）就跳过，绝不截断对齐
                continue
            value = sum(left * right for left, right in zip(vector, query_vector))
            # 余弦 ≤ 0 表示正交或反向：字面无关、语义也无关，不该占一个名次
            if value > 0:
                similarity[index] = value
        similarity_routes.append((similarity, "" if similarity else "no_signal"))

    # 只记条数与耗时。块文本是材料原文，一律不进日志。
    logger.info("多路向量路：路 %d / 块 %d / 缓存命中 %d / 并发 embed %d / 耗时 %.0fms",
                len(queries), len(quotes), cached_hits, len(jobs),
                (time.perf_counter() - started) * 1000)
    return similarity_routes


async def search_multi(
    sources: list[Source],
    queries: list[str],
    top_k: int = 5,
    *,
    filters: SourceFilter | None = None,
    hyde: str | None = None,
    library: bool = False,
) -> list[EvidenceHit]:
    """在**同一批来源**上并行跑 N 个 query，按 RRF 名次把各路**累加**后融合。

    与 `search_sources` 的分工：那个是"一个问题查一次"，这个是"一组问法查一次
    材料"。**它不生成 query**——传哪些问法由调用方（`_rag`）决定，这里只管把
    它们跑完、融合、去重。

    ## 粒度：与 `search_parent` 同构

    `MINDMAP_PARENT_RETRIEVAL=1`（默认）时，本函数与 `search_parent` **同一个
    匹配口径**，只是多了"多路"：

      1. **匹配在子块上**（`_collect_leaves`，约 200 字）。每路 query 各自排序；
      2. **融合也在子块上**（RRF 累加）——先把每一路各自的命中折成父块再融合，
         就等于把"哪个父块"当成候选单位，而一个 600 字的父块里常混着三四个话题，
         "这一小段最相关"的细粒度信号会在折叠那一步被抹掉；
      3. **折叠在融合之后**（`_fold_multi_hits` → `_fold_to_parents`）。同父块的
         多个子块并成一条，保留分数最高的那个叶子区间（`leaf_start/leaf_end`）；
      4. **重排接在折叠之后**，与 `search_parent` 完全一致（`_rerank_depth` 扩池、
         `_rerank_top` 截回 `top_k`、失败降级原顺序）。

    这不是"顺手升级"，是修一个实测差异：父块当匹配单元时，同一份材料、同样 8 支、
    纯 BM25 的覆盖率比子块口径**低 5.7pp**（23.1% vs 28.8%）。原因有两层——父块受
    BM25 长度归一化（`b=0.75`）惩罚更重，且 600 字里混着几个话题，信号被稀释。
    那个 -5.7pp 与"查询扩展有没有收益"混在一起量时，会把扩展的真实收益冲掉，
    所以必须先把这个入口修平，再谈扩展。

    `MINDMAP_PARENT_RETRIEVAL=0` 时本函数与加多路之前**逐字节相同**（单级块匹配、
    `_pick_ordered` 去重、`leaf_*` 恒为 None）：粒度开关关着，多路不该单方面改变
    返回形状。`library=True` 时库那批也跟着这个开关走（`_library_batch(parent=)`），
    否则两批的 `quote` 粒度不同，调用方按 `end - start` 估信息密度会得到两个互相
    矛盾的结论。

    `queries` 里的每条问法都会各自产出 BM25 与向量两路名次，同一块被多路命中
    就把 `1/(RRF_K+名次)` 一路一路加起来。这是多路唯一的价值来源：单路里
    排第二的块，如果在三种问法下都排第二，它比只在一种问法下排第一的块更可能
    是答案。**但累加只决定"非保底席位"的归属**——原始问题那一路的前几名先占位，
    否则票数会压过用户原话（约束 6）。`hyde` 是单独的一路，**只算向量**：它是
    模型编的假答案，字面与材料不沾，拿它当关键词查只会把噪声喂进融合（见
    `_route_queries`）。

    七条硬约束，逐条对应一个具体的浪费或错误（第 6 条是实测退化之后补的，
    其余六条是设计时就定下的）：
    1. **切块只做一次**。颗粒切分在分路之前跑一遍，六路共用这份块集合。这也让
       每路的 BM25 用同一个 `N` 与 `df`——同一块在不同 query 下的分数才有同一种
       口径。切哪一级由 `MINDMAP_PARENT_RETRIEVAL` 决定（见下面的"粒度"一节）。
    2. **并行只在 HTTP 上**。`embed_texts` 是同步的，靠 `asyncio.to_thread`
       离开事件循环；缓存读写留在主线程是工程选择（本地毫秒级操作，扔线程池
       没有收益还要争写锁），不是 SQLite 的连接限制（详见 `_route_vectors`）。
       并行的实际上限由**服务端配额**决定：429 不重试，那一路当场退成纯 BM25，
       所以"六路"是发起上限，不是成功上限。
    3. **原 query 不补**。`queries` 为空就返回 `[]`。原问题必须由调用方放进
       第一位，这是 `plan_query` 那道 `corrected` 闸之外的第二层安全网：闸漏了
       一句同句式换中心词的改写时，只要原句还在，召回整体就不会跑偏。
    4. **最多 6 路**（`MAX_ROUTES`），超了截断并记一条 debug。
    5. **融合复用现成的 RRF**：`_bm25_ranking` / `_vector_ranking` /
       `_rrf_value` / `_pick_ordered` / `_near_duplicate` / `_fold_to_parents`，
       不另起一套。每路先按 `pool = max(want * CANDIDATE_FACTOR, want)` 截断再
       融合，与单路同规则。
    6. **原 query 那一路的答案有保底席位**。纯累加会把真答案顶掉：五条跑偏的
       扩展问法各自贡献 1/61，合起来就超过原始问题自己那一路的 RRF。所以
       「第一位那条问法」的前 `ceil(want/2)` 名先占位，其余名额才由累加分竞争
       （详见下面的"代价"）。去掉这一层时，仓库里有可复现的退化用例。
       **席位的单位跟着粒度走**：子块模式在父块坐标上算（`_fold_multi_hits`），
       因为回给调用方的一条证据是父块——在子块上保 3 席，折完可能只落在 1 个
       父块里，实际席位被折叠静默削掉。
    7. **每路都可能只剩 BM25**（没 key、embed 失败、hybrid 关）。这时多路仍然
       是多路：各路 BM25 名次照样累加，不抛错、不返回空。

    保底席位不是免费的，**代价写在这里**：

      - 扩展路最多只能贡献 `top_k - ceil(top_k/2)` 条（`top_k=5` 时是 2 条，
        `top_k=1` 时是 0 条）。当原 query 那一路本身找到了足够多的正确片段时，
        扩展路的价值就只剩"补位"，与"只补不抢"的两阶段方案差别不大。
      - `score` 是**累加分**，但排序是"保底段 + 竞争段"，所以**分数不单调**：
        保底段最后一条的 score 可能低于竞争段第一条。`score` 仍然只用于同一次
        检索内比较大小，不要拿它反推名次。
      - 保底的只是"位置"，不是"结论"：它保证原始问题的答案**排在前面**，不保证
        那条答案是对的——`search_sources` 的"命中≠正确"在这里同样成立。

    为什么不做"过滤"：实测那次退化的四条扩展 query 与材料**全都有交集**（材料
    确实包含蛋白、超导、合同、天文），按"扩展 query 与材料共享 bigram"筛一条
    都筛不掉。锚定对象必须是"扩展 query 与原问题是不是同一个意思"，而这又不能
    退回字面——`intents` 的定义就是同题不同问法，字面重合本来就低。所以只能从
    融合结构上改。

    `score` 的口径比单路多一种，别读错：
      - 只有一路时：与转发的那条路**逐字段相同**——`MINDMAP_PARENT_RETRIEVAL=1`
        时是 `search_parent`（`_fuse` + `_fold_to_parents`），关着时是
        `search_sources`（`_single_search`）。所以 score 是 BM25 原始分或单路 RRF 分
      - 多路且向量路起来：各路的 RRF 分累加，通常落在 0.02–0.08
      - 多路且只有 BM25：各路 BM25 名次的 RRF 累加（≈1/61 量级），**不是** BM25
        原始分——不同 query 的 BM25 分口径不同，相加没有意义
      三种都**只在本次检索内可比**。跨请求比较看 `method` 与
      `score_bm25`/`score_vector`。子块模式下分数来自**子块**匹配、区间来自父块
      （与 `search_parent` 同一条口径）：分数高只说明"这段原文里有一小段精准命中"。

    `score_bm25` 在多路里取"各条走了 BM25 的路中的最高分"，`score_vector` 取
    "各条向量路中的最高余弦"。取最大而不是相加：不同 query 的 BM25 分与余弦
    尺度不可加，最大值才对应"最好那次匹配"这个可解释的量。

    `library=True` 与 `search_sources` / `search_parent` 的同名参数逐条对齐：
    融合仍然只做一次，交给 `_merge_batches`（本次请求的命中保底占
    `ceil(want/2)` 席，库的只补位）。**跨批融合发生在折叠之后**，也就是父块
    坐标上——两个理由：

      - `_merge_batches` 的语义单位就是"一条回给调用方的证据"，子块模式下那是
        父块。跨批在子块级做的话，保底的 `ceil(want/2)` 席会在随后的折叠里被
        并成更少的父块，不变量被静默削掉；
      - 两批来源的 `source_id` 体系不同（请求内 uuid4 / 持久 id），BM25 的 `N`
        与 `df` 也各自一套。子块级跨批混合打分等于拿两套不可比的分布比大小；
        `_merge_batches` 只用名次（`1/(RRF_K+名次)`），跨批才有意义。

    库那批只用**第一条问法**做粗筛与检索——调用方约定第一位是用户原话，而扩展
    问法是为"同一批材料的多个角度"服务的，拿它们去筛库只会把库里真正相关的那份
    挤掉（`_library_sources` 的粗筛是覆盖率的排序，不是多路召回）。

    默认 `library=False` 时只是 `_search_multi_core` 的转发，行为与加这个参数
    之前**逐字段一致**（`tests/test_search_multi.py` 拿旧模块钉着多路那一半）。
    之所以要接进来而不是"多路时就不查库"：`MINDMAP_QUERY_EXPANSION` 打开后
    `_rag` 走的就是这条路，不接的话知识库会在那个开关下静默失效——而失效的表现
    是"库里明明有材料，模型却一条都没查到"，没有任何一处会说清是开关组合的问题。

    **重排**（`MINDMAP_RERANK`，默认 1）接在这条路的最后一格，而且**整次检索只调
    一次**：候选池是"多路累加 → 保底席位 → 去重"之后的那个池子，不是每路各排一遍。
    每路各排会有两个后果——N 倍的 reranker 调用，以及每一路都看不到别的路捞回来的
    候选（而多路的价值恰好在那些候选上）。用哪个 query 打分：**第一路**，也就是
    调用方放的原始问题（`_route_queries` 的约定），扩展问法只是同一批材料的不同
    角度，拿它们当重排的 query 会把"多少条问法都指向它"这件事重新混进来。
    """
    # 粒度跟着 `search_parent`：同一个开关（`MINDMAP_PARENT_RETRIEVAL`）决定多路
    # 是"子块匹配 → 折父块"还是"单级块匹配"。两处各判一次的话，会出现"本次那批
    # 折父块、库里那批没折"的粒度错位，而那种错只有调用方按 `end - start` 估信息
    # 密度时才会暴露。
    parent = _parent_retrieval_enabled()
    depth = _rerank_depth(top_k)
    fused = await _search_multi_core(sources, queries, top_k, filters, hyde,
                                     depth=depth, parent=parent)
    question = next((text.strip() for text in queries
                     if isinstance(text, str) and text.strip()), "")
    if not question:
        return fused[:top_k]
    if not library:
        return _rerank_top(question, fused, top_k)
    # 跨批融合在**折叠之后**做（`parent` 一致时两批都是同一粒度），理由见
    # docstring 的 `library=True` 那一段。
    extra = _library_batch(question, depth, filters, exclude=sources, parent=parent)
    return _rerank_top(question, _merge_batches(fused, extra, depth), top_k)


async def _search_multi_core(
    sources: list[Source],
    queries: list[str],
    top_k: int,
    filters: SourceFilter | None,
    hyde: str | None,
    *,
    depth: int | None = None,
    parent: bool = False,
) -> list[EvidenceHit]:
    """`search_multi` 在**给定来源集合内**的全部逻辑（不碰库、不做跨批融合）。

    抽出来的理由与 `_search_sources_core` / `_search_parent_core` 完全相同：
    `library=True` 时"多路融合"与"跨批融合"必须是两步，前者的产出（保底席位、
    累加分、`method`）原样作为后者的输入。混在一起写就会出现"库的命中也被算进
    某一路的累加分"这种谁也说不清的名次。

    `depth` 是**返回条数**（候选池），缺省等于 `top_k`；只给重排用。放大时每一处
    截断都跟着走——每路截断的 `pool`、单路快路径（`_single_search` 或
    `_fuse*CANDIDATE_FACTOR` + `_fold_to_parents`）、多路的 `_pick_ordered` /
    `_fold_multi_hits`——少任何一处，池子都会被卡回 `top_k`。

    `parent` 决定**匹配与融合的粒度**，由 `search_multi` 从
    `MINDMAP_PARENT_RETRIEVAL` 读出来传进来（与本函数无关的开关判断不进这一层，
    与 `_search_parent_core` 同一条规矩）：

      - `parent=True`：`_collect_leaves` 出子块 → 每路在子块上排序 → 子块级 RRF
        累加 → `_fold_multi_hits` 折父块（顺带在父块坐标上重排保底席位）；
      - `parent=False`：`_collect_chunks` 出单级块 → 每路排序 → 块级 RRF 累加 →
        `_pick_ordered` 按分段顺序去重截断。**与加多路时逐字节相同**。
    """
    want = top_k if depth is None else depth
    routes = _route_queries(queries, hyde)
    if not routes:
        return []

    # 过滤同样在切块与向量化之前：被筛掉的来源连一次缓存写入都不该发生。
    if filters is not None:
        selected = filter_sources(sources, filters)
        if not selected:
            logger.info("过滤条件筛掉了全部来源（%d→0）", len(sources))
            return []
        if len(selected) != len(sources):
            logger.debug("检索前过滤：来源 %d→%d", len(sources), len(selected))
        sources = selected

    # 约束 1：切块只做一次，六路共用同一个块集合。切哪一级只影响"匹配单元"：
    # 子块模式下父块不参与打分，它只是 `_fold_to_parents` 要返回的那个窗口。
    corpus: _LeafCorpus | None = None
    if parent:
        corpus = _collect_leaves(sources)
        if not corpus.items:
            return []
        collected = corpus.items
    else:
        collected = _collect_chunks(sources)
        if not collected:
            return []

    # 约束 3 的另一半：只有一路问法时，结果必须与**被转发的那条路**逐字段相同。
    # 这不是快路径优化，是可观测契约——关掉查询扩展、或只有一条纠错问法时，用户
    # 看到的召回必须与"没有多路召回"时一模一样。两条转发目标：
    #   - 子块模式 → `_search_parent_core` 的尾巴（`search_parent` 的全部逻辑）；
    #   - 单级模式 → `_single_search`（`search_sources` 的全部打分逻辑）。
    if len(routes) == 1 and routes[0][1]:
        if corpus is None:
            return _single_search(collected, routes[0][0], want)
        return _fold_to_parents(
            _fuse(collected, routes[0][0], want * CANDIDATE_FACTOR), corpus)[:want]

    quotes = [item[3] for item in collected]
    lengths = [item[2] - item[1] for item in collected]

    # 每路 BM25：纯 CPU，留在主线程。hyde 路不参与（约束见 `_route_queries`）。
    lexical_routes: list[list[float] | None] = [
        _bm25_scores(text, quotes) if use_bm25 else None for text, use_bm25 in routes
    ]

    # 向量：只对够长且开关打开的路算；一次 await 让所有路的 HTTP 并起来。
    similarity_routes: list[dict[int, float]] = [{} for _ in routes]
    hybrid = _hybrid_enabled()
    eligible = [index for index, (text, _) in enumerate(routes)
                if hybrid and len(text) >= MIN_VECTOR_QUERY_CHARS]
    if eligible:
        found = await _route_vectors(quotes, [routes[index][0] for index in eligible])
        for index, (similarity, reason) in zip(eligible, found):
            similarity_routes[index] = similarity
            if reason and reason != "unconfigured":
                logger.info("多路检索第 %d 路走单路 BM25（%s）", index + 1, reason)

    # 约束 5：每路各自截断到 pool，再把 RRF 分累加到同一个块上。
    pool = max(want * CANDIDATE_FACTOR, want)
    fused: dict[int, float] = {}
    primary: dict[int, float] = {}
    for position, (lexical, similarity) in enumerate(
            zip(lexical_routes, similarity_routes)):
        top_bm25: dict[int, int] = {}
        if lexical:
            top_bm25 = {index: rank
                        for index, rank in _bm25_ranking(lexical, lengths).items()
                        if rank <= pool}
        top_vector: dict[int, int] = {}
        if similarity:
            top_vector = {index: rank
                          for index, rank in _vector_ranking(similarity, lengths).items()
                          if rank <= pool}
        for index in set(top_bm25) | set(top_vector):
            # 同一块在多个问法下命中就累加，这是多路唯一的价值来源。
            # 名次是唯一在 BM25 与余弦两种量纲下都同义的量，所以只累加名次。
            value = _rrf_value(index, top_bm25, top_vector)
            fused[index] = fused.get(index, 0.0) + value
            if position == 0:
                # 第一位是调用方放的原始问题（`_route_queries` 的约定）。单独留一份
                # 它自己的 RRF，用于下面的保底席位——累加分回答不了"这句话本身
                # 有多相关"，只回答"多少条问法都碰巧指向它"。
                primary[index] = value
    if not fused:
        return []

    # 只要有一路的向量真给了候选，整批就算 hybrid（与单路 `method` 同语义）。
    method: Literal["lexical", "hybrid"] = "hybrid" if any(similarity_routes) else "lexical"

    # 展示用的两路原始分：取各路最大值（不同 query 的尺度不可加）。
    bm25_values: dict[int, float] = {}
    if any(lexical is not None for lexical in lexical_routes):
        # 至少有一条路走了 BM25 → 每个候选块都"走过"这一路，未命中记 0.0
        bm25_values = {
            index: max(lexical[index] for lexical in lexical_routes
                       if lexical is not None)
            for index in fused
        }
    vector_values: dict[int, float] = {}
    for similarity in similarity_routes:
        for index, value in similarity.items():
            if index in fused:
                vector_values[index] = max(vector_values.get(index, value), value)

    # 保底席位：原始问题那一路的前 `reserve` 名先占位，其余名额由所有路的累加分
    # 竞争。没有这一层时，五条跑偏的扩展路各自贡献的 1/61 会把它顶下去——实测：
    # 单路时「预算审批」0.0328 排第 1，五路时「蛋白质折叠」0.0653 反超、真答案
    # 掉到第 2（`tests/test_search_multi.py` 里钉着这个复现）。累加分回答的是
    # "多少条问法都指向它"，而扩展问法只是猜测，票数不该压过用户原话。
    #
    # 席位按**候选池**（`want`）而不是 `top_k` 算：扩池时若仍只保 `ceil(top_k/2)`
    # 席，原始问题自己的第 2、3 名就会在"池子变大"之后被更多扩展路挤出去——
    # 与保底席位的初衷正好相反。`want == top_k` 时两者是同一个数。
    reserve = min(max(1, (want + 1) // 2), len(primary))
    reserved = sorted(primary, key=lambda index: (-primary[index], lengths[index]))
    reserved = reserved[:reserve]
    taken = set(reserved)
    rest = sorted((index for index in fused if index not in taken),
                  key=lambda index: (-fused[index], lengths[index]))
    hits = [_to_hit(index, fused[index], collected, bm25_values, vector_values, method)
            for index in [*reserved, *rest]]
    if corpus is None:
        return _pick_ordered(hits, want)
    # 子块模式：分段顺序在子块坐标上不作数（折叠只留每个父块的最优叶子，那一步
    # 必须按分数降序），所以保底席位在 `_fold_multi_hits` 里按父块重排一次。
    return _fold_multi_hits(hits, collected, corpus, primary, want)
