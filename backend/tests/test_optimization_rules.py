"""F15 的检测规则与建议映射（`optimization/service.py`）。

三层各自钉住的东西：

  1. **规则层**（`check()`）：每条判据的边界——什么时候该报、什么时候**故意不报**。
     不报的那些用例不是凑数：近义检测最容易的失败方式是"平行命名被当成重复"
     （「监督学习」/「无监督学习」），那会让用户点一次就丢半条分支。
  2. **建议层**（`suggest()`）：每个诊断码都能落到一个**动作**上，或者有一份
     写下来的"为什么不转"。`DEPTH_LIMIT` 是后者的样本。
  3. **模型层**（`_model_near_pairs`）：一次调用、可关、绝不抛异常，且它的答案
     要过"同一同级组"这道校验。

真模型的调用不打桩的只有一条 live 用例（`test_live_*`），没有
`MINDMAP_LLM_API_KEY` 时跳过——离线跑全量套件不该产生网络请求。
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from app.contracts.diagnostic import DiagnosticLevel, SuggestionKind
from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source, SourceKind
from app.optimization import service
from app.optimization.service import (
    NOT_SUGGESTED,
    _SUGGESTION_OF_CODE,
    _near_reason,
    _not_converted_reason,
    check,
    suggest,
)


def _map(root: dict, *, title: str = "图") -> MindMapContent:
    return MindMapContent.model_validate({"title": title, "root": root})


def _leaf(node_id: str, label: str, **extra) -> dict:
    return {"id": node_id, "label": label, "children": [], **extra}


def _node(node_id: str, label: str, children: list[dict], **extra) -> dict:
    return {"id": node_id, "label": label, "children": children, **extra}


def _codes(diagnostics) -> list[str]:
    return [item.code for item in diagnostics]


def _of_code(diagnostics, code: str):
    return [item for item in diagnostics if item.code == code]


def _kinds(suggestions) -> dict[str | None, SuggestionKind]:
    return {item.node_id: item.kind for item in suggestions}


def _chain(depth: int) -> MindMapContent:
    """一条 `depth` 层的链（root 算第 1 层）。契约上限正好 8 层。"""
    node = _leaf(f"n{depth}", f"第{depth}层")
    for level in range(depth - 1, 0, -1):
        node = _node(f"n{level}", f"第{level}层", [node])
    return _map(node)


# ---------------------------------------------------------------- 1. 建议映射


def test_depth_limit_is_reported_but_never_turned_into_a_suggestion():
    """`DEPTH_LIMIT` 只有诊断，没有建议——"已到 8 层"不是一个可执行的动作。

    这条用例钉两件事：诊断还在（生成收尾看它），建议里没有它。
    """
    content = _chain(8)

    diagnostics = check(content)
    depth = _of_code(diagnostics, "DEPTH_LIMIT")
    assert [item.node_id for item in depth] == ["n8"]
    assert depth[0].level is DiagnosticLevel.warn

    assert "DEPTH_LIMIT" not in _SUGGESTION_OF_CODE
    assert "8 层" in NOT_SUGGESTED["DEPTH_LIMIT"]
    # 8 层那条链上每层只有一个子节点 → 建议全是 expand，没有一条来自 DEPTH_LIMIT
    suggestions = suggest(content)
    assert suggestions and all(item.kind is SuggestionKind.expand for item in suggestions)


def test_single_child_becomes_an_expand_suggestion():
    """深处的那条单子节点 → `expand`：缺的是内容，不是层级归并。"""
    content = _map(_node("r", "根", [
        _node("b", "分支", [_node("p", "要点", [_leaf("q", "细节")])]),
        _node("c", "另一支", [_leaf("c1", "甲"), _leaf("c2", "乙"), _leaf("c3", "丙")]),
    ]))

    assert _kinds(suggest(content))["p"] is SuggestionKind.expand


def _closure_codes() -> set[str]:
    """这一版的检查器能产出的**全部**诊断码：几张小图各跑一遍取并集。

    加一条新规则时，这里要多一个夹具——那正是这条用例的作用：新码不许悬空。
    """
    contents = [
        # 形态（单子节点 / 一级分支只有一个叶子）+ 同级重复 + 缺失环节
        _map(_node("r", "根", [
            _node("b", "只有一个孩子", [_leaf("b1", "孤子", detail="有说明")]),
            _node("c", "深支", [_node("c1", "一级", [
                _node("c11", "二级", [_leaf("c111", "三层", detail="有说明")]),
                _leaf("c12", "二级之二", detail="有说明"),
                _leaf("c13", "二级之三", detail="有说明"),
            ])]),
            _leaf("d", "重复项"),
            _leaf("e", "重复项"),
            _leaf("f", "没有说明的叶子"),
        ])),
        # 深度上限
        _chain(8),
        # 同级近义
        _map(_node("r", "根", [_leaf("a", "数据处理"), _leaf("b", "数据预处理")])),
        # 跨支同核但没被 relations 指认
        _map(_node("r", "根", [
            _node("s1", "课程内容", [_leaf("s11", "第二部分 进程与线程（8 学时）")]),
            _node("s2", "学时分配", [_leaf("s21", "进程与线程 8 学时")]),
        ])),
    ]
    codes: set[str] = set()
    for content in contents:
        codes |= set(_codes(check(content)))
    # 引用类
    source = Source(id="s1", kind=SourceKind.document, status="parsed",
                    filename="a.txt", text="一句原文。")
    codes |= set(_codes(check(contents[0], [source])))
    return codes


def test_every_emitted_code_is_mapped_or_has_a_written_reason():
    """检查器能产出的每一个码：要么有动作，要么有理由。一个都不许悬空。

    没有这条，以后新增一条诊断然后忘了决定"转不转"，表现是"这条诊断永远不出现在
    建议面板里"——安静得像没做。
    """
    codes = _closure_codes()
    explicit = {
        "HEADING_ONLY_BRANCH", "SINGLE_CHILD", "DUPLICATE_LABEL",
        "SHALLOW_BRANCH", "MISSING_DETAIL", "NEAR_DUPLICATE_LABEL", "DEPTH_LIMIT",
        "CROSS_BRANCH_UNLINKED",
    }
    assert explicit <= codes, codes

    for code in sorted(codes):
        mapped = code in _SUGGESTION_OF_CODE
        explained = _not_converted_reason(code) is not None
        assert mapped or explained, f"{code} 既没有建议映射，也没有写清为什么不转"
        assert not (mapped and code in NOT_SUGGESTED)


def test_suggestion_kinds_are_action_words():
    """`expand` / `add_detail` 是这一轮补的动作名词，值是给人看的动词。"""
    assert SuggestionKind.expand.value == "expand"
    assert SuggestionKind.add_detail.value == "add_detail"
    used = set(_SUGGESTION_OF_CODE.values())
    assert {SuggestionKind.dedupe, SuggestionKind.expand, SuggestionKind.add_detail} <= used


# ---------------------------------------------------------------- 2. 近义（规则层）


def test_identical_labels_stay_duplicate_and_are_not_reported_twice():
    """逐字相同是 `DUPLICATE_LABEL` 的地盘，近义那条不能再来一遍。

    两条诊断落在同一个节点上、只差一个码，是最典型的"重复报"。
    """
    content = _map(_node("r", "根", [_leaf("a", "数据"), _leaf("b", "数据")]))
    assert _codes(check(content)) == ["DUPLICATE_LABEL"]
    assert [item.kind for item in suggest(content)] == [SuggestionKind.dedupe]


def test_labels_that_differ_only_in_punctuation_are_near_duplicates():
    """去标点/空格/全半角后相同 → warn（字面已经不是"不同"，只剩排版）。"""
    content = _map(_node("r", "根", [_leaf("a", "监督学习"), _leaf("b", "（监督）学习")]))
    near = _of_code(check(content), "NEAR_DUPLICATE_LABEL")
    assert len(near) == 1
    assert near[0].level is DiagnosticLevel.warn
    assert near[0].node_id == "b" and near[0].evidence == "a"
    assert "去掉标点与空格后完全相同" in near[0].message


def test_full_width_and_case_are_normalized_too():
    assert _near_reason("ML 基础", "ml基础") is not None
    assert _near_reason("ＡＢＣ处理", "ABC处理") is not None


def test_prefix_relation_is_reported_at_info_level():
    """一方是另一方的前缀 → info：字面包含是事实，"是不是同一件事"还要人看。"""
    content = _map(_node("r", "根", [
        _leaf("a", "监督学习"), _leaf("b", "监督学习与无监督学习"),
    ]))
    near = _of_code(check(content), "NEAR_DUPLICATE_LABEL")
    assert len(near) == 1
    assert near[0].level is DiagnosticLevel.info
    assert near[0].node_id == "b" and near[0].evidence == "a"
    assert "前缀" in near[0].message


@pytest.mark.parametrize("left,right", [
    ("监督学习", "有监督学习"),      # 任务说明里的第一个"模型层"例子，规则层其实抓得到
    ("数据处理", "数据预处理"),
])
def test_inserted_modifier_counts_as_near_duplicate(left, right):
    """加一个限定词 = 同一个概念的两种写法。

    这一档在生成的图上很罕见（提示词本来就要求每支给不同的要点），但用户手工
    在原地补一个更细的分支时极常见。
    """
    near = _of_code(check(_map(_node("r", "根", [
        _leaf("a", left), _leaf("b", right)]))), "NEAR_DUPLICATE_LABEL")
    assert len(near) == 1
    assert near[0].node_id == "b"
    assert "只多了" in near[0].message


@pytest.mark.parametrize("left,right", [
    ("监督学习", "无监督学习"),        # 反向修饰语：合并掉一条就是丢内容
    ("从重处罚的适用", "从轻处罚的适用"),
    ("分类任务选算法", "回归任务选算法"),
    ("按题型分配", "按模块分配"),      # 同一模板的两个实例（带替换）
    ("错题本管理", "错题本整理"),      # 换字型近义：规则层看不见，留给模型层
    ("机器学习", "深度学习"),          # 上下位
    ("定义", "定理"),                  # 太短：距离 1 就是换掉一半
    ("交叉验证", "分层交叉验证"),      # 插入占比 0.5，更像上下位
])
def test_pairs_that_must_not_be_flagged(left, right):
    """**故意不报**的那些形状。每一条都能在真实库里找到同形样本。

    「监督学习」/「无监督学习」尤其重要：它是"平行命名"这一类的代表，全库实测
    38 张真实生成图上，纯编辑距离判据命中 43 对、这类占 40 对、真正近义 0 对。
    合掉一支等于删内容。
    """
    assert _near_reason(left, right) is None


def test_near_duplicates_are_only_compared_within_one_parent():
    """跨分支的近名不报：不同父节点下的「数据处理」「数据预处理」可以各自成立。"""
    content = _map(_node("r", "学科", [
        _node("a", "物理", [_leaf("a1", "数据处理")]),
        _node("b", "数学", [_leaf("b1", "数据预处理")]),
    ]))
    assert _of_code(check(content), "NEAR_DUPLICATE_LABEL") == []


def test_near_duplicate_becomes_a_dedupe_suggestion():
    content = _map(_node("r", "根", [
        _leaf("a", "数据处理"), _leaf("b", "数据预处理"),
    ]))
    suggestion = suggest(content)[0]
    assert suggestion.kind is SuggestionKind.dedupe
    assert suggestion.node_id == "b"
    assert suggestion.evidence == "a"
    # 规则建议没有概率校准，不许伪造可信度
    assert suggestion.confidence is None


# ---------------------------------------------------------------- 3. 缺失环节


def test_shallow_branch_is_reported_where_the_sibling_side_is_deep():
    """一支直接到底、同级最深的还有三层 → 「这一支还没展开」。"""
    content = _map(_node("r", "根", [
        _leaf("shallow", "只有一个词"),
        _node("deep", "展开过的支", [
            _node("d1", "一层", [_node("d2", "两层", [_leaf("d3", "三层")])])]),
    ]))
    shallow = _of_code(check(content), "SHALLOW_BRANCH")
    assert [item.node_id for item in shallow] == ["shallow"]
    assert "还没展开" in shallow[0].message
    assert _kinds(suggest(content))["shallow"] is SuggestionKind.expand


def test_shallow_branch_needs_a_real_depth_gap():
    """1 对 2 层是正常密度（一张三层图里到处都是），不报。"""
    content = _map(_node("r", "根", [
        _leaf("flat", "直接到底"),
        _node("mid", "再一层", [_leaf("m1", "叶子")]),
    ]))
    assert _of_code(check(content), "SHALLOW_BRANCH") == []


def test_heading_like_branch_needs_deeper_siblings_to_be_worth_mentioning():
    """同一个形状分两条判据：全图两层 → SINGLE_CHILD，别的支更深 → 标题。"""
    flat = _map(_node("r", "根", [
        _node("b", "物理", [_leaf("b1", "应用")]),
        _node("c", "数学", [_leaf("c1", "应用")]),
    ]))
    codes = _codes(check(flat))
    assert "HEADING_ONLY_BRANCH" not in codes
    assert codes.count("SINGLE_CHILD") == 2

    deep = _map(_node("r", "根", [
        _node("b", "物理", [_leaf("b1", "应用")]),
        _node("c", "数学", [_node("c1", "一层", [
            _node("c2", "两层", [_leaf("c3", "三层")])])]),
    ]))
    heading = _of_code(check(deep), "HEADING_ONLY_BRANCH")
    assert [item.node_id for item in heading] == ["b"]
    assert "可能只是个标题" in heading[0].message
    assert _kinds(suggest(deep))["b"] is SuggestionKind.merge


def test_one_node_keeps_at_most_one_shape_diagnostic():
    """**不重复报**：同一个节点不会同时拿到 `SINGLE_CHILD` 与 `HEADING_ONLY_BRANCH`。

    两条判据都落在"恰好一个子节点"上，所以一条只能是另一条的替换。改成追加的
    实现会同时报两条，用户看到的是"这个节点有两个毛病"，而实际只有一个。
    """
    content = _map(_node("r", "根", [
        _node("b", "物理", [_leaf("b1", "应用")]),
        _node("c", "数学", [_node("c1", "一层", [
            _node("c2", "两层", [_leaf("c3", "三层")])])]),
        _node("d", "化学", [_leaf("d1", "实验")]),
    ]))
    shape = [item for item in check(content)
             if item.code in ("SINGLE_CHILD", "HEADING_ONLY_BRANCH")]
    node_ids = [item.node_id for item in shape]
    assert len(node_ids) == len(set(node_ids)), node_ids


def test_shallow_branch_and_single_child_are_mutually_exclusive():
    """`SHALLOW_BRANCH` 要求父节点至少两个子节点，`SINGLE_CHILD` 要求恰好一个。"""
    content = _map(_node("r", "根", [
        _leaf("solo", "单子"),
        _node("deep", "深支", [_node("d1", "一", [
            _node("d2", "二", [_leaf("d3", "三")])])]),
    ]))
    diagnostics = check(content)
    shallow_ids = {item.node_id for item in _of_code(diagnostics, "SHALLOW_BRANCH")}
    single_ids = {item.node_id for item in _of_code(diagnostics, "SINGLE_CHILD")}
    assert shallow_ids == {"solo"}
    # 深支那条单链上全是 SINGLE_CHILD，但它不能同时被算成"没展开"
    assert not shallow_ids & single_ids


def test_missing_detail_reports_once_with_a_count():
    """部分覆盖才报，且只报**一条**汇总（不是每个叶子一条）。"""
    content = _map(_node("r", "根", [
        _node("b", "支", [
            _leaf("n1", "写了", detail="这一条有说明"),
            _leaf("n2", "空一"), _leaf("n3", "空二"), _leaf("n4", "空三"),
        ]),
        _node("c", "另一支", [_leaf("c1", "一", detail="有"), _leaf("c2", "二", detail="有")]),
    ]))
    missing = _of_code(check(content), "MISSING_DETAIL")
    assert len(missing) == 1
    assert missing[0].node_id is None
    assert "这 3 个" in missing[0].message
    assert missing[0].evidence.split() == ["n2", "n3", "n4"]
    assert _kinds(suggest(content))[None] is SuggestionKind.add_detail


def test_all_empty_detail_is_an_import_shape_and_is_not_reported():
    """叶子**全空**不报：导入的 Markdown/OPML 只有标题，整张图没有正文一眼可见。

    报它的代价是每张导入图都被喊一次"没有说明"，而能做的动作不是逐节点补
    （那是重跑说明阶段）。实测库里 38 张生成图：21 张全满、17 张全空、
    **0 张部分覆盖**——所以这条规则的现实收益全在手工编辑之后。
    """
    content = _map(_node("r", "根", [
        _leaf("a", "一"), _leaf("b", "二"), _leaf("c", "三"), _leaf("d", "四"),
    ]))
    assert _of_code(check(content), "MISSING_DETAIL") == []


def test_one_or_two_empty_leaves_are_an_editing_state_not_a_finding():
    content = _map(_node("r", "根", [
        _leaf("a", "一", detail="有"), _leaf("b", "二", detail="有"),
        _leaf("c", "三"), _leaf("d", "四"),
    ]))
    assert _of_code(check(content), "MISSING_DETAIL") == []


def test_note_counts_as_a_body_so_a_user_written_note_is_not_called_empty():
    """`note` 是用户自己写的正文，界面上与 `detail` 分区展示，都算"有说明"。"""
    content = _map(_node("r", "根", [
        _leaf("a", "一", detail="有"), _leaf("b", "二", detail="有"),
        _leaf("c", "三", note="我自己写的"),
        _leaf("d", "四"), _leaf("e", "五"), _leaf("f", "六"),
    ]))
    missing = _of_code(check(content), "MISSING_DETAIL")
    assert len(missing) == 1
    assert missing[0].evidence.split() == ["d", "e", "f"]


# ------------------------------------------------ 3.5 跨支同核（未指认）

# 这一节的来历：`docs/CROSS-BRANCH-DUPLICATES.md` §八 的三次真跑里，加了交叉指认
# 规则之后 red 从 6 降到 0 —— 而 red 归零**不是**因为第二处消失了，是模型把 label
# 改写成近义再补一条 relations。逐字判据当场失明，"成功"于是成立。这一节的用例钉的
# 就是那个空洞：`CROSS_BRANCH_UNLINKED` 只在"改写过、且没指认"这一档说话。


def _rels(*pairs: tuple[str, str]) -> list[dict]:
    return [
        {"id": f"rel_{index}", "source": source, "target": target,
         "kind": "references", "why": "这两处是同一件事", "provenance": "ai"}
        for index, (source, target) in enumerate(pairs, start=1)
    ]


def _map_with_rels(root: dict, pairs: tuple[tuple[str, str], ...] = ()) -> MindMapContent:
    return MindMapContent.model_validate(
        {"title": "图", "root": root, "relations": _rels(*pairs)})


def test_cross_branch_same_thing_without_relation_is_reported():
    """同一件事写成近义两处、又没有一条关系指认 → 一条 warn 汇总。"""
    content = _map(_node("r", "根", [
        _node("a", "课程内容", [_leaf("a1", "第一部分 概述（4 学时）")]),
        _node("b", "学时分配", [_leaf("b1", "概述 4 学时")]),
    ]))
    found = _of_code(check(content), "CROSS_BRANCH_UNLINKED")
    assert len(found) == 1
    assert found[0].level is DiagnosticLevel.warn
    assert found[0].node_id is None            # 汇总一条，不落单个节点
    assert "1 处" in found[0].message
    assert "概述" in found[0].message
    assert found[0].evidence.split() == ["a1", "b1"]


def test_a_declared_relation_silences_it_in_both_directions():
    """有 relations 就是"指认过了"，方向无关 —— 反向那条也算。"""
    root = _node("r", "根", [
        _node("a", "课程内容", [_leaf("a1", "第二部分 进程与线程（8 学时）")]),
        _node("b", "学时分配", [_leaf("b1", "进程与线程 8 学时")]),
    ])
    assert _of_code(check(_map_with_rels(root, (("a1", "b1"),))), "CROSS_BRANCH_UNLINKED") == []
    assert _of_code(check(_map_with_rels(root, (("b1", "a1"),))), "CROSS_BRANCH_UNLINKED") == []


def test_verbatim_cross_branch_names_stay_with_the_other_code():
    """逐字同名（只差空白）归 `CROSS_BRANCH_DUPLICATE`，两条码不许落在同一对上。

    两个检查器分工：那条说"这句话出现了两遍"，这条说"这两处你打算怎么处理，图上
    读不出来"。同报一次等于让用户看两条说同一件事的诊断。
    """
    content = _map(_node("r", "根", [
        _node("a", "实验安排", [_leaf("a1", "内核模块编写 4 次")]),
        _node("b", "实验考核", [_leaf("b1", "内核模块编写4次")]),
    ]))
    assert _of_code(check(content), "CROSS_BRANCH_UNLINKED") == []


def test_same_parent_and_ancestor_chain_are_not_cross_branch():
    """同父是 DUPLICATE/NEAR 的地盘；同一条祖先链是层级问题。两者都不算跨支。"""
    same_parent = _map(_node("r", "根", [
        _leaf("a", "第一部分 概述"), _leaf("b", "概述 4 学时"),
    ]))
    assert _of_code(check(same_parent), "CROSS_BRANCH_UNLINKED") == []

    # 父子同名（S07 的「力矩与转动定律」）：父既当条目又当分组
    chain = _map(_node("r", "根", [
        _node("a", "第二部分 刚体转动", [
            _node("a1", "力矩与转动定律", [_leaf("a11", "转动惯量")]),
        ]),
        _node("b", "另一支", [_leaf("b1", "力矩与转动定律")]),
    ]))
    assert _of_code(check(chain), "CROSS_BRANCH_UNLINKED") == []


def test_unlinked_is_one_summary_not_one_per_pair():
    """3 对 → 1 条诊断，message 里带对数与样本（S02 一次能出 8 对，逐对报会淹掉面板）。"""
    content = _map(_node("r", "根", [
        _node("x", "课程内容", [
            _leaf("x1", "第一部分 概述（4 学时）"),
            _leaf("x2", "第二部分 进程与线程（8 学时）"),
            _leaf("x3", "第三部分 调度与并发（6 学时）"),
        ]),
        _node("y", "学时分配", [
            _leaf("y1", "概述 4 学时"),
            _leaf("y2", "进程与线程 8 学时"),
            _leaf("y3", "调度与并发 6 学时"),
        ]),
    ]))
    found = _of_code(check(content), "CROSS_BRANCH_UNLINKED")
    assert len(found) == 1
    assert "3 处" in found[0].message


def test_core_label_strips_numbering_and_quantity_but_keeps_ordinary_words():
    """剥前缀/尾缀的边界：只剥序号与"数量+单位"，不碰正文。

    这一条是判据精度的全部——阈值版（2-gram Dice ≥ 0.5）在库里 28 棵树上假阳性
    210 对：「第二章 8 学时」与「第五章 6 学时」、「WebSocket」与「Socket 编程」都
    被判成同一件事。所以"剥什么"要逐个钉住。
    """
    core = service._core_label
    assert core("第一部分 概述（4 学时）") == core("概述 4 学时") == "概述"
    assert core("第二部分 进程与线程（8 学时）") == core("进程与线程8学时") == "进程与线程"
    assert core("一、本次评审范围") == "本次评审范围"
    assert core("M2：术语库与机译接入") == "术语库与机译接入"
    # 编号不同 → 核心词不同（这一档最容易被相似度判据误判）
    assert core("第二章 8 学时") != core("第五章 6 学时")
    # 裸"一"开头的正文不能被当成序号剥掉
    assert core("一般原理") == "一般原理"


# ---------------------------------------------------------------- 4. 模型层


@pytest.fixture
def _model_off(monkeypatch):
    monkeypatch.delenv(service.LLM_ENV, raising=False)


def _wide_map() -> MindMapContent:
    """同级节点够多的图（≥ `MIN_LLM_SIBLINGS`），模型层才会被考虑。"""
    return _map(_node("r", "根", [
        _node("b1", "支一", [_leaf(f"b1_{index}", f"要点{index}") for index in range(1, 6)]),
        _node("b2", "支二", [_leaf(f"b2_{index}", f"细节{index}") for index in range(1, 6)]),
    ]))


def test_model_layer_is_off_by_default(_model_off):
    """缺省关：一次调用都不发生。"""
    with patch.object(service, "_model_near_pairs") as call:
        suggest(_wide_map())
    call.assert_not_called()


def test_small_maps_never_reach_the_model(_model_off):
    """同级节点总数 < 8：人一眼扫完，不值得花一次调用。"""
    small = _map(_node("r", "根", [_leaf("a", "一"), _leaf("b", "二")]))
    with patch.object(service, "_model_near_pairs") as call:
        suggest(small, use_model=True)
    call.assert_not_called()


def test_env_switch_turns_the_layer_on(_model_off, monkeypatch):
    monkeypatch.setenv(service.LLM_ENV, "1")
    with patch.object(service, "_model_near_pairs", return_value=[]) as call:
        suggest(_wide_map())
    call.assert_called_once()


def test_model_pairs_become_dedupe_suggestions():
    """模型按行号回答，建议自己也带得动：不依赖先有诊断。

    行号在这里是硬契约：`_wide_map` 的三行依次是根的分支、要点组、细节组，
    「要点2」在第 2 行（0 起算的 1）。
    """
    def fake(groups):
        assert groups, "喂给模型的同级行不能是空的"
        return [(1, "要点2", "要点3", "两处都在讲要点")]

    with patch.object(service, "_model_near_pairs", new=fake):
        suggestions = suggest(_wide_map(), use_model=True)

    assert len(suggestions) == 1
    item = suggestions[0]
    assert item.kind is SuggestionKind.dedupe
    assert item.node_id == "b1_3" and item.evidence == "b1_2"
    assert "模型判定" in item.message
    assert item.confidence is None


def test_model_pairs_outside_one_line_are_dropped():
    """行号对不上、或标签不在那一行里 → 丢弃（模型实测里 11/11 都是这种）。"""
    with patch.object(service, "_model_near_pairs",
                      new=lambda groups: [(1, "要点2", "细节2", "跨行")]):
        assert suggest(_wide_map(), use_model=True) == []
    with patch.object(service, "_model_near_pairs",
                      new=lambda groups: [(9, "要点2", "要点3", "行号越界")]):
        assert suggest(_wide_map(), use_model=True) == []


def test_model_pairs_already_found_by_the_rules_are_not_repeated():
    """规则层报过的那一对，模型再说一遍也只留一条建议。"""
    content = _map(_node("r", "根", [
        _node("b1", "支一", [
            _leaf("x", "数据处理"), _leaf("y", "数据预处理"),
            *[_leaf(f"z{index}", f"别的{index}") for index in range(1, 7)],
        ]),
        _node("b2", "支二", [_leaf(f"w{index}", f"要点{index}") for index in range(1, 6)]),
    ]))
    assert [item.node_id for item in suggest(content)] == ["y"]

    with patch.object(service, "_model_near_pairs",
                      new=lambda groups: [(1, "数据处理", "数据预处理", "重复")]):
        suggestions = suggest(content, use_model=True)
    assert [item.node_id for item in suggestions] == ["y"]


def test_one_node_takes_at_most_one_model_suggestion():
    with patch.object(service, "_model_near_pairs", new=lambda groups: [
        (1, "要点1", "要点2", "a"), (1, "要点1", "要点3", "b"),
    ]):
        suggestions = suggest(_wide_map(), use_model=True)
    assert len(suggestions) == 1


def test_model_layer_is_silent_without_configuration(_model_off):
    """没配 `MINDMAP_LLM_*` → 空列表、不抛异常（体检不能因为模型不可用变 500）。"""
    for name in ("MINDMAP_LLM_BASE_URL", "MINDMAP_LLM_API_KEY", "MINDMAP_LLM_MODEL"):
        os.environ.pop(name, None)
    assert service._model_near_pairs([["甲", "乙"]]) == []


def test_model_layer_swallows_transport_errors(monkeypatch):
    """网络层抛错 → 空列表。这一层是附加信息，不许把 `/suggest` 拖成 500。"""
    import httpx

    monkeypatch.setenv("MINDMAP_LLM_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("MINDMAP_LLM_API_KEY", "k")
    monkeypatch.setenv("MINDMAP_LLM_MODEL", "m")

    def boom(*args, **kwargs):
        raise httpx.ConnectError("no route")

    with patch("app.optimization.service.httpx.Client", new=boom):
        assert service._model_near_pairs([["甲", "乙"]]) == []


@pytest.mark.skipif(not os.getenv("MINDMAP_LLM_API_KEY"),
                    reason="需要 MINDMAP_LLM_* 才能跑真模型")
def test_live_model_finds_a_synonym_pair_the_rules_cannot_see():
    """真模型一次：规则层看不见的换字型近义必须被找到。

    「正则化」/「规范化」几乎没有共同字面，正是留给模型的那一档。没有 key 时
    跳过——离线跑全量套件不该产生网络请求与费用。
    """
    pairs = service._model_near_pairs([["正则化", "规范化", "过拟合", "欠拟合"]])
    assert any({"正则化", "规范化"} == {left, right} for _, left, right, _ in pairs), pairs


# ---------------------------------------------------------------- 路由层的树型闸
#
# 在此之前传鱼骨/流程会 `AttributeError: ... has no attribute 'root'`（500）。
# 前端 `stores/structure.ts` 已经在发请求前拦住了，所以这个雷只在接口层可见 ——
# 换一个客户端（curl、将来的移动端）就会踩。422 比 500 诚实：500 说的是"服务器
# 坏了"，而事实是"这个输入不在这个接口的服务范围里"。


def _client():
    from conftest import guest_client

    return guest_client()


_FISH = {"content": {"title": "t", "problem": "p",
                     "bones": [{"category": "人", "root": {"id": "b1", "label": "x"}}]}}
_FLOW = {"content": {"title": "t",
                     "stages": [{"id": "s1", "root": {"id": "a", "label": "提交"}}]}}


@pytest.mark.parametrize("endpoint", ["/api/optimization/check",
                                      "/api/optimization/suggest"])
@pytest.mark.parametrize("body", [_FISH, _FLOW], ids=["fishbone", "flow"])
def test_non_tree_shapes_get_422_not_500(endpoint: str, body: dict) -> None:
    response = _client().post(endpoint, json=body)
    assert response.status_code == 422
    assert "只支持树型" in response.json()["detail"]


def test_tree_still_gets_diagnostics() -> None:
    body = {"content": {"title": "t", "root": {
        "id": "r", "label": "根",
        "children": [{"id": "c1", "label": "唯一子节点"}]}}}
    response = _client().post("/api/optimization/check", json=body)
    assert response.status_code == 200
    codes = [item["code"] for item in response.json()["diagnostics"]]
    assert "SINGLE_CHILD" in codes
