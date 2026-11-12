"""实体关系抽取：一次调用拿到实体与关系；抽不到就当没有这个功能。

**为什么一次调用产出两样**：分两次（先抽实体、再抽关系）会让关系里的 `src`/`dst`
与实体那一轮的结果对不上——模型两次看到的是同一段文本，但它每次给的名字都可能
差一个字，校验就会把大半关系丢掉。一次产出、同一份上下文，`src`/`dst` 至少有
机会与 `entities` 逐字一致。

**降级哲学与 `query_plan.plan_query` 完全一致**：任何一步不成立都返回 `None`，
调用方拿到 `None` 就当作"没有图这个功能"——不补偿、不重试、不缓存。所以开关
关着、没配模型、超时、返回非法 JSON、抽出来的东西全被校验丢掉，出口只有一个：
`None`。一个"有时多花 15 秒"的入库阶段，比没有这个阶段难排查得多。

**默认关闭**（`MINDMAP_GRAPH_EXTRACTION=0`）。抽取是**每条材料 20 次 LLM 调用**
的成本（`graph_ingest` 的成本闸），而在检索融合落地之前，这些边没有任何消费者。
默认开等于让每个上传材料的人都替下一轮的实验付费。

**日志只有形状**：条数、耗时、输入字数、材料指纹。实体名与原文一个字都不记——
材料可能是合同、病历、内部制度，日志会被收走、会被翻。

## 三条硬校验（代码闸，不只写在提示词里）

  1. **关系必须落在实体表里**：`src`/`dst` 归一化后不在本次 `entities` 里就丢弃。
     模型很爱写"他""该组织""前者"——那些不是实体，落库会造出一个每份材料都
     复用的幽灵节点，把所有不相干的材料连成一团。
  2. **名字长度 2~30 字**：更短的不成词（"的""它"），更长的通常是整句（模型把
     一句话当成了实体名）。30 字这个上限在中文里已经能容纳"国家市场监督管理
     总局"这类长机构名。
  3. **关系短语 2~20 字**：单字关系（"是""有"）不携带信息，长于 20 字的"关系"
     其实是模型在复述句子。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from app.generation.service import chat_async, parse_model_json
from app.knowledge.graph_store import normalize_name

logger = logging.getLogger(__name__)

ENABLED_ENV = "MINDMAP_GRAPH_EXTRACTION"

# 单次抽取的硬上限。`chat_async` 自己的 httpx 超时是 600 秒（它服务的是几十秒的
# 整图生成），在这里不构成任何约束，所以必须由 `asyncio.wait_for` 兜住：一次
# 卡死的调用会让整批入库挂在那里，而入库是用户点了"上传"之后在等的事情。
TIMEOUT_SECONDS = 15.0

# 输入上限。切块上限是 700 字（`service.CHUNK_MAX_CHARS`），正常路径永远碰不到
# 这条线；它是给"有人直接拿一整篇材料来调"准备的——1500 字之后的关系密度急剧
# 下降，多喂的部分基本只是在烧 token。
MAX_INPUT_CHARS = 1500

# 输出上限。一块 700 字的材料抽出的实体通常在 5-15 个、关系 3-10 条，中文字符
# 加上 JSON 结构约 600 token。给 1200 是留足标点与重复项被截断前的余量。
MAX_OUTPUT_TOKENS = 1200

# 代码侧的条数闸。模型偶尔会把同一段话拆成几十个碎片实体，截断比全落库好：
# 越靠后的条目通常越边缘。
MAX_ENTITIES = 40
MAX_RELATIONS = 60

MIN_NAME_CHARS = 2
MAX_NAME_CHARS = 30
MIN_RELATION_CHARS = 2
MAX_RELATION_CHARS = 20

# 实体类型白名单。白名单之外的类型折成 `None`（"类型不明"），不做同义词猜测：
# 把"机构"映射成"组织"看起来无害，但它会让同一实体在不同块里落成两行——一处
# 判对了、一处猜错了，图里就多一个只连了一半的节点。折成 `None` 至少是诚实的。
ENTITY_TYPES = ("人物", "组织", "概念", "地点", "事件", "其他")

# 无信息的关系短语。提示词里已经明令禁止，这里**再拦一遍**：模型对"不要写相关"
# 这类否定指令的遵守率并不高，而一条 2 字的"相关"恰好通过了长度闸（2~20 字），
# 落库后它等价于"这两个名字连着"，会把任意两个在同一段里出现过的实体连起来，
# 图的精度当场塌掉。与 `query_plan` 的处理一样：提示词省一次解析，代码兜住
# 模型不听话时的那一次。
EMPTY_RELATIONS = frozenset({"相关", "有关", "涉及", "关联", "关系"})

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}


def _enabled() -> bool:
    """开关，默认关。没设、设成空、设成 0 都是关。"""
    return str(os.getenv(ENABLED_ENV, "")).strip().lower() in _TRUTHY


@dataclass(frozen=True)
class ExtractedEntity:
    """一个抽出来的实体。`name` 已归一化（`graph_store.normalize_name`）。"""

    name: str
    type: str | None = None


@dataclass(frozen=True)
class ExtractedRelation:
    """一条抽出来的关系。`src`/`dst` 已归一化，且保证出现在同一结果的实体里。"""

    src: str
    dst: str
    relation: str


@dataclass(frozen=True)
class GraphExtraction:
    """一段文本的抽取结果。两个列表都可能为空，但不会同时为空（那样返回 `None`）。"""

    entities: list[ExtractedEntity] = field(default_factory=list)
    relations: list[ExtractedRelation] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [entity.name for entity in self.entities]


# 提示词。每条规则都对着一种实测过的模型习惯：抽代词、抽"方法/内容"这类泛化词、
# 把"相关"当关系、给一个短句硬凑两条边。示例只用「甲」「乙」这类占位词，避免
# 模型把示例里的内容抄进结果——`query_plan` 那次实验里改写器编出的领域词，就是
# "照着示例往下写"的产物。
SYSTEM_PROMPT = (
    "你在从一段材料里抽取实体与实体之间的关系，供一个知识图谱使用。"
    "你只输出一个 JSON 对象，不要写解释、不要加代码围栏、不要在 JSON 前后写字。\n"
    "\n"
    "输出结构（两个字段都要出现；某一项没有就给 []）：\n"
    '{"entities": [{"name": "实体名", "type": "人物|组织|概念|地点|事件|其他"}],\n'
    ' "relations": [{"src": "实体名甲", "dst": "实体名乙", "relation": "关系短语"}]}\n'
    "\n"
    "实体规则：\n"
    "1. 只抽名词性的东西：人物、组织、概念、地点、事件。\n"
    "2. 不要抽代词：「他」「她」「它」「该公司」「本人」「本文」「上述」「前者」都不是实体。\n"
    "3. 不要抽泛化词：方法、内容、问题、部分、情况、方面、工作、要求、措施、数据、"
    "系统、项目、时间、过程——这些词指不到具体东西，抽出来只会把整张图连成一团。\n"
    "4. name 用材料里的写法，尽量保留全称；不要加书名号、引号、括号注释，不要写"
    "「某某（以下简称甲方）」这种带释文的形态。\n"
    "5. 同一个东西在一段材料里只写一次，不要换个说法重复出现。\n"
    "6. 一个实体名 2-15 个字：更短的多半不成词，更长的多半是整句。抽不准就不抽。\n"
    "7. type 只能取上面那六个值之一；判不准就用「其他」，不要自己造新类型。\n"
    "\n"
    "关系规则：\n"
    "8. 关系必须发生在**两个实体之间**：src 与 dst 都必须是你在 entities 里写过的"
    " name，逐字一致。不要写「他」「前者」「该组织」这类指代。\n"
    "9. relation 用**短语**（2-8 字），不要用单字，也不要写「相关」「有关」「涉及」"
    "「是」这类不含信息的关系。\n"
    "   ❌ 「相关」「有联系」「是」\n"
    "   ✅ 「属于」「提出」「依赖」「由…审批」「任职于」「位于」「适用于」\n"
    "10. 方向按材料的意思写：材料说甲依赖乙，就写 src=甲, dst=乙, relation=依赖。"
    "材料没说清方向的两样东西，不要连边，只把它们写进 entities。\n"
    "11. 一个短句里没有明确关系，就只给实体，relations 给 []。不要为了凑数编关系。\n"
    "12. 不确定就不抽：宁可少给一个实体，也不要写出材料里没有的东西。\n"
    "\n"
    "上面的例子只用来说明形状，不要把例子里的词写进你的结果。"
)


def _user_message(text: str) -> str:
    """这一次的用户消息：材料 + 一句收尾要求。

    材料用显式分隔行包起来，不加编号、不加引号——材料里的 markdown 标题、
    编号列表混进提示词的分段结构，模型会以为那也是指令。
    """
    return (
        "材料如下（只抽这段材料里明确出现的实体与关系）：\n"
        "---\n"
        f"{text}\n"
        "---\n"
        "\n"
        "只返回那个 JSON 对象。"
    )


def _source_tag(source_id: str) -> str:
    """材料指纹的前 8 位，只用于把日志里的几次抽取对上号。

    不直接记 `source_id`：它可能是用户可见的文件名或业务编号，而这份日志的
    读权限不一定和材料本身一致。前 8 位足够在同一批日志内部做关联。
    """
    return hashlib.sha256(str(source_id).encode("utf-8")).hexdigest()[:8]


def _payload(raw: Any) -> dict[str, Any] | None:
    """把模型出口的返回值收敛成 dict；收敛不了就 `None`。

    `chat_async` 内部已经过了一遍 `parse_model_json`，正常情况下这里拿到的就是
    dict。仍然再解析一次字符串，是因为真有一种响应会以字符串形态出现：模型把
    JSON 当字符串又包了一层。用**同一个**解析器收尾，而不是在这里另写一套剥围栏
    的逻辑——两套规则的结局是同一个响应在两处有两种判定。
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


def _clean_type(value: Any) -> str | None:
    """类型落到白名单内，落不进就 `None`。理由见 `ENTITY_TYPES` 的注释。"""
    if not isinstance(value, str):
        return None
    text = normalize_name(value)
    return text if text in ENTITY_TYPES else None


def _build(raw: dict[str, Any]) -> tuple[GraphExtraction | None, dict[str, int]]:
    """逐项重建抽取结果，返回 `(结果, 丢弃计数)`。

    丢弃计数只进日志：它是"这次抽取是不是在空转"的唯一线索——实体留下 3 个、
    关系丢了 20 条，说明模型根本没按"src/dst 必须是实体名"来写，下一次调提示词
    时这条数字就是判据。
    """
    dropped = {"entity": 0, "relation": 0}
    entities: list[ExtractedEntity] = []
    seen_entities: set[tuple[str, str | None]] = set()

    raw_entities = raw.get("entities")
    if not isinstance(raw_entities, list):
        raw_entities = []
    for item in raw_entities:
        if not isinstance(item, dict):
            dropped["entity"] += 1
            continue
        name = normalize_name(item.get("name"))
        if not MIN_NAME_CHARS <= len(name) <= MAX_NAME_CHARS:
            dropped["entity"] += 1
            continue
        kind = _clean_type(item.get("type"))
        if (name, kind) in seen_entities:
            # 重复项不算丢弃：同一实体在材料里出现多次是常态，模型重复列一遍
            # 正是它"抽到了同一个东西"的表现。
            continue
        seen_entities.add((name, kind))
        entities.append(ExtractedEntity(name=name, type=kind))
        if len(entities) >= MAX_ENTITIES:
            break

    known = {entity.name for entity in entities}
    relations: list[ExtractedRelation] = []
    seen_relations: set[tuple[str, str, str]] = set()
    raw_relations = raw.get("relations")
    if not isinstance(raw_relations, list):
        raw_relations = []
    for item in raw_relations:
        if not isinstance(item, dict):
            dropped["relation"] += 1
            continue
        src = normalize_name(item.get("src"))
        dst = normalize_name(item.get("dst"))
        phrase = normalize_name(item.get("relation"))
        # 这一条是三道闸里最要紧的：落一条 src/dst 不在实体表里的边，就等于凭空
        # 造出一个只被这条边提到的节点，下一轮游走会把它当真的。
        if src not in known or dst not in known:
            dropped["relation"] += 1
            continue
        if not MIN_RELATION_CHARS <= len(phrase) <= MAX_RELATION_CHARS:
            dropped["relation"] += 1
            continue
        if phrase in EMPTY_RELATIONS:
            # 长度闸拦不住的正是这一类：正好 2 个字，但一点信息都没有。
            dropped["relation"] += 1
            continue
        if src == dst:
            # 自环不携带跨实体的信息，游走时还要专门绕开它。
            dropped["relation"] += 1
            continue
        if (src, dst, phrase) in seen_relations:
            continue
        seen_relations.add((src, dst, phrase))
        relations.append(ExtractedRelation(src=src, dst=dst, relation=phrase))
        if len(relations) >= MAX_RELATIONS:
            break

    if not entities and not relations:
        return None, dropped
    return GraphExtraction(entities=entities, relations=relations), dropped


def _log(outcome: str, started: float, *, source_id: str, chunk_start: int,
         chars: int, extraction: GraphExtraction | None = None,
         dropped: dict[str, int] | None = None) -> None:
    """一行日志：**只有形状，没有内容**（见模块 docstring）。"""
    counts = dropped or {"entity": 0, "relation": 0}
    logger.info(
        "图谱抽取 outcome=%s 材料=%s 位置=%d 输入=%d字 耗时=%.0fms 实体=%d 关系=%d "
        "丢弃实体=%d 丢弃关系=%d",
        outcome, _source_tag(source_id), chunk_start, chars,
        (time.perf_counter() - started) * 1000,
        len(extraction.entities) if extraction else 0,
        len(extraction.relations) if extraction else 0,
        counts["entity"], counts["relation"],
    )


async def extract_graph(
    text: str, *, source_id: str, chunk_start: int = 0,
) -> GraphExtraction | None:
    """从一段文本里抽实体与关系；任何一步不成立都返回 `None`。

    `source_id` / `chunk_start` 这一层**不参与抽取**，只进日志：它们说明"这段
    文本是哪份材料的哪一段"，而排查"为什么这批材料一条边都没抽出来"时，唯一
    能定位的就是这两个位置信息。真正把材料落库的是 `graph_ingest`。

    开关 `MINDMAP_GRAPH_EXTRACTION` 关着时在第一行返回：不读文本、不构造客户端、
    不发请求。这条路径的代价必须是零。
    """
    if not _enabled():
        return None
    body = (text or "").strip()
    if not body:
        return None
    started = time.perf_counter()
    chars = len(body)
    if chars > MAX_INPUT_CHARS:
        # 截断而不是整段拒绝：前 1500 字里通常已经有这段材料的全部主要实体，
        # 丢掉整份等于白花一次上传。
        logger.info(
            "图谱抽取输入截断：%d 字 → %d 字（材料=%s 位置=%d）",
            chars, MAX_INPUT_CHARS, _source_tag(source_id), chunk_start,
        )
        body = body[:MAX_INPUT_CHARS]

    outcome = "ok"
    raw: Any = None
    try:
        # 只发一次，绝不重试：失败就是这段没抽到，重试只会把入库时间乘以二，
        # 而且下一次多半还是同样的结果（提示词与模型都没变）。
        raw = await asyncio.wait_for(
            chat_async(SYSTEM_PROMPT, _user_message(body), max_tokens=MAX_OUTPUT_TOKENS),
            timeout=TIMEOUT_SECONDS,
        )
    except (TimeoutError, asyncio.TimeoutError):
        outcome = "timeout"
    # 模型这层怎么坏（没配置 / HTTP 错误 / 非 JSON）都只是"这段没抽到"。注意这里
    # 不拦 `CancelledError`：那是我们被取消（客户端断开），吞掉它会让上层以为
    # 任务正常跑完了。
    except Exception:  # noqa: BLE001
        outcome = "error"
    if outcome != "ok":
        _log(outcome, started, source_id=source_id, chunk_start=chunk_start, chars=chars)
        return None

    payload = _payload(raw)
    if payload is None:
        _log("bad_json", started, source_id=source_id, chunk_start=chunk_start, chars=chars)
        return None

    extraction, dropped = _build(payload)
    if extraction is None:
        _log("empty", started, source_id=source_id, chunk_start=chunk_start,
             chars=chars, dropped=dropped)
        return None
    _log("ok", started, source_id=source_id, chunk_start=chunk_start, chars=chars,
         extraction=extraction, dropped=dropped)
    return extraction
