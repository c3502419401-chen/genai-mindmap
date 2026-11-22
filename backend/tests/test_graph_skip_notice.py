"""图谱不可用时的**那句话**：四种原因、两个 code、一个"只报一次"的账本。

## 为什么这一个文件值得存在

降级本身是对的：不为一条注定失败的请求付一次连接超时（`_graph_outline` 的第 2、3
道闸）。但那两道闸原来 `return "", 0`，**对用户一个字都不说**——界面上拿到的图与
"图谱骨架真的定了骨架"那张长得一模一样，于是出现了这样一次排查：

    MINDMAP_GRAPH_INGEST=1 / MINDMAP_GRAPH_OUTLINE=1 都开着，Neo4j 停了
    → 生成照常出图（3 支 / 51 节点）
    → 日志里全是 "图谱实体计数不可用：GraphStoreError"
    → SSE 里一条 diagnostic 都没有
    → 用户的结论："图谱那一条路在跑"（其实一步都没做）

所以这里的断言不是措辞，是**判据**：

  1. 四种原因各一条，且 `code` 只有两个（`GRAPH_OUTLINE_UNAVAILABLE` /
     `GRAPH_INGEST_UNAVAILABLE` + `GRAPH_INGEST_SKIPPED`）——前端按 code 归组，
     原因写在 message 里；
  2. 四种原因**不合并**：它们的处置分别是"改配置 / 起服务 / 等着 / 什么都不用做"，
     合并成一句"图谱不可用"等于让用户去猜；
  3. 开关是用户自己关的（`MINDMAP_GRAPH_OUTLINE=0`）→ **不报**：那不是降级，
     是选择；
  4. 同一个原因在同一个进程里只报一次（`_GRAPH_NOTICES`），第二次返回 `None`——
     冷却 30 秒里连点五次生成不该得到五条同样的提示。
"""
from __future__ import annotations

import asyncio

from app.agent.tools import (
    GRAPH_INGEST_ENV,
    GRAPH_OUTLINE_ENV,
    GRAPH_OUTLINE_MIN_ENTITIES,
    GRAPH_SKIP_COOLING,
    GRAPH_SKIP_DISABLED,
    GRAPH_SKIP_HAS_OUTLINE,
    GRAPH_SKIP_TOO_FEW,
    GRAPH_SKIP_UNCONFIGURED,
    GRAPH_SKIP_UNREACHABLE,
    _graph_ingest_skip_diagnostic,
    _graph_ingest_diagnostic,
    _graph_outline_attempt,
    _graph_outline_skip_diagnostic,
    _ingest_external_graph,
    reset_graph_notices,
)
from app.contracts.source import Source, SourceKind
from app.knowledge import graph_store

PLAIN = ("碳中和与碳达峰是同一件事的两个阶段：碳达峰是排放由增转降的拐点，"
         "碳中和是排放与吸收相抵之后的净零。" * 12)


def _material(text: str = PLAIN, *, source_id: str = "s-plain") -> Source:
    return Source(id=source_id, kind=SourceKind.paper, status="parsed",
                  filename="openalex: 碳中和.md", text=text)


def _fake(monkeypatch, *, configured: bool = True, available: bool = True,
          count: int = 0, cooldown: float = 0.0) -> dict:
    """图存储替身。`cooldown` 与 `count` 分开给：一个是"连不连得上"，一个是"有没有货"。

    两者在真实代码里都来自 `graph_store`，但**语义完全不同**：`count=0 且
    cooldown>0` 是"刚失败"（该去起服务），`count=0 且 cooldown=0` 是"图里没这批
    材料"（材料太少）。判据若把这两种揉在一起，用户就会被告知"材料太少"而实际是
    Neo4j 没起。
    """
    monkeypatch.setenv(GRAPH_OUTLINE_ENV, "1")
    calls: dict = {"count": 0, "subgraph": 0}
    monkeypatch.setattr(graph_store, "configured", lambda: configured)
    monkeypatch.setattr(graph_store, "available", lambda: available)
    monkeypatch.setattr(graph_store, "cooldown_remaining", lambda: cooldown)
    monkeypatch.setattr(graph_store, "endpoint",
                        lambda: ("bolt://127.0.0.1:7687", "neo4j"))

    def fake_count(scope=None):
        calls["count"] += 1
        return count

    def fake_subgraph(**kwargs):
        calls["subgraph"] += 1
        return [], []

    monkeypatch.setattr(graph_store, "scoped_entity_count", fake_count)
    monkeypatch.setattr(graph_store, "subgraph", fake_subgraph)
    return calls


# ---------------------------------------------------------------- 1. 四种原因


def test_unconfigured_says_change_the_config(monkeypatch):
    """没配密码：一句话说清"整条都不走"，并把那个环境变量名报出来。"""
    _fake(monkeypatch, configured=False)
    block, count, reason = _graph_outline_attempt([_material()])

    assert (block, count, reason) == ("", 0, GRAPH_SKIP_UNCONFIGURED)
    diagnostic = _graph_outline_skip_diagnostic(reason, count)
    assert diagnostic is not None
    assert diagnostic.code == "GRAPH_OUTLINE_UNAVAILABLE"
    assert graph_store.PASSWORD_ENV in diagnostic.message
    # 处置是"改配置"，不是"起服务"——两种情形必须能靠 message 分辨。
    assert "配" in diagnostic.message and "run-neo4j" not in diagnostic.message


def test_unreachable_says_start_the_service(monkeypatch):
    """连不上：`count` 与"图里没材料"都返回 0，只有熔断状态能分开它们。"""
    calls = _fake(monkeypatch, count=0, cooldown=31.0)
    block, count, reason = _graph_outline_attempt([_material()])

    assert (block, count, reason) == ("", 0, GRAPH_SKIP_UNREACHABLE)
    assert calls["count"] == 1          # 这一条查询就是"连不上"的来源
    diagnostic = _graph_outline_skip_diagnostic(reason, count)
    assert diagnostic is not None
    assert diagnostic.level.value == "warn"
    assert "bolt://127.0.0.1:7687" in diagnostic.message
    assert "run-neo4j.bat" in diagnostic.message     # 处置：起服务
    assert "31" in diagnostic.message                # 退避剩多少秒


def test_cooling_says_wait(monkeypatch):
    """熔断冷却：处置是"等着"，而且**不该**建议重启后端（那会把计数清零）。"""
    _fake(monkeypatch, available=False, cooldown=28.0)
    block, count, reason = _graph_outline_attempt([_material()])

    assert (block, count, reason) == ("", 0, GRAPH_SKIP_COOLING)
    diagnostic = _graph_outline_skip_diagnostic(reason, count)
    assert diagnostic is not None
    assert "28" in diagnostic.message
    assert "冷却" in diagnostic.message and "不用重启后端" in diagnostic.message


def test_too_few_entities_is_not_a_fault(monkeypatch):
    """实体不足：材料太少，属正常。message 里必须带**那个数**与阈值。"""
    _fake(monkeypatch, count=3)
    block, count, reason = _graph_outline_attempt([_material()])

    assert (block, count, reason) == ("", 3, GRAPH_SKIP_TOO_FEW)
    diagnostic = _graph_outline_skip_diagnostic(reason, count)
    assert diagnostic is not None
    assert diagnostic.level.value == "info"
    assert "3 个" in diagnostic.message
    assert str(GRAPH_OUTLINE_MIN_ENTITIES) in diagnostic.message
    assert "不是故障" in diagnostic.message


def test_switch_off_says_nothing(monkeypatch):
    """用户自己关掉这一档：不报。那不是降级，是选择（与 `configured()` 同一个精神）。"""
    _fake(monkeypatch)
    monkeypatch.setenv(GRAPH_OUTLINE_ENV, "0")
    block, count, reason = _graph_outline_attempt([_material()])

    assert (block, count, reason) == ("", 0, GRAPH_SKIP_DISABLED)
    assert _graph_outline_skip_diagnostic(reason, count) is None


def test_graph_usable_reports_no_skip(monkeypatch):
    """图够用：reason 是空串，`skip` 那句一个字都不出现（可用路径的产出不变）。"""
    _fake(monkeypatch, count=20, cooldown=0.0)
    entities = [graph_store.GraphEntity(name=f"概念{index}", type="概念", degree=9 - index)
                for index in range(GRAPH_OUTLINE_MIN_ENTITIES)]
    monkeypatch.setattr(graph_store, "subgraph", lambda **kwargs: (entities, []))
    block, count, reason = _graph_outline_attempt([_material()])

    assert reason == ""
    assert block and count == GRAPH_OUTLINE_MIN_ENTITIES
    assert _graph_outline_skip_diagnostic(reason, count) is None


# ---------------------------------------------------------------- 2. 只报一次


def test_same_reason_is_reported_once(monkeypatch):
    """冷却 30 秒里连点五次生成：第一次一条诊断，后四次 `None`（日志仍有痕迹）。"""
    reset_graph_notices()
    _fake(monkeypatch, available=False, cooldown=30.0)
    monkeypatch.setenv(GRAPH_OUTLINE_ENV, "1")

    first = _graph_outline_skip_diagnostic(GRAPH_SKIP_COOLING, 0)
    again = _graph_outline_skip_diagnostic(GRAPH_SKIP_COOLING, 0)
    assert first is not None and again is None


def test_different_reasons_each_get_their_own_line(monkeypatch):
    """"只报一次"按**原因**去抖，不是按 code：换一种原因时用户必须再看到一句。"""
    reset_graph_notices()
    _fake(monkeypatch, count=2)
    assert _graph_outline_skip_diagnostic(GRAPH_SKIP_TOO_FEW, 2) is not None
    assert _graph_outline_skip_diagnostic(GRAPH_SKIP_TOO_FEW, 2) is None
    assert _graph_outline_skip_diagnostic(GRAPH_SKIP_UNCONFIGURED, 0) is not None
    assert _graph_outline_skip_diagnostic(GRAPH_SKIP_UNREACHABLE, 0) is not None


def test_reset_brings_the_notice_back(monkeypatch):
    """账本可清（`conftest` 每个用例清一次，否则第二个用例永远看不到诊断）。"""
    reset_graph_notices()
    _fake(monkeypatch, count=0, cooldown=30.0)
    assert _graph_outline_skip_diagnostic(GRAPH_SKIP_UNREACHABLE, 0) is not None
    assert _graph_outline_skip_diagnostic(GRAPH_SKIP_UNREACHABLE, 0) is None
    reset_graph_notices()
    assert _graph_outline_skip_diagnostic(GRAPH_SKIP_UNREACHABLE, 0) is not None


# ---------------------------------------------------------------- 3. 入图那一侧


def test_ingest_unavailable_is_reported(monkeypatch):
    """入图因为"没配 / 连不上"没做时，出参里带着原因，转成一条诊断。"""
    reset_graph_notices()
    monkeypatch.setenv(GRAPH_INGEST_ENV, "1")
    _fake(monkeypatch, configured=False)
    sink: list[str] = []

    assert asyncio.run(_ingest_external_graph([_material()], skip_sink=sink)) is None
    assert sink == [GRAPH_SKIP_UNCONFIGURED]
    diagnostic = _graph_ingest_skip_diagnostic(sink[0])
    assert diagnostic is not None
    assert diagnostic.code == "GRAPH_INGEST_UNAVAILABLE"
    assert graph_store.PASSWORD_ENV in diagnostic.message


def test_ingest_cooling_is_reported(monkeypatch):
    reset_graph_notices()
    monkeypatch.setenv(GRAPH_INGEST_ENV, "1")
    _fake(monkeypatch, available=False, cooldown=45.0)
    sink: list[str] = []

    assert asyncio.run(_ingest_external_graph([_material()], skip_sink=sink)) is None
    assert sink == [GRAPH_SKIP_COOLING]
    diagnostic = _graph_ingest_skip_diagnostic(sink[0])
    assert diagnostic is not None
    assert diagnostic.level.value == "warn"
    assert "45" in diagnostic.message


def test_ingest_with_outline_is_reported_as_saving(monkeypatch):
    """有章节目录时跳过入图：这不是故障，是"省下了抽取那笔钱"，code 也分开。"""
    reset_graph_notices()
    monkeypatch.setenv(GRAPH_INGEST_ENV, "1")
    _fake(monkeypatch)
    chaptered = "# 目标\n\n" + ("碳达峰与碳中和是两个阶段。" * 30) + "\n\n# 路径\n\n" + (
        "节能、替代、封存三条路径。" * 30)
    sink: list[str] = []

    assert asyncio.run(_ingest_external_graph([_material(chaptered)], skip_sink=sink)) is None
    assert sink == [GRAPH_SKIP_HAS_OUTLINE]
    diagnostic = _graph_ingest_skip_diagnostic(sink[0])
    assert diagnostic is not None
    assert diagnostic.code == "GRAPH_INGEST_SKIPPED"
    assert "章节目录" in diagnostic.message


def test_ingest_without_sink_still_returns_none(monkeypatch):
    """老调用方（不给 `skip_sink`）行为不变：出参是可选的，返回值仍然是 None。"""
    reset_graph_notices()
    monkeypatch.setenv(GRAPH_INGEST_ENV, "1")
    _fake(monkeypatch, configured=False)

    assert asyncio.run(_ingest_external_graph([_material()])) is None


def test_ingest_all_failed_is_a_failure_not_a_done(monkeypatch):
    """一份都没写进去 ≠ 没有材料可入图。

    `chunks` 有数只说明"材料被切开了"。按原来的顺序，Neo4j 停机那一次会得到一句
    "这次把 2 份检索材料抽进了图谱（2 块 / 0 实体 / 0 边）"——那是本轮反例里真实出现
    过的一句话。判据必须站在 `chunks` 前面。
    """
    stats = {"materials": 2, "skipped": 0, "chunks": 2, "entities": 0, "edges": 0,
             "failed": 2, "aborted": False, "seconds": 7.0}
    diagnostic = _graph_ingest_diagnostic(stats)

    assert diagnostic.code == "GRAPH_INGEST_FAILED"
    assert diagnostic.level.value == "warn"
    assert "一份都没能写进图谱" in diagnostic.message
    assert "抽进了图谱" not in diagnostic.message


def test_ingest_partial_failure_keeps_the_done_line_but_says_so(monkeypatch):
    """进了一部分：仍是 DONE，但消息里必须带"其中 N 份失败"。"""
    stats = {"materials": 3, "skipped": 0, "chunks": 5, "entities": 18, "edges": 15,
             "failed": 1, "aborted": False, "seconds": 4.9}
    diagnostic = _graph_ingest_diagnostic(stats)

    assert diagnostic.code == "GRAPH_INGEST_DONE"
    assert "18 实体" in diagnostic.message
    assert "其中 1 份失败" in diagnostic.message


def test_ingest_diagnostic_is_silent_when_switch_is_off(monkeypatch):
    """开关关着（默认）：一个字节都不加——键集与加这一段之前逐字节相同。"""
    reset_graph_notices()
    monkeypatch.setenv(GRAPH_INGEST_ENV, "0")
    _fake(monkeypatch, configured=False)

    assert asyncio.run(_ingest_external_graph([_material()], skip_sink=[])) is None
    assert _graph_ingest_skip_diagnostic(GRAPH_SKIP_DISABLED) is None
