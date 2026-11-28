"""诊断码的**两个出口**普查：哪些码只在 `check()` 之外产生。

一句话问法：**这个仓库里还有多少"只在生成侧产生、事后看不见"的诊断码？**

它回答的方式是两条互相独立的证据，而不是"我 grep 过了"：

  1. **静态**：`app/**/*.py` 里所有 `Diagnostic(code="X")` 的字面量，按文件分区——
     `app/optimization/` 是 `check()` 这一侧（`service.py` 直接产出，
     `cross_branch.py` / `source_refs.py` 是它调用的判据），其余是别的路径
     （生成收尾、导入、编辑…）。
  2. **运行期**：把 `backend/data/acc_trees*` 下所有真树跑一遍 `check()`，收实际
     产出的码。静态说"谁写的"，运行期说"真跑得出来吗"。

两者一对照，某个码落进哪个格子就一目了然：

    两侧都有       已在（不需要动）
    只在 optimization/   事后独有（生成侧看不见，那是另一件事，通常是对的）
    只在别处        **候选：生成侧能发现、事后看不见** ← 本脚本存在的原因
    optimization/ 里写了、运行期一次没产出   可能已有用例覆盖但真树上不出现（正常，
                                            边界小），也可能压根是死码

跑法（在 `backend/` 下）：

    python scripts/check_diagnostic_sides.py
    python scripts/check_diagnostic_sides.py --require-documented   # 有码没判定就非零退出

`--require-documented` 是给 CI 用的：一个码只在 `check()` 之外产生，就说明它**没有**
事后出口，那么"该不该搬"必须有人判过、且判定写在 `docs/DIAGNOSTIC-TWO-SIDES.md` 里。
这条开关把"忘了判定"变成红——判据是"码名出现在那份文档里"，一份可复算的证据，
而不是靠谁记得上一轮查过几条。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

APP = BACKEND / "app"
DATA = BACKEND / "data"
DOC = BACKEND.parent / "docs" / "DIAGNOSTIC-TWO-SIDES.md"
#: `check()` 这一侧：`service.check()` 自己产出的码，加上它直接调用的两个判据模块。
CHECK_SIDE = ("app/optimization/service.py", "app/optimization/cross_branch.py",
              "app/optimization/source_refs.py")

_CODE = re.compile(r'code\s*=\s*"([A-Z][A-Z0-9_]*)"')
#: 引用类码要 `sources` 才判（`check(content, sources)`），所以无 sources 跑真树时
#: 它们必然一个都不出现。这不是"没检出"，是**输入没给**。
_REFERENCE_PREFIXES = ("SOURCE_", "UNSOURCED_", "UNVERIFIED_", "INVALID_",
                       "EMPTY_", "EVIDENCE_")


def _static() -> dict[str, set[str]]:
    found: dict[str, set[str]] = defaultdict(set)
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(BACKEND).as_posix()
        text = path.read_text(encoding="utf-8")
        for code in _CODE.findall(text):
            found[code].add(rel)
    return found


def _runtime() -> dict[str, int]:
    from app.contracts.mindmap import MindMapContent
    from app.optimization.service import check

    counts: dict[str, int] = defaultdict(int)
    for folder in sorted(DATA.glob("acc_trees*")):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                content = payload.get("content") or payload.get("map") or payload
                tree = MindMapContent.model_validate(content)
            except Exception:
                continue
            for diagnostic in check(tree):
                counts[diagnostic.code] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-documented", action="store_true",
                        help="有码只在 check() 之外产生、却没写进 "
                             "docs/DIAGNOSTIC-TWO-SIDES.md 时非零退出（CI 用）")
    args = parser.parse_args()

    static = _static()
    runtime = _runtime()

    def sides(code: str) -> tuple[str, ...]:
        where = static[code]
        return tuple("check" if w in CHECK_SIDE else "other" for w in where)

    both = sorted(c for c in static if "check" in sides(c) and "other" in sides(c))
    check_only = sorted(c for c in static if sides(c) == ("check",))
    other_only = sorted(c for c in static if "check" not in sides(c))
    silent = sorted(c for c in static if not runtime.get(c))

    print(f"静态扫到 {len(static)} 个码（{APP.relative_to(BACKEND).as_posix()}/**/*.py）")
    print(f"运行期跑真树（{DATA.name}/acc_trees*）实际产出 {len(runtime)} 个码\n")

    print(f"两侧都产生（{len(both)}）：")
    for code in both:
        print(f"  {code:38s} 运行期 {runtime.get(code, 0):4d} 次")
    print(f"\n只在 check() 这一侧（{len(check_only)}）：")
    for code in check_only:
        print(f"  {code:38s} 运行期 {runtime.get(code, 0):4d} 次")
    print(f"\n只在 check() 之外产生（{len(other_only)}）——**候选：生成侧能发现、事后看不见**：")
    for code in other_only:
        where = ", ".join(sorted(static[code]))
        print(f"  {code:38s} {where}")
    print(f"\n优化侧写了但真树上一次没出现（{len(silent)}，边界窄或需要 sources）：")
    for code in silent:
        tag = "（引用类，要 sources）" if code.startswith(_REFERENCE_PREFIXES) else ""
        print(f"  {code:38s} {', '.join(sorted(static[code]))}{tag}")

    if args.require_documented:
        if not DOC.exists():
            print(f"\n[fail] 找不到判定文档：{DOC}")
            return 1
        text = DOC.read_text(encoding="utf-8")
        undocumented = [c for c in other_only if c not in text]
        if undocumented:
            print("\n[fail] 这些码只在 check() 之外产生、且没写进 "
                  f"{DOC.name}（谁来判它该不该有事后出口？）：")
            for code in undocumented:
                print(f"  {code:38s} {', '.join(sorted(static[code]))}")
            return 1
        print(f"\n[ok] {len(other_only)} 个「只在 check() 之外」的码都有一份判定"
              f"（{DOC.name}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
