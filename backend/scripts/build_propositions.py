"""把观测压成命题，并把结果打印出来（含**回指核对**）。

这是"事件片"的手工入口。生产上的自动入口在 `summarize.schedule()` 起的后台线程里
（一次 run 开始时判定"上一段工作结束了"，顺手归纳一次）；这个脚本用于三件事：

  1. 第一次把历史数据填成观测与命题（不用等下一次 run）；
  2. 在改过提示词或阈值之后重跑一次，看产出的命题是不是还说得通；
  3. **核对出处**：每条命题都拿 `observation_ids` 回指到 `runs` / `edit_events`
     的具体行，数一遍。数不到就是"出处是编的"，那是这套东西唯一不能接受的失败。

用法（在 `backend` 目录下）：

    python scripts/build_propositions.py --dry-run      # 只抽观测，不调模型
    python scripts/build_propositions.py                # 抽观测 + 归纳
    python scripts/build_propositions.py --rounds 5     # 积压多时多跑几轮
    python scripts/build_propositions.py --db other.db

模型配置来自环境变量（`MINDMAP_LLM_*`）：生产用的那份在仓库根目录的
`run-backend.bat` 里。没配时脚本不会写任何东西——观测也**不**标记成已消费，可以
配置好之后再跑一次。
"""
from __future__ import annotations

import argparse
import sys
from contextlib import closing
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.memory import observations, propositions  # noqa: E402
from app.mindmaps import repository  # noqa: E402


def _count_refs(item_ids: list[str]) -> tuple[int, int, list[str]]:
    """把一批命题的出处数成"能查到几行 run / 几行 edit_event"。

    返回 `(runs, events, missing)`。`missing` 是回指断掉的观测 id——它不该出现，
    出现就说明边表里有一行指向了不存在的观测（删除过、或者写坏了）。
    """
    refs = observations.resolve_refs(
        [obs_id for item in proposals_of(item_ids) for obs_id in item.observation_ids]
    )
    runs = events = 0
    with closing(repository.connect()) as conn:
        if refs["run_ids"]:
            marks = ",".join("?" * len(refs["run_ids"]))
            runs = conn.execute(
                f"SELECT COUNT(*) FROM runs WHERE run_id IN ({marks})", refs["run_ids"],
            ).fetchone()[0]
        if refs["event_ids"]:
            marks = ",".join("?" * len(refs["event_ids"]))
            events = conn.execute(
                f"SELECT COUNT(*) FROM edit_events WHERE id IN ({marks})", refs["event_ids"],
            ).fetchone()[0]
    return runs, events, refs["missing"]


def proposals_of(item_ids: list[str]) -> list[propositions.Proposal]:
    """按 id 取命题（保持传入顺序）。"""
    found = {item.item_id: item for item in propositions.proposals(status=None)}
    return [found[item] for item in item_ids if item in found]


def report(items: list[propositions.Proposal], *, show: int) -> None:
    print(f"\n## 命题（有效 {propositions.count()} 条，被替换 "
          f"{propositions.count(status=propositions.STATUS_SUPERSEDED)} 条，"
          f"边 {propositions.edge_count()} 条）")
    if not items:
        print("  （没有命题）")
        return
    for item in items[:show]:
        runs, events, missing = _count_refs([item.item_id])
        print(f"  - {item.text}")
        print(f"    confidence {item.confidence} / decay {item.decay} "
              f"（权重 {propositions.persistence(item.decay):.2f}）｜"
              f"支撑 {item.support} 条观测 → {runs} 行 run、{events} 行 edit_event"
              + (f"｜**回指断了 {len(missing)} 条**" if missing else ""))
        if item.reasoning:
            print(f"    依据：{item.reasoning}")
        if item.status != propositions.STATUS_ACTIVE:
            print(f"    （已被 {str(item.superseded_by)[:8]} 替换）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="库路径（默认用 MINDMAP_DB_PATH）")
    parser.add_argument("--rounds", type=int, default=5, help="最多归纳几轮")
    parser.add_argument("--show", type=int, default=30, help="打印多少条命题")
    parser.add_argument("--dry-run", action="store_true", help="只抽观测，不调模型")
    args = parser.parse_args(argv)

    if args.db is not None:
        import os

        os.environ["MINDMAP_DB_PATH"] = str(args.db.resolve())
    print(f"库：{repository.db_path()}")

    before = observations.count()
    captured = observations.capture()
    print(f"观测：新增 run {captured.runs} 条 / edit {captured.edits} 条；"
          f"共 {observations.count()} 条（原 {before} 条），"
          f"待归纳 {len(observations.unconsumed(propositions.MAX_INPUT_OBSERVATIONS * 10))} 条")

    if args.dry_run:
        print("\n--dry-run：不调用模型，观测保持未消费状态。")
        for item in observations.recent(10):
            print(f"  [{item.at[:16]}] {item.summary}")
        return 0

    results: list[propositions.Induction] = []
    for _ in range(max(1, args.rounds)):
        outcome = propositions.induce()
        results.append(outcome)
        print(f"归纳：status={outcome.status} 观测 {outcome.observations} 条 → "
              f"候选 {outcome.drafts} 条（丢弃 {outcome.dropped}）；"
              f"新建 {outcome.created}、合并 {outcome.consolidated}、"
              f"命中 {outcome.identical}"
              + (f"｜{outcome.detail}" if outcome.detail else ""))
        if outcome.status != "done":
            break

    report(propositions.proposals(limit=args.show), show=args.show)
    if any(item.created or item.consolidated for item in results):
        return 0
    # 一条都没建也不是错误（合法结论就是 0 条），但用非零退出码让人在脚本里能看出来。
    return 3


if __name__ == "__main__":
    sys.exit(main())
