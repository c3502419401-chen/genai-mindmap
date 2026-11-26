"""乙组（P3 语音 / P4 图像）的凭证核查：把"缺什么"写成可复核的读数。

只读环境变量与配置解析结果，不发任何请求（真跑在 `probe_media.py` 里）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

KEYS = [
    ("DASHSCOPE_API_KEY", "P3 语音走阿里云百炼 ASR"),
    ("MINDMAP_VISION_MODEL", "P4 视觉模型（若有单独配置）"),
    ("MINDMAP_VISION_API_KEY", "P4 视觉密钥（若有单独配置）"),
    ("MINDMAP_FREE_TIER_API_KEY", "游客配额（未设=不启用配额）"),
    ("MINDMAP_OPTIMIZATION_LINK", "P12 父位置建议开关"),
    ("MINDMAP_OPTIMIZATION_LLM", "P8 模型层建议开关"),
    ("MINDMAP_OPTIMIZATION_CROSS_MAP", "P18 跨图参照开关"),
    ("MINDMAP_GRAPH_SEARCH", "P17 图检索开关"),
    ("MINDMAP_EXTERNAL_KNOWLEDGE", "P7 外部检索"),
]


def main() -> int:
    import os

    rows = []
    for name, note in KEYS:
        value = os.getenv(name)
        rows.append({
            "name": name,
            "set": bool(value),
            "length": len(value) if value else 0,
            "note": note,
        })
        print(f"{name:32s} {'set' if value else 'unset':6s} {note}")

    # 视觉走的是生成侧同一处配置（settings/store.resolve）
    from app.settings import store as settings_store

    resolved = settings_store.resolve()
    vision = {
        "base_url": resolved.base_url,
        "model": resolved.model,
        "api_key_present": bool(resolved.api_key),
    }
    print("vision resolves to:", json.dumps(vision, ensure_ascii=False))

    path = Path(__file__).with_name("credentials.json")
    path.write_text(json.dumps({"env": rows, "vision": vision},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved:", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
