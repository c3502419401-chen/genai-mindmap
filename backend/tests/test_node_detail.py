"""节点两层：短标题（`label`）+ 长详情（`detail`）。

这一轮要解决的不是"画布放不下"，而是**生成时没有把长内容写到该去的地方**：
`Node` 契约里早就有 `note` 与 `source_refs`，画布只是把长 `label` 截断显示。所以
缺的两件事是：

  1. 生成侧把 20–30 字的完整句子塞进 `label`，没有一个字段承载"说明"；
  2. 画布与侧栏都不认识"这是详情"，只能回退到标题。

`detail` 与 `note` 的**语义必须分开**（这也是不复用 `note` 的唯一理由）：

  - `detail`：生成产出。整树生成、分片展开、单节点展开都会填它；
  - `note`：用户自己写的备注。只有界面上的编辑框会动它。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.ops import apply_ops
from app.contracts.mindmap import (
    GENERATED_LABEL_MAX_CHARS,
    MAX_NODE_DETAIL_CHARS,
    MAX_NODE_LABEL_CHARS,
    MindMapContent,
    Node,
    clip_label,
)
from app.contracts.ops import OpBatch


def _content(**node_extra) -> dict:
    return {
        "title": "测试",
        "root": {
            "id": "r", "label": "根", "children": [], **node_extra,
        },
    }


# ---------------------------------------------------------------- 1. 契约


def test_detail_is_optional_and_defaults_to_none():
    """旧图没有这个字段：读进来必须是 `None`，而不是空串或异常。"""
    parsed = MindMapContent.model_validate(_content())
    assert parsed.root.detail is None
    assert parsed.root.note is None


def test_detail_is_bounded_and_kept():
    parsed = MindMapContent.model_validate(_content(detail="说明" * 1000))
    assert len(parsed.root.detail) == MAX_NODE_DETAIL_CHARS
    with pytest.raises(ValidationError):
        MindMapContent.model_validate(_content(detail="说明" * 1001))


def test_blank_detail_becomes_none():
    """纯空白等于没写：留空串会让前端多一个"有正文但显示为空"的分支。"""
    parsed = MindMapContent.model_validate(_content(detail="  \n "))
    assert parsed.root.detail is None


def test_detail_and_note_are_independent():
    """两个字段互不覆盖——混用会让用户的备注被重新生成盖掉。"""
    parsed = MindMapContent.model_validate(
        _content(detail="生成写的说明", note="我自己写的备注"))
    assert parsed.root.detail == "生成写的说明"
    assert parsed.root.note == "我自己写的备注"
    # 只有 detail 时 note 仍是 None：生成侧不许借道 note
    only_detail = MindMapContent.model_validate(_content(detail="只有详情"))
    assert only_detail.root.note is None


def test_old_long_label_is_still_readable():
    """旧图的长标题照常读：契约上限仍是 200，只有生成侧受 16 字约束。"""
    long_label = "字" * MAX_NODE_LABEL_CHARS
    parsed = MindMapContent.model_validate(_content(label=long_label))
    assert len(parsed.root.label) == MAX_NODE_LABEL_CHARS
    with pytest.raises(ValidationError):
        MindMapContent.model_validate(_content(label="字" * (MAX_NODE_LABEL_CHARS + 1)))


def test_generated_label_limit_is_shorter_than_the_contract():
    """16 是生成口径，200 是读取口径——两个数必须都在，且前者更小。"""
    assert GENERATED_LABEL_MAX_CHARS == 16
    assert MAX_NODE_LABEL_CHARS == 200


def test_clip_label_counts_code_points():
    """按码点截断：emoji 不该被切成半个，也不该被算成两个字。"""
    assert clip_label("一二三四五六七八九十一二三四五六七八") == "一二三四五六七八九十一二三四五六"
    assert len(clip_label("字" * 100)) == GENERATED_LABEL_MAX_CHARS
    # 单个 emoji 就是一个码点，两个不会被切成半个
    assert clip_label("🚀🚀🚀", 2) == "🚀🚀"
    # 码点口径**不做字形簇切分**：ZWJ 组合（🧑‍🚀 = 4 个码点）会被切开。这是
    # 刻意的取舍——做字形簇切分要引 ICU 的分段器，而前端的 `Array.from` 也是
    # 码点口径，两端用不同的切法只会让"同一个标题在两端被截得不一样"。
    assert len(clip_label("🧑‍🚀" * 5, 4)) == 4
    assert clip_label("短标题") == "短标题"
    assert clip_label("") == ""


# ---------------------------------------------------------------- 2. ops 携带 detail


def _batch(*ops) -> OpBatch:
    return OpBatch(batch_id="b", base_revision=0, ops=list(ops))


def test_add_op_carries_detail():
    content = MindMapContent.model_validate(_content())
    result = apply_ops(content, _batch({
        "op": "add", "id": "n1", "parent_id": "r", "index": 0,
        "label": "新节点", "detail": "这是它的说明",
    }))
    assert result.root.children[0].detail == "这是它的说明"
    assert result.root.children[0].note is None


def test_update_op_can_set_and_clear_detail():
    content = MindMapContent.model_validate({
        "title": "t",
        "root": {"id": "r", "label": "根", "children": [
            {"id": "a", "label": "甲", "detail": "旧说明", "note": "用户备注", "children": []},
        ]},
    })
    updated = apply_ops(content, _batch({
        "op": "update", "id": "a", "changes": {"detail": "新说明"},
    }))
    assert updated.root.children[0].detail == "新说明"
    # 更新 detail 不碰 note
    assert updated.root.children[0].note == "用户备注"

    cleared = apply_ops(updated, _batch({
        "op": "update", "id": "a", "changes": {"detail": ""},
    }))
    assert cleared.root.children[0].detail is None


def test_update_can_change_only_detail():
    """`at_least_one` 要把 detail 算进去，否则"只改详情"会被判成空 changes。"""
    content = MindMapContent.model_validate(_content())
    result = apply_ops(content, _batch({
        "op": "update", "id": "r", "changes": {"detail": "根也有说明"},
    }))
    assert result.root.detail == "根也有说明"


def test_regenerating_never_touches_the_user_note():
    """生成路径（add）写 detail、不写 note：用户的备注必须活过重新生成。"""
    content = MindMapContent.model_validate({
        "title": "t",
        "root": {"id": "r", "label": "根", "children": [
            {"id": "keep", "label": "老节点", "note": "我写的备注", "children": []},
        ]},
    })
    result = apply_ops(content, _batch(
        {"op": "add", "id": "fresh", "parent_id": "r", "index": 1,
         "label": "新节点", "detail": "新说明"},
        {"op": "update", "id": "keep", "changes": {"label": "老节点（改名）",
                                                   "detail": "补了一段说明"}},
    ))
    kept = next(node for node in result.root.children if node.id == "keep")
    assert kept.note == "我写的备注"
    assert kept.detail == "补了一段说明"


# ---------------------------------------------------------------- 3. 生成路径的长度


def test_deep_generation_labels_are_short_and_carry_detail():
    """分片生成：模型给的长标题被裁到 16 字，长内容落进 detail。"""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
    from app.agent.tools import build_registry

    long_label = "把预算审批流程拆成申请与复核两段并写清责任"

    async def fake(system, user, *, max_tokens=None, on_delta=None):
        if "只定一级骨架" in system:
            return {"branches": [f"{long_label}{index}" for index in range(3)]}
        return {"children": [{"label": long_label, "detail": "3 个工作日内完成复核"}]}

    request = AgentRunRequest(
        goal="做一张导图", intent="create",
        budget=RunBudget(max_model_calls=10, max_output_tokens=32768),
    )
    ctx = RunContext(request=request, sources_by_id={})
    with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
        result = asyncio.run(build_registry("create").execute(
            ToolCall(call_id="c", name="mindmap_generate_deep",
                     arguments={"topic": "预算管理办法", "branches": 3,
                                "points_per_branch": 2}),
            ctx,
        ))

    assert result.ok, result.content
    root = ctx.candidate.root
    assert len(root.label) <= GENERATED_LABEL_MAX_CHARS
    branch = root.children[0]
    assert len(branch.label) <= GENERATED_LABEL_MAX_CHARS
    point = branch.children[0]
    assert len(point.label) <= GENERATED_LABEL_MAX_CHARS
    # detail 原样落库——这正是"画布上短、点开看长文"的另一半
    assert point.detail == "3 个工作日内完成复核"


def test_single_node_expand_accepts_both_shapes():
    """`node_expand` 的新旧两种响应都要收：旧格式只有字符串，没有 detail。"""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
    from app.agent.tools import build_registry

    content = MindMapContent.model_validate({
        "title": "t", "root": {"id": "r", "label": "根", "children": [
            {"id": "a", "label": "甲", "children": []}]},
    })

    async def new_shape(system, user, *, max_tokens=None):
        return {"children": [{"label": "要点的标题很长需要被裁掉一半以上才能放进画布",
                              "detail": "这里是说明"}]}

    request = AgentRunRequest(
        goal="展开", intent="expand", selected_ids=["a"],
        budget=RunBudget(max_model_calls=5, max_output_tokens=32768),
    )
    ctx = RunContext(request=request, candidate=content, sources_by_id={})
    with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=new_shape)):
        result = asyncio.run(build_registry("expand").execute(
            ToolCall(call_id="c", name="node_expand",
                     arguments={"node_id": "a", "count": 2}),
            ctx,
        ))
    assert result.ok, result.content
    added = ctx.candidate.root.children[0].children[0]
    assert len(added.label) <= GENERATED_LABEL_MAX_CHARS
    assert added.detail == "这里是说明"

    async def old_shape(system, user, *, max_tokens=None):
        return {"points": ["旧格式的要点一"]}

    legacy_ctx = RunContext(request=request, candidate=content.model_copy(deep=True),
                            sources_by_id={})
    with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=old_shape)):
        legacy = asyncio.run(build_registry("expand").execute(
            ToolCall(call_id="c2", name="node_expand",
                     arguments={"node_id": "a", "count": 2}),
            legacy_ctx,
        ))
    assert legacy.ok, legacy.content
    node = legacy_ctx.candidate.root.children[0].children[0]
    assert node.label == "旧格式的要点一"
    assert node.detail is None


def test_root_label_is_clipped_too():
    """用户填的主题经常是整句指令，画在中心会撑满一屏。"""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
    from app.agent.tools import build_registry

    async def fake(system, user, *, max_tokens=None, on_delta=None):
        if "只定一级骨架" in system:
            return {"branches": ["一", "二", "三"]}
        return {"children": [{"label": "点一"}, {"label": "点二"}]}

    request = AgentRunRequest(
        goal="整理材料", intent="create",
        budget=RunBudget(max_model_calls=10, max_output_tokens=32768),
    )
    ctx = RunContext(request=request, sources_by_id={})
    with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
        result = asyncio.run(build_registry("create").execute(
            ToolCall(call_id="c", name="mindmap_generate_deep",
                     arguments={"topic": "帮我整理这份预算管理办法的全部要点",
                                "branches": 3, "points_per_branch": 2}),
            ctx))
    assert result.ok, result.content
    assert len(ctx.candidate.root.label) <= GENERATED_LABEL_MAX_CHARS
    assert ctx.candidate.title == ctx.candidate.root.label


def test_whole_tree_generation_clips_labels_too():
    """整树生成（`mindmap_generate`）的 label 同样被裁到 16 字。

    分片那条路在 `_slot_from` 里裁，整树这条在 `validate_generated_content` 里裁——
    两条路各裁一次、共用同一个契约常量。少裁任何一条，画布上就有一半的节点又变回
    "一句话标题"。
    """
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
    from app.agent.tools import build_registry

    long_label = "关于预算审批流程中申请与复核两段的责任划分说明"

    async def fake(system, user, *, max_tokens=None, on_delta=None):
        return {"title": "预算", "root": {"id": "r", "label": long_label, "children": [
            {"id": f"b{index}", "label": f"{long_label}{index}", "children": []}
            for index in range(3)
        ]}}

    request = AgentRunRequest(
        goal="做一张导图", intent="create",
        budget=RunBudget(max_model_calls=5, max_output_tokens=32768),
    )
    ctx = RunContext(request=request, sources_by_id={})
    with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
        result = asyncio.run(build_registry("create").execute(
            ToolCall(call_id="c", name="mindmap_generate", arguments={"topic": "预算"}),
            ctx,
        ))
    assert result.ok, result.content
    root = ctx.candidate.root
    assert len(root.label) <= GENERATED_LABEL_MAX_CHARS
    for branch in root.children:
        assert len(branch.label) <= GENERATED_LABEL_MAX_CHARS
    codes = [item["code"] for item in result.data["diagnostics"]]
    assert "LABEL_CLIPPED_TO_GENERATED" in codes


def test_node_model_rejects_unknown_fields():
    """`extra="forbid"` 仍是契约的一部分：`detail` 是显式字段，不是兜底口袋。"""
    with pytest.raises(ValidationError):
        Node.model_validate({"id": "x", "label": "标题", "details": "拼错的字段名"})
