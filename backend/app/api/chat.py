"""接收一轮用户输入，并将其接入持久化会话。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from backend.app.db.session import get_db
from backend.app.schemas.conversation import ChatRequest, ChatResponse
from backend.app.services import chat_service, conversation_service


router = APIRouter(prefix="/chat", tags=["Chat"])
DbSession = Annotated[Session, Depends(get_db)]


@router.post("", response_model=ChatResponse)
async def send_message(data: ChatRequest) -> ChatResponse:
    conversation_id, answer = await chat_service.send_message(
        data.user_text, data.conversation_id, str(data.request_id)
    )
    return ChatResponse(conversation_id=conversation_id, answer=answer, request_id=data.request_id)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: UUID, session: DbSession) -> Response:
    conversation_service.delete_conversation(session, str(conversation_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
