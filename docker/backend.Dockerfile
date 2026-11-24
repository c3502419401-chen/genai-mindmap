# syntax=docker/dockerfile:1
#
# genai-mindmap 后端镜像：FastAPI + docling + pymupdf + neo4j 驱动
#
# 构建上下文是**仓库根目录**（docker-compose.yml 里 context: .），不是 backend/：
# 镜像要同时拿到 backend/requirements.txt 与 backend/app。相应地把 .dockerignore
# 写成"从仓库根出发排除 .venv / node_modules / .git / data"，否则 1.4GB 的 .venv
# 会被塞进构建上下文。
#
# 构建：
#   docker build -f docker/backend.Dockerfile -t genai-mindmap-backend .
# 或者直接：docker compose build backend
#
# Python 3.12 与 CI（.github/workflows/ci.yml 的 python-version: "3.12"）和线上
# （deploy/setup-python.sh 优先 py3.12）一致。不要用本机 3.13 的假设。

FROM python:3.12-slim

# ---------------------------------------------------------------- 可调参数
# 国内机器把 PIP_INDEX_URL 换成镜像（deploy/setup-python.sh 用的是阿里云）：
#   --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
# torch 的 CPU 轮子只有官方源有，别把它也指到镜像上去。
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG TORCH_CPU_INDEX=https://download.pytorch.org/whl/cpu
# .doc / .ppt 的格式转换要 LibreOffice（约 1.5GB，只服务这两种旧格式）。
# 默认 0 = 不装；2C2G 机器上装了会直接顶穿 2.5GB 的体积预算。
ARG WITH_OFFICE=0

# HOME 必须指向一个**可写**目录：docling 的缓存缺省是 Path.home()/".cache"/"docling"
# （见 site-packages/docling/datamodel/settings.py:72），而 libdocling 的 tectonic
# 引擎更是只认 HOME（docling/backend/latex/engines/tectonic.py:85）。systemd 单元里
# 写的是 HOME=/var/lib/genai-mindmap，这里给 /home/app；模型本体由 compose 里的
# HF_HOME / DOCLING_CACHE_DIR 落到卷上（见 docker-compose.yml）。
# 注：这段注释放在 ENV **之前**而不是 ENV 的续行里——续行中间夹注释行是否能被
# 正确剥离是各版本解析器的边缘行为，本机没有 docker 可验证，不去赌。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/app

# ---------------------------------------------------------------- 系统依赖
# libgomp1 是 torch / scipy 的 OpenMP 运行时，python:3.12-slim 里没有，
# 缺了它 import torch 会报 "libgomp.so.1: cannot open shared object file"。
#
# 刻意**不装**：build-essential（requirements 里全是 wheel，没有源码包）、
# libgl1/mesa（docling 装的是 opencv-python-headless，不需要 libGL，那套 mesa
# 有 300MB 量级）。真遇到没有 wheel 的包再加，别提前装。
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend

# ---------------------------------------------------------------- Python 依赖
# 两步走，顺序不能换，理由在下面这段注释里：
#   1. 先从 PyTorch 的 CPU 索引装 torch / torchvision；
#   2. 再装 requirements.txt，此时 torch 已经"已满足"，pip 不会去动它。
#
# 为什么必须这样：**PyPI 上的 linux `torch` 轮子带 CUDA 依赖**，pip 会把
# nvidia-cublas / nvidia-cudnn / nvidia-nccl 等十来个包一起拖进来，解压后
# 2–3GB，镜像直接顶到 5GB 以上。docling 的 standard extra 硬性要求
# torch>=2.2.2（见 docling_slim-*.dist-info/METADATA），躲不掉，只能换成
# CPU 轮子：体积降到 500MB 量级，而本应用（PDF 版面识别、表格结构）本来
# 也用不上 GPU——2C2G 的单机根本没有显卡。
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --index-url "$TORCH_CPU_INDEX" torch torchvision \
 && pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt

# 构建期断言，两件事一起验：
#   1. 装进来的确实是 CPU 轮子。万一哪天 pip 的解析被别的依赖带偏、从 PyPI 抓了
#      带 CUDA 的 torch，镜像会在**构建时**就失败，而不是等到 5GB 的镜像推到仓库
#      才发现（问题在构建日志里，比在镜像体积里好查得多）。
#   2. libgomp1 真的能用——import torch 会去加载它，缺了就是这句报错。
# 写成单行是刻意的：多行 `python -c` 要靠 Dockerfile 的续行把 `\` + 换行吃掉，
# 而本机没有 docker 能验证解析结果，少一层拼装就少一个只能在真机上才发现的问题。
RUN python -c "import torch,sys; sys.exit('CUDA wheel leaked in: ' + torch.__version__) if torch.version.cuda is not None else print('torch', torch.__version__, 'cpu-only')"

# ---------------------------------------------------------------- 运行时用户
# 不用 root 跑：docling 会解析用户上传的 PDF/DOCX，解析器历史上出过 RCE。
# /data 与 /models 是卷挂载点，必须在镜像里就 chown 好——docker 创建**空的**
# named volume 时会把镜像里该目录的内容与属主一起复制进去，这是非 root 用户
# 能写卷的前提。
# 刻意不写 --shell：Debian slim 里 /usr/sbin/nologin 由 login 包提供，本机没有
# docker、无法确认它一定在；而缺省 shell 已经足够——账号没有密码、也不进 sudoers，
# 容器里本来也登不进来。少一个"可能在基础镜像里不存在"的路径引用。
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /data /models \
 && chown -R app:app /data /models /home/app

# 构建期断言（root 侧）：chown 真的生效了，位是 10001:10001。
# 为什么不用 `test -w /data`：这一步还是 root，root 对任何目录的 `-w` 都为真，
# 那是个恒过的假断言。所以这里比**数字属主**——`stat` 是 coreutils，slim 里
# 必定存在；`id`/`su`/`runuser` 的归属包不确定，不去赌。
# 真"以 app 身份能不能写"由下面 USER app 之后那条探针负责。
RUN test "$(stat -c '%u:%g' /data)" = "10001:10001" \
 && test "$(stat -c '%u:%g' /models)" = "10001:10001" \
 && test "$(stat -c '%u:%g' /home/app)" = "10001:10001" \
 && echo "ownership ok: uid/gid 10001 for /data /models /home/app"

# ---------------------------------------------------------------- 应用代码
# 只拷运行期需要的东西。镜像里**没有** tests/、scripts/、docs/——那些属于
# 开发机与 CI，不是运行期依赖（backend/app 里没有一处读仓库里的非 .py 文件）。
COPY --chown=app:app backend/app ./app

# ---------------------------------------------------------------- LibreOffice（可选）
# 放在所有 pip 层之后：WITH_OFFICE 是最后才可能变的参数，放前面会让一次
# 翻开关把 1.5GB 的依赖层也重建一遍。
# 不装时 .doc / .ppt 上传会收到一条"需要 LibreOffice"的 422，其余格式不受影响
# （见 backend/app/ingestion/legacy_office.py 的 install_hint）。
RUN if [ "$WITH_OFFICE" = "1" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends \
           libreoffice-writer libreoffice-impress fonts-noto-cjk \
      && rm -rf /var/lib/apt/lists/*; \
    fi

USER app

# 构建期断言（app 身份）：这才是"能不能写"的真探针。
# 上面那条 root 侧的断言只能证明属主位对不对，证明不了以 app 身份真能写（还有
# 挂载点、ACL、目录位这些变量）。这里以最终运行身份真的建文件再删掉：
#   - /data 是 SQLite（mindmaps.db）与 docling 的 HOME 缓存落点
#     （backend/app/mindmaps/repository.py 的 connect() 会 mkdir 父目录后建库）；
#   - /models 是 HF_HOME / DOCLING_CACHE_DIR 落点（首次解析 PDF/DOCX 时下载模型）。
# 只要有一条写不进，用户第一次保存导图或第一次解析 PDF 就会看到 "Permission denied"
# 并以为整个应用坏了——那种故障很难联想到这里。让它在**构建时**失败，日志里直接
# 就是哪条路径不可写。建完立刻删，同一个 RUN 里不留残层。
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

EXPOSE 8000

# 健康检查打 /api/health（backend/app/main.py:65-67，返回
# {"status":"ok","version":...}）。不装 curl 也能查：镜像里就有 python。
# compose 的 `depends_on: condition: service_healthy` 依赖这一条。
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

# ---------------------------------------------------------------- 启动
# --workers 1 是**硬要求**，不是性能调优：状态全在一个 SQLite 文件里
# （backend/app/mindmaps/repository.py 的 connect()，知识库/向量缓存/外部
# 检索缓存都复用它），多 worker 就是多个写者，会偶发 "database is locked"
# 的 500。2C2G 上多进程也吃不起内存。deploy/genai-mindmap.service 同样是
# --workers 1，两边必须一致。
#
# --host 0.0.0.0：容器网络里前端 nginx 要能连过来（线上形式是只听
# 127.0.0.1:8000、由同机 nginx 反代）。宿主机**不发布**这个端口，
# 见 docker-compose.yml 里的说明。
#
# --proxy-headers / --forwarded-allow-ips=*：X-Forwarded-For 由前面的 nginx
# 写，可信来源只有 compose 内网。uvicorn 的 _TrustedHosts 接受 "*"
# （site-packages/uvicorn/middleware/proxy_headers.py:109 always_trust）。
#
# 超时与 keepalive 照抄 systemd 单元：--timeout-keep-alive 65 比 nginx 的
# upstream keepalive 略长；--timeout-graceful-shutdown 15 给正在收尾的
# SSE 连接一个窗口（生成预算 RunBudget.max_seconds=600 由 stop_grace_period
# 兜住，见 compose）。
# 写成一行（不用 `\` 续行）是刻意的：exec 形式的 JSON 数组要靠 Dockerfile 解析器
# 先吃掉续行再交给 JSON 解析，本机没有 docker 验证这一步。一行就没有歧义。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*", "--no-server-header", "--timeout-keep-alive", "65", "--timeout-graceful-shutdown", "15"]
