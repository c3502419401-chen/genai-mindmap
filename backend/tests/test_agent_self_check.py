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
