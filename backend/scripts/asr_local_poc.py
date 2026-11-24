"""本地 ASR sidecar 的最小可跑验证（sherpa-onnx + SenseVoice + Silero VAD）。

**这不是生产代码**，只是用来回答 `docs/RESEARCH-ASR-LOCAL.md` 里那三个硬需求的
一次性验证：句级时间戳、2C2G 上能吃长音频、中文准确率。它刻意不改
`backend/app/ingestion/asr.py` 的任何一行——调研阶段不碰线上路径。

为什么是 VAD 切分 + SenseVoice：

  - SenseVoice 裸模型（int8）一次解码整段输入时，`result.timestamps` 是**逐 token
    的起点秒**（中文即逐字），但**没有句子边界**：324 秒整段解码 CER 11.6%，
    而且会在长输入上丢字（见报告第 3 节的实测表）。
  - 加一层 Silero VAD 先把音频切成"有人说话的段"，逐段解码后再把每段的
    起始时间加回去，CER 降到 0.3% 量级，且天然得到段级的 begin_ms/end_ms。
    这正是 `backend/app/contracts/source.py` 的 `AudioSentence` 要的形状。
  - 句级边界从**标点**（逐 token 时间戳 + 标点 token）切出来，`end_ms` 用
    "下一句第一个 token 的起点"推出来——**模型只给 token 起点，end 是推导值**，
    报告里必须如实标这一点。

用法：

    # 一次性转写一个文件，打印 AudioSentence 形状的 JSON + 实测耗时/内存
    python scripts/asr_local_poc.py --audio sample.wav

    # 起一个本地 HTTP 服务（对齐 OpenAI /v1/audio/transcriptions 的请求形状，
    # 但响应是 AudioSentence 而不是 OpenAI 的 segments）
    python scripts/asr_local_poc.py --serve --port 8791
    curl -X POST --data-binary @sample.wav \
         -H 'Content-Type: audio/wav' http://127.0.0.1:8791/transcribe

    # 只体检模型与依赖，不转写
    python scripts/asr_local_poc.py --check

依赖（**不在** `backend/requirements.txt` 里，故意不装进线上 venv）：

    pip install sherpa-onnx soundfile numpy

模型（Apache-2.0，约 228MB int8 + 0.6MB VAD）：

    https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/
      sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
      silero_vad.onnx   （在 sherpa-onnx 同一 release 的 asr-models 前缀下）

目录用 `--model-dir` 或 `ASR_MODEL_DIR` 指定，脚本只认这两个文件名：
`model.int8.onnx`、`tokens.txt`（VAD 用 `--vad-model`，默认找同级 `silero_vad.onnx`）。

实测环境（这一节必须读，否则会误以为 worktree 里跑过）：

本文件里的数字**不是在 worktree 的 `backend/.venv` 里跑的**。那个 venv 里没有
`sherpa_onnx`——它是线上 FastAPI/docling 的环境，本任务的约束是"不往线上 venv
装新依赖"，所以依赖与模型都放在 worktree **之外**的两处目录：

    F:\\RPeGio\\Python_Project\\_asr_poc_venv\\Scripts\\python.exe   # sherpa_onnx 2.0.2
    F:\\RPeGio\\Python_Project\\_asr_models\\                       # 模型（228MB，不进 git）

**测试音频已经在仓库里**了：`backend/scripts/asr_poc_sample/`
（`long_8lines.wav` 54.4s 8 句 + `long_8lines_truth.json` 每句真实起止 + `transcript.txt`）。
这样 clone 下来只需要装依赖 + 下模型，不用自己造音频。

要复现（约 5 分钟，模型 228MB）：

    python -m venv _asr_poc_venv
    _asr_poc_venv\\Scripts\\pip install sherpa-onnx soundfile numpy
    # 下模型到 _asr_models\\（见上面两个 URL），解压
    _asr_poc_venv\\Scripts\\python.exe scripts/asr_local_poc.py \\
        --check --model-dir _asr_models/sherpa-onnx-sense-voice-... \\
        --vad-model _asr_models/silero_vad.onnx
    _asr_poc_venv\\Scripts\\python.exe scripts/asr_local_poc.py \\
        --audio scripts/asr_poc_sample/long_8lines.wav \\
        --model-dir _asr_models/sherpa-onnx-sense-voice-... \\
        --vad-model _asr_models/silero_vad.onnx --threads 2

测试音频是**本机合成**的（`System.Speech` + `Microsoft Huihui Desktop` zh-CN，
16kHz/16bit/mono），**不是真人录音**——参考文本来自合成时喂进去的自报文本，
不是人工转写。这对 CER 的可信度影响很大，报告第 8.3 节专门写了这个偏差。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import io
import json
import os
import sys
import threading
import time
import wave
from dataclasses import dataclass, field

# 句级切分的标点集合。SenseVoice 的 `use_itn=True` 会给出这几个标点，
# 所以句边界只需要看 token 本身，不需要额外的标点模型（ct-punc）。
_SENTENCE_END = "。！？；…!?;"
# AudioSentence.text 的契约上限（backend/app/contracts/source.py）。
_MAX_SENTENCE_CHARS = 500


# --------------------------------------------------------------------------- 内存

class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _rss_mb() -> tuple[float, float] | None:
    """(当前, 峰值) 工作集 MB；非 Windows 读 /proc/self/status 的 VmHWM。"""
    if sys.platform == "win32":
        psapi = ctypes.WinDLL("psapi")
        kernel32 = ctypes.WinDLL("kernel32")
        psapi.GetProcessMemoryInfo.argtypes = [
            wt.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), wt.DWORD
        ]
        psapi.GetProcessMemoryInfo.restype = wt.BOOL
        p = _ProcessMemoryCounters()
        p.cb = ctypes.sizeof(_ProcessMemoryCounters)
        if not psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(p), p.cb
        ):
            return None
        return (
            round(p.WorkingSetSize / 1048576, 1),
            round(p.PeakWorkingSetSize / 1048576, 1),
        )
    try:
        cur = peak = 0.0
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    cur = int(line.split()[1]) / 1024
                elif line.startswith("VmHWM:"):
                    peak = int(line.split()[1]) / 1024
        return round(cur, 1), round(peak, 1)
    except OSError:
        return None


# --------------------------------------------------------------------------- 句级切分

@dataclass
class Sentence:
    """与 backend/app/contracts/source.py 的 AudioSentence 同形。"""

    text: str
    begin_ms: int
    end_ms: int

    def as_dict(self) -> dict:
        return {"text": self.text, "begin_ms": self.begin_ms, "end_ms": self.end_ms}


def split_sentences(
    tokens: list[str], timestamps: list[float], seg_start_s: float, seg_len_s: float
) -> list[Sentence]:
    """把「逐 token 起点时间戳」切成句级区间。

    `tokens` / `timestamps` 都是**段内相对秒**（sherpa-onnx 的 `result.timestamps`
    就是相对这次解码输入起点的），本函数负责加上 `seg_start_s` 变成整段录音偏移。

    - `begin_ms` = 该句第一个 token 的起点（模型直接给出的值）。
    - `end_ms` = 下一句第一个 token 的起点；最后一句退化到 VAD 段尾。
      **end 是推导值，不是模型输出**——报告里要这么写。
    - 超过 `_MAX_SENTENCE_CHARS` 的句子按 token 边界硬切，避免契约校验失败。
    """
    if not tokens:
        return []
    # 段尾（相对）作为最后一句 end 的兜底。
    stamps = list(timestamps) + [seg_len_s]
    out: list[Sentence] = []
    cur: list[tuple[str, float]] = []

    def emit(part: list[tuple[str, float]], end_s: float) -> None:
        start_s = part[0][1]
        if end_s <= start_s:  # 时间戳偶尔不单调，退化成至少 1 个 token 的跨度
            end_s = start_s
        out.append(Sentence(
            text="".join(t for t, _ in part),
            begin_ms=int(round((seg_start_s + start_s) * 1000)),
            end_ms=int(round((seg_start_s + end_s) * 1000)),
        ))

    def flush(next_start: float) -> None:
        nonlocal cur
        if not cur:
            return
        while len(cur) > _MAX_SENTENCE_CHARS:
            head, cur = cur[:_MAX_SENTENCE_CHARS], cur[_MAX_SENTENCE_CHARS:]
            emit(head, head[-1][1])
        if cur:
            emit(cur, next_start)
            cur = []

    for idx, tok in enumerate(tokens):
        cur.append((tok, stamps[idx]))
        if tok and tok[-1] in _SENTENCE_END:
            flush(stamps[idx + 1])
    flush(seg_len_s)
    # VAD 段内只有一句、且末 token 不是标点时，末句仍然是一条合法句。
    return [s for s in out if s.text.strip()]


# --------------------------------------------------------------------------- 引擎

@dataclass
class Engine:
    """SenseVoice + Silero VAD。加载一次，之后多线程复用（sherpa-onnx 的
    OfflineRecognizer 内部是 C++ 的，`create_stream` 每次独立，可以跨线程）。"""

    model_dir: str
    vad_model: str
    threads: int = 4
    # VAD 停顿阈值。0.5s 会把一句之内的换气/停顿也切开，导致句级边界多出
    # 一段（实测 8 句音频被切成 9 段，多出的那段让边界误差出现 3.456s 离群）。
    # 1.2s 能把 8 句重新对齐成 8 段，见报告第 3 节。
    min_silence_s: float = 1.2
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _rec: object | None = field(default=None, repr=False)
    _vad: object | None = field(default=None, repr=False)
    _vad_base: int = field(default=0, repr=False)
    load_seconds: float = 0.0

    def load(self) -> None:
        import sherpa_onnx

        t0 = time.perf_counter()
        self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=os.path.join(self.model_dir, "model.int8.onnx"),
            tokens=os.path.join(self.model_dir, "tokens.txt"),
            num_threads=self.threads,
            use_itn=True,
            language="zh",
            provider="cpu",
        )
        self._vad = self._new_vad()
        self.load_seconds = round(time.perf_counter() - t0, 3)

    def _new_vad(self):
        """每次请求新建一个 VAD。

        `VoiceActivityDetector` 是有状态的：`segment.start` 是**自 detector 创建
        以来**的采样数，跨请求复用会让第二段音频的时间戳直接偏移第一段的总时长
        （实测第二个请求返回 `begin_ms: 8578`，而音频只有 4.36 秒）。重建
        detector 是唯一能把它归零的公开手段，代价见报告里的 VAD 建立耗时。
        """
        import sherpa_onnx

        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad = sherpa_onnx.SileroVadModelConfig(
            model=self.vad_model,
            threshold=0.5,
            min_silence_duration=self.min_silence_s,
            min_speech_duration=0.25,
            max_speech_duration=20,  # 单段上限；超过就强制断开，避免大段解码退化
            window_size=512,
        )
        cfg.sample_rate = 16000
        return sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=600)

    def _segments(self, audio) -> list[tuple[float, object]]:
        import numpy as np

        vad = self._new_vad()
        segs: list[tuple[float, object]] = []
        for i in range(0, len(audio), 512):
            vad.accept_waveform(audio[i:i + 512])
            while not vad.empty():
                s = vad.front
                segs.append((s.start / 16000, np.array(s.samples, dtype="float32")))
                vad.pop()
        vad.flush()
        while not vad.empty():
            s = vad.front
            segs.append((s.start / 16000, np.array(s.samples, dtype="float32")))
            vad.pop()
        return segs

    def transcribe(self, audio, sample_rate: int = 16000) -> dict:
        """返回 {"text", "sentences", "timings"}；sentences 对齐 AudioSentence。"""
        import numpy as np

        if sample_rate != 16000:
            raise ValueError(f"只支持 16k 输入，拿到 {sample_rate}")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        audio = audio.astype("float32", copy=False)
        duration = len(audio) / 16000

        t0 = time.perf_counter()
        with self._lock:  # VAD 的状态是共享的，必须串行；识别本身是每段独立
            segs = self._segments(audio)
        vad_seconds = time.perf_counter() - t0

        t0 = time.perf_counter()
        sentences: list[Sentence] = []
        for seg_start, samples in segs:
            st = self._rec.create_stream()
            st.accept_waveform(16000, samples)
            self._rec.decode_stream(st)
            res = st.result
            sentences.extend(
                split_sentences(
                    list(res.tokens), list(res.timestamps), seg_start, len(samples) / 16000
                )
            )
        asr_seconds = time.perf_counter() - t0

        return {
            "text": "".join(s.text for s in sentences),
            "sentences": [s.as_dict() for s in sentences],
            "timings": {
                "audio_seconds": round(duration, 3),
                "vad_seconds": round(vad_seconds, 3),
                "asr_seconds": round(asr_seconds, 3),
                "rtf_excluding_load": round((vad_seconds + asr_seconds) / max(duration, 1e-9), 4),
                "n_vad_segments": len(segs),
            },
        }


def read_wav_bytes(raw: bytes) -> tuple[object, int, int]:
    """(float32 单声道, sample_rate, channels)。用 soundfile，非 16k 由调用方拒绝。"""
    import soundfile as sf

    audio, sr = sf.read(io.BytesIO(raw), dtype="float32")
    channels = 1 if audio.ndim == 1 else audio.shape[1]
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return audio, sr, channels


def read_wav_file(path: str) -> tuple[object, int]:
    with wave.open(path, "rb") as fh:
        sr = fh.getframerate()
        nch = fh.getnchannels()
    import soundfile as sf

    audio, sr = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return audio, sr


# --------------------------------------------------------------------------- HTTP

def _multipart_body(ctype: str, body: bytes) -> bytes:
    """极简 multipart 解析：取第一个文件的 payload。只为 PoC。"""
    marker = "boundary="
    pos = ctype.find(marker)
    if pos < 0:
        return body
    boundary = ctype[pos + len(marker):].split(";")[0].strip().strip('"').encode()
    sep = b"--" + boundary
    for part in body.split(sep):
        head_end = part.find(b"\r\n\r\n")
        if head_end < 0:
            continue
        head = part[:head_end].lower()
        if b"filename=" in head:
            payload = part[head_end + 4:]
            if payload.endswith(b"\r\n"):
                payload = payload[:-2]
            return payload
    return body


def serve(engine: Engine, port: int, host: str) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _json(self, code: int, payload: dict) -> None:
            blob = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def do_GET(self) -> None:  # noqa: N802 - http.server 的约定
            if self.path.rstrip("/") in ("/health", "/healthz"):
                self._json(200, {"ok": True, "load_seconds": engine.load_seconds,
                                 "rss_mb": _rss_mb()})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") not in ("/transcribe", "/v1/audio/transcriptions"):
                self._json(404, {"error": "not found"})
                return
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            ctype = self.headers.get("Content-Type", "")
            if ctype.startswith("multipart/form-data"):
                body = _multipart_body(ctype, body)
            if not body:
                self._json(400, {"error": "empty body"})
                return
            t0 = time.perf_counter()
            try:
                audio, sr, channels = read_wav_bytes(body)
            except Exception as exc:  # noqa: BLE001 - 请求体是用户数据
                self._json(400, {"error": f"cannot decode audio: {exc}"})
                return
            if sr != 16000:
                self._json(400, {"error": f"only 16k mono supported, got {sr}Hz"})
                return
            try:
                result = engine.transcribe(audio, sr)
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
                return
            result["wall_seconds"] = round(time.perf_counter() - t0, 3)
            result["channels"] = channels
            result["rss_mb"] = _rss_mb()
            self._json(200, result)

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"listening on http://{host}:{port}  (POST /transcribe)", flush=True)
    srv.serve_forever()


# --------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="本地 ASR sidecar PoC（SenseVoice + VAD）")
    ap.add_argument("--model-dir", default=os.environ.get("ASR_MODEL_DIR", ""),
                    help="含 model.int8.onnx 与 tokens.txt 的目录")
    ap.add_argument("--vad-model", default=os.environ.get("ASR_VAD_MODEL", ""),
                    help="silero_vad.onnx 路径")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--min-silence", type=float, default=1.2,
                    help="VAD 判定一句结束所需的静音秒数；0.5 会把句内停顿切成两句")
    ap.add_argument("--audio", help="转写这个 wav，打印 JSON")
    ap.add_argument("--serve", action="store_true", help="起本地 HTTP 服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--check", action="store_true", help="只检查依赖与模型文件")
    args = ap.parse_args(argv)

    if not args.model_dir:
        ap.error("需要 --model-dir 或 ASR_MODEL_DIR")
    vad_model = args.vad_model or os.path.join(args.model_dir, "silero_vad.onnx")

    if args.check:
        missing = [
            p for p in (
                os.path.join(args.model_dir, "model.int8.onnx"),
                os.path.join(args.model_dir, "tokens.txt"),
                vad_model,
            ) if not os.path.exists(p)
        ]
        deps = {}
        for mod in ("sherpa_onnx", "soundfile", "numpy"):
            try:
                __import__(mod)
                deps[mod] = "ok"
            except ImportError as exc:
                deps[mod] = f"missing ({exc})"
        print(json.dumps({"missing_files": missing, "deps": deps}, ensure_ascii=False))
        return 0 if not missing and all(v == "ok" for v in deps.values()) else 1

    engine = Engine(args.model_dir, vad_model, args.threads, args.min_silence)
    engine.load()
    print(json.dumps({"loaded": True, "load_seconds": engine.load_seconds,
                      "rss_mb": _rss_mb()}, ensure_ascii=False), file=sys.stderr)

    if args.serve:
        serve(engine, args.port, args.host)
        return 0
    if not args.audio:
        ap.error("需要 --audio 或 --serve")
    audio, sr = read_wav_file(args.audio)
    result = engine.transcribe(audio, sr)
    result["rss_mb"] = _rss_mb()
    result["load_seconds"] = engine.load_seconds
    result["source"] = args.audio
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
