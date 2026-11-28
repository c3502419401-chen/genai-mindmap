"""三种结构在**真模型**下到底产不产得出来：鱼骨的 problem、流程的 role、树的稳定性。

    python scripts/measure_shapes.py                 # 三组各 2 次采样
    python scripts/measure_shapes.py --repeat 3
    python scripts/measure_shapes.py --only fishbone

**为什么必须有这个脚本**：`tests/` 钉的是契约与接线（mock 返回什么就落什么），
它证明不了"模型会不会写 problem、会不会标 decision"。那两件事只有真模型能回答，
而它们正是阶段 2 的成败判据：

  - 鱼骨的 `problem`：模型给的是不是一句「为什么 X 会 Y」，还是把标题复读一遍；
  - 流程的 `role`：判定节点有没有真的被标出来（`role="decision"`），回退边有几条；
  - 树：同一主题同一提示词跑两次，形状是不是稳定的（`--repeat` 对比）。

凭据：进程环境里没有时，从**用户级**环境变量（`HKCU\\Environment`）补上——与
`scripts/measure_single_call.py` 同一套（理由见那个脚本的 docstring）。

外部检索默认**关**（`MINDMAP_EXTERNAL_KNOWLEDGE=0`）：这一组量的是提示词能不能
产出形状，外部检索只会把噪声与等待时间带进来。
"""
# requires-optional: winreg
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

CREDENTIALS = (
    "MINDMAP_LLM_MODE", "MINDMAP_LLM_BASE_URL", "MINDMAP_LLM_API_KEY",
    "MINDMAP_LLM_MODEL", "MINDMAP_EMBEDDING_BASE_URL", "MINDMAP_EMBEDDING_API_KEY",
    "MINDMAP_EMBEDDING_PROTOCOL", "MINDMAP_EMBEDDING_MODEL", "MINDMAP_EMBEDDING_DIM",
)


def _user_env(name: str) -> str:
    try:
        import winreg
    except ImportError:  # 仅 Windows 有：Linux/CI 上没有 HKCU\Environment 这一层可读
        return ""

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(value)


def _fill_credentials() -> None:
    for name in CREDENTIALS:
        if not os.environ.get(name):
            value = _user_env(name)
            if value:
                os.environ[name] = value


# 三组。`diagram_type` 走**请求层**（与界面上选下拉是同一条路）。
CASES = (
    {"key": "fishbone", "goal": "机器学习的主要方向，用鱼骨图",
     "diagram_type": "fishbone", "branches": 6},
    {"key": "flowchart", "goal": "用户注册流程，用流程图",
     "diagram_type": "flowchart", "branches": 5},
    {"key": "tree", "goal": "机器学习的主要方向",
     "diagram_type": None, "branches": 6},
)

FISHBONE_CATEGORIES = ("人", "机", "料", "法", "环", "测")


def _fold(text: str) -> str:
    return "".join((text or "").split())


def _problem_kind(problem: str, title: str) -> str:
    """`problem` 像不像一句主骨陈述。四种形态，只有第一种是这条提示词想要的。

    `echoes_title` 是**实测里最常见的失败形态**（提示词里点名了"不要复读主题"），
    所以它单独一档：它与 `noun_phrase` 的区别是"模型根本没打算写论点"还是
    "写了但缺了为什么"。
    """
    text = _fold(problem)
    if not text:
        return "empty"
    if text == _fold(title):
        return "echoes_title"
    if text.startswith("为什么") or text.endswith("？") or text.endswith("?"):
        return "why_question"
    if text.startswith(("因为", "由于")):
        return "answer"
    return "noun_phrase"


def _tree_rows(content) -> list[tuple[int, str]]:
    """`(层号, label)` 的扁平清单——跨两次采样比较"形状是不是稳定"用。"""
    rows: list[tuple[int, str]] = []

    def walk(node, depth: int) -> None:
        rows.append((depth, node.label))
        for child in node.children:
            walk(child, depth + 1)

    walk(content.root, 1)
    return rows


def _metrics(payload, *, case: dict) -> dict:
    """一份产物 → 这一组的判据。"""
    from app.agent import shapes
    from app.contracts.mindmap import FishboneContent, FlowContent, MindMapContent

    diagnostics = [item["code"] for item in payload["diagnostics"]]
    digest = payload["digest"]
    content = payload["content"]

    if isinstance(content, FishboneContent):
        problems = [_problem_kind(content.problem, content.title)]
        categories = [bone.category for bone in content.bones]
        return {
            "shape": "fishbone",
            "problem": content.problem,
            "problem_kind": problems[0],
            "bones": len(content.bones),
            "categories": categories,
            "categories_complete": all(
                any(_fold(name) == _fold(cat) for cat in categories)
                for name in FISHBONE_CATEGORIES),
            "causes": [len(bone.root.children) for bone in content.bones],
            "nodes": shapes.count_nodes(content),
            "diagnostics": diagnostics,
            "digest": digest,
            "seconds": payload["seconds"],
        }
    if isinstance(content, FlowContent):
        roles: dict[str, int] = {}
        for node in shapes.iter_nodes(content):
            if node.role:
                roles[node.role] = roles.get(node.role, 0) + 1
        return {
            "shape": "flowchart",
            "stages": len(content.stages),
            "stage_labels": [stage.root.label for stage in content.stages],
            "roles": roles,
            "decisions": roles.get("decision", 0),
            "edges": len(content.edges),
            "loop_edges": sum(1 for edge in content.edges if edge.kind == "loop"),
            "branch_edges": sum(1 for edge in content.edges if edge.kind == "branch"),
            "nodes": shapes.count_nodes(content),
            "diagnostics": diagnostics,
            "digest": digest,
            "seconds": payload["seconds"],
        }
    assert isinstance(content, MindMapContent)
    return {
        "shape": "tree",
        "diagram_type": content.diagram_type,
        "nodes": shapes.count_nodes(content),
        "branches": [child.label for child in content.root.children],
        "rows": _tree_rows(content),
        "diagnostics": diagnostics,
        "digest": digest,
        "seconds": payload["seconds"],
    }


async def _one(case: dict, index: int) -> dict:
    """跑一次 `_generate`，把产物压成可比较的 payload。"""
    import hashlib

    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext
    from app.agent.tools import GenerateArgs, _generate

    request = AgentRunRequest(
        goal=case["goal"], intent="create", diagram_type=case["diagram_type"],
        budget=RunBudget(max_model_calls=12, max_output_tokens=32768),
    )
    ctx = RunContext(request=request)
    started = time.perf_counter()
    result = await _generate(GenerateArgs(topic=case["goal"],
                                          branches=case["branches"]), ctx)
    seconds = time.perf_counter() - started
    if not result.ok:
        return {"ok": False, "code": result.data.get("code"), "content": result.content}
    content = ctx.candidate
    return {
        "ok": True,
        "content": content,
        # 指纹用于 --repeat 的"逐字节"对比：树这条路它应当两次相同，鱼骨/流程的
        # `problem` 与 role 分布则可能逐次波动（那是模型行为，不是回归）。
        "digest": hashlib.sha256(
            content.model_dump_json().encode("utf-8")).hexdigest()[:16],
        "seconds": round(seconds, 1),
        # `_generate` 的 `data["diagnostics"]` 已经是 dict 列表（工具结果要能直接
        # 序列化成 JSON），这里原样带走。
        "diagnostics": list(result.data.get("diagnostics") or []),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--only", default="", help="fishbone / flowchart / tree")
    parser.add_argument("--json", default="", help="把逐次结果写到这个文件")
    args = parser.parse_args(argv)

    _fill_credentials()
    os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "0"
    os.environ.setdefault("MINDMAP_DB_PATH", "data/measure_shapes.db")

    cases = [case for case in CASES if not args.only or case["key"] == args.only]
    report: dict[str, list[dict]] = {}
    raw: list[dict] = []
    for case in cases:
        print(f"\n===== {case['key']}：{case['goal']}")
        samples: list[dict] = []
        for index in range(args.repeat):
            payload = asyncio.run(_one(case, index))
            if not payload["ok"]:
                print(f"  [{index + 1}] 失败：{payload['code']} {payload['content']}")
                samples.append({"ok": False, "code": payload["code"]})
                continue
            metrics = _metrics(payload, case=case)
            samples.append(metrics)
            raw.append({"case": case["key"], "index": index, "metrics": metrics})
            printable = {key: value for key, value in metrics.items()
                         if key not in ("rows", "diagnostics")}
            print(f"  [{index + 1}] {json.dumps(printable, ensure_ascii=False)}")
            if metrics.get("shape") == "tree":
                print(f"       指纹 {metrics['digest']}")
            diag = [code for code in metrics["diagnostics"]
                    if code.startswith(("FISHBONE_", "FLOW_"))]
            if diag:
                print(f"       形状诊断：{diag}")
        report[case["key"]] = samples

    print("\n===== 汇总")
    for key, samples in report.items():
        ok = [item for item in samples if item.get("shape")]
        if not ok:
            print(f"  {key}: 全部失败")
            continue
        if key == "fishbone":
            kinds = [item["problem_kind"] for item in ok]
            complete = sum(1 for item in ok if item["categories_complete"])
            print(f"  fishbone: {len(ok)}/{len(samples)} 次成功；"
                  f"problem 形态 {kinds}；六类齐全 {complete}/{len(ok)}")
        elif key == "flowchart":
            print("  flowchart: " + "；".join(
                f"阶段 {item['stages']}、decision {item['decisions']}、"
                f"边 {item['edges']}（回退 {item['loop_edges']}）" for item in ok))
        else:
            digests = [item["digest"] for item in ok]
            nodes = [item["nodes"] for item in ok]
            print(f"  tree: 节点数 {nodes}；指纹 {'相同' if len(set(digests)) == 1 else '不同'}"
                  f"；一级分支 {[item['branches'] for item in ok]}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n逐次结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
