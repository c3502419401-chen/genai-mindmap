"""探针脚本的卫生检查：让"脚本悄悄坏掉 / 产物悄悄落库"这件事有红灯，而不是靠人记。

## 为什么需要它

`backend/scripts/` 下这类"探针 / 临时脚本"已经三次让 `ruff check .` 红过一段时间
（`tests/test_few_branches.py` 的 F401、`scripts/_blind_pack.py:83` 的 F541、
`scripts/_probe_join_rule.py:24` 的 F401）。三次的共同点不是规则难，而是**没人跑**：
lint 不在 CI 里，本地也只有想得起来的时候才跑。

于是有两种"看似解决"的做法，都不成立：

- **探针脚本用完就删**：`docs/` 里有二十多处把它们当"复算入口"（`LABEL-JOIN-ADMISSION.md`
  引用 `_probe_join_rule.py` / `_probe_replay_collapse.py`，`ACCEPTANCE-METRICS.md` 引用
  `_structure_audit.py` / `_score_cards.py`）。删掉脚本，那些数字就变成不可复算的断言。
- **给它们配一套宽松的 ruff 规则**：宽松规则意味着它们**不再被 lint 保护**。探针脚本的
  腐化速度不比产品代码慢——它们跟着 `app/**` 的重构漂，而且没有用例钉住。

所以这里做的是把两件事分开：

| 对象 | 归属 | 保护 |
|---|---|---|
| 脚本 `scripts/**/*.py` | 留在 repo | 必须**能加载**（A）+ import 目标必须存在（B）+ 依赖在 CI 上装得出来（F） |
| 产物 `scripts/**/_*.json` | 默认不进 repo | 已跟踪的必须在 `docs/` 里被引用（D），否则它就是没有引用的裸文件 |

## 六条检查，为什么是这六条

**A · 能不能真的加载（`load`）**

对 `_*.py` / `probe_*.py` 逐个在**独立子进程**里 `runpy.run_path(..., run_name="__probe__")`
——`__name__` 不是 `"__main__"`，所以 `if __name__ == "__main__"` 那个块不执行，只验证
"这个文件的顶层能跑通、它 import 的东西都还在"。

必须开子进程，这一点是实测出来的：同一进程里顺序 `run_path` 一百多个脚本会**挂死**
（有脚本顶层就要连网络 / 等输入），而且一个 `os._exit` 会把整轮检查带走。子进程 + 20 秒
超时是唯一能收敛的形态。

**B · import 的目标还在不在（`imports`）**

`ruff` 的 pyflakes 只做**文件名**内的检查：`from app.optimization.repair import WHATEVER`
里 `WHATEVER` 不存在，它不报（它只会在你**没用**这个导入时报 F401）。探针脚本腐化的主要形态
恰好是"`app/**` 改了名，脚本还指着旧名字"，也就是 ruff 盲区。所以这里用 `ast` 取全部
`Import` / `ImportFrom` 节点，模块用 `importlib.util.find_spec` 验存在，`from app.x import y`
再用 `getattr` 验符号存在。

符号存在性只对 `app.*` 做：第三方库（`httpx`、`openai` 等）大量使用模块级 `__getattr__`
做惰性导出，静态 `getattr` 会误报，而它们不是这份检查要防的东西。

**C · docs 引用的脚本还在不在（`docs`）**

`docs/*.md` 里 `scripts/_structure_audit.py` 这类引用是"复算入口"。脚本改名或删除后，
文档里的数字就失去了复算路径，但没有任何东西会红。这条检查把引用与文件对上。

**D · 产物有没有悄悄落库（`artifacts`）**

`scripts/**/_*.json` 是 `measure_*.py` 的默认输出（`DEFAULT_OUT = "scripts/_xxx.json"`）。
它们默认属于一次性产物，`_selfcheck_once.json` / `_selfcheck_sliced.json` 是例外：
`docs/SUBAGENT-VS-SELFCHECK.md` 把它们当留档的实测结果引用。判据不写成人工名单，而是
**"已跟踪的必须被 docs 引用"**——这样任何新提交的裸产物都会红，而留档证据因为被引用而合法。

**E · 部署文档里的开关还在不在（`env`）**

`.env.example` 里的变量，在 `app/` 与 `scripts/` 里还找得到吗。方向是**单向**的：只查
"文档写了、代码里没有"。反方向（代码里有、文档没写）实测 59 条——`MINDMAP_SECTION_SLICE`
这类行为开关与实验开关大多不该交给部署的人去设；查它就要维护一份 59 条的豁免名单，
而它拦的东西（一个没人读的开关）不会让任何东西坏。单向这一侧是零噪声的：照着
`.env.example` 设了一个代码已经不读的变量，表现是"设了、什么都不会发生"——与仓库里
反复在治的"静默失败"是同一族，只是发生在部署这一层。

变量名按**字符串字面量**认（`"MINDMAP_X"`），因为仓库的主流写法是
`REMEMBER_ENV = "MINDMAP_X"` 之后 `os.getenv(REMEMBER_ENV)`；只认 `os.getenv("MINDMAP_X")`
会把 23 个声明里的 18 个判成没人读（实测过）。

**F · 这些 import 在 CI 上装得出来吗（`deps`）**

`load` 与 `imports` 都在**本机**跑，而本机不是 CI。2026-11 五连红修到最后一条时才
看清这一点：那六个 `_two_arch_*.py` 修好之后，CI 上剩下的是另外四类——

    scripts/_fix_ps1_bom.py                  ← 硬编码 D:\\genai-mindmap\\...（本机路径恰好对得上）
    scripts/acc2/probe_ui_colors.py          ← 本机装了 playwright，requirements 没声明
    scripts/asr_local_poc.py:231             ← 本机装了 sherpa_onnx（soundfile 同）
    scripts/measure_fishbone_categories.py   ← winreg，Windows 专属，CI 是 Ubuntu

所以"干净 checkout"只对齐了**树**，没对齐**机器**。这一档补机器：平台专属模块
（`_PLATFORM_ONLY`）不在 `try` 里就红；第三方模块不在 `requirements.txt` 直接声明里、
也不在 `_DEPS_ALLOWED` 白名单里、且不在 `try` 里就红。白名单只放"CI 实测可导入"的
传递依赖（PIL / pptx / torch / transformers），每条注明依据。

这条的判据是**声明**，不是"我这台机器上有没有"——后者永远为真，所以永远查不出东西。

## 跑法

    cd backend
    python scripts/check_script_hygiene.py            # 全跑，打印
    python scripts/check_script_hygiene.py --gate     # 闩门：有违规退出码 1
    python scripts/check_script_hygiene.py --only imports,docs

`--gate` 是**零容忍**，不是棘轮：这六条检查的基线当前就是全绿（46 个探针脚本全部
可加载、129 个脚本的 import 目标全部还在、1553 处 import 在 CI 上装得出来、101 处
文档引用全部落得到文件、9 个产物全部各就各位、23 个环境变量全部有人在读），所以
不存在"历史欠账要挂 baseline"。真出现需要长期豁免的东西，再加白名单并写明理由——
但默认姿态是"这条规矩现在成立，破了就红"。

`--gate` 时 `load` 档还会**自己物化一棵干净 checkout** 再加载（见
`_materialize_clean_tree`）：这条闸问的是"CI 上能不能加载"，而开发机上有
`.gitignore` 覆盖的 `backend/data/`。连红五次的直接成因就是判据取在了那棵有
`data/` 的树上。
"""
from __future__ import annotations

import argparse
import ast
import importlib
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
SCRIPTS = BACKEND_DIR / "scripts"
DOCS = REPO_DIR / "docs"

#: 单个探针脚本的加载超时。实测最慢的是 `_probe_join_rule.py`（0.5s），20 秒是
#: "它一定在干别的事"的界线，不是性能判据。
LOAD_TIMEOUT_SECONDS = 20

#: 子进程加载的代码。`run_name` 故意不是 `"__main__"`：探针脚本的 `main()` 会真的
#: 花模型额度 / 连网络，这里只验"文件本身活着"。
_LOAD_CODE = "import runpy,sys;runpy.run_path(sys.argv[1], run_name='__probe__')"

#: `docs/` 里指向**脚本**的引用。三种写法都要认：`scripts/x.py`、
#: `backend/scripts/x.py`、`python scripts/acc3/x.py`。
#:
#: 只认 `.py`，不认 `.json`：文档里的 `scripts/_xxx.json` 绝大多数是
#: `--save scripts/_xxx.json` / `DEFAULT_OUT = "scripts/_xxx.json"` 这种**生成路径**，
#: 是"跑完之后会有"，不是"现在就该在"。把它们算成引用会让这条检查专报假红。
#: 尾部 `(?![\w/-])` 用来挡 `measure_external_rank.pool.jsonl` 被当成 `.json`。
_DOC_REF = re.compile(r"(?:backend/)?scripts/[A-Za-z0-9_./-]+\.py(?![\w/-])")

#: 不算引用来源的文档：`docs/research/` 存的是**别人仓库**的 README 快照，
#: 里面的 `scripts/mindmap.py` 指的是那个项目的路径，与本仓库无关。
_NON_LOCAL_DOCS = ("docs/research/",)

#: docs 里引用但按判断不该现在存在的脚本。每条都要说清为什么。
_DOCS_REF_EXEMPT: dict[str, str] = {
    "scripts/migrate_v1_v2.py": (
        "`docs/W1_DAY1.md` 的任务表与 `docs/CONTRACT_V2.md` 的迁移章节把它写成**待建**条目"
        "（v1→v2 迁移脚本 + 单测）。计划文档指向未来文件是正常的，不是死链。"
    ),
    # `scripts/check_doc_map.py` 的豁免在 2026-11-28 删掉了。它当时的理由是"`docs/BACKLOG.md`
    # 第 10 条把它写成待做条目的判据，所以它必须点名一个还不存在的闸门"——那条理由
    # **已经失效**：闸门建起来了，第 10 条的状态也改成了已做。留着一条理由失效的豁免，
    # 等于给"它下次被删掉"发通行证，而 C 档本该在那时报红。
}


#: 产物（`_*.json`）在 docs 里的点名形式：带不带 `scripts/` 前缀都算。
_ARTIFACT_REF = re.compile(r"(?:backend/)?(?:scripts/)?(_[A-Za-z0-9_.-]+\.json)(?![\w-])")

#: `.env.example` 的变量声明。部署的人照着它设环境变量。
_ENV_EXAMPLE = BACKEND_DIR / ".env.example"
_ENV_DECL = re.compile(r"^\s*([A-Z][A-Z0-9_]+)\s*=", re.M)

#: 变量名在源码里的形态：字符串字面量。**两种写法都要认**——`os.getenv("X")`
#: 与 `REMEMBER_ENV = "X"` 之后 `os.getenv(REMEMBER_ENV)`。只认前者会把
#: `MINDMAP_LIBRARY_REMEMBER` 这类"常量集中声明"的变量判成没人读（实测：23 个声明里
#: 会误报 18 个），那种假红正是让人开始无视一条检查的方式。
_ENV_NAME = re.compile(r"""["']([A-Z][A-Z0-9_]{2,})["']""")

#: 变量名去这两棵树里找。不含 `tests/`：用例里出现一个变量名不代表产品会读它。
_ENV_SOURCES = ("app", "scripts")

#: `.env.example` 之外，第三方依赖的声明处。
_REQUIREMENTS = BACKEND_DIR / "requirements.txt"

#: **只在某个操作系统上存在的标准库模块。** 它们在开发机与 CI 之间不对称（开发机
#: Windows、CI Ubuntu），所以必须包在 `try:` 或平台判断里。判据确定性拉满：这些名字
#: 在另一个平台上必然 `ModuleNotFoundError`，而"我这里有"只是因为我在那个平台上。
_PLATFORM_ONLY: dict[str, str] = {
    "winreg": "仅 Windows", "msvcrt": "仅 Windows", "_winapi": "仅 Windows",
    "winsound": "仅 Windows", "nt": "仅 Windows",
    "termios": "仅 POSIX", "fcntl": "仅 POSIX", "pwd": "仅 POSIX",
    "grp": "仅 POSIX", "resource": "仅 POSIX", "posix": "仅 POSIX",
}

#: 第三方模块里"`requirements.txt` 没直接声明、但 CI 上确实能导入"的，白名单 + 理由。
#: 它们靠 requirements 的**传递依赖**存在，所以裸导入此刻不会红——但那是耦合，不是
#: 保证：`docling` 哪天不再依赖它们，脚本会在 CI 上红而本地照旧绿。白名单里的每一条
#: 都注明"CI 实测可导入"的依据，别往里加没验过的名字。
_DEPS_ALLOWED: dict[str, str] = {
    "PIL": "Pillow，docling 的传递依赖。2026-11-28 的 CI 日志里 `imports` 档没报它。",
    "pptx": "python-pptx，docling 的传递依赖。同上。",
    "torch": "docling-ibm-models 的传递依赖。同上。",
    "transformers": "docling 的间接依赖。同上。",
}

#: 可选依赖的**脚本内登记**：`# requires-optional: sherpa_onnx, soundfile`。
#: 放在 docstring 之后、第一个语句之前。给机器查，`docs/OPTIONAL-DEPS.md` 给人读。
_OPTIONAL_DECL = re.compile(r"^#\s*requires-optional:\s*(.+)$", re.M)

#: 可选依赖的**人类可读登记处**。脚本里声明了却不在这份文档里，等于只有本文件知道。
_OPTIONAL_DOC = DOCS / "OPTIONAL-DEPS.md"

#: CI 的 Python 版本（`.github/workflows/ci.yml` 的 `setup-python`）。本地可能是更晚的
#: 版本，所以"本地能解析"不等于"CI 能解析"：`ast.parse(feature_version=...)` 把这一维
#: 也变成确定性判据。
CI_PYTHON = (3, 12)


@dataclass
class Problem:
    check: str
    where: str
    why: str


@dataclass
class Report:
    problems: list[Problem] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    #: `--gate` 时置真：`load` 档必须在**干净 checkout** 上取结论，拿不到干净树就报红。
    #: 不加这个开关的话，单元测试里那些 `tmp_path` 假仓库（没有 git）会被连坐判红。
    require_clean: bool = False

    def add(self, check: str, where: str, why: str) -> None:
        self.problems.append(Problem(check, where, why))

    def of(self, check: str) -> list[Problem]:
        return [p for p in self.problems if p.check == check]


# --------------------------------------------------------------------------- A


def probe_scripts(scripts_dir: Path | None = None) -> list[Path]:
    """探针脚本：文件名以 `_` 或 `probe_` 开头。

    `__init__.py` 也以 `_` 开头，但它不是探针脚本，排除掉。

    `scripts_dir` 缺省是主工作树的 `scripts/`；`load` 档会传**干净树**的那一份，
    因为 CI 上被检查的就是那一份。
    """
    out = []
    for path in sorted((scripts_dir or SCRIPTS).rglob("*.py")):
        if path.name == "__init__.py":
            continue
        if path.name.startswith("_") or path.name.startswith("probe_"):
            out.append(path)
    return out


def _materialize_clean_tree() -> Path | None:
    """把 `git ls-files` 里的文件复制到临时目录，返回那个「干净 checkout」的根。

    ## 为什么 `load` 必须在这棵树上跑

    这道闸要回答的问题是**「CI 上能不能加载」**，而 CI 是干净 checkout。在开发树上
    取结论时，`.gitignore` 覆盖的东西（`backend/data/`、`backend/uploads/`、
    `backend/.env`）**磁盘上有、git 里没有**，脚本顶层读它们就得到"本地绿、干净
    checkout 红"。

    这个仓库已经因此连红五次（`7ac8400 / f8bbc0a / b540fd8 / 00a623b / 157f0b5`：
    `backend/scripts/_two_arch_*.py` 六个脚本在模块顶层 `read_text()` 一个
    `data/two_arch.json`，干净 checkout 上那个文件不存在）。五次都不是代码难改，
    是**判据取错了树**。

    修法有几种，都不成立：

    - 让人"记得先建干净 worktree 再跑闸门"——已经证明"记得"在这个仓库不管用；
    - 在 CI 之前再加一步自查——那是拿 CI 去验 CI，本地仍然什么都发现不了；
    - 让人给每个脚本"声明依赖了哪个被忽略目录"——**声明会漏**，而漏掉的那一条
      正是下一次的红。

    这里用的形态不需要任何人声明任何东西：闸门自己物化一棵干净树，`load` 只认
    那棵树上的结论。于是"本地绿"与"CI 绿"不再是两个需要对齐的判断，而是同一件事。

    拿不到 git 索引（从 tarball 解出来的副本、`tmp_path` 假仓库）时返回 `None`，
    由 `check_load` 决定是退回主树（非 gate）还是报红（gate）。
    """
    tracked = [name for name in _git("ls-files", "-z").split("\x00") if name]
    if not tracked:
        return None
    root = Path(tempfile.mkdtemp(prefix="hygiene-clean-"))
    for rel in tracked:
        src = REPO_DIR / rel
        if not src.is_file():
            # 索引里有、工作树里没有（未暂存的删除）。干净 checkout 上它是在的，
            # 但这里拿不到内容；跳过它，`load_in_clean_tree` 之外另有统计把这件事说出来。
            continue
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    _copy_untracked_scripts(root, tracked)
    return root


def _copy_untracked_scripts(clean_root: Path, tracked: list[str]) -> None:
    """把主工作树里**还没进 git** 的脚本也放进干净树。

    物化树只该有 git 里的东西，但那样会漏掉一类最该被抓的脚本：刚写完、还没
    `git add` 的探针。它在干净树里不存在，于是 `load` 根本不检查它——等它被提交、
    等 CI 跑起来才红，那就又回到"红在 CI"。所以检查对象取主树的全集，运行环境
    取干净树：新脚本照样在有 `data/` 之外的**没有** `data/` 的环境里被加载一次。
    """
    tracked_scripts = {name for name in tracked if name.startswith("backend/scripts/")}
    src_scripts = BACKEND_DIR / "scripts"
    if not src_scripts.is_dir():
        return
    for path in sorted(src_scripts.rglob("*.py")):
        rel = path.relative_to(REPO_DIR).as_posix()
        if rel in tracked_scripts:
            continue
        dst = clean_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dst)


def check_load(report: Report) -> None:
    clean = _materialize_clean_tree()
    report.stats["load_in_clean_tree"] = 0 if clean is None else 1
    if clean is None and report.require_clean:
        report.add(
            "load", "干净 checkout",
            "拿不到 git 索引（`git ls-files` 为空），无法在干净 checkout 上取 load 结论。"
            "这道闸的每一次红都是「本地绿、CI 红」，所以它只认干净树上的结论——"
            "取不到干净树时宁可红，也不要拿一份可能有 `data/` 的树去判绿。",
        )
        return
    try:
        scanned = (clean / "backend") if clean is not None else BACKEND_DIR
        paths = probe_scripts(scanned / "scripts")
        report.stats["load_checked"] = len(paths)
        for path in paths:
            rel = path.relative_to(scanned).as_posix()
            try:
                done = subprocess.run(
                    [sys.executable, "-c", _LOAD_CODE, str(path)],
                    cwd=str(scanned),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=LOAD_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                report.add("load", rel, f"加载超过 {LOAD_TIMEOUT_SECONDS}s 未结束（顶层在等网络/输入？）")
                continue
            if done.returncode != 0:
                tail = (done.stderr or "").strip().splitlines()
                why = tail[-1] if tail else f"退出码 {done.returncode}"
                # 临时目录的绝对路径对读的人没有信息量，而且每次都不一样（会让同一条
                # 红看起来像新的）。换成读者真正需要知道的那个事实。三种形态都要换：
                # traceback 里的 `Path` 是原样单反斜杠，而 `repr()` 出来的字符串是双反斜杠。
                if clean is not None:
                    for variant in (str(scanned).replace("\\", "\\\\"),
                                    str(scanned),
                                    scanned.as_posix()):
                        why = why.replace(variant, "<干净 checkout>/backend")
                report.add("load", rel, why)
    finally:
        if clean is not None:
            shutil.rmtree(clean, ignore_errors=True)


# --------------------------------------------------------------------------- B


def _iter_imports(tree: ast.AST):
    """(行号, 模块名, 符号名或 None)。

    位于 `try:` 块里的导入**不算**：那是可选依赖的标准写法
    （`measure_cross_map.py` 的 `try: from pymilvus import ... except ImportError: print("未安装")`）。
    对这种导入要求"模块必须存在"，等于要求可选依赖变成硬依赖。
    """
    optional: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for child in ast.walk(node):
                if child is not node:
                    optional.add(id(child))

    for node in ast.walk(tree):
        if id(node) in optional:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name, None
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入：在脚本里几乎不出现，且需要包上下文才能解析
                continue
            module = node.module or ""
            if not module:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                yield node.lineno, module, alias.name


def _module_exists(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _symbol_exists(module: str, symbol: str) -> bool:
    """`from module import symbol` 能不能成立。

    两种成立方式都要认：`symbol` 是 `module` 里的属性（函数/类/常量），
    或者 `symbol` 是 `module` 下的**子模块**——`from app.memory import observations`
    在 `observations` 这个子模块还没被导入时，`hasattr(app.memory, "observations")`
    是 False，但那条 import 完全合法。
    """
    if _module_exists(f"{module}.{symbol}"):
        return True
    try:
        return hasattr(importlib.import_module(module), symbol)
    except Exception:  # noqa: BLE001 — 导入失败已在模块层面报过，这里只判符号
        return True


def check_imports(report: Report) -> None:
    paths = [p for p in sorted(SCRIPTS.rglob("*.py")) if p.name != "__init__.py"]
    report.stats["imports_checked"] = len(paths)
    seen: set[tuple[str, str]] = set()
    for path in paths:
        rel = path.relative_to(BACKEND_DIR).as_posix()
        try:
            # `utf-8-sig`：`audit_self_check.py` / `measure_external_rank.py` 带 UTF-8 BOM。
            # Python 自己执行带 BOM 的源文件没问题（PEP 263 会跳过），但把字节按 `utf-8`
            # 解成 str 之后 BOM 就是一个 U+FEFF 字符，`ast.parse` 会当它非法。
            source = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            report.add("imports", rel, f"语法错误：{exc.msg}（第 {exc.lineno} 行）")
            continue
        # 本地 Python 可能比 CI 新。同一份源码在新版本上合法、在 CI 上 SyntaxError，
        # 是"本地绿而 CI 红"的又一条静默通道（PEP 701 的嵌套引号 f-string 就是这样：
        # 3.12 起合法，3.11 及以下直接 SyntaxError）。按 CI 的版本再解析一遍。
        try:
            ast.parse(source, filename=str(path), feature_version=CI_PYTHON)
        except SyntaxError as exc:
            report.add(
                "imports", f"{rel}:{exc.lineno}",
                f"这条语法 CI 的 Python {CI_PYTHON[0]}.{CI_PYTHON[1]} 不认"
                f"（{exc.msg}）。本机版本比 CI 新，所以本地能解析不代表 CI 能。",
            )
            continue
        for lineno, module, symbol in _iter_imports(tree):
            if not _module_exists(module):
                if (module, "") not in seen:
                    seen.add((module, ""))
                    report.add("imports", f"{rel}:{lineno}", f"模块不存在：{module}")
                continue
            # 符号存在性只对 app.* 做：第三方库的模块级 __getattr__ 会误报。
            if symbol and module.startswith("app."):
                if (module, symbol) in seen:
                    continue
                seen.add((module, symbol))
                if not _symbol_exists(module, symbol):
                    report.add("imports", f"{rel}:{lineno}", f"{module} 里没有 {symbol}")


# --------------------------------------------------------------------------- C


def doc_references() -> dict[str, list[str]]:
    """`scripts/....py` 的引用 → 引用它的文档（相对仓库根，正斜杠）。"""
    refs: dict[str, list[str]] = {}
    if not DOCS.is_dir():
        return refs
    for path in sorted(DOCS.rglob("*.md")):
        where = path.relative_to(REPO_DIR).as_posix()
        if where.startswith(_NON_LOCAL_DOCS):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _DOC_REF.finditer(text):
            target = match.group(0)
            if target.startswith("backend/"):
                target = target[len("backend/"):]
            refs.setdefault(target, [])
            if where not in refs[target]:
                refs[target].append(where)
    return refs


def check_docs(report: Report) -> None:
    refs = doc_references()
    report.stats["docs_refs"] = len(refs)
    for target, where in sorted(refs.items()):
        if target in _DOCS_REF_EXEMPT:
            continue
        if not (BACKEND_DIR / target).exists():
            report.add("docs", target, f"被 {', '.join(where)} 引用，文件不存在")
    report.stats["docs_exempt"] = len(_DOCS_REF_EXEMPT)


# --------------------------------------------------------------------------- D


def _git(*args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(REPO_DIR), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return done.stdout if done.returncode == 0 else ""


def artifact_mentions() -> set[str]:
    """docs 里被点名提到过的产物文件名（只取 basename）。

    `docs/SUBAGENT-VS-SELFCHECK.md` 写的是"两份实测结果留在
    `backend/scripts/_selfcheck_sliced.json` 与 `_selfcheck_once.json`"——后一个只有裸
    文件名。引用形式不统一，所以这里按 basename 匹配，宁可宽松：这条检查要拦的是
    **完全没人提过**的产物，不是格式不一致的引用。
    """
    mentioned: set[str] = set()
    if not DOCS.is_dir():
        return mentioned
    for path in sorted(DOCS.rglob("*.md")):
        where = path.relative_to(REPO_DIR).as_posix()
        if where.startswith(_NON_LOCAL_DOCS):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _ARTIFACT_REF.finditer(text):
            mentioned.add(match.group(1))
    return mentioned


def check_artifacts(report: Report) -> None:
    artifacts = sorted(SCRIPTS.rglob("_*.json"))
    report.stats["artifacts_checked"] = len(artifacts)
    if not artifacts:
        return
    if not (REPO_DIR / ".git").exists():
        report.stats["artifacts_checked"] = 0
        return  # 从 tarball 解压出来的副本没有 git，这条检查无从谈起
    tracked = {
        line.strip()
        for line in _git("ls-files", "--", "backend/scripts").splitlines()
        if line.strip()
    }
    ignored: set[str] = set()
    for path in artifacts:
        rel = path.relative_to(REPO_DIR).as_posix()
        if rel in tracked:
            continue
        # 逐个 `check-ignore -q` 而不是 `--stdin` 批量：批量形态在 Windows 上要靠
        # stdin 编码对齐，实测漏过路径（把已被忽略的报成游离）。9 个文件，不值得省。
        if subprocess.run(
            ["git", "-C", str(REPO_DIR), "check-ignore", "-q", rel],
            capture_output=True,
        ).returncode == 0:
            ignored.add(rel)

    mentioned = artifact_mentions()
    for path in artifacts:
        rel = path.relative_to(REPO_DIR).as_posix()
        in_docs = path.name in mentioned
        if rel in tracked:
            if not in_docs:
                report.add(
                    "artifacts", rel,
                    "已进版本库但 docs/ 里没有任何引用——要么补引用当留档证据，要么删掉",
                )
        elif rel not in ignored:
            report.add(
                "artifacts", rel,
                "游离产物：既没进版本库，也没被 .gitignore 覆盖（应加 backend/scripts/_*.json）",
            )


# --------------------------------------------------------------------------- E


def check_env(report: Report) -> None:
    """`.env.example` 里的变量，代码里还在读吗。

    **方向是单向的**：只查"文档写了、代码里没有"，不查"代码里有、文档没写"。

    后一个方向的实测数是 59 条（`MINDMAP_SECTION_SLICE`、`MINDMAP_KEEP_BY_SCORE`、
    `MINDMAP_NODE_POOL` 这类行为开关与实验开关，大多不该交给部署的人去设）。把它
    也查了就要维护一份 59 条的豁免名单——那正是 `find_unwired` 基线那套成本，而它
    拦的东西（一个没人读的开关）不会让任何东西坏。

    这一个方向是确定性的零噪声：`.env.example` 是给部署的人看的。里面留着一个
    **代码已经不读的变量**，意味着他照着设了、而什么都不会发生——与"设了但静默
    不生效"是同一族失败，只是发生在部署这一层。
    """
    if not _ENV_EXAMPLE.exists():
        return
    declared = _ENV_DECL.findall(_ENV_EXAMPLE.read_text(encoding="utf-8", errors="replace"))
    report.stats["env_declared"] = len(declared)
    if not declared:
        return
    mentioned: set[str] = set()
    for folder in _ENV_SOURCES:
        root = BACKEND_DIR / folder
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            mentioned.update(_ENV_NAME.findall(
                path.read_text(encoding="utf-8", errors="replace")))
    for name in declared:
        if name not in mentioned:
            report.add(
                "env", f"backend/.env.example → {name}",
                "这个变量在代码里一次都没出现过：照着设不会有任何效果",
            )


# --------------------------------------------------------------------------- F


def _declared_requirements() -> set[str]:
    """`requirements.txt` 里直接声明的包名，归一化成 import 名。

    `python-multipart` → `python_multipart`、`uvicorn[standard]` → `uvicorn`：
    版本约束与 extras 都切掉，只留 import 时会用的那一段。
    """
    if not _REQUIREMENTS.is_file():
        return set()
    out: set[str] = set()
    for line in _REQUIREMENTS.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[=<>!\[;\s]", line, maxsplit=1)[0]
        if name:
            out.add(name.lower().replace("-", "_"))
    return out


def _try_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """`try:` 块里的 (行号, 模块名)——也就是被 `_iter_imports` 放过的那一批。

    这批正是"可选依赖"的候选：包在 `try` 里意味着"没有它也要能加载"。但**加载得动
    不等于跑得动**，所以它们必须被登记（见 `check_deps` 的后三条判据）。
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for child in ast.walk(node):
            if child is node:
                continue
            if isinstance(child, ast.Import):
                found.extend((child.lineno, alias.name) for alias in child.names)
            elif isinstance(child, ast.ImportFrom) and child.module and not child.level:
                found.append((child.lineno, child.module))
    return sorted(set(found))


def _optional_registrar(module_root: str, declared: set[str],
                        local_modules: set[str]) -> str | None:
    """这个模块算不算"外部可选依赖"。是就返回理由，不是就 `None`。

    与 `check_deps` 的前两条判据同一把尺子：平台专属、或者不在 `requirements.txt`
    直接声明里的第三方模块。`app.*` 与 `scripts/` 内的本地模块不算——包在 `try` 里
    是因为循环导入或类型存在性，与"要装什么"无关。
    """
    if module_root in _PLATFORM_ONLY:
        return _PLATFORM_ONLY[module_root]
    if module_root in sys.stdlib_module_names:
        return None
    if module_root in ("app", "scripts") or module_root in local_modules:
        return None
    if module_root.lower().replace("-", "_") in declared or module_root in _DEPS_ALLOWED:
        return None
    return "不在 requirements.txt 的直接声明里"


def check_deps(report: Report) -> None:
    """`scripts/` 的 import 里，哪些在 CI 上装不出来。

    ## 为什么这条必须单独有一档

    `load` 档与 `imports` 档都在**本机**跑。本机装了 `playwright` / `soundfile` /
    `sherpa_onnx`（`pip install` 过，但不在 `requirements.txt` 里），也有 `winreg`
    （Windows 专属）。于是本地两侧全绿，而 CI 上：

        scripts/_fix_ps1_bom.py       ← FileNotFoundError: 'D:\\genai-mindmap\\check-neo4j.ps1'
        scripts/acc2/probe_ui_colors.py  ← ModuleNotFoundError: No module named 'playwright'
        scripts/asr_local_poc.py:231  ← 模块不存在：sherpa_onnx
        scripts/measure_fishbone_categories.py:57 ← 模块不存在：winreg

    那是"干净 checkout"之外的第二族差异：**干净树 ≠ 干净机器**。物化树只能对齐
    "git 里有什么"，对不齐"这台机器上装了什么、是什么操作系统"。这一档补的就是后者。

    判据两条，都是确定性的：

    - 平台专属标准库模块（`_PLATFORM_ONLY`）不在 `try` 里 → 红。它在另一个平台上
      必然不存在，没有歧义。
    - 第三方模块不在 `requirements.txt` 的直接声明里、也不在 `_DEPS_ALLOWED` 里、
      且不在 `try` 里 → 红。"本地装了"不是依据，"声明过"才是。

    ## 但 `try` 不是终点

    `try: import X except ImportError: X = None` 只让**加载**不崩，它不回答"这个脚本
    缺什么才能跑"。而这些都是被 `docs/` 当复算入口引用的脚本，引用者最需要的恰恰
    是"我该先装什么"。绿得越干净，缺的东西越看不见。

    所以再加三条，把"可选依赖"从隐含变成可查：

    - `try:` 里的外部模块必须在本文件写 `# requires-optional: X`；
    - 那个名字必须在 `docs/OPTIONAL-DEPS.md` 里出现过（给人读的登记处）；
    - `# requires-optional:` 里的名字必须真的在 `try` 里 import 过（守过期声明——
      它比没有更糟，会让人以为那个依赖还在用）。
    """
    declared = _declared_requirements()
    report.stats["deps_declared"] = len(declared)
    local_modules = {path.stem for path in SCRIPTS.rglob("*.py")}
    optional_doc = (
        _OPTIONAL_DOC.read_text(encoding="utf-8", errors="replace")
        if _OPTIONAL_DOC.is_file() else ""
    )
    if not optional_doc:
        report.add(
            "deps", "docs/OPTIONAL-DEPS.md",
            "这份文档不在。可选依赖没有人类可读的登记处——`# requires-optional:` 里的"
            "名字就只剩脚本自己知道，而复算的人要的正是那一份清单。",
        )
    checked = 0
    registered = 0
    for path in sorted(SCRIPTS.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        rel = path.relative_to(BACKEND_DIR).as_posix()
        text = path.read_text(encoding="utf-8-sig")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue  # `imports` 档已经报过语法错误
        for lineno, module, _symbol in _iter_imports(tree):
            root = module.split(".")[0]
            checked += 1
            reason = _optional_registrar(root, declared, local_modules)
            if reason is None:
                continue
            if root in _PLATFORM_ONLY:
                report.add(
                    "deps", f"{rel}:{lineno}",
                    f"{module} 只在 {_PLATFORM_ONLY[root]} 上存在。包进 "
                    f"`try: import ... except ImportError:` 并给出替代行为（返回空值），"
                    f"否则 CI 上这一档必红。",
                )
            else:
                report.add(
                    "deps", f"{rel}:{lineno}",
                    f"{module} {reason}，也没包在 try/except 里——本地装了不代表 CI "
                    f"装了。要么声明依赖，要么当可选依赖处理（`try` + "
                    f"`# requires-optional:`）。",
                )

        optional_roots = {module.split(".")[0] for _line, module in _try_imports(tree)}
        required = {root for root in optional_roots
                    if _optional_registrar(root, declared, local_modules)}
        written = {
            name.strip()
            for line in _OPTIONAL_DECL.findall(text)
            for name in line.split(",")
            if name.strip()
        }
        registered += len(written)
        for root in sorted(required - written):
            report.add(
                "deps", rel,
                f"`try:` 里 import 了 {root}，本文件却没有 `# requires-optional: {root}`。"
                f"包在 try 里只让加载不崩；「它缺什么才能跑」必须写下来。",
            )
        for name in sorted(written):
            if name not in optional_roots:
                report.add(
                    "deps", rel,
                    f"`# requires-optional: {name}` 是过期声明——本文件没有任何 "
                    f"`try: import {name}`。过期声明比没有更糟：它会让下一个人以为"
                    f"那个依赖还在用。",
                )
            if optional_doc and f"`{name}`" not in optional_doc:
                report.add(
                    "deps", rel,
                    f"`# requires-optional: {name}` 没登记进 docs/OPTIONAL-DEPS.md"
                    f"——只有本文件知道的依赖不算登记。",
                )
    report.stats["deps_checked"] = checked
    report.stats["deps_optional"] = registered


# --------------------------------------------------------------------------- main

CHECKS = {
    "load": check_load,
    "imports": check_imports,
    "deps": check_deps,
    "docs": check_docs,
    "artifacts": check_artifacts,
    "env": check_env,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="探针脚本卫生检查")
    parser.add_argument("--gate", action="store_true", help="有违规时退出码 1")
    parser.add_argument(
        "--only", default="",
        help=f"只跑指定检查，逗号分隔（{','.join(CHECKS)}）",
    )
    args = parser.parse_args(argv)

    sys.path.insert(0, str(BACKEND_DIR))
    selected = [name for name in args.only.split(",") if name] or list(CHECKS)
    unknown = [name for name in selected if name not in CHECKS]
    if unknown:
        print(f"未知检查：{', '.join(unknown)}（可选：{', '.join(CHECKS)}）")
        return 2

    report = Report()
    # `--gate` 只认干净 checkout 上的结论，见 `_materialize_clean_tree` 的长注释。
    # 非 gate（人想看一眼）时允许在当前树上跑，但下面会把"跑在哪"打出来。
    report.require_clean = args.gate
    for name in selected:
        CHECKS[name](report)

    for name in selected:
        found = report.of(name)
        mark = "✓" if not found else "✗"
        print(f"{mark} {name:<9} {len(found)} 条")
        for problem in found:
            print(f"    {problem.where}  ← {problem.why}")

    print()
    if "load" in selected:
        where = ("干净 checkout（`git ls-files` 物化到临时目录）"
                 if report.stats.get("load_in_clean_tree")
                 else "**当前工作树**——它可能有 `data/` 之类被忽略的目录，结论偏乐观")
        print(f"load 档跑在：{where}")
    print("检查量：" + "、".join(f"{k}={v}" for k, v in sorted(report.stats.items())))
    if not report.problems:
        print("全部通过。")
        return 0
    print(f"共 {len(report.problems)} 条违规。")
    return 1 if args.gate else 0


if __name__ == "__main__":
    raise SystemExit(main())
