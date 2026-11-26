"""造一份真实 .pptx，用于 P5 验收（PPT 输入能不能出导图）。

不依赖 LibreOffice：python-pptx 直接写 OOXML。内容是"产品设计评审"，
带三级层次（标题页 / 分点页 / 表格页），足以检验 Docling 是否抽出正文与结构。
"""

from pathlib import Path

from pptx import Presentation
from pptx.util import Inches

OUT = Path(__file__).with_name("acceptance_sample.pptx")


def main() -> None:
    prs = Presentation()

    # 第 1 页：标题页
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "智能家居产品设计评审"
    slide.placeholders[1].text = "2026 年 11 月 · 产品与硬件联合评审"

    # 第 2 页：分点页（标题 + 五条要点，检验层级抽取）
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "市场分析"
    body = slide.placeholders[1].text_frame
    body.text = "目标人群：一线城市 25-40 岁有孩家庭"
    for line in (
        "竞品格局：小米、华为、涂鸦三系占据中低端，高端缺位",
        "价格带：整套方案 8000-20000 元，用户对 15000 元以上敏感",
        "增长点：适老化改造需求年增 25%",
        "渠道：线下体验店转化率是线上的 3.2 倍",
    ):
        p = body.add_paragraph()
        p.text = line
        p.level = 0

    # 第 3 页：功能设计（第二级缩进，检验嵌套）
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "功能设计"
    body = slide.placeholders[1].text_frame
    body.text = "中枢网关"
    sub = body.add_paragraph()
    sub.text = "本地语音唤醒，断网可用"
    sub.level = 1
    sub = body.add_paragraph()
    sub.text = "支持 Matter 与 Zigbee 双协议"
    sub.level = 1
    p = body.add_paragraph()
    p.text = "场景引擎"
    p.level = 0
    p = body.add_paragraph()
    p.text = "离家 / 回家 / 睡眠三类内置场景，可自定义"
    p.level = 1

    # 第 4 页：表格页（检验非文本版面）
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "技术难点与风险"
    rows, cols = 4, 2
    table = slide.shapes.add_table(
        rows, cols, Inches(0.6), Inches(1.8), Inches(8.5), Inches(2.2)
    ).table
    data = [
        ("难点", "应对"),
        ("多协议共存时 2.4G 信道拥塞", "网关侧信道调度 + 5G 回传"),
        ("本地语音模型体积过大", "蒸馏到 12MB 的唤醒词模型"),
        ("隐私合规（摄像头数据）", "端侧脱敏，默认不上云"),
    ]
    for r, (a, b) in enumerate(data):
        table.cell(r, 0).text = a
        table.cell(r, 1).text = b

    prs.save(OUT)
    print(f"saved: {OUT} ({OUT.stat().st_size} bytes, {len(prs.slides.__iter__.__self__._sldIdLst)} slides)")


if __name__ == "__main__":
    main()
