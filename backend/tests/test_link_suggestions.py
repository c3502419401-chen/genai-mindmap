"""`SuggestionKind.link` 的第一版：候选父、证据回指、以及"先描述再决定"。

这一组用例守四件事，它们各自都能独立退化：

  1. **候选池**：不能当自己的父（环），不能挂到会超深的层，当前父置顶
     （模型有权选"不用动"）。
  2. **证据回指**：模型写的 id 不在这次请求给过的范围里 → 那一条证据被丢掉。
     "看起来可查、点下去是空的"比没有依据更坏。
  3. **提示词的形状**：`what` / `line` 必须排在候选前面。这条最容易悄悄退化——
     提示词被改成"直接选父节点"之后，产出仍然像模像样，但那是论文里更差的那一侧
     （0.3045 vs 0.4832）。
  4. **失败降级**：模型挂了、关了开关、没有候选——一律返回空报告，**绝不抛**。
     建议是附加信息，判不出来不等于这张图坏了。

全程不碰网络与数据库：`chat` / `history` / `material` 三处注入缝全给死数据。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.contracts.diagnostic import SuggestionKind
from app.contracts.mindmap import MindMapContent
from app.optimization import link

CONTENT = MindMapContent.model_validate({
    "title": "示例",
    "root": {
        "id": "root", "label": "主题", "children": [
            {"id": "a", "label": "成本", "children": [
                {"id": "a1", "label": "人力", "children": [
                    {"id": "a1x", "label": "招聘周期", "children": []},
                ]},
            ]},
            {"id": "b", "label": "风险", "children": [
                {"id": "b1", "label": "合规", "children": []},
            ]},
        ],
    },
})


def run(coro):
    return asyncio.run(coro)


def find(node_id: str):
    """按 id 取节点。`messages` 要的是节点本人（它可能不在树上，实验里就是）。"""
    stack = [CONTENT.root]
    while stack:
        node = stack.pop()
        if node.id == node_id:
            return node
        stack.extend(node.children)
    raise KeyError(node_id)


class FakeChat:
    """记录被调了几次、拿到了什么提示词，然后吐一段固定 JSON。"""

    def __init__(self, payload: dict | str | None = None, *, fail: bool = False):
        self.payload = payload
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system: str, user: str, **kwargs):
        self.calls.append((system, user))
        if self.fail:
            raise RuntimeError("模型挂了")
        return self.payload


# ---------------------------------------------------------------- 候选池


def test_target_and_its_descendants_are_never_candidates():
    """挂到自己的后代会成环——那一层校验在 `move_node` 的 `tree_invalid` 里，
    但这里就不该把它当候选递出去：给人看的候选表里出现一个必然被拒的选项，
    是把"算法该拦的"变成"用户点一下才知道"。"""
    ids = {choice.node_id for choice in link.parent_choices(CONTENT, "a")}

    assert "a" not in ids
    assert "a1" not in ids
    assert "a1x" not in ids
    assert {"root", "b", "b1"} <= ids


def test_current_parent_comes_first():
    """"不用动"必须是选项之一，而且排第一：用户的挂法可能是对的。

    这一步产出的是**建议**，不是纠正——把当前父排除掉，每条建议都会变成
    "你挂错了"，而我们连一个 `add` 样本都还没有。
    """
    choices = link.parent_choices(CONTENT, "b1")

    assert choices[0].node_id == "b"
    assert choices[0].current is True
    assert all(not choice.current for choice in choices[1:])


def test_depth_cap_excludes_the_layer_that_would_break_the_contract():
    """挂上去之后是 `depth + 1` 层，超过契约上限的那一层不可能是答案。"""
    deep = {"title": "深", "root": {"id": "n1", "label": "1", "children": []}}
    node = deep["root"]
    for index in range(2, 9):
        child = {"id": f"n{index}", "label": str(index), "children": []}
        node["children"] = [child]
        node = child
    content = MindMapContent.model_validate(deep)

    ids = {choice.node_id for choice in link.parent_choices(content, "n8")}

    # n7 已经第 7 层，它还有 n8 这个子节点；能当 n8 的父的只有第 1–7 层里的节点。
    assert "n7" in ids
    assert "n8" not in ids


def test_there_is_nothing_to_suggest_when_the_parent_is_the_only_choice():
    """只有一个候选（"只能挂这儿"）不叫建议。"""
    tiny = MindMapContent.model_validate({
        "title": "小", "root": {"id": "root", "label": "题", "children": [
            {"id": "a", "label": "甲", "children": []},
        ]},
    })

    report = run(link.suggest(tiny, "a", on=True, chat=FakeChat({})))

    assert report.skipped == "no_choices"
    assert report.suggestions == []


# ---------------------------------------------------------------- 证据回指


def test_evidence_that_was_not_offered_is_dropped():
    """模型编的 id 一律丢；合法的留着。**不静默保留任何一条。**"""
    choices = link.parent_choices(CONTENT, "b1")
    payload = {
        "what": "合规要求",
        "line": "风险这一支",
        "candidates": [
            {"parent_id": "a", "label": "成本", "why": "跟成本有关",
             "evidence": ["node:a", "根据用户习惯", "node:b"]},
        ],
    }

    parsed = link.parse(payload, choices)

    assert parsed["candidates"][0]["evidence"] == ["node:a", "node:b"]
    assert parsed["dropped_evidence"] == 1


def test_a_parent_outside_the_choices_kills_the_candidate():
    """挂到一个没给过的父上 → 那条候选整条丢掉（界面上无处可点）。"""
    choices = link.parent_choices(CONTENT, "b1")
    payload = {"what": "x", "line": "y", "candidates": [
        {"parent_id": "ghost", "label": "不存在的", "why": "", "evidence": []},
        {"parent_id": "root", "label": "主题", "why": "跨支", "evidence": []},
    ]}

    parsed = link.parse(payload, choices)

    assert [item["parent_id"] for item in parsed["candidates"]] == ["root"]
    assert parsed["dropped_candidates"] == 1


def test_duplicate_parent_is_kept_once():
    choices = link.parent_choices(CONTENT, "b1")
    payload = {"what": "x", "line": "y", "candidates": [
        {"parent_id": "root", "why": "理由一", "evidence": []},
        {"parent_id": "root", "why": "理由二", "evidence": []},
    ]}

    parsed = link.parse(payload, choices)

    assert len(parsed["candidates"]) == 1
    assert parsed["candidates"][0]["why"] == "理由一"


def test_history_and_material_refs_are_acceptable_evidence():
    """跨图与材料两侧的 id 也是合法证据——那正是"依据从哪来"要说清的东西。"""
    choices = link.parent_choices(CONTENT, "b1")
    history = [{"ref": "map:m1#合规", "parent": "风险", "title": "另一张图", "score": 0.91}]
    material = [{"ref": "chunk:s1:120", "text": "监管要求……", "source_id": "s1",
                 "start": 120, "end": 200}]
    payload = {"what": "x", "line": "y", "candidates": [
        {"parent_id": "b", "why": "同源", "evidence": ["map:m1#合规", "chunk:s1:120"]},
    ]}

    parsed = link.parse(payload, choices, history, material)

    assert parsed["candidates"][0]["evidence"] == ["map:m1#合规", "chunk:s1:120"]
    assert parsed["dropped_evidence"] == 0


# ---------------------------------------------------------------- 提示词的形状


def test_prompt_makes_the_model_describe_before_it_decides():
    """`what` / `line` 排在候选前面，而且系统提示词把顺序写死。

    这是整条链上最容易悄悄退化的地方：把它删掉，产出仍然像模像样，但那是
    Coco 论文 §6.1 Table 2 里更差的那一侧（Decision Only，0.3045 vs 0.4832）。
    所以把它钉成断言，而不是靠"记得"。
    """
    system, user = link.messages(CONTENT, find("b1"),
                                 choices=link.parent_choices(CONTENT, "b1"))

    assert '"what"' in system and '"line"' in system and '"candidates"' in system
    assert system.index('"what"') < system.index('"candidates"')
    assert "先回答" in system
    # 用户消息里，节点本身与候选表都在；没有它，模型只能靠标签空猜。
    assert "新加的节点" in user
    assert "可以当父的节点" in user


def test_describe_stage_asks_for_nothing_but_what_and_line():
    """消融的那一半：只问描述。跑它才复现得出论文的对照。"""
    _, user = link.messages(CONTENT, find("b1"),
                            choices=link.parent_choices(CONTENT, "b1"), stage="describe")

    assert "不要给候选" in user
    assert "据此给候选" not in user


def test_two_stage_folds_the_first_answer_into_the_second_prompt():
    _, user = link.messages(CONTENT, find("b1"),
                            choices=link.parent_choices(CONTENT, "b1"),
                            stage="two_stage", what="合规要求", line="风险这一支")

    assert "合规要求" in user
    assert "风险这一支" in user


# ---------------------------------------------------------------- 完整一次


def test_suggest_produces_one_link_suggestion_with_a_readable_payload():
    payload = {
        "what": "供应商资质审查要求",
        "line": "风险这一支",
        "candidates": [{"parent_id": "b", "why": "合规属于风险", "evidence": ["node:b"]}],
        "suggestion_kind": "link",
    }
    chat = FakeChat(payload)

    report = run(link.suggest(CONTENT, "b1", on=True, chat=chat,
                              history=[], material=[]))

    assert report.calls == 1
    assert len(report.suggestions) == 1
    suggestion = report.suggestions[0]
    assert suggestion.kind is SuggestionKind.link
    assert suggestion.node_id == "b1"
    assert "合规" in suggestion.message
    body = json.loads(suggestion.evidence)
    assert body["what"] == "供应商资质审查要求"
    assert body["line"] == "风险这一支"
    assert body["candidates"][0]["parent_id"] == "b"
    assert body["suggestion_kind"] == "link"


def test_disable_switch_means_zero_calls():
    chat = FakeChat({"what": "x", "line": "y", "candidates": []})

    report = run(link.suggest(CONTENT, "b1", on=False, chat=chat))

    assert report.skipped == "disabled"
    assert chat.calls == []
    assert report.suggestions == []


def test_model_failure_returns_an_empty_report_instead_of_raising():
    report = run(link.suggest(CONTENT, "b1", on=True, chat=FakeChat(fail=True),
                              history=[], material=[]))

    assert report.skipped == "model_failed"
    assert report.suggestions == []


def test_garbage_output_is_not_patched_into_a_suggestion():
    """模型回一段不是 JSON 的正文 → 空报告。**不猜、不补救。**"""
    report = run(link.suggest(CONTENT, "b1", on=True,
                              chat=FakeChat("我觉得应该挂在成本下面"),
                              history=[], material=[]))

    assert report.skipped == "no_candidates"
    assert report.suggestions == []


def test_markdown_fence_is_stripped_but_nothing_else_is():
    fenced = ('```json\n{"what": "合规", "line": "风险", "candidates": '
              '[{"parent_id": "b", "why": "同源", "evidence": []}]}\n```')
    report = run(link.suggest(CONTENT, "b1", on=True, chat=FakeChat(fenced),
                              history=[], material=[]))

    assert report.suggestions, "剥掉围栏之后应当能解析"
    assert json.loads(report.suggestions[0].evidence)["what"] == "合规"


def test_two_stage_makes_two_calls_and_says_so():
    class Scripted:
        def __init__(self):
            self.calls: list[str] = []

        async def __call__(self, system: str, user: str, **kwargs):
            self.calls.append(user)
            if len(self.calls) == 1:
                return {"what": "合规要求", "line": "风险这一支"}
            return {"what": "合规要求", "line": "风险这一支",
                    "candidates": [{"parent_id": "b", "why": "同一支", "evidence": []}]}

    chat = Scripted()
    report = run(link.suggest(CONTENT, "b1", on=True, chat=chat, history=[], material=[],
                              two_stage=True))

    assert report.calls == 2
    assert "合规要求" in chat.calls[1]
    assert json.loads(report.suggestions[0].evidence)["stage"] == "two_stage"


@pytest.mark.parametrize("payload", [None, {}, {"candidates": "不是数组"}])
def test_shape_errors_do_not_raise(payload):
    report = run(link.suggest(CONTENT, "b1", on=True, chat=FakeChat(payload),
                              history=[], material=[]))

    assert report.suggestions == []
