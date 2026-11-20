"""Durable request state for chat turns and direct task creation."""

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.models.conversation import ConversationORM  # registers FK target


class ChatTurnORM(Base):
    __tablename__ = "chat_turns"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
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


class DeletedChatRequestORM(Base):
    """Minimal replay guard after the chat turn and its content are removed."""
    __tablename__ = "deleted_chat_requests"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    deleted_at: Mapped[int] = mapped_column(nullable=False)


class TaskCreateRequestORM(Base):
    __tablename__ = "task_create_requests"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[int] = mapped_column(nullable=False)
