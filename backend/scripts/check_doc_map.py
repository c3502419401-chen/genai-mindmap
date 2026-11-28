#!/usr/bin/env python
"""`docs/` 的分级：哪份是入口、哪份按需查、哪份是存档、哪份只给 AI 看。

## 为什么需要它

一天之内同一个问题撞了三次：找"记忆是给谁的"、找"鱼骨为什么不该体检"的原始判断、
想知道"接口缺口那 12 条"该从哪份读起。三次都是**在几十份文档里翻**，而本该当前门的
两份（根目录的 `NEXT-PLAN.md` 与 `docs/TODO.md`）**都过期了**。比"缺索引"更坏的是这个：
读者会先看到过期的那份，然后读到过期的话。

台账第 10 条把这件事写成三件事：每份 md 头顶一行 `tier:`、`entry` 从 `README.md` 出发
2 跳内可达、`ai-only` 不进人的入口。**本脚本只做后两件，不做第一件**——理由见下一节。

## 为什么不给 127 份文件加文件头

台账原来的判据是"每份 md 头顶一行 `tier:`"。上一轮因此判"成本太高，属另一件事"。成本
确实高，但高的不是改文件的力气（那可以脚本化），是**改错了地方**：

  · **"哪一档"不是文件自身的属性，是它相对入口的位置。** 文件头该写"这份文档关于什么"
    （标题、日期、状态），不该写"它离入口多远"——后者是索引的属性，随索引变而变。
  · **变动面被放大一遍。** 今天 `reference`、明天进 `archive`，文件头方案要改两个地方
    （文件头 + 别忘了还有谁在引用这个判断）；本方案改一处，就是这张表的一行。
  · **diff 面。** 127 个文件头的一次性改动会把四个提交全淹掉，review 的人只会看标题。
    一张表的 127 行可以一次读完，而且**后来每次改档只有一行**。
  · 新增一份文档时，文件头方案要求"新人得先知道有这个规矩"，本方案只要跑一次
    `--write`（或让 `--gate` 直接告诉他还差哪一份）。

而"忘记登记"这个风险在两种方案里**完全一样**（都要一道闸来抓），所以它不构成取舍理由。

## 索引为什么是生成物，不是手写目录

这个仓库已经有一份手写目录：`README.md` 的「文档地图」。它现在还准，但台账说得很直白：
**手写目录是这个仓库里最先腐烂的东西**（`TODO.md` 与 `NEXT-PLAN.md` 是活证据）。

所以 `docs/INDEX.md` 分成职责不同的两半：

    第一、二节（人写）  从哪儿开始 / 按问题查——这是**推荐路径**，不是清单，它不可能"漏项"
    第三节（生成物）    全量登记：一份不漏、一份不多，由本脚本 `--write` 维护

第三节的全部内容都能现算（`git ls-files` 给清单、每份的 H1 给题目），所以它像
`backend/openapi.json` 一样是**生成后由 CI 钉住**的产物，而不是需要人维护的表。
人手写的只有"档"与"archive 的替代物"这两列，而且**新文件出现时才需要动**。

## 为什么日期不写进文件

台账要求"归档的那几份要么标日期、要么删"。日期是**会漂的数**：`git log` 一有新提交它就
变，写进文件就等于给自己排一个每轮都要手抄一遍、且漏抄没有报错的活。这正是
`docs/BACKLOG.md` 第 12 条（`check_doc_claims.py` 的 D 族）立的规矩。

所以日期不写，`--gate` **现算并打印**（`archive` 档各自最后一次被改是哪天），可见、不
静默。写进文件的是**替代物**——"归档"两个字不告诉读者下一步去哪儿，替代物才告诉。

## 四档，不要第五档 `dead`

     entry      人从这里开始读。应当只有少数几份，且从 README 出发 2 跳内可达
     reference  按需查阅。绝大多数在这里
     archive    历史留档：内容不再有效，必须写清替代物
     ai-only    给 AI/agent 读的规范与判据，不进人的入口

**不设 `dead`。** `dead` 不是稳定档位，是**待办动作**（该删或该改），而一个标签挂上去
就永远挂着——`TODO.md` 从 2026-11-10 挂到 2026-11-28 正是这个形状。"该删"靠删掉解决，
"该改"靠改掉解决；`archive` 是它们处理完之后的状态，不是处理之前的收件箱。

## 判据（`--gate`，八条）

  ① INDEX 第三节列出的每一份都真的存在（无死链）
  ② `git ls-files '*.md'` 里每一份都在第三节里（无遗漏）
  ③ 档位值合法（四档之一）
  ④ `entry` 档从 `README.md` 出发 2 跳内可达（沿 markdown 链接做 BFS）
  ⑤ `archive` 档的「替代物」非空；写了路径的，那个文件必须存在
  ⑥ 第三节与 `--write` 的现算结果逐字节一致（手改了、或加了新 md 没重生成）
  ⑦ `README.md` 与 `docs/INDEX.md` 里的 markdown 链接都指向存在的文件（导航无死链）
  ⑧ `entry` 档不是空的，且不含 `ai-only` 档的文档（两份清单不许互相污染）

第 ④ 条为什么沿 **markdown 链接**而不认反引号里的文件名：这条判据问的是"**人点得动吗**"。
`README.md` 的文档地图用的是反引号（纯文本，点不动），所以它本身零跳；`docs/INDEX.md`
的第二、三节用的是真链接，于是 `README.md → INDEX.md → 任一份 entry` 恰好两跳。这也是
索引存在的意义：它是这条承诺的**中间节点**。

## 跑法（在 `backend/` 下）

    python scripts/check_doc_map.py --write   # 重生成第三节（保留人写的两列）
    python scripts/check_doc_map.py           # 只体检，打印读数
    python scripts/check_doc_map.py --gate    # 给 CI 用：任一条不过就非零退出

为什么它在 CI 上是确定性的：判据是 `git ls-files '*.md'` 的**清单**、每份文件的**首行标题**、
两份导航文档的**链接文本**；不读运行时、不读数据库、不发网络请求。唯一的 git 调用是
`ls-files`（与 `check_doc_claims.py` / `check_script_hygiene.py` 同一条用法）与
`log -1 --format=%ad`（只为打印 archive 的日期，不参与判据）。所以在干净 checkout 上
与本地读数逐行一致。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
INDEX = REPO_DIR / "docs" / "INDEX.md"
INDEX_RELATIVE = "docs/INDEX.md"
README = REPO_DIR / "README.md"

BEGIN = "<!-- doc-map:begin -->"
END = "<!-- doc-map:end -->"

TIERS = ("entry", "reference", "archive", "ai-only")
#: 新出现的文档默认按 `reference` 登记。这不是偷懒：`reference` 的语义就是"其余的"，
#: 而一份刚写出来的判定记录几乎必然是"按需查"。真要升成 `entry` 或降成 `archive`，
#: 是人读一遍之后的事，不是脚本能猜的。
DEFAULT_TIER = "reference"
#: 建档时这一列先填占位，`--gate` 会点名要求人补（`archive` 档空着就红）。
NO_SUBSTITUTE = "—"

#: 第三节的一行：`| 档 | 路径 | 题目 | 替代物 |`
_ROW = re.compile(r"^\|\s*([a-z-]+)\s*\|\s*`([^`]+)`\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$")

#: markdown 行内链接：`[文字](目标)`。只认 http(s) 之外、且以 `.md` 结尾的目标——
#: 锚点、图片、外链都不属于"导航到另一份文档"。
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


# ---------------------------------------------------------------- git


def _git(*args: str) -> str:
    done = subprocess.run(["git", "-C", str(REPO_DIR), *args],  # noqa: S607
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)
    return done.stdout if done.returncode == 0 else ""


def tracked_docs() -> list[str]:
    """`git ls-files '*.md'` 的清单，加上本索引自己。

    `docs/INDEX.md` 第一次生成时还没被跟踪，而"清单里必须有它"是判据②的一半，漏了
    会让本地绿、CI 红——`check_doc_claims.py` 对 `BACKLOG.md` 用的是同一条兜底。
    """
    names = [row for row in _git("ls-files", "*.md").splitlines() if row.strip()]
    if INDEX_RELATIVE not in names and INDEX.exists():
        names.append(INDEX_RELATIVE)
    return sorted(set(names))


def last_touched(relative: str) -> str:
    """这份文档最后一次被改是哪天（只用于打印，不参与判据）。"""
    out = _git("log", "-1", "--format=%ad", "--date=short", "--", relative).strip()
    return out or "（未跟踪）"


# ---------------------------------------------------------------- 读文件


def title_of(relative: str) -> str:
    """题目 = 第一个 markdown 标题（`#`…`######`）；没有就退回文件名主干。

    只认标题、不退回"第一行非空文本"，是因为 `docs/research/readmes/` 那批**别人仓库的**
    README 十有八九以 `<p align="center">`、徽章图片或 `src="https://…"` 开头。那样填出来
    的题目比空着更糟——它看起来像填过了。文件名主干（`jinzcdev__markmap-mcp-server`）
    反而一定读得懂，而且和路径列一致。

    YAML front matter 要跳过：`.agents/skills/*/SKILL.md` 的开头是 `---` 包起来的那块，
    里面可能有 `#` 开头的注释行，会被当成标题。
    """
    text = (REPO_DIR / relative).read_text(encoding="utf-8", errors="replace")
    text = text.lstrip("\ufeff")            # BOM：`docs/research/` 下几份快照带着它
    head = text.lstrip()
    if head.startswith("---"):
        parts = head.split("---", 2)
        if len(parts) == 3:
            text = parts[2]
    for line in text.splitlines():
        heading = re.match(r"^#+\s+(.+)$", line.strip())
        if heading:
            return _tidy(heading.group(1))
    stem = Path(relative).stem.replace("__", " / ").replace("_", " ")
    return _tidy(stem) or "（无标题）"


def _tidy(text: str) -> str:
    """把说明文字压成能放进表格里的一格。

    链接拆成纯文字是必须的，不只是好看：`docs/research/` 那几份快照的标题行里带着
    `[简体中文](./README_CN.md)` 这类指向**那个仓库**的链接，留着的话判据⑦会把它们当成本
    仓库的导航，然后报一条死链——把别人的相对路径算成自己的断链，是把对的判成错的。
    """
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)          # 粗体
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # 链接只留文字
    text = text.replace("`", "")                           # 行内代码
    text = text.replace("|", "\\|")                        # 表格分隔符
    text = re.sub(r"\s+", " ", text).strip()
    return text[:80] + ("…" if len(text) > 80 else "")


def routes() -> dict[str, str]:
    """markdown 链接 → 它指向的目标（规范化成仓库相对的 posix 路径）。

    只认文档内部导航：以 `.md` 结尾、不是 http(s)、不是纯锚点。
    """
    out: dict[str, str] = {}
    for path in (README, INDEX):
        if not path.exists():
            continue
        where = path.relative_to(REPO_DIR).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for target in _LINK.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if not target.split("#", 1)[0].endswith(".md"):
                continue
            clean = target.split("#", 1)[0]
            resolved = (path.parent / clean).resolve()
            try:
                out.setdefault(resolved.relative_to(REPO_DIR.resolve()).as_posix(), where)
            except ValueError:
                out.setdefault(clean, where)  # 跑出仓库的链接原样留下，由判据⑦报红
    return out


# ---------------------------------------------------------------- 第三节


@dataclass
class Row:
    tier: str
    path: str
    title: str
    substitute: str

    def render(self) -> str:
        return f"| {self.tier} | `{self.path}` | {self.title} | {self.substitute} |"


def parse_section(text: str) -> tuple[list[Row], list[str]]:
    """读 `<!-- doc-map:begin -->` 与 `<!-- doc-map:end -->` 之间的表格。"""
    problems: list[str] = []
    start = text.find(BEGIN)
    stop = text.find(END)
    if start < 0 or stop < 0 or stop < start:
        return [], [f"{INDEX_RELATIVE} 里找不到 `{BEGIN}` / `{END}` 围起来的登记段"]
    rows: list[Row] = []
    for line in text[start:stop].splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        # 表头（第一格是「档」）与分隔行（第一格是 `---`）。只跳过这两行，别的一律交给
        # `_ROW` 解析——解析不了要报出来，静默跳过就等于给"手写错一行"发通行证。
        if cells and (cells[0] == "档" or (cells[0] and set(cells[0]) <= set("-: "))):
            continue
        match = _ROW.match(line)
        if not match:
            problems.append(f"登记段有一行解析不了：{line.strip()[:60]}")
            continue
        rows.append(Row(tier=match.group(1), path=match.group(2),
                        title=match.group(3), substitute=match.group(4)))
    return rows, problems


def merge(rows: list[Row], docs: list[str]) -> tuple[list[Row], list[str], list[str]]:
    """把旧表与现算清单合并 → `(新表, 新增的路径, 删掉的路径)`。

    人写的两列（`tier` / `substitute`）**原样保留**；只有"有没有这一份"与"题目是什么"
    交给现算。这就是"索引是生成物"的边界：能现算的不许手写，手写的部分不许被覆盖。
    """
    known = {row.path: row for row in rows}
    fresh: list[Row] = []
    added: list[str] = []
    for path in docs:
        title = title_of(path)
        old = known.get(path)
        if old is None:
            added.append(path)
            fresh.append(Row(tier=DEFAULT_TIER, path=path, title=title,
                             substitute=NO_SUBSTITUTE))
        else:
            fresh.append(Row(tier=old.tier, path=path, title=title,
                             substitute=old.substitute))
    present = set(docs)
    removed = [row.path for row in rows if row.path not in present]
    return sorted(fresh, key=lambda row: row.path), added, removed


def render_section(rows: list[Row]) -> str:
    head = [
        BEGIN,
        "",
        "| 档 | 路径 | 题目 | archive 的替代物 |",
        "|---|---|---|---|",
    ]
    body = [row.render() for row in rows]
    tail = ["", END]
    return "\n".join(head + body + tail)


def write_index(rows: list[Row]) -> None:
    text = INDEX.read_text(encoding="utf-8")
    start = text.find(BEGIN)
    stop = text.find(END)
    if start < 0 or stop < 0 or stop < start:
        raise SystemExit(f"{INDEX_RELATIVE} 里找不到 {BEGIN} / {END}，先手工加上这两行")
    INDEX.write_text(text[:start] + render_section(rows) + text[stop + len(END):],
                     encoding="utf-8", newline="\n")


# ---------------------------------------------------------------- 判据


def reachable(start: str, links: dict[str, str], hops: int) -> dict[str, int]:
    """从 `start` 出发沿文档链接走 `hops` 跳 → `{文档: 最少跳数}`。"""
    walked = {start: 0}
    frontier = [start]
    for depth in range(1, hops + 1):
        next_frontier: list[str] = []
        for node in frontier:
            path = REPO_DIR / node
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for target in _LINK.findall(text):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                if not target.split("#", 1)[0].endswith(".md"):
                    continue
                resolved = (path.parent / target.split("#", 1)[0]).resolve()
                try:
                    relative = resolved.relative_to(REPO_DIR.resolve()).as_posix()
                except ValueError:
                    continue
                if relative in walked:
                    continue
                walked[relative] = depth
                next_frontier.append(relative)
        frontier = next_frontier
    return walked


def check(rows: list[Row], docs: list[str], problems: list[str]) -> list[str]:
    out = list(problems)
    listed = [row.path for row in rows]

    for row in rows:                                                   # ①
        if not (REPO_DIR / row.path).exists():
            out.append(f"[死链] 登记了 `{row.path}`，文件不存在（删了？改名了？）")
        if row.tier not in TIERS:                                      # ③
            out.append(f"[档位] `{row.path}` 的档 `{row.tier}` 不在 {list(TIERS)} 里")

    listed_set = set(listed)
    for path in docs:                                                  # ②
        if path not in listed_set:
            out.append(f"[遗漏] `{path}` 没有登记（跑一次 `--write`）")
    duplicated = {path for path in listed if listed.count(path) > 1}
    for path in sorted(duplicated):
        out.append(f"[重复] `{path}` 在登记段里出现了不止一次")

    walked = reachable("README.md", routes(), 2)                       # ④
    entries = [row for row in rows if row.tier == "entry"]
    if not entries:                                                    # ⑧
        out.append("[entry] 一份 `entry` 都没有——入口档空着等于没有入口")
    for row in entries:
        if row.path not in walked:
            out.append(f"[不可达] `{row.path}` 是 entry，但从 `README.md` 出发 2 跳内到不了"
                       "（README 里给它一条链接，或在 INDEX 第二节里链它）")
        if row.path.endswith("AGENTS.md") or row.path.startswith("docs/research/"):
            out.append(f"[档位] `{row.path}` 属于 ai-only / 引用留档，不该是 entry")

    for row in rows:                                                   # ⑤
        if row.tier != "archive":
            continue
        if not row.substitute.strip():
            out.append(f"[替代物] `{row.path}` 归档了，但没写替代物"
                       "（写一份现存文档，或 `—` 表示纯历史）")
            continue
        for target in _LINK.findall(row.substitute):
            resolved = (REPO_DIR / "docs" / target).resolve()
            if not resolved.exists():
                out.append(f"[死链] `{row.path}` 的替代物指向不存在的 `{target}`")

    for target, where in sorted(routes().items()):                      # ⑦
        if not (REPO_DIR / target).exists():
            out.append(f"[导航死链] `{where}` 链到不存在的 `{target}`")

    return out


def describe(rows: list[Row]) -> None:
    counts = {tier: sum(1 for row in rows if row.tier == tier) for tier in TIERS}
    print(f"登记 {len(rows)} 份："
          + "，".join(f"{tier} {count}" for tier, count in counts.items()))
    for tier in TIERS:
        if not counts[tier]:
            continue
        print(f"  [{tier}]")
        for row in rows:
            if row.tier != tier:
                continue
            extra = f"  → {row.substitute}" if tier == "archive" else ""
            if tier == "archive":
                extra += f"（最后改于 {last_touched(row.path)}）"
            print(f"    · {row.path}{extra}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="docs/ 的分级（设计见本文件 top docstring）")
    parser.add_argument("--write", action="store_true",
                        help="按现算结果重生成 INDEX 第三节，保留人写的档与替代物")
    parser.add_argument("--gate", action="store_true",
                        help="闸门模式：任一条判据不过 → 退出码 1")
    args = parser.parse_args(argv)

    if not INDEX.exists():
        print(f"缺少 {INDEX_RELATIVE}：阶梯形目录没有中间节点，这条闸门无从判起")
        return 1
    text = INDEX.read_text(encoding="utf-8")
    rows, problems = parse_section(text)
    docs = tracked_docs()
    merged, added, removed = merge(rows, docs)

    if args.write:
        write_index(merged)
        for path in added:
            print(f"+ {path}（按 {DEFAULT_TIER} 登记，要改档就改 INDEX 第三节那一行）")
        for path in removed:
            print(f"- {path}（文件没了，登记也删掉）")
        if not added and not removed and [row.render() for row in merged] \
                == [row.render() for row in rows]:
            print("第三节已是最新，没有改动。")
        else:
            print(f"第三节已重写：{len(merged)} 份。")
        return 0

    # 手改过的判定：用重生成的结果对表，报告"哪几行会变"。
    expected = [row.render() for row in merged]
    actual = [row.render() for row in rows]
    if expected != actual:
        for line in expected:
            if line not in actual:
                problems.append(f"[未重生成] 现算结果是这一行，但文件里没有：{line}")
        for line in actual:
            if line not in expected:
                problems.append(f"[手改/过期] 文件里有这一行，现算结果里没有：{line}")

    problems = check(rows, docs, problems)
    if not args.gate:
        describe(rows)
        if problems:
            print(f"\n体检：{len(problems)} 处问题（不带 --gate 时不影响退出码）")
            for item in problems:
                print(f"  · {item}")
        else:
            print("\n体检：通过。")
        return 0

    if not problems:
        print(f"闸门：通过（{len(rows)} 份文档，四档齐、入口两跳可达）")
        return 0
    print("闸门：不通过")
    for item in problems:
        print(f"  · {item}")
    print("\n三种处理：跑 `--write` 重生成登记段 / 改 INDEX 第三节那一行的档与替代物 / "
          "把死链换成现存文档。")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
