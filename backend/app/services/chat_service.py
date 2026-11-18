"""按 conversation_id 串起数据库历史和一轮 Agent 对话。"""

import asyncio
from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from backend.app.agent.agent_runner import run_agent_loop
from backend.app.db.session import SessionLocal
from backend.app.schemas.conversation import ConversationRead
from backend.app.services import conversation_service, chat_turn_service


def load_conversation(
    conversation_id: str,
    session_factory: Callable[[], Session],
) -> ConversationRead:
    with session_factory() as session:
        return conversation_service.load_history(session, conversation_id)


async def send_message(
    user_text: str,
    conversation_id: str | None = None,
    request_id: str | None = None,
    *,
    session_factory: Callable[[], Session] | None = None,
    evaluation_now: datetime | None = None,
) -> tuple[str, str]:
    """返回 (conversation_id, 本轮回复)；每步结果先持久化再继续。"""
    if not user_text.strip():
        raise ValueError("用户消息不能为空")
    factory = session_factory or SessionLocal
    request_id = request_id or str(uuid4())
    turn = await asyncio.to_thread(
        chat_turn_service.acquire_turn,
        request_id, user_text, conversation_id, factory,
    )
    if turn.answer is not None:
        return turn.conversation_id, turn.answer

    try:
        conversation = await asyncio.to_thread(load_conversation, turn.conversation_id, factory)
        history = conversation.to_llm_history()

        async def persist_message(message):
            await asyncio.to_thread(
                chat_turn_service.append_message,
                request_id, turn.owner_token, message, factory,
            )

        async def durable_tool(registry, name, arguments, call_id):
            return await asyncio.to_thread(
                chat_turn_service.execute_tool,
                request_id, turn.owner_token, registry, name, arguments, call_id, factory,
            )

        answer = await run_agent_loop(
            user_text, history,
            resume_messages=turn.messages,
            persist_message=persist_message,
            durable_tool=durable_tool,
            evaluation_now=evaluation_now,
        )
        await asyncio.to_thread(
            chat_turn_service.complete_turn,
            request_id, turn.owner_token, answer, factory,
        )
        return turn.conversation_id, answer
    except Exception:
        await asyncio.to_thread(
            chat_turn_service.release_turn,
            request_id, turn.owner_token, factory,
        )
        raise
