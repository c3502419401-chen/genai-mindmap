#!/usr/bin/env python
"""文档断言与代码一致：把"文档说的事与代码对不对"变成一道能红的闸门。

## 为什么需要它

一天之内，同类错误撞了五次，五个都是**文档说错了代码现在做的事**：

  ① `START_HERE` 说 `mindmap_generate` 已删除、只剩分片
     → 实际 `_ALLOWED_BY_INTENT = {"create": ("mindmap_generate",)}`（活的那条）
  ② `knowledge/search` 说仍是桩（四处）→ 实际 `implemented=True`
  ③ 说没有登录/权限、接口裸奔（两处）→ 实际 `auth` 全套 + 门禁 + 403 都在
  ④ 说开发骨架/最小演示闭环（三处）→ 实际已上线
  ⑤ 说画布只有一套紧凑树布局 → 同一文件 7 行后又写了四种朝向

这些句子不会让任何用例变红：用例钉的是**代码**，而错的是**关于代码的话**。
上一轮人工清了 18 处，但清法是"人读文档、人去代码里核一遍"——那正是这次想消灭的东西。
`find_unwired.py` 把"用例测了函数、没测接线"变成机器判据；这里把"文档说的事与代码对不对"
变成机器判据，结构相同：**事实由代码读，禁句由事实定**。

## 核心：一张「事实 → 反句」表

每一行是一个三元组：

    (事实探针, 事实为真时全仓禁止的句子正则, 事实为假时禁止的句子正则)

**双向是关键。** 反句只在事实为真时生效，所以**代码一翻回去，闸门自动换掉它禁止的说法**：
`_ALLOWED_BY_INTENT["create"]` 真注册 `mindmap_generate` 时，禁的是"只剩分片"；
哪天把它换成分片，禁的就变成"`create` 只有 `mindmap_generate`"。
这不是"检查文档里有没有某句话"，而是"检查文档说的是不是代码现在做的事"。

## 事实由代码读，不由人写

五类探针，全部只读文本与 AST（外加一个受控子进程 import）：

  ① `ast` 读 `app/agent/tools.py` 的 `_ALLOWED_BY_INTENT` 字面量。
  ② **真的调** `build_registry(intent).names()`（子进程，`PYTHONPATH=.`，环境按
     `tests/conftest.py` 的隔离清单清空）。它和 ① 必须一致：`_ALLOWED_BY_INTENT` 里有、
     而 `_TOOL_SPECS` / `_TOOL_ORDER` 里没有的名字，注册表里**不会**出现——只看 ①
     会把"表里写着但实际没注册"判成真。
  ③ 扫 `app/**/*.py` 里 `X(..., implemented=False)` 的实例化，与 `openapi.json` 里
     声明了 `implemented` 属性的 schema 对表，得出**当前哪些 path 还是桩**。
  ④ 开关：`ast` 找出 `X_ENV = "MINDMAP_..."` + 零参 `*_enabled()` 函数，在**清空所有
     `MINDMAP_*` 与凭证**的子进程里真的调一次，读出"代码现在的默认值"。
     文档里凡说某开关"默认开/默认关"，一律与它比对——**全量**，不是抽验。
  ⑤ 门禁：`app/main.py` 里有 `add_middleware(GateMiddleware)`，**并且** `app/auth/gate.py`
     的 `__call__` 里真有返回 401 的分支。两条都要，理由见下一节。

## 第 ⑤ 条为什么是静态的（它算不算纯静态）

候选三条：a 查 `add_middleware(GateMiddleware)`；b 查 `auth_router` 有没有 `include_router`；
c 跑 `TestClient` 打一个受保护端点断言 401/403。

选了 **a 的加强版**（a + "门禁体里真有 401 分支"），理由三条：

  - b 不足：路由挂上了不等于有门禁（`auth/router.py` 自己就写着"这一批不拦任何请求"）。
  - c 最真，但它把这道闸门从"读文本"变成"跑应用"：`TestClient` 会触发 startup，
    结论就开始依赖外部的 Neo4j、模型服务、磁盘上的库文件。这道闸门要在 CI 的干净
    checkout 上与本地逐字节一致，不能有这个依赖。
  - 行为层已有运行时用例钉着：
    `tests/test_auth.py::test_the_gate_is_now_closed_for_anonymous_callers` 真的打了受保护
    端点。分工是——**行为归用例，接线归这里**。这条事实的 `evidence` 就写着那个用例名：
    用例被删掉，闸门会红。

结论：**⑤ 是纯静态的**（只读 `main.py` 与 `gate.py` 两棵 AST）。唯一不是纯静态的是 ② ④
的"受控子进程"：它们 `import app.*` 但不连库、不发网络请求（`build_registry` 的
`has_library=False` 把"去问库"那条路关掉），环境变量由本脚本按 `conftest` 清单清空。

## 扫描范围与豁免

    范围    `git ls-files '*.md'`，天然跳过 `run-backend.bat`、未跟踪的临时报告
    排除    `docs/research/`（别人仓库的 README 快照）、本文件的设计文档本身
    输出    `文件:行号:命中句:期望事实`

豁免写在文档里，必须**带事实名与理由**：

    <!-- claims-ok: create_registers_generate 历史留档：这一块引的是 11-16 的旧说法 -->

同一行或上一行有效。三条硬规矩：事实名必须真实存在、理由不能为空、`*`/`all` 不接受。
这是刻意的——`unwired_baseline.json` 的教训是"每条都要写理由与 evidence、证据文件被删
也红"，否则豁免会烂成一张"别查我"的白名单。

「开关默认值」一族（B 族）另有**历史语境放行**：那一族在文档里大量以"它曾经以 `X`
（默认关）的形式挂在代码里，现已删除"的形态出现。那不是错话，是关于过去的正确陈述，
逐条加注释会把文档淹掉，所以按语境（`曾经`/`旧版`/`已删`…）放行——但放行的每一条都会
在输出里单独列出，可见，不静默。

## 台账：第三类判据（它不是反句表）

`docs/BACKLOG.md` 记的是"还剩什么事没做"。这类文档最容易烂，烂法固定：**状态写在文档里，
而对不对靠人记**。所以它也被闸门判，判法与前两节不同：

    `parse_backlog` 读**块结构**（每条 `### N. ` 一个块，块里固定字段），`check_backlog` 判语义

**为什么不进反句表。** 反句表是逐行扫、正则是锚点；台账的十二条里"N 处写着待做"这种句子
在别的条目里可能正是对的，全文扫必然互串。块给了边界，`_backlog_rules` 的 `scope` /
`block` 两个字段就是把它交回 `scan` 的通道。

**四态**（待做 / 已做 / 半做 / 不做）而不是三态："离线侧确认、真模型侧没跑"压成"已做"
会让下一轮以为结案，压成"待做"会让已验过的那半重做一遍。

**判据与状态绑定，三种形态**：

    已做/半做   一条钉住该行为的用例，或一道会红的闸门        → 锚必须**现在就在**
    不做        一个可量阈值，或一条钉住"不做"的用例            → 必须带触发条件
    待做        它做完了会长什么样（先写判据，再派人做）        → 锚可以**暂时不在**

**那个循环问题怎么解的。** 账本的状态写在 md 里，而"状态对不对"要靠读代码，看起来是
自指。拆开就不自指了：

  - **状态↔事实**由 `probe` 判，探针只读**代码事实**（`description` 的文本、`Node` 有没有
    `origin`、`clip_label` 调不调 `_clip_index`），不读 md；
  - **判据在不在**由 `_anchor_candidates` 判，它只验"那个 `def` / 那个脚本还在不在"；
  - 两条**报不同的红**。用例被删掉时报的是「**失去判据**」，不是「这件事没做」——
    把它读成"没做"就是误报，而这个仓库有先例（`test_fold_tree.py` / `test_generate_evidence.py`
    都是随功能一起删的；反向的"用例删了、功能还在"一次也没有过，所以这条防线是为将来设的）；
  - **"这一行属于哪一条"**由块边界定，不由正则猜——误报的空间在这里被结构性消掉。

`probe` 为真 ⇔ 这件事已经在代码里，语义只有这一条，不加极性。所以"已做"必须有 probe
（**没有机器判据的"已做"不许进表**），`probe: none` 的条目只能是待做/不做，它们单列在
`--facts` 输出里：可判的判到底，判不了的摆出来，不假装。

## 第四类判据：会漂的数（`drift:` 一族）

前三族判"文档说的与代码做的是不是同一件事"，这一族判**"这句话会不会自己过期"**。
起因是一个数被抄到三处（`FEATURES_AND_OWNERS.md` 写"后端 2113 条用例 / 85 个文件，
前端 900 条 / 55 个 spec"，`PLAN-REWORDING.md` 与 `SCOPE.md` 各抄一份）：数早就是
别的了，而三处都还印着旧值——**每句话单独看都曾经是真的，错的是"它还写着现在"**。
前三族一条都发现不了它，因为没有任何代码事实对应"用例数应该是多少"。

所以这一族不立"数应当是多少"（那要每次 collect 一遍，十几秒，而读数下一秒就过期），
立的是**形状**：这类数不许以字面量出现，要写就写"怎么现取"。

    会随别人正常干活自然变化   → 引用式（用例数 / 文件数 / 端点数 / 开关数 / md 份数）
    不会（契约上限、算法常量）  → 允许写（50 步撤销、50_000 字、压测 P50），自有前三族管

判据是一句话：**这个数会不会因为别人正常提交而变。** 会变的写进文档，就是给自己排了
一件永远干不完的活——每轮手抄一遍，而漏抄不会报错。`holds` 恒为真：这一族没有事实
可读，"永远成立"就是它的语义。范围只到**断言现状**的那几份（`DRIFT_SCOPE`），历史
留档（`docs/PLAN.md` 的版本表、`docs/AGENT_PLAN.md` 的计划期数字）写的是正确的过去时，
不进来。还剩哪几份没搬，见 `docs/BACKLOG.md` 第 12 条。

## 跑法

    cd backend
    python scripts/check_doc_claims.py            # 每条事实的现值 + 全部命中
    python scripts/check_doc_claims.py --gate     # 闸门：命中/证据缺失/探针失败 → 退出码 1
    python scripts/check_doc_claims.py --facts    # 只打印事实现值

## 边界（当闸门之前必须承认的）

- **它假设"代码是对的、文档会漂"。** 反过来（文档对、代码是 bug）时，它会把"改文档"说成
  唯一出路——那正是这道闸门想消灭的那种机械服从。所以每条事实都带 `kind`：
  `contract` 必须给出**钉住它的用例**（`path::test_name`，脚本校验该函数真的存在），
  报错文案是「**冲突**：谁对要看证据」，不是"文档过期"；`observation` 没有用例背书，
  矛盾时才默认改文档。这是用户提的两条防线里的第 ① 条，落成了机器判据而不是纪律。
- 第 ② 条防线是**探针本身可证伪**：`tests/test_doc_claims.py` 把每个探针喂一个假的代码树，
  断言"代码变了，探针读出来的值跟着变"。缺了它，探针可以硬编码 True 而永远绿。
- 反句表不完备：它只能禁"人想到要禁的说法"。新增一类漂移要手动加一行，这不是 bug，
  是这张表的定义。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
APP = BACKEND_DIR / "app"
TESTS = BACKEND_DIR / "tests"
CONFTEST = TESTS / "conftest.py"
OPENAPI = BACKEND_DIR / "openapi.json"
FRONTEND = REPO_DIR / "frontend"
DEPLOY = REPO_DIR / "deploy"
TIDY_TS = FRONTEND / "src" / "canvas" / "layout" / "tidy.ts"
BACKLOG = REPO_DIR / "docs" / "BACKLOG.md"
BACKLOG_RELATIVE = "docs/BACKLOG.md"

#: 自己的设计文档不参与扫描：它整篇在**引用**那些错句当例子，扫它等于自指。
SELF_DOC = "DOC-CLAIMS.md"

#: 不算本仓断言来源的文档：`docs/research/` 存的是别人仓库的 README 快照，里面的
#: "默认关闭""已删除"说那个项目。与 `check_script_hygiene.py` 同一条排除。
NON_LOCAL_DOC_PREFIXES = ("docs/research/",)

#: 历史语境的标记词。只给「开关默认值」一族用，理由见模块 docstring。
HISTORY_WORDS = re.compile(
    r"曾经|历史|旧版|早先|原先|以前|已删|已废弃|不再|撤销|推翻|过时|上一版|旧说法|改回|迁移前")

#: 否定语境的标记词。所有规则都用：这些文档里有一大批**纠错句**——"这是已上线的系统，
#: 不是开发骨架""说某个东西死了，可能它活着"。它们与禁句逐字相同而意思相反，
#: 判红就等于把对的话判成错的，那会让闸门第一周就被人关掉。放行的命中会单独列出。
NEGATION_WORDS = re.compile(r"不是|并非|绝不|不再是|不再|别写|别用|不要说|不要写|并没有")

_TRUE_WORDS = {"开", "开启", "打开", "启用", "1", "true", "yes", "on"}
_FALSE_WORDS = {"关", "关闭", "禁用", "不启用", "0", "false", "no", "off"}

#: 承认的豁免注释。名字与理由都必填。
_CLAIMS_OK = re.compile(r"<!--\s*claims-ok\s*:\s*([^>]*?)\s*-->")

# ---------------------------------------------------------------- 台账
#
# `docs/BACKLOG.md` 是"还剩什么事没做"的唯一入口，它自己也要被闸门判。判法与
# 反句表不同：台账是**结构**（每条一个块、块里固定字段），所以不靠正则扫行，靠
# `parse_backlog` 读块。这样"哪一行属于哪一条"没有歧义，也就没有误报的空间。

#: 四态。三态装不下"离线侧确认、真模型侧没跑"，理由见 `docs/BACKLOG.md` §一。
STATUS_TODO = "待做"
STATUS_PART = "半做"
STATUS_DONE = "已做"
STATUS_WONT = "不做"
STATUSES = (STATUS_TODO, STATUS_PART, STATUS_DONE, STATUS_WONT)
#: 这两态必须给"什么时候回来做"。
NEEDS_TRIGGER = (STATUS_PART, STATUS_WONT)
#: 这两态必须有 `probe`：**"已做"不许没有机器判据**。
NEEDS_PROBE = (STATUS_DONE, STATUS_PART)
#: 判据锚必须**现在就在**的态。`待做` 允许锚先不存在——"先写判据，再派人做"。
NEEDS_LIVE_ANCHOR = (STATUS_DONE, STATUS_PART, STATUS_WONT)

#: "这件事还没做"的说法。事实为真（已经做了）时，本块里禁这一族。
BACKLOG_TAKE_BACK = (
    r"(?:待做|待改|该做|要做|还没做|尚未做|未做|下一轮)",
)
#: "这件事已经做完了"的说法。事实为假时，本块里禁这一族。
BACKLOG_SETTLED = (
    r"(?:已完成|已做|已落地|已销账|已接上|做完了|已实现)",
)

_ENTRY_HEAD = re.compile(r"^###\s+(\d+)\.\s+(.+?)\s*$")
_ENTRY_FIELD = re.compile(
    r"^-\s+\*\*(状态|probe|blocker_probe|判据|出处|触发条件|凭什么)\*\*\s*[：:]\s*(.*)$")
#: `路径::用例名`：判据锚的第一种形状。
_TEST_ANCHOR = re.compile(r"^([A-Za-z0-9_./-]+\.py)::([A-Za-z0-9_]+)$")
#: `python scripts/x.py ...`：判据锚的第二种形状（相对 `backend/`）。
_SCRIPT_ANCHOR = re.compile(r"(?:python\s+)?(scripts/[A-Za-z0-9_./-]+\.py)")
#: "不做"的判据可以是阈值：数字 + 单位。没有锚也要有它。
_THRESHOLD = re.compile(r"\d+(?:\.\d+)?\s*(?:%|次|条|秒|字|个|处|倍)")



# ---------------------------------------------------------------- 事实


@dataclass
class Facts:
    """所有探针的结果。字段为 `None` 表示**没读到**（闸门判红，不静默跳过）。"""

    #: ① AST 读到的 `_ALLOWED_BY_INTENT` 字面量。
    allowed_literal: dict[str, tuple[str, ...]] | None = None
    #: ② 子进程里 `build_registry(intent).names()` 的真实结果。
    registry: dict[str, tuple[str, ...]] | None = None
    #: ③ `X(..., implemented=<bool>)` 的实例化值，只留 openapi 里声明了该字段的 schema。
    stubs: dict[str, bool] | None = None
    #: ③ 由 stubs + openapi 对表得出的桩端点，形如 `POST /api/agent/tasks`。
    stub_endpoints: frozenset[str] | None = None
    #: ④ 开关的实测默认值（清空环境后真的调 `*_enabled()`）。
    env_switches: dict[str, bool] | None = None
    #: ④ 每个开关的判定函数所在文件（给 `evidence` 用，必须是真的路径）。
    switch_sources: dict[str, str] = field(default_factory=dict)
    #: ④ `app/**` 全文里出现过的 `MINDMAP_*` 名字（含注释）。宁宽不严：它只用来抓
    #: "整段代码里搜不到这个名字"。
    env_names_in_code: frozenset[str] = field(default_factory=frozenset)
    #: ⑤ `main.py` 挂了 GateMiddleware。
    gate_wired: bool | None = None
    #: ⑤ `gate.py` 的 `__call__` 里有 401/403 分支。
    gate_refuses: bool | None = None
    #: 上线部署件（systemd unit / nginx / install.sh）都在。
    deploy_ready: bool | None = None
    #: `tidy.ts` 里 `Direction` 的取值个数。
    directions: int | None = None
    #: 台账解析结果（`docs/BACKLOG.md` 的每一条）。`None` 表示没读到。
    backlog: tuple["Entry", ...] | None = None
    #: 台账每条 `probe` 的读数。键是探针名，不是条目编号——两个条目共用同一个探针是允许的。
    backlog_probes: dict[str, bool] = field(default_factory=dict)
    #: 台账每条 `blocker_probe` 的读数：**真 = 卡点还在**（与 `probe` 的极性相反）。
    backlog_blockers: dict[str, bool] = field(default_factory=dict)
    #: 探针自身的问题（读不到、结构变了）。非空即红。
    problems: list[str] = field(default_factory=list)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse(path: Path) -> ast.Module:
    return ast.parse(_read(path), filename=str(path))


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(BACKEND_DIR).with_suffix("").parts)


def _relative(path: Path) -> str:
    try:
        return path.relative_to(REPO_DIR).as_posix()
    except ValueError:
        # 用例会喂临时目录里的假代码树，那不是仓库内路径。
        return str(path)


def probe_allowed_literal(facts: Facts, tools_path: Path | None = None) -> None:
    """① `_ALLOWED_BY_INTENT` 的字面量。

    `tools_path` 只为用例存在：喂一份假 `tools.py` 就能验证"读的是文件，不是常量"。
    """
    path = tools_path or (APP / "agent" / "tools.py")
    try:
        tree = _parse(path)
    except (OSError, SyntaxError) as error:
        facts.problems.append(f"① 读不到 {_relative(path)}: {error}")
        return
    for node in tree.body:
        name = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name != "_ALLOWED_BY_INTENT":
            continue
        try:
            value = ast.literal_eval(node.value)
        except ValueError as error:
            facts.problems.append(f"① `_ALLOWED_BY_INTENT` 不再是字面量: {error}")
            return
        facts.allowed_literal = {key: tuple(items) for key, items in value.items()}
        return
    facts.problems.append("① `app/agent/tools.py` 里找不到 `_ALLOWED_BY_INTENT`")


def _conftest_env_names() -> tuple[list[str], str | None]:
    """`tests/conftest.py` 的 `CREDENTIALS` / `BEHAVIOUR`。

    不复制清单，而是**读它**：测试侧的隔离是唯一权威，这里跟着走。读不到就红——
    否则子进程继承宿主机环境，"默认值"立刻不可信。
    """
    try:
        tree = _parse(CONFTEST)
    except (OSError, SyntaxError) as error:
        return [], f"读不到 tests/conftest.py 的隔离清单: {error}"
    names: list[str] = []
    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in ("CREDENTIALS", "BEHAVIOUR"):
            continue
        try:
            names.extend(item for item in ast.literal_eval(node.value) if isinstance(item, str))
        except ValueError:
            return [], f"conftest.py 的 {target.id} 不再是字符串元组"
        found.add(target.id)
    missing = {"CREDENTIALS", "BEHAVIOUR"} - found
    if missing:
        return names, f"conftest.py 缺 {'/'.join(sorted(missing))}：隔离清单没了依据"
    return sorted(set(names)), None


#: ② ④ 的子进程。stdin 进开关清单，stdout 出一行 JSON。
#: 这是本脚本唯一"跑代码"的地方，环境由父进程按 conftest 清单清空。
_PROBE_CODE = r"""
import json, os, sys
payload = json.loads(sys.stdin.read() or "{}")
out = {"registry": {}, "switches": {}, "errors": []}
try:
    from app.agent.tools import build_registry
    for intent in ("create", "create_deep", "expand", "edit"):
        out["registry"][intent] = sorted(
            build_registry(intent, has_sources=False, has_library=False).names())
except Exception as error:  # noqa: BLE001 - 失败必须如实上报，不能静默
    out["errors"].append("build_registry: %s: %s" % (type(error).__name__, error))
import importlib
for env_name, module_name, func_name in payload.get("switches", []):
    try:
        module = importlib.import_module(module_name)
        out["switches"][env_name] = bool(getattr(module, func_name)())
    except Exception as error:  # noqa: BLE001
        out["errors"].append("%s (%s.%s): %s" % (env_name, module_name, func_name, error))
print("CLAIMS-JSON:" + json.dumps(out, ensure_ascii=False))
"""


def _clean_env(clear: list[str], db_path: str) -> dict[str, str]:
    """子进程的环境：清掉 `MINDMAP_*`、凭证与 conftest 的行为开关，库落临时文件。

    与 `tests/conftest.py` 前言同一条理由：宿主机上配了什么，都不能改变读数。
    """
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("MINDMAP_") and key not in clear}
    env["MINDMAP_DB_PATH"] = db_path
    env["PYTHONPATH"] = str(BACKEND_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


_COMPARE_OPS = (ast.In, ast.NotIn, ast.Eq, ast.NotEq)


def _env_of_arg(node: ast.AST, const_to_env: dict[str, str]) -> str | None:
    """`os.getenv(<这个名字>)` 里的名字：常量引用或字面量字符串。"""
    if isinstance(node, ast.Name):
        return const_to_env.get(node.id)
    if isinstance(node, ast.Constant) and isinstance(node.value, str) \
            and node.value.startswith("MINDMAP_"):
        return node.value
    return None


def _is_getenv(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "getenv" and bool(node.args)
    return False


def discover_switches() -> tuple[list[tuple[str, str, str]], list[str]]:
    """④ 找"开关判定函数"：`X_ENV = "MINDMAP_..."` + 零参函数里 `os.getenv(它) <比较>`。

    返回 `[(环境变量名, 模块名, 函数名)]`。这个仓库的写法是一致的：
        `SECTION_SLICE_ENV = "MINDMAP_SECTION_SLICE"` 之后
        `def section_slice_enabled() -> bool: return os.getenv(SECTION_SLICE_ENV, "").lower() in ...`

    三条判据，缺一不可——少一条就会把"读配置的函数"当成"开关"（实测：`_db_path()` 与
    `embedding_configured()` 都零参、都调 `os.getenv`，读出来的布尔值毫无意义）：
      1. 零参数；
      2. 返回注解是 `bool`，或函数名以 `_enabled` 结尾；
      3. **返回值是一次比较**（`in` / `not in` / `==` / `!=`），且比较的一侧是 `os.getenv`。
         这一条是核心：开关的本质就是"环境变量的值属于哪个集合"，而不是"值非空"。
    """
    found: dict[str, tuple[str, str, str]] = {}
    problems: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = _parse(path)
        except (OSError, SyntaxError) as error:
            problems.append(f"④ 读不到 {_relative(path)}: {error}")
            continue
        const_to_env: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name) \
                    and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str) \
                    and node.value.value.startswith("MINDMAP_"):
                const_to_env[node.targets[0].id] = node.value.value
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.args.args or node.args.kwonlyargs:
                continue
            annotated = isinstance(node.returns, ast.Name) and node.returns.id == "bool"
            if not (annotated or node.name.endswith("_enabled")):
                continue
            env_name = None
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Compare):
                    continue
                if not any(isinstance(op, _COMPARE_OPS) for op in inner.ops):
                    continue
                operands = [inner.left, *inner.comparators]
                for side in operands:
                    for call in ast.walk(side):
                        if _is_getenv(call):
                            env_name = _env_of_arg(call.args[0], const_to_env)
                            break
                    if env_name:
                        break
                if env_name:
                    break
            if env_name and env_name not in found:
                found[env_name] = (env_name, _module_name(path), node.name)
    return sorted(found.values()), problems


def probe_runtime(facts: Facts) -> None:
    """② ④ 子进程：注册表 + 开关默认值。失败即红，不降级。"""
    clear, problem = _conftest_env_names()
    if problem:
        facts.problems.append(problem)
    switches, problems = discover_switches()
    facts.problems.extend(problems)
    facts.switch_sources = {
        env_name: "backend/" + module_name.replace(".", "/") + ".py"
        for env_name, module_name, _ in switches}
    if not switches:
        facts.problems.append("④ 一个开关判定函数都没找到（写法变了？）")

    db_path = str(REPO_DIR / ".claims_probe.db")
    probe = subprocess.run(  # noqa: S603 - argv 固定，见模块 docstring
        [sys.executable, "-X", "utf8", "-c", _PROBE_CODE],
        input=json.dumps({"switches": switches}), capture_output=True, text=True,
        encoding="utf-8", cwd=str(BACKEND_DIR), env=_clean_env(clear, db_path), timeout=300)
    marker = "CLAIMS-JSON:"
    line = next((row for row in reversed(probe.stdout.splitlines()) if row.startswith(marker)), None)
    if line is None:
        tail = ((probe.stderr or "") + (probe.stdout or "")).strip().splitlines()[-1:]
        facts.problems.append(
            f"② 子进程没有输出读数（退出码 {probe.returncode}）：{tail or '无输出'}")
        return
    data = json.loads(line[len(marker):])
    facts.registry = {key: tuple(value) for key, value in data.get("registry", {}).items()}
    facts.env_switches = {key: bool(value) for key, value in data.get("switches", {}).items()}
    for error in data.get("errors", []):
        facts.problems.append(f"②/④ {error}")
    for env_name, _, _ in switches:
        if env_name not in (facts.env_switches or {}):
            facts.problems.append(f"④ 开关 {env_name} 没读到默认值")
    for suffix in ("", "-wal", "-shm"):
        leftover = Path(db_path + suffix)
        if leftover.exists():
            leftover.unlink()
    # ① 与 ② 必须一致：表里写着而 specs 里没有的名字不会出现在注册表里。
    if facts.allowed_literal is not None and facts.registry is not None:
        for intent, names in facts.allowed_literal.items():
            actual = facts.registry.get(intent)
            if actual is None:
                facts.problems.append(f"② `_ALLOWED_BY_INTENT` 有意图 `{intent}`，注册表没问到")
            elif set(names) - set(actual):
                facts.problems.append(
                    f"② 意图 `{intent}` 在表里写着 {sorted(set(names) - set(actual))}，"
                    "注册表里没有——表与 `_TOOL_SPECS` / `_TOOL_ORDER` 脱节了")


def probe_stubs(facts: Facts) -> None:
    """③ 桩标记：`X(..., implemented=False)` 与 openapi 里声明该字段的 schema 对表。

    只认"openapi 里真的声明了 `implemented` 属性"的 schema：`implemented` 是普通字段名，
    别的模型也可能碰巧用它，对表能把它挡在外面。
    """
    try:
        spec = json.loads(_read(OPENAPI))
    except (OSError, json.JSONDecodeError) as error:
        facts.problems.append(f"③ 读不到 backend/openapi.json: {error}")
        return
    schemas = spec.get("components", {}).get("schemas", {})
    declared = {name for name, body in schemas.items()
                if isinstance(body, dict) and "implemented" in (body.get("properties") or {})}
    if not declared:
        facts.problems.append("③ openapi.json 里没有声明 `implemented` 的 schema（写法变了？）")
        return

    values: dict[str, bool] = {}
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = _parse(path)
        except (OSError, SyntaxError) as error:
            facts.problems.append(f"③ 读不到 {_relative(path)}: {error}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else None)
            if name not in declared:
                continue
            for keyword in node.keywords:
                if keyword.arg == "implemented" and isinstance(keyword.value, ast.Constant) \
                        and isinstance(keyword.value.value, bool):
                    values[name] = keyword.value.value
    facts.stubs = values

    endpoints: set[str] = set()
    for route, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            response = (operation.get("responses") or {}).get("200") or {}
            ref = ((response.get("content") or {}).get("application/json") or {}).get("schema", {})
            name = str(ref.get("$ref", "")).rsplit("/", 1)[-1]
            if name in values and values[name] is False:
                endpoints.add(f"{method.upper()} {route}")
    facts.stub_endpoints = frozenset(endpoints)


def probe_env_names(facts: Facts) -> None:
    """④ 兜底："这些 `MINDMAP_*` 名字代码里还真的在读吗"。

    只认**被当作环境变量用**的地方：`os.getenv(...)` 的实参、`X_ENV = "MINDMAP_..."` 这类
    常量表、`os.environ.get(...)` / `os.environ[...]`。注释与 docstring 里的提及不算——
    "`MINDMAP_SOURCE_MODE` 已删除"这句话几乎必然出现在某个 docstring 里，把它算成"还活着"
    会让这条事实永远为假。
    """
    names: set[str] = set()
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = _parse(path)
        except (OSError, SyntaxError) as error:
            facts.problems.append(f"④ 读不到 {_relative(path)}: {error}")
            continue
        const_to_env: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name) \
                    and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str) \
                    and node.value.value.startswith("MINDMAP_"):
                const_to_env[node.targets[0].id] = node.value.value
        names.update(const_to_env.values())
        for node in ast.walk(tree):
            if _is_getenv(node):
                resolved = _env_of_arg(node.args[0], const_to_env)
                if resolved:
                    names.add(resolved)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "get" and node.args \
                    and isinstance(node.func.value, ast.Attribute) \
                    and node.func.value.attr == "environ":
                resolved = _env_of_arg(node.args[0], const_to_env)
                if resolved:
                    names.add(resolved)
            elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) \
                    and node.value.attr == "environ":
                resolved = _env_of_arg(node.slice, const_to_env)
                if resolved:
                    names.add(resolved)
    facts.env_names_in_code = frozenset(names)


def probe_gate(facts: Facts) -> None:
    """⑤ 门禁：接线在 `main.py`，拒绝分支在 `gate.py`。两条都要。"""
    try:
        main_tree = _parse(APP / "main.py")
    except (OSError, SyntaxError) as error:
        facts.problems.append(f"⑤ 读不到 app/main.py: {error}")
        return
    wired = False
    for node in ast.walk(main_tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "add_middleware" and node.args:
            first = node.args[0]
            name = first.id if isinstance(first, ast.Name) else (
                first.attr if isinstance(first, ast.Attribute) else None)
            if name == "GateMiddleware":
                wired = True
    facts.gate_wired = wired

    try:
        gate_tree = _parse(APP / "auth" / "gate.py")
    except (OSError, SyntaxError) as error:
        facts.problems.append(f"⑤ 读不到 app/auth/gate.py: {error}")
        return
    refuses = False
    for node in ast.walk(gate_tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                or node.name != "__call__":
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.keyword) and inner.arg == "status_code" \
                    and isinstance(inner.value, ast.Constant) \
                    and inner.value.value in (401, 403):
                refuses = True
    facts.gate_refuses = refuses


def probe_deploy(facts: Facts, deploy_dir: Path | None = None) -> None:
    """上线件：systemd unit + nginx.conf + install.sh 都在。

    **诚实声明**：它判的是"仓库里带着一套上线部署件"，不是"域名此刻可达"。后者要打网络，
    这道闸门不要网络。文档里"已上线/开发骨架"这类句子由它管，是因为这句话在仓库里的
    **可判部分**就是这三件东西在不在。
    """
    root = deploy_dir or DEPLOY
    expected = ("genai-mindmap.service", "nginx.conf", "install.sh")
    facts.deploy_ready = all((root / name).exists() for name in expected)


def probe_directions(facts: Facts, tidy_path: Path | None = None) -> None:
    """四种朝向：`Direction` 的取值个数（这是 TS，用正则读联合类型）。"""
    path = tidy_path or TIDY_TS
    try:
        text = _read(path)
    except OSError as error:
        facts.problems.append(f"读不到 {_relative(path)}: {error}")
        return
    match = re.search(r"export type Direction\s*=\s*([^;\n]+)", text)
    if not match:
        facts.problems.append(f"{_relative(path)} 里找不到 `export type Direction = ...`")
        return
    facts.directions = len(re.findall(r"'[a-z]+'", match.group(1)))


@dataclass(frozen=True)
class Entry:
    """`docs/BACKLOG.md` 的一条账目。

    台账是**结构**而不是自由文本：每条一个 `### N. ` 块、块里固定字段。所以判它走
    `parse_backlog` 读块，不走反句表扫行——"这一行属于哪一条"没有歧义，也就没有
    台账内部互串的可能（`_backlog_rules` 的 `block` 就是为此存在的）。
    """

    number: int
    title: str
    status: str
    probe: str
    criterion: str
    source: str
    trigger: str
    #: 「卡点还在不在」的探针名。**极性反着**：真 = 卡点仍在，假 = 可以转已做了。
    #: 半做必填（另一半卡在什么必须能机器判），待做可给，已做/不做不许给。
    blocker_probe: str = ""
    #: 标题行与块末行（1-based，含两端）。`scan` 用它把规则限定在自己那一块里。
    start: int = 0
    end: int = 0

    @property
    def where(self) -> str:
        return f"{BACKLOG_RELATIVE} 第 {self.number} 条"


def _blocker_name(raw: str) -> str:
    """`- **blocker_probe**：`名字` —— 为什么卡在这里` → `名字`。

    后半句是给人读的（卡点凭什么成立），会被 `parse_backlog` 的续行逻辑接进同一个字段值，
    所以名字从**反引号**里取：那一栏允许带解释，名字不允许有歧义。
    """
    text = raw.strip()
    match = re.match(r"`([A-Za-z0-9_]+)`", text)
    if match:
        return match.group(1)
    return text.split()[0] if text else ""


def parse_backlog(text: str) -> tuple[list[Entry], list[str]]:
    """读台账的块结构 → `(条目, 结构问题)`。只做结构，不做语义（那是 `check_backlog`）。"""
    lines = text.splitlines()
    problems: list[str] = []
    heads: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        match = _ENTRY_HEAD.match(line)
        if match:
            heads.append((index, match))
    if not heads:
        return [], [f"{BACKLOG_RELATIVE} 里一条账目都没解析到（`### N. ` 的写法变了？）"]

    entries: list[Entry] = []
    for position, (start, match) in enumerate(heads):
        stop = heads[position + 1][0] if position + 1 < len(heads) else len(lines)
        for index in range(start + 1, stop):
            if lines[index].startswith("## "):
                stop = index
                break
        fields: dict[str, str] = {}
        current: str | None = None
        for index in range(start + 1, stop):
            stripped = lines[index].strip()
            field = _ENTRY_FIELD.match(stripped)
            if field:
                current = field.group(1)
                fields[current] = field.group(2).strip()
            elif current and stripped and not stripped.startswith(("-", "#", "<!--")):
                # 中文续行：字段值跨行时直接接上（中间不加空格）。
                fields[current] = f"{fields[current]}{stripped}"
        number = int(match.group(1))
        entry = Entry(
            number=number,
            title=match.group(2),
            status=fields.get("状态", "").strip(),
            probe=fields.get("probe", "").strip().strip("`").strip(),
            criterion=fields.get("判据", "").strip(),
            source=fields.get("出处", "").strip(),
            trigger=fields.get("触发条件", "").strip(),
            blocker_probe=_blocker_name(fields.get("blocker_probe", "")),
            start=start + 1,
            end=stop,
        )
        entries.append(entry)
        missing = [name for name in ("状态", "probe", "判据", "出处")
                   if not getattr(entry, {"状态": "status", "probe": "probe",
                                          "判据": "criterion", "出处": "source"}[name])]
        if missing:
            problems.append(f"{entry.where} 缺字段：{'、'.join(missing)}")
        if entry.status and entry.status not in STATUSES:
            problems.append(f"{entry.where} 的状态 `{entry.status}` 不在四态里"
                            f"（{'/'.join(STATUSES)}）")
    numbers = [entry.number for entry in entries]
    if numbers != list(range(1, len(entries) + 1)):
        # 跳号会让别处的"第 N 条"失锚——`WIRING-GAPS` §13.3 那种引用就是这么烂掉的。
        problems.append(f"{BACKLOG_RELATIVE} 的编号不是 1..{len(entries)}：{numbers}")
    return entries, problems


#: 台账探针名 → 它在判什么。名字必须在这里注册；写错名字会红（不是静默跳过）。
BACKLOG_PROBE_DOC: dict[str, str] = {
    "branch_arg_deprecated":
        "两处 `branches` 的 `description` 都以「已废弃」开头，且不含「上限」",
    "clip_label_retreats":
        "`clip_label` 的切点经 `_clip_index` 回退，不是裸的 `text[:limit]`",
    "node_origin_field":
        "`Node` 上有 `origin` 字段",
    "resolve_has_route":
        "`knowledge/router.py` 真的调 `graph_resolve.resolve_entities`",
    "unwired_no_todo":
        "`unwired_baseline.json` 里没有 `status=todo` 的条目",
    "thin_material_offline":
        "`OUTLINE_FEW_BLOCKS_NOTE` 与薄材料探针脚本都在",
    "doc_drift_guard":
        "D 族（会漂的数）真的被 `--gate` 跑：`_drift_rules` 在，且 `main()` 拼进了 rules",
    "doc_map_gate":
        "`scripts/check_doc_map.py` 的闸门真的在判：有 `--gate` 开关，`main()` 调了 "
        "`check()`，且 `check()` 里接上了 `reachable()`（入口两跳可达）",
    "cross_reference_wired":
        "`CROSS_REFERENCE_RULES` 在，`_generate` 里它与 `GRAPH_RELATION_RULES` 是 "
        "if/else 互斥，且收 `relations` 那一支不再只属于 `graph_mode`",
}


#: `blocker_probe` 的名字 → 它读的**是哪一处卡点**，以及"卡点消失"长什么样。
#:
#: 这一族与 `BACKLOG_PROBE_DOC` **极性相反**，这不是笔误：
#:
#:     probe          真 ⇔ 这件事已经在代码里          （所以"已做"必须为真）
#:     blocker_probe  真 ⇔ **卡点还在**                 （所以"半做"必须为真）
#:
#: 为什么需要它：`probe` 只答"已验的那一半在不在"，而"半做"的含义是**另一半卡在某个
#: 具体的东西上**。上一轮那两条半做暴露了缺口——第 6 条把卡点写成"缺
#: `MINDMAP_LLM_API_KEY`"，而那个 key 一直在 `run-backend.bat` 里（`.env` 里没有，
#: bat 里有）。也就是说：**卡点本身是一句可以被机器否掉的话**，而当时没人判它，于是
#: 一条本来可以推进的账目在"缺凭证"这个错误前提上停了两轮。
#:
#: 判据只有一句：**"卡点在不在"是代码事实，"活干完没有"要人跑。** 前者可机器判，
#: 后者不可——所以 `blocker_probe` 读出假时，闸门只说"可以转已做了"，**不自动转**。
BLOCKER_PROBE_DOC: dict[str, str] = {
    "cross_reference_quota_sampling_stale":
        "`backend/app/agent/tools.py` 的最后一次提交**晚于** "
        "`docs/CROSS-BRANCH-DUPLICATES.md` 的 → 生成侧规则改过、采样记录没跟着重跑，"
        "「8 条够不够」仍是按旧代码量出来的。假=可以重判配额并结案",
    "style_branch_attribution_absent":
        "`scripts/measure_branch_budget.py` 里没有任何 `style_profile` 归因切分，且 "
        "`app/prefs/style.py` 的 `branches` 仍是单值汇总 → 那两格无从归因。"
        "假=归因写出来了，可以真跑",
}


def _field_descriptions(tree: ast.Module, field_name: str) -> list[str]:
    """所有类里 `field_name: X = SomeModel(description="…")` 的 description 字面量。"""
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign) \
                    or not isinstance(statement.target, ast.Name) \
                    or statement.target.id != field_name \
                    or not isinstance(statement.value, ast.Call):
                continue
            for keyword in statement.value.keywords:
                if keyword.arg == "description" and isinstance(keyword.value, ast.Constant) \
                        and isinstance(keyword.value.value, str):
                    out.append(keyword.value.value)
    return out


def _calls_named(tree: ast.Module, function: str, called: str) -> bool:
    """`function` 的函数体里调过 `called` 吗。"""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                or node.name != function:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else None)
            if name == called:
                return True
    return False


def _drift_guard_wired(script: Path) -> bool:
    """D 族真的被 `--gate` 跑吗：模块里定义 `_drift_rules`，且 `main()` 把它拼进 rules。

    这与 `find_unwired.py` 判的是同一件事——**代码在、没接上**。`_drift_rules` 写好了
    却忘了拼进 `main()`，这一类就一行都不扫，而本地与 CI 都是绿的。所以台账第 12 条
    的 probe 读的不是常量，是这层接线。
    """
    try:
        tree = _parse(script)
    except (OSError, SyntaxError):
        return False
    if not any(isinstance(node, ast.FunctionDef) and node.name == "_drift_rules"
               for node in tree.body):
        return False
    for node in tree.body:
        if not (isinstance(node, ast.FunctionDef) and node.name == "main"):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else None)
            if name == "_drift_rules":
                return True
    return False


def _mentions(nodes: list[ast.AST], name: str) -> bool:
    """这棵子树里出现过 `name` 吗（`Name.id` 或 `Attribute.attr`）。"""
    for node in nodes:
        for item in ast.walk(node):
            if isinstance(item, ast.Name) and item.id == name:
                return True
            if isinstance(item, ast.Attribute) and item.attr == name:
                return True
    return False


def _doc_map_gate_wired(script: Path) -> bool:
    """分级闸门真的在判吗：有 `--gate` 开关，`main()` 调了 `check()`，`check()` 里接了
    `reachable()`（"入口两跳可达"那一条）。

    与 `_drift_guard_wired` 同一条要求——台账第 10 条的判据是"分级建成"，而建成之后最可能
    的失败形态不是"脚本没了"（那会被 `check_script_hygiene.py` 的 C 档抓到），是**脚本在、
    判据没接上**：`--write` 照样能跑，`--gate` 什么都不判，于是本地与 CI 全绿，而那份清单
    悄悄退化成一张没人校验的手写目录——正是这条账目当初想消灭的东西。
    """
    try:
        tree = _parse(script)
    except (OSError, SyntaxError):
        return False
    has_gate = False
    calls_check = False
    for node in tree.body:
        if not (isinstance(node, ast.FunctionDef) and node.name == "main"):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else None)
            if name == "check":
                calls_check = True
            elif name == "add_argument" and inner.args \
                    and isinstance(inner.args[0], ast.Constant) \
                    and inner.args[0].value == "--gate":
                has_gate = True
    if not (has_gate and calls_check):
        return False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "check":
            return _mentions([node], "reachable")
    return False


def _relation_gate_accepts(test: ast.AST) -> bool:
    """这个 `if` 的条件会在"只有模型写了、且不是 graph_mode"时成立吗。

    两种写法都算：`graph_mode or raw_relations`（Or 且两个名字都在）与彻底解耦的
    （条件里没有 `graph_mode`）。`graph_mode and raw_relations` **不算**——那与
    `if graph_mode:` 一样，非图谱路一条都收不到，只是看起来像修好了。
    """
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        names = {node.id for node in ast.walk(test) if isinstance(node, ast.Name)}
        if "raw_relations" in names and "graph_mode" in names:
            return True
    return not _mentions([test], "graph_mode")


def _cross_reference_wired(tree: ast.Module) -> bool:
    """交叉指认真的接上了吗：常量在 + 与图谱那份互斥 + 非 graph_mode 也会收 `relations`。

    三件事分开判，因为它们分别会单独坏掉，而坏掉之后**表现完全一样**（模型写了
    `relations`，画布上是一根线都没有，或者整棵树判废）：

      - 常量被删 / 改名：`_generate` 里 `system += ` 一个不存在的名字会 NameError，
        这条不用探针也能发现；但"常量还在、没人引用"就是零引用，必须判；
      - 两条关系规则**不是** if/else：同一个 `relations` 字段会收到两套上限（30 / 8）
        与两套 kind（六值 / 两个值），模型二选一地违背其中一份；
      - 收 `relations` 的那一支还锁在 `graph_mode` 里：这正是上一版的形状——
        规则写了，非图谱路一条都收不到。

    `if graph_mode or raw_relations:` 这个 test 是第三件事的判据：它的条件里出现了
    `raw_relations`（不是只有 `graph_mode`），说明"模型写了就收"。
    """
    if not _mentions([tree], "CROSS_REFERENCE_RULES"):
        return False
    exclusive = False
    receives = False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                or node.name != "_generate":
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.If):
                continue
            if (_mentions(inner.body, "GRAPH_RELATION_RULES")
                    and _mentions(inner.orelse, "CROSS_REFERENCE_RULES")):
                exclusive = True
            if (_mentions([inner.test], "raw_relations")
                    and _relation_gate_accepts(inner.test)
                    and _mentions(inner.body, "_relations_from")):
                receives = True
    return exclusive and receives


def probe_backlog_facts(app_dir: Path | None = None,
                        baseline: Path | None = None,
                        scripts_dir: Path | None = None,
                        script: Path | None = None) -> tuple[dict[str, bool], list[str]]:
    """台账里六条 `probe` 的读数。

    三个参数只为用例存在：喂一份假代码树，读数必须跟着变——否则探针可以硬编码 `True`
    而永远绿（与 `probe_allowed_literal` 同一条要求）。
    """
    app = app_dir or APP
    values: dict[str, bool] = {}
    problems: list[str] = []

    def read(path: Path) -> ast.Module | None:
        try:
            return _parse(path)
        except (OSError, SyntaxError) as error:
            problems.append(f"台账 {_relative(path)}: {error}")
            return None

    tools = read(app / "agent" / "tools.py")
    if tools is not None:
        described = _field_descriptions(tools, "branches")
        values["branch_arg_deprecated"] = bool(described) and all(
            "已废弃" in text and "上限" not in text for text in described)
        has_note = any(
            isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "OUTLINE_FEW_BLOCKS_NOTE"
            for node in ast.walk(tools)) or any(
            isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
            and node.target.id == "OUTLINE_FEW_BLOCKS_NOTE"
            for node in ast.walk(tools))
        probe_script = (scripts_dir or (BACKEND_DIR / "scripts")) \
            / "_probe_thin_material_branches.py"
        values["thin_material_offline"] = has_note and probe_script.exists()
        values["cross_reference_wired"] = _cross_reference_wired(tools)

    contracts = read(app / "contracts" / "mindmap.py")
    if contracts is not None:
        defines_helper = any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_clip_index" for node in ast.walk(contracts))
        values["clip_label_retreats"] = defines_helper and _calls_named(
            contracts, "clip_label", "_clip_index")
        has_origin = False
        for node in ast.walk(contracts):
            if not isinstance(node, ast.ClassDef) or node.name != "Node":
                continue
            for statement in node.body:
                if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) \
                        and statement.target.id == "origin":
                    has_origin = True
        values["node_origin_field"] = has_origin

    router = read(app / "knowledge" / "router.py")
    if router is not None:
        values["resolve_has_route"] = any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "resolve_entities"
            for node in ast.walk(router))

    baseline_path = baseline or (BACKEND_DIR / "scripts" / "acc3" / "unwired_baseline.json")
    try:
        data = json.loads(_read(baseline_path))
    except (OSError, json.JSONDecodeError) as error:
        problems.append(f"台账 读不到 {_relative(baseline_path)}: {error}")
    else:
        # 基线是 `名字 → 条目` 的字典；容一手数组形态（顺序无关，只数 `status`）。
        rows = (list(data.items()) if isinstance(data, dict)
                else [(str(index), row) for index, row in enumerate(data)])
        todo = [name for name, row in rows
                if isinstance(row, dict) and row.get("status") == "todo"]
        values["unwired_no_todo"] = not todo

    values["doc_drift_guard"] = _drift_guard_wired(script or Path(__file__).resolve())
    values["doc_map_gate"] = _doc_map_gate_wired(
        (scripts_dir or (BACKEND_DIR / "scripts")) / "check_doc_map.py")

    for name in BACKLOG_PROBE_DOC:
        if name not in values:
            problems.append(f"台账探针 `{name}` 没读到读数")
    return values, problems


def _last_commit_seconds(repo_dir: Path, relative: str) -> int | None:
    """`relative` 在 `repo_dir` 里的最后一次提交时间（unix 秒）。读不到 → `None`。

    用**提交时间**而不是文件 mtime：干净 checkout 上 mtime 是 checkout 那一刻，"产物
    比补丁新"会永远为真——那是假绿，比没有判据更坏。提交时间在本地与 CI 上一致（CI
    的 `fetch-depth: 0` 正是为这类判据配的）。
    """
    try:
        done = subprocess.run(  # noqa: S603 - argv 固定
            ["git", "log", "-1", "--format=%ct", "--", relative],  # noqa: S607
            cwd=str(repo_dir), capture_output=True, text=True, encoding="utf-8", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    text = (done.stdout or "").strip()
    return int(text) if text.isdigit() else None


def probe_blockers(app_dir: Path | None = None,
                   scripts_dir: Path | None = None,
                   repo_dir: Path | None = None) -> tuple[dict[str, bool], list[str]]:
    """台账里 `blocker_probe` 的读数：**真 = 卡点还在**（与 `probe` 极性相反）。

    `repo_dir` 只给时序型那条用：git 只认仓库相对路径，所以它必须是一个真 git 仓库。
    """
    app = app_dir or APP
    scripts = scripts_dir or (BACKEND_DIR / "scripts")
    repo = repo_dir or REPO_DIR
    values: dict[str, bool] = {}
    problems: list[str] = []

    tools_commit = _last_commit_seconds(repo, "backend/app/agent/tools.py")
    record_commit = _last_commit_seconds(repo, "docs/CROSS-BRANCH-DUPLICATES.md")
    if tools_commit is None or record_commit is None:
        problems.append(
            "台账 blocker `cross_reference_quota_sampling_stale` 读不到提交时间"
            "（不是 git 仓库，或 checkout 没有 `fetch-depth: 0`）——"
            "它是时序型判据，读不到就是不可判，不许当成卡点已消失")
    else:
        # 生成侧规则晚于采样记录 ⇒ 采样是按旧代码做的 ⇒ 「8 条够不够」没按现状量过。
        values["cross_reference_quota_sampling_stale"] = tools_commit > record_commit

    budget = scripts / "measure_branch_budget.py"
    style = app / "prefs" / "style.py"
    try:
        attributed = "style_profile" in _read(budget)
    except OSError as error:
        problems.append(f"台账 blocker `style_branch_attribution_absent` 读不到 "
                        f"{_relative(budget)}: {error}")
    else:
        try:
            tree = _parse(style)
        except (OSError, SyntaxError) as error:
            problems.append(f"台账 blocker `style_branch_attribution_absent` 读不到 "
                            f"{_relative(style)}: {error}")
        else:
            # `branches` 还是单值汇总（`_number("branches", [...])`）：归因没有分桶。
            single = any(
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_number" and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "branches"
                for node in ast.walk(tree))
            values["style_branch_attribution_absent"] = (not attributed) and single

    for name in BLOCKER_PROBE_DOC:
        if name not in values:
            problems.append(f"台账 blocker `{name}` 没读到读数")
    return values, problems


def probe_backlog(facts: Facts, path: Path | None = None,
                  app_dir: Path | None = None) -> None:
    """读 `docs/BACKLOG.md`：块结构 + probe 与 blocker_probe 的读数。读不到即红。"""
    target = path or BACKLOG
    try:
        text = _read(target)
    except OSError as error:
        facts.problems.append(f"台账读不到 {_relative(target)}: {error}")
        return
    entries, problems = parse_backlog(text)
    facts.problems.extend(problems)
    facts.backlog = tuple(entries)
    values, probe_problems = probe_backlog_facts(app_dir)
    facts.backlog_probes = values
    facts.problems.extend(probe_problems)
    blockers, blocker_problems = probe_blockers(app_dir)
    facts.backlog_blockers = blockers
    facts.problems.extend(blocker_problems)


def _anchor_candidates(text: str) -> list[tuple[str, bool]]:
    """判据里能当锚的反引号片段 → `[(锚, 现在是否存在)]`。

    两种形状：`路径::用例名`（要求那个 `def` 真的在文件里）与
    `python scripts/x.py …`（要求脚本文件在）。路径引用（`docs/x.md`）不算锚。
    """
    out: list[tuple[str, bool]] = []
    for token in re.findall(r"`([^`]+)`", text):
        token = token.strip()
        test = _TEST_ANCHOR.match(token)
        if test:
            path = REPO_DIR / test.group(1)
            exists = path.exists() and test.group(2) in set(
                _TEST_DEF.findall(_read(path)))
            out.append((token, exists))
            continue
        script = _SCRIPT_ANCHOR.search(token)
        if script:
            out.append((token, (BACKEND_DIR / script.group(1)).exists()))
    return out


def pending_anchors(facts: Facts) -> list[str]:
    """「待做」条目里，判据锚**现在还不存在**的那些。

    这不是红——`待做` 的判据本来就是"它做完了会长什么样"，锚可以晚于条目出现。但必须
    **可见**：不列出来的话，"待做 + 一个永远不会出现的锚"就成了没人管的一格，那正是
    这条规矩想防的东西。
    """
    out: list[str] = []
    for entry in facts.backlog or ():
        if entry.status != STATUS_TODO:
            continue
        anchors = _anchor_candidates(entry.criterion)
        missing = [token for token, exists in anchors if not exists]
        if missing:
            out.append(f"第 {entry.number} 条：{missing}")
    return out


def _blocker_problems(facts: Facts, entry: Entry) -> list[str]:
    """`blocker_probe` 的形态校验。**不判极性**——卡点消失不是红，是提示。

    三条红，都是"这条账目没法自己报状态"的形状：

      · 半做却没有 `blocker_probe` → 另一半卡在什么**不可机器判**，下一轮只能靠人重读；
      · 名字没注册 → 写错名字会静默通过，等于没给；
      · 已做/不做却给了 → 那两态没有"卡点"可言，这一栏会退化成一句永远为真的备注。
    """
    problems: list[str] = []
    name = entry.blocker_probe
    if not name:
        if entry.status == STATUS_PART:
            problems.append(
                f"{entry.where} 是{STATUS_PART}，必须给 `blocker_probe`——"
                "半做的另一半卡在什么，要能机器判（上一轮第 6 条把卡点写成「缺 "
                "`MINDMAP_LLM_API_KEY`」，而那个 key 一直在 `run-backend.bat` 里："
                "卡点本身是一句可以被否掉的话）")
        return problems
    if entry.status in (STATUS_DONE, STATUS_WONT):
        problems.append(
            f"{entry.where} 是{entry.status}，不该有 `blocker_probe`："
            "这两态没有卡点，写上去就是一句永远为真的备注")
        return problems
    if name not in BLOCKER_PROBE_DOC:
        problems.append(f"{entry.where} 的 blocker_probe `{name}` 没有注册"
                        f"（已注册：{'/'.join(sorted(BLOCKER_PROBE_DOC))}）")
        return problems
    if name not in facts.backlog_blockers:
        problems.append(f"{entry.where} 的 blocker_probe `{name}` 没读到读数")
    return problems


def blocker_hints(facts: Facts) -> list[str]:
    """卡点**已经消失**的条目：可以转已做了，但**不自动转**。

    为什么只提示：`blocker_probe` 答的是"卡点还在不在"，而"已做"要人真跑一遍
    （第 9 条那种采样、第 11 条那两格的归因）。把提示写成自动转就是把"卡点消失"
    当成"活干完"，那正是这台台账要治的病换了个方向。
    """
    out: list[str] = []
    for entry in facts.backlog or ():
        if not entry.blocker_probe or entry.status == STATUS_DONE:
            continue
        value = facts.backlog_blockers.get(entry.blocker_probe)
        if value is False:
            out.append(f"第 {entry.number} 条的卡点已消失（`{entry.blocker_probe}` 读到假）"
                       f"——可以转「已做」，但要先真跑一遍再改状态")
    return out


def check_backlog(facts: Facts) -> list[str]:
    """台账的语义校验：判据形态 / 状态与 probe 一致 / 该给的字段给了。

    三种红，话不同，处理方式也不同：

      · **判据缺失**（既没有锚也没有阈值）→ 这条账目永远会漂，不许进表；
      · **判据锚不在了** → 这条账目失去判据（用例被删、脚本改名），先补判据或改状态；
        **这不等于"这件事没做"**——把它读成"没做"就是误报；
      · **状态与事实不符** → 谁对要看证据：`holds` 为假的"已做"可能是代码被翻回去了。
    """
    entries = facts.backlog
    if entries is None:
        return []
    problems: list[str] = []
    for entry in entries:
        anchors = _anchor_candidates(entry.criterion)
        live = [token for token, exists in anchors if exists]
        if not anchors and not _THRESHOLD.search(entry.criterion):
            problems.append(
                f"{entry.where} 的判据既没有锚（`路径::用例名` / `python scripts/x.py`）"
                "也没有可量阈值——没有判据的条目不许进表")
        elif entry.status in NEEDS_LIVE_ANCHOR and anchors and not live:
            problems.append(
                f"{entry.where} 是{entry.status}，但判据锚一个都不在："
                f"{[token for token, _ in anchors]}（用例被删或脚本改名？"
                "这是「失去判据」，不是「没做」）")
        if entry.status in NEEDS_TRIGGER and not entry.trigger:
            problems.append(f"{entry.where} 是{entry.status}，必须给触发条件"
                            "（「不做」靠一个可量阈值成立，「以后看情况」不算）")
        problems.extend(_blocker_problems(facts, entry))
        if entry.status in NEEDS_PROBE:
            if entry.probe == "none":
                problems.append(f"{entry.where} 是{entry.status}，必须给 probe——"
                                "「已做」不许没有机器判据")
            elif entry.probe not in BACKLOG_PROBE_DOC:
                problems.append(f"{entry.where} 的 probe `{entry.probe}` 没有注册")
            elif entry.probe in facts.backlog_probes \
                    and not facts.backlog_probes[entry.probe]:
                problems.append(
                    f"{entry.where} 写{entry.status}，但探针 `{entry.probe}` 读出来是假"
                    f"（{BACKLOG_PROBE_DOC[entry.probe]}）——状态改错，或代码被翻回去了")
        elif entry.probe != "none":
            value = facts.backlog_probes.get(entry.probe)
            if value:
                # 这一条最要紧：**状态改错**与"代码被翻回去"在这里是同一个读数，
                # 所以话术只说"两处不一致"，处理方式由人看证据定。
                problems.append(
                    f"{entry.where} 写{entry.status}，但探针 `{entry.probe}` 读出来是**真**"
                    f"（{BACKLOG_PROBE_DOC[entry.probe]}）——这件事已经在代码里，"
                    "状态写错了（或代码被翻回去了，那要先改代码再改状态）")
            else:
                problems.append(
                    f"{entry.where} 是{entry.status}，probe 必须是 `none`："
                    "probe 为真 ⇔ 这件事已经在代码里，{待做/不做} 没有可读的事实")
    return problems


def collect() -> Facts:
    facts = Facts()
    probe_allowed_literal(facts)
    probe_runtime(facts)
    probe_stubs(facts)
    probe_env_names(facts)
    probe_gate(facts)
    probe_deploy(facts)
    probe_directions(facts)
    probe_backlog(facts)
    return facts


# ---------------------------------------------------------------- 规则


@dataclass(frozen=True)
class Rule:
    """一行「事实 → 反句」。

    `holds` 读事实，`expect` 给一句"期望事实"（报错时贴在命中句后面）。
    `when_true` / `when_false` 是**双向**的两组禁句：事实为真用前者，为假用后者。
    `pattern` 非空时改走**取值比对**（B 族开关默认值：写法太多，固定正则压不住）。
    """

    name: str
    #: 这条事实凭什么成立：`路径` 或 `路径::用例名`。`contract` 必须给用例名。
    evidence: str
    #: `contract`（有运行时用例背书）或 `observation`（只有代码现状）。
    kind: str
    holds: Callable[[Facts], bool]
    expect: Callable[[Facts], str]
    when_true: tuple[str, ...] = ()
    when_false: tuple[str, ...] = ()
    pattern: re.Pattern[str] | None = None
    #: `True` 表示这一族允许"历史语境"放行（只给开关默认值一族用）。
    history_excused: bool = False
    #: 非空：这条规则只在**这一份文档**里判。台账一族用（见 `_backlog_rules`）。
    scope: str = ""
    #: 非空：只在**这几份文档**里判。D 族用（见 `_drift_rules`）——"会漂的数"只管
    #: 断言现状的那几份，历史留档（`docs/PLAN.md` 的版本表）不进去。
    scopes: tuple[str, ...] = ()
    #: 非 `(0, 0)` 时只在**这些行**里判（1-based，含两端）。台账的每条一个块。
    block: tuple[int, int] = (0, 0)


def _registry(facts: Facts) -> dict[str, tuple[str, ...]]:
    if facts.registry is None:
        raise LookupError("注册表没读到（② 失败）")
    return facts.registry


def _registered(intent: str, tool: str) -> Callable[[Facts], bool]:
    def holds(facts: Facts) -> bool:
        return tool in _registry(facts).get(intent, ())
    return holds


def _not_registered(intent: str, tool: str) -> Callable[[Facts], bool]:
    def holds(facts: Facts) -> bool:
        return tool not in _registry(facts).get(intent, ())
    return holds


def _registry_expect(facts: Facts) -> str:
    return ", ".join(f"{intent} → {list(names)}"
                     for intent, names in sorted(_registry(facts).items()))


def _stub(name: str) -> Callable[[Facts], bool]:
    def holds(facts: Facts) -> bool:
        if facts.stubs is None:
            raise LookupError("桩标记没读到（③ 失败）")
        return bool(facts.stubs.get(name))
    return holds


RULES: list[Rule] = [
    Rule(
        name="create_registers_generate",
        evidence="backend/tests/test_one_path.py::"
                 "test_create_intent_offers_only_the_single_call_generator",
        kind="contract",
        holds=_registered("create", "mindmap_generate"),
        expect=_registry_expect,
        # 事实为真（create 真的注册了整树生成）→ 禁"只剩分片/整树生成没了"这一族。
        when_true=(
            r"(?:只剩|只有|仅剩|仅有|只留)[^\n]{0,4}分片",
            r"分片[^\n]{0,12}(?:是|成了|作为)[^\n]{0,8}(?:唯一|仅有)",
            r"mindmap_generate(?!_deep)[^\n]{0,24}"
            r"(?:已删除|已删掉|不再存在|已经没了|代码不在|没有了)",
            r"create[^\n]{0,16}(?:只有|仅)[^\n]{0,16}mindmap_generate_deep",
        ),
        # 事实为假（整树生成不在 create 里）→ 反过来禁"create 只有整树生成"。
        when_false=(
            r"create[^\n]{0,16}(?:只有|仅|唯一注册)[^\n]{0,16}mindmap_generate(?!_deep)",
            r"mindmap_generate(?!_deep)[^\n]{0,16}(?:是|为)[^\n]{0,10}create[^\n]{0,8}唯一",
        ),
    ),
    Rule(
        name="deep_not_registered_under_create",
        evidence="backend/tests/test_one_path.py::"
                 "test_the_sliced_generator_survives_but_is_not_registered_under_create",
        kind="contract",
        holds=_not_registered("create", "mindmap_generate_deep"),
        expect=_registry_expect,
        when_true=(
            r"mindmap_generate_deep[^\n]{0,24}(?:已删除|已删掉|不存在|代码不在|代码没了)",
            r"分片[^\n]{0,12}(?:已删除|代码没了|已经删掉|整段删了)",
        ),
        when_false=(
            r"mindmap_generate_deep[^\n]{0,24}(?:未注册|不注册|没有注册|还没挂)",
            r"分片[^\n]{0,12}(?:没有注册|未注册)",
        ),
    ),
    Rule(
        name="source_mode_switch_retired",
        evidence="backend/tests/test_one_path.py::test_source_mode_switch_is_gone",
        kind="contract",
        holds=lambda facts: "MINDMAP_SOURCE_MODE" not in facts.env_names_in_code,
        expect=lambda facts: "app/** 里无人在读 `MINDMAP_SOURCE_MODE`"
                             if "MINDMAP_SOURCE_MODE" not in facts.env_names_in_code
                             else "app/** 里**仍有人**读 `MINDMAP_SOURCE_MODE`",
        when_true=(
            r"MINDMAP_SOURCE_MODE[^\n]{0,24}(?:仍然|还在|依然|可以|用来|控制)",
            r"(?:形态|source_mode)[^\n]{0,12}(?:切换|开关)[^\n]{0,12}(?:仍然|还在|可用)",
        ),
        when_false=(
            r"MINDMAP_SOURCE_MODE[^\n]{0,24}(?:已删除|已删掉|不再存在|已经没了)",
        ),
    ),
    Rule(
        name="knowledge_search_implemented",
        evidence="backend/tests/test_knowledge_search_route.py::"
                 "test_unrelated_query_returns_empty_but_still_implemented",
        kind="contract",
        holds=_stub("SearchResponse"),
        expect=lambda facts: f"SearchResponse.implemented = "
                             f"{(facts.stubs or {}).get('SearchResponse')}",
        when_true=(
            # `(?<!不)是桩`：文档里大量"早就**不是桩**了"是在**纠正**旧说法，
            # 它和"仍是桩"是同一句话的正反面，不能一起禁。
            r"knowledge/search[^\n]{0,28}(?:仍是桩|还是桩|(?<!不)是桩|返回空|"
            r"implemented\s*[:=]\s*false|没接入检索|不接检索)",
            r"检索[^\n]{0,20}(?:仍是桩|还是桩)[^\n]{0,10}knowledge",
        ),
        when_false=(
            r"knowledge/search[^\n]{0,28}(?:已实现|已接入|implemented\s*[:=]\s*true)",
        ),
    ),
    Rule(
        name="agent_tasks_endpoint_is_stub",
        evidence="backend/openapi.json",
        kind="observation",
        holds=lambda facts: "POST /api/agent/tasks" in (facts.stub_endpoints or frozenset()),
        expect=lambda facts: f"桩端点 {sorted(facts.stub_endpoints or ())}",
        when_true=(
            r"agent/tasks[^\n]{0,28}(?:已实现|已经接了|不再是桩)",
        ),
        when_false=(
            r"agent/tasks[^\n]{0,28}(?:仍是桩|还是桩|是桩|implemented\s*[:=]\s*false)",
        ),
    ),
    Rule(
        name="auth_gate_active",
        evidence="backend/tests/test_auth.py::test_the_gate_is_now_closed_for_anonymous_callers",
        kind="contract",
        holds=lambda facts: bool(facts.gate_wired and facts.gate_refuses),
        expect=lambda facts: f"main.py 挂 GateMiddleware={facts.gate_wired}，"
                             f"gate.py 有 401/403 分支={facts.gate_refuses}",
        when_true=(
            # "没有登录/账号/认证…"必须是**缺功能**的语气。两处不能碰：
            #   · "未登录 → `auth_required` 401"（`docs/API.md`）是在描述门禁**生效**；
            #   · "没有权限隔离"（`docs/USER-GUIDE.md`）说的是多租户 RBAC，仍然成立。
            # 这两个混进来，闸门就会把对的话判成错的——那比漏报更坏。
            r"(?:没有|无)(?:任何)?[\u4e00-\u9fa5]{0,4}(?:登录|账号|用户|认证|鉴权)"
            r"[^\n]{0,14}(?:功能|机制|体系|校验|检查|拦截)[^\n]{0,6}",
            r"(?:没有|无)[\u4e00-\u9fa5]{0,3}(?:认证|鉴权|登录)(?=[、，,。；;]|$)",
            r"(?:所有|全部|每个)(?:接口|端点|路由)[^\n]{0,10}"
            r"(?:裸奔|都没有鉴权|没有鉴权|无鉴权|没门禁)",
            r"(?:接口|端点)[^\n]{0,10}(?:全都|一律|统统)[^\n]{0,6}裸奔",
        ),
        when_false=(
            r"(?:门禁|鉴权|认证)[^\n]{0,12}(?:已|全)(?:开|生效|启用)",
        ),
    ),
    Rule(
        name="deployed",
        evidence="deploy/genai-mindmap.service",
        kind="observation",
        holds=lambda facts: bool(facts.deploy_ready),
        expect=lambda facts: f"deploy/ 里 unit + nginx.conf + install.sh 齐全={facts.deploy_ready}",
        when_true=(
            r"(?:开发骨架|最小演示闭环|最小可演示|最小演示|还在开发阶段|尚未上线)",
        ),
        when_false=(
            r"(?:已经上线|已上线|已部署上线)",
        ),
    ),
    Rule(
        name="four_directions",
        evidence="frontend/src/canvas/layout/tidy.ts",
        kind="observation",
        holds=lambda facts: facts.directions == 4,
        expect=lambda facts: f"Direction 有 {facts.directions} 个取值",
        when_true=(
            r"(?:画布|布局)[^\n]{0,12}只有[^\n]{0,8}(?:一套|一种)[^\n]{0,12}(?:紧凑树|树布局)",
            r"(?:只有|仅有|只支持)[^\n]{0,8}(?:一套|一种)[^\n]{0,8}(?:紧凑树|树布局)",
        ),
        when_false=(
            r"(?:四种|四个方向)[^\n]{0,10}(?:朝向|布局)",
        ),
    ),
]


def _switch_rules(facts: Facts) -> list[Rule]:
    """B 族：每个开关一条，**自动生成**。

    事实 = 清空环境后 `*_enabled()` 的真实返回值（不是从注释里抄的"默认关"）。
    禁句 = "这个开关名附近给出的默认值与代码相反"。双向：默认开的禁"默认关"，
    默认关的禁"默认开"。这一族的意义是**默认值不必逐条人写句子**：变量名就是锚点，
    代码翻一次，禁句跟着翻——这也把"默认值"那一族一次全钉住，不是抽验两项。
    """
    rules: list[Rule] = []
    words = (r"(?:关闭|禁用|开启|打开|启用|关(?!闭)|开(?!始)|false|true"
             r"|[`\"']?(?:(?<![0-9])(?:0|1)(?![0-9.])|(?:false|true))[`\"']?)")
    for env_name in sorted(facts.env_switches or {}):
        default_on = bool(facts.env_switches[env_name])
        name = f"env_default:{env_name}"
        head = r"`?" + re.escape(env_name) + r"`?[^\n]{0,200}?(?:默认|缺省)[^\n]{0,12}?"
        wrong = head + words + r"(?![A-Za-z])"
        rules.append(Rule(
            name=name,
            evidence=facts.switch_sources.get(env_name, "backend/app"),
            kind="observation",
            holds=lambda _facts, value=default_on: value,
            expect=lambda _facts, value=default_on: f"代码默认{'开' if value else '关'}",
            when_true=(wrong,) if default_on else (),
            when_false=(wrong,) if not default_on else (),
            pattern=re.compile(head + "(" + words + ")", re.IGNORECASE),
            history_excused=True,
        ))
    return rules


def _backlog_rules(facts: Facts) -> list[Rule]:
    """C 族：台账的每条一个块，**自动生成**。

    事实 = 该条 `probe` 的读数（读代码）；禁句 = 本块里与事实相反的状态说法。三个
    `scope` / `block` 字段是这一族存在的理由：`BACKLOG.md` 的十二条里"N 处写着待做"
    这种句子在别的条目里可能正是对的，全文扫必然误报。

    「已做」那条最要紧：把状态改成"待做"而代码没动，这里就红——这就是
    `tests/test_doc_claims.py::test_a_wrong_status_is_red` 钉的那个方向。
    """
    rules: list[Rule] = []
    for entry in facts.backlog or ():
        if entry.probe == "none" or entry.probe not in BACKLOG_PROBE_DOC:
            # 状态不由代码决定的条目没有"代码事实"可读，硬造一条规则就是自欺。
            continue
        rules.append(Rule(
            name=f"backlog:{entry.number}",
            evidence=BACKLOG_RELATIVE,
            kind="observation",
            holds=lambda _facts, key=entry.probe: bool(_facts.backlog_probes.get(key)),
            expect=lambda _facts, key=entry.probe: f"探针 {key} = "
                                                   f"{_facts.backlog_probes.get(key)}",
            when_true=BACKLOG_TAKE_BACK,
            when_false=BACKLOG_SETTLED,
            scope=BACKLOG_RELATIVE,
            block=(entry.start, entry.end),
        ))
    return rules


# ---------------------------------------------------------------- 会漂的数（D 族）
#
# 前三族判的是"文档说的与代码做的是不是同一件事"，这一族判的是**"这句话会不会自己过期"**。
# 起因是一个数被抄到三处：`FEATURES_AND_OWNERS.md` 写着"后端 2113 条用例 / 85 个文件，
# 前端 900 条 / 55 个 spec"，`PLAN-REWORDING.md` 与 `SCOPE.md` 各抄了一份。数早就变成
# 3471 / 155 与 1462 / 93 了，而三处都还印着旧值——**没有任何一条断言能发现它**，因为
# 每句话单独看都曾经是真的，错的是"它还写着现在"。
#
# 所以这一族不给"用例数应该是多少"立事实（那要每次 collect 一遍，太贵，而且 collect 出来
# 的数下一秒就变了）；它立的是**形状**：这类数一律不许以字面量出现，要写就写"怎么现取"。
#
#   会随别人正常干活自然变化（用例数、测试文件数、端点数、开关数、md 份数）→ 引用式
#   不会（契约上限 50 步撤销、`FULL_TEXT_MATERIAL_CHARS = 50_000`、压测 P50）→ 允许写，
#   那些自有前三族与用例管着
#
# 判据是"这个数会不会因为**别人正常提交**而变"。会变的写进文档就是给自己排了一个永远
# 干不完的活（每轮都要手抄一遍，而漏抄没有报错）。

#: 一族一条规则：`(名字后缀, 禁句正则, 该改成什么)`。加一类 = 加一行。
DRIFT_CLASSES: tuple[tuple[str, str, str], ...] = (
    ("use_cases", r"\d{2,5}\s*(?:条|个)\s*(?:用例|测试)",
     "现跑 `pytest --collect-only` / `npm test`，文档里写命令不写结果"),
    ("test_files", r"\d{2,4}\s*个\s*(?:测试)?文件|\d{2,4}\s*个\s*spec",
     "同上：文件数一并交给命令"),
    ("endpoints", r"\d{1,3}\s*个?\s*(?:路径|端点)",
     "指向 `backend/openapi.json`（CI ⑥ 用 `git diff --exit-code` 钉着它）"),
    ("route_groups", r"\d{1,2}\s*组路由",
     "指向 `backend/app/main.py` 的 `include_router`"),
    ("switch_count", r"\d{1,3}\s*个开关",
     "指向本闸门的开关一族（它每次自己数）"),
    ("doc_count", r"\d{2,3}\+?\s*份\s*md|docs/\s*下\s*\d{2,3}\+?\s*份"
                  r"|\d{2,3}\+?\s*份\s*文件|\d{2,3}\+?\s*份(?=[、，—])",
     "现算：`git ls-files '*.md' | wc -l`"),
    ("now_value", r"[（(]\s*现在\s*\d{2,4}|现在是\s*\d{2,4}",
     "写测量的**日期**（\"2026-11-27 那次是 17\"），不写\"现在\""),
)

#: 这一族只管**断言现状**的文件。历史留档不进来——它们写的是"那一天是多少"，是正确的
#: 过去时（`docs/PLAN.md` 的版本表、`docs/AGENT_PLAN.md` 的计划期数字、`docs/TODO.md`
#: 的快照）。加一份 = 加一行。
#:
#: 2026-11-28 从 5 份扩到 8 份：`TEAM-GUIDE.md` / `docs/PROJECT-STATUS.md` /
#: `docs/USER-PATH-TESTS.md` 也在断言现状（"50 个动作 / 39 条用例""后端 2123 passed"
#: "44 个导出函数"），而它们不在 scope 里，等于给自己留了一块没人扫的地方——台账第 12 条
#: 的触发条件原本写"再漂一次或涨到 5 份"，但真读下来它们**现在就在漂**，不必等下一次。
#:
#: 三份里确实有历史留档段落（`USER-PATH-TESTS.md` 第一节"3311 条用例没抓到四种缺陷"是
#: 那一轮的读数、`PROJECT-STATUS.md` 的"这一周的实测数字"是 11-18 那天的表）。这些段落
#: **不搬进 scope 之外**，而是就地加 `claims-ok: drift:* 历史留档：…` —— 与 B 族的历史
#: 语境放行同一条道理：那不是错话，是错误的过去时，逐份文档放行比逐条注释更容易被看见。
DRIFT_SCOPE: tuple[str, ...] = (
    "README.md",
    "TEAM-GUIDE.md",
    "docs/BACKLOG.md",
    "docs/FEATURES_AND_OWNERS.md",
    "docs/PLAN-REWORDING.md",
    "docs/PROJECT-STATUS.md",
    "docs/SCOPE.md",
    "docs/USER-PATH-TESTS.md",
)


def _drift_rules() -> list[Rule]:
    """D 族：文档里会漂的数。`holds` 恒为真——这一族**没有事实可读**。

    这与前三族的分工是刻意的：事实探针答"代码现在是什么"，而"用例数是多少"不是代码
    事实，是**测量结果的快照**。给它立探针就要在每次 `--gate` 里 collect 一遍（十几秒），
    而读数下一秒就过期——那是用贵得多的办法换来一个更差的判据。所以判"不许写"：
    `holds=True` 只表示"这条禁句永远成立"。
    """
    rules: list[Rule] = []
    for suffix, pattern, advice in DRIFT_CLASSES:
        rules.append(Rule(
            name=f"drift:{suffix}",
            evidence="backend/tests/test_doc_claims.py::"
                     "test_a_bare_count_in_a_state_document_is_red",
            kind="contract",
            holds=lambda _facts: True,
            expect=lambda _facts, advice=advice: f"不写裸数字：{advice}",
            when_true=(pattern,),
            scopes=DRIFT_SCOPE,
        ))
    return rules


# ---------------------------------------------------------------- 扫描

@dataclass
class Violation:
    path: str
    line: int
    sentence: str
    rule: Rule
    expect: str
    excused: bool
    #: 放行的原因（`claims-ok` / `历史语境` / `否定语境`）。空表示硬命中。
    because: str = ""


def doc_files() -> tuple[list[Path], list[str]]:
    problems: list[str] = []
    try:
        listed = subprocess.run(["git", "ls-files", "*.md"], cwd=str(REPO_DIR),  # noqa: S607
                                capture_output=True, text=True, encoding="utf-8", timeout=120)
        if listed.returncode != 0:
            raise OSError((listed.stderr or "").strip() or f"退出码 {listed.returncode}")
        names = [row for row in listed.stdout.splitlines() if row.strip()]
    except (OSError, subprocess.SubprocessError) as error:
        problems.append(f"git ls-files 不可用（{error}），退回全树扫描 *.md")
        names = [path.relative_to(REPO_DIR).as_posix()
                 for path in sorted(REPO_DIR.rglob("*.md"))
                 if ".git" not in path.parts and "node_modules" not in path.parts]
    # 台账是本闸门自己的输入：它可能刚建、还没被 git 跟踪，那时 `git ls-files` 看不见
    # 它，`_backlog_rules` 就成了空转（本地会绿、CI 会红——最难查的那种不一致）。
    if BACKLOG_RELATIVE not in names and (REPO_DIR / BACKLOG_RELATIVE).exists():
        names.append(BACKLOG_RELATIVE)
    kept = [REPO_DIR / name for name in names
            if not name.startswith(NON_LOCAL_DOC_PREFIXES) and Path(name).name != SELF_DOC]
    return kept, problems


def _exemptions(line: str, previous: str) -> dict[str, str]:
    """这一行与上一行里的豁免注释 → `{事实名: 理由}`。名字与理由都必填。"""
    out: dict[str, str] = {}
    for text in (previous, line):
        for match in _CLAIMS_OK.finditer(text):
            body = match.group(1).strip()
            if not body:
                continue
            parts = body.split(None, 1)
            name = parts[0].strip().rstrip(",")
            reason = parts[1].strip() if len(parts) > 1 else ""
            out[name] = reason
    return out


def _regex_hit(patterns: tuple[str, ...], line: str) -> tuple[int, str]:
    best = (-1, "")
    for pattern in patterns:
        match = re.search(pattern, line)
        if match and (best[0] < 0 or match.start() < best[0]):
            best = (match.start(), match.group(0))
    return best


def _value_mismatch(rule: Rule, line: str, holds: bool) -> tuple[int, str] | None:
    """B 族：文档给出的默认值与代码相反才算命中。

    "默认开/关"的写法有七八种，固定正则压不住，所以用**取值比对**：捕获文档说的那个词，
    归一成 bool，与事实比。
    """
    assert rule.pattern is not None
    for match in rule.pattern.finditer(line):
        word = match.group(1).strip().strip("`\"'").lower()
        if word in ("开启", "打开", "启用"):
            word = "开"
        if word in ("关闭",):
            word = "关"
        if word in _TRUE_WORDS:
            stated = True
        elif word in _FALSE_WORDS:
            stated = False
        else:
            continue
        if stated != holds:
            return match.start(), match.group(0)
    return None


def _sentence(line: str, start: int, length: int) -> str:
    """命中句的上下文（前后各 24 字）。文档里有整段一行的表格，不整行贴。"""
    left = max(0, start - 24)
    right = min(len(line), start + length + 24)
    prefix = "…" if left else ""
    suffix = "…" if right < len(line) else ""
    return f"{prefix}{line[left:right].strip()}{suffix}"


def scan(facts: Facts, rules: list[Rule], files: list[Path]) -> list[Violation]:
    by_name = {rule.name: rule for rule in rules}
    decided: dict[str, bool] = {}
    for rule in rules:
        try:
            decided[rule.name] = bool(rule.holds(facts))
        except LookupError as error:
            facts.problems.append(f"事实 `{rule.name}` 不可判：{error}")
    violations: list[Violation] = []
    for path in files:
        relative = _relative(path)
        try:
            lines = _read(path).splitlines()
        except OSError as error:
            facts.problems.append(f"读不到 {relative}: {error}")
            continue
        for index, line in enumerate(lines):
            certificates = _exemptions(line, lines[index - 1] if index else "")
            for name, reason in certificates.items():
                rule = by_name.get(name)
                if rule is None:
                    facts.problems.append(f"{relative}:{index + 1}: 豁免了不存在的事实名 `{name}`")
                elif not reason:
                    facts.problems.append(f"{relative}:{index + 1}: 豁免 `{name}` 没写理由")
            for rule in rules:
                if rule.name not in decided:
                    continue
                if rule.scope and relative != rule.scope:
                    continue
                if rule.scopes and relative not in rule.scopes:
                    continue
                if rule.block[0] and not rule.block[0] <= index + 1 <= rule.block[1]:
                    continue
                holds = decided[rule.name]
                forbidden = rule.when_true if holds else rule.when_false
                if not forbidden:
                    continue
                if rule.pattern is not None:
                    hit = _value_mismatch(rule, line, holds)
                    if hit is None:
                        continue
                    start, word = hit
                else:
                    start, word = _regex_hit(forbidden, line)
                    if start < 0:
                        continue
                excused, because = False, ""
                if certificates.get(rule.name):
                    excused, because = True, "claims-ok"
                elif rule.history_excused \
                        and HISTORY_WORDS.search(line[max(0, start - 40):start]):
                    excused, because = True, "历史语境"
                elif NEGATION_WORDS.search(line[max(0, start - 12):start]):
                    # 否定语境："不是开发骨架" 与 "是开发骨架" 逐字相同而意思相反。
                    excused, because = True, "否定语境"
                violations.append(Violation(
                    path=relative, line=index + 1, sentence=_sentence(line, start, len(word)),
                    rule=rule, expect=rule.expect(facts), excused=excused, because=because))
    return violations


# ---------------------------------------------------------------- 证据

_TEST_DEF = re.compile(r"^\s*(?:async )?def (test_[A-Za-z0-9_]+)\s*\(", re.MULTILINE)


def check_evidence(rules: list[Rule]) -> list[str]:
    """每条事实的 `evidence` 必须真的存在。

    `contract` 必须指向一个**用例名**，并在那个文件里找得到 `def <名字>`。这是
    "代码错了、文档对了"那一侧的防线之一：矛盾时先看谁背书，而不是直接改文档。
    `unwired_baseline.json` 的教训同款——背书的理由被删掉而结论还留着，那才是烂账。
    """
    problems: list[str] = []
    for rule in rules:
        target, _, test_name = rule.evidence.partition("::")
        path = REPO_DIR / target
        if rule.kind not in ("contract", "observation"):
            problems.append(f"事实 `{rule.name}` 的 kind=`{rule.kind}` 不合法")
        if not path.exists():
            problems.append(f"事实 `{rule.name}` 的 evidence 文件不存在：{target}")
            continue
        if rule.kind == "contract" and not test_name:
            problems.append(f"事实 `{rule.name}` 是 contract，但 evidence 没指明用例名")
            continue
        if test_name and test_name not in set(_TEST_DEF.findall(_read(path))):
            problems.append(f"事实 `{rule.name}` 的背书用例 `{test_name}` 在 {target} 里找不到")
    return problems


# ---------------------------------------------------------------- 输出


def describe_facts(facts: Facts, rules: list[Rule]) -> None:
    print(f"事实（{len(rules)} 条规则，{len(facts.problems)} 处探针问题）")
    for rule in rules:
        try:
            holds = bool(rule.holds(facts))
            state = "真" if holds else "假"
        except LookupError as error:
            holds, state = None, f"不可判（{error}）"
        marker = "contract   " if rule.kind == "contract" else "observation"
        print(f"  [{state}] {marker} {rule.name}")
        try:
            print(f"      期望事实：{rule.expect(facts)}")
        except LookupError:
            pass
        print(f"      凭什么：  {rule.evidence}")
        if holds is not None:
            for pattern in (rule.when_true if holds else rule.when_false):
                print(f"      禁：{pattern}")
    if facts.problems:
        print("\n探针问题：")
        for problem in facts.problems:
            print(f"  · {problem}")


def describe_backlog(facts: Facts) -> None:
    """台账读数。`probe: none` 的条目单列——它们的状态不由代码决定，别混着读。"""
    entries = facts.backlog or ()
    if not entries:
        return
    print(f"\n台账（{BACKLOG_RELATIVE}，{len(entries)} 条）：")
    for entry in entries:
        extra = ""
        if entry.probe != "none":
            extra = f"  probe {entry.probe}={facts.backlog_probes.get(entry.probe)}"
        print(f"  · #{entry.number:>2} [{entry.status}] {entry.title}{extra}")
    manual = [entry.number for entry in entries if entry.probe == "none"]
    if manual:
        print(f"    只有人判（状态不由代码决定）：{manual}")
    pending = pending_anchors(facts)
    if pending:
        print(f"    待做的判据现在还不存在（先写判据、后派人做）：{pending}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="文档断言与代码一致（设计见本文件 top docstring）")
    parser.add_argument("--gate", action="store_true",
                        help="闸门模式：命中/证据缺失/探针失败 → 退出码 1")
    parser.add_argument("--facts", action="store_true", help="只打印事实现值")
    args = parser.parse_args(argv)

    facts = collect()
    rules = RULES + _switch_rules(facts) + _backlog_rules(facts) + _drift_rules()
    if args.facts:
        describe_facts(facts, rules)
        describe_backlog(facts)
        return 0 if not facts.problems else 1

    files, problems = doc_files()
    facts.problems.extend(problems)
    violations = scan(facts, rules, files)
    hard = [item for item in violations if not item.excused]
    excused = [item for item in violations if item.excused]
    evidence_problems = check_evidence(rules)
    backlog_problems = check_backlog(facts)


    print(f"文档断言与代码一致：{len(rules)} 条事实 / {len(files)} 份文档")
    for rule in rules:
        try:
            state = "真" if rule.holds(facts) else "假"
            print(f"  [{state}] {rule.name:38s} {rule.expect(facts)}")
        except LookupError as error:
            print(f"  [??] {rule.name:38s} 不可判：{error}")
    describe_backlog(facts)
    print()

    if excused:
        print(f"按豁免/语境放行 {len(excused)} 处（可见，不静默）：")
        for item in excused:
            print(f"  · [{item.because}] {item.path}:{item.line} {item.rule.name} "
                  f"「{item.sentence}」")
        print()

    stopping = bool(hard or facts.problems or evidence_problems or backlog_problems)
    if not args.gate:
        for item in hard:
            print(f"{item.path}:{item.line}: {item.sentence} → 事实 {item.rule.name}：{item.expect}")
        for problem in facts.problems:
            print(f"探针问题：{problem}")
        for problem in evidence_problems:
            print(f"证据问题：{problem}")
        for problem in backlog_problems:
            print(f"台账问题：{problem}")
        if not stopping:
            print("没有命中。")
        return 0

    if not stopping:
        print(f"闸门：通过（{len(rules)} 条事实与文档一致）")
        return 0

    print("闸门：不通过")
    for item in hard:
        kind = "冲突" if item.rule.kind == "contract" else "文档过期"
        print(f"  · [{kind}] {item.path}:{item.line}: {item.sentence}")
        print(f"      事实 {item.rule.name} = {item.expect}")
        print(f"      凭什么：{item.rule.evidence}")
        if item.rule.kind == "contract":
            print("      → 有运行时用例背书：代码才是 bug 就改代码；契约本身变了就先改用例"
                  "与 evidence，再改文档。")
    for problem in facts.problems:
        print(f"  · [探针] {problem}")
    for problem in evidence_problems:
        print(f"  · [证据] {problem}")
    for problem in backlog_problems:
        print(f"  · [台账] {problem}")
    print("\n三种处理：改文档让说法与代码一致 / 改代码把事实改回来 / "
          "引用旧说法处加 `<!-- claims-ok: <事实名> 理由 -->`。")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
