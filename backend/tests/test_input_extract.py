"""输入先读成「意图 + 主题」（`MINDMAP_INPUT_EXTRACT`）——以及它修的那个现状。

两组用例，边界很清楚：

  - `TestBaseline`：**不改任何东西**时，一段话原样走到提示词的 `主题：` 那一行，
    于是根节点的 label 就是那句话。这五条钉的是**机制**（那一行收到的是原话），
    不是模型的自由发挥——真模型的五次采样见 `scripts/measure_input_extract.py`
    与 `docs/INPUT-EXTRACT.md` 的基线表（外层 agent 会把 33 字压到 23 字，但压成
    的仍是一个冒号从句，且"别太技术"这类限定在压缩里掉了）。
  - `TestExtract`：开关打开后的消费点、三个退化（太短 / 碎 / 失败）与"关掉即没有
    这个键"。

为什么这条现状值得钉住：它是**用户能看见**的那一列——根节点是画布中央那个框，
写着一整句话的时候，一眼就知道这张图在讲"这句话"而不是"这个主题"。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.agent import tools as tools_module
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    INPUT_EXTRACT_MIN_CHARS,
    build_registry,
    input_extract_enabled,
)

# 五条真实长输入（①…⑤ 与 `scripts/measure_input_extract.py` 的 `CASES` 同一批）。
LONG_INPUTS = [
    ("①带背景的诉求", "我想了解怎么在制造业团队里推行AI工具，公司300人，担心员工抵触"),
    ("②多问题混合", "帮我梳理一下RAG的召回问题，另外我们的向量库选型也没定，还有成本怎么控"),
    ("③带约束的任务", "下周要给老板讲清楚为什么我们的检索效果差，要有数据支撑，别太技术"),
    ("④几乎不成句", "AI学习 怎么学 路线"),
    ("⑤纯主题（对照）", "机器学习的主要方向"),
]

# 提取那一次的输出。`constraints` 里的两条是原话里真有的（制造业 / 300 人）；
# `intent` 与 `questions` 是这一步新增的东西（原话里只以"我想了解…"的形式隐含）。
EXTRACT_REPLY = {
    "topic": "制造业团队推行 AI 工具",
    "intent": "想规划",
    "questions": ["怎么在制造业团队里推行 AI 工具", "怎么降低一线员工的抵触"],
    "constraints": ["制造业", "300 人"],
}

# 骨架响应的 system 里那一句（`PLAN_PROMPT`）；提取那一次的 system 是
# `INPUT_EXTRACT_PROMPT`。两者靠这一行分开，不靠调用顺序——顺序会随开关变。
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


def _stub(record: list[dict], *, extract=None, extract_raises: BaseException | None = None):
    """三问式桩：提取 / 骨架 / 结构，detail 那一步返回空映射。

    结构那一次的 `root.label` **照抄 `主题：` 那一行**——把"模型完全听话"这个上限
    写死，于是这条断言量的是提示词输入，而不是桩自己的创意。
    """

    def fake(system: str, user: str, **kwargs):
        record.append({"system": system, "user": user,
                       "max_tokens": kwargs.get("max_tokens")})
        if _EXTRACT_MARK in system:
            if extract_raises is not None:
                raise extract_raises
            return extract if extract is not None else EXTRACT_REPLY
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


def _generate(topic: str, ctx: RunContext, stub) -> tuple[object, list[dict]]:
    record: list[dict] = []
    with patch("app.agent.tools.chat_async", new=stub(record)):
        result = asyncio.run(build_registry("create", has_library=False).execute(
            ToolCall(call_id="c-generate", name="mindmap_generate",
                     arguments={"topic": topic}),
            ctx,
        ))
    return result, record


def _calls(record: list[dict], mark: str) -> list[dict]:
    return [item for item in record if mark in item["system"]]


# ---------------------------------------------------------------- 现状


class TestBaseline:
    """开关关着（默认）时：一段话原样进 `主题：`，根节点就是那句话。"""

    @pytest.mark.parametrize("label,goal", LONG_INPUTS)
    def test_raw_text_reaches_the_topic_line_unchanged(self, label, goal, monkeypatch):
        """五条输入逐条：`主题：` 那一行收到的是**原话**，树也就围着这句话长。"""
        monkeypatch.delenv("MINDMAP_INPUT_EXTRACT", raising=False)
        assert input_extract_enabled() is False

        ctx = _ctx()
        result, record = _generate(goal, ctx, _stub)

        assert result.ok, result.content
        assert _topic_line(_calls(record, _PLAN_MARK)[0]["user"]) == goal
        assert _topic_line(_calls(record, "root")[0]["user"]) == goal
        # 根节点 = 那句话本身；但 label 有 16 字上限（`GENERATED_LABEL_MAX_CHARS`），
        # 所以画布上看到的是**被腰斩的那句话**——"…我们的检"。这一条是现状最直接的
        # 证据：不是"图讲得太宽"，是画布中央那一格根本读不通。
        root_label = ctx.candidate.root.label
        assert root_label == goal[:len(root_label)]
        assert len(root_label) <= tools_module.GENERATED_LABEL_MAX_CHARS
        # 关着时不产生提取读数：`data` 的键集与改动之前相同
        assert "input" not in result.data

    def test_no_extra_model_call_when_off(self, monkeypatch):
        """关着时**一次调用都不多发**——开关的成本必须是零。"""
        monkeypatch.delenv("MINDMAP_INPUT_EXTRACT", raising=False)
        _result, record = _generate(LONG_INPUTS[0][1], _ctx(), _stub)
        assert _calls(record, _EXTRACT_MARK) == []


# ---------------------------------------------------------------- 提取


class TestExtract:
    def _on(self, monkeypatch):
        monkeypatch.setenv("MINDMAP_INPUT_EXTRACT", "1")

    def test_topic_replaces_the_topic_line(self, monkeypatch):
        """提取到的 topic 换掉 `主题：` 那一行——这是"意图真的影响产出"的第一处。"""
        self._on(monkeypatch)
        ctx = _ctx()
        result, record = _generate(LONG_INPUTS[0][1], ctx, _stub)

        assert result.ok, result.content
        assert _topic_line(_calls(record, _PLAN_MARK)[0]["user"]) \
            == EXTRACT_REPLY["topic"]
        assert _topic_line(_calls(record, "root")[0]["user"]) == EXTRACT_REPLY["topic"]
        assert ctx.candidate.root.label == EXTRACT_REPLY["topic"]

    def test_intent_questions_and_constraints_reach_the_prompts(self, monkeypatch):
        """三样都进 user 的**一小段**（骨架与结构两次调用都有），且不进主题那一行。"""
        self._on(monkeypatch)
        result, record = _generate(LONG_INPUTS[0][1], _ctx(), _stub)

        for call in (_calls(record, _PLAN_MARK)[0], _calls(record, "root")[0]):
            assert "他要拿这张图做的事：想规划" in call["user"]
            assert "怎么降低一线员工的抵触" in call["user"]
            assert "制造业、300 人" in call["user"]
            # 限制**不在**主题那一行：那一行必须还是一个名词短语，一个限定词都不带
            assert _topic_line(call["user"]) == EXTRACT_REPLY["topic"]

        assert result.data["input"]["source"] == "model"
        assert result.data["input"]["intent"] == "想规划"
        assert result.data["input"]["questions"] == EXTRACT_REPLY["questions"]
        assert result.data["input"]["constraints"] == EXTRACT_REPLY["constraints"]
        assert result.data["input"]["chars"]["original"] == len(LONG_INPUTS[0][1])
        assert result.data["input"]["skip"] == ""
        assert result.data["input"]["seconds"] >= 0

    def test_short_clean_topic_skips_the_call(self, monkeypatch):
        """真主题（9 字）不提取，也不多付一次调用——那是最常见的用法。"""
        self._on(monkeypatch)
        goal = "机器学习的主要方向"
        assert len(goal) < INPUT_EXTRACT_MIN_CHARS
        result, record = _generate(goal, _ctx(), _stub)

        assert result.ok, result.content
        assert _calls(record, _EXTRACT_MARK) == []
        assert _topic_line(_calls(record, _PLAN_MARK)[0]["user"]) == goal
        assert result.data["input"]["skip"] == "short"
        assert result.data["input"]["source"] == "raw"
        assert result.data["input"]["topic"] == goal

    def test_short_but_fragmented_input_is_extracted(self, monkeypatch):
        """④那条 11 字却不成句：长度判据会漏，形状判据（冒号 / 疑问词）要接住。"""
        self._on(monkeypatch)
        goal = "AI 学习路线：怎么学"
        assert len(goal) < INPUT_EXTRACT_MIN_CHARS
        result, record = _generate(goal, _ctx(), _stub)

        assert len(_calls(record, _EXTRACT_MARK)) == 1
        assert result.data["input"]["source"] == "model"
        assert result.data["input"]["skip"] == ""

    def test_parenthesised_purpose_is_extracted(self):
        """③那条的实测漏网：18 字，但括号里写的是用途（"面向老板汇报"）。

        同一句原话两次运行被外层 agent 压成 18 / 21 字——纯长度判据会在 18 那一次上
        放行，而"给老板讲"正是最该被捞出来的那一件事。括号是形状信号。
        """
        assert tools_module._too_short_to_extract("检索效果差的原因分析（面向老板汇报）") is None
        assert tools_module._too_short_to_extract("检索效果差的原因分析") == "short"

    def test_failure_degrades_to_the_raw_text(self, monkeypatch):
        """提取失败不失败整次生成：退回原话，并留一条 warn（开关开了却没生效）。"""
        self._on(monkeypatch)
        goal = LONG_INPUTS[0][1]
        result, record = _generate(goal, _ctx(), lambda r: _stub(
            r, extract_raises=RuntimeError("boom")))

        assert result.ok, result.content
        assert _topic_line(_calls(record, _PLAN_MARK)[0]["user"]) == goal
        assert result.data["input"]["skip"] == "failed"
        codes = [item["code"] for item in result.data["diagnostics"]]
        assert "INPUT_EXTRACT_UNAVAILABLE" in codes

    def test_empty_topic_falls_back(self, monkeypatch):
        """返回的 topic 是空的：不拿半截中心去生成，退回原话。"""
        self._on(monkeypatch)
        goal = LONG_INPUTS[0][1]
        result, _record = _generate(goal, _ctx(), lambda r: _stub(
            r, extract={"topic": "  ", "intent": "想了解"}))

        assert result.ok, result.content
        assert result.data["input"]["skip"] == "no_topic"
        assert result.data["input"]["topic"] == goal

    def test_budget_exhausted_skips_without_spending(self, monkeypatch):
        """额度用完只跳过一次提取，不抛错（`_read_input` 的直接退化路径）。"""
        self._on(monkeypatch)
        ctx = _ctx(max_model_calls=3, used=3)
        shape = asyncio.run(tools_module._read_input(LONG_INPUTS[0][1], ctx))
        assert shape.skip == "budget"
        assert shape.topic == LONG_INPUTS[0][1]
        assert ctx.model_calls_used == 3


# ---------------------------------------------------------------- 归一化


class TestShape:
    """那一次响应的收拾规则：能退的都退，能挡的都挡。"""

    def test_colon_clause_is_cut(self):
        """`"检索效果差：给老板的数据化说明"` → `"检索效果差"`（后半句是用途）。"""
        assert tools_module._clean_topic("检索效果差：给老板的数据化说明") == "检索效果差"

    def test_topic_is_capped(self):
        long_topic = "一" * 100
        assert len(tools_module._clean_topic(long_topic)) \
            == tools_module.INPUT_EXTRACT_MAX_TOPIC_CHARS

    def test_items_accept_list_and_string(self):
        assert tools_module._clean_items(["甲", "乙", "甲"], limit=10, count=5) == ("甲", "乙")
        assert tools_module._clean_items("甲，乙、丙", limit=10, count=5) == ("甲", "乙", "丙")

    def test_items_respect_the_count(self):
        assert tools_module._clean_items(["一", "二", "三", "四"], limit=10, count=2) \
            == ("一", "二")

    def test_non_dict_reply_is_unshaped(self):
        shape = tools_module._shape_from(["不是 dict"], "原文", seconds=0.0)
        assert shape.skip == "unshaped"
        assert shape.topic == "原文"

    def test_empty_intent_block_when_raw(self):
        """没有提取就没有那一段（开关关着时 user 逐字节不变的另一半保证）。"""
        shape = tools_module._InputShape(topic="甲", original="甲", skip="short")
        assert tools_module._intent_block(shape) == ""
