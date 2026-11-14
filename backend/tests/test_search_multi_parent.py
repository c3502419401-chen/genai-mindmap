"""多路检索的**子块粒度**：`MINDMAP_PARENT_RETRIEVAL=1` 时的 `search_multi`。

这一组用例守的是"多路融合与单路检索在同一个粒度上"这件事。改动之前，多路那条
入口是 `_collect_chunks`（父块直接匹配），而单路走 `_search_parent_core`（子块
匹配、命中后折父块）——同一份材料、同样 8 支、纯 BM25 的覆盖率差 5.7pp（23.1%
vs 28.8%）。那 5.7pp 混在"查询扩展有没有收益"里，把扩展的真实收益冲掉了，所以
先要把入口修平。八条主线：

1. **匹配单元是子块**。两个 query 落在同一个父块的不同子块上：父块级完全分不开，
   子块级分得开（`leaf_start/leaf_end` 不同，且各自包含自己那半句）。
2. **融合在子块级**。一路命中的是子块 A、另一路是子块 B，融合之后"哪一小段最
   相关"必须还能变——先折父块再融合时，父块只有一条，这个信息已经没了。
3. **折叠在融合之后**。同一父块的 N 个子块命中并成一条，`leaf_*` 是最优那个。
4. **重排在折叠之后**。reranker 收到的候选全是父块（`start/end` 是父块、`leaf_*`
   非 None），且池子按 `pool_size` 扩过——与 `search_parent` 完全一致。
5. **保底席位仍然生效**，且席位的单位是父块（子块上保 3 席，折完可能只剩 1 个
   父块，不变量会被静默削掉）。
6. **`library=True` 的保底半席**在折叠之后照样成立。
7. **`hyde` 那一路仍然只用向量**：它一个字都不进 BM25。
8. **开关关着时逐字节等于 `1342942`**：粒度开关关掉，多路不该单方面改变返回形状。

全部离线：`conftest.py` 已经把凭证清空、开关置 0（含 `MINDMAP_PARENT_RETRIEVAL`），
这里需要子块粒度的用例自己打开。
"""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from app.contracts.source import Source, SourceKind
from app.knowledge import library as knowledge_library
from app.knowledge import rerank, service
from app.knowledge.service import (
    PARENT_RETRIEVAL_ENV,
    _bm25_scores,
    _collect_leaves,
    search_multi,
    search_parent,
)

# 写死，不跟随 HEAD：这一轮改的就是 `search_multi`，拿 HEAD 对比等于拿自己跟
# 自己比。sha256 一并写死——只写 rev 的话，改掉常量就能悄悄换一个"基线"。
BASELINE_REV = "1342942"
BASELINE_SHA256 = "30994712bfda614030ec957d53c82efef99e9cb98eea6aebea622da0194795d0"

REPO_ROOT = Path(__file__).resolve().parents[2]

# 十四句互不共享关键词的正文，约 600 字：刚好切出**一个**父块（< CHUNK_MAX_CHARS）
# 与四个子块。一个父块是要紧的——它让"父块级根本分不开"这件事成为构造事实，
# 而不是靠断言凑出来的。
SENTENCES = (
    "预算审批由部门负责人签核，超过额度必须上报总经理复审，财务只在核准之后执行付款并留存凭证。",
    "费用报销的时限是三十个自然日，逾期不再受理，票据缺失的申请一律退回并通知经办人补齐材料。",
    "采购合同必须经过法务审核，金额超过五十万元另行招标，供应商的资质与授权链条逐项核对归档。",
    "会议纪要由记录人当日整理，决议事项逐条落到责任人，下次例会先复核上一轮的落实情况并记录。",
    "档案归集按年度与部门两级分类，电子件与纸质件分开编号，借阅要留下登记与归还日期备查。",
    "培训计划每季度滚动调整，讲师与场地提前两周确认，缺席超过三次的学员要重新报名并补课时。",
    "设备维护按周巡检按月保养，异常记录当天上报，停机超过四小时启动备机流程并通知厂商到场。",
    "安全巡检覆盖消防通道与配电间，隐患整改单闭环后才允许销项，复检结果留档三年备查并上报。",
    "客户回访按成交金额分级，季度内至少一次电话沟通，投诉工单要在四十八小时内响应并回访闭环。",
    "数据备份每日增量每周全量，异地副本保留三十天，恢复演练每半年做一次并记录耗时与结论。",
    "招聘需求由用人部门提出，编制与预算双审通过后才发布，面试评价表在流程结束前必须填完。",
    "绩效目标按季度对齐，评分分布要避免全部集中在最高档，申诉在结果公布后五个工作日内提。",
    "供应商入库要验营业执照与开户许可，关联关系声明每年更新一次，未更新的一律暂停下单权限。",
    "固定资产盘点每年一次，盘盈盘亏要写明原因与责任人，处置单经财务复核后才允许报废销账。",
)

# 十个互不相关的主题各重复三遍、换行分隔：切成三个父块、九个子块。保底席位与
# 跨批融合需要"多个父块各自有分"，一个父块的材料量不出来。
TOPICS = (
    "预算审批流程由部门负责人核准，超过额度的部分必须上报总经理复审，财务只在核准后执行付款。",
    "烹饪时美拉德反应让食材表面产生褐色风味，温度超过一百四十度后反应速率明显加快，锅具预热很关键。",
    "城市地铁的客流调度依赖实时刷卡数据，早高峰的运力缺口靠区间车与限流共同消化，站厅引导也要配合。",
    "蛋白质折叠的自由能景观由氨基酸序列与溶剂环境共同决定，错误折叠会聚集成淀粉样纤维并致病。",
    "超导体的临界温度取决于晶格振动与电子配对强度，高压下氢化物体系已接近室温区间，机制仍有争议。",
    "合同条款必须经法务审核，违约金上限与争议解决地另行约定，签署前还需确认对方授权链条完整。",
    "天文观测显示星系旋转曲线偏离牛顿预测，暗物质晕的质量分布是目前最主流的解释，但尚未直接探测。",
    "疫苗冷链运输要求全程两到八摄氏度，温度记录仪的数据要在签收时逐段核对，超温批次一律退回。",
    "森林碳汇的计量需要样方调查与遥感校验，灌木与土壤有机碳通常单独建账，重复计算是常见问题。",
    "量子隧穿解释了扫描隧道显微镜的分辨率，针尖与样品的波函数重叠决定了隧穿电流，反馈回路保持间距。",
)


@pytest.fixture
def parent_on(monkeypatch):
    """显式打开两级检索。`conftest.py` 的默认值是 0（与关着时的等价用例共用一套）。"""
    monkeypatch.setenv(PARENT_RETRIEVAL_ENV, "1")


def run(coro):
    return asyncio.run(coro)


def _source(text: str) -> Source:
    return Source(kind=SourceKind.text, status="parsed", text=text)


def _material() -> str:
    """一个父块、四个子块的那份材料（约 600 字）。"""
    return "".join(SENTENCES)


def _mixed_material() -> str:
    """三个父块、九个子块的那份材料（约 1400 字）。"""
    return "\n\n".join(topic * 3 for topic in TOPICS)


def _wide_material(paragraphs: int = 40) -> str:
    """十三个父块的正文：每段都含「预算审批」，所以每个父块都有分、排序有区分度。

    重排那条用例要的是一个"折叠之后还能凑满 `pool_size` 条候选"的池子——一个
    父块的材料上，折叠把候选压到 1 条，扩池与不扩池看起来一样。
    """
    segments = []
    for index in range(paragraphs):
        head = f"预算审批第{index:02d}笔由部门负责人签核，超过额度上报总经理复审。"
        middle = f"本段为第{index:02d}项说明，附经办人意见与归档编号{index:02d}。"
        filler = "原始凭证于次月十日前归档，逾期由财务复核。" * 4
        segments.append(head + middle + filler)
    return "\n\n".join(segments)


def _leaf_of(hit) -> tuple[int, int]:
    """这条命中真正匹配上的子块区间。父块模式下它必须存在。"""
    assert hit.leaf_start is not None and hit.leaf_end is not None, \
        "子块模式的命中必须带 leaf_start/leaf_end"
    return (hit.leaf_start, hit.leaf_end)


def _leaf_text(hit) -> str:
    start, end = _leaf_of(hit)
    return hit.quote[start - hit.start:end - hit.start]


def _dump(hits) -> list[dict]:
    return [hit.model_dump() for hit in hits]


def _baseline_dump(hits) -> list[dict]:
    """基线模块的 `EvidenceHit` 比当前少几个后加的字段，补齐 None 再逐键比。

    `origin` 是图检索那一轮的字段（默认 `None` = "这条来自文本检索"），`1342942`
    的 `service.py` 里还没有它。补 `None` 而不是跳过这个键：一旦当前实现在
    `search_multi` 里把它填成 `"graph"`，`==` 立刻会红——跳过就等于给"多路检索
    静默混进图命中"开了个后门。
    """
    payload = _dump(hits)
    for entry in payload:
        entry.setdefault("origin", None)
    return payload


def _first_difference(expected: list[dict], actual: list[dict]) -> str:
    """逐键报出第一处不同。pytest 对 list[dict] 的 diff 会把整段 `quote` 打出来，
    反而盖住真正的那个字段名——这里是"哪一条、哪个键、两边各是什么"。"""
    if len(expected) != len(actual):
        return f"条数不同：{len(expected)} vs {len(actual)}"
    for index, (left, right) in enumerate(zip(expected, actual)):
        if set(left) != set(right):
            return (f"第 {index} 条的键集不同："
                    f"多出 {sorted(set(right) - set(left))} / 缺少 {sorted(set(left) - set(right))}")
        for key in sorted(left):
            if left[key] != right[key]:
                return (f"第 {index} 条 `{key}` 不同："
                        f"{left[key]!r:.60} vs {right[key]!r:.60}")
        if left != right:
            return f"第 {index} 条逐键相同但 dict 不等：{left!r:.80} vs {right!r:.80}"
    return "逐条相同（问题出在列表以外）"


def _load_baseline_module(tmp_path):
    """把 `BASELINE_REV` 的 `service.py` 载成独立模块，并核对 sha256。"""
    try:
        raw = subprocess.run(
            ["git", "show", f"{BASELINE_REV}:backend/app/knowledge/service.py"],
            cwd=REPO_ROOT, capture_output=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:      # pragma: no cover
        pytest.skip(f"读不到 {BASELINE_REV} 的 service.py：{exc}")

    path = tmp_path / f"service_{BASELINE_REV}.py"
    path.write_bytes(raw)
    spec = importlib.util.spec_from_file_location(f"service_baseline_{BASELINE_REV}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, raw


# ---------------------------------------------------------------- 1 匹配单元


def test_match_unit_is_the_leaf_not_the_parent(parent_on):
    """两个 query 落在同一父块的不同子块上：父块级分不开，子块级分得开。

    「父块级分不开」是这份材料的构造事实——它只有一个父块（< `CHUNK_MAX_CHARS`），
    所以两个 query 的 `start/end/quote` 逐字段相同。差别只能来自子块匹配。
    """
    sources = [_source(_material())]
    budget = run(search_multi(sources, ["预算审批"], 3))
    stock = run(search_multi(sources, ["固定资产盘点"], 3))

    assert budget and stock
    # 父块这一层：同一条证据（同一个窗口），完全区分不出来
    assert [(hit.start, hit.end) for hit in budget] == \
           [(hit.start, hit.end) for hit in stock]
    # 子块这一层：各自命中自己那半句
    assert _leaf_of(budget[0]) != _leaf_of(stock[0])
    assert "预算审批" in _leaf_text(budget[0])
    assert "固定资产盘点" in _leaf_text(stock[0])

    # 子块必须落在父块内，且 `quote` 是父块正文（两级坐标是同一份原文的两层）
    for hit in [*budget, *stock]:
        assert hit.start <= hit.leaf_start < hit.leaf_end <= hit.end
        assert hit.quote == _material()[hit.start:hit.end]


# ---------------------------------------------------------------- 2 融合在子块级


def test_fusion_happens_on_leaves_not_parents(parent_on):
    """扩展路把**另一个子块**抬到第一：这个结论只有子块级融合才拿得到。

    单路问「预算审批」时，第一名是第一个子块；加上「固定资产盘点」那一路之后，
    第四个父块内的子块票数更高（它同时被两路命中），于是折出来的父块虽然还是
    同一个，`leaf_*` 却换了。

    先折父块再融合时，两路命中的是**同一个**父块，融合结果只有一条，而"哪一小段
    最相关"这个信息在折叠那一步就丢了——`leaf_*` 只能退回第一路的第一名。
    """
    sources = [_source(_material())]
    single = run(search_multi(sources, ["预算审批"], 3))
    fused = run(search_multi(sources, ["预算审批", "固定资产盘点"], 3))

    assert _leaf_of(single[0]) != _leaf_of(fused[0]), \
        "两路融合必须能改变'哪一小段最相关'，否则融合发生在父块级"
    # 折完只剩一条：两路都落在同一个父块里
    assert len(fused) == 1
    assert (fused[0].start, fused[0].end) == (single[0].start, single[0].end)
    # 选中的那个叶子必须也是第二路命中的子块（这里靠"它在第二路里有正分"断言，
    # 不拿分数比大小：单路是 BM25 原始分、多路是 RRF 累加，两种口径不可比）
    corpus = _collect_leaves(sources)
    selected = _leaf_of(fused[0])
    index = next(position for position, item in enumerate(corpus.items)
                 if (item[1], item[2]) == selected)
    second = _bm25_scores("固定资产盘点", [item[3] for item in corpus.items])
    assert second[index] > 0


def test_fusion_accumulates_on_the_same_leaf_by_rank(parent_on):
    """同一子块被两路命中 = `1/61 + 1/61`，与单级块模式同一条 RRF 口径。"""
    sources = [_source(_material())]
    hits = run(search_multi(sources, ["设备维护", "按月保养"], 3))
    assert hits
    # 两路都命中的那个子块折出来的父块，分数必须是两路名次倒数之和
    best = hits[0]
    assert best.score == pytest.approx(round(2 / (60 + 1), 6), abs=1e-6)


# ---------------------------------------------------------------- 3 折叠


def test_folding_merges_every_leaf_of_one_parent(parent_on):
    """同一父块的多个子块命中 → 一条父块命中，`leaf_*` 是分数最高那个子块。

    「预算审批」在这份材料里只出现在第一句，但 BM25 会给同一父块里的其他子块
    正分（子块之间有 100 字重叠，第二块的头就是第一块的尾）。折叠必须把它们并成
    一条，否则 `top_k=3` 会被同一段上下文的三次摘录占满。
    """
    sources = [_source(_material())]
    hits = run(search_multi(sources, ["预算审批"], 3))

    assert len(hits) == 1, "只有一个父块，折叠后必须只有一条"
    # 折叠前确实有多个子块拿到正分（否则这条用例证明不了合并）
    corpus = _collect_leaves(sources)
    lexical = _bm25_scores("预算审批", [item[3] for item in corpus.items])
    assert sum(1 for value in lexical if value > 0) >= 2

    hit = hits[0]
    # 保留的是分数最高的那个子块：它就是 BM25 第一名（同分取更靠前的下标）
    top_index = max(range(len(lexical)), key=lambda index: (lexical[index], -index))
    assert _leaf_of(hit) == (corpus.items[top_index][1], corpus.items[top_index][2])
    assert _leaf_text(hit).startswith("预算审批")


# ---------------------------------------------------------------- 4 重排在折叠之后


def _fake_rerank(monkeypatch) -> list[dict]:
    """把重排换成记录入参的替身：只要能证明"它收到的是折叠后的父块"就够了。"""
    calls: list[dict] = []
    monkeypatch.setattr(service.knowledge_rerank, "enabled", lambda: True)
    monkeypatch.setattr(service.knowledge_rerank, "configured", lambda: True)

    def fake(query: str, hits, top_k: int):
        calls.append({"query": query, "hits": list(hits), "top_k": top_k})
        return list(hits)[:top_k]

    monkeypatch.setattr(service.knowledge_rerank, "rerank", fake)
    return calls


def test_reranker_runs_after_folding(parent_on, monkeypatch):
    """候选池按 `pool_size` 扩，且交给 reranker 的每一条都已经是**父块**。

    这条能抓住两种错法：重排接在折叠之前（候选里出现 `leaf_* is None` 或同一个
    父块的多条子块），以及多路模式忘了扩池（候选数被卡在 `top_k`）。
    """
    calls = _fake_rerank(monkeypatch)
    sources = [_source(_wide_material())]

    hits = run(search_multi(sources, ["预算审批", "部门负责人签核"], 2))

    assert len(calls) == 1, "整次检索只该重排一次，不是每路各排一遍"
    pool = calls[0]["hits"]
    assert len(pool) == rerank.pool_size(2), "候选池必须扩到 pool_size"
    assert all(hit.leaf_start is not None for hit in pool), \
        "交出去重排的必须是折叠后的父块命中"
    # 折叠不变量：每条对应不同的父块
    parents = [(hit.source_id, hit.start, hit.end) for hit in pool]
    assert len(set(parents)) == len(parents)
    # 重排的 query 是第一路（原始问题），不是扩展问法
    assert calls[0]["query"] == "预算审批"
    assert len(hits) == 2


def test_reranker_receives_parents_not_leaves(parent_on, monkeypatch):
    """重排前后证据一个都不动：`start/end/quote/leaf_*` 逐字段相同，只换顺序。"""
    calls = _fake_rerank(monkeypatch)
    sources = [_source(_wide_material())]
    hits = run(search_multi(sources, ["预算审批", "部门负责人签核"], 3))

    pool = calls[0]["hits"]
    assert len(pool) >= 3
    by_key = {(hit.source_id, hit.start, hit.end): hit for hit in pool}
    for hit in hits:
        original = by_key[(hit.source_id, hit.start, hit.end)]
        assert hit.quote == original.quote
        assert _leaf_of(hit) == _leaf_of(original)
        assert hit.score == original.score


# ---------------------------------------------------------------- 5 保底席位


def test_reserved_seat_holds_across_folding(parent_on):
    """扩展路票数更高，原始问题的父块仍然排第 1（席位在父块坐标上算）。

    复现的是仓库里那条实测退化：五路累加时「蛋白质折叠」的累加分 0.03252 高于
    「预算审批」的 0.03202，纯按分数排就会掉到第 2。保底席位把它按回第 1，而
    `score` 不单调正是"保底真的生效了"的证据（不是碰巧没退化）。
    """
    sources = [_source(_mixed_material())]
    question = "预算审批流程谁核准"
    extras = ["超导体的临界温度是多少", "蛋白质折叠的错误构象",
              "合同违约金上限", "星系旋转曲线的观测"]

    hits = run(search_multi(sources, [question, *extras], 3))

    assert len(hits) >= 2
    assert hits[0].quote.startswith("预算审批")
    assert hits[1].score > hits[0].score, "第 2 名分更高，说明保底席位在起作用"

    # top_k=1 时只保一席，扩展路完全插不进来
    assert run(search_multi(sources, [question, *extras], 1))[0].quote.startswith("预算审批")


def test_reserved_seat_yields_when_primary_route_hits_one_parent(parent_on):
    """原 query 只命中一个父块时，保底只占一席，其余名额照样给扩展路。"""
    sources = [_source(_mixed_material())]
    hits = run(search_multi(
        sources, ["预算审批流程谁核准", "超导体的临界温度是多少"], 3))

    assert len(hits) >= 2
    assert hits[0].quote.startswith("预算审批")
    assert any("超导体的临界温度" in hit.quote for hit in hits[1:])


# ---------------------------------------------------------------- 6 library 半席


def _library_material(tag: str) -> str:
    """库那批来源：正文仍然含「预算审批」，但每段插一个 tag 让指纹与本次那批不同。

    两个条件缺一不可。**不含查询词**时库那批的 BM25 全是 0、`_fuse` 返回空，
    `_merge_batches` 走 `if not extra: return base` 那条早退，于是这条用例看起来
    "过了"，其实库那条路一次都没跑到。**指纹相同**时 `_library_sources` 会把它们
    全排掉（`present` 按 `content_hash` 去重），同样一条都进不来。
    """
    segments = []
    for index in range(30):
        segments.append(
            f"预算审批第{index:02d}笔由部门负责人签核，{tag}归档编号{index:02d}。"
            "原始凭证于次月十日前归档，逾期由财务复核。")
    return "\n\n".join(segments)


def _ingest(text: str) -> str:
    return knowledge_library.ingest(_source(text))


def test_library_reserve_survives_folding(parent_on):
    """库那批也在子块级匹配，但**跨批融合发生在折叠之后**，保底半席照样成立。

    库放三份高度相关的材料（切出的父块更多、BM25 分更高），本次请求只有一份。
    纯 RRF 累加会把本次那批挤下去；`_merge_batches` 的保底段就是为了这个。
    """
    for tag in ("档案归集", "安全巡检", "客户回访"):
        _ingest(_library_material(tag))

    source = _source(_wide_material())
    top_k = 4
    base = run(search_multi([source], ["预算审批"], top_k))
    assert len(base) >= 2, "本次材料要够切出两个父块，否则保底席无从谈起"

    hits = run(search_multi([source], ["预算审批"], top_k, library=True))
    reserve = (top_k + 1) // 2

    assert [hit.source_id for hit in hits[:reserve]] == [source.id] * reserve
    assert [(hit.start, hit.end) for hit in hits[:reserve]] == \
           [(hit.start, hit.end) for hit in base[:reserve]]
    # 两批的粒度一致：库命中也带子块区间
    assert all(hit.leaf_start is not None for hit in hits)
    rest = hits[reserve:]
    assert rest, "库里有高度相关的材料，不该一条都补不进来"
    assert any(hit.source_id != source.id for hit in rest)


# ---------------------------------------------------------------- 7 hyde


def test_hyde_route_never_touches_bm25(parent_on, monkeypatch):
    """`hyde` 那一路只用向量：它一个字都不该进 BM25。

    与 `tests/test_search_multi.py` 的同名用例同一断言，只是粒度换成了子块——
    假答案是模型编的，字面与材料不沾，拿它当关键词查会让 BM25 在长原文里撞上
    一堆常用 bigram，白占名次。
    """
    seen: list[str] = []
    real = service._bm25_scores

    def counting(query: str, quotes: list[str]):
        seen.append(query)
        return real(query, quotes)

    monkeypatch.setattr(service, "_bm25_scores", counting)
    run(search_multi([_source(_material())], ["预算审批"], 3,
                     hyde="假设性答案：团队建设在季度末安排，费用另行列支。"))

    assert seen == ["预算审批"]


# ---------------------------------------------------------------- 8 开关关着


def test_single_route_matches_search_parent(parent_on):
    """子块模式下，单路 `search_multi` 与 `search_parent` **逐字段相同**。

    这是"B0 应当追上 A"那条对照的构造证据：两者在只有一路问法时走的是同一段
    代码（`_fuse` + `_fold_to_parents`），不是"照着它再实现一遍"。
    """
    sources = [_source(_material())]
    for question in ("预算审批", "设备维护按月保养", "固定资产盘点"):
        for top_k in (1, 3, 5):
            expected = _dump(search_parent(sources, question, top_k))
            actual = _dump(run(search_multi(sources, [question], top_k)))
            assert actual == expected, f"{question} / top_k={top_k}"


def test_switch_off_is_byte_identical_to_1342942(tmp_path, monkeypatch):
    """`MINDMAP_PARENT_RETRIEVAL=0`：多路与改动前**逐字节相同**（含 `score`）。

    粒度开关关着，多路不该单方面改变返回形状——`leaf_*` 必须恒为 None，去重仍走
    `_pick_ordered`，每路截断的 `pool` 与 RRF 名次映射一个数都不许变。这条用例
    同时说明"回退"是能真回退的，不是一句口头承诺。
    """
    baseline, raw = _load_baseline_module(tmp_path)
    import hashlib

    assert hashlib.sha256(raw).hexdigest() == BASELINE_SHA256, \
        f"{BASELINE_REV} 的 service.py 指纹变了"

    monkeypatch.setenv(PARENT_RETRIEVAL_ENV, "0")
    sources = [_source(_material()), _source(_mixed_material())]
    cases = (
        (["预算审批"], 3),
        (["预算审批", "费用报销"], 3),
        (["预算审批", "费用报销", "设备维护"], 1),
        (["预算审批", "超导体的临界温度是多少", "合同违约金上限"], 5),
    )
    for queries, top_k in cases:
        expected = _baseline_dump(
            asyncio.run(baseline.search_multi(sources, queries, top_k)))
        actual = _dump(run(search_multi(sources, queries, top_k)))
        assert actual == expected, (
            f"关着时多路变了：{queries} / {top_k}\n{_first_difference(expected, actual)}")


def test_switch_off_keeps_leaf_fields_empty(monkeypatch):
    """开关关着时 `leaf_*` 恒为 None：返回形状不该随开关悄悄多两个字段。"""
    monkeypatch.setenv(PARENT_RETRIEVAL_ENV, "0")
    hits = run(search_multi([_source(_material())], ["预算审批", "费用报销"], 3))
    assert hits
    assert all(hit.leaf_start is None and hit.leaf_end is None for hit in hits)
