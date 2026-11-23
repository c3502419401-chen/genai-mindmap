"""超出可读性上限时**折叠**（而不是裁）：`tools._fold_tree` 的单元用例。

为什么单独一个文件：`_fold_tree` 是纯结构变换（一棵树进、一棵树出），而"折得对不对"有
五个彼此独立的判据——折谁、聚合节点长什么样、引用怎么并、什么时候投降去裁、折完契约还
合不合法。把它们塞进生成路径的用例里，每条都要先造一次模型响应，读的人分不清失败是折叠
错了还是提示词错了。

被这条机制替掉的那个动作（`_clip_tree` 前序砍尾巴）留在最后一条用例里：它仍在，但只剩
"四档折叠都折不动"之后那一个入口。
"""
from __future__ import annotations

from app.agent.tools import (
    FOLD_CAPS,
    INITIAL_MAX_NODES,
    _count_nodes,
    _fold_tree,
)
from app.contracts.mindmap import MindMapContent


def _ref(start: int, quote: str = "原文里连续的一段") -> dict:
    return {"source_id": "s1", "start": start, "end": start + 4, "quote": quote}


def _node(node_id: str, label: str, children=(), refs=()) -> dict:
    return {"id": node_id, "label": label, "source_refs": list(refs),
            "children": list(children),
            # 契约的缺省就是 `unknown`，显式写 `yes` 才能证明聚合节点那一条断言不是白过的
            "expandable": "yes"}


def _content(children: list[dict], *, refs=()) -> MindMapContent:
    return MindMapContent.model_validate({
        "title": "主题",
        "root": _node("root", "主题", children, refs or (_ref(0),)),
        "source_usage": [
            {"source_id": "s1", "kind": "text", "filename": "材料.md", "char_count": 100},
        ],
    })


def _branch(index: int, ups: int = 0) -> dict:
    """第 index 支，带 `ups` 个直接上级子节点（都没有下级）。"""
    return _node(
        f"b{index}", f"支{index}",
        [_node(f"b{index}-{up}", f"要点{index}-{up}", refs=[_ref(index * 100 + up)])
         for up in range(ups)],
        refs=[_ref(index * 100)])


def test_under_the_limit_is_a_noop():
    """没超就一个字节都不动：这条机制只在超限那一次才有发言权。"""
    content = _content([_branch(index) for index in range(3)])
    folded, report = _fold_tree(content, INITIAL_MAX_NODES)

    assert folded is content
    assert report.rollups == () and report.clipped == 0


def test_the_tail_siblings_become_one_rollup_node():
    """根下 10 支、上限 10：后 2 支并成一个「其他 2 项」，前 8 支原样。"""
    content = _content([_branch(index) for index in range(10)])
    folded, report = _fold_tree(content, 10)

    labels = [child.label for child in folded.root.children]
    assert labels[:-1] == [f"支{index}" for index in range(8)]
    assert labels[-1] == "其他 2 项"
    assert _count_nodes(folded.root) == 10
    assert report.rollups == ("其他 2 项",)
    assert report.clipped == 0
    assert report.dropped == 1          # 11 个节点（根 + 10 支）→ 10
    # 聚合节点是叶子：它的下级没有跟着搬进来（搬进来就等于没折）
    assert folded.root.children[-1].children == []


def test_the_rollup_keeps_every_folded_title_in_its_detail():
    """被并的标题一个不落地进 detail：读图的人据此知道"这里原本是哪几个"。"""
    content = _content([_branch(index, ups=1) for index in range(10)])
    folded, _ = _fold_tree(content, 18)          # 只为折一次：21 → 18

    detail = folded.root.children[-1].detail
    assert detail.startswith("含：")
    assert "支8" in detail and "支9" in detail


def test_the_rollup_takes_the_union_of_refs():
    """聚合节点的引用是**整棵子树**的并集，且去重。

    "它概括的那些节点的引用取并集"是这条机制能成立的前提：折叠丢的是层级，不是出处。
    被折的支 8、支 9 各带两条引用（800/801、900/901），另有一条两支共享的（999）——
    聚合节点应当正好五条，且 999 只出现一次。

    `limit=18`（21 → 18）是为了**只折一次**：折两轮时后面那轮的聚合节点会把前一轮的
    聚合节点再并进去，被并的集合就不再是"支 8、支 9"了——那种情形由
    `test_deep_siblings_fold_before_the_top_level_does` 覆盖。
    """
    shared = _ref(999, quote="两支都知道的一句")
    children = [
        _node(f"b{index}", f"支{index}",
              [_node(f"b{index}-0", f"要点{index}", refs=[_ref(index * 100), shared])],
              refs=[_ref(index * 100 + 1)])
        for index in range(10)
    ]
    content = _content(children)
    folded, _ = _fold_tree(content, 18)

    rollup = folded.root.children[-1]
    starts = sorted(ref.start for ref in rollup.source_refs)
    assert starts == [800, 801, 900, 901, 999]
    assert len(starts) == len(set(starts))            # 999 只出现一次（去重）


def test_the_rollup_is_not_marked_as_exhausted():
    """`expandable="unknown"`：这不是"到底了"，是"这里还有内容、只是没画出来"。

    标成 `no` 的后果很具体：界面把这一支的展开按钮灰掉，而那是唯一还能把它长回来的
    入口（`NODE_EXPANDABLE` 的三态就是为这件事分出来的）。
    """
    content = _content([_branch(index, ups=1) for index in range(10)])
    folded, _ = _fold_tree(content, 18)

    assert folded.root.children[0].expandable == "yes"      # 没被折的照旧
    assert folded.root.children[-1].expandable == "unknown"


def test_deep_siblings_fold_before_the_top_level_does():
    """自底向上折：**一级分支一条都不少**，先缩的是每一支的扇出。

    这条是这套机制存在的理由。3 支 × 每支 10 个要点 = 34 个节点折到 20 以内：
    第 1 轮按 cap=8 折掉每支的尾巴、第 2 轮再收一次，最后按 cap=4 折——根下始终是
    那 3 支。换成 `_clip_tree`，同样的树按前序保留前 20 个，第 3 支早就没了。
    """
    content = _content([_branch(index, ups=10) for index in range(3)])
    assert _count_nodes(content.root) == 34

    folded, report = _fold_tree(content, 20)

    assert [child.label for child in folded.root.children] == ["支0", "支1", "支2"]
    assert _count_nodes(folded.root) <= 20
    assert report.clipped == 0
    # 每一支里都有一个聚合节点收着被并的那些要点
    for branch in folded.root.children:
        assert branch.children[-1].label.startswith("其他 ")
        assert branch.expandable != "no"


def test_a_tree_that_is_narrow_everywhere_still_falls_back_to_clipping():
    """每一层都窄到 2 支以内、总量还超 → 这才是"撑不起"，落到 `_clip_tree`。

    四档折叠的意义就在这里：能折的都折了，剩下的超出量不是"并列太多"，是**总量太多**。
    那时报 `clipped`，由调用方记一条 warn——用户看到的是"少了内容"，而不是"收起来了"。
    """
    grandchildren = [
        _node(f"b{index}-{up}-{low}", f"细{index}-{up}-{low}")
        for index, up in [(0, 0), (0, 1), (1, 0), (1, 1)]
        for low in range(2)
    ]
    # root → 2 支 → 每支 2 个要点 → 每个要点 2 个细节 = 1 + 2 + 4 + 8 = 15
    children = [
        _node(f"b{index}", f"支{index}", [
            _node(f"b{index}-{up}", f"要点{index}-{up}", [
                _node(f"b{index}-{up}-{low}", f"细{index}-{up}-{low}")
                for low in range(2)
            ])
            for up in range(2)
        ])
        for index in range(2)
    ]
    content = _content(children)
    assert _count_nodes(content.root) == 1 + 2 + 4 + 8
    assert grandchildren  # 上面那段只是让形状读得出来

    folded, report = _fold_tree(content, 6)

    assert report.clipped > 0
    assert _count_nodes(folded.root) <= 6


def test_every_cap_in_the_ladder_is_tried():
    """四档的顺序是 8 → 4 → 2，宽的那一档先试：它是"丢得最少"的那一档。"""
    assert FOLD_CAPS == (8, 4, 2)
