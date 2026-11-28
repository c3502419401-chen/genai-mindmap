"""逐个核实那 12 条 `FISHBONE_*` / `FLOW_*` 码：**只靠产物**能不能判出来。

一句话问法：这 12 条"判据干净、只被 `/check` 的形状挡在外面"的码，把 `/check` 扩到
`FishboneContent` / `FlowContent` 之后，**真能在事后算出同样的结论吗**？

做法不是读代码下结论，而是拿真数据跑一遍生成出口的真代码：

  1. 从 `data/acc_trees*` 读真树（与 `check_diagnostic_sides.py` 同一份数据）；
  2. 调**生成出口本身**（`agent/tools.py::_shape_output`）把树折成真鱼骨/真流程，
     同时收下生成侧那一轮产出的诊断——这就是"现在在哪产生"的实证；
  3. 再对**折出来的产物**跑下面写着的候选判据（`_fishbone_from_product` /
     `_flow_from_product`），看同一批码能不能只靠产物重新算出来。

三种结局，对应三种"该不该搬"：

    产物可判、且与生成侧同结论   → 搬进 `check()` 是纯收益（前提是接口放开）
    产物可判、但语义弱一档       → 搬得动，但码的**说法**要改（「没标起点」≠
                                  「起点是代码补的」）
    产物里没有这个信息           → 搬不动：它记的是**转换时的损失**或**代码做过的
                                  动作**，不是产物的性质。要搬就得先把损失存进产物。

跑法（在 `backend/` 下，需要有 `data/acc_trees*`）：

    python scripts/_probe_diagram_check_sides.py
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

DATA = BACKEND / "data"

#: 这 12 条的静态归属全部是 `app/agent/tools.py`（由
#: `check_diagnostic_sides.py --require-documented` 的读数印证）。
CODES = (
    "FISHBONE_CATEGORIES_INCOMPLETE", "FISHBONE_CATEGORIES_UNFIXED",
    "FISHBONE_EXTRA_BRANCH", "FISHBONE_PROBLEM_ECHOES_TITLE",
    "FISHBONE_PROBLEM_MISSING",
    "FLOW_BRANCH_UNRESOLVED", "FLOW_EDGE_DEMOTED", "FLOW_EDGE_DROPPED",
    "FLOW_ROLE_END_DEFAULTED", "FLOW_ROLE_END_MOVED_UP",
    "FLOW_ROLE_END_OVERRIDES_DATA", "FLOW_ROLE_START_DEFAULTED",
)


@dataclass(frozen=True)
class Verdict:
    code: str
    #: 生成侧的产生点（函数名）。
    origin: str
    #: 判据的签名。注意**没有一条收 `DiagramContent`**：它们收 `Node` /
    #: `MindMapContent` / `raw_plan`，也就是说它们本来就不是"事后判据"。
    signature: str
    #: 产物侧能不能判，以及判出来的是什么。
    from_product: str


VERDICTS = (
    Verdict("FISHBONE_PROBLEM_MISSING", "_fishbone_problem",
            "raw_plan, title, diagnostics",
            "**判不了**：产物里 problem 是 min_length=1 的必填项，代码补的那一句就是"
            "标题本身——产物看不出那张鱼头是模型写的还是兜底的"),
    Verdict("FISHBONE_PROBLEM_ECHOES_TITLE", "_fishbone_problem",
            "raw_plan, title, diagnostics",
            "可判（等价重写）：`_skeleton_key(problem) == _skeleton_key(title)`。"
            "产物带着 problem 与 title 两个字段，判据只读它们"),
    Verdict("FISHBONE_CATEGORIES_INCOMPLETE", "_fishbone_shape_diagnostics",
            "FishboneContent, diagnostics",
            "可判：读 bones[].category 与 FISHBONE_CATEGORIES 求差。**但结论取决于"
            "开关**——MINDMAP_FISHBONE_FREE_CATEGORIES 开着时这条本来就不成立，而开关"
            "状态没记进产物，事后判只能按**现在**的开关算"),
    Verdict("FISHBONE_CATEGORIES_UNFIXED", "_fishbone_shape_diagnostics",
            "FishboneContent, diagnostics",
            "同上，是同一个开关的另一支。产物能读出「这次是哪几个维度」，读不出"
            "「当时开关是开的」"),
    Verdict("FISHBONE_EXTRA_BRANCH", "_shape_output",
            "MindMapContent, *, diagram_type, raw_plan, diagnostics",
            "**判不了**：`shapes.to_fishbone` 已经把第 7 根起的一级分支并进最后一根骨，"
            "产物里 len(bones) <= MAX_FISHBONE_BONES 恒成立。这条记的是**转换时的"
            "搬迁**，不是产物的性质"),
    Verdict("FLOW_ROLE_START_DEFAULTED", "_stamp_flow_roles",
            "FlowContent, diagnostics",
            "可判同一事实、**说法要弱一档**：产物没有 role 为 start 的节点是可观察的，"
            "但「没有起点」与「起点是代码补的」在产物上同形"),
    Verdict("FLOW_ROLE_END_DEFAULTED", "_stamp_flow_roles",
            "FlowContent, diagnostics",
            "同上：产物里 roles 不含 end 可观察"),
    Verdict("FLOW_ROLE_END_MOVED_UP", "_stamp_flow_roles",
            "FlowContent, diagnostics",
            "可观察形态、判不了成因：产物里「最后一个阶段的叶子仍是 data/database，而 "
            "end 落在更前一步」是可读的，但「这是因为代码让位」只有转换时知道"),
    Verdict("FLOW_ROLE_END_OVERRIDES_DATA", "_stamp_flow_roles",
            "FlowContent, diagnostics",
            "**判不了**：那一步原来的 data / database 已被 end 覆盖（role 是单值字段），"
            "产物里一点痕迹都不剩"),
    Verdict("FLOW_EDGE_DROPPED", "_shape_output",
            "MindMapContent, *, diagram_type, raw_plan, diagnostics",
            "**判不了**：`to_flow` 返回的 FlowEdgeLoss.dropped 只在转换那一刻存在，"
            "被丢的边不进产物"),
    Verdict("FLOW_EDGE_DEMOTED", "_shape_output",
            "MindMapContent, *, diagram_type, raw_plan, diagnostics",
            "**判不了**：降级后的边还在，但「它原来指到某一步」这个信息没了"),
    Verdict("FLOW_BRANCH_UNRESOLVED", "_shape_output（上游 _flow_branch_edges 之后）",
            "flow_branches, MindMapContent（读的是**原始计划**，不是产物）",
            "**判不了**：判据读 raw_plan 里的 flow_branches，那是**输入侧**的东西，"
            "产物里没有它；对不上的那些边本来就没进图"),
)


def _trees() -> list[Path]:
    if not DATA.is_dir():
        return []
    return sorted(path for folder in DATA.glob("acc_trees*") if folder.is_dir()
                  for path in folder.glob("*.json"))


def _load(path: Path):
    from app.contracts.mindmap import MindMapContent

    payload = json.loads(path.read_text(encoding="utf-8"))
    content = payload.get("content") or payload.get("map") or payload
    return MindMapContent.model_validate(content)


def _roles(content) -> set[str]:
    """产物上所有非空 role。三个图型共用同一个 Node，所以这一句是通用的。"""
    from app.agent import shapes

    return {node.role for node in shapes.iter_nodes(content) if node.role}


def _fishbone_from_product(content) -> dict[str, bool]:
    """候选判据：只读 FishboneContent。搬进 check() 的话，判据就长这样。"""
    from app.agent.tools import _skeleton_key
    from app.contracts.mindmap import FISHBONE_CATEGORIES

    got = {_skeleton_key(bone.category) for bone in content.bones}
    return {
        "FISHBONE_PROBLEM_ECHOES_TITLE":
            _skeleton_key(content.problem) == _skeleton_key(content.title),
        "FISHBONE_CATEGORIES_INCOMPLETE":
            bool([name for name in FISHBONE_CATEGORIES if _skeleton_key(name) not in got]),
        # 产物里永远 <= 6 根骨（超出的已被并进最后一根），所以这一条恒为 False。
        "FISHBONE_EXTRA_BRANCH": False,
    }


def _flow_from_product(content) -> dict[str, bool]:
    """候选判据：只读 FlowContent。"""
    roles = _roles(content)
    return {
        "FLOW_ROLE_START_DEFAULTED": "start" not in roles,
        "FLOW_ROLE_END_DEFAULTED": "end" not in roles,
    }


def _fishbone_pass(trees) -> tuple[Counter, Counter, int]:
    """把每棵真树折成真鱼骨，收生成侧诊断，再跑产物侧候选判据。"""
    from app.agent.tools import _shape_output

    generated: Counter = Counter()
    observed: Counter = Counter()
    rows = 0
    for tree in trees:
        for raw_plan in ({}, {"problem": tree.title},
                         {"problem": f"为什么「{tree.title}」会这样？"}):
            diagnostics: list = []
            shaped = _shape_output(tree.model_copy(deep=True), diagram_type="fishbone",
                                   raw_plan=raw_plan, diagnostics=diagnostics)
            generated.update(item.code for item in diagnostics)
            observed.update(code for code, hit in _fishbone_from_product(shaped).items() if hit)
            rows += 1
    return generated, observed, rows


def _flow_pass(trees) -> tuple[Counter, Counter, int, int]:
    from app.agent import shapes
    from app.agent.tools import _shape_output

    generated: Counter = Counter()
    observed: Counter = Counter()
    rows = 0
    nodes = 0
    for tree in trees:
        diagnostics: list = []
        shaped = _shape_output(tree.model_copy(deep=True), diagram_type="flowchart",
                              raw_plan=None, diagnostics=diagnostics)
        generated.update(item.code for item in diagnostics)
        observed.update(code for code, hit in _flow_from_product(shaped).items() if hit)
        rows += 1
        nodes += len(list(shapes.iter_nodes(shaped)))
    return generated, observed, rows, nodes


def _flow_loss(trees) -> Counter:
    """`to_flow` 的三种边去向。**只在转换那一刻存在**——这就是那三条码的处境。"""
    from app.agent import shapes

    loss: Counter = Counter()
    for tree in trees:
        _product, edge_loss = shapes.to_flow(tree)
        loss["dropped"] += edge_loss.dropped
        loss["demoted"] += edge_loss.demoted
        loss["inner"] += edge_loss.inner
    return loss


def _fishbone_extras(trees) -> tuple[int, Counter]:
    """触发过「一级分支超额」的折叠次数，以及那些产物最后剩几根骨。"""
    from app.agent.tools import _shape_output

    hits = 0
    bones = Counter()
    for tree in trees:
        diagnostics: list = []
        shaped = _shape_output(tree.model_copy(deep=True), diagram_type="fishbone",
                               raw_plan={"problem": "为什么？"}, diagnostics=diagnostics)
        if any(item.code == "FISHBONE_EXTRA_BRANCH" for item in diagnostics):
            hits += 1
            bones[len(shaped.bones)] += 1
    return hits, bones


def _report(title: str, generated: Counter, observed: Counter) -> None:
    print(f"\n=== {title} ===")
    for code in sorted(set(generated) | set(observed)):
        print(f"  {code:34s} 生成侧 {generated.get(code, 0):4d} 次"
              f" / 产物侧可判 {observed.get(code, 0):4d} 次")


def main() -> int:
    trees = [_load(path) for path in _trees()]
    print(f"真树 {len(trees)} 棵（data/acc_trees*）；每棵折两次：鱼骨、流程")
    if not trees:
        print("[skip] 没有真树：data/ 不进 git，干净 checkout 上跑不了这一层。")
        return 0

    os.environ.pop("MINDMAP_FISHBONE_FREE_CATEGORIES", None)
    gen_off, obs_off, rows = _fishbone_pass(trees)
    os.environ["MINDMAP_FISHBONE_FREE_CATEGORIES"] = "1"
    gen_on, obs_on, _ = _fishbone_pass(trees)
    os.environ.pop("MINDMAP_FISHBONE_FREE_CATEGORIES", None)

    _report(f"鱼骨：{rows} 次折叠（每棵树 3 种 problem 写法），开关关", gen_off, obs_off)
    _report("鱼骨：同一个开关**打开**", gen_on, obs_on)
    print("  （开关这样改变结论，而产物里没有记下当时的开关状态）")

    gen_flow, obs_flow, flow_rows, nodes = _flow_pass(trees)
    _report(f"流程：{flow_rows} 次折叠（产物共 {nodes} 个节点）", gen_flow, obs_flow)
    loss = _flow_loss(trees)
    print(f"  三种边去向（只在 to_flow 那一刻存在）：dropped {loss['dropped']}"
          f" / demoted {loss['demoted']} / inner {loss['inner']}")
    print("  → FLOW_EDGE_DROPPED / FLOW_EDGE_DEMOTED / FLOW_BRANCH_UNRESOLVED 在产物上"
          "一次都判不出来：它们记的正是「产物里已经没有了的东西」")

    hits, bones = _fishbone_extras(trees)
    print(f"\n  触发过 FISHBONE_EXTRA_BRANCH 的折叠：{hits} 次；那些产物的骨数分布："
          f"{dict(sorted(bones.items()))}"
          "（超额的分支已被并进最后一根骨，产物里看不出曾有过第 7 根）")

    print("\n=== 逐条：判据签名与「产物能不能判」===")
    for item in VERDICTS:
        print(f"\n{item.code}")
        print(f"  产生点   {item.origin}")
        print(f"  判据签名 {item.signature}")
        print(f"  产物侧   {item.from_product}")

    missing = [code for code in CODES if code not in {item.code for item in VERDICTS}]
    if missing:
        print(f"\n[fail] 有码没写判定：{', '.join(missing)}")
        return 1
    print(f"\n[ok] {len(VERDICTS)} 条逐个判过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
