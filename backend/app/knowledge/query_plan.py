"""查询计划生成器：把一个问题变成一组检索意图（query 阶段的第一块）。

这一块**只生成计划**，不做检索、不做并发、不聚合、不排序。并行检索与融合
（RRF）是下一块的事：这里产出的 `QueryPlan` 就是它的输入。

## 为什么默认关闭

`MINDMAP_QUERY_EXPANSION` 默认 `0`，关掉时 `plan_query` 在第一行返回 `None`，
不发任何请求。默认关不是因为没做完，而是因为**在当前形态下它是负收益**，
仓库里已经实测过（`docs/RAG-DECISIONS.md` 2.3 / 第 4 节）：

  - 改写器不看材料时会**编造领域**：材料是预算审批，它把查询改成超导、离子阱、
    DOI/arXiv、《中央企业合规管理办法》。改写结果与材料的 2-gram 覆盖率**恒为
    0.0** —— 它不是"改写得更准"，是换了一个问题去搜。
  - 多轮判断会**发散**：完全无关的材料也跑满 20 轮（5483 token / 48.9 秒），
    判断器不是噪声源，是确定性地在说"不够"，而这正是循环继续的唯一燃料。
  - 那一轮 127 次 LLM 调用、75 次检索里，17 个轮次是纯浪费。

现在"材料整份进 prompt"，检索是**定位型**（材料里已有答案，检索帮模型找到那
一段），全塞入反而更好。要等"知识库"落地、检索变成**扩展型**（材料里没有的
东西靠检索补进来，对应申报书 3.3.1），多路召回才有正收益。到那时把开关打开。

所以本模块的每条硬约束都对应那个实验里的一个失败模式：
  - 必须喂材料（`materials`）→ 治"改写器不看材料就编领域"
  - 禁止意图膨胀 → 治"它多少钱"被扩成"价格/收费模式/优惠政策"，向量相似度被稀释
  - 子查询只在复杂问题生成 → 治"每次都拆"，把一次往返变成四次
  - 一次调用产出四样 → 2C2G + 5 秒预算下，四路往返不可能
  - 降级彻底（返回 `None`）→ 调用方拿到 `None` 必须与"没有这个功能"逐字节一致

## HyDE 的伪答案

`QueryPlan.hyde` 是模型编的一段"像答案"的文本，**可能整段是幻觉**。它只用来
算查询向量，绝不能出现在任何 sources、引用或最终正文里 —— 一旦被当成证据，
检索就变成了"自己检索自己"。

## 开关与失败

`MINDMAP_QUERY_EXPANSION` 关着 → 返回 `None`，零 HTTP 请求、零 JSON 解析。
开着但没有 `MINDMAP_LLM_*` 配置、调用失败、超时、解析不出来 → 同样返回
`None`。降级是这条路径的**常态**，不是异常：开开关但没配模型不该让每个 query
刷一行警告，也不该改变调用方的任何分支。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.generation.service import chat_async, estimate_input_tokens, parse_model_json
from app.knowledge.service import _bigram_terms

logger = logging.getLogger(__name__)

ENABLED_ENV = "MINDMAP_QUERY_EXPANSION"

# 这次调用的硬上限。检索阶段（下一块）的总预算在 5 秒量级，计划生成只能占其中
# 一部分；8 秒是"慢模型也来得及"与"再慢就没意义了"之间的界。 `chat_async` 自己
# 的 httpx 超时是 600 秒（它服务的是几十秒的整图生成），在这里不构成约束，
# 所以必须由 `asyncio.wait_for` 兜住 —— 否则一次卡死的调用会拖死整条检索。
TIMEOUT_SECONDS = 8.0

# 四个字段加起来是"四句问法 + 一段 150 字伪答案"，中文约 300 token。给 800 是
# 留足 JSON 结构与标点的余量；给大了只会让慢模型更慢，而预算只有 8 秒。
MAX_OUTPUT_TOKENS = 800

# 每条 intend/subquery 都会各自跑一遍检索（BM25 + 向量），多一条就多一份延迟。
# 上限写死在数据结构里，而不是靠提示词"建议 2-4 条"。
MAX_QUERIES = 4

# HyDE 超过这个长度就**截断**（见 `QueryPlan` docstring）。
HYDE_MAX_CHARS = 150

# 每份材料只进提示词前 200 字：材料全塞进去会把这次调用拖到十秒级，而计划生成
# 需要的只是"术语怎么写、范围到哪"，不是全文。
MATERIAL_HEAD_CHARS = 200

# 材料份数上限。60 份材料的开头也能凑出上万字，而计划生成不需要看全 —— 它要
# 判断的是"这个问题问的是哪种东西"，十份足够定位领域。
MAX_MATERIALS = 10
# `corrected` 与原始 query 的 bigram 重合下限。纠错是小改动（错别字、术语写法），
# 低于这条线说明模型改的是"问题本身"而不是"问题的写法"——那已经不是纠错。
# 定得松（允许七成变化）是有意的：宁可放过一次糟糕但同题的改写，也不要把
# 正常的术语统一判成换题。
MIN_CORRECTED_OVERLAP = 0.30
# 与原始 query 重合高于这条线时，视为"只改了用词细节"，不再核材料。
# 纠错本来就会改字面（"批钱"→"批这笔钱"），而材料里未必出现改后的写法；
# 拿材料去核一个只动了几个字的纠错，只会把正确的纠错误删。
TRUSTED_OVERLAP = 0.60
# 材料少于这么多 bigram 时，"与材料无交集"不能当作"没看材料"的证据。
# 短材料（一个标题、一句摘要）本来就覆盖不了问题的用词，拿它当尺子会把正确的
# 纠错误删。真实材料每份给 200 字开头，bigram 数在 200 上下，远高于这条线。
MIN_MATERIAL_BIGRAMS = 20

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}


def _enabled() -> bool:
    """开关，默认关。没设、设成空、设成 0 都是关。"""
    return str(os.getenv(ENABLED_ENV, "")).strip().lower() in _TRUTHY


class QueryPlan(BaseModel):
    """一次查询生成的计划。四个部分一起产出、一起失效。

    `hyde` 是**假设性答案**，可能整段是模型编的。它只用于算向量相似度，永远
    不进 sources、引用或正文。

    `hyde` 超过 `HYDE_MAX_CHARS` 时**截断而不是丢弃**：它只是给向量用的"形状
    模板"，一句话被切短仍然是个形状；丢掉则等于这次 token 白花。这也是本文件
    唯一一处"宁截不弃"的地方 —— 其余字段类型不对就整项丢（见 `plan_query`）。

    `source` 说的是这份计划**能不能拿去扩展检索**，不是"有没有调用模型"：

      - `"llm"`：有材料上下文，模型真的产出了扩展项（intents/hyde/subqueries
        至少一项非空）。
      - `"fallback"`：只有纠错，或者什么都没拿到 —— 调用方只该用 corrected。

    所以"无材料 → 只纠错"的计划，即便 corrected 出自模型，也算 `"fallback"`：
    它没有资格触发扩展检索。默认值就是 `"fallback"`，因为**来源只能由知道
    来源的那一层写**，一份手造的计划不该自称来自模型。
    """

    model_config = ConfigDict(extra="forbid")

    corrected: str | None = None
    intents: list[str] = []
    hyde: str | None = None
    subqueries: list[str] = []
    source: Literal["llm", "fallback"] = "fallback"

    @field_validator("corrected", "hyde")
    @classmethod
    def _strip_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("hyde")
    @classmethod
    def _cap_hyde(cls, value: str | None) -> str | None:
        return None if value is None else value[:HYDE_MAX_CHARS]

    @field_validator("intents", "subqueries")
    @classmethod
    def _tidy_queries(cls, value: list[str]) -> list[str]:
        """去空白、去重、超上限截断。

        去重不是洁癖：同一个问题跑两遍检索只会拿到同一批片段，白花一次向量
        调用，还会在融合时把名次重复计一遍（`_pick_top` 按内容去重能兜住，
        但那是下一块的兜底，不该由它来擦这里的手尾）。
        """
        kept: list[str] = []
        for item in value:
            text = item.strip()
            if text and text not in kept:
                kept.append(text)
        return kept[:MAX_QUERIES]


# 提示词。三条最要紧的约束（必须看材料 / 禁止意图膨胀 / 子查询只在复杂问题时给）
# 都用正反例写成"可对照的形状"，不用形容词：示例里只出现「甲」「乙」「它」这类
# 占位词，避免模型把示例内容抄进结果 —— 那次实测里改写器编出的超导/离子阱，
# 就是"照着别处的词往下写"的产物。
SYSTEM_PROMPT = (
    "你在为一次资料检索做准备：把用户的问题整理成一组检索意图。"
    "你只输出一个 JSON 对象，不要写解释、不要加代码围栏、不要在 JSON 前后写字。\n"
    "\n"
    "字段（四个都要出现；某一项确实没有就给 null 或 []）：\n"
    "  corrected   string|null  纠错后的原问题：改错别字、统一术语、把指代不明的「它」补全，"
    "不改问题的意思。\n"
    "  intents     string[]     同一问题的不同问法，2-4 条。\n"
    "  hyde        string|null  假设性答案：一段 150 字以内、读起来像答案的短文本。\n"
    "  subqueries  string[]     复杂多步问题的子查询，2-4 条；简单问题给 []。\n"
    "\n"
    "五条硬规则，逐条守：\n"
    "\n"
    "1. 必须看材料。用词、术语写法、范围一律以材料为准；材料里没有出现过的领域、"
    "指标、机构、标准、文号、篇名，一个都不要写进来。\n"
    "   ❌ 材料讲的是甲领域的甲事物，问题里说「它」，你就把「它」换成乙领域的一个概念"
    "——乙在材料里根本没出现过，检索从此换了个问题。\n"
    "   ✅ 材料里把这个东西写作「甲（正式名）」，你就按材料的正式写法纠错，其余不动。\n"
    "\n"
    "2. 禁止意图膨胀。intents 必须是同一个问题的不同问法，不得增加原问题没有的方面。"
    "确实要新增方面，就写进 subqueries（而且只有这个问题本来就复杂时才写），不要塞进 intents。\n"
    "   ❌ 原问题「它多少钱」→ 写成「它多少钱」「收费模式是什么」「有哪些优惠政策」"
    "—— 后两条各自新增了一个方面，向量相似度会被这几个方面摊薄。\n"
    "   ✅ 原问题「它多少钱」→ 写成「它多少钱」「它要花多少钱」「它的费用是多少」"
    "—— 三个问法问的是同一件事。\n"
    "\n"
    "3. hyde 只用来算向量相似度，永远不会被当作答案或证据引用；它可以是错的。"
    "所以它要的是「形状」：一两句话、用回答的口吻直接说，150 字以内，超了会被截断。\n"
    "   ❌ 写成一整篇带小标题、分点、结论段的答复。\n"
    "   ✅ 一两句话，像有人已经答完这个问题时说的那两句话。\n"
    "\n"
    "4. subqueries 只给复杂问题。判据：需要比较两个以上对象、需要多跳（先查 A 再用 A 查 B）、"
    "或者一个问题里带着两个以上实体，才算复杂。简单事实问题把 subqueries 给 []，"
    "不要为了显得周全每次都拆。\n"
    "   ❌ 原问题「甲是什么」也拆成三条子查询。\n"
    "   ✅ 原问题「甲的 A 指标和乙的 B 指标谁更高」拆成分别查甲、查乙两条。\n"
    "\n"
    "5. 不确定就给 null 或 []，不要编。类型必须对：corrected 与 hyde 是字符串或 null，"
    "intents 与 subqueries 是字符串数组；数字、布尔、对象、嵌套数组不要塞进来，"
    "类型不对的那一项会被整项丢掉。\n"
    "\n"
    "上面的例子只用来说明形状，不要把例子里的词写进你的结果。"
)

# 没有材料时替换掉整段扩展要求。规则 2 在代码里也硬拦一遍（见 `_build_plan`），
# 这里写清楚是为了让模型别在不该动的地方动 —— 两道闸都要有：提示词是省一次
# 解析，代码是模型不听话时仍然成立。
NO_MATERIAL_NOTICE = (
    "本次没有提供材料。因此不做任何扩展：intents 与 subqueries 给 []，hyde 给 null，"
    "只填 corrected（问题本身没有可纠的错时给 null）。不要凭猜测补材料里没有的东西。"
)


def _material_snippets(materials: list[str] | None) -> list[str]:
    """每份材料取"标题 + 开头"，压平换行，最多 `MAX_MATERIALS` 份。

    调用方按「标题 + 正文开头」拼一个字符串传进来即可；超过
    `MATERIAL_HEAD_CHARS` 的部分在这里被截掉，传全文也不会撑爆提示词。
    换行压平是为了让提示词的分段只由本文件控制 —— 材料里的 markdown 结构
    混进来，模型会以为那也是指令。
    """
    snippets: list[str] = []
    for item in materials or []:
        if not isinstance(item, str):
            continue
        flat = " ".join(item.split())
        if flat:
            snippets.append(flat[:MATERIAL_HEAD_CHARS])
        if len(snippets) >= MAX_MATERIALS:
            break
    return snippets


def _user_message(query: str, snippets: list[str]) -> str:
    """拼这一次的用户消息。有材料与没材料是两段不同的指令，不是同一段加个空表。"""
    if not snippets:
        return f"{NO_MATERIAL_NOTICE}\n\n问题：{query}\n\n只返回那个 JSON 对象。\n"
    block = "\n".join(f"[材料{i}] {text}" for i, text in enumerate(snippets, 1))
    return (
        "材料（每份只给标题与开头，是本次判断用词与范围的唯一依据）：\n"
        f"{block}\n"
        "\n"
        f"问题：{query}\n"
        "\n"
        "先照材料核对问题里的说法，再决定 corrected / intents / hyde / subqueries。"
        "只返回那个 JSON 对象。\n"
    )


def _payload(raw: Any) -> dict[str, Any] | None:
    """把模型出口的返回值收敛成一个 dict；收敛不了就 None。

    `chat_async` 内部已经过了一遍 `parse_model_json`，所以正常情况下这里拿到
    的就是 dict。仍然再解析一次字符串，是因为真有一种响应会以字符串形态出现：
    模型把 JSON 当字符串又包了一层（`"{\\"intents\\": [...]}"`），一次
    `json.loads` 只会把它解成 str。用**同一个**解析器收尾，而不是在这里另写
    一套剥围栏的逻辑 —— 两套规则的结局是同一个响应在两处有两种判定。
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = parse_model_json(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _clean_text(value: Any) -> str | None:
    """字符串字段：不是字符串就丢，不是"尽力转换"。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _clean_queries(value: Any) -> list[str]:
    """数组字段：类型不对整项丢。

    不做"把字符串拆成一条数组"这种猜测：猜对只是省一条，猜错会悄悄多出一条
    与原问题无关的检索，而且没有人会去核对它 —— 这种噪声比少一条难查得多。
    非字符串的元素逐条丢弃，坏一个元素不该带走整个数组。
    """
    if not isinstance(value, list):
        return []
    kept: list[str] = []
    for item in value:
        text = _clean_text(item)
        if text:
            kept.append(text)
    return kept


def _build_plan(payload: dict[str, Any], *, has_materials: bool) -> QueryPlan | None:
    """从 payload 逐字段重建计划：坏哪项丢哪项，全坏返回 None。

    无材料时 `intents`/`hyde`/`subqueries` 一律清空。这是代码闸，不是提示词
    的重复：实测里改写器最容易犯的错就是不看的材料也敢扩，而一处没材料的扩展
    正是那次 2-gram 覆盖率 0.0 的起点。
    """
    corrected = _clean_text(payload.get("corrected"))
    intents = _clean_queries(payload.get("intents"))
    hyde = _clean_text(payload.get("hyde"))
    subqueries = _clean_queries(payload.get("subqueries"))
    if not has_materials:
        intents, hyde, subqueries = [], None, []
    if not (corrected or intents or hyde or subqueries):
        return None
    # 只有真的产出了扩展项才算 "llm"：一份"只有纠错"的计划没有资格触发扩展检索。
    source: Literal["llm", "fallback"] = (
        "llm" if (intents or hyde or subqueries) else "fallback"
    )
    return QueryPlan(corrected=corrected, intents=intents, hyde=hyde,
                     subqueries=subqueries, source=source)


def _log(outcome: str, started: float, *, prompt_tokens: int,
         response_chars: int, plan: QueryPlan | None) -> None:
    """一行日志：**只有形状，没有内容**。

    query 原文与材料正文一律不记：材料可能是合同、病历、内部制度，日志会被
    收走、会被翻。这里记的是耗时、估算 token、各字段的条数与字数 —— 排查
    "为什么没开/为什么降级"够用，泄漏一个字都不用。

    token 数用 `estimate_input_tokens` 的估算值：这条出口（`chat_async`）只
    返回解析结果、不带 usage 字段，因此标注成估算，不假装是账单。
    """
    logger.info(
        "查询计划 outcome=%s 耗时=%.0fms prompt≈%d token 响应 %d 字 "
        "纠错=%s intents=%d subqueries=%d hyde=%d 字 source=%s",
        outcome, (time.perf_counter() - started) * 1000, prompt_tokens, response_chars,
        "有" if (plan and plan.corrected) else "无",
        len(plan.intents) if plan else 0,
        len(plan.subqueries) if plan else 0,
        len(plan.hyde) if plan and plan.hyde else 0,
        plan.source if plan else "-",
    )


def _guard_corrected(
    corrected: str | None, query: str, snippets: list[str],
) -> str | None:
    """`corrected` 的两道闸：换了题就丢、没看材料也丢。

    它是四个字段里唯一会**替换用户原话**的一个，而它的失败是**哑的**：模型把
    问题"纠"成一个材料里确实出现的术语，同时悄悄收窄或改向——此时召回相似度高、
    命中非空、`source="llm"`，没有任何一个信号会报警，下游拿它当原问题用，用户
    的原话就消失了。`intents`/`subqueries` 跑偏还有原 query 兜底，它没有。

    两道闸各挡一种失效，都用 bigram 重合度衡量，与当初证伪那次漂移用的是同一把尺子：

    - 与**原始 query** 的重合率低于 `MIN_CORRECTED_OVERLAP` → 换了题。
      纠错只该改错别字与术语写法，不该改动问题本身。
    - 与**材料正文**的 bigram 交集为空 → 没看材料。这正是那次
      "2-gram 覆盖率恒 0.0" 的签名。

    两条都不成立时原样返回；判不了（短到没有 bigram）时也原样返回——宁可放过，
    也不要因为度量不适用而误删一个正确的纠错。
    """
    if not corrected:
        return None
    # `_bigram_terms` 给的是 list（保留重复与顺序，服务 BM25 的词频），
    # 这里要的是重合度，转成集合；重复项对"两个文本共享多少 bigram"没有意义。
    original = set(_bigram_terms(query))
    candidate = set(_bigram_terms(corrected))
    if not original or not candidate:
        return corrected
    overlap = len(original & candidate) / len(original)
    if overlap < MIN_CORRECTED_OVERLAP:
        return None
    # 只改了用词细节 → 信它，不必再核材料（见 TRUSTED_OVERLAP 的注释）。
    if overlap >= TRUSTED_OVERLAP:
        return corrected
    if snippets:
        material: set[str] = set()
        for text in snippets:
            material |= set(_bigram_terms(text))
        if len(material) >= MIN_MATERIAL_BIGRAMS and not (candidate & material):
            return None
    return corrected


def _guard_plan(plan: QueryPlan, query: str, snippets: list[str]) -> QueryPlan:
    """只动 `corrected`，其余字段原样保留——扩展项由提示词与"无材料清空"那道闸管。"""
    guarded = _guard_corrected(plan.corrected, query, snippets)
    if guarded == plan.corrected:
        return plan
    return plan.model_copy(update={"corrected": guarded})


async def plan_query(
    query: str, *, materials: list[str] | None = None,
) -> QueryPlan | None:
    """把一个问题变成一份检索计划；任何一步不成立都返回 `None`。

    `None` 的语义是**逐字节等于没有这个功能**：调用方拿到它就用原始 query，
    不做补偿、不发第二次请求、不改变分支。所以这里不抛异常、不重试、不缓存
    —— 一个"有时多一次往返"的检索阶段，比没有它更难排查，也会让 5 秒预算
    变成"最坏情况 13 秒"。

    参数：
      - `query`：原始问题。空白问题直接返回 None，不花一次调用。
      - `materials`：每份材料"标题 + 正文开头"的字符串，最多取 `MAX_MATERIALS`
        份、每份前 `MATERIAL_HEAD_CHARS` 字。**没有材料就只做纠错**，不扩展。

    开关 `MINDMAP_QUERY_EXPANSION` 关着时在第一行返回：不读 materials、不构造
    客户端、不发请求。这条路径的代价必须是零。
    """
    if not _enabled():
        return None
    text = (query or "").strip()
    if not text:
        return None
    snippets = _material_snippets(materials)
    prompt = _user_message(text, snippets)
    prompt_tokens = estimate_input_tokens(SYSTEM_PROMPT) + estimate_input_tokens(prompt)
    started = time.perf_counter()
    outcome = "ok"
    raw: Any = None
    try:
        # 只发一次，绝不重试：失败就是这次没有计划，重试只会把 5 秒预算打穿。
        raw = await asyncio.wait_for(
            chat_async(SYSTEM_PROMPT, prompt, max_tokens=MAX_OUTPUT_TOKENS),
            timeout=TIMEOUT_SECONDS,
        )
    except (TimeoutError, asyncio.TimeoutError):
        outcome = "timeout"
    # 模型这层怎么坏（没配置/HTTP 错误/非 JSON）都只是"这次没有计划"。注意这里
    # 不拦 `CancelledError`：那是我方被取消（客户端断开），不是模型坏了，吞掉它
    # 会让上层以为任务正常跑完了。
    except Exception:  # noqa: BLE001
        outcome = "error"
    if outcome != "ok":
        _log(outcome, started, prompt_tokens=prompt_tokens, response_chars=0, plan=None)
        return None

    payload = _payload(raw)
    if payload is None:
        _log("bad_json", started, prompt_tokens=prompt_tokens,
             response_chars=len(raw) if isinstance(raw, str) else 0, plan=None)
        return None

    plan = _build_plan(payload, has_materials=bool(snippets))
    if plan is None:
        _log("empty", started, prompt_tokens=prompt_tokens,
             response_chars=len(json.dumps(payload, ensure_ascii=False, default=str)),
             plan=None)
        return None
    plan = _guard_plan(plan, text, snippets)
    _log("ok", started, prompt_tokens=prompt_tokens,
         response_chars=len(json.dumps(payload, ensure_ascii=False, default=str)),
         plan=plan)
    return plan
