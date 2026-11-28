"""三档判定（`scripts/measure_cross_branch_duplicates.py::triage`）的判据。

为什么这三条要写成用例：它们决定"17 处跨支重复里有几处该改"这个数，而那个数
会直接改验收口径（D 判据红不红）。**判据不进用例，下一次就会按"同名就算重复"
去改产品**，而旧树 17 处里真有 3 处是误报（同名不同指）、8 处是交叉引用（读到两
支才完整）。

四条规则各有一个反例：
  - 同一父下的同级同名 → grey（`check()` 的 `DUPLICATE_LABEL` 管，不该并列）
  - 父子同名（同一条祖先链）→ grey（那是"父节点既当条目又当分组"，另一类缺陷）
  - 两个父是同一模板的不同实例（候选人 A / 候选人 C）→ grey（同名不同指）
  - 跨支同名 + detail 讲同一件事 → red；detail 各讲各自视角 → amber

真值取自旧树实测（`docs/CROSS-BRANCH-DUPLICATES.md` §一）：S02 的六章是 red，
S08 的三个需求、S17 的术语库、S05 的通胀口径是 amber，S14 的两栏与 S07 的同名
父子是 grey。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

from app.contracts.mindmap import MindMapContent  # noqa: E402
from measure_cross_branch_duplicates import analyse  # noqa: E402


def _node(node_id: str, label: str, detail: str = "", children: list | None = None) -> dict:
    return {"id": node_id, "label": label, "detail": detail, "children": children or []}


def _bands(content: MindMapContent) -> dict[str, str]:
    """`{label: 档}` —— 每一组同名给出它的档位。"""
    return {group["key"]: group["band"] for group in analyse(content)["groups"]}


def test_same_parent_duplicate_is_not_a_cross_branch_finding():
    """同父下的两个同名节点：那是 `check()` 的 `DUPLICATE_LABEL`，不是这条判据。"""
    content = MindMapContent.model_validate({"title": "t", "root": _node("root", "主题", children=[
        _node("b1", "分支一", children=[
            _node("a", "概述", "第一处说明。"),
            _node("b", "概述", "第二处说明，与上面完全不同的一句话。"),
        ]),
        _node("b2", "分支二", children=[_node("c", "要点", "说明。")]),
    ])})
    assert _bands(content)["概述"] == "grey"


def test_parent_and_child_with_the_same_label_is_a_level_problem():
    """父子同名（同一条祖先链）：不是跨支，`_cross_branch_duplicates` 也不该报它。

    这是实测踩到的：S07 的「力矩与转动定律」既是一级分支下的条目、又自己带子节点。
    这条判据的 docstring 一直写着"同一条祖先链上的两处不算跨支"，而实现只比**直接
    父元组**——父子同名的两条链是前缀关系、父元组却不同（`()` 与 `(父,)`），于是被
    算成跨支。这一段是**双向**的：档位（grey）与跨支判据（不报）必须同时成立，
    否则"跨支重复 17 处"这个数里会一直躺着一处层级问题。
    """
    from app.agent.tools import _cross_branch_duplicates

    content = MindMapContent.model_validate({"title": "t", "root": _node("root", "主题", children=[
        _node("b1", "课程内容与模块", children=[
            _node("p", "力矩与转动定律", "力矩与转动定律把力矩与角加速度联系起来。", children=[
                _node("q", "力矩与转动定律", "力矩是力使刚体绕轴转动的作用量。"),
            ]),
        ]),
        _node("b2", "教学安排", children=[_node("r", "学时", "共 64 学时。")]),
    ])})
    assert _bands(content)["力矩与转动定律"] == "grey"
    # 修好之后：跨支判据不再把它算进来（旧树实测 34 → 32 个节点就是这么来的）
    assert _cross_branch_duplicates(content.root) == []


def test_two_branches_with_the_same_label_are_still_reported_by_the_cross_branch_check():
    """反方向：两支下同名仍是跨支——修链判据不能顺手把真重复也放过。"""
    from app.agent.tools import _cross_branch_duplicates

    content = MindMapContent.model_validate({"title": "t", "root": _node("root", "主题", children=[
        _node("b1", "课程内容与模块", children=[
            _node("p", "力矩与转动定律", "一处说明。")]),
        _node("b2", "教学安排", children=[
            _node("q", "力矩与转动定律", "另一处说明。")]),
    ])})
    found = _cross_branch_duplicates(content.root)
    assert [label for label, _ in found] == ["力矩与转动定律"]
    assert found[0][1] == ["教学安排", "课程内容与模块"]


def test_the_same_field_of_two_candidates_is_not_a_duplicate():
    """S14 那两处：三位候选人各有「项目经历 / 弱项」——同一模板的不同实例。

    同名不同指。这一条最重要：不排除它，模型把会议纪要写规范（每个人都一栏）就会
    被 D 判据判红，等于用判据去否掉正确的写法。
    """
    content = MindMapContent.model_validate({"title": "t", "root": _node("root", "会议纪要", children=[
        _node("a", "候选人 A（后端方向，硕士）", children=[
            _node("a1", "项目经历", "做过一个日志检索系统。"),
            _node("a2", "弱项", "分布式一致性只停留在名词。"),
        ]),
        _node("c", "候选人 C（算法方向，博士）", children=[
            _node("c1", "项目经历", "检索排序模型微调，论文一篇在投。"),
            _node("c2", "弱项", "沟通偏学术。"),
        ]),
    ])})
    bands = _bands(content)
    assert bands["项目经历"] == "grey"
    assert bands["弱项"] == "grey"


def test_two_branches_saying_the_same_thing_is_red():
    """S02 那 9 处的形态：同一件事在两支下各写一遍，detail 也是同一段内容。"""
    same = "第一部分概述，共 4 学时，内容包括操作系统的定义与作用、发展历史、分时与实时系统。"
    content = MindMapContent.model_validate({"title": "t", "root": _node("root", "课程大纲", children=[
        _node("b1", "课程内容与模块", children=[_node("x", "概述4学时", same)]),
        _node("b2", "六部分学时分配", children=[_node("y", "概述4学时", same)]),
    ])})
    assert _bands(content)["概述4学时"] == "red"


def test_two_branches_talking_about_two_angles_is_amber():
    """S08 / S17 那几处：范围内的清单 vs 讨论记录、设计要求 vs 里程碑交付。

    读者两支都要看得到，删一处会丢信息——所以它不该计入 D 的红，只该当提示。
    """
    content = MindMapContent.model_validate({"title": "t", "root": _node("root", "会议纪要", children=[
        _node("b1", "本次评审范围", children=[
            _node("x", "导出带水印PDF", "第三项需求，把导图导出为带水印的 PDF，用于对外分享与演示。"),
        ]),
        _node("b2", "讨论要点", children=[
            _node("y", "导出带水印PDF", "孙鹏要求水印含团队名与导出时间，李敏担心演示效果，折中为可关闭。"),
        ]),
    ])})
    assert _bands(content)["导出带水印PDF"] == "amber"
