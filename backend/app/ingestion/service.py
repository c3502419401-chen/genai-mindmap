"""多模态输入解析（F05 / F06 / F07 / F08）。

流程位置：
    上传内容 ──parse()──> Source(text) ──> agent 请求的 sources

当前实现状态：
    text / markdown   已实现（解码 + 空白规整）
    document          PDF 走 pymupdf；DOCX / PPTX / XLSX / HTML 走 Docling
    image             视觉模型直接转 Markdown，与 PDF 逐页共用多模态调用
    audio             阿里云一句话识别，超 60 秒自动切片，见 app/ingestion/asr.py

**PDF 为什么不用 Docling**：它的版面模型常驻 1.2GB、实测峰值 1.6–2.8GB，
2C2G 的部署机会被 OOM 直接杀掉。PDF 改用 pymupdf 只读文本层，30 页中文文档
0.38 秒 / 66MB。章节层级交给下游的模型去认，实测比版面模型更准——完整对比
见 docs/PDF-PARSER-SELECTION.md 与 docs/LLM-OUTLINE-ACCURACY.md。

**其余格式仍然走 Docling**：DOCX / PPTX / XLSX / HTML 走的是它的非版式
流水线，不加载 PDF 版面模型，内存开销可以忽略，没有理由换掉。

旧格式 .doc / .ppt / .xls 现已支持，但走的路不同：

    .doc / .ppt   交给 LibreOffice 渲染成 PDF，再复用 PDF 的视觉提取。
                  纯 Python 抽不全——实测 .ppt 只能捞出字体名和母版占位符，
                  正文与表格根本拿不到。
    .xls          xlrd 直接读单元格。它是行列结构，读出来又快又准，
                  没必要绕一圈渲染再识别。

前者需要安装 LibreOffice（约 1.5GB 磁盘），后者需要 xlrd（约 100KB）。
缺依赖时报错文案会给出安装命令，不会静默降级成空内容。

未实现的格式统一抛 NotSupportedSource，由路由层转成 422，
不要返回空 Source 假装成功。

**并发模型（改这里之前先看这段）**：

    靠网络的（PDF 逐页视觉识别、语音转写）并发
    本地 CPU 的（pymupdf 抽文本层、Docling 转结构化格式）串行

分工理由很简单：网络调用绝大部分时间在等回包，并发能把延迟叠起来；
本地 CPU 在 2C2G 上并起来只会互相抢核，总耗时不变而上下文切换变多。

具体落点：
    - PDF 逐页视觉   vision_pdf.extract_pages 内部按页并发（CONCURRENCY 路），
                     渲染仍在同一函数里串行（pymupdf 的 Document 非线程安全）
    - 语音           asr.transcribe_audio 内部双声道并发（MAX_WORKERS 路）
    - 图片           单张没有内部并行；多张之间的并发由调用方负责
                     （前端上传队列按类型分组，见 frontend/src/stores/sources.ts）
    - Docling        _parse_lock 串行：DocumentConverter 是进程内单例，
                     多线程同时调用它内部状态不安全
"""
from __future__ import annotations

import hashlib
import html
import re
import threading

from app.contracts.source import Source, SourceKind, SourceSpan
from app.ingestion import metadata as source_metadata
from app.ingestion.asr import AsrError, transcribe_audio

# 单份文档最多交给 Docling 解析的页数。
#
# 原来定 50，理由是「导图容纳不了几百页的细节」——那个理由现在不成立了：
# 契约已经放到 1000 节点，模型上下文实测能吞 60 万 token。所以这里不再是
# 内容边界，只是**防止 Docling 转换本身跑太久**的护栏：它是本地 CPU 密集
# 转换，几百页要几分钟，而且内存随页数线性涨。
# 真正的长度限制在下游（`ensure_input_within_context`），按 token 算。
MAX_PAGES = 300

# 接受解析的文档扩展名。
#
# 分三类，走三条不同的解析链路：
#   PDF                      视觉提取（逐页渲染 → 多模态模型）
#   DOCX/PPTX/XLSX/HTML/MD   Docling 读结构化格式，不需要版面模型
#   DOC/PPT/XLS              旧版二进制：前两者经 LibreOffice 转 PDF 后走视觉提取，
#                            .xls 用 xlrd 直读单元格
DOCUMENT_SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx",
                     ".html", ".htm", ".md", ".asciidoc", ".adoc",
                     ".doc", ".ppt", ".xls"}

# 需要先经 LibreOffice 渲染成 PDF 的旧格式
LEGACY_RENDER_SUFFIXES = {".doc", ".ppt"}

# 语音来源只收 WAV。云端一句话识别本身支持 mp3/aac/amr/opus，但解码这些
# 要拖进 ffmpeg（2C2G 上又多一个系统依赖），而浏览器录音和 decodeAudioData
# 都能直接产出 16k 单声道 WAV，没必要为边缘格式付出这个代价。
AUDIO_SUFFIXES = {".wav"}

# 图片只收视觉模型能解码的常见位图。SVG 故意不收：它是矢量描述，模型收到的
# 是一段 XML 而不是画面，识别不出任何东西，收进来只会让用户困惑为什么解析
# 结果是空的。
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

_converter = None
_converter_lock = threading.Lock()
# 解析串行化：DocumentConverter 是进程内单例，多线程同时调用它内部状态不安全。
# 两个用户的项目里排队解析可以接受，换来的是不会偶发解析错乱。
_parse_lock = threading.Lock()


class NotSupportedSource(Exception):
    """该来源类型已规划但尚未实现，或格式不被支持。"""


def parse_text(text: str, kind: SourceKind = SourceKind.text) -> Source:
    """纯文本直通，去掉首尾空白；空串视为失败。"""
    if kind not in (SourceKind.text, SourceKind.markdown):
        raise NotSupportedSource("文本入口仅支持 text 或 markdown")
    cleaned = (text or "").strip()
    if not cleaned:
        raise NotSupportedSource("文本为空")
    return Source(kind=kind, text=cleaned, status="parsed")


def _suffix_of(filename: str | None) -> str:
    if not filename:
        return ""
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot >= 0 else ""


def _parse_pdf(raw: bytes) -> tuple[list[str], int]:
    """PyMuPDF 抽文本，返回 (正文, 总页数, 实际解析页数)。

    只读文本层，不碰版面：实测 15 页英文双栏论文 0.19 秒 / 55MB，
    30 页中文文档 0.38 秒 / 66MB。扫描件抽不出文字，这里会返回空正文，
    由调用方拒绝。

    返回逐页文本而不是拼好的一个串：视觉提取可能有个别页失败，那时要用
    同一页的文本层补上。整份拼接后就无法按页取了。

    总页数与已取页数分开返回：超过 MAX_PAGES 时只取前面那些页，
    调用方要据此告诉用户"这份文档被截断了"。
    """
    import pymupdf

    doc = pymupdf.open(stream=raw, filetype="pdf")
    try:
        page_count = doc.page_count
        readable_pages = min(page_count, MAX_PAGES)
        chunks = []
        for index in range(readable_pages):
            try:
                chunks.append(doc[index].get_text("text") or "")
            except Exception:  # noqa: BLE001 - 单页损坏不该拖垮整份文档
                chunks.append("")
    finally:
        doc.close()
    return chunks, page_count


def _parse_pdf_source(filename: str | None, raw: bytes) -> Source:
    """PDF 一律走视觉提取：逐页渲染成图交给多模态模型转 Markdown。

    为什么不再用文本层做主路径：文本层只有字符，表格退化成散行、公式丢结构、
    字号层级全无，扫描件更是一个字符都拿不到。视觉模型读整页能同时拿到文字、
    表格行列、公式与标题层级。

    文本层仍然会被读一遍，但只为兜底：模型服务不可用（没配密钥、网络不通、
    整份识别失败）时，有文本层的文档还能降级给出内容，不至于直接不可用。
    """
    from app.ingestion import vision_pdf

    # 元数据先取：它与正文解析完全独立，视觉那条路失败要回退文本层时也不用重取。
    # 传的是 raw 而不是 filename 判断格式——.doc/.ppt 转成 PDF 后也走这个函数，
    # 那时后缀还是旧格式，但内容已经是 PDF 信息字典了。
    metadata = source_metadata.from_pdf(raw)

    # 兜底用，同时也是"这份文档到底有没有文字"的判断依据
    try:
        text_pages, page_count = _parse_pdf(raw)
    except ImportError as exc:  # pragma: no cover - 只在缺依赖的部署上触发
        raise NotSupportedSource(
            "PDF 解析需要 pymupdf，当前环境没有安装；请执行 "
            "pip install pymupdf 后重启服务") from exc
    except Exception as exc:  # noqa: BLE001 - pymupdf 的异常类型不稳定
        raise NotSupportedSource(f"PDF 解析失败：{exc}") from exc

    def fallback(reason: str) -> Source:
        """视觉不可用时退回文本层；文本层也空就只能报错。"""
        usable = [page for page in text_pages if page.strip()]
        text = _normalize_markdown("\n\n".join(usable))
        if not text:
            raise NotSupportedSource(
                f"{reason}；这份 PDF 也没有文本层可以退回（可能是扫描件），"
                "请确认模型服务可用后重试")
        # 文本层里空白的页也要报出来：整份都是扫描页与"中间夹了几页扫描页"
        # 是两种不同的残缺，用户需要知道到底缺哪几页。
        empty = [index + 1 for index, page in enumerate(text_pages) if not page.strip()]
        return Source(kind=SourceKind.document, filename=filename, text=text,
                      span=SourceSpan(page=page_count) if page_count else None,
                      parsed_pages=len(text_pages),
                      truncated=len(text_pages) < page_count,
                      failed_pages=empty,
                      metadata=metadata,
                      status="parsed")

    try:
        pages, total, limit, failed = vision_pdf.extract_pages(raw)
    except vision_pdf.VisionUnavailable as exc:
        return fallback(f"视觉提取不可用（{exc}）")
    except Exception as exc:  # noqa: BLE001 - 渲染/并发层的意外错误
        return fallback(f"视觉提取失败（{exc}）")

    # 识别失败的页先拿同一页的文本层补回来。原 PDF 有文本层就说明这页
    # 本来就有文字，只是因为渲染/模型那一侧出了问题才没拿到——直接丢掉等于
    # 白丢一页正文。补不回来的才计入 failed_pages 告诉用户。
    unrecovered: list[int] = []
    for page_no in failed:
        index = page_no - 1
        if index < len(text_pages) and text_pages[index].strip():
            pages[index] = text_pages[index]
        else:
            unrecovered.append(page_no)

    usable = [page for page in pages if page.strip()]
    if not usable:
        return fallback("所有页面都没能识别出来")

    text = _normalize_markdown("\n\n".join(page.strip() for page in usable))
    if not text:
        return fallback("识别结果里没有可用文字")

    # 两个信号分开表达，含义不同：
    #   truncated    —— 页数超过上限，第 limit+1 页往后根本没处理
    #   failed_pages —— 处理过的页里有几页彻底没拿到内容（中间有洞）
    #
    # parsed_pages 表示**处理边界**（最多处理到第几页），不是"有内容的页数"。
    # 前端拿它渲染「共 N 页，仅处理前 M 页」，用成功页数会算出偏小的边界
    # ——比如前 20 页里第 2 页失败，成功 19 页，就会误报"仅处理前 19 页"，
    # 而第 20 页其实处理过。缺哪几页由 failed_pages 表达。
    return Source(kind=SourceKind.document, filename=filename, text=text,
                  span=SourceSpan(page=total) if total else None,
                  parsed_pages=limit,
                  truncated=limit < total,
                  failed_pages=unrecovered,
                  metadata=metadata,
                  status="parsed")


def _get_converter():
    """Docling 的 DocumentConverter 初始化要十几秒，全进程复用一份。

    用锁保护，避免并发请求同时初始化两份。
    """
    global _converter
    if _converter is None:
        with _converter_lock:
            if _converter is None:
                from docling.document_converter import DocumentConverter
                _converter = DocumentConverter()
    return _converter


# 图片 alt 是纯哈希 / 哈希加扩展名时没有信息量。
# Docling 给的 alt 常是「图 3 系统架构」，值得留；机器生成的文件名
# （"2ce2f359b3d0c01fba3165739287e14e.jpeg"）留着只是噪音。
_MEANINGLESS_ALT = re.compile(r"^[0-9a-f]{16,}(\.\w{1,5})?$", re.IGNORECASE)


def _normalize_markdown(markdown: str) -> str:
    """把解析出的 Markdown 压成适合喂模型的纯文本。

    保留 `#` 标题层级 —— 那是现成的结构线索，模型可以直接映射成节点层级；
    去掉图片、空链接这类对导图没意义的标记，并压缩多余空行。
    """
    def alt_of(match: re.Match[str]) -> str:
        alt = match.group(1).strip()
        if not alt or _MEANINGLESS_ALT.match(alt):
            return ""
        return f"[图: {alt}]"

    # Docling 与视觉识别都会把 `&lt;` `&amp;` 这类实体原样留在 Markdown 里。
    # 不反转义的话，检索 `<` 永远匹配不到它，而块长还按 4 个字符算——实测一份
    # 中文 docx 里有 158 处。`\xa0` 是非断行空格，检索与切块都当它是普通空白。
    text = html.unescape(markdown).replace("\xa0", " ")
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", alt_of, text)
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)       # 行尾空白
    text = re.sub(r"\n{3,}", "\n\n", text)                        # 连续空行
    return text.strip()


def _parse_office_source(filename: str | None, raw: bytes) -> Source:
    """DOCX / PPTX / XLSX / HTML 走 Docling 的非版式流水线。"""
    import io

    from docling_core.types.io import DocumentStream

    converter = _get_converter()
    try:
        # convert() 只接受 Path / str / DocumentStream / HttpSource，
        # 裸 BytesIO 会被 pydantic 拒掉，必须包成 DocumentStream。
        # 走内存不落盘：上传的文件没必要在内核上留痕。
        stream = DocumentStream(name=filename or "upload", stream=io.BytesIO(raw))
        with _parse_lock:
            result = converter.convert(stream, max_num_pages=MAX_PAGES)
        markdown = result.document.export_to_markdown()
    except NotSupportedSource:
        raise
    except Exception as exc:  # noqa: BLE001 - 第三方库异常类型很杂，统一转成可读文案
        raise NotSupportedSource(f"文档解析失败：{exc}") from exc

    text = _normalize_markdown(markdown)
    if not text:
        raise NotSupportedSource(
            "这份文档没有解析出任何文字，可能是扫描件或纯图片版，暂不支持")

    # 记下文档级页数。注意这是「整份文档多少页」，不是「某个节点出自第几页」——
    # 节点级溯源要模型在生成时标注来源，属于 RAG 阶段的事，见 docs/API.md。
    #
    # Docling 的 pages 对 DOCX/PPTX 是空列表，因为它们不是分页格式，
    # 不存在「第几页」。此时 span 留空。
    page_count = len(result.pages) if getattr(result, "pages", None) else None
    return Source(kind=SourceKind.document, filename=filename, text=text,
                  span=SourceSpan(page=page_count) if page_count else None,
                  metadata=source_metadata.extract(filename, raw),
                  status="parsed")


def _parse_legacy_rendered_source(filename: str | None, raw: bytes,
                                  suffix: str) -> Source:
    """旧格式 .doc / .ppt：先转 PDF，再复用视觉提取。

    转出来的 PDF 自带文本层（LibreOffice 渲染的），所以视觉提取若不可用，
    `_parse_pdf_source` 的兜底逻辑还能从文本层拿到内容。
    """
    from app.ingestion import legacy_office

    try:
        pdf_bytes = legacy_office.convert_to_pdf(raw, suffix)
    except legacy_office.LegacyFormatUnavailable as exc:
        raise NotSupportedSource(str(exc)) from exc
    return _parse_pdf_source(filename, pdf_bytes)


def _parse_xls_source(filename: str | None, raw: bytes) -> Source:
    """旧格式 .xls：xlrd 直读单元格，转成 Markdown 表格。"""
    from app.ingestion import legacy_office

    try:
        markdown = legacy_office.read_xls(raw)
    except legacy_office.LegacyFormatUnavailable as exc:
        raise NotSupportedSource(str(exc)) from exc

    text = _normalize_markdown(markdown)
    if not text:
        raise NotSupportedSource("这份 .xls 里没有可读的单元格内容")

    # 表格没有"页"的概念，span 留空；行数放 parsed_pages 也不合适，一并留空。
    # 元数据同样走 extract：.xls 是 OLE 复合文档，不在 OOXML 集合里，这里会
    # 老老实实返回空元数据——留着这一行是为了"所有文档分支都过同一个入口"，
    # 将来补 xlrd 的属性集读取时不用再回来找哪里漏了。
    return Source(kind=SourceKind.document, filename=filename, text=text,
                  span=None, parsed_pages=None, truncated=False,
                  metadata=source_metadata.extract(filename, raw),
                  status="parsed")


def parse_document_bytes(filename: str | None, raw: bytes) -> Source:
    """解析上传的文档，返回带正文的 Source。

    空内容与无文本层都会显式拒绝 —— 返回空 Source 会让下游生成一棵空树，
    比直接报错更难排查。
    """
    suffix = _suffix_of(filename)
    if suffix not in DOCUMENT_SUFFIXES:
        raise NotSupportedSource(f"暂不支持的文档格式：{suffix or '未知'}")

    if not raw:
        raise NotSupportedSource("文件内容为空")

    if suffix == ".pdf":
        return _parse_pdf_source(filename, raw)
    if suffix in LEGACY_RENDER_SUFFIXES:
        return _parse_legacy_rendered_source(filename, raw, suffix)
    if suffix == ".xls":
        return _parse_xls_source(filename, raw)
    return _parse_office_source(filename, raw)


def write_content_hash(source: Source) -> Source:
    """把正文指纹写进 `Source.content_hash`，返回同一个对象。

    **时机是这里的全部意义**：调用点必须在正文**定稿之后**——`_normalize_markdown`
    已经跑完、任何按上下文截断都还没发生。早一步算得到的是与正文对不上的指纹，
    而指纹是知识库的唯一键：对不上就会把两份不同的正文判成同一份（静默丢数据），
    或者把同一份正文判成两份（重复入库）。

    空正文不写指纹（留 `None`）：没有正文就没有可去重的内容，写一个
    `sha256("")` 反而会让所有空材料在库里挤成一条。判空用 `strip()`，与
    `library.ingest` 的拒绝条件同一口径——一处用 `if text`、另一处用
    `if not text.strip()` 的话，纯空白正文会带着指纹进库，然后被库拒收。

    算法与 `knowledge.library.content_hash` 完全同口径（sha256 十六进制全长）。
    这里独立算一次而不是 import 那一层：`ingestion` 是解析层，不该反向依赖检索层
    ——那会让"解析器能不能单独跑"取决于知识库有没有被 import。
    """
    text = source.text or ""
    source.content_hash = (
        hashlib.sha256(text.encode("utf-8")).hexdigest() if text.strip() else None)
    return source


def parse_bytes(kind: SourceKind, filename: str | None, raw: bytes) -> Source:
    """按类型分发，并给返回的 `Source` 盖上正文指纹。

    分发顺序：
      1. markdown/text → 直接解码
      2. document      → PDF 用 pymupdf，其余用 Docling
      3. image/audio   → 交给模型或多模态服务

    一次只处理一个文件：多文件之间的并发属于上传队列（前端）的职责，
    这一层不该再套一层线程池——那会和 vision / asr 内部的并发叠成 N×N。

    指纹在**唯一出口**处盖（`_parse_unstamped` 的返回值），不是在每个分支里各盖
    一次：四个分支各写一遍，将来加第五个分支时漏盖不会有任何报错，只会在入库时
    静默退化成"按正文重算"——那种退化看不见，直到有人依赖上传路径的指纹去做
    去重比较。
    """
    return write_content_hash(_parse_unstamped(kind, filename, raw))


def _parse_unstamped(kind: SourceKind, filename: str | None, raw: bytes) -> Source:
    """`parse_bytes` 的本体：只解析，不盖指纹。

    `parse_text` / `parse_document_bytes` 这些**内部分支**保持不带指纹，是因为
    它们各自还会被单独调用（`/ingestion/text` 走 `parse_text`，脚本走
    `parse_document_bytes`），而"上传这条路的指纹在 `parse_bytes` 里盖"是一个
    可以静态检查的边界。知识库那一侧不依赖调用方给指纹：`library.ingest` 对正文
    重算一次才是权威（正文在下游仍可能被改）。
    """
    if kind in (SourceKind.text, SourceKind.markdown):
        try:
            source = parse_text(raw.decode("utf-8"), kind)
            source.filename = filename
            return source
        except UnicodeDecodeError as exc:
            raise NotSupportedSource("文件不是 UTF-8 文本") from exc

    if kind is SourceKind.document:
        return parse_document_bytes(filename, raw)

    if kind is SourceKind.image:
        # 和 PDF 逐页走同一个多模态调用，只是提示词不同。图片没有文本层可以
        # 兜底，所以视觉不可用时只能报错——返回空正文会让下游生成一棵空树。
        from app.ingestion import vision_pdf

        try:
            text = vision_pdf.describe_image(raw)
        except vision_pdf.VisionUnavailable as exc:
            raise NotSupportedSource(str(exc)) from exc
        return Source(kind=SourceKind.image, filename=filename, text=text,
                      status="parsed")

    if kind is SourceKind.audio:
        try:
            return transcribe_audio(filename, raw)
        except AsrError as exc:
            raise NotSupportedSource(str(exc)) from exc

    raise NotSupportedSource(f"未知来源类型：{kind}")
