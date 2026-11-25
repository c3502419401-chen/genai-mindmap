"""一级分支的概括（`MINDMAP_BRANCH_DIGEST`，默认关）：一次批量调用 + 一律降级。

为什么单独一个文件：它接在说明之后，是**唯一一处为了"一句话"多调一次模型**的地方。
它的失败面有六种，而每一种在产出上都长得像"成功了一个短一点的版本"：

  1. 开关关着（→ 一个字节都不多：`content` 没有 `branch_digests`、`data` 没有
     `branch_digest`、模型调用次数不变）；
  2. 模型少写了几支（→ 缺的保持空，记 `BRANCH_DIGEST_INCOMPLETE`，**不替它编**）；
  3. 模型给了对不上号的键（→ 计数，不写进去）；
  4. 整次调用失败（→ 树照常交付，记 `BRANCH_DIGEST_SKIPPED`）；
  5. 额度用完（→ 不发抖，skipped="budget"）；
  6. 一条分支都写不回来（→ skipped="error"，诊断说清）。

另一半是**"按 id 对号"不能退化成"按顺序对号"**：顺序错一位，六条概括会整体串行
错位，而每条本身都通顺——从产出上完全看不出来。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    BRANCH_DIGEST_MAX_CHARS,
    BRANCH_DIGEST_PROMPT,
    DETAIL_ONLY_PROMPT,
    PLAN_PROMPT,
    _BranchDigestReport,
    _branch_digest_ask,
    _digest_map,
    _digest_text,
    _fill_branch_digests,
    build_registry,
)
from app.contracts.mindmap import MindMapContent, Node
from app.generation.service import GenerationError

TOPIC = "校园AI应用"
BRANCHES = ("AI辅导与学习认知", "AI学情应用与教师定位", "提问技巧与工具组合")

DIGESTS = {
    "b0": "校园AI活动与科研成长",
    "b1": "AI学情应用与教师定位",
    "b2": "提问技巧与工具组合",
}


def _request(*, calls: int = 20) -> AgentRunRequest:
    return AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default",
        budget=RunBudget(max_model_calls=calls, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=1),
    )


def _ctx(*, calls: int = 20) -> RunContext:
    return RunContext(request=_request(calls=calls))


def _tree() -> dict:
    """三条一级分支、每支两个要点。`detail` 留空 → 结构那一轮不写，第三段来补。"""
    return {"title": TOPIC, "root": {
        "id": "root", "label": TOPIC, "children": [
            {"id": f"b{index}", "label": title, "children": [
                {"id": f"b{index}-1", "label": f"{title}·一"},
                {"id": f"b{index}-2", "label": f"{title}·二"},
            ]}
            for index, title in enumerate(BRANCHES)
        ]}}


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _chat(seen: list[tuple[str, str]], *, answer=None):
    """模型替身。`answer` 是概括那一次的回答（dict / 函数 / 异常）。"""
    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(BRANCHES)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {}
        if system.startswith(BRANCH_DIGEST_PROMPT):
            if isinstance(answer, BaseException):
                raise answer
            return answer(system, user) if callable(answer) else answer
        return _tree()
    return chat


def _run(ctx: RunContext, chat):
    registry = build_registry("create", has_sources=False, has_library=False)
    with patch("app.agent.tools.chat_async", new=chat):
        return asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate",
                     arguments={"topic": TOPIC, "branches": 3}), ctx))


def _digest_calls(seen: list[tuple[str, str]]) -> list[str]:
    return [user for system, user in seen if system.startswith(BRANCH_DIGEST_PROMPT)]


def _codes(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


# ---------------------------------------------------------------- 1. 开关


def test_off_by_default_leaves_content_and_data_untouched():
    """开关关着时：`content` 没有这个键、`data` 没有这个键、一次调用都不多发。

    这条是"默认关"的全部内容。`branch_digests` 为空时靠 `exclude_if` **不出现在
    序列化结果里**——所以 `model_dump()` 的键集与加这个字段之前逐个相同，不只是
    "值相同"。
    """
    seen: list[tuple[str, str]] = []
    ctx = _ctx()
    result = _run(ctx, _chat(seen, answer=DIGESTS))
    assert result.ok, result.content
    assert _digest_calls(seen) == []
    assert "branch_digest" not in result.data
    assert "branch_digests" not in ctx.candidate.model_dump(mode="json")


def test_switched_on_costs_exactly_one_call(monkeypatch):
    """开关开着时**一次**调用覆盖全部一级分支（不是每支一次）。

    分组批量是本轮唯一能接受的成本形状：实测 5 组一起 1.87 秒，逐组各调一次约 9 秒
    （`docs/FOLD-RETIRED.md` §5）。这条用例钉的是"调用次数上界是 1"。
    """
    monkeypatch.setenv("MINDMAP_BRANCH_DIGEST", "1")
    seen: list[tuple[str, str]] = []
    ctx = _ctx()
    result = _run(ctx, _chat(seen, answer=DIGESTS))
    assert result.ok, result.content
    assert len(_digest_calls(seen)) == 1
    assert ctx.candidate.branch_digests == DIGESTS
    account = result.data["branch_digest"]
    assert account["branches"] == 3
    assert account["written"] == 3
    assert account["skipped"] is None
    assert account["missing"] == []
    assert account["prompt_tokens"] > 0


def test_the_digest_is_written_into_the_contract_not_into_data(monkeypatch):
    """产物落契约字段（会跟 `content` 一起存库与下发），账落 `data`。

    两条路分开不是洁癖：`data` 是一次运行的读数，刷新页面就没了；而"这一支讲什么"
    是这张图的属性，重开一张图还得看得见，不然每次都要再调一次模型。
    """
    monkeypatch.setenv("MINDMAP_BRANCH_DIGEST", "1")
    ctx = _ctx()
    _run(ctx, _chat([], answer=DIGESTS))
    dumped = ctx.candidate.model_dump(mode="json")
    assert dumped["branch_digests"] == DIGESTS
    # 反过来：`content` 里不该多出别的键（读数一律不进内容）
    assert "branch_digest" not in dumped


# ---------------------------------------------------------------- 2. 输入形状


def test_the_ask_carries_ids_labels_and_child_titles_but_no_detail():
    """每条一级分支一行：id / 标题 / 下位节点的标题。

    **不带 `detail`**：一段 80–150 字的说明 ×6 支会把这一节从两百字撑到一千字，而
    概括恰恰是"只看目录"比"读完正文"更准的任务。这条用例用一个不含 detail 的树跑，
    断言 user 里也没有任何 detail 文本混进来。
    """
    rows = [
        (Node(id="b0", label=BRANCHES[0], children=[
            Node(id="b0-1", label="校园活动"), Node(id="b0-2", label="科研成长")]), ["校园活动", "科研成长"]),
    ]
    user = _branch_digest_ask(rows, TOPIC)
    assert f"主题：{TOPIC}" in user
    assert "id=b0｜AI辅导与学习认知｜下位：校园活动、科研成长" in user
    assert "请给这 1 条各写一句概括" in user


def test_the_ask_only_samples_the_first_few_children():
    """每支只看前 `BRANCH_DIGEST_CHILD_SAMPLES` 个子节点的标题（不把整支铺开）。"""
    node = Node(id="b0", label="甲", children=[
        Node(id=f"b0-{index}", label=f"子{index}") for index in range(8)])
    user = _branch_digest_ask([(node, [child.label for child in node.children[:3]])], TOPIC)
    assert "下位：子0、子1、子2" in user
    assert "子3" not in user


# ---------------------------------------------------------------- 3. 降级


def test_a_failed_call_degrades_to_numbers_only(monkeypatch):
    """调用抛错 → 树照常交付，只是没有概括。**不因为一句话把整棵树判成失败。**"""
    monkeypatch.setenv("MINDMAP_BRANCH_DIGEST", "1")
    ctx = _ctx()
    result = _run(ctx, _chat([], answer=GenerationError("上游 502")))
    assert result.ok, result.content
    assert ctx.candidate.branch_digests == {}
    assert "BRANCH_DIGEST_SKIPPED" in _codes(result)
    account = result.data["branch_digest"]
    assert account["skipped"] == "error"
    assert "上游 502" in account["failure"]
    # 树本身一个节点都不少（三支 × 两支要点 + 根，补充分支只会更多）
    assert result.data["node_count"] >= 7


def test_a_call_that_returns_nothing_usable_is_a_skip(monkeypatch):
    """模型返回了、但一条都对不上号 → skipped="error"，不是"写了一堆空串"。"""
    monkeypatch.setenv("MINDMAP_BRANCH_DIGEST", "1")
    ctx = _ctx()
    result = _run(ctx, _chat([], answer={"编的键": "编的值"}))
    assert result.ok, result.content
    assert ctx.candidate.branch_digests == {}
    assert result.data["branch_digest"]["skipped"] == "error"
    assert result.data["branch_digest"]["unknown"] == ["编的键"]


def test_no_budget_means_no_request():
    """额度用完 → 一次请求都不发（在发之前就占额度，与骨架那一步同一规矩）。"""
    ctx = _ctx(calls=1)
    ctx.model_calls_used = 1
    content = MindMapContent(
        title=TOPIC,
        root=Node(id="root", label=TOPIC, children=[
            Node(id=f"b{index}", label=title) for index, title in enumerate(BRANCHES)]))

    async def chat(*args, **kwargs):  # pragma: no cover — 不该被调到
        raise AssertionError("额度用完了还发了请求")

    with patch("app.agent.tools.chat_async", new=chat):
        filled, report = asyncio.run(
            _fill_branch_digests(content, ctx, topic=TOPIC))
    assert report.skipped == "budget"
    assert filled.branch_digests == {}


def test_a_single_branch_tree_is_not_worth_a_call():
    """只有一条一级分支：概括只能是它的标题，不调。"""
    ctx = _ctx()
    content = MindMapContent(
        title=TOPIC, root=Node(id="root", label=TOPIC, children=[Node(id="b0", label="甲")]))
    with patch("app.agent.tools.chat_async", new=_chat([])):
        filled, report = asyncio.run(_fill_branch_digests(content, ctx, topic=TOPIC))
    assert report.skipped == "few_branches"
    assert filled.branch_digests == {}
    assert ctx.model_calls_used == 0


# ---------------------------------------------------------------- 4. 按 id 对号


def test_missing_branches_are_named_not_invented(monkeypatch):
    """少写两支 → 只落写回来的那一支，缺的记进诊断。**不替它编一段。**"""
    monkeypatch.setenv("MINDMAP_BRANCH_DIGEST", "1")
    ctx = _ctx()
    result = _run(ctx, _chat([], answer={"b1": "AI学情应用与教师定位"}))
    assert result.ok, result.content
    assert ctx.candidate.branch_digests == {"b1": "AI学情应用与教师定位"}
    account = result.data["branch_digest"]
    assert account["written"] == 1
    assert account["missing"] == ["b0", "b2"]
    assert "BRANCH_DIGEST_INCOMPLETE" in _codes(result)


def test_digests_are_matched_by_id_not_by_order():
    """反着返回也照样落对：**按 id 对号**，不按顺序。"""
    ctx = _ctx()
    content = MindMapContent(
        title=TOPIC,
        root=Node(id="root", label=TOPIC, children=[
            Node(id="b0", label="甲"), Node(id="b1", label="乙")]))

    async def chat(system, user, **kwargs):
        return {"b1": "乙的概括", "b0": "甲的概括"}

    with patch("app.agent.tools.chat_async", new=chat):
        filled, _ = asyncio.run(_fill_branch_digests(content, ctx, topic=TOPIC))
    assert filled.branch_digests["b0"] == "甲的概括"
    assert filled.branch_digests["b1"] == "乙的概括"


def test_three_answer_shapes_are_accepted():
    """`{id: 文案}` / 多包一层 / `[{"id","summary"}]` 三种形状都认——形状容错。"""
    for raw in (
        {"b0": "甲的概括"},
        {"digests": {"b0": "甲的概括"}},
        [{"id": "b0", "summary": "甲的概括"}],
        {"b0": {"summary": "甲的概括"}},
    ):
        applied, missing, unknown = _digest_map(raw, ["b0"])
        assert applied == {"b0": "甲的概括"}, raw
        assert missing == []
        assert unknown == []


# ---------------------------------------------------------------- 5. 文案清洗


def test_digest_text_trims_quotes_and_clips():
    """引号与结尾标点剥掉、超长截断——**不改写、不补全**。"""
    assert _digest_text("「训练与并行」") == "训练与并行"
    assert _digest_text("训练与并行。") == "训练与并行"
    assert _digest_text("概括：训练与并行") == "训练与并行"
    assert _digest_text("长" * 40) == "长" * BRANCH_DIGEST_MAX_CHARS
    assert _digest_text(123) is None
    assert _digest_text("   ") is None


def test_the_report_account_shape():
    """账的键集是脚本与界面读的那一份（改键名等于改读数口径）。"""
    assert set(_BranchDigestReport().account()) == {
        "branches", "written", "missing", "unknown", "skipped", "failure",
        "seconds", "prompt_tokens", "completion_tokens"}


def test_the_prompt_forbids_restating_the_branch_title():
    """提示词必须写着"不许复用分支标题里的词"。

    这条要求是这枚角标能不能立住的前提：概括与标题说的是同一件事时，它就是噪声
    （见 `docs/VIEW-COLLAPSE.md` §七的自评）。而且它不是拍脑袋加的——初版提示词
    只说了"不要复述标题"，实测 6 条里有 5 条仍是标题的改写（「相似度打分与权重分配」
    →「相似度打分与归一化权重」）；改成"标题里出现过的两字以上实词一个都不许写进
    概括"之后，6 条里 5 条给出了标题没有的信息（`docs/BRANCH-DIGEST.md` §三）。
    """
    assert "不许复用分支标题里的词" in BRANCH_DIGEST_PROMPT
    assert "下位节点的词" in BRANCH_DIGEST_PROMPT
    assert "8–12 个汉字" in BRANCH_DIGEST_PROMPT
