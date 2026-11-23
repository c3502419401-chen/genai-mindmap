"""`SuggestionKind.link` 的第一版产出方：**新加的节点该挂在谁下面**。

## 它补的是哪一格

`SuggestionKind.link`（`contracts/diagnostic.py`）从定义那一天起就是空位——注释写
"补跨节点关系"，而全仓没有任何地方产出它。原因不是忘了，是**没有判据**：

    要判"这个新节点与哪条论证线同源"，先得知道"用户把上一个新节点挂在了哪"
    ——而 edit_events 里 add 事件一条都没有（565 条里 update 559 / style 4 /
    remove 2）。没有这一列，任何"判断逻辑联系"的算法都是在猜。

`parent_id` 补上之后（`mindmaps/edits.py::parents_of`），这一格才有底。但**底很薄**：
库里 `add` 零条，所以第一版拿不到"这个用户怎么挂节点"的历史，只能靠两样东西——
**跨图历史同类节点**（别人把同一个概念挂在谁下面）与**材料原文**（这个概念在哪一段
被论述，那一段的小标题就是它该待的地方）。

## 方法论：先描述，再决定

借的是 Coco 论文 §6.1 那条**有消融支撑**的洞察：只让模型输出决定（Decision Only）
时 Qwen-3.5-9B 从 0.4832 掉到 0.3045——**先输出"这是什么 / 意图是什么"再决定，比
直接判定好约 37%**。

映射到这一步就是：不让模型直接吐 `parent_id`，而是让它先写

    what  这个节点讲的是什么（一句话）
    line  它与哪条论证线同源（指向某个一级分支或某个历史节点）

再写候选。**这两行不是装饰**：它们在同一次生成里**排在候选前面**，模型必须先把它
写下来才能接着写候选——这就是"结构化先于决定"在单次调用里的等价形式，而它省掉
第二次往返（论文那边是两次生成，成本翻倍）。

`two_stage=True` 保留了这个消融：第一段只问 `what` / `line`，第二段把第一段的输出
当成已知条件再问候选。要复现论文的对照（Decision Only / 先描述再决定）就跑它，
见 `scripts/measure_link_placement.py`。

## 输出形状

    {
      "what":        "这个节点讲的是什么（一句话）",
      "line":        "它与哪条论证线同源",
      "candidates":  [{"parent_id": "...", "label": "...", "why": "...",
                       "evidence": ["node:...", "chunk:..."]}],
      "suggestion_kind": "link"
    }

它落在 `Suggestion` 上：`kind=SuggestionKind.link`、`node_id`=新节点、
`message`=一句话人话、`evidence`=**上面那个对象的 JSON**。

`evidence` 装 JSON 而不是加字段，是硬约束：`Suggestion` 是
`ConfigDict(extra="forbid")` 且是 `/optimization/suggest` 的响应模型，给它加字段
就要重导 `openapi.json` 与 `schema.d.ts`——那两个文件正在被别人改，而这一步还没
证明自己有用。

## `evidence` 必须能回指

每个候选的 `evidence` 只能从**这次请求里给过模型的 id** 里选：

    node:<节点 id>              树上的节点
    map:<map_id>#<label>        历史图里的同类节点（跨图参照）
    chunk:<source_id>:<start>   材料原文的一段（可按 source_id + 偏移跳回去）

解析时逐条校验，**不在允许集合里的一律丢弃**（`dropped_evidence`）。理由与
"引文必须逐字出现在原文里"是同一条：一个指向不存在 id 的"依据"比没有依据更坏——
它看起来可查，点下去是空的。

## 只给建议，不自动改

产出的是 `Suggestion`，前端 `stores/structure.ts::APPLIERS` 里**没有 `link`**，
`mindmap/diagnostics.ts::APPLICABLE_KINDS` 的白名单也没有它——所以界面上它只有
"忽略"，没有"应用"。这不是没接完，是唯一正确的形态：

  · 改层级不可逆（撤销只救回这一次会话）；
  · 论文 Appendix A 第一条 limitation 正是"用户想看到、改正、忘掉系统学到的东西"，
    作者自己把可审阅/可纠正列进 future work——我们站在他们承认没做好的那一侧。

要接"点一下才改"，动作层已经现成（`agent/tools.py::move_node` + `tree_invalid` 的
环/层级校验，`prefs.suggestion_accepted` 是合法写入来源）。缺的只是"应用它到底改成
什么"的定义，而那要有样本才能定义。

## 开关与失败

`MINDMAP_OPTIMIZATION_LINK` 缺省**关**：这一步要一次模型调用（可能还要一次检索），
与 `cross_map` 同一条规矩——不在用户没点的时候花钱。关闭时连 import 都不多。

**绝不抛异常**：返回空列表。建议是附加信息，判不出来不等于这张图坏了。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from app.contracts.diagnostic import Suggestion, SuggestionKind
from app.contracts.mindmap import DiagramContent, Node

logger = logging.getLogger(__name__)

ENV = "MINDMAP_OPTIMIZATION_LINK"
_TRUTHY = {"1", "true", "yes", "on", "y", "t"}

#: 最多给几条候选。3 是"够选、又不至于让用户读五行"的界，也是
#: `scripts/measure_link_placement.py` 的封闭集实验的分支数（三选一 → 随机基线 1/3，
#: 这是"这个方法有没有用"唯一诚实的对照）。
MAX_CANDIDATES = 3

#: 给模型的父候选窗口上限。超了按"与目标节点的字面重合"截断：这一步要的是
#: "它该挂哪"，而不是"遍历整棵树"。40 与 `cross_map.MAX_QUERY_NODES` 同量级。
MAX_PARENT_CHOICES = 24

#: 材料那一侧取几段原文。3 段够定位"这个概念在哪一节"，再多只是让提示词变长。
MAX_MATERIAL_HITS = 3

#: 跨图那一侧取几个同类节点。
MAX_HISTORY_MATCHES = 3

#: 历史节点与目标节点的余弦阈值。与 `cross_map.MATCH_THRESHOLD` 同一个数：
#: 全库 253912 个节点对的相似度 p95 = 0.693，0.80 以上基本是"同一个概念的
#: 不同说法"；更低的阈值会把相邻概念拉进来（0.75 附近大量出现）。
MATCH_THRESHOLD = 0.80

#: 与 `verify.py` 同一个量级：一次要吐 3 个候选 + 3 条证据，8192 够且不失控。
DEFAULT_MAX_TOKENS = 8192


def enabled() -> bool:
    """这一步的开关状态。稳定接口：调用方用它做闸门。"""
    return str(os.getenv(ENV, "")).strip().lower() in _TRUTHY


# ---------------------------------------------------------------- 结构：谁可以当父


@dataclass(frozen=True)
class _ParentChoice:
    """一个可以当父的节点，喂给模型的一条。"""

    node_id: str
    label: str
    depth: int
    #: 它自己的子节点标签（前几个）。模型据此判断"这一支在讲什么"，比只看标题准。
    children: tuple[str, ...] = ()
    #: 它是不是目标节点**当前**的父。真话要说：模型有权选一个"不用动"。
    current: bool = False


def _subtree_ids(node: Node) -> set[str]:
    """`node` 自己 + 全部后代。它们是"不能当自己的父"的那些。"""
    found: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        found.add(current.id)
        stack.extend(current.children)
    return found


def _walk(content: DiagramContent) -> list[tuple[Node, int, str | None]]:
    """`(节点, 层号, 父 id)`，多棵并列子树各从自己的根数起。

    口径与 `mindmaps/edits.py::parents_of` / 前端 `edit-log.ts::indexForest` 逐条
    对齐——**同一个父节点口径只能有一份语义**，否则同一条挂载关系在两个地方会
    读出两个答案，而读的人不知道信哪个。
    """
    from app.agent import shapes

    rows: list[tuple[Node, int, str | None]] = []
    for root in shapes.roots(content):
        stack: list[tuple[Node, int, str | None]] = [(root, 1, None)]
        while stack:
            node, depth, parent = stack.pop()
            rows.append((node, depth, parent))
            stack.extend((child, depth + 1, node.id) for child in reversed(node.children))
    return rows


def _overlap(target: Node, node: Node) -> int:
    """两个字面集合的交集大小。只用来给候选排序，不参与判据。"""
    left = set(target.label) | set(target.detail or '')
    right = set(node.label) | set(node.detail or '')
    return len(left & right)


def parent_choices(
    content: DiagramContent,
    node_id: str,
    *,
    target: Node | None = None,
    limit: int = MAX_PARENT_CHOICES,
) -> list[_ParentChoice]:
    """可以当 `node_id` 的父的那些节点，**按与目标的字面重合降序**，当前父置顶。

    三条硬排除：

      - 它自己与它的全部后代（挂到后代会成环，`move_node` 会以 `tree_invalid` 拒）；
      - 指向不存在节点的 id（历史脏数据）；
      - 深度已经到顶的那些（`MAX_TREE_DEPTH`，挂上去必然被拒）。

    `target` 是"这个节点本人"的覆盖值：**当它不在 `content` 上时**（还没挂上、
    或者被藏起来做实验），只能由调用方给。给了之后就没有"当前父"（`current`
    全为假）——真话：它现在不挂在任何地方。

    排序不是"最像的就该当父"——那是模型的判断。这里只保证**窗口截断时不至于把
    最可能的那几个先扔掉**：当前父永远留，然后按字面重合。第一版没有 embedding
    介入这一层（那要多一次远程调用），字面重合是不花钱能拿到的最好近似。
    """
    from app.contracts.mindmap import MAX_TREE_DEPTH

    rows = _walk(content)
    by_id = {node.id: (node, depth, parent) for node, depth, parent in rows}
    mine = target if target is not None else (by_id[node_id][0] if node_id in by_id else None)
    if mine is None:
        return []
    current_parent = by_id[node_id][2] if node_id in by_id else None
    banned = _subtree_ids(mine)

    choices: list[_ParentChoice] = []
    for node, depth, _ in rows:
        if node.id in banned:
            continue
        # 挂上去之后目标节点的层号是 `depth + 1`，超过契约上限的那一层不可能是答案。
        if depth + 1 > MAX_TREE_DEPTH:
            continue
        choices.append(_ParentChoice(
            node_id=node.id,
            label=node.label.strip(),
            depth=depth,
            children=tuple(child.label.strip() for child in node.children[:4]),
            current=node.id == current_parent,
        ))
    choices.sort(key=lambda item: (not item.current, -_overlap(mine, by_id[item.node_id][0]),
                                   item.depth, item.node_id))
    return choices[:limit]


# ---------------------------------------------------------------- 跨图历史


def _label_index(content: DiagramContent) -> dict[str, str]:
    return {node.id: node.label.strip() for node, _, _ in _walk(content)}


def history_placements(
    content: DiagramContent,
    node: Node,
    *,
    map_id: str | None = None,
    limit: int = MAX_HISTORY_MATCHES,
    records: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    """历史图里与 `node` 最像的节点，**它们各自挂在谁下面**。

    这是第一版唯一能替代"这个用户的历史"的东西，而它回答的正是同一个问题——
    "别人把这个概念放在哪"。实现借 `cross_map` 已有的一套：节点向量按文本哈希
    缓存在 `knowledge.vector_cache`（复用，不新增表），历史侧**只读缓存**（现算
    历史库等于让一次建议等一分钟），查询侧现算一次。

    `records` 是历史库的注入缝（缺省 `repository.list_maps`）：`scripts/
    measure_link_placement.py` 用它把 `runs.candidate` 那批真图当历史，因为
    `mindmaps` 表里只有二十几张基线图，而那批候选是七十三张、二十个不同主题。

    返回 `[{"ref": "map:<id>#<label>", "parent": "<父标签>", "map_id": ..., "score": ...}]`。
    `ref` 的格式与 `cross_map` 的 `evidence` 逐字一致（`<map_id>#<节点标签>`），
    只是多一个 `map:` 前缀——它是这一步"证据必须能回指"的 id 形态。

    **任何一环取不到就返回空列表**：没有历史只是"少一条依据"，不是错误。
    """
    try:
        from app.knowledge import embedding, vector_cache
        from app.mindmaps import repository

        label = node.label.strip()
        if not label:
            return []
        model = embedding.model_name()
        if records is None:
            records = repository.list_maps(tolerant=True)
        history: list[tuple[str, str, Node, str]] = []
        for record in records:
            if record.id == map_id:
                continue
            if not isinstance(record.content, type(content)):
                continue
            labels = _label_index(record.content)
            for other, _, parent in _walk(record.content):
                if parent is None or not other.label.strip():
                    continue
                history.append((record.id, record.content.title, other, labels.get(parent, '')))
        if not history:
            return []

        hashes = [vector_cache.text_hash(item[2].label.strip()) for item in history]
        cached = vector_cache.get_vectors(hashes, model)
        mine_key = vector_cache.text_hash(label)
        mine = cached.get(mine_key)
        if mine is None:
            fresh = embedding.embed_texts([label])
            mine = fresh[0] if fresh else None
            if mine:
                vector_cache.put_vectors([(mine_key, mine)], model)
        if not mine:
            return []

        import numpy as np

        rows = [(index, cached[key]) for index, key in enumerate(hashes) if key in cached]
        if not rows:
            return []
        base = np.asarray([vector for _, vector in rows], dtype=np.float32)
        query = np.asarray(mine, dtype=np.float32)
        sims = base @ query
        order = np.argsort(-sims)[: max(limit * 3, limit)]

        matches: list[dict[str, Any]] = []
        for position in order:
            score = float(sims[position])
            if score < MATCH_THRESHOLD:
                continue
            hist_index = rows[position][0]
            record_id, title, other, parent_label = history[hist_index]
            matches.append({
                "ref": f"map:{record_id}#{other.label.strip()}",
                "parent": parent_label,
                "map_id": record_id,
                "title": title,
                "score": round(score, 3),
            })
            if len(matches) >= limit:
                break
        return matches
    except Exception:  # noqa: BLE001 — 跨图依据取不到只等于"这一次没有它"
        logger.warning("link：跨图历史取不到，本次只用树与材料", exc_info=True)
        return []


# ---------------------------------------------------------------- 材料原文


def material_hits(query: str, *, limit: int = MAX_MATERIAL_HITS) -> list[dict[str, Any]]:
    """从已入库的材料里检索与 `query` 最相关的几段，**带可回指的 id**。

    id 形态 `chunk:<source_id>:<start>`——`start` 是 Unicode 码点偏移，与
    `EvidenceHit` / `SourceRef` 同口径，前端拿它 slice 出来的就是那段原文。

    材料库空着、embedding 不可用、开关关着，一律返回空列表。
    """
    try:
        from app.knowledge import library, service

        sources = library.recent(limit=20)
        if not sources:
            return []
        hits = service.search_sources(sources, query, top_k=limit)
        return [
            {
                "ref": f"chunk:{hit.source_id}:{hit.start}",
                "source_id": hit.source_id,
                "start": hit.start,
                "end": hit.end,
                "text": hit.quote[:400],
            }
            for hit in hits[:limit]
        ]
    except Exception:  # noqa: BLE001 — 材料取不到只是"少一条依据"
        logger.warning("link：材料检索失败，本次只用树与历史", exc_info=True)
        return []


# ---------------------------------------------------------------- 提示词


def _describe(choice: _ParentChoice) -> str:
    marks = []
    if choice.current:
        marks.append("当前就在这")
    line = f"- {choice.node_id}｜{choice.label}（第 {choice.depth} 层）"
    if choice.children:
        line += f"｜下面有：{'、'.join(choice.children)}"
    if marks:
        line += f"｜{'，'.join(marks)}"
    return line


SYSTEM = """你在帮一个人整理思维导图。他刚手工加了一个节点，而"这个节点该挂在哪儿"还没定。

**先回答两件事，再给候选：**

1. `what`：这个节点讲的是什么（一句话，不超过 30 字）。
2. `line`：它与哪条论证线同源——指向列表里的某个一级分支，或某个历史/材料片段。

**只有写完上面两行，才开始填 `candidates`。** 候选是"这个节点应该挂在谁下面"，
每条要有：`parent_id`（只能从给定的候选父里选）、`label`、`why`（为什么挂在它下面，
一句话）、`evidence`（依据的 id 列表）。

**证据规则（硬性）**：`evidence` 里的每一项必须是**这次请求里给过你的 id**：

    node:<节点 id>            树上的节点
    map:<map_id>#<标签>       历史图里的同类节点
    chunk:<source_id>:<start> 材料原文的一段

不许写"根据用户习惯"这类没有 id 的话，也不许编一个不在列表里的 id——编的会被
机械校验丢掉，那条候选就白写了。没有依据就给空数组 `[]`，那比编一个诚实。

**候选的判据是"它讲的这件事属于哪一支"**，不是"哪个父节点名字更像"。标题短、
标题重名都很常见（两个一级分支下都叫「风险」），靠字面像去选会挂错。看的是
这个节点要说的内容，在这棵树里该跟谁待在一起。

输出**纯 JSON**，不要 markdown 代码块：

{"what": "...", "line": "...", "candidates": [{"parent_id": "...", "label": "...",
 "why": "...", "evidence": ["node:..."]}], "suggestion_kind": "link"}"""


#: **对照组**：只让模型输出决定（论文 §6.1 的 Decision Only，Qwen-3.5-9B 上
#: 0.4832 → 0.3045）。它的存在不是为了跑得更好，是为了**能证明"先描述"这一步
#: 在这个仓库的数据上到底值多少**——没有它，"借了论文的洞察"就只是一句引用。
SYSTEM_DECISION_ONLY = """你在帮一个人整理思维导图。他刚加了一个节点，而"这个节点该挂在哪儿"
还没定。

从给定的候选父里选出最合适的，给出 `candidates`：每条要有 `parent_id`（只能从候选里选）、
`label`、`why`（一句话）、`evidence`（依据的 id 列表）。

**证据规则（硬性）**：`evidence` 的每一项必须是这次请求里给过你的 id：`node:<id>` /
`map:<map_id>#<标签>` / `chunk:<source_id>:<start>`。编一个不在列表里的 id 会被机械
校验丢掉。没有依据就给 `[]`。

输出**纯 JSON**，不要 markdown 代码块：

{"candidates": [{"parent_id": "...", "label": "...", "why": "...", "evidence": []}],
 "suggestion_kind": "link"}"""


def messages(
    content: DiagramContent,
    node: Node,
    *,
    choices: Sequence[_ParentChoice] | None = None,
    history: Sequence[dict[str, Any]] = (),
    material: Sequence[dict[str, Any]] = (),
    stage: str = "decide",
    what: str = '',
    line: str = '',
) -> tuple[str, str]:
    """`(system, user)`。`stage` 有四个取值，它选的是**问模型的方式**：

        decode       先写 what / line，再给候选（默认，论文更好的那一侧）
        describe     只问 what / line（`two_stage` 的第一段）
        two_stage    把第一段的答案当成已知条件再问候选
        decide_only  只问候选（论文更差的那一侧，用来量化"先描述"值多少）

    `node` 是**这个节点本人**，不要求它已经在 `content` 上：实验里它被藏起来，
    而"它原来挂在谁下面"正是要判的东西。

    拆成独立函数是为了让"提示词长什么样"能被用例钉住：这一版最容易悄悄退化的地方
    就是提示词被改得只剩"选一个父节点"——那时它退化成论文里更差的那一侧，而产出
    看起来仍然像模像样。
    """
    if choices is None:
        choices = parent_choices(content, node.id, target=node)

    lines = [f"图的标题：{content.title}", "", "新加的节点：",
             f"- 标签：{node.label.strip()}"]
    if (node.detail or '').strip():
        lines.append(f"- 说明：{node.detail.strip()[:600]}")
    current = next((choice for choice in choices if choice.current), None)
    if current is not None:
        lines.append(f"- 它现在挂在：{current.label}")
    else:
        lines.append("- 它现在不挂在任何地方（还没定）")

    lines += ["", f"可以当父的节点（共 {len(choices)} 个，只能从这里面选 parent_id）："]
    lines += [_describe(choice) for choice in choices]

    if history:
        lines += ["", "历史图里意思相近的节点，它们各自挂在谁下面："]
        for item in history:
            lines.append(f"- {item['ref']}｜挂在「{item['parent']}」下面"
                         f"｜出自《{item.get('title', '')}》｜相似度 {item['score']}")
    if material:
        lines += ["", "材料里相关的段落："]
        for item in material:
            lines.append(f"- {item['ref']}｜{item['text']}")

    if stage == "describe":
        lines += ["", "现在只做第一步：回答 what 与 line，先不要给候选。",
                  '输出：{"what": "...", "line": "..."}']
    elif stage == "two_stage":
        lines += ["", "前面已经判过：", f"- what：{what}", f"- line：{line}",
                  "", "据此给候选。"]
    elif stage == "decide_only":
        lines += ["", "给出候选。"]
    else:
        lines += ["", "先写 what 与 line，再给候选。"]
    system = SYSTEM_DECISION_ONLY if stage == "decide_only" else SYSTEM
    return system, "\n".join(lines)


# ---------------------------------------------------------------- 解析


def allowed_evidence(
    choices: Sequence[_ParentChoice],
    history: Sequence[dict[str, Any]],
    material: Sequence[dict[str, Any]],
) -> dict[str, str]:
    """这次请求里**真实存在**的证据 id → 它指的东西。

    解析候选时用它做机械校验：模型写的 id 不在这张表里，就丢掉那一条证据。这是
    "证据必须能回指"的唯一实现方式——信任模型的自觉等于没有校验。
    """
    table: dict[str, str] = {}
    for choice in choices:
        table[f"node:{choice.node_id}"] = choice.label
    for item in history:
        table[item["ref"]] = f"历史图《{item.get('title', '')}》里的同名节点"
    for item in material:
        table[item["ref"]] = "材料原文"
    return table


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ''


def parse(
    payload: Any,
    choices: Sequence[_ParentChoice],
    history: Sequence[dict[str, Any]] = (),
    material: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """模型输出 → 规范化后的形状。**不做补救，只做拒绝**。

    三处拒绝，都是为了不产出"看起来对、其实无据"的东西：

      - `parent_id` 不在候选里 → 丢那条候选（挂到一个不存在的父上，界面无处可点）；
      - `evidence` 不在允许集合里 → 丢那条证据（编的 id 比没有依据更坏）；
      - `parent_id` 重复 → 只留第一条（同一个父给两个理由，用户选不了）。

    返回里带 `dropped_candidates` / `dropped_evidence`，让"模型有多不听话"可观测——
    这两个数进 `data`，是调提示词的唯一依据。
    """
    if not isinstance(payload, dict):
        return {"what": '', "line": '', "candidates": [],
                "dropped_candidates": 0, "dropped_evidence": 0}
    by_id = {choice.node_id: choice for choice in choices}
    allowed = allowed_evidence(choices, history, material)

    what = _as_text(payload.get("what"))
    line = _as_text(payload.get("line"))
    raw = payload.get("candidates")
    if not isinstance(raw, list):
        raw = []

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    dropped_candidates = 0
    dropped_evidence = 0
    for item in raw:
        if not isinstance(item, dict):
            dropped_candidates += 1
            continue
        parent_id = _as_text(item.get("parent_id"))
        if parent_id not in by_id or parent_id in seen:
            dropped_candidates += 1
            continue
        seen.add(parent_id)
        evidence: list[str] = []
        for ref in item.get("evidence") or []:
            if isinstance(ref, str) and ref.strip() in allowed:
                evidence.append(ref.strip())
            else:
                dropped_evidence += 1
        candidates.append({
            "parent_id": parent_id,
            "label": by_id[parent_id].label,
            "why": _as_text(item.get("why"))[:300],
            # 模型爱写"哪个父节点名字更像"的理由，那类话没有 id 支撑，就在这里
            # 被降级成空数组——候选本身还留着（理由弱不等于挂错）。
            "evidence": evidence[:6],
        })
        if len(candidates) >= MAX_CANDIDATES:
            break

    return {
        "what": what[:200],
        "line": line[:200],
        "candidates": candidates,
        "dropped_candidates": dropped_candidates,
        "dropped_evidence": dropped_evidence,
    }


def _payload_of(response: Any) -> Any:
    """`chat_async` 的返回值 → `parse` 能吃的东西。

    与 `optimization/verify.py::_payload_of` 同一条：`chat_async` 返回的**已经是解析
    好的 JSON**（不是 `{"content": "..."}`）。字符串原样返回，不 `json.dumps` 一遍
    ——那会把"解析失败的正文"变成一个合法的 JSON 字符串，于是静默变成空结果。
    """
    if isinstance(response, dict) and isinstance(response.get("content"), str):
        return response["content"]
    return response


def _loads(text: Any) -> Any:
    """模型偶尔裹一层 markdown 代码块。剥掉再解；解不开就是解不开，不猜。"""
    if not isinstance(text, str):
        return text
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- 对外


@dataclass
class LinkReport:
    """一次推理的全貌。`suggestions` 之外的东西都是给实验与排查用的。"""

    suggestions: list[Suggestion] = field(default_factory=list)
    hints: list[dict[str, Any]] = field(default_factory=list)
    calls: int = 0
    dropped_candidates: int = 0
    dropped_evidence: int = 0
    skipped: str = ''


async def suggest(
    content: DiagramContent,
    node_id: str,
    *,
    map_id: str | None = None,
    chat: Callable[..., Any] | None = None,
    history: Sequence[dict[str, Any]] | None = None,
    history_records: Sequence[Any] | None = None,
    material: Sequence[dict[str, Any]] | None = None,
    choices: Sequence[_ParentChoice] | None = None,
    target: Node | None = None,
    two_stage: bool = False,
    decision_only: bool = False,
    max_tokens: int | None = None,
    on: bool | None = None,
) -> LinkReport:
    """给 `node_id` 这个节点产出 `kind="link"` 的建议。**最多一条，带 1–3 个候选。**

    一条而不是三条：用户看到的是"这个节点该挂哪"，三个候选是同一条建议里的选项
    （`candidates`），不是三条互相竞争的建议。拆成三条会让面板上出现同一件事说三遍。

    `target` 是"这个节点本人"的覆盖值——**当它不在 `content` 上时**（还没挂上，
    或者被藏起来做实验）必须由调用方给。不给就从树上按 id 找。

    `history` / `material` / `choices` / `chat` 都是注入缝：缺省走真实库与
    `generation.service.chat_async`。**全部可注入**，所以它在用例里能对着固定文本
    跑——不需要 HTTP、不需要库、不需要模型（与 `verify.verify_tree` 同一套）。

    `two_stage` / `decision_only` 是消融的两个极端，产出形状完全相同：
    前者两次调用（先描述、再把描述当条件），后者一次调用但不许先描述。
    留着它们不是为了更好，是为了**"先描述"这一步到底值多少能被量出来**。

    `on`：`None` 走环境变量；显式值必须由调用方传（与 `cross_map.suggestions` 同一条：
    在模块里各判一次，调用方的开关就废了）。
    """
    report = LinkReport()
    if not (on if on is not None else enabled()):
        report.skipped = "disabled"
        return report

    rows = _walk(content)
    node = target
    if node is None:
        node = next((item for item, _, _ in rows if item.id == node_id), None)
    if node is None or not node.label.strip():
        report.skipped = "no_node"
        return report
    if choices is None:
        choices = parent_choices(content, node_id, target=node)
    if len(choices) < 2:
        # 只有一个候选（"只能挂这儿"）不叫建议；零个候选是根节点，无处可挂。
        report.skipped = "no_choices"
        return report

    if history is None:
        history = history_placements(content, node, map_id=map_id, records=history_records)
    if material is None:
        material = material_hits(f"{content.title} {node.label.strip()}".strip())

    if chat is None:
        from app.generation.service import chat_async as model_chat

        async def chat(system: str, user: str, **kwargs: Any) -> Any:  # noqa: F811
            return await model_chat(system, user, **kwargs)

    what = line = ''
    if two_stage:
        system, user = messages(content, node, choices=choices, history=history,
                                material=material, stage="describe")
        try:
            described = _loads(_payload_of(await chat(system, user,
                                                      max_tokens=max_tokens or DEFAULT_MAX_TOKENS)))
            report.calls += 1
            if isinstance(described, dict):
                what = _as_text(described.get("what"))
                line = _as_text(described.get("line"))
        except Exception as exc:  # noqa: BLE001 — 第一段失败就退回单次调用，不空转
            logger.warning("link：描述那一段失败（%s）：%s", node_id, type(exc).__name__)

    if decision_only:
        stage = "decide_only"
    elif two_stage and what:
        stage = "two_stage"
    else:
        stage = "decide"
    system, user = messages(content, node, choices=choices, history=history,
                            material=material, stage=stage, what=what, line=line)
    try:
        response = await chat(system, user, max_tokens=max_tokens or DEFAULT_MAX_TOKENS)
        report.calls += 1
        payload = _loads(_payload_of(response))
    except Exception as exc:  # noqa: BLE001 — 见模块 docstring"绝不抛异常"
        logger.warning("link：候选推理失败（%s）：%s: %s",
                       node_id, type(exc).__name__, str(exc)[:160])
        report.skipped = "model_failed"
        return report

    parsed = parse(payload, choices, history, material)
    report.dropped_candidates = parsed["dropped_candidates"]
    report.dropped_evidence = parsed["dropped_evidence"]
    if not parsed["candidates"]:
        report.skipped = "no_candidates"
        return report

    payload_out = {
        "what": parsed["what"],
        "line": parsed["line"],
        "candidates": parsed["candidates"],
        "suggestion_kind": SuggestionKind.link.value,
        # 两样"依据从哪来"的清单也带出去：界面要说清这条建议看没看过历史与材料，
        # 而 `dropped_*` 是提示词调优的唯一读数。
        "history": [item["ref"] for item in history],
        "material": [item["ref"] for item in material],
        "dropped_candidates": parsed["dropped_candidates"],
        "dropped_evidence": parsed["dropped_evidence"],
        "stage": stage,
    }
    first = parsed["candidates"][0]
    label = node.label.strip()
    current = next((choice for choice in choices if choice.current), None)
    # 两种起点的说法不同，不能硬拼一句：节点可能**还没挂**（`place` 那条路，实验与
    # "加之前先建议"都走它），那时"现在挂在「还没挂」下面"是一句读不通的话。
    if current is None:
        message = (f"「{label}」还没挂：可以挂到「{first['label']}」下面——"
                   f"{first['why'] or '与它讲的是同一支'}")
    else:
        message = (f"「{label}」现在挂在「{current.label}」下面，也可以挂到"
                   f"「{first['label']}」：{first['why'] or '与它讲的是同一支'}")
    report.hints.append(payload_out)
    report.suggestions.append(Suggestion(
        kind=SuggestionKind.link,
        node_id=node_id,
        message=message,
        # `Suggestion` 是 `extra="forbid"` 且是 `/suggest` 的响应模型，加字段就要
        # 重导 `openapi.json`；`evidence` 是契约里唯一的自由载荷，形状自由的
        # dict 塞这里与 `agent/tools.py` 往 `data` 里塞三态是同一条取舍。
        evidence=json.dumps(payload_out, ensure_ascii=False),
    ))
    return report


async def place(content: DiagramContent, node: Node, **options: Any) -> LinkReport:
    """`node` **不在** `content` 上（还被没挂 / 被藏起来）：问它该挂在哪。

    与 `suggest` 同一套推理，差别只有一处：没有"当前父"可置顶，所以模型必须从
    整张图里挑一个。`scripts/measure_link_placement.py` 的"藏节点再猜"走的就是
    这条路——把真图上的一个节点摘掉，看它能不能被猜回原来的地方。

    它也是"还没加就先建议"的入口（`suggest` 要求节点已经在树上）。
    """
    return await suggest(content, node.id, target=node, **options)


def _current_parent_label(choices: Sequence[_ParentChoice]) -> str:
    """只给测试与调试读：`suggest` 自己按"有没有当前父"分别组织措辞。"""
    current = next((choice for choice in choices if choice.current), None)
    return current.label if current else '还没挂'


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "ENV",
    "LinkReport",
    "MATCH_THRESHOLD",
    "MAX_CANDIDATES",
    "MAX_HISTORY_MATCHES",
    "MAX_MATERIAL_HITS",
    "MAX_PARENT_CHOICES",
    "SYSTEM",
    "SYSTEM_DECISION_ONLY",
    "allowed_evidence",
    "enabled",
    "history_placements",
    "material_hits",
    "messages",
    "parent_choices",
    "parse",
    "place",
    "suggest",
]
