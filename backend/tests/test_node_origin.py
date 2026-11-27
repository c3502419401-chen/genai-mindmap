"""`Node.origin`：这个节点是**怎么写进来的**。

字段存在的理由只有一条：**把"这张图有 12 个节点没有引用"拆成两类，而这两类的
下一步动作不同**——生成链路写的没找到出处 → 补材料重生成；不来自生成链路 → 本来
就没有原文可引，不必处理。所以本文件不测"字段存下来了"，只测下面这些能被读错的
地方：

  1. **三种形状**（树 / 鱼骨 / 流程）的生成产物每一层都是 `generated`——三种形状
     共用同一个 `Node`，所以字段只有一处，但折叠会换容器，漏了就只剩树有；
  2. **分片生成那条路**：它借 `apply_ops` 落树，而 op 执行器把 add 出来的节点记成
     `manual`（编辑轮的语义）。收尾不刷一遍的话，一张每支都检索过、每条引用都逐字
     核过的图会被界面说成"不来自生成链路——不必处理"。**这是本文件最重要的一条**；
  3. **手动加**（`add_node` 工具与前端手加）是 `manual`；
  4. **导入**：我们自己导出的 JSON 逐节点不变，第三方格式与没有这个字段的文件是
     `unknown`（"不知道"是真话，猜成 `manual` 才是撒谎）；
  5. **压链**：存活的节点继承**它自己的**来历，不是外层的、也不是"repair"；
     撤销时两个节点的来历各回各位；
  6. **不可伪造**：模型写不进这个字段（`AddOp` / `UpdateOp` 都是 `extra="forbid"`）。
"""
from __future__ import annotations

import asyncio

import pytest

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.ops import apply_ops
from app.agent.tools import PLAN_PROMPT, _shape_output, build_registry
from app.contracts.mindmap import (
    MAX_TREE_DEPTH,
    FishboneContent,
    FlowContent,
    MindMapContent,
    Node,
)
from app.contracts.ops import OpBatch
from app.export.service import to_json
from app.importing.service import parse_import
from app.optimization.repair import repair, revert

# ---------------------------------------------------------------- 1. 生成：三种形状


def _tree(branches: int = 3, depth: int = 2) -> MindMapContent:
    """一棵每个节点都没有来历的树（模拟"生成过程里刚建出来"的状态）。"""

    def build(prefix: str, level: int) -> Node:
        return Node(
            id=prefix,
            label=prefix,
            children=[build(f"{prefix}-{index}", level + 1) for index in range(2)]
            if level < depth else [],
        )

    return MindMapContent.model_validate({
        "title": "图",
        "root": {
            "id": "root", "label": "root",
            "children": [{"id": f"b{index}", "label": f"b{index}", "children": []}
                         for index in range(branches)],
        },
    })


def _roots_of(content) -> list[Node]:
    """三种形状的子树根。**手写而不是借 `shapes.walk_depths`**：那正是被测代码用的
    遍历器，用它来断言等于自己证明自己。"""
    if isinstance(content, MindMapContent):
        return [content.root]
    if isinstance(content, FishboneContent):
        return [bone.root for bone in content.bones]
    assert isinstance(content, FlowContent)
    return [stage.root for stage in content.stages]


def _all_origins(content) -> set[str]:
    return {node.origin for root in _roots_of(content) for node in _walk(root)}


@pytest.mark.parametrize("diagram_type", ["tree", "flowchart", "fishbone"])
def test_shape_output_stamps_every_node_generated(diagram_type):
    """出口那一步把整棵树刷成 `generated`，三种形状都要刷到。

    三种形状共用同一个 `Node`，但折叠把节点搬进了 `bones[i].root` /
    `stages[i].root`——刷在折叠之后就要写两遍，所以它必须刷在**折之前**。
    """
    shaped = _shape_output(
        _tree(), diagram_type=diagram_type,
        raw_plan={"problem": "为什么慢"}, diagnostics=[])

    assert type(shaped).__name__ in {
        "MindMapContent": "MindMapContent",
        "FishboneContent": "FishboneContent",
        "FlowContent": "FlowContent",
    }
    assert _all_origins(shaped) == {"generated"}


def test_fishbone_bones_keep_the_origin_of_the_branch_node():
    """鱼骨的骨根就是那一级分支节点本身（不是复制），所以来历跟着一起过去。"""
    shaped = _shape_output(
        _tree(), diagram_type="fishbone", raw_plan={"problem": "为什么慢"}, diagnostics=[])

    assert isinstance(shaped, FishboneContent)
    assert [bone.root.origin for bone in shaped.bones] == ["generated"] * len(shaped.bones)


def test_flow_stages_keep_the_origin_of_the_branch_node():
    shaped = _shape_output(
        _tree(), diagram_type="flowchart", raw_plan={}, diagnostics=[])

    assert isinstance(shaped, FlowContent)
    assert [stage.root.origin for stage in shaped.stages] == ["generated"] * len(shaped.stages)


# ------------------------------------------------- 2. 分片生成：不能落成 manual

BRANCHES = 3
TITLES = [f"分支{index}" for index in range(BRANCHES)]


def _deep_ctx() -> RunContext:
    from app.contracts.source import Source, SourceKind

    sources = [Source(id="doc", kind=SourceKind.document, status="parsed",
                      text="预算由财务经理核准，超过五十万元的支出报总经理审批。")]
    request = AgentRunRequest(
        goal="整理管理办法", intent="create", sources=sources,
        budget=RunBudget(max_model_calls=20, max_tool_calls=4, max_output_tokens=32768),
    )
    return RunContext(request=request, candidate=None,
                      sources_by_id={source.id: source for source in sources})


def _run_deep(monkeypatch) -> RunContext:
    """跑一次真的 `mindmap_generate_deep`：骨架 → 每支展开 → 落树。

    节点是 `apply_ops` 落的（编辑轮那条路，缺省 `manual`），所以这条用例同时钉住
    "收尾那一刷**必须存在**"。
    """
    monkeypatch.setenv("MINDMAP_SOURCE_MODE", "rag")
    monkeypatch.setenv("MINDMAP_LIBRARY_SEARCH", "0")

    async def search(_sources, _title, _intents=()):
        return []

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(TITLES)}
        return {"children": [{"label": f"{user[-6:]}要点", "expandable": False}]}

    monkeypatch.setattr("app.agent.tools._search_branch", search)
    monkeypatch.setattr("app.agent.tools.chat_async", chat)

    ctx = _deep_ctx()
    registry = build_registry("create_deep", has_sources=True)
    result = asyncio.run(registry.execute(
        ToolCall(call_id="c-origin", name="mindmap_generate_deep",
                 arguments={"topic": "管理办法", "branches": BRANCHES,
                            "points_per_branch": 2}),
        ctx,
    ))
    assert result.ok, result.content
    return ctx


def test_deep_generation_is_generated_not_manual(monkeypatch):
    """分片路的节点是**生成产出**，哪怕它是 `apply_ops` 落的。

    这一条能红的方式很具体：把 `_generate_deep` 收尾那句 `_stamp_origin(content)`
    删掉，`apply_ops` 的缺省 `manual` 就是唯一的值——于是界面会对一张刚生成出来的
    图说"不来自生成链路，本来就没有原文可引，不必处理"。
    """
    ctx = _run_deep(monkeypatch)

    nodes = list(_walk(ctx.candidate.root))
    assert nodes, "前提：这次生成要真的落了节点"
    assert {node.origin for node in nodes} == {"generated"}
    # 根与一级分支也是 generated（不是只有叶子）
    assert ctx.candidate.root.origin == "generated"


def _walk(node: Node):
    yield node
    for child in node.children:
        yield from _walk(child)


# ---------------------------------------------------------------- 3. 手动加


def _edit_ctx(candidate: MindMapContent) -> RunContext:
    request = AgentRunRequest(
        goal="改这张图", intent="edit",
        budget=RunBudget(max_model_calls=5, max_output_tokens=32768),
    )
    return RunContext(request=request, candidate=candidate)


def _run_tool(name: str, ctx: RunContext, **arguments):
    registry = build_registry("edit", has_library=False)
    return asyncio.run(registry.execute(
        ToolCall(call_id=f"c-{name}", name=name, arguments=arguments), ctx))


def _sample() -> MindMapContent:
    return MindMapContent.model_validate({
        "title": "示例",
        "root": {"id": "root", "label": "根", "origin": "generated",
                 "children": [{"id": "a", "label": "甲", "children": [],
                               "origin": "generated"}]},
    })


def test_add_node_tool_writes_manual():
    """编辑轮里 `add_node` 加的节点记 `manual`（= **不属于任何一次生成产物**）。

    这个值不是"人打的"，判据是"它有没有被某一次生成写出来"：它没有骨架、没有检索、
    没有引用核验，所以界面**不能**对它说"模型没在材料里找到对应句子，补材料再生成
    一次"——重新生成一次这个节点根本不会出现。
    """
    ctx = _edit_ctx(_sample())

    result = _run_tool("add_node", ctx, parent_id="a", label="新加的")
    assert result.ok, result.content

    added = result.data["node_id"]
    nodes = {node.id: node for node in _walk(ctx.candidate.root)}
    assert nodes[added].origin == "manual"
    # 树上原有的节点不动：一次编辑不该把生成产出改写成手加
    assert nodes["root"].origin == "generated"
    assert nodes["a"].origin == "generated"


def test_apply_ops_add_is_manual_and_update_cannot_touch_origin():
    """op 执行器是"编辑轮"的语言：add 落 `manual`，且**没有任何 op 能改 origin**。"""
    content = _sample()
    batch = OpBatch.model_validate({
        "batch_id": "b1", "base_revision": 0,
        "ops": [{"op": "add", "id": "n1", "parent_id": "a", "index": 0,
                 "label": "op 加的"}],
    })
    after = apply_ops(content, batch)
    assert after.root.children[0].children[0].origin == "manual"

    # `AddOp` / `UpdateOp` 都是 extra="forbid"：模型在草案里写 origin 会被整批拒掉。
    for draft in (
        {"op": "add", "id": "n2", "parent_id": "a", "index": 0, "label": "x",
         "origin": "generated"},
        {"op": "update", "id": "a", "changes": {"origin": "manual"}},
    ):
        with pytest.raises(Exception):
            OpBatch.model_validate({"batch_id": "b2", "base_revision": 0, "ops": [draft]})


# ---------------------------------------------------------------- 4. 导入


def _json_bytes(payload: dict) -> bytes:
    import json

    return json.dumps(payload).encode("utf-8")


def test_import_our_own_export_keeps_every_origin():
    """导出再导入一次，逐节点不变——`origin` 是文件里的事实，不是导入时猜的。"""
    content = _sample()
    content.root.children[0].origin = "manual"
    content.root.children[0].children.append(
        Node(id="old", label="旧节点", origin="unknown"))

    exported = to_json(content)
    imported = parse_import("map.json", exported.encode("utf-8")).content

    def origins(root: Node) -> list[str]:
        return [node.origin for node in _walk(root)]

    assert origins(imported.root) == origins(content.root)
    assert origins(imported.root) == ["generated", "manual", "unknown"]


def test_import_json_without_origin_is_unknown():
    """旧导出（这个字段之前存的）读进来是 `unknown`，且能原样保存回去。"""
    payload = {
        "title": "旧图",
        "root": {"id": "r", "label": "根",
                 "children": [{"id": "a", "label": "甲", "children": []}]},
    }
    imported = parse_import("old.json", _json_bytes(payload)).content

    assert imported.root.origin == "unknown"
    assert imported.root.children[0].origin == "unknown"
    # 存回去：契约能收，序列化带着它（界面按 `?? 'unknown'` 读，两条路一致）
    saved = imported.model_dump(mode="json")
    assert saved["root"]["origin"] == "unknown"


def test_import_third_party_format_is_unknown():
    """第三方格式里没有"谁写的"这个概念：读成 `unknown`，不猜成 `manual`。

    猜成 `manual` 会让界面在导进来的图上说"这个节点不来自生成链路"——那句话
    也许对，但它是我们编的，不是文件说的。而"不知道"恰好是真话。
    """
    markdown = b"# \xe6\xa0\x87\xe9\xa2\x98\n\n- \xe7\x94\xb2\n  - \xe7\x94\xb2\xe4\xb8\x80\n"
    imported = parse_import("outline.md", markdown).content

    assert {node.origin for node in _walk(imported.root)} == {"unknown"}


def test_import_rejects_an_origin_outside_the_set():
    """文件里写了个我们不认识的值：退回 `unknown`，而不是让整份导入 422。"""
    payload = {
        "title": "怪值",
        "root": {"id": "r", "label": "根", "origin": "synthetic", "children": []},
    }
    imported = parse_import("weird.json", _json_bytes(payload)).content

    assert imported.root.origin == "unknown"


# ---------------------------------------------------------------- 5. 压链


def _chain(outer: str, inner: str, **inner_extra) -> MindMapContent:
    """`根 → 外层(单子) → 内层(有内容)`：一次 `SINGLE_CHILD` 压链的形状。

    外层与内层的来历故意写得不一样，才能看出压链之后留下的是谁的。
    """
    return MindMapContent.model_validate({
        "title": "压链",
        "root": {"id": "r", "label": "根", "origin": "generated", "children": [
            {"id": "outer", "label": "外层", "origin": outer, "children": [
                {"id": "inner", "label": "内层一段有内容的话", "origin": inner,
                 "children": [], **inner_extra},
            ]},
            {"id": "keep", "label": "另一支", "children": [
                {"id": "k1", "label": "叶", "children": []}]},
        ]},
    })


@pytest.mark.parametrize(("outer", "inner"), [
    ("generated", "generated"),
    ("manual", "generated"),
    ("generated", "manual"),
    ("unknown", "generated"),
])
def test_collapse_chain_keeps_the_inner_origin(outer, inner):
    """压链后存活的是**内层**（它的 id / label / 子节点都留着），所以来历也归它。

    `origin` 说的是"这个节点从哪来"，不是"它被谁改过"——所以这里既不取外层的，也
    不写一个 `repair`。写入一个"被修过"的值等于让字段同时答两个问题，而撤销数据
    （`RepairUndo` 里两份全字段快照）已经把这次改动记得很清楚了。
    """
    content = _chain(outer, inner)

    result = repair(content)
    folded = result.content.root.children[0]
    assert folded.id == "inner"
    assert folded.origin == inner
    # 没被碰的那一支保持原样（缺字段就是 unknown）
    assert result.content.root.children[1].origin == "unknown"


def test_collapse_keeps_the_inner_origin_verbatim():
    """压链之后内层的来历**保持原值**，且**不得**退化成 `unknown`。

    这条是 `repair.py` 那处改动（压链要写 `origin=inner.origin`）的**条件**，
    两条断言分别拦两种不同的改法，缺一条就漏一种：

      - `== "generated"`（原值）拦的是"顺手改回继承外层"：这次外层是 `manual`，
        所以取外层的值会在这里红；
      - `!= "unknown"` 拦的是"两行都不写"：`Node` 的缺省值正好是 `unknown`，于是
        每一次压链都把来历静默抹掉——界面上表现为"修完之后这张图变旧了"，一句听起来
        像能力限制的假话，而且没有任何红灯。

    与 `test_collapse_chain_keeps_the_inner_origin` 的分工：那条走**四种内外层组合**
    的矩阵（覆盖"取谁"），这条只钉**最容易被下一次重构弄坏**的一种取值，并要求它
    不只是"不为空"。
    """
    result = repair(_chain("manual", "generated"))
    folded = result.content.root.children[0]

    assert folded.origin == "generated", "内层是 generated，压链后必须还是它"
    assert folded.origin != "unknown", "缺省值生效了：每次压链都会把来历抹掉"


def test_collapse_keeps_a_manual_inner_origin_too():
    """内层是 `manual` 时同样保持原值——防止上一条"恰好等于 generated"就过。

    这一对（这条 + 上一条）合起来才说明压链取的是**内层**：只测 `generated` 的话，
    "永远写 generated" 也能全绿。
    """
    result = repair(_chain("generated", "manual"))
    folded = result.content.root.children[0]

    assert folded.origin == "manual"
    assert folded.origin != "unknown"


def test_revert_restores_both_origins():
    """撤销把两层的来历各还原：`origin` 是可撤销改动的一部分，不是只读装饰。"""
    content = _chain("manual", "generated")

    result = repair(content)
    restored = revert(result.content, result.applied)

    assert restored.root.children[0].origin == "manual"
    assert restored.root.children[0].children[0].origin == "generated"


# ---------------------------------------------------------------- 6. 老数据


def test_old_payload_without_origin_validates_and_round_trips():
    """库里那些旧图（没有这个字段）读出来是 `unknown`，保存回去不丢结构。

    零迁移的前提是**新字段有默认值**（与 `expandable` 同一条规矩）：老 JSON 能过
    契约校验，保存时序列化出来带 `unknown`，再读回来还是 `unknown`。
    """
    old = {
        "title": "三天前的图",
        "root": {"id": "r", "label": "根", "expandable": "no", "children": [
            {"id": "a", "label": "甲", "note": "我写的备注", "children": []},
        ]},
    }
    content = MindMapContent.model_validate(old)
    assert content.root.origin == "unknown"

    again = MindMapContent.model_validate(content.model_dump())
    assert again.root.origin == "unknown"
    assert again.root.expandable == "no"
    assert again.root.children[0].note == "我写的备注"


def test_depth_limit_constant_is_unrelated_but_present():
    """一条便宜的自检：本文件用的是真契约上限，不是硬编码的 8。"""
    assert MAX_TREE_DEPTH == 8
