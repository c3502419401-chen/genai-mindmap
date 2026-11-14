"""查询扩展开关在 **manual 套**上的真实收益（脚本化扩展器，不依赖 LLM 密钥）。

## 为什么需要这一份

`MINDMAP_QUERY_EXPANSION` 打开后，`_rag` 与 `_search_branch` 走的都是
`search_multi([原问句, corrected, *intents])`；关着时走 `search_parent(原问句)`。
所以"扩展有没有用"这个问题，**只在多路融合那条路上成立**，而
`evaluate_retrieval.py` 只调 `search_parent` / `search_sources`——它量不到这个开关。

上一轮量过一次，结论是"净增 0"。但那一次的 B0 组是**父块级匹配**的多路，比单路
低 5.7pp（23.1% vs 28.8%）：那个 -5.7pp 是入口差异，不是扩展的代价。两个反向因素
混在一起，"0"这个读数说明不了任何事。这一轮 `search_multi` 已经与 `search_parent`
同构（子块匹配 + 子块级融合 + 折叠 + 重排），差距归零（`--suite manual` 之外的
8 支对照：A 30.5% / B0 30.5%，逐项相同）。**在这个地基上再问一次**，得到的数字才
是扩展自己的收益。

## 扩展问法从哪来：脚本化，与 `measure_branch_expansion.py` 同一口径

`plan_query` 要调一次模型，而本机没有 LLM 凭证；更重要的是扩展问法一变，两组
数字就不可比了。所以这里用 `EXPANSIONS` 里**人工写**的 2 条同义问法，规则与
`plan_query` 的提示词一致：同一分支的不同问法、不引入原分支没有的方面。

偏差方向写在明处：**手写的比真实模型的稳**（真人写的时候会不自觉地贴材料用词），
所以这张表对扩展是**偏有利**的。如果连偏有利的读数都不涨，真实模型的扩展只会更差。

## 两组配置

  | 组 | 走哪条路 | 对应生产里的哪一态 |
  |---|---|---|
  | 关 | `search_parent(query)` | `MINDMAP_QUERY_EXPANSION=0` |
  | 开 | `search_multi([query, alt1, alt2])` | `=1` 且 `plan_query` 真给出 intents |

`corrected` 与 `hyde` 两路**刻意不放进来**：前者是"替换原问句"的改写（`plan_query`
的 fallback 只给这一条时，`_rag` 仍然走单路），后者是向量专用路、纯 BM25 下必然
为空。加进来就变成"扩展 + 改写 + HyDE"三件事的合成结果，分不出是谁的贡献。

## 指标

复用 `evaluate_retrieval` 的 `evaluate_case`：`recall@k`（这条 case 在 top-k 内有
没有命中）、`mrr@3`、`coverage@10`。**不重算一遍判定与度量**——两份实现迟早在
"20% 阈值怎么算"这种地方分叉，而分叉之后两张表就不可比了。

延迟是对每条 query 实测的单次耗时（`perf_counter`，同一进程、`asyncio` 单循环），
不是端到端 wall time：`search_multi` 的 3 路在纯 BM25 下是三次 `_bm25_scores`
（纯 CPU），真实代价主要在 embedding 往返上，那张账本脚本量不出来。

## 用法

    cd backend
    python scripts/measure_query_expansion.py                     # 两组 + 逐档
    python scripts/measure_query_expansion.py --hybrid 1          # 有 embedding 凭证时
    python scripts/measure_query_expansion.py --show-diff         # 逐条 case 的名次变化
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
SCRIPTS = Path(__file__).resolve().parent
for path in (BACKEND, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# 复用评估那套零件：材料装载、manual 套构造、命中判定、指标计算。复制一份的话，
# 这张表与 `docs/RETRIEVAL-EVAL.md` 里的数字就不可比了——而它们唯一的价值就是互相比。
import evaluate_retrieval as ev  # noqa: E402

from app.knowledge.service import search_multi, search_parent  # noqa: E402

DEPTH = 10
"""候选池：与 `docs/RETRIEVAL-EVAL.md` 的 `--top-k 1,3,5,10` 同一口径。"""

REPEATS = 3
"""延迟取中位数的重复次数。纯 BM25 是毫秒级，单次测量在 Windows 上抖动很大。"""


# ---------------------------------------------------------------- 同义问法
#
# 逐条对着 `plan_query` 的提示词规则写：同一分支、不同问法、尽量用材料自己的词、
# 不引入原问法没有的方面。键是 `eval_cases_manual.CASES` 里的原问法。
EXPANSIONS: dict[str, tuple[str, str]] = {
    "终止原因不是正常结束时，旧接口返回的是什么": (
        "任务被终止之后旧的接口返回什么值",
        "非正常结束的情况下对外表现是什么",
    ),
    "一级分支的下限为什么是三，整图生成才卡这条吗": (
        "分支数量的下限定成三的理由",
        "这条下限只在整张图生成时生效吗",
    ),
    "两路候选各取多少条再融合，两路的分数能直接比吗": (
        "融合之前每一路各留多少条候选",
        "BM25 分与向量分能直接比较大小吗",
    ),
    "图数据库常驻内存要多少，两跳邻域查询还快吗": (
        "图存储占用的内存有多大",
        "两跳查询的耗时是多少",
    ),
    "验收对象和交付物这两栏，分别卡的是什么": (
        "验收对象这一栏卡的是什么",
        "交付物这一栏要求什么",
    ),
    "薄切片允许薄到什么程度，哪些必须真做": (
        "薄切片的下限在哪里",
        "哪些部分不允许只做假的",
    ),
    "为什么用 http 打开页面时浏览器不让录音": (
        "浏览器录音需要什么安全上下文",
        "非 https 的页面为什么拿不到麦克风",
    ),
    "节点上最多能挂几个孩子": (
        "子节点的数量上限是多少",
        "一个节点能容纳几个下级节点",
    ),
    "截断的解为什么不能原样重试，修复额度留给谁": (
        "被截断的模型输出为什么不能直接重发",
        "修复用的额度分配给了谁",
    ),
    "引入编排框架要新增依赖，这笔账划算吗": (
        "多一个编排框架的依赖代价",
        "为什么最后没有引入编排框架",
    ),
    "手工改过之后接受候选，会不会静默覆盖当前图": (
        "用户手工修改过的节点会不会被覆盖",
        "接受候选的时候冲突怎么处理",
    ),
    "为什么空行比句末标点优先，这不是反过来了吗": (
        "切块时为什么优先在空行断开",
        "分隔符的优先级顺序为什么这样排",
    ),
    "多编几种同义问法去查同一个分支，覆盖率会涨吗": (
        "同义问法对分支覆盖率的实测结果",
        "多路查询扩展有没有让覆盖率上升",
    ),
    "为什么把微服务拆分明确列进不做": (
        "微服务拆分被列进不做清单的理由",
        "不做的范围里为什么包含服务拆分",
    ),
    "结构合理性靠两个人分开盲评，合格线怎么定": (
        "两个人分开盲评结构合理性",
        "盲评的合格线是多少",
    ),
    "迁移为什么顺延到验收结束之后，不是现在做": (
        "迁移为什么放到验收之后",
        "现在不做迁移的原因",
    ),
    "哪些模块的目录建了，但接口只是桩": (
        "哪些目录只留了接口桩",
        "建有目录但没实现的模块有哪些",
    ),
    "哪些功能只是目录加接口桩，其实没有实现": (
        "只有目录与接口桩的功能清单",
        "哪些功能还没有真正实现",
    ),
    "模型有没有可能自己偷偷把文件写到磁盘上": (
        "模型能不能直接落盘写文件",
        "工具层有没有给模型写磁盘的权限",
    ),
    "两个请求同时回来，怎么保证不串到另一张图或者另一个会话": (
        "并发请求之间怎么隔离",
        "两个会话同时写会不会互相污染",
    ),
    "让模型先编一段假答案再去检索，为什么不做": (
        "HyDE 这个方案为什么被放弃",
        "先造假设答案再检索的代价",
    ),
    "材料里本来就有答案，和材料里根本没有要去外面找，这两件事能用一套做法吗": (
        "两种检索形态为什么不能混着谈",
        "材料内有答案与需要外部检索的区别",
    ),
    "把检索摊到导图每个大分支上，次数是不是就有上限了": (
        "分支级检索的次数上限",
        "每个分支各检索一次总共多少回",
    ),
    "建约束和写数据这两件事，能不能放进一个事务": (
        "建约束与写数据能在同一个事务里吗",
        "约束创建的事务边界在哪",
    ),
    "答辩那天网线断了还能讲吗": (
        "离线状态下能不能演示",
        "没有网络时答辩怎么进行",
    ),
    "备案要是批不下来，整个计划怎么收场": (
        "备案没批下来的退路",
        "备案卡住时的备选方案",
    ),
    "模型自己标的出处，能当证据用吗": (
        "模型标注的引用能不能直接采信",
        "自报出处与逐字校验的关系",
    ),
    "前端当初为什么不选 React": (
        "前端技术选型为什么不是 React",
        "选框架时的取舍理由",
    ),
}


def _missing_expansions(dataset) -> list[str]:
    """没有同义问法的 case。**直接报错**，不静默降级成"这一条只跑单路"。

    静默降级会让"开"那一组混进若干条只发了单路的 case，于是它必然≈"关"那一组，
    而报表上看不出任何异常。扩展问法漏写是一个可以当场修好的编辑错误。
    """
    return [case.query for case in dataset.cases if case.query not in EXPANSIONS]


# ---------------------------------------------------------------- 单组测量


class Group:
    """一个配置跑完的读数：逐 case 的指标 + 延迟样本。"""

    def __init__(self, name: str, label: str):
        self.name = name
        self.label = label
        self.scores: list[dict[int, dict[str, float]]] = []
        self.latency: list[float] = []
        self.ranks: dict[str, list[tuple[int, int, int]]] = {}


async def _run_group(
    name: str, label: str, dataset, id_map: dict[str, str], *, expand: bool,
) -> Group:
    """跑完一套 case。延迟与判定都在同一次检索上量，不额外多跑一遍。"""
    group = Group(name, label)
    sources = [material.source for material in dataset.materials]
    ks = (1, 3, 5, DEPTH)

    def ranked_of(hits) -> list[tuple[int, int, int]]:
        return [(index, hit.start, hit.end)
                for index, hit in enumerate(hits, start=1)
                if id_map.get(hit.source_id, "") == fingerprint]

    for case in dataset.cases:
        fingerprint = case.source_hash
        queries = ([case.query, *EXPANSIONS[case.query]] if expand else [case.query])
        samples: list[float] = []
        hits = []
        for attempt in range(REPEATS):
            started = time.perf_counter()
            if expand:
                hits = await search_multi(sources, queries, DEPTH)
            else:
                # 同步调用，与 `_rag` 的形态一致；套一层 `to_thread` 会多一次线程
                # 调度（几十微秒），在纯 BM25 的毫秒级基数上是实打实的偏差。
                hits = search_parent(sources, case.query, DEPTH)
            samples.append(time.perf_counter() - started)
            if attempt == 0:
                continue
        group.latency.append(statistics.median(samples))
        group.ranks[case.query] = [(start, end, rank) for rank, start, end in ranked_of(hits)]
        group.scores.append(ev.evaluate_case(case, ranked_of(hits), ks))
    return group


def _metrics(group: Group, ks=(1, 3, 5, DEPTH)) -> dict[str, dict[int, float]]:
    """按 case 平均。`mrr` 取 k=3、`coverage` 取 k=10，与 `RETRIEVAL-EVAL.md` 的列一致。"""
    total = len(group.scores)
    return {
        name: {
            k: round(sum(row[k][name] for row in group.scores) / total, 6)
            for k in ks
        }
        for name in ("recall", "mrr", "ndcg", "coverage")
    }


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _report(off: Group, on: Group) -> None:
    left = _metrics(off)
    right = _metrics(on)

    header = ("指标", "扩展关", "扩展开", "绝对差", "相对")
    rows: list[tuple[str, ...]] = []
    for k in (1, 3, 5):
        rows.append((
            f"recall@{k}",
            _pct(left["recall"][k]),
            _pct(right["recall"][k]),
            f"{(right['recall'][k] - left['recall'][k]) * 100:+.1f}pp",
            "—" if left["recall"][k] == 0 else
            f"{(right['recall'][k] / left['recall'][k] - 1) * 100:+.0f}%",
        ))
    rows.append((
        "mrr@3",
        _pct(left["mrr"][3]),
        _pct(right["mrr"][3]),
        f"{(right['mrr'][3] - left['mrr'][3]) * 100:+.1f}pp",
        "—" if left["mrr"][3] == 0 else
        f"{(right['mrr'][3] / left['mrr'][3] - 1) * 100:+.0f}%",
    ))
    rows.append((
        "coverage@10",
        _pct(left["coverage"][DEPTH]),
        _pct(right["coverage"][DEPTH]),
        f"{(right['coverage'][DEPTH] - left['coverage'][DEPTH]) * 100:+.1f}pp",
        "—" if left["coverage"][DEPTH] == 0 else
        f"{(right['coverage'][DEPTH] / left['coverage'][DEPTH] - 1) * 100:+.0f}%",
    ))
    latency_off = statistics.median(off.latency)
    latency_on = statistics.median(on.latency)
    rows.append((
        "延迟（单次中位）",
        f"{latency_off * 1000:.1f}ms",
        f"{latency_on * 1000:.1f}ms",
        f"{(latency_on - latency_off) * 1000:+.1f}ms",
        f"{(latency_on / latency_off - 1) * 100:+.0f}%",
    ))

    widths = [max(len(str(row[index])) for row in [header, *rows])
              for index in range(len(header))]
    print()
    for row in [header, *rows]:
        print("  ".join(str(cell).ljust(widths[index])
                        for index, cell in enumerate(row)))


def _print_diff(off: Group, on: Group) -> None:
    """逐条 case 的名次变化：涨了几条、掉了几条、换成了哪一段。"""
    print()
    print("逐条 case（前 10 条命中里，目标章节的排名与覆盖区间）：")
    up = down = same = 0
    for query, ours in off.ranks.items():
        theirs = on.ranks[query]
        first_off = next((rank for _, _, rank in ours), None)
        first_on = next((rank for _, _, rank in theirs), None)
        if first_off == first_on:
            same += 1
            mark = "="
        elif first_on is not None and (first_off is None or first_on < first_off):
            up += 1
            mark = "↑"
        else:
            down += 1
            mark = "↓"
        if mark != "=":
            print(f"  {mark} off={first_off} on={first_on}  {query}")
    print(f"  合计：↑{up}  ↓{down}  ={same}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hybrid", choices=("0", "1"), default="0",
                        help="向量路开关（默认 0）。没有 embedding 凭证时 =1 会退化成纯 BM25")
    parser.add_argument("--top-k", default=str(DEPTH))
    parser.add_argument("--show-diff", action="store_true", help="逐条 case 的名次变化")
    args = parser.parse_args(argv)

    import os
    os.environ["MINDMAP_HYBRID_SEARCH"] = args.hybrid
    os.environ["MINDMAP_PARENT_RETRIEVAL"] = "1"
    os.environ["MINDMAP_RERANK"] = "0"
    os.environ["MINDMAP_LIBRARY_SEARCH"] = "0"
    os.environ["MINDMAP_GRAPH_SEARCH"] = "0"

    materials = ev.load_materials(list(ev.DEFAULT_MATERIALS), [])
    dataset = ev.build_manual_dataset(materials)
    missing = _missing_expansions(dataset)
    if missing:
        raise SystemExit("这些 case 还没写同义问法（不能静默跳过）：\n  "
                         + "\n  ".join(missing))
    id_map = {material.source.id: material.content_hash for material in materials}

    print(f"manual 套：{len(materials)} 份材料 / {len(dataset.cases)} 条手写问法")
    print(f"配置：hybrid={args.hybrid} / parent=1 / rerank=0 / library=0 / "
          f"graph=0；每支 top-{DEPTH}，延迟取 {REPEATS} 次中位")
    from app.knowledge.embedding import configured as embedding_configured
    if args.hybrid == "1" and not embedding_configured():
        print("注意：没有 embedding 凭证，hybrid=1 实际走纯 BM25，与 hybrid=0 相同"
              "——这是环境事实，不是指标相同。")

    async def drain():
        off = await _run_group("off", "只有原问法（search_parent）",
                               dataset, id_map, expand=False)
        on = await _run_group("on", "原问法 + 2 条同义问法（search_multi）",
                              dataset, id_map, expand=True)
        return off, on

    off, on = asyncio.run(drain())
    _report(off, on)
    if args.show_diff:
        _print_diff(off, on)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
