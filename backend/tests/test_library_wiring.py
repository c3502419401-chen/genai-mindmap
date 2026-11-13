"""知识库接进 agent 链路：`rag_query` 查库、库来源进校验、模型知道库存在。

为什么单独一个文件：`knowledge/library.py` 与 `search_parent(library=True)` 早就
测过了（`tests/test_library.py`），缺的是**生产调用方**。这条链有三处必须同时
成立，任何一处漏掉都让"接了库"变成一个负资产：

  1. `_rag` 的早退。库里 200 份、请求不带 sources 时，`if not sources: return
     _fail("no_sources")` 会让整条链在第一步退出——检索能力明明在，一次都用不上。
  2. **库命中的引用要过校验**。`verify_source_refs` 用 `by_id = {source.id: source}`
     查原文，而库命中的 `source_id` 是持久 id、不在本次 `request.sources` 里。
     不把 `library.load()` 取回的 `Source` 并进去，一条**逐字完全正确**的引用会被
     判成"来源不存在"、在 `remove_invalid=True` 时被静默剔除：检索成功、引用写出、
     收尾时消失。这是整条链上最隐蔽的一种失败，也是本文件里断言写得最细的地方
     ——必须断言"校验后引用还在"，"校验没抛异常"证明不了任何事。
  3. **模型得知道库存在**。材料清单只有 `request.sources`，system 里"先检索"的
     前提也是 `request.sources`，注册表也只在 `has_sources` 时给 `rag_query`。
     三处一起改，模型才有理由、也有工具去查。

第 6 组是回归闸：`MINDMAP_LIBRARY_SEARCH=0` 时与 `07a2740` 的旧模块**逐字节**
相同（`content` 与 `data` 的键集都要比），并且一次库查询都不发生。

全部离线：`conftest.py` 清空了凭证、把数据库落到本次用例的 `tmp_path`，知识库
因此是"每个用例一份空库"。`MINDMAP_PARENT_RETRIEVAL` 在 conftest 里是 `0`
（单级切块，命中没有叶子区间），需要两级坐标的用例自己 `delenv` 打开。
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.agent import tools as agent_tools
from app.agent.contracts import (
    MAX_LIBRARY_SOURCES,
    AgentRunRequest,
    Completion,
    RunBudget,
    RunContext,
    ToolCall,
)
from app.agent.service import run
from app.agent.tools import build_registry
from app.contracts.source import Source, SourceKind
from app.knowledge import library
from app.knowledge.service import PARENT_RETRIEVAL_ENV, EvidenceHit
from app.optimization.source_refs import verify_source_refs

# 写死基线，不跟随 HEAD：这一轮之后就改了 `tools.py`，拿 HEAD 对比等于自己跟
# 自己比。rev 与内容各钉一道——只有 rev 时改常量就能换掉基线，只有 hash 时看不出
# 这份基线是哪一版（与 `tests/test_library.py` 同一条规矩）。
BASELINE_REV = "07a2740"
BASELINE_SHA256 = "a9712e2b9fb96e2f22361763efd7c7429f0febaff4af6dd4eaac51e403bbf63e"

REPO_ROOT = Path(__file__).resolve().parents[2]

# 一份能切出多个父块、且各节互不共享关键词的合成材料。真实材料在最后一组用
# `docs/` 下的 md 跑完整链路，这里用小材料是为了让每条断言都能指着具体字符串。
MATERIAL = (
    "# 制度汇编\n\n"
    "## 预算审批\n\n"
    "预算审批由部门负责人签核，超过额度的部分必须上报总经理复审，"
    "财务只在核准后执行付款，权责边界写在制度附件里。\n\n"
    "## 报销时限\n\n"
    "费用报销的时限是三十个自然日，逾期不再受理，"
    "票据缺失的申请一律退回并通知经办人补齐材料。\n\n"
    "## 数据备份\n\n"
    "数据备份每日增量、每周全量，异地副本保留三十天，"
    "恢复演练每半年做一次并记录耗时。\n\n"
    "## 培训计划\n\n"
    "培训计划每季度滚动调整，讲师与场地提前两周确认，"
    "缺席超过三次的学员要重新报名。\n"
)

QUERY = "报销时限三十个自然日"
MISS = "量子色动力学的渐近自由"


def _stored(text: str = MATERIAL, **kwargs) -> Source:
    """一份待入库/带在请求里的来源。"""
    return Source(kind=SourceKind.text, status="parsed", text=text, **kwargs)


def _ctx(sources: list[Source], *, intent: str = "expand",
         candidate=None) -> RunContext:
    return RunContext(
        request=_request(sources, intent=intent), candidate=candidate,
        sources_by_id={source.id: source for source in sources},
    )


def _request(sources: list[Source], *, intent: str = "expand") -> AgentRunRequest:
    return AgentRunRequest(
        goal="整理材料", intent=intent, thinking_model="default", sources=sources,
        budget=RunBudget(max_model_calls=10, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=4096, max_repairs=1),
    )


def _call(name: str, call_id: str, **arguments: Any) -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments=arguments)


def _exec_rag(ctx: RunContext, *, intent: str = "expand",
              has_sources: bool | None = None, **arguments: Any):
    """按生产形态执行一次 `rag_query`。

    `has_sources` 缺省取"请求里到底有没有材料"——那正是 `service.run()` 的算法。
    库非空时注册表会自己把 `rag_query` 加进来，所以"请求不带 sources"这条用例
    走的是与线上完全相同的注册路径。
    """
    if has_sources is None:
        has_sources = bool(ctx.request.sources)
    registry = build_registry(intent, has_sources=has_sources)
    return asyncio.run(registry.execute(
        _call("rag_query", "c-rag", **{"question": QUERY, "top_k": 5, **arguments}), ctx))


def _leaf_hit(rag) -> dict[str, Any]:
    """取一条带子块区间、且能整段抄进 `SourceRef.quote`（<=300 字）的命中。"""
    return next(
        hit for hit in rag.data["hits"]
        if hit["leaf_start"] is not None and hit["leaf_end"] is not None
        and 0 < hit["leaf_end"] - hit["leaf_start"] <= 300
    )


def _ref(source_id: str, start: int, end: int, text: str) -> dict[str, Any]:
    return {"source_id": source_id, "start": start, "end": end,
            "quote": text[start:end]}


def _tree(ref: dict[str, Any]) -> dict[str, Any]:
    """一条一级分支带引用、另外两条不带：3 条是 `MIN_BRANCHES` 的下限。"""
    return {
        "title": "库引用",
        "root": {"id": "root", "label": "根", "children": [
            {"id": "a", "label": "甲", "children": [], "source_refs": [ref]},
            {"id": "b", "label": "乙", "children": []},
            {"id": "c", "label": "丙", "children": []},
        ]},
    }


# ---------------------------------------------------------------- 1 库能被查到


def test_rag_retrieves_from_the_library_without_request_sources(monkeypatch):
    """核心用例：库里有材料、请求**不带任何 sources**，`_rag` 照样查得到。

    其余七组都是围绕这一条的保障：没有它，后面全是空转。
    """
    monkeypatch.delenv(PARENT_RETRIEVAL_ENV, raising=False)
    source_id = library.ingest(_stored(filename="制度.md"))
    ctx = _ctx([])

    result = _exec_rag(ctx)

    assert result.ok, result.content
    hits = result.data["hits"]
    assert hits, "库里有这份材料，检索必须命中"
    assert hits[0]["source_id"] == source_id
    assert result.data["library_sources"] >= 1
    # 库来源被取回并记进 ctx（收尾校验要用它的正文）
    assert source_id in ctx.library_sources
    assert ctx.library_sources[source_id].text == MATERIAL
    # 回给模型的 evidence_* 是**库来源**的原文切片：拿不到正文时这一组会缺失，
    # 模型于是只能抄一条空引文（见 `_citable_hits`）。
    payload = json.loads(result.content)
    entry = next(item for item in payload if item["start"] == hits[0]["start"])
    assert entry["evidence_quote"] == MATERIAL[entry["evidence_start"]:entry["evidence_end"]]


def test_library_hit_ref_survives_verification(monkeypatch):
    """库命中的引用**校验后还在**——断言的是"还在"，不是"没抛异常"。

    这条钉的是整条链最隐蔽的一处：`verify_source_refs` 用
    `by_id = {source.id: source}` 匹配，库命中的 `source_id` 是持久 id。收尾只传
    `request.sources`（这里是空的）时，一条逐字完全正确的引用会被判
    `SOURCE_TEXT_UNAVAILABLE` 并在 `remove_invalid=True` 那一步被剔除。
    检索成功、引用写出、最后消失，而且没有任何一处日志会说清。
    """
    monkeypatch.delenv(PARENT_RETRIEVAL_ENV, raising=False)
    source_id = library.ingest(_stored(filename="制度.md"))
    ctx = _ctx([], intent="create")
    hit = _leaf_hit(_exec_rag(ctx))
    ref = _ref(source_id, hit["leaf_start"], hit["leaf_end"], MATERIAL)
    assert ref["quote"].strip()

    with patch("app.agent.tools.chat_async", AsyncMock(return_value=_tree(ref))):
        result = asyncio.run(build_registry("create").execute(
            _call("mindmap_generate", "c-gen", topic="整理这些材料"), ctx))

    assert result.ok, result.content
    kept = ctx.candidate.root.children[0].source_refs
    assert [item.source_id for item in kept] == [source_id]
    assert kept[0].quote == ref["quote"]
    codes = {item["code"] for item in result.data["diagnostics"]}
    assert not codes & {"UNKNOWN_SOURCE_REF", "SOURCE_TEXT_UNAVAILABLE",
                        "SOURCE_QUOTE_MISMATCH", "SOURCE_REF_OUTSIDE_EVIDENCE"}, codes
    # `source_usage` 必须包含这条库来源：契约要求"每条引用的 source_id 都在
    # source_usage 里"，少了它整棵树会是 `tree_invalid`。
    assert source_id in {usage.source_id for usage in ctx.candidate.source_usage}

    # 反证：只拿 `request.sources`（空）去校验时，这条同样逐字的引用会被剔掉。
    stripped = ctx.candidate.model_copy(deep=True)
    reports = verify_source_refs(stripped, [], remove_invalid=True)
    assert "SOURCE_TEXT_UNAVAILABLE" in {item.code for item in reports}
    assert stripped.root.children[0].source_refs == []


def test_missing_library_source_is_skipped_with_warning(caplog):
    """来源被 `prune` 删掉时降级：跳过这一份、记 warning，运行不失败。

    `load` 取不到不等于整次运行该失败——检索已经成功、用户拿到的是真实片段。
    取不到的那条命中只是拿不到 `evidence_*`，模型仍能看到 quote。
    """
    source_id = library.ingest(_stored(filename="制度.md"))
    assert library.forget(source_id) is True
    ctx = _ctx([])
    hit = EvidenceHit(source_id=source_id, start=0, end=20, quote=MATERIAL[:20],
                      score=0.1, leaf_start=0, leaf_end=20)

    with patch("app.agent.tools.search_parent", return_value=[hit]):
        with caplog.at_level(logging.WARNING, logger="app.agent.tools"):
            result = _exec_rag(ctx, has_sources=True)

    assert result.ok, result.content              # 不升级成运行失败
    assert ctx.library_sources == {}
    assert result.data["library_sources"] == 0
    assert "已不存在" in caplog.text
    # 取不到正文就一个 `evidence_*` 都不给：只给偏移会让模型抄一条空引文，
    # 那条引用随后必然被 `SOURCE_QUOTE_MISMATCH` 剔掉，而它看不出为什么。
    assert "evidence_quote" not in json.loads(result.content)[0]


# ---------------------------------------------------------------- 2 模型知道库存在


def test_manifest_tells_the_model_the_library_exists():
    """材料清单末尾报出库里的份数，**只报数字、不列文件名**。"""
    for index in range(3):
        library.ingest(_stored(f"{MATERIAL}\n\n## 附{index}\n\n补充条款 {index}。"))

    rendered = agent_tools._source_prompt_input([_stored()])

    assert "知识库中另有 3 份过往材料，同样可以用 rag_query 检索到。" in rendered
    # 库的稳态规模是 200 份，列出来就是又一份清单——清单的 token 上限正是为了
    # 防止这件事。模型对它们唯一的操作是 `rag_query`，名字没有可操作性。
    assert "附0" not in rendered
    assert "本次附带 1 份材料" in rendered


def test_manifest_omits_the_library_line_when_empty():
    """空库时一个字符都不加：这一行是"多出来的东西"，没有就不该出现。"""
    rendered = agent_tools._source_prompt_input([_stored()])

    assert "知识库" not in rendered
    assert "本次附带 1 份材料" in rendered


def test_registry_offers_rag_query_when_only_the_library_has_material():
    """库非空、请求无 sources 时也要注册 `rag_query`。

    否则 system 里那句"先调用 rag_query 检索"指向一个不存在的工具：模型只能
    凭标题编，并以 ok=True 交出一棵没有出处的树——它甚至不算说谎。
    """
    assert "rag_query" not in build_registry("create").names()

    library.ingest(_stored(filename="制度.md"))
    names = build_registry("create").names()

    assert "rag_query" in names
    # 撤掉分片生成的**唯一**理由是"正文整份已经在 prompt 里"（`full` + 有来源）。
    # 请求没带材料时那个前提不成立，所以它必须还在。
    assert "mindmap_generate_deep" in names
    # expand / edit 下没有生成工具，库照样给检索入口。
    assert "rag_query" in build_registry("expand").names()
    assert "rag_query" in build_registry("edit").names()


def test_intent_rule_switches_to_retrieval_when_the_library_has_material():
    """system 里"先检索"这条指令的条件跟着**可检索的东西**走，不跟着请求走。"""
    from app.agent.service import _intent_rule

    request = _request([], intent="create")
    assert "直接生成，不要先去检索资料" in _intent_rule(request)

    library.ingest(_stored(filename="制度.md"))
    assert "先调用 rag_query 检索" in _intent_rule(request)


# ---------------------------------------------------------------- 3 三种空结果


def test_three_empty_result_wordings_are_distinct():
    """过滤筛光 / 本次未命中 / 库里也没匹配：三句不同的话。

    模型据此决定"放宽过滤"、"换个问法"还是"告诉用户先传材料"。三句都写成
    "没有找到"时，它只能猜，而它看不到任何日志。
    """
    filtered = _exec_rag(_ctx([_stored()]), filters={"author": "查无此人"})
    missed = _exec_rag(_ctx([_stored("完全无关的一段文字。")]), question=MISS)

    library.ingest(_stored(filename="制度.md"))
    nowhere = _exec_rag(_ctx([]), question=MISS)

    assert filtered.ok and missed.ok and nowhere.ok
    texts = [filtered.content, missed.content, nowhere.content]
    assert len(set(texts)) == 3, texts
    assert "过滤条件筛掉了全部 1 份资料" in filtered.content
    assert missed.content == "没有在本次资料里找到相关片段。"
    assert "知识库里现有的 1 份材料都没有匹配到这个问题" in nowhere.content
    # 三种空结果都不该被当成"工具坏了"：ok=True 才让模型有下一步可走。
    assert all(item.data["hits"] == [] for item in (filtered, missed, nowhere))


def test_empty_library_and_no_sources_still_fails_with_sources_code():
    """库空 + 请求无材料：仍然是 `no_sources`，但文案要说清"库也是空的"。"""
    result = _exec_rag(_ctx([]), has_sources=True)

    assert not result.ok
    assert result.data["code"] == "no_sources"
    assert "知识库也是空的" in result.content


# ---------------------------------------------------------------- 4 开关关掉


def _load_baseline_tools(tmp_path: Path):
    """把 `BASELINE_REV` 的 `agent/tools.py` 载成独立模块，并核对 sha256。"""
    try:
        raw = subprocess.run(
            ["git", "show", f"{BASELINE_REV}:backend/app/agent/tools.py"],
            cwd=REPO_ROOT, capture_output=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"读不到 {BASELINE_REV} 的 tools.py：{exc}")

    assert hashlib.sha256(raw).hexdigest() == BASELINE_SHA256

    path = tmp_path / f"tools_{BASELINE_REV}.py"
    path.write_bytes(raw)
    spec = importlib.util.spec_from_file_location(f"tools_baseline_{BASELINE_REV}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_switch_off_matches_baseline_rag(tmp_path, monkeypatch):
    """`MINDMAP_LIBRARY_SEARCH=0` 时 `_rag` 与 `07a2740` 逐字节相同。

    三种场景都过一遍：有命中、无命中、请求里根本没有材料。比的是
    `(ok, content, data)`——`data` 的键集也要比，因为"逐字节"包含它。
    """
    baseline = _load_baseline_tools(tmp_path)
    monkeypatch.setenv(agent_tools.LIBRARY_SEARCH_ENV, "0")
    # 库里明明有东西：关掉开关之后必须一个字都看不到，否则这条闸是假的。
    library.ingest(_stored(filename="制度.md"))

    touched: list[str] = []

    def _forbid(name: str):
        def inner(*args: Any, **kwargs: Any):
            touched.append(name)
            raise AssertionError(f"开关关着时不该访问知识库：{name}")
        return inner

    for name in ("list_sources", "recent", "load", "touch"):
        monkeypatch.setattr(library, name, _forbid(name))

    source = _stored("会议决定在十月十七日发布二点零版本，请提前准备上线材料。")
    cases = (
        (QUERY, [source]),            # 有命中
        (MISS, [source]),             # 没命中
        (QUERY, []),                  # 请求不带材料
    )
    for question, sources in cases:
        current = asyncio.run(agent_tools._rag(
            agent_tools.RagArgs(question=question, top_k=3), _ctx(sources)))
        old = asyncio.run(baseline._rag(
            baseline.RagArgs(question=question, top_k=3), _ctx(sources)))
        assert (current.ok, current.content, current.data) \
            == (old.ok, old.content, old.data), (question, len(sources))

    # 注册表同理：关掉之后不因为"库里有东西"多给一个工具。
    assert build_registry("create").names() \
        == baseline.build_registry("create").names()
    assert touched == [], touched


# ---------------------------------------------------------------- 5 缓存上限


def test_library_source_cache_is_capped_and_keeps_request_sources_out():
    """缓存上限生效、丢最旧的；请求内的来源永远不进这个缓存。"""
    request_source = _stored("请求自带的材料。")
    ctx = _ctx([request_source])
    stolen = [_stored(f"库来源 {index}：" + "字" * 20) for index in range(MAX_LIBRARY_SOURCES + 5)]

    ctx.record_library_sources(stolen)

    assert len(ctx.library_sources) == MAX_LIBRARY_SOURCES
    kept = list(ctx.library_sources)
    assert kept[0] == stolen[5].id and kept[-1] == stolen[-1].id
    # 同一份再记一次不该顶掉顺序以外的东西（幂等），请求内来源也不该混进来。
    ctx.record_library_sources([stolen[-1], request_source])
    assert len(ctx.library_sources) == MAX_LIBRARY_SOURCES
    assert request_source.id not in ctx.library_sources


# ---------------------------------------------------------------- 6 端到端


class _ScriptedProvider:
    """按脚本返回工具调用；与 `tests/test_agent_loop.py` 的同名实现同义。"""

    def __init__(self, script: list[Completion]) -> None:
        self._script = list(script)
        self.calls = 0

    async def complete_tools(self, messages, tools, *, budget):
        self.calls += 1
        if not self._script:
            raise AssertionError("脚本已用完：循环比预期多调了一次模型")
        return self._script.pop(0)


def _completion(call: ToolCall | None = None) -> Completion:
    message: dict[str, Any] = {"role": "assistant", "content": "好的"}
    calls = [call] if call else []
    if call:
        message["tool_calls"] = [{
            "id": call.call_id, "type": "function",
            "function": {"name": call.name,
                         "arguments": json.dumps(call.arguments, ensure_ascii=False)},
        }]
    return Completion(message=message, tool_calls=calls,
                      finish_reason="stop" if call is None else "tool_calls",
                      usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})


def _drain(request: AgentRunRequest, provider) -> list:
    async def collect():
        return [event async for event in run(request, provider=provider)]

    return asyncio.run(collect())


def _ingest_docs(names: tuple[str, ...]) -> list[str]:
    """把仓库里真实的 md 入库，返回持久 id（按传入顺序）。"""
    docs = REPO_ROOT / "docs"
    ids: list[str] = []
    for name in names:
        path = docs / name
        assert path.is_file(), path
        ids.append(library.ingest(
            _stored(path.read_text(encoding="utf-8"), filename=name)))
    return ids


def test_end_to_end_library_only_run_cites_persistent_ids(monkeypatch):
    """核心产出：2–3 份真实材料入库 → 不带任何 sources 跑真 agent 循环。

    断言链：检索有命中 → 树有节点 → 节点的 `source_refs` 指向**库来源的持久 id**
    → 这些引用通过了逐字校验、且没有被 `remove_invalid` 剔除。

    最后一条是这次接线的成败判据：检索能出结果但引用在收尾被静默剔掉，等于白接。
    所以"引用还在"必须从最终树上读，不能只看诊断里没有异常。
    """
    monkeypatch.delenv(PARENT_RETRIEVAL_ENV, raising=False)   # 要两级坐标（叶子区间）
    material_ids = _ingest_docs(
        ("RAG-DECISIONS.md", "SOURCE-REFERENCES.md", "LIBRARY-MEASUREMENTS.md"))
    question = "父块模式下引用怎么校验"

    # `rag_query` 的真实结果要能被下一步的"模型"读到：包一层记录器，注册表在
    # build_registry 时读的是模块全局，所以 monkeypatch 生效。
    real_rag = agent_tools._rag
    captured: dict[str, Any] = {}

    async def recording_rag(args, ctx):
        result = await real_rag(args, ctx)
        captured["result"] = result
        return result

    async def fake_chat(system: str, user: str, **kwargs: Any) -> dict[str, Any]:
        """"模型"照抄检索结果里的 `evidence_*`：这就是提示词要求它做的动作。"""
        hits = captured["result"].data["hits"]
        hit = next(item for item in hits
                   if item["leaf_start"] is not None
                   and 0 < item["leaf_end"] - item["leaf_start"] <= 300)
        source = library.load(hit["source_id"])
        assert source is not None
        return _tree(_ref(source.id, hit["leaf_start"], hit["leaf_end"], source.text or ""))

    monkeypatch.setattr(agent_tools, "_rag", recording_rag)
    monkeypatch.setattr(agent_tools, "chat_async", fake_chat)

    provider = _ScriptedProvider([
        _completion(_call("rag_query", "c-rag", question=question)),
        _completion(_call("mindmap_generate", "c-gen", topic="整理知识库里的材料")),
        _completion(),
    ])
    # 请求里**一份材料都不带**：这次能查到的东西全部来自知识库。
    events = _drain(_request([], intent="create"), provider)

    ends = {event.name: event for event in events if event.type == "tool_end"}
    rag_end, gen_end = ends["rag_query"], ends["mindmap_generate"]
    done = next(event for event in events if event.type == "done")

    # 1) 检索有命中，且命中指向库来源的持久 id
    hits = rag_end.result_summary["hits"]
    assert hits, "库里三份真实材料，检索必须命中"
    assert all(hit["source_id"] in material_ids for hit in hits)
    assert rag_end.result_summary["library_sources"] >= 1

    # 2) 生成的树有节点，且树上的引用指向库来源的持久 id
    assert gen_end.result_summary["node_count"] > 1
    assert done.content is not None
    cited = [ref for child in done.content.root.children for ref in child.source_refs]
    assert cited, "模型抄了 evidence_*，引用不该在收尾被剔光"
    assert {ref.source_id for ref in cited} <= set(material_ids)

    # 3) 逐字校验真过了：引文就是持久来源原文的那一段（`library.load` 能回查）
    for ref in cited:
        source = library.load(ref.source_id)
        assert source is not None
        assert ref.start is not None and ref.end is not None
        assert (source.text or "")[ref.start:ref.end] == ref.quote

    # 4) 一条都没被 `remove_invalid` 剔除，且没有任何"来源不存在/引文不符"的诊断
    codes = {item["code"] for item in gen_end.result_summary["diagnostics"]}
    assert not codes & {"UNKNOWN_SOURCE_REF", "SOURCE_TEXT_UNAVAILABLE",
                        "SOURCE_QUOTE_MISMATCH", "SOURCE_REF_OUTSIDE_EVIDENCE"}, codes
    # 树上的引用条数与 `kept` 无关（`kept` 只出现在分片生成的 data 里），
    # 所以这里直接数树：有引用 = 没被剔光。
    assert len(cited) == 1
