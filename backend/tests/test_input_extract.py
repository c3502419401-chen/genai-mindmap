"""现状：一段话原样进 `主题：`，根节点就是那句话（`MINDMAP_INPUT_EXTRACT` 之前的行为）。

这一份用例只有一个目的——**把用户能看见的那一列钉住**：`args.topic` 是一段话时，
提示词的 `主题：` 那一行就是这段话，根节点 label 就是它的前 16 字
（`GENERATED_LABEL_MAX_CHARS`）。

它量的是**机制**，不是模型的自由发挥：桩把 `root.label` 照抄 `主题：` 那一行，于是断言
"进提示词的主题 == 原话"就等于断言"根节点是那句话"。

真模型的五次采样见 `scripts/measure_input_extract.py --arm off`：外层 agent 会先压一遍
（33 字 → 23 字），压出来的仍是一个冒号从句；`docs/INPUT-EXTRACT.md` 第 0 节有那张表。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.agent import tools as tools_module
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import build_registry

# 五条真实长输入（①…⑤ 与 `scripts/measure_input_extract.py` 的 `CASES` 同一批）。
LONG_INPUTS = [
    ("①带背景的诉求", "我想了解怎么在制造业团队里推行AI工具，公司300人，担心员工抵触"),
    ("②多问题混合", "帮我梳理一下RAG的召回问题，另外我们的向量库选型也没定，还有成本怎么控"),
    ("③带约束的任务", "下周要给老板讲清楚为什么我们的检索效果差，要有数据支撑，别太技术"),
    ("④几乎不成句", "AI学习 怎么学 路线"),
    ("⑤纯主题（对照）", "机器学习的主要方向"),
]

# 骨架响应的 system 里那一句（`PLAN_PROMPT`）。
_PLAN_MARK = "只定一级骨架"
_EXTRACT_MARK = "你在把用户的一段话读成"
_DETAIL_MARK = "每一段说明写"


def _ctx(max_model_calls: int = 12, *, used: int = 0) -> RunContext:
    request = AgentRunRequest(
        goal=LONG_INPUTS[0][1], intent="create",
        budget=RunBudget(max_model_calls=max_model_calls, max_output_tokens=32768),
    )
    ctx = RunContext(request=request)
    ctx.model_calls_used = used
    return ctx


def _topic_line(user: str) -> str:
    """user 里 `主题：` 那一行的实际内容——这就是"进提示词的主题"。"""
    after = user.split("主题：", 1)[1]
    return after.split("\n", 1)[0].strip()


def _stub(record: list[dict]):
    """三问式桩：骨架 / 结构 / detail。结构那一次的 `root.label` 照抄 `主题：` 那一行。"""

    def fake(system: str, user: str, **kwargs):
        record.append({"system": system, "user": user,
                       "max_tokens": kwargs.get("max_tokens")})
        if _EXTRACT_MARK in system:
            raise AssertionError("开关默认关时不该有这一问")
        if _PLAN_MARK in system:
            return {"branches": [{"label": "分支甲"}, {"label": "分支乙"},
                                 {"label": "分支丙"}]}
        if _DETAIL_MARK in system:
            return {}
        label = _topic_line(user)
        return {
            "title": label,
            "root": {"id": "root", "label": label, "children": [
                {"id": "b1", "label": "分支甲",
                 "children": [{"id": "n1", "label": "要点一"}]},
                {"id": "b2", "label": "分支乙",
                 "children": [{"id": "n2", "label": "要点二"}]},
                {"id": "b3", "label": "分支丙",
                 "children": [{"id": "n3", "label": "要点三"}]},
            ]},
        }

    return AsyncMock(side_effect=fake)


def _generate(topic: str, ctx: RunContext) -> tuple[object, list[dict]]:
    record: list[dict] = []
    with patch("app.agent.tools.chat_async", new=_stub(record)):
        result = asyncio.run(build_registry("create", has_library=False).execute(
            ToolCall(call_id="c-generate", name="mindmap_generate",
                     arguments={"topic": topic}),
            ctx,
        ))
    return result, record


def _calls(record: list[dict], mark: str) -> list[dict]:
    return [item for item in record if mark in item["system"]]


class TestBaseline:
    """默认（没有任何开关）时：一段话原样进 `主题：`，根节点就是那句话。"""

    @pytest.mark.parametrize("label,goal", LONG_INPUTS)
    def test_raw_text_reaches_the_topic_line_unchanged(self, label, goal):
        """五条输入逐条：`主题：` 那一行收到的是**原话**，树也就围着这句话长。"""
        ctx = _ctx()
        result, record = _generate(goal, ctx)

        assert result.ok, result.content
        assert _topic_line(_calls(record, _PLAN_MARK)[0]["user"]) == goal
        assert _topic_line(_calls(record, "root")[0]["user"]) == goal
        # 根节点 = 那句话本身；但 label 有 16 字上限（`GENERATED_LABEL_MAX_CHARS`），
        # 所以画布上看到的是**被腰斩的那句话**——"…我们的检"。这一条是现状最直接的
        # 证据：不是"图讲得太宽"，是画布中央那一格根本读不通。
        root_label = ctx.candidate.root.label
        assert root_label == goal[:len(root_label)]
        assert len(root_label) <= tools_module.GENERATED_LABEL_MAX_CHARS
        # 没有提取读数（这个键是 `MINDMAP_INPUT_EXTRACT` 之后才有的）
        assert "input" not in result.data

    def test_no_extra_model_call(self):
        """不提取就**一次调用都不多发**：这条路的成本必须是零。"""
        _result, record = _generate(LONG_INPUTS[0][1], _ctx())
        assert _calls(record, _EXTRACT_MARK) == []
