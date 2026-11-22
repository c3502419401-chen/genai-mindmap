<#
.SYNOPSIS
    确认本机 Neo4j 活着，并顺带回答"图谱这条路对生成到底有没有生效"。

.DESCRIPTION
    两件事分开报，因为它们要的处置完全不同：

      1. **服务在不在**：7687 有没有监听、能不能用 MindmapGraph2026 连上、
         库里这张图有多少实体 / 边。服务不在就是 `run-neo4j.bat` 没跑。
      2. **数据够不够**：那两份 openalex 摘要（`filename like '%碳中和%'`）在图上
         有没有带连接的实体。库活着但材料没入图时，`_graph_outline` 仍然静默退回
         "模型自行归纳"——那时该跑的是生成（带 MINDMAP_GRAPH_INGEST=1），不是重启。

    只读，不写库、不改配置。

.EXAMPLE
    pwsh -File D:\genai-mindmap\check-neo4j.ps1
#>
$ErrorActionPreference = 'Stop'
# 中文输出要能落地：默认控制台代码页是 936，Python 打出来的 UTF-8 会变成问号，
# 而这份脚本的输出全是给人看的中文。两处都要设（Python 侧与控制台侧）。
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $repo 'backend'

function Write-Head($text) { Write-Host ""; Write-Host "== $text ==" -ForegroundColor Cyan }

Write-Head '端口'
$listening = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in 7687, 7474 }
if ($listening) {
    $listening | ForEach-Object { Write-Host ("  {0}  LISTEN  pid={1}" -f $_.LocalPort, $_.OwningProcess) }
} else {
    Write-Host '  7687 / 7474 都没有监听 —— Neo4j 没起。先跑 run-neo4j.bat。' -ForegroundColor Yellow
}

Write-Head '连接与计数'
# 走仓库自己的驱动与配置（URI / 用户 / 密码都从环境变量读，与后端同一口径）：
# 在这里另写一套连接代码就等于有第二个真相，查得出的结论也就不可信了。
$env:PYTHONPATH = $backend
$probe = @'
import os
from pathlib import Path
from app.knowledge import graph_store

if not graph_store.configured():
    print("未配置 MINDMAP_NEO4J_PASSWORD：图谱这条路整条不走（这是部署选择，不是故障）")
    raise SystemExit(0)
if not graph_store.available():
    print(f"熔断冷却中：约 {graph_store.cooldown_remaining():.0f} 秒后才会再试")
    raise SystemExit(0)
try:
    stats = graph_store.stats()
except Exception as exc:            # noqa: BLE001 — 体检脚本，报出来比栈更清楚
    print(f"连不上：{type(exc).__name__}: {exc}")
    raise SystemExit(1)
print(f"实体 {stats['entities']} / 边 {stats['edges']} / 别名 {stats.get('aliases', 0)}")
import sqlite3
db = os.environ.get("MINDMAP_DB_PATH") or str(Path("data") / "mindmaps.db")
conn = sqlite3.connect(db)
rows = conn.execute(
    "SELECT id, filename FROM library_sources WHERE filename LIKE '%碳中和%'").fetchall()
if not rows:
    print("库里没有 filename like '%碳中和%' 的材料（薄批用这两份）")
for source_id, filename in rows:
    scoped = graph_store.scoped_entity_count([source_id])
    print(f"  {filename[:40]} -> 带连接实体 {scoped}")
'@
$probe | python -

Write-Head '结论怎么看'
Write-Host '  实体 0 且端口在听 → 库是空的：跑一次生成（GRAPH_INGEST=1）让它入图'
Write-Host '  端口不听        → 跑 run-neo4j.bat（它不会自启）'
Write-Host '  冷却中          → 刚失败过，等它退避结束（30s 起，上限 5 分钟）'
