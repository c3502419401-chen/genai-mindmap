"""混合材料的骨架：**现状**——无章节那几份在骨架里没有位置。

这份用例集先只钉"现状"，也就是缺陷本身。它只调用**既有**的两个函数
（`knowledge.service.heading_outline` / `agent.tools._outline_entries`），所以
在修法落地前后都成立：修法没有改这两个函数的语义，改的是**调用方**怎么用它们。

实测（`scripts/measure_mixed_skeleton.py --probe`，库里 29 份真实材料）：

    只有 markdown（4 份 / 66,536 字）        30 条章节，覆盖 0.91
    只有 paper（20 份 / 6,658 字）            0 条
    markdown + paper 混（24 份 / 73,194 字）  30 条章节，覆盖 0.83 ← 那 20 份一条都没有
    web + paper 混（22 份 / 16,968 字）       16 条标题，覆盖 0.56 ← 过不了闸，一条都不拼

两条用例各对应一种形状：

  1. **整批二选一**：`heading_outline` 分材料出目录、合并后整批裁剪，于是无章节那几份
     的空目录被静默丢弃——"这批有目录"这个结论把它们一起代表了；
  2. **闸的粒度错了**：`_outline_entries` 用**整批**覆盖率判"拼不拼"，标题明明存在，
     只是被一份长材料摊薄到 60% 以下时，连那些真实存在的标题也一起不拼。
"""
from __future__ import annotations

from app.agent.tools import OUTLINE_MIN_COVERAGE, _outline_entries
from app.contracts.source import Source, SourceKind
from app.knowledge.service import heading_outline, outline_coverage

# 有章节的材料：三章、自己那一份的覆盖率接近 100%。长度刻意要占整批六成以上，
# 否则下面那条"整批二选一"会被覆盖率闸先挡掉，量到的就不是同一件事了。
_CHAPTER_PARA = (
    "海绵城市通过渗、滞、蓄、净、用、排六字方针控制雨水径流，示范区的年径流总量"
    "控制率按规划指标确定，透水铺装与下凹式绿地是最常见的两件工程措施。"
)
CHAPTERED = (
    "# 海绵城市与碳中和\n\n"
    "## 海绵城市\n\n" + _CHAPTER_PARA * 8 + "\n\n"
    "## 碳中和\n\n"
    "排放与吸收相抵之后的净零才是碳中和，排放由增转降的拐点是碳达峰。"
    "两者的时间顺序不能颠倒，达峰越晚，中和的坡度越陡。\n\n"
    "## 雨污分流\n\n"
    "把雨水与污水分成两套管网就是雨污分流，难点在既有管线的交错与错接。\n"
)

# 无章节的材料：纯摘要、没有任何标题行（库里那 20 份 openalex / crossref 的形状）。
PLAIN_ONE = (
    "本文以武汉市汉阳四新示范区为例研究海绵城市建设试点中的海绵策略。"
    "示范区采用透水铺装与下凹式绿地，把年径流总量控制率作为主要指标。"
    "研究结论是海绵城市必须与雨污分流同步推进，否则调蓄能力会被合流制抵消。"
) * 3


def _source(text: str, *, source_id: str, kind: SourceKind = SourceKind.paper,
            filename: str = "material.md") -> Source:
    return Source(id=source_id, kind=kind, status="parsed", filename=filename,
                  text=text)


def _titles(entries) -> list[str]:
    return [entry.title for entry in entries]


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
    # 整批覆盖率只按字数摊薄：混进 297 字的一点，30 条目录的覆盖就从 0.98 掉到 0.66
    assert outline_coverage([titled], alone) > 0.9
    assert outline_coverage([titled, plain], mixed) < 0.7


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
