"""`DEFAULT_LIMIT` 从 3 提到 8：条数、请求预算、源端候选宽度是同一件事的三个数。

limit 涨而预算不涨，维基的逐条抓取会被预算悄悄掐掉一半——那是"某个源悄悄变薄"的
静默故障；limit 涨而源端窗口不涨，OpenAlex / Crossref 仍然只给 5 条候选，排序再准
也拿不到池子外的东西。所以这三条断言写在一个文件里，改一个数就会一起红。

条数本身怎么选出来的见 `docs/EXTERNAL-SELECTION.md`（4 个 limit × 三个真主题的
相关条数、材料字数、骨架支数与真实的端到端读数），这里只钉结论。
"""
from __future__ import annotations

from app.knowledge import external
from test_external_selection import _hit


def test_default_limit_is_eight() -> None:
    """3 → 8 的依据是读数：同一份池子上 limit=3 只留 17 条相关、limit=8 留 42 条。

    而"名额不够"正在扔真依据——`城市雨污分流改造` 一题池内有 15 条相关候选，
    3 个名额只能给 3 条，`limit=8` 时 8 条全是相关。
    """
    assert external.DEFAULT_LIMIT == 8


def test_request_budget_scales_with_the_limit() -> None:
    """请求预算随 `limit` 线性涨：`limit=3` 时是改动之前那个 13，`limit=8` 时是 23。"""
    assert external._request_limit(3) == 13
    assert external._request_limit(8) == 23
    assert external.REQUEST_LIMIT == external._request_limit(external.DEFAULT_LIMIT)


def test_candidate_window_asks_sources_for_at_least_the_limit() -> None:
    """向源端要的条数不能少于 `limit`；而 `limit=1/2` 那种调用仍该拿到 5 条候选池。"""
    assert external.SOURCE_CANDIDATES == 5
    assert external._candidate_window(1) == 5
    assert external._candidate_window(3) == 5
    assert external._candidate_window(8) == 8
    assert external._candidate_window(12) == 12


def test_material_inventory_still_fits_at_eight_materials() -> None:
    """8 份材料在骨架的清单里只占 8 行、几百字（`INVENTORY_MAX_LINES` 是 20 行）。

    这是"提高 limit 会不会把清单撑满"这个担心在改之前的答案：清单每行是"文件名 +
    字数"，不是"标题 + 前 200 字"。`DEFAULT_LIMIT=3` 那条注释里的理由（"条数多了
    会把清单撑满"）随清单的格式一起过期了。
    """
    from app.agent import tools

    sources = [
        tools._external_source(_hit(f"材料{index}", "正文" * 200, source_name="openalex"))
        for index in range(8)
    ]
    inventory = tools._material_inventory(sources, has_structure=False)

    assert len([line for line in inventory.splitlines() if line.strip()]) <= 9
    assert len(inventory) < 500
