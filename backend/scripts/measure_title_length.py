"""标题长度与检索质量：把 label 从 20–30 字压到 16 字，召回会不会变差？

这一轮把 AI 生成的节点标题限到 16 码点（`GENERATED_LABEL_MAX_CHARS`），而分片
生成的检索 query **就是分支标题**（`_search_branch` → `_branch_query`）。于是
"标题变短"直接改变了检索的输入，需要一个数字而不是一个判断。

复用 `scripts/evaluate_retrieval.py` 的测试集与度量（不改它、不落库、不调模型）：
同一批 case、同一批原文区间，只改 query 文本。三组对照回答三个不同的问题：

  A. 仓库自带材料的**真实章节标题**（中位数 10 字）截到 16：几乎没人被截，
     所以这一组只能证明"这套语料测不出压缩的影响"——不能拿它当结论。
  B. **正文首句**（长、逐字来自材料）截到 24 vs 16：模拟"原来 20–30 字的
     label 压到 16 字"，两者词面同源，差的是尾部那 8 个字。
  C. 同上但截到 24 vs 12：把压缩幅度放大，看曲线陡不陡——如果 24→16 只掉零点
     几 pp 而 24→12 掉很多，说明 16 这条线还没踩到陡坡。

用法：

    cd backend
    python scripts/measure_title_length.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "scripts"))

import evaluate_retrieval as ev  # noqa: E402

SHORT = 16
MEDIUM = 24
AGGRESSIVE = 12


def clip_dataset(dataset: ev.Dataset, limit: int) -> ev.Dataset:
    """把每条 case 的 query 截到 `limit` 码点，区间与材料不动。"""
    return ev.Dataset(
        cases=[
            ev.EvalCase(query=case.query[:limit], source_hash=case.source_hash,
                        relevant=case.relevant)
            for case in dataset.cases
        ],
        materials=dataset.materials,
        stats=dataset.stats,
    )


def body_sentence_dataset(dataset: ev.Dataset) -> ev.Dataset:
    """用章节正文的第一句当 query：长、啰嗦、但逐字来自材料。

    这是"20–30 字的模型标题"最接近的替代品——真实导图的 label 也是从正文里
    概括出来的一句话，而不是原文的标题行。
    """
    by_hash = {material.content_hash: material for material in dataset.materials}
    cases = []
    for case in dataset.cases:
        text = by_hash[case.source_hash].text
        start, end = case.relevant[0]
        sentence = ev._first_body_sentence(text[start:end])
        if not sentence:
            continue
        cases.append(ev.EvalCase(query=sentence, source_hash=case.source_hash,
                                 relevant=case.relevant))
    return ev.Dataset(cases=cases, materials=dataset.materials, stats=dataset.stats)


def report(name: str, base: ev.Dataset, other: ev.Dataset, config, top_k) -> None:
    with ev._id_space(base.materials, need_library=False) as id_map:
        left = ev.score_config(base, config, top_k, id_map)
        right = ev.score_config(other, config, top_k, id_map)
    print(f"== {name}（{len(base.cases)} 条 vs {len(other.cases)} 条）")
    for metric in ("recall", "coverage", "ndcg"):
        cells = []
        for k in top_k:
            before = left["metrics"][metric][str(k)]
            after = right["metrics"][metric][str(k)]
            cells.append(f"@{k} {before:.3f}→{after:.3f} ({(after - before) * 100:+.1f}pp)")
        print(f"   {metric:9s} " + "  ".join(cells))
    print()


def main() -> int:
    materials = ev.load_materials(list(ev.DEFAULT_MATERIALS), [])
    dataset = ev.build_dataset(materials)
    lengths = sorted(len(case.query) for case in dataset.cases)
    print(f"材料 {len(materials)} 份，case {len(dataset.cases)} 条，"
          f"章节标题长度 min/median/max = {lengths[0]}/{lengths[len(lengths) // 2]}/{lengths[-1]}\n")

    long_titles = body_sentence_dataset(dataset)
    long_lengths = sorted(len(case.query) for case in long_titles.cases)
    print(f"模拟长标题（正文首句）：{len(long_titles.cases)} 条，"
          f"长度 min/median/max = {long_lengths[0]}/{long_lengths[len(long_lengths) // 2]}/"
          f"{long_lengths[-1]}\n")

    top_k = [1, 3, 5]
    configs = [
        # 单级 BM25：分片检索在"父块返回"关掉时走这条。
        ev.Config(source_mode="rag", hybrid=False, parent=False, library=False),
        # 线上默认：父块返回（`MINDMAP_PARENT_RETRIEVAL`）。
        ev.Config(source_mode="rag", hybrid=False, parent=True, library=False),
    ]
    for config in configs:
        print(f"########## {config.label()}")
        report(f"A. 真实章节标题 → {SHORT} 字", dataset, clip_dataset(dataset, SHORT),
               config, top_k)
        report(f"B. 模拟长标题 {MEDIUM} → {SHORT} 字",
               clip_dataset(long_titles, MEDIUM), clip_dataset(long_titles, SHORT),
               config, top_k)
        report(f"C. 模拟长标题 {MEDIUM} → {AGGRESSIVE} 字",
               clip_dataset(long_titles, MEDIUM), clip_dataset(long_titles, AGGRESSIVE),
               config, top_k)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
