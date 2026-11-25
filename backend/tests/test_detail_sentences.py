"""说明那一轮的**句子数下限**：开关 `MINDMAP_DETAIL_SENTENCES`。

「每一段至少 3 句完整的话」是 80 字下限之外的第二道加固（来历见
`tools._detail_only_prompt` 上方那段注释：把结构与说明压在同一次输出时 detail
越写越短，五采样 45.3/28.0/28.5/26.2/27.4 字）。这个文件钉三件事：

1. **关着时逐字节不变**：`DETAIL_ONLY_PROMPT` 的 sha256（467 字符的整份文本）、
   `_detail_prompt(80, 150)` 与之相等、真跑一轮的 system 仍以它开头、user 末尾
   自查句仍在问"是不是都有 3 句以上"。
2. **开着时换的只有那一条**：字数下限 80 还在、上限 150 还在、并列型 label 逐个
   说清还在、`{id: 说明}` 的形状与"不要写 source_refs"一字不改；变的是"至少 N 句"
   → "几句都行 + 不许把一句话硬拉长到下限"。
3. **`is_detail_only_prompt` 两种写法都认**：开关一开，system 就不再以那 467 个
   字符开头，而"按 system 前缀分类哪一轮"散在测试与测量脚本里（十几个
   `startswith(DETAIL_ONLY_PROMPT)`）。没有这个判据，打开开关跑一遍会把第三段误判
   成结构轮。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
from unittest.mock import patch

from app.agent import tools
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext
from app.agent.tools import (
    DETAIL_MAX_CHARS,
    DETAIL_MIN_CHARS,
    DETAIL_ONLY_PROMPT,
    DETAIL_SENTENCES_ENV,
    _detail_only_prompt,
    _detail_only_prompt_open,
    _detail_prompt,
    detail_sentences_open,
    is_detail_only_prompt,
)
from app.contracts.mindmap import MindMapContent

TOPIC = "机器学习的主要方向"
BRANCHES = ("监督学习", "无监督学习", "模型与方法")

#: `DETAIL_ONLY_PROMPT` 的整份文本指纹（467 字符，`_detail_only_prompt(80, 150)`）。
#: 这个值**只能因为 prompt 有意的改动而变**：它是"关着时逐字节不变"那条承诺的
#: 唯一机器判据——改成"包含某几个子串"会漏掉任何一处措辞漂移。
DETAIL_ONLY_PROMPT_SHA256 = (
    "5064c0271312bb18ca107ccfb50cdb33fbadd128fc56f1255e67a19debddd04b"
)

MATERIAL = (
    "监督学习用带标签的数据训练，分类与回归都属于它。常见模型有线性回归、决策树与"
    "神经网络；评价指标看准确率与召回率。\n"
    "无监督学习在无标注的数据上找结构，聚类、降维与异常检测是三类主要任务。\n"
    "线性模型、树模型与神经网络是三类常见载体；集成方法把多个弱模型拼成强模型。\n"
)

#: 3 支 × 2 要点 + 中心主题 = 10 个待写节点，低于 `DETAIL_SPLIT_MIN`（12）：一次写完。
TARGETS = 10


def _tree() -> dict:
    return {"title": TOPIC, "root": {
        "id": "root", "label": TOPIC, "detail": "", "children": [
            {"id": f"b{index}", "label": title, "detail": "", "children": [
                {"id": f"b{index}-1", "label": f"{title}·一", "detail": ""},
                {"id": f"b{index}-2", "label": f"{title}·二", "detail": ""},
            ]}
            for index, title in enumerate(BRANCHES)
        ]}}


def _ctx(*, calls: int = 10) -> RunContext:
    request = AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default", sources=[],
        budget=RunBudget(max_model_calls=calls, max_tool_calls=4, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=0))
    return RunContext(request=request, sources_by_id={})


@contextlib.contextmanager
def _sentences(value: str | None):
    """开关三态：`None` = 不设（默认关）、`"1"` = 开、`"0"` = 显式关。"""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(DETAIL_SENTENCES_ENV, None)
        if value is not None:
            os.environ[DETAIL_SENTENCES_ENV] = value
        yield


def _walk(node):
    for child in node.children:
        yield from _walk(child)
    yield node


def _run_one_detail_round(
    seen: list[tuple[str, str]], *, answer: dict | None = None,
) -> tuple[MindMapContent, dict]:
    """真跑一次 `_fill_details`（模型替身），返回落地后的树与那份账。"""
    from app.agent.contracts import Diagnostic

    content = MindMapContent.model_validate(_tree())
    ctx = _ctx()
    diagnostics: list[Diagnostic] = []

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        seen.append((system, user))
        # 中心主题也是一个待写节点（它的 detail 同样要补），漏了它会变成一条
        # `DETAIL_ROUND_INCOMPLETE`——那是另一件事，不该混进这里的断言。
        ids = [node.id for node in _walk(content.root)]
        return answer if answer is not None else {node_id: "说明正文。" * 8 for node_id in ids}

    with patch("app.agent.tools.chat_async", new=chat):
        filled, report = asyncio.run(tools._fill_details(
            content, ctx=ctx, topic=TOPIC, sections=[MATERIAL],
            external_rules="", diagnostics=diagnostics))
    report["_diagnostics"] = [item.code for item in diagnostics]
    return filled, report


# ---------------------------------------------------------------- 1. 关着：逐字节


def test_the_current_prompt_is_byte_identical_to_the_baseline():
    """整份文本的 sha256：抽取公共前半段（`_DETAIL_ONLY_SHAPE_RULES`）之后仍然不变。"""
    assert len(DETAIL_ONLY_PROMPT) == 467
    assert (hashlib.sha256(DETAIL_ONLY_PROMPT.encode("utf-8")).hexdigest()
            == DETAIL_ONLY_PROMPT_SHA256)


def test_the_switch_is_off_by_default():
    """默认关：没设 / 空串 / `"0"` 一律算关，`"1"` `"on"` `"true"` `"yes"` 算开。"""
    for value in (None, "", "0", "off", "false", "no"):
        with _sentences(value):
            assert not detail_sentences_open(), value
    for value in ("1", "on", "true", "yes", "ON", " 1 "):
        with _sentences(value):
            assert detail_sentences_open(), value


def test_the_dispatcher_returns_the_unchanged_prompt_when_off():
    """关着时 `_detail_prompt` 就是 `_detail_only_prompt`：一条分支都不多。"""
    with _sentences(None):
        assert _detail_prompt(80, 150) == DETAIL_ONLY_PROMPT
        # 伸缩开着时给的是别的区间，也必须与纯函数逐字节相同（开关不接管）
        assert _detail_prompt(75, 135) == _detail_only_prompt(75, 135)


def test_the_third_call_still_asks_for_three_sentences_when_off():
    """真跑一轮：system 以 `DETAIL_ONLY_PROMPT` 开头，user 末尾仍问"3 句以上"。"""
    seen: list[tuple[str, str]] = []
    with _sentences(None):
        filled, report = _run_one_detail_round(seen)
    assert report["filled"] == TARGETS
    system, user = seen[0]
    assert system.startswith(DETAIL_ONLY_PROMPT)
    assert "每一段是不是都有 3 句以上" in user
    assert "sentences_open" not in report
    assert "DETAIL_SENTENCES_OPEN" not in report["_diagnostics"]
    assert all(node.detail for node in _walk(filled.root))


# ---------------------------------------------------------------- 2. 开着：只换那一条


def test_the_open_prompt_drops_the_sentence_quota_and_keeps_the_range():
    """去句数，留字数：下限、上限、并列项、形状、禁引用一个都不许少。"""
    with _sentences("1"):
        opened = _detail_prompt(DETAIL_MIN_CHARS, DETAIL_MAX_CHARS)

    assert opened != DETAIL_ONLY_PROMPT
    # 句子数配额必须真的不在了（三处措辞一起查，漏一处模型就还能读到"3 句"）
    assert "3 句" not in opened
    assert "句完整的话" not in opened
    assert "不是 3 个短语" not in opened
    # 字数下限与上限还在，而且是同两个数（伸缩开着时它们会变，措辞不变）
    assert f"写 {DETAIL_MIN_CHARS}–{DETAIL_MAX_CHARS} 字" in opened
    assert f"少于 {DETAIL_MIN_CHARS} 字就是没写完" in opened
    assert f"超过 {DETAIL_MAX_CHARS} 字就挑最关键的两三点" in opened
    # 防"一句话硬撑到下限"：这句是去掉句数之后唯一的新约束
    assert f"不要把一句话硬拉长到 {DETAIL_MIN_CHARS} 字" in opened
    # 与 A 那一份同源的三条规则：形状、禁引用、并列型 label 逐个说清
    assert '{"d1a":"…","d1b":"…"}' in opened
    assert "不要写 source_refs" in opened
    assert "要逐个说清它们各自做什么、差别在哪" in opened
    # 分寸：不补"一句话就能说清也够"这类与下限打架的话
    assert "一句话就能说清" not in opened


def test_the_open_prompt_follows_the_range_parameter():
    """开着时区间仍然参数化：伸缩给 75–135，措辞里的数字跟着换。"""
    with _sentences("1"):
        opened = _detail_prompt(75, 135)
    assert "写 75–135 字" in opened
    assert "少于 75 字就是没写完" in opened
    assert "150" not in opened


def test_the_pure_constructor_is_not_polluted_by_the_switch():
    """`_detail_only_prompt` 自己不看环境变量：它是被"逐字节相同"钉住的那一份。"""
    with _sentences("1"):
        assert _detail_only_prompt(80, 150) == DETAIL_ONLY_PROMPT
        assert _detail_only_prompt_open(80, 150) != DETAIL_ONLY_PROMPT


def test_the_third_call_drops_the_sentence_check_when_open():
    """真跑一轮：system 不含句数、user 末尾自查句不再问句数、`data` 里留一条自证。"""
    seen: list[tuple[str, str]] = []
    with _sentences("1"):
        filled, report = _run_one_detail_round(seen)
    assert report["filled"] == TARGETS
    system, user = seen[0]
    assert "3 句" not in system
    # 自查句在 user 最末尾，是模型读的最后一行：它也必须跟着变
    assert "句以上" not in user
    assert f"每一段是不是都在 {DETAIL_MIN_CHARS}–{DETAIL_MAX_CHARS} 字" in user
    assert report["sentences_open"] is True
    assert "DETAIL_SENTENCES_OPEN" in report["_diagnostics"]
    assert all(node.detail for node in _walk(filled.root))


def test_the_switch_does_not_change_the_ask_anything_else():
    """user 的正文（主题 / 清单 / 证据 / 自查句之前的部分）逐字节相同。"""
    seen_off: list[tuple[str, str]] = []
    seen_on: list[tuple[str, str]] = []
    with _sentences(None):
        _run_one_detail_round(seen_off)
    with _sentences("1"):
        _run_one_detail_round(seen_on)
    user_off, user_on = seen_off[0][1], seen_on[0][1]
    head_off = user_off.split("写完后自查一遍：")[0]
    head_on = user_on.split("写完后自查一遍：")[0]
    assert head_off == head_on


# ---------------------------------------------------------------- 3. 前缀判据


def test_the_marker_recognises_both_prompts():
    """开关一开 system 就不再以整份常量开头：`is_detail_only_prompt` 必须都认。"""
    with _sentences("1"):
        opened = _detail_prompt(80, 150)
    assert not opened.startswith(DETAIL_ONLY_PROMPT)
    assert is_detail_only_prompt(DETAIL_ONLY_PROMPT)
    assert is_detail_only_prompt(opened)
    # 结构轮 / 计划轮的 system 不能误判进来
    assert not is_detail_only_prompt(tools.SKELETON_TREE_RULES)
    assert not is_detail_only_prompt(tools.PLAN_PROMPT)
