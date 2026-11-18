"""会话历史持久化；Agent 循环只需使用读取出的消息列表。"""

import json
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models.conversation import ConversationMessageORM, ConversationORM
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


def create_conversation(session: Session) -> str:
    conversation_id = str(uuid4())
    now = now_timestamp()
    with session.begin():
        session.add(ConversationORM(
            id=conversation_id,
            created_at=now,
            updated_at=now,
            message_count=0,
        ))
    return conversation_id


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
        if owns_transaction and session.get_bind().dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

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
