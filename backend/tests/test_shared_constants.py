"""四处"同一个数写了两遍"的对账：改一处、另一处不跟，必须红。

## 这条文件拦的是哪一种失败

不是"值不相等"——四处现在都相等，跑一遍全绿也说明不了什么。它拦的是**同步机制
消失**：那四处原本各自写一个数，靠注释承诺"与契约一致"。把契约的深度从 8 改成 9，
优化层仍会在第 8 层说"到顶了"，而图还能合法加一层；把引用上限从 300 改成 320，
检索子块仍在 300 切，那条命中交不出偏移。**两种错都不会红，只是静默地说错话。**

所以每组断言三件事，缺一条这组就不算钉住：

  1. **值同源**：两侧相等。
  2. **副本不许回来**：那个模块顶层不许再出现一个"数字字面量赋值"。用 `ast` 读
     源码，而不是正则——本次改动的注释里就写着"这里曾经写着 `MAX_DEPTH = 8`"，
     正则会被自己的说明文字绊倒。第 2 条是这一整套的重点：第 1 条在有人把数字
     抄回去、且**恰好抄成同一个值**时是绿的，第 2 条会红。
  3. **口径真的对得上**：不只比数，还比一次行为。深度那处比的是"第 8 层报、
     第 9 层存不进"，引用那处比的是"契约字段真的用这个数拒，多一个字就 422"。

第 3 条专治一种假修复：把 `LEAF_MAX_CHARS` 换成 `SOURCE_REF_QUOTE_MAX_CHARS`，
却忘了把 `Field(max_length=300)` 也换过去——那时两侧相等、副本没回来，而常量
根本没接到那个字段上。值相等不等于接上了。

## 第四处是跨语言的

`frontend/src/mindmap/edits.ts::GENERATED_LABEL_HINT` 是 TypeScript，没有 import
关系。这里照 `test_thinking_options.py` 的路子：后端用例读前端源码，正则取数，
断言相等。改后端忘改前端 → 这里红；改前端忘改后端 → 也是这里红。
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError

from app.agent import stream_items
from app.contracts.mindmap import (
    GENERATED_LABEL_MAX_CHARS,
    MAX_NODE_LABEL_CHARS,
    MAX_TREE_DEPTH,
    MindMapContent,
)
from app.contracts.source import SOURCE_REF_QUOTE_MAX_CHARS, SourceRef
from app.knowledge import service as knowledge_service
from app.knowledge.service import LEAF_MAX_CHARS
from app.optimization import service as optimization_service
from app.optimization.service import check

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
FRONTEND_EDITS = REPO_DIR / "frontend" / "src" / "mindmap" / "edits.ts"


# ---------------------------------------------------------------- 公用探针


def _literal_assignments(module: ModuleType) -> dict[str, int]:
    """模块**顶层**所有"赋值为整数常量"的名字 → 值。

    只看 `X = <int>` / `X: int = <int>` 两种形状。注释、字符串、函数体里的赋值
    都不算——这一条要问的正是"这个模块有没有自己存过一份数字"，而注释里写一句
    "这里曾经是 8"不是存货。

    用 `ast` 而不是正则：本次改动的注释里就有 `MAX_DEPTH = 8` 这串字，正则会把
    说明文字当成代码。
    """
    tree = ast.parse(inspect.getsource(module))
    found: dict[str, int] = {}
    for statement in tree.body:
        value: ast.expr | None = None
        names: list[str] = []
        if isinstance(statement, ast.Assign):
            value = statement.value
            names = [t.id for t in statement.targets if isinstance(t, ast.Name)]
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            value = statement.value
            if isinstance(statement.target, ast.Name):
                names = [statement.target.id]
        if (
            len(names) == 1
            and isinstance(value, ast.Constant)
            and isinstance(value.value, int)
            and not isinstance(value.value, bool)
        ):
            found[names[0]] = value.value
    return found


def _chain(depth: int) -> MindMapContent:
    """一条 `depth` 层的链，root 算第 1 层（与 `_iter_nodes` 同口径）。"""
    node: dict = {"id": f"n{depth}", "label": f"第{depth}层", "children": []}
    for level in range(depth - 1, 0, -1):
        node = {"id": f"n{level}", "label": f"第{level}层", "children": [node]}
    return MindMapContent.model_validate({"title": "图", "root": node})


# ---------------------------------------------------------------- ① 深度


def test_optimization_depth_is_the_contract_constant():
    """`optimization` 的深度上限就是 `MAX_TREE_DEPTH`，不另存一份。

    **为什么两者应该相等**（这一条不是"看起来像"）：`_iter_nodes` 的 `depth`
    从 1 起、根是第 1 层，契约的 `MAX_TREE_DEPTH` 也是含根的层数，所以
    "第 8 层是最后一层"在两侧同义。这里没有"优化层故意更严"的余地——它报的是
    `DEPTH_LIMIT`（"已到上限"），比契约更严就变成假诊断：图还能加一层，界面上
    却说到底了。
    """
    assert optimization_service.MAX_TREE_DEPTH == MAX_TREE_DEPTH
    assert "MAX_DEPTH" not in _literal_assignments(optimization_service), (
        "optimization/service.py 顶层又出现了一个数字字面量常量：深度上限必须引用"
        " `app.contracts.mindmap.MAX_TREE_DEPTH`，不许在这里存副本。"
    )


def test_optimization_depth_limit_fires_exactly_on_the_contract_boundary():
    """行为口径：最后一层报 `DEPTH_LIMIT`，再深一层是契约根本不收。

    这一条与上面那条一起才完整。只比常量时，"两侧都是 8"可能仍然是巧合；
    这里问的是**报在第几层**，且右半边问"多一层连树都造不出来"——两个边界
    落在同一层，才叫同口径。
    """
    diagnostics = [item for item in check(_chain(MAX_TREE_DEPTH))
                   if item.code == "DEPTH_LIMIT"]
    assert [item.node_id for item in diagnostics] == [f"n{MAX_TREE_DEPTH}"]

    with pytest.raises(ValidationError):
        _chain(MAX_TREE_DEPTH + 1)


# ---------------------------------------------------------------- ② 预览标题


def test_stream_item_label_limit_is_the_contract_constant():
    """展开预览截 label 的长度就是契约 `Node.label` 的上限。

    **两者应该相等**的理由写在契约那头：预览与最终树是同一段文字的两种呈现，
    预览更短会让用户以为字被吃了；预览更长则会在落库时被 `Field` 拒掉。两侧都是
    "同一条 label 的可见长度"，没有各留余地的空间。
    """
    assert stream_items.MAX_NODE_LABEL_CHARS == MAX_NODE_LABEL_CHARS
    assert "MAX_LABEL_CHARS" not in _literal_assignments(stream_items), (
        "agent/stream_items.py 顶层又出现了一个数字字面量常量：改用"
        " `MAX_NODE_LABEL_CHARS`，不要在这里存 200 的副本。"
    )


def test_stream_item_preview_clips_at_the_contract_length():
    """行为口径：预览真的按契约那个数切，且不切更短。"""
    long_label = "字" * (MAX_NODE_LABEL_CHARS + 50)
    items = stream_items.closed_items(
        '{"children":[{"label":"' + long_label + '","expandable":false}]}'
    )
    assert len(items) == 1
    assert items[0].label == "字" * MAX_NODE_LABEL_CHARS

    exact = "字" * MAX_NODE_LABEL_CHARS
    items = stream_items.closed_items(
        '{"children":[{"label":"' + exact + '","expandable":false}]}'
    )
    assert items[0].label == exact, "刚好到上限的标题不该被切"


# ---------------------------------------------------------------- ③ 引用长度


def test_leaf_chunk_ceiling_is_the_quote_limit():
    """检索子块的硬上限就是 `SourceRef.quote` 的容量。

    **两者应该相等**（不是碰巧同值）：子块整段会被当成 `evidence_quote` 回给
    模型，超过引用容量就只能不给偏移，那条命中的逐字校验随即失效。长注释在
    `knowledge/service.py::LEAF_MAX_CHARS`。
    """
    assert LEAF_MAX_CHARS == SOURCE_REF_QUOTE_MAX_CHARS
    assigned = _literal_assignments(knowledge_service)
    assert "LEAF_MAX_CHARS" not in assigned, (
        "knowledge/service.py 里 `LEAF_MAX_CHARS` 又变回了数字字面量"
        f"（当前是 {assigned.get('LEAF_MAX_CHARS')}）："
        "它必须等于 `contracts.source.SOURCE_REF_QUOTE_MAX_CHARS`。"
    )


def test_quote_field_actually_uses_the_named_limit():
    """契约字段真的接在那个常量上——不是"常量提出来了，字段还写着 300"。

    这一条是本次改动最容易做漏的一步：把内联的 `max_length=300` 提成常量名之后，
    忘了把 `Field` 那一行也换过去，两侧照样相等、测试照样绿，而常量根本没生效。
    所以这里不看常量，只问字段：正好能过，多一个字就拒。
    """
    ok = SourceRef(source_id="s1", quote="字" * SOURCE_REF_QUOTE_MAX_CHARS)
    assert len(ok.quote or "") == SOURCE_REF_QUOTE_MAX_CHARS

    with pytest.raises(ValidationError):
        SourceRef(source_id="s1", quote="字" * (SOURCE_REF_QUOTE_MAX_CHARS + 1))


# ---------------------------------------------------------------- ④ 跨语言


def test_frontend_generated_label_hint_matches_the_backend():
    """前端的 16 与后端的 `GENERATED_LABEL_MAX_CHARS` 是同一个数。

    跨语言，没有 import 关系，所以由后端来读前端那一行——`test_thinking_options.py`
    钉 `thinking-options.json` 用的就是这条路。改后端忘改前端会红在这里，
    反过来也一样。

    为什么不做成生成物（后端脚本 → json → 前端 import）：那条管线是为"首屏就要
    用的列表"建的，这里只有一个标量、只喂一句提示文案，复制整条管线是拿结构换
    一个数字。前端那句注释里写了同样的判断。
    """
    source = FRONTEND_EDITS.read_text(encoding="utf-8")
    match = re.search(
        r"^export const GENERATED_LABEL_HINT\s*=\s*(\d+)\s*;",
        source,
        re.MULTILINE,
    )
    assert match, (
        "frontend/src/mindmap/edits.ts 里找不到 `export const GENERATED_LABEL_HINT = <数字>;`。"
        "改了这个常量的形状就得同步改这条用例——但别让它消失，它是这个跨语言对账的唯一锚点。"
    )
    assert int(match.group(1)) == GENERATED_LABEL_MAX_CHARS, (
        f"前端 GENERATED_LABEL_HINT = {match.group(1)}，后端 "
        f"GENERATED_LABEL_MAX_CHARS = {GENERATED_LABEL_MAX_CHARS}："
        "两处是同一个数（提示文案在说'会被截多长'），改一处必须改另一处。"
    )
