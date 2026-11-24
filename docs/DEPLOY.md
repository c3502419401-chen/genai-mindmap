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
| `MINDMAP_LLM_BASE_URL` | — | 模型服务地址，须以 `http://` 或 `https://` 开头。**可被页面内的设置面板覆盖**（见下） |
| `MINDMAP_LLM_API_KEY` | — | 密钥。**可被页面内的设置面板覆盖**（落库为明文，与 `EnvironmentFile` 同一信任边界） |
| `MINDMAP_LLM_MODEL` | — | 模型名，由服务商决定。当前线上用 `z-ai/glm-5.3-flash`（经 `https://api.commandcode.ai/provider/v1`），先前用 `deepseek-flash`。**可被设置面板覆盖** |
| `MINDMAP_DB_PATH` | `backend/data/mindmaps.db` | SQLite 文件路径 |
| `MINDMAP_SOFFICE_CMD` | 自动探测 | 可选，LibreOffice 可执行文件路径 |
| `DASHSCOPE_API_KEY` | — | 语音转写用。阿里云百炼控制台创建，`sk-` 开头 |
| `MINDMAP_NLI_ENABLED` | 空（关） | 引用核验的第三层（语义蕴含打分）。需同时配 `MINDMAP_NLI_MODEL_DIR` |
| `MINDMAP_NLI_MODEL_DIR` | — | 含 `model.onnx` / `tokenizer.json` / `config.json` 的目录 |
| `MINDMAP_NLI_MIN_FREE_MB` | `300` | 可用内存低于此值就不加载模型（仅 Linux 可检测） |
| `MINDMAP_LIBRARY_SEARCH` | `1` | 生成时要不要从本地知识库捞材料。**线上按演示口径置 `0`**（纯展示，等记忆模块落地再开）；它不影响 `GET /api/knowledge/library` 只读入口 |
| `MINDMAP_LIBRARY_REMEMBER` | `1` | 生成时把用到的材料收进知识库（只认 `"0"` 为关）。与上面那个开关独立 |
| `MINDMAP_SETTINGS_ALLOW_PRIVATE` | 空（关） | 允不允许把模型地址指向本机 / 内网。关着时"测试连接"与生成都会拒（`probe._refuse()`），因为**这个面能用已存密钥当内网探针**。要连 Ollama / LM Studio / 同机自建端点就设 `1`；它同时放开本机与内网，没有更细的粒度。**云元数据地址（`169.254.169.254`、`metadata.google.internal`、`100.100.100.200` 等）这个开关也打不开**——那里没有模型，只有临时凭证 |
| `MINDMAP_FREE_TIER` | 空（关） | 走 Command Code coding plan 里当前标 `:free` 的模型（免费额度那一档用的就是它）。打开后，**只有借用部署方配置的人**（自己没填密钥）会换成免费模型，并自动带 `reasoning_effort=low`、把 `max_tokens` 夹到 4000–16000 之间（下限防止思考吃光额度，上限防止它按 6.5 万 token 的预算思考几十分钟）。默认关：打开它等于改变"这一次生成花谁的钱"，已有部署升级后不该静默变成又慢又受每日次数限制。详见 `docs/FREE-MODELS.md` |
| `MINDMAP_FREE_TIER_API_KEY` | 空（关） | **公共免费密钥**：配了它就有"每 IP 每天 3 次免费生成"，没配就完全没有额度这回事。它本身就是开关（不再有单独的 `MINDMAP_FREE_QUOTA`——那只会多出"配了钥匙忘开开关"的状态）。用的是**部署方那把钥匙**，所以只有"没有自己密钥"的人会走到；且地址也必须是部署方选的（`base_url_source` 是 `instance`/`env`），**调用方自己写的地址永远领不到这把钥匙**。详见 `docs/FREE-MODELS.md` 第 8 节 |
| `MINDMAP_FREE_QUOTA_LIMIT` | `3` | 每个 IP 每天的免费次数上限（1–100）。写错或越界退回 3 并留一条 warning |
| `TZ` | （系统默认） | **部署这台机器时建议显式设成 `Asia/Shanghai`**：免费额度按**本地日期**跨天重置，容器/systemd 默认是 UTC 的话"明天"会从北京时间早八点开始 |

**四个 `MINDMAP_LLM_*` 的优先级：当前身份自己的页面设置 > 本站共享设置 > 环境变量 > 未配置。** 回落是**逐字段**的——只改了模型名、地址与密钥仍从环境变量来。面板保存后立即生效（1 秒 TTL 的进程内缓存，按 owner 分键，单 worker 下等于即时；多 worker 时另一个进程最多滞后 1 秒）。`MINDMAP_LLM_MODE` **不进库**：它是部署方定的"这台机器要不要联网"，面板只显示、不能改。面板只接受 OpenAI 兼容接口（实际请求的是"地址 + `/chat/completions`"）。密钥只回打码值（`…abcd`），要清空必须显式声明。**打码值不是密钥**：把它原样 `PUT` 回去会被拒（400 `settings_api_key_masked`）而不是当成新密钥存下来——否则库里那把真密钥会被字面量顶掉，而接口照旧说"已配置"、之后每次调用都 401。这条对直接调接口的人尤其重要：界面上那个值只出现在 placeholder 里，正常点击不会提交它。

**模型配置按人分开存（BYOK）。** `model_settings` 的主键是 `owner`：`''` 是**本站共享配置**（游客免费额度用的就是它），`user:<id>` 是某个账号，`guest:<id>` 是某台浏览器的游客。**游客也能存自己的密钥**——他不想填而已，不是不许。回落链见上一条，中间那一档（共享配置）必须存在，否则刚注册、还没填密钥的人一条路都走不了；它只在你自己缺那个字段时生效，不然 BYOK 会被公共配置永久盖住。`api_key_source` 因此多一档 `instance`（借来的），与 `saved`（自己的）分开报——合并会让"清除自己那行、回到公共配置"变成不敢点的按钮。

**部署方 = 这台机器上第一个注册的账号**，没有 `MINDMAP_ADMIN` 环境变量。只有他能改共享那一行（`PUT` 带 `scope: "instance"`，别人 403 `settings_forbidden`），也只有他能看见它（`GET` 的 `instance` 字段，别人是 `null`）。判定按 `created_at`、`id` 兜底，登出不影响；他删号之前别把这件事忘了——台上只有一个账号时，那个账号就是部署方。老库（单行 `CHECK (id = 1)`）启动时自动迁移成 `owner = ''` 那一行，两段式可续、幂等，搬迁过程中进程被杀下次会补完。

**改地址之后保存会带一句 `notice`**："上次「测试连接」的结论对新地址不成立，建议现在点一次"。只提醒不阻拦——"先把地址填好、等供应商开白名单"是正常操作，强制先测通会把这条路堵死。同值重复保存、以及从"什么都没配"到"填了个地址"的首次配置都不提醒（没有旧结论可作废）。

**地址不许指向本机 / 内网 / 云元数据地址。** 这个面最贵的失误不是"填错"，是"被改写"：无鉴权时 `PUT` 换掉 `base_url`、再 `POST /model/test`（空 body 走回落生效值这条免费路径），已存密钥就随 `Authorization` 头送到了那个地址——攻击者既不需要知道密钥，也不需要读得到响应。所以现在有三道：门禁要求这一面**必须有身份**（`backend/app/auth/gate.py`，nginx 那层 Basic Auth 已弃用）；`backend/app/settings/probe.py::_refuse()` 在发请求**之前**拒掉本机、内网、链路本地、保留地址（`MINDMAP_SETTINGS_ALLOW_PRIVATE=1` 可放开，见上表）与云元数据地址（开关也打不开）；公网地址必须 `https://`（`http://` 会让密钥明文过中间每一跳；私网不查这一条，本机 Ollama 常开在 http）。它仍不是"防住内网"的完整方案（DNS rebinding、302 跳内网、公网域名解析到私网都挡不住），挡的是成本最低的那一步。

**密钥与地址绑定（`api_key_host`）——第二层，挡"已经改好地址"之后的安静形状。** 上面那些挡的是"填个地址就把密钥送出去"，发生在测试连接那一刻。但还有一个形状不经过测试连接：改完地址（这一步本来没有密钥出门），下一次**正常生成**就会带着库里那把密钥去访问新地址。所以 `model_settings` 多一列 `api_key_host`，记"这把密钥被哪台主机保存过"，`store.resolve()` 只在当前生效地址的主机名与锚点一致时才交出明文；不一致时 `api_key_source` 变成 `unbound`、`api_key` 为空、`configured` 变 `false`，界面上那一栏提示"重新粘贴一次"。

三个边界：① **只比主机名**，不比端口与路径——`/v1` 换 `/v2`、Ollama 11434 挪到 11435 都不该逼人重填，为这些变动弹提示只会训练出"看到提示就再粘一次"的习惯；换主机才意味着"换了一个会收到这把密钥的对方"。② 锚点**落库**而不是每次读时算，算的话它会跟着新地址一起漂，绑定等于不存在。③ 老库升级时该列补成空串，此时按那一行自己的 `base_url` 兜底——否则升级当天所有人的已存配置会一起被判成"未配置"。重新粘贴一次密钥就是唯一的恢复动作，`POST /model/test` 在这个状态下回 400 `settings_key_unbound` 并且**一个字节都不发**。

**免费档（`MINDMAP_FREE_TIER`）：模型名是运行时现问的，不写死。** 打开之后，`store.resolve()` 会问一次部署方那台端点的 `GET /models`，挑出当前 id 以 `:free` 结尾、且 `supported_endpoints` 里带 `/chat/completions` 的那些，按"上下文大的优先、同大按 id 升序"排第一个来用。**这份名单会变**——平台会新上架免费模型，也会把已有的下架，所以这里一个模型名都没硬编码；`docs/FREE-MODELS.md` 记着实测数据与坑。四条运维要点：① 拉一次列表要 20 秒以上，所以它是后台线程 + 1 小时 TTL 缓存，**永远不会出现在请求路径上**（冷启动第一次会报"免费档还没准备好"，点一次重试就好）；② 免费模型**也要密钥**（不带 key 直接 401），只是那次调用不收费，所以这一档用的是部署方那把钥匙，`model_source` 会报 `free`；③ 上游偶发 429 / 524，一次运行内会按候选序列换下一个免费模型，全试完才报错；④ **免费档单次生成要 4–6 分钟**（付费档约 100 秒的同一张图），`RunBudget.max_seconds` 默认 600 秒余量偏薄——把免费档当主路径前先确认它不是唯一开关，或把 `MINDMAP_LLM_MAX_TOKENS` 留在默认（免费档的 `max_tokens` 上限由代码夹在 16000；**往下是跟这个变量的**：把它设成 2000，免费档最后也只发 2000，只有往上调才顶不过 16000 这道夹子）。用户自己填了密钥就完全不进这一档。

**公共免费额度（`MINDMAP_FREE_TIER_API_KEY`）：每 IP 每天 3 次，用完 429。** 配了这把钥匙，没有自己密钥的人就能借它生成，`api_key_source` 报 `public`。四件要知道的事：

1. **按 IP 认人**（`X-Real-IP` 优先，其次 `X-Forwarded-For` 的最后一跳，最后 socket 对端）。按 cookie 的话清一下就重置，按账号的话注册一个就重置——两种都让"限 3 次"变成空话。代价是同一 NAT 后面的人共享一个池，**校园网就是这个情况**。

   **这一条成立的前提是"后端不能被直连"**：`X-Real-IP` 是请求方自己写的头，两份 nginx 配置都写 `X-Real-IP $remote_addr` 覆盖它——默认部署下客户端改不了。但 `deploy/genai-mindmap.service` 把 uvicorn 绑在 `127.0.0.1`、compose 只 `expose` 不发布端口，这两件事**不是安全余量，是这个机制的组成部分**。如果有人把 8000 端口直接放出去（或者外层反代忘了覆盖这个头），每次换一个 `X-Real-IP` 就能把"每天 3 次"变成无限（审查实测：同 IP 第 3 次 429，换头之后全部 200）。登录限速不受影响——它按 IP 与账号两个桶取严，伪造这个头只能刷新 IP 桶，动不了账号桶。
2. **扣在开流之前**（`runs.begin` 之前），所以并发请求不会都看到"还剩 1 次"；代价是失败那次可能白扣一格，所以收尾时"最后没有候选"会 `refund()` 回去——**半棵树交付算有产出、照扣**（那一次真的调过模型）。
3. **用完是 429 `free_quota_exhausted` + `Retry-After`（到明天零点的秒数）**，不开流、不建 `runs` 行、不发任何模型调用。界面上的失败卡片会把重试按死，并同时给出"明天恢复"与"去填自己的密钥"两条路。
4. **降级方向是放开**：额度表读写失败当作还有额度。反过来会把所有人关在门外，而攻击者没法让表坏掉（他得先拿写锁）。

**地址必须是部署方选的，这是这一档的安全前提。** 判据是 `base_url_source ∈ {instance, env}` **且** `api_key_source ∈ {instance, env, unset}`。只看"没有自己的密钥"是不行的：那样任何人 `PUT /api/settings/model {"base_url": "https://attacker.example/v1"}`（**不带密钥**，连 `settings_key_unbound` 都不经过）就能把公共钥匙领走——这正是 P0-1 修过的形状，而且只要一步。用户自己写地址却不填密钥时**不给他公共钥匙**，`configured` 为假、界面说"去填密钥"，这是加公共额度之前的行为。

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

知识库（`backend/data/mindmaps.db`，可用 `MINDMAP_DB_PATH` 指定位置）随使用增长，但入库之后会按 90 天 / 200 份自动淘汰（触发条件与成本见 `docs/LIBRARY-MEASUREMENTS.md`），稳态在 10 MB 以内。仍然建议把该分区剩余空间纳入监控（例如低于 500 MB 告警）：磁盘写满时 SQLite 报 `database or disk is full`，会连带影响生成。

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
export MINDMAP_LLM_BASE_URL=https://api.commandcode.ai/provider/v1
export MINDMAP_LLM_MODEL=z-ai/glm-5.3-flash
export MINDMAP_LLM_API_KEY=<your-key>
# 语音（可选，不配则语音上传返回 422，其余功能正常）
export DASHSCOPE_API_KEY=<your-dashscope-key>
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 换模型服务商

代码里没有写死任何一家服务商：请求只依赖 OpenAI 兼容的
`POST {MINDMAP_LLM_BASE_URL}/chat/completions`，改三个环境变量即可换家。端点必须满足
三条，缺一条会在运行时报错、而报错位置离原因很远，所以先在这里核：

| 能力 | 怎么用 | 缺了会怎样 |
|---|---|---|
| `tools` / `tool_calls` | agent 的每一步都是工具调用，模型必须**真的**调 | 只回一段文字，事件流只有一个 `done`，页面提示"模型这次没有产出导图"，`done.text` 里是它当时的原话 |
| `stream: true` | 生成与展开都走 SSE，边出边画 | 退回一次性请求，首屏要等到全部生成完 |
| 足够大的 `max_tokens` | 默认要 65536 | 服务端 400 会写 `Range of max_tokens should be [1, N]` 或 `supports at most N`，后端**解析出 N 并自动降一次**（只在进程内存里，不落盘）；两条文案都对不上才抛错 |

换家之后先跑一遍 `docs/DEPLOY.md` 第 7.6 节的自检，再在页面上各点一次纯主题生成与带材料生成——
后者要能看到节点上的引用角标，否则多半是材料没被识别成 `parsed`（诊断里会有 `SOURCE_NOT_PARSED`）。

检查：

```bash
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8000/api/ingestion/formats
```

## 9. ECS 上线（Alibaba Cloud Linux 3，2C2G）

目标形态：**nginx 发前端静态文件、`/api/` 反代到本机 uvicorn**，同源，因此不存在 CORS；后端只听 `127.0.0.1:8000`，公网只开 80/443。整个过程约 20–30 分钟，其中装 Python 依赖占大头。

### 9.0 先做三件外部的事

1. 域名加一条 A 记录指向 ECS 公网 IP（`ping 域名` 要返回该 IP）。
2. 安全组放行 22、80、443；**不要**放行 8000——后端不该暴露。
3. 想好用哪份模型密钥（`MINDMAP_LLM_*`）与语音密钥（`DASHSCOPE_API_KEY`，可不配）。

### 9.1 把代码放上去

**分支约定（2026-11-11 起）：`main` 是生产分支**，ECS 上跑的、对外演示的就是它；`dev` 是集成分支，日常开发合到 `dev`，里程碑时才 `dev` → `main` 并打 tag。所以生产部署直接 clone 默认分支即可：

```bash
sudo dnf install -y git
git clone https://github.com/RPeGio/genai-mindmap.git   # 默认就是 main
cd genai-mindmap
ls deploy/          # 能看到 install.sh / nginx.conf 才算对
git describe --tags # 本方案第一次上线后才会打第一个部署 tag；现在还看不到别当成失败
```

**国内 ECS 上 `git clone` 大概率连不上 GitHub**（`OpenSSL SSL_connect: SSL_ERROR_SYSCALL` 或 `Connection timed out`，22 与 443 都一样），这不是仓库权限问题，是出口被拦。三条出路，按省事程度排：

1. **本机打 tar 包再 scp**（最稳，不依赖服务器出网）：

   ```bash
   # 本机：从要部署的 tag 导出（自动排除 .git / node_modules）
   git archive --format=tar.gz -o genai-mindmap.tar.gz v0.5.0
   scp genai-mindmap.tar.gz root@<ECS_IP>:/root/

   # 服务器
   mkdir -p /root/genai-mindmap && tar -xzf /root/genai-mindmap.tar.gz -C /root/genai-mindmap
   cd /root/genai-mindmap && ls deploy/    # 有 install.sh 就对了
   ```

   注意 `git archive` 只导出被 git 跟踪的文件，`deploy/` 在仓库里，所以肯定在包里；`frontend/dist` 不在仓库里，按 9.4 单独传。

2. **在服务器上挂代理**再 clone：`export https_proxy=http://<你的代理>:<端口>`，前提是这台机器能连到代理。

3. **走镜像前缀**（例如 `https://ghfast.top/https://github.com/RPeGio/genai-mindmap.git`）。镜像可用性随时会变，只当临时手段；`git remote set-url origin` 记得改回官方地址。

想先把还没进 `main` 的改动放上去试用，再显式 `-b dev`（或某个特性分支）；试完记得切回 `main`，别让机器长期停在集成分支上——否则"线上跑的是什么"会说不清。生产机上不建议 `git switch` 来回切：`/opt` 里的代码是 `install.sh` 解包过去的，不是 git 工作区（回滚见第 10 节）。

### 9.2 一条命令装完

```bash
sudo bash deploy/install.sh --domain mindmap.example.com --version v0.4.7
# 需要 .doc/.ppt（LibreOffice，+1.5GB）时加 --with-office
# 想让脚本顺便签证书时加 --with-certbot
```

`--version` 只影响一处：它把 `MINDMAP_VERSION` 写进 `/etc/genai-mindmap/env`，后端
`/api/health` 会原样返回，页面标题旁的小徽章显示的就是这个值。用 `git archive` 打的包里
没有 `.git`，脚本自己 `git describe` 拿不到 tag，所以**打包/部署时要显式传它**；不传则
回落到 `/opt/genai-mindmap/DEPLOYED_VERSION`，再回落到 `unknown`。看到徽章写着 `unknown`
就说明这次没传版本号，而不是部署失败。

脚本做六件事，都可重复执行：

| 步骤 | 内容 |
|---|---|
| 1 | `dnf` 装 nginx / sqlite / 编译基础件 |
| 2 | 没有 swap 就加 2G（2G 内存解析 PDF、装 torch 都要余量） |
| 3 | 建 `genai` 系统用户；代码到 `/opt/genai-mindmap`；数据到 `/var/lib/genai-mindmap`；`/etc/genai-mindmap/env` 权限 0640；覆盖前把当前代码快照到 `/var/lib/genai-mindmap/releases/`（保留 5 份，回滚用） |
| 4 | 用 `uv` 装独立 CPython 3.12（避开系统 python3.6）并建 venv、装 `backend/requirements.txt`（默认走阿里云 PyPI 镜像） |
| 5 | 装 systemd 单元并启动（只 1 个 worker，理由见下） |
| 6 | 装 nginx 站点配置（IPv6 关闭时只监听 v4）、删除上一代遗留的 Basic Auth 口令文件（账号体系已接管鉴权）、放行 SELinux 反代与静态目录上下文、装每日备份 cron |

重复执行是安全的。只有一处**故意不覆盖**：`/etc/genai-mindmap/env`（密钥）。

### 9.3 填密钥（运行时注入，不进仓库）

```bash
sudo vi /etc/genai-mindmap/env     # MINDMAP_LLM_BASE_URL / API_KEY / MODEL
sudo systemctl restart genai-mindmap
sudo journalctl -u genai-mindmap -n 30 --no-pager
```

`MINDMAP_LLM_MODE` 默认写的是 `model`；只想给人看界面、不烧额度时可以改 `demo`（PDF 视觉提取会退化成文本层）。

### 9.4 发布前端

2C2G 上装 node 再打包要拉几百 MB 依赖，不如本机构建：

```powershell
cd frontend
npm run build
# 发布前把服务器上现有 dist 留一份，回滚前端时直接换回来
ssh root@<ECS_IP> "cd /opt/genai-mindmap/frontend && rm -rf dist.prev && cp -a dist dist.prev"
scp -r dist/* root@<ECS_IP>:/opt/genai-mindmap/frontend/dist/
```

```bash
ssh root@<ECS_IP> "chmod -R a+rX /opt/genai-mindmap/frontend/dist && restorecon -R /opt/genai-mindmap/frontend/dist && nginx -s reload"
```

`restorecon` 这一步别省：`scp` 上来的文件带着父目录的 SELinux 类型，nginx 读不到时只报 403。

`dist` 里没有 `?sample=` 的示例代码——那是 `import.meta.env.DEV` 分支，构建时已被摇掉。

### 9.5 签 HTTPS

```bash
sudo dnf install -y epel-release
sudo dnf install -y certbot python3-certbot-nginx
# 只用 certonly --webroot：签发与配置分开，重跑 install.sh 不会把 HTTPS 冲掉
sudo certbot certonly --webroot -w /var/www/html -d mindmap.example.com
sudo bash deploy/install.sh --domain mindmap.example.com   # 检测到证书后自动写 443 块并让 80 跳转
```

`certbot renew` 由 `certbot-renew.timer` 负责续期；续期只换证书文件，nginx 配置不需要动。

没有域名时只能 `http://IP`：能用，但浏览器的录音、剪贴板 API 在非安全上下文会被拦，演示语音与"复制全文"会失败。

### 9.6 上线自检

```bash
curl -s http://127.0.0.1:8000/api/health                  # 后端活着
curl -s https://mindmap.example.com/api/health # 反代通（免身份，门禁放行它）
sudo ss -lntup | grep -E ':(80|443|8000)'                 # 8000 只能听 127.0.0.1
```

浏览器里再过一遍：打开页面 → 生成一张导图（要能看到节点**逐步**冒出来，不是转圈转到底）→ 双击改文字 → 保存 → 刷新后能打开 → 上传一份 PDF → 导出 Markdown。

### 9.7 日常运维

```bash
sudo systemctl status genai-mindmap
sudo journalctl -u genai-mindmap -f            # 日志（含 request_id）
sudo systemctl restart genai-mindmap           # 改了 /etc/genai-mindmap/env 之后
sudo tail -n 50 /var/log/nginx/error.log
sudo /usr/local/bin/genai-mindmap-backup       # 手动备份，产物在 /var/backups/genai-mindmap
```

恢复备份：`gunzip -c /var/backups/genai-mindmap/mindmaps-<时间>.db.gz > /var/lib/genai-mindmap/mindmaps.db`，然后 `chown genai:genai` 并重启服务。

**账号在页面里注册，不用命令行。** 打开站点 → 抽屉里的「账号」区 → 注册。**第一个注册的账号就是部署方**（见第 3 节第 5 条），他能改全站共享的模型配置。nginx 那层 Basic Auth 已弃用，`/etc/nginx/.htpasswd-mindmap` 会被重跑的 `install.sh` 删掉——它现在是一份没人消费的口令，留着只会让人以为门外还有一道锁。

**注册是开放的。** 门禁只挡匿名，游客身份谁都能领（每天 3 次免费生成），注册也不需要邀请码。**不想让陌生人进来就在外层限**：安全组只放行自己的 IP、宿主机再架一层 Basic Auth/SSO、或者干脆先不暴露公网。这是"门禁"与"谁能进来"两件事的分工——应用负责前者。

**知识库是共用的**。服务端是单用户单库的设计（见 `docs/PLAN.md` 的 F12）：任何有身份的账号打开「知识库」抽屉，都能列出别人的材料、并读到每份正文的前 4000 字。只想自己用就别开放注册；真要多人共用，先想清楚这些材料能不能互相看见。

**但记忆与偏好是分开的**。上面那条"共用"是**材料库**的口径，别顺手外推：账号体系
落地之后，`memory_items`（从会话里总结出的"关于这个人的推断"）、`user_prefs`（含
`note.*` 这段唯一会进提示词的自由文本）、`style_profile`（从历史图里统计出的作图习惯）
**都按身份隔离**，别人列不到、删不掉，也不会被拼进你的生成提示词。导图仍是读共享、
写限主（协作要能看别人的图，改只能改自己的）。一张表在 `docs/API.md` 的「数据隔离」
一节。

**后端不许被直连暴露**。`X-Real-IP` / `X-Forwarded-For` 是请求方自己写的头，免费额度
按 IP 计数、登录限速按 IP 建桶，两者都只信任"我们自己那一跳写进来的地址"。两份 nginx
配置都覆盖这两个头，所以默认部署是安全的；但**把 8000 端口直接放出去就等于关掉这道
限制**（审查实测：换头即可无限刷额度）。同理，`deploy/genai-mindmap.service` 的
`--host 127.0.0.1` 与 compose 的 `expose`（不 `ports`）不是"顺手写得保守"，是这套
计数的前提。

**重启会等正在跑的生成**：单元里 `TimeoutStopSec=660`（生成预算是 600 秒），所以有长请求在跑时 `systemctl restart` 可能等到十来分钟。演示前别在有人用的时候重启；确实要立刻停，用
`sudo systemctl kill -s SIGKILL genai-mindmap && sudo systemctl start genai-mindmap`（代价是那条请求直接断，前端看到断流）。

### 9.8 故障对照表

| 现象 | 多半是 |
|---|---|
| 502 Bad Gateway | 后端没起来或崩了：`systemctl status genai-mindmap`、`journalctl -u genai-mindmap -n 50` |
| 服务起不来，`status` 里写 `226/NAMESPACE` | 单元里 `ReadWritePaths` 指向了不存在的目录（`/var/log/genai-mindmap`）。现在改成 systemd 的 `LogsDirectory` 自己建，旧单元升级时重跑一次 `install.sh` 即可 |
| 服务起不来，journal 里 `Failed to load environment files` | 这是**故意的**：单元里的 `EnvironmentFile` 没有 `-` 前缀，`/etc/genai-mindmap/env` 不在就拒绝启动。填好该文件（至少 `MINDMAP_DB_PATH` 与 `MINDMAP_LLM_*`）再启动 |
| 403 Forbidden（页面本身） | 两层：文件权限（`chmod -R a+rX .../dist`）与 SELinux 上下文（`/opt` 下默认是 `usr_t`，nginx 读不了；`install.sh` 会 `semanage fcontext -t httpd_sys_content_t` + `restorecon`，手工传 dist 之后要再跑一次） |
| nginx 起不来，error.log 里 `bind() to [::]:80 failed` | 机器把 IPv6 关了，配置里却还有 `listen [::]:80`。`install.sh` 现在按 `/proc/sys/net/ipv6/conf/all/disable_ipv6` 决定写不写这行——注意 `nginx -t` 只查语法，**查不出 bind 失败**，必须是 `systemctl restart nginx` 之后看 `status` 才算数 |
| 重跑过安装脚本后 HTTPS 没了 | 旧版脚本用 `certbot --nginx` 把 443 写进 `conf.d`，又被下一次 `install.sh` 用模板覆盖。现在用 `certbot certonly --webroot` 只签发，443 由 `deploy/nginx-tls.conf` 渲染，重跑不会丢 |
| 413 Request Entity Too Large | 上传超过 nginx 的 `client_max_body_size 24m` |
| 504 但后端日志还在打印 | 生成超过 `proxy_read_timeout`；已设 900s，仍超时说明 `MINDMAP_LLM_*` 指向的模型太慢 |
| 点了生成一直转圈、最后一次性出图 | SSE 被缓冲：确认 `location /api/` 里有 `proxy_buffering off` |
| 偶发 `database is locked` | 起了多个 worker 或多个 uvicorn 实例；SQLite 只能一个写者 |
| 上传 PDF 返回 422 说需要密钥 | `MINDMAP_LLM_MODE=demo`，或 `MINDMAP_LLM_*` 没填全 |
| 页面上 Excel/Word 上传 422 | 没装 `--with-office` 时 `.doc/.ppt` 不可用（`.docx/.xlsx` 不需要 LibreOffice） |
| `git clone` 报 `SSL_ERROR_SYSCALL` / 443 超时 | 服务器出口连不上 GitHub，见 9.1 的三条出路（tar + scp 最省事） |
| `uv python install` 或 `uv pip install` 卡住 | 前者从 GitHub 取解释器（用 `UV_PYTHON_INSTALL_MIRROR`），后者默认已走阿里云镜像（`UV_DEFAULT_INDEX` 可改） |

### 9.9 内存与并发（2C2G 的实话）

- 空闲常驻约 150–250MB；`docling` 是懒加载，第一次上传 docx/pptx/xlsx/html/md 时会冲高到 600MB–1GB，之后常驻。
- PDF 逐页视觉识别会并发 8 个请求，但内存主要花在 SSE 连接与图片缓冲上；**同时演示的人数控制在 2–3 人**。
- 想要更多余量：升到 4G，或把 `MINDMAP_LLM_MODE` 保持 `model` 但避开大 PDF。

## 10. 更新已部署的版本

生产机上永远跟 `main`（默认分支），不要跟 `dev`。注意 **`/opt/genai-mindmap` 不是 git 工作区**，它是 `install.sh` 解包过去的一份拷贝——所以在生产机上 `git switch` 只改源码目录，改不到真正在跑的那份代码。正确的做法是"在新源码上跑脚本，脚本负责同步到 `/opt`"：

```bash
cd ~/genai-mindmap
git fetch --tags && git switch main && git pull --ff-only
VERSION=$(git describe --tags)      # 记下这次发的版本号，出问题时好回滚
sudo bash deploy/install.sh --domain mindmap.example.com --version "$VERSION"   # 幂等；覆盖 /opt 之前先把旧代码快照到 releases/
sudo systemctl restart genai-mindmap
sudo systemctl status genai-mindmap --no-pager
```

发完刷新页面，标题旁徽章应变成刚传的 `$VERSION`；没变就是前端没重新发布（9.4）。

`install.sh` 第 5 步会 `systemctl restart` 并轮询 `/api/health`：**旧版只做 `enable --now`，
服务已在跑时它什么都不做**，于是 `/opt` 换成新代码、进程里还是旧代码，健康检查一直报旧版本
（v0.4.7 第一次部署就是这么卡住的）。现在起不来会直接把 journal 最后 20 行打出来。

两条版本来源要说清：`/opt/genai-mindmap/DEPLOYED_VERSION` 是脚本从源码目录 `git describe --tags`
自动写的（tar 包里没有 `.git` 时它是空的），`--version` 写的是 env 里的 `MINDMAP_VERSION`。
不带 `--version` 重跑时会退回读 `DEPLOYED_VERSION`，再退回 `unknown`——所以在 tar 部署的机器上
**每次都传 `--version`**，否则徽章会停在 `unknown`。

前端有改动时再走一遍 9.4。改了契约（`backend/app/contracts/`）的那次更新，记得本机先 `pytest` 与 `npm test`——CI 绿了再发。

某个 tag 要不要发布，也可以在服务器上直接看：`cat /opt/genai-mindmap/DEPLOYED_VERSION` 就是当前这台的版本。

### 回滚

代码回滚走快照（每次 `install.sh` 覆盖前存的那份，保留最近 5 份）：

```bash
ls -1t /var/lib/genai-mindmap/releases/          # 最新一份在最上
sudo tar -xzf /var/lib/genai-mindmap/releases/20261111-101500.tar.gz -C /opt/genai-mindmap
sudo chown -R genai:genai /opt/genai-mindmap
sudo systemctl restart genai-mindmap
sudo systemctl status genai-mindmap --no-pager
```

前端也要跟着回：9.4 发布时如果按建议把旧目录留成 `dist.prev`，`cd /opt/genai-mindmap/frontend && rm -rf dist && mv dist.prev dist`，再 `restorecon -R dist`。

⚠️ 契约回滚有数据风险：库里一旦有 v2 记录（`schema_version: 2`），退回 v1 的读入路径会被 `extra="forbid"` 拒掉。真要回滚，先备份 `mindmaps.db`，并确认该版本能读现有数据。

## 11. 出网受限的 ECS：PyPI 与模型源

有的实例能 `dnf install`（走的是阿里云内网 repo），却连不上 `mirrors.aliyun.com`、`astral.sh`、GitHub——公网出口被安全组或网络策略收紧了。表现是 pip 一直 `Read timed out`、`uv` 报 `curl: (35)`。

先分三步确认出口到底通不通：

```bash
curl -sI --max-time 8 https://www.baidu.com | head -1                 # 公网一般站点
curl -sI --max-time 8 http://mirrors.cloud.aliyuncs.com/pypi/simple/ | head -1  # ECS 内网镜像
getent hosts mirrors.aliyun.com mirrors.cloud.aliyuncs.com
ip route; cat /etc/resolv.conf
```

- **只有内网镜像通**：把 PyPI 指向 `http://mirrors.cloud.aliyuncs.com/pypi/simple`（内网、不计公网流量）。`deploy/setup-python.sh` 已经按"公网阿里云 → 内网 → 官方"的顺序自动尝试，不用手改。
- **全都不通**：去控制台看安全组**出方向**是否还留着"允许全部"，以及实例是否真的绑了公网 IP（只做端口映射、没有默认路由的实例发不出去包）。
- **DNS 解析不出来**：`/etc/resolv.conf` 改成 `nameserver 100.100.2.136` 与 `100.100.2.138`（阿里云内网 DNS），再试。

公网彻底不通时，两件事要单独安排：

1. **依赖包**：本机 `pip download -r backend/requirements.txt -d wheels/`（Windows 上要加 `--platform manylinux2014_x86_64 --only-binary=:all:`），传上去 `pip install --no-index --find-links wheels/ -r requirements.txt`。
2. **docling 模型**：首次解析 docx/pptx/pdf 会去 HuggingFace 取版面模型。公网不通就用 `HF_ENDPOINT` 指到可达镜像，或在能上网的机器上把 `~/.cache/docling` 打包带过去，解到 `/var/lib/genai-mindmap/.cache/docling`（服务单元的 `HOME` 就指向那里）。⚠️ 这条没做过端到端验证，纯文本生成与思维导图主流程不受影响，受影响的是文档/PDF 上传。

## 12. 自动化部署（GitHub Actions + 自托管 runner，零入方向端口）

手动传 tar 的问题不是麻烦，而是**动作没有记录**：谁传的、传的哪个 tag、有没有漏传前端
dist，全靠人记。`.github/workflows/deploy.yml` 把这件事交给 CI：**打 tag 就发布**，发布
完用 `/api/health` 的版本号自检，对不上直接让 job 红。

### 12.1 为什么是自托管 runner，而不是 Actions 直连 SSH

最初的版本是"GitHub 托管机 scp 到 ECS"，代价是**必须把 22 端口对 `0.0.0.0/0` 放开**——
GitHub 托管 runner 的出口 IP 是一组不断变化的大网段（`https://api.github.com/meta` 的
`actions` 字段），阿里云安全组装不下，只能全开。一台挂着 root 部署密钥的公网 SSH，是
这套部署里最大的敞口，而它换来的只是"不用手动传文件"。

现在改成：ECS 上跑一个 **self-hosted runner**，由它主动连 GitHub 取任务（出站 443），
所以 **入方向一个端口都不用开**：

| | 旧方案（Actions → SSH） | 现在（self-hosted runner） |
|---|---|---|
| 入方向端口 | 22 对 `0.0.0.0/0` | 无 |
| 凭据 | 私钥存在 GitHub secret | 注册 token（一次性，装完即弃） |
| 构建在哪 | GitHub 托管机 | GitHub 托管机（runner 只装包，不构建） |
| 谁能触发 | 有仓库写权限的人打 tag | 同左 |
| runner 权限 | — | 普通用户 `ghrunner`，只能 sudo 一个 root 拥有的脚本 |

构建仍然放在 GitHub 托管机上（`ubuntu-latest` 跑 `npm ci && npm run build`），因为 2C2G
的内存跑不起前端构建；runner 只做两件事：下载 artifact、调用安装脚本。少一个 node 环境，
也少一份"服务器上装的 node 版本和本地不一样"的差异。

### 12.2 一次性配置

**第一步：在服务器上装 runner。** 先在本地拿一个注册 token（有效期一小时，装完就没用了）：

```powershell
gh api -X POST repos/RPeGio/genai-mindmap/actions/runners/registration-token --jq .token
```

然后在服务器的 Workbench 里，用**新版本的源码包**跑（`install-runner.sh` 需要
`/usr/local/sbin/genai-cd-apply` 已存在，这个文件由 `install.sh` 安装）：

```bash
mkdir -p /root/genai-mindmap-src
tar -xzf /root/genai-mindmap-vX.Y.Z.tar.gz -C /root/genai-mindmap-src
cd /root/genai-mindmap-src
sudo bash deploy/install.sh --domain genai-mindmap.rpegio.top --version vX.Y.Z
sudo bash deploy/install-runner.sh --token <上一步拿到的 token>
```

`install-runner.sh` 会：建 `ghrunner` 用户 → 从 `api.github.com` 取 runner 最新版本 →
下载解包 → 以标签 `genai-ecs` 注册 → 装成 systemd 服务 → 写
`/etc/sudoers.d/genai-cd`（**只有一行**：`ghrunner` 可免密执行 `/usr/local/sbin/genai-cd-apply`）。

脚本末尾会打印状态。确认 runner 在线：

```bash
cd ~ghrunner/actions-runner && sudo -u ghrunner ./svc.sh status
gh api repos/RPeGio/genai-mindmap/actions/runners --jq '.runners[] | "\(.name) \(.status)"'
```

**第二步：打一个 tag 试跑。** 见 12.3。

**第三步：收紧入口。** job 跑绿之后，这台机器就再也不需要入方向 SSH 了：

1. 安全组删掉 22 的规则（只留 80/443）；Workbench 走的是阿里云自己的通道，不受影响。
2. 删掉 `~/.ssh/authorized_keys` 里的部署公钥（如果按旧方案加过）。
3. 以前写过 `ECS_HOST` / `ECS_USER` / `ECS_SSH_KEY` / `ECS_PORT` 四个 secret 的，一并删掉
   （`gh secret delete ECS_SSH_KEY` 之类）；现在只用一个仓库变量 `ECS_DOMAIN`（可选，
   缺省 `genai-mindmap.rpegio.top`）。

### 12.3 日常发布

**tag 命名：纯版本号，不带后缀。** 早期几个 tag 叫 `v0.4.9-deploy`（那阵子既有前端发布又有部署，
想用后缀区分），后来发现后缀没有区分到任何东西——一个 tag 就是一次发布，两种都发。两种都
能部署（`cd-apply.sh` 只要求 `[A-Za-z0-9._-]`），但**新 tag 一律用纯版本号**，否则
`git tag --sort=v:refname` 排出来的顺序会是错的（`v0.4.9-deploy` 与 `v0.4.10` 谁新要人算）。

本地是 Windows PowerShell，一行一条，别用 `&&`（PowerShell 5.1 不支持）：

```powershell
git switch main
git pull --ff-only
git tag -a v0.5.0 -m "release: v0.5.0（这次改了什么）"
git push origin v0.5.0            # ← 这一步触发部署
gh run watch                      # 看着它跑完（Ctrl+C 只退出观察，不会取消 job）
```

两个 job 依次做：

1. `build`（GitHub 托管机）：`npm ci && npm run build` 出 dist → `git archive` 出源码包 →
   两个包作为 artifact 上传，保留 7 天。
2. `deploy`（ECS 上的 runner）：下载 artifact 到 `/tmp/genai-release` →
   `sudo /usr/local/sbin/genai-cd-apply --domain … --version <tag>`（解包到
   `~/genai-mindmap` → 跑 `install.sh` 同步 `/opt` 并重启 → 换前端 dist，旧的留成
   `dist.prev` → `nginx -t && reload` → 本机 `/api/health` 自检）→ 最后再从
   `https://<域名>/api/health` 比对一次版本号。

跑完刷新页面，标题旁徽章应显示这次的 tag；不显示或显示旧值，就看 job 日志里自检那一步
（版本不一致会让 job 直接失败，不会"绿着但没生效"）。

### 12.4 回滚

用 `workflow_dispatch` 指定一个旧 tag 重跑即可（tag 必须存在）：

```bash
gh workflow run deploy.yml -f version=v0.5.0
```

它走的是同一条路径：解包旧代码 → `install.sh`（快照当前版本）→ 换回旧 dist。前端与后端
会一起退到该 tag，不会出现"后端退了、前端还是新的"。要手工回滚见第 10 节。

### 12.5 排障

| 现象 | 原因与处置 |
|---|---|
| Actions 里 deploy job 一直 `Waiting for a runner` | runner 没在线：`cd ~ghrunner/actions-runner && sudo -u ghrunner ./svc.sh status`，再看 `journalctl -u 'actions.runner.*' -n 50` |
| runner 注册失败 `Invalid registration token` | token 过期（一小时），重新取一次 |
| job 里 `sudo -n` 报 `a password is required` | `/etc/sudoers.d/genai-cd` 没写上或路径不对；`sudo visudo -c` 检查，`sudo -u ghrunner sudo -n /usr/local/sbin/genai-cd-apply --help` 试一下 |
| `genai-cd-apply: command not found` | 没跑过带 `cd-apply.sh` 的新版 `install.sh`，先手工跑一次 |
| 自检报版本不一致 | 服务没重启成功：`systemctl status genai-mindmap`、`journalctl -u genai-mindmap -n 50`，常见原因是 `/etc/genai-mindmap/env` 里 `MINDMAP_VERSION` 没被覆盖（旧 `install.sh` 不认 `--version`） |
| 前端换了但页面还是旧的 | `dist.prev` 说明替换发生了；检查 nginx 的 `root` 是否指向 `/opt/genai-mindmap/frontend/dist`，以及 `restorecon` 是否执行（SELinux 开着时上下文不对会 403） |
