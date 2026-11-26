"""生成"盲评包"：把已生成的树压成打分卡，并随机编号。

为什么需要它：`backend/data/acc_score_cards.md` 是按 `S01…S20` 排的，编号本身带着
类别意义（S01–S07 课程大纲、S08–S14 会议纪要、S15–S20 立项），而且卡片里直接印了
`覆盖 0.714` / `缺 ['带水印PDF']` —— 那是"覆盖"这一维的答案。看着它打覆盖分，
等于把分抄一遍。

本脚本做两件事：
1. 抹掉 S 编号 → `R01…`（新树）/ `O01…`（旧树），并按固定 seed 打乱顺序；
2. 抹掉所有自动指标（must_rate / ref_coverage / 重复 / 超长 label 提示），只留
   评分者判分真正需要的东西：材料原文、must 要素清单、全量 label 大纲、detail 抽检。

映射写在 `key.json`，评分全部完成后才对回。

用法：
    python scripts/_blind_pack.py --seed 20261127
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DATA = BACKEND / "data"
SAMPLES = Path(__file__).with_name("acc_samples.json")
OUT = DATA / "blind"

# 这批 before-fix 树文件是 2026-11-27 01:17 之后被重跑覆盖过的（S08/S09/S10/S14/S17/
# S19/S20），它们已经不是 45% 那一轮的产物。只取更早的 13 棵作为"旧树"。
OLD_CUTOFF = datetime(2026, 11, 27, 1, 0, 0).timestamp()


def walk(node, depth=1):
    yield node, depth
    for child in node.get("children") or []:
        yield from walk(child, depth + 1)


def outline(root) -> str:
    """整棵树的 label 大纲，缩进表示层级。"""
    lines: list[str] = []
    for node, depth in walk(root):
        label = (node.get("label") or "").strip() or "(空 label)"
        lines.append("  " * (depth - 1) + ("- " if depth > 1 else "· ") + label)
    return "\n".join(lines)


def pick_details(root, k: int = 4) -> list[tuple[str, str, int]]:
    """抽检 detail：2 条一级、1 条二级、1 条最深层，避免只看到顶层概述。"""
    pairs = [(n, d) for n, d in walk(root) if (n.get("detail") or "").strip()]
    out: list[tuple[str, str, int]] = []
    seen: set[int] = set()

    def take(cands, limit):
        for node, depth in cands:
            if limit <= 0:
                return
            if id(node) in seen:
                continue
            seen.add(id(node))
            out.append(((node.get("label") or "").strip(),
                        (node.get("detail") or "").strip(), depth))
            limit -= 1

    take([p for p in pairs if p[1] == 1], 2)
    take([p for p in pairs if p[1] == 2], 1)
    take(sorted(pairs, key=lambda p: -p[1]), 1)
    take(pairs, k - len(out))
    return out[:k]


def card(code: str, text: str, must: list[str], content: dict) -> str:
    root = content["root"]
    pairs = list(walk(root))
    top = root.get("children") or []
    fan = [len(n.get("children") or []) for n in top]
    lines = [
        f"### {code}",
        f"【材料】{text.strip()}",
        "【必须出现的内容要素（must）】" + " ｜ ".join(must),
        f"【树】中心「{(root.get('label') or '').strip()}」　共 {len(pairs)} 节点　"
        f"最大深度 {max(d for _, d in pairs)}　一级分支 {len(top)} 条（每支扇出 {fan}）",
        "【label 大纲】",
        outline(root),
        "【detail 抽检】",
    ]
    for label, detail, depth in pick_details(root):
        lines.append(f"[L{depth}] {label}｜{detail[:160]}")
    return "\n".join(lines)


def build(kind: str, rows: dict, tree_dir: Path, codes: list[str], seed: int) -> list[str]:
    samples = {s["id"]: s for s in json.loads(SAMPLES.read_text(encoding="utf-8"))["samples"]}
    ids = [sid for sid in sorted(rows)]
    rng = random.Random(f"{seed}-{kind}")
    rng.shuffle(ids)                       # 顺序打乱：不按 S 编号排
    # 代号按乱序后的位置重编（R01…R20），避免出现 R30 这种"暗示还有 30 条"的跳号
    assigned = [f"{codes[0][0]}{i:02d}" for i in range(1, len(ids) + 1)]
    blocks, mapping = [], {}
    for sid, code in zip(ids, assigned):
        path = tree_dir / f"{sid}.json"
        if not path.exists():
            continue
        content = json.loads(path.read_text(encoding="utf-8"))
        blocks.append(card(code, samples[sid]["text"], samples[sid]["must"], content))
        mapping[code] = {"sample": sid, "category": samples[sid]["category"],
                         "title": samples[sid]["title"], "tree_dir": tree_dir.name}
    (OUT / f"cards_{kind}.md").write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return [mapping, list(mapping)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20261127)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    rows = {}
    for line in (DATA / "acc_structure.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["id"]] = row          # 以最后一次为准

    new_map, new_codes = build(
        "new", rows, DATA / "acc_trees",
        [f"R{i:02d}" for i in range(1, 41)], args.seed)

    old_dir = DATA / "acc_trees-before-fix"
    old_rows = {sid: row for sid, row in rows.items()
                if (old_dir / f"{sid}.json").exists()
                and (old_dir / f"{sid}.json").stat().st_mtime < OLD_CUTOFF}
    old_map, old_codes = build(
        "old", old_rows, old_dir,
        [f"O{i:02d}" for i in range(1, 41)], args.seed)

    key = {"seed": args.seed, "new": new_map, "old": old_map,
           "generated": datetime.now().isoformat(timespec="seconds")}
    (OUT / "key.json").write_text(json.dumps(key, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
    print(f"new cards: {len(new_codes)}  order: {' '.join(new_codes)}")
    print(f"old cards: {len(old_codes)}  order: {' '.join(old_codes)}")
    print(f"old trees kept (mtime < {datetime.fromtimestamp(OLD_CUTOFF)}): "
          f"{' '.join(sorted(old_rows))}")
    missing = sorted(set(rows) - set(old_rows))
    print(f"old trees NOT recoverable: {' '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
