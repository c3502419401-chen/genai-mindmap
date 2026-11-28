"""给 check-neo4j.ps1 补 UTF-8 BOM。

Windows PowerShell 5.1 会把无 BOM 的 UTF-8 当 GBK 读，中文里的「」/「——」会被解成
智能引号（PowerShell 认 U+2018 当字符串定界符），脚本当场括号失配。写一次就够，
但它必须能被重复执行——`edit` 工具会去掉 BOM，改完脚本跑这个。

**路径按 `__file__` 推，不写本机绝对路径。** 写死 `D:\\genai-mindmap\\...` 的那一版
在 CI 上必然 `FileNotFoundError`（`load` 档连红五次期间，这条一直藏在另一族红下面
没有被看见），而"本地能跑"只是因为那台机器的路径恰好对得上——同一个错误的两种
表现，本地这一侧永远看不见它。

**读写在 `main()` 里，不在模块顶层。** `check_script_hygiene.py` 的 `load` 档会把
本文件加载一遍；跑闸门不该改写仓库里的文件。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

#: `scripts/` 的上两层是仓库根。
TARGET = Path(__file__).resolve().parents[2] / "check-neo4j.ps1"


def main() -> int:
    if not TARGET.is_file():
        print(f"错误：找不到 {TARGET} —— 这个脚本要在仓库树里跑。", file=sys.stderr)
        return 2
    with io.open(TARGET, encoding="utf-8-sig") as handle:
        body = handle.read()
    with io.open(TARGET, "w", encoding="utf-8-sig", newline="\n") as handle:
        handle.write(body)
    with open(TARGET, "rb") as handle:
        print("BOM ok:", handle.read(3))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
