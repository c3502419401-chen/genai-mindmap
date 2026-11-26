"""生成收尾有没有做**确定性修复**：`check()`（只报）与 `repair()`（顺手修）的接线。

这份文件的来历是 `docs/SUBAGENT-VS-SELFCHECK.md` 第五节那一行：

    第 0 件 | 生成收尾接上 `optimization.repair()` | **0 次调用**
            | 79 张图里 37 张的 `SINGLE_CHILD` 冗余层（命中 116 次）
            | **代码已有，生成链路没接**（收尾只跑 `check()`，只报不修）

`tests/test_optimization_repair.py` 已经把 `repair()` 自己的三条硬约束钉住了
（幂等 / 引用并集 / 可撤销）。这里钉的是**接线**，五件事：

  1. 收尾确实调了它，而且只修那三条（`AUTO_REPAIRABLE_CODES`）；
  2. 修到不动点：对交付的树再跑一次 `repair()` 应该是 0 步；
  3. 撤销栈里仍然**一条**——修复发生在 `publish_tree` 之前，所以它属于这一次
     生成，不是用户另外按的一次扳机（可测的那一半：`data` 里的节点数与交付的
     候选树一致，说明没有"先交付、后修复"）；
  4. 修了几处、哪几条规则、少修了几处，进 `data["auto_repair"]` 与诊断流；
  5. 关掉开关 / 非树图型时，产物与接线之前**逐字节同形**（键集也不多一个）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    AUTO_REPAIR_ENV,
    DETAIL_ONLY_PROMPT,
    PLAN_PROMPT,
    build_registry,
)
from app.contracts.mindmap import MAX_NODE_CHILDREN
from app.optimization.repair import repair

TOPIC = "报销制度"
BRANCHES = ["费用报销", "差旅报销", "审批流程"]


def _request() -> AgentRunRequest:
    return AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default",
        budget=RunBudget(max_model_calls=20, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=1),
    )


def _ctx() -> RunContext:
    return RunContext(request=_request(), sources_by_id={})


def _tree() -> dict:
    """一棵**故意带三种冗余**的树，每种的修法都是唯一的：

      - `b0` → `b0-0` → `b0-0-0`：两级单子链（`SINGLE_CHILD`，修法是压成一层）；
      - `b1` 下两个 `报销单` 逐字同名（`DUPLICATE_LABEL`，修法是并进第一个）；
      - `b2` 正常，用来当"其他分支更深"的参照；
      - `b2` 下还有一对**近义**（`数据处理` / `数据预处理`）：`NEAR_DUPLICATE_LABEL`
        只报不修——字面像不等于语义同，修法不唯一，收尾一个字节都不许碰它。
    """
    return {"title": TOPIC, "root": {
        "id": "root", "label": TOPIC, "detail": "中心主题",
        "children": [
            {"id": "b0", "label": "费用报销", "children": [
                {"id": "b0-0", "label": "日常费用", "children": [
                    {"id": "b0-0-0", "label": "办公用品", "detail": "一句具体的说明。"},
                ]},
            ]},
            {"id": "b1", "label": "差旅报销", "children": [
                {"id": "b1-0", "label": "报销单", "detail": "一句具体的说明。"},
                {"id": "b1-1", "label": "报销单", "detail": "另一句具体的说明。"},
            ]},
            {"id": "b2", "label": "审批流程", "children": [
                {"id": "b2-0", "label": "数据处理", "detail": "一句具体的说明。"},
                {"id": "b2-1", "label": "数据预处理", "detail": "一句具体的说明。"},
            ]},
        ]}}


def _walk(node: dict):
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


def _ids(tree: dict) -> set[str]:
    return {node["id"] for node in _walk(tree["root"]) if node.get("id")}


DETAIL_TEXT = ("这句说明写到八十个字以上，讲清它是什么、怎么做、有什么条件与数字，"
               "这样第三段的产出才符合长度目标。")


def _chat(tree: dict, *, plan_prompt: str | None = None):
    """骨架回三条分支，结构回整棵树，说明段按 id 对号写回一段合格长度的说明。

    `plan_prompt` 是**这一次图型**的骨架 system（鱼骨/流程各有一份，
    见 `tools._generate` 里 `base_plan` 那三行）：不传时只认树的 `PLAN_PROMPT`。
    """
    systems = [PLAN_PROMPT] + ([plan_prompt] if plan_prompt else [])

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if any(system.startswith(prompt) for prompt in systems):
            return {"branches": list(BRANCHES)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {node_id: DETAIL_TEXT for node_id in _ids(tree)}
        return tree
    return chat


def _run(ctx: RunContext, chat, *, diagram_type: str | None = None):
    arguments: dict = {"topic": TOPIC, "branches": 3}
    if diagram_type is not None:
        arguments["diagram_type"] = diagram_type
    registry = build_registry("create", has_sources=False, has_library=False)
    with patch("app.agent.tools.chat_async", new=chat):
        return asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate", arguments=arguments), ctx))


def _codes(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


# ---------------------------------------------------------------- 1. 修了

def test_the_tail_repairs_the_three_rules_whose_fix_is_unique():
    """三种冗余在交付之前就没了，而且只少了那三种该少的东西。

    节点集合的变化是可预测的：三条一级分支都在（`b0` 那一支压链后由 `b0-0`
    顶替），`b0`/`b0-0` 两层合成一层，`b1-1` 并进 `b1-0`。近义那一对原样留着。
    """
    ctx = _ctx()
    result = _run(ctx, _chat(_tree()))

    assert result.ok, result.content
    ids = _ids(ctx.candidate.model_dump())
    assert "b0" not in ids and "b0-0" not in ids, "单子链没被压掉"
    assert "b1-1" not in ids, "同级重名没被合并"
    # 近义那一对**必须都还在**：那一类的修法不唯一，收尾不许碰。
    assert {"b2-0", "b2-1"} <= ids
    # 修完之后的诊断里不该再有那两条已经消失的码。
    codes = _codes(result)
    assert "SINGLE_CHILD" not in codes
    assert "DUPLICATE_LABEL" not in codes
    assert "NEAR_DUPLICATE_LABEL" in codes


def test_the_repair_reports_what_it_did_into_data_and_diagnostics():
    """修了几处、哪几条规则、修前修后多少节点：`data["auto_repair"]` + 一条诊断。

    三个数各有各的用途：`steps` 是"这次值不值得开"的读数，`codes` 是"哪条规则
    在实际产物上真的会响"（报告里 `SINGLE_CHILD` 命中 116 次、`DUPLICATE_LABEL`
    少得多，这条数据只能从这里来），`nodes_before / after` 让"树小了多少"可知。
    """
    ctx = _ctx()
    result = _run(ctx, _chat(_tree()))

    report = result.data["auto_repair"]
    assert report["steps"] >= 2
    assert report["codes"].get("SINGLE_CHILD", 0) >= 2      # b0 与 b0-0 各一次
    assert report["codes"].get("DUPLICATE_LABEL", 0) == 1
    assert report["nodes_before"] > report["nodes_after"]
    assert report["skipped"] == 0

    repaired = [item for item in result.data["diagnostics"]
                if item["code"] == "STRUCTURE_AUTO_REPAIRED"]
    assert len(repaired) == 1, "修复要么一条诊断说清，要么不说；不许逐处刷屏"
    assert "合并同级重名" in repaired[0]["message"]
    assert "没有删掉任何内容" in repaired[0]["message"]


def test_the_published_tree_is_the_repaired_one():
    """交付的就是修好的那一棵，`data` 里的节点数与它一致。

    这是"撤销栈仍然只有一条"能测的那一半：修复发生在 `publish_tree` **之前**，
    所以用户按一次 Ctrl+Z 回到生成之前，而不是"第 1 处修复之前"。反过来的实现
    （先交付、再修）会让 `data["auto_repair"]["nodes_after"]` 与 `data["node_count"]`
    对不上——那一版正是要避免的。
    """
    ctx = _ctx()
    result = _run(ctx, _chat(_tree()))

    candidate = ctx.candidate.model_dump()
    assert result.data["node_count"] == result.data["auto_repair"]["nodes_after"]
    assert len(list(_walk(candidate["root"]))) == result.data["node_count"]


def test_repairing_the_delivered_tree_changes_nothing():
    """幂等：对交付的树再跑一次 `repair()` 是 0 步（修到不动点）。

    这条正是 `docs/SUBAGENT-VS-SELFCHECK.md` 对第 0 件提的第 ② 条要求。它不靠
    "收尾调了两次"来保证，而是靠 `repair()` 自己的预算按节点数算（见
    `optimization/repair.py` 的模块注释第 1 条）——修一次就到不动点。
    """
    ctx = _ctx()
    _run(ctx, _chat(_tree()))

    candidate = ctx.candidate
    assert repair(candidate).steps == 0


# ---------------------------------------------------------------- 2. 少修的要报

def test_a_merge_that_would_break_the_contract_is_skipped_and_reported():
    """引用并集 / 子节点数超过契约上限时**放弃那条修复**，并记进 `skipped`。

    两个同名兄弟各有 26 个孩子，合并后 52 个 > `MAX_NODE_CHILDREN`（50）。截断是
    丢内容，丢内容比不修更糟——所以这条放弃。而"放弃了"必须看得见：静默跳过会让
    "这棵树怎么还有一处重名"变成查不出来的问题。
    """
    tree = _tree()
    heavy = {"id": "b1", "label": "差旅报销", "children": [
        {"id": f"b1-{index}", "label": "报销单", "detail": "一句具体的说明。",
         "children": [
             {"id": f"b1-{index}-{position}", "label": f"要点{position}",
              "detail": "一句具体的说明。"}
             for position in range(26 if index == 0 else 25)
         ]}
        for index in range(2)
    ]}
    tree["root"]["children"] = [
        child for child in tree["root"]["children"] if child["id"] != "b1"] + [heavy]

    assert 26 <= MAX_NODE_CHILDREN and 25 <= MAX_NODE_CHILDREN   # 各自合法
    assert 26 + 25 > MAX_NODE_CHILDREN                           # 合并不合法
    ctx = _ctx()
    result = _run(ctx, _chat(tree))

    report = result.data["auto_repair"]
    assert report["skipped"] >= 1
    assert report["codes"].get("DUPLICATE_LABEL", 0) == 0
    # 没修成的那一处仍在结构体检里：用户点「一键修复」时会再看到它一次。
    assert "DUPLICATE_LABEL" in _codes(result)
    summary = [item for item in result.data["diagnostics"]
               if item["code"] == "STRUCTURE_AUTO_REPAIRED"][0]["message"]
    assert "没修" in summary


# ---------------------------------------------------------------- 3. 不该动的没动

def test_cross_branch_duplicates_are_judged_before_the_repair():
    """压链会改 label（`外层 / 内层`），而跨支重复**只比逐字**——判据要跑在修复之前。

    这是一个真的会漏掉的形状：两条一级分支各只有一个孩子、两个孩子同名。修完
    之后它们变成「甲 / 迁移学习」与「乙 / 迁移学习」，`_cross_branch_duplicates`
    就再也看不见了，而模型确实把同一件事写了两遍。

    所以 `duplicates` 在修复之前取（`_generate` 里那一行 `duplicates =
    _cross_branch_duplicates(content.root)`），诊断说的是"模型写了什么"。节点 id
    在压链后仍然有效——压链保留内层 id。
    """
    tree = {"title": TOPIC, "root": {
        "id": "root", "label": TOPIC, "detail": "中心主题",
        "children": [
            {"id": "b0", "label": "监督学习", "children": [
                {"id": "b0-0", "label": "迁移学习", "detail": "一句具体的说明。"},
            ]},
            {"id": "b1", "label": "无监督学习", "children": [
                {"id": "b1-0", "label": "迁移学习", "detail": "一句具体的说明。"},
            ]},
            {"id": "b2", "label": "模型与方法", "children": [
                {"id": "b2-0", "label": "损失函数", "detail": "一句具体的说明。"},
                {"id": "b2-1", "label": "优化器", "detail": "一句具体的说明。"},
            ]},
        ]}}
    ctx = _ctx()
    result = _run(ctx, _chat(tree))

    assert result.data["cross_branch_duplicates"] == 2
    assert "CROSS_BRANCH_DUPLICATE" in _codes(result)
    # 压链确实发生了：两条分支的标题都变成了合成的两层说法。
    labels = [child.label for child in ctx.candidate.root.children]
    assert "监督学习 / 迁移学习" in labels
    assert "无监督学习 / 迁移学习" in labels


def test_switching_it_off_gives_the_pre_wiring_shape(monkeypatch):
    """`MINDMAP_AUTO_REPAIR=0` → 与接线之前**同形**：树原样、没有 `data` 键、
    没有诊断、结构码照旧报三条。

    开关不是为了省钱（这条零调用），是为了能复现接线之前的产物——那份报告的每个
    数字都取自那条路。
    """
    monkeypatch.setenv(AUTO_REPAIR_ENV, "0")
    ctx = _ctx()
    result = _run(ctx, _chat(_tree()))

    assert "auto_repair" not in result.data
    assert "STRUCTURE_AUTO_REPAIRED" not in _codes(result)
    assert {"b0", "b0-0", "b1-0", "b1-1"} <= _ids(ctx.candidate.model_dump())
    assert "SINGLE_CHILD" in _codes(result)


def test_only_the_tree_shape_is_repaired(monkeypatch):
    """鱼骨 / 流程不动手：那两种图的一级是并列类别与独立阶段，判据不一样。

    诊断照旧会响（`check()` 本来就跑在所有图型上），但**改**是另一件事——
    与 `optimization/router.py::_TREE_ONLY` 同一条理由。
    """
    monkeypatch.delenv(AUTO_REPAIR_ENV, raising=False)
    from app.agent import tools

    # 鱼骨的结构那一次返回的仍是**内部那棵树**（骨是 `_shape_output` 按类别折出来的），
    # 所以桩子回的是同一份树；只有骨架那一次换了 system（`fishbone_plan_prompt()`）。
    tree = {"title": TOPIC, "root": {
        "id": "root", "label": TOPIC, "detail": "中心主题",
        "children": [
            {"id": "b0", "label": "人", "children": [
                {"id": "b0-0", "label": "单据齐全", "children": [
                    {"id": "b0-0-0", "label": "发票", "detail": "一句具体的说明。"},
                ]},
            ]},
            {"id": "b1", "label": "机", "children": [
                {"id": "b1-0", "label": "系统", "detail": "一句具体的说明。"},
            ]},
            {"id": "b2", "label": "料", "children": [
                {"id": "b2-0", "label": "材料", "detail": "一句具体的说明。"},
            ]},
            {"id": "b3", "label": "法", "children": [
                {"id": "b3-0", "label": "制度", "detail": "一句具体的说明。"},
            ]},
            {"id": "b4", "label": "环", "children": [
                {"id": "b4-0", "label": "环境", "detail": "一句具体的说明。"},
            ]},
            {"id": "b5", "label": "测", "children": [
                {"id": "b5-0", "label": "校验", "detail": "一句具体的说明。"},
            ]},
        ]}}
    ctx = _ctx()
    result = _run(ctx, _chat(tree, plan_prompt=tools.fishbone_plan_prompt()),
                  diagram_type="fishbone")

    assert result.ok, result.content
    assert "auto_repair" not in result.data
    # 诊断照旧响（`check()` 跑在所有图型上），但树上一个字节都没动。
    assert "SINGLE_CHILD" in _codes(result), "鱼骨上连诊断都不响了，判据变了"
