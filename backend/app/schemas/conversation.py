"""会话存取使用的 DTO；ORM 定义位于 models/conversation.py。"""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class ChatRequest(BaseModel):
    user_text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    conversation_id: str | None = None
    request_id: UUID


class DocumentSource(BaseModel):
    """服务端根据本轮 read_document 结果生成的来源卡片（spec 3.4），不是模型写的链接。"""

    document_id: int
    title: str = Field(description="读取时的标题快照")
    heading_path: list[str]
    quote: str = Field(description="片段里的一段纯文本，前端用它在正文里定位；为空时按小节定位")


class ChatResponse(BaseModel):
    conversation_id: str
    answer: str
    request_id: UUID
    sources: list[DocumentSource] = []


class ConversationMessage(BaseModel):
    # 模型供应商可能附带额外字段；历史只保留下一轮调用必需的字段。
    model_config = ConfigDict(extra="ignore")

    role: Literal["user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def validate_role_fields(self):
        if self.role == "user" and self.content is None:
            raise ValueError("用户消息缺少文本内容")
        if self.role == "assistant" and self.content is None and not self.tool_calls:
            raise ValueError("助手消息缺少文本或工具调用")
        if self.role == "tool" and (self.content is None or not self.tool_call_id):
            raise ValueError("工具结果缺少内容或调用 ID")
        if self.role != "assistant" and self.tool_calls is not None:
            raise ValueError("只有助手消息可以包含工具调用")
        if self.role != "tool" and self.tool_call_id is not None:
            raise ValueError("只有工具结果可以包含调用 ID")
        return self

    def to_llm_message(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True, mode="json")


class ConversationRead(BaseModel):
    conversation_id: str
    message_count: int = Field(ge=0)
    history: list[ConversationMessage]

    def to_llm_history(self) -> list[dict[str, Any]]:
        return [message.to_llm_message() for message in self.history]
