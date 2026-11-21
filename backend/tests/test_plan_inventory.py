"""骨架那一次的**材料清单**：只给文件名与字数，以及"怎么用这个数"那句判据。

为什么单独一个文件：清单是骨架 prompt 的**第四种**材料线索（章节标题行 / 全塞 /
`_material_inputs` 的前 200 字 / 这一条），三条既有路径各有自己的用例文件
（`test_outline.py` / `test_single_call_generate.py` / `test_branch_expansion.py`），
混进去会让"这一条是最窄的那条"这句话无迹可循。

这里盯四件事：

  1. **只有结构**：文件名 + 字数，正文一个字都不进（逐字子串断言）。
  2. **上限不藏规模**：截到 N 份时表头仍是总数，并明说还有几份未列出。
  3. **位置**：主题之后、条数要求之前（与 `outline` 同一条理由）。
  4. **没有材料时逐字节不变**：`_plan_ask` 不带 `materials` 时必须与加它之前一致
     （同口径的第二道闸，第一道在 `test_outline.py`）。
"""
from __future__ import annotations

from app.agent.tools import (
    INVENTORY_HEADER,
    INVENTORY_MAX_LINES,
    INVENTORY_NO_STRUCTURE,
    INVENTORY_RULES,
    OUTLINE_HEADER,
    _material_inventory,
    _plan_ask,
)
from app.contracts.source import Source, SourceKind


def _source(name: str, text: str, *, source_id: str | None = None) -> Source:
    return Source(id=source_id or name, kind=SourceKind.document, status="parsed",
                  filename=name, text=text)


def test_no_material_renders_nothing():
    """纯主题没有清单可给：空串，不是一个只有表头的空壳。"""
    assert _material_inventory([], has_structure=False) == ""
    assert _material_inventory([], has_structure=True) == ""


def test_inventory_lists_name_and_length_but_no_body():
    """清单只回答"有几份、各多长"——正文一个字都不进（这是它比另外三条都窄的地方）。"""
    sources = [
        _source("openalex: 心血管.pdf", "冠心病与心衰的预测模型。" * 20),
        _source("crossref: 电力负荷.pdf", "短期负荷预测用 LSTM。" * 7),
    ]
    block = _material_inventory(sources, has_structure=False)

    assert block.splitlines()[0] == INVENTORY_HEADER.format(
        count=2, chars=f"{len(sources[0].text) + len(sources[1].text):,}",
        note=INVENTORY_NO_STRUCTURE)
    assert "  1. openalex: 心血管.pdf（240 字）" in block
    assert "  2. crossref: 电力负荷.pdf（91 字）" in block
    # 正文的任何一段都不该出现在清单里：拿原文前 12 个字逐字找。
    assert "冠心病与心衰的预测模型" not in block
    assert "短期负荷预测用" not in block


def test_inventory_keeps_the_request_order():
    """顺序按 `sources` 原序：材料在请求里的次序是用户表达"哪份更重要"的方式。"""
    sources = [_source(f"第{index}份.md", "字" * (index * 10)) for index in range(1, 5)]
    lines = _material_inventory(sources, has_structure=False).splitlines()[1:]
    assert [line.split(". ", 1)[1].split("（")[0] for line in lines] == [
        "第1份.md", "第2份.md", "第3份.md", "第4份.md"]


def test_truncation_still_reports_the_true_scale():
    """16 份被截成 10 份时，模型看到的规模必须还是 16——否则清单变成新的谎话。

    这是 `INVENTORY_MAX_LINES` 与 `MAX_MATERIALS` 不能共用的全部理由：那个 10 是
    "每份 200 字正文"的预算，这里的每行只带结构，上限可以宽，而**总数永远不许截**。
    """
    total = INVENTORY_MAX_LINES + 6
    sources = [_source(f"m{index}.md", "字" * 100) for index in range(total)]
    block = _material_inventory(sources, has_structure=True)
    lines = block.splitlines()

    assert lines[0] == INVENTORY_HEADER.format(
        count=total, chars=f"{total * 100:,}", note="")
    assert len(lines) == 1 + INVENTORY_MAX_LINES + 1
    assert lines[1] == "  1. m0.md（100 字）"
    assert lines[-1] == "  （还有 6 份未列出，合计 600 字）"


def test_structure_note_only_when_there_is_no_structure_block():
    """有目录（或图谱骨架）时不再补"没有可用的章节标题"——那句话在那种情形下不成立。"""
    sources = [_source("材料.md", "正文")]
    assert INVENTORY_NO_STRUCTURE in _material_inventory(sources, has_structure=False)
    assert INVENTORY_NO_STRUCTURE not in _material_inventory(sources, has_structure=True)


def test_inventory_sits_between_topic_and_count_with_its_rule():
    """位置与 `outline` 同一条理由（顺序即权重），且判据那句必须紧随其后。"""
    inventory = _material_inventory([_source("a.md", "字" * 30)], has_structure=False)
    ask = _plan_ask("机器学习", 6, materials=inventory)

    assert ask.index("主题：机器学习") < ask.index("这次的材料")
    assert ask.index("这次的材料") < ask.index("最多 6 条")
    # 判据在条数要求**之后**、长度要求之前：它是"怎么数条数"的补丁，得挨着那一句。
    assert ask.index("最多 6 条") < ask.index(INVENTORY_RULES) < ask.index("每条不超过")


def test_outline_stays_above_inventory():
    """两者同时存在时：主题 → 章节（细）→ 清单（粗）→ 条数要求。"""
    outline = OUTLINE_HEADER + "\n1. 预算审批（约占 40%）"
    inventory = _material_inventory([_source("a.md", "字" * 30)], has_structure=True)
    ask = _plan_ask("管理办法", 3, outline=outline, materials=inventory)

    assert (ask.index("主题：管理办法") < ask.index(OUTLINE_HEADER)
            < ask.index("这次的材料") < ask.index("最多 3 条"))


def test_without_material_the_ask_is_byte_identical():
    """没有材料时一个字都不加：body 与加清单之前逐字节相同。"""
    assert _plan_ask("管理办法", 3, materials="") == _plan_ask("管理办法", 3)
    assert INVENTORY_RULES not in _plan_ask("管理办法", 3)
    assert _plan_ask("管理办法", 3) == (
        "主题：管理办法\n"
        "根据内容实际包含的方面来定一级分支数量，最多 3 条。\n"
        "材料层次多就用足、内容单薄就少给，不要为了凑数把不同的事塞进同一支。\n"
        "每条不超过 16 字，互不重叠，按逻辑顺序排列。"
    )


def test_empty_filename_is_named():
    """没有文件名的来源也要有一行——"漏一行"与"少一份材料"是同一件事。"""
    block = _material_inventory(
        [_source("", "正文", source_id="s-anon")], has_structure=True)
    assert "  1. （未命名）（2 字）" in block
