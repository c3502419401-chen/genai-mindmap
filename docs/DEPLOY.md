# 部署与依赖

## 1. Python 依赖

```bash
cd backend
pip install -r requirements.txt
```

八项全部是请求路径上的硬依赖，缺任何一项都会让对应功能不可用（`tests/test_dependencies.py` 会守住这一点）。语音转写复用 `httpx`，不需要额外 SDK，也不依赖 websockets 客户端。

## 2. 系统依赖：LibreOffice

**只有 `.doc` 和 `.ppt` 需要它**，其余格式没有它也能正常工作。

```bash
# Debian / Ubuntu
sudo apt install -y libreoffice-writer libreoffice-impress
# Windows
winget install TheDocumentFoundation.LibreOffice
```

占用约 **1.5 GB 磁盘**。装好后 `soffice` 通常在 PATH 里；不在的话用环境变量指定：

```bash
MINDMAP_SOFFICE_CMD=/usr/bin/libreoffice
MINDMAP_SOFFICE_CMD="C:\Program Files\LibreOffice\program\soffice.exe"   # Windows
```

没装时的行为：上传 `.doc` / `.ppt` 返回 422，错误文案里带上面那两条安装命令，**不会静默降级成空内容**。

## 3. 环境变量

全部经 `os.getenv` 读取，`.env` 文件**不会**自动加载，必须在启动前设入进程环境。样例见 `backend/.env.example`。

| 变量 | 默认 | 说明 |
|---|---|---|
| `MINDMAP_LLM_MODE` | `demo`（仅 PDF 视觉这条） | `demo` 不联网走本地占位；`model` 才调模型。**PDF 视觉提取只在 `model` 下工作**。agent 生成只认显式的 `demo`：不设这个变量不会自动降级成占位图，缺模型配置会直接报错 |
| `MINDMAP_LLM_BASE_URL` | — | 模型服务地址，须以 `http://` 或 `https://` 开头 |
| `MINDMAP_LLM_API_KEY` | — | 密钥，只从环境变量读 |
| `MINDMAP_LLM_MODEL` | — | 模型名，当前用 `deepseek-chat` |
| `MINDMAP_DB_PATH` | `backend/data/mindmaps.db` | SQLite 文件路径 |
| `MINDMAP_SOFFICE_CMD` | 自动探测 | 可选，LibreOffice 可执行文件路径 |
| `DASHSCOPE_API_KEY` | — | 语音转写用。阿里云百炼控制台创建，`sk-` 开头 |
| `MINDMAP_NLI_ENABLED` | 空（关） | 引用核验的第三层（语义蕴含打分）。需同时配 `MINDMAP_NLI_MODEL_DIR` |
| `MINDMAP_NLI_MODEL_DIR` | — | 含 `model.onnx` / `tokenizer.json` / `config.json` 的目录 |
| `MINDMAP_NLI_MIN_FREE_MB` | `300` | 可用内存低于此值就不加载模型（仅 Linux 可检测） |

## 4. 各格式的依赖与耗时

| 格式 | 依赖 | 耗时量级 | 说明 |
|---|---|---|---|
| `.pdf` | `pymupdf` + 模型 | 约 5–10 秒/页 | 逐页渲染后视觉识别；页数由上下文预算算出（`CONTEXT_TOKEN_LIMIT`），另有 300 页护栏 |
| `.docx` `.pptx` `.xlsx` `.html` `.md` | `docling` | 1–2 秒 | 读结构化格式，不加载版面模型 |
| `.doc` `.ppt` | `docling` + LibreOffice + 模型 | 约 10–60 秒 | 先转 PDF 再视觉识别；**耗时按转换后的页数算**，重排版后页数会增加 |
| `.xls` | `xlrd` | 0.1 秒 | 直接读单元格 |
| `.wav` | `httpx` + 阿里云百炼 | 约 0.04 秒/秒音频 | Qwen-Audio-ASR；≤300 秒单次，更长自动切片，带句级时间戳 |
| `.png` `.jpg` `.webp` `.gif` `.bmp` | `MINDMAP_LLM_*` | 约 5–8 秒/张 | 与 PDF 逐页共用多模态调用；`demo` 模式或未配密钥时返回 422 |

`demo` 模式下 PDF 会退回到文本层抽取（约 0.3 秒），扫描件则返回 422——因为视觉提取被模式检查拦下了。

## 5. 资源要求

| 项目 | 用量 |
|---|---|
| Python 依赖磁盘 | 约 700 MB（主要是 `docling` 及其传递依赖） |
| LibreOffice 磁盘 | 约 1.5 GB（仅旧格式需要） |
| 常驻内存 | 约 350 MB（`docling` 在首次解析后常驻） |
| 单次 PDF 解析峰值内存 | 约 70 MB（渲染与模型调用都在外部） |

2C2G 的机器上：不装 LibreOffice 可以跑，装了之后要留意 `.doc/.ppt` 转换时 `soffice` 进程会额外占 350–380 MB。

## 6. 语音输入（F07）

走阿里云百炼的 **Qwen-Audio-ASR**（`qwen-audio-3.1-asr-flash`），不在部署机上跑本地模型。

**为什么选这个接口**：一次同步调用同时返回文本、句级时间戳和词级时间戳。之前用过 ISI 的「一句话识别」，只有整段文本、没有句子边界，节点无法指回「这句话在录音的第几秒」。同批音频实测：

| | 一句话识别 | Qwen-Audio-ASR |
|---|---|---|
| 60 秒耗时 | 8.65 s | **3.1 s** |
| 60 秒成本 | 0.0035 元 | **0.00091 元** |
| 单次时长上限 | 60 秒 | **300 秒** |
| 句子/词时间戳 | 无 | **有** |

**不用本地模型的理由**：`sherpa-onnx` 的 paraformer-zh-small（78 MB）在 2C2G 上常驻 143 MB、短音频 RTF 0.013，但吃不下长输入——33.8 秒整段 CER 86.4%，60 秒直接返回空串并抛 onnxruntime Reshape 错误，只能处理 10 秒以内的片段。

### 开通与凭证

1. 打开[百炼控制台](https://modelstudio.console.aliyun.com) → 创建 **API Key**（`sk-` 开头）
2. 填进环境变量 `DASHSCOPE_API_KEY`

只需要这一个变量，缺了语音上传返回 422 并指明变量名。不需要 AccessKey，也不需要 Appkey——这套接口用 Bearer Token 鉴权。

老域名 `dashscope.aliyuncs.com` 可直接用，不必迁到带 Workspace ID 的新域名。

### 时长边界（实测）

- **≤300 秒**：正常。300 秒音频 12.4 秒返回，文本 1074 字，与期望值差 3 字
- **>300 秒**：返回 400 `AUDIO_DURATION_TOO_LONG`，明确报错不静默截断
- 超过 300 秒的音频按 300 秒切片逐段识别，段间时间戳由服务层按偏移平移

验证边界时**必须用内容多样的音频**。用同一句循环拼出来的长音频会让模型不停复读（实测 180 秒返回 11438 字，是期望值的 17 倍），那是测试素材的问题，不是接口的问题——这一点踩过坑，记在这里免得后来者重复误判。

### 前端录音必须走 HTTPS

浏览器只在**安全上下文**（HTTPS 或 localhost）下暴露 `navigator.mediaDevices`。用 `http://服务器IP:8080` 打开时这个对象是 `undefined`，录音按钮会报「当前页面无法访问麦克风：浏览器要求 HTTPS 或 localhost」。

**文档上传和 WAV 文件上传不受影响**，只有浏览器内直接录音需要 HTTPS。要么配证书，要么让用户改用「上传音频文件」入口。

### 成本

按 token 计费，北京地域原价输入 0.113 / 输出 0.382 美元每百万 token。实测 60 秒音频约 0.00091 元，8.5 秒约 0.00014 元。双声道且两路内容不同时会识别两路，成本翻倍；两路完全相同时后端只识别一路，不翻倍。

### 隐私

录音会上传到阿里云做转写。教育场景若涉及学生发言，需要在上传前取得同意，并确认数据留存策略。

## 7. 引用核验（可选，默认关闭）

节点引用做三层校验，前两层是纯代码、零成本：

1. **字段与来源关联**：`source_id` 必须出现在本次 `sources` 里，`page` 不能超出总页数，也不能指向解析失败的页。
2. **引文逐字匹配**：模型必须给出 `quote`（声称的原文片段），程序在来源原文里做子串查找。对不上就把整条引用丢掉。

第三层是**可选的语义蕴含打分**，需要额外部署一个 NLI 模型：

```bash
export MINDMAP_NLI_ENABLED=1
export MINDMAP_NLI_MODEL_DIR=/opt/models/nli   # 含 model.onnx + tokenizer.json + config.json
```

它补的是前两层管不住的一种情况：引文逐字对得上，但节点论断与原文语义相反（原文说「只在小数据集上验证」，节点写成「已在大规模数据上验证」）。

**这一层只产出诊断，绝不改变引用的有无。** 理由是它是概率模型，自己也会错；让概率模型推翻可复核的机械判据，等于把「诚实性来自判据的机械性」这条原则反过来用。判定矛盾时引用保留、同时给一条 warn 诊断，由人判断。

**默认关闭**，因为模型加载需要实打实的内存。实测两组（USS，空白进程基线约 7.5 MB）：

| 模型 | 文件 | 加载后增量 | 峰值 | 加载耗时 | 中文准确率 |
|---|---|---|---|---|---|
| `typeform/mobilebert-uncased-mnli` int8 | 25.7 MB | +83.5 MB | 91 MB | 9.9 s | **0/4** |
| `IDEA-CCNL/Erlangshen-Roberta-110M-NLI` int8 | 98.3 MB | **+140.3 MB** | 148 MB | 1.4 s | **6/6** |

`onnxruntime` 本身占 21 MB，且**卸载不掉**（`reset()` 只能丢掉模型引用，降到 36 MB）。真正的「关闭」只能是进程启动时就不 import。

**必须用中文模型。** 上表第一行是英文 MNLI 模型，对中文完全失效——「完全不能识别图像」对上「本系统可以识别图像」被判成 `entailment 0.52`，「数据库采用主从架构」这种无关内容也被判成 `entailment 0.65`。四条中文用例全错，包括明确的否定句。它只是在对词汇重叠打分，没有理解否定结构。换成 Erlangshen 后六条全对，矛盾判定的置信度在 0.76–0.98。

中文模型的 fp32 权重 390 MB，需要自己导 ONNX 并量化：

```bash
python -c "
from onnxruntime.quantization import quantize_dynamic, QuantType
quantize_dynamic('model.onnx', 'model.int8.onnx', weight_type=QuantType.QInt8)
"
```

导出后 `model.int8.onnx` 98.3 MB，配合 `tokenizer.json` 和 `config.json` 放进 `MINDMAP_NLI_MODEL_DIR`。注意 **`id2label` 顺序各模型不同**（mobilebert 是 `0=ENTAILMENT`，Erlangshen 是 `0=CONTRADICTION`），代码从 `config.json` 读，写死会整体错位。

2C2G 上的账：后端基线约 350 MB + NLI 140 MB ≈ 490 MB，语音转写峰值 250 MB 与之不同时发生，2 GB 内存够用。

## 8. 启动

```bash
cd backend
export MINDMAP_LLM_MODE=model
export MINDMAP_LLM_BASE_URL=https://api.deepseek.com/v1
export MINDMAP_LLM_MODEL=deepseek-chat
export MINDMAP_LLM_API_KEY=<your-key>
# 语音（可选，不配则语音上传返回 422，其余功能正常）
export DASHSCOPE_API_KEY=<your-dashscope-key>
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

检查：

```bash
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8000/api/ingestion/formats
```

## 7. ECS 上线（Alibaba Cloud Linux 3，2C2G）

目标形态：**nginx 发前端静态文件、`/api/` 反代到本机 uvicorn**，同源，因此不存在 CORS；后端只听 `127.0.0.1:8000`，公网只开 80/443。整个过程约 20–30 分钟，其中装 Python 依赖占大头。

### 7.0 先做三件外部的事

1. 域名加一条 A 记录指向 ECS 公网 IP（`ping 域名` 要返回该 IP）。
2. 安全组放行 22、80、443；**不要**放行 8000——后端不该暴露。
3. 想好用哪份模型密钥（`MINDMAP_LLM_*`）与语音密钥（`DASHSCOPE_API_KEY`，可不配）。

### 7.1 把代码放上去

**分支约定（2026-11-11 起）：`main` 是生产分支**，ECS 上跑的、对外演示的就是它；`dev` 是集成分支，日常开发合到 `dev`，里程碑时才 `dev` → `main` 并打 tag。所以生产部署直接 clone 默认分支即可：

```bash
sudo dnf install -y git
git clone https://github.com/RPeGio/genai-mindmap.git   # 默认就是 main
cd genai-mindmap
ls deploy/          # 能看到 install.sh / nginx.conf 才算对
git describe --tags # 本方案第一次上线后才会打第一个部署 tag；现在还看不到别当成失败
```

想先把还没进 `main` 的改动放上去试用，再显式 `-b dev`（或某个特性分支）；试完记得切回 `main`，别让机器长期停在集成分支上——否则"线上跑的是什么"会说不清。

拉错了不用重来：`git switch main && git pull`。

没有外网 git 访问时，本机 `git archive -o mindmap.tar.gz HEAD` 后 `scp` 上传，再 `tar -xf` 也行。

### 7.2 一条命令装完

```bash
sudo bash deploy/install.sh --domain mindmap.example.com
# 需要 .doc/.ppt（LibreOffice，+1.5GB）时加 --with-office
# 想让脚本顺便签证书时加 --with-certbot
```

脚本做六件事，都可重复执行：

| 步骤 | 内容 |
|---|---|
| 1 | `dnf` 装 nginx / htpasswd / sqlite / 编译基础件 |
| 2 | 没有 swap 就加 2G（2G 内存解析 PDF、装 torch 都要余量） |
| 3 | 建 `genai` 系统用户；代码到 `/opt/genai-mindmap`；数据到 `/var/lib/genai-mindmap`；`/etc/genai-mindmap/env` 权限 0640 |
| 4 | 用 `uv` 装独立 CPython 3.12（避开系统 python3.6）并建 venv、装 `backend/requirements.txt` |
| 5 | 装 systemd 单元并启动（只 1 个 worker，理由见下） |
| 6 | 装 nginx 站点配置、生成 Basic Auth 账号 `demo`（随机密码只打印一次）、放行 SELinux 反代、装每日备份 cron |

### 7.3 填密钥（运行时注入，不进仓库）

```bash
sudo vi /etc/genai-mindmap/env     # MINDMAP_LLM_BASE_URL / API_KEY / MODEL
sudo systemctl restart genai-mindmap
sudo journalctl -u genai-mindmap -n 30 --no-pager
```

`MINDMAP_LLM_MODE` 默认写的是 `model`；只想给人看界面、不烧额度时可以改 `demo`（PDF 视觉提取会退化成文本层）。

### 7.4 发布前端

2C2G 上装 node 再打包要拉几百 MB 依赖，不如本机构建：

```powershell
cd frontend
npm run build
scp -r dist/* root@<ECS_IP>:/opt/genai-mindmap/frontend/dist/
```

```bash
ssh root@<ECS_IP> "chmod -R a+rX /opt/genai-mindmap/frontend/dist && nginx -s reload"
```

`dist` 里没有 `?sample=` 的示例代码——那是 `import.meta.env.DEV` 分支，构建时已被摇掉。

### 7.5 签 HTTPS

```bash
sudo dnf install -y epel-release
sudo dnf install -y certbot python3-certbot-nginx
sudo certbot --nginx -d mindmap.example.com --redirect
```

没有域名时只能 `http://IP`：能用，但浏览器的录音、剪贴板 API 在非安全上下文会被拦，演示语音与"复制全文"会失败。

### 7.6 上线自检

```bash
curl -s http://127.0.0.1:8000/api/health                  # 后端活着
curl -su demo:<密码> https://mindmap.example.com/api/health # 反代通
sudo ss -lntup | grep -E ':(80|443|8000)'                 # 8000 只能听 127.0.0.1
```

浏览器里再过一遍：打开页面 → 生成一张导图（要能看到节点**逐步**冒出来，不是转圈转到底）→ 双击改文字 → 保存 → 刷新后能打开 → 上传一份 PDF → 导出 Markdown。

### 7.7 日常运维

```bash
sudo systemctl status genai-mindmap
sudo journalctl -u genai-mindmap -f            # 日志（含 request_id）
sudo systemctl restart genai-mindmap           # 改了 /etc/genai-mindmap/env 之后
sudo tail -n 50 /var/log/nginx/error.log
sudo /usr/local/bin/genai-mindmap-backup       # 手动备份，产物在 /var/backups/genai-mindmap
```

恢复备份：`gunzip -c /var/backups/genai-mindmap/mindmaps-<时间>.db.gz > /var/lib/genai-mindmap/mindmaps.db`，然后 `chown genai:genai` 并重启服务。

加演示账号：`sudo htpasswd /etc/nginx/.htpasswd-mindmap alice`。

### 7.8 故障对照表

| 现象 | 多半是 |
|---|---|
| 502 Bad Gateway | 后端没起来或崩了：`systemctl status genai-mindmap`、`journalctl -u genai-mindmap -n 50` |
| 403 Forbidden（页面本身） | `dist` 权限（`chmod -R a+rX`）或 SELinux 不让 nginx 读 `/opt` |
| 413 Request Entity Too Large | 上传超过 nginx 的 `client_max_body_size 24m` |
| 504 但后端日志还在打印 | 生成超过 `proxy_read_timeout`；已设 900s，仍超时说明 `MINDMAP_LLM_*` 指向的模型太慢 |
| 点了生成一直转圈、最后一次性出图 | SSE 被缓冲：确认 `location /api/` 里有 `proxy_buffering off` |
| 偶发 `database is locked` | 起了多个 worker 或多个 uvicorn 实例；SQLite 只能一个写者 |
| 上传 PDF 返回 422 说需要密钥 | `MINDMAP_LLM_MODE=demo`，或 `MINDMAP_LLM_*` 没填全 |
| 页面上 Excel/Word 上传 422 | 没装 `--with-office` 时 `.doc/.ppt` 不可用（`.docx/.xlsx` 不需要 LibreOffice） |

### 7.9 内存与并发（2C2G 的实话）

- 空闲常驻约 150–250MB；`docling` 是懒加载，第一次上传 docx/pptx/xlsx/html/md 时会冲高到 600MB–1GB，之后常驻。
- PDF 逐页视觉识别会并发 8 个请求，但内存主要花在 SSE 连接与图片缓冲上；**同时演示的人数控制在 2–3 人**。
- 想要更多余量：升到 4G，或把 `MINDMAP_LLM_MODE` 保持 `model` 但避开大 PDF。

## 8. 更新已部署的版本

生产机上永远跟 `main`（默认分支），不要跟 `dev`：

```bash
cd ~/genai-mindmap
git fetch --tags && git switch main && git pull --ff-only
git describe --tags            # 记下这次发的版本号，出问题时好回滚
sudo bash deploy/install.sh --domain mindmap.example.com   # 幂等，只更新代码与配置
sudo systemctl restart genai-mindmap
sudo systemctl status genai-mindmap --no-pager
```

前端有改动时再走一遍 7.4。改了契约（`backend/app/contracts/`）的那次更新，记得本机先 `pytest` 与 `npm test`——CI 绿了再发。

回滚到上一个 tag：

```bash
git fetch --tags
git switch --detach <要回退到的 tag>
# 关键一步：systemd 跑的是 /opt/genai-mindmap 里的代码，而只有 install.sh 会
# 把当前工作区同步过去（见脚本第 3 步的 tar）。只 switch + restart 的话，
# `git describe` 会显示旧 tag、机器上跑的却还是新代码——演练时会误判"回滚成功"。
sudo bash deploy/install.sh --domain mindmap.example.com
sudo systemctl restart genai-mindmap
sudo systemctl status genai-mindmap --no-pager
```

⚠️ 契约回滚有数据风险：库里一旦有 v2 记录（`schema_version: 2`），退回 v1 的读入路径会被 `extra="forbid"` 拒掉。真要回滚，先备份 `mindmaps.db`，并确认该版本能读现有数据。
