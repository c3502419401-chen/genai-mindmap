"""跨文档测试集（`scripts/eval_cases_crossdoc.py`）与 `crossdoc_metrics`。

与 `test_retrieval_eval_manual.py` 同一套纪律：对 `docs/` 里那四份真材料跑断言（标注
钉在正文上，用假材料测等于测空壳），几何与口径用假数据测（不该依赖真文件）。

这一套要防的是**它自己**最容易出的两类偏差：

1. **悄悄退回单文档**。跨文档是这套件存在的全部理由；一条 case 只涉及一份材料，
   它就该待在 manual 套里，而不是在这里冒充桥接。
2. **重合度失守**。问法抄材料（>50%）或问得太虚（均值 <30%）都会让"图检索有没有
   用"这个结论失去前提——前者测的是原文匹配，后者测的是噪声。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.ingestion.service import parse_text
from app.knowledge import library

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture(scope="module")
def module():
    return _load("evaluate_retrieval_crossdoc_suite", SCRIPTS / "evaluate_retrieval.py")


@pytest.fixture(scope="module")
def crossdoc():
    return _load("eval_cases_crossdoc_under_test", SCRIPTS / "eval_cases_crossdoc.py")


@pytest.fixture(scope="module")
def materials(module):
    return module.load_materials(list(module.DEFAULT_MATERIALS), [])


@pytest.fixture(scope="module")
def dataset(module, materials):
    return module.build_crossdoc_dataset(materials)


# ---------------------------------------------------------------- 1. 规模与「跨」的性质


def test_case_count_is_in_the_declared_band(crossdoc):
    """10–15 条：少了解释不了任何差异，多了每条的单次检索成本会把实验拖成噪声。"""
    assert 10 <= len(crossdoc.CASES) <= 15
    assert len(crossdoc.CASES) == 14


def test_every_case_crosses_at_least_two_materials(crossdoc, dataset):
    """每条都要两份以上**不同材料**，且摊出来的用例数 = targets 数。"""
    for case in crossdoc.CASES:
        assert len(case.targets) >= 2, case.query
        assert crossdoc.is_cross_document(case), case.query
        assert len({target.material for target in case.targets}) >= 2, case.query
    assert len(dataset.cases) == sum(len(case.targets) for case in crossdoc.CASES)
    for group in dataset.crossdoc:
        assert len(group["materials"]) >= 2, group["query"]
        assert set(group["per_material"]) == set(group["materials"])


def test_every_case_carries_a_reason(crossdoc):
    """`why` 写的是「为什么必须跨文档」，不是「为什么难」。太短说明没想清楚。"""
    for case in crossdoc.CASES:
        assert case.why.strip(), case.query
        assert len(case.why) >= 20, f"理由太短，复核时没用：{case.query}"


def test_spans_are_real_ranges(dataset):
    """每条标注都落到正文上：非空、正宽、在正文内。"""
    by_hash = {material.content_hash: material.text for material in dataset.materials}
    for case in dataset.cases:
        text = by_hash[case.source_hash]
        assert case.relevant, case.query
        assert all(0 <= start < end <= len(text) for start, end in case.relevant)


# ---------------------------------------------------------------- 2. 重合度护栏


def _recomputed(module, crossdoc, dataset) -> list[float]:
    """按已解析的区间**重算**重合度（不读报告自称的数）。"""
    by_hash = {material.content_hash: material.text for material in dataset.materials}
    values = []
    for group in dataset.crossdoc:
        target = "".join(
            by_hash[fingerprint][start:end]
            for fingerprint, spans in group["per_material"].items()
            for start, end in spans)
        values.append(crossdoc.overlap_ratio(group["query"], target))
    return values


def test_overlap_mean_inside_band(module, crossdoc, dataset):
    """均值落在 30%–50%：与 manual 套同一道闸（抄的必然 >60%，虚的 <30%）。"""
    values = _recomputed(module, crossdoc, dataset)
    mean = sum(values) / len(values)
    low, high = crossdoc.OVERLAP_MEAN_RANGE
    assert low - 1e-9 <= mean <= high + 1e-9, f"均值 {mean:.1%} 不在 {low:.0%}–{high:.0%}"
    assert dataset.annotations["overlap_mean"] == pytest.approx(round(mean, 6), abs=1e-6)
    assert dataset.annotations["case_count"] == len(crossdoc.CASES)
    assert dataset.annotations["pair_count"] == len(dataset.cases)


def test_no_single_case_copies_the_material(crossdoc, dataset, module):
    """单条上限 50%：越过就是抄材料，不管它标在哪一档。"""
    values = _recomputed(module, crossdoc, dataset)
    assert max(values) <= crossdoc.MAX_OVERLAP + 1e-9, (
        f"最高那条 {max(values):.1%} 已经在抄材料："
        f"{dataset.crossdoc[values.index(max(values))]['query']}")


# ---------------------------------------------------------------- 3. 标注过期的两种报错


def _fake_material(module, ref: str):
    text = f"# 汇编\n\n## {Path(ref).stem}\n\n" + "正文" * 200
    source = parse_text(text, kind="markdown")
    return module.Material(ref=ref, source=source,
                           content_hash=library.content_hash(source.text or ""))


def test_hash_drift_is_fatal(module, crossdoc, monkeypatch):
    """正文变了 → 明确报错（不跳过）：静默跳过会让"标注失效"看起来像"没有收益"。"""
    refs = list(crossdoc.CROSSDOC_SOURCE_HASHES)
    fakes = [_fake_material(module, ref) for ref in refs]
    registered = {material.ref: material.content_hash for material in fakes}
    drifted = {**registered, refs[-1]: "0" * 64}
    monkeypatch.setattr(module.crossdoc_cases, "CROSSDOC_SOURCE_HASHES", drifted)
    with pytest.raises(SystemExit) as info:
        module.build_crossdoc_dataset(fakes)
    assert "跨文档套的标注已失效" in str(info.value)
    assert refs[-1] in str(info.value)


def test_missing_material_is_fatal(module, crossdoc):
    refs = list(crossdoc.CROSSDOC_SOURCE_HASHES)
    with pytest.raises(SystemExit) as info:
        module.build_crossdoc_dataset([_fake_material(module, refs[0])])
    assert "不在本次材料集里" in str(info.value)


# ---------------------------------------------------------------- 4. crossdoc 指标的口径


def _stub_dataset(module) -> object:
    """两份材料、三条 case 的假测试集：只用来钉三个口径的几何关系。"""
    a, b = "a" * 64, "b" * 64
    groups = [
        {"query": "只拿到甲", "materials": [a, b],
         "per_material": {a: [(100, 200)], b: [(300, 400)]}},
        {"query": "两份都拿到", "materials": [a, b],
         "per_material": {a: [(100, 200)], b: [(300, 400)]}},
        {"query": "提到两份但乙的区间不对", "materials": [a, b],
         "per_material": {a: [(100, 200)], b: [(300, 400)]}},
    ]
    return module.Dataset(cases=[], materials=[], stats={}, suite="crossdoc",
                          annotations={}, crossdoc=groups)


def test_crossdoc_metrics_separate_the_three_readings(module, monkeypatch):
    """doc_cover 只看材料、bridge 还看区间、full_bridge 要求两份都对。

    这三条读数分开报的理由：宽松口径（doc_cover）最容易虚高——命中同一份材料里的
    无关章节同样算数，而图的桥接价值恰恰在于"落在正确的位置上"。
    """
    hits = {
        "只拿到甲": [(("a" * 64), 120, 180)],
        "两份都拿到": [(("a" * 64), 120, 180), (("b" * 64), 320, 380)],
        "提到两份但乙的区间不对": [(("a" * 64), 500, 560), (("b" * 64), 320, 380)],
    }
    monkeypatch.setattr(module, "_ranked_hits",
                        lambda dataset, config, query, depth, id_map: hits[query])
    config = module.Config(source_mode="rag", hybrid=False, parent=True, library=True)
    metrics = module.crossdoc_metrics(_stub_dataset(module), config, (10,), {})
    assert metrics["10"]["doc_cover"] == pytest.approx((0.5 + 1.0 + 1.0) / 3, abs=1e-6)
    assert metrics["10"]["bridge"] == pytest.approx((0.5 + 1.0 + 0.5) / 3, abs=1e-6)
    assert metrics["10"]["full_bridge"] == pytest.approx(1 / 3, abs=1e-6)


def test_crossdoc_metrics_empty_for_other_suites(module):
    """title / manual 套没有 `crossdoc`，就不该出现这一节（报告结构保持原样）。"""
    dataset = module.Dataset(cases=[], materials=[], stats={}, suite="manual")
    config = module.Config(source_mode="rag", hybrid=False, parent=True, library=False)
    assert module.crossdoc_metrics(dataset, config, (3,), {}) == {}


# ---------------------------------------------------------------- 5. `--suite crossdoc`


BASE = ("--hybrid", "0", "--parent", "1", "--library", "1", "--top-k", "1,3")


def test_suite_crossdoc_runs_alone(module, tmp_path, capsys):
    """只跑跨文档套：报告里有 crossdoc 指标，且不混入 title / manual。"""
    target = tmp_path / "crossdoc.json"
    code = module.main(["--suite", "crossdoc", *BASE, "--json", str(target)])
    out = capsys.readouterr().out
    assert code == 0, out
    bundle = json.loads(target.read_text("utf-8"))
    assert set(bundle["suites"]) == {"crossdoc"}
    report = bundle["suites"]["crossdoc"]
    assert report["suite"] == "crossdoc"
    assert report["case_count"] == 28
    assert "crossdoc" in report["configs"][0]
    assert set(report["configs"][0]["crossdoc"]["3"]) == {"doc_cover", "bridge", "full_bridge"}
    assert "跨文档标注" in out


def test_check_refuses_crossdoc(module, capsys):
    """`--check --suite crossdoc` 直接拒：它的读数取决于图里有什么，不能当 CI 门槛。"""
    with pytest.raises(SystemExit) as info:
        module.main(["--check", "--suite", "crossdoc"])
    assert "跨机器的外部状态" in str(info.value)
    capsys.readouterr()
