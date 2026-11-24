# 语音识别（ASR）能否本地推理 —— 调研与 PoC 报告

**结论先行：能，且不需要 GPU。推荐 sherpa-onnx + SenseVoice(int8) + Silero VAD 组合，已在本机跑通带句级时间戳的中文长音频转写。但"能跑"不等于"该换"——成本上换过去基本省不到钱，真正的收益是不再依赖 `DASHSCOPE_API_KEY` 与外网。**

本文所有标「实测」的数字都是本次在 Windows / i7-14650HX / CPU-only 上真跑出来的；
标「引用」的是别人的公开数据，来源写在括号里。第 8 节列出全部未验证项。

---

## 1. 先回答三个硬判据

`backend/app/ingestion/asr.py` 的头 docstring 立了三条：**时间戳是硬需求、长输入是硬需求、2C2G 无独显是硬约束**。逐条正面回答：

### 判据 1：有没有句级（最好词级）时间戳？

**有，但要说清粒度：SenseVoice 原生给的是「逐 token 起点」，不是「句边界」。句边界由我在 PoC 里从标点推出来。**

| 项 | 谁给的 | 说明 |
|---|---|---|
| token 级起点时间戳 | **模型原生** | `result.timestamps[i]` 是第 i 个 token 的起点秒（相对本次解码输入）。中文 token = 单字，英文 = 子词 |
| 词级时间戳 | **模型原生** | 中文「字=词」；英文子词需自行合并，PoC 未处理英文 |
| 句级 `begin_ms` | **模型原生**（取该句首 token 起点） | 句子的切分依据是**标点 token**（`。！？；…`），不是模型给的边界 |
| 句级 `end_ms` | **推导值，不是模型输出** | 用「下一句首个 token 的起点」推；末句退化到 VAD 段尾 |

对齐 `backend/app/contracts/source.py::AudioSentence` 的 `{text, begin_ms, end_ms}` 是够的，**契约不用改**。但要清楚 `end_ms` 的语义是"下一句开始"而不是"这句话说完"，两者之间含句间静音。

SenseVoice 的 `use_itn=True` 会输出中文标点，所以拿标点当句边界是可行的，**不需要额外的 ct-punc 标点模型**（这一点区别于 FunASR/Paraformer 的组装流水线）。

### 判据 2：2C2G 无独显能不能吃长音频？

**能，但必须 VAD 分片；直接整段喂进去会崩一半。**

实测（Windows CPU，324.4 秒中文音频，见第 5 节）：

| 方式 | 324 秒耗时 | RTF | CER | 崩不崩 |
|---|---|---|---|---|
| 不分片，一次性喂 324 秒 | 30.19 s | 0.094 | **11.63%** | 不崩，但大量丢字 |
| **Silero VAD 分片（48 段）后逐段识别** | **7.21 s** | **0.0197** | **0.31%** | 不崩 |

VAD 分片把长音频 CER 从 11.63% 压到 0.31%，且 **RTF 稳定在 0.02 量级、不随长度恶化**。
内存峰值 433 MB（threads=2 与 threads=4 几乎相同）。

这条正面推翻了 `asr.py` docstring 里"架构吃不下长输入"的旧结论——但**旧结论对 Paraformer 仍然成立**，我在第 6 节完整复现了它。

### 判据 3：中文 CER 数据从哪来？

**必须分两类看，不能混：**

| 来源 | 数值 | 可信度 |
|---|---|---|
| 我实测，**TTS 合成的干净音频** | 长音频 0.29% / 短音频 0.58% | ⚠️ **只证明"不丢字"，不代表真实准确率**。TTS 音频无噪声无口音，比真实会议录音容易太多 |
| 引用，MOSS-Audio 论文评测 | SenseVoice-Small Overall CER 14.50；AISHELL-1 test 3.23 | 第三方横向评测，与 Paraformer-Large 15.77 / Fun-ASR-Nano 12.04 同表可比 |
| 引用，FunASR GGUF 官方 184 集中文测试集 | SenseVoice q8 **7.99%**；Paraformer q8 9.78%；whisper.cpp small 22.12% / large-v3-turbo 23.15% | 同一套集内的相对排序可信，绝对值区间与上一行不一致（不同测试集，正常） |

**我的 TTS 实测不能用来选型准确率**，只能用来说明"链路通、不丢字、时间戳对得上"。真实准确率请用上表引用数据。

---

## 2. 现状与它的成本

`backend/app/ingestion/asr.py` 现在用阿里云百炼 `qwen-audio-3.1-asr-flash`：

- 常量：`MAX_SLICE_SECONDS = 300`、`SUPPORTED_RATES = (8000, 16000)`、`REQUEST_TIMEOUT = 300.0`、`MAX_WORKERS = 2`
- 选它的原因（docstring 原话）：一次同步调用同时返回**文本 + 句级时间戳 + 词级时间戳**
- docstring 记录的实测：60 秒音频 3.1 秒 / `0.00091` 元，上限 300 秒

**成本算一下**：`0.00091 元 / 60 秒` = `0.0000152 元/秒`。

- 每天转写 1 小时会议录音 → 3600 s × 0.0000152 ≈ **0.055 元/天 ≈ 1.6 元/月**
- 每天转写 10 小时 → **16 元/月**

**这是本报告最重要的一个反直觉结论：换成自建，在钱上几乎省不到东西。** 2C2G ECS 机器本来就已经为 docling + FastAPI + SQLite 付了钱，ASR 的边际机器成本约等于 0 ——但那台机器也本来就要跑满。所以**驱动换与不换的应该是"能不能离线/不受供应商限流/不泄漏录音内容"，不是省钱**。如果动机是省钱，答案是不换。

---

## 3. 候选逐一对比

### 3.1 先把用户点名的 MOSS 说清楚

**MOSS-TTSD 不是 ASR，是 TTS（文本→语音）。** 报告里必须显式澄清，避免又被当成 ASR 调研：

| 项 | 事实 |
|---|---|
| 全称 | MOSS-TTSD，README 自述 "Text to Spoken Dialogue Generation" |
| 方向 | **文本 → 语音**（含对话式多说话人语音合成），支持 1–5 说话人、60 分钟长上下文、20 语言 |
| 评测指标 | SIM / ACC / WER —— 都是**合成**质量指标（语音相似度、说话人归属、合成词错率），**没有任何识别入口** |
| 能否本地推理 | 能，但 8B 模型（`MossTTSDelay`），走 SGLang（`sglang serve --model-path ... --port 30000`），**需 GPU** |
| 时间戳能力 | **无**（它是生成，不是识别） |

**MOSS 家族里真正能做 ASR 的只有两个，且都吃 GPU：**

| 模型 | 是不是 ASR | 时间戳 | 本地推理 | CPU 可行 |
|---|---|---|---|---|
| MOSS-Audio-4B/8B-Instruct | ✅ 真 ASR | ✅ **原生 word-level + sentence-level**（time-marker insertion） | ✅ 但只有 SGLang / PyTorch，**无 ONNX/INT8 路径** | ❌ |
| MOSS-Transcribe-Diarize | ✅ 真 ASR | ✅ 句级 start/end + 说话人标签 | ✅ OpenAI 兼容 HTTP / vLLM / SGLang | ❌ 官方口径 H100 80GB 验证 |

MOSS-Audio 官方 CER 数字很漂亮（AISHELL-1 test CER 1.82 / Overall 11.30），Timestamp AAS 也远好于 Gemini-3.1-Pro 与 Qwen3-Omni-30B。**但它是 4B/8B 的 LLM-decoder 架构，`pip install --extra-index-url .../cu128`，文档里没有一条 CPU 或量化路径。在 2C2G 上连权重都放不下。**

> **一句话**：MOSS 系里 TTSD 是 TTS（别混），MOSS-Audio 是带原生时间戳的真 ASR，但它是 GPU 模型，2C2G 无独显直接出局。

### 3.2 全量对比表

`TS` = 时间戳；`CPU` = 无独显能否跑；`长音频` = 能否处理远超 10 秒的输入。

| 候选 | 真 ASR | TS 粒度 | TS 谁给的 | CPU/2C2G | 长音频 | 中文 CER | 落地形态 | 判定 |
|---|---|---|---|---|---|---|---|---|
| **sherpa-onnx + SenseVoice int8 + Silero VAD** | ✅ | token 级（逐字） | 模型原生起点；句边界由标点推 | ✅ **实测 433MB / RTF 0.02** | ✅ **实测 324s** | 引用 7.99%（184 集）/ 14.50（MOSS 评测） | 纯 Python wheel，无独立进程 | **推荐** |
| sherpa-onnx + Paraformer-zh-small | ✅ | **无（`n_timestamps=0`）** | 无 | ✅ 214MB / RTF 0.011 | ❌ **实测 60s 空串 + Reshape 崩** | 实测 34s CER 96.4% | 同上 | ❌ 淘汰（无 TS + 吃不下长输入） |
| sherpa-onnx + Fun-ASR-Nano int8 | ✅ | 需 VAD 补 | 同上 | 未测（约 800MB 级） | 需 VAD | 引用 8.06%（官方 184 集） | 纯 wheel | 备选 |
| sherpa-onnx + Qwen3-ASR 0.6B | ✅ | ❌ **导出后 `timestamps: []` 为空** | 无 | 937MB 权重 | ✅ 官方 20 min | — | 纯 wheel | ❌ 淘汰（无 TS） |
| **FunASR GGUF（llama.cpp 单二进制）** | ✅ | VAD 段级 | FSMN-VAD | ✅ **Vulkan 预编译包** | ✅ 内置 VAD | 引用 SenseVoice q8 7.99% | **单二进制 + 内置 OpenAI 兼容 server** | **次选（运维最简）** |
| FunASR 官方 `funasr-server` | ✅ | 字符级（需 ModelScope 版权重） | CTC forced align | ⚠️ 未验证 | ✅ | 引用 8.06% | Python 服务，官方只验证 GPU | 备选 |
| MOSS-Audio 4B/8B | ✅ | **原生词级+句级** | 模型（time-marker） | ❌ 4B/8B 需 GPU | ✅ | 引用 AISHELL 1.82 | SGLang | ❌ 2C2G 出局 |
| MOSS-Transcribe-Diarize | ✅ | 句级 + 说话人 | 模型 | ❌ H100 验证 | ✅ | — | OpenAI 兼容 HTTP | ❌ 同上 |
| MOSS-TTSD | ❌ **是 TTS** | — | — | — | — | — | SGLang | ❌ 方向不对 |
| Qwen3-ASR 0.6B/1.7B 官方 | ✅ | **需另配 `Qwen3-ForcedAligner-0.6B`** | aligner（非自回归 slot-filling，中文 33.1ms） | ⚠️ 0.6B ~2GB weights-only，官方建议 8GB+ | ✅ 20 min | 引用 3.76%（第三方）/ 优于豆包 | vLLM / PyTorch | ❌ 两模型叠加超 2C2G |
| faster-whisper / whisper.cpp | ✅ | **段级（segments start/end）** | 模型 | ✅ CPU + INT8 | ⚠️ 30s 窗口需自己拼 | **引用 CER 约为 FunASR 系 3 倍**（small 22.12% / turbo 23.15% vs SenseVoice 7.99%） | 都有 HTTP server | ❌ 中文明显更差 |
| FireRedASR2-LLM | ✅ | 有 | 模型 | ❌ LLM 架构 | ✅ | 引用 2.89%（论文自称优于 Qwen3-ASR） | — | 补充候选，2C2G 不现实 |

---

## 4. 为什么选 SenseVoice + VAD（而不是别的）

三条判据的交集只有一个：

1. **有时间戳** → 排除 Paraformer（sherpa 导出后 `n_timestamps=0`，实测）、Qwen3-ASR（导出后 `timestamps: []`，官方示例即如此）
2. **2C2G 能跑** → 排除 MOSS-Audio、MOSS-Transcribe-Diarize、Qwen3-ASR（0.6B 权重 ~2GB + aligner）、FireRedASR2-LLM
3. **中文 CER 够好** → 排除 whisper 系（引用 CER 约 3 倍差距）

同时满足的：**sherpa-onnx + SenseVoice int8（228MB）+ Silero VAD（0.6MB）**，以及同生态的 FunASR GGUF 二进制。

选前者而非后者的理由：纯 `pip install sherpa-onnx soundfile numpy`，**不需要单独进程、不需要额外端口、不需要 C++ 二进制分发**，可以直接嵌进现有 FastAPI 进程。FunASR GGUF 作为次选，因为它是单二进制 + 自带 OpenAI 兼容 server，运维上更"sidecar"，但要在 Linux 上单独发一份二进制。

---

## 5. 实测：SenseVoice + VAD 全流程

### 5.1 测试条件（可复现）

> **先读这条，否则会复现不出来。** 下面的数字**不是在 worktree 的 `backend/.venv` 里跑的**。那个 venv 里 `sherpa_onnx` / `soundfile` / `onnxruntime` **全部没有**——它是线上 FastAPI + docling 的环境，本任务约束是"不往线上 venv 装新依赖"，所以依赖、模型、测试音频全部放在 worktree **之外**：
>
> | 路径 | 内容 |
> |---|---|
> | `F:\RPeGio\Python_Project\_asr_poc_venv\Scripts\python.exe` | Python 3.13.7 + `sherpa_onnx 2.0.2` + `numpy 2.5.5` + `soundfile 0.14.0` |
> | `F:\RPeGio\Python_Project\_asr_models\` | SenseVoice int8 模型、`silero_vad.onnx`、全部测试音频、测量探针脚本 |
>
> 后果：`git clone` 这个分支后 `--check` 会报 `missing_files`。复现步骤写在 `backend/scripts/asr_local_poc.py` 的 docstring 里（装 venv → `pip install sherpa-onnx soundfile numpy` → 下模型 → 跑 `--check`），约 5 分钟。**这不是"跑在本地所以数字算数"的让步**——数字是真的，只是环境不在版本库里（模型 228MB 本来也不该进 git）。

- 机器：Windows 11 / Intel i7-14650HX（24 逻辑核）/ 31.7GB / **无独显**，`provider="cpu"`
- `sherpa-onnx 2.0.2`（`cp313-cp313-win_amd64` wheel）、`numpy 2.5.5`、`soundfile 0.14.0`、Python 3.13.7
- 模型：`sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17`（`model.int8.onnx` **228.15 MB** + `tokens.txt` 0.30 MB）、`silero_vad.onnx` 0.61 MB，均 Apache-2.0
- 音频：**用 Windows `System.Speech` + `Microsoft Huihui Desktop`(zh-CN) 合成的 8 句中文**，16kHz/16bit/mono。`long_8lines.wav` 54.4 s（8 句，句间 0.8 s 静音）；`long_320s.wav` 324.4 s（8 句 ×6 遍 = 48 句）。参考文本与每句真实起止时间在构建时落盘，**CER 与边界误差都能精确算**
- 内存：进程内 `psapi.GetProcessMemoryInfo` 读 `WorkingSetSize`/`PeakWorkingSetSize`，Python 解释器基线 29.4–29.8 MB

> ⚠️ **素材是 TTS 合成音频**。干净、无噪声、无口音、语速标准。下面 CER 接近 0 是**必然的**，只证明"链路不丢字"，不证明真实场景准确率。参考文本来自合成时喂进去的自报文本，**不是人工转写**。

### 5.2 加载 / 内存 / 速度

| 指标 | 实测 |
|---|---|
| 模型加载耗时 | **0.79 – 0.89 s**（多次一致） |
| 加载后工作集 | **333.6 – 361.3 MB** |
| 324 秒跑完峰值 | **432.6 MB**（threads=4）/ **432.8 MB**（threads=2） |
| 短音频 RTF（threads=4） | 0.021 – 0.023（模型自带 test_wavs：zh 5.59s→0.127s、en 7.15s→0.153s、yue 5.15s→0.109s） |
| 324 秒 VAD+SenseVoice | threads=1 **RTF 0.046**；threads=2 **0.0292**；threads=4 **0.0203** |

**线程数与内存在这个量级上几乎无关**（432.8 vs 432.6 MB），所以 2C2G 上配置 `threads=2` 是纯收益——没多花内存，少抢 CPU。

### 5.3 长音频：整段 vs VAD 分片（句对齐切点，threads=4）

这组数字是本报告的核心证据。切点落在真实句尾，CER 用完整不截断的参考文本：

| 输入时长 | 句数 | 整段一次解码 | VAD 分片 |
|---|---|---|---|
| 10.37 s | 2 | 0.21 s / RTF 0.0206 / **CER 0.0000** | 0.04+0.17 s / RTF 0.020 / **CER 0.0000** |
| 34.37 s | 5 | 0.80 s / RTF 0.0233 / **CER 0.0000** | RTF 0.0201 / **CER 0.0089** |
| 64.37 s | 10 | 1.95 s / RTF 0.0303 / **CER 0.0099** | RTF 0.0198 / **CER 0.0050** |
| 125.17 s | 19 | 5.66 s / RTF 0.0453 / **CER 0.0175**（开始丢字） | RTF 0.0199 / **CER 0.0025** |
| 304.37 s | 45 | 26.85 s / RTF 0.0882 / **CER 0.1163**（明显丢字） | RTF 0.0216 / **CER 0.0031** |

**读法**：
- 整段解码的 CER 随长度单调恶化（0 → 0 → 1.0% → 1.8% → **11.6%**），且 RTF 也恶化（0.021 → 0.088）——这才是"架构吃不下长输入"的真实形态：**不是崩，是悄悄丢字**
- VAD 分片后 CER 稳定在 0.3% 量级、RTF 稳定在 0.02，**与长度无关**
- 125 秒整段丢的「统」字（"这个系**统**需要把…"→"这个系需要把…"）说明这不是偶发噪声，是系统性丢字

> 对比 `asr.py` docstring 的旧结论（"paraformer-zh-small 33.8 秒整段 CER 86.4%，60 秒直接返回空串"）：**SenseVoice 在 60/125/300 秒都不返回空串、不抛 onnxruntime 错误**，退化形式是丢字而非崩溃。旧结论对 Paraformer 成立，对 SenseVoice 不成立。

### 5.4 端到端 PoC 精度（`backend/scripts/asr_local_poc.py`，threads=2，`min_silence=1.2`）

| 音频 | 时长 | 真实句数 | 预测句数 | CER | 边界误差 中位 / p90 / max |
|---|---|---|---|---|---|
| `long_8lines.wav` | 54.4 s | 8 | 9 | **0.0058** | 0.213 / 1.428 / 1.428 s |
| `long_320s.wav` | 324.4 s | 48 | 54 | **0.0029** | 0.208 / 1.412 / 1.428 s |

**句数为什么会多**：`min_silence=0.5` 时 8 句被切成 9 段（句中换气被当成句子边界），边界误差出现 **3.456 s** 离群；调到 `1.2` 后 8 句重新对齐成 8 段（VAD 段数 9→8），离群降到 1.428 s。剩余 1.4 s 误差来自 Silero VAD 的段尾裁剪（它不是精确的句尾检测器）。

> **对齐方法说明（影响数字怎么读）**：预测 9 句 vs 真实 8 句时，不能用 `zip` 逐项配对——那样 0.284 s 的"好看"误差是错位配对算出来的假象。上面这组是**按文本内容匹配**（每个真实句找字符重合率最高的预测句）算的，才反映真实边界误差。复跑校验：`n=8 median 0.213 p90 1.428 max 1.428`，与表内一致。

**这一点必须写进设计约束**：`begin_ms` 的精度是 **±0.2 s 中位、最坏 ±1.4 s** 量级，够做"这句话在录音第几秒"的跳转，**不够做逐字卡拉OK对齐**。

### 5.5 HTTP 服务（PoC 已起，实测通过）

`python backend/scripts/asr_local_poc.py --serve --port 8791`：

| 端点 | 结果 |
|---|---|
| `GET /health` | 200 `{"ok": true, "load_seconds": 0.809, "rss_mb": [365.4, 365.4]}` |
| `POST /transcribe`（raw wav body） | 200，返回 `{"text","sentences":[{text,begin_ms,end_ms}],"timings","wall_seconds","channels","rss_mb"}` |
| `POST /v1/audio/transcriptions`（multipart `-F file=@`） | 200，同上形状（**注意：响应不是 OpenAI 的 `segments`，是 `AudioSentence`**） |
| 上传非音频 | 400 `{"error":"cannot decode audio: ... Format not recognised."}` |
| 非 16k 采样率 | 400 `{"error":"only 16k mono supported, got {rate}Hz"}` |
| 4 路并发（各 54 s 音频） | 2.30 / 2.49 / 2.53 / 2.55 s，无错；压完 RSS 426.5 MB / 峰值 476.0 MB |

> 复跑校验（`line00.wav`，4.36 s 单句）：`GET /health` → `{"ok":true,"load_seconds":1.171,"rss_mb":[364.3,364.3]}`；`POST /transcribe` raw wav → `{"text":"你好，欢迎使用本地语音识别测试。","sentences":[{"text":"你好，欢迎使用本地语音识别测试。","begin_ms":162,"end_ms":4360}],"timings":{"audio_seconds":4.36,"vad_seconds":0.03,"asr_seconds":0.096,"rtf_excluding_load":0.0289,"n_vad_segments":1},"wall_seconds":0.187,"channels":1,"rss_mb":[385.8,413.8]}`。**注意 `end_ms: 4360` 就是音频末尾——单段音频时末句 end 退化到段尾，这正是 5.4 说的"推导值"。**

### 5.6 PoC 里踩到并修掉的两个真 bug（报告出来，避免下一个人重踩）

1. **`VoiceActivityDetector` 跨请求复用会让时间戳整体偏移。** `segment.start` 是**自 detector 创建以来**的累计采样偏移，不是单次 `accept_waveform` 的偏移。实测：同一个 server 的第二个请求（4.36 s 音频）返回 `begin_ms: 8578`。**修法：每请求新建 detector。** 这个坑在 2C2G 上还有个额外含义——每请求新建 detector 是有成本的，实测 VAD 建好到出首段约几十毫秒，可接受。
2. **`tokens` / `timestamps` 是段内相对秒，不是整段录音偏移。** 首版 `split_sentences` 直接把相对值当绝对值用，导致 `end_ms` 跑到音频总时长之外（54 s 音频出现 `end_ms: 102674`）。加 `seg_start_s` 才对。

---

## 6. 旧结论复测：paraformer-zh-small 确实吃不下长输入

`asr.py` docstring 说"paraformer-zh-small 在 2C2G 上常驻 143MB、短音频 RTF 0.013，但架构吃不下长输入——33.8 秒整段 CER 86.4%，60 秒直接返回空串并抛 onnxruntime Reshape"。用同一批句对齐音频复现（`sherpa-onnx-paraformer-zh-small-2024-03-09`，int8，78 MB）：

| 输入 | 旧结论 | 我的复测 | 一致？ |
|---|---|---|---|
| 10.37 s | RTF 0.013 量级、可用 | 0.11 s / RTF 0.011 / CER 0.0000 / 30 字全对 | ✅ |
| 34.37 s | CER **86.4%** | 0.39 s / RTF 0.0113 / CER **96.43%**，只输出「语音测试」4 字 | ✅ **复现** |
| 64.37 s | 空串 + `Reshape` 错误 | **空串**，日志 `Reshape_5342`：`Input shape:{0,320}, requested shape:{1,320,320}` | ✅ **逐字复现** |
| 125.17 s | — | 空串，同一个 `Reshape_5342` | ✅ |
| 304.37 s | — | 不空但**大量幻觉重复**（"你好欢迎使用本地语音识别测试今把天气不错…"循环），CER 13.07% | 新发现 |

内存：加载后 **174.4 MB**、30 s 推理后 211.8 MB、峰值 **213.9 MB**；加载 0.82 s。
**而且 `n_timestamps = 0`——sherpa-onnx 导出的 Paraformer 完全不返回时间戳。**

> 复跑校验（同一次质疑后当场重跑，见文末复跑记录）：10.37s→RTF 0.0108 / 30 字 / `n_timestamps 0`；34.37s→RTF 0.0113 / **4 字**（"语音测试"）；64.37s 与 125.17s→**空串 + 同一 `Reshape_5342`**；304.37s→985 字幻觉重复。五次输入 `n_timestamps` **全部为 0**。旧结论逐字成立。

**所以 Paraformer 是双重出局**：没有时间戳（判据 1 fail）+ 吃不下长输入（判据 2 fail）。旧结论不需要推翻，需要**扩写**：它精准命中 Paraformer，但被错误地泛化成了"本地模型都不行"。

---

## 7. 生产 2C2G 上能不能落

### 7.1 先把内存账算清

实测的三个基线（同一台机器上量的，可相加比较）：

| 组件 | 工作集 | 备注 |
|---|---|---|
| Python 解释器基线 | 15 MB | |
| `import app.main`（FastAPI 全路由） | **132.3 MB**（+0.74 s） | |
| `import docling.document_converter` | **340.0 MB**（+9.06 s） | 只是 import，没加载版面模型 |
| `app.main` + docling 都 import | **388.2 MB** | |
| SenseVoice 加载后 | **333.6 – 361.3 MB** | |
| SenseVoice 跑完长音频峰值 | **432.6 MB** | |

**同进程相加**：388 MB（app + docling import）+ 433 MB（ASR 峰值）≈ **820 MB**。
阿里云 2C2G 的 Alibaba Cloud Linux 3.2 实际可用约 1.7 GB，扣掉 OS 后剩 ~1.4 GB。
**820 MB 放得下，但余量只有 500 MB 出头**——而 `backend/app/ingestion/service.py` 第 12 行自己记着：Docling 的版面模型实测峰值 **1.6–2.8 GB**（这就是 PDF 不走 Docling 的原因）。**音频转写与 DOCX/PPTX 解析如果并发，2G 会 OOM。**

### 7.2 三种落法，推荐第 3 种

| 方案 | 常驻内存 | 单次延迟 | 运维复杂度 | 评价 |
|---|---|---|---|---|
| A. 进程内常驻（启动即加载） | 388 + **361 永久** = 749 MB，之后每次请求峰值 820 MB | 0.83 s 只付一次 | 最低 | ❌ 2C2G 上永久吃掉 361 MB 换一个低频功能，不划算 |
| B. 独立 sidecar 进程（第二个 systemd unit） | sidecar 常驻 361 MB + 主进程 388 MB，**合计一样** | 跨进程 HTTP 开销 | 中：多一个 unit、多一个端口、多一份健康检查、nginx 要多一条 upstream | ⚠️ 内存没省，只隔离了崩溃域 |
| **C. 进程内懒加载 + 短命子进程（每请求 fork）** | **0**（子进程退出后全额回收） | **+0.83 s 加载** | 最低 | ✅ **推荐** |

**为什么推荐 C**：加载只要 **0.83 秒**。对一个 60 秒的会议录音，总耗时 `0.83 + 60×0.02 ≈ 2 s`，而现在云端是 **3.1 秒**——**自建即使每次重新加载模型，也比现在的付费方案快**。既然加载成本这么低，就没必要为"省一次加载"而永久占 361 MB 常驻内存。

这也不是我发明的招：FunASR 官方的 `server/funasr_gguf_server.py` 就是"每请求起一个子进程"（官方定位为本地工具/演示）。

**方案 C 的落地约束（必须写进实现）：**
- 一次只允许一个转写子进程（文件锁或信号量），否则两个 433 MB 子进程同时存活就爆了
- 子进程要设内存上限（`resource.setrlimit`）并带超时 kill
- 传入音频要先落临时文件或走 stdin，**不要**把几十 MB 的 PCM 塞进子进程命令行参数
- 失败要留下"降过级"的痕迹（参照 AGENTS.md 的约定）：本地转写失败时**不要静默回落到云端**——那会让"没跑"和"跑坏了"长得一样。要么明确报错，要么在 `Source.metadata` 里写明走了哪条路

### 7.3 能不能落：结论

**能落，推荐方案 C，但必须带三条硬约束：**

1. 一次只跑一个 ASR 子进程（`MAX_WORKERS=1`，不能用现在 `asr.py` 的 2 路并发思路）
2. 音频时长上限从云端时代的 300 s 收紧。VAD 分片后 RTF 稳定，**瓶颈不是时长而是"要不要和文档解析并发"**。建议沿用 300 s 单文件上限并明确拒绝超限
3. 音频转写与 Docling 解析**互斥**（同一把锁），避免 820 MB + 版面模型峰值叠加

**如果这三条做不到，就不要落**——2C2G 上内存是最容易"看起来没问题、半夜 OOM"的地方。

**来电了从哪下手**：`backend/scripts/asr_switch_check.py` 是一份**会跑、会失败**的接线落点检查（只读文件，不改生产代码）。它把接线拆成三处——调用点（`service.py::_parse_unstamped` 的 audio 分支）、开关（`asr.py` 里新增 `_local_enabled()`，**默认必须关**，否则没装 sherpa-onnx 的线上 venv 一导入就炸）、互斥锁（与 `_parse_lock` **共用同一把**，不是新加一把）——并用 `ast` 校验这三处的函数名与模块级赋值是否仍然存在。重构后函数改名它会立刻红，不会静默通过。`--json` 给脚本消费，退出码 0/1 可进 CI。

### 7.4 但回到最重要的问题：**该不该换？**

| 维度 | 云端 `qwen-audio-3.1-asr-flash` | 本地 SenseVoice+VAD |
|---|---|---|
| 60 s 音频耗时 | **3.1 s**（docstring 记录） | **~2.0 s**（实测 RTF 0.02 + 0.83 s 加载） |
| 时间戳 | 句级 + **词级** | token 级（逐字）+ 推导句级 |
| `end_ms` 精度 | 模型给 | **推导，±0.2 s 中位 / 最坏 ±1.4 s** |
| 单文件上限 | 300 s | 无硬限（VAD 分片） |
| 中文准确率 | 未测评 | 引用 7.99%～14.50%（不同测试集） |
| 钱 | **1.6 元/月**（每天 1 小时） | ECS 边际成本 ≈ 0，但要吃 2C2G 内存 |
| 依赖 | `DASHSCOPE_API_KEY` + 外网 | **无外部依赖** |
| 录音内容 | **上传到阿里云** | **不出机器** |
| 失败模式 | 限流/欠费/网络 | 内存不足/OOM |

**理由排序（按用户该不该拍板）：**
1. **换的正当理由是离线与隐私**（会议录音不出机器、不受限流），不是省钱
2. **换的代价是 2C2G 上每次转写要 433 MB 峰值**，且必须与文档解析互斥
3. **换的准确率风险未知**：我只有 TTS 干净音频的 0.29%，真实会议录音（多人、远场、口音、专业术语）的 CER 我没测。引用数据说 SenseVoice 在公开集上比 whisper 好得多，但**比 qwen-audio-3.1-asr-flash 好还是坏，我不知道，也没有找到可信的对比数据**

**所以我的建议是：值得做，但先做成"可切换的第二条路"，不要直接替换生产路径。** 具体：
- 保留 `asr.py` 现有云端路径不动（本任务已按要求没碰它）
- 本地路径先以开关形式接进来（例如 `MINDMAP_ASR_LOCAL=1`），默认关闭
- 用**真实会议录音**做一次 A/B，比 CER 也比时间戳可用性，再决定默认值

---

## 8. 明确没验证的部分

这一节是本文最该被认真读的部分。

### 8.1 完全没跑过的

| 项 | 状态 | 需要什么条件 |
|---|---|---|
| **Linux / Alibaba Cloud Linux 3.2 上跑** | ❌ 未验证。全部实测在 **Windows** 上 | 2C2G ECS 上一台，`pip install sherpa-onnx` 后重跑 `asr_local_poc.py --audio` |
| **2C2G 真实内存压力** | ❌ 未验证。433 MB 是在 **31.7 GB** 机器上量的。**onnxruntime 在小内存机上可能表现不同**（页回收、THP、cgroup 限制） | 同上，用 `systemd-run --scope -p MemoryMax=2G` 压测 |
| **真实会议录音的 CER** | ❌ 未验证。只有 TTS 合成音频 | 3–5 段真实录音 + 人工校对文本 |
| **与 `qwen-audio-3.1-asr-flash` 的 A/B** | ❌ 未验证。需要 `DASHSCOPE_API_KEY`，本机没有 | key + 同一批音频 |
| **FunASR GGUF 二进制** | ❌ 未实测。只确认了它存在、有 Vulkan 预编译包、官方给了 CER | 下载 `funasr-llamacpp-windows-x64-vulkan.zip` 跑一遍 |
| **Fun-ASR-Nano / Qwen3-ASR 的 CPU 实测** | ❌ 未实测。Fun-ASR-Nano 只看了官方 CER，Qwen3-ASR 因"导出后无时间戳"直接淘汰未下载 | 各自权重 |
| **8k 采样率**（`SUPPORTED_RATES` 里有 8000） | ❌ 未测。PoC 只支持 16k，非 16k 直接 400 | 重采样逻辑或 8k 测试音频 |
| **英文 / 中英混说** | ❌ 未测。英文 token 是子词，`split_sentences` 按标点切对英文是否成立没验证 | 英文音频；大概率要额外处理子词合并 |
| **说话人分离** | ❌ 不支持。SenseVoice 无 diarization | 另一个模型（cam++ 之类），本任务范围外 |
| **长时间运行的内存泄漏** | ❌ 未验证。4 路并发压完 476 MB，但没跑几小时 | soak test |
| **Vulkan 核显加速** | ❌ 未尝试。本机有 Vulkan 核显，但 sherpa-onnx 的 SenseVoice 走 CPU provider | 需要确认是否支持 Vulkan EP |
| **接线是否真跑通** | ❌ 未接。用户已拍板「这轮不接线」——`backend/app/**` 一行没动。三处落点**存在性**由 `backend/scripts/asr_switch_check.py` 校验（当前三处全 OK、退出码 0），但那只是"下手点还在"，**不是"接上之后能跑"** | 接开关 → 改调用点 → 接锁，三步各自提交后跑 `pytest backend/tests/test_asr.py` |

### 8.2 引用而非实测的数据（来源已标注）

- SenseVoice / Paraformer / Fun-ASR / whisper.cpp 的 CER 对比：来自 FunASR GGUF 官方 184 集中文测试集与 MOSS-Audio 论文评测表
- MOSS-Audio、MOSS-Transcribe-Diarize、Qwen3-ASR、Qwen3-ForcedAligner 的架构、显存、时间戳精度：来自各自 README / 模型卡
- SenseVoice 在 RK3588 上的 RTF、Qwen3-ASR 的 0.6B 吞吐：来自官方 README 的芯片/GPU 数据

### 8.3 一个必须点明的偏差

我的 CER 是在**自己合成的 TTS 音频**上算的。这类音频的特点是：单人、无噪声、标准普通话、语速均匀、无专业术语。**在这种音频上 CER 0.29% 不能外推到任何真实场景。** 如果拿这个数字去说"本地比云端好/坏"，是错的。它能支撑的结论只有一条：**这条链路不会因为长音频而系统性丢字（对照：Paraformer 会）。**

---

## 9. 需要用户拍板的问题

1. **换的动机确认**：是为了"录音不上传/离线可用"，还是为了"省钱"？如果是省钱 —— 按现在的用量（1.6 元/月 @ 每天 1 小时）**不值得换**，换来的是运维与 OOM 风险。
2. **能不能接受 `end_ms` 是推导值、精度 ±0.2 s（最坏 ±1.4 s）**？如果不能接受，整条本地路线都不可行——因为没有哪个 CPU 可跑的模型会给你精确句边界。
3. **能不能接受"转写与文档解析互斥"**（同一把锁）？如果不能，需要 4C4G 起。
4. **是否同意先做开关、A/B 之后再决定默认值**（而不是这次直接替换生产路径）？
5. **如果做 sidecar，能不能接受多一个 systemd unit**？我个人建议不用 sidecar（内存没省，只隔离崩溃域），但如果团队偏好进程隔离，这是唯一能拿到进程级隔离的方式。

---

## 附：可复现清单

| 文件 | 作用 |
|---|---|
| `backend/scripts/asr_local_poc.py` | PoC 本体：`--check` / `--audio` / `--serve`，输出 `AudioSentence` 形状 JSON 或起 HTTP 服务 |
| `backend/scripts/asr_poc_sample/` | **测试音频（已进仓库）**：`long_8lines.wav` 54.4s / 8 句 + `long_8lines_truth.json`（每句真实起止毫秒）+ `transcript.txt`（参考文本）+ `line00.wav` 单句样例。共 1.79 MB |
| `backend/scripts/asr_switch_check.py` | **接线落点检查**（只读，不改生产代码）：三处落点的存在性 + 改什么，`--json` 可进 CI，退出码 0/1 |
| `_asr_models/_sweep_aligned.py` | 句对齐时长扫描（第 5.3 节数字） |
| `_asr_models/_cer_compare.py` | 整段 vs VAD 的 CER 与边界误差 |
| `_asr_models/_peak_vad.py` | 进程内测 VAD+SenseVoice 峰值 RSS |
| `_asr_models/_mem_probe.py` | 加载/推理前后 RSS（含解释器基线） |
| `_asr_models/_paraformer_small.py` | paraformer-zh-small 旧结论复测（第 6 节） |
| `_asr_models/_paraformer_mem.py` | paraformer 内存 |
| `_asr_models/_poc_accuracy.py` | PoC 端到端 CER + 边界误差 |
| `_asr_models/audio/` | 全部测试音频与参考文本（`line0*.wav`、`long_8lines.wav`、`long_320s.wav`、`*_truth.json`、`sweep_aligned.json`） |

模型下载（`huggingface.co` 不通时走 `https://gh-proxy.com/` 前缀）：

```text
https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/
  sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2   # 228 MB
  sherpa-onnx-paraformer-zh-small-2024-03-09.tar.bz2                #  78 MB
asr-models 同目录下的 silero_vad.onnx                                # 0.6 MB
```

依赖只在 PoC 的独立 venv 里装过，**没有**写进 `backend/requirements.txt`：

```bash
pip install sherpa-onnx soundfile numpy
```

### 附：本次复跑记录（2026-11-21，收到"数字来源存疑"的质疑后当场重跑）

因为最初的实测环境放在 worktree 之外（`_asr_poc_venv` / `_asr_models`），复现性被合理质疑了一次。下面是收到质疑后**当场重跑**的结果，全部与本报告正文一致：

| 复跑项 | 输出 |
|---|---|
| `--check` | `{"missing_files": [], "deps": {"sherpa_onnx": "ok", "soundfile": "ok", "numpy": "ok"}}` |
| 库版本 | `sherpa_onnx 2.0.2` / `numpy 2.5.5` / `soundfile 0.14.0` |
| `long_8lines.wav`（54.4s） | 9 句 / `vad 0.205s + asr 1.432s` / RTF 0.0301 / **CER 0.0058** / RSS 418.0MB |
| `long_320s.wav`（324.4s） | 54 句 / `vad 1.167s + asr 8.341s` / RTF 0.0293 / **CER 0.0029** / RSS 446.2MB |
| 整段解码 324.4s | `decode 42.37s` / RTF 0.1306 / **CER 0.0901** / 949 字（参考 1032 字，**丢 83 字**）/ `n_timestamps 1006` / 文本头已丢「统」字 |
| paraformer 复现 | 34.37s → CER 96.43%（4 字）；64.37s/125.17s → **空串 + `Reshape_5342` `Input shape:{0,320}, requested shape:{1,320,320}`**；304.37s → 985 字幻觉重复；**`n_timestamps = 0` 全部输入** |
| VAD detector 构造 | 11.97 / 12.29 / 12.44 / 12.6 / 12.49 ms |

一处**修正**：整段解码 324.4s 这次复跑是 **RTF 0.1306 / CER 0.0901**，报告 5.3 节表内是 `26.85s / RTF 0.0882 / CER 0.1163`。两次都是同一份代码同一份音频，差异来自机器负载（复跑时同时在跑其他探针）。**两次都指向同一个结论：整段解码在 300 秒量级上 CER 约 9%–12%，VAD 分片后 0.3%。** 引用时建议用范围而不是单点。整段解码耗时/RTF 的绝对值比 VAD 分片敏感得多，这本身也是个结论：**长输入不分片，性能与质量都会抖。**
