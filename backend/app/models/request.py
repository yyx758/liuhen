"""Durable request state for chat turns and direct task creation."""

from sqlalchemy import JSON, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.models.conversation import ConversationORM  # registers FK target


class ChatTurnORM(Base):
    __tablename__ = "chat_turns"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # request_id 由客户端生成且全局唯一；命中别人的记录时必须拒绝，不能回放内容。
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id"), nullable=False
    )
    user_text: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # NULL after completion; UNIQUE permits only one unfinished turn per conversation.
    active_conversation_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, unique=True
    )
    owner_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[int | None] = mapped_column(nullable=True)
    base_message_count: Mapped[int] = mapped_column(nullable=False)
    messages: Mapped[list] = mapped_column(JSON, nullable=False)
    answer: Mapped[str | None] = mapped_column(nullable=True)
    # UTC Unix 时间戳（秒）；用于按用户统计每日对话轮数。
    created_at: Mapped[int] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_chat_turns_user_id_created_at", "user_id", "created_at"),
    )


class DeletedChatRequestORM(Base):
    """Minimal replay guard after the chat turn and its content are removed."""
    __tablename__ = "deleted_chat_requests"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    deleted_at: Mapped[int] = mapped_column(nullable=False)
    # 被删除的那一轮对话的创建时间。删除会话会删掉 chat_turns，
    # 每日用量要把这里也算上，否则删掉会话就能绕过上限。
    turn_created_at: Mapped[int] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_deleted_chat_requests_user_id_turn_created_at", "user_id", "turn_created_at"),
    )


class TaskCreateRequestORM(Base):
    __tablename__ = "task_create_requests"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[int] = mapped_column(nullable=False)
