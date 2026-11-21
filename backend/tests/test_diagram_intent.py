"""从 `goal` 的文字里认出图型，并把那个词从 `goal` 里剥掉。

修的那条 bug 有真实数据：库里 06:02 的 run，`goal = "机器学习，用鱼骨图"`，
`request.diagram_type` 是 `None`（用户是**打字**说的，没动下拉），模型看不见界面，
就按工具参数的默认值生成了 `layout = tree`，还把那三个字当成主题的修饰语——根节点
叫「机器学习（鱼骨图分析）」。

两件事缺一不可：

  1. **识别**（`diagram_intent.infer`）：认出 `fishbone` / `flowchart` / `tree`；
  2. **剥离**（同一个函数）：把那个词与它紧邻的祈使动词、分隔符一起从 `goal` 里
     拿走——只识别不剥离，`goal` 原样进提示词，根标签就还是那句被污染的主题。

第二组用例专门打"流程"这个词的误判风险：它在中文里太常见，`"优化退货流程"` 讲的是
**主题本身**，不是要求画流程图。判据与正反例都在下面，与
`app/agent/diagram_intent.py` 里的 `_FLOW_IMPERATIVE` 一一对应。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.agent import diagram_intent, runs
from app.agent import router as agent_router
from app.agent.contracts import (
    AgentRunRequest,
    DoneEvent,
    RunBudget,
    RunContext,
    ToolCall,
)
from app.agent.diagram_intent import SOURCE_INFERRED, SOURCE_MODEL, SOURCE_REQUEST
from app.agent.tools import (
    FISHBONE_PLAN_PROMPT,
    FLOW_PLAN_PROMPT,
    PLAN_PROMPT,
    build_registry,
)
from app.contracts.mindmap import MindMapContent
from app.export.service import to_puml


# ---------------------------------------------------------------- 1. 识别 + 剥离


# 正例：`(goal, 图型, 剥完的 goal, 被剥掉的那一段)`。
#
# 每一条都是一个真实说法，不是为了让正则好看才凑的：
#   - 第一行就是库里那条 run 的原话；
#   - `用户注册流程图` 是"整词"这一支；
#   - `请用鱼骨图分析…` 验证祈使动词被一起带走（剥完留 `分析…`，不是 `请分析`）；
#   - `机器学习（用鱼骨图）` 验证括号被一起带走；
#   - `画成流程` 系列是"祈使搭配"那一支（没有"图"字）。
POSITIVE = [
    ("机器学习，用鱼骨图", "fishbone", "机器学习", "用鱼骨图"),
    ("用户注册流程图", "flowchart", "用户注册", "流程图"),
    ("机器学习（用鱼骨图）", "fishbone", "机器学习", "用鱼骨图"),
    ("请用鱼骨图分析机器学习的失败原因", "fishbone", "分析机器学习的失败原因", "请用鱼骨图"),
    ("用流程图说明退货怎么走", "flowchart", "说明退货怎么走", "用流程图"),
    ("把退货做成流程", "flowchart", "把退货做成流程", None),  # 剥完为空 → 保留原文
    ("画成流程", "flowchart", "画成流程", None),
    ("用流程展示退货", "flowchart", "展示退货", "用流程"),
    ("做成一张流程图", "flowchart", "做成一张流程图", None),
    ("用脑图整理竞品", "tree", "整理竞品", "用脑图"),
    ("树状图：机器学习", "tree", "机器学习", "树状图"),
    ("思维导图", "tree", "思维导图", None),
]

# 反例：**一个都不能识别**。第一行是"流程"这个词真正的风险面——它讲的是主题。
NEGATIVE = [
    "优化退货流程",
    "退货流程的痛点",
    "流程优化",
    "采购流程设计",
    "机器学习的主要方向",
    "把这张图拆细一点",
]


@pytest.mark.parametrize(("goal", "expected_type", "expected_goal", "expected_stripped"),
                         POSITIVE)
def test_recognizes_and_strips(goal, expected_type, expected_goal, expected_stripped):
    hit = diagram_intent.infer(goal)
    assert hit is not None, goal
    assert hit.diagram_type == expected_type
    assert hit.goal == expected_goal
    # `stripped` 是**真的从原文里移走的那一段**；剥空而保留原文时它是 None
    # （不能一边留着"用鱼骨图"一边声称把它剥掉了）。
    assert hit.stripped == expected_stripped


@pytest.mark.parametrize("goal", NEGATIVE)
def test_plain_process_talk_is_not_a_flowchart(goal):
    """"流程"单独出现、又不是祈使的宾语时，**不认**。

    这一组是判据的反面：`"优化退货流程"` 的主题就是"退货流程"，把它判成
    `flowchart` 会让一张本该按话题分解的图被塞进"阶段 + 是/否"的骨架里，用户
    看到的东西与他要的正相反。判据只看一件事：`流程` 是不是**祈使动词的宾语**。
    """
    assert diagram_intent.infer(goal) is None


def test_negative_control_is_the_string_from_the_bug_report():
    """反例第一行必须真的是那句实证里的话——用例守护的正是它。"""
    assert NEGATIVE[0] == "优化退货流程"
    assert diagram_intent.infer("优化退货流程") is None


def test_stripping_to_nothing_keeps_the_original_text():
    """剥完为空 → 保留原文（宁可让它当主题，也不能给模型一个空主题）。

    `"用鱼骨图"` 单独一句时，那三个字是这句话里唯一的实词。剥掉它 `goal` 就空了，
    而 `AgentRunRequest.goal` 的 `min_length=1` 会在开流前 422——用户拿到的是一次
    报错，不是一张图。所以这条路的产物是"图型认对了、`goal` 一个字没动"。
    """
    hit = diagram_intent.infer("用鱼骨图")
    assert hit is not None
    assert hit.diagram_type == "fishbone"
    assert hit.goal == "用鱼骨图"
    assert hit.stripped is None


def test_polite_prefix_alone_is_not_a_topic():
    """`"帮我用鱼骨图"` 剥完只剩 `"帮我"`——那是客气话，不是主题。

    判据落在"剥完还剩什么"上，而不是"剥掉了多少"：剩下的是虚词时与剩下空串等价，
    同样保留原文。
    """
    hit = diagram_intent.infer("帮我用鱼骨图")
    assert hit is not None
    assert hit.goal == "帮我用鱼骨图"
    assert hit.stripped is None


def test_first_word_wins_and_negation_is_out_of_scope():
    """一句话里出现两个图型词时，**先说的那个**赢。

    这是词面匹配的边界，写成用例是为了别让人以为否定也处理了：`"不要流程图，
    画成树状图"` 会判成 `flowchart`。要处理否定得做句法分析，而这条路的收益
    （省一次下拉）撑不起那个成本——用户在界面上下拉里选一次是确定性的动作。
    """
    hit = diagram_intent.infer("不要流程图，画成树状图")
    assert hit is not None
    assert hit.diagram_type == "flowchart"


THE_GENRE_ITSELF = [
    # 主题就是"鱼骨图 / 流程图"这个工具本身。剥掉那三个字会剩下 `"的使用方法"`、
    # `"与思维导图的区别"` 这种半截话——图型照样认（那是确定的），但 `goal` 一个字
    # 都不能动：宁可让那个词当主题，也不能给模型一个语法不成立的句子。
    ("鱼骨图的使用方法", "fishbone"),
    ("流程图与思维导图的区别", "flowchart"),
]


@pytest.mark.parametrize(("goal", "expected_type"), THE_GENRE_ITSELF)
def test_when_the_topic_is_the_diagram_itself_nothing_is_stripped(goal, expected_type):
    hit = diagram_intent.infer(goal)
    assert hit is not None
    assert hit.diagram_type == expected_type
    assert hit.goal == goal
    assert hit.stripped is None


# ---------------------------------------------------------------- 2. 请求层的语义


def _request(goal: str, *, diagram_type: str | None = None,
             intent: str = "create") -> AgentRunRequest:
    return AgentRunRequest(
        goal=goal, intent=intent, diagram_type=diagram_type,  # type: ignore[arg-type]
        budget=RunBudget(max_model_calls=12, max_output_tokens=32768),
    )


class TestApplyOnTheRequest:
    def test_request_value_is_never_overwritten(self):
        """请求已经指定图型时**不覆盖**，也不剥 `goal`。

        用户选了下拉、又在句子里重复一遍图型词时，那个词是他对自己主题的描述
        （"用流程图说明报销"），不是需要清理的噪声——`source` 记 `request`。
        """
        request = _request("用流程图说明报销", diagram_type="tree")
        intent = diagram_intent.apply(request)

        assert intent == diagram_intent.DiagramIntent("tree", SOURCE_REQUEST, None)
        assert request.diagram_type == "tree"
        assert request.goal == "用流程图说明报销"

    def test_inferred_value_lands_on_the_request(self):
        request = _request("机器学习，用鱼骨图")
        intent = diagram_intent.apply(request)

        assert intent.diagram_type == "fishbone"
        assert intent.source == SOURCE_INFERRED
        assert intent.stripped == "用鱼骨图"
        assert request.diagram_type == "fishbone"
        assert request.goal == "机器学习"

    def test_nothing_recognized_means_the_model_decides(self):
        request = _request("机器学习的主要方向")
        intent = diagram_intent.apply(request)

        assert intent == diagram_intent.DiagramIntent(None, SOURCE_MODEL, None)
        assert request.diagram_type is None
        assert request.goal == "机器学习的主要方向"

    def test_apply_is_idempotent(self):
        """开流前解析一次，`service.run` 里再读一次，**不能剥第二遍**。

        幂等靠私有属性上的那条记录：第一次 `apply` 之后 `diagram_type` 已经不是
        `None` 了，再按"请求给了图型"判一次会把 `source` 从 `inferred` 悄悄改成
        `request`——最该被查的那一次恰好失去来历。
        """
        request = _request("机器学习，用鱼骨图")
        first = diagram_intent.apply(request)
        second = diagram_intent.apply(request)

        assert first == second
        assert second.source == SOURCE_INFERRED
        assert request.goal == "机器学习"

    def test_edit_and_expand_do_not_strip(self):
        """`edit` / `expand` 的 `goal` 是"改哪儿"，剥图型词会连指令一起改。

        `"把这一支改成流程图"` 剥完只剩 `"把这一支改成"`——模型拿到的是一句没有
        宾语的指令，而用户要的动作（改图型）根本没被这条路径支持。
        """
        for intent_name in ("edit", "expand"):
            request = _request("把这一支改成流程图", intent=intent_name)
            diagram_intent.apply(request)
            assert request.goal == "把这一支改成流程图"
            assert request.diagram_type is None

    def test_private_attributes_stay_out_of_the_contract(self):
        """`source` / `stripped` 是私有属性：不进字段、不进 OpenAPI。"""
        schema = AgentRunRequest.model_json_schema()
        assert "diagram_source" not in schema["properties"]
        assert "diagram_stripped" not in schema["properties"]
        assert "diagram_source" not in AgentRunRequest.model_fields
        # `prefs_applied` 反过来**是**公开字段，而且必须留在契约里：它由前端标
        # （"下拉里这个值来自偏好、用户没动过"），后端只负责原样落进
        # `runs.request`——那面是"这条偏好到底生没生效"的唯一证据来源。它是
        # "这次请求实际带着什么"的元数据，不是"这次图型是什么"，所以与上面两个
        # 私有属性不冲突。
        assert json.loads(_request("x").model_dump_json()) == {
            "goal": "x", "intent": "create", "thinking_model": "default",
            "run_id": None, "diagram_type": None, "snapshot": None,
            "prefs_applied": [],
            "selected_ids": [], "sources": [],
            "budget": _request("x").budget.model_dump(),
            # 「这一次走不走推理模型」是公开字段（前端要发它），缺省 False。
            # 它不进 `prefs_applied`：那一位记的是"长期偏好"，而这个开关只活在
            # 一次请求里（见 `tests/test_deep_thinking.py`）。
            "deep_thinking": False,
        }


# ---------------------------------------------------------------- 3. 一路走到产物


FISHBONE_STUB: dict[str, Any] = {
    "branches": [
        {"label": "人", "color": "blue", "icon": "🧑"},
        {"label": "机", "color": "teal", "icon": "🧰"},
        {"label": "料", "color": "amber", "icon": "📦"},
        {"label": "法", "color": "green", "icon": "📘"},
        {"label": "环", "color": "purple", "icon": "🌡"},
        {"label": "测", "color": "red", "icon": "📏"},
    ],
}


def _stub(systems: list[str]):
    """骨架给六个类别，每一支给两个要点。

    `systems` 收下每次调用的 system：这一组要断言的核心是"骨架那一次用的到底是哪
    一份提示词"。只比树的话，模型没写边的 `flowchart` 与 `tree` 会长出一模一样的
    树，用例会绿着而接错线也没人发现（这条教训来自 `test_agent_diagram_type.py`）。
    """
    async def fake(system: str, user: str, **kwargs):
        systems.append(system)
        if system.startswith(FISHBONE_PLAN_PROMPT):
            return FISHBONE_STUB
        if system.startswith(FLOW_PLAN_PROMPT):
            return FISHBONE_STUB
        if system.startswith(PLAN_PROMPT):
            return FISHBONE_STUB
        return {"children": [{"label": "要点一"}, {"label": "要点二"}]}

    return AsyncMock(side_effect=fake)


def _generate(request: AgentRunRequest):
    """按"真实入口"的顺序跑一遍：解析 → 建注册表 → 执行生成。

    `topic` 用**剥离后的 `goal`**：模型是从用户那句话里提炼主题的，`goal` 干净了，
    它填出来的 `topic` 才干净。这就是这条修法的传导链，也是"根标签不含鱼骨"
    这个断言的真正含义——不是我们改了 `topic`，是我们没让那个词走到模型面前。
    """
    diagram_intent.apply(request)
    intent = diagram_intent.of(request)
    ctx = RunContext(
        request=request, candidate=None,
        diagram_source=intent.source, diagram_stripped=intent.stripped,
    )
    registry = build_registry(
        "create_deep", has_library=False, diagram_type=request.diagram_type)
    systems: list[str] = []
    with patch("app.agent.tools.chat_async", new=_stub(systems)):
        result = asyncio.run(registry.execute(
            ToolCall(call_id="c1", name="mindmap_generate_deep", arguments={
                "topic": request.goal, "branches": 6,
                "points_per_branch": 2, "max_depth": 2,
            }),
            ctx,
        ))
    return result, ctx, systems


class TestEndToEnd:
    def test_the_bug_is_fixed_from_the_goal_text(self):
        """**复现 + 修复**：`goal = "机器学习，用鱼骨图"`。

        - 图型：`fishbone`（不是模型默认的 `tree`）；
        - `goal`：剥成 `"机器学习"`；
        - 根标签：`"机器学习"`——不含"鱼骨"；
        - 产物：`content.diagram_type == "fishbone"`（图自己说得出来）；
        - `data["diagram"]`：`source == "inferred"`、`stripped == "用鱼骨图"`。
        """
        request = _request("机器学习，用鱼骨图")
        result, ctx, systems = _generate(request)

        assert result.ok, result.content
        assert systems[0].startswith(FISHBONE_PLAN_PROMPT)
        assert request.goal == "机器学习"

        assert ctx.candidate is not None
        assert ctx.candidate.root.label == "机器学习"
        assert "鱼骨" not in ctx.candidate.root.label
        # 树里任何一处都不该出现那三个字：根标签干净而子节点带着它，等于没修。
        assert "鱼骨" not in json.dumps(
            ctx.candidate.model_dump(), ensure_ascii=False)
        assert ctx.candidate.diagram_type == "fishbone"
        # 鱼骨图不产出附加边（那三条来自 flow 提示词，这里一条都没有）。
        assert ctx.candidate.edges == []

        assert result.data["diagram"] == {
            "type": "fishbone",
            "source": "inferred",
            "stripped": "用鱼骨图",
            "edges": 0,
            "edge_kinds": {"flow": 0, "branch": 0, "loop": 0},
        }

    def test_flowchart_from_the_goal_uses_the_flow_prompt(self):
        request = _request("用户注册流程图")
        result, ctx, systems = _generate(request)

        assert result.ok, result.content
        assert systems[0].startswith(FLOW_PLAN_PROMPT)
        assert request.goal == "用户注册"
        assert ctx.candidate is not None
        assert ctx.candidate.root.label == "用户注册"
        assert ctx.candidate.diagram_type == "flowchart"
        assert result.data["diagram"]["source"] == "inferred"
        assert result.data["diagram"]["stripped"] == "流程图"

    def test_plain_process_topic_still_grows_a_mindmap(self):
        """`"优化退货流程"` → 仍然是 `tree`，`goal` 一个字没动。

        这条是判据的护栏：误判一次的表现不是报错，是用户拿到一张骨架完全不对
        的图，而他会以为那是模型理解错了。
        """
        request = _request("优化退货流程")
        result, ctx, systems = _generate(request)

        assert result.ok, result.content
        assert systems[0].startswith(PLAN_PROMPT)
        assert request.goal == "优化退货流程"
        assert request.diagram_type is None
        assert ctx.candidate is not None
        assert ctx.candidate.root.label == "优化退货流程"
        assert ctx.candidate.diagram_type == "tree"
        # 解析跑过（source="model"），但这一轮谁都没说图型——不是"没有信息来源"。
        assert result.data["diagram"] == {
            "type": "tree", "source": SOURCE_MODEL, "stripped": None, "edges": 0,
            "edge_kinds": {"flow": 0, "branch": 0, "loop": 0},
        }


# ---------------------------------------------------------------- 4. 契约与导出


class TestContractField:
    def test_default_is_tree_and_old_records_just_work(self):
        """默认 `tree`、旧图零迁移：缺这个键的记录读进来就是思维导图。

        旧图本来就是思维导图——它们全部由 `tree` 那条提示词生成，所以这个默认值
        不是"猜"，是事实。`schema_version` 也不用升：树那一半的形状没变。
        """
        old = MindMapContent.model_validate({
            "title": "旧图", "root": {"id": "r", "label": "根", "children": []},
        })
        assert old.diagram_type == "tree"
        assert old.model_dump()["diagram_type"] == "tree"

    def test_it_is_a_literal_not_free_text(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MindMapContent.model_validate({
                "title": "图", "diagram_type": "fish",
                "root": {"id": "r", "label": "根", "children": []},
            })

    def test_puml_header_names_the_type(self):
        """导出的 PUML 在文件头注明图型：拿到文件的人不看字段，只看得见这一行。"""
        fish = MindMapContent.model_validate({
            "title": "缺陷", "diagram_type": "fishbone",
            "root": {"id": "r", "label": "缺陷", "children": []},
        })
        lines = to_puml(fish).split("\n")
        assert lines[0] == "@startmindmap"
        assert lines[1] == "' 图型：鱼骨图（fishbone）"

        plain = MindMapContent.model_validate({
            "title": "旧图", "root": {"id": "r", "label": "旧图", "children": []},
        })
        assert to_puml(plain).split("\n")[1] == "' 图型：思维导图（tree）"


# ---------------------------------------------------------------- 5. 开流前的接线


class TestRouterWiring:
    def test_stream_run_resolves_before_the_run_is_recorded(self):
        """解析发生在**开流之前**：运行台账里记的是剥完的 `goal` 与定下来的图型。

        台账是事后排查"我要的鱼骨图怎么成了导图"的唯一痕迹。记 `None` / 原句，
        等于最该被查的那一次什么都查不出来。
        """
        request = _request("机器学习，用鱼骨图")
        request.run_id = "diagram-1"

        async def fake_run(body, *, on_close=None, on_checkpoint=None, first_seq=None):
            yield DoneEvent(seq=1, content=None, mode="model")

        async def scenario():
            with patch.object(agent_router, "run", new=fake_run):
                response = await agent_router.stream_run(request)
                return [chunk async for chunk in response.body_iterator]

        chunks = asyncio.run(scenario())
        assert any('"type": "done"' in chunk for chunk in chunks)

        row = runs.get("diagram-1")
        assert row is not None
        assert row.request["goal"] == "机器学习"
        assert row.request["diagram_type"] == "fishbone"
        # 请求对象本身也被改写了：`run()` 拿到的是同一份，工具层读的也是它。
        assert request.diagram_type == "fishbone"

    def test_the_old_frontend_still_reads_none_when_nothing_is_said(self):
        """没识别出来时台账里仍是 `None`——"用户让我自己判断"这件事不能被抹掉。"""
        request = _request("做一张导图")
        request.run_id = "diagram-2"

        async def fake_run(body, *, on_close=None, on_checkpoint=None, first_seq=None):
            yield DoneEvent(seq=1, content=None, mode="model")

        async def scenario():
            with patch.object(agent_router, "run", new=fake_run):
                response = await agent_router.stream_run(request)
                return [chunk async for chunk in response.body_iterator]

        asyncio.run(scenario())
        row = runs.get("diagram-2")
        assert row is not None
        assert row.request["diagram_type"] is None
