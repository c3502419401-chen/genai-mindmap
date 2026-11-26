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
