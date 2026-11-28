"""体检：哪些常量的注释里写着"依据"，而那个依据**没有任何东西能复算**。

## 它治的是什么病

本仓库已经撞到两次同一个形态——**注释里的理由是事后补的，而那个理由从没被验证过**：

    GENERATED_LABEL_MAX_CHARS = 16   "180px 宽 14px 字，一行约 12、两行 24"
                                      → 先有 16 再往回找的依据，没有任何实测
    CROSS_REFERENCE_MAX = 8          "一屏里能读懂的交叉指认就那么几处"
                                      → 同上；画布代码里根本没有条数阈值

两次都不是"写错了数"，而是**理由看起来像结论**。读的人（包括下一轮的我）会以为
这个数测过，于是不去测它，于是它永远不必对。

## 判据只有一条：这句理由指不指得向一条能跑的东西

    grounded   注释里有 `scripts/xxx.py` 或 `tests/xxx.py::test_yyy` —— 有人能复算
    ungrounded 写了"依据 / 口径 / 实测"这类断言，却没有锚 —— **可疑**

它**不判对错**：有锚也可能锚在别处。它判的是"这句话是不是一句可以被否掉的话"。

## 为什么它不是 CI 闸门

因为它的判据是**措辞**。给一句"以上 8 条的依据是实测"加上一个脚本名，它就绿了，
而那个脚本可能根本没量这件事。措辞闸的终点是"大家学会改写措辞"——本仓库对这种
闸的态度写在 `docs/SCRIPT-HYGIENE.md` 与 `BACKLOG` 第 12 条里：会因别人正常干活
而变绿变红的门槛，最后只会被忽略。所以这里只给读数，按需跑。

真正该立的规矩是一句话（`docs/CROSS-BRANCH-DUPLICATES.md` §11.1 与
`contracts/mindmap.py::GENERATED_LABEL_MAX_CHARS` 的注释都是这条规矩的产物）：

> 常量注释里出现"依据 / 口径 / 实测"时，必须带一个可复算的锚（脚本 / 用例 / 命令）。
> 指不出来，就写"这个数没有实测依据"，而不要写一个像结论的理由。

用法：

    cd backend
    python scripts/_audit_constant_grounds.py
    python scripts/_audit_constant_grounds.py --only app/optimization/service.py
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"

#: 这些词一出现，说明注释在**声称**有依据（而不是在描述用法）。
GROUNDS_WORDS = (
    "依据", "口径", "实测", "量出来", "量过", "测过", "经验值", "拍的",
    "一屏", "看得清", "读得出", "大约", "约合",
)

#: 「诚实型」：明写这个数是拍的 / 没测过。**它不是负债**——本仓库要的正是这句话。
#: 负债的是下一种：写了一个具体理由（180px 宽两行、一屏能读懂几处），却指不出任何
#: 能复算它的东西。那两句话读起来像结论，于是没人去测它。
HONEST_WORDS = (
    "拍的", "没有实测", "没测过", "未实测", "没有依据", "没有量过", "未量过",
    "凭经验", "猜的", "没有对应物", "不是量出来的",
)

#: 能跑的东西才算锚。`docs/*.md` 不算：文档也可能只是叙述，而叙述不是复算。
ANCHOR = re.compile(
    r"(?:scripts/[A-Za-z0-9_./-]+\.py"
    r"|tests/[A-Za-z0-9_./-]+\.py::[A-Za-z0-9_]+"
    r"|[A-Za-z0-9_]+\.py::[A-Za-z0-9_]+"
    r"|`?--repeat\s+\d+)")

#: 常量名：全大写，允许数字与下划线，下划线开头（私有）也算。
CONST_NAME = re.compile(r"^_?[A-Z][A-Z0-9_]{1,}$")

#: 只认"标量常量"：数字 / 字符串 / 元组与 frozenset 的字面量。正则式那种不算。
SCALAR_NODES = (ast.Constant, ast.Tuple, ast.Set, ast.Call)


def _comment_block(lines: list[str], lineno: int) -> str:
    """`lineno`（1-based，常量那一行）往上的连续注释块。空行截断。"""
    collected: list[str] = []
    index = lineno - 2
    while index >= 0:
        text = lines[index].strip()
        if text.startswith("#"):
            collected.append(text)
        elif not text:
            break
        else:
            # 允许"注释 + 空行 + 注释"的段落：往上再看一层，但只在已收集到内容时。
            if collected:
                break
            break
        index -= 1
    return "\n".join(reversed(collected))


def _is_scalar(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.Set)):
        return True
    # `frozenset({...})` 这类：只看外层是不是个名字调用，内容不重要
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name)


def audit(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.splitlines()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not _is_scalar(node.value):
                continue
            names = [target.id for target in node.targets
                     if isinstance(target, ast.Name) and CONST_NAME.match(target.id)]
            if not names:
                continue
            block = _comment_block(lines, node.lineno)
            if not block:
                continue
            words = [word for word in GROUNDS_WORDS if word in block]
            if not words:
                continue
            honest = any(word in block for word in HONEST_WORDS)
            rows.append({
                "file": path.relative_to(BACKEND).as_posix(),
                "name": names[0],
                "line": node.lineno,
                "words": words,
                # 三档：诚实写了"拍的" ≥ 带锚 ＞ 写了一个像结论的理由却指不出锚
                "band": "honest" if honest else "anchored" if ANCHOR.search(block)
                        else "suspect",
            })
    return rows


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default="", help="只看这些文件（逗号分隔，相对 backend/）")
    args = parser.parse_args(argv)
    paths = sorted(APP.rglob("*.py"))
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        paths = [path for path in paths
                 if path.relative_to(BACKEND).as_posix() in wanted]
    rows = audit(paths)
    grounded = [row for row in rows if row["band"] == "anchored"]
    honest = [row for row in rows if row["band"] == "honest"]
    suspect = [row for row in rows if row["band"] == "suspect"]
    print(f"{len(paths)} 个文件里，{len(rows)} 个常量的注释在谈依据："
          f"带锚 {len(grounded)} 个、诚实写'拍的/没测过' {len(honest)} 个、"
          f"**可疑 {len(suspect)} 个**。")
    if grounded:
        print("\n带锚（有人能复算）：")
        for row in grounded:
            print(f"  {row['file']}:{row['line']}  {row['name']}"
                  f"　（{'/'.join(row['words'])}）")
    print("\n可疑（写了一个像结论的理由，却指不出任何能复算它的东西）：")
    for row in suspect:
        print(f"  {row['file']}:{row['line']}  {row['name']}"
              f"　（{'/'.join(row['words'])}）")
    print("\n这不是闸门：判据是措辞，给一句'以上依据是实测'加上一个脚本名它就会绿。"
          "它的用途是一次体检——把这些名字拿去问'这个数是被量出来的，还是被写出来的'。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
