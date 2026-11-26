"""「关掉联网检索」的作用域：不只管这一次发不发请求，还管能不能复用以前抓的。

`test_one_path.py::test_user_switch_off_skips_the_search_and_says_so` 钉住了前一半
（`_intake` 那道闸：一次外部 HTTP 都不发），它一直是绿的。漏掉的是另一半：外部命中
会 `library.ingest` 成**持久来源**（`_external_sources` 的三条理由之一），之后每一次
`rag_query` / `node_expand` 都会把它们当成普通库材料捞回来。于是关掉开关之后生成的
图里仍然列着 `wikipedia:` / `openalex:` 的依据，而 `content` 同时宣称"这张图完全
基于模型知识"——一边否认、一边列出出处。

那 22 条用例看不见这条路径有两个具体原因，两条都必须在这里补上：

  1. 它们全跑在**空库**上：`conftest` 把 `MINDMAP_DB_PATH` 指到 `tmp_path`，而
     `MINDMAP_LIBRARY_SEARCH` 是**清空**而不是置 0（见 conftest 那段注释），所以库
     检索是开着的——只是库里一份材料都没有。真实机器上那个库有十七份，其中大半是
     别的日子联网搜回来的。
  2. `test_one_path._run` 写死 `build_registry(..., has_library=False)`：注册表里
     根本没有 `rag_query`，模型无从查库。

所以这里每条用例都**先往库里放东西**，再按真实注册表（`has_library=True`）跑，最后
看用户真正拿到的那件产物（`ctx.candidate.source_usage`），而不是只看 `content` 的
话术。判据是"这次依据里有几条是外部检索抓的"，不是"有没有发请求"——后者在修复前
也一直是"没发"。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from app.agent import tools as tools_module
from app.agent.contracts import (
    AgentRunRequest,
    Completion,
    RunBudget,
    RunContext,
    ToolCall,
)
from app.agent.service import run as run_agent
from app.agent.tools import EXTERNAL_KNOWLEDGE_ENV, build_registry
from app.contracts.source import Source, SourceKind
from app.knowledge import external as external_knowledge_module
from app.knowledge import library, service as knowledge_service
from app.prefs import service as prefs

from test_one_path import BRANCHES, _tree_chat

TOPIC = "机器学习"

#: 外部检索抓回来的正文。三节分别对上 `_tree_chat` 的三条分支标题并不重要——
#: 这里只关心"这份来源进没进本次的依据集合"，不关心引用落没落到节点上。
WIKI_TEXT = (
    "# 机器学习\n\n"
    "## 监督学习\n\n"
    "监督学习使用带标签的数据训练模型，常见算法有线性回归、决策树与支持向量机。"
    "训练前要把数据划分为训练集与验证集，用交叉验证估计泛化误差。\n\n"
    "## 无监督学习\n\n"
    "无监督学习从无标签数据里找结构，聚类与降维是两类主要任务。"
    "K-means 需要预先给定簇数，层次聚类不用，但计算复杂度更高。\n\n"
    "## 评估方法\n\n"
    "分类任务看准确率、精确率、召回率与 F1，回归任务看均方误差与平均绝对误差。"
    "类别不平衡时准确率会骗人，要改用精确率与召回率。\n"
)

#: 用户自己上传的那份材料（`kind=document`，入库路径与外部检索无关）。
OWN_TEXT = (
    "# 机器学习项目排期\n\n"
    "机器学习项目的数据准备阶段排两周，特征工程三周，训练与调参四周。"
    "每周五同步一次进度，延期超过三天的要写清原因并调整后续排期。\n"
)


def _external_hit() -> external_knowledge_module.ExternalHit:
    return external_knowledge_module.ExternalHit(
        title=TOPIC, text=WIKI_TEXT, url="https://zh.wikipedia.org/wiki/机器学习",
        source_name="wikipedia", fetched_at="2026-11-17T03:58:28.565Z", language="zh",
    )


def _own_source() -> Source:
    return Source(id="own-1", kind=SourceKind.document, status="parsed",
                  filename="机器学习项目排期.docx", text=OWN_TEXT)


def _request() -> AgentRunRequest:
    return AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default", sources=[],
        budget=RunBudget(max_model_calls=20, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=1),
    )


def _ctx() -> RunContext:
    return RunContext(request=_request(), sources_by_id={})


def _store_external_source() -> str:
    """把一条"昨天联网搜到的"来源放进库（与 `_external_sources` 入库同一个入口）。"""
    return library.ingest(tools_module._external_source(_external_hit()))


def _generate(monkeypatch, *, with_external_in_library: bool,
              with_own_in_library: bool = False) -> tuple[RunContext, object, list[str]]:
    """走一遍真实检索链：`_rag` → `mindmap_generate`，返回候选树。

    **第一步不再经过注册表**（`build_registry("create")` 一次调用 `rag_query`）：
    `create` 现在拿不到这个工具——理由见 `docs/LATENCY-BREAKDOWN.md` 与
    `test_agent_guards.test_create_gets_no_rag_query_even_when_the_library_is_not_empty`
    （外层查到的片段进不了生成工具的参数，实测白烧 6.8 秒）。这里要验的是
    **检索侧的作用域**（`drop_external` 让不让人复用昨天抓的来源），所以直接调
    `rag_query` 的实现体 `_rag`，判据与产物一个字节都不变。

    `search_external` 一律替换成"返回空"：这条链要验的不是"搜到了什么"，而是
    "库里已有的东西会不会自己冒出来"。真打网络会让用例依赖宿主机代理。
    """
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "1")
    if with_external_in_library:
        _store_external_source()
    if with_own_in_library:
        library.ingest(_own_source())

    requests: list[str] = []
    monkeypatch.setattr(
        external_knowledge_module, "search_external",
        lambda *a, **k: requests.append("called") or [])

    ctx = _ctx()
    registry = build_registry("create", has_sources=False, has_library=True)
    chat = _tree_chat()
    with patch("app.agent.tools.chat_async", new=chat):
        asyncio.run(tools_module._rag(tools_module.RagArgs(question=TOPIC), ctx))
        result = asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate",
                     arguments={"topic": TOPIC, "branches": 3}), ctx))
    return ctx, result, requests


def _usage(ctx: RunContext) -> list[tuple[SourceKind, str]]:
    assert ctx.candidate is not None, "这次生成没有产出候选树"
    return [(item.kind, item.filename or "") for item in ctx.candidate.source_usage]


def test_user_switch_off_keeps_yesterdays_external_sources_out(monkeypatch):
    """用户在界面上关掉之后：昨天联网搜到的维基条目不能成为这次生成的依据。

    这是那个被用户实测到的 bug 的回归测试。修复前这条断言拿到的是
    `[(SourceKind.web, 'wikipedia: 机器学习')]`——一次外部请求都没发，但产物里
    列着外部出处，而 `content` 说的是"完全基于模型知识"。
    """
    prefs.set(prefs.EXTERNAL_SEARCH_KEY, "false")

    ctx, result, requests = _generate(monkeypatch, with_external_in_library=True)

    assert result.ok, result.content
    assert requests == [], "开关关着时不该发外部请求"
    assert _usage(ctx) == [], "关掉之后外部检索的旧产物不能进本次依据"
    assert ctx.library_sources == {}, "旧外部来源不该进引用校验的可查集合"
    assert "你关掉了联网检索" in result.content


def test_user_switch_on_still_reuses_yesterdays_external_sources(monkeypatch):
    """开着时同一条库来源**必须**还能被复用——否则这个修复就是把功能删了。

    与上一条成对：两条用例的差别只有一个偏好值，输出必须相反。只写"关掉之后没有了"
    的测试挡不住"把库检索整个关掉"那种更粗暴的改法。
    """
    prefs.set(prefs.EXTERNAL_SEARCH_KEY, "true")

    ctx, result, _ = _generate(monkeypatch, with_external_in_library=True)

    assert result.ok, result.content
    assert (SourceKind.web, "wikipedia: 机器学习") in _usage(ctx)


def test_user_switch_off_keeps_the_users_own_materials_usable(monkeypatch):
    """关掉的是"外部检索"，不是"知识库"：用户自己传过的材料照常能查。

    这一条防的是另一类过修——把 `library_search_enabled()` 一起关掉，或者把整个库
    候选池清空。那种改法能让上一条测试变绿，代价是用户上传的材料也一起失效。
    """
    prefs.set(prefs.EXTERNAL_SEARCH_KEY, "false")

    ctx, result, _ = _generate(monkeypatch, with_external_in_library=True,
                               with_own_in_library=True)

    assert result.ok, result.content
    usage = _usage(ctx)
    kinds = {kind for kind, _ in usage}
    assert SourceKind.web not in kinds and SourceKind.paper not in kinds
    assert (SourceKind.document, "机器学习项目排期.docx") in usage


def test_drop_external_is_a_retrieval_side_switch_not_a_filter(monkeypatch):
    """`search_parent(drop_external=True)` 在**候选池**那一层就剔掉外部来源。

    上一条端到端用例能过，也可能是因为收尾时被 `_verify_sources` 挡掉了——那时
    模型其实已经读到了维基正文，用户看到的图会"内容来自维基、出处却写着没有"。
    所以这里直接在检索层断言：两个参数的输出必须不同，而差别只是那一条外部来源。
    """
    library.ingest(tools_module._external_source(_external_hit()))

    kept = knowledge_service.search_parent([], TOPIC, 3, library=True)
    dropped = knowledge_service.search_parent([], TOPIC, 3, library=True,
                                              drop_external=True)

    assert kept, "库里有东西时开着开关必须命中（否则这条用例证明不了什么）"
    assert dropped == [], "drop_external=True 时外部来源不该出现在候选池里"


def test_closed_switch_leaves_no_rag_query_tool(monkeypatch):
    """关掉联网之后，如果库里**只剩**外部检索入库的那批，`rag_query` 不该被注册。

    这一条钉的是一个真实故障：关掉开关 + 不带材料时，库被剔空，但工具还在清单里。
    模型照着调、拿到 `no_sources`、再试一次、撞上"累计失败 2 次"的闸——整轮运行
    停掉，用户看到的是一次"生成失败"，而真相是那个工具本就不该出现。

    判据仍是**结果**而不是动作：不查"rag_query 有没有被调用"，直接看注册表里
    有没有它。前者在工具存在但模型没调时也会绿。
    """
    monkeypatch.setenv(tools_module.LIBRARY_SEARCH_ENV, "1")
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "1")

    # 库里只有一条外部检索抓回来的来源——这正是关掉联网之后剩下的全部
    _store_external_source()
    assert tools_module.library_source_count() == 1
    assert tools_module.library_source_count(drop_external=True) == 0

    prefs.set(prefs.EXTERNAL_SEARCH_KEY, "true")
    assert tools_module.library_available() is True, "开着时库里有东西，工具该在"

    prefs.set(prefs.EXTERNAL_SEARCH_KEY, "false")
    assert tools_module.library_available() is False, \
        "关掉之后库里只剩外部来源，剔空即视为没有库——否则模型会调一个必然失败的工具"

    names = build_registry("create", has_sources=False).names()
    assert "rag_query" not in names
    # `create` 恒不给（生成工具内部自己检索）；`expand` 才跟着库走——而关掉联网之后
    # 库里只剩外部来源，剔空即视为没有库，它也不该拿到一个必然失败的工具。
    assert "rag_query" not in build_registry("expand", has_sources=False).names()


# ------------------------------------------------- 端到端：那一轮生成真的跑完
#
# 上面那条只钉住"注册表里没有它"。注册表对了，链路仍可能挂着（比如 system 里
# 还在叫模型去检索，或者失败闸的语义变了）——所以这一条跑一整轮真实的
# `service.run`：真注册表、真工具、真失败计数，只有模型与被替换掉的那次外网
# 往返是替身。


def _tool_turn(call: ToolCall) -> Completion:
    """一轮"模型要调这个工具"。消息体照 OpenAI 的形状写：循环会原样回填。"""
    return Completion(
        message={"role": "assistant", "content": "", "tool_calls": [{
            "id": call.call_id, "type": "function",
            "function": {"name": call.name,
                         "arguments": json.dumps(call.arguments, ensure_ascii=False)},
        }]},
        tool_calls=[call],
        finish_reason="tool_calls",
    )


class _ToolListDrivenModel:
    """脚本化模型：**工具清单里有什么，它就用什么**。

    这一点是整条证据链的关键。写死"先调 rag_query"的脚本在修复之后照样会挂
    （它去调一个不在清单里的工具，那是 `unknown_tool`，同样计入失败），于是这条
    用例永远证明不了修复有效——它证明的只是"模型会调坏工具"。真实模型的输入就是
    那份清单：清单里有它就查（`_RAG_INTENT_RULES` 还明写着"先调用 rag_query
    检索"），清单里没有就只剩生成这一条路。

    两次检索的**问句不同**：线上模型看到 `no_sources` 之后会换个说法再试一次
    （`execute` 那段注释说的"下一轮它通常能自己改对"），而只读工具的同参重复调用
    会先被判成 `duplicate_call`、那一条**不计入**失败数——同一个问句重复两次在
    这里证明不了任何关于失败闸的事。
    """

    def __init__(self) -> None:
        self.tools_per_turn: list[set[str]] = []
        self.system_per_turn: list[str] = []
        self.searches = 0
        self.generated = False

    async def complete_tools(self, messages, tools, *, budget) -> Completion:
        names = {item["function"]["name"] for item in tools}
        self.tools_per_turn.append(names)
        self.system_per_turn.append(str(messages[0].get("content") or ""))
        if "rag_query" in names and self.searches < 2:
            self.searches += 1
            question = TOPIC if self.searches == 1 else f"{TOPIC} 的定义与分类"
            return _tool_turn(ToolCall(call_id=f"rag-{self.searches}", name="rag_query",
                                       arguments={"question": question}))
        if not self.generated:
            self.generated = True
            return _tool_turn(ToolCall(call_id="gen-1", name="mindmap_generate",
                                       arguments={"topic": TOPIC, "branches": 3}))
        return Completion(message={"role": "assistant", "content": "导图已经生成。"},
                          finish_reason="stop")


def _run_whole_turn(monkeypatch, model: _ToolListDrivenModel) -> list:
    """跑一整轮生成，返回事件列表。外部检索与工具内的模型调用都是替身。"""
    monkeypatch.setattr(tools_module.external_knowledge, "search_external",
                        lambda *a, **k: [])

    async def drain():
        return [event async for event in run_agent(_request(), provider=model)]

    with patch("app.agent.tools.chat_async", new=_tree_chat()):
        return asyncio.run(drain())


def test_closed_switch_generates_instead_of_dying_on_a_dead_tool(monkeypatch):
    """用户实测的那个故障，端到端跑一遍：关掉联网 + 不带材料 → 生成正常完成。

    「关掉联网检索」+「库只剩外部来源」这个组合，四件事必须同时成立才算修好：

      1. 模型收到的**清单**里没有 `rag_query`（注册表的产物，也是模型的输入）；
      2. system 里不再叫它"先调用 rag_query 检索"（`_intent_rule` 与注册表问的是
         同一个 `library_available()`，只改一处就只剩半条链）；
      3. 模型没有收到过 `no_sources`——它一次都没被"无处可查"打断；
      4. 没有 `too_many_failures`，而且 `done` 带着一棵树。用户在界面上看到的
         是图，不是"生成失败"。

    修复前这条会红在 1 与 2：清单里多出 `rag_query`，system 里多出那句"先调用
    rag_query 检索"，模型照着做两次、撞上 `max_repairs=1`，事件流以
    `error(too_many_failures)` 收尾、没有 `done`。
    """
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "1")
    prefs.set(prefs.EXTERNAL_SEARCH_KEY, "false")
    _store_external_source()          # 库里只剩这一条外部检索抓回来的

    model = _ToolListDrivenModel()
    events = _run_whole_turn(monkeypatch, model)

    assert model.tools_per_turn[0] == {"mindmap_generate"}, \
        f"模型这一轮看到的工具清单不对：{sorted(model.tools_per_turn[0])}"
    assert "先调用 rag_query" not in model.system_per_turn[0], \
        "system 里还在叫模型去检索，而清单里没有那个工具"

    codes = [event.result_summary.get("code")
             for event in events if event.type == "tool_end"]
    assert "no_sources" not in codes, f"模型还是收到了 no_sources：{codes}"
    assert [event.code for event in events if event.type == "error"] == []
    assert model.searches == 0, "模型不该有一次检索尝试——清单里没有这个工具"

    assert events[-1].type == "done"
    content = events[-1].content
    assert content is not None, "这一轮没有产出树"
    # 不去断言根 label：那是树替身自己写的字面量（`test_one_path.TOPIC`），
    # 与本文件的主题无关。要看的是"树真的长出来了"——三条一级分支都在。
    assert [child.label for child in content.root.children] == list(BRANCHES)
