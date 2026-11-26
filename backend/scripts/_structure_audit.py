"""结构合规的自动判据：不依赖人评的那一半「结构合理性」。

为什么单列：`docs/PLAN.md:128` 把指标叫「结构合理性」，但它只规定了 4 个维度与
「双人盲评」，没说什么算合格。人评那 4 维里，「覆盖」「无捏造」是内容维度，
「层次」「可读」才是结构维度。结构维度能用确定性判据算出来，内容维度只能人评。

所以这里把「结构」这一半做成可复算的判据（A–F），人评只管内容那两维。这样
「结构合规率」是一个谁都能重跑出来的数，而不是一句"我们觉得结构还行"。

判据与取值：
    A 一级分支数 3–8          少了像列表，多了读不动（可读性参考值 80 在下面 E 之外）
    B 树的深度 3–6            2 层没有信息量，7 层以上是目录不是图
    C 一级分支没有空的        空支在界面上就是一个点，用户点开什么都没有
    D 完全重复的 label ≤1     重复在一张图上就是信息丢失
    E 无 detail 的节点 ≤5%    说明是导图的可读性的一半
    F label 都在可读上限内    超了在画布上被截成半句（实测出现过「最短路径 Dijkstra 与」）

★ F 的那个数是**从代码里取的**（`optimization/repair.py::READABLE_LABEL_CHARS`），
  不在这里写死。它是渲染线的数：`frontend/src/stores/mindmap.ts` 的
  `TRUNCATE_CHARS = 24` 把超出的部分截成 `…`，`canvas/MindNode.vue` 的
  `line-clamp-2` 只留两行。契约给的是 200（`contracts/mindmap.py:82`），
  **两者不是一件事**：200 是"什么算合法"，24 是"两行盒子装得下多少"。

======================================================================
为什么要两处都量（基线快照 / 当前产物）
======================================================================
收尾的确定性修复（`agent/tools.py` 的 `MINDMAP_AUTO_REPAIR`）会改 label：压链把
两层的标题合成一层。曾经它拼到契约上限（200）就收手，于是**修复自己造出来的**长标题
没有任何人量过——本脚本只跑在交付产物上，看起来"量过了"，而那批长 label 恰恰是修复
的产物（20 条样本里 9 条只差 F）。

★ **不能**把 `data/acc_trees-before-fix/` 当成"模型原本写了什么"的证词。它是**上一次
跑验收的产物**，那次收尾修复是开着的：实测它里面 6 条超长 label **全部**含 ` / `，
和当前产物是同一个来源（压链拼接）。所以这份对比量的是**旧拼接逻辑 vs 新拼接逻辑**，
不是"模型 vs 修复"。

"模型原本写了什么"那个口径在别处：生成入口把 label 截到
`GENERATED_LABEL_MAX_CHARS`（16）并报 `LABEL_CLIPPED_TO_GENERATED`
（`generation/service.py`）——模型写超了在那一步就已经看得见，与这里无关。

于是每条样本报三档，含义是准的：

    基线也长 + 当前也长    旧拼接同样拼长了，新逻辑在这条上还没解决
    基线不长 + 当前长      ★**本次改动引入的**（真正要红的那一档）
    基线长 + 当前不长      本次修好了

基线目录不在（`gitignore` 的产物）时只量当前产物，不猜。

用法：
    python scripts/_structure_audit.py
    python scripts/_structure_audit.py --json data/acc_structure_audit.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.optimization.repair import READABLE_LABEL_CHARS  # noqa: E402  （要先补 sys.path）

DATA = BACKEND / "data"
#: 可读上限与修复那一侧同源，见文件头 ★。
LABEL_LIMIT = READABLE_LABEL_CHARS
LABEL_CHECK = "F label ≤%d 字" % LABEL_LIMIT
#: 修复前的基线快照（`gitignore` 里的 `data/acc_trees-before-fix/`）。
BEFORE_DIR = DATA / "acc_trees-before-fix"


def walk(node, depth=1):
    yield node, depth
    for child in node.get("children") or []:
        yield from walk(child, depth + 1)


def audit(content: dict) -> dict:
    root = content.get("root") or {}
    pairs = list(walk(root))
    labels = [(n.get("label") or "").strip() for n, _ in pairs]
    details = [(n.get("detail") or "").strip() for n, _ in pairs]
    top = root.get("children") or []
    depth = max((d for _, d in pairs), default=0)
    dup = len([label for label in labels if label]) - len({label for label in labels if label})
    empty_top = sum(1 for node in top if not (node.get("children") or []))
    no_detail = sum(1 for detail in details if not detail)
    long_labels = [label for label in labels if len(label) > LABEL_LIMIT]
    checks = {
        "A 一级分支 3-8": 3 <= len(top) <= 8,
        "B 深度 3-6": 3 <= depth <= 6,
        "C 无空一级分支": empty_top == 0,
        "D 重复 label ≤1": dup <= 1,
        "E 无 detail ≤5%": (no_detail / len(details)) <= 0.05 if details else False,
        LABEL_CHECK: not long_labels,
    }
    return {
        "nodes": len(pairs), "depth": depth, "top": len(top),
        "dup": dup, "empty_top": empty_top, "no_detail": no_detail,
        "long_labels": long_labels[:5],
        "long_count": len(long_labels),
        "checks": checks,
        "pass": all(checks.values()),
        "failed": [key for key, value in checks.items() if not value],
    }


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _label_origin(before: dict | None, after: dict) -> str:
    """F 这一条的来历，一句话分类。**只比"有没有超长"，不猜是谁写的**。

    基线快照本身也是修复产物（见文件头 ★），所以这里能说的只有"换逻辑前后各是什么
    样"，说不了"模型原本写了什么"。空缺（没有基线）时返回空串。
    """
    if before is None:
        return ""
    was = bool(before["long_count"])
    now = bool(after["long_count"])
    if was and now:
        return "F：基线也长（旧拼接同样拼出）"
    if not was and now:
        return "F：★只有当前产物长（本次引入）"
    if was and not now:
        return "F：本次修好了"
    return ""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)
    rows = [json.loads(line) for line in (DATA / "acc_structure.jsonl")
            .read_text(encoding="utf-8").splitlines() if line]
    latest = {}
    for row in rows:
        latest[row["id"]] = row
    report = []
    before_report = []
    for sid in sorted(latest):
        row = latest[sid]
        path = DATA / "acc_trees" / f"{sid}.json"
        if not row.get("ok") or not path.exists():
            report.append({"id": sid, "category": row["category"], "pass": False,
                           "failed": ["未产出树"], "must_rate": None,
                           "ref_coverage": None})
            continue
        content = _load(path)
        if content is None:
            report.append({"id": sid, "category": row["category"], "pass": False,
                           "failed": ["树文件读不出来"], "must_rate": None,
                           "ref_coverage": None})
            continue
        result = audit(content)
        result.update({"id": sid, "category": row["category"],
                       "must_rate": row["fingerprint"]["must_rate"],
                       "ref_coverage": row["fingerprint"]["ref_coverage"]})
        # 修复前的那一份：同一套判据跑在基线快照上，只为了把 F 的两种来源分开。
        baseline = _load(BEFORE_DIR / f"{sid}.json") if BEFORE_DIR.is_dir() else None
        before = audit(baseline) if baseline is not None else None
        if before is not None:
            before.update({"id": sid, "category": row["category"]})
            before_report.append(before)
        result["origin"] = _label_origin(before, result)
        result["f_before"] = None if before is None else (LABEL_CHECK not in before["failed"])
        result["f_after"] = LABEL_CHECK not in result["failed"]
        report.append(result)
    for item in report:
        flag = "通过" if item["pass"] else "不通过"
        marks = "" if item["pass"] else "　失败项：" + "；".join(item["failed"])
        extra = ""
        if item.get("nodes"):
            extra = (f"　节点 {item['nodes']} 深 {item['depth']} 一级 {item['top']} "
                     f"重复 {item['dup']} 无说明 {item['no_detail']}"
                     f" 覆盖 {item['must_rate']} 引用 {item['ref_coverage']}"
                     f" 超长 {item['long_count']}")
        origin = f"　［{item['origin']}］" if item.get("origin") else ""
        print(f"{item['id']} 【{item['category']}】{flag}{extra}{marks}{origin}")
    passed = sum(1 for item in report if item["pass"])
    print(f"\n结构合规（交付产物 / 修复后）{passed}/{len(report)} = {round(passed / len(report), 3)}")
    if before_report:
        # 修复前也不可能全对：`C 无空一级分支` 与 `E` 修复都不管。所以这一行不是
        # "应该等于 1"，而是"改动前后同一把尺子量的两个数"。
        before_passed = sum(1 for item in before_report if item["pass"])
        print(f"结构合规（基线快照 / 修复前）{before_passed}/{len(before_report)} = "
              f"{round(before_passed / len(before_report), 3)}")
        both = sum(1 for item in report if item.get("f_before") and item.get("f_after"))
        neither = sum(1 for item in report if item.get("f_before") is False
                      and item.get("f_after") is False)
        introduced = sum(1 for item in report if item.get("f_before") and item.get("f_after") is False)
        fixed = sum(1 for item in report if item.get("f_before") is False and item.get("f_after"))
        print(f"{LABEL_CHECK}：两边都过 {both}；两边都不过 {neither}；"
              f"★只有当前产物不过（本次引入）{introduced}；本次修好了 {fixed}")
    else:
        print(f"（没有 {BEFORE_DIR.name}/ 基线快照，只量了交付产物这一份）")
    ok_must = [item["must_rate"] for item in report if item.get("must_rate") is not None]
    if ok_must:
        print(f"内容覆盖（must 命中率）均值 {round(statistics.mean(ok_must), 3)}"
              f"　中位 {round(statistics.median(ok_must), 3)}"
              f"　≥0.7 的比例 {round(sum(1 for v in ok_must if v >= 0.7) / len(ok_must), 3)}")
    by_cat: dict[str, list[float]] = {}
    for item in report:
        if item.get("must_rate") is not None:
            by_cat.setdefault(item["category"], []).append(item["must_rate"])
    for category, values in sorted(by_cat.items()):
        print(f"  {category}: n={len(values)} 覆盖均值 {round(statistics.mean(values), 3)}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print(f"明细写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
