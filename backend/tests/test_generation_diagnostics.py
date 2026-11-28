"""结构体检（`check`）与语义复核（`annotate_nli`）接回生成路径后的接线契约。

这两处原本接在整树生成（`_generate`）的收尾，随那条路一起被删掉——函数活着、没有
调用点。这个文件钉的是"接回来了，且接的方式对"：

  1. `check()` 的诊断进 `data["diagnostics"]`，`SINGLE_CHILD` 在分片树上真的会触发
     （某一支只长出一个子节点，正是分片最容易出现的形状）；
  2. 深度诊断用的是契约的 8 层上限，不会被本次的 `max_depth` 预算误触发；
  3. `nli.available()` 为假时**零调用、零诊断**——连 `annotate_nli` 都不进；
  4. NLI 走线程池：它跑的时候事件循环还能跳（同步跑会把整段独占）；
  5. `MAX_NLI_CHECKS=100` 的上限仍然生效（150 条引用只判 100 条）；
  6. NLI 抛错只是没有评分，生成照常完成；
  7. 不回归：逐节点引用噪声仍被过滤，逐字校验与 `EVIDENCE_UNUSED` 不受影响。
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import MAX_NLI_CHECKS, PLAN_PROMPT, build_registry
from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source, SourceKind
from app.optimization.service import check
from app.optimization.source_refs import annotate_nli

TOPIC = "预算管理"
BRANCHES = ("预算审批", "报销时限", "采购合规")

# 超过 `SHORT_MATERIAL_CHARS=300`，且小节标题与 `BRANCHES` 逐字对上——按支检索
# 用分支标题当 query，标题对不上就召不回，这条链路会静默变成"没有证据"。
MATERIAL_TEXT = (
    "# 预算管理办法\n\n"
    "## 预算审批\n\n"
    "预算由财务经理核准后执行，超过五十万元报总经理审批。"
    "每季度末做一次预算执行分析，偏差超过百分之十的需要书面说明原因。"
    "年度预算在十一月启动编制，各部门按上一年的执行率申报，财务汇总后报董事会。\n\n"
    "## 报销时限\n\n"
    "费用报销的时限是三十个自然日，逾期不再受理；跨年度的单据必须附说明。"
    "报销单需要部门负责人签字，财务只做形式审核，实质合规由内审抽查。"
    "差旅费按标准报销，超标部分需要总经理特批，特批记录留存三年备查。\n\n"
    "## 采购合规\n\n"
    "采购合同必须经过法务审核，金额超过五十万元另行招标。"
    "供应商准入每两年复核一次，出现过重大违约的记录在案，三年内不得再次准入。\n\n"
    "## 附则\n\n"
    "本办法由财务部负责解释，自发布之日起执行；此前发布的相关规定同时废止。"
    "各部门可以在本办法框架内制定实施细则，报财务部备案后生效。\n"
)


# ---------------------------------------------------------------- 夹具


def _source() -> Source:
    return Source(id="s-budget", kind=SourceKind.document, status="parsed",
                  filename="预算管理办法.docx", text=MATERIAL_TEXT)


def _ctx(*, sources: list[Source] | None = None,
         max_model_calls: int = 30) -> RunContext:
    payload = sources if sources is not None else [_source()]
    request = AgentRunRequest(
        goal=TOPIC, intent="create", sources=payload,
        budget=RunBudget(max_model_calls=max_model_calls, max_tool_calls=8,
                         max_seconds=60.0, max_output_tokens=8192, max_repairs=1),
    )
    return RunContext(request=request,
                      sources_by_id={source.id: source for source in payload})


def _branch_of(user: str) -> str:
    return user.split("本条一级分支：", 1)[1].split("\n", 1)[0]


def _evidence_of(user: str) -> list[dict]:
    if "evidence 数组）：\n" not in user:
        return []
    import json
    return json.loads(user.split("evidence 数组）：\n", 1)[1])


def _ref_of(user: str) -> list[dict]:
    """照抄本支第一条命中的 `evidence_*`——收尾的逐字校验会剔掉自己编的引文。"""
    payload = _evidence_of(user)
    if not payload:
        return []
    hit = payload[0]
    if hit.get("evidence_quote"):
        return [{"source_id": hit["source_id"], "start": hit["evidence_start"],
                 "end": hit["evidence_end"], "quote": hit["evidence_quote"]}]
    return [{"source_id": hit["source_id"], "quote": hit["quote"][:300]}]


def _node_of(user: str) -> str:
    """第三层请求里的「本节点：X」——用它给第三层的 label 带上父节点名。

    不带的话，`per_branch × 3 支` 个父节点会各写一份「细节1」「细节2」，六处逐字
    同名、又分属不同父——那是**真的**跨支重复，`CROSS_BRANCH_DUPLICATE` 会报它
    （判据在 `optimization/cross_branch.py`，`check()` 里就有）。真实模型不会把
    六处都写成同一个名字，所以带上父节点名是让夹具更真，而不是绕过判据。
    """
    if "本节点：" not in user:
        return ""
    return user.split("本节点：", 1)[1].split("\n", 1)[0]


def _chat(*, per_branch: int = 2, third_level: int = 1, refs: bool = True,
          seen: list[str] | None = None, detail: bool = False,
          child_label: str | None = None):
    """脚本模型：骨架给 `BRANCHES`，每支 `per_branch` 个要点（每个都照抄一条引用），
    每个要点再给 `third_level` 个子节点。

    `third_level=1` 是这条文件的关键夹具：它让每个二级节点**恰好只有一个孩子**，
    即 `SINGLE_CHILD` 的形状——分片按支展开时，这就是"那一支模型没展开"的现场。
    `refs=True` 时每个要点各带一条引用（`per_branch=2` × 3 支 = 6 条），
    与真实形态一致：模型会为每个要点写引用，而不是每支只写一条。
    `detail=True` 时每个要点再带一句 `detail`——真实生成会写它，而 NLI 的
    hypothesis 正是"label + detail"而不是 label 本身。
    `child_label` 覆盖二级要点的名字（缺省带分支名区分）。它存在只为一件事：
    造出"两支写了同一个说法"的形状，用来钉跨支重复的诊断。
    """
    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if seen is not None:
            seen.append(user)
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(BRANCHES)}
        if "本条一级分支：" in user:
            sourced = _ref_of(user) if refs else []
            children = []
            for index in range(1, per_branch + 1):
                item = {"label": child_label or f"{_branch_of(user)}·{index}",
                        "source_refs": list(sourced)}
                if detail:
                    item["detail"] = f"{_branch_of(user)}的第{index}条论断，五万元以内由财务经理核准。"
                children.append(item)
            return {"children": children}
        # 第三层：不再写引用（引用只挂在二级要点上，与真实提示词的粒度一致）。
        # label 带父节点名——理由见 `_node_of`。
        parent = _node_of(user)
        return {"children": [
            {"label": f"{parent}·细节{index}", "expandable": False}
            for index in range(1, third_level + 1)
        ]}
    return chat


def _run(ctx: RunContext, chat, *, arguments: dict | None = None):
    args = {"topic": TOPIC, "branches": len(BRANCHES), "points_per_branch": 2,
            "max_depth": 2}
    args |= arguments or {}
    registry = build_registry("create_deep", has_sources=True, has_library=False)
    with patch("app.agent.tools.chat_async", new=chat):
        return asyncio.run(registry.execute(
            ToolCall(call_id="c-deep", name="mindmap_generate_deep", arguments=args),
            ctx,
        ))


def _codes(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


@pytest.fixture(autouse=True)
def _nli_off(monkeypatch):
    """缺省状态：NLI 没启用。要跑语义层的用例自己打开。"""
    monkeypatch.delenv("MINDMAP_NLI_ENABLED", raising=False)
    monkeypatch.delenv("MINDMAP_NLI_ON_GENERATE", raising=False)


# ---------------------------------------------------------------- 1. SINGLE_CHILD


def test_single_child_branch_is_reported():
    """某一支只长出一个子节点 → 诊断里必须有一条 `SINGLE_CHILD`。

    这就是这条接线的主要理由：整树一次成型时模型总把每支要点数给满，`SINGLE_CHILD`
    几乎不触发；而分片按支展开，"这一支没展开"在树上留下的正是这个形状。
    """
    ctx = _ctx()
    result = _run(ctx, _chat(per_branch=2, third_level=1),
                  arguments={"points_per_branch": 2, "max_depth": 3})

    assert result.ok, result.content
    codes = _codes(result)
    assert "SINGLE_CHILD" in codes, codes
    # 3 支 × 2 个二级节点，每个二级各只有一个孩子
    assert codes.count("SINGLE_CHILD") == 6

    single = [item for item in result.data["diagnostics"]
              if item["code"] == "SINGLE_CHILD"]
    # 每一条都要能点回具体节点：只说"有 6 个"而不给 id 等于没给可操作信息
    assert all(item["node_id"] for item in single)
    assert all(item["level"] == "info" for item in single)
    # 用户读的是 content，这句话必须在那儿
    assert "只有一个子节点" in result.content
    assert result.data["structure"]["single_child"] == 6


def test_healthy_tree_reports_no_structure_diagnostic():
    """每支两个要点、没有单子节点：`check()` 一条都不该报。

    没有这条，"诊断里出现 SINGLE_CHILD"可能来自一个永远报警的坏规则。
    """
    ctx = _ctx()
    result = _run(ctx, _chat(per_branch=2, third_level=2),
                  arguments={"points_per_branch": 2, "max_depth": 3})

    assert result.ok, result.content
    assert "SINGLE_CHILD" not in _codes(result)
    # 3 支 → 2 个要点 → 2 个细节：4 层（含中心主题），与 `max_depth=3` 一致
    assert result.data["structure"] == {"single_child": 0, "deepest": 4,
                                        "reported": 0}


def test_the_deep_path_reports_cross_branch_duplicates_too():
    """分片路径的 `check()` 也报跨支逐字同名——它不再是整树生成的私产。

    这条判据原先只挂在 `_generate` 的收尾（`agent/tools.py` 自己追加），分片这条路
    一次都不报。而现在 `check()` 里有它（`optimization/cross_branch.py`），两条生成
    路径都走 `check()`，于是分片也开始报——**这是刻意的**：跨支重复正是分片的结构性
    缺陷（每支只看得到自己的标题链），它比整树更需要这条诊断。

    分片路径**没有** `data["cross_branch_duplicates"]` 那个计数（那是整树路径的读
    数），所以这里只钉诊断，不钉 data。
    """
    ctx = _ctx()
    # `points_per_branch` 的下限是 2；每支写两条同名要点，同父那一份在收槽时就被
    # 去掉了（`_keep_new_slots`），所以每支只留一个 —— 留下来的三个正好跨三支同名。
    result = _run(ctx, _chat(per_branch=2, child_label="内核模块编写 4 次"),
                  arguments={"points_per_branch": 2, "max_depth": 2})

    assert result.ok, result.content
    codes = _codes(result)
    assert "CROSS_BRANCH_DUPLICATE" in codes, codes
    duplicate = [item for item in result.data["diagnostics"]
                 if item["code"] == "CROSS_BRANCH_DUPLICATE"][0]
    # 一条汇总：3 支各留一个同名要点 = 3 个节点、1 个说法
    assert "3 个节点跨分支重复" in duplicate["message"]
    # 只报不删：三个节点都还在树上
    kept = [node for branch in ctx.candidate.root.children for node in branch.children
            if node.label == "内核模块编写 4 次"]
    assert len(kept) == 3


# ---------------------------------------------------------------- 2. 深度诊断


def _chain(depth: int) -> MindMapContent:
    """造一条 `depth` 层（root 算第 1 层）的链。

    契约本身就限死"深度不超过 8 层"，所以 `check()` 的 `MAX_DEPTH=8` 只能在
    **边界上**触发：第 8 层那一个节点。造 9 层会先被 `model_validate` 打回。
    """
    node: dict = {"id": f"n{depth}", "label": f"第{depth}层", "children": []}
    for level in range(depth - 1, 0, -1):
        node = {"id": f"n{level}", "label": f"第{level}层", "children": [node]}
    return MindMapContent.model_validate({
        "title": "链", "source_usage": [], "root": node,
    })


def test_depth_limit_diagnostic_still_fires():
    """深度诊断用的是契约的 8 层上限（`check.MAX_DEPTH`），不是本次的预算。

    分片路径的 `max_depth` 默认只有 3，**不会**触发它；这条钉的是"接进来的那个
    `check()` 还是原来那个 `check()`"——最大深度那一条不能被接线的改写弄丢。
    """
    diagnostics = check(_chain(8))
    depth_codes = [item for item in diagnostics if item.code == "DEPTH_LIMIT"]
    assert len(depth_codes) == 1                 # 只有第 8 层越界
    assert depth_codes[0].node_id == "n8"
    assert depth_codes[0].level.value == "warn"


def test_depth_limit_is_not_triggered_by_the_generation_budget():
    """3 层预算生成的树不该报 `DEPTH_LIMIT`：8 层是上限，3 层离它很远。"""
    ctx = _ctx()
    result = _run(ctx, _chat(per_branch=2, third_level=2),
                  arguments={"points_per_branch": 2, "max_depth": 3})
    assert result.ok, result.content
    assert "DEPTH_LIMIT" not in _codes(result)


# ---------------------------------------------------------------- 3. NLI 关闭


def test_disabled_nli_is_never_called():
    """`nli.available()` 为假（缺省的关）→ `annotate_nli` 一次都不进。

    "零调用"比"零诊断"更强：它同时保证了 `onnxruntime` 不会被 import 进来。
    """
    ctx = _ctx()
    with patch("app.agent.tools.annotate_nli") as spy, \
            patch("app.optimization.nli.available") as available:
        result = _run(ctx, _chat(per_branch=2, third_level=2))

    assert result.ok, result.content
    spy.assert_not_called()
    # 默认关时连模型探测都不发生——`on_generate()` 只读环境变量
    available.assert_not_called()
    assert result.data["nli"]["on_generate"] is False
    assert result.data["nli"]["judged"] == 0
    codes = _codes(result)
    assert not [code for code in codes
                if code in ("SOURCE_REF_NLI", "SOURCE_REF_CONTRADICTED")]
    assert "语义复核" not in result.content


def test_enabled_model_runs_the_semantic_layer(monkeypatch):
    """模型开了、生成这道闸没被关 → 每条留存引用各判一次。"""
    monkeypatch.setenv("MINDMAP_NLI_ENABLED", "1")
    monkeypatch.setenv("MINDMAP_NLI_ON_GENERATE", "1")
    calls: list[str] = []

    def fake_judge(claim: str, evidence: str) -> dict | None:
        calls.append(claim)
        return {"score": 0.9, "label": "entailment", "probs": {}}

    ctx = _ctx()
    with patch("app.optimization.nli.available", return_value=True), \
            patch("app.optimization.nli.judge", new=fake_judge):
        result = _run(ctx, _chat(per_branch=2, third_level=2))

    assert result.ok, result.content
    assert len(calls) == 6                       # 3 支 × 2 个要点，各一条引用
    assert result.data["nli"]["judged"] == 6
    # 全部判「蕴含」→ 合成一条 info，而不是每条引用一条（几百引用时那是噪声）
    assert _codes(result).count("SOURCE_REF_NLI") == 1
    assert "语义复核：6 条引用" in result.content


def test_contradiction_is_reported_one_by_one(monkeypatch):
    """判成「相反」的逐条留 warn：那是要人核的东西，不能压成计数。"""
    monkeypatch.setenv("MINDMAP_NLI_ENABLED", "1")
    monkeypatch.setenv("MINDMAP_NLI_ON_GENERATE", "1")

    ctx = _ctx()
    with patch("app.optimization.nli.available", return_value=True), \
            patch("app.optimization.nli.judge",
                  new=lambda claim, evidence: {"score": 0.81,
                                               "label": "contradiction",
                                               "probs": {}}):
        result = _run(ctx, _chat(per_branch=2, third_level=2))

    assert result.ok, result.content
    contradicted = [item for item in result.data["diagnostics"]
                    if item["code"] == "SOURCE_REF_CONTRADICTED"]
    assert len(contradicted) == 6
    assert all(item["level"] == "warn" for item in contradicted)
    assert all(item["node_id"] for item in contradicted)
    assert result.data["nli"]["contradicted"] == 6
    # 相反的那几条**不改变引用**：第三层只产出诊断
    refs = sum(len(node.source_refs) for node in _walk(ctx.candidate.root))
    assert refs == 6


def test_claim_carries_the_node_detail_through_the_whole_path(monkeypatch):
    """端到端：`judge` 收到的 hypothesis 是「label + detail」，不是 label。

    这条钉的是接线本身。上一版传的是 `node.label`（"预算审批·1"这种短标题），
    模型能判的只有"这段引文提到这些字了吗"，而 `detail` 里的论断——真正可能
    与原文相反的东西——根本没进过模型。
    """
    monkeypatch.setenv("MINDMAP_NLI_ENABLED", "1")
    monkeypatch.setenv("MINDMAP_NLI_ON_GENERATE", "1")
    claims: list[str] = []

    def fake_judge(claim: str, evidence: str) -> dict | None:
        claims.append(claim)
        return {"score": 0.9, "label": "entailment", "probs": {}}

    ctx = _ctx()
    with patch("app.optimization.nli.available", return_value=True), \
            patch("app.optimization.nli.judge", new=fake_judge):
        result = _run(ctx, _chat(per_branch=2, third_level=2, detail=True))

    assert result.ok, result.content
    assert len(claims) == 6
    for claim in claims:
        assert "。" in claim, claim
        assert "五万元以内由财务经理核准" in claim, claim
        assert claim.index("。") < claim.index("五万元"), "label 必须排在 detail 前面"
    assert result.data["nli"]["judged"] == 6
    assert result.data["nli"]["contradicted"] == 0


def test_three_verdicts_are_counted_apart(monkeypatch):
    """三档混着来：相反逐条 warn，其余合成一条 info，两个计数各自对得上。

    `judged` 是实判条数、`contradicted` 是其中 warn 的条数——混淆这两个数（比如
    把 judged 记成"开关开着"）会让 `on_generate=True + judged=0` 被读成"跑了但
    一条都没判"，而真相是"没有引用可判"。
    """
    monkeypatch.setenv("MINDMAP_NLI_ENABLED", "1")
    monkeypatch.setenv("MINDMAP_NLI_ON_GENERATE", "1")
    verdicts = ["contradiction", "entailment", "neutral",
                "contradiction", "entailment", "neutral"]
    offset = {"index": 0}

    def fake_judge(claim: str, evidence: str) -> dict | None:
        label = verdicts[offset["index"] % len(verdicts)]
        offset["index"] += 1
        return {"score": 0.75, "label": label, "probs": {}}

    ctx = _ctx()
    with patch("app.optimization.nli.available", return_value=True), \
            patch("app.optimization.nli.judge", new=fake_judge):
        result = _run(ctx, _chat(per_branch=2, third_level=2, detail=True))

    assert result.ok, result.content
    codes = _codes(result)
    assert result.data["nli"]["judged"] == 6
    assert result.data["nli"]["contradicted"] == 2
    assert codes.count("SOURCE_REF_CONTRADICTED") == 2
    # 其余 4 条（entailment/neutral）合成一条 info，不逐条列出
    assert codes.count("SOURCE_REF_NLI") == 1
    assert "语义复核：6 条引用的论断与引文做过蕴含判定" in result.content
    assert "其中 2 条与原文语义相反，已逐条列出" in result.content


def _walk(root):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.children)


# ---------------------------------------------------------------- 4. 事件循环


def test_nli_does_not_block_the_event_loop(monkeypatch):
    """NLI 必须在线程池里跑：它占着的这段时间事件循环还得能跳。

    同步调用的现场就是这样：judge 每睡 20 ms、共 6 条 = 120 ms，这 120 ms 里
    心跳一次都跳不出来（`mark` 窗口内最大间隔 ≈ 总耗时）。走 `to_thread` 之后
    同一个窗口里能跳二十来次、最大间隔只剩心跳周期。
    """
    monkeypatch.setenv("MINDMAP_NLI_ENABLED", "1")
    monkeypatch.setenv("MINDMAP_NLI_ON_GENERATE", "1")
    ctx = _ctx()
    marks: dict[str, float] = {}
    ticks: list[tuple[float, float]] = []

    def slow_judge(claim: str, evidence: str) -> dict:
        marks.setdefault("start", time.perf_counter())
        time.sleep(0.02)
        marks["end"] = time.perf_counter()
        return {"score": 0.5, "label": "neutral", "probs": {}}

    async def scenario() -> None:
        stop = False

        async def ticker() -> None:
            last = time.perf_counter()
            while not stop:
                await asyncio.sleep(0.005)
                now = time.perf_counter()
                ticks.append((last, now))
                last = now

        beat = asyncio.create_task(ticker())
        registry = build_registry("create_deep", has_sources=True, has_library=False)
        try:
            with patch("app.optimization.nli.available", return_value=True), \
                    patch("app.optimization.nli.judge", new=slow_judge), \
                    patch("app.agent.tools.chat_async",
                          new=_chat(per_branch=2, third_level=2)):
                await registry.execute(
                    ToolCall(call_id="c", name="mindmap_generate_deep",
                             arguments={"topic": TOPIC, "branches": len(BRANCHES),
                                        "points_per_branch": 2, "max_depth": 2}),
                    ctx,
                )
        finally:
            stop = True
            await beat

    asyncio.run(scenario())

    assert marks.get("end"), "NLI 一次都没跑，这条用例就白测了"
    window = marks["end"] - marks["start"]
    assert window > 0.09, f"NLI 窗口只有 {window * 1000:.0f} ms，量不出阻塞"
    inside = [after - before for before, after in ticks
              if before >= marks["start"] and after <= marks["end"]]
    assert inside, "窗口里一次心跳都没有——事件循环被独占了"
    peak = max(inside)
    # 同步实现下 peak ≈ window（120 ms）；线程池实现下 peak ≈ 心跳周期。
    assert peak < window / 2, (
        f"NLI 期间事件循环停顿 {peak * 1000:.0f} ms，接近整段 {window * 1000:.0f} ms："
        "没有走 to_thread")


# ---------------------------------------------------------------- 5. 条数上限


def _content_with_refs(count: int) -> tuple[MindMapContent, Source]:
    """造一棵带 `count` 条引用的树（每支最多 50 个要点，契约的上限）。"""
    quote = "预算由财务经理核准后执行"
    source = Source(id="s1", kind=SourceKind.document, status="parsed",
                    filename="m.txt", text=f"开头。{quote}。结尾。")
    children = []
    remaining = count
    for branch in range(6):
        points = min(50, remaining)
        if not points:
            break
        children.append({
            "id": f"b{branch}", "label": f"支{branch}", "children": [
                {"id": f"p{branch}-{index}", "label": f"要点{index}",
                 "children": [],
                 "source_refs": [{"source_id": "s1", "quote": quote}]}
                for index in range(points)
            ],
        })
        remaining -= points
    assert remaining == 0, "夹具没造够条数"
    content = MindMapContent.model_validate({
        "title": "大量引用",
        "source_usage": [{"source_id": "s1", "kind": "document",
                          "filename": "m.txt", "char_count": 20}],
        "root": {"id": "root", "label": "根", "children": children},
    })
    return content, source


def test_nli_stops_at_the_check_limit():
    """150 条引用只判 100 条：分片树的引用比整树多，这个上限必须还在。"""
    content, source = _content_with_refs(150)
    calls: list[str] = []

    def counting_judge(claim: str, evidence: str) -> dict:
        calls.append(claim)
        return {"score": 0.5, "label": "neutral", "probs": {}}

    with patch("app.optimization.nli.available", return_value=True), \
            patch("app.optimization.nli.judge", new=counting_judge):
        diagnostics = annotate_nli(content, [source])

    assert MAX_NLI_CHECKS == 100
    assert len(calls) == MAX_NLI_CHECKS
    assert len(diagnostics) == MAX_NLI_CHECKS


def test_generation_reports_the_nli_limit():
    """生成路把上限与实判条数一起报出来：被截断时只看 `judged` 是看不出来的。"""
    ctx = _ctx()
    result = _run(ctx, _chat(per_branch=2, third_level=2))
    assert result.data["nli"]["limit"] == MAX_NLI_CHECKS


# ---------------------------------------------------------------- 6. 失败降级


def test_nli_failure_does_not_break_generation(monkeypatch):
    """NLI 抛错 → 生成照常完成，诊断里没有 NLI 条目。"""
    monkeypatch.setenv("MINDMAP_NLI_ENABLED", "1")
    monkeypatch.setenv("MINDMAP_NLI_ON_GENERATE", "1")
    ctx = _ctx()
    with patch("app.agent.tools.annotate_nli",
               new=lambda content, sources: (_ for _ in ()).throw(
                   RuntimeError("onnxruntime 炸了"))):
        result = _run(ctx, _chat(per_branch=2, third_level=2))

    assert result.ok, result.content
    assert ctx.candidate is not None
    assert result.data["node_count"] > 1
    codes = _codes(result)
    assert not [code for code in codes
                if code in ("SOURCE_REF_NLI", "SOURCE_REF_CONTRADICTED")]
    assert result.data["nli"]["judged"] == 0
    assert "语义复核" not in result.content


# ---------------------------------------------------------------- 7. 不回归


def test_per_node_reference_noise_stays_out_of_diagnostics():
    """结构体检的第三层不能把逐节点引用提示塞回诊断里。

    分片树每个叶子都可能"没有引用"，几百条同质提示会把 `SPARSE_BRANCH` 这类
    真正要看的东西淹掉——这条与收尾剔除共用同一个清单（`_PER_NODE_REF_CODES`）。
    """
    ctx = _ctx()
    result = _run(ctx, _chat(per_branch=2, third_level=2))
    codes = _codes(result)
    assert "UNSOURCED_NODE" not in codes
    assert "SOURCE_PAGE_UNVERIFIED" not in codes
    # 引用确实在树上（不是"因为一条引用都没有所以没噪声"）
    assert result.data["source_refs"]["kept"] == 6


def test_verbatim_check_still_drops_refs_without_a_quote():
    """逐字校验照旧在跑，并且**剔除**那一类过不了它的引用。

    夹具走的是唯一能活到收尾、又必然被判死的那条路：`_ref_from` 会把超过 300 字
    的引文清空、只留区间（截断再配原区间必然对不上，那比没有引文更糟）。到了
    收尾，`_check_reference` 对"只有区间没有引文"判 `UNVERIFIED_SOURCE_REF` 并剔除。
    """
    async def overlong_chat(system: str, user: str, *, max_tokens=None, on_delta=None):
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(BRANCHES)}
        if "本条一级分支：" in user:
            payload = _evidence_of(user)
            refs: list[dict] = []
            if payload:
                hit = payload[0]
                # 单级命中没有 `evidence_*`（父块模式才有），退化到命中自身的区间
                start = hit.get("evidence_start", hit.get("start"))
                end = hit.get("evidence_end", hit.get("end"))
                refs = [{"source_id": hit["source_id"], "start": start, "end": end,
                         "quote": "超" * 350}]
            return {"children": [
                {"label": f"{_branch_of(user)}·{index}", "expandable": False,
                 "source_refs": refs}
                for index in (1, 2)
            ]}
        return {"children": []}

    ctx = _ctx()
    result = _run(ctx, overlong_chat, arguments={"points_per_branch": 2})

    assert result.ok, result.content
    assert result.data["source_refs"]["kept"] == 0
    assert result.data["source_refs"]["dropped"] == 6
    assert result.data["source_refs"]["dropped_by_code"] == {
        "UNVERIFIED_SOURCE_REF": 6}
    assert "SOURCE_REFS_DROPPED" in _codes(result)


def test_evidence_unused_signal_survives(monkeypatch):
    """`EVIDENCE_UNUSED` 仍然会报：证据给了、模型一条引用都没写。"""
    ctx = _ctx()
    result = _run(ctx, _chat(per_branch=2, third_level=2, refs=False),
                  arguments={"points_per_branch": 2})

    assert result.ok, result.content
    assert "EVIDENCE_UNUSED" in _codes(result)
    assert result.data["source_refs"]["kept"] == 0
