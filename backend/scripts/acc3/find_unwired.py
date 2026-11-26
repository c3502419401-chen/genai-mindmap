"""扫"产品代码里零引用（可达）的公开函数"——即"用例测了函数、没测接线"的可操作查找法。

## 为什么需要它

这一轮修的三处缺口（`sources` id 漂移 / `/knowledge/search` 是桩 / `link.suggest`
零调用）共同点是**代码在、用例绿、产品里没接上**。它们不会让任何用例变红，
因为用例测的是函数本身。要防这一类，唯一可操作的判据是静态的：

    "这个公开函数被 `backend/app/**` 里的**别的**代码调用过吗？"

没调用过的，要么由框架调用（路由、validator），要么导给外部（`__all__`），
要么**真的没人调**——第三类就是缺口。

## 判据 v2：从"字面出现"改成"语法树里被引用"

v1 数的是 `\\b函数名\\b` 在文本里出现几次。它会**漏报**，而且是往最不该漏的方向：
本仓库的注释与 docstring 极爱写函数名（"见 `generation.service.THINKING_MODELS`"、
"`verify_source_refs` 做过了"），于是一个**只被注释提到**的函数会被算成"有引用"，
从此再也不会出现在清单里。这类漏报不会自己暴露，它只让清单变短。

v2 用 `ast` 数**真正的引用节点**：

- `ast.Name`（直接调用 / 当值用）；
- `ast.Attribute`（`module.func`）；
- `ast.alias`（`from m import func`，含 `as`）。

注释与字符串在这棵树里根本不存在，所以"注释里提了一句"不再算接线；反过来
`getattr(m, "name")` 这类动态调用现在算**漏报**（v1 也只是碰巧对）——这一条写在
下面的"边界"里，它不是这次修的，但它是把这份清单当闸门用之前必须承认的。

## 跑法

    cd backend
    python scripts/acc3/find_unwired.py              # 汇总 + 全部零引用清单
    python scripts/acc3/find_unwired.py --suspects   # 只列真正可疑的（plain）
    python scripts/acc3/find_unwired.py --gate       # 闸门模式：退出码非 0 表示有新增

## 闸门（`--gate`）是**棘轮**，不是"零"

绝对判据（"一条零引用都不许有"）挡不住任何东西，只会被人加进豁免名单然后忘掉。
所以闸门比的是**基线**：`scripts/acc3/unwired_baseline.json` 里逐条写着"这条为什么
可以零引用"，闸门只对**基线之外的新增**报错。两条反向检查让基线不会腐烂：

1. 基线里的条目如果已经不再是可疑项 → 打印提示（不红）：修好了就该删这一条；
2. 条目里声明的 `evidence` 文件/用例不存在 → **红**：豁免的理由被删掉了，
   而豁免还在——那正是"理由消失、结论留下"的形状。

## 边界（当闸门之前必须知道）

- 动态分派（`getattr`、注册表、装饰器注入）会**误报**成零引用；
- 只被外部客户端调用（HTTP 路由由框架调，已按 `route` 豁免；但被别的进程/仓库
  直接 import 的库函数不会豁免，那属于 `__all__` 该干的事）；
- 类方法不在此列：它们通常由实例调用，静态判据对它们误报太多。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
APP = BACKEND_DIR / "app"
TESTS = BACKEND_DIR / "tests"
SCRIPTS = BACKEND_DIR / "scripts"
BASELINE = Path(__file__).resolve().parent / "unwired_baseline.json"

_FRAMEWORK_DECORATORS = ("router.", "app.", "field_validator", "model_validator",
                         "validator", "property", "staticmethod", "classmethod",
                         "lru_cache", "cached_property", "on_event", "middleware")


@dataclass
class Entry:
    name: str
    path: str
    line: int
    #: v1 口径：`\\bname\\b` 在文本里出现的次数（含注释/docstring）。留作对照。
    hits: int = 0
    #: v2 口径：`ast` 里真正的引用节点数（app/**，定义行除外）。
    refs: int = 0
    tags: list[str] = field(default_factory=list)
    #: 只出现在 tests/ 或 scripts/ 里的引用数（"用例测了、产品没用"的直接读数）。
    test_refs: int = 0
    script_refs: int = 0

    @property
    def suspect(self) -> bool:
        return self.refs == 0 and not self.tags

    @property
    def key(self) -> str:
        return f"{self.name}"


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


_TOKEN = re.compile(r"\b[A-Za-z_]\w*\b")


def _literal_index(root: Path) -> Counter[str]:
    """v1 口径的索引：文本里每个标识符出现几次（注释、docstring 都算）。

    与 `_reference_index` 同一条理由：一遍扫完建表，别对 515 个名字各扫一遍全树。
    定义行自己也算在这里面，缩进由调用方减掉（每个模块级 `def` 减 1）。
    """
    index: Counter[str] = Counter()
    if not root.exists():
        return index
    for path in root.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        index.update(_TOKEN.findall(text))
    return index


def _reference_index(root: Path) -> Counter[str]:
    """把一棵目录树里**所有被引用的标识符**数一遍（每个文件只 `ast.parse` 一次）。

    为什么要有索引：515 个候选函数 × 每棵树几百个文件，逐个函数重扫一遍是
    30 万次 `parse`——那不是一个能在提交前跑的东西。所以反过来做：扫一遍全树，
    建一张 `名字 → 出现次数` 的表，之后每个候选函数只是一次查表。

    代价是精度：`Name` 节点分不出"这是函数引用"还是"这是一同名局部变量"，所以
    同名局部变量、同名字段也会被算成一次引用——方向是把死代码看成活的（漏报）。
    它比 v1 小得多的原因只有一个：**字符串常量与注释不在这棵树里**，而那正是 v1
    在本仓库漏得最狠的地方（注释里写函数名是这个仓库的书写习惯）。
    """
    index: Counter[str] = Counter()
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Load):
                    index[node.id] += 1
            elif isinstance(node, ast.Attribute):
                index[node.attr] += 1
            elif isinstance(node, ast.alias):
                index[node.name.split(".")[-1]] += 1
                if node.asname:
                    index[node.asname] += 1
    return index


def scan() -> list[Entry]:
    entries: list[Entry] = []
    app_index = _reference_index(APP)
    literal_index = _literal_index(APP)
    test_index = _reference_index(TESTS)
    script_index = _reference_index(SCRIPTS)
    for path in sorted(APP.rglob("*.py")):
        for name, line, tags in _public_defs(path):
            entry = Entry(name=name, path=path.relative_to(APP.parent).as_posix(),
                          line=line, tags=tags)
            # v1 口径减掉定义行自己那一次（`def name(` 里的名字）。
            entry.hits = literal_index[name] - 1
            entry.refs = app_index[name]
            entry.test_refs = test_index[name]
            entry.script_refs = script_index[name]
            entries.append(entry)
    return entries


def _load_baseline() -> dict[str, dict]:
    if not BASELINE.exists():
        return {}
    return json.loads(BASELINE.read_text(encoding="utf-8"))["entries"]


def _gate(suspects: list[Entry]) -> int:
    """棘轮闸门：只对基线之外的新增报错。返回退出码。"""
    baseline = _load_baseline()
    names = {entry.name for entry in suspects}
    problems: list[str] = []

    for entry in suspects:
        if entry.name not in baseline:
            problems.append(f"新增零引用公开函数：{entry.path}:{entry.line} {entry.name}")

    for name, item in baseline.items():
        for evidence in item.get("evidence", []):
            if not (BACKEND_DIR.parent / evidence).exists():
                problems.append(f"基线条目 {name} 的豁免理由已经不在了：{evidence}")
        if name not in names:
            print(f"（提示）基线条目 {name} 已不再是零引用，可以删掉它了")

    todo = [name for name in baseline if baseline[name].get("status") == "todo" and name in names]
    if todo:
        print(f"待判（{len(todo)} 条，它们是往年遗留、不在任何一轮的边界里）：")
        for name in todo:
            print(f"  · {name}：{baseline[name]['reason']}")
        print()

    if problems:
        print("闸门：不通过")
        for line in problems:
            print(f"  · {line}")
        print("\n每条新增都要么接上，要么在 "
              f"{BASELINE.relative_to(BACKEND_DIR).as_posix()} 里写清为什么它可以零引用。")
        return 1
    print(f"闸门：通过（{len(suspects)} 条零引用全部在基线内）")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suspects", action="store_true",
                        help="只列真正可疑的（产品代码零引用、且不是路由/装饰器/__all__）")
    parser.add_argument("--gate", action="store_true",
                        help="闸门模式：对基线之外的零引用公开函数返回非 0")
    args = parser.parse_args(argv)

    entries = scan()
    suspects = [entry for entry in entries if entry.suspect]
    orphans = [entry for entry in entries if entry.refs == 0]
    tagged = Counter(tag for entry in orphans for tag in entry.tags)
    # v1 会漏掉"只被注释提到"的那批：字面判据说 0 条、语法树也说 0 条才算可疑；
    # 反过来，字面 > 0 而语法树 = 0 的，就是 v1 看不见的缺口。
    missed_by_literal = [entry for entry in suspects if entry.hits > 0]

    print(f"顶层公开函数：{len(entries)}")
    print(f"  零引用（含路由/装饰器/__all__）：{len(orphans)}")
    for tag, count in sorted(tagged.items()):
        print(f"    其中标记为 {tag}：{count}")
    print(f"  真正可疑（零引用且无任何豁免标记）：{len(suspects)}")
    print(f"    其中 v1 字面判据看不见的（注释里提过名字）：{len(missed_by_literal)}")
    print()
    print("判据 v2：在 `backend/app/**/*.py` 的语法树里，除定义行外引用数为 0。")
    print("tests / scripts 两列是「只在用例/脚本里被用到」的引用数——它们非零而 app 为 0，")
    print("正是「用例绿、产品没接上」的形状。")
    print()

    if args.gate:
        return _gate(suspects)

    shown = suspects if args.suspects else orphans
    by_file: dict[str, list[Entry]] = {}
    for entry in shown:
        by_file.setdefault(entry.path, []).append(entry)
    for path in sorted(by_file):
        for entry in sorted(by_file[path], key=lambda item: item.line):
            marks = ",".join(entry.tags) or "-"
            print(f"{entry.path}:{entry.line}\t{entry.name}\t"
                  f"text={entry.hits}\ttests={entry.test_refs}\tscripts={entry.script_refs}"
                  f"\t{marks}")
    print(f"\n合计 {len(shown)} 条。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
