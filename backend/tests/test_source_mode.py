"""`MINDMAP_SOURCE_MODE` 的两态：`full`（正文整份进 prompt）与 `rag`（只给清单）。

这次切换是**形态变化**，不是优化：`full` 下模型自己就能扫全文，`rag_query` 只是
"帮我定位该引用哪一段"的辅助；`rag` 下模型只看得见检索回来的片段，检索成了唯一
入口。两者的提示词、引用来源、失败方式都不同，所以各钉一组用例。

本文件只钉两种形态的**差别**，四件事：

  1. `rag`（默认）下正文**不进 system 提示词**，进 user 的唯一通道是检索命中
     （`mindmap_generate` 生成前自己检一次，见 `tests/test_generate_evidence.py`）；
     反过来 `full` 下正文必须整份进，否则这份用例证明不了"差别是开关造成的"。
     这条口径在 `mindmap_generate` 补上自己那次检索之前是"一个字都不进 prompt"，
     现在是"只能经命中进、不能进 system"——旧口径守的是"别把全文塞进去"，
     新口径守的是同一件事，同时允许那份被检索回来的片段。
  2. `full` 与 `901b484` 逐字节一致。哈希写死，不取 HEAD：拿 HEAD 当基准等于
     "今天的我再确认一次昨天的我"，一次误改会被下一次提交顺手合法化。
  3. 清单超限只截清单项；**短材料也不回退 `full`**——行为随材料大小跳变是最难
     排查的一类 bug，同一份请求换个字号结论就变了。
  4. 检索规程（"看不到全文 / 引用必须来自检索结果 / 检索不到就不要写"）真的
     写进了模型看得到的那段系统提示词，而不只是躺在常量里。

`full` 形态的其余既有用例留在 `tests/test_agent_tools.py`（那个文件按模块固定成
`full` 形态），这里不重复。
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.agent.contracts import (
    MAX_SOURCES,
    AgentRunRequest,
    Completion,
    RunBudget,
    RunContext,
    ToolCall,
)
from app.agent.service import _INTENT_RULES, _RAG_INTENT_RULES, _system_text, run
from app.agent.tools import (
    GENERATE_EVIDENCE_RULES,
    MATERIAL_MANIFEST_MAX_ITEMS,
    MATERIAL_MANIFEST_TOKEN_LIMIT,
    _material_manifest,
    _source_prompt_input,
    build_registry,
)
from app.contracts.source import AudioSentence, Source, SourceKind
from app.generation.service import (
    CONTEXT_TOKEN_LIMIT,
    RAG_SOURCE_PROMPT,
    RAG_THINKING_GUARD,
    SOURCE_MODE_ENV,
    SOURCE_MODE_FULL,
    SOURCE_MODE_RAG,
    SOURCED_DETAIL_PROMPT,
    SOURCED_THINKING_GUARD,
    _WARNED_SOURCE_MODES,
    estimate_input_tokens,
    source_mode,
    thinking_block,
)
from app.knowledge.service import search_parent

# 一级分支必须 3 条：`mindmap_generate` 走 `MIN_BRANCHES = 3` 那道关。
TREE = {
    "title": "示例",
    "root": {
        "id": "root", "label": "根", "children": [
            {"id": "a", "label": "甲", "children": []},
            {"id": "b", "label": "乙", "children": []},
            {"id": "c", "label": "丙", "children": []},
        ],
    },
}

# 正文里的独特标记：只要它出现在 prompt 里，就说明有正文漏进去了。
MARKER = "ZZQX_MARKER"

REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_COMMIT = "901b484"
LEGACY_PATH = "backend/app/agent/tools.py"


def _env(mode: str):
    """把形态显式切到 `mode`（退出时还原）。"""
    return patch.dict(os.environ, {SOURCE_MODE_ENV: mode})


def _ctx(sources: list[Source]) -> RunContext:
    request = AgentRunRequest(
        goal="整理材料", intent="create", sources=sources,
        budget=RunBudget(max_model_calls=5, max_output_tokens=32768),
    )
    return RunContext(
        request=request, candidate=None,
        sources_by_id={source.id: source for source in sources},
    )


def _generate_prompt(sources: list[Source], *,
                     topic: str = "整理这些材料") -> tuple[str, str]:
    """跑一次真的 `mindmap_generate`，把模型实际收到的 (system, user) 拿回来。

    `topic` 可换：`rag` 形态下 `mindmap_generate` 会拿它做**一次主题检索**（见
    `tools._retrieve_topic`），而检索命中进不进得来，取决于主题与正文有没有共同
    词项。默认的"整理这些材料"是**刻意选的**：它与制度类正文不共享任何 bigram，
    于是那几条用例走的是"检索为空 → 退回无来源形态"这条路。
    """
    mocked = AsyncMock(return_value=TREE)
    with patch("app.agent.tools.chat_async", new=mocked):
        result = asyncio.run(build_registry("create").execute(
            ToolCall(call_id="c-gen", name="mindmap_generate",
                     arguments={"topic": topic}),
            _ctx(sources),
        ))
    assert result.ok, result.content
    system, user = mocked.await_args.args
    return system, user


# 命中被渲染进 user 消息时的那句表头（`tools.GENERATE_EVIDENCE_HEADER` 的开头）。
# 断言里拿它当分隔符：它之前是材料清单，之后是检索命中。
EVIDENCE_HEADER = "本次已对该主题做过一次检索"


def _split_evidence(user: str) -> tuple[str, str]:
    """(清单部分, 命中部分)。没有命中时第一部分是整段、第二部分是空串。"""
    manifest, _, evidence = user.partition(EVIDENCE_HEADER)
    return manifest, evidence


def _material(text: str, *, filename: str = "预算管理办法.docx",
              source_id: str = "src-1", **extra) -> Source:
    return Source(id=source_id, kind=SourceKind.document, status="parsed",
                  filename=filename, text=text, **extra)


# ---------------------------------------------------------------- 一、正文进不进


def test_rag_mode_keeps_the_material_text_out_of_the_system_prompt():
    """`rag` 下正文一个字都不进 **system**；进 user 的只有检索命中那一段。

    这条用例原来断言的是"正文不进 prompt"。`mindmap_generate` 现在会自己先检索
    一次（`tools._retrieve_topic`：不检索就一个字都拿不到，见
    `tests/test_generate_evidence.py`），所以"正文永不进 prompt"不再是事实，也
    不再是要求——要求变成了：**正文只能经检索命中进入 user，且不能进 system**。
    断言因此从"不含标记词"改成"标记词只出现在命中段之后"：长度或截断都不能
    满足这条，位置可以。
    """
    source = _material(f"第一章 总则。{MARKER} 预算由财务经理核准。", filename="预算管理办法.docx")
    with _env(SOURCE_MODE_RAG):
        system, user = _generate_prompt([source], topic="预算由谁核准")

    manifest, evidence = _split_evidence(user)
    assert MARKER not in system            # 提示词里没有正文
    assert evidence                        # 主题与正文有共同词项：这次必须命中
    assert MARKER not in manifest          # 清单里也没有正文
    assert MARKER in evidence              # 正文只从检索命中这一条路进来
    assert "预算管理办法" in user          # 清单里要有名字
    assert "docx" in user                  # 与类型
    assert f"{len(source.text or ''):,} 字" in user
    assert source.id in user               # id 也要在：模型才知道引用该指向谁


def test_full_mode_puts_the_material_text_in_the_prompt():
    """同一个标记词在 `full` 下必须出现——否则上一条证明不了是开关造成的差别。

    system 用**整串相等**钉住（不是 `startswith`）：`full` 形态要与 `901b484`
    逐字节一致，提示词的拼接顺序（思维步骤在中间、限定句在最后）也在其中。
    """
    source = _material(f"第一章 总则。{MARKER} 预算由财务经理核准。")
    with _env(SOURCE_MODE_FULL):
        system, user = _generate_prompt([source])

    assert MARKER in user
    assert system == SOURCED_DETAIL_PROMPT + thinking_block("default") + SOURCED_THINKING_GUARD


def test_default_mode_is_rag():
    """不设变量时走 `rag`：默认值本身是被测行为，不能只靠"某处显式设了"。

    这里把变量删掉而不是设成 `"rag"`：`conftest.clean_env` 也是删掉它，所以
    "删掉之后是 rag"正是线上不带配置时的实际路径。`patch.dict` 退出时会把整个
    环境恢复（包括我们 pop 掉的那个键），不需要手工还原。
    """
    source = _material(f"正文 {MARKER}")
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(SOURCE_MODE_ENV, None)
        assert source_mode() == SOURCE_MODE_RAG
        system, user = _generate_prompt([source])
    assert MARKER not in system + user


def test_short_material_still_goes_through_retrieval():
    """材料很短（合计不到 3000 字）也走清单 + 检索，不偷偷退回 `full`。

    "小材料退回整份塞入"看起来是优化，实际是把一条形态分界线藏进材料大小里：
    用户改了材料内容、行为就变了，而提示词、引用来源、失败方式全都跟着换。

    判据是**形状**不是"正文在不在"：`full` 形态的输入是一段 `sources 数组（JSON）`，
    而这里必须是"清单 + 命中"。短材料也会被主题检索命中（"材料"这两个字就在正文
    里），所以正文会经命中进来——那不是退回 `full`，`full` 的数组形状并不存在。
    """
    sources = [_material("短材料一段。" * 20, source_id=f"s{index}",
                         filename=f"材料{index}.txt") for index in range(3)]
    total = sum(len(source.text or "") for source in sources)
    assert total < 3000
    with _env(SOURCE_MODE_RAG):
        _, user = _generate_prompt(sources)
    manifest, evidence = _split_evidence(user)
    assert "sources 数组" not in user          # 没有退回 `full` 的输入形状
    assert "短材料一段" not in manifest         # 清单里一个字的正文都没有
    assert "短材料一段" in evidence             # 正文只经检索命中进来
    assert f"本次附带 {len(sources)} 份材料" in user


# ---------------------------------------------------------------- 二、full 形态的回归


def _fixtures() -> dict[str, list[Source]]:
    """`full` 形态的回归样本。id 与正文全部写死，否则哈希无法钉住。"""
    return {
        "single": [Source(id="s1", kind=SourceKind.text, status="parsed",
                          text="第一段原始材料。")],
        "two": [
            Source(id="s2", kind=SourceKind.text, status="parsed", text="甲材料正文。"),
            Source(id="s3", kind=SourceKind.document, status="parsed",
                   filename="乙.docx", text="乙材料正文。"),
        ],
        # 空正文：`json.dumps` 出来是 `""`，与"没有这个字段"不是一回事
        "blank": [Source(id="s4", kind=SourceKind.text, status="parsed", text=None)],
        # 转义：`ensure_ascii=False` 与裸引号/反斜杠的行为一起钉住
        "escapes": [Source(id="s5", kind=SourceKind.text, status="parsed",
                           text='中A\\"\n\t引号"括号（）')],
        # 超预算：走二分裁剪，落点取决于 token 估算，最容易被"顺手优化"改动
        "oversized": [Source(id="s6", kind=SourceKind.text, status="parsed",
                             text="字" * 400_000)],
    }


# 取自 `901b484` 的 `_source_prompt_input` 渲染结果（sha256）。写死是刻意的：
# 这条断言的价值在于"今天的输出与那个提交逐字节相同"，而不是"与今天自己相同"。
#
# `oversized` 这一条在"内容可自由展开、引用必须标出处"那一轮更新过：那个样本走
# 二分裁剪，裁剪点由 `system` 的 token 估算决定，而 `SOURCED_DETAIL_PROMPT` 变长了
# ——渲染算法一个字节没动，落点却必须跟着变。这是**有意的**更新，其余四条仍与
# `901b484` 一致，正好证明改的只是预算、不是渲染。
#
# 第二次更新（`label ≤16 字 + detail`）同理：提示词里多了"label 是一句话短语…
# detail 是 2–3 句说明"那几句，`SOURCED_DETAIL_PROMPT` 又长了 200 多字，同一个
# 样本的裁剪落点随之后移。渲染算法仍然没动，另外四条摘要一字未变。
GOLDEN_DIGESTS: dict[str, str] = {
    "single": "09dad633796b95214e26057a537a50b1acc002083ccc06522b30fe9d67c7d3ac",
    "two": "0ddbba96dfa703350137b9d315e07fe2e5c77774eb1768ff7d5fcd42115d0e33",
    "blank": "309d1757a17504d05d7747846944f2b0280213a74a6b348ffe95311c5331ce22",
    "escapes": "46593d9c4358005c37cf70123016af4643daace3e2f1bb8ec75397e747d9c10c",
    "oversized": "67942f263df84649b59e3c6e3cdd9e1c889a05154e75def5338994e694021e24",
}


def _digest(sources: list[Source]) -> str:
    with _env(SOURCE_MODE_FULL):
        rendered = _source_prompt_input(sources, system=SOURCED_DETAIL_PROMPT)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def test_full_mode_rendering_is_pinned_by_digest():
    """`full` 的渲染结果按 sha256 钉死。不需要 git，也不看 HEAD。"""
    mismatches = {}
    for name, sources in _fixtures().items():
        actual = _digest(sources)
        expected = GOLDEN_DIGESTS.get(name)
        if actual != expected:
            mismatches[name] = {"actual": actual, "pinned": expected}
    assert not mismatches, (
        "`full` 形态的输入渲染变了。它必须与 901b484 逐字节一致——"
        "要改请确认这是有意的，并同时更新 GOLDEN_DIGESTS："
        + json.dumps(mismatches, ensure_ascii=False)
    )


def _legacy_renderer():
    """载入 `901b484` 的 `tools.py`，借它的 `_source_prompt_input` 做逐字节对比。

    取文件用 `git show`，但**基准不取 HEAD**：HEAD 会随提交漂移，那样这条用例
    只能证明"我和我自己一样"。提交号写死在 `LEGACY_COMMIT` 里。
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
        spec = importlib.util.spec_from_file_location("legacy_tools_901b484", path)
        module = importlib.util.module_from_spec(spec)
        # pydantic 在 exec 期间要能从 `sys.modules` 找到这个模块（dataclass 同理）
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
    return module._source_prompt_input


def test_full_mode_matches_the_901b484_module():
    """逐字节对比旧模块：哈希钉住的是"与那个提交相同"，这条钉住"同一条实现"。"""
    legacy = _legacy_renderer()
    if legacy is None:
        raise unittest.SkipTest(
            f"取不到 {LEGACY_COMMIT}:{LEGACY_PATH}（没有 git 或没有这个提交）；"
            "哈希钉的那条用例仍然有效")
    for name, sources in _fixtures().items():
        with _env(SOURCE_MODE_FULL):
            actual = _source_prompt_input(sources, system=SOURCED_DETAIL_PROMPT)
        expected = legacy(sources, system=SOURCED_DETAIL_PROMPT)
        assert actual == expected, f"{name}: full 形态与 {LEGACY_COMMIT} 不再逐字节一致"


# ---------------------------------------------------------------- 三、清单的边界


def test_manifest_truncation_drops_items_never_the_text():
    """60 份材料：清单被截断，但任何一份的正文都不在 prompt 里。

    这是"清单也要有上限"与"绝不截断正文拼进去"两条要求的交叉点：截断发生在
    **清单项**上，正文从头到尾没有进入过 prompt，所以不存在"截一半"的中间态。
    """
    sources = [
        Source(id=f"m{index}", kind=SourceKind.text, status="parsed",
               filename=f"材料{index}.txt", text=f"第 {index} 份的正文 ZZQX_{index}")
        for index in range(60)
    ]
    rendered = _material_manifest(sources)

    listed = [line for line in rendered.splitlines() if line[:1].isdigit()]
    assert len(listed) == MATERIAL_MANIFEST_MAX_ITEMS
    assert f"另有 {len(sources) - MATERIAL_MANIFEST_MAX_ITEMS} 份材料未在清单里列出" in rendered
    # 一个标记都不许出现：列出来的、没列出来的，正文都一样不进 prompt
    for index in range(60):
        assert f"ZZQX_{index}" not in rendered


def test_manifest_respects_its_own_token_ceiling():
    """条数没到上限、但每条都很长时，截断由 token 上限触发。

    两个上限都要有用：只按条数截，50 条 80 字的标题能把清单撑到 3000 token，
    清单就从一个"索引"变成了又一份材料。
    """
    long_name = "很长的文件名" * 20          # 120 字，会被夹到 80 字 + 省略号
    sources = [
        Source(id=f"t{index}", kind=SourceKind.text, status="parsed",
               filename=f"{long_name}{index}.txt", text="正文" * 50)
        for index in range(50)
    ]
    rendered = _material_manifest(sources)

    listed = [line for line in rendered.splitlines() if line[:1].isdigit()]
    assert 0 < len(listed) < MATERIAL_MANIFEST_MAX_ITEMS
    assert "另有" in rendered
    # 清单项本身必须落在上限内（尾注与"另有"那行是上限之后追加的，留一点余量）
    assert estimate_input_tokens(rendered) <= MATERIAL_MANIFEST_TOKEN_LIMIT + 200


# ---------------------------------------------------------------- 四、提示词与接线


def test_rag_prompt_carries_the_citation_protocol():
    """`rag` 那一段必须把"内容可以展开、出处不许编"说清，且不再要求模型自行检索。

    材料里带着主题词（"整理这些材料"），所以这次会命中、走的是 `rag` 提示词这条
    路。检索为空时 system 会退回 `DETAIL_PROMPT`（用户至少拿到一张图，见
    `tests/test_generate_evidence.py`），那条路上这些规程一句都不成立——不设成
    能命中的材料，这条用例测的就不再是它名字里那句话。

    这一轮改掉了三条旧规则，逐条钉住它们**没有回来**：
      - "检索不到就不要写…空着是正确结果" → 改成按知识展开、不挂引用；
      - "写任何与材料有关的内容之前，先检索" → 整树这条路已经做过预检索
        （`tools._retrieve_topic`），再让模型自行检索是重复的；
      - "可以多次检索…分开问" → 同上，模型手里已经有命中，再查拿不到新东西。
    """
    source = _material(f"整理这些材料的说明：正文 {MARKER}")
    with _env(SOURCE_MODE_RAG):
        system, user = _generate_prompt([source])

    assert EVIDENCE_HEADER in user          # 前提：这次真的命中了
    assert "看不到材料全文" in system
    assert "引用必须来自 evidence" in system
    assert "检索不到就按自己的知识展开" in system
    assert "未检索到不等于材料里没有" in system
    # 规程要成组出现：缺一条都不行，所以逐条钉关键动作
    for phrase in ("逐字", "source_id", "不要自己算", "evidence_quote"):
        assert phrase in system, phrase
    # 旧规则必须真的没了：留在提示词里就是两套契约打架，模型只听更保守的那套
    assert "空着是正确结果" not in system
    assert "检索不到就不要写" not in system
    assert "写任何与材料有关的内容之前" not in system
    assert "可以多次检索" not in system
    assert "rag_query" not in system
    # `full` 的提示词教模型读 `sources` 数组；`rag` 下那个数组不存在，
    # 一个字都不能混进来，否则模型会去找一个没有的字段。
    assert "sources 数组" not in system
    # 有命中时末尾多一句"这次不用再检索"：`RAG_SOURCE_PROMPT` 说正文没进来，
    # 而那一版输入末尾已经有检索结果，不点破这层提示词与输入互相矛盾。
    assert system == (RAG_SOURCE_PROMPT + thinking_block("default") + RAG_THINKING_GUARD
                      + GENERATE_EVIDENCE_RULES)


def test_audio_material_is_manifest_only():
    """音频材料同样走清单 + 命中：`sentences` 一个字都不进 prompt。

    主题取"录音转写正文"，与转写正文共享词项——这样命中段才是这段音频的原文。
    `sentences`（带时间戳的分句）不在 `_citable_hits` 的取材范围内（它只用
    `Source.text`），所以"分句永远不进 prompt"这条断言与命中无关。
    """
    audio = Source(
        id="audio-1", kind=SourceKind.audio, status="parsed", filename="会议纪要.mp3",
        text="录音转写正文。",
        duration_seconds=192.0,
        sentences=[AudioSentence(text="第一句转写 ZZQX_SENTENCE", begin_ms=0, end_ms=900)],
    )
    with _env(SOURCE_MODE_RAG):
        system, user = _generate_prompt([audio], topic="录音转写正文")

    manifest, evidence = _split_evidence(user)
    assert evidence                          # 前提：这次命中了
    assert "ZZQX_SENTENCE" not in system + user
    assert "录音转写正文" not in manifest      # 清单里没有正文
    assert "会议纪要" in user
    assert "时长 3:12" in user       # 规模要说清楚，否则模型不知道这段录音值不值得查
    assert "mp3" in user


def test_invalid_mode_falls_back_to_rag_and_warns_once():
    """非法取值按 `rag` 处理，且只告警一次。

    告警一次不是洁癖：`source_mode()` 每拼一次提示词就调一次，一次请求十几次，
    逐次告警会把日志刷满，把真正要看的东西埋掉。
    """
    _WARNED_SOURCE_MODES.discard("zz-not-a-mode")
    source = _material(f"正文 {MARKER}")
    with patch.dict(os.environ, {SOURCE_MODE_ENV: "zz-not-a-mode"}):
        with unittest.TestCase().assertLogs("app.generation.service", level="WARNING") as logs:
            assert source_mode() == SOURCE_MODE_RAG
            assert source_mode() == SOURCE_MODE_RAG      # 第二次不再刷日志
        system, user = _generate_prompt([source])

    assert len(logs.output) == 1
    assert "zz-not-a-mode" in logs.output[0]
    assert MARKER not in system + user


def test_registry_hands_create_the_generation_tools_by_mode():
    """`rag` 下 create 拿到的是**生成工具**，不是检索工具。

    早先这里是反的：那时整树生成不自己检索，不给 `rag_query` 模型就只能凭
    材料标题编。补上主题预检索之后结论翻转——两条生成路都在工具内部先查一次
    （整树预检索、分片按支检索），再给检索工具反而让模型连查八次把
    `max_tool_calls` 用光、一次生成都没调（实测 0 个节点 + "工具调用次数已用完"）。

    `full` 下 `rag_query` 同样不给（正文已在 prompt，给检索入口只会诱导它把
    "先查一下"当规定动作）；`mindmap_generate_deep` 在 `full` 下仍撤掉——分片
    检索是白花，而它按主题自由展开的树与材料脱钩却会 ok=True 返回。
    """
    with _env(SOURCE_MODE_RAG):
        names = build_registry("create", has_sources=True).names()
    assert "rag_query" not in names                  # 生成工具内部已预检索
    assert "mindmap_generate" in names
    assert "mindmap_generate_deep" in names          # 分片 + 按支检索

    with _env(SOURCE_MODE_FULL):
        names = build_registry("create", has_sources=True).names()
    assert "rag_query" not in names
    assert "mindmap_generate_deep" not in names      # 形态决定，`full` 下不变


def test_loop_prompt_does_not_forbid_retrieval_in_rag_mode():
    """外层循环的 system 不能在 `rag` 下继续说"不要先去检索资料"。

    两处提示词打架时模型听更具体的那句：一边"看不到全文必须先查"，一边"不要先查"，
    结果是它既不查也写不出，交出一棵空心树。
    """
    source = _material("正文")
    with _env(SOURCE_MODE_RAG):
        text = _system_text(_ctx([source]).request, None)
    assert _RAG_INTENT_RULES["create"] in text
    assert _INTENT_RULES["create"] not in text

    with _env(SOURCE_MODE_FULL):
        text = _system_text(_ctx([source]).request, None)
    assert _INTENT_RULES["create"] in text


def test_blank_source_is_left_out_of_the_manifest():
    """没正文的来源不进清单，也不进份数——它没有内容可查。"""
    blank = Source(id="blank-1", kind=SourceKind.text, status="pending", text=None)
    usable = _material("有正文的一段材料。", source_id="ok-1")
    with _env(SOURCE_MODE_RAG):
        _, user = _generate_prompt([blank, usable])

    assert "本次附带 1 份材料" in user
    assert blank.id not in user
    assert usable.id in user


# ---------------------------------------------------------------- 五、整条链路


class _ScriptedProvider:
    """按脚本依次返回预设 Completion，并记下每轮模型看到的 system。"""

    def __init__(self, script: list[Completion]) -> None:
        self._script = list(script)
        self.systems: list[str] = []

    async def complete_tools(self, messages, tools, *, budget):
        if not self._script:
            raise AssertionError("脚本已用完，循环比预期多调了一次模型")
        self.systems.append(messages[0]["content"])
        return self._script.pop(0)


def _completion(calls: list[ToolCall] | None = None, finish: str = "stop") -> Completion:
    calls = calls or []
    message: dict = {"role": "assistant", "content": "好的"}
    if calls:
        message["tool_calls"] = [
            {"id": call.call_id, "type": "function",
             "function": {"name": call.name,
                          "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
            for call in calls
        ]
    return Completion(message=message, tool_calls=calls, finish_reason=finish,
                      usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


def test_loop_retrieves_first_then_generates_from_hits():
    """整条链路：`rag_query` 真的能查到 → 生成时清单 + 自己那次检索的命中 → 引用逐字来自命中。

    这才是这次改动要证明的东西。"正文不进 prompt"本身不构成能力——变化在于
    **检索成了入口**：模型必须先查，查到什么才有什么，引用也只能从命中里抄。
    只测 `_source_prompt_input` 的字符串形状测不出这条链有没有接上（比如
    `rag_query` 忘了按形态注册给 `create`，或者循环的 system 还在说"别先检索"）。

    "生成时只带清单"这句话在 `_generate` 补上自己那次检索之后不再成立：清单之外
    还带着 `mindmap_generate` 对主题查到的命中（否则模型手里一个字都没有，只能
    交一条"没检索到"的分支——见 `tests/test_generate_evidence.py`）。所以这里
    断言的是**两段**都在，而不是"只有清单"。
    """
    text = ("第一章 总则。本制度适用于全部预算事项。\n"
            "第八条 预算由财务经理核准后执行，超过五十万元报总经理审批。\n"
            "第九条 每季度末做一次预算执行分析。")
    source = _material(text, filename="预算管理办法.docx", source_id="doc-1")

    # 先算一次真实命中：`_generate` 的主题预检索走 `search_parent`（无网络，纯 BM25）。
    hits = search_parent([source], "预算由谁核准", 5)
    assert hits, "样本必须能命中，否则这条用例证明不了引用来自检索"
    quote = hits[0].quote[:200]

    provider = _ScriptedProvider([
        _completion([ToolCall(call_id="c-gen", name="mindmap_generate",
                              arguments={"topic": "整理预算管理办法"})], finish="tool_calls"),
        _completion(),
    ])
    prompts: list[tuple[str, str]] = []

    async def chat(system, user, *, max_tokens, on_delta=None):
        prompts.append((system, user))
        tree = json.loads(json.dumps(TREE))
        # 逐字回填检索到的那一段：`rag` 形态要求引用只能这么来。
        tree["root"]["source_refs"] = [{"source_id": "doc-1", "quote": quote}]
        if on_delta is not None:      # `run()` 这条入口会带流式预览回调
            await on_delta(json.dumps(tree, ensure_ascii=False))
        return tree

    async def drain():
        return [event async for event in run(_ctx([source]).request, provider=provider)]

    with _env(SOURCE_MODE_RAG), patch("app.agent.tools.chat_async", new=chat):
        events = asyncio.run(drain())

    # 第一轮的 system 里是"先检索"，不是 `full` 形态里那句"不要先去检索资料"
    assert _RAG_INTENT_RULES["create"] in provider.systems[0]
    # `rag_query` 不在注册表里：生成工具自己预检索（模型再查只会烧额度）
    assert not [event for event in events
                if event.type == "tool_end" and event.name == "rag_query"]
    gen_ends = [event for event in events
                if event.type == "tool_end" and event.name == "mindmap_generate"]
    assert gen_ends and gen_ends[0].ok
    # 预检索真有命中（不然下面的引用无从谈起）
    assert gen_ends[0].result_summary["retrieval"]["hits"], gen_ends[0].result_summary

    system, user = prompts[0]
    assert system.startswith(RAG_SOURCE_PROMPT)
    assert "本次附带 1 份材料" in user
    # 主题是"整理预算管理办法"，与正文里的"预算"共享词项：这一次检索必有命中，
    # 正文因此出现在命中段里（清单里仍然没有，断言在上面那条用例里）。
    manifest, evidence = _split_evidence(user)
    assert evidence, "主题与材料有共同词项，`mindmap_generate` 自己那次检索必须命中"
    assert "预算由财务经理核准" in evidence
    assert "预算由财务经理核准" not in manifest

    done = [event for event in events if event.type == "done"]
    assert done and done[-1].content is not None, [event.type for event in events]
    refs = done[-1].content.root.source_refs
    # 逐字校验通过 = 这条引用确实能在原文里找到（它也确实来自检索结果）
    assert refs and refs[0].quote == quote


# ---------------------------------------------------------------- 六、解析层的预检


def test_material_over_the_context_is_rejected_in_full_and_accepted_in_rag():
    """同一份超大材料：`full` 下 422，`rag` 下放行。

    这是这次改动在**请求解析层**的收尾。留着旧的 token 预检不是"保守"，是错：
    `rag` 形态下正文不进 prompt，一份 25 万字的材料只贡献一行清单（几十 token），
    却会在开流之前被拒掉——而它本来是能处理的。

    断言成对出现，因为单独任一半都证明不了"差别是形态造成的"：只断言 rag 放行，
    可能只是阈值被调大了；只断言 full 拒绝，可能只是这条路根本没被改。
    """
    text = "字" * int(CONTEXT_TOKEN_LIMIT * 1.6)
    source = Source(kind=SourceKind.text, status="parsed", text=text)

    with _env(SOURCE_MODE_FULL):
        with pytest.raises(ValidationError) as raised:
            AgentRunRequest(goal="整理材料", sources=[source])
    assert "超过模型上下文上限" in str(raised.value)
    assert "请精简后重试，或分成多次生成" in str(raised.value)

    with _env(SOURCE_MODE_RAG):
        request = AgentRunRequest(goal="整理材料", sources=[source])
    assert len(request.sources) == 1
    # 放行的理由要落地：这份材料进 prompt 的部分只有一行清单，几十个 token。
    manifest = _material_manifest(request.sources)
    assert estimate_input_tokens(manifest) < 100
    assert "字" * 10 not in manifest          # 正文一个字节都没有


def test_too_many_materials_explain_a_reason_that_matches_the_mode():
    """份数上限两种形态都拦得住，但"为什么"不同，文案不能共用一句。

    `rag` 下卡住的是清单（列不下的材料模型连存在都不知道），`full` 下卡住的是
    上下文。共用一句"材料太多"会让用户按错误的办法处理：`rag` 形态下他去精简
    正文是白费力气，真正要做的是分批。
    """
    sources = [Source(id=f"n{index}", kind=SourceKind.text, status="parsed", text="正文")
               for index in range(MAX_SOURCES + 1)]

    with _env(SOURCE_MODE_RAG):
        with pytest.raises(ValidationError) as rag_raised:
            AgentRunRequest(goal="整理材料", sources=sources)
    with _env(SOURCE_MODE_FULL):
        with pytest.raises(ValidationError) as full_raised:
            AgentRunRequest(goal="整理材料", sources=sources)

    rag_message, full_message = str(rag_raised.value), str(full_raised.value)
    both = f"超过单次请求上限 {MAX_SOURCES} 份"
    assert both in rag_message and both in full_message
    # 分叉点在"为什么"上：只有 rag 那句要提到清单，也只有它不该提上下文上限
    assert "清单" in rag_message
    assert "清单" not in full_message
    for message in (rag_message, full_message):
        # 份数超限与体积超限是两件事，不能混成一句话
        assert "超过模型上下文上限" not in message
