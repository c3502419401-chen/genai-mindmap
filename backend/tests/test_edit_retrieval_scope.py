"""改图**该查时查不查**：提示词层那条「默认不要检索」的收窄，与它的兜底裁定。

背景（`9c2ebcf`）：`edit` / `expand` 原先一个字都没提检索，查不查全凭模型临场判断。
实测同一份代码下「把标题改短一点」4.2 秒（没查）、「把这一支按材料补充一下」15.1 秒
（其中 7.3 秒是 `rag_query`）。于是两条意图各写了一句**默认不要检索**，同时留了
「按材料补充 / 核对原文」这个合法口子。

那位 agent 自己记下的代价是：模型可能在该查时也不查，表现为改图后引用为空。
建议的兜底是"goal 里出现「材料 / 原文 / 引用 / 出处」时，把检索指令提到最前面"。

**判据是"有没有发生"，不是"会不会发生"** —— 所以先量，实测结果写在
`docs/EDIT-NO-RETRIEVAL.md`：

    5 组"该查"的说法，**0 组没查**（每组 1–2 次 rag_query）
    3 组"不该查"的说法，**0 组查了**
    → 兜底是给不存在的问题加代码，**不做**。

并且"引用为空"这条判据本身不成立：模型能走的那条编辑层**根本没有
`source_refs` 字段**（`contracts/ops.py::UpdateChanges` 的注释：引用在模型这条路
上是关着的，开一个入口等于多一条能凭空造引用的路）。所以改图之后引用为空是
**结构上必然**的，与查没查无关——那条症状描述的其实不是检索缺失。
"""
from __future__ import annotations

from app.agent.contracts import AgentRunRequest
from app.contracts.source import Source, SourceKind

#: 这 5 句是实测用的原话，一条都不能换成"语义相近"的说法：判据是"这句话说出口时
#: 用户期望它去翻材料"，而期望藏在措辞里（「按材料」与「依据材料」在模型那边的
#: 触发强度不一样，实测过才知道）。节点名取自真图 `c1d30b17`（注意力机制），
#: 材料是那篇论文的正文（42,526 字）。
MATERIAL_ASKS = (
    "把「自注意力与跨序列对齐」这一支按材料补充一下",
    "「相似度打分与权重分配」下面这几条的引用要和原文对上",
    "把「多头并行捕捉多类关系」这段改成材料里的说法",
    "按原文核对一下「查询、键与值的三元结构」这几条说得对不对",
    "把材料里的原文补进「加权求和生成上下文向量」这几条",
)

CONTROL_ASKS = (
    "把「注意力机制的核心思想」这个标题改短一点",
    "把「多头并行捕捉多类关系」改名成「多头并行」",
    "把标题改短一点",
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


def test_keywords_do_not_change_the_instruction_and_that_is_the_verdict():
    """带材料时，无论 goal 说不说「材料 / 原文 / 引用 / 出处」，拿到的都是**同一句**
    `_RAG_INTENT_RULES["edit"]`。

    这一条同时钉着两件事：

      - 代码层**没有**关键词兜底（`docs/EDIT-NO-RETRIEVAL.md` 的裁定：实测 0/5
        没查，兜底是给不存在的问题加代码）；
      - 如果将来有人加了兜底，这条会红——那时该做的是把文档里那段实测重跑一遍、
        连同本用例一起翻面，而不是悄悄改断言。
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
    用户看到的是"它没去翻材料，凭标题编了一段"。实测这 5 句里 5 句都真的查了，
    所以这个口子今天确实是通的。
    """
    rule = _rule(MATERIAL_ASKS[0])
    assert "默认不要检索" in rule
    for hint in ("按材料", "原文"):
        assert hint in rule, f"合法口子里的「{hint}」不见了"


def test_the_edit_layer_cannot_write_refs_so_empty_refs_is_not_evidence():
    """"改图后引用为空"不是"没查"的证据——**模型能走的那条编辑层写不了引用**。

    这是实测之后才看清的一条：契约里 `UpdateChanges.source_refs` 存在，但它是给
    原生编辑（导入 / 界面）用的；模型看到的是 `agent.tools.EditChange`，那里没有
    这个字段（`contracts/ops.py` 的注释写着为什么：编辑路径再开一个入口，等于多
    一条能凭空造引用的路）。所以 `edit` 之后节点引用恒为空，查过多少次都一样。

    留着这条用例是因为它把"症状"和"原因"钉开了：下一次谁再说"改完引用为空，
    说明没检索"，这条会先拦住他。
    """
    from app.agent import tools
    from app.contracts.ops import UpdateChanges

    assert "source_refs" in UpdateChanges.model_fields     # 契约里有（原生编辑用）
    edit_fields = getattr(tools.EditChange, "model_fields", {})
    assert "source_refs" not in edit_fields, (
        "模型那条编辑层能写引用了——那么「改完引用为空」重新成为可疑信号，"
        "`docs/EDIT-NO-RETRIEVAL.md` 的结论要重跑")
