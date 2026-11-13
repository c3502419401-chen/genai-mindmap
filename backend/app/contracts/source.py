"""统一来源模型（F05–F08）。

无论输入是文本、文档、图片还是语音，解析后都收敛成 Source，
生成流水线只认识 Source，不关心原始模态。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 元数据时间的唯一输出形状：UTC、秒级、`Z` 结尾。
# 秒级不是随手定的——混入毫秒会破坏字典序（`...00Z` 的 `Z` 排在 `...00.500Z`
# 的 `.` 之后），而检索前置过滤正是靠字典序比较时间。
ISO_SECONDS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def to_utc_iso(value: str | None) -> str | None:
    """把任意 ISO8601 时间串归一化成 UTC 秒级 `...Z`；解析不了返回 None。

    这是格式化，不是"猜时间"：解不出来就是 None，绝不拿当前时间顶替——顶替
    出来的时间会让时间过滤静默放进错误的来源，比没有时间更难排查。

    归一化是过滤正确的前提：`2024-01-15T10:30:00+08:00` 与
    `2024-01-15T02:30:00Z` 是同一时刻，字典序却差得很远。来源侧与条件侧都先
    过这一道，`since`/`until` 才敢用字符串比较。
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        # 没有时区就按 UTC：与 `MapRecord.updated_at`（SQLite 的 strftime 'now'）
        # 同口径。凭空假设本地时区会让同一份文档在开发机和服务器上差 8 小时。
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).strftime(ISO_SECONDS_FORMAT)


def blank_to_none(value: str | None) -> str | None:
    """空白当 None。

    Office 把没填的字段写成 `<dc:creator></dc:creator>`、PDF 的 `/Author`
    经常是空串、调用方也可能传 `"  "`。三者都该收敛成同一个值——否则"有作者
    但作者是空的"会和"没有作者"在过滤里表现不同（空串能匹配空前缀）。
    """
    if not value:
        return None
    text = value.strip()
    return text or None


def clean_tags(tags: list[str]) -> list[str]:
    """标签清洗：去首尾空白、丢空项、保序去重。

    来源侧（`SourceMetadata`）与条件侧（`knowledge.SourceFilter`）必须用同一套
    规则。任何一边多做或漏做，`" 预算 "` 与 `"预算"` 就比不上，过滤会静默失效
    ——不报错、只是筛不中，这种 bug 在界面上看不出来。
    """
    cleaned = [tag.strip() for tag in tags if tag and tag.strip()]
    return list(dict.fromkeys(cleaned))


class SourceKind(str, Enum):
    text = "text"
    markdown = "markdown"
    document = "document"   # PDF / DOCX / PPTX
    image = "image"         # 手绘草图、截图
    audio = "audio"         # 录音、视频音轨


class SourceSpan(BaseModel):
    """来源定位，用于节点溯源。"""
    model_config = ConfigDict(extra="forbid")
    page: int | None = None
    start: int | None = None
    end: int | None = None


class SourceMetadata(BaseModel):
    """来源的描述性元数据：检索前过滤的依据，不参与正文解析。

    两类的来源完全不同，不要混：
      - `author` / `created_at` / `modified_at` **自动提取**。ingestion 层从文件
        自己写的元数据里读（DOCX 的 `docProps/core.xml`、PDF 的文档信息字典），
        确定性、零成本、不喂模型。
      - `category` / `tags` **只由调用方给**。文件里没有可靠出处，本轮也不做
        模型自动推断——推断错的分类会让检索静默漏掉正确来源，比没有分类糟。

    时间一律是 **ISO8601 UTC 秒级字符串**（`2024-01-15T02:30:00Z`），与
    `MapRecord.updated_at` 同口径。不用 `datetime` 对象：跨端传的是 JSON 字符串，
    在这里放 datetime 只会让序列化格式随 pydantic 版本漂移；而过滤靠时间的
    字典序比较，格式一变，比较就错。

    字段全部有默认值，所以旧请求、旧存档不带元数据也照常构造。
    """

    model_config = ConfigDict(extra="forbid")
    # 文档作者。自动提取：DOCX/PPTX/XLSX 取 `dc:creator`（缺失时退回
    # `cp:lastModifiedBy`），PDF 取 `/Author`。为 None 只表示"文件里没写"，
    # 不表示"这份文档没有作者"。
    author: str | None = None
    # 文档创建时间。自动提取：OOXML 的 `dcterms:created`、PDF 的 `creationDate`。
    created_at: str | None = None
    # 最后修改时间。自动提取：OOXML 的 `dcterms:modified`、PDF 的 `modDate`。
    modified_at: str | None = None
    # 业务分类（"财务"、"实验报告"……）。文件里没有可靠出处，只由调用方给。
    category: str | None = None
    # 自由标签。同上，只由调用方给；ingestion 永远不填这两项。
    tags: list[str] = Field(default_factory=list)

    @field_validator("created_at", "modified_at")
    @classmethod
    def _normalize_time(cls, value: str | None) -> str | None:
        """时间一律先归一化成 UTC 秒级；解析不了折成 None。

        这一步不是洁癖。调用方（用户标注、外层模型）常直接写 `2024-01-15`
        或带偏移的 `+08:00`；不在这里折平，检索时的时间比较就会把"同一天"
        判成不等——`"2024-01-15" < "2024-01-15T00:00:00Z"` 是字符串比较的
        必然结果，于是一份 1 月 15 日的文档会被 `since=2024-01-15` 漏掉。

        解析不了就折 None：与自动提取同一条规矩——宁可"没有时间"
        （时间过滤会把它排除），也不要留一个无法比较的脏字符串。
        """
        return to_utc_iso(value)

    @field_validator("author", "category")
    @classmethod
    def _blank_text_is_none(cls, value: str | None) -> str | None:
        return blank_to_none(value)

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        return clean_tags(value)


class AudioSentence(BaseModel):
    """音频里的一句话，以及它在**原始录音**中的位置。

    时间是从整段录音开头算的毫秒偏移，不是相对于某个切片。切片识别时
    由服务层按段的起点平移过——否则第二段的所有时间会从 0 重新开始，
    节点会指回错误的位置。

    只保留句级粒度。词级时间戳接口也返回，但节点引用用不到那么细，
    存下来只会让契约和前端都变重。
    """
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500)
    begin_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=100)
    kind: SourceKind
    filename: str | None = None
    text: str | None = None          # 抽取后的纯文本；未解析成功时为 None
    span: SourceSpan | None = None
    status: Literal["pending", "parsed", "failed"] = "pending"
    error: str | None = None
    # 实际得到内容的页数。与 span.page（整份文档共几页）不是一回事：
    # 51 页的 PDF 只处理前 50 页时 span.page = 51、parsed_pages = 50；
    # 中间有页识别失败时它还会更小。只有分页格式（PDF）才有意义，其余留 None。
    parsed_pages: int | None = None
    # 是否因 MAX_PAGES 被截断：后半截根本没处理过。
    # 必须显式告诉调用方——否则用户拿到的是一份缺了尾巴的正文，
    # 界面却显示"解析成功"，导图会静默漏掉后面的章节。
    truncated: bool = False
    # 处理过的页里，彻底没拿到内容的页码（1 起）。
    # 与 truncated 语义不同：那个是"没处理到"，这个是"处理了但中间有洞"。
    # 两者对用户的含义不一样，合并成一个布尔值会给出自相矛盾的提示
    # （"共 3 页，仅处理前 3 页"这种）。
    failed_pages: list[int] = Field(default_factory=list)
    # 语音来源的音频时长（秒）。文档用 parsed_pages 表示"处理了多少"，
    # 语音用时长更贴切：转写结果没有页的概念，用户关心的是"这段录音多长"。
    # 其余模态留 None。
    duration_seconds: float | None = None
    # 逐句时间戳。只有语音来源会填，供节点引用指回"这句话在第几秒"。
    # 其余模态为空列表，不要用 None——前端少一个空值判断。
    sentences: list[AudioSentence] = Field(default_factory=list)
    # 描述性元数据（作者/时间/分类/标签）。默认空对象而不是 None：检索前置
    # 过滤要读 `source.metadata.author`，空对象与"全字段 None"在过滤语义上
    # 完全等价，却让调用方少一层 None 判断。
    metadata: SourceMetadata = Field(default_factory=SourceMetadata)
    # 正文指纹（`sha256(text)` 的十六进制），知识库的去重键（`library_sources`
    # 上的 `UNIQUE(content_hash)`）。
    #
    # 写入方只有一个：`ingestion.service.parse_bytes` 的**唯一出口**，时机是正文
    # 定稿之后（`_normalize_markdown` 已跑完、按上下文截断之前）。这两条都不能挪：
    # 早算会得到与正文对不上的指纹，而指纹就是唯一键——对不上会把两份不同的正文
    # 判成同一份（静默丢数据），或者把同一份判成两份（重复入库）。
    #
    # 它仍然**不是权威值**：正文在下游还可能被调用方编辑或截断，所以落库方
    # `knowledge.library.ingest` 对收到的正文重算一次，只有两者不一致时才记一条
    # warning 并以正文为准。
    content_hash: str | None = None


class SourceUsage(BaseModel):
    """整张导图使用过的来源摘要；不代表具体节点已建立证据链接。"""
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=100)
    kind: SourceKind
    filename: str | None = None
    char_count: int = Field(ge=1)


class SourceRef(BaseModel):
    """节点引用。字符区间以原始 Source.text 为准，使用从零开始的 [start, end)。

    第一层只校验字段；来源关联和原文匹配分别由导图契约与服务层校验。
    """
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=100)
    page: int | None = Field(default=None, ge=1, strict=True)
    start: int | None = Field(default=None, ge=0, strict=True)
    end: int | None = Field(default=None, ge=0, strict=True)
    quote: str | None = Field(default=None, max_length=300)

    @field_validator("source_id")
    @classmethod
    def source_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("引用的来源 ID 不能为空白")
        return value

    @field_validator("quote")
    @classmethod
    def blank_quote_as_none(cls, value: str | None) -> str | None:
        return value if value and value.strip() else None

    @model_validator(mode="after")
    def validate_range(self) -> SourceRef:
        if (self.start is None) != (self.end is None):
            raise ValueError("引用的 start 和 end 必须同时提供")
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("引用的 end 必须大于 start")
        return self


class CreateTextSourceRequest(BaseModel):
    """最小可用入口：直接贴文本。"""
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=10000)
    kind: Literal[SourceKind.text, SourceKind.markdown] = SourceKind.text


class CreateTextSourceResponse(BaseModel):
    source: Source
