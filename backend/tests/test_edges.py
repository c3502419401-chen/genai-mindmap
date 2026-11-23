"""附加边（`MindMapContent.edges`）：「树 + 边」这个折中的每一处承诺。

分四组：

  1. **契约与校验**：默认空列表 → 旧图零迁移；dangling / 自环硬拒（渲染会崩）；
     重复边去重；`label` 不同的平行边保留；环允许；超限与超长环报 warn 码。
  2. **图型**：`diagram_type="tree"` 与 `e3ba390` 的旧模块**逐字节一致**（这一条
     用 `git show` 把旧 `tools.py` 载进内存跑同一组桩响应，比对 content / data /
     候选树的整份 dump）；`flowchart` 出来的树带 edges 且引用的 id 都存在；
     `fishbone` 不出边。
  3. **细粒度工具**：`add_edge` / `remove_edge` 的原子性；`remove_node` 连带删边。
  4. **导出**：puml 里能 grep 到 `A --> B : label`，markdown / opml 里有
     「附加关系」一节；没有边的图三种格式**逐字节不变**。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import build_registry
from app.contracts.mindmap import (
    MAX_EDGE_CYCLE_NODES,
    MAX_EDGE_LABEL_CHARS,
    MAX_EDGES,
    Edge,
    MindMapContent,
)
from app.export.service import to_markdown, to_opml, to_puml
from app.mindmaps.edges import (
    EDGE_CYCLE_TOO_LONG,
    EDGE_DANGLING,
    EDGE_LIMIT,
    clean_edges,
    edge_diagnostics,
    has_long_cycle,
)

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent

# 逐字节回归的基线提交：`feat: 节点视觉元素与细粒度编辑工具`（本次改动之前的那一版）。
BASELINE_REV = "e3ba390"


def edge(edge_id: str, source: str, target: str, label=None, kind=None) -> dict:
    return {"id": edge_id, "source": source, "target": target, "label": label, "kind": kind}


def content(children=None, **extra) -> dict:
    return {
        "title": "报销流程",
        "root": {
            "id": "root", "label": "报销", "children": children or [
                {"id": "a", "label": "提交申请", "children": []},
                {"id": "b", "label": "形式审查", "children": []},
                {"id": "c", "label": "付款", "children": []},
            ],
        },
        **extra,
    }


# ---------------------------------------------------------------- 契约


class TestContract:
    def test_edges_default_to_empty_list(self):
        """旧图没有这个键时要能读进来：这是"零迁移"的全部实现。"""
        parsed = MindMapContent.model_validate(content())
        assert parsed.edges == []

    def test_old_record_needs_no_migration(self):
        """一份 v2 早期记录（没有 `edges`）存回去再读出来，只多一个空列表。"""
        raw = content()
        parsed = MindMapContent.model_validate(raw)
        dumped = parsed.model_dump()
        assert dumped["edges"] == []
        # 除 `edges` 之外，其余字段与输入一致（没有别的字段被悄悄补上）。
        # `diagram_type` 是后来加的产物字段，同样靠字段默认值补齐（默认 `tree`），
        # 不写迁移脚本——旧图本来就是用 `tree` 那条提示词生成的。
        assert set(dumped) - set(raw) == {"edges", "schema_version", "layout",
                                          "theme", "relations", "source_usage",
                                          "diagram_type"}
        assert dumped["diagram_type"] == "tree"

    def test_kind_is_a_literal(self):
        """`kind` 是 `Literal` 而不是自由字符串：渲染层按它选画法。"""
        with pytest.raises(ValidationError):
            Edge.model_validate(edge("e1", "a", "b", kind="next"))
        assert Edge.model_validate(edge("e1", "a", "b", kind="loop")).kind == "loop"
        assert Edge.model_validate(edge("e1", "a", "b")).kind is None

    def test_label_is_capped(self):
        with pytest.raises(ValidationError):
            Edge.model_validate(edge("e1", "a", "b", label="长" * (MAX_EDGE_LABEL_CHARS + 1)))
        assert Edge.model_validate(edge("e1", "a", "b", label="是")).label == "是"

    def test_blank_label_becomes_none(self):
        """纯空白的标签等于没有标签——与 `note` / `detail` 同一条归一。"""
        assert Edge.model_validate(edge("e1", "a", "b", label="   ")).label is None


class TestValidation:
    def test_dangling_is_rejected(self):
        """端点不存在 → 硬拒。前端按 id 找节点，找不到就是一条 `source: undefined`
        的边，Vue Flow 会抛错而不是跳过，整张画布白屏。"""
        with pytest.raises(ValidationError) as exc:
            MindMapContent.model_validate(content(edges=[edge("e1", "a", "nope")]))
        assert "不在导图中" in str(exc.value)

    def test_self_loop_is_rejected(self):
        with pytest.raises(ValidationError) as exc:
            MindMapContent.model_validate(content(edges=[edge("e1", "a", "a")]))
        assert "自环" in str(exc.value)

    def test_duplicate_edges_are_deduped(self):
        """幂等输入（模型重试、用户点两次）不该让整张图 422。"""
        parsed = MindMapContent.model_validate(content(edges=[
            edge("e1", "a", "b", "是"), edge("e2", "a", "b", "是"),
        ]))
        assert [item.id for item in parsed.edges] == ["e1"]

    def test_parallel_edges_with_different_labels_survive(self):
        """判断节点的"是"与"否"经常指向同一个目标，那是两条边而不是重复。"""
        parsed = MindMapContent.model_validate(content(edges=[
            edge("e1", "b", "c", "是", "branch"),
            edge("e2", "b", "c", "否", "branch"),
        ]))
        assert sorted(item.label for item in parsed.edges) == ["否", "是"]

    def test_cycles_are_allowed(self):
        """A→B→A 是回退线，是流程图的核心需求。"""
        parsed = MindMapContent.model_validate(content(edges=[
            edge("e1", "a", "b", "通过", "flow"),
            edge("e2", "b", "a", "驳回", "loop"),
        ]))
        assert len(parsed.edges) == 2

    def test_edges_may_cross_subtrees(self):
        """汇合就是"连到另一条 subtree 上的节点"，不能有"必须同父"这种校验。"""
        parsed = MindMapContent.model_validate(content(
            children=[
                {"id": "a", "label": "甲", "children": [{"id": "a1", "label": "甲一", "children": []}]},
                {"id": "b", "label": "乙", "children": []},
            ],
            edges=[edge("e1", "a1", "b", "汇合", "flow")],
        ))
        assert parsed.edges[0].target == "b"

    def test_edge_ids_must_be_unique(self):
        with pytest.raises(ValidationError):
            MindMapContent.model_validate(content(edges=[
                edge("e1", "a", "b"), edge("e1", "b", "c"),
            ]))


# ---------------------------------------------------------------- 诊断码


class TestDiagnostics:
    def test_dangling_is_reported_as_warn(self):
        """生成路径先过滤再报 warn；三条诊断码的级别都是 warn。"""
        parsed = MindMapContent.model_validate(content())
        bogus = Edge.model_construct(id="e9", source="a", target="ghost", label=None, kind=None)
        kept, diagnostics = clean_edges([bogus], {"root", "a", "b", "c"})
        assert kept == []
        assert [item.code for item in diagnostics] == [EDGE_DANGLING]
        assert diagnostics[0].level.value == "warn"
        # `edge_diagnostics` 对同一批边给出同一条码（导出/前端可以直接复用）。
        assert EDGE_DANGLING in [item.code for item in edge_diagnostics([bogus], {"a", "b"})]
        assert parsed.edges == []

    def test_edge_limit_is_warn_not_a_hard_failure(self):
        """超过 200 条只报 `EDGE_LIMIT`，图照常能存能画。"""
        many = [Edge(id=f"e{index}", source="a", target="b", label=str(index))
                for index in range(MAX_EDGES + 5)]
        parsed = MindMapContent.model_validate(content(edges=[item.model_dump() for item in many]))
        assert len(parsed.edges) == MAX_EDGES + 5
        codes = [item.code for item in edge_diagnostics(parsed.edges)]
        assert codes == [EDGE_LIMIT]
        # 生成路径则把它裁到上限，并说明裁了几条。
        kept, diagnostics = clean_edges(parsed.edges, {"a", "b"})
        assert len(kept) == MAX_EDGES
        assert diagnostics[0].code == EDGE_LIMIT
        assert "5" in diagnostics[0].message

    def test_long_cycle_is_warn(self):
        """超过 12 个节点的环报 `EDGE_CYCLE_TOO_LONG`，不硬拒。"""
        size = MAX_EDGE_CYCLE_NODES + 2
        nodes = [{"id": f"n{index}", "label": f"步{index}", "children": []}
                 for index in range(size)]
        ring = [edge(f"e{index}", f"n{index}", f"n{(index + 1) % size}")
                for index in range(size)]
        parsed = MindMapContent.model_validate(content(children=nodes, edges=ring))
        codes = [item.code for item in edge_diagnostics(parsed.edges)]
        assert EDGE_CYCLE_TOO_LONG in codes
        assert has_long_cycle(parsed.edges) is True

    def test_short_cycle_is_fine(self):
        """12 个节点及以内的环不报——这是正常的返工流程。"""
        size = MAX_EDGE_CYCLE_NODES
        nodes = [{"id": f"n{index}", "label": f"步{index}", "children": []}
                 for index in range(size)]
        ring = [edge(f"e{index}", f"n{index}", f"n{(index + 1) % size}")
                for index in range(size)]
        parsed = MindMapContent.model_validate(content(children=nodes, edges=ring))
        assert edge_diagnostics(parsed.edges) == []
        assert has_long_cycle(parsed.edges) is False

    def test_clean_edges_drops_the_edge_that_closes_a_long_cycle(self):
        """增量裁剪：丢掉"会形成超长环"的那一条，其余边照常保留。"""
        size = MAX_EDGE_CYCLE_NODES + 1
        ids = {f"n{index}" for index in range(size)}
        chain = [Edge(id=f"e{index}", source=f"n{index}", target=f"n{index + 1}")
                 for index in range(size - 1)]
        closing = Edge(id="eclose", source=f"n{size - 1}", target="n0")
        kept, diagnostics = clean_edges([*chain, closing], ids)
        assert [item.id for item in kept] == [item.id for item in chain]
        assert [item.code for item in diagnostics] == [EDGE_CYCLE_TOO_LONG]


# ---------------------------------------------------------------- 图型与生成


def _ctx(intent: str = "create", candidate: MindMapContent | None = None,
         max_model_calls: int = 12) -> RunContext:
    request = AgentRunRequest(
        goal="做一张图", intent=intent,
        budget=RunBudget(max_model_calls=max_model_calls, max_output_tokens=32768),
    )
    return RunContext(request=request, candidate=candidate)


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(call_id=f"c-{name}", name=name, arguments=arguments)


def _run(coro):
    return asyncio.run(coro)


def _stub():
    """骨架 + 每支两个要点。分支由 user 消息里的主题决定，固定 3 支。

    骨架**带 color**：逐字节回归用例要拿 `e3ba390` 的旧模块整份比对，而"缺色由
    代码按调色板补"是后来加的、有独立用例守着的一条改动（`_fill_branch_colors`）。
    桩里给全颜色，这条回归就继续只盯"tree 这一支有没有多出别的东西"，不会被那条
    改动牵连成一片红。
    """
    def fake(system: str, user: str, **kwargs):
        if "只定一级骨架" in system or "只定主干骨架" in system or "只定骨架" in system:
            return {
                "branches": [
                    {"label": "提交申请", "color": "blue", "icon": "📝"},
                    {"label": "形式审查", "color": "amber", "icon": "🔍"},
                    {"label": "付款", "color": "teal", "icon": "💳"},
                ],
                "edges": [
                    {"source": 1, "target": 2, "label": "是", "kind": "branch"},
                    {"source": 1, "target": 0, "label": "驳回", "kind": "loop"},
                    {"source": 0, "target": 9},          # dangling：9 号分支不存在
                ],
            }
        return {"children": [{"label": "要点一"}, {"label": "要点二"}]}

    return AsyncMock(side_effect=fake)


def _uuid_sequence():
    """确定性的 uuid 序列：`new_node_id` 读它，两次运行的 id 才能对得上。"""
    return iter([uuid.UUID(int=index) for index in range(1, 5000)])


class TestDiagramType:
    def test_flowchart_generation_carries_edges(self):
        """`flowchart` 的骨架里那些合法边要真的落到树上，写错的端点被丢掉。"""
        ctx = _ctx()
        with patch("app.agent.tools.chat_async", new=_stub()):
            result = _run(build_registry("create_deep", has_library=False).execute(
                _call("mindmap_generate_deep", topic="报销", branches=3,
                      points_per_branch=2, max_depth=2, diagram_type="flowchart"),
                ctx,
            ))
        assert result.ok, result.content
        assert ctx.candidate is not None
        # 桩里给了三条：branch、loop、以及一条 source 0 → target 9 的越界边。
        # 前两条落树，第三条被丢掉，并且**要有一条 `EDGE_DANGLING` 说明它去哪了**
        # ——静默少一条流程分支是这张图最不该有的失败方式。
        assert [(item.label, item.kind) for item in ctx.candidate.edges] == [
            ("是", "branch"), ("驳回", "loop"),
        ]
        assert result.data["diagram"]["edges"] == 2
        codes = [item["code"] for item in result.data["diagnostics"]]
        assert EDGE_DANGLING in codes
        assert "附加关系" in result.content

    def test_flowchart_edges_reference_existing_nodes(self):
        """每一条边引用的 id 都必须在树里（否则落库时契约会拒）。"""
        ctx = _ctx()
        with patch("app.agent.tools.chat_async", new=_stub()):
            result = _run(build_registry("create_deep", has_library=False).execute(
                _call("mindmap_generate_deep", topic="报销", branches=3,
                      points_per_branch=2, max_depth=2, diagram_type="flowchart"),
                ctx,
            ))
        assert result.ok
        assert ctx.candidate is not None
        ids = _node_ids(ctx.candidate.root)
        assert ctx.candidate.edges, "flowchart 应该产出边"
        for item in ctx.candidate.edges:
            assert item.source in ids
            assert item.target in ids
        # 边只连骨架层（root 与一级分支），不连子节点——展开阶段不产出边。
        assert all(item.source in ids and item.target in ids
                   for item in ctx.candidate.edges)

    def test_fishbone_emits_no_edges(self):
        ctx = _ctx()
        with patch("app.agent.tools.chat_async", new=_stub()):
            result = _run(build_registry("create_deep", has_library=False).execute(
                _call("mindmap_generate_deep", topic="缺陷原因", branches=3,
                      points_per_branch=2, max_depth=2, diagram_type="fishbone"),
                ctx,
            ))
        assert result.ok
        assert ctx.candidate is not None
        assert ctx.candidate.edges == []
        assert result.data["diagram"] == {
            # `source` / `stripped` 是"这次图型是怎么定下来的"的两个诊断字段：
            # 这一条请求层没带图型（`_ctx()` 的 `diagram_type` 是 None），
            # 值来自工具参数，所以是 `model`；`goal` 里没有可剥的图型词，`stripped`
            # 是 None。它们不影响 `type` / `edges` 这两个核心值。
            "type": "fishbone", "source": "model", "stripped": None, "edges": 0,
            "edge_kinds": {"flow": 0, "branch": 0, "loop": 0},
        }

    def test_plan_prompts_differ_per_diagram_type(self):
        from app.agent.tools import FISHBONE_PLAN_PROMPT, FLOW_PLAN_PROMPT, PLAN_PROMPT
        assert PLAN_PROMPT != FLOW_PLAN_PROMPT
        # 流程图骨架必须要求 edges，鱼骨图必须明确不要边。
        assert "edges" in FLOW_PLAN_PROMPT and "branch" in FLOW_PLAN_PROMPT
        assert "不要输出 edges" in FISHBONE_PLAN_PROMPT
        assert "人 / 机 / 料 / 法 / 环 / 测" in FISHBONE_PLAN_PROMPT


def _node_ids(root) -> set[str]:
    out: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        out.add(node.id)
        stack.extend(node.children)
    return out


# ---------------------------------------------------------------- 逐字节回归


def _load_baseline_tools():
    """把 `e3ba390` 的 `tools.py` 取出来当模块载入（不落盘到 app/ 下）。"""
    source = subprocess.run(
        ["git", "show", f"{BASELINE_REV}:backend/app/agent/tools.py"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    name = "baseline_tools_e3ba390"
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


def _generation_snapshot(module, *, diagram_args: dict) -> tuple[str, dict, dict]:
    """用同一组桩响应跑一次分片生成，返回 (content, data, 候选树 dump)。"""
    from app.agent import ops as ops_module

    ctx = _ctx(max_model_calls=12)
    args = module.GenerateDeepArgs(topic="报销", branches=3, points_per_branch=2,
                                   max_depth=2, **diagram_args)
    with patch.object(module, "chat_async", new=_stub()), \
            patch.object(ops_module, "uuid4", new=lambda: next(_GENERATION_SNAPSHOT.seq)):
        result = _run(module._generate_deep(args, ctx))
    assert result.ok, result.content
    return result.content, result.data, ctx.candidate.model_dump()


class _SnapshotState:
    seq = _uuid_sequence()


_GENERATION_SNAPSHOT = _SnapshotState()


class TestTreeIsByteIdentical:
    def test_tree_generation_matches_baseline(self):
        """`diagram_type="tree"`（默认值）跑出来的 content / data / 整棵树与
        `e3ba390` **逐字节一致**。

        比的是三份东西：回给模型的那句话、`data` 的键与值、以及候选树的整份
        `model_dump()`。tree 这一支多出来任何字段（哪怕一个恒为 0 的 `edges`），
        这条用例就会红——这正是"现有行为一字不改"该有的守门方式。

        **例外只有一句**：外部检索开关在测试环境里是关的（`conftest.clean_env`），
        而"你关掉了联网检索…"是后来加的用户可见行为。它与此处钉的东西（`diagram_type`
        这一支的产出）无关，比对前剥掉；它自己的守卫在
        `test_one_path.test_external_switch_off_says_it_was_switched_off`。
        剥的时候引用 `tools.EXTERNAL_DISABLED_NOTE` 而不是抄字面量——抄一份就成了
        第三处真相，下次改文案这里会静默失效。
        """
        from app.agent.tools import EXTERNAL_DISABLED_NOTE

        baseline = _load_baseline_tools()

        _GENERATION_SNAPSHOT.seq = _uuid_sequence()
        old_content, old_data, old_tree = _generation_snapshot(baseline, diagram_args={})

        from app.agent import tools as new_tools
        _GENERATION_SNAPSHOT.seq = _uuid_sequence()
        new_content, new_data, new_tree = _generation_snapshot(
            new_tools, diagram_args={"diagram_type": "tree"})

        assert EXTERNAL_DISABLED_NOTE not in old_content, (
            "基线版本不该有这句话——有了说明基线选错了，剥掉就等于掩盖")
        new_content = new_content.replace(EXTERNAL_DISABLED_NOTE, "")
        # `data.diagnostics` 里同一条也得滤掉；浅拷贝是为了不改动被比的产物本身。
        comparable = {
            **new_data,
            "diagnostics": [item for item in new_data["diagnostics"]
                            if item["code"] != "GENERATION_EXTERNAL_DISABLED"],
        }
        assert len(comparable["diagnostics"]) == len(new_data["diagnostics"]) - 1, \
            "这条过滤本身要能被验证：诊断里确实少了一条"

        assert new_content == old_content
        assert json.dumps(comparable, ensure_ascii=False, sort_keys=True) == \
               json.dumps(old_data, ensure_ascii=False, sort_keys=True)
        assert json.dumps(new_tree, ensure_ascii=False, sort_keys=True) == \
               json.dumps(old_tree, ensure_ascii=False, sort_keys=True)
        # 树上确实没有多出一"半"东西：edges 是空列表。
        assert new_tree["edges"] == []

    def test_plan_prompt_is_unchanged_and_the_ask_only_changed_at_the_tail(self):
        """骨架 system 一字不差；user 只**收尾那一句**变了（配额 → 判据）。

        这条以前比的是"整条 user 与基线逐字节相等"。本次改动把"最多 N 条"那两句撤掉了
        （`tools.NO_BRANCH_CAP_RULE` 上面记着原因：那个 N 的唯一来源是模型读到材料之前
        自己填的工具参数），基线那份 `_plan_ask` 连签名都不同了，所以"整条相等"这条
        口径本身失效。

        留下的是仍然成立、也仍然值得守的三件事：system 逐字节不变；`tree` 这个默认
        图型一个字都不加；**收尾之前那段（主题 + 目录块）与基线逐字节相同**——本次改的
        只是收尾，前面一个字都不该动。
        """
        baseline = _load_baseline_tools()
        from app.agent import tools as new_tools

        assert new_tools.PLAN_PROMPT == baseline.PLAN_PROMPT
        assert new_tools._plan_ask("主题") == new_tools._plan_ask(
            "主题", diagram_type="tree")
        prefix = baseline._plan_ask("主题", 6, outline="目录").split("根据内容实际包含")[0]
        assert prefix.strip()          # 基线那一份确实有前缀可比
        assert new_tools._plan_ask("主题", outline="目录").startswith(prefix)


# ---------------------------------------------------------------- 细粒度工具

TREE = {
    "title": "示例",
    "root": {
        "id": "root", "label": "根", "children": [
            {"id": "a", "label": "甲", "children": [
                {"id": "a1", "label": "甲一", "children": []},
            ]},
            {"id": "b", "label": "乙", "children": []},
        ],
    },
}


def _edit_ctx(**extra) -> RunContext:
    parsed = MindMapContent.model_validate({**TREE, **extra})
    request = AgentRunRequest(
        goal="改这张图", intent="edit",
        budget=RunBudget(max_model_calls=5, max_output_tokens=32768),
    )
    return RunContext(request=request, candidate=parsed)


def _tool(name: str, ctx: RunContext, **arguments):
    return _run(build_registry("edit", has_library=False).execute(
        _call(name, **arguments), ctx))


def _untouched(ctx: RunContext, before: MindMapContent) -> None:
    assert ctx.candidate is before
    assert ctx.candidate.model_dump() == before.model_dump()
    assert ctx.batches == []
    assert ctx.revision == 0


class TestEdgeTools:
    def test_add_edge_lands_and_reports_id(self):
        ctx = _edit_ctx()
        before = ctx.candidate
        result = _tool("add_edge", ctx, source="a", target="b", label="是", kind="branch")
        assert result.ok, result.content
        assert ctx.candidate is not before
        assert [(item.source, item.target, item.label, item.kind)
                for item in ctx.candidate.edges] == [("a", "b", "是", "branch")]
        assert result.data["edge_id"] == ctx.candidate.edges[0].id
        assert result.data["edge_id"].startswith("e_")

    def test_add_edge_is_atomic_on_failure(self):
        """三种失败都必须让候选树逐字节不变。"""
        cases = [
            ({"source": "a", "target": "ghost"}, "node_not_found"),
            ({"source": "a", "target": "a"}, "self_loop"),
            ({"source": "a", "target": "b", "label": "是", "kind": "next"}, "invalid_tool_args"),
        ]
        for arguments, code in cases:
            ctx = _edit_ctx()
            before = ctx.candidate
            result = _tool("add_edge", ctx, **arguments)
            assert result.ok is False
            assert result.data["code"] == code, (arguments, result.data)
            _untouched(ctx, before)

    def test_duplicate_edge_is_rejected_with_a_hint(self):
        ctx = _edit_ctx(edges=[edge("e1", "a", "b", "是", "branch")])
        result = _tool("add_edge", ctx, source="a", target="b", label="是")
        assert result.ok is False
        assert result.data["code"] == "duplicate_edge"
        assert "label" in result.content

    def test_parallel_edges_survive_when_labels_differ(self):
        ctx = _edit_ctx(edges=[edge("e1", "a", "b", "是", "branch")])
        result = _tool("add_edge", ctx, source="a", target="b", label="否", kind="branch")
        assert result.ok, result.content
        assert sorted(item.label for item in ctx.candidate.edges) == ["否", "是"]

    def test_cycles_are_allowed(self):
        ctx = _edit_ctx(edges=[edge("e1", "a", "b", "通过", "flow")])
        result = _tool("add_edge", ctx, source="b", target="a", label="驳回", kind="loop")
        assert result.ok, result.content
        assert {item.kind for item in ctx.candidate.edges} == {"flow", "loop"}

    def test_edge_limit_is_enforced_by_the_tool(self):
        """第 201 条边明确拒绝（而不是等保存时被 422 打回）。"""
        edges = [edge(f"e{index}", "a", "b", str(index)) for index in range(MAX_EDGES)]
        ctx = _edit_ctx(edges=edges)
        result = _tool("add_edge", ctx, source="a", target="b", label="第201条")
        assert result.ok is False
        assert result.data["code"] == "too_many_edges"

    def test_remove_edge_is_atomic(self):
        ctx = _edit_ctx(edges=[edge("e1", "a", "b", "是", "branch")])
        before = ctx.candidate
        result = _tool("remove_edge", ctx, edge_id="nope")
        assert result.ok is False
        assert result.data["code"] == "edge_not_found"
        _untouched(ctx, before)

    def test_remove_edge_keeps_the_tree(self):
        ctx = _edit_ctx(edges=[edge("e1", "a", "b", "是", "branch")])
        result = _tool("remove_edge", ctx, edge_id="e1")
        assert result.ok, result.content
        assert ctx.candidate.edges == []
        assert ctx.candidate.root.model_dump() == MindMapContent.model_validate(TREE).root.model_dump()

    def test_remove_node_deletes_its_edges(self):
        """删节点连带删边：源或目标命中**整棵子树**（含子孙）。"""
        ctx = _edit_ctx(edges=[
            edge("e1", "a1", "b", "汇合"),      # 端点在子孙上
            edge("e2", "b", "a", "回退", "loop"),  # 端点在祖先上
            edge("e3", "root", "b", "顺序"),     # 不命中，必须留下
        ])
        result = _tool("remove_node", ctx, node_id="a")
        assert result.ok, result.content
        assert [item.id for item in ctx.candidate.edges] == ["e3"]
        assert sorted(result.data["removed_edges"]) == ["e1", "e2"]
        assert result.data["removed_edge_count"] == 2
        assert "附加关系" in result.content

    def test_remove_node_description_mentions_edges(self):
        """行为写进了描述：模型看不到画布，不说它会以为线还在。"""
        spec = build_registry("edit", has_library=False).get("remove_node")
        assert "附加关系" in spec.description

    def test_edge_tools_are_not_reachable_from_create_or_expand(self):
        for intent in ("create", "expand"):
            registry = build_registry(intent, has_library=False)
            result = _run(registry.execute(_call("add_edge", source="a", target="b"), _edit_ctx()))
            assert result.ok is False
            assert result.data["code"] == "unknown_tool"


# ---------------------------------------------------------------- 导出


def _export_content(**extra) -> MindMapContent:
    return MindMapContent.model_validate(content(
        edges=[
            edge("e1", "b", "c", "是", "branch"),
            edge("e2", "b", "a", "驳回", "loop"),
        ],
        **extra,
    ))


class TestExport:
    def test_puml_carries_edges_as_relation_lines(self):
        text = to_puml(_export_content())
        assert "@endmindmap" in text
        # 关系行的原文形态：`A --> B : label`（前缀是 PlantUML 注释符，保住文件的
        # 可解析性；行内文字仍然是需求里的那一段）。
        assert "形式审查 --> 付款 : 是" in text
        assert "形式审查 --> 提交申请 : 驳回" in text

    def test_markdown_has_a_dedicated_section(self):
        text = to_markdown(_export_content())
        assert "## 附加关系" in text
        assert "- 形式审查 → 付款（是）" in text
        assert "- 形式审查 → 提交申请（驳回）" in text
        # 不硬塞进树：那一节在末尾，树行仍然是缩进列表。
        assert text.index("## 附加关系") > text.index("    - 付款")

    def test_opml_has_a_sibling_outline(self):
        import xml.etree.ElementTree as ET

        root = ET.fromstring(to_opml(_export_content()))
        sections = [node.get("text") for node in root.findall("./body/outline")]
        assert "报销" in sections
        assert "附加关系" in sections
        extra = [node for node in root.findall("./body/outline")
                 if node.get("text") == "附加关系"][0]
        assert [item.get("text") for item in extra.findall("./outline")] == [
            "形式审查 → 付款（是）", "形式审查 → 提交申请（驳回）",
        ]

    def test_formats_are_unchanged_without_edges(self):
        """没有边的图：markdown / opml 与加 `edges` 之前逐字节相同。

        **PUML 多了一行图型注释**，那是有意的：`mindmap` 语法分不出流程图与思维
        导图（形状可以一模一样），拿到文件的人只能从这一行知道它本来是什么。
        除这一行之外，文件与之前一字不差。
        """
        plain = MindMapContent.model_validate(content())
        text = to_puml(plain)
        assert text.replace("' 图型：思维导图（tree）\n", "") == \
            "@startmindmap\n* 报销\n** 提交申请\n** 形式审查\n** 付款\n@endmindmap\n"
        assert text.split("\n")[1] == "' 图型：思维导图（tree）"
        assert to_markdown(plain) == (
            "# 报销流程\n\n- 报销\n    - 提交申请\n    - 形式审查\n    - 付款\n")
        assert "附加关系" not in to_opml(plain)


# ---------------------------------------------------------------- 不回归


def test_existing_fields_still_round_trip():
    """`expandable` / `source_refs` / `detail` / `color` / `icon` 一个都没丢。"""
    raw = {
        "title": "带视觉与引用的图",
        "root": {
            "id": "root", "label": "根", "expandable": "no",
            "color": "blue", "icon": "🎯", "detail": "根节点的说明",
            "children": [],
        },
        "source_usage": [{"source_id": "s1", "kind": "text", "filename": None, "char_count": 10}],
        "edges": [edge("e1", "root", "root2")],  # 会被 dangling 拒收
    }
    with pytest.raises(ValidationError):
        MindMapContent.model_validate(raw)
    del raw["edges"]
    raw["root"]["children"] = [{
        "id": "child", "label": "子", "expandable": "yes",
        "source_refs": [{"source_id": "s1", "start": 0, "end": 2, "quote": "人工智能"}],
    }]
    parsed = MindMapContent.model_validate(raw)
    dumped = parsed.model_dump()
    assert dumped["root"]["color"] == "blue"
    assert dumped["root"]["icon"] == "🎯"
    assert dumped["root"]["detail"] == "根节点的说明"
    assert dumped["root"]["expandable"] == "no"
    assert dumped["root"]["children"][0]["source_refs"][0]["quote"] == "人工智能"
    assert dumped["edges"] == []
