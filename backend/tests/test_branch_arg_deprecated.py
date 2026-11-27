"""`branches` 这个工具参数：**填了不生效**，所以要明说，而且要被钉住。

账本出处是 `docs/BRANCH-CAP.md` §5.1①。一级分支条数改成由内容定（`NO_BRANCH_CAP_RULE`）
之后，`app/` 里已经没有一处读 `GenerateArgs.branches` / `GenerateDeepArgs.branches`
——它从"上限"退化成"一个永远填了没用的参数"，而仓库自己的规矩（`GenerateArgs`
docstring）说后者比没有这个参数更糟。

§5.1① 给两种处置：删字段，或把 `description` 改成实话。这里选了后者，理由写在字段
旁边的注释里（`extra="forbid"` 下删字段会把"噪音"升级成"模型凭旧记忆一传就整次失败"）。
两种处置都不进 `openapi.json`，所以这个选择只关乎故障面，不关乎契约。

这个文件盯两件事：

  1. **对外那两处说明是"废止"，不是"上限"**：schema 里字段还在、描述说清了填了没用。
     谁要把它接上，先得改这两句，改了这条就红——那正是"接上时要跟着改"的提示。
  2. **产出真的不变**：同一组桩响应下，`branches=3` 与 `branches=20` 跑完整条分片
     流水线，回给模型的那句话、`data`、候选树的 `model_dump()` 逐字节相同。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, patch

from app.agent import ops as ops_module
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext
from app.agent.tools import GenerateArgs, GenerateDeepArgs, _generate_deep, build_registry


def _ctx(max_model_calls: int = 12) -> RunContext:
    return RunContext(request=AgentRunRequest(
        goal="做一张图", intent="create",
        budget=RunBudget(max_model_calls=max_model_calls, max_output_tokens=32768)))


def _uuid_sequence():
    return iter([uuid.UUID(int=index) for index in range(1, 5000)])


def _stub(seen: list | None = None):
    """骨架固定三支、每支两个要点。**响应与 `branches` 无关**，这正是被测的前提。

    `seen` 收下每次调用的 `(system, user)`：把它一并比对，是为了让"有人把这个数
    接进**任何一次**模型调用"这条回归真的能红——只比产出的话，桩的响应是固定的，
    提示词变了产出也不变，用例会静默通过。
    """
    def fake(system: str, user: str, **kwargs):
        if seen is not None:
            seen.append((system, user))
        if "只定" in system:
            return {"branches": [
                {"label": "提交申请", "color": "blue", "icon": "📝"},
                {"label": "形式审查", "color": "amber", "icon": "🔍"},
                {"label": "付款", "color": "teal", "icon": "💳"},
            ]}
        return {"children": [{"label": "要点一"}, {"label": "要点二"}]}

    return AsyncMock(side_effect=fake)


def _snapshot(branches: int) -> tuple[str, dict, dict, list]:
    ctx = _ctx()
    args = GenerateDeepArgs(topic="报销", branches=branches, points_per_branch=2,
                            max_depth=2)
    # 序列**在 patch 外建一次**：建在 `lambda` 里的话每次调用都从头开始，
    # 于是所有节点拿到同一个 id（`uuid(int=1)`），流水线会绕着它转。
    seq = _uuid_sequence()
    seen: list = []
    with patch("app.agent.tools.chat_async", new=_stub(seen)), \
            patch.object(ops_module, "uuid4", new=lambda: next(seq)):
        result = asyncio.run(_generate_deep(args, ctx))
    assert result.ok, result.content
    assert ctx.candidate is not None
    return result.content, result.data, ctx.candidate.model_dump(), seen


def _properties(model) -> dict:
    """参数模型暴露给模型看的那个 schema（`ToolSpec.parameters` 同一条路径）。"""
    return model.model_json_schema()["properties"]


def test_both_models_describe_the_argument_as_inert():
    """两处 `description` 都说"废止"，而且**字段还在**（没有偷偷删）。"""
    for model in (GenerateArgs, GenerateDeepArgs):
        properties = _properties(model)
        assert "branches" in properties, (
            f"{model.__name__}.branches 被删了：`extra=\"forbid\"` 下模型凭旧记忆"
            "传它会整次调用校验失败，删之前先确认调用方已经全部不传")
        description = properties["branches"]["description"]
        assert "已废弃" in description, description
        assert "不会改变结果" in description, description
        # 反向：旧口径不能回来。它一旦写回"上限"，说明有人把它接上了，
        # 那时要一起改的是这条用例、`NO_BRANCH_CAP_RULE` 那一段与 §5.1①。
        assert "上限" not in description, description


def test_the_registry_exposes_the_same_wording():
    """注册表是**模型真正看到的**那一份：上面那两句必须原样透到 `properties` 里。"""
    for intent, tool in (("create", "mindmap_generate"),
                         ("create_deep", "mindmap_generate_deep")):
        specs = build_registry(intent, has_library=False).specs()
        by_name = {item["function"]["name"]: item
                   for item in specs if item["type"] == "function"}
        branches = by_name[tool]["function"]["parameters"]["properties"]["branches"]
        assert "已废弃" in branches["description"], tool


def test_the_argument_does_not_change_any_output():
    """`3` 与 `20`：**每一次模型调用的输入**、content、data、整棵候选树全都相同。

    前一半（`seen`）才是这条用例真正会红的地方——它把"这个数没被接进任何一次调用"
    变成可断言的事实；后一半钉住"就算有人绕开提示词去改代码，产出也不能变"。
    """
    content_three, data_three, tree_three, seen_three = _snapshot(3)
    content_twenty, data_twenty, tree_twenty, seen_twenty = _snapshot(20)

    assert len(seen_three) == len(seen_twenty) > 0
    assert seen_three == seen_twenty, "branches 漏进了某一次模型调用的输入"
    assert content_three == content_twenty
    assert json.dumps(data_three, ensure_ascii=False, sort_keys=True) == \
           json.dumps(data_twenty, ensure_ascii=False, sort_keys=True)
    assert json.dumps(tree_three, ensure_ascii=False, sort_keys=True) == \
           json.dumps(tree_twenty, ensure_ascii=False, sort_keys=True)
