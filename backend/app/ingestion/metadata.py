"""来源元数据提取：作者与时间，供检索前置过滤使用（`SourceMetadata`）。

只提取**文件自己写着的**信息。业务分类与自由标签没有可靠自动来源，不在这里
——那是调用方的活（见 `app/contracts/source.py::SourceMetadata`）。

三条铁律，改之前先看：

1. **绝不抛错**。元数据缺失是常态：扫描件、截图、纯文本根本没有作者字段。
   它不该让一次上传失败。任何异常都吞掉、字段留 None、记一条 debug 日志。
2. **不拿文件系统 mtime 冒充文档时间**。mtime 是"这个文件被拷到这台机器上的
   时间"——下载一份 2023 年的报告，mtime 是今天。用它过滤会把旧文档误判成新的，
   比没有元数据更糟，所以宁可留 None。
3. **时间统一成 UTC 秒级**（`2024-01-15T02:30:00Z`）。归一化是过滤正确的前提，
   细节见 `app/contracts/source.py::to_utc_iso`。

格式与字段的对应：

    DOCX / PPTX / XLSX   `docProps/core.xml` 的 `dc:creator` / `dcterms:created` /
                         `dcterms:modified`；`dc:creator` 缺失时退回
                         `cp:lastModifiedBy`（模板生成的文档常没有前者）
    PDF                  `pymupdf` 的 `doc.metadata`：`author` / `creationDate` /
                         `modDate`；日期要自己从 `D:2024…` 转 ISO8601
    其余                 None —— Markdown / 纯文本 / 图片 / 音频没有可靠出处

不碰正文：正文由 `service.py` 解析，两者独立。这里失败不影响解析，解析失败也
不该拦着元数据（元数据在 `service.py` 里是**解析前**取的）。
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

from app.contracts.source import ISO_SECONDS_FORMAT, SourceMetadata, blank_to_none, to_utc_iso

logger = logging.getLogger(__name__)

# 只有 OOXML 系列是 zip 包、才有 core.xml。.doc/.ppt/.xls 是 OLE 复合文档，
# 结构完全不同，本轮不做（它们的元数据靠 LibreOffice 转换后的 PDF 拿到，
# 见 `service._parse_legacy_rendered_source`）。
OOXML_SUFFIXES = frozenset({".docx", ".pptx", ".xlsx"})

_CORE_XML = "docProps/core.xml"

# core.xml 的命名空间。ElementTree 的 find() 必须带完整 URI，写死这三组而不是
# 从 payload 里动态解析：任何 Office 文档的 core.xml 都用固定的前缀与 URI。
_NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
}

# PDF 日期：`D:YYYYMMDDHHmmSS` + 可选的 `Z` 或 `±HH'mm'`。
# 每一段都可截断（`D:2024`、`D:202401` 都合法），所以逐段可选。
# 尾部的 `'` 允许出现也允许缺失：生成器写法不一致，`+08'00'` 与 `+08'00`
# 在真实文件里都能见到。
_PDF_DATE_RE = re.compile(
    r"^(?:D:)?"
    r"(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
    r"(?:([Zz])|([+-])(\d{2})(?:'?(\d{2})'?)?)?"
    r"'?$"
)


def _suffix_of(filename: str | None) -> str:
    if not filename:
        return ""
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot >= 0 else ""


def _xml_text(root: ElementTree.Element, tag: str) -> str | None:
    node = root.find(tag, _NS)
    if node is None:
        return None
    return blank_to_none(node.text)


def pdf_date_to_iso(value: str | None) -> str | None:
    """PDF 日期（`D:20240115103000+08'00'`）→ UTC 秒级 ISO8601。

    转不了就返回 None，不猜：PDF 里这一栏是自由填写的，见过 `D:00000000`、
    `now`、整段中文。猜错的时间会让时间过滤静默漏掉正确来源。

    规则：
      - 缺省的年月日时分秒补 `01` / `00`（`D:2024` → `2024-01-01T00:00:00Z`）
      - `Z` / `z` 表示 UTC；`±HH'mm'` 按偏移**换算**到 UTC；没有时区就按 UTC
        处理（PDF 规范里时区可选，实践中生成器基本写 UTC）
      - 月份 0 或 13、2 月 30 日、偏移超过 ±23 小时、分钟超过 59 → None
    """
    if not value:
        return None
    match = _PDF_DATE_RE.match(value.strip())
    if match is None:
        return None

    year, month, day, hour, minute, second, _zulu, sign, off_hour, off_minute = \
        match.groups()
    # 除年份外每一段都可能缺失，也可能写成 "00"（`D:20240000000000` 这种占位值，
    # 真实 PDF 里确实存在）。缺的补 1，`00` 的字面值也当"没填"补 1。
    try:
        stamp = datetime(
            int(year),
            int(month or 1) or 1,
            int(day or 1) or 1,
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None                     # 13 月、2 月 30 日、24 点……

    if sign:
        hours, minutes = int(off_hour), int(off_minute or 0)
        if hours > 23 or minutes > 59:
            return None
        offset = timedelta(hours=hours, minutes=minutes)
        # `+08'00'` 是"当地比 UTC 早 8 小时"，换算要减；`-` 则加
        stamp = stamp - offset if sign == "+" else stamp + offset

    # `_zulu`（Z）与"没有时区"在这里同义：都按 UTC 处理，不需要额外分支。
    return stamp.astimezone(timezone.utc).strftime(ISO_SECONDS_FORMAT)


def _from_ooxml(raw: bytes) -> SourceMetadata:
    """读 OOXML 的 `docProps/core.xml`。只解这一份 XML，不碰正文。"""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            payload = archive.read(_CORE_XML)
    except (KeyError, zipfile.BadZipFile, OSError, ValueError) as exc:
        # 没有 core.xml 很常见（第三方工具生成的 docx 常整份省掉 docProps），
        # 坏 zip 则说明文件本身有问题——两种都只是"没有元数据"。正文解析会
        # 给出更准确的报错，这里不要抢它的活。
        logger.debug("读取 %s 失败：%s：%s", _CORE_XML, type(exc).__name__, exc)
        return SourceMetadata()

    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        logger.debug("%s 不是合法 XML：%s", _CORE_XML, exc)
        return SourceMetadata()

    # dc:creator 优先。它是 Dublin Core 的"文档创建者"，语义就是 author；
    # cp:lastModifiedBy 是最后一个保存文件的人，模板生成的文档里常与真实作者
    # 不同（实测一份实验报告：creator 是模板作者，lastModifiedBy 是学生），
    # 所以只在前者缺失时兜底，不覆盖。
    author = _xml_text(root, "dc:creator") or _xml_text(root, "cp:lastModifiedBy")
    return SourceMetadata(
        author=author,
        created_at=to_utc_iso(_xml_text(root, "dcterms:created")),
        modified_at=to_utc_iso(_xml_text(root, "dcterms:modified")),
    )


def from_pdf(raw: bytes) -> SourceMetadata:
    """读 PDF 文档信息字典里的作者与时间。

    `pymupdf` 延迟导入：元数据是可选增强，缺依赖时不该让整个模块 import 失败
    （`requirements.txt` 里有它，但部署机可能只装了最小集）。

    旧格式 .doc/.ppt 经 LibreOffice 转成 PDF 后也走这里——转出来的 PDF 带完整
    信息字典，比反过来解析 OLE 属性集省事得多。
    """
    try:
        import pymupdf
    except ImportError as exc:          # pragma: no cover - 只在缺依赖的部署上触发
        logger.debug("没有 pymupdf，跳过 PDF 元数据：%s", exc)
        return SourceMetadata()

    try:
        document = pymupdf.open(stream=raw, filetype="pdf")
    except Exception as exc:            # noqa: BLE001 - pymupdf 异常类型不稳定
        logger.debug("打开 PDF 失败，跳过元数据：%s：%s", type(exc).__name__, exc)
        return SourceMetadata()

    try:
        info = document.metadata or {}
    except Exception as exc:            # noqa: BLE001 - 加密文档要到这一步才炸
        logger.debug("读取 PDF 元数据失败：%s：%s", type(exc).__name__, exc)
        info = {}
    finally:
        document.close()

    return SourceMetadata(
        author=blank_to_none(info.get("author")),
        created_at=pdf_date_to_iso(info.get("creationDate")),
        modified_at=pdf_date_to_iso(info.get("modDate")),
    )


def extract(filename: str | None, raw: bytes) -> SourceMetadata:
    """按后缀选提取器；没有元数据来源的格式返回空元数据。

    Markdown / 纯文本 / 图片 / 音频一律留 None：文本文件里有没有"作者"全看
    作者手写，不是元数据；图片和音频的 EXIF/ID3 里确实有 `Artist`，但那是
    **拍摄者/演唱者**，不是"这份材料的作者"，混为一谈会让作者过滤给出误导性
    结果。要这类信息就由调用方显式传 `SourceMetadata`。
    """
    suffix = _suffix_of(filename)
    if suffix in OOXML_SUFFIXES:
        return _from_ooxml(raw)
    if suffix == ".pdf":
        return from_pdf(raw)
    return SourceMetadata()
