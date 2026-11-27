"""节点两层：短标题（`label`）+ 长详情（`detail`）。

这一轮要解决的不是"画布放不下"，而是**生成时没有把长内容写到该去的地方**：
`Node` 契约里早就有 `note` 与 `source_refs`，画布只是把长 `label` 截断显示。所以
缺的两件事是：

  1. 生成侧把 20–30 字的完整句子塞进 `label`，没有一个字段承载"说明"；
  2. 画布与侧栏都不认识"这是详情"，只能回退到标题。

`detail` 与 `note` 的**语义必须分开**（这也是不复用 `note` 的唯一理由）：

  - `detail`：生成产出。整树生成、分片展开、单节点展开都会填它；
  - `note`：用户自己写的备注。只有界面上的编辑框会动它。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.ops import apply_ops
from app.contracts.mindmap import (
    GENERATED_LABEL_MAX_CHARS,
    MAX_NODE_DETAIL_CHARS,
    MAX_NODE_LABEL_CHARS,
    MindMapContent,
    Node,
    clip_label,
)
from app.contracts.ops import OpBatch
from app.generation.service import validate_generated_content


def _content(**node_extra) -> dict:
    return {
        "title": "测试",
        "root": {
            "id": "r", "label": "根", "children": [], **node_extra,
        },
    }


# ---------------------------------------------------------------- 1. 契约


def test_detail_is_optional_and_defaults_to_none():
    """旧图没有这个字段：读进来必须是 `None`，而不是空串或异常。"""
    parsed = MindMapContent.model_validate(_content())
    assert parsed.root.detail is None
    assert parsed.root.note is None


def test_detail_is_bounded_and_kept():
    parsed = MindMapContent.model_validate(_content(detail="说明" * 1000))
    assert len(parsed.root.detail) == MAX_NODE_DETAIL_CHARS
    with pytest.raises(ValidationError):
        MindMapContent.model_validate(_content(detail="说明" * 1001))


def test_blank_detail_becomes_none():
    """纯空白等于没写：留空串会让前端多一个"有正文但显示为空"的分支。"""
    parsed = MindMapContent.model_validate(_content(detail="  \n "))
    assert parsed.root.detail is None


def test_detail_and_note_are_independent():
    """两个字段互不覆盖——混用会让用户的备注被重新生成盖掉。"""
    parsed = MindMapContent.model_validate(
        _content(detail="生成写的说明", note="我自己写的备注"))
    assert parsed.root.detail == "生成写的说明"
    assert parsed.root.note == "我自己写的备注"
    # 只有 detail 时 note 仍是 None：生成侧不许借道 note
    only_detail = MindMapContent.model_validate(_content(detail="只有详情"))
    assert only_detail.root.note is None


def test_old_long_label_is_still_readable():
    """旧图的长标题照常读：契约上限仍是 200，只有生成侧受 16 字约束。"""
    long_label = "字" * MAX_NODE_LABEL_CHARS
    parsed = MindMapContent.model_validate(_content(label=long_label))
    assert len(parsed.root.label) == MAX_NODE_LABEL_CHARS
    with pytest.raises(ValidationError):
        MindMapContent.model_validate(_content(label="字" * (MAX_NODE_LABEL_CHARS + 1)))


def test_generated_label_limit_is_shorter_than_the_contract():
    """16 是生成口径，200 是读取口径——两个数必须都在，且前者更小。"""
    assert GENERATED_LABEL_MAX_CHARS == 16
    assert MAX_NODE_LABEL_CHARS == 200


def test_clip_label_counts_code_points():
    """按码点截断：emoji 不该被切成半个，也不该被算成两个字。"""
    assert clip_label("一二三四五六七八九十一二三四五六七八") == "一二三四五六七八九十一二三四五六"
    assert len(clip_label("字" * 100)) == GENERATED_LABEL_MAX_CHARS
    # 单个 emoji 就是一个码点，两个不会被切成半个
    assert clip_label("🚀🚀🚀", 2) == "🚀🚀"
    # 码点口径**不做字形簇切分**：ZWJ 组合（🧑‍🚀 = 4 个码点）会被切开。这是
    # 刻意的取舍——做字形簇切分要引 ICU 的分段器，而前端的 `Array.from` 也是
    # 码点口径，两端用不同的切法只会让"同一个标题在两端被截得不一样"。
    assert len(clip_label("🧑‍🚀" * 5, 4)) == 4
    assert clip_label("短标题") == "短标题"
    assert clip_label("") == ""


# ------------------------------------------------- 一·二 切点不落在半个 token 上


def test_clip_label_does_not_cut_a_token_in_half():
    """切在半个 token 上时往前退：`#21` 不是 `#218` 的缩写，是**另一个东西**。

    实测（`scripts/measure_label_clip.py`，20 棵验收树 / 1130 节点）里 16 字 label
    有 10 条是这么撞出来的：`Kru`←Kruskal、`VLA`←VLAN、`BG`←BGP、`P`←PNG、
    `W`←Word、`Pos`←PostgreSQL（三处）、`Eleme`←Element Plus、`21`←21ms、`#21`←#218。
    前八个只是"读不通"，最后两个是**说错事**——`#21` 与 `#218` 指的是两个不同的
    issue，`21` 与 `21ms` 差三个数量级。长度是"排不排得进盒子"，切点是"切开之后
    还是不是同一个东西"，后者更硬。
    """
    # 两个说错事的
    assert clip_label("错位问题已提 issue #218") == "错位问题已提 issue"
    assert clip_label("平均查询 180ms 降到 21ms") == "平均查询 180ms 降到"
    # 六个读不通的
    assert clip_label("路由协议 RIP OSPF BGP") == "路由协议 RIP OSPF"
    assert clip_label("CSMA/CD 与交换机 VLAN") == "CSMA/CD 与交换机"
    assert clip_label("最小生成树 Prim 与 Kruskal") == "最小生成树 Prim 与"
    assert clip_label("导入 docx 生成导图导出 PNG") == "导入 docx 生成导图导出"
    assert clip_label("后端 Node.js + PostgreSQL") == "后端 Node.js"
    assert clip_label("前端 Vue 3 + Element Plus") == "前端 Vue 3"


def test_clip_label_strips_what_the_retreat_left_dangling():
    """退到 token 之前以后，末尾的空白 / `/+|` / 悬空运算符要抹掉。

    不抹的话 `导出 PNG / PDF / W` 会退化成 `导出 PNG / PDF /`——末尾一个孤零零的
    斜杠，读起来像被截了两次；`精确完成率≥90%` 会退化成 `…完成率≥`。
    """
    assert clip_label("导出 PNG / PDF / Word") == "导出 PNG / PDF"
    assert clip_label("90 次局部请求精确完成率≥90%") == "90 次局部请求精确完成率"
    # `、`/`,`/`:` 不抹：它们留在末尾是"这里还有下文"的可见标记
    assert clip_label("(user_id, created_at)") == "(user_id,"


def test_clip_label_does_not_pretend_to_know_chinese_word_boundaries():
    """已知边界：中文词内切**不**修，钉住这个取舍。

    `与归一` 后面的"化"没了（原文"归一化"），`服务器` 本来就是完整词。两者在
    机制上不可区分——中文没有词边界符，要判它就得引词表，而词表会把模型新造的
    术语判错。为了少几个不好看的切点引入一类新故障，方向是反的。
    """
    assert clip_label("训练技巧：dropout 与归一化") == "训练技巧：dropout 与归一"
    assert clip_label("部署在校内 2 核 4G 服务器上") == "部署在校内 2 核 4G 服务器"


def test_clip_label_leaves_half_quotes_alone():
    """引用配平**不**修，同样钉住：两个方向的代价都比"留着半个括号"大。

    退到开符号之前会整块丢掉引用里的内容（`Silberschatz《操作系统概念》` →
    `Silberschatz`）；而开符号落在第 0 个码点时退无可退（`(user_id, created_at)`）。
    半个括号至少还读得出原意，还给了"这里被截过"的可见提示。
    """
    assert clip_label("Silberschatz《操作系统概念》") == "Silberschatz《操作系"
    assert clip_label("第 3 章 数据链路层（6 学时）") == "第 3 章 数据链路层（6 学时"


def test_clip_label_never_empties_a_single_token_label():
    """整条就是一个 token 时放弃回退：清空标题比切一刀更糟。

    `ensure_finish_reason` 是实测里真出现过的那种 label（主库 `mindmaps.db` 的
    一棵图里就是 `ensure_finish_re`）。它整条是 token，退无可退，所以仍然切在
    中间——这是这条规则的极限，账写在 `docs/LABEL-CLIP-VERDICT.md`。
    """
    assert clip_label("abcdefghijklmnopqrstuvwxyz") == "abcdefghijklmnop"
    assert clip_label("ensure_finish_reason 的兜底") == "ensure_finish_re"


@pytest.mark.parametrize("text", [
    "路由协议 RIP OSPF BGP",
    "错位问题已提 issue #218",
    "平均查询 180ms 降到 21ms",
    "导出 PNG / PDF / Word",
    "90 次局部请求精确完成率≥90%",
    "导入 docx 生成导图导出 PNG",
    "后端 FastAPI 加 PostgreSQL",
    "CSMA/CD 与交换机 VLAN",
    "最小生成树 Prim 与 Kruskal",
    "The quick brown fox jumps over the lazy dog",
    "(user_id, created_at, deleted_at)",
])
def test_clip_label_output_never_ends_inside_a_token(text):
    """性质：产出撞上限时，切点两侧不会同属一个 token。

    判据与 `scripts/measure_label_clip.py` 同一套（`_IDENT_CHARS`），所以这条性质
    和那份测量说的是同一件事——测量报"库里还剩几条"，这条钉"以后不再新增"。
    """
    result = clip_label(text)
    assert len(result) <= GENERATED_LABEL_MAX_CHARS
    if len(text) <= GENERATED_LABEL_MAX_CHARS:
        assert result == text
        return
    ident = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._+#%<>=-")
    cut = len(result)
    assert cut < len(text)
    assert not (text[cut - 1] in ident and text[cut] in ident), (
        f"{text!r} 切在 token 中间：{result!r}"
    )


# ---------------------------------------------------------------- 2. ops 携带 detail


def _batch(*ops) -> OpBatch:
    return OpBatch(batch_id="b", base_revision=0, ops=list(ops))


def test_add_op_carries_detail():
    content = MindMapContent.model_validate(_content())
    result = apply_ops(content, _batch({
        "op": "add", "id": "n1", "parent_id": "r", "index": 0,
        "label": "新节点", "detail": "这是它的说明",
    }))
    assert result.root.children[0].detail == "这是它的说明"
    assert result.root.children[0].note is None


def test_update_op_can_set_and_clear_detail():
    content = MindMapContent.model_validate({
        "title": "t",
        "root": {"id": "r", "label": "根", "children": [
            {"id": "a", "label": "甲", "detail": "旧说明", "note": "用户备注", "children": []},
        ]},
    })
    updated = apply_ops(content, _batch({
        "op": "update", "id": "a", "changes": {"detail": "新说明"},
    }))
    assert updated.root.children[0].detail == "新说明"
    # 更新 detail 不碰 note
    assert updated.root.children[0].note == "用户备注"

    cleared = apply_ops(updated, _batch({
        "op": "update", "id": "a", "changes": {"detail": ""},
    }))
    assert cleared.root.children[0].detail is None


def test_update_can_change_only_detail():
    """`at_least_one` 要把 detail 算进去，否则"只改详情"会被判成空 changes。"""
    content = MindMapContent.model_validate(_content())
    result = apply_ops(content, _batch({
        "op": "update", "id": "r", "changes": {"detail": "根也有说明"},
    }))
    assert result.root.detail == "根也有说明"


def test_regenerating_never_touches_the_user_note():
    """生成路径（add）写 detail、不写 note：用户的备注必须活过重新生成。"""
    content = MindMapContent.model_validate({
        "title": "t",
        "root": {"id": "r", "label": "根", "children": [
            {"id": "keep", "label": "老节点", "note": "我写的备注", "children": []},
        ]},
    })
    result = apply_ops(content, _batch(
        {"op": "add", "id": "fresh", "parent_id": "r", "index": 1,
         "label": "新节点", "detail": "新说明"},
        {"op": "update", "id": "keep", "changes": {"label": "老节点（改名）",
                                                   "detail": "补了一段说明"}},
    ))
    kept = next(node for node in result.root.children if node.id == "keep")
    assert kept.note == "我写的备注"
    assert kept.detail == "补了一段说明"


# ---------------------------------------------------------------- 3. 生成路径的长度


def test_deep_generation_labels_are_short_and_carry_detail():
    """分片生成：模型给的长标题被裁到 16 字，长内容落进 detail。"""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
    from app.agent.tools import build_registry

    long_label = "把预算审批流程拆成申请与复核两段并写清责任"

    async def fake(system, user, *, max_tokens=None, on_delta=None):
        if "只定一级骨架" in system:
            return {"branches": [f"{long_label}{index}" for index in range(3)]}
        return {"children": [{"label": long_label, "detail": "3 个工作日内完成复核"}]}

    request = AgentRunRequest(
        goal="做一张导图", intent="create",
        budget=RunBudget(max_model_calls=10, max_output_tokens=32768),
    )
    ctx = RunContext(request=request, sources_by_id={})
    with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
        result = asyncio.run(build_registry("create_deep").execute(
            ToolCall(call_id="c", name="mindmap_generate_deep",
                     arguments={"topic": "预算管理办法", "branches": 3,
                                "points_per_branch": 2}),
            ctx,
        ))

    assert result.ok, result.content
    root = ctx.candidate.root
    assert len(root.label) <= GENERATED_LABEL_MAX_CHARS
    branch = root.children[0]
    assert len(branch.label) <= GENERATED_LABEL_MAX_CHARS
    point = branch.children[0]
    assert len(point.label) <= GENERATED_LABEL_MAX_CHARS
    # detail 原样落库——这正是"画布上短、点开看长文"的另一半
    assert point.detail == "3 个工作日内完成复核"


def test_single_node_expand_accepts_both_shapes():
    """`node_expand` 的新旧两种响应都要收：旧格式只有字符串，没有 detail。

    这条用例原先还顺带钉着"超长 label 被裁到 16 字"。**那一条已经反过来了**：
    单节点展开现在对 `label` 是严格的——超长判整批失败、树不变（理由见
    `tools._strict_label`），超长的那条改由 `test_expand_node.py` 的原子性用例
    覆盖。这里只留"两种形状都收"。
    """
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
    from app.agent.tools import build_registry

    content = MindMapContent.model_validate({
        "title": "t", "root": {"id": "r", "label": "根", "children": [
            {"id": "a", "label": "甲", "children": []}]},
    })

    async def new_shape(system, user, *, max_tokens=None):
        return {"children": [{"label": "要点的标题", "detail": "这里是说明"},
                             {"label": "另一个要点", "detail": "这里是另一段说明"}]}

    request = AgentRunRequest(
        goal="展开", intent="expand", selected_ids=["a"],
        budget=RunBudget(max_model_calls=5, max_output_tokens=32768),
    )
    ctx = RunContext(request=request, candidate=content, sources_by_id={})
    with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=new_shape)):
        result = asyncio.run(build_registry("expand").execute(
            ToolCall(call_id="c", name="node_expand",
                     arguments={"node_id": "a", "count": 2}),
            ctx,
        ))
    assert result.ok, result.content
    added = ctx.candidate.root.children[0].children[0]
    assert added.label == "要点的标题"
    assert added.detail == "这里是说明"

    async def old_shape(system, user, *, max_tokens=None):
        return {"points": ["旧格式的要点一"]}

    legacy_ctx = RunContext(request=request, candidate=content.model_copy(deep=True),
                            sources_by_id={})
    with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=old_shape)):
        legacy = asyncio.run(build_registry("expand").execute(
            ToolCall(call_id="c2", name="node_expand",
                     arguments={"node_id": "a", "count": 2}),
            legacy_ctx,
        ))
    assert legacy.ok, legacy.content
    node = legacy_ctx.candidate.root.children[0].children[0]
    assert node.label == "旧格式的要点一"
    assert node.detail is None


def test_root_label_is_clipped_too():
    """用户填的主题经常是整句指令，画在中心会撑满一屏。"""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
    from app.agent.tools import build_registry

    async def fake(system, user, *, max_tokens=None, on_delta=None):
        if "只定一级骨架" in system:
            return {"branches": ["一", "二", "三"]}
        return {"children": [{"label": "点一"}, {"label": "点二"}]}

    request = AgentRunRequest(
        goal="整理材料", intent="create",
        budget=RunBudget(max_model_calls=10, max_output_tokens=32768),
    )
    ctx = RunContext(request=request, sources_by_id={})
    with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
        result = asyncio.run(build_registry("create_deep").execute(
            ToolCall(call_id="c", name="mindmap_generate_deep",
                     arguments={"topic": "帮我整理这份预算管理办法的全部要点",
                                "branches": 3, "points_per_branch": 2}),
            ctx))
    assert result.ok, result.content
    assert len(ctx.candidate.root.label) <= GENERATED_LABEL_MAX_CHARS
    assert ctx.candidate.title == ctx.candidate.root.label


def test_whole_tree_payload_clips_labels_too():
    """整树形态的输入（dict）交给 `validate_generated_content` 时 label 同样被裁到 16 字。

    分片那条路在 `_slot_from` 里裁，整树这条在 `validate_generated_content` 里裁——
    两条路各裁一次、共用同一个契约常量。少裁任何一条，画布上就有一半的节点又变回
    "一句话标题"。这条用例原来跑在 `mindmap_generate` 上；那个工具已删除
    （`docs/ONE-PATH.md`），但"接受一整棵树作为模型输出"的入口函数还在，判据照旧。
    """
    long_label = "关于预算审批流程中申请与复核两段的责任划分说明"
    tree = {"title": "预算", "root": {"id": "r", "label": long_label, "children": [
        {"id": f"b{index}", "label": f"{long_label}{index}", "children": []}
        for index in range(3)
    ]}}

    content, diagnostics = validate_generated_content(tree)

    assert len(content.root.label) <= GENERATED_LABEL_MAX_CHARS
    for branch in content.root.children:
        assert len(branch.label) <= GENERATED_LABEL_MAX_CHARS
    assert "LABEL_CLIPPED_TO_GENERATED" in [item.code for item in diagnostics]


def test_node_model_rejects_unknown_fields():
    """`extra="forbid"` 仍是契约的一部分：`detail` 是显式字段，不是兜底口袋。"""
    with pytest.raises(ValidationError):
        Node.model_validate({"id": "x", "label": "标题", "details": "拼错的字段名"})
