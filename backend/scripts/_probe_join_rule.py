"""第三问：把"该拼"与"不该拼"变成可算的判据，并在 74 对真实压链对上回测。

判据方向的选择（这一轮的核心判断）
-----------------------------------
禁止拼的条件写成**白名单**（"只有上位词或量化补充才许拼"）时，谁来兜剩下的一大类
「维度名 + 具体内容」？回测会说：那类占 3/4。白名单会把能读的也赶进第三档，
于是画布上大批外层标题消失——那不是修可读，是换了一种不可读。

所以判据写成**黑名单**：只拦"两段在说同一件事"。可算的特征就是**两段共享的
连续汉字**：复述必有共同主题词（讲的是同一个对象），互补则各在各的域里
（维度词 vs 取值）。

跑法：`python scripts/_probe_join_rule.py`
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.optimization.repair import LABEL_JOINER, READABLE_LABEL_CHARS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "data" / "acc_trees"
HAN = re.compile(r"[\u4e00-\u9fff]")
# 标点与数字不参与"共享主题词"：`20%` 与 `两成` 是同一个数的两种写法，但那要靠
# 数字判据（见下），不是靠汉字重合。
STRIP = re.compile(r"[\s\u3000-\u303f\uff00-\uff65\d%%.+／/·—\-()（）]")


def han_only(text: str) -> str:
    return "".join(HAN.findall(text))


def shared_run(left: str, right: str) -> str:
    """最长公共**连续**汉字子串。"""
    a, b = han_only(left), han_only(right)
    if not a or not b:
        return ""
    best = ""
    # 朴素 DP：标题 ≤24 字，O(n*m) 无所谓，写清楚比写快重要。
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > len(best):
                    best = a[i - cur[j]:i]
        prev = cur
    return best


def walk(node: dict, depth: int = 1):
    yield node, depth
    for child in node.get("children") or []:
        yield from walk(child, depth + 1)


def seg_count(node_id: str) -> int:
    return len([p for p in re.split(r"-", node_id or "") if p])


def pairs() -> list[tuple[str, str, str]]:
    """(树, id, label) → 只取压链产物（id 跳代）。"""
    out = []
    for file in sorted(ROOT.glob("S*.json")):
        tree = json.loads(file.read_text(encoding="utf-8"))
        for node, depth in walk(tree["root"]):
            label = (node.get("label") or "").strip()
            if LABEL_JOINER not in label:
                continue
            if seg_count(node.get("id", "")) <= depth - 1:
                continue  # 模型自己写的并列，不归这条规则管
            left, _, right = label.partition(LABEL_JOINER)
            out.append((file.stem, node.get("id", ""), left, right))
    return out


#: 人工 ground truth：这些是**同一件事的两种说法**，拼起来是重复而不是补充。
#:
#: `S06/b2-1-1`「文体演变主线目标 / 把握各体文学演进脉络」**不在**这张表里，
#: 虽然上一轮报告把它和 `b4-1-1` 并列点名。逐对读过之后的判断是：它属于
#: 「维度名 + 它的取值」（left 是一个维度、right 是这一维度的具体内容），
#: 与 `S16/s2-3-1`「培训与准入挂钩 / 未通过培训不得预约高风险设备」同一类——
#: 拼起来读作"A 下的 B"，是合法的层级表达，不是复述。把它算作"该拦"会让整张
#: ground truth 自相矛盾：同类的 54 条里就要拦掉一半以上。
REPEAT = {
    ("S04", "g3-1-1"), ("S06", "b2-3-1"), ("S06", "b4-1-1"),
    ("S06", "b4-2-1"), ("S06", "b4-3-1"), ("S06", "b4-4-1"), ("S06", "b5-1-1"),
    ("S06", "b5-3-1"), ("S07", "g5-1-1"), ("S08", "s1-1-1"), ("S10", "s1-3-1"),
    ("S11", "s2-3-1"), ("S11", "s2-4-1"), ("S12", "s1-1-1"), ("S12", "s3-1"),
    ("S13", "s3-1-1"), ("S16", "s7-1-1"), ("S18", "s1-2-1"), ("S20", "s2-1-1"),
    ("S20", "s5-4-1"),
}


def main() -> None:
    rows = pairs()
    print(f"压链产出的 ` / ` 节点：{len(rows)}（另一条 S12/s2-3 是模型写的，不计）\n")
    hits = 0
    for file, nid, left, right in rows:
        run = shared_run(left, right)
        truth = (file, nid) in REPEAT
        guess = len(run) >= 2
        mark = "✓" if guess == truth else "✗"
        hits += guess == truth
        joined = f"{left}{LABEL_JOINER}{right}"
        print(f"{mark} 共{len(run)}「{run}」 len={len(joined):>2} "
              f"{'该拦' if truth else '该拼'} → {'拦' if guess else '拼'}  {joined}")

    guess_repeat = [r for r in rows if len(shared_run(r[2], r[3])) >= 2]
    print()
    print(f"一致 {hits}/{len(rows)}；判据拦下 {len(guess_repeat)} 条，"
          f"其中真复述 {len([r for r in guess_repeat if (r[0], r[1]) in REPEAT])} 条")
    over = [r for r in guess_repeat if (r[0], r[1]) not in REPEAT]
    print(f"误杀（能读却被降级）{len(over)} 条：")
    for file, nid, left, right in over:
        print(f"    {file} {nid:<10} {left}{LABEL_JOINER}{right}")
    miss = [r for r in rows if (r[0], r[1]) in REPEAT and len(shared_run(r[2], r[3])) < 2]
    print(f"漏放（复述却仍拼）{len(miss)} 条：")
    for file, nid, left, right in miss:
        print(f"    {file} {nid:<10} {left}{LABEL_JOINER}{right}")


if __name__ == "__main__":
    main()
