"""汇总盲评：对回代号、算一致率、与上一轮 45% 逐条比。

输入：
    data/blind/key.json                     代号 → 真实样本（评分完成后才读）
    data/scores-blind-lcs.json              本次人工评分（20 条 × 4 维 + 理由）
    data/blind/judge-deepseek-chat-new-*.json   第二个评分者（两遍）
    data/blind/judge-deepseek-chat-old-*.json   第二个评分者对旧树的评分
输出：
    data/blind/summary.json / summary.md

用法：
    python scripts/_blind_agree.py
"""
from __future__ import annotations

import json
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
BLIND = BACKEND / "data" / "blind"
DIMS = ("cover", "hierarchy", "nofabricate", "readable")
PASS_MIN, PASS_TOTAL = 3, 14

# docs/ACCEPTANCE-METRICS.md §2.3（上一轮单人评分，评分对象是 acc_trees-before-fix 里
# 未被 01:17 那次重跑覆盖的树 + 已丢失的跑题树）。原样抄录，用于逐条对照。
OLD = {
    "S01": (5, 4, 5, 3), "S02": (5, 3, 5, 3), "S03": (4, 4, 5, 4), "S04": (4, 4, 3, 3),
    "S05": (4, 4, 5, 4), "S06": (5, 4, 4, 4), "S07": (5, 4, 4, 4), "S08": (2, 4, 2, 4),
    "S09": (2, 4, 2, 4), "S10": (2, 4, 2, 4), "S11": (2, 4, 5, 4), "S12": (4, 4, 5, 4),
    "S13": (3, 3, 4, 3), "S14": (2, 4, 5, 4), "S15": (3, 4, 2, 4), "S16": (4, 4, 3, 4),
    "S17": (1, 1, 1, 1), "S18": (4, 4, 2, 3), "S19": (1, 1, 1, 1), "S20": (1, 1, 1, 1),
}


def passed(scores) -> bool:
    return all(v >= PASS_MIN for v in scores) and sum(scores) >= PASS_TOTAL


def stats(rows: dict[str, tuple[int, int, int, int]]) -> dict:
    n = len(rows)
    ok = sum(1 for s in rows.values() if passed(s))
    return {
        "n": n,
        "pass": ok,
        "pass_rate": round(ok / n, 3),
        "total_mean": round(sum(sum(s) for s in rows.values()) / n, 2),
        "dim_mean": {d: round(sum(s[i] for s in rows.values()) / n, 2)
                     for i, d in enumerate(DIMS)},
    }


def agree(a: dict[str, tuple], b: dict[str, tuple]) -> dict:
    common = sorted(set(a) & set(b))
    exact = sum(1 for c in common if a[c] == b[c])
    cells = len(common) * 4
    cell_exact = sum(1 for c in common for i in range(4) if a[c][i] == b[c][i])
    cell_near = sum(1 for c in common for i in range(4) if abs(a[c][i] - b[c][i]) <= 1)
    pass_same = sum(1 for c in common if passed(a[c]) == passed(b[c]))
    return {
        "rows": len(common),
        "row_exact": exact,
        "row_exact_rate": round(exact / len(common), 3),
        "cell_exact": cell_exact,
        "cell_exact_rate": round(cell_exact / cells, 3),
        "cell_within1_rate": round(cell_near / cells, 3),
        "pass_same": pass_same,
        "pass_same_rate": round(pass_same / len(common), 3),
        "disagreements": [
            {"sample": c, "a": list(a[c]), "b": list(b[c]),
             "diff": [a[c][i] - b[c][i] for i in range(4)]}
            for c in common if a[c] != b[c]],
    }


def load_judge(name: str) -> dict[str, tuple]:
    path = BLIND / name
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {row["code"]: tuple(row[d] for d in DIMS)
            for row in payload["rows"] if row.get("ok")}


def main() -> int:
    key = json.loads((BLIND / "key.json").read_text(encoding="utf-8"))
    lcs_raw = json.loads((BACKEND / "data" / "scores-blind-lcs.json")
                         .read_text(encoding="utf-8"))["rows"]
    lcs = {r["code"]: tuple(r[d] for d in DIMS) for r in lcs_raw}
    new_code2sid = {code: meta["sample"] for code, meta in key["new"].items()}
    old_code2sid = {code: meta["sample"] for code, meta in key["old"].items()}

    ds1 = load_judge("judge-deepseek-chat-new-r1.json")
    ds2 = load_judge("judge-deepseek-chat-new-r2-topic.json")
    ds_old = load_judge("judge-deepseek-chat-old-r1-topic.json")
    topics = {row["code"]: row.get("topic")
              for row in json.loads((BLIND / "judge-deepseek-chat-new-r2-topic.json")
                                    .read_text(encoding="utf-8"))["rows"]}

    lcs_by_sid = {new_code2sid[c]: s for c, s in lcs.items()}
    ds1_by_sid = {new_code2sid[c]: s for c, s in ds1.items()}
    ds2_by_sid = {new_code2sid[c]: s for c, s in ds2.items()}
    old_by_sid = {old_code2sid[c]: s for c, s in ds_old.items()}
    topic_by_sid = {new_code2sid[c]: topics.get(c) for c in lcs}

    out = {
        "new_lcs": stats(lcs_by_sid),
        "new_ds1": stats(ds1_by_sid),
        "new_ds2": stats(ds2_by_sid),
        "old_ds": stats(old_by_sid),
        "old_doc": stats({k: tuple(v) for k, v in OLD.items()}),
        "agree_lcs_ds1": agree(lcs, ds1),
        "agree_lcs_ds2": agree(lcs, ds2),
        "agree_ds1_ds2": agree(ds1, ds2),
        "topic": {
            "generic": sum(1 for v in topic_by_sid.values() if v == "generic"),
            "specific": sum(1 for v in topic_by_sid.values() if v == "specific"),
            "by_sample": topic_by_sid,
        },
        "per_sample": {},
    }

    for sid in sorted(OLD):
        row = {
            "old": list(OLD[sid]),
            "lcs": list(lcs_by_sid[sid]) if sid in lcs_by_sid else None,
            "ds1": list(ds1_by_sid[sid]) if sid in ds1_by_sid else None,
            "ds2": list(ds2_by_sid[sid]) if sid in ds2_by_sid else None,
            "old_tree_kept": list(old_by_sid[sid]) if sid in old_by_sid else None,
            "topic": topic_by_sid.get(sid),
        }
        if row["old"] and row["lcs"]:
            row["delta_total"] = sum(row["lcs"]) - sum(row["old"])
            row["delta_dim"] = [row["lcs"][i] - row["old"][i] for i in range(4)]
            row["old_pass"] = passed(tuple(row["old"]))
            row["lcs_pass"] = passed(tuple(row["lcs"]))
        out["per_sample"][sid] = row

    (BLIND / "summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                       encoding="utf-8")

    # ---- markdown ----
    lines = ["# 盲评汇总（脚本生成，勿手改）", ""]
    for name, key_name in [("本轮 LCS 人工盲评", "new_lcs"),
                           ("deepseek 第 1 遍", "new_ds1"),
                           ("deepseek 第 2 遍（含切题题）", "new_ds2"),
                           ("deepseek 对旧树 13 条", "old_ds"),
                           ("上一轮 45% 的原始分（文档）", "old_doc")]:
        s = out[key_name]
        lines.append(f"- {name}: n={s['n']} 合格 {s['pass']} = {s['pass_rate']*100:.0f}%，"
                     f"总分均值 {s['total_mean']}，各维 " +
                     " / ".join(f"{d[:4]} {v}" for d, v in s["dim_mean"].items()))
    lines += ["", "## 一致率", ""]
    for name in ("agree_lcs_ds1", "agree_lcs_ds2", "agree_ds1_ds2"):
        a = out[name]
        lines.append(f"- {name}: 逐维完全相同 {a['cell_exact']}/{a['rows']*4} "
                     f"= {a['cell_exact_rate']*100:.0f}%，±1 内 {a['cell_within1_rate']*100:.0f}%，"
                     f"整条四维全同 {a['row_exact']}/{a['rows']}，"
                     f"合格判定相同 {a['pass_same']}/{a['rows']}")
    lines += ["", "## 逐条对照（S 编号）", "",
              "| 样本 | 旧分 | 本轮 LCS | Δ总分 | ds1 | ds2 | 旧树重打 | 切题 |",
              "|---|---|---|---|---|---|---|---|"]
    for sid, row in out["per_sample"].items():
        fmt = lambda v: "/".join(str(x) for x in v) if v else "—"  # noqa: E731
        lines.append(f"| {sid} | {fmt(row['old'])} | {fmt(row['lcs'])} | "
                     f"{row.get('delta_total', '—'):+} | {fmt(row['ds1'])} | "
                     f"{fmt(row['ds2'])} | {fmt(row['old_tree_kept'])} | "
                     f"{row['topic'] or '—'} |")
    (BLIND / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
