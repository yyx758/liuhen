"""文档接口的请求与响应模型（spec 2.2）。正文的归一化和长度校验都在这里完成。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.config.settings import DOCUMENT_MAX_CHARS
from backend.app.services.document_chunker import normalize_content


def _clean_title(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("标题不能为空")
    if len(value) > 100:
        raise ValueError("标题不能超过 100 字")
    return value


def _clean_category(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if len(value) > 30:
        raise ValueError("分类不能超过 30 字")
    return value or None


def _clean_content(value: str) -> str:
    value = normalize_content(value)
    if len(value) > DOCUMENT_MAX_CHARS:
        raise ValueError(f"正文不能超过 {DOCUMENT_MAX_CHARS} 字")
    return value


class DocumentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="标题，1–100 字")
    category: str | None = Field(default=None, description="分类，≤ 30 字；空字符串表示没有分类")
    content: str = Field(default="", description="Markdown 正文")

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _clean_title(value)

    @field_validator("category")
    @classmethod
    def validate_category(cls, value: str | None) -> str | None:
        return _clean_category(value)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return _clean_content(value)


class DocumentUpdate(BaseModel):
    """未提供的字段保持不变；category 显式传 null 或空字符串表示清空分类。"""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    category: str | None = None
    content: str | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("标题不能为空")
        return _clean_title(value)

    @field_validator("category")
    @classmethod
    def validate_category(cls, value: str | None) -> str | None:
        return _clean_category(value)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("正文不能为 null，清空正文请传空字符串")
        return _clean_content(value)

    @model_validator(mode="after")
    def require_any_field(self):
        if not self.model_fields_set:
            raise ValueError("至少提供一个要修改的字段")
        return self


class DocumentSummary(BaseModel):
    id: int
    title: str
    category: str | None
    excerpt: str
    content_chars: int
    version: int
    created_at: datetime
    updated_at: datetime


class OutlineItem(BaseModel):
    section_seq: int
    heading_path: list[str]
    chars: int


class DocumentLinkRead(BaseModel):
    text: str
    title: str
    heading: str | None
    document_ids: list[int] = Field(description="同名多篇时按最近更新排序；未解析为空列表")


class DocumentRead(DocumentSummary):
    content: str
    outline: list[OutlineItem]
    links: list[DocumentLinkRead]


class DocumentListPage(BaseModel):
    items: list[DocumentSummary]
    next_before_id: int | None
    total: int = Field(description="符合筛选条件的总篇数")


class DocumentCategory(BaseModel):
    name: str
    count: int


class SearchHit(BaseModel):
    document_id: int
    title: str
    category: str | None
    chunk_id: int
    heading_path: list[str]
    snippet: str
    matched_queries: list[str]
    is_heading_only: bool
    updated_at: datetime


class DocumentSearchGroup(BaseModel):
    document: DocumentSummary
    hits: list[SearchHit]


class ImageRead(BaseModel):
    id: str
    url: str
    content_type: str
    byte_size: int
    original_name: str | None
    reused: bool
