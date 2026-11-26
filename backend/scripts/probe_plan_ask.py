"""骨架那一次到底看到了什么：`_skeleton_outline` / `_material_inventory` / `_plan_ask` 的实拼。

为什么需要它：一级分支是整棵树的覆盖上限，而它只由**骨架那一次调用的 user 消息**决定。
那条消息里"材料长什么样"只有两个来源——目录（`heading_outline`）与清单（文件名+字数）。
所以"一级分支跑题/偏少"这类缺陷，第一步不是猜提示词，而是把这段文字原样打出来看：
**它有没有真的把材料的结构给到模型**。

`docs/ACCEPTANCE-METRICS.md` 的两个缺陷都是这么定位的：
  - 会议纪要 4/7 跑题：`heading_outline` 对真实中文材料返回 0 条 → user 里只有
    主题"+ 文件名 + 字数"，模型只能按"这类文档"的先验写模板；
  - 项目立项 3/6 判死：`heading_outline` 返回的是**两条正文列表项**（"3. 半年内…"），
    它们后面恰好是空行，于是过版式闸；"优先按它划分一级分支"把树钉死在 2 条 →
    `MIN_BRANCHES = 3` 当场判废。

用法：
    cd backend && python scripts/probe_plan_ask.py                 # 20 条样本全过一遍
    cd backend && python scripts/probe_plan_ask.py S17 S08         # 只看这两条
    cd backend && python scripts/probe_plan_ask.py --ask S17       # 连完整 user 消息一起打
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.agent import tools  # noqa: E402
from app.contracts.source import Source, SourceKind  # noqa: E402

SAMPLES = BACKEND / "scripts" / "acc_samples.json"


def _source(sample: dict) -> Source:
    return Source(id=sample["id"], kind=SourceKind.document, status="parsed",
                  filename=f"{sample['title']}.txt", text=sample["text"])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="*", default=None, help="只看这些样本 id")
    parser.add_argument("--ask", default="", help="把这条样本的完整 user 消息打出来")
    parser.add_argument("--json", action="store_true", help="按行输出 JSON（逐条一份）")
    args = parser.parse_args(argv)

    samples = json.loads(SAMPLES.read_text(encoding="utf-8"))["samples"]
    wanted = {item.upper() for item in args.ids}
    for sample in samples:
        if wanted and sample["id"].upper() not in wanted:
            continue
        sources = [_source(sample)]
        outline = tools._skeleton_outline(sources)
        entries = tools._outline_entries(sources)
        titles = [entry.title for entry in entries]
        inventory = tools._material_inventory(sources, has_structure=bool(outline))
        if args.json:
            print(json.dumps({
                "id": sample["id"], "category": sample["category"],
                "chars": len(sample["text"]), "titles": titles,
                "has_outline": bool(outline), "must": sample.get("must", []),
            }, ensure_ascii=False))
            continue
        print(f"\n===== {sample['id']} {sample['category']}（{len(sample['text'])} 字）")
        print(f"目录 {len(titles)} 条：{titles}")
        print(f"清单：{inventory.splitlines()[0] if inventory else '（空）'}")
        print(f"骨架 user 的第一段：主题：{sample['title']}｜目录块 {'有' if outline else '无'}")
        if args.ask and args.ask.upper() == sample["id"].upper():
            ask = tools._plan_ask(sample["title"], outline=outline,
                                  materials=inventory, diagram_type="tree")
            print("\n---- user 消息（原样）----")
            print(ask)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
