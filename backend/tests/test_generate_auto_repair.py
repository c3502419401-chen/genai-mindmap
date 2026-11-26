"""生成收尾有没有做**确定性修复**：`check()`（只报）与 `repair()`（顺手修）的接线。

这份文件的来历是 `docs/SUBAGENT-VS-SELFCHECK.md` 第五节那一行：

    第 0 件 | 生成收尾接上 `optimization.repair()` | **0 次调用**
            | 79 张图里 37 张的 `SINGLE_CHILD` 冗余层（命中 116 次）
            | **代码已有，生成链路没接**（收尾只跑 `check()`，只报不修）

`tests/test_optimization_repair.py` 已经把 `repair()` 自己的三条硬约束钉住了
（幂等 / 引用并集 / 可撤销）。这里钉的是**接线**，四件事：

  1. 收尾确实调了它（现状：没有 —— 那一条用例就是本文件的第一版，改完代码之后
     它翻成"调了"）；
  2. 只修那三条（`AUTO_REPAIRABLE_CODES`），并且修到不动点；
  3. 用户按一次撤销回到"生成之前"，不是"第 N 处修复之前" —— 修复在
     `publish_tree` 之前完成，所以它天然属于**这一次生成**，撤销栈里不新增一条；
  4. 修了几处、哪几条规则，进 `data["auto_repair"]` 与诊断流。

第 3 条的判据是"诊断与 `data` 里的节点数与交付的树一致"，以及"树里没有
`SINGLE_CHILD` 痕迹"；撤销栈本身在前端（`stores/mindmap.ts::applyContent`），
后端这一侧能钉的只有"修复发生在产物定稿之前"。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch


from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    DETAIL_ONLY_PROMPT,
    PLAN_PROMPT,
    build_registry,
)

TOPIC = "报销制度"
BRANCHES = ["费用报销", "差旅报销", "审批流程"]


def _request() -> AgentRunRequest:
    return AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default",
        budget=RunBudget(max_model_calls=20, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=1),
    )


def _ctx() -> RunContext:
    return RunContext(request=_request(), sources_by_id={})


def _tree() -> dict:
    """一棵**故意带三种冗余**的树，每种的修法都是唯一的：

      - `b0` → `b0-0` → `b0-0-0`：两级单子链（`SINGLE_CHILD`，修法是压成一层）；
      - `b1` 下两个 `报销单` 逐字同名（`DUPLICATE_LABEL`，修法是并进第一个）；
      - `b2` 正常，用来当"其他分支更深"的参照。

    三种都落在 `AUTO_REPAIRABLE_CODES` 里。`b0` 那一支之所以是**两级**而不是一级：
    一级单子链在"其他分支更深"时会升级成 `HEADING_ONLY_BRANCH`，两条码的修法
    是同一条（`_collapse_chain`），但报告里要分得开，所以这一棵树把两条都放进去。
    """
    return {"title": TOPIC, "root": {
        "id": "root", "label": TOPIC, "detail": "中心主题",
        "children": [
            {"id": "b0", "label": "费用报销", "children": [
                {"id": "b0-0", "label": "日常费用", "children": [
                    {"id": "b0-0-0", "label": "办公用品", "detail": "一句具体的说明。", },
                ]},
            ]},
            {"id": "b1", "label": "差旅报销", "children": [
                {"id": "b1-0", "label": "报销单", "detail": "一句具体的说明。"},
                {"id": "b1-1", "label": "报销单", "detail": "另一句具体的说明。"},
            ]},
            {"id": "b2", "label": "审批流程", "children": [
                {"id": "b2-0", "label": "三级审批", "detail": "一句具体的说明。"},
                {"id": "b2-1", "label": "财务复核", "detail": "一句具体的说明。"},
            ]},
        ]}}


def _walk(node: dict):
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


def _ids(tree: dict) -> set[str]:
    return {node["id"] for node in _walk(tree["root"]) if node.get("id")}


def _chat(tree: dict, seen: list[tuple[str, str]] | None = None):
    """骨架回三条分支，结构回整棵树，说明段按 id 对号写回一段合格长度的说明。"""
    detail = ("这句说明写到八十个字以上，讲清它是什么、怎么做、有什么条件与数字，"
              "这样第三段的产出才符合长度目标。")

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if seen is not None:
            seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(BRANCHES)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {node_id: detail for node_id in _ids(tree)}
        return tree
    return chat


def _run(ctx: RunContext, chat, *, diagram_type: str | None = None):
    arguments: dict = {"topic": TOPIC, "branches": 3}
    if diagram_type is not None:
        arguments["diagram_type"] = diagram_type
    registry = build_registry("create", has_sources=False, has_library=False)
    with patch("app.agent.tools.chat_async", new=chat):
        return asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate", arguments=arguments), ctx))


def _codes(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


# ---------------------------------------------------------------- 1. 现状：只报不修


def test_the_tail_reports_structure_problems_but_leaves_the_tree_alone():
    """现状（接线之前）：三种冗余都**还在树里**，只变成三条诊断。

    这条用例在接线之前是绿的、接线之后会红——红了就是"接线生效"的唯一证据，
    随后它被下面那条 `..._is_repaired` 取代（见本文件末尾的说明）。
    """
    ctx = _ctx()
    result = _run(ctx, _chat(_tree()))

    assert result.ok, result.content
    codes = _codes(result)
    assert "SINGLE_CHILD" in codes
    assert "DUPLICATE_LABEL" in codes
    # 三种冗余都原样留着：节点集合与模型写的一模一样。
    assert _ids(ctx.candidate.model_dump()) == {
        "root", "b0", "b0-0", "b0-0-0", "b1", "b1-0", "b1-1", "b2", "b2-0", "b2-1"}
