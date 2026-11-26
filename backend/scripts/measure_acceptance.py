"""三条量化指标的实测脚本：结构合理性 / 多模态支持率 / 分档时延。

为什么要有它：`docs/PLAN.md:128-130` 已经把口径定死，但三条指标从来没有数据。
这个脚本把口径变成可复算的执行：

- `structure`：20 条中文样本（`acc_samples.json`，课程大纲 / 会议纪要 / 项目立项）
  逐条走「文本入库 → 真实 SSE 生成」，存下整棵树、结构指纹与每一帧的到达时刻。
  「覆盖」这一维的命中率由样本自带的 `must` 关键词算；其余三维留给人评
  （评分卡在 `docs/ACCEPTANCE-METRICS.md`）。
- `media`：图片 / 语音样本逐个走真实上传接口，记录「成功进到导图」还是「卡在哪一步」。
- `latency`：不单独跑，直接从 `structure` 的帧时间线算三档 —— 首帧 / 根节点 /
  首个一级分支 / 完整树。

**它必须走 HTTP**：进程内直调工具量不出「用户点下去到看见第一个节点」，
那段是 SSE 的行为（`node_grow` 帧）。代价是要过门禁，所以先取一份游客 cookie。

用法：
    python scripts/measure_acceptance.py structure --limit 5     # 先看数据有没有意义
    python scripts/measure_acceptance.py structure               # 跑满 20 条
    python scripts/measure_acceptance.py media --kind image
    python scripts/measure_acceptance.py media --kind audio
    python scripts/measure_acceptance.py summary                 # 只重算已落盘的 jsonl

结果落在 `backend/data/`：`acc_structure.jsonl`（逐条）、`acc_media.jsonl`、
`acc_structure_outline.md`（整树大纲，供人评）。
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import mimetypes
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

BASE = "http://127.0.0.1:8000"
DATA = BACKEND / "data"
SAMPLES = Path(__file__).with_name("acc_samples.json")
STREAM = f"{BASE}/api/agent/runs/stream"
ME = f"{BASE}/api/auth/me"
TEXT_SOURCE = f"{BASE}/api/ingestion/text"
FILE_SOURCE = f"{BASE}/api/ingestion/files"


# ---------------------------------------------------------------- HTTP 底座

def session() -> urllib.request.OpenerDirector:
    """带 cookie 的 opener。门禁认 `mm_guest`，它只在 `/api/auth/me` 发一次。"""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(ME, timeout=15) as response:
        response.read()
    return opener


def post_json(opener, url: str, payload: dict, timeout: float = 120.0):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        return {"ok": False, "status": exc.code, "detail": detail,
                "seconds": round(time.perf_counter() - started, 2)}
    elapsed = round(time.perf_counter() - started, 2)
    try:
        return {"ok": True, "status": status, "body": json.loads(text), "seconds": elapsed}
    except ValueError:
        return {"ok": False, "status": status, "detail": text[:600], "seconds": elapsed}


def upload(opener, filename: str, raw: bytes, timeout: float = 300.0):
    """multipart/form-data。字段名必须是 `file`（`router.create_file_source`）。"""
    boundary = "----acc" + uuid.uuid4().hex
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if filename.lower().endswith(".wav"):
        mime = "audio/wav"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    request = urllib.request.Request(
        FILE_SOURCE, data=head + raw + tail, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        return {"ok": False, "status": exc.code, "detail": detail,
                "seconds": round(time.perf_counter() - started, 2)}
    elapsed = round(time.perf_counter() - started, 2)
    try:
        return {"ok": True, "status": status, "body": json.loads(text), "seconds": elapsed}
    except ValueError:
        return {"ok": False, "status": status, "detail": text[:600], "seconds": elapsed}


# ---------------------------------------------------------------- 一次生成

def stream_run(opener, body: dict, timeout: float = 900.0) -> dict:
    """发一次 SSE 生成，返回每一帧的类型与到达时刻（相对发出请求的秒数）。

    时间线的四档口径（`docs/PLAN.md:130` 要求「分开报」）：
      t_first_frame  第一个 data 帧 = 界面有反应
      t_root         第一个 `node_grow`（`path: null`）= 中心主题上屏
      t_first_branch 第一个 `node_grow`（`path: []`）= 第一条一级分支上屏
      t_snapshot / t_done = 整树进工作副本 / 落到画布
    """
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        STREAM, data=payload, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Accept": "text/event-stream"})
    marks: dict[str, float] = {}
    counts = {"node_grow": 0, "heartbeat": 0, "stage": 0}
    tools: list[dict] = []
    content = None
    error = None
    frames = 0
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                text = line[5:].strip()
                if not text or text == "[DONE]":
                    continue
                try:
                    frame = json.loads(text)
                except ValueError:
                    continue
                frames += 1
                elapsed = round(time.perf_counter() - started, 3)
                kind = frame.get("type") or frame.get("event") or "?"
                marks.setdefault(f"t_{kind}", elapsed)
                if kind == "node_grow":
                    counts["node_grow"] += 1
                    path = frame.get("path")
                    if path is None:
                        marks.setdefault("t_root", elapsed)
                    elif path == []:
                        marks.setdefault("t_first_branch", elapsed)
                elif kind == "heartbeat":
                    counts["heartbeat"] += 1
                elif kind == "stage":
                    counts["stage"] += 1
                elif kind == "done":
                    content = frame.get("content")
                elif kind == "tool_end":
                    # 工具失败的那一刻，原因只在这一帧的 content 里（日志只写
                    # "运行异常"）。不留下它，失败就无法归因。
                    tools.append({"name": frame.get("name"), "ok": frame.get("ok"),
                                  "latency_ms": frame.get("latency_ms"),
                                  "summary": frame.get("result_summary")})
                elif kind == "error":
                    error = {k: frame.get(k) for k in ("code", "message", "limit")}
    except Exception as exc:  # noqa: BLE001 — 断流也要留下已量到的时间线
        error = {"code": "client_error", "message": str(exc)[:300]}
    total = round(time.perf_counter() - started, 2)
    return {"marks": marks, "counts": counts, "frames": frames, "tools": tools,
            "content": content, "error": error, "seconds": total}


# ---------------------------------------------------------------- 结构指纹

def walk(node, depth: int = 1):
    yield node, depth
    for child in node.get("children") or []:
        yield from walk(child, depth + 1)


def fingerprint(content: dict, must: list[str]) -> dict:
    root = (content or {}).get("root") or {}
    pairs = list(walk(root))
    labels = [(node.get("label") or "").strip() for node, _ in pairs]
    details = [(node.get("detail") or "").strip() for node, _ in pairs]
    depth = max((d for _, d in pairs), default=0)
    top = root.get("children") or []
    per_level: dict[int, int] = {}
    for _, d in pairs:
        per_level[d] = per_level.get(d, 0) + 1
    refs = sum(1 for node, _ in pairs if node.get("source_refs"))
    blob = "\n".join(labels + [d for d in details if d])
    hits = [word for word in must if word in blob]
    duplicated = len(labels) - len({label for label in labels if label})
    long_labels = [label for label in labels if len(label) > 24]
    return {
        "nodes": len(pairs),
        "depth": depth,
        "top_branches": len(top),
        "widest_level": max(per_level.values(), default=0),
        "per_level": {str(k): v for k, v in sorted(per_level.items())},
        "top_labels": [(node.get("label") or "").strip() for node in top],
        "label_avg": round(sum(len(label) for label in labels) / len(labels), 1) if labels else 0,
        "label_max": max((len(label) for label in labels), default=0),
        "long_labels": long_labels[:10],
        "duplicated_labels": duplicated,
        "with_detail": sum(1 for d in details if d),
        "detail_avg": round(sum(len(d) for d in details) / len(details), 1) if details else 0,
        "with_source_refs": refs,
        "ref_coverage": round(refs / len(pairs), 3) if pairs else 0,
        "must_hits": hits,
        "must_total": len(must),
        "must_rate": round(len(hits) / len(must), 3) if must else None,
        "must_missing": [word for word in must if word not in blob],
    }


def outline(content: dict) -> str:
    root = (content or {}).get("root") or {}
    if not root:
        return "（无 root）"
    lines = [f"# 中心：{(root.get('label') or '').strip()}"]

    def emit(node, depth: int):
        for child in node.get("children") or []:
            label = (child.get("label") or "").strip()
            detail = (child.get("detail") or "").strip().replace("\n", " ")
            refs = len(child.get("source_refs") or [])
            tail = f"　｜{detail[:60]}" if detail and depth <= 2 else ""
            mark = f" [{refs}引]" if refs else ""
            lines.append(f"{'  ' * depth}- {label}{mark}{tail}")
            emit(child, depth + 1)

    emit(root, 0)
    return "\n".join(lines)


def contract_ok(content) -> tuple[bool, str]:
    """`MindMapContent` 是否合法：直接交回后端契约去验，不自己另写一套判据。"""
    if not isinstance(content, dict) or not content.get("root"):
        return False, "content 为空或没有 root"
    try:
        from app.contracts.mindmap import DiagramContent  # noqa: WPS433
        from pydantic import TypeAdapter  # noqa: WPS433

        TypeAdapter(DiagramContent).validate_python(content)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"契约校验失败：{str(exc)[:200]}"


# ---------------------------------------------------------------- 三个模式

def load_samples() -> list[dict]:
    return json.loads(SAMPLES.read_text(encoding="utf-8"))["samples"]


def mode_structure(args) -> int:
    opener = session()
    samples = load_samples()
    if args.only:
        wanted = {item.strip().upper() for item in args.only.split(",") if item.strip()}
        samples = [s for s in samples if s["id"].upper() in wanted]
    if args.start:
        samples = [s for s in samples if int(s["id"][1:]) >= args.start]
    if args.limit:
        samples = samples[: args.limit]
    out = DATA / "acc_structure.jsonl"
    done = set()
    if out.exists() and not (args.redo or args.only):
        for line in out.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["id"])
            except (ValueError, KeyError):
                continue
    outlines: list[str] = []
    for sample in samples:
        if sample["id"] in done:
            print(f"[skip] {sample['id']} 已量过", flush=True)
            continue
        record: dict = {"id": sample["id"], "category": sample["category"],
                        "title": sample["title"], "goal": sample["goal"]}
        parsed = post_json(opener, TEXT_SOURCE, {"text": sample["text"], "kind": "text"})
        record["source"] = {"ok": parsed["ok"], "status": parsed.get("status"),
                            "seconds": parsed.get("seconds"),
                            "kind": (parsed.get("body") or {}).get("source", {}).get("kind"),
                            "chars": len(((parsed.get("body") or {}).get("source") or {})
                                         .get("text") or ""),
                            "detail": parsed.get("detail")}
        if not parsed["ok"]:
            record.update({"stage_failed": "ingestion", "ok": False})
            _append(out, record)
            print(f"[fail] {sample['id']} 入库失败 {record['source']}", flush=True)
            continue
        source = parsed["body"]["source"]
        body = {"goal": sample["goal"], "intent": "create", "diagram_type": "tree",
                "sources": [source]}
        run = stream_run(opener, body)
        content = run["content"]
        ok, why = contract_ok(content)
        record.update({
            "marks": run["marks"],
            "frame_counts": run["counts"],
            "frames": run["frames"],
            "tools": run["tools"],
            "seconds": run["seconds"],
            "error": run["error"],
            "ok": ok,
            "contract_problem": why,
        })
        if ok:
            record["fingerprint"] = fingerprint(content, sample["must"])
            content_path = DATA / "acc_trees" / f"{sample['id']}.json"
            content_path.parent.mkdir(parents=True, exist_ok=True)
            content_path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
            outlines.append(f"\n\n## {sample['id']} 【{sample['category']}】{sample['title']}"
                            f"\n\n任务：{sample['goal']}\n\n```\n{outline(content)}\n```")
        _append(out, record)
        fp = record.get("fingerprint") or {}
        print(f"[{sample['id']}] {run['seconds']}s  节点 {fp.get('nodes')} "
              f"深 {fp.get('depth')} 一级 {fp.get('top_branches')} "
              f"覆盖 {fp.get('must_rate')} 引用覆盖 {fp.get('ref_coverage')} "
              f"根帧 {run['marks'].get('t_root')}s 完成 {run['marks'].get('t_done')}s "
              f"ok={ok}{'' if ok else ' ' + why}", flush=True)
    if outlines:
        path = DATA / "acc_structure_outline.md"
        with path.open("a", encoding="utf-8") as handle:
            handle.write("".join(outlines))
        print(f"大纲追加到 {path}", flush=True)
    return 0


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


AUDIO = Path(__file__).with_name("acc_media")


def mode_media(args) -> int:
    """多模态：逐个真实样本走上传接口，判「解析出非空 Source」这一步成不成。

    `PLAN.md:129` 的「成功」更严（还要生成合法 `MindMapContent`），所以解析通过的
    样本会继续跑一次生成 —— 那才是分母里该记的「成功进到导图」。
    """
    opener = session()
    folder = AUDIO / args.kind
    if not folder.exists():
        print(f"样本目录不存在：{folder}", file=sys.stderr)
        return 2
    out = DATA / "acc_media.jsonl"
    done = set()
    if out.exists() and not args.redo:
        for line in out.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["file"])
            except (ValueError, KeyError):
                continue
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.name in done:
            continue
        record: dict = {"kind": args.kind, "file": path.name,
                        "bytes": path.stat().st_size}
        up = upload(opener, path.name, path.read_bytes())
        record["upload"] = {"ok": up["ok"], "status": up.get("status"),
                            "seconds": up.get("seconds"),
                            "detail": up.get("detail")}
        source = ((up.get("body") or {}).get("source") if up["ok"] else None)
        record["source_nonempty"] = bool(source and (source.get("text") or "").strip())
        record["source_chars"] = len((source or {}).get("text") or "")
        record["source_kind"] = (source or {}).get("kind")
        if not record["source_nonempty"]:
            record["ok"] = False
            record["stage_failed"] = "ingestion"
            _append(out, record)
            print(f"[{args.kind}] {path.name}: 解析失败 {record['upload']['detail']}",
                  flush=True)
            continue
        # 解析之后必须真能进到导图才算「支持」
        goal = {"image": "把这张图片里的结构整理成思维导图。",
                "audio": "把这段语音的内容整理成思维导图。"}[args.kind]
        run = stream_run(opener, {"goal": goal, "intent": "create",
                                  "diagram_type": "tree", "sources": [source]})
        ok, why = contract_ok(run["content"])
        record.update({"ok": ok, "contract_problem": why,
                       "seconds": run["seconds"], "marks": run["marks"],
                       "error": run["error"]})
        if ok:
            record["fingerprint"] = fingerprint(run["content"], [])
        _append(out, record)
        print(f"[{args.kind}] {path.name}: 解析 {record['source_chars']} 字 → "
              f"生成 ok={ok} {run['seconds']}s", flush=True)
    return 0


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    # 线性插值（与 numpy 默认的 linear 口径一致），n 小的时候也不至于跳档
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (pos - low), 2)


def mode_summary(args) -> int:
    path = DATA / "acc_structure.jsonl"
    if not path.exists():
        print("还没有 acc_structure.jsonl", file=sys.stderr)
        return 2
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    # 同一个 id 可能有多条：失败样本会用 `--only` 重跑。取最后一条（最新的判断）。
    latest: dict[str, dict] = {}
    for row in rows:
        latest[row["id"]] = row
    rows = [latest[key] for key in sorted(latest)]
    ok_rows = [r for r in rows if r.get("ok")]
    print(f"样本 {len(rows)}｜合法 MindMapContent {len(ok_rows)}｜失败 {len(rows) - len(ok_rows)}")
    for key, label in (("__first__", "首帧（第一个 data 帧）"), ("t_root", "根节点"),
                       ("t_first_branch", "首个一级分支"), ("t_snapshot", "整树进副本"),
                       ("t_done", "完整树落地")):
        if key == "__first__":
            # 首帧的具体帧型不固定（有材料时是 `sources`，无材料时是 `stage`），
            # 所以取"最早到达的那一帧"，而不是按名字找。
            values = [min(r["marks"].values()) for r in ok_rows if r.get("marks")]
        else:
            values = [r["marks"][key] for r in ok_rows if r.get("marks", {}).get(key)]
        if not values:
            print(f"  {label}: 无数据")
            continue
        print(f"  {label}: n={len(values)} P50={_percentile(values, 0.5)}s "
              f"P95={_percentile(values, 0.95)}s min={min(values)}s max={max(values)}s")
    by_cat: dict[str, list[dict]] = {}
    for row in ok_rows:
        by_cat.setdefault(row["category"], []).append(row)
    for category, group in sorted(by_cat.items()):
        rates = [g["fingerprint"]["must_rate"] for g in group
                 if g["fingerprint"]["must_rate"] is not None]
        refs = [g["fingerprint"]["ref_coverage"] for g in group]
        nodes = [g["fingerprint"]["nodes"] for g in group]
        print(f"  {category}: n={len(group)} 覆盖均值={round(statistics.mean(rates), 3)} "
              f"引用覆盖均值={round(statistics.mean(refs), 3)} "
              f"节点中位={statistics.median(nodes)}")
    all_rates = [r["fingerprint"]["must_rate"] for r in ok_rows
                 if r["fingerprint"]["must_rate"] is not None]
    if all_rates:
        print(f"  全体覆盖：均值 {round(statistics.mean(all_rates), 3)}"
              f" 中位 {round(statistics.median(all_rates), 3)}"
              f" ≥0.8 的比例 {round(sum(1 for r in all_rates if r >= 0.8) / len(all_rates), 3)}")
    media = DATA / "acc_media.jsonl"
    if media.exists():
        mrows = [json.loads(line) for line in media.read_text(encoding="utf-8").splitlines() if line]
        for kind in ("text", "image", "audio"):
            group = [r for r in mrows if r["kind"] == kind]
            if not group:
                continue
            good = sum(1 for r in group if r.get("ok"))
            print(f"  多模态 {kind}: {good}/{len(group)}")
    return 0


def mode_latency(args) -> int:
    """无材料 create 的端到端时延，串行跑（不与任何测量并发）。

    为什么单独一条：`structure` 那 20 条是**带材料**的复杂场景，`docs/PLAN.md:130`
    的历史数字（41.0 / 72.6 / 139.5 秒）却是**无材料**的。两者不是同一件事，
    混在一张表里报会得出错结论。这里用同一台后端、同一套开关，把无材料那条
    重新量一遍，才能判断"历史数字现在还成不成立"。
    """
    topics = args.topics.split("|") if args.topics else [
        "碳中和的主要方面", "考研数学复习规划", "社区团购配送调度系统"]
    opener = session()
    out = DATA / "acc_latency.jsonl"
    for index, topic in enumerate(topics, 1):
        run = stream_run(opener, {"goal": topic, "intent": "create",
                                  "diagram_type": "tree"})
        ok, why = contract_ok(run["content"])
        record = {"round": index, "topic": topic, "ok": ok, "contract_problem": why,
                  "marks": run["marks"], "seconds": run["seconds"],
                  "frames": run["frames"], "counts": run["counts"],
                  "error": run["error"]}
        if ok:
            record["fingerprint"] = fingerprint(run["content"], [])
        _append(out, record)
        print(f"[{index}/{len(topics)}] {topic}: {run['seconds']}s "
              f"根帧 {run['marks'].get('t_root')}s 完成 {run['marks'].get('t_done')}s "
              f"节点 {(record.get('fingerprint') or {}).get('nodes')} ok={ok}", flush=True)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    p_struct = sub.add_parser("structure")
    p_struct.add_argument("--limit", type=int, default=0)
    p_struct.add_argument("--start", type=int, default=0)
    p_struct.add_argument("--only", default="", help="只跑这些 id，逗号分隔（重跑失败样本用）")
    p_struct.add_argument("--redo", action="store_true")
    p_media = sub.add_parser("media")
    p_media.add_argument("--kind", required=True, choices=["text", "image", "audio"])
    p_media.add_argument("--redo", action="store_true")
    p_latency = sub.add_parser("latency")
    p_latency.add_argument("--topics", default="", help="用 | 分隔；缺省跑三个固定主题")
    sub.add_parser("summary")
    args = parser.parse_args(argv)
    if args.mode == "structure":
        return mode_structure(args)
    if args.mode == "media":
        return mode_media(args)
    if args.mode == "latency":
        return mode_latency(args)
    return mode_summary(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
