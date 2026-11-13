"""材料章节标题的可提取性评估（只读分析，不改检索与生成）。

要回答的问题：**如果让骨架先看一眼材料的"目录"，这份目录能从现有切块里免费拿到多少、
拿得准不准？** 沿着"加分支数、加 top_k"往上走是线性收益，而"骨架贴材料结构"是另一条
路——它值不值得做，取决于这里的三组数字。

做法：复用 `knowledge.service` 已有的东西，不发明第二套边界规则：

  - `_chunk_tree(text)`：父块（450–700 字）+ 子块；
  - `_HEADING_RE`：切块器**自己**用的标题行首规则（`#`、`第N章节条`、`一、`、
    `（一）`、`1.` / `2、` / `3.1`）。切块器的第一级分隔符就是它。

三组数字：

  1. **父块层**：多少父块的起始行是标题、多少含标题。它回答"能不能靠块边界直接得到
     目录"（不能，就得逐行扫）。
  2. **目录层**：把标题行去重、截断后能拼出多大一份目录、一级标题的字数覆盖、以及
     最大作用域占比（一条标题管住全文 89% 时，这份目录对骨架毫无用处）。
  3. **提取精度**：仓库里的文档都是 markdown，`^#{1,3}` 就是**现成的真值**，可以
     直接量"用 `_HEADING_RE` 当提取器"的 precision / recall——假阳性从哪来（编号列表项）。

用法：`cd backend && python scripts/measure_outline_headings.py`
"""
from __future__ import annotations

import re
import statistics
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.knowledge.service import _chunk_tree, _HEADING_RE  # noqa: E402

MATERIALS = (
    "docs/RAG-DECISIONS.md",
    "docs/AGENT_PLAN.md",
    "docs/DEPLOY.md",
    "docs/ARCHITECTURE.md",
    "docs/CONTRACT_V2.md",
    "docs/PLAN.md",
    "docs/FEATURES_AND_OWNERS.md",
    "docs/GENERATION-PIPELINE-NOTES.md",
    "docs/API.md",
    "docs/SOURCE-REFERENCES.md",
)

TITLE_MAX = 40        # 目录项长度上限：骨架要认"分成哪几块"，不要整行
OUTLINE_MAX = 40      # 目录条数上限


# ---------------------------------------------------------------- 标题识别


def heading_lines(text: str) -> list[tuple[int, str]]:
    """全部标题行：`(行首偏移, 整行文字)`，判定规则**就是切块器那条**。

    `_HEADING_RE` 带 `re.MULTILINE` 且用 `^`：正文中间出现的"1.5 倍"不会被当成标题。
    """
    found: list[tuple[int, str]] = []
    for match in _HEADING_RE.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.start())
        line_end = len(text) if line_end < 0 else line_end
        line = text[line_start:line_end].strip()
        if line:
            found.append((line_start, line))
    return found


def clean_title(line: str) -> str:
    title = re.sub(r"^#{1,6}\s*", "", line).strip()
    title = re.sub(r"\s+", " ", title)
    return title[:TITLE_MAX]


def heading_level(line: str) -> int:
    """标题层级：1 = 一级（可以直接当一级分支），2 = 二级及以下，0 = 文档标题。

    markdown：`#` → 0，`##` → 1，`###` 及更深 → 2；
    中文序号：`第N章` / `一、` → 1，`第N节` / `（一）` → 2；`1.` / `3.1` 按点号个数。
    """
    stripped = line.lstrip()
    if stripped.startswith("#"):
        hashes = len(stripped) - len(stripped.lstrip("#"))
        return 0 if hashes == 1 else (1 if hashes == 2 else 2)
    if re.match(r"^第[一二三四五六七八九十百]+章", stripped):
        return 1
    if re.match(r"^第[一二三四五六七八九十百]+节", stripped):
        return 2
    if re.match(r"^[一二三四五六七八九十]+、", stripped):
        return 1
    if re.match(r"^（[一二三四五六七八九十]+）", stripped):
        return 2
    digits = re.match(r"^(\d+(?:\.\d+)*)", stripped)
    if digits:
        return 1 if "." not in digits.group(1) else 2
    return 2


def looks_like_section(title: str) -> bool:
    """启发式一：这行像不像"章节标题"而不是"编号列表项/表格行"。

    三条都是可证的负例特征（来自本仓库的实测误判）：

      - 含 `**`：`1. **判断器不是噪声源，是系统性失效**。同一 query 跑 3 次…`
        是列表项，不是标题；
      - 含 `|`：`| 4 | 单换行 | 列…` 是表格行；
      - 太长（> `TITLE_MAX`）：真标题很少这么长，长的是散文句。
    """
    if "**" in title or "|" in title:
        return False
    return 0 < len(title) <= TITLE_MAX


def scopes(text: str, headings: list[tuple[int, str]]):
    """每条标题的作用域 `(start, end, level, title)`，end 是下一条**同级或更高级**标题的起点。

    为什么不是"下一条标题"：那样一个 `##` 节的正文会在它第一个 `###` 处被切断，
    于是"一级标题覆盖了多少字"永远偏低——量到的是"每个二级小节之前的导语"，
    不是"一级节有多大"。而一级节的大小正是"一个分支该管多大一块"的量。
    """
    result = []
    for index, (start, line) in enumerate(headings):
        level = heading_level(line)
        end = len(text)
        for later_start, later_line in headings[index + 1:]:
            if heading_level(later_line) <= level:
                end = later_start
                break
        result.append((start, end, level, clean_title(line)))
    return result


def _dense(text: str) -> int:
    """非空白字符数。分母用它：目录覆盖的字数不该被缩进/换行灌水。"""
    return len(re.sub(r"\s", "", text))


# ---------------------------------------------------------------- 单份材料


def evaluate(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    dense = _dense(text)
    tree = _chunk_tree(text)
    parents = [span for span in tree if span.level == 0]
    headings = heading_lines(text)
    offsets = {offset for offset, _ in headings}

    starts_with_heading = sum(
        1 for span in parents
        if _HEADING_RE.match(text[span.start:span.end].lstrip("\n \t").split("\n", 1)[0]))
    contains_heading = sum(
        1 for span in parents
        if any(span.start <= offset < span.end for offset in offsets))

    spans = scopes(text, headings)
    major = [item for item in spans if item[2] == 1]
    major_chars = sum(_dense(text[start:end]) for start, end, _, _ in major)
    largest = max((end - start for start, end, level, _ in spans if level == 1), default=0)

    # 真值：markdown 文档里 `^#{1,3}` 就是章节标题（本仓库 10 份材料全是 markdown，
    # 这个真值只对这一类成立；纯文本 docx/PDF 没有它，那里只能看启发式，见报告）。
    truth = {match.start() for match in re.finditer(r"^#{1,3}\s", text, flags=re.M)}
    candidate = {offset for offset, line in headings if looks_like_section(clean_title(line))}
    hit = candidate & truth

    outline = []
    for _, line in headings:
        title = clean_title(line)
        if looks_like_section(title) and title not in outline:
            outline.append(title)

    # 分级提取：文档里 markdown 标题足够多时**只认它们**（真值即提取结果，P/R=100%），
    # 只有纯文本材料（docx/PDF 抽出来的正文没有 `#`）才退回 `_HEADING_RE` + 启发式。
    markdown_lines = [(match.start(), text[match.start():
                                          text.find("\n", match.start()) if text.find(
                                              "\n", match.start()) > 0 else len(text)])
                      for match in re.finditer(r"^#{1,3}\s", text, flags=re.M)]
    if len(markdown_lines) >= 3:
        dispatched = {offset for offset, _ in markdown_lines}
    else:
        dispatched = candidate
    dispatch_hit = dispatched & truth

    return {
        "name": path.name,
        "chars": len(text),
        "parents": len(parents),
        "parents_starting": starts_with_heading,
        "parents_containing": contains_heading,
        "headings": len(headings),
        "major": len(major),
        "major_share": major_chars / max(1, dense),
        "largest_share": largest / max(1, len(text)),
        "truth": len(truth),
        "candidate": len(candidate),
        "precision": len(hit) / len(candidate) if candidate else 0.0,
        "recall": len(hit) / len(truth) if truth else 0.0,
        "dispatch_precision": (len(dispatch_hit) / len(dispatched) if dispatched else 0.0),
        "dispatch_recall": len(dispatch_hit) / len(truth) if truth else 0.0,
        "outline_items": len(outline[:OUTLINE_MAX]),
        "outline_chars": sum(len(item) for item in outline[:OUTLINE_MAX]),
        "sample": [item[3] for item in major if looks_like_section(item[3])][:8],
    }


# ---------------------------------------------------------------- 报告


def main() -> int:
    rows = [evaluate(REPO / name) for name in MATERIALS if (REPO / name).exists()]
    if not rows:
        print("找不到材料")
        return 1

    table = [("材料", "字数", "父块", "起始=标题", "含标题", "标题行",
              "一级标题", "一级覆盖字数", "最大作用域", "目录项/字符",
              "提取 P", "提取 R", "分级 P")]
    for row in rows:
        table.append((
            row["name"], f"{row['chars']:,}", row["parents"],
            f"{row['parents_starting']}（{row['parents_starting'] / row['parents']:.0%}）",
            f"{row['parents_containing']}（{row['parents_containing'] / row['parents']:.0%}）",
            row["headings"], row["major"], f"{row['major_share']:.0%}",
            f"{row['largest_share']:.0%}",
            f"{row['outline_items']}/{row['outline_chars']}",
            f"{row['precision']:.0%}", f"{row['recall']:.0%}",
            f"{row['dispatch_precision']:.0%}",
        ))
    widths = [max(len(str(row[index])) for row in table) for index in range(len(table[0]))]
    for row in table:
        print("  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)))
        if row is table[0]:
            print()

    starts = [row["parents_starting"] / row["parents"] for row in rows]
    shares = [row["major_share"] for row in rows]
    precisions = [row["precision"] for row in rows]
    recalls = [row["recall"] for row in rows]
    outlines = [row["outline_chars"] for row in rows]
    print()
    print(f"父块起始=标题：中位数 {statistics.median(starts):.0%}"
          f"（{min(starts):.0%}–{max(starts):.0%}）"
          f"；含标题：中位数 "
          f"{statistics.median([row['parents_containing'] / row['parents'] for row in rows]):.0%}")
    print(f"一级标题覆盖字数：中位数 {statistics.median(shares):.0%}"
          f"（{min(shares):.0%}–{max(shares):.0%}）")
    print(f"标题行提取（对 markdown 真值）：P 中位数 {statistics.median(precisions):.0%}"
          f"（{min(precisions):.0%}–{max(precisions):.0%}），"
          f"R 中位数 {statistics.median(recalls):.0%}（{min(recalls):.0%}–{max(recalls):.0%}）")
    dispatch = [row["dispatch_precision"] for row in rows]
    print(f"分级提取（有 markdown 标题就只认它）：P 中位数 {statistics.median(dispatch):.0%}"
          f"（{min(dispatch):.0%}–{max(dispatch):.0%}）")
    ratios = [row["outline_chars"] / max(1, row["chars"]) for row in rows]
    print(f"目录成本：{min(outlines)}–{max(outlines)} 字符"
          f"（占材料 {min(ratios):.1%}–{max(ratios):.1%}）")

    print()
    for row in rows[:3]:
        print(f"{row['name']} 一级标题样本：{row['sample']}")

    print()
    print("假阳性来源（提取器命中了、但不是 markdown 标题的行）：")
    for name in MATERIALS[:3]:
        text = (REPO / name).read_text(encoding="utf-8")
        truth = {match.start() for match in re.finditer(r"^#{1,3}\s", text, flags=re.M)}
        for offset, line in heading_lines(text):
            title = clean_title(line)
            if offset not in truth and looks_like_section(title):
                print(f"  {name}: {title}")
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
