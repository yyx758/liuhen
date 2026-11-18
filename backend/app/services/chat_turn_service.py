"""Persist each chat turn before side effects and resume it after interruption."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text

from backend.app.agent.tool_registry import ToolRegistry
from backend.app.models.conversation import ConversationORM
from backend.app.models.request import ChatTurnORM
from backend.app.services import conversation_service


LEASE_SECONDS = 120


def now_timestamp() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def begin_immediate(session) -> None:
    if session.get_bind().dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


@dataclass
class TurnState:
    request_id: str
    conversation_id: str
    owner_token: str | None
    messages: list[dict]
    answer: str | None


def acquire_turn(request_id: str, user_text: str, conversation_id: str | None, session_factory) -> TurnState:
    owner_token = str(uuid4())
    now = now_timestamp()
    with session_factory() as session, session.begin():
        begin_immediate(session)
        if session.get_bind().dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(2, hashtext(:key))"),
                {"key": request_id},
            )
            if conversation_id is not None:
                session.execute(
                    text("SELECT pg_advisory_xact_lock(3, hashtext(:key))"),
                    {"key": conversation_id},
                )
        turn = session.get(ChatTurnORM, request_id)
        if turn is not None:
            if turn.user_text != user_text or (
                conversation_id is not None and turn.conversation_id != conversation_id
            ):
                raise HTTPException(409, "请求 ID 已用于不同的对话内容")
            if turn.status == "completed":
                return TurnState(request_id, turn.conversation_id, None, list(turn.messages), turn.answer)
            if turn.lease_until is not None and turn.lease_until > now:
                raise HTTPException(409, "这一轮对话正在处理，请稍后用同一请求 ID 重试")
            turn.owner_token = owner_token
            turn.lease_until = now + LEASE_SECONDS
            return TurnState(request_id, turn.conversation_id, owner_token, list(turn.messages), None)

        if conversation_id is None:
            conversation_id = str(uuid4())
            session.add(ConversationORM(
                id=conversation_id, created_at=now, updated_at=now, message_count=0
            ))
            message_count = 0
        else:
            conversation = session.get(ConversationORM, conversation_id)
            if conversation is None:
                raise HTTPException(404, "会话不存在")
            message_count = conversation.message_count

        active = session.scalar(select(ChatTurnORM.request_id).where(
            ChatTurnORM.active_conversation_id == conversation_id
        ))
        if active is not None:
            raise HTTPException(409, f"会话有未完成的请求，请先重试请求 {active}")

        messages = [{"role": "user", "content": user_text}]
        session.add(ChatTurnORM(
            request_id=request_id,
            conversation_id=conversation_id,
            user_text=user_text,
            status="processing",
            active_conversation_id=conversation_id,
            owner_token=owner_token,
            lease_until=now + LEASE_SECONDS,
            base_message_count=message_count,
            messages=messages,
        ))
        return TurnState(request_id, conversation_id, owner_token, messages, None)


def require_owner(session, request_id: str, owner_token: str) -> ChatTurnORM:
    turn = session.get(ChatTurnORM, request_id)
    if turn is None or turn.status != "processing" or turn.owner_token != owner_token:
        raise HTTPException(409, "请求处理权已变化，请重新读取请求结果")
    return turn


def append_message(request_id: str, owner_token: str, message: dict, session_factory) -> None:
    normalized = conversation_service.normalize_message(message)
    with session_factory() as session, session.begin():
        begin_immediate(session)
        turn = require_owner(session, request_id, owner_token)
        turn.messages = [*turn.messages, normalized]
        turn.lease_until = now_timestamp() + LEASE_SECONDS


def execute_tool(
    request_id: str,
    owner_token: str,
    registry: ToolRegistry,
    name: str,
    arguments: str,
    call_id: str,
    session_factory,
) -> str:
    with session_factory() as session, session.begin():
        begin_immediate(session)
        turn = require_owner(session, request_id, owner_token)
        for message in turn.messages:
            if message.get("role") == "tool" and message.get("tool_call_id") == call_id:
                return message["content"]

        pending_calls = []
        for message in reversed(turn.messages):
            if message.get("role") == "assistant":
                pending_calls = message.get("tool_calls") or []
                break
        if not any(
            call.get("id") == call_id
            and call.get("function", {}).get("name") == name
            and call.get("function", {}).get("arguments") == arguments
            for call in pending_calls
        ):
            raise HTTPException(409, "工具调用与已保存的模型指令不一致")

        session.info["chat_tool_transaction"] = True
        session.info["audit_request_id"] = request_id
        session.info["audit_conversation_id"] = turn.conversation_id
        session.info["audit_tool_call_id"] = call_id
        output = registry.execute_tool(name, arguments, session)
        tool_message = conversation_service.normalize_message({
            "role": "tool", "tool_call_id": call_id, "content": output
        })
        # The business operation and its result become durable in the same commit.
        turn.messages = [*turn.messages, tool_message]
        turn.lease_until = now_timestamp() + LEASE_SECONDS
        return output


def complete_turn(request_id: str, owner_token: str, answer: str, session_factory) -> None:
    with session_factory() as session, session.begin():
        begin_immediate(session)
        turn = require_owner(session, request_id, owner_token)
        if not turn.messages or turn.messages[-1].get("role") != "assistant":
            raise HTTPException(500, "本轮对话缺少最终回复")
        if turn.messages[-1].get("content") != answer:
            raise HTTPException(500, "本轮回复与保存内容不一致")
        session.info["chat_turn_completion"] = True
        conversation_service.append_messages(
            session, turn.conversation_id, turn.messages, turn.base_message_count
        )
        turn.status = "completed"
        turn.active_conversation_id = None
        turn.owner_token = None
        turn.lease_until = None
        turn.answer = answer


def release_turn(request_id: str, owner_token: str, session_factory) -> None:
    """Allow an immediate retry after a handled error; committed steps stay intact."""
    with session_factory() as session, session.begin():
        begin_immediate(session)
        turn = session.get(ChatTurnORM, request_id)
        if turn is not None and turn.status == "processing" and turn.owner_token == owner_token:
            turn.lease_until = now_timestamp()
