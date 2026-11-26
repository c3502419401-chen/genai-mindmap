"""改图**该查时查不查**：提示词层那条「默认不要检索」的收窄，与它的兜底。

背景（`9c2ebcf`）：`edit` / `expand` 原先一个字都没提检索，查不查全凭模型临场判断。
实测同一份代码下「把标题改短一点」4.2 秒（没查）、「把这一支按材料补充一下」15.1 秒
（其中 7.3 秒是 `rag_query`）。于是两条意图各写了一句**默认不要检索**，同时留了
「按材料补充 / 核对原文」这个合法口子。

代价是那位 agent 自己记下的：模型可能在该查时也不查，表现为改图后引用为空。
`docs/SUBAGENT-VS-SELFCHECK.md` 之外的那条建议（第 2 件）就是给这种情况加兜底：
goal 里出现「材料 / 原文 / 引用 / 出处」时，把检索指令提到最前面。

**判据是"有没有发生"，不是"会不会发生"**（见 `docs/EDIT-NO-RETRIEVAL.md` 的实测）：
5 组"该查"的改图说法里，真的有 ≥2 组没查、改完引用为空，才加兜底；0/5 就不加，
把这条记进文档。本文件先钉住**现状**（关键词不改变指令），实测之后按结论翻。
"""
from __future__ import annotations

from app.agent.contracts import AgentRunRequest
from app.contracts.source import Source, SourceKind

#: 这 5 句是实测用的原话，一条都不能换成"语义相近"的说法：判据是"这句话说出口时
#: 用户期望它去翻材料"，而期望藏在措辞里（「按材料」与「依据材料」在模型那边的
#: 触发强度不一样，实测过才知道）。
MATERIAL_ASKS = (
    "把「单据齐全」这一支按材料补充一下",
    "这几个节点的引用要和原文对上",
    "把「审批时限」这段改成材料里的说法",
    "按原文核对一下这几条说得对不对",
    "把材料里的原文补进这几条",
)

CONTROL_ASKS = (
    "把标题改短一点",
    "把这两支换个颜色",
    "把「财务复核」改名成「财务审核」",
)


def _material() -> Source:
    return Source(id="s-rule", kind=SourceKind.document, status="parsed",
                  filename="报销制度.md",
                  text="# 报销制度\n\n单据齐全：发票、审批单、行程单三样缺一不可。"
                       "审批时限：提交后三个工作日内完成审批。\n")


def _rule(goal: str) -> str:
    from app.agent import service

    request = AgentRunRequest(goal=goal, intent="edit", sources=[_material()])
    return service._intent_rule(request)


def test_current_state_keywords_do_not_change_the_instruction():
    """现状：带材料时，无论 goal 说不说「材料 / 原文 / 引用 / 出处」，拿到的都是
    **同一句** `_RAG_INTENT_RULES["edit"]`。

    这就是那条建议想补的缝：指令里写着"只有用户明确说……才先调 rag_query"，
    但"用户说了"这件事由**模型自己**从 goal 里认，代码层没有任何标记。

    兜底一旦加上（实测 ≥2/5 才加），这条会翻成"关键词出现时指令被提到最前面"，
    届时本用例的名字与断言一起改——改的是判据，不是措辞。
    """
    baseline = _rule(CONTROL_ASKS[0])
    assert "不要检索" in baseline
    for goal in MATERIAL_ASKS:
        assert _rule(goal) == baseline, (
            f"「{goal}」拿到了不同的指令——说明已经有人加了兜底；"
            "那么这条用例该翻面，而不是留在这里")


def test_the_narrowing_keeps_the_legal_hole_open():
    """收窄不是禁止：那句指令必须同时说清"默认不查"与"这几句话时才查"。

    只写前半句的后果是可复现的——"把这一支按材料补充一下"这类真实用法被堵死，
    用户看到的是"它没去翻材料，凭标题编了一段"。
    """
    rule = _rule(MATERIAL_ASKS[0])
    assert "默认不要检索" in rule
    for hint in ("按材料", "原文"):
        assert hint in rule, f"合法口子里的「{hint}」不见了"
