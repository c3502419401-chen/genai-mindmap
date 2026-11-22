"""生成完之后用**一次**联网搜索核对整棵树：三态分布实测（不改产品行为）。

要回答的问题只有一个：**"整树一次搜索"这个便宜版本，够不够拿来核对一棵纯主题生成的树？**

    cd backend
    python scripts/measure_verify_states.py                # 10 张真图，各搜一次
    python scripts/measure_verify_states.py --trees 3      # 先跑 3 张看看
    python scripts/measure_verify_states.py --dry-run      # 只挑样本、拼 query，不联网
    python scripts/measure_verify_states.py --reuse        # 复用落盘结果，只重算统计
    python scripts/measure_verify_states.py --reuse --strict   # 再补一次严格复核

## 为什么是"一次搜索"而不是"逐节点搜索"

    逐节点   40 节点 × 1 次搜索 × 0.9 秒 = 36 秒（并发 5 路仍要 8 秒）/ 约 0.2 元
    整树一次 title + 一级分支拼一个 query → 1 次搜索 / 0.9 秒 / 0.005 元
             → 拿到 3–5 篇正文
             → 让模型判"这棵树里哪些节点在这几篇里找得到支持"

差 40 倍的成本。这张表就是"便宜的那个到底能不能用"的判据：supported 只有个位数
说明整树一次太粗（得换逐节点），supported 过半说明这个形态已经够回答"哪些是编的"。

## 样本口径（复用 `measure_cross_map.load_maps`）

真图 = 库里保存过的 + `runs.candidate` 里生成出来没保存的，**排除 22 张画布回归基线**
（节点标签是「第二条分支」「A 侧交付面」这类占位文本）。在那个池子上再加两条：

  1. **同题只取一张**（取节点最多的那张）。同题 10 张测的是同一个 query、同一批文章，
     把它们算成 10 个独立样本只会让区间看起来比实际窄；
  2. 按节点数降序取前 `--trees` 张。这就是全部规则——没有别的筛选，跑出来的数字
     就是"库里那些真树上这个版本的表现"。

## 三条读数怎么读

  - **三态各占多少**：分母是**被判过的节点**（超过 60 个节点分块判，块内不漏），
    不等于树的总节点数时脚本会单独报覆盖率。
  - **supported 的引用是不是真在正文里**：判 supported 要求模型逐字抄原文，脚本
    拿那句话去正文里做子串匹配（去空白后比）。**不在**的比例就是"模型编了一句
    像原文的话"的比例——它比 state 本身更能说明这个核对有没有用。
  - **`--strict`：supported 里有多少是真的被支持**。把（节点, 引文）单独再问一次
    模型："这段引文是在陈述这个节点的内容，还是只是在聊同一个话题？"这一问才是
    "核对"与"搜到了同一话题的文章"的分界线，脚本把它单列一栏。

## 成本

一次 DeepSeek Search 0.005 元、返回 900–2200 字正文；每棵树一次判定调用（节点多
时按 60 个一块），10 张图合计约 0.1 元。**不写缓存**：缓存会把"这次到底搜了什么"
抹掉，而这批数字的意义全在"同一批 query 下真实返回了什么"。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(BACKEND / "scripts") not in sys.path:
    sys.path.insert(0, str(BACKEND / "scripts"))

import measure_cross_map as cross_map  # noqa: E402  （同一份样本口径 + env 装载）

OUT = BACKEND / "data" / "measure_verify_states.json"

# 三态。**只有这三个**，多一个值就等于多一种没有人处理的产出。
STATES = ("supported", "unverified", "contradicted")

# 一次判定塞几个节点。60 个 × 约 110 字（路径+标签+说明）≈ 6,600 字，加上 4 篇
# 正文 ≈ 13,000 字，一次调用装得下且注意力够用；再往上就开始漏判（实测漏判集中在
# 尾部）。
JUDGE_MAX_NODES = 60

# 引用一句话的长度区间。太短（<15）在正文里必然多处命中、判不了是指哪一句；太长
# （>120）模型会开始"合并两段"而不是抄一句，逐字校验必然失败。
QUOTE_MIN_CHARS = 15
QUOTE_MAX_CHARS = 120

# 每棵树取几篇。3 篇是 `external.DEFAULT_LIMIT`；核对比生成更吃覆盖面，取 4。
ARTICLES = 4

_WS = re.compile(r"\s+")

logger = logging.getLogger("measure_verify_states")


def _payload_of(response: Any) -> str | dict[str, Any]:
    """`chat_async` 的返回值 → `parse_verdicts` 能吃的东西。

    **`chat_async` 返回的是已经解析好的 JSON**（`_chat_once` 里就是
    `parse_model_json`），不是 `{"content": "..."}`。这一点必须写成函数：它的形状
    反直觉，第一版按 `response["content"]` 取，拿到的是 `None`，而 `None` 会被下面的
    `except` 接住记成"这一块漏判"——整棵树判 0 条，看起来像模型不听话。
    """
    if isinstance(response, dict) and isinstance(response.get("content"), str):
        return response["content"]
    if isinstance(response, (str, dict, list)):
        return response
    return json.dumps(response)


# ---------------------------------------------------------------- 样本与 query


def pick_trees(maps: Sequence[tuple[str, str, Any]], limit: int) -> list[dict[str, Any]]:
    """同题去重 → 节点数降序 → 前 `limit` 张。

    返回 `{origin, id, title, content, node_count, refs}`。排序键里带上标题与 id 是为了
    **确定性**：同节点数时输出顺序不变，两次跑拿到同一批样本，否则这张表的"复现方法"
    里就得写"看你运气"。
    """
    from app.agent import shapes

    best: dict[str, dict[str, Any]] = {}
    for origin, ident, content in maps:
        nodes = list(shapes.iter_nodes(content))
        row = {"origin": origin, "id": ident, "title": content.title, "content": content,
               "node_count": len(nodes),
               "refs": sum(len(node.source_refs) for node in nodes)}
        current = best.get(content.title)
        if current is None or (row["node_count"], row["refs"]) > (
                current["node_count"], current["refs"]):
            best[content.title] = row
    rows = sorted(best.values(),
                  key=lambda row: (-row["node_count"], row["title"], row["id"]))
    return rows[:limit]


def build_query(title: str, branches: Iterable[str], *, limit: int = 50) -> str:
    """整树一次搜索的那个 query：`标题 + 一级分支名`，去重、截断到 `limit` 字符。

    三条规则，每条都对应一种实测的坏结果：

      - **标题在前**：它是这棵树在说哪件事的唯一权威表述。反过来（分支在前）会
        得到"目标与边界 技术选型与依据 …"这种一串方法论语汇，搜回来的是"怎么写
        方案"而不是"这个主题本身"；
      - **分支名去重且跳过"已经在串里"的**：一级分支常是标题的复述（标题「碳中和
        的主要方面」+ 分支「碳中和与碳达峰目标」），不去重会把 50 字预算吃掉一半
        换零信息量；
      - **满了就停，不截半个词**：`external.QUERY_MAX_CHARS=50` 是抓取层的硬上限，
        在词边界停比让它从中间砍断更可读——砍断的那半截既是噪声又占预算。

    `limit` 只给用例改小用；生产上就是 `external.QUERY_MAX_CHARS`。
    """
    parts: list[str] = []
    used = 0
    for raw in (title, *branches):
        text = _WS.sub(" ", (raw or "").strip())
        if not text:
            continue
        if any(text in part or part in text for part in parts):
            continue
        extra = len(text) + (1 if parts else 0)
        if used + extra > limit:
            if parts:
                break
            text = text[:limit]          # 标题自己就超长：只能截它
            extra = len(text)
        parts.append(text)
        used += extra
    return " ".join(parts)[:limit]


def node_rows(content: Any) -> list[dict[str, str]]:
    """树的每个节点一行：`id / 路径 / 标签 / 说明`。**前序**，与树的读法一致。

    路径用「父 / 子」而不是只给标签：同一个词在不同分支下意思不同（「评估与调优」
    在「AI 模型训练」和「检索与召回的取舍」下不是一件事），只给标签时判的是词面，
    给了路径才判得到"这个节点说的是什么"。

    根节点的路径写成空串（它就是标题，再重复一遍只会占预算）。
    """
    rows: list[dict[str, str]] = []
    stack: list[tuple[Any, str]] = [(content.root, "")]
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


# ---------------------------------------------------------------- 判定


def judge_messages(
    title: str, rows: Sequence[dict[str, str]], articles: Sequence[dict[str, str]],
) -> tuple[str, str]:
    """一次判定用的 `(system, user)`。

    system 里那三句"什么不算 supported"是整张表的**判据**，别删：只说"找得到支持
    就判 supported"，模型会把"文章提到了同一个话题"算成支持——那会让 supported
    的数值看起来漂亮，而它量的其实是"这个话题在网上有没有资料"，不是"这个节点
    对不对"。三句反例（相关但没说到这一点 / 只是同一个词 / 只说了大方向）对应
    实测里最常被误判成 supported 的三种形状。
    """
    system = (
        "你是核对员，只做一件事：判断一棵已经生成的思维导图里，每个节点的内容能不能"
        "在给定的文章正文里找到依据。\n"
        "三种判定，只有这三种：\n"
        "- supported：文章里有**一句话本身就在陈述这个节点的内容**，而不只是聊到同一个话题。\n"
        "- unverified：这几篇文章里找不到能直接支持它的句子。\n"
        "- contradicted：文章里的说法与这个节点的内容相反。\n"
        "以下三种**不算** supported：文章聊的是同一个话题但没说到这一点；只是出现了"
        "同一个词；只说了这个方向的大概意思而没有这句话。\n"
        "判 supported 时必须**逐字抄出那句原文**（"
        f"{QUOTE_MIN_CHARS}–{QUOTE_MAX_CHARS} 字，一字不改、不加省略号、不改写、不拼接），"
        "并给出它在第几篇。\n"
        "只回 JSON，不要任何解释：\n"
        '{"verdicts":[{"id":"节点 id","state":"supported","article":1,"quote":"逐字原文"}]}\n'
        "判 unverified / contradicted 时 quote 给空字符串。"
        "给到的每个节点 id 都要有一条判定，一条都不能漏，也不要编新的 id。"
    )
    lines = [f"## 这棵树（标题：{title}；{len(rows)} 个节点）", "",
             "格式：id | 层级路径 | 标签 | 说明"]
    for row in rows:
        lines.append(f"{row['id']} | {row['path']} | {row['label']} | {row['detail']}")
    lines += ["", f"## 文章正文（共 {len(articles)} 篇）"]
    for index, article in enumerate(articles, start=1):
        lines += ["", f"[{index}] {article['title']}（{article['site']}）", article["text"]]
    return system, "\n".join(lines)


def parse_verdicts(raw: Any, known: Sequence[str]) -> tuple[list[dict[str, Any]], int]:
    """模型返回 → 合法判定列表。返回 `(判定, 无效条数)`。

    只收 `known` 里的 id、且 `state` 在三态内的条目：模型偶尔会返回它自己编的 id
    （读取时对不上任何节点）或第四个状态名（实测出现过 `unknown` /
    `partially_supported`）。**不把无效条目折成 unverified**——那会让"模型没判"与
    "搜不到依据"混成一个数，而这张表整段的结论都建立在这两个数分得开。
    """
    from app.generation.service import parse_model_json

    payload = parse_model_json(raw if isinstance(raw, str) else json.dumps(raw))
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
                    "article": article if isinstance(article, int) else None})
    return out, invalid


def quote_at(quote: str, articles: Sequence[dict[str, str]]) -> int | None:
    """引文逐字落在第几篇正文里（1 起），不在任何一篇里返回 `None`。

    **去空白后比**：模型抄一句跨行的原文时会顺手把换行换成空格，那不是"编的"。
    除此之外不做任何模糊匹配——去掉空白之后仍然对不上，就是它自己写的一句话。
    """
    needle = _WS.sub("", quote or "")
    if not needle:
        return None
    for index, article in enumerate(articles, start=1):
        if needle in _WS.sub("", article["text"] or ""):
            return index
    return None


def _bigrams(text: str) -> set[str]:
    clean = _WS.sub("", (text or ""))
    return {clean[i:i + 2] for i in range(len(clean) - 1)}


def label_in(quote: str, label: str) -> float:
    """节点标签有多少比例（按二元组）出现在这段引文里。0–1。

    这是"主题相关"与"说的是这件事"之间一条**零成本**的旁证：引文把节点标签的词
    全带着（≈1.0）时它至少在讲同一件事；接近 0 时那段引文只是同话题的另一句。
    它不能替代语义判断（`--strict` 那一栏才是），但它能给出"便宜版本判得有多虚"
    的第一层数字。
    """
    left, right = _bigrams(quote), _bigrams(label)
    if not right:
        return 0.0
    return len(left & right) / len(right)


# ---------------------------------------------------------------- 统计


def summarise(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """一批（树, 节点, 判定）→ 这张表的数字。

    分母一律是**判过的节点数**（`judged`），不是树的总节点数：两者不等时 `coverage`
    会把差额说出来，否则"支持率 60%"到底是 60% of 什么就没人答得上。
    """
    total_nodes = sum(row["node_count"] for row in rows)
    counts = {state: 0 for state in STATES}
    judged = 0
    quote_ok = quote_bad = quote_missing = 0
    for row in rows:
        for verdict in row["verdicts"]:
            judged += 1
            counts[verdict["state"]] = counts.get(verdict["state"], 0) + 1
            if verdict["state"] != "supported":
                continue
            if not verdict["quote"]:
                quote_missing += 1
            elif quote_at(verdict["quote"], row["articles"]) is not None:
                quote_ok += 1
            else:
                quote_bad += 1
    supported = counts["supported"]
    return {
        "trees": len(rows),
        "total_nodes": total_nodes,
        "judged": judged,
        "coverage": round(judged / total_nodes, 3) if total_nodes else 0.0,
        **counts,
        "share": {state: round(counts[state] / judged, 3) if judged else 0.0
                  for state in STATES},
        "quote_verbatim": quote_ok,
        "quote_not_in_text": quote_bad,
        "quote_missing": quote_missing,
        "quote_verbatim_rate": round(quote_ok / supported, 3) if supported else None,
        "invalid_items": sum(row.get("invalid", 0) for row in rows),
        "seconds": round(sum(row.get("seconds", 0.0) for row in rows), 2),
    }


def _table(rows: Sequence[dict[str, Any]]) -> str:
    """每张图一行——三态分布是**逐图**才看得出问题的（有的题网上资料多、有的没有）。"""
    lines = ["| # | 树 | 节点 | 已有引用 | 判过 | supported | unverified | "
             "contradicted | 引文逐字 | 文章 | query |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for index, row in enumerate(rows, start=1):
        counts = {state: 0 for state in STATES}
        for verdict in row["verdicts"]:
            counts[verdict["state"]] = counts.get(verdict["state"], 0) + 1
        ok = sum(1 for verdict in row["verdicts"]
                 if verdict["state"] == "supported" and verdict["quote"]
                 and quote_at(verdict["quote"], row["articles"]) is not None)
        lines.append(
            f"| {index} | {row['title'][:28]} | {row['node_count']} | {row['refs']} | "
            f"{len(row['verdicts'])} | {counts['supported']} | {counts['unverified']} | "
            f"{counts['contradicted']} | {ok}/{counts['supported']} | "
            f"{len(row['articles'])} | {row['query']} |")
    return "\n".join(lines)


# ---------------------------------------------------------------- 执行


def _fill_credentials() -> None:
    """凭证从 `run-backend.bat` 来（`measure_cross_map.load_env` 已装载），这里只
    去代理：DeepSeek 的两个接口**必须直连**（过 Clash 100% TLS 失败）。"""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(name, None)
    os.environ.pop("MINDMAP_EXTERNAL_PROXY", None)


def search_once(query: str) -> list[dict[str, str]]:
    """一次 DeepSeek Search → 最多 `ARTICLES` 篇正文。**失败返回空列表**。

    只用第六个源（`external._deepseek_search`），不用六源混合：这条链要的是
    "一整段能逐字抄的正文"，而五个源里维基给条目正文、论文给 abstract（都不是
    能被引用的一整段），混进来只会让"引文在不在正文里"这个判据失去意义。
    """
    from app.knowledge import external

    budget = external._Budget(3, 30.0)
    hits = external._deepseek_search(query, budget, ARTICLES)
    return [{"title": hit.title[:120], "site": hit.site or hit.source_name,
             "text": hit.text, "url": hit.url} for hit in hits if (hit.text or "").strip()]


async def judge_tree(row: dict[str, Any]) -> dict[str, Any]:
    """一棵树：拼 query → 搜一次 → 按 `JUDGE_MAX_NODES` 一块判。"""
    from app.generation.service import chat_async

    content = row["content"]
    branches = [node.label for node in content.root.children]
    query = build_query(row["title"], branches)
    row["query"] = query

    started = time.perf_counter()
    articles = await asyncio.to_thread(search_once, query)
    row["articles"] = articles
    row["search_seconds"] = round(time.perf_counter() - started, 2)
    rows = node_rows(content)
    row["rows"] = rows
    if not articles:
        row["verdicts"] = []
        row["invalid"] = 0
        row["seconds"] = row["search_seconds"]
        return row

    verdicts: list[dict[str, Any]] = []
    invalid = 0
    for start in range(0, len(rows), JUDGE_MAX_NODES):
        chunk = rows[start:start + JUDGE_MAX_NODES]
        system, user = judge_messages(row["title"], chunk, articles)
        try:
            response = await chat_async(system, user, max_tokens=8192)
            parsed, rejected = parse_verdicts(
                _payload_of(response), [item["id"] for item in chunk])
        except Exception as exc:  # noqa: BLE001 — 一块判不出来算它漏判，不废整棵树
            logger.warning("判定调用失败（%s）：%s", type(exc).__name__, str(exc)[:160])
            parsed, rejected = [], len(chunk)
        verdicts.extend(parsed)
        invalid += rejected
    row["verdicts"] = verdicts
    row["invalid"] = invalid
    row["seconds"] = round(time.perf_counter() - started, 2)
    return row


async def run(trees: int, reuse: bool, strict: bool) -> int:
    if reuse and OUT.exists():
        rows = json.loads(OUT.read_text(encoding="utf-8"))["trees"]
        print(f"复用 {OUT}（{len(rows)} 张）")
    else:
        maps, fixtures = cross_map.load_maps()
        picked = pick_trees(maps, trees)
        print(f"真图 {len(maps)} 张（排除夹具 {fixtures} 张）→ 同题去重后取 {len(picked)} 张")
        rows = []
        for index, item in enumerate(picked, start=1):
            print(f"[{index}/{len(picked)}] {item['title'][:30]}"
                  f"（{item['node_count']} 节点 / 已有引用 {item['refs']}）…",
                  file=sys.stderr, flush=True)
            row = await judge_tree(item)
            row.pop("content", None)
            print(f"    query={row.get('query')!r} 文章 {len(row.get('articles') or [])} 篇，"
                  f"判 {len(row.get('verdicts') or [])} 条，{row.get('seconds')}s",
                  file=sys.stderr, flush=True)
            rows.append(row)
        OUT.write_text(json.dumps({"trees": rows}, ensure_ascii=False, indent=1),
                       encoding="utf-8")

    for row in rows:
        row.setdefault("query", "")
        row.setdefault("articles", [])
        row.setdefault("verdicts", [])
        row.setdefault("rows", [])
    print()
    print(_table(rows))
    summary = summarise(rows)
    print()
    print(f"- 树 {summary['trees']} 张、节点 {summary['total_nodes']} 个、"
          f"判过 {summary['judged']} 个（覆盖 {summary['coverage']:.0%}）")
    print(f"- 三态：supported {summary['supported']}（{summary['share']['supported']:.0%}）、"
          f"unverified {summary['unverified']}（{summary['share']['unverified']:.0%}）、"
          f"contradicted {summary['contradicted']}"
          f"（{summary['share']['contradicted']:.0%}）")
    print(f"- supported 的引文：逐字在正文里 {summary['quote_verbatim']} 条"
          f"（{summary['quote_verbatim_rate']}）、不在 {summary['quote_not_in_text']} 条、"
          f"没给引文 {summary['quote_missing']} 条；无效/漏判 {summary['invalid_items']} 条")
    print(f"- 耗时合计 {summary['seconds']}s")

    overlay = _overlay(rows)
    print()
    print('## supported 里"真的被支持"还是"只是同话题"（零成本旁证）')
    print(f"- 引文覆盖节点标签二元组 ≥50%：{overlay['covered']} 条 / "
          f"{overlay['supported']} 条 supported（{overlay['covered_rate']}）")
    print(f"- 引文几乎没带节点标签的词（<20%）：{overlay['loose']} 条")
    _mismatches(rows)
    if strict:
        await _strict(rows)
    OUT.write_text(json.dumps({"trees": rows}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    return 0


def _overlay(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    covered = loose = supported = 0
    for row in rows:
        for verdict in row["verdicts"]:
            if verdict["state"] != "supported":
                continue
            supported += 1
            label = _label_of(row, verdict["id"])
            score = label_in(verdict["quote"], label)
            verdict["label_in_quote"] = round(score, 2)
            if score >= 0.5:
                covered += 1
            if score < 0.2:
                loose += 1
    return {"supported": supported, "covered": covered, "loose": loose,
            "covered_rate": round(covered / supported, 3) if supported else None}


def _label_of(row: dict[str, Any], node_id: str) -> str:
    for item in row.get("rows") or []:
        if item["id"] == node_id:
            return item["label"]
    return ""


def _mismatches(rows: Sequence[dict[str, Any]], limit: int = 12) -> None:
    """逐条列出"判了 supported、但引文在正文里逐字找不到"的那几条。

    这一栏是"模型编了一句像原文的话"的直接证据，也是唯一能分辨它的地方：
    `quote_at` 只回答在不在，回答不了"它是怎么来的"。所以每条再给两个旁证——
    引文开头 12 字在正文里找不找得到（找得到 = 它改了几个字/拼了两段），
    以及引文与所指那篇正文的最长公共前缀长度。
    """
    bad: list[dict[str, Any]] = []
    for row in rows:
        for verdict in row["verdicts"]:
            if verdict["state"] != "supported" or not verdict["quote"]:
                continue
            if quote_at(verdict["quote"], row["articles"]) is not None:
                continue
            head = _WS.sub("", verdict["quote"])[:12]
            anywhere = any(head in _WS.sub("", article["text"])
                           for article in row["articles"])
            bad.append({"tree": row["title"], "label": _label_of(row, verdict["id"]),
                        "quote": verdict["quote"], "head_anywhere": anywhere})
    if not bad:
        return
    print(f"- 引文逐字对不上的 {len(bad)} 条（下面按原文核过）：")
    print()
    print("| 树 | 节点 | 模型给的引文 | 开头 12 字在正文里 |")
    print("|---|---|---|---|")
    for item in bad[:limit]:
        print(f"| {item['tree'][:16]} | {item['label'][:14]} | "
              f"{item['quote'][:50]} | {'在' if item['head_anywhere'] else '不在'} |")
    if len(bad) > limit:
        print(f"| … | 另 {len(bad) - limit} 条 | | |")


STRICT_SYSTEM = (
    "你在核一对（节点, 引文）。只判断这**一段引文本身**是否在陈述这个节点的内容，"
    "不要管它出自哪篇文章、也不要替它补充上下文。\n"
    "三选一：\n"
    "- entails：这段引文本身就在说这个节点讲的事（可以略有出入，但要点一致）。\n"
    "- same_topic：引文在聊同一个话题/同一个领域，但没有陈述这个节点的那句话。\n"
    "- unrelated：引文与这个节点无关，或者意思相反。\n"
    '只回 JSON：{"verdict":"entails|same_topic|unrelated"}'
)


async def _strict(rows: Sequence[dict[str, Any]]) -> None:
    """把 supported 逐条再问一次（节点 + 引文，不给文章）。这就是自评那一栏。"""
    from app.generation.service import chat_async, parse_model_json

    async def one(row: dict[str, Any], verdict: dict[str, Any]) -> str:
        label = _label_of(row, verdict["id"])
        detail = next((item["detail"] for item in (row.get("rows") or [])
                       if item["id"] == verdict["id"]), "")
        user = f"节点：{label}\n说明：{detail}\n\n引文：{verdict['quote']}"
        try:
            response = await chat_async(STRICT_SYSTEM, user, max_tokens=64)
            raw = _payload_of(response)
            payload = raw if isinstance(raw, (dict, list)) else parse_model_json(raw)
            return str((payload or {}).get("verdict") or "").strip().lower()
        except Exception:  # noqa: BLE001 — 判不出来算它没判，不写进任何一档
            return ""

    counts = {"entails": 0, "same_topic": 0, "unrelated": 0}
    samples: list[dict[str, Any]] = []
    limit = asyncio.Semaphore(8)
    tasks: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        for verdict in row["verdicts"]:
            if verdict["state"] == "supported" and verdict["quote"]:
                tasks.append((row, verdict))

    async def run(item: tuple[dict[str, Any], dict[str, Any]]) -> None:
        row, verdict = item
        async with limit:
            kind = await one(row, verdict)
        verdict["strict"] = kind
        if kind in counts:
            counts[kind] += 1
        if len(samples) < 14:
            samples.append({"tree": row["title"], "label": _label_of(row, verdict["id"]),
                            "quote": verdict["quote"], "strict": kind,
                            "label_in_quote": verdict.get("label_in_quote")})

    await asyncio.gather(*(run(item) for item in tasks))
    total = sum(counts.values())
    print(f"- 严格复核（节点 + 引文，不看文章）：entails {counts['entails']}、"
          f"same_topic {counts['same_topic']}、unrelated {counts['unrelated']}"
          f"（共 {total} 条；entails 占 "
          f"{round(counts['entails'] / total, 3) if total else 0}）")
    print()
    print("| 树 | 节点 | 引文 | 严格判定 | 标签覆盖 |")
    print("|---|---|---|---|---|")
    for sample in samples:
        print(f"| {sample['tree'][:18]} | {sample['label'][:16]} | "
              f"{sample['quote'][:44]} | {sample['strict']} | {sample['label_in_quote']} |")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trees", type=int, default=10, help="取前几张真图")
    parser.add_argument("--dry-run", action="store_true", help="只挑样本与拼 query")
    parser.add_argument("--reuse", action="store_true", help="复用落盘结果，只重算统计")
    parser.add_argument("--strict", action="store_true",
                        help="对 supported 逐条再问一次（节点 + 引文）")
    args = parser.parse_args()

    _fill_credentials()
    if args.dry_run:
        maps, fixtures = cross_map.load_maps()
        picked = pick_trees(maps, args.trees)
        print(f"真图 {len(maps)} 张（夹具 {fixtures}）→ 取 {len(picked)} 张")
        for item in picked:
            branches = [node.label for node in item["content"].root.children]
            print(f"- {item['title'][:30]}"
                  f"（{item['node_count']} 节点 / 已有引用 {item['refs']}）")
            print(f"  query: {build_query(item['title'], branches)!r}")
        return 0
    return asyncio.run(run(args.trees, args.reuse, args.strict))


if __name__ == "__main__":
    raise SystemExit(main())
