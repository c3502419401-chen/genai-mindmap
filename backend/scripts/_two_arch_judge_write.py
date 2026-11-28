"""把四份盲评 JSON 落成 `data/two-arch-judge/*.json`（评审者只给了文本，这里人工抄一份）。

只做一件事：保证报告里的数字可以逐条回溯到评审证据。改数字前先看
`docs/TWO-ARCH-COMPARISON.md` 第 3 节的判据，别在这里调。

**落盘发生在 `main()` 里，不放模块顶层。** 这不是洁癖：`check_script_hygiene.py`
的 `load` 档会在每次 `--gate` 时把这个文件加载一遍，顶层写文件就意味着**跑一次闸门
就重写一遍 `data/two-arch-judge/` 下的四份证据**——而「跑闸门」不该改动任何数据。
"""
from __future__ import annotations

import json
from pathlib import Path

JUDGE = Path(__file__).resolve().parents[1] / "data" / "two-arch-judge"

SETS = {
    "j1-图01-06": [("图01", 56, 1), ("图02", 53, 0), ("图03", 69, 1),
                   ("图04", 175, 5), ("图05", 69, 0), ("图06", 132, 0)],
    "j2-图07-12": [("图07", 67, 3), ("图08", 71, 1), ("图09", 53, 1),
                   ("图10", 114, 1), ("图11", 68, 1), ("图12", 70, 10)],
    "j3-图13-18": [("图13", 214, 15), ("图14", 80, 5), ("图15", 85, 1),
                   ("图16", 60, 7), ("图17", 71, 3), ("图18", 58, 0)],
    "j4-图19-24": [("图19", 71, 0), ("图20", 64, 6), ("图21", 57, 5),
                   ("图22", 71, 2), ("图23", 76, 3), ("图24", 73, 2)],
}


def main() -> int:
    JUDGE.mkdir(parents=True, exist_ok=True)
    for name, items in SETS.items():
        (JUDGE / f"{name}.json").write_text(json.dumps(
            [{"图": anon, "节点数": nodes, "多余重复处数": count}
             for anon, nodes, count in items], ensure_ascii=False, indent=1),
            encoding="utf-8")
    print("写了", ", ".join(sorted(path.name for path in JUDGE.glob("*.json"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
