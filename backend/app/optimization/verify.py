"""生成完之后用**一次**联网搜索核对这棵树：给每个节点打"有依据 / 未证实 / 相矛盾"。

## 它补的是哪一环

生成那条路上，**没有任何一步回头看这棵树是不是真的**：

    纯主题生成   骨架凭主题猜、结构凭主题写、说明凭主题写；
                 而 `DETAIL_NO_SOURCE_RULES` 明令"不要写年份、人名、论文标题、数字统计"
                 → 结果是一大棵【没有具体内容、也无法核对】的树

材料那条路是有核对的（每条 `source_ref` 都过逐字校验），但那是"引文出处在不在原文
里"，不是"这个节点说的对不对"。纯主题那条路上连前者都没有。

这一步只做一件事：拿**一个** query 去搜一次，把 3–5 篇正文摆给模型，让它逐节点判
"这些正文里有没有一句话在说这件事"。判据是"引文能不能逐字落回正文"，不是它的措辞
有多自信。

## 为什么是"整树一次"而不是"逐节点搜"

    逐节点   40 节点 × 1 次搜索 × 0.9 秒 = 36 秒（并发 5 路仍要 8 秒）/ 约 0.2 元
    整树一次 1 次搜索 / 0.9 秒 / 0.005 元

40 倍。实测（`scripts/measure_verify_states.py`，10 张真图、1,300 个节点）见
`docs/GENERATION-VERIFY.md` 第 1 节：这个便宜版本的支持率与"引文逐字率"两条读数
都单独给了，**先看数字再决定要不要逐节点精搜**——把它写成可复现的一条命令，而不是
凭直觉选一个。

## 三种状态，一条不能改的处置

    supported     留 下，挂引用（quote 逐字来自搜到的正文，过 `verify_source_refs`）
    contradicted  留下但标 conflict，让用户看
    unverified    **保留，不删**，标 unverified

**"未证实"是一个状态，不是一个待删除项。** 删是不可逆的，而"小众但正确"与"模型编的"
在"搜不到"这个结果上长得一模一样——删一次就把两者一起删了。所以这一步**从不删节点**，
它只往 `data` 里写一份 `node_id → 状态` 的表（`Node` 是 `extra="forbid"`，三态进不了
契约字段；进契约就要重导 `openapi.json` 与 `schema.d.ts`，那是另一条线的事）。

## 与"全塞不检索"的关系

材料装得下时（`FULL_TEXT_MATERIAL_CHARS`）按支检索整段跳过，省下的钱正好付给这一步的
搜索：**同一笔钱从"召回材料"换成"核对这棵树"**。这是这一步在成本上唯一站得住的理由，
见 `docs/GENERATION-VERIFY.md` 第 3 节。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from app.contracts.mindmap import DiagramContent, Node
from app.contracts.source import SourceRef, Source

logger = logging.getLogger(__name__)

# 开关。**默认关**：它是"生成之后多一次外网往返 + 一次模型调用"，而默认值一改就是
# 每一次生成都多这两笔——与 `MINDMAP_EXTERNAL_DEEPSEEK_SEARCH` 同一条规矩。
VERIFY_ENV = "MINDMAP_VERIFY_AFTER_GENERATE"
_TRUTHY = {"1", "true", "yes", "on", "y", "t"}

# 三态。**只有这三个**：多一个值就多一种没有人处理的产出，而 UI 那一层（按状态过滤）
# 现在只认识这三个。
SUPPORTED = "supported"
UNVERIFIED = "unverified"
CONTRADICTED = "contradicted"
STATES = (SUPPORTED, UNVERIFIED, CONTRADICTED)

# 一次判定塞几个节点。60 个 × 约 110 字（路径 + 标签 + 说明）≈ 6,600 字，加上 4 篇
# 正文 ≈ 13,000 字，一次调用装得下；再往上就开始漏判（实测漏判集中在尾部）。
JUDGE_MAX_NODES = 60

# 每棵树取几篇正文。4 比生成用的 `external.DEFAULT_LIMIT=3` 多一篇：核对比生成更吃
# 覆盖面——生成时少一篇只是少一块素材，核对时少一篇会让一整批节点凭空变成"未证实"。
ARTICLES = 4

# 引用一句话的长度区间。太短（<15）在正文里必然多处命中、判不了指哪一句；太长
# （>120）模型会开始"合并两段"而不是抄一句，逐字校验必然失败。
QUOTE_MIN_CHARS = 15
QUOTE_MAX_CHARS = 120
# `SourceRef.quote` 的契约上限是 300。取 120 之后仍留一倍余量：跨行原文里可能夹着
# 换行与缩进，定位回来之后那个切片会比模型抄的长。
_MAX_REF_QUOTE = 280

_WS = re.compile(r"\s+")

# 判 supported 时逐字抄原文——**这一段是整个功能的判据**，不是措辞偏好。只说"找得到
# 支持就判 supported"，模型会把"文章提到了同一个话题"算成支持：那样 supported 量的
# 是"这个话题在网上有没有资料"，不是"这个节点对不对"。三句反例对应实测里最常被误判
# 的三种形状。
JUDGE_SYSTEM = (
    "你是核对员，只做一件事：判断一棵已经生成的思维导图里，每个节点的内容能不能在"
    "给定的文章正文里找到依据。\n"
    "三种判定，只有这三种：\n"
    f"- {SUPPORTED}：文章里有**一句话本身就在陈述这个节点的内容**，而不只是聊到同一个话题。\n"
    f"- {UNVERIFIED}：这几篇文章里找不到能直接支持它的句子。\n"
    f"- {CONTRADICTED}：文章里的说法与这个节点的内容相反。\n"
    "以下三种**不算** supported：文章聊的是同一个话题但没说到这一点；只是出现了同一个"
    "词；只说了这个方向的大概意思而没有这句话。\n"
    "判 supported 时必须**逐字抄出那句原文**（"
    f"{QUOTE_MIN_CHARS}–{QUOTE_MAX_CHARS} 字，一字不改、不加省略号、不改写、不拼接），"
    "并给出它在第几篇。\n"
    "只回 JSON，不要任何解释：\n"
    '{"verdicts":[{"id":"节点 id","state":"supported","article":1,"quote":"逐字原文"}]}\n'
    "判 unverified / contradicted 时 quote 给空字符串。"
    "给到的每个节点 id 都要有一条判定，一条都不能漏，也不要编新的 id。"
)


def verify_enabled() -> bool:
    """开关是否打开。默认关，理由见 `VERIFY_ENV` 上面那段。"""
    return os.getenv(VERIFY_ENV, "").strip().lower() in _TRUTHY


# ---------------------------------------------------------------- 输入：query 与节点


def build_query(content: DiagramContent, *, limit: int | None = None) -> str:
    """整树一次搜索的 query：`标题 + 最外层内容节点名`，去重、截断到 `limit` 字符。

    三条规则，每条都对应一种实测的坏结果：

      - **标题在前**：它是这棵树在说哪件事的唯一权威表述。反过来（分支在前）会得到
        "目标与边界 技术选型与依据 …"这种一串方法论语汇，搜回来的是"怎么写方案"而不是
        "这个主题本身"；
      - **分支名去重且跳过"已经在串里"的**：一级分支常是标题的复述（标题「碳中和的主要
        方面」+ 分支「碳中和与碳达峰目标」），不去重会把 50 字预算吃掉一半换零信息量；
      - **满了就停，不截半个词**：`external.QUERY_MAX_CHARS=50` 是抓取层的硬上限，在词
        边界停比让它从中间砍断更可读——砍断的那半截既是噪声又占预算。

    `limit` 缺省取 `external.QUERY_MAX_CHARS`（抓取层截断用的同一个数：两处各写一个值，
    迟早一个是 50、一个是 60，而表现是"拼进去的词被静默砍掉"）。
    """
    from app.agent import shapes
    from app.knowledge import external

    budget = external.QUERY_MAX_CHARS if limit is None else limit
    parts: list[str] = []
    used = 0
    for raw in (content.title, *(node.label for node in shapes.top_branches(content))):
        text = _WS.sub(" ", (raw or "").strip())
        if not text:
            continue
        if any(text in part or part in text for part in parts):
            continue
        extra = len(text) + (1 if parts else 0)
        if used + extra > budget:
            if parts:
                break
            text = text[:budget]          # 标题自己就超长：只能截它
            extra = len(text)
        parts.append(text)
        used += extra
    return " ".join(parts)[:budget]


def node_rows(content: DiagramContent) -> list[dict[str, str]]:
    """每个节点一行：`id / 路径 / 标签 / 说明`。**前序**，与画布上的阅读顺序一致。

    路径用「父 / 子」而不是只给标签：同一个词在不同分支下意思不同（「评估与调优」在
    「AI 模型训练」与「检索与召回的取舍」下不是一件事），只给标签时判的是词面，给了
    路径才判得到"这个节点说的是什么"。

    多棵树（鱼骨六根骨、流程各阶段）各自从自己的根开始数路径，根节点所在那一层的
    路径写成空串——它就是这根骨/这个阶段的名字，重复一遍只占预算。
    """
    from app.agent import shapes

    rows: list[dict[str, str]] = []
    for root in shapes.roots(content):
        stack: list[tuple[Node, str]] = [(root, "")]
        while stack:
            node, path = stack.pop()
            rows.append({
                "id": node.id,
                "path": path,
                "label": (node.label or "").strip(),
                "detail": _WS.sub(" ", (node.detail or "").strip())[:200],
            })
            child_path = f"{path} / {node.label}" if path else (node.label or "")
            for child in reversed(list(node.children)):
                stack.append((child, child_path))
    return rows


def judge_messages(
    title: str, rows: Sequence[dict[str, str]], articles: Sequence[dict[str, str]],
) -> tuple[str, str]:
    """一次判定用的 `(system, user)`。判据在 `JUDGE_SYSTEM` 里，这里只拼材料。

    `articles` 的元素是 `{title, site, text}`：站点名要进 prompt——判"这句是百科还是
    招生简章说的"直接影响可信度，而只给正文时那几篇长得一样。
    """
    lines = [f"## 这棵树（标题：{title}；{len(rows)} 个节点）", "",
             "格式：id | 层级路径 | 标签 | 说明"]
    for row in rows:
        lines.append(f"{row['id']} | {row['path']} | {row['label']} | {row['detail']}")
    lines += ["", f"## 文章正文（共 {len(articles)} 篇）"]
    for index, article in enumerate(articles, start=1):
        lines += ["", f"[{index}] {article.get('title', '')}"
                  f"（{article.get('site', '')}）", article.get("text", "")]
    return JUDGE_SYSTEM, "\n".join(lines)


# ---------------------------------------------------------------- 判定：解析与定位


def parse_verdicts(raw: Any, known: Sequence[str]) -> tuple[list[dict[str, Any]], int]:
    """模型返回 → 合法判定列表；第二个返回值是"无效/漏判"的条数。

    只收 `known` 里的 id、且 `state` 在三态内的条目：模型偶尔会返回它自己编的 id
    （对不上任何节点）或第四个状态名（实测出现过 `unknown` / `partially_supported`）。
    **不把无效条目折成 unverified**——那会让"模型没判"与"搜不到依据"混成一个数，
    而这一步的结论全建立在这两个数分得开。

    `raw` 已经是 `dict` / `list` 时直接读它（`chat_async` 返回的就是解析好的 JSON），
    **不要再走 `parse_model_json`**：那个函数第一行是 `content.strip()`，一个 dict
    进去会抛 `AttributeError`，而调用方把异常记成"这一块漏判"——整棵树判 0 条，
    看起来像模型不听话。
    """
    from app.generation.service import parse_model_json

    if isinstance(raw, (dict, list)):
        payload: Any = raw
    else:
        payload = parse_model_json(raw)
    if isinstance(payload, dict):
        items = payload.get("verdicts") or payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    allowed = set(known)
    out: list[dict[str, Any]] = []
    invalid = 0
    for item in items:
        if not isinstance(item, dict):
            invalid += 1
            continue
        node_id = str(item.get("id") or "").strip()
        state = str(item.get("state") or "").strip().lower()
        if node_id not in allowed or state not in STATES:
            invalid += 1
            continue
        quote = _WS.sub(" ", str(item.get("quote") or "")).strip()
        article = item.get("article")
        out.append({"id": node_id, "state": state, "quote": quote,
                    "article": article if isinstance(article, int) and article > 0 else None})
    return out, invalid


def locate_quote(quote: str, text: str) -> tuple[int, int] | None:
    """引文在原文里的确切位置 `[start, end)`；定位不到返回 `None`。

    **容错的只有空白**：模型抄一句跨行的原文时会顺手把换行换成空格，那不是"编的"；
    除此之外一个字都不放过——去掉空白仍然对不上，就是它自己写的一句话。

    返回的是 `text[start:end]` 这个**原始切片**的坐标，而挂进 `SourceRef` 的 quote 也
    必须用这个切片：`verify_source_refs` 的判据是 `quote in source.text`，拿模型那版
    （换行已被替换）去挂会命中 `SOURCE_QUOTE_MISMATCH`——一条我们自己造出来的失败。
    """
    needle = _WS.sub("", quote or "")
    if not needle:
        return None
    positions = [index for index, char in enumerate(text or "") if not char.isspace()]
    flat = "".join((text or "")[index] for index in positions)
    at = flat.find(needle)
    if at < 0:
        return None
    return positions[at], positions[at + len(needle) - 1] + 1


def quote_in(quote: str, articles: Sequence[dict[str, str]]) -> int | None:
    """引文逐字落在第几篇正文里（1 起），都不在返回 `None`。"""
    for index, article in enumerate(articles, start=1):
        if locate_quote(quote, article.get("text", "")) is not None:
            return index
    return None


# ---------------------------------------------------------------- 报告


@dataclass
class VerifyReport:
    """这一步的产物：三态计数 + 每个节点的判定 + 那一笔账。

    `nodes` 是 `node_id → {state, quote, article, source_id}`，**只有被判过的节点**
    在里面：漏判的节点不在表里，而"不在表里"与"判成未证实"必须分得开（前者是模型没
    回答，后者是回答"搜不到"）。`skipped` 说明这一次为什么没判（预算 / 没有可判的节点
    / 搜索空手而归），它进 `data` 之后"这次为什么没有三态"就有了答案。
    """

    query: str = ""
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    articles: int = 0
    seconds: float = 0.0
    search_seconds: float = 0.0
    invalid: int = 0
    skipped: str = ""
    attached: int = 0
    dropped: int = 0
    calls: int = 0
    # 这一步亲手挂上引用的节点 id。**不进 `as_dict`**（那是给前端与遥测的形状），
    # 但收尾那一遍逐字校验必须知道"哪些引用是我挂的"：把生成那一步写的引用一起
    # 塞进校验，等于用一个新步骤去改一棵已经被校验过的树。
    attached_nodes: list[str] = field(default_factory=list)

    def count(self, state: str) -> int:
        return sum(1 for item in self.nodes.values() if item.get("state") == state)

    @property
    def states(self) -> dict[str, int]:
        return {state: self.count(state) for state in STATES}

    @property
    def judged(self) -> int:
        return len(self.nodes)

    def as_dict(self) -> dict[str, Any]:
        """进 `data["verify"]` 的形状。

        键集固定（空结果也带 `states` 的三个 0）：读它的脚本不该因为"这次一条都没判"
        而要去处理"键不存在"。
        """
        return {
            "query": self.query,
            "states": self.states,
            "judged": self.judged,
            "articles": self.articles,
            "nodes": self.nodes,
            "attached_refs": self.attached,
            "dropped_refs": self.dropped,
            "invalid": self.invalid,
            "calls": self.calls,
            "search_seconds": round(self.search_seconds, 3),
            "seconds": round(self.seconds, 3),
            "skipped": self.skipped,
        }


# ---------------------------------------------------------------- 执行


def search_articles(query: str, *, limit: int = ARTICLES) -> list[Any]:
    """一次 DeepSeek Search → `ExternalHit` 列表。**失败返回空列表，永不抛**。

    只用第六个源，不用六源混合：这条链要的是"一整段能逐字抄的正文"。五个源里维基给
    条目正文、三篇论文源给 abstract——后者不是"能引用的整段"，混进来只会让"引文在不在
    正文里"这个判据失去意义。搜索失败与"搜到 0 条"在报告里分开（`skipped`），因为
    前者重试有用、后者重试没用。
    """
    from app.knowledge import external

    try:
        budget = external._Budget(3, 30.0)
        hits = external._deepseek_search(query, budget, limit)
    except Exception as exc:  # noqa: BLE001 — 核对失败只降级，理由见模块 docstring
        logger.warning("核对检索失败（%r）：%s: %s",
                       query[:40], type(exc).__name__, str(exc)[:160])
        return []
    return [hit for hit in hits if (hit.text or "").strip()]


async def verify_tree(
    content: DiagramContent,
    *,
    articles: Sequence[Any] | None = None,
    chat: Callable[..., Any] | None = None,
    search: Callable[[str], list[Any]] | None = None,
    max_tokens: int | None = None,
    title: str | None = None,
) -> VerifyReport:
    """搜一次（或收下调用方搜好的），逐节点判三态。**只读树，一条引用都不挂。**

    挂引用是 `attach_refs` 的事，由调用方在核对之后做——两个理由：

      - `Source` 要持久 id 才敢挂（`library.ingest`），而那要写库、要进 `ctx`，
        属于工具层；
      - "改写树"与"判断树"分开之后，`verify_tree` 可以在用例里对着固定文本跑，
        不需要 HTTP、不需要库、不需要模型。

    `articles` 给了就不搜（调用方可能已经搜过，或者要拿固定文本复现）；
    `chat` / `search` 是两条注入缝，缺省走 `generation.service.chat_async` 与
    `knowledge.external`。

    **失败一律降级成空报告**：核对是生成之后多出来的一步，它挂掉不该让一棵已经成型
    的树交付失败。
    """
    started = time.perf_counter()
    query = build_query(content)
    report = VerifyReport(query=query)
    if articles is None:
        searcher = search or (lambda text: search_articles(text))
        search_started = time.perf_counter()
        try:
            articles = await asyncio.to_thread(searcher, query)
        except Exception as exc:  # noqa: BLE001 — 见 docstring 最后一段
            logger.warning("核对检索抛出（%r）：%s: %s",
                           query[:40], type(exc).__name__, str(exc)[:160])
            articles = []
        report.search_seconds = time.perf_counter() - search_started
    if not articles:
        report.skipped = "no_articles"
        report.seconds = time.perf_counter() - started
        return report

    plain = [{"title": getattr(hit, "title", "") or "", "site": getattr(hit, "site", "")
              or getattr(hit, "source_name", "") or "", "text": getattr(hit, "text", "") or ""}
             for hit in articles]
    report.articles = len(plain)
    rows = node_rows(content)
    if not rows:
        report.skipped = "no_nodes"
        report.seconds = time.perf_counter() - started
        return report

    if chat is None:
        from app.generation.service import chat_async

        async def chat(system: str, user: str, **kwargs: Any) -> Any:  # noqa: F811
            return await chat_async(system, user, **kwargs)

    for start in range(0, len(rows), JUDGE_MAX_NODES):
        chunk = rows[start:start + JUDGE_MAX_NODES]
        system, user = judge_messages(title or content.title, chunk, plain)
        try:
            response = await chat(system, user, max_tokens=max_tokens or 8192)
            parsed, rejected = parse_verdicts(
                _payload_of(response), [item["id"] for item in chunk])
        except Exception as exc:  # noqa: BLE001 — 一块判不出来只算它漏判
            logger.warning("核对判定失败（%s）：%s: %s",
                           chunk[0]["id"], type(exc).__name__, str(exc)[:160])
            parsed, rejected = [], len(chunk)
        report.calls += 1
        report.invalid += rejected
        for item in parsed:
            report.nodes[item["id"]] = {
                "state": item["state"],
                "quote": item["quote"],
                "article": item["article"],
            }
    report.seconds = time.perf_counter() - started
    if not report.nodes:
        report.skipped = "no_verdicts"
    return report


def _payload_of(response: Any) -> Any:
    """`chat_async` 的返回值 → `parse_verdicts` 能吃的东西。

    **`chat_async` 返回的是已经解析好的 JSON**（`_chat_once` 里就是
    `parse_model_json`），不是 `{"content": "..."}`。写成函数是因为这个形状反直觉：
    按 `response["content"]` 取会拿到 `None`，而 `None` 会被调用方的 `except` 接住
    记成"这一块漏判"——整棵树判 0 条，看起来像模型不听话。

    字符串**原样返回**，不再 `json.dumps` 一遍：模型偶尔回一段不是 JSON 的正文，
    `json.dumps` 会把 `不是 JSON` 变成一个合法的 JSON **字符串**，于是"解析失败"
    变成"解析出一个字符串"——`parse_verdicts` 拿到空表，那一块的漏判被静默记成 0。
    """
    if isinstance(response, dict) and isinstance(response.get("content"), str):
        return response["content"]
    if isinstance(response, (str, dict, list)):
        return response
    import json

    return json.dumps(response)


def attach_refs(
    content: DiagramContent,
    report: VerifyReport,
    sources: Sequence[Source],
) -> int:
    """把 supported 的引文挂到节点上，返回挂上几条。**不动已经有引用的节点。**

    两条硬规则：

      - **引文必须是原文切片**（`locate_quote` 的返回值），不是模型抄的那版：后者的
        换行被替换过，`verify_source_refs` 会判它不是原文；
      - **已经有 `source_refs` 的节点一个字节都不碰**：那些是生成那一步写下的、已经
        过过逐字校验的引用，用核对这一步的引文盖掉它等于把"材料里的依据"换成"网上搜
        到的一句"，两件事的可信度不同。

    `sources` 与 `report.nodes[...]["article"]` 的下标一一对应（第 1 篇 → `sources[0]`）。
    对不上的（搜索返回的篇数与挂引用时的不一致）跳过并计数，不猜。
    """
    from app.agent import shapes

    index = {node.id: node for node in shapes.iter_nodes(content)}
    attached = 0
    attached_ids: list[str] = []
    for node_id, verdict in report.nodes.items():
        if verdict.get("state") != SUPPORTED:
            continue
        node = index.get(node_id)
        if node is None or node.source_refs:
            continue
        article = verdict.get("article")
        if not isinstance(article, int) or not 1 <= article <= len(sources):
            continue
        source = sources[article - 1]
        found = locate_quote(verdict.get("quote", ""), source.text or "")
        if found is None:
            continue
        start, end = found
        # quote 用**原文切片**：契约的逐字校验比的是 `quote in source.text`。
        quote = (source.text or "")[start:end][:_MAX_REF_QUOTE]
        node.source_refs = [SourceRef(source_id=source.id, quote=quote,
                                     start=start, end=end)]
        attached += 1
        attached_ids.append(node_id)
    report.attached = attached
    report.attached_nodes = attached_ids
    return attached


def summarize(report: VerifyReport, sources: Sequence[Source]) -> dict[str, Any]:
    """核对这一步给人看的那一句话。

    三句话术各自对应一个要去做的动作，不能合并：`contradicted` 要人去看那两条，
    `unverified` 多半是"这个题太小众"，`supported` 是常态。一条都没有时说明为什么。
    """
    states = report.states
    if report.skipped == "no_articles":
        return "核对没有做：这次联网搜索一篇正文都没拿到（不代表节点是编的，只是没核对）"
    if report.skipped == "no_nodes":
        return "核对没有做：这棵树没有可判的节点"
    if not report.judged:
        return "核对没有做：模型没有给出任何一条可用的判定"
    parts = [f"已用一次联网搜索核对 {report.judged} 个节点（{report.articles} 篇正文）"]
    if states[SUPPORTED]:
        parts.append(f"{states[SUPPORTED]} 个在正文里找得到支持"
                     + (f"（其中 {report.attached} 个已挂上逐字引用）"
                        if report.attached else ""))
    if states[UNVERIFIED]:
        parts.append(f"{states[UNVERIFIED]} 个未证实（保留，未删除）")
    if states[CONTRADICTED]:
        parts.append(f"{states[CONTRADICTED]} 个与正文相矛盾（保留并标出，请你核对）")
    if report.invalid:
        parts.append(f"{report.invalid} 个节点没被判到")
    return "；".join(parts) + "。"
