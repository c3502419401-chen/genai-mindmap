"""知识库落库与"只靠库检索"的实测（本机可复现，不需要任何密钥）。

回答三个问题，每个都给出可以直接引用的数字：

  1. **落库代价**：一份真实材料进库要多久、库里多出多少字节、切块台账几行。
  2. **库的核心价值**：把请求内的 `sources` 清空、只留 `library=True`，还能不能
     召回。这一条是"材料跨请求复用"唯一不可替代的证据——`library=False` 在同样
     输入下必须返回空，否则测的就不是库。
  3. **100 份的账**：按实测单份大小外推，给出库文件体积与 2C2G 上的判断。

用法（在 `backend/` 下）：

    python scripts/measure_library.py
    python scripts/measure_library.py --docx "C:/path/to/报告.docx"
    python scripts/measure_library.py --keep      # 保留临时库文件以便自己去看

默认用 `docs/AGENT_PLAN.md`、`docs/RAG-DECISIONS.md` 与桌面那份实验报告 docx
（`--docx` 可以改路径；找不到就只用两份 md）。库落在临时目录，**不碰**
`backend/data/mindmaps.db`。

口径说明：

  - 入库耗时只算 `library.ingest`（切块 + 写库），**不含解析**。解析里 Docling
    首次要加载模型（本机实测 55 秒），混进入库耗时会把这张表变成"解析器有多慢"。
  - "库文件增长"是 `-journal` 之外的主库文件字节差，含 SQLite 页开销。正文是
    UTF-8 存储，中文约 3 字节/字，所以这个数一定大于字符数的 3 倍。
  - 检索耗时用 `search_parent(..., library=True)` 冷启动一次、再取 3 次中位数。
    `library=True` 每次都要从库里读最近 100 份正文做粗筛，这是它的固定成本。
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# 库落临时目录。必须在第一次 `repository.connect()` 之前设好：`db_path()` 每次都
# 读环境变量，但连接一旦建好就不会再改路径。
_WORK = Path(tempfile.mkdtemp(prefix="library-measure-"))
os.environ["MINDMAP_DB_PATH"] = str(_WORK / "library.db")

from app.contracts.source import Source, SourceKind  # noqa: E402
from app.ingestion.service import parse_bytes  # noqa: E402
from app.knowledge import library  # noqa: E402
from app.knowledge.service import search_parent, search_sources  # noqa: E402

DEFAULT_DOCX = Path.home() / "Desktop" / "螺线管磁场测量实验报告_填写内容.docx"

# (说明, 路径, 来源种类)。md 走文本直通，docx 走 Docling。
MD_MATERIALS = [
    ("docs/AGENT_PLAN.md", REPO / "docs" / "AGENT_PLAN.md"),
    ("docs/RAG-DECISIONS.md", REPO / "docs" / "RAG-DECISIONS.md"),
]

# 每个 query 贴着材料自己的用词，但**都不是标题原文**——title 是"一句话结论"，
# query 用"两路名次怎么融合"这种问法，才测得到检索而不是测字符串相等。
QUERIES = [
    "工具调用的轮数上限是多少",
    "两路名次怎么融合",
    "霍尔元件的输出电压怎么算",
]


def _db_bytes() -> int:
    return (Path(os.environ["MINDMAP_DB_PATH"])).stat().st_size


def _scale_probe(materials: list[tuple[str, object]], count: int = 100) -> None:
    """100 份的成本实测：体积、入库耗时、`library=True` 的检索耗时。

    **只测成本，不测召回质量**：这份副本是按模板轮换生成的（正文几乎相同，只多
    一行唯一注释让 `content_hash` 不同），所以它们互相竞争同一个查询——那是
    "重复内容"的形态，不是真实语料。要说的是：正文量放大到 100 份时，落库要多久、
    库文件多大、一次 `library=True` 检索要读多少字节与多少毫秒。

    这一段的库是**清空重建**的（前面的 3 份测量已经完成），所以行数与体积都从
    建表基线重新计。
    """
    library.prune(keep_days=0, keep_count=0)
    baseline = _db_bytes()

    started = time.perf_counter()
    for index in range(count):
        _name, template = materials[index % len(materials)]
        text = f"<!-- 副本 {index:03d} -->\n" + (template.text or "")
        library.ingest(Source(kind=template.kind, filename=f"副本{index:03d}.md",
                              status="parsed", text=text))
    ingest_seconds = time.perf_counter() - started
    grown = _db_bytes() - baseline
    rows = library.list_sources(limit=count)
    chars = sum(entry["chars"] for entry in rows)
    chunks = sum(entry["chunks"] for entry in rows)

    print(f"\n{'=' * 68}")
    print(f"{count} 份的成本实测（副本轮换生成，只测成本）")
    print("=" * 68)
    print(f"正文合计：{chars / 1000:.0f} 千字符 / 切块 {chunks} 行")
    print(f"库文件：{baseline} B → {_db_bytes()} B"
          f"（+{grown / 1024 / 1024:.2f} MB，{grown / max(chars, 1):.2f} B/字符）")
    print(f"入库总耗时：{ingest_seconds:.1f} 秒（{count / ingest_seconds:.1f} 份/秒）")

    for query in ("工具调用的轮数上限是多少", "两路名次怎么融合"):
        timings = []
        for _ in range(3):
            started = time.perf_counter()
            hits = search_sources([], query, 5, library=True)
            timings.append((time.perf_counter() - started) * 1000)
        print(f"检索 library=True「{query}」：中位 {statistics.median(timings):.0f} ms"
              f" / {len(hits)} 条（每次读最近 100 份 ≈ {chars / 1000 / count * 100:.0f}"
              f" 千字符）")

    started = time.perf_counter()
    library.list_sources(limit=50)
    print(f"list_sources(50)：{(time.perf_counter() - started) * 1000:.1f} ms")
    started = time.perf_counter()
    removed = library.prune(keep_days=90, keep_count=200)
    print(f"prune(keep_days=90, keep_count=200)：删 {removed} 份，"
          f"{(time.perf_counter() - started) * 1000:.1f} ms")


def _material_rows(docx: Path | None) -> list[tuple[str, Path, SourceKind]]:
    rows = [(name, path, SourceKind.markdown) for name, path in MD_MATERIALS]
    if docx is not None and docx.exists():
        rows.append((f"desktop/{docx.name}", docx, SourceKind.document))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docx", type=Path, default=DEFAULT_DOCX)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--hundred", action="store_true",
                        help="额外跑一段 100 份的成本实测（会清空库并重建）")
    args = parser.parse_args()

    rows = _material_rows(args.docx)
    print(f"库文件：{os.environ['MINDMAP_DB_PATH']}")
    print(f"材料：{len(rows)} 份\n")

    # 建表之后的基线（三张 CREATE TABLE IF NOT EXISTS 的页开销也算进"库有多大"，
    # 但不算进"材料带来了多少"。）
    library.list_sources()
    baseline_bytes = _db_bytes()

    print(f"{'材料':34}{'字符':>8}{'块数':>7}{'入库ms':>9}{'正文KB':>9}")
    print("-" * 68)
    total_chars = total_chunks = 0
    ingest_ms: list[float] = []
    parsed: list[tuple[str, object]] = []
    for name, path, kind in rows:
        started = time.perf_counter()
        source = parse_bytes(kind, path.name, path.read_bytes())
        parse_seconds = time.perf_counter() - started
        parsed.append((name, source))
        text = source.text or ""
        total_chars += len(text)

        started = time.perf_counter()
        source_id = library.ingest(source)
        elapsed = (time.perf_counter() - started) * 1000
        ingest_ms.append(elapsed)

        chunk_rows = len(library.chunks(source_id))
        total_chunks += chunk_rows
        print(f"{name:34}{len(text):>8}{chunk_rows:>7}{elapsed:>9.1f}"
              f"{len(text.encode('utf-8')) / 1024:>9.0f}"
              f"   (解析 {parse_seconds:.1f}s)")

    grown = _db_bytes() - baseline_bytes
    print("-" * 68)
    print(f"{'合计':34}{total_chars:>8}{total_chunks:>7}"
          f"{sum(ingest_ms):>9.1f}{total_chars * 3 / 1024:>9.0f}")

    sources = library.list_sources()
    print(f"\n库文件：建表后 {baseline_bytes} B → 入库后 {_db_bytes()} B"
          f"（+{grown} B，{grown / max(total_chars, 1):.2f} B/字符）")
    print(f"表内行数：library_sources={len(sources)}  library_chunks={total_chunks}")
    print(f"入库耗时：中位 {statistics.median(ingest_ms):.1f} ms/份"
          f"（{total_chars / max(sum(ingest_ms) / 1000, 1e-6) / 1000:.0f} 千字符/秒）")

    # ---------------------------------------------------------------- 只靠库召回
    print("\n" + "=" * 68)
    print("只靠库检索：请求内 sources = []（也就是「关掉浏览器再回来」的形态）")
    print("=" * 68)
    hit_any = 0
    for query in QUERIES:
        off = search_parent([], query, 3)
        started = time.perf_counter()
        on = search_parent([], query, 3, library=True)
        elapsed = (time.perf_counter() - started) * 1000
        hit_any += bool(on)
        print(f"\nquery：{query}")
        print(f"  library=False → {len(off)} 条")
        print(f"  library=True  → {len(on)} 条（{elapsed:.0f} ms）")
        for hit in on:
            loaded = library.load(hit.source_id)
            name = (loaded.filename if loaded else hit.source_id)[:22]
            head = hit.quote.replace("\n", " ")[:46]
            print(f"    · {name:22} [{hit.start:>5},{hit.end:>5}) {hit.score:.4f}  {head}…")

    # ---------------------------------------------------------------- 混合检索的固定成本
    print("\n" + "=" * 68)
    print("library=True 的固定成本：每次都要从库里取最近 100 份做粗筛")
    print("=" * 68)
    timings = []
    for _ in range(3):
        started = time.perf_counter()
        search_sources([], "预算审批流程", 5, library=True)
        timings.append((time.perf_counter() - started) * 1000)
    print(f"search_sources(library=True) 中位 {statistics.median(timings):.1f} ms"
          f"（库内 {len(sources)} 份 / {total_chars} 字符）")
    print(f"粗筛读入：最近 100 份的来源正文，本次实际 {len(sources)} 份")

    # ---------------------------------------------------------------- 100 份外推
    print("\n" + "=" * 68)
    print("100 份的外推（按本次实测的单份均值）")
    print("=" * 68)
    per_source = {
        "chars": total_chars / len(rows),
        "bytes": grown / len(rows),
        "chunks": total_chunks / len(rows),
        "ms": sum(ingest_ms) / len(rows),
    }
    print(f"单份均值：{per_source['chars']:.0f} 字符 / {per_source['bytes']:.0f} B"
          f" / {per_source['chunks']:.0f} 行 / {per_source['ms']:.1f} ms")
    print(f"100 份：正文 {per_source['chars'] * 100 / 1000:.0f} 千字符，"
          f"库文件 {per_source['bytes'] * 100 / 1024 / 1024:.2f} MB，"
          f"切块 {per_source['chunks'] * 100:.0f} 行，"
          f"入库 {per_source['ms'] * 100 / 1000:.1f} 秒")
    # prune 的默认上限是 200 条：库真正会停在那儿，不是"无限增长"。
    print(f"200 条上限（prune 默认 keep_count）：库文件 "
          f"{per_source['bytes'] * 200 / 1024 / 1024:.2f} MB，"
          f"切块 {per_source['chunks'] * 200:.0f} 行")
    print(f"检索时读放大：最近 100 份 ≈ {per_source['chars'] * 100 / 1000:.0f} 千字符"
          f"（约 {per_source['bytes'] * 100 / 1024 / 1024:.2f} MB 的 UTF-8 正文）")

    if args.hundred:
        _scale_probe(parsed)

    if not args.keep:
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = Path(str(os.environ["MINDMAP_DB_PATH"]) + suffix)
            if candidate.exists():
                candidate.unlink()
        try:
            _WORK.rmdir()
        except OSError:
            pass
    else:
        print(f"\n库文件保留在：{os.environ['MINDMAP_DB_PATH']}")

    return 0 if hit_any else 1


if __name__ == "__main__":
    raise SystemExit(main())
