"""图型（tree / flowchart / fishbone）从**请求层**一路走到骨架提示词。

这一组钉的是用户实测撞到的那次失败：他说"画个流程图"，模型把"流程图"这三个字
在 `goal` 里当成主题描述消化掉了，**没有回头去设工具参数**——`GenerateDeepArgs.
diagram_type` 的默认值是 `tree`，于是产出还是一张思维导图。接线一直是通的（三份
骨架提示词、edges 解析、落树都在），断的是"用户说了算"这条链。

修法：图型提到 `AgentRunRequest.diagram_type`。请求给了就由请求说了算，工具参数
那一份不再由模型决定（registry 里连这个字段都不下发）。

分五组：

  1. **请求指定图型**：骨架用哪一份 system、产出带不带 edges；
  2. **请求不带图型**：与 `2ed92ac`（加请求层之前那一版）逐字节一致；
  3. **两处打架**：请求赢；以及"参数已经不下发给模型"这件事本身；
  4. **缺色补齐**：一级分支必须有色，模型不给就按调色板顺序分配；
  5. **不受影响的部分**：`node_expand` / `mindmap_edit` 不生成骨架，图型对它们
     没有任何意义——工具集与加这个字段之前一模一样。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    FISHBONE_PLAN_PROMPT,
    FLOW_PLAN_PROMPT,
    PLAN_PROMPT,
    _fill_branch_colors,
    _generate_deep_spec,
    _SkeletonBranch,
    build_registry,
)
from app.contracts.mindmap import NODE_COLORS

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent

# 基线与 `test_edges.py` 里那条不同，是**故意的**：那条守的是"tree 与加
# `diagram_type` 之前一致"（e3ba390），这条守的是"请求不带图型时与加**请求层**
# 之前一致"（2ed92ac，它的工具参数已经有 diagram_type 了）。
BASELINE_REV = "2ed92ac"


# ---------------------------------------------------------------- 桩与工具


def _ctx(diagram_type: str | None = None, *, intent: str = "create",
         max_model_calls: int = 12) -> RunContext:
    request = AgentRunRequest(
        goal="画个流程图", intent=intent, diagram_type=diagram_type,
        budget=RunBudget(max_model_calls=max_model_calls, max_output_tokens=32768),
    )
    return RunContext(request=request, candidate=None)


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(call_id=f"c-{name}", name=name, arguments=arguments)


def _run(coro):
    return asyncio.run(coro)


PLAN_STUB: dict[str, Any] = {
    "branches": [
        {"label": "提交申请", "color": "blue", "icon": "📝"},
        {"label": "形式审查", "color": "amber", "icon": "🔍"},
        {"label": "付款", "color": "teal", "icon": "💳"},
    ],
    "edges": [
        {"source": "root", "target": 0, "kind": "flow"},
        {"source": 1, "target": 2, "label": "是", "kind": "branch"},
        {"source": 1, "target": 0, "label": "驳回", "kind": "loop"},
    ],
}


def _stub(plan: dict[str, Any] | None = None, *, systems: list[str] | None = None):
    """骨架 + 每支两个要点。

    `systems` 非 None 时把每次调用的 system 收进去：这一组用例的核心断言就是
    "骨架那一次的 system 到底是哪一份"——只比产出的话，`tree` 与 `flowchart`
    在"模型没写边"时会产出一模一样的树，用例会绿着而接错线也没人发现。
    """
    body = dict(PLAN_STUB if plan is None else plan)

    async def fake(system: str, user: str, **kwargs):
        if systems is not None:
            systems.append(system)
        if (system.startswith(PLAN_PROMPT) or system.startswith(FLOW_PLAN_PROMPT)
                or system.startswith(FISHBONE_PLAN_PROMPT)):
            return body
        return {"children": [{"label": "要点一"}, {"label": "要点二"}]}

    return AsyncMock(side_effect=fake)


def _generate(ctx: RunContext, plan: dict[str, Any] | None = None,
              *, systems: list[str] | None = None, **arguments):
    """按请求里的图型建注册表，然后执行一次生成。工具参数由调用方决定写不写。"""
    registry = build_registry(
        "create_deep", has_library=False, diagram_type=ctx.request.diagram_type)
    with patch("app.agent.tools.chat_async", new=_stub(plan, systems=systems)):
        return _run(registry.execute(
            _call("mindmap_generate_deep", topic="报销流程", branches=3,
                  points_per_branch=2, max_depth=2, **arguments),
            ctx,
        ))


# ---------------------------------------------------------------- 1. 请求指定图型


class TestRequestSelectsDiagramType:
    def test_flowchart_request_wins_over_the_default_parameter(self):
        """**bug 的复现与修复**：工具参数一个都不写（模型就是这么干的），
        请求说 flowchart → 骨架必须是 FLOW 那一份，且产出带 edges。"""
        systems: list[str] = []
        ctx = _ctx("flowchart")
        result = _generate(ctx, systems=systems)

        assert result.ok, result.content
        assert systems, "一次模型调用都没发生"
        # 骨架是第一次调用（system 里含"只定主干骨架"那一份）
        assert systems[0].startswith(FLOW_PLAN_PROMPT), systems[0][:80]
        assert not systems[0].startswith(PLAN_PROMPT)
        assert ctx.candidate is not None
        assert [(item.kind, item.label) for item in ctx.candidate.edges] == [
            ("flow", None),         # flow：root → 0，没有 label
            ("branch", "是"),
            ("loop", "驳回"),
        ]
        assert result.data["diagram"] == {
            "type": "flowchart",
            # 请求层带的就是 flowchart：`source` 是 `request`（不是 `inferred`——
            # 推断只在请求没给的时候发生）。
            "source": "request",
            # 请求已经指定图型时不剥 `goal`，所以没有"被剥掉的那一段"。
            "stripped": None,
            "edges": 3,
            "edge_kinds": {"flow": 1, "branch": 1, "loop": 1},
        }
        # 不回归：`expandable` 与 `edges` 一起产出，互不影响
        for branch in ctx.candidate.root.children:
            assert branch.expandable in ("yes", "no", "unknown")

    def test_fishbone_request_uses_the_fishbone_prompt_and_no_edges(self):
        systems: list[str] = []
        ctx = _ctx("fishbone")
        result = _generate(ctx, systems=systems)

        assert result.ok, result.content
        assert systems[0].startswith(FISHBONE_PLAN_PROMPT)
        assert ctx.candidate is not None
        # 鱼骨图不产出边：桩里给了三条边，一条都不该落树
        assert ctx.candidate.edges == []
        assert result.data["diagram"]["type"] == "fishbone"

    def test_tree_request_uses_the_plain_prompt_and_drops_edges(self):
        systems: list[str] = []
        ctx = _ctx("tree")
        result = _generate(ctx, systems=systems)

        assert result.ok, result.content
        assert systems[0].startswith(PLAN_PROMPT)
        assert ctx.candidate is not None
        assert ctx.candidate.edges == []
        # tree 的 data 键集与加 diagram_type 之前相同：**没有** diagram 这个键
        assert "diagram" not in result.data


# ---------------------------------------------------------------- 2. 不带图型时的回归


def _load_baseline_tools():
    """把 `2ed92ac` 的 `tools.py` 取出来当模块载入（不落盘到 app/ 下）。"""
    source = subprocess.run(
        ["git", "show", f"{BASELINE_REV}:backend/app/agent/tools.py"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    name = "baseline_tools_2ed92ac"
    if name in sys.modules:
        return sys.modules[name]
    module_path = BACKEND / f"_{name}.py"
    module_path.write_text(source, encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location(name, module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    finally:
        module_path.unlink(missing_ok=True)
    return module


class _Seq:
    """确定性的 uuid 序列：`new_node_id` 读它，两次运行的 id 才能对得上。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.seq = iter([uuid.UUID(int=index) for index in range(1, 5000)])


_SEQ = _Seq()


def _snapshot(module, *, request_diagram_type: str | None, arguments: dict) -> tuple:
    """用同一组桩响应跑一次分片生成，返回 (content, data, 候选树 dump)。"""
    from app.agent import ops as ops_module

    ctx = _ctx(request_diagram_type, max_model_calls=12)
    args = module.GenerateDeepArgs(topic="报销流程", branches=3, points_per_branch=2,
                                   max_depth=2, **arguments)
    _SEQ.reset()
    with patch.object(module, "chat_async", new=_stub()), \
            patch.object(ops_module, "uuid4", new=lambda: next(_SEQ.seq)):
        result = _run(module._generate_deep(args, ctx))
    assert result.ok, result.content
    return result.content, result.data, ctx.candidate.model_dump()


def _strip_origin(node: dict) -> dict:
    """去掉 `origin` 之后的节点树（递归）。

    与 `test_edges.py` 的同名函数是同一处例外、同一份理由：`Node.origin` 是这个基线
    之后才加的契约字段，记的是"这个节点怎么写进来的"，树的形状一个字节都没动。
    剥之前先证明基线里它只有缺省值 / `manual`、新树里只有 `generated`——否则剥皮会
    掩盖真正的行为改变。
    """
    out = {key: value for key, value in node.items() if key != "origin"}
    out["children"] = [_strip_origin(child) for child in node.get("children", [])]
    return out


def _origins_in(node: dict) -> list[str]:
    return [node.get("origin")] + [item for child in node.get("children", [])
                                   for item in _origins_in(child)]


class TestRequestWithoutDiagramTypeIsUnchanged:
    def test_generation_matches_the_pre_request_layer_version(self):
        """请求不带 `diagram_type`（老前端）时，产出与 `2ed92ac` **逐字节一致**。

        比三份东西：回给模型的那句话、`data` 的键与值、候选树的整份 `model_dump()`。
        这条要求就是"向后兼容"的全部含义——用户在旧界面上点生成，拿到的必须还是
        那一棵树。

        桩里的骨架**带全颜色**：让"缺色由调色板补"这条新加的行为不参与比较（它有
        自己的用例）。否则这条回归会被那条改动牵连成一片红，反而看不出图型这条链
        有没有接歪。

        **例外只有一句**：外部检索开关在测试环境里是关的（`conftest.clean_env`），
        而"你关掉了联网检索…"是后来加的用户可见行为，与"图型参数不影响输出"无关。
        比对前剥掉，并引用 `tools.EXTERNAL_DISABLED_NOTE` 而不是抄字面量——抄一份
        就是第三处真相，下次改文案这里会静默失效。它的守卫在
        `test_one_path.test_external_switch_off_says_it_was_switched_off`。
        """
        from app.agent.tools import EXTERNAL_DISABLED_NOTE

        baseline = _load_baseline_tools()
        old_content, old_data, old_tree = _snapshot(
            baseline, request_diagram_type=None, arguments={})

        from app.agent import tools as new_tools
        new_content, new_data, new_tree = _snapshot(
            new_tools, request_diagram_type=None, arguments={})

        assert EXTERNAL_DISABLED_NOTE not in old_content, (
            "基线版本不该有这句话——有了说明基线选错了，剥掉就等于掩盖")
        new_content = new_content.replace(EXTERNAL_DISABLED_NOTE, "")
        # `data.diagnostics` 里同一条也得滤掉；浅拷贝是为了不改动被比的产物本身。
        comparable = {
            **new_data,
            "diagnostics": [item for item in new_data["diagnostics"]
                            if item["code"] != "GENERATION_EXTERNAL_DISABLED"],
        }
        assert new_data["diagnostics"], "这条过滤本身要能被验证：诊断列表不该本来就空"
        assert len(comparable["diagnostics"]) == len(new_data["diagnostics"]) - 1

        assert new_content == old_content
        assert json.dumps(comparable, ensure_ascii=False, sort_keys=True) == \
               json.dumps(old_data, ensure_ascii=False, sort_keys=True)
        # `Node.origin` 那处例外的两半。基线跑的是"旧的 `tools.py` + 现在的
        # `ops.py`"：旧代码从不写这个字段，所以直接建出来的节点停在缺省 `unknown`，
        # op 落的节点带着现在执行器写的 `manual`。两句话合起来是"基线这一路从不产出
        # `generated`"，而新树整棵都是它——剥掉的那一列里因此只有本次新增的行为。
        assert set(_origins_in(old_tree["root"])) <= {"unknown", "manual"}, \
            "基线不该出现 generated：出现了说明它其实已经在写这个字段"
        assert set(_origins_in(new_tree["root"])) == {"generated"}, \
            "剥掉 origin 的前提是它的取值只有一个正确值"
        assert json.dumps(_strip_origin(new_tree["root"]), ensure_ascii=False,
                          sort_keys=True) == \
               json.dumps(_strip_origin(old_tree["root"]), ensure_ascii=False,
                          sort_keys=True)

    def test_tool_schema_is_byte_identical_when_no_type_is_fixed(self):
        """请求没定图型时，下发给模型的工具定义与 `2ed92ac` 一字不差。

        这一条守的是"老前端看不到任何变化"：参数还在、描述还是那句"diagram_type
        选图型"、没有多出隐藏字段。改描述时最容易在这里露馅。

        **两处例外，且例外本身被断言了**：`branches.description` 从"一级分支数量上限"
        改成了"已废弃：…填什么都不会改变结果"，`parameters.description`（模型 docstring）
        里关于它的那半句也跟着改（`docs/BRANCH-CAP.md` §5.2.1）。那个字段早就不控制任何
        东西（`app/**` 里零读取），旧描述是一句**错话**；改的是它，不是行为。所以下面把
        这两格归一化之后再整份比——别的任何一格变了、或者这两格被改回旧口径，断言各自会红。
        """
        def _normalized(schema: dict) -> dict:
            clone = json.loads(json.dumps(schema, ensure_ascii=False))
            parameters = clone["function"]["parameters"]
            parameters["description"] = "<params>"
            parameters["properties"]["branches"]["description"] = "<inert>"
            return clone

        baseline = _load_baseline_tools()
        old = baseline._TOOL_SPECS["mindmap_generate_deep"]().schema()
        new = _generate_deep_spec().schema()

        old_parameters = old["function"]["parameters"]
        new_parameters = new["function"]["parameters"]
        assert "期望值" in old_parameters["description"], (
            "基线那一头的旧口径变了——改了说明基线选错或这处已被人动过")
        assert "历史遗留的无效字段" in new_parameters["description"]
        assert old_parameters["properties"]["branches"]["description"] == "一级分支数量上限"
        new_description = new_parameters["properties"]["branches"]["description"]
        assert "已废弃" in new_description and "上限" not in new_description

        assert json.dumps(_normalized(new), ensure_ascii=False, sort_keys=True) == \
               json.dumps(_normalized(old), ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------- 3. 两处打架


class TestRequestBeatsToolArguments:
    def test_request_tree_beats_arguments_flowchart(self):
        """请求说 tree、工具参数写 flowchart → **请求赢**（没有边、用 PLAN 提示词）。"""
        systems: list[str] = []
        ctx = _ctx("tree")
        result = _generate(ctx, systems=systems, diagram_type="flowchart")

        assert result.ok, result.content
        assert systems[0].startswith(PLAN_PROMPT)
        assert not systems[0].startswith(FLOW_PLAN_PROMPT)
        assert ctx.candidate is not None
        assert ctx.candidate.edges == []
        assert "diagram" not in result.data

    def test_request_flowchart_beats_arguments_tree(self):
        """反向也是请求赢：参数写 tree、请求说 flowchart → 流程图。"""
        systems: list[str] = []
        ctx = _ctx("flowchart")
        result = _generate(ctx, systems=systems, diagram_type="tree")

        assert result.ok, result.content
        assert systems[0].startswith(FLOW_PLAN_PROMPT)
        assert ctx.candidate is not None
        assert ctx.candidate.edges
        assert result.data["diagram"]["type"] == "flowchart"

    def test_frozen_spec_hides_the_parameter_from_the_model(self):
        """请求定了图型 → 这个参数**不再下发给模型**，描述改成"已选定"。

        两件事一起钉:
          - `parameters.properties` 里没有 `diagram_type`（模型看不见就不填）；
          - 描述里说清本次是哪一种（不然它会按默认的思维导图去理解任务）。
        """
        schema = _generate_deep_spec("flowchart").schema()["function"]
        assert "diagram_type" not in schema["parameters"]["properties"]
        # 别的参数一个都不能少：隐藏是精确到这一个字段的
        assert set(schema["parameters"]["properties"]) == {
            "topic", "branches", "points_per_branch", "max_depth",
        }
        assert "diagram_type 选图型" not in schema["description"]
        assert "已由用户在界面上选定" in schema["description"]
        assert "流程图" in schema["description"]

    def test_parameter_written_anyway_is_overridden_not_rejected(self):
        """模型凭老习惯补一个参数时：**校验收下、请求覆盖**，而不是报参数不合法。

        报错的代价是一整轮调用白烧（它下一轮才改对），而那个值本来就要被覆盖。
        """
        from app.agent.tools import GenerateDeepArgs

        # 冻结之后的 args_model 还是同一个模型：字段仍在，只是不下发
        args = GenerateDeepArgs.model_validate({
            "topic": "报销", "diagram_type": "tree",
        })
        assert args.diagram_type == "tree"      # 模型写的值确实进来了
        systems: list[str] = []
        ctx = _ctx("flowchart")
        result = _generate(ctx, systems=systems, diagram_type="tree")
        assert result.ok, result.content
        assert systems[0].startswith(FLOW_PLAN_PROMPT)   # 生效的是请求
        assert ctx.candidate is not None and ctx.candidate.edges


# ---------------------------------------------------------------- 4. 缺色补齐


class TestBranchColorsAreFilled:
    """一级分支必须有色——这是"颜色=分支分组"的前提（后代沿父链继承它）。"""

    def test_model_gives_no_color_at_all(self):
        ctx = _ctx()
        plan = {"branches": [{"label": "甲"}, {"label": "乙"}, {"label": "丙"}]}
        result = _generate(ctx, plan=plan)

        assert result.ok, result.content
        assert ctx.candidate is not None
        assert [branch.color for branch in ctx.candidate.root.children] == \
               list(NODE_COLORS[:3])

    def test_fill_skips_colors_the_model_already_used(self):
        """模型给了 green，补齐的那几个就不能也给 green——撞色等于两条分支同组。"""
        ctx = _ctx()
        plan = {"branches": [
            {"label": "甲", "color": "green"}, {"label": "乙"}, {"label": "丙"},
        ]}
        result = _generate(ctx, plan=plan)

        assert result.ok, result.content
        assert ctx.candidate is not None
        colors = [branch.color for branch in ctx.candidate.root.children]
        # green 被占，池子从 blue 起按调色板顺序给
        assert colors == ["green", "blue", "amber"]
        assert len(set(colors)) == 3

    def test_colors_already_given_are_left_alone(self):
        ctx = _ctx()
        plan = {"branches": [
            {"label": "甲", "color": "purple"},
            {"label": "乙", "color": "pink"},
            {"label": "丙", "color": "gray"},
        ]}
        result = _generate(ctx, plan=plan)

        assert result.ok, result.content
        assert ctx.candidate is not None
        assert [branch.color for branch in ctx.candidate.root.children] == \
               ["purple", "pink", "gray"]

    def test_helper_is_pure_and_palette_ordered(self):
        """纯函数：同一条骨架永远得到同一组颜色；池子用完就按序循环。"""
        plain = [_SkeletonBranch(f"支{index}") for index in range(len(NODE_COLORS) + 2)]
        first = [branch.color for branch in _fill_branch_colors(plain)]
        second = [branch.color for branch in _fill_branch_colors(plain)]
        assert first == second
        assert first[:len(NODE_COLORS)] == list(NODE_COLORS)
        # 8 色用完、还得继续补：从头循环，而不是留空
        assert first[len(NODE_COLORS):] == list(NODE_COLORS[:2])
        # 原列表没被就地改动（dataclass 是 frozen 的，返回的是新列表）
        assert all(branch.color is None for branch in plain)

    def test_children_still_get_no_color(self):
        """补色只到一级：二级往下仍然不带（它们靠渲染时沿父链继承）。"""
        ctx = _ctx()
        result = _generate(ctx, plan={"branches": [
            {"label": "甲"}, {"label": "乙"}, {"label": "丙"},
        ]})

        assert result.ok, result.content
        assert ctx.candidate is not None
        for branch in ctx.candidate.root.children:
            assert branch.color is not None
            for child in branch.children:
                assert child.color is None


# ---------------------------------------------------------------- 5. 别的意图不受影响


class TestOtherIntentsAreUntouched:
    """`node_expand` 与 `mindmap_edit` 不生成骨架，图型对它们没有意义。

    这两条路必须与加这个字段之前**一模一样**：多一个参数、多一次分支判断，都会在
    这两条最常用的路径上引入没人要的差异。
    """

    @pytest.mark.parametrize("intent", ["expand", "edit"])
    def test_tool_set_is_the_same_with_and_without_a_diagram_type(self, intent):
        plain = build_registry(intent, has_library=False)
        typed = build_registry(intent, has_library=False, diagram_type="flowchart")
        assert plain.names() == typed.names()
        assert "mindmap_generate_deep" not in typed.names()
        for name in typed.names():
            spec = typed.get(name)
            assert spec is not None
            schema = spec.schema()["function"]
            assert "diagram_type" not in schema["parameters"]["properties"]
            # node_expand 的参数一个都没变
            if name == "node_expand":
                assert set(schema["parameters"]["properties"]) == {
                    "node_id", "count", "instruction",
                }

    def test_generate_tool_is_not_registered_for_other_intents(self):
        """哪怕请求点名了图型，非 create 意图也不会因此多出生成工具。"""
        for intent in ("expand", "edit"):
            names = build_registry(intent, has_library=False,
                                   diagram_type="fishbone").names()
            assert "mindmap_generate_deep" not in names


@pytest.mark.parametrize("diagram_type", ["tree", "flowchart"])
def test_plan_prompt_composition_rules_hold_per_type(monkeypatch, diagram_type):
    """骨架 system 的**拼装规则**不因图型而变：有材料时不加那两条纯文本契约，
    没有材料时才加。这是"图型只决定用哪一份提示词"这句话的可执行版本——我改的
    是图型的来源，不是拼装规则，两条都不能被顺手改掉。
    """
    from app.agent.contracts import Source
    from app.generation.service import CONTENT_FREEDOM_CONTRACT

    systems: list[str] = []

    monkeypatch.setattr("app.agent.tools._search_branch",
                        AsyncMock(return_value=[]))
    monkeypatch.setattr("app.agent.tools.chat_async", _stub(systems=systems))

    # 有材料（长到不至于被当成"太短"）：不加自由补全契约
    with_material = _ctx(diagram_type)
    text = "第 1 条 本办法适用于全体成员。" * 40
    with_material.request.sources = [
        Source(id="doc", kind="document", status="parsed", text=text),
    ]
    with_material.sources_by_id = {source.id: source for source in with_material.request.sources}
    result = _run(
        build_registry("create_deep", has_sources=True, has_library=False,
                       diagram_type=diagram_type).execute(
            _call("mindmap_generate_deep", topic="管理办法", branches=3,
                  points_per_branch=2, max_depth=2),
            with_material),)
    assert result.ok, result.content
    assert CONTENT_FREEDOM_CONTRACT not in systems[0]

    # 没材料：加。两次用的都是同一个图型对应的那一份 base prompt
    systems.clear()
    monkeypatch.setattr("app.agent.tools.external_knowledge_enabled",
                        lambda: False)
    plain = _ctx(diagram_type)
    result = _generate(plain, systems=systems)
    assert result.ok, result.content
    assert CONTENT_FREEDOM_CONTRACT in systems[0]
    assert systems[0].startswith(
        FLOW_PLAN_PROMPT if diagram_type == "flowchart" else PLAN_PROMPT)


# ---------------------------------------------------------------- 6. 契约层


class TestRequestContract:
    def test_default_is_none_so_old_clients_are_unchanged(self):
        request = AgentRunRequest(goal="画个流程图")
        assert request.diagram_type is None

    def test_unknown_type_is_rejected_before_the_stream_opens(self):
        """拼错的值当场 422，而不是静默变成"自动判断"（与 thinking_model 同一条）。"""
        with pytest.raises(ValueError):
            AgentRunRequest(goal="画个流程图", diagram_type="flow")  # type: ignore[arg-type]

    def test_three_types_are_accepted(self):
        for value in ("tree", "flowchart", "fishbone"):
            assert AgentRunRequest(goal="x", diagram_type=value).diagram_type == value
