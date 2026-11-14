"""按需展开单个节点（`node_expand`）与 `expandable` 三态。

这一轮要消灭的形态是"一次生成到底"：初次只出 4 层（`INITIAL_MAX_DEPTH`），往下的
内容由用户**点哪个节点就展开哪个**。于是 `node_expand` 不再是一个"能加子节点就行"
的工具，它要同时守住四件事，每条都有对应用例：

  1. **路径当上下文**：给模型的是「根 → … → 目标节点」的标题链，不是一个孤立标题；
  2. **同级去重**：已有的子节点（归一化后）不能重复出现；
  3. **原子应用**：任何一个节点不合契约就整批回退，树上不出现"挂了一半"；
  4. **限额前置**：1000 节点 / 8 层在**调用模型之前**判，别花了钱才发现放不下。

另外两件是这一轮新接的：

  - **材料找回**：重新打开旧导图时前端没有正文，`request.sources` 是空的；这时按
    目标节点 `source_refs` 里的持久 id 从知识库把来源找回来（`library.load`）。
    库里也没有了（被 `prune` 删掉）→ 降级成"无证据展开"，照常生成、不写引用，
    并把这件事写进 `content`。
  - **`expandable` 三态**：`yes` / `no` / `unknown` 落库，界面据此画按钮。被额度
    截断的**不是** `no`——那是"没展开完"，不是"没得展开"。

模型一律用 mock（工具里值得测的是"拿到结果之后怎么落树"）。检索是真的：`rag` 形态
与 `search_parent` 都跑本地 BM25，不发任何网络请求（`conftest` 已清掉凭证）。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.contracts import (
    AgentRunRequest,
    Completion,
    DoneEvent,
    NodeGrowEvent,
    OpsEvent,
    RunBudget,
    RunContext,
    ToolCall,
)
from app.agent.service import run
from app.agent.tools import (
    PLAN_PROMPT,
    _node_path,
    _parse_expand_items,
    _stamp_expandable,
    _ExpandInvalid,
    build_registry,
)
from app.contracts.mindmap import (
    GENERATED_LABEL_MAX_CHARS,
    MAX_TREE_DEPTH,
    MAX_TREE_NODES,
    NODE_EXPANDABLE,
    MindMapContent,
    Node,
)
from app.contracts.source import Source, SourceKind
from app.knowledge import library

# 一份能召回的材料：查询是「风险 + 测试方案」，这段正文两个词都含，BM25 必有命中。
SOURCE_TEXT = "测试方案的风险控制：先做冒烟测试，再做回归测试。预算审批由财务复核。"

# 目标节点挂在 3 层的路径上：根 > 测试 > 风险，用来断言标题链进了提示词。
TREE = {
    "title": "测试方案",
    "root": {
        "id": "root", "label": "测试方案", "children": [
            {"id": "t1", "label": "验收", "children": [
                {"id": "risk", "label": "风险", "children": []},
            ]},
        ],
    },
}


def _content(tree: dict | None = None) -> MindMapContent:
    return MindMapContent.model_validate(tree or TREE)


def _source(text: str = SOURCE_TEXT, **extra: Any) -> Source:
    return Source(kind=SourceKind.text, status="parsed", text=text, **extra)


def _ctx(
    candidate: MindMapContent | None = None,
    *,
    intent: str = "expand",
    sources: list[Source] | None = None,
    selected: list[str] | None = None,
    max_model_calls: int = 10,
) -> RunContext:
    sources = sources or []
    request = AgentRunRequest(
        goal="展开这一支", intent=intent, sources=sources,
        selected_ids=selected if selected is not None else ["risk"],
        budget=RunBudget(max_model_calls=max_model_calls, max_output_tokens=32768),
    )
    return RunContext(
        request=request,
        candidate=candidate,
        sources_by_id={source.id: source for source in sources},
    )


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(call_id=f"c-{name}", name=name, arguments=arguments)


def _run(coro):
    return asyncio.run(coro)


def _expand(ctx: RunContext, fake, *, node_id: str = "risk", count: int = 3,
            instruction: str | None = None):
    """跑一次 `node_expand`：`fake` 是模型替身（可以是 AsyncMock）。"""
    arguments: dict[str, Any] = {"node_id": node_id, "count": count}
    if instruction is not None:
        arguments["instruction"] = instruction
    with patch("app.agent.tools.chat_async", new=fake):
        return _run(build_registry("expand").execute(_call("node_expand", **arguments), ctx))


def _children(*labels: str) -> dict:
    return {"children": [{"label": label, "detail": f"{label}的说明"} for label in labels]}


def _evidence_in(user: str) -> list[dict]:
    """从 user 消息里取出这次检索到的 evidence 数组（`_expand` 拼在末尾）。"""
    marker = "evidence 数组：\n"
    if marker not in user:
        return []
    return json.loads(user.split(marker, 1)[1])


def _ref_from_evidence(user: str) -> dict | None:
    """按模型该做的那样，从 evidence 里抄一条引用出来。"""
    evidence = _evidence_in(user)
    if not evidence:
        return None
    entry = evidence[0]
    return {"source_id": entry["source_id"], "quote": entry["quote"][:60]}


def _risk(content: MindMapContent) -> Node:
    return content.root.children[0].children[0]


# ---------------------------------------------------------------- 入参与产出


def test_expand_appends_two_to_four_children_with_detail():
    """一次展开追加 2–4 个子节点，每个都带 label 与 detail。"""
    ctx = _ctx(_content())
    result = _expand(ctx, AsyncMock(return_value=_children("冒烟", "回归", "压力")), count=3)

    assert result.ok, result.content
    node = _risk(ctx.candidate)
    assert [child.label for child in node.children] == ["冒烟", "回归", "压力"]
    # 两层节点：标题短、说明长。`detail` 与 `label` 一起落树，否则画布上又是一句长话。
    assert all(child.detail == f"{child.label}的说明" for child in node.children)
    # 产出契约：批次、id 映射、修订号都在 data 里（前端据此增量应用）
    assert result.data["labels"] == ["冒烟", "回归", "压力"]
    assert result.data["revision"] == 1
    assert len(result.data["batches"]) == 1
    assert result.data["path"] == "测试方案 > 验收 > 风险"


def test_count_out_of_range_is_rejected_before_any_work():
    """`count` 只允许 2–4：参数模型拦下，工具根本不执行。"""
    ctx = _ctx(_content())
    mocked = AsyncMock(return_value=_children("甲", "乙"))
    result = _expand(ctx, mocked, count=9)
    assert result.ok is False
    assert result.data["code"] == "invalid_tool_args"
    mocked.assert_not_called()


def test_children_get_source_refs_on_the_node_not_in_detail():
    """引用挂在节点上，不写进 detail：写进正文的引用收尾校验看不到，界面上也点不开。"""
    source = _source()
    ctx = _ctx(_content(), sources=[source])

    async def fake(system, user, *, max_tokens=None):
        ref = _ref_from_evidence(user)
        return {"children": [
            {"label": "风险清单", "detail": "先跑冒烟测试。", "source_refs": [ref] if ref else []},
            {"label": "回归范围", "detail": "回归覆盖核心链路。"},
        ]}

    result = _expand(ctx, AsyncMock(side_effect=fake), count=2)

    assert result.ok, result.content
    node = _risk(ctx.candidate)
    first, second = node.children
    assert first.source_refs, "命中了材料却没有挂引用"
    assert first.source_refs[0].source_id == source.id
    # 引文不进 detail（两个字段各管各的显示区）
    assert "测试方案的风险控制" not in (first.detail or "")
    assert second.source_refs == []
    assert result.data["refs"]["kept"] == len(first.source_refs)


# ---------------------------------------------------------------- 路径上下文


def test_path_chain_reaches_both_system_and_user_prompt():
    """标题链必须进 prompt：一个孤立的 16 字标题在几乎任何主题下都成立。"""
    ctx = _ctx(_content())
    seen: list[tuple[str, str]] = []

    async def fake(system, user, *, max_tokens=None):
        seen.append((system, user))
        return _children("甲", "乙")

    assert _expand(ctx, AsyncMock(side_effect=fake), count=2).ok
    system, user = seen[0]
    for text in (system, user):
        assert "测试方案 > 验收 > 风险" in text
    # 目标节点与同级已有子节点也要在里面：模型看不到自己上一轮写过什么
    assert "要展开的节点：风险" in user
    assert "它已有的子节点：（空）" in user


def test_instruction_is_passed_through_to_the_prompt():
    """用户填的补充要求要能到达模型，否则界面上那个输入框等于没接。"""
    seen: list[str] = []

    async def fake(system, user, *, max_tokens=None):
        seen.append(user)
        return _children("甲", "乙")

    assert _expand(_ctx(_content()), AsyncMock(side_effect=fake), count=2,
                   instruction="只写落地动作").ok
    assert "补充要求：只写落地动作" in seen[0]


def test_path_helper_walks_from_the_root():
    """`_node_path` 给的是根到目标的整条链（含两端）。"""
    content = _content()
    chain = _node_path(content.root, "risk")
    assert [node.label for node in chain or []] == ["测试方案", "验收", "风险"]
    assert _node_path(content.root, "ghost") is None


# ---------------------------------------------------------------- 同级去重


def test_existing_sibling_title_is_not_added_twice():
    """已有的子节点（归一化后）不能再出现一次。"""
    tree = {
        "title": "t",
        "root": {"id": "root", "label": "根", "children": [
            {"id": "a", "label": "甲", "children": [
                {"id": "keep", "label": "冒烟测试", "children": []},
            ]},
        ]},
    }
    ctx = _ctx(_content(tree), selected=["a"])
    # 第二个只是标点与空格不同，归一化之后与已有子节点一模一样
    result = _expand(ctx, AsyncMock(return_value=_children("冒烟测试 ", "压力测试")),
                     node_id="a", count=2)

    assert result.ok, result.content
    labels = [child.label for child in ctx.candidate.root.children[0].children]
    assert labels == ["冒烟测试", "压力测试"]
    assert result.data["duplicates"] == [{"label": "冒烟测试", "existing": "冒烟测试"}]


def test_all_duplicates_is_a_failure_that_leaves_the_tree_alone():
    """整批都重复时不落树：挂一个"看起来展开了"的节点比报错更糟。"""
    tree = {
        "title": "t",
        "root": {"id": "root", "label": "根", "children": [
            {"id": "a", "label": "甲", "children": [
                {"id": "keep", "label": "冒烟测试", "children": []},
            ]},
        ]},
    }
    ctx = _ctx(_content(tree), selected=["a"])
    result = _expand(ctx, AsyncMock(return_value=_children("冒烟测试")),
                     node_id="a", count=2)

    assert result.ok is False
    assert result.data["code"] == "duplicate_points"
    assert [child.label for child in ctx.candidate.root.children[0].children] == ["冒烟测试"]


# ---------------------------------------------------------------- 原子应用


def test_one_bad_label_rolls_back_the_whole_batch():
    """一个 label 超长 → 整批不落树，树一个节点都没变。"""
    ctx = _ctx(_content())
    before = ctx.candidate.model_dump()
    too_long = "长" * (GENERATED_LABEL_MAX_CHARS + 1)
    result = _expand(ctx, AsyncMock(return_value=_children("合法的一个", too_long)), count=2)

    assert result.ok is False
    assert result.data["code"] == "label_too_long"
    assert ctx.candidate.model_dump() == before
    assert ctx.revision == 0
    assert ctx.batches == []


def test_blank_label_is_rejected_as_a_whole_batch():
    ctx = _ctx(_content())
    bad = {"children": [{"label": "正常的"}, {"label": "   "}]}
    result = _expand(ctx, AsyncMock(return_value=bad), count=2)
    assert result.ok is False
    assert result.data["code"] == "invalid_label"
    assert _risk(ctx.candidate).children == []


def test_malformed_refs_are_rejected_while_unbacked_refs_are_dropped():
    """形状错的引用判整批失败；形状对但证不出来的引用只丢掉。"""
    ctx = _ctx(_content(), sources=[_source()])

    async def malformed(system, user, *, max_tokens=None):
        return {"children": [
            {"label": "甲", "source_refs": [{"quote": "没有 source_id"}]},
            {"label": "乙"},
        ]}

    assert _expand(ctx, AsyncMock(side_effect=malformed), count=2).data["code"] == "invalid_refs"

    async def unbacked(system, user, *, max_tokens=None):
        # source_id 是编的：形状合法，但不在本次可见的来源里
        return {"children": [
            {"label": "甲", "source_refs": [{"source_id": "ghost", "quote": "编的"}]},
            {"label": "乙", "source_refs": [{"source_id": "ghost", "quote": "也编的"}]},
        ]}

    ctx = _ctx(_content(), sources=[_source()])
    result = _expand(ctx, AsyncMock(side_effect=unbacked), count=2)
    assert result.ok, result.content
    assert all(child.source_refs == [] for child in _risk(ctx.candidate).children)
    assert result.data["refs"]["kept"] == 0
    # 丢掉几条也要数得出来：界面上"这一支没有出处"与"被剔掉了"是同一屏，
    # 区别只能从 data 里看
    assert result.data["refs"]["dropped"] == 2
    codes = [item["code"] for item in result.data["diagnostics"]]
    assert "SOURCE_REFS_DROPPED" in codes


def test_parse_rejects_nested_shapes_it_does_not_understand():
    """`{"children":[{"name":"x"}]}` 说明模型没照格式写：宁可失败，也不把 dict 的
    repr 当成标题挂上去（那种"成功"比失败更难发现）。"""
    with pytest.raises(_ExpandInvalid) as caught:
        _parse_expand_items(
            {"children": [{"label": "甲"}, {"name": "没有 label"}]}, known=set())
    assert caught.value.code == "invalid_label"


# ---------------------------------------------------------------- 限额前置


def test_tree_full_is_decided_before_the_model_call():
    """整图 1000 个节点时**一次模型调用都不发**：额度花了收不回来。"""
    branches = [
        {"id": f"b{index}", "label": f"分支{index}", "children": [
            {"id": f"b{index}-{child}", "label": f"要点{child}", "children": []}
            for child in range(49)
        ]}
        for index in range(19)
    ]
    branches.append({"id": "b19", "label": "分支19", "children": [
        {"id": f"b19-{child}", "label": f"要点{child}", "children": []}
        for child in range(48)
    ]})
    content = MindMapContent.model_validate(
        {"title": "满树", "root": {"id": "root", "label": "根", "children": branches}})
    assert 1 + sum(1 + len(branch["children"]) for branch in branches) == MAX_TREE_NODES

    ctx = _ctx(content, selected=["b19"])
    mocked = AsyncMock(return_value=_children("甲", "乙"))
    result = _expand(ctx, mocked, node_id="b19", count=2)

    assert result.ok is False
    assert result.data["code"] == "tree_full"
    assert result.data["node_count"] == MAX_TREE_NODES
    assert result.data["node_limit"] == MAX_TREE_NODES
    mocked.assert_not_called()


def test_eighth_level_is_refused_before_the_model_call():
    node = {"id": "n8", "label": "第八层", "children": []}
    for level in range(7, 0, -1):
        node = {"id": f"n{level}", "label": f"第{level}层", "children": [node]}
    content = MindMapContent.model_validate({"title": "深树", "root": node})

    ctx = _ctx(content, selected=["n8"])
    mocked = AsyncMock(return_value=_children("甲", "乙"))
    result = _expand(ctx, mocked, node_id="n8", count=2)

    assert result.ok is False
    assert result.data["code"] == "too_deep"
    assert result.data["depth"] == MAX_TREE_DEPTH
    mocked.assert_not_called()


# ---------------------------------------------------------------- expandable 三态


def test_expandable_table_matches_the_contract_constant():
    """约定的三个值只该有一份真源：常数与字段的注解必须一致。"""
    values = set(Node.model_fields["expandable"].annotation.__args__)
    assert values == set(NODE_EXPANDABLE)


def test_first_generation_marks_its_leaves_expandable():
    """初次生成之后每个节点都还能再展开（`yes`）——包括最下面那层叶子。"""

    async def fake(system, user, *, max_tokens=None):
        if system.startswith(PLAN_PROMPT):
            return {"branches": ["甲", "乙", "丙"]}
        return _children("要点一", "要点二")

    ctx = _ctx(intent="create", selected=[])
    with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
        result = _run(build_registry("create").execute(
            _call("mindmap_generate_deep", topic="测试方案", branches=3,
                  points_per_branch=2, max_depth=3), ctx))

    assert result.ok, result.content
    states = {node.expandable for node in _walk(ctx.candidate.root)}
    assert states == {"yes"}


def test_the_eighth_level_is_not_expandable():
    """深度到契约的 8 层就是 `no`：再加子节点整树就不合法，这是结构事实。"""
    node = {"id": "n8", "label": "第八层", "children": []}
    for level in range(7, 0, -1):
        node = {"id": f"n{level}", "label": f"第{level}层", "children": [node]}
    content = MindMapContent.model_validate({"title": "深树", "root": node})

    # 即便 stamps 说它 `yes`，深度那条覆盖规则也要赢
    _stamp_expandable(content, {"n8": "yes", "n7": "yes"}, max_depth=MAX_TREE_DEPTH)

    assert content.root.children[0].children[0].children[0].children[0].children[0] \
        .children[0].children[0].expandable == "no"
    assert content.root.children[0].children[0].children[0].children[0].children[0] \
        .children[0].expandable == "yes"


def test_quota_truncated_branch_is_unknown_not_no():
    """被模型调用额度挡住的那一支是 `unknown`：没展开完，不是没得展开。"""

    async def fake(system, user, *, max_tokens=None):
        if system.startswith(PLAN_PROMPT):
            return {"branches": ["甲", "乙", "丙"]}
        return _children("要点一", "要点二")

    # 骨架 1 次 + 只能再展开 1 支 → 另外两支从没被问过
    ctx = _ctx(intent="create", selected=[], max_model_calls=2)
    with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
        result = _run(build_registry("create").execute(
            _call("mindmap_generate_deep", topic="测试方案", branches=3,
                  points_per_branch=2, max_depth=3), ctx))

    assert result.ok, result.content
    states = [branch.expandable for branch in ctx.candidate.root.children]
    assert states.count("unknown") >= 1
    assert "no" not in states
    codes = [item["code"] for item in result.data["diagnostics"]]
    assert "BRANCH_SKIPPED_BUDGET" in codes


def test_expanded_children_are_marked_by_the_model_declaration():
    """新节点：模型说"到底了"就是 `no`，没说就是 `yes`（叶子当然还能再拆）。"""
    ctx = _ctx(_content())

    async def fake(system, user, *, max_tokens=None):
        return {"children": [
            {"label": "冒烟", "detail": "最小可执行单元。", "expandable": False},
            {"label": "回归", "detail": "还有细项可写。", "expandable": True},
            {"label": "压力", "detail": "没声明。"},
        ]}

    assert _expand(ctx, AsyncMock(side_effect=fake), count=3).ok
    node = _risk(ctx.candidate)
    assert [child.expandable for child in node.children] == ["no", "yes", "yes"]
    # 被展开的节点自己：它现在有子节点了，但仍然还能继续展开
    assert node.expandable == "yes"


# ---------------------------------------------------------------- 材料找回


def _ingest(text: str = SOURCE_TEXT) -> str:
    """把一份材料写进知识库，返回**持久 id**（不是 `Source.id`）。"""
    return library.ingest(_source(text))


def _tree_with_ref(source_id: str) -> MindMapContent:
    """一棵"旧树"：节点的 `source_refs` 里存着持久 id，`source_usage` 也列着它。"""
    return MindMapContent.model_validate({
        "title": "旧图",
        "root": {"id": "root", "label": "旧图", "children": [
            {"id": "t1", "label": "验收", "children": [
                {"id": "risk", "label": "风险", "source_refs": [
                    {"source_id": source_id, "quote": "先做冒烟测试"},
                ], "children": []},
            ]},
        ]},
        "source_usage": [
            {"source_id": source_id, "kind": "text", "filename": "测试方案.md",
             "char_count": len(SOURCE_TEXT)},
        ],
    })


def test_sources_are_recovered_from_the_library_when_the_request_has_none():
    """`sources=[]` + 树上有 `source_refs` → 从库里把正文找回来，检索照常发生。

    这条是"重新打开旧导图，再点展开"那个场景的最小复现：前端手里没有正文（它只活
    在当次会话的内存里），能用的只有节点上那个持久 id。
    """
    source_id = _ingest()
    ctx = _ctx(_tree_with_ref(source_id), sources=[])
    assert ctx.request.sources == []

    async def fake(system, user, *, max_tokens=None):
        evidence = _evidence_in(user)
        assert evidence, "库里找回来的材料没有进检索"
        return {"children": [
            {"label": "冒烟范围", "detail": "先跑冒烟。",
             "source_refs": [{"source_id": evidence[0]["source_id"],
                              "quote": evidence[0]["quote"][:40]}]},
            {"label": "回归范围", "detail": "回归覆盖核心链路。"},
        ]}

    result = _expand(ctx, AsyncMock(side_effect=fake), count=2)

    assert result.ok, result.content
    assert result.data["materials"]["mode"] == "library"
    assert result.data["materials"]["recovered"] >= 1
    assert result.data["retrieval"]["hits"] >= 1
    node = _risk(ctx.candidate)
    assert node.children[0].source_refs, "找回材料之后引用应该写得出来"
    assert node.children[0].source_refs[0].source_id == source_id


def test_missing_library_material_degrades_to_an_unsourced_expand():
    """库也找不到（被 `prune` 删了）→ 照常展开，不写引用，并在 content 里说明。"""
    ctx = _ctx(_tree_with_ref("src-gone"), sources=[])

    async def fake(system, user, *, max_tokens=None):
        return {"children": [
            {"label": "冒烟范围", "detail": "凭知识写的。",
             "source_refs": [{"source_id": "src-gone", "quote": "不存在的原文"}]},
            {"label": "回归范围", "detail": "同样没有出处。"},
        ]}

    result = _expand(ctx, AsyncMock(side_effect=fake), count=2)

    assert result.ok, result.content
    assert result.data["materials"]["mode"] == "missing"
    assert result.data["refs"]["kept"] == 0
    assert all(child.source_refs == [] for child in _risk(ctx.candidate).children)
    # 降级必须说出来：这句话是用户判断"这棵树有没有依据"的唯一信号
    assert "这次没找到原始材料" in result.content


def test_request_sources_win_over_the_library():
    """请求带了材料就不查库：本次上传的正文才是这次展开的依据。"""
    source_id = _ingest()
    fresh = _source("预算审批的风险控制：超五十万报总经理。")
    ctx = _ctx(_tree_with_ref(source_id), sources=[fresh])

    async def fake(system, user, *, max_tokens=None):
        return _children("甲", "乙")

    result = _expand(ctx, AsyncMock(side_effect=fake), count=2)
    assert result.ok, result.content
    assert result.data["materials"]["mode"] == "request"
    assert result.data["materials"]["recovered"] == 0


# ---------------------------------------------------------------- 生长预览


def test_grow_frames_land_under_the_expanded_node():
    """`node_grow` 的 `path` 指向**被展开的那个节点**，不是根。

    写成 `[]` 会把新节点画到根下面：界面上看起来像"根上又长出几个分支"，而用户点
    的是第三层的某个叶子。
    """
    ctx = _ctx(_content())
    frames: list[dict] = []

    async def emit(payload):
        frames.append(payload)

    ctx.emit = emit
    assert _expand(ctx, AsyncMock(return_value=_children("冒烟", "回归")), count=2).ok

    grows = [frame for frame in frames if frame["type"] == "node_grow"]
    assert [frame["label"] for frame in grows] == ["冒烟", "回归"]
    # 目标节点是 root.children[0].children[0] → 索引链 [0, 0]
    assert {tuple(frame["path"]) for frame in grows} == {(0, 0)}


def test_no_emitter_means_no_frames_and_no_behaviour_change():
    """通道为 None 时行为与加预览之前逐字相同（单测里就是这个状态）。"""
    ctx = _ctx(_content())
    assert ctx.emit is None
    result = _expand(ctx, AsyncMock(return_value=_children("冒烟", "回归")), count=2)
    assert result.ok, result.content
    assert [child.label for child in _risk(ctx.candidate).children] == ["冒烟", "回归"]


# ---------------------------------------------------------------- 端到端


class _ScriptedProvider:
    """按脚本返回预设的 Completion；脚本用完还调就报错。"""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls = 0

    async def complete_tools(self, messages, tools, *, budget):
        self.calls += 1
        if not self._script:
            raise AssertionError("脚本已用完，循环比预期多调了一次模型")
        return self._script.pop(0)


def _completion(calls: list[ToolCall] | None = None) -> Completion:
    calls = calls or []
    message: dict[str, Any] = {"role": "assistant", "content": "好的"}
    if calls:
        message["tool_calls"] = [
            {"id": call.call_id, "type": "function",
             "function": {"name": call.name,
                          "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
            for call in calls
        ]
    return Completion(message=message, tool_calls=calls,
                      finish_reason="stop" if not calls else "tool_calls",
                      usage={"total_tokens": 1})


def test_end_to_end_expand_produces_a_candidate_with_the_new_children():
    """选中一个叶子 → 展开 → 候选 → （前端确认后）应用。这里覆盖后端那一段。

    断言三件事：候选里目标节点多了 2 个孩子、`ops` 帧带得出批次、`done` 帧交出的
    是同一棵树。前端"接受"那一步在 `frontend/tests/expand-node.spec.ts`。
    """
    content = _content()

    async def fake(system, user, *, max_tokens=None):
        return _children("冒烟范围", "回归范围")

    provider = _ScriptedProvider([
        _completion([ToolCall(call_id="c1", name="node_expand",
                              arguments={"node_id": "risk", "count": 2})]),
        _completion(),
    ])

    async def drain():
        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
            return [event async for event in run(
                AgentRunRequest(goal="展开风险这一支", intent="expand", snapshot=content,
                                selected_ids=["risk"],
                                budget=RunBudget(max_model_calls=10)),
                provider=provider,
            )]

    events = _run(drain())

    done = [event for event in events if isinstance(event, DoneEvent)]
    assert done, "没有收到 done 帧"
    final = done[-1].content
    assert final is not None
    assert [child.label for child in _risk(final).children] == ["冒烟范围", "回归范围"]
    # 原树的其余部分一个节点都没动
    assert [child.label for child in final.root.children] == ["验收"]

    ops = [event for event in events if isinstance(event, OpsEvent)]
    assert len(ops) == 1
    assert ops[0].batch.base_revision == 0
    assert all(op.op == "add" for op in ops[0].batch.ops)
    assert {op.parent_id for op in ops[0].batch.ops} == {"risk"}

    grows = [event for event in events if isinstance(event, NodeGrowEvent)]
    assert [grow.path for grow in grows] == [[0, 0], [0, 0]]
    assert provider.calls == 2


def _walk(root):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.children)
