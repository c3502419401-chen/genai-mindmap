"""实体消解：把指向同一个事物的多个名字合成一个，交给图存储落成别名边。

流程位置：
    entity_degrees() ─┐
                      ├─ 结构判定（顺序子序列）──┐
    embed_texts() ────┴─ 余弦兜底（≥0.95）────┴─> 并查集 ─> 组 ─> 主名 ─> merge_candidates

**为什么需要这一层**：`neighbors("市场监管总局")` 返回 `[]` 与"这两份材料确实无关"
返回的 `[]` 逐字节相同。图被切成孤岛，而失败是哑的：读代码的人只会以为图是完整的。
抽取粒度不一致（"预算审批流程" vs "审批流程"）会让这件事更重。

## 判定是两段式的：结构负责召回，embedding 只兜底

第一版只有一条闸：余弦 ≥ 0.92。在 66 个中文实体名上标定之后，那条闸被证明是错的
——**它的排序能力不够**：

    正例  预算审批流程 | 审批流程        0.9099
    错合  市场监管总局 | 市场监管部门     0.9241   ← 比正例还高

一段区间里正例低于负例，说明不是阈值高低的问题：**0.87–0.93 之间没有任何一个数能
同时收下前者、挡住后者**。继续调参只是在"漏合"与"错合"之间换边站。

所以判据换成两种性质不同的证据，**不是加权，是替换**：

  1. **结构关系是充分条件，单独成立，不看分数**（`_abbreviation_ratio`）：短名是
     长名的顺序子序列、长度比过闸、短名里有一整块连续字、且没有丢掉类型词。这不是
     "像"，这是构词关系，它不需要余弦背书，也不该被余弦否掉——`test_structural_pair_
     is_enough_even_when_cosine_is_zero` 钉着"余弦 0.1 也合并"。
  2. **余弦只在 ≥ 0.95 兜底**（`DEFAULT_THRESHOLD`），收"几乎完全相同"的那一类
     （错字、异体、夹注、全半角残留）。0.95 的下界由实测定：它必须高于"市场监管
     总局|市场监管部门"那条 0.9241 的错合。**兜底这一路宁可一个都不收，也不收一
     个错的。**

## 为什么是"顺序子序列"而不是"连续子串"

任务书里给的判定是"子串 + 长度比"，但实测撞出一个事实：**「市场监管总局」与「国家
市场监督管理总局」不是子串关系**——简称把「管理」两个字整段删掉了（国家|市场监督|
管理|总局 → 市场监管总局），中间断了一处。按连续子串实现，那个旗舰案例反而会漏掉：

    判据                              市场监管总局这对    总体（12 正例 / 22 负例）
    连续子串 + 0.95 兜底                    漏（0.9349）      合 4 / 漏 8 / 错 4
    顺序子序列 + 五道闸 + 0.95 兜底          合（结构判定）    合 7 / 漏 5 / 错 1

中文简称的构词本质是"按顺序取字"，连续性不是它的约束（发改委、个保法、董秘都断过）。
子序列是子串的超集，多出来的那部分由下面五道闸管住。

## 五道闸，一道防一种错

判定的顺序是按代价从便宜到贵排的（`_abbreviation_ratio`）：

  1. **泛称黑名单**（`GENERIC_TERMS`）：`预算`、`审计`、`风险`、`合规`、`数据`、
     `董事`、`采购`… 这些名字单独出现时是上位概念，永远不是某个更具体名字的同义
     缩写。命中即拒，失效模式是漏合。
  2. **长度比 ≥ `MIN_LENGTH_RATIO`（0.4）**：0.4 挡住的是"中华人民共和国财政部|
     财政部"（3/10 = 0.3）。**降它的收益与代价都实测过**：降到 0.3 能收下财政部与
     发改委，代价是 3 字短名 × 8–10 字长名这一整档全部进来。
  3. **顺序子序列**：跳字可以，乱序不行。
  4. **最长连续公共段 ≥ 短名的一半**（`2 * run >= len(短名)`）：挡"子序列巧合"——
     `数据安全法` 是 `数据出境安全评估办法` 的子序列（数|据|安|全|法 都在），但那是
     两个不同的法规名。真简称总有一大块字是连着的（市场监管总局的「市场监督」、董秘
     的「董」、个保法的「保护法」）。
  5. **类型词丢了没有**（`_lost_type_word`）：长名以文件类词结尾而短名不含文件类词
     （`董事会 ⊂ 董事会议事规则`）、或长名以机构类词结尾而短名不含机构类词
     （`市场监管 ⊂ 市场监管总局`），都判为"上位/别层 ⊂ 专名"。两张表分开查是因为
     两类方向相反——合成一张表时 `董事会`（机构）会因为含「会」而混过文件类那一闸。

## 不合并的边界

  - **向量为空的名字仍然参与结构判定**：子序列、长度比、类型词都不需要向量。只有
    cosine 那一路会因为拿不到向量而跳过（`None` 不参与比较，也绝不补零向量——补零
    会让"没结论"伪装成"比过了，不相似"）。
  - **同名不同型不区分**：别名边是名字级的（`(:Entity {name})`），"苹果"公司 /
    苹果水果本来就是两个节点，别名表也表达不了"只有公司那个苹果有别名"。这是这一
    轮的已知粗糙处：真出现时会让一条路径接错，所以落库前有 `dry_run`。
  - **不做传递阈值的收紧**：A~B、B~C 会合成一组（并查集的传递闭包），即使 A 与 C
    之间什么关系都没有。这是刻意的（第一轮的硬要求），代价在实测里看得见：只要有一
    个泛称短名漏网，它就能把两个本该分开的名字连成一组。`MergeCandidate` 会如实报出
    组内最弱的一环与它是靠什么进来的。

## 日志只有形状，没有内容

实体名就是材料内容（合同里的当事人、病历里的诊断）。这一层只记"取了多少个实体、
几条结构边、几条兜底边、几个候选组、耗时多少"，一个名字都不进日志——与
`graph_store` / `service` 的日志原则一致。

## 校准到什么程度（实测记录，2026-11）

**没有在 `text-embedding-v3` 上量到**：做这一轮时机器上没有 embedding 凭证，一次
真实调用都没发出。下面是拿代理模型量的，它回答的是"这套判定的形状对不对、错在哪
一侧"，**不能当成 v3 的分值**。

设置：`BAAI/bge-small-zh-v1.5`（CLS pooling + L2），66 个中文材料里常见的实体名；
12 对"该合"，22 对"不该合"（10 对近邻/并列概念 + 12 对"上位或别层 ⊂ 专名"）。标是
我自己标的——「管理办法」与「管理制度」算不算同一个东西，做制度的人会吵起来，那种
边界本来就不该由阈值决定。

    判据                                  合并   漏合   错合
    余弦 ≥ 0.92（第一版）                  2/12   10     1/22
    连续子串 + 0.95 兜底（字面照做）        4/12    8     4/22
    顺序子序列 + 五道闸 + 0.95 兜底         7/12    5     1/22

**合上的 7 对全部来自结构判定，兜底那一路一个都没收**（最高余弦是 0.9349，低于
0.95）——它只在"几乎完全相同"时才会出场，这批样本里没有那一类。

    合上  国家市场监督管理总局|市场监管总局            0.9349  长度比 0.60
          预算审批流程|审批流程                        0.9099  0.67
          数字化转型办公室|数字化转型办                0.9215  0.75
          中华人民共和国个人信息保护法|个人信息保护法    0.9192  0.50
          数据出境安全评估办法|数据出境安全评估          0.8981  0.80
          电子发票|电子增值税发票                      0.8397  0.57
          董事会秘书|董秘                             0.8089  0.40
    漏掉  中华人民共和国财政部|财政部                  0.8703  长度比 0.30（被 0.4 挡）
          国家发展和改革委员会|发改委                  0.8128  0.30（被 0.4 挡）
          中国人民银行|央行                           0.6929  抽字，无结构关系
          碳达峰碳中和|双碳                           0.6482  抽字，无结构关系
          固定资产管理办法|固定资产管理制度            0.8964  「办法/制度」互不包含
    错合  董事会|董事会秘书                           0.8313  0.60（机构 ⊂ 职务）

四条结论：

  1. **召回的主力是结构规则，不是 embedding**：7/12 全靠它，兜底贡献 0。指望余弦
     救抽字缩写是徒劳的（0.65–0.81），那三类（央行、双碳、发改委）只能靠别名表。
  2. **0.95 的兜底线这批样本上收 0 个，符合设计意图**：它的职责是"只收近乎相同"，
     而不是贡献召回。真要它干活得等"同一个名字的错字/异体"进库，那是另一类样本。
  3. **仍然错 1 对：`董事会 ⊂ 董事会秘书`**——机构名被并进"机构+职务"的名。类型词
     那两道闸管不到它，因为「秘书」既不是文件类词也不是机构类词。这一族（机构 vs
     机构里的角色）需要"人名/职务"的识别，本层不做，如实留在这里。
  4. **长度比 0.4 是"漏合"的选择**：它把机构全称（财政部 0.3）挡在外面。任务书里
     估的 0.43 是把「中华人民共和国财政部」当成 7 个字算的，实际是 10 个字，比值
     0.3。要收它就把 `MIN_LENGTH_RATIO` 降到 0.3，代价见上面第 2 条闸。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from math import sumprod as _dot

from app.knowledge import graph_store, vector_cache
from app.knowledge.embedding import EmbeddingNotConfigured
from app.knowledge.embedding import configured as embedding_configured
from app.knowledge.embedding import embed_texts
from app.knowledge.embedding import model_name as embedding_model

logger = logging.getLogger(__name__)

# 余弦兜底阈值。**它不再是唯一判据**：子串关系走另一条路，不看分数。
# 0.95 的下界由实测定：这次的错合「市场监管总局|市场监管部门」是 0.9241，兜底必须
# 高于它才可能真的"只收近乎相同的"。宁可一个都不收，也不收一个错的。
DEFAULT_THRESHOLD = 0.95

# 子串判定的长度比下限：len(短) / len(长)。
#
# 0.4 这个数挡住的是"中华人民共和国财政部|财政部"（3/10 = 0.3）：机构全称里那串
# 限定词太长，短名只占三成。**这是有意的漏合**——它是缺失，不是伪造。
# 想收它就把这个值降到 0.3，代价是"管理办法 ⊂ 固定资产管理办法"（0.5）、
# "采购 ⊂ 采购管理办法"（0.33）这一档会一起进来，其中只有后一半能被类型词否决。
MIN_LENGTH_RATIO = 0.4

# 参与聚类的实体名个数。按边数降序取前 N 个：边少的合并价值低，也顺手按住 N²。
DEFAULT_LIMIT = 500

# 上限。每对两次判定：一次子串（C 层字符串包含，几十纳秒）+ 一次点积。点积是主成本：
# `math.sumprod`（C 层）实测 1024 维 **1.42 秒 / 12.5 万对（500 个名字）**，等价的
# 生成器写法 `sum(x*y for x, y in zip(a, b))` 是 10.38 秒。任务书里写的"毫秒级"是
# numpy 的量级（`matrix @ matrix.T`），本项目没有 numpy 依赖（`requirements.txt` 里
# 没有它，`nli.py` 只是可选懒加载），所以这里是**秒级**，不是算错了。
# 1000 个名字约 50 万对、约 6 秒，到此为止；再往上应该先减少参与聚类的实体。
MAX_LIMIT = 1000

# 少于两个名字就没有"对"可比。
MIN_NAMES = 2

# 判定依据。写进 `MergeCandidate.reason` 与别名边的 `source` 属性：
# "这条边凭什么加的"必须能回答，否则出错时连该怀疑哪条路都不知道。
REASON_SUBSTRING = "substring"   # 子串关系（结构证据，不看分数）
REASON_EMBEDDING = "embedding"   # 余弦兜底（≥ DEFAULT_THRESHOLD）
REASON_NONE = ""                 # 不合并

# 类型词分两类，因为它们的"上位/专名"方向相反：
#
#   - **文件/过程类**（办法、规则、流程、评估…）：`董事会` ⊂ `董事会议事规则`、
#     `数据安全` ⊂ `数据出境安全评估` —— 机构或概念被并进"它的文件/过程"，这是
#     把两个不同层级的东西当同一个；
#   - **机构类**（局、部、委员会、办公室…）：`市场监管` ⊂ `市场监管总局` —— 泛指
#     被并进专名。
#
# 判据是同一条："长名以这类词结尾，而短名一个这类词都不含"。分成两类是为了**不
# 误伤跨类简称**：`董事会`（机构）是 `董事会议事规则`（文件）的前缀，但它不含任何
# 文件类词，所以过不了文件类那一闸；而 `数字化转型办` 含机构类的「办」，过机构类
# 那一闸。合成一张表就分不出这两种情况。
#
# 缩写形（办/委/局）也在表里：「数字化转型办」的"办"仍然是类型词。
DOC_TYPE_WORDS = (
    "办法", "条例", "规定", "通知", "细则", "规则", "章程", "制度", "流程", "程序",
    "报告", "方案", "台账", "清单", "评估", "审查", "法",
)
ORG_TYPE_WORDS = (
    "委员会", "办公室", "管理局", "总局", "部门", "公司", "集团", "中心", "单位",
    "局", "部", "委", "办", "会", "厅", "署", "室",
)

# 泛称短名黑名单：这些名字单独出现时是**上位概念**，永远不会是某个更具体名字的同义
# 缩写。「预算」不是「年度预算」的另一个名字，「审计」也不是「内部审计」的另一个
# 名字——把它们合起来，图上"预算"的查询会命中"年度预算"的材料，而那条路径看起来
# 有证据。上面两闸管不到它们：长名那边根本没有类型词可丢（`年度预算` 以「预算」
# 结尾，而「预算」不是类型词）。
#
# 这道闸不定义构词规则，只在结构规则必错的地方踩刹车：命中即拒绝，失效模式是漏合
# （本该合的短名没合），不是错合。词表是**领域相关的**——这一份覆盖公司治理 / 财务 /
# 法务 / 制度类材料，换领域要重审。
#
# 加词的判据：这个词单独作为实体名出现时，它指的是"某一类东西"而不是"某个具体东西"。
GENERIC_TERMS = frozenset({
    "预算", "审计", "风险", "合规", "数据", "董事", "采购", "资金", "流程", "制度",
    "管理", "安全", "控制", "审查", "评估", "办法", "规则", "章程", "方案", "报告",
    "台账", "清单", "权限", "岗位", "指标", "标准", "体系", "机制", "合同", "项目",
})


@dataclass(frozen=True)
class MergeCandidate:
    """一组待合并的名字。

    `similarity` 是**组内最弱一环的强度**，单位随 `reason` 变：

      - `reason == "substring"`：长度比（`len(短) / len(长)`）；
      - `reason == "embedding"`：余弦。

    取组内最小而不是最大：如果 A~B 是 0.99、B~C 是 0.92，那么"C 也是 A 的名字"这
    件事的全部可信度来自最弱那一跳；报最大值等于让下游以为这是孪生名字。

    `reason` 是**组内最弱的判定层级**：只要组里有一条边是余弦兜底来的，整组就报
    `embedding`——那一跳的可信度低于结构证据，而它决定了整组的成色。
    """

    canonical: str          # 建议作为主名的那个
    aliases: list[str]      # 建议并进去的
    similarity: float       # 合并依据的强度（随 reason：长度比 / 余弦）
    reason: str = REASON_NONE   # "substring" | "embedding" | ""（未标注：手工构造）


def _clamp_limit(limit: int) -> int:
    """参与聚类的名字个数夹取。下限是 `MIN_NAMES`：1 个名字凑不出一对。"""
    return min(max(int(limit), MIN_NAMES), MAX_LIMIT)


def _ends_with(name: str, words: tuple[str, ...]) -> bool:
    """名字是否以某个词**结尾**（长名那一侧才需要这个方向）。

    只认结尾：`市场监管总局` 以「总局」结尾，是"专名带机构后缀"；`数据安全法` 以
    「法」结尾，是"法规带文件后缀"。中间的「管理」不算——那会把
    `国家市场监督管理总局` 这种全称误判成"加了类型词"。
    """
    return any(name.endswith(word) for word in words)


def _lost_type_word(short: str, long: str) -> bool:
    """短名是否丢掉了长名那一侧的类型词（"上位概念 ⊂ 专名"的判据）。

    长名以文件类词结尾而短名不含文件类词 → 拒（`董事会` ⊂ `董事会议事规则`）；
    长名以机构类词结尾而短名不含机构类词 → 拒（`市场监管` ⊂ `市场监管总局`）。
    两张表分开查，是因为两类词方向相反，见常量的注释。
    """
    if _ends_with(long, DOC_TYPE_WORDS) and not any(
            word in short for word in DOC_TYPE_WORDS):
        return True
    return _ends_with(long, ORG_TYPE_WORDS) and not any(
        word in short for word in ORG_TYPE_WORDS)


def _is_ordered_subsequence(short: str, long: str) -> bool:
    """`short` 是否按顺序出现在 `long` 里：可以跳字，不可乱序。

    **为什么不用连续子串**：「国家市场监督管理总局」与「市场监管总局」不是子串关系
    ——简称把「管理」两个字整段删掉了（国家|市场监督|管理|总局 → 市场监管总局），
    中间断了一处。按连续子串实现，任务书举的那个旗舰案例反而会被漏掉，只能回头靠
    余弦（bge 上 0.9349，低于 0.95 的兜底线）。

    中文简称的构词本质就是"按顺序取字"，连续性不是它的约束：市场监管总局、发改委、
    个保法、董秘都断过。
    """
    if not short or len(short) > len(long):
        return False
    remaining = iter(long)
    return all(char in remaining for char in short)


def _longest_common_run(left: str, right: str) -> int:
    """两个名字的**最长连续公共段**长度（最长公共子串，经典滚动数组 DP）。

    子序列判定太松：`数据安全法` 是 `数据出境安全评估办法` 的子序列（数|据|安|全|法
    都按序出现），于是两个不同的法规名会被硬合。真简称在这个指标上完全不同——它总有
    一大块字是**连着**的（市场监管总局的「市场监督」、董秘的「董」、个保法的「保护法」），
    而巧合匹配是东一个字西一个字。判据见 `_abbreviation_ratio` 的第三闸。
    """
    best = 0
    previous = [0] * (len(right) + 1)
    for char in left:
        current = [0] * (len(right) + 1)
        for index, other in enumerate(right, start=1):
            if char == other:
                current[index] = previous[index - 1] + 1
                if current[index] > best:
                    best = current[index]
        previous = current
    return best


def _abbreviation_ratio(left: str, right: str) -> float:
    """两个名字的结构关系强度：返回长度比，**不构成缩写形态时返回 0.0**。

    五条同时成立才算"缩写形态"，顺序按代价从便宜到贵：

      1. 短名不在 `GENERIC_TERMS` 里（泛称永远不是别名，见该常量的注释）；
      2. 长度比 ≥ `MIN_LENGTH_RATIO`；
      3. 短名是长名的**顺序子序列**（构词关系，见 `_is_ordered_subsequence`）；
      4. **最长连续公共段 ≥ 短名的一半**（`2 * run >= len(短名)`）：短名里至少有一
         半的字是连着的一整块。这一闸挡的是"子序列巧合"——`数据安全法` ⊂
         `数据出境安全评估办法`、`数据安全` ⊂ `数据出境安全评估` 这类两个不同专名
         共享高频字的形态；
      5. 短名没有丢掉长名那一侧的类型词（见 `_lost_type_word`）：`数据安全 ⊂
         数据安全法`、`董事会 ⊂ 董事会议事规则` 都是"上位/别层 ⊂ 专名"，返回 0.0。

    返回值直接当强度用：长度比越大，短名覆盖长名的比例越高，"整名缩写"的可信度越好
    （`数字化转型办公室|数字化转型办` 0.75 好过 `预算审批流程|审批流程` 0.67）。
    """
    if not left or not right or left == right:
        return 0.0
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    if short in GENERIC_TERMS:
        return 0.0
    ratio = len(short) / len(long)
    if ratio < MIN_LENGTH_RATIO:
        return 0.0
    if not _is_ordered_subsequence(short, long):
        return 0.0
    if 2 * _longest_common_run(short, long) < len(short):
        return 0.0
    if _lost_type_word(short, long):
        # 短名把"是什么"丢了：这是上位概念（或别的层级），不是简称。
        return 0.0
    return ratio


def _should_merge(left: str, right: str, cosine: float | None,
                  threshold: float = DEFAULT_THRESHOLD) -> tuple[bool, str, float]:
    """这一对该不该合并：返回 `(是否, 依据, 强度)`。**纯函数**，可以穷举。

    顺序是固定的，因为两者的性质不同：

      1. 结构关系（子序列 + 四道闸）→ 直接成立，`cosine` 连读都不读。这一条不受向量
         影响，所以 embed 失败的名字照样能凭它合并；
      2. 余弦 ≥ `threshold` → 兜底成立；
      3. 其余不合并。

    `cosine` 为 `None`（拿不到向量）时只剩第 1 条——不补零向量、不猜。
    `threshold` 只作用于兜底那一路：把它调到 1.0，结构对也不会消失。

    返回值带强度而不是只给 `bool`：判定要用一次长度比，报给下游还要用同一次结果。
    两处各算一遍迟早分叉（一边按 0.4 判、一边按 0.39 报）。
    """
    ratio = _abbreviation_ratio(left, right)
    if ratio > 0:
        return True, REASON_SUBSTRING, ratio
    if cosine is not None and cosine >= threshold:
        return True, REASON_EMBEDDING, float(cosine)
    return False, REASON_NONE, 0.0


class _DisjointSet:
    """并查集：路径压缩 + 按秩合并。

    **为什么不能"从第一个名字往下连"**：A~B、B~C 但 A 不~C 时，那种写法合出什么
    取决于遍历顺序——先遇到 A 就只合成 {A,B}，先遇到 B 就合成 {A,B,C}。同一个库
    跑两次给出两个答案，比少合一条更糟。传递闭包与顺序无关，代价只有一次几乎常数
    的 `find`。
    """

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))
        self._rank = [0] * size

    def find(self, index: int) -> int:
        root = index
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[index] != root:
            self._parent[index], index = root, self._parent[index]
        return root

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        if self._rank[left_root] < self._rank[right_root]:
            left_root, right_root = right_root, left_root
        self._parent[right_root] = left_root
        if self._rank[left_root] == self._rank[right_root]:
            self._rank[left_root] += 1


def _merge_edges(names: list[str], vectors: dict[str, list[float]],
                 threshold: float) -> list[tuple[str, str, float, str]]:
    """两两判定，返回够格合并的边 `(左, 右, 强度, 依据)`。

    名字先排序：输出顺序与字典遍历无关，同一份数据跑两次给出同一份候选，`diff` 才
    有意义。

    一对只算一次余弦（向量已 L2 归一化，余弦就是点积）；维度对不上（换过模型但缓存
    没清）就当这一对没有余弦——绝不截断对齐，与 `service._vector_scores` 同一条规则。
    没有向量的名字（embed 部分失败）**仍然参与子串判定**：那是结构证据，不依赖向量。
    """
    ordered = sorted(names)
    edges: list[tuple[str, str, float, str]] = []
    for position, left in enumerate(ordered):
        left_vector = vectors.get(left)
        for right in ordered[position + 1:]:
            cosine: float | None = None
            right_vector = vectors.get(right)
            if left_vector is not None and right_vector is not None \
                    and len(left_vector) == len(right_vector):
                cosine = _dot(left_vector, right_vector)
            merged, reason, strength = _should_merge(left, right, cosine, threshold)
            if merged:
                edges.append((left, right, strength, reason))
    return edges


def _weakness(strength: float, reason: str) -> tuple[int, float]:
    """一环"有多弱"的排序键（越大越弱）：兜底边比子串边弱，同层内强度低的更弱。

    层级放在强度前面，是因为两者单位不同（余弦 / 长度比），不能直接比大小：
    「0.95 的余弦兜底」与「0.83 的子串缩写」，前者的可信度更低——它只说明两个向量
    靠得近，不说明构词上有关系。
    """
    return (0 if reason == REASON_SUBSTRING else 1, -strength)


def _group_pairs(
    edges: list[tuple[str, str, float, str]],
) -> list[tuple[list[str], float, str]]:
    """把"够格合并"的边聚成组，返回 `[(成员, 强度, 依据)]`。

    只返回成员 ≥ 2 的组：单打独斗的名字没有可合并的对象，把它们也列出来只会让
    "候选组数"这个指标虚高。

    组级强度取组内最小值，组级依据取**最弱的那一环**（见 `_weakness`）：只要组里有
    一条边是余弦兜底来的，整组报 `embedding`——那一跳决定了整组的成色。排序把兜底组
    放前面：它比子串组更需要人看。
    """
    names: list[str] = []
    index: dict[str, int] = {}
    for left, right, _strength, _reason in edges:
        for name in (left, right):
            if name not in index:
                index[name] = len(names)
                names.append(name)
    if not names:
        return []

    forest = _DisjointSet(len(names))
    for left, right, _strength, _reason in edges:
        forest.union(index[left], index[right])

    # 最弱一环：并查集定完之后每条边只属于一个组，按组取最小是精确的。
    weakest: dict[int, tuple[float, str]] = {}
    for left, _right, strength, reason in edges:
        root = forest.find(index[left])
        current = weakest.get(root)
        if current is None or _weakness(strength, reason) > _weakness(*current):
            weakest[root] = (strength, reason)

    members: dict[int, list[str]] = {}
    for name, position in index.items():
        members.setdefault(forest.find(position), []).append(name)
    groups = [(sorted(group), *weakest[root]) for root, group in members.items()
              if len(group) > 1]
    groups.sort(key=lambda item: (item[2] == REASON_SUBSTRING, -item[1], item[0][0]))
    return groups


def _pick_canonical(members: list[str], degrees: dict[str, int]) -> str:
    """主名：**边数最多的**那个；并列时名字最长的；再并列取字典序小的。

    边数优先——主名会出现在检索结果、日志和界面里，边多的那个名字信息最丰富，
    也最可能是抽取层反复见到的写法。长度其次——「国家市场监督管理总局」比「市场
    监管总局」更适合当主名（它更正式，也不会把"市场监管"这个通用词组误当专名）。
    第三档只是为了确定性，不承载任何语义。
    """
    return min(members, key=lambda name: (-degrees.get(name, 0), -len(name), name))


def _name_vectors(names: list[str]) -> dict[str, list[float]]:
    """名字 → 单位向量。缓存优先，缺失的合成一次 `embed_texts`。

    缓存键用现成的 `vector_cache.text_hash`（与块向量同一张表、同一个模型维度）：
    一次消解要跑几百个名字，隔天再跑一遍不该重新烧额度。名字和块文本共用一个
    缓存表没有副作用——同一段文本在任何上下文里的向量本来就该一样。

    分批在 `embed_texts` 里做（`MAX_BATCH = 25`），这里不再切一遍：两处各有一套
    批大小，迟早分叉成"一片 25 条、一片 40 条"。

    返回的字典里只放拿到了向量的名字。缺席**只影响余弦那一路**：子串判定是纯结构的，
    没有向量的名字照样能凭它合并（模块 docstring 的「不合并的边界」）。
    """
    model = embedding_model()
    hashes = [vector_cache.text_hash(name) for name in names]
    cached = vector_cache.get_vectors(hashes, model)
    vectors: dict[str, list[float]] = {}
    missing: list[tuple[str, str]] = []
    for name, hash_value in zip(names, hashes):
        vector = cached.get(hash_value)
        if vector:
            vectors[name] = vector
        else:
            missing.append((name, hash_value))
    if not missing:
        return vectors

    fresh = embed_texts([name for name, _hash in missing])
    written: dict[str, list[float]] = {}
    for (name, hash_value), vector in zip(missing, fresh):
        if not vector:
            continue
        vectors[name] = vector
        written[hash_value] = vector
    if written:
        vector_cache.put_vectors(list(written.items()), model)
    return vectors


async def find_merge_candidates(
    *, threshold: float = DEFAULT_THRESHOLD, limit: int = DEFAULT_LIMIT,
) -> list[MergeCandidate]:
    """找出"可能是同一个事物"的名字组，按依据强度降序返回。

    流程：取度数最高的 `limit` 个实体名 → 两两判定（子串 / 余弦兜底）→ 并查集聚组
    → 每组选一个主名。判定的分工见模块 docstring。

    **没配 embedding 时返回 `[]`，不抛错**：这是"还没配"这个预期状态，不是故障
    （与 `service._vector_scores` 的 `unconfigured` 同一口径）。但**配了却调不通**
    时抛 `EmbeddingError`——两者必须分开：前者返回空是答案，后者返回空会被读成
    "没有可合并的名字"，而实际上一次都没比过。哑失败是这一层要消掉的东西，不能
    在这里再造一个。

    注意 `threshold` 只作用于**余弦兜底**那一路；子串关系不看它（把 threshold 调到
    1.0，子串对也不会消失）。这是刻意的：`threshold` 是数值判据的闸，不是总闸。
    """
    started = time.perf_counter()
    if not embedding_configured():
        # 一次 HTTP 都不发。子串判定本来不需要向量，但候选生成是"embedding 驱动"的
        # 一条路，没配就整条不起——这条契约从第一版就在，不在这里改。
        logger.info("实体消解跳过：未配置 embedding")
        return []

    cap = _clamp_limit(limit)
    rows = graph_store.entity_degrees(limit=cap)
    names = [name for name, _degree in rows]
    degrees = dict(rows)
    if len(names) < MIN_NAMES:
        logger.info("实体消解：只有 %d 个实体，无需合并", len(names))
        return []

    try:
        vectors = _name_vectors(names)
    except EmbeddingNotConfigured:
        # 预检与调用之间凭证被清掉：并发下真会发生，仍按"未配置"处理，不是故障。
        logger.info("实体消解跳过：embedding 凭证在调用前消失")
        return []

    edges = _merge_edges(names, vectors, threshold)
    candidates: list[MergeCandidate] = []
    for members, strength, reason in _group_pairs(edges):
        canonical = _pick_canonical(members, degrees)
        candidates.append(MergeCandidate(
            canonical=canonical,
            aliases=[name for name in members if name != canonical],
            similarity=strength,
            reason=reason,
        ))

    logger.info(
        "实体消解：实体 %d / 有向量 %d / 子串边 %d / 兜底边 %d / 候选组 %d / "
        "耗时 %dms",
        len(names), len(vectors),
        sum(1 for edge in edges if edge[3] == REASON_SUBSTRING),
        sum(1 for edge in edges if edge[3] == REASON_EMBEDDING),
        len(candidates), int((time.perf_counter() - started) * 1000),
    )
    return candidates


def _describe(candidates: list[MergeCandidate]) -> list[dict]:
    """候选的可读形态（`resolve_entities` 的返回值用）。强度留四位小数。

    `reason` 一定要出现在这里：dry-run 的全部价值就是让人在落库前看出"这一组是凭
    子串进的，还是凭余弦兜底进的"——后者应当被更仔细地看。
    """
    return [
        {"canonical": candidate.canonical,
         "aliases": list(candidate.aliases),
         "similarity": round(candidate.similarity, 4),
         "reason": candidate.reason or REASON_NONE}
        for candidate in candidates
    ]


async def resolve_entities(
    *, threshold: float = DEFAULT_THRESHOLD, dry_run: bool = True,
) -> dict:
    """跑一遍候选生成；（`dry_run=False` 时）把候选落成别名边，返回统计。

    **默认 `dry_run=True`**：合并是破坏性的——一旦落库，`neighbors` 的路径会变，
    而错合会造出一条有证据、有位置、能通过校验的假路径。所以默认只算不写，调用方
    先把候选打出来看一眼，确认了再 `dry_run=False` 跑第二遍（第二遍的向量全部命中
    缓存，不会再花额度）。

    返回值的键固定为五个，dry-run 与非 dry-run 同构：

      - `dry_run`：这一次到底写没写；
      - `groups`：候选组数；
      - `aliases`：**真正新建**的别名边数（dry-run 恒为 0）；
      - `skipped`：想写却没写成的对数 = 总对数 - `aliases`。已存在的、成环被拒的、
        两端有名字在库里找不到节点的，都算在这里。
      - `candidates`：`[{canonical, aliases, similarity, reason}]`，dry-run 的
        意义所在。

    `aliases == 0` 且 `groups > 0` 且非 dry-run，说明这一遍是重跑（边早就在了）——
    幂等，不是失败。
    """
    candidates = await find_merge_candidates(threshold=threshold)
    listing = _describe(candidates)
    pairs = sum(len(candidate.aliases) for candidate in candidates)
    if dry_run:
        logger.info(
            "实体消解 dry-run：候选组 %d（子串 %d / 兜底 %d）/ 待写别名 %d（未写库）",
            len(candidates),
            sum(1 for item in candidates if item.reason == REASON_SUBSTRING),
            sum(1 for item in candidates if item.reason == REASON_EMBEDDING),
            pairs,
        )
        return {"dry_run": True, "groups": len(candidates), "aliases": 0,
                "skipped": 0, "candidates": listing}

    written = graph_store.add_aliases(candidates)
    logger.info("实体消解落库：候选组 %d / 写入别名 %d / 跳过 %d",
                len(candidates), written, pairs - written)
    return {"dry_run": False, "groups": len(candidates), "aliases": written,
            "skipped": pairs - written, "candidates": listing}
