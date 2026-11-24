"""Persist each chat turn before side effects and resume it after interruption."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text

from backend.app.agent.document_sources import build_sources, chars_read_in_turn, strip_markers
from backend.app.agent.document_tools import CHARS_READ_KEY
from backend.app.agent.tool_registry import ToolRegistry
from backend.app.models.conversation import ConversationORM
from backend.app.models.request import ChatTurnORM, DeletedChatRequestORM
from backend.app.services import conversation_service, usage_service


LEASE_SECONDS = 120


def now_timestamp() -> int:
    return int(datetime.now(timezone.utc).timestamp())


@dataclass
class TurnState:
    request_id: str
    conversation_id: str
    owner_token: str | None
    messages: list[dict]
    answer: str | None
    # 来源卡片（spec 3.4）；本轮完成后才有值。
    sources: list[dict] = field(default_factory=list)


REQUEST_ID_TAKEN = "请求 ID 已被使用，请为新消息生成新的请求 ID"


def acquire_turn(
    request_id: str, user_text: str, conversation_id: str | None, session_factory, *, user_id: int
) -> TurnState:
    owner_token = str(uuid4())
    now = now_timestamp()
    with session_factory() as session, session.begin():
        session.execute(
            text("SELECT pg_advisory_xact_lock(2, hashtext(:key))"),
            {"key": request_id},
        )
        if conversation_id is not None:
            session.execute(
                text("SELECT pg_advisory_xact_lock(3, hashtext(:key))"),
                {"key": conversation_id},
            )
        # request_id 由客户端生成、全局唯一。命中别人的记录时只说"已被使用"，
        # 不回放别人的回答，也不透露对方会话是否已删除。
        deleted = session.get(DeletedChatRequestORM, request_id)
        if deleted is not None:
            if deleted.user_id != user_id:
                raise HTTPException(409, REQUEST_ID_TAKEN)
            raise HTTPException(410, "该请求所属的会话已删除，请开始新对话")
        turn = session.get(ChatTurnORM, request_id)
        if turn is not None:
            if turn.user_id != user_id:
                raise HTTPException(409, REQUEST_ID_TAKEN)
            if turn.user_text != user_text or (
                conversation_id is not None and turn.conversation_id != conversation_id
            ):
                raise HTTPException(409, "请求 ID 已用于不同的对话内容")
            if turn.status == "completed":
                return TurnState(
                    request_id, turn.conversation_id, None, list(turn.messages), turn.answer, list(turn.sources or [])
                )
            if turn.lease_until is not None and turn.lease_until > now:
                raise HTTPException(409, "这一轮对话正在处理，请稍后用同一请求 ID 重试")
            turn.owner_token = owner_token
            turn.lease_until = now + LEASE_SECONDS
            return TurnState(request_id, turn.conversation_id, owner_token, list(turn.messages), None)

        # 走到这里才是新的一轮：重试同一 request_id 已在上面返回，不占用额度。
        usage_service.ensure_quota(session, user_id=user_id, now=now)

        if conversation_id is None:
            conversation_id = str(uuid4())
            session.add(ConversationORM(
                id=conversation_id, user_id=user_id, created_at=now, updated_at=now, message_count=0
            ))
            message_count = 0
        else:
            conversation = session.get(ConversationORM, conversation_id)
            if conversation is None or conversation.user_id != user_id:
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
            user_id=user_id,
            conversation_id=conversation_id,
            user_text=user_text,
            status="processing",
            active_conversation_id=conversation_id,
            owner_token=owner_token,
            lease_until=now + LEASE_SECONDS,
            base_message_count=message_count,
            messages=messages,
            created_at=now,
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
        # 阅读量以已持久化的工具结果为准，崩溃恢复后不会重新计数（spec 3.3）。
        session.info[CHARS_READ_KEY] = chars_read_in_turn(turn.messages)
        # 工具只在本轮所属用户的数据范围内执行；归属来自已保存的 turn，而不是模型参数。
        output = registry.execute_tool(name, arguments, session, user_id=turn.user_id)
        tool_message = conversation_service.normalize_message({
            "role": "tool", "tool_call_id": call_id, "content": output
        })
        # The business operation and its result become durable in the same commit.
        turn.messages = [*turn.messages, tool_message]
        turn.lease_until = now_timestamp() + LEASE_SECONDS
        return output


def complete_turn(request_id: str, owner_token: str, answer: str, session_factory) -> list[dict]:
    """结束本轮并返回来源卡片。

    chat_turns.messages 保留带来源标记的最终回复，卡片由它算出；
    写进会话历史的是去掉标记的 answer，下一轮模型看不到已失效的片段 ID。
    """
    with session_factory() as session, session.begin():
        turn = require_owner(session, request_id, owner_token)
        if not turn.messages or turn.messages[-1].get("role") != "assistant":
            raise HTTPException(500, "本轮对话缺少最终回复")
        if strip_markers(turn.messages[-1].get("content") or "") != answer:
            raise HTTPException(500, "本轮回复与保存内容不一致")
        session.info["chat_turn_completion"] = True
        history = [*turn.messages[:-1], {**turn.messages[-1], "content": answer}]
        conversation_service.append_messages(
            session, turn.conversation_id, history, turn.base_message_count
        )
        sources = [source.model_dump() for source in build_sources(turn.messages, request_id=request_id)]
        turn.status = "completed"
        turn.active_conversation_id = None
        turn.owner_token = None
        turn.lease_until = None
        turn.answer = answer
        turn.sources = sources
        return sources


def release_turn(request_id: str, owner_token: str, session_factory) -> None:
    """Allow an immediate retry after a handled error; committed steps stay intact."""
    with session_factory() as session, session.begin():
        turn = session.get(ChatTurnORM, request_id)
        if turn is not None and turn.status == "processing" and turn.owner_token == owner_token:
            turn.lease_until = now_timestamp()
