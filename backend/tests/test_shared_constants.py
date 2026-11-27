"""同源常量的对账：改一处、另一处不跟，必须红。

前四组是"同一个数写了两遍"，第五组是"同一组中文名写了两遍"——图型中文名在
`stores/options.ts` 读生成物，而 `mindmap/shape.ts` 另有一份手抄的副本。判据一样：
不看值相不相等，看**同步机制消失会不会红**。

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

## 第五组反过来：通路已经建好，缺的是"别再抄一份"

图型中文名那头，前端**已经有生成物**（`api/diagram-labels.json`，`stores/options.ts`
在读），所以这一组守的不是"两侧相等"——相等由"同一次文件读取"自动保证——而是
"**副本不许回来**"。判据与上面 `_literal_assignments` 那条同构：值相等的那一版是绿的，
抄回去的那一版必须是红的。

## ★ 写这类守卫的第一条纪律：扫源码文本时，只扫**目标那一段**

本文件五组断言都要读源码：① ② ③ 读被测模块自己的源码（`ast` 走语法树），
④ ⑤ 读前端文件的文本。**凡是要读文本的那些，先定位目标、再只对目标那一段断言；
绝不扫全文件。**

理由不是洁癖，是这个仓库的注释密度：**注释里必然在解释这些常量本身**——本文件头顶
这段就在解释，④ ⑤ 那两条用例的 docstring 也在解释。扫全文的版本会在自己的说明上
假红，或者更糟：被改注释的人绕过。已经踩过的三个具体形态：

  · ① ③ 的注释里写着"这里曾经写着 `MAX_DEPTH = 8`"——正则会把那句说明当成代码。
    所以那两条改用 `ast`："这个模块有没有自己存过一份数字"要问语法树，不是问文本。
  · ④ 只取**一行**（行首 `export const GENERATED_LABEL_HINT = ` + 一个数字 + 行尾，
    `re.MULTILINE`），锚死在行首行尾——注释里出现同样的整行是极不自然的写法。
  · ⑤ 只取**一个函数的花括号配平段**（`_ts_function_body`），断言落在函数体里。
    那个函数的注释提到三样东西（值来自哪、判据为什么不变、守卫在哪），全文扫描会把
    这些说明当副本。

三条配套常识：

  1. **取一段文本是权宜，不是解析器**。⑤ 的花括号配平在"函数体里出现对象字面量或
     模板字符串里的 `}`"时会失真——所以它的 docstring 写明"改这个函数的形状要连同
     这条用例的假设一起复核"。真需要稳健就把那一段也换成解析（TS 侧没有现成的，
     代价要单独判）。
  2. **失败信息也在本文件里**。断言写"源码里不许出现 X"时，X 若同时出现在某条用例的
     失败文案里，扫全文的版本会连自己绊倒自己。
  3. **能被绕过的不算守卫**。只扫目标段之后，"换一种写法躲开"才是真问题——那要靠
     多条断言叠（② 里"等值 + 无副本 + 真按这个数切"三条一起才关得住）。
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
FRONTEND_SHAPE = REPO_DIR / "frontend" / "src" / "mindmap" / "shape.ts"


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


# ---------------------------------------------------------------- ⑤ 图型中文名


# 三个中文名的**字面量**形态（带引号）。注释里提到这些名字不算副本土，只有能
# 出现在 JS 表达式里的带引号字符串才算——这条正则只喂给"函数体"那一段文本。
_DIAGRAM_NAME_LITERAL = re.compile(r"""['"](鱼骨图|流程图|思维导图)['"]""")

# 生成物在前端的位置。`stores/options.ts` 用的也是这一份，两个消费点必须同一个文件。
_DIAGRAM_LABELS_IMPORT = re.compile(
    r"^import\s+diagramLabels\s+from\s+['\"]\.\./api/diagram-labels\.json['\"];",
    re.MULTILINE,
)


def _ts_function_body(source: str, name: str) -> str:
    """取 `export function <name>(…) { … }` 的整段函数体（含两侧花括号）。

    这不是 TypeScript 解析器，只是花括号配平——它够用的前提是这个函数只有一层
    语句块。改了形状（比如里面出现对象字面量或模板字符串里的 `}`）就要连同这条
    用例的假设一起复核；失败信息里会写这句。

    为什么只取一段而不是扫全文件：见文件头"★ 写这类守卫的第一条纪律"。
    """
    match = re.search(rf"export function {name}\s*\(", source)
    assert match, (
        f"frontend/src/mindmap/shape.ts 里找不到 `export function {name}(`："
        "函数改名或被合并了吗？这条用例按这个名字取函数体。"
    )
    start = source.index("{", match.end())
    depth = 0
    for index in range(start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"shape.ts 里 `{name}` 的花括号没有配平，取不到函数体。")


def test_shape_label_reads_the_generated_labels():
    """图型中文名在前端只有一个来源：生成物 `api/diagram-labels.json`。

    ## 这条拦的是哪一种失败

    同一个界面上**两处叫法不同，而且不红**。图型中文名有两个消费点：

      · 图形类型下拉（`stores/options.ts`）——读生成物 `diagramLabels[value]`；
      · 导入结果面板那句"已导入一张 ___"（`ImportMindmap.vue` → `shapeLabel()`）。

    后者原先是三个手写字面量，注释里自认"与后端 `DIAGRAM_LABELS` 同一份字面量"。
    改一个中文名（后端 → 导出脚本 → json），下拉跟着变、面板不变，两边都没有
    一条用例会红：json 与后端有 `test_diagram_options.py` 对账，而"前端又抄了一遍"
    这件事此前无人管。**两处都绿着说不同的话，就是这条要拦的东西。**

    ## 为什么入参可以直连（不是"恰好同名"）

    `shapeLabel` 的入参是 `ShapeKind = 'tree' | 'fishbone' | 'flowchart'`（`kindOf()`
    按形状判别，不读 `diagram_type` 字段——这条设计不变），而生成物的键集恰好是
    同一套 `DiagramKind`。两套键同源（都来自后端那个 Literal），所以直连是**语义
    上的同一个键**，不是两次命名撞车。顺带一个改善：`ShapeKind` 将来加第四种形状时，
    `diagramLabels[kind]` 会是一次 `npm run type-check` 错误（生成物里没这个键），
    而原来的穷尽 `return '思维导图'` 是静默返回错名字。

    ## 三条断言各管一件事

      1. **生成物被 import**：`diagramLabels` 那一行还在文件里（有人手抄后顺手把
         import 删掉，第 2 条会因为函数体干净而放行）；
      2. **函数体里真的在索引它**：`diagramLabels[kind]` 出现——防"import 了但没用"；
      3. **函数体里不许再有那三个中文字面量**：这是"副本不许回来"，与上面三组
         `_literal_assignments` 同一条思路。第 1 条在有人把字面量抄回去、同时留着
         import 时是绿的，第 3 条会红。用 `body` 而不是整份 `source` 断言，理由见
         文件头"★ 写这类守卫的第一条纪律"。
    """
    source = FRONTEND_SHAPE.read_text(encoding="utf-8")
    body = _ts_function_body(source, "shapeLabel")

    assert _DIAGRAM_LABELS_IMPORT.search(source), (
        "frontend/src/mindmap/shape.ts 没有 `import diagramLabels from "
        "'../api/diagram-labels.json';`：图型中文名必须来自生成物，"
        "与 `stores/options.ts` 同一份。"
    )
    assert "diagramLabels[kind]" in body, (
        f"shape.ts 里 `shapeLabel` 的函数体没有索引生成物：\n{body}\n"
        "它必须直接返回 `diagramLabels[kind]`（入参 shapeKind 与生成物的键同源），"
        "不许另写一份映射。"
    )
    copied = _DIAGRAM_NAME_LITERAL.search(body)
    assert copied is None, (
        f"shape.ts 的 `shapeLabel` 函数体里又出现了中文字面量 {copied.group(0)}：\n"
        f"{body}\n"
        "改回 `diagramLabels[kind]`。手抄的副本不会跟导出脚本一起变，"
        "界面上两处叫法不同也不会红——这正是这条用例存在的理由。"
    )
