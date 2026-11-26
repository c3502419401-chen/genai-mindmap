"""造多模态测量的真实样本（图片 5 张 / 语音 6 个）。

为什么自己造而不是找：验收要的是「真实样本能不能走通」，不是「能不能上传」。
图片必须带结构信息（层级、表格、坐标轴），否则模型即使识别成功也无从生成导图；
语音必须是人声且内容可成图，否则失败原因会混进「素材本身没有可成图的信息」。

图片用 PIL 画（中文用系统字体），语音一部分用仓库里 PoC 时期的真实录音、
一部分用 Windows 中文 TTS 合成后改参数 —— **参数变体是有意的**：采样率与
声道数是服务端硬边界（`app/ingestion/asr.py::_decode_wav`），不试就不知道边界
在哪一侧。

用法：
    python scripts/make_acc_media.py
    python scripts/make_acc_media.py --voice   # 只造语音（TTS 已在外面跑过则跳过）
"""
from __future__ import annotations

import argparse
import array
import math
import random
import shutil
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEDIA = HERE / "acc_media"
SHOTS = HERE.parents[0].parent / ".visioncheck" / "shots"
FONTS = [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simkai.ttf",
         r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\msyhbd.ttc"]


def _font(size: int):
    from PIL import ImageFont

    for path in FONTS:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size, index=0)
            except OSError:
                continue
    return ImageFont.load_default()


def _jitter_draw(draw, points, fill, width=3, jitter=1.6, seed=0):
    """手绘感的线：把每一段拆成小段并加一点随机偏移。"""
    rng = random.Random(seed)
    dense: list[tuple[float, float]] = []
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        steps = max(2, int(math.hypot(x2 - x1, y2 - y1) / 12))
        for i in range(steps):
            t = i / steps
            dense.append((x1 + (x2 - x1) * t + rng.uniform(-jitter, jitter),
                          y1 + (y2 - y1) * t + rng.uniform(-jitter, jitter)))
    draw.line(dense, fill=fill, width=width, joint="curve")


def sketch_map(path: Path) -> None:
    """手绘思维导图草图：中心主题 + 4 条分支 + 若干二级节点。"""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1400, 1000), (253, 252, 248))
    draw = ImageDraw.Draw(image)
    node, small = _font(40), _font(30)
    draw.ellipse([560, 440, 840, 560], outline=(40, 40, 40), width=4)
    draw.text((601, 468), "考研复习", font=node, fill=(30, 30, 30))
    branches = [
        ((560, 500), (330, 300), "数学", [(180, 200, "高数"), (170, 380, "线代")]),
        ((840, 500), (1080, 300), "英语", [(1240, 210, "阅读"), (1250, 380, "作文")]),
        ((560, 520), (330, 720), "专业课", [(180, 800, "数据结构"), (200, 630, "操作系统")]),
        ((840, 520), (1080, 720), "政治", [(1240, 800, "马原"), (1240, 640, "近代史")]),
    ]
    for (sx, sy), (bx, by), label, children in branches:
        _jitter_draw(draw, [(sx, sy), ((sx + bx) / 2, (sy + by) / 2 - 20), (bx, by)],
                     (60, 60, 60), width=4, seed=hash(label) % 999)
        draw.rectangle([bx - 90, by - 32, bx + 90, by + 32], outline=(50, 50, 50), width=3)
        draw.text((bx - 40, by - 22), label, font=node, fill=(30, 30, 30))
        for cx, cy, child in children:
            _jitter_draw(draw, [(bx, by), (cx, cy)], (90, 90, 90), width=2,
                         seed=hash(child) % 999)
            draw.text((cx - 70, cy - 16), child, font=small, fill=(40, 40, 40))
    draw.text((60, 900), "日期：3月-12月　每天 6 小时", font=small, fill=(70, 70, 70))
    image.save(path)


def whiteboard(path: Path) -> None:
    """白板照片：灰白底 + 蓝黑两色笔迹 + 噪点。"""
    from PIL import Image, ImageDraw, ImageFilter

    image = Image.new("RGB", (1500, 1000), (238, 240, 236))
    draw = ImageDraw.Draw(image)
    title, body = _font(46), _font(32)
    draw.text((80, 60), "导图生成流程", font=title, fill=(20, 40, 90))
    boxes = [(90, 200, "输入材料"), (420, 200, "骨架规划"), (750, 200, "结构生成"),
             (1080, 200, "写说明"), (1080, 470, "校验"), (750, 470, "导出")]
    for index, (x, y, label) in enumerate(boxes):
        draw.rounded_rectangle([x, y, x + 240, y + 90], radius=16,
                               outline=(20, 40, 90), width=4)
        draw.text((x + 24, y + 26), label, font=body, fill=(20, 40, 90))
        if index < len(boxes) - 1:
            nx, ny, _ = boxes[index + 1]
            _jitter_draw(draw, [(x + 240, y + 45), (nx, ny + 45)], (20, 40, 90),
                         width=4, seed=index)
    draw.text((90, 620), "约束：", font=body, fill=(160, 30, 30))
    for offset, text in enumerate(["节点数 ≤ 80 保证可读", "说明必须有出处", "失败要交出半棵树"]):
        draw.text((200, 620 + offset * 52), "· " + text, font=body, fill=(160, 30, 30))
    draw.text((90, 830), "负责人：王涛　截止：11/30", font=body, fill=(40, 40, 40))
    image = image.filter(ImageFilter.GaussianBlur(0.6))
    pixels = image.load()
    rng = random.Random(7)
    for _ in range(9000):
        x, y = rng.randrange(1500), rng.randrange(1000)
        base = pixels[x, y]
        delta = rng.randint(-12, 12)
        pixels[x, y] = tuple(max(0, min(255, channel + delta)) for channel in base)
    image.rotate(-1.4, fillcolor=(238, 240, 236)).save(path, quality=88)


def chart(path: Path) -> None:
    """柱状图：学时分配，带坐标轴与数据标签。"""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1200, 800), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    title, label, value = _font(38), _font(28), _font(26)
    draw.text((70, 40), "各章学时分配（共 48 学时）", font=title, fill=(20, 20, 20))
    draw.line([(120, 640), (1120, 640)], fill=(40, 40, 40), width=3)
    draw.line([(120, 120), (120, 640)], fill=(40, 40, 40), width=3)
    data = [("绪论", 2), ("线性表", 6), ("栈与队列", 4), ("树", 8), ("图", 8),
            ("查找", 6), ("排序", 8), ("实验", 6)]
    width = 1080 / len(data)
    for index, (name, hours) in enumerate(data):
        x = 135 + index * width
        height = hours * 52
        draw.rectangle([x, 640 - height, x + width * 0.62, 640], fill=(70, 120, 200))
        draw.text((x + 8, 640 - height - 32), str(hours), font=value, fill=(20, 20, 20))
        draw.text((x + 4, 655), name, font=label, fill=(30, 30, 30))
    for tick in range(0, 11, 2):
        y = 640 - tick * 52
        draw.line([(112, y), (120, y)], fill=(40, 40, 40), width=2)
        draw.text((70, y - 14), str(tick), font=value, fill=(60, 60, 60))
    image.save(path)


def outline_shot(path: Path) -> None:
    """大纲列表截图：多级缩进 + 编号，模拟从 Word 里截出来的教学大纲。"""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1100, 900), (252, 252, 252))
    draw = ImageDraw.Draw(image)
    body = _font(30)
    rows = [
        (0, "《操作系统》课程大纲"),
        (1, "一、概述（4 学时）"),
        (2, "操作系统的定义与作用"),
        (2, "内核态与用户态、系统调用"),
        (1, "二、进程与线程（8 学时）"),
        (2, "进程状态转换与进程控制块"),
        (2, "线程模型：用户级与内核级"),
        (2, "进程间通信：管道、共享内存"),
        (1, "三、调度与并发（6 学时）"),
        (2, "时间片轮转与多级反馈队列"),
        (2, "信号量、管程、死锁与银行家算法"),
        (1, "四、内存管理（8 学时）"),
        (2, "分页、分段、段页式"),
        (2, "页面置换：FIFO、LRU、Clock"),
        (1, "五、文件系统（6 学时）"),
        (2, "文件分配与 inode"),
        (2, "磁盘调度：FCFS、SSTF、SCAN"),
    ]
    for index, (level, text) in enumerate(rows):
        y = 40 + index * 48
        draw.text((60 + level * 60, y), text, font=body,
                  fill=(20, 20, 20) if level <= 1 else (70, 70, 70))
    draw.rectangle([40, 20, 1060, 40 + len(rows) * 48 + 20], outline=(210, 210, 210), width=2)
    image.save(path)


IMAGE_BUILDERS = {
    "i1_sketch_map.png": sketch_map,
    "i2_whiteboard.jpg": whiteboard,
    "i3_chart.png": chart,
    "i4_outline.png": outline_shot,
}


def build_images() -> None:
    folder = MEDIA / "image"
    folder.mkdir(parents=True, exist_ok=True)
    for name, builder in IMAGE_BUILDERS.items():
        builder(folder / name)
        print("image:", name, (folder / name).stat().st_size)
    shot = SHOTS / "chatdock-11-tree.png"
    if shot.exists():
        shutil.copyfile(shot, folder / "i5_product_screenshot.png")
        print("image: i5_product_screenshot.png", (folder / "i5_product_screenshot.png").stat().st_size)
    else:
        print("缺真实截图：", shot)


def _read(path: Path) -> tuple[array.array, int, int]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    return samples, rate, channels


def _write(path: Path, samples: array.array, rate: int, channels: int) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())


def _resample(samples: array.array, rate: int, target: int) -> array.array:
    if rate == target:
        return samples
    ratio = target / rate
    count = int(len(samples) * ratio)
    out = array.array("h", [0]) * count
    for index in range(count):
        pos = index / ratio
        low = int(pos)
        high = min(low + 1, len(samples) - 1)
        frac = pos - low
        out[index] = int(samples[low] * (1 - frac) + samples[high] * frac)
    return out


TTS_TEXTS = {
    "_raw_a.wav": ("本次会议讨论了校园二手交易小程序的立项。背景是校内交易主要靠微信群，"
                   "信息沉底快，无法检索，也缺乏信用机制。目标是一学期覆盖三千名活跃用户，"
                   "月成交八百单，交易纠纷率低于百分之一。功能范围包括发布、检索、站内私信和实名认证。"),
    "_raw_b.wav": ("故障复盘结论如下。故障持续四十八分钟，影响三千二百名用户。根因是新增的"
                   "分页接口没有走索引，在六万行表上做全表排序，单次耗时八秒，前端每次渲染"
                   "触发三次请求，最终把数据库连接池打满。改进项包括补复合索引、列表请求合并"
                   "加防抖、压测覆盖全部读接口、慢查询告警阈值改成一秒。"),
    "_raw_c.wav": ("数据结构与算法课程共四十八学时。第一章绪论讲算法与复杂度分析。第二章"
                   "线性表讲顺序存储与链式存储。第三章栈与队列讲表达式求值与循环队列。"
                   "第五章树与二叉树讲遍历与哈夫曼编码。第六章图讲最小生成树与最短路径。"
                   "第七章查找讲二叉排序树与散列表。"),
}


def build_tts() -> None:
    """用 Windows 自带的中文 TTS 造三段真实可识别的语音（16k/16bit/单声道）。

    为什么不用仓库里那两段 PoC 录音代替：那两段内容固定，只够测一遍格式；
    验收要的是"不同内容、不同参数的语音样本"，得先有可控的内容来源。
    合成音不是真人，但它有完整的音素与语义，对"能不能转成文本再成图"这个问题
    足够 —— 而且**可重建**：不把二进制样本提交进仓库，重跑一次就有。
    """
    import subprocess

    folder = MEDIA / "audio"
    folder.mkdir(parents=True, exist_ok=True)
    todo = {name: text for name, text in TTS_TEXTS.items()
            if not (folder / name).exists()}
    if not todo:
        return
    parts = ["Add-Type -AssemblyName System.Speech"]
    for name, text in todo.items():
        target = str(folder / name)
        parts.append(
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$s.SelectVoice('Microsoft Huihui Desktop'); $s.Rate = 0; "
            f"$s.SetOutputToWaveFile('{target}', (New-Object "
            "System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, "
            "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
            "[System.Speech.AudioFormat.AudioChannel]::Mono))); "
            "$s.Speak('" + text + "'); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", "; ".join(parts)],
                   check=True)
    for name in todo:
        print("tts:", name, (folder / name).stat().st_size)


def build_audio() -> None:
    folder = MEDIA / "audio"
    folder.mkdir(parents=True, exist_ok=True)
    build_tts()
    poc = HERE / "asr_poc_sample"
    # 1 / 2：仓库里 PoC 时期的真实录音（16k 单声道，8 句内容多样）
    for src, dst in ((poc / "line00.wav", "a1_real_line00.wav"),
                     (poc / "long_8lines.wav", "a2_real_long.wav")):
        if src.exists():
            shutil.copyfile(src, folder / dst)
            print("audio:", dst, (folder / dst).stat().st_size)
    raw_a = folder / "_raw_a.wav"
    raw_b = folder / "_raw_b.wav"
    raw_c = folder / "_raw_c.wav"
    if raw_a.exists():                     # 3：TTS 16k 单声道（服务端的标准形态）
        shutil.copyfile(raw_a, folder / "a3_tts_16k_mono.wav")
        print("audio: a3_tts_16k_mono.wav")
    if raw_b.exists():                     # 4：44.1k —— 采样率在支持列表之外
        samples, rate, _ = _read(raw_b)
        _write(folder / "a4_tts_44k1_mono.wav", _resample(samples, rate, 44100), 44100, 1)
        print("audio: a4_tts_44k1_mono.wav")
    if raw_c.exists():                     # 5：16k 双声道（应支持，服务端会拆声道）
        samples, rate, _ = _read(raw_c)
        stereo = array.array("h")
        for sample in samples:
            stereo.append(sample)
            stereo.append(sample)
        _write(folder / "a5_tts_16k_stereo.wav", stereo, rate, 2)
        print("audio: a5_tts_16k_stereo.wav")
    if raw_a.exists():                     # 6：非 WAV 扩展名（格式面）
        shutil.copyfile(raw_a, folder / "a6_tts_as_mp3.mp3")
        print("audio: a6_tts_as_mp3.mp3")
    for name in ("_raw_a.wav", "_raw_b.wav", "_raw_c.wav"):
        (folder / name).unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice", action="store_true")
    parser.add_argument("--image", action="store_true")
    args = parser.parse_args(argv)
    if args.voice or not args.image:
        build_audio()
    if args.image or not args.voice:
        build_images()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
