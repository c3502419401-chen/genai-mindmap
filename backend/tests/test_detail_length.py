"""说明那一轮的三档措辞：原样 / 去句数 / 去字数。

为什么单独一个文件：这三份的差别全在 system 的**最后一段**，而共用前半段
（`_DETAIL_ONLY_SHAPE_RULES`）必须逐字相同——`is_detail_only_prompt` 靠它判
"这是不是第三段"，判错了会把说明轮当成结构轮，返回一棵没有 detail 的树。
所以这里同时钉两件事：**档与档之间只差最后一段**，以及**关着时与基线逐字节相同**。
"""
from __future__ import annotations

import hashlib

from app.agent import tools
from app.contracts import mindmap as mindmap_contract

BASE = tools.DETAIL_ONLY_PROMPT
#: `ad61003` 那一版的 sha256。三档全关时必须回到它，一个字都不能动
#: （467 字符，同 `tests/test_detail_sentences.py` 钉的是同一份）。
BASE_SHA256 = "5064c0271312bb18ca107ccfb50cdb33fbadd128fc56f1255e67a19debddd04b"


def test_all_off_is_byte_identical(monkeypatch):
    """三档都不开 = 加这套开关之前的那一份。"""
    for name in (tools.DETAIL_LENGTH_ENV, tools.DETAIL_SENTENCES_ENV):
        monkeypatch.delenv(name, raising=False)
    assert tools._detail_prompt(80, 150) == BASE
    assert hashlib.sha256(BASE.encode()).hexdigest() == BASE_SHA256
    assert tools.detail_length_free() is False
    assert tools.detail_sentences_open() is False


def test_sentences_mode_keeps_the_char_bounds(monkeypatch):
    """去句数那一档**只**动句数：字数下限是当时唯一还在挡"越写越短"的，必须还在。"""
    monkeypatch.setenv(tools.DETAIL_SENTENCES_ENV, "1")
    prompt = tools._detail_prompt(80, 150)
    assert "80–150 字" in prompt
    assert "少于 80 字就是没写完" in prompt
    assert "至少 3 句" not in prompt
    assert prompt != BASE


def test_free_mode_drops_both_bounds(monkeypatch):
    """去字数那一档：两个数一个都不出现。"""
    monkeypatch.setenv(tools.DETAIL_LENGTH_ENV, "free")
    prompt = tools._detail_prompt(80, 150)
    assert "80" not in prompt
    assert "150" not in prompt
    assert "按这个节点在材料里有多少分量决定" in prompt
    assert prompt != BASE


def test_free_mode_wins_over_sentences(monkeypatch):
    """两档同时开时取更彻底的那个——否则"开了 free 却还看到 80 字"要查很久。"""
    monkeypatch.setenv(tools.DETAIL_LENGTH_ENV, "free")
    monkeypatch.setenv(tools.DETAIL_SENTENCES_ENV, "1")
    assert tools._detail_prompt(80, 150) == tools._detail_only_prompt_free()


def test_free_value_is_explicit(monkeypatch):
    """`MINDMAP_DETAIL_LENGTH` 用值不用布尔：`1` 不该被当成 free。"""
    monkeypatch.setenv(tools.DETAIL_LENGTH_ENV, "1")
    assert tools.detail_length_free() is False
    monkeypatch.setenv(tools.DETAIL_LENGTH_ENV, "free")
    assert tools.detail_length_free() is True
    monkeypatch.setenv(tools.DETAIL_LENGTH_ENV, "FREE ")
    assert tools.detail_length_free() is True


def test_all_three_share_the_same_head():
    """三档的前半段必须逐字相同：`is_detail_only_prompt` 靠它分类。"""
    three = [BASE, tools._detail_only_prompt_open(80, 150),
             tools._detail_only_prompt_free()]
    for prompt in three:
        assert tools.is_detail_only_prompt(prompt), prompt[-60:]
        assert prompt.startswith(tools._DETAIL_ONLY_SHAPE_RULES)


def test_free_mode_self_check_does_not_ask_for_bounds(monkeypatch):
    """自查句不能把刚放开的那条又当成检查项问回来。"""
    monkeypatch.setenv(tools.DETAIL_LENGTH_ENV, "free")
    check = tools._detail_self_check(5, tools._default_detail_span())
    assert "80" not in check
    assert "150" not in check
    assert "分量相称" in check


def test_free_mode_keeps_the_parallel_label_rule():
    """并列型 label 那条与字数无关（管的是"别只报菜名"），放开字数不该一起放开。"""
    free = tools._detail_only_prompt_free()
    assert "不要只把名字列一遍就收" in free


def test_free_mode_does_not_touch_node_budget():
    """这一档只改措辞：节点规模那一组数（参考值 / 下限 / 契约上限）一个都不动。"""
    from app.generation import budget

    assert budget.INITIAL_MAX_NODES == 80
    assert budget.MIN_INITIAL_NODES == 10
    # 可读性参考值之上不折也不裁，唯一会动手的是契约上限（见 `tools._generate` 的
    # 「超出可读性上限：不折、不裁，只报数」那段）。它也不该被这一档带着走。
    assert mindmap_contract.MAX_TREE_NODES == 1000

