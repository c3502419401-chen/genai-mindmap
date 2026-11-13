"""`scripts/evaluate_retrieval.py`：测试集构造、命中判定、四个指标、`--check`。

这个脚本是"改检索之前先量一遍"的那把尺，所以它自己的正确性只有一种验证方式：
**手算**。下面每条度量用例都写着算式，断言的是一串具体数字，而不是"两个数差不多"。

三条边界：

  - 全部离线。检索侧只走 BM25（`conftest.py` 已清空凭证并把开关置 0），因此每次
    检索都是可复现的；脚本本身一个模型都不调。
  - 材料是现造的假 markdown，不用 `docs/` 里的真文件：真文件会随文档改动而变，
    把它们钉进断言等于给测试装一个随机的定时炸弹。
  - `--check` 的用例走完整 `main()`，断言的是**退出码**——那正是这条功能对外的
    唯一契约（手动跑、看末尾那行）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.contracts.source import Source, SourceKind
from app.ingestion.service import parse_text
from app.knowledge import library

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_retrieval.py"


@pytest.fixture(scope="module")
def module():
    """按文件路径加载脚本（`scripts/` 不是包，与 `test_openapi_snapshot.py` 同法）。

    必须先把模块登记进 `sys.modules` 再执行：脚本用了 `from __future__ import
    annotations`，`@dataclass` 要靠 `sys.modules[cls.__module__]` 才能把字符串注解
    解析回类型，不登记就报 `AttributeError: 'NoneType' object has no attribute
    '__dict__'`。
    """
    spec = importlib.util.spec_from_file_location("evaluate_retrieval", SCRIPT)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


# ---------------------------------------------------------------- 夹具


def _section(title: str, chars: int) -> str:
    """一段 markdown 章节：标题 + 空行 + 约 `chars` 个字的正文。"""
    filler = "本节交代背景与口径，涉及时间地点与参与人员。"
    body = (filler * (chars // len(filler) + 1))[:chars]
    return f"## {title}\n\n{body}\n\n"


def _material(module, text: str, *, ref: str = "fake.md"):
    source = parse_text(text, kind="markdown")
    return module.Material(ref=ref, source=source,
                           content_hash=library.content_hash(source.text or ""))


THREE_SECTIONS = (
    "# 制度汇编\n\n"
    + _section("预算审批的层级", 320)
    + _section("报销时限与票据", 260)
    + _section("采购合同的法务审核", 300)
)


# ---------------------------------------------------------------- 1. 构造


def test_eval_case_from_headings(module):
    """标题就是 query，章节区间就是 relevant，条数与可用章节数一致。"""
    material = _material(module, THREE_SECTIONS)
    # `parse_text` 会 strip 首尾空白，所以区间一律对着**存下来的正文**算
    text = material.text
    dataset = module.build_dataset([material])

    titles = ["预算审批的层级", "报销时限与票据", "采购合同的法务审核"]
    starts = [text.index(f"## {title}") for title in titles]
    assert [case.query for case in dataset.cases] == titles
    assert all(case.source_hash == material.content_hash for case in dataset.cases)
    assert [case.relevant for case in dataset.cases] == [
        [(starts[0], starts[1])],
        [(starts[1], starts[2])],
        [(starts[2], len(text))],
    ]
    # 区间必须能逐字切回原文，且落在这份材料内部
    for case in dataset.cases:
        start, end = case.relevant[0]
        assert text[start:end].startswith("## ")
        assert 0 <= start < end <= len(text)

    stats = dataset.stats[material.ref]
    assert stats["outline_entries"] == 3
    assert stats["eligible_sections"] == 3
    assert stats["cases"] == 3


def test_eval_case_capped_per_material(module):
    """每份材料最多 8 条，且取的是均匀铺开的 8 条而不是前 8 条。"""
    titles = [f"第{n}章的编号与说明" for n in range(1, 13)]
    text = "# 长文\n\n" + "".join(_section(title, 260) for title in titles)
    dataset = module.build_dataset([_material(module, text)])

    assert len(dataset.cases) == module.MAX_CASES_PER_MATERIAL
    picked = [case.query for case in dataset.cases]
    assert picked[0] == titles[0], "第一条必须是材料开头那一章（等距取样）"
    assert picked[-1] == titles[-1], "最后一条必须是材料末尾那一章"
    assert picked == sorted(picked, key=titles.index), "顺序必须是原文顺序"


# ---------------------------------------------------------------- 2. 排除规则


def test_exclusion_rules(module):
    """短标题 / 短章节 / 覆盖超 60% 的章节，三条各自生效。"""
    text = (
        "# 汇编\n\n"
        + _section("引言部", 300)              # 标题 3 字 → 排除
        + _section("预算审批的层级", 120)        # 章节 120 字 → 排除
        + _section("大章吃掉全文的六成以上", 2400)  # 覆盖 >60% → 排除
        + _section("报销时限与票据", 300)       # 保留
        + _section("采购合同的法务审核", 300)    # 保留
    )
    dataset = module.build_dataset([_material(module, text)])
    queries = [case.query for case in dataset.cases]

    assert queries == ["报销时限与票据", "采购合同的法务审核"]
    assert "引言部" not in queries
    assert "预算审批的层级" not in queries
    assert "大章吃掉全文的六成以上" not in queries
    # 三条规则各砍掉一条：5 条目录项 → 2 条可用
    assert dataset.stats["fake.md"]["outline_entries"] == 5
    assert dataset.stats["fake.md"]["eligible_sections"] == 2


def test_exclusion_thresholds_are_the_constants(module):
    """阈值就是 `MIN_TITLE_CHARS` / `MIN_SECTION_CHARS` / `MAX_SECTION_SHARE` 本身。"""
    assert (module.MIN_TITLE_CHARS, module.MIN_SECTION_CHARS) == (4, 200)
    assert module.MAX_SECTION_SHARE == 0.60
    # 恰好达到下限的标题与章节要留下（判据是 `<` 与 `>`，不是 `<=` / `>=`）
    text = ("# 汇编\n\n"
            + _section("四个字的标题", 200)
            + _section("另一章的标题也要够长", 200))
    dataset = module.build_dataset([_material(module, text)])
    assert len(dataset.cases) == 2


# ---------------------------------------------------------------- 3. 命中判定


def test_hit_judgement_three_interval_relations(module):
    """完全包含 / 部分交集 ≥20% / 交集 <20%，只有前两种算命中。"""
    relevant = [(50, 400)]

    # 命中块整段落在 relevant 里：交集 = 块长，必然命中
    assert module.hit_is_relevant((100, 200), relevant) is True
    # 交集 30/100 = 30% ≥ 20%：算命中
    assert module.hit_is_relevant((0, 100), [(70, 400)]) is True
    # 交集 15/100 = 15% < 20%：不算命中（这是重叠带来的邻块，讲的是别的事）
    assert module.hit_is_relevant((0, 100), [(85, 400)]) is False


def test_hit_judgement_boundary_and_edges(module):
    """阈值取 `>=`：恰好 20% 算命中；零宽命中和完全不相交一律不算。"""
    assert module.hit_is_relevant((0, 100), [(80, 400)]) is True      # 20/100
    assert module.hit_is_relevant((0, 100), [(80 + 1, 400)]) is False  # 19/100
    assert module.hit_is_relevant((0, 0), [(0, 400)]) is False         # 零宽
    assert module.hit_is_relevant((500, 600), [(0, 400)]) is False     # 不相交


def test_hit_judgement_accepts_multiple_relevant_spans(module):
    """relevant 多段时按并集算交集，不要求单段连续。"""
    relevant = [(0, 100), (200, 300)]
    assert module.hit_is_relevant((50, 250), relevant) is True   # 100/200 = 50%
    assert module.hit_is_relevant((100, 200), relevant) is False  # 恰好落在两段之间


# ---------------------------------------------------------------- 4. 指标手算


def test_metrics_hand_computed(module):
    """L=100、命中按 50 字切两段：逐 k 断言四个指标。

    ranked = [(1, 0, 50), (2, 50, 100), (3, 300, 400)]，relevant = [(0, 100)]

      k=1：命中(0,50)→recall 1、mrr 1/1、ndcg (50/100)/log2(2)=0.5、coverage 0.5
      k=2：第二条补上另外 50 字 → ndcg 0.5 + 0.5/log2(3)=0.8155、coverage 1.0
      k=3：第三条与 relevant 不相交 → 三个数都不动
    """
    case = module.EvalCase(query="预算审批的层级", source_hash="h", relevant=[(0, 100)])
    scores = module.evaluate_case(case, [(1, 0, 50), (2, 50, 100), (3, 300, 400)], [1, 2, 3])

    assert scores[1]["recall"] == 1.0
    assert scores[1]["mrr"] == 1.0
    assert scores[1]["ndcg"] == pytest.approx(0.5)
    assert scores[1]["coverage"] == pytest.approx(0.5)

    assert scores[2]["recall"] == 1.0
    assert scores[2]["mrr"] == 1.0
    assert scores[2]["ndcg"] == pytest.approx(0.5 + 0.5 / 1.5849625007211562)
    assert scores[2]["coverage"] == pytest.approx(1.0)

    assert scores[3] == scores[2]


def test_mrr_uses_rank_of_first_hit_and_zero_without_hit(module):
    """`mrr` 取**第一条命中**的名次；top_k 内没命中记 0，不是记 1/k。"""
    case = module.EvalCase(query="报销时限", source_hash="h", relevant=[(0, 100)])
    ranked = [(1, 500, 600), (2, 600, 700), (3, 40, 140)]
    scores = module.evaluate_case(case, ranked, [1, 3])

    assert scores[1]["mrr"] == 0.0, "k=1 里没有命中"
    assert scores[1]["recall"] == 0.0
    assert scores[3]["mrr"] == pytest.approx(1 / 3), "第一条命中在名次 3"
    assert scores[3]["recall"] == 1.0


def test_rank_gap_costs_ndcg(module):
    """名次越靠后折扣越大：同一条命中排第 1 与排第 5，ndcg 之比是 log2(6)。"""
    case = module.EvalCase(query="采购合同", source_hash="h", relevant=[(0, 100)])
    top = module.evaluate_case(case, [(1, 0, 100)], [1])
    deep = module.evaluate_case(case, [(1, 900, 1000)] + [(rank, 0, 100) for rank in (5,)],
                                [5])
    assert top[1]["ndcg"] == pytest.approx(1.0)
    assert deep[5]["ndcg"] == pytest.approx(1 / 2.584962500721156)


# ---------------------------------------------------------------- 5. coverage


def test_coverage_ratio_and_no_double_counting(module):
    """coverage 是"relevant 被覆盖了多少"的比例口径，重叠命中不重复计。"""
    case = module.EvalCase(query="跨段章节", source_hash="h",
                           relevant=[(0, 100), (200, 300)])
    ranked = [(1, 50, 250), (2, 40, 240)]
    scores = module.evaluate_case(case, ranked, [1, 2])

    # 第 1 条覆盖 50..100（50 字）+ 200..250（50 字）= 100/200
    assert scores[1]["coverage"] == pytest.approx(0.5)
    assert scores[1]["ndcg"] == pytest.approx(0.5)
    # 第 2 条与第 1 条几乎重合，只多出 40..50 这 10 字
    assert scores[2]["coverage"] == pytest.approx(0.55)
    # recall 只问"有没有命中"，两条都命中，所以一直是 1
    assert scores[1]["recall"] == scores[2]["recall"] == 1.0


def test_coverage_is_stricter_than_recall(module):
    """命中一段就记满 recall，但 coverage 只记它实际覆盖的那一部分。

    这两个口径的差异是这套评估的核心：只看 recall，会把"父块够大、沾一点就算命中"
    的退化当成进步。
    """
    case = module.EvalCase(query="大章", source_hash="h", relevant=[(0, 500)])
    shallow = module.evaluate_case(case, [(1, 0, 120)], [1])
    assert shallow[1]["recall"] == 1.0, "120/120 全在 relevant 内 → 命中"
    assert shallow[1]["coverage"] == pytest.approx(120 / 500)
    assert shallow[1]["coverage"] < 0.25


# ---------------------------------------------------------------- 6. --check


def _corpus(tmp_path: Path) -> Path:
    """一份够出 3 条 case 的假材料，落在 tmp（不进仓库）。"""
    path = tmp_path / "corpus.md"
    path.write_text(
        "# 语料\n\n" + _section("预算审批的层级", 320)
        + _section("报销时限与票据", 260) + _section("采购合同的法务审查", 300),
        encoding="utf-8")
    return path


def _run(module, *argv: str, capsys) -> tuple[int, str]:
    code = module.main(list(argv))
    return code, capsys.readouterr().out


def _baseline_file(module, tmp_path: Path, report: dict) -> Path:
    path = tmp_path / "BASELINE.md"
    path.write_text("# 基线\n\n" + module.render_markdown(report), encoding="utf-8")
    return path


def test_markdown_round_trips_into_baseline(module):
    """`--markdown` 产出的那一坨能被 `baseline_from_markdown` 读回来（--check 的入口）。"""
    report = {"schema": module.REPORT_SCHEMA, "top_k": [1, 3], "case_count": 1,
              "chance_recall@1": 0.1, "materials": [], "ladder": [],
              "configs": [{"label": "rag/hybrid=0 parent=1 lib=0", "cases": 1,
                           "metrics": {name: {"1": 1.0, "3": 1.0} for name in
                                       module.METRIC_NAMES}}]}
    text = "# 文档\n\n" + module.render_markdown(report)
    parsed = module.baseline_from_markdown(text)
    assert parsed["configs"][0]["label"] == report["configs"][0]["label"]


def test_check_passes_against_fresh_baseline(module, tmp_path, capsys):
    """拿刚跑出来的表当基线，`--check` 必须过（退出码 0）。"""
    corpus = _corpus(tmp_path)
    base_argv = ("--material", str(corpus), "--hybrid", "0",
                 "--parent", "1", "--library", "0", "--top-k", "1,3")
    report_path = tmp_path / "report.json"
    assert module.main([*base_argv, "--json", str(report_path)]) == 0
    capsys.readouterr()
    baseline = _baseline_file(module, tmp_path, json.loads(report_path.read_text("utf-8")))

    code, out = _run(module, "--material", str(corpus),
                     "--check", "--baseline", str(baseline), capsys=capsys)
    assert code == 0, out
    assert "与基线一致" in out


def test_check_fails_on_3pp_drop_and_names_the_config(module, tmp_path, capsys):
    """基线被抬高 3pp → 退出码 1，并指出是哪个配置的哪个指标掉了。"""
    corpus = _corpus(tmp_path)
    base_argv = ("--material", str(corpus), "--hybrid", "0",
                 "--parent", "1", "--library", "0", "--top-k", "1,3")
    report_path = tmp_path / "report.json"
    assert module.main([*base_argv, "--json", str(report_path)]) == 0
    report = json.loads(report_path.read_text("utf-8"))
    target = report["configs"][0]
    target["metrics"]["coverage"]["3"] = round(target["metrics"]["coverage"]["3"] + 0.03, 6)
    capsys.readouterr()
    baseline = _baseline_file(module, tmp_path, report)

    code, out = _run(module, "--material", str(corpus),
                     "--check", "--baseline", str(baseline), capsys=capsys)
    assert code == 1, out
    assert target["label"] in out
    assert "coverage@3" in out
    assert "-3.0pp" in out


def test_compare_reports_flags_missing_config(module):
    """配置从本次运行里消失也算问题：不比对就等于悄悄缩小了覆盖面。"""
    baseline = {"configs": [{"label": "rag/hybrid=0 parent=1 lib=0",
                             "metrics": {name: {"1": 1.0} for name in module.METRIC_NAMES}}]}
    problems = module.compare_reports(baseline, {"configs": []})
    assert problems and "没有这个配置" in problems[0]


# ---------------------------------------------------------------- 7. 退化输入


def test_material_without_headings_yields_no_cases(module):
    """没有章节结构的材料：0 条 case，不抛错，stats 仍然如实记账。"""
    dataset = module.build_dataset([_material(module, "只有一段正文，没有任何标题。\n")])
    assert dataset.cases == []
    assert dataset.stats["fake.md"]["outline_entries"] == 0
    assert dataset.stats["fake.md"]["cases"] == 0
    assert module.chance_recall_at_1(dataset) == 0.0


def test_single_section_material_yields_no_cases(module):
    """只有一个章节：`heading_outline` 认为那不是目录（一条覆盖全文 ≈ 书名）。"""
    dataset = module.build_dataset([_material(module, _section("唯一的一章", 400))])
    assert dataset.cases == []


def test_empty_and_blank_materials_do_not_crash(module):
    """空列表、空正文、纯空白正文都不炸；报告里的 case 数是 0。

    空正文不能走 `parse_text`（它把空串判成解析失败，`NotSupportedSource`）——
    直接造 `Source`：检索层面对的正是这种"解析过但没正文"的来源。
    """
    assert module.build_dataset([]).cases == []
    blank = module.Material(
        ref="blank.md",
        source=Source(kind=SourceKind.markdown, text="", status="parsed"),
        content_hash=library.content_hash(""),
    )
    dataset = module.build_dataset([blank])
    assert dataset.cases == []
    report = module.run_matrix(dataset, [], [1, 3])
    assert report["case_count"] == 0
    assert report["configs"] == []
    assert module.render_table(report).startswith("配置")


def test_spread_is_even_and_deterministic(module):
    """等距取样：首尾必取、数量正确、重复调用结果一致。

    `round` 是"四舍六入五成双"，所以 11/7 的倍数取整后是 `0,2,3,5,6,8,9,11`——
    重要的是间距均匀且首尾命中，不是某一种取整规则。
    """
    picked = module._spread(12, 8)
    assert picked == [0, 2, 3, 5, 6, 8, 9, 11]
    assert len(picked) == 8 and picked[0] == 0 and picked[-1] == 11
    assert picked == module._spread(12, 8), "同一输入必须给出同一批 case"
    assert module._spread(3, 8) == [0, 1, 2]
    assert module._spread(0, 8) == []
