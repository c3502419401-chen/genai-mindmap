"""把 `two_arch.json` + 盲评结果合成报告要用的几张表（markdown，可直接贴进 docs）。

盲评结果放在 `data/two-arch-judge/*.json`（每个评审者一份，格式见
`docs/TWO-ARCH-COMPARISON.md` 第 3 节），本脚本按 `_key.json` 把 anon 名解回
(组, 臂, 次)，再算"每百节点多余重复数"。
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
ROWS = DATA / "two_arch.json"
BLIND = DATA / "two-arch-blind"
JUDGE = DATA / "two-arch-judge"


def load_rows() -> list[dict]:
    return json.loads(ROWS.read_text(encoding="utf-8"))


def load_key() -> dict:
    path = BLIND / "_key.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def load_judge() -> dict[str, int]:
    """图号 -> 多余重复处数。多份评审文件合并；同一图号出现两次时取后者。"""
    out: dict[str, int] = {}
    if not JUDGE.exists():
        return out
    for path in sorted(JUDGE.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload if isinstance(payload, list) else payload.get("items") or []
        for item in items:
            anon = str(item.get("图") or "").strip()
            count = item.get("多余重复处数")
            if anon and isinstance(count, int):
                out[anon] = count
    return out


def table_runs(rows: list[dict]) -> None:
    print("### 每张图（24 张）\n")
    print("| 组 | 臂 | 次 | 节点 | 一级 | 深 | 调用 | 输入tk | 输出tk | 墙钟 | 机械重复 | 每百 | 可引用覆盖 | 材料覆盖 | 部分 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        if not row.get("ok"):
            print(f"| {row['group']} | {row['arm']} | {row['run']} | 失败 | | | | | | | | | | | |")
            continue
        print(f"| {row['group']} | {row['arm']} | {row['run']} | {row['node_count']} | "
              f"{row['branches']} | {row['max_depth']} | {row['model_calls']} | "
              f"{row['prompt_tokens']} | {row['completion_tokens']} | {row['seconds']}s | "
              f"{row['dup_places']} | {row['dup_per_100']:.2f} | "
              f"{row['citable_coverage']:.0%} | {row['source_coverage']:.0%} | "
              f"{'是' if row.get('partial') else '—'} |")


def table_arms(rows: list[dict], judge: dict[str, int], key: dict) -> None:
    print("\n### 每臂汇总\n")
    print("| 指标 | 整树（一次写完+修复+自检） | 逐支（骨架→按支展开→验收→补支） |")
    print("|---|---|---|")
    arms = {}
    for arm in ("A", "B"):
        ok = [row for row in rows if row["arm"] == arm and row.get("ok")]
        nodes = sum(row["node_count"] for row in ok)
        dup = sum(row["dup_places"] for row in ok)
        judged = [court for court in ok
                  if judge.get(_anon_of(court, key)) is not None]
        dup_j = sum(judge[_anon_of(row, key)] for row in judged)
        nodes_j = sum(row["node_count"] for row in judged)
        arms[arm] = {
            "图": len(ok), "节点合计": nodes,
            "节点中位": statistics.median([row["node_count"] for row in ok]),
            "机械重复": dup, "机械每百": dup / max(1, nodes) * 100,
            "人工图": len(judged), "人工重复": dup_j,
            "人工每百": dup_j / max(1, nodes_j) * 100 if nodes_j else float("nan"),
            "有重复图": sum(1 for row in judged if judge[_anon_of(row, key)]),
            "调用均": statistics.mean([row["model_calls"] for row in ok]),
            "调用合计": sum(row["model_calls"] for row in ok),
            "输入tk": sum(row["prompt_tokens"] for row in ok),
            "输出tk": sum(row["completion_tokens"] for row in ok),
            "墙钟均": statistics.mean([row["seconds"] for row in ok]),
            "墙钟合计": sum(row["seconds"] for row in ok),
            "可引用覆盖": statistics.mean([row["citable_coverage"] for row in ok]),
            "材料覆盖": statistics.mean([row["source_coverage"] for row in ok]),
            "部分交付": sum(1 for row in ok if row.get("partial")),
            "detail中位": statistics.median([row["detail_chars_mean"] for row in ok]),
        }
    keys = [
        ("图（成功）", "图"), ("节点合计", "节点合计"), ("节点中位", "节点中位"),
        ("机械重复 处数", "机械重复"), ("机械 每百节点", "机械每百"),
        ("人工判 有图数（分母）", "人工图"), ("人工判 多余重复 处数", "人工重复"),
        ("人工判 每百节点", "人工每百"), ("人工判 有重复的图数", "有重复图"),
        ("调用数 均值", "调用均"), ("调用数 合计", "调用合计"),
        ("输入 token 合计", "输入tk"), ("输出 token 合计", "输出tk"),
        ("墙钟 均值（秒）", "墙钟均"), ("墙钟 合计（秒）", "墙钟合计"),
        ("可引用覆盖率 均值", "可引用覆盖"), ("材料份数覆盖 均值", "材料覆盖"),
        ("部分交付 图数", "部分交付"), ("detail 长度 中位", "detail中位"),
    ]
    for label, key_name in keys:
        left, right = arms["A"][key_name], arms["B"][key_name]
        print(f"| {label} | {_fmt(left)} | {_fmt(right)} |")


def _fmt(value: float) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _anon_of(row: dict, key: dict) -> str:
    for anon, meta in key.items():
        if (meta.get("group"), meta.get("arm"), meta.get("run")) == (
                row["group"], row["arm"], row["run"]):
            return anon
    return ""


def table_pairs(rows: list[dict], judge: dict[str, int], key: dict) -> None:
    print("\n### 按组配对（同一组=同一主题+同一批材料）\n")
    print("| 组 | 主题 | 材料 | 整树 节点/人工重复/每百 | 逐支 节点/人工重复/每百 |")
    print("|---|---|---|---|---|")
    groups = {row["group"]: row for row in rows if row.get("ok")}
    for group in ("S", "M", "L", "X", "P1", "P2"):
        block = [row for row in rows if row["group"] == group and row.get("ok")]
        if not block:
            continue
        cells = []
        for arm in ("A", "B"):
            side = [row for row in block if row["arm"] == arm]
            nodes = sum(row["node_count"] for row in side)
            dup = sum(judge.get(_anon_of(row, key), 0) for row in side)
            cells.append(f"{nodes} / {dup} / {dup / max(1, nodes) * 100:.2f}")
        sample = block[0]
        print(f"| {group} | {sample['topic']} | {len(sample['materials'])} 份 "
              f"{sample['material_chars']} 字 | {cells[0]} | {cells[1]} |")


def main() -> int:
    rows = load_rows()
    key = load_key()
    judge = load_judge()
    print(f"<!-- 由 scripts/_two_arch_tables.py 生成；{len(rows)} 行，盲评覆盖 {len(judge)} 张 -->")
    table_runs(rows)
    table_arms(rows, judge, key)
    table_pairs(rows, judge, key)
    if not judge:
        print("\n（还没有盲评结果：`data/two-arch-judge/*.json` 为空或缺）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
