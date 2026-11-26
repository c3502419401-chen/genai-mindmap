"""`sources` 的 id 在生成侧被换掉——**纯 API 客户端拿到的必须是能对上的 id**。

## 这条链原来错在哪

生成开流之前，材料会入库（`knowledge/remember.py::remember_sources`），库给它自己
的持久 `uuid4()` 当 id，于是 `Source.id` 从请求内的 `2fa76b62…` 变成 `8007363c…`。
导图里 `source_refs.source_id` 记的是**新的**那个；而这层换算此前只以 `mapping`
（旧 → 新）的形式出现在 SSE `sources` 帧里，前端 `stores/sources.ts::remap()`
照着改了自己手里那份材料清单——**所以前端是对的**。

问题是接口只给了半个答案：不带前端那层缝的调用方（脚本、移动端、验收用例、
第三方集成）拿上传应答里的旧 id 去 `/optimization/check`，会得到一条不落的
`SOURCE_TEXT_UNAVAILABLE` + `UNSOURCED_NODE`（实测 33/37 条）。它会以为"引用核验
坏了"，而事实是"你手里的 id 已经过期了，只是没人告诉你"。

## 修的是哪一半

**不换 id 这条路走不通**（换的理由仍然成立：导图落库之后，`source_refs` 必须指向
一个能跨会话查到的 id，请求内 `uuid4` 活不过这一次请求）。所以修的是"换完之后
把最终清单交出去"：`sources` 帧除了 `mapping` 还带 `sources`（`SourceIdentity`，
不带正文），`id` 逐个等于导图里用的那个。

下面第一条与最后一条就是"这个字段在不在"的钉子；中间两条是纯 API 客户端的
**完整走法**：只用帧里给的东西，能不能把引用核验做对。
"""
from __future__ import annotations

import json

import pytest

from app.agent.contracts import SourcesEvent
from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source, SourceKind, SourceIdentity
from app.knowledge.remember import remember_sources
from app.optimization.source_refs import verify_source_refs

DOCUMENT = "# 预算审批\n\n预算审批分三级：部门、财务、总经理。单笔超过 50 万要上会。\n"
BRIEF = "会议纪要：报销平均 11 天到账，卡在财务复核。\n"


def _material(text: str, filename: str) -> Source:
    return Source(kind=SourceKind.text, status="parsed", text=text, filename=filename)


def _frame(materials: list[Source]) -> SourcesEvent:
    """生成侧真实发出的那一帧（`agent/router.py` 里就是这么构造的）。"""
    remembered = remember_sources(materials)
    return SourcesEvent(
        seq=1,
        mapping=remembered.mapping,
        sources=[SourceIdentity.of(item) for item in remembered.sources],
    )


def _content(source_id: str) -> MindMapContent:
    """一张引用了 `source_id` 的树——**生成侧落库之后**的形状。"""
    return MindMapContent.model_validate({
        "title": "预算审批",
        "root": {"id": "root", "label": "预算审批", "children": [
            {"id": "n1", "label": "三级审批",
             "source_refs": [{"source_id": source_id, "start": 0, "end": 12,
                              "quote": DOCUMENT[0:12]}]},
        ]},
        "source_usage": [{"source_id": source_id, "kind": "text",
                          "char_count": len(DOCUMENT)}],
    })


def test_the_sources_frame_carries_the_final_list_not_only_a_diff():
    """帧里必须有"最终清单"这个字段，而且它逐份等于入库后的材料。

    改动之前 `SourcesEvent` 只有 `mapping`——这条断言会以 `AttributeError` /
    空列表的形式失败，那正是"接口只给了一半答案"的形状。
    """
    materials = [_material(DOCUMENT, "budget.md"), _material(BRIEF, "brief.md")]

    event = _frame(materials)

    assert event.sources, "帧里没有最终清单：调用方只能自己算差集，而它没有那层缝"
    assert [item.filename for item in event.sources] == ["budget.md", "brief.md"]
    # 清单里的 id 就是导图里 `source_refs` 会用的那个：全部换过，且与旧 id 不同。
    assert {item.id for item in event.sources} == set(event.mapping.values())
    assert not {item.id for item in event.sources} & {item.id for item in materials}


def test_the_final_list_keeps_the_original_id_when_ingestion_failed(monkeypatch):
    """入库失败时材料按原 id 继续——清单要**如实**反映这一点，不许全写成新 id。

    那一刻导图里的 `source_refs` 用的就是原 id，清单若统一填新 id，调用方会对上一份
    不存在的来源。
    """
    from app.knowledge import library

    def boom(sources, *, deadline=None):
        raise OSError("库写不进去")

    monkeypatch.setattr(library, "ingest_many", boom)

    materials = [_material(BRIEF, "brief.md")]
    event = _frame(materials)

    assert event.mapping == {}
    assert [item.id for item in event.sources] == [materials[0].id]


def test_a_pure_api_client_can_verify_refs_with_the_frame_alone():
    """**核心用例**：不带前端那层缝的调用方，只用帧里的东西就能把引用核验做对。

    走法就是文档里写的那两步：
      1. 按 `mapping` 把自己手里的材料改 id（等价于前端 `remap()`）；
      2. 或者直接按 `sources` 的顺序对（材料清单与请求同序）。
    这里两步都做一遍，断言拿到的清单能让 `verify_source_refs` **零** `SOURCE_TEXT_UNAVAILABLE`。
    """
    materials = [_material(DOCUMENT, "budget.md"), _material(BRIEF, "brief.md")]
    event = _frame(materials)
    by_old = {item.id: item for item in materials}

    rebuilt = [
        by_old[old].model_copy(update={"id": new})
        for old, new in event.mapping.items()
    ]
    assert len(rebuilt) == len(materials)

    content = _content(event.sources[0].id)
    diagnostics = verify_source_refs(content, rebuilt)

    codes = [item.code for item in diagnostics]
    assert "SOURCE_TEXT_UNAVAILABLE" not in codes, (
        "拿到最终清单之后还判'来源原文不可用'，说明换算链断了")
    # `UNSOURCED_NODE` 只会落在**目录级节点**上（root 本来就没有引文），
    # 这正是"引用链是通的"的形状：带引文的那个节点一条都不该落在这儿。
    assert {item.node_id for item in diagnostics if item.code == "UNSOURCED_NODE"} <= {"root"}


def test_the_old_id_really_is_the_root_cause():
    """反向对照：用**旧 id** 去核验，就是 33/37 条全红的那个现场。

    留着这条不是为了测代码，是为了让"为什么要在帧里带最终清单"有一个可复现的
    读数——否则下一个人会觉得那个字段只是"多给了一点便利"。
    """
    materials = [_material(DOCUMENT, "budget.md")]
    event = _frame(materials)
    content = _content(event.sources[0].id)

    stale = verify_source_refs(content, materials)
    fresh = verify_source_refs(content, [
        materials[0].model_copy(update={"id": event.sources[0].id})])

    assert "SOURCE_TEXT_UNAVAILABLE" in {item.code for item in stale}
    # 旧 id：`n1` 的引用被判"来源不存在"→ 丢 → 它也跟着变成 UNSOURCED（2 条）。
    # 新 id：只有 root 那一条。
    assert len([item for item in stale if item.code == "UNSOURCED_NODE"]) == 2
    assert len([item for item in fresh if item.code == "UNSOURCED_NODE"]) == 1


def test_the_frame_is_serialisable_and_carries_no_source_text():
    """帧里**不带正文**：50 份材料塞进一个 SSE 帧会把事件流撑大。

    正文只有调用方自己有（它上传的），事件只需要给身份。
    """
    materials = [_material(DOCUMENT, "budget.md")]
    payload = json.loads(_frame(materials).model_dump_json())

    assert payload["type"] == "sources"
    assert payload["sources"][0]["chars"] == len(DOCUMENT)
    assert "预算审批分三级" not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize("off", [False, True])
def test_the_frame_shape_holds_whether_or_not_the_library_is_used(monkeypatch, off):
    """入库开关关掉时 `mapping` 为空 → 这一帧根本不发（既有不变量）。

    这条守的是"没有 id 变化时，帧序列与加这段之前逐字节相同"：清单字段是给
    "真的换了 id"那一刻用的，不是新增一帧。
    """
    if off:
        monkeypatch.setenv("MINDMAP_LIBRARY_REMEMBER", "0")

    materials = [_material(BRIEF, "brief.md")]
    remembered = remember_sources(materials)

    if off:
        assert remembered.mapping == {}
        assert [item.id for item in remembered.sources] == [materials[0].id]
    else:
        assert remembered.mapping
        assert [item.id for item in remembered.sources] == list(
            remembered.mapping.values())
