"""检索层评估：用**材料自己的结构**造测试集，把检索改动变成可比的数字。

## 为什么不用 ragas

`ragas` 评的是生成质量（faithfulness / answer relevancy / context precision），
它的输入是**人工标注的 ground truth**（一个问题 + 一份标准答案）。我们要的不是
那个：给定一个 query，`top_k` 里到底有没有该被召回的段落——这纯粹是检索层的事，
和"模型答得好不好"是两个失效面。混在一起量，检索退化会被提示词退化掩盖。

更实际的差别是成本：标注一份标准答案要人，而**章节结构就是现成的标注**。一份有
目录的材料，天生自带"这个标题管这几段原文"这份对应关系。所以这里一个模型都不调、
一条标注都不要、一个新依赖都不装。

## 测试集怎么来（构造规则）

1. 对每份材料单独跑 `heading_outline`（已有实现，逐行扫原文），拿它的一级目录；
2. 每一条目录项就是一条 `EvalCase`：**标题当 query**，`[start, start+coverage)`
   这段原文当 `relevant`（`coverage` 是它到下一个同级标题的字数，含标题行本身）；
3. 三条排除规则（阈值即常量，理由见各自的注释）：
   `MIN_TITLE_CHARS` / `MIN_SECTION_CHARS` / `MAX_SECTION_SHARE`；
4. 每份材料最多 `MAX_CASES_PER_MATERIAL` 条，**按原文顺序等距取样**——不是按
   coverage 取最大的那几条：那样统计会被少数大章押住，而一份材料里细碎的小节才是
   检索最容易失手的地方。

`heading_outline` 只给一级标题、且条目互不重叠，所以一条 case 的 `relevant` 通常
只有一段。做成 `list[tuple[int, int]]` 是因为判定与度量都必须对"多段"成立：手工
测试集、以及将来"章节被切块边界拆开"的构造都会用到，判定逻辑不能靠"恰好只有一段"
这条偶然性质。

## 命中判定（阈值 `HIT_OVERLAP_RATIO = 20%`）

命中块与 `relevant` 的交集 ≥ **命中块自身长度的 20%** 才算命中。方向刻意选"占命中
块的比例"而不是"占 relevant 的比例"：`relevant` 是一整章（几百到几千字），一块
600 字的父块哪怕整块落在章内也只占这一章的 10%–25%，用后者会把绝大多数真命中判掉。

20% 这个数被两侧夹出来：

  - 下界：父块之间有 `CHUNK_OVERLAP_CHARS = 100` 的重叠。一个与章节只共享那 100 字
    重叠的邻块，在 600 字的典型父块上是 100/600 ≈ 17%，在 `CHUNK_MAX_CHARS = 700`
    上是 14%——都低于 20%，判为不命中。这正是要的：那一块讲的是别的事，只是尾巴搭
    在同一段文字上。
  - 上界：`relevant` 的边界是**标题行首**，父块是"450–700 字 + 句首吸附"切出来的，
    真讲这一章的父块常有 30%–60% 的长度落在章外（前一块的尾巴、下一章的开头）。
    阈值抬到 40% 以上就开始把真命中判掉。

代价写在明处：`CHUNK_MIN_CHARS = 150` 的短父块若整块恰好是那 100 字重叠，比值
100/150 ≈ 67% 会被判成命中。但 `_merge_short` 会把 150 字以下的块并进邻块，真实
语料里几乎不出现；`tests/test_retrieval_eval.py` 把这个方向钉住。

## 四个指标

设某条 case 的 relevant 总长为 `L`（多段合并去重后求和），前 `k` 条命中依次新增
覆盖的原文比例 `g₁…g_k`（每条只算"之前没覆盖过"的字，故 `Σgᵢ ≤ 1`）：

  - `recall@k`＝**二值**：这条 case 在 `top_k` 内有没有任何一条命中（判据见上），
    取 case 平均。分母是全部 case，一条 case 只贡献 0 或 1。
  - `mrr@k`＝`1 / 第一条命中的名次`（`top_k` 内没命中记 0），取 case 平均。
  - `ndcg@k`＝`Σ_{rank≤k} g_rank / log2(rank + 1)`，取 case 平均。IDCG 取 1：
    "一条命中完全覆盖 relevant 且排第一"是可达上界（父块区间理论上可以正好等于章节
    区间），于是"排第一但只覆盖一半"得 0.5，比常规二值 nDCG 更严。一个 case 命中
    多段时折扣累加：第 2、3 名的补偿覆盖照样按 `log2` 打折。
  - `coverage@k`＝`Σ_{rank≤k} g_rank`，取 case 平均。**软指标**。

`recall` 与 `coverage` 的口径差异是这套东西的核心，不能只看一个：

  - `recall@k` 回答"这一章有没有被摸到"，一条命中覆盖 20% 就记满分；
  - `coverage@k` 回答"这一章被摸到了多少"，同一条命中只记 0.2。

两者一起看才分得出"命中一段就收工"（recall 高、coverage 低）与"真把整章召回"
（两者都高）。只看 recall，会把"父块够大、随便沾一点就算命中"的退化当成进步。

## 三套测试集：`--suite title|manual|crossdoc|both`

`title` 套就是上面那套（标题当 query）。它有一个结构性偏差：标题是章节的第一行，
query 的每个 bigram 都在目标区间里，于是 `R@3/5/10` 全饱和、三列判别力为零。

`manual` 套是 `scripts/eval_cases_manual.py` 里 28 条**手写问法**（人读材料之后写的
真实提问，按近/中/远三档与章/段/跨章三种粒度标注）。它量的是"真实提问下检索还剩
多少"，数字比 `title` 套低 20pp 上下——那是难度差，不是检索退步。

`crossdoc` 套是 `scripts/eval_cases_crossdoc.py` 里 14 条**必须两份以上材料才能答完**
的问法，专为量图检索（`MINDMAP_GRAPH_SEARCH`）而建：manual 那套 28 条里 26 条只指向
一份材料，那个形状测不出桥接价值。它只在 `--suite crossdoc` 时跑——读数取决于图里
有什么（那是跨机器的外部状态），并进默认回归会让 `--check` 依赖一台跑着的 Neo4j。
报告里除了四类常规指标，还多一节 `crossdoc`（`doc_cover` / `bridge` / `full_bridge`，
见 `crossdoc_metrics`）。

**各套分别算指标，绝不混在一起平均**：混算出来的均值既不是"标题查询"的分数，也不是
"真实提问"的分数，几套 20pp 的难度差会被一个看似合理的中间数掩掉。报告里它们是
`suites.title` / `suites.manual` / `suites.crossdoc` 几个并列对象。

`manual` 套的 `source_hash` 对着 `MANUAL_SOURCE_HASHES` 校验：材料正文一变，
**直接报错退出**，不跳过。跳过会让"标注失效"伪装成"指标掉了 10pp"。

`--check` 只比 `title` 套：`manual` 的绝对分更低、方差更大（28 条里一条翻面就是
3.6pp），当 CI 门槛会天天误报。它的数字进文档、由人看趋势。

## 多配置对照

`--source-mode` / `--hybrid` / `--parent` / `--library` 各接受 `0,1` 或 `both`，
默认 `both`，于是默认跑满 8 个组合。`--source-mode` 是**只记录、不影响度量**的
维度：它决定正文进不进 prompt，而本脚本只调 `search_sources` / `search_parent`，
不碰生成。所以它两行数字逐位相同——那恰好是"它没漏进度量"的证据，不是 bug。

`--library 1` 时**请求内来源清空**、只靠知识库：那样量的是"关掉浏览器再回来还能不能
召回"。若照常把材料当请求内来源传进去，`_library_batch(exclude=sources)` 会把库那批
排掉，这一行就退化成 `library=0` 的副本，开关等于没测。

## 用法

    cd backend
    python scripts/evaluate_retrieval.py                     # 两套并列（默认 --suite both）
    python scripts/evaluate_retrieval.py --suite manual      # 只看手写那套
    python scripts/evaluate_retrieval.py --suite crossdoc    # 只看跨文档那套（要 Neo4j）
    python scripts/evaluate_retrieval.py --json out.json     # 报告落盘
    python scripts/evaluate_retrieval.py --markdown -        # 贴进 docs 的表 + 基线块
    python scripts/evaluate_retrieval.py --check             # 与基线比 title 套，掉超 2pp 退出码 1
    python scripts/evaluate_retrieval.py --ladder            # 量化"标题当 query"的偏差

`--check` 读 `docs/RETRIEVAL-EVAL.md` 里**最后一段** ```json 围栏（`--markdown` 产出
的那段），**配置也取自那段基线**：改完检索不必记得当初用了哪些开关，比对的两边一定
是同一组配置。

边界：不改任何检索实现（只调用）、不引入依赖、不碰前端与 `RAG-DECISIONS.md`。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
SCRIPTS = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
# `scripts/` 不是包（没有 `__init__.py`），而 `eval_cases_manual` 就躺在旁边。把目录
# 本身放上 sys.path 是最短的路：测试按文件路径加载本模块时也照样能 import 到它。
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import eval_cases_crossdoc as crossdoc_cases  # noqa: E402
import eval_cases_manual as manual_cases  # noqa: E402
from app.contracts.source import Source, SourceKind  # noqa: E402
from app.ingestion.service import parse_bytes, parse_text  # noqa: E402
from app.knowledge import library as knowledge_library  # noqa: E402
from app.knowledge.embedding import configured as embedding_configured  # noqa: E402
from app.knowledge.service import (  # noqa: E402
    heading_outline,
    search_parent,
    search_sources,
)

# ---------------------------------------------------------------- 测试集构造阈值

MIN_TITLE_CHARS = 4
"""标题最短字数。

`heading_outline` 自己的下限是 `OUTLINE_MIN_TITLE_CHARS = 2`（那是"编号残片"的
下限：`1.5 倍` 剥掉编号剩下的 `倍`）。当 query 是另一回事：3 个汉字只切得出
**2 个 bigram**，而 BM25 的区分度全靠 bigram 的 IDF——两个 bigram 的 query 几乎
任何一章都能沾上，case 会退化成"必然命中"，指标失去意义。4 个字起才有 3 个
bigram，刚够判"这一章还是那一章"。
"""

MIN_SECTION_CHARS = 200
"""章节最短字数（含标题行）。

父块是 450–700 字切出来的，200 字以下的章节**没有独立边界**：命中它的那一块必然
大部分落在章外，"命中了这一章"与"命中了这一章旁边"在父块坐标上分不开，判定噪声
比信号大。
"""

MAX_SECTION_SHARE = 0.60
"""单章占全文的比例上限。

一章吃掉 60% 以上时，随便命中正文的稀疏区域也能算"命中这一章"，case 退化成"整份
材料找不找得到"这种弱标签，而那种 case 永远满分。实测本仓库四份 md 的最大单章是
16%（`docs/AGENT_PLAN.md`）、桌面那份 docx 是 48%，没有材料撞到这条上限——它是
安全阀，不是常态过滤器。
"""

MAX_CASES_PER_MATERIAL = 8
"""每份材料的 case 上限。

不设上限时 40 KB 的长文档会产出十几条 case，统计被它一份主导；8 条让五份材料的
权重接近，长文档不至于把短文档的信号淹掉。
"""

HIT_OVERLAP_RATIO = 0.20
"""命中阈值：交集 ≥ 命中块长度的 20%。推导见模块 docstring 的「命中判定」。"""

# ---------------------------------------------------------------- 度量与对照参数

DEFAULT_TOP_K = (1, 3, 5, 10)
CHECK_TOLERANCE_PP = 2.0
"""`--check` 的容差：绝对百分点。掉超过它算回归（不接 CI，手动跑）。"""

METRIC_NAMES = ("recall", "mrr", "ndcg", "coverage")
REPORT_SCHEMA = "retrieval-eval/v2"
"""v2：外层多了 `suites`（两套并列）。v1 是单套报告，`baseline_from_markdown` 仍认。"""

SUITE_NAMES = ("title", "manual")
"""默认两套的名字与固定顺序。顺序进报告与控制台：先回归那套，再看真实那套。

`crossdoc` **不在**这里：它是本轮为量图检索而新加的一套，只由 `--suite crossdoc`
显式触发。放进默认列表会改掉 `--suite both` 的契约（两套并列、57 条），而那条契约
已经被 `test_retrieval_eval_manual.py` 钉住——更要紧的是，跨文档套的价值取决于
「图里到底有什么」，把它并进默认回归等于让每次 `--check` 都依赖一台跑着的 Neo4j。
"""

EXTRA_SUITE_NAMES = ("crossdoc",)
"""只在显式指定时跑的套。`run_suites` 与报告渲染都按 `SUITE_NAMES + EXTRA_SUITE_NAMES`
遍历，所以加一套不需要再改两处循环。"""

ALL_SUITE_NAMES = (*SUITE_NAMES, *EXTRA_SUITE_NAMES)

DEFAULT_BASELINE = REPO / "docs" / "RETRIEVAL-EVAL.md"

# 默认材料：`docs/` 下四份结构清楚的真实 md。全部随仓库走，谁 clone 下来都能跑出
# 同一张表。
DEFAULT_MATERIALS = (
    REPO / "docs" / "AGENT_PLAN.md",
    REPO / "docs" / "RAG-DECISIONS.md",
    REPO / "docs" / "PLAN.md",
    REPO / "docs" / "ARCHITECTURE.md",
)

DEFAULT_DOCX = Path.home() / "Desktop" / "螺线管磁场测量实验报告_填写内容.docx"
"""桌面上那份真实 docx：docx 的章节靠"整段加粗"的行，与 markdown 是两条不同的
抽取路径（`_outline_lines` 的第二条规则），值得进基线。缺失就跳过。"""


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class EvalCase:
    """一条测试样例：一个 query，一份材料，若干段"应该被召回"的原文区间。

    `source_hash` 是材料的 `sha256(text)`（与 `knowledge.library.content_hash`
    同口径）。用它而不是文件名：文件名会变，同一份正文换个名字重新上传不该让旧
    基线失效；更不能用 `Source.id`——那是每次请求现生成的 uuid4，连一次进程都活不过。

    `relevant` 是**原文码点区间**，与 `EvidenceHit` / `SourceRef` 同一口径。多段是
    合法的：判定与度量都按"多段并集"算，不假设只有一段。
    """

    query: str
    source_hash: str
    relevant: list[tuple[int, int]]


@dataclass(frozen=True)
class Material:
    """一份进评估的材料：来源对象 + 指纹 + 人类可读的名字。"""

    ref: str
    source: Source
    content_hash: str

    @property
    def text(self) -> str:
        return self.source.text or ""


@dataclass(frozen=True)
class Config:
    """一个待评的检索配置。四个维度与命令行的四个开关一一对应。"""

    source_mode: str
    hybrid: bool
    parent: bool
    library: bool

    def label(self) -> str:
        return (f"{self.source_mode}/hybrid={int(self.hybrid)}"
                f" parent={int(self.parent)} lib={int(self.library)}")


@dataclass(frozen=True)
class Dataset:
    """测试集：case 列表 + 材料清单 + 每份材料的结构统计（都进报告）。

    `suite` 标明这套 case 是怎么来的（`title` / `manual` / `crossdoc`），`annotations`
    放手写套自己的元数据（词面重合统计）：`title` 套是结构派生的，没有可注明的东西。

    `crossdoc` 只有跨文档套才非空：每条是 `{query, materials, per_material, overlap,
    why}`，`materials` 是这条 case 涉及的**材料指纹**（≥2 个），`per_material` 是
    `{指纹: [(start, end), ...]}`。`cases` 里那条 case 被摊成了多条 `EvalCase`
    （一条 query × 一份材料），所以「跨了几份材料」这件事在 `cases` 上已经看不出来
    ——它必须留在这里，`crossdoc_metrics` 靠它算覆盖份数。
    """

    cases: list[EvalCase]
    materials: list[Material]
    stats: dict[str, dict]
    suite: str = "title"
    annotations: dict = field(default_factory=dict)
    crossdoc: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------- 区间运算


def _merge(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠或相接的区间；空区间与反序区间丢掉。"""
    merged: list[tuple[int, int]] = []
    for start, end in sorted((start, end) for start, end in ranges if end > start):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _overlap_len(left: Sequence[tuple[int, int]], right: Sequence[tuple[int, int]]) -> int:
    """两组**已合并**区间的交集总长。双指针，O(n+m)。"""
    total = 0
    i = j = 0
    while i < len(left) and j < len(right):
        low = max(left[i][0], right[j][0])
        high = min(left[i][1], right[j][1])
        if high > low:
            total += high - low
        if left[i][1] < right[j][1]:
            i += 1
        else:
            j += 1
    return total


def _clip(span: tuple[int, int], ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """`span` 落在 `ranges` 里的那些片段（已合并）。"""
    pieces: list[tuple[int, int]] = []
    for start, end in ranges:
        low = max(span[0], start)
        high = min(span[1], end)
        if high > low:
            pieces.append((low, high))
    return _merge(pieces)


def hit_is_relevant(
    span: tuple[int, int],
    relevant: Sequence[tuple[int, int]],
    *,
    ratio: float = HIT_OVERLAP_RATIO,
) -> bool:
    """这条命中是否算"命中了 relevant"：交集 ≥ 命中块长度的 `ratio`。

    零宽命中（`end <= start`）一律不算：那是切块器在空正文上的产物，不代表任何
    原文位置。
    """
    length = span[1] - span[0]
    if length <= 0:
        return False
    return _overlap_len(_merge([span]), _merge(relevant)) / length >= ratio


# ---------------------------------------------------------------- 测试集构造


def _spread(count: int, limit: int) -> list[int]:
    """在 `[0, count)` 里按**原文顺序等距**取 `limit` 个下标。

    等距而不是"取 coverage 最大的"：按大小取会把统计押在少数大章上，而检索最容易
    失手的恰恰是中后段那些细碎小节；等距取样对"材料前段更好检"这类偏差也更中性
    ——它把 case 均匀铺在整份文档上。
    """
    if limit <= 0 or count <= 0:
        return []
    if count <= limit:
        return list(range(count))
    if limit == 1:
        return [0]
    step = (count - 1) / (limit - 1)
    return sorted({round(index * step) for index in range(limit)})


def build_dataset(
    materials: Sequence[Material],
    *,
    max_per_material: int = MAX_CASES_PER_MATERIAL,
    min_title_chars: int = MIN_TITLE_CHARS,
    min_section_chars: int = MIN_SECTION_CHARS,
    max_section_share: float = MAX_SECTION_SHARE,
) -> Dataset:
    """从材料的章节结构造测试集。

    逐份材料单独出目录，`max_items` 设得足够大以绕开 `heading_outline` 的
    `OUTLINE_MAX_ITEMS = 30` 裁剪：那个上限是为了给模型看骨架时控制条数，评估这边
    要的是"这份材料有多少章"，裁掉等于把后半份材料的 case 静默丢掉。裁剪与否只影响
    case 数，不影响单条 case 的内容（`_trim_outline` 只做取舍，不改条目本身）。
    """
    cases: list[EvalCase] = []
    stats: dict[str, dict] = {}
    for material in materials:
        text = material.text
        entries = heading_outline([material.source], max_items=10 ** 6) if text else []
        eligible: list[tuple[str, int, int]] = []
        for entry in entries:
            end = min(entry.start + entry.coverage, len(text))
            if len(entry.title) < min_title_chars:
                continue
            if entry.coverage < min_section_chars:
                continue
            if text and entry.coverage / len(text) > max_section_share:
                continue
            eligible.append((entry.title, entry.start, end))
        picked = [eligible[index] for index in _spread(len(eligible), max_per_material)]
        for title, start, end in picked:
            cases.append(EvalCase(query=title, source_hash=material.content_hash,
                                  relevant=[(start, end)]))
        stats[material.ref] = {
            "ref": material.ref,
            "content_hash": material.content_hash,
            "chars": len(text),
            "outline_entries": len(entries),
            "eligible_sections": len(eligible),
            "cases": len(picked),
        }
    return Dataset(cases=cases, materials=list(materials), stats=stats)


# ---------------------------------------------------------------- 手写测试集装载


def _manual_spans(case, material: Material, entries) -> list[tuple[int, int]]:
    """把一条手写 case 的章节/锚点解析成 `relevant` 区间（已合并）。

    任何一步对不上都**抛 `SystemExit`**，不降级、不跳过：手写标注与材料是一对一绑死的，
    对不上就说明标注过期了，此时唯一正确的动作是让人回去重标。
    """
    text = material.text
    if case.distance not in manual_cases.DISTANCES:
        raise SystemExit(f"手写用例的距离档不认识：{case.distance}（{case.query}）")
    if case.granularity not in manual_cases.GRANULARITIES:
        raise SystemExit(f"手写用例的粒度不认识：{case.granularity}（{case.query}）")
    if case.granularity == "passage" and not case.focus:
        raise SystemExit(f"passage 粒度必须给 focus 锚点：{case.query}")
    if case.granularity == "chapter" and case.focus:
        raise SystemExit(f"chapter 粒度不该有 focus：{case.query}")
    if case.granularity == "cross" and len(case.sections) < 2:
        raise SystemExit(f"cross 粒度至少要两章：{case.query}")

    spans: list[tuple[int, int]] = []
    for prefix in case.sections:
        matches = [entry for entry in entries if entry.title.startswith(prefix)]
        if len(matches) != 1:
            raise SystemExit(
                f"手写用例的章节前缀命中 {len(matches)} 个（要恰好 1 个）："
                f"{case.material} /「{prefix}」\n  用例：{case.query}")
        entry = matches[0]
        spans.append((entry.start, min(entry.start + entry.coverage, len(text))))

    if case.focus:
        found = text.count(case.focus)
        if found != 1:
            raise SystemExit(
                f"手写用例的锚点命中 {found} 次（要恰好 1 次）："
                f"{case.material} /「{case.focus}」\n  用例：{case.query}")
        start = text.index(case.focus)
        owner = next(((low, high) for low, high in spans if low <= start < high), None)
        if owner is None:
            raise SystemExit(
                f"手写用例的锚点不在目标章节内：{case.material} /「{case.focus}」"
                f"\n  用例：{case.query}")
        end = start + manual_cases.FOCUS_CHARS
        if end > owner[1]:
            raise SystemExit(
                f"手写用例的锚点离章末太近（{manual_cases.FOCUS_CHARS} 字的标注段会越界）："
                f"{case.material} /「{case.focus}」\n  用例：{case.query}")
        spans = [(start, end)]
    return _merge(spans)


def manual_materials_present(materials: Sequence[Material]) -> bool:
    """本次材料集里有没有手写套需要的那四份 md。

    与"hash 对不对"是两件事：材料**不在**集里说明这次是显式换了材料（`--material`），
    manual 套无从评；材料**在**但正文变了，说明标注过期了——后者必须报错，前者可以
    降级成只跑 title（见 `main`）。
    """
    present = {material.ref for material in materials}
    return all(ref in present for ref in manual_cases.MANUAL_SOURCE_HASHES)


def build_manual_dataset(materials: Sequence[Material]) -> Dataset:
    """装载 `eval_cases_manual.py` 那 28 条手写 case。

    三道校验都在最前面做完，任何一道不过就整轮退出：

    1. 四份材料都在本次材料集里（`--material` 换掉材料时，手写套无从标注）；
    2. 每份材料的 `content_hash` 与 `MANUAL_SOURCE_HASHES` 一致——**材料正文一变就
       报错**。跳过会让"标注失效"看起来像"检索掉了 10pp"，那是两种完全不同的结论；
    3. 每条 case 的章节前缀唯一、锚点唯一且落在章节内（`_manual_spans`）。

    顺带算出每条 query 对**目标区间**的词面重合（`overlap_ratio`），进报告：它是这套
    case 的"离原话多远"的刻度，也是后人往里加 case 时的护栏。
    """
    by_ref = {material.ref: material for material in materials}
    missing = [ref for ref in manual_cases.MANUAL_SOURCE_HASHES if ref not in by_ref]
    if missing:
        raise SystemExit(
            "manual 套需要的材料不在本次材料集里：\n  "
            + "\n  ".join(missing)
            + "\n手写 case 只对这四份 md 标注，换材料请先补标注。")

    drifted = [
        (ref, expected, by_ref[ref].content_hash)
        for ref, expected in manual_cases.MANUAL_SOURCE_HASHES.items()
        if by_ref[ref].content_hash != expected
    ]
    if drifted:
        detail = "\n  ".join(
            f"{ref}\n    登记 {expected[:16]}…  实际 {actual[:16]}…"
            for ref, expected, actual in drifted)
        raise SystemExit(
            "材料正文变了，manual 套的标注已失效（不跳过，直接报错）：\n  "
            + detail
            + "\n请重读材料、复核 relevant 区间与问法，再把新指纹写回 "
              "scripts/eval_cases_manual.py 的 MANUAL_SOURCE_HASHES。")

    entries_by_ref = {
        ref: heading_outline([material.source], max_items=10 ** 6)
        for ref, material in by_ref.items()
    }
    cases: list[EvalCase] = []
    overlaps: list[float] = []
    per_ref: dict[str, list[float]] = {ref: [] for ref in manual_cases.MANUAL_SOURCE_HASHES}
    for case in manual_cases.CASES:
        material = by_ref[case.material]
        relevant = _manual_spans(case, material, entries_by_ref[case.material])
        ratio = manual_cases.overlap_ratio(
            case.query, "".join(material.text[start:end] for start, end in relevant))
        per_ref[case.material].append(ratio)
        overlaps.append(ratio)
        cases.append(EvalCase(query=case.query, source_hash=material.content_hash,
                              relevant=relevant))

    stats: dict[str, dict] = {}
    for ref, material in by_ref.items():
        entries = entries_by_ref[ref]
        owned = len({case.sections[0] for case in manual_cases.CASES if case.material == ref})
        stats[ref] = {
            "ref": ref,
            "content_hash": material.content_hash,
            "chars": len(material.text),
            "outline_entries": len(entries),
            "eligible_sections": owned,
            "cases": len(per_ref[ref]),
        }

    by_distance: dict[str, dict] = {}
    for name in manual_cases.DISTANCES:
        values = [ratio for case, ratio in zip(manual_cases.CASES, overlaps)
                  if case.distance == name]
        if values:
            by_distance[name] = {
                "cases": len(values),
                "mean": round(sum(values) / len(values), 6),
                "min": round(min(values), 6),
                "max": round(max(values), 6),
            }
    annotations = {
        "overlap_mean": round(sum(overlaps) / len(overlaps), 6) if overlaps else 0.0,
        "overlap_by_distance": by_distance,
        "by_granularity": {
            name: sum(1 for case in manual_cases.CASES if case.granularity == name)
            for name in manual_cases.GRANULARITIES
        },
        # 逐条明细进报告：它是"这套 case 到底离原话多远"的唯一一手证据，缺了它，
        # 文档里那张表就只是别人不得不信的一段话。
        "cases": [
            {
                "query": case.query,
                "material": case.material,
                "sections": list(case.sections),
                "focus": case.focus,
                "distance": case.distance,
                "granularity": case.granularity,
                "overlap": round(ratio, 6),
                "why": case.why,
            }
            for case, ratio in zip(manual_cases.CASES, overlaps)
        ],
    }
    return Dataset(cases=cases, materials=list(materials), stats=stats,
                   suite="manual", annotations=annotations)


# ---------------------------------------------------------------- 跨文档测试集装载


def _crossdoc_spans(target, material: Material, entries) -> list[tuple[int, int]]:
    """把一条 `CrossTarget` 解析成 `relevant` 区间（已合并）。

    与 `_manual_spans` 同一套纪律，只是没有 `distance` / `granularity` 两种元数据：
    跨文档套的每一条目标**各自**决定是整章还是章内某段（有 `focus` 就是段）。
    任何一步对不上都抛 `SystemExit`——标注与正文是一对一绑死的，对不上说明标注过期，
    此时唯一正确的动作是让人回去重标，不是跳过。
    """
    text = material.text
    matches = [entry for entry in entries if entry.title.startswith(target.section)]
    if len(matches) != 1:
        raise SystemExit(
            f"跨文档用例的章节前缀命中 {len(matches)} 个（要恰好 1 个）："
            f"{target.material} /「{target.section}」")
    entry = matches[0]
    span = (entry.start, min(entry.start + entry.coverage, len(text)))
    if not target.focus:
        return [span]
    found = text.count(target.focus)
    if found != 1:
        raise SystemExit(
            f"跨文档用例的锚点命中 {found} 次（要恰好 1 次）："
            f"{target.material} /「{target.focus}」")
    start = text.index(target.focus)
    if not span[0] <= start < span[1]:
        raise SystemExit(
            f"跨文档用例的锚点不在目标章节内：{target.material} /「{target.focus}」")
    end = start + target.chars
    if end > span[1]:
        raise SystemExit(
            f"跨文档用例的锚点离章末太近（{target.chars} 字的标注段会越界）："
            f"{target.material} /「{target.focus}」")
    return [(start, end)]


def build_crossdoc_dataset(materials: Sequence[Material]) -> Dataset:
    """装载 `eval_cases_crossdoc.py` 那 14 条跨文档 case。

    三道校验与 manual 套共用同一批指纹与同一套报错口径：材料不在集里报错、正文变了
    报错（不跳过）、前缀/锚点对不上报错。

    **一条 case 摊成多条 `EvalCase`**（query × 每份目标材料一条）。这个摊法是刻意的：
    `evaluate_case` 只认一份材料，而"两段证据各命中没命中"本来就是要分开算的两个
    问题——摊开之后 `recall@k` 自然变成"两条证据命中了几条"的平均，不需要给
    `EvalCase` 加一个会污染 title/manual 两套的多目标字段。真正的跨文档指标
    （覆盖了几份材料）由 `crossdoc_metrics` 在 `crossdoc` 上算。
    """
    by_ref = {material.ref: material for material in materials}
    missing = [ref for ref in crossdoc_cases.CROSSDOC_SOURCE_HASHES if ref not in by_ref]
    if missing:
        raise SystemExit(
            "跨文档套需要的材料不在本次材料集里：\n  "
            + "\n  ".join(missing)
            + "\n跨文档 case 只对这四份 md 标注，换材料请先补标注。")

    drifted = [
        (ref, expected, by_ref[ref].content_hash)
        for ref, expected in crossdoc_cases.CROSSDOC_SOURCE_HASHES.items()
        if by_ref[ref].content_hash != expected
    ]
    if drifted:
        detail = "\n  ".join(
            f"{ref}\n    登记 {expected[:16]}…  实际 {actual[:16]}…"
            for ref, expected, actual in drifted)
        raise SystemExit(
            "材料正文变了，跨文档套的标注已失效（不跳过，直接报错）：\n  "
            + detail
            + "\n请重读材料、复核 relevant 区间与问法，再把新指纹写回 "
              "scripts/eval_cases_manual.py 的 MANUAL_SOURCE_HASHES（两套共用那张表）。")

    entries_by_ref = {
        ref: heading_outline([material.source], max_items=10 ** 6)
        for ref, material in by_ref.items()
    }
    cases: list[EvalCase] = []
    groups: list[dict] = []
    overlaps: list[float] = []
    material_counts: dict[str, int] = {ref: 0 for ref in crossdoc_cases.CROSSDOC_SOURCE_HASHES}
    for case in crossdoc_cases.CASES:
        if not crossdoc_cases.is_cross_document(case):
            raise SystemExit(f"跨文档用例只涉及一份材料（应 ≥2 份）：{case.query}")
        per_material: dict[str, list[tuple[int, int]]] = {}
        texts: list[str] = []
        for target in case.targets:
            material = by_ref[target.material]
            spans = _crossdoc_spans(target, material, entries_by_ref[target.material])
            per_material.setdefault(material.content_hash, []).extend(spans)
            texts.extend(material.text[start:end] for start, end in spans)
            cases.append(EvalCase(query=case.query, source_hash=material.content_hash,
                                  relevant=spans))
        for fingerprint in per_material:
            for ref, material in by_ref.items():
                if material.content_hash == fingerprint:
                    material_counts[ref] += 1
                    break
        ratio = crossdoc_cases.overlap_ratio(case.query, "".join(texts))
        overlaps.append(ratio)
        groups.append({
            "query": case.query,
            "materials": sorted(per_material),
            "per_material": {fingerprint: per_material[fingerprint]
                             for fingerprint in sorted(per_material)},
            "targets": [
                {"material": target.material, "section": target.section,
                 "focus": target.focus}
                for target in case.targets
            ],
            "overlap": round(ratio, 6),
            "why": case.why,
        })

    stats: dict[str, dict] = {}
    for ref, material in by_ref.items():
        stats[ref] = {
            "ref": ref,
            "content_hash": material.content_hash,
            "chars": len(material.text),
            "outline_entries": len(entries_by_ref[ref]),
            "eligible_sections": material_counts[ref],
            "cases": material_counts[ref],
        }

    mean = sum(overlaps) / len(overlaps) if overlaps else 0.0
    annotations = {
        "overlap_mean": round(mean, 6),
        "overlap_min": round(min(overlaps), 6) if overlaps else 0.0,
        "overlap_max": round(max(overlaps), 6) if overlaps else 0.0,
        "cases": groups,
        "case_count": len(crossdoc_cases.CASES),
        "pair_count": len(cases),
        "materials_per_case": round(
            sum(len(group["materials"]) for group in groups) / len(groups), 3)
        if groups else 0.0,
    }
    return Dataset(cases=cases, materials=list(materials), stats=stats,
                   suite="crossdoc", annotations=annotations, crossdoc=groups)


def crossdoc_metrics(
    dataset: Dataset,
    config: Config,
    top_k: Sequence[int],
    id_map: dict[str, str],
    *,
    cache: dict[str, list[tuple[str, int, int]]] | None = None,
) -> dict[str, dict]:
    """跨文档指标：一行检索结果覆盖了几份目标材料。

    三个口径一起报，因为「覆盖了几份材料」有强弱两种读法，差的正是这套件想量的东西：

      - `doc_cover@k`：top-k 命中**提到了**几份目标材料（只要指纹对得上就算，不管落在
        材料里哪一段）。**宽松**，也最容易虚高：命中同一份材料里的无关章节同样算数。
      - `bridge@k`：top-k 里**落在 relevant 区间**的命中覆盖了几份目标材料（
        `hit_is_relevant` 同一判据）。这才是"桥"——两份材料的正确位置都拿到了。
      - `full_bridge@k`：`bridge@k == 1.0` 的 case 比例，即真正的**跨文档命中率**：
        一次检索把两份材料各自该去的地方都覆盖到了。

    都按 case 平均。分母是该 case 的**目标材料份数**（本套恒为 2，但公式不假设它）。
    空 `crossdoc`（title / manual 套）返回 `{}`，报告里就不会有这一节。
    """
    groups = dataset.crossdoc
    if not groups:
        return {}
    ks = sorted({int(value) for value in top_k})
    depth = max(ks)
    hits_cache = cache if cache is not None else {}
    rows: dict[int, list[tuple[float, float, float]]] = {k: [] for k in ks}
    for group in groups:
        if group["query"] not in hits_cache:
            hits_cache[group["query"]] = _ranked_hits(
                dataset, config, group["query"], depth, id_map)
        hits = hits_cache[group["query"]][:depth]
        per_material = group["per_material"]
        total = len(per_material) or 1
        for k in ks:
            window = hits[:k]
            mentioned = {fingerprint for fingerprint, _s, _e in window
                         if fingerprint in per_material}
            strict = {
                fingerprint for fingerprint, start, end in window
                if fingerprint in per_material
                and hit_is_relevant((start, end), per_material[fingerprint])
            }
            rows[k].append((
                len(mentioned) / total,
                len(strict) / total,
                1.0 if len(strict) == len(per_material) else 0.0,
            ))
    metrics: dict[str, dict] = {}
    for k in ks:
        values = rows[k]
        metrics[str(k)] = {
            "doc_cover": round(sum(v[0] for v in values) / len(values), 6),
            "bridge": round(sum(v[1] for v in values) / len(values), 6),
            "full_bridge": round(sum(v[2] for v in values) / len(values), 6),
        }
    return metrics


# ---------------------------------------------------------------- 材料装载


def _material_of(source: Source, ref: str) -> Material:
    return Material(ref=ref, source=source,
                    content_hash=knowledge_library.content_hash(source.text or ""))


def _read_text(path: Path) -> Material:
    source = parse_text(path.read_text(encoding="utf-8"), kind=SourceKind.markdown)
    # 一律用 posix 分隔符记 ref：这份名字会进报告与基线，Windows 的反斜杠在
    # 别的机器上读起来像转义。
    ref = path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else path.name
    return _material_of(source, ref)


def _read_document(path: Path) -> Material:
    source = parse_bytes(SourceKind.document, path.name, path.read_bytes())
    if not (source.text or "").strip():
        raise SystemExit(f"解析不出正文：{path}")
    return _material_of(source, path.name)


def load_materials(paths: Sequence[Path], docx_paths: Sequence[Path]) -> list[Material]:
    """按后缀分发：`md/txt` 文本直通，其余走 `parse_bytes`（docx → Docling）。

    docx 默认**不在**材料集里（要显式 `--docx`）：解析一份 docx 本机实测 17 秒（冷缓存
    更久，`scripts/measure_library.py` 记的 55 秒是首次加载模型），而默认那一套材料必须
    只依赖仓库里的文件——否则 `docs/RETRIEVAL-EVAL.md` 里的基线就只在这台机器上成立，
    `--check` 换台机器必挂。
    """
    materials: list[Material] = []
    for path in paths:
        if not path.exists():
            raise SystemExit(f"材料不存在：{path}")
        text_suffix = path.suffix.lower() in {".md", ".markdown", ".txt"}
        materials.append(_read_text(path) if text_suffix else _read_document(path))
    for path in docx_paths:
        if not path.exists():
            print(f"跳过（不存在）：{path}", file=sys.stderr)
            continue
        materials.append(_read_document(path))
    return materials


# ---------------------------------------------------------------- 单条 case 打分


def evaluate_case(
    case: EvalCase,
    ranked: Sequence[tuple[int, int, int]],
    top_k: Sequence[int],
) -> dict[int, dict[str, float]]:
    """把一条 case 的检索结果算成各 k 下的四个指标。

    `ranked` 是 `[(名次, start, end)]`，只含**本材料**的命中，名次是它在整次检索里
    的原始位置（跨材料竞争之后的位置，不压缩）——压缩会让"前面被别的材料占了两个
    位"这件事从指标里消失。

    新增覆盖按名次累加：第 `rank` 条只记"之前的命中没覆盖过的那些字"，于是
    `Σ新增 / L` 天然 ≤ 1，多段 relevant 也不会被重复计数。
    """
    relevant = _merge(case.relevant)
    total = sum(end - start for start, end in relevant)
    steps: list[tuple[int, bool, float]] = []
    covered: list[tuple[int, int]] = []
    for rank, start, end in sorted(ranked):
        span = (start, end)
        piece = _clip(span, relevant)
        gap = 0
        if piece:
            gap = sum(high - low for low, high in piece) - _overlap_len(piece, covered)
            covered = _merge([*covered, *piece])
        gain = (gap / total) if total > 0 and gap > 0 else 0.0
        steps.append((rank, hit_is_relevant(span, relevant), gain))

    first_hit: int | None = None
    gains: list[float] = []
    discounts: list[float] = []
    running_gain = 0.0
    running_dcg = 0.0
    for rank, hit, gain in steps:
        running_gain += gain
        running_dcg += gain / math.log2(rank + 1)
        gains.append(min(running_gain, 1.0))
        discounts.append(min(running_dcg, 1.0))
        if hit and first_hit is None:
            first_hit = rank

    result: dict[int, dict[str, float]] = {}
    for k in sorted({int(value) for value in top_k}):
        depth = sum(1 for rank, _, _ in steps if rank <= k)
        hit = first_hit is not None and first_hit <= k
        result[k] = {
            "recall": 1.0 if hit else 0.0,
            "mrr": (1.0 / first_hit) if hit and first_hit else 0.0,
            "ndcg": discounts[depth - 1] if depth else 0.0,
            "coverage": gains[depth - 1] if depth else 0.0,
        }
    return result


# ---------------------------------------------------------------- 检索与打分


def _flag(value: bool) -> str:
    return "1" if value else "0"


@contextmanager
def _env(**values: str) -> Iterator[None]:
    """临时改环境变量，退出还原。检索的开关全在环境变量上（见 `service.py`）。"""
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, old in previous.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


PERSIST_DB_ENV = "MINDMAP_EVAL_DB_PATH"
"""把材料入库到**固定库**而不是临时库。只在量图检索时用。

为什么非它不可：图上的每条边存的是**入库那一刻**的持久 `source_id`，而
`library.ingest` 的 id 是 `uuid4`（同一个正文在两个库里是两个 id）。默认的临时库
每次换路径，于是图证据的 `source_id` 在 `library.load` 里查不到，`_Resolver` 把它
整条丢掉——`--graph 1` 会与 `--graph 0` 逐位相同，而且**看不出原因**（丢证据是
`logger.debug`）。所以建图与评估必须指同一个库文件：建图那一刻 `MINDMAP_DB_PATH`
指哪，评估就把它写进 `MINDMAP_EVAL_DB_PATH`。
"""


@contextmanager
def _id_space(materials: Sequence[Material], *, need_library: bool) -> Iterator[dict[str, str]]:
    """给出 `source_id → 指纹` 的映射；需要时把材料入库到（临时或固定）库。

    这是整条链路最容易出错的地方：请求内 `Source.id` 是 uuid4，库命中的 `source_id`
    是持久 id，两套身份指向同一份正文。`EvalCase` 只认 `content_hash`，所以两个
    id 空间都得登记——漏了任何一个，那批命中都会因为"查不到指纹"被静默判成别的
    材料（`library=False` 时 id_map 为空，就是把全部 recall 变成 0 的那个 bug）。

    默认库落临时目录、跑完删掉：**不碰** `backend/data/mindmaps.db`。设了
    `MINDMAP_EVAL_DB_PATH` 就改用那个文件并且**不删**（理由见那个常量）——重复
    `ingest` 同一份正文是幂等的（`UNIQUE(content_hash)`），所以多条 case 共用它安全。
    """
    id_map = {material.source.id: material.content_hash for material in materials}
    if not need_library:
        yield id_map
        return
    persistent = os.getenv(PERSIST_DB_ENV, "").strip()
    work = None
    if persistent:
        db_path = Path(persistent)
        db_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        work = Path(tempfile.mkdtemp(prefix="retrieval-eval-"))
        db_path = work / "library.db"
    previous = os.environ.get("MINDMAP_DB_PATH")
    os.environ["MINDMAP_DB_PATH"] = str(db_path)
    try:
        for material in materials:
            id_map[knowledge_library.ingest(material.source)] = material.content_hash
        yield id_map
    finally:
        if previous is None:
            os.environ.pop("MINDMAP_DB_PATH", None)
        else:
            os.environ["MINDMAP_DB_PATH"] = previous
        if work is None:
            return
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = Path(f"{db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()
        try:
            work.rmdir()
        except OSError:
            pass


def _ranked_hits(
    dataset: Dataset,
    config: Config,
    query: str,
    depth: int,
    id_map: dict[str, str],
) -> list[tuple[str, int, int]]:
    """跑一次检索，把命中摊成 `[(指纹, start, end)]`（顺序即名次）。"""
    sources = [] if config.library else [material.source for material in dataset.materials]
    search = search_parent if config.parent else search_sources
    with _env(
        MINDMAP_HYBRID_SEARCH=_flag(config.hybrid),
        MINDMAP_PARENT_RETRIEVAL=_flag(config.parent),
        MINDMAP_SOURCE_MODE=config.source_mode,
    ):
        hits = search(sources, query, depth, library=config.library)
    return [(id_map.get(hit.source_id, ""), hit.start, hit.end) for hit in hits]


def score_config(
    dataset: Dataset,
    config: Config,
    top_k: Sequence[int],
    id_map: dict[str, str],
) -> dict:
    """跑完一个配置：逐 case 打分，再按指标取平均。

    同一个 query 只检索一次，结果按 query 缓存给全部 case 复用（同一份材料里的
    标题本身互不重复，跨材料偶有同名标题——那种 case 本来就该共享同一批命中）。
    """
    depth = max(top_k)
    cache: dict[str, list[tuple[str, int, int]]] = {}
    per_hash: dict[str, list[dict[int, dict[str, float]]]] = {
        material.content_hash: [] for material in dataset.materials}
    for case in dataset.cases:
        if case.query not in cache:
            cache[case.query] = _ranked_hits(dataset, config, case.query, depth, id_map)
        ranked = [
            (rank, start, end)
            for rank, (fingerprint, start, end) in enumerate(cache[case.query], start=1)
            if fingerprint == case.source_hash
        ]
        per_hash.setdefault(case.source_hash, []).append(evaluate_case(case, ranked, top_k))

    metrics: dict[str, dict[str, float]] = {name: {} for name in METRIC_NAMES}
    for k in sorted({int(value) for value in top_k}):
        for name in METRIC_NAMES:
            values = [scores[k][name] for bucket in per_hash.values() for scores in bucket]
            metrics[name][str(k)] = round(sum(values) / len(values), 6) if values else 0.0

    refs = {material.content_hash: material.ref for material in dataset.materials}
    by_material: dict[str, dict] = {}
    for fingerprint, bucket in per_hash.items():
        ref = refs.get(fingerprint, fingerprint[:8])
        if not bucket:
            by_material[ref] = {"cases": 0, f"recall@{depth}": 0.0}
            continue
        by_material[ref] = {
            "cases": len(bucket),
            f"recall@{depth}": round(
                sum(scores[depth]["recall"] for scores in bucket) / len(bucket), 6),
        }

    result = {
        "label": config.label(),
        "source_mode": config.source_mode,
        "hybrid": config.hybrid,
        "parent": config.parent,
        "library": config.library,
        "cases": len(dataset.cases),
        "metrics": metrics,
        "by_material": by_material,
    }
    # 跨文档套额外算一组「覆盖了几份材料」：`cases` 已经被摊成 (query × 材料) 对，
    # 那一侧的 recall 只回答"每条证据各自命中没有"，回答不了"两份都拿到了没有"。
    # 复用同一个 `cache`：同一批 query 的命中不再检索第二遍（一次检索真调 embedding）。
    crossdoc = crossdoc_metrics(dataset, config, top_k, id_map, cache=cache)
    if crossdoc:
        result["crossdoc"] = crossdoc
    return result


def run_matrix(
    dataset: Dataset,
    configs: Sequence[Config],
    top_k: Sequence[int],
    *,
    ladder: bool = False,
    id_map: dict[str, str] | None = None,
) -> dict:
    """跑完一个测试集的所有配置，组装成一份单套报告。

    `id_map` 默认自己建（单套跑法）；两套一起跑时由 `run_suites` 传入同一份——库只
    ingest 一次，两套共用一套身份映射，省掉重复入库与临时目录的反复改动。
    """
    if id_map is not None:
        return _run_matrix(dataset, configs, top_k, ladder=ladder, id_map=id_map)
    need_library = any(config.library for config in configs)
    with _id_space(dataset.materials, need_library=need_library) as built:
        return _run_matrix(dataset, configs, top_k, ladder=ladder, id_map=built)


def _run_matrix(
    dataset: Dataset,
    configs: Sequence[Config],
    top_k: Sequence[int],
    *,
    ladder: bool,
    id_map: dict[str, str],
) -> dict:
    started = time.perf_counter()
    rows = [score_config(dataset, config, top_k, id_map) for config in configs]
    probe = None
    if ladder:
        anchor = next((c for c in configs if c.parent and not c.library), configs[0])
        probe = distance_ladder(dataset, anchor, top_k, id_map)
    report = {
        "suite": dataset.suite,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "embedding_configured": bool(embedding_configured()),
        "top_k": sorted({int(value) for value in top_k}),
        "case_count": len(dataset.cases),
        # 无信息检索的地板：真实 recall@1 减去它才是检索自己的贡献。
        "chance_recall@1": chance_recall_at_1(dataset),
        "materials": [dataset.stats[ref] for ref in dataset.stats],
        "configs": rows,
    }
    if dataset.annotations:
        report["annotations"] = dataset.annotations
    if probe is not None:
        report["ladder"] = probe
    return report


def run_suites(
    datasets: dict[str, Dataset],
    configs: Sequence[Config],
    top_k: Sequence[int],
    ladder_suites: set[str],
) -> dict:
    """两套一起跑：外层 `suites` 并列，**每套的指标各自算、绝不混平均**。

    混着算出来的均值既不是"标题查询"的分数也不是"真实提问"的分数，而两套的难度差
    20pp 会被一个看起来合理的中间数吃掉——那正是这套评估要防的那类假信号。
    """
    materials = next(iter(datasets.values())).materials
    need_library = any(config.library for config in configs)
    bundle = {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "embedding_configured": bool(embedding_configured()),
        "top_k": sorted({int(value) for value in top_k}),
        "suites": {},
    }
    with _id_space(materials, need_library=need_library) as id_map:
        for name in ALL_SUITE_NAMES:
            dataset = datasets.get(name)
            if dataset is None:
                continue
            bundle["suites"][name] = _run_matrix(
                dataset, configs, top_k, ladder=name in ladder_suites, id_map=id_map)
    bundle["case_count"] = sum(item["case_count"] for item in bundle["suites"].values())
    return bundle


# ---------------------------------------------------------------- 偏差探针


def _bigrams(text: str) -> list[str]:
    """中文 bigram（与检索同口径：先剔空白，再两两成组）。"""
    chars = [char for char in text if not char.isspace()]
    if len(chars) < 2:
        return chars
    return [chars[index] + chars[index + 1] for index in range(len(chars) - 1)]


def wording_overlap(query: str, text: str) -> float:
    """query 的 bigram 有多少**逐字出现在**目标章节的 token 流里。

    两边都先剔空白再比：检索侧的 `_bigram_terms` 正是这样切的（`0 一页摘要` 那种
    带空格的标题，不剔空白就永远凑不出跨空格的 bigram，重合率会被莫名压低）。

    这不是相关性判据，是"这条 query 离材料原话有多远"的刻度：1.0 表示 query 的每个
    bigram 都在目标章节中（标题当 query 必然如此——标题就是章节的第一行），越低表示
    提问者换了说法。
    """
    grams = _bigrams(query)
    if not grams:
        return 0.0
    squashed = "".join(char for char in text if not char.isspace())
    return sum(1 for gram in grams if gram in squashed) / len(grams)


def chance_recall_at_1(dataset: Dataset) -> float:
    """**无信息检索**的 recall@1 期望值：随机排第一。

    父块均匀铺满原文、一块约 `CHUNK_TARGET_CHARS` 字，所以"随机一块落在这一章里"
    的概率约等于 `章节字数 / 全文长度`（再乘上 20% 判定阈值那点修正，忽略不计）。
    它给出这张表的**地板**：真实 recall@1 减去它就是检索本身的贡献。

    取 case 平均；多段 relevant 按合并后长度算。
    """
    lengths = {material.content_hash: len(material.text) for material in dataset.materials}
    values = []
    for case in dataset.cases:
        total = lengths.get(case.source_hash, 0)
        if total <= 0:
            continue
        span = sum(end - start for start, end in _merge(case.relevant))
        values.append(span / total)
    return round(sum(values) / len(values), 6) if values else 0.0


def _first_body_sentence(section: str) -> str | None:
    """章节正文的第一句（跳过标题行）。取不到返回 None。

    这是"长而啰嗦的提问"的替身：真实用户问题不是关键词堆叠，而是一整句带冗余的
    话。它与标题的区别不在词面（这句话本身就是章节里的原文），而在**长度与形状**
    ——BM25 的长度归一化、父块命中的覆盖量都会因此不同。
    """
    lines = section.splitlines()
    body = "\n".join(lines[1:]).strip() if lines else ""
    if not body:
        return None
    match = re.search(r"[。！？!?]", body)
    sentence = re.sub(r"\s+", "", body[: match.end()] if match else body)
    return sentence[:200] if len(sentence) >= MIN_TITLE_CHARS else None


# 台阶定义：`名字 → (变换, 说明)`。变换只碰 query 文本，不碰 relevant——所以四个
# 台阶量与主测试集是同一批 case、同一批原文区间，可比。
LADDER_RUNGS = (
    "title",
    "title_drop",
    "title_half",
    "body_sentence",
)


def _ladder_query(name: str, case: EvalCase, texts: dict[str, str]) -> str | None:
    """按台阶造 query。`None` = 这条 case 在这一档不适用（正文取不到句子）。"""
    if name == "title":
        return case.query
    if name == "title_drop":
        # 每三个字里去掉第三个：破坏 bigram 但保留可辨认的残形，模拟"记得大概说法、
        # 记不住原话"。比整段截断更接近真实的口误/转述。
        query = case.query
        if len(query) < 6:
            return query
        return "".join(char for index, char in enumerate(query) if index % 3 != 2)
    if name == "title_half":
        # 只有前半截：丢掉一半词面，且后半截的区分词全部消失。
        return case.query[: max(MIN_TITLE_CHARS, len(case.query) // 2)]
    start, end = _merge(case.relevant)[0]
    return _first_body_sentence(texts[case.source_hash][start:end])


def distance_ladder(
    dataset: Dataset,
    config: Config,
    top_k: Sequence[int],
    id_map: dict[str, str],
) -> list[dict]:
    """把 query 从"标题原文"逐步推到"记不住原话"，看指标掉多少。

    四个台阶（`LADDER_RUNGS`），全部只依赖材料结构，一个模型都不调：

      - `title`：标题原文（主测试集就是这一档，`wording_overlap` 恒为 1.0）；
      - `title_drop`：每三个字去掉一个——模拟"记得大概说法"；
      - `title_half`：只留前半截——模拟"只记得半个说法"；
      - `body_sentence`：章节正文第一句——长而啰嗦、但逐字来自材料。

    每档同时报 `wording_overlap`，把"掉多少"与"离原话多远"绑成一组数。这条曲线是
    "标题当 query"那份偏差的**量纲**：真实用户问法的重合率不会停在 1.0。
    """
    texts = {material.content_hash: material.text for material in dataset.materials}
    ks = sorted({int(value) for value in top_k})
    widths = ks[-1]
    rungs: list[dict] = []
    for name in LADDER_RUNGS:
        cases: list[EvalCase] = []
        for case in dataset.cases:
            query = _ladder_query(name, case, texts)
            if query is None:
                continue
            cases.append(EvalCase(query=query, source_hash=case.source_hash,
                                  relevant=case.relevant))
        if not cases:
            continue
        rungs.append(_ladder_rung(name, cases, dataset, config, ks, widths, texts, id_map))
    return rungs


def _ladder_rung(
    name: str,
    cases: Sequence[EvalCase],
    dataset: Dataset,
    config: Config,
    ks: Sequence[int],
    widths: int,
    texts: dict[str, str],
    id_map: dict[str, str],
) -> dict:
    """跑一个台阶：recall@k / coverage@max，外加词面重合。"""
    scores: list[dict[int, dict[str, float]]] = []
    cache: dict[str, list[tuple[str, int, int]]] = {}
    for case in cases:
        if case.query not in cache:
            cache[case.query] = _ranked_hits(dataset, config, case.query, widths, id_map)
        ranked = [
            (rank, start, end)
            for rank, (fingerprint, start, end) in enumerate(cache[case.query], start=1)
            if fingerprint == case.source_hash
        ]
        scores.append(evaluate_case(case, ranked, ks))
    overlaps = []
    for case in cases:
        start, end = _merge(case.relevant)[0]
        overlaps.append(wording_overlap(case.query, texts[case.source_hash][start:end]))
    metrics = {
        str(k): {
            "recall": round(sum(score[k]["recall"] for score in scores) / len(scores), 6),
            "coverage": round(sum(score[k]["coverage"] for score in scores) / len(scores), 6),
        }
        for k in ks
    }
    return {
        "rung": name,
        "cases": len(cases),
        "wording_overlap": round(sum(overlaps) / len(overlaps), 6),
        "metrics": metrics,
    }


# ---------------------------------------------------------------- 输出


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _display_columns(top_k: Sequence[int]) -> list[str]:
    ks = sorted({int(value) for value in top_k})
    primary = 3 if 3 in ks else ks[0]
    return [*[f"R@{k}" for k in ks], f"MRR@{primary}", f"NDCG@{ks[-1]}", f"COV@{ks[-1]}"]


def _row_values(config_row: dict, top_k: Sequence[int]) -> list[str]:
    ks = sorted({int(value) for value in top_k})
    primary = 3 if 3 in ks else ks[0]
    metrics = config_row["metrics"]
    return [
        *[_pct(metrics["recall"][str(k)]) for k in ks],
        _pct(metrics["mrr"][str(primary)]),
        _pct(metrics["ndcg"][str(ks[-1])]),
        _pct(metrics["coverage"][str(ks[-1])]),
    ]


def _pad(cells: Sequence[str], widths: Sequence[int], *, left_first: bool = True) -> str:
    lined = [cells[0].ljust(widths[0])] if left_first else [cells[0].rjust(widths[0])]
    lined += [cells[index].rjust(widths[index]) for index in range(1, len(cells))]
    return "  ".join(lined)


def render_table(report: dict) -> str:
    """控制台对照表：一行一个配置，列是指标。"""
    top_k = report["top_k"]
    header = ["配置", "case", *_display_columns(top_k)]
    rows = [[row["label"], str(row["cases"]), *_row_values(row, top_k)]
            for row in report["configs"]]
    widths = [max(len(line[index]) for line in [header, *rows]) for index in range(len(header))]
    lines = [_pad(header, widths), "  ".join("─" * width for width in widths)]
    lines += [_pad(row, widths) for row in rows]
    return "\n".join(lines)


def _markdown_table(report: dict) -> list[str]:
    """一份单套报告的表格行（表头 + 分隔 + 每个配置一行）。"""
    top_k = report["top_k"]
    header = ["配置", "case", *_display_columns(top_k)]
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["---"] + ["---:"] * (len(header) - 1)) + "|",
    ]
    for row in report["configs"]:
        lines.append("| " + " | ".join(
            [row["label"], str(row["cases"]), *_row_values(row, top_k)]) + " |")
    return lines


def render_markdown(report: dict) -> str:
    """单套报告 → 可直接贴进 `docs/` 的表 + 一段机器可读的基线块。

    基线块是 `--check` 的唯一输入：**没有它，`--check` 读不到基线**。贴表的时候连
    围栏一起贴，不要只贴上面那张人看的表。
    """
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    return (
        "\n".join(_markdown_table(report))
        + "\n\n<!-- 下面这段是 --check 的基线：不要删、不要改格式（最后一段 json 围栏）。 -->\n"
        + "```json\n" + payload + "\n```\n"
    )


SUITE_TITLES = {
    "title": "title 套（query = 章节标题原文，自动构造）",
    "manual": "manual 套（28 条手写真实问法）",
    "crossdoc": "crossdoc 套（14 条跨文档问法，量图检索用）",
}


def render_bundle_markdown(bundle: dict) -> str:
    """两套 → 两张表 + **一段**围栏。

    只留一段围栏是要紧的：`--check` 读的是"最后一段 json 围栏"，若两套各写一段，
    它会读到 manual 那套——而 `--check` 的契约是只比 `title` 套。所以两份报告一起
    塞进同一个对象（`suites.title` / `suites.manual`），`baseline_from_markdown`
    再从里面挑 `title`。
    """
    blocks: list[str] = []
    for name in ALL_SUITE_NAMES:
        report = bundle.get("suites", {}).get(name)
        if report is None:
            continue
        blocks.append(f"### {SUITE_TITLES.get(name, name)}\n\n"
                      + "\n".join(_markdown_table(report)))
    payload = json.dumps(bundle, ensure_ascii=False, indent=2)
    return (
        "\n\n".join(blocks)
        + "\n\n<!-- 下面这段是 --check 的基线（两套都在里面，--check 只取 title 套）："
          "不要删、不要改格式（最后一段 json 围栏）。 -->\n"
        + "```json\n" + payload + "\n```\n"
    )


def baseline_from_markdown(text: str) -> dict:
    """从 `docs/RETRIEVAL-EVAL.md` 里取基线：**最后一段** ```json 围栏里的 `title` 套。

    兼容两种载荷：v2 外层（`{"suites": {"title": …}}`）与旧的 v1 单套报告。两种情况
    都只返回**一套**（title），因为 `--check` 的契约就是"只比 title 套"。
    """
    blocks = re.findall(r"```json[ \t]*\r?\n(.*?)\r?\n```", text, re.DOTALL)
    if not blocks:
        raise SystemExit(
            "基线文件里没有 ```json 围栏。先用 --markdown 产出一段贴进去：\n"
            "    python scripts/evaluate_retrieval.py --markdown -")
    try:
        payload = json.loads(blocks[-1])
    except json.JSONDecodeError as error:
        raise SystemExit(f"基线 JSON 解析失败：{error}") from error
    if isinstance(payload, dict) and "suites" in payload:
        title = payload["suites"].get("title")
        if not isinstance(title, dict):
            raise SystemExit("基线的 suites 里没有 title 套：--check 只比 title 套，"
                             "先用 --markdown 重新生成基线。")
        return title
    return payload


def compare_reports(
    baseline: dict,
    current: dict,
    *,
    tolerance_pp: float = CHECK_TOLERANCE_PP,
) -> list[str]:
    """逐配置逐指标比：掉超过 `tolerance_pp` 个绝对百分点就是一条问题。

    配置消失也算问题——那是"这一行不再被评"的信号，不比对会让覆盖面悄悄变窄。基线
    里没有、本次多出来的配置不算问题（新增维度是好事），由调用方打一行提示。

    `ladder` 一起比（两边都有时）：**这是唯一能抓住"recall 已饱和"那类退化的地方**。
    标题当 query 时 `recall@3` 常年 100%，一次把 k≥3 全打满的退化在配置表里看不出来，
    但在 `title_half` / `title_drop` 那两行上会立刻掉。
    """
    problems: list[str] = []
    current_rows = {row["label"]: row for row in current.get("configs", [])}
    for base_row in baseline.get("configs", []):
        label = base_row["label"]
        now = current_rows.get(label)
        if now is None:
            problems.append(f"{label}：本次运行里没有这个配置")
            continue
        for metric in METRIC_NAMES:
            for k, base_value in base_row["metrics"][metric].items():
                new_value = now["metrics"].get(metric, {}).get(k)
                if new_value is None:
                    problems.append(f"{label}  {metric}@{k}：本次没算出这个指标")
                    continue
                drop = (base_value - new_value) * 100
                if drop > tolerance_pp + 1e-9:
                    problems.append(
                        f"{label}  {metric}@{k}  {_pct(base_value)} → {_pct(new_value)}"
                        f"（-{drop:.1f}pp）")

    current_rungs = {row["rung"]: row for row in current.get("ladder", [])}
    for base_rung in baseline.get("ladder", []):
        name = base_rung["rung"]
        now = current_rungs.get(name)
        if now is None:
            problems.append(f"ladder/{name}：本次运行里没有这一档（--ladder）")
            continue
        for k, base_values in base_rung.get("metrics", {}).items():
            for metric, base_value in base_values.items():
                new_value = now.get("metrics", {}).get(k, {}).get(metric)
                if new_value is None:
                    problems.append(f"ladder/{name}  {metric}@{k}：本次没算出这个指标")
                    continue
                drop = (base_value - new_value) * 100
                if drop > tolerance_pp + 1e-9:
                    problems.append(
                        f"ladder/{name}  {metric}@{k}  {_pct(base_value)} → {_pct(new_value)}"
                        f"（-{drop:.1f}pp）")
    return problems


# ---------------------------------------------------------------- 命令行


def _bool_list(raw: str) -> list[bool]:
    """`0,1` / `both` → `[False, True]`（首次出现的顺序去重）。"""
    tokens = (raw or "both").split(",")
    values: list[bool] = []
    for token in (item.strip().lower() for item in tokens):
        if token in {"both", "all"}:
            candidates = [False, True]
        elif token in {"1", "on", "true", "yes"}:
            candidates = [True]
        elif token in {"0", "off", "false", "no"}:
            candidates = [False]
        else:
            raise SystemExit(f"开关只认 0/1/both，收到：{token}")
        for candidate in candidates:
            if candidate not in values:
                values.append(candidate)
    return values


def _mode_list(raw: str) -> list[str]:
    tokens = (raw or "rag").split(",")
    modes: list[str] = []
    for token in (item.strip().lower() for item in tokens):
        candidates = ["rag", "full"] if token in {"both", "all"} else [token]
        for candidate in candidates:
            if candidate not in {"rag", "full"}:
                raise SystemExit(f"--source-mode 只认 rag/full/both，收到：{candidate}")
            if candidate not in modes:
                modes.append(candidate)
    return modes


def _k_list(raw: str) -> list[int]:
    values = sorted({int(token) for token in raw.split(",") if token.strip()})
    if not values or values[0] < 1:
        raise SystemExit("--top-k 只收正整数，逗号分隔")
    return values


def build_configs(args: argparse.Namespace) -> list[Config]:
    """四个维度做**笛卡尔积**，顺序固定：mode → hybrid → parent → library。"""
    return [
        Config(source_mode=mode, hybrid=hybrid, parent=parent, library=library)
        for mode in _mode_list(args.source_mode)
        for hybrid in _bool_list(args.hybrid)
        for parent in _bool_list(args.parent)
        for library in _bool_list(args.library)
    ]


def configs_from_report(report: dict) -> list[Config]:
    """从基线报告还原配置：`--check` 用它，保证比对两边是同一组开关。"""
    return [
        Config(source_mode=row["source_mode"], hybrid=bool(row["hybrid"]),
               parent=bool(row["parent"]), library=bool(row["library"]))
        for row in report.get("configs", [])
    ]


def _material_paths(args: argparse.Namespace) -> tuple[list[Path], list[Path]]:
    paths = [Path(item).expanduser() for item in (args.material or [])] or list(DEFAULT_MATERIALS)
    return paths, [Path(item).expanduser() for item in (args.docx or [])]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="检索层评估：材料结构造测试集，输出 recall@k / mrr / ndcg / coverage。")
    parser.add_argument("--material", action="append", metavar="PATH",
                        help="材料路径（可重复；缺省用 docs/ 下四份 md）")
    parser.add_argument("--docx", action="append", nargs="?", const=str(DEFAULT_DOCX),
                        metavar="PATH",
                        help="额外的 docx（可重复；不带参数=桌面那份）。docx 解析要十几秒"
                             "（冷缓存更久），所以默认不跑——默认那一套必须**只依赖仓库里"
                             "的文件**，--check 才能在任何机器上跑")
    parser.add_argument("--source-mode", default="rag", metavar="rag|full|both",
                        help="只记录、不影响度量（默认 rag）")
    parser.add_argument("--hybrid", default="", metavar="0,1")
    parser.add_argument("--parent", default="", metavar="0,1")
    parser.add_argument("--library", default="", metavar="0,1")
    parser.add_argument("--rerank", choices=("0", "1"), default="0", metavar="0,1",
                        help="重排开关（默认 0）。**默认关是刻意的**：本文件里记的基线都是"
                             "关着采的，而线上 `MINDMAP_RERANK` 已默认开——评估必须显式"
                             "固定这一维，否则同一份配置在有凭证的机器上会读出另一个数，"
                             "`--check` 会莫名其妙地挂。要看重排的收益就用 --rerank 1 跑一遍"
                             "再与 --rerank 0 比（两份结果不会自动并列，手工对比）。")
    parser.add_argument("--graph", choices=("0", "1"), default="0", metavar="0,1",
                        help="图游走开关（`MINDMAP_GRAPH_SEARCH`，默认 0）。默认关的理由与"
                             "`--rerank` 相同，加上一条：图是**跨机器的外部状态**（Neo4j 在不在、"
                             "图里有什么），不钉住这一维，`--check` 会在配了图的机器上悄悄多一条"
                             "召回通道。Neo4j 不通时 --graph 1 与 --graph 0 逐位相同（降级契约）。")
    parser.add_argument("--top-k", default=",".join(str(k) for k in DEFAULT_TOP_K))
    parser.add_argument("--json", metavar="PATH", help="报告落盘（- 表示 stdout）")
    parser.add_argument("--markdown", nargs="?", const="-", metavar="PATH",
                        help="输出可贴进 docs 的表 + 基线块（缺省 stdout）")
    parser.add_argument("--check", action="store_true",
                        help="与基线比对，掉超过 2pp 退出码 1")
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE), metavar="PATH",
                        help="基线文件（缺省 docs/RETRIEVAL-EVAL.md）")
    parser.add_argument("--suite", choices=(*ALL_SUITE_NAMES, "both"), default=None,
                        help="测试集：title=章节标题自动构造 / manual=28 条手写真实问法 /"
                             " crossdoc=14 条跨文档问法（要 Neo4j 里真有图才有意义）/"
                             " both=title+manual 两套并列（默认）。--check 时默认只跑 title")
    parser.add_argument("--ladder", action="store_true",
                        help="跑 query 距离阶梯（量化「标题当 query」的偏差；只对 title 套）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    top_k = _k_list(args.top_k)
    # 重排是**环境变量**驱动的（`rerank.enabled()`），而且线上已默认开。评估必须把它
    # 显式钉住：不钉的话，同一份配置在有凭证的机器上会多跑一次重排、读出另一个数。
    os.environ["MINDMAP_RERANK"] = args.rerank
    # 图游走同理，而且更尖锐：`MINDMAP_GRAPH_SEARCH` 的读数依赖**图里有什么**，而图是
    # 一个跨机器的外部状态（Neo4j 可能在某台机器上跑着、在另一台上没有）。不钉住它，
    # `--check` 会在"配了图"的机器上悄悄多一条召回通道，基线对比立刻漂 1–2pp 而看不出
    # 原因。要看图的收益就 `--graph 1` 跑一遍再与默认那遍比，与 `--rerank` 完全相同。
    os.environ["MINDMAP_GRAPH_SEARCH"] = args.graph
    # 默认值分两种情形：平时两套都跑（要看见真实那套），--check 只跑 title（回归门槛）。
    suite = args.suite or ("title" if args.check else "both")
    if args.check and suite == "manual":
        raise SystemExit(
            "--check 只比 title 套：manual 套的绝对分更低、方差更大（28 条里一条翻面"
            "就是 3.6pp），不适合当 CI 门槛。要看 manual 请用 --suite both --check，"
            "或直接 --suite manual 人工看数字。")
    if args.check and suite == "crossdoc":
        raise SystemExit(
            "--check 只比 title 套：crossdoc 套的读数取决于**图里有什么**（Neo4j 在不在、"
            "建图花了多少块、抽取精度），那是跨机器的外部状态，不能当 CI 门槛。"
            "要看跨文档套请直接 --suite crossdoc，与 --graph 0/1 各跑一遍手工对比。")

    if args.check:
        baseline = baseline_from_markdown(Path(args.baseline).read_text(encoding="utf-8"))
        configs = configs_from_report(baseline)
        if not configs:
            raise SystemExit("基线里没有任何配置，无法比对")
        top_k = [int(value) for value in baseline.get("top_k", top_k)]
        # 基线里有阶梯就一起跑、一起比：饱和的 recall@k 抓不住的那类退化，只有阶梯
        # 能抓住。少跑一档会让 --check 静默地少看一半。阶梯只对 title 套有意义。
        ladder_suites = {"title"} if baseline.get("ladder") else set()
    else:
        baseline = None
        configs = build_configs(args)
        ladder_suites = {"title"} if args.ladder else set()

    paths, docx = _material_paths(args)
    materials = load_materials(paths, docx)
    datasets: dict[str, Dataset] = {}
    if suite in {"title", "both"}:
        datasets["title"] = build_dataset(materials)
    if suite in {"manual", "both"}:
        available = manual_materials_present(materials)
        if available:
            # 材料 hash 对不上就在这里退出——比跑到一半发现指标掉了要好。
            datasets["manual"] = build_manual_dataset(materials)
        elif suite == "manual":
            raise SystemExit(
                "manual 套需要 docs/ 下那四份 md，本次材料集里没有。"
                "要么去掉 --material（用默认材料集），要么先给新材料补标注。")
        else:
            print("提示：本次材料集不含手写套需要的四份 docs md，manual 套跳过，"
                  "只跑 title 套（回归照旧）。", file=sys.stderr)
    if suite == "crossdoc":
        datasets["crossdoc"] = build_crossdoc_dataset(materials)
    for name in ALL_SUITE_NAMES:
        dataset = datasets.get(name)
        if dataset is not None and not dataset.cases:
            raise SystemExit(f"{name} 套测试集为空：材料里没有可用章节（检查标题/长度阈值）")

    # 有凭证的机器上，hybrid=1 × 多配置是一个时间陷阱：每个 case 每次检索都要真调
    # 一次（或两次）embedding 接口，28 条 case × 8 配置足以把一轮推到十分钟以上，
    # 而多出来的那几行读数在无凭证的机器上根本不存在。跑到超时才反应过来没有意义，
    # 所以在开跑之前就把代价说清楚。
    hybrid_rows = sum(1 for config in configs if config.hybrid)
    if hybrid_rows and embedding_configured():
        cases = sum(len(dataset.cases) for dataset in datasets.values())
        print(f"提示：本次 {len(configs)} 个配置里有 {hybrid_rows} 个 hybrid=1，且本机配了"
              f" embedding 凭证——{cases} 条 case 每条都会真调向量接口。"
              "只想要可比读数时用 --hybrid 0，或把配置缩到一个（--parent 1 --library 0）。",
              file=sys.stderr)

    bundle = run_suites(datasets, configs, top_k, ladder_suites)
    for name in ALL_SUITE_NAMES:
        report = bundle["suites"].get(name)
        if report is None:
            continue
        print()
        _describe(report, datasets[name])
        print()
        print(render_table(report))
        if name == "crossdoc":
            _print_crossdoc(report)
        elif "annotations" in report:
            _print_annotations(report["annotations"], top_k)
        if "ladder" in report:
            _print_ladder(report["ladder"], top_k)

    if args.json:
        _emit(args.json, json.dumps(bundle, ensure_ascii=False, indent=2))
    if args.markdown is not None:
        _emit(args.markdown, render_bundle_markdown(bundle))

    if baseline is None:
        return 0

    problems = compare_reports(baseline, bundle["suites"]["title"])
    print()
    if problems:
        print(f"与基线不符（只比 title 套；容差 {CHECK_TOLERANCE_PP:.1f}pp，"
              f"共 {len(problems)} 处）：")
        for problem in problems:
            print(f"  ↓ {problem}")
        return 1
    print(f"与基线一致（只比 title 套）：没有任何配置掉超过 {CHECK_TOLERANCE_PP:.1f}pp。")
    return 0


def _describe(report: dict, dataset: Dataset) -> None:
    """开头几行交代测试集规模与口径，省得看表的人去翻代码。"""
    total_chars = sum(int(item["chars"]) for item in report["materials"])
    print(f"测试集（{dataset.suite} 套）：{len(dataset.materials)} 份材料 / {total_chars} 字 / "
          f"{report['case_count']} 条 case")
    for item in report["materials"]:
        print(f"  · {item['ref']:<28} {item['chars']:>6} 字  章 {item['outline_entries']:>2}"
              f"  可用 {item['eligible_sections']:>2}  case {item['cases']:>2}")
    if dataset.suite == "title":
        print(f"命中判据：交集 ≥ 命中块长度的 {HIT_OVERLAP_RATIO:.0%}；排除：标题<{MIN_TITLE_CHARS}字"
              f" / 章节<{MIN_SECTION_CHARS}字 / 章节占全文>{MAX_SECTION_SHARE:.0%}")
    elif dataset.suite == "manual":
        low, high = manual_cases.OVERLAP_MEAN_RANGE
        annotations = report.get("annotations", {})
        print(f"手写标注：query 对**目标区间**的词面重合均值 "
              f"{_pct(annotations.get('overlap_mean', 0.0))}（构造区间 "
              f"{low:.0%}–{high:.0%}；标题当 query 恒为 100%）")
        granularity = annotations.get("by_granularity", {})
        print(f"粒度分布：整章 {granularity.get('chapter', 0)} / 章内某段 "
              f"{granularity.get('passage', 0)} / 跨章 {granularity.get('cross', 0)}")
    else:
        low, high = crossdoc_cases.OVERLAP_MEAN_RANGE
        annotations = report.get("annotations", {})
        print(f"跨文档标注：{annotations.get('case_count', 0)} 条问法 / 摊成 "
              f"{annotations.get('pair_count', 0)} 个（query × 材料）用例，平均每条跨 "
              f"{annotations.get('materials_per_case', 0):.2f} 份材料")
        print(f"词面重合：均值 {_pct(annotations.get('overlap_mean', 0.0))}"
              f"（单条 {_pct(annotations.get('overlap_min', 0.0))}–"
              f"{_pct(annotations.get('overlap_max', 0.0))}；构造区间 {low:.0%}–{high:.0%}）")
    print(f"无信息地板：随机排第一的 recall@1 期望 {_pct(report['chance_recall@1'])}"
          "（≈ 章节占全文的比例）")
    if not report["embedding_configured"]:
        print("注意：没有 embedding 凭证，hybrid=1 的行实际走纯 BM25，与 hybrid=0 相同"
              "——这是环境事实，不是指标相同。")


def _print_crossdoc(report: dict) -> None:
    """跨文档套的逐条明细 + 覆盖表。

    逐条明细先给（问法、涉及哪两份材料、重合度），覆盖表后给：看表的人得先知道这 14
    条问的到底是什么，才判得动「doc_cover 与 bridge 差这么多」意味着什么。
    """
    annotations = report.get("annotations", {})
    print()
    print("跨文档 case（重合 = query 的中文 bigram 落在**各目标区间**里的比例）：")
    table = [["#", "材料", "重合", "问法"]]
    for index, case in enumerate(annotations.get("cases", []), 1):
        refs = " + ".join(Path(item["material"]).name for item in case["targets"])
        table.append([str(index), refs, _pct(case["overlap"]), case["query"]])
    widths = [max(len(line[0]) for line in table),
              max(len(line[1]) for line in table),
              max(len(line[2]) for line in table)]
    for line in table:
        print(_pad(line[:3], widths) + "  " + line[3])

    ks = sorted({int(value) for value in report.get("top_k", [])})
    if not ks:
        return
    k = ks[-1]
    print()
    print(f"跨文档覆盖 @top{k}（doc_cover=提到几份 / bridge=落在正确区间几份 / "
          f"full_bridge=两份都拿到）：")
    rows = [["配置", "doc_cover", "bridge", "full_bridge"]]
    for row in report.get("configs", []):
        block = row.get("crossdoc", {}).get(str(k))
        if block is None:
            continue
        rows.append([row["label"], _pct(block["doc_cover"]), _pct(block["bridge"]),
                     _pct(block["full_bridge"])])
    widths = [max(len(line[index]) for line in rows) for index in range(len(rows[0]))]
    for line in rows:
        print(_pad(line, widths))


def _print_annotations(annotations: dict, top_k: Sequence[int]) -> None:
    """手写套的逐条明细：距离档、粒度、重合度、问法。"""
    print()
    print("手写 case（按距离分组；重合 = query 的中文 bigram 落在目标区间里的比例）：")
    table = [["档", "粒", "重合", "材料", "问法"]]
    for case in annotations.get("cases", []):
        table.append([
            case["distance"],
            case["granularity"][:4],
            _pct(case["overlap"]),
            Path(case["material"]).name,
            case["query"],
        ])
    widths = [max(len(line[index]) for line in table) for index in range(len(table[0]) - 1)]
    for line in table:
        print(_pad(line[:-1], widths) + "  " + line[-1])


def _print_ladder(rows: Sequence[dict], top_k: Sequence[int]) -> None:
    ks = sorted({int(value) for value in top_k})
    print()
    print("query 距离阶梯（同一批 case，只换提问的写法）：")
    header = ["台阶", "case", "词面重合", *[f"R@{k}" for k in ks], f"COV@{ks[-1]}"]
    table = [header]
    for row in rows:
        table.append([
            row["rung"],
            str(row["cases"]),
            _pct(row["wording_overlap"]),
            *[_pct(row["metrics"][str(k)]["recall"]) for k in ks],
            _pct(row["metrics"][str(ks[-1])]["coverage"]),
        ])
    widths = [max(len(line[index]) for line in table) for index in range(len(header))]
    for line in table:
        print(_pad(line, widths))


def _emit(target: str, text: str) -> None:
    if target == "-":
        print()
        print(text)
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    print(f"\n已写出：{path}")


if __name__ == "__main__":
    raise SystemExit(main())
