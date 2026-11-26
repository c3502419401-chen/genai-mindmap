"""把后端那张图型名表导出成前端可以直接 `import` 的 JSON。

## 为什么是生成物，不是接口

与 `export_thinking_options.py` 同一条理由（那一份论证了整个方案，这里不重复）：
图型下拉就在生成按钮旁边，**首屏就要可用**。走接口只有两种收场，空着或者再抄一份
兜底——后者正是要消灭的东西。

## 这里导出的是"名字"，不是"选项"

后端 `contracts/mindmap.py` 里有两份东西，一份是值域（`DIAGRAM_TYPES` /
`DiagramKind`），一份是中文名（`DIAGRAM_LABELS`）。**只导出中文名**，因为：

- 值域已经有一条自己的通路，而且比 JSON 更早生效——它走
  `openapi.json → schema.d.ts → api/types.ts::DiagramType`，`npm run gen:api`
  出产物、CI 里 `git diff --exit-code` 保新鲜。再导一遍 JSON 等于同一件事有两条
  真源，改了一处忘了另一处时，红的会是"两个生成物不一致"，而不是"契约改了"。
- 中文名**进不了 OpenAPI**：`DIAGRAM_LABELS` 是普通 dict，不是 pydantic 模型的
  字段，`app.openapi()` 里根本没有它。它唯一的生成期出口就是本脚本。

## hint 为什么**不**在这里

`contracts/mindmap.py:154-157` 的注释已经把这件事判过了，原文是"**只放中文名，
不放说明**——说明属于界面"。`hint` 描述的是"选了它，界面上会发生什么"，只有写
界面的人知道该说多少；把它搬到后端表里，会让契约层持有一段与生成逻辑无关的中文，
而且照旧要靠人写——换不到"加图型零改动"。`hint` 留在
`frontend/src/stores/options.ts`，那里的 `Record<DiagramType, {hint}>` 保证少一条
就是编译错误。

## 链路

    backend/app/contracts/mindmap.py::DIAGRAM_LABELS     ← 唯一的那一份
        ↓ 本脚本
    frontend/src/api/diagram-labels.json                  ← 提交、可 review 的产物
        ↓ import
    frontend/src/stores/options.ts::DIAGRAM_TYPE_OPTIONS  ← 中文名不含任何字面量

改完表忘了重跑本脚本时，`tests/test_diagram_options.py` 会红。

用法（在 `backend` 目录下）：

    python scripts/export_diagram_labels.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.contracts.mindmap import DIAGRAM_LABELS  # noqa: E402

TARGET = BACKEND_DIR.parent / "frontend" / "src" / "api" / "diagram-labels.json"


def render() -> str:
    """序列化成稳定字符串。

    不做 `sort_keys`：顺序是契约的一部分。`DIAGRAM_LABELS` 的键序就是下拉里那几项
    的展示顺序（`tree` 最常用，排第一），排序会把"后端调了顺序、前端没跟上"抹掉。
    """
    return json.dumps(DIAGRAM_LABELS, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {TARGET.relative_to(BACKEND_DIR.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
