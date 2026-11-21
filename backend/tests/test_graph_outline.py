"""第三档骨架：材料抽不出章节标题时，用图谱定骨架。

它补的是一个具体缺口：`heading_outline` 抽不出标题 → 覆盖率 0 → 过不了
`OUTLINE_MIN_COVERAGE` → 提示词里那句「目录为空或不完整时再自行归纳」生效，而模型
那时手上只有"主题 + 每份材料前 200 字"。**"自行归纳"没有任何结构信息可用**，产出的
是模板词（实测：主题「城市垃圾分类政策」连跑 5 次，一级分支一字不差）。

这个文件按风险从高到低盯四件事：

  1. **有章节的材料一个字符都不变**——那条路是现状，钉着逐字节用例。第三档只在
     `_outline_entries` 返回空时才被问到。
  2. **判据只看 scope**。`graph_store.subgraph` 在 scope 落空时会回退全图（那是两段式
     的取舍），拿它的返回条数当判据等于"库里只要有图，每一批材料就都算有图"——而
     "这批材料没入过图"恰恰是这一档要区分的事。所以判据走 `scoped_entity_count`。
  3. **阈值 `GRAPH_OUTLINE_MIN_ENTITIES`**：低于它的那批退回"模型自行归纳"，与加这一
     档之前逐字节相同。
  4. **没配图 / 熔断中一律不发起连接**：`configured()` 只看密码，不该为它付连接超时。

模型替身与 `test_graph_two_pass.py` / `test_single_call_generate.py` 同一套写法
（`patch("app.agent.tools.chat_async")`）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    DETAIL_ONLY_PROMPT,
    GRAPH_OUTLINE_ENV,
    GRAPH_OUTLINE_HEADER,
    GRAPH_OUTLINE_MIN_ENTITIES,
    GRAPH_PLAN_RULES,
    GRAPH_TREE_RULES,
    OUTLINE_HEADER,
    PLAN_PROMPT,
    _GRAPH_BRANCH_SOURCE,
    _graph_outline,
    build_registry,
    graph_outline_enabled,
)
from app.contracts.source import Source, SourceKind
from app.knowledge import graph_store

TOPIC = "心血管疾病风险预测"
BRANCHES = ("风险因素", "模型方法", "临床落地")

# 只有一段摘要、没有任何标题行：库里那 15 份外部检索正文（openalex / crossref /
# wikipedia）的形状。`heading_outline` 对它返回空列表——不是"标题不好"，是没有标题。
#
# 长度必须超过 `SHORT_MATERIAL_CHARS`（300 字）：低于它 `_intake` 会把材料当"用户附
# 的一段文字"塞进 `hint_block` 而不是 `sources`，第三档根本没机会被问到——那测的
# 就不是这一档了。
PLAIN_MATERIAL = (
    "心血管疾病风险预测模型以电子病历为输入，用机器学习方法估计个体在未来十年的"
    "发病风险。训练集来自三家医院的门诊记录，缺失值用同一列的中位数填补。"
    "模型在独立测试集上的曲线下面积是零点八六，交叉验证的结果与它相差不到零点零二。"
    "把模型接入门诊系统之后，医生在开检查单之前会先看到这个风险值，"
    "高风险的病人会被安排额外的血脂与心电图检查。模型的阈值按医院的实际床位"
    "与随访能力调整，阈值调低会提高灵敏度但增加复诊量。"
    "训练数据里的年龄与血压分布和上线后的门诊人群并不完全一致，"
    "所以每季度用新数据重估一次校准曲线，偏差超过五个百分点就重新拟合。"
    "吸烟史与家族史的缺失率最高，缺失本身也被当成一个特征喂进模型，"
    "因为「没问」与「没有」在临床上不是同一件事。"
)

# 有章节标题的材料：一级标题各占一半，覆盖率接近 100%，第一档直接接管。
# 同样必须超过 300 字（见上一条注释）。
CHAPTERED_MATERIAL = (
    "# 风险因素\n\n"
    "年龄、血压、血脂与吸烟史是最稳定的四个预测因子。家族史在早发病人群里权重更高，"
    "而单一的体重指数解释力有限。队列随访十年之后，血压的边际贡献开始下降，"
    "血脂与血糖的贡献上升。体力活动量靠问卷估计，问卷的回忆偏差会削弱它的系数，"
    "所以它在最终模型里只作为校正项出现。睡眠时长与打鼾频率通过可穿戴设备采集，"
    "采集依从性只有六成，缺测的那四成按「没有记录」单独成一类，不做过多的推测。\n\n"
    "# 模型方法\n\n"
    "逻辑回归是基线，梯度提升树在这批数据上略好。交叉验证按医院分组，"
    "避免同一个病人的多条记录跨在训练集与测试集两边。缺失值用多重插补而不是均值填补，"
    "插补次数取二十次，系数在二十份数据上取平均。校准曲线比区分度更早失效，"
    "所以上线之后先盯校准，再盯曲线下面积。超参数只用随机搜索，搜两百组，"
    "按分组交叉验证的平均对数损失挑。\n"
)

ENTITIES = [
    graph_store.GraphEntity(name="心血管疾病", type="概念", degree=9),
    graph_store.GraphEntity(name="电子病历", type="概念", degree=7),
    graph_store.GraphEntity(name="交叉验证", type="概念", degree=6),
    graph_store.GraphEntity(name="曲线下面积", type="概念", degree=5),
    graph_store.GraphEntity(name="梯度提升树", type="概念", degree=4),
    graph_store.GraphEntity(name="逻辑回归", type="概念", degree=4),
    graph_store.GraphEntity(name="血脂", type="概念", degree=3),
    graph_store.GraphEntity(name="随访能力", type="概念", degree=2),
]
LINKS = [
    graph_store.GraphLink(src="心血管疾病", dst="电子病历", relation="以…为输入",
                          source_id="s-1", start=0, end=60, weight=2),
    graph_store.GraphLink(src="交叉验证", dst="曲线下面积", relation="用来估计",
                          source_id="s-1", start=60, end=120, weight=1),
]


def _request(sources: list[Source] | None = None) -> AgentRunRequest:
    return AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default",
        sources=sources or [],
        budget=RunBudget(max_model_calls=20, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=1),
    )


def _ctx(sources: list[Source] | None = None) -> RunContext:
    payload = sources or []
    return RunContext(request=_request(payload),
                      sources_by_id={source.id: source for source in payload})


def _material(text: str, *, source_id: str = "s-plain") -> Source:
    return Source(id=source_id, kind=SourceKind.paper, status="parsed",
                  filename="crossref: 心血管疾病风险预测.md", text=text)


def _fake_graph(monkeypatch, *, count: int, entities=None, links=None,
                configured: bool = True, available: bool = True) -> dict:
    """把图存储换成"这批材料有多少个带边实体"的替身，返回它收到的调用账。

    `count` 与 `entities` 分开给是刻意的：真实世界里两者由两条不同的查询产出
    （`scoped_entity_count` 一条、`subgraph` 两条），把它们绑在一起就测不出
    "判据说够、取回来不够"那条分支。

    开关在这里一并打开：`conftest.SWITCHES` 把 `MINDMAP_GRAPH_OUTLINE` 置 0
    （理由见那里），所以每个要测这一档的用例都得自己开回来。
    """
    monkeypatch.setenv(GRAPH_OUTLINE_ENV, "1")
    calls: dict = {"count": 0, "subgraph": 0, "kwargs": None}
    monkeypatch.setattr(graph_store, "configured", lambda: configured)
    monkeypatch.setattr(graph_store, "available", lambda: available)

    def fake_count(scope=None):
        calls["count"] += 1
        calls["scope"] = list(scope or [])
        return count

    def fake_subgraph(**kwargs):
        calls["subgraph"] += 1
        calls["kwargs"] = kwargs
        return list(entities if entities is not None else []), list(links or [])

    monkeypatch.setattr(graph_store, "scoped_entity_count", fake_count)
    monkeypatch.setattr(graph_store, "subgraph", fake_subgraph)
    return calls


def _walk(node: dict):
    yield node
    for child in node.get("children") or []:
        if isinstance(child, dict):
            yield from _walk(child)


def _tree() -> dict:
    return {"title": TOPIC, "root": {
        "id": "root", "label": TOPIC, "detail": "", "children": [
            {"id": f"b{index}", "label": label, "children": [
                {"id": f"b{index}-1", "label": f"{label}·要点", "detail": "",
                 "children": []},
            ]}
            for index, label in enumerate(BRANCHES)
        ]}}


def _chat(seen: list[tuple[str, str]]):
    tree = _tree()

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(BRANCHES)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {node["id"]: "这句说明写到八十个字以上，讲清它是什么、怎么做、"
                                "有什么条件与数字，这样第三段的产出才符合长度目标。"
                    for node in _walk(tree["root"]) if node.get("id")}
        return tree
    return chat


def _run(ctx: RunContext, *, branches: int = 3):
    registry = build_registry("create", has_sources=bool(ctx.request.sources),
                              has_library=False)
    seen: list[tuple[str, str]] = []
    with patch("app.agent.tools.chat_async", new=_chat(seen)):
        result = asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate",
                     arguments={"topic": TOPIC, "branches": branches}), ctx))
    return result, seen


def _codes(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


# ---------------------------------------------------------------- 1. 开关


def test_third_tier_is_on_by_default(monkeypatch):
    """默认**开**：它补的是一个缺口，不是加一个新能力。

    与 `MINDMAP_EXTERNAL_KNOWLEDGE` 同一条口径：默认值写在一处、由一条用例直接问，
    不放进 `conftest.SWITCHES` 的断言里（那里只会把它置 0）。
    """
    monkeypatch.delenv(GRAPH_OUTLINE_ENV, raising=False)
    assert graph_outline_enabled() is True


@pytest.mark.parametrize("value", ["0", "off", "false", "no", " OFF "])
def test_switch_reads_falsy_values(monkeypatch, value):
    monkeypatch.setenv(GRAPH_OUTLINE_ENV, value)
    assert graph_outline_enabled() is False


def test_switch_off_returns_nothing(monkeypatch):
    """关掉之后连"图里有没有实体"都不问：A/B 的 A 组就是这一档。"""
    calls = _fake_graph(monkeypatch, count=99, entities=ENTITIES)
    monkeypatch.setenv(GRAPH_OUTLINE_ENV, "0")
    assert _graph_outline([_material(PLAIN_MATERIAL)]) == ("", 0)
    assert calls["count"] == 0


# ---------------------------------------------------------------- 2. `_graph_outline` 的四道闸


def test_no_material_returns_nothing(monkeypatch):
    calls = _fake_graph(monkeypatch, count=99, entities=ENTITIES)
    assert _graph_outline([]) == ("", 0)
    assert calls["count"] == 0


def test_unconfigured_graph_is_never_probed(monkeypatch):
    """没配密码 = 这台机器不用图。**一次连接都不该发**（否则每条请求多 3 秒）。"""
    calls = _fake_graph(monkeypatch, count=99, entities=ENTITIES, configured=False)

    def explode():
        raise AssertionError("没配图时不该建驱动")

    monkeypatch.setattr(graph_store, "_driver", explode)
    assert _graph_outline([_material(PLAIN_MATERIAL)]) == ("", 0)
    assert calls["count"] == 0


def test_open_breaker_blocks_the_probe(monkeypatch):
    """熔断中（刚连失败过）同样不连：`available()` 就是"现在值不值得发一条查询"。"""
    calls = _fake_graph(monkeypatch, count=99, entities=ENTITIES, available=False)
    assert _graph_outline([_material(PLAIN_MATERIAL)]) == ("", 0)
    assert calls["count"] == 0


def test_below_threshold_returns_nothing(monkeypatch):
    """7 个实体 < 阈值 8：这一批撑不起骨架，退回"模型自行归纳"。"""
    entities = ENTITIES[:GRAPH_OUTLINE_MIN_ENTITIES - 1]
    calls = _fake_graph(monkeypatch, count=len(entities), entities=entities)
    assert _graph_outline([_material(PLAIN_MATERIAL)]) == ("", len(entities))
    assert calls["subgraph"] == 0


def test_scope_without_edges_does_not_fall_back_to_the_whole_graph(monkeypatch):
    """**这条是这一档最容易错的地方**：`subgraph` 在 scope 落空时会回退全图。

    真图上的表现是：材料没入过图时 `subgraph` 照样返回 60 个实体（别的材料的）。
    判据若用它的返回条数，"图里有没有这批材料"就永远为真。所以判据走
    `scoped_entity_count`，返回 0 时**根本不调** `subgraph`。
    """
    calls = _fake_graph(monkeypatch, count=0, entities=ENTITIES, links=LINKS)
    assert _graph_outline([_material(PLAIN_MATERIAL)]) == ("", 0)
    assert calls["subgraph"] == 0


def test_enough_entities_render_the_block(monkeypatch):
    calls = _fake_graph(monkeypatch, count=len(ENTITIES), entities=ENTITIES, links=LINKS)
    block, count = _graph_outline([_material(PLAIN_MATERIAL)])

    assert count == len(ENTITIES)
    assert block.startswith(GRAPH_OUTLINE_HEADER)
    assert "心血管疾病" in block                     # 实体名
    assert "以…为输入" in block                       # 关系短语
    assert "连接数 9" in block                        # 度数（挑分支的唯一信号）
    # scope 必须是**本次材料**：图是跨材料共享的，不限定范围会把别的主题的实体带进来。
    assert calls["kwargs"]["scope"] == ["s-plain"]


def test_count_and_subgraph_disagreeing_means_no_outline(monkeypatch):
    """判据说够、取回来不够（两次查询之间图被改了）：按不够处理，不硬拼一个空块。"""
    _fake_graph(monkeypatch, count=9, entities=ENTITIES[:2])
    block, count = _graph_outline([_material(PLAIN_MATERIAL)])
    assert block == ""
    assert count == 2


# ---------------------------------------------------------------- 3. 接线：进 prompt


def test_generate_uses_the_graph_when_chapters_are_missing(monkeypatch):
    """无标题材料 + 图够用 → 骨架那一次的 system / user 都换成图谱那一套。"""
    monkeypatch.setenv(GRAPH_OUTLINE_ENV, "1")
    _fake_graph(monkeypatch, count=len(ENTITIES), entities=ENTITIES, links=LINKS)
    result, seen = _run(_ctx([_material(PLAIN_MATERIAL)]))

    assert result.ok, result.content
    system, user = seen[0]
    assert system.startswith(PLAN_PROMPT)
    assert GRAPH_PLAN_RULES in system
    assert GRAPH_OUTLINE_HEADER in user
    assert "心血管疾病" in user
    assert OUTLINE_HEADER not in user            # 这一批没有章节，不许假装有
    assert "GRAPH_OUTLINE_USED" in _codes(result)


def test_generate_keeps_the_chapter_path_untouched(monkeypatch):
    """有章节的材料：第三档**一次都不问**，prompt 与改动之前一样。

    这是这次改动的主要风险——那条路上有三条逐字节用例（`test_outline.py` /
    `test_single_call_generate.py`），判据写错会让它们在"多了一段图谱"上失败。
    """
    monkeypatch.setenv(GRAPH_OUTLINE_ENV, "1")
    calls = _fake_graph(monkeypatch, count=len(ENTITIES), entities=ENTITIES, links=LINKS)
    result, seen = _run(_ctx([_material(CHAPTERED_MATERIAL)]))

    assert result.ok, result.content
    system, user = seen[0]
    assert GRAPH_PLAN_RULES not in system
    assert OUTLINE_HEADER in user
    assert GRAPH_OUTLINE_HEADER not in user
    assert calls["count"] == 0
    assert calls["subgraph"] == 0
    assert "GRAPH_OUTLINE_USED" not in _codes(result)


def test_generate_falls_back_when_the_graph_is_too_small(monkeypatch):
    """图不够（或没入过图）：静默退回"模型自行归纳"，与加这一档之前一样。"""
    monkeypatch.setenv(GRAPH_OUTLINE_ENV, "1")
    _fake_graph(monkeypatch, count=3, entities=ENTITIES[:3])
    result, seen = _run(_ctx([_material(PLAIN_MATERIAL)]))

    assert result.ok, result.content
    system, user = seen[0]
    assert GRAPH_PLAN_RULES not in system
    assert GRAPH_OUTLINE_HEADER not in user
    assert "GRAPH_OUTLINE_USED" not in _codes(result)


# ---------------------------------------------------------------- 4. 规则本身


def test_rules_share_the_branch_source_line():
    """两条路（整树两段式 / 只定骨架）必须说**同一句**"分支怎么挑"。

    各写一遍时，同一个图谱在两条路上会给出不同的挑法，而 A/B 就分辨不出"图谱好不好"
    与"规则松紧"。
    """
    assert _GRAPH_BRANCH_SOURCE in GRAPH_TREE_RULES
    assert _GRAPH_BRANCH_SOURCE in GRAPH_PLAN_RULES


def test_plan_rules_only_ask_for_branches():
    """这一份只要几条分支名：要它写 children 会撞上 `_skeleton_plan` 的"分支太少"。"""
    assert "只定一级分支" in GRAPH_PLAN_RULES
    assert "不写子要点" in GRAPH_PLAN_RULES
    assert "不是要照抄的清单" in GRAPH_PLAN_RULES
