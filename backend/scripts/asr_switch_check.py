"""本地 ASR 接线的可执行落点检查。

**这个脚本不改任何生产代码**，只回答一个问题：如果把
`backend/scripts/asr_local_poc.py` 接进应用，实际要改哪几处、它们现在长什么样。
一份纯文字的"下一步建议"过三个月就找不到落点了；把这些落点写成一个**会跑、
会失败**的检查，下一个人 5 分钟就能接上。

用法：

    python backend/scripts/asr_switch_check.py
    python backend/scripts/asr_switch_check.py --json   # 给 CI / 脚本消费

退出码：0 = 三处落点全部就位；1 = 有落点漂移（函数改名、被删、结构变了）。

判据是一个"探针列表"：每项给出**文件路径 + 必须存在的锚点**。锚点不是关键词
模糊匹配，而是 `ast` 里真实的 `FunctionDef` / `Assign` 名字或逐字源码片段——
这样重构时函数改名会立刻红，而不是静默通过。

为什么落在 `backend/scripts/` 而不是 `backend/tests/`：它不是项目的测试断言，
是调研交付物的一部分（`docs/RESEARCH-ASR-LOCAL.md` 第 8 节指向它）。
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 本脚本位于 backend/scripts/，应用代码在同级 app/ 下。用相对位置定位，
# 不硬编码盘符——CI 在 Linux 上也要能跑。
_BACKEND = Path(__file__).resolve().parent.parent
_APP = _BACKEND / "app"


@dataclass
class Landing:
    """一个接线落点：文件 + 必须存在的锚点 + 接线时该改什么。"""

    key: str
    title: str
    path: Path
    # 锚点种类：("def", 名字) 必须有同名函数/方法；("assign", 名字) 必须有同名模块级赋值；
    # ("import", 名字) 必须 import 了这个名字；("text", 片段) 源码里必须含这段逐字文本。
    anchors: list[tuple[str, str]]
    todo: str
    checks: list[str] = field(default_factory=list)


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None


def _top_level_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """返回 (模块级函数名, 模块级整名称)。方法不算——落点都在模块层。"""
    funcs: set[str] = set()
    assigns: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigns.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigns.add(node.target.id)
    return funcs, assigns


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


def check_landing(landing: Landing) -> dict:
    """检查一个落点，返回可直接打印的结构。"""
    result = {
        "key": landing.key,
        "title": landing.title,
        "file": str(landing.path.relative_to(_BACKEND.parent)),
        "exists": landing.path.is_file(),
        "missing_anchors": [],
        "todo": landing.todo,
        "ok": True,
    }
    if not landing.path.is_file():
        result["ok"] = False
        result["missing_anchors"] = [f"{kind}:{value}" for kind, value in landing.anchors]
        return result

    tree = _parse(landing.path)
    if tree is None:
        result["ok"] = False
        result["missing_anchors"] = ["<文件无法解析为 Python>"]
        return result

    funcs, assigns = _top_level_names(tree)
    imports = _imported_names(tree)
    source = landing.path.read_text(encoding="utf-8")

    missing: list[str] = []
    for kind, value in landing.anchors:
        if kind == "def":
            found = value in funcs
        elif kind == "assign":
            found = value in assigns
        elif kind == "import":
            found = value in imports
        elif kind == "text":
            found = value in source
        else:  # pragma: no cover - 内部编程错误
            raise ValueError(f"未知锚点类型：{kind}")
        if not found:
            missing.append(f"{kind}:{value}")

    result["missing_anchors"] = missing
    result["ok"] = not missing
    return result


def _landings() -> list[Landing]:
    """三处落点。每条都对应一份真实代码，路径与锚点都核对过。"""
    return [
        Landing(
            key="call-site",
            title="1. 调用点：audio 分支把 transcribe_audio 换成「本地优先、云端兜底」",
            path=_APP / "ingestion" / "service.py",
            anchors=[
                ("def", "_parse_unstamped"),
                ("def", "parse_bytes"),
                ("import", "transcribe_audio"),
                # 现在 audio 分支的唯一调用点，接线时在这里分叉。
                ("text", "if kind is SourceKind.audio:"),
            ],
            todo=(
                "在 `_parse_unstamped` 的 `if kind is SourceKind.audio:` 分支里加开关判断："
                "本地开 → 调 asr_local_poc 的 Engine/子进程；否则走现有 `transcribe_audio`。"
                "注意 `_parse_unstamped` 是四个分支共用的唯一出口，"
                "别在 `parse_bytes` 里再套一层（指纹只在 parse_bytes 盖）。"
            ),
        ),
        Landing(
            key="switch",
            title="2. 开关：MINDMAP_ASR_LOCAL 该在哪读",
            path=_APP / "ingestion" / "asr.py",
            anchors=[
                ("def", "transcribe_audio"),
                ("def", "_api_key"),
                ("assign", "MAX_WORKERS"),
                ("assign", "MAX_SLICE_SECONDS"),
                # 现有凭证预检，是读取环境变量的既有风格参照。
                ("text", 'os.getenv("DASHSCOPE_API_KEY"'),
            ],
            todo=(
                "在本文件加 `def _local_enabled() -> bool`，默认 **关**"
                "（`os.getenv(\"MINDMAP_ASR_LOCAL\", \"0\")`，与 `MINDMAP_PARENT_RETRIEVAL` "
                "等现有开关同一套 truthy 语义）。默认关是硬要求：没装 sherpa-onnx 的"
                "线上 venv 一旦默认开就会在导入期炸。"
                "注意 `MAX_WORKERS = 2` 是云端双声道并发用的；本地路径必须**不**复用这个"
                "并发度——2C2G 上同时两个 433MB 子进程会 OOM。"
            ),
        ),
        Landing(
            key="mutex",
            title="3. 互斥锁：音频转写与 Docling 解析必须共用一把锁",
            path=_APP / "ingestion" / "service.py",
            anchors=[
                ("assign", "_parse_lock"),
                ("assign", "_converter_lock"),
                ("def", "_parse_office_source"),
                ("text", "with _parse_lock:"),
            ],
            todo=(
                "`_parse_lock` 现在是 Docling 的解析串行化锁，注释写明原因是"
                "\"DocumentConverter 是进程内单例\"。音频转写要和它**共用同一把锁**，"
                "不是新加一把：Docling 版面模型峰值 1.6–2.8GB，加 ASR 的 433MB，"
                "2C2G 上并发必定 OOM。共用锁的代价是解析排队——这是可接受的。"
            ),
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="检查把本地 ASR 接进应用需要改的三处落点是否仍然存在")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args(argv)

    results = [check_landing(landing) for landing in _landings()]
    ok = all(r["ok"] for r in results)

    if args.json:
        print(json.dumps({"ok": ok, "landings": results}, ensure_ascii=False, indent=1))
        return 0 if ok else 1

    print("本地 ASR 接线落点检查")
    print("=" * 72)
    for r in results:
        mark = "OK  " if r["ok"] else "DRIFT"
        print(f"\n[{mark}] {r['title']}")
        print(f"        文件： {r['file']}" + ("" if r["exists"] else "  ← 不存在！"))
        if r["missing_anchors"]:
            print(f"        缺失锚点： {', '.join(r['missing_anchors'])}")
        for line in r["todo"].split("。"):
            if line.strip():
                print(f"        · {line.strip()}。")

    print("\n" + "=" * 72)
    if ok:
        print("三处落点全部就位。接线从上面三个阶段依次下手。")
        print("顺序建议：先加开关（默认关）→ 再改调用点 → 最后接锁，")
        print("每一步单独提交，任何一步出问题都能只回滚一步。")
    else:
        print("有落点漂移（文件不存在 / 函数改名 / 结构变了）。")
        print("请先按当前代码重新定位，不要照搬本文的锚点——")
        print("这正是把这份检查做成可执行的原因。")
    print("\n注：本检查只读文件，不改任何生产代码。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
