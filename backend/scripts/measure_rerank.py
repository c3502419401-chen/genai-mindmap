"""测量重排（`knowledge/rerank.py`）值不值：分数分布、指标变化、单次耗时。

三件事，各自回答一个具体问题：

1. **分数分得开吗**（`--local-model` 或真端点）。父块是 450–700 字的粗颗粒，
   而 cross-encoder 一般按句子级相关性训练。如果给 12–40 条父块打分之后，分数
   挤在很窄的区间里（比如极差 < 0.05、top1 与 top2 差 < 0.01），那说明模型
   在这个粒度上没有有效信号——重排的收益会被"父块本身就很粗"吃掉。
   这里逐次打印 n / min / p50 / max / 极差 / 标准差 / top1−top2 间隔。
2. **指标涨了吗**。用 `scripts/evaluate_retrieval.py` 的 `score_config` 跑同一套
   manual case：一次 `MINDMAP_RERANK=0`，一次把 `rerank.score_documents` 换成
   本地 cross-encoder。**接入点、排序、降级、日志全都走产品代码**，只有"分数
   从哪来"不同。所以两次的差就是"重排这一步"的净效果。
3. **一次调用多贵**。逐次记录候选条数与耗时。

两种分数来源：
  - `--local-model PATH`：本地 cross-encoder（CPU）。**这是替身**，不是百炼的
    `gte-rerank`，读数不能冒充线上表现；它的用处是在没有凭证的机器上量出
    "粒度这件事本身"的影响。注意 CPU 上很慢：40 条 600 字候选约 13 秒/次，
    整轮 manual 套要 5–6 分钟（`qwen3-rerank` 同样 40 条只要 0.9 秒）。
  - 不带参数：走 `rerank.score_documents` 打真端点，需要
    `MINDMAP_EMBEDDING_API_KEY`（或 `DASHSCOPE_API_KEY`）。这条路还要 `--suite`
    才会跑指标。

`--latency-only` 只测端点往返：没有 key 也能测，因为它量的是"多一次 HTTP 调用"
的固定成本（TLS 握手 + 往返），401 与 200 在这段上没有区别。
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
for entry in (BACKEND, BACKEND / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import evaluate_retrieval as ev                          # noqa: E402
from app.knowledge import rerank                         # noqa: E402

TOP_K = (1, 3, 5, 10)


# ---------------------------------------------------------------- 分数来源


def local_scorer(model_dir: str):
    """本地 cross-encoder：`(query, passage)` 配对打分，返回 0–1。

    `tokenizer(queries, passages)` 就是 sentence-transformers `CrossEncoder` 内部的
    那一步（`query </s></s> passage`），所以不需要装 sentence-transformers。
    sigmoid 把 logits 压到 0–1，与 `gte-rerank` 的 `relevance_score` 同一量纲，
    这样两边的分布可以放在一起看。
    """
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_dir)
    model = AutoModelForSequenceClassification.from_pretrained(model_dir)
    model.eval()
    # 2C2G 上开满线程只会互相抢；4 是"比默认少一点、又不太慢"的折中。
    torch.set_num_threads(min(4, os.cpu_count() or 1))

    def score(query: str, documents: list[str]) -> list[float]:
        with torch.inference_mode():
            batch = tokenizer(
                [query] * len(documents), list(documents),
                padding=True, truncation=True, max_length=512, return_tensors="pt",
            )
            logits = model(**batch).logits.reshape(-1).float()
            return [float(value) for value in torch.sigmoid(logits)]

    return score


def http_scorer():
    """真端点：包一层计时，其余行为与产品代码一致。"""

    def score(query: str, documents: list[str]) -> list[float]:
        return rerank.score_documents(query, documents)

    return score


class Recorder:
    """记每一次打分的候选数、耗时、分数分布。"""

    def __init__(self, score):
        self.score = score
        self.rows: list[dict] = []

    def __call__(self, query: str, documents: list[str]) -> list[float]:
        started = time.perf_counter()
        values = self.score(query, documents)
        elapsed = (time.perf_counter() - started) * 1000
        ordered = sorted(values, reverse=True)
        gap = ordered[0] - ordered[1] if len(ordered) > 1 else 0.0
        self.rows.append({
            "query": query,
            "n": len(documents),
            "ms": elapsed,
            "min": min(values) if values else 0.0,
            "p50": statistics.median(values) if values else 0.0,
            "max": max(values) if values else 0.0,
            "range": (max(values) - min(values)) if values else 0.0,
            "stdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
            "gap": gap,
        })
        return values


# ---------------------------------------------------------------- 报告


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _table(rows: list[list[str]]) -> str:
    widths = [max(len(line[index]) for line in rows) for index in range(len(rows[0]))]
    lines = []
    for line in rows:
        lines.append("  ".join(
            cell.rjust(width) if index else cell.ljust(width)
            for index, (cell, width) in enumerate(zip(line, widths))))
    return "\n".join(lines)


def print_distribution(recorder: Recorder) -> None:
    """分数分布：回答"父块这么粗，分数分得开吗"。"""
    rows = recorder.rows
    if not rows:
        print("没有打分记录（候选池不比 top_k 多时不会调用重排）。")
        return
    print()
    print(f"分数分布（{len(rows)} 次调用；候选 {rows[0]['n']} 条上下）：")
    table = [["#", "候选", "min", "p50", "max", "极差", "标准差", "top1−top2", "耗时ms"]]
    for index, row in enumerate(rows, 1):
        table.append([
            str(index), str(row["n"]),
            f"{row['min']:.4f}", f"{row['p50']:.4f}", f"{row['max']:.4f}",
            f"{row['range']:.4f}", f"{row['stdev']:.4f}", f"{row['gap']:.4f}",
            f"{row['ms']:.0f}",
        ])
    print(_table(table))

    def summary(name: str, values: list[float]) -> str:
        return (f"  {name}: 中位 {statistics.median(values):.4f} / "
                f"最小 {min(values):.4f} / 最大 {max(values):.4f}")

    print()
    print("跨调用汇总：")
    print(summary("极差 ", [row["range"] for row in rows]))
    print(summary("标准差", [row["stdev"] for row in rows]))
    print(summary("top1−top2 间隔", [row["gap"] for row in rows]))
    print(summary("单次耗时/ms", [row["ms"] for row in rows]))
    narrow = sum(1 for row in rows if row["stdev"] < 0.05)
    print(f"  标准差 < 0.05 的调用：{narrow}/{len(rows)}"
          "（这一档说明模型几乎没在候选之间拉开差距）")


def print_latency(recorder: Recorder) -> None:
    rows = recorder.rows
    if not rows:
        return
    times = [row["ms"] for row in rows]
    total = sum(times)
    print()
    print(f"单次重排耗时（候选 {min(r['n'] for r in rows)}–{max(r['n'] for r in rows)} 条）："
          f"中位 {statistics.median(times):.0f}ms / 均值 {total / len(times):.0f}ms / "
          f"最快 {min(times):.0f}ms / 最慢 {max(times):.0f}ms")
    budget = 5000.0
    print(f"  按 5 秒检索预算算占比：中位 {statistics.median(times) / budget:.1%} / "
          f"最慢 {max(times) / budget:.1%}")


def measure_endpoint_latency(rounds: int = 6) -> None:
    """端点往返：没有 key 也能测，量的是"多一次 HTTP 调用"的固定成本。

    每次新建 `httpx.Client`（与 `rerank._post` 同形），所以含 TLS 握手——那正是
    真实路径上每次都要付的钱。
    """
    import httpx

    url = f"{rerank.PUBLIC_ROOT}{rerank.RERANK_PATH}"
    payload = {"model": rerank.DEFAULT_MODEL,
               "input": {"query": "预算审批", "documents": ["占位候选"] * 12},
               "parameters": {"return_documents": False, "top_n": 12}}
    print()
    print(f"端点往返（{url}，无 key 时 401 也走完 TLS + 往返）：")
    samples: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=httpx.Timeout(rerank.TIMEOUT)) as client:
                response = client.post(url, json=payload,
                                       headers={"Authorization": "Bearer invalid"})
            status = response.status_code
        except httpx.HTTPError as exc:               # pragma: no cover - 离线机器
            print(f"  · 连接失败：{type(exc).__name__}")
            return
        elapsed = (time.perf_counter() - started) * 1000
        samples.append(elapsed)
        print(f"  · {elapsed:6.0f}ms  HTTP {status}")
    print(f"  中位 {statistics.median(samples):.0f}ms / 最小 {min(samples):.0f}ms / "
          f"最大 {max(samples):.0f}ms")


def run_suite(dataset, config) -> dict:
    """跑一遍 manual 套（复用评估脚本的打分器，一行代码都不重写指标）。

    `score_config` 返回的是**一个配置行**；`render_table` 要的是"整套报告"，
    也就是 `{"top_k": [...], "configs": [行]}`。在这里补上那层壳，而不是在评估
    脚本里加一个只被本脚本用到的分支。
    """
    with ev._id_space(dataset.materials, need_library=False) as id_map:
        row = ev.score_config(dataset, config, TOP_K, id_map)
    return {"suite": dataset.suite, "top_k": list(TOP_K), "configs": [row]}


# ---------------------------------------------------------------- 主流程


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="测量重排：分数分布 / manual 套指标变化 / 单次耗时。")
    parser.add_argument("--local-model", metavar="PATH",
                        help="本地 cross-encoder 目录（替身打分；不需要凭证）")
    parser.add_argument("--suite", choices=("manual", "title"), default="manual",
                        help="跑哪套指标（默认 manual：接近真实那套）")
    parser.add_argument("--latency-only", action="store_true",
                        help="只测端点往返，不跑指标")
    parser.add_argument("--without-baseline", action="store_true",
                        help="跳过 rerank=0 那一遍（省一半时间，只想要分数分布时用）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.latency_only:
        measure_endpoint_latency()
        return 0

    materials = ev.load_materials(list(ev.DEFAULT_MATERIALS), [])
    dataset = ev.build_manual_dataset(materials) if args.suite == "manual" \
        else ev.build_dataset(materials)
    config = ev.Config(source_mode="rag", hybrid=False, parent=True, library=False)
    print(f"测试集：{dataset.suite} 套 / {len(dataset.cases)} 条 case / "
          f"{len(materials)} 份材料")

    # 重排开关必须打开；凭证只为让 `configured()` 通过——替身模式下不会真发请求。
    os.environ["MINDMAP_RERANK"] = "1"
    os.environ.setdefault("MINDMAP_EMBEDDING_API_KEY", "local-proxy")
    os.environ["MINDMAP_PARENT_RETRIEVAL"] = "1"

    if not args.without_baseline:
        os.environ["MINDMAP_RERANK"] = "0"
        started = time.perf_counter()
        baseline = run_suite(dataset, config)
        print(f"\n[rerank=0] 耗时 {time.perf_counter() - started:.1f}s")
        print(ev.render_table(baseline))
        os.environ["MINDMAP_RERANK"] = "1"

    recorder = Recorder(local_scorer(args.local_model) if args.local_model
                        else http_scorer())
    original = rerank.score_documents
    rerank.score_documents = recorder        # 产品代码的接入点原样保留
    try:
        started = time.perf_counter()
        candidate = run_suite(dataset, config)
    finally:
        rerank.score_documents = original

    label = "本地 cross-encoder 替身" if args.local_model else "真端点 gte-rerank"
    print(f"\n[rerank=1 / {label}] 耗时 {time.perf_counter() - started:.1f}s")
    print(ev.render_table(candidate))
    print_distribution(recorder)
    print_latency(recorder)

    if not args.without_baseline:
        print()
        print("逐指标差（rerank=1 − rerank=0）：")
        base_row = baseline["configs"][0]
        cand_row = candidate["configs"][0]
        rows = [["指标", *[f"@{k}" for k in TOP_K]]]
        for name in ev.METRIC_NAMES:
            rows.append([name, *[
                f"{(cand_row['metrics'][name][str(k)] - base_row['metrics'][name][str(k)]) * 100:+.1f}pp"
                for k in TOP_K
            ]])
        print(_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
