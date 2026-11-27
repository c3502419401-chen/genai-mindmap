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
import re
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
        (root / "probe.py").write_text("", encoding="utf-8")
        return root

    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"x": {"status": "accepted"}}', encoding="utf-8")

    deprecated = build(tmp_path / "a", "已废弃：一级分支条数由内容决定")
    values, _ = claims.probe_backlog_facts(
        app_dir=deprecated, baseline=baseline, scripts_dir=tmp_path / "a")
    assert values["branch_arg_deprecated"] is True
    assert values["clip_label_retreats"] is True
    assert values["node_origin_field"] is True
    assert values["resolve_has_route"] is True

    alive = build(tmp_path / "b", "一级分支数量上限")
    values, _ = claims.probe_backlog_facts(
        app_dir=alive, baseline=baseline, scripts_dir=tmp_path / "b")
    assert values["branch_arg_deprecated"] is False


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
    """「待做」的判据是"它做完了会长什么样"，所以锚可以先不在——但「已做」不行。"""
    assert "scripts/check_doc_map.py" in _backlog_text()
    todo = [entry for entry in (REAL.backlog or ()) if entry.number == 10]
    assert todo and todo[0].status == claims.STATUS_TODO
    assert claims.check_backlog(REAL) == []


def test_the_backlog_is_scanned_even_before_git_tracks_it() -> None:
    """台账是本闸门自己的输入：没被 `git ls-files` 列出时也必须扫，否则块内禁句空转。"""
    files, problems = claims.doc_files()
    assert problems == [], problems
    names = [path.relative_to(claims.REPO_DIR).as_posix() for path in files]
    assert claims.BACKLOG_RELATIVE in names, names


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
    assert values["doc_drift_guard"] is True

    unwired = tmp_path / "unwired.py"
    replaced = script.replace("+ _drift_rules()", "", 1)
    assert replaced != script, "样例失效：`main()` 里找不到 `+ _drift_rules()`"
    unwired.write_text(replaced, encoding="utf-8")
    values, _ = claims.probe_backlog_facts(script=unwired)
    assert values["doc_drift_guard"] is False


def test_the_drift_family_is_green_on_this_repo() -> None:
    """本仓 HEAD 上这五份文档里没有裸数字了——这是"改成引用式"这一轮的直接读数。"""
    files, _ = claims.doc_files()
    scoped = [path for path in files
              if path.relative_to(claims.REPO_DIR).as_posix() in claims.DRIFT_SCOPE]
    assert len(scoped) == len(claims.DRIFT_SCOPE), scoped
    hits = claims.scan(fresh(), claims._drift_rules(), scoped)
    assert [item for item in hits if not item.excused] == [], \
        [f"{item.path}:{item.line} {item.rule.name}" for item in hits if not item.excused]
