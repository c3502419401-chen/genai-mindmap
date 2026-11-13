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
    python scripts/evaluate_retrieval.py                     # 控制台表格
    python scripts/evaluate_retrieval.py --json out.json     # 报告落盘
    python scripts/evaluate_retrieval.py --markdown -        # 贴进 docs 的表 + 基线块
    python scripts/evaluate_retrieval.py --check             # 与基线比，掉超 2pp 退出码 1
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

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
REPORT_SCHEMA = "retrieval-eval/v1"

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
    """测试集：case 列表 + 材料清单 + 每份材料的结构统计（都进报告）。"""

    cases: list[EvalCase]
    materials: list[Material]
    stats: dict[str, dict]


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


@contextmanager
def _id_space(materials: Sequence[Material], *, need_library: bool) -> Iterator[dict[str, str]]:
    """给出 `source_id → 指纹` 的映射；需要时把材料入库到临时库并并进同一个映射。

    这是整条链路最容易出错的地方：请求内 `Source.id` 是 uuid4，库命中的 `source_id`
    是持久 id，两套身份指向同一份正文。`EvalCase` 只认 `content_hash`，所以两个
    id 空间都得登记——漏了任何一个，那批命中都会因为"查不到指纹"被静默判成别的
    材料（`library=False` 时 id_map 为空，就是把全部 recall 变成 0 的那个 bug）。

    库落临时目录、跑完删掉：**不碰** `backend/data/mindmaps.db`。
    """
    id_map = {material.source.id: material.content_hash for material in materials}
    if not need_library:
        yield id_map
        return
    work = Path(tempfile.mkdtemp(prefix="retrieval-eval-"))
    previous = os.environ.get("MINDMAP_DB_PATH")
    os.environ["MINDMAP_DB_PATH"] = str(work / "library.db")
    try:
        for material in materials:
            id_map[knowledge_library.ingest(material.source)] = material.content_hash
        yield id_map
    finally:
        if previous is None:
            os.environ.pop("MINDMAP_DB_PATH", None)
        else:
            os.environ["MINDMAP_DB_PATH"] = previous
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = Path(f"{work / 'library.db'}{suffix}")
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

    return {
        "label": config.label(),
        "source_mode": config.source_mode,
        "hybrid": config.hybrid,
        "parent": config.parent,
        "library": config.library,
        "cases": len(dataset.cases),
        "metrics": metrics,
        "by_material": by_material,
    }


def run_matrix(
    dataset: Dataset,
    configs: Sequence[Config],
    top_k: Sequence[int],
    *,
    ladder: bool = False,
) -> dict:
    """跑完所有配置，组装报告（也是 `--json` / 基线块的全部内容）。"""
    started = time.perf_counter()
    with _id_space(dataset.materials, need_library=any(c.library for c in configs)) as id_map:
        rows = [score_config(dataset, config, top_k, id_map) for config in configs]
        probe = None
        if ladder:
            anchor = next((c for c in configs if c.parent and not c.library), configs[0])
            probe = distance_ladder(dataset, anchor, top_k, id_map)
    report = {
        "schema": REPORT_SCHEMA,
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
    if probe is not None:
        report["ladder"] = probe
    return report


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


def render_markdown(report: dict) -> str:
    """可直接贴进 `docs/` 的表 + 一段机器可读的基线块。

    基线块是 `--check` 的唯一输入：**没有它，`--check` 读不到基线**。贴表的时候连
    围栏一起贴，不要只贴上面那张人看的表。
    """
    top_k = report["top_k"]
    header = ["配置", "case", *_display_columns(top_k)]
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["---"] + ["---:"] * (len(header) - 1)) + "|",
    ]
    for row in report["configs"]:
        lines.append("| " + " | ".join(
            [row["label"], str(row["cases"]), *_row_values(row, top_k)]) + " |")
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    return (
        "\n".join(lines)
        + "\n\n<!-- 下面这段是 --check 的基线：不要删、不要改格式（最后一段 json 围栏）。 -->\n"
        + "```json\n" + payload + "\n```\n"
    )


def baseline_from_markdown(text: str) -> dict:
    """从 `docs/RETRIEVAL-EVAL.md` 里取基线：**最后一段** ```json 围栏。"""
    blocks = re.findall(r"```json[ \t]*\r?\n(.*?)\r?\n```", text, re.DOTALL)
    if not blocks:
        raise SystemExit(
            "基线文件里没有 ```json 围栏。先用 --markdown 产出一段贴进去：\n"
            "    python scripts/evaluate_retrieval.py --markdown -")
    try:
        return json.loads(blocks[-1])
    except json.JSONDecodeError as error:
        raise SystemExit(f"基线 JSON 解析失败：{error}") from error


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
    parser.add_argument("--top-k", default=",".join(str(k) for k in DEFAULT_TOP_K))
    parser.add_argument("--json", metavar="PATH", help="报告落盘（- 表示 stdout）")
    parser.add_argument("--markdown", nargs="?", const="-", metavar="PATH",
                        help="输出可贴进 docs 的表 + 基线块（缺省 stdout）")
    parser.add_argument("--check", action="store_true",
                        help="与基线比对，掉超过 2pp 退出码 1")
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE), metavar="PATH",
                        help="基线文件（缺省 docs/RETRIEVAL-EVAL.md）")
    parser.add_argument("--ladder", action="store_true",
                        help="跑 query 距离阶梯（量化「标题当 query」的偏差）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    top_k = _k_list(args.top_k)

    if args.check:
        baseline = baseline_from_markdown(Path(args.baseline).read_text(encoding="utf-8"))
        configs = configs_from_report(baseline)
        if not configs:
            raise SystemExit("基线里没有任何配置，无法比对")
        top_k = [int(value) for value in baseline.get("top_k", top_k)]
        # 基线里有阶梯就一起跑、一起比：饱和的 recall@k 抓不住的那类退化，只有阶梯
        # 能抓住。少跑一档会让 --check 静默地少看一半。
        ladder = bool(baseline.get("ladder"))
    else:
        baseline = None
        configs = build_configs(args)
        ladder = args.ladder

    paths, docx = _material_paths(args)
    dataset = build_dataset(load_materials(paths, docx))
    if not dataset.cases:
        raise SystemExit("测试集为空：材料里没有可用章节（检查标题/长度阈值）")

    report = run_matrix(dataset, configs, top_k, ladder=ladder)
    _describe(report, dataset)
    print()
    print(render_table(report))

    if args.json:
        _emit(args.json, json.dumps(report, ensure_ascii=False, indent=2))
    if args.markdown is not None:
        _emit(args.markdown, render_markdown(report))
    if "ladder" in report:
        _print_ladder(report["ladder"], top_k)

    if baseline is None:
        return 0

    problems = compare_reports(baseline, report)
    print()
    if problems:
        print(f"基线不符（容差 {CHECK_TOLERANCE_PP:.1f}pp，共 {len(problems)} 处）：")
        for problem in problems:
            print(f"  ↓ {problem}")
        return 1
    print(f"与基线一致：没有任何配置掉超过 {CHECK_TOLERANCE_PP:.1f}pp。")
    return 0


def _describe(report: dict, dataset: Dataset) -> None:
    """开头几行交代测试集规模与口径，省得看表的人去翻代码。"""
    total_chars = sum(int(item["chars"]) for item in report["materials"])
    print(f"测试集：{len(dataset.materials)} 份材料 / {total_chars} 字 / "
          f"{report['case_count']} 条 case（每份最多 {MAX_CASES_PER_MATERIAL} 条）")
    for item in report["materials"]:
        print(f"  · {item['ref']:<28} {item['chars']:>6} 字  章 {item['outline_entries']:>2}"
              f"  可用 {item['eligible_sections']:>2}  case {item['cases']:>2}")
    print(f"命中判据：交集 ≥ 命中块长度的 {HIT_OVERLAP_RATIO:.0%}；排除：标题<{MIN_TITLE_CHARS}字"
          f" / 章节<{MIN_SECTION_CHARS}字 / 章节占全文>{MAX_SECTION_SHARE:.0%}")
    print(f"无信息地板：随机排第一的 recall@1 期望 {_pct(report['chance_recall@1'])}"
          "（≈ 章节占全文的比例）")
    if not report["embedding_configured"]:
        print("注意：没有 embedding 凭证，hybrid=1 的行实际走纯 BM25，与 hybrid=0 相同"
              "——这是环境事实，不是指标相同。")


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
