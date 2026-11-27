"""label 撞 16 字上限之后，切点落在哪里：可确证的"被切"有多少、自然 16 字有多少。

## 这个脚本回答的问题

`docs/ACCEPTANCE-REPORT.md` §6.4① 写的是"label 16 字硬切导致 **44 个节点被截成半句**"，
而 `app/agent/tools.py:2624` 写的是"宁可丢掉那半句"。两份材料在"要不要把被切下的
后半截搬进 `detail`"上结论相反，所以要有一个数，而不是再吵一遍。

第一版提问方式（"44 个里几个切在词中间"）**本身问不出来**：`clip_label` 是
`text[:16]`，原文的后半截在落库那一刻就没了，`len(label) == 16` 也不等于"被切过"
——模型自己写一个 16 字完整句，落在同一个桶里（主库里 `机器学习是人工智能的一个分支领域`
这种句子有一大批）。

所以换三个能证的角度：

- **A · 长度不是证据，形状才是。** `len(label) == 16` 的节点里，有多少带着"被切"的
  结构痕迹（未闭合的括号/书名号、末尾被截断的拉丁或数字 token）。
- **B · 谁造的这条 label。** 含 `" / "` 的是 `repair._absorb` 拼出来的
  （两半拼起来正好 16），与 16 这条限制无关，不能算进"被切"。
- **C · 直方图有没有断层。** 如果 16 是生成侧硬切且**全覆盖**，那 17 字以上应该一个都
  没有。实测有，而它们全部含 `" / "` —— 这反过来证明"生成侧 clip 全覆盖、长出来的
  都是事后拼接"。

## 四档判据（都只认结构证据，不猜中文词）

| 档 | 判据 | 误判 |
|---|---|---|
| `joined` | label 含 `" / "` | 零：这是 `repair` 的拼接符 |
| `clipped_symbol` | 存在未闭合的成对符号（`《》（）()[]“”【】`） | 零：没人故意写半个括号 |
| `clipped_token` | 末尾的拉丁/数字 token 在同一棵树的任一 `detail`/`label` 里以更长串出现 | 零：`Kru`→`Kruskal` 这种对得上就是被切 |
| `unverified` | 其余 | **不是"自然"，是"这份数据判不了"** |

`unverified` 里一半是自然完整句（闭合括号结尾、中文短句），一半是**中文词内切**
（`与归一` 后面的"化"没了）。中文没有词边界符，`服务器` 和 `与归一` 在机制上不可区分
——这条限制写在 `docs/LABEL-CLIP-VERDICT.md`，不藏。

## 跑法

    cd backend
    python scripts/measure_label_clip.py                 # 两批样本都跑，打印
    python scripts/measure_label_clip.py --out scripts/_label_clip.json

两批样本来源与用途不同，必须分开报：

- `backend/data/acc_trees/*.json`：验收样本（20 棵 / 1130 节点），`ACCEPTANCE-REPORT` 的
  "44 个"就出自这一批；
- `backend/data/mindmaps.db` 的 `mindmaps` 表：开发过程中真实攒下的图（24 棵 / 1085 节点），
  里面有一整批 16 字完整中文短句，也有一批被切在英文标识符中间的。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.contracts.mindmap import GENERATED_LABEL_MAX_CHARS  # noqa: E402

DATA = BACKEND / "data"
DEFAULT_OUT = BACKEND / "scripts" / "_label_clip.json"

#: 成对符号。开符号多于闭符号 = 切在引用内部。
PAIRS = {"《": "》", "（": "）", "(": ")", "[": "]", "【": "】", "“": "”", "「": "」"}

#: 拉丁/数字 token 的字符集。`#`/`.`/`+`/`%`/`≥` 都进 token：`#218`、`Node.js`、
#: `C++`、`35%`、`≥90` 被切开时，切口两侧都在这套字符里。
TOKEN_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "._+#%≥<=/-"
)

#: 找"末尾 token"时只认这些：字母、数字、以及词内连接符。
#:
#: **不能用 `TOKEN_CHARS`**：那里的 `=`/`+`/`≥` 是"切口可能落在运算符旁边"的判据，
#: 拿它取末尾 token 会让 `revision=baseRev` 整串成为一个 token，于是在 `baseRevision`
#: 里找不到它——那是把一条真被切的 label 判成"判不了"（实测主库里就漏了它）。
_TAIL_TOKEN_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


@dataclass
class Hit:
    """一条撞上限的 label。"""

    batch: str
    where: str
    label: str
    detail: str
    verdict: str
    evidence: str = ""


@dataclass
class Report:
    counts: Counter[str] = field(default_factory=Counter)
    hits: list[Hit] = field(default_factory=list)
    histograms: dict[str, Counter[int]] = field(default_factory=dict)


def walk(node: dict):
    yield node
    for child in node.get("children") or []:
        yield from walk(child)


def _unclosed(label: str) -> str:
    """第一个开得比闭得多的符号（没有则空串）。"""
    for opener, closer in PAIRS.items():
        if label.count(opener) > label.count(closer):
            return opener
    return ""


def _tail_token(label: str) -> str:
    """label 末尾的拉丁/数字串。空串表示末字是汉字或标点。"""
    end = len(label)
    start = end
    while start > 0 and label[start - 1] in _TAIL_TOKEN_CHARS:
        start -= 1
    return label[start:end]


def _grows(token: str, corpus: str) -> str:
    """token 在 `corpus` 里以更长 token 出现时，返回那个更长的串。"""
    if not token:
        return ""
    index = corpus.find(token)
    while index >= 0:
        end = index + len(token)
        while end < len(corpus) and corpus[end] in _TAIL_TOKEN_CHARS:
            end += 1
        if end > index + len(token):
            return corpus[index:end]
        index = corpus.find(token, index + 1)
    return ""


def classify(label: str, detail: str, corpus: str) -> tuple[str, str]:
    """→ (档, 证据)。

    证据串只认**同一节点 `detail` 里**的那一份：全树语料里随便找到一个以同前缀
    开头的串就判"被切"，会把 `issue #21` 的证据写成 `21ms`（同一棵树另一条 detail
    里的数）——结论对、证据错，那种证据经不起复核。全树语料只在明细里当弱线索列。
    """
    if " / " in label:
        return "joined", ""
    opener = _unclosed(label)
    if opener:
        return "clipped_symbol", f"未闭合的 {opener}"
    token = _tail_token(label)
    grown = _grows(token, detail)
    if grown:
        return "clipped_token", f"{token} → {grown}"
    weak = _grows(token, corpus)
    return "unverified", (f"同树语料有 {token} → {weak}" if weak else "")


def scan(batch: str, trees: list[tuple[str, dict]], report: Report) -> None:
    histogram: Counter[int] = Counter()
    for where, root in trees:
        nodes = list(walk(root))
        # 同一棵树的 detail/label 全文当语料：`Kru` 的 `Kruskal` 可能只写在该树的
        # 另一条 detail 里，限定在自身 detail 会漏。
        corpus = "\n".join(
            f"{node.get('label') or ''}\n{node.get('detail') or ''}" for node in nodes
        )
        for node in nodes:
            label = node.get("label") or ""
            histogram[len(label)] += 1
            report.counts[f"{batch}:nodes"] += 1
            if len(label) != GENERATED_LABEL_MAX_CHARS:
                continue
            verdict, evidence = classify(label, node.get("detail") or "", corpus)
            report.counts[f"{batch}:{verdict}"] += 1
            report.hits.append(Hit(batch, where, label, (node.get("detail") or "")[:80],
                                   verdict, evidence))
    report.histograms[batch] = histogram


def load_acc_trees() -> list[tuple[str, dict]]:
    out = []
    for path in sorted((DATA / "acc_trees").glob("*.json")):
        tree = json.loads(path.read_text(encoding="utf-8"))
        content = tree.get("content") or tree
        out.append((path.stem, content.get("root") or content))
    return out


def load_db() -> list[tuple[str, dict]]:
    db = DATA / "mindmaps.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    out = []
    for row in conn.execute("select id, content from mindmaps"):
        content = json.loads(row["content"])
        out.append((str(row["id"])[:8], content.get("root") or content))
    return out


def print_histogram(batch: str, histogram: Counter[int]) -> None:
    top = max(histogram) if histogram else 0
    print(f"  长度分布 2..{top}：")
    for length in range(2, top + 1):
        count = histogram[length]
        if not count:
            continue
        bar = "#" * min(count, 60)
        mark = "  ← 上限" if length == GENERATED_LABEL_MAX_CHARS else ""
        print(f"    {length:3d} {count:4d} {bar}{mark}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="label 撞 16 字上限后的切点分布")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="明细 JSON 落盘位置")
    parser.add_argument("--quiet", action="store_true", help="只打印汇总，不列明细")
    args = parser.parse_args(argv)

    report = Report()
    batches = {
        "acc_trees": load_acc_trees(),
        "mindmaps.db": load_db(),
    }
    for batch, trees in batches.items():
        if not trees:
            print(f"跳过 {batch}：没有样本")
            continue
        scan(batch, trees, report)

    print(f"上限 GENERATED_LABEL_MAX_CHARS = {GENERATED_LABEL_MAX_CHARS}\n")
    for batch, trees in batches.items():
        if not trees:
            continue
        nodes = report.counts[f"{batch}:nodes"]
        print(f"== {batch}：{len(trees)} 棵 / {nodes} 节点")
        print_histogram(batch, report.histograms[batch])
        for verdict in ("joined", "clipped_symbol", "clipped_token", "unverified"):
            print(f"    {verdict:<15} {report.counts[f'{batch}:{verdict}']:4d}")
        print()
        if not args.quiet:
            for hit in report.hits:
                if hit.batch != batch or hit.verdict == "unverified":
                    continue
                print(f"      {hit.verdict:<15} [{hit.where}] {hit.label!r}"
                      + (f"  {hit.evidence}" if hit.evidence else ""))

    if args.out:
        Path(args.out).write_text(json.dumps({
            "limit": GENERATED_LABEL_MAX_CHARS,
            "counts": dict(report.counts),
            "hits": [hit.__dict__ for hit in report.hits],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明细落盘：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
