"""页面内通知的响应模型。邮件发送状态不对用户展示（PRD R14）。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class NotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: Literal["reminder", "missed"]
    task_id: int
    task_title: str
    task_time: datetime
    created_at: datetime
    read_at: datetime | None


class UnreadCountRead(BaseModel):
    count: int
