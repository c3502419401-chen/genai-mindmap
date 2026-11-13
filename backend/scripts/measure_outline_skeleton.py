"""骨架来源的三组对照 + docx 目录实测（本机可复现，无 LLM 密钥）。

上一轮（`measure_deep_slicing.py`）证明了"分片 + 按支检索"把覆盖率从 5.8% 拉到
28.8%，也顺手量出一个天花板：**骨架把标题猜成"背景/现状/方法"这类通用词时，
覆盖率掉到 15.4%**。覆盖度由此由骨架决定——这是本轮要回答的问题：

    **让骨架先看一眼材料自带的章节，覆盖率能涨到多少？**

四组的**唯一差别**是"骨架返回哪 8 条一级分支"，检索路径完全相同（分片生成、
`rag` 形态、每支一次 `search_parent`、query 只发分支标题 = 随代码发布的那一版）：

  | 组 | 骨架来源 |
  |---|---|
  | A  | 主题凭空想：只看主题（"RAG 与召回：一轮评估记录与取舍"）能编出来的 8 条 |
  | A' | 材料小节标题 `^#{2,3}`：上一轮 28.8% 的口径，用来确认脚本与上一轮可比 |
  | B  | **材料目录**：`knowledge.service.heading_outline` 的输出（本次新增的能力） |
  | C  | 通用词：背景/现状/问题/方法/数据/结论/风险/展望（上一轮 15.4% 的对照） |

"模型"仍然是脚本化的（`ScriptedModel`）：它按给定标题回骨架、按本支的 evidence
数组写节点。所以这些数字量的是**覆盖度**——材料被送进模型的范围，不是文笔。
B 组因此读作"模型老老实实采纳了材料目录"时的收益，是这个能力的**上界**；
真实模型会不会采纳，要等有凭证的线上数据（脚本末尾会把真正拼给骨架的那段
目录原样打出来，那就是它可以采纳的东西）。

用法（在 `backend/` 下）：

    python scripts/measure_outline_skeleton.py                  # 四组，纯 BM25
    python scripts/measure_outline_skeleton.py --only AB        # 只跑某几组
    python scripts/measure_outline_skeleton.py --docx <path>    # 只验 docx 目录提取

`--docx` 那一支独立于三组对照：它回答"用户上传的 docx 能不能过这一关"。docx 没有
`#`（除非 Docling 从标题样式推出来），走的是 fallback 那条路，而 fallback 有一串
假阳性过滤——**这是上线前必须验的那一条**。真值取 docx 自己的段落样式
（`w:pStyle` 是 Heading*），于是精度/召回是数出来的，不是眼估的。
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
SCRIPTS = Path(__file__).resolve().parent
for path in (BACKEND, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# 复用上一轮的度量口径与运行器：段落切法、覆盖率定义、支间重合、脚本化模型。
# 复制一份的话，两轮的数字就不可比了——而这张表唯一的用处就是与上一轮比。
import measure_deep_slicing as base  # noqa: E402

from app.contracts.source import Source, SourceKind  # noqa: E402
from app.knowledge.service import heading_outline, outline_coverage  # noqa: E402

MATERIAL = REPO / "docs" / "RAG-DECISIONS.md"
TOPIC = "RAG 与召回：一轮评估记录与取舍"
BRANCHES = base.MAX_BRANCHES          # 8，与上一轮同一口径
SOURCE_ID = "m1"

# A 组：只看主题、没读过材料的模型会写的 8 条。刻意选"合理但不贴材料用词"的那种
# ——它不是坏模型的输出，而是一个认真猜的模型在看材料之前能给的最好结果。
TOPIC_TITLES = (
    "背景与动机", "核心概念", "技术方案", "召回策略",
    "排序与融合", "评估方法", "取舍与权衡", "下一步计划",
)

# C 组：与材料用词无关的通用骨架（上一轮 15.4% 的对照）。
GENERIC_TITLES = base.GENERIC_TITLES


def _depth_headings(text: str) -> tuple[str, ...]:
    """材料小节标题 `^#{2,3}`，与上一轮 `measure_deep_slicing.heading_titles` 同源。

    直接调它而不是重写：A' 组的唯一作用是"确认这一轮的脚本能复现 28.8%"，
    取标题的方式差一个字，那个确认就失效了。
    """
    return base.heading_titles(text)


def _outline_titles(text: str, *, limit: int | None = BRANCHES) -> tuple[str, ...]:
    """B 组：`heading_outline` 的标题。

    `limit=None` 表示"全部收下"——工具本身允许模型给出超过请求条数的分支
    （`_generate_deep` 只记一条 `SKELETON_SIZE_OFF` 诊断，按实际条数继续），
    所以"模型把 10 章都写成分支"也是一条真实的路径。
    """
    source = Source(id=SOURCE_ID, kind=SourceKind.document, status="parsed",
                    filename=MATERIAL.name, text=text)
    entries = heading_outline([source], max_items=limit or 30)
    return tuple(entry.title for entry in entries)


def _pad(titles: tuple[str, ...]) -> tuple[str, ...]:
    """标题不够 N 条时补足（只影响"材料只给了几章"的情形）。

    补出来的条目与材料无关，所以它会拉低该组的覆盖率——这正是真实情形：
    材料只有 5 章而用户要 8 条时，多出来的 3 条只能是模型自己编的。
    """
    padded = list(titles)
    for index in range(1, BRANCHES - len(padded) + 1):
        padded.append(f"补充方面{index}")
    return tuple(padded[:BRANCHES])


def run(text: str, spans: list[tuple[int, int]], groups: str) -> None:
    """六组：A / A′ / B / B′ / B″ / C。

    B 的三个变体不是凑数，它们把这个能力的**不确定区间**框住了：目录有 10 章而
    用户只要 8 条时，"模型丢哪两条"没人能预知——顺序取前 8 是"顺着材料的逻辑走"，
    按覆盖取最大 8 是"只留大章"（也是 `heading_outline(max_items=8)` 的裁剪规则），
    全部收下是"模型无视条数"（工具确实收得下）。
    """
    plans = {
        "A": ("主题凭空想（今天）", _pad(TOPIC_TITLES)),
        "A'": ("材料小节标题 ##/###（上一轮口径）", _pad(_depth_headings(text))),
        "B": ("材料目录：前 8 条", _pad(_outline_titles(text, limit=None)[:BRANCHES])),
        "B'": ("材料目录：覆盖最大的 8 条", _pad(_outline_titles(text, limit=BRANCHES))),
        "B''": ("材料目录：全部 10 条（工具按实际条数收下）", _outline_titles(text, limit=None)),
        "C": ("通用词", GENERIC_TITLES[:BRANCHES]),
    }
    wanted = set(groups.replace("'", "'").replace(",", " ").split())
    order = [name for name in plans if name in wanted] or list(plans)

    results = []
    probes_by_group = {}
    for name in order:
        label, titles = plans[name]
        result, probes = base.run_group(
            name, "mindmap_generate_deep", "rag",
            text=text, spans=spans, titles=titles, topic=TOPIC,
            topic_query=TOPIC, source_id=SOURCE_ID,
            fake_embedding=False, embed_latency=0.0,
            # 生产口径：每支的 query 只发分支标题（`_branch_query`）。上一轮量过
            # `topic-title` 更差（19.2% vs 28.8%），这里不为它再跑一遍。
            query_mode="title",
        )
        results.append((name, label, titles, result))
        probes_by_group[name] = probes

    print(f"材料 {MATERIAL.name}：{len(text):,} 字，切 {len(spans)} 段；"
          f"每支 top-{base.POINTS_PER_BRANCH}，检索路径完全相同")
    print()
    header = ("组", "骨架来源", "支数", "覆盖率", "上界", "引用 写出/留存/逐字", "节点")
    rows = [(name, label, len(titles),
             f"{result.coverage:.1%}", f"{result.coverage_any:.1%}",
             f"{result.refs_written}/{result.refs_kept}/{result.refs_verbatim}",
             result.nodes)
            for name, label, titles, result in results]
    widths = [max(len(str(row[index])) for row in [header, *rows])
              for index in range(len(header))]
    for row in [header, *rows]:
        print("  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)))

    print()
    print("各组骨架（就是喂给检索的 query）：")
    for name, label, titles, _ in results:
        print(f"  {name:3s} {label}：{'、'.join(titles)}")

    for name, label, _, _ in results:
        print()
        print(f"[{name}] {label} 的支间重合：")
        base.branch_overlap(probes_by_group[name], spans, limit=base.POINTS_PER_BRANCH)


def show_gate(text: str) -> None:
    """把生产路径**真正**拼给骨架的那段目录打出来，并报闸口判定。"""
    from app.agent.tools import OUTLINE_MIN_COVERAGE, _skeleton_outline

    source = Source(id=SOURCE_ID, kind=SourceKind.document, status="parsed",
                    filename=MATERIAL.name, text=text)
    sources = [source]
    entries = heading_outline(sources)
    coverage = outline_coverage(sources, entries)
    block = _skeleton_outline(sources)
    print()
    print(f"目录闸口：{len(entries)} 条一级标题，覆盖率 {coverage:.1%}，"
          f"阈值 {OUTLINE_MIN_COVERAGE:.0%} → "
          f"{'拼进骨架' if block else '不拼（回退到自行归纳）'}")
    if block:
        print(block)


# ---------------------------------------------------------------- docx


W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
HEADING_STYLE_RE = re.compile(r"^(?:heading|标题)\s*\d*$", re.IGNORECASE)
BOLD_PARAGRAPH_RE = re.compile(r"^[ \t]*\*\*([^*\n]+)\*\*[ \t]*$", re.MULTILINE)


def docx_paragraphs(path: Path) -> list[tuple[str, str]]:
    """docx 的 `(段落样式名, 文字)`，按正文顺序。

    读 `word/document.xml` 而不是上 python-docx：真值只需要段落样式名，而
    `zipfile + ElementTree` 是标准库（不引入新依赖这一条对本脚本同样有效）。
    """
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    found: list[tuple[str, str]] = []
    for paragraph in root.iter(f"{W_NS}p"):
        style = paragraph.find(f"{W_NS}pPr/{W_NS}pStyle")
        name = (style.get(f"{W_NS}val") or "") if style is not None else ""
        text = "".join(node.text or "" for node in paragraph.iter(f"{W_NS}t")).strip()
        found.append((name, text))
    return found


def docx_report(path: Path) -> None:
    from app.ingestion.service import parse_document_bytes

    raw = path.read_bytes()
    source = parse_document_bytes(path.name, raw)
    text = source.text or ""
    entries = heading_outline([source])
    coverage = outline_coverage([source], entries)
    titles = {entry.title for entry in entries}

    print(f"docx：{path.name}")
    print(f"解析：{len(text):,} 字（Docling → markdown；`#` 开头的行 "
          f"{len(re.findall(r'^#{1,6}[ \t]', text, flags=re.M))} 条）")
    print(f"目录：{len(entries)} 条，覆盖率 {coverage:.1%}")
    print()

    print("提取到的标题（一条最多 40 字，去掉了编号）：")
    for index, entry in enumerate(entries, start=1):
        print(f"  {index:2d}. {entry.title}（{entry.coverage} 字，"
              f"占全文 {entry.coverage / max(1, len(text)):.0%}）")

    paragraphs = docx_paragraphs(path)
    truth = [text_ for style, text_ in paragraphs
             if HEADING_STYLE_RE.match(style or "") and text_]

    bold = [match.group(1).strip() for match in BOLD_PARAGRAPH_RE.finditer(text)]
    print()
    print(f"docx 里**整段加粗**的段落：{len(bold)} 条（这是 Word 手工加粗后的样子，" + \
          "也是 docx 唯一的结构线索）")
    for item in bold:
        print(f"  - {item[:46]}　[{'已收录' if _strip_marker(item) in titles else '未收录'}]")

    print()
    if truth:
        print(f"docx 自带标题样式（`w:pStyle=Heading*`）的段落：{len(truth)} 条")
    else:
        print("docx 自带标题样式（`w:pStyle=Heading*`）的段落：0 条"
              " ——这份文档**没有用 Word 标题样式**，")
        print("      章节名全是手工加粗的，所以「对标题样式算 P/R」在它身上恒为 0/0；")
        print("      真值只能取上面那份加粗清单，逐条人工核对（结论写进报告的 docx 小节）。")
        return
    for item in truth[:40]:
        print(f"  - {item}　[{'已收录' if item in titles else '未收录'}]")
    hit = titles & set(truth)
    print()
    print(f"对 docx 自带标题样式：P={len(hit) / len(titles):.0%}（{len(hit)}/{len(titles)}）"
          f" R={len(hit) / len(set(truth)):.0%}（{len(hit)}/{len(set(truth))}）")


def _strip_marker(text: str) -> str:
    """把 `一、实验须知` 拆成 `实验须知`，用来与目录项对齐。"""
    return re.sub(r"^(?:第[一二三四五六七八九十百]+[章节条][ \t]*|"
                  r"[一二三四五六七八九十]+、[ \t]*|"
                  r"[（(][一二三四五六七八九十]+[）)][ \t]*|"
                  r"\d+(?:\.\d+)*[、.][ \t]*)", "", text).strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="A A' B B' B'' C",
                        help="只跑某几组（A / A' / B / B' / B'' / C，空格分隔）")
    parser.add_argument("--docx", default=None, help="只验这份 docx 的目录提取")
    args = parser.parse_args()

    if args.docx:
        docx_report(Path(args.docx))
        return 0

    text = base.load_material(MATERIAL)
    spans = base.segment(text)
    show_gate(text)
    print()
    run(text, spans, args.only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
