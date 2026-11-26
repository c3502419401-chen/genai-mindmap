"""三条接线缺口的**基线复现**：每一条都在本文提交时是红的。

为什么单独一个文件：`docs/ACCEPTANCE-EVIDENCE.md` 的 B1 / B3 / B4 是靠人读日志发现的，
而"代码在、用例绿、产品里没接上"这一类，**任何既有用例都不会变红**。所以先写下三条
"红得清楚"的断言，再分三次把它们修绿——这样缺口本身在 `git log` 里是可见的，
而不是只存在于一份文档里。

三条各自守什么、修在哪：

1. `sources` 帧只有 `mapping`（换算），没有最终清单 → `fix:` 那一条；
2. `/api/knowledge/search` 恒 `implemented=false`（桩）→ `feat:` 检索那一条；
3. `/optimization/suggest` 从不产 `kind="link"`（`link.suggest` 零调用）→ `feat:` 补关系那一条。

修完之后这三条与各自的详测（`test_source_id_contract.py` /
`test_knowledge_search_route.py` / `test_link_wiring.py`）会重合。留着它们不是冗余：
它们是"这三处曾经是什么形状"的唯一可执行记录，删掉之后只剩文档里的一段话。
"""
from __future__ import annotations

import json

import pytest
from conftest import guest_client

from app.agent.contracts import SourcesEvent

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


def test_sources_frame_carries_the_final_list():
    """缺口一：帧里只有 `mapping` 时，非前端调用方拿不到"最终清单"。

    这条断言很硬：字段不存在就是不存在。它变红的时候，纯 API 客户端只能自己
    按 `mapping` 算差集——而它们不知道要算。
    """
    assert "sources" in SourcesEvent.model_fields, (
        "`sources` 帧必须同时给最终清单（SourceIdentity），不能只给旧→新的 mapping")
    assert "mapping" in SourcesEvent.model_fields, "`mapping` 要保留：删了会让前端白改一遍"


def test_knowledge_search_is_wired():
    """缺口二：库里有向量，路由却恒 `implemented=false`。

    临时库是空的，所以这里不要求命中——只要求"接上了没有"这个字段说的是真话。
    有材料时真的检索得到，见 `tests/test_knowledge_search_route.py`。
    """
    with guest_client() as api:
        response = api.post("/api/knowledge/search", json={"query": "碳中和"})

    assert response.status_code == 200
    assert response.json()["implemented"] is True


def test_suggest_produces_link_suggestions(monkeypatch):
    """缺口三：`link.suggest` 算法齐备，而 `/suggest` 从不产 `kind="link"`。

    这条**必须经过 HTTP**：算法自己的 8 条用例（`test_link_suggestions.py`）在缺口
    存在的整段时间里一直是绿的——它们测的是函数，不是链路。
    """
    async def fake_chat(system: str, user: str, **kwargs):
        return {"content": json.dumps({
            "what": "这一支在讲什么", "line": "风险这一支",
            "candidates": [{"parent_id": "b", "label": "风险", "why": "同一支",
                            "evidence": []}],
        }, ensure_ascii=False)}

    monkeypatch.setattr("app.generation.service.chat_async", fake_chat)

    with guest_client() as api:
        body = api.post("/api/optimization/suggest", json={"content": TREE}).json()

    kinds = [item["kind"] for item in body["suggestions"]]
    assert "link" in kinds, "补关系这一层没有出现在 /suggest 的产出里"


@pytest.mark.parametrize("path", ["/api/knowledge/search", "/api/optimization/suggest"])
def test_the_three_gaps_are_about_wiring_not_logic(path):
    """三处的共同点：**函数是好的，接线没做**。

    所以它们的入口都必须能被 HTTP 直接调到——"只能从内部调"正是这类缺口的表现。
    """
    with guest_client() as api:
        if path.endswith("search"):
            status = api.post(path, json={"query": "x"}).status_code
        else:
            status = api.post(path, json={"content": TREE}).status_code
    assert status == 200
