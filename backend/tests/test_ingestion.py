"""文档导入的边界行为。

真实的 Docling 解析很慢（首次要加载模型），所以这里只测不触发解析的路径：
格式白名单、空文件、文本直通。实际解析效果由手工端到端验证覆盖。
"""
import unittest

from app.contracts.source import SourceKind
from app.ingestion.service import (
    DOCUMENT_SUFFIXES,
    MAX_PAGES,
    NotSupportedSource,
    parse_bytes,
    parse_document_bytes,
    parse_text,
)


class TextSourceTest(unittest.TestCase):
    def test_text_passes_through(self):
        source = parse_text("  一段文本  ")
        self.assertEqual(source.text, "一段文本")
        self.assertEqual(source.status, "parsed")

    def test_blank_text_rejected(self):
        with self.assertRaises(NotSupportedSource):
            parse_text("   ")

    def test_text_endpoint_rejects_document_kind(self):
        """文本入口不该接受 document 类型，避免绕过格式检查。"""
        with self.assertRaises(NotSupportedSource):
            parse_text("x", SourceKind.document)


class TextDocumentPassthroughTest(unittest.TestCase):
    """`.md` / `.adoc` 直通：**不进 Docling**。

    这是"文件上传卡在正在解析"那条缺陷的回归用例（`docs/ACCEPTANCE-WALKTHROUGH-2026-11-28.md`
    §四.2 的现场：548 字 `.md`，40 秒仍在解析）。断言方式不是"跑得快"——CI 机器的
    速度不可控，而且那种断言会在fast 机器上漏掉回归。这里钉的是**结构**：把
    `_get_converter` 换成"一被调用就炸"的桩，正常返回即证明这条路绕开了 Docling
    的冷启动（实测 102.6 秒）与 `_parse_lock` 串行队列。
    """

    def test_markdown_never_initializes_docling(self):
        from unittest.mock import patch

        import app.ingestion.service as service

        raw = "# 产品需求评审会纪要\n\n## 一、评审范围\n拖拽改父级。\n".encode()
        with patch.object(service, "_get_converter",
                          side_effect=AssertionError("`.md` 不该初始化 Docling")):
            source = parse_document_bytes("产品需求评审会.md", raw)
        self.assertEqual(source.status, "parsed")
        self.assertEqual(source.kind, SourceKind.document)
        self.assertEqual(source.filename, "产品需求评审会.md")
        self.assertIn("拖拽改父级", source.text)

    def test_asciidoc_takes_the_same_path(self):
        from unittest.mock import patch

        import app.ingestion.service as service

        raw = "= 标题\n\n== 小节\n正文一段。\n".encode()
        with patch.object(service, "_get_converter",
                          side_effect=AssertionError("`.adoc` 不该初始化 Docling")):
            source = parse_document_bytes("笔记.adoc", raw)
        self.assertIn("正文一段", source.text)

    def test_paragraph_breaks_survive(self):
        """Docling 的 `export_to_markdown()` 会把段落间的单换行合并成一行；
        直通必须保住它们——每一行就是导图里的一个知识点。"""
        source = parse_document_bytes("s.md", "第一段\n第二段\n第三段\n".encode())
        self.assertEqual(source.text, "第一段\n第二段\n第三段")

    def test_gbk_source_is_decoded_not_rejected(self):
        """中文 Windows 上「另存为 UTF-8」经常得到 GB18030；那不是坏文件。"""
        source = parse_document_bytes("纪要.md", "产品评审会\n".encode("gb18030"))
        self.assertIn("产品评审会", source.text)

    def test_blank_text_document_rejected(self):
        with self.assertRaises(NotSupportedSource):
            parse_document_bytes("空.md", "  \n\n  \n".encode())


class MarkdownCleanupTest(unittest.TestCase):
    """解析结果转文本时的清洗规则。"""

    def test_image_alt_is_kept_url_dropped(self):
        from app.ingestion.service import _normalize_markdown
        out = _normalize_markdown("正文\n\n![图 3 系统架构](http://x/y.png)\n\n后续")
        self.assertIn("[图: 图 3 系统架构]", out)
        self.assertNotIn("http://x/y.png", out)

    def test_image_without_alt_disappears(self):
        from app.ingestion.service import _normalize_markdown
        out = _normalize_markdown("正文\n\n![](http://x/y.png)\n\n后续")
        self.assertNotIn("http://x/y.png", out)
        self.assertNotIn("[图:", out)

    def test_blank_lines_are_squeezed(self):
        from app.ingestion.service import _normalize_markdown
        self.assertEqual(_normalize_markdown("a\n\n\n\n\nb"), "a\n\nb")


class DocumentGuardTest(unittest.TestCase):
    """这些分支在真正调用 Docling 之前就返回，跑得很快。"""

    def test_unsupported_suffix_rejected(self):
        with self.assertRaises(NotSupportedSource) as raised:
            parse_document_bytes("virus.exe", b"MZ\x90\x00")
        self.assertIn("暂不支持", str(raised.exception))

    def test_no_suffix_rejected(self):
        with self.assertRaises(NotSupportedSource):
            parse_document_bytes("README", b"hello")

    def test_empty_file_rejected(self):
        with self.assertRaises(NotSupportedSource) as raised:
            parse_document_bytes("a.pdf", b"")
        self.assertIn("为空", str(raised.exception))

    def test_supported_suffixes_include_core_formats(self):
        for suffix in (".pdf", ".docx", ".pptx"):
            self.assertIn(suffix, DOCUMENT_SUFFIXES)

    def test_legacy_office_formats_are_supported(self):
        """旧格式现在支持：.doc/.ppt 经 LibreOffice 转 PDF 再走视觉提取，
        .xls 用 xlrd 直读。依赖缺失时会报错并给出安装命令，不静默降级。"""
        for suffix in (".doc", ".ppt", ".xls"):
            with self.subTest(suffix=suffix):
                self.assertIn(suffix, DOCUMENT_SUFFIXES)

    def test_bytes_dispatch_by_kind(self):
        """parse_bytes 会把 document 交给文档解析器，而不是当成文本。"""
        with self.assertRaises(NotSupportedSource) as raised:
            parse_bytes(SourceKind.document, "a.exe", b"x")
        self.assertIn("暂不支持", str(raised.exception))

    def test_image_and_audio_still_pending(self):
        for kind in (SourceKind.image, SourceKind.audio):
            with self.assertRaises(NotSupportedSource):
                parse_bytes(kind, None, b"x")


def make_pdf(pages: int) -> bytes:
    """生成一份带文本层的 PDF，用于测分页与截断。"""
    import pymupdf

    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"page {index + 1} content")
    raw = doc.tobytes()
    doc.close()
    return raw


class PdfPageLimitTest(unittest.TestCase):
    """超过 MAX_PAGES 时必须显式标注截断，不能静默丢内容。

    验收时上传 51 页 PDF，接口返回成功但只有前 50 页进了正文，
    响应里没有任何字段说明这件事——用户拿到的是一份缺了尾巴的正文。
    """

    def test_within_limit_is_not_truncated(self):
        source = parse_document_bytes("small.pdf", make_pdf(5))
        self.assertEqual(source.parsed_pages, 5)
        self.assertFalse(source.truncated)
        self.assertEqual(source.span.page, 5)
        self.assertIn("page 1 content", source.text)

    def test_exactly_at_limit_is_not_truncated(self):
        source = parse_document_bytes("edge.pdf", make_pdf(MAX_PAGES))
        self.assertEqual(source.parsed_pages, MAX_PAGES)
        self.assertFalse(source.truncated)
        self.assertEqual(source.span.page, MAX_PAGES)

    def test_over_limit_is_marked_truncated(self):
        over = MAX_PAGES + 1
        source = parse_document_bytes("big.pdf", make_pdf(over))

        # 正文只到第 MAX_PAGES 页
        self.assertEqual(source.parsed_pages, MAX_PAGES)
        self.assertIn(f"page {MAX_PAGES} content", source.text)
        self.assertNotIn(f"page {over} content", source.text)

        # 但必须明确告诉调用方：总共多少页、解析了多少页、内容被截断了
        self.assertEqual(source.span.page, over)
        self.assertTrue(source.truncated)
        self.assertEqual(source.status, "parsed")


class PdfVisionTest(unittest.TestCase):
    """PDF 主路径是视觉提取，文本层只在模型不可用时兜底。"""

    def test_vision_output_is_used(self):
        from unittest.mock import patch

        with patch("app.ingestion.vision_pdf.extract_pages") as extract:
            extract.return_value = (["# 第一章\n\n正文内容", "## 1.1 小节\n\n更多"], 2, 2, [])
            source = parse_document_bytes("a.pdf", make_pdf(2))
        self.assertIn("# 第一章", source.text)
        self.assertIn("## 1.1 小节", source.text)
        self.assertFalse(source.truncated)
        self.assertEqual(source.parsed_pages, 2)
        self.assertEqual(source.span.page, 2)

    def test_vision_failure_falls_back_to_text_layer(self):
        from unittest.mock import patch

        from app.ingestion.vision_pdf import VisionUnavailable

        with patch("app.ingestion.vision_pdf.extract_pages") as extract:
            extract.side_effect = VisionUnavailable("未配置模型地址")
            source = parse_document_bytes("a.pdf", make_pdf(3))
        # 回落到 pymupdf 抽出的文本层
        self.assertIn("page 1 content", source.text)
        self.assertEqual(source.status, "parsed")

    def test_vision_returns_nothing_falls_back(self):
        from unittest.mock import patch

        with patch("app.ingestion.vision_pdf.extract_pages") as extract:
            extract.return_value = (["", ""], 2, 2, [1, 2])
            source = parse_document_bytes("a.pdf", make_pdf(2))
        self.assertIn("page 1 content", source.text)

    def test_vision_failure_without_text_layer_raises(self):
        from unittest.mock import patch

        import pymupdf

        from app.ingestion.vision_pdf import VisionUnavailable

        # 造一份纯图片 PDF：整页是一张图，没有文本层
        doc = pymupdf.open()
        page = doc.new_page()
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
        pix.clear_with(200)
        page.insert_image(pymupdf.Rect(50, 50, 250, 250), pixmap=pix)
        raw = doc.tobytes()
        doc.close()

        with patch("app.ingestion.vision_pdf.extract_pages") as extract:
            extract.side_effect = VisionUnavailable("网络不通")
            with self.assertRaises(NotSupportedSource) as raised:
                parse_document_bytes("scan.pdf", raw)
        self.assertIn("没有文本层", str(raised.exception))

    def test_failed_page_is_filled_from_text_layer(self):
        """视觉识别失败的页，若同一页有文本层就用它补回来。

        原 PDF 有文本层说明这页本来就有文字，只是渲染或模型那侧出了问题；
        直接丢掉等于白丢一页正文。
        """
        from unittest.mock import patch

        with patch("app.ingestion.vision_pdf.extract_pages") as extract:
            # 3 页里第 2 页识别失败
            extract.return_value = (["# 第一页", "", "# 第三页"], 3, 3, [2])
            source = parse_document_bytes("a.pdf", make_pdf(3))

        self.assertIn("# 第一页", source.text)
        self.assertIn("# 第三页", source.text)
        # 第 2 页由文本层补回：make_pdf 每页写的是 "page N content"
        self.assertIn("page 2 content", source.text)
        self.assertEqual(source.failed_pages, [])
        self.assertFalse(source.truncated)
        self.assertEqual(source.parsed_pages, 3)

    def test_unrecoverable_page_is_reported(self):
        """既识别失败又没有文本层时，页码要明确报出来，不能只标一个 truncated。"""
        from unittest.mock import patch

        with patch("app.ingestion.service._parse_pdf", return_value=(["", "", ""], 3)), \
             patch("app.ingestion.vision_pdf.extract_pages") as extract:
            extract.return_value = (["# 第一页", "", "# 第三页"], 3, 3, [2])
            source = parse_document_bytes("a.pdf", b"%PDF-fake")

        self.assertIn("# 第一页", source.text)
        self.assertEqual(source.failed_pages, [2])
        # 页数没超上限，所以不算"截断"——这两个信号必须分开
        self.assertFalse(source.truncated)
        # parsed_pages 是处理边界（3 页都处理了），不是"有内容的页数"
        self.assertEqual(source.parsed_pages, 3)
        self.assertEqual(source.span.page, 3)

    def test_partial_failure_does_not_fake_truncated_hint(self):
        """3 页里 1 页失败，不能给出"共 3 页仅处理前 3 页"这种自相矛盾的组合。"""
        from unittest.mock import patch

        with patch("app.ingestion.service._parse_pdf", return_value=(["", "", ""], 3)), \
             patch("app.ingestion.vision_pdf.extract_pages") as extract:
            extract.return_value = (["# 一", "", "# 三"], 3, 3, [2])
            source = parse_document_bytes("a.pdf", b"%PDF-fake")

        # truncated 为假时前端不渲染"仅处理前 N 页"，缺页改由 failed_pages 说明
        self.assertFalse(source.truncated)
        self.assertEqual(source.failed_pages, [2])
        self.assertEqual(source.span.page, 3)
        # 处理边界是 3（全部处理过），但正文里只有两页的内容
        self.assertEqual(source.parsed_pages, 3)
        self.assertNotIn("page 2", source.text or "")

    def test_fallback_reports_pages_without_text_layer(self):
        """视觉整体不可用、退回文本层时，文本层里空白的页同样要报出来。"""
        from unittest.mock import patch

        # 3 页里第 2 页没有文本层（扫描页），另外两页有
        with patch("app.ingestion.service._parse_pdf",
                   return_value=(["page 1 text", "", "page 3 text"], 3)):
            source = parse_document_bytes("a.pdf", b"%PDF-fake")

        self.assertIn("page 1 text", source.text)
        self.assertEqual(source.failed_pages, [2])
        self.assertEqual(source.parsed_pages, 3)
        self.assertFalse(source.truncated)

    def test_page_limit_marks_truncated(self):
        from unittest.mock import patch

        with patch("app.ingestion.vision_pdf.extract_pages") as extract:
            # 25 页的文档，视觉只处理前 20 页
            extract.return_value = (["# 内容"] * 20, 25, 20, [])
            source = parse_document_bytes("a.pdf", make_pdf(25))
        self.assertEqual(source.span.page, 25)
        self.assertEqual(source.parsed_pages, 20)
        self.assertTrue(source.truncated)


class PdfVisionConcurrencyTest(unittest.TestCase):
    """逐页识别并发之后，Source 这一层仍按页码拼正文、按页码报失败。

    这两件事是下游引用正确性的前提：页码对不上，节点就会指到错误的页，
    而导图本身看起来完全正常。
    """

    def _parse_scan(self, pages: int, fake):
        """把一份 PDF 当扫描件解析：没有文本层可用，逐页结果由 fake 决定。"""
        from unittest.mock import patch

        with patch("app.ingestion.vision_pdf._config",
                   return_value=("http://example.invalid/v1", "k", "m")), \
             patch("app.ingestion.service._parse_pdf",
                   return_value=([""] * pages, pages)), \
             patch("app.ingestion.vision_pdf._page_to_markdown", side_effect=fake):
            return parse_document_bytes("scan.pdf", make_pdf(pages))

    def test_pages_are_joined_in_page_order(self):
        import time

        def fake(image_png, page_no):
            # 页码越小越慢：完成顺序与页码顺序相反
            time.sleep((7 - page_no) * 0.01)
            return f"# 第 {page_no} 页标题"

        source = self._parse_scan(6, fake)

        positions = [source.text.index(f"# 第 {page_no} 页标题") for page_no in range(1, 7)]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(source.failed_pages, [])
        self.assertEqual(source.parsed_pages, 6)
        self.assertFalse(source.truncated)

    def test_failed_page_is_reported_others_survive(self):
        def fake(image_png, page_no):
            if page_no == 2:
                raise RuntimeError("模型返回 500")
            return f"# 第 {page_no} 页标题"

        source = self._parse_scan(4, fake)

        self.assertEqual(source.failed_pages, [2])
        self.assertIn("# 第 1 页标题", source.text)
        self.assertIn("# 第 4 页标题", source.text)
        self.assertNotIn("# 第 2 页标题", source.text)
        # 4 页都处理过，所以不是"截断"；缺的是第 2 页，由 failed_pages 表达
        self.assertEqual(source.parsed_pages, 4)
        self.assertFalse(source.truncated)


class LegacyOfficeTest(unittest.TestCase):
    """旧格式的三条链路：.doc/.ppt 转 PDF 后走视觉，.xls 用 xlrd。"""

    def test_doc_without_libreoffice_reports_install_hint(self):
        """没装 LibreOffice 时要给出安装命令，而不是笼统的"解析失败"。"""
        from unittest.mock import patch

        with patch("app.ingestion.legacy_office.soffice_cmd", return_value=None):
            with self.assertRaises(NotSupportedSource) as raised:
                parse_document_bytes("a.doc", b"\xd0\xcf\x11\xe0fake")
        message = str(raised.exception)
        self.assertIn("LibreOffice", message)
        self.assertIn("apt install", message)

    def test_ppt_conversion_failure_is_reported(self):
        from unittest.mock import patch

        from app.ingestion.legacy_office import LegacyFormatUnavailable

        with patch("app.ingestion.legacy_office.convert_to_pdf",
                   side_effect=LegacyFormatUnavailable("转换超时")):
            with self.assertRaises(NotSupportedSource) as raised:
                parse_document_bytes("a.ppt", b"\xd0\xcf\x11\xe0fake")
        self.assertIn("转换超时", str(raised.exception))

    def test_converted_pdf_goes_through_vision(self):
        """转换成功后应当复用 PDF 的视觉提取链路。"""
        from unittest.mock import patch

        with patch("app.ingestion.legacy_office.convert_to_pdf",
                   return_value=make_pdf(2)) as convert, \
             patch("app.ingestion.vision_pdf.extract_pages") as extract:
            extract.return_value = (["# 第一章", "## 1.1"], 2, 2, [])
            source = parse_document_bytes("a.doc", b"\xd0\xcf\x11\xe0fake")
        convert.assert_called_once()
        self.assertIn("# 第一章", source.text)
        self.assertEqual(source.span.page, 2)

    def test_xls_cell_formatting(self):
        """日期的序列号、整数的多余小数都要在输出前处理掉。"""
        from app.ingestion.legacy_office import _cell_text

        class Cell:
            def __init__(self, ctype, value):
                self.ctype = ctype
                self.value = value

        import xlrd

        book = type("Book", (), {"datemode": 0})()
        self.assertEqual(_cell_text(book, Cell(xlrd.XL_CELL_NUMBER, 12.0)), "12")
        self.assertEqual(_cell_text(book, Cell(xlrd.XL_CELL_NUMBER, 3.5)), "3.5")
        self.assertEqual(_cell_text(book, Cell(xlrd.XL_CELL_TEXT, "苹果")), "苹果")
        self.assertEqual(_cell_text(book, Cell(xlrd.XL_CELL_EMPTY, "")), "")
        self.assertEqual(_cell_text(book, Cell(xlrd.XL_CELL_BOOLEAN, 1)), "TRUE")
        # 46096 是 Excel 日期序列号，对应 2026-03-15
        self.assertEqual(_cell_text(book, Cell(xlrd.XL_CELL_DATE, 46096.0)), "2026-03-15")

    def test_xls_empty_content_is_rejected(self):
        from unittest.mock import patch

        from app.ingestion.legacy_office import LegacyFormatUnavailable

        with patch("app.ingestion.legacy_office.read_xls",
                   side_effect=LegacyFormatUnavailable("没有可读的单元格")):
            with self.assertRaises(NotSupportedSource):
                parse_document_bytes("a.xls", b"\xd0\xcf\x11\xe0fake")


    def test_truncated_page_output_is_treated_as_failure(self):
        """单页输出撞上 max_tokens 时不能当成功——半页正文收下就等于永久缺一截。"""
        from unittest.mock import patch

        import httpx

        from app.ingestion.vision_pdf import VisionUnavailable, _page_to_markdown

        fake = httpx.Response(200, json={
            "choices": [{"message": {"content": "ONLY THE FIRST HALF"},
                         "finish_reason": "length"}],
        })

        with patch("app.ingestion.vision_pdf._config",
                   return_value=("https://example.com/v1", "k", "m")), \
             patch("httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.post.return_value = fake
            with self.assertRaises(VisionUnavailable) as raised:
                _page_to_markdown(b"\x89PNG-fake", 3)

        self.assertIn("截断", str(raised.exception))

    def test_complete_page_output_is_accepted(self):
        from unittest.mock import patch

        import httpx

        from app.ingestion.vision_pdf import _page_to_markdown

        fake = httpx.Response(200, json={
            "choices": [{"message": {"content": "## 标题\n\n正文"},
                         "finish_reason": "stop"}],
        })

        with patch("app.ingestion.vision_pdf._config",
                   return_value=("https://example.com/v1", "k", "m")), \
             patch("httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.post.return_value = fake
            self.assertIn("标题", _page_to_markdown(b"\x89PNG-fake", 1))


if __name__ == "__main__":
    unittest.main()
