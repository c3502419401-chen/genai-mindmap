"""外部知识源：把百科与论文的检索结果转成 `Source`。

## 外部源清单（2026-11 实测，逐个的可抓性报告见 `_baike` / `_openalex` 的 docstring）

接了五个：中文维基（`wikipedia`，web）、快懂百科（`baike`，web）、arXiv（`arxiv`，
paper）、OpenAlex（`openalex`，paper）、Crossref（`crossref`，paper）。

还有一个**默认关**的第六个：DeepSeek 的 Search 工具 API（`deepseek`，web，见
"DeepSeek Search / Fetch"一节）。它与上面五个**并列而不是替换**，理由是三件事：

  - **强项不同**。中文维基对"人物 / 事件 / 这个名词是什么"更强（它给的是词条正文，
    有定义、有沿革）；Search 对"技术方向 / 流程 / 方案 / 现状"更强（它一次就带回
    带正文的站点页，实测中文主题 0.5–1.0 秒、3 条、正文 1.5k–21k 字）。用 Search
    替换维基，`碳中和` 这类主题会拿到"某咨询公司的双碳方案 PPT"，而不是词条。
  - **可用性不同**。五个源要么靠代理（维基 / arXiv / OpenAlex / Crossref），要么在
    代理下 100% 失败（快懂百科）；Search 走国内直连、不吃代理。反过来，Search 要
    key、要计费（0.005 元/次）、把 query 发到第三方——五源不要钱、不出境。
  - **失败面不同**。五源全挂是"代理没开"这一种故障；Search 挂是"没 key / 额度没了 /
    接口改了"另一种。两条链路同时挂的概率远低于各自挂的概率，而这正是"失败即空"
    这条契约要的冗余。

所以默认关：它是**可选的第六个**，不是新版默认行为。开关与判断见
`DEEPSEEK_SEARCH_ENV`。

**没接的那些，理由都是"过不去"而不是"没试"**：

  - 百度百科：`robots.txt` 的 `User-agent: *` 组是 `Disallow: /`（全站拒绝），
    词条页对正常请求返回的是 `window.__PARIS_CHALLENGE_START__` 的 JS 挑战页
    （4.7 KB、零正文容器），要拿正文得执行它的挑战脚本——那是绕过，不做。
  - 知乎：`robots.txt` 的 `User-agent: *` 组同样是 `Disallow: /`（只 `Allow:
    /tardis/jm`），且 `/search` 对**每一个**具名 bot 都显式 `Disallow`。
    `/search?q=…` 回 403、`/api/v4/search_v3` 回 403 + `{"error":{"code":40362}}`、
    `/api/v4/search` 回 403 + `{"error":{"need_login":true,"code":40352}}`。
  - Semantic Scholar：连续三次实测都是 429（无 key 时全 IP 共享池），不稳定，
    不能作为生成链路的依赖。
  - Stack Exchange：能通（200 + gzip），但**中文 query 返回跑题噪声**
    （`q=碳中和` 命中"Segmentation fault while using a C module"），与主题无关。
  - 萌娘百科：Cloudflare 挑战页（403 + `Just a moment...`），要过挑战才能读。
  - 搜狗百科：`567` + 7.3 KB 拦截页；360 百科：域名已不解析。
  - Wikidata：有中文 label/description，但没有正文，对生成无用。

## 为什么是"转成 Source"而不是另造一套命中

引用逐字校验（`optimization/source_refs`）、父块坐标、"这条引用落在可见父块里吗"、
`library.load()` 回查、前端"来源引用"面板——这五件事都已经只认 `Source`。外部结果
只要变成 `Source` 并 `library.ingest`，它就走完整条既有的 RAG 链路，不需要第二套
hit 形状、第二套引用格式、第二套校验。所以这个模块只做三件事：**抓取、清洗、转换**。
它不碰切块、不碰打分、不碰引用校验。

## 外部内容是**不可信数据**

`text` 里可能有人写"忽略上面的要求"、可能有 HTML、可能五万字。三条规矩：

  - 标签剥掉（`clean_text`），HTML 实体反转义一次；
  - 单条正文截到 `TEXT_MAX_CHARS`，抓取时的原始长度记在
    `Source.metadata.raw_chars`——不记的话，"模型只看到开头 8000 字"与"这条本来就
    只有 8000 字"在界面上长得一模一样；
  - 提示词侧另有一句"这些是外部抓取的内容，只当资料用"，见
    `generation.service.EXTERNAL_TRUST_RULES`。本模块不负责教育模型。

**本模块永不执行、永不请求外部内容里出现的任何 URL**：`url` 只作为数据存进
`Source.metadata.url`，供界面渲染成链接；代码里没有任何一处拿它再发请求。
`contracts.source.safe_http_url` 另外把非 http/https 的 scheme 折成 None——前端会把
这个值塞进 `<a href>`，`javascript:` 进来就是一个 XSS 面。

## 失败即空

单请求超时 10 秒、不重试、任何异常都吞掉并返回已经拿到的部分。外部源挂了不能影响
生成：调用方（`agent.tools._generate`）拿不到结果就退回"只凭主题"的那条路。总预算
（`REQUEST_LIMIT` 次请求 / `DEADLINE_SECONDS` 秒）是硬的，理由见 `_Budget`。

## 同一 query 24 小时内只打一次网

`search_external` 的最终产物会按**归一化 query** 存进 `external_cache`（同库另一张
表），24 小时内同一个 query 再来就不发一个 HTTP 请求。三个边界决定了它长成这样：

  - **缓存的是 `ExternalHit` 的字段，不是 `Source`**：`Source.id` 每次入库都是新的，
    存下来下次也用不上；真正稳定的是 title/text/url/source_name/language/raw_chars。
    命中之后转 `Source`、`library.ingest`、检索、引用校验**全部照常走**——入库那一步
    会因 `content_hash` 判重变成 no-op，这是对的（缓存省的是再搜一次，不是再入库一次）。
  - **失败不进缓存，"0 条"进**：外网不通是暂时的，缓存住它会让"网络恢复了但一天没
    结果"；而"搜到了 0 条"是个有效结论，重搜一遍仍然是 0 条（`_worth_caching`）。
  - **注入方向时不碰缓存**：`directions is not None` 是测量脚本与用例的出口，它们要的
    是真实耗时与真实候选池（理由见 `search_external`）。

命中时仍走完整条后续链路，只是跳过 HTTP；`data.external.cached` 把"这次是缓存命中
还是真搜"报给用户看（`ExternalHits.cached`）。`hit_count` 那个同样的问题
（"这个主题被搜过几次"）留给缓存表自己回答。

## 多方向检索：查一遍 = 一个方面，短主题要的是几个方面

`碳中和` 这样的主题，用户期待的是"政策 / 技术路径 / 企业案例"这类**多面覆盖**，而
一条 query 只能打回一个方面。所以检索前先拆方向（`plan_directions`，**一次** LLM
调用），每个方向各打一轮，合并去重后按方向轮转取样：

    主题 → 2-4 个方向 → 每方向并行打源（各 2 条）→ URL 去重
         → 按**方向**各自过词面门 → 方向内按源交替 → 跨方向轮转 → 取 limit 条

**这条路默认关**（`DIRECTIONS_ENV`），所以它保留着改动之前的取样实现（`_interleave`
/ `_interleave_directions`），而单方向那条路已经换成打分排序（见下面"取数策略"
一节）。两条路的选择器不同是**刻意的**：方向路径的每个方向只有 5 次请求预算、每个
方向回来的候选只有 1-2 条，在那种规模上"排序"与"轮转"没有区别；而它的实测结论
（负收益）也不因为换了选择器而改变。

三条约束是这个形状存在的前提，改动它们等于取消这个功能：

  - **门按方向施加而不是按主题**（`_relevant_hits(direction, hits)`）。合并后的池子
    里一条"讲碳中和但不讲政策"的命中，对方向「碳中和政策」就是噪声；拿主题去判它
    反而放行。拆方向如果不动门，门就会从"判一条命中是不是这个 query 的东西"退化成
    "判它是不是这个话题的东西"——粗一档，等于白拆。
  - **跨方向轮转取样**（`_interleave_directions`）而不是直接 `_interleave`：后者按
    **源**分桶，三个方向都命中维基时，维基桶的前 3 条会占满名额，很可能全来自方向 1
    ——拆了三个方向，返回的还是同一个方面。方向内仍按源交替（`_interleave`），两层
    交替各管一件事：方向内保源多样、跨方向保方面覆盖。
  - **降级必须是一个方向**（`[topic]`）。拆方向的 LLM 调用失败/超时/没配置时，走的是
    与 `3c40ba6` 完全相同的单方向路径（5 源、9 次请求 / 20 秒），不是"少拆一点"。

预算分两层（`_Budget.take(slot)`）：全局 `MULTI_REQUEST_LIMIT`=15 次 / 20 秒；每个
方向另有 `DIRECTION_REQUEST_LIMIT`=5 次的硬上限，3 方向 × 5 = 15 与全局正好对齐。
每方向的请求数由"维基 1 次条目检索 + 1 次抓正文 + 三个论文源各 1 次"构成，所以
**方向路径跳过快懂百科**：它只做精确词条命中（`/search` 被 robots 禁），而方向是
「碳中和政策」这类组合短语，在它那里必然是 404——那一次请求是纯浪费，还会挤掉一个
真能出结果的源。

## DeepSeek Search：三个判断

**`lang:zh` 写在 query 里，不是一个请求体字段**（实测：请求体加 `"lang": "zh"` 被
静默忽略，不带 `lang:` 的 `机器学习的主要方向` 返回 3 条英文站）。判据复用
`_query_language`——也就是"中文 query 时让中文桶先占名额"的同一份判断。两处必须
同源：源侧过滤中文、排序侧偏好中文是同一件事，各写一份迟早分叉成"上游把中文滤掉、
下游还在等中文命中"。

**正文截到 `DEEPSEEK_TEXT_MAX_CHARS`（4000）**，而不是 `TEXT_MAX_CHARS`（8000）。
一条 21470 字的网页按 300 字/子块切是 71 个 leaf chunk，五源里最长的一条（维基
"碳中和" 2704 字）只有 9 个；4000 字是 13 个，与它同量级。切块数决定检索池的
投票权，而"这条资料值不值得 21470 字"这件事我们没有证据支持（复现：`scripts/
measure_external_deepseek.py --truncation`）。截断前的长度照旧进 `raw_chars`。

**不再独占名额**（原先 `_blend_deepseek` 给它 1 席）。那条规则与"每源保底"是同一个
病：用源的配额代替相关性判据。它今天还害了一次——五源全挂时它抓回的是教育平台课程页
（正文前 300 字里"机器学习"出现 5 次，全在课程名与页头导航），而"给 Search 1 席"
保证了这条会进材料。现在六个源在同一个分数上排队，Search 的 `rerank_score` 只做同档
内的微调（系数 0.25），不再能靠"有分数"顶掉五源。

## 取数策略：打分 → 排序 → 去重 → 多样性（`limit` 的默认值由此而定）

    六源候选（不去重、不截断）
      → score_hit（标题分档 0.5 + 前 300 字次数 0.3 + 全文次数 0.2，再乘 rerank 系数）
      → 降序 → 内容去重（Jaccard ≥0.60 只留一条）→ MMR（与已选内容相似就扣分）
      → 取前 limit 条

**没有按源的配额**，既没有保底也没有封顶。理由全是读数（7 个主题、256 条候选、
人工判定，脚本 `scripts/measure_external_rank.py`）：词面门 + 每源交替在 `limit=3`
上给 14 相关 / 6 噪声，打分排序给 17 / 2；`limit=8` 上是 20 / 20 对 42 / 10。而
"每源封顶"（每源最多 `ceil(limit/2)`）在同一份池子上是**负收益**：`limit=8` 的相关
从 42 掉到 36、噪声从 10 涨到 14——openalex/crossref 恰是某些主题唯一的相关来源。

`DEFAULT_LIMIT` 因此从 3 提到 8：判据分得开时，提高 limit 拿到的是更多相关材料
（0.3 条相关/主题 → 6 条/主题），而"3 个名额"本身就在扔东西（`城市雨污分流改造`
一题 8 条候选全相关，3 个名额只能给 3 条）。完整读数与复现命令见
`docs/EXTERNAL-SELECTION.md`。

## 代理

外网直连在本机不通（实测：`zh.wikipedia.org` 连不上、`export.arxiv.org` 的 https
读超时），所以代理是这条链路的前置条件，不是可选优化。默认值就是 Clash Verge 的
`127.0.0.1:7897`；`MINDMAP_EXTERNAL_PROXY=none` 是显式直连（给"机器上本来就有全局
透明代理"那种环境留的出口）。

**DeepSeek 的两个接口不走代理**（`_deepseek_client`，默认直连）。这是实测之后必须
改的一处：`api.deepseek.com` 是国内端点，直连 0.47–1.13 秒；而经本机 Clash
（`127.0.0.1:7897`）时 TLS 握手直接被掐——`SSL: UNEXPECTED_EOF_WHILE_READING`，
两个接口 100% 失败。要给它单独指定出口时用 `MINDMAP_DEEPSEEK_TOOLS_PROXY`，
缺省 `none`（直连）；给它是给"机器在海外、必须走代理才能到国内"那种环境留的出口。
**这不是"给一个源单独一套网络策略"那种坑**：五个源共用代理是因为实测它们要代理才
通，DeepSeek 反过来——共用代理等于让它永远失败。

**国内源也走同一个代理**，这是实测之后的选择而不是偷懒：`baike.com`（字节的
`volc-dcdn`）走代理 1.1 秒、直连 7.5 秒，反而代理更快。反过来 `baike.baidu.com`
走代理直接被掐（TLS 握手 `UNEXPECTED_EOF` / 502），但那个站本来就不接（见上）。
一个 `_client()` 覆盖全部源，比给每个源配一份代理策略少一处会配错的地方。

`trust_env=False` 是必须的：本机 `ProxyEnable=0`（系统代理开关是关的），让 httpx
去读环境变量会拿到空值然后直连超时——那是最难解释的一种"外部源全挂"。
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Sequence
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

from app.contracts.source import ISO_SECONDS_FORMAT
# 模型出口与 JSON 解析**借用同一份实现**（`query_plan` 也是这么用的）：`parse_model_json`
# 已经吃过"代码围栏 / 前后多写字 / 括号收支错位"这三类真实故障，在这里另写一套剥围栏
# 逻辑等于把那些故障的两套判定带到两个模块里。
from app.generation.service import chat_async, parse_model_json
# 缓存层只认 dict，不认 `ExternalHit`——所以它可以被这里 import 而不反向依赖本模块
# （`external_cache` 不 import `external`，两者没有环）。命中/回写时的形状校验在
# `_record_hit` / `_hit_record` 里，那两个函数是"缓存 ↔ 命中"的唯一转接点。
from app.knowledge import external_cache
# 私有名跨模块引用：分词这件事已经有实现（`_token_list`），门必须与检索用同一套
# "什么算同一个词"，另写一份就是两处真相。模块级导入而不是函数内懒导入——两者
# 之间没有循环（`service` 不引用 `external`），懒导入只会把导入期错误推后。
from app.knowledge.service import _token_list

logger = logging.getLogger(__name__)

# 代理。默认值对着本机的 Clash Verge（`mixed-port: 7897`）。
EXTERNAL_PROXY_ENV = "MINDMAP_EXTERNAL_PROXY"
DEFAULT_PROXY = "http://127.0.0.1:7897"

# 单请求超时。10 秒是"比一条普通 HTTP 往返长得多、又不至于让用户以为卡死"的量级：
# 实测维基 1.1–1.4s、arXiv 0.7–4.0s，而代理节点抖动时会出现 25s 以上的读超时——
# 那种时刻的重试没有意义（同一个节点、同一个目标），所以要的是快速放弃。
REQUEST_TIMEOUT = 10.0

# 一次检索最多拿几条（跨源合计）。**3 → 8**，理由只有一条：判据现在能把相关与
# 不相关分开（`_select_hits` 的打分把噪声率从 43% 压到 18%），所以提高 limit 拿到的是
# 更多**相关**材料，而不是更多垃圾。同一份池子上的读数：`limit=3` → 17 条相关 / 2 条
# 噪声，`limit=8` → 42 / 10，`limit=12` → 59 / 17；固定 3 条时，那 17 条相关里有 14 条
# 是**因为名额不够被扔掉的**（`城市雨污分流改造` 一题 8 条全相关，3 个名额只能给 3 条）。
#
# 8 而不是 12 或更大：12 条材料把骨架那次调用的 prompt 从 ~5k 字推到 ~9k 字，而相关
# 条数只多 4 条（`机器学习` 从 4/12 到 4/12 不动，「垃圾主题」拿到的是噪声）；而 8 是
# "材料厚度够了、清单还没撑满"的那个点——材料清单每行约 30 字、`INVENTORY_MAX_LINES`
# 是 20 行，8 份占 240 字，把 10 份塞满也就 300 字，**清单不再是 limit 的约束**。
# 另外它等于界面"最多 8 条一级分支"那个上限，一次外部检索最多能支撑满一支图。
DEFAULT_LIMIT = 8

# 整次外部检索的总预算。两个数字一起构成"最坏情况下用户等多久"：
#   - `DEADLINE_SECONDS`：整次调用的墙钟上限，20 秒后任何新请求都不再发；
#   - `_request_limit(limit)`：请求次数上限，**随 `limit` 线性涨的两项**：
#       五源固定 5 次（维基精确标题 1 + 条目检索 1 + arXiv / OpenAlex / Crossref /
#       快懂百科各 1）+ 维基逐条抓 `limit` 次 + DeepSeek Search 1 次 + DeepSeek 补抓
#       至多 `limit` 次，再留 1 次余量 = 7 + 2 × limit。
#     `DEFAULT_LIMIT=3` 时它等于 13，与改动之前那个常量逐字相同——**默认 limit 下的
#     请求预算没有变**，变的只是"limit 提上去时预算跟着涨"这件事（以前 `limit` 提到
#     8，维基的逐条抓取会被 13 次预算掐掉一半，而那是"某个源悄悄变薄"的静默故障）。
# 没有次数上限时，源返回一个"下一条标题"的畸形响应就能把链路拖进无界循环；给多了
# 则是"为几条候选把预算烧光"。
DEADLINE_SECONDS = 20.0
# 默认 limit 下的请求预算。**它现在是一个计算结果**，不是一个独立常量：把它写成
# 常量而让 `limit` 浮动，就是"改一个数忘了改另一个"的那类故障。
REQUEST_LIMIT = 7 + 2 * DEFAULT_LIMIT


def _request_limit(limit: int) -> int:
    """一次单方向检索的请求次数上界（构成见 `REQUEST_LIMIT` 上面那段）。"""
    return 7 + 2 * max(int(limit), 0)

# 多方向检索的请求预算。15 = 3 个方向 × 5 次（每方向的次数构成见 `DIRECTION_SOURCES`
# 与 `_wikipedia` 的 `exact_first` / `fetch_limit`）。**墙钟不放宽**：`DEADLINE_SECONDS`
# 仍然是 20 秒，三个方向的源请求并行跑，所以"多了两轮检索"不会把等待变成三倍。
MULTI_REQUEST_LIMIT = 15
# 每方向的请求上限。它与 `MAX_DIRECTIONS` 相乘必须等于 `MULTI_REQUEST_LIMIT`：
# 3 × 5 = 15。改其中一个而不改另一个，预算就会在"某个方向整轮被饿死"和"全局还有
# 余量却没人能用"之间二选一——这两种都在实测里表现为"偶尔少一个方面的结果"。
DIRECTION_REQUEST_LIMIT = 5
# 默认最多拆几个方向。提示词要 2-4 个（给模型一点选择余地），代码取前
# `max_directions` 个：多一个方向就多一条"名额被摊薄"的路径，而最终只要 3 条。
MAX_DIRECTIONS = 3
# 每方向最多留几条候选。**不是每方向 3 条**：3 方向 × 3 = 9 条候选去争最终 3 个名额，
# 等于把名额浪费在"同方向的三条"上。2 是"一个方面能贡献两个视角"与"别挤掉别的方面"
# 之间的取值。
DIRECTION_HIT_LIMIT = 2
# 方向路径里维基只抓 1 条正文。理由同上：第 2 条正文的边际价值低于"别的源还有一次
# 请求额度"，而维基的逐条抓取是这条链上唯一会随 `limit` 线性涨请求数的地方。
DIRECTION_WIKI_FETCH = 1
# 方向路径的源清单：**跳过 baike**（精确词条命中，组合短语必然 404），见模块 docstring。
#
# **也不含 deepseek**，虽然它对"组合短语"是强项。理由是预算而不是效果：每个方向只有
# `DIRECTION_REQUEST_LIMIT=5` 次额度，五个源已经把它顶满（维基 2 + 三个论文源各 1），
# 再加 Search 就得从某个论文源手里抢走一次。而整条方向路径默认关（实测负收益，见
# `DIRECTIONS_ENV`），为一条关着的路径重新分配预算，调完也没有人跑。
DIRECTION_SOURCES: tuple[str, ...] = ("wikipedia", "openalex", "crossref", "arxiv")

# 拆方向的 LLM 调用：单次、8 秒、不重试。与 `query_plan.TIMEOUT_SECONDS` 同一个量级，
# 但单独一个名字——两条路径的预算将来可能不同，共用一个常量会把它们绑死。
DIRECTION_TIMEOUT_SECONDS = 8.0
# 2-4 个方向、每个十几个字 + JSON 结构，中文约 80 token。给 300 是留足余量：给大了
# 只会让慢模型更慢，而预算只有 8 秒。
DIRECTION_MAX_OUTPUT_TOKENS = 300
# 方向的长度区间。提示词里要的是 6-20 字（写得具体一点才检索得出新东西），但**代码闸
# 的下限放到 4**：实测合法方向的字数是「碳中和政策」5、「碳交易市场」5、「Rust 内存模型」
# 9——6 字下限会把它们整批打掉，而那正是最自然的形状（主题原词 + 两个字的限定语）。
#
# **单位是"折算字数"而不是字符数**：英文主题的方向必然是英文（模型对 `transformer
# architecture` 给的是 `transformer architecture 原理`，27 个字符），按字符数上限 20
# 会把英文主题整批判死——实测三个英文主题全部降级成单方向就是这个原因。折算是
# 1 个中文汉字 = 1，1 个英文词 = 2（一个英文词的检索价值大致相当于两个汉字）。
DIRECTION_MIN_CHARS = 4
DIRECTION_MAX_CHARS = 20
# 纯英文方向的独立闸：2-8 个词、总长度 ≤ 60 字符。`Byzantine fault tolerance threat
# models` 是 5 个词 39 字符，合法；而一段整句会在词数上暴露。
DIRECTION_MIN_WORDS = 2
DIRECTION_MAX_WORDS = 8
DIRECTION_MAX_ASCII_CHARS = 60
# 方向里的"英文词"：字母开头，允许 `C++` / `C#` / `node.js` 这类带符号的写法。
_DIRECTION_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#._-]*")
# 模型一次最多给几个方向。提示词写 2-4，这里是它的代码执行处：多给的部分直接截掉，
# 而不是"多跑几个"。截到 4（提示词的上界）后再由 `max_directions` 收一次。
DIRECTION_MAX_ITEMS = 4

# 拆方向的开关。`MINDMAP_EXTERNAL_DIRECTIONS` **默认关**——量过了，是负收益：
# 同一时段、同一出口、同一批源的两份池子（`scripts/measure_external_relevance.py
# --collect-single` / `--collect-directions`，20 主题）上，三条全相关 13/20 → 6/20、
# 成功率 95% → 50%、耗时中位 4.51 → 6.65 秒，噪声 8 → 5 只是因为返回从 43 条掉到
# 21 条。三处原因都不是"再调调"能消掉的：
#
#   - **限定语闸把有料的主题砍空**：`幻觉` 的方向池里有 4 条相关候选，过门后 0 条；
#     `内卷` 2 条相关 → 0 条。方向以主题原词开头时，`_phrase_terms(direction)` 退化成
#     主题的判定词块，必须靠限定语补第二刀，而限定语在这批源站的摘要里经常不出现。
#   - **单条命中本来就够**：20 个主题里有 10 个，单方向路径回来那条维基正文的前 300 字
#     就已经含 2-3 个方向限定语（`注意力机制` 三条全中、`最终一致性` 三条全中）。
#     拆方向是在把一份已经多面的材料切成单面的几份。
#   - **每方向预算太薄**：15 次 / 3 方向 = 每源一次机会，任何一个源 429 就丢掉该方向的
#     一整个源；单方向路径有 9 次预算、维基能抓 3 条，容错高得多。实测方向池里"至少
#     1 条相关候选"的主题只有 12/20，而单方向池是 16/20——池子本身就差一档。
#
# 保留实现而不是删掉，是因为它把"为什么不行"变成了可复现的东西：打开开关、跑
# `--collect-directions` / `--directions`，上面每个数字都能重算。默认关保证
# `3c40ba6` 的行为逐字节不变（`_search_single` 就是它）。
DIRECTIONS_ENV = "MINDMAP_EXTERNAL_DIRECTIONS"
_TRUTHY = {"1", "true", "yes", "on", "y", "t"}

# 单条正文的入库上限。8000 字大约是中文维基一个条目正文的两倍、arXiv 摘要的十几倍：
# 再长的部分进不了 prompt（父块 450–700 字，一次检索最多给 5 条），留着只是占库。
TEXT_MAX_CHARS = 8000



# 查询词的长度上限。用户可能整段贴进来；维基的 `titles=` 是一次精确查找、arXiv 的
# `all:` 是一条检索式，两者在 50 字以上都只会返回噪声或空。
QUERY_MAX_CHARS = 50

WIKIPEDIA_API = "https://zh.wikipedia.org/w/api.php"
ARXIV_API = "http://export.arxiv.org/api/query"
OPENALEX_API = "https://api.openalex.org/works"
CROSSREF_API = "https://api.crossref.org/works"
# 快懂百科（原互动百科）。**路径是 `/wiki/<词条名>`，不是站内搜索**：它的
# `robots.txt` 明确 `Disallow: /search`，而 `/wiki/*` 在 `User-agent: *` 组下是
# `Allow: /`。所以这个源只能做精确词条命中，拼不出候选（详见 `_baike`）。
BAIKE_WIKI_URL = "https://www.baike.com/wiki/"

# ---------------------------------------------------------------- DeepSeek 工具 API

# 两个端点。基址默认 `https://api.deepseek.com/v1`（实测 `/v1/tools/search` 与
# `/v1/tools/fetch` 都在它下面）。
DEEPSEEK_DEFAULT_BASE = "https://api.deepseek.com/v1"
DEEPSEEK_SEARCH_PATH = "/tools/search"
DEEPSEEK_FETCH_PATH = "/tools/fetch"
# 凭据：**先看专用的那个，再回落到主模型那条路**。理由很实际——本仓的主模型
# 端点/密钥（页面「模型设置」里保存的值，或没保存过时的 `MINDMAP_LLM_BASE_URL` /
# `MINDMAP_LLM_API_KEY`）通常就是指向 DeepSeek 的那一对，让用户把同一个 key 抄两遍
# 只会多一处会过期的地方；而真的换供应商时（LLM 走别家、tools 仍用 DeepSeek），
# 专用变量是唯一的出口。回落顺序的唯一实现在 `_deepseek_config()`。
DEEPSEEK_KEY_ENV = "MINDMAP_DEEPSEEK_TOOLS_API_KEY"
DEEPSEEK_BASE_ENV = "MINDMAP_DEEPSEEK_TOOLS_BASE_URL"
# 它自己的出口。缺省**直连**（`none`），理由见模块 docstring 的"代理"一节：经本机
# Clash 时 TLS 握手被掐，两个接口 100% 失败；而直连 0.5–1.1 秒。给它是给"机器在
# 海外、必须走代理才能到国内"那种环境留的出口。
DEEPSEEK_PROXY_ENV = "MINDMAP_DEEPSEEK_TOOLS_PROXY"

# 两个开关，**都默认关**。三条理由按分量排序：
#
#   1. **数据出境**。Search 把主题原文发给第三方；五源里唯一带 query 出境的是 arXiv /
#      OpenAlex / Crossref（英文学术库），而它们在默认路径上本来就发。多一路出境是
#      部署方与用户都该知情之后才发生的事，不该由一次升级替他决定。
#   2. **成本**。0.005 元/次。单看很小，但"每次生成都多搜一轮"是乘在生成次数上的。
#   3. **够用**。五源实测 97.2% 的主题能拿到 ≥1 条、中位 4.3 秒。第六个源是**改进**，
#      不是**修复**——改默认值的门槛应该比"实现做完了"高。
#
# 关着时它连 `DEFAULT_SOURCES` 都不会进（`_source_available`），所以行为与 `3c40ba6`
# 逐字节相同：不发请求、不占预算、日志里不多一行。
DEEPSEEK_SEARCH_ENV = "MINDMAP_EXTERNAL_DEEPSEEK_SEARCH"
DEEPSEEK_FETCH_ENV = "MINDMAP_EXTERNAL_DEEPSEEK_FETCH"

# 单次调用的超时。8 秒：实测 0.47–1.13 秒（20 个主题、每次 3 条、`need_content` 为真），
# 8 秒是"抖动 5 倍也还能回来"的量级。**不能直接用 `REQUEST_TIMEOUT=10`**：那个数
# 服务的是五源的 GET（含 GDPR 语料的自建索引），而这条是同一家公司的 JSON API，
# 它慢起来通常是排队而不是传输——多等 2 秒没有信息增益。
DEEPSEEK_TIMEOUT = 8.0

# 一条正文的入库上限。4000 而不是 `TEXT_MAX_CHARS=8000`，理由见模块 docstring 的
# "三个判断"。这个数只压 DeepSeek 的命中：五源的 `truncate_text` 仍按 8000 走，
# 改那个数会同时改变五个源的既有行为。
DEEPSEEK_TEXT_MAX_CHARS = 4000

# 每个源向服务端要几条候选。**取 `max(5, limit)`**（见 `_candidate_window`）：
# `limit=8` 时向源端要 5 条，等于让 OpenAlex / Crossref 只能贡献 5 条候选，而它们
# 恰好是某些主题（`城市雨污分流改造`、`碳中和`）唯一的相关来源——上限设在源端，
# 排序再准也拿不到池子里没有的东西。
SOURCE_CANDIDATES = 5


def _candidate_window(limit: int) -> int:
    """这次向每个源要几条候选：`max(SOURCE_CANDIDATES, limit)`。

    为什么不让它恒等于 `limit`：`limit=1/2` 那种调用（测量脚本、单条溯源）仍然该拿到
    5 条候选池——排序需要挑，1 条候选时"按相关度排"是句空话。
    """
    return max(SOURCE_CANDIDATES, max(int(limit), 0))

# 维基要求可识别的 User-Agent 并带联系方式，否则 403：
# 实测纯 `Mozilla/5.0 ...` 得到 "Please respect our robot policy"，
# 带产品名 + URL + 邮箱的规范 UA 才是 200。这不是能省的礼貌，是硬门槛。
#
# 域名与邮箱用 RFC 2606 的保留域名（`example.invalid`），因为它只是一个占位：
# 编一个真实域名等于冒充别人的联系地址。**部署时换成自己的**——维基的机器人
# 政策要的正是"出事了能找到人"。
USER_AGENT = (
    "genai-mindmap/0.1 (external knowledge retrieval; "
    "+https://example.invalid; research@example.invalid)"
)

# Crossref 的示好邮箱。多方向检索把请求密度抬到单方向的 3 倍（3 个方向各打一轮），
# 实测会撞上匿名限流：同一出口 IP 一分钟内十几次请求后开始回 429。Crossref 明确支持
# "polite pool"——把联系方式放进 UA（已经有了）**并且**放进 `mailto` 参数，限流额度
# 按这个身份算。所以这既是礼貌，也是密度上去之后能不能用的分界。
#
# **OpenAlex 不传这个参数**：实测它的 `meta.notice` 原文写着 "The mailto parameter
# doesn't raise this limit"——它只认 API key，`mailto` 一个字都不影响预算。给它加一个
# 无效参数会让下一个读代码的人以为"限流已经处理过了"，那是比没有更坏的状态。
# 它撞限流时的行为是 429 → 该源降级为空，与"源挂了"同一条路（见 `_openalex`）。
POLITE_MAILTO = "research@example.invalid"

# 响应体上限。2 MB 与 `generation.service._MAX_RESPONSE_BYTES` 同口径：外部源不是
# 我们可以信任的一端，一个畸形响应不该把内存吃满。
MAX_RESPONSE_BYTES = 2_000_000

# 标签清洗。先整段删 `<script>` / `<style>`（它们的内容不是文字），再删剩下的标签。
_BLOCK_TAG_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]{0,200}>")
_HSPACE_RE = re.compile(r"[ \t\u00a0\u3000]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
# 维基 explaintext 保留 `== 小节 ==`。转成 Markdown 标题不是装饰：切块器
# （`knowledge.service._HEADING_RE`）与材料目录（`heading_outline`）都只认
# `#{1,6} ` 这一种标题行，留着 `==` 的话这些小节在检索侧只是正文里的怪符号。
_WIKI_HEADING_RE = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.MULTILINE)

# CJK 统一表意文字。只用来判"这条正文是不是中文"，不判语种细分。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 快懂百科的词条页。两个正则各对应一处**服务端渲染**出来的结构：
#   - 词条名：`<h1 class="container-xxx doc-title-container">碳中和</h1>`
#     （`container-xxx` 带构建哈希，`doc-title-container` 是语义名，钉语义名）；
#   - 正文段落：`<div class="baike-render-paragraph">…</div>`。
#
# **不能对整页 `clean_text`**：实测"碳中和"页 307 KB 里，参考资料 JSON 的
# `"Content"` 字段藏着一整段 1427 字的今日头条转载原文，还有导航、推荐位、
# 轮播文案。整页清洗会把它们当成词条正文——引用会指向一段用户没看过的转载。
_BAIKE_TITLE_RE = re.compile(
    r'<h1[^>]*class="[^"]*doc-title-container[^"]*"[^>]*>(.*?)</h1>', re.DOTALL)
_BAIKE_PARA_RE = re.compile(r'<div class="baike-render-paragraph">(.*?)</div>', re.DOTALL)

# 段落内的标签上限 800 而不是 `_TAG_RE` 的 200：快懂百科的内链 `<a>` 带五个 class
# 加 id 加三个 data 属性，实测约 230 字符——超过 200 就匹配不上，`class=…` 会原样
# 留在正文里。这是"复用 `clean_text`"在这里会静默出错的那一类。
_BAIKE_TAG_RE = re.compile(r"<[^>]{0,800}>")

_ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class ExternalHit:
    """一条外部命中。**只是抓取的产物**，不是检索契约的一部分。

    字段与"转成 `Source` 需要什么"一一对应：`title` + `source_name` 拼文件名，
    `text` 进正文（**已按 `TEXT_MAX_CHARS` 截断**），`raw_chars` 是截断前的长度
    （进 `metadata.raw_chars`，让"模型只看到开头"这件事可见），`url` 进
    `metadata.url`，`fetched_at` / `language` 是抓取过程的描述。

    刻意不含 `score`：外部源不给我们可信的相关性分数，编一个只会让下游误以为
    它和 `EvidenceHit` 是同一种东西。

    `direction` 是这条命中来自哪个检索方向（`""` = 没拆方向，即单方向检索的既有
    行为）。它**只用于说明与报告**：不是评分、不参与排序，`_interleave` 也只把它当
    分组用（见 `_interleave_directions`）。带默认值是为了"没拆方向"那条路径的构造
    点一个都不用改——降级必须与 `3c40ba6` 逐字节一致。
    """

    title: str
    text: str
    url: str
    source_name: str
    fetched_at: str
    language: str
    # 截断**之前**的长度。`len(text)` 在长条目上恒等于 `TEXT_MAX_CHARS`，
    # 靠它分不清"这份资料本来就这么长"与"被砍掉了九成"。
    raw_chars: int = 0
    # 这条命中来自哪个方向。空串 = 单方向检索（既有行为）。
    direction: str = ""
    # 搜索源自己给的站点名（`ccf.org.cn` / `IEEE Xplore` …）。空串 = 这个源不提供。
    # 它只用于 `Source.metadata.tags` 的附加项与日志，不参与排序——但"这条来自哪个
    # 站"是用户判断可信度的第一手依据，而 `source_name="deepseek"` 本身不带这个信息。
    site: str = ""
    # 搜索源自己给的权威度与相关性分。语义与用途见 `SourceMetadata.authority` 那段：
    # **不参与我们的打分**，只回答"源头为什么把它排在这里"。
    authority: int | None = None
    rerank_score: float | None = None


class ExternalHits(list):
    """`search_external` 的返回值：一个普通列表 + 一个 `cached` 标记。

    **必须是 `list` 的子类而不是换一个返回类型**：这条链的调用方全都按列表用——
    `tools._external_sources` 迭代它、`scripts/measure_external*.py` 取 `len(hits)`、
    十几条用例写 `assert external.search_external(...) == []`。换成一个 dataclass
    会让那些地方全部要改一遍，而真正新增的信息只有一个布尔值。

    所以 `cached` 走属性，别处一律按 `list` 处理。读取方用
    `getattr(hits, "cached", False)` 而不是直接取属性——用例与脚本会把这个函数
    `monkeypatch` 成 `lambda: [hit]`，那是一个普通 list，没有这个属性。
    """

    cached: bool = False

    def __init__(self, hits=(), *, cached: bool = False) -> None:
        super().__init__(hits)
        self.cached = cached


@dataclass(frozen=True)
class _SearchRun:
    """一次检索过程的自述：拿到什么、发了几个请求、几个源失败了。

    三个数一起才够回答"这次结果能不能当结论缓存下来"（`_worth_caching`）：只有
    `hits` 时空列表分不清"源里确实没有"与"五个源全挂了"——而这两件事一个是有效
    结论、另一个必须下次重试。

    `failures` 数的是**源级失败**（超时 / 5xx / 脏响应 / 连接被拒），由各源自己的
    `except` 与 `_run_source` 的兜底各记一次（`_Budget.note_failure`）。它不记
    "维基的三个候选条目里抓失败了两个"这种部分失败——那种情况下源整体是通的，
    调用方拿到的是它成功的部分，那是可缓存的结论。
    """

    hits: list[ExternalHit]
    requests: int
    failures: int


@dataclass(frozen=True)
class _Slot:
    """一个方向在这次检索里的请求槽位：`key` 是它的名字，`limit` 是它的请求上限。

    单独造一个类型而不是给 `take()` 传两个参数，是为了让"同一方向的四次 take 用同
    一个槽位"这件事在调用处只是一个变量传递：`_wikipedia(query, budget, 2, slot=slot)`
    里三次请求各自 `budget.take(slot)`，漏传一次就会多花掉别的方向的额度，而这种
    泄漏在线程池里表现为"某个方向偶尔少一条"，最难查。

    `key` 为空时**不做**单槽位计数（单方向路径），语义与 `take()` 不带参数相同。
    """

    key: str = ""
    limit: int | None = None


class _Budget:
    """这次外部检索的共享预算：请求次数 + 墙钟截止时间。

    各源在线程里并行跑，所以计数必须加锁——"两个线程各自读到 8、各自发第 9 次"
    是典型的一读一写竞态，而它只在预算刚好用尽时出现，最难复现。

    多方向检索时这是**一份**全局预算（`MULTI_REQUEST_LIMIT`），另有一个按槽位
    （`_Slot`）计的维度：每个方向 5 次。两个维度在同一把锁里判、同一把锁里加，
    所以"两个方向同时抢最后一次全局额度"不会双开。分两次加锁（先判槽位再判全局）
    会留下一个窗口：两边都通过槽位判定、各自 +1，全局就超了 1 次。

    `take()` 同时给出本次请求可用的超时：`min(REQUEST_TIMEOUT, 剩余预算)`。不这么夹
    的话，第 9 次请求仍会拿到满额 10 秒，实际总耗时就能超出 20 秒的预算。
    """

    def __init__(self, limit: int, seconds: float) -> None:
        self._limit = max(int(limit), 0)
        self._deadline = time.monotonic() + max(float(seconds), 0.0)
        self._used = 0
        # 槽位名 → 已用次数。键是方向的序号（`str(index)`），不值钱。
        self._by_slot: dict[str, int] = {}
        # 源级失败次数。它记在这儿而不是另造一个对象，是因为"这次检索的共享状态"
        # 已经只有这一份了（各源在线程里跑，任何新对象都得自己带锁）；而
        # `note_failure()` 的调用点全在各源的 `except` 里，手里正好有这个 budget。
        self._failures = 0
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def failures(self) -> int:
        with self._lock:
            return self._failures

    def note_failure(self) -> None:
        """记一次**源级失败**（超时 / 5xx / 脏响应 / 连接被拒）。

        唯一的读者是 `_worth_caching`：一次全源失败的检索返回的是空列表，与"确实
        没有 0 条"在返回值上完全一样，而只有后者该进缓存（否则代理没开的那一次会把
        "没有结果"钉在库里 24 小时，用户开上代理再生成还是空）。
        """
        with self._lock:
            self._failures += 1

    def slot_used(self, key: str) -> int:
        with self._lock:
            return self._by_slot.get(key, 0)

    def take(self, slot: _Slot | None = None) -> float | None:
        """拿一次请求额度，返回本次允许的超时；没额度或已过截止时间返回 None。"""
        with self._lock:
            if self._used >= self._limit:
                return None
            key = slot.key if slot is not None else ""
            if key and slot is not None and slot.limit is not None:
                if self._by_slot.get(key, 0) >= slot.limit:
                    return None
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._used += 1
            if key:
                self._by_slot[key] = self._by_slot.get(key, 0) + 1
            return min(REQUEST_TIMEOUT, remaining)


def _directions_enabled() -> bool:
    """拆方向开关，**默认关**。显式设成 1 / true / yes / on 才开。

    默认关的依据写在 `DIRECTIONS_ENV` 上面那段（三条全相关 13/20 → 6/20）。这里再
    强调一句：它不是"功能没做完"，实现是完整的、有 29 条用例；它是"做完了、量过了、
    现在的形态不划算"。要翻默认值，先让 `--directions` 的那张表达标。
    """
    return str(os.getenv(DIRECTIONS_ENV, "")).strip().lower() in _TRUTHY


def _proxy() -> str | None:
    """本次检索用的代理地址；None 表示直连。

    只认显式配置：`MINDMAP_EXTERNAL_PROXY` 优先，其次 `HTTPS_PROXY` / `HTTP_PROXY`，
    都没有就落到 `DEFAULT_PROXY`。`none` / `off` / `direct` 是显式直连——没有这个出口，
    在"机器上本来就有透明代理"的环境里就没法关掉它。
    """
    raw = ""
    for name in (EXTERNAL_PROXY_ENV, "HTTPS_PROXY", "https_proxy",
                 "HTTP_PROXY", "http_proxy"):
        raw = os.getenv(name, "").strip()
        if raw:
            break
    if not raw:
        return DEFAULT_PROXY
    if raw.lower() in ("none", "off", "direct", "0"):
        return None
    return raw


def _client() -> httpx.Client:
    """建一个本次检索用的客户端。

    单独成函数是为了可测：用例 patch 掉它就能拿到完整的假响应，而不用碰
    `httpx.HTTPTransport`（`conftest.py` 的 `no_real_http` 闸会把真请求变成断言失败）。
    """
    return httpx.Client(
        proxy=_proxy(),
        timeout=REQUEST_TIMEOUT,
        trust_env=False,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/atom+xml, */*"},
    )


def clean_text(text: str) -> str:
    """剥标签、反转义、压空白。**只做形态清洗，不做内容判断**。

    顺序是关键：先 `html.unescape` 再删标签。反过来（先删后反转义）会把
    `&lt;script&gt;alert(1)&lt;/script&gt;` 留成一段看得见的 `<script>` 文本——不危险，
    但模型会把它当成"这段代码该被执行"的上下文，而我们要的是它读资料。

    压的只有行内空白，换行保留：父块切分按空行与句末标点走，把换行吃掉会把整篇
    压成一行，切块就只剩句末这一个分隔符了。
    """
    value = html.unescape(text or "")
    value = _BLOCK_TAG_RE.sub(" ", value)
    value = _TAG_RE.sub(" ", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _HSPACE_RE.sub(" ", value)
    value = _BLANK_LINES_RE.sub("\n\n", value)
    return "\n".join(line.strip() for line in value.split("\n")).strip()


def _wiki_markdown(text: str) -> str:
    """维基的 `== 小节 ==` → Markdown `## 小节`。理由见 `_WIKI_HEADING_RE`。

    层级一一对应：维基用两侧等号的数量表示级别（`==` 一级章节、`===` 二级），
    Markdown 用 `#` 的数量，所以 `"=" * n` 直接映射成 `"#" * n`（n 从 2 起）。
    少映射一级会让所有小节挤成同一个 `#` 标题，切块的标题吸附就白做了。
    """
    return _WIKI_HEADING_RE.sub(
        lambda match: "#" * len(match.group(1)) + " " + match.group(2), text)


def truncate_chars(text: str, limit: int) -> str:
    """按 `limit` 截断，**结果长度不超上限**（省略号算在里面）。

    单独成函数而不是只留 `truncate_text`：DeepSeek 的正文上限（4000）与五源的
    （`TEXT_MAX_CHARS`）不是同一个数（理由见模块 docstring），而截断的写法必须
    只有一份——两份的差异迟早会体现在"一条有省略号、一条没有"这种细节上。
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:max(limit - 1, 0)].rstrip() + "…"


def truncate_text(text: str) -> str:
    """按 `TEXT_MAX_CHARS` 截断，**结果长度不超上限**（省略号算在里面）。

    省略号不是装饰：正文里出现"…"比出现一个看不出来的硬切口更诚实——用户会知道
    这里少了一截。截断位置先 `rstrip`，免得留半个空格在句尾。
    """
    return truncate_chars(text, TEXT_MAX_CHARS)


def _now() -> str:
    return datetime.now(timezone.utc).strftime(ISO_SECONDS_FORMAT)


def _guess_language(text: str) -> str:
    """按字符集判"zh"还是"en"。**故意不信源自己申报的 `language` 字段。**

    实测 OpenAlex 把《"碳中和"如何"致中和"》标成 `ja`、把《"触电"也要"碳中和"》
    标成 `it`——它的语言检测器在这类短标题上是噪声。字符集不会撒谎：有汉字就是
    `zh`。这个字段唯一的下游用途是 `_interleave` 的 `prefer_language`（中文 query
    时让中文条目先占名额），所以判错一个 `ja`/`it` 的代价就是排序错一位。
    """
    return "zh" if _CJK_RE.search(text or "") else "en"


def _request(
    client: httpx.Client, url: str, *, params: dict[str, Any] | None, timeout: float,
) -> httpx.Response:
    """发一次请求并做两道外部数据的基本校验：状态码、响应体大小。

    非法 JSON **不在这里**判：`Response.json()` 的异常由调用方的 `except Exception`
    收掉，两处各判一次会让"哪一层负责降级"变得含糊。
    返回的 `Response` 已经把正文读进内存，后续解析不会再有网络。
    """
    response = client.get(url, params=params, timeout=timeout)
    if response.status_code >= 400:
        raise ValueError(f"HTTP {response.status_code}")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError(f"响应体 {len(response.content)} 字节，超过上限")
    return response


# ---------------------------------------------------------------- 维基百科


def _wiki_pages(payload: Any) -> list[dict[str, Any]]:
    """从 `action=query` 的响应里取出**有正文**的页面。

    同时认 `formatversion=2`（`pages` 是列表）与 1（`pages` 是以 pageid 为键的对象）：
    我们自己请求的是 2，但这条路也能被样本/旧响应喂进来，形状判断放在这里比放在
    解析处更省事。
    """
    if not isinstance(payload, dict):
        return []
    query = payload.get("query")
    if not isinstance(query, dict):
        return []
    pages = query.get("pages")
    if isinstance(pages, dict):
        pages = list(pages.values())
    if not isinstance(pages, list):
        return []
    return [
        page for page in pages
        if isinstance(page, dict) and (page.get("extract") or "").strip()
    ]


def _wiki_search_titles(
    client: httpx.Client, query: str, budget: _Budget, limit: int,
    slot: _Slot | None = None,
) -> list[str]:
    """条目名检索：主题不是精确标题时（"碳中和 政策"），先找候选条目。

    维基的 `titles=` 是**精确**查找，不命中就只回 `missing`。没有这一步，
    "碳中和技术路径"这类输入会一条都拿不到——而它恰恰是最常见的输入形态。
    """
    timeout = budget.take(slot)
    if timeout is None:
        return []
    response = _request(client, WIKIPEDIA_API, params={
        "action": "query", "list": "search", "srsearch": query,
        "srlimit": str(limit), "format": "json", "formatversion": "2",
    }, timeout=timeout)
    payload = response.json()
    search = (payload.get("query") or {}).get("search") if isinstance(payload, dict) else None
    if not isinstance(search, list):
        return []
    return [
        str(item["title"]) for item in search
        if isinstance(item, dict) and isinstance(item.get("title"), str)
        and item["title"].strip()
    ]


def _wikipedia(
    query: str, budget: _Budget, limit: int, *, slot: _Slot | None = None,
    exact_first: bool = True, fetch_limit: int | None = None,
) -> list[ExternalHit]:
    """中文维基：先试精确标题，不命中再走条目检索，然后逐条抓正文。

    请求次数的上界是 1（精确）+ 1（检索）+ limit（逐条）。每一次都过 `budget`：
    预算用尽时立刻返回已经拿到的部分，而不是"再试最后一条"。

    逐条抓取里的任何一次失败都**保留已拿到的条目**：第 2 条超时时把第 1 条一起丢掉，
    等于用一次抖动废掉整个源——而这两件事没有因果关系。

    两个只给多方向路径用的开关（默认值与单方向路径的既有行为一致）：

      - `exact_first=False`：跳过精确标题那一次。方向是「碳中和政策」这类组合短语，
        `titles=` 几乎必然 `missing`，那一次请求是纯浪费——而每个方向只有 5 次额度。
        跳过不等于放弃精确命中：条目检索（`list=search`）对"碳中和"这种真词条名
        同样会把它排在第一，只是多花一次抓取请求。
      - `fetch_limit`：逐条抓取的上限，独立于 `limit`（返回条数）。方向路径传 1，
        因为"多抓一条正文"的边际价值低于"留一次额度给别的源"。
    """
    hits: list[ExternalHit] = []
    fetch_max = limit if fetch_limit is None else max(min(limit, fetch_limit), 0)
    try:
        with _client() as client:
            pages: list[dict[str, Any]] = []
            if exact_first:
                timeout = budget.take(slot)
                if timeout is None:
                    return []
                payload = _request(client, WIKIPEDIA_API, params={
                    "action": "query", "prop": "extracts|info", "inprop": "url",
                    "explaintext": "1", "redirects": "1", "titles": query,
                    "format": "json", "formatversion": "2",
                }, timeout=timeout).json()
                pages = _wiki_pages(payload)

            if not pages and fetch_max > 0:
                # `[:fetch_max]` 是必须的：`srlimit` 由**服务端**执行，而外部源不是可信的
                # 一端——它回 8 条候选时，逐条抓取就会变成 8 次请求。即使 `budget`
                # 最终会兜住次数，那也已经是"为了几条候选把预算烧光"了。
                for title in _wiki_search_titles(
                        client, query, budget, fetch_max, slot)[:fetch_max]:
                    if len(pages) >= fetch_max:
                        break
                    timeout = budget.take(slot)
                    if timeout is None:
                        break
                    try:
                        payload = _request(client, WIKIPEDIA_API, params={
                            "action": "query", "prop": "extracts|info", "inprop": "url",
                            "explaintext": "1", "redirects": "1", "titles": title,
                            "format": "json", "formatversion": "2",
                        }, timeout=timeout).json()
                    except Exception as exc:  # noqa: BLE001 — 单条失败不废整源
                        logger.warning("外部检索：维基条目 %r 抓取失败（%s）：%s",
                                       title, type(exc).__name__, str(exc)[:120])
                        continue
                    pages.extend(_wiki_pages(payload))

            for page in pages[:fetch_max]:
                hit = _wikipedia_hit(page)
                if hit is not None:
                    hits.append(hit)
    except Exception as exc:  # noqa: BLE001 — 外部源失败只降级，理由见模块 docstring
        budget.note_failure()
        logger.warning("外部检索：中文维基失败（%s）：%s",
                       type(exc).__name__, str(exc)[:160])
    return hits


def _wikipedia_hit(page: dict[str, Any]) -> ExternalHit | None:
    title = str(page.get("title") or "").strip()
    body = clean_text(_wiki_markdown(str(page.get("extract") or "")))
    if not title or not body:
        return None
    url = str(page.get("fullurl") or "").strip()
    if not url:
        # `inprop=url` 在样本/旧响应里可能缺；按维基的固定形状自己拼一个，
        # 这仍然只是**数据**，不会被本模块请求。
        url = "https://zh.wikipedia.org/wiki/" + quote(title.replace(" ", "_"))
    return ExternalHit(
        title=title, text=truncate_text(body), url=url,
        source_name="wikipedia", fetched_at=_now(), language="zh",
        raw_chars=len(body),
    )


# ---------------------------------------------------------------- arXiv


def _arxiv_terms(query: str) -> str:
    """把主题变成 arXiv 的检索式。

    用 `all:"…"` 而不是 `all:词1 词2`：后者是分词后的布尔组合，实测按
    `all:carbon neutrality policy` 命中过"碳纳米材料毒性"——完全跑题的条目会
    污染整棵树，而一条跑题的引用比没有引用更糟（它看起来有出处）。
    短语检索返回空是更好的失败。
    """
    return 'all:"' + re.sub(r'"', " ", query).strip() + '"'


def _arxiv(
    query: str, budget: _Budget, limit: int, *, slot: _Slot | None = None,
) -> list[ExternalHit]:
    """arXiv Atom 接口。用 `xml.etree` 解析，不引新依赖。

    中文主题在这里通常返回 0 条（arXiv 的语料基本是英文），这不是 bug、也不该被
    翻译成英文再查：凭空翻译一个术语就是凭空造一条检索式，而用户输入的语言是
    他的意思的一部分。0 条时维基那条源仍然有结果。
    """
    timeout = budget.take(slot)
    if timeout is None:
        return []
    try:
        with _client() as client:
            response = _request(client, ARXIV_API, params={
                "search_query": _arxiv_terms(query), "max_results": str(limit),
            }, timeout=timeout)
        return _parse_arxiv(response.text, limit)
    except Exception as exc:  # noqa: BLE001 — 外部源失败只降级，理由见模块 docstring
        budget.note_failure()
        logger.warning("外部检索：arXiv 失败（%s）：%s",
                       type(exc).__name__, str(exc)[:160])
        return []


def _parse_arxiv(payload: str, limit: int) -> list[ExternalHit]:
    """Atom → `ExternalHit`。**解析失败抛异常**，由 `search_external` 的兜底收掉。

    单独成函数是为了让用例能拿固定 XML 直接喂进来（不联网、不 patch 客户端）。
    """
    root = ElementTree.fromstring(payload)
    hits: list[ExternalHit] = []
    for entry in root.findall(f"{_ATOM}entry"):
        if len(hits) >= limit:
            break
        title = clean_text((entry.findtext(f"{_ATOM}title") or ""))
        summary = clean_text((entry.findtext(f"{_ATOM}summary") or ""))
        if not title or not summary:
            continue
        raw_url = (entry.findtext(f"{_ATOM}id") or "").strip()
        # arXiv 的 `<id>` 是 `http://arxiv.org/abs/2310.01234v1`。换成 https 只是
        # 让浏览器别吃一次重定向，值本身不再被本模块请求。
        url = re.sub(r"^http://", "https://", raw_url)
        hits.append(ExternalHit(
            title=title, text=truncate_text(summary), url=url,
            source_name="arxiv", fetched_at=_now(), language="en",
            raw_chars=len(summary),
        ))
    return hits


# ---------------------------------------------------------------- OpenAlex


# `abstract_inverted_index` 里的位置索引上限。外部源给什么数字都得先当敌意输入：
# 一个 `[10**9]` 能让下面的 `sorted` 与字典分配吃掉几百 MB，而这只是一个字段。
MAX_ABSTRACT_POSITION = 100_000


def _openalex_abstract(inverted: Any) -> str:
    """OpenAlex 的 `abstract_inverted_index` → 一段文本。

    它的形状是 `{单词: [出现位置, …]}`——存的是倒排索引而不是字符串（省空间）。
    重建就是按位置把词排回去。

    中文实测可用：OpenAlex 对中文摘要的分词位置是连续的，重建后是
    `目前全球气温已达过去12万年来最高值…` 这样的正常句子，不带多余空格。
    """
    if not isinstance(inverted, dict):
        return ""
    positions: dict[int, str] = {}
    for word, indexes in inverted.items():
        if not isinstance(word, str) or not isinstance(indexes, list):
            continue
        for index in indexes:
            if isinstance(index, int) and 0 <= index < MAX_ABSTRACT_POSITION:
                positions[index] = word
    if not positions:
        return ""
    return " ".join(positions[index] for index in sorted(positions))


def _parse_openalex(payload: Any, limit: int) -> list[ExternalHit]:
    """OpenAlex 的 `results[]` → `ExternalHit`。**没有摘要的条目直接丢弃**。

    只有标题的条目对生成毫无用处：它会作为"一条有出处的资料"进入清单，让模型以为
    有据可查，而它实际上只有一个名字。宁可少一条。

    单独成函数是为了让用例能拿固定 JSON 直接喂进来（不联网、不 patch 客户端）。
    """
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    hits: list[ExternalHit] = []
    for work in results:
        if len(hits) >= limit:
            break
        if not isinstance(work, dict):
            continue
        title = clean_text(str(work.get("title") or "")).strip()
        body = clean_text(_openalex_abstract(work.get("abstract_inverted_index")))
        if not title or not body:
            continue
        # 原文地址按"能点回真正落地页"降序取：`primary_location` 是出版社/库的
        # 落地页，`doi` 能换出落地页，`id`（openalex.org/works/W…）是最后的兜底。
        location = work.get("primary_location")
        url = ""
        if isinstance(location, dict):
            url = str(location.get("landing_page_url") or "").strip()
        if not url:
            url = str(work.get("doi") or "").strip()
        if not url:
            url = str(work.get("id") or "").strip()
        hits.append(ExternalHit(
            title=title, text=truncate_text(body), url=url,
            source_name="openalex", fetched_at=_now(),
            language=_guess_language(title), raw_chars=len(body),
        ))
    return hits


def _openalex(
    query: str, budget: _Budget, limit: int, *, slot: _Slot | None = None,
) -> list[ExternalHit]:
    """OpenAlex 检索。免费、无需 key，**对中文 query 直接有效**。

    实测 `search=碳中和` 返回 70562 条命中，取前 20 条里 20 条是中文标题、
    20 条有摘要（中位 213 字）；加 `filter=language:zh` 则有 37138 条。

    所以这里**不做"中文→英文"翻译**：它能直接吃中文，翻译反而会把一条中文命中
    换成三条不那么相关的英文命中。

    无 key 时有一个按出口 IP 共享的日预算（响应里的 `meta.notice` 会说明），
    超了就是 429。429 走 `_request` 的 `status_code >= 400` 那条路，降级为空——
    这与"源挂了"在行为上一致，日志里会写明是 OpenAlex。
    """
    timeout = budget.take(slot)
    if timeout is None:
        return []
    try:
        with _client() as client:
            payload = _request(client, OPENALEX_API, params={
                "search": query, "per-page": str(_candidate_window(limit)),
            }, timeout=timeout).json()
        return _parse_openalex(payload, limit)
    except Exception as exc:  # noqa: BLE001 — 外部源失败只降级，理由见模块 docstring
        budget.note_failure()
        logger.warning("外部检索：OpenAlex 失败（%s）：%s",
                       type(exc).__name__, str(exc)[:160])
        return []


# ---------------------------------------------------------------- Crossref


def _parse_crossref(payload: Any, limit: int) -> list[ExternalHit]:
    """Crossref 的 `message.items[]` → `ExternalHit`。同样丢弃无摘要的条目。

    Crossref 的 `abstract` 是 JATS XML（`<jats:p>…</jats:p>`），`clean_text` 的
    "先反转义再剥标签"正好把它变成正文——顺序反过来会把 `&lt;jats:p&gt;` 留成
    一段看得见的标签文本。

    单独成函数是为了让用例能拿固定 JSON 直接喂进来。
    """
    if not isinstance(payload, dict):
        return []
    message = payload.get("message")
    if not isinstance(message, dict):
        return []
    items = message.get("items")
    if not isinstance(items, list):
        return []
    hits: list[ExternalHit] = []
    for item in items:
        if len(hits) >= limit:
            break
        if not isinstance(item, dict):
            continue
        # `title` 有时是列表、有时是字符串（旧条目），两种都要认。
        raw_title = item.get("title")
        if isinstance(raw_title, list):
            raw_title = raw_title[0] if raw_title else ""
        title = clean_text(str(raw_title or "")).strip()
        body = clean_text(str(item.get("abstract") or ""))
        if not title or not body:
            continue
        url = str(item.get("URL") or "").strip()
        if not url:
            doi = str(item.get("DOI") or "").strip()
            url = f"https://doi.org/{doi}" if doi else ""
        hits.append(ExternalHit(
            title=title, text=truncate_text(body), url=url,
            source_name="crossref", fetched_at=_now(),
            language=_guess_language(title), raw_chars=len(body),
        ))
    return hits


def _crossref(
    query: str, budget: _Budget, limit: int, *, slot: _Slot | None = None,
) -> list[ExternalHit]:
    """Crossref 检索。免费、无需 key，对中文 query 有效（中文标题 19/20）。

    但**摘要覆盖率只有约一半**（实测 20 条里 9–12 条带 abstract）——剩下的
    只有标题，会被 `_parse_crossref` 丢掉。所以这个源在生产上比 OpenAlex 稀疏，
    两个都接是为了"一个没摘要时另一个还有"。

    `USER_AGENT` 里带了邮箱，正好满足 Crossref 的 polite pool 要求（它明确
    建议把联系方式放在 UA 或 `mailto` 参数里，别把匿名流量当默认）。
    """
    timeout = budget.take(slot)
    if timeout is None:
        return []
    try:
        with _client() as client:
            payload = _request(client, CROSSREF_API, params={
                "query": query, "rows": str(_candidate_window(limit)),
                "mailto": POLITE_MAILTO,
            }, timeout=timeout).json()
        return _parse_crossref(payload, limit)
    except Exception as exc:  # noqa: BLE001 — 外部源失败只降级，理由见模块 docstring
        budget.note_failure()
        logger.warning("外部检索：Crossref 失败（%s）：%s",
                       type(exc).__name__, str(exc)[:160])
        return []


# ---------------------------------------------------------------- 快懂百科


def _baike_paragraph(block: str) -> str:
    """一个 `baike-render-paragraph` 块 → 一行纯文本。

    段落内的文字被服务端拆成一串 `<span>`（每个词、每条内链各一个），直接拼 span
    内容会丢词间空格（`Carbon` + ` Neutrality` 被各自的 `strip` 一挤就成了
    `CarbonNeutrality`）。所以**先剥标签再统一压空白**——标签之间的空格本来就是
    正文的一部分，压一次就够。

    内链 `<a>` 的文字留在这里是对的：它是句子的一部分（"途径是减少碳排放和进行
    碳交易"），只是不该把 `href` 带进正文。
    """
    value = html.unescape(_BLOCK_TAG_RE.sub(" ", block))
    # 标签替换成**空串**而不是空格：段落块里只有行内 `span`/`a`，词间空格由 span
    # 的内容自带（实测第三片是 `" Neutrality）…"`，前导空格就在里面）。换成空格
    # 会得到 `碳中和（ Carbon Neutrality）` 这种中文括号后多一个空格的样子。
    value = _BAIKE_TAG_RE.sub("", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return _HSPACE_RE.sub(" ", value).strip()


def _parse_baike(page: str, limit: int, url: str = "") -> list[ExternalHit]:
    """快懂百科词条页 → 最多一条 `ExternalHit`。**解析失败返回空，不抛**。

    一次请求就是一页一个词条，所以这里天然最多 1 条；`limit` 只用于"调用方一条
    都不要"的短路。

    正文按 `baike-render-paragraph` 段落拼接、段落之间留空行：切块器按空行与句末
    标点分块，段间没有空行整篇就会挤成一块。

    站内搜索**不做**：`robots.txt` 的 `User-agent: *` 组明确 `Disallow: /search`，
    而 `/wiki/*` 是 `Allow: /`。所以这个源只做精确词条命中——"碳中和技术路径"
    这种输入在这里是 404，由维基的条目检索兜住。

    `url` 由调用方给**重定向落地后**的地址；留空时下游 `safe_http_url` 折成
    None、界面不渲染链接——好过拼一个可能不存在的 `/wiki/<title>`。

    单独成函数是为了让用例能拿固定的 HTML 文本直接喂进来。
    """
    if limit <= 0:
        return []
    title_match = _BAIKE_TITLE_RE.search(page)
    title = clean_text(title_match.group(1)) if title_match else ""
    paragraphs = [
        text for text in (_baike_paragraph(block) for block in _BAIKE_PARA_RE.findall(page))
        if text
    ]
    body = "\n\n".join(paragraphs)
    if not title or not body:
        return []
    return [ExternalHit(
        title=title, text=truncate_text(body), url=url,
        source_name="baike", fetched_at=_now(), language="zh",
        raw_chars=len(body),
    )]


def _baike(
    query: str, budget: _Budget, limit: int, *, slot: _Slot | None = None,
) -> list[ExternalHit]:
    """快懂百科（`baike.com`，原互动百科）：**唯一实测 robots 允许的中文百科**。

    为什么是它而不是百度百科/知乎：那两个站的 `robots.txt` 在 `User-agent: *`
    组下都是 `Disallow: /`（全站拒绝未列名爬虫），百度百科的词条页还会返回
    `window.__PARIS_CHALLENGE_START__` 的 JS 挑战页而不是正文。快懂百科是
    `Allow: /`（只禁 `/search`、`/user`、`/edit_community` 等），词条页服务端
    直接渲染正文，不需要任何绕过。这条差别是这个源存在的唯一理由。

    正文实测偏短（"碳中和" 725 字、"量子计算" 881 字、"光合作用" 256 字），
    比中文维基（"碳中和" 2704 字）短得多。它的价值是**第二个中文百科视角**，
    不是替代维基。
    """
    timeout = budget.take(slot)
    if timeout is None:
        return []
    try:
        with _client() as client:
            response = _request(
                client, BAIKE_WIKI_URL + quote(query), params=None, timeout=timeout)
        # 落地 URL 用**跟随重定向之后**的地址：`/wiki/碳中和` 会 302 到
        # `/wikiid/7194…`，存后者才是一个稳定可点的链接（前者依赖词条名，
        # 改名的词条会 404）。
        return _parse_baike(response.text, limit, url=str(response.url))
    except Exception as exc:  # noqa: BLE001 — 外部源失败只降级，理由见模块 docstring
        budget.note_failure()
        logger.warning("外部检索：快懂百科失败（%s）：%s",
                       type(exc).__name__, str(exc)[:160])
        return []


# ---------------------------------------------------------------- DeepSeek Search / Fetch
#
# 与上面五个源的关系是**并列**，不是替换：理由写在模块 docstring 的"外部源清单"
# 与"DeepSeek Search：三个判断"两节里。这一段只放实现。

# 这个源在 `source_name` / `_EXTERNAL_KINDS` / 缓存字段里的身份。取 `"deepseek"`
# 而不是按站点名（`ccf.org.cn`）分桶：`_interleave` 按 `source_name` 建桶，按站点名
# 分会让同一个源变成五六个桶，占满全部名额——那正是它当初要修的病。
DEEPSEEK_SOURCE = "deepseek"


def _deepseek_config() -> tuple[str, str]:
    """`(base, key)`。base 去掉尾部斜杠；key 缺失时返回空串。

    三级回落，顺序写死在这里（全仓只有这一处读这两个变量）：

      1. `MINDMAP_DEEPSEEK_TOOLS_BASE_URL` / `MINDMAP_DEEPSEEK_TOOLS_API_KEY`
         （这个源**专属**的变量，优先它）；
      2. `app/settings/store.resolve()` 的生效值——页面「模型设置」里保存过的值，
         没有才回落到 `MINDMAP_LLM_BASE_URL` / `MINDMAP_LLM_API_KEY`；
      3. base 再缺省成 `DEEPSEEK_DEFAULT_BASE`。

    第 2 档走 `store.resolve()` 而不是自己 `os.getenv` 那两个名字，是与另外三处出口
    （生成、agent 工具循环、视觉提取）保持同一套口径：用户在面板里换了 key，这条
    外部检索的路也必须跟着换。只把前三条路改掉、留着这里读旧环境变量，就是
    AGENTS.md 那条"改一个能力的入口，要检查所有会走到它的路径"警告的典型翻版——
    面板显示"已保存"，生成走得通，而"贴个链接让模型读正文"这一步在旧 key 上 401。

    专属变量排在最前是有意义的：它可以指向**另一家**服务（一个专门给 tools 用的
    端点），面板管的是主模型那条路，两者不该互相覆盖。

    **key 没有缺省值**：没有 key 时这个源不该发请求，返回空串让调用方走"配置缺失"
    那条路——而不是拿一个空 Bearer 去换 401，那种日志看起来像"接口鉴权炸了"。
    """
    # 函数内 import：本模块在 `app.knowledge` 里被较早导入，`settings.store` 顶层
    # 虽然只依赖 `core.errors`，但把这条边留在函数里，等于保证"外部检索这个可选
    # 源"不会参与应用启动时的导入顺序。与 `ingestion/vision_pdf._config()` 同一手法。
    from app.settings import store as _settings_store

    base = os.getenv(DEEPSEEK_BASE_ENV, "").strip().rstrip("/")
    key = os.getenv(DEEPSEEK_KEY_ENV, "").strip()
    if not base or not key:
        resolved = _settings_store.resolve()
        base = base or resolved.base_url
        key = key or resolved.api_key
    if not base:
        base = DEEPSEEK_DEFAULT_BASE
    return base, key


def deepseek_search_enabled() -> bool:
    """第六个源（Search）的开关，**默认关**。三条理由见 `DEEPSEEK_SEARCH_ENV`。"""
    return str(os.getenv(DEEPSEEK_SEARCH_ENV, "")).strip().lower() in _TRUTHY


def deepseek_fetch_enabled() -> bool:
    """按 URL 抓正文的开关（粘贴链接 → 材料），**默认关**。

    它与 Search 分开而不是共用：Search 是"生成时自动多发一次请求"，Fetch 是"用户
    自己贴了一个链接"。两件事的成本与出境面不同（一次生成 = 一次 Search；一个链接 =
    一次 Fetch），要开哪一个由用户决定。
    """
    return str(os.getenv(DEEPSEEK_FETCH_ENV, "")).strip().lower() in _TRUTHY


def deepseek_configured() -> bool:
    """有没有可用的 key。**只回答"配没配"，不发请求**。

    读者是 `knowledge.router` 的 `/fetch`：没有 key 时它在契约层给 503（"这是部署
    问题，你改不了"），而不是让请求打出去换成 401 再被吞成 502（"这个链接读不出
    正文"）——后者会指着一个好链接说它坏了。
    """
    return bool(_deepseek_config()[1])


def _deepseek_client() -> httpx.Client:
    """DeepSeek 专用的客户端：**默认直连**，与五源那个 `_client()` 分开。

    单独一个工厂而不是复用 `_client()`，因为两边的出口必须相反：五源在本机只有经
    Clash 才通，DeepSeek 只有直连才通（经 Clash 时 `SSL: UNEXPECTED_EOF_WHILE_
    READING`，20/20 全失败）。`MINDMAP_DEEPSEEK_TOOLS_PROXY` 是唯一的覆盖口，
    `none` / `off` / `direct` / 空 都表示直连。

    `trust_env=False` 的理由与 `_client()` 相同：本机 `ProxyEnable=0`，让 httpx 去
    读环境变量会拿到空值——那是最难解释的一种"所有源全挂"。
    """
    raw = os.getenv(DEEPSEEK_PROXY_ENV, "").strip()
    proxy = None if raw.lower() in ("", "none", "off", "direct", "0") else raw
    return httpx.Client(
        proxy=proxy,
        timeout=DEEPSEEK_TIMEOUT,
        trust_env=False,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )


def _deepseek_post(path: str, body: dict[str, Any], timeout: float) -> Any:
    """POST 一次 + 两道基本校验，返回解析后的 JSON。**失败抛异常**。

    不写进 `_request`：那个函数是 GET + `params`，五源的形状；给 DeepSeek 加 `json=`
    参数会让它长成"按调用方分叉"的第四个分支。两道校验（状态码、响应体大小）照抄
    ——那是外部数据的底线，不是因为换了家公司就可以省。
    """
    base, key = _deepseek_config()
    if not key:
        raise ValueError("没有配置 DeepSeek key")
    with _deepseek_client() as client:
        response = client.post(
            base + path, json=body, timeout=timeout,
            headers={"Authorization": f"Bearer {key}"},
        )
    if response.status_code >= 400:
        # 401/402/429 各自的下一步不同（换 key / 充值 / 等），但这一层只负责报出
        # 状态码：正文里可能有服务端的错误说明，统一截下来交给日志。
        detail = response.text[:160].replace("\n", " ")
        raise ValueError(f"HTTP {response.status_code}: {detail}")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError(f"响应体 {len(response.content)} 字节，超过上限")
    return response.json()


def _deepseek_query(query: str) -> str:
    """发给 Search 的 query：中文主题追加 `lang:zh`，其余原样。

    **`lang:zh` 是 query 的一部分，不是请求体字段**。实测请求体加 `"lang": "zh"` 被
    静默忽略（同一份 query 带不带那个字段，结果集完全相同），而写进 query 立刻改变
    结果：`机器学习的主要方向` 不带时回 3 条英文站（`ieee.org` / `IEEE Xplore` /
    一个印尼域名），带 `lang:zh` 时回 3 条中文站（`cppinfo.cn` / 中国计算机学会 /
    Google Patents 的中文专利）。

    判据复用 `_query_language`（有 CJK 就是 zh）。**两处必须同源**：那个函数决定
    `_interleave` 要不要让中文桶先占名额，这里决定要不要让上游只回中文——一个中文
    query 若在源侧被滤成英文结果、又在排序侧被压到后面，两条规则会互相抵消。
    反过来，英文 query 不追加任何 `lang:`：那是没有证据支撑的偏好（同一批英文主题
    带不带 `lang:en` 结果完全相同，实测）。
    """
    text = (query or "").strip()
    if not text or "lang:" in text:
        return text
    if _query_language(text) != "zh":
        return text
    return f"{text} lang:zh"


def _deepseek_float(value: Any) -> float | None:
    """搜索源给的分数 → float 或 None。**NaN / inf / 字符串一律当没有**。

    外部源不是可信的一端：一个 `"rerank_score": "1.9"` 或 `1e999` 进到 SQLite 之后，
    下游 `max()` 与比较运算会在某个时刻抛 `TypeError`——那会把"少一个元数据"升级成
    "整次生成失败"。`-1e6 < value < 1e6` 顺手把 NaN 挡掉（NaN 的任何比较都是 False）。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    return score if -1e6 < score < 1e6 else None


def _deepseek_count(value: Any) -> int | None:
    """搜索源给的权威度 → 非负 int 或 None。`bool` 不是数字，负数当没有。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _parse_deepseek_search(payload: Any, limit: int) -> list[ExternalHit]:
    """`{"results": [...]}` → `ExternalHit`。**纯解析，不发请求**。

    每个字段的取法都有实测形状兜着（`scripts/probe_deepseek_tools.py` 的原始响应）：

      - `content` 才是正文（1.5k–21k 字）；`summary` 是**服务端生成的摘要**（0.8k–5.4k
        字），`snippet` 是一句话。**不用 `summary` 顶替空 `content`**：我们的引用是
        "逐字可校验"的，而摘要里那句话在原文页面上找不到——用户点开链接核对时会发现
        对不上，那比"这条没有正文"更糟。空 `content` 交给 `_fill_missing_content`
        按 `url` 补抓，抓不到就丢弃这条。
      - `authority` / `rerank_score` 进 `ExternalHit` 的同名字段，最终落到
        `Source.metadata`（那两个字段的语义见契约里的注释）。
      - `date` **丢掉了**：实测一多半是 None，而且取值混合了"出版日期"与"站点更新
        日期"（同一批里既有 2018-09-24 也有 2026-06-29）。把它塞进
        `metadata.created_at` 会让 `since`/`until` 过滤静默误判——那是"看起来有时效
        信息、其实不可信"的一类。
      - 没有 `url` 的命中**直接丢弃**：既补抓不了正文，也没法在界面上点回原文，
        而"点不开的引用"和"编的引用"在用户那里没有区别。

    按 `rerank_score` 降序（缺失的排最后，保持原序）：实测它落在 1.75–2.02，越大越
    相关；同一批里 `cppinfo.cn` 1.797 排在 `中国计算机学会` 1.750 之前。这个排序只
    影响"哪条被我们留下"，不影响检索打分。
    """
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, list) or limit <= 0:
        return []
    hits: list[ExternalHit] = []
    for item in results:
        if len(hits) >= limit:
            break
        if not isinstance(item, dict):
            continue
        title = clean_text(str(item.get("title") or "")).strip()
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        body = clean_text(str(item.get("content") or ""))
        language = _guess_language(f"{title}\n{body[:200]}")
        hits.append(ExternalHit(
            title=title or url,
            text=truncate_chars(body, DEEPSEEK_TEXT_MAX_CHARS),
            url=url,
            source_name=DEEPSEEK_SOURCE,
            fetched_at=_now(),
            language=language,
            # `raw_chars` 记的是**截断前**的长度，与五源同口径；正文空时是 0，补抓
            # 成功之后由 `_fill_missing_content` 换成补抓到的长度。
            raw_chars=len(body),
            site=str(item.get("site_name") or "").strip(),
            authority=_deepseek_count(item.get("authority")),
            rerank_score=_deepseek_float(item.get("rerank_score")),
        ))
    ranked = sorted(
        hits, key=lambda hit: (-hit.rerank_score if hit.rerank_score is not None else 1e9))
    return ranked


def _deepseek_fetch(
    url: str, *, budget: _Budget | None = None, slot: _Slot | None = None,
    max_chars: int = DEEPSEEK_TEXT_MAX_CHARS,
) -> ExternalHit | None:
    """按 URL 抓一篇正文，失败返回 None（**永不抛**）。

    两个调用方各用自己的上限：用途 B（补 Search 的空 `content`）用
    `DEEPSEEK_TEXT_MAX_CHARS`，与 Search 的其它命中等长；用途 A（用户贴了一个链接）
    用 `TEXT_MAX_CHARS`——那是用户明确指过来的一整篇材料，没有理由比他自己上传的
    文档拿得更少。

    `budget` 可选：用途 A 是用户的一次显式动作，它不在生成链路的预算里（那次生成还
    没开始）；硬塞一个预算只会让"用户点了一下链接"和"生成时多搜一轮"共用一笔账。
    """
    target = (url or "").strip()
    if not target.lower().startswith(("http://", "https://")):
        return None
    timeout = DEEPSEEK_TIMEOUT
    if budget is not None:
        granted = budget.take(slot)
        if granted is None:
            return None
        timeout = min(timeout, granted)
    try:
        payload = _deepseek_post(DEEPSEEK_FETCH_PATH, {"url": target}, timeout)
    except Exception as exc:  # noqa: BLE001 — 外部源失败只降级，理由见模块 docstring
        if budget is not None:
            budget.note_failure()
        logger.warning("外部检索：DeepSeek Fetch %r 失败（%s）：%s",
                       target[:120], type(exc).__name__, str(exc)[:160])
        return None
    if not isinstance(payload, dict):
        return None
    body = clean_text(str(payload.get("content") or ""))
    if not body:
        return None
    title = clean_text(str(payload.get("title") or "")).strip() or target
    return ExternalHit(
        title=title,
        text=truncate_chars(body, max_chars),
        url=target,
        source_name=DEEPSEEK_SOURCE,
        fetched_at=_now(),
        language=_guess_language(f"{title}\n{body[:200]}"),
        raw_chars=len(body),
        site=_host_of(target),
    )


def _host_of(url: str) -> str:
    """URL → 主机名（`https://www.ccf.org.cn/x` → `www.ccf.org.cn`）。取不出返回空串。

    只用于 `ExternalHit.site`（进 `Source.metadata.tags`，让界面能显示"这条来自哪个
    站"）。不引 `urllib.parse.urlsplit` 之外的任何东西，也不做任何规范化：这个值只
    用于显示。
    """
    _, _, rest = (url or "").partition("://")
    host = rest.split("/", 1)[0].split("?", 1)[0].strip()
    return host[:120]


def fetch_url(url: str, *, max_chars: int = TEXT_MAX_CHARS) -> ExternalHit | None:
    """用途 A 的入口：按 URL 读一篇网页，返回一条 `ExternalHit`；失败返回 None。

    单独一个公开名而不是让调用方直接调 `_deepseek_fetch`：那个私有函数还带
    `budget` / `slot` 两个"生成链路预算"的参数，而这条路上没有预算（用户点了一下，
    还没开始生成）。一个只收 URL 的签名把这件事说清楚，调用方也不会传错。
    """
    return _deepseek_fetch(url, max_chars=max_chars)


def _fill_missing_content(
    hits: Sequence[ExternalHit], budget: _Budget, slot: _Slot | None,
) -> list[ExternalHit]:
    """用途 B：`content` 为空的命中按 `url` 补抓一次，抓不到就丢掉。

    为什么值得多一次请求：Search 的 `need_content` 大多数时候会给正文，但"给不出"
    是有真实形态的（付费墙、纯 JS 渲染、PDF、robots 拦）。那时 `content` 是空串而
    `url` / `title` / `authority` 都在——只差正文这一步，而 Fetch 正是补它的工具。

    抓不到就丢弃：一条 `text` 为空的 `ExternalHit` 转成 `Source` 之后是一个
    `status="parsed"`、正文为空的来源。它会进材料清单、看起来像依据、引用一条都生
    不出来，并且 `sourceInputError` 那类检查在服务端没有对应物——静默的假来源比
    少一条糟得多。
    """
    filled: list[ExternalHit] = []
    for hit in hits:
        if hit.text:
            filled.append(hit)
            continue
        fetched = _deepseek_fetch(hit.url, budget=budget, slot=slot)
        if fetched is None:
            logger.info("外部检索：DeepSeek 命中 %r 正文为空且补抓失败，丢弃该条",
                        hit.title[:60])
            continue
        # 保留 Search 给的标题与排序元数据：补抓回来的标题常常是站点名（`Example
        # Domain`），而 Search 的标题更贴近这一条讲什么。
        filled.append(replace(
            fetched,
            title=hit.title or fetched.title,
            site=hit.site or fetched.site,
            authority=hit.authority,
            rerank_score=hit.rerank_score,
            language=hit.language or fetched.language,
        ))
    return filled


def _deepseek_search(
    query: str, budget: _Budget, limit: int, *, slot: _Slot | None = None,
) -> list[ExternalHit]:
    """DeepSeek 的 Search 工具 API：一次往返换回最多 `limit` 条**已抓好正文**的命中。

    与五源的形态差别是本函数存在的原因：维基要"检索 + 逐条抓正文"两三轮，这里一次
    POST 就返回 `content`（实测 0.47–1.13 秒、3 条、正文 1.5k–21k 字）。所以它只花
    1 次预算。

    `count` 传 `limit` 而不是 `SOURCE_CANDIDATES`：这个源的每一条都是**要正文**的
    （`need_content=True`），响应体随 `count` 线性涨。要 `limit` 条是因为最终可能全部
    入选（打分之后不再有"只剩 1 席"这回事），再多要只会让响应体更大、延迟更高。

    失败处理与五源同一套：`budget.note_failure()` + 返回已拿到的部分。**key 缺失也
    走这一条**——它是配置错误，不是"这个源没有结果"，而 `_worth_caching` 靠这个计数
    区分两者（空结果不该被缓存 24 小时，否则用户配好 key 之后一天内都不会重试）。
    """
    base, key = _deepseek_config()
    if not key:
        budget.note_failure()
        logger.warning(
            "外部检索：DeepSeek Search 开着但没有 key（%s，或页面「模型设置」/"
            "MINDMAP_LLM_API_KEY）", DEEPSEEK_KEY_ENV)
        return []
    timeout = budget.take(slot)
    if timeout is None:
        return []
    started = time.perf_counter()
    try:
        payload = _deepseek_post(
            DEEPSEEK_SEARCH_PATH,
            {"query": _deepseek_query(query), "count": max(limit, 1),
             "need_content": True},
            min(timeout, DEEPSEEK_TIMEOUT),
        )
    except Exception as exc:  # noqa: BLE001 — 外部源失败只降级，理由见模块 docstring
        budget.note_failure()
        logger.warning("外部检索：DeepSeek Search 失败（%s）：%s",
                       type(exc).__name__, str(exc)[:160])
        return []
    hits = _parse_deepseek_search(payload, limit)
    hits = _fill_missing_content(hits, budget, slot)
    logger.info(
        "外部检索：DeepSeek Search %r 返回 %d 条（%s），耗时 %.2fs",
        query[:40], len(hits),
        ", ".join(f"{hit.site or '-'}/{len(hit.text)}字" for hit in hits[:3]),
        time.perf_counter() - started,
    )
    return hits


# ---------------------------------------------------------------- 聚合


def _interleave(
    hits: Sequence[ExternalHit], limit: int, *, prefer_language: str | None = None,
) -> list[ExternalHit]:
    """按 `source_name` 交替取。

    **只服务多方向检索**：单方向那条路已换成 `_select_hits`（按相关度排 + 内容去重 +
    MMR）。方向路径保留它是因为那里的规模完全不同——每个方向只有 `DIRECTION_HIT_LIMIT`
    =2 个名额、候选 1-2 条，排序与轮转在那儿没有区别，而"方向内保源多样"能保证
    一个方向的 3 条不全是同一份百科。改它之前先看 `docs/EXTERNAL-SELECTION.md`：
    "每源保底"在单方向路径上被证伪的那套读数，与这里不是同一个场景。

    不交替的后果是一边倒：维基一次能给 3 条、arXiv 常给 0–3 条，直接拼接会让维基
    占满全部名额，用户拿到的"资料"其实是同一个百科条目的三个邻接条目。交替之后，
    只要两个源都有结果，就至少各占一条。

    `prefer_language` 解决的是交替之后仍然存在的一个坑：**5 个源抢 3 个名额**时，
    排在后面的源一条都进不了结果。中文 query 下如果不偏好中文，"碳中和"可能拿到
    三条英文论文摘要（OpenAlex/Crossref 的英文命中比中文多得多），而用户输入的是
    中文。所以中文 query 时把中文桶提到前面——桶内顺序不动，源之间也不打乱，
    只是"先给谁名额"。

    只有 `prefer_language is not None` 才排序。英文 query 传 None，走原来的顺序：
    一个"猜出来的偏好"不该改写英文检索的既有行为。
    """
    buckets: dict[str, list[ExternalHit]] = {}
    for hit in hits:
        buckets.setdefault(hit.source_name, []).append(hit)
    queues = [queue[:] for queue in buckets.values()]
    if prefer_language is not None:
        # `sorted` 是稳定排序：同语言内保持源的收集顺序，不会打乱既有交替。
        queues.sort(key=lambda queue: 0 if queue[0].language == prefer_language else 1)
    merged: list[ExternalHit] = []
    while len(merged) < limit:
        taken = False
        for queue in queues:
            if not queue or len(merged) >= limit:
                continue
            merged.append(queue.pop(0))
            taken = True
        if not taken:
            break
    return merged


def _query_language(query: str) -> str | None:
    """query 是不是中文。**只回 "zh" 或 None，从不回 "en"。**

    回 "en" 意味着英文 query 时把中文源压到后面，而中文源在英文主题上经常也有
    结果（中文维基有"伦敦"、Crossref 有英文标题的中文期刊论文）。那是一种没有
    证据支撑的偏好。中文 query 相反：用户用中文问，就该先看到中文材料。

    它同时是 `_deepseek_query` 的判据（决定要不要追加 `lang:zh`）：源侧过滤与排序侧
    偏好必须由同一个函数回答，理由写在那函数的 docstring 里。
    """
    return "zh" if _CJK_RE.search(query or "") else None


# ---------------------------------------------------------------- 取数策略：打分 → 排序 → 去重 → 多样性
#
# 这一段取代了"每源轮流取 + 给 DeepSeek 留 1 席"。**为什么换成排序**：那两条规则
# 的本质是"在没有相关性判据的情况下，用源的配额代替判据"。而判据现在是有的——
# 不需要语义模型，光是"词面证据出现在哪里、出现几次"就把噪声率从 43% 压到 18%
# （7 主题 / 256 条候选 / 人工判定的读数见 `docs/EXTERNAL-SELECTION.md`）。
#
#   title  0.5  —— 标题的得分分四档（逐字 / 整串 / 词块 / token），不是二值。分档
#                  的理由是一个具体反例：「机器学习」是维基词条，「机器学习理论与实践」
#                  是图书商品页，二值口径下两者一样高（标题都含这个词块），而前者是
#                  这条 query 的答案、后者是一张商品页。分档之后前者 1.0、后者 0.9，
#                  排序把它们分开了（`--variant-grid` 的"分档"那几行）。
#   lead   0.3  —— 正文前 `RELEVANCE_CHARS` 字里的**次数**（封顶 3）。封顶是因为
#                  "出现"与"出现一次"都只是有无，而导航/页头里的重复不算证据：
#                  教育平台课程页在开场 5 次，全在课程名与页头。
#   freq   0.2  —— 全文次数（封顶 12），弱信号，只用它拉开"提了一下"与"通篇在讲"。
#
# 权重之和 = 1，所以 `evidence` 落在 [0,1]；`rerank` 只乘一层 [0.75, 1.0] 的系数。
# **它不改变档位，只改变同档内的先后**——理由是一个实测：DeepSeek 自己的重排分把
# 上面那条课程页排在自己那批的第一名（1.812），所以拿它定档位等于把这个 bug 原样
# 搬进来。五源没有这个字段，缺它时给**中性值 0.5**而不是 0：给 0 等于"没有分数就是
# 最低分"，实测后果是 deepseek 的每一条都压过维基词条（`机器学习` 那题 limit=3 变成
# 三条 deepseek 商品页，相关 0/3；中性值 0.5 时是 2/3）。

TITLE_SCORE_EXACT = 1.0
TITLE_SCORE_FULL = 0.9
TITLE_SCORE_PHRASE = 0.8
TITLE_SCORE_TOKENS = 0.5
TITLE_WEIGHT = 0.5
LEAD_WEIGHT = 0.3
FREQ_WEIGHT = 0.2
# 两处词频的上限。3 = "引用能取证的深度里出现两次以上"；12 = 全文里"通篇在讲"。
# 都不封顶会让一条把主题词写进页头的目录页刷满词频分——那正是坏样本的形态。
LEAD_OCCURRENCE_CAP = 3
BODY_OCCURRENCE_CAP = 12
# `rerank_score` 的归一化区间。实测它落在 1.49–2.20（越大越相关），1.5–2.0 覆盖
# 绝大多数取值，超出部分截断——它是**同档内**的微调，不需要精确的量纲。
RERANK_FLOOR = 1.5
RERANK_CEIL = 2.0
RERANK_INFLUENCE = 0.25
RERANK_NEUTRAL = 0.5

# 分数地板：**没有任何词面证据（score == 0）的一条不当依据**。它是那道词面门的
# 分级版本——门要求"整块出现"，这里只要求"分数大于 0"（标题四档任意一档、或正文里
# 出现过一次），所以它放行的范围比门宽，同时保住了门那条契约：全站跑题时返回空列表，
# 而不是把一条零证据的页面塞进材料（`test_all_sources_filtered_returns_empty_list`
# 钉着这条）。
#
# 代价量过，可以忽略：256 条候选里打分为 0 的 22 条，21 条是噪声（听课页/词条撞形），
# 1 条是被英文词口径漏掉的相关条目——给 `_occurrences` 补上英文词之后它就不是 0 了。
SCORE_FLOOR = 0.0
# 跨语言那一档的分数（见 `score_hit`）。0.05 比任何真实词面证据都小：正文里出现过
# 一次的候选是 `LEAD_WEIGHT / LEAD_OCCURRENCE_CAP` = 0.1，所以"有证据的永远在前"；
# 而它大于 `SCORE_FLOOR`，所以名额有剩时它仍然能进材料——那是"判据没有说话能力"
# 的正确读法，不是"这条一定相关"。
CROSS_LINGUAL_SCORE = 0.05

# 去重的相似度阈值：`_token_list`（中文 bigram + 英文整词）在「标题 + 正文前
# `DEDUPE_TEXT_CHARS` 字」上的 Jaccard。0.60 是量出来的分界——池内真正"同一条资料
# 从两个源回来"的 13 对在 0.66–1.00，而"同主题不同文"的最高一对在 0.40 以下。
# 不引新依赖：分词复用检索那套（`knowledge.service._token_list`），语义相似度那套
# （`optimization` 的余弦）在这里是多余的——要并的是同一篇文章，不是同一个意思。
DEDUPE_TEXT_CHARS = 200
DEDUPE_THRESHOLD = 0.60

# 多样性：每选一条，把"与已选内容最相似"的程度折算成扣分（MMR 的朴素版）。
# **这一层不按源**——按源去重就是这次要拆掉的东西。0.25 的含义：相似度 1.0 的一条
# 损失 1/4 分，不足以把一条高分相关命中挤到一条低分噪声后面，但足够在同分附近把
# 重复的排到后面。
DIVERSITY_LAMBDA = 0.25


def _occurrences(query: str, text: str) -> int:
    """query 的词面在一段文字里出现的**次数**。

    中文词块区分大小写（汉字没有大小写），英文词按小写比——纯英文 query 的
    `_phrase_terms` 是空的，只数中文词块会让它永远拿不到正文证据（实测：
    `Cursor` 那题里维基的 `Anysphere` 正文开头就写着它为 Cursor 提供开发服务，
    只数中文词块时它打分为 0，一条真依据被判成噪声）。
    """
    value = text or ""
    total = sum(value.count(term) for term in _phrase_terms(query))
    lowered = value.lower()
    total += sum(lowered.count(term) for term in _word_terms(query))
    return total


def _title_score(query: str, hit: ExternalHit) -> float:
    """标题的得分**分四档**：逐字相同 > 整串出现 > 词块出现 > 只有 token 重合。

    分档而不是二值，理由见上面 `TITLE_WEIGHT` 那段（维基词条 vs 图书商品页）。
    最后一档是 token 覆盖率的折价（0.5 倍）：它只在"标题与 query 共享 bigram 但
    没有整块命中"时生效，权重低是因为这一档的证据最弱（`用户画像` 的标题里出现
    `画像` 二字说明不了什么）。

    英文 query 靠 `_word_terms` 那一档：`Cursor` 的标题里出现 `cursor` 就是命中，
    与中文 query 的"词块出现在标题里"是同一件事。
    """
    text = (query or "").strip()
    title = hit.title or ""
    if text and title.strip() == text:
        return TITLE_SCORE_EXACT
    if text and text in title:
        return TITLE_SCORE_FULL
    if any(phrase in title for phrase in _phrase_terms(text)):
        return TITLE_SCORE_PHRASE
    words = _word_terms(text)
    if words and _word_terms(title) & words:
        return TITLE_SCORE_PHRASE
    tokens = {token for token in _token_list(title) if token.strip()}
    query_tokens = [token for token in _token_list(text) if token.strip()]
    if not query_tokens:
        return 0.0
    return TITLE_SCORE_TOKENS * (
        sum(1 for token in query_tokens if token in tokens) / len(query_tokens))


def _rerank_norm(hit: ExternalHit) -> float:
    """源站自己的重排分 → [0,1]；缺失给中性值（理由见 `RERANK_NEUTRAL`）。"""
    score = hit.rerank_score
    if score is None:
        return RERANK_NEUTRAL
    span = RERANK_CEIL - RERANK_FLOOR
    return max(0.0, min(1.0, (score - RERANK_FLOOR) / span))


def score_hit(query: str, hit: ExternalHit) -> float:
    """一条命中的相关度分，落在 [0, 1]。**纯函数、无 IO、可离线重放**。

    构成与系数见上面那一段注释。它是 `_select_hits` 唯一的排序依据，也是这个模块
    里唯一"函数值可以解释"的地方：任何一个分数都能拆成"标题几分 / 前 300 字几分 /
    全文几分 / 源站分几分"，所以"为什么这条排第一"永远有答案。

    **跨语言的那一档单独给一个极小分**（`CROSS_LINGUAL_SCORE`）：query 与命中的
    书写系统没有交集时，词面判据在这里没有说话的能力（中文 query 配一篇纯英文论文，
    两边没有一个可比的字符），把它当成"零证据"等于用"拼写不同"判"内容无关"——
    实测里 `注意力机制 → Attention Is All You Need` 就是这样被误杀的。给它一个
    **比任何真实词面证据都小**的分，效果是"排到最后但不被抹掉"：有词面证据的候选
    永远排在它前面，名额还有剩时才轮到它。
    """
    title = _title_score(query, hit)
    lead = min(_occurrences(query, _hit_blob(hit)), LEAD_OCCURRENCE_CAP) / LEAD_OCCURRENCE_CAP
    freq = min(_occurrences(query, hit.text or ""), BODY_OCCURRENCE_CAP) / BODY_OCCURRENCE_CAP
    evidence = TITLE_WEIGHT * title + LEAD_WEIGHT * lead + FREQ_WEIGHT * freq
    if evidence <= 0:
        return CROSS_LINGUAL_SCORE if _cross_lingual(query, hit) else 0.0
    return evidence * (1.0 - RERANK_INFLUENCE + RERANK_INFLUENCE * _rerank_norm(hit))


def _cross_lingual(query: str, hit: ExternalHit) -> bool:
    """query 与命中的**书写系统有没有交集**。没有交集时词面判据无从比较。

    与 `_relevant_hits` 里那句放行用的是同一个判据（有没有中文），两处必须同源：
    一处说"判不了"就放行、另一处说"判不了"就当零分，等于同一批命中在两个环节里
    得到相反的评价。
    """
    query_cjk = bool(_CJK_RUN_RE.search(query or ""))
    blob_cjk = bool(_CJK_RUN_RE.search(_hit_blob(hit)))
    return query_cjk != blob_cjk


def _shingles(hit: ExternalHit) -> frozenset[str]:
    """去重用的身份：标题 + 正文前 `DEDUPE_TEXT_CHARS` 字的 token 集合。

    用 token（中文 bigram + 英文整词）而不是字符 n-gram：同一篇文章从两个源回来时
    标题几乎逐字相同，token 集合的重合度就足以判同；而标题里的标点、来源后缀
    （`- 中国医药信息查询平台`）在这些 token 上本来就不算数。
    """
    tokens = _token_list(hit.title) + _token_list((hit.text or "")[:DEDUPE_TEXT_CHARS])
    return frozenset(token for token in tokens if token.strip())


def _similarity(left: ExternalHit, right: ExternalHit) -> float:
    """两条命中的内容相似度（Jaccard）。空集返回 0：没有可比的内容就不判同。"""
    first, second = _shingles(left), _shingles(right)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def _dedupe(
    ranked: Sequence[ExternalHit], *, threshold: float = DEDUPE_THRESHOLD,
) -> tuple[list[ExternalHit], int]:
    """按给定顺序留第一条、并掉后面相似的。返回 `(留下的, 并掉几条)`。

    顺序由调用方先排好（分数降序），所以留下的永远是分数高的那条——与"哪个源先
    抓到"无关。13 对实测重复（跨源同一篇、同源同文异址）在 0.66–1.00，去重阈值
    0.60 一条不漏；而"同主题不同文"的最高一对在 0.40 以下，不会被误并。
    """
    kept: list[ExternalHit] = []
    dropped = 0
    for hit in ranked:
        if any(_similarity(hit, other) >= threshold for other in kept):
            dropped += 1
            continue
        kept.append(hit)
    return kept, dropped


def _select_hits(
    query: str, hits: Sequence[ExternalHit], limit: int, *,
    threshold: float = DEDUPE_THRESHOLD, diversity: float = DIVERSITY_LAMBDA,
) -> list[ExternalHit]:
    """全量候选 → 打分 → 排序 → 去重 → 按内容多样性挑前 `limit` 条。

    **没有任何按源的配额**：既不保底（"每个源至少一条"），也不封顶（"每个源最多几条"）。
    数据说封顶是有害的——同一个池子上加"每源最多 ceil(limit/2)"之后，`limit=8` 的相关
    条数从 42 掉到 36、噪声从 10 涨到 14：openalex/crossref 恰好是 `城市雨污分流改造`
    那类工程主题唯一的相关来源，封顶等于把它们挤掉、让 deepseek 的商品页补位。

    "多样性"由两步保：去重（同一条资料只留一次）+ MMR（每选一条就按"与已选内容最
    相似"扣分）。两步都只看内容，不看源。它们管的是"别拿三条讲同一件事的材料"；
    "三条来自同一个源但内容不同"是允许的——那正是"好材料恰好都在一处"时的正确输出。

    顺序稳定：同分时保持上游（源站 + 收集顺序），不引入随机。

    打分为 `SCORE_FLOOR`（没有任何词面证据）的候选**不参与**：它是"门"那条契约的
    分级版本，全站跑题时返回空列表（见 `SCORE_FLOOR`）。
    """
    if limit <= 0 or not hits:
        return []
    # 地板在排序之前：先把它滤掉，后面的 dedupe/MMR 只在"有一条证据"的候选之间做，
    # 否则一条零证据的候选会占掉一个名额、也占掉一次相似度比较。
    scored = [hit for hit in hits if score_hit(query, hit) > SCORE_FLOOR]
    scored.sort(key=lambda hit: -score_hit(query, hit))
    unique, dropped = _dedupe(scored, threshold=threshold)
    if dropped:
        logger.debug("外部检索：内容去重并掉 %d 条（阈值 %.2f）", dropped, threshold)
    picked: list[ExternalHit] = []
    pool = list(unique)
    while pool and len(picked) < limit:
        best_index = 0
        best_value: float | None = None
        for index, hit in enumerate(pool):
            overlap = max((_similarity(hit, other) for other in picked), default=0.0)
            value = score_hit(query, hit) - diversity * overlap
            if best_value is None or value > best_value:
                best_value, best_index = value, index
        picked.append(pool.pop(best_index))
    return picked


def _run_source(name: str, fetch: Any, budget: _Budget | None = None) -> list[ExternalHit]:
    """跑一个源，**永不抛**。

    每个源自己已经吞过异常；这里再兜一层是因为"某个源写错一个字段名"不该让
    整天生成挂掉——而 `AttributeError` 恰恰不会被源内部的 `except Exception` 之外
    的任何地方拦住。

    `budget` 只用来记一次失败（`note_failure`）：这一层捕获的是源自己**漏网**的
    异常，它同样是"这个源这次没给出东西"，对 `_worth_caching` 的判据与源内部的
    失败没有区别。给成可选是为了让"只想换个源函数试试"的调用点不必造一个预算。
    """
    try:
        return list(fetch())
    except Exception as exc:  # noqa: BLE001 — 外部源失败只降级，理由见模块 docstring
        if budget is not None:
            budget.note_failure()
        logger.warning("外部检索：%s 失败（%s）：%s",
                       name, type(exc).__name__, str(exc)[:160])
        return []


# 单方向检索的源清单与顺序。顺序现在只影响**同分时的稳定次序**（`_select_hits`
# 的 `sorted` 是稳定的，同分保持收集顺序）与 `data.external_used` 里那句话的措辞，
# 不再决定"谁能拿到名额"——名额由分数决定，与源无关。
#
# `deepseek` 排在最后一条历史遗留：它曾经需要独立席位（`_interleave` 轮不到第六个
# 桶），现在不需要了，但顺序本身没有理由改，改了只是让"同一批候选的展示顺序"变一次。
DEFAULT_SOURCES: tuple[str, ...] = (
    "wikipedia", "arxiv", "openalex", "crossref", "baike", DEEPSEEK_SOURCE)


def _source_available(name: str) -> bool:
    """这个源这次参不参加。

    只有 DeepSeek 会被拦：它是唯一一个**默认关**的源（理由见 `DEEPSEEK_SEARCH_ENV`）。
    拦在这里而不是拦在 `DEFAULT_SOURCES` 上，是因为那个元组同时是"顺序"的声明
    ——按开关裁一个常量会让顺序变成运行时的，而 `_interleave` 的稳定性依赖它是常量。
    """
    if name == DEEPSEEK_SOURCE:
        return deepseek_search_enabled()
    return True


def _search_one(
    query: str, limit: int, *, budget: _Budget, slot: _Slot | None = None,
    sources: Sequence[str] = DEFAULT_SOURCES, exact_first: bool = True,
    fetch_limit: int | None = None,
) -> list[ExternalHit]:
    """打**一轮**源，返回全部候选：**不筛、不去重、不截断**。

    这是两条路共用的那一轮（单方向 6 源、多方向的每个方向 4 源）。出口的三件事
    留给调用方，因为它们的位置不同：

      - 单方向：聚合全部候选 → `_select_hits` 打分排序（**不过门**，理由见
        `_search_single`）；
      - 多方向：先按**方向**过门 → 跨方向合并去重 → 方向内按源交替 → 跨方向轮转。

    并行在这里而不是在调用方：串行时最坏耗时是六个 10 秒超时相加，而它们都只是
    "多等一会儿"，没有任何顺序依赖。多方向时每个方向各有一个线程池，共享同一份
    `_Budget`（全局 + 槽位两个维度，见 `_Budget.take`）。

    **DeepSeek 也在这一个线程池里**，所以六源是并行的、耗时取最慢的那个而不是相加。
    它自己内部最多再发 3 次补抓（`_fill_missing_content`），那是**串行**的：一次
    Search 的响应里有几条空正文，就依次补几次。串行的理由是它们共享 `limit` 条候选
    的额度，并发补抓只会让"预算够几条"变成竞态。
    """
    registry: dict[str, Any] = {
        "wikipedia": lambda: _wikipedia(
            query, budget, limit, slot=slot, exact_first=exact_first,
            fetch_limit=fetch_limit),
        "arxiv": lambda: _arxiv(query, budget, limit, slot=slot),
        "openalex": lambda: _openalex(query, budget, limit, slot=slot),
        "crossref": lambda: _crossref(query, budget, limit, slot=slot),
        "baike": lambda: _baike(query, budget, limit, slot=slot),
        DEEPSEEK_SOURCE: lambda: _deepseek_search(query, budget, limit, slot=slot),
    }
    if slot is None and exact_first and fetch_limit is None:
        # 单方向路径：按**三个位置参数**调用，与 `3c40ba6` 的形态完全一样。
        # 这不是为了好看——既有用例（`tests/test_external_relevance.py` 的
        # `_bare_sources`）把五个源替换成 `lambda q, budget, limit: ...`，多传一个
        # kwarg 就会 TypeError。而"降级与基线逐字节一致"本来也包括**调用形态**。
        registry = {
            "wikipedia": lambda: _wikipedia(query, budget, limit),
            "arxiv": lambda: _arxiv(query, budget, limit),
            "openalex": lambda: _openalex(query, budget, limit),
            "crossref": lambda: _crossref(query, budget, limit),
            "baike": lambda: _baike(query, budget, limit),
            # DeepSeek 在这里**保持带 kwarg 的形态**：它是这次新加的源，没有既有的
            # `lambda q, budget, limit` 替身要迁就；而 `slot=None` 时 `slot=slot`
            # 与不传是同一件事。
            DEEPSEEK_SOURCE: lambda: _deepseek_search(query, budget, limit, slot=slot),
        }
    planned = [(name, registry[name]) for name in sources
               if name in registry and _source_available(name)]
    collected: list[ExternalHit] = []
    if not planned:
        return collected
    with ThreadPoolExecutor(max_workers=len(planned)) as pool:
        futures = [pool.submit(_run_source, name, fetch, budget)
                   for name, fetch in planned]
        for (name, _), future in zip(planned, futures):
            hits = future.result()
            collected.extend(hits)
            logger.debug("外部检索：%s 返回 %d 条（累计请求 %d 次）",
                         name, len(hits), budget.used)
    return collected


# ---------------------------------------------------------------- 相关性门

# 判定用的三个数：看多长的正文、英文词多短算噪声、中文要求几个字。判据本身见
# `_relevant_hits`，这几个数各自的来历见紧跟着的注释与 `_phrase_terms`。

# 只看**标题 + 正文开头**。正文可以长到 `TEXT_MAX_CHARS`（8000）字，而判"这条讲的
# 是不是 query 那件事"只需要开头：跑题命中在开篇就暴露了（实测 `rust 学习路线` 拿到
# 「陳淑樺」，正文第一段就是歌手生平）。300 不是新数字——它是 `LEAF_MAX_CHARS` 与
# `SourceRef.quote.max_length` 的同款口径，也就是"一条引用装得下的字数"：门放行的
# 深度与引用能取证的深度重合，更靠后的正文本来就长不出一句可校验的引用。
RELEVANCE_CHARS = 300

# 参与判定的英文词最短长度。1–2 字符的词在中文主题的正文里是纯噪声（`ai`、`os`、
# `ui`、`no`），命中它们说明不了任何事；3 是全大写缩写（TCP / GPT / OCR）的下界。
MIN_ASCII_TERM = 3

# 中文判定词块的长度：query 的中文段去掉开头虚词后，要求**头这么多个字**原样出现。
# 3 的来历见 `_phrase_terms` 的 docstring（2 字等于 bigram 口径，更长会打死
# "概念 + 限定语"这一类查询）。
MIN_PHRASE_CHARS = 3

# 只认"以字母开头"的英文词。`2024` / `3` 这种纯数字在任何正文里都有，拿它当相关
# 证据等于没判。小写是因为 `_token_list` 已经把英文词全部小写化。
_ASCII_TERM_RE = re.compile(r"^[a-z][a-z0-9]*$")

# query 的中文词块 = 连续的 CJK 段。这是**切段**不是分词：真正的分词（中文 bigram /
# 英文整词）只有 `_token_list` 一套，这里只负责把"中英混排"里的中文部分整段取出来，
# 好让"移动语义"不被 `C++` 的空格切成两截。与 `_CJK_RE` 同一个字符区间。
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")

# 句首虚词：出现在查询词块开头时没有判别力、要先跳过的字。
#
# 用途只有一处（`_phrase_terms`）：`为什么中文大模型…` 的前三个字全是虚词，不跳过
# 的话判据会去匹配"为什么"，一篇讲足球的文章只要用了这三个字就能混进来。
#
# 为什么这张表**不含** `所`/`最`/`一`/`因`/`更`/`太`/`很`：它们同样是实词的开头
# （`所有权`、`最终一致性`、`一致性`、`因果`、`更新`、`太阳`、`很少`）。混在一起
# 会把 `所有权` 剥成 `有权`、把 `一致性` 剥成 `致性`——判据要求的那 3 个字随之变弱，
# 噪声正好从这些缝里进来。
#
# 跳过句首虚词对召回没有代价：命中若含整个词块，必然也含跳完前缀后的那一段
# （子串关系），所以这一步只会放宽"命中只给出核心词条"的情形。
_LEADING_FILLER_CHARS = frozenset(
    "的了着过是在和与及或而就也都还又再把被从对向为以于其之这那个们"
    "我你他她它么呢吗吧啊呀嘛哦什怎"
)


def _word_terms(text: str) -> frozenset[str]:
    """一段文字里的英文词（小写、≥`MIN_ASCII_TERM`、以字母开头）。

    分词**直接用 `knowledge.service._token_list`**（BM25 那条路用的同一份实现），
    不在这里另写一套：门与检索对"什么算同一个词"必须只有一个答案，两套实现迟早
    分叉，而分叉的表现是"检索认为命中、门认为无关"这种谁也说不清的自相矛盾。
    """
    words: set[str] = set()
    for token in _token_list(text):
        if (token.isascii() and len(token) >= MIN_ASCII_TERM
                and _ASCII_TERM_RE.match(token)):
            words.add(token)
    return frozenset(words)


def _phrase_terms(text: str) -> frozenset[str]:
    """query 的判定词块：每个中文连续段**去掉开头虚词后的头 `MIN_PHRASE_CHARS` 个字**。

    为什么不是"整块出现"（第一版）：`碳中和技术路径` 的命中是维基的 `碳中和` 词条，
    整块判据会把它判成无关——而"概念 + 限定语"是最常见的查询形状（`碳中和 技术路径`、
    `Rust 所有权`、`深度学习 路线`）。既有用例
    （`test_wikipedia_falls_back_to_search_when_title_misses`）钉的正是这件事：
    搜不到精确标题时退回条目检索，`碳中和` 必须还能用。

    为什么是**前缀**而不是任意 2 字窗口（任务最初给的 bigram 口径）：中文查询的
    修饰语在前、中心词在后，命中往往只给出核心词条。任意窗口等于把尾部那个最泛的
    中心词也当成证据——`一致性` 会命中"无线电一致性测试"、`学习路线` 会命中
    "深度学习路线"的教学模式论文。取最左的内容字，留下的恰好是限定语。

    为什么是 3 个字：2 字重合等于 bigram 口径（实测噪声一条不少、还误杀简繁）；
    再长就把"概念 + 限定语"整类打死。3 是"一个中文实词"的下界，也是缩写
    （`碳中和`/`注意力`）的长度。

    开头虚词要先跳过去：`为什么中文大模型…` 的前 3 个字全是虚词，不跳的话判据会
    去匹配"为什么"，一篇讲足球的文章只要用了这三个字就能混进来。跳完之后要求
    `中文大` 出现——那才是这个查询的内容起点。
    """
    phrases: set[str] = set()
    for run in _CJK_RUN_RE.findall(text):
        start = 0
        while start < len(run) and run[start] in _LEADING_FILLER_CHARS:
            start += 1
        core = run[start:]
        if len(core) < 2:
            # 整段都是虚词（`的了`、`为什么`）：没有可判的内容，不参与比对。
            continue
        phrases.add(core[:MIN_PHRASE_CHARS])
    return frozenset(phrases)


def _hit_blob(hit: ExternalHit) -> str:
    """判定用的命中文本：标题 + 正文前 `RELEVANCE_CHARS` 字（理由见那个常量）。"""
    return f"{hit.title}\n{hit.text[:RELEVANCE_CHARS]}"


def _qualifier_terms(text: str) -> tuple[frozenset[str], frozenset[str]]:
    """限定语的判定词面：**全部中文 2-gram** + 全部英文整词。

    与 `_phrase_terms` 的"只取头 3 字前缀"不同：这里要判的是"命中里有没有提到这个
    限定语"，限定语本身只有 2-4 个字（`政策`、`技术路径`、`企业案例`），取前缀会
    把「技术路径」压成「技术路」——一个几乎不可能原样出现的串。滑 2-gram 之后
    「路径」「案例」这些真实会被写进标题的词才留得住。

    代价是 2-gram 本来就宽（`路径` 在无关文章里也常见）。这是词面判据的天花板，
    与 `_relevant_hits` 是同一类取舍：宁可放过几条同形噪声，也不能把方向整轮判成空的。
    """
    phrases: set[str] = set()
    for run in _CJK_RUN_RE.findall(text):
        for index in range(len(run) - 1):
            gram = run[index:index + 2]
            if not all(char in _LEADING_FILLER_CHARS for char in gram):
                phrases.add(gram)
    return frozenset(phrases), _word_terms(text)


def _direction_qualifier(topic: str, direction: str) -> str:
    """方向里**比主题多出来的那部分**：`碳中和政策` → `政策`；`Rust 内存模型` → `内存模型`。

    这一步是"按方向施门"能成立的前提。方向按提示词的要求以主题原词开头，而
    `_phrase_terms` 只取头 3 字——`_phrase_terms("碳中和政策")` 恰好等于
    `_phrase_terms("碳中和")`。也就是说，直接拿方向去调 `_relevant_hits`，判据会
    **退化成主题门**：一条"讲碳中和但完全不讲政策"的命中照样通过。拆方向的意义
    就在这里丢掉。

    切除用的是主题的判定词块/英文词在方向里最长的那一次匹配（不是"去掉方向前
    若干字"）：模型偶尔会把主题写在后面（`政策视角下的碳中和`），按位置切就会
    切出一段没有意义的尾巴。
    """
    if not direction:
        return ""
    anchors = [phrase for phrase in _phrase_terms(topic) if phrase in direction]
    lowered = direction.lower()
    anchors += [word for word in _word_terms(topic) if word in lowered]
    if not anchors:
        # 方向不含主题原词（比如主题「碳中和」、方向「碳交易市场」）。这时方向的
        # 全文就是限定语——它得自己站得住。
        return direction
    anchor = max(anchors, key=len)
    position = lowered.find(anchor.lower())
    if position < 0:
        return direction
    rest = direction[:position] + " " + direction[position + len(anchor):]
    return rest.strip(" \t\u3000，,、·:：-|/（）()")


def _direction_gate(
    topic: str, direction: str, hits: Sequence[ExternalHit],
) -> list[ExternalHit]:
    """**按方向**过词面门：先过既有那道门，再要求命中提到这个方向的限定语。

    两道顺序不能换：第一道（`_relevant_hits(direction, ...)`）保证命中讲的是这个
    主题，第二道（限定语）保证它讲的是**这个方面**。只有第二道时，一条只含"政策"
    二字的无关文章也能进；只有第一道时，方向门退化成主题门（理由见
    `_direction_qualifier`）。

    第二道**不退回**：被限定语挡掉的命中不留作后备。理由与 `_relevant_hits` 的
    "被滤掉的槽位由候选池补位"不同——那里的候选池是同一次查询的候选，这里的后备
    是"另一个方向"的东西，挪过来只会让报告里"政策那个方向拿回了什么"变成谎话。
    代价是某个方向可能一条都不剩（实测里维基对「碳中和政策」就是这样），那由
    跨方向轮转从别的方向补名额，不是故障。
    """
    passed = _relevant_hits(direction, hits)
    qualifier = _direction_qualifier(topic, direction)
    if not qualifier:
        return passed
    phrases, words = _qualifier_terms(qualifier)
    if not phrases and not words:
        return passed
    kept: list[ExternalHit] = []
    dropped: list[str] = []
    for hit in passed:
        blob = _hit_blob(hit)
        if any(gram in blob for gram in phrases) or words & _word_terms(blob):
            kept.append(hit)
        else:
            dropped.append(f"{hit.source_name}/{hit.title[:40]}")
    if dropped:
        # 只记数量，不记方向文本：方向是主题的改写，泄漏程度与主题同级；而这条日志
        # 每个方向都会触发一次，把文本抄进去等于在 info 级别重复一遍主题。
        # "哪个方向挡了几条"由 `_search_many` 的汇总行回答（那是一行一条）。
        logger.info("外部检索：限定语闸挡掉 %d 条（方向与主题的差异部分共 %d 字）",
                    len(dropped), len(qualifier))
    return kept


def _relevant_hits(query: str, hits: Sequence[ExternalHit]) -> list[ExternalHit]:
    """按**词面**相关性过滤命中。判据见下方"判据"一节。

    ## 它现在只服务多方向路径（`_direction_gate`）

    单方向路径**不再拿它当门**，判据的位置从"过/不过"换成了"排在谁前面"
    （`score_hit`）。读数是直接的：同一份池子上，"门 + 打分"与"只打分"的噪声条数
    完全相同，而门额外挡掉 2–6 条相关；它挡掉的是"限定语换了说法"的那一类，
    `城市雨污分流改造` 一题 15 条相关候选里被它挡掉 12 条（`_phrase_terms` 只取
    中文段前 3 字，`城市雨` 不在「城市排水系统雨污分流改造技术研究」里）。

    保留它的两个理由：方向路径仍然靠它做"主题锚"（方向只有 1-2 条候选、没有排序
    余地，需要一道硬判据）；以及它下面这些实测记录是"为什么最终选了打分"的依据。

    ## 为什么需要这道门

    "搜到"不等于"搜准"：外部源按自己的相似度排序，而它们对中文主题的理解偏差
    很大。实测 `rust 学习路线` 拿到「陳淑樺」维基页与一篇卢托斯拉夫斯基音乐论文；
    `Rust 所有权` 拿到"农业"（rust 是锈病）；`C++ 移动语义` 三条里两条是语言学；
    `zzqx 不存在的词` 也照样返回 3 条 crossref 垃圾。这些命中随后**会变成引用**
    ——逐字可校验、能点回原文、界面上写着"依据来自外部检索"，而它们与主题毫无
    关系。整条链最怕的正是这种失败形态：不是没有依据，是一份看起来正规的假依据。

    ## 判据

    命中必须在 `标题 + 正文前 300 字` 里**原样包含** query 的某个中文词块
    （连续 CJK 段，≥2 字，非纯虚词），或包含 query 里的某个英文词
    （≥3 字符，大小写不敏感，按整词比）。两条任一成立即保留。

    ## 放行：判据说不出话时，沉默

    两类命中不过判据也**放行**：

      - **query 切不出任何可比词面**（纯符号、纯数字、`C++` 只剩一个被
        `MIN_ASCII_TERM` 裁掉的 `c`）：判据在这里没有表决权，把"判不了"当成
        "都不相关"会让这些主题一条资料都拿不到。
      - **跨语言**：query 与命中在书写系统上没有交集（中文 query 配全英文命中，
        或英文 query 配纯中文正文）。判据在那里无从比较，放行是为了不误杀
        （实测救回 `consensus algorithm` 的中文正文 `Raft`）；代价见"天花板"。

    ## 天花板（方法本身的上限，不是"没调好"）

    词面重合只能证明"两边出现了同一串字符"，不能证明"讲的是同一件事"。三类它
    永远分不开，实测各有实例（数据在 `scripts/measure_external_relevance.py`）：

      - **同形异义**：`Rust`（语言）/ `rust`（锈病）、`Cursor`（编辑器）/ `cursor`
        （光标）、`consensus`（共识算法）/ `Consensus`（RANSAC 的全名）、
        `学习路线`（学习规划）/ `深度学习路线`（教学模式）。判据看见字符就放行。
      - **简繁差异**：`用戶畫像` 不含 `用户画像`，一条真相关的维基条目会被滤掉
        （实测 1 条）。要修得引入简繁映射表，那是一个新的数据依赖，代价大于收益。
      - **跨语言**：中文 query 与全英文命中之间没有可比的字符。`注意力机制` 的相关
        命中 `Attention Is All You Need` 靠放行才留得住；同一条放行也让
        `Rust 所有权` 有可能收下纯英文的小麦锈病论文（实测未触发：那批命中带中文
        摘要，不走放行支路；arXiv 全英文语料会走）。

    ## 实测（`scripts/measure_external_relevance.py`，20 主题同一份候选池）

    | | 无门 | 前缀 3 字 + 放行（本实现） |
    |---|---|---|
    | 返回条数 | 60 | 49 |
    | 噪声条目（人工判定） | 22 | 10 |
    | 三条全相关的主题 | 10/20 | 12/20 |
    | 成功率（≥1 条） | 100% | 95% |
    | 误杀（判为相关却被滤） | 0 | 1 |

    同一份池子上另外两条口径的成绩，是"为什么不是它们"的依据：

    | | 返回条数 | 噪声 | 三条全相关 | 成功率 | 误杀 |
    |---|---|---|---|---|---|
    | bigram（任务最初给的口径） | 59 | 22 | 9/20 | 100% | 2 |
    | 整块词块（第一版） | 47 | 9 | 11/20 | 85% | 2 |

    bigram 口径**噪声一条没少、三条全相关还掉了一个**：它挡下的只是"完全没共享
    词面"的少数命中，而源站自己的排序已经很少返回那种东西；它唯一的"功劳"是误杀
    ——`用戶畫像`（繁体）不含 `用户画像`（简体）。整块口径多砍了一条噪声，却把
    `碳中和技术路径 → 碳中和` 这类"概念 + 限定语"查询整类打死（成功率掉到 85%）。

    残余的 10 条噪声里 **9 条是靠词面命中的**（`幻觉` 的戏曲与决定论论文、`晶体学`
    论文——正文写着"最终一致性因子为R=0.030"、`Rust` 的小麦锈病、`cursor` 的光标
    论文、`学习路线` 的教学模式），另外 1 条是跨语言放行进来的。这道门一条都挡不住
    ——那是词面判据的天花板，不是阈值问题。

    要跨过这条线得用语义判据：`knowledge/rerank.score_documents(query, documents)`
    已经能给出真正的相关性分数（`qwen3-rerank`）。**量过了，没接**——接上之后
    （同一份候选池、同一批 20 个主题、分数真打 `qwen3-rerank`）的读数是：

    | | 返回条数 | 噪声 | 三条全相关 | 成功率 | 重复标题 |
    |---|---|---|---|---|
    | 词面门（本实现） | 49 | 10 | 12/20 | 95% | 3 |
    | 词面门 + rerank | 49 | 9 | 12/20 | 95% | 5 |

    噪声少 1 条、三条全相关一条没动，代价是每次外部检索多 0.74 秒中位（合计中位
    4.20→5.08 秒）、重复标题 3→5（重排取代 `_interleave` 之后，同一标题来自两个源
    时两条都会进结果）。按"全相关 16/20 以上、噪声 5 条以下"的判据，这叫"只动了一
    两条"。

    根因有三条，都不是调阈值能解决的：

      - **重排在最脏的地方没机会开口**：门槛是"过门候选 > `limit`"，而最脏的三个
        主题过门候选恰好是 3 / 1 / 0 条，一次都不触发；
      - **16/20 是候选池的物理上限**：池内一条相关候选都没有的主题有 4 个
        （`加密货币的量子套利`、`为什么中文大模型…`、`rust 学习路线`、`zzqx`），
        任何排序或阈值都救不回来，得换源或扩源；
      - **绝对分数跨查询不可比**：晶体学论文 0.7108 比 `TCP 三次握手` 的真依据
        （0.4738）、`Rust 所有权` 的真依据（0.5635）都高，切它必然切掉别的主题的
        真依据；同一主题内用**相对**阈值（≥0.90×top1）才分得开，而那一格是
        15/20、4 条噪声、返回条数从 49 腰斩到 27。

    真要走语义判据，它是**替换**这道门而不是叠加：实测里"不要词面门、只按分数阈值
    筛"与"门 + 重排 + 阈值"在每一个阈值上的读数完全相同。数字与逐条读数在
    `scripts/measure_external_relevance.py`（`--rerank` / `--sweep`）。
    """
    phrases = _phrase_terms(query)
    words = _word_terms(query)
    if not phrases and not words:
        return list(hits)
    # 跨语言放行的判据：query 与命中的**书写系统**有没有交集。中文 query 配一条全
    # 英文命中时，两边没有任何可比的字符——那时判据说不出话，让它表决等于用"拼写
    # 不同"判"内容无关"。实测放行这一支救回了 `consensus algorithm` 那条中文正文的
    # `Raft`（不放行时它被误杀，名额转手给一条 RANSAC 噪声）。
    #
    # 代价要说清：全英文命中在中文 query 下一律放行，`Rust 所有权` 若拿到一篇纯英文
    # 的小麦锈病论文也会进来。实测这批 OpenAlex 命中都带中文摘要（不走这一支），
    # 所以没有触发；arXiv 是全英文语料，会走这一支。这个洞是"词面判据跨语言无定义"
    # 的直接后果，不是实现取舍。
    query_writes_cjk = bool(_CJK_RUN_RE.search(query))
    kept: list[ExternalHit] = []
    dropped: list[str] = []
    for hit in hits:
        blob = _hit_blob(hit)
        if any(phrase in blob for phrase in phrases) or words & _word_terms(blob):
            kept.append(hit)
            continue
        if bool(_CJK_RUN_RE.search(blob)) != query_writes_cjk:
            kept.append(hit)
            continue
        dropped.append(f"{hit.source_name}/{hit.title[:60]}")
    if dropped:
        # info 而不是 debug：这道门**扔掉的是要变成引用的东西**，扔了哪几条必须
        # 在日志里查得到，否则"这次怎么一条依据都没有"永远无从复盘。
        logger.info("外部检索：%r 过滤掉 %d 条词面无关命中（%s）",
                    (query or "")[:50], len(dropped), "；".join(dropped[:5]))
    return kept


def search_external(
    query: str, *, limit: int = DEFAULT_LIMIT, directions: Sequence[str] | None = None,
) -> ExternalHits:
    """去外部源搜一次，返回最多 `limit` 条命中。**失败返回空列表，永不抛。**

    返回值是 `ExternalHits`（`list` 的子类）：`cached` 说明这次是命中缓存还是真搜了
    一次网。它给 `data.external.cached` 用，也是"这次为什么这么快"的唯一解释。

    这是整条外部检索的入口，两条路：

      - **单方向**（默认；`directions` 只有一条，或开关关着，或拆方向失败）：五源
        （外加默认关的第六个 `deepseek`）并行打同一个 query，
        `_request_limit(limit)` 次请求 / 20 秒，出口**打分排序**（`_select_hits`：
        按相关度排、内容去重、按内容多样性挑），不再有按源的配额，也不再有一道
        过/不过的词面门（理由见 `_search_single`）。
      - **多方向**（`MINDMAP_EXTERNAL_DIRECTIONS=1` 且拆出 2-4 个方向）：每个方向各打
        一轮 `DIRECTION_SOURCES`，15 次请求 / 20 秒、每方向 5 次，门按**各自的方向**施加，
        合并去重后跨方向轮转。**默认关**，理由与实测见 `DIRECTIONS_ENV`。

    源并行跑：串行时最坏耗时是六个 10 秒超时相加，而它们都只是"多等一会儿"，
    没有任何顺序依赖。并行之后墙钟上界由 `DEADLINE_SECONDS` 那一份预算兜住。

    源的分工（都是实测通过的，可抓性报告见模块 docstring 的"外部源清单"）：

      - `wikipedia` / `baike`：中文百科，给"这个词条是什么"；
      - `arxiv` / `openalex` / `crossref`：论文，给"这个方向研究到哪了"。
        其中 arXiv 对中文 query 基本 0 条（语料是英文），OpenAlex 与 Crossref
        能吃中文 query 并返回中文论文——所以中文检索不靠翻译；
      - `deepseek`（默认关）：Search 工具 API，一次往返带回整页正文，给"技术方向 /
        流程 / 方案 / 现状"。它占 `limit` 里的 1 席，五源占其余。

    用时与条数各记一行 info：这是用户点"生成"之后**多出来的一次外网等待**，
    混在总耗时里就永远看不出"慢在哪"（与 `_retrieve_topic` 的同一条规矩）。

    `directions` 是给调用方（测量脚本、用例）**注入**方向的出口：不传就自己拆
    （`plan_directions`），传了就不调模型——拆方向的效果可以脱离模型单独量。

    ## 缓存：什么时候用它，什么时候绕开

    命中就整段跳过 HTTP（`external_cache`，24 小时 / 归一化 query），但**后续链路一条
    都不少**：转 `Source`、入库、检索、引用校验全在调用方，缓存命中时照常走。

    三个判断值得单独说：

      - `directions is not None` → **不读也不写**。那是测量脚本与用例的出口：它们要
        真实耗时与真实候选池，命中缓存会让"这次检索花了几秒"变成 0、让 A/B 两组拿到
        同一份池子（`scripts/measure_external_relevance.py` 的 `--collect-single` 与
        `--collect-directions` 正是这么调用的）。生产链路永远不传 `directions`。
      - 空结果也写缓存（`_worth_caching`）：区分"确实 0 条"与"源全挂了"是 `_Budget`
        记账的失败次数，不是返回值的形状。
      - 缓存写失败只是少一次加速，`external_cache.put` 自己吞异常并返回 False。
    """
    text = (query or "").strip()[:QUERY_MAX_CHARS]
    if not text or limit <= 0:
        return ExternalHits((), cached=False)

    use_cache = directions is None
    if use_cache:
        cached = external_cache.get(text, limit=limit)
        if cached is not None:
            records, hits_before = cached
            hits = [hit for hit in (_record_hit(record) for record in records)
                    if hit is not None]
            # `records` 与 `hits` 不等长说明某些行解不出（见 `_record_hit`）：那几行
            # 不明不白地少一条，比重新搜一次更糟——"这次只有 1 条资料"看起来像结论。
            if len(hits) == len(records):
                logger.info(
                    "外部检索：%r 命中缓存 %d 条（此前命中 %d 次，跳过全部 HTTP）",
                    text, len(hits), hits_before,
                )
                return ExternalHits(hits, cached=True)

    started = time.perf_counter()
    if directions is not None:
        planned = list(directions)
    elif _directions_enabled():
        planned = _plan_directions_sync(text)
    else:
        # 开关关着：零 LLM 调用、零额外分支，与 `3c40ba6` 完全一致。
        planned = [text]
    usable: list[str] = []
    for item in planned:
        direction = (item or "").strip()
        if direction and direction not in usable:
            usable.append(direction)
    usable = usable[:MAX_DIRECTIONS]

    if len(usable) <= 1:
        # 降级路径：**一个方向**。没有拆方向、拆出来只有一条、或拆失败——三者都走
        # 这里，且与 `3c40ba6` 的单方向实现逐字节相同（同一份预算、同一批源、同一
        # 道门、同一行日志）。"拆方向失败"不能顺带改变别的东西。
        run = _search_single(usable[0] if usable else text, limit, started)
    else:
        run = _search_many(text, usable, limit, started)

    if use_cache and _worth_caching(run):
        external_cache.put(
            text, limit=limit, records=[_hit_record(hit) for hit in run.hits])
    return ExternalHits(run.hits, cached=False)


def _worth_caching(run: _SearchRun) -> bool:
    """这次结果该不该记成结论。

    两种情况写：

      - **拿到了东西**（`hits` 非空）。哪怕某几个源失败了：返回的这批是这次检索的
        真实产出，24 小时内重搜一遍多半仍是这一批（源站的排序不会隔天翻面）；
      - **一条都没拿到，但没有一个源失败**（`failures == 0 and requests > 0`）。
        那是"搜过，确实 0 条"——一个有效结论。`requests > 0` 是必要条件：一次请求
        都没发出去（`limit <= 0` 之外的情况：预算被 `DEADLINE_SECONDS=0` 提前耗光）
        不能算"查过"。

    反过来，**全源失败的空结果不写**：代理没开、外网被封时所有源一起超时，把它缓存
    24 小时意味着"网络恢复了但一天没结果"——而用户重试的正是那一刻。部分失败但
    仍拿到了东西时会写（见上），代价是那几个失败源的内容在 TTL 内缺失；这个取舍的
    方向是明确的：宁可少一条资料，也不要让用户等两次一样长的等待。
    """
    if run.hits:
        return True
    return run.failures == 0 and run.requests > 0


# 缓存 ↔ `ExternalHit` 的字段转接。**只搬 `ExternalHit` 自己声明的字段**：`Source`
# 的 id / content_hash / status 每次都重新生成（`library.ingest` 会重算），存下来下次
# 也用不上，而存了它们会让"缓存里那份是不是就是库里那份"变成一个新问题。
#
# `site` / `authority` / `rerank_score` 必须在这里：它们只由 DeepSeek 的响应提供，
# 缓存命中时**没有第二次机会**去问源站——漏一个字段，第二次生成的材料清单就少一个
# "这条来自哪个站"与两个排序元数据，而界面上分不出"这条本来就没有"与"缓存吃掉了"。
# 三个都有默认值，所以旧缓存行（没有这三个键）仍然解得出来。
_HIT_FIELDS: tuple[str, ...] = (
    "title", "text", "url", "source_name", "fetched_at", "language", "raw_chars",
    "direction", "site", "authority", "rerank_score",
)


def _hit_record(hit: ExternalHit) -> dict[str, Any]:
    """`ExternalHit` → 可 JSON 化的 dict。`direction` 一起存：多方向检索时它是
    "这条命中来自哪个方面"，丢了它重建出来的 `ExternalHit` 与第一次的不等。"""
    return {name: getattr(hit, name) for name in _HIT_FIELDS}


def _record_hit(record: dict[str, Any]) -> ExternalHit | None:
    """dict → `ExternalHit`；形状不对返回 None（调用方据此整条缓存作废）。

    **按最严的写法校验**：缓存表是本地 sqlite，可能被旧版本写过、被人手动改过、
    被磁盘故障截断过。`title` / `text` / `source_name` 缺了或不是字符串，这条命中
    就过不了 `_hit_blob` 与 `_external_source`（`filename` 要拼字符串）；`raw_chars`
    给了个字符串会让 `max(hit.raw_chars, len(text))` 抛 `TypeError`——那会从
    "少一条资料"升级成"整次生成失败"，而这两件事没有因果关系。

    三个新字段（`site` / `authority` / `rerank_score`）走**同一套宽容降级**而不是
    整条作废：它们只影响显示与排障，缺了不影响这条命中能不能当依据。旧缓存行没有
    这三个键，正是这条规则要接住的情形。
    """
    title = record.get("title")
    body = record.get("text")
    source_name = record.get("source_name")
    if not isinstance(title, str) or not isinstance(body, str):
        return None
    if not isinstance(source_name, str) or not source_name:
        return None
    url = record.get("url")
    fetched_at = record.get("fetched_at")
    language = record.get("language")
    direction = record.get("direction")
    raw_chars = record.get("raw_chars")
    site = record.get("site")
    if not isinstance(url, str):
        url = ""
    if not isinstance(fetched_at, str) or not fetched_at:
        fetched_at = _now()
    if not isinstance(language, str) or not language:
        language = _guess_language(title)
    if not isinstance(raw_chars, int) or isinstance(raw_chars, bool) or raw_chars < 0:
        raw_chars = len(body)
    if not isinstance(site, str):
        site = ""
    return ExternalHit(
        title=title, text=body, url=url, source_name=source_name,
        fetched_at=fetched_at, language=language, raw_chars=raw_chars,
        direction=direction if isinstance(direction, str) else "",
        site=site[:120],
        authority=_deepseek_count(record.get("authority")),
        rerank_score=_deepseek_float(record.get("rerank_score")),
    )


def _plan_directions_sync(topic: str) -> list[str]:
    """同步地拆方向。失败/超时/没配置时 `plan_directions` 自己返回 `[topic]`。

    `search_external` 是同步函数（调用方 `agent.tools._external_sources` 用
    `asyncio.to_thread` 把它丢进线程），所以这里要自己造事件循环。

    已经在事件循环里的情况（有人直接在协程里调 `search_external`）**另起一个线程**：
    `asyncio.run` 在运行中的 loop 里会抛 `RuntimeError`，而把异常当成"拆方向失败"
    吞掉是错的——那会让一个调用方式的错误静默退化成"永远只有一个方向"。
    """
    coro = plan_directions(topic)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return list(asyncio.run(coro))
    with ThreadPoolExecutor(max_workers=1) as pool:
        return list(pool.submit(asyncio.run, coro).result())


def _search_single(query: str, limit: int, started: float) -> _SearchRun:
    """单方向检索：全部候选 → 打分 → 排序 → 去重 → 按内容多样性取前 `limit` 条。

    单独成函数是为了让"降级"有唯一的落点：`search_external` 里任何一条拆方向失败
    的支路都汇到这里，而不是各自复制一份预算与选择器。

    **出口从"每源轮流取 + 给 DeepSeek 1 席"换成了 `_select_hits`**。这两条老规则
    的共同前提是"没有相关性判据，所以用源配额代替判据"；判据有了之后它们只剩成本：
    同一份池子上，打分排序在 `limit=3` 给 17 相关 / 2 噪声，老规则给 14 / 6。

    返回 `_SearchRun` 而不是裸列表：`search_external` 要用后两个数判断"这个空结果
    该不该缓存"（`_worth_caching`），而预算对象里的两个计数只有这里拿得到。
    """
    budget = _Budget(_request_limit(limit), DEADLINE_SECONDS)
    collected = _search_one(query, limit, budget=budget)

    # **不再有词面门**。这句话读起来像删保护，其实是换了一个位置：判据从"过/不过"
    # 变成"排在谁前面"，而两道口径的读数差得很干脆——同一份池子上，"门 + 打分"与
    # "只打分"在 `limit=3/6/8/12` 上的**噪声条数完全相同**（2/6/10/14），而门额外
    # 挡掉了 2–6 条相关（`limit=8` 时 42→40，`limit=12` 时 63→57）。也就是说门在
    # 打分之后只剩误杀这一个效果。
    #
    # 它误杀的是"限定语换了个说法"的那一类，最典型的是 `城市雨污分流改造`：
    # `_phrase_terms` 只取中文段的前 3 字（`城市雨`），于是「城市排水系统雨污分流
    # 改造技术研究」「城市内涝治理与雨污分流改造措施」这些**逐字讲这件事**的论文
    # 全被判无关——实测 15 条相关候选里有 12 条被它挡掉。门原来挡住的噪声，打分排序
    # 自己就会把它们排到最后。
    hits = _select_hits(query, collected, limit)
    logger.info(
        "外部检索：%r 命中 %d 条（%s），候选 %d 条，请求 %d 次，耗时 %.2fs",
        query, len(hits), ", ".join(
            f"{hit.source_name}/{hit.title}/score={score_hit(query, hit):.2f}"
            for hit in hits[:3]),
        len(collected), budget.used, time.perf_counter() - started,
    )
    return _SearchRun(hits, budget.used, budget.failures)


def _search_many(
    topic: str, directions: Sequence[str], limit: int, started: float,
) -> list[ExternalHit]:
    """多方向检索：方向之间并行，每个方向各自过门，合并去重后跨方向轮转。

    五件事的先后顺序都不能换，理由见模块 docstring 的"多方向检索"一节：

      1. 每方向一轮，共享 `MULTI_REQUEST_LIMIT` 全局预算 + `DIRECTION_REQUEST_LIMIT`
         单方向预算；
      2. **按方向**过词面门（`_direction_gate`：主题锚 + 该方向的限定语，不是按主题）；
      3. 合并后按 URL 去重（不同方向命中同一条维基/论文是常态）；
      4. 方向内按源交替（`_interleave`），每方向留 `DIRECTION_HIT_LIMIT` 条；
      5. 跨方向轮转（`_interleave_directions`）取最终 `limit` 条。

    门放在"合并去重"之前还是之后？这里放在**之前**：一条命中属于哪个方向，只有
    那个方向的候选池知道；合并之后它已经变成"某条来自方向 2 的命中"，再用方向 2
    去判它就得把方向标签跟着候选走一遍——那是同一个判断的第二种写法。
    """
    budget = _Budget(MULTI_REQUEST_LIMIT, DEADLINE_SECONDS)
    slots = [_Slot(str(index), DIRECTION_REQUEST_LIMIT) for index in range(len(directions))]

    def run(index: int) -> list[ExternalHit]:
        direction = directions[index]
        candidates = _search_one(
            direction, DIRECTION_HIT_LIMIT, budget=budget, slot=slots[index],
            sources=DIRECTION_SOURCES, exact_first=False,
            fetch_limit=DIRECTION_WIKI_FETCH,
        )
        kept = _direction_gate(topic, direction, candidates)
        logger.debug("外部检索：方向 #%d 候选 %d 条过门 %d 条（该方向已用 %d 次请求）",
                     index, len(candidates), len(kept), budget.slot_used(slots[index].key))
        return [replace(hit, direction=direction) for hit in kept]

    with ThreadPoolExecutor(max_workers=max(len(directions), 1)) as pool:
        per_direction = list(pool.map(run, range(len(directions))))

    merged, duplicated = _dedupe_by_url(per_direction)
    groups: list[list[ExternalHit]] = []
    for group in merged:
        groups.append(_interleave(group, DIRECTION_HIT_LIMIT,
                                  prefer_language=_query_language(topic)))
    hits = _interleave_directions(groups, limit)
    logger.info(
        "外部检索：方向 %d 个（%s）命中 %d 条（%s），候选 %d 条去重掉 %d 条，请求 %d 次，耗时 %.2fs",
        len(directions), "、".join(directions), len(hits),
        ", ".join(f"{hit.direction}/{hit.source_name}/{hit.title[:24]}" for hit in hits[:3]),
        sum(len(group) for group in per_direction), duplicated, budget.used,
        time.perf_counter() - started,
    )
    return _SearchRun(hits, budget.used, budget.failures)


def _hit_key(hit: ExternalHit) -> str:
    """去重用的身份：**URL 优先**，没有 URL 时退回 `(source_name, title)`。

    URL 要先去 fragment、去尾部斜杠：同一篇论文在两个方向上可能一个带 `#abstract`、
    一个不带；那是同一个落地页。没有 URL 的命中（快懂百科在拿不到落地地址时、以及
    各种样本）用源名 + 标题兜底——标题在两个方向上都一样时，那本来就是同一条。
    """
    url = (hit.url or "").strip().split("#", 1)[0].rstrip("/")
    if url:
        return url
    return f"{hit.source_name}\x00{hit.title}"


def _dedupe_by_url(
    groups: Sequence[Sequence[ExternalHit]],
) -> tuple[list[list[ExternalHit]], int]:
    """跨方向按 URL 去重：**保留首次出现的方向**，返回 `(各组, 去掉几条)`。

    保留首见而不是"合并方向标签"：一条命中只该有一个方向，否则报告里"政策那个方向
    拿回了什么"就说不清（一条命中同时挂在两个方向下，两条线都算它的功劳）。代价是
    "同一篇论文同时讲政策与技术"时第二个方向少一条——可接受，因为它本来就只该
    贡献一次。
    """
    seen: set[str] = set()
    kept: list[list[ExternalHit]] = []
    dropped = 0
    for group in groups:
        survivors: list[ExternalHit] = []
        for hit in group:
            key = _hit_key(hit)
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            survivors.append(hit)
        kept.append(survivors)
    return kept, dropped


def _interleave_directions(
    groups: Sequence[Sequence[ExternalHit]], limit: int,
) -> list[ExternalHit]:
    """跨方向轮流取：第 1 轮每个方向各取一条，第 2 轮再各取一条，直到 `limit`。

    为什么不是直接用 `_interleave`（按源分桶）：三个方向都命中维基时，维基桶里的
    前 3 条会占满名额，而它们很可能**全来自同一个方向**——拆了三个方向，返回的还是
    同一个方面。方向内那层 `_interleave` 保的是源多样，这一层保的是方面覆盖，两层
    各管一件事，缺一层都会退化成"某一维占满名额"。

    `groups` 里的一组的顺序已经由 `_interleave` 排好（源交替），所以这里只负责
    "先给谁名额"。空组自然跳过；全部取尽时提前结束。
    """
    merged: list[ExternalHit] = []
    depth = 0
    while len(merged) < limit:
        progressed = False
        for group in groups:
            if len(merged) >= limit:
                break
            if depth < len(group):
                merged.append(group[depth])
                progressed = True
        if not progressed:
            break
        depth += 1
    return merged


# ---------------------------------------------------------------- 拆方向


# 提示词。写法借 `query_plan.BRANCH_SYSTEM_PROMPT`：三条硬约束都用**正反例**
# 写成"可对照的形状"，不用形容词；例子只用「碳中和」这类通用词并明确说"不要抄
# 例子"，因为 2.3 那次编出的超导/离子阱正是"照着别处的词往下写"的产物。
#
# 与那个模块的差别是本质的：那边服务"在材料里找片段"（corrected/intents/hyde/
# subqueries + 四道防发散闸），这边服务"一个主题从几个方面查"。语义不同，所以
# 不复用 `plan_query`——复用会让两边互相绑死在对方的失败模式上。
DIRECTION_SYSTEM_PROMPT = (
    "你在为一次外部资料检索做规划：把一个主题拆成 2-4 个**检索方向**，每个方向各搜一轮。"
    "你只输出一个 JSON 对象，不要写解释、不要加代码围栏、不要在 JSON 前后写字。\n"
    "\n"
    "形状：{\"directions\": [\"方向1\", \"方向2\", \"方向3\"]}\n"
    "\n"
    "每个方向的写法要求：\n"
    "  - 6-20 个字，是可检索的短语，不是问句、不是整段话；\n"
    "  - 必须**原样包含主题的写法**（主题是「碳中和」，方向就写成「碳中和政策」）；\n"
    "  - 方向之间是主题的不同方面（政策 / 技术路径 / 企业案例 / 争议 / 产业），互不重复。\n"
    "\n"
    "三条硬规则，逐条守：\n"
    "\n"
    "1. 方向必须是主题的子方面，不能换领域。\n"
    "   ❌ 主题是「碳中和」，方向写成「超导量子比特」「离子阱」——那是别的领域。\n"
    "   ✅ 主题是「碳中和」，方向写成「碳中和政策」「碳中和技术路径」「碳中和企业案例」。\n"
    "\n"
    "2. 主题的写法逐字照抄，不要换同义词、不要翻译、不要改写。\n"
    "   ❌ 主题「碳中和」→ 方向「碳抵消」「温室气体净零排放」（换了说法，检索会换一批源）。\n"
    "   ✅ 主题「碳中和」→ 方向「碳中和政策」（原词在前，只追加限定语）。\n"
    "\n"
    "3. 不要编造主题里没有的机构、标准、文号、篇名、年份。\n"
    "   ❌ 「碳中和 GB/T 36132-2018 合规要求」——那个文号是你编的。\n"
    "   ✅ 「碳中和 标准与核算方法」。\n"
    "\n"
    "上面的例子只用来说明形状，不要把例子里的词写进你的结果。"
)


def _directions_message(topic: str) -> str:
    """这一次的用户消息：只有主题。**不塞材料**——这条路上没有材料可塞。

    没有材料时"锚定"全靠主题本身，所以提示词把"原样包含主题写法"写成硬规则，代码
    侧再用 `_direction_anchored` 逐条核对（`query_plan` 那条"没有材料就返回空"的
    代码闸在这里的等价物）。
    """
    return f"主题：{topic}\n\n只返回那个 JSON 对象。\n"


def _directions_payload(raw: Any) -> dict[str, Any] | None:
    """模型出口 → dict。`chat_async` 正常返回 dict；字符串形态再解析一次。

    与 `query_plan._payload` 同一条规矩（用同一个解析器收尾），但**不复用它的函数**：
    那是 query_plan 的私有实现，跨模块直接调私有名会让两个模块的改动互相牵制。
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


def _raw_directions(payload: dict[str, Any]) -> list[str]:
    """取出候选方向：认 `directions` / `queries` 两种键，外加一个裸数组形状。

    收多种键不是宽容，是"模型把数组换个名字"这件事的代价太低：为它丢掉一整次调用
    的产出，等于让整条路径的成功率下降一截。
    """
    for key in ("directions", "queries", "aspects"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str)]
    return []


def _direction_length_ok(text: str) -> bool:
    """方向的长度闸：中英混排按"折算字数"算，纯英文按词数算。

    折算规则与理由见 `DIRECTION_MAX_CHARS` 上面那段：一个英文词算两个字。纯英文
    方向再另设一道"2-8 个词、≤60 字符"的闸——`Byzantine fault tolerance threat
    models` 五个词是合法的，而一段整句会在词数上先暴露。
    """
    cjk = sum(len(run) for run in _CJK_RUN_RE.findall(text))
    words = _DIRECTION_WORD_RE.findall(text)
    if cjk and words:
        size = cjk + 2 * len(words)
        return DIRECTION_MIN_CHARS <= size <= DIRECTION_MAX_CHARS
    if cjk:
        return DIRECTION_MIN_CHARS <= cjk <= DIRECTION_MAX_CHARS
    if not words:
        return False
    return (DIRECTION_MIN_WORDS <= len(words) <= DIRECTION_MAX_WORDS
            and len(text) <= DIRECTION_MAX_ASCII_CHARS)


def _direction_anchored(topic: str, direction: str) -> str | None:
    """方向能不能被主题**词面锚住**；能就返回清洗后的方向，不能返回 None。

    这是"不许编造领域"的代码执行处。四条判据，任一成立即通过：

      1. 方向包含主题的判定词块（`_phrase_terms(topic)` 的头 3 字）——「碳中和政策」
         含「碳中和」；
      2. 方向包含主题里的英文整词（≥3 字符）——「Rust 内存模型」含 `rust`；
      3. 反向包含：主题里含**方向**的判定词块——主题「为什么中文大模型在长文本任务
         上表现不稳定」，方向「中文大模型 长文本」的方向词块「中文大」出现在主题里。

    为什么用"原词出现在方向里"而不是"与主题共享 bigram"：bigram 判据在这件事上
    两头不讨好。它放不过「碳交易」这种**不含**主题原词但确实相关的子方面（`碳中`/
    `中和` 与 `碳交`/`交易` 没有交集），也挡不住「中超联赛」这类只是撞了一个高频
    汉字的编造（共享「中」）。既然两种口径在"合法的窄化方向"上都失手，就选那条
    **可执行、可解释、且与提示词一致**的：提示词要求方向原样包含主题写法，代码按
    同一句话核对。被拒的方向丢掉，不拼回去——拼成「碳中和 超导量子比特」比丢掉更糟。

    长度闸（`DIRECTION_MIN_CHARS`-`DIRECTION_MAX_CHARS`）与"等于主题"的闸也在这里：
    方向的字数区间是任务给的 6-20 字，而等于主题的那条是**白跑一路**（同一个 query
    打两遍），它会让"3 个方向"实际只有 2 个方面。
    """
    text = " ".join((direction or "").split())
    if not _direction_length_ok(text):
        return None
    if text == topic:
        return None
    if any(phrase in text for phrase in _phrase_terms(topic)):
        return text
    if _word_terms(topic) & _word_terms(text):
        return text
    if any(phrase in topic for phrase in _phrase_terms(text)):
        return text
    return None


def _build_directions(payload: dict[str, Any], topic: str, max_directions: int) -> list[str]:
    """从 payload 重建方向清单：逐条过锚定闸、去重保序、截到 `max_directions`。

    全部被拒时返回**空列表**（不是 `[topic]`）——那是调用方的事：`plan_directions`
    的契约是"拆不出来就 `[topic]`"，而空列表在这个函数里只表示"模型给的东西一条
    都不能用"。两者分开，才能让日志区分"模型没答"与"模型答的东西全被拒"。
    """
    kept: list[str] = []
    for item in _raw_directions(payload)[:DIRECTION_MAX_ITEMS]:
        direction = _direction_anchored(topic, item)
        if direction is not None and direction not in kept:
            kept.append(direction)
    return kept[:max(max_directions, 1)]


def _log_directions(outcome: str, started: float, *, topics: int,
                    directions: Sequence[str]) -> None:
    """一行日志：**只有形状，没有内容**。

    主题本身不记：它是用户的输入，可能是"我的合同违约金怎么算"这种不该出现在日志里
    的东西；方向数、可用方向数、耗时足够回答"拆没拆、拆出几条、降没降级"。方向文本
    也不记——它是主题的改写，泄漏程度与主题同级。
    """
    logger.info(
        "外部检索拆方向 outcome=%s 耗时=%.0fms 条数=%d 可用=%d 长度=%d",
        outcome, (time.perf_counter() - started) * 1000, topics, len(directions),
        sum(len(item) for item in directions),
    )


async def plan_directions(topic: str, *, max_directions: int = MAX_DIRECTIONS) -> list[str]:
    """把主题拆成 2-4 个检索方向；拆不出来返回 `[topic]`。**一次调用、8 秒、不重试。**

    返回值的三种形态，调用方只需要认第一种与最后一种：

      - `[topic]`：没配置模型、调用失败、超时、JSON 解不出、方向全被拒。**与"没有
        这个功能"逐字节一致**——调用方（`search_external`）据此走单方向路径。
      - 2-4 条方向：每条都是主题的子方面，且词面被主题锚住（`_direction_anchored`）。
      - `[]`：主题为空。**没有方向可拆**，调用方拿到空列表时按"没搜到"处理。

    为什么一次调用产出全部方向：3 个方向各调一次 = 3 次往返，而每次 8 秒预算，
    2C2G 上不可能；一次调用产出 2-4 条的成本与产出 1 条几乎相同。

    为什么不复用 `query_plan.plan_query`：那个函数服务"在材料里找片段"，产出是
    corrected/intents/hyde/subqueries，还带四道防发散闸与"没有材料就不许扩"的代码闸。
    这里没有材料、也不需要改写用户的问题——只需要"这个主题有哪几个方面"。语义不同，
    复用会让两边绑死在对方的失败模式上（`query_plan` 的教训正是"不看材料就编领域"，
    而这里**必然**没有材料，那条闸一开就是永久关闭）。
    """
    text = (topic or "").strip()[:QUERY_MAX_CHARS]
    if not text:
        return []
    started = time.perf_counter()
    outcome = "ok"
    raw: Any = None
    try:
        # 只发一次，绝不重试：失败就是这次不拆方向，重试只会把 8 秒预算打穿。
        raw = await asyncio.wait_for(
            chat_async(DIRECTION_SYSTEM_PROMPT, _directions_message(text),
                       max_tokens=DIRECTION_MAX_OUTPUT_TOKENS),
            timeout=DIRECTION_TIMEOUT_SECONDS,
        )
    except (TimeoutError, asyncio.TimeoutError):
        outcome = "timeout"
    # 与 `plan_query` 同样不拦 `CancelledError`：那是我方被取消（客户端断开），
    # 不是模型坏了，吞掉它会让上层以为任务正常跑完了。
    except Exception:  # noqa: BLE001 — 没配置/网络/解析都可能，全部降级
        outcome = "error"
    if outcome != "ok":
        _log_directions(outcome, started, topics=1, directions=[text])
        return [text]

    payload = _directions_payload(raw)
    if payload is None:
        _log_directions("bad_json", started, topics=1, directions=[text])
        return [text]
    directions = _build_directions(payload, text, max_directions)
    if not directions:
        # 全被锚定闸拒掉：这次拆方向没有可用的东西，与"模型挂了"同一个结果。
        # 不能返回模型给的原话——那些正是判据说"不是这个主题"的东西。
        _log_directions("rejected", started, topics=1, directions=[text])
        return [text]
    _log_directions("ok", started, topics=1, directions=directions)
    return directions
