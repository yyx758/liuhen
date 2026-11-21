"""接收一轮用户输入，并将其接入持久化会话。"""

from uuid import UUID

from fastapi import APIRouter, Response, status

from backend.app.api.deps import CurrentUserId, DbSession
from backend.app.schemas.conversation import ChatRequest, ChatResponse
from backend.app.services import chat_service, conversation_service


router = APIRouter(prefix="/chat", tags=["Chat"])


@router.post("", response_model=ChatResponse)
async def send_message(data: ChatRequest, user_id: CurrentUserId) -> ChatResponse:
    conversation_id, answer = await chat_service.send_message(
        data.user_text, data.conversation_id, str(data.request_id), user_id=user_id
    )
    return ChatResponse(conversation_id=conversation_id, answer=answer, request_id=data.request_id)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: UUID, session: DbSession, user_id: CurrentUserId) -> Response:
    conversation_service.delete_conversation(session, str(conversation_id), user_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
