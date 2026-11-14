"""初次限额与扩展限额：**初次紧、后续松**。

这一轮要消灭的形态是"残缺的大树"：实测 rag 形态下一棵树长到 240 节点 / 7 层，
`max_model_calls` 在第 6/7 层烧光，最后交付的是一棵上面丰满、下面整片留空的树。
用户要的是**完整的中等树**——同样的调用量，只要求 4 层（1+6+18+54=79 个节点）
时每一次调用恰好对应一个父节点，树是完整的。

所以三条边界各管一段：

  - 初次生成：`INITIAL_MAX_NODES = 80` / `INITIAL_MAX_DEPTH = 3`（root 之下，
    即 4 层含根）；
  - 之后的扩展（`node_expand`）：回到契约的 1000 节点 / 8 层；
  - 单次调用：`generation.budget` 的容量，夹住"这一次要几个节点"。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    INITIAL_MAX_DEPTH,
    INITIAL_MAX_NODES,
    PLAN_PROMPT,
    _candidate_slots,
    _trim_slots,
    build_registry,
)
from app.contracts.mindmap import MAX_TREE_DEPTH, MAX_TREE_NODES


def _ctx(*, max_model_calls: int = 40, candidate=None, selected: list[str] | None = None,
         max_output_tokens: int = 32768) -> RunContext:
    request = AgentRunRequest(
        goal="做一张导图", intent="create", selected_ids=selected or [],
        budget=RunBudget(max_model_calls=max_model_calls,
                         max_output_tokens=max_output_tokens),
    )
    return RunContext(request=request, candidate=candidate, sources_by_id={})


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(call_id=f"c-{name}", name=name, arguments=arguments)


def _run(tool: str, arguments: dict, ctx: RunContext):
    with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=_fake)):
        return asyncio.run(build_registry("create").execute(_call(tool, **arguments), ctx))


def _children(*labels: str) -> dict:
    return {"children": [{"label": label, "detail": f"{label}的说明"} for label in labels]}


async def _fake(system: str, user: str, *, max_tokens=None, on_delta=None):
    """模型替身：6 条分支、每支 3 个要点、每个要点 3 个下一层。

    三种 user 形状各对应一次调用：骨架（只有主题）、分支展开（带"本条一级分支"）、
    单节点展开（`node_expand` 的"父节点：…"）。
    """
    if system.startswith(PLAN_PROMPT):
        return {"branches": [f"分支{index}" for index in range(1, 7)]}
    if "本条一级分支：" in user:
        title = user.split("本条一级分支：", 1)[1].split("\n", 1)[0]
        return _children(*[f"{title}·{i}" for i in (1, 2, 3)])
    if "父节点：" in user:
        parent = user.split("父节点：", 1)[1].split("\n", 1)[0]
        return _children(*[f"{parent}·{i}" for i in (1, 2, 3)])
    node = user.split("本节点：", 1)[1].split("\n", 1)[0]
    return _children(*[f"{node}·{i}" for i in (1, 2, 3)])


# ---------------------------------------------------------------- 初次限额


def test_first_generation_stops_at_four_levels_and_eighty_nodes():
    """初次生成的层数与节点数都不超：4 层含根、≤80 个节点。"""
    ctx = _ctx()
    result = _run("mindmap_generate_deep",
                  {"topic": "机器学习", "branches": 6, "points_per_branch": 3,
                   "max_depth": MAX_TREE_DEPTH}, ctx)
    assert result.ok, result.content
    data = result.data
    # `depth_limit` 是 root 之下的层数：3 = 4 层树
    assert data["depth_limit"] == INITIAL_MAX_DEPTH
    assert [item["depth"] for item in data["levels"]] == [0, 1, 2, 3]
    assert data["node_limit"] == INITIAL_MAX_NODES
    assert data["node_count"] <= INITIAL_MAX_NODES
    # 满额 4 层是 1+6+18+54=79；模型每层给 3 个而不是 3 的幂，所以更少
    assert data["node_count"] == 1 + 6 + 18 + 54


def test_first_generation_never_exceeds_the_call_budget_in_the_formula():
    """真实调用数：1 骨架 + 6 + 18 = 25，默认预算 100 装得下。

    这条断言把"预算够不够"变成可验证的：满额 4 层树的每一次展开调用都真的发出去
    了，没有一次因为 `budget_exhausted` 被跳过——`partial` 为假就是证据。
    """
    ctx = _ctx()
    result = _run("mindmap_generate_deep",
                  {"topic": "机器学习", "branches": 6, "points_per_branch": 3},
                  ctx)
    assert result.ok, result.content
    assert result.data["partial"] is False, result.content
    # 25 次内容调用：骨架 1 + 展开 6 个分支 + 展开 18 个二级
    # （最后一层 54 个节点是这 18 次调用写出来的，不需要再展开）
    assert ctx.model_calls_used == 25
    assert ctx.model_calls_used <= RunBudget().max_model_calls


def test_whole_tree_generation_is_clipped_to_the_initial_limit():
    """整树生成没有"层"的概念，至少要守节点总数：超了裁到 80 并说明。

    裁剪而不是判失败：这一次调用已经花掉，前 80 个节点是一棵合法的树；判失败
    就什么都没有，而下一轮大概率还是给这么多。
    """
    # 每个节点最多 50 个子节点（契约），所以铺两层：5 × 24 + 1 = 121 个节点
    wide = {"title": "宽树", "root": {"id": "r", "label": "根", "children": [
        {"id": f"b{group}", "label": f"组{group}", "children": [
            {"id": f"n{group}-{index}", "label": f"节点{group}-{index}", "children": []}
            for index in range(24)
        ]}
        for group in range(5)
    ]}}

    async def wide_model(system, user, *, max_tokens=None, on_delta=None):
        return wide

    ctx = _ctx()
    with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=wide_model)):
        result = asyncio.run(build_registry("create").execute(
            _call("mindmap_generate", topic="宽树"), ctx))

    assert result.ok, result.content
    assert result.data["node_count"] == INITIAL_MAX_NODES
    codes = [item["code"] for item in result.data["diagnostics"]]
    assert "TREE_CLIPPED_TO_INITIAL" in codes
    assert "80" in result.content


# ---------------------------------------------------------------- 扩展限额


def test_node_expand_keeps_the_contract_limits():
    """`node_expand` 退回契约上限：8 层 / 1000 节点，而不是初次的 80 / 4。

    用户点了某个节点要更深，那时该按承载能力给；把初次的 80 也套在扩展上，
    等于"生成了一张 79 节点的图之后就再也长不大"。
    """
    from app.contracts.mindmap import MindMapContent

    # 已经 4 层、79 个节点的图：再展开第 4 层的一个叶子
    leaf = {"id": "leaf", "label": "叶子", "children": []}
    root = {"id": "root", "label": "根", "children": [
        {"id": "b1", "label": "分支", "children": [
            {"id": "p1", "label": "要点", "children": [leaf]},
        ]},
    ]}
    content = MindMapContent.model_validate({"title": "图", "root": root})

    ctx = _ctx(candidate=content, selected=["leaf"])
    ctx.request.intent = "expand"
    registry = build_registry("expand")
    with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=_fake)):
        result = asyncio.run(registry.execute(
            _call("node_expand", node_id="leaf", count=3), ctx))

    assert result.ok, result.content
    # 第 5 层照样能长：限额是契约的 8 层，不是初次的 4 层
    assert result.data["depth"] == 4
    assert result.data["node_limit"] == MAX_TREE_NODES
    assert len(ctx.candidate.root.children[0].children[0].children) == 1
    added = ctx.candidate.root.children[0].children[0].children[0].children
    assert [node.label for node in added] == ["叶子·1", "叶子·2", "叶子·3"]


def test_node_expand_refuses_at_the_eighth_level():
    """第 8 层的节点不能再往下加：契约最多 8 层。"""
    from app.contracts.mindmap import MindMapContent

    node = {"id": "n8", "label": "第八层", "children": []}
    for level in range(7, 0, -1):
        node = {"id": f"n{level}", "label": f"第{level}层", "children": [node]}
    content = MindMapContent.model_validate({"title": "深树", "root": node})

    ctx = _ctx(candidate=content, selected=["n8"])
    ctx.request.intent = "expand"
    with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=_fake)) as mocked:
        result = asyncio.run(build_registry("expand").execute(
            _call("node_expand", node_id="n8", count=2), ctx))

    assert result.ok is False
    assert result.data["code"] == "too_deep"
    assert result.data["depth"] == MAX_TREE_DEPTH
    # 判定在调用模型之前：额度一旦花掉就收不回来
    assert mocked.await_count == 0


def test_node_expand_refuses_when_the_tree_is_full():
    """整图接近 1000 节点时不再发请求：剩不下两个位置就不值一次调用。"""
    from app.contracts.mindmap import MindMapContent

    # 根 + 19 个分支 × 50 个节点 + 1 个只有 48 个节点 = 1 + 950 + 48 = 999
    branches = []
    for index in range(19):
        branches.append({"id": f"b{index}", "label": f"分支{index}", "children": [
            {"id": f"b{index}-{child}", "label": f"要点{child}", "children": []}
            for child in range(49)
        ]})
    branches.append({"id": "b19", "label": "分支19", "children": [
        {"id": f"b19-{child}", "label": f"要点{child}", "children": []}
        for child in range(47)
    ]})
    content = MindMapContent.model_validate(
        {"title": "满树", "root": {"id": "root", "label": "根", "children": branches}})
    total = 1 + sum(1 + len(branch["children"]) for branch in branches)
    assert total == 999

    ctx = _ctx(candidate=content, selected=["b19"])
    ctx.request.intent = "expand"
    with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=_fake)) as mocked:
        result = asyncio.run(build_registry("expand").execute(
            _call("node_expand", node_id="b19", count=3), ctx))

    assert result.ok is False
    assert result.data["code"] == "tree_full"
    assert result.data["node_count"] == 999
    assert mocked.await_count == 0


# ---------------------------------------------------------------- 预览只报被接纳的


def test_preview_quota_matches_the_trimmed_slots():
    """`_trim_slots` 裁掉的候选，预览里一个都不该出现。

    直接对着两个函数比：同样 12 个候选、allowance=4，裁剪后的节点数与"允许上报
    的额度"必须一致——预览与最终树同形这件事，靠的就是这两个数字相等。
    """
    raw = {"children": [{"label": f"候选{index}"} for index in range(12)]}
    slots = _candidate_slots(raw, 12)
    assert len(slots) == 12
    kept, used = _trim_slots(slots, 4)
    assert used == 4
    assert [slot.label for slot in kept] == ["候选0", "候选1", "候选2", "候选3"]


def test_initial_depth_constant_matches_the_four_layer_tree():
    """`INITIAL_MAX_DEPTH = 3` 是 root 之下的层数：4 层含根，差一层就翻倍。"""
    assert INITIAL_MAX_DEPTH == MAX_TREE_DEPTH - 5
    assert INITIAL_MAX_DEPTH + 1 == 4
    assert INITIAL_MAX_NODES == 80
    # 5 层满额就装不下了：1+6+18+54+162 = 241
    assert 1 + 6 + 18 + 54 + 162 > INITIAL_MAX_NODES
