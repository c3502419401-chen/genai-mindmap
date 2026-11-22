"""跨图参照（F16 的第四层判据）：在**历史图库**里找同类节点，对比结构与篇幅。

位置：`optimization.service.suggest()` 的最后一步，**默认关**
（`MINDMAP_OPTIMIZATION_CROSS_MAP`）。关闭时连 `numpy` 与数据库都不碰。

=====================================================================
它回答的问题与前几层不同
=====================================================================
    check()      这张图**内部**有没有结构问题      （词面判据，零成本）
    模型层       同一父节点下有没有语义重复        （一次 LLM 调用）
    本模块       同类图里，这个节点别人怎么写的    （一次检索 + 一次矩阵乘）

前两层判的是"你的图自相矛盾/自相重复"。这一层判不了那两件事，判的是**缺省**：
"这个节点在别的图里被展开了三层，你这里只有一行"、"别人写了 140 字，你没写"。
缺省不是错误，所以产出的建议一律是 `info` 性质、必须由用户判断。

=====================================================================
检索规模：这一步**没有**用向量数据库，是一个刻意的决定
=====================================================================
实测（`scripts/measure_cross_map.py`，本机 2C2G）：

    · 全库 3652 个节点 × 1024 维，numpy 一次矩阵乘 = 15–60 ms
    · Milvus Lite（同规模、FLAT/COSINE）单次 top-10 = 179 ms，常驻内存 +119 MB
    · 一次 `suggest()` 现在的基线是 0.5 ms（规则层），模型层是 1.4 s

也就是说：**检索不是这里的瓶颈**。引入 Milvus 会让这一步慢 10 倍并多吃 100 MB，
而它省下的那部分（暴力扫 → ANN）在这个规模上不存在。见
`docs/OPTIMIZATION-CROSS-MAP.md` 的实测表和"什么时候才需要换掉"。

真正的成本是 **embedding**：每个节点一次远程调用（实测 0.11 s/条）。所以：

    · 节点向量按文本哈希缓存进 `knowledge.vector_cache`（复用，不新增表）
    · 同步路径上只对**这张图**的节点补向量，且封顶 `MAX_QUERY_NODES`
    · 历史节点**只读缓存**，不现算——历史库是别人几十次生成攒下来的，
      在 `/suggest` 这个同步请求里现算它等于让一个按钮点下去等一分钟

代价是**冷启动没有输出**：库里一张图都没保存过时，历史向量为空，这一层返回空
列表。这是设计选择不是缺陷——预热入口是 `prime_history()`，保存路径或一次性脚本
调用它之后，后面的每次体检都是缓存命中。

=====================================================================
为什么排除同名图
=====================================================================
实测里最大的噪声源不是"不相似的节点"，而是**同一主题的多个版本互相参照**：
全库 238 条候选里有 148 条（62%）的参照物与当前图**标题完全相同**，那是"你上次
写得更细"，不是"别人的写法"。同一张图每次保存都会在历史里多一份，同主题生成
三次就形成 3 倍的自我参照，而用户看到的是一堆"那张图在这里写了 152 字"。

所以历史库排除**标题相同**的图。代价是"上次的版本"不能当参照，收益是这条建议
真的指向另一个主题的图。

=====================================================================
建议的落点
=====================================================================
`SuggestionKind.cross_map`，与 `expand` / `add_detail` 分开：动作指引可能一样
（都是"再展开一层"或"补说明"），但**依据来自别的图**，所以用户要能单独关掉它，
界面也要能说清出处。`evidence` 的格式是 `"<map_id>#<节点标签>"`——跳转入口需要的
两个东西都在里面，而契约里 `evidence` 本来就是"相似节点/原文片段"。
"""
from __future__ import annotations

import logging
import os

from app.contracts.diagnostic import Suggestion, SuggestionKind
from app.contracts.mindmap import MindMapContent, Node

logger = logging.getLogger(__name__)

# 唯一的成本开关，缺省关：与模型层同一个理由——这一层要网络与数据库，
# 而"库里还没有历史图"在小规模部署上是常态，默认开只会让每次体检多等几秒。
ENV = "MINDMAP_OPTIMIZATION_CROSS_MAP"

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}

# 余弦阈值。这个数来自实测而不是拍的：全库 253912 个节点对的相似度 p99 = 1.000、
# p95 = 0.693，也就是说**同一张图内部的任意两个节点**在 0.70 以下，0.80 以上
# 基本就是"同一个概念的不同说法"。再低的阈值会把「偏差-方差分解」与
# 「模型复杂度权衡」这类相邻概念拉进来（0.75 附近大量出现）。
MATCH_THRESHOLD = 0.80

# 每个节点取几个历史近邻。取多个不是为了"多条建议"，是为了**跨图投票**：
# 单张图的差异可能是那次生成的偶然，3 张图都更长才说明这是共性。
TOP_K = 3

# 一次体检最多给这张图的几个节点找参照。与 `MAX_LLM_LABELS` 同口径：
# 这是同步请求，节点越多 embedding 越多，而"这张图有 300 个节点"那种情况
# 本来也该先分片体检。_PROBE 实测 40 个节点 ≈ 4 批 embedding ≈ 4.4 s。
MAX_QUERY_NODES = 40

# 历史库最多扫多少个节点。20 万 × 1024 维 float32 是 800 MB，不能全量进内存；
# 2 万 × 4 KB = 80 MB，是一个 2C2G 机器扛得住的上限，也是"几千张图"才撞到的量。
MAX_HISTORY_NODES = 20000

# 一次最多几条。这类建议的信息密度低于规则层（同一条"可以更详细"在 20 个节点上
# 说 20 遍没有意义），而前端面板只列前三条。
MAX_SUGGESTIONS = 5

# 「参照图写得更详细」的门槛：比当前多这么多字才算。40 与 `_probe` 里的判据一致：
# 少 10–20 字是措辞松紧，不值得让用户点一次。
MIN_DETAIL_GAP = 40

# 「参照图展开了而这里是叶子」的门槛：子树高度差。1 层就已经是"这里少了一级"。
MIN_HEIGHT_GAP = 1


def _enabled() -> bool:
    return str(os.getenv(ENV, "")).strip().lower() in _TRUTHY


def enabled() -> bool:
    """这一层开关的状态。稳定接口：`service.suggest()` 用它做闸门。"""
    return _enabled()


def _subtree_height(node: Node) -> int:
    """以 `node` 为根的子树的层数（叶子 = 1）。

    与 `optimization/service.py::_subtree_height` 同一份判据、同一份实现语义。
    不在那边 import 是因为 `service` 要 import 本模块（延迟导入的那一步），
    而这条只在跨图对比里用一次，重复几行比引入一个循环导入的风险划算。

    **历史库那一侧不用这个函数**：它对每个节点各算一次，全库 1075 个节点是
    O(n²)（一张 561 节点的图就能把一次体检拖到 3 秒），见 `_heights`。
    """
    stack, best = [(node, 1)], 1
    while stack:
        current, depth = stack.pop()
        best = max(best, depth)
        stack.extend((child, depth + 1) for child in current.children)
    return best


def _heights(roots: list[Node]) -> dict[str, int]:
    """一次后序遍历算出整批节点的高度（`{节点 id: 层数}`）。

    与 `_subtree_height` 的区别只在复杂度：那是"问一个节点多高"，这是"问所有
    节点多高"。历史库要的是后者——按前者做，一张 561 节点的图会让每次 `/suggest`
    多花 2–3 秒（实测 2401 ms/图 → 修完 168 ms/图）。
    """
    heights: dict[str, int] = {}
    stack: list[tuple[Node, bool]] = [(root, False) for root in roots]
    while stack:
        node, expanded = stack.pop()
        if expanded:
            heights[node.id] = 1 + max(
                (heights.get(child.id, 1) for child in node.children), default=0)
            continue
        stack.append((node, True))
        stack.extend((child, False) for child in node.children)
    return heights


def _body_len(node: Node) -> int:
    """节点的正文长度。`detail` 是生成的、`note` 是用户写的，两者都算——
    只认 `detail` 会把"用户已经补过备注"报成空白（与 `service._has_body` 同口径）。"""
    return len((node.detail or node.note or "").strip())


#: 历史节点的进程内缓存：`{"key": 库指纹, "items": [...]}`。
#:
#: 不加这层，每次体检都要把库里**每张图的完整 JSON** 读出来再走一遍 pydantic
#: 校验（实测 58 张图、3652 节点时单次 `_history()` 约 130 ms，占"缓存热的
#: 一次体检"的九成）。失效判据用 `repository.maps_fingerprint()`——它本来就是
#: 为"每次开抽屉都要算，而算它得解析全库"这个场景写的（库位置 + 条数 + 最后
#: 更新时间），任何保存都会改动其中一项。
_HISTORY: dict[str, object] = {"key": None, "items": []}


def _history(diagram_type: str) -> list[tuple[str, str, Node, int]]:
    """历史库里可以当参照的节点：`(map_id, 标题, 节点, 子树高度)`。

    只取**已保存**的树图：`runs.candidate` 是跑到一半的检查点与未保存的草稿，
    把它当"别人的写法"会让参照物全是这台机器上刚才那次失败尝试。
    """
    from app.mindmaps import repository

    try:
        key = repository.maps_fingerprint()
    except Exception:  # noqa: BLE001 - 指纹拿不到就不走缓存，不影响正确性
        key = None
    if key is not None and _HISTORY["key"] == key:
        return list(_HISTORY["items"])  # type: ignore[arg-type]

    items: list[tuple[str, str, Node, int]] = []
    try:
        records = repository.list_maps(tolerant=True)
    except Exception:  # noqa: BLE001 - 历史库读不出来只是"这一层没有依据"
        logger.warning("跨图参照：历史图库读不出来，本次跳过", exc_info=True)
        return []
    for record in records:
        content = record.content
        if not isinstance(content, MindMapContent):
            continue
        if content.diagram_type != diagram_type:
            continue
        nodes = _walk(content)
        heights = _heights([content.root])
        for node in nodes:
            if len(items) >= MAX_HISTORY_NODES:
                logger.info("跨图参照：历史节点已到上限 %d，停止取样", MAX_HISTORY_NODES)
                _HISTORY["key"], _HISTORY["items"] = key, items
                return items
            items.append((record.id, content.title, node, heights.get(node.id, 1)))
    _HISTORY["key"], _HISTORY["items"] = key, items
    return items


def _walk(content: MindMapContent) -> list[Node]:
    from app.agent import shapes

    return list(shapes.iter_nodes(content))


def prime_history() -> dict:
    """把历史图库里还没缓存的节点向量算出来，写进缓存。返回统计。

    **不在 `suggest()` 里自动跑**：那会把一次同步请求变成"等三分钟"。
    调用方是保存路径的后台任务或一次性脚本（`scripts/measure_cross_map.py`）。
    失败一律静默——预热不成功只意味着下一次体检少几条建议。
    """
    from app.knowledge import vector_cache

    items = _history("tree")
    if not items:
        return {"nodes": 0, "hit": 0, "embedded": 0}
    model = _model()
    labels = [node.label.strip() for _, _, node, _ in items]
    hashes = [vector_cache.text_hash(label) for label in labels]
    cached = vector_cache.get_vectors(hashes, model)
    pending: dict[str, str] = {}
    for label, value in zip(labels, hashes):
        if value not in cached and label:
            pending[value] = label
    written = 0
    if pending:
        from app.knowledge import embedding

        keys = list(pending)
        try:
            vectors = embedding.embed_texts([pending[k] for k in keys])
        except embedding.EmbeddingError:
            logger.warning("跨图参照：历史向量预热失败，下一次体检只剩已有缓存", exc_info=True)
            vectors = []
        pairs = [(k, v) for k, v in zip(keys, vectors) if v]
        if pairs:
            written = vector_cache.put_vectors(pairs, model)
    return {"nodes": len(items), "hit": len(cached), "embedded": written}


def _model() -> str:
    from app.knowledge import embedding

    return embedding.model_name()


def _suggestion_message(
    node: Node, matches: list[tuple[str, str, Node, int, float]], *, what: str,
) -> str:
    """一句话说清楚：这里和外面对比出了什么差、依据是哪几张图。"""
    titles = list(dict.fromkeys(title for _, title, _, _, _ in matches))[:3]
    source = "、".join(f"「{title}」" for title in titles)
    count = len(titles)
    if what == "expand":
        theirs = max(height for _, _, _, height, _ in matches)
        return (f"同类图里这个节点被展开了：{source} 里与「{node.label}」相近的节点"
                f"（共 {count} 张图命中）往下还有 {theirs - 1} 层，这里是个叶子")
    longest = max(_body_len(item) for _, _, item, _, _ in matches)
    return (f"同类图里这个节点写得更详细：{source} 里与「{node.label}」相近的节点"
            f"最多写了 {longest} 字，这里是 {_body_len(node)} 字（共 {count} 张图命中）")


def suggestions(
    content: MindMapContent, *, map_id: str | None = None, enabled: bool | None = None,
) -> list[Suggestion]:
    """跨图参照建议。关闭、未配置、历史为空、向量取不到——一律返回空列表。

    `enabled`：`None` 走环境变量；`True` / `False` 显式开关。显式值必须由
    **调用方**传进来而不是在本模块里各判一次：`service.suggest(use_cross_map=True)`
    的语义是"这一次要跑"，如果这一层自己再读一遍环境变量，那个参数就废了。

    **绝不抛异常**：与模型层同一条硬约束。体检是附加信息，数据库或向量服务出问题
    不该让 `/suggest` 变成 500。
    """
    if not (enabled if enabled is not None else _enabled()):
        return []

    from app.agent import shapes

    try:
        history = [
            item for item in _history(content.diagram_type)
            # 同名图不当参照（理由见模块 docstring）；当前这张图自己也不当。
            if item[1] != content.title and item[0] != map_id
        ]
        if not history:
            return []

        nodes = [node for node in shapes.iter_nodes(content) if node.label.strip()]
        if not nodes:
            return []
        nodes = nodes[:MAX_QUERY_NODES]

        return _compare(nodes, history, map_id=map_id)
    except Exception:  # noqa: BLE001 - 这一层挂掉只等于"没有跨图依据"
        logger.warning("跨图参照失败，本次跳过这一类建议", exc_info=True)
        return []


def _compare(
    nodes: list[Node], history: list[tuple[str, str, Node, int]], *, map_id: str | None,
) -> list[Suggestion]:
    import numpy as np

    from app.knowledge import embedding, vector_cache

    model = _model()
    query_labels = [node.label.strip() for node in nodes]
    history_labels = [node.label.strip() for _, _, node, _ in history]

    # 两批都先查缓存：历史那批**只用缓存**，查不到就丢掉（现算它是同步请求里
    # 最贵的一步，而历史库恰恰是最不可能有缓存的那一侧）。
    query_hashes = [vector_cache.text_hash(label) for label in query_labels]
    history_hashes = [vector_cache.text_hash(label) for label in history_labels]
    cached = vector_cache.get_vectors(query_hashes + history_hashes, model)

    missing = [label for label, key in zip(query_labels, query_hashes)
               if key not in cached and label]
    if missing:
        try:
            fresh = embedding.embed_texts(list(dict.fromkeys(missing)))
        except embedding.EmbeddingError:
            logger.warning("跨图参照：这张图的节点向量取不到，本次跳过", exc_info=True)
            fresh = []
        pairs = [(vector_cache.text_hash(text), vector)
                 for text, vector in zip(dict.fromkeys(missing), fresh) if vector]
        if pairs:
            vector_cache.put_vectors(pairs, model)
            cached.update({key: value for key, value in pairs})

    query_rows = [(index, cached[key]) for index, key in enumerate(query_hashes)
                  if key in cached]
    history_rows = [index for index, key in enumerate(history_hashes) if key in cached]
    if not query_rows or not history_rows:
        return []

    queries = np.asarray([vector for _, vector in query_rows], dtype=np.float32)
    base = np.asarray([cached[history_hashes[i]] for i in history_rows], dtype=np.float32)
    sims = queries @ base.T

    take = min(TOP_K, len(history_rows))
    top = np.argpartition(-sims, take - 1, axis=1)[:, :take]

    # 先把全部候选收起来，最后统一排序、去重，再截断。
    #
    # **为什么不按遍历顺序边算边截**：一张图里经常有三个节点都匹配到同一个历史
    # 节点（「监督学习」「监督学习（标注映射）」「按学习范式划分」），边算边截的
    # 结果是前 5 条里有 3 条在说同一件事，而真正差得最远的那条反而被挤掉。
    candidates: list[tuple[int, int, Node, list, str]] = []
    for row, (node_index, _) in enumerate(query_rows):
        node = nodes[node_index]
        order = top[row][np.argsort(-sims[row][top[row]])]
        mine_height = _subtree_height(node)
        mine_body = _body_len(node)
        expand_matches: list[tuple[str, str, Node, int, float]] = []
        detail_matches: list[tuple[str, str, Node, int, float]] = []
        for position in order:
            score = float(sims[row][position])
            if score < MATCH_THRESHOLD:
                continue
            history_index = history_rows[position]
            entry = history[history_index]
            height = entry[3]
            if not node.children and height - mine_height >= MIN_HEIGHT_GAP:
                expand_matches.append((*entry, score))
            if _body_len(entry[2]) - mine_body >= MIN_DETAIL_GAP:
                detail_matches.append((*entry, score))
        # 「该展开」优先于「该补说明」：同一句话里两个动作会让用户不知道该点哪个。
        if expand_matches:
            gap = max(height for _, _, _, height, _ in expand_matches) - mine_height
            candidates.append((2, gap, node, expand_matches, "expand"))
        elif detail_matches:
            gap = max(_body_len(item) for _, _, item, _, _ in detail_matches) - mine_body
            candidates.append((1, gap, node, detail_matches, "detail"))

    # 结构差异（缺一层）比篇幅差异（少写了几十个字）更值得先说，所以先按类型
    # 再按差距排序。`-` 是因为这是降序。
    candidates.sort(key=lambda item: (-item[0], -item[1]))

    suggestions: list[Suggestion] = []
    used: set[str] = set()          # 已经当过参照物的历史节点（`map_id#label`）
    for _, _, node, matches, what in candidates:
        fresh = [m for m in matches if f"{m[0]}#{m[2].label.strip()}" not in used]
        if not fresh:
            continue
        used.update(f"{m[0]}#{m[2].label.strip()}" for m in fresh)
        suggestions.append(_build(node, fresh, SuggestionKind.cross_map, what=what))
        if len(suggestions) >= MAX_SUGGESTIONS:
            break
    return suggestions


def _build(
    node: Node, matches: list[tuple[str, str, Node, int, float]], kind: SuggestionKind,
    *, what: str,
) -> Suggestion:
    """一条建议。`evidence` 是 `"<map_id>#<节点标签>"`：跳转入口需要的两样东西。"""
    first = matches[0]
    return Suggestion(
        kind=kind,
        node_id=node.id,
        message=_suggestion_message(node, matches, what=what),
        evidence=f"{first[0]}#{first[2].label.strip()}"[:200],
    )
