"""交叉指认（`relations`）在三段式那条路上**有手段可用**。

背景（`docs/CROSS-BRANCH-DUPLICATES.md` §4.1）：`_SKELETON_BODY` 从很早就在要求
"需要交叉指认时……直接指向已有节点"，而"指向"的写法（`relations`）此前只在
`graph_mode` 下追加。于是这条禁令**不可执行**——模型只剩删内容或复制节点两条路，
而复制那一条会连带把 detail 改写成材料里没有的说法。

这个文件钉四件事：

  1. 三段式的结构那一次 system 里有 `CROSS_REFERENCE_RULES`，且在**最末尾**
     （紧挨输出位置，与 `GRAPH_RELATION_RULES` 同一个位置逻辑）；
  2. 两段式（`graph_mode`）**没有**它——那里已经有六类 kind 那一份，加第二份会让
     同一个字段收到两套上限与两套 kind；
  3. 三段式真写了 `relations` 时被**收下**（端点写标题也认），不再因为契约的
     "dangling 硬拒"把整棵树判废；
  4. 认不出的端点丢一条、树不动，并留一条 `CROSS_REFERENCE_DROPPED` 诊断。

第 3 条是这次的关键：没有它，加了规则等于给模型一根会把自己炸掉的引线。
"""
from __future__ import annotations

import asyncio

from unittest.mock import patch

from app.agent import tools as tools_module
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    CROSS_REFERENCE_MAX,
    CROSS_REFERENCE_RULES,
    DETAIL_ONLY_PROMPT,
    DETAIL_PROMPT_OPEN,
    GRAPH_MAX_RELATIONS,
    GRAPH_RELATION_RULES,
    PLAN_PROMPT,
    build_registry,
)
from app.contracts.source import Source, SourceKind
from app.knowledge.graph_store import GraphEntity, GraphLink

TOPIC = "操作系统课程大纲"
BRANCHES = ("课程内容与模块", "学时分配", "考核方式")

MATERIAL = (
    "# 操作系统教学大纲\n\n"
    "## 课程内容与模块\n\n"
    "课程分六个部分：概述、进程管理、内存管理、文件系统、设备管理、并发与同步。"
    "概述讲操作系统的定义与作用，进程管理讲调度与通信，内存管理讲分页与虚拟内存，"
    "文件系统讲目录组织与磁盘分配，设备管理讲中断与驱动，并发与同步讲互斥与信号量。"
    "每一部分都配一次实验，实验要求在 Linux 上完成，报告里要写出观察到的时间与次数。"
    "内存管理部分按页表讲解地址转换，进程管理部分按状态图讲解切换，"
    "文件系统部分按 inode 讲解索引，设备管理部分按中断向量讲解响应。\n\n"
    "## 学时分配\n\n"
    "总学时 64，其中概述 4 学时、进程管理 12 学时、内存管理 12 学时，"
    "其余部分各 8 学时，考试 4 学时。学时按周排布，每周一次课，"
    "第 8 周做期中检查，第 16 周结课，结课后两周内安排补考。"
    "实验学时已经算在各部分里，不另加课时；补考只考期末同样的范围。\n\n"
    "## 考核方式\n\n"
    "平时成绩占 30%（作业与实验），期末考试占 70%，实验要求提交实验报告。"
    "作业迟交按每天 10% 扣分，实验报告缺一次扣 5 分，缺三次平时成绩不及格。"
    "期末卷面分低于 50 分时，总评直接按卷面判不及格，补考按同一口径计算。\n"
)


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


def _material() -> Source:
    return Source(id="s-os", kind=SourceKind.document, status="parsed",
                  filename="操作系统教学大纲.md", text=MATERIAL)


def _node(node_id: str, label: str, children: list[dict] | None = None) -> dict:
    return {"id": node_id, "label": label, "detail": "", "children": children or []}


def _tree(relations: list[dict] | None = None) -> dict:
    tree = {
        "title": TOPIC,
        "root": _node("root", TOPIC, [
            _node("b1", BRANCHES[0], [
                _node("b1-1", "内存管理"), _node("b1-2", "文件系统")]),
            _node("b2", BRANCHES[1], [
                _node("b2-1", "内存管理 12 学时"), _node("b2-2", "总学时 64")]),
            _node("b3", BRANCHES[2], [_node("b3-1", "实验报告")]),
        ]),
    }
    if relations is not None:
        tree["relations"] = relations
    return tree


def _detail_answer() -> dict:
    text = ("这句说明写到八十个字以上，把这个要点是什么、怎么做、有什么条件与数字"
            "都讲清楚，这样第三段交出来的东西才符合长度目标，也才像一份真的说明。")
    ids = ["root", "b1", "b1-1", "b1-2", "b2", "b2-1", "b2-2", "b3", "b3-1"]
    return {node_id: text for node_id in ids}


def _chat(tree: dict, *, seen: list[tuple[str, str]] | None = None):
    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if seen is not None:
            seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(BRANCHES)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return _detail_answer()
        return tree
    return chat


def _run(ctx: RunContext, chat, *, arguments: dict | None = None):
    args = {"topic": TOPIC, "branches": 3}
    args |= arguments or {}
    registry = build_registry("create", has_sources=bool(ctx.request.sources),
                              has_library=False)
    with patch("app.agent.tools.chat_async", new=chat):
        return asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate", arguments=args), ctx))


def _structure_systems(seen: list[tuple[str, str]]) -> list[str]:
    return [system for system, _ in seen
            if not system.startswith(PLAN_PROMPT)
            and not system.startswith(DETAIL_ONLY_PROMPT)]


def _codes(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


# ---------------------------------------------------------------- 1. 规则在哪一段


def test_three_stage_structure_prompt_carries_the_cross_reference_rules():
    """三段式的结构那一次：system 末尾就是交叉指认规则。"""
    seen: list[tuple[str, str]] = []
    result = _run(_ctx([_material()]), _chat(_tree(), seen=seen))

    assert result.ok, result.content
    systems = _structure_systems(seen)
    assert len(systems) == 1
    system = systems[0]
    assert CROSS_REFERENCE_RULES in system
    # 位置：紧挨输出位置的那一段（与 `GRAPH_RELATION_RULES` 同一个理由）。
    assert system.endswith(CROSS_REFERENCE_RULES)
    # 两份规则互斥：三段式不该收到图谱那份（六类 kind、上限 30）。
    assert GRAPH_RELATION_RULES not in system


def test_the_rule_says_how_not_only_that_it_is_forbidden():
    """"什么时候写 / 写哪个字段 / kind 两个值 / why 必填 / 上限"五件事都在。

    这一段存在的理由就是"禁令不可执行"：只写"不要复制"（`_SKELETON_BODY` 早就写了）
    实测无效。所以这里钉的是**手段**的每一件零件，而不是措辞。
    """
    assert "relations" in CROSS_REFERENCE_RULES
    assert "references" in CROSS_REFERENCE_RULES
    assert "part_of" in CROSS_REFERENCE_RULES
    assert "why" in CROSS_REFERENCE_RULES
    assert str(CROSS_REFERENCE_MAX) in CROSS_REFERENCE_RULES
    assert "什么时候写" in CROSS_REFERENCE_RULES
    # 超过上限该干什么（合并节点），不是只说"最多 N 条"。
    assert "合并节点" in CROSS_REFERENCE_RULES
    # 上限比图谱那份小：两条路服务的是不同的东西（30 来自材料里明写的实体关系，
    # 10 是"同一件事被写了两遍"这一种补救）。10 的来历是实测，不是画布口径——
    # `scripts/measure_cross_reference_quota.py` 的两臂读数见工具常量那一段注释。
    assert CROSS_REFERENCE_MAX < GRAPH_MAX_RELATIONS
    # 位置那一句与图谱那份同源（模型会把 relations 塞进 root 里面，而 Node 是
    # `extra="forbid"`）。
    assert "不要**把它放进 root 里面" in CROSS_REFERENCE_RULES


def test_graph_mode_does_not_get_the_second_copy(monkeypatch):
    """两段式只有六类 kind 那一份：同一个字段两套规矩会互相打架。

    这条不是"洁癖"：两份规则的上限不同（30 / 8）、kind 集合不同（六值 / 两个值），
    同时给出时模型只能二选一地违背其中一份，而违背哪一份都看不出来。
    """
    monkeypatch.setattr(tools_module, "graph_skeleton_enabled", lambda: True)
    entities = [GraphEntity(name=label, type="concept", degree=10 - index)
                for index, label in enumerate(BRANCHES)]
    links = [GraphLink(src=BRANCHES[0], dst=BRANCHES[1], relation="包含",
                       source_id="s-none", start=0, end=2, weight=1.0)]
    monkeypatch.setattr(tools_module.graph_store, "subgraph",
                        lambda **kwargs: (entities, links))

    seen: list[tuple[str, str]] = []
    two_stage = _tree([
        {"source": "b1-1", "target": "b2-1", "kind": "references",
         "why": "两处说的是同一个内存管理模块", "confidence": 0.8},
    ])
    ctx = _ctx()
    result = _run(ctx, _chat(two_stage, seen=seen))

    assert result.ok, result.content
    systems = _structure_systems(seen)
    assert systems, "两段式的结构调用没有发生"
    system = systems[0]
    assert GRAPH_RELATION_RULES in system
    assert CROSS_REFERENCE_RULES not in system
    # 关系照旧收下（两段式的老能力没有被这次改动碰坏）
    assert [relation.target for relation in ctx.candidate.relations] == ["b2-1"]


# ---------------------------------------------------------------- 2. 写了要收下


def test_relations_written_by_the_three_stage_path_are_kept():
    """端点写**标题**也认：三段式的 id 是模型自己编的，它经常直接抄标题。

    关键不是"能解析"，而是**不判废**：`_validate_relations` 对 dangling 是硬拒，
    `relations` 若留在 raw 里交给 `model_validate`，一条连错的关系就把整棵树换掉。
    """
    relations = [{"source": "内存管理", "target": "内存管理 12 学时",
                  "kind": "references", "why": "学时分配表里的这一项就是课程内容里的那个模块"}]
    ctx = _ctx([_material()])
    result = _run(ctx, _chat(_tree(relations)))

    assert result.ok, result.content
    content = ctx.candidate
    assert len(content.relations) == 1
    relation = content.relations[0]
    assert (relation.source, relation.target) == ("b1-1", "b2-1")
    assert relation.kind is not None and relation.kind.value == "references"
    assert relation.why
    # 三段式自己的账：键只在模型真写过时出现，与 `graph` 那个键互斥。
    assert result.data["cross_reference"]["declared"] == 1
    assert result.data["cross_reference"]["relations"] == 1
    assert result.data["cross_reference"]["relation_limit"] == CROSS_REFERENCE_MAX
    assert "graph" not in result.data


def test_an_unresolvable_endpoint_costs_one_line_not_the_tree():
    """一条端点写错的关系：丢那一根虚线，树与其余关系照旧。

    代码**不**替模型判"这个 kind 在这条路上有没有依据"：提示词限定 references /
    part_of，而机械判据判不准语义（与 `NEAR_DUPLICATE_LABEL` 要人判同一条哲学）。
    这里只钉三条**确定性**的过滤：端点解析不出、没写 why、kind 不在契约六值里。
    """
    relations = [
        {"source": "b1-1", "target": "b2-1", "kind": "references", "why": "两处是同一件事"},
        {"source": "不存在的节点", "target": "b2-1", "kind": "part_of", "why": "写错了端点"},
        {"source": "b1-1", "target": "b2-2", "kind": "depends_on", "why": "六值之内，收下"},
        {"source": "b1-1", "target": "b2-2", "kind": "references", "why": ""},
        {"source": "b1-2", "target": "b2-2", "kind": "见第 3 章", "why": "kind 认不出"},
    ]
    ctx = _ctx([_material()])
    result = _run(ctx, _chat(_tree(relations)))

    assert result.ok, result.content
    content = ctx.candidate
    assert len(content.relations) == 2
    assert "CROSS_REFERENCE_DROPPED" in _codes(result)
    drops = result.data["cross_reference"]["relation_drops"]
    # dangling 1（端点解析不出）+ no_reason 2（空 why、kind 不在六值里）
    assert drops["dangling"] == 1
    assert drops["no_reason"] == 2


def test_no_relations_means_no_extra_key_and_no_noise():
    """模型没写关系时：不建空数组、不报"写了 0 条收下 0 条"的诊断。"""
    result = _run(_ctx([_material()]), _chat(_tree()))

    assert result.ok, result.content
    assert "cross_reference" not in result.data
    assert "CROSS_REFERENCE_DROPPED" not in _codes(result)
    assert "CROSS_REFERENCE_AT_LIMIT" not in _codes(result)


# ----------------------------------------------------- 3. 顶满 ≠ 被截（两条不同的码）


def _wide_tree(count: int) -> dict:
    """三支各 5 个叶子：够放 `count` 条不重复的跨父关系。

    `_tree()` 只有 8 个跨父对，装不下现在的上限（10），也不该为了这条用例把
    `_tree()` 改大——它上面那几条断言钉的是"收下几条"，形状一动读数全变。
    三支而不是两支：`_generate` 有"一级分支少于 3 条就判不合法"那道闸。
    """
    sides = [("A", "甲支", "甲"), ("B", "乙支", "乙"), ("C", "丙支", "丙")]
    body = [_node(tag, label, [_node(f"{tag.lower()}{index}", f"{prefix}{index}")
                              for index in range(5)])
            for tag, label, prefix in sides]
    pairs = [(f"{left.lower()}{index}", f"{right.lower()}{other}")
             for (left, _, _), (right, _, _) in
             [(sides[i], sides[j]) for i in range(3) for j in range(3) if i != j]
             for index in range(5) for other in range(5)]
    tree = _tree()
    tree["root"]["children"] = body
    tree["relations"] = [
        {"source": source, "target": target, "kind": "references",
         "why": "两处说的是同一件事"}
        for source, target in pairs[:count]
    ]
    return tree


def test_hitting_the_limit_is_reported_as_a_limit_not_as_a_drop():
    """顶满上限时报 `CROSS_REFERENCE_AT_LIMIT`，且**不**报 `DROPPED`。

    两条码说的是两件事：`DROPPED` = 代码截掉了；`AT_LIMIT` = 模型自己写到这里就停
    （实测 `limited` 全程为 0）。混成一条会把"模型压着没写"读成"代码砍掉了"。
    """
    result = _run(_ctx([_material()]), _chat(_wide_tree(CROSS_REFERENCE_MAX)))

    assert result.ok, result.content
    account = result.data["cross_reference"]
    assert account["declared"] == CROSS_REFERENCE_MAX
    assert account["relations"] == CROSS_REFERENCE_MAX
    assert account["relation_limit"] == CROSS_REFERENCE_MAX
    assert "CROSS_REFERENCE_AT_LIMIT" in _codes(result)
    assert "CROSS_REFERENCE_DROPPED" not in _codes(result)


def test_one_below_the_limit_says_nothing():
    """差一条不报：那条码只回答"是不是压在上限上"，不回答"写得多不多"。"""
    result = _run(_ctx([_material()]), _chat(_wide_tree(CROSS_REFERENCE_MAX - 1)))

    assert result.ok, result.content
    assert result.data["cross_reference"]["relations"] == CROSS_REFERENCE_MAX - 1
    assert "CROSS_REFERENCE_AT_LIMIT" not in _codes(result)


def test_the_rule_does_not_reach_the_degraded_three_stage_path():
    """无材料、无库片段的降级路径不给结构规则，也就不给这条规则。

    那一条 system 里没有"不要跨支重复"的禁令（`shape_rules` 整段没有给），教它
    "用关系指代"只会让它去指一棵没被告知形状的树。
    """
    seen: list[tuple[str, str]] = []
    result = _run(_ctx(), _chat(_tree(), seen=seen))

    assert result.ok, result.content
    system = _structure_systems(seen)[0]
    assert CROSS_REFERENCE_RULES not in system
    # 但那一次的结构规则确实没给（这才是上面那条断言的前提，不是巧合）
    assert DETAIL_PROMPT_OPEN in system


# ---------------------------------------------------------------- 3. 落库 / 重开


def test_relations_survive_a_save_and_a_reopen(tmp_path, monkeypatch):
    """落库 → 重开：关系线在 jsonb 里，不在内存里。

    `mindmaps.content` 存的是整份 `DiagramContent.model_dump_json()`，所以 `relations`
    天然跟着走——这条用例钉的是"整份"这件事不被哪天的一次投影改坏（比如有人为了
    省空间只存 `root` + `edges`，那 `relations` 就会在刷新后消失，而画布上什么都不报）。
    """
    from app.mindmaps import repository

    monkeypatch.setenv("MINDMAP_DB_PATH", str(tmp_path / "t.db"))

    relations = [{"source": "内存管理", "target": "内存管理 12 学时",
                  "kind": "references", "why": "同一件事在两支下都提到"}]
    ctx = _ctx([_material()])
    result = _run(ctx, _chat(_tree(relations)))
    assert result.ok, result.content
    content = ctx.candidate

    record = repository.create_map(content)
    reopened = repository.get_map(record.id)
    assert reopened is not None
    assert [(item.source, item.target, item.kind.value)
            for item in reopened.content.relations] == [("b1-1", "b2-1", "references")]
    assert reopened.content.relations[0].why == "同一件事在两支下都提到"
