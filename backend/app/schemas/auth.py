from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str = Field(min_length=1, max_length=255, description="用户名或邮箱")
    password: str = Field(min_length=1, max_length=128)
    remember: bool = Field(default=False, description="保持登录 7 天；否则 12 小时无操作即失效")


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class ChatUsage(BaseModel):
    today: int = Field(description="今天（UTC+08:00）已用的对话轮数")
    limit: int | None = Field(description="每日上限；管理员不限量时为 null")


class UserRead(BaseModel):
    id: int
    username: str
    email: str | None
    role: str
    must_change_password: bool
    created_at: datetime
    chat_usage: ChatUsage | None = None
