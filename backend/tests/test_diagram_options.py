"""图型的"跨语言两端"：后端那张名表 ↔ 前端那份生成物 ↔ OpenAPI 的 union。

## 这条用例拦的是哪一种失败

`docs/WIRING-GAPS.md` 第十一节留的原话是：`DIAGRAM_TYPE_OPTIONS` 的值域仍靠手写的
`api/types.ts::DiagramType`，与 `schema.d.ts` 的 union 之间没有任何用例连着。加一个
图型同样不会红。

这一轮把三样东西各接了一条路，每条路都得有人守：

| 东西 | 唯一真源 | 生成物 | 守卫 |
|---|---|---|---|
| 值域 | `DiagramKind` 的 Literal | `openapi.json` → `schema.d.ts` | 本文件的 `test_schema_union_matches_diagram_kind` + CI 的 `npm run gen:api` diff |
| 中文名 | `DIAGRAM_LABELS` | `frontend/src/api/diagram-labels.json` | 本文件前两条 |
| `hint` | 前端 `stores/options.ts` | 无 | `frontend/tests/diagram-options-source.spec.ts` + `npm run type-check` |

## 为什么"值域"那条要在这里写，而不是在前端

因为要比对的两端**只有后端看得见全**：`DiagramKind` 是 Python 的 `Literal`，
`schema.d.ts` 是文本。前端的 vitest 只能读文本、拼不出 `DiagramKind`；Python 能
`get_args()` 拿到真值域，再读前端文件对账。跨语言的守卫放在能同时看见两侧的那一端。

它红了的意思是"OpenAPI 快照/类型没重跑"，而不是"这段代码写错了"——所以在断言里
把该跑的两条命令写全。
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import get_args

from app.contracts.mindmap import DIAGRAM_LABELS, DIAGRAM_TYPES, DiagramKind

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
SCRIPT = BACKEND_DIR / "scripts" / "export_diagram_labels.py"
EXPORT = REPO_DIR / "frontend" / "src" / "api" / "diagram-labels.json"
SCHEMA = REPO_DIR / "frontend" / "src" / "api" / "schema.d.ts"
STORE = REPO_DIR / "frontend" / "src" / "stores" / "options.ts"

REGEN = (
    "重新生成：\n"
    "    cd backend && python scripts/export_diagram_labels.py\n"
    "    cd backend && python scripts/export_openapi.py\n"
    "    cd frontend && npm run gen:api"
)


def _load_export_script():
    spec = importlib.util.spec_from_file_location("export_diagram_labels", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema_diagram_types() -> list[str]:
    """`schema.d.ts` 里请求侧那条 `diagram_type?: ("tree" | …) | null`。"""
    match = re.search(r'diagram_type\?:\s*\(([^)]*)\)\s*\|\s*null', SCHEMA.read_text(encoding="utf-8"))
    assert match, "schema.d.ts 里找不到 AgentRunRequest.diagram_type 的 union"
    return [item.strip().strip('"') for item in match.group(1).split("|")]


def test_table_has_one_label_per_diagram_kind():
    """后端内部：名表的键必须**恰好**是那三个图型，且顺序一致。

    顺序不是洁癖：`DIAGRAM_LABELS` 的键序就是下拉的展示顺序（生成物照抄它，
    `DIAGRAM_TYPE_COPY` 也照它写）。`tree` 排第一是因为它最常用，而"不动下拉就
    生成"发的就是第一项。
    """
    assert DIAGRAM_TYPES == get_args(DiagramKind), (
        "`DIAGRAM_TYPES` 与 `DiagramKind` 的 Literal 不同步——它们必须逐字同序，"
        "否则请求层收下了一个契约层不认识的图型。"
    )
    assert tuple(DIAGRAM_LABELS) == DIAGRAM_TYPES, (
        "`DIAGRAM_LABELS` 的键与 `DIAGRAM_TYPES` 不一致：少一个键，导出文件头里那张图"
        "会退回显示 `fishbone` 这种裸值。"
    )
    assert all(label.strip() for label in DIAGRAM_LABELS.values())


def test_exported_snapshot_is_up_to_date():
    """生成物与后端名表一致。忘了重跑导出脚本，就是这里红。"""
    module = _load_export_script()
    assert EXPORT.read_text(encoding="utf-8") == module.render(), f"diagram-labels.json 过期了。{REGEN}"


def test_export_has_one_entry_per_diagram_kind():
    """生成物是"全部"，不是"一部分"——少一条就等于那个选项从下拉里消失。"""
    exported = json.loads(EXPORT.read_text(encoding="utf-8"))
    assert exported == DIAGRAM_LABELS
    assert list(exported) == list(DIAGRAM_TYPES)


def test_schema_union_matches_diagram_kind():
    """跨语言：`schema.d.ts` 的 union 必须等于 `DiagramKind`。

    这一条把"后端加了图型、前端那份生成物没跟上"变成红灯。它红了通常是没跑命令，
    而不是代码写错了——所以把三条命令写在提示里。
    """
    assert _schema_diagram_types() == list(get_args(DiagramKind)), (
        "frontend/src/api/schema.d.ts 的 diagram_type union 与后端 DiagramKind 不同步。"
        f"往 `contracts/mindmap.py` 加了一种图型，却只改了一侧。{REGEN}"
    )
