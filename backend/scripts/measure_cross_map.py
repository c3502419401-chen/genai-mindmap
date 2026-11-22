"""跨图参照与向量索引：先量，再决定要不要装东西。

这一轮要回答的不是"能不能接 Milvus"，而是**接了有没有用**。四类候选各有多少，
是判断的前提：

  ① 语义冗余   同一张图内两个节点的 label 余弦 ≥ 0.85 的对数（按同级/跨分支分开）
  ② 矛盾       带引文的 `source_ref` 有多少条能判（`annotate_nli` 的输入量）
  ③ 缺失环节   同一父节点下"部分兄弟有子节点、部分是叶子"的处数
  ④ 现状       `check()` 各码条数、`suggest(model=off)` 条数

② 有一个必须说清的前提：`annotate_nli` 判的是"节点论断与**引用原文**是否相反"，
不是"图内两个节点互相矛盾"。而且它默认关（`MINDMAP_NLI_ENABLED` + 模型目录），
所以本机跑出来的 ② 永远是 0。这里报的是**可判样本量**，不是矛盾数。

用法：

    cd backend
    python scripts/measure_cross_map.py                 # ①③④ + ②的样本量
    python scripts/measure_cross_map.py --embed         # 连 ①，要 embedding 凭证
    python scripts/measure_cross_map.py --index         # 与 Milvus Lite 对比（可选依赖）
    python scripts/measure_cross_map.py --ab            # 加索引前后的建议条数

`--embed` 会把向量写进 `data/_measure_cross_map_vectors.json`（**不进仓库**），
第二次跑就是缓存命中。3856 个节点大约 8 分钟，56 个节点的一张图约 6 秒。
"""
from __future__ import annotations

import argparse
import collections
import itertools
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

CACHE = BACKEND / "data" / "_measure_cross_map_vectors.json"

# 画布/保存的回归基线。它们的节点标签是「第二条分支」「A 侧交付面」这类占位文本，
# 算进"语义冗余"会把结论全部染成噪声（实测：含基线的 3775 对里 2846 对是同名）。
# 这套数据是**测量口径**的一部分，不是生产代码里的知识——`cross_map.py` 不认识
# 这些标题，它只会按"同名图不自比"过滤。
FIXTURE_TITLES = frozenset({
    "树基线（改动前后逐像素对比用）", "报销审批流程", "六种形状基线", "鱼骨基线",
    "判定节点细节检验", "E2E 无阶段内边", "E2E 有阶段内边",
    "触发器拦住事件时保存的标题",
})

THRESHOLD = 0.85


def load_env(path: Path) -> None:
    """把 `run-backend.bat` 里的 `set` 行灌进环境（key 只活在本进程）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    import re
    for name, value in re.findall(r"(?im)^\s*set\s+([A-Z0-9_]+)=(.*)$", text):
        os.environ.setdefault(name.strip(), value.strip())


load_env(BACKEND.parent / "run-backend.bat")
os.environ.setdefault("MINDMAP_DB_PATH", str(BACKEND / "data" / "mindmaps.db"))

from app.agent import shapes  # noqa: E402
from app.contracts.mindmap import MindMapContent, migrate_content  # noqa: E402
from app.mindmaps import repository  # noqa: E402
from app.optimization.service import _near_reason, check, suggest  # noqa: E402


def load_maps() -> tuple[list[tuple[str, str, MindMapContent]], int]:
    """真图：库里保存过的树图 + `runs.candidate` 里生成出来但没保存的。

    返回 `(图列表, 被排除的基线数)`，元素是 `(来路, 标识, 内容)`。夹具不丢，
    只是不参与统计——"库里有几张是回归基线"本身是个要被看见的数字。
    """
    maps: list[tuple[str, str, MindMapContent]] = []
    fixtures = 0
    for record in repository.list_maps(tolerant=True):
        if not isinstance(record.content, MindMapContent):
            continue
        if record.content.title in FIXTURE_TITLES:
            fixtures += 1
            continue
        maps.append(("mindmaps", record.id, record.content))

    path = os.environ["MINDMAP_DB_PATH"]
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute("select run_id, candidate from runs "
                                 "where candidate is not null"))
        conn.close()
    except sqlite3.Error:
        rows = []
    for row in rows:
        raw = (row["candidate"] or "").strip()
        if not raw:
            continue
        try:
            data = migrate_content(json.loads(raw))
            if "root" not in data:
                continue
            content = MindMapContent.model_validate(data)
        except Exception:  # noqa: BLE001 - 坏行算"读不出来"，不参与统计
            continue
        if content.title in FIXTURE_TITLES:
            fixtures += 1
            continue
        maps.append(("runs", row["run_id"], content))
    return maps, fixtures


def report_baseline(maps: list[tuple[str, str, MindMapContent]], fixtures: int) -> dict:
    """②③④：全都不联网。"""
    detail_filled = detail_total = 0
    ref_nodes = quote_refs = 0
    mixed_parents = mixed_parents3 = 0
    codes: collections.Counter = collections.Counter()
    counts: list[int] = []
    sugg_now: list[int] = []
    t_check = t_sugg = 0.0
    for _, _, content in maps:
        nodes = list(shapes.iter_nodes(content))
        counts.append(len(nodes))
        detail_total += len(nodes)
        detail_filled += sum(1 for node in nodes if (node.detail or "").strip())
        for node in nodes:
            if node.source_refs:
                ref_nodes += 1
            for ref in node.source_refs:
                if (ref.quote or "").strip() or (ref.start is not None
                                                and ref.end is not None):
                    quote_refs += 1
        for node in nodes:
            kids = node.children
            if len(kids) < 2:
                continue
            grown = sum(1 for kid in kids if kid.children)
            if 0 < grown < len(kids):
                mixed_parents += 1
                if len(kids) >= 3:
                    mixed_parents3 += 1
        t0 = time.perf_counter()
        for diag in check(content):
            codes[diag.code] += 1
        t_check += time.perf_counter() - t0
        t0 = time.perf_counter()
        sugg_now.append(len(suggest(content, use_model=False, use_cross_map=False)))
        t_sugg += time.perf_counter() - t0

    counts.sort()
    print(f"真图 {len(maps)} 张（排除回归基线 {fixtures} 张），节点 {sum(counts)} 个，"
          f"每图中位 {counts[len(counts)//2]}，最大 {counts[-1]}")
    print(f"`detail` 覆盖 {detail_filled}/{detail_total} = "
          f"{detail_filled / detail_total:.1%}")
    print(f"② 可判样本：带 `source_refs` 的节点 {ref_nodes} 个，"
          f"其中带引文/区间 {quote_refs} 条 —— 本机 NLI 未启用，contradiction 恒为 0")
    print(f"③ 兄弟完备性差异 {mixed_parents} 处（≥3 兄弟的 {mixed_parents3} 处）")
    print(f"④ check() {dict(codes)}，{t_check * 1000:.0f} ms")
    print(f"④ suggest(model=off) {sum(sugg_now)} 条，"
          f"有建议的图 {sum(1 for c in sugg_now if c)}/{len(maps)}，"
          f"分布 {sorted(collections.Counter(sugg_now).items())}，{t_sugg * 1000:.0f} ms")
    return {"maps": len(maps), "fixtures": fixtures, "nodes": sum(counts),
            "detail": [detail_filled, detail_total], "ref_nodes": ref_nodes,
            "quote_refs": quote_refs, "mixed_parents": mixed_parents,
            "mixed_parents_ge3": mixed_parents3, "codes": dict(codes),
            "suggest": sum(sugg_now),
            "suggest_maps": sum(1 for c in sugg_now if c)}


def report_redundancy(maps: list[tuple[str, str, MindMapContent]]) -> dict:
    """① 语义冗余：label 余弦 ≥ `THRESHOLD` 的节点对，按"同级 / 跨分支"分开。

    **分开是这一节的全部意义**：`_near_reason` 只比同级标签，所以跨分支的语义
    重复是规则层的盲区。只报一个总数会把"规则层已经覆盖的"和"规则层看不见的"
    混在一起，那个数字就无法支撑任何决定。
    """
    from app.knowledge import embedding

    rows: list[tuple[int, str, str]] = []
    for index, (_, _, content) in enumerate(maps):
        for node in shapes.iter_nodes(content):
            rows.append((index, node.id, node.label.strip()))

    cache: dict[str, list[float]] = {}
    if CACHE.exists():
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    missing = [text for text in dict.fromkeys(row[2] for row in rows) if text not in cache]
    if missing:
        if not embedding.configured():
            print("① 跳过：没配 embedding 凭证（`MINDMAP_EMBEDDING_API_KEY`）")
            return {}
        print(f"① 取向量 {len(missing)} 条（约 {len(missing) * 0.11 / 10:.0f} 分钟）…")
        for start in range(0, len(missing), 50):
            window = missing[start:start + 50]
            for text, vector in zip(window, embedding.embed_texts(window)):
                if vector:
                    cache[text] = vector
            CACHE.write_text(json.dumps(cache), encoding="utf-8")
    else:
        print(f"① 向量全部命中缓存（{len(cache)} 条）")

    same_level: set[tuple[int, frozenset[str]]] = set()
    for index, (_, _, content) in enumerate(maps):
        for node in shapes.iter_nodes(content):
            for left, right in itertools.combinations(node.children, 2):
                same_level.add((index, frozenset((left.id, right.id))))

    by_map: dict[int, list[int]] = collections.defaultdict(list)
    for position, (index, _, _) in enumerate(rows):
        by_map[index].append(position)

    pairs = exact = hits = cross = same_new = old_rule = 0
    samples: list[tuple[float, str, str]] = []
    for index, positions in by_map.items():
        for left, right in itertools.combinations(positions, 2):
            pairs += 1
            label_a, label_b = rows[left][2], rows[right][2]
            if label_a == label_b:
                exact += 1
            score = sum(x * y for x, y in zip(cache[label_a], cache[label_b]))
            if score < THRESHOLD:
                continue
            hits += 1
            if (index, frozenset((rows[left][1], rows[right][1]))) in same_level:
                if _near_reason(label_a, label_b) is None:
                    same_new += 1
                else:
                    old_rule += 1
            else:
                cross += 1
                if label_a != label_b:
                    samples.append((score, label_a, label_b))
    samples.sort(reverse=True)
    print(f"① 图内节点对 {pairs}；label 完全相同 {exact}")
    print(f"   ≥{THRESHOLD} 共 {hits} 对：同级 {hits - cross}（规则层已报 {old_rule}，"
          f"规则层漏 {same_new}），**跨分支 {cross}**（规则层看不见）")
    for score, label_a, label_b in samples[:12]:
        print(f"     {score:.3f}  「{label_a}」 ↔ 「{label_b}」")
    return {"pairs": pairs, "exact": exact, "hits": hits, "cross": cross,
            "same_level_rule": old_rule, "same_level_new": same_new}


def report_index(maps: list[tuple[str, str, MindMapContent]]) -> dict:
    """Milvus Lite vs numpy 暴力扫。`pymilvus` 不在 requirements 里，装不上就跳过。"""
    import numpy as np

    if not CACHE.exists():
        print("--index 需要先跑一次 --embed（要有向量）")
        return {}
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    labels = list(cache)[:1000]
    matrix = np.asarray([cache[k] for k in labels], dtype=np.float32)

    t0 = time.perf_counter()
    for _ in range(10):
        sims = matrix[:10] @ matrix.T
        np.argpartition(-sims, 9, axis=1)[:, :10]
    brute = (time.perf_counter() - t0) / 10
    print(f"numpy 暴力扫 {len(labels)} 条 × top-10：{brute * 1000:.2f} ms/次")

    try:
        from pymilvus import DataType, MilvusClient
    except ImportError:
        print("Milvus Lite 未安装（pymilvus 会拖进 faiss/grpcio/pandas/pyarrow 等 19 个包，")
        print("约 75 MB，所以**没有**写进 requirements）。装了之后重跑这一段即可。")
        return {"brute_ms": round(brute * 1000, 2)}
    path = BACKEND / "data" / "_measure_milvus.db"
    if path.is_dir():
        import shutil
        shutil.rmtree(path, ignore_errors=True)
    client = MilvusClient(uri=str(path))
    schema = client.create_schema(auto_id=True, enable_dynamic_field=False)
    schema.add_field("id", DataType.INT64, is_primary=True)
    schema.add_field("label", DataType.VARCHAR, max_length=512)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=len(matrix[0]))
    params = client.prepare_index_params()
    params.add_index(field_name="vector", index_type="FLAT", metric_type="COSINE")
    client.create_collection("nodes", schema=schema, index_params=params)
    t0 = time.perf_counter()
    client.insert("nodes", [{"label": k[:512], "vector": cache[k]} for k in labels])
    insert = time.perf_counter() - t0
    t0 = time.perf_counter()
    for row in matrix[:10]:
        client.search("nodes", data=[row.tolist()], limit=10, output_fields=["label"])
    search = (time.perf_counter() - t0) / 10
    client.close()
    print(f"Milvus Lite 写入 {len(labels)} 条 {insert * 1000:.0f} ms，"
          f"top-10 {search * 1000:.1f} ms/次（慢 {search / brute:.1f} 倍）")
    return {"brute_ms": round(brute * 1000, 2), "insert_ms": round(insert * 1000, 1),
            "search_ms": round(search * 1000, 1), "ratio": round(search / brute, 1)}


def report_ab(maps: list[tuple[str, str, MindMapContent]]) -> dict:
    """A/B：只跑规则层 vs 加上跨图参照。模型层单独由 `--model` 控制（有成本）。"""
    from app.optimization import cross_map

    t0 = time.perf_counter()
    primed = cross_map.prime_history()
    warm = time.perf_counter() - t0
    print(f"预热历史向量 {primed}，{warm:.0f} s")

    before = after = 0
    cold = hot = 0.0
    rows = []
    for index, (origin, ident, content) in enumerate(maps):
        base = suggest(content, use_model=False, use_cross_map=False)
        t0 = time.perf_counter()
        extra = cross_map.suggestions(content, map_id=ident if origin == "mindmaps" else None,
                                      enabled=True)
        first = time.perf_counter() - t0
        t0 = time.perf_counter()
        cross_map.suggestions(content, map_id=ident if origin == "mindmaps" else None,
                              enabled=True)
        again = time.perf_counter() - t0
        before += len(base)
        after += len(base) + len(extra)
        cold += first
        hot += again
        if extra:
            rows.append((content.title, len(base), len(extra), first * 1000))
    print(f"A 规则层 {before} 条；B 规则层+跨图 {after} 条（+{after - before}）")
    print(f"延迟：首次 {cold / len(maps) * 1000:.0f} ms/图（含补向量），"
          f"缓存热 {hot / len(maps) * 1000:.1f} ms/图")
    print(f"出建议的图 {len(rows)}/{len(maps)}")
    for title, base, extra, ms in rows[:10]:
        print(f"   [{title[:20]}] 规则层 {base} → +{extra} 条（{ms:.0f} ms）")
    return {"before": before, "after": after,
            "cold_ms": round(cold / len(maps) * 1000, 1),
            "hot_ms": round(hot / len(maps) * 1000, 2), "maps_with_hits": len(rows)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embed", action="store_true", help="跑 ①（要向量凭证）")
    parser.add_argument("--index", action="store_true", help="Milvus Lite 对比（可选依赖）")
    parser.add_argument("--ab", action="store_true", help="加跨图参照前后的建议条数")
    args = parser.parse_args()

    maps, fixtures = load_maps()
    if not (args.embed or args.index or args.ab):
        report_baseline(maps, fixtures)
        return 0
    if args.embed:
        report_redundancy(maps)
    if args.index:
        report_index(maps)
    if args.ab:
        report_ab(maps)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

