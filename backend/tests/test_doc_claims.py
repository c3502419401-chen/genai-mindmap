"""`scripts/check_doc_claims.py` 自己的用例：**探针与反句表必须可证伪**。

## 为什么这些用例比闸门本身更重要

闸门的前提是"代码是对的，文档会漂"。反过来（文档对、代码是 bug）时，它会要求改文档去
迎合 bug——那是这道闸门最危险的失效方式，而**没有任何一条断言文档的用例能发现它**。
能发现它的只有两层：

  1. 每条事实带 `kind` 与 `evidence`：`contract` 必须指向一个真实存在的用例名
     （闸门自己校验，见 `check_evidence`），矛盾时先看证据而不是先改文档；
  2. **探针本身被证明会跟着代码变**——就是这个文件在做的事。

第 2 层缺了的话，探针可以写成 `return True` 而永远绿，闸门就退化成一张装饰。所以这里
不测"文档对不对"，只测两件更基本的事：

  - 每条规则的禁句，在它自己那侧事实上真的能命中一句话（否则那条规则是摆设）；
  - 探针读的是**文件**而不是常量：喂一份假代码树，读出来的值必须跟着变。
"""
from __future__ import annotations

import copy
import dataclasses
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_doc_claims.py"
_spec = importlib.util.spec_from_file_location("check_doc_claims", _SCRIPT)
claims = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
# `@dataclass` 会去 `sys.modules[cls.__module__]` 找模块，所以必须先登记再执行。
sys.modules[_spec.name] = claims
_spec.loader.exec_module(claims)

#: 真事实（在本仓 HEAD 上读出来的），用于"每条规则都能命中"这一组。
REAL = claims.collect()

#: 事实为真 / 为假两侧各一句**必然**该被抓到的样例句。手写这八条，剩下的开关一族自动生成。
SAMPLES: dict[str, str] = {
    "create_registers_generate": "新建导图只剩分片这一条路。",
    "deep_not_registered_under_create": "分片生成的代码没了，`mindmap_generate_deep` 已删除。",
    "source_mode_switch_retired": "`MINDMAP_SOURCE_MODE` 仍然控制着两条路。",
    "knowledge_search_implemented": "`POST /api/knowledge/search` 仍是桩，返回空。",
    "agent_tasks_endpoint_is_stub": "`POST /api/agent/tasks` 已实现，可以直接用。",
    "auth_gate_active": "所有接口都没有鉴权，匿名请求随便打。",
    "deployed": "这是一套开发骨架，最小演示闭环。",
    "four_directions": "画布仍然只有一套紧凑树布局。",
}

#: 事实为**假**时该被抓到的样例句（只挑两条最容易翻的，验证双向真的在）。
REVERSE_SAMPLES: dict[str, str] = {
    "create_registers_generate": "`create` 只有 `mindmap_generate` 这一条路。",
    "knowledge_search_implemented": "`POST /api/knowledge/search` 已经接了检索，implemented: true。",
    "deep_not_registered_under_create": "分片没有注册到 `create` 里。",
}


def rules() -> list:
    return (list(claims.RULES) + claims._switch_rules(REAL)
            + claims._backlog_rules(REAL) + claims._drift_rules())


def fresh(**overrides):
    """`REAL` 的一份副本，`problems` 清空。`scan` 会往 facts.problems 里追加东西。"""
    facts = copy.deepcopy(REAL)
    facts.problems = []
    for key, value in overrides.items():
        setattr(facts, key, value)
    return facts


def by_name() -> dict:
    return {rule.name: rule for rule in rules()}


def _scan_text(tmp_path: Path, text: str, facts=None) -> list:
    """在临时目录里造一份 md，走完整的 `scan`（不是只测正则）。"""
    facts = facts or REAL
    document = tmp_path / "SAMPLE.md"
    document.write_text(text + "\n", encoding="utf-8")
    return claims.scan(facts, rules(), [document])


def test_every_rule_has_a_sample_and_its_own_side_is_armed():
    """每条规则要么有手写样例，要么属于开关一族（自动生成），没有第三条路。

    一条规则如果既没有样例又不在开关族里，它就没人管——那正是"反句表看起来齐全、
    实际有一条永远不生效"的形状。
    """
    for rule in claims.RULES:
        assert rule.name in SAMPLES or rule.name in REVERSE_SAMPLES, f"{rule.name} 没有样例"
        assert rule.when_true or rule.when_false, f"{rule.name} 两侧都是空的"


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_the_rule_catches_its_own_sample(tmp_path: Path, name: str) -> None:
    """每条规则在自己那侧事实上，真的能把那句话抓出来。"""
    assert by_name()[name].name == name
    hits = [item for item in _scan_text(tmp_path, SAMPLES[name]) if item.rule.name == name]
    assert hits, f"{name} 没抓到样例：{SAMPLES[name]}"
    assert not hits[0].excused


def _violation_text(rule) -> str:
    """开关一族的违反句可以机械生成：把代码的默认值反过来写。"""
    env_name = rule.name.split(":", 1)[1]
    declared = "关" if rule.holds(REAL) else "开"
    return f"`{env_name}` 默认{declared}，部署时不必管它。"


def test_switch_rules_catch_the_opposite_default(tmp_path: Path) -> None:
    """开关一族：代码默认开，文档说默认关 → 红；代码默认关，文档说默认开 → 红。

    这一族是"默认值"那一族的全量核（上一轮只抽验了两项）。它成立的前提是
    `_switch_rules` 用的是**实测默认值**而不是注释里抄来的说法。
    """
    switch_rules = claims._switch_rules(REAL)
    assert len(switch_rules) >= 30, f"只找到 {len(switch_rules)} 个开关，少得可疑"
    for rule in switch_rules:
        hits = [item for item in _scan_text(tmp_path, _violation_text(rule))
                if item.rule.name == rule.name]
        assert hits, f"{rule.name} 没抓到反着写的默认值"


def test_the_forbidden_sentence_flips_with_the_fact(tmp_path: Path) -> None:
    """**这套设计的核心**：代码翻回去，闸门换掉它禁止的说法。

    事实为真时禁"只剩分片"；把注册表换成只剩分片之后，同一句话必须被**放行**，
    而"create 只有 mindmap_generate"必须被**抓**。
    """
    flipped = claims.Facts(
        registry={"create": ("mindmap_generate_deep",),
                  "create_deep": ("mindmap_generate",),
                  "expand": ("node_expand",),
                  "edit": ("add_node",)},
        stubs={"SearchResponse": True},
    )
    rule = by_name()["create_registers_generate"]
    assert rule.holds(REAL), "本仓 HEAD 上 create 应注册整树生成"
    assert not rule.holds(flipped)

    # 旧的禁句在新事实上不该再命中（它已经不对了）。
    assert not [item for item in _scan_text(tmp_path, SAMPLES["create_registers_generate"],
                                            flipped) if item.rule.name == rule.name]
    # 新的禁句该命中。
    hits = [item for item in _scan_text(tmp_path, REVERSE_SAMPLES["create_registers_generate"],
                                        flipped) if item.rule.name == rule.name]
    assert hits, "代码翻回去之后，闸门没有开始禁止「create 只有 mindmap_generate」"


def test_the_probe_reads_the_file_not_a_constant(tmp_path: Path) -> None:
    """探针可证伪：同一个 `_ALLOWED_BY_INTENT`，喂两份假代码树，读出两个不同的值。

    这条用例是整套设计唯一的"防硬编码"来源。没有它，探针可以永远 `return True`。
    """
    body = """\
_ALLOWED_BY_INTENT: dict[str, tuple[str, ...]] = {{
    "create": {create!r},
    "create_deep": {deep!r},
}}
"""
    one = tmp_path / "tools.py"
    one.write_text(body.format(create=("mindmap_generate",),
                               deep=("mindmap_generate_deep",)), encoding="utf-8")
    facts = claims.Facts()
    claims.probe_allowed_literal(facts, one)
    assert facts.allowed_literal == {
        "create": ("mindmap_generate",), "create_deep": ("mindmap_generate_deep",)}

    two = tmp_path / "tools2.py"
    two.write_text(body.format(create=("mindmap_generate_deep",),
                               deep=("mindmap_generate",)), encoding="utf-8")
    facts = claims.Facts()
    claims.probe_allowed_literal(facts, two)
    assert facts.allowed_literal == {
        "create": ("mindmap_generate_deep",), "create_deep": ("mindmap_generate",)}


def test_the_direction_probe_counts_what_is_in_the_file(tmp_path: Path) -> None:
    three = tmp_path / "tidy.ts"
    three.write_text("export type Direction = 'right' | 'left' | 'down';\n", encoding="utf-8")
    facts = claims.Facts()
    claims.probe_directions(facts, three)
    assert facts.directions == 3

    four = tmp_path / "tidy4.ts"
    four.write_text("export type Direction = 'right' | 'left' | 'down' | 'both';\n",
                    encoding="utf-8")
    facts = claims.Facts()
    claims.probe_directions(facts, four)
    assert facts.directions == 4


def test_deploy_probe_follows_the_files(tmp_path: Path) -> None:
    facts = claims.Facts()
    claims.probe_deploy(facts, tmp_path)
    assert facts.deploy_ready is False
    for name in ("genai-mindmap.service", "nginx.conf", "install.sh"):
        (tmp_path / name).write_text("", encoding="utf-8")
    facts = claims.Facts()
    claims.probe_deploy(facts, tmp_path)
    assert facts.deploy_ready is True


def test_an_exemption_must_name_a_real_claim_and_say_why(tmp_path: Path) -> None:
    """豁免不许烂成"别查我"白名单：名字必须存在、理由必须有。"""
    facts = fresh()
    _scan_text(tmp_path, "<!-- claims-ok: no_such_claim 随便 -->\n画布仍然只有一套紧凑树布局。",
               facts)
    assert any("不存在的事实名" in item for item in facts.problems), facts.problems

    facts = fresh()
    _scan_text(tmp_path, "<!-- claims-ok: four_directions -->\n画布仍然只有一套紧凑树布局。",
               facts)
    assert any("没写理由" in item for item in facts.problems), facts.problems


def test_a_proper_exemption_silences_exactly_one_rule(tmp_path: Path) -> None:
    facts = fresh()
    hits = _scan_text(
        tmp_path,
        "<!-- claims-ok: four_directions 这一块引的是旧版说法 -->\n"
        "画布仍然只有一套紧凑树布局。",
        facts)
    mine = [item for item in hits if item.rule.name == "four_directions"]
    assert mine and all(item.excused and item.because == "claims-ok" for item in mine)
    assert not facts.problems


def test_a_negation_is_not_a_claim(tmp_path: Path) -> None:
    """"不是开发骨架" 与 "是开发骨架" 逐字节几乎相同而意思相反。"""
    hits = _scan_text(tmp_path, "这是一套已上线的系统，不是开发骨架。")
    mine = [item for item in hits if item.rule.name == "deployed"]
    assert mine and all(item.excused and item.because == "否定语境" for item in mine)


def test_every_contract_fact_points_at_a_test_that_exists() -> None:
    """`contract` 的背书用例必须真的在仓库里——这是"代码错了、文档对了"那一侧的防线。"""
    missing = claims.check_evidence(claims.RULES)
    assert missing == [], missing
    for rule in claims.RULES:
        if rule.kind != "contract":
            continue
        assert "::" in rule.evidence, f"{rule.name} 是 contract 却没有用例名"
        assert "test_" in rule.evidence.split("::", 1)[1]


def test_the_numbering_of_the_five_probes_is_still_true() -> None:
    """五类探针都在，且 `problems` 为空——探针静默失败时，闸门会变成永远绿。"""
    assert REAL.problems == [], REAL.problems
    assert REAL.registry and REAL.allowed_literal and REAL.stubs is not None
    assert REAL.env_switches and REAL.env_names_in_code
    assert REAL.gate_wired and REAL.gate_refuses
    assert REAL.deploy_ready is True
    assert REAL.directions == 4


def test_the_allowed_literal_and_the_registry_agree() -> None:
    """① 与 ② 一致：表里写着而 `_TOOL_SPECS` 里没有的名字，注册表里不会出现。"""
    for intent, names in (REAL.allowed_literal or {}).items():
        actual = set((REAL.registry or {}).get(intent, ()))
        assert set(names) <= actual, f"{intent}: 表里 {set(names) - actual} 没有注册"


def test_the_sentence_crop_keeps_it_readable() -> None:
    """命中句是"前后各 24 字"，不是整行——文档里有整段一行的表格。"""
    line = "甲" * 100 + "只剩分片" + "乙" * 100
    text = claims._sentence(line, 100, 4)
    assert text.startswith("…") and text.endswith("…")
    assert "只剩分片" in text
    assert len(text) < 80


def test_the_doc_list_skips_other_repos_readmes() -> None:
    """`docs/research/` 里是别人仓库的 README 快照，扫它必然误报。"""
    files, problems = claims.doc_files()
    assert problems == [], problems
    names = [path.relative_to(claims.REPO_DIR).as_posix() for path in files]
    assert names, "一份文档都没扫到"
    assert not [name for name in names if name.startswith("docs/research/")]
    assert not [name for name in names if name == claims.SELF_DOC]


def test_every_switch_rule_has_a_real_evidence_file() -> None:
    """开关一族的 evidence 是真实文件路径——不是一句"当前实现"。"""
    for rule in claims._switch_rules(REAL):
        assert (claims.REPO_DIR / rule.evidence).exists(), rule.evidence


def test_no_switch_rule_is_green_on_both_sides() -> None:
    """开关一族只能禁一侧：两边都禁等于"这句话怎么写都错"，会误伤正确的写法。

    `RULES` 里的八条**两侧都写**是刻意的——那就是双向：事实为真禁一套说法，为假禁另一套。
    """
    for rule in claims._switch_rules(REAL):
        assert not (rule.when_true and rule.when_false), rule.name
    for rule in claims.RULES:
        assert rule.when_true and rule.when_false, rule.name


def test_the_pattern_and_the_sentence_set_do_not_disagree() -> None:
    """开关一族既有 `pattern`（取值比对）又有禁句正则：两者必须命中同一侧。"""
    for rule in claims._switch_rules(REAL):
        forbidden = rule.when_true if rule.holds(REAL) else rule.when_false
        assert rule.pattern is not None and forbidden
        assert re.search(forbidden[0], _violation_text(rule)), rule.name


# ---------------------------------------------------------------- 台账
#
# `docs/BACKLOG.md` 是"还剩什么事没做"的唯一入口。它自己也要被证伪，否则台账会退化成
# 一份"状态随便写、反正没人验"的待办快照——那正是它要取代的东西。


def _backlog_text() -> str:
    return (claims.BACKLOG).read_text(encoding="utf-8")


def _entry(number: int) -> object:
    return {entry.number: entry for entry in (REAL.backlog or ())}[number]


def test_the_backlog_parses_every_entry_and_its_own_check_is_green() -> None:
    """结构、编号、四态、判据锚、状态与 probe —— 一条都不能含糊。"""
    entries = REAL.backlog or ()
    assert len(entries) >= 8, entries
    assert [entry.number for entry in entries] == list(range(1, len(entries) + 1))
    assert all(entry.status in claims.STATUSES for entry in entries)
    assert claims.check_backlog(REAL) == [], claims.check_backlog(REAL)


def test_every_entry_with_a_probe_has_a_reading_and_a_known_name() -> None:
    """`probe` 非 `none` 的条目必须有读数；名字必须注册过（写错名字会红，不是跳过）。"""
    for entry in REAL.backlog or ():
        if entry.probe == "none":
            assert entry.status in (claims.STATUS_TODO, claims.STATUS_WONT), entry.number
            continue
        assert entry.probe in claims.BACKLOG_PROBE_DOC, entry.probe
        assert entry.probe in REAL.backlog_probes, entry.probe
        assert entry.status in claims.NEEDS_PROBE, (entry.number, entry.status)


def test_a_wrong_status_is_red() -> None:
    """**把一条"已做"改成"待做"，台账必须红**（这是这台账本存在的理由）。

    两条红都要在：语义层（`check_backlog`）与块内禁句（`_backlog_rules` 的 `when_true`）。
    只留一条，另一个方向的漂移就没人管。
    """
    text = _backlog_text()
    wrong = re.sub(
        r"(- \*\*状态\*\*：)已做(\s*\n- \*\*probe\*\*：`branch_arg_deprecated`)",
        r"\1待做\2", text, count=1)
    assert wrong != text, "样例失效：第 1 条不是「已做 + branch_arg_deprecated」"
    entries, problems = claims.parse_backlog(wrong)
    assert problems == [], problems
    facts = copy.deepcopy(REAL)
    facts.backlog = tuple(entries)
    facts.problems = []
    hits = claims.check_backlog(facts)
    assert any("第 1 条" in hit and "已经在代码里" in hit for hit in hits), hits

    rule = {item.name: item for item in claims._backlog_rules(facts)}["backlog:1"]
    assert rule.holds(facts)
    lines = wrong.splitlines()[rule.block[0] - 1:rule.block[1]]
    caught = [line for line in lines if re.search(rule.when_true[0], line)]
    assert any("状态" in line for line in caught), caught


def test_the_backlog_rule_flips_the_forbidden_sentence() -> None:
    """**代码翻回去，台账开始禁另一套说法**。

    事实为真（`description` 是"已废弃"）→ 禁"待做/该做"；把事实翻成假之后，同一个块里
    的"已做"必须被抓。这条固化的就是手工验红②，免得下次只能靠人记得去改一遍代码。
    """
    rule = {item.name: item for item in claims._backlog_rules(REAL)}["backlog:1"]
    assert rule.holds(REAL), "本仓 HEAD 上 branches 的 description 应已改成废止"
    lines = _backlog_text().splitlines()[rule.block[0] - 1:rule.block[1]]
    assert not [line for line in lines if re.search(rule.when_true[0], line)]

    flipped = copy.deepcopy(REAL)
    flipped.backlog_probes["branch_arg_deprecated"] = False
    rules_flipped = {item.name: item for item in claims._backlog_rules(flipped)}
    assert not rules_flipped["backlog:1"].holds(flipped)
    assert any(re.search(rule.when_false[0], line) for line in lines), \
        "代码翻回去之后，台账没有开始禁止「已做」"


def test_the_backlog_probes_read_the_file_not_a_constant(tmp_path: Path) -> None:
    """六条 probe 里最容易被写死的那条：`description` 的文本必须从文件读。"""
    def build(root: Path, description: str) -> Path:
        (root / "agent").mkdir(parents=True)
        (root / "agent" / "tools.py").write_text(
            'from pydantic import Field\n'
            'OUTLINE_FEW_BLOCKS_NOTE = "x"\n'
            'class GenerateArgs:\n'
            f'    branches: int = Field(default=5, description="{description}")\n',
            encoding="utf-8")
        (root / "contracts").mkdir()
        (root / "contracts" / "mindmap.py").write_text(
            'def clip_label(text, limit=16):\n'
            '    return "".join(text[:_clip_index(text, limit)])\n'
            'def _clip_index(chars, limit):\n'
            '    return limit\n'
            'class Node:\n'
            '    origin: str = "unknown"\n', encoding="utf-8")
        (root / "knowledge").mkdir()
        (root / "knowledge" / "router.py").write_text(
            'from app.knowledge import graph_resolve\n'
            'def go():\n'
            '    return graph_resolve.resolve_entities([])\n', encoding="utf-8")
        (root / "knowledge" / "graph_resolve.py").write_text(
            'def resolve_entities(items):\n'
            '    return items\n', encoding="utf-8")
        (root / "probe.py").write_text("", encoding="utf-8")
        return root

    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"x": {"status": "accepted"}}', encoding="utf-8")

    deprecated = build(tmp_path / "a", "已废弃：一级分支条数由内容决定")
    values, _ = claims.probe_backlog_facts(
        app_dir=deprecated, baseline=baseline, scripts_dir=tmp_path / "a")
    assert values["branch_arg_deprecated"] == claims.PRESENT
    assert values["clip_label_retreats"] == claims.PRESENT
    assert values["node_origin_field"] == claims.PRESENT
    assert values["resolve_has_route"] == claims.PRESENT

    alive = build(tmp_path / "b", "一级分支数量上限")
    values, _ = claims.probe_backlog_facts(
        app_dir=alive, baseline=baseline, scripts_dir=tmp_path / "b")
    assert values["branch_arg_deprecated"] == claims.ABSENT


# ------------------------------------------------ 三条探针的改名反例
#
# 二值读数把两件事压成同一个 `False`：**判据自己坏了**（目标符号改名、文件读不到）与
# **被测的事实不成立**。前者是闸门的问题，后者是代码的问题；混着报就会把"我的判据失效
# 了"说成"代码被翻回去了"，读者于是去翻 git、去重做已经做完的活。
#
# 下面三条各读三档：`present`（原料在、事实成立）、`absent`（原料在、事实为假）、
# `missing`（原料改名）。**第三档是重点**：只测 present/absent 的用例挡不住"把 missing
# 硬编码成 absent"这件事，而这正是上一轮漏掉的那个缺口。

_ALIVE_CONTRACTS = (
    "def clip_label(text, limit=16):\n"
    '    return "".join(text[:_clip_index(text, limit)])\n'
    "def _clip_index(chars, limit):\n"
    "    return limit\n"
    "class Node:\n"
    '    origin: str = "unknown"\n')

_ALIVE_ROUTER = ("from app.knowledge import graph_resolve\n"
                 "def go():\n"
                 "    return graph_resolve.resolve_entities([])\n")

_ALIVE_RESOLVER = ("def resolve_entities(items):\n"
                   "    return items\n")


def _probe_sandbox(root: Path, contracts: str = _ALIVE_CONTRACTS,
                   router: str = _ALIVE_ROUTER,
                   resolver: str = _ALIVE_RESOLVER) -> Path:
    """一棵"三条探针各得其所"的最小假代码树：只给原料，别的什么都不给。"""
    for folder in ("agent", "contracts", "knowledge"):
        (root / folder).mkdir(parents=True)
    (root / "agent" / "tools.py").write_text("", encoding="utf-8")
    (root / "contracts" / "mindmap.py").write_text(contracts, encoding="utf-8")
    (root / "knowledge" / "router.py").write_text(router, encoding="utf-8")
    (root / "knowledge" / "graph_resolve.py").write_text(resolver, encoding="utf-8")
    return root


def _readings(root: Path) -> tuple[dict[str, str], list[str]]:
    return claims.probe_backlog_facts(app_dir=root, scripts_dir=root)


def _about(problems: list[str], name: str) -> list[str]:
    return [item for item in problems if f"`{name}`" in item]


def _assert_missing_text(problems: list[str], name: str) -> str:
    """判据失效的报错必须点名、必须带标记，**且不许用 `absent` 的话术**。"""
    text = " ".join(_about(problems, name))
    assert claims.MISSING_MARK in text, text
    assert "状态改错" not in text, text
    assert "代码被翻回去了" not in text, text
    return text


def test_clip_label_probe_distinguishes_missing_from_absent(tmp_path: Path) -> None:
    """`clip_label_retreats`：`_clip_index` **改名**时读 `missing`，不是 `absent`。

    改名反例是这条用例的全部理由：`_clip_index` 换成 `_clip_index_v2` 之后，探针里那句
    `node.name == "_clip_index"` 悄悄落空。旧实现返 `False`，闸门于是报「状态改错，或
    代码被翻回去了」——而代码一步都没退，是判据自己断了。
    """
    alive = _probe_sandbox(tmp_path / "alive")
    values, problems = _readings(alive)
    assert values["clip_label_retreats"] == claims.PRESENT, problems

    # 事实为假：切点不再回退，直接 `text[:limit]`
    bare = _probe_sandbox(tmp_path / "bare", contracts=_ALIVE_CONTRACTS.replace(
        '"".join(text[:_clip_index(text, limit)])', "text[:limit]"))
    values, problems = _readings(bare)
    assert values["clip_label_retreats"] == claims.ABSENT, problems
    assert _about(problems, "clip_label_retreats") == [], "absent 不该报判据失效"

    renamed = _probe_sandbox(
        tmp_path / "renamed",
        contracts=_ALIVE_CONTRACTS.replace("_clip_index", "_clip_index_v2"))
    values, problems = _readings(renamed)
    assert values["clip_label_retreats"] == claims.MISSING, values
    text = _assert_missing_text(problems, "clip_label_retreats")
    assert "`_clip_index`" in text, text


def test_resolve_probe_distinguishes_missing_from_absent(tmp_path: Path) -> None:
    """`resolve_has_route`：`graph_resolve.resolve_entities` 改名 → `missing`。

    这条的原料是**被调方的定义**，不是 `router.py` 里的调用点：名字一变，调用点必然跟着
    变（否则运行时 NameError），于是"找不到调用"这件事只可能来自判据失效。
    """
    alive = _probe_sandbox(tmp_path / "alive")
    values, problems = _readings(alive)
    assert values["resolve_has_route"] == claims.PRESENT, problems

    # 事实为假：router 还在，但它不再走那条路由
    dead_end = _probe_sandbox(
        tmp_path / "dead", router="from app.knowledge import graph_resolve\n"
                                  "def go():\n"
                                  "    return None\n")
    values, problems = _readings(dead_end)
    assert values["resolve_has_route"] == claims.ABSENT, problems
    assert _about(problems, "resolve_has_route") == []

    renamed = _probe_sandbox(
        tmp_path / "renamed",
        router=_ALIVE_ROUTER.replace("resolve_entities", "resolve_entities_v2"),
        resolver=_ALIVE_RESOLVER.replace("resolve_entities", "resolve_entities_v2"))
    values, problems = _readings(renamed)
    assert values["resolve_has_route"] == claims.MISSING, values
    text = _assert_missing_text(problems, "resolve_has_route")
    assert "`resolve_entities`" in text, text


def test_node_origin_probe_distinguishes_missing_from_absent(tmp_path: Path) -> None:
    """`node_origin_field`：`Node` **类**改名 → `missing`；字段被删 → `absent`。

    这一对最容易被写混：`Node.origin` 不在有两种完全不同的原因——`Node` 还是那个 `Node`
    而字段没写（事实为假），或者这段代码根本不是 `Node` 了（判据失效）。
    """
    alive = _probe_sandbox(tmp_path / "alive")
    values, problems = _readings(alive)
    assert values["node_origin_field"] == claims.PRESENT, problems

    # 事实为假：`Node` 在，`origin` 字段被删（留 `pass` 保证这棵树还能解析）
    no_field = _probe_sandbox(tmp_path / "nofield", contracts=_ALIVE_CONTRACTS.replace(
        '    origin: str = "unknown"\n', "    pass\n"))
    values, problems = _readings(no_field)
    assert values["node_origin_field"] == claims.ABSENT, problems
    assert _about(problems, "node_origin_field") == []

    renamed = _probe_sandbox(tmp_path / "renamed",
                             contracts=_ALIVE_CONTRACTS.replace("class Node:",
                                                                "class NodeV2:"))
    values, problems = _readings(renamed)
    assert values["node_origin_field"] == claims.MISSING, values
    text = _assert_missing_text(problems, "node_origin_field")
    assert "`Node`" in text, text


def test_every_probe_declares_its_raw_material() -> None:
    """每条 probe 都要登记原料，且与注册表同名同数——漏一条就退回二值读数。"""
    assert set(claims.PROBE_MATERIAL) == set(claims.BACKLOG_PROBE_DOC), (
        sorted(set(claims.BACKLOG_PROBE_DOC) - set(claims.PROBE_MATERIAL)),
        sorted(set(claims.PROBE_MATERIAL) - set(claims.BACKLOG_PROBE_DOC)))
    for name, items in claims.PROBE_MATERIAL.items():
        assert items, name
        assert all("`" in item for item in items), (name, items)


def test_no_probe_is_missing_on_this_checkout() -> None:
    """本仓 HEAD 上**一条都不许是 missing**：那是判据失效，不是"没做"。

    这条在 CI 上的独立价值：真有人改了一处符号名而没跟探针，这里会指名道姓地红，
    而不是让闸门报一句"状态写错，或代码被翻回去了"。
    """
    missing = sorted(name for name, state in REAL.backlog_probes.items()
                     if state == claims.MISSING)
    assert missing == [], missing
    assert not [item for item in REAL.problems if claims.MISSING_MARK in item], REAL.problems


def test_the_cross_reference_probe_follows_the_wiring(tmp_path: Path) -> None:
    """台账第 9 条的探针读三件事，任何一件被翻回去都要红。

    它守的正是这一轮修的那个形状：**规则写了但没人收**。所以探针不能只判"常量在"，
    还要判"两段式那份与三段式那份是 if/else 互斥"与"收 relations 的那一支不再只属于
    `graph_mode`"。三条各给一个反例——只判一条的探针在另外两条翻回去时仍然是绿的。
    """
    wired = (
        'CROSS_REFERENCE_RULES = "x"\n'
        'GRAPH_RELATION_RULES = "y"\n'
        'def _generate():\n'
        '    if graph_mode:\n'
        '        system += GRAPH_RELATION_RULES\n'
        '    elif structure_rules_given:\n'
        '        system += CROSS_REFERENCE_RULES\n'
        '    raw_relations = raw.pop("relations", [])\n'
        '    if graph_mode or raw_relations:\n'
        '        relations, drops = _relations_from(raw_relations, content)\n'
        '    return system\n')

    def values_for(source: str, name: str) -> dict[str, str]:
        root = tmp_path / name
        (root / "agent").mkdir(parents=True)
        (root / "agent" / "tools.py").write_text(source, encoding="utf-8")
        for folder in ("contracts", "knowledge"):
            (root / folder).mkdir()
        (root / "contracts" / "mindmap.py").write_text("", encoding="utf-8")
        (root / "knowledge" / "router.py").write_text("", encoding="utf-8")
        read, _ = claims.probe_backlog_facts(app_dir=root, scripts_dir=root)
        return read

    assert values_for(wired, "ok")["cross_reference_wired"] == claims.PRESENT
    # ① 常量在，但 `_generate` 不再引用它（零引用）
    unused = wired.replace("        system += CROSS_REFERENCE_RULES\n",
                           "        pass\n")
    assert values_for(unused, "unused")["cross_reference_wired"] == claims.ABSENT
    # ② 两份关系规则不再是 if/else：同一个字段会收到两套上限与两套 kind
    both = wired.replace("    elif structure_rules_given:\n", "    if True:\n")
    assert values_for(both, "both")["cross_reference_wired"] == claims.ABSENT
    # ③ 收 relations 那一支又锁回 graph_mode：模型写了也收不到（上一版的形状）
    locked = wired.replace("if graph_mode or raw_relations:",
                           "if graph_mode and raw_relations:")
    assert values_for(locked, "locked")["cross_reference_wired"] == claims.ABSENT


def test_a_missing_anchor_is_reported_as_lost_evidence_not_as_undone() -> None:
    """判据被删 ≠ 功能没做。两种红必须报不同的话，否则下一轮会把已做的活重做一遍。

    这条也是那个循环问题的答案：**探针读代码事实，锚只看证据在不在**。所以用例被删
    时报的是「失去判据」。
    """
    text = _backlog_text()
    broken = text.replace(
        "`backend/tests/test_node_detail.py::test_clip_label_output_never_ends_inside_a_token`",
        "`backend/tests/test_node_detail.py::test_this_name_does_not_exist`", 1)
    assert broken != text
    entries, _ = claims.parse_backlog(broken)
    facts = copy.deepcopy(REAL)
    facts.backlog = tuple(entries)
    hits = claims.check_backlog(facts)
    assert any("失去判据" in hit for hit in hits), hits
    # 只有这一条：不能顺手再报一句"状态写错了"——那就把"证据丢了"读成"活没做"。
    assert len(hits) == 1, hits


def test_a_todo_entry_may_point_at_a_gate_that_does_not_exist_yet() -> None:
    """「待做」的判据是"它做完了会长什么样"，所以锚可以先不在——但「已做」不行。

    这条原来挂在真台账第 10 条身上（那时它写着待做、判据指向还不存在的
    `scripts/check_doc_map.py`）。**那条账目 2026-11-28 做完了**，闸门也建起来了；
    再把用例挂在它身上，这条例子的存亡就跟着那条账目走。所以改成合成台账：
    「待做可以指向未来」是规矩，不该依赖真实仓库里恰好留着一个指向未来的条目。
    """
    text = _backlog_text()
    made = text.replace(
        "- **状态**：已做\n- **probe**：`doc_map_gate`",
        "- **状态**：待做\n- **probe**：`none`", 1)
    made = made.replace("`python scripts/check_doc_map.py --gate`",
                        "`python scripts/check_doc_map_not_built_yet.py --gate`", 1)
    assert made != text, "第 10 条的字段形态变了，先改用例"
    entries, _ = claims.parse_backlog(made)
    facts = copy.deepcopy(REAL)
    facts.backlog = tuple(entries)
    # 锚不存在也不算红——但必须**可见**，否则"待做 + 一个永远不会出现的锚"就成了没人管的一格。
    assert claims.check_backlog(facts) == []
    assert any("第 10 条" in item for item in claims.pending_anchors(facts))


def test_the_doc_map_entry_is_settled_and_its_gate_has_a_reading() -> None:
    """第 10 条做完了，而"做完了"必须有机器判据：`doc_map_gate` 读到真。

    探针读的不是"脚本文件在不在"（那是 `check_script_hygiene.py` 的 C 档），是**它有没有
    真的在判**：有 `--gate`、`main()` 调了 `check()`、`check()` 里接了 `reachable()`。
    """
    entry = next((item for item in (REAL.backlog or ()) if item.number == 10), None)
    assert entry is not None
    assert entry.status == claims.STATUS_DONE
    assert entry.probe == "doc_map_gate"
    assert REAL.backlog_probes.get("doc_map_gate") == claims.PRESENT
    assert claims.check_backlog(REAL) == []


def test_the_backlog_is_scanned_even_before_git_tracks_it() -> None:
    """台账是本闸门自己的输入：没被 `git ls-files` 列出时也必须扫，否则块内禁句空转。"""
    files, problems = claims.doc_files()
    assert problems == [], problems
    names = [path.relative_to(claims.REPO_DIR).as_posix() for path in files]
    assert claims.BACKLOG_RELATIVE in names, names


# ---------------------------------------------------------------- 台账：blocker_probe
#
# `probe` 答的是"已验的那一半在不在"，而"半做"的另一半**卡在什么**，过去只有一句人写的
# 自述。第 6 条那句自述是错的（写"缺 MINDMAP_LLM_API_KEY"，而 key 一直在
# `run-backend.bat` 第 5 行），条目因此在错误前提上停了两轮。这一组钉住"卡点必须能机器判"，
# 以及它的极性：**真 = 卡点还在**。


def test_a_half_done_entry_without_a_blocker_probe_is_red() -> None:
    """半做没有 `blocker_probe` → 红。另一半卡在什么，不许只留一句人写的自述。"""
    text = _backlog_text()
    broken = re.sub(r"- \*\*blocker_probe\*\*：[^\n]*\n", "", text, count=1)
    assert broken != text, "样例失效：台账里没有 blocker_probe 那一行"
    entries, problems = claims.parse_backlog(broken)
    assert problems == [], problems
    facts = copy.deepcopy(REAL)
    facts.backlog = tuple(entries)
    hits = claims.check_backlog(facts)
    assert any("必须给 `blocker_probe`" in hit for hit in hits), hits


def test_a_vanished_blocker_is_reported_as_ready_not_as_done() -> None:
    """卡点消失**不是红**，也不是"已做"——只提示"可以转已做，但要先真跑一遍"。

    自动转就把"卡点消失"当成了"活干完"，那是同一个病换个方向：第 9 条那种采样、第 11 条
    那两格的归因，都必须有人真跑一次才叫做完。
    """
    facts = copy.deepcopy(REAL)
    facts.backlog_blockers["cross_reference_quota_sampling_stale"] = False
    assert claims.check_backlog(facts) == []
    hints = claims.blocker_hints(facts)
    assert any("第 9 条" in hint and "可以转" in hint for hint in hints), hints
    entry = next(item for item in facts.backlog or () if item.number == 9)
    assert entry.status == claims.STATUS_PART, "闸门不许自己改状态"


def test_the_blocker_probes_read_the_code_and_the_history_not_a_constant(
        tmp_path: Path) -> None:
    """两条 blocker 探针各喂一份假输入，读数必须跟着变。

    第 9 条是**时序型**（产物与补丁的提交先后），第 11 条是**代码事实型**。前者用提交时间
    而不是 mtime：干净 checkout 上 mtime 是 checkout 那一刻，那会永远为真。
    """
    def fake_repo(root: Path, first: str, second: str) -> Path:
        """建一个真 git 仓库：`first` 先提交、`second` 后提交（时间显式隔开）。"""
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "a"], cwd=root, check=True)
        for index, name in enumerate((first, second)):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n", encoding="utf-8")
            env = {**os.environ,
                   "GIT_AUTHOR_DATE": f"@{1_800_000_000 + index * 3600}",
                   "GIT_COMMITTER_DATE": f"@{1_800_000_000 + index * 3600}"}
            subprocess.run(["git", "add", name], cwd=root, check=True, env=env)
            subprocess.run(["git", "commit", "-q", "-m", name], cwd=root, check=True, env=env)
        return root

    def fake_code(root: Path, budget: str, style: str) -> Path:
        (root / "scripts").mkdir(parents=True, exist_ok=True)
        (root / "prefs").mkdir(parents=True, exist_ok=True)
        (root / "scripts" / "measure_branch_budget.py").write_text(budget, encoding="utf-8")
        (root / "prefs" / "style.py").write_text(style, encoding="utf-8")
        return root

    tools = "backend/app/agent/tools.py"
    record = "docs/CROSS-BRANCH-DUPLICATES.md"
    # 生成侧后提交（规则改过、采样没重跑）→ 卡点仍在。
    stale = fake_repo(tmp_path / "stale", record, tools)
    # 采样记录后提交（按新规则重跑过）→ 卡点消失。
    fresh_repo = fake_repo(tmp_path / "fresh", tools, record)

    single = 'def _number(k, v):\n    return v\n\ndef go(xs):\n    return {"branches": _number("branches", [x for x in xs])}\n'
    bucketed = "style_profile = 1\n"
    budget_plain = "ARMS = {}\n"
    budget_attributed = "style_profile = 'x'\nARMS = {}\n"

    code = fake_code(tmp_path / "code", budget_plain, single)
    values, problems = claims.probe_blockers(
        app_dir=code, scripts_dir=code / "scripts", repo_dir=stale)
    assert problems == [], problems
    assert values["cross_reference_quota_sampling_stale"] is True
    assert values["style_branch_attribution_absent"] is True

    values, _ = claims.probe_blockers(
        app_dir=code, scripts_dir=code / "scripts", repo_dir=fresh_repo)
    assert values["cross_reference_quota_sampling_stale"] is False

    # 归因切分出现了（脚本开始读画像）→ 卡点消失。
    fake_code(tmp_path / "code2", budget_attributed, single)
    values, _ = claims.probe_blockers(
        app_dir=tmp_path / "code2", scripts_dir=tmp_path / "code2" / "scripts", repo_dir=stale)
    assert values["style_branch_attribution_absent"] is False

    # `branches` 不再单值汇总（分桶了）→ 卡点也消失。
    fake_code(tmp_path / "code3", budget_plain, bucketed)
    values, _ = claims.probe_blockers(
        app_dir=tmp_path / "code3", scripts_dir=tmp_path / "code3" / "scripts", repo_dir=stale)
    assert values["style_branch_attribution_absent"] is False

    # 读不到 git（不是仓库）→ 不可判，**不当成卡点已消失**。
    values, problems = claims.probe_blockers(
        app_dir=code, scripts_dir=code / "scripts", repo_dir=tmp_path / "not-a-repo")
    assert "cross_reference_quota_sampling_stale" not in values
    assert any("读不到提交时间" in item for item in problems), problems


# ---------------------------------------------------------------- 会漂的数（D 族）
#
# 第四族判"这句话会不会自己过期"。它不起因于代码变了，起因于**别人正常干活**：
# 同一条"2113 条用例"被抄到三处，三处都过期了而没人发现——每句话单独看都曾经是真的。


def _drift(name: str):
    return {rule.name: rule for rule in claims._drift_rules()}[name]


def _scoped(rule, document: Path):
    """把一条 D 族规则的 scope 改到临时文件上：`scan` 只按仓库内相对路径比。"""
    return dataclasses.replace(rule, scopes=(claims._relative(document),))


def test_a_bare_count_in_a_state_document_is_red(tmp_path: Path) -> None:
    """`2113 条用例 / 85 个文件` 这个形状必须红——它就是本轮三处过期的那个数。"""
    document = tmp_path / "README.md"
    document.write_text("后端 2113 条用例 / 85 个文件，前端 900 条用例 / 55 个 spec。\n",
                        encoding="utf-8")
    for name in ("drift:use_cases", "drift:test_files"):
        hits = claims.scan(fresh(), [_scoped(_drift(name), document)], [document])
        assert [item for item in hits if item.rule.name == name], name


def test_the_reference_form_is_what_we_want_written(tmp_path: Path) -> None:
    """"现跑 `pytest --collect-only`" 这类写法必须放行——否则这一族只会教人删信息。"""
    document = tmp_path / "README.md"
    document.write_text(
        "后端规模现跑 `python -m pytest --collect-only -q`，前端现跑 `npm test`；"
        "端点数见 `backend/openapi.json`；份数见 `git ls-files '*.md'`。\n",
        encoding="utf-8")
    hits = claims.scan(fresh(), [dataclasses.replace(rule, scopes=(claims._relative(document),))
                                for rule in claims._drift_rules()], [document])
    assert hits == [], hits


def test_the_drift_family_only_judges_the_state_documents(tmp_path: Path) -> None:
    """历史留档写的是"那一天是多少"，是正确的过去时——`docs/PLAN.md` 的版本表不进来。"""
    document = tmp_path / "PLAN.md"
    document.write_text("基线 1630+610 → 后端 2120（另有 29 subtest）、前端 900（55 文件）\n",
                        encoding="utf-8")
    assert claims.scan(fresh(), claims._drift_rules(), [document]) == []


def test_the_drift_probe_reads_the_wiring_not_a_constant(tmp_path: Path) -> None:
    """台账第 12 条的 probe 判**接线**：规则写了却没拼进 `main()`，这一类一行都不扫。

    与 `find_unwired.py` 同一条教训：代码在、没接上，本地与 CI 都是绿的。
    """
    script = _SCRIPT.read_text(encoding="utf-8")
    wired = tmp_path / "wired.py"
    wired.write_text(script, encoding="utf-8")
    values, _ = claims.probe_backlog_facts(script=wired)
    assert values["doc_drift_guard"] == claims.PRESENT

    unwired = tmp_path / "unwired.py"
    replaced = script.replace("+ _drift_rules()", "", 1)
    assert replaced != script, "样例失效：`main()` 里找不到 `+ _drift_rules()`"
    unwired.write_text(replaced, encoding="utf-8")
    values, _ = claims.probe_backlog_facts(script=unwired)
    assert values["doc_drift_guard"] == claims.ABSENT


def test_a_new_drift_candidate_outside_the_scope_is_red(tmp_path: Path) -> None:
    """scope 外**新增**一处裸数字 → 红。清单不许烂成"别查我"的白名单。

    这一族的意义全在这一条上：`DRIFT_SCOPE` 是手写名单，漏出去的地方以前**没人知道漏了
    多少**。枚举之后存量可见、新增即红、每条要理由——与 `find_unwired.py` 的棘轮同形。
    """
    document = tmp_path / "SOMEWHERE.md"
    document.write_text("后端 4321 条用例，全部通过。\n", encoding="utf-8")
    found, in_scope, problems = claims.drift_candidates([document])
    assert problems == [], problems
    assert in_scope == 0, "临时文件不在 DRIFT_SCOPE 里"
    assert found == {(claims._relative(document), "4321 条用例"): 1}, found
    hits = claims.check_drift_candidates(found)
    assert any("新的漂移候选" in hit for hit in hits), hits


def test_a_registered_candidate_that_vanished_is_red() -> None:
    """登记了而文档里已经没有 → 红。理由还在、结论没了，就是 `unwired_baseline` 那种烂账。"""
    hits = claims.check_drift_candidates({})
    assert any("登记失效" in hit for hit in hits), hits
    # 反过来：本仓真实读数是干净的（存量都登记了，没有新增）。
    files, _ = claims.doc_files()
    found, _in_scope, problems = claims.drift_candidates(files)
    assert problems == [], problems
    assert claims.check_drift_candidates(found) == [], claims.check_drift_candidates(found)


def test_the_enumeration_counts_what_the_scope_does_not_cover() -> None:
    """那个"scope 外还有多少"的数以前没人量——现在每次 `--gate` 都打印它。

    断言的是**这件事有读数**，不是"应当是几处"：处数会随别人的正常提交变，写死它就成了
    D 族要消灭的那种数。
    """
    files, _ = claims.doc_files()
    found, in_scope, _problems = claims.drift_candidates(files)
    assert in_scope >= 0
    assert sum(found.values()) > 0, "一份 md 都没枚举到，说明枚举本身空转了"
    outside = {path for path, _text in found}
    assert not [path for path in outside if path in claims.DRIFT_SCOPE], outside


def test_the_status_distribution_is_printed_not_stored(capsys) -> None:
    """分布由闸门现取并打印；`docs/BACKLOG.md` 里不许再手抄一份。

    上一版 §七 手抄过两行汇总，两轮就漂了，而且没有闸门判它。这条钉住的是"分布有地方
    出、而且只有一处"：打印里有，文档里没有。
    """
    claims.describe_backlog(REAL)
    printed = capsys.readouterr().out
    assert "状态分布：" in printed
    assert "机器判据：" in printed
    assert "卡点（blocker 真 = 还卡着）：" in printed
    text = _backlog_text()
    assert "状态分布：" not in text, "分布又被手抄进文档了——它会漂，交回给 `--facts`"


def test_the_drift_family_is_green_on_this_repo() -> None:
    """本仓 HEAD 上 scope 里那几份文档没有裸数字——"改成引用式"这一轮的直接读数。"""
    files, _ = claims.doc_files()
    scoped = [path for path in files
              if path.relative_to(claims.REPO_DIR).as_posix() in claims.DRIFT_SCOPE]
    assert len(scoped) == len(claims.DRIFT_SCOPE), scoped
    hits = claims.scan(fresh(), claims._drift_rules(), scoped)
    assert [item for item in hits if not item.excused] == [], \
        [f"{item.path}:{item.line} {item.rule.name}" for item in hits if not item.excused]
