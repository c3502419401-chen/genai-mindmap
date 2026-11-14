"""`rag` 形态下 `mindmap_generate` 自己检索一次：命中进 user 消息，证据进 `ctx`。

为什么单独一个文件：这条链是**跨模块**的接线，任何一段断掉都不会报错——

    tools._retrieve_topic → knowledge.search_parent → tools._citable_hits
                          → RunContext.record_evidence
                          → validate_generated_content(evidence=...)

断在哪一段，用户看到的都是同一件事：一条分支、内容"材料中没有相关内容：未检索到
任何原文依据"，然后被 `MIN_BRANCHES=3` 判成 `invalid_map`。真实用户第一次跑 `rag`
形态就撞上了它（`tool_calls_used: 5`：模型其实先调了 `rag_query`，但那些命中进不了
`mindmap_generate` 的输入——它的入参只有 `topic`）。所以这里每一段都单独钉一次，
最后再钉一遍端到端。

`full` 形态一个字都没改（正文本来就在 prompt 里），`full` 那两条用例拿 `00a0d12`
的模块逐字节对比钉住这一点，并证明那条路上连一次检索都不发生。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from app.agent.contracts import (
    AgentRunRequest,
    Completion,
    RunBudget,
    RunContext,
    ToolCall,
)
from app.agent.service import run
from app.agent.tools import (
    GENERATE_EVIDENCE_HEADER,
    GENERATE_EVIDENCE_RULES,
    GENERATE_RETRIEVAL_TOP_K,
    _source_prompt_input,
    build_registry,
)
from app.contracts.source import Source, SourceKind
from app.generation.service import (
    DETAIL_NO_SOURCE_RULES,
    DETAIL_PROMPT,
    RAG_SOURCE_PROMPT,
    RAG_THINKING_GUARD,
    SOURCE_MODE_ENV,
    SOURCE_MODE_FULL,
    SOURCE_MODE_RAG,
    thinking_block,
)
from app.knowledge.service import PARENT_RETRIEVAL_ENV, search_parent

# 一份结构清楚的制度材料：三个章节，各有各的关键词。
MATERIAL = (
    "第一章 总则\n\n本制度适用于全部预算事项，预算审批由部门负责人签核，"
    "超过额度的部分必须上报总经理复核。\n\n"
    "第二章 报销时限\n\n费用报销的时限是三十个自然日，逾期不再受理，"
    "票据缺失的申请一律退回并通知经办人补齐材料。\n\n"
    "第三章 采购合规\n\n采购合同必须经过法务审核，金额超过五十万元另行招标。\n"
)

# 与正文共享词项（报销/时限/预算/审批），所以主题检索必有命中。
TOPIC = "报销时限与预算审批"

# 只在原文里出现的句子：它出现在 prompt 里，就说明正文真的进去了。
PASSAGE = "费用报销的时限是三十个自然日"

# 与材料**零**共同词项的问法：这条路上检索必回空（用来钉"检索为空"那条分支）。
UNRELATED_TOPIC = "预算审批流程"
UNRELATED_TEXT = "甲乙丙丁戊己庚辛壬癸。"

REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_COMMIT = "00a0d12"
LEGACY_PATH = "backend/app/agent/tools.py"


def _env(mode: str):
    """把形态显式切到 `mode`（退出时还原）。"""
    return patch.dict(os.environ, {SOURCE_MODE_ENV: mode})


def _material(text: str = MATERIAL, *, source_id: str = "doc-1") -> Source:
    return Source(id=source_id, kind=SourceKind.document, status="parsed",
                  filename="制度汇编.md", text=text)


def _ctx(sources: list[Source], *, intent: str = "create") -> RunContext:
    request = AgentRunRequest(
        goal="整理材料", intent=intent, sources=sources,
        budget=RunBudget(max_model_calls=10, max_tool_calls=8,
                         max_output_tokens=32768),
    )
    return RunContext(request=request, candidate=None,
                      sources_by_id={source.id: source for source in sources})


def _tree(refs: list[dict[str, Any]] | None = None, *, branches: int = 3) -> dict[str, Any]:
    """一张合法的整树：一级分支 3 条（`MIN_BRANCHES=3`），每条一个要点。"""
    children = [
        {"id": f"b{index}", "label": f"分支{index}", "children": [
            {"id": f"b{index}-1", "label": f"要点{index}", "children": []},
        ]}
        for index in range(branches)
    ]
    children[0]["source_refs"] = list(refs or [])
    return {"title": "制度要点", "root": {"id": "root", "label": "制度要点",
                                          "children": children}}


def _run_generate(sources: list[Source], topic: str, tree: dict[str, Any], *,
                  intent: str = "create") -> tuple[Any, tuple[str, str], RunContext]:
    """跑一次 `mindmap_generate`，把 (结果, 模型收到的 (system, user), ctx) 拿回来。"""
    ctx = _ctx(sources, intent=intent)
    mocked = AsyncMock(return_value=tree)
    with patch("app.agent.tools.chat_async", new=mocked):
        result = asyncio.run(build_registry(intent, has_sources=bool(sources)).execute(
            ToolCall(call_id="c-gen", name="mindmap_generate",
                     arguments={"topic": topic}), ctx))
    system, user = mocked.await_args.args
    return result, (system, user), ctx


def _split(user: str) -> tuple[str, str]:
    """(清单部分, 命中部分)：按 `GENERATE_EVIDENCE_HEADER` 切开。"""
    manifest, _, evidence = user.partition(GENERATE_EVIDENCE_HEADER)
    return manifest, evidence


def _evidence_payload(user: str) -> list[dict[str, Any]]:
    """把 user 消息里那段 evidence 数组解析回来——它就是模型看到的东西。"""
    _manifest, evidence = _split(user)
    assert evidence, "这次没有命中，prompt 里没有 evidence 数组"
    return json.loads(evidence.strip())


# ---------------------------------------------------------------- 一、核心回归


def test_rag_generation_hands_the_retrieved_text_to_the_model(monkeypatch):
    """核心回归：`rag` 形态下清单之外必须带上原文片段，且不再"只有清单"。

    这就是真实用户撞到的那个阻断：`_source_prompt_input` 在 `rag` 下只给标题/
    字数/类型，工具自己又不检索，模型手里一个字都没有；而当时的 `RAG_SOURCE_PROMPT`
    要求"检索不到就不要写"，它照做，于是只有一条"材料中没有相关内容"的分支。
    （那一版提示词已经换掉了：现在检索不到可以按知识展开、只是不挂引用，见
    `tests/test_citation_contract.py`。这一次预检索仍然必须存在——没有原文，
    "标明出处"就无从谈起。）
    断言的落点因此是**正文片段出现在模型看得到的那段 user 消息里**。
    """
    monkeypatch.delenv(PARENT_RETRIEVAL_ENV, raising=False)
    source = _material()

    with _env(SOURCE_MODE_RAG):
        result, (system, user), _ctx_after = _run_generate([source], TOPIC, _tree())

    assert result.ok, result.content
    manifest, evidence = _split(user)
    # 1) 清单还在：模型仍要知道手上有哪几份材料
    assert "本次附带 1 份材料" in manifest
    assert "制度汇编" in manifest
    # 2) 清单里没有正文，正文只在命中段里
    assert PASSAGE not in manifest
    assert PASSAGE in evidence
    # 3) "不再只有清单"：整段 user 与纯清单渲染结果必须不同
    assert user != _source_prompt_input([source], system=system)
    # 4) 命中形状与 `rag_query` 的返回同形：一串 JSON 对象，字段可抄
    payload = _evidence_payload(user)
    assert payload and {"source_id", "quote", "start", "end"} <= set(payload[0])
    assert payload[0]["source_id"] == source.id
    # 5) 提示词这条路上仍然必须是 `rag` 那一份（不是 `full` 的 sources 数组），
    #    末尾只多一句"这次不用再检索"（见 `GENERATE_EVIDENCE_RULES` 的注释）。
    assert system == (RAG_SOURCE_PROMPT + thinking_block("default")
                      + RAG_THINKING_GUARD + GENERATE_EVIDENCE_RULES)
    assert "sources 数组" not in system


def test_rag_generation_records_evidence_so_citations_can_be_verified(monkeypatch):
    """同一次生成之后 `ctx.evidence` 非空，收尾的引用校验按它判——引用留在树上。

    这是"命中进 prompt"之外的另一半：模型照着 `evidence_*` 抄的引用，必须在
    `verify_source_refs` 那里查得到判据。只喂 prompt、不累积证据的话，引用会被
    判成"落在父块里与查询无关的部分"（或因为查不到证据而根本没人判），最后
    静默剔光——检索成功、引用写出、收尾消失，这是整条链上最隐蔽的一种失败。
    """
    monkeypatch.delenv(PARENT_RETRIEVAL_ENV, raising=False)
    source = _material()
    # 判据自己算一遍：工具那一次检索走的是同一个函数（纯 BM25，无网络）。
    hits = search_parent([source], TOPIC, GENERATE_RETRIEVAL_TOP_K)
    assert hits and hits[0].leaf_start is not None
    leaf_quote = (source.text or "")[hits[0].leaf_start:hits[0].leaf_end]
    ref = {"source_id": source.id, "start": hits[0].leaf_start,
           "end": hits[0].leaf_end, "quote": leaf_quote}

    with _env(SOURCE_MODE_RAG):
        result, (system, user), ctx = _run_generate([source], TOPIC, _tree([ref]))
        # 前提：这条引用确实是从 prompt 里那段命中抄下来的
        payload = _evidence_payload(user)
        assert any(item.get("evidence_quote") == leaf_quote for item in payload)
        assert system.startswith(RAG_SOURCE_PROMPT)

        assert result.ok, result.content
        assert ctx.evidence, "生成之后 `ctx.evidence` 必须是空的补集：收尾校验要它"
        assert ctx.candidate is not None
        kept = ctx.candidate.root.children[0].source_refs
        assert [item.quote for item in kept] == [leaf_quote]
        codes = {item["code"] for item in result.data["diagnostics"]}
        assert not codes & {"SOURCE_REF_OUTSIDE_EVIDENCE", "SOURCE_QUOTE_MISMATCH",
                            "SOURCE_TEXT_UNAVAILABLE", "UNKNOWN_SOURCE_REF"}, codes


# ---------------------------------------------------------------- 二、检索为空


def test_empty_retrieval_falls_back_to_topic_only_generation(monkeypatch):
    """检索回空 → 退回 `DETAIL_PROMPT`（无来源）生成，并明说"只基于主题"。

    选的是 (b) 而不是 (a)。(a) 是"仍然按 rag 提示词生成"：模型会说"没检索到"，
    一条分支、`invalid_map`——就是这次的 bug，用户拿不到图。(b) 至少给一张能用的
    图，而且"没有引用"这件事在 `content`、`data.diagnostics`、`data.retrieval`
    三处都可见，不会假装有依据。

    判据也一并钉住：`validate_generated_content` 对"有 sources 但零引用"**不是**
    硬失败——它给的是逐节点 `UNSOURCED_NODE`（warn），树本身照常通过。所以 (b)
    不需要额外绕开契约校验。
    """
    source = _material(UNRELATED_TEXT)
    with _env(SOURCE_MODE_RAG):
        result, (system, user), ctx = _run_generate([source], UNRELATED_TOPIC, _tree())

    assert result.ok, result.content
    # 1) 提示词与输入都退回"无来源"形态：一个字的材料清单都不给。
    #    末尾那句 `DETAIL_NO_SOURCE_RULES` 是这一轮加的：没有材料可标出处时，
    #    提示词要明说"不要写引用"，否则模型会把 `CONTENT_FREEDOM_CONTRACT` 的
    #    前半句（内容可以自由展开）执行到底，连引用一起编。
    assert system == DETAIL_PROMPT + thinking_block("default") + DETAIL_NO_SOURCE_RULES
    assert user == UNRELATED_TOPIC
    # 2) 一句话说清这次没有依据（用户看得到的地方）
    assert "只基于主题" in result.content
    assert "不含任何材料依据" in result.content
    # 3) 诊断里只有一条聚合的（不是逐节点几十条 UNSOURCED_NODE）
    codes = [item["code"] for item in result.data["diagnostics"]]
    assert "GENERATION_WITHOUT_EVIDENCE" in codes
    assert "UNSOURCED_NODE" not in codes
    # 4) 检索耗时与命中数照样报出来（0 条也是信息：说明问法没对上材料）
    assert result.data["retrieval"]["hits"] == 0
    assert result.data["retrieval"]["top_k"] == GENERATE_RETRIEVAL_TOP_K
    assert result.data["retrieval"]["failed"] is None
    # 5) 树是能用的：3 条一级分支，只是没有引用
    assert len(ctx.candidate.root.children) == 3
    assert all(not child.source_refs for child in ctx.candidate.root.children)


def test_retrieval_failure_falls_back_but_says_it_failed(monkeypatch):
    """检索**出错**与检索**为空**要给两句不同的话：一个是"这次没查成"，一个是"材料里没有"。

    把前者说成后者是谎报（用户会去改问法，而真正的原因是服务不可用），反过来
    则让人白等一次重试。
    """
    def boom(*_args, **_kwargs):
        raise RuntimeError("embedding 服务超时")

    monkeypatch.setattr("app.agent.tools.search_parent", boom)
    source = _material()
    with _env(SOURCE_MODE_RAG):
        result, (system, _user), _ctx_after = _run_generate([source], TOPIC, _tree())

    assert result.ok, result.content
    assert system == DETAIL_PROMPT + thinking_block("default") + DETAIL_NO_SOURCE_RULES
    assert "检索出错" in result.content
    assert "RuntimeError" in result.data["retrieval"]["failed"]
    assert result.data["retrieval"]["hits"] == 0


# ---------------------------------------------------------------- 三、full 不动


def _legacy_tools():
    """载入 `00a0d12` 的 `tools.py`（改动前的基线），用来做逐字节对比。

    基准写死提交号、不取 HEAD：拿 HEAD 当基准等于"今天的我再确认一次昨天的我"，
    一次误改会被下一次提交顺手合法化。
    """
    shown = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{LEGACY_COMMIT}:{LEGACY_PATH}"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if shown.returncode != 0 or not shown.stdout:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "legacy_tools.py"
        path.write_text(shown.stdout, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("legacy_tools_00a0d12", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module      # pydantic / dataclass 在 exec 期间要能找到它
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
    return module


def _legacy_prompt(module, sources: list[Source], topic: str) -> tuple[str, str]:
    """用旧模块的 `_generate` 跑一次，拿它发给模型的 (system, user)。"""
    ctx = _ctx(sources)
    mocked = AsyncMock(return_value=_tree())
    original = module.chat_async
    module.chat_async = mocked
    try:
        asyncio.run(module._generate(module.GenerateArgs(topic=topic), ctx))
    finally:
        module.chat_async = original
    system, user = mocked.await_args.args
    return system, user


def test_full_mode_prompts_are_byte_identical_to_the_baseline(monkeypatch):
    """`full` 形态一个字都不改：正文本来就在 prompt 里，多检索一次是白花。

    对比的是 `00a0d12`（本次改动之前）那一版模块的 `_generate`：同一个 ctx、同一份
    材料、同一段思维模型，`(system, user)` 必须逐字节相等。仓库里没有 git 或那个
    提交时跳过——那属于环境缺失，不是行为差异。
    """
    legacy = _legacy_tools()
    if legacy is None:
        raise unittest.SkipTest(
            f"取不到 {LEGACY_COMMIT}:{LEGACY_PATH}（没有 git 或没有这个提交）")

    fixtures = {
        "single": [_material()],
        "two": [_material(source_id="doc-1"),
                _material("另一份材料的正文。", source_id="doc-2")],
        "thinking": [_material()],
    }
    for name, sources in fixtures.items():
        with _env(SOURCE_MODE_FULL):
            _result, (system, user), _ctx_after = _run_generate(sources, TOPIC, _tree())
            # 旧模块也必须在同一个形态下跑：形态是环境变量，不是调用参数。
            expected = _legacy_prompt(legacy, sources, TOPIC)
        assert (system, user) == expected, f"{name}: full 形态与 {LEGACY_COMMIT} 不再逐字节一致"


def test_full_mode_never_runs_the_extra_retrieval(monkeypatch):
    """`full` 形态**一次检索都不发**：多这一次往返对它是纯浪费（正文已在 prompt 里）。"""
    calls: list[Any] = []

    def spy(*args, **kwargs):
        calls.append(args)
        return []

    monkeypatch.setattr("app.agent.tools.search_parent", spy)
    with _env(SOURCE_MODE_FULL):
        result, _prompt, _ctx_after = _run_generate([_material()], TOPIC, _tree())

    assert result.ok, result.content
    assert calls == []
    assert "retrieval" not in result.data      # 连观测字段都不该出现


# ---------------------------------------------------------------- 四、可观测性


def test_retrieval_seconds_are_logged(monkeypatch, caplog):
    """额外那次检索的耗时必须单独记一行：用户要能看出"生成慢在哪"。

    它夹在"点了生成"和"第一段预览"之间，混在总耗时里永远分辨不出来——重排
    0.6–0.9 秒 + 一次 embedding 往返，是这个功能唯一新增的等待。
    """
    monkeypatch.delenv(PARENT_RETRIEVAL_ENV, raising=False)
    with _env(SOURCE_MODE_RAG):
        with caplog.at_level(logging.INFO, logger="app.agent.tools"):
            result, _prompt, _ctx_after = _run_generate([_material()], TOPIC, _tree())

    assert result.ok, result.content
    assert "主题预检索" in caplog.text
    assert "耗时" in caplog.text
    assert f"top_k={GENERATE_RETRIEVAL_TOP_K}" in caplog.text
    assert result.data["retrieval"]["seconds"] >= 0.0
    assert result.data["retrieval"]["hits"] >= 1


def test_empty_retrieval_is_logged_too(monkeypatch, caplog):
    """回空也要记一行：不然"这次为什么没有引用"在日志里完全没有痕迹。"""
    monkeypatch.delenv(PARENT_RETRIEVAL_ENV, raising=False)
    with _env(SOURCE_MODE_RAG):
        with caplog.at_level(logging.INFO, logger="app.agent.tools"):
            _run_generate([_material(UNRELATED_TEXT)], UNRELATED_TOPIC, _tree())

    assert "没有命中" in caplog.text
    assert "耗时" in caplog.text


# ---------------------------------------------------------------- 五、端到端


class _ScriptedProvider:
    """按脚本返回工具调用；与 `tests/test_evidence_wiring.py` 的同名实现同义。"""

    def __init__(self, script: list[Completion]) -> None:
        self._script = list(script)
        self.systems: list[str] = []

    async def complete_tools(self, messages, tools, *, budget):
        if not self._script:
            raise AssertionError("脚本已用完：循环比预期多调了一次模型")
        self.systems.append(messages[0]["content"])
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


def test_end_to_end_rag_mode_produces_a_sourced_map(monkeypatch):
    """端到端：`rag` 形态下 `mindmap_generate` 产出 ≥3 条分支，节点带 `source_refs`。

    这才是用户要的结果——真实用户那次拿到的是一张一条分支、内容是"没检索到"的图，
    被 `validate_generated_content` 判 `invalid_map`，整次运行失败。

    这里的"模型"严格照 prompt 里那段 evidence 抄引用（`evidence_start/end/quote`
    三个字段从 JSON 里现取），所以这条用例同时证明了：**模型从提示词里就拿得到写
    引用所需的一切**，不需要再猜偏移、也不需要再检索一次。
    """
    monkeypatch.delenv(PARENT_RETRIEVAL_ENV, raising=False)
    source = _material()
    prompts: list[tuple[str, str]] = []

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None):
        prompts.append((system, user))
        item = next(entry for entry in _evidence_payload(user)
                    if entry.get("evidence_start") is not None)
        ref = {"source_id": item["source_id"], "start": item["evidence_start"],
               "end": item["evidence_end"], "quote": item["evidence_quote"]}
        tree = _tree([ref])
        if on_delta is not None:          # `run()` 这条入口会带流式预览回调
            await on_delta(json.dumps(tree, ensure_ascii=False))
        return tree

    provider = _ScriptedProvider([
        _completion(ToolCall(call_id="c-gen", name="mindmap_generate",
                             arguments={"topic": TOPIC})),
        _completion(),
    ])

    async def drain():
        return [event async for event in run(_ctx([source]).request, provider=provider)]

    with _env(SOURCE_MODE_RAG), patch("app.agent.tools.chat_async", new=chat):
        events = asyncio.run(drain())

    done = [event for event in events if event.type == "done"]
    assert done and done[-1].content is not None, [event.type for event in events]
    content = done[-1].content
    assert len(content.root.children) >= 3, "一级分支不足 3 条 = 就是用户撞到的那个失败"
    refs = content.root.children[0].source_refs
    assert refs, "节点应当带着从命中抄来的引用"
    # 引用确实指向本次检索命中的那一小段（`evidence_*`），不是随手抄的一段原文
    hits = search_parent([source], TOPIC, GENERATE_RETRIEVAL_TOP_K)
    assert hits and hits[0].leaf_start is not None
    assert (refs[0].start, refs[0].end) == (hits[0].leaf_start, hits[0].leaf_end)
    assert refs[0].quote == (source.text or "")[hits[0].leaf_start:hits[0].leaf_end]
    gen_end = next(event for event in events
                   if event.type == "tool_end" and event.name == "mindmap_generate")
    assert gen_end.ok
    assert gen_end.result_summary["retrieval"]["hits"] >= 1
    codes = {item["code"] for item in gen_end.result_summary["diagnostics"]}
    assert not codes & {"SOURCE_QUOTE_MISMATCH", "SOURCE_REF_OUTSIDE_EVIDENCE"}, codes
