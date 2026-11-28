"""量三段式（说明一段/两段）/ 一次生成 / 分片：同一主题、同一模型，各路的节点数 / detail 长度 / 跨支重复 / 耗时。

    python scripts/measure_single_call.py                  # 三段式（无材料、外部关）
    python scripts/measure_single_call.py --repeat 3        # 同一件事跑三次（采样）
    python scripts/measure_single_call.py --deep           # 分片（同前提）
    python scripts/measure_single_call.py --material FILE   # 三段式 + 一份材料（验引用落地）

**为什么要有这个脚本**：`tests/` 里钉的是契约与接线，量不出"detail 会不会变短"
——那件事只有真模型能回答，而它正是这条路最大的风险。脚本把六个指标一起打出来，
任何一次模型或提示词改动之后都能重跑同一组数字。

说明那一段现在会拆成两次并发（节点够多时，见 `tools.DETAIL_SPLIT_MIN`），所以输出
里多了 `style` 一块：两半各自的长度、句数、**模板化指称**（"本部分规定…" vs
"这一节讲…" 这种分叉）。`style.diverged == False` 才是"两半风格没分叉"。

`--repeat N` 是给"一次采样说明不了问题"准备的：detail 长度是模型行为，单次采样
波动可以达到十几字，对比表必须写清每一档采了几次。

凭据：进程环境里没有时，从**用户级**环境变量（`HKCU\\Environment`）补上。DSH /
CI 的 shell 是进程级作用域，未必继承了用户级那几项，`subprocess` 调 pwsh 在本机
也不一定找得到 PATH 里的可执行文件——直接读注册表最稳。

`--material` 会临时把 `MINDMAP_DB_PATH` 指到 `data/measure.db`（外部来源要入库才
能拿到持久 id），跑完可以直接删。
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

# 脚本在 `scripts/` 下，`python scripts/xxx.py` 会把**脚本目录**放进 sys.path
# 而不是 cwd——不补这一行，`import app` 在仓库根之外直接失败。
BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

CREDENTIALS = (
    "MINDMAP_LLM_MODE", "MINDMAP_LLM_BASE_URL", "MINDMAP_LLM_API_KEY",
    "MINDMAP_LLM_MODEL", "MINDMAP_EMBEDDING_BASE_URL", "MINDMAP_EMBEDDING_API_KEY",
    "MINDMAP_EMBEDDING_PROTOCOL", "MINDMAP_EMBEDDING_MODEL", "MINDMAP_EMBEDDING_DIM",
)

FILLER = ("简要说明", "详见", "可参考", "不再赘述", "不赘述", "见教材", "参见教材",
          "此处略", "从略", "待补充", "略述", "如上所述", "同上")

DEFAULT_TOPIC = "机器学习的主要方向"

# 一份结构清楚、长度过 `SHORT_MATERIAL_CHARS`（300 字）的材料。章节与正文一一对应，
# 按支检索才可能真的召回——用一份没有标题行的材料量"引用落地"是量不出来的。
DEFAULT_MATERIAL = (
    "# 预算管理办法\n\n"
    "## 预算审批\n\n"
    "预算由财务经理核准后执行，超过五十万元报总经理审批。"
    "每季度末做一次预算执行分析，偏差超过百分之十的需要书面说明原因。"
    "年度预算在十一月启动编制，各部门按上一年的执行率申报，财务汇总后报董事会。\n\n"
    "## 报销时限\n\n"
    "费用报销的时限是三十个自然日，逾期不再受理；跨年度的单据必须附说明。"
    "报销单需要部门负责人签字，财务只做形式审核，实质合规由内审抽查。"
    "差旅费按标准报销，超标部分需要总经理特批，特批记录留存三年备查。\n\n"
    "## 采购合规\n\n"
    "采购合同必须经过法务审核，金额超过五十万元另行招标。"
    "供应商准入每两年复核一次，出现过重大违约的记录在案，三年内不得再次准入。\n\n"
    "## 资产处置\n\n"
    "固定资产报废需要技术部门出具评估意见，残值收入上缴财务统一管理。"
    "处置金额超过一百万元的要报董事会备案，处置过程留痕，档案保存五年。\n\n"
    "## 附则\n\n"
    "本办法由财务部负责解释，自发布之日起执行；此前发布的相关规定同时废止。"
    "各部门可以在本办法框架内制定实施细则，报财务部备案后生效。\n"
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


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _report(content, result, elapsed: float) -> dict:
    from app.agent.tools import _cross_branch_duplicates

    nodes = list(_walk(content.root))
    details = [len((node.detail or "").strip()) for node in nodes if node is not content.root]
    duplicates = _cross_branch_duplicates(content.root)
    detail_report = result.data.get("detail") or {}
    return {
        "ok": result.ok,
        "seconds": round(elapsed, 2),
        "nodes": len(nodes),
        "branches": [child.label for child in content.root.children],
        "depths": {
            depth: sum(1 for node in nodes if _depth(content.root, node) == depth)
            for depth in sorted({_depth(content.root, node) for node in nodes})
        },
        "detail_avg": round(sum(details) / max(1, len(details)), 1),
        "detail_min": min(details, default=0),
        "detail_max": max(details, default=0),
        "filler_hits": [
            node.label for node in nodes
            if any(word in (node.detail or "") for word in FILLER)
        ],
        "cross_branch_duplicates": sum(len(places) for _, places in duplicates),
        "duplicate_labels": [label for label, _ in duplicates],
        "nodes_with_refs": sum(1 for node in nodes if node.source_refs),
        "refs_total": sum(len(node.source_refs) for node in nodes),
        # 两半的风格是否分叉。没拆（单次写全部）时是 None。
        "style": _style_split(content, detail_report),
        "data": {key: value for key, value in result.data.items()
                 if key in ("partial", "retrieval", "source_refs", "detail", "calls")},
        "diagnostics": sorted({item["code"] for item in result.data.get("diagnostics", [])}),
    }


def _depth(root, target) -> int:
    stack = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if node is target:
            return depth
        stack.extend((child, depth + 1) for child in node.children)
    return 0


# 开头要先剥掉的标点与空白：说明偶尔以「」（或引号、「一、」这类序号开头，
# 不剥掉的话同一个起手模式会被拆成好几个计数项。
_LEAD_NOISE = "「」『』（）(《【“‘\"' \t\r\n，。、；：!！?？—-·"

# **模板化指称**：说明以这些词开头时，读起来像"文件/教材在陈述"，而不是在讲这个
# 节点。两半是两次独立生成，最可能分叉的地方就是各用一套这样的起手词
# （「本部分规定…」 vs 「这一节讲…」）。这也是这条度量**唯一**要抓的东西。
_TEMPLATE_HEADS = (
    "本部分", "本节", "本章", "本文", "此处", "这一节", "这部分", "这一章", "这个话题",
    "该节", "该章", "该部分", "此节", "这里", "这其中", "其中", "下面", "以下",
    "首先", "其次", "此外", "另外", "总之", "综上", "总的来说", "总体而言", "简言之",
)


def _opener(text: str, label: str, width: int) -> str:
    """一段说明的"起手 pattern"：剥掉开头的标点**与节点自己的 label** 后取前 `width` 字。

    剥 label 是关键：detail 常以 label 开头（"监督学习用带标签的数据训练…"），
    不剥的话两半的"起手分布"量的其实是**它们各自写的是哪些节点**——那是内容差异，
    本来就该不同，把它算成分叉等于给自己造一个假警报（实测第一版就是这样：4–6 个
    "独占起手"全是「监督」「关联」「强化」这些 label 开头）。
    """
    body = (text or "").strip().lstrip(_LEAD_NOISE)
    if label:
        head = body[:len(label)]
        if head == label or (len(label) > 3 and head[:3] == label[:3]):
            body = body[len(head):]
    return body.lstrip(_LEAD_NOISE + "是指即，。：、").strip()[:width]


def _template_head(text: str) -> str | None:
    """这一段是不是以**模板化指称**开头的；是的话返回那个词。"""
    body = (text or "").strip().lstrip(_LEAD_NOISE)
    for word in _TEMPLATE_HEADS:
        if body.startswith(word):
            return word
    return None


def _style_profile(rows: list[tuple[str, str]]) -> dict:
    """一批说明的开头画像：长度分布 + 模板化指称 + 剥 label 后的起手 pattern。

    `rows` 是 `(label, detail)`。长度那一列是"拆两半有没有摊薄注意力"的直接答案，
    模板化指称那一列是"两半风格有没有分叉"的直接答案。
    """
    from collections import Counter

    from app.agent.tools import DETAIL_FILL_FLOOR

    lengths = [len(text) for _, text in rows]
    sentences = [text.count("。") + text.count("；") for _, text in rows]
    templates = Counter(word for _, text in rows if (word := _template_head(text)))
    heads = Counter(head for label, text in rows
                    if (head := _opener(text, label, 2)))
    return {
        "n": len(rows),
        "avg": round(sum(lengths) / max(1, len(lengths)), 1),
        "min": min(lengths, default=0),
        "under_floor": sum(1 for value in lengths if value < DETAIL_FILL_FLOOR),
        # 每段几句。提示词要求"至少 3 句"，这一列是那张要求在两半各自的执行度：
        # 一半 3.1 句、另一半 5.6 句，读起来就是两种笔调。
        "sentences": round(sum(sentences) / max(1, len(sentences)), 1),
        # **主判据**：以模板化指称开头的条数与具体词。两半各有一套（比如一半全是
        # 「本部分」、另一半全是「这一节」）就是分叉。
        "template_heads": dict(templates.most_common()),
        "templates": sum(templates.values()),
        # 辅助：剥掉 label 后剩下的起手二字。它反映"两半怎么起句"，label 带来的
        # 内容差异已经被剥掉了。
        "openers": dict(heads.most_common(4)),
        "_heads": heads,
    }


def _style_split(content, detail_report: dict) -> dict | None:
    """两半的说明风格是否分叉。**给数据，不给"应该没问题"。**

    两半各写了哪些节点从 `data["detail"]["halves"][i]["ids"]` 直接读——从树上重建
    切分是不行的：补过说明的节点不再满足 `_needs_detail`，事后重建必然错位。
    """
    halves = detail_report.get("halves")
    if not halves or not all(half.get("ids") for half in halves):
        return None
    by_id = {node.id: node for node in _walk(content.root)}
    groups = [
        [(by_id[node_id].label, (by_id[node_id].detail or "").strip())
         for node_id in half["ids"] if node_id in by_id]
        for half in halves
    ]
    profiles = [_style_profile(rows) for rows in groups]
    heads = [dict(profile.pop("_heads")) for profile in profiles]
    shared = sorted(set(heads[0]) & set(heads[1]))
    exclusive = sorted(
        pattern for pattern in set(heads[0]) | set(heads[1])
        if (heads[0].get(pattern, 0) >= 2 and heads[1].get(pattern, 0) == 0)
        or (heads[1].get(pattern, 0) >= 2 and heads[0].get(pattern, 0) == 0))
    # 模板化指称的分叉：只在**一侧**出现、≥2 次的词。这才是"一半「本部分规定」、
    # 另一半「这一节讲」"的判据；两边各有一些但**不重合**才算分叉。
    templates = [set(profile["template_heads"]) for profile in profiles]
    template_exclusive = sorted(
        (templates[0] - templates[1]) | (templates[1] - templates[0]))
    return {
        "halves": profiles,
        "shared_openers": shared,
        "exclusive_openers": exclusive,
        # 判据本身：两半共用的模板词有几个、只在单侧出现的模板词有几个。
        "shared_templates": sorted(templates[0] & templates[1]),
        "exclusive_templates": template_exclusive,
        "diverged": bool(template_exclusive),
        "avg_gap": round(abs(profiles[0]["avg"] - profiles[1]["avg"]), 1),
        "sentence_gap": round(abs(profiles[0]["sentences"] - profiles[1]["sentences"]), 1),
        "template_gap": profiles[0]["templates"] - profiles[1]["templates"],
    }


def _summary_line(index: int, report: dict) -> str:
    """一行采样摘要：对比表就是这几列手工/脚本拼出来的。"""
    calls = report["data"].get("calls") or {}
    detail = report["data"].get("detail") or {}
    halves = detail.get("halves") or []
    style = report.get("style") or {}
    split = (
        f"｜两半 {halves[0]['requested']}/{halves[1]['requested']}"
        f" 各 {halves[0]['seconds']}s/{halves[1]['seconds']}s"
        f" avg {style['halves'][0]['avg']}/{style['halves'][1]['avg']}"
        f" 句 {style['halves'][0]['sentences']}/{style['halves'][1]['sentences']}"
        f" 模板词 {style['halves'][0]['templates']}/{style['halves'][1]['templates']}"
        f" 分叉 {'是' if style['diverged'] else '否'}"
        if len(halves) == 2 and style else ""
    )
    return (
        f"#{index} 节点 {report['nodes']}｜detail 平均 {report['detail_avg']}"
        f" 最短 {report['detail_min']} 最长 {report['detail_max']}"
        f"｜空话 {len(report['filler_hits'])}｜跨支重复 {report['cross_branch_duplicates']}"
        f"｜总 {report['seconds']}s"
        f"（骨架 {calls.get('skeleton', '—')} / 结构 {calls.get('structure', '—')}"
        f" / 说明 {calls.get('detail', '—')}，检索 {report['data'].get('retrieval', {}).get('seconds', '—')}）"
        f"｜补写 {detail.get('filled', '—')}/{detail.get('requested', '—')}"
        f"｜引用 {report['refs_total']}"
        + split
    )


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deep", action="store_true", help="跑分片那条路（对比基线）")
    parser.add_argument("--material", metavar="FILE", nargs="?", const="",
                        help="带材料跑一次生成（不给路径时用内置的预算管理办法）")
    parser.add_argument("--topic", default="", help="中心主题")
    parser.add_argument("--branches", type=int, default=5)
    parser.add_argument("--external", action="store_true", help="无材料时开外部检索")
    parser.add_argument("--repeat", type=int, default=1, help="同一条件跑几次（采样）")
    parser.add_argument("--save", default="", help="把产出树写到这个路径")
    args = parser.parse_args(argv)

    _fill_credentials()
    os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "1" if args.external else "0"
    os.environ.setdefault("MINDMAP_EXTERNAL_PROXY", "http://127.0.0.1:7890")

    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
    from app.agent.tools import build_registry
    from app.contracts.source import Source, SourceKind

    topic = args.topic or ("预算管理办法" if args.material is not None else DEFAULT_TOPIC)
    sources: list[Source] = []
    if args.material is not None:
        text = (open(args.material, encoding="utf-8").read() if args.material
                else DEFAULT_MATERIAL)
        sources = [Source(id="s-measure", kind=SourceKind.document, status="parsed",
                          filename="measure.md", text=text)]
        os.environ.setdefault("MINDMAP_DB_PATH", "data/measure.db")

    reports: list[dict] = []
    for round_index in range(1, max(1, args.repeat) + 1):
        request = AgentRunRequest(
            goal=topic, intent="create", thinking_model="default", sources=sources,
            budget=RunBudget(max_model_calls=40, max_tool_calls=8, max_seconds=600.0,
                             max_output_tokens=65536, max_repairs=1),
        )
        ctx = RunContext(request=request,
                         sources_by_id={source.id: source for source in sources})
        if args.deep:
            registry = build_registry("create_deep", has_sources=bool(sources),
                                      has_library=False)
            call = ToolCall(call_id="measure", name="mindmap_generate_deep",
                            arguments={"topic": topic, "branches": args.branches,
                                       "points_per_branch": 3, "max_depth": 3})
        else:
            registry = build_registry("create", has_sources=bool(sources),
                                      has_library=False)
            call = ToolCall(call_id="measure", name="mindmap_generate",
                            arguments={"topic": topic, "branches": args.branches})

        started = time.perf_counter()
        result = await registry.execute(call, ctx)
        elapsed = time.perf_counter() - started

        if not result.ok:
            print(json.dumps({"ok": False, "content": result.content,
                              "seconds": round(elapsed, 2)}, ensure_ascii=False, indent=2))
            return 1
        report = _report(ctx.candidate, result, elapsed)
        reports.append(report)
        if args.repeat > 1:
            print(_summary_line(round_index, report), file=sys.stderr)
        if args.save:
            target = args.save if args.repeat == 1 else f"{args.save}.{round_index}"
            with open(target, "w", encoding="utf-8") as handle:
                json.dump(ctx.candidate.model_dump(), handle, ensure_ascii=False, indent=1)

    payload: dict = reports[0] if args.repeat == 1 else {
        "samples": [report["detail_avg"] for report in reports],
        "detail_avg": [report["detail_avg"] for report in reports],
        "detail_min": [report["detail_min"] for report in reports],
        "seconds": [report["seconds"] for report in reports],
        "calls": [report["data"].get("calls") for report in reports],
        "nodes": [report["nodes"] for report in reports],
        "cross_branch_duplicates": [report["cross_branch_duplicates"] for report in reports],
        "filler_hits": sum(len(report["filler_hits"]) for report in reports),
        "reports": reports,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
