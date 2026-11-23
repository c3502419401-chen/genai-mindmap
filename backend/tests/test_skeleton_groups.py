"""两级骨架：一级分支太多时先归成几组，**在骨架那一次调用里**。

材料有二十节时，扁平骨架就是二十条一级分支——图宽得没法看。这条改动把决定提前到
骨架那一次：模型在那一次就看到了全部二十节，它比任何事后聚类都更会判断哪些该归一起。
这个文件盯五件事：

  1. **触发与组数**：`> SKELETON_GROUP_THRESHOLD`（10）才两级；组数按每组 4 支倒推、
     夹在 3–6（20 节 → 5 组）。十支以内一个字都不变（提示词、`data` 键集都钉着）。
  2. **契约两种形状都收**：嵌套 `{"groups":[{"children":[…]}]}` 与老形状
     `{"branches":[…]}`。老形状在"该分组"的时候**退化成扁平**，不判失败。
  3. **组节点一次调用都不吃**：检索按二级分支（二十次本地检索）、结构那一次照旧、
     第三段的分组不进清单。调用次数与扁平完全一样。
  4. **形状有兜底**：模型仍写成二十条并列时，`_apply_groups` 按骨架摆正（只搬位置、
     不改内容），并记一条 `SKELETON_GROUPS_APPLIED`。
  5. **三条旧契约不回归**：`EVIDENCE_UNUSED`、`partial`、`node_grow`。两级这条路上
     它们与扁平时逐字相同。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.agent import tools as tools_module
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    DETAIL_ONLY_PROMPT,
    GROUPED_TREE_RULES,
    INITIAL_MAX_NODES,
    PLAN_PROMPT,
    SKELETON_GROUP_HARD_MAX,
    SKELETON_GROUP_MAX,
    SKELETON_GROUP_MIN,
    SKELETON_GROUP_TARGET,
    SKELETON_GROUP_THRESHOLD,
    SKELETON_TREE_RULES,
    _SkeletonBranch,
    _cap_plan,
    _group_plan_rules,
    _skeleton_branches,
    _skeleton_group_count,
    _skeleton_groups,
    _skeleton_plan,
    build_registry,
)
from app.contracts.source import Source, SourceKind

TOPIC = "机器学习讲义"

# 二十节，正好落进五个语义分组（组数 = ceil(20 / 4) = 5）。这不是凑出来的：
# 一份真的讲义本来就长这样——先是定义与来龙，再是方法与模型，再是数据、工程、前沿。
GROUPS: dict[str, tuple[str, ...]] = {
    "基础与定义": ("概念界定", "发展脉络", "与相邻领域的关系", "术语约定"),
    "方法与模型": ("监督学习", "无监督学习", "强化学习", "半监督学习"),
    "数据与特征": ("数据采集", "清洗与标注", "特征工程", "数据增强"),
    "工程实践": ("训练流程", "调参与评估", "部署与监控", "常见故障"),
    "前沿与局限": ("代表工作", "开放问题", "可解释性", "成本与伦理"),
}
SECTIONS = tuple(label for labels in GROUPS.values() for label in labels)


def _material() -> Source:
    """一份真材料：每节一个 Markdown 标题 + 一段正文，目录覆盖率接近 100%。"""
    body = []
    for label in SECTIONS:
        body.append(
            f"## {label}\n\n"
            f"{label}这一节讲的是它的定义、适用条件与常见做法。"
            f"做{label}时要先确定目标与评价口径，再看数据是否够用；"
            f"遇到偏差先查标注与切分，遇到方差不稳就先加正则或扩样本。"
            f"{label}的坑主要集中在口径不一致与过早优化这两处。\n"
        )
    return Source(id="s-lecture", kind=SourceKind.document, status="parsed",
                  filename="机器学习讲义.md",
                  text="# 机器学习讲义\n\n" + "\n".join(body))


def _request(sources: list[Source] | None = None) -> AgentRunRequest:
    return AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default",
        sources=sources or [],
        budget=RunBudget(max_model_calls=20, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=1),
    )


def _ctx(sources: list[Source] | None = None) -> RunContext:
    payload = sources or []
    return RunContext(
        request=_request(payload),
        sources_by_id={source.id: source for source in payload},
    )


def _run(ctx: RunContext, chat, *, arguments: dict | None = None):
    args = {"topic": TOPIC, "branches": 20}
    args |= arguments or {}
    registry = build_registry("create", has_sources=bool(ctx.request.sources),
                              has_library=False)
    with patch("app.agent.tools.chat_async", new=chat):
        return asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate", arguments=args), ctx))


def _codes(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


def _walk(node: dict):
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


def _grouped_response(groups=GROUPS) -> dict:
    """骨架的**两级**响应（模型照 `_group_plan_rules` 写的那种）。"""
    palette = ("blue", "green", "amber", "red", "purple", "teal", "pink", "gray")
    return {"groups": [
        {"label": name, "color": palette[index % len(palette)], "icon": "🧩",
         "children": [{"label": label} for label in labels]}
        for index, (name, labels) in enumerate(groups.items())
    ]}


def _grouped_tree(groups=GROUPS, *, points: int = 2) -> dict:
    """结构那一轮的替身：照骨架写成 root → 分组 → 分支 → 要点。"""
    children = []
    for group_index, (name, labels) in enumerate(groups.items()):
        branches = []
        for branch_index, label in enumerate(labels):
            branches.append({
                "id": f"b{group_index}{branch_index}", "label": label,
                "children": [
                    {"id": f"p{group_index}{branch_index}{point}",
                     "label": f"{label}·要点{point}"}
                    for point in range(points)
                ],
            })
        children.append({"id": f"g{group_index}", "label": name, "children": branches})
    return {"title": TOPIC, "root": {"id": "root", "label": TOPIC, "children": children}}


def _flat_tree(labels=SECTIONS) -> dict:
    """结构那一轮的另一种替身：**无视两级规则**，把二十节全挂在根上。"""
    return {"title": TOPIC, "root": {"id": "root", "label": TOPIC, "children": [
        {"id": f"f{index}", "label": label, "children": [
            {"id": f"fp{index}{point}", "label": f"{label}·要点{point}"}
            for point in range(2)
        ]}
        for index, label in enumerate(labels)
    ]}}


DETAIL_TEXT = (
    "这一条说明写到八十个字以上，讲清它是什么、怎么做、有什么条件与数字，"
    "这样第三段的产出才符合长度目标。"
)


def _chat(*, skeleton: dict, tree: dict, seen: list[tuple[str, str]] | None = None,
          details: dict | None = None, on_delta_chunks: int = 0):
    """模型替身：骨架回 `skeleton`，结构回 `tree`，第三段回 `details`。"""
    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if seen is not None:
            seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return skeleton
        if system.startswith(DETAIL_ONLY_PROMPT):
            if details is not None:
                return details
            return {node["id"]: DETAIL_TEXT
                    for node in _walk(tree["root"]) if node.get("id")}
        if on_delta is not None and on_delta_chunks:
            text = __import__("json").dumps(tree, ensure_ascii=False)
            size = max(1, len(text) // on_delta_chunks)
            for start in range(0, len(text), size):
                await on_delta(text[start:start + size])
        return tree
    return chat


# ---------------------------------------------------------------- 1. 触发与组数


def test_threshold_and_group_count_are_the_documented_numbers():
    """N=10、每组 4 支、3–6 组：20 节 → 5 组。

    这三个数不是拍的：调色板 8 色（一色一组）、画布横向能扫完的一行在 10 条上下、
    20 节按每组 4 支正好五组。数字改了，这条用例会先红——那是刻意的，它是
    "为什么是 10 而不是 8/20"的唯一一份可执行记录。
    """
    assert SKELETON_GROUP_THRESHOLD == 10
    assert SKELETON_GROUP_TARGET == 4
    assert (SKELETON_GROUP_MIN, SKELETON_GROUP_MAX) == (3, 6)
    assert SKELETON_GROUP_HARD_MAX >= SKELETON_GROUP_MAX
    assert _skeleton_group_count(11) == 3      # ceil(11/4)=3
    assert _skeleton_group_count(12) == 3
    assert _skeleton_group_count(13) == 4
    assert _skeleton_group_count(16) == 4
    assert _skeleton_group_count(17) == 5
    assert _skeleton_group_count(20) == 5


def test_no_grouping_at_or_below_the_threshold():
    """十支以内保持扁平：`groups` 给了也会被压平（多一层只是多一次点击）。"""
    flat = [{"label": label} for label in SECTIONS[:SKELETON_GROUP_THRESHOLD]]
    plan = _skeleton_plan({"groups": [
        {"label": "甲", "children": flat[:5]},
        {"label": "乙", "children": flat[5:]},
    ]})
    assert not plan.grouped
    assert [branch.label for branch in plan.flat] == list(
        SECTIONS[:SKELETON_GROUP_THRESHOLD])


def test_only_one_group_is_not_two_tier():
    """一个组装着二十支时不分级：根 → 组 → 二十支 与 根 → 二十支 的读法一样。"""
    plan = _skeleton_plan({"groups": [
        {"label": "全部", "children": [{"label": label} for label in SECTIONS]}]})
    assert not plan.grouped
    assert len(plan.flat) == len(SECTIONS)


def test_old_flat_shape_still_parses():
    """老形状 `{"branches":[...]}`（含纯字符串）照旧只当一层。"""
    plan = _skeleton_plan({"branches": [{"label": "甲"}, "乙"]})
    assert not plan.grouped
    assert [branch.label for branch in plan.flat] == ["甲", "乙"]
    assert [branch.label for branch in _skeleton_branches({"branches": ["甲", "乙"]})] == \
           ["甲", "乙"]


def test_group_parser_tolerates_the_usual_sloppiness():
    """组解析只丢结构，不为装饰字段丢内容：`color`/`icon` 归一化、空组丢掉。"""
    groups = _skeleton_groups({"groups": [
        {"label": "甲", "color": "深蓝", "icon": "🔧⚙️",
         "children": ["一", {"label": "二"}, {"name": "三"}, ""]},
        {"label": "空组", "children": []},
        {"label": "乙", "children": [{"label": "四"}]},
        "丙",
    ]})
    assert [group.label for group in groups] == ["甲", "乙"]
    # 甲的四条让它超过阈值 → 两级成立；"二"收下，"三"（没 label）与空串丢掉。
    plan = _skeleton_plan({"groups": [
        {"label": "甲", "children": [{"label": f"甲{i}"} for i in range(6)]},
        {"label": "乙", "children": [{"label": f"乙{i}"} for i in range(6)]},
    ]})
    assert plan.grouped
    assert len(plan.flat) == 12


def test_cap_plan_keeps_the_first_twenty_branches():
    """八组各三支（24）→ 只留前 20 支，多出来的整组丢掉（与分支尾巴截断同义）。"""
    groups = {"组%d" % index: tuple(f"支{index}-{j}" for j in range(3))
              for index in range(SKELETON_GROUP_HARD_MAX)}
    plan = _cap_plan(_skeleton_plan(_grouped_response(groups)))
    assert len(plan.flat) == 20
    assert plan.grouped
    assert sum(len(group.branches) for group in plan.groups) == 20


def test_cap_plan_drops_a_branch_listed_under_two_groups():
    """同一支被列进两个组 → 只留第一次出现的那一处（骨架自己不能自相矛盾）。

    判据与 `_cross_branch_duplicates` 同源（只折空白）：骨架若允许一条分支挂两组，
    树里要么画两遍（重复），要么按代码挑的那一组归位（模型另一半意思被静默丢掉）。
    留一处、归第一个提到它的组，是两者里唯一说得清的行为。
    """
    plan = _cap_plan(_skeleton_plan({"groups": [
        {"label": "甲", "children": [{"label": label} for label in SECTIONS[:6]]},
        {"label": "乙", "children": [{"label": label} for label in SECTIONS[4:10]]},
    ]}))
    assert plan.grouped
    labels = [branch.label for branch in plan.flat]
    assert labels == list(SECTIONS[:6]) + list(SECTIONS[6:10])
    assert len(labels) == len(set(labels))


# ---------------------------------------------------------------- 2. 骨架提示词


def test_group_rules_are_appended_to_plan_prompt_not_replacing_it():
    """两级要求拼在 `PLAN_PROMPT` **之后**：前缀不变，`startswith` 仍认得出骨架调用。

    这不是格式洁癖：`PLAN_PROMPT` 与 `_plan_ask` 有逐字节回归用例钉着
    （`test_edges.TestTreeIsByteIdentical`），把两级塞进去会把"扁平这条路一个字
    都没变"这条保证一起毁掉。
    """
    rules = _group_plan_rules(len(SECTIONS))
    assert rules.startswith("\n\n**这一次请输出两级骨架**")
    assert '"groups"' in rules
    # 组数写成具体的数，不是"分几组都行"
    assert str(_skeleton_group_count(len(SECTIONS))) in rules
    assert str(SKELETON_GROUP_MIN) in rules and str(SKELETON_GROUP_MAX) in rules
    # 二级不写视觉元素（它们的颜色随分组继承）
    assert "只给**分组**" in rules


def test_grouped_tree_rules_share_the_no_duplicate_and_no_detail_parts():
    """两级与扁平的树规则只差"根下挂什么"，另外两段逐字相同。"""
    assert GROUPED_TREE_RULES != SKELETON_TREE_RULES
    for shared in ("不要在多条分支下挂同一个泛化标题", "**这一轮不要写 detail**"):
        assert shared in GROUPED_TREE_RULES
        assert shared in SKELETON_TREE_RULES
    assert "分组下面直接挂分支" in GROUPED_TREE_RULES
    assert "分组下面直接挂分支" not in SKELETON_TREE_RULES


# ---------------------------------------------------------------- 3. 端到端


def test_twenty_sections_become_five_groups():
    """20 节的材料 → 根下 5 个分组，每个分组下面是它的 4 支，分支下面是要点。"""
    seen: list[tuple[str, str]] = []
    result = _run(
        _ctx([_material()]),
        _chat(skeleton=_grouped_response(), tree=_grouped_tree(), seen=seen))

    assert result.ok, result.content
    tree = result.data["branches"]
    assert [item["label"] for item in tree] == list(GROUPS)
    assert [item["points"] for item in tree] == [4, 4, 4, 4, 4]
    assert result.data["planned_branches"] == len(SECTIONS)
    assert result.data["skeleton"]["two_tier"] is True
    assert result.data["skeleton"]["shuffled_nodes"] == 0
    # 一级节点 = 1 + 5 + 20 + 40
    assert result.data["node_count"] == 1 + 5 + 20 + 40
    assert "SKELETON_GROUPS_APPLIED" not in _codes(result)


def test_group_prompt_asks_for_two_tiers_once():
    """骨架仍然只有**一次**调用，那一次就带上了分组要求与材料目录。"""
    seen: list[tuple[str, str]] = []
    result = _run(
        _ctx([_material()]),
        _chat(skeleton=_grouped_response(), tree=_grouped_tree(), seen=seen))

    assert result.ok, result.content
    skeleton_calls = [item for item in seen if item[0].startswith(PLAN_PROMPT)]
    assert len(skeleton_calls) == 1
    system, user = skeleton_calls[0]
    assert "请输出两级骨架" in system
    # 目录在这条路上仍然拼进 user（它同时是"有多少个方面"的来源）
    assert "材料自带的章节" in user
    # 结构那一次用的是两级的树规则
    structure_calls = [
        item for item in seen
        if not item[0].startswith(PLAN_PROMPT)
        and not item[0].startswith(DETAIL_ONLY_PROMPT)]
    assert len(structure_calls) == 1
    assert GROUPED_TREE_RULES in structure_calls[0][0]
    # user 里是缩进清单：分组一行、它的分支缩进
    assert "1. 基础与定义" in structure_calls[0][1]
    assert "   1.1 概念界定" in structure_calls[0][1]


def test_group_nodes_never_buy_a_call():
    """组节点不消耗任何一次调用：检索按二级分支、结构一次、说明按节点数切。

    这条是这次改动的成本口径：**总调用次数不分两级前后相同**。扁平时是
    1（骨架）+ 1（结构）+ 2（说明两半）= 4；两级之后仍然是 4——
    检索是本地/向量运算，分组不进 `titles`、不进 detail 清单。
    """
    seen: list[tuple[str, str]] = []
    result = _run(
        _ctx([_material()]),
        _chat(skeleton=_grouped_response(), tree=_grouped_tree(), seen=seen))

    assert result.ok, result.content
    assert len(seen) == 4                     # 骨架 1 + 结构 1 + 说明 2
    # 检索仍然按二十节各一次，不是 25 次（分组不进检索）
    queries = result.data["retrieval"]["queries"]
    assert len(queries) >= len(SECTIONS)
    for group in GROUPS:
        assert group not in queries
    # 说明那两半的清单里没有任何分组 id
    detail_users = [user for system, user in seen
                    if system.startswith(DETAIL_ONLY_PROMPT)]
    assert len(detail_users) == 2
    for user in detail_users:
        for index in range(len(GROUPS)):
            assert f"id=g{index}" not in user
    # 1 + 5 + 20 + 40 个节点里，需要写说明的是除 5 个分组之外的那些
    assert result.data["detail"]["requested"] == 1 + 20 + 40
    assert result.data["detail"]["short_nodes"] == 0


def test_group_nodes_carry_one_color_each_and_branches_inherit():
    """一级色只落在分组上：五组五个色，二级分支一个色都不写（前端沿父链继承）。"""
    ctx = _ctx([_material()])
    result = _run(ctx, _chat(skeleton=_grouped_response(), tree=_grouped_tree()))

    assert result.ok, result.content
    root = ctx.candidate.root
    colors = [child.color for child in root.children]
    assert colors == ["blue", "green", "amber", "red", "purple"]
    assert all(child.icon == "🧩" for child in root.children)
    for group in root.children:
        assert group.children and all(branch.color is None for branch in group.children)


def test_model_writing_a_flat_tree_gets_regrouped():
    """模型无视两级的规则写了二十条并列 → 按骨架摆正，内容一个都没丢。

    这就是"形状兜底"：提示词负责让它写对，`_apply_groups` 负责让"没写对"不致命。
    搬的只是位置——二十节的 label、要点、引用都原样留着。
    """
    result = _run(
        _ctx([_material()]),
        _chat(skeleton=_grouped_response(), tree=_flat_tree()))

    assert result.ok, result.content
    assert [item["label"] for item in result.data["branches"]] == list(GROUPS)
    assert result.data["skeleton"]["shuffled_nodes"] > 0
    assert "SKELETON_GROUPS_APPLIED" in _codes(result)
    labels = {branch for branch in SECTIONS}
    # 二十节都还在树上，归位到了各自的分组
    for index, (name, members) in enumerate(GROUPS.items()):
        assert result.data["skeleton"]["groups"][index]["label"] == name
        assert tuple(result.data["skeleton"]["groups"][index]["branches"]) == members
    assert labels == {label for labels in GROUPS.values() for label in labels}


def test_grouped_shape_with_too_few_branches_degrades_to_flat():
    """给了分组但只有六支 → 压平成扁平，不判失败（老形状兼容的同一条理由）。"""
    six = {"甲": ("一", "二", "三"), "乙": ("四", "五", "六")}
    labels = tuple(label for group in six.values() for label in group)
    seen: list[tuple[str, str]] = []
    result = _run(
        _ctx(),
        _chat(skeleton=_grouped_response(six), tree=_flat_tree(labels), seen=seen),
        arguments={"topic": TOPIC, "branches": 6})

    assert result.ok, result.content
    assert "skeleton" not in result.data
    assert [item["label"] for item in result.data["branches"]] == list(labels)
    # system 用的是扁平那份树规则（形状按**实际形态**选，不是按"问没问过"）
    assert GROUPED_TREE_RULES not in seen[1][0]


def test_flat_skeleton_when_two_tiers_was_requested_degrades_to_flat():
    """该分组时模型仍回扁平（老 provider / 老 mock）→ 扁平落树，不失败。

    二十条并列确实是这次要改善的形态，但**判失败更糟**：骨架已经花掉了，而一棵
    二十支的树仍然可用。退化路径的存在让"预判"不需要预测得准。
    """
    result = _run(
        _ctx([_material()]),
        _chat(skeleton={"branches": [{"label": label} for label in SECTIONS]},
              tree=_flat_tree()))

    assert result.ok, result.content
    assert "skeleton" not in result.data
    assert [item["label"] for item in result.data["branches"]] == list(SECTIONS)
    assert result.data["planned_branches"] == len(SECTIONS)


def test_ten_branches_change_nothing():
    """十支以内：提示词没有那一段，`data` 也没有 `skeleton` 键（键集与改动前相同）。"""
    labels = SECTIONS[:10]
    seen: list[tuple[str, str]] = []
    result = _run(
        _ctx(),
        _chat(skeleton={"branches": [{"label": label} for label in labels]},
              tree=_flat_tree(labels), seen=seen),
        arguments={"topic": TOPIC, "branches": 10})

    assert result.ok, result.content
    assert "skeleton" not in result.data
    skeleton_system = seen[0][0]
    assert "请输出两级骨架" not in skeleton_system
    # 无材料时骨架 system 仍是 PLAN_PROMPT + 那两句契约，一字不多
    assert skeleton_system.startswith(PLAN_PROMPT)
    assert "groups" not in skeleton_system


# ---------------------------------------------------------------- 4. 旧契约不回归


def test_evidence_unused_still_reported_on_the_two_tier_path():
    """两级这条路上 `EVIDENCE_UNUSED` 照旧：证据给了、树上一条引用都没有 → warn。"""
    result = _run(
        _ctx([_material()]),
        _chat(skeleton=_grouped_response(), tree=_grouped_tree()))

    assert result.ok, result.content
    assert "EVIDENCE_UNUSED" in _codes(result)
    assert result.data["source_refs"]["declared"] == 0


def test_partial_and_folding_still_work_on_the_two_tier_path():
    """两级这条路上 `partial` 语义不变：超过可读性上限 → **折叠**并标 `partial`。

    这条以前钉"裁到 80"。裁剪现在只剩四档折叠都折不动之后的兜底（`tools._fold_tree`），
    而两级树的每一个父节点扇出都很宽（根 5 组、每组 4 支、每支 6 个要点），正是折叠
    最省节点的那种形状——146 个节点被折进可读性上限以内，一个都不用裁。

    钉它要守住的两件事：一级、二级这两层**照样成立**（`_apply_groups` 的结果没有被后来
    的折叠抹平），以及 `partial` 仍然如实上报。
    """
    big = _grouped_tree(points=6)      # 1 + 5 + 20 + 120 = 146 个节点
    result = _run(_ctx(), _chat(skeleton=_grouped_response(), tree=big))

    assert result.ok, result.content
    assert result.data["node_count"] <= INITIAL_MAX_NODES
    assert result.data["cut_nodes"] == 0            # 一个都没裁
    assert result.data["folded_rollups"] >= 1
    assert result.data["partial"] is True
    assert "TREE_FOLDED_TO_READABILITY" in _codes(result)
    assert "TREE_CLIPPED_TO_INITIAL" not in _codes(result)


def test_node_grow_frames_cover_the_three_levels():
    """两级这条路上 `node_grow` 同样是"每写出一个完整节点推一帧"，层级多一层而已。"""
    frames: list[dict] = []

    async def emit(frame) -> None:
        frames.append(frame)

    ctx = _ctx()
    ctx.emit = emit
    result = _run(ctx, _chat(skeleton=_grouped_response(), tree=_grouped_tree(),
                             on_delta_chunks=16))

    assert result.ok, result.content
    grows = [frame for frame in frames if frame.get("type") == "node_grow"]
    assert grows
    assert grows[0]["path"] is None and grows[0]["label"] == TOPIC
    # 分组挂在根上（`path=()`），分支挂在分组下（`path` 长度 1），要点长度 2
    assert [frame["path"] for frame in grows].count([]) == len(GROUPS)
    assert sum(1 for frame in grows if len(frame["path"] or []) == 1) >= len(SECTIONS)


# ---------------------------------------------------------------- 5. 纯函数


@pytest.mark.parametrize("labels", [("甲",), ("甲", "乙")])
def test_palette_fill_is_shared_by_groups_and_branches(labels):
    """分组与分支共用一套补色算法：缺色的按调色板顺序补、跳过已占用的。"""
    branches = [_SkeletonBranch(label) for label in labels]
    groups = tools_module._skeleton_groups({"groups": [
        {"label": label, "children": [f"{label}支"]} for label in labels]})
    assert [item.color for item in tools_module._fill_branch_colors(branches)] == \
           [item.color for item in tools_module._fill_group_colors(groups)]
