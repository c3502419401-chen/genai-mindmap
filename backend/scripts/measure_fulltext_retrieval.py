"""数一遍：全塞（材料装得下 → 整份正文进 prompt）时到底有没有发检索。

用户那句话是「全塞的话不用检索了直接大模型依据全文总结就行」。代码上它是不是真的
成立，靠读代码回答不了——`_generate` 里有**三条**检索路径（按支检索、按节点章内
检索、兜底的整树检索），任何一条漏判都会让"跳过"变成"跳过了两条、还剩一条"。
所以这里用计数替身把三条都包住，跑一次全塞，把次数打出来。

    cd backend
    python scripts/measure_fulltext_retrieval.py            # 三格都跑（真模型，约 2 分钟）
    python scripts/measure_fulltext_retrieval.py --dry      # 只造材料，不调模型

三格回答三件事：

    A 全塞 · 材料无章节    topic-only 那条路的样子（外部检索回来的正文没有 markdown
                          标题）：两条检索都该是 0 次
    B 全塞 · 材料有章节    一份小 markdown：**全塞与按章切同时成立**。这一格是
                          "跳过"这句话的边界——按节点章内检索（`MINDMAP_DETAIL_RETRIEVAL`）
                          的前置条件是 `chapters`，而它有章节
    C 按支 · 材料有章节    对照格：同样的材料，闸门关掉（`FULL_TEXT_MATERIAL_CHARS=0`），
                          按支检索与章内检索都该真的发出去

B 与 C 的差就是"全塞跳过的那部分"；A 与 B 的差说明这句"全塞不检索"在什么形状上
才无条件成立。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(BACKEND / "scripts") not in sys.path:
    sys.path.insert(0, str(BACKEND / "scripts"))

import measure_incontext as harness  # noqa: E402  （凭证装载与拓扑照抄那一份）

TOPIC = "园区能耗管理办法：谁登记、备案号是多少"

# 四节 × 约 600 字。有标题版本是 markdown（`##`），无标题版本把标题行删掉、段落
# 逐字不变——于是 A 与 B 的差只有"材料有没有章节"这一件事。
SECTIONS = (
    ("登记与备案", (
        "本办法适用于园区全部用能单元的能耗台账管理。凡单月用电量超过五十万度、或单月"
        "用水量超过两万吨的单元，须在次月五日前提交用能登记表，由能源管理中心统一编号，"
        "编号规则为 EN-{year}-{seq}。登记内容包括用能设备清单、计量点位、运行时段、"
        "责任岗位与联络人。未按期登记的，能源管理中心应当书面通知其限期补正，并在月度"
        "通报中列出补正情况。登记信息发生变更的，责任岗位应在五个工作日内更新；逾期"
        "未更新的，按未登记处理。新投产的单元在并网送电前完成首次登记，不得先投后用。"
        "登记表由能源管理中心统一归档，保存期限不少于五年，跨年度查阅须经分管负责人同意。"
    )),
    ("计量与抄表", (
        "计量点位的安装位置由能源管理中心会同使用部门确定，任何部门不得擅自迁移或短接。"
        "抄表周期为每旬一次，抄表数据须在采集当日录入台账系统，录入人与复核人不得为"
        "同一人。计量装置故障期间，按前三个周期同口径的均值估算，并在备注中标注估算"
        "原因与时段；故障修复后十个工作日内按实际数据回溯更正。估算数据不得用于考核"
        "结算，仅作为运行分析的参考。重点用能单元应逐步加装分项计量，分项与总表每月"
        "核对一次，偏差超过百分之三的应查明原因并记录处置过程。"
    )),
    ("核查与抽查", (
        "能源管理中心每季度组织一次交叉核查，抽查比例不低于登记单元的百分之十，且覆盖"
        "全部重点用能单元。核查内容包括台账与计量数据的一致性、变更记录的完整性、异常"
        "用能的处置留痕、以及上季度整改事项的落实。抽查发现台账与计量数据偏差超过"
        "百分之五的，应要求责任部门在十个工作日内说明原因；无法说明的，按数据不实登记。"
        "连续两个季度被要求说明原因的单元，纳入下一年度的重点监测名单并向园区管委会报告。"
        "核查结论书面反馈责任部门，抄送内部审计。"
    )),
    ("考核与改进", (
        "能耗指标纳入部门年度考核，权重不低于百分之五。考核以年度累计单位产值能耗为准，"
        "并按季度公布排名。排名末位的部门应在次季度提交改进方案，方案须包含具体措施、"
        "责任人、所需投入与完成时限。改进方案实施满一年后由能源管理中心组织复评，复评"
        "结论分为达标、基本达标、未达标三档；未达标的部门在下一年度扣减相应的用能配额，"
        "扣减比例不低于百分之五。连续两年未达标的，由管委会约谈部门负责人并要求提交"
        "专项整改报告。达标部门的经验做法在园区范围内通报推广。"
    )),
)


def build_material(*, headings: bool) -> str:
    """四节正文。`headings` 决定要不要 markdown 标题行（正文逐字相同）。"""
    parts: list[str] = []
    for index, (title, body) in enumerate(SECTIONS, start=1):
        parts.append(f"## {index} {title}\n\n{body}" if headings else body)
    return "\n\n".join(parts) + "\n"


class Counter:
    """一次运行里各条检索路径各发了几次。**只数，不改行为**。"""

    NAMES = ("_retrieve_branches", "search_parent", "search_multi",
             "search_exact_blocks", "_detail_evidence", "_retrieve_whole")

    def __init__(self) -> None:
        self.counts: dict[str, int] = {name: 0 for name in self.NAMES}

    def wrap(self, tools: Any) -> dict[str, Any]:
        originals: dict[str, Any] = {}
        for name in self.NAMES:
            original = getattr(tools, name)
            originals[name] = original
            setattr(tools, name, self._make(name, original))
        return originals

    def _make(self, name: str, original: Any) -> Any:
        counter = self

        if asyncio.iscoroutinefunction(original):
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                counter.counts[name] += 1
                return await original(*args, **kwargs)
            async_wrapper.__name__ = name
            return async_wrapper

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            counter.counts[name] += 1
            return original(*args, **kwargs)
        wrapper.__name__ = name
        return wrapper

    def total(self) -> int:
        return sum(self.counts.values())


async def run_one(label: str, *, headings: bool, full_text: bool,
                  branches: int) -> dict[str, Any]:
    """跑一次 `mindmap_generate`，返回这一格的读数。"""
    from app.agent import tools as agent_tools
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
    from app.contracts.source import Source, SourceKind

    material = build_material(headings=headings)
    # 唯一的自变量：闸门。0 = 永不启用（按支检索），极大 = 永远启用（全塞）。
    agent_tools.FULL_TEXT_MATERIAL_CHARS = 10 ** 9 if full_text else 0

    counter = Counter()
    originals = counter.wrap(agent_tools)
    source = Source(id=f"src-{label}", kind=SourceKind.document, status="parsed",
                    filename=f"园区能耗管理办法{'（无标题）' if not headings else ''}.md",
                    text=material)
    request = AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default", sources=[source],
        budget=RunBudget(max_model_calls=12, max_tool_calls=4, max_seconds=1800.0,
                         max_output_tokens=32768, max_repairs=1),
    )
    ctx = RunContext(request=request, sources_by_id={source.id: source})
    registry = agent_tools.build_registry("create", has_sources=True, has_library=False)
    call = ToolCall(call_id=f"measure-{label}", name="mindmap_generate",
                    arguments={"topic": TOPIC, "branches": branches})
    started = time.perf_counter()
    try:
        result = await registry.execute(call, ctx)
    finally:
        for name, original in originals.items():
            setattr(agent_tools, name, original)
    seconds = time.perf_counter() - started
    data = result.data if result.ok else {}
    retrieval = data.get("retrieval") or {}
    slice_report = data.get("section_slice") or {}
    report = {
        "label": label, "ok": result.ok, "content": None if result.ok else result.content,
        "material_chars": len(material), "headings": headings, "full_text": full_text,
        "calls": counter.counts, "retrieval_calls": counter.total(),
        "mode": retrieval.get("mode"),
        "queries": len(retrieval.get("queries") or []),
        "chapters": slice_report.get("chapters", 0),
        "seconds": round(seconds, 1),
        "nodes": data.get("node_count"),
    }
    if result.ok and ctx.candidate is not None:
        report["detail_filled"] = sum(
            1 for node, _ in harness._walk(ctx.candidate.root) if (node.detail or "").strip())
        detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
        report["detail_evidence"] = detail.get("evidence")
    return report


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry", action="store_true", help="只造材料，不调模型")
    parser.add_argument("--branches", type=int, default=4)
    parser.add_argument("--only", default="", help="只跑某一格（A/B/C）")
    parser.add_argument("--out", default="", help="把结果写到这个 JSON")
    args = parser.parse_args(argv)

    cells = (
        ("A", {"headings": False, "full_text": True}),
        ("B", {"headings": True, "full_text": True}),
        ("C", {"headings": True, "full_text": False}),
    )
    if args.dry:
        for name, options in cells:
            text = build_material(headings=options["headings"])
            print(f"{name}: {len(text)} 字、标题 {options['headings']}、"
                  f"全塞 {options['full_text']}｜首行 {text.splitlines()[0][:40]!r}")
        return 0

    harness._fill_credentials()
    from app.agent import tools as agent_tools

    # 章内检索这一格必须开着（它是 B 与 C 里唯一可能发检索的第三段）。
    os.environ[agent_tools.DETAIL_RETRIEVAL_ENV] = "1"
    os.environ["MINDMAP_SECTION_SLICE"] = "1"

    rows: list[dict[str, Any]] = []
    for name, options in cells:
        if args.only and name not in args.only:
            continue
        print(f"== {name}：{'全塞' if options['full_text'] else '按支'}"
              f" · {'有章节' if options['headings'] else '无章节'}", file=sys.stderr)
        row = await run_one(name, **options, branches=args.branches)
        rows.append(row)
        print(f"   检索调用 {row['retrieval_calls']} 次 {row['calls']}｜"
              f"mode={row['mode']}｜queries={row['queries']}｜"
              f"节点 {row['nodes']}｜{row['seconds']}s", file=sys.stderr, flush=True)

    print()
    print("| 格 | 闸门 | 材料 | 章节 | 按支检索 | search_parent | search_multi | "
          "章内检索 | _detail_evidence | 兜底整树 | 合计 | retrieval.mode | 切章 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        counts = row["calls"]
        print(f"| {row['label']} | {'全塞' if row['full_text'] else '按支'} | "
              f"{row['material_chars']} 字 | {'有' if row['headings'] else '无'} | "
              f"{counts['_retrieve_branches']} | {counts['search_parent']} | "
              f"{counts['search_multi']} | {counts['search_exact_blocks']} | "
              f"{counts['_detail_evidence']} | {counts['_retrieve_whole']} | "
              f"{row['retrieval_calls']} | {row['mode']} | {row['chapters']} 章 |")
    if args.out:
        Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
    return 0 if all(row["ok"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
