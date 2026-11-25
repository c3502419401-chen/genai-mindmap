"""真跑一次带文档的整树生成，量 node_grow 的时刻序列（首帧 / 空档 / 总数）。

用法：`python scripts/measure_grow_stream.py`（环境变量在本文件里按 run-backend.bat 设）

不经过 8000 那个服务：直接构造 `RunContext` 调 `mindmap_generate`，通道换成
一个记时间戳的 `emit`。这样量到的就是工具内部真实推帧的时刻，没有被 SSE/HTTP
那一层掩盖。模型、材料、开关与 run-backend.bat 完全一致。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
os.chdir(BACKEND)
sys.path.insert(0, BACKEND)

SOURCE_NAME = "1706.03762v1.pdf"

# 与 run-backend.bat 同一套（只是把库换成副本，避免真跑污染 data/mindmaps.db）
os.environ.setdefault("MINDMAP_LLM_MODE", "model")
os.environ.setdefault("MINDMAP_LLM_BASE_URL", "https://api.deepseek.com")
os.environ.setdefault("MINDMAP_LLM_API_KEY", "sk-7961cf33c9c94110b749e800adfb067d")
os.environ.setdefault("MINDMAP_LLM_MODEL", "deepseek-chat")
os.environ.setdefault("MINDMAP_LLM_MODEL_DEEP", "deepseek-reasoner")
os.environ.setdefault("MINDMAP_EMBEDDING_PROTOCOL", "openai")
os.environ.setdefault(
    "MINDMAP_EMBEDDING_BASE_URL",
    "https://ws-y7zvccm7g093iscn.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")
os.environ.setdefault(
    "MINDMAP_EMBEDDING_API_KEY",
    "sk-ws-H.YPDPXIH.i6xQ.MEYCIQC_IlGF2kv7V-DDTl2I54Qkv3n37srUT9H4YWQD6rCGoAIhAKhOJIp5ukcLqzfQFrb5AxbSUMIpXh17MqPv0dDK5psU")
os.environ.setdefault("MINDMAP_RERANK", "1")
os.environ.setdefault("MINDMAP_RERANK_MODEL", "qwen3-rerank")
os.environ.setdefault("MINDMAP_PROFILE_RERANK", "1")
os.environ.setdefault("MINDMAP_SECTION_SLICE", "1")
os.environ.setdefault("MINDMAP_NODE_SCALE", "1")
os.environ.setdefault("MINDMAP_DETAIL_SCALE", "1")
os.environ.setdefault("MINDMAP_DETAIL_LENGTH", "free")
os.environ.setdefault("MINDMAP_VERIFY_AFTER_GENERATE", "0")
os.environ.setdefault("MINDMAP_EXTERNAL_KNOWLEDGE", "1")
os.environ.setdefault("MINDMAP_DETAIL_RETRIEVAL", "1")

# 副本库：材料从真库里读，写入落在临时文件上
REAL_DB = os.path.join(BACKEND, "data", "mindmaps.db")
COPY_DB = os.path.join(tempfile.gettempdir(), "grow-stream-measure.db")
shutil.copyfile(REAL_DB, COPY_DB)
os.environ["MINDMAP_DB_PATH"] = COPY_DB

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall  # noqa: E402
from app.agent.tools import build_registry  # noqa: E402
from app.contracts.source import Source, SourceKind  # noqa: E402


def load_source() -> Source:
    con = sqlite3.connect(COPY_DB)
    row = con.execute(
        "select id, filename, kind, text from library_sources where filename = ?",
        (SOURCE_NAME,),
    ).fetchone()
    con.close()
    if row is None:
        raise SystemExit(f"库里没有 {SOURCE_NAME}")
    source_id, filename, kind, text = row
    return Source(
        id=source_id,
        kind=SourceKind(kind) if kind in {item.value for item in SourceKind} else SourceKind.document,
        status="parsed",
        filename=filename,
        text=text,
    )


async def main() -> None:
    source = load_source()
    print(f"材料：{source.filename} / {len(source.text or '')} 字")

    frames: list[tuple[float, dict]] = []
    started = time.perf_counter()

    async def emit(payload: dict) -> None:
        frames.append((time.perf_counter() - started, payload))

    request = AgentRunRequest(
        goal="Attention Is All You Need 的精读笔记",
        intent="create",
        thinking_model="default",
        sources=[source],
        budget=RunBudget(max_model_calls=40, max_tool_calls=8, max_seconds=600.0,
                         max_output_tokens=8192, max_repairs=1),
    )
    ctx = RunContext(request=request, sources_by_id={source.id: source})
    ctx.emit = emit

    registry = build_registry("create", has_sources=True, has_library=False)
    result = await registry.execute(
        ToolCall(call_id="c-measure", name="mindmap_generate",
                 arguments={"topic": "Attention Is All You Need 的精读笔记", "branches": 12}),
        ctx,
    )
    total = time.perf_counter() - started

    grows = [(at, frame) for at, frame in frames if frame.get("type") == "node_grow"]
    stages = [(at, frame) for at, frame in frames if frame.get("type") == "stage"]

    print(f"\n结果 ok={result.ok} 总时长={total:.1f}s")
    print(f"帧总数={len(frames)}  node_grow={len(grows)}  stage={len(stages)}")
    if not result.ok:
        print("失败：", result.content[:400])
        return
    print(f"落树节点数={result.data.get('node_count')}  "
          f"章节={result.data.get('section_slice', {}).get('chapters')}  "
          f"诊断={len(result.data.get('diagnostics') or [])}")

    for at, frame in stages:
        print(f"  [stage {at:6.1f}s] {frame.get('stage')} · {frame.get('detail')}")

    if grows:
        first = grows[0][0]
        gaps = [(grows[index][0] - grows[index - 1][0], grows[index - 1][0], grows[index][0])
                for index in range(1, len(grows))]
        worst = max(gaps, default=(0.0, 0.0, 0.0))
        print(f"\n首帧时刻 = {first:.2f}s（相对点下生成）")
        print(f"末帧时刻 = {grows[-1][0]:.2f}s")
        print(f"最大空档 = {worst[0]:.2f}s（{worst[1]:.2f}s → {worst[2]:.2f}s）")
        print("前 8 帧：")
        for at, frame in grows[:8]:
            print(f"  {at:6.2f}s  path={frame.get('path')}  {frame.get('label')}")
        print("末 3 帧：")
        for at, frame in grows[-3:]:
            print(f"  {at:6.2f}s  path={frame.get('path')}  {frame.get('label')}")
        print("\n逐帧时刻（间隔 >5 秒的用 ⚠ 标出）：")
        line = []
        for index, (at, frame) in enumerate(grows):
            mark = ""
            if index:
                gap = at - grows[index - 1][0]
                if gap > 5:
                    mark = f"⚠{gap:.1f}s "
            line.append(f"{mark}{at:.1f}")
        print("  " + " ".join(line))
        print("\nJSON:", json.dumps({
            "first_frame_s": round(first, 2),
            "last_frame_s": round(grows[-1][0], 2),
            "max_gap_s": round(worst[0], 2),
            "frames": len(grows),
            "nodes": result.data.get("node_count"),
            "total_s": round(total, 2),
        }, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
