"""整树生成的契约：骨架从哪来、证据怎么给、三段式怎么接、旧契约不回归。

这条路（`mindmap_generate`）与分片（`mindmap_generate_deep`）共用骨架与检索，
差别只有生成调几次。所以这个文件盯四件事：

  1. **骨架的两个来源**：有材料时用材料自带的章节（`_skeleton_outline`，覆盖率
     ≥60% 才拼进去），没有材料时由模型从主题定。两条路走的是同一次调用。
  2. **证据按支给、每支各检索一次**：退化成"一次主题检索 top_k=5"的表现是
     `queries` 只有一条，而那正是当年整树路径覆盖 5.1% 的原因。
  3. **三段式**：结构一次（只写 id/label/引用、detail 留空）+ 只写说明一次
     （按节点 id 对号返回说明）。第三次调用存在的理由见 `tools._fill_details`。
  4. **三条旧契约不回归**：`EVIDENCE_UNUSED`、`partial`、`node_grow` 预览，以及
     初次限额（80 节点 / 4 层）一个都没动。

第 4 条里的限额是硬约束：一次响应装不下更多，改了它这条路的全部前提就没了。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from app.agent import tools as tools_module
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    DETAIL_ONLY_PROMPT,
    DETAIL_SPLIT_MIN,
    INITIAL_MAX_DEPTH,
    INITIAL_MAX_NODES,
    NO_BRANCH_CAP_RULE,
    OUTLINE_HEADER,
    PLAN_PROMPT,
    build_registry,
)
from app.contracts.source import Source, SourceKind
from app.generation.service import SalvagedOutput

TOPIC = "机器学习的主要方向"
BRANCHES = ("监督学习", "无监督学习", "模型与方法")

# 章节结构清楚的材料：`_skeleton_outline` 的覆盖率会接近 100%，目录一定被拼进去。
OUTLINED_MATERIAL = (
    "# 机器学习讲义\n\n"
    "## 监督学习\n\n"
    "监督学习用带标签的数据训练，分类与回归都属于它。常见模型有线性回归、"
    "决策树与神经网络，评价指标看准确率与召回率。训练集与测试集要分开，"
    "交叉验证用来估计泛化误差；正则化项抑制过拟合，学习率决定每一步走多远。"
    "类别不平衡时看 F1 而不是准确率，标注噪声会直接变成模型的上限。\n\n"
    "## 无监督学习\n\n"
    "无监督学习在无标注的数据上找结构，聚类、降维与异常检测是三类主要任务。"
    "K-means 与层次聚类按距离度量划分，PCA 把高维数据投到低维。"
    "聚类个数通常靠轮廓系数或肘部法则挑，降维之后仍要保留主要方差；"
    "异常检测把密度低处的点当作可疑样本，阈值要按业务代价来定。\n\n"
    "## 强化学习\n\n"
    "强化学习靠与环境交互的奖励信号学习策略，马尔可夫决策过程是它的形式化框架。"
    "值函数方法与策略梯度是两条主要路线。回报折扣因子决定看多远，"
    "探索与利用的平衡靠 ε-贪心或熵正则；仿真到真机的差距要靠域随机化缩小，"
    "奖励设计错了会得到一个会钻空子的策略。\n"
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


def _material(text: str = OUTLINED_MATERIAL, *, source_id: str = "s-ml") -> Source:
    return Source(id=source_id, kind=SourceKind.document, status="parsed",
                  filename="机器学习讲义.md", text=text)


def _run(ctx: RunContext, chat, *, arguments: dict | None = None):
    args = {"topic": TOPIC, "branches": 3}
    args |= arguments or {}
    registry = build_registry("create", has_sources=bool(ctx.request.sources),
                              has_library=False)
    with patch("app.agent.tools.chat_async", new=chat):
        return asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate", arguments=args), ctx))


def _codes(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


def _tree(children: list[dict], *, title: str = TOPIC) -> dict:
    return {"title": title, "root": {
        "id": "root", "label": title, "detail": "中心主题", "children": children}}


def _branch(index: int, label: str, points: list[tuple[str, str]],
            *, refs: list[dict] | None = None) -> dict:
    """一条一级分支 + 它的要点。`points` 是 `(label, detail)`。"""
    return {"id": f"b{index}", "label": label, "children": [
        {"id": f"b{index}-{position}", "label": point_label, "detail": detail,
         "source_refs": refs or []}
        for position, (point_label, detail) in enumerate(points)
    ]}


def _two_points(label: str) -> list[tuple[str, str]]:
    return [(f"{label}·一", "一句具体的说明。"), (f"{label}·二", "另一句具体的说明。")]


def _walk_payload(node: dict):
    yield node
    for child in node.get("children") or []:
        if isinstance(child, dict):
            yield from _walk_payload(child)


def _detail_answer(tree: dict, *, text: str = "这句说明写到八十个字以上，讲清它是什么、"
                                                 "怎么做、有什么条件与数字，"
                                                 "这样第三段的产出才符合长度目标。") -> dict:
    """第三段的模型替身：按 `tree` 里的 id 造一份 `{id: 说明}`。

    长度按提示词的目标区间（80–150 字）写：拿它测出来的 detail 才是"第三段成功"
    的样子，写一句短语会把断言变成在测兜底。
    """
    return {node["id"]: text for node in _walk_payload(tree["root"]) if node.get("id")}


def _chat(tree: dict, *, seen: list[tuple[str, str]] | None = None,
          on_delta_chunks: int = 0, details: dict | None = None):
    """模型替身：骨架回 `BRANCHES`，结构回 `tree`，第三段回 `details`。

    `details` 传 `{}` 表示"模型一个说明也没写回来"——兜底（保留原 detail、
    记 `DETAIL_ROUND_EMPTY`）就是靠它测的；不传时按 `tree` 的 id 造一份合格的说明。

    `on_delta_chunks > 0` 时把整棵树的 JSON 按这个段数喂给 `on_delta`——预览那条
    路只有真收到增量才会往外推帧，假流式是测不出 `node_grow` 的。
    """
    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if seen is not None:
            seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(BRANCHES)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return _detail_answer(tree) if details is None else details
        if on_delta is not None and on_delta_chunks:
            text = json.dumps(tree, ensure_ascii=False)
            size = max(1, len(text) // on_delta_chunks)
            for start in range(0, len(text), size):
                await on_delta(text[start:start + size])
        return tree
    return chat


def _structure_users(seen: list[tuple[str, str]]) -> list[str]:
    """结构那一次的 user（不含骨架与第三段）。"""
    return [user for system, user in seen
            if not system.startswith(PLAN_PROMPT)
            and not system.startswith(DETAIL_ONLY_PROMPT)]


def _detail_users(seen: list[tuple[str, str]]) -> list[str]:
    """第三段（只写说明）那一次的 user。"""
    return [user for system, user in seen if system.startswith(DETAIL_ONLY_PROMPT)]


def _three_branches(tree: dict | None = None) -> dict:
    return tree or _tree([
        _branch(index, label, _two_points(label))
        for index, label in enumerate(BRANCHES)
    ])


# ---------------------------------------------------------------- 1. 骨架从哪来


def test_skeleton_uses_the_material_outline():
    """有材料且目录覆盖率够 → 骨架那一次的 user 里有材料自带的章节。

    目录进的是 `_plan_ask` 的 user 消息（不是 system）：它的语义是"这一次的输入
    长什么样"，而 system 是跨请求稳定的规则。
    """
    seen: list[tuple[str, str]] = []
    result = _run(_ctx([_material()]), _chat(_three_branches(), seen=seen))

    assert result.ok, result.content
    plan_system, plan_user = seen[0]
    assert plan_system.startswith(PLAN_PROMPT)
    assert OUTLINE_HEADER in plan_user
    # 三个章节名都在（正文一行都不进 prompt）
    for chapter in ("监督学习", "无监督学习", "强化学习"):
        assert chapter in plan_user
    assert "K-means 与层次聚类" not in plan_user


def test_skeleton_sees_the_material_inventory():
    """有材料时骨架还看得见**清单**：几份、每份多长（正文仍然一行都不进）。

    这条与上一条不重复：目录说"有哪几块"，清单说"有几份东西、各多长"。16 份独立
    摘要抽不出章节，旧行为下骨架对"有 16 份"这件事一无所知，于是按主题猜出 3 支
    （实测见 `docs/GRAPH-SCALE.md` 第 4 节）。清单是让那个数进入骨架那一次调用的
    唯一入口，所以它必须在**整树这条路上**也被接线到（分片那条有 `test_outline.py`）。
    """
    seen: list[tuple[str, str]] = []
    source = _material()
    chars = len((source.text or "").strip())
    result = _run(_ctx([source]), _chat(_three_branches(), seen=seen))

    assert result.ok, result.content
    plan_user = seen[0][1]
    # 表头只报字数、不报份数（`docs/GRAPH-SCALE.md` 第 11 节：那个数字在薄批上会被
    # 读成"该切几条分支"，两份摘要 → 两条分支 → `MIN_BRANCHES` 判废）。
    assert f"这次的材料（合计 {chars:,} 字" in plan_user
    assert "份，" not in plan_user.split("这次的材料", 1)[1].split("\n", 1)[0]
    assert "1. 机器学习讲义.md" in plan_user
    # 清单在主题之后、收尾判据之前（顺序即权重），并且带上了"怎么用这个数"那句。
    # 收尾那一端原先锚在"最多 3 条"上：条数不再由代码给（`tools.NO_BRANCH_CAP_RULE`），
    # 那句已经不进提示词，所以改锚判据——位置不变，钉的还是"清单在前、收尾在后"。
    assert (plan_user.index(TOPIC) < plan_user.index("这次的材料")
            < plan_user.index(NO_BRANCH_CAP_RULE))
    # 只有结构：正文里那些句子一句都不在
    assert "K-means" not in plan_user


def test_skeleton_falls_back_to_the_topic_without_material():
    """无材料 → 没有目录可拼，骨架由模型从主题定；这条路一次生成照常跑完。"""
    seen: list[tuple[str, str]] = []
    result = _run(_ctx(), _chat(_three_branches(), seen=seen))

    assert result.ok, result.content
    assert OUTLINE_HEADER not in seen[0][1]
    # 没有材料时连清单都没有：骨架那一次的 user 与加清单之前逐字节相同
    assert "这次的材料" not in seen[0][1]
    assert TOPIC in seen[0][1]
    # 没有材料时骨架的 system 里有那两句纯文本契约（别写引用、可以自由展开）
    assert "不要写 source_refs" in seen[0][0]
    assert result.data["retrieval"]["mode"] == "off"
    assert result.data["node_count"] == 1 + len(BRANCHES) * 3


# ---------------------------------------------------------------- 2. 证据按支给


def test_each_branch_is_retrieved_once_and_grouped_for_the_model():
    """三条分支 → 三次检索；生成那一次的 user 里三组证据，各带分支归属。

    这条与 `test_one_path.py` 里的同名断言分工不同：那条验"带材料时会检索"，
    这条验**形状**——分组表头里必须有分支标题，否则模型无法知道哪条命中属于哪支
    （分片那条路不需要这层，它每次只给一支看）。
    """
    calls: list[str] = []
    real_search = tools_module._search_branch

    async def counting(sources, title, intents=()):
        calls.append(title)
        return await real_search(sources, title, intents)

    seen: list[tuple[str, str]] = []
    with patch.object(tools_module, "_search_branch", counting):
        result = _run(_ctx([_material()]), _chat(_three_branches(), seen=seen))

    assert result.ok, result.content
    assert calls == list(BRANCHES)
    generate_user = _structure_users(seen)[0]
    for index, title in enumerate(BRANCHES, start=1):
        assert f"第 {index} 支「{title}」的检索结果" in generate_user
    # 每条 query 都是分支标题本身（不拼主题，那是实测选出来的）
    assert result.data["retrieval"]["queries"] == list(BRANCHES)
    assert result.data["retrieval"]["top_k"] == 3


def test_generation_is_three_calls_after_the_skeleton():
    """骨架一次 + 结构一次 + 只写说明一次：总调用 3，与分支数无关。

    第三段的存在理由是"一次输出里塞几十段说明"这件事本身（数字见
    `docs/ONE-CALL.md`）：结构与说明拆开之后，说明那一次只面对一份
    id / label / 层级清单。
    """
    seen: list[tuple[str, str]] = []
    result = _run(_ctx(), _chat(_three_branches(), seen=seen))

    assert result.ok, result.content
    assert len(seen) == 3, [system[:20] for system, _ in seen]
    # 结构那一次的 user 里必须带着骨架清单：模型据此写出一级分支，而不是自己再定
    structure_user = _structure_users(seen)[0]
    for index, title in enumerate(BRANCHES, start=1):
        assert f"{index}. {title}" in structure_user
    assert "不要增删改名" in structure_user
    # 第三段只看到 id / label / 位置，看不到整棵树的结构（它不需要，也不该动）
    detail_user = _detail_users(seen)[0]
    for title in BRANCHES:
        assert title in detail_user
    assert result.data["detail"]["requested"] == result.data["node_count"]
    assert result.data["detail"]["filled"] == result.data["detail"]["requested"]


# ---------------------------------------------------------------- 3. 跨支重复


def test_cross_branch_duplicates_are_reported_but_kept():
    """两支写了一字不差的同一节点 → 报一条诊断、保留两个节点、计数进 `data`。

    这是分片那条路的实测缺陷（56 节点的图里 6 处以上），也是这次回到一次生成
    要说清的收益。代码不做删除：删哪个是用户的决定。
    """
    same = ("无监督学习：无标注找结构", "在无标注数据上找结构，聚类与降维是主要任务。")
    tree = _tree([
        _branch(0, BRANCHES[0], [same, ("监督学习·二", "有标注训练。")]),
        _branch(1, BRANCHES[1], [same, ("无监督学习·二", "无标注找结构。")]),
        _branch(2, BRANCHES[2], [("模型与方法·一", "模型是载体。")]),
    ])
    ctx = _ctx()
    result = _run(ctx, _chat(tree))

    assert result.ok, result.content
    assert "CROSS_BRANCH_DUPLICATE" in _codes(result)
    # 计数是**节点数**：一个说法写了两遍 = 2 个重复节点（库里那张图是 3 个说法
    # 各写 2 遍 = 6，与用户实测的口径一致）。
    assert result.data["cross_branch_duplicates"] == 2
    labels = [child.label for child in ctx.candidate.root.children[1].children]
    assert same[0] in labels, "重复的节点被删掉了——这条诊断只报不删"
    # 两个都还在树上
    kept = [
        node.label
        for branch in ctx.candidate.root.children for node in branch.children
        if node.label == same[0]
    ]
    assert len(kept) == 2


def test_same_label_with_different_detail_still_counts_as_a_duplicate():
    """同名不同说明**也算重复**：判据是 label 级，这是拿真实数据校准出来的。

    库里那三张 `机器学习的主要方向`（56/57 节点）各有 6 个跨支重复节点，而它们的
    detail **各不相同**——按"连 detail 也要相同"判，这条诊断对真实缺陷完全瞎掉。
    同一个概念挂在两条一级分支下，无论说明怎么写，用户都会看到两块内容讲同一件事。

    保守的那一半在**不做语义归一**：`无监督学习` 与 `非监督学习` 不算重复。
    """
    tree = _tree([
        _branch(0, BRANCHES[0], [("迁移学习", "它是监督学习的一种延伸：先在大数据集上预训练。")]),
        _branch(1, BRANCHES[1], [("迁移学习", "它也能和强化学习结合：把仿真里学到的策略搬过来。")]),
        _branch(2, BRANCHES[2], [("模型与方法·一", "模型是载体。")]),
    ])
    result = _run(_ctx(), _chat(tree))

    assert result.ok, result.content
    assert "CROSS_BRANCH_DUPLICATE" in _codes(result)
    assert result.data["cross_branch_duplicates"] == 2


def test_near_synonyms_are_not_a_duplicate():
    """同义不同名不算重复：判据只做空白折叠，不做语义归一。

    这条是"宁可漏、不误报"的那一半——误报会让整条信号变成需要人工逐条确认的
    噪声，而这条诊断本身不删任何节点，价值全在"值得看一眼"。
    """
    tree = _tree([
        _branch(0, BRANCHES[0], [("非监督学习", "在无标注数据上找结构。")]),
        _branch(1, BRANCHES[1], [("自监督学习", "同样是在无标注数据上找结构。")]),
        _branch(2, BRANCHES[2], [("模型与方法·一", "模型是载体。")]),
    ])
    result = _run(_ctx(), _chat(tree))

    assert result.ok, result.content
    assert "CROSS_BRANCH_DUPLICATE" not in _codes(result)
    assert result.data["cross_branch_duplicates"] == 0


# ---------------------------------------------------------------- 4. 旧契约不回归


def test_evidence_unused_still_fires():
    """证据拼进了 prompt、树上一条引用都没有 → `EVIDENCE_UNUSED` 必须报。

    这是 `rag` 形态下唯一没有信号的失败面：模型看着证据却没写引用，而产出与
    "本来就没有证据"完全一样。
    """
    tree = _three_branches()  # 三个分支都不带 source_refs
    result = _run(_ctx([_material()]), _chat(tree))

    assert result.ok, result.content
    assert result.data["retrieval"]["branches_with_hits"] >= 1
    assert "EVIDENCE_UNUSED" in _codes(result)
    assert result.data["source_refs"] == {"declared": 0, "kept": 0}


def test_partial_is_reported_when_the_output_is_truncated():
    """输出被长度上限截断（`SalvagedOutput`）→ `partial=true` + 一条 warn。

    截断本身不是失败（回收出来的树仍然可用），但 `ok=true` 与"完整成功"长得
    一模一样，差别只有这个字段与那条诊断。
    """
    tree = SalvagedOutput(_three_branches(), recovered=7)
    result = _run(_ctx(), _chat(tree))

    assert result.ok, result.content
    assert result.data["partial"] is True
    assert "OUTPUT_TRUNCATED_RECOVERED" in _codes(result)


def test_node_grow_streams_the_whole_tree():
    """开了事件通道时，整树响应每写出一个完整节点就推一帧 `node_grow`。

    第一帧必须是根（`path=None`），之后每一帧的 `path` 是**父节点的索引链**——
    这条与分片那条路的预览协议同形，前端不必分叉。
    """
    frames: list[dict] = []

    async def emit(frame) -> None:
        frames.append(frame)

    ctx = _ctx()
    ctx.emit = emit
    result = _run(ctx, _chat(_three_branches(), on_delta_chunks=12))

    assert result.ok, result.content
    grows = [frame for frame in frames if frame.get("type") == "node_grow"]
    assert grows, frames
    assert grows[0]["path"] is None and grows[0]["label"] == TOPIC
    # 一级分支挂在根上（`path=()`），二级挂在它的分支索引下。帧顺序跟着 JSON
    # 文本的增量解析走（深度优先），所以只断言"这几类帧都在"，不钉先后。
    assert [frame["path"] for frame in grows].count([]) == len(BRANCHES)
    assert any(len(frame["path"] or []) == 1 for frame in grows)


def test_initial_limits_are_unchanged():
    """初次限额一个都没动：80 个节点、root 之下 3 层（含根 4 层）。

    一次响应装不下更多——这正是分片那条路存在的理由，也是它在真实用法（100%
    无材料）里没收益的原因。数字改了，这条路的全部前提就没了。
    """
    assert INITIAL_MAX_NODES == 80
    assert INITIAL_MAX_DEPTH == 3
    result = _run(_ctx(), _chat(_three_branches()))
    assert result.ok, result.content
    assert result.data["node_limit"] == INITIAL_MAX_NODES
    assert result.data["depth_limit"] == INITIAL_MAX_DEPTH
    assert result.data["partial"] is False


def test_over_the_readability_limit_is_folded_not_clipped():
    """超过 80 → **折叠**，不是裁：一级分支一条不少，被并的那些标题留在聚合节点里。

    这条用例以前钉的是"裁到 80、记 `TREE_CLIPPED_TO_INITIAL`、标 `partial`"。裁那条路
    现在只剩"四档折叠都折不动"之后的兜底（`tools._fold_tree`），正常形态是：

        20 条一级分支 × 每支 6 个要点 = 141 个节点
        → 根下超过 8 个的那 12 支并成一个「其他 12 项」
        → 58 个节点、`cut_nodes=0`、落树 9 个一级节点（8 支 + 1 个聚合）

    为什么这条比原先那条重要：裁丢掉的是后 12 支**整条**（用户看到 8 支的一张图，不知道
    少了什么），折叠之后根下第 9 个节点明说"这里原本还有 12 项"，而且它带着那 12 支的
    标题——这是"丢层级"与"丢内容"的差别。
    """
    wide = _tree([
        _branch(index, f"分支{index}", [
            (f"要点{index}-{position}", "一句具体的说明。") for position in range(6)
        ])
        for index in range(20)
    ])  # 1 + 20 + 120 = 141 个节点
    ctx = _ctx()
    result = _run(ctx, _chat(wide))

    assert result.ok, result.content
    assert result.data["node_count"] < INITIAL_MAX_NODES
    assert result.data["cut_nodes"] == 0           # 一个节点都没被裁
    assert result.data["folded_rollups"] == 1
    assert result.data["partial"] is True
    assert "TREE_FOLDED_TO_READABILITY" in _codes(result)
    assert "TREE_CLIPPED_TO_INITIAL" not in _codes(result)

    top = ctx.candidate.root.children
    assert len(top) == 9                            # 8 支 + 1 个聚合，比裁到 8 支多活一条
    assert top[0].label == "分支0" and len(top[0].children) == 6   # 前面几支原样
    rollup = top[-1]
    assert rollup.label == "其他 12 项"
    # 被并的 12 支的标题一个不落地在 detail 里（读图的人据此知道少了什么）
    for index in range(8, 20):
        assert f"分支{index}" in rollup.detail
    # 而这份清单**不能被第三段重写**：它是这个节点唯一的说明（`skip_ids` 里那一格）。
    # 实测厚批就是这样丢的——聚合节点还在、`含：…` 没了，用户再也说不清"其他 3 项"是哪三项。
    assert rollup.detail.startswith("含：")
    # 不是"到底了"：这一支还能从这里长回来（`NODE_EXPANDABLE` 的三态）
    assert rollup.expandable == "unknown"


@pytest.mark.parametrize("branches", [3, 6])
def test_branch_count_only_changes_the_skeleton_size(branches):
    """分支数变化只影响骨架规模，不改变"骨架 + 结构 + 说明"这条结构。

    "说明"那一段在节点够多时拆成**两次并发**（`DETAIL_SPLIT_MIN`）：3 支的树 10 个
    节点不拆、3 次调用；6 支的树 19 个节点拆、4 次。所以这里不能硬编码 3——要钉的
    性质是"分支数本身不是调用次数的决定因素，节点数才是"。
    """
    titles = tuple(f"分支{index}" for index in range(branches))
    tree = _tree([
        _branch(index, title, _two_points(title))
        for index, title in enumerate(titles)
    ])
    seen: list[tuple[str, str]] = []
    answers = {"branches": list(titles)}

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if seen is not None:
            seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return answers
        if system.startswith(DETAIL_ONLY_PROMPT):
            return _detail_answer(tree)
        return tree

    result = _run(_ctx(), chat, arguments={"branches": branches})
    assert result.ok, result.content
    detail_calls = len([user for system, user in seen
                        if system.startswith(DETAIL_ONLY_PROMPT)])
    assert detail_calls == (1 if result.data["node_count"] < DETAIL_SPLIT_MIN else 2)
    assert len(seen) == 2 + detail_calls
    assert result.data["planned_branches"] == branches
