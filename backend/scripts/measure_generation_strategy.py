"""生成策略这一轮的实测：三批 × 每批 2 次，六个指标。

跑的是**真模型**（DeepSeek，凭据取自用户级环境变量），走的是生产那条路
（`mindmap_generate` 整树一次生成），与界面上点一次"生成"的代码完全一致。

三批：

    薄批  `filename like '%碳中和%'`（4 份 / 673 字）
    厚批  docs 那四份（66,536 字）
    纯主题  不给材料（走 B+C：`DETAIL_NO_SOURCE_RULES` + `MINDMAP_VERIFY_AFTER_GENERATE`）

六个指标（每个都从**产出本身**算，不看提示词）：

    ① 引用覆盖率   有至少一条 source_refs 的节点 / 全部节点      ★ 核心
    ② 分支数 / 扇出  一级分支数、扇出直方图、有没有分叉
    ③ 节点数
    ④ 折叠 / 裁剪  `folded_rollups` / `folded_nodes` / `cut_nodes` + 聚合节点样本
    ⑤ 耗时
    ⑥ token（输入/输出，取自 `data["tokens"]`，没有就不报）

用法：

    python scripts/measure_generation_strategy.py --batch thin --repeat 2
    python scripts/measure_generation_strategy.py --batch thick --repeat 2
    python scripts/measure_generation_strategy.py --batch topic --repeat 2
    python scripts/measure_generation_strategy.py --batch all --repeat 2

一条纪律：**不碰用户的库**。`MINDMAP_DB_PATH` 指到 `data/generation_strategy.db`，
开跑前删、跑完留着（报告要能复现；要清掉就自己删那个文件）。
"""
# requires-optional: winreg
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DB = BACKEND / "data" / "generation_strategy.db"

#: 每一次模型调用的记录（`_instrument_chat` 往里写）。失败时它随行返回——
#: 「模型返回非 JSON」有两种来路，只有这两个能分开：输出被 `finish_reason=length`
#: 截断（参数缺一半），还是输出完整但跑了格式。两者的修法完全不同（调 max_tokens
#: vs 调提示词/条数），不分开就只能瞎猜。
_CALLS: list[dict] = []

#: 本机代理。实测（用户那一轮）走它维基 / openalex / crossref 都是 200，直连超时。
PROXY = "http://127.0.0.1:7897"

#: 材料的快照，**在切库之前**读好（见 `_snapshot_library` 里那段）。
_LIBRARY: list = []

#: 与 `run-backend.bat` 同一批键（那里设的、用户级环境变量里也有一份）。
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


def _proxy_alive(url: str, timeout: float = 1.0) -> bool:
    """代理端口通不通。**1 秒、只发 TCP 连接**，不发请求。

    为什么要测：这个脚本原来把 `HTTP_PROXY` 写死成 `127.0.0.1:7897`（注释里那句
    "实测走它 200"是当时的事实）。而代理是**人来开关的**——它一关，httpx 里每一个
    请求（包括 DeepSeek 的）都会撞上 `ConnectError`，整批 10 轮在 4.8 秒内全红，
    报错还长得像"模型服务不可用"。实测踩过一次：两臂各 10 轮 20 次全失败、
    每轮 4.5–4.9 秒、`failure_kind=no_call`（一次响应都没回来）。
    """
    import socket
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    host, port = parsed.hostname, parsed.port or 80
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _prepare() -> None:
    for name in CREDENTIALS:
        if not os.environ.get(name):
            value = _user_env(name)
            if value:
                os.environ[name] = value
    # 与运行中的后端同一套开关（用户给的那张表）。逐个显式设，不读 .env——
    # 实测要能复现，而"这次到底开没开"必须在这份文件里读得到。
    for name in (
        "MINDMAP_SECTION_SLICE", "MINDMAP_GRAPH_INGEST", "MINDMAP_GRAPH_OUTLINE",
        "MINDMAP_DETAIL_RETRIEVAL", "MINDMAP_NODE_SCALE", "MINDMAP_DETAIL_SCALE",
        "MINDMAP_MIXED_SKELETON", "MINDMAP_VERIFY_AFTER_GENERATE",
        "MINDMAP_OPTIMIZATION_CROSS_MAP", "MINDMAP_EXTERNAL_DEEPSEEK_SEARCH",
    ):
        os.environ[name] = "1"
    # 本机代理：**通才设**。它只对外部检索（维基 / openalex / crossref）有用，而
    # `thick`/`thin` 两批材料都在库里、不搜外网——代理一关还被写死进环境，就把
    # DeepSeek 与 embedding 一起带进 ConnectError（见 `_proxy_alive` 的 docstring）。
    proxy_names = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    if _proxy_alive(PROXY):
        for name in proxy_names:
            os.environ[name] = PROXY
        print(f"代理 {PROXY} 可用，已设", flush=True)
    else:
        for name in proxy_names:
            os.environ.pop(name, None)
        print(f"代理 {PROXY} 不可用（端口通不了）→ 本批直连，外部检索会失效",
              flush=True)


def _snapshot_library(db_path: Path) -> None:
    """把**用户真实库**里的材料读进内存，然后才把库切到本脚本的临时文件。

    顺序不能反：`MINDMAP_DB_PATH` 一改，`library.recent()` 读到的就是那个空的临时库
    （实测第一次跑就是这么得到 0 份材料的）。材料一旦读进内存，后面全链路用的都是
    `ctx.sources` 里的 `Source` 对象，检索也直接打这些正文——不再回头读库。

    `db_path` 由调用方按"臂 + 批次"命名：原来固定一个文件名，两臂并行会互相
    `unlink` 对方的库（实测过一次——后启动的那个把先启动的库删了）。A/B 要能并行跑，
    这一条必须先解决。
    """
    global _LIBRARY
    from app.knowledge import library as knowledge_library

    _LIBRARY = knowledge_library.recent(limit=400)
    os.environ["MINDMAP_DB_PATH"] = str(db_path)


def _instrument_chat() -> None:
    """插桩两个点，回答一个具体问题：**那几次失败是不是输出被 max_tokens 截断**。

    为什么需要两个点（单点不够，实测过）：

      - `tools._chat` 是工具内部唯一的模型出口（`tests/test_model_exit_single_path.py`
        用 AST 钉着），但它返回的是**已经解析好的 JSON**，不是 raw response——
        从那里拿不到 `finish_reason` 与 `usage`（第一次插桩就是这样，字段全 None）。
        它只用来记 `prompt_chars`（"prompt 有没有变长"）。
      - `generation.service._choice_message(body)` 是**一次性路径**的必经点，从 `body`
        里能同时拿到 `choices[0].finish_reason`、`message.content` 长度与 `usage`。
        流式路径（`on_delta` 非空时）不经过它，所以响应记录可能少于 prompt 记录——
        那一条差额本身也是信息（这次走了流式）。

    分类口径（写进 `docs/RETRIEVAL-KEEP.md` §4.2）：

      - `truncated`：出现过 `finish_reason=length`，且这一轮最终失败 → max_tokens 的
        问题，不是 keep 的问题（修法是调 max_tokens / 少要节点）；
      - `format`：所有调用都是 `stop` 但最终失败 → 输出完整、跑了格式（修法是提示词
        或条数）；
      - `no_call`：一次响应都没记到 → 插桩没覆盖到，不能下结论。
    """
    from app.agent import tools as agent_tools
    from app.generation import service as generation_service

    original_chat = agent_tools._chat
    original_choice = generation_service._choice_message

    async def wrapped_chat(ctx, system, user, **kwargs):
        raw = await original_chat(ctx, system, user, **kwargs)
        _CALLS.append({"kind": "prompt", "prompt_chars": len(system) + len(user)})
        return raw

    def wrapped_choice(body):
        try:
            choice = (body.get("choices") or [{}])[0] or {}
            message = choice.get("message") or {}
            content = message.get("content") or ""
            usage = body.get("usage") or {}
            _CALLS.append({
                "kind": "response",
                "finish": choice.get("finish_reason"),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "content_chars": len(content),
                "content_tail": content[-160:],
                "tool_calls": len(message.get("tool_calls") or []),
            })
        except Exception:  # noqa: BLE001 — 插桩永不改变被测行为
            pass
        return original_choice(body)

    agent_tools._chat = wrapped_chat
    generation_service._choice_message = wrapped_choice


def _load_batch(batch: str) -> list:
    """从快照里挑这一批的材料。"""
    everything = _LIBRARY
    if batch == "thin":
        picked = [item for item in everything if "碳中和" in (item.filename or "")]
        return picked[:4]
    if batch == "thick":
        # docs 那四份，合计 66,536 字（26265 + 20017 + 14120 + 6134）——超过
        # `FULL_TEXT_MATERIAL_CHARS=50_000`，所以这一批走**按支检索**那条引用规约
        # （`BRANCHED_EVIDENCE_RULES`），而不是全塞。这一点必须在报告里说清：
        # 两批走的不是同一段提示词。
        wanted = {"AGENT_PLAN.md", "PLAN.md", "RAG-DECISIONS.md", "ARCHITECTURE.md"}
        return [item for item in everything if (item.filename or "") in wanted]
    return []


def _fanout(root) -> Counter:
    """每个节点的子节点个数 → 计数。扇出 0 = 叶子。"""
    out: Counter = Counter()
    stack = [root]
    while stack:
        node = stack.pop()
        out[len(node.children)] += 1
        stack.extend(node.children)
    return out


def _count_refs(root) -> dict[str, int]:
    """引用覆盖率，**两个分母**。

    `node_count` 那个分母（全部节点）会把结构层稀释掉：骨架/章节那一层**结构上不写
    说明、因此不可能有引用**（早前实测过），它们进分母等于给一条与检索无关的常量打折。
    所以另给一个**可引用**分母：

        可引用节点 = `detail` 非空 的节点

    引用是给"说明"这一段挂的出处（`source_refs` 挂在节点上、`detail` 是它支撑的那段
    文字），没有说明的节点没有可引用的内容。这与"每个节点都必须有引用"那条规约要分清：
    规约要求的是"写了说明就要说出处"，它没说"结构层也要写出处"。
    """
    total = with_refs = citable = citable_with_refs = 0
    stack = [root]
    while stack:
        node = stack.pop()
        total += 1
        has_refs = bool(node.source_refs)
        with_refs += 1 if has_refs else 0
        if (node.detail or "").strip():
            citable += 1
            citable_with_refs += 1 if has_refs else 0
        stack.extend(node.children)
    return {
        "node_count": total,
        "with_refs": with_refs,
        "citable": citable,
        "citable_with_refs": citable_with_refs,
        "ref_coverage": round(with_refs / max(1, total), 3),
        "citable_coverage": round(citable_with_refs / max(1, citable), 3),
    }


def _by_depth(root) -> dict[int, dict]:
    """逐层的 (节点数, 有引用数, 可引用数)。

    这一列是"每个节点必须有引用"那条规则**会不会惩罚深度**的唯一证据：深层节点离原文
    更远，逐字引更难。如果第 3 层以下的有引用率断崖，那这条规则实际上在惩罚深度。
    「可引用」与 `_count_refs` 同口径（`detail` 非空）——两个口径必须同源，否则
    "总数涨了但覆盖率跌了"会无法归因。
    """
    out: dict[int, dict] = {}
    stack: list[tuple[object, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        bucket = out.setdefault(depth, {"nodes": 0, "with_refs": 0, "citable": 0,
                                        "citable_with_refs": 0})
        bucket["nodes"] += 1
        bucket["with_refs"] += 1 if node.source_refs else 0
        if (node.detail or "").strip():
            bucket["citable"] += 1
            bucket["citable_with_refs"] += 1 if node.source_refs else 0
        stack.extend((child, depth + 1) for child in node.children)
    for bucket in out.values():
        bucket["coverage"] = round(bucket["with_refs"] / max(1, bucket["nodes"]), 3)
        bucket["citable_coverage"] = round(
            bucket["citable_with_refs"] / max(1, bucket["citable"]), 3)
    return dict(sorted(out.items()))


def _rollup_samples(root) -> list[dict]:
    """产出里的聚合节点：label 是「其他 N 项」的那些。"""
    found = []
    stack = [root]
    while stack:
        node = stack.pop()
        if (node.label or "").startswith("其他 ") and (node.label or "").endswith(" 项"):
            found.append({
                "label": node.label,
                "detail": (node.detail or "")[:120],
                "refs": len(node.source_refs),
                "expandable": node.expandable,
            })
        stack.extend(node.children)
    return found


def _run_once(batch: str, index: int) -> dict:
    from app.agent import tools as agent_tools
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall

    _CALLS.clear()
    sources = _load_batch(batch)
    topic = {
        "thin": "碳中和的主要方面",
        "thick": "生成流水线的设计与实测",
        "topic": "机器学习的主要方向",
    }[batch]
    request = AgentRunRequest(
        goal=topic, intent="create", thinking_model="default", sources=sources,
        budget=RunBudget(max_model_calls=60, max_tool_calls=8, max_seconds=900.0,
                         max_output_tokens=32768, max_repairs=1),
    )
    ctx = RunContext(request=request,
                     sources_by_id={source.id: source for source in sources})
    registry = agent_tools.build_registry(
        "create", has_sources=bool(sources), has_library=bool(sources))
    started = time.perf_counter()
    result = asyncio.run(registry.execute(
        ToolCall(call_id=f"m{index}", name="mindmap_generate",
                 arguments={"topic": topic, "diagram_type": "tree"}), ctx))
    seconds = round(time.perf_counter() - started, 1)

    row: dict = {
        "batch": batch, "run": index, "ok": bool(result.ok),
        "seconds": seconds,
        "material_chars": sum(len(item.text or "") for item in sources),
        "sources": len(sources),
        # 只留响应记录 + prompt 的最大字数：一次生成有 5–27 次调用，把两类原始记录
        # 全存进去会让 10 轮的 JSON 涨到几 MB，而判读只需要这几列。
        "calls": [call for call in _CALLS if call.get("kind") == "response"],
        "prompt_chars_max": max((call.get("prompt_chars", 0)
                                 for call in _CALLS
                                 if call.get("kind") == "prompt"), default=0),
    }
    responses = row["calls"]
    # 每一轮都记 token 峰值与 finish 集合——**成功轮也要**：用户要的是"失败那几轮
    # 是不是顶满"与"成功那几轮长什么样"的对照，只记失败就无从对照。
    row["completion_max"] = max((call.get("completion_tokens") or 0
                                 for call in responses), default=0)
    row["finish_set"] = sorted({str(call.get("finish")) for call in responses})
    if not result.ok:
        row["error"] = result.content[:300]
        # 失败分类（`docs/RETRIEVAL-KEEP.md` §4.2）：`length` = 输出被 max_tokens
        # 截断；`stop` + content 不是 JSON = 输出完整但跑了格式。两者修法不同。
        truncated = any(call.get("finish") == "length" for call in responses)
        row["failure_kind"] = ("truncated" if truncated
                              else "format" if responses else "no_call")
        return row
    tree = ctx.candidate
    if tree is None:
        row["ok"] = False
        row["error"] = "工具返回 ok 但没有产出树"
        return row
    root = tree.root
    stats = _count_refs(root)
    data = result.data or {}
    fanout = _fanout(root)
    row.update({
        "node_count": stats["node_count"],
        "branches": len(root.children),
        "with_refs": stats["with_refs"],
        "ref_coverage": stats["ref_coverage"],
        "citable": stats["citable"],
        "citable_with_refs": stats["citable_with_refs"],
        "citable_coverage": stats["citable_coverage"],
        "refs_total": data.get("source_refs", {}).get("kept"),
        "fanout": dict(sorted(fanout.items())),
        "by_depth": _by_depth(root),
        "forked_parents": sum(count for size, count in fanout.items() if size >= 2),
        "max_depth": _max_depth(root),
        "folded_rollups": data.get("folded_rollups", 0),
        "folded_nodes": data.get("folded_nodes", 0),
        "cut_nodes": data.get("cut_nodes", 0),
        "node_limit": data.get("node_limit"),
        "partial": data.get("partial"),
        "rollups_in_tree": _rollup_samples(root),
        "verify": data.get("verify"),
        "diagnostic_codes": [item["code"] for item in data.get("diagnostics", [])],
        "summary": (result.content or "")[:400].replace("\n", " "),
    })
    return row


def _max_depth(root, depth: int = 1) -> int:
    if not root.children:
        return depth
    return max(_max_depth(child, depth + 1) for child in root.children)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", default="all",
                        choices=("thin", "thick", "topic", "all"))
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument(
        "--resume", action="store_true",
        help=("沿用同名的明细文件，跳过已经跑过的 (批次, 轮次)。10 轮是一次 100 分钟的"
              "长跑，中途被中断后不该从第 1 轮重来——每一轮都落盘，这一项就是接着往下跑"))
    parser.add_argument("--topic", default="", help="覆盖纯主题那一批的主题")
    parser.add_argument(
        "--keep", default="on", choices=("on", "off"),
        help=("按分数取数（`tools.KEEP_BY_SCORE_ENV`）的 A/B 臂。`off` = 回到"
              "\"每支取排序后前 3 条\"那个旧口径。两臂唯一的差别就是这一个环境变量，"
              "所以两行报告可以直接相减（见 `docs/RETRIEVAL-KEEP.md` 第 4 节）"))
    parser.add_argument(
        "--no-external", action="store_true",
        help=("关掉外部检索（`MINDMAP_EXTERNAL_DEEPSEEK_SEARCH`）。纯主题那一批**必须**"
              "带这个开关才测得到 B：外部检索会先把'没有材料'变成'8 份 / 22,744 字'，"
              "于是 `sources` 非空、`DETAIL_NO_SOURCE_RULES` 那句根本不进提示词"))
    args = parser.parse_args()

    _prepare()
    from app.agent.tools import KEEP_BY_SCORE_ENV

    os.environ[KEEP_BY_SCORE_ENV] = "1" if args.keep == "on" else "0"
    if args.no_external:
        os.environ["MINDMAP_EXTERNAL_DEEPSEEK_SEARCH"] = ""
    if not os.environ.get("MINDMAP_LLM_API_KEY"):
        print("缺 MINDMAP_LLM_API_KEY（用户级环境变量里也没有）", file=sys.stderr)
        return 2
    # 库按"臂 + 批次"分文件：两臂并行时共用一个文件名会互相 `unlink`（实测过一次）。
    db_path = BACKEND / "data" / f"generation-strategy-{args.keep}-{args.batch}.db"
    db_path.unlink(missing_ok=True)
    _snapshot_library(db_path)
    _instrument_chat()
    print(f"材料快照 {len(_LIBRARY)} 份；库 {db_path.name}", flush=True)

    batches = ("thin", "thick", "topic") if args.batch == "all" else (args.batch,)
    out = BACKEND / "data" / f"generation-strategy-keep-{args.keep}-{args.batch}.json"
    rows: list[dict] = []
    done: set[tuple[str, int]] = set()
    if args.resume and out.exists():
        rows = json.loads(out.read_text(encoding="utf-8"))
        done = {(row.get("batch"), row.get("run")) for row in rows}
        print(f"续跑：已有 {len(rows)} 轮（{sorted(done)}）", flush=True)

    def save() -> None:
        """**每一轮都落盘**。10 轮 × 10 分钟是一次长跑，中途被挂掉不能再白跑一遍
        （实测过一次：整段跑完前被中断，产物一个字都没有）。"""
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    for batch in batches:
        for index in range(1, args.repeat + 1):
            if (batch, index) in done:
                print(f"… {batch} #{index} 已有，跳过", flush=True)
                continue
            print(f"… {batch} #{index}", flush=True)
            try:
                row = _run_once(batch, index)
            except Exception as exc:                    # noqa: BLE001 — 一批失败不该中断报告
                row = {"batch": batch, "run": index, "ok": False,
                       "error": f"{type(exc).__name__}: {exc}"}
            rows.append(row)
            save()
            print(json.dumps({key: row.get(key) for key in (
                "ok", "seconds", "node_count", "branches", "ref_coverage",
                "citable", "citable_coverage", "completion_max", "failure_kind",
                "error")}, ensure_ascii=False), flush=True)

    print(f"\n明细：{out}")

    print("\n批次    次  节点  分支  有引用/总   覆盖率  可引用  可引用覆盖率  裁剪  耗时")
    for row in rows:
        if not row.get("ok"):
            print(f"{row['batch']:<6} {row['run']:>2}  FAILED  {row.get('error', '')[:60]}")
            continue
        print(f"{row['batch']:<6} {row['run']:>2} {row['node_count']:>5} "
              f"{row['branches']:>5} {row['with_refs']:>4}/{row['node_count']:<4} "
              f"{row['ref_coverage']:>6.1%} {row['citable']:>6} "
              f"{row['citable_coverage']:>13.1%} {row['cut_nodes']:>5} "
              f"{row['seconds']:>5}s")

    _report_aggregate(rows, args.keep)
    return 0


def _report_aggregate(rows: list[dict], keep: str) -> None:
    """多轮汇总：**均值、极差、失败率**，不是单次。

    判据要提前定死（`docs/RETRIEVAL-KEEP.md` §4.1）：`repeat=1` 时两臂的差里混着"这次
    树长得大不大"，`repeat=3` 之后看两件事——

      1. 可引用覆盖率的均值差还在不在（厚批那 +5.9pp）；
      2. 两臂各自的极差有多大。**极差大于均值差**时，这个差别不该归因于改动。

    `repeat=10` 之后再加第三件：**失败率**。它是"交不出树"，代价比覆盖率低几个 pp 高
    一个量级，所以要单独一列，并按时长把 `truncated`（输出被 max_tokens 截断）与
    `format`（输出完整但跑了格式）分开——两者的修法完全不同（调 max_tokens vs 调提示词
    或条数）。
    """
    if not rows:
        print("\n没有观测")
        return
    print(f"\n汇总（keep={keep}，共 {len(rows)} 次尝试）")
    print(f"{'批次':<8}{'成功':>5}{'失败':>5}{'失败率':>8}{'截断':>6}{'格式':>6}"
          f"{'可引用覆盖率 均值':>18}{'极差':>10}{'节点数 均值':>12}{'极差':>8}"
          f"{'耗时 均值':>10}{'极差':>8}{'completion 均值':>16}{'最大':>8}")
    batches = sorted({row["batch"] for row in rows})
    for batch in batches:
        group = [row for row in rows if row["batch"] == batch]
        ok = [row for row in group if row.get("ok")]
        fail = [row for row in group if not row.get("ok")]
        truncated = sum(1 for row in fail if row.get("failure_kind") == "truncated")
        fmt = sum(1 for row in fail if row.get("failure_kind") == "format")
        cite = [row.get("citable_coverage", 0.0) for row in ok]
        nodes = [row.get("node_count", 0) for row in ok]
        times = [row.get("seconds", 0.0) for row in ok]
        completions = [row.get("completion_max", 0) for row in group]
        mean_cite = statistics.mean(cite) if cite else 0.0
        spread_cite = (max(cite) - min(cite)) if cite else 0.0
        mean_nodes = statistics.mean(nodes) if nodes else 0.0
        spread_nodes = (max(nodes) - min(nodes)) if nodes else 0
        mean_time = statistics.mean(times) if times else 0.0
        spread_time = (max(times) - min(times)) if times else 0.0
        print(f"{batch:<8}{len(ok):>5}{len(fail):>5}"
              f"{(len(fail) / len(group)):>8.1%}"
              f"{truncated:>6}{fmt:>6}"
              f"{mean_cite:>17.1%}{spread_cite:>10.1%}"
              f"{mean_nodes:>12.0f}{spread_nodes:>8}"
              f"{mean_time:>9.0f}s{spread_time:>7.0f}s"
              f"{statistics.mean(completions):>16.0f}{max(completions):>8}")
    # 失败轮与成功轮的 completion 峰值对照：这是"顶满了吗"的唯一直接证据。
    ok_c = [row.get("completion_max", 0) for row in rows if row.get("ok")]
    bad_c = [row.get("completion_max", 0) for row in rows if not row.get("ok")]
    if ok_c and bad_c:
        print(f"\ncompletion 峰值：成功轮 均值 {statistics.mean(ok_c):.0f} / "
              f"最大 {max(ok_c)}；失败轮 均值 {statistics.mean(bad_c):.0f} / "
              f"最大 {max(bad_c)}（max_tokens 天花板 32768）")
    failures = [row for row in rows if not row.get("ok")]
    if failures:
        print("\n失败明细（`finish` 里有没有 length = 是不是被截断）")
        for row in failures:
            responses = [call for call in (row.get("calls") or [])
                         if call.get("kind") == "response"]
            prompts = [call for call in (row.get("calls") or [])
                       if call.get("kind") == "prompt"]
            print(f"  {row['batch']}#{row['run']}  {row.get('failure_kind')}"
                  f"  finish={[r.get('finish') for r in responses]}"
                  f"  completion={[r.get('completion_tokens') for r in responses]}"
                  f"  prompt={[r.get('prompt_tokens') for r in responses]}"
                  f"  prompt_chars(max)={max((p['prompt_chars'] for p in prompts), default=0)}"
                  f"  {str(row.get('error'))[:36]}")
            tails = [r.get("content_tail") for r in responses if r.get("content_tail")]
            if tails:
                print(f"      最后一次输出结尾：{str(tails[-1])[-90:]!r}")


if __name__ == "__main__":
    raise SystemExit(main())
