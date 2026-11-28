"""同条件对比：**整树一次生成**（+确定性修复+自检回路）对**逐支分片**（骨架→按支展开→验收→补支）。

为什么有这个脚本（回答的是一次具体的测量事故）
------------------------------------------------
`docs/SUBAGENT-VS-SELFCHECK.md` 里那两个数字（分片 4/13 张有跨支重复、整树 7/66、
每张图 1.31 对 0.32）来自 `probe_history_health.py` 按**日期**分组的历史产物：
主题与材料没有配对、两边节点规模不同（中位 57 对 48）、其中 38/66 张根本没有材料、
旧分片产物也不含"模型检查修正"那一步。所以那不是一个同条件对比，不该用它否掉
任何一条架构。这个脚本重做一遍，把能控住的都控住。

两臂
------
    A  整树     `mindmap_generate`          —— `MINDMAP_AUTO_REPAIR=1` + `MINDMAP_SELF_CHECK=1`
    B  逐支     `mindmap_generate_deep`     —— 骨架 1 次 + 按支展开 N 次 + 确定性验收 + 只补不合格的支

**B 臂用的是产品里那份真实现，不是手搓的模拟。** `bc3494e` 把主路径换成了整树，
但 `mindmap_generate_deep` 的代码与规格一直在 `_TOOL_SPECS` 里（挂在 `create_deep`
这个不对外暴露的意图下）。本脚本按 `create_deep` 建注册表、按 `create` 建 RunContext
（工具内部那句 `intent != "create"` 的守卫要过），一行产品代码都不改。

已知的、消不掉的偏差（报告里必须写，不然又是一次不可比的对比）
------------------------------------------------------------
1. **材料入口形态不同**，这是架构差异本身：整树在材料 ≤ `FULL_TEXT_MATERIAL_CHARS`
   （50,000 字）时把正文**全塞**进 prompt；分片**永远**按支检索（它不采用全塞）。
   所以 `S`/`M`/`L` 三组里两臂看到的材料形态不同；只有 `X`（>50k）与 `P`（0 份）
   两组是真正同输入。
2. **分片的"检查"是确定性的**（`_audit_branches` 只数子节点个数，零模型调用），
   而用户提的架构是"每支各做一次检查"。这份实现里**没有**那一步的模型版，
   本脚本也补不出来（补了就是"我测的东西不等于产品"）。这是 B 臂相对用户方案
   **更弱**的一处，报告里必须交代。
3. `max_depth=3`、`branches=5` 两臂同值：分片的默认深度是契约上限 8 层，与整树
   的 `INITIAL_MAX_DEPTH=3` 不同规模，不夹住就等于比"谁铺得大"。

控住的东西
------------
- 主题与材料**逐字相同**（同一份 `Source` 对象，同一批字符）。
- 检索**快照固定**：`_search_branch` 被包了一层按 query 的缓存，同一 (语料, query)
  在整轮测量里只真检索一次，之后命中同一批 `EvidenceHit`。两臂、两次重复因此
  面对同一份证据；重复里检索不再是变量。
- 外部检索**全关**（`MINDMAP_EXTERNAL_KNOWLEDGE=0` / `_DEEPSEEK_SEARCH=0`）：
  否则"无材料"那一组会被外部搜索补成"有材料"，两臂的输入就分叉了。
- 库自动补料关（`MINDMAP_LIBRARY_IN_GENERATE=0`）：同上，它会把 0 份材料变成 N 份。
- 查询扩展关（`MINDMAP_QUERY_EXPANSION` 缺省即 0）：它是一次模型调用，两臂都发
  只是把噪声加倍。
- 模型、温度、预算、开关集（取 `run-backend.bat` 那一套，逐条写在 `SWITCHES` 里，
  也随每行结果落盘，报告可复算）。

量什么
------
① 机械跨支重复（`tools._cross_branch_duplicates`，与旧说法同口径，只用于对齐）
② **每百节点**多余重复数 —— 绝对数不可比，两臂节点数不同
③ 覆盖度（引用覆盖率 / 可引用节点覆盖率 / 材料份数覆盖）
④ 调用数、token（prompt/completion）、墙钟
⑤ 人工（语义）判重：只在这里落盘标签与路径，判在 `--dump-blind` 出来的盲包上做

用法（在 `backend/` 下）
------------------------
    python scripts/measure_two_arch.py --dry            # 只打印计划，不调模型
    python scripts/measure_two_arch.py --repeat 1       # 6 组 × 2 臂 × 1 次 = 12 张
    python scripts/measure_two_arch.py --repeat 2 --resume   # 接着往下跑
    python scripts/measure_two_arch.py --dump-blind     # 出盲评包（不含臂标签）
    python scripts/measure_two_arch.py --offline-repair # 零模型：修复对两臂各做了什么
"""
# requires-optional: winreg
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DATA = BACKEND / "data"
OUT = DATA / "two_arch.json"
TREES = DATA / "two-arch-trees"
BLIND = DATA / "two-arch-blind"

#: 与 `run-backend.bat` 同一批键（那里的值就是"这次要测的产品配置"）。
CREDENTIALS = (
    "MINDMAP_LLM_MODE", "MINDMAP_LLM_BASE_URL", "MINDMAP_LLM_API_KEY",
    "MINDMAP_LLM_MODEL", "MINDMAP_LLM_MODEL_DEEP",
    "MINDMAP_EMBEDDING_BASE_URL", "MINDMAP_EMBEDDING_API_KEY",
    "MINDMAP_EMBEDDING_PROTOCOL", "MINDMAP_EMBEDDING_MODEL", "MINDMAP_EMBEDDING_DIM",
    "MINDMAP_RERANK", "MINDMAP_RERANK_MODEL", "MINDMAP_PROFILE_RERANK",
)

#: 逐条显式设死（不读 .env、不继承外面那份）：两臂必须跑在同一个开关集上，
#: 而"这次到底开了什么"要能从结果文件里读回来。
SWITCHES = {
    "MINDMAP_SECTION_SLICE": "1",
    "MINDMAP_GRAPH_INGEST": "1",
    "MINDMAP_GRAPH_EXTRACTION": "1",
    "MINDMAP_DETAIL_RETRIEVAL": "1",
    "MINDMAP_DETAIL_SCALE": "1",
    "MINDMAP_NODE_SCALE": "0",       # 实测 -43% 耗时
    "MINDMAP_OPTIMIZATION_CROSS_MAP": "0",
    "MINDMAP_VERIFY_AFTER_GENERATE": "0",
    "MINDMAP_DETAIL_LENGTH": "free",
    # 整树那一臂的定义：修复开、自检开。分片那条路不接这两个开关（不是我不给）。
    "MINDMAP_AUTO_REPAIR": "1",
    "MINDMAP_SELF_CHECK": "1",
    # 固定输入：外部检索与库补料一律关，否则"无材料"这一组会被补成"有材料"。
    "MINDMAP_EXTERNAL_KNOWLEDGE": "0",
    "MINDMAP_EXTERNAL_DEEPSEEK_SEARCH": "0",
    "MINDMAP_LIBRARY_IN_GENERATE": "0",
    "MINDMAP_LIBRARY_SEARCH": "0",
    "MINDMAP_QUERY_EXPANSION": "0",
}

#: 六组主题。`need` 是要哪几份材料（按文件名精确匹配），`topic` 是两臂共用的主题。
GROUPS = (
    {"key": "S", "label": "小材料 2.7k 字", "topic": "碳中和的主要方面",
     "need": ["wikipedia: 碳中和"], "pick": 1},
    {"key": "M", "label": "中材料 11.9k 字", "topic": "机器学习的主要方向",
     "need": ["wikipedia: 机器学习", "deepseek: 机器学习 - 多领域交叉学科"], "pick": 2},
    {"key": "L", "label": "大材料 42.5k 字（闸内·全塞）", "topic": "注意力机制的原理与应用",
     "need": ["attention-37973.md"], "pick": 1},
    {"key": "X", "label": "超阈值 66.5k 字（闸外·按支检索）", "topic": "生成流水线的设计与实测",
     "need": ["AGENT_PLAN.md", "PLAN.md", "RAG-DECISIONS.md", "ARCHITECTURE.md"], "pick": 4},
    {"key": "P1", "label": "无材料", "topic": "机器学习的主要方向", "need": [], "pick": 0},
    {"key": "P2", "label": "无材料", "topic": "碳中和的主要方面", "need": [], "pick": 0},
)

ARMS = ("A", "B")
ARM_NAMES = {"A": "整树（一次写完+修复+自检）", "B": "逐支（骨架→按支展开→验收→补支）"}

#: 每一次模型调用的记账（`_parse_choice` 往这里写）。
_CALLS: list[dict] = []
#: 检索缓存：同一 (语料, query) 只真检索一次。这就是"固定检索快照"。
_SEARCH_CACHE: dict[str, list | None] = {}
_SEARCH_STATS = {"hit": 0, "miss": 0, "failure": 0}

#: 材料快照，**切库之前**读好（与 `measure_generation_strategy._snapshot_library` 同因）。
_LIBRARY: list = []


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


def _prepare() -> None:
    for name in CREDENTIALS:
        if not os.environ.get(name):
            value = _user_env(name)
            if value:
                os.environ[name] = value
    for name, value in SWITCHES.items():
        os.environ[name] = value
    # 外部检索关着，代理只对它有副作用：留着它只会让 embedding 也去撞代理。
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(name, None)


def _snapshot_library(db_path: Path) -> None:
    """先读材料、再切库（顺序反了就是 0 份材料，实测踩过一次）。"""
    global _LIBRARY
    from app.knowledge import library as knowledge_library

    _LIBRARY = knowledge_library.recent(limit=400)
    os.environ["MINDMAP_DB_PATH"] = str(db_path)


def _sources_for(group: dict) -> list:
    """按文件名精确取材料；取不到就报错，绝不静默降级成"少一份材料"。"""
    picked = []
    for need in group["need"]:
        hits = [item for item in _LIBRARY if (item.filename or "") == need]
        if not hits:
            raise SystemExit(f"材料库里没有《{need}》（组 {group['key']}）")
        picked.append(hits[0])
    return picked[: group["pick"]] if group["pick"] else picked


def _instrument() -> None:
    """三个插桩点，都不改变被测行为。

    1. `generation.service._parse_choice`：三条模型出口最后都到这里，从 `body`
       拿 `usage`（tool 内部那次调用也走它——`chat_async` → `_chat_once`）。
    2. `agent.tools._chat`：工具内部**唯一**的模型出口，用它数调用次数与 prompt
       体量，与 (1) 对账。
    3. `agent.tools._search_branch`：按 (语料, query) 缓存命中，固定检索快照。
    """
    from app.agent import tools as agent_tools
    from app.generation import service as generation_service

    original_parse = generation_service._parse_choice
    original_chat = agent_tools._chat
    original_search = agent_tools._search_branch

    def wrapped_parse(body, request_body=None):
        try:
            usage = body.get("usage") or {}
            choice = (body.get("choices") or [{}])[0] or {}
            _CALLS.append({
                "kind": "usage",
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "finish": choice.get("finish_reason"),
                "content_chars": len(((choice.get("message") or {}).get("content") or "")),
                "model": body.get("model"),
            })
        except Exception:  # noqa: BLE001 — 插桩永不改变被测行为
            pass
        return original_parse(body, request_body)

    async def wrapped_chat(ctx, system, user, **kwargs):
        _CALLS.append({"kind": "prompt", "prompt_chars": len(system) + len(user)})
        return await original_chat(ctx, system, user, **kwargs)

    async def wrapped_search(sources, title, intents=(), **kwargs):
        key = json.dumps(
            [sorted(source.id for source in sources), title, list(intents),
             sorted(kwargs.get("directions") or [])],
            ensure_ascii=False, sort_keys=True, default=str)
        if key in _SEARCH_CACHE:
            _SEARCH_STATS["hit"] += 1
            return _SEARCH_CACHE[key]
        _SEARCH_STATS["miss"] += 1
        try:
            hits = await original_search(sources, title, intents, **kwargs)
        except Exception:
            _SEARCH_STATS["failure"] += 1
            raise
        _SEARCH_CACHE[key] = hits
        return hits

    generation_service._parse_choice = wrapped_parse
    agent_tools._chat = wrapped_chat
    agent_tools._search_branch = wrapped_search


# ---------------------------------------------------------------- 树上的读数


def _walk(root):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.children)


def _max_depth(root, depth: int = 1) -> int:
    if not root.children:
        return depth
    return max(_max_depth(child, depth + 1) for child in root.children)


def _ref_stats(root, sources) -> dict:
    """引用覆盖率两个分母 + 材料份数覆盖。口径与 `measure_generation_strategy` 同源。"""
    total = with_refs = citable = citable_with_refs = 0
    used: set[str] = set()
    for node in _walk(root):
        total += 1
        has = bool(node.source_refs)
        with_refs += 1 if has else 0
        if (node.detail or "").strip():
            citable += 1
            citable_with_refs += 1 if has else 0
        for ref in node.source_refs or []:
            used.add(ref.source_id)
    known = {source.id for source in sources}
    return {
        "node_count": total,
        "with_refs": with_refs,
        "citable": citable,
        "citable_with_refs": citable_with_refs,
        "ref_coverage": round(with_refs / max(1, total), 3),
        "citable_coverage": round(citable_with_refs / max(1, citable), 3),
        "sources_total": len(known),
        "sources_used": len(used & known),
        "source_coverage": round(len(used & known) / max(1, len(known)), 3),
        "detail_chars_mean": round(
            statistics.mean([len((node.detail or "").strip()) for node in _walk(root)]), 1),
    }


def _dup_mechanical(root) -> dict:
    """机械判据（与旧说法同口径）：不同父、去空白后逐字相同的标签。

    留它不是因为我信它——它抓不到语义重复。留它是为了让新数字与旧数字能对上，
    以及让"机械 vs 人工"的差额本身成为一个读数。
    """
    from app.agent.tools import _cross_branch_duplicates

    pairs = _cross_branch_duplicates(root)
    places = sum(len(spot) for _, spot in pairs)
    return {"dup_groups": len(pairs), "dup_places": places}


def _labels(root) -> list[dict]:
    """给人工判重用的扁平清单：标签 + 路径 + 一级分支祖先 + 深度 + 说明首句。"""
    out: list[dict] = []

    def walk(node, path, branch, depth):
        for child in node.children:
            here = f"{path} / {child.label}" if path else child.label
            top = branch if branch is not None else child.label
            out.append({
                "label": child.label,
                "path": here,
                "branch": top,
                "depth": depth,
                "detail": (child.detail or "").strip()[:60],
            })
            walk(child, here, top, depth + 1)

    walk(root, "", None, 1)
    return out


# ---------------------------------------------------------------- 一臂一次


def _run_once(group: dict, arm: str, index: int) -> dict:
    from app.agent import tools as agent_tools
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall

    sources = _sources_for(group)
    topic = group["topic"]
    _CALLS.clear()
    _SEARCH_STATS.update({"hit": 0, "miss": 0, "failure": 0})

    request = AgentRunRequest(
        goal=topic, intent="create", thinking_model="default", sources=sources,
        budget=RunBudget(max_model_calls=80, max_tool_calls=8, max_seconds=900.0,
                         max_output_tokens=32768, max_repairs=1),
    )
    ctx = RunContext(request=request,
                     sources_by_id={source.id: source for source in sources})
    tool = "mindmap_generate" if arm == "A" else "mindmap_generate_deep"
    arguments = {"topic": topic, "branches": 5, "diagram_type": "tree"}
    if arm == "B":
        # 深度与扇出夹到整树那条路的同一个量级（`INITIAL_MAX_DEPTH=3`），
        # 否则比的是"谁铺得更大"而不是"谁重复得多"。
        arguments.update({"points_per_branch": 3, "max_depth": 3})
        registry = agent_tools.build_registry("create_deep", has_sources=bool(sources),
                                              has_library=False)
    else:
        registry = agent_tools.build_registry("create", has_sources=bool(sources),
                                              has_library=False)

    started = time.perf_counter()
    error = ""
    try:
        result = asyncio.run(registry.execute(
            ToolCall(call_id=f"{group['key']}{arm}{index}", name=tool,
                     arguments=arguments), ctx))
        ok = bool(result.ok)
        if not ok:
            error = (result.content or "")[:400]
        data = result.data or {}
    except Exception as exc:  # noqa: BLE001 — 一轮失败不该中断整批
        ok, error, data = False, f"{type(exc).__name__}: {exc}", {}
    seconds = round(time.perf_counter() - started, 1)

    usages = [call for call in _CALLS if call["kind"] == "usage"]
    prompts = [call for call in _CALLS if call["kind"] == "prompt"]
    row: dict = {
        "group": group["key"], "arm": arm, "run": index, "ok": ok,
        "tool": tool, "arguments": arguments, "topic": topic,
        "materials": [source.filename for source in sources],
        "material_chars": sum(len(source.text or "") for source in sources),
        "sources": len(sources),
        "seconds": seconds,
        "model_calls": len(prompts),
        "usage_records": len(usages),
        "prompt_tokens": sum(call.get("prompt_tokens") or 0 for call in usages),
        "completion_tokens": sum(call.get("completion_tokens") or 0 for call in usages),
        "prompt_chars_max": max((call["prompt_chars"] for call in prompts), default=0),
        "finish_set": sorted({str(call.get("finish")) for call in usages}),
        "completion_max": max((call.get("completion_tokens") or 0 for call in usages),
                             default=0),
        "search": dict(_SEARCH_STATS),
        "partial": data.get("partial"),
        "partial_reason": data.get("partial_reason"),
        "auto_repair": data.get("auto_repair"),
        "self_check": data.get("self_check"),
        "diagnostic_codes": sorted({item.get("code") for item in data.get("diagnostics") or []
                                    if item.get("code")}),
        "error": error,
    }
    tree = ctx.candidate
    if not ok or tree is None:
        row["ok"] = False
        row.setdefault("error", "工具返回 ok 但没有产出树")
        return row

    root = tree.root
    row.update(_ref_stats(root, sources))
    row.update(_dup_mechanical(root))
    row["branches"] = len(root.children)
    row["max_depth"] = _max_depth(root)
    row["dup_per_100"] = round(row["dup_places"] / max(1, row["node_count"]) * 100, 2)
    row["labels"] = _labels(root)
    TREES.mkdir(exist_ok=True)
    (TREES / f"{group['key']}-{arm}-{index}.json").write_text(
        json.dumps(tree.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8")
    return row


# ---------------------------------------------------------------- 报告


def _table(rows: list[dict]) -> None:
    print(f"\n{'组':<3}{'臂':<3}{'次':<3}{'成功':<5}{'节点':>5}{'一级':>5}{'深':>4}"
          f"{'可引用覆盖':>10}{'机械重复':>9}{'每百':>7}{'调用':>5}{'输入tk':>8}"
          f"{'输出tk':>8}{'墙钟':>7}")
    for row in rows:
        if not row.get("ok"):
            print(f"{row['group']:<3}{row['arm']:<3}{row['run']:<3}失败  {str(row.get('error'))[:60]}")
            continue
        print(f"{row['group']:<3}{row['arm']:<3}{row['run']:<3}{'✓':<5}"
              f"{row['node_count']:>5}{row['branches']:>5}{row['max_depth']:>4}"
              f"{row['citable_coverage']:>9.1%}{row['dup_places']:>9}"
              f"{row['dup_per_100']:>7.2f}{row['model_calls']:>5}"
              f"{row['prompt_tokens']:>8}{row['completion_tokens']:>8}{row['seconds']:>6}s")


def _aggregate(rows: list[dict]) -> None:
    print(f"\n{'臂':<28}{'图':>4}{'节点':>7}{'机械重复':>9}{'每百':>8}{'可引用覆盖':>11}"
          f"{'材料覆盖':>9}{'调用':>7}{'输入tk':>9}{'输出tk':>9}{'墙钟':>8}")
    for arm in ARMS:
        ok = [row for row in rows if row["arm"] == arm and row.get("ok")]
        if not ok:
            continue
        nodes = sum(row["node_count"] for row in ok)
        dups = sum(row["dup_places"] for row in ok)
        print(f"{ARM_NAMES[arm]:<28}{len(ok):>4}{nodes:>7}{dups:>9}"
              f"{dups / max(1, nodes) * 100:>8.2f}"
              f"{statistics.mean([r['citable_coverage'] for r in ok]):>10.1%}"
              f"{statistics.mean([r['source_coverage'] for r in ok]):>9.1%}"
              f"{statistics.mean([r['model_calls'] for r in ok]):>7.1f}"
              f"{sum(r['prompt_tokens'] for r in ok):>9}"
              f"{sum(r['completion_tokens'] for r in ok):>9}"
              f"{statistics.mean([r['seconds'] for r in ok]):>7.0f}s")


def _dump_blind(rows: list[dict]) -> None:
    """盲评包：**不含臂标签**，每棵树一个 anon 名，另存一份只有我这边看的 key。"""
    ok = [row for row in rows if row.get("ok") and row.get("labels")]
    rng = random.Random(20261126)
    order = ok[:]
    rng.shuffle(order)
    BLIND.mkdir(exist_ok=True)
    key = {}
    for position, row in enumerate(order, start=1):
        anon = f"图{position:02d}"
        key[anon] = {"group": row["group"], "arm": row["arm"], "run": row["run"],
                     "node_count": row["node_count"], "topic": row["topic"],
                     "materials": row["materials"]}
        lines = [f"{anon}  主题：{row['topic']}",
                 f"节点数 {row['node_count']}（以下是全部非根节点，每行：一级分支 › 路径 ｜ 说明首句）",
                 ""]
        for item in row["labels"]:
            detail = f" ｜ {item['detail']}" if item["detail"] else ""
            lines.append(f"[{item['branch']}] {item['path']}{detail}")
        (BLIND / f"{anon}.txt").write_text("\n".join(lines), encoding="utf-8")
    (BLIND / "_key.json").write_text(
        json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n盲评包：{BLIND}（{len(order)} 张；臂标签只在 _key.json 里）")


def _offline_repair(rows: list[dict]) -> None:
    """零模型：把 `repair()` 对两臂产物各跑一遍，看"确定性修复"这一层本身值多少。

    这一列回答的是另一个问题——不是"哪条架构好"，而是"修复这一层能不能替代
    架构差异"。它不改变任何产物（只在内存里修），也不改产品代码。
    """
    from app.contracts.mindmap import MindMapContent
    from app.optimization.repair import AUTO_REPAIRABLE_CODES, repair

    print(f"\n{'组':<4}{'臂':<4}{'节点 修前→修后':>18}{'修复步数':>9}{'跳过':>6}"
          f"{'机械重复 修前→修后':>20}")
    for row in rows:
        path = TREES / f"{row['group']}-{row['arm']}-{row['run']}.json"
        if not row.get("ok") or not path.exists():
            continue
        content = MindMapContent.model_validate(json.loads(path.read_text(encoding="utf-8")))
        before = _dup_mechanical(content.root)["dup_places"]
        outcome = repair(content, codes=AUTO_REPAIRABLE_CODES)
        after = _dup_mechanical(outcome.content.root)["dup_places"]
        nodes_before = sum(1 for _ in _walk(content.root))
        nodes_after = sum(1 for _ in _walk(outcome.content.root))
        print(f"{row['group']:<4}{row['arm']:<4}"
              f"{f'{nodes_before} → {nodes_after}':>18}{outcome.steps:>9}"
              f"{len(outcome.skipped):>6}{f'{before} → {after}':>20}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=1, help="每组每臂跑几次")
    parser.add_argument("--group", default="", help="只跑这几组（逗号分隔）")
    parser.add_argument("--arm", default="", help="只跑这几臂（逗号分隔，A/B）")
    parser.add_argument("--resume", action="store_true",
                        help="跳过已有的 (组, 臂, 次)：中途挂了接着往下跑")
    parser.add_argument("--dry", action="store_true", help="只打印计划，不调模型")
    parser.add_argument("--dump-blind", action="store_true", help="出盲评包后退出")
    parser.add_argument("--offline-repair", action="store_true", help="零模型修复对照后退出")
    args = parser.parse_args()

    _prepare()
    if not os.environ.get("MINDMAP_LLM_API_KEY"):
        print("缺 MINDMAP_LLM_API_KEY（用户级环境变量里也没有）", file=sys.stderr)
        return 2
    db_path = DATA / "two-arch.db"
    _snapshot_library(db_path)
    _instrument()

    groups = [g for g in GROUPS
              if not args.group or g["key"] in args.group.split(",")]
    arms = [a for a in ARMS if not args.arm or a in args.arm.split(",")]

    rows: list[dict] = []
    done: set[tuple[str, str, int]] = set()
    if OUT.exists():
        rows = json.loads(OUT.read_text(encoding="utf-8"))
        done = {(row.get("group"), row.get("arm"), row.get("run")) for row in rows}
        if args.resume:
            print(f"续跑：已有 {len(rows)} 张（跳过这些）", flush=True)
        else:
            print(f"注意：{OUT.name} 已有 {len(rows)} 张，本次结果会覆盖同名 (组,臂,次)",
                  flush=True)

    if args.dump_blind:
        _dump_blind(rows)
        return 0
    if args.offline_repair:
        _table(rows)
        _aggregate(rows)
        _offline_repair(rows)
        return 0

    plan = [(g, a, index) for index in range(1, args.repeat + 1)
            for g in groups for a in arms]
    # 顺序对齐：奇数轮 A→B、偶数轮 B→A。检索缓存让"先跑的那一臂"替后跑的把
    # 证据取回来，虽然命中率不高（两臂的 query 大多不同），能对半分开就对半分开。
    plan.sort(key=lambda item: (item[2], item[0]["key"],
                                0 if (item[2] % 2 == 1) == (item[1] == "A") else 1))
    if args.dry:
        for group, arm, index in plan:
            print(f"{group['key']:<3}{arm}  第 {index} 次  {group['label']:<26}"
                  f"{group['topic']}  材料 {len(group['need'])} 份")
        print(f"\n共 {len(plan)} 张（去重后 "
              f"{len({(g['key'], a, i) for g, a, i in plan})} 张）")
        return 0

    for group, arm, index in plan:
        if args.resume and (group["key"], arm, index) in done:
            print(f"… {group['key']}{arm}#{index} 已有，跳过", flush=True)
            continue
        print(f"… {group['key']}{arm}#{index} 开始（{group['topic']}）", flush=True)
        row = _run_once(group, arm, index)
        rows = [existing for existing in rows
                if (existing.get("group"), existing.get("arm"), existing.get("run"))
                != (group["key"], arm, index)]
        rows.append(row)
        rows.sort(key=lambda item: (item.get("group", ""), item.get("arm", ""),
                                    item.get("run", 0)))
        OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: row.get(key) for key in (
            "ok", "seconds", "node_count", "branches", "max_depth", "dup_places",
            "dup_per_100", "citable_coverage", "model_calls", "prompt_tokens",
            "completion_tokens", "error")}, ensure_ascii=False), flush=True)

    print(f"\n明细：{OUT}")
    _table(rows)
    _aggregate(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
