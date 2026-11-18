"""会话和逐条消息的持久化模型。"""

from sqlalchemy import CheckConstraint, ForeignKey, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class ConversationORM(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[int] = mapped_column(nullable=False)
    updated_at: Mapped[int] = mapped_column(nullable=False)
    # 已保存的消息数，也是追加消息时使用的乐观并发版本。
    message_count: Mapped[int] = mapped_column(default=0, nullable=False)


class ConversationMessageORM(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id"), nullable=False
    )
    position: Mapped[int] = mapped_column(nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # 保存可再次发给模型的消息结构，包括 tool_calls / tool_call_id。
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[int] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("conversation_id", "position", name="uq_conversation_position"),
        CheckConstraint("role IN ('user', 'assistant', 'tool')", name="ck_conversation_message_role"),
    )
