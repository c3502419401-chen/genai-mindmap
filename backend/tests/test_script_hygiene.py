"""探针脚本卫生检查（`scripts/check_script_hygiene.py`）自身的用例。

两件事都要证明：

1. **真仓库当前是绿的**——这条用例就是"本地也会红"的入口。规矩不进用例，
   就还是会变成"记得的时候才跑"。
2. **每条检查真的会红**——一个从不失败的检查等于没有检查。所以下面给每一档都
   造了一个违规样本，钉住"它确实抓得住"。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

import check_script_hygiene as hygiene  # noqa: E402


def _fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把检查指向一个假仓库（scripts/ 与 docs/ 都在 tmp 下）。"""
    scripts = tmp_path / "backend" / "scripts"
    docs = tmp_path / "docs"
    scripts.mkdir(parents=True)
    docs.mkdir(parents=True)
    monkeypatch.setattr(hygiene, "BACKEND_DIR", tmp_path / "backend")
    monkeypatch.setattr(hygiene, "REPO_DIR", tmp_path)
    monkeypatch.setattr(hygiene, "SCRIPTS", scripts)
    monkeypatch.setattr(hygiene, "DOCS", docs)
    return tmp_path


# ------------------------------------------------------------------ 真仓库是绿的


def test_the_real_repository_passes_every_check():
    """四档全绿。破了这条，就该先修仓库而不是先改这条用例。"""
    assert hygiene.main(["--gate"]) == 0


# ------------------------------------------------------------------ 每档都会红


def test_load_check_catches_a_probe_script_that_cannot_be_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探针脚本 import 了一个不存在的模块——`runpy` 会抛 ModuleNotFoundError。"""
    _fake_repo(tmp_path, monkeypatch)
    (tmp_path / "backend" / "scripts" / "_probe_broken.py").write_text(
        "import module_that_does_not_exist_zzz\n", encoding="utf-8",
    )
    report = hygiene.Report()
    hygiene.check_load(report)
    found = report.of("load")
    assert len(found) == 1
    assert "module_that_does_not_exist_zzz" in found[0].why


def test_load_check_only_watches_probe_scripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`measure_*.py` 不归 load 管——它们顶层就要连库/读数据，加载不等于能跑。"""
    _fake_repo(tmp_path, monkeypatch)
    (tmp_path / "backend" / "scripts" / "measure_broken.py").write_text(
        "import module_that_does_not_exist_zzz\n", encoding="utf-8",
    )
    report = hygiene.Report()
    hygiene.check_load(report)
    assert report.of("load") == []
    assert report.stats["load_checked"] == 0


# ------------------------------------------------- load 必须在干净 checkout 上取结论


def test_load_runs_in_the_clean_tree_not_the_working_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工作树里有 `data/`、干净树里没有——同一份脚本两侧结论必须不同。

    这就是这条闸在 2026-11 连红五次的那个形态：`_two_arch_*.py` 六个脚本在模块顶层
    读 `backend/data/two_arch.json`，本地磁盘上有那个文件（`.gitignore` 不删本地文件），
    CI 的干净 checkout 上没有。判据取错树 → 本地五次全绿、CI 五次全红。
    """
    _fake_repo(tmp_path, monkeypatch)
    body = (
        "import json\n"
        "from pathlib import Path\n"
        "D = Path(__file__).resolve().parents[1] / 'data'\n"
        "json.loads((D / 'x.json').read_text(encoding='utf-8'))\n"
    )
    dirty = tmp_path / "backend" / "scripts" / "_reads_data.py"
    dirty.write_text(body, encoding="utf-8")
    (tmp_path / "backend" / "data").mkdir()
    (tmp_path / "backend" / "data" / "x.json").write_text("{}", encoding="utf-8")

    # 干净树：同一份脚本，但没有 `data/`。
    clean = tmp_path / "clean"
    (clean / "backend" / "scripts").mkdir(parents=True)
    shutil.copyfile(dirty, clean / "backend" / "scripts" / "_reads_data.py")
    monkeypatch.setattr(hygiene, "_materialize_clean_tree", lambda: clean)

    report = hygiene.Report()
    hygiene.check_load(report)
    found = report.of("load")
    assert len(found) == 1, "干净树上没有 data/，这个脚本必须加载失败"
    assert found[0].where == "scripts/_reads_data.py"
    # 临时目录的绝对路径要换成这句，否则同一条红每次长得都不一样。
    assert "<干净 checkout>/backend" in found[0].why
    assert str(clean) not in found[0].why


def test_gate_refuses_to_pass_when_it_cannot_get_a_clean_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--gate` 拿不到干净树时报红，而不是退回工作树判绿。

    退回工作树就是又一个"本地绿"：它会在有 `data/` 的树上说"全部通过"。宁可红在
    本地一个人手上，也不要红在 CI 上五次。
    """
    _fake_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(hygiene, "_materialize_clean_tree", lambda: None)
    report = hygiene.Report(require_clean=True)
    hygiene.check_load(report)
    found = report.of("load")
    assert len(found) == 1
    assert found[0].where == "干净 checkout"
    assert report.stats["load_in_clean_tree"] == 0


def test_untracked_probe_scripts_still_get_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """还没 `git add` 的新脚本也要进干净树被加载一次。

    只物化 `git ls-files` 会漏掉"刚写完、还没提交"的那一类——而那正是最容易带上
    `data/` 依赖的脚本。检查对象取工作树的全集，运行环境取干净树。
    """
    _fake_repo(tmp_path, monkeypatch)
    scripts = tmp_path / "backend" / "scripts"
    (scripts / "_tracked.py").write_text("x = 1\n", encoding="utf-8")
    (scripts / "_brand_new.py").write_text("y = 2\n", encoding="utf-8")
    monkeypatch.setattr(hygiene, "_git", lambda *args: "backend/scripts/_tracked.py\x00")

    clean = hygiene._materialize_clean_tree()
    assert clean is not None
    try:
        assert (clean / "backend" / "scripts" / "_tracked.py").is_file()
        assert (clean / "backend" / "scripts" / "_brand_new.py").is_file()
    finally:
        shutil.rmtree(clean, ignore_errors=True)


def test_import_check_catches_a_symbol_that_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`from app.x import 名字` 里名字不存在。

    这正是 `ruff` 的盲区：pyflakes 只在这个导入**没被用**时报 F401，不会去
    `app/x.py` 里核对名字在不在。探针脚本跟着 `app/**` 重构漂，坏的就是这一层。
    """
    _fake_repo(tmp_path, monkeypatch)
    (tmp_path / "backend" / "scripts" / "measure_moved.py").write_text(
        "from app.optimization.repair import NO_SUCH_CONSTANT_ZZZ\n", encoding="utf-8",
    )
    report = hygiene.Report()
    hygiene.check_imports(report)
    found = report.of("imports")
    assert len(found) == 1
    assert "NO_SUCH_CONSTANT_ZZZ" in found[0].why


def test_import_check_ignores_optional_dependencies_in_try(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`try: import 可选依赖 / except ImportError:` 不算违规。

    `measure_cross_map.py` 就这么写 `pymilvus`（19 个传递依赖、75 MB，故意不进
    requirements）。把这种写法判红，等于要求所有可选依赖都变成硬依赖。
    """
    _fake_repo(tmp_path, monkeypatch)
    (tmp_path / "backend" / "scripts" / "measure_optional.py").write_text(
        "try:\n"
        "    import module_that_does_not_exist_zzz\n"
        "except ImportError:\n"
        "    module_that_does_not_exist_zzz = None\n",
        encoding="utf-8",
    )
    report = hygiene.Report()
    hygiene.check_imports(report)
    assert report.of("imports") == []


def test_import_check_reads_utf8_bom_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """带 BOM 的脚本不该被判语法错误。

    `scripts/audit_self_check.py` 与 `scripts/measure_external_rank.py` 都带 UTF-8 BOM；
    Python 执行它们没问题（PEP 263 跳过 BOM），但按 `utf-8` 解成 str 后 BOM 会变成
    一个 U+FEFF 字符，`ast.parse` 会认为它非法。
    """
    _fake_repo(tmp_path, monkeypatch)
    (tmp_path / "backend" / "scripts" / "measure_bom.py").write_bytes(
        b"\xef\xbb\xbfimport os\n",
    )
    report = hygiene.Report()
    hygiene.check_imports(report)
    assert report.of("imports") == []


def test_docs_check_catches_a_reference_to_a_deleted_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文档把脚本当"复算入口"引用，脚本没了就成死链。"""
    _fake_repo(tmp_path, monkeypatch)
    (tmp_path / "docs" / "SOMETHING.md").write_text(
        "跑法：`python scripts/_structure_audit_gone.py`\n", encoding="utf-8",
    )
    report = hygiene.Report()
    hygiene.check_docs(report)
    found = report.of("docs")
    assert len(found) == 1
    assert "_structure_audit_gone.py" in found[0].where


def test_docs_check_does_not_read_other_projects_readmes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`docs/research/` 是别人仓库的 README 快照，里面的 `scripts/x.py` 不是本仓库的。"""
    _fake_repo(tmp_path, monkeypatch)
    research = tmp_path / "docs" / "research" / "readmes"
    research.mkdir(parents=True)
    (research / "other__project.md").write_text(
        "见 `scripts/mindmap.py`\n", encoding="utf-8",
    )
    report = hygiene.Report()
    hygiene.check_docs(report)
    assert report.of("docs") == []
    assert report.stats["docs_refs"] == 0


def test_artifact_check_catches_an_untracked_artifact_without_a_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未跟踪、又没被 .gitignore 覆盖的产物 = 游离产物（`git status` 里永远挂在那儿）。"""
    if shutil.which("git") is None:
        pytest.skip("没有 git")
    _fake_repo(tmp_path, monkeypatch)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("", encoding="utf-8")
    (tmp_path / "backend" / "scripts" / "_orphan.json").write_text("{}", encoding="utf-8")

    report = hygiene.Report()
    hygiene.check_artifacts(report)
    found = report.of("artifacts")
    assert len(found) == 1
    assert "_orphan.json" in found[0].where
    assert "游离产物" in found[0].why


def test_artifact_check_accepts_an_ignored_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """被 .gitignore 覆盖的产物是合法的一次性产物。"""
    if shutil.which("git") is None:
        pytest.skip("没有 git")
    _fake_repo(tmp_path, monkeypatch)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("backend/scripts/_*.json\n", encoding="utf-8")
    (tmp_path / "backend" / "scripts" / "_output.json").write_text("{}", encoding="utf-8")

    report = hygiene.Report()
    hygiene.check_artifacts(report)
    assert report.of("artifacts") == []


def test_artifact_check_catches_a_committed_artifact_nobody_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已进版本库的产物必须在 docs 里被引用，否则它就是没有引用的裸文件。"""
    if shutil.which("git") is None:
        pytest.skip("没有 git")
    _fake_repo(tmp_path, monkeypatch)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "backend" / "scripts" / "_kept.json").write_text("{}", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
         "add", "-f", "backend/scripts/_kept.json"],
        check=True,
    )

    report = hygiene.Report()
    hygiene.check_artifacts(report)
    found = report.of("artifacts")
    assert len(found) == 1
    assert "docs/ 里没有任何引用" in found[0].why

    # 补上引用之后就合法了（`_selfcheck_once.json` 走的就是这条路）。
    (tmp_path / "docs" / "NOTE.md").write_text(
        "结果留在 `_kept.json`。\n", encoding="utf-8",
    )
    report = hygiene.Report()
    hygiene.check_artifacts(report)
    assert report.of("artifacts") == []


def test_env_check_catches_a_documented_variable_nobody_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`.env.example` 里留着一个代码已经不读的变量 = 部署的人照设了、什么都不会发生。

    这条与"设了但静默不生效"是同一族失败，只是发生在部署那一层。方向是**单向**的：
    只查"文档写了、代码里没有"；反方向实测 59 条（行为开关与实验开关大多不该交给
    部署的人去设），要查就得维护一份 59 条豁免名单，成本远高于收益。
    """
    _fake_repo(tmp_path, monkeypatch)
    (tmp_path / "backend" / "app").mkdir()
    (tmp_path / "backend" / ".env.example").write_text(
        "MINDMAP_STILL_READ=1\nMINDMAP_GONE=0\n", encoding="utf-8",
    )
    monkeypatch.setattr(
        hygiene, "_ENV_EXAMPLE", tmp_path / "backend" / ".env.example")
    # 变量名以**字符串字面量**出现即算在用：`REMEMBER_ENV = "X"` 之后
    # `os.getenv(REMEMBER_ENV)` 是仓库里的主流写法，只认 `os.getenv("X")` 会误报。
    (tmp_path / "backend" / "app" / "settings.py").write_text(
        'STILL_READ_ENV = "MINDMAP_STILL_READ"\n', encoding="utf-8",
    )

    report = hygiene.Report()
    hygiene.check_env(report)
    found = report.of("env")
    assert len(found) == 1
    assert "MINDMAP_GONE" in found[0].where
    assert "照着设不会有任何效果" in found[0].why


def test_env_check_is_quiet_when_the_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 `.env.example` 的副本（解压出来的、裁剪过的）不该让检查崩。"""
    _fake_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        hygiene, "_ENV_EXAMPLE", tmp_path / "backend" / ".env.example")
    report = hygiene.Report()
    hygiene.check_env(report)
    assert report.of("env") == []


# ------------------------------------------------ deps：这些 import 在 CI 上装得出来吗


def test_deps_check_catches_a_platform_only_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`import winreg` 裸写在顶层：Windows 上绿，CI（Ubuntu）上加载就崩。

    这是 2026-11 五连红修到最后一条时剩下的那族：`load` 与 `imports` 都在本机跑，
    而本机是 Windows。判据不含"我这台机器上有没有"——那个永远为真。
    """
    _fake_repo(tmp_path, monkeypatch)
    (tmp_path / "backend" / "scripts" / "probe_registry.py").write_text(
        "import winreg\n", encoding="utf-8",
    )
    report = hygiene.Report()
    hygiene.check_deps(report)
    found = report.of("deps")
    assert len(found) == 1
    assert "winreg" in found[0].why
    assert "仅 Windows" in found[0].why


def test_deps_check_catches_a_package_requirements_does_not_declare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`playwright` 本机装了、`requirements.txt` 没声明：CI 上是 ModuleNotFoundError。"""
    _fake_repo(tmp_path, monkeypatch)
    (tmp_path / "backend" / "requirements.txt").write_text(
        "fastapi==0.115.0\nhttpx>=0.27\n", encoding="utf-8",
    )
    monkeypatch.setattr(
        hygiene, "_REQUIREMENTS", tmp_path / "backend" / "requirements.txt")
    (tmp_path / "backend" / "scripts" / "probe_ui.py").write_text(
        "from playwright.sync_api import sync_playwright\n", encoding="utf-8",
    )
    report = hygiene.Report()
    hygiene.check_deps(report)
    found = report.of("deps")
    assert len(found) == 1
    assert "playwright" in found[0].why
    assert "本地装了不代表 CI 装了" in found[0].why


def test_deps_check_accepts_optional_dependencies_in_try(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """包在 `try: import ... except ImportError:` 里，并且登记了，才算过。

    那正是这次修 `playwright` / `soundfile` / `sherpa_onnx` / `winreg` 用的形态：
    缺依赖时由 `main()` 打印明确消息并退出，而不是让模块加载就崩。**但 `try` 不是
    终点**——登记行与 `docs/OPTIONAL-DEPS.md` 是另外两件（见下面三条用例）。
    """
    _fake_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        hygiene, "_REQUIREMENTS", tmp_path / "backend" / "requirements.txt")
    (tmp_path / "backend" / "scripts" / "probe_optional.py").write_text(
        "# requires-optional: winreg, playwright\n"
        "try:\n"
        "    import winreg\n"
        "except ImportError:\n"
        "    winreg = None\n"
        "try:\n"
        "    from playwright.sync_api import sync_playwright\n"
        "except ImportError:\n"
        "    sync_playwright = None\n",
        encoding="utf-8",
    )
    report = hygiene.Report()
    hygiene.check_deps(report)
    assert report.of("deps") == []


def test_deps_requires_try_imports_to_be_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`try` 里 import 了却没写 `# requires-optional:`：`try` 会变成藏依赖的口袋。

    闸门绿了，而"这个脚本缺什么才能跑"这个事实消失了——绿得越干净，缺的东西越
    看不见。这是"包在 try 里就放过"这个口子的代价。
    """
    _fake_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        hygiene, "_REQUIREMENTS", tmp_path / "backend" / "requirements.txt")
    (tmp_path / "backend" / "scripts" / "probe_unregistered.py").write_text(
        "try:\n"
        "    from playwright.sync_api import sync_playwright\n"
        "except ImportError:\n"
        "    sync_playwright = None\n",
        encoding="utf-8",
    )
    report = hygiene.Report()
    hygiene.check_deps(report)
    found = report.of("deps")
    assert len(found) == 1
    assert "# requires-optional: playwright" in found[0].why


def test_deps_rejects_a_stale_optional_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """登记了 `sherpa_onnx`，代码里却没有 `try: import sherpa_onnx`。

    过期声明比没有更糟：下一个人会以为那个依赖还在用。
    """
    _fake_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        hygiene, "_REQUIREMENTS", tmp_path / "backend" / "requirements.txt")
    (tmp_path / "backend" / "scripts" / "probe_stale.py").write_text(
        "# requires-optional: sherpa_onnx\nx = 1\n", encoding="utf-8",
    )
    report = hygiene.Report()
    hygiene.check_deps(report)
    found = report.of("deps")
    assert len(found) == 1
    assert "过期声明" in found[0].why


def test_deps_requires_the_optional_doc_to_list_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """脚本里登记了，`docs/OPTIONAL-DEPS.md` 里没有：只有本文件知道的依赖不算登记。

    复算入口的引用者最需要的正是那一份"我该先装什么"的清单。
    """
    _fake_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        hygiene, "_REQUIREMENTS", tmp_path / "backend" / "requirements.txt")
    doc = tmp_path / "docs" / "OPTIONAL-DEPS.md"
    doc.write_text("# 可选依赖\n\n这里一条都没写。\n", encoding="utf-8")
    monkeypatch.setattr(hygiene, "_OPTIONAL_DOC", doc)
    (tmp_path / "backend" / "scripts" / "probe_undocumented.py").write_text(
        "# requires-optional: playwright\n"
        "try:\n"
        "    from playwright.sync_api import sync_playwright\n"
        "except ImportError:\n"
        "    sync_playwright = None\n",
        encoding="utf-8",
    )
    report = hygiene.Report()
    hygiene.check_deps(report)
    found = report.of("deps")
    assert len(found) == 1
    assert "OPTIONAL-DEPS.md" in found[0].why


# ------------------------------------------ imports：本机能解析 ≠ CI 能解析


def test_imports_check_rejects_syntax_newer_than_ci_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本机 Python 比 CI 新时，新版本语法会在本地通过、在 CI 上 SyntaxError。

    判据用 `ast.parse(feature_version=CI_PYTHON)`，所以不依赖本机装的是哪个版本：
    这里把 `CI_PYTHON` 调成 (3, 8)，`match`（3.10 起才有）就必须红。
    """
    _fake_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(hygiene, "CI_PYTHON", (3, 8))
    (tmp_path / "backend" / "scripts" / "measure_new_syntax.py").write_text(
        "match 1:\n    case 1:\n        pass\n", encoding="utf-8",
    )
    report = hygiene.Report()
    hygiene.check_imports(report)
    found = report.of("imports")
    assert len(found) == 1
    assert "CI 的 Python 3.8 不认" in found[0].why
