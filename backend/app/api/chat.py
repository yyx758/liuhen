"""接收一轮用户输入，并将其接入持久化会话。"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import StreamingResponse

from backend.app.api.deps import CurrentUserId, DbSession
from backend.app.schemas.conversation import ChatRequest, ChatResponse
from backend.app.services import chat_service, conversation_service


router = APIRouter(prefix="/chat", tags=["Chat"])
logger = logging.getLogger(__name__)

# 事件循环只持有任务的弱引用；客户端断开后生成器不再等待任务，这里保留强引用直到任务结束。
running_turns: set[asyncio.Task] = set()


def format_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("", response_model=ChatResponse)
async def send_message(data: ChatRequest, user_id: CurrentUserId) -> ChatResponse:
    turn = await chat_service.begin_turn(
        data.user_text, data.conversation_id, str(data.request_id), user_id=user_id
    )
    if turn.answer is None:
        await chat_service.run_turn(turn, data.user_text, user_id=user_id)
    return ChatResponse(
        conversation_id=turn.conversation_id, answer=turn.answer, request_id=data.request_id, sources=turn.sources
    )


@router.post("/stream")
async def stream_message(data: ChatRequest, user_id: CurrentUserId) -> StreamingResponse:
    """以 SSE 返回一轮对话：tool、delta 是过程预览，done 带已持久化的最终回复，error 表示本轮失败。"""
    # 校验、额度、会话归属等错误在发出响应头之前抛出，仍以 HTTP 状态码返回。
    turn = await chat_service.begin_turn(
        data.user_text, data.conversation_id, str(data.request_id), user_id=user_id
    )

    def done_event() -> tuple[str, dict[str, Any]]:
        return "done", {
            "conversation_id": turn.conversation_id,
            "answer": turn.answer,
            "request_id": str(data.request_id),
            "sources": turn.sources,
        }

    if turn.answer is not None:
        # 同一 request_id 已完成：直接回放结果，不再调用模型。
        replay = done_event()

        async def replay_events() -> AsyncIterator[str]:
            yield format_sse(*replay)

        return sse_response(replay_events())

    queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

    async def on_event(event: str, payload: dict[str, Any]) -> None:
        queue.put_nowait((event, payload))

    async def run() -> None:
        try:
            await chat_service.run_turn(turn, data.user_text, user_id=user_id, on_event=on_event)
            queue.put_nowait(done_event())
        except HTTPException as exc:
            queue.put_nowait(("error", {"detail": exc.detail}))
        except Exception:
            logger.exception("流式对话处理失败 request_id=%s", data.request_id)
            queue.put_nowait(("error", {"detail": "本轮对话处理失败，请用同一请求 ID 重试"}))

    # Agent 在独立任务中运行：客户端断开只取消下面的生成器，本轮仍会执行完并持久化，
    # 之后用同一 request_id 重试即可拿到结果。
    task = asyncio.create_task(run())
    running_turns.add(task)
    task.add_done_callback(running_turns.discard)

    async def events() -> AsyncIterator[str]:
        while True:
            event, payload = await queue.get()
            yield format_sse(event, payload)
            if event in ("done", "error"):
                return

    return sse_response(events())


def sse_response(events: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        # 禁止浏览器与反向代理缓存或攒批，保证每个事件立即送达。
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: UUID, session: DbSession, user_id: CurrentUserId) -> Response:
    conversation_service.delete_conversation(session, str(conversation_id), user_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
