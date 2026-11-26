"""扫"产品代码里零引用的公开函数"——即"用例测了函数、没测接线"的可操作查找法。

## 为什么需要它

这一轮修的三处缺口（`sources` id 漂移 / `/knowledge/search` 是桩 / `link.suggest`
零调用）共同点是**代码在、用例绿、产品里没接上**。它们不会让任何用例变红，
因为用例测的是函数本身。要防这一类，唯一可操作的判据是静态的：

    "这个公开函数被 `backend/app/**` 里的**别的**代码调用过吗？"

没调用过的，要么由框架调用（路由、validator），要么导给外部（`__all__`），
要么**真的没人调**——第三类就是缺口。

## 判据与它自己的边界

1. 目标只取**模块顶层、名字不以 `_` 开头**的 `def`（类方法不在此列：它们通常由
   实例调用，静态判据对它们误报太多）。
2. 引用 = 在 `backend/app/**/*.py` 里除定义行以外，`\bname\b` 的出现次数。
   这是**字面**判据，所以它会漏掉 `getattr(module, "name")` 这类动态调用，
   也会把同名字符串（日志、docstring 里的例子）算成引用——两个方向的误差都在，
   所以输出里带 `hits` 计数，人复核时先看 `hits == 0` 的那批。
3. 三类自动排除的理由各不相同，都不该混进"可疑"里：
   - `route`：被 `@router.*` / `@app.*` 装饰 → 由框架调用；
   - `decorated`：被 `@field_validator` / `@model_validator` 等装饰 → 由 pydantic 调用；
   - `exported`：列在模块 `__all__` 里 → 对外 API，调用方可能在仓库外。

## 跑法

    cd backend
    python scripts/acc3/find_unwired.py              # 汇总 + 清单
    python scripts/acc3/find_unwired.py --suspects   # 只列真正可疑的（plain）

退出码恒为 0：这是体检，不是闸门。把它做成闸门之前先想清楚 `hits == 0` 的
误报率——字面判据上，只要有一条是真的，闸门就挡住了合法的重构。
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
TESTS = Path(__file__).resolve().parents[2] / "tests"
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

_FRAMEWORK_DECORATORS = ("router.", "app.", "field_validator", "model_validator",
                         "validator", "property", "staticmethod", "classmethod",
                         "lru_cache", "cached_property", "on_event", "middleware")


@dataclass
class Entry:
    name: str
    path: str
    line: int
    hits: int = 0
    tags: list[str] = field(default_factory=list)
    #: 只出现在 tests/ 或 scripts/ 里的次数（"用例测了、产品没用"的直接读数）。
    test_hits: int = 0
    script_hits: int = 0

    @property
    def suspect(self) -> bool:
        return self.hits == 0 and not self.tags


def _public_defs(path: Path) -> list[tuple[str, int, list[str]]]:
    """模块顶层的公开函数：`(名字, 行号, 分类标签)`。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    found: list[tuple[str, int, list[str]]] = []
    exported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    try:
                        exported = {item for item in ast.literal_eval(node.value)
                                    if isinstance(item, str)}
                    except ValueError:
                        pass
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_"):
            continue
        tags: list[str] = []
        for decorator in node.decorator_list:
            text = ast.unparse(decorator)
            if any(text.startswith(prefix) for prefix in ("router.", "app.")):
                tags.append("route")
            elif text.split("(")[0] in _FRAMEWORK_DECORATORS or any(
                    text.startswith(prefix) for prefix in _FRAMEWORK_DECORATORS):
                tags.append("decorated")
        if node.name in exported:
            tags.append("exported")
        found.append((node.name, node.lineno, tags))
    return found


def _count(pattern: re.Pattern[str], root: Path, skip: Path | None = None,
           skip_line: int = 0) -> int:
    total = 0
    if not root.exists():
        return 0
    for path in root.rglob("*.py"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, 1):
            if skip is not None and path == skip and index == skip_line:
                continue
            total += len(pattern.findall(line))
    return total


def scan() -> list[Entry]:
    entries: list[Entry] = []
    for path in sorted(APP.rglob("*.py")):
        for name, line, tags in _public_defs(path):
            pattern = re.compile(rf"\b{re.escape(name)}\b")
            entry = Entry(name=name, path=str(path.relative_to(APP.parent)),
                          line=line, tags=tags)
            # 产品代码里的引用：排除定义行自己。
            entry.hits = _count(pattern, APP, skip=path, skip_line=line)
            entry.test_hits = _count(pattern, TESTS)
            entry.script_hits = _count(pattern, SCRIPTS)
            entries.append(entry)
    return entries


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suspects", action="store_true",
                        help="只列真正可疑的（产品代码零引用、且不是路由/装饰器/__all__）")
    args = parser.parse_args(argv)

    entries = scan()
    suspects = [entry for entry in entries if entry.suspect]
    orphans = [entry for entry in entries if entry.hits == 0]
    tagged = Counter(tag for entry in orphans for tag in entry.tags)

    print(f"顶层公开函数：{len(entries)}")
    print(f"  零产品引用（含路由/装饰器/__all__）：{len(orphans)}")
    for tag, count in sorted(tagged.items()):
        print(f"    其中标记为 {tag}：{count}")
    print(f"  真正可疑（零产品引用且无任何豁免标记）：{len(suspects)}")
    print()
    print("判据：在 backend/app/**/*.py 里除定义行外，`\\b函数名\\b` 出现 0 次。")
    print("列出的行里 tests / scripts 两列是「只在用例/脚本里被用到」的次数——")
    print("它们非零而 app 为 0，正是「用例绿、产品没接上」的形状。")
    print()

    shown = suspects if args.suspects else orphans
    by_file: dict[str, list[Entry]] = {}
    for entry in shown:
        by_file.setdefault(entry.path, []).append(entry)
    for path in sorted(by_file):
        for entry in sorted(by_file[path], key=lambda item: item.line):
            marks = ",".join(entry.tags) or "-"
            print(f"{entry.path}:{entry.line}\t{entry.name}\t"
                  f"tests={entry.test_hits}\tscripts={entry.script_hits}\t{marks}")
    print(f"\n合计 {len(shown)} 条。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
