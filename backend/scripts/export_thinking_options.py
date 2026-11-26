"""把后端那一张思考方式表导出成前端可以直接 `import` 的 JSON。

## 为什么是生成物，不是接口

那个下拉就在生成按钮旁边（`ChatDock.vue` 的 `#thinking-model`），**首屏就要可用**。
走 `GET /api/generation/thinking-options` 的话只有两种收场：下拉在响应到达之前是空的，
或者前端再抄一份兜底——后者就是这轮要消灭的"两份定义源"，只是从静态副本变成动态副本。
所以值只能跟包一起发。

这个仓库已经有同形状的先例：`openapi.json` 由 `scripts/export_openapi.py` 生成、
由 `tests/test_openapi_snapshot.py` 守卫，前端再据它生成 `schema.d.ts`。这里照抄那条路，
生成的产物直接落进前端源码树：

    backend/app/generation/service.py::THINKING_MODELS     ← 唯一的那一份
        ↓ 本脚本
    frontend/src/api/thinking-options.json                  ← 提交、可 review 的产物
        ↓ import
    frontend/src/stores/options.ts::THINKING_MODEL_OPTIONS  ← 不含任何字面量

改完表忘了重跑本脚本时，`tests/test_thinking_options.py` 会红——那句红是这一整套的
全部价值：**两份定义源不一致必须是一个红灯，而不是一次沉默**。

用法（在 `backend` 目录下）：

    python scripts/export_thinking_options.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.generation.service import thinking_options  # noqa: E402

TARGET = BACKEND_DIR.parent / "frontend" / "src" / "api" / "thinking-options.json"


def render() -> str:
    """序列化成稳定字符串。

    不做 `sort_keys`，理由与 `export_openapi.py` 相同：顺序是契约的一部分——
    `default` 必须排第一，它是前后端共同的默认选中项，且 `THINKING_MODELS` 的
    dict 顺序就是界面的展示顺序。排序会把"后端调了顺序、前端没跟上"这件事抹掉。
    """
    return json.dumps(thinking_options(), ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {TARGET.relative_to(BACKEND_DIR.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
