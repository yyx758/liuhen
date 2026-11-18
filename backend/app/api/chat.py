"""接收一轮用户输入，并将其接入持久化会话。"""

from fastapi import APIRouter

from backend.app.schemas.conversation import ChatRequest, ChatResponse
from backend.app.services import chat_service


router = APIRouter(prefix="/chat", tags=["Chat"])


@router.post("", response_model=ChatResponse)
async def send_message(data: ChatRequest) -> ChatResponse:
    conversation_id, answer = await chat_service.send_message(
        data.user_text, data.conversation_id, str(data.request_id)
    )
    return ChatResponse(conversation_id=conversation_id, answer=answer, request_id=data.request_id)
