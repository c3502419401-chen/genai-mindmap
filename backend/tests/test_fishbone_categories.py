"""鱼骨图的类别可变（`MINDMAP_FISHBONE_FREE_CATEGORIES`，默认关）。

三件事用用例钉住，一件一组：

1. **关着时逐字节不变**：`fishbone_plan_prompt()` 返回的就是 `FISHBONE_PLAN_PROMPT`
   本身，`fishbone_tree_rules()` 返回的就是 `FISHBONE_TREE_RULES` 本身；整份文本的
   sha256 与 `a290555` 那一版相同。这是"能回滚"的证据，不是注释里的一句承诺。
2. **开着时那三条判据真的在提示词里**：变量/领域的界线、四个成因面的覆盖面、先写
   problem 再定类别。缺一条，模型就只剩"随便起六个名字"。
3. **用户点名压过模型自定**：`人机料法环测` / `5M1E` / `4M1E` 能从主题里认出来并写进
   user 末尾；认不出来时**一个字都不加**（猜一套类别比让模型自己定更糟）。
"""
from __future__ import annotations

import hashlib

import pytest

from app.agent.tools import (
    FISHBONE_CATEGORIES,
    FISHBONE_FREE_ENV,
    FISHBONE_FREE_PLAN_PROMPT,
    FISHBONE_FREE_TREE_RULES,
    FISHBONE_PLAN_PROMPT,
    FISHBONE_TREE_RULES,
    _fishbone_shape_diagnostics,
    _plan_ask,
    fishbone_free_categories_enabled,
    fishbone_plan_prompt,
    fishbone_tree_rules,
    named_fishbone_categories,
)
from app.contracts.diagnostic import DiagnosticLevel
from app.contracts.mindmap import FishboneBone, FishboneContent, Node

# `a290555` 那一版的整份文本。关着时必须回到这两个值——改一个字这里就红。
PLAN_SHA256 = "72c9d6f5a25a24da5cd661a11c069e5fb4932c2b77722c832cc5a98b16277838"
TREE_RULES_SHA256 = "c763ff56d0cb86133f4676672f5c10406bd3e2a7a6614381adcc9e057e9fd4b2"


@pytest.fixture
def free_on(monkeypatch):
    monkeypatch.setenv(FISHBONE_FREE_ENV, "1")


@pytest.fixture
def free_off(monkeypatch):
    monkeypatch.delenv(FISHBONE_FREE_ENV, raising=False)


def _bone(category: str, causes: tuple[str, ...] = ()) -> FishboneBone:
    """一根骨。`Node.id` 是必填的（契约里它是 `source_refs` 的挂载点）。"""
    return FishboneBone(
        category=category,
        root=Node(id=f"b_{category}", label=category, children=[
            Node(id=f"c_{category}_{index}", label=cause)
            for index, cause in enumerate(causes)]),
    )


class TestSwitchOffIsByteIdentical:
    """关着时一个字都不动。"""

    def test_plan_prompt_sha256(self, free_off):
        assert not fishbone_free_categories_enabled()
        assert fishbone_plan_prompt() is FISHBONE_PLAN_PROMPT
        assert (hashlib.sha256(fishbone_plan_prompt().encode("utf-8")).hexdigest()
                == PLAN_SHA256)

    def test_tree_rules_sha256(self, free_off):
        assert fishbone_tree_rules() is FISHBONE_TREE_RULES
        assert (hashlib.sha256(fishbone_tree_rules().encode("utf-8")).hexdigest()
                == TREE_RULES_SHA256)

    def test_plan_ask_keeps_the_six_categories(self, free_off):
        ask = _plan_ask("为什么设备停机降不下来", diagram_type="fishbone")
        assert ("这次是鱼骨图：一级分支固定为人 / 机 / 料 / 法 / 环 / 测 六条，"
                "不要输出 edges；") in ask
        assert "成因面" not in ask

    def test_diagnostics_still_report_the_missing_categories(self, free_off):
        content = FishboneContent(
            title="协作", problem="为什么协作效率低",
            bones=[_bone("人"), _bone("机")])
        diagnostics: list = []
        _fishbone_shape_diagnostics(content, diagnostics)
        assert [item.code for item in diagnostics] == ["FISHBONE_CATEGORIES_INCOMPLETE"]
        assert diagnostics[0].level is DiagnosticLevel.warn


class TestSwitchOnChangesTheJudgement:
    """开着时：判据在，六类那句话不在。"""

    def test_plan_prompt_carries_the_three_rules(self, free_on):
        prompt = fishbone_plan_prompt()
        assert prompt is FISHBONE_FREE_PLAN_PROMPT
        # ① 变量 / 领域这条界线（类别名本身判不了，只有这条判得了）
        assert "变量还是领域" in prompt
        # ② 覆盖面：四个成因面至少三个
        assert "至少覆盖三个" in prompt
        assert "谁来做" in prompt and "怎么做" in prompt and "在哪做" in prompt
        # ③ 顺序：先 problem 再类别
        assert "先写 problem，再定一级分支" in prompt
        # 反例必须在：现行提示词就是靠正反例工作的
        assert "人员管理" in prompt and "沟通不畅" in prompt

    def test_plan_prompt_drops_the_fixed_categories(self, free_on):
        prompt = fishbone_plan_prompt()
        assert "必须正好是这六个类别" not in prompt
        assert "一级分支**必须正好是这六个类别**" not in prompt
        # 3–6 的下界仍在：少于 3 条 `MIN_BRANCHES` 会让整次生成判废
        assert "3 到 6 个" in prompt

    def test_tree_rules_talk_about_reason_dimensions(self, free_on):
        rules = fishbone_tree_rules()
        assert rules is FISHBONE_FREE_TREE_RULES
        assert "原因维度" in rules
        assert "这六个类别" not in rules
        # 骨下面写什么与类别固不固定无关：这一段必须与固定版同一份文字
        assert "每个类别下面写的不是「这一类是什么」的介绍" in rules

    def test_plan_ask_asks_for_own_dimensions(self, free_on):
        ask = _plan_ask("为什么客户复购率上不去", diagram_type="fishbone")
        assert "成因面" in ask
        assert "先写它" in ask
        assert "固定为人 / 机 / 料 / 法 / 环 / 测" not in ask

    def test_diagnostics_switch_to_info(self, free_on):
        content = FishboneContent(
            title="协作", problem="为什么协作效率低",
            bones=[_bone("职责"), _bone("沟通"), _bone("工具")])
        diagnostics: list = []
        _fishbone_shape_diagnostics(content, diagnostics)
        assert [item.code for item in diagnostics] == ["FISHBONE_CATEGORIES_UNFIXED"]
        assert diagnostics[0].level is DiagnosticLevel.info
        # 三条也在里面：自由类别下"少了人机料法环测"不是缺陷
        assert "3 个原因维度" in diagnostics[0].message
        assert "职责" in diagnostics[0].message

    def test_extra_branch_message_names_the_cap_not_the_six(self, free_on):
        from app.agent.tools import MAX_FISHBONE_BONES, _shape_output
        from app.contracts.mindmap import MindMapContent

        content = MindMapContent(
            title="协作",
            root=Node(id="root", label="协作", children=[
                Node(id=f"n{index}", label=f"维度{index}")
                for index in range(MAX_FISHBONE_BONES + 2)]))
        diagnostics: list = []
        shaped = _shape_output(
            content, diagram_type="fishbone",
            raw_plan={"problem": "为什么协作效率低"}, diagnostics=diagnostics)
        extra = [item for item in diagnostics if item.code == "FISHBONE_EXTRA_BRANCH"]
        assert extra, [item.code for item in diagnostics]
        assert f"比 {MAX_FISHBONE_BONES} 个多了" in extra[0].message
        assert "比六类多了" not in extra[0].message
        assert len(shaped.bones) == MAX_FISHBONE_BONES


class TestNamedCategories:
    """用户点名的那几套类别。**认不出来就什么都不加**。"""

    @pytest.mark.parametrize("text", [
        "为什么停机时间降不下来，按人机料法环测画",
        "按 5M1E 画设备停机的原因",
        "用5m1e分析一下",
        "人 机 料 法 环 测 各写几条",
        "人机料法环五个方面",          # 漏了"测"，仍然认 5M1E 那套
    ])
    def test_recognises_the_classic_sets(self, text):
        assert named_fishbone_categories(text) == FISHBONE_CATEGORIES

    def test_recognises_4m1e(self):
        assert named_fishbone_categories("按 4M1E 画") == ("人", "机", "料", "法", "环")

    @pytest.mark.parametrize("text", [
        "为什么团队协作效率低",
        "为什么客户复购率上不去",
        "按 8P 画",                 # 8 根骨超过契约上限 6，这次不进词表
        "",
    ])
    def test_says_nothing_when_not_named(self, text):
        assert named_fishbone_categories(text) == ()

    def test_user_named_set_is_written_into_the_ask(self, free_on):
        ask = _plan_ask("为什么停机降不下来，按人机料法环测画", diagram_type="fishbone")
        assert "主题里点名了一套类别（人 / 机 / 料 / 法 / 环 / 测）" in ask
        assert "逐字就用这一套" in ask

    def test_switch_off_ignores_the_named_set(self, free_off):
        ask = _plan_ask("为什么停机降不下来，按人机料法环测画", diagram_type="fishbone")
        assert "点名了一套类别" not in ask
