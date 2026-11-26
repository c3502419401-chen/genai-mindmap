"""`link.suggest` 接进 `/optimization/suggest` 的**接线**用例。

## 为什么这组必须存在

`tests/test_link_suggestions.py` 那 8 条把 `link.suggest` 本身钉得很牢：候选池、
证据回指、提示词顺序、失败降级。它们全绿，而产品里**一个调用者都没有**——
`/optimization/suggest` 的产出里从来没有 `kind="link"`，用户点「检查结构」看到的
永远只有规则层的 expand / dedupe / add_detail。

那不是逻辑写错，是接线没做。用例测的是函数，不是链路，所以它不会变红。
下面第一条就是补这一枪：**经过 HTTP，断言响应里出现 `kind="link"`**。

模型出口在 `app.generation.service.chat_async` 上替换（`link.suggest` 是运行时
import 的），所以全程离线。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from conftest import guest_client

from app.contracts.mindmap import MindMapContent
from app.optimization import service

# 两个一级分支各挂一个叶子：这正是 `SINGLE_CHILD` 的形状，也是 link 的目标来源。
TREE = {
    "title": "示例",
    "root": {
        "id": "root", "label": "主题", "children": [
            {"id": "a", "label": "成本", "children": [
                {"id": "a1", "label": "人力", "children": []},
            ]},
            {"id": "b", "label": "风险", "children": [
                {"id": "b1", "label": "合规", "children": []},
            ]},
        ],
    },
}


def _answer(parent_id: str) -> dict:
    return {"content": json.dumps({
        "what": "这一支出在讲什么",
        "line": "风险这一支",
        "candidates": [{"parent_id": parent_id, "label": "风险", "why": "同一支",
                        "evidence": ["node:root"]}],
    }, ensure_ascii=False)}


@pytest.fixture
def fake_model(monkeypatch):
    """把模型出口换成脚本：每次返回"首选父 = 给定父"。"""

    def install(parent_id: str):
        calls: list[str] = []

        async def fake_chat(system: str, user: str, **kwargs):
            calls.append(user)
            return _answer(parent_id)

        monkeypatch.setattr("app.generation.service.chat_async", fake_chat)
        return calls

    return install


# ---------------------------------------------------------------- 接线本身


def test_suggest_endpoint_produces_a_link_suggestion(fake_model):
    """**这条在接上之前必红**：`/suggest` 的产出里有 `kind="link"`。

    它测的不是 link 算得对不对（那由 `test_link_suggestions.py` 管），而是
    "它会不会出现在用户面前"。
    """
    calls = fake_model("b")

    with guest_client() as api:
        response = api.post("/api/optimization/suggest", json={"content": TREE})

    assert response.status_code == 200
    body = response.json()
    link_items = [item for item in body["suggestions"] if item["kind"] == "link"]
    assert link_items, "link 建议没有出现在 /suggest 的产出里"
    assert calls, "一次模型调用都没发生：这一层根本没跑"
    assert body["link"]["status"] == "ok"
    assert body["link"]["calls"] >= 1
    # 界面要靠这两个东西：点得回节点、读得到候选。
    assert link_items[0]["node_id"] == "a1"
    payload = json.loads(link_items[0]["evidence"])
    assert payload["suggestion_kind"] == "link"
    assert payload["candidates"][0]["parent_id"]


# ---------------------------------------------------------------- 目标与状态


def test_targets_come_from_shape_diagnostics_first():
    """`SINGLE_CHILD` 的落点是**父节点**，而 link 问的是那个孩子该挂哪。

    拿父节点去问，模型回答"它就该在这儿"是必然的——那是一条永远不产出的建议。
    """
    content = MindMapContent.model_validate(TREE)

    targets = service.link_targets(content)

    assert targets == ["a1"], "一次只问一个目标（成本闸，见 MAX_LINK_TARGETS 的实测）"


def test_a_clean_tree_still_has_targets():
    """一张**没有任何形态诊断**的图也要有入口。

    真实生成的图多半是这种（实测一棵 37 节点的干净树 0 条诊断）。只认诊断的话
    这一层在正常图上永远不出现，等于又回到了"产品里看不到"。
    """
    clean = MindMapContent.model_validate({
        "title": "干净",
        "root": {"id": "root", "label": "题", "children": [
            {"id": "a", "label": "甲", "children": [
                {"id": "a1", "label": "甲一", "children": []},
                {"id": "a2", "label": "甲二", "children": []}]},
            {"id": "b", "label": "乙", "children": [
                {"id": "b1", "label": "乙一", "children": []},
                {"id": "b2", "label": "乙二", "children": []}]},
        ]},
    })
    assert not [item for item in service.check(clean)
                if item.code in service.LINK_TARGET_CODES]

    targets = service.link_targets(clean)

    assert targets and "root" not in targets
    assert len(targets) <= service.MAX_LINK_TARGETS


def test_a_single_node_tree_has_no_targets():
    tiny = MindMapContent.model_validate(
        {"title": "空", "root": {"id": "root", "label": "题", "children": []}})

    assert service.link_targets(tiny) == []


def test_the_current_parent_being_the_top_choice_means_no_suggestion(fake_model):
    """模型说"现在挂得对"→ 不产出建议，状态说清原因。

    不丢的话，一张本来没问题的图会收到几条"可以挂到它现在待的地方"——用户点一次
    就会开始不信这个面板。
    """
    # 目标节点是 `a1`（`SINGLE_CHILD` 的落点往下挪一层），它现在挂在 `a` 下面。
    # 让模型说"首选父 = a"——那就是"不用动"。
    fake_model("a")

    outcome = asyncio.run(service.suggest_all(
        MindMapContent.model_validate(TREE), use_link=True))

    assert [item for item in outcome.suggestions if item.kind == "link"] == []
    assert outcome.link.status == "already_best"
    assert outcome.link.dropped >= 1


def test_link_can_be_turned_off_per_request():
    """`link=false` → 这一层一次调用都不发（`link` 为 null 表示"这次没跑它"）。"""
    with guest_client() as api:
        body = api.post("/api/optimization/suggest",
                        json={"content": TREE, "link": False}).json()

    assert body["link"] is None
    assert "link" not in [item["kind"] for item in body["suggestions"]]


def test_the_deployment_switch_can_turn_it_off(monkeypatch):
    """`MINDMAP_OPTIMIZATION_LINK=0` 仍然能关掉（部署方要省这笔钱时）。"""
    monkeypatch.setenv("MINDMAP_OPTIMIZATION_LINK", "0")

    outcome = asyncio.run(service.suggest_all(MindMapContent.model_validate(TREE)))

    assert outcome.link.status == "disabled"
    assert outcome.link.calls == 0


def test_rule_suggestions_still_come_first():
    """产出顺序：规则层在前、link 在后。

    规则层是免费的、同步算完的，用户先看到不用等的那几条；等模型的那一条落在
    末尾。顺序反了会让"面板打开后先空两秒"变成常态。
    """
    outcome = asyncio.run(service.suggest_all(
        MindMapContent.model_validate(TREE), use_link=False))

    assert outcome.suggestions, "这棵树有 SINGLE_CHILD，规则层必须有建议"
    assert all(item.kind != "link" for item in outcome.suggestions)
