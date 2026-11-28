"""`CROSS_REFERENCE_MAX = 8` 够不够 —— 台账第 9 条那个卡点的采样。

## 为什么要有它

第 9 条此前只有一个采样支撑"8 不够"（`data/acc_trees-s02-crossref/S02-r2`），而那份
产物**只存了树**。于是"顶满 8 条"这件事事后无法区分两种情况：

    模型自己收敛到 8        ← 上限没起作用，病在"它只肯写 8 条"
    模型写了 12 条被截到 8  ← 上限起了作用，病在配额

两者的修法正相反。所以本脚本量四个数，而不是只数树上那 8 条：

    declared  模型这次自己写了几条 `relations`（`raw_relations` 的长度）
    kept      收下几条（= 树上 `relations` 的条数）
    limited   被 `CROSS_REFERENCE_MAX` 截掉几条（`relation_drops["limited"]`）
    unlinked  `CROSS_BRANCH_UNLINKED` 报几处（同核而没有任何关系指认的节点对）

再加两个**分母**：跨支同核对全集有几对、其中几对已被指认。少了它，"unlinked = 2"
读不出是"只挤掉了 2 条"还是"12 对里只有 2 对没交代"。

## 它按内容认版本，不按时间

每次采样把 `CROSS_REFERENCE_RULES` 的 sha256 与 `CROSS_REFERENCE_MAX` 写进记录。
上一轮的读数之所以过期，是因为"这份读数属于哪版代码"只能靠提交时间判
（`docs/BACKLOG.md` 第 9 条的 `blocker_probe` 就是那个时序判据），而时序判据在
一次干净 checkout 上永远为真 —— 它测的不是代码，是文件的修改时间。

用法（模型环境变量与 `run-backend.bat` 是同一批）：

    python scripts/measure_cross_reference_quota.py --only S02 --repeat 5
    python scripts/measure_cross_reference_quota.py --only S02 --repeat 1 --no-ingest
    python scripts/measure_cross_reference_quota.py --replay data/acc_trees-s02-crossref

`--replay` 只读已落盘的树、重算分母与 unlinked（不产生模型调用）：这样上一轮那三棵
也能进同一张表，上一轮缺的 `declared/limited` 标成 `null` 而不是 0。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DATA = BACKEND / "data"
SAMPLES = Path(__file__).with_name("acc_samples.json")


# ---------------------------------------------------------------- 判据（与 service 同构）

def core_pairs(content) -> list[dict]:
    """跨支同核的**全部**节点对，带"是否已被 relations 指认"。

    边界与 `optimization/service.py::_cross_branch_unlinked_diagnostics` 逐条对应
    （同父不算 / 同一祖先链不算 / 逐字同名让给 `CROSS_BRANCH_DUPLICATE`），差别只有
    一个：这里**不跳过**已指认的对，而是标出来 —— 分母本身就是要量的东西。

    防漂：`tests/test_cross_reference_quota.py` 断言"未指认的对数"与那条诊断报的处数
    相等。两份实现写在两处是本仓库反复踩过的坑（§十），所以这条用例不是装饰。
    """
    from app.optimization.cross_branch import normalize_label
    from app.optimization.service import MIN_CORE_CHARS, _core_label, _declared_pairs

    declared = _declared_pairs(content)
    buckets: dict[str, list[tuple]] = {}

    def walk(node, parent, ancestors: frozenset[str]) -> None:
        core = _core_label(node.label)
        if len(core) >= MIN_CORE_CHARS:
            buckets.setdefault(core, []).append((node, parent, ancestors))
        below = ancestors | {node.id}
        for child in node.children:
            walk(child, node, below)

    walk(content.root, None, frozenset())

    rows: list[dict] = []
    seen: set[frozenset[str]] = set()
    for core, group in buckets.items():
        for index, (left, left_parent, left_anc) in enumerate(group):
            for right, right_parent, right_anc in group[index + 1:]:
                if left_parent is not None and left_parent is right_parent:
                    continue
                if right.id in left_anc or left.id in right_anc:
                    continue
                if normalize_label(left.label) == normalize_label(right.label):
                    continue          # 逐字同名：CROSS_BRANCH_DUPLICATE 的地盘
                pair = frozenset((left.id, right.id))
                if pair in seen:
                    continue
                seen.add(pair)
                rows.append({
                    "core": core,
                    "left": left.label.strip(),
                    "right": right.label.strip(),
                    "left_parent": (left_parent.label.strip() if left_parent else "根"),
                    "right_parent": (right_parent.label.strip() if right_parent else "根"),
                    "declared": pair in declared,
                })
    return rows


def measure(content, *, limit: int) -> dict:
    """一棵树上的读数。`declared` / `limited` 来自生成链路，`--replay` 时为 `None`。"""
    pairs = core_pairs(content)
    unlinked = [row for row in pairs if not row["declared"]]
    kept = len(content.relations)
    return {
        "relations": kept,
        "limit": limit,
        "full": kept >= limit,
        "pairs_total": len(pairs),
        "pairs_declared": len(pairs) - len(unlinked),
        "unlinked": len(unlinked),
        "unlinked_detail": unlinked,
    }


def rules_fingerprint() -> dict:
    """这次采样按哪一版提示词、哪个上限量的 —— **内容指纹**，不是文件时间。"""
    from app.agent.tools import CROSS_REFERENCE_MAX, CROSS_REFERENCE_RULES

    return {
        "rules_sha": hashlib.sha256(CROSS_REFERENCE_RULES.encode("utf-8")).hexdigest()[:12],
        "rules_chars": len(CROSS_REFERENCE_RULES),
        "limit": CROSS_REFERENCE_MAX,
    }


def apply_limit_override(limit: int) -> None:
    """把上限临时改成 `limit`（进程内，不落盘）——**对照臂**。

    只改数字不够：提示词正文里写着"最多 8 条……超过这个数说明你该合并节点"。
    只把代码上限抬到 16 而提示词还说 8，模型照样写 8 条，量到的就只是代码那一半。
    所以两处一起改，并把"超了该干什么"那句里的数一起换掉。

    这不是在测"改到多少合适"：它测的是**模型在更宽的上限下会不会多写**。会写，
    说明 8 是配额挤掉的；不写，说明 8 是它自己的收敛口径，改数字没有用。
    """
    from app.agent import tools as tools_module

    old = tools_module.CROSS_REFERENCE_MAX
    if limit <= old:
        return
    tools_module.CROSS_REFERENCE_RULES = tools_module.CROSS_REFERENCE_RULES.replace(
        f"最多 {old} 条", f"最多 {limit} 条").replace(
        f"加第{old + 1}条线", f"加第{limit + 1}条线")
    tools_module.CROSS_REFERENCE_MAX = limit


# ---------------------------------------------------------------- 输入

def load_samples(only: str) -> list[dict]:
    payload = json.loads(SAMPLES.read_text(encoding="utf-8"))
    items = payload["samples"] if isinstance(payload, dict) else payload
    if only:
        wanted = {item.strip().upper() for item in only.split(",")}
        items = [item for item in items if item["id"].upper() in wanted]
    return items


def source_for(sample: dict, *, ingest: bool):
    """把样本变成 `Source`。默认先走一次真实入库（与验收脚本同一条路）。"""
    from app.contracts.source import Source, SourceKind

    if ingest:
        from measure_acceptance import TEXT_SOURCE, post_json, session

        parsed = post_json(session(), TEXT_SOURCE,
                           {"text": sample["text"], "kind": "text"}, timeout=300.0)
        if parsed.get("ok") and (parsed.get("body") or {}).get("source"):
            return Source.model_validate(parsed["body"]["source"])
        print(f"  ! 入库失败，退回本地构造：{parsed.get('detail')}", flush=True)
    return Source(id=f"q-{sample['id']}", kind=SourceKind.document, status="parsed",
                  filename=f"{sample['id']}.md", text=sample["text"])


async def generate_once(sample: dict, *, ingest: bool):
    """真跑一次整树生成。**进程内直调工具**，不经 agent 循环。

    为什么是进程内：`declared` / `relation_drops` 只出现在工具返回值里
    （`result.data["cross_reference"]`），SSE 的 `done` 帧只带 `content`。
    上一轮走 HTTP 采样，于是这三个数一个都没留下。
    """
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
    from app.agent.tools import build_registry

    source = source_for(sample, ingest=ingest)
    request = AgentRunRequest(
        goal=sample["goal"], intent="create", thinking_model="default",
        sources=[source],
        budget=RunBudget(max_model_calls=20, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=1),
    )
    ctx = RunContext(request=request, sources_by_id={source.id: source})
    registry = build_registry("create", has_sources=True, has_library=False)
    started = time.perf_counter()
    result = await registry.execute(
        ToolCall(call_id="quota", name="mindmap_generate",
                 arguments={"topic": sample["goal"], "diagram_type": "tree"}), ctx)
    return result, ctx, round(time.perf_counter() - started, 1)


# ---------------------------------------------------------------- 两个模式

def report_rows(rows: list[dict]) -> None:
    print("\n  declared  kept  limited  unlinked/分母  节点")
    for row in rows:
        print(f"  {str(row.get('declared')):>8}  {row['relations']:>4}  "
              f"{str(row.get('limited')):>7}  "
              f"{row['unlinked']:>3}/{row['pairs_total']:<8}  {row.get('nodes', '-')}")


def mode_run(args) -> int:
    if args.limit:
        apply_limit_override(args.limit)
    fingerprint = rules_fingerprint()
    print(f"提示词指纹 {fingerprint['rules_sha']}（{fingerprint['rules_chars']} 字），"
          f"上限 {fingerprint['limit']}"
          f"{'（对照臂：进程内临时抬到 ' + str(args.limit) + '，不落盘）' if args.limit else ''}",
          flush=True)
    out_dir = Path(args.out) if args.out else DATA / f"acc_trees-{args.tag}"
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = DATA / "cross_reference_quota.jsonl"
    rows: list[dict] = []
    for sample in load_samples(args.only):
        for index in range(1, args.repeat + 1):
            result, ctx, seconds = asyncio.run(
                generate_once(sample, ingest=not args.no_ingest))
            content = ctx.candidate
            if not result.ok or content is None:
                print(f"[{sample['id']} r{index}] 生成失败 {seconds}s：{result.message}",
                      flush=True)
                rows.append({"id": sample["id"], "round": index, "ok": False,
                             "seconds": seconds, "message": result.message,
                             **fingerprint})
                continue
            report = measure(content, limit=fingerprint["limit"])
            account = (result.data or {}).get("cross_reference") or {}
            row = {
                "id": sample["id"], "round": index, "ok": True, "seconds": seconds,
                "tag": out_dir.name,
                # 这四个才是"8 够不够"的答案：模型写的 / 收下的 / 被上限截的 / 未指认的
                "declared": account.get("declared"),
                "limited": (account.get("relation_drops") or {}).get("limited"),
                "drops": account.get("relation_drops") or {},
                "relations": report["relations"],
                "full": report["full"],
                "nodes": _count_nodes(content),
                "pairs_total": report["pairs_total"],
                "pairs_declared": report["pairs_declared"],
                "unlinked": report["unlinked"],
                "unlinked_detail": report["unlinked_detail"],
                "codes": [item["code"] for item in (result.data or {}).get("diagnostics", [])],
                **fingerprint,
            }
            rows.append(row)
            path = out_dir / f"{sample['id']}-r{index}.json"
            path.write_text(json.dumps(content.model_dump(mode="json"), ensure_ascii=False),
                            encoding="utf-8")
            with jsonl.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"[{sample['id']} r{index}] declared {row['declared']} 收下 "
                  f"{row['relations']}（上限 {row['limit']}）被上限截 "
                  f"{row['limited']}；未指认 {row['unlinked']}/{row['pairs_total']} 对；"
                  f"节点 {row['nodes']}；{seconds}s → {path.name}", flush=True)
            for item in row["unlinked_detail"]:
                print(f"    · 「{item['left']}」({item['left_parent']}) ↔ "
                      f"「{item['right']}」({item['right_parent']})", flush=True)
    report_rows(rows)
    _summary(rows)
    return 0


def mode_replay(args) -> int:
    """只读已落盘的树：重算分母与未指认数。`declared/limited` 无从恢复，记 `None`。"""
    from app.contracts.mindmap import MindMapContent

    folder = Path(args.replay)
    files = sorted(folder.glob("*.json"))
    if not files:
        print(f"{folder} 里没有 json", file=sys.stderr)
        return 2
    limit = rules_fingerprint()["limit"]
    rows = []
    print(f"重放 {folder}（当前上限 {limit}；declared/limited 无法从树恢复，记 null）")
    for path in files:
        content = MindMapContent.model_validate(
            json.loads(path.read_text(encoding="utf-8")))
        report = measure(content, limit=limit)
        rows.append({"id": path.stem, "declared": None, "limited": None,
                     "relations": report["relations"], "full": report["full"],
                     "nodes": _count_nodes(content),
                     "pairs_total": report["pairs_total"],
                     "pairs_declared": report["pairs_declared"],
                     "unlinked": report["unlinked"],
                     "unlinked_detail": report["unlinked_detail"]})
        print(f"\n[{path.stem}] relations {report['relations']}  "
              f"未指认 {report['unlinked']}/{report['pairs_total']} 对")
        for item in report["unlinked_detail"]:
            print(f"    · 「{item['left']}」({item['left_parent']}) ↔ "
                  f"「{item['right']}」({item['right_parent']})")
    report_rows(rows)
    _summary(rows)
    return 0


def _count_nodes(content) -> int:
    total, stack = 0, [content.root]
    while stack:
        node = stack.pop()
        total += 1
        stack.extend(node.children)
    return total


def _summary(rows: list[dict]) -> None:
    usable = [row for row in rows if row.get("ok", True)]
    if not usable:
        return
    full = sum(1 for row in usable if row["full"])
    unlinked = sum(1 for row in usable if row["unlinked"])
    limited = [row["limited"] for row in usable if row.get("limited") is not None]
    declared = [row["declared"] for row in usable if row.get("declared") is not None]
    print(f"\n{len(usable)} 次采样里：顶满上限 {full} 次；有未指认 {unlinked} 次。")
    if declared:
        print(f"  模型自己写的条数：{declared}"
              f"（最大值 {max(declared)}；> 上限的次数 {sum(1 for v in declared if v > max(row['limit'] for row in usable))}）")
    if limited:
        print(f"  被上限截掉的条数：{limited}"
              f"（> 0 的次数 {sum(1 for value in limited if value)}）")
    print("  读法：`declared` 都不超过上限且 `limited` 全 0 → 顶满的是模型的自我收敛，"
          "调上限不会多出一条；反过来 `limited > 0` 才是配额真的挤掉了指认。")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default="S02", help="样本 id，逗号分隔")
    parser.add_argument("--repeat", type=int, default=3, help="同一棵树采样几次")
    parser.add_argument("--tag", default="s02-quota", help="产物目录名：data/acc_trees-<tag>")
    parser.add_argument("--out", default="", help="直接指定产物目录")
    parser.add_argument("--no-ingest", action="store_true",
                        help="跳过 HTTP 入库，直接用样本原文构造 Source")
    parser.add_argument("--limit", type=int, default=0,
                        help="对照臂：进程内把上限与提示词里的数一起抬到 N（不落盘）")
    parser.add_argument("--replay", default="", help="只重放某个目录里已落盘的树")
    args = parser.parse_args(argv)
    if args.replay:
        return mode_replay(args)
    return mode_run(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
