"""每段说明写多长：**材料撑得住多久就写多久**（`MINDMAP_DETAIL_SCALE`）。

上一轮只把节点数接上了材料，说明长度仍是常量，于是同一批材料上还是配额驱动：

    48 节点 × 130 字（现状实测均长）= 6,240 字 · 材料 5,616 字 → κ 0.90（还是膨胀）

这个文件守两条：

  1. **算术**（`budget.detail_span`）：支撑 → 目标均长 → 区间，以及那道与材料无关的
     地板（3 句 × 25 字 = 75 字）。四个真实批次各算一遍，结论要能一眼看出对错；
  2. **接线的方向**：分母是"这一次真的要写说明的节点数"，不是配额；开关关着时提示词
     与出账**逐字节不变**（`data["detail"]` 不多一个键）。

第 2 条比第 1 条重要：这个开关默认关，所以它最大的风险不是算错，而是**在关着的时候
把现状改了**。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.agent import tools as tools_module
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    DETAIL_MAX_CHARS,
    DETAIL_MIN_CHARS,
    DETAIL_ONLY_PROMPT,
    PLAN_PROMPT,
    _detail_only_prompt,
    build_registry,
)
from app.contracts.source import Source, SourceKind
from app.generation import budget

TOPIC = "机器学习的主要方向"
BRANCHES = ("监督学习", "无监督学习", "模型与方法")

# 一份 400 字上下的材料：与 `` 那批同一形状（单份短摘要）。十节点树上
# 每节点支撑约 40 字 —— 低于 3 句的地板，正好验"地板压住支撑"那一格。
MATERIAL_TEXT = (
    "# 机器学习讲义\n\n"
    "## 监督学习\n\n"
    "监督学习用带标签的数据训练，分类与回归都属于它。常见模型有线性回归、决策树与"
    "神经网络；评价指标看准确率与召回率，训练集与测试集要分开，交叉验证用来估计"
    "泛化误差。正则化项抑制过拟合，学习率决定每一步走多远；类别不平衡时看 F1 "
    "而不是准确率，标注噪声会直接变成模型的上限。\n\n"
    "## 无监督学习\n\n"
    "无监督学习在无标注的数据上找结构，聚类、降维与异常检测是三类主要任务。"
    "K-means 与层次聚类按距离度量划分，PCA 把高维数据投到低维；聚类个数通常靠"
    "轮廓系数或肘部法则挑，降维之后仍要保留主要方差，异常检测把密度低处的点"
    "当作可疑样本。\n"
)


def _material() -> Source:
    return Source(id="s-ml", kind=SourceKind.document, status="parsed",
                  filename="讲义.md", text=MATERIAL_TEXT)


def _ctx(sources: list[Source] | None = None) -> RunContext:
    payload = sources or []
    return RunContext(
        request=AgentRunRequest(
            goal=TOPIC, intent="create", thinking_model="default", sources=payload,
            budget=RunBudget(max_model_calls=20, max_tool_calls=8, max_seconds=60.0,
                             max_output_tokens=8192, max_repairs=1)),
        sources_by_id={source.id: source for source in payload})


def _tree() -> dict:
    """三条一级分支、每支两个要点 + 根 = 10 个要写说明的节点。"""
    return {"title": TOPIC, "root": {
        "id": "root", "label": TOPIC, "detail": "", "children": [
            {"id": f"b{index}", "label": title, "detail": "", "children": [
                {"id": f"b{index}-1", "label": f"{title}·一", "detail": ""},
                {"id": f"b{index}-2", "label": f"{title}·二", "detail": ""},
            ]}
            for index, title in enumerate(BRANCHES)
        ]}}


def _run(ctx: RunContext, *, long_answer: str = "这是一段足够长的说明，用来占位。") -> tuple:
    """跑一次 `mindmap_generate`，返回 `(result, 第三段那次调用的 (system, user))`。"""
    seen: list[tuple[str, str]] = []
    tree = _tree()

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(BRANCHES)}
        if "只写思维导图节点的「说明」" in system:
            return {node_id: long_answer
                    for node_id in ("root", *(f"b{index}" for index in range(3)),
                                    *(f"b{index}-{point}" for index in range(3)
                                      for point in (1, 2)))}
        return tree

    registry = build_registry("create", has_sources=bool(ctx.request.sources),
                              has_library=False)
    with patch("app.agent.tools.chat_async", new=chat):
        result = asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate",
                     arguments={"topic": TOPIC, "branches": 3}), ctx))
    detail = [(system, user) for system, user in seen
              if "只写思维导图节点的「说明」" in system]
    return result, detail


# ---------------------------------------------------------------- 1. 算术


def test_the_switch_is_off_by_default_and_the_span_is_the_constant(monkeypatch):
    """默认关：区间就是 `DETAIL_MIN_CHARS`–`DETAIL_MAX_CHARS`，`scaled` 为假。

    这条是**整个改动的安全绳**：关着时 `detail_span` 必须返回常量，而
    `_detail_only_prompt(80, 150)` 必须与 `DETAIL_ONLY_PROMPT` 逐字节相同——
    前者错一位，第三段的提示词就变了，而现网每一张图都走这条路。
    """
    monkeypatch.delenv(budget.DETAIL_SCALE_ENV, raising=False)
    span = budget.detail_span(material_chars=5_616, nodes=48,
                              floor_chars=DETAIL_MIN_CHARS, ceiling_chars=DETAIL_MAX_CHARS)
    assert (span.min_chars, span.max_chars) == (DETAIL_MIN_CHARS, DETAIL_MAX_CHARS)
    assert span.scaled is False
    assert span.mean_chars == 130          # 现状实测均长，0.867 × 150
    assert _detail_only_prompt(DETAIL_MIN_CHARS, DETAIL_MAX_CHARS) == DETAIL_ONLY_PROMPT
    assert f"{DETAIL_MIN_CHARS}–{DETAIL_MAX_CHARS} 字" in DETAIL_ONLY_PROMPT


@pytest.mark.parametrize("value", ["1", "on", "TRUE", "yes"])
def test_the_switch_accepts_the_same_words_as_the_node_scale(monkeypatch, value):
    """开关的取值与 `MINDMAP_NODE_SCALE` 同一套词形：两个开关不该有两套语法。"""
    monkeypatch.setenv(budget.DETAIL_SCALE_ENV, value)
    assert budget.detail_scale_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "off", "no", "maybe"])
def test_the_switch_is_off_for_everything_else(monkeypatch, value):
    monkeypatch.setenv(budget.DETAIL_SCALE_ENV, value)
    assert budget.detail_scale_enabled() is False


def _implied_kappa(span: budget.DetailSpan) -> float:
    """按这个区间的目标均长反推的 κ（材料字数 / 预测说明总量）。

    用 `support / mean_chars` 而不是拿原始材料字数相除：`support` 是每节点支撑，
    `mean_chars` 是这一次要求写的均长，两者相比就是预测的压缩比。
    """
    return span.support / span.mean_chars if span.mean_chars else 0.0


@pytest.mark.parametrize(
    ("material", "nodes", "span", "kappa"),
    [
        # 中批 5,616 字 / 48 个节点：支撑 117 → 区间 75–135。预测 Σ = 48 × 117 =
        # 5,616 → κ 1.00。这就是"两个常量都要动"的那一格。
        (5_616, 48, (75, 135), 1.00),
        # 同一批材料铺满 80 个节点：支撑 70 → 目标均长 75（地板）→ 75–87。
        # 同样的材料铺更宽，每段就该更短——这正是"节点数 × 每段长度"总和守恒。
        (5_616, 80, (75, 87), 0.94),
        # 大批 66,536 字 / 80 个节点：支撑 832，远超可读性上限 → 夹回 80–150
        # （与现状逐字节相同）。κ 由材料决定，不由提示词决定：这一格该压缩。
        (66_536, 80, (80, 150), 6.40),
        # 小批 492 字 / 10 个节点（结构下限）：支撑 49 → 地板 75 → 75–87 → κ 0.66。
        # 想让它 κ ≥ 1 需要每段 49 字，而 49 字写不出 3 句——冲突不可调和，见文件尾。
        (492, 10, (75, 87), 0.66),
    ],
)
def test_the_span_follows_the_material(monkeypatch, material, nodes, span, kappa):
    """四个真实批次各算一遍：区间、目标均长、以及它预测出来的 κ。

    `kappa` 是**按目标均长算出来的**（材料 / (节点 × 均长)）：它是"这段算术希望发生
    什么"，实际值由 A/B 量（`data/detail-scale-ab.json`）。差得远说明模型没按区间写，
    而不是算术错了。
    """
    monkeypatch.setenv(budget.DETAIL_SCALE_ENV, "1")
    got = budget.detail_span(material_chars=material, nodes=nodes,
                             floor_chars=DETAIL_MIN_CHARS, ceiling_chars=DETAIL_MAX_CHARS)
    assert (got.min_chars, got.max_chars) == span
    assert got.scaled is True
    assert _implied_kappa(got) == pytest.approx(kappa, abs=0.01)


def test_the_floor_is_the_three_sentence_arithmetic(monkeypatch):
    """下限不许低于"3 句"：支撑再小，区间也停在 75 字。

    这是本次唯一**故意不随材料伸缩**的数。理由：80 本来就不是配额，而是
    `3 句 × 25 字 = 75` 的四舍五入（见 `budget.DETAIL_SENTENCE_FLOOR`）。把它压到
    40 字去迁就 492 字的材料，等于把"一段完整说明"改回"一句短语"——那正是第三段
    当初要消灭的形态（实测 21–46 字）。
    """
    monkeypatch.setenv(budget.DETAIL_SCALE_ENV, "1")
    span = budget.detail_span(material_chars=10, nodes=48,
                              floor_chars=DETAIL_MIN_CHARS, ceiling_chars=DETAIL_MAX_CHARS)
    assert span.min_chars == budget.DETAIL_SENTENCE_FLOOR == 75
    assert span.max_chars > span.min_chars          # 不退化成 [75,75]


def test_no_material_falls_back_to_the_constant(monkeypatch):
    """没有材料时是"模型写自己的知识"，不是"材料 0 字所以每段写 0 字"。"""
    monkeypatch.setenv(budget.DETAIL_SCALE_ENV, "1")
    span = budget.detail_span(material_chars=0, nodes=20,
                              floor_chars=DETAIL_MIN_CHARS, ceiling_chars=DETAIL_MAX_CHARS)
    assert (span.min_chars, span.max_chars) == (DETAIL_MIN_CHARS, DETAIL_MAX_CHARS)
    assert span.scaled is False


# ---------------------------------------------------------------- 2. 接线


def test_the_detail_call_gets_the_scaled_range(monkeypatch):
    """开关打开时：第三段的 system 与自查句都换成这次算出来的区间，`data` 里留账。"""
    monkeypatch.setenv(budget.DETAIL_SCALE_ENV, "1")
    ctx = _ctx([_material()])
    result, detail = _run(ctx)
    assert result.ok, result.content
    system, user = detail[0]
    # 材料的字数口径是 `intake.material_chars`：**去过首尾空白**的正文字数
    # （`sum(len(source.text.strip()))`），不是原文长度。
    material_chars = len(MATERIAL_TEXT.strip())
    span = budget.detail_span(material_chars=material_chars, nodes=10,
                              floor_chars=DETAIL_MIN_CHARS, ceiling_chars=DETAIL_MAX_CHARS)
    assert span.scaled is True
    assert f"{span.min_chars}–{span.max_chars} 字" in system
    assert f"{span.min_chars}–{span.max_chars} 字" in user      # 末尾那句自查
    # 出账：区间与它的两个因都在 `data["detail"]["chars"]` 里
    assert result.data["detail"]["chars"] == span.as_dict()
    assert "DETAIL_SCALE_APPLIED" in [item["code"] for item in result.data["diagnostics"]]


def test_the_switch_off_leaves_the_third_segment_byte_identical(monkeypatch):
    """开关关着时：提示词是常量、自查句是常量、`data["detail"]` 不多一个键。"""
    monkeypatch.delenv(budget.DETAIL_SCALE_ENV, raising=False)
    ctx = _ctx([_material()])
    result, detail = _run(ctx)
    assert result.ok, result.content
    system, user = detail[0]
    assert system.startswith(DETAIL_ONLY_PROMPT)
    assert f"{DETAIL_MIN_CHARS}–{DETAIL_MAX_CHARS} 字" in user
    assert "chars" not in result.data["detail"]
    assert "DETAIL_SCALE_APPLIED" not in [item["code"]
                                          for item in result.data["diagnostics"]]


def test_the_denominator_is_the_nodes_that_get_written(monkeypatch):
    """分母是**这一次真的要写说明的节点数**，不是配额。

    树只有 10 个节点、材料 5,000 字时，每段能分到 500 字支撑（→ 夹回 150）；按配额
    （80）算只会得到 62 字。两种算法差 2.4 倍，而"配额 80、树上只有 10 个节点"在
    真实产出里是常态（`node_expand` 长出来的小树、被裁过的树）。
    """
    monkeypatch.setenv(budget.DETAIL_SCALE_ENV, "1")
    by_targets = budget.detail_span(material_chars=5_000, nodes=10,
                                    floor_chars=DETAIL_MIN_CHARS,
                                    ceiling_chars=DETAIL_MAX_CHARS)
    by_quota = budget.detail_span(material_chars=5_000, nodes=80,
                                  floor_chars=DETAIL_MIN_CHARS, ceiling_chars=DETAIL_MAX_CHARS)
    assert (by_targets.min_chars, by_targets.max_chars) == (80, 150)
    assert (by_quota.min_chars, by_quota.max_chars) == (75, 87)


def test_the_span_reaches_the_prompt_through_fill_details(monkeypatch):
    """`_fill_details` 是唯一的入口：直接调它也能拿到伸缩后的区间。

    这条与上一条是两件事：上面验的是 `_generate` 把材料字数传对了，这里验的是
    `_fill_details` 自己会算、会出账、会把 `span` 一路传到 `_ask` 与 system。
    """
    from app.agent.tools import _fill_details
    from app.contracts.mindmap import MindMapContent, Node
    from app.contracts.diagnostic import Diagnostic

    monkeypatch.setenv(budget.DETAIL_SCALE_ENV, "1")
    content = MindMapContent(title=TOPIC, root=Node(
        id="root", label=TOPIC, detail="", children=[
            Node(id=f"n{index}", label=f"要点{index}", detail="")
            for index in range(5)]))
    seen: list[str] = []

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        seen.append(system)
        return {node_id: "说明" * 40 for node_id in
                ("root", *(f"n{index}" for index in range(5)))}

    ctx = _ctx()
    diagnostics: list[Diagnostic] = []
    with patch("app.agent.tools.chat_async", new=chat):
        refined, report = asyncio.run(_fill_details(
            content, ctx=ctx, topic=TOPIC, sections=[], external_rules="",
            diagnostics=diagnostics, material_chars=600))
    span = budget.detail_span(material_chars=600, nodes=6,
                              floor_chars=DETAIL_MIN_CHARS, ceiling_chars=DETAIL_MAX_CHARS)
    assert report["chars"] == span.as_dict()
    assert f"{span.min_chars}–{span.max_chars} 字" in seen[0]
    assert [item.code for item in diagnostics] == ["DETAIL_SCALE_APPLIED"]
    assert refined.root.detail


def test_the_switch_and_the_node_scale_are_independent(monkeypatch):
    """字数伸缩不依赖节点伸缩：`MINDMAP_NODE_SCALE` 关着也不影响这一段。

    两个开关各自回滚是这次的设计前提（`docs/GRAPH-SCALE.md` 的"两个常量各占一半"），
    一旦其中一个变成另一个的前置条件，A/B 就再也分不清是哪一处的功劳。
    """
    monkeypatch.setenv(budget.DETAIL_SCALE_ENV, "1")
    monkeypatch.delenv(budget.NODE_SCALE_ENV, raising=False)
    assert tools_module.budget.detail_scale_enabled() is True
    assert tools_module.budget.node_scale_enabled() is False
