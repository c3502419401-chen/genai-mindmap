"""从分片生成的展开响应里，挑出「已经写完」的子项。

深层生成的展开响应形如 `{"children":[{"label":"...","expandable":false}]}`，
**没有 id**，所以 `generation/stream_partial.collect_new_nodes` 那套用不上：
它靠节点 id 判断一个节点是不是写完了，而这里一个 id 都没有——父子关系由调用方
（这一路在展开哪个 slot）决定，不在 JSON 里。

做法是扫描已收到的文本，跟踪字符串、转义与括号深度，把 `children` 数组里**已经
闭合**的对象逐个交给 json.loads。半截对象一律不碰：补齐括号也能"解析成功"，
但那样推出去的标题随后还会变长，前端会看到文字被改写。

`parse_partial` / `collect_new_nodes` 一行都不动：它们服务于普通生成路径，
那边正跑着，改它会连带改变那条路径的行为。

重扫而不是维护跨批次的扫描状态：一次展开的正文只有几百到几千字符，按段重扫
比维护状态机便宜，也不会因为某一段异常留下一份半截状态。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.contracts.mindmap import MAX_NODE_LABEL_CHARS

# 预览里 label 的上限**就是**契约 `Node.label` 那一个数，不另存一份。
#
# 这里曾经写着 `MAX_LABEL_CHARS = 200` 加一句"与契约里 `Node.label` 同口径"——
# 同口径的承诺没人执行：把契约放宽到 300，预览仍截在 200，于是同一条文字在预览里
# 比落库之后短一截，而没有任何东西会红。现在直接引用契约常量。
#
# 断言在 `tests/test_shared_constants.py`。

# 展开响应里装着子项的那个键。第一个出现的才是这次要的那层；模型多给一层时
# 里层也会有一个 `children`，那不是同一层。
_CHILDREN_KEY = "children"


@dataclass(frozen=True)
class StreamItem:
    """一个已经写完的子项。

    `expandable` 只在模型真的写了布尔值时才不是 None：写成 `"false"` 字符串、
    写成 0、或者还没写到，都算「没写」。缺省（按可展开兜底）与「明确说不能再拆」
    是两件事，预览里必须分得开。

    `children` 是模型**多给**的一层（`tools._candidate_slots` 会收下它）。
    单独报出来是为了让预览与最终树的形状一致，报的时候父节点先于子节点。
    """

    label: str
    expandable: bool | None = None
    children: tuple["StreamItem", ...] = field(default=())


def _as_item(parsed: object) -> StreamItem | None:
    """已解析的一个对象 → `StreamItem`。label 不是非空字符串的项一律丢掉。"""
    if not isinstance(parsed, dict):
        return None
    label = parsed.get("label")
    if not isinstance(label, str) or not label.strip():
        return None
    flag = parsed.get("expandable")
    nested = parsed.get("children")
    children: tuple[StreamItem, ...] = ()
    if isinstance(nested, list):
        children = tuple(
            item for item in (_as_item(entry) for entry in nested) if item is not None
        )
    return StreamItem(
        label=label.strip()[:MAX_NODE_LABEL_CHARS],
        expandable=flag if isinstance(flag, bool) else None,
        children=children,
    )


def _closed_spans(buffer: str) -> list[str]:
    """`children` 数组里已经闭合的元素原文，按出现顺序。

    只认完整闭合的 `{...}`：数组里还没写到大括号收尾的那一项不返回，下一次
    喂进更多内容时自然会被算进来。
    """
    spans: list[str] = []
    depth = 0
    in_string = False
    escaped = False
    token_start = -1           # 当前字符串 token 的起始引号下标
    pending = ""               # "" | "key"（刚读完 "children"）| "colon"（读到冒号）
    array_depth: int | None = None   # 目标数组所在的括号深度
    item_start: int | None = None    # 当前元素对象的起始下标

    for index, char in enumerate(buffer):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                if array_depth is None and buffer[token_start + 1:index] == _CHILDREN_KEY:
                    pending = "key"
            continue
        if char == '"':
            in_string = True
            token_start = index
            pending = ""
            continue
        if pending == "key":
            # 键与值之间只可能有空白和冒号；出现别的字符说明这不是一个键
            if char == ":":
                pending = "colon"
                continue
            if char.isspace():
                continue
            pending = ""
        elif pending == "colon":
            if char.isspace():
                continue
            pending = ""
            if char == "[":
                # 这个 `[` 就是目标数组的起点，交给下面的括号处理
                array_depth = depth + 1
        if char in "{[":
            depth += 1
            if array_depth is not None and depth == array_depth + 1 and char == "{":
                item_start = index
            continue
        if char in "}]":
            if (array_depth is not None and item_start is not None
                    and char == "}" and depth == array_depth + 1):
                spans.append(buffer[item_start:index + 1])
                item_start = None
            depth -= 1
            if array_depth is not None and depth < array_depth:
                # 目标数组已经闭合。后面即使再出现 `children`（例如模型在别的
                # 地方又写了一遍推荐结构），也不是这次要的那一层，停手。
                break
    return spans


def closed_items(buffer: str) -> list[StreamItem]:
    """`buffer` 里已经完整闭合的子项，按出现顺序。

    已闭合的部分是稳定的前缀：后面再喂进新内容，前面返回过的项不会变，
    所以调用方可以按「上次报了几个」的偏移继续往后报。
    """
    items: list[StreamItem] = []
    for span in _closed_spans(buffer):
        try:
            parsed = json.loads(span)
        except json.JSONDecodeError:
            # 括号闭合但内容不是合法 JSON（模型写了坏转义）：跳过这一项，
            # 后面还有别的项等着，不该为它把整段扫描作废。
            continue
        item = _as_item(parsed)
        if item is not None:
            items.append(item)
    return items


__all__ = ["MAX_NODE_LABEL_CHARS", "StreamItem", "closed_items"]
