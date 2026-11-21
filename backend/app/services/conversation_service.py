"""会话历史持久化；Agent 循环只需使用读取出的消息列表。"""

import json
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from backend.app.models.conversation import ConversationMessageORM, ConversationORM
from backend.app.models.request import ChatTurnORM, DeletedChatRequestORM
from backend.app.schemas.conversation import ConversationMessage, ConversationRead


def now_timestamp() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def normalize_message(message: dict[str, Any]) -> dict[str, Any]:
    """只保存下一轮模型调用需要的字段，不持久化 system 提示。"""
    try:
        data = ConversationMessage.model_validate(message).to_llm_message()
        return json.loads(json.dumps(data, ensure_ascii=False))
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(422, "会话消息不合法") from exc


def load_history(session: Session, conversation_id: str) -> ConversationRead:
    conversation = session.get(ConversationORM, conversation_id)
    if conversation is None:
        raise HTTPException(404, "会话不存在")

    rows = session.scalars(
        select(ConversationMessageORM)
        .where(ConversationMessageORM.conversation_id == conversation_id)
        .order_by(ConversationMessageORM.position)
    ).all()
    return ConversationRead(
        conversation_id=conversation.id,
        message_count=conversation.message_count,
        history=[ConversationMessage.model_validate(row.payload) for row in rows],
    )


def delete_conversation(session: Session, conversation_id: str) -> None:
    """Remove chat content; keep only request IDs needed to reject replay."""
    with session.begin():
        # acquire_turn takes this lock before starting another turn.
        session.execute(
            text("SELECT pg_advisory_xact_lock(3, hashtext(:key))"),
            {"key": conversation_id},
        )

        conversation = session.get(
            ConversationORM, conversation_id,
            with_for_update=True,
        )
        if conversation is None:
            raise HTTPException(404, "会话不存在")
        active = session.scalar(
            select(ChatTurnORM.request_id).where(
                ChatTurnORM.conversation_id == conversation_id,
                ChatTurnORM.status == "processing",
            ).limit(1)
        )
        if active is not None:
            raise HTTPException(409, "会话正在处理消息，请稍后删除")

        request_ids = session.scalars(
            select(ChatTurnORM.request_id).where(ChatTurnORM.conversation_id == conversation_id)
        ).all()
        deleted_at = now_timestamp()
        session.add_all(
            DeletedChatRequestORM(request_id=request_id, deleted_at=deleted_at)
            for request_id in request_ids
        )
        session.execute(delete(ChatTurnORM).where(ChatTurnORM.conversation_id == conversation_id))
        session.execute(delete(ConversationMessageORM).where(
            ConversationMessageORM.conversation_id == conversation_id
        ))
        session.delete(conversation)


def append_messages(
    session: Session,
    conversation_id: str,
    messages: list[dict[str, Any]],
    expected_count: int,
) -> int:
    """按顺序原子追加一批消息；expected_count 防止覆盖较新的历史。"""
    if not messages:
        raise HTTPException(422, "没有要保存的消息")
    stored_messages = [normalize_message(message) for message in messages]

    owns_transaction = not session.info.get("chat_turn_completion", False)
    with session.begin() if owns_transaction else nullcontext():
        conversation = session.get(ConversationORM, conversation_id)
        if conversation is None:
            raise HTTPException(404, "会话不存在")
        if conversation.message_count != expected_count:
            raise HTTPException(409, "会话已有新消息，请重新读取历史")

        now = now_timestamp()
        for offset, message in enumerate(stored_messages):
            session.add(ConversationMessageORM(
                conversation_id=conversation_id,
                position=expected_count + offset,
                role=message["role"],
                payload=message,
                created_at=now,
            ))
        conversation.message_count += len(stored_messages)
        conversation.updated_at = now
        new_count = conversation.message_count

    return new_count
