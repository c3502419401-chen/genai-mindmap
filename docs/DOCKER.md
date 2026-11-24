# 单机容器化部署（Docker / docker compose）

这一页只讲**一台机器、两个容器、一个 SQLite 文件**的形态：backend（FastAPI + docling）+ frontend（nginx 托静态产物 + `/api` 反代）。

> **先说结论性的一句话**：这套东西**没有在真机上构建或运行过**（开发机上不存在 `docker` 命令），所有"能跑"的结论都是静态核验推出来的。第 9 节逐条列了哪些必须到真机上才能确认。请把这一页当成"一份写得很仔细的部署脚本 + 一份诚实的未验证清单"，而不是"已验证的部署说明"。

---

## 1. 与现有 systemd 部署的关系

仓库里已经有一套**不用容器**的部署（`deploy/`：`install.sh`、`genai-mindmap.service`、`nginx.conf`、`setup-python.sh`、`backup.sh`、CD 入口），目标机是 2C2G 的单台 ECS，细节见 `docs/DEPLOY.md`。

**两套部署互斥，不要同时跑。** 三个硬冲突：

| 冲突点 | 后果 |
| --- | --- |
| 端口 80 | systemd 那套的 nginx 与 frontend 容器都监听 80，后启动的直接失败 |
| 端口 8000 | systemd 那套的 uvicorn 监听 127.0.0.1:8000；容器版不发布 8000，但若按调试说明临时发布了，就会撞上 |
| SQLite 文件 | 两边各有自己的 `MINDMAP_DB_PATH`，同时跑就是**两份互不同步的数据**——用户在这边存的图，在那边看不见 |

真正的风险是第三条：端口冲突会当场报错、你立刻知道；两个 SQLite 文件同时存在则**不报错**，等你切回去才发现"我的导图呢"。

迁移只有一条路：**停一套 → 导数据 → 起另一套**。两个方向的过程见第 6 节。切换期间必须让用户知道服务会中断——这不是蓝绿，是换轮胎。

另外明确一句：**容器化不接管现有生产 CD。** `.github/workflows/` 与 `/usr/local/sbin/genai-cd-apply` 一行都没动，CI 不会构建镜像、也不会部署容器。要不要加一个 build 步骤见第 12 节。

---

## 2. 产出物清单

| 文件 | 作用 |
| --- | --- |
| `docker-compose.yml` | 两个 service、两个 named volume、健康检查、依赖顺序 |
| `docker/backend.Dockerfile` | `python:3.12-slim` + `requirements.txt` + CPU 版 torch |
| `docker/frontend.Dockerfile` | `node:24-slim` 构建 → `nginx:1.27-alpine` 托管 |
| `docker/nginx.conf` | 站点配置：静态托管 + `/api` 反代 + SSE 三条 |
| `docker/.env.example` | 配置模板（**要拷到仓库根**成 `.env`，见第 3 节） |
| `.dockerignore` | 构建上下文排除规则（`.venv` 1.4GB、`node_modules`、`.git`、`data`） |
| `docker/verify-static.py` | 静态核验脚本（第 10 节），只读，需要 PyYAML |

`deploy/` 下的文件**全部未改动**，两套部署可以共存于同一分支，靠"你选哪套去跑"区分。

---

## 3. 构建与启动

### 3.1 前置

- Docker Engine 20.10+ / Docker Desktop，带 compose v2（`docker compose`，不是老的 `docker-compose`）
- 磁盘：镜像约 1.3–1.7GB（见第 7 节）+ LibreOffice 可选层 1.5GB
- 内存：常驻约 350MB，解析 PDF 时峰值再 +70MB 量级（`docs/DEPLOY.md` 第 5 节）

### 3.2 配好 `.env`（**必须在仓库根**）

```bash
cp docker/.env.example .env
# 编辑 .env，至少填 MINDMAP_LLM_* 三项
```

为什么模板在 `docker/` 下、成品却在仓库根：`docker-compose.yml` 在仓库根，compose 的 `env_file: - .env` 与 `${VAR}` 插值都按**compose 文件所在目录**解析。放别处等于配置不生效。

三件必须知道的事：

1. **缺 `.env` 会直接启动失败**，不是静默带空配置跑。这是刻意的，与 `deploy/genai-mindmap.service` 里 `EnvironmentFile=` 不带 `-` 前缀同一个用意——静默空配置会让 `MINDMAP_DB_PATH` 回落进镜像的只读层，表现成"一保存就 500"。
2. **后端没有 dotenv**。`backend/app/**` 里不存在 `load_dotenv`，它只读进程环境变量。`env_file` 不是"让程序去读文件"，而是 compose 替你把这些键注入进程环境。
3. 仓库根的 `.env` / `.env.*` 已被 `.gitignore` 忽略，密钥不会进版本库。

**必填四项**（`MINDMAP_LLM_MODE` / `_BASE_URL` / `_API_KEY` / `_MODEL`）：代码的缺省值不是 demo，不配时 agent 因缺密钥直接报错，不会降级成占位图。

### 3.3 构建与启动

```bash
docker compose build            # 构建两个镜像
docker compose up -d            # 启动
docker compose ps               # 看健康状态（两个都该是 healthy）
docker compose logs -f backend  # 跟日志
```

打开：

- `http://<主机>/` —— 前端
- `http://<主机>/api/health` —— 后端健康检查，返回 `{"status":"ok","version":"..."}`

`version` 来自 `MINDMAP_VERSION` 环境变量（`backend/app/main.py` 的 `_app_version()`，缺省 `dev`）。想让它有意义就在 `.env` 里加 `MINDMAP_VERSION=<提交或标签>`。

前端端口不是 80 就在 `.env` 里改 `WEB_PORT=8080`。

### 3.4 构建参数（两个）

```bash
# 国内机器换 pip 镜像（torch 的 CPU 轮子固定在官方源，不走这个）
PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple docker compose build backend

# 装 LibreOffice，支持 .doc / .ppt（约 1.5GB，会顶穿 2.5GB 体积预算）
WITH_OFFICE=1 docker compose build backend
```

两个都能写进 `.env`，不用每次带环境变量：

```bash
docker compose build --build-arg WITH_OFFICE=1 backend   # 等价写法
```

`WITH_OFFICE=0`（默认）时 `.doc` / `.ppt` 上传收到一条"需要 LibreOffice"的 422，**其余格式全部不受影响**（`.pdf` / `.docx` / `.pptx` / `.xlsx` / `.xls` / `.html` / `.md` / 图片 / 音频）。要不要为两种旧格式付 1.5GB，见 `docs/LEGACY-FORMAT-COST.md`。

### 3.5 不想每次都重建

```bash
docker compose up -d --build    # build + up 一步
docker compose down             # 停并删容器（卷保留）
docker compose down -v          # 连卷一起删（数据没了，慎用）
```

---

## 4. 数据在哪

**唯一的状态文件是 `/data/mindmaps.db`**（容器内路径），落在 named volume `mindmap-data` 上。

它装的不只是"导图"：`backend/app/mindmaps/repository.py::connect()` 是全仓唯一的 SQLite 入口，导图、运行记录、知识库、块向量缓存、外部检索缓存在**同一个文件**里。所以一个卷就够，不存在"要不要再挂一个上传目录"的问题。

> **没有上传目录。** 上传走 `UploadFile` 读进内存（`backend/app/ingestion/router.py:80`，上限 10MB / 音频 20MB / 图片 10MB），不落盘。唯一落盘的是 `.doc` / `.ppt` 转换时的临时目录（`backend/app/ingestion/legacy_office.py:80` 的 `tempfile.TemporaryDirectory`），退出即清，用的是容器 `/tmp`。别在文档里给自己编一个"uploads 卷"。

另一个卷 `mindmap-models` 挂到 `/models`，装的是 docling 的版面/表格模型（首次解析 PDF/DOCX 时从 HuggingFace 下载，几百 MB）。**丢了只是下次要重新下**，不是数据损失。

| 卷 | 容器内路径 | 内容 | 丢了会怎样 |
| --- | --- | --- | --- |
| `genai-mindmap_mindmap-data` | `/data` | `mindmaps.db` | 全部导图与知识库丢失（除非有备份） |
| `genai-mindmap_mindmap-models` | `/models` | docling/HF 模型缓存 | 下次解析重新下载 |

卷名前缀来自 compose 的 `name: genai-mindmap`（写死，不与目录名绑定）。

**为什么要两个卷而不是一个**：模型缓存通常几百 MB 且可重建，数据库通常几十 MB 且不可重建。分开才能在"备份"和"搬家"时区别对待——你不会想把模型也 gzip 进每日备份。

**`HOME=/data` 是必须的**，不是随手写的。docling 的缓存目录缺省是 `Path.home()/".cache"/"docling"`（`site-packages/docling/datamodel/settings.py:72`），而 tectonic 引擎**只认 HOME**（`docling/backend/latex/engines/tectonic.py:85`）。`HOME` 不可写时，首次解析 docx/pptx 下载模型会失败，而报错长得像"模型有问题"。容器里 `/home/app` 是可写的，但把 `HOME` 指到 `/data` 是为了让**.cache 也落在卷上**，否则容器一重建就重下。

### 4.1 看卷在宿主机哪

```bash
docker volume inspect genai-mindmap_mindmap-data --format '{{ .Mountpoint }}'
```

一般 `/var/lib/docker/volumes/genai-mindmap_mindmap-data/_data`。**别直接去改那个目录里的文件**，尤其别在容器跑着的时候 `cp` 一个旧 db 盖上去。

---

## 5. 备份与恢复

`deploy/backup.sh` 用的是 `sqlite3 "$DB" ".backup '$OUT'"`，不是 `cp`。它在脚本第 5-6 行写了理由：服务在跑的时候直接复制文件，**有可能拿到"页写了一半"的副本**。`cp` 只按字节拷，不理解 SQLite 的页边界，也不会去拿锁——拷到一半时正好有一笔事务在写，那份副本里就有半页。

**容器版有个现实差异：镜像里没有 `sqlite3` 命令**（`python:3.12-slim` 不带，`requirements.txt` 也不提供）。所以不能照抄 `deploy/backup.sh` 里的那一行。三种可行做法：

### 5.1 用临时容器调 SQLite 的 `.backup`（推荐，可在线）

```bash
docker run --rm \
  -v genai-mindmap_mindmap-data:/data:ro \
  -v "$PWD/backups:/backup" \
  keinos/sqlite3 \
  sqlite3 /data/mindmaps.db ".backup '/backup/mindmaps-$(date +%Y%m%d).db'"
```

`:ro` 是**故意的**：备份进程不该能改库。`.backup` 在只读挂载上也能工作（它读源、写目标）。

缺点：多引入一个第三方镜像。介意就用 5.2。

### 5.2 用后端镜像里的 Python（不引入新镜像）

镜像里就有 `python` 和 `sqlite3` 标准库，走 Python 的 backup API：

```bash
docker compose exec -T backend python -c "
import sqlite3
src = sqlite3.connect('/data/mindmaps.db')
dst = sqlite3.connect('/data/mindmaps-backup.db')
with dst:
    src.backup(dst)
dst.close(); src.close()
print('ok')
"
docker compose cp backend:/data/mindmaps-backup.db "backups/mindmaps-$(date +%Y%m%d).db"
docker compose exec -T backend rm /data/mindmaps-backup.db
```

`Connection.backup()` 与 `sqlite3 .backup` 是同一套在线备份 API，能拿到一致快照。缺点是中间产物落在数据卷上（写完就删，占的是同盘空间）。

### 5.3 停容器后拷卷（最简单，但会中断服务）

```bash
docker compose stop backend
docker run --rm -v genai-mindmap_mindmap-data:/data:ro -v "$PWD/backups:/backup" \
  alpine tar czf "/backup/mindmaps-$(date +%Y%m%d).tar.gz" -C /data .
docker compose start backend
```

停写之后 `cp` 也安全。代价是停服期间前端会 502。

### 5.4 恢复

```bash
docker compose stop backend
docker run --rm -v genai-mindmap_mindmap-data:/data -v "$PWD/backups:/backup" \
  alpine sh -c 'rm -f /data/mindmaps.db* && cp /backup/mindmaps-20250101.db /data/mindmaps.db'
docker compose start backend
```

三条注意：

- **先删掉可能存在的 `mindmaps.db-wal` / `-shm` / `-journal`**（上面用 `rm -f /data/mindmaps.db*` 一起删）。这几个是 SQLite 的日志文件：`-journal` 是回滚日志，主库文件被换掉之后它还指着旧的事务，配起来会被判成"库损坏"。后端代码没有显式设 `journal_mode`（全仓只有 `library.py:147` 的一条 `PRAGMA foreign_keys = ON`），所以走的是默认回滚日志；但换库时把这类残留一并清掉是零成本的确定性做法——**别去分辨"这个库用哪种日志模式"，直接删干净。**
- **别在容器跑着的时候盖文件。** 后端连着旧 inode，你换掉文件它不知道，之后写入会落到那个已经被 `rm` 掉的旧文件里。
- 恢复后打一次 `/api/health` 确认起来了，再打一次 `/api/mindmaps` 看列表是不是预期的那份。

### 5.5 定时备份

容器版的定时任务由**宿主机**的 cron / systemd timer 调上面的命令，不要放进容器（容器重启就没了）。迁移/重建的完整说明见第 6 节。

---

## 6. 在 systemd 部署与容器部署之间迁移

只需要搬 **`mindmaps.db` 一个文件**；模型缓存不必搬（会重新下载，也可以拷过去省一次下载）。

### 6.1 systemd → 容器

```bash
# 1. 停服务（这一步之后到第 5 步之间，用户看到的是 502）
sudo systemctl stop genai-mindmap

# 2. 在线备份一份（服务已停，安全）
sudo sqlite3 /var/lib/genai-mindmap/mindmaps.db ".backup '/tmp/mindmaps.db'"

# 3. 起容器
cp docker/.env.example .env && $EDITOR .env      # 填 key
docker compose up -d --build

# 4. 把库塞进卷（容器已起来但还没数据，先停后端再放）
docker compose stop backend
docker run --rm -v genai-mindmap_mindmap-data:/data -v /tmp:/src:ro \
  alpine sh -c 'rm -f /data/mindmaps.db* && cp /src/mindmaps.db /data/mindmaps.db'
docker compose start backend

# 5. 验证
curl -s http://127.0.0.1/api/health
curl -s http://127.0.0.1/api/mindmaps | head -c 300
```

### 6.2 容器 → systemd

反向同理，用 `docker compose exec` 里的 Python `.backup`（5.2）或停容器后拷卷（5.3）取出 `mindmaps.db`，放回 `/var/lib/genai-mindmap/mindmaps.db`（注意属主 `genai`），再 `systemctl start genai-mindmap`。

### 6.3 想两边都留着做对比

**不要。** 见第 1 节。要对比就在不同机器上跑，别在同一台机器上跑两份指向"同一个应用、不同数据"的东西——用户分不清哪个是真数据。

---

## 7. 镜像分层与体积估算

### 7.1 后端镜像（`docker/backend.Dockerfile`）

| 层 | 内容 | 估算 |
| --- | --- | --- |
| 1 | `python:3.12-slim` 基础 | ~150MB |
| 2 | `apt-get install libgomp1` | ~2MB |
| 3 | `pip install` torch/torchvision（CPU 索引） | ~450–550MB |
| 4 | `pip install -r requirements.txt`（其余依赖） | ~700–900MB |
| 5 | `backend/app`（只有 .py） | ~2.3MB |
| 6 | LibreOffice（`WITH_OFFICE=1` 才有） | ~1.5GB |

**合计：约 1.3–1.7GB（不装 LibreOffice），2.8–3.2GB（装）。** 2.5GB 预算只有不装 LibreOffice 时才守得住。

估算依据与不确定性（重要）：

- 本机 `backend/.venv/Lib/site-packages` 实测 **1403.9MB**，其中 `torch` **513.7MB**、`cv2` 112MB、`scipy` 108.5MB、`transformers` 103.4MB、`pandas` 67.9MB、`sympy` 65.4MB、`pymupdf` 52.9MB。但那是 **Windows + Python 3.13** 的展开体积，与容器里的 **Linux + Python 3.12** 不是同一批 wheel，只能当量级参考。
- `docs/DEPLOY.md` 第 5 节记的是"Python 依赖约 700MB"，与本机实测的 1.4GB 差一倍。两者可能都对：平台/版本不同、是否计入 torch 不同。**我没能力判定哪个数字更接近容器里的真实值**，所以上面写的是区间。
- **torch 的 CPU 轮子下载体积没量到**：对 `download.pytorch.org` 上的 `.whl` 发 `HEAD` 返回 403，发 `Range: bytes=0-0` 只回 1 字节、不给总长。所以只能从"本机装好的 CPU 版 torch 展开 513.7MB"反推。
- 关键结论不受影响：**torch 必须从 CPU 索引装**。PyPI 上的 linux `torch` 轮子会拖 `nvidia-cublas` / `nvidia-cudnn` / `nvidia-nccl` 等十来个包，解压后 2–3GB，镜像直接顶到 5GB 以上。docling 的 `standard` extra 硬性要求 `torch>=2.2.2`（`docling_slim-*.dist-info/METADATA`），躲不掉，只能换 CPU 轮子。

### 7.2 为什么分两步 pip

`docker/backend.Dockerfile` 里是：

```
pip install --index-url <torch cpu> torch torchvision
pip install --index-url <PIP_INDEX_URL> -r requirements.txt
```

顺序不能换。第一步把 torch 装成"已满足"，第二步解析 `requirements.txt` 时 pip 就不会再去 PyPI 抓带 CUDA 的那份。

并且紧跟一条构建期断言，万一解析被带偏就在**构建时**失败，而不是推出 5GB 镜像之后才发现：

```dockerfile
RUN python -c "import torch,sys; sys.exit('CUDA wheel leaked in: ' + torch.__version__) if torch.version.cuda is not None else print('torch', torch.__version__, 'cpu-only')"
```

（本机 venv 里的 `torch 2.15.0+cpu`、`torch.version.cuda is None`，说明 CPU 轮子是这条路子，但**没有**在 Linux/py3.12 上验证过。）

### 7.3 前端镜像（`docker/frontend.Dockerfile`）

| 层 | 内容 | 估算 |
| --- | --- | --- |
| 1 | `node:24-slim`（仅构建段，不进最终镜像） | — |
| 2 | `npm ci`（仅构建段） | — |
| 3 | `nginx:1.27-alpine` | ~20MB |
| 4 | `dist`（静态产物） | ~0.3–1MB |
| 5 | `docker/nginx.conf` | <10KB |

**最终镜像约 21–22MB。** 多阶段构建的意义就在这里：`node_modules`（本机实测 129MB）与 `node:24-slim` 全部留在构建段，不进产物。

### 7.4 构建上下文

`.dockerignore` 把三块最大的排掉：`backend/.venv`（**实测 1415MB**）、`frontend/node_modules`（**实测 129MB**）、`.git`。不排的话每次 `docker build` 都要把 1.5GB 打包发给 dockerd——表现是"以为卡死了"。

---

## 8. SSE（流式生成）是怎么保住的

这是整套配置里最容易在"看起来能跑"的表象下丢东西的一块。三条链路各有一道保险，**少任何一层都会退回"攒够一批才发"**。

### 8.1 三道保险

1. **应用层**：`backend/app/agent/router.py:51-58` 的 `_SSE_HEADERS` 带 `X-Accel-Buffering: no`。代码注释明确记着"漏掉这条的那条路会在反代后面攒够一批才发"。
2. **nginx 层**：`docker/nginx.conf` 的 `location /api/` 里，从 `deploy/nginx.conf` 第 53-55 行原样搬来三条：

   ```nginx
   proxy_buffering off;
   proxy_cache off;
   chunked_transfer_encoding on;
   ```

3. **超时层**：生成预算最长 600 秒（`backend/app/agent/contracts.py:206` 的 `RunBudget.max_seconds`，`default=600.0`、`le=3600`），所以读超时必须比它大：

   ```nginx
   proxy_connect_timeout 10s;
   proxy_send_timeout    900s;
   proxy_read_timeout    900s;
   ```

   写成 60s 之类的"看起来合理"的值，长材料会以 `504 Gateway Time-out` 收场——**而后端其实还在跑**。用户看到的是"失败"，真实情况是"nginx 放弃等待"。这是 `deploy/nginx.conf` 里已经踩过一次的坑，别重新踩。

### 8.2 还有两条容易被当成"无关"的

```nginx
proxy_http_version 1.1;
proxy_set_header Connection "";
```

不上 1.1、不把 `Connection` 置空，nginx 与上游之间就是每个请求一条新连接。长生成期间会攒一堆 `TIME_WAIT`。

### 8.3 gzip 里**不能**有 `text/event-stream`

`docker/nginx.conf` 的 `gzip_types` 是：

```nginx
gzip_types text/css application/javascript application/json image/svg+xml;
```

**刻意不写 `text/event-stream`，也刻意不写 `*`。** 把事件流交给 zlib，事件的边界会被压缩缓冲吃掉，前端 `ReadableStream` 那边看到的是一段一段的、延迟不可预测的数据。这点在 `docker/nginx.conf` 里有注释，删之前读一遍。

### 8.4 前端怎么消费

`frontend/src/api/client.ts:228` 的 `postSse()` 用 `fetch` + `ReadableStream` 自己切帧（不用 `EventSource`，因为要 POST 长文本）。它依赖的是"每帧以空行分隔、`data:` 行取内容"——这正是 nginx 不能重新分帧、不能缓冲的原因。

### 8.5 怎么确认真的在流

真机上用这条（浏览器 F12 也行，看 Network 里那条 `text/event-stream` 是逐步增长还是"突然全出现"）：

```bash
curl -N -s -X POST http://127.0.0.1/api/agent/runs/stream \
  -H 'Content-Type: application/json' \
  -d '{"topic":"测试"}' | head -c 500
```

`-N` 关掉 curl 自己的缓冲。如果 30 秒内一个字节都没出来，而日志显示后端已经在跑，就是某一道保险没生效。

---

## 9. 这些**没在真机上验证过**

**开发机上没有 `docker` 命令**（`docker --version` 与 `docker compose version` 都报"术语不被识别"），所以下面每一条都只做到了静态核验，没有一条跑起来过。

### 9.1 从来没执行过的（构建期）

| # | 未验证项 | 需要什么才能验证 | 可能怎么坏 |
| --- | --- | --- | --- |
| 1 | `docker compose build` 整体成功 | Linux 上装 Docker 20.10+ 的机器 | 某个 apt 包名在 slim 里不存在；`libgomp1` 之外的隐式系统库缺失 |
| 2 | torch CPU 轮子能装到 py3.12 / linux | 同上，且能访问 `download.pytorch.org` | 只确认了目录结构，没确认 cp312 wheel 的实际依赖解析 |
| 3 | 构建期断言 `torch.version.cuda is None` 通过 | 同上 | 若 pip 仍从 PyPI 解析，断言会失败（这是它的设计目的），要改的是 pip 参数不是断言 |
| 4 | `libgomp1` 足够让 `import torch` 成功 | 同上 | 可能还缺 `libstdc++` 之外的别的（如 `libc6` 的版本要求） |
| 5 | `pip install -r requirements.txt` 在 py3.12 全绿 | 同上 | 本机是 py3.13，某些包在 3.12 上的 wheel 覆盖没核过 |
| 6 | `npm ci` 在 node:24-slim 上成功 | 同上 | lockfile 里有 linux 原生包（rollup/oxide/lightningcss），核过存在但没装过 |
| 7 | `npm run build`（`vue-tsc --noEmit && vite build`）在容器里通过 | 同上 | 与 CI 同一命令，但容器内 node 版本/字体等环境不同 |
| 8 | 镜像体积真的在 1.3–1.7GB | 构建完 `docker images` | 第 7 节的估算偏差可能达 ±30%，见那里的说明 |
| 9 | `WITH_OFFICE=1` 时 LibreOffice 层能装 | 同上 | Debian slim 里 `libreoffice-writer` / `libreoffice-impress` 的包名与依赖没核过 |
| 10 | `.dockerignore` 在真 Docker 下的语义与 `fnmatch` 模拟一致 | 构建完看上下文大小 | 脚本用 `fnmatch` 近似，`**` 的递归匹配在 Docker 实现里细节不同 |

### 9.2 从来没执行过的（运行期）

| # | 未验证项 | 需要什么才能验证 | 可能怎么坏 |
| --- | --- | --- | --- |
| 11 | 两个容器能起来并通过 healthcheck | `docker compose up -d` | — |
| 12 | `backend:8000` 的 DNS 在 compose 内网能解析 | 同上 | 依赖 `resolver 127.0.0.11`（Docker 内嵌 DNS），只在 user-defined network 存在 |
| 13 | nginx 启动时**不**因为 backend 未就绪而退出 | 同上 | 用了 `set $backend` + `resolver` 就是为了这个，但没实测过"backend 先挂掉再重启"的时序 |
| 14 | nginx 在 backend 换 IP 后能自动跟上 | 重建 backend 容器 | `valid=10s` 的重新解析行为只在文档上确认过 |
| 15 | `/api/health` 过反代返回 200 | `curl http://<host>/api/health` | — |
| 16 | 前端页面能打开、能调通 `/api` | 浏览器打开 | 同源假设：`client.ts:45` 缺省 `BASE='/api'`，没设 `VITE_API_BASE` |
| 17 | **SSE 真的在流**（第 8 节） | 第 8.5 节的 `curl -N` | 这是最需要真机确认的一条，也是最容易"看起来正常其实不流"的一条 |
| 18 | SQLite 写在 `/data` 卷上、容器重建后数据还在 | `docker compose down && up` | — |
| 19 | docling 首次解析 PDF 能下模型到 `/models` | 传一份 PDF | `HF_ENDPOINT=https://hf-mirror.com` 的可达性、卷权限 |
| 20 | `HOME=/data` 下的 `.cache/docling` 可写 | 同上 | `useradd --uid 10001` 的属主与卷属主是否匹配（"空 named volume 复制镜像目录属主"这个行为本身没实测，但写权限已由第 9.5 节的构建期探针兜住，属主对不上会在构建时就失败而不是运行时） |
| 21 | 健康检查脚本（`python -c` 里的 urllib）在容器里能跑 | `docker compose ps` 显示 healthy | 单行 `python -c` 的引号转义没在真 shell 里跑过 |
| 22 | `wget` 在 `nginx:1.27-alpine` 里存在（busybox 提供） | 同上 | alpine 的 busybox 通常带 wget，没实测 |
| 23 | `stop_grace_period: 90s` 下正在跑的生成能优雅收尾 | 生成中途 `docker compose down` | 与 systemd 的 `TimeoutStopSec=90` 同量级，但没实测 |

### 9.3 明确"不知道"的

- **镜像体积的确切数字**：第 7 节是区间估算，不是测量值。
- **`HF_ENDPOINT=https://hf-mirror.com` 的端到端可用性**：`docs/DEPLOY.md` 第 11 节已经标注"这条没做过端到端验证"，容器版沿用同一个不确定性。
- **docling 在容器里首次初始化要多久**：代码注释说"十几秒"，但那是作者机器，容器里可能更久（`start_period: 40s` 是猜的）。
- **compose 的 `env_file` 与 `environment` 谁覆盖谁**：文档上 `environment` 优先，核验脚本也按这个假设写断言（`docker/.env.example` 里那三个容器路径是注释掉的，就是为了防误改），但没在真机上对比过 `docker compose config` 的实际输出。

### 9.4 已有的自动化核验（能替代一部分，替代不了上面这些）

`docker/verify-static.py`，第 10 节说明。

### 9.5 卷属主：从"未验证"改成"已用构建期断言兜住"

这是原本 9.2 里第 21 条，也是**第一次真跑最可能炸**的地方：非 root（`app` / uid 10001）写不进 `/data`，用户第一次保存导图就看到 `Permission denied`，很容易以为应用本身坏了。

现在不是靠"文档上说应该没问题"，而是在**构建期**就把它证掉。`docker/backend.Dockerfile` 里加了两道，顺序有意义：

```dockerfile
# 第一道（root 身份）：比数字属主。不能用 test -w —— 这一步还是 root，
# root 对任何路径的 -w 都为真，那是恒过的假断言。
RUN test "$(stat -c '%u:%g' /data)" = "10001:10001" \
 && test "$(stat -c '%u:%g' /models)" = "10001:10001" \
 && test "$(stat -c '%u:%g' /home/app)" = "10001:10001" \
 && echo "ownership ok: uid/gid 10001 for /data /models /home/app"

USER app

# 第二道（最终运行身份）：真的建文件再删掉，这才是"能不能写"。
RUN for d in /data /models; do \
      f="$d/.write-probe"; \
      if : > "$f"; then \
        rm -f "$f"; \
        echo "writable as uid $(stat -c '%u' "$d"): $d"; \
      else \
        echo "NOT WRITABLE as non-root user: $d" >&2; \
        exit 1; \
      fi; \
    done
```

为什么第二道不能省：第一道只证明**属主位**对，证明不了以 app 身份真能写（还有 ACL、目录位、挂载方式这些变量）。反过来第一道也不能省——它能在 `USER app` 之前就报错，定位信息更直接。两道都在**构建期**失败，日志里直接写着哪条路径不可写，而不是等用户第一次保存导图时给一个 `Permission denied`。

这也解释了为什么 `chown` 必须放在 `USER app` **之前**：放到之后等于没做（那时已经不是 root，chown 会失败）。核验脚本对这一点的断言见第 10.1 节第 13 类。

**仍然没被兜住的**：named volume 首次创建时"把镜像里该目录的属主复制进卷"这个 Docker 行为本身，只在官方文档上确认过、没在本机实测。如果它不成立（比如用了 `volume` 的 `driver_opts` 或预置了非空卷），容器里仍会是 `Permission denied`——但那时构建期两道断言都是绿的，说明问题出在卷而不是镜像。文档第 5 节的备份/恢复流程里如果手工造过卷内容，要记得 `chown 10001:10001`。

---

## 10. 静态核验做了什么

```bash
backend/.venv/Scripts/python.exe docker/verify-static.py .    # Windows
.venv/bin/python docker/verify-static.py .                    # Linux
```

只读脚本，**96 项断言，当前全部 PASS**（需要 PyYAML；`backend/.venv` 里有）。它检查的是"文件之间是否自洽"，不检查"能不能跑"。

### 10.1 覆盖的十三类

1. **compose 结构**：YAML 可解析；service 恰为 `{backend, frontend}`；`name: genai-mindmap`；顶层卷恰为 `{mindmap-data, mindmap-models}`
2. **路径存在性**：两个 service 的 `build.context` 与 `build.dockerfile` 解析成绝对路径后真实存在
3. **卷与路径一致性**：`mindmap-data→/data`、`mindmap-models→/models`；`MINDMAP_DB_PATH` 以 `/data/` 开头；`HF_HOME`/`DOCLING_CACHE_DIR` 以 `/models` 开头；`HOME` 以 `/data` 开头；`MINDMAP_EXTERNAL_PROXY` 存在
4. **端口与依赖**：backend **无** `ports`、只有 `expose: ["8000"]`；frontend `ports == ["${WEB_PORT:-80}:80"]`；`depends_on.backend.condition == service_healthy`；backend 有 healthcheck 且命中 `/api/health`；`env_file == [".env"]`
5. **变量闭环**：compose 里（剥掉注释行后）每个 `${VAR}` 在 `docker/.env.example` 都有赋值行；compose 的每个 build arg 在 `backend.Dockerfile` 有 `ARG` 声明
6. **nginx upstream 名 == compose service 名**：从 nginx 里抽出所有 `http://host:port`，主机名集合必须恰为 `{"backend"}`
7. **SSE 八条逐条正则**：`proxy_buffering off` / `proxy_cache off` / `chunked_transfer_encoding on` / `proxy_http_version 1.1` / `proxy_set_header Connection ""` / `proxy_connect_timeout 10s` / `proxy_send_timeout 900s` / `proxy_read_timeout 900s`
8. **超时与体积语义**：`proxy_read_timeout`(900) > `RunBudget.max_seconds`(600)；`client_max_body_size` ≥ 21m（音频上限 20MB）；`gzip_types` 不含 `event-stream` 也不含 `*`
9. **nginx 语法级自检**：花括号净深度 0；每条指令以 `;`/`{`/`}` 结尾；有 `resolver 127.0.0.11`；`set $backend` 在 server 块内；剥注释后无 `__DOMAIN__`/`__WEB_ROOT__`/`__CERT_DIR__`/`__LISTEN6__` 残留
10. **Dockerfile 关键事实**：后端 `python:3.12-slim`、`--workers 1`、`USER app`、HEALTHCHECK 打 `/api/health`；前端构建段 `node:24`、有 `AS build`、运行段 `FROM nginx:`、有 HEALTHCHECK
11. **COPY 源路径**：从两个 Dockerfile 抽出所有非 `--from=` 的 COPY 源（10 个），逐个 `exists()`；再用 `fnmatch` 模拟 `.dockerignore`，断言这 10 个**都没被排除**，同时断言 `.git`/`backend/.venv`/`frontend/node_modules`/`frontend/dist`/`.env`/`backend/data` 六个**都**会命中排除规则（这一条是**规则匹配**，不是存在性检查——`frontend/dist` 与 `backend/data` 在当前工作树里本来就不存在，它们在构建机与生产机上才会出现，规则必须能挡住它们）
12. **依赖清单与脚本名**：`requirements.txt` 包名可解析（`docling, fastapi, httpx, pydantic, pymupdf, python-multipart, uvicorn[standard], xlrd, neo4j`）；含 `docling` 与 `uvicorn[standard]`；`package.json` 的 `build` 恰为 `vue-tsc --noEmit && vite build`；`package-lock.json` 的 `lockfileVersion == 3`（`npm ci` 的前提）；`tsconfig.json` 的 `include` 含 `"tests"`
13. **卷属主与非 root 可写（第 9.5 节那条）**：`mkdir -p /data /models` 存在；`chown -R app:app /data /models` 存在；**`chown` 的行号在 `USER app` 之前**（放到之后等于没做）；`chown` 与 `mkdir` 在同一条 `RUN` 内（相差 ≤3 行，防止中间插层把属主盖掉）；root 侧断言用的是 `stat` 比数字属主而不是 `test -w`（后者对 root 恒真，是假断言）；`USER app` **之后**还有一条真探针；探针覆盖 `/data` 与 `/models` 两个落点；探针失败会 `exit 1`

这一类的断言做过**变异测试**：在临时副本里分别把 `chown` 删掉、把 `chown` 挪到 `USER app` 之后、把 `stat` 断言退化成 `test -w`、把探针改成只测 `/tmp`、把探针里的 `exit 1` 去掉——5 种改法全部被脚本抓到（exit 1），还原后回到 96/0。没做变异测试的断言不能算"在检查"，只能算"恰好没报错"。

### 10.2 核验过程中真的抓到过两个问题

写下来是因为"核验脚本第一次跑就全绿"通常说明它没在检查什么：

1. **真问题**：`HF_ENDPOINT` 只出现在 compose 里，模板 `docker/.env.example` 缺赋值行 → 用户按模板配了 `.env`，`${HF_ENDPOINT:-...}` 仍走缺省，但模板里看不到这个键，改起来要找文档。已在模板补一节"模型下载源"。
2. **脚本自己的误报**：正则扫到了注释里的 `${VAR}` 和注释里提到的 `__DOMAIN__`。两处断言改成先剥注释行/剥注释后判断。修的是脚本，不是配置。

### 10.3 三处"为了少赌一层解析"而改的写法

本机没有 docker 验证不了 Dockerfile 解析器的边缘行为，所以：

- **`CMD` 写成单行 JSON 数组**（不用 `\` 续行）。exec 形式要靠解析器先吃掉续行再交 JSON 解析。
- **构建期断言写成单行 `python -c`**（不用 `\` 续行 + 多语句）。
- **`ENV` 的注释放在 `ENV` 之前**，不夹在续行中间。续行里的注释行是否能被正确剥离，各版本解析器的行为不一定一致。

---

## 11. 已知限制

1. **不内置 TLS。** `docker/nginx.conf` 只 `listen 80`。要 HTTPS 就在宿主机再架一层 nginx/caddy 反代到这个容器的 80，或者上 `deploy/nginx-tls.conf` 那套（但那套是给 systemd 部署的）。参考 `deploy/nginx-tls.conf` 的 443 配法，注意里面同样有三条 SSE 配置与 900s 超时，别在搬的时候漏掉。
2. **nginx 那层没有任何 Basic Auth，鉴权全在应用层。** 账号体系上线后，两份 nginx 配置（`deploy/nginx.conf`、`docker/nginx.conf`）都不再有 `auth_basic`；拦截匿名请求的是 `backend/app/auth/gate.py`，发身份与游客 cookie 的是 `IdentityMiddleware`。全站一个口令那层已经**弃用**——它不只是重复，还与"游客每天 3 次免费"直接冲突：口令过不去就拿不到游客 cookie，新访客连试用的机会都没有。

   > **⚠️ 但容器版仍然要求"有身份"，而身份是应用自己发的不代表外面有一道锁。** 唯一被应用放行的匿名路径是 `/api/health` 和 `/api/auth/*`（注册、登录、领游客 cookie）。**直接把它暴露到公网，任何人扫到 `IP:WEB_PORT` 都能立刻领一个游客身份、用掉那 3 次免费额度，并注册账号**——如果你不打算接受陌生人注册，就在外层（宿主机 nginx/caddy、安全组、SSO）限住来源。这是"门禁"与"谁能进来"两件事的分工：应用负责前者，部署方负责后者。
   >
   > **`/api/settings/model` 那个面为什么现在安全了。** 它曾经完全没有鉴权：`PUT` 能把模型地址改指向任意主机、`POST /api/settings/model/test` 会带着**已存的那把密钥**去请求那个地址（`backend/app/settings/probe.py`）。两步就能把密钥明文送到攻击者手上，不需要知道它是什么。现在有三道，且都在代码层（不依赖部署方记得配什么）：门禁要求有身份；`probe._refuse()` 拒本机/内网/保留地址/云元数据（要连本机模型得显式设 `MINDMAP_SETTINGS_ALLOW_PRIVATE=1`）；`store.resolve()` 把密钥与它保存时的**主机名**绑定，换个地址就不再交出密钥（`api_key_source: "unbound"`，界面提示重贴一次）。

3. **`--workers 1` 写死。** 不是性能调优：状态全在一个 SQLite 文件里（`backend/app/mindmaps/repository.py::connect()`），多 worker 就是多个写者，会偶发 `database is locked` 的 500。2C2G 上也吃不起多进程内存。**不要为了"多用几个核"去改这个值。**
4. **后端不发布 8000。** 只有前端容器在宿主机上开 `WEB_PORT`。要本机调后端就临时加 `ports: ["127.0.0.1:8000:8000"]`，调完删掉——线上形态是"后端只听内网"。
5. **`MINDMAP_EXTERNAL_PROXY` 必须显式设。** 代码缺省值是 `http://127.0.0.1:7897`（作者本机的 Clash，`backend/app/knowledge/external.py:196`）。**容器里的 `127.0.0.1` 是容器自己**，写它等于"哪都连不上"，而且失败是 5–6 秒后静默返回空。compose 已默认成 `none`（直连）。要走宿主机代理：

   - Docker Desktop（macOS/Windows）：`http://host.docker.internal:7897` 直接可用
   - Linux：`host.docker.internal` 默认不解析，要给 backend 加

     ```yaml
     extra_hosts:
       - "host.docker.internal:host-gateway"
     ```

     而且代理得监听 `0.0.0.0` 而不是 `127.0.0.1`，否则容器照样连不上。

6. **Neo4j 也不在容器里。** 图存储是可选的独立服务（`MINDMAP_GRAPH_*`），compose 没带。要连宿主机的就写 `host.docker.internal:7687`，别写 `127.0.0.1:7687`。
7. **`MINDMAP_NLI_ENABLED=0` 默认关。** 引用语义复核要 ONNX 模型，模型文件得自己放进卷（`MINDMAP_NLI_MODEL_DIR`）。容器里没预置，开了但目录是空的会失败。
8. **卷不会自动备份。** 第 5 节给的是命令，不是定时任务。宿主机的 cron/timer 要自己配。
9. **`docker compose down -v` 会连卷一起删。** 手滑一次数据全没。备份没做起来之前，别在生产机上敲这条。
10. **镜像里几乎没有调试工具。** 基础镜像是 `python:3.12-slim`（Debian 系），没有装 `sqlite3` 命令行、没有 `curl`、没有 `git`（`requirements.txt` 也不提供这些东西）。要在容器里查数据用 `docker compose exec backend python -c "..."`——`sqlite3` 标准库一定有。这是刻意的：镜像里每多一个工具，攻击面和体积都涨一点。至于这个 slim 镜像里到底还有哪些 shell 与工具（`bash` / `sh` / `wget` 之类），**我没有在真机上看过**，见第 9.2 节。
11. **不接管 CD。** `.github/workflows/` 没有构建镜像的步骤，`/usr/local/sbin/genai-cd-apply` 也不会去动容器。见第 12 节。

---

## 12. 已拍板的取舍，与落地前必须做的一件事

下面四条都**已经由用户拍板**，这里是记录结论与理由，不需要再讨论。

1. **CI 里不加 `docker build`。** 保持 `.github/workflows/ci.yml` 现状不动。理由：本机没有 Docker，在没人见过它成功构建之前就塞进 CI，只会得到"天天红、而且红不指向真实回归"——CI 的红灯必须意味着"代码坏了"，不能意味着"我们的验证环境不完整"。等第 12.1 节那条做完再考虑加。
2. **LibreOffice 默认不装**（`WITH_OFFICE=0`）。装了能支持 `.doc`/`.ppt`，代价是镜像从约 1.5GB 变成约 3GB，顶穿 2.5GB 预算。不装时这两种格式返回 422，提示里说明需要 LibreOffice。要开就在 `.env` 里设 `WITH_OFFICE=1` 重新构建。成本分析见 `docs/LEGACY-FORMAT-COST.md`。
3. **不用 nginx 的 Basic Auth**，鉴权由应用层账号体系承担（见第 11 节第 2 条）。这**不代表容器版"开箱即可安全上公网"**：门禁只拦匿名，游客身份谁都能领、注册是开放的，能不能进来要由部署方在外层限。
4. **不引入 registry**，只用本地 tag（`genai-mindmap-backend:local` / `genai-mindmap-frontend:local`）。单机部署不需要分发；要做多机再说。

### 12.1 落地前必须做：找一台有 Docker 的机器跑通一次

**这一条是硬的，不能跳。** 本仓库里这 8 个文件**从头到尾没有在任何一台机器上构建或运行过**（原因见第 9 节）。在有人真的跑通之前，它们的地位是"一份经过静态核验的草案"，不是"能用的部署方案"。

按这个顺序做，每一步失败都能定位到具体一层：

```bash
# 1. 构建（先只建后端，它最可能炸）
docker compose build backend

# 2. 起后端，确认 /api/health 通
#    后端不发布端口（第 11 节第 4 条），所以从容器内部打，不绕宿主机
docker compose up -d backend
docker compose exec backend python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/api/health').read())"

# 3. 确认两个卷真的可写（第 9.5 节那两道构建期断言之外的最后一道）
#    用 python 而不是 sh/touch：这个镜像里到底有没有 sh 我没验证过，
#    但 python 一定在
docker compose exec backend python -c "import pathlib
for d in ('/data','/models'):
    p = pathlib.Path(d)/'probe.txt'; p.write_text('x'); p.unlink(); print('RW ok', d)"

# 4. 起前端，浏览器打开，保存一张导图，重启容器确认数据还在
docker compose up -d frontend
docker compose restart backend

# 5. 确认 SSE 真的在流（第 8.5 节）
curl -N -s -X POST http://127.0.0.1/api/agent/runs/stream -H 'Content-Type: application/json' -d '{"topic":"测试"}' | head -c 500

# 6. 传一份 PDF，确认 docling 模型下到 /models
docker compose exec backend python -c "import pathlib
for d in ('/models','/data/.cache'): print(d, pathlib.Path(d).exists() and sum(1 for _ in pathlib.Path(d).rglob('*')))"
```

全部通过之后，再回头决定要不要把 `docker compose build`（不 push）加进 CI——那时候加，红灯才有意义。

**如果第 1 步就失败**：把构建日志末尾贴出来，最可能的三处是 `libgomp1` 之外缺系统库、torch CPU 索引的 URL 拼错、`npm ci` 的 linux 原生包没命中。第 9.1 节的表就是按"可能怎么坏"排的，对照着看。

---

## 13. 排障速查

| 现象 | 先看这里 |
| --- | --- |
| `compose up` 报 `.env` 不存在 | 第 3.2 节，模板要拷到**仓库根** |
| backend 一直 `unhealthy` | `docker compose logs backend`；`docker compose exec backend python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/api/health').read())"` |
| 前端打开是 502 | backend 没起来或 healthcheck 没过；nginx 用了变量 + `resolver`，backend 挂着时它不退出、只返 502——这是设计 |
| 首字迟迟不出来（SSE） | 第 8 节，尤其 8.5 的 `curl -N` |
| 长材料 504 | `proxy_read_timeout` 被改小了；必须 > 600s |
| 保存导图 500 | `MINDMAP_DB_PATH` 没指到卷，或卷属主不对（第 9.5 节） |
| 首次解析 PDF 很慢/失败 | 模型在下载（`/models`）；看 `HF_ENDPOINT` 与出网；日志里会有 HuggingFace 的下载记录 |
| 外部检索总是返回空 | `MINDMAP_EXTERNAL_PROXY` 指向了容器自己的 127.0.0.1（第 11 节第 5 条） |
| `.doc`/`.ppt` 报 422 | 没装 LibreOffice，见第 12 节第 2 条 |
| `database is locked` | 有人把 `--workers` 改成了 >1（第 11 节第 3 条） |
| 构建卡在 "transferring context" | `.dockerignore` 没生效，1.5GB 的 `.venv` 被发了（第 7.4 节） |

---

## 附：和 `docs/DEPLOY.md` 的分工

- `docs/DEPLOY.md`：systemd + nginx + CD 那套（**生产在用**），含资源测算、格式依赖表、出网受限、自动化部署
- 本页：容器那套（**新增、未验证**），含构建、compose 结构、SSE 三道保险、静态核验、未验证清单

两边共用的背景事实（格式依赖、资源占用、模型缓存）都写在 `DEPLOY.md`，本页只引用不复述，避免两份文档各自漂移。
