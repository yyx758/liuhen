"""文档、片段与图片（spec 1.2–1.4）。所有按 ID 或列表的查询都必须带上 user_id 条件。"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, JSON, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class DocumentORM(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # 归一化后的 Markdown 正文；长度上限由服务层按 DOCUMENT_MAX_CHARS 校验。
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    # 由服务层用 now_utc() 写入，不用数据库默认值，便于测试固定时钟。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("char_length(btrim(title)) > 0", name="ck_documents_title_not_blank"),
        CheckConstraint(
            "category IS NULL OR char_length(btrim(category)) > 0", name="ck_documents_category_not_blank"
        ),
        Index("ix_documents_user_id_updated_at_id", "user_id", "updated_at", "id"),
        Index("ix_documents_user_id_category", "user_id", "category"),
        Index(
            "ix_documents_title_trgm", "title",
            postgresql_using="gin", postgresql_ops={"title": "gin_trgm_ops"},
        ),
    )


class DocumentChunkORM(Base):
    """片段是派生数据，每次保存文档整篇重建；id、seq、偏移量只对当前版本有效。"""

    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    # 冗余存放，搜索只查片段表时隔离条件直接落在这里；写入时取自所属文档。
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    seq: Mapped[int] = mapped_column(nullable=False)
    section_seq: Mapped[int] = mapped_column(nullable=False)
    heading_path: Mapped[list] = mapped_column(JSON, nullable=False)
    # content == documents.content[start_offset:end_offset]
    start_offset: Mapped[int] = mapped_column(nullable=False)
    end_offset: Mapped[int] = mapped_column(nullable=False)
    is_heading_only: Mapped[bool] = mapped_column(nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 文档标题 + 小节路径 + 可读化正文，只用于匹配和排序。
    search_text: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("document_id", "seq", name="uq_document_chunks_document_id_seq"),
        CheckConstraint("start_offset >= 0 AND end_offset > start_offset", name="ck_document_chunks_offsets"),
        Index("ix_document_chunks_document_id_section_seq", "document_id", "section_seq"),
        Index("ix_document_chunks_user_id", "user_id"),
        Index(
            "ix_document_chunks_search_text_trgm", "search_text",
            postgresql_using="gin", postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
    )


class DocumentImageORM(Base):
    """图片不挂在某篇文档上；正文里以 /documents/images/<public_id> 引用。"""

    __tablename__ = "document_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    content_type: Mapped[str] = mapped_column(String(16), nullable=False)
    byte_size: Mapped[int] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # 最大 5 MB；延迟加载，只有真正读取图片时才查出来。
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, deferred=True)
    original_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "content_type IN ('image/png', 'image/jpeg', 'image/gif', 'image/webp')",
            name="ck_document_images_content_type",
        ),
        CheckConstraint("byte_size > 0", name="ck_document_images_byte_size"),
        UniqueConstraint("user_id", "sha256", name="uq_document_images_user_id_sha256"),
        Index("ix_document_images_user_id_created_at", "user_id", "created_at"),
    )
