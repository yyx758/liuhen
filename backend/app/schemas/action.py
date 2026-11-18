from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from backend.app.schemas.task import TaskRead


class PendingAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 操作 ID
    id: int

    # 目标待办 ID
    task_id: int

    # 修改或删除
    action_type: Literal["update", "delete"]

    # 操作之前的数据
    before: TaskRead

    # 操作之后的数据，删除时为 None
    after: TaskRead | None = None

    # 当前操作状态
    status: Literal[
        "pending",
        "executed",
        "cancelled",
        "expired",
        "conflicted",
        "replaced",
        "failed",
    ] = "pending"

    # 创建时间
    created_at: datetime

    # 过期时间
    expires_at: datetime
