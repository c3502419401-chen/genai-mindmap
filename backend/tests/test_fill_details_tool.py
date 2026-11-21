"""`mindmap_fill_details`：从检查点续写时**只补说明、不动结构**。

这个工具存在的理由只有一个场景：生成在写完说明之前被打断，库里留下一棵**结构
合法、`detail` 全空**的树。用户把它载回画布之后，唯一缺的是说明，而从前唯一的
补法是重跑一次生成——再花十几秒与三次模型调用，还可能换出一棵不一样的树。

所以这里守四条，每条都对应一种"补错了比不补更糟"的形态：

  1. **不调模型就不该报成功**：说明已经写满时直接 `nothing_to_fill`，`seen` 里
     一次调用都不能多。
  2. **额度用完不改树**：`budget` 时 `ctx.candidate` 逐字节不变、`produced` 仍为假
     ——否则之后某条额度耗尽的路径会把"什么都没发生"包装成一棵半成品交付。
  3. **非树直说**：`_fill_details` 的签名只接 `MindMapContent`，鱼骨/流程给
     `not_tree` 而不是崩。
  4. **`instruction` 真的进了提示词**，且空串时 user 与不传时逐字节相同（生成
     那条路永远不传它，靠这条钉住"没动生成路径"）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    DETAIL_FILL_FLOOR,
    DETAIL_ONLY_PROMPT,
    FillDetailsArgs,
    _fill_details_tool,
    build_registry,
)
from app.contracts.mindmap import FishboneContent, MindMapContent

TOPIC = "报销为什么慢"

LONG = ("这是一段写到八十个字以上的具体说明：它讲清这个要点是什么、怎么做、"
        "在什么条件下成立，并给出可以核对的数字或判断标准，而不是把标题换个说法。")

SHORT = "太短"


def _tree(*, detail: str = "") -> MindMapContent:
    """三支、每支两个要点的小树。`detail` 是结构那一次写的说明（默认留空）。"""
    return MindMapContent.model_validate({"title": TOPIC, "root": {
        "id": "root", "label": TOPIC, "detail": detail, "children": [
            {"id": f"b{index}", "label": f"分支{index}", "detail": detail, "children": [
                {"id": f"b{index}-1", "label": f"要点{index}-1", "detail": detail},
                {"id": f"b{index}-2", "label": f"要点{index}-2", "detail": detail},
            ]}
            for index in range(1, 4)
        ]}},
    )


def _fishbone() -> FishboneContent:
    return FishboneContent.model_validate({
        "title": TOPIC, "problem": TOPIC,
        "bones": [{"category": "人", "root": {
            "id": "h1", "label": "人", "children": [{"id": "h1a", "label": "审批人太多"}]}}],
    })


def _ctx(candidate: MindMapContent | FishboneContent | None, *, calls: int = 10) -> RunContext:
    request = AgentRunRequest(
        goal="补齐说明", intent="edit",
        budget=RunBudget(max_model_calls=calls, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=1),
    )
    return RunContext(request=request, candidate=candidate)


def _chat(seen: list[tuple[str, str]], answer):
    """模型替身。`answer` 可以是 dict，也可以是 `(system, user) -> Any`。"""
    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        seen.append((system, user))
        return answer(system, user) if callable(answer) else answer
    return chat


def _run(ctx: RunContext, chat, instruction: str | None = None):
    """走真实的注册表：这条用例要验的包括"这个工具在 `edit` 里真的可达"。"""
    registry = build_registry("edit", has_library=False)
    with patch("app.agent.tools.chat_async", new=chat):
        return asyncio.run(registry.execute(
            ToolCall(call_id="c-fill", name="mindmap_fill_details",
                     arguments={} if instruction is None else {"instruction": instruction}),
            ctx))


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _detail_ids(root) -> list[str]:
    return [node.id for node in _walk(root) if node.detail]


def _answers(ids) -> dict[str, str]:
    return {node_id: LONG for node_id in ids}


# ---------------------------------------------------------------- 1. 该省的调用要省


def test_a_full_tree_costs_no_model_call():
    """说明都写够了 → 一次模型都不调，也不把"没改"报成一次改动。"""
    seen: list[tuple[str, str]] = []
    ctx = _ctx(_tree(detail=LONG))
    before = ctx.candidate
    result = _run(ctx, _chat(seen, {}))

    assert result.ok, result.content
    assert seen == [], "说明不缺时不该发任何请求"
    assert result.data["detail"]["skipped"] == "nothing_to_fill"
    assert result.data["changes"] == 0
    assert "没有调用模型" in result.content
    # 树一个字都没动：连对象都不该被换掉
    assert ctx.candidate is before
    assert ctx.produced is False, "没补上任何说明时不该置 produced"


def test_the_floor_is_a_length_not_a_hole():
    """判据是长度（`DETAIL_FILL_FLOOR`），不是"空不空"——26 字那种也要补。"""
    assert len(SHORT.strip()) < DETAIL_FILL_FLOOR
    seen: list[tuple[str, str]] = []
    ctx = _ctx(_tree(detail=SHORT))
    result = _run(ctx, _chat(seen, lambda system, user: _answers(_ask_ids(user))))

    assert result.ok, result.content
    assert len(seen) == 1
    assert result.data["detail"]["requested"] > 0
    assert result.data["changes"] > 0


def test_no_budget_leaves_the_tree_untouched():
    """额度用尽 → 报 `budget`、不改树、不置 `produced`。"""
    seen: list[tuple[str, str]] = []
    ctx = _ctx(_tree())
    before = ctx.candidate
    ctx.model_calls_used = ctx.request.budget.max_model_calls  # 额度提前用光
    result = _run(ctx, _chat(seen, {}))

    assert result.ok, result.content
    assert seen == [], "没额度时一次请求都不该发"
    assert result.data["detail"]["skipped"] == "budget"
    assert "结构没动" in result.content
    assert ctx.candidate is before
    assert ctx.produced is False


# ---------------------------------------------------------------- 2. 补上了才叫产出


def test_filling_publishes_the_new_tree():
    """补上了就是内容变了：`publish_tree` 要置 `produced` 并推进 `revision`。"""
    seen: list[tuple[str, str]] = []
    ctx = _ctx(_tree())
    result = _run(ctx, _chat(seen, lambda system, user: _answers(_ask_ids(user))))

    assert result.ok, result.content
    assert len(seen) == 1
    assert system_is_detail_only(seen[0][0])
    assert ctx.produced is True, "补上说明之后收尾的 done 帧才带得回这棵树"
    assert ctx.revision == 1
    assert result.data["changes"] == len(_detail_ids(ctx.candidate.root)) > 0
    # 结构不动：id 顺序与数量都没变
    assert [node.id for node in _walk(ctx.candidate.root)] == \
        [node.id for node in _walk(_tree().root)]


def test_filling_lands_a_checkpoint_too():
    """补完说明要落一次检查点：这一步之后还要等模型收尾，进程没了就全丢。

    PR#23 给生成/展开/编辑三条产出路径都装了检查点，补说明是第四条——它花的
    时间是同一量级（`_fill_details` 实测二十几秒），而且丢掉的恰恰是"这次好不容易
    补上的说明"。
    """
    seen: list[tuple[str, str]] = []
    landed: list = []

    async def checkpoint(content) -> None:
        landed.append(content)

    ctx = _ctx(_tree())
    ctx.checkpoint = checkpoint
    result = _run(ctx, _chat(seen, lambda system, user: _answers(_ask_ids(user))))

    assert result.ok, result.content
    assert len(landed) == 1, "补上说明之后必须落一次检查点"
    # 落下去的就是交付的那一棵：不能是补之前那份，也不能带别的形状
    assert landed[0] is ctx.candidate
    assert _detail_ids(landed[0].root), "检查点里得已经有这次补上的说明"


def test_nothing_filled_lands_no_checkpoint():
    """一个都没补上就不该打点：那一枪会让人以为"上一轮有东西可捡"。

    与 `publish_tree` 同一判据——检查点也是一次"产出"，没产出就打点，等于给
    下一轮的用户留一份和进来时一模一样的"成果"。
    """
    seen: list[tuple[str, str]] = []
    landed: list = []

    async def checkpoint(content) -> None:
        landed.append(content)

    ctx = _ctx(_tree())
    ctx.checkpoint = checkpoint
    result = _run(ctx, _chat(seen, {}))

    assert result.ok, result.content
    assert result.data["changes"] == 0
    assert landed == []


def test_a_failed_call_says_so_and_changes_nothing():
    """模型没返回可用内容 → 说清"这次没有改动任何内容"，且不置 `produced`。"""
    seen: list[tuple[str, str]] = []
    ctx = _ctx(_tree())
    result = _run(ctx, _chat(seen, {}))

    assert result.ok, result.content
    assert len(seen) == 1
    assert result.data["changes"] == 0
    assert "没有改动任何内容" in result.content
    assert ctx.produced is False


# ---------------------------------------------------------------- 3. 非树直说


def test_a_fishbone_is_refused_with_a_sentence():
    """鱼骨/流程的说明不归这里管：给 `not_tree`，不要崩在 `content.root` 上。"""
    seen: list[tuple[str, str]] = []
    ctx = _ctx(_fishbone())
    result = _run(ctx, _chat(seen, {}))

    assert result.ok is False
    assert result.data["code"] == "not_tree"
    assert "只有思维导图" in result.content
    assert seen == []
    assert ctx.produced is False


def test_without_a_tree_it_asks_for_a_generation_first():
    """空画布上调它 = `no_candidate`，与 `node_expand` 同一条规矩。"""
    ctx = _ctx(None)
    result = _run(ctx, _chat([], {}))

    assert result.ok is False
    assert result.data["code"] == "no_candidate"


# ---------------------------------------------------------------- 4. instruction


def test_the_instruction_reaches_the_detail_prompt():
    """`instruction` 要出现在 user 里，且排在自查句**之前**。"""
    seen: list[tuple[str, str]] = []
    ctx = _ctx(_tree())
    result = _run(ctx, _chat(seen, lambda system, user: _answers(_ask_ids(user))),
                  instruction="用面向新人的口吻，每段控制在 100 字以内")

    assert result.ok, result.content
    user = seen[0][1]
    assert "用面向新人的口吻，每段控制在 100 字以内" in user
    assert user.index("用面向新人的口吻") < user.index("写完后自查一遍"), \
        "补充要求放在自查句之后会被读成格式提醒"


def test_no_instruction_keeps_the_prompt_byte_identical():
    """不传 `instruction` 时 user 与加这个参数之前逐字节相同（生成路径不传它）。"""
    with_blank: list[tuple[str, str]] = []
    without: list[tuple[str, str]] = []
    answer = lambda system, user: _answers(_ask_ids(user))  # noqa: E731

    _run(_ctx(_tree()), _chat(with_blank, answer), instruction="   ")
    _run(_ctx(_tree()), _chat(without, answer))

    assert without[0][1] == with_blank[0][1], "纯空白的 instruction 不该改变提示词"
    assert "补充要求" not in without[0][1]


def test_instruction_is_bounded_but_optional():
    """参数模型只有 `instruction`，且给了上限（模型没有别的旋钮可拧）。"""
    assert FillDetailsArgs().instruction is None
    assert FillDetailsArgs(instruction="短一点").instruction == "短一点"
    with pytest.raises(Exception):
        FillDetailsArgs(instruction="字" * 301)
    with pytest.raises(Exception):
        FillDetailsArgs(node_ids=["b1"])  # 要补哪些由代码数，不接受模型指定


# ---------------------------------------------------------------- 辅助


def system_is_detail_only(system: str) -> bool:
    return system.startswith(DETAIL_ONLY_PROMPT)


def _ask_ids(user: str) -> list[str]:
    """user 里点名要写的节点 id（行格式见 `_detail_ask`）。"""
    import re
    return re.findall(r"id=(\S+?)｜", user)


def test_chat_helper_still_looks_like_a_real_exchange():
    """自检：替身拿到的 system 是「只写说明」的那一段，不是骨架那一段。

    这条防的是"用例本来就没走到补说明"——`seen` 非空但内容是别的阶段时，上面那些
    断言会静默变成空转。
    """
    seen: list[tuple[str, str]] = []
    _run(_ctx(_tree()), _chat(seen, lambda system, user: _answers(_ask_ids(user))))
    assert seen and system_is_detail_only(seen[0][0])
    assert f"共 {len(_ask_ids(seen[0][1]))} 个节点需要写说明" in seen[0][1]


def test_patch_of_chat_async_is_the_only_exit(monkeypatch):
    """`_fill_details_tool` 的模型出口仍然只有 `_chat` 一条（不绕过它自己发请求）。"""
    calls: list[str] = []

    async def fake(system: str, user: str, **kwargs) -> dict:
        calls.append(system)
        return _answers(_ask_ids(user))

    with patch("app.agent.tools.chat_async", new=fake):
        ctx = _ctx(_tree())
        result = asyncio.run(_fill_details_tool(FillDetailsArgs(), ctx))

    assert result.ok, result.content
    assert len(calls) == 1 and calls[0].startswith(DETAIL_ONLY_PROMPT)