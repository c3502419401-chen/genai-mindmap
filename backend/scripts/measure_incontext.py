"""量「全塞」与「按支检索」在三个材料长度上的四项指标（真模型）。

    python scripts/measure_incontext.py                       # 三档 × 两条路，各跑一次
    python scripts/measure_incontext.py --lengths 50000       # 只跑一档
    python scripts/measure_incontext.py --modes full          # 只跑一条路
    python scripts/measure_incontext.py --dry                 # 只造材料、不调模型

**要回答的问题**：上下文多长时模型开始"看得到但用不上"？`FULL_TEXT_MATERIAL_CHARS`
那个阈值的唯一依据就是这张表。

材料是**可控的**：同一份制度正文按目标字数扩充（前面的段落逐字相同），三个关键事实
（`KP-OPEN-1187` / `KP-MID-4429` / `KP-END-9032`）分别埋在第一段、正中间那段、最后
一段。主题问的是**中间**那个事实——它是 "lost in the middle" 最容易丢的位置。

四项指标（与四条路一一对应）：

  1. **命中**：三个编号各自有没有出现在产出里（label / detail / 引文都算）。
     中间那条没命中而头尾命中，就是"看得到但用不上"的直接证据。
  2. **引用存活率**：`declared`（模型写了几条）vs `kept`（通过逐字校验留下几条）。
  3. **引用准确性**：自动部分只看"引文是否落在关键事实上"；**语义是否支持那个节点
     的论断必须人工看**——`verify_source_refs` 只查逐字，不查语义。脚本把每条
     `(节点路径, label, detail, quote)` 连同它的来处一起导出，供人读。
  4. **detail 质量**：平均字数、最短、空话命中、平均句数。

材料不带 markdown 标题（`outline` 为空）：两条路的骨架调用输入因此**逐字相同**，
差异只剩"第二步有没有检索"。带标题反而会让 RAG 那档多拿到一份目录，比较就不干净了。

凭据：进程环境里没有时，从用户级环境变量（`HKCU\\Environment`）补上（理由同
`measure_single_call.py`）。**不走代理**：本机代理对 `api.deepseek.com` 不通时，
一次 15 万字的请求会挂在超时上，而报错长得像"模型不支持长上下文"。
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

# 代理对 `api.deepseek.com` 不通（本机实测），所以实验里一个都不留。
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
              "all_proxy", "MINDMAP_EXTERNAL_PROXY")

TOPIC = "数据导出审批：批量导出由谁登记、备案号是多少"

LENGTHS = (10_000, 50_000, 150_000)

# 三个关键事实：编号是**自造的**，模型不可能凭常识猜中，所以"命中"只可能来自读材料。
FACT_OPEN = "KP-OPEN-1187"
FACT_MID = "KP-MID-4429"
FACT_END = "KP-END-9032"

FILLER = ("简要说明", "详见", "可参考", "不再赘述", "不赘述", "见教材", "参见教材",
          "此处略", "从略", "待补充", "略述", "如上所述", "同上")

_DUTIES = (
    "导出申请的受理", "字段级权限的复核", "脱敏规则的执行", "跨境传输的报备",
    "对外提供数据的评估", "日志留存与审计", "账号权限的回收", "共享清单的维护",
    "分级标识的标注", "敏感样本的留存", "接口调用的限额", "离场人员的权限清理",
    "第三方接入的准入", "数据质量巡检", "去标识化效果的验证", "备份介质的销毁",
    "查询行为的异常识别", "批量下载的审批留痕", "分析模型的训练数据准备",
    "外部合作方的定期复评", "数据目录的发布", "访问申请的时效管理",
    "脱敏字典的版本管理", "水印与溯源标记", "样本外发的双人复核",
    "分级变更的重新评估", "导出结果的抽查", "接收方的责任约定",
    "临时授权的到期收回", "数据资产的盘点",
)
_DEPTS = (
    "数据管理部", "信息化办公室", "合规与法务部", "业务运营中心", "内部审计部",
    "信息安全中心", "质量与标准部", "财务共享中心", "人力资源部", "客户服务中心",
    "战略规划部", "风险管理部",
)
_DUTY_EXTRA = (
    "按季度汇总一次台账", "每月核对一次授权清单", "每两周抽查一次调用记录",
    "在半年度审计前完成自查", "按批次登记唯一编号", "随业务流程同步更新状态",
)
_PREFIXES = ("DE", "EX", "AU", "SH", "TR", "GR", "MED", "REF")

# 六个句式模板，**每句都带 `{tag}` 的某个派生**（`tag` / `tag-{days}` / `tag` 加一个
# 段号派生的数字）。这是刻意的：模型抄出的 quote 是"一句完整的话"，只要每句都带唯一
# 编号，任一条 quote 在原文里唯一 → `verify_source_refs` 能唯一定位、不落
# `SOURCE_QUOTE_AMBIGUOUS`。段落重复会把「引用存活率」这项指标污染成"材料重复度"的
# 度量，而不是模型行为。
_TEMPLATES = (
    "第{i}条 关于{duty}的执行口径：{dept}负责的台账以 {tag} 统一编号，{extra}。"
    "涉及{digits}万元以上事项的复核意见记入 {tag}-{days}，由{dept2}在{cycle}内"
    "抽查{ratio}%。{dept3}每年对 {tag} 的整改情况复评一次，结论分为两档。",

    "第{i}条 {dept}在{duty}中承担第一责任，台账以 {tag} 归档，{extra}。"
    "{dept2}每年组织一次 {tag}-{days} 的专项检查，结论分为合规、基本合规、不合规三档。"
    "基本合规的须在{days}个工作日内整改，整改记录与 {tag} 一并留存{cycle}。",

    "第{i}条 {duty}的留痕要求：所有操作必须可追溯到具体账号，流水号形如 "
    "{tag}-{digits}。{dept}发现同一账号在{cycle}内重复执行 {tag} 所述事项超过"
    "{ratio}次的，应当暂停其权限。{extra}，相关材料报{dept2}备案，附 {tag} 的副本。",

    "第{i}条 涉及{duty}的对外约定由{dept}会同{dept2}拟订，{extra}。"
    "约定文本须写明用途、范围与保存期限，编号为 {tag}。"
    "接收方变更的，须在{days}个工作日内按 {tag}-{digits} 重新签署，否则按原约定失效处理。",

    "第{i}条 {dept}应当建立{duty}的负面清单，{extra}，清单版本号随 {tag} 递增。"
    "{tag}-{days} 项下的情形不得例外审批；例外由{dept3}主要负责人签批 {tag}-{i}。"
    "签批材料附 {tag}-{ratio}，并在{cycle}内向{dept2}报备一次。",

    "第{i}条 关于{duty}的培训与考核：{dept}每年至少组织{ratio}次专项培训，{extra}。"
    "考核不通过的账号在{days}个工作日内不得执行 {tag} 项下事项，复考通过后恢复。"
    "复考记录编入 {tag}-{digits}，{dept2}对覆盖率低于{digits}成的部门予以通报。",
)
_EXTRA_TAIL = (
    "相关记录保存{digits}年", "清单每{cycle}更新一次", "台账由两人分别保管",
    "变更须留书面痕迹", "纸质与电子两套并行", "统计口径以系统导出为准",
)
_CYCLES = ("月", "季度", "半年", "年度", "两周", "三周")


def _fill_text(index: int) -> str:
    """第 `index` 段填充正文（约 200–260 字）。每段唯一。"""
    template = _TEMPLATES[index % len(_TEMPLATES)]
    tag = f"{_PREFIXES[index % len(_PREFIXES)]}-{index:04d}"
    return template.format(
        i=index,
        duty=_DUTIES[index % len(_DUTIES)],
        dept=_DEPTS[index % len(_DEPTS)],
        dept2=_DEPTS[(index + 3) % len(_DEPTS)],
        dept3=_DEPTS[(index + 7) % len(_DEPTS)],
        extra=_EXTRA_TAIL[index % len(_EXTRA_TAIL)].format(
            digits=2 + index % 4, cycle=_CYCLES[index % len(_CYCLES)]),
        tag=tag,
        digits=2 + index % 7,
        days=3 + index % 9,
        cycle=_CYCLES[index % len(_CYCLES)],
        ratio=10 + index % 40,
    )


def _fact_open() -> str:
    return (
        f"第0条 本管理办法依据 {FACT_OPEN} 号批复文件制定，适用于本单位全部业务系统的"
        "数据导出行为。文件明确要求：凡涉及客户信息、经营数据与技术资料的对外提供，"
        "一律先分级、后审批、再留痕，任何岗位不得以口头同意替代书面审批。"
        f"批复文件 {FACT_OPEN} 同时废止了此前关于数据外发的三份临时通知。"
    )


def _fact_mid() -> str:
    return (
        "本条为全篇的执行关键：**批量导出**（单次超过一万条记录，或单次超过五百兆字节）"
        "的申请，由**数据安全与合规委员会**统一登记，登记备案号为 "
        f"{FACT_MID}，未经该备案号登记的导出行为一律视为违规。"
        "登记时须同时提交导出目的、接收方、字段清单与保存期限四项要素；"
        f"备案号 {FACT_MID} 的有效期为三十个自然日，逾期须重新登记。"
        "委员会每月将登记情况汇总通报信息化办公室与内部审计部。"
    )


def _fact_end() -> str:
    return (
        "附则：本办法的修订须经数据治理专题会议审议，并以会议纪要形式确认；"
        f"现行版本的修订依据为 {FACT_END} 号会议纪要。本办法自发布之日起施行，"
        "由数据管理部负责解释。此前发布的实施细则与本办法不一致的，以本办法为准。"
        f"会议纪要 {FACT_END} 同时要求各业务部门在两个月内完成本部门细则的对齐。"
    )


def build_material(target_chars: int) -> tuple[str, dict]:
    """按目标字数造一份材料：三个关键事实固定在开头 / 正中 / 结尾。

    段落长度不齐（200–260 字），所以"正中"按**字符位置**算而不是按段号算：段号正中
    在段落长度不均匀时会偏出几段，那几段正是这个实验最敏感的位置。
    """
    probe = _fill_text(1)
    per_paragraph = len(probe)
    count = max(9, round(target_chars / per_paragraph))
    paragraphs = [_fill_text(index) for index in range(count)]
    opening, middle, closing = _fact_open(), _fact_mid(), _fact_end()
    # 三个事实段替换掉同位置的填充段：0 / 正中 / 末尾。
    paragraphs[0] = opening
    paragraphs[count // 2] = middle
    paragraphs[count - 1] = closing
    text = "\n\n".join(paragraphs) + "\n"
    middle_at = text.find(middle)
    facts = {
        "open": FACT_OPEN, "mid": FACT_MID, "end": FACT_END,
        "chars": len(text),
        # 中间那条事实的**字符位置占比**：0.5 左右才说明它真的在正中。
        "mid_position": round(middle_at / max(1, len(text)), 3),
    }
    return text, facts


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
    for name in PROXY_VARS:
        os.environ.pop(name, None)
    # 实验必须本地可复现：向量路、外部 rerank、查询扩展、联网检索全部关掉。
    # 关掉向量路让"检索"这一档退成纯 BM25——那正是没有 embedding 凭证时的生产行为，
    # 也把"这次差异是不是 embedding 服务抖了"这个变量从表里去掉。
    os.environ["MINDMAP_HYBRID_SEARCH"] = "0"
    os.environ["MINDMAP_RERANK"] = "0"
    os.environ["MINDMAP_QUERY_EXPANSION"] = "0"
    os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "0"


def _walk(node, path=()):
    yield node, path
    for child in node.children:
        yield from _walk(child, (*path, node.label))


def _hits(text: str, needle: str) -> bool:
    return bool(needle) and needle in (text or "")


def _measure(content, result, elapsed: float, facts: dict) -> dict:
    """一棵树 + 一次 `ToolResult` → 四项指标。"""
    from app.agent.tools import DETAIL_FILL_FLOOR

    nodes = [(node, path) for node, path in _walk(content.root)]
    labels = "\n".join(node.label or "" for node, _ in nodes)
    details = "\n".join(node.detail or "" for node, _ in nodes)
    quotes = "\n".join(
        (ref.quote or "") for node, _ in nodes for ref in node.source_refs)
    body = "\n".join((labels, details, quotes))

    lengths = [len((node.detail or "").strip()) for node, _ in nodes if node is not content.root]
    sentences = [s.count("。") + s.count("；") for node, _ in nodes
                 if node is not content.root
                 for s in [(node.detail or "")]]
    filler_nodes = [
        node.label for node, _ in nodes
        if any(word in (node.detail or "") for word in FILLER)
    ]
    # 引用样本：人工复核用。`from_fact` 标出这条引文抄的是不是关键事实那三段之一。
    samples = []
    for node, path in nodes:
        for ref in node.source_refs:
            quote = (ref.quote or "").strip()
            samples.append({
                "path": " / ".join(path) or "中心主题",
                "label": node.label,
                "detail": (node.detail or "").strip(),
                "quote": quote,
                "source_id": ref.source_id,
                "from_fact": next(
                    (name for name, fact in (("open", facts["open"]), ("mid", facts["mid"]),
                                            ("end", facts["end"]))
                     if fact in quote), None),
            })
    refs = result.data.get("source_refs") or {}
    declared, kept = refs.get("declared", 0), refs.get("kept", 0)
    return {
        "seconds": round(elapsed, 2),
        "nodes": len(nodes),
        "branches": [child.label for child in content.root.children],
        # 1. 命中：三个编号各自有没有出现在产出里（标题 / 说明 / 引文都算）。
        "hit_open": _hits(body, facts["open"]),
        "hit_mid": _hits(body, facts["mid"]),
        "hit_end": _hits(body, facts["end"]),
        "hit_mid_in_label": _hits(labels, facts["mid"]),
        "hit_mid_in_detail": _hits(details, facts["mid"]),
        "hit_mid_in_quote": _hits(quotes, facts["mid"]),
        # 2. 引用存活率
        "declared": declared,
        "kept": kept,
        "survival": round(kept / declared, 3) if declared else None,
        "nodes_with_refs": sum(1 for node, _ in nodes if node.source_refs),
        "ref_density": round(
            sum(1 for node, _ in nodes if node.source_refs) / max(1, len(nodes)), 3),
        # 3. 引用准确性（自动那一半）：引文落在关键事实上的条数，以及引文长度分布。
        "quotes_on_facts": sum(1 for item in samples if item["from_fact"]),
        "quote_avg_chars": round(
            sum(len(item["quote"]) for item in samples) / max(1, len(samples)), 1),
        # 4. detail 质量
        "detail_avg": round(sum(lengths) / max(1, len(lengths)), 1),
        "detail_min": min(lengths, default=0),
        "detail_max": max(lengths, default=0),
        "detail_sentences": round(sum(sentences) / max(1, len(sentences)), 1),
        "detail_under_floor": sum(1 for value in lengths if value < DETAIL_FILL_FLOOR),
        "filler_nodes": filler_nodes,
        "retrieval": result.data.get("retrieval"),
        "material": result.data.get("material"),
        "calls": result.data.get("calls"),
        "diagnostics": sorted({item["code"] for item in result.data.get("diagnostics", [])}),
        "samples": samples,
    }


async def _run_one(material: str, facts: dict, mode: str, branches: int,
                   raw_dir: Path | None = None) -> dict:
    """跑一次 `mindmap_generate`。`mode` 只切换全塞的闸，别的输入逐字相同。"""
    from app.agent import tools as agent_tools
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
    from app.contracts.source import Source, SourceKind

    # 两条路只差这一个数：0 = 永不启用（按支检索），极大 = 永远启用（全塞）。
    agent_tools.FULL_TEXT_MATERIAL_CHARS = 0 if mode == "rag" else 10 ** 9

    # `--raw`：把每一次模型调用的 system / user / 原始返回落盘。引用"一条都没写"
    # 与"写了但被校验剔掉"在产出上长得完全一样，分不开就不知道该改提示词还是改代码。
    if raw_dir is not None:
        original = agent_tools._chat
        seen: list[dict] = []

        async def recorded(ctx, system, user, **kwargs):
            response = await original(ctx, system, user, **kwargs)
            seen.append({"system": system, "user": user,
                         "response": response if isinstance(response, (dict, list)) else str(response)})
            (raw_dir / f"raw-{mode}-{len(seen)}.json").write_text(
                json.dumps(seen[-1], ensure_ascii=False, indent=1), encoding="utf-8")
            return response

        agent_tools._chat = recorded
    else:
        recorded = None

    source = Source(id=f"src-{mode}", kind=SourceKind.document, status="parsed",
                    filename="数据分级与导出审批管理办法.md", text=material)
    sources = [source]
    request = AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default", sources=sources,
        budget=RunBudget(max_model_calls=12, max_tool_calls=4, max_seconds=1800.0,
                         max_output_tokens=32768, max_repairs=1),
    )
    ctx = RunContext(request=request,
                     sources_by_id={item.id: item for item in sources})
    registry = agent_tools.build_registry("create", has_sources=True, has_library=False)
    call = ToolCall(call_id=f"measure-{mode}", name="mindmap_generate",
                    arguments={"topic": TOPIC, "branches": branches})
    started = time.perf_counter()
    try:
        result = await registry.execute(call, ctx)
    finally:
        if recorded is not None:
            agent_tools._chat = original
    elapsed = time.perf_counter() - started
    if not result.ok:
        return {"ok": False, "mode": mode, "content": result.content,
                "seconds": round(elapsed, 2)}
    report = _measure(ctx.candidate, result, elapsed, facts)
    report.update({"ok": True, "mode": mode, "material_chars": facts["chars"],
                   "tree": ctx.candidate.model_dump()})
    return report


def _report(outdir: Path) -> str:
    """把目录里已有的结果汇成一张 markdown 表——四个指标各几行。

    表就是这次实验的答案，所以它由脚本生成而不是手抄：手抄的那一版一旦代码或提示词
    再改一次就没人重算，而这张表的作用恰恰是"每次改动之后重跑同一组数字"。
    """
    rows = sorted(
        (json.loads(path.read_text(encoding="utf-8")) for path in outdir.glob("*.json")),
        key=lambda row: (row.get("material_chars") or 0, row.get("mode") or ""))
    rows = [row for row in rows if row.get("ok")]
    if not rows:
        return "（目录里没有结果）"
    order = {"rag": "检索", "full": "全塞"}

    def cell(value) -> str:
        if value is None:
            return "—"
        if isinstance(value, bool):
            return "✓" if value else "✗"
        return str(value)

    lines = [
        "| 材料字数 | 路 | 节点 | 命中 开/中/尾 | 引用 kept/declared | 存活率 | "
        "引用节点 | 落事实引文 | quote 均长 | detail 均/最短/最长 | 句数 | <40 字 | 空话 | 耗时 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append("| {chars} | {mode} | {nodes} | {hits} | {kept}/{declared} | {survival} "
                     "| {nodes_with_refs} | {quotes_on_facts} | {quote_avg_chars} "
                     "| {detail_avg}/{detail_min}/{detail_max} | {detail_sentences} "
                     "| {detail_under_floor} | {filler} | {seconds}s |".format(
                         chars=row.get("material_chars"),
                         mode=order.get(row.get("mode"), row.get("mode")),
                         nodes=row.get("nodes"),
                         hits="/".join(cell(row.get(key)) for key in
                                       ("hit_open", "hit_mid", "hit_end")),
                         kept=row.get("kept"), declared=row.get("declared"),
                         survival=cell(row.get("survival")),
                         nodes_with_refs=row.get("nodes_with_refs"),
                         quotes_on_facts=row.get("quotes_on_facts"),
                         quote_avg_chars=row.get("quote_avg_chars"),
                         detail_avg=row.get("detail_avg"),
                         detail_min=row.get("detail_min"),
                         detail_max=row.get("detail_max"),
                         detail_sentences=row.get("detail_sentences"),
                         detail_under_floor=row.get("detail_under_floor"),
                         filler=len(row.get("filler_nodes") or []),
                         seconds=row.get("seconds")))
    return "\n".join(lines)


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lengths", default="", help="逗号分隔，如 10000,50000,150000")
    parser.add_argument("--modes", default="rag,full", help="rag,full")
    parser.add_argument("--branches", type=int, default=8)
    parser.add_argument("--dry", action="store_true", help="只造材料、不调模型")
    parser.add_argument("--raw", action="store_true",
                        help="把每次模型调用的 system/user/原始返回落到 <out>/raw/")
    parser.add_argument("--tag", default="",
                        help="落盘文件名后缀（`--tag=-2` 用来存第二次采样，不覆盖第一次）")
    parser.add_argument("--report", action="store_true",
                        help="不跑模型，只把 <out> 里已有的 JSON 汇总成表")
    parser.add_argument("--out", default="data/measure_incontext",
                        help="结果目录（每个 长度×路 一个 JSON）")
    args = parser.parse_args(argv)

    if args.report:
        print(_report(Path(args.out)))
        return 0

    lengths = [int(item) for item in args.lengths.split(",") if item.strip()] or list(LENGTHS)
    modes = [item.strip() for item in args.modes.split(",") if item.strip()]
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    raw_dir = outdir / "raw" if args.raw else None
    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)

    if not args.dry:
        _fill_credentials()
        if not os.environ.get("MINDMAP_LLM_API_KEY"):
            print("缺少 MINDMAP_LLM_API_KEY（进程与用户级环境都没有）", file=sys.stderr)
            return 2

    rows: list[dict] = []
    for target in lengths:
        material, facts = build_material(target)
        print(f"== 目标 {target} 字 → 实际 {facts['chars']} 字，"
              f"中间事实位置 {facts['mid_position']}", file=sys.stderr)
        if args.dry:
            continue
        for mode in modes:
            print(f"   · {mode} 跑起来…", file=sys.stderr, flush=True)
            report = await _run_one(material, facts, mode, args.branches, raw_dir)
            rows.append(report)
            path = outdir / f"{facts['chars']}-{mode}{args.tag}.json"
            path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
            if not report["ok"]:
                print(f"   × {mode} 失败：{report['content']}", file=sys.stderr)
                continue
            print(f"   ✓ {mode}：命中 中={report['hit_mid']} "
                  f"开={report['hit_open']} 尾={report['hit_end']}｜"
                  f"引用 {report['kept']}/{report['declared']}｜"
                  f"detail {report['detail_avg']} 字｜{report['seconds']}s",
                  file=sys.stderr, flush=True)

    if args.dry:
        return 0
    print(json.dumps([
        {key: value for key, value in row.items() if key not in ("samples", "tree")}
        for row in rows], ensure_ascii=False, indent=1))
    return 0 if all(row.get("ok") for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
