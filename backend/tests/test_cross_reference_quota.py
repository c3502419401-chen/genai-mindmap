"""`scripts/measure_cross_reference_quota.py` 的读数是"8 够不够"那个判定的唯一依据。

为什么这条要写成用例：那个脚本自己实现了一遍"跨支同核 + 有没有指认"的遍历，而
`optimization/service.py` 里已经有同一套判据（`_cross_branch_unlinked_diagnostics`）。
两份实现写在两处是本仓库反复踩过的坑（`docs/CROSS-BRANCH-DUPLICATES.md` §十：逐字
判据曾分居两处，后果是同一个缺陷在两个时刻有两种答案）。

所以第一条用例把两边**钉成相等的数**：脚本报的"未指认对数"必须等于诊断报的处数。
另外三条钉采样口径本身：顶满判定、指纹覆盖提示词正文、对照臂同时改常量与正文里的数。

末端那句"只抬代码上限而提示词还说 8，量到的只是代码那一半"是这条臂的全部价值——
没有它，对照臂的读数会是一个假负数。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

from app.agent import tools as tools_module  # noqa: E402
from app.contracts.mindmap import MindMapContent  # noqa: E402
from app.optimization.service import check  # noqa: E402
from measure_cross_reference_quota import (  # noqa: E402
    apply_limit_override,
    core_pairs,
    measure,
    rules_fingerprint,
)


def _leaf(node_id: str, label: str) -> dict:
    return {"id": node_id, "label": label, "children": []}


def _node(node_id: str, label: str, children: list[dict]) -> dict:
    return {"id": node_id, "label": label, "children": children}


def _rels(*pairs: tuple[str, str]) -> list[dict]:
    return [{"id": f"rel_{index}", "source": left, "target": right,
             "kind": "references", "why": "两处说的是同一件事", "provenance": "ai"}
            for index, (left, right) in enumerate(pairs, start=1)]


def _map(root: dict, relations: list[dict] | None = None) -> MindMapContent:
    payload: dict = {"title": "图", "root": root}
    if relations is not None:
        payload["relations"] = relations
    return MindMapContent.model_validate(payload)


def _unlinked_from_diagnostic(content: MindMapContent) -> int:
    """诊断 `message` 里的处数（"有 N 处同一件事写在了两条分支下"）。"""
    found = [item for item in check(content) if item.code == "CROSS_BRANCH_UNLINKED"]
    if not found:
        return 0
    match = re.search(r"有 (\d+) 处", found[0].message)
    assert match, found[0].message
    return int(match.group(1))


def _three_pairs(*declared: tuple[str, str]) -> MindMapContent:
    """三对跨支同核（剥掉序号与数量后同句），其中 `declared` 里的对已写关系。"""
    return _map(
        _node("r", "操作系统", [
            _node("a", "课程内容与模块", [
                _leaf("a1", "第一部分 概述（4 学时）"),
                _leaf("a2", "第二部分 进程与线程（8 学时）"),
                _leaf("a3", "第三部分 调度与并发（6 学时）"),
            ]),
            _node("b", "六部分学时分配", [
                _leaf("b1", "概述 4 学时"),
                _leaf("b2", "进程与线程 8 学时"),
                _leaf("b3", "调度与并发 6 学时"),
            ]),
        ]),
        _rels(*declared),
    )


# ---------------------------------------------------------------- 1. 与诊断同数


def test_the_script_unlinked_count_equals_the_diagnostic():
    """脚本的"未指认对数"与 `CROSS_BRANCH_UNLINKED` 的处数**必须相等**。"""
    content = _three_pairs(("a1", "b1"))
    rows = core_pairs(content)
    assert len(rows) == 3
    assert sum(1 for row in rows if row["declared"]) == 1
    assert len([row for row in rows if not row["declared"]]) \
        == _unlinked_from_diagnostic(content)


def test_the_denominator_counts_pairs_that_are_already_linked():
    """分母不是"未指认数"：已指认的对也要留在里面，否则读不出"挤掉了几条"。"""
    without = measure(_three_pairs(), limit=8)
    with_one = measure(_three_pairs(("a1", "b1")), limit=8)
    assert without["pairs_total"] == with_one["pairs_total"] == 3
    assert without["unlinked"] == 3
    assert with_one["unlinked"] == 2


def test_same_parent_and_verbatim_pairs_stay_out_of_the_denominator():
    """同父的一对、逐字同名的一对都由别的码管，不进这个分母。"""
    content = _map(_node("r", "根", [
        _node("a", "实验安排", [_leaf("a1", "内核模块编写 4 次"), _leaf("a2", "页面置换模拟 1 次")]),
        _node("b", "实验考核", [_leaf("b1", "内核模块编写4次")]),
    ]))
    assert core_pairs(content) == []


# ---------------------------------------------------------------- 2. 顶满与上限


def test_full_is_judged_against_the_limit_it_was_measured_with():
    """顶满是"收下的条数到没到这一次的上限"，不是一个写死的 8。"""
    content = _three_pairs(("a1", "b1"))
    assert measure(content, limit=1)["full"] is True
    assert measure(content, limit=2)["full"] is False


# ---------------------------------------------------------------- 3. 指纹与对照臂


def test_the_fingerprint_covers_the_prompt_body(monkeypatch):
    """指纹按**提示词正文**算：正文改一个字，指纹就要变（时间戳不会）。"""
    before = rules_fingerprint()["rules_sha"]
    monkeypatch.setattr(tools_module, "CROSS_REFERENCE_RULES",
                        tools_module.CROSS_REFERENCE_RULES + "\n")
    assert rules_fingerprint()["rules_sha"] != before


def test_the_override_moves_both_the_constant_and_the_number_in_the_prompt(monkeypatch):
    """对照臂必须同时改两处：常量与正文里的数。

    只改常量时模型照旧写上限条（正文还写着"最多 N 条"），量到的就只是代码那一半，
    结论会反过来——"放宽上限没用"是假的。
    """
    # 两条都登记给 monkeypatch：`apply_limit_override` 改的是模块属性，用例跑完要还原。
    monkeypatch.setattr(tools_module, "CROSS_REFERENCE_RULES",
                        tools_module.CROSS_REFERENCE_RULES)
    monkeypatch.setattr(tools_module, "CROSS_REFERENCE_MAX",
                        tools_module.CROSS_REFERENCE_MAX)
    old = tools_module.CROSS_REFERENCE_MAX
    assert f"最多 {old} 条" in tools_module.CROSS_REFERENCE_RULES

    apply_limit_override(old + 6)

    assert tools_module.CROSS_REFERENCE_MAX == old + 6
    assert f"最多 {old + 6} 条" in tools_module.CROSS_REFERENCE_RULES
    assert f"最多 {old} 条" not in tools_module.CROSS_REFERENCE_RULES
    # "超了该干什么"那一句里的数也要跟着走，否则超出上限的那些没有动作指令
    assert f"加第{old + 7}条线" in tools_module.CROSS_REFERENCE_RULES


def test_the_override_never_lowers_the_limit(monkeypatch):
    """比现状小的数不生效：这条臂只用来量"放宽会不会多写"。"""
    monkeypatch.setattr(tools_module, "CROSS_REFERENCE_RULES",
                        tools_module.CROSS_REFERENCE_RULES)
    monkeypatch.setattr(tools_module, "CROSS_REFERENCE_MAX",
                        tools_module.CROSS_REFERENCE_MAX)
    old = tools_module.CROSS_REFERENCE_MAX
    apply_limit_override(old - 2)
    assert tools_module.CROSS_REFERENCE_MAX == old
