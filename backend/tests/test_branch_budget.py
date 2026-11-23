"""预算不该压掉一级分支数。

这个 bug 的实测形态（`docs/GRAPH-SCALE.md` 第 7 节）：

    材料 424 字 · 骨架 3 支 · `initial_nodes = 10`
    → 模型写了 39 个节点、3 支齐全（`before_clip = 39`，`cut_nodes = 29`）
    → `_clip_tree(content, 10)` 按**前序**保留前 10 个，第 1 支的子树吃掉 9 个名额
    → 落树 **1 支** / 10 节点

用户显式选了 6 条一级分支，拿到 1 条。所以这里钉三件事：

  1. **算术**：`branch_budget` / `nodes_per_branch` / `branch_share` 三个数在两端
     （下限 3、上限 20）与常规区间上的值；
  2. **提示词**：`detail_prompt` 在按支摊时说的是"每条一级分支 M 个"，而**不传**
     `per_branch` 时仍逐字节是旧那句（总量池那条路要能原样跑）；
  3. **落树后的分支数**（最后一条，回归用例）：同一个模型响应，旧口径落 1 支、
     新口径落 6 支。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    DETAIL_ONLY_PROMPT,
    INITIAL_MAX_NODES,
    PLAN_PROMPT,
    _clip_tree,
    build_registry,
)
from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source, SourceKind
from app.generation import budget
from app.generation.service import DETAIL_PROMPT, detail_prompt

TOPIC = "碳中和的主要方面"

# 与实测那两篇 openalex 摘要同量级（424 字）。长度只要落在 `MIN_INITIAL_NODES`
# 那一档（< 1,600 字、1 份、0 章）就算出来 10——判据见 `budget.scaled_initial_nodes`。
ABSTRACT = (
    "碳中和与碳达峰的约束正在改变工业园区的规划逻辑。绿色低碳工业园区要把能源结构、"
    "产业耦合与碳汇三条线一起算：能源侧以绿电与余热回收替代燃煤，产业侧让上游的"
    "副产物成为下游的原料，碳汇侧用林地与土壤固碳补掉工艺上削不掉的排放。"
    "高速铁路的节能则是另一条路——它以电代油，把单位人公里的碳排降到航空的三分之一，"
    "站城的紧凑开发又把接驳产生的排放压下来。两条路都要算清口径：直接排放、"
    "间接排放与外购电力的折算因子不同，边界一变结论就会翻转。"
) * 3


def _ctx(sources: list[Source]) -> RunContext:
    request = AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default", sources=sources,
        budget=RunBudget(max_model_calls=20, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=1),
    )
    return RunContext(request=request,
                      sources_by_id={source.id: source for source in sources})


def _material() -> Source:
    return Source(id="s-carbon", kind=SourceKind.paper, status="parsed",
                  filename="openalex: 碳中和摘要", text=ABSTRACT)


def _content(branches: int, per_branch: int) -> MindMapContent:
    """一棵 `branches` 支、每支 `per_branch` 个节点（含分支自己）的树。"""
    return MindMapContent.model_validate({
        "title": TOPIC,
        "root": {
            "id": "root", "label": TOPIC, "children": [
                {"id": f"b{index}", "label": f"支{index}", "children": [
                    {"id": f"b{index}-{point}", "label": f"要点{point}",
                     "detail": "一句具体的说明。"}
                    for point in range(per_branch - 1)
                ]}
                for index in range(branches)
            ],
        },
    })


def _branch_labels(content: MindMapContent) -> list[str]:
    return [child.label for child in content.root.children]


def _count(root) -> int:
    return 1 + sum(_count(child) for child in root.children)


# ---------------------------------------------------------------- 1. 算术


def test_nodes_per_branch_clamps_at_both_ends():
    """每支预算夹在 `[3, 20]`：下限是"一支至少三个节点"，上限是"一支在吃别人的预算"。"""
    # 424 字算出来 10，6 支 → 10/6 = 1.67，抬到下限 3
    assert budget.nodes_per_branch(initial_nodes=10, branches=6) == 3
    # 5,616 字 16 份算出来 32，6 支 → 5.33 → 5（不进两位小数，不进"四舍五入后 6"）
    assert budget.nodes_per_branch(initial_nodes=32, branches=6) == 5
    # 66,536 字算出来 80，6 支 → 13.33 → 13
    assert budget.nodes_per_branch(initial_nodes=80, branches=6) == 13
    # 支数少到每支能摊 > 20：夹回 20，否则一支就把总量池独吞掉
    assert budget.nodes_per_branch(initial_nodes=80, branches=2) == 20
    # 16 份摘要各要两支（32）、拢成 16 支 → 2，还是抬到 3
    assert budget.nodes_per_branch(initial_nodes=32, branches=16) == 3


def test_branch_budget_folds_the_pool_but_never_shrinks_it():
    """总量 = `max(N, 1 + 支数 × M)`：`1` 是中心主题，少了它裁剪与提示词会差一个。"""
    # 薄：10 抬到 1 + 6×3 = 19（按支摊之后总量确实变大了——那是"6 条分支各写一点"
    # 的必要开销，而不是把输出预算偷偷放大：19 仍远在 80 之下）
    assert budget.branch_budget(initial_nodes=10, branches=6) == (3, 19)
    # 3 支：1 + 3×3 = 10，与 N 恰好相等
    assert budget.branch_budget(initial_nodes=10, branches=3) == (3, 10)
    # 中批：32 支数 6 → 5，1 + 30 = 31 ≤ 32，总量仍以 N 为底
    assert budget.branch_budget(initial_nodes=32, branches=6) == (5, 32)
    # 大批：80 支数 6 → 13，1 + 78 = 79 ≤ 80
    assert budget.branch_budget(initial_nodes=80, branches=6) == (13, 80)
    # 支数极多时总量会超 N，但仍夹在可读性上限之内
    assert budget.branch_budget(initial_nodes=10, branches=20) == (3, 61)


def test_branch_share_divides_the_ceiling_by_branches():
    """裁剪名额：`(total - 1) / 支数`，整除的余数留在根上（不裁根）。"""
    assert budget.branch_share(total=19, branches=6) == 3
    assert budget.branch_share(total=10, branches=3) == 3
    assert budget.branch_share(total=32, branches=6) == 5
    assert budget.branch_share(total=80, branches=6) == 13
    # 支数多于名额时不留 0：每支至少 1 个（否则那一支在画布上不存在）
    assert budget.branch_share(total=5, branches=20) == 1


# ---------------------------------------------------------------- 2. 提示词


def test_scale_line_speaks_per_branch_and_forbids_dropping_one():
    """按支摊那一句：说"每条一级分支 M 个"、"这次 K 条一条都不能少"。

    关键是它**不再说总量**——"第一次生成本身请控制在 N 个节点以内"正是模型据此
    砍分支（或被 `_clip_tree` 砍成分支）的那句话。
    """
    prompt = detail_prompt(19, per_branch=3, branches=6)
    assert "请按每条一级分支 3 个节点来铺" in prompt
    assert "这次 6 条一级分支" in prompt
    assert "一条都不能少" in prompt
    # 作用域收窄：原句"宁可少两层"变成了"每一支内部宁可少两层"
    assert "该缩的是每一支的深度，不是支数" in prompt
    # 总量的说法不再出现
    assert "控制在 19 个节点" not in prompt


def test_scale_line_without_per_branch_is_the_old_pool_sentence():
    """不传 `per_branch` → 仍是总量池那一句（旧口径的测量臂与回归路径都靠它）。"""
    prompt = detail_prompt(10)
    assert "控制在 10 个节点" in prompt
    assert "宁可少两层，也要让每一层都写完整" in prompt
    assert "每条一级分支" not in prompt


def test_default_detail_prompt_is_byte_identical():
    """默认路径必须**返回常量本身**：开关关着时提示词一个字节都不能变。"""
    assert detail_prompt() is DETAIL_PROMPT
    assert detail_prompt(INITIAL_MAX_NODES) is DETAIL_PROMPT
    # 即使给了按支摊的参数，80 那一档也不换句（总量池在 80 上没有可诊断的伤害）
    assert detail_prompt(INITIAL_MAX_NODES, per_branch=13, branches=6) is DETAIL_PROMPT


# ---------------------------------------------------------------- 3. 裁剪


def test_clip_tree_without_per_branch_keeps_the_old_preorder_behaviour():
    """默认（`per_branch=None`）逐字节是旧行为：前序保留前 `limit` 个。

    这条不是"以防万一"：`INITIAL_MAX_NODES=80` 那条路上几乎不触发裁剪，但
    `test_single_call_generate` / `test_skeleton_groups` 里有用例把它压到 5 验裁剪，
    所以它是活的契约，不能跟着改。
    """
    content = _content(branches=3, per_branch=10)
    trimmed, dropped = _clip_tree(content, 10)
    assert _count(trimmed.root) == 10
    assert dropped == 31 - 10
    # 前序：第 1 支的 9 个后代先把名额吃光，第 2、3 支一个都不剩
    assert _branch_labels(trimmed) == ["支0"]


def test_clip_tree_with_per_branch_keeps_every_branch():
    """按支配额：名额不够时缩的是**每一支的深度**，不是分支数。"""
    content = _content(branches=6, per_branch=10)
    trimmed, dropped = _clip_tree(content, 19, per_branch=3)
    assert _branch_labels(trimmed) == [f"支{index}" for index in range(6)]
    assert _count(trimmed.root) == 19
    assert dropped == 61 - 19
    # 每支都留下了"分支 + 两个要点"——不是只有第一支深、后面几支只剩标签
    assert [len(child.children) for child in trimmed.root.children] == [2] * 6


def test_clip_tree_per_branch_never_exceeds_the_ceiling():
    """名额分不匀时也不越界：`limit` 仍是硬上限。"""
    content = _content(branches=20, per_branch=4)
    trimmed, _ = _clip_tree(content, 10, per_branch=3)
    assert _count(trimmed.root) <= 10
    assert len(trimmed.root.children) <= 19


# ---------------------------------------------------------------- 4. 端到端（回归）


def _chat(tree: dict, seen: list[tuple[str, str]] | None = None, *, branches: int = 6):
    """模型替身：骨架回 `branches` 条，结构回 `tree`，第三段按 id 造一份说明。"""
    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if seen is not None:
            seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": [f"支{index}" for index in range(branches)]}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {node["id"]: "这句说明写到八十个字以上，讲清它是什么、怎么做、"
                                "有什么条件与数字，这样第三段的产出才符合长度目标。"
                    for node in _walk(tree["root"]) if node.get("id")}
        return tree
    return chat


def _walk(node: dict):
    yield node
    for child in node.get("children") or []:
        if isinstance(child, dict):
            yield from _walk(child)


def _run(chat, ctx: RunContext, branches: int = 6):
    registry = build_registry("create", has_sources=bool(ctx.request.sources),
                              has_library=False)
    with patch("app.agent.tools.chat_async", new=chat):
        return asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate",
                     arguments={"topic": TOPIC, "branches": branches}), ctx))


def _payload(branches: int, per_branch: int) -> dict:
    return {"title": TOPIC, "root": {
        "id": "root", "label": TOPIC, "children": [
            {"id": f"b{index}", "label": f"支{index}", "children": [
                {"id": f"b{index}-{point}", "label": f"要点{point}"}
                for point in range(per_branch - 1)
            ]}
            for index in range(branches)
        ]}}


def test_thin_material_keeps_every_branch(monkeypatch):
    """**这个 bug 的回归用例**：薄材料上 6 条一级分支必须全都落树。

    模型替身写的是 6 支 × 10 个节点（61 个）。旧口径下 `_clip_tree(content, 10)` 按
    前序只能留下一支——用户选 6 条拿到 1 条。

    现在这条路上**没有任何配额**可压：`node_limit` 是可读性上限 80（不是"这次要几个"），
    61 < 80 所以连折叠都不触发，六支原样落树。真超了 80 也是折叠（
    `test_over_the_readability_limit_is_folded_not_clipped` 钉那条），不是裁。
    """
    monkeypatch.setenv(budget.NODE_SCALE_ENV, "1")
    monkeypatch.delenv(budget.NODE_POOL_ENV, raising=False)
    seen: list[tuple[str, str]] = []
    ctx = _ctx([_material()])

    result = _run(_chat(_payload(6, 10), seen), ctx)

    assert result.ok, result.content
    assert len(_branch_labels(ctx.candidate)) == 6
    # 配额不再进提示词：`MINDMAP_NODE_SCALE` 开着也一样（那条路只影响
    # `budget.initial_nodes`，而生成路径已经不再读它——见 `tools._generate` 里
    # 「规模：不再由代码给一个数」那一段）
    assert result.data["node_limit"] == INITIAL_MAX_NODES
    assert "控制在" not in seen[1][0]
    assert "每条一级分支" not in seen[1][0]
    assert ctx.candidate.root.children[0].children  # 每支都还有内容，不是光杆标签
    codes = [item["code"] for item in result.data["diagnostics"]]
    assert "TREE_CLIPPED_TO_INITIAL" not in codes
    assert "TREE_FOLDED_TO_READABILITY" not in codes


def test_the_old_preorder_clip_still_collapses_to_one_branch():
    """为什么**不能**回到前序裁剪：同一个 61 节点的树、`limit=10`，只剩一支。

    这条以前是"对照臂"（`MINDMAP_NODE_POOL=1` 走旧口径）。旧口径已经从生成路径上
    撤掉了，所以对照直接打在 `_clip_tree` 上——它仍在，但只剩一个入口：四档折叠都
    折不动之后的兜底（`tools._fold_tree`）。把这条留在测试里，是为了让"为什么不该
    用它"这件事永远有一条可执行的证据，而不是一段注释。
    """
    content = _content(branches=6, per_branch=10)      # 1 + 6×10 = 61
    trimmed, dropped = _clip_tree(content, 10)

    assert len(trimmed.root.children) == 1             # 后 5 支整条消失
    assert dropped == 51


@pytest.mark.parametrize("scale", [False, True])
def test_default_path_reports_the_readability_ceiling(monkeypatch, scale):
    """`node_limit` 恒为 80（= 可读性上限），**与 `MINDMAP_NODE_SCALE` 无关**。

    这条以前钉"开关关着时恒为 80、开着时按材料算成 10，提示词跟着换那句"。那个数
    已经从生成路径上撤掉了（`tools._generate`：「规模：不再由代码给一个数」），所以
    现在无论是哪一档开关，提示词里都不出现节点数——两档必须给出**同一份** system。

    钉它的理由：这条是"配额真的没了"的守卫。哪天 `MINDMAP_NODE_SCALE` 又被接回
    生成路径，这里立刻红。
    """
    if scale:
        monkeypatch.setenv(budget.NODE_SCALE_ENV, "1")
    seen: list[tuple[str, str]] = []
    ctx = _ctx([_material()])

    result = _run(_chat(_payload(3, 2), seen, branches=3), ctx, branches=3)

    assert result.ok, result.content
    assert result.data["node_limit"] == INITIAL_MAX_NODES
    system = seen[1][0]
    assert "控制在" not in system
    assert "每条一级分支" not in system
    # 那一句换成了判据（`tools.SCALE_BY_MATERIAL_RULE`）
    assert "由内容决定" in system


def test_materials_are_the_thin_batch():
    """材料自证：这份抽象与实测那一批同量级（< 1,600 字 → 判据给 10）。"""
    assert 400 <= len(ABSTRACT) < 1600
    assert budget.scaled_initial_nodes(
        material_chars=len(ABSTRACT), sources=1, chapters=0) == 10
