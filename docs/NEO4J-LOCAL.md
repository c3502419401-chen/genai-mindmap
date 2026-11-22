# 这台机器上的 Neo4j：怎么起、怎么确认活着

> 一句话：**它不会自启。** `run-backend.bat` 里已经 `call ensure-neo4j.bat`，
> 所以"起后端"这件事现在等于"把图也拉起来"；除此之外只有一个入口
> `run-neo4j.bat`，一次确认用 `check-neo4j.ps1`。机器重启之后不需要做别的事，
> 只要照旧跑 `run-backend.bat`。

## 1. 为什么不是 `neo4j.bat start`

那一条走的是 **Windows 服务**入口：

```
> D:\neo4j\neo4j-community-5.26.0\bin\neo4j.bat start
The service 'Neo4j' is not installed
```

服务没装，而装它要管理员权限（`neo4j.bat install-service` 会写
`HKLM\SYSTEM\CurrentControlSet\Services`）。本机的日常 shell 不是管理员，
`schtasks /create /sc onlogon` 同样被拒（`ERROR: Access is denied`）——
所以**"登录时自动起"这条路在当前权限下走不通**，只能靠脚本收口。

## 2. 三个脚本，一件事

| 脚本 | 干什么 | 什么时候跑它 |
|---|---|---|
| `run-neo4j.bat` | 起一个 console 模式的 Neo4j，输出追加到 `.visioncheck\neo4j.log` | 机器重启后想让后端用到图；或 `check-neo4j.ps1` 说 7687 没人听 |
| `ensure-neo4j.bat` | 幂等：7687 已监听则立刻返回，否则 detached 起一个并等它监听（最多 40 秒） | 由 `run-backend.bat` 自动 call，人不用管 |
| `check-neo4j.ps1` | 只读体检：端口 / 连接 / 实体边数 / 那两份薄材料在图里有多少带边实体 | 怀疑图没生效时 |

`run-neo4j.bat` 里写死了两件事，都是为了"少一个会忘的前提"：

```
JAVA_HOME   = C:\tools\jdk-21.0.12.1+1     （Neo4j 5.26 要 JDK 17/21）
NEO4J_HOME  = D:\neo4j\neo4j-community-5.26.0
```

`JAVA_HOME` 必须写死：agent 的 shell 是**进程级**作用域，不继承用户级环境变量，
每次手工起都要记得先 set 一遍，漏一次就是 `JAVA_HOME is not set`。

## 2.5 `run-backend.bat` 不入库：要手工补两行

`run-backend.bat` 被 `.gitignore` 挡着（它含 DeepSeek key），所以仓库里没有它——
**"起后端就顺带起图"这件事，在别人的 clone 上不存在**。在自己机器上要让它成立，
那个文件里需要这两段（`chore: run-neo4j.bat ...` 那一条提交的说明里写了理由）：

```bat
set MINDMAP_GRAPH_EXTRACTION=1
...
call "D:\genai-mindmap\ensure-neo4j.bat"
```

第一行是入图的另半边开关（只切块不抽实体时，库里一个节点都不会多）；第二行让
`run-backend.bat` 在起 uvicorn 之前确保 7687 有人听。漏掉第二行也不会静默：
图谱那几档会在诊断里说"连不上"，只是要你自己记得去起。

## 3. "进程随会话回收"这件事怎么避开的

已经发生过两次：agent 用后台 job 起的进程，会话一结束就被回收（Neo4j 与后端
都死过）。原因是 job object 的 kill-on-close 会把整个进程树带走。

所以起长期进程**不要用 job**。两种在这台机器上验证过的起法：

```powershell
# ① 从 shell 起，进程脱离命令的生命周期（Start-Process 建的是独立进程）
Start-Process -FilePath "cmd.exe" -ArgumentList '/c','D:\genai-mindmap\run-neo4j.bat' -WindowStyle Hidden

# ② 用 WMI 起：父进程是 WmiPrvSE/cmd，不在任何 shell 的 job 里（更彻底）
Invoke-CimMethod -ClassName Win32_Process -MethodName Create `
  -Arguments @{ CommandLine = 'cmd.exe /c "D:\genai-mindmap\run-neo4j.bat"' }
```

人手动用时双击 `run-neo4j.bat` 即可（新控制台，天然不在 job 里）。

## 4. 连接参数

放在 `run-backend.bat` 里（后端与体检脚本读同一组环境变量，不另写一套）：

```
MINDMAP_NEO4J_URI=bolt://127.0.0.1:7687
MINDMAP_NEO4J_USER=neo4j
MINDMAP_NEO4J_PASSWORD=MindmapGraph2026
```

密码**没有代码默认值**（忘了配就是"不用图"，不是"连到别处去"）。
`MINDMAP_GRAPH_EXTRACTION=1` 也必须显式给：入图那一步只切块、不抽实体时，
库里一个节点都不会多，而后端只会留下一句"N 块一条实体都没抽到"。

## 5. 怎么确认它活着

```
powershell -NoProfile -ExecutionPolicy Bypass -File D:\genai-mindmap\check-neo4j.ps1
```

三段输出，各自对应一种处置：

```
== 端口 ==            7687 / 7474 有没有监听        → 没有：跑 run-neo4j.bat
== 连接与计数 ==      实体 / 边，以及那两份薄材料     → 连不上：看它打出的异常类名
                      在图里各有几个带连接实体
== 结论怎么看 ==      三行对照
```

**"实体 0 且端口在听"不是故障**：库是空的，跑一次生成（`MINDMAP_GRAPH_INGEST=1`）
让它入图。薄批那两份（`filename like '%碳中和%'`）实测一次生成后是 10 与 7 个带连接
实体（阈值 `GRAPH_OUTLINE_MIN_ENTITIES = 8`，判据看**并集** 17）。

## 6. 图停掉之后会看到什么（这一条是本轮新增的）

以前是**静默**的：生成照常出图，SSE 里一条提示都没有，只有日志里几行
`GraphStoreError`。现在同一件事会出现在诊断里，四种原因各一句：

| 原因 | code | 处置 |
|---|---|---|
| 没配 `MINDMAP_NEO4J_PASSWORD` | `GRAPH_OUTLINE_UNAVAILABLE` | 改配置，重启后端 |
| 连不上（刚失败一次） | `GRAPH_OUTLINE_UNAVAILABLE` | 起服务（`run-neo4j.bat`） |
| 熔断冷却中 | `GRAPH_OUTLINE_UNAVAILABLE` | 等着（30s → 300s，重启后端反而更慢） |
| 带边实体 < 8 | `GRAPH_OUTLINE_UNAVAILABLE` | 什么都不用做（材料太少） |

入图那一侧另有两个 code：`GRAPH_INGEST_UNAVAILABLE`（没配 / 连不上）与
`GRAPH_INGEST_SKIPPED`（这批材料自带章节目录，入图的产物不会被读到——省下的正是
抽取的钱）。同一个原因**在一个进程里只报一次**（否则冷却 30 秒里连点几次生成就会
刷出几条一样的话），日志里每次都有痕迹。
