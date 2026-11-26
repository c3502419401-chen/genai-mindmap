"""一级分支的两处"生成出来的东西真的不对"：跑题与判死。

两个缺陷的现状与根因见 `docs/OFF-TOPIC-AND-FEW-BRANCHES.md`，这里钉四件事：

  1. **目录提取器认得出真实中文材料的结构**（`一、标题` 紧贴正文也算），并且
     **不把正文列表项当章节**（`3. 半年内支持 20 个热门项目、100 位贡献者。`）。
     这两条是那 7 条会议纪要"生成成会议纪要怎么写"与 3 条项目立项"2 条分支被判死"
     的共同上游——骨架那一次看不到材料正文，只看得见目录与清单。
  2. **有材料时骨架拼了"材料是整理对象"这条判据**（`MATERIAL_AS_OBJECT_RULES`），
     没有材料时一个字都不加（纯主题那条路与改动之前逐字节相同）。
  3. **目录比 `MIN_BRANCHES` 窄时补一句"别被块数锁死"**（`OUTLINE_FEW_BLOCKS_NOTE`）。
  4. **少于 `MIN_BRANCHES` 时的三种结局**：有材料先带证据重问一次；重问仍少就交树
     + `BRANCHES_BELOW_MIN` 诊断；纯主题仍然判死——门槛没有被拆掉。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.agent import tools as tools_module
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    DETAIL_ONLY_PROMPT,
    MATERIAL_AS_OBJECT_RULES,
    OUTLINE_FEW_BLOCKS_NOTE,
    OUTLINE_HEADER,
    PLAN_PROMPT,
    build_registry,
)
from app.contracts.source import Source, SourceKind

TOPIC = "产品需求评审会"

# 真实材料的版式：`一、` 标题**紧贴正文**（中间没有空行），四块内容。
# 就是 `backend/scripts/acc_samples.json` 里那 13 条材料的形状。
# 字数必须过 `SHORT_MATERIAL_CHARS`（300）：更短的材料会被当成"主题补充说明"，
# 走的是另一条路（`tools._intake`），那测的就不是这里要测的东西了。
MATERIAL = (
    "产品需求评审会纪要\n"
    "时间：2026-11-18 14:00-15:40　地点：三楼小会议室　主持：李敏\n"
    "参会：李敏（产品）、王涛（后端）、张晓（前端）、陈立（测试）、赵雪（设计）\n"
    "一、本次评审范围\n"
    "新版导图编辑器的三个需求：节点拖拽改父级、多选批量改颜色、导出带水印 PDF。"
    "本期只做这三个，导出其他格式（XMind、Markdown）留到下一期；旧版编辑器的兼容"
    "问题不在本次范围内。\n"
    "二、讨论要点\n"
    "王涛提出拖拽要处理环路检测，张晓说画布已支持碰撞提示，结论是只允许同层或跨层"
    "移动到非后代节点，禁止跨图移动；赵雪给出色板方案（9 色 + 5 个预设），陈立要求"
    "补一条批量改色后撤销要一次撤回全部；孙鹏要求水印含团队名与导出时间，折中方案"
    "是水印可关闭但默认开启。\n"
    "三、结论\n"
    "三个需求全部进入本期开发，优先级：拖拽 > 批量改色 > 水印 PDF；验收标准按上面"
    "三条讨论结论逐条对，不额外加需求。\n"
    "四、待办\n"
    "王涛 11/21 前给出拖拽环路检测的技术方案；张晓 11/22 前提交多选状态的联调分支；"
    "赵雪 11/20 前出色板与预设的视觉稿；陈立 11/23 前补批量改色的撤销用例；"
    "孙鹏 11/25 前确定水印文案规范。\n"
)

SECTIONS = ("本次评审范围", "讨论要点", "结论", "待办")


def _request(sources: list[Source] | None = None) -> AgentRunRequest:
    return AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default",
        sources=sources or [],
        budget=RunBudget(max_model_calls=20, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=1),
    )


def _ctx(sources: list[Source] | None = None) -> RunContext:
    payload = sources or []
    return RunContext(
        request=_request(payload),
        sources_by_id={source.id: source for source in payload},
    )


def _material(text: str = MATERIAL, *, source_id: str = "s-minutes") -> Source:
    return Source(id=source_id, kind=SourceKind.document, status="parsed",
                  filename="产品需求评审会纪要.txt", text=text)


def _tree(branches: list[str]) -> dict:
    """模型的结构响应：每条一级分支挂**两个**要点。

    两个而不是一个：自动修复（`AUTO_REPAIR=1`）会"并单子"——只有独子的分支会被
    拍平（`SINGLE_CHILD`），于是交付出来的树少一层，那测的就不是这里要测的东西。
    """
    return {"title": TOPIC, "root": {
        "id": "root", "label": TOPIC, "detail": "中心主题", "children": [
            {"id": f"b{index}", "label": label, "children": [
                {"id": f"b{index}-1", "label": f"{label}·一", "detail": "一句说明。"},
                {"id": f"b{index}-2", "label": f"{label}·二", "detail": "另一句说明。"},
            ]}
            for index, label in enumerate(branches)
        ]}}


def _run(ctx: RunContext, chat, *, arguments: dict | None = None):
    args = {"topic": TOPIC}
    args |= arguments or {}
    registry = build_registry("create", has_sources=bool(ctx.request.sources),
                              has_library=False)
    with patch("app.agent.tools.chat_async", new=chat):
        result = asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate", arguments=args), ctx))
    return result, ctx


def _branches(ctx: RunContext) -> list[str]:
    """这一次生成出来的一级分支标题（`ctx.candidate` 是树上真正落下的东西）。"""
    assert ctx.candidate is not None
    return [child.label for child in ctx.candidate.root.children]


def _codes(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


def _plan_calls(seen: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [item for item in seen if item[0].startswith(PLAN_PROMPT)]


# ---------------------------------------------------------------- 1. 目录提取


def test_real_material_structure_is_visible_to_the_skeleton():
    """`一、标题` + 紧贴的正文 → 目录里就有这四块，骨架那一次看得见。

    这是缺陷一的直接上游：目录为空时骨架只拿到"主题：产品需求评审会"与文件名，
    它只能按"这类文档"的先验写「定义与作用 / 内容要素 / 写作要求 / 格式规范」。
    """
    seen: list[tuple[str, str]] = []

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(SECTIONS)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {}
        return _tree(list(SECTIONS))

    result, _ = _run(_ctx([_material()]), chat)

    assert result.ok, result.content
    plan_user = _plan_calls(seen)[0][1]
    assert OUTLINE_HEADER in plan_user
    for section in SECTIONS:
        assert section in plan_user
    # 目录窄于 MIN_BRANCHES 才有那句话；四条时不该出现。
    assert OUTLINE_FEW_BLOCKS_NOTE.format(count=4, minimum=3) not in plan_user


def test_narrow_outline_gets_a_note_instead_of_locking_the_tree():
    """只有两块章节时，目录仍然拼，但补一句"别只给两块"。

    材料真只有两块时，`OUTLINE_HEADER` 那句"请优先按它划分一级分支"会把树钉在
    2 条上，而 `MIN_BRANCHES = 3` 立刻判废——用户拿到的是错误卡片，不是一棵窄树。
    """
    narrow = (
        "实验室设备预约系统（立项）\n"
        "一、现状\n"
        "学院 12 个实验室、86 台设备目前靠 Excel 加微信群预约，冲突率高、审批靠口头，"
        "使用记录散落在个人笔记里；共聚焦显微镜这类紧俏设备经常出现同一时段被两个人"
        "预约成功的情况，事后只能靠人工协调，浪费大量时间。设备培训也没有记录，"
        "新人上手全靠老带新，同一台仪器被不同人用出不同故障，维修费用一年比一年高。\n"
        "\n"
        "二、目标\n"
        "预约冲突归零；审批与培训记录集中归档，教务处统计工作量从 3 天降到半天；"
        "设备使用率提升 20%，维修工单可追溯到具体使用者与预约时段；每月生成一份"
        "设备使用报告，供学院排课与采购决策参考。培训记录要与预约绑定：没通过培训"
        "的账号不能预约高价值设备，这一条由系统自动拦截，不再靠管理员人工核对。\n"
    )
    seen: list[tuple[str, str]] = []

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": ["现状", "目标", "冲突与改进"]}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {}
        return _tree(["现状", "目标", "冲突与改进"])

    result, _ = _run(_ctx([_material(narrow, source_id="s-narrow")]), chat)

    assert result.ok, result.content
    plan_user = _plan_calls(seen)[0][1]
    assert OUTLINE_HEADER in plan_user
    assert OUTLINE_FEW_BLOCKS_NOTE.format(count=2, minimum=3) in plan_user


# ---------------------------------------------------------------- 2. 材料是整理对象


def test_material_object_rule_is_in_the_plan_system_only_with_materials():
    """有材料时拼"材料是要整理的对象"；纯主题时一个字都不加。

    第一半防"会议纪要生成成怎么写"（材料是范文的读法），第二半防"多一句话把
    纯主题那条路的归纳挤走"——那一条路与改动之前必须逐字节相同。
    """
    seen: list[tuple[str, str]] = []

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(SECTIONS)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {}
        return _tree(list(SECTIONS))

    _run(_ctx([_material()]), chat)
    assert MATERIAL_AS_OBJECT_RULES in _plan_calls(seen)[0][0]

    seen.clear()
    _run(_ctx(), chat)
    assert MATERIAL_AS_OBJECT_RULES not in _plan_calls(seen)[0][0]


# ---------------------------------------------------------------- 3. 少于下限


def test_two_branches_with_material_is_retried_with_evidence():
    """第一次 2 条 → 带证据重问一次；第二次 6 条 → 出树。

    "带证据"指重问的 user 里出现了材料自带的章节：同材料同提示词再发一次是
    确定性同答案（验收里那三条就是这么死的），要它改只能换输入。
    """
    seen: list[tuple[str, str]] = []

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            if len(_plan_calls(seen)) == 1:
                return {"branches": list(SECTIONS[:2])}
            return {"branches": list(SECTIONS)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {}
        return _tree(list(SECTIONS))

    result, ctx = _run(_ctx([_material()]), chat)

    assert result.ok, result.content
    plans = _plan_calls(seen)
    assert len(plans) == 2, "少于下限只重问一次，不能变成重试风暴"
    retry_user = plans[1][1]
    assert "少于成图要求的 3 条" in retry_user
    for section in SECTIONS:
        assert section in retry_user
    assert _branches(ctx) == list(SECTIONS)
    assert "BRANCHES_BELOW_MIN" not in _codes(result)


def test_two_branches_twice_is_delivered_with_a_diagnostic():
    """两次都 2 条 → **交树** + 一条诊断，而不是错误卡片。

    `partial` 机制救不了"结构阶段就死"（它要求至少长出过一棵树），而一棵带引用的
    2 分支树用户能点开、能继续展开；错误卡片什么都做不了。质量门槛没被拆掉：
    这条诊断就是"这次结构偏窄"的记账。
    """
    seen: list[tuple[str, str]] = []

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(SECTIONS[:2])}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {}
        return _tree(list(SECTIONS[:2]))

    result, ctx = _run(_ctx([_material()]), chat)

    assert result.ok, result.content
    assert len(_plan_calls(seen)) == 2
    assert _branches(ctx) == list(SECTIONS[:2])
    assert "BRANCHES_BELOW_MIN" in _codes(result)


def test_two_branches_without_material_still_fails():
    """纯主题 2 条仍然判死：没有材料时"2 条"就是模型没话说，门槛照守。

    与上一条成对：**降级交付只发生在有材料的情形**。这个边界一旦松，(材料, 2 条)
    与 (无材料, 2 条) 会共用一条出口，而它们的原因完全不同——前者是"材料只讲了
    两件事"，后者是"模型什么都没想出来"。
    """
    seen: list[tuple[str, str]] = []

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        seen.append((system, user))
        return {"branches": ["甲", "乙"]}

    result, _ = _run(_ctx(), chat)

    assert not result.ok
    assert result.data["code"] == "too_few_branches"
    assert len(_plan_calls(seen)) == 1, "无材料不重问：没有材料证据可以换"
