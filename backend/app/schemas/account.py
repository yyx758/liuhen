"""注册、个人设置与管理后台的请求 / 响应模型。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

UserStatus = Literal["pending", "active", "rejected", "disabled"]


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    email: str | None = Field(default=None, max_length=255)


class RegisterRead(BaseModel):
    username: str
    status: UserStatus


class EmailChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str | None = Field(default=None, max_length=255, description="留空表示清除邮箱")
    current_password: str = Field(min_length=1, max_length=128)


class ReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=200)


class SessionRead(BaseModel):
    id: int
    created_at: datetime
    last_seen_at: datetime
    ip: str | None
    user_agent: str | None
    current: bool


class RevokedRead(BaseModel):
    revoked: int


class OwnEventRead(BaseModel):
    id: int
    event_type: str
    ip: str | None
    created_at: datetime


class AdminUserRead(BaseModel):
    id: int
    username: str
    email: str | None
    role: str
    status: UserStatus
    must_change_password: bool
    created_at: datetime
    last_login_at: datetime | None
    reviewed_at: datetime | None
    # 已停用 / 已拒绝账号最近一次停用或拒绝时填写的原因（取自账号事件）。
    status_reason: str | None = None
    # 用户自己设置的上限（为空表示用默认值）与实际生效的上限（管理员不限量时为空）。
    daily_chat_limit: int | None
    chat_limit: int | None
    chat_today: int
    chat_week: int
    # 最近 7 个自然日的轮数，从早到晚，最后一项是今天；只在账号详情中返回。
    chat_days: list[int] | None = None


class AdminUserPage(BaseModel):
    items: list[AdminUserRead]
    total: int
    page: int
    page_size: int
    # 各状态的账号数（不受搜索词影响），用于筛选 Tab 上的数字。
    counts: dict[str, int]
    # 全部用户今天与最近 7 天的对话轮数合计，以及默认的每日上限。
    chat_today: int
    chat_week: int
    default_chat_limit: int


class ChatLimitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 必须显式给出：数字表示自定义上限，null 表示恢复默认值。
    daily_chat_limit: int | None = Field(ge=1, le=1000)


class PendingCountRead(BaseModel):
    count: int


class TemporaryPasswordRead(BaseModel):
    temporary_password: str


class AccountEventRead(BaseModel):
    id: int
    event_type: str
    target_user_id: int
    actor_user_id: int | None
    target_username: str | None
    actor_username: str | None
    reason: str | None
    details: dict | None
    created_at: datetime
