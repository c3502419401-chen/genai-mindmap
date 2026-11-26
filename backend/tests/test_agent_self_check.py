"""结构自检回路（`MINDMAP_SELF_CHECK`）：一次审查调用买到什么、以及**它不改树**。

`docs/SUBAGENT-VS-SELFCHECK.md` 第四节实测过这条回路的产出：两张真实历史产物各
+1 次调用，跨支重复 6→0 / 4→0（逐字判据召回 100%），另抓到 8 条**逐字判据完全
看不见**的语义重复（正落在 `_cross_branch_duplicates` 只比逐字、
`NEAR_DUPLICATE_LABEL` 只比同级的缝里）与 2 条内容跑题。精度 22/25 = 88%，
误报全在 `chain` 一类。

据此定下的两条设计约束（本文件逐条钉住）：

  1. **产出是"可指认的问题清单"，不是"自动修"**。88% 的精度意味着自动改会伤到
     好东西；而"可指认"正好对上"哪一支错了能定位"这个真实诉求。
  2. **降级不阻断**。额度用完 / 调用出错 / 模型没给出可解析的清单，都只让这次
     没有自检，生成照常交付。

时间位置：结构那一次之后、说明那一次之前。理由是那一刻**标签已经定稿**（说明段
只填 `detail`，不改任何 `label`），而清单只要标签与 id —— 等说明写完再看，输入
一个字都不会多，代价只是多等十几秒。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import DETAIL_ONLY_PROMPT, PLAN_PROMPT, build_registry

TOPIC = "报销制度"
BRANCHES = ["费用报销", "差旅报销", "审批流程"]


def _request(model_calls: int = 20) -> AgentRunRequest:
    return AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default",
        budget=RunBudget(max_model_calls=model_calls, max_tool_calls=8,
                         max_seconds=60.0, max_output_tokens=8192, max_repairs=1),
    )


def _ctx(model_calls: int = 20) -> RunContext:
    return RunContext(request=_request(model_calls), sources_by_id={})


def _tree() -> dict:
    """一棵"自检该有话可说"的树：跨支重复一处 + 内容跑题一处，结构本身干净。"""
    return {"title": TOPIC, "root": {
        "id": "root", "label": TOPIC, "detail": "中心主题",
        "children": [
            {"id": "b0", "label": "费用报销", "children": [
                {"id": "b0-0", "label": "单据齐全", "detail": "一句具体的说明。"},
                {"id": "b0-1", "label": "审批时限", "detail": "一句具体的说明。"},
            ]},
            {"id": "b1", "label": "差旅报销", "children": [
                {"id": "b1-0", "label": "单据齐全", "detail": "一句具体的说明。"},
                {"id": "b1-1", "label": "营改增下的纳税管理", "detail": "一句具体的说明。"},
            ]},
            {"id": "b2", "label": "审批流程", "children": [
                {"id": "b2-0", "label": "三级审批", "detail": "一句具体的说明。"},
                {"id": "b2-1", "label": "财务复核", "detail": "一句具体的说明。"},
            ]},
        ]}}


def _ids(tree: dict) -> set[str]:
    def walk(node: dict):
        yield node
        for child in node.get("children") or []:
            yield from walk(child)
    return {node["id"] for node in walk(tree["root"]) if node.get("id")}


def _walk(node: dict):
    """树里所有节点（含根）。根是传进来的那个 dict 本身。"""
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


DETAIL_TEXT = ("这句说明写到八十个字以上，讲清它是什么、怎么做、有什么条件与数字，"
               "这样第三段的产出才符合长度目标。")


class _Model:
    """模型替身：骨架 → 结构 → 说明，中间按需插入一次自检。

    `self_check` 是它给自检那一次的回话（`None` 表示"这次不该有人问自检"，
    真被调到时抛 AssertionError —— 那正是"多花了一次调用"的证据）。
    """

    #: 结构那一次的 user 里出现这个标记就认为是自检那一次（下面用 system 判，够用）。

    def __init__(self, *, self_check: dict | None | str | Exception = None) -> None:
        self.self_check = self_check
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system: str, user: str, *, max_tokens=None,
                       on_delta=None) -> dict:
        self.calls.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(BRANCHES)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {node_id: DETAIL_TEXT for node_id in _ids(_tree())}
        if self._is_self_check(system):
            assert self.self_check is not None, "这次调用不该发生（自检开关是关的）"
            if isinstance(self.self_check, Exception):
                raise self.self_check
            return self.self_check
        return _tree()

    @staticmethod
    def _is_self_check(system: str) -> bool:
        from app.agent import tools
        prompt = getattr(tools, "SELF_CHECK_PROMPT", None)
        return bool(prompt) and system.startswith(prompt)

    def kinds(self) -> list[str]:
        """按"骨架 / 结构 / 说明 / 自检"给每次调用贴个标签，用来数调用数。"""
        out: list[str] = []
        for system, _ in self.calls:
            if system.startswith(PLAN_PROMPT):
                out.append("plan")
            elif system.startswith(DETAIL_ONLY_PROMPT):
                out.append("detail")
            elif self._is_self_check(system):
                out.append("self_check")
            else:
                out.append("structure")
        return out


def _run(ctx: RunContext, model: _Model):
    registry = build_registry("create", has_sources=False, has_library=False)
    with patch("app.agent.tools.chat_async", new=model):
        return asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate",
                     arguments={"topic": TOPIC, "branches": 3}), ctx))


def _codes(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


# ---------------------------------------------------------------- 1. 现状：没有这条回路


def test_without_the_switch_there_is_no_self_check_call_and_no_key(monkeypatch):
    """现状 = 默认关：`data` 里没有 `self_check` 键、诊断里没有它的码、
    模型调用次数与加这条回路之前**相同**（骨架 + 结构 + 说明）。

    这条用例接线之后仍然要绿：它锁的是"开关关着时，产出与没有这个功能时逐字节
    相同"（`test_edges.TestTreeIsByteIdentical` 那批回归用例同一套口径）。
    """
    monkeypatch.delenv("MINDMAP_SELF_CHECK", raising=False)
    model = _Model()
    result = _run(_ctx(), model)

    assert result.ok, result.content
    assert "self_check" not in result.data
    assert not [code for code in _codes(result) if code.startswith("SELF_CHECK")]
    assert model.kinds() == ["plan", "structure", "detail"], model.kinds()


# ---------------------------------------------------------------- 2. 打开之后

#: 一份"该报的问题"清单：跨支重复一处、语义重复一处、跑题一处、层级异常一处。
ISSUES = {"issues": [
    {"node_id": "b1-0", "kind": "cross_branch_duplicate",
     "problem": "与「费用报销 / 单据齐全」字面相同", "fix": "保留一处，另一处删掉"},
    {"node_id": "b1-1", "kind": "off_topic",
     "problem": "财税政策与报销无关", "fix": "删掉或换成报销相关的一条"},
    {"node_id": "b2-1", "kind": "semantic_duplicate",
     "problem": "与「三级审批」是同一件事的两种说法", "fix": "合并成一条"},
    {"node_id": "b0-1", "kind": "level_odd",
     "problem": "「审批时限」挂错了分支", "fix": "挪到「审批流程」下"},
]}


def _switched_on(monkeypatch) -> None:
    monkeypatch.setenv("MINDMAP_SELF_CHECK", "1")


def test_it_costs_exactly_one_call_between_structure_and_detail(monkeypatch):
    """+1 次调用，位置在**结构之后、说明之前**。

    位置是可测的，而且必须测：清单要的是"标签已定稿"，而说明段只填 `detail`。
    插错了（说明之后）总耗时一样、但晚一屏；插到结构之前，下位节点还不存在。
    """
    _switched_on(monkeypatch)
    model = _Model(self_check=ISSUES)
    result = _run(_ctx(), model)

    assert result.ok, result.content
    assert model.kinds() == ["plan", "structure", "self_check", "detail"], model.kinds()


def test_the_list_is_identifiable_and_lands_in_data_and_one_diagnostic(monkeypatch):
    """清单进 `data["self_check"]`，诊断里只有**一条**汇总（不逐条刷屏）。

    "可指认"是这条回路的全部价值：每条都要有 `node_id` 与**位置路径**，用户才能
    在画布上找到它。诊断只说一句"N 处"——逐条诊断会把面板刷成 15 行，而
    `check()` 那七条规则已经在往那儿写了。
    """
    _switched_on(monkeypatch)
    model = _Model(self_check=ISSUES)
    result = _run(_ctx(), model)

    report = result.data["self_check"]
    assert report["skipped"] is None
    assert report["counts"] == {"cross_branch_duplicate": 1, "off_topic": 1,
                                "semantic_duplicate": 1, "level_odd": 1}
    first = report["issues"][0]
    assert first["node_id"] == "b1-0"
    assert first["path"] == "差旅报销 / 单据齐全"
    assert first["problem"] and first["fix"]

    reported = [item for item in result.data["diagnostics"]
                if item["code"] == "SELF_CHECK_ISSUES"]
    assert len(reported) == 1
    assert "4 处" in reported[0]["message"]
    assert "没有自动改" in reported[0]["message"]
    # 读数不是解码器：输入规模也报了（这张图 9 个节点、没有 detail）。
    assert report["outline"]["nodes"] == 9


def test_the_outline_carries_labels_and_ids_but_no_detail(monkeypatch):
    """喂进去的清单**没有 detail**：只有分支、缩进、id。

    `detail` 让输入翻三倍以上，而四类问题没有一类需要正文（判的都是标题与位置）。
    这条用例拿真的那次调用的 user 去比：模型写回的说明一个字都不该出现在里面。
    """
    _switched_on(monkeypatch)
    model = _Model(self_check=ISSUES)
    _run(_ctx(), model)

    _system, user = model.calls[[index for index, kind in enumerate(model.kinds())
                                 if kind == "self_check"][0]]
    assert "【一级分支】费用报销" in user
    assert "  - 单据齐全  (id=b0-0)" in user
    assert DETAIL_TEXT not in user
    assert "一句具体的说明" not in user


def test_it_does_not_touch_the_tree(monkeypatch):
    """**一个节点都不动**：清单是给人看的，落地要过人的眼。

    精度 22/25 = 88%，自动合并会伤到那 12%，而合并没有反操作（`repair()` 有
    `RepairUndo`，这一条没有——那正是它不该自动落地的一半理由）。
    """
    _switched_on(monkeypatch)
    ctx = _ctx()
    result = _run(ctx, _Model(self_check=ISSUES))

    ids = {node["id"] for node in _walk(ctx.candidate.model_dump()["root"])}
    assert ids == {"root", "b0", "b0-0", "b0-1", "b1", "b1-0", "b1-1",
                   "b2", "b2-0", "b2-1"}
    assert result.data["node_count"] == 10


def test_an_unknown_kind_or_id_is_dropped_and_counted(monkeypatch):
    """自创的 kind、对不上号的 id、不是对象的条目：丢掉并**记数**。

    静默丢的后果是"模型报了 6 条、界面显示 3 条"，而"少了 3 条"在产出上看不出来。
    """
    _switched_on(monkeypatch)
    noisy = {"issues": [
        ISSUES["issues"][0],
        {"node_id": "b1-1", "kind": "other", "problem": "自创一类", "fix": "?"},
        {"node_id": "nope", "kind": "off_topic", "problem": "id 对不上", "fix": "?"},
        "这不是一个对象",
        {"kind": "off_topic", "problem": "没有 id", "fix": "?"},
        {"nodes": ["b0-0", "b2-0"], "kind": "semantic_duplicate",
         "problem": "一条报了两个节点", "fix": "合并"},
    ]}
    model = _Model(self_check=noisy)
    result = _run(_ctx(), model)

    report = result.data["self_check"]
    assert len(report["issues"]) == 3
    assert report["dropped"] == {"unknown_kind": 1, "unknown_id": 2,
                                 "bad_item": 1}


def test_a_failed_call_degrades_to_a_warning(monkeypatch):
    """调用失败 → 一条 `SELF_CHECK_SKIPPED` 告警，生成照常交付。

    为一份清单把一棵已经成型的树判成失败，是拿确定的好换不确定的好
    （与 `_fill_branch_digests` 同一条方向）。但也不能沉默：用户开了开关、
    界面上却什么都没有，唯一的线索就是这一条。
    """
    from app.generation.service import GenerationError

    _switched_on(monkeypatch)
    ctx = _ctx()
    result = _run(ctx, _Model(self_check=GenerationError("上游 500")))

    assert result.ok, result.content
    assert result.data["self_check"]["skipped"] == "error"
    assert result.data["self_check"]["issues"] == []
    codes = _codes(result)
    assert "SELF_CHECK_SKIPPED" in codes
    assert "SELF_CHECK_ISSUES" not in codes
    assert ctx.candidate is not None


def test_no_budget_means_skipped_not_failed(monkeypatch):
    """额度用尽 → `skipped="budget"`，不阻断。额度在**发请求之前**就占（与骨架同一规矩）。"""
    _switched_on(monkeypatch)
    ctx = _ctx(model_calls=2)      # 骨架 1 + 结构 1，到自检时额度已用完
    result = _run(ctx, _Model(self_check=ISSUES))

    assert result.ok, result.content
    assert result.data["self_check"]["skipped"] == "budget"
    assert "SELF_CHECK_SKIPPED" in _codes(result)


def test_two_branch_trees_are_not_worth_a_call():
    """少于三条一级分支不调：那种规模上"跨支重复"要么不存在、要么一眼可见。

    直接调 `_run_self_check`：一个 ctx、一棵两分支的树，`skipped` 必须是
    `no_outline`，而且**一次调用都不发**（`chat_async` 被换成会炸的桩）。
    """
    from app.agent import tools
    from app.contracts.mindmap import MindMapContent

    content = MindMapContent.model_validate({"title": TOPIC, "root": {
        "id": "root", "label": TOPIC, "children": [
            {"id": "b0", "label": "甲", "children": [{"id": "b0-0", "label": "甲一"}]},
            {"id": "b1", "label": "乙", "children": [{"id": "b1-0", "label": "乙一"}]},
        ]}})

    async def boom(*args, **kwargs):
        raise AssertionError("两分支的树不该为自检花一次调用")

    with patch("app.agent.tools.chat_async", new=boom):
        report = asyncio.run(tools._run_self_check(content, _ctx(), topic=TOPIC))
    assert report.skipped == "no_outline"
    assert report.issues == []
