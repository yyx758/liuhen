"""按 conversation_id 串起数据库历史和一轮 Agent 对话。"""

import asyncio
from collections.abc import Awaitable, Callable
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
    user_id: int,
    session_factory: Callable[[], Session] | None = None,
    evaluation_now: datetime | None = None,
) -> tuple[str, str]:
    """返回 (conversation_id, 本轮回复)；每步结果先持久化再继续。"""
    turn = await begin_turn(
        user_text, conversation_id, request_id,
        user_id=user_id, session_factory=session_factory,
    )
    if turn.answer is not None:
        return turn.conversation_id, turn.answer
    answer = await run_turn(
        turn, user_text,
        user_id=user_id, session_factory=session_factory, evaluation_now=evaluation_now,
    )
    return turn.conversation_id, answer


async def begin_turn(
    user_text: str,
    conversation_id: str | None = None,
    request_id: str | None = None,
    *,
    user_id: int,
    session_factory: Callable[[], Session] | None = None,
) -> chat_turn_service.TurnState:
    """取得本轮处理权；已完成的请求直接带回 answer。

    校验、额度、会话归属等错误都在这里抛出，流式接口借此在发出响应头之前返回 HTTP 错误。
    """
    if not user_text.strip():
        raise ValueError("用户消息不能为空")
    factory = session_factory or SessionLocal
    return await asyncio.to_thread(
        chat_turn_service.acquire_turn,
        request_id or str(uuid4()), user_text, conversation_id, factory, user_id=user_id,
    )


async def run_turn(
    turn: chat_turn_service.TurnState,
    user_text: str,
    *,
    user_id: int,
    session_factory: Callable[[], Session] | None = None,
    evaluation_now: datetime | None = None,
    on_event: Callable[[str, dict], Awaitable[None]] | None = None,
) -> str:
    """执行 begin_turn 取得的一轮对话并持久化，返回最终回复。"""
    factory = session_factory or SessionLocal
    request_id = turn.request_id
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
            on_event=on_event,
            user_id=user_id,
        )
        await asyncio.to_thread(
            chat_turn_service.complete_turn,
            request_id, turn.owner_token, answer, factory,
        )
        return answer
    except Exception:
        await asyncio.to_thread(
            chat_turn_service.release_turn,
            request_id, turn.owner_token, factory,
        )
        raise
