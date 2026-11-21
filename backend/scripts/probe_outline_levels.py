"""一次性探针：`_outline_lines` 到底收哪些级别，以及切出来的章是什么形状。

问题（用户提的隐患）：如果一级二级都收，同一段原文会被两支拿到 → 跨支重复
会以另一种形式回来。这个脚本把 RAG-DECISIONS.md 的实际结果打出来。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.contracts.source import Source, SourceKind  # noqa: E402
from app.knowledge.service import _outline_lines, heading_outline  # noqa: E402

DOC = Path(__file__).resolve().parents[2] / "docs" / "RAG-DECISIONS.md"


def main() -> None:
    text = DOC.read_text(encoding="utf-8")
    print(f"文件：{DOC.name}  {len(text)} 字")

    levels: dict[int, int] = {}
    for match in re.finditer(r"^(#{1,6})[ \t]+\S", text, re.MULTILINE):
        levels[len(match.group(1))] = levels.get(len(match.group(1)), 0) + 1
    print("各层 markdown 标题条数：", dict(sorted(levels.items())))

    lines = _outline_lines(text, max_title_chars=40)
    print(f"\n_outline_lines 收到 {len(lines)} 条（只可能是一个级别）")
    for start, title in lines:
        print(f"  @{start:>6}  {title}")

    source = Source(id="src-probe", filename=DOC.name, kind=SourceKind.markdown,
                    text=text)
    entries = heading_outline([source])
    print(f"\nheading_outline 收到 {len(entries)} 条")
    covered = 0
    for index, entry in enumerate(entries, start=1):
        body = text[entry.start:entry.start + entry.coverage]
        covered += entry.coverage
        inner = len(re.findall(r"^###[ \t]+\S", body, re.MULTILINE))
        print(f"  {index:>2}. {entry.title[:34]:<34} start={entry.start:>6} "
              f"coverage={entry.coverage:>5} 章内###={inner}")
    share = covered / len(text) if text else 0
    print(f"\nΣ coverage / 全文 = {covered}/{len(text)} = {share:.1%}")
    first = entries[0].start if entries else 0
    print(f"首个标题之前有 {first} 字（章前导语，任何一支都拿不到）")


if __name__ == "__main__":
    main()
