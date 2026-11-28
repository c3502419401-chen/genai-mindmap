"""三条"修法唯一"规则的自动修复（`optimization/repair.py`）。

这一组用例的四个守护对象，每条都对应一个具体的坏结果：

  1. **幂等**（`test_idempotent_*`）。跑两次结果一样。不幂等的直接后果是用户连点
     两次「一键修复」，第二次又改了东西——而两棵树都存过盘，撤不回去。
  2. **引用并集**（`test_refs_union_*`）。合并取并集而不是覆盖。覆盖的后果是用户
     点一次修复就丢掉一半文献依据，而界面上什么都看不出来。
  3. **可撤销**（`test_revert_*`）。`revert(repair(x))` 与 `x` 逐字段相同。这是
     "可撤销"唯一该被信任的形式：前端的撤销栈只活在一个会话里。
  4. **只修三条**（`test_only_three_codes`）。近义/太浅/缺说明继续只给建议——它们的
     动作要模型或要人。

还有一条贯穿的：**修完那条码要真的消失**，且没引入新的 warn。`remaining` 是修完
重跑的 `check()`，所以这条断言用的是修完之后的诊断，不是修之前的猜测。

## 测试数据为什么大多有"第三个分支"

`SINGLE_CHILD` 会连锁：合并掉两条重名之后，如果父节点只剩一个孩子，压链段就会顺手
把父节点也压掉。那不是 bug（它就是"修到不动点"），但会让"只想验合并"的用例顺带
验到压链。所以验合并的数据都留一条正常分支（两个子节点），让树在合并后仍有 2 个分支。
`test_merge_can_chain_into_a_collapse` 专门钉住这个连锁——它是真会发生的。
"""
from __future__ import annotations

import json

import pytest

from app.contracts.mindmap import MindMapContent, Node
from app.optimization.repair import (
    AUTO_REPAIRABLE_CODES,
    DEFERRED_LABEL_PREFIX,
    MANUAL_ONLY,
    MAX_SOURCE_REFS,
    READABLE_LABEL_CHARS,
    overlong_labels,
    repair,
    repair_summary,
    revert,
)
from app.optimization.service import _SUGGESTION_OF_CODE, NOT_SUGGESTED, check


def _map(root: dict, *, title: str = "图", **extra) -> MindMapContent:
    """造一张合法导图：`source_usage` 按树里出现的引用自动补齐。

    契约强制"节点引用的来源必须在 `source_usage` 里"，所以每次手写引用都要跟着补一份
    摘要。这里自动补，免得每个用例重复一遍与它要验的东西无关的字段。
    """
    ids: list[str] = []

    def walk(node: dict) -> None:
        for ref in node.get("source_refs") or []:
            if ref["source_id"] not in ids:
                ids.append(ref["source_id"])
        for child in node.get("children") or []:
            walk(child)

    walk(root)
    extra.setdefault("source_usage", [
        {"source_id": source_id, "kind": "text", "char_count": 100} for source_id in ids
    ])
    return MindMapContent.model_validate({"title": title, "root": root, **extra})


def _leaf(node_id: str, label: str, **extra) -> dict:
    return {"id": node_id, "label": label, "children": [], **extra}


def _node(node_id: str, label: str, children: list[dict], **extra) -> dict:
    return {"id": node_id, "label": label, "children": children, **extra}


def _ref(source_id: str, start: int = 0, end: int = 2) -> dict:
    return {"source_id": source_id, "start": start, "end": end}


def _codes(diagnostics) -> list[str]:
    return [item.code for item in diagnostics]


def _dump(content: MindMapContent) -> str:
    return content.model_dump_json()


def _labels(content: MindMapContent) -> list[str]:
    out: list[str] = []

    def walk(node: Node) -> None:
        out.append(node.label)
        for child in node.children:
            walk(child)

    walk(content.root)
    return out


#: 一条"正常分支"，两个子节点。留在数据里挡住压链段的连锁（见文件头）。
def _normal() -> dict:
    return _node("normal", "正常分支", [_leaf("n1", "正常甲"), _leaf("n2", "正常乙")])


# ---------------------------------------------------------------- 1. 幂等


def test_idempotent_duplicate_merge():
    """合并同一个重名两次：第二次什么都不做，树逐字节相同。"""
    content = _map(_node("r", "根", [
        _leaf("a", "重复"),
        _leaf("b", "重复"),
        _normal(),
    ]))

    first = repair(content)
    assert [item.code for item in first.applied] == ["DUPLICATE_LABEL"]
    assert "DUPLICATE_LABEL" not in _codes(first.remaining)

    second = repair(first.content)
    assert second.applied == []
    assert _dump(second.content) == _dump(first.content)


def test_merge_keeps_both_subtrees_in_order():
    """合并保留子树并集：先是保留者的子节点，再是被并进来的，顺序不动。"""
    content = _map(_node("r", "根", [
        _node("a", "重复", [_leaf("a1", "甲")]),
        _node("b", "重复", [_leaf("b1", "乙")]),
        _normal(),
    ]))

    merged = repair(content).content
    first = merged.root.children[0]
    assert first.id == "a"
    assert [child.id for child in first.children] == ["a1", "b1"]


def test_merge_always_keeps_the_first_occurrence():
    """三个同名节点并进**第一个**：结果与"从哪一头开始处理"无关。"""
    content = _map(_node("r", "根", [
        _leaf("a", "三胞胎"),
        _leaf("b", "三胞胎"),
        _leaf("c", "三胞胎"),
        _normal(),
    ]))

    result = repair(content)
    assert result.content.root.children[0].id == "a"
    assert {child.id for child in result.content.root.children} == {"a", "normal"}
    assert result.steps == 2


def test_idempotent_single_child_collapse():
    """单子链压到不动点：第二次跑没有可压的了。"""
    content = _map(_node("r", "根", [
        _node("a", "第一层", [_node("b", "第二层", [_leaf("c", "第三层")])]),
        _leaf("d", "别的分支"),
    ]))

    first = repair(content)
    assert first.applied, "应当至少压掉一层"
    second = repair(first.content)
    assert second.applied == []
    assert _dump(second.content) == _dump(first.content)


def test_single_child_runs_to_fixpoint():
    """一条 4 层的单子链要一路压到底——不是"压一次就交差"。"""
    content = _map(_node("r", "根", [
        _node("a", "甲", [_node("b", "乙", [_node("c", "丙", [_leaf("d", "丁")])])]),
        _normal(),
    ]))

    result = repair(content)
    only = result.content.root.children[0]
    assert only.children == []
    for part in ("甲", "乙", "丙", "丁"):
        assert part in only.label
    assert "SINGLE_CHILD" not in _codes(result.remaining)


def test_merge_can_chain_into_a_collapse():
    """合并之后父节点只剩一个孩子 → 同一趟里继续把它压掉（修到不动点，不是修一半）。"""
    content = _map(_node("r", "根", [
        _leaf("a", "重复"),
        _leaf("b", "重复"),
    ]))

    result = repair(content)
    assert [item.code for item in result.applied] == ["DUPLICATE_LABEL", "SINGLE_CHILD"]
    # 根吸收掉唯一的孩子，标签把两层都写进去。
    assert "重复" in result.content.root.label
    assert result.content.root.children == []


# ---------------------------------------------------------------- 2. 引用并集


def test_refs_union_is_a_union_not_an_overwrite():
    """两边各有引用 → 合并后两边都在，且重复的那条只留一次。"""
    content = _map(_node("r", "根", [
        _leaf("a", "同一节", source_refs=[_ref("s1", 0, 4), _ref("s2", 10, 14)]),
        _leaf("b", "同一节", source_refs=[_ref("s2", 10, 14), _ref("s3", 20, 24)]),
        _normal(),
    ]))

    kept = repair(content).content.root.children[0]
    source_ids = [ref.source_id for ref in kept.source_refs]
    # 保序：先是保留者的，再是被并进来的（重复的 s2 不重复出现）。
    assert source_ids == ["s1", "s2", "s3"]


def test_refs_union_keeps_detail_and_note_from_both_sides():
    """正文与用户备注都保留：`detail` 是模型写的、`note` 是用户写的，两边都不能丢。"""
    content = _map(_node("r", "根", [
        _leaf("a", "同名", detail="甲的说明", note="甲的备注"),
        _leaf("b", "同名", detail="乙的说明", note="乙的备注"),
        _normal(),
    ]))

    kept = repair(content).content.root.children[0]
    assert "甲的说明" in (kept.detail or "")
    assert "乙的说明" in (kept.detail or "")
    assert "甲的备注" in (kept.note or "")
    assert "乙的备注" in (kept.note or "")


def test_refs_overflow_is_skipped_not_truncated():
    """并集超过契约上限（20 条）→ **放弃这条修复**并说明原因，绝不截断。

    截断是丢证据：用户会得到一棵"看起来修好了"的树，而其中一半的引文消失了，
    且没有任何地方提示这件事发生过。
    """
    left = [_ref(f"l{index}", index, index + 1) for index in range(11)]
    right = [_ref(f"r{index}", index, index + 1) for index in range(11)]
    content = _map(_node("r", "根", [
        _leaf("a", "同名", source_refs=left),
        _leaf("b", "同名", source_refs=right),
        _normal(),
    ]))

    result = repair(content)
    assert result.applied == []
    assert len(result.skipped) == 1
    assert "超出契约上限" in result.skipped[0].reason
    # 树一个字节都没动。
    assert _dump(result.content) == _dump(content)


def test_source_ref_limit_matches_contract():
    """`MAX_SOURCE_REFS` 是抄契约的一个数，契约改了这里要跟着改。"""
    field = Node.model_fields["source_refs"]
    assert field.metadata, "契约的 source_refs 上应当有 max_length"
    assert any(getattr(item, "max_length", None) == MAX_SOURCE_REFS for item in field.metadata)


# ---------------------------------------------------------------- 3. 可撤销


def test_revert_restores_the_original_tree_field_by_field():
    """`revert(repair(x))` 与 `x` 逐字段相同——含被并掉的节点与它整棵子树。"""
    content = _map(
        _node("r", "根", [
            _node("a", "重复", [_leaf("a1", "甲"), _leaf("a2", "乙")],
                  detail="甲的说明", source_refs=[_ref("s1")]),
            _node("b", "重复", [_leaf("b1", "丙")], source_refs=[_ref("s2")]),
            _node("c", "单子", [_leaf("c1", "唯一")]),
            _normal(),
        ]),
        relations=[{"id": "rel1", "source": "b1", "target": "a1", "kind": "causes"}],
    )

    result = repair(content)
    assert len(result.applied) >= 2
    restored = revert(result.content, result.applied)
    assert _dump(restored) == _dump(content)


def test_revert_after_each_step_is_possible():
    """只撤销最后一步也成立：撤销是逐条的，不是"全有或全无"。"""
    content = _map(_node("r", "根", [
        _leaf("a", "重复"),
        _leaf("b", "重复"),
        _node("c", "单子", [_leaf("c1", "唯一")]),
        _normal(),
    ]))

    result = repair(content)
    assert len(result.applied) >= 2
    partial = revert(result.content, result.applied[-1:])
    assert _dump(partial) != _dump(result.content)
    assert _dump(partial) != _dump(content)


def test_revert_restores_redirected_relation_endpoints():
    """指向被并掉节点的关系线要改到保留者，撤销时改回来。"""
    content = _map(
        _node("r", "根", [
            _leaf("a", "同名"),
            _leaf("b", "同名"),
            _node("x", "别处", [_leaf("x1", "甲"), _leaf("x2", "乙")]),
        ]),
        relations=[
            {"id": "rel1", "source": "b", "target": "x", "kind": "causes"},
            {"id": "rel2", "source": "x", "target": "a", "kind": "part_of"},
        ],
    )

    result = repair(content)
    relations = {item.id: item for item in result.content.relations}
    # b 被并进 a：指向 b 的那条线改到 a，**不是被删掉**。
    assert relations["rel1"].source == "a"
    assert relations["rel1"].target == "x"
    # 另一条没被碰。
    assert relations["rel2"].source == "x"

    back = {item.id: item for item in revert(result.content, result.applied).relations}
    assert back["rel1"].source == "b"
    assert back["rel2"].source == "x"


def test_revert_rejects_an_inconsistent_history():
    """撤销一份对不上的历史要**报错**，不能静默返回一棵怪树。"""
    content = _map(_node("r", "根", [_leaf("a", "重复"), _leaf("b", "重复"), _normal()]))
    result = repair(content, codes=["DUPLICATE_LABEL"])
    broken = result.content.model_copy(deep=True)
    broken.root.children.clear()
    with pytest.raises(ValueError):
        revert(broken, result.applied)


# ---------------------------------------------------------------- 4. 只修三条


def test_only_three_codes_are_auto_repaired():
    """近义 / 太浅 / 缺说明一个都不动，并且各自有写下来的理由。"""
    content = _map(_node("r", "根", [
        _leaf("a", "监督学习"),
        _leaf("b", "有监督学习"),
        _normal(),
    ]))
    assert "NEAR_DUPLICATE_LABEL" in _codes(check(content))

    result = repair(content)
    assert result.applied == []
    assert _dump(result.content) == _dump(content)
    for code in ("NEAR_DUPLICATE_LABEL", "SHALLOW_BRANCH", "MISSING_DETAIL"):
        assert code in MANUAL_ONLY


def test_every_checkable_code_has_a_decision():
    """`check()` 能产出的每个结构码，要么可自动修，要么在 `MANUAL_ONLY` 里。

    这条与 `service.py` 的 "每个码要么转建议、要么在 NOT_SUGGESTED 里" 是同一条
    规矩的第二个出口：新增诊断码时忘了决定"能不能自动修"，这里会红。
    """
    codes = {
        "SINGLE_CHILD", "DEPTH_LIMIT", "DUPLICATE_LABEL", "NEAR_DUPLICATE_LABEL",
        "SHALLOW_BRANCH", "HEADING_ONLY_BRANCH", "MISSING_DETAIL",
        "CROSS_BRANCH_UNLINKED", "CROSS_BRANCH_DUPLICATE",
        "LABEL_OVERLONG_IN_DELIVERY",
    }
    for code in codes:
        assert code in AUTO_REPAIRABLE_CODES or code in MANUAL_ONLY, code
    for code in _SUGGESTION_OF_CODE:
        assert code in AUTO_REPAIRABLE_CODES or code in MANUAL_ONLY, code
    for code in NOT_SUGGESTED:
        assert code in MANUAL_ONLY, code


def test_codes_argument_narrows_the_scope():
    """只传 `DUPLICATE_LABEL` 时，单子链一条都不压。"""
    content = _map(_node("r", "根", [
        _leaf("a", "重复"),
        _leaf("b", "重复"),
        _node("c", "单子", [_leaf("c1", "唯一")]),
        _normal(),
    ]))

    result = repair(content, codes=["DUPLICATE_LABEL"])
    assert [item.code for item in result.applied] == ["DUPLICATE_LABEL"]
    assert "SINGLE_CHILD" in _codes(result.remaining)


def test_order_of_repairs_does_not_change_the_result():
    """**"修法唯一"的可检验形式**：分段跑与一趟跑，结果逐字节相同。

    这条用例是"唯一"这个说法的证据。合并段永远在压链段之前，所以"先只修重名、再只
    压链"与"一趟全修"必须得到同一棵树。反过来的顺序（先压链）得不到——压链会改
    label，两个同名兄弟会被压成 `甲 / 子` 与 `乙 / 子`，那时候它们已经不同名了。
    这个顺序是 `repair()` 里写死的，不是调用方决定的。
    """
    root = _node("r", "根", [
        _node("a", "重名", [_leaf("a1", "甲")]),
        _node("b", "重名", [_leaf("b1", "乙")]),
        _node("c", "单子", [_leaf("c1", "唯一")]),
        _normal(),
    ])
    both = repair(_map(root))
    staged = repair(
        repair(_map(root), codes=["DUPLICATE_LABEL"]).content,
        codes=["SINGLE_CHILD", "HEADING_ONLY_BRANCH"],
    )
    assert _dump(both.content) == _dump(staged.content)
    # 分两段跑多了一次调用的边界，但**步数之和**与一趟跑相同（每步都对应一个真实的
    # 结构改动，没有哪一段偷偷多修或少修）。
    assert both.steps == 1 + staged.steps


# ---------------------------------------------------------------- 5. 根的边界


def test_root_with_one_child_collapses_into_the_root():
    """根只有一个孩子：根不能消失，所以把孩子并进根，根的 id 保住。"""
    content = _map(_node("r", "主题", [
        _node("only", "唯一条支", [_leaf("x", "内容甲"), _leaf("y", "内容乙")]),
    ]))

    result = repair(content)
    assert result.content.root.id == "r"
    assert "唯一条支" in result.content.root.label
    assert "主题" in result.content.root.label
    assert {child.id for child in result.content.root.children} == {"x", "y"}
    assert "SINGLE_CHILD" not in _codes(result.remaining)
    assert _dump(revert(result.content, result.applied)) == _dump(content)


def test_heading_only_branch_is_folded_with_its_leaf():
    """一级分支只有一个叶子、而别的分支更深 → 那个叶子被并到分支上。"""
    content = _map(_node("r", "根", [
        _node("heading", "只是个标题", [_leaf("h1", "实际内容")]),
        _node("deep", "深分支", [
            _node("d1", "层一", [_node("d2", "层二", [_leaf("d3", "层三")])]),
            _leaf("d4", "旁支"),
        ]),
    ]))

    result = repair(content)
    folded = [item for item in result.applied if item.code == "HEADING_ONLY_BRANCH"]
    assert folded, _codes(check(content))
    labels = _labels(result.content)
    assert any("只是个标题" in label and "实际内容" in label for label in labels)


def test_repair_never_adds_a_warn_level_problem():
    """修完只能变好：`warn` 及以上的诊断数不增加。"""
    content = _map(_node("r", "根", [
        _node("a", "重复", [_leaf("a1", "甲")]),
        _node("b", "重复", [_leaf("b1", "乙")]),
        _node("c", "单子", [_leaf("c1", "唯一")]),
        _normal(),
    ]))

    before = [item for item in check(content) if item.level != "info"]
    after = [item for item in repair(content).remaining if item.level != "info"]
    assert len(after) <= len(before)


# ------------------------------------------------- 5.5 压链的标题预算（两行盒子）
#
# 这一节守的是**修复自己造出来的**那一种缺陷：两层标题合成一层时拼成一句长话，
# 画布上是一个两行盒子（`stores/mindmap.ts` 的 `TRUNCATE_CHARS = 24`），超出就截成
# 半句。真产物里量到 13 个（`data/acc_trees`），全部形如
# 「基础：CSS 布局与事件循环清楚 / 浏览器渲染流程回答不完整」（31 字）。
#
# 下面第一条用**真产物的形状**：旧写法在这个输入上会产出 31 字，用例必红。


def _two_line_violation():
    """真产物形状：外层 16 字、内层 12 字，拼起来 31 字。"""
    return _map(_node("r", "候选人 B（前端方向，本科）", [
        _node("base", "基础：CSS 布局与事件循环清楚", [
            _leaf("render", "浏览器渲染流程回答不完整"),
        ]),
        _normal(),
    ]))


def test_collapse_label_stays_inside_two_lines():
    """压链的标题不得超出画布两行能放下的长度。

    旧写法（两段一直拼到契约上限 200）在这个输入上产出
    「基础：CSS 布局与事件循环清楚 / 浏览器渲染流程回答不完整」（31 字），
    画布上被截成「… / 浏览器渲染流程…」——用户看到的是一个半句。
    """
    content = _two_line_violation()

    result = repair(content)
    folded = result.content.root.children[0]
    assert folded.id == "render", "压链保留内层（内容层）"
    assert len(folded.label) <= READABLE_LABEL_CHARS, folded.label
    assert overlong_labels(result.content) == []
    # 契约上限（200）本来就没被违反过，所以"合规"不能拿来当这条的判据。
    assert "SINGLE_CHILD" not in _codes(result.remaining)


def test_collapse_never_loses_the_outer_title():
    """放不下两行时外层标题进 `detail` 的第一行——不丢，只是换了个位置。

    这是"不把两句话合成一句"的代价，必须被钉住：换位置可以，丢字不行。
    """
    content = _two_line_violation()

    folded = repair(content).content.root.children[0]
    assert folded.label == "浏览器渲染流程回答不完整"
    assert folded.detail is not None
    assert folded.detail.startswith(DEFERRED_LABEL_PREFIX + "基础：CSS 布局与事件循环清楚")
    # 两边的原 detail 都还在（降级只在前面加一行，不覆盖）。
    assert "CSS" in folded.detail


def _repeating_collapse():
    """真产物形状（`data/acc_trees/S06.json` 的 `b4-1-1`）：两段在说同一件事。

    外层 10 字、内层 8 字，拼起来 21 字——**两行放得下**，所以旧判据（只比长度）
    会照拼。但这两段说的是同一个事实的两种写法（一个是目标、一个是它的占比），
    拼起来是重复而不是补充。
    """
    return _map(_node("r", "根", [
        _node("a", "经典篇目背诵 20%", [_leaf("b", "背诵成绩占比两成")]),
        _normal(),
    ]))


def test_collapse_does_not_join_two_titles_that_say_the_same_thing():
    """"放得下"不等于"读得懂"：两段互相复述时不拼，走第三档。

    这是"可读"这一维扣分的**单一系统性成因**（验收报告 §6.4 第 3 条）：只有长度
    一个条件时，任何拼得下的父子标题都会被拼上，包括这种同一句话的两种说法。
    """
    content = _repeating_collapse()

    folded = repair(content).content.root.children[0]
    assert folded.label == "背诵成绩占比两成", folded.label
    assert folded.detail is not None
    assert folded.detail.startswith(DEFERRED_LABEL_PREFIX + "经典篇目背诵 20%"), \
        "外层那句没进 detail 就是丢内容"


def test_short_collapse_still_joins_a_dimension_and_its_value():
    """两段讲的是**不同域**（维度名 + 它的取值）时照旧拼接：信息最全，不必降级。

    与上一条成对：判据不是"放得下就拼"（会把复述拼成病句），也不是"必须是量化
    补充"（会把这一类也赶进第三档），而是"两段有没有共享主题词"——共享就是在复述，
    不共享才是补充。`服务器费用 / 300 元/月` 是 `S15/s6-2-1` 的真产物形状。
    """
    content = _map(_node("r", "根", [
        _node("a", "服务器费用", [_leaf("b", "300 元/月")]),
        _normal(),
    ]))

    folded = repair(content).content.root.children[0]
    assert folded.label == f"服务器费用{' / '}300 元/月"
    assert folded.detail is None, "拼得下就不该往 detail 里塞东西"


def test_containment_does_not_double_the_title():
    """包含关系（「监督学习」/「有监督学习」）取长的那个，不拼、也不进 detail。"""
    content = _map(_node("r", "根", [
        _node("a", "监督学习", [_leaf("b", "有监督学习")]),
        _normal(),
    ]))

    folded = repair(content).content.root.children[0]
    assert folded.label == "有监督学习"
    assert folded.detail is None


def test_three_level_chain_keeps_every_outer_title():
    """三层单子链压两次：两次的外层标题都在，label 仍是内层那个。

    数据取自 `data/acc_trees/S02.json`（压出过 33 字的
    「教材与参考资源 / 指定教材 / Silberschatz《操作系」）。
    """
    content = _map(_node("r", "课程资源", [
        _node("books", "教材与参考资源", [
            _node("picked", "指定教材", [
                _leaf("book", "Silberschatz《操作系统概念》"),
            ]),
        ]),
        _normal(),
    ]))

    result = repair(content)
    folded = result.content.root.children[0]
    assert folded.id == "book"
    assert folded.label == "Silberschatz《操作系统概念》"
    assert len(folded.label) <= READABLE_LABEL_CHARS
    assert folded.detail is not None
    # 两个外层标题都没丢：第一次压链拼进 label、第二次压链把它整段降级进 detail。
    assert "教材与参考资源" in folded.detail
    assert "指定教材" in folded.detail


def test_deferring_does_not_stack_on_a_second_run():
    """降级过的树再跑一次不会多出一行 `原上级：`（幂等）。"""
    content = _two_line_violation()

    first = repair(content)
    second = repair(first.content)
    assert second.applied == []
    assert _dump(second.content) == _dump(first.content)
    assert first.content.root.children[0].detail.count(DEFERRED_LABEL_PREFIX) == 1


def test_long_inner_label_is_not_shortened_by_the_collapse():
    """内层本来就超两行：压链不加剧它，也不改用外层（那会把内容藏起来）。

    这一条与"可读"看似冲突，其实不是：`_joined_label` 管的是**压链不许把标题变长**，
    不管"标题本来多长"。旧图（生成侧 16 字上限之前）里 20–30 字的标题必须照原样活着
    ——`stores/mindmap.ts` 的 `displayTitle` 是给它留的兜底。
    """
    inner = "这个节点的内层标题本来就超过两行盒子的长度"
    content = _map(_node("r", "根", [
        _node("a", "外层", [_leaf("b", inner)]),
        _normal(),
    ]))

    folded = repair(content).content.root.children[0]
    assert folded.label == inner
    assert folded.detail is not None and "外层" in folded.detail


def test_collapse_unions_refs_and_keeps_both_details():
    """压链不只是并 label：引用取并集、说明拼接，两边都不丢。

    与 `test_refs_union_*`（合并那条）分开写，因为这是**另一个函数**
    （`_absorb` 与 `_merge_siblings`）——同一条不变量写在两处，只有一处有用例时
    另一处改坏了没人知道。
    """
    content = _map(_node("r", "根", [
        _node("a", "外层", [
            _leaf("b", "内层", source_refs=[_ref("s2", 5, 9)]),
        ], source_refs=[_ref("s1", 0, 4)], detail="外层说明"),
        _normal(),
    ]))

    folded = repair(content).content.root.children[0]
    assert folded.id == "b"
    keys = [(ref.source_id, ref.start, ref.end) for ref in folded.source_refs]
    assert keys == [("s1", 0, 4), ("s2", 5, 9)], "外层引用在前、内层在后，且去重"
    assert "外层说明" in (folded.detail or "")


def test_repair_never_increases_depth():
    """深度只可能变小：压链减层、合并不加层。

    "修完 depth 会超上限"是**查过并否掉**的那一类风险（用户点名要逐条查）：修复
    没有一条动作会新增层级，所以这里钉的是不变量，而不是加一道运行时闸。
    """
    content = _map(_node("r", "根", [
        _node("a", "重复", [_node("a1", "甲层", [_leaf("a1x", "甲孙")])]),
        _node("b", "重复", [_leaf("b1", "乙叶")]),
        _node("c", "单子", [_leaf("c1", "唯一")]),
        _normal(),
    ]))

    before = _depth(content.root)
    after = _depth(repair(content).content.root)
    assert after <= before
    assert repair(content).steps >= 2, "前提：这条数据要真的修过东西"


def _depth(node: Node) -> int:
    return 1 + max((_depth(child) for child in node.children), default=0)


def test_collapse_undo_restores_the_deferred_title():
    """降级过的那一步撤回之后，label 与 detail 双双还原。"""
    content = _two_line_violation()

    result = repair(content)
    assert result.applied, _codes(check(content))
    assert _dump(revert(result.content, result.applied)) == _dump(content)


def test_overlong_labels_matches_the_renderer_limit():
    """口径与渲染层同源：24 字过、25 字报出来，报的是 `(id, label)`。"""
    at_limit = "字" * READABLE_LABEL_CHARS
    content = _map(_node("r", "根", [
        _leaf("ok", at_limit),
        _leaf("bad", at_limit + "字"),
        _normal(),
    ]))

    assert overlong_labels(content) == [("bad", at_limit + "字")]
    # 首尾空白不计长度：与 `displayTitle` 的 `label.trim()` 同一口径。
    padded = _map(_node("r", "根", [_leaf("pad", "  " + at_limit + "  "), _normal()]))
    assert overlong_labels(padded) == []


# ---------------------------------------------------------------- 6. 摘要与序列化


def test_summary_says_what_happened():
    content = _map(_node("r", "根", [_leaf("a", "重复"), _leaf("b", "重复"), _normal()]))
    text = repair_summary(repair(content))
    assert "合并" in text
    # 没有修复时是空串：调用方据此不加那一段。
    assert repair_summary(repair(repair(content).content)) == ""


def test_result_is_json_serialisable():
    """端点要把它当响应体发出去：整份结果必须能过 `json.dumps`。"""
    content = _map(_node("r", "根", [_leaf("a", "重复"), _leaf("b", "重复"), _normal()]))
    payload = repair(content).model_dump(mode="json")
    assert json.loads(json.dumps(payload, ensure_ascii=False))["steps"] >= 1


def test_budget_is_the_node_count_not_a_constant():
    """步数预算 = 节点数（每次修复严格少一个节点）。

    固定成 200 那类常数会让"200 处重名的大图"第一趟修不完，于是同一个输入两次调用
    给出两棵不同的树——真图上量到过（`scripts/measure_repair.py`，561 节点那张）。
    """
    root = _node("r", "根", [_leaf("a", "重复"), _leaf("b", "重复"), _normal()])
    content = _map(root)
    assert repair(content).steps == 1
    # 显式给一个更小的预算时，它照做（并且因此不再是幂等的——调用方自己负责）。
    limited = repair(content, max_steps=0)
    assert limited.steps == 0
    assert _dump(limited.content) == _dump(content)


def test_empty_allowed_codes_do_nothing():
    """`codes=[]` 是"什么都不修"，不是"用默认值"。"""
    content = _map(_node("r", "根", [_leaf("a", "重复"), _leaf("b", "重复"), _normal()]))
    result = repair(content, codes=[])
    assert result.applied == []
    assert _dump(result.content) == _dump(content)


# ---------------------------------------------------------------- 7. 端点


def _client():
    # 门禁（`app/auth/gate.py`）之后，任何 `/api/` 请求都必须先"有身份"；这些用例
    # 关心的是"有身份时这个接口对不对"，身份由 `guest_client()` 补齐（与
    # `test_optimization_rules.py` 同一个写法）。
    from conftest import guest_client

    return guest_client()


def _body(root: dict, **extra) -> dict:
    content = _map(root, **extra)
    return {"content": content.model_dump(mode="json")}


def test_endpoint_repairs_and_hands_back_the_undo():
    """`POST /optimization/repair` 返回修好的树 + 每一步的反操作。"""
    body = _body(_node("r", "根", [_leaf("a", "重复"), _leaf("b", "重复"), _normal()]))
    response = _client().post("/api/optimization/repair", json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["steps"] == 1
    assert payload["summary"]
    assert payload["applied"][0]["code"] == "DUPLICATE_LABEL"
    assert payload["applied"][0]["undo"]["parent_id"] == "r"
    assert [child["id"] for child in payload["content"]["root"]["children"]] == ["a", "normal"]


def test_endpoint_accepts_a_narrowed_scope():
    body = _body(_node("r", "根", [
        _leaf("a", "重复"), _leaf("b", "重复"),
        _node("c", "单子", [_leaf("c1", "唯一")]), _normal(),
    ]))
    payload = _client().post("/api/optimization/repair",
                             json={**body, "codes": ["SINGLE_CHILD"]}).json()
    assert [item["code"] for item in payload["applied"]] == ["SINGLE_CHILD"]


def test_endpoint_rejects_a_code_it_cannot_repair():
    """不认识的码是 422，且把可修的三个码写清楚——静默忽略会让"我选了它却没生效"
    变成查不出来的状态。"""
    body = _body(_node("r", "根", [_leaf("a", "重复"), _leaf("b", "重复"), _normal()]))
    response = _client().post("/api/optimization/repair",
                              json={**body, "codes": ["NEAR_DUPLICATE_LABEL"]})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "NEAR_DUPLICATE_LABEL" in detail
    assert "DUPLICATE_LABEL" in detail


def test_endpoint_is_tree_only_like_the_other_two():
    """鱼骨/流程 422，与 `/check`、`/suggest` 同一条闸。"""
    fish = {"content": {"title": "t", "problem": "p",
                        "bones": [{"category": "人", "root": {"id": "b1", "label": "x"}}]}}
    response = _client().post("/api/optimization/repair", json=fish)
    assert response.status_code == 422
    assert "只支持树型" in response.json()["detail"]


def test_repair_route_stays_out_of_the_openapi_snapshot():
    """这条路由**故意**不在快照里（`include_in_schema=False`）。

    理由在 `optimization/router.py` 那段注释里：`openapi.json` 与 `schema.d.ts` 这一轮
    由另一条线在改，加进 schema 就必须重导那两个文件，而它们是硬约束里点名不许碰的。
    用例把"故意隐藏"钉成契约——下一个人看到它不在快照里时，会先看到这条用例的解释，
    而不是顺手把装饰器参数删掉。
    """
    document = _client().get("/openapi.json").json()
    assert not [path for path in document["paths"] if path.endswith("/optimization/repair")]
    # 另外两条仍然在（别把整个 optimization 路由都藏起来）。
    assert "/api/optimization/check" in document["paths"]
    assert "/api/optimization/suggest" in document["paths"]
