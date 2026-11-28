"""量两级骨架：同一份二十节材料、同一个模型，扁平 vs 分组三层对比。

    python scripts/measure_two_tier.py                 # 三个 arm 各跑一次
    python scripts/measure_two_tier.py --repeat 2      # 采样（detail 长度是模型行为）
    python scripts/measure_two_tier.py --material FILE # 换一份材料（照样取它的目录）

**三个 arm 是什么**：同一份代码，只动 `tools.SKELETON_GROUP_THRESHOLD` 与骨架这一粒：

  - `flat`（自然）：阈值调到不可能达到，骨架照旧只要一层。**模型自己决定几支**——
    这一列回答"不改的话现实是什么样的"；
  - `flat_same_aspects`（同方面）：扁平，但骨架**复用 `two_tier` 那一份的二十节**
    （不调模型，直接喂给它）。与上一列合起来回答"二十支是不是本来就会出现"；
  - `two_tier`：真实阈值（10），骨架那一次就要求"归成 N 组"。

`flat_same_aspects` 与 `two_tier` 的**唯一**差别是树的形状：同样的二十个方面、同样的
检索结果、同样的结构调用。所以"一级宽度 20 → 5"这一列的因果是干净的——它不是
"模型这次恰好少写了几支"。

**为什么要这个脚本**：`tests/` 里钉的是契约（分组出现了、组不吃调用、老形状不回归），
量不出"分两级之后图宽了多少、detail 有没有变短、耗时有没有变长"——那几件事只有真
模型能回答，而且每次改提示词或换 provider 都值得重跑同一组数字。

口径（与 `measure_single_call.py` 对齐，两处可以直接并读）：

  - `一级宽度`：根节点的子节点数。分两级之后它就是**组数**（这是这次改动要压的那个数）；
  - `节点/层`：全树的节点数与各层个数；
  - `detail 平均/最短`：不含根节点；两级的**分组节点不参与**（它们本来就不写说明）；
  - `模型调用`：在 `chat_async` 外面数的，按 system 分成 骨架/结构/说明 三类；
    `flat_same_aspects` 的骨架是复用的，那一格按"这一步存在"计 1（实际没发出去）；
  - `分组进检索/进说明`：两级路特有的两列，直接回答"组节点有没有浪费调用"；
  - `跨支重复`：`_cross_branch_duplicates` 的节点数（分两级之后**会不会回来**的判据）。

凭据与开关沿用 `measure_single_call.py` 的两条约定：进程环境里没有就从用户级
（`HKCU\\Environment`）补；外部检索默认关（它要外网，会污染对比）。
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

DEFAULT_TOPIC = "机器学习讲义"
# 表的列顺序（读的人先看"不改是什么样"，再看"形状换来什么"）。
ARMS = ("flat", "flat_same_aspects", "two_tier")
# 执行顺序：`flat_same_aspects` 要吃 `two_tier` 那一份骨架，所以它必须排后面。
RUN_ORDER = ("two_tier", "flat_same_aspects", "flat")

# 二十节，每节正文互不相同。**正文必须真不同**：上一版每节共用一句"做X时要先确定
# 目标与评价口径"，模型于是把同一句拆成同样的要点挂在每一支下面，跨支重复量出来 58
# ——那不是分两级造成的，是材料本身在重复自己。要量"分两级会不会让重复回来"，
# 材料就不能自带重复。
BODY: dict[str, str] = {
    "概念界定": "机器学习让程序从数据里归纳规律，而不是由人把规则一条条写死。"
                "它的三要素是数据、模型与优化目标，输出可以是数值、类别，也可以是一个策略。",
    "发展脉络": "从感知机、统计学习到深度网络，主线是特征从人工设计变成自动学习。"
                "算力与数据规模两次跃迁各带来一轮方法更替，符号主义那条线并未消失。",
    "与相邻领域的关系": "统计学关心推断与置信，机器学习更关心在新样本上的预测表现。"
                        "运筹学提供优化工具，认知科学提供关于表征的假设，三者边界是软的。",
    "术语约定": "样本是最小单元，特征是它的可测属性，标签是想要预测的那个量。"
                "损失函数把预测偏差压成一个数，超参数是训练前定、不由梯度更新的那些。",
    "监督学习": "有标签数据上的学习，分类输出离散类别、回归输出连续数值。"
                "它假设训练与测试同分布，分布一变精度就掉，这一点比模型选择更关键。",
    "无监督学习": "没有标签时只能利用数据自身的结构，聚类按相似度分组、降维保留主要变化方向。"
                  "结果没有唯一正确答案，评估往往要回到下游任务上做。",
    "强化学习": "智能体在环境里试错，用奖励信号调整策略，马尔可夫决策过程给出形式化框架。"
                "信用分配与探索是两条主要困难，仿真到真机的差距要靠域随机化缓解。",
    "半监督学习": "少量标注加大量未标注时，先用模型给未标注数据打伪标签再一起训练。"
                  "自训练容易把早期错误固化，一致性正则与教师学生结构是目前更稳的做法。",
    "数据采集": "采样方式决定了模型能学到什么，方便采样与真实分布之间往往差着一层筛选。"
                "埋点口径不统一时，两边数值对不上会被误读成模型退化。",
    "清洗与标注": "去重先去近似重复，否则同一份内容会同时出现在训练与测试里。"
                  "标注一致性用两人标注的 kappa 衡量，低于 0.6 的类别通常需要重写标注规范。",
    "特征工程": "数值特征做标准化或分箱，类别特征用独热或目标编码。"
                "时间类特征要防泄漏，把未来信息带进训练是准确率虚高的头号原因。",
    "数据增强": "图像常用翻转、裁剪、颜色扰动，文本常用回译与同义替换。"
                "增强方式必须与任务语义相容，把数字翻转会让模型学到错的东西。",
    "训练流程": "先划分训练、验证、测试三份，验证集只用于选模型与调参。"
                "早停看验证损失连续若干轮不降，学习率调度与批大小共同决定收敛快慢。",
    "调参与评估": "网格搜索适合低维，贝叶斯搜索在超参数多时更省预算。"
                  "类别不平衡时看 F1 与 PR 曲线，阈值要按业务代价定，不能默认 0.5。",
    "部署与监控": "推理要控延迟与显存，量化与蒸馏是常用的两条压缩路线。"
                  "上线后监控输入分布与预测分布，漂移报警比离线指标更能提前发现问题。",
    "常见故障": "过拟合表现为训练集远好于验证集，正则与更多数据都能缓解。"
                "数据泄漏让指标高得不合理，类别不平衡会让模型退化成只预测多数类。",
    "代表工作": "ResNet 用残差连接解决了深层网络的退化，Transformer 用自注意力替代循环结构。"
                "梯度提升树在表格数据上仍然强势，选型要看数据形态而不是看热度。",
    "开放问题": "因果推断与相关性学习的边界还没划清，鲁棒性与对抗样本缺乏统一度量。"
                "长尾分布下的泛化、持续学习中的遗忘，都是当前没有标准答案的问题。",
    "可解释性": "SHAP 用博弈论的边际贡献分摊特征影响，注意力权重不等于解释。"
                "置信校准让预测概率可以当概率用，温度缩放是最便宜的一种校准手段。",
    "成本与伦理": "训练成本集中在算力与标注，推理成本集中在并发与延迟。"
                  "隐私要求推动联邦与差分隐私，偏见则要在数据与评估口径两处同时检查。",
}


def _default_material() -> str:
    """一份真材料：每节一个 Markdown 标题 + 互不相同的正文，目录覆盖率接近 100%。"""
    body = ["# 机器学习讲义"]
    for label, text in BODY.items():
        body.append(f"## {label}\n\n{text}")
    return "\n\n".join(body) + "\n"


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


def _depths(root) -> dict[int, int]:
    counts: dict[int, int] = {}
    stack = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        counts[depth] = counts.get(depth, 0) + 1
        stack.extend((child, depth + 1) for child in node.children)
    return dict(sorted(counts.items()))


def _classify(system: str) -> str:
    """这一条 system 属于哪一段：骨架 / 结构 / 说明（只看前缀，与用例同一判据）。"""
    from app.agent.tools import DETAIL_ONLY_PROMPT, PLAN_PROMPT

    if system.startswith(PLAN_PROMPT):
        return "skeleton"
    if system.startswith(DETAIL_ONLY_PROMPT):
        return "detail"
    return "structure"


async def _run_arm(
    *, arm: str, text: str, topic: str, branches: int,
    forced_skeleton: tuple[str, ...] = (),
):
    """跑一个 arm，返回 `(报告, 产出树)`；`forced_skeleton` 非空时不调骨架。"""
    from app.agent import tools as tools_module
    from app.agent.contracts import (
        AgentRunRequest, RunBudget, RunContext, ToolCall,
    )
    from app.agent.tools import build_registry
    from app.contracts.source import Source, SourceKind

    calls: dict[str, int] = {"skeleton": 0, "structure": 0, "detail": 0}
    real_chat = tools_module.chat_async

    async def counting_chat(system: str, user: str, **kwargs):
        kind = _classify(system)
        if kind == "skeleton" and forced_skeleton:
            # 复用另一臂的骨架：这一步不计入"实际发出的调用"，但报告里按 1 记
            # （它在真实流水线里存在，只是内容来自另一次运行）。
            return {"branches": [{"label": label} for label in forced_skeleton]}
        calls[kind] += 1
        return await real_chat(system, user, **kwargs)

    sources = [Source(id="s-lecture", kind=SourceKind.document, status="parsed",
                      filename="机器学习讲义.md", text=text)]
    request = AgentRunRequest(
        goal=topic, intent="create", thinking_model="default", sources=sources,
        budget=RunBudget(max_model_calls=40, max_tool_calls=8, max_seconds=900.0,
                         max_output_tokens=65536, max_repairs=1),
    )
    ctx = RunContext(request=request,
                     sources_by_id={source.id: source for source in sources})
    registry = build_registry("create", has_sources=True, has_library=False)
    call = ToolCall(call_id="measure", name="mindmap_generate",
                    arguments={"topic": topic, "branches": branches})

    threshold = tools_module.SKELETON_GROUP_THRESHOLD
    tools_module.SKELETON_GROUP_THRESHOLD = (
        threshold if arm == "two_tier" else 10**6)
    tools_module.chat_async = counting_chat
    try:
        started = time.perf_counter()
        result = await registry.execute(call, ctx)
        elapsed = time.perf_counter() - started
    finally:
        tools_module.chat_async = real_chat
        tools_module.SKELETON_GROUP_THRESHOLD = threshold

    if not result.ok:
        return {"ok": False, "arm": arm, "content": result.content}, None

    content = ctx.candidate
    root = content.root
    group_ids = {child.id for child in root.children}
    all_nodes = list(_walk(root))
    nodes = [node for node in all_nodes if node is not root]
    # detail 平均不含根，也不含分组（分组本来就不写说明，把它算进来会让两级那列
    # 恒定偏低——那不是"说明变短了"，是分母换了）。
    skeleton = result.data.get("skeleton") or {}
    grouped = bool(skeleton.get("two_tier"))
    group_labels = [child.label for child in root.children] if grouped else []
    # 骨架那一次的**原样二十节**（裁剪之前的）；`flat_same_aspects` 用的就是它。
    aspects = [branch for group in skeleton.get("groups") or []
               for branch in group.get("branches") or []]
    # 二十节里有几节真的上了画布：两级数分组的孩子（二级分支），扁平数根的孩子。
    # 两者都是"方面"那一层的节点数，所以可以和 `planned_branches` 直接比——差掉的
    # 那几节是被 `INITIAL_MAX_NODES=80` 裁掉的：**这正是"一级太宽"的第二个代价**，
    # 宽的那一版先被裁掉的恰恰是方面本身，而分组把这一层保住了。
    aspects_visible = (sum(len(child.children) for child in root.children)
                       if grouped else len(root.children))
    aspects_total = result.data.get("planned_branches") or aspects_visible
    detail_nodes = [node for node in nodes
                    if not (grouped and node.id in group_ids)]
    details = [len((node.detail or "").strip()) for node in detail_nodes]
    retrieval = result.data.get("retrieval") or {}
    detail_report = result.data.get("detail") or {}
    refs = sum(len(node.source_refs) for node in all_nodes)

    from app.agent.tools import _cross_branch_duplicates

    duplicates = _cross_branch_duplicates(root)
    # 组节点有没有被当成"要展开的东西"：进了检索 query 或进了说明清单就是浪费。
    # 说明清单从 `detail.halves[*].ids` 读（那是**本半要写哪些节点**的原件；
    # 补完说明之后从树上再也还原不出切分）。
    groups_in_detail = sum(
        1 for half in (detail_report.get("halves") or [])
        for node_id in half.get("ids") or []
        if node_id in group_ids)
    skeleton_calls = 1          # 逻辑上这一步存在（forced 时内容来自另一臂）
    return {
        "ok": True,
        "arm": arm,
        "reused_skeleton": bool(forced_skeleton),
        "width": len(root.children),            # 一级宽度 = 根的子节点数
        "groups": group_labels,
        "aspects": aspects,
        "aspects_visible": aspects_visible,
        "aspects_total": aspects_total,
        "planned_branches": result.data.get("planned_branches"),
        "nodes": len(all_nodes),
        "depths": _depths(root),
        "detail_avg": round(sum(details) / max(1, len(details)), 1),
        "detail_min": min(details, default=0),
        "detail_max": max(details, default=0),
        "detail_requested": detail_report.get("requested"),
        "detail_filled": detail_report.get("filled"),
        "seconds": round(elapsed, 1),
        "calls": calls,
        "calls_total": calls["structure"] + calls["detail"] + skeleton_calls,
        # 组节点有没有被当成"要展开的东西"：一级标题出现在检索 query 里、或出现在
        # 说明清单里，就是浪费了一次本可以给二十节的资源。
        "groups_in_retrieval": sum(
            1 for query in retrieval.get("queries") or []
            if query.strip() in set(group_labels)),
        "groups_in_detail": groups_in_detail,
        "retrieval_queries": len(retrieval.get("queries") or []),
        "retrieval_seconds": retrieval.get("seconds"),
        "branches_with_hits": retrieval.get("branches_with_hits"),
        "cross_branch_duplicates": sum(len(places) for _, places in duplicates),
        "duplicate_labels": [label for label, _ in duplicates],
        "refs": refs,
        "partial": result.data.get("partial"),
        "diagnostics": sorted({item["code"] for item in result.data["diagnostics"]}),
    }, content


def _table(reports: dict[str, dict]) -> str:
    """对比表。行顺序就是读的顺序：先回答"宽不宽"，再回答"贵不贵"。"""

    def _d(value) -> str:
        if not isinstance(value, dict):
            return "—"
        return " / ".join(f"{depth}层{count}个" for depth, count in value.items())

    def _calls(value) -> str:
        if not isinstance(value, dict):
            return "—"
        return (f"{value.get('skeleton', 0)}+{value.get('structure', 0)}"
                f"+{value.get('detail', 0)}")

    def cell(key: str, render=str) -> str:
        return " | ".join(f"{render(reports[arm][key])}" for arm in ARMS)

    rows = [
        ("骨架要的方面数", cell("planned_branches")),
        ("一级分支数（落树后图宽）", cell("width")),
        ("分组数", cell("groups", lambda value: len(value) if value else "—")),
        ("上画布的方面（/骨架要的）", " | ".join(
            f"{reports[arm]['aspects_visible']}/{reports[arm]['aspects_total']}"
            for arm in ARMS)),
        ("总节点数", cell("nodes")),
        ("层分布", cell("depths", _d)),
        ("detail 平均", cell("detail_avg")),
        ("detail 最短/最长", " | ".join(
            f"{reports[arm]['detail_min']}/{reports[arm]['detail_max']}"
            for arm in ARMS)),
        ("写回说明/需要", " | ".join(
            f"{reports[arm]['detail_filled']}/{reports[arm]['detail_requested']}"
            for arm in ARMS)),
        ("总耗时(s)", cell("seconds")),
        ("模型调用（骨架+结构+说明）", cell("calls", _calls)),
        ("模型调用总数", cell("calls_total")),
        ("组节点进检索", cell("groups_in_retrieval")),
        ("组节点进说明清单", cell("groups_in_detail")),
        ("检索 query 数", cell("retrieval_queries")),
        ("检索命中支数", cell("branches_with_hits")),
        ("跨支重复（节点）", cell("cross_branch_duplicates")),
        ("引用条数", cell("refs")),
        ("partial", cell("partial")),
    ]
    head = ("| 指标 | 不分两级（自然） | 不分两级（同 20 方面） | 分两级 |"
            "\n|---|---|---|---|")
    body = "\n".join(f"| {label} | {values} |" for label, values in rows)
    return head + "\n" + body


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--material", metavar="FILE", default="",
                        help="材料文件（不给就用内置的二十节讲义）")
    parser.add_argument("--topic", default="", help="中心主题")
    parser.add_argument("--branches", type=int, default=len(BODY),
                        help="请求的一级分支上限（三个 arm 用同一个值）")
    parser.add_argument("--repeat", type=int, default=1, help="每一轮跑几条")
    parser.add_argument("--save", default="", help="把报告写到这个 JSON 路径")
    args = parser.parse_args(argv)

    _fill_credentials()
    os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "0"
    os.environ["MINDMAP_LIBRARY_SEARCH"] = "0"
    os.environ["MINDMAP_LIBRARY_REMEMBER"] = "0"
    os.environ.setdefault("MINDMAP_DB_PATH", "data/measure_two_tier.db")

    text = (Path(args.material).read_text(encoding="utf-8")
            if args.material else _default_material())
    topic = args.topic or DEFAULT_TOPIC

    rounds: list[dict[str, dict]] = []
    for round_index in range(1, max(1, args.repeat) + 1):
        reports: dict[str, dict] = {}
        aspects: tuple[str, ...] = ()
        for arm in RUN_ORDER:
            report, _content = await _run_arm(
                arm=arm, text=text, topic=topic, branches=args.branches,
                forced_skeleton=aspects if arm == "flat_same_aspects" else ())
            reports[arm] = report
            if arm == "two_tier" and report["ok"]:
                # `two_tier` 那一臂本来就要跑，它的二十节正好是同方面对比的输入。
                aspects = tuple(report["aspects"])
            if report["ok"]:
                line = (f"[{round_index}] {arm}: 一级 {report['width']} 宽"
                        f"｜{report['nodes']} 节点｜detail {report['detail_avg']}"
                        f"｜{report['seconds']}s｜调用 {report['calls_total']}"
                        f"｜重复 {report['cross_branch_duplicates']}")
            else:
                line = f"[{round_index}] {arm}: 失败 {report['content']}"
            print(line, file=sys.stderr)
        rounds.append(reports)

    first = rounds[0]
    print(_table(first))
    if args.repeat > 1:
        for arm in ARMS:
            print(f"\n{arm} 采样：" + json.dumps({
                key: [item[arm][key] for item in rounds]
                for key in ("width", "nodes", "detail_avg", "seconds", "calls_total",
                            "cross_branch_duplicates")
            }, ensure_ascii=False))
    if args.save:
        Path(args.save).write_text(
            json.dumps(rounds, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n报告已写到 {args.save}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
