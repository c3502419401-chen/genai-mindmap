"""思考方式的"两份定义源"：现在只剩一份，而且它过期会红。

## 这条用例拦的是哪一种失败

`generation.service.THINKING_MODELS` 是这张表的真源，`thinking_options()` 把它
摊成 `[{value, label}]`。此前前端另抄了一份同值同名的数组（`stores/options.ts`），
于是**后端加一种思考方式 → 下拉不变、界面不报错、用户永远选不到**。三条断言分别
钉住这条链上的三个接缝：

1. 后端内部：`THINKING_MODELS` 的键与 `AgentRunRequest.thinking_model` 的 `Literal`
   必须逐字同序。不同步的后果是"选了就 422"，而且只有点下去才知道。
2. 后端 → 前端：生成物 `frontend/src/api/thinking-options.json` 必须等于
   `thinking_options()` 的当前输出。忘了重跑导出脚本，就是这里红。
3. 前端内部：`stores/options.ts` 里**不许**再出现思考方式的字面量。这一条防的是
   "有人把副本抄回去"——那正是这次要修掉的形状，它不该有机会长回来。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import get_args

from app.agent.contracts import AgentRunRequest
from app.generation.service import THINKING_MODELS, thinking_options

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
SCRIPT = BACKEND_DIR / "scripts" / "export_thinking_options.py"
EXPORT = REPO_DIR / "frontend" / "src" / "api" / "thinking-options.json"
STORE = REPO_DIR / "frontend" / "src" / "stores" / "options.ts"


def _contract_values() -> tuple[str, ...]:
    """`AgentRunRequest.thinking_model` 允许的值，按 Literal 的书写顺序。"""
    annotation = AgentRunRequest.model_fields["thinking_model"].annotation
    return get_args(annotation)


def _load_export_script():
    spec = importlib.util.spec_from_file_location("export_thinking_options", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_is_first():
    """`default` 永远排第一。

    前端"不动下拉就生成"发的就是列表第一项，后端字段的默认值也是 `default`；
    两者不一样时，用户看到的拆解方式与真正生效的那一套不是同一个——而界面上
    没有任何地方会露馅。顺序是从 `THINKING_MODELS` 的 dict 顺序来的，所以这条
    断言同时钉住了那张表的书写顺序。
    """
    assert thinking_options()[0]["value"] == "default"
    assert AgentRunRequest.model_fields["thinking_model"].default == "default"


def test_table_and_contract_literal_agree():
    """后端内部的两份（dict 的键 / Literal）必须逐字同序。"""
    assert tuple(THINKING_MODELS) == _contract_values(), (
        "`THINKING_MODELS` 与 `AgentRunRequest.thinking_model` 的 Literal 不同步："
        "往表里加了一种思考方式，却没把它写进 Literal，前端选了就会 422。\n"
        "改 `app/agent/contracts.py` 的 Literal，然后重跑：\n"
        "    python scripts/export_openapi.py && (cd ../frontend && npm run gen:api)"
    )


def test_exported_snapshot_is_up_to_date():
    """生成物与后端表一致。"""
    module = _load_export_script()
    assert EXPORT.read_text(encoding="utf-8") == module.render(), (
        "frontend/src/api/thinking-options.json 过期了。重新生成：\n"
        "    cd backend && python scripts/export_thinking_options.py"
    )


def test_export_has_one_entry_per_thinking_model():
    """生成物是"全部"，不是"一部分"——少一条就等于那个选项从界面上消失。"""
    exported = json.loads(EXPORT.read_text(encoding="utf-8"))
    assert exported == thinking_options()
    assert [item["value"] for item in exported] == list(THINKING_MODELS)
    assert all(item["label"].strip() for item in exported)


def test_frontend_store_holds_no_copy_of_the_table():
    """前端那份必须只剩 `import`，不能有权值/标签的字面量。

    断言分两步，读起来是一件事：它 import 了生成物，而且没有任何一个非 `default`
    的键出现在源码里。`default` 豁免是因为它同时是**状态初值**
    （`thinkingModel = ref('default')`），那是语义不是副本。
    """
    source = STORE.read_text(encoding="utf-8")
    assert "thinking-options.json" in source, (
        "stores/options.ts 不再 import 生成物了——两份定义源是不是又回来了？"
    )
    for value in THINKING_MODELS:
        if value == "default":
            continue
        assert f"'{value}'" not in source and f'"{value}"' not in source, (
            f"stores/options.ts 里出现了思考方式字面量 {value!r}：候选列表必须来自"
            " frontend/src/api/thinking-options.json，不许在这里重抄一份。"
        )
