"""跨文档测试集：每条问法都要两份以上材料才能答完。

## 为什么另起一套

`eval_cases_manual.py` 那 28 条里有 26 条只指向**一份**材料（另外 2 条是同一份材料
里的两章）。那个形状测不出图检索的价值：单文档问答里，文本检索（BM25 + 向量）只要
命中那一章就够了，图能补的东西本来就不在这条赛道上。图的唯一卖点是**跨文档桥接**
——问的人手里没有任何一段原文，答案分散在两份材料的两个位置，而这两份材料之间只靠
一个共享的实体连起来。所以这一套把「必须跨文档」写成硬约束：每条 `targets` 至少
两份**不同**材料，一份都不够。

## 与 manual 套共用同一套标注纪律

区间写在**章节前缀**上（唯一匹配），`focus` 是可选锚点（全文唯一、落在该章内、向后
取 `FOCUS_CHARS` 字）。材料一改（`content_hash` 变）这里与 manual 套一起报错——两套
钉的是同一批正文，不能一套报错一套静默跑。

## 词面重合：挡「抄材料」的那道闸

`overlap_ratio` 直接复用 `eval_cases_manual`（query 的中文 bigram 有多少逐字出现在
**目标区间**里，不含全文）。均值落在 `OVERLAP_MEAN_RANGE`（30%–50%），与 manual 套
同一区间、同一个理由：抄来的问法必然 >60%，纯噪声问法 <30% 且失去「人真的会这么问」
这个前提。

**跨文档套有一条 manual 套没有的天然劣势**：一条 query 要同时贴近两份不同材料的
用词，天然比单文档问法更难凑出高重合。反过来也意味着写的时候要刻意留一两个术语，
否则均值会掉到 30% 以下——那是「问得太虚」，不是「问得好」。

## 为什么问法里可以出现文件名

真实用户读过材料，会用「AGENT_PLAN 里那三阶段」这种指代——这是**人**的用法，不是给
检索器定制的写法。所以这里允许出现文件名与材料里的术语，但不许出现「第 2 节第 3 段」
这种只有标注者才知道的坐标：那种问法等于把答案位置编进 query，测出来的不是检索能力。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

# `scripts/` 不是包，而这两个函数（`query_terms` / `overlap_ratio`）的口径必须与
# manual 套**逐字相同**：两套的重合度数字要能并排放进同一张表，各写一份必然分叉。
sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_cases_manual as manual_cases  # noqa: E402

FOCUS_CHARS = manual_cases.FOCUS_CHARS
"""`passage` 粒度下 relevant 的长度，与 manual 套同值（320 字）。

同值的理由不是省事：命中判据是「交集 ≥ 命中块长度的 20%」，父块 450–700 字，320 字
的标注段能达到 46%，够窄又可达。换个数字就得重新算这条可达性。
"""

OVERLAP_MEAN_RANGE = manual_cases.OVERLAP_MEAN_RANGE
"""全体 query 的 `overlap_ratio` 均值区间（30%–50%），与 manual 套同一道闸。"""

MAX_OVERLAP = 0.50
"""任何单条超过这个数就是在抄材料。测试里按它挡，不看档位。"""


CROSSDOC_SOURCE_HASHES: dict[str, str] = dict(manual_cases.MANUAL_SOURCE_HASHES)
"""四份材料的 `sha256(正文)`。与 manual 套共用一张表：两套标的是同一批正文。"""


@dataclass(frozen=True)
class CrossTarget:
    """一条 case 在**某一份材料**上的标注：章节前缀 + 可选锚点。

    没有距离档：跨文档套的难度不靠「离原话多远」分层（两份材料一起拉低了重合度，分档
    会变成「两份都远 / 一份近一份远」这种没法解释的格子），只有一条硬约束——每条的
    `targets` 至少两份不同材料。
    """

    material: str
    section: str
    focus: str | None = None
    chars: int = FOCUS_CHARS


@dataclass(frozen=True)
class CrossDocCase:
    """一条跨文档问法。

    `why` 回答的不是「为什么难」，而是**为什么必须跨文档**：哪一半信息在哪一份材料
    里、单看一份会缺什么。这一栏是给人工复核用的——它写不出来，说明这条 case 其实
    是单文档的，该退回 manual 套。
    """

    query: str
    targets: tuple[CrossTarget, ...]
    why: str


# ---------------------------------------------------------------- 14 条跨文档问法
#
# 材料路径用全路径，与 `evaluate_retrieval.DEFAULT_MATERIALS` 的 ref 同形：
#   P=docs/PLAN.md  A=docs/AGENT_PLAN.md  R=docs/RAG-DECISIONS.md  X=docs/ARCHITECTURE.md

CASES: tuple[CrossDocCase, ...] = (
    # 1 周验收门（只写了「逐节点长出」）↔ 帧序与终态规则
    CrossDocCase(
        query="第一周要演示浏览器里输入一句话、导图一格一格长出来，那这套流式事件到底按什么顺序发、"
              "最后怎么算真的结束",
        targets=(
            CrossTarget("docs/PLAN.md", "4 四周里程碑"),
            CrossTarget("docs/AGENT_PLAN.md", "SSE 与前端通道"),
        ),
        why="周验收门只说「逐节点长出导图」，帧的种类、顺序与「必须有 done 才算成功」的终态规则"
            "全在 agent 计划里；只看前者不知道要实现什么协议，只看后者不知道这是哪一周的门。",
    ),
    # 2 两个窗口保存冲突（验收门）↔ 候选版本号与保存版本号是两套
    CrossDocCase(
        query="两个窗口改同一张图，后保存的那个会弹版本冲突，这个冲突用的版本号和接受候选时比的"
              "那个计数是同一个吗",
        targets=(
            CrossTarget("docs/PLAN.md", "4 四周里程碑",
                        focus="同一导图在另一窗口打开后保存会提示版本冲突"),
            CrossTarget("docs/AGENT_PLAN.md", "真相、会话与编辑同步"),
        ),
        why="「另一窗口保存提示 409」是 W2 的验收门，而「MapRecord.version 只在保存成功后 +1、"
            "candidateRevision 与 batchId 永不作为数据库版本」只在 agent 计划里；两份各写一半。",
    ),
    # 3 公网 IP 上能不能录音
    CrossDocCase(
        query="现场用 IP:8080 打开页面要录音，浏览器会不会因为不是安全上下文就不给麦克风",
        targets=(
            CrossTarget("docs/ARCHITECTURE.md", "安全与部署边界"),
            CrossTarget("docs/PLAN.md", "4 四周里程碑"),
        ),
        why="安全上下文（HTTPS 或 localhost 才给 mediaDevices）写在架构文档的部署边界里，"
            "而「W1 只上 IP:8080、不做 TLS、域名推到 W3」写在周排期里；合起来才知道哪一周录音能用。",
    ),
    # 4 规划里的 pgvector ↔ 真实的混合检索
    CrossDocCase(
        query="申报里写 pgvector 做向量检索，实际那一路走的是混合检索里的 BM25 加向量吗，两路用 RRF 怎么合",
        targets=(
            CrossTarget("docs/PLAN.md", "2 范围：L1 / L2 / L3"),
            CrossTarget("docs/RAG-DECISIONS.md", "1 已经落地的两件事"),
        ),
        why="pgvector 是范围表里的规划（F13），实际实现是 text-embedding-v3 走 MaaS 端点 + "
            "BM25 + RRF k=60；规划与落地分处两份材料，任一份都答不全「到底用了什么」。",
    ),
    # 5 四阶段代理 ↔ 自写工具循环
    CrossDocCase(
        query="申报里承诺的四阶段可见 AI 代理，为什么最后变成后端自己写的工具循环，取舍在哪",
        targets=(
            CrossTarget("docs/PLAN.md", "2 范围：L1 / L2 / L3"),
            CrossTarget("docs/AGENT_PLAN.md", "循环位置与框架取舍"),
        ),
        why="四阶段可见是范围表 L2 的承诺（F02），而「自写循环 + 三方案取舍表」在 agent 计划里；"
            "改口的理由与代价只有后者写了，前者还留着旧说法。",
    ),
    # 6 agent 的检索工具属于哪种形态
    CrossDocCase(
        query="agent 那个检索工具只翻这次带进来的材料，它到底算哪种检索，能不能当准确性的答案",
        targets=(
            CrossTarget("docs/AGENT_PLAN.md", "四个工具及复用边界"),
            CrossTarget("docs/RAG-DECISIONS.md", "4 三种检索形态，别混着谈"),
        ),
        why="工具边界（top_k 1–5、只查本轮附件、无 embedding）在 agent 计划，三种形态的定义与"
            "「申报书 3.3.1 要的是扩展型」在 RAG 记录里；两份合起来才能判它属于哪一类。",
    ),
    # 7 2C2G 上再塞一个图库
    CrossDocCase(
        query="2C2G 的机器上再常驻一个图数据库，按当时实测要吃多少内存，预算表还留得下吗",
        targets=(
            CrossTarget("docs/PLAN.md", "3 技术架构定稿"),
            CrossTarget("docs/RAG-DECISIONS.md", "8 图存储选型：Neo4j 还是 SQLite"),
        ),
        why="各容器常驻估算（合计 ≈790MB、余 1.2GB）在 PLAN 第 3 节，Neo4j 五档堆配置的启动与"
            "常驻实测在 RAG 第 8 节；两个数不在同一份材料里，任何一份都算不出「还剩多少」。",
    ),
    # 8 架构图上的 PostgreSQL 为什么还没上
    CrossDocCase(
        query="架构图里画着 PostgreSQL，为什么到现在还是 SQLite 单文件，迁移到底卡在哪",
        targets=(
            CrossTarget("docs/ARCHITECTURE.md", "模块边界"),
            CrossTarget("docs/PLAN.md", "11 决策记录（Q26–Q36）"),
        ),
        why="模块边界写的是「当前 SQLite、后续 PostgreSQL」（只描述现状与方向），顺延到验收之后的"
            "决定、理由与连带影响在 Q35；只有两份一起看才知道「为什么至今没上」。",
    ),
    # 9 引用为什么用子块区间
    CrossDocCase(
        query="引用为什么非得卡在子块那两百字，父块整章给过去，逐字校验不就形同虚设了吗",
        targets=(
            CrossTarget("docs/RAG-DECISIONS.md", "3 评估后决定不做的"),
            CrossTarget("docs/AGENT_PLAN.md", "op、ID 与整树保存"),
        ),
        why="「父子切块默认关、score 来自子块而 quote 是父块会让逐字校验退化成格式检查」在 RAG "
            "记录里；「quote ≤300 字、start/end 成对且是码点区间」的契约在 agent 计划的 op 与"
            "引用一节；一个说为什么、一个说落成什么字段。",
    ),
    # 10 八次模型调用的上限从哪来
    CrossDocCase(
        query="为什么给模型调用卡八次预算，多轮检索既然会发散，这数是拍脑袋定的吗",
        targets=(
            CrossTarget("docs/AGENT_PLAN.md", "循环位置与框架取舍"),
            CrossTarget("docs/RAG-DECISIONS.md", "2 实测数据"),
        ),
        why="RunBudget 的数值（8 次模型 / 8 次工具 / 120 秒）在 agent 计划；而「多轮判断跑满 20 轮、"
            "127 次调用里 17 轮纯浪费」的实测在 RAG 第 2 节。数量的依据在一份，数值在另一份。",
    ),
    # 11 测试规模 vs 覆盖率门槛
    CrossDocCase(
        query="后端现在这个测试规模，离验收要求的覆盖率还差多少",
        targets=(
            CrossTarget("docs/AGENT_PLAN.md", "兼容、测试逐文件迁移与数据库"),
            CrossTarget("docs/PLAN.md", "5 验收门与指标口径"),
        ),
        why="当前实测（488 passed + 19 subtests / 20 个文件）写在 agent 计划第 6 节，覆盖率 ≥60% 与"
            "12 条工程门写在 PLAN 第 5 节；「还差多少」这个判断本身就要两份的数。",
    ),
    # 12 分支标题扩写值不值得做
    CrossDocCase(
        query="分支标题先扩成几个同义问法多搜一遍，到底管不管用",
        targets=(
            CrossTarget("docs/RAG-DECISIONS.md", "10 分支查询扩展：实测零收益"),
            CrossTarget("docs/AGENT_PLAN.md", "可独立上线与回滚的切片"),
        ),
        why="四组对照（A/B0/B/C 的覆盖率与并集段落）与「开关保持 0」的结论在 RAG 第 10 节；"
            "「先 10% 试用、100 次后仍达标再扩大」的放量门槛在 agent 计划阶段 4。要不要开、按什么"
            "标准开，结论与门槛分处两份。",
    ),
    # 13 op 批次上限与整树上限
    CrossDocCase(
        query="一批 op 最多能带多少条，跟整树一千节点、每节点五十个孩子的上限怎么配合",
        targets=(
            CrossTarget("docs/AGENT_PLAN.md", "op、ID 与整树保存"),
            CrossTarget("docs/ARCHITECTURE.md", "数据与调用约定"),
        ),
        why="每批 0–100 op、整次运行 ≤500 在 op 契约里；1000 节点 / 8 层 / 每节点 50 个孩子的上限"
            "在架构文档的数据约定里。两个上限分处两份材料，而它们的配合关系（validate_tree 兜底）"
            "只在其中一份被点明。",
    ),
    # 14 前端为什么不用 React
    CrossDocCase(
        query="前端从 React 换到 Vue 这个技术选择是谁拍的板，顺手把排期改成了先做 D4 吗",
        targets=(
            CrossTarget("docs/ARCHITECTURE.md", "技术选择及来源"),
            CrossTarget("docs/PLAN.md", "11 决策记录（Q26–Q36）"),
        ),
        why="技术选择一节给的是理由（负责前端的人只有 Vue 实战经验、旧 React 骨架打 tag 存档），"
            "Q33 给的是决定与连带排期（先做 D4）；理由与决定分处两份材料。",
    ),
)


def is_cross_document(case: CrossDocCase) -> bool:
    """这条 case 是否真的跨了两份以上材料。

    判据是**材料路径去重后的个数**，不是 `targets` 的个数：同一份材料写两个章节
    （manual 套的 `cross` 粒度）在图的视角下仍然是单文档——两份材料之间的桥才是这
    一套要测的东西。
    """
    return len({target.material for target in case.targets}) >= 2


def overlap_ratio(query: str, text: str) -> float:
    """query 的中文 bigram 有多少逐字落在 `text` 里；口径与 manual 套逐字相同。"""
    return manual_cases.overlap_ratio(query, text)
