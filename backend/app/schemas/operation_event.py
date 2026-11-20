from datetime import datetime

from pydantic import BaseModel, ConfigDict


class OperationEventRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    event_type: str
    task_id: int
    action_id: int | None
    request_id: str | None
    conversation_id: str | None
    tool_call_id: str | None
    from_status: str | None
    to_status: str | None
    reason: str | None
    details: dict | None
    created_at: datetime
    subject_title: str | None = None


class AuditSummaryRead(BaseModel):
    total: int
    proposals: int
    executed: int
    cancelled: int
    expired: int
    failures: int
    requests: int


class AuditEventPageRead(BaseModel):
    items: list[OperationEventRead]
    page: int
    page_size: int
    total: int
    total_pages: int
    anchor_id: int | None
