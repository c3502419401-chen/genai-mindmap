"""手写真实问法测试集：把「标题当 query」那份偏差校准到人工提问的距离。

自动构造（`evaluate_retrieval.build_dataset`）拿章节标题当 query，而标题就是章节的
第一行，于是 query 的每个 bigram 都在目标章节里（重合 100%），`R@3/5/10` 全饱和。
这里的 28 条是**人读材料之后手写的问法**，用来量"真实提问下检索还剩多少"。

## 构造原则

1. **不抄材料用词**。每条问法写完都要算一遍「query 的中文 bigram 有多少逐字出现在
   目标区间里」（`overlap_ratio`），近档 30%–50%、中档 30%–50%、远档 0%–30%；
   全体均值 `OVERLAP_MEAN_RANGE`（30%–50%），与 `docs/RETRIEVAL-EVAL.md` 第 7 节
   记的真实口语问法均值 33.7% 同量级。抄来的问法必然 >60%，脚本里挡死。
2. **三档距离各约三分之一**：
   - `near`：用材料里的术语，但整句换成人在问问题的形状（"一级分支的下限为什么定成三"）；
   - `mid`：同义改述，术语只留一两个，其余换成日常说法（"回复被截住时不原样重试"）；
   - `far`：只有语义关联，几乎无字面重合（"备案要是批不下来，整个计划怎么收场"）。
3. **三种粒度**：
   - `chapter`：整章都是答案（`relevant` = 整章区间）；
   - `passage`：答案只在章内某一段（`focus` 给出该段第一句的逐字锚点，
     `relevant` 收窄到从锚点起 `FOCUS_CHARS` 字）；
   - `cross`：要两章合起来才答得全（`sections` 给两个标题，`relevant` 是两段并集）。

## 为什么 `overlap_ratio` 不等于 `evaluate_retrieval.wording_overlap`

后者（`--ladder` 用的那个）按**字符**切 bigram，`PostgreSQL` 会被切成 `Po os st tg
gr re eS SQ QL` 九个 bigram——写一个英文专名就能把重合度刷到 57.9%，那是测量伪影：
检索侧是「中文 bigram + 英文按词切」，纯 ASCII 的字符 bigram 根本不参与匹配。这里的
`query_terms` 只取**两端都是汉字**的 bigram，量的是"中文用词被复用了多少"。
两个口径的数字不可直接相比，`--ladder` 的历史数字（57.6%）保持原样。

## 为什么只用仓库里这四份 md

第五份候选是桌面上那份 docx，但它**不进这个套件**：桌面文件不随仓库走，而且 docx 的
正文由 Docling 解析产出，换版本/换机器解析结果就可能变——`MANUAL_SOURCE_HASHES` 一
对不上，整个 manual 套当场报错，等于把这套尺子钉死在一台机器上。这四份 md 随仓库走、
纯文本直通，谁 clone 下来都能跑出同一批数字。

## 材料改了怎么办

`MANUAL_SOURCE_HASHES` 是这四份材料的 `sha256(正文)`（与 `knowledge.library.
content_hash` 同口径）。材料一改，`load_manual_dataset` **明确报错**，不静默跳过：
静默跳过会让"标注失效"看起来像"指标掉了"。改法是重读那一章、复核区间与问法，再把
新指纹写回本文件。
"""
from __future__ import annotations

from dataclasses import dataclass

FOCUS_CHARS = 320
"""`passage` 粒度下 `relevant` 的长度（字）。

为什么不是"整段到下一个空行"：段长不定，短段（100 字）会让命中判据不可达——判据是
「交集 ≥ 命中块长度的 20%」，而父块是 450–700 字，100 字的 relevant 最多只能凑出
100/700 = 14%，再准的命中也会被判掉。320 字保证可达（320/700 = 46%），又足够窄，
能把"命中了这一章"与"命中了这一段"分开。
"""

OVERLAP_MEAN_RANGE = (0.30, 0.50)
"""全体 query 的 `overlap_ratio` 均值必须落在这里。

下界挡"全是远处的问法"（那更像噪声 query，检索必然失手，量的是玄学）；上界挡"其实
还在抄材料"。实测真实口语问法对目标章节的均值就在这个区间。
"""

DISTANCE_BANDS = {
    "near": (0.30, 0.50),
    "mid": (0.30, 0.50),
    "far": (0.0, 0.30),
}
"""各档的重合区间。

`far` 的上限是 30%（语义关联、几乎无字面重合），下限只留 0（完全不重合也是合法的远
处提问，它的得分本来就该是 0——那是信息，不是缺陷）。**近/中两档不设低于 30% 的
例外**：它们的定义就是"用得上材料的词"，低于 30% 说明写成了远档，档位标错了。
"""

DISTANCES = ("near", "mid", "far")
GRANULARITIES = ("chapter", "passage", "cross")


@dataclass(frozen=True)
class ManualCase:
    """一条手写样例。

    `sections` 是 `heading_outline` 给出的标题**前缀**（唯一匹配；前缀而不是全等，
    是为了标题后半截微调时少改一个字——但材料一改 hash 就报错，所以这条便利只在
    "标题没动、正文动了"这种本就要重标的情况下才有意义）。
    """

    query: str
    material: str
    sections: tuple[str, ...]
    distance: str
    granularity: str
    why: str
    focus: str | None = None


MANUAL_SOURCE_HASHES: dict[str, str] = {
    "docs/AGENT_PLAN.md": "1a06838dd1b45a98664c17d20db063cbb0427f9f4466b4ec28ac44f551f34393",
    "docs/RAG-DECISIONS.md": "0a61ef1684479991fae70e9bba07d1ece3e0f0f0997feab9d5bbbda2b59bcfb8",
    "docs/PLAN.md": "9b25248a2cde486f921fa8f5addbc76d06fa72ba99671a7d0992c1bd7642b810",
    "docs/ARCHITECTURE.md": "d95bd8455e094bb1def66018a3d6b8fb88a0939527bff3d1a49f35df135c426b",
}
"""这四份材料的 `sha256(正文)`；对不上就是材料变了，整套标注要复核。"""


# ---------------------------------------------------------------- 近：术语还在，句形变了
# 这八条刻意保留材料自己的名词（"终止原因""一级分支""两路""薄切片""接口桩"），
# 只把句子换成提问。它们量的是"看懂术语但不知道原文怎么写"这种最常见的情形。

NEAR_CASES: tuple[ManualCase, ...] = (
    ManualCase(
        query="终止原因不是正常结束时，旧接口返回的是什么",
        material="docs/AGENT_PLAN.md",
        sections=("先修三件小事",),
        distance="near",
        granularity="chapter",
        why="术语（终止原因、旧接口）是材料的，但没人会照抄'拒绝截断成功'那句话去问；"
            "读者想知道的是失败后对外表现成什么。",
    ),
    ManualCase(
        query="一级分支的下限为什么是三，整图生成才卡这条吗",
        material="docs/AGENT_PLAN.md",
        sections=("先修三件小事",),
        distance="near",
        granularity="passage",
        focus="0b：提高分支下限",
        why="问的是同一条规则的两面（数量下限 + 只在整图生效），原文分散在表格两格里，"
            "query 只借了'一级分支''整图生成'两个词。",
    ),
    ManualCase(
        query="两路候选各取多少条再融合，两路的分数能直接比吗",
        material="docs/RAG-DECISIONS.md",
        sections=("1 已经落地的两件事",),
        distance="near",
        granularity="chapter",
        why="'两路''融合'是材料术语，但原文用的是 RRF/k=60/分数不可比这套写法，"
            "口语问法不会出现这些符号。",
    ),
    ManualCase(
        query="图数据库常驻内存要多少，两跳邻域查询还快吗",
        material="docs/RAG-DECISIONS.md",
        sections=("8 图存储选型",),
        distance="near",
        granularity="chapter",
        why="'常驻''两跳'是材料的词，但整句是运维视角的提问；原文用表格给 MB 与毫秒，"
            "没有一句话这么说。",
    ),
    ManualCase(
        query="验收对象和交付物这两栏，分别卡的是什么",
        material="docs/PLAN.md",
        sections=("0 一页摘要",),
        distance="near",
        granularity="chapter",
        why="直接点表格里两个字段名，但'分别卡的是什么'是读者的问题，原文只写结论、"
            "不解释为什么这两栏重要。",
    ),
    ManualCase(
        query="薄切片允许薄到什么程度，哪些必须真做",
        material="docs/PLAN.md",
        sections=("2 范围",),
        distance="near",
        granularity="chapter",
        why="'薄切片'是材料术语，'薄到什么程度'把原文的边界列（允许薄、不允许假）"
            "压成了一个反问。",
    ),
    ManualCase(
        query="为什么用 http 打开页面时浏览器不让录音",
        material="docs/ARCHITECTURE.md",
        sections=("安全与部署边界",),
        distance="near",
        granularity="passage",
        focus="**这条与录音功能冲突**",
        why="症状式提问（点了没反应）；原文讲的是安全上下文与 navigator.mediaDevices，"
            "只有'录音''浏览器'两个词重合。",
    ),
    ManualCase(
        query="节点上最多能挂几个孩子",
        material="docs/ARCHITECTURE.md",
        sections=("数据与调用约定",),
        distance="near",
        granularity="chapter",
        why="最短的一条，靠'节点''最多''孩子'三个词定位；原文是'8 层、每节点最多 50 "
            "个孩子'的一句话约束。",
    ),
)


# ---------------------------------------------------------------- 中：同义改述，术语只剩一两个
# 十条都换了动词与名词（"重试"对"不原样重试"、"静默覆盖"对"手工编辑令候选 stale"），
# 保留的那一两个词是唯一能把问题钉到某一章的路标。

MID_CASES: tuple[ManualCase, ...] = (
    ManualCase(
        query="截断的解为什么不能原样重试，修复额度留给谁",
        material="docs/AGENT_PLAN.md",
        sections=("循环位置与框架取舍",),
        distance="mid",
        granularity="chapter",
        why="原文是'length 不原样重试'与'全轮共享 1 次修复额度'两句；问法把它们合成"
            "一句人话，'回复''参数错误'是替换词。",
    ),
    ManualCase(
        query="引入编排框架要新增依赖，这笔账划算吗",
        material="docs/AGENT_PLAN.md",
        sections=("循环位置与框架取舍",),
        distance="mid",
        granularity="chapter",
        why="'编排''依赖'是材料的词，'这笔账划算吗'是读者视角；原文用'取舍与代价'表格"
            "陈述，没有这个问句。",
    ),
    ManualCase(
        query="手工改过之后接受候选，会不会静默覆盖当前图",
        material="docs/AGENT_PLAN.md",
        sections=("真相、会话与编辑同步",),
        distance="mid",
        granularity="chapter",
        why="把原文的 stale / 版本校验 / '静默覆盖=0'翻译成用户能问出口的担忧；"
            "'手工''候选''静默'三个词撑起定位。",
    ),
    ManualCase(
        query="为什么空行比句末标点优先，这不是反过来了吗",
        material="docs/RAG-DECISIONS.md",
        sections=("2 实测数据",),
        distance="mid",
        granularity="passage",
        focus="中文 L4 占比低不是缺陷",
        why="原文的论点是'段落边界比句子边界更强'，但没说'更硬'；这条几乎是把结论"
            "反过来问成因。",
    ),
    ManualCase(
        query="多编几种同义问法去查同一个分支，覆盖率会涨吗",
        material="docs/RAG-DECISIONS.md",
        sections=("10 分支查询扩展",),
        distance="mid",
        granularity="chapter",
        why="原文叫'分支查询扩展'/'每支 2–3 个同义问法'；问法用'多编几种''会涨吗'"
            "换掉了术语外壳。",
    ),
    ManualCase(
        query="为什么把微服务拆分明确列进不做",
        material="docs/PLAN.md",
        sections=("3 技术架构定稿",),
        distance="mid",
        granularity="chapter",
        why="'微服务拆分'是原词，但原文把它塞在'不做：Celery/Redis、K8s…'的清单里，"
            "问法问的是清单背后的理由。",
    ),
    ManualCase(
        query="结构合理性靠两个人分开盲评，合格线怎么定",
        material="docs/PLAN.md",
        sections=("5 验收门与指标口径",),
        distance="mid",
        granularity="passage",
        focus="**指标口径**",
        why="原文是'双人独立盲评 4 维度'与'通过率 = 双人一致判合格的比例'；问法用"
            "'两个人分开''合格线'替换。",
    ),
    ManualCase(
        query="迁移为什么顺延到验收结束之后，不是现在做",
        material="docs/PLAN.md",
        sections=("11 决策记录",),
        distance="mid",
        granularity="chapter",
        why="术语只留'迁移'，其余是追问顺序（为什么不是现在）；原文是 Q35 的一行决策"
            "记录加两句理由。",
    ),
    ManualCase(
        query="哪些模块的目录建了，但接口只是桩",
        material="docs/ARCHITECTURE.md",
        sections=("模块边界",),
        distance="mid",
        granularity="chapter",
        why="原文写'模块目录已创建，但接口桩不等于功能完成'；问法把陈述改成清点"
            "（哪些），'目录''接口'是仅剩的原词。",
    ),
    ManualCase(
        query="哪些功能只是目录加接口桩，其实没有实现",
        material="docs/ARCHITECTURE.md",
        sections=("目标与当前边界", "模块边界"),
        distance="mid",
        granularity="cross",
        why="答案分在两章：'目标与当前边界'讲哪些落地、哪些仍是设计，'模块边界'讲"
            "目录建了但接口是桩。问法跨这两章才对得上。",
    ),
)


# ---------------------------------------------------------------- 远：只有语义关联
# 十条几乎不用材料的名词，靠"这件事对应哪一章"来定位。它们量的是检索在真实提问下
# 最可能失手的那一段：字面没有桥，只能靠语义（纯 BM25 直接掉，hybrid 才有机会）。

FAR_CASES: tuple[ManualCase, ...] = (
    ManualCase(
        query="模型有没有可能自己偷偷把文件写到磁盘上",
        material="docs/AGENT_PLAN.md",
        sections=("循环位置与框架取舍",),
        distance="far",
        granularity="chapter",
        why="问的是权限边界（模型不能调保存/文件工具），原文用'模型不能调用保存、任意"
            "网络或文件工具'一句带过；字面只剩'模型''文件'。",
    ),
    ManualCase(
        query="两个请求同时回来，怎么保证不串到另一张图或者另一个会话",
        material="docs/AGENT_PLAN.md",
        sections=("真相、会话与编辑同步", "最可能出错的三个地方"),
        distance="far",
        granularity="cross",
        why="用户说的是'串'，材料说的是 runId / mapSession / 迟到帧；两章分别是机制与"
            "故障注入，问法要把它们连起来才答得全。",
    ),
    ManualCase(
        query="让模型先编一段假答案再去检索，为什么不做",
        material="docs/RAG-DECISIONS.md",
        sections=("3 评估后决定不做的",),
        distance="far",
        granularity="chapter",
        why="HyDE 的中文说法一个字没提；问法只描述做法（先编一段假答案），要靠"
            "'为什么不做'的语义落到那张'不做'的表格上。",
    ),
    ManualCase(
        query="材料里本来就有答案，和材料里根本没有要去外面找，这两件事能用一套做法吗",
        material="docs/RAG-DECISIONS.md",
        sections=("4 三种检索形态",),
        distance="far",
        granularity="chapter",
        why="把'定位型 / 扩展型'翻译成两句描述，术语零重合；答案是'这三件事别混着谈'"
            "那张表。",
    ),
    ManualCase(
        query="把检索摊到导图每个大分支上，次数是不是就有上限了",
        material="docs/RAG-DECISIONS.md",
        sections=("5 下一步：结构引导的召回",),
        distance="far",
        granularity="chapter",
        why="'有界'这件事用户会说成'有上限'；原文的论证是'检索次数由结构决定，不由"
            "模型判断'，字面只有'分支''检索'。",
    ),
    ManualCase(
        query="建约束和写数据这两件事，能不能放进一个事务",
        material="docs/RAG-DECISIONS.md",
        sections=("9 图数据库落地",),
        distance="far",
        granularity="passage",
        focus="9.1 `CREATE CONSTRAINT` 不能与数据写同事务",
        why="问的是'能不能'，原文给的是报错与机制（schema modification 不能与 write "
            "同事务）；'约束''事务'是通用词，不是这一章的特征词。",
    ),
    ManualCase(
        query="答辩那天网线断了还能讲吗",
        material="docs/PLAN.md",
        sections=("4 四周里程碑",),
        distance="far",
        granularity="chapter",
        why="原文只在缓冲周写了'断网也能讲完'五个字；问法是场景化的（答辩那天），"
            "与'里程碑''缓冲'零字面重合。",
    ),
    ManualCase(
        query="备案要是批不下来，整个计划怎么收场",
        material="docs/PLAN.md",
        sections=("6 风险表",),
        distance="far",
        granularity="chapter",
        why="风险表的 R1 条目讲的是'域名备案 7–20 天不可控'与预案；问法问的是后果，"
            "只有'备案'重合。",
    ),
    ManualCase(
        query="模型自己标的出处，能当证据用吗",
        material="docs/ARCHITECTURE.md",
        sections=("数据与调用约定",),
        distance="far",
        granularity="chapter",
        why="原文讲的是 source_refs 的机械核验与 NLI 复核；'能不能当证据'是使用者的"
            "信任问题，字面只剩'模型'。",
    ),
    ManualCase(
        query="前端当初为什么不选 React",
        material="docs/ARCHITECTURE.md",
        sections=("技术选择及来源",),
        distance="far",
        granularity="chapter",
        why="原文的理由是'负责前端的人只有 Vue 实战经验'；问法只出现框架名，"
            "属于典型的'记得有这回事、记不住原话'。",
    ),
)


CASES: tuple[ManualCase, ...] = (*NEAR_CASES, *MID_CASES, *FAR_CASES)
"""全部 28 条，按距离分组的顺序保留（报告与文档都按这个顺序列）。"""


def _is_cjk(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


def query_terms(query: str) -> list[str]:
    """query 里参与词面比较的中文 bigram（两端都是汉字）。

    与检索侧同口径：中文按 bigram、英文按词。这里只保留中文 bigram 是为了量"中文
    用词复用"，ASCII 字符的相邻组（`Po`/`os`/`st`…）是测量伪影，见模块 docstring。
    """
    chars = [char for char in query if not char.isspace()]
    return [
        chars[index] + chars[index + 1]
        for index in range(len(chars) - 1)
        if _is_cjk(chars[index]) and _is_cjk(chars[index + 1])
    ]


def overlap_ratio(query: str, text: str) -> float:
    """query 的中文 bigram 有多少**逐字出现**在 `text` 里。

    `text` 是**目标区间**（不是全文）：对全文量会把分母做大、把重合率抬高，那样
    33.7% 这种数字反而更容易"达标"，也就失去了挡住抄写的作用。
    """
    grams = query_terms(query)
    if not grams:
        return 0.0
    squashed = "".join(char for char in text if not char.isspace())
    return sum(1 for gram in grams if gram in squashed) / len(grams)
