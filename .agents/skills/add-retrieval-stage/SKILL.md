---
name: add-retrieval-stage
description: 往检索链加阶段或改检索行为时使用——加一路召回、加一次过滤、改去重/融合策略、改保底席位、加检索开关。当任务涉及 knowledge/service.py 的 search_parent / search_multi、MINDMAP_* 检索开关、或"检索结果变少/重复/名次奇怪"时，先读这个。
---

# 往检索链加一个阶段

检索链的失败方式和别处不一样：**它不抛错，只是少给一条**。所以本 skill 的重点不是"怎么加"，是"加完怎么证明关掉时等于没加"。

## 1. 接入点：`backend/app/knowledge/service.py`

三个入口，先确认你的阶段属于哪条路：

| 函数 | 行 | 什么时候走到 |
| --- | --- | --- |
| `search_parent` | 2095 | 单条问法 + 父块模式（`_rag` 的主路） |
| `search_multi` | 2423 | 多条问法（`MINDMAP_QUERY_EXPANSION` 打开后 `_rag` 走这条） |
| `_search_multi_core` | 2596 | `search_multi` 在**给定来源集合内**的全部逻辑，不碰库、不做跨批融合 |

底层的三个 core 也各有分工，别把逻辑错放进它们：

- `_search_sources_core`（1709）— 单级块
- `_search_parent_core`（2049）— 子块匹配、父块召回
- `_fold_to_parents`（1865）/ `_fold_multi_hits`（1912）/ `_merge_batches`（1626）— 三个折叠点

### 多路那条路的粒度跟着开关走

`search_multi` 在 `:2575` 读开关，把 `parent` 传给下游，**不要在下游再判一次**：

```python
    parent = _parent_retrieval_enabled()
    depth = _rerank_depth(top_k)
    fused, reserve = await _search_multi_core(sources, queries, top_k, filters, hyde,
                                             depth=depth, parent=parent)
```

`:2571` 的注释写清了为什么：两处各判一次会出现"本次那批折父块、库里那批没折"的粒度错位，而那种错只有调用方按 `end - start` 估信息密度时才会暴露。

## 2. 开关：常量 + 真值集 + `_x_enabled()`

照 `_parent_retrieval_enabled`（`:1133`）与 `_graph_search_enabled`（`:1150`）的三段式，**现有三个开关都不是裸的 `os.getenv`**：

```python
PARENT_RETRIEVAL_ENV = "MINDMAP_PARENT_RETRIEVAL"
_PARENT_RETRIEVAL_TRUTHY = {"1", "on", "true", "yes"}

def _parent_retrieval_enabled() -> bool:
    return os.getenv(PARENT_RETRIEVAL_ENV, "1").strip().lower() in _PARENT_RETRIEVAL_TRUTHY
```

正列举真值的理由写在 `:1126`：改默认值时判定逻辑不用跟着改，改错的表现也只剩"默认值写错了"一种。

**默认值的理由必须写在常量上方的 docstring 里，且要带实测依据。** 对照 `_graph_search_enabled` 上方 `:1137`——它默认 `0`，理由是"图值不值得进检索要由数字回答，不由设计回答"，并引了 `graph_resolve` 的实测（合 7 / 漏 5 / 错 1）。写不出实测依据的默认值，就是没量过。

## 3. 降级路径

三种失败都必须退化成**原行为**，不能让整次检索失败：

- **没配置**（无 key）→ 照 `_rerank_depth`（`:1997`）：`enabled() and configured()` 两个条件缺一不可
- **超时 / 抛错** → 吞掉异常、记 warning、走原路
- **返回空** → 返回原候选，不是返回空列表

反例在 `_vector_scores`（`:1236`）：失败时返回 `({}, "no_signal")`——空分数 + 原因码，调用方据此退回单路。

**但降级要留下痕迹**（AGENTS.md 里的约定）：无 key 退纯 BM25、检索空就留空、向量批失败——每一项单看都对，合起来就是"坏了和本来就没有长得一样"。所以降级处要给出可分辨的信号：返回值里带原因码、`logger.warning` 里写清是哪一种降级。

## 4. 关掉时必须与上一个提交逐字节一致

这是本 skill 的核心要求，也是**唯一能挡住"改一半"的机械检查**。

不是"测试还绿"——测试可能只覆盖一半。做法是拿旧模块跑同一批输入做对比，照 `backend/tests/test_search_multi.py:163` 的 `_load_baseline_module`：

```python
BASELINE_REV = "41dda8e"

def _load_baseline_module(tmp_path):
    """把 `BASELINE_REV` 的 `service.py` 载成一个独立模块。"""
    source = subprocess.run(
        ["git", "show", f"{BASELINE_REV}:backend/app/knowledge/service.py"],
        cwd=REPO_ROOT, capture_output=True, check=True, text=True, encoding="utf-8",
    ).stdout
    path = tmp_path / "service_baseline.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"service_baseline_{BASELINE_REV}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module      # pydantic 在 exec 期间要能从 sys.modules 找到它
    spec.loader.exec_module(module)
    return module
```

现成用例：`test_search_sources_is_byte_identical_to_baseline`（`:200`）。同一招在 `test_source_mode.py:295` 用过（`LEGACY_COMMIT = "901b484"`）。

两条纪律：

- **基准不取 HEAD**：HEAD 会随提交漂移，那样只能证明"我和我自己一样"。提交号写死在常量里。
- **对比用 `model_dump()`**，逐字段比，不只比条数。

你的新阶段如果**开关打开时改行为**，那么关掉时必须与提交号那个版本逐字节一致；打开时的差异要由新用例正面钉住。

## 5. 去重与融合：先确认坐标系

**动手之前先回答一个问题：你的命中是块坐标还是父块坐标？**

| 坐标系 | 用哪个 | 为什么 |
| --- | --- | --- |
| 块坐标（`_collect_chunks`） | `_pick_ordered`（1322）→ 内部用 `_near_duplicate`（1307） | 两条命中重叠 = 切块器的 `CHUNK_OVERLAP_CHARS` 重复正文，是同一份证据的两个副本，该筛 |
| 父块坐标（`_collect_leaves` → `_fold_*`） | `_dedupe_exact`（1342） | 重叠来自 `_overlap_start` 把起点吸附到句首，与"重复证据"无关 |

父块短于 `2×CHUNK_OVERLAP_CHARS`（200 字）时，与相邻父块的必然重叠会超过短的一半。套重叠率判据会把**两个不同父块**判成重复、静默丢掉一条。

这条坑的真实代价记在 `test_short_parents_are_not_deduped`（`backend/tests/test_parent_retrieval.py:551`）：早年它只测 `library=False`，那条路根本不碰重叠率判据，所以一直绿；库分支（`library=True`）多了一次 `_merge_batches` 跨批融合，那里在父块坐标上又跑了一遍重叠率筛，第二个父块就是在这里被吞掉的。现在用 `@pytest.mark.parametrize("library_mode", [False, True])` 两边都钉。

**加阶段时要一起检查的四个折叠点**：`_fold_to_parents`、`_fold_multi_hits`、`_merge_batches`、`_fuse`（1447）。任何一个在错误的坐标系上做重叠率筛，都会静默少给一条。

## 6. 保底席位：新候选来源要不要改它

`_reserve_seats`（1372）的公式 `ceil(pool_size/2)`、至少 1 席，被三处共用：分支 `_merge_batches`、融合 `_fold_multi_hits` / `_search_multi_core`、交付 `_rerank_top`。

**口径必须跨过 rerank**：`reserve` 按交付口径（`top_k`）算，不是候选池口径（`depth`）。原因在 `_rerank_top`（`:2011`）的 docstring——`_rerank_depth` 把池子放宽到 `pool_size(top_k)`，上游按**池子**算的保底段看着还在池子前排，可重排一过全局按 `rerank_score` 排序，保底信息完全不带过去，`top_k=5` 时扩展路的五条可以全占，原问题的答案直接消失。

现成的兑现处（`search_multi` `:2585`）：

```python
    seats = min(reserve, _reserve_seats(top_k))
    ...
    return _rerank_top(question, fused, top_k, reserve=seats)
```

你引入了新的候选来源，就要回答：它会不会挤掉保底段？挤掉了 `_rerank_top(reserve=...)` 还成立吗？`_search_sources_core` / `_search_parent_core` / `_search_multi_core` 的返回值里那个"保底段条数"要不要跟着改？

## 7. 评估：跑一次，把数字写回文档

在 `backend/` 下：

```bash
python scripts/evaluate_retrieval.py --suite manual
```

其它口径：`--suite title`（标题当 query）、`--suite crossdoc`（跨文档，要 Neo4j）、`--suite both`（默认，两套并列）。

把变动的数字写进 `docs/RETRIEVAL-EVAL.md`。**不要只写新数字**——表里既有 `parent=0/1` × `library=0/1` × `hybrid=0/1` 的配置矩阵，新阶段要能和它们并列比较，就得补进同一张表。

一次实测零收益也要写，照 `docs/RAG-DECISIONS.md` 第 10 节「分支查询扩展：实测零收益」——负结果同样是决策依据。

## 8. 文档：`docs/RAG-DECISIONS.md`

加一节，**接着现有编号**（当前到第 11 节）。写清：为什么加、默认值是多少、实测数字、什么情况下会退回原行为。

### 先看这是不是评估语料

`docs/RAG-DECISIONS.md` 本身在 `MANUAL_SOURCE_HASHES` 里（`backend/scripts/eval_cases_manual.py:101`）。四份评估语料是：

```
docs/AGENT_PLAN.md  docs/RAG-DECISIONS.md  docs/PLAN.md  docs/ARCHITECTURE.md
```

改它们**被 case 标注过的章节正文**会让 28 条手工标注失效——指纹只覆盖 `sections` / `targets` 指向的那几章（`evaluate_retrieval.annotated_hashes`），追加变更记录、改别的小节不会红。改到标注章节时 `load_manual_dataset` 会**当场报错**而不是静默跳过（`test_hash_drift_is_fatal_not_skipped` 钉着这一点）。改法是重读那一章、复核区间与问法，再把新指纹写回 `eval_cases_manual.py`。

**这是本仓库最容易被无意触发的一条**：写检索文档时顺手改了 `RAG-DECISIONS.md` 已被标注的章节正文，评估套件就红了（只改别的章节不再触发）。

## 自检清单

- [ ] 接入点是 `search_parent` / `search_multi` / `_search_multi_core` 中正确的那一个，不是硬塞进三个都走
- [ ] 多路那条路的粒度跟着 `_parent_retrieval_enabled()` 走，没有在下游再判一次
- [ ] 开关是"常量 + `_x_TRUTHY` + `_x_enabled()`"三段式
- [ ] 默认值上方的 docstring 写了理由，**且带实测依据**
- [ ] 无配置 / 超时 / 抛错 / 返回空四种降级都退化成原行为，且各自留下可分辨的痕迹（原因码或 warning）
- [ ] 写了 `git show <BASELINE_REV>:path` 载旧模块的对比用例，开关关掉时逐字段（`model_dump()`）一致
- [ ] 基准提交号写死在常量里，不是 HEAD
- [ ] 明确回答过"我的命中是块坐标还是父块坐标"，选对了 `_pick_ordered` / `_dedupe_exact`
- [ ] 检查过四个折叠点：`_fold_to_parents` / `_fold_multi_hits` / `_merge_batches` / `_fuse`
- [ ] 新候选来源不会挤掉保底段，或已相应改 `_reserve_seats` 与 `_rerank_top(reserve=...)`
- [ ] 跑过 `python scripts/evaluate_retrieval.py --suite manual`，数字写进了 `docs/RETRIEVAL-EVAL.md`
- [ ] `docs/RAG-DECISIONS.md` 加了节（接现有编号），且**没碰四份评估语料的正文**
- [ ] `python -m pytest -o addopts="" -q` 全绿
