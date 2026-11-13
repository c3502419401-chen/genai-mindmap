"""手写测试集（`scripts/eval_cases_manual.py`）与 `--suite` 三档分派。

与 `test_retrieval_eval.py` 的纪律**相反**：这里对 `docs/` 里那四份真材料跑断言。
理由是手写 case 的标注（章节前缀、锚点、`source_hash`）就钉在这四份正文上，用假材料
测等于测个空壳——真正会失效的东西（材料改了、章节改名了、锚点不再唯一）一个都测不到。

代价是"改 docs 会把这几条打红"，但那正是要的：标注过期必须有人回来看一眼，不能让
它静默滑过。打红时脚本给的报错已经写明该改哪里（重读材料 → 复核区间 → 更新指纹）。

`_manual_spans` 自己的边界（锚点重复、锚点越界、粒度与锚点不匹配）用假材料测，那些
是纯几何逻辑，不该依赖真文件。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

from app.ingestion.service import parse_text
from app.knowledge import library

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT = SCRIPTS / "evaluate_retrieval.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture(scope="module")
def module():
    """`evaluate_retrieval`（按路径加载：`scripts/` 不是包）。"""
    return _load("evaluate_retrieval_manual_suite", SCRIPT)


@pytest.fixture(scope="module")
def manual():
    """`eval_cases_manual` 本身：case 数据与区间常量。"""
    return _load("eval_cases_manual_under_test", SCRIPTS / "eval_cases_manual.py")


@pytest.fixture(scope="module")
def materials(module):
    return module.load_materials(list(module.DEFAULT_MATERIALS), [])


@pytest.fixture(scope="module")
def dataset(module, materials):
    return module.build_manual_dataset(materials)


# ---------------------------------------------------------------- 1. 规模与构成


def test_manual_case_count_and_split(dataset, manual):
    """28 条，三档 8/10/10，三种粒度 21/5/2，每条都解出了区间。"""
    assert len(manual.CASES) == 28
    assert len(dataset.cases) == len(manual.CASES)
    assert Counter(case.distance for case in manual.CASES) == {"near": 8, "mid": 10, "far": 10}
    assert Counter(case.granularity for case in manual.CASES) == {
        "chapter": 21, "passage": 5, "cross": 2}
    # 每条 case 都真的落到了原文上（非空、正宽、在正文内）
    by_hash = {material.content_hash: material.text for material in dataset.materials}
    for case in dataset.cases:
        text = by_hash[case.source_hash]
        assert case.relevant, case.query
        assert all(0 <= start < end <= len(text) for start, end in case.relevant)


def test_every_manual_case_carries_a_reason(manual):
    """每条都写了"为什么算远处提问"——那一栏是给人工复核用的，不能空。"""
    for case in manual.CASES:
        assert case.why.strip(), case.query
        assert len(case.why) >= 20, f"理由太短，复核时没用：{case.query}"
        assert case.material in manual.MANUAL_SOURCE_HASHES, case.query


# ---------------------------------------------------------------- 2. 重合度护栏


def _recomputed_overlaps(module, manual, dataset) -> list[float]:
    """按 `dataset` 里已解析好的区间**重新算**一遍重合度（不读报告自称的数）。"""
    by_hash = {material.content_hash: material.text for material in dataset.materials}
    values = []
    for case in dataset.cases:
        text = by_hash[case.source_hash]
        target = "".join(text[start:end] for start, end in case.relevant)
        values.append(manual.overlap_ratio(case.query, target))
    return values


def test_overlap_per_case_is_inside_its_band(module, manual, dataset, materials):
    """每条的词面重合落在自己那一档的区间里。

    这一条是"防后人往里塞抄来的问法"的闸：抄材料的问法重合度必然 60% 以上，撞上限；
    写得太虚（纯噪声）会撞近/中档的下限。上限对**每一档**都有，下限只对近/中——远档
    的定义就是"几乎无字面重合"，给它设 30% 的下限等于逼人往里塞材料用词。
    """
    values = _recomputed_overlaps(module, manual, dataset)
    assert len(values) == len(dataset.cases)
    for case, ratio in zip(dataset.cases, values):
        declared = next(item for item in manual.CASES if item.query == case.query)
        low, high = manual.DISTANCE_BANDS[declared.distance]
        assert low - 1e-9 <= ratio <= high + 1e-9, (
            f"{declared.distance} 档的重合度 {ratio:.1%} 越界（{low:.0%}–{high:.0%}）："
            f"{case.query}")
    # 任何一条超过 50% 都算在抄材料，不管标在哪一档
    assert max(values) <= 0.50, f"最高的那条 {max(values):.1%} 已经在抄了"


def test_overlap_mean_inside_declared_range(module, manual, dataset):
    """全体均值落在 30%–50%：与文档里记的真实口语问法（33.7%）同量级。"""
    values = _recomputed_overlaps(module, manual, dataset)
    mean = sum(values) / len(values)
    low, high = manual.OVERLAP_MEAN_RANGE
    assert low - 1e-9 <= mean <= high + 1e-9, f"均值 {mean:.1%} 不在 {low:.0%}–{high:.0%}"

    annotations = dataset.annotations
    assert annotations["overlap_mean"] == pytest.approx(round(mean, 6), abs=1e-6)
    assert set(annotations["overlap_by_distance"]) == set(manual.DISTANCES)
    for name in manual.DISTANCES:
        block = annotations["overlap_by_distance"][name]
        assert block["cases"] == sum(1 for c in manual.CASES if c.distance == name)
        assert block["min"] <= block["mean"] <= block["max"]


def test_title_suite_overlap_stays_at_one(module, materials):
    """对照：标题当 query 的 word_overlap 恒为 1.0——那是这套手写 case 要校准的偏差。"""
    dataset = module.build_dataset(materials)
    by_hash = {material.content_hash: material.text for material in dataset.materials}
    values = [
        module.wording_overlap(case.query, "".join(
            by_hash[case.source_hash][start:end] for start, end in case.relevant))
        for case in dataset.cases
    ]
    assert values and all(value == 1.0 for value in values)


# ---------------------------------------------------------------- 3. 指纹与标注失效


def test_manual_source_hashes_match_the_real_materials(dataset, manual):
    """四份材料的指纹与登记值一致。

    这条红掉只有一种含义：`docs/` 里那四份正文被改了，手写标注需要复核。改法写在
    `MANUAL_SOURCE_HASHES` 的注释与脚本报错里。
    """
    actual = {material.ref: material.content_hash for material in dataset.materials}
    for ref, expected in manual.MANUAL_SOURCE_HASHES.items():
        assert ref in actual, f"材料集里没有 {ref}"
        assert actual[ref] == expected, (
            f"{ref} 的正文变了（登记 {expected[:12]}… 实际 {actual[ref][:12]}…）："
            "请重读该材料、复核 relevant 区间与问法，再把新指纹写回 "
            "scripts/eval_cases_manual.py")


def _fake_material(module, ref: str, *, body: str = "正文" * 200):
    text = f"# 汇编\n\n## 预算审批的层级\n\n{body}\n"
    source = parse_text(text, kind="markdown")
    return module.Material(ref=ref, source=source,
                           content_hash=library.content_hash(source.text or ""))


def test_hash_drift_is_fatal_not_skipped(module, manual, monkeypatch):
    """材料正文变了 → 明确报错（不跳过）。

    跳过是最坏的选项：指标会掉 10pp 而输出里只有一行提示，事后看起来像"检索退步"。
    """
    refs = list(manual.MANUAL_SOURCE_HASHES)
    fakes = [_fake_material(module, ref) for ref in refs]
    registered = {material.ref: material.content_hash for material in fakes}
    drifted = {**registered, refs[-1]: "0" * 64}
    monkeypatch.setattr(module.manual_cases, "MANUAL_SOURCE_HASHES", drifted)

    with pytest.raises(SystemExit) as info:
        module.build_manual_dataset(fakes)
    message = str(info.value)
    assert "材料正文变了" in message
    assert "不跳过" in message
    assert refs[-1] in message


def test_missing_material_is_fatal_too(module, manual, monkeypatch):
    """材料集里没有那四份 md（换了 `--material`）→ 报错，并说清缺哪一份。"""
    refs = list(manual.MANUAL_SOURCE_HASHES)
    fake = _fake_material(module, refs[0])
    with pytest.raises(SystemExit) as info:
        module.build_manual_dataset([fake])
    message = str(info.value)
    assert "不在本次材料集里" in message
    assert refs[1] in message


def test_manual_materials_present_helper(module, manual, materials):
    """降级判据：默认四份材料 → True；换一份别的 → False。"""
    assert module.manual_materials_present(materials) is True
    assert module.manual_materials_present(materials[:2]) is False


# ---------------------------------------------------------------- 4. 区间解析的边界


def _entries(module, text: str):
    source = parse_text(text, kind="markdown")
    material = module.Material(ref="fake.md", source=source,
                               content_hash=library.content_hash(source.text or ""))
    return material, module.heading_outline([source], max_items=10 ** 6)


BODY = "这一段交代口径与边界，涉及时间地点与参与人员，重复两遍也不影响判定。" * 12


def test_focus_must_be_unique(module, manual):
    """锚点在全文出现两次 → 报错（猜一个会让 relevant 悄悄标错地方）。"""
    text = (f"# 汇编\n\n## 预算审批的层级\n\n锚点句。{BODY}锚点句。{BODY}\n"
            f"## 报销时限与票据\n\n{BODY}\n")
    material, entries = _entries(module, text)
    case = manual.ManualCase(
        query="锚点在哪", material="fake.md", sections=("预算审批",),
        distance="near", granularity="passage", why="测边界", focus="锚点句。")
    with pytest.raises(SystemExit) as info:
        module._manual_spans(case, material, entries)
    assert "命中 2 次" in str(info.value)


def test_focus_must_land_inside_the_section(module, manual):
    """锚点落在别的章里 → 报错（章节前缀与锚点必须指同一处）。"""
    text = (f"# 汇编\n\n## 预算审批的层级\n\n{BODY}\n"
            f"## 报销时限与票据\n\n另一章的锚点。{BODY}\n")
    material, entries = _entries(module, text)
    case = manual.ManualCase(
        query="锚点在哪", material="fake.md", sections=("预算审批",),
        distance="near", granularity="passage", why="测边界", focus="另一章的锚点。")
    with pytest.raises(SystemExit) as info:
        module._manual_spans(case, material, entries)
    assert "不在目标章节内" in str(info.value)


def test_focus_too_close_to_section_end(module, manual):
    """锚点之后的正文不够 `FOCUS_CHARS` → 报错，而不是截一段短的当标注。"""
    text = (f"# 汇编\n\n## 预算审批的层级\n\n{BODY}\n短尾巴锚点。\n"
            f"## 报销时限与票据\n\n{BODY}\n")
    material, entries = _entries(module, text)
    case = manual.ManualCase(
        query="锚点在哪", material="fake.md", sections=("预算审批",),
        distance="near", granularity="passage", why="测边界", focus="短尾巴锚点。")
    with pytest.raises(SystemExit) as info:
        module._manual_spans(case, material, entries)
    assert "越界" in str(info.value)


def test_granularity_must_match_annotation(module, manual):
    """粒度与标注自洽：chapter 不该有锚点、passage 必须有、cross 至少两章。"""
    text = f"# 汇编\n\n## 预算审批的层级\n\n{BODY}\n## 报销时限与票据\n\n{BODY}\n"
    material, entries = _entries(module, text)

    chapter_with_focus = manual.ManualCase(
        query="q", material="fake.md", sections=("预算审批",), distance="near",
        granularity="chapter", why="测边界", focus="预算审批")
    passage_without_focus = manual.ManualCase(
        query="q", material="fake.md", sections=("预算审批",), distance="near",
        granularity="passage", why="测边界")
    cross_with_one = manual.ManualCase(
        query="q", material="fake.md", sections=("预算审批",), distance="near",
        granularity="cross", why="测边界")
    for case, expected in ((chapter_with_focus, "chapter 粒度不该有 focus"),
                           (passage_without_focus, "passage 粒度必须给 focus"),
                           (cross_with_one, "cross 粒度至少要两章")):
        with pytest.raises(SystemExit) as info:
            module._manual_spans(case, material, entries)
        assert expected in str(info.value)


def test_unknown_section_prefix_is_fatal(module, manual):
    """章节前缀一个都匹配不上 → 报错，并说清楚是哪一条 case。"""
    text = f"# 汇编\n\n## 预算审批的层级\n\n{BODY}\n## 报销时限与票据\n\n{BODY}\n"
    material, entries = _entries(module, text)
    case = manual.ManualCase(
        query="问一个不存在的章节", material="fake.md", sections=("采购合同",),
        distance="near", granularity="chapter", why="测边界")
    with pytest.raises(SystemExit) as info:
        module._manual_spans(case, material, entries)
    assert "命中 0 个" in str(info.value)
    assert "问一个不存在的章节" in str(info.value)


def test_cross_case_keeps_two_spans(module, manual):
    """跨章 case 的 relevant 是两段（不是把它们之间的一切都算进去）。"""
    text = f"# 汇编\n\n## 预算审批的层级\n\n{BODY}\n## 报销时限与票据\n\n{BODY}\n"\
           f"## 采购合同的法务审查\n\n{BODY}\n"
    material, entries = _entries(module, text)
    case = manual.ManualCase(
        query="跨两章的问题", material="fake.md", sections=("预算审批", "采购合同"),
        distance="far", granularity="cross", why="测边界")
    spans = module._manual_spans(case, material, entries)
    assert len(spans) == 2
    assert spans[0][0] < spans[0][1] < spans[1][0] < spans[1][1]


# ---------------------------------------------------------------- 5. `--suite` 三档


BASE = ("--hybrid", "0", "--parent", "1", "--library", "0", "--top-k", "1,3")


def _run(module, *argv: str, capsys) -> tuple[int, str]:
    code = module.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_suite_title_runs_only_title(module, capsys):
    code, out = _run(module, "--suite", "title", *BASE, capsys=capsys)
    assert code == 0, out
    assert "（title 套）" in out
    assert "（manual 套）" not in out


def test_suite_manual_runs_only_manual(module, capsys):
    code, out = _run(module, "--suite", "manual", *BASE, capsys=capsys)
    assert code == 0, out
    assert "（manual 套）" in out
    assert "（title 套）" not in out
    assert "手写标注" in out


def test_suite_both_keeps_the_two_suites_apart(module, tmp_path, capsys):
    """默认 both：两套并列、各自算，28 与 29 条不混在一起。"""
    target = tmp_path / "bundle.json"
    code, out = _run(module, "--suite", "both", *BASE, "--json", str(target), capsys=capsys)
    assert code == 0, out
    bundle = json.loads(target.read_text("utf-8"))
    assert bundle["schema"] == module.REPORT_SCHEMA == "retrieval-eval/v2"
    assert set(bundle["suites"]) == {"title", "manual"}
    assert bundle["suites"]["title"]["case_count"] == 29
    assert bundle["suites"]["manual"]["case_count"] == 28
    assert bundle["case_count"] == 57
    for name, report in bundle["suites"].items():
        assert report["suite"] == name
        assert report["configs"][0]["cases"] in (28, 29)
    # 两套难度不同：manual 的 R@1 明显低于 title（这正是分开算的理由）
    title_r1 = bundle["suites"]["title"]["configs"][0]["metrics"]["recall"]["1"]
    manual_r1 = bundle["suites"]["manual"]["configs"][0]["metrics"]["recall"]["1"]
    assert manual_r1 < title_r1


def test_manual_suite_refuses_custom_materials(module, tmp_path, capsys):
    """`--suite manual` 配自定义材料 → 报错（而不是给一份空结果）。"""
    corpus = tmp_path / "corpus.md"
    corpus.write_text("# 语料\n\n## 预算审批的层级\n\n" + BODY + "\n", encoding="utf-8")
    with pytest.raises(SystemExit) as info:
        module.main(["--suite", "manual", "--material", str(corpus), *BASE])
    assert "docs/ 下那四份 md" in str(info.value)


def test_both_degrades_to_title_on_custom_materials(module, tmp_path, capsys):
    """默认 both 配自定义材料 → 只跑 title，并在 stderr 明说 manual 被跳过。"""
    corpus = tmp_path / "corpus.md"
    corpus.write_text(
        "# 语料\n\n" + "".join(
            f"## {title}\n\n{BODY}\n" for title in
            ("预算审批的层级", "报销时限与票据", "采购合同的法务审查")),
        encoding="utf-8")
    code, out = _run(module, "--material", str(corpus), *BASE, capsys=capsys)
    assert code == 0, out
    assert "（title 套）" in out
    assert "manual 套跳过" in out


# ---------------------------------------------------------------- 6. `--check` 只比 title


def _bundle_report(suite: str, cases: int, recall: float) -> dict:
    return {
        "suite": suite,
        "top_k": [1, 3],
        "case_count": cases,
        "chance_recall@1": 0.1,
        "materials": [],
        "configs": [{
            "label": "rag/hybrid=0 parent=1 lib=0",
            "source_mode": "rag", "hybrid": False, "parent": True, "library": False,
            "cases": cases,
            "metrics": {name: {"1": recall, "3": recall} for name in
                        ("recall", "mrr", "ndcg", "coverage")},
        }],
    }


def test_baseline_from_bundle_picks_title(module, tmp_path):
    """v2 外层载荷 → `--check` 拿到的是 title 那套，不是最后写的 manual 那套。"""
    bundle = {"schema": module.REPORT_SCHEMA, "top_k": [1, 3],
              "suites": {"title": _bundle_report("title", 29, 1.0),
                         "manual": _bundle_report("manual", 28, 0.1)}}
    text = "# 基线\n\n" + module.render_bundle_markdown(bundle)
    parsed = module.baseline_from_markdown(text)
    assert parsed["suite"] == "title"
    assert parsed["configs"][0]["cases"] == 29


def test_baseline_without_title_is_error(module):
    """只有 manual 的基线不能当回归基线：`--check` 无从比。"""
    text = ("# 基线\n\n```json\n"
            + json.dumps({"schema": "retrieval-eval/v2",
                          "suites": {"manual": {"configs": []}}})
            + "\n```\n")
    with pytest.raises(SystemExit) as info:
        module.baseline_from_markdown(text)
    assert "没有 title 套" in str(info.value)


def test_check_ignores_manual_numbers(module, tmp_path, capsys):
    """把基线里 manual 的数字全部改成 0，`--check` 仍然过——它只比 title。"""
    target = tmp_path / "bundle.json"
    assert module.main(["--suite", "both", *BASE, "--json", str(target)]) == 0
    capsys.readouterr()
    bundle = json.loads(target.read_text("utf-8"))
    for row in bundle["suites"]["manual"]["configs"]:
        for metric in module.METRIC_NAMES:
            for key in row["metrics"][metric]:
                row["metrics"][metric][key] = 0.0
    baseline = tmp_path / "BASELINE.md"
    baseline.write_text("# 基线\n\n" + module.render_bundle_markdown(bundle), encoding="utf-8")

    code, out = _run(module, "--check", "--baseline", str(baseline), capsys=capsys)
    assert code == 0, out
    assert "与基线一致" in out
    assert "只比 title 套" in out


def test_check_refuses_manual_suite(module, capsys):
    """`--check --suite manual` 直接拒：manual 的波动不该当 CI 门槛。"""
    with pytest.raises(SystemExit) as info:
        module.main(["--check", "--suite", "manual"])
    assert "--check 只比 title 套" in str(info.value)
    capsys.readouterr()
